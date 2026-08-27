import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, apiRequest } from "./api-client";
import {
  AGENDA_IMPORT_MAX_UTF8_BYTES,
  AGENDA_TITLE_MAX_UTF8_BYTES,
  agendaTextByteError,
  isCurrentAgendaRequest,
  utf8ByteLength,
} from "./agenda-contract";

type AgendaItem = { item_id: string; ordinal: number; title: string; public_summary: string | null; staff_notes: string | null; visibility: "public" | "staff_only" };
type Agenda = { agenda_id: string; channel_id: string; meeting_id: string; title: string; meeting_starts_at_unix_ms: number; provenance: unknown; sync_intent: { channel_id: string; schedule_item_id: string; schedule_revision: number; media_asset_sha256: string; expected_media_duration_ms: number; requested_by: unknown; requested_at_unix_ms: number }; items: AgendaItem[]; chapters: unknown[]; revision: number };
type History = { agenda: Agenda; changed_by: string; occurred_at_unix_ms: number };

export function AgendaPanel({ channelId }: { channelId: string }) {
  const [agendaId, setAgendaId] = useState("");
  const [agenda, setAgenda] = useState<Agenda | null>(null);
  const [history, setHistory] = useState<History[]>([]);
  const [title, setTitle] = useState("Town council meeting");
  const [importText, setImportText] = useState("");
  const [meetingId, setMeetingId] = useState("");
  const [scheduleItemId, setScheduleItemId] = useState("");
  const [scheduleRevision, setScheduleRevision] = useState(1);
  const [mediaSha256, setMediaSha256] = useState("");
  const [mediaDurationMs, setMediaDurationMs] = useState(60000);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const requestGeneration = useRef(0);
  const mutationGeneration = useRef(0);
  const titleByteError = agendaTextByteError(title, AGENDA_TITLE_MAX_UTF8_BYTES, "Meeting title");
  const importByteError = agendaTextByteError(importText, AGENDA_IMPORT_MAX_UTF8_BYTES, "Import text");

  const load = useCallback(async (id: string) => {
    if (!id) return;
    const generation = ++requestGeneration.current;
    const token = { generation, agendaId: id, channelId };
    setLoading(true); setError(null); setNotice(null);
    try {
      const next = await apiRequest<Agenda>(`/api/v1/agendas/${id}`);
      if (generation !== requestGeneration.current) return;
      if (!isCurrentAgendaRequest(token, requestGeneration.current, channelId, id, next.channel_id)) throw new Error("This agenda belongs to a different channel.");
      setAgenda(next); setTitle(next.title); setMeetingId(next.meeting_id); setScheduleItemId(next.sync_intent.schedule_item_id); setScheduleRevision(next.sync_intent.schedule_revision); setMediaSha256(next.sync_intent.media_asset_sha256); setMediaDurationMs(next.sync_intent.expected_media_duration_ms);
      const nextHistory = await apiRequest<History[]>(`/api/v1/agendas/${id}/history?limit=20`);
      if (generation !== requestGeneration.current) return;
      setHistory(nextHistory);
    } catch (problem) {
      if (generation !== requestGeneration.current) return;
      setAgenda(null); setHistory([]); setError(problem instanceof ApiError && problem.status === 404 ? "No agenda exists for that identity yet." : problem instanceof Error ? problem.message : "The agenda could not be loaded.");
    } finally { if (generation === requestGeneration.current) setLoading(false); }
  }, [channelId]);

  useEffect(() => {
    requestGeneration.current += 1; mutationGeneration.current += 1;
    setAgenda(null); setHistory([]); setError(null); setNotice(null); setLoading(false); setBusy(false);
    if (agendaId) void load(agendaId);
  }, [channelId]);

  async function save() {
    if (agenda && agenda.channel_id !== channelId) { setError("The selected agenda belongs to a different channel; reload the current channel before saving."); return; }
    if (titleByteError) { setError(titleByteError); return; }
    const generation = requestGeneration.current;
    const mutation = ++mutationGeneration.current;
    if (!agenda && (!meetingId.trim() || !scheduleItemId.trim() || !/^[0-9a-f]{64}$/.test(mediaSha256) || scheduleRevision < 1 || mediaDurationMs < 1)) { setError("Creation requires an exact meeting, committed schedule revision, media SHA-256, and duration binding."); return; }
    setBusy(true); setError(null); setNotice(null);
    const id = agenda?.agenda_id ?? (agendaId.trim() || crypto.randomUUID());
    const current = agenda ?? { agenda_id: id, channel_id: channelId, meeting_id: meetingId.trim(), title, meeting_starts_at_unix_ms: Date.now(), provenance: { kind: "staff_authored", author: { actor_id: crypto.randomUUID() }, asserted_at_unix_ms: 0 }, sync_intent: { channel_id: channelId, schedule_item_id: scheduleItemId.trim(), schedule_revision: scheduleRevision, media_asset_sha256: mediaSha256, expected_media_duration_ms: mediaDurationMs, requested_by: { actor_id: crypto.randomUUID() }, requested_at_unix_ms: 0 }, items: [{ item_id: crypto.randomUUID(), ordinal: 1, title: "Call to order", public_summary: "Opening business", staff_notes: null, visibility: "public" as const }], chapters: [], revision: 1 };
    try {
      const next = await apiRequest<Agenda>(`/api/v1/agendas/${id}`, { method: "PUT", body: JSON.stringify({ expected_revision: agenda?.revision ?? null, agenda: { ...current, title: title.trim() } }) });
      if (generation !== requestGeneration.current) return;
      const nextHistory = await apiRequest<History[]>(`/api/v1/agendas/${id}/history?limit=20`);
      if (generation !== requestGeneration.current) return;
      setAgendaId(id); setAgenda(next); setHistory(nextHistory); setNotice("Agenda saved. Server attribution and revision are authoritative.");
    } catch (problem) { if (generation === requestGeneration.current) setError(problem instanceof ApiError && problem.status === 409 ? "This agenda changed elsewhere. Reload before saving." : problem instanceof Error ? problem.message : "The agenda could not be saved."); }
    finally { if (mutation === mutationGeneration.current) setBusy(false); }
  }

  async function importPlainText() {
    if (agenda && agenda.channel_id !== channelId) { setError("The selected agenda belongs to a different channel; reload the current channel before importing."); return; }
    if (agenda) { setError("Existing agenda source cannot be rebound; text import is creation-only."); return; }
    if (titleByteError || importByteError) { setError(titleByteError ?? importByteError); return; }
    const generation = requestGeneration.current;
    const mutation = ++mutationGeneration.current;
    if (!agenda && (!meetingId.trim() || !scheduleItemId.trim() || !/^[0-9a-f]{64}$/.test(mediaSha256) || scheduleRevision < 1 || mediaDurationMs < 1)) { setError("Creation requires an exact meeting, committed schedule revision, media SHA-256, and duration binding."); return; }
    setBusy(true); setError(null); setNotice(null);
    const id = agendaId.trim() || crypto.randomUUID();
    try {
      const next = await apiRequest<Agenda>(`/api/v1/agendas/${id}/import-text`, { method: "POST", body: JSON.stringify({ source_text: importText, channel_id: channelId, meeting_id: meetingId.trim(), schedule_item_id: scheduleItemId.trim(), schedule_revision: scheduleRevision, media_asset_sha256: mediaSha256, expected_media_duration_ms: mediaDurationMs, meeting_starts_at_unix_ms: Date.now(), title: title.trim(), expected_revision: null }) });
      if (generation !== requestGeneration.current) return;
      const nextHistory = await apiRequest<History[]>(`/api/v1/agendas/${id}/history?limit=20`);
      if (generation !== requestGeneration.current) return;
      setAgendaId(id); setAgenda(next); setImportText(""); setHistory(nextHistory); setNotice("Plain text imported. The server recorded the exact source hash and attribution.");
    } catch (problem) { if (generation === requestGeneration.current) setError(problem instanceof ApiError && problem.status === 409 ? "This agenda changed elsewhere. Reload before importing." : problem instanceof Error ? problem.message : "The text could not be imported."); }
    finally { if (mutation === mutationGeneration.current) setBusy(false); }
  }

  const identityChanged = (value: string) => { requestGeneration.current += 1; mutationGeneration.current += 1; setAgendaId(value); setAgenda(null); setHistory([]); setError(null); setNotice(null); setLoading(false); setBusy(false); };
  const visibleAgenda = agenda?.channel_id === channelId ? agenda : null;
  const visibleHistory = visibleAgenda ? history : [];
  return <section className="panel agenda-panel" aria-labelledby="agenda-panel-title">
    <div className="panel-head"><div><p className="eyebrow">Meeting agenda</p><h2 id="agenda-panel-title">Create, edit, and review</h2></div><span className="count">{visibleHistory.length}</span></div>
    <p className="muted">Durable current and immutable history are available to operators. Creation-only plain-text import is bounded to 64 KiB and records an exact source hash; existing source provenance cannot be rebound. PDF parsing is not implemented.</p>
    <div className="agenda-controls"><label>Agenda identity<input value={agendaId} onChange={(event) => identityChanged(event.target.value)} placeholder="Leave blank to create a new agenda" /></label><button type="button" className="secondary" disabled={loading || !agendaId} onClick={() => void load(agendaId)}>Reload agenda</button></div>
    {loading && <div role="status" className="agenda-state">Reading agenda…</div>}{error && <div role="alert" className="error-box">{error}</div>}{notice && <div role="status" className="notice">{notice}</div>}{!loading && !visibleAgenda && !error && <div className="agenda-state"><strong>No agenda selected</strong><span>Enter an identity and choose Reload, or provide a real binding to create one.</span></div>}
    <label>Meeting title<input value={title} onChange={(event) => { setTitle(event.target.value); setError(null); setNotice(null); }} maxLength={200} /><span className={titleByteError ? "field-error" : "muted"} role={titleByteError ? "alert" : undefined}>{titleByteError ?? `${utf8ByteLength(title).toLocaleString("en-US")} / ${AGENDA_TITLE_MAX_UTF8_BYTES.toLocaleString("en-US")} UTF-8 bytes`}</span></label>
    {!visibleAgenda && <fieldset><legend>Existing committed schedule/media binding</legend><p className="muted">The server accepts only a current committed schedule revision and Ready media asset whose ID exactly equals the SHA-256 value.</p><label>Meeting identity<input value={meetingId} onChange={(event) => setMeetingId(event.target.value)} maxLength={36} /></label><label>Schedule item identity<input value={scheduleItemId} onChange={(event) => setScheduleItemId(event.target.value)} maxLength={36} /></label><label>Schedule revision<input type="number" min={1} max={2147483647} value={scheduleRevision} onChange={(event) => setScheduleRevision(Number(event.target.value))} /></label><label>Ready media SHA-256<input value={mediaSha256} onChange={(event) => setMediaSha256(event.target.value)} maxLength={64} /></label><label>Media duration (ms)<input type="number" min={1} max={86400000} value={mediaDurationMs} onChange={(event) => setMediaDurationMs(Number(event.target.value))} /></label></fieldset>}
    <div className="button-row"><button type="button" className="primary" disabled={busy || !channelId || !!titleByteError} onClick={() => void save()}>{busy ? "Saving…" : visibleAgenda ? "Save revision" : "Create agenda"}</button>{visibleAgenda && <span className="muted">Revision {visibleAgenda.revision} · actor and time are server-owned</span>}</div>
    <label>Bounded plain-text import<textarea value={importText} onChange={(event) => { setImportText(event.target.value); setError(null); setNotice(null); }} maxLength={65536} rows={6} placeholder="One public agenda item per line" /><span className={importByteError ? "field-error" : "muted"} role={importByteError ? "alert" : undefined}>{importByteError ?? `${utf8ByteLength(importText).toLocaleString("en-US")} / ${AGENDA_IMPORT_MAX_UTF8_BYTES.toLocaleString("en-US")} UTF-8 bytes`}</span></label><div className="button-row"><button type="button" className="secondary" disabled={busy || !!visibleAgenda || !importText.trim() || !!titleByteError || !!importByteError} onClick={() => void importPlainText()}>{busy ? "Importing…" : "Import reviewed text"}</button><span className="muted">UTF-8 text only; creates a new agenda; PDF files are rejected.</span></div>
    {visibleHistory.length > 0 && <details className="agenda-history"><summary>History ({visibleHistory.length})</summary>{visibleHistory.map((entry) => <div key={`${entry.agenda.agenda_id}-${entry.agenda.revision}`}><strong>Revision {entry.agenda.revision}</strong><small>{entry.agenda.title} · {entry.changed_by}</small></div>)}</details>}
  </section>;
}
