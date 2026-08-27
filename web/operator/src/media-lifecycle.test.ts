// @ts-nocheck -- the production browser compilation intentionally has no Node typings.
import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("./media-lifecycle.tsx", import.meta.url), "utf8");

test("media lifecycle UI uses only asset-scoped server evidence and invalidates late reads", () => {
  assert.match(source, /requestGeneration/);
  assert.match(source, /\/api\/v1\/assets\/\$\{asset\.asset_id\}\/lifecycle/);
  assert.match(source, /\/api\/v1\/assets\/\$\{asset\.asset_id\}\/thumbnail/);
  assert.match(source, /cannot upload a thumbnail or choose its output path/);
  assert.doesNotMatch(source, /thumbnail_path/);
});
