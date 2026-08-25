import { ChangeEvent, useCallback, useEffect, useMemo, useState } from "react";
import { apiRequest } from "./api-client";
import {
  CaptionCueDraft,
  CaptionTrackSourceKind,
  captionTrackMutationReason,
  parseCaptionFile,
} from "./caption-track-contract";

interface AssetCaptionTrackSummary {
  track_id: string;
  asset_id: string;
  language: string;
  service_number: number;
  source_kind: CaptionTrackSourceKind;
  label: string;
  cue_count: number;
  cues_sha256: string;
  revision: number;
  created_at_unix_ms: number;
  updated_at_unix_ms: number;
  updated_by: string;
}

interface AssetCaptionTrack extends Omit<AssetCaptionTrackSummary, "cue_count"> {
  cues: CaptionCueDraft[];
}

interface AssetCaptionTrackAuditEvent {
  audit_id: string;
  revision: number;
  actor: string;
  occurred_at_unix_ms: number;
  cues_sha256: string;
  cues: CaptionCueDraft[];
}

interface AssetCaptionTrackAuthority {
  track: AssetCaptionTrack;
  audit: AssetCaptionTrackAuditEvent[];
}

interface AssetCaptionTrackList {
  asset_id: string;
  tracks: AssetCaptionTrackSummary[];
}

function timestamp(value: number): string {
  const hours = Math.floor(value / 3_600_000);
  const minutes = Math.floor((value % 3_600_000) / 60_000);
  const seconds = Math.floor((value % 60_000) / 1_000);
  const milliseconds = value % 1_000;
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(milliseconds).padStart(3, "0")}`;
}

function cuesAsWebVtt(cues: CaptionCueDraft[]): string {
  return `WEBVTT\n\n${cues.map((cue) => `${timestamp(cue.start_ms)} --> ${timestamp(cue.start_ms + cue.duration_ms)}\n${cue.text}`).join("\n\n")}\n`;
}

export function CaptionTrackEditor({
  assetId,
  assetName,
  assetDurationMs,
  assetReady,
  onClose,
}: {
  assetId: string;
  assetName: string;
  assetDurationMs: number;
  assetReady: boolean;
  onClose: () => void;
}) {
  const [tracks, setTracks] = useState<AssetCaptionTrackSummary[] | null>(null);
  const [authority, setAuthority] = useState<AssetCaptionTrackAuthority | null>(null);
  const [trackId, setTrackId] = useState<string | null>(null);
  const [language, setLanguage] = useState("en-US");
  const [serviceNumber, setServiceNumber] = useState(1);
  const [sourceKind, setSourceKind] = useState<CaptionTrackSourceKind>("sidecar");
  const [label, setLabel] = useState("Reviewed English");
  const [captionText, setCaptionText] = useState("WEBVTT\n\n");
  const [cues, setCues] = useState<CaptionCueDraft[]>([]);
  const [cueTextDirty, setCueTextDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loadingTrack, setLoadingTrack] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const listPath = `/api/v1/assets/${encodeURIComponent(assetId)}/caption-tracks`;
  const loadList = useCallback(async () => {
    const next = await apiRequest<AssetCaptionTrackList>(listPath);
    setTracks(next.tracks);
  }, [listPath]);

  useEffect(() => {
    setTracks(null);
    setAuthority(null);
    setError(null);
    setMessage(null);
    void loadList().catch((problem) => {
      setError(problem instanceof Error ? problem.message : "Caption tracks could not be read.");
    });
  }, [loadList]);

  function startNew() {
    setTrackId(null);
    setAuthority(null);
    setLanguage("en-US");
    setServiceNumber(1);
    setSourceKind("sidecar");
    setLabel("Reviewed English");
    setCaptionText("WEBVTT\n\n");
    setCues([]);
    setCueTextDirty(false);
    setError(null);
    setMessage(null);
  }

  async function openTrack(selectedTrackId: string) {
    setLoadingTrack(true);
    setError(null);
    setMessage(null);
    try {
      const next = await apiRequest<AssetCaptionTrackAuthority>(`${listPath}/${encodeURIComponent(selectedTrackId)}`);
      setAuthority(next);
      setTrackId(next.track.track_id);
      setLanguage(next.track.language);
      setServiceNumber(next.track.service_number);
      setSourceKind(next.track.source_kind);
      setLabel(next.track.label);
      setCues(next.track.cues);
      setCaptionText(cuesAsWebVtt(next.track.cues));
      setCueTextDirty(false);
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "The caption track could not be read.");
    } finally {
      setLoadingTrack(false);
    }
  }

  async function importFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setError(null);
    setMessage(null);
    try {
      if (file.size > 8 * 1024 * 1024) throw new Error("Caption source files may not exceed 8 MiB.");
      const source = await file.text();
      const imported = parseCaptionFile(source);
      const reason = captionTrackMutationReason(assetReady, language, serviceNumber, label, imported, assetDurationMs);
      if (reason) throw new Error(reason);
      setCues(imported);
      setCaptionText(cuesAsWebVtt(imported));
      setCueTextDirty(false);
      setSourceKind("sidecar");
      setMessage(`${file.name} loaded with ${imported.length.toLocaleString()} timed cue${imported.length === 1 ? "" : "s"}. Review, then save.`);
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "The caption file could not be imported.");
    }
  }

  const parsed = useMemo(() => {
    if (!cueTextDirty) return { cues, error: null as string | null };
    try {
      return { cues: parseCaptionFile(captionText), error: null as string | null };
    } catch (problem) {
      return { cues: [] as CaptionCueDraft[], error: problem instanceof Error ? problem.message : "Caption timing is invalid." };
    }
  }, [captionText, cueTextDirty, cues]);
  const blockedReason = parsed.error ?? captionTrackMutationReason(
    assetReady,
    language,
    serviceNumber,
    label,
    parsed.cues,
    assetDurationMs,
  );

  async function save() {
    if (blockedReason) return;
    setBusy(true);
    setError(null);
    setMessage(null);
    const savingTrackId = trackId ?? crypto.randomUUID();
    try {
      await apiRequest<AssetCaptionTrack>(`${listPath}/${encodeURIComponent(savingTrackId)}`, {
        method: "PUT",
        body: JSON.stringify({
          language,
          service_number: serviceNumber,
          source_kind: sourceKind,
          label,
          cues: parsed.cues,
          expected_revision: authority?.track.revision ?? 0,
        }),
      });
      await loadList();
      await openTrack(savingTrackId);
      setMessage("Caption track saved with a new immutable server audit revision.");
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "The caption track could not be saved.");
    } finally {
      setBusy(false);
    }
  }

  return <section className="panel caption-track-editor" aria-label={`Caption tracks for ${assetName}`}>
    <div className="panel-head"><div><p className="eyebrow">Asset caption authority</p><h2>{assetName}</h2></div><button className="text-button" onClick={onClose}>Close</button></div>
    <p className="muted">Import and review WebVTT or SRT timing bound to this exact media hash. Stored cues do not prove that captions aired or decoded from an output.</p>
    <div className="caption-track-layout">
      <aside className="caption-track-list">
        <div className="caption-track-list-head"><strong>Tracks</strong><button className="secondary" onClick={startNew}>New track</button></div>
        {tracks === null ? <div className="caption-track-loading" role="status">Loading caption tracks…</div> : tracks.length === 0 ? <div className="caption-track-empty"><strong>No caption tracks</strong><p>Import a reviewed WebVTT or SRT file to create the first authority.</p></div> : tracks.map((track) => <button key={track.track_id} className={track.track_id === trackId ? "selected" : ""} onClick={() => void openTrack(track.track_id)} disabled={loadingTrack}><span><strong>{track.label}</strong><small>{track.language} · service {track.service_number} · {track.cue_count.toLocaleString()} cues</small></span><b>r{track.revision}</b></button>)}
      </aside>
      <div className="caption-track-workspace">
        <div className="caption-track-fields">
          <label>Track label<input required maxLength={120} value={label} onChange={(event) => { setLabel(event.target.value); setMessage(null); }} disabled={busy} /></label>
          <label>Language<input required minLength={2} maxLength={35} value={language} onChange={(event) => { setLanguage(event.target.value); setMessage(null); }} disabled={busy || Boolean(authority)} /></label>
          <label>CEA-708 service<input type="number" min="1" max="63" step="1" value={serviceNumber} onChange={(event) => { setServiceNumber(Number(event.target.value)); setMessage(null); }} disabled={busy || Boolean(authority)} /></label>
          <label>Source provenance<select value={sourceKind} onChange={(event) => setSourceKind(event.target.value as CaptionTrackSourceKind)} disabled={busy}><option value="sidecar">Sidecar file</option><option value="embedded">Embedded source</option><option value="generated">Generated transcript</option><option value="external">External provider</option></select></label>
        </div>
        <div className="caption-track-import"><label className="secondary">Import WebVTT or SRT<input className="visually-hidden" type="file" accept=".vtt,.srt,text/vtt,application/x-subrip,text/plain" onChange={(event) => void importFile(event)} disabled={busy || !assetReady} /></label><span>{parsed.cues.length.toLocaleString()} timed cue{parsed.cues.length === 1 ? "" : "s"} · media {Math.round(assetDurationMs / 1_000).toLocaleString()} seconds</span></div>
        <label>Reviewed cue source<textarea className="caption-track-source" spellCheck="false" value={captionText} onChange={(event) => { setCaptionText(event.target.value); setCueTextDirty(true); setMessage(null); }} disabled={busy} aria-describedby="caption-track-format" /></label>
        <small id="caption-track-format" className="caption-track-help">Use WebVTT timing: 00:00:01.000 --&gt; 00:00:03.000 followed by one or more caption text lines.</small>
        {authority && <dl className="caption-track-authority"><div><dt>Exact asset</dt><dd><code>{assetId}</code></dd></div><div><dt>Current revision</dt><dd>{authority.track.revision} · {authority.track.updated_by}</dd></div><div><dt>Cue digest</dt><dd><code>{authority.track.cues_sha256}</code></dd></div><div><dt>Immutable revisions</dt><dd>{authority.audit.length}</dd></div></dl>}
        {blockedReason && <div className="headend-warning" role="status">{blockedReason}</div>}
        {error && <div className="error-box" role="alert">{error}</div>}
        {message && <div className="notice" role="status">{message}</div>}
        <small className="caption-track-help">Saving caption authority requires the Setup Admin role.</small>
        <button className="primary wide" disabled={busy || Boolean(blockedReason)} onClick={() => void save()}>{busy ? "Saving…" : authority ? "Save new revision" : "Create caption track"}</button>
      </div>
    </div>
  </section>;
}
