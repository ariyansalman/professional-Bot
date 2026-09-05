"""Bot handler tests: real updates driven through the real dispatcher.

These catch runtime errors that importing a module never would. The harness
lives in ``conftest.py`` so the admin tests share it.
"""

from __future__ import annotations

from tests.factories import add_stock, make_category, make_product
from tests.integration.conftest import (
    TELEGRAM_ID,
    callback_update,
    feed,
    message_update,
)

# --- customer flow --------------------------------------------------------


async def test_start_creates_a_user_and_shows_the_welcome(bot_harness, session):
    from app.db.repositories.users import UserRepository

    recorder = await feed(bot_harness, message_update("/start"))

    assert recorder.texts, "no message was sent"
    assert "Welcome" in recorder.texts[0]
    assert "Start Shopping" in " ".join(recorder.buttons)

    user = await UserRepository(session).get_by_telegram_id(TELEGRAM_ID)
    assert user is not None
    assert user.referral_code


async def test_returning_user_is_not_re_onboarded(bot_harness, session):
    await feed(bot_harness, message_update("/start"))
    recorder = await feed(bot_harness, message_update("/start", update_id=3))

    # The onboarding copy is only for a genuinely new account.
    assert "Welcome back" in recorder.texts[0]
    assert "Premium products" not in recorder.texts[0]


async def test_empty_shop_shows_an_empty_state_not_an_empty_keyboard(bot_harness):
    await feed(bot_harness, message_update("/start"))
    recorder = await feed(bot_harness, callback_update("nav:shop:"))

    assert "No products are available" in recorder.texts[0]
    assert "Home" in " ".join(recorder.buttons)


async def test_shop_lists_a_category_once_a_product_exists(bot_harness, session):
    category = await make_category(session)
    product = await make_product(session, category=category)
    await add_stock(session, product, count=3)
    await session.commit()

    await feed(bot_harness, message_update("/start"))
    recorder = await feed(bot_harness, callback_update("nav:shop:"))

    assert "SHOP" in recorder.texts[0]
    assert any("License Keys" in b for b in recorder.buttons)


async def test_product_details_render_and_offer_buy(bot_harness, session):
    from app.bot.callbacks import ProductCB, pack_uuid

    category = await make_category(session)
    product = await make_product(session, category=category)
    await add_stock(session, product, count=3)
    await session.commit()

    await feed(bot_harness, message_update("/start"))
    recorder = await feed(
        bot_harness, callback_update(ProductCB(action="view", pid=pack_uuid(product.id)).pack())
    )

    body = recorder.texts[0]
    assert "Premium License" in body
    # Three items against a threshold of five is legitimately "low stock".
    assert "Only 3 left" in body
    assert any("BUY" in b.upper() for b in recorder.buttons)


async def test_out_of_stock_product_hides_buy(bot_harness, session):
    from app.bot.callbacks import ProductCB, pack_uuid

    product = await make_product(session, sku="NOSTOCK")
    await session.commit()

    await feed(bot_harness, message_update("/start"))
    recorder = await feed(
        bot_harness, callback_update(ProductCB(action="view", pid=pack_uuid(product.id)).pack())
    )

    assert not any("BUY" in b.upper() for b in recorder.buttons)
    assert any("Notify" in b for b in recorder.buttons)


async def test_checkout_shows_the_price_snapshot(bot_harness, session):
    from app.bot.callbacks import CheckoutCB, pack_uuid

    product = await make_product(session, price="15.00")
    await add_stock(session, product, count=3)
    await session.commit()

    await feed(bot_harness, message_update("/start"))
    recorder = await feed(
        bot_harness,
        callback_update(CheckoutCB(action="open", pid=pack_uuid(product.id)).pack()),
    )

    body = recorder.texts[0]
    assert "CHECKOUT" in body
    assert "15.00" in body
    assert "TOTAL" in body
    assert any("Confirm Order" in b for b in recorder.buttons)


async def test_confirming_an_order_creates_it_and_asks_for_a_payment_method(
    bot_harness, session
):
    from app.bot.callbacks import CheckoutCB, pack_uuid
    from app.db.repositories.orders import OrderRepository
    from tests.factories import make_payment_method

    product = await make_product(session, price="15.00")
    await add_stock(session, product, count=3)
    await make_payment_method(session)
    await session.commit()

    await feed(bot_harness, message_update("/start"))
    recorder = await feed(
        bot_harness,
        callback_update(CheckoutCB(action="confirm", pid=pack_uuid(product.id), qty=1).pack()),
    )

    assert "Creating order" in " ".join(recorder.toasts)
    body = recorder.texts[0]
    assert "PAYMENT METHOD" in body

    orders = await OrderRepository(session).list_for_admin()
    assert orders.total == 1
    assert orders.items[0].total.normalize() == __import__("decimal").Decimal("15")


async def test_empty_orders_screen_offers_shopping(bot_harness):
    await feed(bot_harness, message_update("/start"))
    recorder = await feed(bot_harness, callback_update("nav:orders:"))

    assert "No orders yet" in recorder.texts[0]
    assert any("Shop Now" in b for b in recorder.buttons)


async def test_profile_renders(bot_harness):
    await feed(bot_harness, message_update("/start"))
    recorder = await feed(bot_harness, callback_update("nav:profile:"))

    assert "PROFILE" in recorder.texts[0]
    assert any("My Orders" in b for b in recorder.buttons)


async def test_support_menu_renders_categories(bot_harness):
    await feed(bot_harness, message_update("/start"))
    recorder = await feed(bot_harness, callback_update("nav:support:"))

    assert "SUPPORT" in recorder.texts[0]
    assert any("Payment Issue" in b for b in recorder.buttons)


async def test_unknown_callback_recovers_instead_of_doing_nothing(bot_harness):
    await feed(bot_harness, message_update("/start"))
    recorder = await feed(bot_harness, callback_update("totally:unknown:data"))

    assert recorder.calls, "a stale button must still get a response"
    assert any("expired" in text.lower() for text in recorder.texts)


async def test_stray_text_guides_the_customer_back(bot_harness):
    await feed(bot_harness, message_update("/start"))
    recorder = await feed(bot_harness, message_update("hello there", update_id=9))

    assert any("buttons" in text.lower() for text in recorder.texts)


# --- errors ---------------------------------------------------------------


async def test_a_handler_error_shows_a_safe_message_not_a_traceback(
    bot_harness, session, monkeypatch
):
    """The error middleware must never leak internals to a customer."""
    from app.db.repositories.catalog import CategoryRepository

    async def boom(self):
        raise RuntimeError("psycopg2.errors.UndefinedTable: relation does not exist")

    monkeypatch.setattr(CategoryRepository, "list_active", boom)

    await feed(bot_harness, message_update("/start"))
    recorder = await feed(bot_harness, callback_update("nav:shop:"))

    rendered = " ".join(recorder.texts) + " ".join(
        getattr(call, "text", "") or "" for call in recorder.calls
    )
    assert "psycopg2" not in rendered
    assert "RuntimeError" not in rendered
    assert "relation does not exist" not in rendered


# --- admin ----------------------------------------------------------------


async def test_admin_panel_is_closed_to_a_normal_customer(bot_harness):
    await feed(bot_harness, message_update("/start"))
    recorder = await feed(bot_harness, message_update("/admin", update_id=11))

    rendered = " ".join(recorder.texts)
    assert "ADMIN DASHBOARD" not in rendered


async def test_admin_dashboard_opens_for_a_bootstrap_admin(bot_harness, session, monkeypatch):
    """A bootstrap admin id grants SUPER_ADMIN on first use."""
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings.telegram, "bootstrap_admin_ids", [TELEGRAM_ID])

    # The role must exist for the grant to be persisted.
    from app.db.models.user import Role
    from app.domain.enums import RoleName

    session.add(Role(name=RoleName.SUPER_ADMIN, description="Full access"))
    await session.commit()

    await feed(bot_harness, message_update("/start"))
    recorder = await feed(bot_harness, message_update("/admin", update_id=12))

    assert "ADMIN DASHBOARD" in recorder.texts[0]
    assert any("Orders" in b for b in recorder.buttons)
    assert any("Payments" in b for b in recorder.buttons)
