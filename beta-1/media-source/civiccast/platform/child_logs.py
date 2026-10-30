# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Bounded best-effort logs for long-running child processes."""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
from typing import Any

# Shared with the Windows supervisor's durable child logs. Keep this one policy
# for every continuously running CivicCast child process.
CHILD_LOG_MAX_BYTES = 10 * 1024 * 1024
CHILD_LOG_BACKUP_COUNT = 10
CHILD_LOG_READ_SIZE = 64 * 1024


class BestEffortRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A bounded child-output sink whose failures never stop pipe drainage."""

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        # Losing diagnostic output is preferable to blocking a child on a full
        # pipe when the disk is full or a rotation rename is temporarily denied.
        return


def drain_child_output(
    stream: Any,
    handler: logging.Handler,
    *,
    read_size: int = CHILD_LOG_READ_SIZE,
    logger_name: str = "civiccast.child_output",
) -> None:
    """Drain a child's binary output into a bounded handler on its owner thread.

    Reads are fixed-size and synchronous: buffered streams use ``read1`` so
    short output is visible before the child exits; raw streams and test doubles
    fall back to ``read``. Memory stays bounded, and handler failures are
    discarded after each read so the child pipe cannot fill and stall the
    worker. The drainer owns both the stream and handler lifetimes.
    """

    try:
        read_chunk = stream.read1
    except AttributeError:
        read_chunk = stream.read
    try:
        while True:
            try:
                chunk = read_chunk(read_size)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            try:
                record = logging.LogRecord(
                    name=logger_name,
                    level=logging.INFO,
                    pathname="",
                    lineno=0,
                    msg=chunk.decode("utf-8", errors="replace"),
                    args=(),
                    exc_info=None,
                )
                handler.handle(record)
            except Exception:  # noqa: S112 -- sink failures must not stop pipe drainage
                continue
    finally:
        with contextlib.suppress(Exception):
            stream.close()
        with contextlib.suppress(Exception):
            handler.close()
