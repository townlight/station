// @ts-nocheck -- the production browser compilation intentionally has no Node typings.
import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { assetOperationStillOwns, mediaLifecycleDraftCanSubmit, mediaLifecycleDraftForAsset, mediaLifecycleRenderState } from "./media-lifecycle-contract.ts";

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

test("a late asset mutation cannot write, reload, or clear the next asset operation", async () => {
  let currentAssetId = "a".repeat(64);
  let currentGeneration = 4;
  const owner = { asset_id: currentAssetId, generation: currentGeneration };
  const writes: string[] = [];
  let release!: () => void;
  const response = new Promise<void>((resolve) => { release = resolve; });

  const lateMutation = (async () => {
    await response;
    if (!assetOperationStillOwns(owner, currentAssetId, currentGeneration)) return;
    writes.push("message", "reload", "clear-busy");
  })();
  currentAssetId = "b".repeat(64);
  currentGeneration += 1;
  writes.push("b-busy");
  release();
  await lateMutation;

  assert.deepEqual(writes, ["b-busy"]);
  assert.match(source, /if \(!owns\(owner\)\) return;/);
  assert.match(source, /finally \{ if \(owns\(owner\)\) setBusy\(false\); \}/);
});

test("an asset switch clears every prior draft field and refuses submission until the new draft is owned", () => {
  const assetA = "a".repeat(64);
  const assetB = "b".repeat(64);
  const staleDraft = {
    draft_asset_id: assetA,
    source_path: "C:\\private\\asset-a.ts",
    review_after: "1893456000000",
    permanent: true,
    legal_hold: true,
  };
  const renderedForB = mediaLifecycleDraftForAsset(assetB, staleDraft);

  assert.deepEqual(renderedForB, {
    draft_asset_id: assetB,
    source_path: "",
    review_after: "",
    permanent: false,
    legal_hold: false,
  });
  assert.equal(mediaLifecycleDraftCanSubmit(assetB, staleDraft, { asset_id: assetB }), false);
  assert.equal(mediaLifecycleDraftCanSubmit(assetB, renderedForB, null), false);
  assert.equal(mediaLifecycleDraftCanSubmit(assetB, renderedForB, { asset_id: assetB }), true);
  assert.match(source, /if \(!mediaLifecycleDraftCanSubmit\(asset\.asset_id,/);
  assert.match(source, /disabled=\{!formCanSubmit\}/);
});
