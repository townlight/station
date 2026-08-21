# Contributing

TownLight Station is developed in small, executable vertical slices.

## Setup

1. Install Rust 1.97.1 or newer on Windows.
2. Clone the repository.
3. Run `cargo test --offline` to establish a clean baseline.

## Change contract

- Write a failing behavior test before changing logic or a public interface.
- Confirm the test fails for the intended assertion.
- Implement the smallest passing change, then refactor under green tests.
- Keep station authority in the control plane and media timing in channel workers.
- Never commit secrets, station data, credentials, signing material, or generated database files.
- Update operator, API, or architecture documentation in the same change as behavior.

Before committing, run:

```powershell
cargo fmt --check
cargo clippy --offline --all-targets --all-features -- -D warnings
cargo test --offline --workspace
```
