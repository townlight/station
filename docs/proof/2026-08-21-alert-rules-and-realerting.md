# Alert-rule and re-alert proof — 2026-08-21

## Automated evidence

- Domain tests reject invalid destination identities and re-alert windows beyond seven days.
- Fresh databases seed one validated rule for every registered condition; API tests list the seed, persist an operator edit, and recover the exact policy after reopen.
- Storage tests prove a rule overrides caller severity, scopes to one output, routes only to a selected destination, suppresses repeat samples inside the interval, blocks duplicates while a re-alert is pending, and creates one new durable `realert` after each completed interval.
- The same lifecycle test proves `notify_on_resolve=false` closes the incident without creating a resolution delivery.
- A migration test rebuilds the former delivery constraint, preserves a historical sent row, adds re-alert support, and seeds all rules on an existing database.
- The production operator-console build includes rule enabled/severity, output scope, all-or-selected destinations, dedupe interval, re-alert interval, and resolution-notice controls.

## Boundary

This is source-level contract, persistence, API, migration, and production-UI evidence. It does not claim the rule/re-alert slice is installed until an exact candidate upgrades the existing station database and an installed LocalSystem failure exercise crosses a shortened re-alert window.
