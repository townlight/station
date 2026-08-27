import { FormEvent, ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiRequest } from "./api-client";
import type { AuthoritativeCommissioningReport } from "./commissioning-contract";
import {
  PUBLISH_TARGET_KINDS,
  emptyPublishingConfiguration,
  publishAccountMutation,
  publishTargetMutation,
  LatestPublishingAuthorityLoad,
  publishingAccountMutationReason,
  publishingHistoryPath,
  publishingExecutionHistoryPath,
  publishingExecutionJobPath,
  publishingExecutionRequestPath,
  publishingExecutionUnavailableReason,
  publishingJobStateLabel,
  publishingStageLabel,
  publishingKindLabel,
  publishingReadinessLabel,
  publishingTargetMutationReason,
  redactConfigurationHash,
  type PublicationReadinessCheck,
  type PublishAccountDraft,
  type PublishTargetAccount,
  type PublishTargetDraft,
  type PublishTargetProfile,
  type PublishExecutionJobResponse,
  type PublishExecutionTargetConfig,
} from "./publishing-contract";

interface OperatorSession { session: { roles: string[] } }
interface AccountHistory { account: PublishTargetAccount; changed_by: string; occurred_at_unix_ms: number }
interface TargetHistory { target: PublishTargetProfile; changed_by: string; occurred_at_unix_ms: number }
export interface ReadyPublishAsset { asset_id: string; media_path: string; duration_ms?: number; }

const EMPTY_ACCOUNT: PublishAccountDraft = { kind: "portal", label: "", enabled: true };
const EMPTY_TARGET: PublishTargetDraft = { accountId: "", kind: "portal", label: "", enabled: true, configuration: emptyPublishingConfiguration("portal") };

function time(value: number): string { return new Date(value).toLocaleString(); }
function statusClass(status: PublicationReadinessCheck["status"] | undefined): string {
  return status === "pass" ? "pass" : status === "blocked" ? "blocked" : "unknown";
}

export function PublishingPanel({ readyAssets = [] }: { readyAssets?: ReadyPublishAsset[] }) {
  const [catalogAssets, setCatalogAssets] = useState<ReadyPublishAsset[]>(readyAssets);
  const [accounts, setAccounts] = useState<PublishTargetAccount[]>([]);
  const [targets, setTargets] = useState<PublishTargetProfile[]>([]);
  const [accountHistory, setAccountHistory] = useState<Record<string, AccountHistory[]>>({});
  const [targetHistory, setTargetHistory] = useState<Record<string, TargetHistory[]>>({});
  const [executionJobs, setExecutionJobs] = useState<Record<string, PublishExecutionJobResponse[]>>({});
  const [selectedAssets, setSelectedAssets] = useState<Record<string, string>>({});
  const [readiness, setReadiness] = useState<PublicationReadinessCheck | null>(null);
  const [setupAdmin, setSetupAdmin] = useState(false);
  const [publishOperator, setPublishOperator] = useState(false);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [accountEditor, setAccountEditor] = useState<PublishTargetAccount | "new" | null>(null);
  const [targetEditor, setTargetEditor] = useState<PublishTargetProfile | "new" | null>(null);
  const [accountDraft, setAccountDraft] = useState<PublishAccountDraft>(EMPTY_ACCOUNT);
  const [targetDraft, setTargetDraft] = useState<PublishTargetDraft>(EMPTY_TARGET);
  const [credentialAccount, setCredentialAccount] = useState<PublishTargetAccount | null>(null);
  const [credential, setCredential] = useState("");
  const publishingLoad = useRef(new LatestPublishingAuthorityLoad());
  const hasLoadedAuthority = useRef(false);
  const [authorityCurrent, setAuthorityCurrent] = useState(false);

  const load = useCallback(async () => {
    const request = publishingLoad.current.begin();
    setRefreshing(true);
    setAuthorityCurrent(false);
    setAccountEditor(null);
    setTargetEditor(null);
    try {
      const [nextAccounts, nextTargets, session, report, dashboard] = await Promise.all([
        apiRequest<PublishTargetAccount[]>("/api/v1/publishing/accounts"),
        apiRequest<PublishTargetProfile[]>("/api/v1/publishing/targets"),
        apiRequest<OperatorSession>("/api/v1/auth/session"),
        apiRequest<AuthoritativeCommissioningReport>("/api/v1/commissioning/report"),
        apiRequest<{ by_asset?: Array<{ asset_id: string; media_path: string; duration_ms?: number; readiness_state?: string }> }>("/api/v1/assets/readiness-dashboard"),
      ]);
      if (!publishingLoad.current.accepts(request)) return;
      const [accountHistories, targetHistories, executionHistories] = await Promise.all([
        Promise.all(nextAccounts.map(async (account) => [account.account_id, await apiRequest<AccountHistory[]>(publishingHistoryPath("accounts", account.account_id))] as const)),
        Promise.all(nextTargets.map(async (target) => [target.target_id, await apiRequest<TargetHistory[]>(publishingHistoryPath("targets", target.target_id))] as const)),
        Promise.all(nextTargets.map(async (target) => {
          const path = publishingExecutionHistoryPath(target.target_id);
          const listed = path ? await apiRequest<PublishExecutionJobResponse[]>(path) : [];
          const detailed = await Promise.all(listed.map(async (job) => {
            const detailPath = publishingExecutionJobPath(target.target_id, job.job.job_id);
            return detailPath ? apiRequest<PublishExecutionJobResponse>(detailPath) : job;
          }));
          return [target.target_id, detailed] as const;
        })),
      ]);
      if (!publishingLoad.current.accepts(request)) return;
      setAccounts(nextAccounts);
      setTargets(nextTargets);
      setAccountHistory(Object.fromEntries(accountHistories));
      setTargetHistory(Object.fromEntries(targetHistories));
      setExecutionJobs(Object.fromEntries(executionHistories));
      setCatalogAssets(readyAssets.length ? readyAssets : (dashboard.by_asset ?? []).filter((asset) => asset.readiness_state === "ready"));
      setReadiness(report.checks.find((check) => check.id === "publish_targets") as PublicationReadinessCheck | undefined ?? null);
      setSetupAdmin(session.session.roles.includes("setup_admin"));
      setPublishOperator(session.session.roles.includes("publish_operator"));
      setAuthorityCurrent(true);
      hasLoadedAuthority.current = true;
      setError(null); setHistoryError(null);
    } catch (problem) {
      if (!publishingLoad.current.accepts(request)) return;
      setError(problem instanceof Error ? problem.message : "Publishing authority could not be read.");
      if (hasLoadedAuthority.current) setHistoryError("The shown publishing authority may be stale. Refresh before editing.");
    } finally {
      if (publishingLoad.current.accepts(request)) { setLoading(false); setRefreshing(false); }
    }
  }, []);

  useEffect(() => {
    void load();
    return () => publishingLoad.current.invalidate();
  }, [load]);

  function beginAccount(account: PublishTargetAccount | null) {
    setTargetEditor(null); setAccountEditor(account ?? "new"); setError(null); setNotice(null);
    setAccountDraft(account ? {
      kind: account.kind, label: account.label, enabled: account.enabled,
    } : EMPTY_ACCOUNT);
  }

  function beginTarget(target: PublishTargetProfile | null) {
    setAccountEditor(null); setTargetEditor(target ?? "new"); setError(null); setNotice(null);
    const selected = target ?? null;
    setTargetDraft(selected ? {
      accountId: selected.account_id, kind: selected.kind, label: selected.label, enabled: selected.enabled, configuration: emptyPublishingConfiguration(selected.kind),
    } : { ...EMPTY_TARGET, accountId: accounts[0]?.account_id ?? "", kind: accounts[0]?.kind ?? "portal", configuration: emptyPublishingConfiguration(accounts[0]?.kind ?? "portal") });
  }

  async function saveAccount(event: FormEvent) {
    event.preventDefault();
    const existing = accountEditor !== "new" ? accountEditor : null;
    const blocked = publishingAccountMutationReason(accountDraft, existing?.revision ?? null);
    if (blocked || !setupAdmin || !authorityCurrent) { setError(blocked ?? "Refresh current publishing authority before saving."); return; }
    setSaving(true); setError(null); setNotice(null);
    try {
      const accountId = existing?.account_id ?? crypto.randomUUID();
      await apiRequest(`/api/v1/publishing/accounts/${encodeURIComponent(accountId)}`, {
        method: "PUT", body: JSON.stringify(publishAccountMutation(accountDraft, existing?.revision ?? null)),
      });
      setAccountEditor(null); setNotice(`Publishing account ${existing ? "revision saved" : "created"}. Trusted connection evidence remains server-owned.`);
      await load();
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "The publishing account could not be saved. Refresh before retrying if authority changed.");
    } finally { setSaving(false); }
  }

  async function saveTarget(event: FormEvent) {
    event.preventDefault();
    const existing = targetEditor !== "new" ? targetEditor : null;
    const blocked = publishingTargetMutationReason(targetDraft, existing?.revision ?? null);
    if (blocked || !setupAdmin || !authorityCurrent) { setError(blocked ?? "Refresh current publishing authority before saving."); return; }
    setSaving(true); setError(null); setNotice(null);
    try {
      const targetId = existing?.target_id ?? crypto.randomUUID();
      await apiRequest(`/api/v1/publishing/targets/${encodeURIComponent(targetId)}`, {
        method: "PUT", body: JSON.stringify(publishTargetMutation(targetDraft, existing?.revision ?? null)),
      });
      setTargetEditor(null); setNotice(`Publishing target ${existing ? "revision saved" : "created"}. A saved target is not a delivery, publication, or audience-reach claim.`);
      await load();
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "The publishing target could not be saved. Refresh before retrying if authority changed.");
    } finally { setSaving(false); }
  }

  async function saveCredential(event: FormEvent) {
    event.preventDefault();
    if (!credentialAccount || credential.length < 8 || credential.length > 16384 || !canWrite) return;
    setSaving(true); setError(null); setNotice(null);
    try {
      await apiRequest(`/api/v1/publishing/accounts/${encodeURIComponent(credentialAccount.account_id)}/credential`, {
        method: "PUT",
        body: JSON.stringify({ expected_account_revision: credentialAccount.revision, credential }),
      });
      setCredential(""); setCredentialAccount(null);
      setNotice("Publishing credential stored with Windows machine protection. Its value cannot be read back.");
      await load();
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "The publishing credential could not be stored.");
    } finally { setSaving(false); }
  }

  async function deleteCredential(account: PublishTargetAccount) {
    setSaving(true); setError(null); setNotice(null);
    try {
      await apiRequest(`/api/v1/publishing/accounts/${encodeURIComponent(account.account_id)}/credential`, {
        method: "DELETE",
        body: JSON.stringify({ expected_account_revision: account.revision }),
      });
      setNotice("Publishing credential deleted. Existing execution evidence cannot authorize new work.");
      await load();
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "The publishing credential could not be deleted.");
    } finally { setSaving(false); }
  }

  async function requestExecution(target: PublishTargetProfile) {
    const unavailable = publishingExecutionUnavailableReason(target.kind);
    if (unavailable) { setNotice(unavailable); return; }
    const assetId = selectedAssets[target.target_id] ?? catalogAssets[0]?.asset_id;
    const path = assetId ? publishingExecutionRequestPath(target.target_id, assetId) : null;
    if (!path || !canPublish) return;
    setSaving(true); setError(null); setNotice(null);
    try {
      await apiRequest<PublishExecutionJobResponse>(path, { method: "POST" });
      setNotice(`Publication job queued for ${target.label}. The job records provider acknowledgements; it does not assert audience reach.`);
      await load();
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "The publication job could not be queued. Refresh before retrying.");
    } finally { setSaving(false); }
  }

  const accountById = useMemo(() => new Map(accounts.map((account) => [account.account_id, account])), [accounts]);
  const canWrite = setupAdmin && authorityCurrent && !refreshing;
  const canPublish = (setupAdmin || publishOperator) && authorityCurrent && !refreshing;
  const accountBlock = publishingAccountMutationReason(accountDraft, accountEditor && accountEditor !== "new" ? accountEditor.revision : null);
  const targetBlock = publishingTargetMutationReason(targetDraft, targetEditor && targetEditor !== "new" ? targetEditor.revision : null);

  if (loading && !accounts.length && !targets.length) return <>
    <header className="page-header"><div><p className="eyebrow">Publication authority</p><h1>Reading publishing authority…</h1><p>The console is reading redacted, server-owned account and target records.</p></div></header>
    <section className="panel publishing-loading" aria-live="polite"><div className="boot-line"><i /></div><p>Checking current account, target, history, and connection authority…</p></section>
  </>;

  return <>
    <header className="page-header"><div><p className="eyebrow">Publication authority</p><h1>Publishing accounts and targets</h1><p>These records establish configuration authority. Connection tests are separate, server-trusted evidence; neither proves publication, delivery, or audience reach.</p></div><div className="header-actions"><button className="secondary" onClick={() => void load()} disabled={refreshing || saving}>{refreshing ? "Refreshing…" : "Refresh authority"}</button></div></header>
    {error && <div className="error-box" role="alert">{error}</div>}
    {historyError && <div className="headend-warning" role="status">{historyError}</div>}
    {notice && <div className="notice" role="status">{notice}</div>}
    {!setupAdmin && !publishOperator && !error && <div className="headend-warning" role="status">Read-only view. Setup Admin is required to revise authority; Publish Operator or Setup Admin may queue execution.</div>}
    {setupAdmin && !authorityCurrent && !error && <div className="headend-warning" role="status">Publishing authority is refreshing. Editing stays disabled until the newest server response and history are current.</div>}
    <section className="panel publishing-readiness" aria-label="Server publication readiness">
      <div className="panel-head"><div><p className="eyebrow">Authoritative commissioning state</p><h2>Connection readiness</h2></div>{readiness && <span className={`status ${statusClass(readiness.status)}`}>{publishingReadinessLabel(readiness.status)}</span>}</div>
      {readiness ? <><p>{readiness.detail}</p><small>{readiness.evidence || "No trusted connection authority detail was reported."}</small></> : <div className="publishing-empty"><strong>Readiness is unavailable</strong><p>The server did not return its Required publish targets commissioning check. This browser will not infer readiness.</p></div>}
      <p className="publishing-nonclaim">A current connection test verifies only the configured target connection authority for the installed candidate. It does not claim a published item, successful delivery, a receiver accepting content, or audience reach.</p>
    </section>
    <div className="publishing-layout">
      <section className="panel"><div className="panel-head"><div><p className="eyebrow">Redacted account authority</p><h2>Accounts</h2></div><div className="panel-actions"><span className="count">{accounts.length}</span>{setupAdmin && <button className="secondary" onClick={() => beginAccount(null)} disabled={saving || accountEditor !== null || !canWrite}>Add account</button>}</div></div>
        {!accounts.length ? <div className="publishing-empty"><strong>No publishing accounts configured</strong><p>Create a bounded account record before linking a target.</p>{setupAdmin && <button className="primary" onClick={() => beginAccount(null)} disabled={!canWrite}>Create account</button>}</div> : <div className="publishing-record-list">{accounts.map((account) => <article className="publishing-record" key={account.account_id}><div className="publishing-record-head"><div><strong>{account.label}</strong><small>{publishingKindLabel(account.kind)} · revision {account.revision}</small></div><span className={`status ${account.enabled ? "pass" : "unknown"}`}>{account.enabled ? "enabled" : "disabled"}</span></div><dl><div><dt>Credential</dt><dd>{account.credential_configured ? "Configured (secret redacted)" : "Not configured"}</dd></div><div><dt>Account identity</dt><dd><code>{account.account_id}</code></dd></div></dl>{setupAdmin && <><button className="text-button" onClick={() => beginAccount(account)} disabled={saving || !canWrite}>Edit account</button><button className="text-button" onClick={() => { setCredential(""); setCredentialAccount(account); }} disabled={saving || !canWrite}>{account.credential_configured ? "Rotate credential" : "Provision credential"}</button>{account.credential_configured && <button className="text-button" onClick={() => void deleteCredential(account)} disabled={saving || !canWrite}>Delete credential</button>}</>}<History title="Account history" entries={accountHistory[account.account_id] ?? []} render={(entry) => <>{entry.account.label} · revision {entry.account.revision} · {entry.account.enabled ? "enabled" : "disabled"}</>} /></article>)}</div>}
        {credentialAccount && <form className="publishing-editor" onSubmit={saveCredential}><div className="panel-head"><div><p className="eyebrow">Write-only secret</p><h3>{credentialAccount.credential_configured ? "Rotate" : "Provision"} {credentialAccount.label} credential</h3></div><button type="button" className="text-button" onClick={() => { setCredential(""); setCredentialAccount(null); }}>Cancel</button></div><label>Provider credential<textarea required minLength={8} maxLength={16384} value={credential} onChange={(event) => setCredential(event.target.value)} autoComplete="off" spellCheck={false} /></label><p className="publishing-nonclaim">The browser sends this once over the authenticated local API. It is Windows-protected and can never be displayed again.</p><button className="primary" disabled={saving || credential.length < 8}>{saving ? "Protecting…" : "Store credential"}</button></form>}
        {accountEditor && <AccountEditor editor={accountEditor} draft={accountDraft} blocked={accountBlock} saving={saving} onDraft={setAccountDraft} onCancel={() => setAccountEditor(null)} onSave={saveAccount} />}
      </section>
      <section className="panel"><div className="panel-head"><div><p className="eyebrow">Redacted target authority</p><h2>Targets</h2></div><div className="panel-actions"><span className="count">{targets.length}</span>{setupAdmin && <button className="secondary" onClick={() => beginTarget(null)} disabled={saving || targetEditor !== null || !accounts.length || !canWrite}>Add target</button>}</div></div>
        {!targets.length ? <div className="publishing-empty"><strong>No publishing targets configured</strong><p>Targets remain intentionally inert until a trusted, current connection test is recorded outside this browser.</p>{setupAdmin && accounts.length > 0 && <button className="primary" onClick={() => beginTarget(null)} disabled={!canWrite}>Create target</button>}</div> : <div className="publishing-record-list">{targets.map((target) => { const account = accountById.get(target.account_id); const jobs = executionJobs[target.target_id] ?? []; const selectedAssetId = selectedAssets[target.target_id] ?? catalogAssets[0]?.asset_id ?? ""; const executionUnavailable = publishingExecutionUnavailableReason(target.kind); return <article className="publishing-record" key={target.target_id}><div className="publishing-record-head"><div><strong>{target.label}</strong><small>{publishingKindLabel(target.kind)} · revision {target.revision}</small></div><span className={`status ${target.enabled ? "pass" : "unknown"}`}>{target.enabled ? "enabled" : "disabled"}</span></div><dl><div><dt>Linked account</dt><dd>{account?.label ?? "Missing account"}</dd></div><div><dt>Configuration fingerprint</dt><dd><code>{redactConfigurationHash(target.configuration_hash)}</code></dd></div><div><dt>Target identity</dt><dd><code>{target.target_id}</code></dd></div></dl>{setupAdmin && <button className="text-button" onClick={() => beginTarget(target)} disabled={saving || !canWrite}>Edit target</button>}<div className="publishing-execution"><div><p className="eyebrow">Provider execution</p><strong>{executionUnavailable ? "Publication unavailable" : "Queue a bounded publication job"}</strong><p className="publishing-nonclaim">The server rechecks current media, credential, target, and installed-candidate authority. A provider acknowledgement is recorded stage by stage; it is not proof of audience reach.</p></div>{executionUnavailable ? <p className="headend-warning" role="status">{executionUnavailable}</p> : catalogAssets.length ? <div className="publishing-execution-controls"><label>Ready media<select value={selectedAssetId} onChange={(event) => setSelectedAssets((current) => ({ ...current, [target.target_id]: event.target.value }))}>{catalogAssets.map((asset) => <option key={asset.asset_id} value={asset.asset_id}>{asset.media_path.split(/[\\/]/).pop() ?? asset.asset_id.slice(0, 12) + "…"}{asset.duration_ms ? ` · ${Math.round(asset.duration_ms / 1000)}s` : ""}</option>)}</select></label><button className="primary" onClick={() => void requestExecution(target)} disabled={!canPublish || saving || !target.enabled}>{saving ? "Queuing…" : "Queue publication"}</button></div> : <p className="muted">No ready media is available. Import and verify an asset before queuing provider execution.</p>}{jobs.length > 0 && <ExecutionHistory jobs={jobs} />}</div><History title="Target history" entries={targetHistory[target.target_id] ?? []} render={(entry) => <>{entry.target.label} · revision {entry.target.revision} · {entry.target.enabled ? "enabled" : "disabled"} · configuration {redactConfigurationHash(entry.target.configuration_hash)}</>} /></article>; })}</div>}
        {targetEditor && <TargetEditor editor={targetEditor} draft={targetDraft} accounts={accounts} blocked={targetBlock} saving={saving} onDraft={setTargetDraft} onCancel={() => setTargetEditor(null)} onSave={saveTarget} />}
      </section>
    </div>
  </>;
}

function History<T extends { changed_by: string; occurred_at_unix_ms: number }>({ title, entries, render }: { title: string; entries: T[]; render: (entry: T) => ReactNode }) {
  return <details className="publishing-history"><summary>{title} ({entries.length})</summary>{entries.length ? <ol>{entries.map((entry, index) => <li key={`${entry.occurred_at_unix_ms}-${index}`}><span>{render(entry)}</span><small>{time(entry.occurred_at_unix_ms)} · changed by {entry.changed_by}</small></li>)}</ol> : <p>No history was returned for this record.</p>}</details>;
}

function ExecutionHistory({ jobs }: { jobs: PublishExecutionJobResponse[] }) {
  return <details className="publishing-history"><summary>Execution history ({jobs.length})</summary><ol>{jobs.map((item) => <li key={item.job.job_id}><span><strong>{publishingJobStateLabel(item.state)}</strong> · {item.job.media_sha256.slice(0, 12)}… · attempt {item.attempts}</span><small>{item.failure_class ? `Failure: ${item.failure_class} · ` : ""}{item.receipts.length ? item.receipts.map((receipt) => publishingStageLabel(receipt.stage)).join(" → ") : "No provider acknowledgement recorded yet."}</small>{item.receipts.length > 0 && <ul>{item.receipts.map((receipt) => <li key={receipt.receipt_id}><span>{publishingStageLabel(receipt.stage)}: {receipt.detail}</span><small>{time(receipt.occurred_at_unix_ms)} · acknowledgement fingerprint {receipt.delivery_identity_sha256.slice(0, 12)}…</small></li>)}</ul>}</li>)}</ol><p className="publishing-nonclaim">These are TownLight's durable execution receipts, bound to one candidate, target revision, credential revision, and media digest. They do not claim playback, retention, or audience reach.</p></details>;
}

function AccountEditor({ editor, draft, blocked, saving, onDraft, onCancel, onSave }: { editor: PublishTargetAccount | "new"; draft: PublishAccountDraft; blocked: string | null; saving: boolean; onDraft: (value: PublishAccountDraft) => void; onCancel: () => void; onSave: (event: FormEvent) => void }) {
  return <form className="publishing-editor" onSubmit={onSave}><div className="panel-head"><div><p className="eyebrow">{editor === "new" ? "New account" : `Revision ${editor.revision} → ${editor.revision + 1}`}</p><h3>{editor === "new" ? "Create publishing account" : "Edit publishing account"}</h3></div><button type="button" className="text-button" onClick={onCancel}>Cancel</button></div><label>Account label<input required maxLength={200} value={draft.label} onChange={(event) => onDraft({ ...draft, label: event.target.value })} placeholder="County public information portal" /></label><label>Target kind<select value={draft.kind} onChange={(event) => onDraft({ ...draft, kind: event.target.value as PublishAccountDraft["kind"] })}>{PUBLISH_TARGET_KINDS.map((kind) => <option key={kind} value={kind}>{publishingKindLabel(kind)}</option>)}</select></label><label className="publishing-checkbox"><input type="checkbox" checked={draft.enabled} onChange={(event) => onDraft({ ...draft, enabled: event.target.checked })} /> Enable this account</label><p className="publishing-nonclaim">Credential presence is derived from the protected station secret authority and cannot be set here.</p>{blocked && <div className="headend-warning" role="status">{blocked}</div>}<button className="primary" disabled={saving || Boolean(blocked)}>{saving ? "Saving…" : editor === "new" ? "Create account" : "Save account revision"}</button></form>;
}

function TargetEditor({ editor, draft, accounts, blocked, saving, onDraft, onCancel, onSave }: { editor: PublishTargetProfile | "new"; draft: PublishTargetDraft; accounts: PublishTargetAccount[]; blocked: string | null; saving: boolean; onDraft: (value: PublishTargetDraft) => void; onCancel: () => void; onSave: (event: FormEvent) => void }) {
  function selectAccount(accountId: string) { const account = accounts.find((item) => item.account_id === accountId); const kind = account?.kind ?? draft.kind; onDraft({ ...draft, accountId, kind, configuration: emptyPublishingConfiguration(kind) }); }
  function setConfigurationField(field: string, value: string) { onDraft({ ...draft, configuration: { ...draft.configuration, [field]: value } as PublishExecutionTargetConfig }); }
  const configuration = draft.configuration;
  return <form className="publishing-editor" onSubmit={onSave}><div className="panel-head"><div><p className="eyebrow">{editor === "new" ? "New target" : `Revision ${editor.revision} → ${editor.revision + 1}`}</p><h3>{editor === "new" ? "Create publishing target" : "Edit publishing target"}</h3></div><button type="button" className="text-button" onClick={onCancel}>Cancel</button></div><label>Linked account<select required value={draft.accountId} onChange={(event) => selectAccount(event.target.value)}><option value="">Select an account</option>{accounts.map((account) => <option key={account.account_id} value={account.account_id}>{account.label} · {publishingKindLabel(account.kind)}</option>)}</select></label><label>Target label<input required maxLength={200} value={draft.label} onChange={(event) => onDraft({ ...draft, label: event.target.value })} placeholder="Municipal meeting archive" /></label><label>Target kind<input value={publishingKindLabel(draft.kind)} readOnly aria-readonly="true" /><small>Kind is bound to the selected account and cannot be rebound by this browser.</small></label>{configuration.kind === "portal" && <label>Portal HTTPS endpoint<input required type="url" value={configuration.endpoint} onChange={(event) => setConfigurationField("endpoint", event.target.value)} placeholder="https://portal.example.gov/publish" /><small>HTTPS only. Credentials, query strings, fragments, and trailing slash are rejected.</small></label>}{configuration.kind === "internet_archive" && <label>Internet Archive item identifier<input required maxLength={100} value={configuration.item_identifier} onChange={(event) => setConfigurationField("item_identifier", event.target.value)} placeholder="county-council-2026-08-26" /></label>}{configuration.kind === "local_nas" && <label>Managed NAS root<input required maxLength={500} value={configuration.managed_root} onChange={(event) => setConfigurationField("managed_root", event.target.value)} placeholder="\\\\SERVER\\TownLight\\Published" /><small>Absolute UNC path only; do not include traversal.</small></label>}{configuration.kind === "youtube_live" && <><label>YouTube channel ID<input required maxLength={100} value={configuration.channel_id} onChange={(event) => setConfigurationField("channel_id", event.target.value)} /></label><label>YouTube broadcast ID<input required maxLength={100} value={configuration.broadcast_id} onChange={(event) => setConfigurationField("broadcast_id", event.target.value)} /></label><label>YouTube stream ID<input required maxLength={100} value={configuration.stream_id} onChange={(event) => setConfigurationField("stream_id", event.target.value)} /></label><label>YouTube privacy<select value={configuration.privacy_status} onChange={(event) => setConfigurationField("privacy_status", event.target.value)}><option value="unlisted">Unlisted</option><option value="public">Public</option></select><small>TownLight records provider acknowledgement; it does not claim audience reach.</small></label></>}{blocked && <div className="headend-warning" role="status">{blocked}</div>}<p className="publishing-nonclaim">Saving creates a revisioned typed authority record. It does not test a connection or assert delivery, publication, receiver acceptance, or reach.</p><button className="primary" disabled={saving || Boolean(blocked)}>{saving ? "Saving…" : editor === "new" ? "Create target" : "Save target revision"}</button></form>;
}
