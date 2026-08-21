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

async function cgRequest<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" } });
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

function BoardDesigner({ data }: { data: CgData }) {
  const template = data.templates.find((item) => item.template_id === data.board?.template_id);
  return <>
    <section className="panel"><div className="panel-head"><div><p className="eyebrow">Board configuration</p><h2>Layout designer</h2></div><span className={`cg-state-pill ${data.board?.active ? "cg-state-accepted" : "cg-state-declined"}`}>{data.board ? (data.board.active ? "active" : "inactive") : "not configured"}</span></div><MutationNotice>Template selection and zone writes are shown as a contract surface but are not enabled yet.</MutationNotice>{!data.board ? <CgEmpty title="No CG board configured" detail="Create the first board through the audited station setup workflow before designing zones." /> : <><div className="cg-board-meta"><div><span>Template</span><strong>{template?.display_name ?? data.board.template_id}</strong><small>{data.board.template_id} · revision {data.board.revision}</small></div><div><span>Created by</span><strong>{data.board.created_by}</strong><small>Updated {formatCgTime(data.board.updated_at_unix_ms)}</small></div></div><div className="cg-zone-list">{data.zones.length ? data.zones.map((zone) => <article className="cg-zone-row" key={zone.zone_id}><div className="cg-zone-index">{zone.template_zone_key.slice(0, 2).toUpperCase()}</div><div><strong>{displayKind(zone.template_zone_key)}</strong><small>{displayKind(zone.zone_kind)} · {displayKind(zone.content_source.kind)}{zone.approval_required ? " · approval gate" : ""}</small>{zone.allowed_tags.length > 0 && <small>Tags: {zone.allowed_tags.join(", ")}</small>}</div><span className="cg-zone-dim">{zone.rotation_seconds ? `${zone.rotation_seconds}s` : "static"}</span></article>) : <CgEmpty title="No zones configured" detail="The board has no durable zone bindings yet." />}</div><button className="secondary wide" disabled>Save board changes · unavailable</button></>}</section>
    <section className="panel"><div className="panel-head"><div><p className="eyebrow">Built-in library</p><h2>Templates</h2></div><span className="count">{data.templates.length}</span></div><div className="cg-template-grid">{data.templates.map((item) => <article className={`cg-template-card ${item.template_id === data.board?.template_id ? "selected" : ""}`} key={item.template_id}><div className="cg-template-mini" data-kind={item.kind}><span>{item.zones.length} zones</span></div><strong>{item.display_name}</strong><small>{item.template_id} · {displayKind(item.kind)}</small></article>)}</div></section>
  </>;
}

function FeedManager({ data }: { data: CgData }) {
  return <section className="panel"><div className="panel-head"><div><p className="eyebrow">External sources</p><h2>Feed manager</h2></div><span className="count">{data.feeds.length}</span></div><MutationNotice>Feed registration and deletion controls are intentionally disabled until credential handling and audited mutation forms are connected.</MutationNotice>{data.feeds.length === 0 ? <CgEmpty title="No feeds registered" detail="RSS, iCal, CalDAV, weather, and social sources will appear after they are registered." /> : <div className="cg-list">{data.feeds.map((feed) => <article className="cg-card" key={feed.feed_source_id}><div className="cg-card-top"><div><strong>{feed.label}</strong><small>{displayKind(feed.kind)} · {displayKind(feed.trust_tier)} · every {feed.refresh_seconds}s</small></div><span className={`cg-state-pill ${feed.enabled ? "cg-state-accepted" : "cg-state-declined"}`}>{feed.enabled ? "enabled" : "disabled"}</span></div><code className="cg-url">{feed.source_url}</code>{feed.last_fetch_error ? <p className="cg-feed-error">{feed.last_fetch_error}</p> : <p>{feed.last_fetched_at_unix_ms ? `Last fetched ${formatCgTime(feed.last_fetched_at_unix_ms)}.` : "No fetch receipt yet; no content is fabricated in preview."}</p>}<div className="cg-card-foot"><small>Owner {feed.created_by} · revision {feed.revision}</small><button className="secondary" disabled>Manage · unavailable</button></div></article>)}</div>}</section>;
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
  const [data, setData] = useState<CgData | undefined>();
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  useEffect(() => { setSelectedChannelId(channelId); }, [channelId]);
  const activeChannelId = selectedChannelId ?? channelId;
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
    <header className="page-header"><div><p className="eyebrow">CG / Bulletin board</p><h1>{CG_ROUTES.find((item) => item.id === route)?.label ?? "Operator tools"}</h1><p>Authoring and management contract</p></div><div className="header-actions"><label className="cg-channel-picker">Channel<select value={activeChannelId} onChange={(event) => setSelectedChannelId(event.target.value)}>{channels.map((channel) => <option key={channel.channel_id} value={channel.channel_id}>{channel.display_name}</option>)}</select></label><span className="cg-channel-badge">{data?.board?.active ? "Board active" : "Board not configured"}</span><button className="secondary" onClick={() => void load()} disabled={refreshing}>{refreshing ? "Reading…" : "Refresh"}</button></div></header>
    <CgTabs route={route} onNavigate={onNavigate} />
    {error ? <CgError error={error} onRetry={() => void load()} /> : !data ? <CgLoading /> : route === "cg-moderation" ? <ModerationQueue data={data} /> : route === "cg-board" ? <BoardDesigner data={data} /> : route === "cg-feeds" ? <FeedManager data={data} /> : route === "cg-preview" ? <LivePreview data={data} /> : <SettingsHistory data={data} />}
  </>;
}
