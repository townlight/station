export type PublishTargetKind = "portal" | "internet_archive" | "local_nas" | "youtube_live" | "syndication" | "podcast";

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
  "portal", "internet_archive", "local_nas", "youtube_live", "syndication", "podcast",
];

export interface PublishAccountDraft {
  kind: PublishTargetKind;
  label: string;
  enabled: boolean;
  credentialConfigured: boolean;
}

export interface PublishTargetDraft {
  accountId: string;
  kind: PublishTargetKind;
  label: string;
  enabled: boolean;
  configurationHash: string;
}

export interface PublishAccountMutation {
  kind: PublishTargetKind;
  label: string;
  enabled: boolean;
  credential_configured: boolean;
  expected_revision: number | null;
}

export interface PublishTargetMutation {
  account_id: string;
  kind: PublishTargetKind;
  label: string;
  enabled: boolean;
  configuration_hash: string;
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

export function publishingAccountMutationReason(draft: PublishAccountDraft, expectedRevision: number | null): string | null {
  return labelReason(draft.label, "account") ?? revisionReason(expectedRevision);
}

export function publishingTargetMutationReason(draft: PublishTargetDraft, expectedRevision: number | null): string | null {
  return labelReason(draft.label, "target")
    ?? (!UUID.test(draft.accountId) ? "Select a valid publishing account." : null)
    ?? (!SHA256.test(draft.configurationHash) ? "Configuration authority must be a lowercase SHA-256 hash." : null)
    ?? revisionReason(expectedRevision);
}

/** Browser payloads deliberately exclude identity, attribution, secrets, and trusted evidence. */
export function publishAccountMutation(draft: PublishAccountDraft, expectedRevision: number | null): PublishAccountMutation {
  return {
    kind: draft.kind,
    label: draft.label.trim(),
    enabled: draft.enabled,
    credential_configured: draft.credentialConfigured,
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
    configuration_hash: draft.configurationHash,
    expected_revision: expectedRevision,
  };
}

export function publishingKindLabel(kind: PublishTargetKind): string {
  return {
    portal: "Canonical portal",
    internet_archive: "Internet archive",
    local_nas: "Local NAS",
    youtube_live: "YouTube Live",
    syndication: "Syndication",
    podcast: "Podcast",
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
