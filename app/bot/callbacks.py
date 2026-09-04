"""Typed callback data.

aiogram 3's ``CallbackData`` factory keeps callback payloads under Telegram's
64-byte limit and makes them parse-safe: a malformed or tampered payload fails
to parse instead of reaching a handler with unexpected values.

UUIDs are passed as their 32-char hex form (no dashes) to save 4 bytes each.
"""

from __future__ import annotations

import uuid

from aiogram.filters.callback_data import CallbackData


def pack_uuid(value: uuid.UUID | str) -> str:
    return uuid.UUID(str(value)).hex


def unpack_uuid(value: str) -> uuid.UUID:
    return uuid.UUID(value)


class Nav(CallbackData, prefix="nav"):
    """Plain navigation between screens."""

    to: str
    arg: str = ""


class ShopCB(CallbackData, prefix="shop"):
    action: str  # categories | category | flag | search | product
    ref: str = ""
    page: int = 1


class ProductCB(CallbackData, prefix="prod"):
    action: str  # view | buy | notify | media
    pid: str
    qty: int = 1


class CheckoutCB(CallbackData, prefix="chk"):
    action: str  # open | qty | coupon | clear_coupon | confirm | cancel
    pid: str = ""
    qty: int = 1


class OrderCB(CallbackData, prefix="ord"):
    action: str  # list | view | pay | cancel | product | receipt | delivery | reorder
    oid: str = ""
    page: int = 1
    arg: str = ""


class PayCB(CallbackData, prefix="pay"):
    action: str  # methods | select | screen | paid | submit | status | qr | new | copy
    oid: str = ""
    ref: str = ""


class ProfileCB(CallbackData, prefix="prof"):
    action: str  # view | referral | notifications | settings | language | mark_read
    arg: str = ""
    page: int = 1


class SupportCB(CallbackData, prefix="sup"):
    action: str  # menu | category | tickets | view | reply | close
    arg: str = ""
    page: int = 1


class ResellerCB(CallbackData, prefix="res"):
    action: str  # center | activate | terms | accept | dashboard | keys | create_key
    arg: str = ""
    page: int = 1


class AdminCB(CallbackData, prefix="adm"):
    """Admin panel navigation.

    ``section`` is the screen, ``action`` the operation, ``arg`` the target.
    Every handler re-checks the operator's permission; the callback data is
    never treated as authorisation.
    """

    section: str
    action: str = "open"
    arg: str = ""
    page: int = 1


class ConfirmCB(CallbackData, prefix="cfm"):
    """Two-step confirmation for destructive/high-risk actions.

    ``token`` is a short-lived Redis key holding the pending operation, so a
    stale confirmation button cannot replay an action.
    """

    token: str
    decision: str  # yes | no


class PageCB(CallbackData, prefix="pg"):
    """Shared pagination control."""

    scope: str
    page: int
    arg: str = ""


class NoopCB(CallbackData, prefix="noop"):
    """Inert button (page indicators, section headers)."""

    tag: str = ""
