export type AutomationFeature = "captions" | "summaries" | "translation";
export type AutomationStatus =
  | "selection_missing"
  | "invalid_configuration"
  | "credential_unavailable"
  | "not_tested"
  | "invalid_evidence"
  | "stale_configuration"
  | "stale_candidate"
  | "expired"
  | "passed"
  | "failed";

export interface AutomationAdapterDescriptor {
  adapter_id: string;
  location: "local" | "cloud";
  model_key: string;
  model_identifier: string;
  supported_features: AutomationFeature[];
  credential_availability: "available" | "unavailable" | "not_required";
  revision: number;
}

export interface AutomationFeaturePolicy {
  policy_id: string;
  feature: AutomationFeature;
  operator_selected_adapter_id: string | null;
  adaptive_default: { preferred_adapter_id: string; fallback_adapter_id: string } | null;
  configuration_hash: string;
  revision: number;
}

export interface AutomationConnectionEvidence {
  evidence_id: string;
  tested_at_unix_ms: number;
  expires_at_unix_ms: number;
  outcome: "passed" | "failed";
  detail: string;
}

export interface AutomationPolicyStatus {
  policy: AutomationFeaturePolicy;
  status: AutomationStatus;
  evidence: AutomationConnectionEvidence | null;
}

export const AUTOMATION_FEATURES: AutomationFeature[] = ["captions", "summaries", "translation"];

export function automationFeatureLabel(feature: AutomationFeature): string {
  return { captions: "Live captions", summaries: "Meeting summaries", translation: "Translation" }[feature];
}

export function automationStatusLabel(status: AutomationStatus): string {
  return {
    selection_missing: "Adapter selection required",
    invalid_configuration: "Configuration invalid",
    credential_unavailable: "Credential unavailable",
    not_tested: "Connection not tested",
    invalid_evidence: "Evidence invalid",
    stale_configuration: "Retest after configuration change",
    stale_candidate: "Retest after software update",
    expired: "Connection evidence expired",
    passed: "Connection verified",
    failed: "Connection test failed",
  }[status];
}

export function adaptersForFeature(
  adapters: AutomationAdapterDescriptor[],
  feature: AutomationFeature,
): AutomationAdapterDescriptor[] {
  return adapters
    .filter((adapter) => adapter.supported_features.includes(feature))
    .sort((left, right) => left.model_key.localeCompare(right.model_key) || left.adapter_id.localeCompare(right.adapter_id));
}

export function automationPolicyMutationReason(
  adapters: AutomationAdapterDescriptor[],
  feature: AutomationFeature,
  selectedAdapterId: string,
  configurationHash: string,
): string | null {
  const adapter = adapters.find((item) => item.adapter_id === selectedAdapterId);
  if (!adapter || !adapter.supported_features.includes(feature)) return "Select an adapter that supports this feature.";
  if (adapter.credential_availability === "unavailable") return "The selected adapter has no available credential.";
  if (!/^[0-9a-f]{64}$/.test(configurationHash)) return "Configuration authority must be a lowercase SHA-256 hash.";
  return null;
}
