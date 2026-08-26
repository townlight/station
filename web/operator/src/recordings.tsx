import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, apiRequest } from "./api-client";
import {
  canArmRecording, canCancelRecording, cancelScheduledRecordingBody, createScheduledRecordingBody, recordingExpectedRevisionBody,
  isFutureCommittedScheduleItem, recordingHistoryLimit, recordingLifecycleLabel, scheduleCandidateRefreshDelay, scheduledRecordingCandidatesPath,
  type ScheduleItem, type ScheduledRecording, type ScheduledRecordingHistoryEntry,
} from "./recordings-contract";

interface RecordingChannel { channel_id: string; display_name: string; }

function formatRecordingTime(unixMs: number): string { return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(unixMs); }
function formatRecordingDuration(milliseconds: number): string { const seconds = Math.max(0, Math.round(milliseconds / 1000)); const minutes = Math.floor(seconds / 60); return minutes ? `${minutes}m ${seconds % 60}s` : `${seconds}s`; }
function recordingError(problem: unknown): string {
  if (problem instanceof ApiError) {
    if (problem.code === "revision_conflict") return "This recording changed elsewhere. Read the current revision, review it, and retry.";
    if (problem.code === "schedule_binding_changed") return "The committed schedule changed. Refresh the schedule and create or arm a new recording intent.";
    if (problem.code === "schedule_not_committed") return "This schedule item is no longer committed. It cannot be armed or recorded.";
    if (problem.code === "recording_conflict") return "A recording with this identity or window already exists. Refresh the channel list.";
    if (problem.code === "transition_rejected") return `The recording transition was rejected: ${problem.message}`;
    return problem.message;
  }
  return problem instanceof Error ? problem.message : "Scheduled recording authority could not be updated.";
}

export function ScheduledRecordingsPanel({ channels }: { channels: RecordingChannel[] }) {
  const [selectedChannelId, setSelectedChannelId] = useState(channels[0]?.channel_id ?? "");
  const [schedule, setSchedule] = useState<ScheduleItem[]>([]);
  const [recordings, setRecordings] = useState<ScheduledRecording[]>([]);
  const [selected, setSelected] = useState<ScheduledRecording | null>(null);
  const [history, setHistory] = useState<ScheduledRecordingHistoryEntry[]>([]);
  const [scheduleItemId, setScheduleItemId] = useState("");
  const [cancelReason, setCancelReason] = useState("");
  const [showCancel, setShowCancel] = useState(false);
  const [canOperate, setCanOperate] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [conflictLock, setConflictLock] = useState(false);
  const preserveDetailError = useRef(false);
  const channelRequest = useRef(0);
  const detailRequest = useRef(0);
  const nowUnixMs = Date.now();

  function clearChannelState() {
    channelRequest.current += 1;
    detailRequest.current += 1;
    setSchedule([]); setRecordings([]); setSelected(null); setHistory([]); setScheduleItemId("");
    setCancelReason(""); setShowCancel(false); setConflictLock(false); setError(null); setNotice(null);
  }

  useEffect(() => { if (!channels.some((channel) => channel.channel_id === selectedChannelId)) { clearChannelState(); setSelectedChannelId(channels[0]?.channel_id ?? ""); } }, [channels, selectedChannelId]);

  const loadChannel = useCallback(async (channelId: string, preserveError = false) => {
    const requestId = ++channelRequest.current;
    detailRequest.current += 1;
    setSchedule([]); setRecordings([]); setSelected(null); setHistory([]); setScheduleItemId(""); setShowCancel(false);
    if (!channelId) { setBusy(null); return; }
    setBusy("load"); if (!preserveError) setError(null);
    try {
      const [nextSchedule, nextRecordings, session] = await Promise.all([
        apiRequest<ScheduleItem[]>(scheduledRecordingCandidatesPath(channelId, 128)),
        apiRequest<ScheduledRecording[]>(`/api/v1/scheduled-recordings?channel_id=${encodeURIComponent(channelId)}&limit=128`),
        apiRequest<{ session: { roles: string[] } }>("/api/v1/auth/session"),
      ]);
      if (requestId !== channelRequest.current) return;
      const channelSchedule = nextSchedule.filter((item) => item.channel_id === channelId);
      const channelRecordings = nextRecordings.filter((item) => item.definition.channel_id === channelId);
      setSchedule(channelSchedule); setRecordings(channelRecordings); setCanOperate(session.session.roles.some((role) => role === "setup_admin" || role === "meeting_operator"));
      setSelected((current) => channelRecordings.find((item) => item.definition.recording_id === current?.definition.recording_id) ?? channelRecordings[0] ?? null);
      setScheduleItemId((current) => channelSchedule.some((item) => item.item_id === current && item.state === "committed") ? current : channelSchedule.find((item) => item.state === "committed")?.item_id ?? "");
    } catch (problem) { if (requestId === channelRequest.current) { setSchedule([]); setRecordings([]); setSelected(null); setHistory([]); setScheduleItemId(""); setError(recordingError(problem)); } }
    finally { if (requestId === channelRequest.current) setBusy(null); }
  }, []);

  const readSelected = useCallback(async (recordingId: string, preserveError = false): Promise<boolean> => {
    const requestId = ++detailRequest.current;
    setHistory([]); setBusy("read"); if (!preserveError) setError(null);
    try {
      const [fresh, nextHistory] = await Promise.all([
        apiRequest<ScheduledRecording>(`/api/v1/scheduled-recordings/${encodeURIComponent(recordingId)}`),
        apiRequest<ScheduledRecordingHistoryEntry[]>(`/api/v1/scheduled-recordings/${encodeURIComponent(recordingId)}/history?limit=${recordingHistoryLimit()}`),
      ]);
      if (requestId !== detailRequest.current) return false;
      setSelected(fresh); setHistory(nextHistory); setNotice(`Read authoritative recording revision ${fresh.revision}.`); setShowCancel(false);
      return true;
    } catch (problem) { if (requestId === detailRequest.current && !preserveError) setError(recordingError(problem)); return false; }
    finally { if (requestId === detailRequest.current) setBusy(null); }
  }, []);

  useEffect(() => { void loadChannel(selectedChannelId); }, [loadChannel, selectedChannelId]);
  useEffect(() => { if (selected) { const preserve = preserveDetailError.current; preserveDetailError.current = false; void readSelected(selected.definition.recording_id, preserve); } else setHistory([]); }, [readSelected, selected?.definition.recording_id]);

  const committedSchedule = useMemo(() => schedule.filter((item) => item.channel_id === selectedChannelId && isFutureCommittedScheduleItem(item, nowUnixMs)), [nowUnixMs, schedule, selectedChannelId]);
  const selectedSchedule = schedule.find((item) => item.item_id === scheduleItemId) ?? null;
  const selectedChannel = channels.find((channel) => channel.channel_id === selectedChannelId);
  const lifecycle = selected ? recordingLifecycleLabel(selected.lifecycle) : null;
  const canArm = Boolean(selected && canArmRecording(selected, nowUnixMs));
  const canCancel = Boolean(selected && canCancelRecording(selected, nowUnixMs));

  useEffect(() => {
    const earliest = committedSchedule.reduce<ScheduleItem | null>((current, item) => !current || item.starts_at_unix_ms < current.starts_at_unix_ms ? item : current, null);
    if (!earliest) return;
    const refreshDelay = scheduleCandidateRefreshDelay(earliest, Date.now());
    if (!refreshDelay) { void loadChannel(selectedChannelId); return; }
    const timer = window.setTimeout(() => void loadChannel(selectedChannelId), refreshDelay);
    return () => window.clearTimeout(timer);
  }, [committedSchedule, loadChannel, selectedChannelId]);

  async function refreshAfterTransition(recordingId: string, message: string) {
    await loadChannel(selectedChannelId);
    const refreshed = await readSelected(recordingId);
    if (refreshed) setNotice(message);
  }

  async function acknowledgeConflict() {
    if (!selected) return;
    const refreshed = await readSelected(selected.definition.recording_id, true);
    if (refreshed) { setConflictLock(false); setError(null); setNotice("Current recording revision and history reviewed. Actions are unlocked."); }
  }

  async function create(event: FormEvent) {
    event.preventDefault();
    const candidate = schedule.find((item) => item.item_id === scheduleItemId);
    if (!canOperate || conflictLock || !candidate || !isFutureCommittedScheduleItem(candidate, Date.now()) || candidate.channel_id !== selectedChannelId) { setError("Select a strictly-future committed schedule item belonging to the selected channel."); return; }
    setBusy("create"); setError(null); setNotice(null);
    try {
      const created = await apiRequest<ScheduledRecording>("/api/v1/scheduled-recordings", { method: "POST", body: JSON.stringify(createScheduledRecordingBody(crypto.randomUUID(), candidate.item_id)) });
      await refreshAfterTransition(created.definition.recording_id, "Recording intent created from the server-authoritative committed schedule.");
    } catch (problem) { setError(recordingError(problem)); }
    finally { setBusy(null); }
  }

  async function arm() {
    if (!selected || !canOperate || conflictLock || !canArmRecording(selected, Date.now())) return;
    setBusy("arm"); setError(null); setNotice(null);
    try {
      await apiRequest<ScheduledRecording>(`/api/v1/scheduled-recordings/${encodeURIComponent(selected.definition.recording_id)}/arm`, { method: "POST", body: JSON.stringify(recordingExpectedRevisionBody(selected.revision)) });
      await refreshAfterTransition(selected.definition.recording_id, "Arm intent accepted. Capture and finalization remain trusted worker responsibilities.");
    } catch (problem) { const message = recordingError(problem); setConflictLock(problem instanceof ApiError && problem.status === 409); preserveDetailError.current = problem instanceof ApiError && problem.status === 409; await loadChannel(selectedChannelId, true); setError(message); }
    finally { setBusy(null); }
  }

  async function cancel(event: FormEvent) {
    event.preventDefault(); if (!selected || !canOperate || conflictLock || !canCancelRecording(selected, Date.now()) || !cancelReason.trim()) return;
    setBusy("cancel"); setError(null); setNotice(null);
    try {
      await apiRequest<ScheduledRecording>(`/api/v1/scheduled-recordings/${encodeURIComponent(selected.definition.recording_id)}/cancel`, { method: "POST", body: JSON.stringify(cancelScheduledRecordingBody(selected.revision, cancelReason.trim())) });
      setShowCancel(false); setCancelReason(""); await refreshAfterTransition(selected.definition.recording_id, "Cancellation recorded by the station authority.");
    } catch (problem) { const message = recordingError(problem); setConflictLock(problem instanceof ApiError && problem.status === 409); preserveDetailError.current = problem instanceof ApiError && problem.status === 409; await loadChannel(selectedChannelId, true); setError(message); }
    finally { setBusy(null); }
  }

  return <>
    <header className="page-header"><div><p className="eyebrow">Durable capture intent</p><h1>Recordings</h1><p>Create and operate schedule-bound recording intents. The server derives the channel, window, schedule revision, actor, time, and execution authority.</p></div><div className="header-actions"><button className="secondary" onClick={() => void loadChannel(selectedChannelId)} disabled={busy !== null}>Refresh authority</button></div></header>
    {error && <div className="error-box" role="alert">{error}</div>}{notice && <div className="notice" role="status">{notice}</div>}
    {!canOperate && <div className="recording-boundary" role="status"><strong>Recording access is restricted</strong><span>Setup Admin or Meeting Operator is required to create, arm, or cancel recordings.</span></div>}
    <section className="recording-layout">
      <article className="panel recording-create"><div className="panel-head"><div><p className="eyebrow">New intent</p><h2>Record a committed schedule item</h2></div></div><form onSubmit={create}><label>Output channel<select value={selectedChannelId} onChange={(event) => { clearChannelState(); setSelectedChannelId(event.target.value); }}><option value="">Select a channel</option>{channels.map((channel) => <option key={channel.channel_id} value={channel.channel_id}>{channel.display_name}</option>)}</select></label><label>Committed schedule item<select required value={scheduleItemId} onChange={(event) => setScheduleItemId(event.target.value)} disabled={!committedSchedule.length || conflictLock}><option value="">Select a committed item</option>{committedSchedule.map((item) => <option key={item.item_id} value={item.item_id}>{item.title} · {formatRecordingTime(item.starts_at_unix_ms)} · rev {item.revision}</option>)}</select><small>The API receives only the schedule item identity; all timing and channel facts are resolved from current committed authority.</small></label>{selectedSchedule && <div className="recording-schedule-fact"><strong>{selectedSchedule.title}</strong><span>{formatRecordingTime(selectedSchedule.starts_at_unix_ms)} · {formatRecordingDuration(selectedSchedule.duration_ms)} · schedule revision {selectedSchedule.revision}</span><small>Asset <code>{selectedSchedule.asset_id.slice(0, 12)}…</code></small></div>}<button className="primary wide" disabled={!canOperate || busy !== null || conflictLock || !selectedSchedule || !isFutureCommittedScheduleItem(selectedSchedule, Date.now()) || selectedSchedule.channel_id !== selectedChannelId}>{busy === "create" ? "Creating…" : "Create recording intent"}</button></form>{!committedSchedule.length && <div className="recording-empty"><strong>No committed schedule items</strong><p>Commit a future schedule item before creating a recording intent.</p></div>}</article>
      <article className="panel recording-list"><div className="panel-head"><div><p className="eyebrow">Channel authority</p><h2>{selectedChannel?.display_name ?? "Select a channel"}</h2></div><span className="count">{recordings.length}</span></div>{!recordings.length ? <div className="recording-empty"><strong>No scheduled recordings</strong><p>Recording intents for this channel will appear here.</p></div> : <div className="recording-cards">{recordings.map((item) => <button key={item.definition.recording_id} aria-pressed={selected?.definition.recording_id === item.definition.recording_id} className={selected?.definition.recording_id === item.definition.recording_id ? "selected" : ""} onClick={() => setSelected(item)}><span><strong>{recordingLifecycleLabel(item.lifecycle)}</strong><small>{formatRecordingTime(item.definition.scheduled_start_unix_ms)} · revision {item.revision}</small><code>{item.definition.recording_id.slice(0, 12)}…</code></span><b>{item.definition.schedule_revision}</b></button>)}</div>}</article>
    </section>
    {selected && <section className="panel recording-detail"><div className="panel-head"><div><p className="eyebrow">Selected recording</p><h2>{lifecycle}</h2></div><div className="button-row"><span className="pill pill-active">revision {selected.revision}</span><button type="button" className="text-button" onClick={() => void readSelected(selected.definition.recording_id)} disabled={busy !== null}>Read current</button></div></div><div className="recording-facts"><div><span>Schedule window</span><strong>{formatRecordingTime(selected.definition.scheduled_start_unix_ms)}</strong><small>through {formatRecordingTime(selected.definition.scheduled_end_unix_ms)}</small></div><div><span>Schedule binding</span><strong>Revision {selected.definition.schedule_revision}</strong><small><code>{selected.definition.schedule_item_id}</code></small></div><div><span>Requested</span><strong>{selected.definition.requested_by.role.replaceAll("_", " ")}</strong><small>{formatRecordingTime(selected.definition.requested_at_unix_ms)}</small></div></div>{conflictLock && <div className="recording-boundary"><strong>Review required after a concurrent change</strong><span>The server rejected a stale or changed authority. Read the refreshed revision and history, then acknowledge before taking another action.</span><button type="button" className="secondary" onClick={() => void acknowledgeConflict()} disabled={busy !== null}>{busy === "read" ? "Reading current authority…" : "Read and acknowledge current revision"}</button></div>}{canOperate && <div className="recording-actions">{canArm && <button className="primary" disabled={busy !== null || conflictLock} onClick={() => void arm()}>{busy === "arm" ? "Arming…" : "Arm recording"}</button>}{canCancel && (showCancel ? <form onSubmit={cancel} className="recording-cancel-form"><label>Cancellation reason<textarea required maxLength={1000} value={cancelReason} onChange={(event) => setCancelReason(event.target.value)} placeholder="Why will this recording not proceed?" /></label><div className="button-row"><button type="button" className="secondary" onClick={() => setShowCancel(false)}>Keep intent</button><button className="danger" disabled={busy !== null || conflictLock || !cancelReason.trim()}>{busy === "cancel" ? "Cancelling…" : "Confirm cancellation"}</button></div></form> : <button className="secondary" disabled={conflictLock} onClick={() => setShowCancel(true)}>Cancel recording</button>)}</div>}{!canArm && lifecycle === "draft" && <div className="recording-boundary"><strong>Arm unavailable</strong><span>The server has not exposed a runnable execution authority for this recording.</span></div>}<details className="recording-history" open><summary>Durable transition history ({history.length})</summary>{history.length ? history.map((entry) => <div key={`${entry.recording.revision}-${entry.occurred_at_unix_ms}`}><span><strong>Revision {entry.recording.revision}</strong><small>{entry.receipt?.kind ?? "created"} · {entry.actor.role.replaceAll("_", " ")}</small></span><small>{formatRecordingTime(entry.occurred_at_unix_ms)}</small></div>) : <p className="muted">No history available.</p>}</details></section>}
  </>;
}
