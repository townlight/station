// @ts-nocheck
import assert from "node:assert/strict";
import test from "node:test";
import {
  boundedMaterializationError,
  canGenerateRecurringDrafts,
  channelSchedulePath,
  formatUtcMaterializationTime,
  recurringDraftPermissionState,
  scheduleMaterializationPath,
  scheduleMaterializationRequestInit,
  summarizeMaterializationOutcomes,
} from "./schedule-materialization-contract.ts";

test("materialization POST path is exact and encoded with no browser-owned body", () => {
  assert.equal(
    scheduleMaterializationPath("channel/with spaces?"),
    "/api/v1/scheduling/channels/channel%2Fwith%20spaces%3F/materializations",
  );
  const init = scheduleMaterializationRequestInit();
  assert.deepEqual(Object.keys(init), ["method"]);
  assert.equal(init.method, "POST");
  assert.equal("body" in init, false);
  assert.equal("window" in init, false);
  assert.equal("rule_id" in init, false);
  assert.equal("observed_at_utc_ms" in init, false);
});

test("channel schedule refresh path is exact and encoded", () => {
  assert.equal(
    channelSchedulePath("channel/with spaces?"),
    "/api/v1/schedule/items?channel_id=channel%2Fwith%20spaces%3F",
  );
});

test("only scheduling writer roles can generate recurring drafts", () => {
  assert.equal(canGenerateRecurringDrafts(["setup_admin"]), true);
  assert.equal(canGenerateRecurringDrafts(["meeting_operator"]), true);
  assert.equal(canGenerateRecurringDrafts(["support_admin"]), false);
  assert.equal(canGenerateRecurringDrafts([]), false);
});

test("permission loading and authority failures stay distinct from read-only roles", () => {
  assert.equal(recurringDraftPermissionState(null, null), "checking");
  assert.equal(recurringDraftPermissionState([], "Session authority could not be read."), "unavailable");
  assert.equal(recurringDraftPermissionState(["support_admin"], null), "read_only");
  assert.equal(recurringDraftPermissionState(["meeting_operator"], null), "available");
});

test("durable outcome dispositions are counted deterministically", () => {
  assert.deepEqual(
    summarizeMaterializationOutcomes([
      { ordinal: 0, rule_id: "b", outcome: { kind: "proposed" } },
      { ordinal: 1, rule_id: "a", outcome: { kind: "blocked" } },
      { ordinal: 2, rule_id: "a", outcome: { kind: "proposed" } },
      { ordinal: 3, rule_id: "a", outcome: { kind: "existing_committed" } },
    ]),
    [
      { kind: "blocked", count: 1 },
      { kind: "existing_committed", count: 1 },
      { kind: "proposed", count: 2 },
    ],
  );
});

test("operator errors and UTC timestamps stay bounded and deterministic", () => {
  assert.equal(formatUtcMaterializationTime(4_070_908_800_000), "2099-01-01 00:00:00 UTC");
  assert.equal(boundedMaterializationError(new Error("  unavailable  ")), "unavailable");
  assert.equal(boundedMaterializationError({}), "Recurring drafts could not be generated.");
  assert.equal(boundedMaterializationError(new Error("x".repeat(800))).length, 500);
});
