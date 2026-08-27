// @ts-nocheck
import assert from "node:assert/strict";
import test from "node:test";
import { parsePublicCatalogPage } from "./public-catalog-contract.ts";
const item = { asset_id: "a".repeat(64), title: "Council", published_at_utc_ms: 1, duration_ms: 1000, facets: [{ field_key: "topic", value: { kind: "text", value: "budget" } }] };
test("public catalog parser accepts exact bounded redacted data", () => assert.equal(parsePublicCatalogPage({ items: [item], next_cursor: null }).items.length, 1));
test("public catalog parser rejects internal fields and duplicate identities", () => {
  assert.throws(() => parsePublicCatalogPage({ items: [{ ...item, media_path: "secret" }], next_cursor: null }));
  assert.throws(() => parsePublicCatalogPage({ items: [item, item], next_cursor: null }));
});
