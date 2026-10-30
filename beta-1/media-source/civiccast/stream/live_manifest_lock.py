# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Shared in-process synchronization for the advertised live HLS manifest."""

from threading import Lock

LIVE_MANIFEST_LOCK = Lock()
