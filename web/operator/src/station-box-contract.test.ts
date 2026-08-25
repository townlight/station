// @ts-nocheck
import assert from "node:assert/strict";
import test from "node:test";

import {
  referenceQualificationLabel,
  stationBoxLabel,
  stationBoxStatus,
} from "./station-box-contract.ts";
import type { StationBoxProfile } from "./station-box-contract.ts";

function profile(matchingTiers: string[]): StationBoxProfile {
  return {
    schema_version: 2,
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
