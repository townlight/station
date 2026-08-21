# Live CG clear transition proof

Date: 2026-08-21

## Scope

This development proof covers the orchestration state transition from an acknowledged visible CG
snapshot to authoritative absence, plus reactivation after clear.

## Evidence

- Missing board, inactive board, and a resolved board with no zones are classified as authoritative
  clear states.
- Invalid data and transient storage failures preserve the last known live state and do not emit a
  false clear.
- A clear is required only after a worker-acknowledged snapshot, is consumed only by `CgCleared`,
  and is not repeated after success.
- A failed or unexpected clear response retains the invalidated authority and pending clear for
  retry, preventing the stale snapshot from being resent during worker recovery.
- A later valid snapshot is eligible for apply even when it reuses the prior board revision.

The verified commands were:

```text
cargo test -p station-orchestration --offline
cargo clippy -p station-orchestration --all-targets --all-features --offline -- -D warnings
```

All eight orchestration tests passed with zero failures and warnings-denied Clippy passed.

## Non-claims

This record does not claim the installed service or decoded transport clear path. The installed CG
image proof is the next gate and must compare the decoded post-clear pixel with the captured program
baseline rather than assuming the program is black.
