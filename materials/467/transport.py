"""Transports with controllable and real completion timing.

``ControlledTransport`` keeps callbacks pending until :meth:`finish` is
called, which lets tests choose an exact completion order.  ``ThreadTransport``
runs a real callable per request on its own thread; completions therefore
arrive asynchronously and may race with cancellation.

Both transports invoke each callback at most once.  The callback is always
called *outside* the transport lock so a callback may immediately submit or
tick the dispatcher (callback re-entrancy).
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional


class ControlledTransport:
    def __init__(self) -> None:
        self.pending: Dict[str, Callable[[Any], None]] = {}
        self.started: List[str] = []
        self.cancelled: List[str] = []
        self.lock = threading.RLock()

    def start(self, request: Any, callback: Callable[[Any], None]) -> None:
        with self.lock:
            self.started.append(request.id)
            self.pending[request.id] = callback

    def finish(self, request_id: str, response: Any = None) -> bool:
        """Deliver a response exactly once; return whether it was delivered."""
        with self.lock:
            callback = self.pending.pop(request_id, None)
        if callback is None:
            return False
        callback(response if response is not None else {})
        return True

    def cancel(self, request_id: str) -> bool:
        with self.lock:
            if request_id not in self.pending:
                return False
            self.cancelled.append(request_id)
        return self.finish(request_id, {"cancelled": True})

    def is_pending(self, request_id: str) -> bool:
        with self.lock:
            return request_id in self.pending

    def pending_ids(self) -> List[str]:
        with self.lock:
            return list(self.pending)


class ThreadTransport(ControlledTransport):
    """Runs ``fn(request)`` on a daemon thread and reports its result."""

    def __init__(self, fn: Callable[[Any], Any]) -> None:
        super().__init__()
        self.fn = fn
        self.threads: Dict[str, threading.Thread] = {}
        self._closed = False

    def start(self, request: Any, callback: Callable[[Any], None]) -> None:
        with self.lock:
            if self._closed:
                raise RuntimeError("transport is closed")
            super().start(request, callback)

        def work() -> None:
            try:
                result = self.fn(request)
            except BaseException as exc:  # noqa: BLE001 - reported as outcome
                result = {"error": f"{type(exc).__name__}: {exc}"}
            self.finish(request.id, result)

        thread = threading.Thread(
            target=work, name=f"transport-{request.id}", daemon=True
        )
        with self.lock:
            self.threads[request.id] = thread
        thread.start()

    def finish(self, request_id: str, response: Any = None) -> bool:
        delivered = super().finish(request_id, response)
        with self.lock:
            self.threads.pop(request_id, None)
        return delivered

    def join(self, timeout: Optional[float] = None) -> None:
        with self.lock:
            threads = list(self.threads.values())
        for thread in threads:
            thread.join(timeout)

    def wait_idle(self, timeout: Optional[float] = None) -> bool:
        """Wait until every started thread has delivered or timed out."""
        with self.lock:
            threads = list(self.threads.values())
        for thread in threads:
            thread.join(timeout)
            if thread.is_alive():
                return False
        return True

    def close(self) -> None:
        self._closed = True
