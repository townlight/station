import { useEffect, useMemo, useRef, useState } from "react";
import { apiRequest } from "./api-client";
import {
  boundedMaterializationError,
  formatUtcMaterializationTime,
  recurringDraftPermissionState,
  scheduleMaterializationPath,
  scheduleMaterializationRequestInit,
  summarizeMaterializationOutcomes,
  type ScheduleMaterializationResponse,
} from "./schedule-materialization-contract";

interface OperatorSessionResponse {
  session: { roles: string[] };
}

interface ScheduleMaterializationCardProps {
  channelId: string;
  channelName: string;
  onApplied: () => Promise<void>;
}

function dispositionLabel(kind: string): string {
  return kind.replaceAll("_", " ");
}

export function ScheduleMaterializationCard({
  channelId,
  channelName,
  onApplied,
}: ScheduleMaterializationCardProps) {
  const [roles, setRoles] = useState<string[] | null>(null);
  const [authorityError, setAuthorityError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ScheduleMaterializationResponse | null>(null);
  const requestSequence = useRef(0);

  useEffect(() => {
    let current = true;
    void apiRequest<OperatorSessionResponse>("/api/v1/auth/session")
      .then((session) => {
        if (current) {
          setRoles(session.session.roles);
          setAuthorityError(null);
        }
      })
      .catch((problem) => {
        if (current) {
          setRoles([]);
          setAuthorityError(boundedMaterializationError(problem));
        }
      });
    return () => {
      current = false;
    };
  }, []);

  useEffect(() => {
    requestSequence.current += 1;
    setResult(null);
    setError(null);
    setBusy(false);
  }, [channelId]);

  const permissionState = recurringDraftPermissionState(roles, authorityError);
  const canGenerate = permissionState === "available";
  const summary = useMemo(
    () => summarizeMaterializationOutcomes(result?.outcomes ?? []),
    [result],
  );

  async function generate() {
    if (!channelId || !canGenerate || busy) return;
    const requestId = ++requestSequence.current;
    setBusy(true);
    setError(null);
    try {
      const next = await apiRequest<ScheduleMaterializationResponse>(
        scheduleMaterializationPath(channelId),
        scheduleMaterializationRequestInit(),
      );
      if (requestId !== requestSequence.current) return;
      setResult(next);
      if (next.disposition === "applied") await onApplied();
    } catch (problem) {
      if (requestId === requestSequence.current) {
        setError(boundedMaterializationError(problem));
      }
    } finally {
      if (requestId === requestSequence.current) setBusy(false);
    }
  }

  return (
    <section className="panel schedule-materialization-card" aria-labelledby="schedule-materialization-title">
      <div className="panel-head">
        <div>
          <p className="eyebrow">Recurring schedule authority</p>
          <h2 id="schedule-materialization-title">Generate recurring drafts</h2>
        </div>
        <span className="pill pill-active">{channelName}</span>
      </div>
      <p className="muted">
        The station derives every enabled rule, exact rolling horizon, source snapshot, and UTC
        window. Generated items remain Drafts until an operator reviews and commits them.
      </p>
      {permissionState === "checking" && (
        <div className="schedule-materialization-boundary" role="status" aria-live="polite">
          <strong>Checking recurring generation access</strong>
          <span>Reading the current operator roles.</span>
        </div>
      )}
      {permissionState === "read_only" && (
        <div className="schedule-materialization-boundary" role="status">
          <strong>Recurring generation is read-only for this account</strong>
          <span>Setup Admin or Meeting Operator is required to generate schedule Drafts.</span>
        </div>
      )}
      <button
        type="button"
        className="primary"
        disabled={!channelId || roles === null || !canGenerate || busy}
        aria-busy={permissionState === "checking" || busy}
        aria-describedby="schedule-materialization-help"
        onClick={() => void generate()}
      >
        {busy ? "Generating recurring drafts…" : "Generate recurring drafts"}
      </button>
      <small id="schedule-materialization-help">
        No window, rule, candidate, evidence, actor, or time value is sent by this browser.
      </small>
      {authorityError && <div className="error-box" role="alert">Operator authority could not be read: {authorityError}</div>}
      {error && <div className="error-box" role="alert">{error}</div>}
      {result && (
        <div className="schedule-materialization-result" role="status" aria-live="polite">
          <div className="schedule-materialization-result-head">
            <div>
              <strong>{result.disposition === "applied" ? "Recurring Drafts applied" : "Run already applied"}</strong>
              <span>{result.plans.length} plan{result.plans.length === 1 ? "" : "s"} · {result.outcomes.length} outcome{result.outcomes.length === 1 ? "" : "s"}</span>
            </div>
            <span className={`pill ${result.disposition === "applied" ? "pill-active" : "pill-warning"}`}>
              {dispositionLabel(result.disposition)}
            </span>
          </div>
          <dl>
            <div><dt>UTC union window</dt><dd>{formatUtcMaterializationTime(result.window.starts_at_utc_ms)} through {formatUtcMaterializationTime(result.window.ends_at_utc_ms)}</dd></div>
            <div><dt>Run ID</dt><dd><code>{result.run_id}</code></dd></div>
          </dl>
          <div className="schedule-materialization-summary" aria-label="Disposition summary">
            {summary.length ? summary.map((entry) => (
              <span key={entry.kind}><strong>{entry.count}</strong> {dispositionLabel(entry.kind)}</span>
            )) : <span><strong>0</strong> slot outcomes</span>}
          </div>
        </div>
      )}
    </section>
  );
}
