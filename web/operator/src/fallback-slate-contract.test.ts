// @ts-nocheck
import assert from "node:assert/strict";
import test from "node:test";
import { fallbackSlateMutationReason } from "./fallback-slate-contract.ts";

test("fallback slate changes require a stopped channel and ready managed image", () => {
  assert.match(fallbackSlateMutationReason(true, "ready") ?? "", /Stop/);
  assert.match(fallbackSlateMutationReason(false, "missing") ?? "", /verified/);
  assert.equal(fallbackSlateMutationReason(false, "ready"), null);
});
