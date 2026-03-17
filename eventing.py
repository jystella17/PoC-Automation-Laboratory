from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Callable

EventCallback = Callable[[dict[str, object]], None]
_EVENT_CALLBACK: ContextVar[EventCallback | None] = ContextVar("global_event_callback", default=None)


def set_event_callback(callback: EventCallback | None):
    return _EVENT_CALLBACK.set(callback)


def reset_event_callback(token) -> None:
    _EVENT_CALLBACK.reset(token)


def emit_event(
    *,
    owner: str,
    phase: str,
    status: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    callback = _EVENT_CALLBACK.get()
    if callback is None:
        return
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "owner": owner,
        "phase": phase,
        "status": status,
        "message": message,
        "details": details or {},
    }
    try:
        callback(payload)
    except Exception:
        return
