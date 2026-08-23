// @ts-nocheck
import assert from "node:assert/strict";
import test from "node:test";
import {
  MAX_CAPTURE_BYTES,
  MAX_SAMPLE_DURATION_MS,
  MIN_SAMPLE_DURATION_MS,
  isTerminalLoudnessProofState,
  validateLoudnessProofRequest,
} from "./loudness-proof-contract.ts";

test("proof request accepts the documented inclusive bounds", () => {
  assert.equal(validateLoudnessProofRequest(MIN_SAMPLE_DURATION_MS, 1), null);
  assert.equal(validateLoudnessProofRequest(MAX_SAMPLE_DURATION_MS, MAX_CAPTURE_BYTES), null);
});

test("proof request rejects unbounded or too-short capture settings", () => {
  assert.match(validateLoudnessProofRequest(MIN_SAMPLE_DURATION_MS - 1, 1), /Sample duration/);
  assert.match(validateLoudnessProofRequest(MAX_SAMPLE_DURATION_MS + 1, 1), /Sample duration/);
  assert.match(validateLoudnessProofRequest(MIN_SAMPLE_DURATION_MS, MAX_CAPTURE_BYTES + 1), /Capture limit/);
});

test("only completed and failed jobs are terminal", () => {
  for (const state of ["pending", "claimed", "capture_pending", "analyzing"]) {
    assert.equal(isTerminalLoudnessProofState(state), false);
  }
  assert.equal(isTerminalLoudnessProofState("completed"), true);
  assert.equal(isTerminalLoudnessProofState("failed"), true);
});
