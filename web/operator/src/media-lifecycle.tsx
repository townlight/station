import { FormEvent, useEffect, useRef, useState } from "react";
import { apiRequest } from "./api-client";
import { AssetOperationOwner, MediaLifecycleAuthority, assetOperationStillOwns, mediaLifecycleRenderState } from "./media-lifecycle-contract";

type Asset = { asset_id: string; media_path: string; readiness_state: "ready" | "processing" | "missing_file" | "rejected" };

export function MediaLifecyclePanel({ asset, onClose, onRefresh }: { asset: Asset; onClose: () => void; onRefresh: () => Promise<void> }) {
  const [stateAssetId, setStateAssetId] = useState(asset.asset_id);
  const [lifecycle, setLifecycle] = useState<MediaLifecycleAuthority | null>(null);
  const [sourcePath, setSourcePath] = useState("");
  const [reviewAfter, setReviewAfter] = useState("");
  const [permanent, setPermanent] = useState(false);
  const [legalHold, setLegalHold] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const requestGeneration = useRef(0);
  const currentAssetId = useRef(asset.asset_id);
  if (currentAssetId.current !== asset.asset_id) {
    currentAssetId.current = asset.asset_id;
    requestGeneration.current += 1;
  }

  function owns(owner: AssetOperationOwner) {
    return assetOperationStillOwns(owner, currentAssetId.current, requestGeneration.current);
  }

  async function load(requestedAssetId = asset.asset_id) {
    const generation = ++requestGeneration.current;
    const owner = { asset_id: requestedAssetId, generation };
    setStateAssetId(requestedAssetId); setLifecycle(null); setMessage(null); setBusy(false);
    try {
      const value = await apiRequest<MediaLifecycleAuthority>(`/api/v1/assets/${requestedAssetId}/lifecycle`);
      if (!owns(owner)) return;
      if (value.asset_id !== requestedAssetId) throw new Error("Lifecycle authority was returned for a different asset.");
      setLifecycle(value);
      setPermanent(value.retention?.permanent ?? false);
      setLegalHold(value.retention?.legal_hold ?? false);
      setReviewAfter(value.retention?.review_after_unix_ms?.toString() ?? "");
    } catch (problem) {
      if (owns(owner)) setMessage(problem instanceof Error ? problem.message : "Lifecycle authority could not be read.");
    }
  }
  useEffect(() => { void load(); return () => { requestGeneration.current += 1; }; }, [asset.asset_id]);

  async function relink(event: FormEvent) {
    event.preventDefault();
    const requestedAssetId = asset.asset_id;
    const owner = { asset_id: requestedAssetId, generation: ++requestGeneration.current };
    setStateAssetId(requestedAssetId); setBusy(true); setMessage(null);
    try {
      await apiRequest(`/api/v1/assets/${requestedAssetId}/relink`, { method: "POST", body: JSON.stringify({ source_path: sourcePath.trim() }) });
      if (!owns(owner)) return;
      setSourcePath(""); setMessage("The server admitted exact matching bytes and restored this missing asset.");
      await onRefresh();
      if (!owns(owner)) return;
      await load(requestedAssetId);
    } catch (problem) { if (owns(owner)) setMessage(problem instanceof Error ? problem.message : "The missing asset could not be relinked."); }
    finally { if (owns(owner)) setBusy(false); }
  }
  async function replace(event: FormEvent) {
    event.preventDefault();
    const requestedAssetId = asset.asset_id;
    const owner = { asset_id: requestedAssetId, generation: ++requestGeneration.current };
    setStateAssetId(requestedAssetId); setBusy(true); setMessage(null);
    try {
      await apiRequest(`/api/v1/assets/${requestedAssetId}/replacements`, { method: "POST", body: JSON.stringify({ source_path: sourcePath.trim(), expected_predecessor_revision: lifecycle?.replacement ? 1 : 0 }) });
      if (!owns(owner)) return;
      setSourcePath(""); setMessage("A distinct immutable replacement was admitted. No scheduled or on-air authority was changed.");
      await onRefresh();
      if (!owns(owner)) return;
      await load(requestedAssetId);
    } catch (problem) { if (owns(owner)) setMessage(problem instanceof Error ? problem.message : "Replacement could not be recorded."); }
    finally { if (owns(owner)) setBusy(false); }
  }
  async function saveRetention(event: FormEvent) {
    event.preventDefault();
    const requestedAssetId = asset.asset_id;
    const owner = { asset_id: requestedAssetId, generation: ++requestGeneration.current };
    setStateAssetId(requestedAssetId); setBusy(true); setMessage(null);
    try {
      const review_after_unix_ms = reviewAfter.trim() ? Number(reviewAfter) : null;
      await apiRequest(`/api/v1/assets/${requestedAssetId}/retention`, { method: "PUT", body: JSON.stringify({ permanent, review_after_unix_ms, legal_hold: legalHold, expected_revision: lifecycle?.retention?.revision ?? 0 }) });
      if (!owns(owner)) return;
      setMessage("Retention intent was recorded. Due items require review; the station never deletes media automatically.");
      await load(requestedAssetId);
    } catch (problem) { if (owns(owner)) setMessage(problem instanceof Error ? problem.message : "Retention intent could not be saved."); }
    finally { if (owns(owner)) setBusy(false); }
  }

  const rendered = mediaLifecycleRenderState(asset.asset_id, { state_asset_id: stateAssetId, authority: lifecycle, message, busy });
  const renderedLifecycle = rendered.authority;
  const renderedMessage = rendered.message;
  const renderedBusy = rendered.busy;
  const formIsCurrent = stateAssetId === asset.asset_id;
  const renderedSourcePath = formIsCurrent ? sourcePath : "";
  const renderedReviewAfter = formIsCurrent ? reviewAfter : "";
  const renderedPermanent = formIsCurrent ? permanent : false;
  const renderedLegalHold = formIsCurrent ? legalHold : false;
  const isMissing = asset.readiness_state === "missing_file";
  return <section className="panel media-lifecycle-panel" aria-live="polite">
    <div className="panel-head"><div><p className="eyebrow">Immutable lifecycle authority</p><h2>{asset.media_path.split(/[\\/]/).pop()}</h2></div><button className="secondary" onClick={onClose}>Close</button></div>
    {renderedMessage && <div className="notice" role="status">{renderedMessage}</div>}
    {!renderedLifecycle && !renderedMessage && <p className="muted">Reading server authority…</p>}
    {renderedLifecycle && <div className="lifecycle-summary"><p><strong>Retention:</strong> {renderedLifecycle.retention?.disposition ?? "not configured"}</p><p><strong>Replacement:</strong> {renderedLifecycle.replacement ? `successor ${renderedLifecycle.replacement.successor_asset_id.slice(0, 12)}…` : "none"}</p><p><strong>Review thumbnail:</strong> {renderedLifecycle.thumbnail ? `derived ${renderedLifecycle.thumbnail.thumbnail_sha256.slice(0, 12)}…` : "not yet generated"}</p><p><strong>Exact-byte recoveries:</strong> {renderedLifecycle.recovery_history.length ? `${renderedLifecycle.recovery_history.length}; latest by ${renderedLifecycle.recovery_history.at(-1)?.changed_by}` : "none"}</p></div>}
    {renderedLifecycle?.thumbnail && <figure className="lifecycle-thumbnail"><img src={`/api/v1/assets/${asset.asset_id}/thumbnail`} alt={`Server-derived review thumbnail for ${asset.media_path.split(/[\\/]/).pop() ?? "media asset"}`} /><figcaption>Server-derived, immutable review evidence. The browser does not receive or control a filesystem path.</figcaption></figure>}
    <div className="lifecycle-grid">
      <form onSubmit={saveRetention}><h3>Retention intent</h3><label><input type="checkbox" checked={renderedPermanent} onChange={(event) => setPermanent(event.target.checked)} /> Permanent retention</label><label>Review after (UTC milliseconds)<input disabled={renderedPermanent} inputMode="numeric" value={renderedReviewAfter} onChange={(event) => setReviewAfter(event.target.value)} placeholder="1735689600000" /></label><label><input type="checkbox" checked={renderedLegalHold} onChange={(event) => setLegalHold(event.target.checked)} /> Legal hold</label><button className="secondary" disabled={renderedBusy || !renderedLifecycle}>{renderedBusy ? "Saving…" : "Save retention"}</button></form>
      <form onSubmit={isMissing ? relink : replace}><h3>{isMissing ? "Recover missing bytes" : "Create replacement"}</h3><p className="muted">The server copies, discovers, and hashes the candidate. {isMissing ? "It accepts only this exact asset identity." : "It creates a new immutable identity and never changes an existing schedule."}</p><label>Absolute source path<input required value={renderedSourcePath} onChange={(event) => setSourcePath(event.target.value)} placeholder="C:\\Media\\Council Meeting.mp4" /></label><button className="secondary" disabled={renderedBusy || !renderedLifecycle || Boolean(renderedLifecycle?.replacement)}>{renderedBusy ? "Admitting…" : isMissing ? "Verify & recover" : renderedLifecycle?.replacement ? "Replacement already recorded" : "Admit replacement"}</button></form>
    </div>
    <p className="muted lifecycle-boundary">Thumbnail evidence is shown only when a bounded server worker has produced it. This screen cannot upload a thumbnail or choose its output path.</p>
  </section>;
}
