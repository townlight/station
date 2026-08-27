import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, apiRequest } from "./api-client";
import { asRunExportPath, asRunPagePath, mergeAsRunPages, parseAsRunProjectionPage, reportingChannelRenderState, type AsRunProjectedEntry, type AsRunProjectionPage } from "./reporting-contract";

interface ReportingChannel { channel_id: string; display_name: string; }

function formatTime(unixMs: number, timezone?: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium", timeZone: timezone }).format(unixMs);
}

function entryLabel(entry: AsRunProjectedEntry): string {
  return entry.title ?? entry.content_id ?? entry.state.replaceAll("_", " ");
}

export function ReportingPanel({ channels }: { channels: ReportingChannel[] }) {
  const [selectedChannelId, setSelectedChannelId] = useState(channels[0]?.channel_id ?? "");
  const [page, setPage] = useState<AsRunProjectionPage | null>(null);
  const [busy, setBusy] = useState<"load" | "more" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [stateChannelId, setStateChannelId] = useState(selectedChannelId);
  const requestGeneration = useRef(0);
  const renderedChannelId = channels.some((channel) => channel.channel_id === selectedChannelId) ? selectedChannelId : channels[0]?.channel_id ?? "";
  const activeChannelId = useRef(renderedChannelId);
  if (activeChannelId.current !== renderedChannelId) {
    activeChannelId.current = renderedChannelId;
    requestGeneration.current += 1;
  }

  useEffect(() => {
    if (!channels.some((channel) => channel.channel_id === selectedChannelId)) setSelectedChannelId(channels[0]?.channel_id ?? "");
  }, [channels, selectedChannelId]);

  const load = useCallback(async (channelId: string) => {
    const generation = ++requestGeneration.current;
    setStateChannelId(channelId); setPage(null); setError(null);
    if (!channelId) { setBusy(null); return; }
    const path = asRunPagePath(channelId, 100);
    if (!path) { setError("The selected channel identity is invalid."); return; }
    setBusy("load");
    try {
      const value = await apiRequest<unknown>(path);
      const parsed = parseAsRunProjectionPage(value);
      if (generation !== requestGeneration.current || activeChannelId.current !== channelId || parsed.channel_id !== channelId) return;
      setPage(parsed);
    } catch (problem) {
      if (generation !== requestGeneration.current || activeChannelId.current !== channelId) return;
      if (problem instanceof ApiError && problem.status === 404) setError("No authoritative projection yet for this channel.");
      else setError(problem instanceof Error ? problem.message : "The authoritative as-run projection could not be read.");
    } finally { if (generation === requestGeneration.current) setBusy(null); }
  }, []);

  useEffect(() => { void load(renderedChannelId); }, [load, renderedChannelId]);

  async function loadMore() {
    if (!displayed.page?.next_cursor || displayed.busy) return;
    const generation = requestGeneration.current;
    const requestedChannelId = renderedChannelId;
    const path = asRunPagePath(requestedChannelId, 100, displayed.page.next_cursor);
    if (!path) { setError("The continuation supplied by the station is invalid."); return; }
    setBusy("more"); setError(null);
    try {
      const next = parseAsRunProjectionPage(await apiRequest<unknown>(path));
      if (generation !== requestGeneration.current || activeChannelId.current !== requestedChannelId) return;
      setPage((current) => current ? mergeAsRunPages(current, next) : null);
    } catch (problem) {
      if (generation === requestGeneration.current) setError(problem instanceof Error ? problem.message : "The next authoritative page could not be read.");
    } finally { if (generation === requestGeneration.current) setBusy(null); }
  }

  const displayed = reportingChannelRenderState(renderedChannelId, { state_channel_id: stateChannelId, page, busy, error });
  const selectedChannel = channels.find((channel) => channel.channel_id === renderedChannelId);
  const csvPath = asRunExportPath(renderedChannelId, "csv");
  const xmltvPath = asRunExportPath(renderedChannelId, "xmltv");

  return <>
    <header className="page-header"><div><p className="eyebrow">Authoritative journal projection</p><h1>As-run and EPG reports</h1><p>Read the station’s bounded, journal-derived UTC timeline. This evidence records the station projection; it does not prove receiver delivery or audience viewing.</p></div><div className="header-actions"><button className="secondary" onClick={() => void load(renderedChannelId)} disabled={displayed.busy !== null || !renderedChannelId}>Refresh projection</button></div></header>
    <section className="panel reporting-controls" aria-label="Reporting channel and exports"><label>Output channel<select value={renderedChannelId} onChange={(event) => { requestGeneration.current += 1; activeChannelId.current = event.target.value; setStateChannelId(event.target.value); setPage(null); setError(null); setBusy(null); setSelectedChannelId(event.target.value); }}><option value="">Select a channel</option>{channels.map((channel) => <option key={channel.channel_id} value={channel.channel_id}>{channel.display_name}</option>)}</select></label><div className="reporting-downloads">{csvPath && <a className="secondary" href={csvPath} download>Download CSV</a>}{xmltvPath && <a className="secondary" href={xmltvPath} download>Download XMLTV</a>}</div><small>Downloads are authenticated, bounded exports from the same persisted projection. Export generation still does not prove publication or receiver delivery.</small></section>
    {displayed.error && <div className="error-box" role="alert">{displayed.error}</div>}
    {displayed.busy === "load" && <div className="notice" role="status">Reading current reporting authority…</div>}
    {displayed.page && <>
      <section className="reporting-facts" aria-label="Projection authority"><div><span>Channel</span><strong>{selectedChannel?.display_name ?? displayed.page.channel_id}</strong><small>{displayed.page.channel_id}</small></div><div><span>Station-local window</span><strong>{displayed.page.projection_window?.local_date ?? "Window unavailable"}</strong><small>{displayed.page.station_timezone}</small></div><div><span>Durable source</span><strong>{displayed.page.source_raw_sequence ? `Sequence ${displayed.page.source_raw_sequence}` : "Reader cursor not established"}</strong><small>{displayed.page.source_checkpoint_revision ? `checkpoint revision ${displayed.page.source_checkpoint_revision} · ` : ""}materialized {displayed.page.checkpoint_sequence}</small></div></section>
      <section className="panel reporting-table-panel"><div className="panel-head"><div><p className="eyebrow">Projected intervals</p><h2>{displayed.page.entries.length} retained entries</h2></div><span className="pill pill-active">UTC-backed authority</span></div>{displayed.page.entries.length ? <div className="reporting-table-wrap" tabIndex={0} role="region" aria-label="Scrollable as-run intervals"><table className="reporting-table"><caption>As-run intervals shown in station-local time ({displayed.page.station_timezone})</caption><thead><tr><th scope="col">Start (station local)</th><th scope="col">End (station local)</th><th scope="col">State</th><th scope="col">Program / evidence</th><th scope="col">Journal</th></tr></thead><tbody>{displayed.page.entries.map((entry) => <tr key={entry.event_id}><td>{formatTime(entry.started_at_unix_ms, displayed.page?.station_timezone)}</td><td>{formatTime(entry.ended_at_unix_ms, displayed.page?.station_timezone)}</td><td><span className={`pill pill-${entry.state === "program" ? "good" : "active"}`}>{entry.state}</span></td><td><strong>{entryLabel(entry)}</strong>{entry.category && <small>{entry.category}{entry.franchise_id ? ` · ${entry.franchise_id}` : ""}</small>}{entry.unavailable_reason && <small className="reporting-unavailable">{entry.unavailable_reason}</small>}</td><td><code>#{entry.journal_sequence}</code><small>{entry.journal_hash.slice(0, 12)}…</small></td></tr>)}</tbody></table></div> : <div className="recording-empty"><strong>No projected intervals</strong><p>The authority exists, but this bounded page contains no intervals.</p></div>} {displayed.page.next_cursor && <button className="secondary reporting-more" onClick={() => void loadMore()} disabled={displayed.busy !== null}>{displayed.busy === "more" ? "Reading next page…" : "Load more authoritative entries"}</button>}</section>
      <section className="reporting-boundary"><strong>Evidence boundary</strong><span>Source journal <code>{displayed.page.source_journal_id}</code>{displayed.page.source_file_identity ? <> and file identity <code>{displayed.page.source_file_identity}</code></> : <>; durable reader file identity is not established for this projection</>} bind the displayed authority. They do not attest headend receipt, transport delivery, downstream playback, or audience measurement.</span></section>
    </>}
  </>;
}
