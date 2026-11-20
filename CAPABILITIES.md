# Townlight Station recorder scope

Current application source is in beta-1/: a Python service and TypeScript operator
UI reusing pinned CivicCast media code. It implements configured single live
capture, meeting/agenda identity, private media validation and Records handoff.
Scheduling, playout and optional delivery code also exists. Source presence does
not establish complete installed operator workflows or field acceptance.

Station is separately configured. Separation does not fix first-run provisioning,
input selection, long-meeting limits or other recorder usability gaps. The service
README and manual document implemented limits.

The former S1-S26 Rust engine ledger is preserved in CivicCast Lab with its history
and dated evidence. It is not this recorder's acceptance scope. See
[the separation record](docs/RUST-SEPARATION.md).
