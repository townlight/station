# ADR 0011: Resilient watch-folder ingest

## Status

Accepted — 2026-08-20

## Context

An unattended station must ingest recorder, USB, and NAS drops without requiring an operator to upload every file. A directory listing is not proof that a file is complete: recorders commonly expose the destination while it is still growing, and SMB availability can change during copy. Volatile discovery state also causes duplicate work after a daemon restart.

## Decision

`WatchFolderConfig` is durable station authority. It contains an absolute monitor path, a bounded lowercase extension allow-list, an operator-configured settle duration, enablement, and service-owned scan health. An unavailable folder records an operator-readable error and does not stop other scanning, ingest, playout, or the control plane.

The monitor polls every five seconds. It accepts only direct, nonempty regular files whose extension is allowed. SQLite stores each path's size, modification time, unchanged-since time, observation count, and queued job identity. A file may be queued only after at least two matching observations and the full settle duration. Any size or modification-time change resets settling.

The queued job UUID is deterministically derived from the configuration identity, path, size, and modification time. Creating the job and marking the observation are therefore safely repeatable after a crash; an unchanged file version cannot be duplicated, while a genuinely changed version produces a new identity.

Watch-folder work uses the same media admission boundary as operator work. The destination is re-hashed and size-checked after its atomic library move, then re-probed. Source-not-found and file-I/O failures receive a durable bounded retry schedule; unsupported or invalid media remains a visible terminal failure. Existing databases gain the retry-schedule column through an in-place, write-locked migration.

## Consequences

- Local disks, removable storage, and SMB/NAS shares use one behavior and one proof path.
- Temporary share outages are visible but do not create false permanent media rejection.
- File identity does not rely on NAS inode semantics.
- Renaming an identical file can create a second ingest job, but content-addressed admission still converges on one asset.
- Recursive traversal and delete-after-import are not implicit; an operator must configure each intended drop directory, and source files remain untouched.
