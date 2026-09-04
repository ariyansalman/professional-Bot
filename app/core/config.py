"""Environment-driven application configuration.

All configuration is loaded through pydantic-settings so that the application
never reads ``os.environ`` in an ad-hoc way. Secrets are typed as
:class:`~pydantic.SecretStr` so an accidental ``repr``/log never leaks them.
"""

from __future__ import annotations

import json
from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "staging", "production"]


_BASE_CONFIG: dict[str, Any] = {
    "env_file": ".env",
    "env_file_encoding": "utf-8",
    "extra": "ignore",
    "case_sensitive": False,
}


def _config(**overrides: Any) -> SettingsConfigDict:
    """Build a settings config that inherits the shared base options."""
    return SettingsConfigDict(**{**_BASE_CONFIG, **overrides})


class _Base(BaseSettings):
    model_config = _config()


class TelegramSettings(_Base):
    model_config = _config(env_prefix="TELEGRAM_")

    bot_token: SecretStr = Field(default=SecretStr(""))
    #: When set the bot runs in webhook mode, otherwise long polling is used.
    webhook_base_url: str | None = None
    webhook_path: str = "/telegram/webhook"
    webhook_secret: SecretStr = Field(default=SecretStr(""))
    #: Bootstrap super-admin Telegram user ids. Further admins are DB-managed.
    bootstrap_admin_ids: list[int] = Field(default_factory=list)
    support_username: str | None = None
    parse_mode: str = "HTML"
    #: Global outgoing messages/second budget (Telegram allows ~30/s globally).
    global_rate_limit: int = 25
    #: Per-chat messages/second budget (Telegram allows ~1/s per chat).
    chat_rate_limit: float = 1.0

    @field_validator("bootstrap_admin_ids", mode="before")
    @classmethod
    def _parse_ids(cls, value: Any) -> Any:
        return _parse_list(value, int)

    @property
    def webhook_url(self) -> str | None:
        if not self.webhook_base_url:
            return None
        return self.webhook_base_url.rstrip("/") + self.webhook_path


class DatabaseSettings(_Base):
    model_config = _config(env_prefix="DATABASE_")

    #: Async SQLAlchemy DSN, e.g. postgresql+asyncpg://user:pass@host:5432/db
    url: SecretStr = Field(default=SecretStr("postgresql+asyncpg://postgres:postgres@localhost:5432/commerce"))
    pool_size: int = 10
    max_overflow: int = 10
    pool_timeout: int = 30
    pool_recycle: int = 1800
    echo: bool = False
    #: Supabase pooler (pgbouncer, transaction mode) cannot use prepared statements.
    statement_cache_size: int = 0
    connect_timeout: int = 10

    @property
    def dsn(self) -> str:
        return self.url.get_secret_value()

    @property
    def is_sqlite(self) -> bool:
        return self.dsn.startswith("sqlite")


class RedisSettings(_Base):
    model_config = _config(env_prefix="REDIS_")

    url: SecretStr = Field(default=SecretStr("redis://localhost:6379/0"))
    namespace: str = "tgshop"
    socket_timeout: int = 5
    max_connections: int = 50

    @property
    def dsn(self) -> str:
        return self.url.get_secret_value()


class SecuritySettings(_Base):
    model_config = _config(env_prefix="SECURITY_")

    #: 32-byte urlsafe-base64 key used to encrypt provider credentials at rest.
    #: Generate with: python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
    secrets_encryption_key: SecretStr = Field(default=SecretStr(""))
    #: Previous keys, kept to allow transparent key rotation of stored secrets.
    secrets_previous_keys: list[str] = Field(default_factory=list)
    #: Pepper mixed into API key hashing.
    api_key_pepper: SecretStr = Field(default=SecretStr(""))
    #: Number of seconds an admin elevated-confirmation stays valid.
    admin_confirmation_ttl: int = 300

    @field_validator("secrets_previous_keys", mode="before")
    @classmethod
    def _parse_keys(cls, value: Any) -> Any:
        return _parse_list(value, str)


class APISettings(_Base):
    model_config = _config(env_prefix="API_")

    host: str = "0.0.0.0"  # noqa: S104 - container binding
    port: int = 8000
    root_path: str = ""
    docs_enabled: bool = True
    cors_origins: list[str] = Field(default_factory=list)
    #: Default per-minute request budget for reseller API keys.
    default_rate_limit_per_minute: int = 120
    #: How long idempotency keys are retained.
    idempotency_ttl_seconds: int = 86_400

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_origins(cls, value: Any) -> Any:
        return _parse_list(value, str)


class PaymentSettings(_Base):
    model_config = _config(env_prefix="PAYMENT_")

    #: Default lifetime of a payment intent in seconds.
    default_window_seconds: int = 1800
    #: Absolute tolerance (in quote currency) accepted as an exact payment.
    #: Kept at 0 by default: underpayment is never silently accepted.
    underpayment_tolerance: Decimal = Decimal("0")
    #: Payments above expected + this tolerance are flagged for review.
    overpayment_tolerance: Decimal = Decimal("0")
    #: How long after expiry a detected transaction is still reconciled (late payment).
    late_payment_grace_seconds: int = 86_400
    #: Poll cadence for the verification worker.
    verification_poll_interval: int = 20
    #: Maximum automatic verification attempts before manual review.
    max_verification_attempts: int = 120
    #: Inventory reservation lifetime; released automatically when it lapses.
    reservation_ttl_seconds: int = 2400
    #: Delivery retry policy.
    delivery_max_attempts: int = 8
    #: Uniqueness scope for generated public order references.
    order_reference_prefix: str = "TG"

    @field_validator("underpayment_tolerance", "overpayment_tolerance", mode="before")
    @classmethod
    def _to_decimal(cls, value: Any) -> Any:
        return Decimal(str(value)) if value is not None else value


class ObservabilitySettings(_Base):
    model_config = _config(env_prefix="LOG_")

    level: str = "INFO"
    json_output: bool = True
    #: Include the correlation id in every log record.
    include_correlation: bool = True


class FeatureFlags(_Base):
    model_config = _config(env_prefix="FEATURE_")

    referrals_enabled: bool = True
    coupons_enabled: bool = True
    reseller_enabled: bool = True
    reseller_self_activation: bool = True
    reviews_enabled: bool = False
    restock_notifications_enabled: bool = True
    support_enabled: bool = True
    broadcast_enabled: bool = True


class Settings(_Base):
    """Root settings object."""

    environment: Environment = "local"
    app_name: str = "Telegram Commerce"
    debug: bool = False
    #: Service role of the current process. Set by the entrypoint.
    service: Literal["bot", "api", "worker", "all"] = "all"

    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    api: APISettings = Field(default_factory=APISettings)
    payments: PaymentSettings = Field(default_factory=PaymentSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    features: FeatureFlags = Field(default_factory=FeatureFlags)

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        if self.environment == "production":
            missing: list[str] = []
            if not self.telegram.bot_token.get_secret_value():
                missing.append("TELEGRAM_BOT_TOKEN")
            if not self.security.secrets_encryption_key.get_secret_value():
                missing.append("SECURITY_SECRETS_ENCRYPTION_KEY")
            if not self.security.api_key_pepper.get_secret_value():
                missing.append("SECURITY_API_KEY_PEPPER")
            if missing:
                raise ValueError(
                    "Missing required production configuration: " + ", ".join(missing)
                )
            if self.debug:
                raise ValueError("DEBUG must be disabled in production")
        return self

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


def _parse_list(value: Any, cast: type) -> Any:
    """Accept JSON arrays and comma-separated strings for list settings."""
    if value is None or isinstance(value, list):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        if raw.startswith("["):
            return [cast(item) for item in json.loads(raw)]
        return [cast(item.strip()) for item in raw.split(",") if item.strip()]
    return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()


SettingsDep = Annotated[Settings, Field()]
