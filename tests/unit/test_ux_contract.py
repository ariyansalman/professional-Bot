"""UX contract tests (specification sections 75-82, 130).

These guard against the failure modes that are easy to introduce and hard to
notice: a button whose callback no handler answers, a list with no empty state,
an error path that leaks internals, or an untranslated string.
"""

from __future__ import annotations

import inspect

import pytest
from aiogram import Router

from app.bot.callbacks import (
    AdminCB,
    CheckoutCB,
    ConfirmCB,
    Nav,
    NoopCB,
    OrderCB,
    PageCB,
    PayCB,
    ProductCB,
    ProfileCB,
    ResellerCB,
    ShopCB,
    SupportCB,
)
from app.domain.enums import Language, OrderStatus, PaymentStatus
from app.i18n import t
from app.i18n.translator import get_translator


def _all_handlers(router: Router) -> list:
    handlers = list(router.callback_query.handlers) + list(router.message.handlers)
    for sub in router.sub_routers:
        handlers.extend(_all_handlers(sub))
    return handlers


@pytest.fixture(scope="module")
def router() -> Router:
    from app.bot.handlers import build_router

    return build_router()


# --- navigation -----------------------------------------------------------


NAV_DESTINATIONS = [
    "home",
    "shop",
    "orders",
    "profile",
    "referral",
    "support",
    "reseller",
    "how_it_works",
]


def test_every_nav_destination_has_a_handler(router):
    """A Back or Home button must never lead nowhere."""
    # Handlers declare their destination in the Nav filter, e.g. F.to == "home".
    modules = set()
    for handler in _all_handlers(router):
        module = inspect.getmodule(handler.callback)
        if module is not None:
            modules.add(module)
    registered = "".join(inspect.getsource(module) for module in modules)

    for destination in NAV_DESTINATIONS:
        assert f'F.to == "{destination}"' in registered, f"no handler for Nav(to={destination!r})"


CALLBACK_PREFIXES = [
    (ShopCB, ["categories", "category", "flag", "search"]),
    (ProductCB, ["view", "notify"]),
    (CheckoutCB, ["open", "qty", "coupon", "clear_coupon", "confirm"]),
    (PayCB, ["methods", "select", "screen", "paid", "submit", "status", "qr", "new"]),
    (OrderCB, ["list", "view", "pay", "cancel", "product", "receipt", "delivery", "reorder"]),
    (ProfileCB, ["view", "settings", "language", "set_language", "notifications", "mark_read"]),
    (SupportCB, ["menu", "category", "tickets", "view", "reply"]),
    (ResellerCB, ["center", "terms", "accept", "dashboard", "keys", "create_key", "docs"]),
]


@pytest.mark.parametrize(("factory", "actions"), CALLBACK_PREFIXES)
def test_every_callback_action_is_handled(router, factory, actions):
    """Every action a keyboard can emit must have a handler branch."""
    modules = set()
    for handler in _all_handlers(router):
        module = inspect.getmodule(handler.callback)
        if module is not None:
            modules.add(module)
    source = "".join(inspect.getsource(module) for module in modules)

    for action in actions:
        assert f'"{action}"' in source, (
            f"{factory.__name__} action {action!r} is emitted by a keyboard but "
            "no handler appears to branch on it"
        )


def test_callback_scope_rejects_the_field_separator():
    """aiogram packs fields with ':', so a scope containing one would crash."""
    with pytest.raises(ValueError, match="separator"):
        PageCB(scope="adm:payments", page=1)


def test_callback_payloads_stay_within_telegram_limit(router):
    """Telegram rejects callback_data longer than 64 bytes."""
    import uuid

    identifier = uuid.uuid4().hex
    samples = [
        Nav(to="home", arg="x" * 20),
        ShopCB(action="category", ref=identifier, page=99),
        ProductCB(action="view", pid=identifier, qty=999),
        CheckoutCB(action="clear_coupon", pid=identifier, qty=999),
        PayCB(action="methods", oid=identifier, ref="usdt_arbitrum"),
        OrderCB(action="delivery", oid=identifier, page=99, arg="completed"),
        ProfileCB(action="referral_history", arg="bn", page=99),
        SupportCB(action="tickets", arg=identifier, page=99),
        ResellerCB(action="revoke_confirm", arg=identifier, page=99),
        AdminCB(section="payments", action="approve", arg=identifier, page=99),
        ConfirmCB(token="x" * 22, decision="yes"),
        PageCB(scope="adm_reconciliation", page=999, arg="amount_mismatch"),
        NoopCB(tag="page"),
    ]
    for sample in samples:
        packed = sample.pack()
        assert len(packed.encode()) <= 64, f"{packed} is {len(packed.encode())} bytes"


# --- state-aware buttons --------------------------------------------------


def test_order_keyboard_offers_only_valid_actions():
    """Section 76: a button must match the order's real state."""
    from types import SimpleNamespace
    from uuid import uuid4

    from app.bot.keyboards.customer import order_details_keyboard

    def buttons_for(status: OrderStatus, *, ready: bool = False) -> set[str]:
        order = SimpleNamespace(id=uuid4(), status=status, reference="TG-1")
        keyboard = order_details_keyboard(
            Language.EN, order, has_delivery=ready, delivery_ready=ready, has_open_payment=True
        )
        return {b.text for row in keyboard.inline_keyboard for b in row}

    pending = buttons_for(OrderStatus.PAYMENT_PENDING)
    assert any("Payment" in b for b in pending)
    assert any("Cancel" in b for b in pending)
    # An unpaid order must not offer the product.
    assert not any("View Product" in b for b in pending)

    completed = buttons_for(OrderStatus.COMPLETED, ready=True)
    assert any("View Product" in b for b in completed)
    # A completed order must not offer cancellation.
    assert not any("Cancel Order" in b for b in completed)

    cancelled = buttons_for(OrderStatus.CANCELLED)
    assert any("Reorder" in b for b in cancelled)
    assert not any("Pay" in b for b in cancelled)

    expired = buttons_for(OrderStatus.EXPIRED)
    assert any("New Payment" in b or "Create New" in b for b in expired)


def test_payment_keyboard_offers_only_valid_actions():
    from types import SimpleNamespace
    from uuid import uuid4

    from app.bot.keyboards.customer import payment_status_keyboard

    def buttons_for(status: PaymentStatus, order_status: OrderStatus) -> set[str]:
        intent = SimpleNamespace(status=status)
        order = SimpleNamespace(id=uuid4(), status=order_status)
        keyboard = payment_status_keyboard(Language.EN, intent, order)
        return {b.text for row in keyboard.inline_keyboard for b in row}

    # Waiting for confirmations: offer a refresh, not a retry.
    pending = buttons_for(PaymentStatus.PENDING_CONFIRMATION, OrderStatus.PAYMENT_PENDING)
    assert any("Refresh" in b for b in pending)

    # Expired: offer a new payment.
    expired = buttons_for(PaymentStatus.EXPIRED, OrderStatus.EXPIRED)
    assert any("New Payment" in b for b in expired)

    # Under review: the customer needs support, not a retry button.
    review = buttons_for(PaymentStatus.UNDER_REVIEW, OrderStatus.MANUAL_REVIEW)
    assert any("Support" in b for b in review)
    assert not any("Refresh" in b for b in review)


def test_out_of_stock_product_offers_no_buy_button():
    """Section 13: never expose Buy when purchase is impossible."""
    from types import SimpleNamespace
    from uuid import uuid4

    from app.bot.keyboards.customer import product_details_keyboard
    from app.domain.inventory.service import StockStatus

    product = SimpleNamespace(id=uuid4(), name="X")
    out = StockStatus(available=0, is_unlimited=False, in_stock=False, low_stock=False)
    buttons = {
        b.text
        for row in product_details_keyboard(Language.EN, product, out).inline_keyboard
        for b in row
    }
    assert not any("BUY" in b.upper() for b in buttons)
    assert any("Notify" in b for b in buttons)

    in_stock = StockStatus(available=5, is_unlimited=False, in_stock=True, low_stock=False)
    buttons = {
        b.text
        for row in product_details_keyboard(Language.EN, product, in_stock).inline_keyboard
        for b in row
    }
    assert any("BUY" in b.upper() for b in buttons)


# --- i18n -----------------------------------------------------------------


def test_every_string_is_translated_into_bengali():
    """Section 120: the architecture must support English and Bengali."""
    missing = get_translator().missing_keys(Language.BN)
    assert missing == [], f"untranslated keys: {missing[:10]}"


def test_missing_key_does_not_crash():
    assert t("this.key.does.not.exist", Language.EN) == "this.key.does.not.exist"


REQUIRED_STATE_KEYS = [
    # Empty states (section 80)
    "orders.empty",
    "notifications.empty",
    "support.empty",
    "coupon.none",
    "referral.empty",
    "shop.empty",
    "shop.no_results",
    "reseller.no_keys",
    # Loading states (section 77)
    "loading.checking_payment",
    "loading.creating_order",
    "loading.loading_products",
    "loading.preparing_delivery",
    # Success states (section 78)
    "success.order_created",
    "success.copied",
    "success.saved",
    # Error states (section 79)
    "error.generic",
    "error.not_found",
    "error.expired_session",
    "error.rate_limited",
    # Payment outcome screens (sections 22-34)
    "payment.submitted_title",
    "payment.verifying_title",
    "payment.detected_title",
    "payment.confirmations_title",
    "payment.verified_title",
    "payment.failed_title",
    "payment.expired_title",
    "payment.underpaid_title",
    "payment.overpaid_title",
    "payment.wrong_network_title",
    "payment.wrong_asset_title",
    "payment.duplicate_title",
    "payment.review_title",
    # Delivery states (sections 35-37)
    "delivery.preparing_title",
    "delivery.ready_title",
    "delivery.delayed_title",
]


@pytest.mark.parametrize("key", REQUIRED_STATE_KEYS)
def test_required_screen_state_exists(key):
    catalog = get_translator().catalog
    assert key in catalog, f"the UX contract requires a {key!r} string"
    assert catalog[key]["en"].strip(), f"{key!r} is empty"


def test_error_strings_never_expose_internals():
    """Section 79: no customer-facing string may leak technical detail."""
    forbidden = ("traceback", "sql", "exception", "stack", "postgres", "asyncpg", "http 5")
    for key, entry in get_translator().catalog.items():
        if not key.startswith("error."):
            continue
        for language, text in entry.items():
            lowered = text.lower()
            for token in forbidden:
                assert token not in lowered, f"{key}[{language}] leaks {token!r}"


# --- payment screens ------------------------------------------------------


def test_every_verification_outcome_has_a_customer_screen():
    """Every way a payment can end must render something sensible."""
    from decimal import Decimal
    from types import SimpleNamespace

    from app.bot.services.formatting import payment_status_screen
    from app.core.timeutils import utcnow
    from app.domain.enums import VerificationOutcome

    for status in PaymentStatus:
        for outcome in [None, *VerificationOutcome]:
            intent = SimpleNamespace(
                status=status,
                last_outcome=outcome,
                reference="TG-10284",
                asset="USDT",
                expected_amount=Decimal("10"),
                received_amount=Decimal("8.5"),
                confirmations=8,
                required_confirmations=12,
                expires_at=utcnow(),
                network=SimpleNamespace(value="trc20"),
                verification_config={},
                method=SimpleNamespace(network_label="TRON / TRC20"),
            )
            text = payment_status_screen(intent, Language.EN)
            assert text.strip(), f"{status}/{outcome} rendered nothing"
            # No screen may show a raw enum name to a customer.
            assert "VerificationOutcome." not in text
            assert "PaymentStatus." not in text
