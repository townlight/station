# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""GI-free admission control for live-caption heartbeat GAP events.

Callers create a GStreamer GAP event and record its automatically assigned
sequence number with :meth:`CaptionGapGate.reserve` before ``appsrc.send_event``.
The queue-source downstream-event probe calls :meth:`entered_downstream` for that
same number.  If sending fails, the caller calls :meth:`cancel`; this only clears
its own reservation, so a probe that ran before a failed send returned cannot
release a newer GAP.

The gate limits heartbeat GAP events only.  Caption cue buffers deliberately use
their existing appsrc flow path and are not counted here.
"""

from __future__ import annotations

from threading import Lock


class CaptionGapGate:
    """Permit at most one queued heartbeat GAP while the caption queue is live.

    This class owns no sequence-number generator.  The caller supplies the
    sequence number GStreamer assigned to the particular GAP event.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._pending: int | None = None
        self._closed = False

    @property
    def pending(self) -> int | None:
        """Return the reserved heartbeat GAP sequence number, if any."""
        with self._lock:
            return self._pending

    def reserve(self, seqnum: int) -> bool:
        """Reserve ``seqnum`` before sending its heartbeat GAP to appsrc."""
        with self._lock:
            if self._closed or self._pending is not None:
                return False
            self._pending = seqnum
            return True

    def entered_downstream(self, seqnum: int) -> bool:
        """Release ``seqnum`` once the queue source starts forwarding that GAP."""
        return self._release_if_current(seqnum)

    def cancel(self, seqnum: int) -> bool:
        """Release ``seqnum`` after its caller's appsrc send failure."""
        return self._release_if_current(seqnum)

    def close(self) -> None:
        """Close the gate permanently during caption-pipeline teardown."""
        with self._lock:
            self._closed = True
            self._pending = None

    def _release_if_current(self, seqnum: int) -> bool:
        with self._lock:
            if self._closed or self._pending != seqnum:
                return False
            self._pending = None
            return True
