import { FormEvent, useEffect, useMemo, useState } from "react";
import { apiRequest } from "./api-client";
import {
  canSubmitReceiverReceipt,
  receiverReceiptOutcomeLabel,
  type ReceiverEvidenceMethod,
  type ReceiverReceiptOutcome,
} from "./receiver-receipt-contract";

interface ReceiverSink {
  sink_id: string;
  label: string;
  enabled: boolean;
}

interface ReceiverReceipt {
  receipt_id: string;
  channel_id: string;
  sink_id: string;
  outcome: ReceiverReceiptOutcome;
  method: ReceiverEvidenceMethod;
  receiver_label: string;
  evidence_reference: string;
  detail: string;
  observed_at_unix_ms: number;
  recorded_at_unix_ms: number;
  recorded_by: string;
  candidate_version: string;
  source_commit: string;
  configuration_hash: string;
  current: boolean;
}

interface ReceiverReceiptHistory {
  channel_id: string;
  receipts: ReceiverReceipt[];
}

function localDateTimeValue(date: Date): string {
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000)
    .toISOString()
    .slice(0, 16);
}

export function ReceiverReceipts({
  channelId,
  sinks,
  onRecorded,
}: {
  channelId: string;
  sinks: ReceiverSink[];
  onRecorded: () => Promise<void>;
}) {
  const enabledSinks = useMemo(() => sinks.filter((sink) => sink.enabled), [sinks]);
  const [sinkId, setSinkId] = useState(enabledSinks[0]?.sink_id ?? "");
  const [outcome, setOutcome] = useState<ReceiverReceiptOutcome>("accepted");
  const [method, setMethod] = useState<ReceiverEvidenceMethod>("headend_operator_confirmation");
  const [receiverLabel, setReceiverLabel] = useState("");
  const [evidenceReference, setEvidenceReference] = useState("");
  const [detail, setDetail] = useState("");
  const [observedAt, setObservedAt] = useState(() => localDateTimeValue(new Date()));
  const [history, setHistory] = useState<ReceiverReceipt[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    if (!enabledSinks.some((sink) => sink.sink_id === sinkId)) {
      setSinkId(enabledSinks[0]?.sink_id ?? "");
    }
  }, [enabledSinks, sinkId]);

  async function loadHistory() {
    if (!channelId) return;
    try {
      const response = await apiRequest<ReceiverReceiptHistory>(
        `/api/v1/channels/${encodeURIComponent(channelId)}/receiver-receipts`,
      );
      setHistory(response.receipts);
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "Receiver evidence could not be read.");
    }
  }

  useEffect(() => { void loadHistory(); }, [channelId]);

  const observedAtUnixMs = new Date(observedAt).getTime();
  const canSubmit = canSubmitReceiverReceipt({
    sinkId,
    receiverLabel,
    evidenceReference,
    detail,
    observedAtUnixMs,
  }, Date.now());

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    setBusy(true); setError(null); setMessage(null);
    try {
      const created = await apiRequest<ReceiverReceipt>(
        `/api/v1/channels/${encodeURIComponent(channelId)}/receiver-receipts`,
        {
          method: "POST",
          body: JSON.stringify({
            sink_id: sinkId,
            outcome,
            method,
            receiver_label: receiverLabel.trim(),
            evidence_reference: evidenceReference.trim(),
            detail: detail.trim(),
            observed_at_unix_ms: observedAtUnixMs,
          }),
        },
      );
      setMessage(`${receiverReceiptOutcomeLabel(created.outcome)}. Receipt ${created.receipt_id} is bound to this installed candidate and output configuration.`);
      setEvidenceReference(""); setDetail("");
      setObservedAt(localDateTimeValue(new Date()));
      await loadHistory();
      await onRecorded();
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : "Receiver evidence could not be recorded.");
    } finally {
      setBusy(false);
    }
  }

  return <section className="receiver-receipts">
    <div className="panel-head"><div><p className="eyebrow">External acceptance</p><h3>Record receiver evidence</h3></div><span className="count">{history.filter((receipt) => receipt.current).length} current</span></div>
    <p className="muted">Use this only after a cable operator, receiver device, or observed return path confirms the signal. TownLight records the evidence; it does not create the external fact.</p>
    <form onSubmit={submit} className="receiver-receipt-form">
      <div className="form-pair"><label>Output sink<select required value={sinkId} onChange={(event) => setSinkId(event.target.value)}>{enabledSinks.map((sink) => <option key={sink.sink_id} value={sink.sink_id}>{sink.label}</option>)}</select></label><label>Result<select value={outcome} onChange={(event) => setOutcome(event.target.value as ReceiverReceiptOutcome)}><option value="accepted">Accepted by receiver</option><option value="rejected">Rejected by receiver</option></select></label></div>
      <div className="form-pair"><label>Evidence method<select value={method} onChange={(event) => setMethod(event.target.value as ReceiverEvidenceMethod)}><option value="headend_operator_confirmation">Headend operator confirmation</option><option value="receiver_telemetry">Receiver telemetry</option><option value="return_path_observation">Return-path observation</option><option value="visual_audio_observation">Visual/audio observation</option></select></label><label>Observed at<input required type="datetime-local" value={observedAt} onChange={(event) => setObservedAt(event.target.value)} /></label></div>
      <label>Receiver or confirmer<input required maxLength={200} value={receiverLabel} onChange={(event) => setReceiverLabel(event.target.value)} placeholder="Cable operator master control" /></label>
      <label>Evidence reference<input required maxLength={500} value={evidenceReference} onChange={(event) => setEvidenceReference(event.target.value)} placeholder="Headend ticket, telemetry record, or call reference" /></label>
      <label>What was confirmed<textarea required maxLength={2000} value={detail} onChange={(event) => setDetail(event.target.value)} placeholder="Decoded video remained stable and program audio was audible." /></label>
      {error && <div className="error-box" role="alert">{error}</div>}
      {message && <div className="notice" role="status">{message}</div>}
      <button className="primary wide" disabled={!canSubmit || busy || enabledSinks.length === 0}>{busy ? "Recording evidence…" : "Append receiver receipt"}</button>
    </form>
    {history.length > 0 && <div className="row-list receiver-receipt-history">{history.slice(0, 10).map((receipt) => <article className="data-row" key={receipt.receipt_id}><div><strong>{receiverReceiptOutcomeLabel(receipt.outcome)}</strong><small>{receipt.receiver_label} · {receipt.evidence_reference} · {new Date(receipt.observed_at_unix_ms).toLocaleString()}</small><p>{receipt.detail}</p></div><span className={`status ${receipt.current ? receipt.outcome === "accepted" ? "pass" : "blocked" : "unknown"}`}>{receipt.current ? "current" : "historical"}</span></article>)}</div>}
  </section>;
}
