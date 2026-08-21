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
