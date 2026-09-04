"""Screen text builders.

All customer-visible copy is assembled here from i18n keys, so the handlers
stay about flow control and these functions own presentation. Telegram HTML is
escaped for every value that originates from user or product data.
"""

from __future__ import annotations

from decimal import Decimal
from html import escape
from typing import Any

from app.core.money import format_amount
from app.core.security import mask_address
from app.core.timeutils import format_countdown, humanize_datetime, short_date
from app.db.models.catalog import Product
from app.db.models.order import Order
from app.db.models.payment import PaymentIntent
from app.domain.enums import DeliveryType, Language, OrderStatus, PaymentStatus
from app.domain.inventory.service import StockStatus
from app.i18n import t

DIVIDER = "────────────────"


def esc(value: Any) -> str:
    return escape(str(value), quote=False)


def money(amount: Decimal | str, currency: str | None = None) -> str:
    return esc(format_amount(amount, currency))


def code_block(value: str) -> str:
    """A tap-to-copy block. Telegram copies <code> content on tap."""
    return f"<code>{esc(value)}</code>"


# -- product ---------------------------------------------------------------


def stock_line(status: StockStatus, lang: Language) -> str:
    if not status.in_stock:
        return t("product.out_of_stock", lang)
    if status.is_unlimited:
        return t("product.in_stock", lang)
    if status.low_stock:
        return t("product.low_stock", lang, count=status.available)
    return t("product.in_stock", lang)


def product_card(product: Product, status: StockStatus, lang: Language) -> str:
    """Compact card used in listings."""
    flag = "🔥 " if product.is_best_seller else ("🆕 " if product.is_new_arrival else "")
    lines = [
        f"{flag}<b>{esc(product.display_name(lang.value))}</b>",
        money(product.price, product.currency),
        stock_line(status, lang),
    ]
    if product.short_description:
        lines.append(esc(product.short_description))
    return "\n".join(lines)


def product_details(product: Product, status: StockStatus, lang: Language) -> str:
    lines = [
        f"<b>{esc(product.display_name(lang.value))}</b>",
        "",
        money(product.price, product.currency),
        stock_line(status, lang),
    ]
    description = (
        product.full_description_bn
        if lang is Language.BN and product.full_description_bn
        else product.full_description
    ) or product.short_description
    if description:
        lines += ["", esc(description)]

    if product.features:
        lines += ["", t("product.features", lang)]
        lines += [f"• {esc(item)}" for item in product.features[:8]]
    if product.included_items:
        lines += ["", t("product.included", lang)]
        lines += [f"• {esc(item)}" for item in product.included_items[:8]]
    if product.requirements:
        lines += ["", t("product.requirements", lang)]
        lines += [f"• {esc(item)}" for item in product.requirements[:6]]
    if product.faq:
        lines += ["", t("product.faq", lang)]
        for entry in product.faq[:4]:
            if isinstance(entry, dict):
                lines.append(f"<b>{esc(entry.get('q', ''))}</b>")
                lines.append(esc(entry.get("a", "")))

    lines += ["", t("product.delivery_info", lang), esc(_delivery_label(product, lang))]
    if not status.in_stock:
        lines += ["", t("product.unavailable", lang)]
    return "\n".join(lines)


def _delivery_label(product: Product, lang: Language) -> str:
    if product.delivery_instructions:
        return product.delivery_instructions
    return {
        DeliveryType.STOCK_ITEM: "Instant delivery after payment confirmation.",
        DeliveryType.STATIC_PAYLOAD: "Instant delivery after payment confirmation.",
        DeliveryType.FILE: "File delivered instantly after payment confirmation.",
        DeliveryType.MANUAL: "Fulfilled manually by our team after payment.",
    }[product.delivery_type]


# -- checkout --------------------------------------------------------------


def checkout_screen(
    *,
    product_name: str,
    quantity: int,
    subtotal: Decimal,
    discount: Decimal,
    total: Decimal,
    currency: str,
    lang: Language,
    confirm: bool = False,
) -> str:
    title = t("checkout.confirm_title" if confirm else "checkout.title", lang)
    return "\n".join(
        [
            title,
            "",
            f"{t('checkout.product', lang)}:",
            esc(product_name),
            "",
            f"{t('checkout.quantity', lang)}:",
            str(quantity),
            "",
            f"{t('checkout.subtotal' if confirm else 'checkout.price', lang)}:",
            money(subtotal, currency),
            "",
            f"{t('checkout.discount', lang)}:",
            money(discount, currency),
            "",
            DIVIDER,
            f"{t('checkout.total', lang)}:",
            f"<b>{money(total, currency)}</b>",
        ]
    )


def coupon_applied(discount: Decimal, new_total: Decimal, currency: str, lang: Language) -> str:
    return "\n".join(
        [
            t("coupon.applied_title", lang),
            "",
            f"{t('checkout.discount', lang)}:",
            f"-{money(discount, currency)}",
            "",
            f"{t('coupon.new_total', lang)}:",
            f"<b>{money(new_total, currency)}</b>",
        ]
    )


# -- payment ---------------------------------------------------------------


def blockchain_payment_screen(intent: PaymentIntent, lang: Language) -> str:
    method = intent.method
    lines = [
        f"{method.emoji} <b>{esc(method.display_name).upper()}</b>",
        "",
        f"{t('payment.order', lang)}:",
        f"#{esc(intent.reference)}",
        "",
        f"{t('payment.amount', lang)}:",
        f"<b>{money(intent.expected_amount, intent.asset)}</b>",
        "",
        f"{t('payment.network', lang)}:",
        esc(method.network_label or method.network.value.upper()),
        "",
        f"{t('payment.receiving_address', lang)}:",
        code_block(intent.destination),
    ]
    if intent.memo:
        lines += [
            "",
            t("payment.memo_required", lang),
            code_block(intent.memo),
        ]
    lines += [
        "",
        t(
            "payment.only_send",
            lang,
            asset=esc(intent.asset),
            network=esc(method.network_label or method.network.value.upper()),
        ),
    ]
    if method.warning_text:
        lines.append(esc(method.warning_text))
    lines += ["", f"{t('payment.expires_in', lang)}:", format_countdown(intent.expires_at)]
    return "\n".join(lines)


def exchange_payment_screen(intent: PaymentIntent, lang: Language) -> str:
    method = intent.method
    lines = [
        f"{method.emoji} <b>{esc(method.display_name).upper()}</b>",
        "",
        f"{t('payment.order', lang)}:",
        f"#{esc(intent.reference)}",
        "",
        f"{t('payment.amount', lang)}:",
        f"<b>{money(intent.expected_amount, intent.asset)}</b>",
        "",
        f"{t('payment.destination', lang)}:",
        code_block(intent.destination),
        "",
        f"{t('payment.reference', lang)}:",
        code_block(intent.reference),
    ]
    if method.instructions:
        lines += ["", esc(method.instructions)]
    lines += ["", f"{t('payment.expires_in', lang)}:", format_countdown(intent.expires_at)]
    return "\n".join(lines)


def payment_submitted_screen(intent: PaymentIntent, lang: Language) -> str:
    return "\n".join(
        [
            t("payment.submitted_title", lang),
            "",
            t("payment.submitted_body", lang),
            "",
            f"{t('payment.order', lang)}:",
            f"#{esc(intent.reference)}",
            "",
            "Status:",
            t("payment.detecting", lang),
            "",
            t("payment.leave_screen", lang),
        ]
    )


def payment_status_screen(intent: PaymentIntent, lang: Language) -> str:
    """State-aware payment screen (sections 22-34).

    Chooses the right screen from the intent's status, so the customer always
    sees a message that matches reality.
    """
    status = intent.status
    reference = f"#{esc(intent.reference)}"

    if status is PaymentStatus.VERIFIED:
        return "\n".join(
            [
                t("payment.verified_title", lang),
                "",
                f"{t('payment.order', lang)}:",
                reference,
                "",
                f"{t('payment.amount', lang)}:",
                money(intent.received_amount or intent.expected_amount, intent.asset),
                "",
                f"{t('order.payment', lang)}:",
                t("order.verified", lang),
                "",
                t("payment.verified_body", lang),
            ]
        )

    if status is PaymentStatus.PENDING_CONFIRMATION:
        return "\n".join(
            [
                t("payment.confirmations_title", lang),
                "",
                f"{t('payment.amount', lang)}:",
                money(intent.received_amount or intent.expected_amount, intent.asset),
                "",
                f"{t('payment.confirmations', lang)}:",
                f"<b>{intent.confirmations} / {intent.required_confirmations}</b>",
                "",
                t("payment.confirmations_body", lang),
            ]
        )

    if status is PaymentStatus.DETECTED:
        return "\n".join(
            [
                t("payment.detected_title", lang),
                "",
                t("payment.detected_body", lang),
                "",
                f"{t('payment.amount', lang)}:",
                money(intent.received_amount or intent.expected_amount, intent.asset),
                "",
                t("payment.awaiting_confirmation", lang),
            ]
        )

    if status is PaymentStatus.UNDER_REVIEW:
        return _review_screen(intent, lang)

    if status is PaymentStatus.EXPIRED:
        return "\n".join(
            [
                t("payment.expired_title", lang),
                "",
                f"{t('payment.order', lang)}:",
                reference,
                "",
                t("payment.expired_body", lang),
            ]
        )

    if status is PaymentStatus.FAILED:
        return "\n".join([t("payment.failed_title", lang), "", t("payment.failed_body", lang)])

    if status in (PaymentStatus.VERIFYING, PaymentStatus.DETECTING):
        return "\n".join(
            [
                t("payment.verifying_title", lang),
                "",
                f"{t('payment.order', lang)}:",
                reference,
                "",
                t("payment.final_verification", lang),
            ]
        )

    return payment_submitted_screen(intent, lang)


def _review_screen(intent: PaymentIntent, lang: Language) -> str:
    """Specialised review screens for each anomaly (sections 29-34).

    The customer is told what happened in business terms; the technical
    evidence stays in the admin panel.
    """
    from app.domain.enums import VerificationOutcome

    outcome = intent.last_outcome
    expected = money(intent.expected_amount, intent.asset)
    received = money(intent.received_amount or Decimal("0"), intent.asset)

    if outcome is VerificationOutcome.UNDERPAID:
        shortfall = (intent.expected_amount - (intent.received_amount or Decimal("0")))
        return "\n".join(
            [
                t("payment.underpaid_title", lang),
                "",
                f"{t('payment.expected', lang)}:",
                expected,
                "",
                f"{t('payment.received', lang)}:",
                received,
                "",
                f"{t('payment.short', lang)}:",
                money(shortfall, intent.asset),
                "",
                t("payment.underpaid_body", lang),
            ]
        )

    if outcome is VerificationOutcome.OVERPAID:
        return "\n".join(
            [
                t("payment.overpaid_title", lang),
                "",
                f"{t('payment.expected', lang)}:",
                expected,
                "",
                f"{t('payment.received', lang)}:",
                received,
                "",
                t("payment.overpaid_body", lang),
            ]
        )

    if outcome is VerificationOutcome.WRONG_NETWORK:
        detected = _detected_network(intent)
        return "\n".join(
            [
                t("payment.wrong_network_title", lang),
                "",
                f"{t('payment.expected', lang)}:",
                f"{esc(intent.asset)} — {esc(intent.method.network_label or intent.network.value.upper())}",
                "",
                f"{t('payment.detected_label', lang)}:",
                esc(detected),
                "",
                t("payment.wrong_network_body", lang),
            ]
        )

    if outcome is VerificationOutcome.WRONG_ASSET:
        return "\n".join(
            [t("payment.wrong_asset_title", lang), "", t("payment.wrong_asset_body", lang)]
        )

    if outcome is VerificationOutcome.DUPLICATE:
        return "\n".join(
            [t("payment.duplicate_title", lang), "", t("payment.duplicate_body", lang)]
        )

    return "\n".join([t("payment.review_title", lang), "", t("payment.review_body", lang)])


def _detected_network(intent: PaymentIntent) -> str:
    """Read the observed network from the last verification evidence."""
    config = intent.verification_config or {}
    return str(config.get("detected_network") or "another network")


def verification_progress(intent: PaymentIntent, checks: dict[str, Any], lang: Language) -> str:
    """The tick-list screen (section 23), built from real check evidence."""
    lines = [t("payment.verifying_title", lang), "", f"{t('payment.order', lang)}:", f"#{esc(intent.reference)}", ""]
    labels = [
        ("transaction_successful", "payment.check_found"),
        ("asset", "payment.check_asset"),
        ("network", "payment.check_network"),
        ("receiver", "payment.check_receiver"),
        ("amount", "payment.check_amount"),
    ]
    for key, label in labels:
        entry = checks.get(key)
        if entry is None:
            continue
        mark = "✓" if entry.get("passed") else "✗"
        lines.append(f"{mark} {t(label, lang).lstrip('✓ ')}")
    lines += ["", t("payment.final_verification", lang)]
    return "\n".join(lines)


# -- orders ----------------------------------------------------------------


ORDER_STATUS_LABEL: dict[OrderStatus, str] = {
    OrderStatus.CREATED: "🆕 Created",
    OrderStatus.PAYMENT_PENDING: "⏳ Awaiting payment",
    OrderStatus.PAYMENT_VERIFIED: "✅ Paid",
    OrderStatus.FULFILLING: "📦 Preparing",
    OrderStatus.DELIVERED: "✅ Delivered",
    OrderStatus.COMPLETED: "✅ Completed",
    OrderStatus.CANCELLED: "❌ Cancelled",
    OrderStatus.EXPIRED: "⚠️ Expired",
    OrderStatus.MANUAL_REVIEW: "🔎 Under review",
    OrderStatus.DELIVERY_FAILED: "⚠️ Delivery delayed",
    OrderStatus.REFUNDED: "↩️ Refunded",
}


def order_row(order: Order) -> str:
    item = order.items[0] if order.items else None
    name = item.product_name if item else "-"
    return (
        f"#{esc(order.reference)} · {esc(name)}\n"
        f"{money(order.total, order.currency)} · {ORDER_STATUS_LABEL.get(order.status, order.status.value)}"
        f" · {short_date(order.created_at)}"
    )


def order_details(
    order: Order, lang: Language, *, delivery_summary: dict[str, Any] | None = None
) -> str:
    item = order.items[0] if order.items else None
    lines = [
        f"📦 <b>ORDER #{esc(order.reference)}</b>",
        "",
        f"{t('checkout.product', lang)}:",
        esc(item.product_name if item else "-"),
        "",
        f"{t('checkout.quantity', lang)}:",
        str(item.quantity if item else 0),
        "",
        f"{t('checkout.total', lang)}:",
        f"<b>{money(order.total, order.currency)}</b>",
    ]
    if order.discount_total and order.discount_total > 0:
        lines += ["", f"{t('checkout.discount', lang)}:", money(order.discount_total, order.currency)]
    lines += [
        "",
        f"{t('order.payment', lang)}:",
        t("order.verified", lang) if order.status.is_paid else t("order.pending", lang),
        "",
        f"{t('order.delivery', lang)}:",
        _delivery_status_line(order, delivery_summary, lang),
        "",
        f"{t('order.created', lang)}:",
        short_date(order.created_at),
        "",
        f"Status: {ORDER_STATUS_LABEL.get(order.status, order.status.value)}",
    ]
    return "\n".join(lines)


def _delivery_status_line(
    order: Order, summary: dict[str, Any] | None, lang: Language
) -> str:
    if order.status in (OrderStatus.COMPLETED, OrderStatus.DELIVERED):
        return t("order.complete", lang)
    if order.status is OrderStatus.DELIVERY_FAILED:
        return "⚠️ Delayed"
    if summary and summary.get("total"):
        return f"{summary['completed']}/{summary['total']}"
    return t("order.pending", lang)


def receipt(order: Order, intent: PaymentIntent | None, lang: Language) -> str:
    item = order.items[0] if order.items else None
    lines = [
        "📄 <b>RECEIPT</b>",
        "",
        f"{t('payment.order', lang)}: #{esc(order.reference)}",
        f"{t('order.created', lang)}: {humanize_datetime(order.created_at)}",
        "",
        DIVIDER,
        f"{esc(item.product_name if item else '-')} × {item.quantity if item else 0}",
        f"{t('checkout.subtotal', lang)}: {money(order.subtotal, order.currency)}",
        f"{t('checkout.discount', lang)}: {money(order.discount_total, order.currency)}",
        f"<b>{t('checkout.total', lang)}: {money(order.total, order.currency)}</b>",
        DIVIDER,
    ]
    if intent is not None:
        lines += [
            "",
            f"{t('order.payment', lang)}: {esc(intent.method.display_name)}",
            f"{t('payment.network', lang)}: {esc(intent.method.network_label or intent.network.value)}",
            f"{t('payment.amount', lang)}: {money(intent.received_amount or intent.expected_amount, intent.asset)}",
        ]
        if intent.verified_at:
            lines.append(f"Verified: {humanize_datetime(intent.verified_at)}")
        if intent.destination:
            lines.append(f"To: {esc(mask_address(intent.destination))}")
    return "\n".join(lines)


def delivered_product(
    *, product_name: str, payloads: list[str], order_reference: str, lang: Language
) -> str:
    lines = [
        t("product.your_product", lang),
        "",
        f"<b>{esc(product_name)}</b>",
        "",
    ]
    for payload in payloads:
        lines.append(code_block(payload))
    lines += [
        "",
        t("product.keep_secure", lang),
        "",
        f"{t('payment.order', lang)}:",
        f"#{esc(order_reference)}",
    ]
    return "\n".join(lines)
