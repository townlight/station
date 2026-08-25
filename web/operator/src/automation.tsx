import { useCallback, useEffect, useMemo, useState } from "react";
import { apiRequest } from "./api-client";
import {
  AUTOMATION_FEATURES,
  adaptersForFeature,
  automationFeatureLabel,
  automationPolicyMutationReason,
  automationStatusLabel,
  type AutomationAdapterDescriptor,
  type AutomationFeature,
  type AutomationFeaturePolicy,
  type AutomationPolicyStatus,
} from "./automation-contract";

interface OperatorSession { session: { roles: string[] } }

export function AutomationPanel() {
  const [adapters, setAdapters] = useState<AutomationAdapterDescriptor[]>([]);
  const [policies, setPolicies] = useState<AutomationFeaturePolicy[]>([]);
  const [statuses, setStatuses] = useState<Record<string, AutomationPolicyStatus>>({});
  const [setupAdmin, setSetupAdmin] = useState(false);
  const [busyFeature, setBusyFeature] = useState<AutomationFeature | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    const [nextAdapters, nextPolicies, session] = await Promise.all([
      apiRequest<AutomationAdapterDescriptor[]>("/api/v1/automation/adapters"),
      apiRequest<AutomationFeaturePolicy[]>("/api/v1/automation/policies"),
      apiRequest<OperatorSession>("/api/v1/auth/session"),
    ]);
    const nextStatuses = Object.fromEntries(await Promise.all(nextPolicies.map(async (policy) => [
      policy.feature,
      await apiRequest<AutomationPolicyStatus>(`/api/v1/automation/policies/${encodeURIComponent(policy.policy_id)}/status`),
    ])));
    setAdapters(nextAdapters);
    setPolicies(nextPolicies);
    setStatuses(nextStatuses);
    setSetupAdmin(session.session.roles.includes("setup_admin"));
  }, []);

  useEffect(() => {
    void load().catch((problem) => setError(problem instanceof Error ? problem.message : "Automation authority could not be read."));
  }, [load]);

  const byFeature = useMemo(() => new Map(policies.map((policy) => [policy.feature, policy])), [policies]);

  async function save(feature: AutomationFeature, adapterId: string, configurationHash: string) {
    const policy = byFeature.get(feature);
    const blocked = automationPolicyMutationReason(adapters, feature, adapterId, configurationHash);
    if (blocked || !setupAdmin) return;
    setBusyFeature(feature); setError(null); setMessage(null);
    try {
      const policyId = policy?.policy_id ?? crypto.randomUUID();
      await apiRequest(`/api/v1/automation/policies/${encodeURIComponent(policyId)}`, {
        method: "PUT",
        body: JSON.stringify({
          feature,
          operator_selected_adapter_id: adapterId,
          adaptive_default: null,
          configuration_hash: configurationHash,
          expected_revision: policy?.revision ?? null,
        }),
      });
      await load();
      setMessage(`${automationFeatureLabel(feature)} policy saved. Connection evidence must be produced by the trusted adapter test.`);
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "Automation policy could not be saved.");
    } finally { setBusyFeature(null); }
  }

  return <>
    <header className="page-header"><div><p className="eyebrow">Server-authoritative automation</p><h1>Models and adapters</h1><p>Choose the adapter for each feature. A saved selection is not ready until a trusted connection test proves the exact installed candidate and configuration.</p></div><div className="header-actions"><button className="secondary" onClick={() => void load()} disabled={busyFeature !== null}>Refresh authority</button></div></header>
    {error && <div className="error-box" role="alert">{error}</div>}
    {message && <div className="notice" role="status">{message}</div>}
    <section className="automation-grid" aria-label="Automation feature policies">
      {AUTOMATION_FEATURES.map((feature) => <AutomationFeatureCard
        key={feature}
        feature={feature}
        adapters={adaptersForFeature(adapters, feature)}
        policy={byFeature.get(feature) ?? null}
        status={statuses[feature] ?? null}
        canWrite={setupAdmin}
        busy={busyFeature === feature}
        onSave={save}
      />)}
    </section>
  </>;
}

function AutomationFeatureCard({ feature, adapters, policy, status, canWrite, busy, onSave }: {
  feature: AutomationFeature;
  adapters: AutomationAdapterDescriptor[];
  policy: AutomationFeaturePolicy | null;
  status: AutomationPolicyStatus | null;
  canWrite: boolean;
  busy: boolean;
  onSave: (feature: AutomationFeature, adapterId: string, configurationHash: string) => Promise<void>;
}) {
  const [adapterId, setAdapterId] = useState(policy?.operator_selected_adapter_id ?? adapters[0]?.adapter_id ?? "");
  const [configurationHash, setConfigurationHash] = useState(policy?.configuration_hash ?? "");
  useEffect(() => {
    setAdapterId(policy?.operator_selected_adapter_id ?? adapters[0]?.adapter_id ?? "");
    setConfigurationHash(policy?.configuration_hash ?? "");
  }, [adapters, policy]);
  const blocked = automationPolicyMutationReason(adapters, feature, adapterId, configurationHash);
  const selected = adapters.find((adapter) => adapter.adapter_id === adapterId);
  return <article className="panel automation-card">
    <div className="panel-head"><div><p className="eyebrow">{feature}</p><h2>{automationFeatureLabel(feature)}</h2></div><span className={`pill pill-${status?.status === "passed" ? "good" : status?.status === "failed" ? "bad" : "active"}`}>{status ? automationStatusLabel(status.status) : "Policy missing"}</span></div>
    <p className="muted">Operator selection is explicit. Adaptive defaults, credentials, and test evidence cannot be invented by this browser.</p>
    <label>Selected adapter<select value={adapterId} onChange={(event) => setAdapterId(event.target.value)} disabled={!canWrite || busy}><option value="">Select an adapter</option>{adapters.map((adapter) => <option key={adapter.adapter_id} value={adapter.adapter_id}>{adapter.model_key} · {adapter.location}</option>)}</select></label>
    <label>Configuration SHA-256<input value={configurationHash} onChange={(event) => setConfigurationHash(event.target.value)} minLength={64} maxLength={64} spellCheck={false} placeholder="64 lowercase hexadecimal characters" disabled={!canWrite || busy} /></label>
    {selected && <dl className="automation-authority"><div><dt>Model</dt><dd>{selected.model_identifier}</dd></div><div><dt>Credential</dt><dd>{selected.credential_availability.replaceAll("_", " ")}</dd></div><div><dt>Adapter revision</dt><dd>{selected.revision}</dd></div>{policy && <div><dt>Policy revision</dt><dd>{policy.revision}</dd></div>}</dl>}
    {status?.evidence && <small className="muted">Last trusted test: {new Date(status.evidence.tested_at_unix_ms).toLocaleString()} · {status.evidence.outcome}</small>}
    {!canWrite && <div className="headend-warning" role="status">Setup Admin is required to change model policy.</div>}
    {canWrite && blocked && <div className="headend-warning" role="status">{blocked}</div>}
    <button className="primary wide" disabled={!canWrite || busy || Boolean(blocked)} onClick={() => void onSave(feature, adapterId, configurationHash)}>{busy ? "Saving…" : policy ? "Save policy revision" : "Create policy"}</button>
  </article>;
}
