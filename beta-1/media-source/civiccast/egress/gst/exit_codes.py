# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Exit-code contract between the GStreamer playout worker (``worker.py``) and
the daemon (``civiccast.egress.daemon``).

Deliberately gi-free with zero import-time side effects: the daemon must be
able to read these constants WITHOUT triggering ``civiccast.egress.gst.engine``'s
module-level ``bootstrap_installed_gstreamer_runtime()`` / ``Gst.init()`` calls,
which is exactly what importing ``engine.py`` from the daemon process would do.

Item 82 (sandbox run 13): a fresh worker under CPU load can take longer than a
few seconds to reach PLAYING (preroll). That is a slow start, not a crash, and
the daemon's relaunch path (``EgressDaemon._relaunch_after_crash`` /
``_begin_relaunch``) needs to tell the two apart from the child's exit code
alone — the only signal that survives the subprocess boundary.

Item 84 (measured in sandbox run 15, soak-fcfcb81-20260906-183448Z, and in
three seamless-OFF runs): PLAYING is reached quickly (``CTRL preroll: reached
PLAYING after 0.3s``) but the FIRST output buffer can legitimately take much
longer under start-up load than the 10s post-first-buffer stall bound was
ever meant to cover -- a slow first buffer is a slow start too, distinct from
both an ordinary crash AND from a preroll that never reached PLAYING at all,
and needs its own exit code for the same relaunch-path reason above.
"""

from __future__ import annotations

# 0 = clean exit (unchanged).
# 1 = generic worker error/crash — every engine failure except the ones named
#     below (unchanged: this is still the default `main()` returns for a
#     non-None `result["error"]`, e.g. the post-first-buffer S9-5 stall
#     watchdog, ``("stall", ...)``).
# 2 = worker.py usage error (missing argv[1]) — predates this change.
GST_PREROLL_TIMEOUT_EXIT_CODE = 3
# Item 84: the pipeline reached PLAYING but never produced a single output
# buffer within ``GstPlayoutEngine.first_output_timeout_s`` -- a slow start
# under load, not a crash and not a preroll failure. See
# ``GstPlayoutEngine._check_stall`` (the ``("first-output-timeout", ...)``
# error reason) and ``EgressDaemon._relaunch_after_crash``, which rate-limits
# this exit the same way it already does ``GST_PREROLL_TIMEOUT_EXIT_CODE``.
GST_FIRST_OUTPUT_TIMEOUT_EXIT_CODE = 4

# Item 85 (sandbox runs 12/14/15): ``GstPlayoutEngine._commit_reload`` did not
# finish within its own watchdog bound (``_arm_commit_watchdog``,
# ``commit_timeout_s``). The wedge this item was opened against is NOT yet
# localized to a specific line inside ``_commit_reload``/``_dispose_source_
# leg`` -- see those methods' own docstrings for round 1's reordering
# hypothesis and why hostile review reverted it. What IS proven: a
# ``GLib.timeout_add`` source could never fire if the wedge is the SAME
# GLib main-loop thread that would run it, so only a real OS thread
# (``threading.Timer``) can escape it; that thread dumps every live Python
# stack (``faulthandler.dump_traceback``) before force-exiting via
# ``os._exit`` with this distinct code -- the localization tool for whichever
# future soak reproduces the wedge. UNLIKE ``GST_PREROLL_TIMEOUT_EXIT_CODE``
# and ``GST_FIRST_OUTPUT_TIMEOUT_EXIT_CODE`` above (both genuine "slow start,
# not a crash" cases), this exit is deliberately treated as an ORDINARY CRASH
# by the daemon's relaunch path (counts toward the crash-loop streak on every
# occurrence, no rate-limited exemption) -- a reload-commit wedge is a real
# failure of an already-running channel, not a slow-but-progressing start.
# Also unlike every other exit path in this worker: this one emits NO
# ``WORKER_RESULT`` receipt, by design -- ``os._exit`` bypasses every
# remaining line of Python on this process, including whatever would have
# built and printed that receipt, so a caller reading this exit (e.g.
# ``civiccast.native.installed_gstreamer_smoke.require_clean_worker_result``)
# must treat this code as its own distinct, receipt-less signal rather than a
# missing-receipt failure of some other kind.
GST_RELOAD_COMMIT_TIMEOUT_EXIT_CODE = 5
