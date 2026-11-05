from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Event
from typing import Callable, Iterator


TaskEventHandler = Callable[[dict], None]


class TaskCancelled(RuntimeError):
    """Raised at a cooperative cancellation checkpoint."""


@dataclass
class CancellationToken:
    _event: Event = field(default_factory=Event)

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise TaskCancelled("任务已取消。")

    def wait(self, timeout: float | None = None) -> bool:
        """Block until cancelled or ``timeout`` elapses; return True when cancelled."""
        return self._event.wait(timeout)


_CURRENT_TOKEN: ContextVar[CancellationToken | None] = ContextVar(
    "srtmatcher_current_cancel_token", default=None
)
_CURRENT_EVENT_HANDLER: ContextVar[TaskEventHandler | None] = ContextVar(
    "srtmatcher_current_event_handler", default=None
)


@contextmanager
def task_context(
    token: CancellationToken | None,
    event_handler: TaskEventHandler | None = None,
) -> Iterator[None]:
    token_marker = _CURRENT_TOKEN.set(token)
    handler_marker = _CURRENT_EVENT_HANDLER.set(event_handler)
    try:
        yield
    finally:
        _CURRENT_EVENT_HANDLER.reset(handler_marker)
        _CURRENT_TOKEN.reset(token_marker)


def current_cancellation_token() -> CancellationToken | None:
    return _CURRENT_TOKEN.get()


def check_cancelled() -> None:
    token = current_cancellation_token()
    if token is not None:
        token.raise_if_cancelled()


def sleep_cancellable(seconds: float, stop_event: Event | None = None) -> bool:
    """Sleep for ``seconds`` but wake immediately when the task is cancelled.

    Raises :class:`TaskCancelled` on cancellation.  Returns True when the optional
    ``stop_event`` was set during the wait (caller decides how to stop), else False.
    Without a cancellation token or stop event this is a plain ``time.sleep``.
    """
    token = current_cancellation_token()
    seconds = max(0.0, float(seconds or 0.0))
    if token is None and stop_event is None:
        time.sleep(seconds)
        return False
    deadline = time.monotonic() + seconds
    while True:
        if token is not None:
            token.raise_if_cancelled()
        if stop_event is not None and stop_event.is_set():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if stop_event is None:
            token.wait(remaining)
        elif token is None:
            stop_event.wait(remaining)
        else:
            # Two events cannot be awaited together; poll briefly so both wake fast.
            token.wait(min(remaining, 0.05))


def emit_task_event(event_type: str, **payload) -> None:
    handler = _CURRENT_EVENT_HANDLER.get()
    if handler is None:
        return
    event = {"type": event_type, **payload}
    try:
        handler(event)
    except Exception:
        # UI progress reporting must never be able to fail a media job.
        pass
