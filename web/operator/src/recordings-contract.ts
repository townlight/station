export interface RecordingDefinition {
  recording_id: string;
  channel_id: string;
  schedule_item_id: string;
  schedule_revision: number;
  scheduled_start_unix_ms: number;
  scheduled_end_unix_ms: number;
  requested_by: { actor_id: string; role: "operator" | "supervisor" | "recording_worker" };
  requested_at_unix_ms: number;
}

export interface ScheduledRecording {
  definition: RecordingDefinition;
  lifecycle: string | Record<string, unknown>;
  revision: number;
}

export interface RecordingTransitionReceipt {
  recording_id: string;
  prior_revision: number;
  resulting_revision: number;
  kind: string;
  actor: { actor_id: string; role: string };
  occurred_at_unix_ms: number;
  execution: unknown;
}

export interface ScheduledRecordingHistoryEntry {
  recording: ScheduledRecording;
  receipt: RecordingTransitionReceipt | null;
  actor: { actor_id: string; role: string };
  occurred_at_unix_ms: number;
}

export interface ScheduleItem {
  item_id: string;
  revision: number;
  channel_id: string;
  asset_id: string;
  title: string;
  starts_at_unix_ms: number;
  duration_ms: number;
  state: "draft" | "committed" | "cancelled";
}

export type RecordingLifecycleKind = "draft" | "armed" | "capturing" | "finalizing" | "completed" | "failed" | "cancelled_before_arm" | "cancelled_after_arm";

/** The server derives channel, window, schedule revision, actor, and time. */
export function createScheduledRecordingBody(recordingId: string, scheduleItemId: string) {
  return { recording_id: recordingId, schedule_item_id: scheduleItemId };
}

/** Arm is a CAS intent only; worker events and execution authority are not browser inputs. */
export function recordingExpectedRevisionBody(expectedRevision: number) {
  return { expected_revision: expectedRevision };
}

/** Cancellation reason is operator intent; actor, time, and transition receipt remain server-owned. */
export function cancelScheduledRecordingBody(expectedRevision: number, reason: string) {
  return { expected_revision: expectedRevision, reason };
}

export function recordingHistoryLimit(limit = 12): number {
  return Math.max(1, Math.min(128, Math.trunc(limit)));
}

export function scheduledRecordingCandidatesPath(channelId: string, limit = 128): string {
  return `/api/v1/scheduled-recordings/candidates?channel_id=${encodeURIComponent(channelId)}&limit=${recordingHistoryLimit(limit)}`;
}

export function isFutureCommittedScheduleItem(item: ScheduleItem, nowUnixMs: number): boolean {
  return item.state === "committed" && item.starts_at_unix_ms > nowUnixMs;
}

export function scheduleCandidateRefreshDelay(item: ScheduleItem, nowUnixMs: number, maxDelayMs = 86_400_000): number {
  if (!isFutureCommittedScheduleItem(item, nowUnixMs)) return 0;
  return Math.max(1, Math.min(maxDelayMs, item.starts_at_unix_ms - nowUnixMs));
}

export function recordingLifecycleLabel(lifecycle: ScheduledRecording["lifecycle"]): string {
  if (typeof lifecycle === "string") return lifecycle.replaceAll("_", " ");
  const kind = Object.keys(lifecycle)[0] ?? "unknown";
  return kind.replaceAll("_", " ");
}

/** Rust's externally tagged lifecycle is a string for unit variants and an object for data variants. */
export function recordingLifecycleKind(lifecycle: ScheduledRecording["lifecycle"]): RecordingLifecycleKind | "unknown" {
  const value = typeof lifecycle === "string" ? lifecycle : Object.keys(lifecycle)[0];
  return (value ?? "unknown") as RecordingLifecycleKind | "unknown";
}

export function canArmRecording(recording: ScheduledRecording, nowUnixMs: number): boolean {
  return recordingLifecycleKind(recording.lifecycle) === "draft" && recording.definition.scheduled_start_unix_ms > nowUnixMs;
}

export function canCancelRecording(recording: ScheduledRecording, nowUnixMs: number): boolean {
  const kind = recordingLifecycleKind(recording.lifecycle);
  return (kind === "draft" || kind === "armed") && recording.definition.scheduled_start_unix_ms > nowUnixMs;
}
