// @ts-nocheck
import assert from "node:assert/strict";
import test from "node:test";
import {
  boundedMaterializationError,
  appendMaterializationHistoryPage,
  canGenerateRecurringDrafts,
  channelSchedulePath,
  continueAfterMaterialization,
  formatUtcMaterializationTime,
  scheduleMaterializationHistoryPath,
  recurringDraftPermissionState,
  materializationHistoryOriginLabel,
  materializationChannelRenderState,
  parseMaterializationHistoryResponse,
  recurringDraftChannelState,
  recordedValue,
  scheduleMaterializationPath,
  scheduleMaterializationRequestInit,
  shouldApplyMaterializationHistoryResponse,
  shouldApplyMaterializationChannel,
  summarizeMaterializationOutcomes,
} from "./schedule-materialization-contract.ts";

const HISTORY_CHANNEL = "22222222-2222-4222-8222-222222222222";
const HISTORY_RULE = "33333333-3333-4333-8333-333333333333";
const HISTORY_SEARCH = "44444444-4444-4444-8444-444444444444";
const HISTORY_BLOCK = "55555555-5555-4555-8555-555555555555";

function validHistoryEntry(
  runId = "eca684f6-15eb-8983-a5c6-15ae994e5933",
  occurredAt = 1000,
  plans = [{ ordinal: 0, rule_id: HISTORY_RULE, rule_revision: 1, channel_id: HISTORY_CHANNEL, saved_search_id: HISTORY_SEARCH, saved_search_revision: 1, block_id: HISTORY_BLOCK, block_revision: 1, window: { starts_at_utc_ms: 1000, ends_at_utc_ms: 2000 } }],
  generatedDrafts = [],
  resultSha = "b".repeat(64),
) {
  return {
    run_id: runId,
    channel_id: HISTORY_CHANNEL,
    origin: "automatic",
    actor_id: "00000000-0000-4000-8000-000000000022",
    request_sha256: "a".repeat(64),
    result_sha256: resultSha,
    window: { starts_at_utc_ms: 1000, ends_at_utc_ms: 2000 },
    observed_at_utc_ms: 1000,
    occurred_at_unix_ms: occurredAt,
    checkpoint_utc_day: 0,
    plans,
    outcomes: generatedDrafts.map((draft, index) => ({
      ordinal: index,
      rule_id: HISTORY_RULE,
      rule_revision: 1,
      channel_id: HISTORY_CHANNEL,
      saved_search_id: HISTORY_SEARCH,
      saved_search_revision: 1,
      block_id: HISTORY_BLOCK,
      block_revision: 1,
      plan_window: { starts_at_utc_ms: 1000, ends_at_utc_ms: 2000 },
      materialization_id: "d".repeat(64),
      compiled_outcome: { kind: "proposed", occurrence_id: draft.occurrence_id, schedule_item_id: draft.schedule_item_id, asset_sha256: draft.current_schedule.asset_id, asset_title: draft.current_schedule.title, starts_at_utc_ms: draft.current_schedule.starts_at_unix_ms, ends_at_utc_ms: draft.current_schedule.starts_at_unix_ms + draft.current_schedule.duration_ms },
      outcome: { kind: "proposed", occurrence_id: draft.occurrence_id, schedule_item_id: draft.schedule_item_id, asset_sha256: draft.current_schedule.asset_id, asset_title: draft.current_schedule.title, starts_at_utc_ms: draft.current_schedule.starts_at_unix_ms, ends_at_utc_ms: draft.current_schedule.starts_at_unix_ms + draft.current_schedule.duration_ms },
    })),
    generated_drafts: generatedDrafts,
  };
}

const VALID_DRAFT = {
  occurrence_id: "a".repeat(64),
  schedule_item_id: "9ee05ded-b98d-8ce2-b2f2-ce4028fbbe5b",
  current_schedule: {
    item_id: "9ee05ded-b98d-8ce2-b2f2-ce4028fbbe5b",
    revision: 1,
    channel_id: HISTORY_CHANNEL,
    asset_id: "c".repeat(64),
    title: "Meeting",
    starts_at_unix_ms: 1000,
    duration_ms: 1000,
    state: "draft",
  },
};

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

test("materialization history uses the bounded keyset cursor contract", () => {
  const channelId = "channel/with spaces?";
  assert.equal(
    scheduleMaterializationHistoryPath(channelId),
    "/api/v1/scheduling/channels/channel%2Fwith%20spaces%3F/materializations/history?limit=24",
  );
  assert.equal(
    scheduleMaterializationHistoryPath(channelId, 128, {
      occurred_at_unix_ms: 4_070_908_800_000,
      run_id: "11111111-2222-4333-8444-555555555555",
    }),
    "/api/v1/scheduling/channels/channel%2Fwith%20spaces%3F/materializations/history?limit=128&before_occurred_at_unix_ms=4070908800000&before_run_id=11111111-2222-4333-8444-555555555555",
  );
  assert.throws(() => scheduleMaterializationHistoryPath(channelId, 0));
  assert.throws(() => scheduleMaterializationHistoryPath(channelId, 129));
});

test("history response guard rejects late pages from an older request or channel", () => {
  assert.equal(shouldApplyMaterializationHistoryResponse(4, 4, "channel-a", "channel-a"), true);
  assert.equal(shouldApplyMaterializationHistoryResponse(3, 4, "channel-a", "channel-a"), false);
  assert.equal(shouldApplyMaterializationHistoryResponse(4, 4, "channel-a", "channel-b"), false);
});

test("channel-scoped continuations fail closed when props switch before effects run", () => {
  assert.equal(shouldApplyMaterializationChannel("channel-a", "channel-a"), true);
  assert.equal(shouldApplyMaterializationChannel("channel-a", "channel-b"), false);
});

test("render after a channel prop switch hides prior history and operation state before effects run", () => {
  const previous = materializationChannelRenderState("channel-b", {
    operation_channel_id: "channel-a",
    busy: true,
    error: "channel A generation failed",
    result: { disposition: "applied", run_id: "channel-a-run", window: { starts_at_utc_ms: 1, ends_at_utc_ms: 2 }, plans: [], outcomes: [] },
    history_channel_id: "channel-a",
    history: [{ run_id: "channel-a-history" } as never],
    history_next_before: { occurred_at_unix_ms: 1, run_id: "channel-a-history" },
    history_loading: false,
    history_error: "channel A history failed",
  });

  assert.equal(previous.result, null);
  assert.equal(previous.busy, false);
  assert.equal(previous.error, null);
  assert.deepEqual(previous.history, []);
  assert.equal(previous.history_next_before, null);
  assert.equal(previous.history_loading, true);
  assert.equal(previous.history_error, null);
});

test("delayed channel A onApplied continuation cannot refresh or invalidate channel B history", async () => {
  let releaseApplied!: () => void;
  const applied = new Promise<void>((resolve) => { releaseApplied = resolve; });
  let currentGenerationRequest = 7;
  let historyRefreshes = 0;
  const continuation = continueAfterMaterialization(
    "applied",
    7,
    () => currentGenerationRequest,
    () => applied,
    () => { historyRefreshes += 1; },
  );

  currentGenerationRequest = 8; // The operator switched from channel A to B.
  releaseApplied();

  assert.equal(await continuation, false);
  assert.equal(currentGenerationRequest, 8);
  assert.equal(historyRefreshes, 0);
});

test("history presentation labels keep server origin and unavailable values explicit", () => {
  assert.equal(materializationHistoryOriginLabel("automatic"), "Automatic");
  assert.equal(materializationHistoryOriginLabel("operator"), "Operator");
  assert.equal(materializationHistoryOriginLabel("future" as never), "Unknown origin");
  assert.equal(recordedValue(null), "Not recorded");
  assert.equal(recordedValue(""), "Not recorded");
  assert.equal(recordedValue("draft"), "draft");
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

test("materialization history parser accepts the complete durable response shape", () => {
  const response = parseMaterializationHistoryResponse({
    entries: [{
      run_id: "eca684f6-15eb-8983-a5c6-15ae994e5933",
      channel_id: "22222222-2222-4222-8222-222222222222",
      origin: "automatic",
      actor_id: "00000000-0000-4000-8000-000000000022",
      request_sha256: "a".repeat(64),
      result_sha256: "b".repeat(64),
      window: { starts_at_utc_ms: 1000, ends_at_utc_ms: 2000 },
      observed_at_utc_ms: 1000,
      occurred_at_unix_ms: 1000,
      checkpoint_utc_day: 0,
      plans: [{ ordinal: 0, rule_id: "33333333-3333-4333-8333-333333333333", rule_revision: 1, channel_id: "22222222-2222-4222-8222-222222222222", saved_search_id: "44444444-4444-4444-8444-444444444444", saved_search_revision: 1, block_id: "55555555-5555-4555-8555-555555555555", block_revision: 1, window: { starts_at_utc_ms: 1000, ends_at_utc_ms: 2000 } }],
      outcomes: [{ ordinal: 0, rule_id: "33333333-3333-4333-8333-333333333333", rule_revision: 1, channel_id: "22222222-2222-4222-8222-222222222222", saved_search_id: "44444444-4444-4444-8444-444444444444", saved_search_revision: 1, block_id: "55555555-5555-4555-8555-555555555555", block_revision: 1, plan_window: { starts_at_utc_ms: 1000, ends_at_utc_ms: 2000 }, materialization_id: "d".repeat(64), compiled_outcome: { kind: "proposed", occurrence_id: "a".repeat(64), schedule_item_id: "9ee05ded-b98d-8ce2-b2f2-ce4028fbbe5b", asset_sha256: "c".repeat(64), asset_title: "Meeting", starts_at_utc_ms: 1000, ends_at_utc_ms: 2000 }, outcome: { kind: "proposed", occurrence_id: "a".repeat(64), schedule_item_id: "9ee05ded-b98d-8ce2-b2f2-ce4028fbbe5b", asset_sha256: "c".repeat(64), asset_title: "Meeting", starts_at_utc_ms: 1000, ends_at_utc_ms: 2000 } }],
      generated_drafts: [{ occurrence_id: "a".repeat(64), schedule_item_id: "9ee05ded-b98d-8ce2-b2f2-ce4028fbbe5b", current_schedule: { item_id: "9ee05ded-b98d-8ce2-b2f2-ce4028fbbe5b", revision: 1, channel_id: "22222222-2222-4222-8222-222222222222", asset_id: "c".repeat(64), title: "Meeting", starts_at_unix_ms: 1000, duration_ms: 1000, state: "draft" } }],
    }],
    next_cursor: { occurred_at_unix_ms: 1000, run_id: "eca684f6-15eb-8983-a5c6-15ae994e5933" },
  });
  assert.equal(response.entries[0].origin, "automatic");
  assert.equal(response.entries[0].outcomes[0].outcome.kind, "proposed");
  assert.equal(response.entries[0].outcomes[0].materialization_id, "d".repeat(64));
  assert.equal(response.entries[0].generated_drafts[0].current_schedule.state, "draft");
  assert.equal(response.entries[0].generated_drafts[0].schedule_item_id, "9ee05ded-b98d-8ce2-b2f2-ce4028fbbe5b");
  assert.equal(response.entries[0].run_id, "eca684f6-15eb-8983-a5c6-15ae994e5933");
});

test("materialization history parser rejects unknown origin and malformed nested data", () => {
  const base = { entries: [], next_cursor: null };
  assert.throws(() => parseMaterializationHistoryResponse({ entries: new Array(129).fill(null), next_cursor: null }), /bounded array/i);
  assert.throws(() => parseMaterializationHistoryResponse({ entries: [], next_cursor: { occurred_at_unix_ms: 1, run_id: "11111111-1111-4111-8111-111111111111" } }), /last returned entry/i);
  assert.throws(() => parseMaterializationHistoryResponse({ ...base, next_cursor: { occurred_at_unix_ms: 1, run_id: "run" , extra: true } }), /history response/i);
  const runId = "11111111-1111-4111-8111-111111111111";
  const channelId = "22222222-2222-4222-8222-222222222222";
  const actorId = "00000000-0000-4000-8000-000000000022";
  assert.throws(() => parseMaterializationHistoryResponse({ ...base, entries: [{ run_id: runId, channel_id: channelId, origin: "future", actor_id: actorId, request_sha256: "a".repeat(64), result_sha256: "b".repeat(64), window: { starts_at_utc_ms: 1, ends_at_utc_ms: 2 }, observed_at_utc_ms: 1, occurred_at_unix_ms: 1, checkpoint_utc_day: 0, plans: [], outcomes: [], generated_drafts: [] }] }), /origin/i);
  assert.throws(() => parseMaterializationHistoryResponse({ ...base, entries: [{ run_id: runId, channel_id: channelId, origin: "operator", actor_id: actorId, request_sha256: "not-a-hash", result_sha256: "b".repeat(64), window: { starts_at_utc_ms: 1, ends_at_utc_ms: 2 }, observed_at_utc_ms: 1, occurred_at_unix_ms: 1, checkpoint_utc_day: 0, plans: [], outcomes: [], generated_drafts: [] }] }), /hash/i);
  assert.throws(() => parseMaterializationHistoryResponse({ ...base, entries: [{ run_id: runId, channel_id: channelId, origin: "operator", actor_id: actorId, request_sha256: "A".repeat(64), result_sha256: "b".repeat(64), window: { starts_at_utc_ms: 1, ends_at_utc_ms: 2 }, observed_at_utc_ms: 1, occurred_at_unix_ms: 1, checkpoint_utc_day: 0, plans: [], outcomes: [], generated_drafts: [] }] }), /hash/i);
  assert.throws(() => parseMaterializationHistoryResponse({ ...base, entries: [{ run_id: "eca684f6-15eb-8983-a5c6-15ae994e5933", channel_id: channelId, origin: "automatic", actor_id: actorId, request_sha256: "a".repeat(64), result_sha256: "b".repeat(64), window: { starts_at_utc_ms: 1, ends_at_utc_ms: 2 }, observed_at_utc_ms: 1, occurred_at_unix_ms: 1, checkpoint_utc_day: 0, plans: [{ ordinal: 0, rule_id: "33333333-3333-4333-8333-333333333333", rule_revision: 1, channel_id: channelId, saved_search_id: "44444444-4444-4444-8444-444444444444", saved_search_revision: 1, block_id: "55555555-5555-4555-8555-555555555555", block_revision: 1, window: { starts_at_utc_ms: 1, ends_at_utc_ms: 2 }, extra: "future" }], outcomes: [], generated_drafts: [] }] }), /plan/i);
});

test("history parser bounds plans, rejects duplicate rules, and enforces descending run order", () => {
  const rule = (index) => `${index.toString(16).padStart(8, "0")}-0000-4000-8000-000000000000`;
  const plans = Array.from({ length: 65 }, (_, index) => ({
    ordinal: index,
    rule_id: rule(index),
    rule_revision: 1,
    channel_id: HISTORY_CHANNEL,
    saved_search_id: HISTORY_SEARCH,
    saved_search_revision: 1,
    block_id: HISTORY_BLOCK,
    block_revision: 1,
    window: { starts_at_utc_ms: 1000, ends_at_utc_ms: 2000 },
  }));
  assert.throws(() => parseMaterializationHistoryResponse({ entries: [validHistoryEntry(undefined, undefined, plans)], next_cursor: null }), /plans.*bounded array/i);
  assert.throws(() => parseMaterializationHistoryResponse({ entries: [validHistoryEntry(undefined, undefined, [plans[0], { ...plans[0], ordinal: 1 }])], next_cursor: null }), /duplicate plan/i);
  assert.throws(() => parseMaterializationHistoryResponse({ entries: [validHistoryEntry("d39e0de9-f78e-8e8a-8cb5-05cd76bea837", 1000, undefined, [], "c".repeat(64)), validHistoryEntry("eca684f6-15eb-8983-a5c6-15ae994e5933", 1000)], next_cursor: null }), /descending order/i);
});

test("history parser binds occurrences and keeps nested outcomes inside their plan window", () => {
  const outcome = {
    ordinal: 0,
    rule_id: HISTORY_RULE,
    rule_revision: 1,
    channel_id: HISTORY_CHANNEL,
    saved_search_id: HISTORY_SEARCH,
    saved_search_revision: 1,
    block_id: HISTORY_BLOCK,
    block_revision: 1,
    plan_window: { starts_at_utc_ms: 1000, ends_at_utc_ms: 2000 },
    materialization_id: null,
    compiled_outcome: { kind: "no_match", starts_at_utc_ms: 2000 },
    outcome: { kind: "blocked", starts_at_utc_ms: 1500 },
  };
  assert.throws(() => parseMaterializationHistoryResponse({ entries: [{ ...validHistoryEntry(), outcomes: [outcome] }], next_cursor: null }), /authority/i);
  assert.throws(() => parseMaterializationHistoryResponse({ entries: [validHistoryEntry(undefined, undefined, undefined, [{ ...VALID_DRAFT, schedule_item_id: "66666666-6666-4666-8666-666666666666", current_schedule: { ...VALID_DRAFT.current_schedule, item_id: "66666666-6666-4666-8666-666666666666" } }])], next_cursor: null }), /identity.*occurrence/i);
});

test("generated Drafts require one final Proposed outcome with materialization identity", () => {
  assert.throws(() => parseMaterializationHistoryResponse({ entries: [{ ...validHistoryEntry(undefined, undefined, undefined, [VALID_DRAFT]), outcomes: [] }], next_cursor: null }), /proposed outcome/i);
});

test("history origin must agree with the reserved automatic actor", () => {
  assert.throws(() => parseMaterializationHistoryResponse({ entries: [{ ...validHistoryEntry(), origin: "automatic", actor_id: "00000000-0000-4000-8000-000000000023" }], next_cursor: null }), /origin.*actor/i);
});

test("history page merge rejects cross-page duplicates before changing the current records", () => {
  const first = parseMaterializationHistoryResponse({ entries: [validHistoryEntry("eca684f6-15eb-8983-a5c6-15ae994e5933", 2000, undefined, [VALID_DRAFT])], next_cursor: { occurred_at_unix_ms: 2000, run_id: "eca684f6-15eb-8983-a5c6-15ae994e5933" } });
  const older = parseMaterializationHistoryResponse({ entries: [validHistoryEntry("d39e0de9-f78e-8e8a-8cb5-05cd76bea837", 1000, undefined, [], "c".repeat(64))], next_cursor: { occurred_at_unix_ms: 1000, run_id: "d39e0de9-f78e-8e8a-8cb5-05cd76bea837" } });
  const merged = appendMaterializationHistoryPage(first.entries, older);
  assert.equal(merged.length, 2);
  assert.throws(() => appendMaterializationHistoryPage(first.entries, first), /repeats a run/i);
  assert.throws(() => appendMaterializationHistoryPage(first.entries, parseMaterializationHistoryResponse({ entries: [validHistoryEntry("d39e0de9-f78e-8e8a-8cb5-05cd76bea837", 1000, undefined, [VALID_DRAFT], "c".repeat(64))], next_cursor: { occurred_at_unix_ms: 1000, run_id: "d39e0de9-f78e-8e8a-8cb5-05cd76bea837" } })), /generated Draft/i);
  assert.throws(() => appendMaterializationHistoryPage(first.entries, parseMaterializationHistoryResponse({ entries: [validHistoryEntry("f6c50f03-d324-810f-8cde-f9057e966da7", 2000, undefined, [], "e".repeat(64))], next_cursor: { occurred_at_unix_ms: 2000, run_id: "f6c50f03-d324-810f-8cde-f9057e966da7" } })), /not older/i);
  assert.equal(first.entries.length, 1);
});

test("disabled channels cannot generate recurring drafts but retain readable history", () => {
  assert.equal(recurringDraftChannelState(false), "disabled");
  assert.equal(recurringDraftChannelState(true), "enabled");
});
