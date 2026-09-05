"""Admin panel tests: every screen rendered through the real dispatcher.

The admin panel is large and mostly reachable only after authentication, so
these walk it screen by screen. They catch runtime errors that no import check
would, and they assert the access-control rules rather than assuming them.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from app.bot.callbacks import AdminCB
from app.db.models.user import Role
from tests.factories import add_stock, make_category, make_payment_method, make_product
from tests.integration.conftest import (
    TELEGRAM_ID,
    callback_update,
    feed,
    message_update,
)

ADMIN_SECTIONS = [
    "dashboard",
    "orders",
    "payments",
    "products",
    "inventory",
    "users",
    "resellers",
    "support",
    "coupons",
    "analytics",
    "providers",
    "reconciliation",
    "audit",
    "broadcast",
    "settings",
    "refunds",
]


@pytest_asyncio.fixture
async def admin_session(session, monkeypatch):
    """Grant the test user SUPER_ADMIN via the bootstrap path."""
    from app.admin.permissions.rbac import ROLE_PERMISSIONS
    from app.core.config import get_settings
    from app.db.models.user import Permission

    settings = get_settings()
    monkeypatch.setattr(settings.telegram, "bootstrap_admin_ids", [TELEGRAM_ID])

    permissions = {}
    for code in {p.value for perms in ROLE_PERMISSIONS.values() for p in perms}:
        permission = Permission(code=code, description=code)
        session.add(permission)
        permissions[code] = permission
    await session.flush()

    for role_name, granted in ROLE_PERMISSIONS.items():
        role = Role(name=role_name, description=role_name.value)
        role.permissions = [permissions[p.value] for p in granted]
        session.add(role)
    await session.commit()
    return session


async def _open_admin(harness) -> None:
    await feed(harness, message_update("/start"))
    await feed(harness, message_update("/admin", update_id=100))


@pytest.mark.parametrize("section", ADMIN_SECTIONS)
async def test_every_admin_section_renders(bot_harness, admin_session, section):
    """Each panel must render real content, not an error."""
    await _open_admin(bot_harness)
    recorder = await feed(
        bot_harness, callback_update(AdminCB(section=section).pack(), update_id=101)
    )

    assert recorder.texts, f"admin section {section!r} rendered nothing"
    body = recorder.texts[0]
    assert "Something went wrong" not in body, f"{section} errored: {body[:200]}"
    assert "expired" not in body.lower(), f"{section} was treated as a stale callback"
    assert recorder.buttons, f"admin section {section!r} rendered no navigation"


async def test_dashboard_leads_with_actionable_items(bot_harness, admin_session):
    await _open_admin(bot_harness)
    recorder = await feed(
        bot_harness, callback_update(AdminCB(section="dashboard").pack(), update_id=102)
    )
    body = recorder.texts[0]
    assert "ADMIN DASHBOARD" in body
    assert "Today" in body
    # With an empty system there is nothing to action, and it says so.
    assert "Nothing needs attention" in body


async def test_products_section_lists_and_opens_a_product(bot_harness, admin_session, session):
    category = await make_category(session)
    product = await make_product(session, category=category)
    await add_stock(session, product, count=4)
    await session.commit()

    await _open_admin(bot_harness)
    listing = await feed(
        bot_harness, callback_update(AdminCB(section="products").pack(), update_id=103)
    )
    assert "PRODUCTS" in listing.texts[0]
    assert "Premium License" in listing.texts[0]
    assert any("Add product" in b for b in listing.buttons)

    detail = await feed(
        bot_harness,
        callback_update(
            AdminCB(section="products", action="view", arg=product.id.hex).pack(), update_id=104
        ),
    )
    body = detail.texts[0]
    assert "Premium License" in body
    assert "Available: 4" in body
    assert any("Add stock" in b for b in detail.buttons)


async def test_inventory_never_shows_the_secret_payload(bot_harness, admin_session, session):
    """Admin screens show a preview, never the key itself."""
    product = await make_product(session)
    await add_stock(session, product, count=3)
    await session.commit()

    await _open_admin(bot_harness)
    recorder = await feed(
        bot_harness,
        callback_update(
            AdminCB(section="inventory", action="view", arg=product.id.hex).pack(), update_id=105
        ),
    )
    body = recorder.texts[0]
    assert "Available: <b>3</b>" in body
    # The factory writes payloads like "XXXX-YYYY-ZZZZ-0000"; only the masked
    # preview may appear.
    assert "XXXX-YYYY" not in body
    assert "****" in body


async def test_provider_screen_never_reveals_credentials(bot_harness, admin_session, session):
    from app.core.security import get_secret_box
    from app.db.models.payment import PaymentProvider

    method = await make_payment_method(session)
    provider = await session.get(PaymentProvider, method.provider_id)
    box = get_secret_box()
    provider.encrypted_api_key = box.encrypt("SUPERSECRETKEY123456")
    provider.encrypted_api_secret = box.encrypt("SUPERSECRETSECRET789")
    provider.api_key_hint = "3456"
    await session.commit()

    await _open_admin(bot_harness)
    recorder = await feed(
        bot_harness,
        callback_update(
            AdminCB(section="providers", action="view", arg=provider.id.hex).pack(),
            update_id=106,
        ),
    )
    body = recorder.texts[0]
    assert "SUPERSECRETKEY" not in body
    assert "SUPERSECRETSECRET" not in body
    assert provider.encrypted_api_key not in body
    # Only the masked hint and a configured/not-set indicator, so an operator
    # can tell which key is in place without it being readable.
    assert "****3456" in body
    assert "configured" in body


async def test_payments_review_queue_renders(bot_harness, admin_session):
    await _open_admin(bot_harness)
    recorder = await feed(
        bot_harness, callback_update(AdminCB(section="payments").pack(), update_id=107)
    )
    body = recorder.texts[0]
    assert "PAYMENTS" in body
    assert any("Review" in b for b in recorder.buttons)


async def test_high_risk_action_requires_a_reason_then_a_confirmation(
    bot_harness, admin_session, session
):
    """Section 114: approval is never one tap."""

    from app.db.repositories.users import UserRepository
    from app.domain.enums import OrderStatus
    from app.domain.orders.service import OrderService
    from app.domain.payments.service import PaymentService

    product = await make_product(session, price="10.00")
    await add_stock(session, product, count=2)
    method = await make_payment_method(session)
    user = await UserRepository(session).get_by_telegram_id(TELEGRAM_ID)
    if user is None:
        from tests.factories import make_user

        user = await make_user(session, telegram_id=TELEGRAM_ID)

    orders = OrderService(session)
    quote = await orders.quote(product=product, quantity=1, user=user)
    order = await orders.create_order(quote=quote, user=user)
    await orders.transition(order, OrderStatus.PAYMENT_PENDING)
    payments = PaymentService(session)
    intent = await payments.create_intent(order=order, method=method)
    await payments._to_review(intent, "needs a human")
    await session.commit()

    await _open_admin(bot_harness)

    detail = await feed(
        bot_harness,
        callback_update(
            AdminCB(section="payments", action="view", arg=intent.id.hex).pack(), update_id=108
        ),
    )
    assert "PAYMENT REVIEW" in detail.texts[0]
    assert "needs a human" in detail.texts[0]
    assert any("Approve" in b for b in detail.buttons)

    # Tapping Approve asks for a reason rather than approving.
    approve = await feed(
        bot_harness,
        callback_update(
            AdminCB(section="payments", action="approve", arg=intent.id.hex).pack(),
            update_id=109,
        ),
    )
    assert "reason" in approve.texts[0].lower()
    await session.refresh(intent)
    assert intent.status.value == "under_review", "approval must not happen on the first tap"

    # Supplying a reason produces a confirmation, still not an approval.
    confirm = await feed(
        bot_harness, message_update("customer confirmed by email", update_id=110)
    )
    assert "CONFIRM APPROVE" in confirm.texts[0]
    assert any("Approve" in b for b in confirm.buttons)
    await session.refresh(intent)
    assert intent.status.value == "under_review", "approval must not happen before confirmation"


async def test_refund_flow_requires_amount_reason_then_confirmation(
    bot_harness, admin_session, session, monkeypatch
):
    """A refund is never one tap: amount + reason, then an explicit confirm."""
    from tests.integration.test_refunds import _paid_order

    order, _ = await _paid_order(session, monkeypatch, price="20.00")
    await session.commit()

    await _open_admin(bot_harness)

    start = await feed(
        bot_harness,
        callback_update(
            AdminCB(section="refunds", action="new", arg=order.id.hex).pack(), update_id=120
        ),
    )
    assert "CREATE REFUND" in start.texts[0]
    assert "Refundable" in start.texts[0]

    entered = await feed(bot_harness, message_update("5.00 duplicate payment", update_id=121))
    assert "CONFIRM REFUND" in entered.texts[0]
    assert "duplicate payment" in entered.texts[0]

    from app.db.repositories.orders import RefundRepository

    assert await RefundRepository(session).list_for_order(order.id) == [], (
        "no refund may be recorded before the confirmation"
    )


async def test_a_non_admin_cannot_reach_any_admin_section(bot_harness, session):
    """No admin callback may render for an ordinary customer."""
    await feed(bot_harness, message_update("/start"))
    for section in ("dashboard", "payments", "providers", "settings"):
        recorder = await feed(
            bot_harness, callback_update(AdminCB(section=section).pack(), update_id=111)
        )
        rendered = " ".join(recorder.texts)
        assert "ADMIN" not in rendered.upper() or "expired" in rendered.lower(), (
            f"customer reached admin section {section!r}: {rendered[:160]}"
        )
