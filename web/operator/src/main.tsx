import React, { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

type View = "overview" | "media" | "channels" | "schedule" | "health" | "commissioning";
type JobStatus = "pending" | "running" | "awaiting_transcode" | "completed" | "failed" | "cancelled";

interface StationDocument {
  station_id: string;
  display_name: string;
  timezone: string;
  revision: number;
}

interface IngestJob {
  job_id: string;
  source_path: string;
  status: JobStatus;
  progress_percent: number;
  error_detail: string | null;
  asset_id: string | null;
  attempts: number;
}

interface TranscodeJob {
  job_id: string;
  ingest_job_id: string;
  source_asset_id: string;
  output_asset_id: string | null;
  status: Exclude<JobStatus, "awaiting_transcode">;
  progress_percent: number;
  error_detail: string | null;
  attempts: number;
}

interface AssetView {
  asset_id: string;
  readiness_state: "ready" | "processing" | "missing_file" | "rejected";
  readiness_reason: string | null;
  media_path: string;
  duration_ms: number;
  loudness_status: "ok" | "failed" | "not_checked";
  measured_lufs: number | null;
  target_lufs: number;
  loudness_detail: string | null;
}

interface Dashboard {
  total_assets: number;
  ready_count: number;
  processing_count: number;
  missing_count: number;
  rejected_count: number;
  pending_ingest_count: number;
  running_ingest_count: number;
  awaiting_transcode_ingest_count: number;
  failed_ingest_count: number;
  pending_transcode_count: number;
  running_transcode_count: number;
  failed_transcode_count: number;
  by_asset: AssetView[];
  ingest_jobs: IngestJob[];
  transcode_jobs: TranscodeJob[];
}

interface Channel {
  channel_id: string;
  display_name: string;
  udp_destination: string;
  enabled: boolean;
}

interface ScheduleItem {
  item_id: string;
  channel_id: string;
  asset_id: string;
  title: string;
  starts_at_unix_ms: number;
  duration_ms: number;
  state: "draft" | "committed" | "cancelled";
}

interface CommitPlan {
  plan_id: string;
  dry_run_passed: boolean;
  conflicts: unknown[];
  missing_media_detail: string | null;
  gaps: unknown[];
}

type SafeToAirColor = "green" | "yellow" | "red";

interface ChannelRuntimeStatus {
  channel_id: string;
  display_name: string;
  state: "starting" | "healthy_fallback" | "on_air" | "error" | "stopped";
  on_air: boolean;
  on_healthy_fallback: boolean;
  seconds_in_state: number;
  heartbeat_age_seconds: number;
  color: SafeToAirColor;
  operator_message: string;
}

interface RuntimeSafeToAirStatus {
  generated_at_unix_ms: number;
  color: SafeToAirColor;
  label: string;
  operator_message: string;
  channels: ChannelRuntimeStatus[];
  active_critical_alerts: number;
  active_warning_alerts: number;
}

interface AlertEvent {
  event_id: string;
  condition: string;
  severity: "critical" | "warning" | "info";
  state: "firing" | "resolved";
  resource_ref: string;
  summary: string;
  detail: string;
  source_section: string;
  first_observed_at_unix_ms: number;
  last_observed_at_unix_ms: number;
  resolved_at_unix_ms: number | null;
  occurrence_count: number;
  acknowledged_at_unix_ms: number | null;
  acknowledged_by: string | null;
}

type AlertChannelKind = "email" | "sms" | "webhook";

interface AlertChannel {
  channel_id: string;
  kind: AlertChannelKind;
  label: string;
  enabled: boolean;
  target_redacted: string;
  credential_configured: boolean;
  quiet_hours_start_utc: string | null;
  quiet_hours_end_utc: string | null;
  created_at_unix_ms: number;
  updated_at_unix_ms: number;
}

interface AlertDelivery {
  delivery_id: string;
  event_id: string;
  alert_channel_id: string;
  notification_kind: "firing" | "resolved" | "test";
  state: "pending" | "leased" | "sent" | "dead_letter";
  attempts: number;
  next_attempt_at_unix_ms: number;
  lease_expires_at_unix_ms: number | null;
  last_error: string;
  signature: string | null;
  created_at_unix_ms: number;
  updated_at_unix_ms: number;
  sent_at_unix_ms: number | null;
}

function initialView(): View {
  const requested = new URLSearchParams(window.location.search).get("view");
  return ["overview", "media", "channels", "schedule", "health", "commissioning"].includes(requested ?? "")
    ? requested as View
    : "overview";
}

interface CommitReport {
  report_id: string;
  schedule_item_id: string;
  dispatch_status: "pending" | "queued" | "acknowledged" | "completed" | "error" | "cancelled";
  operator_notes: string;
}

class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  const body = await response.json().catch(() => null) as { message?: string } | T | null;
  if (!response.ok) {
    const message = body && typeof body === "object" && "message" in body
      ? String(body.message)
      : `Request failed with HTTP ${response.status}.`;
    throw new ApiError(response.status, message);
  }
  return body as T;
}

function formatDuration(milliseconds: number): string {
  const seconds = Math.max(0, Math.round(milliseconds / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  return hours > 0 ? `${hours}h ${minutes}m` : minutes > 0 ? `${minutes}m ${remainder}s` : `${remainder}s`;
}

function formatTime(milliseconds: number): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(milliseconds);
}

function StatusPill({ status }: { status: string }) {
  const tone = ["ready", "completed", "acknowledged", "ok", "enabled", "healthy_fallback", "on_air", "sent"].includes(status)
    ? "good"
    : ["failed", "error", "missing_file", "rejected", "stopped", "critical", "dead_letter"].includes(status)
      ? "bad"
      : "active";
  return <span className={`pill pill-${tone}`}>{status.replaceAll("_", " ")}</span>;
}

function Icon({ name }: { name: View }) {
  const paths: Record<View, React.ReactNode> = {
    overview: <><path d="M4 13h6V4H4zM14 20h6v-9h-6zM4 20h6v-3H4zM14 7h6V4h-6z" /></>,
    media: <><rect x="3" y="5" width="18" height="14" rx="2" /><path d="m10 9 5 3-5 3z" /></>,
    channels: <><path d="M5 8.5a10 10 0 0 0 0 7M19 8.5a10 10 0 0 1 0 7M8.5 10.5a5 5 0 0 0 0 3M15.5 10.5a5 5 0 0 1 0 3" /><circle cx="12" cy="12" r="1.5" /></>,
    schedule: <><rect x="3" y="5" width="18" height="16" rx="2" /><path d="M8 3v4M16 3v4M3 10h18M8 14h3M8 17h7" /></>,
    health: <><path d="M3 12h4l2-5 4 10 2-5h6" /><path d="M4 5a10 10 0 1 1-1 11" /></>,
    commissioning: <><path d="M12 3 4 7v5c0 5 3.4 8 8 9 4.6-1 8-4 8-9V7z" /><path d="m9 12 2 2 4-4" /></>,
  };
  return <svg className="nav-icon" viewBox="0 0 24 24" aria-hidden="true">{paths[name]}</svg>;
}

function SetupWizard({ onComplete }: { onComplete: (station: StationDocument) => void }) {
  const [step, setStep] = useState(0);
  const [displayName, setDisplayName] = useState("");
  const [timezone, setTimezone] = useState(Intl.DateTimeFormat().resolvedOptions().timeZone || "America/Denver");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function commission(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const station = await request<StationDocument>("/api/v1/station", {
        method: "PUT",
        body: JSON.stringify({
          station_id: crypto.randomUUID(),
          display_name: displayName.trim(),
          timezone: timezone.trim(),
          expected_revision: 0,
        }),
      });
      onComplete(station);
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "Commissioning could not be saved.");
    } finally {
      setBusy(false);
    }
  }

  return <main className="setup-shell">
    <section className="setup-brand">
      <div className="mark">TL</div>
      <p className="eyebrow">TownLight Station</p>
      <h1>Your station starts here.</h1>
      <p>Commission this Windows broadcast appliance, verify its local services, and put the first channel on air.</p>
      <div className="setup-proof"><span className="live-dot" /> Installed service connected</div>
    </section>
    <section className="setup-card">
      <div className="steps" aria-label="Commissioning progress">
        {[0, 1].map((value) => <span key={value} className={value <= step ? "step active" : "step"} />)}
      </div>
      {step === 0 ? <>
        <p className="eyebrow">Commissioning · Welcome</p>
        <h2>Let’s identify this station.</h2>
        <p className="muted">This profile is the durable identity used by schedules, proof reports, and support records. You can revise it later.</p>
        <div className="check-list">
          <div><span>1</span><p><strong>Station identity</strong><small>Name and local timezone</small></p></div>
          <div><span>2</span><p><strong>Broadcast setup</strong><small>Channel output and readiness</small></p></div>
          <div><span>3</span><p><strong>First-air proof</strong><small>Schedule and verify a real asset</small></p></div>
        </div>
        <button className="primary wide" onClick={() => setStep(1)}>Begin commissioning <span>→</span></button>
      </> : <form onSubmit={commission}>
        <p className="eyebrow">Commissioning · Station profile</p>
        <h2>Name this station.</h2>
        <label>Station name<input autoFocus required minLength={2} maxLength={120} value={displayName} onChange={(event) => setDisplayName(event.target.value)} placeholder="KTLT Community Television" /></label>
        <label>Local timezone<input required value={timezone} onChange={(event) => setTimezone(event.target.value)} placeholder="America/Denver" /><small>Schedules and operator timestamps use this IANA timezone.</small></label>
        {error && <div className="error-box" role="alert">{error}</div>}
        <div className="button-row"><button type="button" className="secondary" onClick={() => setStep(0)}>Back</button><button className="primary" disabled={busy || displayName.trim().length < 2}>{busy ? "Saving…" : "Create station"}</button></div>
      </form>}
    </section>
  </main>;
}

function Overview({ station, dashboard, channels, runtime, onNavigate }: { station: StationDocument; dashboard: Dashboard | null; channels: Channel[]; runtime: RuntimeSafeToAirStatus | null; onNavigate: (view: View) => void }) {
  const activeWork = (dashboard?.pending_ingest_count ?? 0) + (dashboard?.running_ingest_count ?? 0) + (dashboard?.awaiting_transcode_ingest_count ?? 0);
  const problems = (dashboard?.failed_ingest_count ?? 0) + (dashboard?.failed_transcode_count ?? 0) + (dashboard?.missing_count ?? 0);
  return <>
    <header className="page-header"><div><p className="eyebrow">Station overview</p><h1>Good evening.</h1><p>{station.display_name} is connected and locally operable.</p></div><div className="header-actions"><span className="service-ready"><span className="live-dot" /> Service ready</span><button className="primary" onClick={() => onNavigate("schedule")}>Schedule program</button></div></header>
    {runtime && <button className={`safe-banner safe-${runtime.color}`} onClick={() => onNavigate("health")}><span className="safe-indicator">{runtime.color === "green" ? "✓" : "!"}</span><span><strong>{runtime.label}</strong><small>{runtime.operator_message}</small></span><b>View health →</b></button>}
    <section className="metric-grid">
      <article className="metric"><span>Channels enabled</span><strong>{channels.filter((channel) => channel.enabled).length}<small> / {channels.length}</small></strong><p>{channels.length ? "Persistent output workers" : "Configure the first channel"}</p></article>
      <article className="metric"><span>Ready assets</span><strong>{dashboard?.ready_count ?? "—"}</strong><p>{dashboard?.total_assets ?? 0} managed media objects</p></article>
      <article className="metric"><span>Processing</span><strong>{activeWork}</strong><p>{activeWork ? "Media pipeline active" : "Queue is clear"}</p></article>
      <article className={`metric ${problems || runtime?.active_critical_alerts || runtime?.active_warning_alerts ? "attention" : ""}`}><span>Needs attention</span><strong>{problems + (runtime?.active_critical_alerts ?? 0) + (runtime?.active_warning_alerts ?? 0)}</strong><p>{problems || runtime?.active_critical_alerts || runtime?.active_warning_alerts ? "Review media and operating alerts" : "No active blockers"}</p></article>
    </section>
    <section className="split-grid">
      <article className="panel">
        <div className="panel-head"><div><p className="eyebrow">Air chain</p><h2>Channel status</h2></div><button className="text-button" onClick={() => onNavigate("channels")}>Manage channels →</button></div>
        {channels.length === 0 ? <Empty title="No channels configured" detail="Add the station’s first loopback, headend, or proof output." action="Configure channel" onAction={() => onNavigate("channels")} /> : <div className="row-list">{channels.slice(0, 5).map((channel) => <div className="data-row" key={channel.channel_id}><div className="signal-icon">◉</div><div><strong>{channel.display_name}</strong><small>{channel.udp_destination}</small></div><StatusPill status={channel.enabled ? "enabled" : "disabled"} /></div>)}</div>}
      </article>
      <article className="panel">
        <div className="panel-head"><div><p className="eyebrow">Media pipeline</p><h2>Recent ingest</h2></div><button className="text-button" onClick={() => onNavigate("media")}>Open library →</button></div>
        {!dashboard?.ingest_jobs.length ? <Empty title="No media admitted yet" detail="Import a program from this station or a configured watch folder." action="Import media" onAction={() => onNavigate("media")} /> : <div className="row-list">{dashboard.ingest_jobs.slice(0, 5).map((job) => <div className="data-row" key={job.job_id}><div className="file-icon">▶</div><div className="truncate"><strong>{job.source_path.split(/[\\/]/).pop()}</strong><small>{job.progress_percent}% · attempt {job.attempts}</small></div><StatusPill status={job.status} /></div>)}</div>}
      </article>
    </section>
  </>;
}

function Empty({ title, detail, action, onAction }: { title: string; detail: string; action: string; onAction: () => void }) {
  return <div className="empty"><div className="empty-orbit">+</div><strong>{title}</strong><p>{detail}</p><button className="secondary" onClick={onAction}>{action}</button></div>;
}

function Media({ dashboard, onRefresh }: { dashboard: Dashboard | null; onRefresh: () => Promise<void> }) {
  const [sourcePath, setSourcePath] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  async function admit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setMessage(null);
    try {
      await request<IngestJob>("/api/v1/assets/ingest", { method: "POST", body: JSON.stringify({ job_id: crypto.randomUUID(), source_path: sourcePath.trim() }) });
      setSourcePath(""); setMessage("Media admitted. Validation and canonical transcode are running in the background."); await onRefresh();
    } catch (problem) { setMessage(problem instanceof Error ? problem.message : "Media could not be admitted."); }
    finally { setBusy(false); }
  }
  return <><header className="page-header"><div><p className="eyebrow">Media lifecycle</p><h1>Managed library</h1><p>Only verified canonical derivatives can become air-ready.</p></div></header>
    <section className="panel import-panel"><div><p className="eyebrow">Import from this station</p><h2>Admit a media file</h2><p className="muted">Use an absolute path on this PC, USB media, or an accessible network share. The original is left untouched.</p></div><form onSubmit={admit} className="inline-form"><label>Absolute source path<input required value={sourcePath} onChange={(event) => setSourcePath(event.target.value)} placeholder="C:\Media\Council Meeting.mp4" /></label><button className="primary" disabled={busy}>{busy ? "Admitting…" : "Import & verify"}</button></form>{message && <div className="notice">{message}</div>}</section>
    <section className="panel"><div className="panel-head"><div><p className="eyebrow">Air-ready media</p><h2>Library assets</h2></div><span className="count">{dashboard?.by_asset.length ?? 0} items</span></div>
      {!dashboard?.by_asset.length ? <Empty title="The library is empty" detail="Import a program to begin the managed-media lifecycle." action="Focus import" onAction={() => document.querySelector<HTMLInputElement>(".inline-form input")?.focus()} /> : <div className="asset-grid">{dashboard.by_asset.map((asset) => <article className="asset-card" key={asset.asset_id}><div className="asset-preview"><span>TL</span><StatusPill status={asset.readiness_state} /></div><div className="asset-copy"><strong>{asset.media_path.split(/[\\/]/).pop()}</strong><p>{formatDuration(asset.duration_ms)} · {asset.measured_lufs === null ? "Loudness unavailable" : `${asset.measured_lufs.toFixed(2)} LUFS`}</p><code>{asset.asset_id.slice(0, 12)}…</code>{asset.readiness_reason && <small className="reason">{asset.readiness_reason}</small>}</div></article>)}</div>}
    </section>
    <section className="panel"><div className="panel-head"><div><p className="eyebrow">Background work</p><h2>Ingest and transcode jobs</h2></div></div><div className="row-list">{dashboard?.ingest_jobs.map((job) => <div className="job-row" key={job.job_id}><div><strong>{job.source_path.split(/[\\/]/).pop()}</strong><small>{job.error_detail || `${job.progress_percent}% complete · ${job.attempts} attempt${job.attempts === 1 ? "" : "s"}`}</small></div><div className="progress"><i style={{ width: `${job.progress_percent}%` }} /></div><StatusPill status={job.status} /></div>)}</div></section>
  </>;
}

function Channels({ channels, onRefresh }: { channels: Channel[]; onRefresh: () => Promise<void> }) {
  const [name, setName] = useState(""); const [destination, setDestination] = useState("127.0.0.1:5500"); const [busy, setBusy] = useState(false); const [error, setError] = useState<string | null>(null);
  async function save(event: FormEvent) { event.preventDefault(); setBusy(true); setError(null); try { await request<Channel>("/api/v1/channels", { method: "PUT", body: JSON.stringify({ channel_id: crypto.randomUUID(), display_name: name.trim(), udp_destination: destination.trim(), enabled: true }) }); setName(""); await onRefresh(); } catch (problem) { setError(problem instanceof Error ? problem.message : "Channel could not be saved."); } finally { setBusy(false); } }
  async function toggle(channel: Channel) { await request<Channel>("/api/v1/channels", { method: "PUT", body: JSON.stringify({ ...channel, enabled: !channel.enabled }) }); await onRefresh(); }
  return <><header className="page-header"><div><p className="eyebrow">Output control</p><h1>Channels</h1><p>Each enabled channel owns an isolated persistent media worker.</p></div></header><section className="split-grid channels-layout"><article className="panel"><p className="eyebrow">Configured outputs</p><h2>{channels.length || "No"} channel{channels.length === 1 ? "" : "s"}</h2>{channels.length ? <div className="channel-cards">{channels.map((channel) => <article className="channel-card" key={channel.channel_id}><div className="channel-top"><div className="signal-icon">◉</div><StatusPill status={channel.enabled ? "enabled" : "disabled"} /></div><h3>{channel.display_name}</h3><code>{channel.udp_destination}</code><button className="secondary wide" onClick={() => void toggle(channel)}>{channel.enabled ? "Stop output" : "Start output"}</button></article>)}</div> : <Empty title="No output configured" detail="Create the first persistent channel worker." action="Use the form" onAction={() => document.querySelector<HTMLInputElement>("#channel-name")?.focus()} />}</article><article className="panel sticky-form"><p className="eyebrow">New output</p><h2>Add a channel</h2><form onSubmit={save}><label>Channel name<input id="channel-name" required minLength={2} value={name} onChange={(event) => setName(event.target.value)} placeholder="Government Channel 12" /></label><label>UDP destination<input required value={destination} onChange={(event) => setDestination(event.target.value)} placeholder="192.168.1.100:5000" /><small>Use 127.0.0.1 for a local proof channel.</small></label>{error && <div className="error-box">{error}</div>}<button className="primary wide" disabled={busy}>{busy ? "Saving…" : "Create & start channel"}</button></form></article></section></>;
}

function Schedule({ dashboard, channels }: { dashboard: Dashboard | null; channels: Channel[] }) {
  const readyAssets = dashboard?.by_asset.filter((asset) => asset.readiness_state === "ready") ?? [];
  const [channelId, setChannelId] = useState(channels[0]?.channel_id ?? ""); const [assetId, setAssetId] = useState(readyAssets[0]?.asset_id ?? ""); const [title, setTitle] = useState(""); const [start, setStart] = useState(""); const [duration, setDuration] = useState("60"); const [plan, setPlan] = useState<CommitPlan | null>(null); const [item, setItem] = useState<ScheduleItem | null>(null); const [report, setReport] = useState<CommitReport | null>(null); const [busy, setBusy] = useState(false); const [error, setError] = useState<string | null>(null);
  useEffect(() => { if (!channelId && channels[0]) setChannelId(channels[0].channel_id); }, [channelId, channels]);
  useEffect(() => { if (!assetId && readyAssets[0]) setAssetId(readyAssets[0].asset_id); }, [assetId, readyAssets]);
  async function prepare(event: FormEvent) { event.preventDefault(); setBusy(true); setError(null); setReport(null); try { const scheduleItem: ScheduleItem = { item_id: crypto.randomUUID(), channel_id: channelId, asset_id: assetId, title: title.trim(), starts_at_unix_ms: new Date(start).getTime(), duration_ms: Math.round(Number(duration) * 1000), state: "draft" }; await request<ScheduleItem>("/api/v1/schedule/items", { method: "PUT", body: JSON.stringify(scheduleItem) }); const nextPlan = await request<CommitPlan>("/api/v1/schedule/prepare", { method: "POST", body: JSON.stringify({ plan_id: `plan-${crypto.randomUUID()}`, schedule_item_id: scheduleItem.item_id }) }); setItem(scheduleItem); setPlan(nextPlan); } catch (problem) { setError(problem instanceof Error ? problem.message : "The schedule could not be prepared."); } finally { setBusy(false); } }
  async function approve() { if (!plan || !item) return; setBusy(true); setError(null); try { const nextReport = await request<CommitReport>("/api/v1/schedule/commit", { method: "POST", body: JSON.stringify({ report_id: `report-${crypto.randomUUID()}`, plan_id: plan.plan_id, schedule_item_id: item.item_id, approved_by: "station-operator", operator_notes: "Approved in TownLight Station operator console." }) }); setReport(nextReport); const reportId = nextReport.report_id; const deadline = Date.now() + Math.max(30_000, item.starts_at_unix_ms - Date.now() + item.duration_ms + 10_000); const poll = async () => { const current = await request<CommitReport>(`/api/v1/schedule/commits/${reportId}`); setReport(current); if (!["completed", "error", "cancelled"].includes(current.dispatch_status) && Date.now() < deadline) window.setTimeout(() => void poll(), 500); }; void poll(); } catch (problem) { setError(problem instanceof Error ? problem.message : "Approval could not be committed."); } finally { setBusy(false); } }
  return <><header className="page-header"><div><p className="eyebrow">Commit to air</p><h1>Schedule a program</h1><p>Every approval reruns readiness and conflict checks under the station write lock.</p></div></header>{(!channels.length || !readyAssets.length) ? <section className="panel"><Empty title="Schedule prerequisites are missing" detail={!channels.length ? "Configure a channel before scheduling." : "Import and verify at least one air-ready asset."} action={!channels.length ? "Channel required" : "Ready media required"} onAction={() => undefined} /></section> : <section className="split-grid schedule-layout"><article className="panel sticky-form"><p className="eyebrow">Program details</p><h2>Create draft</h2><form onSubmit={prepare}><label>Channel<select required value={channelId} onChange={(event) => setChannelId(event.target.value)}>{channels.map((channel) => <option key={channel.channel_id} value={channel.channel_id}>{channel.display_name}</option>)}</select></label><label>Air-ready asset<select required value={assetId} onChange={(event) => setAssetId(event.target.value)}>{readyAssets.map((asset) => <option key={asset.asset_id} value={asset.asset_id}>{asset.media_path.split(/[\\/]/).pop()} · {formatDuration(asset.duration_ms)}</option>)}</select></label><label>Program title<input required value={title} onChange={(event) => setTitle(event.target.value)} placeholder="City Council Meeting" /></label><label>Starts at<input required type="datetime-local" value={start} onChange={(event) => setStart(event.target.value)} /></label><label>Air duration (seconds)<input required type="number" min="1" value={duration} onChange={(event) => setDuration(event.target.value)} /></label>{error && <div className="error-box">{error}</div>}<button className="primary wide" disabled={busy}>{busy ? "Checking…" : "Prepare commit"}</button></form></article><article className="panel"><p className="eyebrow">Approval gate</p><h2>Commit preview</h2>{!plan || !item ? <Empty title="No draft prepared" detail="Enter the program details to run conflict, gap, duration, and media-readiness checks." action="Complete the form" onAction={() => document.querySelector<HTMLInputElement>(".schedule-layout input")?.focus()} /> : <div className="commit-preview"><div className={`gate-banner ${plan.dry_run_passed ? "pass" : "fail"}`}><span>{plan.dry_run_passed ? "✓" : "!"}</span><div><strong>{plan.dry_run_passed ? "Safe to commit" : "Commit blocked"}</strong><p>{plan.dry_run_passed ? "No conflicts or missing media were found." : "Resolve every blocker, then prepare again."}</p></div></div><dl><div><dt>Program</dt><dd>{item.title}</dd></div><div><dt>Starts</dt><dd>{formatTime(item.starts_at_unix_ms)}</dd></div><div><dt>Duration</dt><dd>{formatDuration(item.duration_ms)}</dd></div><div><dt>Conflicts</dt><dd>{plan.conflicts.length}</dd></div><div><dt>Missing media</dt><dd>{plan.missing_media_detail ? 1 : 0}</dd></div></dl>{plan.missing_media_detail && <div className="error-box">{plan.missing_media_detail}</div>}<button className="primary wide" disabled={!plan.dry_run_passed || busy || Boolean(report)} onClick={() => void approve()}>{report ? "Approval recorded" : "Approve & commit to air"}</button>{report && <div className="dispatch"><span className="live-dot" /><div><strong>Dispatch {report.dispatch_status.replaceAll("_", " ")}</strong><p>The channel worker advances this record only after durable acknowledgements.</p></div><StatusPill status={report.dispatch_status} /></div>}</div>}</article></section>}</>;
}

function Health({ runtime, alerts, alertChannels, deliveries, onRefresh }: { runtime: RuntimeSafeToAirStatus | null; alerts: AlertEvent[]; alertChannels: AlertChannel[]; deliveries: AlertDelivery[]; onRefresh: () => Promise<void> }) {
  const [busyId, setBusyId] = useState<string | null>(null);
  const [kind, setKind] = useState<AlertChannelKind>("webhook");
  const [label, setLabel] = useState("");
  const [target, setTarget] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [sender, setSender] = useState("");
  const [identity, setIdentity] = useState("");
  const [credential, setCredential] = useState("");
  const [quietStart, setQuietStart] = useState("");
  const [quietEnd, setQuietEnd] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const firing = alerts.filter((alert) => alert.state === "firing");
  const recentDeliveries = [...deliveries].sort((a, b) => b.created_at_unix_ms - a.created_at_unix_ms).slice(0, 12);
  async function acknowledge(eventId: string) {
    setBusyId(eventId);
    try {
      await request<AlertEvent>(`/api/v1/alerts/${eventId}/acknowledge`, { method: "POST", body: JSON.stringify({ acknowledged_by: "station-operator" }) });
      await onRefresh();
    } finally {
      setBusyId(null);
    }
  }
  async function saveDestination(event: FormEvent) {
    event.preventDefault();
    setBusyId("new-destination");
    setFormError(null);
    try {
      const body = {
        channel_id: crypto.randomUUID(), kind, label: label.trim(), enabled: true, target: target.trim(), credential,
        endpoint: kind === "webhook" ? null : endpoint.trim(),
        sender: kind === "webhook" ? null : sender.trim(),
        identity: kind === "webhook" ? null : identity.trim(),
        quiet_hours_start_utc: quietStart || null,
        quiet_hours_end_utc: quietEnd || null,
      };
      await request<AlertChannel>("/api/v1/alert-channels", { method: "PUT", body: JSON.stringify(body) });
      setLabel(""); setTarget(""); setEndpoint(""); setSender(""); setIdentity(""); setCredential(""); setQuietStart(""); setQuietEnd("");
      await onRefresh();
    } catch (problem) {
      setFormError(problem instanceof Error ? problem.message : "The alert destination could not be saved.");
    } finally {
      setBusyId(null);
    }
  }
  async function sendTest(channelId: string) {
    setBusyId(channelId);
    try {
      await request<AlertDelivery>(`/api/v1/alert-channels/${channelId}/test`, { method: "POST" });
      await onRefresh();
    } catch (problem) {
      setFormError(problem instanceof Error ? problem.message : "The test notification could not be queued.");
    } finally {
      setBusyId(null);
    }
  }
  return <>
    <header className="page-header"><div><p className="eyebrow">Unattended survival</p><h1>System health & alerts</h1><p>Runtime truth comes from durable worker heartbeats and incident records.</p></div></header>
    {runtime ? <section className={`health-hero health-${runtime.color}`}><div className="health-orb">{runtime.color === "green" ? "✓" : "!"}</div><div><p className="eyebrow">Runtime safe-to-air</p><h2>{runtime.label}</h2><p>{runtime.operator_message}</p></div><div className="health-counts"><strong>{runtime.active_critical_alerts}</strong><small>critical</small><strong>{runtime.active_warning_alerts}</strong><small>warning</small></div></section> : <section className="panel"><p>Runtime status is loading.</p></section>}
    <section className="split-grid health-layout"><article className="panel"><div className="panel-head"><div><p className="eyebrow">Enabled outputs</p><h2>Channel runtime</h2></div><span className="count">{runtime?.channels.length ?? 0}</span></div><div className="runtime-list">{runtime?.channels.map((channel) => <article className={`runtime-card runtime-${channel.color}`} key={channel.channel_id}><div><span className="runtime-dot" /><strong>{channel.display_name}</strong></div><StatusPill status={channel.state} /><p>{channel.operator_message}</p><small>{formatDuration(channel.seconds_in_state * 1000)} in state · heartbeat {channel.heartbeat_age_seconds}s ago</small></article>)}</div>{runtime?.channels.length === 0 && <div className="clear-state"><div className="empty-orbit">→</div><strong>No enabled outputs</strong><p>Enable a channel to begin runtime monitoring.</p></div>}</article><article className="panel"><div className="panel-head"><div><p className="eyebrow">Notify on first failure</p><h2>Active alerts</h2></div><span className="count">{firing.length}</span></div>{firing.length ? <div className="alert-list">{firing.map((alert) => <article className={`alert-card alert-${alert.severity}`} key={alert.event_id}><div className="alert-top"><StatusPill status={alert.severity} /><small>{alert.source_section} · {formatTime(alert.first_observed_at_unix_ms)}</small></div><strong>{alert.summary}</strong><p>{alert.detail || alert.condition.replaceAll("-", " ")}</p><small>{alert.occurrence_count > 1 ? `Observed ${alert.occurrence_count} times; repeats deduplicated.` : "First failure recorded."}</small>{alert.acknowledged_at_unix_ms ? <span className="acknowledged">✓ Acknowledged by {alert.acknowledged_by}</span> : <button className="secondary wide" disabled={busyId === alert.event_id} onClick={() => void acknowledge(alert.event_id)}>{busyId === alert.event_id ? "Recording…" : "Acknowledge"}</button>}</article>)}</div> : <div className="clear-state"><div className="empty-orbit">✓</div><strong>No active alerts</strong><p>Runtime failures will appear here once and remain until the condition recovers.</p></div>}</article></section>
    <section className="split-grid alerting-layout"><article className="panel"><div className="panel-head"><div><p className="eyebrow">Off-site notification</p><h2>Destinations</h2></div><span className="count">{alertChannels.length}</span></div>{alertChannels.length ? <div className="destination-list">{alertChannels.map((channel) => <article className="destination-card" key={channel.channel_id}><div><strong>{channel.label}</strong><small>{channel.kind} · {channel.target_redacted}</small>{channel.quiet_hours_start_utc && <small>Quiet {channel.quiet_hours_start_utc}–{channel.quiet_hours_end_utc} UTC; critical alerts bypass</small>}</div><StatusPill status={channel.enabled ? "enabled" : "disabled"} /><button className="secondary" disabled={busyId === channel.channel_id} onClick={() => void sendTest(channel.channel_id)}>{busyId === channel.channel_id ? "Queuing…" : "Send test"}</button></article>)}</div> : <div className="clear-state"><div className="empty-orbit">→</div><strong>No notification destination</strong><p>Add email, SMS, or a signed webhook for off-site failures and recoveries.</p></div>}</article><article className="panel"><p className="eyebrow">Encrypted at rest</p><h2>Add a destination</h2><form className="alert-form" onSubmit={saveDestination}><div className="form-pair"><label>Kind<select value={kind} onChange={(event) => setKind(event.target.value as AlertChannelKind)}><option value="webhook">Signed webhook</option><option value="email">Email</option><option value="sms">SMS</option></select></label><label>Label<input required minLength={2} value={label} onChange={(event) => setLabel(event.target.value)} placeholder="On-call operator" /></label></div><label>{kind === "email" ? "Recipient email" : kind === "sms" ? "Recipient phone" : "Webhook URL"}<input required value={target} onChange={(event) => setTarget(event.target.value)} placeholder={kind === "email" ? "operator@example.gov" : kind === "sms" ? "+13035550123" : "https://alerts.example.gov/station"} /></label>{kind !== "webhook" && <><label>{kind === "email" ? "SMTP endpoint" : "Provider endpoint"}<input required value={endpoint} onChange={(event) => setEndpoint(event.target.value)} placeholder={kind === "email" ? "smtps://smtp.example.gov:465" : "https://api.twilio.com/2010-04-01/Accounts/.../Messages.json"} /></label><div className="form-pair"><label>{kind === "email" ? "From email" : "From phone"}<input required value={sender} onChange={(event) => setSender(event.target.value)} /></label><label>{kind === "email" ? "SMTP username" : "Account SID"}<input required value={identity} onChange={(event) => setIdentity(event.target.value)} /></label></div></>}<label>{kind === "webhook" ? "Signing secret" : kind === "email" ? "SMTP password" : "Auth token"}<input required minLength={8} type="password" autoComplete="new-password" value={credential} onChange={(event) => setCredential(event.target.value)} /><small>Stored with Windows machine encryption and never returned by the API.</small></label><div className="form-pair"><label>Quiet start (UTC)<input type="time" value={quietStart} onChange={(event) => setQuietStart(event.target.value)} /></label><label>Quiet end (UTC)<input type="time" value={quietEnd} onChange={(event) => setQuietEnd(event.target.value)} /></label></div>{formError && <div className="error-box">{formError}</div>}<button className="primary wide" disabled={busyId === "new-destination"}>{busyId === "new-destination" ? "Saving…" : "Save destination"}</button></form></article></section>
    <section className="panel"><div className="panel-head"><div><p className="eyebrow">Durable delivery ledger</p><h2>Recent notifications</h2></div><span className="count">{deliveries.length}</span></div>{recentDeliveries.length ? <div className="delivery-list">{recentDeliveries.map((delivery) => { const channel = alertChannels.find((item) => item.channel_id === delivery.alert_channel_id); return <article className="delivery-row" key={delivery.delivery_id}><div><strong>{delivery.notification_kind === "test" ? "Operator test" : `${delivery.notification_kind} alert`} · {channel?.label ?? "Removed destination"}</strong><small>{formatTime(delivery.created_at_unix_ms)} · {delivery.attempts} attempt{delivery.attempts === 1 ? "" : "s"}{delivery.last_error ? ` · ${delivery.last_error}` : ""}</small></div><StatusPill status={delivery.state} /></article>; })}</div> : <div className="clear-state compact"><strong>No delivery attempts</strong><p>Tests and incident notifications will remain visible here across restarts.</p></div>}</section>
  </>;
}

function Commissioning({ station, dashboard, channels }: { station: StationDocument; dashboard: Dashboard | null; channels: Channel[] }) {
  const checks = [
    { label: "Windows station service", detail: "Loopback API and SQLite authority are ready", ok: true },
    { label: "Station identity", detail: `${station.display_name} · ${station.timezone}`, ok: true },
    { label: "Channel output", detail: channels.length ? `${channels.length} configured, ${channels.filter((channel) => channel.enabled).length} enabled` : "Configure at least one output", ok: channels.length > 0 },
    { label: "Canonical media", detail: dashboard?.ready_count ? `${dashboard.ready_count} verified air-ready asset${dashboard.ready_count === 1 ? "" : "s"}` : "Import and verify the first program", ok: Boolean(dashboard?.ready_count) },
    { label: "Processing queue", detail: dashboard?.failed_ingest_count || dashboard?.failed_transcode_count ? "A media job needs operator attention" : "No failed background work", ok: !(dashboard?.failed_ingest_count || dashboard?.failed_transcode_count) },
  ];
  const ready = checks.every((check) => check.ok);
  return <><header className="page-header"><div><p className="eyebrow">Commissioning report</p><h1>{ready ? "Ready for first-air proof." : "Commissioning in progress."}</h1><p>Every item below is read from the running station, not a saved checklist.</p></div></header><section className="panel commissioning-panel"><div className={`commission-banner ${ready ? "pass" : "pending"}`}><div className="commission-score">{checks.filter((check) => check.ok).length}/{checks.length}</div><div><strong>{ready ? "Core station checks passed" : "Finish the remaining station checks"}</strong><p>{ready ? "Schedule a verified asset on a proof channel to advance the station." : "Blocked items name the next operator action."}</p></div></div><div className="commission-list">{checks.map((check) => <div key={check.label}><span className={check.ok ? "check-ok" : "check-wait"}>{check.ok ? "✓" : "→"}</span><p><strong>{check.label}</strong><small>{check.detail}</small></p><StatusPill status={check.ok ? "ready" : "pending"} /></div>)}</div></section></>;
}

function App() {
  const [station, setStation] = useState<StationDocument | null | undefined>(undefined);
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [runtime, setRuntime] = useState<RuntimeSafeToAirStatus | null>(null);
  const [alerts, setAlerts] = useState<AlertEvent[]>([]);
  const [alertChannels, setAlertChannels] = useState<AlertChannel[]>([]);
  const [deliveries, setDeliveries] = useState<AlertDelivery[]>([]);
  const [view, setView] = useState<View>(initialView);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    try {
      const [nextDashboard, nextChannels, nextRuntime, nextAlerts, nextAlertChannels, nextDeliveries] = await Promise.all([request<Dashboard>("/api/v1/assets/readiness-dashboard"), request<Channel[]>("/api/v1/channels"), request<RuntimeSafeToAirStatus>("/api/v1/runtime-safe-to-air"), request<AlertEvent[]>("/api/v1/alerts"), request<AlertChannel[]>("/api/v1/alert-channels"), request<AlertDelivery[]>("/api/v1/alert-deliveries")]);
      setDashboard(nextDashboard); setChannels(nextChannels); setRuntime(nextRuntime); setAlerts(nextAlerts); setAlertChannels(nextAlertChannels); setDeliveries(nextDeliveries); setError(null);
    } catch (problem) { setError(problem instanceof Error ? problem.message : "Station state could not be refreshed."); }
  }, []);
  useEffect(() => { void request<StationDocument>("/api/v1/station").then((value) => { setStation(value); void refresh(); }).catch((problem) => { if (problem instanceof ApiError && problem.status === 404) setStation(null); else { setStation(null); setError(problem instanceof Error ? problem.message : "Station profile could not be read."); } }); }, [refresh]);
  useEffect(() => { if (!station) return; const timer = window.setInterval(() => void refresh(), 5_000); return () => window.clearInterval(timer); }, [refresh, station]);
  const nav = useMemo(() => ([{ id: "overview" as View, label: "Overview" }, { id: "media" as View, label: "Media" }, { id: "channels" as View, label: "Channels" }, { id: "schedule" as View, label: "Schedule" }, { id: "health" as View, label: "Health" }, { id: "commissioning" as View, label: "Commission" }]), []);
  if (station === undefined) return <div className="boot"><div className="mark">TL</div><div className="boot-line"><i /></div><p>Connecting to TownLight Station…</p></div>;
  if (!station) return <SetupWizard onComplete={(value) => { setStation(value); void refresh(); }} />;
  return <div className="app-shell"><aside className="sidebar"><div className="brand-lockup"><div className="mark small">TL</div><div><strong>TownLight</strong><span>Station</span></div></div><nav>{nav.map((item) => <button key={item.id} className={view === item.id ? "selected" : ""} onClick={() => setView(item.id)}><Icon name={item.id} /><span>{item.label}</span>{item.id === "media" && Boolean(dashboard?.failed_ingest_count) && <b>{dashboard?.failed_ingest_count}</b>}{item.id === "health" && Boolean(runtime?.active_critical_alerts || runtime?.active_warning_alerts) && <b>{(runtime?.active_critical_alerts ?? 0) + (runtime?.active_warning_alerts ?? 0)}</b>}</button>)}</nav><div className="station-card"><span className={`live-dot runtime-${runtime?.color ?? "yellow"}`} /><div><strong>{station.display_name}</strong><small>{runtime?.label ?? station.timezone}</small></div></div></aside><main className="workspace">{error && <div className="global-error" role="alert">{error}<button onClick={() => void refresh()}>Retry</button></div>}{view === "overview" && <Overview station={station} dashboard={dashboard} channels={channels} runtime={runtime} onNavigate={setView} />}{view === "media" && <Media dashboard={dashboard} onRefresh={refresh} />}{view === "channels" && <Channels channels={channels} onRefresh={refresh} />}{view === "schedule" && <Schedule dashboard={dashboard} channels={channels} />}{view === "health" && <Health runtime={runtime} alerts={alerts} alertChannels={alertChannels} deliveries={deliveries} onRefresh={refresh} />}{view === "commissioning" && <Commissioning station={station} dashboard={dashboard} channels={channels} />}</main><nav className="mobile-nav">{nav.map((item) => <button key={item.id} aria-label={item.label} className={view === item.id ? "selected" : ""} onClick={() => setView(item.id)}><Icon name={item.id} /><span>{item.label}</span></button>)}</nav></div>;
}

createRoot(document.getElementById("root")!).render(<React.StrictMode><App /></React.StrictMode>);
