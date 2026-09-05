"""Reseller API tests: authentication, scopes, idempotency and isolation."""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api.app import create_app
from app.db.repositories.resellers import ApiKeyRepository
from app.domain.enums import ApiScope, ResellerStatus
from tests.factories import add_stock, make_category, make_product, make_user

ALL_SCOPES = [
    ApiScope.PRODUCTS_READ,
    ApiScope.ORDERS_CREATE,
    ApiScope.ORDERS_READ,
    ApiScope.PAYMENTS_READ,
    ApiScope.DELIVERIES_READ,
    ApiScope.WEBHOOKS_MANAGE,
]


async def _make_reseller(session, *, telegram_id: int, scopes=None, status=ResellerStatus.ACTIVE):
    from app.db.models.reseller import ResellerAccount

    user = await make_user(session, telegram_id=telegram_id)
    account = ResellerAccount(
        user_id=user.id,
        business_name=f"Shop {telegram_id}",
        status=status,
        rate_limit_per_minute=1000,
    )
    session.add(account)
    await session.flush()
    record, plaintext = await ApiKeyRepository(session).create(
        reseller_id=account.id,
        name="test key",
        scopes=list(scopes if scopes is not None else ALL_SCOPES),
    )
    await session.commit()
    return account, plaintext


@pytest_asyncio.fixture
async def api_client(sessionmaker_):
    """ASGI client bound to the test sessionmaker (no network involved)."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def reseller(session, sessionmaker_):
    return await _make_reseller(session, telegram_id=5001)


@pytest_asyncio.fixture
async def catalog(session):
    category = await make_category(session)
    product = await make_product(
        session,
        price="20.00",
        sku="API-SKU-1",
        category=category,
        available_to_resellers=True,
        reseller_price=Decimal("16.00"),
        reseller_min_price=Decimal("18.00"),
        reseller_recommended_price=Decimal("25.00"),
    )
    await add_stock(session, product, count=5)
    await session.commit()
    return product


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- authentication -------------------------------------------------------


async def test_missing_credentials_are_rejected(api_client, catalog):
    response = await api_client.get("/api/v1/products")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


async def test_malformed_key_is_rejected(api_client, catalog):
    response = await api_client.get("/api/v1/products", headers=auth("not-a-key"))
    assert response.status_code == 401


async def test_unknown_key_gives_the_same_error_as_a_revoked_key(api_client, session, catalog):
    """Failures must be indistinguishable so keys cannot be probed."""
    account, token = await _make_reseller(session, telegram_id=5002)
    unknown = await api_client.get(
        "/api/v1/products", headers=auth("rt_live_deadbeef_" + "x" * 40)
    )

    key = (await ApiKeyRepository(session).list_for_reseller(account.id))[0]
    await ApiKeyRepository(session).revoke(key, None)
    await session.commit()
    revoked = await api_client.get("/api/v1/products", headers=auth(token))

    assert unknown.status_code == revoked.status_code == 401
    assert unknown.json()["error"] == revoked.json()["error"] | {
        "request_id": unknown.json()["error"]["request_id"]
    }


async def test_suspended_reseller_cannot_use_the_api(api_client, session, catalog):
    account, token = await _make_reseller(session, telegram_id=5003)
    account.status = ResellerStatus.SUSPENDED
    await session.commit()

    response = await api_client.get("/api/v1/products", headers=auth(token))
    assert response.status_code == 401


# --- scopes ---------------------------------------------------------------


async def test_scope_is_enforced(api_client, session, catalog):
    _, token = await _make_reseller(
        session, telegram_id=5004, scopes=[ApiScope.PRODUCTS_READ]
    )
    allowed = await api_client.get("/api/v1/products", headers=auth(token))
    assert allowed.status_code == 200

    denied = await api_client.post(
        "/api/v1/orders",
        headers=auth(token),
        json={"product_id": str(catalog.id), "quantity": 1},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "permission_denied"


# --- products -------------------------------------------------------------


async def test_products_expose_reseller_pricing_only(api_client, reseller, catalog):
    _, token = reseller
    response = await api_client.get("/api/v1/products", headers=auth(token))
    assert response.status_code == 200
    body = response.json()

    assert body["meta"]["total"] == 1
    product = body["data"][0]
    assert product["sku"] == "API-SKU-1"
    assert product["pricing"]["wholesale_price"] == "16.00000000"
    assert product["pricing"]["minimum_price"] == "18.00000000"
    assert product["in_stock"] is True
    assert product["available_quantity"] == 5
    # Internal fulfilment data must never be serialised.
    assert "delivery_payload" not in product
    assert "delivery_file_id" not in product


async def test_products_hidden_from_resellers_are_not_listed(api_client, session, reseller):
    await make_product(session, sku="PRIVATE-1", available_to_resellers=False)
    await session.commit()
    _, token = reseller
    response = await api_client.get("/api/v1/products", headers=auth(token))
    skus = {item["sku"] for item in response.json()["data"]}
    assert "PRIVATE-1" not in skus


# --- orders ---------------------------------------------------------------


async def test_create_order_uses_reseller_pricing(api_client, reseller, catalog):
    _, token = reseller
    response = await api_client.post(
        "/api/v1/orders",
        headers=auth(token),
        json={
            "product_id": str(catalog.id),
            "quantity": 2,
            "customer_reference": "cust-1",
            "reseller_reference": "my-order-1",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    # 2 x wholesale 16.00, not 2 x list 20.00
    assert body["total"] == "32.00000000"
    assert body["customer_reference"] == "cust-1"
    assert body["reseller_reference"] == "my-order-1"
    assert body["status"] == "payment_pending"


async def test_order_creation_is_idempotent(api_client, reseller, catalog):
    _, token = reseller
    payload = {"product_id": str(catalog.id), "quantity": 1}
    headers = auth(token) | {"Idempotency-Key": "order-key-1"}

    first = await api_client.post("/api/v1/orders", headers=headers, json=payload)
    second = await api_client.post("/api/v1/orders", headers=headers, json=payload)

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["reference"] == second.json()["reference"]


async def test_reusing_a_key_with_a_different_body_is_rejected(api_client, reseller, catalog):
    _, token = reseller
    headers = auth(token) | {"Idempotency-Key": "order-key-2"}

    await api_client.post(
        "/api/v1/orders", headers=headers, json={"product_id": str(catalog.id), "quantity": 1}
    )
    conflict = await api_client.post(
        "/api/v1/orders", headers=headers, json={"product_id": str(catalog.id), "quantity": 3}
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


async def test_unknown_product_is_rejected(api_client, reseller, catalog):
    _, token = reseller
    response = await api_client.post(
        "/api/v1/orders",
        headers=auth(token),
        json={"product_id": "00000000-0000-0000-0000-000000000000", "quantity": 1},
    )
    assert response.status_code == 404


async def test_invalid_quantity_is_rejected(api_client, reseller, catalog):
    _, token = reseller
    response = await api_client.post(
        "/api/v1/orders",
        headers=auth(token),
        json={"product_id": str(catalog.id), "quantity": 0},
    )
    assert response.status_code == 422


async def test_reseller_cannot_read_another_resellers_order(
    api_client, session, reseller, catalog
):
    """Cross-tenant isolation, reported as 404 rather than 403."""
    _, token_a = reseller
    created = await api_client.post(
        "/api/v1/orders", headers=auth(token_a), json={"product_id": str(catalog.id), "quantity": 1}
    )
    order_id = created.json()["id"]

    _, token_b = await _make_reseller(session, telegram_id=5005)
    response = await api_client.get(f"/api/v1/orders/{order_id}", headers=auth(token_b))
    assert response.status_code == 404


async def test_delivery_is_empty_before_payment(api_client, reseller, catalog):
    _, token = reseller
    created = await api_client.post(
        "/api/v1/orders", headers=auth(token), json={"product_id": str(catalog.id), "quantity": 1}
    )
    order_id = created.json()["id"]

    response = await api_client.get(f"/api/v1/orders/{order_id}/delivery", headers=auth(token))
    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    assert response.json()["items"] == []


# --- webhooks -------------------------------------------------------------


async def test_webhook_registration_returns_the_secret_once(api_client, reseller):
    _, token = reseller
    response = await api_client.post(
        "/api/v1/webhooks",
        headers=auth(token),
        json={"url": "https://example.com/hooks", "events": ["order.created"]},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["secret"].startswith("whsec_")

    listed = await api_client.get("/api/v1/webhooks", headers=auth(token))
    assert listed.status_code == 200
    # The secret is never returned again.
    assert "secret" not in listed.json()[0]


async def test_webhook_url_must_not_target_internal_infrastructure(api_client, reseller):
    _, token = reseller
    for url in (
        "http://example.com/hook",
        "https://169.254.169.254/latest/meta-data",
        "https://localhost/hook",
    ):
        response = await api_client.post(
            "/api/v1/webhooks", headers=auth(token), json={"url": url}
        )
        assert response.status_code == 422, url


# --- health ---------------------------------------------------------------


async def test_health_endpoint_exposes_no_secrets(api_client):
    response = await api_client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    serialised = response.text.lower()
    for leak in ("password", "postgres://", "redis://", "token", "secret"):
        assert leak not in serialised


async def test_request_id_is_returned(api_client):
    response = await api_client.get("/health")
    assert response.headers.get("X-Request-ID", "").startswith("api_")
