use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StationProfile {
    pub station_id: String,
    pub display_name: String,
    pub timezone: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct StationProfileDocument {
    #[serde(flatten)]
    pub profile: StationProfile,
    pub revision: u64,
}

impl StationProfile {
    pub fn validate(&self) -> Result<(), &'static str> {
        if !is_uuid(&self.station_id) {
            return Err("station_id must be a canonical UUID.");
        }
        let display_name = self.display_name.trim();
        if display_name.is_empty()
            || display_name.len() > 120
            || display_name.chars().any(char::is_control)
        {
            return Err("display_name must contain 1 to 120 visible characters.");
        }
        if !is_iana_timezone(&self.timezone) {
            return Err("timezone must be an IANA timezone such as America/Denver.");
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ChannelRuntimeState {
    Starting,
    HealthyFallback,
    OnAir,
    Error,
    Stopped,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChannelRuntimeObservation {
    pub channel_id: String,
    pub state: ChannelRuntimeState,
    pub on_air_asset_id: Option<String>,
    pub state_started_at_unix_ms: i64,
    pub heartbeat_at_unix_ms: i64,
    pub detail: Option<String>,
}

impl ChannelRuntimeObservation {
    pub fn validate(&self) -> Result<(), &'static str> {
        if !is_uuid(&self.channel_id) {
            return Err("channel_id must be a canonical UUID.");
        }
        if self.state_started_at_unix_ms < 0
            || self.heartbeat_at_unix_ms < self.state_started_at_unix_ms
        {
            return Err("runtime timestamps must be nonnegative and monotonic.");
        }
        if self
            .on_air_asset_id
            .as_deref()
            .is_some_and(|value| !is_sha256(value))
        {
            return Err("on_air_asset_id must be a lowercase SHA-256 digest.");
        }
        if (self.state == ChannelRuntimeState::OnAir) != self.on_air_asset_id.is_some() {
            return Err("only an on-air runtime may carry an on-air asset identity.");
        }
        if self
            .detail
            .as_deref()
            .is_some_and(|value| value.trim().is_empty() || value.len() > 1_000)
        {
            return Err("runtime detail must contain 1 to 1000 characters.");
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SafeToAirColor {
    Green,
    Yellow,
    Red,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChannelRuntimeStatus {
    pub channel_id: String,
    pub display_name: String,
    pub state: ChannelRuntimeState,
    pub on_air: bool,
    pub on_healthy_fallback: bool,
    pub seconds_in_state: u64,
    pub heartbeat_age_seconds: u64,
    pub color: SafeToAirColor,
    pub operator_message: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RuntimeSafeToAirStatus {
    pub generated_at_unix_ms: i64,
    pub color: SafeToAirColor,
    pub label: String,
    pub operator_message: String,
    pub channels: Vec<ChannelRuntimeStatus>,
    pub active_critical_alerts: usize,
    pub active_warning_alerts: usize,
}

pub fn derive_runtime_safe_to_air(
    now_unix_ms: i64,
    channels: &[(String, String, Option<ChannelRuntimeObservation>)],
    active_critical_alerts: usize,
    active_warning_alerts: usize,
) -> Result<RuntimeSafeToAirStatus, &'static str> {
    if now_unix_ms < 0 {
        return Err("runtime status time must be nonnegative.");
    }
    let mut statuses = Vec::with_capacity(channels.len());
    for (channel_id, display_name, observation) in channels {
        if !is_uuid(channel_id)
            || display_name.trim().is_empty()
            || display_name.len() > 120
            || display_name.chars().any(char::is_control)
        {
            return Err("runtime channel identity or display name is invalid.");
        }
        let status = match observation {
            None => ChannelRuntimeStatus {
                channel_id: channel_id.clone(),
                display_name: display_name.clone(),
                state: ChannelRuntimeState::Stopped,
                on_air: false,
                on_healthy_fallback: false,
                seconds_in_state: 0,
                heartbeat_age_seconds: 0,
                color: SafeToAirColor::Red,
                operator_message: "No worker runtime has reported for this enabled channel.".into(),
            },
            Some(observation) => {
                observation.validate()?;
                if observation.channel_id != *channel_id {
                    return Err("runtime observation belongs to a different channel.");
                }
                let heartbeat_age_ms = now_unix_ms
                    .checked_sub(observation.heartbeat_at_unix_ms)
                    .ok_or("runtime heartbeat is in the future.")?;
                let state_age_ms = now_unix_ms
                    .checked_sub(observation.state_started_at_unix_ms)
                    .ok_or("runtime state start is in the future.")?;
                let heartbeat_stale = heartbeat_age_ms > 15_000;
                let (color, operator_message) = if heartbeat_stale {
                    (
                        SafeToAirColor::Red,
                        "Worker heartbeat is stale; output cannot be trusted.".into(),
                    )
                } else {
                    match observation.state {
                        ChannelRuntimeState::HealthyFallback => (
                            SafeToAirColor::Green,
                            "Healthy fallback is on air between programs.".into(),
                        ),
                        ChannelRuntimeState::OnAir => {
                            (SafeToAirColor::Green, "Scheduled media is on air.".into())
                        }
                        ChannelRuntimeState::Starting => (
                            SafeToAirColor::Yellow,
                            "Channel worker is starting; fallback is not yet confirmed.".into(),
                        ),
                        ChannelRuntimeState::Error => (
                            SafeToAirColor::Red,
                            observation
                                .detail
                                .clone()
                                .unwrap_or_else(|| "Channel worker reported an error.".into()),
                        ),
                        ChannelRuntimeState::Stopped => (
                            SafeToAirColor::Red,
                            "Enabled channel worker is stopped.".into(),
                        ),
                    }
                };
                ChannelRuntimeStatus {
                    channel_id: channel_id.clone(),
                    display_name: display_name.clone(),
                    state: observation.state,
                    on_air: observation.state == ChannelRuntimeState::OnAir && !heartbeat_stale,
                    on_healthy_fallback: observation.state == ChannelRuntimeState::HealthyFallback
                        && !heartbeat_stale,
                    seconds_in_state: u64::try_from(state_age_ms / 1_000).unwrap_or(u64::MAX),
                    heartbeat_age_seconds: u64::try_from(heartbeat_age_ms / 1_000)
                        .unwrap_or(u64::MAX),
                    color,
                    operator_message,
                }
            }
        };
        statuses.push(status);
    }
    let mut color = statuses
        .iter()
        .map(|status| status.color)
        .max()
        .unwrap_or(SafeToAirColor::Yellow);
    if active_critical_alerts > 0 {
        color = SafeToAirColor::Red;
    } else if active_warning_alerts > 0 && color == SafeToAirColor::Green {
        color = SafeToAirColor::Yellow;
    }
    let (label, operator_message) = match color {
        SafeToAirColor::Green => (
            "On air — all channels healthy.",
            "Every enabled channel is on scheduled media or verified fallback.",
        ),
        SafeToAirColor::Yellow => (
            "Station needs attention.",
            "Air is not known to be down, but at least one runtime check is degraded.",
        ),
        SafeToAirColor::Red => (
            "OFF AIR RISK — act now.",
            "At least one enabled channel or critical operating condition is unhealthy.",
        ),
    };
    Ok(RuntimeSafeToAirStatus {
        generated_at_unix_ms: now_unix_ms,
        color,
        label: label.into(),
        operator_message: operator_message.into(),
        channels: statuses,
        active_critical_alerts,
        active_warning_alerts,
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum AlertConditionKind {
    OffAir,
    EncoderDeath,
    ServerCrash,
    SchemaDrift,
    RelayBlocked,
    ComplianceProbeFail,
    MissingMedia,
    CommitFailure,
    TakeoverStuck2h,
    AiRuntimeDown,
    DiskLow,
    ClockSkew,
    DatabaseUnreachable,
    ServiceDown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AlertSeverity {
    Critical,
    Warning,
    Info,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AlertEventState {
    Firing,
    Resolved,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AlertEvent {
    pub event_id: String,
    pub condition: AlertConditionKind,
    pub severity: AlertSeverity,
    pub state: AlertEventState,
    pub resource_ref: String,
    pub summary: String,
    pub detail: String,
    pub source_section: String,
    pub first_observed_at_unix_ms: i64,
    pub last_observed_at_unix_ms: i64,
    pub resolved_at_unix_ms: Option<i64>,
    pub occurrence_count: u64,
    pub acknowledged_at_unix_ms: Option<i64>,
    pub acknowledged_by: Option<String>,
}

impl AlertEvent {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.event_id.trim().is_empty()
            || self.event_id.len() > 160
            || self.resource_ref.trim().is_empty()
            || self.resource_ref.len() > 200
            || self.summary.trim().is_empty()
            || self.summary.len() > 300
            || self.detail.len() > 2_000
            || self.source_section.len() < 2
            || self.source_section.len() > 8
            || self.first_observed_at_unix_ms < 0
            || self.last_observed_at_unix_ms < self.first_observed_at_unix_ms
            || self.occurrence_count == 0
        {
            return Err("alert event fields are invalid.");
        }
        match self.state {
            AlertEventState::Firing if self.resolved_at_unix_ms.is_some() => {
                return Err("a firing alert cannot have a resolution time.");
            }
            AlertEventState::Resolved
                if self
                    .resolved_at_unix_ms
                    .is_none_or(|value| value < self.last_observed_at_unix_ms) =>
            {
                return Err("a resolved alert needs a valid resolution time.");
            }
            _ => {}
        }
        if self.acknowledged_at_unix_ms.is_some() != self.acknowledged_by.is_some() {
            return Err("alert acknowledgment time and operator must be recorded together.");
        }
        Ok(())
    }
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn is_uuid(value: &str) -> bool {
    value.len() == 36
        && value.bytes().enumerate().all(|(index, byte)| match index {
            8 | 13 | 18 | 23 => byte == b'-',
            _ => byte.is_ascii_hexdigit(),
        })
}

fn is_iana_timezone(value: &str) -> bool {
    value == "UTC"
        || (value.contains('/')
            && value.split('/').all(|part| {
                !part.is_empty()
                    && part.bytes().all(|byte| {
                        byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'+')
                    })
            }))
}

#[cfg(test)]
mod runtime_tests {
    use super::*;

    const CHANNEL_ID: &str = "8ea48bc4-7f2e-4f89-9bbc-2c58472f0e8e";

    fn observation(
        state: ChannelRuntimeState,
        heartbeat_at_unix_ms: i64,
    ) -> ChannelRuntimeObservation {
        ChannelRuntimeObservation {
            channel_id: CHANNEL_ID.into(),
            state,
            on_air_asset_id: (state == ChannelRuntimeState::OnAir).then(|| "a".repeat(64)),
            state_started_at_unix_ms: 1_000,
            heartbeat_at_unix_ms,
            detail: None,
        }
    }

    #[test]
    fn treats_verified_fallback_as_healthy_and_stale_heartbeat_as_red() {
        let healthy = derive_runtime_safe_to_air(
            10_000,
            &[(
                CHANNEL_ID.into(),
                "Government Channel".into(),
                Some(observation(ChannelRuntimeState::HealthyFallback, 9_000)),
            )],
            0,
            0,
        )
        .unwrap();
        assert_eq!(healthy.color, SafeToAirColor::Green);
        assert!(healthy.channels[0].on_healthy_fallback);

        let stale = derive_runtime_safe_to_air(
            30_001,
            &[(
                CHANNEL_ID.into(),
                "Government Channel".into(),
                Some(observation(ChannelRuntimeState::HealthyFallback, 15_000)),
            )],
            0,
            0,
        )
        .unwrap();
        assert_eq!(stale.color, SafeToAirColor::Red);
        assert!(!stale.channels[0].on_healthy_fallback);
    }

    #[test]
    fn critical_alert_escalates_otherwise_healthy_station() {
        let status = derive_runtime_safe_to_air(
            10_000,
            &[(
                CHANNEL_ID.into(),
                "Government Channel".into(),
                Some(observation(ChannelRuntimeState::OnAir, 9_000)),
            )],
            1,
            0,
        )
        .unwrap();
        assert_eq!(status.color, SafeToAirColor::Red);
        assert_eq!(status.active_critical_alerts, 1);
    }

    #[test]
    fn validates_alert_lifecycle_invariants() {
        let mut event = AlertEvent {
            event_id: "alert-1".into(),
            condition: AlertConditionKind::EncoderDeath,
            severity: AlertSeverity::Critical,
            state: AlertEventState::Firing,
            resource_ref: CHANNEL_ID.into(),
            summary: "Channel worker exited unexpectedly.".into(),
            detail: String::new(),
            source_section: "S8".into(),
            first_observed_at_unix_ms: 1_000,
            last_observed_at_unix_ms: 2_000,
            resolved_at_unix_ms: None,
            occurrence_count: 2,
            acknowledged_at_unix_ms: None,
            acknowledged_by: None,
        };
        event.validate().unwrap();
        event.state = AlertEventState::Resolved;
        assert!(event.validate().is_err());
        event.resolved_at_unix_ms = Some(2_001);
        event.validate().unwrap();
    }
}
