import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiRequest } from "./api-client";
import {
  boundedMaterializationError,
  appendMaterializationHistoryPage,
  continueAfterMaterialization,
  formatUtcMaterializationTime,
  materializationHistoryOriginLabel,
  materializationChannelRenderState,
  parseMaterializationHistoryResponse,
  recordedValue,
  recurringDraftChannelState,
  recurringDraftPermissionState,
  scheduleMaterializationHistoryPath,
  scheduleMaterializationPath,
  scheduleMaterializationRequestInit,
  shouldApplyMaterializationChannel,
  shouldApplyMaterializationHistoryResponse,
  summarizeMaterializationOutcomes,
  type ScheduleMaterializationHistoryEntry,
  type ScheduleMaterializationHistoryCursor,
  type ScheduleMaterializationHistoryResponse,
  type ScheduleMaterializationResponse,
} from "./schedule-materialization-contract";

interface OperatorSessionResponse {
  session: { roles: string[] };
}

interface ScheduleMaterializationCardProps {
  channelId: string;
  channelName: string;
  channelEnabled: boolean;
  onApplied: (channelId: string) => Promise<void>;
}

function dispositionLabel(kind: string): string {
  return kind.replaceAll("_", " ");
}

export function ScheduleMaterializationCard({
  channelId,
  channelName,
  channelEnabled,
  onApplied,
}: ScheduleMaterializationCardProps) {
  const [roles, setRoles] = useState<string[] | null>(null);
  const [authorityError, setAuthorityError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ScheduleMaterializationResponse | null>(null);
  const [operationChannelId, setOperationChannelId] = useState(channelId);
  const [history, setHistory] = useState<ScheduleMaterializationHistoryEntry[]>([]);
  const [historyNextBefore, setHistoryNextBefore] = useState<ScheduleMaterializationHistoryCursor | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [historyChannelId, setHistoryChannelId] = useState(channelId);
  const historyRecords = useRef<ScheduleMaterializationHistoryEntry[]>([]);
  const requestSequence = useRef(0);
  const historySequence = useRef(0);
  const activeHistoryChannel = useRef(channelId);
  if (!shouldApplyMaterializationChannel(activeHistoryChannel.current, channelId)) {
    activeHistoryChannel.current = channelId;
    requestSequence.current += 1;
    historySequence.current += 1;
  }
  activeHistoryChannel.current = channelId;

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

  const loadHistory = useCallback(async (reset: boolean, cursor?: ScheduleMaterializationHistoryCursor) => {
    if (!channelId) return;
    const requestId = ++historySequence.current;
    const requestedChannelId = channelId;
    if (reset) {
      historyRecords.current = [];
      setHistoryChannelId(requestedChannelId);
      setHistory([]);
      setHistoryNextBefore(null);
    }
    setHistoryLoading(true);
    setHistoryError(null);
    try {
      const raw = await apiRequest<unknown>(
        scheduleMaterializationHistoryPath(requestedChannelId, undefined, cursor),
      );
      const next: ScheduleMaterializationHistoryResponse = parseMaterializationHistoryResponse(raw);
      if (!shouldApplyMaterializationHistoryResponse(requestId, historySequence.current, requestedChannelId, activeHistoryChannel.current)) return;
      if (!shouldApplyMaterializationChannel(requestedChannelId, activeHistoryChannel.current) || next.entries.some((entry) => entry.channel_id !== requestedChannelId)) {
        historyRecords.current = [];
        setHistoryChannelId(requestedChannelId);
        setHistory([]);
        setHistoryNextBefore(null);
        setHistoryError("The station returned history for a different channel.");
        return;
      }
      const merged = appendMaterializationHistoryPage(historyRecords.current, next, reset);
      historyRecords.current = merged;
      setHistoryChannelId(requestedChannelId);
      setHistory(merged);
      setHistoryNextBefore(next.next_cursor);
    } catch (problem) {
      if (shouldApplyMaterializationHistoryResponse(requestId, historySequence.current, requestedChannelId, activeHistoryChannel.current)) {
        if (problem instanceof Error && problem.message.startsWith("Materialization history response is malformed:")) {
          historyRecords.current = [];
          setHistoryChannelId(requestedChannelId);
          setHistory([]);
          setHistoryNextBefore(null);
        }
        setHistoryError(boundedMaterializationError(problem));
      }
    } finally {
      if (shouldApplyMaterializationHistoryResponse(requestId, historySequence.current, requestedChannelId, activeHistoryChannel.current)) setHistoryLoading(false);
    }
  }, [channelId]);

  useEffect(() => {
    requestSequence.current += 1;
    historySequence.current += 1;
    setOperationChannelId(channelId);
    setHistoryChannelId(channelId);
    setResult(null);
    setError(null);
    setBusy(false);
    void loadHistory(true);
  }, [channelId, loadHistory]);

  const permissionState = recurringDraftPermissionState(roles, authorityError);
  const channelState = recurringDraftChannelState(channelEnabled);
  const canGenerate = permissionState === "available" && channelState === "enabled";
  const displayed = materializationChannelRenderState(channelId, {
    operation_channel_id: operationChannelId,
    busy,
    error,
    result,
    history_channel_id: historyChannelId,
    history,
    history_next_before: historyNextBefore,
    history_loading: historyLoading,
    history_error: historyError,
  });
  const summary = useMemo(
    () => summarizeMaterializationOutcomes(displayed.result?.outcomes ?? []),
    [displayed.result],
  );

  async function generate() {
    if (!channelId || !canGenerate || displayed.busy) return;
    const requestedChannelId = channelId;
    const requestId = ++requestSequence.current;
    setOperationChannelId(requestedChannelId);
    setBusy(true);
    setError(null);
    try {
      const next = await apiRequest<ScheduleMaterializationResponse>(
        scheduleMaterializationPath(requestedChannelId),
        scheduleMaterializationRequestInit(),
      );
      if (requestId !== requestSequence.current || !shouldApplyMaterializationChannel(requestedChannelId, activeHistoryChannel.current)) return;
      setOperationChannelId(requestedChannelId);
      setResult(next);
      await continueAfterMaterialization(
        next.disposition,
        requestId,
        () => requestSequence.current,
        () => shouldApplyMaterializationChannel(requestedChannelId, activeHistoryChannel.current) ? onApplied(requestedChannelId) : Promise.resolve(),
        () => { void loadHistory(true); },
      );
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
      {!channelEnabled && (
        <div className="schedule-materialization-boundary" role="status">
          <strong>Recurring generation is unavailable while this channel is disabled</strong>
          <span>Enable this channel in Output control to generate recurring schedule Drafts. Existing materialization history remains readable below.</span>
        </div>
      )}
      <button
        type="button"
        className="primary"
        disabled={!channelId || roles === null || !canGenerate || displayed.busy}
        aria-busy={permissionState === "checking" || displayed.busy}
        aria-describedby="schedule-materialization-help"
        onClick={() => void generate()}
      >
        {displayed.busy ? "Generating recurring drafts…" : "Generate recurring drafts"}
      </button>
      <small id="schedule-materialization-help">
        No window, rule, candidate, evidence, actor, or time value is sent by this browser.
      </small>
      {authorityError && <div className="error-box" role="alert">Operator authority could not be read: {authorityError}</div>}
      {displayed.error && <div className="error-box" role="alert">{displayed.error}</div>}
      {displayed.result && (
        <div className="schedule-materialization-result" role="status" aria-live="polite">
          <div className="schedule-materialization-result-head">
            <div>
              <strong>{displayed.result.disposition === "applied" ? "Recurring Drafts applied" : "Run already applied"}</strong>
              <span>{displayed.result.plans.length} plan{displayed.result.plans.length === 1 ? "" : "s"} · {displayed.result.outcomes.length} outcome{displayed.result.outcomes.length === 1 ? "" : "s"}</span>
            </div>
            <span className={`pill ${displayed.result.disposition === "applied" ? "pill-active" : "pill-warning"}`}>
              {dispositionLabel(displayed.result.disposition)}
            </span>
          </div>
          <dl>
            <div><dt>UTC union window</dt><dd>{formatUtcMaterializationTime(displayed.result.window.starts_at_utc_ms)} through {formatUtcMaterializationTime(displayed.result.window.ends_at_utc_ms)}</dd></div>
            <div><dt>Run ID</dt><dd><code>{displayed.result.run_id}</code></dd></div>
          </dl>
          <div className="schedule-materialization-summary" aria-label="Disposition summary">
            {summary.length ? summary.map((entry) => (
              <span key={entry.kind}><strong>{entry.count}</strong> {dispositionLabel(entry.kind)}</span>
            )) : <span><strong>0</strong> slot outcomes</span>}
          </div>
        </div>
      )}
      <MaterializationHistory
        records={[...displayed.history]}
        nextBefore={displayed.history_next_before}
        loading={displayed.history_loading}
        error={displayed.history_error}
        onRefresh={() => loadHistory(true)}
        onLoadMore={() => displayed.history_next_before ? loadHistory(false, displayed.history_next_before) : Promise.resolve()}
      />
    </section>
  );
}

interface MaterializationHistoryProps {
  records: ScheduleMaterializationHistoryEntry[];
  nextBefore: ScheduleMaterializationHistoryCursor | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => Promise<void>;
  onLoadMore: () => Promise<void>;
}

export function MaterializationHistory({
  records,
  nextBefore,
  loading,
  error,
  onRefresh,
  onLoadMore,
}: MaterializationHistoryProps) {
  return (
    <section className="schedule-materialization-history" aria-labelledby="materialization-history-title">
      <div className="schedule-materialization-history-head">
        <div>
          <p className="eyebrow">Durable run record</p>
          <h3 id="materialization-history-title">Materialization history</h3>
          <p className="muted">Newest first. This records Draft generation only; it does not indicate review, commit, dispatch, or what aired.</p>
        </div>
        <button type="button" className="text-button" onClick={() => void onRefresh()} disabled={loading}>
          {loading && !records.length ? "Reading…" : "Refresh history"}
        </button>
      </div>
      {error && <div className="error-box" role="alert">Materialization history could not be read: {error}<button type="button" className="text-button" onClick={() => void onRefresh()}>Retry</button></div>}
      {loading && !records.length && !error && <div className="schedule-history-state" role="status">Reading durable materialization runs…</div>}
      {loading && !!records.length && <span className="schedule-history-progress" role="status" aria-live="polite">Updating materialization history…</span>}
      {!loading && !error && !records.length && <div className="schedule-history-state"><strong>No materialization runs recorded</strong><span>Automatic and operator-generated runs for this channel will appear here.</span></div>}
      {!!records.length && <div className="schedule-history-list">
        {records.map((record) => <MaterializationHistoryRecordView key={record.run_id} record={record} />)}
      </div>}
      {nextBefore && <button type="button" className="secondary wide" onClick={() => void onLoadMore()} disabled={loading}>{loading ? "Loading older runs…" : "Load older runs"}</button>}
    </section>
  );
}

function MaterializationHistoryRecordView({ record }: { record: ScheduleMaterializationHistoryEntry }) {
  return (
    <details className="schedule-history-record">
      <summary>
        <span className="schedule-history-record-title">
          <strong>{materializationHistoryOriginLabel(record.origin)} run</strong>
          <small>{formatUtcMaterializationTime(record.occurred_at_unix_ms)} · {record.plans.length} plan{record.plans.length === 1 ? "" : "s"} · {record.outcomes.length} outcome{record.outcomes.length === 1 ? "" : "s"}</small>
        </span>
        <span className={`pill ${record.origin === "automatic" ? "pill-active" : "pill-warning"}`}>{materializationHistoryOriginLabel(record.origin)}</span>
      </summary>
      <div className="schedule-history-record-body">
        <dl>
          <div><dt>UTC checkpoint day</dt><dd>{formatUtcMaterializationTime(record.checkpoint_utc_day * 86_400_000).slice(0, 10)}</dd></div>
          <div><dt>UTC checkpoint time</dt><dd>{formatUtcMaterializationTime(record.observed_at_utc_ms)}</dd></div>
          <div><dt>UTC run time</dt><dd>{formatUtcMaterializationTime(record.occurred_at_unix_ms)}</dd></div>
          <div><dt>Run ID</dt><dd><code>{recordedValue(record.run_id)}</code></dd></div>
          <div><dt>Disposition</dt><dd>Not recorded</dd></div>
          <div><dt>Plan count</dt><dd>{recordedValue(record.plans.length)}</dd></div>
          <div><dt>Outcome count</dt><dd>{recordedValue(record.outcomes.length)}</dd></div>
        </dl>
        <div className="schedule-history-subsection">
          <strong>Outcomes</strong>
          {!record.outcomes.length ? <span className="schedule-history-not-recorded">No outcomes recorded</span> : <div className="schedule-history-outcomes">{record.outcomes.map((outcome) => <div key={`${outcome.ordinal}-${outcome.rule_id}`}><span>{outcome.ordinal + 1}. {outcome.rule_id}</span><small>{dispositionLabel(outcome.outcome.kind)}</small></div>)}</div>}
        </div>
        <div className="schedule-history-subsection">
          <strong>Generated Drafts</strong>
          {!record.generated_drafts.length ? <span className="schedule-history-not-recorded">No generated Drafts recorded</span> : <div className="schedule-history-drafts">{record.generated_drafts.map((draft) => <div key={draft.schedule_item_id}><span><code>{recordedValue(draft.schedule_item_id)}</code><small>{recordedValue(draft.occurrence_id)}</small></span><span>{recordedValue(draft.current_schedule.state)}</span></div>)}</div>}
        </div>
      </div>
    </details>
  );
}
