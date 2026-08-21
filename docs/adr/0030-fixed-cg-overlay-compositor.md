# ADR 0030: Fixed CG overlay compositor and snapshot handoff

Date: 2026-08-21

Status: Accepted

## Context

The CG authority and resolver produce bounded, cache-only resolved snapshots, while the media
worker owns a persistent GStreamer graph. Updating a live graph by adding and removing elements
would risk output continuity. The first renderer slice must therefore have a fixed topology and a
small, explicit worker protocol boundary.

## Decision

The media engine builds one continuously flowing black canvas and eight logical serial overlay
pairs. The serial chain then feeds one shared black-key transparency stage, queue, and compositor
pad at z-order 1. UDP branches install the fixed branch before PLAYING; SRT branches
install it only after required output readiness. Text and clock rendering are mutually exclusive
through their `silent` properties. Snapshot updates change element properties and pad alpha/z-order
only; they never mutate the topology while PLAYING. The base program/fallback video remains
compositor z-order 0. Logical overlays are sorted by `(z_index, slot)` before assignment to physical
serial layers, so higher z layers are drawn later.

`station-media-protocol` carries bounded `ApplyCgSnapshot` and `ClearCgSnapshot` commands. The
worker applies them on its existing media-control thread and emits `CgApplied`, `CgCleared`, or a
nonfatal structured `CommandRejected`. Sequence checks and durable journal-before-emit behavior
remain unchanged.

The cache-only resolved snapshot is the authority handed to the worker; the worker performs no
database or network access. Deduplication compares the complete snapshot, not only board ID and
revision, so content changes with the same board revision are applied. Validation completes before
the current visible state is hidden. Invalid updates retain the last valid snapshot; clear makes
all slots transparent.

The implemented content scope is manual text, text bulletins, clocks, and static lower-third,
ticker-region, and bug-region geometry. Text is bounded, control-safe, and newline-normalized.

## Consequences

The fixed topology preserves persistent playout continuity and bounds worker protocol payloads.
The one-canvas serial design avoids sixteen full-frame overlay branches. The renderer can be
tested independently of feed/network availability, and malformed updates fail without taking the
existing output off air.

## Explicit non-claims

This ADR does not claim image/logo rendering, WPE/HTML styling, live-video bulletins, audio CG,
animated ticker crawling, program-aware interstitials, or field/soak operation. The decoded UDP
MPEG-TS pixel apply/change/clear proof is bounded to the focused machine test; it is not an
installed, multi-station, or field-soak claim.
