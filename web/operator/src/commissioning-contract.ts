export type CommissioningStatus = "pass" | "warning" | "pending" | "blocked" | "not_verified" | "unknown" | "recoverable";
export type CommissioningPhase = "identity" | "station_box" | "sources_outputs" | "guided_air_proof" | "external_proof";
export type CommissioningAction = "station" | "health" | "media" | "channels" | "schedule";

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
  publish_targets: "channels",
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
    || ["external_receiver_proof", "installed_field_proof", "clean_machine_install_proof", "one_station_week_proof", "three_station_week_proof"].includes(check.id);
}

const PHASE_LABELS: Record<CommissioningPhase, string> = {
  identity: "Station identity",
  station_box: "StationBox & resilience",
  sources_outputs: "Sources & outputs",
  guided_air_proof: "Guided first-air proof",
  external_proof: "External acceptance & field proof",
};

export interface CommissioningGroup {
  id: CommissioningPhase | "authoritative_checks";
  label: string;
  checks: AuthoritativeCommissioningCheck[];
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
