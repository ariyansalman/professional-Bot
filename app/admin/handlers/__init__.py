"""Admin router assembly."""

from __future__ import annotations

from aiogram import Router


def admin_router() -> Router:
    from app.admin.handlers import (
        catalog,
        categories,
        dashboard,
        orders,
        payments,
        product_edit,
        providers,
        refunds,
        resellers,
        system,
        users,
    )

    router = Router(name="admin")
    router.include_router(dashboard.router)
    router.include_router(orders.router)
    router.include_router(payments.router)
    router.include_router(refunds.router)
    router.include_router(catalog.router)
    router.include_router(product_edit.router)
    router.include_router(categories.router)
    router.include_router(users.router)
    router.include_router(resellers.router)
    router.include_router(system.router)
    router.include_router(providers.router)
    return router


__all__ = ["admin_router"]
