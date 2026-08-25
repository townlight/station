import { FormEvent, useEffect, useMemo, useState } from "react";
import {
  MAX_CAPTURE_BYTES,
  MAX_SAMPLE_DURATION_MS,
  MIN_SAMPLE_DURATION_MS,
  isTerminalLoudnessProofState,
  validateLoudnessProofRequest,
  type LoudnessProofJobState,
} from "./loudness-proof-contract";
import { ApiError, apiRequest } from "./api-client";

const DEFAULT_SAMPLE_DURATION_MS = 600_000;
const DEFAULT_CAPTURE_BYTES = 768 * 1024 * 1024;

export interface LoudnessProofSink {
  sink_id: string;
  label: string;
  kind: "udp-ts" | "srt";
  destination: string;
  loudness_target_lufs: number;
  enabled: boolean;
}

interface LoudnessProofSummary {
  sink_id: string;
  label: string;
  configured_target_lufs: number;
  measured_lufs: number | null;
  measurement_status: "pass" | "fail" | "not_run" | "invalid" | "expired";
  receiver_acceptance: "not_attempted" | "unknown" | "verified" | "rejected";
  captured_at_unix_ms: number | null;
  sample_duration_ms: number | null;
  measurement_engine: string | null;
  measurement_method: string | null;
  detail: string | null;
}

interface LoudnessProofJob {
  job_id: string;
  channel_id: string;
  target_group: "cable" | "streaming";
  sink_id: string;
  target_lufs_milli: number;
  sample_duration_ms: number;
  max_capture_bytes: number;
  state: LoudnessProofJobState;
  created_at_unix_ms: number;
  updated_at_unix_ms: number;
  lease_expires_at_unix_ms: number | null;
  capture: {
    artifact_relative_path: string;
    capture_bytes: number;
    sample_duration_ms: number;
    capture_sha256: string;
  } | null;
  completed_at_unix_ms: number | null;
  failed_at_unix_ms: number | null;
  failure_detail: string | null;
}

interface LoudnessProofSummaryResponse {
  channel_id: string;
  sinks: LoudnessProofSummary[];
}

class ProofApiError extends Error {
  constructor(public status: number, public code: string | null, message: string) {
    super(message);
  }
}

async function proofRequest<T>(path: string, init?: RequestInit): Promise<T> {
  try { return await apiRequest<T>(path, init); }
  catch (problem) { if (problem instanceof ApiError) throw new ProofApiError(problem.status, problem.code, problem.message); throw problem; }
}

function proofStateLabel(state: LoudnessProofJobState): string {
  return {
    pending: "Pending",
    claimed: "Claimed",
    capture_pending: "Capture pending",
    analyzing: "Analyzing",
    completed: "Completed",
    failed: "Failed",
  }[state];
}

function statusLabel(status: LoudnessProofSummary["measurement_status"]): string {
  return status.replaceAll("_", " ");
}

function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(0)} MiB`;
  return `${Math.round(bytes / 1024)} KiB`;
}

function targetGroup(target: number): string {
  if (Math.abs(target + 24) < 0.001) return "Cable · −24 LUFS";
  if (Math.abs(target + 16) < 0.001) return "Streaming · −16 LUFS";
  return `${target.toFixed(1)} LUFS · no executable proof group`;
}

function statusTone(status: string): string {
  if (["pass", "verified", "completed"].includes(status)) return "good";
  if (["fail", "rejected", "failed", "invalid", "expired"].includes(status)) return "bad";
  return "active";
}

function ProofPill({ value }: { value: string }) {
  return <span className={`pill pill-${statusTone(value)}`}>{value.replaceAll("_", " ")}</span>;
}

function SinkProofCard({ sink, proof, selected, onSelect }: { sink: LoudnessProofSink; proof: LoudnessProofSummary | undefined; selected: boolean; onSelect: () => void }) {
  return <button type="button" className={`proof-sink-card ${selected ? "selected" : ""}`} onClick={onSelect} aria-pressed={selected}>
    <span className="proof-sink-card-head"><strong>{sink.label}</strong><ProofPill value={proof?.measurement_status ?? "not_run"} /></span>
    <small>{sink.kind === "srt" ? "SRT" : "UDP MPEG-TS"} · {sink.destination}</small>
    <span className="proof-sink-target">Configured target: <strong>{targetGroup(sink.loudness_target_lufs)}</strong></span>
    <span className="proof-sink-measurement">Measured: <strong>{proof?.measured_lufs == null ? "Not measured" : `${proof.measured_lufs.toFixed(2)} LUFS`}</strong></span>
    <span className="proof-sink-acceptance">Receiver acceptance: <strong>{proof?.receiver_acceptance?.replaceAll("_", " ") ?? "unknown"}</strong></span>
  </button>;
}

export function LoudnessProofPanel({ channelId, sinks }: { channelId: string; sinks: LoudnessProofSink[] }) {
  const enabledSinks = useMemo(() => sinks.filter((sink) => sink.enabled && (sink.kind === "udp-ts" || sink.kind === "srt")), [sinks]);
  const [selectedSinkId, setSelectedSinkId] = useState("");
  const [proofs, setProofs] = useState<LoudnessProofSummary[]>([]);
  const [job, setJob] = useState<LoudnessProofJob | null>(null);
  const [sampleDurationMs, setSampleDurationMs] = useState(String(DEFAULT_SAMPLE_DURATION_MS));
  const [maxCaptureBytes, setMaxCaptureBytes] = useState(String(DEFAULT_CAPTURE_BYTES));
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const selectedSink = enabledSinks.find((sink) => sink.sink_id === selectedSinkId) ?? enabledSinks[0];
  const proofBySink = useMemo(() => new Map(proofs.map((proof) => [proof.sink_id, proof])), [proofs]);

  useEffect(() => {
    if (!selectedSink || !enabledSinks.some((sink) => sink.sink_id === selectedSinkId)) {
      setSelectedSinkId(selectedSink?.sink_id ?? "");
    }
  }, [enabledSinks, selectedSink, selectedSinkId]);

  useEffect(() => {
    let active = true;
    setJob(null);
    setProofs([]);
    setError(null);
    if (!enabledSinks.length) return () => { active = false; };
    setLoading(true);
    void proofRequest<LoudnessProofSummaryResponse>(`/api/v1/channels/${encodeURIComponent(channelId)}/loudness-proof`)
      .then((response) => { if (active) setProofs(response.sinks); })
      .catch((problem) => { if (active) setError(problem instanceof Error ? problem.message : "Loudness proof status could not be read."); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [channelId, enabledSinks.length]);

  useEffect(() => {
    if (!job || isTerminalLoudnessProofState(job.state)) return;
    let active = true;
    const poll = async () => {
      try {
        const current = await proofRequest<LoudnessProofJob>(`/api/v1/channels/${encodeURIComponent(channelId)}/loudness-proof/jobs/${encodeURIComponent(job.job_id)}`);
        if (!active) return;
        setJob(current);
        if (isTerminalLoudnessProofState(current.state)) {
          const summary = await proofRequest<LoudnessProofSummaryResponse>(`/api/v1/channels/${encodeURIComponent(channelId)}/loudness-proof`);
          if (active) setProofs(summary.sinks);
        }
      } catch (problem) {
        if (active) setError(problem instanceof Error ? problem.message : "Loudness proof job status could not be read.");
      }
    };
    const timer = window.setInterval(() => void poll(), 2_000);
    return () => { active = false; window.clearInterval(timer); };
  }, [channelId, job]);

  async function requestProof(event: FormEvent) {
    event.preventDefault();
    if (!selectedSink) return;
    const duration = Number(sampleDurationMs);
    const bytes = Number(maxCaptureBytes);
    const validationError = validateLoudnessProofRequest(duration, bytes);
    if (validationError) {
      setError(validationError);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const created = await proofRequest<LoudnessProofJob>(`/api/v1/channels/${encodeURIComponent(channelId)}/loudness-proof`, {
        method: "POST",
        body: JSON.stringify({ sink_id: selectedSink.sink_id, sample_duration_ms: duration, max_capture_bytes: bytes }),
      });
      setJob(created);
    } catch (problem) {
      setError(problem instanceof ProofApiError && problem.code === "candidate_not_verified"
        ? `${problem.message} The installed candidate must be verified before proof can run.`
        : problem instanceof Error ? problem.message : "Loudness proof could not be requested.");
    } finally {
      setBusy(false);
    }
  }

  if (!enabledSinks.length) {
    return <section className="panel proof-panel"><div className="panel-head"><div><p className="eyebrow">Post-encode loudness</p><h2>Output proof</h2></div></div><p className="muted">No enabled UDP or SRT sink is configured for this channel.</p></section>;
  }

  const selectedProof = selectedSink ? proofBySink.get(selectedSink.sink_id) : undefined;
  return <section className="panel proof-panel">
    <div className="panel-head"><div><p className="eyebrow">Post-encode loudness</p><h2>Output proof</h2></div>{loading && <span className="muted">Reading status…</span>}</div>
    <p className="muted">Select an enabled output to capture and measure its encoded transport. This is local proof only; it does not establish acceptance by an external headend receiver.</p>
    <div className="proof-sink-list">{enabledSinks.map((sink) => <SinkProofCard key={sink.sink_id} sink={sink} proof={proofBySink.get(sink.sink_id)} selected={sink.sink_id === selectedSink?.sink_id} onSelect={() => setSelectedSinkId(sink.sink_id)} />)}</div>
    <div className="proof-work-grid">
      <form className="proof-request-form" onSubmit={requestProof}>
        <p className="eyebrow">Request a fresh local proof</p>
        <h3>{selectedSink?.label}</h3>
        <p className="proof-target-note">{targetGroup(selectedSink?.loudness_target_lufs ?? 0)}</p>
        <label>Sample duration (milliseconds)<input type="number" min={MIN_SAMPLE_DURATION_MS} max={MAX_SAMPLE_DURATION_MS} step="1" value={sampleDurationMs} onChange={(event) => setSampleDurationMs(event.target.value)} /><small>Allowed range: {MIN_SAMPLE_DURATION_MS.toLocaleString()}–{MAX_SAMPLE_DURATION_MS.toLocaleString()} ms. Default: 600,000 ms for commissioning. Capture adds 4 seconds of bounded transport context for independent decode.</small></label>
        <label>Maximum capture size (bytes)<input type="number" min="1" max={MAX_CAPTURE_BYTES} step="1" value={maxCaptureBytes} onChange={(event) => setMaxCaptureBytes(event.target.value)} /><small>Allowed range: 1–{MAX_CAPTURE_BYTES.toLocaleString()} bytes. Default: {formatBytes(DEFAULT_CAPTURE_BYTES)}.</small></label>
        {error && <div className="error-box" role="alert">{error}</div>}
        <button className="primary wide" disabled={busy || !selectedSink}>{busy ? "Requesting…" : "Request output proof"}</button>
      </form>
      <div className="proof-job-panel">
        <p className="eyebrow">Durable job</p>
        {!job ? <p className="muted">No proof job is running for this channel in this view.</p> : <>
          <div className="proof-job-status"><div><strong>{proofStateLabel(job.state)}</strong><small>{job.target_group === "cable" ? "Cable target · −24 LUFS" : "Streaming target · −16 LUFS"}</small></div><ProofPill value={job.state} /></div>
          <dl className="proof-job-details"><div><dt>Output</dt><dd>{enabledSinks.find((sink) => sink.sink_id === job.sink_id)?.label ?? job.sink_id}</dd></div><div><dt>Requested sample</dt><dd>{job.sample_duration_ms.toLocaleString()} ms</dd></div><div><dt>Capture limit</dt><dd>{formatBytes(job.max_capture_bytes)}</dd></div>{job.capture && <div><dt>Captured</dt><dd>{formatBytes(job.capture.capture_bytes)} · {job.capture.sample_duration_ms.toLocaleString()} ms</dd></div>}{job.failure_detail && <div><dt>Failure</dt><dd className="proof-failure">{job.failure_detail}</dd></div>}</dl>
          <p className="proof-boundary">Local measurement completion is not external receiver acceptance. Receiver acceptance remains <strong>{selectedProof?.receiver_acceptance?.replaceAll("_", " ") ?? "unknown"}</strong>.</p>
        </>}
      </div>
    </div>
  </section>;
}
