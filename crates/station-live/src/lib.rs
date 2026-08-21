use std::net::IpAddr;

use serde::{Deserialize, Serialize};

const MAX_LABEL: usize = 120;
const MAX_OPERATOR: usize = 120;
const MAX_REASON: usize = 500;
const MAX_FAILURE: usize = 1000;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LiveSource {
    pub source_id: String,
    pub channel_id: String,
    pub label: String,
    pub enabled: bool,
    pub endpoint: LiveEndpoint,
    pub revision: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum LiveEndpoint {
    UdpMpegTs { bind_ip: String, port: u16 },
}

impl LiveEndpoint {
    pub fn validate(&self) -> Result<(), String> {
        match self {
            Self::UdpMpegTs { bind_ip, port } => {
                let address = bind_ip
                    .parse::<IpAddr>()
                    .map_err(|_| "UDP live source bind_ip must be an IP address".to_string())?;
                if bind_ip.trim() != bind_ip || bind_ip.chars().any(char::is_control) {
                    return Err("UDP live source bind_ip has invalid whitespace or controls".into());
                }
                if *port == 0 {
                    return Err("UDP live source port must be nonzero".into());
                }
                let station_local = address.is_unspecified()
                    || address.is_loopback()
                    || matches!(address, IpAddr::V4(value) if value.is_link_local())
                    || matches!(address, IpAddr::V6(value) if (value.segments()[0] & 0xffc0) == 0xfe80)
                    || matches!(address, IpAddr::V6(value) if (value.segments()[0] & 0xfe00) == 0xfc00)
                    || address.is_multicast()
                    || matches!(address, IpAddr::V4(value) if value.is_private());
                if !station_local {
                    return Err(
                        "UDP live source bind_ip must be station-local, private, or multicast"
                            .into(),
                    );
                }
                Ok(())
            }
        }
    }

    pub fn storage_fields(&self) -> (&'static str, &str, u16) {
        match self {
            Self::UdpMpegTs { bind_ip, port } => ("udp_mpeg_ts", bind_ip, *port),
        }
    }

    pub fn from_storage(kind: &str, bind_ip: String, port: u16) -> Result<Self, String> {
        match kind {
            "udp_mpeg_ts" => Ok(Self::UdpMpegTs { bind_ip, port }),
            _ => Err("unknown live source endpoint kind".into()),
        }
    }
}

impl LiveSource {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.source_id, "live source identity")?;
        validate_uuid(&self.channel_id, "live source channel identity")?;
        validate_visible(&self.label, MAX_LABEL, "live source label")?;
        if self.revision == 0 {
            return Err("live source revision must be nonzero".into());
        }
        self.endpoint.validate()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TakeoverState {
    Arming,
    Armed,
    TakeRequested,
    OnAir,
    ReturnRequested,
    Returning,
    Completed,
    Failed,
}

impl TakeoverState {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Arming => "arming",
            Self::Armed => "armed",
            Self::TakeRequested => "take_requested",
            Self::OnAir => "on_air",
            Self::ReturnRequested => "return_requested",
            Self::Returning => "returning",
            Self::Completed => "completed",
            Self::Failed => "failed",
        }
    }

    pub fn parse(value: &str) -> Result<Self, String> {
        match value {
            "arming" => Ok(Self::Arming),
            "armed" => Ok(Self::Armed),
            "take_requested" => Ok(Self::TakeRequested),
            "on_air" => Ok(Self::OnAir),
            "return_requested" => Ok(Self::ReturnRequested),
            "returning" => Ok(Self::Returning),
            "completed" => Ok(Self::Completed),
            "failed" => Ok(Self::Failed),
            _ => Err("unknown takeover state".into()),
        }
    }

    pub fn can_transition_to(self, next: Self) -> bool {
        matches!(
            (self, next),
            (Self::Arming, Self::Armed | Self::Failed)
                | (Self::Armed, Self::TakeRequested | Self::Failed)
                | (Self::TakeRequested, Self::OnAir | Self::Failed)
                | (Self::OnAir, Self::ReturnRequested | Self::Failed)
                | (Self::ReturnRequested, Self::Returning | Self::Failed)
                | (Self::Returning, Self::Completed | Self::Failed)
        )
    }

    pub fn is_active(self) -> bool {
        matches!(
            self,
            Self::Arming
                | Self::Armed
                | Self::TakeRequested
                | Self::OnAir
                | Self::ReturnRequested
                | Self::Returning
        )
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TakeoverSession {
    pub session_id: String,
    pub channel_id: String,
    pub source_id: String,
    pub source_revision: u64,
    pub authority_revision: u64,
    pub operator_id: String,
    pub reason: Option<String>,
    pub requested_at_unix_ms: i64,
    pub expires_at_unix_ms: i64,
    pub taken_at_unix_ms: Option<i64>,
    pub returned_at_unix_ms: Option<i64>,
    pub state: TakeoverState,
    pub failure_detail: Option<String>,
}

impl TakeoverSession {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.session_id, "takeover session identity")?;
        validate_uuid(&self.channel_id, "takeover channel identity")?;
        validate_uuid(&self.source_id, "takeover source identity")?;
        if self.source_revision == 0 || self.authority_revision == 0 {
            return Err("takeover revisions must be nonzero".into());
        }
        validate_visible(&self.operator_id, MAX_OPERATOR, "takeover operator")?;
        if let Some(reason) = &self.reason {
            validate_visible(reason, MAX_REASON, "takeover reason")?;
        }
        if self.requested_at_unix_ms < 0
            || self.expires_at_unix_ms <= self.requested_at_unix_ms
            || self.expires_at_unix_ms - self.requested_at_unix_ms > 3_600_000
            || self
                .taken_at_unix_ms
                .is_some_and(|value| value < self.requested_at_unix_ms)
            || self.returned_at_unix_ms.is_some_and(|value| {
                value < self.taken_at_unix_ms.unwrap_or(self.requested_at_unix_ms)
            })
        {
            return Err("takeover timestamps are invalid".into());
        }
        if matches!(
            self.state,
            TakeoverState::OnAir | TakeoverState::ReturnRequested | TakeoverState::Returning
        ) && self.taken_at_unix_ms.is_none()
        {
            return Err("on-air takeover must have a taken timestamp".into());
        }
        if self.state == TakeoverState::Completed && self.returned_at_unix_ms.is_none() {
            return Err("completed takeover must have a returned timestamp".into());
        }
        if let Some(detail) = &self.failure_detail {
            validate_visible(detail, MAX_FAILURE, "takeover failure detail")?;
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TakeoverAuditEvent {
    pub audit_id: String,
    pub session_id: String,
    pub channel_id: String,
    pub source_id: String,
    pub operator_id: String,
    pub state: TakeoverState,
    pub occurred_at_unix_ms: i64,
    pub detail: Option<String>,
}

impl TakeoverAuditEvent {
    pub fn validate(&self) -> Result<(), String> {
        validate_visible(&self.audit_id, 120, "takeover audit identity")?;
        validate_uuid(&self.session_id, "takeover audit session identity")?;
        validate_uuid(&self.channel_id, "takeover audit channel identity")?;
        validate_uuid(&self.source_id, "takeover audit source identity")?;
        validate_visible(&self.operator_id, MAX_OPERATOR, "takeover audit operator")?;
        if self.occurred_at_unix_ms < 0 {
            return Err("takeover audit time must be nonnegative".into());
        }
        if let Some(detail) = &self.detail {
            validate_visible(detail, MAX_FAILURE, "takeover audit detail")?;
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LiveSourceWriteError {
    Conflict(u64),
    Invalid(String),
    Storage(String),
}

fn validate_uuid(value: &str, field: &str) -> Result<(), String> {
    let bytes = value.as_bytes();
    if bytes.len() != 36
        || ![8, 13, 18, 23].iter().all(|&index| bytes[index] == b'-')
        || bytes.iter().enumerate().any(|(index, byte)| {
            index != 8 && index != 13 && index != 18 && index != 23 && !byte.is_ascii_hexdigit()
        })
    {
        return Err(format!("{field} must be a UUID"));
    }
    Ok(())
}

fn validate_visible(value: &str, maximum: usize, field: &str) -> Result<(), String> {
    if value.is_empty()
        || value.trim() != value
        || value.len() > maximum
        || value.chars().any(char::is_control)
    {
        return Err(format!(
            "{field} must be nonempty, trimmed, visible, and bounded"
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    const UUID: &str = "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e";

    #[test]
    fn validates_typed_udp_endpoint_without_pipeline_strings() {
        let source = LiveSource {
            source_id: UUID.into(),
            channel_id: UUID.into(),
            label: "Council feed".into(),
            enabled: true,
            endpoint: LiveEndpoint::UdpMpegTs {
                bind_ip: "239.1.2.3".into(),
                port: 5000,
            },
            revision: 1,
        };
        assert!(source.validate().is_ok());
        assert!(
            LiveEndpoint::UdpMpegTs {
                bind_ip: "0.0.0.0".into(),
                port: 0
            }
            .validate()
            .is_err()
        );
        assert!(
            LiveEndpoint::UdpMpegTs {
                bind_ip: "not-an-ip".into(),
                port: 5000
            }
            .validate()
            .is_err()
        );
        assert!(
            LiveEndpoint::UdpMpegTs {
                bind_ip: "fd00::1".into(),
                port: 5000
            }
            .validate()
            .is_ok()
        );
    }

    #[test]
    fn permits_only_declared_takeover_transitions() {
        assert!(TakeoverState::Arming.can_transition_to(TakeoverState::Armed));
        assert!(TakeoverState::OnAir.can_transition_to(TakeoverState::ReturnRequested));
        assert!(!TakeoverState::Arming.can_transition_to(TakeoverState::Completed));
        assert!(!TakeoverState::Completed.is_active());
    }
}
