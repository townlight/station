// @ts-nocheck
import assert from "node:assert/strict";
import test from "node:test";
import {
  canApplyHeadendProfile,
  canStartHeadendProfile,
  receiverAcceptanceLabel,
  readinessLabel,
} from "./headend-contract.ts";

test("runnable and staging-only profiles can be applied only while stopped", () => {
  assert.equal(canApplyHeadendProfile("runnable", false), true);
  assert.equal(canApplyHeadendProfile("runnable", true), false);
  assert.equal(canApplyHeadendProfile("staged_only", false), true);
  assert.equal(canApplyHeadendProfile("staged_only", true), false);
  assert.equal(canApplyHeadendProfile("unimplemented", false), false);
});

test("headend start fails closed unless the catalog says the profile is runnable", () => {
  assert.equal(canStartHeadendProfile("runnable"), true);
  assert.equal(canStartHeadendProfile("staged_only"), false);
  assert.equal(canStartHeadendProfile("unimplemented"), false);
  assert.equal(canStartHeadendProfile(null), false);
});

test("readiness labels distinguish local transport state from receiver acceptance", () => {
  assert.equal(readinessLabel("pass"), "Verified locally");
  assert.equal(readinessLabel("blocked"), "Blocked locally");
  assert.equal(readinessLabel("unknown"), "Local proof pending");
  assert.equal(receiverAcceptanceLabel("verified"), "Verified by receiver");
  assert.equal(receiverAcceptanceLabel("not_verified"), "Not verified by receiver");
  assert.equal(receiverAcceptanceLabel("unknown"), "Receiver acceptance unknown");
});
