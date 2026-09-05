"""Security primitives: secret encryption, hashing, API keys, signatures.

* Provider credentials are encrypted at rest with Fernet (AES-128-CBC +
  HMAC-SHA256). Key rotation is supported by keeping previous keys for
  decryption only.
* API keys are never stored: only a peppered SHA-256 digest is persisted, and
  the plaintext is shown to the reseller exactly once.
* All comparisons of secret material use ``hmac.compare_digest``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import re
import secrets
from dataclasses import dataclass
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.config import get_settings
from app.core.exceptions import ConfigurationError, ValidationError

API_KEY_PREFIX_LIVE = "rt_live"
API_KEY_PREFIX_TEST = "rt_test"
_API_KEY_RE = re.compile(r"^(rt_(?:live|test))_([A-Za-z0-9]{8})_([A-Za-z0-9_-]{32,})$")


# --------------------------------------------------------------------------
# Secret storage
# --------------------------------------------------------------------------


class SecretBox:
    """Encrypts/decrypts provider credentials for database storage."""

    def __init__(self, primary_key: str, previous_keys: list[str] | None = None) -> None:
        if not primary_key:
            raise ConfigurationError(
                "SECURITY_SECRETS_ENCRYPTION_KEY is not configured; "
                "provider credentials cannot be stored safely."
            )
        keys = [primary_key, *(previous_keys or [])]
        try:
            self._fernet = MultiFernet([Fernet(key.encode()) for key in keys])
        except (ValueError, TypeError) as exc:
            raise ConfigurationError("Invalid Fernet key configured for secret storage") from exc

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise ConfigurationError(
                "Stored credential could not be decrypted; the encryption key may have changed."
            ) from exc

    def rotate(self, ciphertext: str) -> str:
        """Re-encrypt an existing token with the current primary key."""
        return self._fernet.rotate(ciphertext.encode()).decode()


_secret_box: SecretBox | None = None


def get_secret_box() -> SecretBox:
    global _secret_box
    if _secret_box is None:
        settings = get_settings()
        _secret_box = SecretBox(
            settings.security.secrets_encryption_key.get_secret_value(),
            settings.security.secrets_previous_keys,
        )
    return _secret_box


def generate_encryption_key() -> str:
    """Helper used by the setup CLI to mint a new Fernet key."""
    return Fernet.generate_key().decode()


# --------------------------------------------------------------------------
# API keys
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GeneratedApiKey:
    """A freshly minted API key. ``plaintext`` is shown once and discarded."""

    plaintext: str
    prefix: str
    public_id: str
    hashed: str


def generate_api_key(live: bool = True) -> GeneratedApiKey:
    prefix = API_KEY_PREFIX_LIVE if live else API_KEY_PREFIX_TEST
    public_id = secrets.token_hex(4)
    body = secrets.token_urlsafe(36).replace("=", "")
    plaintext = f"{prefix}_{public_id}_{body}"
    return GeneratedApiKey(
        plaintext=plaintext,
        prefix=prefix,
        public_id=public_id,
        hashed=hash_api_key(plaintext),
    )


def hash_api_key(plaintext: str) -> str:
    """Peppered SHA-256 digest. Keys are high-entropy so a KDF is unnecessary."""
    pepper = get_settings().security.api_key_pepper.get_secret_value().encode()
    return hmac.new(pepper, plaintext.encode(), hashlib.sha256).hexdigest()


def parse_api_key(plaintext: str) -> tuple[str, str]:
    """Return ``(prefix, public_id)`` for a syntactically valid key."""
    match = _API_KEY_RE.match(plaintext.strip())
    if not match:
        raise ValidationError("Malformed API key", safe_message="Invalid API key.")
    return match.group(1), match.group(2)


def verify_api_key(plaintext: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_api_key(plaintext), stored_hash)


# --------------------------------------------------------------------------
# Webhook signing
# --------------------------------------------------------------------------


def generate_webhook_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32).replace("=", "")


def sign_webhook(secret: str, timestamp: int, event_id: str, body: bytes) -> str:
    """Signature scheme documented for resellers.

    ``v1=hex(hmac_sha256(secret, "<timestamp>.<event_id>.<raw_body>"))``
    """
    payload = b"%d.%s." % (timestamp, event_id.encode())
    digest = hmac.new(secret.encode(), payload + body, hashlib.sha256).hexdigest()
    return f"v1={digest}"


def verify_webhook_signature(
    secret: str, timestamp: int, event_id: str, body: bytes, signature: str
) -> bool:
    return hmac.compare_digest(sign_webhook(secret, timestamp, event_id, body), signature)


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


# --------------------------------------------------------------------------
# Signing helpers used by exchange adapters
# --------------------------------------------------------------------------


def hmac_sha256_hex(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def hmac_sha512_hex_upper(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha512).hexdigest().upper()


def hmac_sha256_b64(secret: str, payload: str) -> str:
    return base64.b64encode(
        hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    ).decode()


# --------------------------------------------------------------------------
# SSRF protection for outbound webhooks
# --------------------------------------------------------------------------

_BLOCKED_HOSTNAMES = frozenset({"localhost", "metadata.google.internal", "instance-data"})


def assert_safe_outbound_url(url: str, *, allow_http: bool = False) -> str:
    """Validate a reseller-supplied webhook URL before we ever call it.

    Blocks non-HTTP(S) schemes, credentials in the URL, and hostnames that
    resolve to loopback/private/link-local ranges (cloud metadata endpoints in
    particular). DNS is re-checked at delivery time by the HTTP client.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ({"https", "http"} if allow_http else {"https"}):
        raise ValidationError(
            f"Blocked webhook scheme: {parsed.scheme}",
            safe_message="Webhook URL must use HTTPS.",
        )
    if parsed.username or parsed.password:
        raise ValidationError(
            "Credentials in webhook URL",
            safe_message="Webhook URL must not contain credentials.",
        )
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValidationError("Missing host", safe_message="Webhook URL is invalid.")
    if host in _BLOCKED_HOSTNAMES or host.endswith(".local") or host.endswith(".internal"):
        raise ValidationError(
            f"Blocked webhook host: {host}", safe_message="Webhook URL host is not allowed."
        )
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return url  # hostname; runtime resolution is re-validated by the client
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise ValidationError(
            f"Blocked webhook IP: {ip}", safe_message="Webhook URL host is not allowed."
        )
    return url


def mask_secret(value: str | None, *, keep: int = 4) -> str:
    """Render a credential for admin display without revealing it."""
    if not value:
        return "not set"
    if len(value) <= keep:
        return "*" * len(value)
    return f"{'*' * 8}{value[-keep:]}"


def mask_address(value: str | None, head: int = 6, tail: int = 6) -> str:
    if not value:
        return "-"
    if len(value) <= head + tail + 3:
        return value
    return f"{value[:head]}...{value[-tail:]}"
