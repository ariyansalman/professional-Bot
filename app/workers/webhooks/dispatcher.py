"""Outbound webhook delivery with signing, retry and backoff (section 112)."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx

from app.core.logging import get_logger
from app.core.security import assert_safe_outbound_url, get_secret_box, sign_webhook
from app.core.timeutils import utcnow
from app.db.repositories.resellers import WebhookDeliveryRepository, WebhookRepository
from app.db.session import session_scope
from app.domain.enums import WebhookDeliveryStatus
from app.workers.base import PeriodicWorker

log = get_logger(__name__)

MAX_ATTEMPTS = 8
#: Exponential backoff between attempts, capped so a broken endpoint does not
#: retry forever at a high rate.
BACKOFF_SECONDS = [30, 120, 300, 900, 1800, 3600, 7200, 21600]
REQUEST_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)


class WebhookWorker(PeriodicWorker):
    """Delivers queued webhook events.

    Each delivery is signed and carries a stable ``X-Event-Id`` so the receiver
    can deduplicate. Delivery is at-least-once: the receiver must be idempotent,
    which the API documentation states explicitly.
    """

    name = "webhooks"
    interval = 10.0

    async def run_once(self) -> int:
        async with session_scope() as session:
            due = await WebhookDeliveryRepository(session).due(limit=25)
            delivery_ids = [d.id for d in due]

        if not delivery_ids:
            return 0

        processed = 0
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=False) as client:
            for delivery_id in delivery_ids:
                if self.is_stopping:
                    break
                processed += await self._deliver(client, delivery_id)
        return processed

    async def _deliver(self, client: httpx.AsyncClient, delivery_id) -> int:
        async with session_scope() as session:
            deliveries = WebhookDeliveryRepository(session)
            endpoints = WebhookRepository(session)

            delivery = await deliveries.get(delivery_id)
            if delivery is None or delivery.status is WebhookDeliveryStatus.DELIVERED:
                return 0

            endpoint = await endpoints.get(delivery.endpoint_id)
            if endpoint is None or not endpoint.is_active:
                delivery.status = WebhookDeliveryStatus.EXHAUSTED
                delivery.last_error = "endpoint disabled"
                await session.flush()
                return 0

            try:
                # Re-validated at delivery time: an endpoint registered before a
                # DNS change must not become an SSRF vector.
                assert_safe_outbound_url(endpoint.url)
            except Exception as exc:
                delivery.status = WebhookDeliveryStatus.EXHAUSTED
                delivery.last_error = f"blocked url: {exc}"[:512]
                await endpoints.record_failure(endpoint, None)
                log.warning("webhook.blocked_url", endpoint_id=str(endpoint.id))
                return 0

            secret = get_secret_box().decrypt(endpoint.encrypted_secret)
            body = json.dumps(delivery.payload, separators=(",", ":"), default=str).encode()
            timestamp = int(utcnow().timestamp())
            signature = sign_webhook(secret, timestamp, delivery.event_id, body)

            delivery.attempts += 1
            started = utcnow()
            status_code: int | None = None
            error: str | None = None

            try:
                response = await client.post(
                    endpoint.url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "telegram-commerce-webhooks/1.0",
                        "X-Event-Id": delivery.event_id,
                        "X-Event-Type": delivery.event_type,
                        "X-Timestamp": str(timestamp),
                        "X-Signature": signature,
                        "X-Delivery-Attempt": str(delivery.attempts),
                    },
                )
                status_code = response.status_code
            except httpx.HTTPError as exc:
                error = f"{type(exc).__name__}: {exc}"[:400]

            delivery.duration_ms = int((utcnow() - started).total_seconds() * 1000)
            delivery.last_status_code = status_code

            if status_code is not None and 200 <= status_code < 300:
                delivery.status = WebhookDeliveryStatus.DELIVERED
                delivery.delivered_at = utcnow()
                delivery.last_error = None
                delivery.next_attempt_at = None
                await endpoints.record_success(endpoint, status_code)
                log.info(
                    "webhook.delivered",
                    event_type=delivery.event_type,
                    endpoint_id=str(endpoint.id),
                    attempts=delivery.attempts,
                    status=status_code,
                )
                return 1

            delivery.last_error = error or f"HTTP {status_code}"
            await endpoints.record_failure(endpoint, status_code)

            if delivery.attempts >= MAX_ATTEMPTS:
                delivery.status = WebhookDeliveryStatus.EXHAUSTED
                delivery.next_attempt_at = None
                log.warning(
                    "webhook.exhausted",
                    event_type=delivery.event_type,
                    endpoint_id=str(endpoint.id),
                    attempts=delivery.attempts,
                    last_error=delivery.last_error,
                )
            else:
                backoff = BACKOFF_SECONDS[min(delivery.attempts - 1, len(BACKOFF_SECONDS) - 1)]
                delivery.status = WebhookDeliveryStatus.FAILED
                delivery.next_attempt_at = utcnow() + timedelta(seconds=backoff)
                log.info(
                    "webhook.retry_scheduled",
                    event_type=delivery.event_type,
                    attempts=delivery.attempts,
                    retry_in=backoff,
                )
            await session.flush()
            return 1
