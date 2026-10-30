# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S21 scheduled recording — the last S18 capability gap.

Net-new module that closes S18 gap 2: forward-scheduled capture from live
inputs (SDI/HDMI) and network streams (RTSP/SRT/HLS/RTMP/MPEG-TS/NDI) into
normal ``Asset`` rows. PEG automation coverage: Record Schedule. CivicCast pre-S21
only recorded REACTIVELY (when a live session ended); this adds the
forward-scheduled path a PEG station needs to capture fixed-calendar
meetings.

Two durable tables (migration ``0056_scheduled_recording`` — the long-
reserved sibling slot off ``0055_asrun_and_epg`` per RECONCILIATION's
chain-shape footer; an Alembic merge revision in this same module unifies
``0056`` with the existing ``0059_paywall_access`` head so the global chain
stays single-headed):

* ``recording_schedules`` — one row per (station, name). The
  ``recurrence`` JSON encodes a one-shot date or an RFC-5545-like RRULE
  (reusing the shape S19 already established). ``source`` is a typed
  union: SDI / HDMI / NDI / RTSP / SRT / HLS / RTMP / MPEG-TS. The
  ``encoder_profile`` references the S2/S7 encode-profile registry. The
  ``loudness_regime`` (S11) is applied at capture time. ``target_series``
  + ``custom_field_values`` (S22) auto-file the produced asset.
* ``recording_jobs`` — one row per planned/running/finished capture. The
  state machine is ``scheduled → arming → recording → finalizing → done``
  with terminal ``failed`` / ``skipped`` branches (DC-3 / DC-5). The
  produced ``asset_id`` is set on transition to ``done`` so the rest of
  the asset / readiness pipeline (S7) is unchanged from a watch-folder
  ingest.

The capture pipeline itself is a Protocol (``CapturePipeline``) injected
into ``RecordingService`` — production wires it to the S15 GStreamer
engine; tests inject a stub that records "arm called" / "start called" /
"finalize called" without opening real sockets or file handles. The same
seam pattern as the S26 ``MagicLinkEmailSender`` and the S25
``chapter_provider`` — keeps unit tests fast and the production seam
explicit.

Failure handling is built around the "never a silent miss" invariant
(DC-3): an unreachable source at arm time raises an S8 alert via the
injected ``AlertSink`` Protocol, and the job transitions to ``failed``
with a structured reason. A crash mid-record leaves the job in
``recording`` state; the scheduler's startup reconciliation marks any
``recording``-state job older than its window-end as ``failed`` with a
"interrupted by restart" reason and flags any partial file for operator
review.

The module follows the eas / metadata / reporting / underwriting / agenda
/ paywall layout — bound-param SQL, lazy session factory, role-gated
staff routes (``setup_admin`` + ``meeting_operator`` for write,
``support_admin`` for read), 503 over silent 200 when DI is unwired.
"""
