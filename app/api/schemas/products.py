"""Reseller-facing product schemas.

Only fields a reseller is permitted to see are exposed: internal delivery
payloads, stock item contents and cost data are never serialised.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field

from app.api.schemas.common import APIModel


class CategoryOut(APIModel):
    id: str
    slug: str
    name: str


class PricingOut(APIModel):
    currency: str
    #: What the reseller is charged.
    wholesale_price: Decimal | None = None
    #: The lowest price the reseller may resell at.
    minimum_price: Decimal | None = None
    recommended_price: Decimal | None = None
    #: Retail price shown in our own storefront.
    list_price: Decimal


class ProductOut(APIModel):
    id: str
    sku: str
    name: str
    short_description: str = ""
    description: str = ""
    category: CategoryOut | None = None
    pricing: PricingOut
    delivery_type: str
    in_stock: bool
    available_quantity: int | None = Field(
        default=None, description="null when the product has unlimited stock"
    )
    min_quantity: int = 1
    max_quantity: int | None = None
    features: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
