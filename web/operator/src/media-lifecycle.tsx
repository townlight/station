import { FormEvent, useEffect, useRef, useState } from "react";
import { apiRequest } from "./api-client";

type Asset = { asset_id: string; media_path: string; readiness_state: "ready" | "processing" | "missing_file" | "rejected" };
type Retention = { revision: number; permanent: boolean; review_after_unix_ms: number | null; legal_hold: boolean; disposition: "retained" | "review_required" | "legal_hold" };
type Lifecycle = { asset_id: string; retention: Retention | null; replacement: { successor_asset_id: string } | null; thumbnail: { thumbnail_sha256: string } | null };

export function MediaLifecyclePanel({ asset, onClose, onRefresh }: { asset: Asset; onClose: () => void; onRefresh: () => Promise<void> }) {
  const [lifecycle, setLifecycle] = useState<Lifecycle | null>(null);
  const [sourcePath, setSourcePath] = useState("");
  const [reviewAfter, setReviewAfter] = useState("");
  const [permanent, setPermanent] = useState(false);
  const [legalHold, setLegalHold] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const requestGeneration = useRef(0);

  async function load() {
    const generation = ++requestGeneration.current;
    setLifecycle(null); setMessage(null);
    try {
      const value = await apiRequest<Lifecycle>(`/api/v1/assets/${asset.asset_id}/lifecycle`);
      if (generation !== requestGeneration.current) return;
      setLifecycle(value);
      setPermanent(value.retention?.permanent ?? false);
      setLegalHold(value.retention?.legal_hold ?? false);
      setReviewAfter(value.retention?.review_after_unix_ms?.toString() ?? "");
    } catch (problem) {
      if (generation === requestGeneration.current) setMessage(problem instanceof Error ? problem.message : "Lifecycle authority could not be read.");
    }
  }
  useEffect(() => { void load(); return () => { requestGeneration.current += 1; }; }, [asset.asset_id]);

  async function relink(event: FormEvent) {
    event.preventDefault(); setBusy(true); setMessage(null);
    try {
      await apiRequest(`/api/v1/assets/${asset.asset_id}/relink`, { method: "POST", body: JSON.stringify({ source_path: sourcePath.trim() }) });
      setSourcePath(""); setMessage("The server admitted exact matching bytes and restored this missing asset."); await onRefresh(); await load();
    } catch (problem) { setMessage(problem instanceof Error ? problem.message : "The missing asset could not be relinked."); }
    finally { setBusy(false); }
  }
  async function replace(event: FormEvent) {
    event.preventDefault(); setBusy(true); setMessage(null);
    try {
      await apiRequest(`/api/v1/assets/${asset.asset_id}/replacements`, { method: "POST", body: JSON.stringify({ source_path: sourcePath.trim(), expected_predecessor_revision: lifecycle?.replacement ? 1 : 0 }) });
      setSourcePath(""); setMessage("A distinct immutable replacement was admitted. No scheduled or on-air authority was changed."); await onRefresh(); await load();
    } catch (problem) { setMessage(problem instanceof Error ? problem.message : "Replacement could not be recorded."); }
    finally { setBusy(false); }
  }
  async function saveRetention(event: FormEvent) {
    event.preventDefault(); setBusy(true); setMessage(null);
    try {
      const review_after_unix_ms = reviewAfter.trim() ? Number(reviewAfter) : null;
      await apiRequest(`/api/v1/assets/${asset.asset_id}/retention`, { method: "PUT", body: JSON.stringify({ permanent, review_after_unix_ms, legal_hold: legalHold, expected_revision: lifecycle?.retention?.revision ?? 0 }) });
      setMessage("Retention intent was recorded. Due items require review; the station never deletes media automatically."); await load();
    } catch (problem) { setMessage(problem instanceof Error ? problem.message : "Retention intent could not be saved."); }
    finally { setBusy(false); }
  }

  const isMissing = asset.readiness_state === "missing_file";
  return <section className="panel media-lifecycle-panel" aria-live="polite">
    <div className="panel-head"><div><p className="eyebrow">Immutable lifecycle authority</p><h2>{asset.media_path.split(/[\\/]/).pop()}</h2></div><button className="secondary" onClick={onClose}>Close</button></div>
    {message && <div className="notice" role="status">{message}</div>}
    {!lifecycle && !message && <p className="muted">Reading server authority…</p>}
    {lifecycle && <div className="lifecycle-summary"><p><strong>Retention:</strong> {lifecycle.retention?.disposition ?? "not configured"}</p><p><strong>Replacement:</strong> {lifecycle.replacement ? `successor ${lifecycle.replacement.successor_asset_id.slice(0, 12)}…` : "none"}</p><p><strong>Review thumbnail:</strong> {lifecycle.thumbnail ? `derived ${lifecycle.thumbnail.thumbnail_sha256.slice(0, 12)}…` : "not yet generated"}</p></div>}
    {lifecycle?.thumbnail && <figure className="lifecycle-thumbnail"><img src={`/api/v1/assets/${asset.asset_id}/thumbnail`} alt={`Server-derived review thumbnail for ${asset.media_path.split(/[\\/]/).pop() ?? "media asset"}`} /><figcaption>Server-derived, immutable review evidence. The browser does not receive or control a filesystem path.</figcaption></figure>}
    <div className="lifecycle-grid">
      <form onSubmit={saveRetention}><h3>Retention intent</h3><label><input type="checkbox" checked={permanent} onChange={(event) => setPermanent(event.target.checked)} /> Permanent retention</label><label>Review after (UTC milliseconds)<input disabled={permanent} inputMode="numeric" value={reviewAfter} onChange={(event) => setReviewAfter(event.target.value)} placeholder="1735689600000" /></label><label><input type="checkbox" checked={legalHold} onChange={(event) => setLegalHold(event.target.checked)} /> Legal hold</label><button className="secondary" disabled={busy}>{busy ? "Saving…" : "Save retention"}</button></form>
      <form onSubmit={isMissing ? relink : replace}><h3>{isMissing ? "Recover missing bytes" : "Create replacement"}</h3><p className="muted">The server copies, discovers, and hashes the candidate. {isMissing ? "It accepts only this exact asset identity." : "It creates a new immutable identity and never changes an existing schedule."}</p><label>Absolute source path<input required value={sourcePath} onChange={(event) => setSourcePath(event.target.value)} placeholder="C:\\Media\\Council Meeting.mp4" /></label><button className="secondary" disabled={busy || Boolean(lifecycle?.replacement)}>{busy ? "Admitting…" : isMissing ? "Verify & recover" : lifecycle?.replacement ? "Replacement already recorded" : "Admit replacement"}</button></form>
    </div>
    <p className="muted lifecycle-boundary">Thumbnail evidence is shown only when a bounded server worker has produced it. This screen cannot upload a thumbnail or choose its output path.</p>
  </section>;
}
