export type PublishTargetKind = "portal" | "internet_archive" | "local_nas" | "youtube_live";

/** Non-secret, typed provider configuration. Credentials are never browser-readable. */
export type PublishExecutionTargetConfig =
  | { kind: "portal"; endpoint: string }
  | { kind: "internet_archive"; item_identifier: string }
  | { kind: "local_nas"; managed_root: string }
  | { kind: "youtube_live"; channel_id: string; broadcast_id: string; stream_id: string; privacy_status: "public" | "unlisted" };

export interface PublishTargetAccount {
  account_id: string;
  kind: PublishTargetKind;
  label: string;
  enabled: boolean;
  credential_configured: boolean;
  revision: number;
}

export interface PublishTargetProfile {
  target_id: string;
  account_id: string;
  kind: PublishTargetKind;
  label: string;
  enabled: boolean;
  configuration_hash: string;
  revision: number;
}

export interface PublicationReadinessCheck {
  id: "publish_targets";
  label: string;
  status: "pass" | "warning" | "pending" | "blocked" | "not_verified" | "unknown" | "recoverable";
  detail: string;
  evidence: string;
}

export const PUBLISH_TARGET_KINDS: PublishTargetKind[] = [
  "portal", "internet_archive", "local_nas", "youtube_live",
];

export interface PublishAccountDraft {
  kind: PublishTargetKind;
  label: string;
  enabled: boolean;
}

export interface PublishTargetDraft {
  accountId: string;
  kind: PublishTargetKind;
  label: string;
  enabled: boolean;
  configuration: PublishExecutionTargetConfig;
}

export interface PublishAccountMutation {
  kind: PublishTargetKind;
  label: string;
  enabled: boolean;
  expected_revision: number | null;
}

export interface PublishTargetMutation {
  account_id: string;
  kind: PublishTargetKind;
  label: string;
  enabled: boolean;
  configuration: PublishExecutionTargetConfig;
  expected_revision: number | null;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const SHA256 = /^[0-9a-f]{64}$/;

function revisionReason(revision: number | null): string | null {
  return revision !== null && (!Number.isSafeInteger(revision) || revision < 1)
    ? "The current authority revision is invalid. Refresh before saving."
    : null;
}

function labelReason(label: string, subject: string): string | null {
  return label.trim().length === 0 || label.length > 200
    ? `A bounded ${subject} label is required.`
    : null;
}

function validManagedNasRoot(value: string): boolean {
  if (value.length === 0 || value.length > 500 || value !== value.trim() || value.includes("/")) return false;
  const lower = value.toLowerCase();
  if (lower.startsWith("\\\\?\\") || lower.startsWith("\\\\.\\") || lower.startsWith("\\??\\") || lower.startsWith("\\device\\")) return false;
  const validComponent = (component: string) => {
    if (!component || component === "." || component === ".." || component.endsWith(".") || component.endsWith(" ") || component.trim() !== component || /[\x00-\x1f\x7f:\"<>|?*]/.test(component)) return false;
    const basename = component.split(".", 1)[0].toUpperCase();
    return !["CON", "PRN", "AUX", "NUL"].includes(basename) && !/^(COM|LPT)[1-9]$/.test(basename);
  };
  if (value.startsWith("\\\\")) return value.slice(2).split("\\").length >= 2 && value.slice(2).split("\\").every(validComponent);
  if (!/^[A-Za-z]:\\/.test(value)) return false;
  return value.slice(3).split("\\").every(validComponent);
}

export function emptyPublishingConfiguration(kind: PublishTargetKind): PublishExecutionTargetConfig {
  switch (kind) {
    case "portal": return { kind, endpoint: "https://" };
    case "internet_archive": return { kind, item_identifier: "" };
    case "local_nas": return { kind, managed_root: "\\\\" };
    case "youtube_live": return { kind, channel_id: "", broadcast_id: "", stream_id: "", privacy_status: "unlisted" };
  }
}

function configurationReason(configuration: PublishExecutionTargetConfig, kind: PublishTargetKind): string | null {
  if (!configuration || typeof configuration !== "object" || typeof configuration.kind !== "string" || configuration.kind !== kind) return "Provider configuration must match the selected target kind.";
  if (configuration.kind === "portal") {
    if (typeof configuration.endpoint !== "string") return "Portal endpoint is required.";
    const endpoint = configuration.endpoint.trim();
    if (endpoint.length === 0 || endpoint.length > 500 || !endpoint.startsWith("https://") || endpoint.endsWith("/") || endpoint.includes("@") || endpoint.includes("?") || endpoint.includes("#")) return "Portal endpoint must be a bounded HTTPS URL without credentials, query, fragment, or trailing slash.";
    try { if (new URL(endpoint).hostname.length === 0) return "Portal endpoint must include a host."; } catch { return "Portal endpoint must be a valid HTTPS URL."; }
  } else if (configuration.kind === "internet_archive") {
    if (typeof configuration.item_identifier !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/.test(configuration.item_identifier)) return "Internet Archive item identifier must be a bounded slug.";
  } else if (configuration.kind === "local_nas") {
    if (typeof configuration.managed_root !== "string" || !validManagedNasRoot(configuration.managed_root)) return "Managed NAS root must be an ordinary absolute drive or UNC path without traversal or device aliases.";
  } else if (![configuration.channel_id, configuration.broadcast_id, configuration.stream_id].every((value) => typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/.test(value))) {
    return "YouTube channel, broadcast, and stream identities must be bounded identifiers.";
  } else if (configuration.privacy_status !== "public" && configuration.privacy_status !== "unlisted") {
    return "YouTube privacy status must be public or unlisted.";
  }
  return null;
}

export function publishingAccountMutationReason(draft: PublishAccountDraft, expectedRevision: number | null): string | null {
  return labelReason(draft.label, "account") ?? revisionReason(expectedRevision);
}

export function publishingTargetMutationReason(draft: PublishTargetDraft, expectedRevision: number | null): string | null {
  return labelReason(draft.label, "target")
    ?? (!UUID.test(draft.accountId) ? "Select a valid publishing account." : null)
    ?? configurationReason(draft.configuration, draft.kind)
    ?? revisionReason(expectedRevision);
}

/** Browser payloads deliberately exclude identity, attribution, secrets, and trusted evidence. */
export function publishAccountMutation(draft: PublishAccountDraft, expectedRevision: number | null): PublishAccountMutation {
  return {
    kind: draft.kind,
    label: draft.label.trim(),
    enabled: draft.enabled,
    expected_revision: expectedRevision,
  };
}

/** Browser payloads deliberately exclude identity, attribution, secrets, and trusted evidence. */
export function publishTargetMutation(draft: PublishTargetDraft, expectedRevision: number | null): PublishTargetMutation {
  return {
    account_id: draft.accountId,
    kind: draft.kind,
    label: draft.label.trim(),
    enabled: draft.enabled,
    configuration: draft.configuration,
    expected_revision: expectedRevision,
  };
}

export function publishingKindLabel(kind: PublishTargetKind): string {
  return {
    portal: "Canonical portal",
    internet_archive: "Internet archive",
    local_nas: "Local NAS",
    youtube_live: "YouTube Live",
  }[kind];
}

export function publishingReadinessLabel(status: PublicationReadinessCheck["status"]): string {
  return {
    pass: "Connection authority current",
    warning: "Connection authority needs attention",
    pending: "Connection authority pending",
    blocked: "Connection authority blocked",
    not_verified: "Connection authority not verified",
    unknown: "Connection authority unknown",
    recoverable: "Connection authority needs action",
  }[status];
}

export function redactConfigurationHash(value: string): string {
  return SHA256.test(value) ? `${value.slice(0, 10)}…${value.slice(-6)}` : "Not recorded";
}

export function publishingHistoryPath(kind: "accounts" | "targets", id: string): string {
  return `/api/v1/publishing/${kind}/${encodeURIComponent(id)}/history?limit=20`;
}

export function publishingExecutionRequestPath(targetId: string, assetId: string): string | null {
  return UUID.test(targetId) && SHA256.test(assetId)
    ? `/api/v1/publishing/targets/${encodeURIComponent(targetId)}/assets/${assetId}/jobs`
    : null;
}

/** NAS execution uses the server's handle-anchored finalization boundary. */
export function publishingExecutionUnavailableReason(kind: PublishTargetKind): string | null {
  return null;
}

export function publishingExecutionJobPath(targetId: string, jobId: string): string | null {
  return UUID.test(targetId) && UUID.test(jobId)
    ? `/api/v1/publishing/targets/${encodeURIComponent(targetId)}/jobs/${encodeURIComponent(jobId)}`
    : null;
}

export function publishingExecutionHistoryPath(targetId: string, limit = 20): string | null {
  if (!UUID.test(targetId) || !Number.isInteger(limit) || limit < 1 || limit > 128) return null;
  return `/api/v1/publishing/targets/${encodeURIComponent(targetId)}/jobs?limit=${limit}`;
}

export type PublishExecutionJobState = "pending" | "running" | "completed" | "failed";
export type PublishExecutionStage = "attempted" | "uploaded" | "delivered" | "published" | "externally_verified";

export interface PublishExecutionReceipt {
  receipt_id: string; job_id: string; account_id: string; account_revision: number;
  target_id: string; target_revision: number; target_kind: PublishTargetKind;
  configuration_hash: string; credential_version: number;
  candidate: { version: string; source_commit: string };
  media_sha256: string; media_bytes: number; stage: PublishExecutionStage;
  delivery_identity_sha256: string; occurred_at_unix_ms: number; recorded_at_unix_ms: number; detail: string;
}

export interface PublishExecutionJob {
  job_id: string; account_id: string; account_revision: number; target_id: string; target_revision: number;
  target_kind: PublishTargetKind; configuration_hash: string; credential_version: number;
  candidate_version: string; source_commit: string; install_generation: number; asset_id: string;
  media_sha256: string; media_bytes: number; media_duration_ms: number; requested_by: string; created_at_unix_ms: number;
}

export interface PublishExecutionJobResponse {
  job: PublishExecutionJob; state: PublishExecutionJobState; attempts: number;
  next_attempt_at_unix_ms: number; updated_at_unix_ms: number; lease_expires_at_unix_ms: number | null;
  completed_at_unix_ms: number | null; failure_class: string | null; receipts: PublishExecutionReceipt[];
}

export function publishingJobStateLabel(state: PublishExecutionJobState): string {
  return { pending: "Queued", running: "Running", completed: "Completed", failed: "Failed" }[state];
}

export function publishingStageLabel(stage: PublishExecutionStage): string {
  return { attempted: "Attempted", uploaded: "Uploaded", delivered: "Delivered", published: "Published", externally_verified: "Externally verified" }[stage];
}

export class LatestPublishingAuthorityLoad {
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
