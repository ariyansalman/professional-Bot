"""Outbound webhook delivery: signing, retry, backoff and SSRF re-validation.

HTTP is mocked with respx, so nothing leaves the process, but the worker, the
signing, the retry policy and the database bookkeeping are all real. This path
previously crashed on its success branch with no test to catch it.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest_asyncio
import respx

from app.core.security import generate_webhook_secret, get_secret_box
from app.db.models.reseller import ResellerAccount, WebhookEndpoint
from app.db.repositories.resellers import WebhookDeliveryRepository
from app.domain.enums import ResellerStatus, WebhookDeliveryStatus, WebhookEvent
from app.workers.webhooks.dispatcher import WebhookWorker
from tests.factories import make_user

ENDPOINT_URL = "https://reseller.example/hooks"


@pytest_asyncio.fixture
async def endpoint(session) -> tuple[WebhookEndpoint, str]:
    user = await make_user(session, telegram_id=6001)
    account = ResellerAccount(
        user_id=user.id, business_name="Hook Shop", status=ResellerStatus.ACTIVE
    )
    session.add(account)
    await session.flush()

    secret = generate_webhook_secret()
    record = WebhookEndpoint(
        reseller_id=account.id,
        url=ENDPOINT_URL,
        encrypted_secret=get_secret_box().encrypt(secret),
        secret_hint=secret[:12],
        events=[],
        is_active=True,
    )
    session.add(record)
    await session.flush()
    return record, secret


async def _queue(session, endpoint: WebhookEndpoint, event_id: str = "evt_1"):
    return await WebhookDeliveryRepository(session).enqueue(
        endpoint_id=endpoint.id,
        event_id=event_id,
        event_type=WebhookEvent.PAYMENT_VERIFIED.value,
        payload={"id": event_id, "type": "payment.verified", "data": {"order": "TG-1"}},
    )


@respx.mock
async def test_successful_delivery_is_signed_and_recorded(session, endpoint, sessionmaker_):
    record, secret = endpoint
    delivery = await _queue(session, record)
    await session.commit()

    route = respx.post(ENDPOINT_URL).mock(return_value=httpx.Response(200))
    processed = await WebhookWorker().run_once()
    assert processed == 1

    assert route.called
    request = route.calls[0].request

    # The signature must verify against the raw body, exactly as documented.
    timestamp = int(request.headers["X-Timestamp"])
    event_id = request.headers["X-Event-Id"]
    payload = f"{timestamp}.{event_id}.".encode() + request.content
    expected = "v1=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    assert request.headers["X-Signature"] == expected

    assert request.headers["X-Event-Type"] == "payment.verified"
    assert json.loads(request.content)["type"] == "payment.verified"

    await session.refresh(delivery)
    assert delivery.status is WebhookDeliveryStatus.DELIVERED
    assert delivery.delivered_at is not None
    assert delivery.last_status_code == 200

    await session.refresh(record)
    assert record.consecutive_failures == 0
    assert record.last_success_at is not None


@respx.mock
async def test_a_failure_schedules_a_backed_off_retry(session, endpoint, sessionmaker_):
    record, _ = endpoint
    delivery = await _queue(session, record, event_id="evt_fail")
    await session.commit()

    respx.post(ENDPOINT_URL).mock(return_value=httpx.Response(500))
    await WebhookWorker().run_once()

    await session.refresh(delivery)
    assert delivery.status is WebhookDeliveryStatus.FAILED
    assert delivery.attempts == 1
    assert delivery.next_attempt_at is not None, "a failed delivery must be retried"
    assert delivery.last_status_code == 500

    await session.refresh(record)
    assert record.consecutive_failures == 1


@respx.mock
async def test_delivery_is_exhausted_rather_than_retried_forever(
    session, endpoint, sessionmaker_
):
    from app.workers.webhooks.dispatcher import MAX_ATTEMPTS

    record, _ = endpoint
    delivery = await _queue(session, record, event_id="evt_exhaust")
    delivery.attempts = MAX_ATTEMPTS - 1
    await session.commit()

    respx.post(ENDPOINT_URL).mock(return_value=httpx.Response(503))
    await WebhookWorker().run_once()

    await session.refresh(delivery)
    assert delivery.status is WebhookDeliveryStatus.EXHAUSTED
    assert delivery.next_attempt_at is None


@respx.mock
async def test_a_transport_error_is_retried_not_lost(session, endpoint, sessionmaker_):
    record, _ = endpoint
    delivery = await _queue(session, record, event_id="evt_timeout")
    await session.commit()

    respx.post(ENDPOINT_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
    await WebhookWorker().run_once()

    await session.refresh(delivery)
    assert delivery.status is WebhookDeliveryStatus.FAILED
    assert delivery.next_attempt_at is not None
    assert "ConnectTimeout" in (delivery.last_error or "")


@respx.mock
async def test_an_endpoint_that_became_internal_is_blocked_at_delivery_time(
    session, endpoint, sessionmaker_
):
    """SSRF is re-checked at delivery, because DNS can change after registration."""
    record, _ = endpoint
    record.url = "https://169.254.169.254/latest/meta-data"
    delivery = await _queue(session, record, event_id="evt_ssrf")
    await session.commit()

    route = respx.post("https://169.254.169.254/latest/meta-data").mock(
        return_value=httpx.Response(200)
    )
    await WebhookWorker().run_once()

    assert not route.called, "the worker must never call an internal address"
    await session.refresh(delivery)
    assert delivery.status is WebhookDeliveryStatus.EXHAUSTED
    assert "blocked" in (delivery.last_error or "").lower()


async def test_the_same_event_is_never_queued_twice(session, endpoint, sessionmaker_):
    record, _ = endpoint
    first = await _queue(session, record, event_id="evt_dupe")
    second = await _queue(session, record, event_id="evt_dupe")

    assert first is not None
    assert second is None, "a replayed internal event must not queue a second delivery"


@respx.mock
async def test_a_disabled_endpoint_receives_nothing(session, endpoint, sessionmaker_):
    record, _ = endpoint
    record.is_active = False
    delivery = await _queue(session, record, event_id="evt_disabled")
    await session.commit()

    route = respx.post(ENDPOINT_URL).mock(return_value=httpx.Response(200))
    await WebhookWorker().run_once()

    assert not route.called
    await session.refresh(delivery)
    assert delivery.status is WebhookDeliveryStatus.EXHAUSTED
