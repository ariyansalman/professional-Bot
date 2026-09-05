"""Customer keyboards - one builder per screen in the navigation contract."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.callbacks import (
    CheckoutCB,
    Nav,
    OrderCB,
    PayCB,
    ProductCB,
    ProfileCB,
    ResellerCB,
    ShopCB,
    SupportCB,
    pack_uuid,
)
from app.bot.keyboards.common import build, button, nav_button, pagination_row
from app.db.models.catalog import Category, Product
from app.db.models.order import Order
from app.db.models.payment import PaymentIntent, PaymentMethod
from app.db.repositories.base import Page
from app.domain.enums import (
    DeliveryStatus,
    Language,
    OrderStatus,
    PaymentProviderKind,
    PaymentStatus,
)
from app.domain.inventory.service import StockStatus
from app.i18n import t


def welcome_keyboard(lang: Language, *, first_time: bool) -> InlineKeyboardMarkup:
    if first_time:
        return build(
            [
                [nav_button(t("btn.start_shopping", lang), "shop")],
                [nav_button(t("btn.how_it_works", lang), "how_it_works")],
                [nav_button(t("btn.support", lang), "support")],
            ]
        )
    return build(
        [
            [nav_button(t("btn.shop", lang), "shop")],
            [
                nav_button(t("btn.my_orders", lang), "orders"),
                nav_button(t("btn.profile", lang), "profile"),
            ],
        ]
    )


def active_payment_keyboard(lang: Language, order: Order) -> InlineKeyboardMarkup:
    return build(
        [
            [
                button(
                    t("btn.continue_payment", lang),
                    OrderCB(action="pay", oid=pack_uuid(order.id)).pack(),
                )
            ],
            [nav_button(t("btn.home", lang), "home")],
        ]
    )


def home_keyboard(
    lang: Language,
    *,
    unread: int = 0,
    reseller_enabled: bool = True,
    referrals_enabled: bool = True,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            nav_button(t("btn.shop", lang), "shop"),
            nav_button(t("btn.my_orders", lang), "orders"),
        ]
    ]
    second: list[InlineKeyboardButton] = []
    if referrals_enabled:
        second.append(nav_button(t("btn.referral", lang), "referral"))
    if reseller_enabled:
        second.append(nav_button(t("btn.reseller", lang), "reseller"))
    rows.append(second)

    notif_label = t("btn.notifications", lang)
    if unread:
        notif_label = f"{notif_label} ({unread})"
    rows.append(
        [
            button(notif_label, ProfileCB(action="notifications").pack()),
            nav_button(t("btn.profile", lang), "profile"),
        ]
    )
    rows.append([nav_button(t("btn.support", lang), "support")])
    return build(rows)


def shop_keyboard(
    lang: Language, categories: list[Category], counts: dict
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for category in categories:
        count = counts.get(category.id, 0)
        if count == 0:
            continue  # never offer an empty category
        rows.append(
            [
                button(
                    f"{category.emoji} {category.display_name(lang.value)} ({count})",
                    ShopCB(action="category", ref=pack_uuid(category.id)).pack(),
                )
            ]
        )
    rows.append(
        [
            button(t("shop.best_sellers", lang), ShopCB(action="flag", ref="best_sellers").pack()),
            button(t("shop.new_arrivals", lang), ShopCB(action="flag", ref="new_arrivals").pack()),
        ]
    )
    rows.append(
        [
            button(t("btn.search", lang), ShopCB(action="search").pack()),
            nav_button(t("btn.home", lang), "home"),
        ]
    )
    return build(rows)


def product_list_keyboard(
    lang: Language,
    page: Page[Product],
    stock: dict,
    *,
    scope: str,
    scope_arg: str,
    back_to: str = "shop",
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for product in page.items:
        status: StockStatus = stock[product.id]
        details = button(
            f"{t('btn.details', lang)} · {product.display_name(lang.value)[:18]}",
            ProductCB(action="view", pid=pack_uuid(product.id)).pack(),
        )
        if status.in_stock:
            # Buy is only offered when the purchase can actually succeed.
            rows.append(
                [
                    details,
                    button(
                        t("btn.buy", lang),
                        CheckoutCB(action="open", pid=pack_uuid(product.id)).pack(),
                    ),
                ]
            )
        else:
            rows.append([details])
    rows.append(pagination_row(page, scope, arg=scope_arg))
    rows.append(
        [nav_button(t("btn.back", lang), back_to), nav_button(t("btn.home", lang), "home")]
    )
    return build(rows)


def product_details_keyboard(
    lang: Language,
    product: Product,
    status: StockStatus,
    *,
    back_to: str = "shop",
    back_arg: str = "",
    restock_enabled: bool = True,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if status.in_stock:
        rows.append(
            [
                button(
                    t("btn.buy_now", lang),
                    CheckoutCB(action="open", pid=pack_uuid(product.id)).pack(),
                )
            ]
        )
    elif restock_enabled:
        rows.append(
            [
                button(
                    t("btn.notify_me", lang),
                    ProductCB(action="notify", pid=pack_uuid(product.id)).pack(),
                )
            ]
        )
    rows.append(
        [
            nav_button(t("btn.back", lang), back_to, back_arg),
            nav_button(t("btn.home", lang), "home"),
        ]
    )
    return build(rows)


def checkout_keyboard(
    lang: Language,
    product_id,
    quantity: int,
    *,
    has_coupon: bool,
    max_quantity: int | None,
    coupons_enabled: bool = True,
) -> InlineKeyboardMarkup:
    pid = pack_uuid(product_id)
    rows: list[list[InlineKeyboardButton]] = []

    if max_quantity is None or max_quantity > 1:
        quantity_row: list[InlineKeyboardButton] = []
        if quantity > 1:
            quantity_row.append(
                button("➖", CheckoutCB(action="qty", pid=pid, qty=quantity - 1).pack())
            )
        quantity_row.append(button(f"× {quantity}", CheckoutCB(action="qty", pid=pid, qty=quantity).pack()))
        if max_quantity is None or quantity < max_quantity:
            quantity_row.append(
                button("➕", CheckoutCB(action="qty", pid=pid, qty=quantity + 1).pack())
            )
        rows.append(quantity_row)

    if coupons_enabled:
        if has_coupon:
            rows.append(
                [
                    button(
                        "🎟 Remove Coupon",
                        CheckoutCB(action="clear_coupon", pid=pid, qty=quantity).pack(),
                    )
                ]
            )
        else:
            rows.append(
                [
                    button(
                        t("btn.apply_coupon", lang),
                        CheckoutCB(action="coupon", pid=pid, qty=quantity).pack(),
                    )
                ]
            )

    rows.append(
        [
            button(
                t("btn.confirm_order", lang),
                CheckoutCB(action="confirm", pid=pid, qty=quantity).pack(),
            )
        ]
    )
    rows.append(
        [
            button(t("btn.back", lang), ProductCB(action="view", pid=pid).pack()),
            nav_button(t("btn.home", lang), "home"),
        ]
    )
    return build(rows)


def coupon_prompt_keyboard(lang: Language, product_id, quantity: int) -> InlineKeyboardMarkup:
    pid = pack_uuid(product_id)
    return build(
        [
            [
                button(
                    t("btn.back", lang),
                    CheckoutCB(action="open", pid=pid, qty=quantity).pack(),
                )
            ]
        ]
    )


def coupon_invalid_keyboard(lang: Language, product_id, quantity: int) -> InlineKeyboardMarkup:
    pid = pack_uuid(product_id)
    return build(
        [
            [
                button(
                    t("btn.try_again", lang),
                    CheckoutCB(action="coupon", pid=pid, qty=quantity).pack(),
                )
            ],
            [
                button(
                    t("btn.back", lang),
                    CheckoutCB(action="open", pid=pid, qty=quantity).pack(),
                )
            ],
        ]
    )


def payment_method_keyboard(
    lang: Language, methods: list[PaymentMethod], order: Order
) -> InlineKeyboardMarkup:
    """Exchange and blockchain methods are visually separated (section 18)."""
    oid = pack_uuid(order.id)
    exchange = [m for m in methods if m.provider.kind is PaymentProviderKind.EXCHANGE]
    blockchain = [m for m in methods if m.provider.kind is PaymentProviderKind.BLOCKCHAIN]
    rows: list[list[InlineKeyboardButton]] = []

    def add(group: list[PaymentMethod]) -> None:
        for method in group:
            rows.append(
                [
                    button(
                        f"{method.emoji} {method.display_name}",
                        PayCB(action="select", oid=oid, ref=method.code).pack(),
                    )
                ]
            )

    add(exchange)
    add(blockchain)
    rows.append(
        [
            button(t("btn.back", lang), OrderCB(action="view", oid=oid).pack()),
            nav_button(t("btn.home", lang), "home"),
        ]
    )
    return build(rows)


def payment_screen_keyboard(
    lang: Language, intent: PaymentIntent, order: Order, *, needs_reference: bool
) -> InlineKeyboardMarkup:
    oid = pack_uuid(order.id)
    rows: list[list[InlineKeyboardButton]] = []
    if intent.method.provider.kind is PaymentProviderKind.BLOCKCHAIN:
        rows.append([button(t("btn.qr_code", lang), PayCB(action="qr", oid=oid).pack())])
    if needs_reference:
        rows.append(
            [button(t("btn.submit_transaction", lang), PayCB(action="submit", oid=oid).pack())]
        )
    else:
        rows.append([button(t("btn.i_paid", lang), PayCB(action="paid", oid=oid).pack())])
    rows.append(
        [
            button(t("btn.back", lang), PayCB(action="methods", oid=oid).pack()),
            nav_button(t("btn.home", lang), "home"),
        ]
    )
    return build(rows)


def payment_status_keyboard(
    lang: Language, intent: PaymentIntent, order: Order
) -> InlineKeyboardMarkup:
    """State-aware actions (section 76): only valid actions are shown."""
    oid = pack_uuid(order.id)
    status = intent.status
    rows: list[list[InlineKeyboardButton]] = []

    if status is PaymentStatus.VERIFIED:
        if order.status in (OrderStatus.DELIVERED, OrderStatus.COMPLETED):
            rows.append(
                [button(t("btn.view_product", lang), OrderCB(action="product", oid=oid).pack())]
            )
        else:
            rows.append(
                [button(t("btn.delivery_status", lang), OrderCB(action="delivery", oid=oid).pack())]
            )
    elif status is PaymentStatus.EXPIRED:
        rows.append([button(t("btn.new_payment", lang), PayCB(action="new", oid=oid).pack())])
    elif status is PaymentStatus.FAILED:
        rows.append([button(t("btn.try_again", lang), PayCB(action="new", oid=oid).pack())])
    elif status in (
        PaymentStatus.SUBMITTED,
        PaymentStatus.DETECTING,
        PaymentStatus.DETECTED,
        PaymentStatus.VERIFYING,
        PaymentStatus.PENDING_CONFIRMATION,
    ):
        rows.append([button(t("btn.refresh", lang), PayCB(action="status", oid=oid).pack())])

    rows.append([button(t("btn.view_order", lang), OrderCB(action="view", oid=oid).pack())])
    if status.is_terminal and status is not PaymentStatus.VERIFIED:
        rows.append([nav_button(t("btn.support", lang), "support")])
    elif intent.status is PaymentStatus.UNDER_REVIEW:
        rows.append([nav_button(t("btn.contact_support", lang), "support")])
    rows.append([nav_button(t("btn.home", lang), "home")])
    return build(rows)


def order_list_keyboard(
    lang: Language, page: Page[Order], *, active_filter: str
) -> InlineKeyboardMarkup:
    filters = [
        ("all", t("orders.filter_all", lang)),
        ("pending", t("orders.filter_pending", lang)),
        ("paid", t("orders.filter_paid", lang)),
        ("completed", t("orders.filter_completed", lang)),
        ("cancelled", t("orders.filter_cancelled", lang)),
    ]
    filter_row = [
        button(
            f"• {label} •" if key == active_filter else label,
            OrderCB(action="list", arg=key).pack(),
        )
        for key, label in filters
    ]
    rows: list[list[InlineKeyboardButton]] = [filter_row[:3], filter_row[3:]]
    for order in page.items:
        rows.append(
            [
                button(
                    f"#{order.reference} · {order.total} {order.currency}",
                    OrderCB(action="view", oid=pack_uuid(order.id)).pack(),
                )
            ]
        )
    rows.append(pagination_row(page, "orders", arg=active_filter))
    rows.append([nav_button(t("btn.home", lang), "home")])
    return build(rows)


def order_details_keyboard(
    lang: Language,
    order: Order,
    *,
    has_delivery: bool,
    delivery_ready: bool,
    has_open_payment: bool,
) -> InlineKeyboardMarkup:
    """Section 76: the button always matches the order's real state."""
    oid = pack_uuid(order.id)
    rows: list[list[InlineKeyboardButton]] = []

    if order.status in (OrderStatus.CREATED, OrderStatus.PAYMENT_PENDING):
        label = t("btn.continue_payment", lang) if has_open_payment else "💳 Pay Now"
        rows.append([button(label, OrderCB(action="pay", oid=oid).pack())])
        rows.append([button("❌ Cancel Order", OrderCB(action="cancel", oid=oid).pack())])
    elif order.status is OrderStatus.EXPIRED:
        rows.append([button(t("btn.new_payment", lang), PayCB(action="new", oid=oid).pack())])
    elif order.status in (OrderStatus.PAYMENT_VERIFIED, OrderStatus.FULFILLING):
        rows.append(
            [button(t("btn.delivery_status", lang), OrderCB(action="delivery", oid=oid).pack())]
        )
    elif order.status in (OrderStatus.DELIVERED, OrderStatus.COMPLETED) and delivery_ready:
        rows.append(
            [button(t("btn.view_product", lang), OrderCB(action="product", oid=oid).pack())]
        )
    elif order.status is OrderStatus.DELIVERY_FAILED:
        rows.append(
            [button(t("btn.delivery_status", lang), OrderCB(action="delivery", oid=oid).pack())]
        )
    elif order.status is OrderStatus.CANCELLED:
        rows.append([button(t("btn.reorder", lang), OrderCB(action="reorder", oid=oid).pack())])

    if order.status.is_paid:
        rows.append([button(t("btn.receipt", lang), OrderCB(action="receipt", oid=oid).pack())])
    if order.status is OrderStatus.MANUAL_REVIEW:
        rows.append([nav_button(t("btn.contact_support", lang), "support")])

    rows.append(
        [
            button(t("btn.back", lang), OrderCB(action="list").pack()),
            nav_button(t("btn.home", lang), "home"),
        ]
    )
    return build(rows)


def empty_orders_keyboard(lang: Language) -> InlineKeyboardMarkup:
    return build(
        [
            [nav_button(t("btn.shop_now", lang), "shop")],
            [nav_button(t("btn.home", lang), "home")],
        ]
    )


def product_delivery_keyboard(lang: Language, order: Order) -> InlineKeyboardMarkup:
    oid = pack_uuid(order.id)
    return build(
        [
            [
                button(t("btn.receipt", lang), OrderCB(action="receipt", oid=oid).pack()),
                button(t("btn.order_details", lang), OrderCB(action="view", oid=oid).pack()),
            ],
            [nav_button(t("btn.shop", lang), "shop"), nav_button(t("btn.home", lang), "home")],
        ]
    )


def profile_keyboard(lang: Language, *, referrals_enabled: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [nav_button(t("btn.my_orders", lang), "orders")],
    ]
    if referrals_enabled:
        rows.append([nav_button(t("btn.referral", lang), "referral")])
    rows.append(
        [
            button(t("btn.notifications", lang), ProfileCB(action="notifications").pack()),
            button(t("btn.settings", lang), ProfileCB(action="settings").pack()),
        ]
    )
    rows.append(
        [
            nav_button(t("btn.support", lang), "support"),
            nav_button(t("btn.home", lang), "home"),
        ]
    )
    return build(rows)


def settings_keyboard(lang: Language, *, notifications_on: bool) -> InlineKeyboardMarkup:
    toggle = "🔔 Notifications: ON" if notifications_on else "🔕 Notifications: OFF"
    return build(
        [
            [button(toggle, ProfileCB(action="toggle_notifications").pack())],
            [button(t("btn.language", lang), ProfileCB(action="language").pack())],
            [
                nav_button(t("btn.back", lang), "profile"),
                nav_button(t("btn.home", lang), "home"),
            ],
        ]
    )


def language_keyboard(lang: Language) -> InlineKeyboardMarkup:
    return build(
        [
            [
                button(
                    "🇬🇧 English" + (" ✓" if lang is Language.EN else ""),
                    ProfileCB(action="set_language", arg="en").pack(),
                )
            ],
            [
                button(
                    "🇧🇩 বাংলা" + (" ✓" if lang is Language.BN else ""),
                    ProfileCB(action="set_language", arg="bn").pack(),
                )
            ],
            [button(t("btn.back", lang), ProfileCB(action="settings").pack())],
        ]
    )


def referral_keyboard(lang: Language, share_url: str) -> InlineKeyboardMarkup:
    return build(
        [
            [InlineKeyboardButton(text=t("btn.share", lang), url=share_url)],
            [button(t("btn.referral_history", lang), ProfileCB(action="referral_history").pack())],
            [
                nav_button(t("btn.back", lang), "profile"),
                nav_button(t("btn.home", lang), "home"),
            ],
        ]
    )


def notifications_keyboard(lang: Language, page: Page, *, has_unread: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if has_unread:
        rows.append([button("✅ Mark all read", ProfileCB(action="mark_read").pack())])
    rows.append(pagination_row(page, "notifications"))
    rows.append(
        [
            nav_button(t("btn.back", lang), "profile"),
            nav_button(t("btn.home", lang), "home"),
        ]
    )
    return build(rows)


def support_menu_keyboard(lang: Language) -> InlineKeyboardMarkup:
    return build(
        [
            [button(t("support.cat_payment", lang), SupportCB(action="category", arg="payment").pack())],
            [button(t("support.cat_order", lang), SupportCB(action="category", arg="order").pack())],
            [button(t("support.cat_product", lang), SupportCB(action="category", arg="product").pack())],
            [button(t("support.cat_technical", lang), SupportCB(action="category", arg="technical").pack())],
            [button(t("support.cat_other", lang), SupportCB(action="category", arg="other").pack())],
            [button(t("btn.my_tickets", lang), SupportCB(action="tickets").pack())],
            [nav_button(t("btn.home", lang), "home")],
        ]
    )


def ticket_keyboard(lang: Language, ticket_id, *, can_reply: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if can_reply:
        rows.append(
            [button("💬 Reply", SupportCB(action="reply", arg=pack_uuid(ticket_id)).pack())]
        )
    rows.append(
        [
            button(t("btn.back", lang), SupportCB(action="tickets").pack()),
            nav_button(t("btn.home", lang), "home"),
        ]
    )
    return build(rows)


def reseller_center_keyboard(
    lang: Language, *, is_active: bool, docs_url: str | None
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if is_active:
        rows.append([button(t("reseller.dashboard", lang), ResellerCB(action="dashboard").pack())])
        rows.append([button(t("reseller.api_keys", lang), ResellerCB(action="keys").pack())])
    else:
        rows.append([button(t("reseller.become", lang), ResellerCB(action="terms").pack())])
    if docs_url:
        rows.append([InlineKeyboardButton(text=t("reseller.api_docs", lang), url=docs_url)])
    else:
        rows.append([button(t("reseller.api_docs", lang), ResellerCB(action="docs").pack())])
    rows.append([nav_button(t("btn.home", lang), "home")])
    return build(rows)


def delivery_status_keyboard(
    lang: Language, order: Order, *, ready: bool, failed: bool
) -> InlineKeyboardMarkup:
    oid = pack_uuid(order.id)
    rows: list[list[InlineKeyboardButton]] = []
    if ready:
        rows.append(
            [button(t("btn.view_product", lang), OrderCB(action="product", oid=oid).pack())]
        )
    else:
        rows.append(
            [button(t("btn.refresh", lang), OrderCB(action="delivery", oid=oid).pack())]
        )
    if failed:
        rows.append([nav_button(t("btn.contact_support", lang), "support")])
    rows.append(
        [
            button(t("btn.view_order", lang), OrderCB(action="view", oid=oid).pack()),
            nav_button(t("btn.home", lang), "home"),
        ]
    )
    return build(rows)


def delivery_status_labels(status: DeliveryStatus) -> str:
    return {
        DeliveryStatus.PENDING: "⏳ Queued",
        DeliveryStatus.PROCESSING: "⏳ Preparing",
        DeliveryStatus.COMPLETED: "✅ Completed",
        DeliveryStatus.FAILED: "⚠️ Retrying",
        DeliveryStatus.CANCELLED: "❌ Cancelled",
    }[status]
