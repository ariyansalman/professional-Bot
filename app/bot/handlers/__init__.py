"""Handler router assembly.

Order matters: the admin router is registered first so an admin command is not
swallowed by a customer catch-all.

Handler routers are module-level singletons, which is aiogram's idiom, and a
Router can only be attached to one parent. The assembled root router is
therefore cached: calling :func:`build_router` twice returns the same object
rather than raising a confusing "already attached" error deep inside aiogram.
One process runs one dispatcher, so this is exactly the desired behaviour.
"""

from __future__ import annotations

from aiogram import Router

_root: Router | None = None


def build_router() -> Router:
    global _root
    if _root is None:
        _root = _assemble()
    return _root


def _assemble() -> Router:
    from app.admin.handlers import admin_router
    from app.bot.handlers import (
        checkout,
        common,
        orders,
        payments,
        products,
        profile,
        reseller,
        shop,
        start,
        support,
    )

    router = Router(name="root")
    router.include_router(admin_router())
    router.include_router(start.router)
    router.include_router(shop.router)
    router.include_router(products.router)
    router.include_router(checkout.router)
    router.include_router(payments.router)
    router.include_router(orders.router)
    router.include_router(profile.router)
    router.include_router(support.router)
    router.include_router(reseller.router)
    # Catch-all last: unknown callbacks and stray text land here.
    router.include_router(common.router)
    return router


__all__ = ["build_router"]
