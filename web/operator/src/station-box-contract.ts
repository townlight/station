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
  reference_station_tier_selection: {
    catalog_version: number;
    tier: string;
    revision: number;
    created_at_unix_ms: number;
    updated_at_unix_ms: number;
    updated_by: string;
  } | null;
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

export interface ReferenceTierSelectionPayload {
  expected_revision: number | null;
  catalog_version: number;
  tier: string;
}

export class LatestStationBoxLoad {
  private latestRequest = 0;

  begin(): number {
    this.latestRequest += 1;
    return this.latestRequest;
  }

  accepts(request: number): boolean {
    return request === this.latestRequest;
  }

  invalidate(): void {
    this.latestRequest += 1;
  }
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

export function referenceTierSelectionPayload(
  profile: StationBoxProfile,
  tier: string,
): ReferenceTierSelectionPayload {
  return {
    expected_revision: profile.reference_station_tier_selection?.revision ?? null,
    catalog_version: profile.reference_station_catalog.version,
    tier,
  };
}

export function referenceTierSelectionIsCurrent(
  profile: StationBoxProfile,
  tier: string,
): boolean {
  const selection = profile.reference_station_tier_selection;
  return selection?.tier === tier
    && selection.catalog_version === profile.reference_station_catalog.version;
}
