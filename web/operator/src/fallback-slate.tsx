import { useCallback, useEffect, useMemo, useState } from "react";
import { apiRequest } from "./api-client";
import { fallbackSlateMutationReason, FallbackImageReadiness } from "./fallback-slate-contract";

interface ManagedImage {
  asset_id: string;
  mime_type: string;
  byte_size: number;
  width: number;
  height: number;
  readiness: FallbackImageReadiness;
}

interface FallbackSlateSelection {
  channel_id: string;
  image_asset: ManagedImage;
  revision: number;
  created_at_unix_ms: number;
  updated_at_unix_ms: number;
  updated_by: string;
}

interface FallbackSlateAudit {
  audit_id: string;
  image_asset_id: string;
  revision: number;
  actor: string;
  occurred_at_unix_ms: number;
}

interface FallbackSlateAuthority {
  channel_id: string;
  selection: FallbackSlateSelection | null;
  audit: FallbackSlateAudit[];
}

function imageLabel(image: ManagedImage): string {
  return `${image.asset_id.slice(0, 12)}… · ${image.width}×${image.height} ${image.mime_type === "image/png" ? "PNG" : "JPEG"} · ${image.readiness}`;
}

export function FallbackSlatePanel({ channelId, channelName, enabled }: { channelId: string; channelName: string; enabled: boolean }) {
  const [authority, setAuthority] = useState<FallbackSlateAuthority | null>(null);
  const [images, setImages] = useState<ManagedImage[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    const [nextAuthority, nextImages] = await Promise.all([
      apiRequest<FallbackSlateAuthority>(`/api/v1/channels/${channelId}/fallback-slate`),
      apiRequest<ManagedImage[]>("/api/v1/cg/images"),
    ]);
    setAuthority(nextAuthority);
    setImages(nextImages);
    setSelectedId(nextAuthority.selection?.image_asset.asset_id ?? nextImages.find((image) => image.readiness === "ready")?.asset_id ?? "");
  }, [channelId]);

  useEffect(() => {
    setError(null);
    void load().catch((problem) => setError(problem instanceof Error ? problem.message : "Fallback slate authority could not be read."));
  }, [load]);

  const selectedImage = useMemo(() => images.find((image) => image.asset_id === selectedId) ?? null, [images, selectedId]);
  const blockedReason = fallbackSlateMutationReason(enabled, selectedImage?.readiness ?? null);

  async function upload(file: File | undefined) {
    if (!file) return;
    setBusy(true); setError(null); setMessage(null);
    try {
      const image = await apiRequest<ManagedImage>("/api/v1/cg/images", { method: "POST", headers: file.type ? { "Content-Type": file.type } : undefined, body: file });
      await load();
      setSelectedId(image.asset_id);
      setMessage("Image verified and added to the managed library. Save it to bind this channel.");
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "The image could not be uploaded.");
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    if (!selectedImage || blockedReason) return;
    setBusy(true); setError(null); setMessage(null);
    try {
      await apiRequest<FallbackSlateSelection>(`/api/v1/channels/${channelId}/fallback-slate`, {
        method: "PUT",
        body: JSON.stringify({ image_asset_id: selectedImage.asset_id, expected_revision: authority?.selection?.revision ?? 0 }),
      });
      await load();
      setMessage("Fallback slate authority saved. It will be loaded before this channel worker reports Ready.");
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "The fallback slate could not be saved.");
    } finally {
      setBusy(false);
    }
  }

  return <article className="panel fallback-slate-panel">
    <div className="panel-head"><div><p className="eyebrow">Fallback source</p><h2>{channelName}</h2></div><span className={`cg-state-pill cg-state-${authority?.selection?.image_asset.readiness ?? "missing"}`}>{authority?.selection ? `revision ${authority.selection.revision}` : "not configured"}</span></div>
    <p className="muted">This image appears only when the channel is in fallback. Program, live, CG, and EAS remain separate authorities.</p>
    <label>Managed station slate<select value={selectedId} onChange={(event) => { setSelectedId(event.target.value); setMessage(null); }} disabled={busy || enabled}><option value="">Choose a ready PNG or JPEG</option>{images.map((image) => <option key={image.asset_id} value={image.asset_id} disabled={image.readiness !== "ready"}>{imageLabel(image)}</option>)}</select></label>
    <label>Upload PNG or JPEG<input type="file" accept="image/png,image/jpeg" disabled={busy || enabled} onChange={(event) => { void upload(event.target.files?.[0]); event.currentTarget.value = ""; }} /></label>
    {authority?.selection && <dl className="fallback-slate-authority"><div><dt>Current image</dt><dd><code>{authority.selection.image_asset.asset_id}</code></dd></div><div><dt>Verified state</dt><dd>{authority.selection.image_asset.readiness}</dd></div><div><dt>Last changed</dt><dd>{new Date(authority.selection.updated_at_unix_ms).toLocaleString()} · {authority.selection.updated_by}</dd></div><div><dt>Audit entries</dt><dd>{authority.audit.length}</dd></div></dl>}
    {blockedReason && <div className="headend-warning" role="status">{blockedReason}</div>}
    {error && <div className="error-box" role="alert">{error}</div>}
    {message && <div className="notice" role="status">{message}</div>}
    <button className="primary wide" disabled={busy || Boolean(blockedReason)} onClick={() => void save()}>{busy ? "Working…" : enabled ? "Stop output to change" : "Save fallback slate"}</button>
  </article>;
}
