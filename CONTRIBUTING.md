# Contributing to Townlight Station

Source is the Python recorder in beta-1/ and TypeScript UI in beta-1/operator-ui/.
Read the [service README](beta-1/README.md) for explicit runtime configuration.

For existing source contracts, install beta-1/ and pytest in an isolated Python
environment, then run `python -X utf8 -m pytest tests -q` from beta-1/. Real media
and database acceptance need their explicitly configured dependencies; a contract
pass does not establish installed behavior.

Preserve the media lock, staff authorization, recording provenance and safe
idempotent handoffs. Run meaningful affected checks and update affected docs.
Do not commit credentials, recordings, databases or signing keys.

The Rust engine and Cargo instructions moved to private CivicCast Lab. See
[the separation record](docs/RUST-SEPARATION.md).
