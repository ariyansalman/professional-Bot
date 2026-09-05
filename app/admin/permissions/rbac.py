"""Role-based access control.

Permissions are granular strings; roles are bundles of them. A user's effective
permission set is the union of every role they hold. Super admin is the only
role granted a wildcard, and even it is checked explicitly rather than by
bypassing the check.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from app.core.exceptions import PermissionDeniedError
from app.domain.enums import RoleName


class Permissions(StrEnum):
    """Every guarded capability in the admin panel."""

    # Dashboard / read
    DASHBOARD_VIEW = "dashboard.view"
    ANALYTICS_VIEW = "analytics.view"
    AUDIT_VIEW = "audit.view"
    LOGS_VIEW = "logs.view"

    # Orders
    ORDERS_VIEW = "orders.view"
    ORDERS_CANCEL = "orders.cancel"
    ORDERS_FORCE_DELIVERY = "orders.force_delivery"

    # Payments
    PAYMENTS_VIEW = "payments.view"
    PAYMENTS_RECHECK = "payments.recheck"
    PAYMENTS_APPROVE = "payments.approve"
    PAYMENTS_REJECT = "payments.reject"
    REFUNDS_CREATE = "refunds.create"
    REFUNDS_COMPLETE = "refunds.complete"
    RECONCILIATION_RESOLVE = "reconciliation.resolve"

    # Catalog
    PRODUCTS_VIEW = "products.view"
    PRODUCTS_MANAGE = "products.manage"
    PRODUCTS_ARCHIVE = "products.archive"
    CATEGORIES_MANAGE = "categories.manage"
    INVENTORY_VIEW = "inventory.view"
    INVENTORY_MANAGE = "inventory.manage"
    INVENTORY_ADJUST = "inventory.adjust"

    # Commerce
    COUPONS_VIEW = "coupons.view"
    COUPONS_MANAGE = "coupons.manage"
    REFERRALS_VIEW = "referrals.view"

    # Users
    USERS_VIEW = "users.view"
    USERS_MANAGE = "users.manage"
    USERS_BAN = "users.ban"

    # Resellers
    RESELLERS_VIEW = "resellers.view"
    RESELLERS_MANAGE = "resellers.manage"
    RESELLERS_KEYS = "resellers.keys"

    # Support
    SUPPORT_VIEW = "support.view"
    SUPPORT_REPLY = "support.reply"
    SUPPORT_ASSIGN = "support.assign"

    # Providers / infrastructure
    PROVIDERS_VIEW = "providers.view"
    PROVIDERS_MANAGE = "providers.manage"
    PROVIDERS_CREDENTIALS = "providers.credentials"
    PAYMENT_METHODS_MANAGE = "payment_methods.manage"
    BLOCKCHAIN_MANAGE = "blockchain.manage"

    # System
    SETTINGS_MANAGE = "settings.manage"
    MAINTENANCE_TOGGLE = "maintenance.toggle"
    BROADCAST_SEND = "broadcast.send"
    ROLES_MANAGE = "roles.manage"


PERMISSIONS: tuple[Permissions, ...] = tuple(Permissions)

#: Permissions that move money, change where money arrives, or destroy data.
#: The UI must require an explicit confirmation step for each of these.
HIGH_RISK_PERMISSIONS: frozenset[Permissions] = frozenset(
    {
        Permissions.PAYMENTS_APPROVE,
        Permissions.PAYMENTS_REJECT,
        Permissions.REFUNDS_CREATE,
        Permissions.REFUNDS_COMPLETE,
        Permissions.PROVIDERS_CREDENTIALS,
        Permissions.PAYMENT_METHODS_MANAGE,
        Permissions.BLOCKCHAIN_MANAGE,
        Permissions.INVENTORY_ADJUST,
        Permissions.PRODUCTS_ARCHIVE,
        Permissions.USERS_BAN,
        Permissions.BROADCAST_SEND,
        Permissions.MAINTENANCE_TOGGLE,
        Permissions.ROLES_MANAGE,
        Permissions.ORDERS_FORCE_DELIVERY,
    }
)

_READ_ONLY = {
    Permissions.DASHBOARD_VIEW,
    Permissions.ORDERS_VIEW,
    Permissions.PAYMENTS_VIEW,
    Permissions.PRODUCTS_VIEW,
    Permissions.INVENTORY_VIEW,
    Permissions.USERS_VIEW,
    Permissions.COUPONS_VIEW,
    Permissions.RESELLERS_VIEW,
    Permissions.SUPPORT_VIEW,
    Permissions.ANALYTICS_VIEW,
    Permissions.REFERRALS_VIEW,
    Permissions.PROVIDERS_VIEW,
}

ROLE_PERMISSIONS: dict[RoleName, frozenset[Permissions]] = {
    RoleName.SUPER_ADMIN: frozenset(PERMISSIONS),
    RoleName.ADMIN: frozenset(
        set(PERMISSIONS)
        - {
            Permissions.ROLES_MANAGE,
            Permissions.PROVIDERS_CREDENTIALS,
            Permissions.BLOCKCHAIN_MANAGE,
        }
    ),
    RoleName.PAYMENT_MANAGER: frozenset(
        _READ_ONLY
        | {
            Permissions.PAYMENTS_RECHECK,
            Permissions.PAYMENTS_APPROVE,
            Permissions.PAYMENTS_REJECT,
            Permissions.REFUNDS_CREATE,
            Permissions.RECONCILIATION_RESOLVE,
            Permissions.ORDERS_CANCEL,
            Permissions.AUDIT_VIEW,
        }
    ),
    RoleName.PRODUCT_MANAGER: frozenset(
        _READ_ONLY
        | {
            Permissions.PRODUCTS_MANAGE,
            Permissions.PRODUCTS_ARCHIVE,
            Permissions.CATEGORIES_MANAGE,
            Permissions.INVENTORY_MANAGE,
            Permissions.INVENTORY_ADJUST,
            Permissions.COUPONS_MANAGE,
        }
    ),
    RoleName.SUPPORT_AGENT: frozenset(
        _READ_ONLY
        | {
            Permissions.SUPPORT_REPLY,
            Permissions.SUPPORT_ASSIGN,
            Permissions.PAYMENTS_RECHECK,
        }
    ),
    RoleName.ANALYST: frozenset(_READ_ONLY | {Permissions.AUDIT_VIEW}),
    RoleName.RESELLER_MANAGER: frozenset(
        _READ_ONLY | {Permissions.RESELLERS_MANAGE, Permissions.RESELLERS_KEYS}
    ),
}


def permissions_for(role_names: Iterable[RoleName | str]) -> frozenset[Permissions]:
    """Union of the permissions granted by the given roles."""
    granted: set[Permissions] = set()
    for name in role_names:
        role = RoleName(name) if not isinstance(name, RoleName) else name
        granted |= ROLE_PERMISSIONS.get(role, frozenset())
    return frozenset(granted)


def has_permission(
    role_names: Iterable[RoleName | str], permission: Permissions | str
) -> bool:
    try:
        needed = Permissions(permission)
    except ValueError:
        return False
    return needed in permissions_for(role_names)


def require_permission(
    role_names: Iterable[RoleName | str], permission: Permissions | str
) -> None:
    """Raise unless the roles grant ``permission``."""
    if not has_permission(role_names, permission):
        raise PermissionDeniedError(
            f"missing permission {permission}",
            context={"required": str(permission)},
        )


def is_high_risk(permission: Permissions | str) -> bool:
    try:
        return Permissions(permission) in HIGH_RISK_PERMISSIONS
    except ValueError:
        return False


def describe_role(role: RoleName) -> str:
    return {
        RoleName.SUPER_ADMIN: "Full access, including roles and provider credentials",
        RoleName.ADMIN: "Full operational access",
        RoleName.PAYMENT_MANAGER: "Payment review, approval, refunds and reconciliation",
        RoleName.PRODUCT_MANAGER: "Catalog, inventory and coupons",
        RoleName.SUPPORT_AGENT: "Support tickets and read-only order/payment access",
        RoleName.ANALYST: "Read-only analytics and audit access",
        RoleName.RESELLER_MANAGER: "Reseller accounts and API keys",
    }[role]
