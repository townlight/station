//! Pure, fail-closed production-control-room authority contracts.
//!
//! This crate authorizes bounded, non-secret control intent and records immutable
//! worker receipts. It deliberately does not connect to or execute against a device.

use std::collections::HashSet;

use serde::{Deserialize, Serialize};

pub const MAX_DEVICES_PER_SESSION: usize = 128;
pub const MAX_COMMAND_RECEIPTS: usize = 100_000;
pub const MAX_ENDPOINT_HOST_BYTES: usize = 253;
pub const MAX_LABEL_BYTES: usize = 200;
pub const MAX_OSC_ARGUMENTS: usize = 16;
pub const MAX_OSC_ADDRESS_BYTES: usize = 200;
pub const MAX_SERIAL_PAYLOAD_BYTES: usize = 512;
pub const MAX_RECEIPT_DETAIL_BYTES: usize = 500;
pub const MAX_GPI_PULSE_MS: u32 = 10_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ControlDeviceKind {
    Obs,
    Vmix,
    Atem,
    Hyperdeck,
    Ptz,
    Osc,
    Gpi,
    Serial,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ControlCapability {
    SceneRecall,
    ProgramSwitch,
    Record,
    InputRecall,
    AuxRoute,
    Transport,
    PresetRecall,
    PtzMove,
    OscMessage,
    GpiPulse,
    SerialCommand,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum ControlEndpoint {
    Network { host: String, port: u16 },
    Gpi { bank: u16 },
    Serial { port: String },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ControlDevice {
    pub device_id: String,
    pub label: String,
    pub kind: ControlDeviceKind,
    pub endpoint: ControlEndpoint,
    pub capabilities: Vec<ControlCapability>,
    pub revision: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ControlSession {
    pub session_id: String,
    pub channel_id: String,
    pub label: String,
    pub device_ids: Vec<String>,
    pub revision: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ControlActorRole {
    Operator,
    Worker,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ControlActor {
    pub actor_id: String,
    pub role: ControlActorRole,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum ControlCommand {
    Obs {
        scene: String,
    },
    Vmix {
        input: u16,
    },
    Atem {
        input: u16,
    },
    Hyperdeck {
        transport: HyperdeckTransport,
    },
    Ptz {
        pan: i16,
        tilt: i16,
        zoom: u8,
    },
    Osc {
        address: String,
        arguments: Vec<OscArgument>,
    },
    Gpi {
        line: u16,
        pulse_ms: u32,
    },
    Serial {
        payload: String,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum HyperdeckTransport {
    Play,
    Stop,
    Record,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(
    tag = "type",
    content = "value",
    rename_all = "snake_case",
    deny_unknown_fields
)]
pub enum OscArgument {
    Integer(i32),
    Boolean(bool),
    Text(String),
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ControlCue {
    pub cue_id: String,
    pub session_id: String,
    pub session_revision: u64,
    pub device_id: String,
    pub device_revision: u64,
    pub label: String,
    pub command: ControlCommand,
    pub revision: u64,
}

/// A bounded operator request. This is dispatch intent, not a device-execution claim.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ControlCommandIntent {
    pub intent_id: String,
    pub cue_id: String,
    pub cue_revision: u64,
    pub session_id: String,
    pub session_revision: u64,
    pub device_id: String,
    pub device_revision: u64,
    pub requested_by: ControlActor,
    pub requested_at_unix_ms: i64,
    pub command: ControlCommand,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ControlReceiptOutcome {
    AcceptedForDispatch,
    RejectedBeforeDispatch,
}

/// Immutable worker acknowledgement of an intent. Neither outcome asserts a
/// device connection, command delivery, or physical execution.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ControlCommandReceipt {
    pub receipt_id: String,
    pub intent_id: String,
    pub worker: ControlActor,
    pub recorded_at_unix_ms: i64,
    pub outcome: ControlReceiptOutcome,
    pub detail: String,
}

impl ControlActor {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.actor_id, "control actor identity")
    }
}

impl ControlDevice {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.device_id, "control device identity")?;
        validate_visible(&self.label, MAX_LABEL_BYTES, "control device label")?;
        if self.revision == 0 || self.capabilities.is_empty() || self.capabilities.len() > 16 {
            return Err(
                "control device revision or capability count is invalid or unbounded".into(),
            );
        }
        self.endpoint.validate_for(self.kind)?;
        let mut capabilities = HashSet::with_capacity(self.capabilities.len());
        for capability in &self.capabilities {
            if !capabilities.insert(capability) || !supports_capability(self.kind, *capability) {
                return Err(
                    "control device capabilities must be unique and supported by its kind".into(),
                );
            }
        }
        Ok(())
    }
}

impl ControlEndpoint {
    fn validate_for(&self, kind: ControlDeviceKind) -> Result<(), String> {
        match (kind, self) {
            (ControlDeviceKind::Gpi, Self::Gpi { .. }) => Ok(()),
            (ControlDeviceKind::Serial, Self::Serial { port }) => {
                validate_visible(port, MAX_ENDPOINT_HOST_BYTES, "serial port name")
            }
            (ControlDeviceKind::Gpi | ControlDeviceKind::Serial, _) => {
                Err("control device kind requires its typed local endpoint".into())
            }
            (_, Self::Network { host, port }) if *port != 0 => validate_network_host(host),
            _ => Err("control device kind requires a bounded network endpoint".into()),
        }
    }
}

impl ControlSession {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.session_id, "control session identity")?;
        validate_uuid(&self.channel_id, "control session channel identity")?;
        validate_visible(&self.label, MAX_LABEL_BYTES, "control session label")?;
        if self.revision == 0
            || self.device_ids.is_empty()
            || self.device_ids.len() > MAX_DEVICES_PER_SESSION
        {
            return Err("control session revision or device count is invalid or unbounded".into());
        }
        let mut device_ids = HashSet::with_capacity(self.device_ids.len());
        for device_id in &self.device_ids {
            validate_uuid(device_id, "control session device identity")?;
            if !device_ids.insert(device_id.as_str()) {
                return Err("control session device identities must be unique".into());
            }
        }
        Ok(())
    }
}

impl ControlCommand {
    pub fn device_kind(&self) -> ControlDeviceKind {
        match self {
            Self::Obs { .. } => ControlDeviceKind::Obs,
            Self::Vmix { .. } => ControlDeviceKind::Vmix,
            Self::Atem { .. } => ControlDeviceKind::Atem,
            Self::Hyperdeck { .. } => ControlDeviceKind::Hyperdeck,
            Self::Ptz { .. } => ControlDeviceKind::Ptz,
            Self::Osc { .. } => ControlDeviceKind::Osc,
            Self::Gpi { .. } => ControlDeviceKind::Gpi,
            Self::Serial { .. } => ControlDeviceKind::Serial,
        }
    }

    pub fn required_capability(&self) -> ControlCapability {
        match self {
            Self::Obs { .. } => ControlCapability::SceneRecall,
            Self::Vmix { .. } => ControlCapability::InputRecall,
            Self::Atem { .. } => ControlCapability::ProgramSwitch,
            Self::Hyperdeck { .. } => ControlCapability::Transport,
            Self::Ptz { .. } => ControlCapability::PtzMove,
            Self::Osc { .. } => ControlCapability::OscMessage,
            Self::Gpi { .. } => ControlCapability::GpiPulse,
            Self::Serial { .. } => ControlCapability::SerialCommand,
        }
    }

    pub fn validate(&self) -> Result<(), String> {
        match self {
            Self::Obs { scene } => validate_visible(scene, MAX_LABEL_BYTES, "OBS scene"),
            Self::Vmix { input } | Self::Atem { input } if *input > 0 => Ok(()),
            Self::Vmix { .. } | Self::Atem { .. } => Err("switcher input must be positive".into()),
            Self::Hyperdeck { .. } => Ok(()),
            Self::Ptz { pan, tilt, zoom }
                if (-100..=100).contains(pan) && (-100..=100).contains(tilt) && *zoom <= 100 =>
            {
                Ok(())
            }
            Self::Ptz { .. } => Err("PTZ movement is outside its bounded range".into()),
            Self::Osc { address, arguments } => {
                if !address.starts_with('/') || arguments.len() > MAX_OSC_ARGUMENTS {
                    return Err("OSC address or argument count is invalid or unbounded".into());
                }
                validate_visible(address, MAX_OSC_ADDRESS_BYTES, "OSC address")?;
                for argument in arguments {
                    if let OscArgument::Text(value) = argument {
                        validate_visible(value, MAX_LABEL_BYTES, "OSC text argument")?;
                    }
                }
                Ok(())
            }
            Self::Gpi { pulse_ms, .. } if *pulse_ms > 0 && *pulse_ms <= MAX_GPI_PULSE_MS => Ok(()),
            Self::Gpi { .. } => Err("GPI pulse duration is invalid or unbounded".into()),
            Self::Serial { payload } => {
                validate_visible(payload, MAX_SERIAL_PAYLOAD_BYTES, "serial payload")
            }
        }
    }
}

impl ControlCue {
    pub fn validate_shape(&self) -> Result<(), String> {
        validate_uuid(&self.cue_id, "control cue identity")?;
        validate_uuid(&self.session_id, "control cue session identity")?;
        validate_uuid(&self.device_id, "control cue device identity")?;
        validate_visible(&self.label, MAX_LABEL_BYTES, "control cue label")?;
        if self.session_revision == 0 || self.device_revision == 0 || self.revision == 0 {
            return Err("control cue revisions must be positive".into());
        }
        self.command.validate()
    }
}

impl ControlCommandIntent {
    pub fn validate_shape(&self) -> Result<(), String> {
        for (value, field) in [
            (&self.intent_id, "control command intent identity"),
            (&self.cue_id, "control command intent cue identity"),
            (&self.session_id, "control command intent session identity"),
            (&self.device_id, "control command intent device identity"),
        ] {
            validate_uuid(value, field)?;
        }
        self.requested_by.validate()?;
        if self.requested_by.role != ControlActorRole::Operator
            || self.cue_revision == 0
            || self.session_revision == 0
            || self.device_revision == 0
            || self.requested_at_unix_ms < 0
        {
            return Err(
                "control command intent must be bounded and requested by an operator".into(),
            );
        }
        self.command.validate()
    }
}

impl ControlCommandReceipt {
    pub fn validate_shape(&self) -> Result<(), String> {
        validate_uuid(&self.receipt_id, "control receipt identity")?;
        validate_uuid(&self.intent_id, "control receipt intent identity")?;
        self.worker.validate()?;
        if self.worker.role != ControlActorRole::Worker || self.recorded_at_unix_ms < 0 {
            return Err("control receipt must be bounded and recorded by a worker".into());
        }
        validate_visible(
            &self.detail,
            MAX_RECEIPT_DETAIL_BYTES,
            "control receipt detail",
        )
    }
}

pub fn validate_device_write(
    current: Option<&ControlDevice>,
    expected_revision: Option<u64>,
    proposed: &ControlDevice,
    actor: &ControlActor,
) -> Result<(), String> {
    require_operator(actor, "device write")?;
    proposed.validate()?;
    match current {
        None if expected_revision.is_none() && proposed.revision == 1 => Ok(()),
        None => {
            Err("control device creation requires revision one and no expected revision".into())
        }
        Some(current) => {
            current.validate()?;
            require_next_revision(
                current.revision,
                expected_revision,
                proposed.revision,
                "control device",
            )?;
            if proposed.device_id != current.device_id
                || proposed.kind != current.kind
                || proposed.endpoint != current.endpoint
            {
                return Err("control device identity, kind, and endpoint cannot be rebound".into());
            }
            Ok(())
        }
    }
}

pub fn validate_session_write(
    current: Option<&ControlSession>,
    expected_revision: Option<u64>,
    proposed: &ControlSession,
    actor: &ControlActor,
) -> Result<(), String> {
    require_operator(actor, "session write")?;
    proposed.validate()?;
    match current {
        None if expected_revision.is_none() && proposed.revision == 1 => Ok(()),
        None => {
            Err("control session creation requires revision one and no expected revision".into())
        }
        Some(current) => {
            current.validate()?;
            require_next_revision(
                current.revision,
                expected_revision,
                proposed.revision,
                "control session",
            )?;
            if proposed.session_id != current.session_id
                || proposed.channel_id != current.channel_id
            {
                return Err("control session identity and channel cannot be rebound".into());
            }
            Ok(())
        }
    }
}

pub fn validate_cue_write(
    current: Option<&ControlCue>,
    expected_revision: Option<u64>,
    proposed: &ControlCue,
    session: &ControlSession,
    device: &ControlDevice,
    actor: &ControlActor,
) -> Result<(), String> {
    require_operator(actor, "cue write")?;
    validate_cue_binding(proposed, session, device)?;
    match current {
        None if expected_revision.is_none() && proposed.revision == 1 => Ok(()),
        None => Err("control cue creation requires revision one and no expected revision".into()),
        Some(current) => {
            current.validate_shape()?;
            require_next_revision(
                current.revision,
                expected_revision,
                proposed.revision,
                "control cue",
            )?;
            if proposed.cue_id != current.cue_id
                || proposed.session_id != current.session_id
                || proposed.device_id != current.device_id
            {
                return Err("control cue identity, session, and device cannot be rebound".into());
            }
            Ok(())
        }
    }
}

pub fn validate_command_intent(
    intent: &ControlCommandIntent,
    cue: &ControlCue,
    session: &ControlSession,
    device: &ControlDevice,
) -> Result<(), String> {
    intent.validate_shape()?;
    validate_cue_binding(cue, session, device)?;
    if intent.cue_id != cue.cue_id
        || intent.cue_revision != cue.revision
        || intent.session_id != session.session_id
        || intent.session_revision != session.revision
        || intent.device_id != device.device_id
        || intent.device_revision != device.revision
        || intent.command != cue.command
    {
        return Err(
            "control command intent must bind the exact current cue, session, device, and command"
                .into(),
        );
    }
    Ok(())
}

pub fn validate_receipt_append(
    existing: &[ControlCommandReceipt],
    proposed: &ControlCommandReceipt,
    intent: &ControlCommandIntent,
) -> Result<(), String> {
    proposed.validate_shape()?;
    intent.validate_shape()?;
    if existing.len() >= MAX_COMMAND_RECEIPTS {
        return Err("control receipt context exceeds its bounded record count".into());
    }
    let mut receipt_ids = HashSet::with_capacity(existing.len());
    let mut intent_ids = HashSet::with_capacity(existing.len());
    for receipt in existing {
        receipt.validate_shape()?;
        if !receipt_ids.insert(receipt.receipt_id.as_str())
            || !intent_ids.insert(receipt.intent_id.as_str())
        {
            return Err("existing control receipts are not immutable and unique".into());
        }
    }
    if proposed.intent_id != intent.intent_id
        || receipt_ids.contains(proposed.receipt_id.as_str())
        || intent_ids.contains(proposed.intent_id.as_str())
    {
        return Err("control command receipt identity and intent may be recorded only once".into());
    }
    Ok(())
}

fn validate_cue_binding(
    cue: &ControlCue,
    session: &ControlSession,
    device: &ControlDevice,
) -> Result<(), String> {
    cue.validate_shape()?;
    session.validate()?;
    device.validate()?;
    if cue.session_id != session.session_id
        || cue.session_revision != session.revision
        || cue.device_id != device.device_id
        || cue.device_revision != device.revision
        || !session
            .device_ids
            .iter()
            .any(|device_id| device_id == &device.device_id)
        || cue.command.device_kind() != device.kind
        || !device
            .capabilities
            .contains(&cue.command.required_capability())
    {
        return Err(
            "control cue must bind exact session/device revisions and a supported capability"
                .into(),
        );
    }
    Ok(())
}

fn supports_capability(kind: ControlDeviceKind, capability: ControlCapability) -> bool {
    matches!(
        (kind, capability),
        (
            ControlDeviceKind::Obs,
            ControlCapability::SceneRecall | ControlCapability::Record
        ) | (
            ControlDeviceKind::Vmix,
            ControlCapability::InputRecall | ControlCapability::Record
        ) | (
            ControlDeviceKind::Atem,
            ControlCapability::ProgramSwitch | ControlCapability::AuxRoute
        ) | (ControlDeviceKind::Hyperdeck, ControlCapability::Transport)
            | (
                ControlDeviceKind::Ptz,
                ControlCapability::PresetRecall | ControlCapability::PtzMove
            )
            | (ControlDeviceKind::Osc, ControlCapability::OscMessage)
            | (ControlDeviceKind::Gpi, ControlCapability::GpiPulse)
            | (ControlDeviceKind::Serial, ControlCapability::SerialCommand)
    )
}

fn require_operator(actor: &ControlActor, operation: &str) -> Result<(), String> {
    actor.validate()?;
    if actor.role != ControlActorRole::Operator {
        return Err(format!("{operation} requires an operator actor"));
    }
    Ok(())
}

fn require_next_revision(
    current: u64,
    expected: Option<u64>,
    proposed: u64,
    subject: &str,
) -> Result<(), String> {
    if expected != Some(current) || current.checked_add(1) != Some(proposed) {
        return Err(format!(
            "{subject} write has a stale or invalid compare-and-set revision"
        ));
    }
    Ok(())
}

fn validate_uuid(value: &str, field: &str) -> Result<(), String> {
    let bytes = value.as_bytes();
    if bytes.len() != 36
        || ![8, 13, 18, 23].iter().all(|&index| bytes[index] == b'-')
        || bytes.iter().enumerate().any(|(index, byte)| {
            !matches!(index, 8 | 13 | 18 | 23)
                && !(byte.is_ascii_digit() || (b'a'..=b'f').contains(byte))
        })
    {
        return Err(format!("{field} must be a lowercase canonical UUID"));
    }
    Ok(())
}

fn validate_visible(value: &str, max_bytes: usize, field: &str) -> Result<(), String> {
    if value.is_empty()
        || value.trim() != value
        || value.len() > max_bytes
        || value.chars().any(char::is_control)
    {
        return Err(format!(
            "{field} must be nonempty, trimmed, visible, and bounded"
        ));
    }
    Ok(())
}

fn validate_network_host(host: &str) -> Result<(), String> {
    validate_visible(host, MAX_ENDPOINT_HOST_BYTES, "network endpoint host")?;
    if host
        .chars()
        .any(|character| matches!(character, '/' | '\\' | '@' | ':'))
    {
        return Err("network endpoint must be a host only, without URL credentials or path".into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    const OPERATOR: &str = "11111111-1111-4111-8111-111111111111";
    const WORKER: &str = "22222222-2222-4222-8222-222222222222";
    const DEVICE: &str = "33333333-3333-4333-8333-333333333333";
    const SESSION: &str = "44444444-4444-4444-8444-444444444444";
    const CHANNEL: &str = "55555555-5555-4555-8555-555555555555";
    const CUE: &str = "66666666-6666-4666-8666-666666666666";

    fn operator() -> ControlActor {
        ControlActor {
            actor_id: OPERATOR.into(),
            role: ControlActorRole::Operator,
        }
    }
    fn worker() -> ControlActor {
        ControlActor {
            actor_id: WORKER.into(),
            role: ControlActorRole::Worker,
        }
    }
    fn device(revision: u64) -> ControlDevice {
        ControlDevice {
            device_id: DEVICE.into(),
            label: "Studio OBS".into(),
            kind: ControlDeviceKind::Obs,
            endpoint: ControlEndpoint::Network {
                host: "obs.local".into(),
                port: 4455,
            },
            capabilities: vec![ControlCapability::SceneRecall],
            revision,
        }
    }
    fn session(revision: u64) -> ControlSession {
        ControlSession {
            session_id: SESSION.into(),
            channel_id: CHANNEL.into(),
            label: "Morning control room".into(),
            device_ids: vec![DEVICE.into()],
            revision,
        }
    }
    fn cue(revision: u64) -> ControlCue {
        ControlCue {
            cue_id: CUE.into(),
            session_id: SESSION.into(),
            session_revision: 1,
            device_id: DEVICE.into(),
            device_revision: 1,
            label: "Open graphics scene".into(),
            command: ControlCommand::Obs {
                scene: "Headlines".into(),
            },
            revision,
        }
    }
    fn intent() -> ControlCommandIntent {
        ControlCommandIntent {
            intent_id: "77777777-7777-4777-8777-777777777777".into(),
            cue_id: CUE.into(),
            cue_revision: 1,
            session_id: SESSION.into(),
            session_revision: 1,
            device_id: DEVICE.into(),
            device_revision: 1,
            requested_by: operator(),
            requested_at_unix_ms: 1_700_000_000_000,
            command: cue(1).command,
        }
    }
    fn receipt() -> ControlCommandReceipt {
        ControlCommandReceipt {
            receipt_id: "88888888-8888-4888-8888-888888888888".into(),
            intent_id: intent().intent_id,
            worker: worker(),
            recorded_at_unix_ms: 1_700_000_000_001,
            outcome: ControlReceiptOutcome::AcceptedForDispatch,
            detail: "queued for a future adapter".into(),
        }
    }

    #[test]
    fn validates_revision_bound_operator_cue_intent_and_worker_receipt() {
        let device = device(1);
        let session = session(1);
        let cue = cue(1);
        let intent = intent();
        let receipt = receipt();
        assert!(validate_device_write(None, None, &device, &operator()).is_ok());
        assert!(validate_session_write(None, None, &session, &operator()).is_ok());
        assert!(validate_cue_write(None, None, &cue, &session, &device, &operator()).is_ok());
        assert!(validate_command_intent(&intent, &cue, &session, &device).is_ok());
        assert!(validate_receipt_append(&[], &receipt, &intent).is_ok());
    }

    #[test]
    fn rejects_unsupported_device_command_and_unbounded_non_secret_contracts() {
        let mut invalid_device = device(1);
        invalid_device.capabilities = vec![ControlCapability::Transport];
        assert!(invalid_device.validate().is_err());
        let mut cue = cue(1);
        cue.command = ControlCommand::Serial {
            payload: "bounded but wrong device".into(),
        };
        assert!(
            validate_cue_write(None, None, &cue, &session(1), &device(1), &operator()).is_err()
        );
        let mut osc = ControlCommand::Osc {
            address: "/cue".into(),
            arguments: vec![],
        };
        if let ControlCommand::Osc { arguments, .. } = &mut osc {
            *arguments = vec![OscArgument::Boolean(true); MAX_OSC_ARGUMENTS + 1];
        }
        assert!(osc.validate().is_err());
        let mut endpoint = device(1);
        endpoint.endpoint = ControlEndpoint::Network {
            host: "token@example.invalid".into(),
            port: 4455,
        };
        assert!(endpoint.validate().is_err());
    }

    #[test]
    fn rejects_worker_writes_stale_cas_and_identity_rebinding() {
        let current = device(1);
        let mut update = device(2);
        assert!(validate_device_write(Some(&current), Some(1), &update, &worker()).is_err());
        assert!(validate_device_write(Some(&current), Some(2), &update, &operator()).is_err());
        update.endpoint = ControlEndpoint::Network {
            host: "other.local".into(),
            port: 4455,
        };
        assert!(validate_device_write(Some(&current), Some(1), &update, &operator()).is_err());
        let mut cue_update = cue(2);
        cue_update.session_id = "99999999-9999-4999-8999-999999999999".into();
        assert!(
            validate_cue_write(
                Some(&cue(1)),
                Some(1),
                &cue_update,
                &session(1),
                &device(1),
                &operator()
            )
            .is_err()
        );
    }

    #[test]
    fn receipt_is_append_only_and_replay_fails_closed() {
        let intent = intent();
        let receipt = receipt();
        assert!(
            validate_receipt_append(std::slice::from_ref(&receipt), &receipt, &intent).is_err()
        );
        let mut replay = receipt.clone();
        replay.receipt_id = "99999999-9999-4999-8999-999999999999".into();
        assert!(validate_receipt_append(&[receipt], &replay, &intent).is_err());
        let mut wrong_actor = replay;
        wrong_actor.intent_id = intent.intent_id.clone();
        wrong_actor.worker = operator();
        assert!(wrong_actor.validate_shape().is_err());
    }

    #[test]
    fn rejects_unknown_serde_and_bare_device_execution_claims() {
        assert!(
            serde_json::from_str::<ControlDevice>(
                r#"{"device_id":"33333333-3333-4333-8333-333333333333","unexpected":true}"#
            )
            .is_err()
        );
        let receipt: ControlCommandReceipt = serde_json::from_str(r#"{"receipt_id":"88888888-8888-4888-8888-888888888888","intent_id":"77777777-7777-4777-8777-777777777777","worker":{"actor_id":"22222222-2222-4222-8222-222222222222","role":"worker"},"recorded_at_unix_ms":1,"outcome":"accepted_for_dispatch","detail":"queued"}"#).expect("contract shape");
        assert!(receipt.validate_shape().is_ok());
    }
}
