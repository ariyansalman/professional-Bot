"""Reseller order schemas."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import Field, field_validator

from app.api.schemas.common import APIModel


class OrderCreateIn(APIModel):
    product_id: str = Field(description="Product identifier from GET /products")
    quantity: int = Field(default=1, ge=1, le=1000)
    customer_reference: str | None = Field(
        default=None, max_length=128, description="Your customer's identifier, echoed back"
    )
    reseller_reference: str | None = Field(
        default=None, max_length=128, description="Your own order identifier, echoed back"
    )
    payment_method: str | None = Field(
        default=None, description="Payment method code; omit to choose later"
    )
    metadata: dict = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def _bound_metadata(cls, value: dict) -> dict:
        if len(value) > 20:
            raise ValueError("metadata may contain at most 20 keys")
        return value


class OrderItemOut(APIModel):
    product_id: str | None
    product_name: str
    sku: str
    quantity: int
    unit_price: Decimal
    line_total: Decimal


class PaymentOut(APIModel):
    """Payment instructions. Contains no internal identifiers."""

    reference: str
    status: str
    asset: str
    network: str
    amount: Decimal
    destination: str
    memo: str | None = None
    required_confirmations: int
    confirmations: int = 0
    received_amount: Decimal | None = None
    expires_at: datetime
    verified_at: datetime | None = None


class DeliveryOut(APIModel):
    status: str
    delivered_at: datetime | None = None
    #: Present only once payment is verified and delivery has completed.
    items: list[str] = Field(default_factory=list)
    attempts: int = 0


class OrderOut(APIModel):
    id: str
    reference: str
    status: str
    currency: str
    subtotal: Decimal
    discount_total: Decimal
    total: Decimal
    customer_reference: str | None = None
    reseller_reference: str | None = None
    items: list[OrderItemOut]
    payment: PaymentOut | None = None
    delivery_status: str = "pending"
    created_at: datetime
    paid_at: datetime | None = None
    completed_at: datetime | None = None
    metadata: dict = Field(default_factory=dict)


class WebhookCreateIn(APIModel):
    url: str = Field(max_length=1024, description="HTTPS endpoint that receives events")
    events: list[str] = Field(
        default_factory=list, description="Event types; empty means all events"
    )
    description: str | None = Field(default=None, max_length=255)


class WebhookOut(APIModel):
    id: str
    url: str
    events: list[str]
    is_active: bool
    health: str
    created_at: datetime


class WebhookCreatedOut(WebhookOut):
    #: Returned exactly once, at creation.
    secret: str = Field(description="Signing secret. Store it now; it is not shown again.")
