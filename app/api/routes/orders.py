"""Reseller order endpoints (sections 51-53)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.dependencies.auth import APIPrincipal, SessionDep, require_scope
from app.api.dependencies.idempotency import IdempotencyGuard, idempotency
from app.api.schemas.common import PageMeta, Paginated
from app.api.schemas.orders import (
    DeliveryOut,
    OrderCreateIn,
    OrderItemOut,
    OrderOut,
    PaymentOut,
)
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.models.order import Order
from app.db.repositories.catalog import ProductRepository
from app.db.repositories.orders import DeliveryRepository, OrderRepository
from app.db.repositories.payments import PaymentIntentRepository, PaymentMethodRepository
from app.db.repositories.resellers import ResellerRepository
from app.domain.enums import ApiScope, DeliveryStatus, OrderStatus, WebhookEvent
from app.domain.orders.delivery import DeliveryService
from app.domain.orders.service import OrderService
from app.domain.payments.service import PaymentService
from app.domain.resellers.service import ResellerService

log = get_logger(__name__)
router = APIRouter(prefix="/orders", tags=["orders"])


async def _serialise(session, order: Order, *, include_delivery: bool = False) -> OrderOut:
    """Build the reseller's view of an order.

    Internal ids, verification evidence and inventory item references are never
    exposed; the reseller sees the state machine and the payment instructions.
    """
    intent = await PaymentIntentRepository(session).latest_for_order(order.id)
    payment = None
    if intent is not None:
        payment = PaymentOut(
            reference=intent.reference,
            status=intent.status.value,
            asset=intent.asset,
            network=intent.network.value,
            amount=intent.expected_amount,
            destination=intent.destination,
            memo=intent.memo,
            required_confirmations=intent.required_confirmations,
            confirmations=intent.confirmations,
            received_amount=intent.received_amount,
            expires_at=intent.expires_at,
            verified_at=intent.verified_at,
        )

    deliveries = await DeliveryRepository(session).list_for_order(order.id)
    if not deliveries:
        delivery_status = "pending"
    elif all(d.status is DeliveryStatus.COMPLETED for d in deliveries):
        delivery_status = "completed"
    elif any(d.status is DeliveryStatus.FAILED for d in deliveries):
        delivery_status = "retrying"
    else:
        delivery_status = "processing"

    return OrderOut(
        id=str(order.id),
        reference=order.reference,
        status=order.status.value,
        currency=order.currency,
        subtotal=order.subtotal,
        discount_total=order.discount_total,
        total=order.total,
        customer_reference=order.customer_reference,
        reseller_reference=order.reseller_reference,
        items=[
            OrderItemOut(
                product_id=str(item.product_id) if item.product_id else None,
                product_name=item.product_name,
                sku=item.product_sku,
                quantity=item.quantity,
                unit_price=item.unit_price,
                line_total=item.line_total,
            )
            for item in order.items
        ],
        payment=payment,
        delivery_status=delivery_status,
        created_at=order.created_at,
        paid_at=order.paid_at,
        completed_at=order.completed_at,
        metadata=order.order_metadata or {},
    )


@router.post(
    "",
    response_model=OrderOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an order",
    description=(
        "Creates an order and, when a payment method is supplied, its payment "
        "intent. Send an `Idempotency-Key` header: replaying the same key with "
        "the same body returns the original order instead of creating a second one."
    ),
)
async def create_order(
    payload: OrderCreateIn,
    response: Response,
    session: SessionDep,
    principal: Annotated[APIPrincipal, Depends(require_scope(ApiScope.ORDERS_CREATE))],
    guard: Annotated[IdempotencyGuard, Depends(idempotency("orders.create"))],
) -> OrderOut:
    body = payload.model_dump(mode="json")

    replayed = await guard.replay(body)
    if replayed is not None:
        response.status_code = status.HTTP_200_OK
        return OrderOut.model_validate(replayed)

    # Second line of defence: even without an Idempotency-Key header, a repeated
    # reseller_reference must not create a duplicate order.
    if guard.key:
        existing = await OrderRepository(session).get_by_idempotency_key(
            principal.reseller.id, guard.key
        )
        if existing is not None:
            response.status_code = status.HTTP_200_OK
            return await _serialise(session, existing)

    try:
        product_id = uuid.UUID(payload.product_id)
    except ValueError as exc:
        raise ValidationError(
            f"invalid product id {payload.product_id}", safe_message="Unknown product."
        ) from exc

    product = await ProductRepository(session).get_for_reseller(product_id)
    if product is None:
        raise NotFoundError(
            f"product {product_id} not available to resellers",
            safe_message="Product not found or not available to resellers.",
        )

    orders = OrderService(session)
    quote = await orders.quote(product=product, quantity=payload.quantity, is_reseller=True)
    order = await orders.create_order(
        quote=quote,
        reseller_id=principal.reseller.id,
        idempotency_key=guard.key,
        customer_reference=payload.customer_reference,
        reseller_reference=payload.reseller_reference,
        channel="api",
        metadata=payload.metadata,
    )
    await orders.transition(order, OrderStatus.PAYMENT_PENDING)

    if payload.payment_method:
        method = await PaymentMethodRepository(session).get_by_code(payload.payment_method)
        if method is None:
            raise ValidationError(
                f"unknown payment method {payload.payment_method}",
                safe_message="Unknown payment method.",
            )
        await PaymentService(session).create_intent(order=order, method=method)

    await ResellerRepository(session).record_sale(principal.reseller.id, order.total)

    result = await _serialise(session, order)
    await guard.store(body, result.model_dump(mode="json"), status.HTTP_201_CREATED)

    reseller_service = ResellerService(session)
    await reseller_service.dispatch_event(
        event=WebhookEvent.ORDER_CREATED,
        order=order,
        payload={"order": result.model_dump(mode="json")},
    )
    if payload.payment_method:
        await reseller_service.dispatch_event(
            event=WebhookEvent.PAYMENT_PENDING,
            order=order,
            payload={"order": result.model_dump(mode="json")},
        )

    log.info(
        "api.order_created",
        order=order.reference,
        reseller=str(principal.reseller.id),
        api_key=principal.api_key.public_id,
    )
    return result


@router.get("", response_model=Paginated[OrderOut], summary="List your orders")
async def list_orders(
    session: SessionDep,
    principal: Annotated[APIPrincipal, Depends(require_scope(ApiScope.ORDERS_READ))],
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 25,
    order_status: Annotated[str | None, Query(alias="status")] = None,
) -> Paginated[OrderOut]:
    statuses = None
    if order_status:
        try:
            statuses = [OrderStatus(order_status)]
        except ValueError as exc:
            raise ValidationError(
                f"unknown status {order_status}", safe_message="Unknown order status."
            ) from exc

    result = await OrderRepository(session).list_for_reseller(
        principal.reseller.id, statuses=statuses, page=page, per_page=per_page
    )
    return Paginated[OrderOut](
        data=[await _serialise(session, order) for order in result.items],
        meta=PageMeta(
            page=result.page,
            per_page=result.per_page,
            total=result.total,
            pages=result.pages,
            has_next=result.has_next,
        ),
    )


async def _load_own_order(session, principal: APIPrincipal, order_id: str) -> Order:
    """Fetch an order, enforcing that it belongs to the calling reseller."""
    try:
        identifier = uuid.UUID(order_id)
    except ValueError:
        order = await OrderRepository(session).get_by_reference(order_id)
    else:
        order = await OrderRepository(session).get_with_items(identifier)

    # A wrong-owner order is reported as not-found so the API cannot be used to
    # confirm that another reseller's order id exists.
    if order is None or order.reseller_id != principal.reseller.id:
        raise NotFoundError(
            f"order {order_id} not visible to reseller {principal.reseller.id}",
            safe_message="Order not found.",
        )
    return order


@router.get("/{order_id}", response_model=OrderOut, summary="Get order status")
async def get_order(
    order_id: str,
    session: SessionDep,
    principal: Annotated[APIPrincipal, Depends(require_scope(ApiScope.ORDERS_READ))],
) -> OrderOut:
    order = await _load_own_order(session, principal, order_id)
    return await _serialise(session, order)


@router.get(
    "/{order_id}/delivery",
    response_model=DeliveryOut,
    summary="Get delivered product",
    description=(
        "Returns the delivered digital goods once payment is verified and "
        "delivery has completed. Before that the payload is empty."
    ),
)
async def get_delivery(
    order_id: str,
    session: SessionDep,
    principal: Annotated[APIPrincipal, Depends(require_scope(ApiScope.DELIVERIES_READ))],
) -> DeliveryOut:
    order = await _load_own_order(session, principal, order_id)
    deliveries = await DeliveryRepository(session).list_for_order(order.id)

    if not deliveries:
        return DeliveryOut(status="pending", items=[], attempts=0)

    service = DeliveryService(session)
    items: list[str] = []
    completed = [d for d in deliveries if d.status is DeliveryStatus.COMPLETED]
    for delivery in completed:
        try:
            items.extend(service.reveal(delivery).items)
        except ConflictError:
            continue

    if len(completed) == len(deliveries):
        state = "completed"
    elif any(d.status is DeliveryStatus.FAILED for d in deliveries):
        state = "retrying"
    else:
        state = "processing"

    return DeliveryOut(
        status=state,
        delivered_at=max((d.delivered_at for d in completed if d.delivered_at), default=None),
        items=items,
        attempts=max((d.attempts for d in deliveries), default=0),
    )


@router.post(
    "/{order_id}/payment",
    response_model=OrderOut,
    summary="Attach a payment method to an order",
)
async def create_payment(
    order_id: str,
    method_code: Annotated[str, Query(alias="payment_method")],
    session: SessionDep,
    principal: Annotated[APIPrincipal, Depends(require_scope(ApiScope.ORDERS_CREATE))],
) -> OrderOut:
    order = await _load_own_order(session, principal, order_id)
    method = await PaymentMethodRepository(session).get_by_code(method_code)
    if method is None:
        raise ValidationError(
            f"unknown payment method {method_code}", safe_message="Unknown payment method."
        )
    await PaymentService(session).create_intent(order=order, method=method)
    result = await _serialise(session, order)

    await ResellerService(session).dispatch_event(
        event=WebhookEvent.PAYMENT_PENDING,
        order=order,
        payload={"order": result.model_dump(mode="json")},
    )
    return result
