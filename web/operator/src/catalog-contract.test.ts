// @ts-nocheck
import assert from "node:assert/strict";
import test from "node:test";
import {
  catalogHistoryLimit,
  catalogTransitionBody,
  catalogTransitionPath,
  catalogTextFits,
  catalogWriteBody,
  parseCatalogAssetPage,
  parseCatalogAssetRecord,
  readAllCatalogAssetPages,
} from "./catalog-contract.ts";

const record = {
  asset_sha256: "a".repeat(64), title: "Council", meeting_body: "City Council",
  editorial_state: "draft", published_at_utc_ms: null, revision: 1,
};

test("catalog mutation bodies contain no server-owned facts", () => {
  const body = catalogWriteBody(record, 1);
  assert.deepEqual(Object.keys(body).sort(), ["expected_revision", "meeting_body", "title"]);
  assert.equal("editorial_state" in body, false);
  assert.equal("asset_sha256" in body, false);
  assert.equal("revision" in body, false);
  assert.equal("published_at_utc_ms" in body, false);
  assert.equal("actor" in body, false);
  assert.equal("occurred_at_utc_ms" in body, false);
});

test("catalog lifecycle mutations use explicit action routes and revision-only bodies", () => {
  assert.equal(catalogTransitionPath(`/api/v1/catalog/assets/${record.asset_sha256}`, "publish"), `/api/v1/catalog/assets/${record.asset_sha256}/publish`);
  assert.equal(catalogTransitionPath(`/api/v1/catalog/assets/${record.asset_sha256}`, "withdraw"), `/api/v1/catalog/assets/${record.asset_sha256}/withdraw`);
  assert.deepEqual(catalogTransitionBody(7), { expected_revision: 7 });
});

test("catalog records reject unknown and semantically malformed fields", () => {
  assert.equal(parseCatalogAssetRecord(record).revision, 1);
  assert.throws(() => parseCatalogAssetRecord({ ...record, unexpected: true }));
  assert.throws(() => parseCatalogAssetRecord({ ...record, asset_sha256: "not-a-sha" }));
  assert.throws(() => parseCatalogAssetRecord({ ...record, revision: 0 }));
});

test("catalog pages reject forged continuation cursors", () => {
  assert.equal(parseCatalogAssetPage({ records: [record], next_after_asset_sha256: "b".repeat(64) }).records.length, 1);
  assert.throws(() => parseCatalogAssetPage({ records: [record], next_after_asset_sha256: "../next" }));
});

test("catalog pagination follows every canonical cursor without duplication", async () => {
  const second = { ...record, asset_sha256: "b".repeat(64) };
  const paths: string[] = [];
  const records = await readAllCatalogAssetPages(async (path) => {
    paths.push(path);
    return paths.length === 1
      ? { records: [record], next_after_asset_sha256: record.asset_sha256 }
      : { records: [second], next_after_asset_sha256: null };
  });
  assert.deepEqual(paths, [
    "/api/v1/catalog/assets?limit=1000",
    `/api/v1/catalog/assets?after_asset_sha256=${record.asset_sha256}&limit=1000`,
  ]);
  assert.deepEqual(records.map((item) => item.asset_sha256), [record.asset_sha256, second.asset_sha256]);
  await assert.rejects(() => readAllCatalogAssetPages(async () => ({ records: [record], next_after_asset_sha256: record.asset_sha256 })));
});

test("catalog history limits stay bounded", () => {
  assert.equal(catalogHistoryLimit(0), 1);
  assert.equal(catalogHistoryLimit(900), 128);
  assert.equal(catalogHistoryLimit(12.8), 12);
});

test("catalog text limits measure UTF-8 bytes rather than UTF-16 units", () => {
  assert.equal(catalogTextFits("x".repeat(240)), true);
  assert.equal(catalogTextFits("é".repeat(120)), true);
  assert.equal(catalogTextFits("é".repeat(121)), false);
});
