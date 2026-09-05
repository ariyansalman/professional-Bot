"""Reseller webhook endpoint management (section 53)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.dependencies.auth import APIPrincipal, SessionDep, require_scope
from app.api.schemas.orders import WebhookCreatedOut, WebhookCreateIn, WebhookOut
from app.core.exceptions import NotFoundError
from app.db.repositories.resellers import WebhookRepository
from app.domain.enums import ApiScope, WebhookEvent
from app.domain.resellers.service import ResellerService

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.get("", response_model=list[WebhookOut], summary="List webhook endpoints")
async def list_webhooks(
    session: SessionDep,
    principal: Annotated[APIPrincipal, Depends(require_scope(ApiScope.WEBHOOKS_MANAGE))],
) -> list[WebhookOut]:
    endpoints = await WebhookRepository(session).list_for_reseller(principal.reseller.id)
    return [
        WebhookOut(
            id=str(endpoint.id),
            url=endpoint.url,
            events=list(endpoint.events or []),
            is_active=endpoint.is_active,
            health=endpoint.health,
            created_at=endpoint.created_at,
        )
        for endpoint in endpoints
    ]


@router.post(
    "",
    response_model=WebhookCreatedOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register a webhook endpoint",
    description=(
        "Registers an HTTPS endpoint. The signing secret is returned once and "
        "cannot be retrieved again. Every delivery carries `X-Event-Id`, "
        "`X-Timestamp` and `X-Signature: v1=hex(hmac_sha256(secret, "
        "\"<timestamp>.<event_id>.<raw_body>\"))`."
    ),
)
async def create_webhook(
    payload: WebhookCreateIn,
    session: SessionDep,
    principal: Annotated[APIPrincipal, Depends(require_scope(ApiScope.WEBHOOKS_MANAGE))],
) -> WebhookCreatedOut:
    endpoint, secret = await ResellerService(session).register_webhook(
        account=principal.reseller,
        url=payload.url,
        events=payload.events,
        description=payload.description,
    )
    return WebhookCreatedOut(
        id=str(endpoint.id),
        url=endpoint.url,
        events=list(endpoint.events or []),
        is_active=endpoint.is_active,
        health=endpoint.health,
        created_at=endpoint.created_at,
        secret=secret,
    )


@router.delete(
    "/{webhook_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Disable a webhook endpoint",
)
async def delete_webhook(
    webhook_id: str,
    session: SessionDep,
    principal: Annotated[APIPrincipal, Depends(require_scope(ApiScope.WEBHOOKS_MANAGE))],
) -> None:
    repo = WebhookRepository(session)
    try:
        identifier = uuid.UUID(webhook_id)
    except ValueError as exc:
        raise NotFoundError("invalid webhook id", safe_message="Webhook not found.") from exc

    endpoint = await repo.get(identifier)
    if endpoint is None or endpoint.reseller_id != principal.reseller.id:
        raise NotFoundError("webhook not found", safe_message="Webhook not found.")

    # Disabled rather than deleted: the delivery log stays auditable.
    endpoint.is_active = False
    await session.flush()


@router.get("/events", response_model=list[str], summary="List available event types")
async def list_events(
    principal: Annotated[APIPrincipal, Depends(require_scope(ApiScope.WEBHOOKS_MANAGE))],
) -> list[str]:
    return [event.value for event in WebhookEvent]
