# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Generic background-worker thread supervision (Stage F).

The finalization worker (Stage B+D) established the deployment shape for
CivicCast background services: env-selected mode, a synchronous
``run_forever(poll_seconds, stop_event)`` loop that survives scan exceptions,
started by the app lifespan only when durable storage is active, stopped via
the event on shutdown. :class:`ThreadSupervisor` is that shape extracted so
new workers (ActivityPub retry, retention review) don't re-implement it.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

_LOG = logging.getLogger(__name__)

__all__ = ["ThreadSupervisor"]


class ThreadSupervisor:
    """Owns one daemon worker thread; idempotent start, event-driven stop.

    Args:
        name: thread name (shown in logs and thread dumps).
        run_forever: blocking loop accepting ``poll_seconds`` and
            ``stop_event`` keyword arguments (the house worker-loop shape).
        poll_seconds: poll interval handed to the loop.
        enabled: when False, ``start()`` is a no-op (mode ``off``).
    """

    def __init__(
        self,
        *,
        name: str,
        run_forever: Callable[..., None],
        poll_seconds: float,
        enabled: bool,
    ) -> None:
        self._name = name
        self._run_forever = run_forever
        self._poll_seconds = poll_seconds
        self._enabled = enabled
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        if not self._enabled:
            return
        with self._lock:
            if self.running:
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_forever,
                kwargs={"poll_seconds": self._poll_seconds, "stop_event": self._stop_event},
                name=self._name,
                daemon=True,
            )
            self._thread.start()
            _LOG.info("%s started (poll=%ss).", self._name, self._poll_seconds)

    def stop(self, timeout: float = 10.0) -> None:
        with self._lock:
            thread = self._thread
            if thread is None:
                return
            self._stop_event.set()
            thread.join(timeout=timeout)
            if thread.is_alive():  # pragma: no cover - defensive timeout path
                _LOG.warning("%s did not stop within %ss.", self._name, timeout)
            else:
                _LOG.info("%s stopped.", self._name)
            self._thread = None
