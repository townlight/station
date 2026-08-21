# Alert-rule and re-alert proof — 2026-08-21

## Automated evidence

- Domain tests reject invalid destination identities and re-alert windows beyond seven days.
- Fresh databases seed one validated rule for every registered condition; API tests list the seed, persist an operator edit, and recover the exact policy after reopen.
- Storage tests prove a rule overrides caller severity, scopes to one output, routes only to a selected destination, suppresses repeat samples inside the interval, blocks duplicates while a re-alert is pending, and creates one new durable `realert` after each completed interval.
- The same lifecycle test proves `notify_on_resolve=false` closes the incident without creating a resolution delivery.
- A migration test rebuilds the former delivery constraint, preserves a historical sent row, adds re-alert support, and seeds all rules on an existing database.
- The production operator-console build includes rule enabled/severity, output scope, all-or-selected destinations, dedupe interval, re-alert interval, and resolution-notice controls.

## Boundary

Candidate commit `1c77c98f9436d4097af4e2d6017259e12c27dfae` produced installer SHA-256 `D16169C343F3520F902C1EBF7344941960016531BA0F49C8EB86124122FF0BC0`. Installed verification matched that source receipt and both binary hashes, reported the automatic LocalSystem service and database ready, and loaded every required media factory. The existing station database upgraded in place, exposed all 14 seeded rules, and retained its prior alert/delivery history.

The installed fault harness shortened only `encoder-death` and `off-air` to a one-second re-alert window, routed them to one temporary signed loopback webhook, enabled one isolated output, and used an elevated helper that verified the exact installed executable path and channel command line before repeatedly terminating only that worker for eight seconds. The installed LocalSystem service emitted five correctly signed notifications spanning `firing`, two distinct durable `realert` rows, and `resolved` across both conditions. Automatic worker recovery resolved every proof event. Cleanup restored both rules to their 3600-second defaults, disabled the proof output and destination, left zero proof events firing, and `/health` remained ready.

This proves the rule/re-alert slice on the installed candidate and migrated operating database. It does not claim a credentialed external-provider round trip, human receipt, resource sampling, scheduled self-tests, or update alerting.
