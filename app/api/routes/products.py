"""GET /api/v1/products - reseller catalog (sections 50, 54)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.dependencies.auth import APIPrincipal, SessionDep, require_scope
from app.api.schemas.common import PageMeta, Paginated
from app.api.schemas.products import CategoryOut, PricingOut, ProductOut
from app.core.exceptions import NotFoundError
from app.db.models.catalog import Product
from app.db.repositories.catalog import ProductRepository
from app.domain.enums import ApiScope, DeliveryType
from app.domain.inventory.service import InventoryService, StockStatus

router = APIRouter(prefix="/products", tags=["products"])


def _serialise(product: Product, stock: StockStatus) -> ProductOut:
    """Map a product to its reseller view.

    Deliberately omitted: delivery payloads, file ids, stock item contents,
    internal metadata and any cost figure other than the reseller's own price.
    """
    return ProductOut(
        id=str(product.id),
        sku=product.sku,
        name=product.name,
        short_description=product.short_description or "",
        description=product.full_description or "",
        category=CategoryOut(
            id=str(product.category.id),
            slug=product.category.slug,
            name=product.category.name_en,
        )
        if product.category
        else None,
        pricing=PricingOut(
            currency=product.currency,
            wholesale_price=product.reseller_price,
            minimum_price=product.reseller_min_price,
            recommended_price=product.reseller_recommended_price,
            list_price=product.price,
        ),
        delivery_type=product.delivery_type.value,
        in_stock=stock.in_stock,
        available_quantity=None if stock.is_unlimited else stock.available,
        min_quantity=product.min_quantity,
        max_quantity=product.max_quantity,
        features=[str(f) for f in (product.features or [])],
        requirements=[str(r) for r in (product.requirements or [])],
        metadata=product.product_metadata or {},
    )


@router.get(
    "",
    response_model=Paginated[ProductOut],
    summary="List products available to resellers",
)
async def list_products(
    session: SessionDep,
    principal: Annotated[APIPrincipal, Depends(require_scope(ApiScope.PRODUCTS_READ))],
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 25,
    category_id: Annotated[str | None, Query()] = None,
) -> Paginated[ProductOut]:
    products = ProductRepository(session)
    inventory = InventoryService(session)

    category_uuid: uuid.UUID | None = None
    if category_id:
        try:
            category_uuid = uuid.UUID(category_id)
        except ValueError as exc:
            raise NotFoundError(
                f"invalid category id {category_id}", safe_message="Unknown category."
            ) from exc

    result = await products.list_for_resellers(
        page=page, per_page=per_page, category_id=category_uuid
    )
    stock = await inventory.stock_map(list(result.items))
    return Paginated[ProductOut](
        data=[_serialise(product, stock[product.id]) for product in result.items],
        meta=PageMeta(
            page=result.page,
            per_page=result.per_page,
            total=result.total,
            pages=result.pages,
            has_next=result.has_next,
        ),
    )


@router.get("/{product_id}", response_model=ProductOut, summary="Get one product")
async def get_product(
    product_id: str,
    session: SessionDep,
    principal: Annotated[APIPrincipal, Depends(require_scope(ApiScope.PRODUCTS_READ))],
) -> ProductOut:
    try:
        identifier = uuid.UUID(product_id)
    except ValueError as exc:
        raise NotFoundError(
            f"invalid product id {product_id}", safe_message="Product not found."
        ) from exc

    products = ProductRepository(session)
    product = await products.get_for_reseller(identifier)
    if product is None:
        raise NotFoundError(f"product {product_id} not available", safe_message="Product not found.")

    stock = await InventoryService(session).stock_status(product)
    return _serialise(product, stock)
