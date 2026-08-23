export type HeadendProfileExecutionState = "runnable" | "staged_only" | "unimplemented";
export type ReadinessState = "pass" | "blocked" | "recoverable" | "optional" | "unknown";
export type ExternalReceiverState = "verified" | "not_verified" | "unknown";

export interface HeadendProfile {
  profile_id: string;
  label: string;
  vendor: string;
  source_urls: string[];
  canonical_profile: string;
  muxrate_kbps: number;
  transport: "udp-unicast" | "udp-multicast" | "file-drop";
  packet_size_bytes: number;
  minimum_port: number;
  operator_must_supply: string[];
  not_claimed: string[];
  execution_state: HeadendProfileExecutionState;
  execution_detail: string;
}

export interface SinkHeadendReadiness {
  sink_id: string;
  label: string;
  kind: "udp_ts" | "srt" | "sdi" | "ndi";
  required: boolean;
  enabled: boolean;
  capability: "supported" | "unsupported" | "unimplemented" | "unknown";
  local_transport: ReadinessState;
  external_receiver: ExternalReceiverState;
  state: ReadinessState;
  detail: string;
  evidence: string;
}

export interface ChannelHeadendReadiness {
  channel_id: string;
  display_name: string;
  enabled: boolean;
  profile_id: string;
  profile_state: ReadinessState;
  sinks: SinkHeadendReadiness[];
  state: ReadinessState;
  detail: string;
  evidence: string;
}

export interface HeadendReadiness {
  schema_version: number;
  generated_at_unix_ms: number;
  channels: ChannelHeadendReadiness[];
  rollup: {
    color: "green" | "yellow" | "red";
    enabled_channels: number;
    passing_channels: number;
    blocked_channels: number;
    detail: string;
  };
  detail: string;
}

export function canApplyHeadendProfile(executionState: HeadendProfileExecutionState, channelEnabled: boolean): boolean {
  return executionState !== "unimplemented" && !channelEnabled;
}

export function canStartHeadendProfile(executionState: HeadendProfileExecutionState | null): boolean {
  return executionState === "runnable";
}

export function readinessLabel(state: ReadinessState): string {
  switch (state) {
    case "pass": return "Verified locally";
    case "blocked": return "Blocked locally";
    case "recoverable": return "Local action required";
    case "optional": return "Not active";
    case "unknown": return "Local proof pending";
  }
}

export function receiverAcceptanceLabel(state: ExternalReceiverState): string {
  switch (state) {
    case "verified": return "Verified by receiver";
    case "not_verified": return "Not verified by receiver";
    case "unknown": return "Receiver acceptance unknown";
  }
}
