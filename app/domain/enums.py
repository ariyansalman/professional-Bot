"""Domain enumerations shared by the database, services and UI layers."""

from __future__ import annotations

from enum import StrEnum


class Language(StrEnum):
    EN = "en"
    BN = "bn"


class UserStatus(StrEnum):
    ACTIVE = "active"
    RESTRICTED = "restricted"
    BANNED = "banned"


class RoleName(StrEnum):
    """Built-in RBAC roles. Permissions are granular and role-attached."""

    SUPER_ADMIN = "super_admin"
    ADMIN = "admin"
    PAYMENT_MANAGER = "payment_manager"
    PRODUCT_MANAGER = "product_manager"
    SUPPORT_AGENT = "support_agent"
    ANALYST = "analyst"
    RESELLER_MANAGER = "reseller_manager"


class ProductStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    INACTIVE = "inactive"
    HIDDEN = "hidden"
    ARCHIVED = "archived"

    @property
    def is_purchasable(self) -> bool:
        return self is ProductStatus.ACTIVE

    @property
    def is_listed(self) -> bool:
        return self is ProductStatus.ACTIVE


class DeliveryType(StrEnum):
    """How a purchased item is fulfilled."""

    #: Pre-loaded stock items (license keys, codes, accounts, credentials).
    STOCK_ITEM = "stock_item"
    #: The same static payload for every buyer (download link, instructions).
    STATIC_PAYLOAD = "static_payload"
    #: A file stored on Telegram, re-sent by file_id.
    FILE = "file"
    #: Fulfilled by a human operator from the admin panel.
    MANUAL = "manual"


class StockItemStatus(StrEnum):
    AVAILABLE = "available"
    RESERVED = "reserved"
    SOLD = "sold"
    INVALID = "invalid"


class ReservationStatus(StrEnum):
    ACTIVE = "active"
    CONSUMED = "consumed"
    RELEASED = "released"
    EXPIRED = "expired"


class OrderStatus(StrEnum):
    CREATED = "created"
    PAYMENT_PENDING = "payment_pending"
    PAYMENT_VERIFIED = "payment_verified"
    FULFILLING = "fulfilling"
    DELIVERED = "delivered"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    MANUAL_REVIEW = "manual_review"
    DELIVERY_FAILED = "delivery_failed"
    REFUNDED = "refunded"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_ORDER_STATES

    @property
    def is_paid(self) -> bool:
        return self in {
            OrderStatus.PAYMENT_VERIFIED,
            OrderStatus.FULFILLING,
            OrderStatus.DELIVERED,
            OrderStatus.COMPLETED,
            OrderStatus.DELIVERY_FAILED,
            OrderStatus.REFUNDED,
        }


_TERMINAL_ORDER_STATES = frozenset(
    {
        OrderStatus.COMPLETED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
        OrderStatus.REFUNDED,
    }
)

#: Explicit order transition table. Nothing else is permitted - status is never
#: assigned directly, only through :func:`assert_order_transition`.
ORDER_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset(
        {OrderStatus.PAYMENT_PENDING, OrderStatus.CANCELLED, OrderStatus.EXPIRED}
    ),
    OrderStatus.PAYMENT_PENDING: frozenset(
        {
            OrderStatus.PAYMENT_VERIFIED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
            OrderStatus.MANUAL_REVIEW,
            OrderStatus.PAYMENT_PENDING,  # new payment intent for the same order
        }
    ),
    OrderStatus.PAYMENT_VERIFIED: frozenset(
        {OrderStatus.FULFILLING, OrderStatus.MANUAL_REVIEW, OrderStatus.REFUNDED}
    ),
    OrderStatus.FULFILLING: frozenset(
        {OrderStatus.DELIVERED, OrderStatus.DELIVERY_FAILED, OrderStatus.MANUAL_REVIEW}
    ),
    OrderStatus.DELIVERED: frozenset({OrderStatus.COMPLETED, OrderStatus.REFUNDED}),
    OrderStatus.DELIVERY_FAILED: frozenset(
        {OrderStatus.FULFILLING, OrderStatus.MANUAL_REVIEW, OrderStatus.REFUNDED}
    ),
    OrderStatus.MANUAL_REVIEW: frozenset(
        {
            OrderStatus.PAYMENT_VERIFIED,
            OrderStatus.FULFILLING,
            OrderStatus.CANCELLED,
            OrderStatus.REFUNDED,
            OrderStatus.PAYMENT_PENDING,
        }
    ),
    OrderStatus.EXPIRED: frozenset({OrderStatus.PAYMENT_PENDING, OrderStatus.MANUAL_REVIEW}),
    OrderStatus.COMPLETED: frozenset({OrderStatus.REFUNDED}),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REFUNDED: frozenset(),
}


class PaymentStatus(StrEnum):
    """Payment intent lifecycle (section 83 of the specification)."""

    CREATED = "created"
    AWAITING_PAYMENT = "awaiting_payment"
    SUBMITTED = "submitted"
    DETECTING = "detecting"
    DETECTED = "detected"
    VERIFYING = "verifying"
    PENDING_CONFIRMATION = "pending_confirmation"
    VERIFIED = "verified"
    UNDER_REVIEW = "under_review"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            PaymentStatus.VERIFIED,
            PaymentStatus.FAILED,
            PaymentStatus.EXPIRED,
            PaymentStatus.CANCELLED,
        }

    @property
    def is_open(self) -> bool:
        """True while the customer can still complete this payment."""
        return self in {
            PaymentStatus.CREATED,
            PaymentStatus.AWAITING_PAYMENT,
            PaymentStatus.SUBMITTED,
            PaymentStatus.DETECTING,
            PaymentStatus.DETECTED,
            PaymentStatus.VERIFYING,
            PaymentStatus.PENDING_CONFIRMATION,
        }


PAYMENT_TRANSITIONS: dict[PaymentStatus, frozenset[PaymentStatus]] = {
    PaymentStatus.CREATED: frozenset(
        {PaymentStatus.AWAITING_PAYMENT, PaymentStatus.CANCELLED, PaymentStatus.EXPIRED}
    ),
    PaymentStatus.AWAITING_PAYMENT: frozenset(
        {
            PaymentStatus.SUBMITTED,
            PaymentStatus.DETECTING,
            PaymentStatus.DETECTED,
            PaymentStatus.EXPIRED,
            PaymentStatus.CANCELLED,
            PaymentStatus.UNDER_REVIEW,
        }
    ),
    PaymentStatus.SUBMITTED: frozenset(
        {
            PaymentStatus.DETECTING,
            PaymentStatus.DETECTED,
            PaymentStatus.VERIFYING,
            PaymentStatus.FAILED,
            PaymentStatus.EXPIRED,
            PaymentStatus.UNDER_REVIEW,
            PaymentStatus.CANCELLED,
        }
    ),
    PaymentStatus.DETECTING: frozenset(
        {
            PaymentStatus.DETECTED,
            PaymentStatus.VERIFYING,
            PaymentStatus.FAILED,
            PaymentStatus.EXPIRED,
            PaymentStatus.UNDER_REVIEW,
            PaymentStatus.CANCELLED,
        }
    ),
    PaymentStatus.DETECTED: frozenset(
        {
            PaymentStatus.VERIFYING,
            PaymentStatus.PENDING_CONFIRMATION,
            PaymentStatus.VERIFIED,
            PaymentStatus.FAILED,
            PaymentStatus.UNDER_REVIEW,
            PaymentStatus.EXPIRED,
        }
    ),
    PaymentStatus.VERIFYING: frozenset(
        {
            PaymentStatus.PENDING_CONFIRMATION,
            PaymentStatus.VERIFIED,
            PaymentStatus.FAILED,
            PaymentStatus.UNDER_REVIEW,
            PaymentStatus.DETECTED,
            PaymentStatus.EXPIRED,
        }
    ),
    PaymentStatus.PENDING_CONFIRMATION: frozenset(
        {
            PaymentStatus.VERIFIED,
            PaymentStatus.VERIFYING,
            PaymentStatus.FAILED,
            PaymentStatus.UNDER_REVIEW,
            PaymentStatus.EXPIRED,
        }
    ),
    # A verified payment is final. Money moves on from here through refunds,
    # never by rewinding the payment state.
    PaymentStatus.VERIFIED: frozenset(),
    PaymentStatus.UNDER_REVIEW: frozenset(
        {PaymentStatus.VERIFIED, PaymentStatus.FAILED, PaymentStatus.VERIFYING, PaymentStatus.CANCELLED}
    ),
    PaymentStatus.FAILED: frozenset({PaymentStatus.UNDER_REVIEW, PaymentStatus.VERIFYING}),
    PaymentStatus.EXPIRED: frozenset({PaymentStatus.UNDER_REVIEW, PaymentStatus.VERIFYING}),
    PaymentStatus.CANCELLED: frozenset(),
}


class VerificationOutcome(StrEnum):
    """Result of a single verification attempt against a provider."""

    VERIFIED = "verified"
    NOT_FOUND = "not_found"
    PENDING_CONFIRMATION = "pending_confirmation"
    UNDERPAID = "underpaid"
    OVERPAID = "overpaid"
    WRONG_NETWORK = "wrong_network"
    WRONG_ASSET = "wrong_asset"
    WRONG_RECEIVER = "wrong_receiver"
    DUPLICATE = "duplicate"
    FAILED_TRANSACTION = "failed_transaction"
    OUTSIDE_WINDOW = "outside_window"
    PROVIDER_ERROR = "provider_error"
    UNSUPPORTED = "unsupported"
    MEMO_MISMATCH = "memo_mismatch"

    @property
    def is_success(self) -> bool:
        return self is VerificationOutcome.VERIFIED

    @property
    def is_retryable(self) -> bool:
        """Whether polling again could still succeed."""
        return self in {
            VerificationOutcome.NOT_FOUND,
            VerificationOutcome.PENDING_CONFIRMATION,
            VerificationOutcome.PROVIDER_ERROR,
        }

    @property
    def needs_review(self) -> bool:
        """Money likely arrived but cannot be auto-credited."""
        return self in {
            VerificationOutcome.UNDERPAID,
            VerificationOutcome.OVERPAID,
            VerificationOutcome.WRONG_NETWORK,
            VerificationOutcome.WRONG_ASSET,
            VerificationOutcome.WRONG_RECEIVER,
            VerificationOutcome.DUPLICATE,
            VerificationOutcome.OUTSIDE_WINDOW,
            VerificationOutcome.MEMO_MISMATCH,
            VerificationOutcome.UNSUPPORTED,
        }


class PaymentProviderKind(StrEnum):
    EXCHANGE = "exchange"
    BLOCKCHAIN = "blockchain"


class ProviderCode(StrEnum):
    BINANCE = "binance"
    BINANCE_PAY = "binance_pay"
    BYBIT = "bybit"
    OKX = "okx"
    TRON = "tron"
    EVM = "evm"
    TON = "ton"
    SOLANA = "solana"
    UTXO = "utxo"


class NetworkCode(StrEnum):
    """Chain identifiers used for method configuration and validation."""

    TRC20 = "trc20"
    BEP20 = "bep20"
    ERC20 = "erc20"
    TON = "ton"
    SOL = "sol"
    AVAXC = "avaxc"
    ARBITRUM = "arbitrum"
    POLYGON = "polygon"
    BTC = "btc"
    LTC = "ltc"
    #: Off-chain movement inside an exchange (Binance Pay / internal transfer).
    EXCHANGE_INTERNAL = "exchange_internal"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RefundStatus(StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    PROCESSING = "processing"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"


class CouponType(StrEnum):
    PERCENTAGE = "percentage"
    FIXED = "fixed"


class ReferralStatus(StrEnum):
    PENDING = "pending"
    QUALIFIED = "qualified"
    REWARDED = "rewarded"
    REJECTED = "rejected"


class ResellerStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"


class ApiScope(StrEnum):
    PRODUCTS_READ = "products.read"
    ORDERS_CREATE = "orders.create"
    ORDERS_READ = "orders.read"
    PAYMENTS_READ = "payments.read"
    DELIVERIES_READ = "deliveries.read"
    WEBHOOKS_MANAGE = "webhooks.manage"


class WebhookEvent(StrEnum):
    ORDER_CREATED = "order.created"
    PAYMENT_PENDING = "payment.pending"
    PAYMENT_DETECTED = "payment.detected"
    PAYMENT_VERIFIED = "payment.verified"
    PAYMENT_FAILED = "payment.failed"
    DELIVERY_PROCESSING = "delivery.processing"
    DELIVERY_COMPLETED = "delivery.completed"
    ORDER_COMPLETED = "order.completed"
    ORDER_CANCELLED = "order.cancelled"


class WebhookDeliveryStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    EXHAUSTED = "exhausted"


class TicketStatus(StrEnum):
    OPEN = "open"
    ASSIGNED = "assigned"
    WAITING_USER = "waiting_user"
    RESOLVED = "resolved"
    CLOSED = "closed"


class TicketCategory(StrEnum):
    PAYMENT = "payment"
    ORDER = "order"
    PRODUCT = "product"
    TECHNICAL = "technical"
    OTHER = "other"


class TicketPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class NotificationKind(StrEnum):
    PAYMENT = "payment"
    ORDER = "order"
    DELIVERY = "delivery"
    SUPPORT = "support"
    REFERRAL = "referral"
    SYSTEM = "system"
    RESTOCK = "restock"


class LedgerEntryType(StrEnum):
    """Append-only financial journal entry types."""

    ORDER_CREATED = "order_created"
    PAYMENT_VERIFIED = "payment_verified"
    PAYMENT_OVERPAID = "payment_overpaid"
    PAYMENT_UNDERPAID = "payment_underpaid"
    REFUND = "refund"
    MANUAL_ADJUSTMENT = "manual_adjustment"
    REFERRAL_REWARD = "referral_reward"


class AuditAction(StrEnum):
    LOGIN = "admin.login"
    PAYMENT_APPROVED = "payment.approved"
    PAYMENT_REJECTED = "payment.rejected"
    PAYMENT_RECHECKED = "payment.rechecked"
    ORDER_CANCELLED = "order.cancelled"
    ORDER_FORCED_DELIVERY = "order.forced_delivery"
    REFUND_CREATED = "refund.created"
    REFUND_COMPLETED = "refund.completed"
    PRODUCT_CREATED = "product.created"
    PRODUCT_UPDATED = "product.updated"
    PRODUCT_ARCHIVED = "product.archived"
    INVENTORY_ADDED = "inventory.added"
    INVENTORY_ADJUSTED = "inventory.adjusted"
    COUPON_CREATED = "coupon.created"
    COUPON_UPDATED = "coupon.updated"
    USER_RESTRICTED = "user.restricted"
    USER_BANNED = "user.banned"
    RESELLER_APPROVED = "reseller.approved"
    RESELLER_SUSPENDED = "reseller.suspended"
    API_KEY_CREATED = "api_key.created"
    API_KEY_REVOKED = "api_key.revoked"
    PROVIDER_CREDENTIALS_UPDATED = "provider.credentials_updated"
    PROVIDER_TOGGLED = "provider.toggled"
    PAYMENT_METHOD_UPDATED = "payment_method.updated"
    PAYMENT_ADDRESS_CHANGED = "payment_method.address_changed"
    TOKEN_CONTRACT_CHANGED = "payment_method.contract_changed"  # noqa: S105 - action name, not a credential
    SETTINGS_UPDATED = "settings.updated"
    MAINTENANCE_TOGGLED = "maintenance.toggled"
    BROADCAST_SENT = "broadcast.sent"
    ROLE_ASSIGNED = "role.assigned"
    ROLE_REVOKED = "role.revoked"


class BroadcastStatus(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    SENDING = "sending"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class BroadcastAudience(StrEnum):
    ALL = "all"
    ACTIVE = "active"
    CUSTOMERS = "customers"
    RESELLERS = "resellers"
    LANGUAGE = "language"


class ReconciliationKind(StrEnum):
    UNMATCHED_TRANSACTION = "unmatched_transaction"
    DUPLICATE_TRANSACTION = "duplicate_transaction"
    LATE_PAYMENT = "late_payment"
    WRONG_NETWORK = "wrong_network"
    AMOUNT_MISMATCH = "amount_mismatch"
    EXPIRED_WITH_FUNDS = "expired_with_funds"
    PROVIDER_INCONSISTENCY = "provider_inconsistency"
    STUCK_DELIVERY = "stuck_delivery"
    ORPHAN_PAYMENT = "orphan_payment"


class ReconciliationStatus(StrEnum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    RESOLVED = "resolved"
    IGNORED = "ignored"
