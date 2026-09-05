"""FSM state groups for multi-step flows."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class CheckoutFlow(StatesGroup):
    entering_coupon = State()
    entering_quantity = State()
    entering_payment_reference = State()
    searching = State()


class SupportFlow(StatesGroup):
    describing_issue = State()
    replying = State()


class AdminFlow(StatesGroup):
    searching = State()
    # Product creation wizard
    product_name = State()
    product_category = State()
    product_description = State()
    product_price = State()
    product_delivery_type = State()
    product_stock = State()
    product_media = State()
    # Product editing: the field being edited is kept in FSM data.
    product_edit_value = State()
    product_media_upload = State()
    # Categories
    category_name = State()
    category_edit_value = State()
    # Inventory
    adding_stock = State()
    # Coupons
    coupon_code = State()
    coupon_value = State()
    coupon_limits = State()
    # Providers
    provider_api_key = State()
    provider_api_secret = State()
    provider_passphrase = State()
    method_address = State()
    method_contract = State()
    method_confirmations = State()
    method_rate = State()
    # Support / broadcast
    support_reply = State()
    broadcast_message = State()
    # Generic reason capture for high-risk actions
    action_reason = State()
