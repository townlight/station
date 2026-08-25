export type ReceiverReceiptOutcome = "accepted" | "rejected";
export type ReceiverEvidenceMethod =
  | "headend_operator_confirmation"
  | "receiver_telemetry"
  | "return_path_observation"
  | "visual_audio_observation";

export interface ReceiverReceiptDraft {
  sinkId: string;
  receiverLabel: string;
  evidenceReference: string;
  detail: string;
  observedAtUnixMs: number;
}

export function canSubmitReceiverReceipt(draft: ReceiverReceiptDraft, nowUnixMs: number): boolean {
  return draft.sinkId.trim().length > 0
    && draft.receiverLabel.trim().length > 0
    && draft.receiverLabel.length <= 200
    && draft.evidenceReference.trim().length > 0
    && draft.evidenceReference.length <= 500
    && draft.detail.trim().length > 0
    && draft.detail.length <= 2_000
    && Number.isSafeInteger(draft.observedAtUnixMs)
    && draft.observedAtUnixMs >= 0
    && draft.observedAtUnixMs <= nowUnixMs;
}

export function receiverReceiptOutcomeLabel(outcome: ReceiverReceiptOutcome): string {
  return outcome === "accepted"
    ? "Accepted by external receiver"
    : "Rejected by external receiver";
}
