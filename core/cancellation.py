from __future__ import annotations

from typing import Protocol, runtime_checkable


class CancellationRequested(Exception):
    pass


@runtime_checkable
class CancellationToken(Protocol):
    def is_cancelled(self) -> bool:
        ...


def cancellation_requested(token: CancellationToken | object | None) -> bool:
    if token is None:
        return False
    checker = getattr(token, "is_cancelled", None)
    try:
        if callable(checker):
            return bool(checker())
        return bool(getattr(token, "cancelled", False))
    except Exception:
        return True


def raise_if_cancelled(token: CancellationToken | object | None) -> None:
    if cancellation_requested(token):
        raise CancellationRequested("Cancellation requested.")
