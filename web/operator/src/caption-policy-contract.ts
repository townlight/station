export type CaptionMode = "disabled" | "cea708";

export function isCaptionLanguage(value: string): boolean {
  return value.length >= 2
    && value.length <= 35
    && /^[A-Za-z][A-Za-z0-9-]*[A-Za-z0-9]$/.test(value)
    && !value.includes("--");
}

export function captionPolicyMutationReason(
  channelEnabled: boolean,
  serviceNumber: number,
  language: string,
): string | null {
  if (channelEnabled) return "Stop this output before changing its caption policy.";
  if (!Number.isInteger(serviceNumber) || serviceNumber < 1 || serviceNumber > 63) {
    return "Choose a CEA-708 service number from 1 through 63.";
  }
  if (!isCaptionLanguage(language)) {
    return "Enter a bounded caption language tag such as en-US.";
  }
  return null;
}
