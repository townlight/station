# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""The session-0 native supervisor (slice:ws5-supervisor).

Charter Sec.7 step 1. The supervisor is the Windows service that starts the
direct children (PostgreSQL, the FastAPI control plane) in D6 order,
keeps them alive per D5, and reports a single overall state. NATS JetStream
was removed from the product (owner decision 2026-08-20; see ADR 0023, which
supersedes ADR 0001) -- it was never one of the supervised direct children's
production event path, only a health gate and packaging cost. Media workers stay
owned by the egress daemon inside the control plane (D2) -- the supervisor does
not take over per-channel worker lifecycle.

This package is layered so the correctness-critical logic stays pure and
CI-testable on any OS, and the Windows-only mechanism (Job Object, named pipe,
service framework) sits at the edges:

* ``config``  -- the configuration model and fixed identity constants (pure).
* ``states``  -- the supervisor/child state vocabulary and the pure, total
  transition function plus the D5/D6 helper predicates (pure).

The Windows-mechanism modules (job object, control-pipe server, service shim)
land in later commits on this slice and import only from these pure modules.
"""
