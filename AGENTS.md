# TownLight Station engineering rules

- Build the complete station product; do not redefine acceptance by shrinking capability scope.
- Keep the active repository free of historical product names, code layouts, runtime assumptions, and repository history.
- Treat archived material only as a behavior, fixture, and product-requirement quarry.
- Use Rust for the native appliance and TypeScript/React for web interfaces unless an accepted architecture decision changes that boundary.
- Preserve one authoritative SQLite writer and one isolated persistent media worker per channel.
- Never introduce PostgreSQL, NATS, WSL, a Python runtime chassis, or FFmpeg-based linear playout.
- Work test-first for logic and public interfaces. Capture a real failing assertion before implementation and run the full relevant suite before commit.
- Do not claim a capability from source presence. Require the same-candidate installed-machine evidence.
- Never commit secrets, signing material, customer data, generated databases, or local environment files.
- Keep commits atomic and update contracts and documentation with behavior.
- Treat Windows Sandbox as a shared machine resource. Never assume it is free. Before every launch, use
  `scripts/invoke-owned-windows-sandbox.ps1`; unknown ownership, an active sandbox process, or a
  concurrent launch decision fails closed. Never terminate a sandbox process by name or broad
  search. Cleanup is permitted only for an exact PID, creation identity, and executable recorded by
  the harness that launched it. All Claude-owned or Claude-created local processes and files are
  inside a no-touch fence, always outside TownLight's ownership boundary, and must not be inspected
  beyond the minimum identity needed to detect occupancy, changed, moved, deleted, or used as
  cleanup targets.
- Run Cargo commands from the workspace root. On Windows, invoke Cargo only through
  `scripts/invoke-cargo.ps1`; it binds Cargo and every descendant process to the explicit GStreamer
  runtime. The checked-in target runner is a second boundary. Never bypass either boundary or launch
  `target` artifacts directly.
- This is an operator-safety rule, not a preference: raw `cargo`, `cargo.exe`, direct test binaries,
  and IDE test launchers can create persistent modal Windows DLL-error dialogs. The only allowed
  Windows Cargo command form is `powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass
  -File scripts\invoke-cargo.ps1 ...`. If that wrapper cannot run, report the test as blocked; do not
  substitute another launcher.
