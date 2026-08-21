use serde::{Deserialize, Deserializer, Serialize, de::DeserializeOwned};
use std::net::IpAddr;
use std::num::NonZeroU16;
use std::path::Path;

pub const MAX_FRAME_BYTES: usize = 65_536;
pub const PROTOCOL_VERSION: ProtocolVersion = ProtocolVersion { major: 1, minor: 7 };

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
pub struct WorkerBootstrapEnvelope {
    pub version: ProtocolVersion,
    pub worker_id: String,
    pub channel_id: String,
    pub output_profile: String,
    /// Station-managed root used by the worker to resolve CG image identities.
    /// Overlay commands carry only an asset id and format, never a path.
    pub cg_image_root: String,
    pub outputs: Vec<WorkerOutputConfiguration>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "kebab-case")]
pub enum WorkerOutputConfiguration {
    UdpTs {
        destination: String,
    },
    Srt {
        uri: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        passphrase: Option<SrtPassphrase>,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CommandEnvelope {
    pub version: ProtocolVersion,
    pub channel_id: String,
    pub command_id: String,
    pub expected_sequence: u64,
    pub command: ChannelCommand,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
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
    },
    DisarmLive {
        session_id: String,
    },
    Shutdown,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
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
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
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
    InvalidLiveCommand(String),
    InvalidLiveEvent(String),
}

pub fn encode_command_frame(command: &CommandEnvelope) -> Result<Vec<u8>, ProtocolError> {
    validate_live_command(&command.command).map_err(ProtocolError::InvalidLiveCommand)?;
    encode_frame(command)
}

pub fn encode_bootstrap_frame(
    bootstrap: &WorkerBootstrapEnvelope,
) -> Result<Vec<u8>, ProtocolError> {
    validate_cg_image_root(&bootstrap.cg_image_root)
        .map_err(ProtocolError::InvalidManagedCgImageRoot)?;
    encode_frame(bootstrap)
}

pub fn decode_bootstrap_frame(frame: &[u8]) -> Result<WorkerBootstrapEnvelope, ProtocolError> {
    let bootstrap: WorkerBootstrapEnvelope = decode_frame(frame)?;
    if bootstrap.version.major != PROTOCOL_VERSION.major {
        return Err(ProtocolError::UnsupportedMajor(bootstrap.version.major));
    }
    validate_cg_image_root(&bootstrap.cg_image_root)
        .map_err(ProtocolError::InvalidManagedCgImageRoot)?;
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

pub fn decode_command_frame(frame: &[u8]) -> Result<CommandEnvelope, ProtocolError> {
    let command: CommandEnvelope = decode_frame(frame)?;
    if command.version.major != PROTOCOL_VERSION.major {
        return Err(ProtocolError::UnsupportedMajor(command.version.major));
    }
    validate_live_command(&command.command).map_err(ProtocolError::InvalidLiveCommand)?;
    Ok(command)
}

fn validate_live_command(command: &ChannelCommand) -> Result<(), String> {
    match command {
        ChannelCommand::ArmLive { input, .. } => input.validate().map_err(str::to_owned),
        ChannelCommand::TakeLive { session_id, .. }
        | ChannelCommand::ReturnFromLive { session_id }
        | ChannelCommand::DisarmLive { session_id } => {
            validate_live_id(session_id, "session_id").map_err(str::to_owned)
        }
        _ => Ok(()),
    }
}

pub fn encode_event_frame(event: &WorkerEventEnvelope) -> Result<Vec<u8>, ProtocolError> {
    validate_live_event(&event.event).map_err(ProtocolError::InvalidLiveEvent)?;
    encode_frame(event)
}

pub fn decode_event_frame(frame: &[u8]) -> Result<WorkerEventEnvelope, ProtocolError> {
    let event: WorkerEventEnvelope = decode_frame(frame)?;
    if event.version.major != PROTOCOL_VERSION.major {
        return Err(ProtocolError::UnsupportedMajor(event.version.major));
    }
    validate_live_event(&event.event).map_err(ProtocolError::InvalidLiveEvent)?;
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
        WorkerEvent::LiveReturned { session_id } | WorkerEvent::LiveDisarmed { session_id } => {
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
