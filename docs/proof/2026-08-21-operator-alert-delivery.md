# Operator alert-delivery proof — 2026-08-21

## Automated evidence

- SQLite tests prove atomic first-failure routing, duplicate suppression, one resolution delivery, critical quiet-hours bypass, and warning hold-until-window-end.
- Queue tests prove one due claimant, persisted payload snapshots, duplicate rejection, expired-lease recovery after database reopen, bounded exponential retry, sent terminal state, and dead-letter terminal state.
- Worker tests prove the destination secret is decrypted only after claim, success is persisted, and provider failure returns the same job to its durable retry schedule.
- A real loopback HTTP receiver proves the versioned webhook JSON body and HMAC-SHA256 header; an RFC 4231 vector independently validates the signer. A hostile redirect test proves the signed request is not forwarded.
- A real loopback HTTP receiver proves the Twilio-shaped authenticated SMS form request and the 140-character body limit.
- A real loopback SMTP server proves authenticated SMTP envelope and message submission. A CRLF injection test proves hostile resource text is rejected before connection.

## Boundary

This is local protocol and persistence proof. It does not claim installed-candidate activation of the new worker, a credentialed external provider round trip, human receipt, alert-rule editing, re-alert windows, or the operator send-test/status interface. Those claims require their own evidence.

