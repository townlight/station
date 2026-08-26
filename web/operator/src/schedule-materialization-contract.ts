export type ScheduleMaterializationDisposition = "applied" | "already_applied";

export interface MaterializationWindow {
  starts_at_utc_ms: number;
  ends_at_utc_ms: number;
}

export interface ScheduleMaterializationOutcome {
  kind: string;
  [key: string]: unknown;
}

export interface ScheduleMaterializationOutcomeRecord {
  ordinal: number;
  rule_id: string;
  outcome: ScheduleMaterializationOutcome;
}

export interface ScheduleMaterializationPlan {
  ordinal: number;
  rule_id: string;
  window: MaterializationWindow;
}

export interface ScheduleMaterializationResponse {
  disposition: ScheduleMaterializationDisposition;
  run_id: string;
  window: MaterializationWindow;
  plans: ScheduleMaterializationPlan[];
  outcomes: ScheduleMaterializationOutcomeRecord[];
}

export interface MaterializationDispositionCount {
  kind: string;
  count: number;
}

export type RecurringDraftPermissionState =
  | "checking"
  | "available"
  | "read_only"
  | "unavailable";

export function scheduleMaterializationPath(channelId: string): string {
  return `/api/v1/scheduling/channels/${encodeURIComponent(channelId)}/materializations`;
}

export function scheduleMaterializationRequestInit(): RequestInit {
  return { method: "POST" };
}

export function channelSchedulePath(channelId: string): string {
  return `/api/v1/schedule/items?channel_id=${encodeURIComponent(channelId)}`;
}

export function canGenerateRecurringDrafts(roles: readonly string[]): boolean {
  return roles.some((role) => role === "setup_admin" || role === "meeting_operator");
}

export function recurringDraftPermissionState(
  roles: readonly string[] | null,
  authorityError: string | null,
): RecurringDraftPermissionState {
  if (authorityError) return "unavailable";
  if (roles === null) return "checking";
  return canGenerateRecurringDrafts(roles) ? "available" : "read_only";
}

export function summarizeMaterializationOutcomes(
  outcomes: readonly ScheduleMaterializationOutcomeRecord[],
): MaterializationDispositionCount[] {
  const counts = new Map<string, number>();
  for (const record of outcomes) {
    const kind = record.outcome.kind;
    counts.set(kind, (counts.get(kind) ?? 0) + 1);
  }
  return [...counts]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([kind, count]) => ({ kind, count }));
}

export function boundedMaterializationError(problem: unknown): string {
  const fallback = "Recurring drafts could not be generated.";
  const message = problem instanceof Error ? problem.message.trim() : fallback;
  return (message || fallback).slice(0, 500);
}

export function formatUtcMaterializationTime(unixMs: number): string {
  const timestamp = new Date(unixMs);
  if (!Number.isFinite(unixMs) || Number.isNaN(timestamp.getTime())) return "Invalid UTC time";
  return `${timestamp.toISOString().slice(0, 19).replace("T", " ")} UTC`;
}
