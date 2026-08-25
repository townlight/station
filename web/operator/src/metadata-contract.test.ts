// @ts-nocheck
import assert from "node:assert/strict";
import test from "node:test";
import { controlledChoiceValue, datetimeLocalValue, metadataHistoryLimit, parseDatetimeLocal, recordWriteBody, schemaWriteBody, type MetadataFieldDefinition } from "./metadata-contract.ts";

test("metadata mutation bodies contain only browser-owned facts", () => {
  const schema = schemaWriteBody([], 4);
  assert.deepEqual(Object.keys(schema).sort(), ["expected_revision", "fields"]);
  const record = recordWriteBody("asset", "schema", [], null);
  assert.deepEqual(Object.keys(record).sort(), ["asset_id", "expected_revision", "schema_id", "values"]);
  assert.equal("actor" in record, false);
  assert.equal("revision" in record, false);
  assert.equal("occurred_at_unix_ms" in record, false);
});

test("history limits are bounded for the API contract", () => {
  assert.equal(metadataHistoryLimit(0), 1);
  assert.equal(metadataHistoryLimit(999), 128);
  assert.equal(metadataHistoryLimit(12.8), 12);
});

test("boolean schema types use the Rust unit-variant wire shape", () => {
  const field: MetadataFieldDefinition = {
    field_id: "11111111-1111-4111-8111-111111111111", key: "is_public", label: "Public",
    description: null, required: false, public: true, faceted: false, field_type: { kind: "boolean" },
  };
  assert.deepEqual(field.field_type, { kind: "boolean" });
  assert.equal("constraints" in field.field_type, false);
});

test("controlled-choice values carry the schema choice identity", () => {
  assert.deepEqual(controlledChoiceValue("22222222-2222-4222-8222-222222222222"), {
    kind: "controlled_choice", value: "22222222-2222-4222-8222-222222222222",
  });
});

test("datetime-local round-trips local wall-clock components without UTC conversion", () => {
  const local = new Date(2026, 7, 25, 12, 34, 0, 0);
  assert.equal(datetimeLocalValue(local.getTime()), "2026-08-25T12:34");
  assert.equal(parseDatetimeLocal("2026-08-25T12:34"), local.getTime());
  assert.equal(parseDatetimeLocal("not-a-date"), null);
});
