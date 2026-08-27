export type AsRunState = "program" | "fallback" | "live" | "cg" | "caption" | "gap";

export interface AsRunProjectionWindow {
  local_date: string;
  starts_at_utc_ms: number;
  ends_at_utc_ms: number;
}

export interface AsRunProjectedEntry {
  event_id: string;
  journal_sequence: number;
  started_at_unix_ms: number;
  ended_at_unix_ms: number;
  state: AsRunState;
  content_id: string | null;
  franchise_id: string | null;
  category: string | null;
  title: string | null;
  catalog_revision: number | null;
  schedule_revision: number | null;
  journal_hash: string;
  unavailable_reason: string | null;
}

export interface AsRunProjectionPage {
  channel_id: string;
  station_timezone: string;
  projection_window: AsRunProjectionWindow | null;
  source_journal_id: string;
  source_file_identity: string;
  source_raw_sequence: number;
  source_checkpoint_revision: number;
  entries: AsRunProjectedEntry[];
  next_cursor: string | null;
  checkpoint_sequence: number;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const SHA256 = /^[0-9a-fA-F]{64}$/;
const WORKER_ID = /^station-worker-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const OPAQUE_CURSOR = /^[A-Za-z0-9_-]{1,128}$/;
const LOCAL_DATE = /^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])$/;
const STATES = new Set<AsRunState>(["program", "fallback", "live", "cg", "caption", "gap"]);
const PAGE_KEYS = ["channel_id", "station_timezone", "projection_window", "source_journal_id", "source_file_identity", "source_raw_sequence", "source_checkpoint_revision", "entries", "next_cursor", "checkpoint_sequence"] as const;
const WINDOW_KEYS = ["local_date", "starts_at_utc_ms", "ends_at_utc_ms"] as const;
const ENTRY_KEYS = ["event_id", "journal_sequence", "started_at_unix_ms", "ended_at_unix_ms", "state", "content_id", "franchise_id", "category", "title", "catalog_revision", "schedule_revision", "journal_hash", "unavailable_reason"] as const;

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function exactShape(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
}

function safeInteger(value: unknown, minimum = 0): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= minimum;
}

function boundedText(value: unknown, maximum: number, allowEmpty = false): value is string {
  return typeof value === "string" && new TextEncoder().encode(value).length <= maximum && (allowEmpty || value.trim().length > 0) && !/[\u0000-\u001f\u007f-\u009f]/.test(value);
}

function nullableText(value: unknown, maximum = 512): value is string | null {
  return value === null || boundedText(value, maximum);
}

function nullableRevision(value: unknown): value is number | null {
  return value === null || safeInteger(value, 1);
}

export function isCanonicalUuid(value: string): boolean { return UUID.test(value); }

export function asRunPagePath(channelId: string, limit: number, cursor?: string | null): string | null {
  if (!isCanonicalUuid(channelId) || !safeInteger(limit, 1) || limit > 200) return null;
  let path = `/api/v1/reporting/as-run/${channelId}?limit=${limit}`;
  if (cursor !== undefined && cursor !== null) {
    if (!OPAQUE_CURSOR.test(cursor)) return null;
    path += `&cursor=${cursor}`;
  }
  return path;
}

export function asRunExportPath(channelId: string, format: "csv" | "xmltv" | string): string | null {
  if (!isCanonicalUuid(channelId) || (format !== "csv" && format !== "xmltv")) return null;
  return `/api/v1/reporting/as-run/${channelId}/export.${format}`;
}

function localParts(unixMs: number, timezone: string): { date: string; hour: number; minute: number; second: number } {
  const parts = new Intl.DateTimeFormat("en-CA-u-ca-iso8601", { timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }).formatToParts(unixMs);
  const part = (kind: Intl.DateTimeFormatPartTypes) => parts.find((entry) => entry.type === kind)?.value ?? "";
  return { date: `${part("year")}-${part("month")}-${part("day")}`, hour: Number(part("hour")), minute: Number(part("minute")), second: Number(part("second")) };
}

function adjacentDate(localDate: string, days: number): string | null {
  const [year, month, day] = localDate.split("-").map(Number);
  const instant = new Date(Date.UTC(year, month - 1, day));
  if (instant.getUTCFullYear() !== year || instant.getUTCMonth() !== month - 1 || instant.getUTCDate() !== day) return null;
  instant.setUTCDate(instant.getUTCDate() + days);
  return `${instant.getUTCFullYear().toString().padStart(4, "0")}-${(instant.getUTCMonth() + 1).toString().padStart(2, "0")}-${instant.getUTCDate().toString().padStart(2, "0")}`;
}

function parseWindow(value: unknown, timezone: string): AsRunProjectionWindow | null {
  if (value === null) return null;
  if (!record(value) || !exactShape(value, WINDOW_KEYS) || !boundedText(value.local_date, 10) || !LOCAL_DATE.test(value.local_date)
    || !safeInteger(value.starts_at_utc_ms) || !safeInteger(value.ends_at_utc_ms) || value.ends_at_utc_ms <= value.starts_at_utc_ms) {
    throw new Error("Reporting projection window is invalid.");
  }
  const previousDate = adjacentDate(value.local_date, -1);
  const nextDate = adjacentDate(value.local_date, 1);
  if (!previousDate || !nextDate || value.starts_at_utc_ms % 1000 !== 0 || value.ends_at_utc_ms % 1000 !== 0) throw new Error("Reporting projection window is invalid.");
  const start = localParts(value.starts_at_utc_ms, timezone);
  const immediatelyBeforeStart = localParts(value.starts_at_utc_ms - 1, timezone);
  const end = localParts(value.ends_at_utc_ms, timezone);
  const immediatelyBeforeEnd = localParts(value.ends_at_utc_ms - 1, timezone);
  if (start.date !== value.local_date || start.hour !== 0 || start.minute !== 0 || start.second !== 0 || immediatelyBeforeStart.date !== previousDate
    || end.date !== nextDate || end.hour !== 0 || end.minute !== 0 || end.second !== 0 || immediatelyBeforeEnd.date !== value.local_date) {
    throw new Error("Reporting projection window does not match its station timezone.");
  }
  return value as unknown as AsRunProjectionWindow;
}

function parseEntry(value: unknown, channelWindow: AsRunProjectionWindow | null): AsRunProjectedEntry {
  if (!record(value) || !exactShape(value, ENTRY_KEYS)
    || typeof value.event_id !== "string" || !isCanonicalUuid(value.event_id)
    || !safeInteger(value.journal_sequence, 1)
    || !safeInteger(value.started_at_unix_ms) || !safeInteger(value.ended_at_unix_ms) || value.ended_at_unix_ms <= value.started_at_unix_ms
    || typeof value.state !== "string" || !STATES.has(value.state as AsRunState)
    || !nullableText(value.content_id, 240) || !nullableText(value.franchise_id, 240) || !nullableText(value.category, 240) || !nullableText(value.title, 240)
    || !nullableRevision(value.catalog_revision) || !nullableRevision(value.schedule_revision)
    || typeof value.journal_hash !== "string" || !SHA256.test(value.journal_hash)
    || !nullableText(value.unavailable_reason)) {
    throw new Error("Reporting projection entry is invalid.");
  }
  if (channelWindow && (value.started_at_unix_ms < channelWindow.starts_at_utc_ms || value.ended_at_unix_ms > channelWindow.ends_at_utc_ms)) {
    throw new Error("Reporting projection entry falls outside its window.");
  }
  const programIncomplete = value.state === "program" && [value.content_id, value.franchise_id, value.category, value.title, value.catalog_revision, value.schedule_revision].some((field) => field === null);
  if ((value.state !== "program" || programIncomplete) && !boundedText(value.unavailable_reason, 512)) {
    throw new Error("Reporting projection entry requires an unavailable reason.");
  }
  if (value.state !== "program" && [value.content_id, value.franchise_id, value.category, value.title, value.catalog_revision, value.schedule_revision].some((field) => field !== null)) {
    throw new Error("Non-program reporting entry carries program authority.");
  }
  return value as unknown as AsRunProjectedEntry;
}

export function parseAsRunProjectionPage(value: unknown): AsRunProjectionPage {
  if (!record(value) || !exactShape(value, PAGE_KEYS)) throw new Error("Reporting page shape is invalid.");
  if (typeof value.channel_id !== "string" || !isCanonicalUuid(value.channel_id)) throw new Error("Reporting channel identity is invalid.");
  if (!boundedText(value.station_timezone, 80)) throw new Error("Reporting station timezone is invalid.");
  try { new Intl.DateTimeFormat("en", { timeZone: value.station_timezone }).format(0); } catch { throw new Error("Reporting station timezone is invalid."); }
  const establishedSource = boundedText(value.source_file_identity, 512) && safeInteger(value.source_raw_sequence, 1) && safeInteger(value.source_checkpoint_revision, 1);
  const zeroReaderSource = value.source_file_identity === "" && value.source_raw_sequence === 0 && value.source_checkpoint_revision === 0;
  if (typeof value.source_journal_id !== "string" || !WORKER_ID.test(value.source_journal_id) || (!establishedSource && !zeroReaderSource)
    || !safeInteger(value.checkpoint_sequence, 1) || (establishedSource && value.checkpoint_sequence > (value.source_raw_sequence as number))
    || (value.next_cursor !== null && (typeof value.next_cursor !== "string" || !OPAQUE_CURSOR.test(value.next_cursor))) || !Array.isArray(value.entries) || value.entries.length > 200) {
    throw new Error("Reporting source sequence or authority is invalid.");
  }
  const projectionWindow = parseWindow(value.projection_window, value.station_timezone);
  const entries = value.entries.map((entry) => parseEntry(entry, projectionWindow));
  const checkpointSequence = value.checkpoint_sequence as number;
  for (let index = 1; index < entries.length; index += 1) {
    const previous = entries[index - 1];
    const current = entries[index];
    if (current.journal_sequence <= previous.journal_sequence || current.started_at_unix_ms !== previous.ended_at_unix_ms) {
      throw new Error("Reporting page entries are not ordered.");
    }
  }
  if (entries.some((entry) => entry.journal_sequence > checkpointSequence)) throw new Error("Reporting entry exceeds its checkpoint sequence.");
  return { ...value, projection_window: projectionWindow, entries } as unknown as AsRunProjectionPage;
}

function sameWindow(left: AsRunProjectionWindow | null, right: AsRunProjectionWindow | null): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

export function mergeAsRunPages(first: AsRunProjectionPage, next: AsRunProjectionPage): AsRunProjectionPage {
  if (first.channel_id !== next.channel_id || first.station_timezone !== next.station_timezone
    || !sameWindow(first.projection_window, next.projection_window) || first.source_journal_id !== next.source_journal_id
    || first.source_file_identity !== next.source_file_identity || first.source_raw_sequence !== next.source_raw_sequence
    || first.source_checkpoint_revision !== next.source_checkpoint_revision || first.checkpoint_sequence !== next.checkpoint_sequence) {
    throw new Error("Reporting pagination crossed projection authority.");
  }
  const identities = new Set(first.entries.map((entry) => entry.event_id));
  if (next.entries.some((entry) => identities.has(entry.event_id))) throw new Error("Reporting pagination contains a duplicate event identity.");
  const entries = [...first.entries, ...next.entries];
  for (let index = 1; index < entries.length; index += 1) {
    if (entries[index].journal_sequence <= entries[index - 1].journal_sequence || entries[index].started_at_unix_ms !== entries[index - 1].ended_at_unix_ms) {
      throw new Error("Reporting pagination is not ordered.");
    }
  }
  return { ...first, entries, next_cursor: next.next_cursor, checkpoint_sequence: next.checkpoint_sequence };
}

export interface ReportingChannelState {
  state_channel_id: string;
  page: AsRunProjectionPage | null;
  busy: "load" | "more" | null;
  error: string | null;
}

/** Props can change before effects run; never attribute older authority to the replacement channel. */
export function reportingChannelRenderState(channelId: string, state: ReportingChannelState): ReportingChannelState {
  if (state.state_channel_id === channelId) return state;
  return { state_channel_id: channelId, page: null, busy: null, error: null };
}
