import { useCallback, useEffect, useMemo, useState } from "react";

export type CgRoute = "cg-moderation" | "cg-board" | "cg-feeds" | "cg-preview" | "cg-settings";

export interface CgTemplateZone {
  template_zone_key: string;
  zone_kind: string;
  bounds: { x: number; y: number; width: number; height: number };
  z_index: number;
}

export interface CgTemplate {
  template_id: string;
  display_name: string;
  kind: string;
  zones: CgTemplateZone[];
}

export interface CgBoard {
  board_id: string;
  channel_id: string;
  template_id: string;
  active: boolean;
  background_audio_asset_id: string | null;
  revision: number;
  created_at_unix_ms: number;
  updated_at_unix_ms: number;
  created_by: string;
}

export interface CgZone {
  zone_id: string;
  board_id: string;
  template_zone_key: string;
  zone_kind: string;
  content_source: { kind: string; text?: string; feed_source_id?: string; mode?: string; asset_id?: string; format?: string; source_id?: string };
  rotation_seconds: number | null;
  approval_required: boolean;
  allowed_tags: string[];
  revision: number;
  created_at_unix_ms: number;
  updated_at_unix_ms: number;
}

export interface CgFeed {
  feed_source_id: string;
  channel_id: string;
  kind: string;
  label: string;
  source_url: string;
  trust_tier: string;
  refresh_seconds: number;
  enabled: boolean;
  revision: number;
  created_at_unix_ms: number;
  updated_at_unix_ms: number;
  created_by: string;
  last_fetched_at_unix_ms: number | null;
  last_fetch_error: string | null;
  tags?: string[];
}

export interface CgFeedItemRead {
  item_id: string;
  title: string;
  summary: string | null;
  starts_at_unix_ms: number | null;
  url: string | null;
  tags: string[];
  approved: boolean;
  approved_by: string | null;
  approved_at_unix_ms: number | null;
}

export interface CgFeedItemApproval {
  feed_source_id: string;
  item_id: string;
  approved_by: string;
  approved_at_unix_ms: number;
}

export interface CgFeedRead {
  feed_source_id: string;
  label: string;
  kind: string;
  source_url: string;
  trust_tier: string;
  tags: string[];
  enabled: boolean;
  refresh_seconds: number;
  last_fetched_at_unix_ms: number | null;
  last_fetch_error: string | null;
  cache_fetched_at_unix_ms: number | null;
  cache_expires_at_unix_ms: number | null;
  cache_last_error: string | null;
  stale: boolean;
  never_fetched: boolean;
  items: CgFeedItemRead[];
}

export interface CgFeedItemsRead {
  channel_id: string;
  as_of_unix_ms: number;
  feeds: CgFeedRead[];
}

export interface CgResolvedFeedItem {
  item_id: string;
  title: string;
  summary: string | null;
  starts_at_unix_ms: number | null;
  url: string | null;
  tags: string[];
}

export type CgResolvedBulletinContent =
  | { kind: "text"; title: string; body: string }
  | { kind: "uploaded_image"; asset_id: string; alt_text: string }
  | { kind: "fullscreen_slide"; asset_id: string; alt_text: string }
  | { kind: "live_video"; source_id: string; title: string };

export type CgResolvedPlaceholderSource = "bulletin_rotation" | "emergency" | "feed" | "schedule" | "image" | "live_video";
export type CgResolvedPlaceholderReason = "no_airable_bulletins" | "no_bulletins_match_tags" | "no_active_emergency" | "missing_feed_source" | "feed_source_disabled" | "feed_data_unavailable" | "no_approved_feed_items" | "no_feed_items_match_tags" | "schedule_data_unavailable" | "image_asset_readiness_unknown" | "live_source_readiness_unknown";

export type CgResolvedContent =
  | { kind: "manual_text"; text: string }
  | { kind: "bulletin"; bulletin_id: string; content: CgResolvedBulletinContent }
  | { kind: "clock"; format: string }
  | { kind: "feed_items"; feed_source_id: string; items: CgResolvedFeedItem[]; stale: boolean; last_error: string | null }
  | { kind: "placeholder"; source: CgResolvedPlaceholderSource; reason: CgResolvedPlaceholderReason };

export interface CgResolvedZone {
  zone_id: string;
  template_zone_key: string;
  zone_kind: string;
  bounds: { x: number; y: number; width: number; height: number };
  z_index: number;
  content_source: CgZone["content_source"];
  content: CgResolvedContent;
  revision: number;
}

export interface CgResolvedSnapshot {
  board_id: string;
  channel_id: string;
  template_id: string;
  template_kind: string;
  board_revision: number;
  generated_at_unix_ms: number;
  zones: CgResolvedZone[];
}

export interface CgBulletin {
  bulletin_id: string;
  channel_id: string;
  content: { kind: string; title?: string; body?: string; asset_id?: string; alt_text?: string; source_id?: string };
  state: string;
  tags: string[];
  requested_start_at_unix_ms: number | null;
  requested_end_at_unix_ms: number | null;
  display_seconds: number;
  audio_asset_id: string | null;
  submitted_by: string;
  moderation_notes: string | null;
  moderated_by: string | null;
  revision: number;
  created_at_unix_ms: number;
  updated_at_unix_ms: number;
}

export interface CgAuditEvent {
  audit_id: string;
  board_id: string | null;
  channel_id: string;
  event_kind: string;
  operator_id: string;
  occurred_at_unix_ms: number;
  details: Record<string, string>;
}

export interface CgData {
  templates: CgTemplate[];
  board: CgBoard | null;
  zones: CgZone[];
  feeds: CgFeed[];
  feedItems: CgFeedItemsRead;
  bulletins: CgBulletin[];
  audit: CgAuditEvent[];
}

export interface CgChannelOption {
  channel_id: string;
  display_name: string;
}

export class CgApiError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message);
  }
}

async function cgRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...init, headers: { "Content-Type": "application/json", ...init?.headers } });
  const body = await response.json().catch(() => null) as { message?: string; error?: { message?: string } } | T | null;
  if (!response.ok) {
    const message = body && typeof body === "object" && "message" in body
      ? String(body.message)
      : body && typeof body === "object" && "error" in body && body.error && typeof body.error === "object" && "message" in body.error
        ? String(body.error.message)
      : `CG request failed with HTTP ${response.status}.`;
    throw new CgApiError(response.status, message);
  }
  return body as T;
}

export const cgApi = {
  templates: () => cgRequest<CgTemplate[]>("/api/v1/cg/templates"),
  board: (channelId: string) => cgRequest<CgBoard>(`/api/v1/cg/board?channel_id=${encodeURIComponent(channelId)}`),
  zones: (boardId: string) => cgRequest<CgZone[]>(`/api/v1/cg/zones?board_id=${encodeURIComponent(boardId)}`),
  feeds: (channelId: string) => cgRequest<CgFeed[]>(`/api/v1/cg/feeds?channel_id=${encodeURIComponent(channelId)}`),
  feedItems: (channelId: string, atUnixMs = Date.now()) => cgRequest<CgFeedItemsRead>(`/api/v1/cg/feed-items?channel_id=${encodeURIComponent(channelId)}&at_unix_ms=${atUnixMs}`),
  preview: (channelId: string, atUnixMs: number) => cgRequest<CgResolvedSnapshot>(`/api/v1/cg/preview?channel_id=${encodeURIComponent(channelId)}&at_unix_ms=${atUnixMs}`),
  bulletins: (channelId: string) => cgRequest<CgBulletin[]>(`/api/v1/cg/bulletins?channel_id=${encodeURIComponent(channelId)}`),
  audit: (channelId: string) => cgRequest<CgAuditEvent[]>(`/api/v1/cg/audit?channel_id=${encodeURIComponent(channelId)}&limit=50`),
  writeBoard: (body: { board: CgBoard; expected_revision: number; audit: CgAuditEvent }) => cgRequest<CgBoard>("/api/v1/cg/board", { method: "PUT", body: JSON.stringify(body) }),
  writeZone: (body: { zone: CgZone; expected_revision: number; audit: CgAuditEvent }) => cgRequest<CgZone>("/api/v1/cg/zones", { method: "PUT", body: JSON.stringify(body) }),
  deleteZone: (zoneId: string, body: { expected_revision: number; audit: CgAuditEvent }) => cgRequest<void>(`/api/v1/cg/zones/${encodeURIComponent(zoneId)}`, { method: "DELETE", body: JSON.stringify(body) }),
  writeFeed: (body: { feed: CgFeed; expected_revision: number; audit: CgAuditEvent }) => cgRequest<CgFeed>("/api/v1/cg/feeds", { method: "PUT", body: JSON.stringify(body) }),
  deleteFeed: (feedId: string, body: { expected_revision: number; audit: CgAuditEvent }) => cgRequest<void>(`/api/v1/cg/feeds/${encodeURIComponent(feedId)}`, { method: "DELETE", body: JSON.stringify(body) }),
  writeBulletin: (body: { bulletin: CgBulletin; expected_revision: number; audit: CgAuditEvent }) => cgRequest<CgBulletin>("/api/v1/cg/bulletins", { method: "PUT", body: JSON.stringify(body) }),
  approveFeedItem: (body: { approval: CgFeedItemApproval; audit: CgAuditEvent }) => cgRequest<CgFeedItemApproval>("/api/v1/cg/feed-items/approve", { method: "POST", body: JSON.stringify(body) }),
};

export const CG_ROUTES: Array<{ id: CgRoute; label: string; shortLabel: string }> = [
  { id: "cg-moderation", label: "Moderation queue", shortLabel: "Queue" },
  { id: "cg-board", label: "Board designer", shortLabel: "Board" },
  { id: "cg-feeds", label: "Feed manager", shortLabel: "Feeds" },
  { id: "cg-preview", label: "Live preview", shortLabel: "Preview" },
  { id: "cg-settings", label: "Settings & history", shortLabel: "Settings" },
];

function formatCgTime(value: number): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(value);
}

function toLocalDateTime(value: number | null): string {
  if (value === null) return "";
  const date = new Date(value);
  const pad = (part: number) => String(part).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function displayKind(value: string): string {
  return value.replaceAll("_", " ").replaceAll("-", " ");
}

function newUuid(): string {
  return crypto.randomUUID();
}

function buildAudit(channelId: string, boardId: string | null, eventKind: string, operatorId: string, details: Record<string, string>, occurredAtUnixMs: number): CgAuditEvent {
  return {
    audit_id: newUuid(),
    board_id: boardId,
    channel_id: channelId,
    event_kind: eventKind,
    operator_id: operatorId.trim(),
    occurred_at_unix_ms: occurredAtUnixMs,
    details,
  };
}

function readSessionOperator(): string {
  try {
    return sessionStorage.getItem("townlight.cg.operator") ?? "";
  } catch {
    return "";
  }
}

function writeSessionOperator(value: string): void {
  try {
    if (value.trim()) sessionStorage.setItem("townlight.cg.operator", value);
    else sessionStorage.removeItem("townlight.cg.operator");
  } catch {
    // Some embedded WebViews disable storage; the field remains usable in memory.
  }
}

function CgLoading() {
  return <section className="panel cg-state" role="status"><div className="cg-spinner" aria-hidden="true" /><strong>Reading CG configuration…</strong><p>Board, zones, feeds, bulletins, and audit history are loaded from this station.</p></section>;
}

function CgError({ error, onRetry }: { error: string; onRetry: () => void }) {
  return <section className="panel cg-state cg-state-error" role="alert"><div className="cg-state-icon">!</div><strong>CG configuration could not be read</strong><p>{error}</p><button className="secondary" onClick={onRetry}>Retry</button></section>;
}

function CgEmpty({ title, detail }: { title: string; detail: string }) {
  return <div className="cg-empty"><div className="empty-orbit">+</div><strong>{title}</strong><p>{detail}</p></div>;
}

function MutationNotice({ children = "Editing is read-only in this slice." }: { children?: string }) {
  return <div className="cg-notice"><span aria-hidden="true">i</span><p>{children} The station API requires an explicit revision and append-only audit payload; no un-audited changes are offered here.</p></div>;
}

function OperatorIdentity({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  return <label className="cg-operator-identity">Operator name <span>(session-local; not authentication)</span><input value={value} maxLength={120} onChange={(event) => onChange(event.target.value)} placeholder="Enter your operator name" /></label>;
}

function MutationResult({ error, message }: { error: string | null; message: string | null }) {
  return <>{error && <div className="error-box" role="alert">{error}</div>}{message && <div className="notice" role="status">{message}</div>}</>;
}

function CgTabs({ route, onNavigate }: { route: CgRoute; onNavigate: (route: CgRoute) => void }) {
  return <nav className="cg-tabs" aria-label="CG operator screens">{CG_ROUTES.map((item) => <button key={item.id} className={route === item.id ? "selected" : ""} onClick={() => onNavigate(item.id)}>{item.label}</button>)}</nav>;
}

function ModerationQueue({ data, channelId, operatorId, onRefresh }: { data: CgData; channelId: string; operatorId: string; onRefresh: () => Promise<void> }) {
  const counts = useMemo(() => data.bulletins.reduce<Record<string, number>>((result, bulletin) => ({ ...result, [bulletin.state]: (result[bulletin.state] ?? 0) + 1 }), {}), [data.bulletins]);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [tags, setTags] = useState("");
  const [displaySeconds, setDisplaySeconds] = useState("12");
  const [notesById, setNotesById] = useState<Record<string, string>>({});
  const [schedulingId, setSchedulingId] = useState<string | null>(null);
  const [scheduleStart, setScheduleStart] = useState("");
  const [scheduleEnd, setScheduleEnd] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  function clearResult() {
    setError(null);
    setMessage(null);
  }

  function parseTags(value: string): string[] | null {
    const parsed = value.split(/[\s,]+/).map((tag) => tag.trim().toLowerCase()).filter(Boolean);
    if (parsed.some((tag) => !/^[a-z0-9_-]{1,40}$/.test(tag)) || new Set(parsed).size !== parsed.length || parsed.length > 32) return null;
    return parsed;
  }

  async function submitTextBulletin() {
    clearResult();
    const parsedTags = parseTags(tags);
    const seconds = Number(displaySeconds);
    if (!operatorId.trim()) {
      setError("Enter an operator name before submitting a bulletin.");
      return;
    }
    if (!title.trim() || !body.trim() || !parsedTags || !Number.isInteger(seconds) || seconds < 3 || seconds > 300) {
      setError("Enter a title, body, valid tags, and a display duration from 3 to 300 seconds.");
      return;
    }
    setBusy("submit");
    try {
      const now = Date.now();
      const bulletin: CgBulletin = {
        bulletin_id: newUuid(),
        channel_id: channelId,
        content: { kind: "text", title: title.trim(), body: body.trim() },
        state: "submitted",
        tags: parsedTags,
        requested_start_at_unix_ms: null,
        requested_end_at_unix_ms: null,
        display_seconds: seconds,
        audio_asset_id: null,
        submitted_by: operatorId.trim(),
        moderation_notes: null,
        moderated_by: null,
        revision: 1,
        created_at_unix_ms: now,
        updated_at_unix_ms: now,
      };
      await cgApi.writeBulletin({ bulletin, expected_revision: 0, audit: buildAudit(channelId, data.board?.board_id ?? null, "bulletin_submitted", operatorId, { content_kind: "text" }, bulletin.updated_at_unix_ms) });
      await onRefresh();
      setTitle("");
      setBody("");
      setTags("");
      setDisplaySeconds("12");
      setMessage("Text bulletin submitted and audit evidence recorded.");
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "The text bulletin could not be submitted.");
    } finally {
      setBusy(null);
    }
  }

  async function moderateBulletin(bulletin: CgBulletin, nextState: "needs_changes" | "accepted" | "declined") {
    clearResult();
    const note = notesById[bulletin.bulletin_id]?.trim() ?? "";
    if (!operatorId.trim()) {
      setError("Enter an operator name before moderating a bulletin.");
      return;
    }
    if ((nextState === "needs_changes" || nextState === "declined") && !note) {
      setError(nextState === "declined" ? "A decline reason is required." : "A request-changes note is required.");
      return;
    }
    if (nextState === "declined" && !window.confirm(`Decline “${bulletin.content.title ?? "this bulletin"}”? This is a terminal moderation state.`)) return;
    setBusy(`${nextState}-${bulletin.bulletin_id}`);
    try {
      const now = Math.max(Date.now(), bulletin.updated_at_unix_ms);
      const updated: CgBulletin = {
        ...bulletin,
        state: nextState,
        requested_start_at_unix_ms: nextState === "accepted" ? null : bulletin.requested_start_at_unix_ms,
        requested_end_at_unix_ms: nextState === "accepted" ? null : bulletin.requested_end_at_unix_ms,
        moderation_notes: note || bulletin.moderation_notes,
        moderated_by: operatorId.trim(),
        revision: bulletin.revision + 1,
        updated_at_unix_ms: now,
      };
      await cgApi.writeBulletin({ bulletin: updated, expected_revision: bulletin.revision, audit: buildAudit(channelId, data.board?.board_id ?? null, "bulletin_moderated", operatorId, { state: nextState }, updated.updated_at_unix_ms) });
      await onRefresh();
      setMessage(`Bulletin ${displayKind(nextState)} and audit evidence recorded.`);
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "The bulletin moderation change could not be saved.");
    } finally {
      setBusy(null);
    }
  }

  function beginSchedule(bulletin: CgBulletin) {
    clearResult();
    setSchedulingId(bulletin.bulletin_id);
    setScheduleStart(toLocalDateTime(bulletin.requested_start_at_unix_ms));
    setScheduleEnd(toLocalDateTime(bulletin.requested_end_at_unix_ms));
  }

  async function scheduleBulletin(bulletin: CgBulletin) {
    clearResult();
    if (!operatorId.trim()) {
      setError("Enter an operator name before scheduling a bulletin.");
      return;
    }
    const start = new Date(scheduleStart).getTime();
    const end = new Date(scheduleEnd).getTime();
    if (!scheduleStart || !scheduleEnd || !Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
      setError("Choose a valid start and end time; the end must be after the start.");
      return;
    }
    setBusy(`schedule-${bulletin.bulletin_id}`);
    try {
      const now = Math.max(Date.now(), bulletin.updated_at_unix_ms);
      const updated: CgBulletin = {
        ...bulletin,
        state: "scheduled",
        requested_start_at_unix_ms: start,
        requested_end_at_unix_ms: end,
        moderated_by: operatorId.trim(),
        moderation_notes: notesById[bulletin.bulletin_id]?.trim() || bulletin.moderation_notes,
        revision: bulletin.revision + 1,
        updated_at_unix_ms: now,
      };
      await cgApi.writeBulletin({ bulletin: updated, expected_revision: bulletin.revision, audit: buildAudit(channelId, data.board?.board_id ?? null, "bulletin_moderated", operatorId, { state: "scheduled" }, updated.updated_at_unix_ms) });
      await onRefresh();
      setSchedulingId(null);
      setMessage("Bulletin scheduled and audit evidence recorded.");
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "The bulletin schedule could not be saved.");
    } finally {
      setBusy(null);
    }
  }

  const textBulletins = data.bulletins.filter((bulletin) => bulletin.content.kind === "text");
  return <>
    <div className="cg-stat-strip">{["submitted", "needs_changes", "accepted", "scheduled", "declined"].map((state) => <div key={state}><span>{displayKind(state)}</span><strong>{counts[state] ?? 0}</strong></div>)}</div>
    <section className="panel"><div className="panel-head"><div><p className="eyebrow">Text bulletin intake</p><h2>Submit for review</h2></div><span className="cg-state-pill cg-state-submitted">submitted</span></div><MutationNotice>Only text bulletins are admitted here. Media uploads, live-video sources, deletion, and role authentication are not offered.</MutationNotice><div className="cg-editor-grid"><label>Title<input maxLength={160} value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Library board meeting" /></label><label>Display seconds<input type="number" min={3} max={300} value={displaySeconds} onChange={(event) => setDisplaySeconds(event.target.value)} /></label><label>Tags <span className="cg-form-help">comma-separated slugs</span><input value={tags} onChange={(event) => setTags(event.target.value)} placeholder="community, public-safety" /></label><label>Body<textarea maxLength={4000} value={body} onChange={(event) => setBody(event.target.value)} placeholder="Tuesday at 6 PM in the community room." /></label></div><button className="primary" disabled={busy !== null || !operatorId.trim()} onClick={() => void submitTextBulletin()}>{busy === "submit" ? "Submitting…" : "Submit text bulletin"}</button><MutationResult error={error} message={message} /></section>
    <section className="panel"><div className="panel-head"><div><p className="eyebrow">Durable bulletin lifecycle</p><h2>Review queue</h2></div><span className="count">{data.bulletins.length}</span></div>{data.bulletins.length === 0 ? <CgEmpty title="No bulletins in the queue" detail="Submit the first text bulletin above." /> : <div className="cg-list">{data.bulletins.map((bulletin) => { const isText = bulletin.content.kind === "text"; const note = notesById[bulletin.bulletin_id] ?? bulletin.moderation_notes ?? ""; const busyAction = busy?.endsWith(bulletin.bulletin_id) ?? false; return <article className="cg-card" key={bulletin.bulletin_id}><div className="cg-card-top"><div><strong>{bulletin.content.title ?? displayKind(bulletin.content.kind)}</strong><small>{bulletin.submitted_by} · {formatCgTime(bulletin.created_at_unix_ms)}</small></div><span className={`cg-state-pill cg-state-${bulletin.state}`}>{displayKind(bulletin.state)}</span></div><p>{bulletin.content.body ?? bulletin.content.alt_text ?? "Media bulletin; this screen only mutates text submissions."}</p><div className="cg-tags">{bulletin.tags.length ? bulletin.tags.map((tag) => <span key={tag}>{tag}</span>) : <span>untagged</span>}<small>{bulletin.display_seconds}s · revision {bulletin.revision}</small></div>{isText ? <>{bulletin.state === "needs_changes" && <div className="cg-notice"><span aria-hidden="true">i</span><p>Resubmission is not exposed here; this item remains in needs-changes until the submitter resubmits through a supported intake path.</p></div>}{bulletin.state === "declined" && <div className="cg-notice"><span aria-hidden="true">i</span><p>Declined is terminal in the current lifecycle; no revival control is offered.</p></div>}<label>Moderation note <textarea maxLength={2000} value={note} onChange={(event) => setNotesById((previous) => ({ ...previous, [bulletin.bulletin_id]: event.target.value }))} placeholder="Required for request changes or decline" /></label><div className="cg-inline-actions cg-moderation-actions">{bulletin.state === "submitted" && <><button className="secondary" disabled={busyAction || !operatorId.trim()} onClick={() => void moderateBulletin(bulletin, "needs_changes")}>Request changes</button><button className="primary" disabled={busyAction || !operatorId.trim()} onClick={() => void moderateBulletin(bulletin, "accepted")}>Accept</button></>}{(bulletin.state === "submitted" || bulletin.state === "accepted" || bulletin.state === "scheduled") && <button className="secondary" disabled={busyAction || !operatorId.trim()} onClick={() => beginSchedule(bulletin)}>Schedule</button>}{(bulletin.state === "submitted" || bulletin.state === "accepted" || bulletin.state === "scheduled") && <button className="secondary danger-button" disabled={busyAction || !operatorId.trim()} onClick={() => void moderateBulletin(bulletin, "declined")}>Decline</button>}</div>{schedulingId === bulletin.bulletin_id && <div className="cg-schedule-editor"><label>Start<input type="datetime-local" value={scheduleStart} onChange={(event) => setScheduleStart(event.target.value)} /></label><label>End<input type="datetime-local" value={scheduleEnd} onChange={(event) => setScheduleEnd(event.target.value)} /></label><div className="cg-inline-actions"><button className="primary" disabled={busyAction || !operatorId.trim()} onClick={() => void scheduleBulletin(bulletin)}>{busy === `schedule-${bulletin.bulletin_id}` ? "Scheduling…" : "Save schedule"}</button><button className="secondary" onClick={() => setSchedulingId(null)}>Cancel</button></div></div>}</> : <div className="cg-notice"><span aria-hidden="true">i</span><p>Media and live-video bulletin moderation is not enabled in this text-only screen.</p></div>}</article>; })}</div>}</section>
    {textBulletins.length === 0 && data.bulletins.length > 0 && <section className="panel"><CgEmpty title="No text bulletins available" detail="Existing non-text bulletins remain visible but cannot be changed by this text-only workflow." /></section>}
  </>;
}

function BoardDesigner({ data, channelId, operatorId, onRefresh }: { data: CgData; channelId: string; operatorId: string; onRefresh: () => Promise<void> }) {
  const template = data.templates.find((item) => item.template_id === data.board?.template_id);
  const [templateId, setTemplateId] = useState(data.board?.template_id ?? data.templates[0]?.template_id ?? "");
  const [active, setActive] = useState(data.board?.active ?? true);
  const [zoneKey, setZoneKey] = useState("");
  const [manualText, setManualText] = useState("");
  const [editingZoneId, setEditingZoneId] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const selectedTemplate = data.templates.find((item) => item.template_id === templateId);
  const usedZoneKeys = new Set(data.zones.map((zone) => zone.template_zone_key));
  const availableZoneKeys = selectedTemplate?.zones.filter((item) => !usedZoneKeys.has(item.template_zone_key)) ?? [];
  const editableZoneOptions = editingZoneId ? selectedTemplate?.zones.filter((item) => item.template_zone_key === zoneKey) ?? [] : availableZoneKeys;

  useEffect(() => {
    if (data.board) {
      setTemplateId(data.board.template_id);
      setActive(data.board.active);
    } else if (!templateId && data.templates[0]) {
      setTemplateId(data.templates[0].template_id);
    }
  }, [data.board?.board_id, data.board?.revision, data.templates.length]);

  function clearResult() {
    setError(null);
    setMessage(null);
  }

  async function saveBoard() {
    clearResult();
    if (!operatorId.trim()) {
      setError("Enter an operator name before making an audited change.");
      return;
    }
    const chosenTemplate = data.templates.find((item) => item.template_id === templateId);
    if (!chosenTemplate) {
      setError("Choose one of the built-in templates.");
      return;
    }
    setBusy("board");
    try {
      const now = Date.now();
      const board: CgBoard = data.board
        ? { ...data.board, template_id: templateId, active, revision: data.board.revision + 1, updated_at_unix_ms: Math.max(now, data.board.updated_at_unix_ms) }
        : { board_id: newUuid(), channel_id: channelId, template_id: templateId, active, background_audio_asset_id: null, revision: 1, created_at_unix_ms: now, updated_at_unix_ms: now, created_by: operatorId.trim() };
      await cgApi.writeBoard({ board, expected_revision: data.board?.revision ?? 0, audit: buildAudit(board.channel_id, board.board_id, data.board ? "board_updated" : "board_created", operatorId, { template_id: templateId, active: String(active) }, board.updated_at_unix_ms) });
      await onRefresh();
      setMessage(data.board ? "Board updated and audit evidence recorded." : "Board created and audit evidence recorded.");
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "The board change could not be saved.");
    } finally {
      setBusy(null);
    }
  }

  async function saveManualZone() {
    clearResult();
    if (!operatorId.trim()) {
      setError("Enter an operator name before making an audited change.");
      return;
    }
    if (!data.board || !selectedTemplate || !zoneKey || !manualText.trim()) {
      setError("Choose a built-in zone key and enter manual text.");
      return;
    }
    setBusy("zone");
    try {
      const now = Date.now();
      const editing = editingZoneId ? data.zones.find((zone) => zone.zone_id === editingZoneId) : undefined;
      const templateZone = selectedTemplate.zones.find((zone) => zone.template_zone_key === (editing?.template_zone_key ?? zoneKey));
      if (!templateZone) throw new Error("That zone key is not part of the selected built-in template.");
      const zone: CgZone = editing
        ? { ...editing, content_source: { kind: "manual", text: manualText.trim() }, revision: editing.revision + 1, updated_at_unix_ms: Math.max(now, editing.updated_at_unix_ms) }
        : { zone_id: newUuid(), board_id: data.board.board_id, template_zone_key: zoneKey, zone_kind: templateZone.zone_kind, content_source: { kind: "manual", text: manualText.trim() }, rotation_seconds: null, approval_required: false, allowed_tags: [], revision: 1, created_at_unix_ms: now, updated_at_unix_ms: now };
      await cgApi.writeZone({ zone, expected_revision: editing?.revision ?? 0, audit: buildAudit(data.board.channel_id, data.board.board_id, editing ? "zone_updated" : "zone_created", operatorId, { template_zone_key: zone.template_zone_key, content_source: "manual" }, zone.updated_at_unix_ms) });
      await onRefresh();
      setEditingZoneId(null);
      setZoneKey("");
      setManualText("");
      setMessage(editing ? "Manual zone updated and audit evidence recorded." : "Manual zone added and audit evidence recorded.");
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "The manual zone could not be saved.");
    } finally {
      setBusy(null);
    }
  }

  async function removeManualZone(zone: CgZone) {
    if (!data.board || zone.content_source.kind !== "manual" || !operatorId.trim() || !window.confirm(`Delete the manual zone “${zone.template_zone_key}”?`)) return;
    clearResult();
    setBusy(zone.zone_id);
    try {
      await cgApi.deleteZone(zone.zone_id, { expected_revision: zone.revision, audit: buildAudit(data.board.channel_id, data.board.board_id, "zone_removed", operatorId, { template_zone_key: zone.template_zone_key }, Math.max(Date.now(), zone.updated_at_unix_ms)) });
      await onRefresh();
      setMessage("Manual zone deleted and audit evidence recorded.");
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "The manual zone could not be deleted.");
    } finally {
      setBusy(null);
    }
  }

  return <>
    <section className="panel"><div className="panel-head"><div><p className="eyebrow">Board configuration</p><h2>Layout designer</h2></div><span className={`cg-state-pill ${data.board?.active ? "cg-state-accepted" : "cg-state-declined"}`}>{data.board ? (data.board.active ? "active" : "inactive") : "not configured"}</span></div><div className="cg-editor-grid"><label>Built-in template<select value={templateId} onChange={(event) => setTemplateId(event.target.value)}>{data.templates.map((item) => <option key={item.template_id} value={item.template_id}>{item.display_name}</option>)}</select></label><label className="cg-checkbox"><input type="checkbox" checked={active} onChange={(event) => setActive(event.target.checked)} />Board active</label></div><p className="cg-form-help">Writes use the current revision and append a {data.board ? "board-updated" : "board-created"} audit event. The operator name below is session-local and is not authentication.</p><button className="primary" disabled={busy !== null || !operatorId.trim() || !templateId} onClick={() => void saveBoard()}>{busy === "board" ? "Saving board…" : data.board ? "Save board" : "Create board"}</button><MutationResult error={error} message={message} />{!data.board && <CgEmpty title="No CG board configured" detail="Create a board from one of the six built-in templates to unlock manual zones." />}{data.board && <><div className="cg-board-meta"><div><span>Template</span><strong>{template?.display_name ?? data.board.template_id}</strong><small>{data.board.template_id} · revision {data.board.revision}</small></div><div><span>Created by</span><strong>{data.board.created_by}</strong><small>Updated {formatCgTime(data.board.updated_at_unix_ms)}</small></div></div><div className="cg-zone-list">{data.zones.length ? data.zones.map((zone) => <article className="cg-zone-row" key={zone.zone_id}><div className="cg-zone-index">{zone.template_zone_key.slice(0, 2).toUpperCase()}</div><div><strong>{displayKind(zone.template_zone_key)}</strong><small>{displayKind(zone.zone_kind)} · {displayKind(zone.content_source.kind)}{zone.approval_required ? " · approval gate" : ""}</small>{zone.allowed_tags.length > 0 && <small>Tags: {zone.allowed_tags.join(", ")}</small>}</div><span className="cg-zone-dim">{zone.content_source.kind === "manual" ? <><button className="text-button" onClick={() => { setEditingZoneId(zone.zone_id); setZoneKey(zone.template_zone_key); setManualText(zone.content_source.text ?? ""); clearResult(); }}>Edit</button><button className="text-button danger-text" disabled={busy === zone.zone_id || !operatorId.trim()} onClick={() => void removeManualZone(zone)}>{busy === zone.zone_id ? "Deleting…" : "Delete"}</button></> : "External binding"}</span></article>) : <CgEmpty title="No zones configured" detail="Add the first manual zone below." />}</div></>}</section>
    {data.board && <section className="panel"><div className="panel-head"><div><p className="eyebrow">Manual zone text</p><h2>{editingZoneId ? "Edit manual zone" : "Add a manual zone"}</h2></div><span className="count">{availableZoneKeys.length}</span></div><MutationNotice>Only manual zone content is editable here. External feed, image, schedule, and live-video bindings remain untouched.</MutationNotice><div className="cg-editor-grid"><label>Template zone<select value={zoneKey} disabled={Boolean(editingZoneId)} onChange={(event) => setZoneKey(event.target.value)}><option value="">Choose a built-in zone key</option>{editableZoneOptions.map((item) => <option key={item.template_zone_key} value={item.template_zone_key}>{item.template_zone_key} · {displayKind(item.zone_kind)}</option>)}</select></label><label>Manual text<textarea value={manualText} maxLength={2000} onChange={(event) => setManualText(event.target.value)} placeholder="Text shown in this zone" /></label></div><div className="button-row"><button className="primary" disabled={busy !== null || !operatorId.trim() || !zoneKey || !manualText.trim()} onClick={() => void saveManualZone()}>{busy === "zone" ? "Saving zone…" : editingZoneId ? "Save manual zone" : "Add manual zone"}</button>{editingZoneId && <button className="secondary" onClick={() => { setEditingZoneId(null); setZoneKey(""); setManualText(""); }}>Cancel</button>}</div></section>}
    <section className="panel"><div className="panel-head"><div><p className="eyebrow">Built-in library</p><h2>Templates</h2></div><span className="count">{data.templates.length}</span></div><div className="cg-template-grid">{data.templates.map((item) => <article className={`cg-template-card ${item.template_id === data.board?.template_id ? "selected" : ""}`} key={item.template_id}><div className="cg-template-mini" data-kind={item.kind}><span>{item.zones.length} zones</span></div><strong>{item.display_name}</strong><small>{item.template_id} · {displayKind(item.kind)}</small></article>)}</div></section>
  </>;
}

function FeedManager({ data, channelId, operatorId, onRefresh }: { data: CgData; channelId: string; operatorId: string; onRefresh: () => Promise<void> }) {
  const [editingFeedId, setEditingFeedId] = useState<string | null>(null);
  const [kind, setKind] = useState("rss");
  const [label, setLabel] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");
  const [trustTier, setTrustTier] = useState("moderated");
  const [tags, setTags] = useState("");
  const [refreshSeconds, setRefreshSeconds] = useState("300");
  const [enabled, setEnabled] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  function clearForm() {
    setEditingFeedId(null);
    setKind("rss");
    setLabel("");
    setSourceUrl("");
    setTrustTier("moderated");
    setTags("");
    setRefreshSeconds("300");
    setEnabled(true);
  }

  function editFeed(feed: CgFeed) {
    setEditingFeedId(feed.feed_source_id);
    setKind(feed.kind);
    setLabel(feed.label);
    setSourceUrl(feed.source_url);
    setTrustTier(feed.trust_tier);
    setTags(feed.tags?.join(", ") ?? "");
    setRefreshSeconds(String(feed.refresh_seconds));
    setEnabled(feed.enabled);
    setError(null);
    setMessage(null);
  }

  async function saveFeed() {
    setError(null);
    setMessage(null);
    if (!operatorId.trim()) {
      setError("Enter an operator name before making an audited change.");
      return;
    }
    const refresh = Number(refreshSeconds);
    if (!label.trim() || !sourceUrl.trim() || !sourceUrl.startsWith("https://") || !Number.isInteger(refresh) || refresh < 60 || refresh > 86400) {
      setError("Enter a label, an HTTPS source URL, and a refresh interval from 60 to 86,400 seconds.");
      return;
    }
    const tagValues = tags.split(/[\s,]+/).map((tag) => tag.trim().toLowerCase()).filter(Boolean);
    if (tagValues.some((tag) => !/^[a-z0-9_-]{1,40}$/.test(tag)) || new Set(tagValues).size !== tagValues.length) {
      setError("Tags must be unique lowercase slugs using letters, numbers, hyphens, or underscores.");
      return;
    }
    setBusy("feed");
    try {
      const now = Date.now();
      const existing = editingFeedId ? data.feeds.find((feed) => feed.feed_source_id === editingFeedId) : undefined;
      const feed: CgFeed = existing
        ? { ...existing, kind, label: label.trim(), source_url: sourceUrl.trim(), trust_tier: trustTier, tags: tagValues, refresh_seconds: refresh, enabled, revision: existing.revision + 1, updated_at_unix_ms: Math.max(now, existing.updated_at_unix_ms) }
        : { feed_source_id: newUuid(), channel_id: channelId, kind, label: label.trim(), source_url: sourceUrl.trim(), trust_tier: trustTier, tags: tagValues, refresh_seconds: refresh, enabled, revision: 1, created_at_unix_ms: now, updated_at_unix_ms: now, created_by: operatorId.trim(), last_fetched_at_unix_ms: null, last_fetch_error: null };
      await cgApi.writeFeed({ feed, expected_revision: existing?.revision ?? 0, audit: buildAudit(channelId, data.board?.board_id ?? null, existing ? "feed_updated" : "feed_created", operatorId, { kind, enabled: String(enabled) }, feed.updated_at_unix_ms) });
      await onRefresh();
      clearForm();
      setMessage(existing ? "Feed source updated and audit evidence recorded." : "Feed source registered and audit evidence recorded.");
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "The feed source could not be saved.");
    } finally {
      setBusy(null);
    }
  }

  async function removeFeed(feed: CgFeed) {
    if (!operatorId.trim() || !window.confirm(`Delete the feed source “${feed.label}”?`)) return;
    setError(null);
    setMessage(null);
    setBusy(feed.feed_source_id);
    try {
      await cgApi.deleteFeed(feed.feed_source_id, { expected_revision: feed.revision, audit: buildAudit(channelId, data.board?.board_id ?? null, "feed_removed", operatorId, { label: feed.label }, Math.max(Date.now(), feed.updated_at_unix_ms)) });
      await onRefresh();
      if (editingFeedId === feed.feed_source_id) clearForm();
      setMessage("Feed source deleted and audit evidence recorded.");
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "The feed source could not be deleted.");
    } finally {
      setBusy(null);
    }
  }

  return <>
    <section className="panel"><div className="panel-head"><div><p className="eyebrow">External sources</p><h2>{editingFeedId ? "Edit feed source" : "Add feed source"}</h2></div><span className="count">{data.feeds.length}</span></div><MutationNotice>These sources are unauthenticated HTTPS registrations. No credentials are collected or implied; fetch receipts and failures remain visible below.</MutationNotice><div className="cg-editor-grid"><label>Kind<select value={kind} onChange={(event) => setKind(event.target.value)}><option value="rss">RSS</option><option value="ical">iCal</option><option value="caldav">CalDAV</option><option value="weather">Weather</option><option value="social">Social</option></select></label><label>Trust tier<select value={trustTier} onChange={(event) => setTrustTier(event.target.value)}><option value="station">Station</option><option value="trusted">Trusted</option><option value="moderated">Moderated</option></select></label><label>Label<input maxLength={160} value={label} onChange={(event) => setLabel(event.target.value)} placeholder="Town notices" /></label><label>HTTPS source URL<input type="url" maxLength={500} value={sourceUrl} onChange={(event) => setSourceUrl(event.target.value)} placeholder="https://example.gov/notices.xml" /></label><label>Refresh seconds<input type="number" min={60} max={86400} value={refreshSeconds} onChange={(event) => setRefreshSeconds(event.target.value)} /></label><label>Tags <span className="cg-form-help">comma-separated slugs</span><input value={tags} onChange={(event) => setTags(event.target.value)} placeholder="public-safety, community" /></label></div><label className="cg-checkbox"><input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />Source enabled</label><div className="button-row"><button className="primary" disabled={busy !== null || !operatorId.trim()} onClick={() => void saveFeed()}>{busy === "feed" ? "Saving feed…" : editingFeedId ? "Save feed" : "Register feed"}</button>{editingFeedId && <button className="secondary" onClick={clearForm}>Cancel</button>}</div><MutationResult error={error} message={message} /></section>
    <section className="panel"><div className="panel-head"><div><p className="eyebrow">Registered sources</p><h2>Feed health</h2></div><span className="count">{data.feeds.length}</span></div>{data.feeds.length === 0 ? <CgEmpty title="No feeds registered" detail="Add an unauthenticated HTTPS source above. The fetcher will report its first receipt or failure here." /> : <div className="cg-list">{data.feeds.map((feed) => <article className="cg-card" key={feed.feed_source_id}><div className="cg-card-top"><div><strong>{feed.label}</strong><small>{displayKind(feed.kind)} · {displayKind(feed.trust_tier)} · every {feed.refresh_seconds}s</small></div><span className={`cg-state-pill ${feed.enabled ? "cg-state-accepted" : "cg-state-declined"}`}>{feed.enabled ? "enabled" : "disabled"}</span></div><code className="cg-url">{feed.source_url}</code>{feed.last_fetch_error ? <p className="cg-feed-error">{feed.last_fetch_error}</p> : <p>{feed.last_fetched_at_unix_ms ? `Last fetched ${formatCgTime(feed.last_fetched_at_unix_ms)}.` : "No fetch receipt yet; no content is fabricated in preview."}</p>}<div className="cg-card-foot"><small>Owner {feed.created_by} · revision {feed.revision}{feed.tags?.length ? ` · tags ${feed.tags.join(", ")}` : ""}</small><div className="cg-inline-actions"><button className="secondary" onClick={() => editFeed(feed)}>Edit</button><button className="secondary danger-button" disabled={busy === feed.feed_source_id || !operatorId.trim()} onClick={() => void removeFeed(feed)}>{busy === feed.feed_source_id ? "Deleting…" : "Delete"}</button></div></div></article>)}</div>}</section>
    <FeedItemApprovalQueue data={data} channelId={channelId} operatorId={operatorId} onRefresh={onRefresh} />
  </>;
}

function FeedItemApprovalQueue({ data, channelId, operatorId, onRefresh }: { data: CgData; channelId: string; operatorId: string; onRefresh: () => Promise<void> }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const feeds = data.feedItems.feeds;
  const pendingCount = feeds.reduce((total, feed) => total + feed.items.filter((item) => !item.approved).length, 0);

  async function approve(feed: CgFeedRead, item: CgFeedItemRead) {
    if (item.approved || busy || !operatorId.trim()) return;
    setError(null);
    setMessage(null);
    const now = Date.now();
    setBusy(`${feed.feed_source_id}:${item.item_id}`);
    try {
      await cgApi.approveFeedItem({
        approval: {
          feed_source_id: feed.feed_source_id,
          item_id: item.item_id,
          approved_by: operatorId.trim(),
          approved_at_unix_ms: now,
        },
        audit: buildAudit(channelId, data.board?.board_id ?? null, "feed_item_approved", operatorId, { feed_source_id: feed.feed_source_id, item_id: item.item_id }, now),
      });
      await onRefresh();
      setMessage(`Approved “${item.title}”; audit evidence recorded.`);
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "The feed item could not be approved.");
    } finally {
      setBusy(null);
    }
  }

  return <section className="panel"><div className="panel-head"><div><p className="eyebrow">Moderated cache</p><h2>Feed-item approvals</h2></div><span className="count">{pendingCount} pending</span></div><MutationNotice>Only cached items are shown. This screen does not fetch the network; approval is durable, channel-scoped, and append-only.</MutationNotice>{feeds.length === 0 ? <CgEmpty title="No configured feeds" detail="Register a feed source before reviewing cached items." /> : <div className="cg-list">{feeds.map((feed) => { const fetchError = feed.last_fetch_error ?? feed.cache_last_error; const state = feed.never_fetched ? "never fetched" : fetchError ? "fetch error" : feed.stale ? "stale cache" : "fresh cache"; return <article className="cg-card" key={feed.feed_source_id}><div className="cg-card-top"><div><strong>{feed.label}</strong><small>{displayKind(feed.kind)} · {feed.items.length} cached item{feed.items.length === 1 ? "" : "s"}</small></div><span className={`cg-state-pill ${feed.never_fetched || fetchError ? "cg-state-declined" : feed.stale ? "cg-state-submitted" : "cg-state-accepted"}`}>{state}</span></div>{feed.never_fetched ? <p>No successful cache exists yet. Network fetching is not part of this slice.</p> : fetchError ? <p className="cg-feed-error">{fetchError}</p> : <p>Cache fetched {feed.cache_fetched_at_unix_ms ? formatCgTime(feed.cache_fetched_at_unix_ms) : "at an unknown time"}{feed.cache_expires_at_unix_ms ? ` · expires ${formatCgTime(feed.cache_expires_at_unix_ms)}` : ""}.</p>}{feed.items.length > 0 && <div className="cg-feed-item-list">{feed.items.map((item) => <div className="cg-feed-item" key={item.item_id}><div><strong>{item.title}</strong><small>{item.tags.length ? item.tags.join(", ") : "untagged"}{item.starts_at_unix_ms ? ` · ${formatCgTime(item.starts_at_unix_ms)}` : ""}</small>{item.summary && <p>{item.summary}</p>}</div><button className={item.approved ? "secondary" : "primary"} disabled={item.approved || busy !== null || !operatorId.trim()} onClick={() => void approve(feed, item)}>{item.approved ? `Approved by ${item.approved_by ?? "operator"}` : busy === `${feed.feed_source_id}:${item.item_id}` ? "Approving…" : "Approve"}</button></div>)}</div>}</article>; })}</div>}<MutationResult error={error} message={message} /></section>;
}

function ResolvedContent({ content }: { content: CgResolvedContent }) {
  switch (content.kind) {
    case "manual_text":
      return <div className="cg-preview-content"><strong>Manual text</strong><p>{content.text || "(empty)"}</p></div>;
    case "clock":
      return <div className="cg-preview-content"><strong>Clock</strong><code>{content.format}</code></div>;
    case "bulletin":
      if (content.content.kind === "text") {
        return <div className="cg-preview-content cg-preview-bulletin"><strong>{content.content.title}</strong><p>{content.content.body}</p><small>Bulletin {content.bulletin_id}</small></div>;
      }
      return <div className="cg-preview-placeholder"><strong>Unsupported bulletin media</strong><p>{displayKind(content.content.kind)} is resolved but not rendered by this handoff preview.</p><small>Asset/source readiness is not invented.</small></div>;
    case "feed_items":
      return <div className="cg-preview-content cg-preview-feed"><div className="cg-preview-content-head"><strong>Feed items</strong>{content.stale && <span className="cg-state-pill cg-state-submitted">stale cache</span>}</div>{content.last_error && <p className="cg-feed-error">Feed error: {content.last_error}</p>}{content.items.length === 0 ? <p>No resolved items.</p> : <ul>{content.items.map((item) => <li key={item.item_id}><strong>{item.title}</strong>{item.summary && <span>{item.summary}</span>}<small>{item.tags.length ? item.tags.join(", ") : "untagged"}</small></li>)}</ul>}</div>;
    case "placeholder":
      return <div className="cg-preview-placeholder"><strong>Unresolved {displayKind(content.source)}</strong><p>{displayKind(content.reason)}</p><small>No external content was invented.</small></div>;
  }
}

function LivePreview({ data, channelId }: { data: CgData; channelId: string }) {
  const [snapshot, setSnapshot] = useState<CgResolvedSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [atUnixMs, setAtUnixMs] = useState(() => Date.now());
  const hasBoard = data.board !== null;

  const loadPreview = useCallback(async (timestamp: number) => {
    if (!hasBoard) return;
    setLoading(true);
    setError(null);
    try {
      setSnapshot(await cgApi.preview(channelId, timestamp));
      setAtUnixMs(timestamp);
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "The resolved CG snapshot could not be read.");
    } finally {
      setLoading(false);
    }
  }, [channelId, hasBoard]);

  useEffect(() => {
    setSnapshot(null);
    setError(null);
    if (hasBoard) void loadPreview(Date.now());
  }, [channelId, data, hasBoard, loadPreview]);

  function refresh() {
    void loadPreview(Date.now());
  }

  return <section className="panel"><div className="panel-head"><div><p className="eyebrow">Resolved renderer handoff</p><h2>Live preview</h2></div><span className="cg-state-pill cg-state-accepted">resolved snapshot</span></div><div className="cg-preview-warning"><strong>Deterministic snapshot handoff — not on-air pixel parity.</strong><p>This renders the server-resolved snapshot at a supplied timestamp. It does not claim pixel parity with the on-air worker or prove media/output readiness.</p></div>{!data.board ? <CgEmpty title="Preview unavailable" detail="Configure a board before a resolved snapshot can be assembled." /> : <div className="cg-preview-wrap"><div className="cg-preview-controls"><span>Resolved at {formatCgTime(atUnixMs)}</span><button className="secondary" onClick={refresh} disabled={loading}>{loading ? "Resolving…" : "Refresh snapshot"}</button></div>{loading && !snapshot ? <div className="cg-preview-status">Resolving board, zones, and airable content…</div> : error ? <div className="cg-preview-status cg-preview-status-error"><strong>Snapshot unavailable</strong><p>{error}</p><button className="secondary" onClick={refresh}>Retry</button></div> : snapshot ? <><div className="cg-preview-meta"><span>Board revision {snapshot.board_revision}</span><span>Template {displayKind(snapshot.template_kind)}</span><span>Generated {formatCgTime(snapshot.generated_at_unix_ms)}</span></div><div className="cg-preview-stage" aria-label="Resolved CG snapshot"><div className="cg-preview-stage-label">Resolved snapshot</div>{snapshot.zones.length ? snapshot.zones.map((zone) => <div className="cg-preview-zone" key={zone.zone_id} style={{ left: `${zone.bounds.x / 10}%`, top: `${zone.bounds.y / 10}%`, width: `${zone.bounds.width / 10}%`, height: `${zone.bounds.height / 10}%`, zIndex: zone.z_index }}><span className="cg-preview-zone-label">{displayKind(zone.template_zone_key)} · {displayKind(zone.zone_kind)}</span><ResolvedContent content={zone.content} /></div>) : <span className="cg-preview-empty">No zones resolved</span>}</div><div className="cg-preview-legend"><span><i className="cg-legend-configured" />Resolved zone</span><span><i className="cg-legend-unresolved" />Typed unresolved placeholder</span></div></> : <CgEmpty title="No resolved snapshot" detail="Refresh to request a deterministic snapshot for this channel." />}</div>}</section>;
}

function SettingsHistory({ data }: { data: CgData }) {
  return <>
    <section className="panel"><div className="panel-head"><div><p className="eyebrow">Board identity</p><h2>Settings</h2></div><span className="count">{data.board?.revision ?? "—"}</span></div>{data.board ? <dl className="cg-definition-list"><div><dt>Board ID</dt><dd><code>{data.board.board_id}</code></dd></div><div><dt>Channel ID</dt><dd><code>{data.board.channel_id}</code></dd></div><div><dt>Template</dt><dd>{data.board.template_id}</dd></div><div><dt>State</dt><dd>{data.board.active ? "Active" : "Inactive"}</dd></div><div><dt>Created</dt><dd>{formatCgTime(data.board.created_at_unix_ms)} by {data.board.created_by}</dd></div><div><dt>Updated</dt><dd>{formatCgTime(data.board.updated_at_unix_ms)}</dd></div></dl> : <CgEmpty title="No board settings" detail="Board metadata becomes available after the first audited board is created." />}</section>
    <section className="panel"><div className="panel-head"><div><p className="eyebrow">Append-only evidence</p><h2>History</h2></div><span className="count">{data.audit.length}</span></div>{data.audit.length === 0 ? <CgEmpty title="No audit events" detail="Board, zone, feed, bulletin, and approval events will remain here across restarts." /> : <div className="cg-timeline">{data.audit.map((event) => <article key={event.audit_id}><span className="cg-timeline-dot" /><div><strong>{displayKind(event.event_kind)}</strong><small>{formatCgTime(event.occurred_at_unix_ms)} · {event.operator_id}</small>{Object.keys(event.details).length > 0 && <code>{JSON.stringify(event.details)}</code>}</div></article>)}</div>}</section>
  </>;
}

async function loadCgData(channelId: string): Promise<CgData> {
  const [templates, feeds, feedItems, bulletins, audit] = await Promise.all([
    cgApi.templates(),
    cgApi.feeds(channelId),
    cgApi.feedItems(channelId),
    cgApi.bulletins(channelId),
    cgApi.audit(channelId),
  ]);
  let board: CgBoard | null = null;
  try {
    board = await cgApi.board(channelId);
  } catch (error) {
    if (!(error instanceof CgApiError && error.status === 404)) throw error;
  }
  const zones = board ? await cgApi.zones(board.board_id) : [];
  return { templates, board, zones, feeds, feedItems, bulletins, audit };
}

export function CgOperator({ channelId, channels, route, onNavigate }: { channelId: string | undefined; channels: CgChannelOption[]; route: CgRoute; onNavigate: (route: CgRoute) => void }) {
  const [selectedChannelId, setSelectedChannelId] = useState(channelId);
  const [operatorId, setOperatorId] = useState(readSessionOperator);
  const [data, setData] = useState<CgData | undefined>();
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  useEffect(() => { setSelectedChannelId(channelId); }, [channelId]);
  const activeChannelId = selectedChannelId ?? channelId;
  function updateOperatorId(value: string) {
    setOperatorId(value);
    writeSessionOperator(value);
  }
  const load = useCallback(async () => {
    if (!activeChannelId) return;
    setRefreshing(true);
    setError(null);
    try {
      setData(await loadCgData(activeChannelId));
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "The CG configuration could not be read.");
    } finally {
      setRefreshing(false);
    }
  }, [activeChannelId]);
  useEffect(() => { setData(undefined); void load(); }, [load]);
  if (!activeChannelId) return <><header className="page-header"><div><p className="eyebrow">CG / Bulletin board</p><h1>Choose a channel first.</h1><p>CG boards are durable per-channel configurations.</p></div></header><section className="panel"><CgEmpty title="No output channel configured" detail="Configure a channel before opening its CG board, feeds, or bulletin queue." /></section></>;
  return <>
    <header className="page-header"><div><p className="eyebrow">CG / Bulletin board</p><h1>{CG_ROUTES.find((item) => item.id === route)?.label ?? "Operator tools"}</h1><p>Authoring and management contract</p></div><div className="header-actions"><OperatorIdentity value={operatorId} onChange={updateOperatorId} /><label className="cg-channel-picker">Channel<select value={activeChannelId} onChange={(event) => setSelectedChannelId(event.target.value)}>{channels.map((channel) => <option key={channel.channel_id} value={channel.channel_id}>{channel.display_name}</option>)}</select></label><span className="cg-channel-badge">{data?.board?.active ? "Board active" : "Board not configured"}</span><button className="secondary" onClick={() => void load()} disabled={refreshing}>{refreshing ? "Reading…" : "Refresh"}</button></div></header>
    <CgTabs route={route} onNavigate={onNavigate} />
    {error ? <CgError error={error} onRetry={() => void load()} /> : !data ? <CgLoading /> : route === "cg-moderation" ? <ModerationQueue data={data} channelId={activeChannelId} operatorId={operatorId} onRefresh={load} /> : route === "cg-board" ? <BoardDesigner data={data} channelId={activeChannelId} operatorId={operatorId} onRefresh={load} /> : route === "cg-feeds" ? <FeedManager data={data} channelId={activeChannelId} operatorId={operatorId} onRefresh={load} /> : route === "cg-preview" ? <LivePreview data={data} channelId={activeChannelId} /> : <SettingsHistory data={data} />}
  </>;
}
