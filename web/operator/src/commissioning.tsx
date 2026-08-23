import { useCallback, useEffect, useState } from "react";
import {
  commissioningAction,
  commissioningGroups,
  commissioningStatusLabel,
  currentCommissioningCheck,
  isCommissioningComplete,
  isCoreCommissioningComplete,
  isExternalCommissioningCheck,
} from "./commissioning-contract";
import type {
  AuthoritativeCommissioningCheck,
  AuthoritativeCommissioningReport,
  CommissioningAction,
  CommissioningStatus,
} from "./commissioning-contract";

type CommissioningDestination = "health" | "media" | "channels" | "schedule";

interface StationBoxDimension {
  name: string;
  state: "pass" | "recoverable" | "unknown" | "fail";
  required: boolean;
  detail: string;
  evidence: string;
  next_step: string | null;
}

interface StationBoxProfile {
  engine_tier_verdict: string;
  peg_readiness: {
    color: "green" | "yellow" | "red";
    dimensions: StationBoxDimension[];
  };
}

interface CommissioningProps {
  onNavigate: (view: CommissioningDestination) => void;
}

async function readJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json() as { error?: string; detail?: string; message?: string };
      detail = body.detail ?? body.error ?? body.message ?? detail;
    } catch { /* Preserve the HTTP status when the body is not JSON. */ }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

function actionView(action: CommissioningAction | null): CommissioningDestination | null {
  return action === "station" ? null : action;
}

function actionLabel(action: CommissioningAction): string {
  return { station: "station identity", health: "system health", media: "media setup", channels: "channel setup", schedule: "schedule" }[action];
}

function statusTone(status: CommissioningStatus | "fail"): CommissioningStatus {
  return status === "fail" ? "blocked" : status;
}

function Status({ status }: { status: CommissioningStatus | "fail" }) {
  const tone = statusTone(status);
  return <span className={`commission-status commission-status-${tone}`}>{commissioningStatusLabel(tone)}</span>;
}

export function AuthoritativeCommissioning({ onNavigate }: CommissioningProps) {
  const [report, setReport] = useState<AuthoritativeCommissioningReport | null>(null);
  const [stationBox, setStationBox] = useState<StationBoxProfile | null>(null);
  const [selectedCheckId, setSelectedCheckId] = useState<string | null>(null);
  const [readError, setReadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const [nextReport, nextStationBox] = await Promise.all([
        readJson<AuthoritativeCommissioningReport>("/api/v1/commissioning/report"),
        readJson<StationBoxProfile>("/api/v1/station-box-profile").catch(() => null),
      ]);
      setReport(nextReport);
      setStationBox(nextStationBox);
      setReadError(null);
    } catch (problem) {
      setReadError(problem instanceof Error ? problem.message : "Authoritative commissioning state could not be read.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 5_000);
    return () => window.clearInterval(timer);
  }, [load]);

  const recommended = report ? currentCommissioningCheck(report) : null;
  const selected = report?.checks.find((check) => check.id === selectedCheckId) ?? null;
  const focused = selected ?? recommended ?? report?.checks[0] ?? null;
  const groups = report ? commissioningGroups(report) : [];
  const coreComplete = report ? isCoreCommissioningComplete(report) : false;
  const fullyComplete = report ? isCommissioningComplete(report) : false;

  function openAction(check: AuthoritativeCommissioningCheck) {
    setSelectedCheckId(check.id);
    const view = actionView(commissioningAction(check));
    if (view) onNavigate(view);
  }

  if (loading && !report) return <>
    <header className="page-header"><div><p className="eyebrow">Guided commissioning</p><h1>Reading station authority…</h1><p>The console is loading the ordered commissioning report. No browser state is used as proof.</p></div></header>
    <section className="panel commissioning-loading" aria-live="polite"><div className="boot-line"><i /></div><p>Checking current station, StationBox, sources, outputs, and first-air evidence…</p></section>
  </>;

  if (!report) return <>
    <header className="page-header"><div><p className="eyebrow">Guided commissioning</p><h1>Commissioning authority is unavailable.</h1><p>This console will not substitute browser guesses for the missing server report.</p></div></header>
    <section className="panel commissioning-loading">{readError && <div className="error-box" role="alert">{readError}</div>}<button className="primary" onClick={() => void load()}>Retry authoritative report</button></section>
  </>;

  const focusedAction = focused ? commissioningAction(focused) : null;
  const focusedView = actionView(focusedAction);
  return <>
    <header className="page-header commissioning-header"><div><p className="eyebrow">Authoritative first-run commissioning</p><h1>{fullyComplete ? "Commissioning evidence is complete." : coreComplete ? "Local station commissioning is complete." : recommended ? `Next: ${recommended.label}` : "Review commissioning evidence."}</h1><p>Server checks stay in their required order and refresh every five seconds. The browser cannot mark a step complete.</p></div><div className="header-actions"><button className="secondary" onClick={() => void load()}>Refresh proof</button></div></header>
    <div className="commissioning-boundary" role="note"><strong>Required gate: under 30:00</strong><span>healthy first launch → worker-acknowledged first scheduled broadcast</span><p>Installer and runtime-provisioning time are measured separately. Required product gaps remain blocked; external receiver and field proof are never inferred from local transport.</p></div>
    {readError && <div className="error-box" role="alert">Latest refresh failed: {readError}. Previously loaded authority remains visible.</div>}
    <section className={`commission-banner commission-authority-${report.overall_status}`} aria-live="polite"><div className="commission-score">{report.core_progress ? `${report.core_progress.passed}/${report.core_progress.total}` : "—"}</div><div><span className="commission-rollup">{coreComplete ? "Core verified by server" : report.schema_version === 2 ? "Core commissioning blocked" : "Core completion not reported"}</span><strong>{coreComplete ? "Ready for separate external and field gates" : recommended ? recommended.label : "No current server step reported"}</strong><p>{coreComplete ? "Local completion came from core_complete in the authoritative report; external gates remain visible below." : recommended?.detail ?? "This station must supply a schema-v2 core rollup before the console can claim local completion."}</p></div><Status status={report.overall_status === "pass" ? "pass" : report.overall_status} /></section>
    <div className="commissioning-layout">
      <section className="commissioning-journey" aria-label="Ordered authoritative commissioning steps">
        {groups.length === 0 ? <div className="panel commissioning-empty"><strong>No commissioning checks reported</strong><p>The station returned an empty authoritative report. Refresh after the service is repaired.</p></div> : groups.map((group) => <section className="panel commission-phase" key={group.id}><div className="panel-head"><div><p className="eyebrow">Commissioning phase</p><h2>{group.label}</h2></div><span className="count">{group.checks.filter((check) => check.status === "pass").length}/{group.checks.length}</span></div><div className="commission-checks">{group.checks.map((check) => {
          const external = isExternalCommissioningCheck(check);
          const current = check.id === recommended?.id;
          const active = check.id === focused?.id;
          const action = commissioningAction(check);
          const view = actionView(action);
          return <article className={`commission-check commission-${check.status} ${active ? "selected" : ""}`} key={check.id}>
            <button className="commission-check-select" aria-current={current ? "step" : undefined} aria-expanded={active} onClick={() => setSelectedCheckId(check.id)}>
              <span className="commission-check-mark" aria-hidden="true">{check.status === "pass" ? "✓" : current ? "→" : "·"}</span>
              <span><strong>{check.label}</strong><small>{check.detail}</small></span>
            </button>
            <div className="commission-check-meta"><span className={`commission-boundary-tag ${external ? "external" : "local"}`}>{external ? "External evidence" : "Local authority"}</span><Status status={check.status} />{check.required === false && <span className="commission-optional">Not core-required</span>}</div>
            {active && <div className="commission-evidence"><div><span>Evidence</span><p>{check.evidence || "No evidence detail was reported."}</p></div>{check.next_step && <div><span>Next step</span><p>{check.next_step}</p></div>}{check.status !== "pass" && !external && !view && <div className="commission-no-action"><strong>No operator action is available in this build.</strong><p>This required gap stays blocked until the product exposes and proves the capability.</p></div>}{view && action && <button className="secondary" onClick={() => openAction(check)}>Open {actionLabel(action)} →</button>}</div>}
          </article>;
        })}</div></section>)}
      </section>
      <aside className="commissioning-side">
        <section className="panel commission-focus"><p className="eyebrow">Current server step</p>{focused ? <><h2>{focused.label}</h2><Status status={focused.status} /><p>{focused.detail}</p><dl><div><dt>Boundary</dt><dd>{isExternalCommissioningCheck(focused) ? "External evidence" : "Local station authority"}</dd></div><div><dt>Required for core</dt><dd>{focused.required === false ? "No" : "Yes"}</dd></div><div><dt>Evidence source</dt><dd>{focused.evidence || "Not reported"}</dd></div></dl>{focusedView && focusedAction && <button className="primary wide" onClick={() => openAction(focused)}>Continue to {actionLabel(focusedAction)}</button>}</> : <><h2>No current step</h2><p>The server did not name a next required check.</p></>}</section>
        <section className="panel stationbox-evidence"><div className="panel-head"><div><p className="eyebrow">Separate live authority</p><h2>StationBox profile</h2></div>{stationBox && <span className={`headend-rollup headend-${stationBox.peg_readiness.color}`}>{stationBox.engine_tier_verdict.replaceAll("_", " ")}</span>}</div>{stationBox ? <div className="stationbox-dimensions">{stationBox.peg_readiness.dimensions.map((dimension) => <article key={dimension.name}><div><strong>{dimension.name.replaceAll("_", " ")}</strong><Status status={dimension.state} /></div><p>{dimension.detail}</p><small>{dimension.evidence}</small>{dimension.next_step && <b>{dimension.next_step}</b>}</article>)}</div> : <div className="commissioning-empty"><strong>StationBox evidence unavailable</strong><p>The commissioning report remains authoritative; this supporting profile could not be loaded.</p></div>}</section>
      </aside>
    </div>
  </>;
}
