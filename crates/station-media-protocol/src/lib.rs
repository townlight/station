use serde::{Deserialize, Deserializer, Serialize, de::DeserializeOwned};
use std::net::IpAddr;
use std::num::NonZeroU16;
use std::path::Path;

/// A finite, quantized loudness target carried with one output branch.
///
/// The wire representation remains the familiar `loudness_target_lufs` decimal,
/// while the authority is an integer number of milli-LUFS so NaN and infinity
/// cannot enter the worker contract or compare ambiguously.
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct LoudnessTarget(i32);

impl LoudnessTarget {
    pub const CABLE: Self = Self(-24_000);
    pub const STREAMING: Self = Self(-16_000);

    pub fn from_lufs(value: f64) -> Result<Self, &'static str> {
        if !value.is_finite() {
            return Err("loudness target must be finite");
        }
        let milli = (value * 1_000.0).round();
        if !(-70_000.0..=-5_000.0).contains(&milli) {
            return Err("loudness target must be between -70 and -5 LUFS");
        }
        if (value * 1_000.0 - milli).abs() > 1e-6 {
            return Err("loudness target must have at most three decimal places");
        }
        Ok(Self(milli as i32))
    }

    pub const fn from_milli_lufs(value: i32) -> Result<Self, &'static str> {
        if value < -70_000 || value > -5_000 {
            return Err("loudness target must be between -70 and -5 LUFS");
        }
        Ok(Self(value))
    }

    pub const fn milli_lufs(self) -> i32 {
        self.0
    }

    pub const fn as_lufs(self) -> f64 {
        self.0 as f64 / 1_000.0
    }
}

impl std::fmt::Debug for LoudnessTarget {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_tuple("LoudnessTarget")
            .field(&self.as_lufs())
            .finish()
    }
}

impl Serialize for LoudnessTarget {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        serializer.serialize_f64(self.as_lufs())
    }
}

impl<'de> Deserialize<'de> for LoudnessTarget {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let value = f64::deserialize(deserializer)?;
        Self::from_lufs(value).map_err(serde::de::Error::custom)
    }
}

pub const MAX_FRAME_BYTES: usize = 65_536;
pub const PROTOCOL_VERSION: ProtocolVersion = ProtocolVersion { major: 3, minor: 0 };

pub const MAX_OUTPUT_PROOF_INDEX: u16 = 255;
// Operator-requested analyzable program is capped at 300 seconds. The worker
// protocol permits two additional seconds so a bounded MPEG-TS capture can
// include enough leading transport context for independent decode.
pub const MAX_OUTPUT_PROOF_DURATION_MS: u64 = 302_000;
pub const MIN_OUTPUT_PROOF_DURATION_MS: u64 = 3_000;
pub const MAX_OUTPUT_PROOF_BYTES: u64 = 256 * 1024 * 1024;
const MAX_OUTPUT_PROOF_ARTIFACT_PATH_BYTES: usize = 2_048;
const MAX_OUTPUT_PROOF_TEXT_BYTES: usize = 512;

const MIN_LIVE_TOKEN_BYTES: usize = 16;
const MAX_LIVE_TOKEN_BYTES: usize = 128;
const MAX_LIVE_ID_BYTES: usize = 128;

pub const MAX_CG_SLOTS: usize = 8;
pub const MAX_CG_TEXT_BYTES: usize = 2048;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CgRect {
    pub x: u16,
    pub y: u16,
    pub width: u16,
    pub height: u16,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum CgOverlayContent {
    Text {
        text: String,
    },
    Clock {
        format: String,
    },
    Image {
        asset_id: String,
        format: CgImageFormat,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CgImageFormat {
    Png,
    Jpeg,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CgOverlay {
    pub slot: u8,
    pub rect: CgRect,
    pub z_index: i16,
    pub content: CgOverlayContent,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CgSnapshot {
    pub board_id: String,
    pub board_revision: u64,
    pub overlays: Vec<CgOverlay>,
}

#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(transparent)]
pub struct SrtPassphrase(String);

impl SrtPassphrase {
    pub fn new(value: String) -> Result<Self, &'static str> {
        if !(10..=79).contains(&value.len())
            || !value.bytes().all(|byte| (0x21..=0x7e).contains(&byte))
        {
            return Err(
                "SRT passphrases must contain 10 to 79 printable ASCII characters without spaces",
            );
        }
        Ok(Self(value))
    }

    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Debug for SrtPassphrase {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("SrtPassphrase([REDACTED])")
    }
}

/// A short-lived bearer capability reused for the arm-to-single-take handshake,
/// then consumed. Its value is intentionally omitted from debug output.
#[derive(Clone, PartialEq, Eq, Serialize)]
#[serde(transparent)]
pub struct LiveArmToken(String);

impl LiveArmToken {
    pub fn new(value: String) -> Result<Self, &'static str> {
        validate_live_token(&value).map(|()| Self(value))
    }

    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Debug for LiveArmToken {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("LiveArmToken([REDACTED])")
    }
}

impl<'de> Deserialize<'de> for LiveArmToken {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let value = String::deserialize(deserializer)?;
        Self::new(value).map_err(serde::de::Error::custom)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct LiveInputConfiguration {
    pub session_id: String,
    pub source_id: String,
    pub source_revision: u64,
    pub bind_address: String,
    pub port: NonZeroU16,
}

impl LiveInputConfiguration {
    pub fn new(
        session_id: String,
        source_id: String,
        source_revision: u64,
        bind_address: String,
        port: NonZeroU16,
    ) -> Result<Self, &'static str> {
        let configuration = Self {
            session_id,
            source_id,
            source_revision,
            bind_address,
            port,
        };
        configuration.validate()?;
        Ok(configuration)
    }

    fn validate(&self) -> Result<(), &'static str> {
        validate_live_id(&self.session_id, "session_id")?;
        validate_live_id(&self.source_id, "source_id")?;
        if self.source_revision == 0 {
            return Err("live source revision must be nonzero");
        }
        self.bind_address
            .parse::<IpAddr>()
            .map_err(|_| "live bind address must be an IP address")?;
        Ok(())
    }
}

impl<'de> Deserialize<'de> for LiveInputConfiguration {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        #[derive(Deserialize)]
        struct Wire {
            session_id: String,
            source_id: String,
            source_revision: u64,
            bind_address: String,
            port: NonZeroU16,
        }
        let wire = Wire::deserialize(deserializer)?;
        Self::new(
            wire.session_id,
            wire.source_id,
            wire.source_revision,
            wire.bind_address,
            wire.port,
        )
        .map_err(serde::de::Error::custom)
    }
}

fn validate_live_token(value: &str) -> Result<(), &'static str> {
    if !(MIN_LIVE_TOKEN_BYTES..=MAX_LIVE_TOKEN_BYTES).contains(&value.len())
        || !value.bytes().all(|byte| (0x21..=0x7e).contains(&byte))
    {
        return Err(
            "live arm tokens must contain 16 to 128 printable ASCII characters without spaces",
        );
    }
    Ok(())
}

fn validate_live_id(value: &str, field: &'static str) -> Result<(), &'static str> {
    let bytes = value.as_bytes();
    if bytes.len() != 36
        || bytes.len() > MAX_LIVE_ID_BYTES
        || ![8, 13, 18, 23].iter().all(|&index| bytes[index] == b'-')
        || bytes
            .iter()
            .enumerate()
            .any(|(index, byte)| !matches!(index, 8 | 13 | 18 | 23) && !byte.is_ascii_hexdigit())
    {
        return Err(match field {
            "session_id" => "live session_id must be a canonical UUID",
            _ => "live source_id must be a canonical UUID",
        });
    }
    Ok(())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProtocolVersion {
    pub major: u16,
    pub minor: u16,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkerBootstrapEnvelope {
    pub version: ProtocolVersion,
    pub worker_id: String,
    pub channel_id: String,
    pub output_profile: String,
    /// Station-managed root used by the worker to resolve CG image identities.
    /// Overlay commands carry only an asset id and format, never a path.
    pub cg_image_root: String,
    /// Station-managed root under which the worker writes output-proof artifacts.
    pub proof_artifact_root: String,
    pub outputs: Vec<WorkerOutputConfiguration>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "kebab-case", deny_unknown_fields)]
pub enum WorkerOutputConfiguration {
    UdpTs {
        sink_id: String,
        destination: String,
        loudness_target_lufs: LoudnessTarget,
    },
    Srt {
        sink_id: String,
        uri: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        passphrase: Option<SrtPassphrase>,
        loudness_target_lufs: LoudnessTarget,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CommandEnvelope {
    pub version: ProtocolVersion,
    pub channel_id: String,
    pub command_id: String,
    pub expected_sequence: u64,
    pub command: ChannelCommand,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum ChannelCommand {
    Ping,
    ApplyPlan {
        plan_id: String,
        revision: u64,
    },
    ApplyCgSnapshot {
        snapshot: CgSnapshot,
    },
    ClearCgSnapshot,
    LoadAsset {
        asset_id: String,
        media_path: String,
    },
    TakeAsset {
        asset_id: String,
        media_offset_ms: u64,
    },
    ArmLive {
        input: LiveInputConfiguration,
        arm_token: LiveArmToken,
    },
    TakeLive {
        session_id: String,
        arm_token: LiveArmToken,
    },
    ReturnToSchedule,
    ReturnFromLive {
        session_id: String,
        target: LiveReturnTarget,
    },
    DisarmLive {
        session_id: String,
    },
    StartOutputProof {
        proof_id: String,
        output_index: u16,
        sink_id: String,
        loudness_target_lufs: LoudnessTarget,
        duration_ms: u64,
        max_bytes: u64,
    },
    PollOutputProof {
        proof_id: String,
    },
    AbortOutputProof {
        proof_id: String,
    },
    Shutdown,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum LiveReturnTarget {
    Fallback,
    Program {
        asset_id: String,
        media_offset_ms: u64,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkerEventEnvelope {
    pub version: ProtocolVersion,
    pub worker_id: String,
    pub channel_id: String,
    pub sequence: u64,
    pub event: WorkerEvent,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OutputTransportObservation {
    pub output_index: u16,
    pub bytes_sent_total: u64,
    pub audio_buffers_total: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum WorkerEvent {
    Ready {
        graph_revision: u64,
    },
    OnAirChanged {
        source_kind: String,
        source_id: String,
    },
    LiveArmed {
        session_id: String,
        source_id: String,
        source_revision: u64,
    },
    LiveOnAir {
        session_id: String,
        source_id: String,
        source_revision: u64,
    },
    LiveReturned {
        session_id: String,
        source_kind: String,
        source_id: String,
    },
    LiveDisarmed {
        session_id: String,
    },
    AssetLoaded {
        asset_id: String,
    },
    CgApplied {
        board_id: String,
        board_revision: u64,
    },
    CgCleared,
    CommandRejected {
        command_id: String,
        code: String,
        message: String,
    },
    Heartbeat {
        monotonic_milliseconds: u64,
        outputs: Vec<OutputTransportObservation>,
    },
    ShutdownComplete,
    OutputProofStarted {
        proof_id: String,
        output_index: u16,
        sink_id: String,
        loudness_target_lufs: LoudnessTarget,
        duration_ms: u64,
        max_bytes: u64,
        artifact_relative_path: String,
        worker_instance_id: String,
        graph_revision: u64,
    },
    OutputProofPending {
        proof_id: String,
        artifact_relative_path: String,
        worker_instance_id: String,
        graph_revision: u64,
    },
    OutputProofReady {
        proof_id: String,
        output_index: u16,
        sink_id: String,
        loudness_target_lufs: LoudnessTarget,
        artifact_relative_path: String,
        worker_instance_id: String,
        graph_revision: u64,
        size_bytes: u64,
        capture_sha256: String,
        duration_ms: u64,
    },
    OutputProofFailed {
        proof_id: String,
        artifact_relative_path: String,
        worker_instance_id: String,
        graph_revision: u64,
        code: String,
        message: String,
    },
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips_cg_snapshot_command() {
        let command = CommandEnvelope {
            version: PROTOCOL_VERSION,
            channel_id: "channel-1".into(),
            command_id: "command-1".into(),
            expected_sequence: 4,
            command: ChannelCommand::ApplyCgSnapshot {
                snapshot: CgSnapshot {
                    board_id: "board-1".into(),
                    board_revision: 7,
                    overlays: vec![CgOverlay {
                        slot: 0,
                        rect: CgRect {
                            x: 10,
                            y: 700,
                            width: 800,
                            height: 200,
                        },
                        z_index: 1,
                        content: CgOverlayContent::Text {
                            text: "hello".into(),
                        },
                    }],
                },
            },
        };
        let decoded = decode_command_frame(&encode_command_frame(&command).unwrap()).unwrap();
        assert_eq!(decoded, command);
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProtocolError {
    FrameTooShort,
    FrameTooLarge(usize),
    LengthMismatch { declared: usize, actual: usize },
    InvalidJson(String),
    UnsupportedMajor(u16),
    InvalidManagedCgImageRoot(String),
    InvalidManagedProofRoot(String),
    InvalidLiveCommand(String),
    InvalidLiveEvent(String),
    InvalidOutputProof(String),
}

pub fn encode_command_frame(command: &CommandEnvelope) -> Result<Vec<u8>, ProtocolError> {
    validate_live_command(&command.command).map_err(ProtocolError::InvalidLiveCommand)?;
    validate_output_proof_command(&command.command).map_err(ProtocolError::InvalidOutputProof)?;
    encode_frame(command)
}

pub fn encode_bootstrap_frame(
    bootstrap: &WorkerBootstrapEnvelope,
) -> Result<Vec<u8>, ProtocolError> {
    validate_cg_image_root(&bootstrap.cg_image_root)
        .map_err(ProtocolError::InvalidManagedCgImageRoot)?;
    validate_proof_artifact_root(&bootstrap.proof_artifact_root)
        .map_err(ProtocolError::InvalidManagedProofRoot)?;
    validate_bootstrap_outputs(bootstrap).map_err(ProtocolError::InvalidOutputProof)?;
    encode_frame(bootstrap)
}

pub fn decode_bootstrap_frame(frame: &[u8]) -> Result<WorkerBootstrapEnvelope, ProtocolError> {
    let bootstrap: WorkerBootstrapEnvelope = decode_frame(frame)?;
    if bootstrap.version.major != PROTOCOL_VERSION.major {
        return Err(ProtocolError::UnsupportedMajor(bootstrap.version.major));
    }
    validate_cg_image_root(&bootstrap.cg_image_root)
        .map_err(ProtocolError::InvalidManagedCgImageRoot)?;
    validate_proof_artifact_root(&bootstrap.proof_artifact_root)
        .map_err(ProtocolError::InvalidManagedProofRoot)?;
    validate_bootstrap_outputs(&bootstrap).map_err(ProtocolError::InvalidOutputProof)?;
    Ok(bootstrap)
}

fn validate_cg_image_root(root: &str) -> Result<(), String> {
    let trimmed = root.trim_end_matches(['\\', '/']);
    if trimmed.is_empty()
        || !Path::new(root).is_absolute()
        || root.chars().any(char::is_control)
        || trimmed.rsplit(['\\', '/']).next() != Some("cg-images")
        || root.split(['\\', '/']).any(|component| component == "..")
    {
        return Err("CG image root must be a safe managed cg-images directory".into());
    }
    Ok(())
}

fn validate_proof_artifact_root(root: &str) -> Result<(), String> {
    let trimmed = root.trim_end_matches(['\\', '/']);
    if trimmed.is_empty()
        || !Path::new(root).is_absolute()
        || root.chars().any(char::is_control)
        || trimmed.rsplit(['\\', '/']).next() != Some("output-proofs")
        || root.split(['\\', '/']).any(|component| component == "..")
    {
        return Err("proof artifact root must be a safe managed output-proofs directory".into());
    }
    Ok(())
}

fn validate_canonical_uuid(value: &str, field: &str) -> Result<(), String> {
    let bytes = value.as_bytes();
    if bytes.len() != 36
        || [8, 13, 18, 23].iter().any(|&index| bytes[index] != b'-')
        || bytes.iter().enumerate().any(|(index, byte)| {
            !matches!(index, 8 | 13 | 18 | 23)
                && !byte.is_ascii_digit()
                && !(b'a'..=b'f').contains(byte)
        })
    {
        return Err(format!("{field} must be a lowercase canonical UUID"));
    }
    Ok(())
}

fn validate_proof_target(target: LoudnessTarget) -> Result<(), String> {
    if target != LoudnessTarget::CABLE && target != LoudnessTarget::STREAMING {
        return Err("output proof target must be exactly -24 or -16 LUFS".into());
    }
    Ok(())
}

fn validate_artifact_relative_path(path: &str) -> Result<(), String> {
    if path.is_empty()
        || path.len() > MAX_OUTPUT_PROOF_ARTIFACT_PATH_BYTES
        || path.chars().any(char::is_control)
        || path.starts_with(['\\', '/'])
        || path.contains(':')
        || path
            .split(['\\', '/'])
            .any(|part| part.is_empty() || part == "." || part == "..")
    {
        return Err("output proof artifact path must be a bounded relative managed path".into());
    }
    Ok(())
}

fn validate_worker_instance(value: &str) -> Result<(), String> {
    if value.is_empty()
        || value.len() > MAX_OUTPUT_PROOF_TEXT_BYTES
        || value.chars().any(char::is_control)
    {
        return Err("output proof worker instance identity is invalid".into());
    }
    Ok(())
}

fn validate_proof_text(value: &str, field: &str) -> Result<(), String> {
    if value.is_empty()
        || value.len() > MAX_OUTPUT_PROOF_TEXT_BYTES
        || value.chars().any(char::is_control)
    {
        return Err(format!("output proof {field} is invalid"));
    }
    Ok(())
}

fn validate_capture_hash(value: &str) -> Result<(), String> {
    if value.len() != 64
        || value
            .bytes()
            .any(|byte| !byte.is_ascii_digit() && !(b'a'..=b'f').contains(&byte))
    {
        return Err("output proof capture hash must be a lowercase SHA-256 identity".into());
    }
    Ok(())
}

fn validate_proof_bounds(duration_ms: u64, max_bytes: u64) -> Result<(), String> {
    if !(MIN_OUTPUT_PROOF_DURATION_MS..=MAX_OUTPUT_PROOF_DURATION_MS).contains(&duration_ms) {
        return Err("output proof duration is outside the bounded range".into());
    }
    if max_bytes == 0 || max_bytes > MAX_OUTPUT_PROOF_BYTES {
        return Err("output proof byte bound is outside the bounded range".into());
    }
    Ok(())
}

fn validate_output_proof_command(command: &ChannelCommand) -> Result<(), String> {
    match command {
        ChannelCommand::StartOutputProof {
            proof_id,
            output_index,
            sink_id,
            loudness_target_lufs,
            duration_ms,
            max_bytes,
        } => {
            validate_canonical_uuid(proof_id, "proof_id")?;
            validate_canonical_uuid(sink_id, "sink_id")?;
            if *output_index > MAX_OUTPUT_PROOF_INDEX {
                return Err("output proof output index is outside the bounded range".into());
            }
            validate_proof_target(*loudness_target_lufs)?;
            validate_proof_bounds(*duration_ms, *max_bytes)
        }
        ChannelCommand::PollOutputProof { proof_id }
        | ChannelCommand::AbortOutputProof { proof_id } => {
            validate_canonical_uuid(proof_id, "proof_id")
        }
        _ => Ok(()),
    }
}

fn validate_proof_identity_fields(
    proof_id: &str,
    output_index: u16,
    sink_id: &str,
    target: LoudnessTarget,
) -> Result<(), String> {
    validate_canonical_uuid(proof_id, "proof_id")?;
    validate_canonical_uuid(sink_id, "sink_id")?;
    if output_index > MAX_OUTPUT_PROOF_INDEX {
        return Err("output proof output index is outside the bounded range".into());
    }
    validate_proof_target(target)
}

fn validate_output_proof_event(event: &WorkerEvent) -> Result<(), String> {
    match event {
        WorkerEvent::OutputProofStarted {
            proof_id,
            output_index,
            sink_id,
            loudness_target_lufs,
            duration_ms,
            max_bytes,
            artifact_relative_path,
            worker_instance_id,
            graph_revision,
        } => {
            validate_proof_identity_fields(
                proof_id,
                *output_index,
                sink_id,
                *loudness_target_lufs,
            )?;
            validate_proof_bounds(*duration_ms, *max_bytes)?;
            validate_artifact_relative_path(artifact_relative_path)?;
            validate_worker_instance(worker_instance_id)?;
            if *graph_revision == 0 {
                return Err("output proof graph revision must be nonzero".into());
            }
            Ok(())
        }
        WorkerEvent::OutputProofPending {
            proof_id,
            artifact_relative_path,
            worker_instance_id,
            graph_revision,
        } => {
            validate_canonical_uuid(proof_id, "proof_id")?;
            validate_artifact_relative_path(artifact_relative_path)?;
            validate_worker_instance(worker_instance_id)?;
            if *graph_revision == 0 {
                return Err("output proof graph revision must be nonzero".into());
            }
            Ok(())
        }
        WorkerEvent::OutputProofReady {
            proof_id,
            output_index,
            sink_id,
            loudness_target_lufs,
            artifact_relative_path,
            worker_instance_id,
            graph_revision,
            size_bytes,
            capture_sha256,
            duration_ms,
        } => {
            validate_proof_identity_fields(
                proof_id,
                *output_index,
                sink_id,
                *loudness_target_lufs,
            )?;
            validate_artifact_relative_path(artifact_relative_path)?;
            validate_worker_instance(worker_instance_id)?;
            if *graph_revision == 0 {
                return Err("output proof graph revision must be nonzero".into());
            }
            if *size_bytes == 0 || *size_bytes > MAX_OUTPUT_PROOF_BYTES {
                return Err("output proof ready size is outside the bounded range".into());
            }
            validate_capture_hash(capture_sha256)?;
            if !(MIN_OUTPUT_PROOF_DURATION_MS..=MAX_OUTPUT_PROOF_DURATION_MS).contains(duration_ms)
            {
                return Err("output proof ready duration is outside the bounded range".into());
            }
            Ok(())
        }
        WorkerEvent::OutputProofFailed {
            proof_id,
            artifact_relative_path,
            worker_instance_id,
            graph_revision,
            code,
            message,
        } => {
            validate_canonical_uuid(proof_id, "proof_id")?;
            validate_artifact_relative_path(artifact_relative_path)?;
            validate_worker_instance(worker_instance_id)?;
            if *graph_revision == 0 {
                return Err("output proof graph revision must be nonzero".into());
            }
            validate_proof_text(code, "failure code")?;
            validate_proof_text(message, "failure message")
        }
        _ => Ok(()),
    }
}

fn validate_bootstrap_outputs(bootstrap: &WorkerBootstrapEnvelope) -> Result<(), String> {
    if bootstrap.outputs.len() > usize::from(MAX_OUTPUT_PROOF_INDEX) + 1 {
        return Err("worker bootstrap has more outputs than the proof index can address".into());
    }
    let mut seen = std::collections::HashSet::new();
    for output in &bootstrap.outputs {
        let (sink_id, target) = match output {
            WorkerOutputConfiguration::UdpTs {
                sink_id,
                loudness_target_lufs,
                ..
            }
            | WorkerOutputConfiguration::Srt {
                sink_id,
                loudness_target_lufs,
                ..
            } => (sink_id, *loudness_target_lufs),
        };
        validate_canonical_uuid(sink_id, "sink_id")?;
        if !seen.insert(sink_id) {
            return Err("worker bootstrap output sink identities must be unique".into());
        }
        validate_proof_target(target)?;
    }
    Ok(())
}

impl WorkerBootstrapEnvelope {
    pub fn validate_output_proof_binding(
        &self,
        output_index: u16,
        sink_id: &str,
        target: LoudnessTarget,
    ) -> Result<(), String> {
        validate_canonical_uuid(sink_id, "sink_id")?;
        validate_proof_target(target)?;
        if usize::from(output_index) >= self.outputs.len() {
            return Err("output proof output index is not configured".into());
        }
        let configured = match &self.outputs[usize::from(output_index)] {
            WorkerOutputConfiguration::UdpTs {
                sink_id,
                loudness_target_lufs,
                ..
            }
            | WorkerOutputConfiguration::Srt {
                sink_id,
                loudness_target_lufs,
                ..
            } => (sink_id, *loudness_target_lufs),
        };
        if configured.0 != sink_id || configured.1 != target {
            return Err("output proof identity does not match bootstrap output".into());
        }
        Ok(())
    }
}

pub fn decode_command_frame(frame: &[u8]) -> Result<CommandEnvelope, ProtocolError> {
    let command: CommandEnvelope = decode_frame(frame)?;
    if command.version.major != PROTOCOL_VERSION.major {
        return Err(ProtocolError::UnsupportedMajor(command.version.major));
    }
    validate_live_command(&command.command).map_err(ProtocolError::InvalidLiveCommand)?;
    validate_output_proof_command(&command.command).map_err(ProtocolError::InvalidOutputProof)?;
    Ok(command)
}

fn validate_live_command(command: &ChannelCommand) -> Result<(), String> {
    match command {
        ChannelCommand::ArmLive { input, .. } => input.validate().map_err(str::to_owned),
        ChannelCommand::TakeLive { session_id, .. }
        | ChannelCommand::ReturnFromLive { session_id, .. }
        | ChannelCommand::DisarmLive { session_id } => {
            validate_live_id(session_id, "session_id").map_err(str::to_owned)
        }
        _ => Ok(()),
    }
}

pub fn encode_event_frame(event: &WorkerEventEnvelope) -> Result<Vec<u8>, ProtocolError> {
    validate_live_event(&event.event).map_err(ProtocolError::InvalidLiveEvent)?;
    validate_output_proof_event(&event.event).map_err(ProtocolError::InvalidOutputProof)?;
    encode_frame(event)
}

pub fn decode_event_frame(frame: &[u8]) -> Result<WorkerEventEnvelope, ProtocolError> {
    let event: WorkerEventEnvelope = decode_frame(frame)?;
    if event.version.major != PROTOCOL_VERSION.major {
        return Err(ProtocolError::UnsupportedMajor(event.version.major));
    }
    validate_live_event(&event.event).map_err(ProtocolError::InvalidLiveEvent)?;
    validate_output_proof_event(&event.event).map_err(ProtocolError::InvalidOutputProof)?;
    Ok(event)
}

fn validate_live_event(event: &WorkerEvent) -> Result<(), String> {
    match event {
        WorkerEvent::LiveArmed {
            session_id,
            source_id,
            source_revision,
        }
        | WorkerEvent::LiveOnAir {
            session_id,
            source_id,
            source_revision,
        } => {
            validate_live_id(session_id, "session_id").map_err(str::to_owned)?;
            validate_live_id(source_id, "source_id").map_err(str::to_owned)?;
            if *source_revision == 0 {
                return Err("live source revision must be nonzero".into());
            }
            Ok(())
        }
        WorkerEvent::LiveReturned {
            session_id,
            source_kind,
            source_id,
        } => {
            validate_live_id(session_id, "session_id")?;
            if !matches!(source_kind.as_str(), "fallback" | "asset") || source_id.is_empty() {
                return Err("live return event source is invalid".into());
            }
            Ok(())
        }
        WorkerEvent::LiveDisarmed { session_id } => {
            validate_live_id(session_id, "session_id").map_err(str::to_owned)
        }
        _ => Ok(()),
    }
}

fn encode_frame(value: &impl Serialize) -> Result<Vec<u8>, ProtocolError> {
    let payload =
        serde_json::to_vec(value).map_err(|error| ProtocolError::InvalidJson(error.to_string()))?;
    if payload.len() > MAX_FRAME_BYTES {
        return Err(ProtocolError::FrameTooLarge(payload.len()));
    }
    let length = u32::try_from(payload.len())
        .expect("the maximum protocol frame always fits in a u32")
        .to_le_bytes();
    let mut frame = Vec::with_capacity(4 + payload.len());
    frame.extend_from_slice(&length);
    frame.extend_from_slice(&payload);
    Ok(frame)
}

fn decode_frame<T: DeserializeOwned>(frame: &[u8]) -> Result<T, ProtocolError> {
    if frame.len() < 4 {
        return Err(ProtocolError::FrameTooShort);
    }
    let declared =
        u32::from_le_bytes(frame[..4].try_into().expect("four bytes were checked")) as usize;
    if declared > MAX_FRAME_BYTES {
        return Err(ProtocolError::FrameTooLarge(declared));
    }
    let actual = frame.len() - 4;
    if declared != actual {
        return Err(ProtocolError::LengthMismatch { declared, actual });
    }
    serde_json::from_slice(&frame[4..])
        .map_err(|error| ProtocolError::InvalidJson(error.to_string()))
}
