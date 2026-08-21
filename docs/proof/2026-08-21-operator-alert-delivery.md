# Operator alert-delivery proof — 2026-08-21

## Automated evidence

- SQLite tests prove atomic first-failure routing, duplicate suppression, one resolution delivery, critical quiet-hours bypass, and warning hold-until-window-end.
- Queue tests prove one due claimant, persisted payload snapshots, duplicate rejection, expired-lease recovery after database reopen, bounded exponential retry, sent terminal state, and dead-letter terminal state.
- Worker tests prove the destination secret is decrypted only after claim, success is persisted, and provider failure returns the same job to its durable retry schedule.
- A real loopback HTTP receiver proves the versioned webhook JSON body and HMAC-SHA256 header; an RFC 4231 vector independently validates the signer. A hostile redirect test proves the signed request is not forwarded.
- A real loopback HTTP receiver proves the Twilio-shaped authenticated SMS form request and the 140-character body limit.
- A real loopback SMTP server proves authenticated SMTP envelope and message submission. A CRLF injection test proves hostile resource text is rejected before connection.
- API tests prove that an operator send-test creates a visible pending `test` delivery plus a resolved informational audit record without exposing destination secrets. Worker tests prove that an explicit test can reach a disabled destination. The production console build exposes destination configuration, send-test, and durable attempt/status history.

## Installed-machine evidence

Candidate commit `759d79e32b9435b564c35c0da1e7128dff5fefa9` produced installer SHA-256 `467052EAC9FA7E97373EE9B1C14C44A0C7BE7E2792D1F1D92014ADF929AA54F4`. Installed verification matched the source receipt and both binary hashes, loaded every required media factory, and reported the service running as LocalSystem with immediate `AUTO_START` plus ready HTTP/database health.

The reproducible proof harness configured a loopback webhook and one temporary enabled channel, terminated that channel's LocalSystem worker once, and received six validly signed firing/resolution requests while automatic restart recovered the channel. The received set contained both `encoder-death` and `off-air`; the durable API subsequently showed both induced incidents resolved. Cleanup disabled the temporary output and pager, and `/health` remained ready.

## Boundary

The incident-driven installed proof above belongs to candidate `759d79e`. Send-test/status is source-level automated evidence until its own exact candidate is installed and exercised. No evidence here claims a credentialed external provider round trip, human receipt, alert-rule editing, or re-alert windows.
