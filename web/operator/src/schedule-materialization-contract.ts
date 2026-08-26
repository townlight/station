export type ScheduleMaterializationDisposition = "applied" | "already_applied";

export interface MaterializationWindow {
  starts_at_utc_ms: number;
  ends_at_utc_ms: number;
}

export type ScheduleMaterializationOutcome =
  | { kind: "proposed"; occurrence_id: string; schedule_item_id: string; asset_sha256: string; asset_title: string; starts_at_utc_ms: number; ends_at_utc_ms: number }
  | { kind: "no_match"; starts_at_utc_ms: number }
  | { kind: "blocked"; starts_at_utc_ms: number }
  | { kind: "suppressed_by_precedence"; starts_at_utc_ms: number; winner_rule_id: string; candidate_priority: number; winner_priority: number; reason: string }
  | { kind: "existing_automation_draft_unchanged"; occurrence_id: string; schedule_item_id: string; existing: ScheduleMaterializationExistingSchedule }
  | { kind: "existing_automation_draft_changed"; occurrence_id: string; schedule_item_id: string; existing: ScheduleMaterializationExistingSchedule }
  | { kind: "existing_committed"; occurrence_id: string; schedule_item_id: string; existing: ScheduleMaterializationExistingSchedule }
  | { kind: "cancelled_occurrence"; occurrence_id: string; schedule_item_id: string; existing: ScheduleMaterializationExistingSchedule }
  | { kind: "already_aired"; occurrence_id: string; schedule_item_id: string; existing: ScheduleMaterializationExistingSchedule }
  | { kind: "suppressed_by_existing_schedule"; occurrence_id: string; schedule_item_id: string; existing: ScheduleMaterializationExistingSchedule; existing_owner: string };

export interface ScheduleMaterializationExistingSchedule {
  schedule_item_id: string;
  revision: number;
  channel_id: string;
  asset_sha256: string;
  title: string;
  starts_at_utc_ms: number;
  duration_ms: number;
  state: "draft" | "committed" | "cancelled";
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

export type ScheduleMaterializationOrigin = "automatic" | "operator";

export interface ScheduleMaterializationCurrentSchedule {
  item_id: string;
  revision: number;
  channel_id: string;
  asset_id: string;
  title: string;
  starts_at_unix_ms: number;
  duration_ms: number;
  state: "draft" | "committed" | "cancelled";
}

export interface ScheduleMaterializationGeneratedDraft {
  occurrence_id: string;
  schedule_item_id: string;
  current_schedule: ScheduleMaterializationCurrentSchedule;
}

export interface ScheduleMaterializationHistoryPlan {
  ordinal: number;
  rule_id: string;
  rule_revision: number;
  channel_id: string;
  saved_search_id: string;
  saved_search_revision: number;
  block_id: string;
  block_revision: number;
  window: MaterializationWindow;
}

export interface ScheduleMaterializationHistoryOutcome {
  ordinal: number;
  rule_id: string;
  rule_revision: number;
  channel_id: string;
  saved_search_id: string;
  saved_search_revision: number;
  block_id: string;
  block_revision: number;
  plan_window: MaterializationWindow;
  materialization_id: string | null;
  compiled_outcome: ScheduleMaterializationOutcome;
  outcome: ScheduleMaterializationOutcome;
}

/** The durable history surface intentionally excludes receipts and evidence secrets. */
export interface ScheduleMaterializationHistoryEntry {
  run_id: string;
  channel_id: string;
  origin: ScheduleMaterializationOrigin;
  actor_id: string;
  request_sha256: string;
  result_sha256: string;
  window: MaterializationWindow;
  observed_at_utc_ms: number;
  occurred_at_unix_ms: number;
  checkpoint_utc_day: number;
  plans: ScheduleMaterializationHistoryPlan[];
  outcomes: ScheduleMaterializationHistoryOutcome[];
  generated_drafts: ScheduleMaterializationGeneratedDraft[];
}

export interface ScheduleMaterializationHistoryCursor {
  occurred_at_unix_ms: number;
  run_id: string;
}

export interface ScheduleMaterializationHistoryResponse {
  entries: ScheduleMaterializationHistoryEntry[];
  next_cursor: ScheduleMaterializationHistoryCursor | null;
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

export const MATERIALIZATION_HISTORY_PAGE_SIZE = 24;

export function scheduleMaterializationHistoryPath(
  channelId: string,
  limit = MATERIALIZATION_HISTORY_PAGE_SIZE,
  before?: ScheduleMaterializationHistoryCursor,
): string {
  if (!Number.isInteger(limit) || limit < 1 || limit > 128) {
    throw new Error("Materialization history limit must be between 1 and 128.");
  }
  const query = `limit=${limit}${before ? `&before_occurred_at_unix_ms=${encodeURIComponent(String(before.occurred_at_unix_ms))}&before_run_id=${encodeURIComponent(before.run_id)}` : ""}`;
  return `/api/v1/scheduling/channels/${encodeURIComponent(channelId)}/materializations/history?${query}`;
}

export function materializationHistoryOriginLabel(origin: ScheduleMaterializationOrigin): string {
  if (origin === "automatic") return "Automatic";
  if (origin === "operator") return "Operator";
  return "Unknown origin";
}

export function recordedValue(value: string | number | null | undefined): string | number {
  return value === null || value === undefined || value === "" ? "Not recorded" : value;
}

export function shouldApplyMaterializationHistoryResponse(
  requestId: number,
  currentRequestId: number,
  requestedChannelId: string,
  currentChannelId: string,
): boolean {
  return requestId === currentRequestId && requestedChannelId === currentChannelId;
}

export function shouldApplyMaterializationChannel(requestedChannelId: string, currentChannelId: string): boolean {
  return requestedChannelId === currentChannelId;
}

export interface MaterializationChannelRenderState {
  operation_channel_id: string;
  busy: boolean;
  error: string | null;
  result: ScheduleMaterializationResponse | null;
  history_channel_id: string;
  history: readonly ScheduleMaterializationHistoryEntry[];
  history_next_before: ScheduleMaterializationHistoryCursor | null;
  history_loading: boolean;
  history_error: string | null;
}

/**
 * Props can change before React runs the channel-reset effect. Do not let that
 * intermediate render attribute an older channel's operation or history to the
 * newly selected channel.
 */
export function materializationChannelRenderState(
  channelId: string,
  state: MaterializationChannelRenderState,
): MaterializationChannelRenderState {
  const operationIsCurrent = shouldApplyMaterializationChannel(state.operation_channel_id, channelId);
  const historyIsCurrent = shouldApplyMaterializationChannel(state.history_channel_id, channelId);
  return {
    operation_channel_id: channelId,
    busy: operationIsCurrent ? state.busy : false,
    error: operationIsCurrent ? state.error : null,
    result: operationIsCurrent ? state.result : null,
    history_channel_id: channelId,
    history: historyIsCurrent ? state.history : [],
    history_next_before: historyIsCurrent ? state.history_next_before : null,
    history_loading: historyIsCurrent ? state.history_loading : true,
    history_error: historyIsCurrent ? state.history_error : null,
  };
}

export async function continueAfterMaterialization(
  disposition: ScheduleMaterializationDisposition,
  requestId: number,
  currentRequestId: () => number,
  onApplied: () => Promise<void>,
  refreshHistory: () => void,
): Promise<boolean> {
  if (disposition === "applied") await onApplied();
  if (requestId !== currentRequestId()) return false;
  refreshHistory();
  return true;
}

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

const MAX_HISTORY_ENTRIES = 128;
const MAX_HISTORY_ARRAY_LENGTH = 4_096;
const MAX_HISTORY_PLANS_PER_ENTRY = 64;
const MAX_HISTORY_STRING_LENGTH = 2_048;
const MAX_HISTORY_TIME = 8_640_000_000_000_000;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const AUTOMATIC_MATERIALIZATION_ACTOR_ID = "00000000-0000-4000-8000-000000000022";

function malformedHistory(detail: string): never {
  throw new Error(`Materialization history response is malformed: ${detail}`);
}

function historyObject(value: unknown, label: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) malformedHistory(`${label} must be an object.`);
  return value as Record<string, unknown>;
}

function historyKeys(value: Record<string, unknown>, expected: readonly string[], label: string): void {
  const actual = Object.keys(value);
  if (actual.length !== expected.length || expected.some((key) => !Object.prototype.hasOwnProperty.call(value, key))) {
    malformedHistory(`${label} has unexpected fields.`);
  }
}

function historyString(value: unknown, label: string, max = MAX_HISTORY_STRING_LENGTH): string {
  if (typeof value !== "string" || value.length === 0 || value.length > max || value.trim() !== value || [...value].some((character) => character.charCodeAt(0) < 0x20)) malformedHistory(`${label} must be a bounded string.`);
  return value;
}

function historyIdentity(value: unknown, label: string): string {
  const identity = historyString(value, label, 36);
  if (!UUID_PATTERN.test(identity)) malformedHistory(`${label} must be a canonical UUID.`);
  return identity;
}

function historyOccurrenceIdentity(value: unknown, label: string): string {
  return historyHash(value, label);
}

function historyHash(value: unknown, label: string): string {
  const hash = historyString(value, label, 64);
  if (!SHA256_PATTERN.test(hash)) malformedHistory(`${label} must be a SHA-256 identity.`);
  return hash;
}

function historyInteger(value: unknown, label: string, min = Number.MIN_SAFE_INTEGER, max = Number.MAX_SAFE_INTEGER): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < min || value > max) malformedHistory(`${label} must be a bounded integer.`);
  return value;
}

function historyTime(value: unknown, label: string): number {
  return historyInteger(value, label, 0, MAX_HISTORY_TIME);
}

function historyPositiveTime(value: unknown, label: string): number {
  return historyInteger(value, label, 1, MAX_HISTORY_TIME);
}

function historyArray(value: unknown, label: string, max = MAX_HISTORY_ARRAY_LENGTH): unknown[] {
  if (!Array.isArray(value) || value.length > max) malformedHistory(`${label} must be a bounded array.`);
  return value;
}

function scheduleItemIdForOccurrence(occurrenceId: string): string {
  const prefix = new TextEncoder().encode("townlight.schedule-item-id.v1");
  const bytes = new Uint8Array(prefix.length + 2 + 8 + occurrenceId.length);
  bytes.set(prefix);
  const view = new DataView(bytes.buffer);
  view.setUint16(prefix.length, 1, true);
  view.setBigUint64(prefix.length + 2, BigInt(occurrenceId.length), true);
  bytes.set(new TextEncoder().encode(occurrenceId), prefix.length + 10);
  return uuidV8FromDigest(sha256(bytes));
}

function deterministicUuidV8(namespace: string, identity: string): string {
  const namespaceBytes = new TextEncoder().encode(namespace);
  const identityBytes = new TextEncoder().encode(identity);
  const bytes = new Uint8Array(namespaceBytes.length + 1 + identityBytes.length);
  bytes.set(namespaceBytes);
  bytes[namespaceBytes.length] = 0;
  bytes.set(identityBytes, namespaceBytes.length + 1);
  return uuidV8FromDigest(sha256(bytes));
}

function uuidV8FromDigest(source: Uint8Array): string {
  const digest = source.slice(0, 16);
  digest[6] = (digest[6] & 0x0f) | 0x80;
  digest[8] = (digest[8] & 0x3f) | 0x80;
  const hex = [...digest].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function sha256(input: Uint8Array): Uint8Array {
  const constants = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ];
  const paddedLength = Math.ceil((input.length + 9) / 64) * 64;
  const padded = new Uint8Array(paddedLength);
  padded.set(input);
  padded[input.length] = 0x80;
  new DataView(padded.buffer).setBigUint64(padded.length - 8, BigInt(input.length) * 8n, false);
  let a = 0x6a09e667, b = 0xbb67ae85, c = 0x3c6ef372, d = 0xa54ff53a;
  let e = 0x510e527f, f = 0x9b05688c, g = 0x1f83d9ab, h = 0x5be0cd19;
  for (let offset = 0; offset < padded.length; offset += 64) {
    const words = new Uint32Array(64);
    const view = new DataView(padded.buffer, offset, 64);
    for (let index = 0; index < 16; index++) words[index] = view.getUint32(index * 4, false);
    for (let index = 16; index < 64; index++) {
      const value = words[index - 15];
      const s0 = ((value >>> 7) | (value << 25)) ^ ((value >>> 18) | (value << 14)) ^ (value >>> 3);
      const prior = words[index - 2];
      const s1 = ((prior >>> 17) | (prior << 15)) ^ ((prior >>> 19) | (prior << 13)) ^ (prior >>> 10);
      words[index] = (words[index - 16] + s0 + words[index - 7] + s1) >>> 0;
    }
    let aa = a, bb = b, cc = c, dd = d, ee = e, ff = f, gg = g, hh = h;
    for (let index = 0; index < 64; index++) {
      const s1 = ((ee >>> 6) | (ee << 26)) ^ ((ee >>> 11) | (ee << 21)) ^ ((ee >>> 25) | (ee << 7));
      const choose = (ee & ff) ^ (~ee & gg);
      const temp1 = (hh + s1 + choose + constants[index] + words[index]) >>> 0;
      const s0 = ((aa >>> 2) | (aa << 30)) ^ ((aa >>> 13) | (aa << 19)) ^ ((aa >>> 22) | (aa << 10));
      const majority = (aa & bb) ^ (aa & cc) ^ (bb & cc);
      const temp2 = (s0 + majority) >>> 0;
      hh = gg; gg = ff; ff = ee; ee = (dd + temp1) >>> 0;
      dd = cc; cc = bb; bb = aa; aa = (temp1 + temp2) >>> 0;
    }
    a = (a + aa) >>> 0; b = (b + bb) >>> 0; c = (c + cc) >>> 0; d = (d + dd) >>> 0;
    e = (e + ee) >>> 0; f = (f + ff) >>> 0; g = (g + gg) >>> 0; h = (h + hh) >>> 0;
  }
  const digest = new Uint8Array(32);
  const view = new DataView(digest.buffer);
  [a, b, c, d, e, f, g, h].forEach((value, index) => view.setUint32(index * 4, value, false));
  return digest;
}

function parseHistoryWindow(value: unknown, label: string): MaterializationWindow {
  const source = historyObject(value, label);
  historyKeys(source, ["starts_at_utc_ms", "ends_at_utc_ms"], label);
  const window = {
    starts_at_utc_ms: historyTime(source.starts_at_utc_ms, `${label} start`),
    ends_at_utc_ms: historyTime(source.ends_at_utc_ms, `${label} end`),
  };
  if (window.ends_at_utc_ms <= window.starts_at_utc_ms) malformedHistory(`${label} has an inverted time range.`);
  return window;
}

function parseHistoryState(value: unknown, label: string): "draft" | "committed" | "cancelled" {
  if (value !== "draft" && value !== "committed" && value !== "cancelled") malformedHistory(`${label} has an unknown state.`);
  return value;
}

function parseHistoryCurrentSchedule(value: unknown): ScheduleMaterializationCurrentSchedule {
  const source = historyObject(value, "generated draft current schedule");
  historyKeys(source, ["item_id", "revision", "channel_id", "asset_id", "title", "starts_at_unix_ms", "duration_ms", "state"], "generated draft current schedule");
  return {
    item_id: historyIdentity(source.item_id, "current schedule item identity"),
    revision: historyInteger(source.revision, "current schedule revision", 1),
    channel_id: historyIdentity(source.channel_id, "current schedule channel identity"),
    asset_id: historyHash(source.asset_id, "current schedule asset identity"),
    title: historyString(source.title, "current schedule title", 240),
    starts_at_unix_ms: historyTime(source.starts_at_unix_ms, "current schedule start"),
    duration_ms: historyInteger(source.duration_ms, "current schedule duration", 1, MAX_HISTORY_TIME),
    state: parseHistoryState(source.state, "current schedule"),
  };
}

function parseHistoryExistingSchedule(value: unknown): ScheduleMaterializationExistingSchedule {
  const source = historyObject(value, "materialization outcome existing schedule");
  historyKeys(source, ["schedule_item_id", "revision", "channel_id", "asset_sha256", "title", "starts_at_utc_ms", "duration_ms", "state"], "materialization outcome existing schedule");
  return {
    schedule_item_id: historyIdentity(source.schedule_item_id, "existing schedule item identity"),
    revision: historyInteger(source.revision, "existing schedule revision", 1),
    channel_id: historyIdentity(source.channel_id, "existing schedule channel identity"),
    asset_sha256: historyHash(source.asset_sha256, "existing schedule asset hash"),
    title: historyString(source.title, "existing schedule title", 240),
    starts_at_utc_ms: historyTime(source.starts_at_utc_ms, "existing schedule start"),
    duration_ms: historyInteger(source.duration_ms, "existing schedule duration", 1, MAX_HISTORY_TIME),
    state: parseHistoryState(source.state, "existing schedule"),
  };
}

function parseHistoryOutcome(value: unknown): ScheduleMaterializationOutcome {
  const source = historyObject(value, "materialization outcome");
  const kind = historyString(source.kind, "materialization outcome kind", 64);
  if (kind === "proposed") {
    historyKeys(source, ["kind", "occurrence_id", "schedule_item_id", "asset_sha256", "asset_title", "starts_at_utc_ms", "ends_at_utc_ms"], "proposed outcome");
    const starts = historyTime(source.starts_at_utc_ms, "proposed start");
    const ends = historyTime(source.ends_at_utc_ms, "proposed end");
    if (ends <= starts) malformedHistory("proposed outcome has an inverted time range.");
    const occurrenceId = historyOccurrenceIdentity(source.occurrence_id, "proposed occurrence identity");
    const scheduleItemId = historyIdentity(source.schedule_item_id, "proposed schedule identity");
    if (scheduleItemIdForOccurrence(occurrenceId) !== scheduleItemId) malformedHistory("proposed schedule identity does not match its occurrence.");
    return { kind, occurrence_id: occurrenceId, schedule_item_id: scheduleItemId, asset_sha256: historyHash(source.asset_sha256, "proposed asset hash"), asset_title: historyString(source.asset_title, "proposed asset title"), starts_at_utc_ms: starts, ends_at_utc_ms: ends };
  }
  if (kind === "no_match" || kind === "blocked") {
    historyKeys(source, ["kind", "starts_at_utc_ms"], `${kind} outcome`);
    return { kind, starts_at_utc_ms: historyTime(source.starts_at_utc_ms, `${kind} time`) };
  }
  if (kind === "suppressed_by_precedence") {
    historyKeys(source, ["kind", "starts_at_utc_ms", "winner_rule_id", "candidate_priority", "winner_priority", "reason"], "precedence outcome");
    const reason = historyString(source.reason, "precedence reason", 64);
    if (reason !== "higher_priority" && reason !== "rule_id_tie_break") malformedHistory("precedence outcome reason is unknown.");
    return { kind, starts_at_utc_ms: historyTime(source.starts_at_utc_ms, "precedence time"), winner_rule_id: historyIdentity(source.winner_rule_id, "precedence winner rule"), candidate_priority: historyInteger(source.candidate_priority, "candidate priority", -32_768, 32_767), winner_priority: historyInteger(source.winner_priority, "winner priority", -32_768, 32_767), reason };
  }
  const existingKinds = ["existing_automation_draft_unchanged", "existing_automation_draft_changed", "existing_committed", "cancelled_occurrence", "already_aired"] as const;
  if ((existingKinds as readonly string[]).includes(kind)) {
    historyKeys(source, ["kind", "occurrence_id", "schedule_item_id", "existing"], `${kind} outcome`);
    const scheduleItemId = historyIdentity(source.schedule_item_id, `${kind} schedule identity`);
    const existing = parseHistoryExistingSchedule(source.existing);
    if (existing.schedule_item_id !== scheduleItemId) malformedHistory(`${kind} existing schedule item does not match its outcome.`);
    const occurrenceId = historyOccurrenceIdentity(source.occurrence_id, `${kind} occurrence identity`);
    if (scheduleItemIdForOccurrence(occurrenceId) !== scheduleItemId) malformedHistory(`${kind} schedule identity does not match its occurrence.`);
    return { kind: kind as (typeof existingKinds)[number], occurrence_id: occurrenceId, schedule_item_id: scheduleItemId, existing };
  }
  if (kind === "suppressed_by_existing_schedule") {
    historyKeys(source, ["kind", "occurrence_id", "schedule_item_id", "existing", "existing_owner"], "existing schedule outcome");
    const scheduleItemId = historyIdentity(source.schedule_item_id, "existing schedule identity");
    const existing = parseHistoryExistingSchedule(source.existing);
    const owner = historyString(source.existing_owner, "existing schedule owner", 32);
    if (existing.schedule_item_id !== scheduleItemId || !["manual", "automation", "committed"].includes(owner)) malformedHistory("existing schedule disposition is inconsistent.");
    const occurrenceId = historyOccurrenceIdentity(source.occurrence_id, "existing schedule occurrence identity");
    if (scheduleItemIdForOccurrence(occurrenceId) !== scheduleItemId) malformedHistory("existing schedule identity does not match its occurrence.");
    return { kind, occurrence_id: occurrenceId, schedule_item_id: scheduleItemId, existing, existing_owner: owner };
  }
  malformedHistory(`unknown materialization outcome kind '${kind}'.`);
}

function parseHistoryPlan(value: unknown): ScheduleMaterializationHistoryPlan {
  const source = historyObject(value, "materialization history plan");
  historyKeys(source, ["ordinal", "rule_id", "rule_revision", "channel_id", "saved_search_id", "saved_search_revision", "block_id", "block_revision", "window"], "materialization history plan");
  return { ordinal: historyInteger(source.ordinal, "plan ordinal", 0, MAX_HISTORY_ARRAY_LENGTH - 1), rule_id: historyIdentity(source.rule_id, "plan rule identity"), rule_revision: historyInteger(source.rule_revision, "plan rule revision", 1), channel_id: historyIdentity(source.channel_id, "plan channel identity"), saved_search_id: historyIdentity(source.saved_search_id, "plan saved-search identity"), saved_search_revision: historyInteger(source.saved_search_revision, "plan saved-search revision", 1), block_id: historyIdentity(source.block_id, "plan block identity"), block_revision: historyInteger(source.block_revision, "plan block revision", 1), window: parseHistoryWindow(source.window, "plan window") };
}

function parseHistoryOutcomeRecord(value: unknown): ScheduleMaterializationHistoryOutcome {
  const source = historyObject(value, "materialization history outcome record");
  historyKeys(source, ["ordinal", "rule_id", "rule_revision", "channel_id", "saved_search_id", "saved_search_revision", "block_id", "block_revision", "plan_window", "materialization_id", "compiled_outcome", "outcome"], "materialization history outcome record");
  if (source.materialization_id !== null && typeof source.materialization_id !== "string") malformedHistory("materialization identity must be null or a string.");
  const compiledOutcome = parseHistoryOutcome(source.compiled_outcome);
  const outcome = parseHistoryOutcome(source.outcome);
  const channelId = historyIdentity(source.channel_id, "outcome channel identity");
  for (const candidate of [compiledOutcome, outcome]) {
    if ("existing" in candidate && candidate.existing.channel_id !== channelId) malformedHistory("existing schedule channel does not match its outcome.");
  }
  return { ordinal: historyInteger(source.ordinal, "outcome ordinal", 0, MAX_HISTORY_ARRAY_LENGTH - 1), rule_id: historyIdentity(source.rule_id, "outcome rule identity"), rule_revision: historyInteger(source.rule_revision, "outcome rule revision", 1), channel_id: channelId, saved_search_id: historyIdentity(source.saved_search_id, "outcome saved-search identity"), saved_search_revision: historyInteger(source.saved_search_revision, "outcome saved-search revision", 1), block_id: historyIdentity(source.block_id, "outcome block identity"), block_revision: historyInteger(source.block_revision, "outcome block revision", 1), plan_window: parseHistoryWindow(source.plan_window, "outcome plan window"), materialization_id: source.materialization_id === null ? null : historyHash(source.materialization_id, "materialization identity"), compiled_outcome: compiledOutcome, outcome };
}

function parseHistoryEntry(value: unknown): ScheduleMaterializationHistoryEntry {
  const source = historyObject(value, "materialization history entry");
  historyKeys(source, ["run_id", "channel_id", "origin", "actor_id", "request_sha256", "result_sha256", "window", "observed_at_utc_ms", "occurred_at_unix_ms", "checkpoint_utc_day", "plans", "outcomes", "generated_drafts"], "materialization history entry");
  const runId = historyIdentity(source.run_id, "history run identity");
  const origin = source.origin === "automatic" || source.origin === "operator" ? source.origin : malformedHistory("history origin is unknown.");
  const actorId = historyIdentity(source.actor_id, "history actor identity");
  const requestSha = historyHash(source.request_sha256, "history request hash");
  const resultSha = historyHash(source.result_sha256, "history result hash");
  const expectedOrigin = actorId === AUTOMATIC_MATERIALIZATION_ACTOR_ID ? "automatic" : "operator";
  if (origin !== expectedOrigin) malformedHistory("history origin does not match its actor identity.");
  if (runId !== deterministicUuidV8("townlight.schedule-materialization-run.v1", resultSha)) malformedHistory("history run identity does not match its result hash.");
  const channelId = historyIdentity(source.channel_id, "history channel identity");
  const plans = historyArray(source.plans, "history plans", MAX_HISTORY_PLANS_PER_ENTRY).map(parseHistoryPlan);
  if (plans.length === 0) malformedHistory("history entry must contain at least one plan.");
  const outcomes = historyArray(source.outcomes, "history outcomes").map(parseHistoryOutcomeRecord);
  if (plans.some((plan, index) => plan.ordinal !== index) || outcomes.some((outcome, index) => outcome.ordinal !== index)) malformedHistory("history record ordinals are not contiguous.");
  const generatedDrafts = historyArray(source.generated_drafts, "history generated drafts").map((value) => {
    const draft = historyObject(value, "generated draft");
    historyKeys(draft, ["occurrence_id", "schedule_item_id", "current_schedule"], "generated draft");
    const occurrenceId = historyOccurrenceIdentity(draft.occurrence_id, "generated draft occurrence identity");
    const scheduleItemId = historyIdentity(draft.schedule_item_id, "generated draft schedule identity");
    if (scheduleItemIdForOccurrence(occurrenceId) !== scheduleItemId) malformedHistory("generated draft schedule identity does not match its occurrence.");
    const currentSchedule = parseHistoryCurrentSchedule(draft.current_schedule);
    if (currentSchedule.item_id !== scheduleItemId || currentSchedule.channel_id !== channelId) malformedHistory("generated draft identity does not match its channel or schedule item.");
    return { occurrence_id: occurrenceId, schedule_item_id: scheduleItemId, current_schedule: currentSchedule };
  });
  const window = parseHistoryWindow(source.window, "history window");
  if (plans.some((plan) => plan.channel_id !== channelId || plan.window.starts_at_utc_ms !== window.starts_at_utc_ms || plan.window.ends_at_utc_ms > window.ends_at_utc_ms)) malformedHistory("plan window does not match its history entry.");
  const ruleIds = new Set<string>();
  for (const plan of plans) {
    if (ruleIds.has(plan.rule_id)) malformedHistory("history contains a duplicate plan rule identity.");
    ruleIds.add(plan.rule_id);
  }
  if (Math.max(...plans.map((plan) => plan.window.ends_at_utc_ms)) !== window.ends_at_utc_ms) malformedHistory("history window must equal the union of plan windows.");
  const plansByRule = new Map(plans.map((plan) => [plan.rule_id, plan]));
  if (outcomes.some((outcome) => {
    const plan = plansByRule.get(outcome.rule_id);
    const candidates = [outcome.compiled_outcome, outcome.outcome];
    const escapesWindow = candidates.some((candidate) => {
      if (!("starts_at_utc_ms" in candidate)) return false;
      if (candidate.starts_at_utc_ms < outcome.plan_window.starts_at_utc_ms || candidate.starts_at_utc_ms >= outcome.plan_window.ends_at_utc_ms) return true;
      return candidate.kind === "proposed" && candidate.ends_at_utc_ms > outcome.plan_window.ends_at_utc_ms;
    });
    return outcome.channel_id !== channelId || !plan || outcome.rule_revision !== plan.rule_revision || outcome.saved_search_id !== plan.saved_search_id || outcome.saved_search_revision !== plan.saved_search_revision || outcome.block_id !== plan.block_id || outcome.block_revision !== plan.block_revision || outcome.plan_window.starts_at_utc_ms !== plan.window.starts_at_utc_ms || outcome.plan_window.ends_at_utc_ms !== plan.window.ends_at_utc_ms || escapesWindow;
  })) malformedHistory("outcome authority does not match its plan.");
  const proposedOutcomes = outcomes.filter((outcome) => outcome.outcome.kind === "proposed");
  if (proposedOutcomes.length !== generatedDrafts.length) malformedHistory("generated Drafts do not match the final Proposed outcomes.");
  const matchedProposals = new Set<ScheduleMaterializationHistoryOutcome>();
  for (const draft of generatedDrafts) {
    const match = proposedOutcomes.find((outcome) => {
      if (matchedProposals.has(outcome) || outcome.materialization_id === null || outcome.outcome.kind !== "proposed") return false;
      return outcome.outcome.occurrence_id === draft.occurrence_id
        && outcome.outcome.schedule_item_id === draft.schedule_item_id;
    });
    if (!match) malformedHistory("generated Draft has no final Proposed outcome with matching materialization identity.");
    matchedProposals.add(match);
  }
  const occurredAt = historyPositiveTime(source.occurred_at_unix_ms, "history occurrence time");
  const checkpointDay = historyInteger(source.checkpoint_utc_day, "history checkpoint day", 0);
  if (checkpointDay !== Math.floor(occurredAt / 86_400_000)) malformedHistory("history checkpoint day does not match its occurrence time.");
  return { run_id: runId, channel_id: channelId, origin, actor_id: actorId, request_sha256: requestSha, result_sha256: resultSha, window, observed_at_utc_ms: historyTime(source.observed_at_utc_ms, "history observed time"), occurred_at_unix_ms: occurredAt, checkpoint_utc_day: checkpointDay, plans, outcomes, generated_drafts: generatedDrafts };
}

export function parseMaterializationHistoryResponse(value: unknown): ScheduleMaterializationHistoryResponse {
  const source = historyObject(value, "history response");
  historyKeys(source, ["entries", "next_cursor"], "history response");
  const rawEntries = historyArray(source.entries, "history entries", MAX_HISTORY_ENTRIES);
  const entries: ScheduleMaterializationHistoryEntry[] = [];
  const runIds = new Set<string>();
  const draftIds = new Set<string>();
  const occurrenceIds = new Set<string>();
  let totalOutcomes = 0;
  let totalDrafts = 0;
  let totalPlans = 0;
  let previousEntry: ScheduleMaterializationHistoryEntry | null = null;
  for (const rawEntry of rawEntries) {
    const entry = parseHistoryEntry(rawEntry);
    if (runIds.has(entry.run_id)) malformedHistory("history contains a duplicate run identity.");
    if (previousEntry && !historyEntryIsOlder(entry, previousEntry)) malformedHistory("history entries must be in strict descending order.");
    runIds.add(entry.run_id);
    previousEntry = entry;
    totalPlans += entry.plans.length;
    totalOutcomes += entry.outcomes.length;
    totalDrafts += entry.generated_drafts.length;
    if (totalPlans > MAX_HISTORY_ARRAY_LENGTH || totalOutcomes > MAX_HISTORY_ARRAY_LENGTH || totalDrafts > MAX_HISTORY_ARRAY_LENGTH) malformedHistory("history aggregate arrays exceed the bounded capacity.");
    for (const draft of entry.generated_drafts) {
      if (draftIds.has(draft.schedule_item_id) || occurrenceIds.has(draft.occurrence_id)) malformedHistory("history contains a duplicate generated Draft identity.");
      draftIds.add(draft.schedule_item_id);
      occurrenceIds.add(draft.occurrence_id);
    }
    entries.push(entry);
  }
  let nextCursor: ScheduleMaterializationHistoryCursor | null = null;
  if (source.next_cursor !== null) {
    const cursor = historyObject(source.next_cursor, "history cursor");
    historyKeys(cursor, ["occurred_at_unix_ms", "run_id"], "history cursor");
    nextCursor = { occurred_at_unix_ms: historyPositiveTime(cursor.occurred_at_unix_ms, "history cursor time"), run_id: historyIdentity(cursor.run_id, "history cursor run identity") };
    const last = entries.at(-1);
    if (!last || nextCursor.occurred_at_unix_ms !== last.occurred_at_unix_ms || nextCursor.run_id !== last.run_id) malformedHistory("history cursor does not identify the last returned entry.");
  }
  return { entries, next_cursor: nextCursor };
}

function historyEntryIsOlder(entry: ScheduleMaterializationHistoryEntry, newer: ScheduleMaterializationHistoryEntry): boolean {
  return entry.occurred_at_unix_ms < newer.occurred_at_unix_ms
    || (entry.occurred_at_unix_ms === newer.occurred_at_unix_ms && entry.run_id < newer.run_id);
}

export function appendMaterializationHistoryPage(
  current: readonly ScheduleMaterializationHistoryEntry[],
  next: ScheduleMaterializationHistoryResponse,
  reset = false,
): ScheduleMaterializationHistoryEntry[] {
  if (reset || current.length === 0) return [...next.entries];
  const runIds = new Set(current.map((entry) => entry.run_id));
  if (next.entries.some((entry) => runIds.has(entry.run_id))) malformedHistory("history page repeats a run identity.");
  if (next.entries.length > 0 && !historyEntryIsOlder(next.entries[0], current[current.length - 1])) {
    malformedHistory("history page is not older than the records already shown.");
  }
  const draftIds = new Set(current.flatMap((entry) => entry.generated_drafts.map((draft) => draft.schedule_item_id)));
  const occurrenceIds = new Set(current.flatMap((entry) => entry.generated_drafts.map((draft) => draft.occurrence_id)));
  for (const entry of next.entries) {
    if (runIds.has(entry.run_id)) malformedHistory("history page repeats a run identity.");
    runIds.add(entry.run_id);
    for (const draft of entry.generated_drafts) {
      if (draftIds.has(draft.schedule_item_id) || occurrenceIds.has(draft.occurrence_id)) malformedHistory("history page repeats a generated Draft identity.");
      draftIds.add(draft.schedule_item_id);
      occurrenceIds.add(draft.occurrence_id);
    }
  }
  return [...current, ...next.entries];
}

/** Alias kept descriptive for callers that name the endpoint's full response. */
export const parseScheduleMaterializationHistoryResponse = parseMaterializationHistoryResponse;

export function recurringDraftChannelState(enabled: boolean): "enabled" | "disabled" {
  return enabled ? "enabled" : "disabled";
}
