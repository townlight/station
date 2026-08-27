// @ts-nocheck
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  asRunExportPath,
  asRunPagePath,
  mergeAsRunPages,
  parseAsRunProjectionPage,
  reportingChannelRenderState,
} from "./reporting-contract.ts";

const CHANNEL_ID = "00000000-0000-4000-8000-000000000001";

function page(overrides = {}) {
  return {
    channel_id: CHANNEL_ID,
    station_timezone: "America/Denver",
    projection_window: {
      local_date: "2026-08-27",
      starts_at_utc_ms: 1_787_810_400_000,
      ends_at_utc_ms: 1_787_896_800_000,
    },
    source_journal_id: "station-worker-33333333-3333-4333-8333-333333333333",
    source_file_identity: "windows-file-id:0000000000000001:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    source_raw_sequence: 14,
    source_checkpoint_revision: 3,
    entries: [{
      event_id: "11111111-1111-4111-8111-111111111111",
      journal_sequence: 12,
      started_at_unix_ms: 1_787_810_400_000,
      ended_at_unix_ms: 1_787_814_000_000,
      state: "program",
      content_id: "asset-1",
      franchise_id: "franchise-1",
      category: "Council",
      title: "Regular meeting",
      catalog_revision: 2,
      schedule_revision: 4,
      journal_hash: "a".repeat(64),
      unavailable_reason: null,
    }],
    next_cursor: "opaque_next",
    checkpoint_sequence: 14,
    ...overrides,
  };
}

test("reporting paths are channel-scoped, bounded, and encoded", () => {
  assert.equal(asRunPagePath(CHANNEL_ID, 100), `/api/v1/reporting/as-run/${CHANNEL_ID}?limit=100`);
  assert.equal(asRunPagePath(CHANNEL_ID, 100, "opaque_abc-123"), `/api/v1/reporting/as-run/${CHANNEL_ID}?limit=100&cursor=opaque_abc-123`);
  assert.equal(asRunPagePath(CHANNEL_ID, 100, "opaque+/="), null);
  assert.equal(asRunPagePath("bad", 100), null);
  assert.equal(asRunPagePath(CHANNEL_ID, 0), null);
  assert.equal(asRunPagePath(CHANNEL_ID, 201), null);
  assert.equal(asRunExportPath(CHANNEL_ID, "csv"), `/api/v1/reporting/as-run/${CHANNEL_ID}/export.csv`);
  assert.equal(asRunExportPath(CHANNEL_ID, "xmltv"), `/api/v1/reporting/as-run/${CHANNEL_ID}/export.xmltv`);
  assert.equal(asRunExportPath(CHANNEL_ID, "pdf"), null);
});

test("projection pages reject forged or malformed authority", () => {
  assert.equal(parseAsRunProjectionPage(page()).entries.length, 1);
  assert.throws(() => parseAsRunProjectionPage(page({ unexpected: true })), /shape/i);
  assert.throws(() => parseAsRunProjectionPage(page({ channel_id: "BAD" })), /channel/i);
  assert.throws(() => parseAsRunProjectionPage(page({ source_raw_sequence: 0 })), /sequence/i);
  assert.throws(() => parseAsRunProjectionPage(page({ source_journal_id: "journal-1" })), /source/i);
  assert.throws(() => parseAsRunProjectionPage(page({ projection_window: { ...page().projection_window, ends_at_utc_ms: 1 } })), /window/i);
  assert.throws(() => parseAsRunProjectionPage(page({ station_timezone: "Not/A_Zone" })), /timezone/i);
  assert.throws(() => parseAsRunProjectionPage(page({ projection_window: { ...page().projection_window, local_date: "2026-02-31" } })), /window/i);
  assert.throws(() => parseAsRunProjectionPage(page({ projection_window: { ...page().projection_window, starts_at_utc_ms: 1_787_810_400_001 } })), /window/i);
  assert.doesNotThrow(() => parseAsRunProjectionPage(page({ entries: [{ ...page().entries[0], journal_hash: "A".repeat(64) }] })));
  assert.throws(() => parseAsRunProjectionPage(page({ entries: [{ ...page().entries[0], journal_hash: "g".repeat(64) }] })), /entry/i);
  assert.throws(() => parseAsRunProjectionPage(page({ entries: [{ ...page().entries[0], state: "live", unavailable_reason: null }] })), /unavailable/i);
  assert.throws(() => parseAsRunProjectionPage(page({ entries: [{ ...page().entries[0], franchise_id: null, unavailable_reason: null }] })), /unavailable/i);
  assert.throws(() => parseAsRunProjectionPage(page({ entries: [{ ...page().entries[0], title: "😀".repeat(61) }] })), /entry/i);
  assert.throws(() => parseAsRunProjectionPage(page({ entries: [{ ...page().entries[0], title: "Council\u0085meeting" }] })), /entry/i);
  assert.doesNotThrow(() => parseAsRunProjectionPage(page({ source_raw_sequence: 0, source_checkpoint_revision: 0, source_file_identity: "" })));
});

test("pagination cannot mix projections or duplicate event identities", () => {
  const first = parseAsRunProjectionPage(page());
  const second = parseAsRunProjectionPage(page({
    entries: [{ ...page().entries[0], event_id: "22222222-2222-4222-8222-222222222222", journal_sequence: 13, started_at_unix_ms: 1_787_814_000_000, ended_at_unix_ms: 1_787_817_600_000 }],
    next_cursor: null,
    checkpoint_sequence: 14,
  }));
  assert.equal(mergeAsRunPages(first, second).entries.length, 2);
  assert.throws(() => mergeAsRunPages(first, parseAsRunProjectionPage(page({ source_checkpoint_revision: 4 }))), /projection/i);
  assert.throws(() => mergeAsRunPages(first, parseAsRunProjectionPage(page({ checkpoint_sequence: 13 }))), /projection/i);
  assert.throws(() => mergeAsRunPages(first, parseAsRunProjectionPage(page({ next_cursor: null }))), /duplicate/i);
  assert.throws(() => mergeAsRunPages(first, parseAsRunProjectionPage(page({
    entries: [{ ...page().entries[0], event_id: "22222222-2222-4222-8222-222222222222", journal_sequence: 13, started_at_unix_ms: 1_787_814_000_001, ended_at_unix_ms: 1_787_817_600_000 }],
    next_cursor: null,
    checkpoint_sequence: 14,
  }))), /ordered/i);
});

test("operator workflow keeps evidence and delivery claims distinct", () => {
  const source = readFileSync(new URL("./reporting.tsx", import.meta.url), "utf8");
  assert.match(source, /Authoritative journal projection/);
  assert.match(source, /does not prove receiver delivery/i);
  assert.match(source, /Download CSV/);
  assert.match(source, /Download XMLTV/);
  assert.match(source, /<caption>/);
  assert.match(source, /scope="col"/);
  assert.match(source, /tabIndex=\{0\}/);
});

test("reporting is a durable deep-linkable core view", () => {
  const shell = readFileSync(new URL("./main.tsx", import.meta.url), "utf8");
  assert.match(shell, /\["overview", "media", "channels", "schedule", "live", "automation", "publishing", "metadata", "recordings", "reporting", "health"/);
});

test("a channel prop switch hides stale authority before effects and invalidates late work", () => {
  const stale = reportingChannelRenderState("channel-b", {
    state_channel_id: CHANNEL_ID,
    page: parseAsRunProjectionPage(page()),
    busy: "more",
    error: "channel A failed",
  });
  assert.equal(stale.state_channel_id, "channel-b");
  assert.equal(stale.page, null);
  assert.equal(stale.busy, null);
  assert.equal(stale.error, null);

  const source = readFileSync(new URL("./reporting.tsx", import.meta.url), "utf8");
  assert.match(source, /activeChannelId\.current !== renderedChannelId/);
  assert.match(source, /requestGeneration\.current \+= 1/);
});
