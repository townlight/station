// @ts-nocheck
import assert from "node:assert/strict";
import test from "node:test";
import {
  cancelScheduledRecordingBody,
  createScheduledRecordingBody,
  recordingExpectedRevisionBody,
  canArmRecording,
  canCancelRecording,
  recordingHistoryLimit,
  recordingLifecycleKind,
  isFutureCommittedScheduleItem,
  scheduleCandidateRefreshDelay,
  scheduledRecordingCandidatesPath,
} from "./recordings-contract.ts";

test("recording creation cannot forge schedule facts or server attribution", () => {
  const body = createScheduledRecordingBody("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222");
  assert.deepEqual(Object.keys(body).sort(), ["recording_id", "schedule_item_id"]);
  assert.equal("channel_id" in body, false);
  assert.equal("schedule_revision" in body, false);
  assert.equal("scheduled_start_unix_ms" in body, false);
  assert.equal("requested_at_unix_ms" in body, false);
});

test("arm and cancel carry only CAS intent and an explicit bounded reason", () => {
  assert.deepEqual(recordingExpectedRevisionBody(3), { expected_revision: 3 });
  assert.deepEqual(cancelScheduledRecordingBody(3, "Operator cancelled before air."), { expected_revision: 3, reason: "Operator cancelled before air." });
  assert.equal("actor" in cancelScheduledRecordingBody(3, "x"), false);
  assert.equal("event" in recordingExpectedRevisionBody(3), false);
});

test("recording history limits remain bounded", () => {
  assert.equal(recordingHistoryLimit(0), 1);
  assert.equal(recordingHistoryLimit(999), 128);
  assert.equal(recordingHistoryLimit(12.8), 12);
});

const draft = (lifecycle: string | Record<string, unknown>, start = 10_000) => ({ definition: { scheduled_start_unix_ms: start }, lifecycle, revision: 1 });
test("externally tagged lifecycle and pre-start action gates are fail-closed", () => {
  assert.equal(recordingLifecycleKind("draft"), "draft");
  assert.equal(recordingLifecycleKind({ armed: { armed_at_unix_ms: 1 } }), "armed");
  assert.equal(canArmRecording(draft("draft"), 9_999), true);
  assert.equal(canArmRecording(draft("draft"), 10_000), false);
  assert.equal(canArmRecording(draft({ armed: {} }), 1), false);
  assert.equal(canCancelRecording(draft("draft"), 9_999), true);
  assert.equal(canCancelRecording(draft({ armed: {} }), 9_999), true);
  assert.equal(canCancelRecording(draft({ capturing: {} }), 1), false);
  assert.equal(canCancelRecording(draft("draft"), 10_000), false);
});

test("candidate schedule endpoint is exact, encoded, and bounded", () => {
  assert.equal(
    scheduledRecordingCandidatesPath("11111111-1111-4111-8111-111111111111", 0),
    "/api/v1/scheduled-recordings/candidates?channel_id=11111111-1111-4111-8111-111111111111&limit=1",
  );
  assert.equal(scheduledRecordingCandidatesPath("channel/with spaces?", 999), "/api/v1/scheduled-recordings/candidates?channel_id=channel%2Fwith%20spaces%3F&limit=128");
});

test("candidate aging is strict and refresh delay is bounded", () => {
  const item = { state: "committed", starts_at_unix_ms: 10_000 };
  assert.equal(isFutureCommittedScheduleItem(item, 9_999), true);
  assert.equal(isFutureCommittedScheduleItem(item, 10_000), false);
  assert.equal(isFutureCommittedScheduleItem({ ...item, state: "draft" }, 1), false);
  assert.equal(scheduleCandidateRefreshDelay(item, 9_000, 500), 500);
  assert.equal(scheduleCandidateRefreshDelay(item, 10_000, 500), 0);
});
