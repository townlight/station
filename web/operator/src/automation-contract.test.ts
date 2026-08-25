// @ts-nocheck
import assert from "node:assert/strict";
import test from "node:test";
import {
  adaptersForFeature,
  automationPolicyMutationReason,
  automationStatusLabel,
  type AutomationAdapterDescriptor,
} from "./automation-contract.ts";

const adapters: AutomationAdapterDescriptor[] = [
  {
    adapter_id: "22222222-2222-4222-8222-222222222222",
    location: "cloud",
    model_key: "zeta",
    model_identifier: "provider/zeta",
    supported_features: ["summaries"],
    credential_availability: "available",
    revision: 1,
  },
  {
    adapter_id: "11111111-1111-4111-8111-111111111111",
    location: "local",
    model_key: "alpha",
    model_identifier: "alpha:latest",
    supported_features: ["captions", "summaries"],
    credential_availability: "not_required",
    revision: 1,
  },
];

test("feature adapters are filtered and deterministically ordered", () => {
  assert.deepEqual(adaptersForFeature(adapters, "summaries").map((adapter) => adapter.model_key), ["alpha", "zeta"]);
  assert.deepEqual(adaptersForFeature(adapters, "captions").map((adapter) => adapter.model_key), ["alpha"]);
  assert.deepEqual(adaptersForFeature(adapters, "translation"), []);
});

test("writes fail closed on unsupported adapters and non-authoritative hashes", () => {
  assert.match(automationPolicyMutationReason(adapters, "translation", adapters[0].adapter_id, "a".repeat(64)) ?? "", /supports/);
  assert.match(automationPolicyMutationReason(adapters, "summaries", adapters[0].adapter_id, "A".repeat(64)) ?? "", /SHA-256/);
  assert.equal(automationPolicyMutationReason(adapters, "summaries", adapters[0].adapter_id, "a".repeat(64)), null);
});

test("operator-facing status never inflates connection evidence", () => {
  assert.equal(automationStatusLabel("passed"), "Connection verified");
  assert.equal(automationStatusLabel("stale_candidate"), "Retest after software update");
  assert.equal(automationStatusLabel("not_tested"), "Connection not tested");
});
