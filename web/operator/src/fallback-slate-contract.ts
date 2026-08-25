export type FallbackImageReadiness = "ready" | "missing" | "rejected" | "processing";

export function fallbackSlateMutationReason(
  channelEnabled: boolean,
  readiness: FallbackImageReadiness | null,
): string | null {
  if (channelEnabled) return "Stop this output before changing its fallback slate.";
  if (readiness !== "ready") return "Select a currently verified managed image.";
  return null;
}
