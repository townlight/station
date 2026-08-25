import React, { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import { CgOperator, CG_ROUTES, CgRoute } from "./cg";
import { LoudnessProofPanel } from "./loudness-proof";
import { ReceiverReceipts } from "./receiver-receipts";
import { FallbackSlatePanel } from "./fallback-slate";
import { CaptionPolicyPanel } from "./caption-policy";
import { CaptionTrackEditor } from "./caption-tracks";
import { AutomationPanel } from "./automation";
import {
  HeadendProfile,
  HeadendReadiness,
  canApplyHeadendProfile,
  canStartHeadendProfile,
  readinessLabel,
  receiverAcceptanceLabel,
} from "./headend-contract";
import { AuthoritativeCommissioning as Commissioning } from "./commissioning";
import { ApiError, apiRequest as request } from "./api-client";
import { AuthBoundary } from "./auth";

type CoreView = "overview" | "media" | "channels" | "schedule" | "live" | "automation" | "health" | "commissioning";
type View = CoreView | CgRoute;
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
  output_profile: "proof-360p30" | "atsc-720p5994";
  output_sinks: OutputSink[];
  enabled: boolean;
}

type LiveTakeoverState = "arming" | "armed" | "take_requested" | "on_air" | "return_requested" | "returning" | "completed" | "failed";

interface LiveSource {
  source_id: string;
  channel_id: string;
  label: string;
  enabled: boolean;
  endpoint: { UdpMpegTs: { bind_ip: string; port: number } };
  revision: number;
}

interface LiveAuthority {
  channel_id: string;
  authority_revision: number;
}

interface LiveTakeoverSession {
  session_id: string;
  channel_id: string;
  source_id: string;
  source_revision: number;
  authority_revision: number;
  operator_id: string;
  reason: string | null;
  requested_at_unix_ms: number;
  expires_at_unix_ms: number;
  taken_at_unix_ms: number | null;
  returned_at_unix_ms: number | null;
  state: LiveTakeoverState;
  failure_detail: string | null;
}

interface LiveAuditEvent {
  audit_id: string;
  session_id: string;
  channel_id: string;
  source_id: string;
  operator_id: string;
  state: LiveTakeoverState;
  occurred_at_unix_ms: number;
  detail: string | null;
}

interface OutputSink {
  sink_id: string;
  label: string;
  kind: "udp-ts" | "srt";
  destination: string;
  loudness_target_lufs: number;
  required: boolean;
  enabled: boolean;
  encryption_configured: boolean;
  headend?: {
    profile_id: string;
    muxrate_kbps: number;
    packet_size_bytes: number;
  } | null;
}

interface OutputProfile {
  profile_id: Channel["output_profile"];
  display_name: string;
  intended_use: string;
  width: number;
  height: number;
  frame_rate_numerator: number;
  frame_rate_denominator: number;
  interlaced: boolean;
  video_bitrate_bps: number;
  audio_bitrate_bps: number;
  transport_bitrate_bps: number;
  audio_sample_rate_hz: number;
  loudness_target_lufs: number;
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
  notification_kind: "firing" | "realert" | "resolved" | "test";
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

interface AlertRule {
  rule_id: string;
  condition: string;
  enabled: boolean;
  severity: "critical" | "warning" | "info";
  channel_ids: string[];
  dedupe_window_seconds: number;
  re_alert_after_seconds: number;
  scope_channel_id: string | null;
  notify_on_resolve: boolean;
  updated_at_unix_ms: number;
  updated_by: string;
}

interface SystemResourceSample {
  sample_id: number | null;
  sampled_at_unix_ms: number;
  cpu_percent: number | null;
  ram_used_gb: number | null;
  ram_total_gb: number | null;
  gpu_percent: number | null;
  gpu_vram_used_gb: number | null;
  media_volume_free_gb: number | null;
  backup_volume_free_gb: number | null;
  database_reachable: boolean;
  backup_volume_writable: boolean;
  service_running: boolean;
  clock_skew_seconds: number | null;
}

type SelfTestKind = "daily" | "weekly";
type SelfTestStatus = "pass" | "warn" | "fail";

interface SystemSelfTest {
  self_test_id: string;
  kind: SelfTestKind;
  trigger: "scheduled" | "manual";
  schedule_period_key: string | null;
  started_at_unix_ms: number;
  finished_at_unix_ms: number;
  status: SelfTestStatus;
  checks: Record<string, boolean>;
  summary: string;
  evidence_path: string | null;
}

interface SelfTestSchedule {
  kind: SelfTestKind;
  enabled: boolean;
  local_hour: number;
  local_minute: number;
  weekday: number | null;
  updated_at_unix_ms: number;
  updated_by: string;
}

interface SupportBundleRecord {
  bundle_id: string;
  commissioning_report_id?: string | null;
  created_at_unix_ms: number;
  file_name: string;
  path: string;
  download_uri: string;
  sha256: string;
  size_bytes: number;
  entries: string[];
  contains: string[];
  excludes: string[];
  retention_warning: string | null;
}

interface ReleaseManifest {
  version: string;
  source_commit: string;
  published_at_unix_ms: number;
  package_size_bytes: number;
  package_sha256: string;
  release_notes_markdown: string;
}

interface StagedPackageRecord {
  staged_at_unix_ms: number;
  manifest: ReleaseManifest;
  package_file_name: string;
}

interface PreparedSlotRecord {
  prepared_at_unix_ms: number;
  manifest: ReleaseManifest;
  package_file_name: string;
  slot_directory_name: string;
}

interface UpdateCheckRecord {
  checked_at_unix_ms: number;
  next_automatic_check_at_unix_ms: number;
  trigger: "automatic" | "manual";
  outcome: "up_to_date" | "update_available" | "updater_too_old" | "failed";
  offered_release: ReleaseManifest | null;
  error: string | null;
}

interface UpdateStatus {
  configured: boolean;
  current_version: string;
  source_commit: string;
  release_channel: string | null;
  feed_origin: string | null;
  latest_check: UpdateCheckRecord | null;
  staged_package: StagedPackageRecord | null;
  prepared_slot: PreparedSlotRecord | null;
  maintenance: MaintenanceStatus | null;
  next_step: string;
}

interface MaintenanceStatus {
  transaction_id: string;
  requested_at_unix_ms: number;
  requested_by: string;
  updated_at_unix_ms: number;
  phase: "requested" | "service_stopped" | "candidate_configured" | "candidate_started" | "commit_started" | "committed" | "rollback_started" | "rolled_back" | "rollback_failed";
  from_version: string;
  candidate_version: string;
  detail: string;
}

function initialView(): View {
  const requested = new URLSearchParams(window.location.search).get("view");
  return ["overview", "media", "channels", "schedule", "live", "automation", "health", "commissioning", ...CG_ROUTES.map((item) => item.id)].includes(requested ?? "")
    ? requested as View
    : "overview";
}

interface CommitReport {
  report_id: string;
  schedule_item_id: string;
  dispatch_status: "pending" | "queued" | "acknowledged" | "completed" | "error" | "cancelled";
  operator_notes: string;
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
  const tone = ["ready", "completed", "acknowledged", "ok", "enabled", "healthy_fallback", "on_air", "sent", "up_to_date"].includes(status)
    ? "good"
    : ["failed", "error", "missing_file", "rejected", "stopped", "critical", "dead_letter"].includes(status)
      ? "bad"
      : "active";
  return <span className={`pill pill-${tone}`}>{status.replaceAll("_", " ")}</span>;
}

function Icon({ name }: { name: CoreView }) {
  const paths: Record<CoreView, React.ReactNode> = {
    overview: <><path d="M4 13h6V4H4zM14 20h6v-9h-6zM4 20h6v-3H4zM14 7h6V4h-6z" /></>,
    media: <><rect x="3" y="5" width="18" height="14" rx="2" /><path d="m10 9 5 3-5 3z" /></>,
    channels: <><path d="M5 8.5a10 10 0 0 0 0 7M19 8.5a10 10 0 0 1 0 7M8.5 10.5a5 5 0 0 0 0 3M15.5 10.5a5 5 0 0 1 0 3" /><circle cx="12" cy="12" r="1.5" /></>,
    schedule: <><rect x="3" y="5" width="18" height="16" rx="2" /><path d="M8 3v4M16 3v4M3 10h18M8 14h3M8 17h7" /></>,
    live: <><circle cx="12" cy="12" r="8" /><circle cx="12" cy="12" r="3" /><path d="M12 2v2M12 20v2M2 12h2M20 12h2" /></>,
    automation: <><path d="M8 4h8M9 8h6M7 12h10M5 16h14M8 20h8" /><circle cx="5" cy="12" r="1" /><circle cx="19" cy="16" r="1" /></>,
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
      <div className="steps" aria-label="Station identity setup progress">
        {[0, 1].map((value) => <span key={value} className={value <= step ? "step active" : "step"} />)}
      </div>
      {step === 0 ? <>
        <p className="eyebrow">First run · Welcome</p>
        <h2>Let’s identify this station.</h2>
        <p className="muted">This short form creates the durable station identity. The authoritative commissioning report then guides every required station, output, and first-air proof step.</p>
        <div className="check-list">
          <div><span>1</span><p><strong>Station identity</strong><small>Name and local timezone</small></p></div>
          <div><span>2</span><p><strong>StationBox & sources</strong><small>Storage, time, backup, watch folder, fallback, and outputs</small></p></div>
          <div><span>3</span><p><strong>Guided first-air proof</strong><small>Ingest, schedule, air, and verify one causally linked program</small></p></div>
        </div>
        <button className="primary wide" onClick={() => setStep(1)}>Create station identity <span>→</span></button>
      </> : <form onSubmit={commission}>
        <p className="eyebrow">First run · Station identity</p>
        <h2>Name this station.</h2>
        <label>Station name<input autoFocus required minLength={2} maxLength={120} value={displayName} onChange={(event) => setDisplayName(event.target.value)} placeholder="KTLT Community Television" /></label>
        <label>Local timezone<input required value={timezone} onChange={(event) => setTimezone(event.target.value)} placeholder="America/Denver" /><small>Schedules and operator timestamps use this IANA timezone.</small></label>
        {error && <div className="error-box" role="alert">{error}</div>}
        <div className="button-row"><button type="button" className="secondary" onClick={() => setStep(0)}>Back</button><button className="primary" disabled={busy || displayName.trim().length < 2}>{busy ? "Saving…" : "Save identity & continue"}</button></div>
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
        {channels.length === 0 ? <Empty title="No channels configured" detail="Add the station’s first loopback, headend, or proof output." action="Configure channel" onAction={() => onNavigate("channels")} /> : <div className="row-list">{channels.slice(0, 5).map((channel) => <div className="data-row" key={channel.channel_id}><div className="signal-icon">◉</div><div><strong>{channel.display_name}</strong><small>{channel.output_sinks.filter((sink) => sink.enabled).map((sink) => sink.destination).join(" · ")}</small></div><StatusPill status={channel.enabled ? "enabled" : "disabled"} /></div>)}</div>}
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
  const [captionAsset, setCaptionAsset] = useState<AssetView | null>(null);
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
      {!dashboard?.by_asset.length ? <Empty title="The library is empty" detail="Import a program to begin the managed-media lifecycle." action="Focus import" onAction={() => document.querySelector<HTMLInputElement>(".inline-form input")?.focus()} /> : <div className="asset-grid">{dashboard.by_asset.map((asset) => <article className={`asset-card ${captionAsset?.asset_id === asset.asset_id ? "selected" : ""}`} key={asset.asset_id}><div className="asset-preview"><span>TL</span><StatusPill status={asset.readiness_state} /></div><div className="asset-copy"><strong>{asset.media_path.split(/[\\/]/).pop()}</strong><p>{formatDuration(asset.duration_ms)} · {asset.measured_lufs === null ? "Loudness unavailable" : `${asset.measured_lufs.toFixed(2)} LUFS`}</p><code>{asset.asset_id.slice(0, 12)}…</code>{asset.readiness_reason && <small className="reason">{asset.readiness_reason}</small>}<button className="secondary wide asset-caption-button" onClick={() => setCaptionAsset(asset)}>{captionAsset?.asset_id === asset.asset_id ? "Captions open" : "Manage captions"}</button></div></article>)}</div>}
    </section>
    {captionAsset && <CaptionTrackEditor assetId={captionAsset.asset_id} assetName={captionAsset.media_path.split(/[\\/]/).pop() ?? captionAsset.asset_id} assetDurationMs={captionAsset.duration_ms} assetReady={captionAsset.readiness_state === "ready"} onClose={() => setCaptionAsset(null)} />}
    <section className="panel"><div className="panel-head"><div><p className="eyebrow">Background work</p><h2>Ingest and transcode jobs</h2></div></div><div className="row-list">{dashboard?.ingest_jobs.map((job) => <div className="job-row" key={job.job_id}><div><strong>{job.source_path.split(/[\\/]/).pop()}</strong><small>{job.error_detail || `${job.progress_percent}% complete · ${job.attempts} attempt${job.attempts === 1 ? "" : "s"}`}</small></div><div className="progress"><i style={{ width: `${job.progress_percent}%` }} /></div><StatusPill status={job.status} /></div>)}</div></section>
  </>;
}

function HeadendWorkflow({ channels, profiles, loadingCatalog, catalogError, onReloadProfiles, onRefresh }: { channels: Channel[]; profiles: HeadendProfile[] | null; loadingCatalog: boolean; catalogError: string | null; onReloadProfiles: () => Promise<void>; onRefresh: () => Promise<void> }) {
  const [readiness, setReadiness] = useState<HeadendReadiness | null>(null);
  const [selectedChannelId, setSelectedChannelId] = useState("");
  const [selectedProfileId, setSelectedProfileId] = useState("");
  const [destination, setDestination] = useState("");
  const [muxrateKbps, setMuxrateKbps] = useState("");
  const [keepExistingSinks, setKeepExistingSinks] = useState(false);
  const [loadingReadiness, setLoadingReadiness] = useState(true);
  const [applying, setApplying] = useState(false);
  const [readinessError, setReadinessError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const channelFingerprint = channels.map((channel) => `${channel.channel_id}:${channel.enabled}:${channel.output_sinks.length}`).join("|");

  const loadReadiness = useCallback(async () => {
    setLoadingReadiness(true);
    try {
      setReadiness(await request<HeadendReadiness>("/api/v1/headend-readiness"));
      setReadinessError(null);
    } catch (problem) {
      setReadiness(null);
      setReadinessError(problem instanceof Error ? problem.message : "Headend readiness could not be read.");
    } finally {
      setLoadingReadiness(false);
    }
  }, []);

  useEffect(() => { void loadReadiness(); }, [loadReadiness, channelFingerprint]);
  useEffect(() => {
    if (!channels.some((channel) => channel.channel_id === selectedChannelId)) {
      setSelectedChannelId(channels.find((channel) => !channel.enabled)?.channel_id ?? channels[0]?.channel_id ?? "");
    }
  }, [channels, selectedChannelId]);
  useEffect(() => {
    if (!profiles?.some((profile) => profile.profile_id === selectedProfileId)) {
      const first = profiles?.find((profile) => profile.execution_state === "runnable") ?? profiles?.[0];
      setSelectedProfileId(first?.profile_id ?? "");
      setMuxrateKbps(first ? String(first.muxrate_kbps) : "");
    }
  }, [profiles, selectedProfileId]);

  const selectedChannel = channels.find((channel) => channel.channel_id === selectedChannelId);
  const selectedProfile = profiles?.find((profile) => profile.profile_id === selectedProfileId);
  const selectedReadiness = readiness?.channels.find((channel) => channel.channel_id === selectedChannelId);
  const canApply = Boolean(selectedChannel && selectedProfile && destination.trim() && Number.isInteger(Number(muxrateKbps)) && Number(muxrateKbps) > 0 && canApplyHeadendProfile(selectedProfile.execution_state, selectedChannel.enabled));

  function chooseProfile(profile: HeadendProfile) {
    setSelectedProfileId(profile.profile_id);
    setMuxrateKbps(profile.muxrate_kbps ? String(profile.muxrate_kbps) : "");
    setActionError(null);
    setMessage(null);
  }

  async function applyProfile(event: FormEvent) {
    event.preventDefault();
    if (!selectedChannel || !selectedProfile || !canApply) return;
    setApplying(true); setActionError(null); setMessage(null);
    try {
      await request(`/api/v1/channels/${selectedChannel.channel_id}/headend-profile`, {
        method: "POST",
        body: JSON.stringify({
          profile_id: selectedProfile.profile_id,
          destination_uri: destination.trim(),
          muxrate_kbps: Number(muxrateKbps),
          keep_existing_sinks: keepExistingSinks,
        }),
      });
      setMessage(selectedProfile.execution_state === "runnable"
        ? `${selectedProfile.label} is applied to ${selectedChannel.display_name}. Review readiness, then use Start output above.`
        : `${selectedProfile.label} is saved on ${selectedChannel.display_name} for future execution. This installed engine will keep Start output blocked.`);
      await onRefresh();
      await loadReadiness();
    } catch (problem) {
      setActionError(problem instanceof Error ? problem.message : "The headend profile could not be applied.");
    } finally {
      setApplying(false);
    }
  }

  return <section className="panel headend-panel">
    <div className="panel-head"><div><p className="eyebrow">Headend handoff</p><h2>Configure a carriage profile</h2></div>{readiness && <span className={`headend-rollup headend-${readiness.rollup.color}`}>{readiness.rollup.color} · {readiness.rollup.passing_channels}/{readiness.rollup.enabled_channels} ready</span>}</div>
    <div className="headend-boundary"><strong>Two proofs, kept separate</strong><p>“Verified locally” means this station is producing the configured transport. Receiver acceptance remains unknown until a cable operator or headend integrator verifies it.</p></div>
    {!channels.length ? <Empty title="Create a channel first" detail="A headend profile is applied to an existing, stopped channel." action="Use the channel form above" onAction={() => document.querySelector<HTMLInputElement>("#channel-name")?.focus()} /> : <div className="headend-layout">
      <form className="headend-config" onSubmit={applyProfile}>
        <label>Channel<select value={selectedChannelId} onChange={(event) => { setSelectedChannelId(event.target.value); setActionError(null); setMessage(null); }}>{channels.map((channel) => <option value={channel.channel_id} key={channel.channel_id}>{channel.display_name}{channel.enabled ? " · running" : " · stopped"}</option>)}</select><small>{selectedChannel?.enabled ? "Stop this channel with the control above before changing its carriage contract." : "Stopped and available for profile configuration."}</small></label>
        <fieldset className="headend-profile-picker"><legend>Headend profile</legend>{loadingCatalog ? <p className="muted">Loading the installed profile catalog…</p> : catalogError ? <div className="headend-catalog-error"><div className="error-box" role="alert">{catalogError}</div><button className="secondary" type="button" onClick={() => void onReloadProfiles()}>Retry catalog</button></div> : !profiles?.length ? <p className="muted">No headend profiles are installed.</p> : <div className="headend-profile-grid">{profiles.map((profile) => <button type="button" className={`headend-profile-card ${selectedProfileId === profile.profile_id ? "selected" : ""} profile-${profile.execution_state}`} aria-pressed={selectedProfileId === profile.profile_id} onClick={() => chooseProfile(profile)} key={profile.profile_id}><span><strong>{profile.label}</strong><small>{profile.vendor}</small></span><b>{profile.execution_state === "runnable" ? "Runnable locally" : profile.execution_state === "staged_only" ? "Staging only" : "Not implemented"}</b><p>{profile.execution_detail}</p></button>)}</div>}</fieldset>
        {selectedProfile && <div className={`headend-profile-detail profile-${selectedProfile.execution_state}`}><div><strong>{selectedProfile.execution_state === "runnable" ? "Local execution available" : "Execution blocked"}</strong><span>{selectedProfile.canonical_profile} · {(selectedProfile.muxrate_kbps / 1000).toFixed(1)} Mb/s · {selectedProfile.packet_size_bytes}-byte UDP payload</span></div><p>{selectedProfile.execution_detail}</p><ul>{selectedProfile.operator_must_supply.map((item) => <li key={item}>{item}</li>)}</ul>{selectedProfile.not_claimed.map((item) => <small key={item}>{item}</small>)}</div>}
        <div className="form-pair"><label>Assigned destination<input required value={destination} onChange={(event) => setDestination(event.target.value)} placeholder="udp://192.168.10.20:5000" disabled={!selectedProfile || selectedProfile.execution_state === "unimplemented"} /><small>Enter the address and port supplied by the receiving facility.</small></label><label>{selectedProfile?.execution_state === "runnable" ? "Executable" : "Staged"} multiplex rate (kb/s)<input required type="number" min="1" max="100000" value={muxrateKbps} readOnly={selectedProfile?.execution_state === "runnable"} onChange={(event) => setMuxrateKbps(event.target.value)} disabled={!selectedProfile || selectedProfile.execution_state === "unimplemented"} /><small>{selectedProfile?.execution_state === "runnable" ? "This installed profile is locked to its locally proved catalog rate." : "Stored for future execution; this profile cannot be started by the installed engine."}</small></label></div>
        <label className="headend-check"><span><input type="checkbox" checked={keepExistingSinks} onChange={(event) => setKeepExistingSinks(event.target.checked)} />Keep existing output sinks</span><small>Off replaces existing sinks with the required headend output. On retains them as additional branches.</small></label>
        {selectedChannel?.enabled && <div className="headend-warning" role="status">Profile changes are locked while this channel is running. Stop output above; the current output remains untouched.</div>}
        {selectedProfile && selectedProfile.execution_state === "staged_only" && <div className="headend-warning" role="status">This contract can be staged while stopped, but TownLight Station will reject Start output until its execution path is implemented and proved.</div>}
        {selectedProfile && selectedProfile.execution_state === "unimplemented" && <div className="headend-warning" role="status">This catalog entry is visible for planning and cannot yet be applied: {selectedProfile.execution_detail}</div>}
        {actionError && <div className="error-box" role="alert">{actionError}</div>}
        {message && <div className="notice" role="status">{message}</div>}
        <button className="primary wide" disabled={!canApply || applying}>{applying ? "Applying profile…" : selectedChannel?.enabled ? "Stop channel to apply" : selectedProfile?.execution_state === "staged_only" ? "Stage blocked profile" : selectedProfile?.execution_state === "unimplemented" ? "Profile not implemented" : "Apply profile to stopped channel"}</button>
      </form>
      <aside className="headend-readiness" aria-live="polite">
        <div className="panel-head"><div><p className="eyebrow">Current evidence</p><h2>{selectedChannel?.display_name ?? "Channel readiness"}</h2></div>{selectedReadiness && <StatusPill status={selectedReadiness.state} />}</div>
        {loadingReadiness ? <p className="muted">Reading current output evidence…</p> : readinessError ? <div className="headend-empty"><div className="error-box" role="alert">{readinessError}</div><button className="secondary" type="button" onClick={() => void loadReadiness()}>Retry readiness</button></div> : !selectedReadiness ? <div className="headend-empty"><strong>No readiness record</strong><p>Refresh after the channel has been saved.</p><button className="secondary" type="button" onClick={() => void loadReadiness()}>Retry</button></div> : <><p className="headend-readiness-detail">{selectedReadiness.detail}</p><div className="headend-evidence-list">{selectedReadiness.sinks.map((sink) => <article key={sink.sink_id}><div className="headend-evidence-head"><div><strong>{sink.label}</strong><small>{sink.kind.replaceAll("_", " ")} · {sink.required ? "required" : "optional"}</small></div><StatusPill status={sink.state} /></div><div className="headend-proof-split"><div><span>Local transport</span><strong>{readinessLabel(sink.local_transport)}</strong><small>{sink.evidence}</small></div><div><span>Receiver acceptance</span><strong>{receiverAcceptanceLabel(sink.external_receiver)}</strong><small>{sink.external_receiver_evidence ?? "No current external receiver receipt is recorded; local output is not proof of reception."}</small></div></div><p>{sink.detail}</p></article>)}</div><button className="text-button" type="button" onClick={() => void loadReadiness()}>Refresh evidence →</button></>}
      </aside>
    </div>}
    {selectedChannel && <ReceiverReceipts channelId={selectedChannel.channel_id} sinks={selectedChannel.output_sinks} onRecorded={loadReadiness} />}
  </section>;
}

function Channels({ channels, outputProfiles, onRefresh }: { channels: Channel[]; outputProfiles: OutputProfile[]; onRefresh: () => Promise<void> }) {
  const [name, setName] = useState(""); const [transportKind, setTransportKind] = useState<OutputSink["kind"]>("udp-ts"); const [destination, setDestination] = useState("127.0.0.1:5500"); const [redundantDestination, setRedundantDestination] = useState(""); const [passphrase, setPassphrase] = useState(""); const [redundantPassphrase, setRedundantPassphrase] = useState(""); const [outputProfile, setOutputProfile] = useState<Channel["output_profile"]>("proof-360p30"); const [busy, setBusy] = useState(false); const [error, setError] = useState<string | null>(null);
  const [headendProfiles, setHeadendProfiles] = useState<HeadendProfile[] | null>(null); const [headendProfilesLoading, setHeadendProfilesLoading] = useState(true); const [headendProfilesError, setHeadendProfilesError] = useState<string | null>(null);
  const loadHeadendProfiles = useCallback(async () => {
    setHeadendProfilesLoading(true);
    try { setHeadendProfiles(await request<HeadendProfile[]>("/api/v1/headend-profiles")); setHeadendProfilesError(null); }
    catch (problem) { setHeadendProfilesError(problem instanceof Error ? problem.message : "Headend profiles could not be read."); }
    finally { setHeadendProfilesLoading(false); }
  }, []);
  useEffect(() => { void loadHeadendProfiles(); }, [loadHeadendProfiles]);
  async function save(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(null);
    try {
      if (!selected) throw new Error("Select a supported output profile.");
      const primarySinkId = crypto.randomUUID();
      const outputSinks: OutputSink[] = [{ sink_id: primarySinkId, label: "Primary transport", kind: transportKind, destination: destination.trim(), loudness_target_lufs: selected.loudness_target_lufs, required: true, enabled: true, encryption_configured: transportKind === "srt" && Boolean(passphrase) }];
      const srtPassphrases: Record<string, string> = {};
      if (transportKind === "srt" && passphrase) srtPassphrases[primarySinkId] = passphrase;
      if (redundantDestination.trim()) {
        const redundantSinkId = crypto.randomUUID();
        outputSinks.push({ sink_id: redundantSinkId, label: "Redundant transport", kind: transportKind, destination: redundantDestination.trim(), loudness_target_lufs: selected.loudness_target_lufs, required: true, enabled: true, encryption_configured: transportKind === "srt" && Boolean(redundantPassphrase) });
        if (transportKind === "srt" && redundantPassphrase) srtPassphrases[redundantSinkId] = redundantPassphrase;
      }
      await request<Channel>("/api/v1/channels", { method: "PUT", body: JSON.stringify({ channel_id: crypto.randomUUID(), display_name: name.trim(), output_profile: outputProfile, output_sinks: outputSinks, enabled: true, srt_passphrases: srtPassphrases }) });
      setName(""); setRedundantDestination(""); setPassphrase(""); setRedundantPassphrase(""); await onRefresh();
    } catch (problem) { setError(problem instanceof Error ? problem.message : "Channel could not be saved."); }
    finally { setBusy(false); }
  }
  async function toggle(channel: Channel) { await request<Channel>("/api/v1/channels", { method: "PUT", body: JSON.stringify({ ...channel, enabled: !channel.enabled }) }); await onRefresh(); }
  const selected = outputProfiles.find((profile) => profile.profile_id === outputProfile);
  return <><header className="page-header"><div><p className="eyebrow">Output control</p><h1>Channels</h1><p>Each enabled channel owns an isolated persistent media worker and explicit stable output branches.</p></div></header><section className="split-grid channels-layout"><article className="panel"><p className="eyebrow">Configured outputs</p><h2>{channels.length || "No"} channel{channels.length === 1 ? "" : "s"}</h2>{channels.length ? <div className="channel-cards">{channels.map((channel) => { const profile = outputProfiles.find((item) => item.profile_id === channel.output_profile); const headend = channel.output_sinks.find((sink) => sink.headend)?.headend; const namedHeadend = headendProfiles?.find((item) => item.profile_id === headend?.profile_id); const blockedHeadendStart = !channel.enabled && headend !== undefined && !canStartHeadendProfile(namedHeadend?.execution_state ?? null); const blockedStartLabel = namedHeadend ? "Headend profile blocked" : "Headend status unavailable"; return <article className="channel-card" key={channel.channel_id}><div className="channel-top"><div className="signal-icon">◉</div><StatusPill status={channel.enabled ? "enabled" : "disabled"} /></div><h3>{channel.display_name}</h3><p>{profile?.display_name ?? channel.output_profile} · {channel.output_sinks.filter((sink) => sink.enabled).length} sink{channel.output_sinks.filter((sink) => sink.enabled).length === 1 ? "" : "s"}</p>{headend && <span className="channel-headend-tag">{headend.profile_id.replaceAll("-", " ")} · {(headend.muxrate_kbps / 1000).toFixed(1)} Mb/s{namedHeadend?.execution_state === "staged_only" ? " · staging only" : ""}</span>}{channel.output_sinks.map((sink) => <code key={sink.sink_id}>{sink.label} ({sink.kind.toUpperCase()}{sink.encryption_configured ? " · encrypted" : ""}): {sink.destination}</code>)}<button className="secondary wide" disabled={blockedHeadendStart} title={blockedHeadendStart ? namedHeadend?.execution_detail ?? headendProfilesError ?? "The installed headend catalog has not been verified." : undefined} onClick={() => void toggle(channel)}>{channel.enabled ? "Stop output" : blockedHeadendStart ? blockedStartLabel : "Start output"}</button></article>; })}</div> : <Empty title="No output configured" detail="Create the first persistent channel worker." action="Use the form" onAction={() => document.querySelector<HTMLInputElement>("#channel-name")?.focus()} />}</article><article className="panel sticky-form"><p className="eyebrow">New output</p><h2>Add a channel</h2><form onSubmit={save}><label>Channel name<input id="channel-name" required minLength={2} value={name} onChange={(event) => setName(event.target.value)} placeholder="Government Channel 12" /></label><label>Output profile<select required value={outputProfile} onChange={(event) => setOutputProfile(event.target.value as Channel["output_profile"])}>{outputProfiles.map((profile) => <option key={profile.profile_id} value={profile.profile_id}>{profile.display_name}</option>)}</select>{selected && <small>{selected.intended_use} · {(selected.transport_bitrate_bps / 1_000_000).toFixed(1)} Mb/s CBR transport</small>}</label><label>Transport<select value={transportKind} onChange={(event) => { const next = event.target.value as OutputSink["kind"]; setTransportKind(next); setDestination(next === "srt" ? "srt://127.0.0.1:5600" : "127.0.0.1:5500"); setRedundantDestination(""); setPassphrase(""); setRedundantPassphrase(""); }}><option value="udp-ts">UDP MPEG-TS</option><option value="srt">SRT caller</option></select><small>SRT uses caller mode with a fixed 200 ms latency and optional AES-256 encryption.</small></label><label>Primary {transportKind === "srt" ? "SRT URI" : "UDP destination"}<input required value={destination} onChange={(event) => setDestination(event.target.value)} placeholder={transportKind === "srt" ? "srt://192.168.1.100:5600" : "192.168.1.100:5000"} /><small>{transportKind === "srt" ? "Use a credential-free srt://IP:port URI." : "Use 127.0.0.1 for a local proof channel."}</small></label>{transportKind === "srt" && <label>Primary passphrase <small>Optional</small><input type="password" minLength={10} maxLength={79} autoComplete="new-password" value={passphrase} onChange={(event) => setPassphrase(event.target.value)} /><small>Stored with Windows machine encryption and never returned by the API.</small></label>}<label>Redundant {transportKind === "srt" ? "SRT URI" : "UDP destination"} <small>Optional</small><input value={redundantDestination} onChange={(event) => setRedundantDestination(event.target.value)} placeholder={transportKind === "srt" ? "srt://192.168.2.100:5600" : "192.168.2.100:5000"} /><small>The persistent graph sends the same encoded transport to both required branches.</small></label>{transportKind === "srt" && redundantDestination.trim() && <label>Redundant passphrase <small>Optional</small><input type="password" minLength={10} maxLength={79} autoComplete="new-password" value={redundantPassphrase} onChange={(event) => setRedundantPassphrase(event.target.value)} /></label>}{error && <div className="error-box">{error}</div>}<button className="primary wide" disabled={busy || outputProfiles.length === 0}>{busy ? "Saving…" : "Create & start channel"}</button></form></article></section>{channels.length > 0 && <section className="fallback-slate-grid">{channels.map((channel) => <FallbackSlatePanel key={channel.channel_id} channelId={channel.channel_id} channelName={channel.display_name} enabled={channel.enabled} />)}</section>}{channels.length > 0 && <section className="caption-policy-grid">{channels.map((channel) => <CaptionPolicyPanel key={channel.channel_id} channelId={channel.channel_id} channelName={channel.display_name} enabled={channel.enabled} />)}</section>}<HeadendWorkflow channels={channels} profiles={headendProfiles} loadingCatalog={headendProfilesLoading} catalogError={headendProfilesError} onReloadProfiles={loadHeadendProfiles} onRefresh={onRefresh} />{channels.filter((channel) => channel.enabled).map((channel) => <LoudnessProofPanel key={channel.channel_id} channelId={channel.channel_id} sinks={channel.output_sinks} />)}</>;
}

function Schedule({ dashboard, channels }: { dashboard: Dashboard | null; channels: Channel[] }) {
  const readyAssets = dashboard?.by_asset.filter((asset) => asset.readiness_state === "ready") ?? [];
  const [channelId, setChannelId] = useState(channels[0]?.channel_id ?? ""); const [assetId, setAssetId] = useState(readyAssets[0]?.asset_id ?? ""); const [title, setTitle] = useState(""); const [start, setStart] = useState(""); const [duration, setDuration] = useState("60"); const [plan, setPlan] = useState<CommitPlan | null>(null); const [item, setItem] = useState<ScheduleItem | null>(null); const [report, setReport] = useState<CommitReport | null>(null); const [busy, setBusy] = useState(false); const [error, setError] = useState<string | null>(null);
  useEffect(() => { if (!channelId && channels[0]) setChannelId(channels[0].channel_id); }, [channelId, channels]);
  useEffect(() => { if (!assetId && readyAssets[0]) setAssetId(readyAssets[0].asset_id); }, [assetId, readyAssets]);
  async function prepare(event: FormEvent) { event.preventDefault(); setBusy(true); setError(null); setReport(null); try { const scheduleItem: ScheduleItem = { item_id: crypto.randomUUID(), channel_id: channelId, asset_id: assetId, title: title.trim(), starts_at_unix_ms: new Date(start).getTime(), duration_ms: Math.round(Number(duration) * 1000), state: "draft" }; await request<ScheduleItem>("/api/v1/schedule/items", { method: "PUT", body: JSON.stringify(scheduleItem) }); const nextPlan = await request<CommitPlan>("/api/v1/schedule/prepare", { method: "POST", body: JSON.stringify({ plan_id: `plan-${crypto.randomUUID()}`, schedule_item_id: scheduleItem.item_id }) }); setItem(scheduleItem); setPlan(nextPlan); } catch (problem) { setError(problem instanceof Error ? problem.message : "The schedule could not be prepared."); } finally { setBusy(false); } }
  async function approve() { if (!plan || !item) return; setBusy(true); setError(null); try { const nextReport = await request<CommitReport>("/api/v1/schedule/commit", { method: "POST", body: JSON.stringify({ report_id: `report-${crypto.randomUUID()}`, plan_id: plan.plan_id, schedule_item_id: item.item_id, operator_notes: "Approved in TownLight Station operator console." }) }); setReport(nextReport); const reportId = nextReport.report_id; const deadline = Date.now() + Math.max(30_000, item.starts_at_unix_ms - Date.now() + item.duration_ms + 10_000); const poll = async () => { const current = await request<CommitReport>(`/api/v1/schedule/commits/${reportId}`); setReport(current); if (!["completed", "error", "cancelled"].includes(current.dispatch_status) && Date.now() < deadline) window.setTimeout(() => void poll(), 500); }; void poll(); } catch (problem) { setError(problem instanceof Error ? problem.message : "Approval could not be committed."); } finally { setBusy(false); } }
  return <><header className="page-header"><div><p className="eyebrow">Commit to air</p><h1>Schedule a program</h1><p>Every approval reruns readiness and conflict checks under the station write lock.</p></div></header>{(!channels.length || !readyAssets.length) ? <section className="panel"><Empty title="Schedule prerequisites are missing" detail={!channels.length ? "Configure a channel before scheduling." : "Import and verify at least one air-ready asset."} action={!channels.length ? "Channel required" : "Ready media required"} onAction={() => undefined} /></section> : <section className="split-grid schedule-layout"><article className="panel sticky-form"><p className="eyebrow">Program details</p><h2>Create draft</h2><form onSubmit={prepare}><label>Channel<select required value={channelId} onChange={(event) => setChannelId(event.target.value)}>{channels.map((channel) => <option key={channel.channel_id} value={channel.channel_id}>{channel.display_name}</option>)}</select></label><label>Air-ready asset<select required value={assetId} onChange={(event) => setAssetId(event.target.value)}>{readyAssets.map((asset) => <option key={asset.asset_id} value={asset.asset_id}>{asset.media_path.split(/[\\/]/).pop()} · {formatDuration(asset.duration_ms)}</option>)}</select></label><label>Program title<input required value={title} onChange={(event) => setTitle(event.target.value)} placeholder="City Council Meeting" /></label><label>Starts at<input required type="datetime-local" value={start} onChange={(event) => setStart(event.target.value)} /></label><label>Air duration (seconds)<input required type="number" min="1" value={duration} onChange={(event) => setDuration(event.target.value)} /></label>{error && <div className="error-box">{error}</div>}<button className="primary wide" disabled={busy}>{busy ? "Checking…" : "Prepare commit"}</button></form></article><article className="panel"><p className="eyebrow">Approval gate</p><h2>Commit preview</h2>{!plan || !item ? <Empty title="No draft prepared" detail="Enter the program details to run conflict, gap, duration, and media-readiness checks." action="Complete the form" onAction={() => document.querySelector<HTMLInputElement>(".schedule-layout input")?.focus()} /> : <div className="commit-preview"><div className={`gate-banner ${plan.dry_run_passed ? "pass" : "fail"}`}><span>{plan.dry_run_passed ? "✓" : "!"}</span><div><strong>{plan.dry_run_passed ? "Safe to commit" : "Commit blocked"}</strong><p>{plan.dry_run_passed ? "No conflicts or missing media were found." : "Resolve every blocker, then prepare again."}</p></div></div><dl><div><dt>Program</dt><dd>{item.title}</dd></div><div><dt>Starts</dt><dd>{formatTime(item.starts_at_unix_ms)}</dd></div><div><dt>Duration</dt><dd>{formatDuration(item.duration_ms)}</dd></div><div><dt>Conflicts</dt><dd>{plan.conflicts.length}</dd></div><div><dt>Missing media</dt><dd>{plan.missing_media_detail ? 1 : 0}</dd></div></dl>{plan.missing_media_detail && <div className="error-box">{plan.missing_media_detail}</div>}<button className="primary wide" disabled={!plan.dry_run_passed || busy || Boolean(report)} onClick={() => void approve()}>{report ? "Approval recorded" : "Approve & commit to air"}</button>{report && <div className="dispatch"><span className="live-dot" /><div><strong>Dispatch {report.dispatch_status.replaceAll("_", " ")}</strong><p>The channel worker advances this record only after durable acknowledgements.</p></div><StatusPill status={report.dispatch_status} /></div>}</div>}</article></section>}</>;
}

function AlertRuleEditor({ rule, destinations, outputChannels, onRefresh }: { rule: AlertRule; destinations: AlertChannel[]; outputChannels: Channel[]; onRefresh: () => Promise<void> }) {
  const [draft, setDraft] = useState(rule);
  const [routeAll, setRouteAll] = useState(rule.channel_ids.length === 0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { setDraft(rule); setRouteAll(rule.channel_ids.length === 0); }, [rule]);
  function toggleDestination(channelId: string) {
    setDraft((current) => ({ ...current, channel_ids: current.channel_ids.includes(channelId) ? current.channel_ids.filter((id) => id !== channelId) : [...current.channel_ids, channelId] }));
  }
  async function save() {
    if (!routeAll && draft.channel_ids.length === 0) { setError("Select at least one destination or route to all enabled destinations."); return; }
    setBusy(true); setError(null);
    try {
      await request<AlertRule>(`/api/v1/alert-rules/${rule.rule_id}`, { method: "PUT", body: JSON.stringify({ condition: draft.condition, enabled: draft.enabled, severity: draft.severity, channel_ids: routeAll ? [] : draft.channel_ids, dedupe_window_seconds: draft.dedupe_window_seconds, re_alert_after_seconds: draft.re_alert_after_seconds, scope_channel_id: draft.scope_channel_id, notify_on_resolve: draft.notify_on_resolve }) });
      await onRefresh();
    } catch (problem) { setError(problem instanceof Error ? problem.message : "The alert rule could not be saved."); }
    finally { setBusy(false); }
  }
  return <article className="rule-card"><div className="rule-title"><div><strong>{draft.condition.replaceAll("-", " ")}</strong><small>{draft.scope_channel_id ? `Scoped to ${outputChannels.find((channel) => channel.channel_id === draft.scope_channel_id)?.display_name ?? draft.scope_channel_id}` : "All output channels"}</small></div><label className="switch-label"><input type="checkbox" checked={draft.enabled} onChange={(event) => setDraft({ ...draft, enabled: event.target.checked })} />Enabled</label></div><div className="rule-grid"><label>Severity<select value={draft.severity} onChange={(event) => setDraft({ ...draft, severity: event.target.value as AlertRule["severity"] })}><option value="critical">Critical</option><option value="warning">Warning</option><option value="info">Info</option></select></label><label>Re-alert after<select value={draft.re_alert_after_seconds} onChange={(event) => setDraft({ ...draft, re_alert_after_seconds: Number(event.target.value) })}><option value="0">Never</option><option value="1800">30 minutes</option><option value="3600">1 hour</option><option value="7200">2 hours</option><option value="21600">6 hours</option></select></label><label>Dedupe window (seconds)<input type="number" min="0" max="86400" value={draft.dedupe_window_seconds} onChange={(event) => setDraft({ ...draft, dedupe_window_seconds: Number(event.target.value) })} /></label><label>Output scope<select value={draft.scope_channel_id ?? ""} onChange={(event) => setDraft({ ...draft, scope_channel_id: event.target.value || null })}><option value="">All outputs</option>{outputChannels.map((channel) => <option key={channel.channel_id} value={channel.channel_id}>{channel.display_name}</option>)}</select></label></div><div className="rule-options"><label className="switch-label"><input type="checkbox" checked={draft.notify_on_resolve} onChange={(event) => setDraft({ ...draft, notify_on_resolve: event.target.checked })} />Notify when resolved</label><label className="switch-label"><input type="checkbox" checked={routeAll} onChange={(event) => setRouteAll(event.target.checked)} />All enabled destinations</label></div>{!routeAll && <div className="destination-checks">{destinations.map((destination) => <label key={destination.channel_id}><input type="checkbox" checked={draft.channel_ids.includes(destination.channel_id)} onChange={() => toggleDestination(destination.channel_id)} />{destination.label} <small>{destination.kind}</small></label>)}</div>}{error && <div className="error-box">{error}</div>}<button className="secondary wide" disabled={busy} onClick={() => void save()}>{busy ? "Saving…" : "Save rule"}</button></article>;
}

function SelfTestScheduleEditor({ schedule, latest, busy, onRun, onRefresh }: { schedule: SelfTestSchedule; latest: SystemSelfTest | undefined; busy: boolean; onRun: (kind: SelfTestKind) => Promise<void>; onRefresh: () => Promise<void> }) {
  const [enabled, setEnabled] = useState(schedule.enabled);
  const [time, setTime] = useState(`${String(schedule.local_hour).padStart(2, "0")}:${String(schedule.local_minute).padStart(2, "0")}`);
  const [weekday, setWeekday] = useState(schedule.weekday ?? 0);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function save() {
    const [hour, minute] = time.split(":").map(Number);
    setSaving(true); setError(null);
    try {
      await request<SelfTestSchedule>(`/api/v1/self-test-schedules/${schedule.kind}`, { method: "PUT", body: JSON.stringify({ enabled, local_hour: hour, local_minute: minute, weekday: schedule.kind === "weekly" ? weekday : null }) });
      await onRefresh();
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "The self-test schedule could not be saved.");
    } finally { setSaving(false); }
  }
  return <article className="self-test-card">
    <div className="self-test-head"><div><strong>{schedule.kind} validation</strong><small>{latest ? `${latest.trigger} · ${formatTime(latest.finished_at_unix_ms)}` : "No completed run"}</small></div><StatusPill status={latest?.status ?? "pending"} /></div>
    <p>{latest?.summary ?? "Run once now or wait for the configured local schedule."}</p>
    {latest && <div className="self-test-checks">{Object.entries(latest.checks).map(([name, passed]) => <span key={name} className={passed ? "check-pass" : "check-fail"}>{passed ? "✓" : "!"} {name.replaceAll("_", " ")}</span>)}</div>}
    <div className="self-test-controls"><label className="switch-label"><input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />Scheduled</label><label>Local time<input type="time" value={time} onChange={(event) => setTime(event.target.value)} /></label>{schedule.kind === "weekly" && <label>Day<select value={weekday} onChange={(event) => setWeekday(Number(event.target.value))}><option value={0}>Sunday</option><option value={1}>Monday</option><option value={2}>Tuesday</option><option value={3}>Wednesday</option><option value={4}>Thursday</option><option value={5}>Friday</option><option value={6}>Saturday</option></select></label>}</div>
    {error && <div className="error-box">{error}</div>}
    <div className="button-row"><button className="secondary" disabled={saving || busy} onClick={() => void save()}>{saving ? "Saving…" : "Save schedule"}</button><button className="primary" disabled={saving || busy} onClick={() => void onRun(schedule.kind)}>{busy ? "Running…" : "Run now"}</button></div>
  </article>;
}

function Health({ runtime, resources, selfTests, selfTestSchedules, updateStatus, alerts, alertChannels, deliveries, rules, outputChannels, onRefresh }: { runtime: RuntimeSafeToAirStatus | null; resources: SystemResourceSample | null; selfTests: SystemSelfTest[]; selfTestSchedules: SelfTestSchedule[]; updateStatus: UpdateStatus | null; alerts: AlertEvent[]; alertChannels: AlertChannel[]; deliveries: AlertDelivery[]; rules: AlertRule[]; outputChannels: Channel[]; onRefresh: () => Promise<void> }) {
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
  const [supportBundle, setSupportBundle] = useState<SupportBundleRecord | null>(null);
  const [supportError, setSupportError] = useState<string | null>(null);
  const [updateError, setUpdateError] = useState<string | null>(null);
  const [maintenanceConfirmation, setMaintenanceConfirmation] = useState("");
  const firing = alerts.filter((alert) => alert.state === "firing");
  const recentDeliveries = [...deliveries].sort((a, b) => b.created_at_unix_ms - a.created_at_unix_ms).slice(0, 12);
  async function acknowledge(eventId: string) {
    setBusyId(eventId);
    try {
      await request<AlertEvent>(`/api/v1/alerts/${eventId}/acknowledge`, { method: "POST", body: "{}" });
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
  async function runSelfTest(kind: SelfTestKind) {
    setBusyId(`self-test-${kind}`); setFormError(null);
    try {
      await request<SystemSelfTest>(`/api/v1/self-tests/run?kind=${kind}`, { method: "POST" });
      await onRefresh();
    } catch (problem) {
      setFormError(problem instanceof Error ? problem.message : "The station self-test could not run.");
    } finally { setBusyId(null); }
  }
  async function createSupportBundle() {
    setBusyId("support-bundle"); setSupportError(null);
    try {
      setSupportBundle(await request<SupportBundleRecord>("/api/v1/support-bundles", { method: "POST" }));
    } catch (problem) {
      setSupportError(problem instanceof Error ? problem.message : "The support bundle could not be created.");
    } finally { setBusyId(null); }
  }
  async function checkUpdates() {
    setBusyId("update-check"); setUpdateError(null);
    try {
      await request<UpdateCheckRecord>("/api/v1/updates/check", { method: "POST" });
      await onRefresh();
    } catch (problem) {
      setUpdateError(problem instanceof Error ? problem.message : "The signed release check could not run.");
    } finally { setBusyId(null); }
  }
  async function stageUpdate() {
    setBusyId("update-stage"); setUpdateError(null);
    try {
      await request<StagedPackageRecord>("/api/v1/updates/stage", { method: "POST" });
      await onRefresh();
    } catch (problem) {
      setUpdateError(problem instanceof Error ? problem.message : "The verified release package could not be staged.");
    } finally { setBusyId(null); }
  }
  async function prepareUpdate() {
    setBusyId("update-prepare"); setUpdateError(null);
    try {
      await request<PreparedSlotRecord>("/api/v1/updates/prepare", { method: "POST" });
      await onRefresh();
    } catch (problem) {
      setUpdateError(problem instanceof Error ? problem.message : "The verified application slot could not be prepared.");
    } finally { setBusyId(null); }
  }
  async function applyUpdate(event: FormEvent) {
    event.preventDefault();
    setBusyId("update-apply"); setUpdateError(null);
    try {
      await request("/api/v1/updates/apply", {
        method: "POST",
        body: JSON.stringify({ confirmation: maintenanceConfirmation }),
      });
      setMaintenanceConfirmation("");
      window.setTimeout(() => void onRefresh(), 3_000);
    } catch (problem) {
      setUpdateError(problem instanceof Error ? problem.message : "The maintenance activation could not be queued.");
    } finally { setBusyId(null); }
  }
  return <>
    <header className="page-header"><div><p className="eyebrow">Unattended survival</p><h1>System health & alerts</h1><p>Runtime truth comes from durable worker heartbeats and incident records.</p></div></header>
    {runtime ? <section className={`health-hero health-${runtime.color}`}><div className="health-orb">{runtime.color === "green" ? "✓" : "!"}</div><div><p className="eyebrow">Runtime safe-to-air</p><h2>{runtime.label}</h2><p>{runtime.operator_message}</p></div><div className="health-counts"><strong>{runtime.active_critical_alerts}</strong><small>critical</small><strong>{runtime.active_warning_alerts}</strong><small>warning</small></div></section> : <section className="panel"><p>Runtime status is loading.</p></section>}
    <section className="resource-grid"><article><span>CPU</span><strong>{resources?.cpu_percent == null ? "—" : `${resources.cpu_percent.toFixed(1)}%`}</strong></article><article><span>Memory</span><strong>{resources?.ram_used_gb == null || resources.ram_total_gb == null ? "—" : `${resources.ram_used_gb.toFixed(1)} / ${resources.ram_total_gb.toFixed(1)} GB`}</strong></article><article><span>Media free</span><strong>{resources?.media_volume_free_gb == null ? "—" : `${resources.media_volume_free_gb.toFixed(1)} GB`}</strong></article><article><span>Backup probe</span><strong>{resources?.backup_volume_writable ? "Writable" : "Not verified"}</strong><small>{resources ? formatTime(resources.sampled_at_unix_ms) : "Waiting for first sample"}</small></article></section>
    <section className="split-grid health-layout"><article className="panel"><div className="panel-head"><div><p className="eyebrow">Enabled outputs</p><h2>Channel runtime</h2></div><span className="count">{runtime?.channels.length ?? 0}</span></div><div className="runtime-list">{runtime?.channels.map((channel) => <article className={`runtime-card runtime-${channel.color}`} key={channel.channel_id}><div><span className="runtime-dot" /><strong>{channel.display_name}</strong></div><StatusPill status={channel.state} /><p>{channel.operator_message}</p><small>{formatDuration(channel.seconds_in_state * 1000)} in state · heartbeat {channel.heartbeat_age_seconds}s ago</small></article>)}</div>{runtime?.channels.length === 0 && <div className="clear-state"><div className="empty-orbit">→</div><strong>No enabled outputs</strong><p>Enable a channel to begin runtime monitoring.</p></div>}</article><article className="panel"><div className="panel-head"><div><p className="eyebrow">Notify on first failure</p><h2>Active alerts</h2></div><span className="count">{firing.length}</span></div>{firing.length ? <div className="alert-list">{firing.map((alert) => <article className={`alert-card alert-${alert.severity}`} key={alert.event_id}><div className="alert-top"><StatusPill status={alert.severity} /><small>{alert.source_section} · {formatTime(alert.first_observed_at_unix_ms)}</small></div><strong>{alert.summary}</strong><p>{alert.detail || alert.condition.replaceAll("-", " ")}</p><small>{alert.occurrence_count > 1 ? `Observed ${alert.occurrence_count} times; repeats deduplicated.` : "First failure recorded."}</small>{alert.acknowledged_at_unix_ms ? <span className="acknowledged">✓ Acknowledged by {alert.acknowledged_by}</span> : <button className="secondary wide" disabled={busyId === alert.event_id} onClick={() => void acknowledge(alert.event_id)}>{busyId === alert.event_id ? "Recording…" : "Acknowledge"}</button>}</article>)}</div> : <div className="clear-state"><div className="empty-orbit">✓</div><strong>No active alerts</strong><p>Runtime failures will appear here once and remain until the condition recovers.</p></div>}</article></section>
    <section className="split-grid alerting-layout"><article className="panel"><div className="panel-head"><div><p className="eyebrow">Off-site notification</p><h2>Destinations</h2></div><span className="count">{alertChannels.length}</span></div>{alertChannels.length ? <div className="destination-list">{alertChannels.map((channel) => <article className="destination-card" key={channel.channel_id}><div><strong>{channel.label}</strong><small>{channel.kind} · {channel.target_redacted}</small>{channel.quiet_hours_start_utc && <small>Quiet {channel.quiet_hours_start_utc}–{channel.quiet_hours_end_utc} UTC; critical alerts bypass</small>}</div><StatusPill status={channel.enabled ? "enabled" : "disabled"} /><button className="secondary" disabled={busyId === channel.channel_id} onClick={() => void sendTest(channel.channel_id)}>{busyId === channel.channel_id ? "Queuing…" : "Send test"}</button></article>)}</div> : <div className="clear-state"><div className="empty-orbit">→</div><strong>No notification destination</strong><p>Add email, SMS, or a signed webhook for off-site failures and recoveries.</p></div>}</article><article className="panel"><p className="eyebrow">Encrypted at rest</p><h2>Add a destination</h2><form className="alert-form" onSubmit={saveDestination}><div className="form-pair"><label>Kind<select value={kind} onChange={(event) => setKind(event.target.value as AlertChannelKind)}><option value="webhook">Signed webhook</option><option value="email">Email</option><option value="sms">SMS</option></select></label><label>Label<input required minLength={2} value={label} onChange={(event) => setLabel(event.target.value)} placeholder="On-call operator" /></label></div><label>{kind === "email" ? "Recipient email" : kind === "sms" ? "Recipient phone" : "Webhook URL"}<input required value={target} onChange={(event) => setTarget(event.target.value)} placeholder={kind === "email" ? "operator@example.gov" : kind === "sms" ? "+13035550123" : "https://alerts.example.gov/station"} /></label>{kind !== "webhook" && <><label>{kind === "email" ? "SMTP endpoint" : "Provider endpoint"}<input required value={endpoint} onChange={(event) => setEndpoint(event.target.value)} placeholder={kind === "email" ? "smtps://smtp.example.gov:465" : "https://api.twilio.com/2010-04-01/Accounts/.../Messages.json"} /></label><div className="form-pair"><label>{kind === "email" ? "From email" : "From phone"}<input required value={sender} onChange={(event) => setSender(event.target.value)} /></label><label>{kind === "email" ? "SMTP username" : "Account SID"}<input required value={identity} onChange={(event) => setIdentity(event.target.value)} /></label></div></>}<label>{kind === "webhook" ? "Signing secret" : kind === "email" ? "SMTP password" : "Auth token"}<input required minLength={8} type="password" autoComplete="new-password" value={credential} onChange={(event) => setCredential(event.target.value)} /><small>Stored with Windows machine encryption and never returned by the API.</small></label><div className="form-pair"><label>Quiet start (UTC)<input type="time" value={quietStart} onChange={(event) => setQuietStart(event.target.value)} /></label><label>Quiet end (UTC)<input type="time" value={quietEnd} onChange={(event) => setQuietEnd(event.target.value)} /></label></div>{formError && <div className="error-box">{formError}</div>}<button className="primary wide" disabled={busyId === "new-destination"}>{busyId === "new-destination" ? "Saving…" : "Save destination"}</button></form></article></section>
    <section className="panel"><div className="panel-head"><div><p className="eyebrow">Automatic proof</p><h2>Station self-tests</h2></div><span className="count">{selfTests.length}</span></div><p className="muted rule-intro">Daily and weekly checks run in local station time. Every result stores its named checks and evidence path.</p><div className="self-test-grid">{selfTestSchedules.map((schedule) => <SelfTestScheduleEditor key={schedule.kind} schedule={schedule} latest={selfTests.find((record) => record.kind === schedule.kind)} busy={busyId === `self-test-${schedule.kind}`} onRun={runSelfTest} onRefresh={onRefresh} />)}</div></section>
    <section className="panel support-panel"><div className="panel-head"><div><p className="eyebrow">Redacted diagnostics</p><h2>Support bundle</h2></div><span className="count">ZIP</span></div><p className="muted rule-intro">Create a bounded diagnostic archive with a manifest and SHA-256. Credentials, the station database, raw logs, environment variables, signatures, and full media paths are excluded.</p>{supportError && <div className="error-box">{supportError}</div>}{supportBundle ? <div className="support-result"><div><strong>Support bundle ready</strong><small>{supportBundle.entries.length} files · {(supportBundle.size_bytes / 1024).toFixed(1)} KiB · {formatTime(supportBundle.created_at_unix_ms)}</small><small>{supportBundle.commissioning_report_id ? `Commissioning receipt linked to completed report ${supportBundle.commissioning_report_id}.` : "No completed guided broadcast was available, so this bundle is not commissioning evidence."}</small></div><dl><div><dt>Saved at</dt><dd>{supportBundle.path}</dd></div><div><dt>SHA-256</dt><dd><code>{supportBundle.sha256}</code></dd></div></dl>{supportBundle.retention_warning && <div className="error-box">The bundle is ready, but old-bundle cleanup needs attention: {supportBundle.retention_warning}</div>}<div className="support-actions"><a className="secondary" href={supportBundle.download_uri} download={supportBundle.file_name}>Download ZIP</a><button className="primary" disabled={busyId === "support-bundle"} onClick={() => void createSupportBundle()}>{busyId === "support-bundle" ? "Creating…" : "Create fresh bundle"}</button></div></div> : <button className="primary" disabled={busyId === "support-bundle"} onClick={() => void createSupportBundle()}>{busyId === "support-bundle" ? "Creating redacted bundle…" : "Create support bundle"}</button>}</section>
    <section className="panel update-panel"><div className="panel-head"><div><p className="eyebrow">Signed releases</p><h2>Station updates</h2></div><StatusPill status={updateStatus?.staged_package ? "staged" : updateStatus?.latest_check?.outcome ?? (updateStatus?.configured ? "never checked" : "not configured")} /></div>{updateStatus ? <div className="update-status"><div className="update-version"><span>Installed</span><strong>{updateStatus.current_version}</strong><small><code>{updateStatus.source_commit.slice(0, 12)}</code></small></div><div><strong>{updateStatus.staged_package ? `${updateStatus.staged_package.manifest.version} is verified and staged` : updateStatus.latest_check?.outcome === "update_available" ? `${updateStatus.latest_check.offered_release?.version} is available` : updateStatus.latest_check?.outcome === "updater_too_old" ? "A full installer is required" : updateStatus.latest_check?.outcome === "failed" ? "Last check failed" : updateStatus.latest_check ? "This station is up to date" : updateStatus.configured ? "Ready for first check" : "Release source not configured"}</strong><p className="muted">{updateStatus.latest_check?.error ?? updateStatus.next_step}</p>{updateStatus.feed_origin && <small>{updateStatus.release_channel} · {updateStatus.feed_origin}</small>}{updateStatus.latest_check && <small>Last checked {formatTime(updateStatus.latest_check.checked_at_unix_ms)} · {updateStatus.latest_check.trigger}</small>}{updateStatus.staged_package && <small>Staged {formatTime(updateStatus.staged_package.staged_at_unix_ms)} · {(updateStatus.staged_package.manifest.package_size_bytes / 1024 / 1024).toFixed(1)} MiB · <code>{updateStatus.staged_package.manifest.package_sha256.slice(0, 12)}</code></small>}{updateStatus.latest_check?.offered_release && <a className="secondary notes-link" href={`/api/v1/updates/release-notes?version=${encodeURIComponent(updateStatus.latest_check.offered_release.version)}`} target="_blank" rel="noreferrer">Release notes</a>}</div></div> : <div className="clear-state compact"><strong>Update identity unavailable</strong><p>The installed release receipt could not be read.</p></div>}{updateError && <div className="error-box">{updateError}</div>}<div className="update-actions"><p>Checks never download packages. Staging is an explicit action that streams through signed length and SHA-256 checks into managed quarantine; staged bytes are never loaded or executed. Installation still requires a separate maintenance window.</p><div className="button-row"><button className="secondary" disabled={!updateStatus?.configured || busyId === "update-check" || busyId === "update-stage"} onClick={() => void checkUpdates()}>{busyId === "update-check" ? "Verifying…" : "Check signed feed"}</button><button className="primary" disabled={updateStatus?.latest_check?.outcome !== "update_available" || Boolean(updateStatus?.staged_package) || busyId === "update-check" || busyId === "update-stage"} onClick={() => void stageUpdate()}>{busyId === "update-stage" ? "Downloading & verifying…" : updateStatus?.staged_package ? "Package staged" : "Stage verified package"}</button></div></div></section>
    <section className="panel"><div className="panel-head"><div><p className="eyebrow">Notify once, then pace</p><h2>Alert rules</h2></div><span className="count">{rules.length}</span></div><p className="muted rule-intro">Rules own severity, output scope, destinations, resolution notices, and the minimum interval between repeat pages.</p><div className="rule-list">{rules.map((rule) => <AlertRuleEditor key={rule.rule_id} rule={rule} destinations={alertChannels} outputChannels={outputChannels} onRefresh={onRefresh} />)}</div></section>
    <section className="panel"><div className="panel-head"><div><p className="eyebrow">Durable delivery ledger</p><h2>Recent notifications</h2></div><span className="count">{deliveries.length}</span></div>{recentDeliveries.length ? <div className="delivery-list">{recentDeliveries.map((delivery) => { const channel = alertChannels.find((item) => item.channel_id === delivery.alert_channel_id); return <article className="delivery-row" key={delivery.delivery_id}><div><strong>{delivery.notification_kind === "test" ? "Operator test" : `${delivery.notification_kind} alert`} · {channel?.label ?? "Removed destination"}</strong><small>{formatTime(delivery.created_at_unix_ms)} · {delivery.attempts} attempt{delivery.attempts === 1 ? "" : "s"}{delivery.last_error ? ` · ${delivery.last_error}` : ""}</small></div><StatusPill status={delivery.state} /></article>; })}</div> : <div className="clear-state compact"><strong>No delivery attempts</strong><p>Tests and incident notifications will remain visible here across restarts.</p></div>}</section>
    {updateStatus?.maintenance && <section className={`panel maintenance-history maintenance-${updateStatus.maintenance.phase}`}><div className="panel-head"><div><p className="eyebrow">Durable maintenance ledger</p><h2>Latest update transaction</h2></div><StatusPill status={updateStatus.maintenance.phase} /></div><div className="maintenance-summary"><strong>{updateStatus.maintenance.from_version} → {updateStatus.maintenance.candidate_version}</strong><p>{updateStatus.maintenance.detail}</p><small>{formatTime(updateStatus.maintenance.updated_at_unix_ms)} · requested by {updateStatus.maintenance.requested_by} · <code>{updateStatus.maintenance.transaction_id}</code></small></div></section>}
    {updateStatus?.staged_package && <section className="panel update-panel"><div className="panel-head"><div><p className="eyebrow">Immutable application slot</p><h2>{updateStatus.prepared_slot ? "Install prepared update" : "Prepare verified update"}</h2></div><StatusPill status={updateStatus.prepared_slot ? "prepared" : "not prepared"} /></div><p className="muted">Preparation revalidates the complete signed package, rejects unexpected archive entries, and extracts exact per-file hashes into a new version-and-commit slot. It does not stop channels, switch the service, execute candidate code, or install the update.</p>{updateStatus.prepared_slot ? <div className="maintenance-layout"><div className="support-result"><div><strong>{updateStatus.prepared_slot.manifest.version} is prepared</strong><small>Prepared {formatTime(updateStatus.prepared_slot.prepared_at_unix_ms)} · <code>{updateStatus.prepared_slot.slot_directory_name}</code></small></div></div><form className="maintenance-form" onSubmit={applyUpdate}><div className="maintenance-warning"><strong>This interrupts every output.</strong><p>The station service will stop, switch to the authenticated candidate, restart, and prove both health and green safe-to-air. If proof fails, the independent watchdog restores and proves the previous application. Do this only during an announced maintenance window.</p></div><p className="muted">The signed-in operator identity will be recorded by the server.</p><label>Type INSTALL UPDATE<input required value={maintenanceConfirmation} onChange={(event) => setMaintenanceConfirmation(event.target.value)} autoComplete="off" /></label><button className="danger wide" disabled={runtime?.color !== "green" || busyId === "update-apply" || maintenanceConfirmation !== "INSTALL UPDATE"}>{busyId === "update-apply" ? "Handing off to watchdog…" : runtime?.color !== "green" ? "Blocked until safe-to-air is green" : "Stop outputs and install update"}</button></form></div> : <button className="primary" disabled={busyId === "update-prepare"} onClick={() => void prepareUpdate()}>{busyId === "update-prepare" ? "Validating & extracting…" : "Prepare immutable slot"}</button>}</section>}
  </>;
}

function liveEndpoint(source: LiveSource | null): { bindIp: string; port: string } {
  const endpoint = source?.endpoint?.UdpMpegTs;
  return { bindIp: endpoint?.bind_ip ?? "0.0.0.0", port: endpoint ? String(endpoint.port) : "5000" };
}

function liveStateLabel(state: LiveTakeoverState): string {
  return state.replaceAll("_", " ");
}

function Live({ channels }: { channels: Channel[] }) {
  const [selectedChannelId, setSelectedChannelId] = useState(channels[0]?.channel_id ?? "");
  const [sources, setSources] = useState<LiveSource[]>([]);
  const [authority, setAuthority] = useState<LiveAuthority | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [session, setSession] = useState<LiveTakeoverSession | null>(null);
  const [audit, setAudit] = useState<LiveAuditEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [selectedSourceId, setSelectedSourceId] = useState("");
  const [confirmAction, setConfirmAction] = useState<"take" | "return" | null>(null);
  const [editingSource, setEditingSource] = useState<LiveSource | null>(null);
  const [sourceEditorOpen, setSourceEditorOpen] = useState(false);
  const [sourceLabel, setSourceLabel] = useState("");
  const [sourceBindIp, setSourceBindIp] = useState("0.0.0.0");
  const [sourcePort, setSourcePort] = useState("5000");
  const [sourceEnabled, setSourceEnabled] = useState(true);
  const channelLoadRef = useRef(0);
  const sessionPollRef = useRef(0);

  const selectedChannel = channels.find((channel) => channel.channel_id === selectedChannelId) ?? null;
  const selectedSource = sources.find((source) => source.source_id === selectedSourceId) ?? null;
  const sourceName = (sourceId: string) => sources.find((source) => source.source_id === sourceId)?.label ?? sourceId;
  const isTerminal = session?.state === "completed" || session?.state === "failed";

  const loadChannel = useCallback(async (channelId: string, resetSession: boolean) => {
    if (!channelId) return;
    const loadToken = ++channelLoadRef.current;
    setLoading(true);
    setError(null);
    if (resetSession) {
      sessionPollRef.current += 1;
      setSessionId(null);
      setSession(null);
      setAudit([]);
      setNotice(null);
    }
    try {
      const [nextSources, nextAuthority, active] = await Promise.all([
        request<LiveSource[]>(`/api/v1/channels/${channelId}/live-sources`),
        request<LiveAuthority>(`/api/v1/channels/${channelId}/live-authority`),
        request<LiveTakeoverSession | null>(`/api/v1/channels/${channelId}/active-live-takeover`),
      ]);
      if (loadToken !== channelLoadRef.current) return;
      setSources(nextSources);
      setAuthority(nextAuthority);
      setSelectedSourceId((current) => nextSources.some((source) => source.source_id === current) ? current : nextSources.find((source) => source.enabled)?.source_id ?? nextSources[0]?.source_id ?? "");
      if (active) {
        setSessionId(active.session_id);
        setSession(active);
      }
      setConflict(false);
    } catch (problem) {
      if (loadToken !== channelLoadRef.current) return;
      if (problem instanceof ApiError && problem.status === 409) setConflict(true);
      setError(problem instanceof Error ? problem.message : "Live state could not be loaded.");
    } finally {
      if (loadToken !== channelLoadRef.current) return;
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!channels.some((channel) => channel.channel_id === selectedChannelId)) setSelectedChannelId(channels[0]?.channel_id ?? "");
  }, [channels, selectedChannelId]);

  useEffect(() => {
    if (selectedChannelId) void loadChannel(selectedChannelId, true);
  }, [loadChannel, selectedChannelId]);

  const pollSession = useCallback(async (id: string) => {
    const pollToken = sessionPollRef.current;
    try {
      const [nextSession, nextAudit] = await Promise.all([
        request<LiveTakeoverSession>(`/api/v1/live-takeovers/${id}`),
        request<LiveAuditEvent[]>(`/api/v1/live-takeovers/${id}/audit`),
      ]);
      if (pollToken !== sessionPollRef.current) return;
      setSession(nextSession);
      setAuthority((current) => current ? { ...current, authority_revision: nextSession.authority_revision } : { channel_id: nextSession.channel_id, authority_revision: nextSession.authority_revision });
      setAudit(nextAudit);
      setError(null);
    } catch (problem) {
      if (pollToken !== sessionPollRef.current) return;
      if (problem instanceof ApiError && problem.status === 409) setConflict(true);
      setError(problem instanceof Error ? problem.message : "The live session could not be read.");
    }
  }, []);

  useEffect(() => {
    if (!sessionId || isTerminal) return;
    const pollToken = ++sessionPollRef.current;
    void pollSession(sessionId);
    const timer = window.setInterval(() => void pollSession(sessionId), 1200);
    return () => {
      if (pollToken === sessionPollRef.current) sessionPollRef.current += 1;
      window.clearInterval(timer);
    };
  }, [isTerminal, pollSession, sessionId]);

  function beginSource(source: LiveSource | null) {
    const endpoint = liveEndpoint(source);
    setEditingSource(source);
    setSourceEditorOpen(true);
    setSourceLabel(source?.label ?? "");
    setSourceBindIp(endpoint.bindIp);
    setSourcePort(endpoint.port);
    setSourceEnabled(source?.enabled ?? true);
    setError(null);
  }

  async function saveSource(event: FormEvent) {
    event.preventDefault();
    if (!selectedChannelId || conflict) return;
    const port = Number(sourcePort);
    if (!sourceLabel.trim() || !sourceBindIp.trim() || !Number.isInteger(port) || port < 1 || port > 65535) {
      setError("Enter a label, a station-local bind IP, and a UDP port from 1 to 65535.");
      return;
    }
    setBusy(true); setError(null); setConflict(false); setNotice(null);
    const expectedRevision = editingSource?.revision ?? 0;
    const source: LiveSource = {
      source_id: editingSource?.source_id ?? crypto.randomUUID(),
      channel_id: selectedChannelId,
      label: sourceLabel.trim(),
      enabled: sourceEnabled,
      endpoint: { UdpMpegTs: { bind_ip: sourceBindIp.trim(), port } },
      revision: expectedRevision + 1,
    };
    try {
      await request<LiveSource>(`/api/v1/live-sources/${source.source_id}`, { method: "PUT", body: JSON.stringify({ ...source, expected_revision: expectedRevision }) });
      setEditingSource(null);
      setSourceEditorOpen(false);
      setNotice("Source configuration saved. The revision is now authoritative.");
      await loadChannel(selectedChannelId, false);
    } catch (problem) {
      if (problem instanceof ApiError && problem.status === 409) setConflict(true);
      setError(problem instanceof Error ? problem.message : "Live source could not be saved.");
    } finally { setBusy(false); }
  }

  async function requestArm(event: FormEvent) {
    event.preventDefault();
    if (!selectedChannelId || !selectedSource || !authority || conflict) return;
    const takeoverReason = reason.trim();
    if (!takeoverReason) { setError("A reason is required before arming."); return; }
    setBusy(true); setError(null); setConflict(false); setNotice(null); setConfirmAction(null);
    try {
      const requested = await request<LiveTakeoverSession>("/api/v1/live-takeovers", {
        method: "POST",
        body: JSON.stringify({ session_id: crypto.randomUUID(), channel_id: selectedChannelId, source_id: selectedSource.source_id, reason: takeoverReason, expires_at_unix_ms: Date.now() + 5 * 60 * 1000, expected_authority_revision: authority.authority_revision }),
      });
      setReason("");
      setSession(null);
      setSessionId(requested.session_id);
      setNotice("Arm request submitted. Waiting for the authoritative worker state to become Armed…");
    } catch (problem) {
      if (problem instanceof ApiError && problem.status === 409) setConflict(true);
      setError(problem instanceof Error ? problem.message : "The live arm request could not be submitted.");
    } finally { setBusy(false); }
  }

  async function confirmTake() {
    if (!session || session.state !== "armed" || conflict) return;
    setBusy(true); setError(null); setConflict(false); setConfirmAction(null);
    try {
      await request<LiveTakeoverSession>(`/api/v1/live-takeovers/${session.session_id}/take`, { method: "POST", body: JSON.stringify({ expected_authority_revision: session.authority_revision }) });
      setNotice("Take Live command submitted. Waiting for the authoritative OnAir state…");
    } catch (problem) {
      if (problem instanceof ApiError && problem.status === 409) setConflict(true);
      setError(problem instanceof Error ? problem.message : "Take Live could not be submitted.");
    } finally { setBusy(false); }
  }

  async function confirmReturn() {
    if (!session || session.state !== "on_air" || conflict) return;
    setBusy(true); setError(null); setConflict(false); setConfirmAction(null);
    try {
      await request<LiveTakeoverSession>(`/api/v1/live-takeovers/${session.session_id}/return`, { method: "POST", body: JSON.stringify({ expected_authority_revision: session.authority_revision }) });
      setNotice("Return to Schedule command submitted. Waiting for completion…");
    } catch (problem) {
      if (problem instanceof ApiError && problem.status === 409) setConflict(true);
      setError(problem instanceof Error ? problem.message : "Return to Schedule could not be submitted.");
    } finally { setBusy(false); }
  }

  const duration = session?.taken_at_unix_ms ? Math.max(0, Date.now() - session.taken_at_unix_ms) : 0;
  return <>
    <header className="page-header"><div><p className="eyebrow">Live operations</p><h1>Live operator console</h1><p>Arm a verified UDP MPEG-TS source, then make a deliberate on-air transition.</p></div><div className="header-actions"><label className="live-channel-picker">Channel<select aria-label="Live channel" value={selectedChannelId} onChange={(event) => setSelectedChannelId(event.target.value)} disabled={!channels.length || busy}>{channels.length ? channels.map((channel) => <option key={channel.channel_id} value={channel.channel_id}>{channel.display_name}</option>) : <option value="">No channels configured</option>}</select></label><button className="secondary" onClick={() => void loadChannel(selectedChannelId, false)} disabled={loading || busy || !selectedChannelId}>{loading ? "Reading…" : "Refresh"}</button></div></header>
    {conflict && <div className="live-conflict" role="alert"><strong>Live authority changed elsewhere.</strong><span>This console has a stale revision. Refresh before sending another source or takeover command.</span><button className="secondary" onClick={() => { setConflict(false); setEditingSource(null); setSourceEditorOpen(false); void loadChannel(selectedChannelId, false); }}>Refresh authority</button></div>}
    {error && <div className="error-box" role="alert">{error}</div>}
    {notice && <div className="notice" role="status">{notice}</div>}
    {!selectedChannel ? <section className="panel"><div className="live-empty"><strong>No channel is configured yet.</strong><p>Create and enable a channel before configuring a live source.</p></div></section> : <>
      <section className="panel"><div className="panel-head"><div><p className="eyebrow">Input inventory</p><h2>Live sources</h2></div><div className="live-panel-actions"><span className="count">Authority rev {authority?.authority_revision ?? "—"}</span><button className="secondary" onClick={() => beginSource(null)} disabled={sourceEditorOpen}>Add source</button></div></div><p className="muted">Sources are revisioned independently. Saving a source never arms it or takes it on air.</p>
        {!sources.length && !sourceEditorOpen ? <div className="live-empty"><strong>No live sources configured.</strong><p>Add a station-local UDP MPEG-TS endpoint to begin.</p><button className="secondary" onClick={() => beginSource(null)}>Configure first source</button></div> : <div className="live-source-list">{sources.map((source) => { const endpoint = liveEndpoint(source); return <article className={`live-source-card ${source.source_id === selectedSourceId ? "selected" : ""}`} key={source.source_id}><div><strong>{source.label}</strong><small>{endpoint.bindIp}:{endpoint.port} · revision {source.revision}</small></div><StatusPill status={source.enabled ? "enabled" : "disabled"} /><div className="live-source-actions"><button className="text-button" aria-pressed={source.source_id === selectedSourceId} onClick={() => setSelectedSourceId(source.source_id)}>Use for arm</button><button className="secondary" onClick={() => beginSource(source)}>Edit</button></div></article>; })}</div>}
        {sourceEditorOpen && <form className="live-source-form" onSubmit={saveSource}><div className="panel-head"><div><p className="eyebrow">{editingSource ? `Revision ${editingSource.revision} → ${editingSource.revision + 1}` : "New source"}</p><h2>{editingSource ? "Edit source" : "Configure UDP MPEG-TS"}</h2></div><button type="button" className="text-button" onClick={() => { setEditingSource(null); setSourceEditorOpen(false); }}>Cancel</button></div><div className="form-pair"><label>Source label<input required maxLength={120} value={sourceLabel} onChange={(event) => setSourceLabel(event.target.value)} placeholder="Council chamber feed" /></label><label>Bind IP<input required value={sourceBindIp} onChange={(event) => setSourceBindIp(event.target.value)} placeholder="239.1.2.3" /><small>Must be station-local, private, loopback, or multicast.</small></label></div><div className="form-pair"><label>UDP port<input required type="number" min="1" max="65535" value={sourcePort} onChange={(event) => setSourcePort(event.target.value)} /></label><label className="live-checkbox"><span>Enabled<input type="checkbox" checked={sourceEnabled} onChange={(event) => setSourceEnabled(event.target.checked)} /></span><small>Disabled sources cannot be armed.</small></label></div><button className="primary" disabled={busy || conflict}>{conflict ? "Refresh before saving" : busy ? "Saving…" : "Save source revision"}</button></form>}
      </section>
      <section className={`panel live-session-panel ${session?.state === "on_air" ? "live-on-air-panel" : ""}`}><div className="panel-head"><div><p className="eyebrow">Takeover authority</p><h2>Live session</h2></div>{session && <StatusPill status={session.state} />}</div>
        {!session ? <form onSubmit={requestArm}><div className="live-arm-intro"><strong>Request an arm window</strong><p>The signed-in operator will be recorded by the server. The worker validates the source and reports Armed; this five-minute intent does not take the channel live.</p></div><label>Source<select required value={selectedSourceId} onChange={(event) => setSelectedSourceId(event.target.value)} disabled={!sources.length || conflict}>{sources.length ? sources.map((source) => <option key={source.source_id} value={source.source_id} disabled={!source.enabled}>{source.label}{source.enabled ? "" : " (disabled)"}</option>) : <option value="">Configure a source first</option>}</select></label><label>Reason<textarea className="live-reason" required maxLength={500} value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Scheduled council meeting live feed" /></label><div className="button-row"><span className="muted live-expiry-note">Arm window: 5 minutes after submission</span><button className="primary" disabled={busy || conflict || !selectedSource?.enabled || !authority}>{conflict ? "Refresh before arming" : busy ? "Submitting…" : "Request arm"}</button></div></form> : <>
          <div className={`live-state-banner live-state-${session.state}`} role={session.state === "on_air" ? "status" : undefined}><span className="live-state-light" aria-hidden="true" /> <div><strong>{session.state === "on_air" ? "ON AIR" : liveStateLabel(session.state)}</strong><small>{session.state === "on_air" ? `Live source: ${sourceName(session.source_id)}` : session.failure_detail ?? `Operator ${session.operator_id} · requested ${formatTime(session.requested_at_unix_ms)}`}</small></div>{session.state === "on_air" && <div className="live-duration"><span>Duration</span><strong>{formatDuration(duration)}</strong></div>}</div>
          <dl className="live-session-details"><div><dt>Source</dt><dd>{sourceName(session.source_id)}</dd></div><div><dt>Operator</dt><dd>{session.operator_id}</dd></div><div><dt>Reason</dt><dd>{session.reason ?? "—"}</dd></div><div><dt>Authority revision</dt><dd>{session.authority_revision}</dd></div><div><dt>Requested</dt><dd>{formatTime(session.requested_at_unix_ms)}</dd></div>{session.expires_at_unix_ms && <div><dt>Arm expiry</dt><dd>{formatTime(session.expires_at_unix_ms)}</dd></div>}</dl>
          {!isTerminal && <div className="live-session-actions">{session.state === "armed" && (confirmAction === "take" ? <div className="live-confirm"><strong>Take this source live?</strong><span>The schedule will be interrupted. Confirm only when the source and reason are correct.</span><div className="button-row"><button className="secondary" onClick={() => setConfirmAction(null)}>Cancel</button><button className="danger" onClick={() => void confirmTake()} disabled={busy || conflict}>{conflict ? "Refresh before taking live" : "Confirm Take Live"}</button></div></div> : <button className="danger" onClick={() => setConfirmAction("take")} disabled={busy || conflict}>Take Live</button>)}{session.state === "on_air" && (confirmAction === "return" ? <div className="live-confirm"><strong>Return to Schedule?</strong><span>This ends the live takeover and hands control back to the schedule.</span><div className="button-row"><button className="secondary" onClick={() => setConfirmAction(null)}>Cancel</button><button className="primary" onClick={() => void confirmReturn()} disabled={busy || conflict}>{conflict ? "Refresh before returning" : "Confirm Return to Schedule"}</button></div></div> : <button className="primary" onClick={() => setConfirmAction("return")} disabled={busy || conflict}>Return to Schedule</button>)}</div>}
          {isTerminal && <div className={`notice ${session.state === "failed" ? "live-failure" : ""}`}><span>{session.state === "failed" ? `Live session failed: ${session.failure_detail ?? "The worker reported a failure."}` : "Live session completed and schedule authority has been restored."}</span><button className="secondary" onClick={() => void loadChannel(selectedChannelId, true)}>Start another session</button></div>}
        </>}
      </section>
      {session && <section className="panel"><div className="panel-head"><div><p className="eyebrow">Durable record</p><h2>Live audit</h2></div><span className="count">{audit.length} events</span></div>{!audit.length ? <p className="muted">Waiting for the authoritative audit trail…</p> : <div className="live-audit">{audit.map((event) => <article key={event.audit_id}><span className="live-audit-dot" /><div><strong>{liveStateLabel(event.state)}</strong><small>{formatTime(event.occurred_at_unix_ms)} · {event.operator_id}</small>{event.detail && <p>{event.detail}</p>}</div></article>)}</div>}</section>}
    </>}
  </>;
}

function App() {
  const [station, setStation] = useState<StationDocument | null | undefined>(undefined);
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [outputProfiles, setOutputProfiles] = useState<OutputProfile[]>([]);
  const [runtime, setRuntime] = useState<RuntimeSafeToAirStatus | null>(null);
  const [alerts, setAlerts] = useState<AlertEvent[]>([]);
  const [alertChannels, setAlertChannels] = useState<AlertChannel[]>([]);
  const [deliveries, setDeliveries] = useState<AlertDelivery[]>([]);
  const [rules, setRules] = useState<AlertRule[]>([]);
  const [resources, setResources] = useState<SystemResourceSample | null>(null);
  const [selfTests, setSelfTests] = useState<SystemSelfTest[]>([]);
  const [selfTestSchedules, setSelfTestSchedules] = useState<SelfTestSchedule[]>([]);
  const [updateStatus, setUpdateStatus] = useState<UpdateStatus | null>(null);
  const [view, setView] = useState<View>(initialView);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    try {
      const [nextDashboard, nextChannels, nextOutputProfiles, nextRuntime, nextResources, nextSelfTests, nextSelfTestSchedules, nextUpdateStatus, nextAlerts, nextAlertChannels, nextDeliveries, nextRules] = await Promise.all([request<Dashboard>("/api/v1/assets/readiness-dashboard"), request<Channel[]>("/api/v1/channels"), request<OutputProfile[]>("/api/v1/output-profiles"), request<RuntimeSafeToAirStatus>("/api/v1/runtime-safe-to-air"), request<SystemResourceSample | null>("/api/v1/system-resources/latest"), request<SystemSelfTest[]>("/api/v1/self-tests"), request<SelfTestSchedule[]>('/api/v1/self-test-schedules'), request<UpdateStatus>("/api/v1/updates/status").catch(() => null), request<AlertEvent[]>("/api/v1/alerts"), request<AlertChannel[]>("/api/v1/alert-channels"), request<AlertDelivery[]>("/api/v1/alert-deliveries"), request<AlertRule[]>("/api/v1/alert-rules")]);
      setDashboard(nextDashboard); setChannels(nextChannels); setOutputProfiles(nextOutputProfiles); setRuntime(nextRuntime); setResources(nextResources); setSelfTests(nextSelfTests); setSelfTestSchedules(nextSelfTestSchedules); setUpdateStatus(nextUpdateStatus); setAlerts(nextAlerts); setAlertChannels(nextAlertChannels); setDeliveries(nextDeliveries); setRules(nextRules); setError(null);
    } catch (problem) { setError(problem instanceof Error ? problem.message : "Station state could not be refreshed."); }
  }, []);
  useEffect(() => { void request<StationDocument>("/api/v1/station").then((value) => { setStation(value); void refresh(); }).catch((problem) => { if (problem instanceof ApiError && problem.status === 404) setStation(null); else { setStation(null); setError(problem instanceof Error ? problem.message : "Station profile could not be read."); } }); }, [refresh]);
  useEffect(() => { if (!station) return; const timer = window.setInterval(() => void refresh(), 5_000); return () => window.clearInterval(timer); }, [refresh, station]);
  const nav = useMemo(() => ([{ id: "overview" as CoreView, label: "Overview" }, { id: "media" as CoreView, label: "Media" }, { id: "channels" as CoreView, label: "Channels" }, { id: "schedule" as CoreView, label: "Schedule" }, { id: "live" as CoreView, label: "Live" }, { id: "automation" as CoreView, label: "Automation" }, { id: "health" as CoreView, label: "Health" }, { id: "commissioning" as CoreView, label: "Commission" }]), []);
  const cgNav = useMemo(() => CG_ROUTES.map((item) => ({ id: item.id as View, label: item.shortLabel })), []);
  const navigate = useCallback((next: View) => {
    setView(next);
    const url = new URL(window.location.href);
    url.searchParams.set("view", next);
    window.history.replaceState({}, "", url);
  }, []);
  if (station === undefined) return <div className="boot"><div className="mark">TL</div><div className="boot-line"><i /></div><p>Connecting to TownLight Station…</p></div>;
  if (!station) return <SetupWizard onComplete={(value) => { setStation(value); navigate("commissioning"); void refresh(); }} />;
  return <div className="app-shell"><aside className="sidebar"><div className="brand-lockup"><div className="mark small">TL</div><div><strong>TownLight</strong><span>Station</span></div></div><nav>{nav.map((item) => <button key={item.id} className={view === item.id ? "selected" : ""} onClick={() => navigate(item.id)}><Icon name={item.id} /><span>{item.label}</span>{item.id === "media" && Boolean(dashboard?.failed_ingest_count) && <b>{dashboard?.failed_ingest_count}</b>}{item.id === "health" && Boolean(runtime?.active_critical_alerts || runtime?.active_warning_alerts) && <b>{(runtime?.active_critical_alerts ?? 0) + (runtime?.active_warning_alerts ?? 0)}</b>}</button>)}<p className="nav-section-label">CG authoring</p>{cgNav.map((item) => <button key={item.id} className={`cg-nav-item ${view === item.id ? "selected" : ""}`} onClick={() => navigate(item.id)}><span className="cg-nav-mark">CG</span><span>{item.label}</span></button>)}</nav><div className="station-card"><span className={`live-dot runtime-${runtime?.color ?? "yellow"}`} /><div><strong>{station.display_name}</strong><small>{runtime?.label ?? station.timezone}</small></div></div></aside><main className="workspace">{error && <div className="global-error" role="alert">{error}<button onClick={() => void refresh()}>Retry</button></div>}{view === "overview" && <Overview station={station} dashboard={dashboard} channels={channels} runtime={runtime} onNavigate={navigate} />}{view === "media" && <Media dashboard={dashboard} onRefresh={refresh} />}{view === "channels" && <Channels channels={channels} outputProfiles={outputProfiles} onRefresh={refresh} />}{view === "schedule" && <Schedule dashboard={dashboard} channels={channels} />}{view === "live" && <Live channels={channels} />}{view === "automation" && <AutomationPanel />}{view === "health" && <Health runtime={runtime} resources={resources} selfTests={selfTests} selfTestSchedules={selfTestSchedules} updateStatus={updateStatus} alerts={alerts} alertChannels={alertChannels} deliveries={deliveries} rules={rules} outputChannels={channels} onRefresh={refresh} />}{view === "commissioning" && <Commissioning onNavigate={navigate} />}{view.startsWith("cg-") && <CgOperator channelId={channels[0]?.channel_id} channels={channels.map((channel) => ({ channel_id: channel.channel_id, display_name: channel.display_name }))} route={view as CgRoute} onNavigate={(route) => navigate(route)} />}</main><nav className="mobile-nav">{[...nav, { id: "cg-moderation" as View, label: "CG" }].map((item) => <button key={item.id} aria-label={item.label} className={view === item.id || (item.id === "cg-moderation" && view.startsWith("cg-")) ? "selected" : ""} onClick={() => navigate(item.id)}>{"id" in item && item.id.startsWith("cg-") ? <span className="cg-nav-mark">CG</span> : <Icon name={item.id as CoreView} />}<span>{item.label}</span></button>)}</nav></div>;
}

createRoot(document.getElementById("root")!).render(<React.StrictMode><AuthBoundary><App /></AuthBoundary></React.StrictMode>);
