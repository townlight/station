# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S16 — Production & Control Room.

A thin CivicCast control surface that drives the station's existing production
switchers (OBS / vMix / ATEM / HyperDeck / PTZ / OSC / generic TCP-HTTP /
CasparCG) through TSR (timeline-state-resolver, MIT), then routes the produced
program feed into the S15 playout engine as an ordinary ``live/`` source. S16
produces the source; S5 arbitrates air.

``gpi`` and ``serial`` are also selectable ``ProductionDevice`` kinds, but
honestly labeled: they are network-relay triggers (TCP), not direct hardware
support. There is no GPI contact-closure or RS-232/422 serial driver in this
release -- ``tsr_service/index.mjs`` routes both through TSR's generic
``TCPSEND`` adapter, same as the plain ``tcp`` kind. A station that needs real
hardware fronts it with its own TCP-to-GPI or TCP-to-serial relay box. See
``ProductionDevice.kind``'s field description (``models.py``) and
``CAPABILITIES.md``.

This package owns the CivicCast-side control plane (models, store, cue plan/fire
service, API, operator console). The Node TSR sidecar that actually speaks to
the devices lives under ``tsr_service/`` and is reached only over a localhost
REST/IPC contract — no GPL/AGPL source is vendored into this Apache tree.
"""
