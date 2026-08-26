// @ts-nocheck
import assert from "node:assert/strict";
import test from "node:test";
import {
  publishAccountMutation,
  publishTargetMutation,
  publishingAccountMutationReason,
  publishingHistoryPath,
  publishingReadinessLabel,
  publishingTargetMutationReason,
  redactConfigurationHash,
  LatestPublishingAuthorityLoad,
} from "./publishing-contract.ts";

test("publishing account mutations reject unbounded or blank browser fields", () => {
  assert.match(publishingAccountMutationReason({ kind: "portal", label: "", enabled: true, credentialConfigured: false }, null) ?? "", /label/i);
  assert.match(publishingAccountMutationReason({ kind: "portal", label: "x".repeat(201), enabled: true, credentialConfigured: false }, null) ?? "", /label/i);
  assert.match(publishingAccountMutationReason({ kind: "portal", label: "Portal", enabled: true, credentialConfigured: false }, 0) ?? "", /revision/i);
  assert.equal(publishingAccountMutationReason({ kind: "portal", label: "Portal", enabled: true, credentialConfigured: false }, null), null);
});

test("target mutations require a bounded account, authoritative hash, and exact CAS revision", () => {
  const draft = { accountId: "11111111-1111-4111-8111-111111111111", kind: "portal", label: "Civic portal", enabled: true, configurationHash: "a".repeat(64) };
  assert.equal(publishingTargetMutationReason(draft, 4), null);
  assert.match(publishingTargetMutationReason({ ...draft, configurationHash: "A".repeat(64) }, 4) ?? "", /SHA-256/);
  assert.match(publishingTargetMutationReason({ ...draft, accountId: "not-an-id" }, 4) ?? "", /account/i);
  assert.match(publishingTargetMutationReason(draft, Number.MAX_SAFE_INTEGER + 1) ?? "", /revision/i);
});

test("browser mutation DTOs omit secrets, trusted evidence, identities, and attribution", () => {
  const account = publishAccountMutation({ kind: "portal", label: " Portal ", enabled: true, credentialConfigured: true }, 2);
  assert.deepEqual(account, { kind: "portal", label: "Portal", enabled: true, credential_configured: true, expected_revision: 2 });
  assert.deepEqual(Object.keys(account).sort(), ["credential_configured", "enabled", "expected_revision", "kind", "label"]);
  const target = publishTargetMutation({ accountId: "11111111-1111-4111-8111-111111111111", kind: "portal", label: " Portal target ", enabled: true, configurationHash: "a".repeat(64) }, null);
  assert.deepEqual(Object.keys(target).sort(), ["account_id", "configuration_hash", "enabled", "expected_revision", "kind", "label"]);
  assert.equal("evidence" in target || "secret" in target || "credential" in target, false);
});

test("readiness labels and history paths avoid delivery and audience claims", () => {
  assert.equal(publishingReadinessLabel("pass"), "Connection authority current");
  assert.doesNotMatch(publishingReadinessLabel("pass"), /delivery|audience|published/i);
  assert.equal(publishingHistoryPath("targets", "a/b?c"), "/api/v1/publishing/targets/a%2Fb%3Fc/history?limit=20");
  assert.equal(redactConfigurationHash("a".repeat(64)), "aaaaaaaaaa…aaaaaa");
  assert.equal(redactConfigurationHash("not-a-hash"), "Not recorded");
});

test("only the newest overlapping publishing authority response may update operator state", () => {
  const load = new LatestPublishingAuthorityLoad();
  const first = load.begin();
  const second = load.begin();
  assert.notEqual(first, second);
  assert.equal(load.accepts(first), false);
  assert.equal(load.accepts(second), true);
  load.invalidate();
  assert.equal(load.accepts(second), false);
});
