"""Admin panel tests: every screen rendered through the real dispatcher.

The admin panel is large and mostly reachable only after authentication, so
these walk it screen by screen. They catch runtime errors that no import check
would, and they assert the access-control rules rather than assuming them.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio

from app.admin.handlers.product_edit import ref
from app.bot.callbacks import AdminCB
from app.db.models.user import Role
from app.domain.enums import AuditAction
from tests.factories import add_stock, make_category, make_payment_method, make_product
from tests.integration.conftest import (
    TELEGRAM_ID,
    callback_update,
    feed,
    message_update,
    photo_update,
)

ADMIN_SECTIONS = [
    "dashboard",
    "orders",
    "payments",
    "products",
    "inventory",
    "categories",
    "users",
    "resellers",
    "support",
    "coupons",
    "analytics",
    "providers",
    "reconciliation",
    "audit",
    "broadcast",
    "settings",
    "refunds",
]


@pytest_asyncio.fixture
async def admin_session(session, monkeypatch):
    """Grant the test user SUPER_ADMIN via the bootstrap path."""
    from app.admin.permissions.rbac import ROLE_PERMISSIONS
    from app.core.config import get_settings
    from app.db.models.user import Permission

    settings = get_settings()
    monkeypatch.setattr(settings.telegram, "bootstrap_admin_ids", [TELEGRAM_ID])

    permissions = {}
    for code in {p.value for perms in ROLE_PERMISSIONS.values() for p in perms}:
        permission = Permission(code=code, description=code)
        session.add(permission)
        permissions[code] = permission
    await session.flush()

    for role_name, granted in ROLE_PERMISSIONS.items():
        role = Role(name=role_name, description=role_name.value)
        role.permissions = [permissions[p.value] for p in granted]
        session.add(role)
    await session.commit()
    return session


async def _open_admin(harness) -> None:
    await feed(harness, message_update("/start"))
    await feed(harness, message_update("/admin", update_id=100))


@pytest.mark.parametrize("section", ADMIN_SECTIONS)
async def test_every_admin_section_renders(bot_harness, admin_session, section):
    """Each panel must render real content, not an error."""
    await _open_admin(bot_harness)
    recorder = await feed(
        bot_harness, callback_update(AdminCB(section=section).pack(), update_id=101)
    )

    assert recorder.texts, f"admin section {section!r} rendered nothing"
    body = recorder.texts[0]
    assert "Something went wrong" not in body, f"{section} errored: {body[:200]}"
    assert "expired" not in body.lower(), f"{section} was treated as a stale callback"
    assert recorder.buttons, f"admin section {section!r} rendered no navigation"


async def test_dashboard_leads_with_actionable_items(bot_harness, admin_session):
    await _open_admin(bot_harness)
    recorder = await feed(
        bot_harness, callback_update(AdminCB(section="dashboard").pack(), update_id=102)
    )
    body = recorder.texts[0]
    assert "ADMIN DASHBOARD" in body
    assert "Today" in body
    # With an empty system there is nothing to action, and it says so.
    assert "Nothing needs attention" in body


async def test_products_section_lists_and_opens_a_product(bot_harness, admin_session, session):
    category = await make_category(session)
    product = await make_product(session, category=category)
    await add_stock(session, product, count=4)
    await session.commit()

    await _open_admin(bot_harness)
    listing = await feed(
        bot_harness, callback_update(AdminCB(section="products").pack(), update_id=103)
    )
    assert "PRODUCTS" in listing.texts[0]
    assert "Premium License" in listing.texts[0]
    assert any("Add product" in b for b in listing.buttons)

    detail = await feed(
        bot_harness,
        callback_update(
            AdminCB(section="products", action="view", arg=product.id.hex).pack(), update_id=104
        ),
    )
    body = detail.texts[0]
    assert "Premium License" in body
    assert "Available: 4" in body
    assert any("Add stock" in b for b in detail.buttons)


async def test_inventory_never_shows_the_secret_payload(bot_harness, admin_session, session):
    """Admin screens show a preview, never the key itself."""
    product = await make_product(session)
    await add_stock(session, product, count=3)
    await session.commit()

    await _open_admin(bot_harness)
    recorder = await feed(
        bot_harness,
        callback_update(
            AdminCB(section="inventory", action="view", arg=product.id.hex).pack(), update_id=105
        ),
    )
    body = recorder.texts[0]
    assert "Available: <b>3</b>" in body
    # The factory writes payloads like "XXXX-YYYY-ZZZZ-0000"; only the masked
    # preview may appear.
    assert "XXXX-YYYY" not in body
    assert "****" in body


async def test_provider_screen_never_reveals_credentials(bot_harness, admin_session, session):
    from app.core.security import get_secret_box
    from app.db.models.payment import PaymentProvider

    method = await make_payment_method(session)
    provider = await session.get(PaymentProvider, method.provider_id)
    box = get_secret_box()
    provider.encrypted_api_key = box.encrypt("SUPERSECRETKEY123456")
    provider.encrypted_api_secret = box.encrypt("SUPERSECRETSECRET789")
    provider.api_key_hint = "3456"
    await session.commit()

    await _open_admin(bot_harness)
    recorder = await feed(
        bot_harness,
        callback_update(
            AdminCB(section="providers", action="view", arg=provider.id.hex).pack(),
            update_id=106,
        ),
    )
    body = recorder.texts[0]
    assert "SUPERSECRETKEY" not in body
    assert "SUPERSECRETSECRET" not in body
    assert provider.encrypted_api_key not in body
    # Only the masked hint and a configured/not-set indicator, so an operator
    # can tell which key is in place without it being readable.
    assert "****3456" in body
    assert "configured" in body


async def test_payments_review_queue_renders(bot_harness, admin_session):
    await _open_admin(bot_harness)
    recorder = await feed(
        bot_harness, callback_update(AdminCB(section="payments").pack(), update_id=107)
    )
    body = recorder.texts[0]
    assert "PAYMENTS" in body
    assert any("Review" in b for b in recorder.buttons)


async def test_high_risk_action_requires_a_reason_then_a_confirmation(
    bot_harness, admin_session, session
):
    """Section 114: approval is never one tap."""

    from app.db.repositories.users import UserRepository
    from app.domain.enums import OrderStatus
    from app.domain.orders.service import OrderService
    from app.domain.payments.service import PaymentService

    product = await make_product(session, price="10.00")
    await add_stock(session, product, count=2)
    method = await make_payment_method(session)
    user = await UserRepository(session).get_by_telegram_id(TELEGRAM_ID)
    if user is None:
        from tests.factories import make_user

        user = await make_user(session, telegram_id=TELEGRAM_ID)

    orders = OrderService(session)
    quote = await orders.quote(product=product, quantity=1, user=user)
    order = await orders.create_order(quote=quote, user=user)
    await orders.transition(order, OrderStatus.PAYMENT_PENDING)
    payments = PaymentService(session)
    intent = await payments.create_intent(order=order, method=method)
    await payments._to_review(intent, "needs a human")
    await session.commit()

    await _open_admin(bot_harness)

    detail = await feed(
        bot_harness,
        callback_update(
            AdminCB(section="payments", action="view", arg=intent.id.hex).pack(), update_id=108
        ),
    )
    assert "PAYMENT REVIEW" in detail.texts[0]
    assert "needs a human" in detail.texts[0]
    assert any("Approve" in b for b in detail.buttons)

    # Tapping Approve asks for a reason rather than approving.
    approve = await feed(
        bot_harness,
        callback_update(
            AdminCB(section="payments", action="approve", arg=intent.id.hex).pack(),
            update_id=109,
        ),
    )
    assert "reason" in approve.texts[0].lower()
    await session.refresh(intent)
    assert intent.status.value == "under_review", "approval must not happen on the first tap"

    # Supplying a reason produces a confirmation, still not an approval.
    confirm = await feed(
        bot_harness, message_update("customer confirmed by email", update_id=110)
    )
    assert "CONFIRM APPROVE" in confirm.texts[0]
    assert any("Approve" in b for b in confirm.buttons)
    await session.refresh(intent)
    assert intent.status.value == "under_review", "approval must not happen before confirmation"


async def test_refund_flow_requires_amount_reason_then_confirmation(
    bot_harness, admin_session, session, monkeypatch
):
    """A refund is never one tap: amount + reason, then an explicit confirm."""
    from tests.integration.test_refunds import _paid_order

    order, _ = await _paid_order(session, monkeypatch, price="20.00")
    await session.commit()

    await _open_admin(bot_harness)

    start = await feed(
        bot_harness,
        callback_update(
            AdminCB(section="refunds", action="new", arg=order.id.hex).pack(), update_id=120
        ),
    )
    assert "CREATE REFUND" in start.texts[0]
    assert "Refundable" in start.texts[0]

    entered = await feed(bot_harness, message_update("5.00 duplicate payment", update_id=121))
    assert "CONFIRM REFUND" in entered.texts[0]
    assert "duplicate payment" in entered.texts[0]

    from app.db.repositories.orders import RefundRepository

    assert await RefundRepository(session).list_for_order(order.id) == [], (
        "no refund may be recorded before the confirmation"
    )


async def test_a_non_admin_cannot_reach_any_admin_section(bot_harness, session):
    """No admin callback may render for an ordinary customer."""
    await feed(bot_harness, message_update("/start"))
    for section in ("dashboard", "payments", "providers", "settings"):
        recorder = await feed(
            bot_harness, callback_update(AdminCB(section=section).pack(), update_id=111)
        )
        rendered = " ".join(recorder.texts)
        assert "ADMIN" not in rendered.upper() or "expired" in rendered.lower(), (
            f"customer reached admin section {section!r}: {rendered[:160]}"
        )


# --- product editing ------------------------------------------------------


async def test_editing_a_field_validates_before_it_saves(bot_harness, admin_session, session):
    """A bad price is refused and the product keeps its old value."""
    product = await make_product(session, price="15.00")
    await session.commit()
    pid = product.id.hex

    await _open_admin(bot_harness)
    menu = await feed(
        bot_harness,
        callback_update(AdminCB(section="pedit", action="menu", arg=pid).pack(), update_id=120),
    )
    assert "EDIT — Premium License" in menu.texts[0]

    await feed(
        bot_harness,
        callback_update(
            AdminCB(section="pedit", action="field", arg=f"{pid}.price").pack(), update_id=121
        ),
    )

    rejected = await feed(bot_harness, message_update("not a price", update_id=122))
    assert "not a valid number" in rejected.texts[0]
    await session.refresh(product)
    assert str(product.price) == "15.00000000"

    # Still in the state, so a corrected value goes straight through.
    accepted = await feed(bot_harness, message_update("22.50", update_id=123))
    assert "EDIT — Premium License" in accepted.texts[0]
    await session.refresh(product)
    assert product.price == Decimal("22.50")


async def test_a_negative_price_is_refused(bot_harness, admin_session, session):
    product = await make_product(session, price="15.00", sku="SKU-NEG")
    await session.commit()
    pid = product.id.hex

    await _open_admin(bot_harness)
    await feed(
        bot_harness,
        callback_update(
            AdminCB(section="pedit", action="field", arg=f"{pid}.price").pack(), update_id=124
        ),
    )
    recorder = await feed(bot_harness, message_update("-5", update_id=125))
    assert "greater than zero" in recorder.texts[0]
    await session.refresh(product)
    assert product.price == Decimal("15.00")


async def test_field_edit_is_audited(bot_harness, admin_session, session):
    from app.db.repositories.support import AuditRepository

    product = await make_product(session, sku="SKU-AUD")
    await session.commit()
    pid = product.id.hex

    await _open_admin(bot_harness)
    await feed(
        bot_harness,
        callback_update(
            AdminCB(section="pedit", action="field", arg=f"{pid}.name").pack(), update_id=126
        ),
    )
    await feed(bot_harness, message_update("Renamed License", update_id=127))

    await session.refresh(product)
    assert product.name == "Renamed License"

    entries = await AuditRepository(session).list_recent(per_page=20)
    updates = [e for e in entries.items if e.action is AuditAction.PRODUCT_UPDATED]
    assert updates, "a product edit must leave an audit trail"
    assert updates[0].details["field"] == "name"


async def test_reseller_sales_cannot_be_enabled_without_a_wholesale_price(
    bot_harness, admin_session, session
):
    """Otherwise resellers would buy at the retail price."""
    product = await make_product(session, sku="SKU-RES")
    assert product.reseller_price is None
    await session.commit()
    pid = product.id.hex

    await _open_admin(bot_harness)
    recorder = await feed(
        bot_harness,
        callback_update(
            AdminCB(section="pedit", action="toggle", arg=f"{pid}.resellers").pack(),
            update_id=128,
        ),
    )
    assert "reseller wholesale price" in recorder.texts[0]
    await session.refresh(product)
    assert product.available_to_resellers is False

    # With a wholesale price set it goes through.
    product.reseller_price = Decimal("9.00")
    await session.commit()
    await feed(
        bot_harness,
        callback_update(
            AdminCB(section="pedit", action="toggle", arg=f"{pid}.resellers").pack(),
            update_id=129,
        ),
    )
    await session.refresh(product)
    assert product.available_to_resellers is True


async def test_changing_the_category_from_the_edit_menu(bot_harness, admin_session, session):
    category = await make_category(session, slug="tools")
    product = await make_product(session, sku="SKU-CAT")
    await session.commit()

    await _open_admin(bot_harness)
    picker = await feed(
        bot_harness,
        callback_update(
            AdminCB(section="pedit", action="category", arg=product.id.hex).pack(),
            update_id=130,
        ),
    )
    assert any("License Keys" in b for b in picker.buttons)

    await feed(
        bot_harness,
        callback_update(
            AdminCB(
                section="pedit", action="setcat", arg=f"{product.id.hex}.{ref(category.id)}"
            ).pack(),
            update_id=131,
        ),
    )
    await session.refresh(product)
    assert product.category_id == category.id


# --- product media --------------------------------------------------------


async def test_uploading_a_photo_stores_the_largest_file_id(
    bot_harness, admin_session, session
):
    product = await make_product(session, sku="SKU-IMG")
    await session.commit()
    product_id = product.id
    pid = product_id.hex

    await _open_admin(bot_harness)
    prompt = await feed(
        bot_harness,
        callback_update(AdminCB(section="pmedia", action="add", arg=pid).pack(), update_id=132),
    )
    assert "ADD IMAGE" in prompt.texts[0]

    recorder = await feed(bot_harness, photo_update("PHOTO-FULL-ID", update_id=133))
    assert "1 image(s)" in recorder.texts[0]

    from app.db.repositories.catalog import ProductRepository

    reloaded = await ProductRepository(session).get_with_media(product_id)
    assert [m.file_id for m in reloaded.media] == ["PHOTO-FULL-ID"]


async def test_a_non_photo_upload_is_refused_and_the_state_is_kept(
    bot_harness, admin_session, session
):
    product = await make_product(session, sku="SKU-IMG2")
    await session.commit()
    pid = product.id.hex

    await _open_admin(bot_harness)
    await feed(
        bot_harness,
        callback_update(AdminCB(section="pmedia", action="add", arg=pid).pack(), update_id=134),
    )
    refused = await feed(bot_harness, message_update("here is my image", update_id=135))
    assert "Send a photo" in refused.texts[0]

    # The state survived, so the operator can simply send the photo now.
    accepted = await feed(bot_harness, photo_update("PHOTO-SECOND", update_id=136))
    assert "1 image(s)" in accepted.texts[0]


async def test_making_an_image_primary_reorders_it_first(bot_harness, admin_session, session):
    from app.db.repositories.catalog import ProductRepository

    product = await make_product(session, sku="SKU-IMG3")
    await session.commit()
    product_id = product.id
    pid = product_id.hex

    await _open_admin(bot_harness)
    for index, update_id in enumerate((137, 139), start=1):
        await feed(
            bot_harness,
            callback_update(
                AdminCB(section="pmedia", action="add", arg=pid).pack(), update_id=update_id
            ),
        )
        await feed(bot_harness, photo_update(f"PHOTO-{index}", update_id=update_id + 1))

    reloaded = await ProductRepository(session).get_with_media(product_id)
    assert [m.file_id for m in reloaded.media] == ["PHOTO-1", "PHOTO-2"]
    second = reloaded.media[1]

    await feed(
        bot_harness,
        callback_update(
            AdminCB(section="pmedia", action="primary", arg=f"{pid}.{ref(second.id)}").pack(),
            update_id=141,
        ),
    )
    reloaded = await ProductRepository(session).get_with_media(product_id)
    assert [m.file_id for m in reloaded.media] == ["PHOTO-2", "PHOTO-1"]

    await feed(
        bot_harness,
        callback_update(
            AdminCB(section="pmedia", action="remove", arg=f"{pid}.{ref(second.id)}").pack(),
            update_id=142,
        ),
    )
    reloaded = await ProductRepository(session).get_with_media(product_id)
    assert [m.file_id for m in reloaded.media] == ["PHOTO-1"]


# --- categories -----------------------------------------------------------


async def test_creating_a_category_generates_a_unique_slug(bot_harness, admin_session, session):
    from app.db.repositories.catalog import CategoryRepository

    await _open_admin(bot_harness)
    await feed(
        bot_harness,
        callback_update(AdminCB(section="categories", action="new").pack(), update_id=143),
    )
    created = await feed(bot_harness, message_update("Gift Cards", update_id=144))
    assert "Gift Cards" in created.texts[0]

    repository = CategoryRepository(session)
    first = await repository.get_by_slug("gift-cards")
    assert first is not None
    assert first.is_active is True

    # A second category with the same name must not collide on the unique slug.
    await feed(
        bot_harness,
        callback_update(AdminCB(section="categories", action="new").pack(), update_id=145),
    )
    await feed(bot_harness, message_update("Gift Cards", update_id=146))
    assert await repository.get_by_slug("gift-cards-2") is not None


async def test_a_non_latin_category_name_still_gets_a_usable_slug(
    bot_harness, admin_session, session
):
    from app.db.repositories.catalog import CategoryRepository

    await _open_admin(bot_harness)
    await feed(
        bot_harness,
        callback_update(AdminCB(section="categories", action="new").pack(), update_id=147),
    )
    await feed(bot_harness, message_update("গিফট কার্ড", update_id=148))

    categories = await CategoryRepository(session).list_all()
    match = next(c for c in categories if c.name_en == "গিফট কার্ড")
    assert match.slug, "a Bengali-only name must still produce a slug"
    assert match.slug.startswith("cat-")


async def test_an_empty_category_name_is_refused(bot_harness, admin_session, session):
    from app.db.repositories.catalog import CategoryRepository

    await _open_admin(bot_harness)
    await feed(
        bot_harness,
        callback_update(AdminCB(section="categories", action="new").pack(), update_id=149),
    )
    refused = await feed(bot_harness, message_update("   ", update_id=150))
    assert "cannot be empty" in refused.texts[0]
    assert await CategoryRepository(session).list_all() == []


async def test_renaming_a_category_and_hiding_it_from_the_shop(
    bot_harness, admin_session, session
):
    category = await make_category(session)
    await session.commit()
    cid = category.id.hex

    await _open_admin(bot_harness)
    await feed(
        bot_harness,
        callback_update(
            AdminCB(section="categories", action="field", arg=f"{cid}.name").pack(),
            update_id=151,
        ),
    )
    await feed(bot_harness, message_update("Software Keys", update_id=152))
    await session.refresh(category)
    assert category.name_en == "Software Keys"

    await feed(
        bot_harness,
        callback_update(
            AdminCB(section="categories", action="toggle", arg=cid).pack(), update_id=153
        ),
    )
    await session.refresh(category)
    assert category.is_active is False


async def test_archiving_is_refused_while_products_still_use_the_category(
    bot_harness, admin_session, session
):
    """Archiving would orphan the products, so it is blocked, not cascaded."""
    from app.core.timeutils import utcnow

    category = await make_category(session)
    product = await make_product(session, category=category, sku="SKU-ARCH")
    await session.commit()
    cid = category.id.hex

    await _open_admin(bot_harness)
    detail = await feed(
        bot_harness,
        callback_update(
            AdminCB(section="categories", action="view", arg=cid).pack(), update_id=154
        ),
    )
    assert "Products: 1" in detail.texts[0]
    assert not any("Archive" in b for b in detail.buttons), (
        "the archive button must not be offered while products remain"
    )

    refused = await feed(
        bot_harness,
        callback_update(
            AdminCB(section="categories", action="archive", arg=cid).pack(), update_id=155
        ),
    )
    assert "still use this category" in refused.texts[0]
    await session.refresh(category)
    assert category.deleted_at is None

    # Once the product is gone the category can be archived.
    product.deleted_at = utcnow()
    await session.commit()
    await feed(
        bot_harness,
        callback_update(
            AdminCB(section="categories", action="archive", arg=cid).pack(), update_id=156
        ),
    )
    await session.refresh(category)
    assert category.deleted_at is not None
    assert category.is_active is False


async def test_an_archived_category_disappears_from_the_list(
    bot_harness, admin_session, session
):
    from app.core.timeutils import utcnow

    category = await make_category(session)
    category.deleted_at = utcnow()
    await session.commit()

    await _open_admin(bot_harness)
    recorder = await feed(
        bot_harness, callback_update(AdminCB(section="categories").pack(), update_id=157)
    )
    assert "License Keys" not in recorder.texts[0]
    assert "0 category(ies)" in recorder.texts[0]


async def test_a_staff_member_without_the_permission_cannot_manage_categories(
    bot_harness, session, monkeypatch
):
    """RBAC is checked per handler, not only on the dashboard."""
    from app.admin.permissions.rbac import ROLE_PERMISSIONS
    from app.core.config import get_settings
    from app.db.models.user import Permission
    from app.db.repositories.users import UserRepository
    from app.domain.enums import RoleName

    settings = get_settings()
    monkeypatch.setattr(settings.telegram, "bootstrap_admin_ids", [])

    permissions = {}
    for code in {p.value for perms in ROLE_PERMISSIONS.values() for p in perms}:
        permission = Permission(code=code, description=code)
        session.add(permission)
        permissions[code] = permission
    await session.flush()
    role = Role(name=RoleName.SUPPORT_AGENT, description="support")
    role.permissions = [permissions[p.value] for p in ROLE_PERMISSIONS[RoleName.SUPPORT_AGENT]]
    session.add(role)
    await session.commit()

    await feed(bot_harness, message_update("/start"))
    user = await UserRepository(session).get_by_telegram_id(TELEGRAM_ID)
    await session.execute(
        Role.__table__.metadata.tables["user_roles"].insert().values(
            user_id=user.id, role_id=role.id
        )
    )
    await session.commit()

    recorder = await feed(
        bot_harness, callback_update(AdminCB(section="categories").pack(), update_id=158)
    )
    rendered = " ".join(recorder.texts + recorder.toasts)
    assert "CATEGORIES" not in rendered.upper()
