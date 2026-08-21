# ADR 0027: Required-output progress heartbeats

Date: 2026-08-21

Status: Accepted

## Context

A live worker process does not prove that its protected control loop is responsive or that encoded bytes are leaving every required branch. The prior orchestration loop checked process presence and then wrote its own five-second database timestamp. A hung worker or an SRT connection lost after startup could therefore retain green runtime truth.

## Decision

Every five seconds the supervisor sends a real `Ping` command through the protected named pipe. Protocol 1.4 returns one ordered byte counter per enabled output in the durable `Heartbeat` event.

- UDP branches report GStreamer's `bytes-served`; SRT branches report `bytes-sent-total` from the SRT statistics structure.
- The first heartbeat requires a nonzero count for every required output. Each later heartbeat requires strict progress from every required output.
- Output count or index disagreement is a protocol failure. A GStreamer bus error is an output-health failure rather than a successful heartbeat.
- A timeout or invalid response is a worker-control failure. A pipeline error or stalled required counter stops and reaps the worker, records red channel truth, routes critical `output-stall` and `off-air` incidents, and enters the bounded 1/5/15/60-second restart path.
- Optional-output policy remains separate. This slice does not claim remote receiver acknowledgment for UDP; UDP progress proves only that the local sink continues accepting encoded bytes.

## Consequences

Database heartbeat freshness now represents a successful service-to-worker round trip plus required local transport progress, not daemon-side optimism. SRT receiver loss becomes red and triggers recovery even when the worker process remains alive. UDP can still require a downstream monitoring device for end-to-end headend truth.

Five-second heartbeat events add about 17,280 compact journal records per enabled channel per day. Journal retention and compaction must be bounded before the multi-station field gate; the current append-only journal is acceptable for development and short proofs but is not the final retention design.
