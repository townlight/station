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
  bulletins: (channelId: string) => cgRequest<CgBulletin[]>(`/api/v1/cg/bulletins?channel_id=${encodeURIComponent(channelId)}`),
  audit: (channelId: string) => cgRequest<CgAuditEvent[]>(`/api/v1/cg/audit?channel_id=${encodeURIComponent(channelId)}&limit=50`),
  writeBoard: (body: { board: CgBoard; expected_revision: number; audit: CgAuditEvent }) => cgRequest<CgBoard>("/api/v1/cg/board", { method: "PUT", body: JSON.stringify(body) }),
  writeZone: (body: { zone: CgZone; expected_revision: number; audit: CgAuditEvent }) => cgRequest<CgZone>("/api/v1/cg/zones", { method: "PUT", body: JSON.stringify(body) }),
  deleteZone: (zoneId: string, body: { expected_revision: number; audit: CgAuditEvent }) => cgRequest<void>(`/api/v1/cg/zones/${encodeURIComponent(zoneId)}`, { method: "DELETE", body: JSON.stringify(body) }),
  writeFeed: (body: { feed: CgFeed; expected_revision: number; audit: CgAuditEvent }) => cgRequest<CgFeed>("/api/v1/cg/feeds", { method: "PUT", body: JSON.stringify(body) }),
  deleteFeed: (feedId: string, body: { expected_revision: number; audit: CgAuditEvent }) => cgRequest<void>(`/api/v1/cg/feeds/${encodeURIComponent(feedId)}`, { method: "DELETE", body: JSON.stringify(body) }),
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

function ModerationQueue({ data }: { data: CgData }) {
  const counts = useMemo(() => data.bulletins.reduce<Record<string, number>>((result, bulletin) => ({ ...result, [bulletin.state]: (result[bulletin.state] ?? 0) + 1 }), {}), [data.bulletins]);
  return <>
    <div className="cg-stat-strip">{["submitted", "needs_changes", "accepted", "scheduled", "declined"].map((state) => <div key={state}><span>{displayKind(state)}</span><strong>{counts[state] ?? 0}</strong></div>)}</div>
    <section className="panel"><div className="panel-head"><div><p className="eyebrow">Durable bulletin lifecycle</p><h2>Review queue</h2></div><span className="count">{data.bulletins.length}</span></div><MutationNotice>Approve, request changes, and decline controls are withheld until the operator identity and moderation mutation contract are wired into this console.</MutationNotice>{data.bulletins.length === 0 ? <CgEmpty title="No bulletins in the queue" detail="Submissions will appear here after the bulletin intake path is connected." /> : <div className="cg-list">{data.bulletins.map((bulletin) => <article className="cg-card" key={bulletin.bulletin_id}><div className="cg-card-top"><div><strong>{bulletin.content.title ?? displayKind(bulletin.content.kind)}</strong><small>{bulletin.submitted_by} · {formatCgTime(bulletin.created_at_unix_ms)}</small></div><span className={`cg-state-pill cg-state-${bulletin.state}`}>{displayKind(bulletin.state)}</span></div><p>{bulletin.content.body ?? bulletin.content.alt_text ?? "Media bulletin; renderer details remain in the source contract."}</p><div className="cg-tags">{bulletin.tags.length ? bulletin.tags.map((tag) => <span key={tag}>{tag}</span>) : <span>untagged</span>}<small>{bulletin.display_seconds}s · revision {bulletin.revision}</small></div></article>)}</div>}</section>
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
  </>;
}

function LivePreview({ data }: { data: CgData }) {
  const template = data.templates.find((item) => item.template_id === data.board?.template_id);
  return <section className="panel"><div className="panel-head"><div><p className="eyebrow">Renderer handoff</p><h2>Live preview</h2></div><span className="cg-state-pill cg-state-declined">contract only</span></div><div className="cg-preview-warning"><strong>This is not an on-air preview.</strong><p>The current API exposes board configuration, not a resolved runtime snapshot. This view shows the stored geometry and source bindings without inventing feed, schedule, image, or live-video data.</p></div>{!data.board ? <CgEmpty title="Preview unavailable" detail="Configure a board before a contract preview can be assembled." /> : <div className="cg-preview-wrap"><div className="cg-preview-stage" aria-label="Stored CG zone geometry">{data.zones.length ? data.zones.map((zone) => { const templateZone = template?.zones.find((item) => item.template_zone_key === zone.template_zone_key); const bounds = templateZone?.bounds ?? { x: 0, y: 0, width: 1000, height: 1000 }; return <div className="cg-preview-zone" key={zone.zone_id} style={{ left: `${bounds.x / 10}%`, top: `${bounds.y / 10}%`, width: `${bounds.width / 10}%`, height: `${bounds.height / 10}%`, zIndex: templateZone?.z_index ?? 0 }}><strong>{displayKind(zone.template_zone_key)}</strong><small>{displayKind(zone.content_source.kind)}</small></div>; }) : <span className="cg-preview-empty">No zone bindings</span>}</div><div className="cg-preview-legend"><span><i className="cg-legend-configured" />Stored zone</span><span><i className="cg-legend-unresolved" />External content unresolved</span></div></div>}</section>;
}

function SettingsHistory({ data }: { data: CgData }) {
  return <>
    <section className="panel"><div className="panel-head"><div><p className="eyebrow">Board identity</p><h2>Settings</h2></div><span className="count">{data.board?.revision ?? "—"}</span></div>{data.board ? <dl className="cg-definition-list"><div><dt>Board ID</dt><dd><code>{data.board.board_id}</code></dd></div><div><dt>Channel ID</dt><dd><code>{data.board.channel_id}</code></dd></div><div><dt>Template</dt><dd>{data.board.template_id}</dd></div><div><dt>State</dt><dd>{data.board.active ? "Active" : "Inactive"}</dd></div><div><dt>Created</dt><dd>{formatCgTime(data.board.created_at_unix_ms)} by {data.board.created_by}</dd></div><div><dt>Updated</dt><dd>{formatCgTime(data.board.updated_at_unix_ms)}</dd></div></dl> : <CgEmpty title="No board settings" detail="Board metadata becomes available after the first audited board is created." />}</section>
    <section className="panel"><div className="panel-head"><div><p className="eyebrow">Append-only evidence</p><h2>History</h2></div><span className="count">{data.audit.length}</span></div>{data.audit.length === 0 ? <CgEmpty title="No audit events" detail="Board, zone, feed, bulletin, and approval events will remain here across restarts." /> : <div className="cg-timeline">{data.audit.map((event) => <article key={event.audit_id}><span className="cg-timeline-dot" /><div><strong>{displayKind(event.event_kind)}</strong><small>{formatCgTime(event.occurred_at_unix_ms)} · {event.operator_id}</small>{Object.keys(event.details).length > 0 && <code>{JSON.stringify(event.details)}</code>}</div></article>)}</div>}</section>
  </>;
}

async function loadCgData(channelId: string): Promise<CgData> {
  const [templates, feeds, bulletins, audit] = await Promise.all([
    cgApi.templates(),
    cgApi.feeds(channelId),
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
  return { templates, board, zones, feeds, bulletins, audit };
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
    {error ? <CgError error={error} onRetry={() => void load()} /> : !data ? <CgLoading /> : route === "cg-moderation" ? <ModerationQueue data={data} /> : route === "cg-board" ? <BoardDesigner data={data} channelId={activeChannelId} operatorId={operatorId} onRefresh={load} /> : route === "cg-feeds" ? <FeedManager data={data} channelId={activeChannelId} operatorId={operatorId} onRefresh={load} /> : route === "cg-preview" ? <LivePreview data={data} /> : <SettingsHistory data={data} />}
  </>;
}
