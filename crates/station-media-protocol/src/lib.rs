use serde::{Deserialize, Serialize, de::DeserializeOwned};

pub const MAX_FRAME_BYTES: usize = 65_536;
pub const PROTOCOL_VERSION: ProtocolVersion = ProtocolVersion { major: 1, minor: 3 };

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
    LoadAsset {
        asset_id: String,
        media_path: String,
    },
    TakeAsset {
        asset_id: String,
    },
    ArmLive {
        source_id: String,
        arm_token: String,
    },
    TakeLive {
        arm_token: String,
    },
    ReturnToSchedule,
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
#[serde(tag = "type", rename_all = "snake_case")]
pub enum WorkerEvent {
    Ready {
        graph_revision: u64,
    },
    OnAirChanged {
        source_kind: String,
        source_id: String,
    },
    AssetLoaded {
        asset_id: String,
    },
    CommandRejected {
        command_id: String,
        code: String,
        message: String,
    },
    Heartbeat {
        monotonic_milliseconds: u64,
    },
    ShutdownComplete,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProtocolError {
    FrameTooShort,
    FrameTooLarge(usize),
    LengthMismatch { declared: usize, actual: usize },
    InvalidJson(String),
    UnsupportedMajor(u16),
}

pub fn encode_command_frame(command: &CommandEnvelope) -> Result<Vec<u8>, ProtocolError> {
    encode_frame(command)
}

pub fn encode_bootstrap_frame(
    bootstrap: &WorkerBootstrapEnvelope,
) -> Result<Vec<u8>, ProtocolError> {
    encode_frame(bootstrap)
}

pub fn decode_bootstrap_frame(frame: &[u8]) -> Result<WorkerBootstrapEnvelope, ProtocolError> {
    let bootstrap: WorkerBootstrapEnvelope = decode_frame(frame)?;
    if bootstrap.version.major != PROTOCOL_VERSION.major {
        return Err(ProtocolError::UnsupportedMajor(bootstrap.version.major));
    }
    Ok(bootstrap)
}

pub fn decode_command_frame(frame: &[u8]) -> Result<CommandEnvelope, ProtocolError> {
    let command: CommandEnvelope = decode_frame(frame)?;
    if command.version.major != PROTOCOL_VERSION.major {
        return Err(ProtocolError::UnsupportedMajor(command.version.major));
    }
    Ok(command)
}

pub fn encode_event_frame(event: &WorkerEventEnvelope) -> Result<Vec<u8>, ProtocolError> {
    encode_frame(event)
}

pub fn decode_event_frame(frame: &[u8]) -> Result<WorkerEventEnvelope, ProtocolError> {
    let event: WorkerEventEnvelope = decode_frame(frame)?;
    if event.version.major != PROTOCOL_VERSION.major {
        return Err(ProtocolError::UnsupportedMajor(event.version.major));
    }
    Ok(event)
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
