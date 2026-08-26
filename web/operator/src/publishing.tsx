import { FormEvent, ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiRequest } from "./api-client";
import type { AuthoritativeCommissioningReport } from "./commissioning-contract";
import {
  PUBLISH_TARGET_KINDS,
  publishAccountMutation,
  publishTargetMutation,
  LatestPublishingAuthorityLoad,
  publishingAccountMutationReason,
  publishingHistoryPath,
  publishingKindLabel,
  publishingReadinessLabel,
  publishingTargetMutationReason,
  redactConfigurationHash,
  type PublicationReadinessCheck,
  type PublishAccountDraft,
  type PublishTargetAccount,
  type PublishTargetDraft,
  type PublishTargetProfile,
} from "./publishing-contract";

interface OperatorSession { session: { roles: string[] } }
interface AccountHistory { account: PublishTargetAccount; changed_by: string; occurred_at_unix_ms: number }
interface TargetHistory { target: PublishTargetProfile; changed_by: string; occurred_at_unix_ms: number }

const EMPTY_ACCOUNT: PublishAccountDraft = { kind: "portal", label: "", enabled: true, credentialConfigured: false };
const EMPTY_TARGET: PublishTargetDraft = { accountId: "", kind: "portal", label: "", enabled: true, configurationHash: "" };

function time(value: number): string { return new Date(value).toLocaleString(); }
function statusClass(status: PublicationReadinessCheck["status"] | undefined): string {
  return status === "pass" ? "pass" : status === "blocked" ? "blocked" : "unknown";
}

export function PublishingPanel() {
  const [accounts, setAccounts] = useState<PublishTargetAccount[]>([]);
  const [targets, setTargets] = useState<PublishTargetProfile[]>([]);
  const [accountHistory, setAccountHistory] = useState<Record<string, AccountHistory[]>>({});
  const [targetHistory, setTargetHistory] = useState<Record<string, TargetHistory[]>>({});
  const [readiness, setReadiness] = useState<PublicationReadinessCheck | null>(null);
  const [setupAdmin, setSetupAdmin] = useState(false);
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
      const [nextAccounts, nextTargets, session, report] = await Promise.all([
        apiRequest<PublishTargetAccount[]>("/api/v1/publishing/accounts"),
        apiRequest<PublishTargetProfile[]>("/api/v1/publishing/targets"),
        apiRequest<OperatorSession>("/api/v1/auth/session"),
        apiRequest<AuthoritativeCommissioningReport>("/api/v1/commissioning/report"),
      ]);
      if (!publishingLoad.current.accepts(request)) return;
      const [accountHistories, targetHistories] = await Promise.all([
        Promise.all(nextAccounts.map(async (account) => [account.account_id, await apiRequest<AccountHistory[]>(publishingHistoryPath("accounts", account.account_id))] as const)),
        Promise.all(nextTargets.map(async (target) => [target.target_id, await apiRequest<TargetHistory[]>(publishingHistoryPath("targets", target.target_id))] as const)),
      ]);
      if (!publishingLoad.current.accepts(request)) return;
      setAccounts(nextAccounts);
      setTargets(nextTargets);
      setAccountHistory(Object.fromEntries(accountHistories));
      setTargetHistory(Object.fromEntries(targetHistories));
      setReadiness(report.checks.find((check) => check.id === "publish_targets") as PublicationReadinessCheck | undefined ?? null);
      setSetupAdmin(session.session.roles.includes("setup_admin"));
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
      kind: account.kind, label: account.label, enabled: account.enabled, credentialConfigured: account.credential_configured,
    } : EMPTY_ACCOUNT);
  }

  function beginTarget(target: PublishTargetProfile | null) {
    setAccountEditor(null); setTargetEditor(target ?? "new"); setError(null); setNotice(null);
    const selected = target ?? null;
    setTargetDraft(selected ? {
      accountId: selected.account_id, kind: selected.kind, label: selected.label, enabled: selected.enabled, configurationHash: selected.configuration_hash,
    } : { ...EMPTY_TARGET, accountId: accounts[0]?.account_id ?? "", kind: accounts[0]?.kind ?? "portal" });
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

  const accountById = useMemo(() => new Map(accounts.map((account) => [account.account_id, account])), [accounts]);
  const canWrite = setupAdmin && authorityCurrent && !refreshing;
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
    {!setupAdmin && !error && <div className="headend-warning" role="status">Read-only view. An enabled Setup Admin account is required to create or revise publishing authority.</div>}
    {setupAdmin && !authorityCurrent && !error && <div className="headend-warning" role="status">Publishing authority is refreshing. Editing stays disabled until the newest server response and history are current.</div>}
    <section className="panel publishing-readiness" aria-label="Server publication readiness">
      <div className="panel-head"><div><p className="eyebrow">Authoritative commissioning state</p><h2>Connection readiness</h2></div>{readiness && <span className={`status ${statusClass(readiness.status)}`}>{publishingReadinessLabel(readiness.status)}</span>}</div>
      {readiness ? <><p>{readiness.detail}</p><small>{readiness.evidence || "No trusted connection authority detail was reported."}</small></> : <div className="publishing-empty"><strong>Readiness is unavailable</strong><p>The server did not return its Required publish targets commissioning check. This browser will not infer readiness.</p></div>}
      <p className="publishing-nonclaim">A current connection test verifies only the configured target connection authority for the installed candidate. It does not claim a published item, successful delivery, a receiver accepting content, or audience reach.</p>
    </section>
    <div className="publishing-layout">
      <section className="panel"><div className="panel-head"><div><p className="eyebrow">Redacted account authority</p><h2>Accounts</h2></div><div className="panel-actions"><span className="count">{accounts.length}</span>{setupAdmin && <button className="secondary" onClick={() => beginAccount(null)} disabled={saving || accountEditor !== null || !canWrite}>Add account</button>}</div></div>
        {!accounts.length ? <div className="publishing-empty"><strong>No publishing accounts configured</strong><p>Create a bounded account record before linking a target. Credential material is never entered or displayed here.</p>{setupAdmin && <button className="primary" onClick={() => beginAccount(null)} disabled={!canWrite}>Create account</button>}</div> : <div className="publishing-record-list">{accounts.map((account) => <article className="publishing-record" key={account.account_id}><div className="publishing-record-head"><div><strong>{account.label}</strong><small>{publishingKindLabel(account.kind)} · revision {account.revision}</small></div><span className={`status ${account.enabled ? "pass" : "unknown"}`}>{account.enabled ? "enabled" : "disabled"}</span></div><dl><div><dt>Credential</dt><dd>{account.credential_configured ? "Configured (secret redacted)" : "Not configured"}</dd></div><div><dt>Account identity</dt><dd><code>{account.account_id}</code></dd></div></dl>{setupAdmin && <button className="text-button" onClick={() => beginAccount(account)} disabled={saving || !canWrite}>Edit account</button>}<History title="Account history" entries={accountHistory[account.account_id] ?? []} render={(entry) => <>{entry.account.label} · revision {entry.account.revision} · {entry.account.enabled ? "enabled" : "disabled"} · credential {entry.account.credential_configured ? "configured" : "not configured"}</>} /></article>)}</div>}
        {accountEditor && <AccountEditor editor={accountEditor} draft={accountDraft} blocked={accountBlock} saving={saving} onDraft={setAccountDraft} onCancel={() => setAccountEditor(null)} onSave={saveAccount} />}
      </section>
      <section className="panel"><div className="panel-head"><div><p className="eyebrow">Redacted target authority</p><h2>Targets</h2></div><div className="panel-actions"><span className="count">{targets.length}</span>{setupAdmin && <button className="secondary" onClick={() => beginTarget(null)} disabled={saving || targetEditor !== null || !accounts.length || !canWrite}>Add target</button>}</div></div>
        {!targets.length ? <div className="publishing-empty"><strong>No publishing targets configured</strong><p>Targets remain intentionally inert until a trusted, current connection test is recorded outside this browser.</p>{setupAdmin && accounts.length > 0 && <button className="primary" onClick={() => beginTarget(null)} disabled={!canWrite}>Create target</button>}</div> : <div className="publishing-record-list">{targets.map((target) => { const account = accountById.get(target.account_id); return <article className="publishing-record" key={target.target_id}><div className="publishing-record-head"><div><strong>{target.label}</strong><small>{publishingKindLabel(target.kind)} · revision {target.revision}</small></div><span className={`status ${target.enabled ? "pass" : "unknown"}`}>{target.enabled ? "enabled" : "disabled"}</span></div><dl><div><dt>Linked account</dt><dd>{account?.label ?? "Missing account"}</dd></div><div><dt>Configuration fingerprint</dt><dd><code>{redactConfigurationHash(target.configuration_hash)}</code></dd></div><div><dt>Target identity</dt><dd><code>{target.target_id}</code></dd></div></dl>{setupAdmin && <button className="text-button" onClick={() => beginTarget(target)} disabled={saving || !canWrite}>Edit target</button>}<History title="Target history" entries={targetHistory[target.target_id] ?? []} render={(entry) => <>{entry.target.label} · revision {entry.target.revision} · {entry.target.enabled ? "enabled" : "disabled"} · configuration {redactConfigurationHash(entry.target.configuration_hash)}</>} /></article>; })}</div>}
        {targetEditor && <TargetEditor editor={targetEditor} draft={targetDraft} accounts={accounts} blocked={targetBlock} saving={saving} onDraft={setTargetDraft} onCancel={() => setTargetEditor(null)} onSave={saveTarget} />}
      </section>
    </div>
  </>;
}

function History<T extends { changed_by: string; occurred_at_unix_ms: number }>({ title, entries, render }: { title: string; entries: T[]; render: (entry: T) => ReactNode }) {
  return <details className="publishing-history"><summary>{title} ({entries.length})</summary>{entries.length ? <ol>{entries.map((entry, index) => <li key={`${entry.occurred_at_unix_ms}-${index}`}><span>{render(entry)}</span><small>{time(entry.occurred_at_unix_ms)} · changed by {entry.changed_by}</small></li>)}</ol> : <p>No history was returned for this record.</p>}</details>;
}

function AccountEditor({ editor, draft, blocked, saving, onDraft, onCancel, onSave }: { editor: PublishTargetAccount | "new"; draft: PublishAccountDraft; blocked: string | null; saving: boolean; onDraft: (value: PublishAccountDraft) => void; onCancel: () => void; onSave: (event: FormEvent) => void }) {
  return <form className="publishing-editor" onSubmit={onSave}><div className="panel-head"><div><p className="eyebrow">{editor === "new" ? "New account" : `Revision ${editor.revision} → ${editor.revision + 1}`}</p><h3>{editor === "new" ? "Create publishing account" : "Edit publishing account"}</h3></div><button type="button" className="text-button" onClick={onCancel}>Cancel</button></div><label>Account label<input required maxLength={200} value={draft.label} onChange={(event) => onDraft({ ...draft, label: event.target.value })} placeholder="County public information portal" /></label><label>Target kind<select value={draft.kind} onChange={(event) => onDraft({ ...draft, kind: event.target.value as PublishAccountDraft["kind"] })}>{PUBLISH_TARGET_KINDS.map((kind) => <option key={kind} value={kind}>{publishingKindLabel(kind)}</option>)}</select></label><label className="publishing-checkbox"><input type="checkbox" checked={draft.enabled} onChange={(event) => onDraft({ ...draft, enabled: event.target.checked })} /> Enable this account</label><label className="publishing-checkbox"><input type="checkbox" checked={draft.credentialConfigured} onChange={(event) => onDraft({ ...draft, credentialConfigured: event.target.checked })} /> Credential configured (presence only; no secret is entered here)</label>{blocked && <div className="headend-warning" role="status">{blocked}</div>}<button className="primary" disabled={saving || Boolean(blocked)}>{saving ? "Saving…" : editor === "new" ? "Create account" : "Save account revision"}</button></form>;
}

function TargetEditor({ editor, draft, accounts, blocked, saving, onDraft, onCancel, onSave }: { editor: PublishTargetProfile | "new"; draft: PublishTargetDraft; accounts: PublishTargetAccount[]; blocked: string | null; saving: boolean; onDraft: (value: PublishTargetDraft) => void; onCancel: () => void; onSave: (event: FormEvent) => void }) {
  function selectAccount(accountId: string) { const account = accounts.find((item) => item.account_id === accountId); onDraft({ ...draft, accountId, kind: account?.kind ?? draft.kind }); }
  return <form className="publishing-editor" onSubmit={onSave}><div className="panel-head"><div><p className="eyebrow">{editor === "new" ? "New target" : `Revision ${editor.revision} → ${editor.revision + 1}`}</p><h3>{editor === "new" ? "Create publishing target" : "Edit publishing target"}</h3></div><button type="button" className="text-button" onClick={onCancel}>Cancel</button></div><label>Linked account<select required value={draft.accountId} onChange={(event) => selectAccount(event.target.value)}><option value="">Select an account</option>{accounts.map((account) => <option key={account.account_id} value={account.account_id}>{account.label} · {publishingKindLabel(account.kind)}</option>)}</select></label><label>Target label<input required maxLength={200} value={draft.label} onChange={(event) => onDraft({ ...draft, label: event.target.value })} placeholder="Municipal meeting archive" /></label><label>Target kind<input value={publishingKindLabel(draft.kind)} readOnly aria-readonly="true" /><small>Kind is bound to the selected account and cannot be rebound by this browser.</small></label><label>Configuration SHA-256<input required minLength={64} maxLength={64} value={draft.configurationHash} onChange={(event) => onDraft({ ...draft, configurationHash: event.target.value })} placeholder="64 lowercase hexadecimal characters" spellCheck={false} /><small>Configuration fingerprint only. Do not paste credentials, access tokens, or evidence.</small></label><label className="publishing-checkbox"><input type="checkbox" checked={draft.enabled} onChange={(event) => onDraft({ ...draft, enabled: event.target.checked })} /> Enable this target</label>{blocked && <div className="headend-warning" role="status">{blocked}</div>}<p className="publishing-nonclaim">Saving creates only a revisioned authority record. It does not test a connection or assert delivery, publication, receiver acceptance, or reach.</p><button className="primary" disabled={saving || Boolean(blocked)}>{saving ? "Saving…" : editor === "new" ? "Create target" : "Save target revision"}</button></form>;
}
