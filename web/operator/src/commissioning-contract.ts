export type CommissioningStatus = "pass" | "warning" | "pending" | "blocked" | "not_verified" | "unknown" | "recoverable";
export type CommissioningPhase = "identity" | "station_box" | "sources_outputs" | "guided_air_proof" | "external_proof";
export type CommissioningAction = "station" | "health" | "media" | "channels" | "schedule" | "publishing" | "automation";

export interface AuthoritativeCommissioningCheck {
  id: string;
  label: string;
  status: CommissioningStatus;
  blocking: boolean;
  detail: string;
  evidence: string;
  phase?: CommissioningPhase;
  required?: boolean;
  boundary?: "local" | "external" | "local_authority" | "external_evidence";
  action?: CommissioningAction | null;
  next_step?: string | null;
}

export interface AuthoritativeCommissioningReport {
  schema_version?: number;
  generated_at_unix_ms?: number;
  overall_status: "pass" | "warning" | "blocked";
  current_step_id?: string | null;
  core_progress?: { passed: number; total: number };
  core_complete?: boolean;
  checks: AuthoritativeCommissioningCheck[];
}

export function isCommissioningComplete(report: AuthoritativeCommissioningReport): boolean {
  return report.overall_status === "pass";
}

export function isCoreCommissioningComplete(report: AuthoritativeCommissioningReport): boolean {
  return report.core_complete === true;
}

export function currentCommissioningCheck(report: AuthoritativeCommissioningReport): AuthoritativeCommissioningCheck | null {
  if (report.current_step_id) return report.checks.find((check) => check.id === report.current_step_id) ?? null;
  return report.checks.find((check) => check.blocking && check.status !== "pass") ?? null;
}

const FALLBACK_ACTIONS: Record<string, CommissioningAction> = {
  station_profile: "station",
  runtime_api: "health",
  station_box_storage: "health",
  station_time: "health",
  station_backup: "health",
  watch_folder: "media",
  channel_watch_folder: "media",
  enabled_channel: "channels",
  fallback_slate: "channels",
  cea708_captions: "channels",
  publish_targets: "publishing",
  automation_model_policy: "automation",
  headend_output_setup: "channels",
  cable_transport_validation: "health",
  ready_media: "media",
  guided_ingest: "media",
  schedule_authority: "schedule",
  guided_schedule: "schedule",
  dispatch_execution: "schedule",
  guided_air_proof: "schedule",
  selected_sink_ten_minute_proof: "health",
  support_bundle: "health",
  first_broadcast_report: "schedule",
};

export function commissioningAction(check: AuthoritativeCommissioningCheck): CommissioningAction | null {
  if (check.action !== undefined) return check.action;
  return FALLBACK_ACTIONS[check.id] ?? null;
}

export function isExternalCommissioningCheck(check: AuthoritativeCommissioningCheck): boolean {
  return check.boundary === "external"
    || check.boundary === "external_evidence"
    || check.phase === "external_proof"
    || ["external_receiver_proof", "installed_field_proof", "clean_machine_install_proof", "beta_soak_proof"].includes(check.id);
}

const PHASE_LABELS: Record<CommissioningPhase, string> = {
  identity: "Station identity",
  station_box: "StationBox & resilience",
  sources_outputs: "Sources & outputs",
  guided_air_proof: "Guided first-air proof",
  external_proof: "External acceptance & beta proof",
};

export interface CommissioningGroup {
  id: CommissioningPhase | "authoritative_checks";
  label: string;
  checks: AuthoritativeCommissioningCheck[];
}

export type CommissioningWizardStepId =
  | "profile"
  | "hardware"
  | "storage"
  | "operator_account"
  | "publish_targets"
  | "models_automation"
  | "health"
  | "cable_checks"
  | "channel_output"
  | "output_proof"
  | "commissioning_report";

export interface CommissioningWizardStepDefinition {
  id: CommissioningWizardStepId;
  number: number;
  title: string;
  purpose: string;
  checkIds: readonly string[];
}

export interface CommissioningWizardStep extends CommissioningWizardStepDefinition {
  checks: AuthoritativeCommissioningCheck[];
  passed: number;
  status: CommissioningStatus;
  current: boolean;
}

export const COMMISSIONING_WIZARD_STEPS: readonly CommissioningWizardStepDefinition[] = [
  { id: "profile", number: 1, title: "Station profile", purpose: "Establish the durable station identity and local timezone.", checkIds: ["station_profile"] },
  { id: "hardware", number: 2, title: "Hardware probe & tier", purpose: "Verify the running station authority, installed engine, and measured StationBox resources.", checkIds: ["runtime_api", "station_box_storage"] },
  { id: "storage", number: 3, title: "Storage & backup", purpose: "Prove that station backup storage is currently writable and recoverable.", checkIds: ["station_backup"] },
  { id: "operator_account", number: 4, title: "Operator account", purpose: "Require an enabled setup administrator and current authenticated session.", checkIds: ["operator_account_auth"] },
  { id: "publish_targets", number: 5, title: "Publish targets", purpose: "Verify required portal, permanence, and reach-target connection authority.", checkIds: ["publish_targets"] },
  { id: "models_automation", number: 6, title: "Models & automation", purpose: "Verify explicit automation policies and current connection evidence without claiming model output quality.", checkIds: ["automation_model_policy"] },
  { id: "health", number: 7, title: "First health check", purpose: "Verify synchronized station time as a required first-run health condition.", checkIds: ["station_time"] },
  { id: "cable_checks", number: 8, title: "First-run cable checks", purpose: "Require current-candidate transport analysis and target-device reachability evidence.", checkIds: ["cable_transport_validation"] },
  { id: "channel_output", number: 9, title: "Channel output setup", purpose: "Configure ingest, fallback, captions, and the named required headend output.", checkIds: ["channel_watch_folder", "fallback_slate", "cea708_captions", "headend_output_setup"] },
  { id: "output_proof", number: 10, title: "Output proof", purpose: "Carry real managed media through schedule, worker acknowledgement, selected-sink capture, and receiver acceptance.", checkIds: ["guided_ingest", "guided_schedule", "guided_air_proof", "selected_sink_ten_minute_proof", "external_receiver_proof"] },
  { id: "commissioning_report", number: 11, title: "Commissioning report", purpose: "Preserve the support bundle, first-broadcast receipt, clean-install evidence, and exact-candidate four-to-eight-hour beta-soak gate.", checkIds: ["support_bundle", "first_broadcast_report", "clean_machine_install_proof", "beta_soak_proof"] },
] as const;

function wizardStepStatus(checks: AuthoritativeCommissioningCheck[], expectedCount: number): CommissioningStatus {
  if (checks.length !== expectedCount) return "unknown";
  if (checks.every((check) => check.status === "pass")) return "pass";
  if (checks.some((check) => check.status === "blocked")) return "blocked";
  if (checks.some((check) => check.status === "unknown" || check.status === "not_verified")) return "unknown";
  if (checks.some((check) => check.status === "warning" || check.status === "recoverable")) return "warning";
  return "pending";
}

export function commissioningWizardCoverage(report: AuthoritativeCommissioningReport): { unmapped: string[]; duplicated: string[]; missing: string[] } {
  const assignments = new Map<string, number>();
  const reported = new Map<string, number>();
  for (const step of COMMISSIONING_WIZARD_STEPS) {
    for (const checkId of step.checkIds) assignments.set(checkId, (assignments.get(checkId) ?? 0) + 1);
  }
  for (const check of report.checks) reported.set(check.id, (reported.get(check.id) ?? 0) + 1);
  return {
    unmapped: report.checks.map((check) => check.id).filter((id) => !assignments.has(id)),
    duplicated: [...new Set(report.checks.map((check) => check.id).filter((id) => (assignments.get(id) ?? 0) > 1 || (reported.get(id) ?? 0) > 1))],
    missing: [...assignments.keys()].filter((id) => !report.checks.some((check) => check.id === id)),
  };
}

export function commissioningWizardSteps(report: AuthoritativeCommissioningReport): CommissioningWizardStep[] {
  return COMMISSIONING_WIZARD_STEPS.map((definition) => {
    const checks = definition.checkIds.flatMap((id) => report.checks.filter((candidate) => candidate.id === id));
    return {
      ...definition,
      checks,
      passed: checks.filter((check) => check.status === "pass").length,
      status: wizardStepStatus(checks, definition.checkIds.length),
      current: Boolean(report.current_step_id && definition.checkIds.includes(report.current_step_id)),
    };
  });
}

export function commissioningGroups(report: AuthoritativeCommissioningReport): CommissioningGroup[] {
  const groups: CommissioningGroup[] = [];
  for (const check of report.checks) {
    const id = check.phase ?? "authoritative_checks";
    let group = groups.find((item) => item.id === id);
    if (!group) {
      group = { id, label: id === "authoritative_checks" ? "Authoritative station checks" : PHASE_LABELS[id], checks: [] };
      groups.push(group);
    }
    group.checks.push(check);
  }
  return groups;
}

export function commissioningStatusLabel(status: CommissioningStatus): string {
  switch (status) {
    case "pass": return "Verified";
    case "warning": return "Warning";
    case "pending": return "Pending";
    case "blocked": return "Blocked";
    case "not_verified": return "Not verified";
    case "unknown": return "Unknown — verification required";
    case "recoverable": return "Recoverable gap";
  }
}
