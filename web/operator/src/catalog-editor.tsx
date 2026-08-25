import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, apiRequest } from "./api-client";
import {
  catalogHistoryLimit,
  catalogTransitionBody,
  catalogTransitionPath,
  catalogTextFits,
  catalogWriteBody,
  formatCatalogTime,
  parseCatalogAssetHistory,
  parseCatalogAssetRecord,
  readAllCatalogAssetPages,
  type CatalogAssetHistoryEntry,
  type CatalogAssetRecord,
  type CatalogAssetTransition,
} from "./catalog-contract";

interface CatalogAssetSummary { asset_id: string; media_path: string; }
interface OperatorSession { session: { roles: string[] } }

const catalogPath = (assetSha256: string) => `/api/v1/catalog/assets/${encodeURIComponent(assetSha256)}`;
export function CatalogAssetEditor({ assets }: { assets: CatalogAssetSummary[] }) {
  const [records, setRecords] = useState<CatalogAssetRecord[]>([]);
  const [record, setRecord] = useState<CatalogAssetRecord | null>(null);
  const [history, setHistory] = useState<CatalogAssetHistoryEntry[]>([]);
  const [title, setTitle] = useState("");
  const [meetingBody, setMeetingBody] = useState("");
  const [selectedSha, setSelectedSha] = useState("");
  const [roles, setRoles] = useState<string[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const selectedShaRef = useRef("");
  const readGeneration = useRef(0);
  const loadGeneration = useRef(0);
  const loadInFlight = useRef(false);
  const busyRef = useRef<string | null>(null);
  selectedShaRef.current = selectedSha;

  const supportAdmin = roles.includes("support_admin");
  const canEdit = roles.includes("setup_admin") || roles.includes("meeting_operator");
  const selectedAsset = useMemo(() => assets.find((asset) => asset.asset_id === selectedSha), [assets, selectedSha]);
  const dirty = record === null
    ? title.length > 0 || meetingBody.length > 0
    : title !== record.title || (meetingBody || null) !== record.meeting_body;
  const dirtyRef = useRef(false);
  dirtyRef.current = dirty;
  busyRef.current = busy;

  const applyRecord = useCallback((next: CatalogAssetRecord) => {
    setRecord(next);
    setTitle(next.title);
    setMeetingBody(next.meeting_body ?? "");
    setConflict(false);
  }, []);

  const load = useCallback(async (force = false) => {
    if (loadInFlight.current || (!force && (dirtyRef.current || busyRef.current !== null))) return;
    loadInFlight.current = true;
    const generation = ++loadGeneration.current;
    setBusy("load"); setError(null);
    try {
      const [catalogRecords, session] = await Promise.all([
        readAllCatalogAssetPages((path) => apiRequest<unknown>(path)),
        apiRequest<OperatorSession>("/api/v1/auth/session"),
      ]);
      if (generation !== loadGeneration.current) return;
      setRecords(catalogRecords);
      setRoles(session.session.roles);
      setSelectedSha((current) => assets.some((item) => item.asset_id === current) ? current : assets[0]?.asset_id ?? "");
      if (!assets.length) { setRecord(null); setHistory([]); setTitle(""); setMeetingBody(""); }
    } catch (problem) {
      if (generation === loadGeneration.current) {
        setError(problem instanceof Error ? problem.message : "Catalog authority could not be read.");
      }
    } finally {
      loadInFlight.current = false;
      if (generation === loadGeneration.current) setBusy(null);
    }
  }, [assets]);

  const readCurrent = useCallback(async (assetSha256: string, announce = true) => {
    if (!assetSha256) return;
    const generation = ++readGeneration.current;
    setBusy("read"); setError(null);
    try {
      const [recordValue, historyValue] = await Promise.all([
        apiRequest<unknown>(catalogPath(assetSha256)),
        apiRequest<unknown>(`${catalogPath(assetSha256)}/history?limit=${catalogHistoryLimit()}`),
      ]);
      if (generation !== readGeneration.current || selectedShaRef.current !== assetSha256) return;
      const next = parseCatalogAssetRecord(recordValue);
      applyRecord(next);
      setHistory(parseCatalogAssetHistory(historyValue));
      if (announce) setNotice(`Read authoritative catalog revision ${next.revision}.`);
    } catch (problem) {
      if (generation === readGeneration.current && selectedShaRef.current === assetSha256) {
        setError(problem instanceof Error ? problem.message : "The catalog record could not be read.");
      }
    } finally {
      if (generation === readGeneration.current) setBusy(null);
    }
  }, [applyRecord]);

  useEffect(() => { void load(false); }, [load]);
  useEffect(() => {
    const next = records.find((item) => item.asset_sha256 === selectedSha);
    if (!next) {
      readGeneration.current += 1;
      setRecord(null);
      setHistory([]);
      setTitle("");
      setMeetingBody("");
      setConflict(false);
      return;
    }
    applyRecord(next);
    void readCurrent(next.asset_sha256, false);
  }, [applyRecord, readCurrent, records, selectedSha]);

  function acceptSavedRecord(saved: CatalogAssetRecord) {
    applyRecord(saved);
    setRecords((current) => current.some((item) => item.asset_sha256 === saved.asset_sha256)
      ? current.map((item) => item.asset_sha256 === saved.asset_sha256 ? saved : item)
      : [...current, saved].sort((left, right) => left.asset_sha256.localeCompare(right.asset_sha256)));
  }

  function discardEdits() {
    setTitle(record?.title ?? "");
    setMeetingBody(record?.meeting_body ?? "");
    setError(null);
    setNotice("Unsaved catalog edits discarded.");
  }

  async function save(event: { preventDefault: () => void }) {
    event.preventDefault();
    if (!selectedSha || !canEdit || conflict || (record !== null && record.asset_sha256 !== selectedSha)) return;
    if (!catalogTextFits(title) || !catalogTextFits(meetingBody)) {
      setError("Title and meeting body must each be no more than 240 UTF-8 bytes.");
      return;
    }
    setBusy("save"); setError(null); setNotice(null);
    try {
      const saved = parseCatalogAssetRecord(await apiRequest<unknown>(catalogPath(selectedSha), {
        method: "PUT",
        body: JSON.stringify(catalogWriteBody({ title, meeting_body: meetingBody || null }, record?.revision ?? null)),
      }));
      acceptSavedRecord(saved);
      setHistory(parseCatalogAssetHistory(await apiRequest<unknown>(`${catalogPath(saved.asset_sha256)}/history?limit=${catalogHistoryLimit()}`)));
      setNotice(`Catalog revision ${saved.revision} saved.`);
    } catch (problem) {
      if (problem instanceof ApiError && problem.status === 409) {
        setConflict(true);
        setNotice("This catalog record changed elsewhere. Read current before making another change.");
      }
      setError(problem instanceof Error ? problem.message : "Catalog record could not be saved.");
    } finally { setBusy(null); }
  }

  async function transition(event: { preventDefault: () => void }, action: CatalogAssetTransition) {
    event.preventDefault();
    if (!record || record.asset_sha256 !== selectedSha || !canEdit || conflict) return;
    setBusy(action); setError(null); setNotice(null);
    try {
      const saved = parseCatalogAssetRecord(await apiRequest<unknown>(catalogTransitionPath(catalogPath(record.asset_sha256), action), {
        method: "POST",
        body: JSON.stringify(catalogTransitionBody(record.revision)),
      }));
      acceptSavedRecord(saved);
      setHistory(parseCatalogAssetHistory(await apiRequest<unknown>(`${catalogPath(saved.asset_sha256)}/history?limit=${catalogHistoryLimit()}`)));
      setNotice(`Catalog revision ${saved.revision} ${action === "publish" ? "published" : "withdrawn"}.`);
    } catch (problem) {
      if (problem instanceof ApiError && problem.status === 409) {
        setConflict(true);
        setNotice("This catalog record changed elsewhere. Read current before making another change.");
      }
      setError(problem instanceof Error ? problem.message : `Catalog record could not be ${action === "publish" ? "published" : "withdrawn"}.`);
    } finally { setBusy(null); }
  }

  const actionBusy = busy !== null;
  return <section className="catalog-editor panel">
    <div className="panel-head"><div><p className="eyebrow">Editorial authority</p><h2>Catalog details</h2></div><div className="button-row"><span className="count">{records.length} records</span>{dirty && <><span className="count">Unsaved changes</span><button type="button" className="secondary" onClick={discardEdits} disabled={actionBusy}>Discard changes</button></>}<button type="button" className="secondary" onClick={() => void load(true)} disabled={actionBusy || dirty}>Refresh</button></div></div>
    {error && <div className="error-box" role="alert">{error}</div>}
    {notice && <div className="notice" role="status">{notice}</div>}
    {supportAdmin && !canEdit && <div className="catalog-boundary" role="status"><strong>Support Admin read-only</strong><span>Catalog authority can be inspected, but editorial mutations are unavailable to this role.</span></div>}
    {!roles.length && !busy && <div className="catalog-boundary" role="status"><strong>Catalog access unavailable</strong><span>Your session does not have an operator role that can maintain catalog authority.</span></div>}
    {!assets.length ? <div className="catalog-empty"><strong>No media assets</strong><p>Import and verify media before creating its editorial catalog authority.</p></div> : <>
      <div className="catalog-selector"><label>Media asset<select value={selectedSha} onChange={(event) => setSelectedSha(event.target.value)} disabled={actionBusy || dirty}>{assets.map((item) => <option key={item.asset_id} value={item.asset_id}>{item.media_path.split(/[\\/]/).pop() ?? item.asset_id.slice(0, 16) + "…"}</option>)}</select></label>{selectedAsset && <small>{record ? "Catalog authority exists" : "No catalog authority yet"} · {selectedAsset.asset_id.slice(0, 16)}…</small>}</div>
      {selectedSha && <div className="catalog-editor-layout"><form className="catalog-form" onSubmit={(event) => void save(event)}>
        <div className="catalog-state-row"><div><span className="catalog-label">Current state</span>{record ? <strong className={`catalog-state catalog-state-${record.editorial_state}`}>{record.editorial_state}</strong> : <strong>New draft</strong>}</div><div><span className="catalog-label">Revision</span><strong>{record?.revision ?? "Not created"}</strong></div><div><span className="catalog-label">First published</span><strong>{record?.published_at_utc_ms == null ? "Not published" : formatCatalogTime(record.published_at_utc_ms)}</strong></div></div>
        <label>Title<input required maxLength={240} value={title} disabled={!canEdit || actionBusy || conflict} onChange={(event) => setTitle(event.target.value)} /><small>Up to 240 UTF-8 bytes. The server remains authoritative.</small></label>
        <label>Meeting body<input maxLength={240} value={meetingBody} disabled={!canEdit || actionBusy || conflict} onChange={(event) => setMeetingBody(event.target.value)} /><small>Optional, up to 240 UTF-8 bytes.</small></label>
        {conflict && record && <div className="catalog-conflict" role="alert"><strong>Authoritative reload required</strong><span>Save, publish, republish, and withdraw are locked until you read the current record.</span><button type="button" className="secondary" onClick={() => void readCurrent(record.asset_sha256)} disabled={actionBusy}>Read current</button></div>}
        <div className="button-row"><button type="submit" className="primary" disabled={!canEdit || actionBusy || conflict}>{busy === "save" ? "Saving…" : record ? "Save" : "Create draft"}</button>{record?.editorial_state === "draft" && <button type="button" className="primary" disabled={!canEdit || actionBusy || conflict} onClick={(event) => void transition(event, "publish")}>Publish</button>}{record?.editorial_state === "withdrawn" && <button type="button" className="primary" disabled={!canEdit || actionBusy || conflict} onClick={(event) => void transition(event, "publish")}>Republish</button>}{record?.editorial_state === "published" && <button type="button" className="danger" disabled={!canEdit || actionBusy || conflict} onClick={(event) => void transition(event, "withdraw")}>Withdraw</button>}</div>
      </form><aside className="catalog-history"><div className="panel-head"><div><p className="eyebrow">Append-only record</p><h3>Revision history</h3></div>{record && <button type="button" className="text-button" onClick={() => void readCurrent(record.asset_sha256)} disabled={actionBusy}>Read current</button>}</div>{history.length ? <div className="catalog-history-list">{history.map((entry) => <div key={`${entry.record.revision}-${entry.occurred_at_utc_ms}`}><strong>Revision {entry.record.revision} · {entry.record.editorial_state}</strong><small>{entry.changed_by} · {formatCatalogTime(entry.occurred_at_utc_ms)}</small></div>)}</div> : <p className="muted">{record ? "No history available." : "Create the draft to begin append-only history."}</p>}</aside></div>}
    </>}
  </section>;
}
