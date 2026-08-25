// @ts-nocheck
import assert from "node:assert/strict";
import test from "node:test";

import {
  LatestStationBoxLoad,
  referenceQualificationLabel,
  referenceTierSelectionIsCurrent,
  referenceTierSelectionPayload,
  stationBoxLabel,
  stationBoxStatus,
} from "./station-box-contract.ts";
import type { StationBoxProfile } from "./station-box-contract.ts";

function profile(matchingTiers: string[]): StationBoxProfile {
  return {
    schema_version: 3,
    reference_station_tier_selection: null,
    hardware: {
      logical_cpus: 32,
      ram_total_gb: 64,
      media_disk_free_gb: 1_000,
      state: "pass",
    },
    engine_tier_verdict: "full_broadcast",
    reference_station_catalog: {
      version: 1,
      matching_tiers: matchingTiers,
      assessments: [],
    },
    peg_readiness: { color: "yellow", dimensions: [] },
  };
}

test("engine capability does not fabricate reference-station qualification", () => {
  const stationBox = profile([]);

  assert.equal(stationBox.engine_tier_verdict, "full_broadcast");
  assert.equal(referenceQualificationLabel(stationBox), "No complete build match");
});

test("reference-station labels and optional state are operator readable", () => {
  assert.equal(referenceQualificationLabel(profile(["cable_sdi"])), "Build matches: Cable / SDI");
  assert.equal(stationBoxLabel("tier2_multi_stream"), "Tier 2 multi-stream");
  assert.equal(stationBoxStatus("optional"), "pending");
});

test("tier selection payload carries only CAS intent and catalog identity", () => {
  const stationBox = profile([]);
  const first = referenceTierSelectionPayload(stationBox, "tier1_streaming");
  assert.deepEqual(first, {
    expected_revision: null,
    catalog_version: 1,
    tier: "tier1_streaming",
  });
  assert.equal("actor" in first, false);
  assert.equal("occurred_at_unix_ms" in first, false);

  stationBox.reference_station_tier_selection = {
    catalog_version: 1,
    tier: "tier0_batch_stream",
    revision: 7,
    created_at_unix_ms: 1,
    updated_at_unix_ms: 2,
    updated_by: "server-account-id",
  };
  assert.equal(
    referenceTierSelectionPayload(stationBox, "cable_sdi").expected_revision,
    7,
  );
  assert.equal(referenceTierSelectionIsCurrent(stationBox, "tier0_batch_stream"), true);
  stationBox.reference_station_tier_selection.catalog_version = 0;
  assert.equal(
    referenceTierSelectionIsCurrent(stationBox, "tier0_batch_stream"),
    false,
    "the same tier from an old catalog must remain selectable",
  );
});

test("only the newest overlapping StationBox load may update operator state", async () => {
  const gate = new LatestStationBoxLoad();
  const accepted: string[] = [];
  let resolveOlder!: (value: string) => void;
  let resolveNewer!: (value: string) => void;
  const older = new Promise<string>((resolve) => { resolveOlder = resolve; });
  const newer = new Promise<string>((resolve) => { resolveNewer = resolve; });
  const load = async (request: Promise<string>) => {
    const sequence = gate.begin();
    const value = await request;
    if (gate.accepts(sequence)) accepted.push(value);
  };

  const olderLoad = load(older);
  const newerLoad = load(newer);
  resolveNewer("revision-2");
  await newerLoad;
  resolveOlder("revision-1");
  await olderLoad;

  assert.deepEqual(accepted, ["revision-2"]);
});
