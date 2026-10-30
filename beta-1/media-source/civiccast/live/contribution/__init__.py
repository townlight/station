# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S17 Remote Contribution (VDO.Ninja) — the live/real-time sibling of the
asset-upload ``civiccast/contribute/`` portal.

Lets remote participants (remote council members, remote presenters, public
comment from home) join a CivicCast channel over the browser via WebRTC. The
guest feed is produced by a self-hosted, unmodified VDO.Ninja process (AGPL-3.0,
arms-length) plus a coturn TURN server, driven from the portal via VDO.Ninja's
IFRAME API, composited (GStreamer ``wpesrc`` / OBS browser source) into a clean
frame, and ingested as an existing ``live/`` NDI/SRT ``LiveSource`` — after which
the existing ``LiveSession`` + ``RecordingTarget`` lifecycle and the S15 egress
engine carry it to air, recorded, with no new egress code.

This package owns the **room / invite / session orchestration** and the
co-process supervision of VDO.Ninja + coturn. It does NOT own compositing or
egress (see S17 §1 honest scope boundary).
"""
