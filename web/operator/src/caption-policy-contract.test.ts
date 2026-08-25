// @ts-nocheck
import assert from "node:assert/strict";
import test from "node:test";
import { captionPolicyMutationReason, isCaptionLanguage } from "./caption-policy-contract.ts";

test("caption policy changes require a stopped channel and bounded service metadata", () => {
  assert.match(captionPolicyMutationReason(true, 1, "en-US") ?? "", /Stop/);
  assert.match(captionPolicyMutationReason(false, 0, "en-US") ?? "", /service/i);
  assert.match(captionPolicyMutationReason(false, 1, "not a language") ?? "", /language/i);
  assert.equal(captionPolicyMutationReason(false, 1, "en-US"), null);
});

test("caption language accepts bounded BCP-47-shaped values", () => {
  assert.equal(isCaptionLanguage("en"), true);
  assert.equal(isCaptionLanguage("en-US"), true);
  assert.equal(isCaptionLanguage("zh-Hant-TW"), true);
  assert.equal(isCaptionLanguage("en--US"), false);
  assert.equal(isCaptionLanguage("1n-US"), false);
  assert.equal(isCaptionLanguage("en US"), false);
});
