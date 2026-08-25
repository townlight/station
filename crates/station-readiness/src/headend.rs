use std::collections::HashSet;

use serde::{Deserialize, Serialize};

use super::{
    CapabilitySupport, PegReadinessColor, ReadinessState, StationBoxProfile, ValidationError,
    is_uuid,
};

const SCHEMA_VERSION: u32 = 1;
const MAX_HEARTBEAT_AGE_MS: i64 = 15_000;
const MAX_TEXT: usize = 1_000;
const DEFAULT_LOUDNESS_TOLERANCE_MILLI: i32 = 2_000;
const MAX_PROOF_SAMPLE_DURATION_MS: u64 = 600_000;
const MAX_PROOF_CAPTURE_BYTES: u64 = 1024 * 1024 * 1024;
const MAX_PROOF_ENGINE_TEXT: usize = 200;
const MAX_PROOF_METHOD_TEXT: usize = 500;
const MAX_PROOF_DETAIL_TEXT: usize = 4_000;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CandidateIdentity {
    pub version: String,
    pub source_commit: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LoudnessProofOutcome {
    Pass,
    Fail,
    NotRun,
    Invalid,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EffectiveLoudnessStatus {
    Pass,
    Fail,
    NotRun,
    Invalid,
    Expired,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReceiverAcceptance {
    NotAttempted,
    Unknown,
    Verified,
    Rejected,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SinkLoudnessProof {
    pub proof_id: String,
    pub channel_id: String,
    pub sink_id: String,
    pub target_lufs_milli: i32,
    pub measured_lufs_milli: Option<i32>,
    pub tolerance_lufs_milli: i32,
    pub outcome: LoudnessProofOutcome,
    pub receiver_acceptance: ReceiverAcceptance,
    pub captured_at_unix_ms: i64,
    pub sample_duration_ms: u64,
    pub capture_bytes: u64,
    pub capture_sha256: String,
    pub candidate_version: String,
    pub source_commit: Option<String>,
    pub configuration_hash: String,
    pub measurement_engine: String,
    pub measurement_method: String,
    pub detail: String,
}

impl SinkLoudnessProof {
    pub fn validate(&self) -> Result<(), ValidationError> {
        if !is_uuid(&self.proof_id) || !is_uuid(&self.channel_id) || !is_uuid(&self.sink_id) {
            return invalid("loudness proof identities must be canonical UUIDs");
        }
        if !is_executable_loudness_target(self.target_lufs_milli)
            || self.tolerance_lufs_milli != DEFAULT_LOUDNESS_TOLERANCE_MILLI
        {
            return invalid("loudness proof target or tolerance is unsupported");
        }
        if self
            .measured_lufs_milli
            .is_some_and(|value| !(-200_000..=20_000).contains(&value))
        {
            return invalid("loudness proof measurement is outside the supported range");
        }
        match (self.outcome, self.measured_lufs_milli) {
            (LoudnessProofOutcome::Pass | LoudnessProofOutcome::Fail, None) => {
                return invalid("a completed loudness proof requires a measurement");
            }
            (LoudnessProofOutcome::NotRun | LoudnessProofOutcome::Invalid, Some(_)) => {
                return invalid(
                    "an incomplete or invalid loudness proof cannot contain a measurement",
                );
            }
            _ => {}
        }
        if matches!(
            self.outcome,
            LoudnessProofOutcome::Pass | LoudnessProofOutcome::Fail
        ) && derive_loudness_outcome(
            self.target_lufs_milli,
            self.measured_lufs_milli,
            self.tolerance_lufs_milli,
        ) != self.outcome
        {
            return invalid("loudness proof outcome does not match its measurement");
        }
        if self.captured_at_unix_ms < 0
            || self.sample_duration_ms == 0
            || self.sample_duration_ms > MAX_PROOF_SAMPLE_DURATION_MS
            || self.capture_bytes == 0
            || self.capture_bytes > MAX_PROOF_CAPTURE_BYTES
            || !is_sha256_hash(&self.capture_sha256)
            || !is_sha256_hash(&self.configuration_hash)
        {
            return invalid("loudness proof capture facts are invalid or unbounded");
        }
        visible_bounded(
            &self.candidate_version,
            "loudness proof candidate version",
            MAX_PROOF_ENGINE_TEXT,
        )?;
        if let Some(source_commit) = &self.source_commit
            && !is_source_commit(source_commit)
        {
            return invalid("loudness proof source commit must be a lowercase SHA-1 identity");
        }
        visible_bounded(
            &self.measurement_engine,
            "loudness proof measurement engine",
            MAX_PROOF_ENGINE_TEXT,
        )?;
        visible_bounded(
            &self.measurement_method,
            "loudness proof measurement method",
            MAX_PROOF_METHOD_TEXT,
        )?;
        visible_bounded(&self.detail, "loudness proof detail", MAX_PROOF_DETAIL_TEXT)
    }

    pub fn effective_status(
        &self,
        current_candidate: &CandidateIdentity,
        current_configuration_hash: &str,
        now_unix_ms: i64,
        max_age_ms: i64,
    ) -> EffectiveLoudnessStatus {
        if self.validate().is_err()
            || now_unix_ms < self.captured_at_unix_ms
            || max_age_ms <= 0
            || !is_sha256_hash(current_configuration_hash)
            || current_candidate.source_commit.is_none()
            || !current_candidate
                .source_commit
                .as_deref()
                .is_some_and(is_source_commit)
            || self.candidate_version != current_candidate.version
            || self.source_commit != current_candidate.source_commit
            || self.configuration_hash != current_configuration_hash
            || now_unix_ms - self.captured_at_unix_ms > max_age_ms
        {
            return EffectiveLoudnessStatus::Expired;
        }
        match self.outcome {
            LoudnessProofOutcome::Pass => EffectiveLoudnessStatus::Pass,
            LoudnessProofOutcome::Fail => EffectiveLoudnessStatus::Fail,
            LoudnessProofOutcome::NotRun => EffectiveLoudnessStatus::NotRun,
            LoudnessProofOutcome::Invalid => EffectiveLoudnessStatus::Invalid,
        }
    }

    pub fn claims_receiver_acceptance(&self) -> bool {
        self.outcome == LoudnessProofOutcome::Pass
            && self.receiver_acceptance == ReceiverAcceptance::Verified
    }
}

pub fn derive_loudness_outcome(
    target_lufs_milli: i32,
    measured_lufs_milli: Option<i32>,
    tolerance_lufs_milli: i32,
) -> LoudnessProofOutcome {
    if !is_executable_loudness_target(target_lufs_milli)
        || tolerance_lufs_milli != DEFAULT_LOUDNESS_TOLERANCE_MILLI
    {
        return LoudnessProofOutcome::Invalid;
    }
    let Some(measured_lufs_milli) = measured_lufs_milli else {
        return LoudnessProofOutcome::NotRun;
    };
    if !(-200_000..=20_000).contains(&measured_lufs_milli) {
        return LoudnessProofOutcome::Invalid;
    }
    let distance = i64::from(measured_lufs_milli) - i64::from(target_lufs_milli);
    if distance.abs() <= i64::from(tolerance_lufs_milli) {
        LoudnessProofOutcome::Pass
    } else {
        LoudnessProofOutcome::Fail
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HeadendProbeInput {
    pub generated_at_unix_ms: i64,
    pub channels: Vec<HeadendChannelProbeInput>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HeadendChannelProbeInput {
    pub channel_id: String,
    pub display_name: String,
    pub enabled: bool,
    pub profile: HeadendProfileProbeInput,
    pub sinks: Vec<HeadendSinkProbeInput>,
    pub runtime: ChannelOutputRuntimeProbe,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HeadendProfileProbeInput {
    pub profile_id: String,
    pub width: u32,
    pub height: u32,
    pub frame_rate_numerator: u32,
    pub frame_rate_denominator: u32,
    pub interlaced: bool,
    pub video_bitrate_bps: u32,
    pub audio_bitrate_bps: u32,
    pub transport_bitrate_bps: u64,
    pub audio_sample_rate_hz: u32,
    pub loudness_target_lufs: f32,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HeadendSinkProbeInput {
    pub sink_id: String,
    pub label: String,
    pub kind: HeadendSinkKind,
    pub required: bool,
    pub enabled: bool,
    pub external_receiver: ExternalReceiverState,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ChannelOutputRuntimeProbe {
    pub state: ReadinessState,
    pub heartbeat_age_ms: Option<i64>,
    pub evidence: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum HeadendSinkKind {
    UdpTs,
    Srt,
    Sdi,
    Ndi,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExternalReceiverState {
    Verified,
    NotVerified,
    Unknown,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HeadendReadiness {
    pub schema_version: u32,
    pub generated_at_unix_ms: i64,
    pub channels: Vec<ChannelHeadendReadiness>,
    pub rollup: HeadendReadinessRollup,
    pub detail: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ChannelHeadendReadiness {
    pub channel_id: String,
    pub display_name: String,
    pub enabled: bool,
    pub profile_id: String,
    pub profile_state: ReadinessState,
    pub sinks: Vec<SinkHeadendReadiness>,
    pub state: ReadinessState,
    pub detail: String,
    pub evidence: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SinkHeadendReadiness {
    pub sink_id: String,
    pub label: String,
    pub kind: HeadendSinkKind,
    pub required: bool,
    pub enabled: bool,
    pub capability: CapabilitySupport,
    pub local_transport: ReadinessState,
    pub external_receiver: ExternalReceiverState,
    pub state: ReadinessState,
    pub detail: String,
    pub evidence: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HeadendReadinessRollup {
    pub color: PegReadinessColor,
    pub enabled_channels: usize,
    pub passing_channels: usize,
    pub blocked_channels: usize,
    pub detail: String,
}

pub fn derive_headend_readiness(
    station_box: &StationBoxProfile,
    input: HeadendProbeInput,
) -> Result<HeadendReadiness, ValidationError> {
    validate_input(&input)?;
    if station_box.generated_at_unix_ms > input.generated_at_unix_ms {
        return invalid("StationBox evidence cannot be newer than headend readiness");
    }

    let mut channels = Vec::with_capacity(input.channels.len());
    for channel in input.channels {
        channels.push(derive_channel(station_box, channel));
    }

    let enabled_channels = channels.iter().filter(|channel| channel.enabled).count();
    let passing_channels = channels
        .iter()
        .filter(|channel| channel.enabled && channel.state == ReadinessState::Pass)
        .count();
    let blocked_channels = channels
        .iter()
        .filter(|channel| channel.enabled && channel.state == ReadinessState::Blocked)
        .count();
    let color = if enabled_channels == 0 || blocked_channels > 0 {
        PegReadinessColor::Red
    } else if passing_channels == enabled_channels {
        PegReadinessColor::Green
    } else {
        PegReadinessColor::Yellow
    };
    let detail = if enabled_channels == 0 {
        "No enabled channel has an output configured.".into()
    } else {
        format!(
            "{passing_channels} of {enabled_channels} enabled channels have verified receiver acceptance; {blocked_channels} are blocked."
        )
    };
    Ok(HeadendReadiness {
        schema_version: SCHEMA_VERSION,
        generated_at_unix_ms: input.generated_at_unix_ms,
        channels,
        rollup: HeadendReadinessRollup {
            color,
            enabled_channels,
            passing_channels,
            blocked_channels,
            detail: detail.clone(),
        },
        detail,
    })
}

fn derive_channel(
    station_box: &StationBoxProfile,
    input: HeadendChannelProbeInput,
) -> ChannelHeadendReadiness {
    if !input.enabled {
        return ChannelHeadendReadiness {
            channel_id: input.channel_id,
            display_name: input.display_name,
            enabled: false,
            profile_id: input.profile.profile_id,
            profile_state: ReadinessState::Optional,
            sinks: input
                .sinks
                .into_iter()
                .map(|sink| disabled_sink(sink, &input.runtime.evidence))
                .collect(),
            state: ReadinessState::Optional,
            detail: "The channel is disabled and does not affect headend readiness.".into(),
            evidence: "authoritative channel configuration".into(),
        };
    }

    let station_identity_ready = station_box.station_id.is_some()
        && station_box.station_name.is_some()
        && station_box.timezone.is_some();
    let profile_state = if !station_identity_ready
        || input.profile.profile_id == "proof-360p30"
        || station_box.engine.state == ReadinessState::Blocked
    {
        ReadinessState::Blocked
    } else if station_box.engine.state != ReadinessState::Pass {
        station_box.engine.state.clone()
    } else if input.profile.interlaced {
        capability_state(&station_box.engine.interlaced)
    } else {
        ReadinessState::Pass
    };
    let runtime_state = effective_runtime_state(&input.runtime);
    let mut sinks = Vec::with_capacity(input.sinks.len());
    for sink in input.sinks {
        sinks.push(derive_sink(
            station_box,
            sink,
            &profile_state,
            &runtime_state,
            &input.runtime.evidence,
        ));
    }

    let enabled_sinks = sinks.iter().filter(|sink| sink.enabled).count();
    let required_sinks = sinks
        .iter()
        .filter(|sink| sink.enabled && sink.required)
        .collect::<Vec<_>>();
    let state = if enabled_sinks == 0
        || profile_state == ReadinessState::Blocked
        || runtime_state == ReadinessState::Blocked
        || required_sinks
            .iter()
            .any(|sink| sink.state == ReadinessState::Blocked)
    {
        ReadinessState::Blocked
    } else if required_sinks
        .iter()
        .any(|sink| sink.state != ReadinessState::Pass)
        || sinks
            .iter()
            .any(|sink| sink.enabled && sink.state != ReadinessState::Pass)
    {
        ReadinessState::Unknown
    } else {
        ReadinessState::Pass
    };

    let mut blockers = Vec::new();
    if input.profile.interlaced && profile_state != ReadinessState::Pass {
        blockers.push("interlaced output is unimplemented or unavailable".to_string());
    }
    if input.profile.profile_id == "proof-360p30" {
        blockers.push("the selected profile is for local proof, not headend delivery".to_string());
    }
    if !station_identity_ready {
        blockers.push("station identity and timezone are not commissioned".to_string());
    }
    if runtime_state == ReadinessState::Blocked {
        blockers.push("local worker output evidence is missing, stale, or failed".to_string());
    }
    for sink in &sinks {
        if sink.enabled && sink.state == ReadinessState::Blocked {
            blockers.push(sink.detail.clone());
        }
    }
    let detail = match state {
        ReadinessState::Pass => {
            "Every enabled output has fresh local progress and verified receiver acceptance.".into()
        }
        ReadinessState::Blocked => format!("Channel output is blocked: {}.", blockers.join("; ")),
        _ => "Local output is advancing, but receiver acceptance is not verified.".into(),
    };
    ChannelHeadendReadiness {
        channel_id: input.channel_id,
        display_name: input.display_name,
        enabled: true,
        profile_id: input.profile.profile_id,
        profile_state,
        sinks,
        state,
        detail,
        evidence: input.runtime.evidence,
    }
}

fn derive_sink(
    station_box: &StationBoxProfile,
    input: HeadendSinkProbeInput,
    profile_state: &ReadinessState,
    runtime_state: &ReadinessState,
    runtime_evidence: &str,
) -> SinkHeadendReadiness {
    if !input.enabled {
        return disabled_sink(input, runtime_evidence);
    }
    let capability = match input.kind {
        HeadendSinkKind::UdpTs => station_box.engine.udp.clone(),
        HeadendSinkKind::Srt => station_box.engine.srt.clone(),
        HeadendSinkKind::Sdi => station_box.engine.sdi.clone(),
        HeadendSinkKind::Ndi => station_box.engine.ndi.clone(),
    };
    let capability_readiness = capability_state(&capability);
    let local_transport = if *profile_state == ReadinessState::Blocked
        || capability_readiness == ReadinessState::Blocked
        || *runtime_state == ReadinessState::Blocked
    {
        ReadinessState::Blocked
    } else if *profile_state == ReadinessState::Pass
        && capability_readiness == ReadinessState::Pass
        && *runtime_state == ReadinessState::Pass
    {
        ReadinessState::Pass
    } else {
        ReadinessState::Unknown
    };
    let state = if local_transport == ReadinessState::Blocked {
        ReadinessState::Blocked
    } else if local_transport == ReadinessState::Pass
        && input.external_receiver == ExternalReceiverState::Verified
    {
        ReadinessState::Pass
    } else {
        ReadinessState::Unknown
    };
    let detail = if state == ReadinessState::Pass {
        "Local transport progress and external receiver acceptance are verified.".into()
    } else if capability == CapabilitySupport::Unimplemented {
        format!("{} output is unimplemented.", sink_name(input.kind))
    } else if capability == CapabilitySupport::Unsupported {
        format!(
            "{} output is unsupported on this station.",
            sink_name(input.kind)
        )
    } else if capability == CapabilitySupport::Unknown {
        format!("{} output capability is unknown.", sink_name(input.kind))
    } else if local_transport == ReadinessState::Blocked {
        format!(
            "{} local transport is not advancing.",
            sink_name(input.kind)
        )
    } else {
        "Local transport is advancing; external receiver acceptance is not verified.".into()
    };
    SinkHeadendReadiness {
        sink_id: input.sink_id,
        label: input.label,
        kind: input.kind,
        required: input.required,
        enabled: true,
        capability,
        local_transport,
        external_receiver: input.external_receiver,
        state,
        detail,
        evidence: runtime_evidence.to_string(),
    }
}

fn disabled_sink(input: HeadendSinkProbeInput, runtime_evidence: &str) -> SinkHeadendReadiness {
    SinkHeadendReadiness {
        sink_id: input.sink_id,
        label: input.label,
        kind: input.kind,
        required: input.required,
        enabled: false,
        capability: CapabilitySupport::Unknown,
        local_transport: ReadinessState::Optional,
        external_receiver: input.external_receiver,
        state: ReadinessState::Optional,
        detail: "The output is disabled and does not affect readiness.".into(),
        evidence: runtime_evidence.to_string(),
    }
}

fn effective_runtime_state(runtime: &ChannelOutputRuntimeProbe) -> ReadinessState {
    match (runtime.state.clone(), runtime.heartbeat_age_ms) {
        (ReadinessState::Pass, Some(age)) if age <= MAX_HEARTBEAT_AGE_MS => ReadinessState::Pass,
        (_, None) | (ReadinessState::Pass, _) | (ReadinessState::Blocked, _) => {
            ReadinessState::Blocked
        }
        (state, _) => state,
    }
}

fn capability_state(capability: &CapabilitySupport) -> ReadinessState {
    match capability {
        CapabilitySupport::Supported => ReadinessState::Pass,
        CapabilitySupport::Unsupported | CapabilitySupport::Unimplemented => {
            ReadinessState::Blocked
        }
        CapabilitySupport::Unknown => ReadinessState::Unknown,
    }
}

fn sink_name(kind: HeadendSinkKind) -> &'static str {
    match kind {
        HeadendSinkKind::UdpTs => "UDP transport",
        HeadendSinkKind::Srt => "SRT",
        HeadendSinkKind::Sdi => "SDI",
        HeadendSinkKind::Ndi => "NDI",
    }
}

fn validate_input(input: &HeadendProbeInput) -> Result<(), ValidationError> {
    if input.generated_at_unix_ms < 0 {
        return invalid("headend readiness timestamp must be nonnegative");
    }
    if input.channels.len() > 64 {
        return invalid("headend readiness cannot contain more than 64 channels");
    }
    let mut channel_ids = HashSet::new();
    for channel in &input.channels {
        if !is_uuid(&channel.channel_id) || !channel_ids.insert(channel.channel_id.as_str()) {
            return invalid("headend channel identities must be unique canonical UUIDs");
        }
        visible(&channel.display_name, "channel display name")?;
        validate_profile(&channel.profile)?;
        validate_runtime(&channel.runtime)?;
        if channel.sinks.len() > 64 {
            return invalid("a headend channel cannot contain more than 64 sinks");
        }
        let mut sink_ids = HashSet::new();
        for sink in &channel.sinks {
            if !is_uuid(&sink.sink_id) || !sink_ids.insert(sink.sink_id.as_str()) {
                return invalid(
                    "headend sink identities must be unique canonical UUIDs per channel",
                );
            }
            visible(&sink.label, "headend sink label")?;
        }
    }
    Ok(())
}

fn validate_profile(input: &HeadendProfileProbeInput) -> Result<(), ValidationError> {
    if input.width == 0
        || input.height == 0
        || input.frame_rate_numerator == 0
        || input.frame_rate_denominator == 0
        || input.video_bitrate_bps == 0
        || input.audio_bitrate_bps == 0
        || input.transport_bitrate_bps
            <= u64::from(input.video_bitrate_bps) + u64::from(input.audio_bitrate_bps)
        || input.audio_sample_rate_hz != 48_000
        || !input.loudness_target_lufs.is_finite()
        || !(-70.0..=-5.0).contains(&input.loudness_target_lufs)
    {
        return invalid("headend profile contains an impossible transport contract");
    }
    let expected = match input.profile_id.as_str() {
        "proof-360p30" => (640, 360, 30, 1, false, 1_000_000, 128_000, 1_500_000, -16.0),
        "atsc-720p5994" => (
            1_280, 720, 60_000, 1_001, false, 6_000_000, 192_000, 8_000_000, -24.0,
        ),
        "atsc-1080i5994" => (
            1_920, 1_080, 30_000, 1_001, true, 10_000_000, 384_000, 12_000_000, -24.0,
        ),
        "ntsc-480i2997" => (
            720, 480, 30_000, 1_001, true, 3_180_000, 192_000, 3_750_000, -24.0,
        ),
        _ => return invalid("headend profile identity is unknown"),
    };
    let actual = (
        input.width,
        input.height,
        input.frame_rate_numerator,
        input.frame_rate_denominator,
        input.interlaced,
        input.video_bitrate_bps,
        input.audio_bitrate_bps,
        input.transport_bitrate_bps,
        input.loudness_target_lufs,
    );
    if actual != expected {
        return invalid("headend profile facts do not match the named product contract");
    }
    Ok(())
}

fn validate_runtime(input: &ChannelOutputRuntimeProbe) -> Result<(), ValidationError> {
    if input.heartbeat_age_ms.is_some_and(|age| age < 0)
        || (input.state == ReadinessState::Pass && input.heartbeat_age_ms.is_none())
    {
        return invalid("headend runtime heartbeat evidence is inconsistent");
    }
    visible(&input.evidence, "headend runtime evidence")
}

fn visible(value: &str, field: &str) -> Result<(), ValidationError> {
    if value.trim().is_empty() || value.len() > MAX_TEXT || value.chars().any(char::is_control) {
        return invalid(format!("{field} must contain visible bounded text"));
    }
    Ok(())
}

fn visible_bounded(value: &str, field: &str, max_length: usize) -> Result<(), ValidationError> {
    if value.trim().is_empty() || value.len() > max_length || value.chars().any(char::is_control) {
        return invalid(format!("{field} must contain visible bounded text"));
    }
    Ok(())
}

fn is_executable_loudness_target(value: i32) -> bool {
    value == -24_000 || value == -16_000
}

fn is_sha256_hash(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn is_source_commit(value: &str) -> bool {
    value.len() == 40
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn invalid<T>(message: impl Into<String>) -> Result<T, ValidationError> {
    Err(ValidationError::InvalidInput(message.into()))
}
