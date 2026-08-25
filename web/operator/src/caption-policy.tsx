import { useCallback, useEffect, useState } from "react";
import { apiRequest } from "./api-client";
import { CaptionMode, captionPolicyMutationReason } from "./caption-policy-contract";

interface CaptionPolicy {
  channel_id: string;
  mode: CaptionMode;
  service_number: number;
  language: string;
  revision: number;
  created_at_unix_ms: number;
  updated_at_unix_ms: number;
  updated_by: string;
}

interface CaptionPolicyAudit {
  audit_id: string;
  channel_id: string;
  mode: CaptionMode;
  service_number: number;
  language: string;
  revision: number;
  actor: string;
  occurred_at_unix_ms: number;
}

interface CaptionPolicyAuthority {
  channel_id: string;
  policy: CaptionPolicy | null;
  audit: CaptionPolicyAudit[];
}

export function CaptionPolicyPanel({
  channelId,
  channelName,
  enabled,
}: {
  channelId: string;
  channelName: string;
  enabled: boolean;
}) {
  const [authority, setAuthority] = useState<CaptionPolicyAuthority | null>(null);
  const [mode, setMode] = useState<CaptionMode>("cea708");
  const [serviceNumber, setServiceNumber] = useState(1);
  const [language, setLanguage] = useState("en-US");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    const next = await apiRequest<CaptionPolicyAuthority>(
      `/api/v1/channels/${encodeURIComponent(channelId)}/caption-policy`,
    );
    setAuthority(next);
    if (next.policy) {
      setMode(next.policy.mode);
      setServiceNumber(next.policy.service_number);
      setLanguage(next.policy.language);
    }
  }, [channelId]);

  useEffect(() => {
    setError(null);
    setMessage(null);
    void load().catch((problem) => {
      setError(problem instanceof Error ? problem.message : "Caption policy could not be read.");
    });
  }, [load]);

  const blockedReason = captionPolicyMutationReason(enabled, serviceNumber, language);

  async function save() {
    if (blockedReason) return;
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      await apiRequest<CaptionPolicy>(
        `/api/v1/channels/${encodeURIComponent(channelId)}/caption-policy`,
        {
          method: "PUT",
          body: JSON.stringify({
            mode,
            service_number: serviceNumber,
            language,
            expected_revision: authority?.policy?.revision ?? 0,
          }),
        },
      );
      await load();
      setMessage(
        mode === "cea708"
          ? "CEA-708 caption policy saved. The next worker start will load this authority."
          : "Caption transport disabled for the next worker start.",
      );
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "Caption policy could not be saved.");
    } finally {
      setBusy(false);
    }
  }

  return <article className="panel caption-policy-panel">
    <div className="panel-head"><div><p className="eyebrow">Caption transport</p><h2>{channelName}</h2></div><span className={`caption-mode caption-mode-${authority?.policy?.mode ?? "missing"}`}>{authority?.policy ? `${authority.policy.mode === "cea708" ? "CEA-708" : "disabled"} · revision ${authority.policy.revision}` : "not configured"}</span></div>
    <p className="muted">Set the server-authoritative embedded caption transport loaded when this channel worker starts. Caption cues remain separate timed program events.</p>
    <div className="caption-policy-fields">
      <label>Transport mode<select value={mode} onChange={(event) => { setMode(event.target.value as CaptionMode); setMessage(null); }} disabled={busy || enabled}><option value="cea708">CEA-708 embedded captions</option><option value="disabled">Disabled</option></select></label>
      <label>CEA-708 service<input type="number" min="1" max="63" step="1" value={serviceNumber} onChange={(event) => { setServiceNumber(Number(event.target.value)); setMessage(null); }} disabled={busy || enabled} /></label>
      <label>Language tag<input required minLength={2} maxLength={35} value={language} onChange={(event) => { setLanguage(event.target.value); setMessage(null); }} placeholder="en-US" disabled={busy || enabled} /></label>
    </div>
    {authority?.policy && <dl className="caption-policy-authority"><div><dt>Current authority</dt><dd>{authority.policy.mode === "cea708" ? `Service ${authority.policy.service_number} · ${authority.policy.language}` : "Caption transport disabled"}</dd></div><div><dt>Last changed</dt><dd>{new Date(authority.policy.updated_at_unix_ms).toLocaleString()} · {authority.policy.updated_by}</dd></div><div><dt>Audit entries</dt><dd>{authority.audit.length}</dd></div></dl>}
    {blockedReason && <div className="headend-warning" role="status">{blockedReason}</div>}
    {error && <div className="error-box" role="alert">{error}</div>}
    {message && <div className="notice" role="status">{message}</div>}
    <button className="primary wide" disabled={busy || Boolean(blockedReason)} onClick={() => void save()}>{busy ? "Saving…" : enabled ? "Stop output to change" : "Save caption policy"}</button>
  </article>;
}
