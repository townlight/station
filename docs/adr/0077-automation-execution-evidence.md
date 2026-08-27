# ADR 0077: Automation execution evidence

## Decision

TownLight records model execution separately from connection-test authority. An execution request
binds one server-owned job to the exact current feature policy, operator-selected adapter, policy and
adapter revisions, configuration hash, verified active-slot install generation, input artifact digest and size, and
request time. Captions accept a media artifact; summaries accept a text-bearing artifact; translation
accepts a language-bound text artifact plus a distinct requested target language.

A successful receipt repeats that complete binding and adds an exact output artifact digest, size,
media type, feature-specific structural facts, bounded execution interval, and an execution-engine
proof. A local proof identifies the executable and model artifact by SHA-256. A cloud proof records
digests of the provider request identity and provider response. Raw prompts, content, credentials,
tokens, provider request identifiers, and provider responses are not receipt fields. Unknown fields
are rejected.

Receipt validation fails closed on stale candidate, policy, adapter, or configuration authority;
input substitution; local/cloud engine mismatch; malformed or empty artifacts; cross-feature output
facts; wrong translation target; or an execution interval longer than 24 hours. These contracts make
it possible for later storage and controller code to preserve exact execution provenance without
turning a connection test into an execution claim.

The single SQLite writer persists exact execution requests as idempotent jobs. Enqueue requires a
current passing connection test. Claim and renewal require opaque verified-install authority and use
a bounded worker identity, random per-attempt claim identity, claim start time, attempt count, and
lease. An expired claim can be reclaimed without letting its old owner or its receipt complete.
Completion
rechecks current policy, adapter, configuration, candidate, and connection-test authority inside the
same immediate transaction that appends the immutable receipt and marks the job complete. Indexed
identity, revision, candidate, input, output, size, and time columns are checked against the stored
JSON on every read. Request-binding columns cannot be updated, terminal job rows and receipt rows
reject update or delete, and reads discover receipts by durable job identity so a corrupted receipt
pointer cannot hide terminal evidence. Only the live exact lease owner can record an attempt failure.
A retryable failure returns the job to Pending with a durable next-attempt time until the bounded
attempt ceiling; the initial delay is one minute and doubles to a one-hour ceiling. A nonretryable
failure or exhausted ceiling produces an immutable Failed terminal row with a bounded categorical
failure code and no receipt.

FIFO recovery terminalizes stale current-authority bindings as `authority_changed` and an expired
tenth attempt as `attempt_limit_exhausted`, then continues to later current work in the same bounded
transaction. Receipt validation requires the exact live claim identity and an execution interval
beginning no earlier than that claim. Reads validate bounded failure codes and discover receipts by
job identity even when a corrupted parent pointer or parent row tries to hide them.

## Boundaries and non-claims

stationd runs an automation-execution queue controller under the install-authority fence, but the
installed runtime deliberately registers no executable adapter. It records paced `adapter_unavailable`
truth without entering executable setup. The experimental base controller accepts only a
declarative installed worker program whose adapter ID/revision matches the claimed request and whose
regular executable is rooted under the trusted installed application tree, free of reparse points, and
rehashes to the configured identity. The host starts the child suspended, assigns it to a fresh
kill-on-close Windows Job Object before resuming its primary thread, drains bounded stdout and stderr
concurrently, and checks both tree termination and reaping before parsing a receipt. Its monotonic
deadline bounds an already-started owned child and cancellable drains, but synchronous Windows path,
hash, and `CreateProcess` setup are not independently cancellable. This slice therefore does not claim
an absolute 60-second host boundary for a configured executable adapter; production registration is
blocked until setup moves behind a separately killable helper boundary.

`station-automation-launch-helper` is built and may be packaged, installed, and bound into the
candidate and active-package manifests. Schema-2 packages retain compatibility when it is absent.
Any future elected execution path must verify that helper's exact active-candidate identity under the
install fence before using it; the current unavailable path
claims and durably paces work without requiring the helper. Its
length-framed stdin read is bounded by a helper-process watchdog and the request is schema-closed, binding exact job, claim, adapter,
candidate, install generation, configuration, input digest/size, output name, and deadline. It has
no prompt, credential, provider-response, or diagnostic field. The legacy root-authority mode
presently has no configured semantic adapter: it does not resolve or open request-named input/output
paths, creates no output, and returns only categorical `adapter_unavailable`. The separate,
unregistered `cloud_summary_v1` helper mode is schema-closed and accepts only three exact inherited
handles (regular input, regular output, and a bounded framed credential pipe): no artifact pathname
or root authority is accepted. Its first provider route is fixed to HTTPS
`api.openai.com/v1/chat/completions`, with redirects disabled, a bounded request/response/deadline,
`store:false`, non-streaming one-choice requests, and categorical redacted failures. It writes only
validated plain-text draft output and returns its digest/bytes/word count plus digests of the provider
request identifier and response; it never returns raw prompt, credential, or provider text. The
controller does not yet construct the restricted Windows handle list or invoke this mode, so no
production semantic adapter is elected and synthetic-only protocol coverage is not provider proof. A prior pathname-based managed-artifact
prototype was removed after proving that a retained directory handle did not prevent rename followed
by junction substitution before the later leaf open. A future adapter must instead use handle-relative
opens or exact inherited input/output-parent handles with file-identity binding; managed-handle proof
therefore remains open. The host supervisor starts an owned child suspended, assigns it before resume
to a fresh kill-on-close Job Object capped at eight active processes and 1 GiB total tree memory, bounds
argv, environment, stdout, stderr, and monotonic execution time, and terminates and reaps the tree.
No production semantic adapter is configured, so the packaged helper is not invoked as a fake
adapter and current jobs retain categorical `adapter_unavailable`. Handle-relative managed-artifact
proof, usable adapter configuration, installed execution proof, and field proof remain open.
An executable-helper capability requires a fence bound to both the canonical data authority root and
the canonical active application root; both retained directory handles bind their Windows identities
and exclude deletion while the fence is held. The update receipt does not independently persist an
absolute application-root pathname, so the runtime binds that supplied root only for the fenced
candidate-read-and-construction epoch. No semantic adapter is elected until that execution boundary
can also bind any managed artifacts safely.
Configured input/output roots must be trusted, ACL-protected ordinary local-drive directories; UNC,
verbatim/device/NT paths, aliases, traversal, reserved components, and unsafe Win32 names fail
closed. Artifact paths must include at least one helper-owned relative parent directory. These fields
are validated contract data only in this unavailable prototype and are not filesystem authority.
An expired claim is left for
the next durable claim transaction rather than allowing its old capability to write a late failure; renewal,
retryable failure, and completion all require the random per-attempt claim identity as well as the stable
worker identity. A returned receipt is not success until the storage completion transaction rechecks the live
candidate, policy, adapter, configuration, connection evidence, claim, and receipt binding. A late
worker return is never completed. Restarted or expired claims remain
recoverable through the durable queue rules. Fence, verified-install, and store failures are not
treated as idle: the controller appends/routs a redacted durable S13 `ServiceDown` health alert and
retries with bounded backoff, resolving that alert only after a successful durable iteration. The
authenticated `GET /api/v1/automation/jobs/{job_id}` status route exposes request/status/receipt and
next-attempt truth without lease-owner or claim identity.

No production automation adapter is implemented or registered in this slice. Current jobs with no
exact registered adapter record a paced retryable `adapter_unavailable` attempt and no receipt; this
does not claim model execution, provider contact, output creation, or external success. The base
controller's Job Object boundary does not prove that any provider worker has implemented managed
input/output-handle verification, nor does it prove an installed candidate actually runs a production
adapter. Later adapters must hash input and output from retained managed handles and store secrets only
in the Windows-protected credential authority.
