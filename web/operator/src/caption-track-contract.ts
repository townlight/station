export type CaptionTrackSourceKind = "embedded" | "sidecar" | "generated" | "external";

export interface CaptionCueDraft {
  cue_id: string;
  start_ms: number;
  duration_ms: number;
  text: string;
}

const MAX_CUES = 100_000;
const MAX_CUE_DURATION_MS = 60_000;
const MAX_CUE_TEXT_BYTES = 2_048;
const MAX_LABEL_BYTES = 120;

function timestampMilliseconds(value: string): number {
  const parts = value.replace(",", ".").split(":");
  const secondParts = parts.pop()?.split(".") ?? [];
  if (secondParts.length !== 2 || !/^\d{2}$/.test(secondParts[0]) || !/^\d{3}$/.test(secondParts[1])) {
    throw new Error(`Caption timing '${value}' is invalid.`);
  }
  const seconds = Number(secondParts[0]);
  const milliseconds = Number(secondParts[1]);
  const minutesText = parts.pop();
  const hoursText = parts.pop() ?? "0";
  if (parts.length > 0 || minutesText === undefined || !/^\d{2}$/.test(minutesText) || !/^\d+$/.test(hoursText)) {
    throw new Error(`Caption timing '${value}' is invalid.`);
  }
  const minutes = Number(minutesText);
  const hours = Number(hoursText);
  if (minutes > 59 || seconds > 59 || !Number.isSafeInteger(hours)) {
    throw new Error(`Caption timing '${value}' is invalid.`);
  }
  const result = (((hours * 60) + minutes) * 60 + seconds) * 1_000 + milliseconds;
  if (!Number.isSafeInteger(result)) throw new Error(`Caption timing '${value}' is unbounded.`);
  return result;
}

function cueFromBlock(block: string): CaptionCueDraft | null {
  const lines = block.split("\n").map((line) => line.replace(/\s+$/, ""));
  if (["NOTE", "STYLE", "REGION"].some((kind) => lines[0] === kind || lines[0].startsWith(`${kind} `))) {
    return null;
  }
  const timingIndex = lines.findIndex((line) => line.includes("-->"));
  if (timingIndex < 0) throw new Error("Every imported caption cue requires a valid timing line.");
  const timing = lines[timingIndex].match(
    /^(\d{2,}:\d{2}:\d{2}[.,]\d{3}|\d{2}:\d{2}[.,]\d{3})\s+-->\s+(\d{2,}:\d{2}:\d{2}[.,]\d{3}|\d{2}:\d{2}[.,]\d{3})(?:\s+.*)?$/,
  );
  if (!timing) throw new Error("Every imported caption cue requires a valid timing line.");
  const startMs = timestampMilliseconds(timing[1]);
  const endMs = timestampMilliseconds(timing[2]);
  const durationMs = endMs - startMs;
  if (durationMs < 1 || durationMs > MAX_CUE_DURATION_MS) {
    throw new Error("Caption cue duration must be from 1 millisecond through 60 seconds.");
  }
  const text = lines.slice(timingIndex + 1).join("\n").trim();
  if (!text) throw new Error("Every imported caption cue requires visible text.");
  if (new TextEncoder().encode(text).length > MAX_CUE_TEXT_BYTES) {
    throw new Error("Caption cue text may not exceed 2,048 UTF-8 bytes.");
  }
  if ([...text].some((character) => character !== "\n" && /[\u0000-\u001f\u007f]/.test(character))) {
    throw new Error("Caption cue text may not contain control characters.");
  }
  return { cue_id: crypto.randomUUID(), start_ms: startMs, duration_ms: durationMs, text };
}

export function parseCaptionFile(source: string): CaptionCueDraft[] {
  const normalized = source.replace(/^\uFEFF/, "").replace(/\r\n?/g, "\n").trim();
  if (!normalized) throw new Error("The caption file is empty.");
  const blocks = normalized.split(/\n[\t ]*\n+/);
  if (blocks[0].split("\n")[0].trim().startsWith("WEBVTT")) blocks.shift();
  const cues = blocks.map(cueFromBlock).filter((cue): cue is CaptionCueDraft => cue !== null);
  if (cues.length === 0) throw new Error("The caption file contains no timed cues.");
  if (cues.length > MAX_CUES) throw new Error("A caption track may not exceed 100,000 cues.");
  let priorEndMs = 0;
  cues.forEach((cue, index) => {
    if (index > 0 && cue.start_ms < priorEndMs) {
      throw new Error("Caption cues must be ordered and may not overlap.");
    }
    priorEndMs = cue.start_ms + cue.duration_ms;
  });
  return cues;
}

export function captionTrackMutationReason(
  assetReady: boolean,
  language: string,
  serviceNumber: number,
  label: string,
  cues: CaptionCueDraft[],
  assetDurationMs?: number,
): string | null {
  if (!assetReady) return "Caption authority requires an air-ready media asset.";
  if (language.length < 2 || language.length > 35 || !/^[A-Za-z][A-Za-z0-9-]*[A-Za-z0-9]$/.test(language) || language.includes("--")) {
    return "Enter a bounded caption language tag such as en-US.";
  }
  if (!Number.isInteger(serviceNumber) || serviceNumber < 1 || serviceNumber > 63) {
    return "Choose a CEA-708 service number from 1 through 63.";
  }
  if (!label || label.trim() !== label || new TextEncoder().encode(label).length > MAX_LABEL_BYTES || /[\u0000-\u001f\u007f]/.test(label)) {
    return "Enter a visible caption track label of at most 120 UTF-8 bytes.";
  }
  if (cues.length === 0 || cues.length > MAX_CUES) return "Import a bounded, nonempty caption cue set.";
  if (assetDurationMs !== undefined) {
    const last = cues.at(-1);
    if (!Number.isSafeInteger(assetDurationMs) || assetDurationMs < 1 || !last || last.start_ms + last.duration_ms > assetDurationMs) {
      return "The final caption cue extends beyond this media asset.";
    }
  }
  return null;
}
