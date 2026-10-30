# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""CivicCast S15 GStreamer playout engine.

`pipeline` holds pure description builders (no ``gi`` import; unit-testable
anywhere). `engine` (added in a later slice) holds the live ``gi``/``Gst``
runtime. Runs on native Windows via the bundled GStreamer runtime and
the D2 named-pipe transport, and on Linux via the POSIX FIFO.
"""
