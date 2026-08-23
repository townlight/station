export const MIN_SAMPLE_DURATION_MS = 3_000;
export const MAX_SAMPLE_DURATION_MS = 300_000;
export const MAX_CAPTURE_BYTES = 256 * 1024 * 1024;

export type LoudnessProofJobState = "pending" | "claimed" | "capture_pending" | "analyzing" | "completed" | "failed";

export function isTerminalLoudnessProofState(state: LoudnessProofJobState): boolean {
  return state === "completed" || state === "failed";
}

export function validateLoudnessProofRequest(sampleDurationMs: number, maxCaptureBytes: number): string | null {
  if (!Number.isInteger(sampleDurationMs) || sampleDurationMs < MIN_SAMPLE_DURATION_MS || sampleDurationMs > MAX_SAMPLE_DURATION_MS) {
    return `Sample duration must be between ${MIN_SAMPLE_DURATION_MS.toLocaleString()} and ${MAX_SAMPLE_DURATION_MS.toLocaleString()} ms.`;
  }
  if (!Number.isInteger(maxCaptureBytes) || maxCaptureBytes < 1 || maxCaptureBytes > MAX_CAPTURE_BYTES) {
    return `Capture limit must be between 1 byte and ${MAX_CAPTURE_BYTES.toLocaleString()} bytes.`;
  }
  return null;
}
