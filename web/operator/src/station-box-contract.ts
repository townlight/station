import type { CommissioningStatus } from "./commissioning-contract";

export type StationBoxReadinessState = "pass" | "blocked" | "recoverable" | "optional" | "unknown";

export interface StationBoxDimension {
  name: string;
  state: StationBoxReadinessState;
  required: boolean;
  detail: string;
  evidence: string;
  next_step: string | null;
}

export interface ReferenceStationRequirement {
  name: string;
  state: StationBoxReadinessState;
  detail: string;
}

export interface ReferenceStationTierAssessment {
  tier: string;
  state: StationBoxReadinessState;
  requirements: ReferenceStationRequirement[];
  detail: string;
  next_step: string | null;
}

export interface StationBoxProfile {
  schema_version: number;
  hardware: {
    logical_cpus: number;
    ram_total_gb: number;
    media_disk_free_gb: number;
    state: StationBoxReadinessState;
  };
  engine_tier_verdict: string;
  reference_station_catalog: {
    version: number;
    matching_tiers: string[];
    assessments: ReferenceStationTierAssessment[];
  };
  peg_readiness: {
    color: "green" | "yellow" | "red";
    dimensions: StationBoxDimension[];
  };
}

export function stationBoxStatus(state: StationBoxReadinessState): CommissioningStatus {
  return state === "optional" ? "pending" : state;
}

export function stationBoxLabel(value: string): string {
  const referenceLabels: Record<string, string> = {
    tier0_batch_stream: "Tier 0 batch / stream",
    tier1_streaming: "Tier 1 streaming",
    development_validation: "Development / validation",
    cable_sdi: "Cable / SDI",
    tier2_multi_stream: "Tier 2 multi-stream",
  };
  if (referenceLabels[value]) return referenceLabels[value];
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function referenceQualificationLabel(profile: StationBoxProfile): string {
  const tiers = profile.reference_station_catalog.matching_tiers;
  return tiers.length > 0
    ? `Build matches: ${tiers.map(stationBoxLabel).join(", ")}`
    : "No complete build match";
}
