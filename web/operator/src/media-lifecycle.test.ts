// @ts-nocheck -- the production browser compilation intentionally has no Node typings.
import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { mediaLifecycleRenderState } from "./media-lifecycle-contract.ts";

const source = readFileSync(new URL("./media-lifecycle.tsx", import.meta.url), "utf8");

test("media lifecycle UI uses only asset-scoped server evidence and invalidates late reads", () => {
  assert.match(source, /requestGeneration/);
  assert.match(source, /\/api\/v1\/assets\/\$\{requestedAssetId\}\/lifecycle/);
  assert.match(source, /\/api\/v1\/assets\/\$\{asset\.asset_id\}\/thumbnail/);
  assert.match(source, /cannot upload a thumbnail or choose its output path/);
  assert.doesNotMatch(source, /thumbnail_path/);
});

test("an asset prop switch synchronously hides older authority, errors, and mutation state", () => {
  const stale = mediaLifecycleRenderState("b".repeat(64), {
    state_asset_id: "a".repeat(64),
    authority: {
      asset_id: "a".repeat(64),
      retention: null,
      replacement: { successor_asset_id: "c".repeat(64) },
      thumbnail: { thumbnail_sha256: "d".repeat(64) },
      recovery_history: [],
    },
    message: "Asset A finished late",
    busy: true,
  });
  assert.deepEqual(stale, {
    state_asset_id: "b".repeat(64),
    authority: null,
    message: null,
    busy: false,
  });
});
