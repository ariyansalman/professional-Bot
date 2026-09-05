"""Correlation id propagation.

A single correlation id ties together a Telegram action, the order it creates,
the payment intent, every verification attempt, the inventory allocation and
the final delivery. It is stored in a :class:`~contextvars.ContextVar` so it
survives across ``await`` boundaries without threading it through every call.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def new_correlation_id(prefix: str = "cid") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def set_correlation_id(value: str | None) -> Token:
    return _correlation_id.set(value)


def reset_correlation_id(token: Token) -> None:
    _correlation_id.reset(token)


@contextmanager
def correlation_scope(value: str | None = None, prefix: str = "cid") -> Iterator[str]:
    """Bind a correlation id for the duration of the block."""
    cid = value or new_correlation_id(prefix)
    token = _correlation_id.set(cid)
    try:
        yield cid
    finally:
        _correlation_id.reset(token)
