use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

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

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SystemResourceSample {
    pub sample_id: Option<u64>,
    pub sampled_at_unix_ms: i64,
    pub cpu_percent: Option<f64>,
    pub ram_used_gb: Option<f64>,
    pub ram_total_gb: Option<f64>,
    pub gpu_percent: Option<f64>,
    pub gpu_vram_used_gb: Option<f64>,
    pub media_volume_free_gb: Option<f64>,
    pub backup_volume_free_gb: Option<f64>,
    pub database_reachable: bool,
    pub backup_volume_writable: bool,
    pub service_running: bool,
    pub clock_skew_seconds: Option<f64>,
}

impl SystemResourceSample {
    pub fn validate(&self) -> Result<(), &'static str> {
        let percentage = |value: Option<f64>| {
            value.is_none_or(|number| number.is_finite() && (0.0..=100.0).contains(&number))
        };
        let nonnegative =
            |value: Option<f64>| value.is_none_or(|number| number.is_finite() && number >= 0.0);
        if self.sample_id == Some(0)
            || self.sampled_at_unix_ms < 0
            || !percentage(self.cpu_percent)
            || !percentage(self.gpu_percent)
            || !nonnegative(self.ram_used_gb)
            || !nonnegative(self.ram_total_gb)
            || !nonnegative(self.gpu_vram_used_gb)
            || !nonnegative(self.media_volume_free_gb)
            || !nonnegative(self.backup_volume_free_gb)
            || self
                .ram_used_gb
                .zip(self.ram_total_gb)
                .is_some_and(|(used, total)| used > total)
            || self
                .clock_skew_seconds
                .is_some_and(|value| !value.is_finite())
        {
            return Err("system resource sample fields are invalid.");
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SelfTestKind {
    Daily,
    Weekly,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SelfTestTrigger {
    Scheduled,
    Manual,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SelfTestStatus {
    Pass,
    Warn,
    Fail,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SystemSelfTest {
    pub self_test_id: String,
    pub kind: SelfTestKind,
    pub trigger: SelfTestTrigger,
    pub schedule_period_key: Option<String>,
    pub started_at_unix_ms: i64,
    pub finished_at_unix_ms: i64,
    pub status: SelfTestStatus,
    pub checks: BTreeMap<String, bool>,
    pub summary: String,
    pub evidence_path: Option<String>,
}

impl SystemSelfTest {
    pub fn validate(&self) -> Result<(), &'static str> {
        if !visible_text(&self.self_test_id, 120)
            || self.started_at_unix_ms < 0
            || self.finished_at_unix_ms < self.started_at_unix_ms
            || self.checks.is_empty()
            || self.checks.len() > 64
            || self.checks.keys().any(|name| !visible_text(name, 120))
            || !visible_text(&self.summary, 600)
            || self
                .evidence_path
                .as_deref()
                .is_some_and(|path| !visible_text(path, 500))
            || self
                .schedule_period_key
                .as_deref()
                .is_some_and(|key| !visible_text(key, 40))
            || (self.trigger == SelfTestTrigger::Scheduled) != self.schedule_period_key.is_some()
            || (self.status == SelfTestStatus::Pass && self.checks.values().any(|passed| !passed))
        {
            return Err("system self-test fields are invalid.");
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SelfTestSchedule {
    pub kind: SelfTestKind,
    pub enabled: bool,
    pub local_hour: u8,
    pub local_minute: u8,
    pub weekday: Option<u8>,
    pub updated_at_unix_ms: i64,
    pub updated_by: String,
}

impl SelfTestSchedule {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.local_hour > 23
            || self.local_minute > 59
            || self.updated_at_unix_ms < 0
            || !visible_text(&self.updated_by, 120)
            || match self.kind {
                SelfTestKind::Daily => self.weekday.is_some(),
                SelfTestKind::Weekly => self.weekday.is_none_or(|day| day > 6),
            }
        {
            return Err("self-test schedule fields are invalid.");
        }
        Ok(())
    }
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
    OutputStall,
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
    SelfTestFail,
    UpdateAvailable,
    UpdateCheckFailed,
    UpdateApplyFailed,
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
pub enum AlertChannelKind {
    Email,
    Sms,
    Webhook,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AlertRule {
    pub rule_id: String,
    pub condition: AlertConditionKind,
    pub enabled: bool,
    pub severity: AlertSeverity,
    pub channel_ids: Vec<String>,
    pub dedupe_window_seconds: u32,
    pub re_alert_after_seconds: u32,
    pub scope_channel_id: Option<String>,
    pub notify_on_resolve: bool,
    pub updated_at_unix_ms: i64,
    pub updated_by: String,
}

impl AlertRule {
    pub fn validate(&self) -> Result<(), &'static str> {
        let mut unique_channels = std::collections::HashSet::new();
        if !visible_text(&self.rule_id, 120)
            || !visible_text(&self.updated_by, 120)
            || self.updated_at_unix_ms < 0
            || self.dedupe_window_seconds > 86_400
            || self.re_alert_after_seconds > 604_800
            || self.channel_ids.len() > 64
            || self
                .channel_ids
                .iter()
                .any(|channel_id| !is_uuid(channel_id) || !unique_channels.insert(channel_id))
            || self
                .scope_channel_id
                .as_deref()
                .is_some_and(|channel_id| !is_uuid(channel_id))
        {
            return Err("alert rule fields are invalid.");
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AlertChannel {
    pub channel_id: String,
    pub kind: AlertChannelKind,
    pub label: String,
    pub enabled: bool,
    pub target_redacted: String,
    pub credential_configured: bool,
    pub quiet_hours_start_utc: Option<String>,
    pub quiet_hours_end_utc: Option<String>,
    pub created_at_unix_ms: i64,
    pub updated_at_unix_ms: i64,
}

impl AlertChannel {
    pub fn validate(&self) -> Result<(), &'static str> {
        if !is_uuid(&self.channel_id)
            || !visible_text(&self.label, 120)
            || !visible_text(&self.target_redacted, 200)
            || self.created_at_unix_ms < 0
            || self.updated_at_unix_ms < self.created_at_unix_ms
            || self.quiet_hours_start_utc.is_some() != self.quiet_hours_end_utc.is_some()
            || self
                .quiet_hours_start_utc
                .as_ref()
                .is_some_and(|start| self.quiet_hours_end_utc.as_ref() == Some(start))
            || self
                .quiet_hours_start_utc
                .as_deref()
                .is_some_and(|value| !is_utc_clock(value))
            || self
                .quiet_hours_end_utc
                .as_deref()
                .is_some_and(|value| !is_utc_clock(value))
        {
            return Err("alert channel fields are invalid.");
        }
        Ok(())
    }
}

pub fn validate_alert_channel_target(
    kind: AlertChannelKind,
    target: &str,
) -> Result<(), &'static str> {
    if target.trim() != target || target.chars().any(char::is_control) || target.len() > 500 {
        return Err("alert destination target is invalid.");
    }
    match kind {
        AlertChannelKind::Email => {
            let Some((local, domain)) = target.split_once('@') else {
                return Err("email alert targets must be a single email address.");
            };
            if local.is_empty()
                || domain.is_empty()
                || domain.contains('@')
                || !domain.contains('.')
                || target.contains([',', ';', ' '])
            {
                return Err("email alert targets must be a single email address.");
            }
        }
        AlertChannelKind::Sms => {
            if target.len() < 9
                || target.len() > 16
                || !target.starts_with('+')
                || !target[1..].bytes().all(|byte| byte.is_ascii_digit())
            {
                return Err("SMS alert targets must be an E.164 number.");
            }
        }
        AlertChannelKind::Webhook => validate_secure_url(target, "webhook")?,
    }
    Ok(())
}

pub fn redact_alert_channel_target(
    kind: AlertChannelKind,
    target: &str,
) -> Result<String, &'static str> {
    validate_alert_channel_target(kind, target)?;
    Ok(match kind {
        AlertChannelKind::Email => {
            let (local, domain) = target.split_once('@').expect("validated email");
            format!(
                "{}***@{domain}",
                local.chars().next().expect("nonempty local")
            )
        }
        AlertChannelKind::Sms => format!("••••{}", &target[target.len() - 4..]),
        AlertChannelKind::Webhook => {
            let (scheme, remainder) = target.split_once("://").expect("validated URL");
            let authority = remainder
                .split(['/', '?', '#'])
                .next()
                .expect("validated authority");
            format!("{scheme}://{authority}/…")
        }
    })
}

pub fn validate_secure_url(value: &str, label: &str) -> Result<(), &'static str> {
    if value.trim() != value
        || value.is_empty()
        || value.len() > 2048
        || value.chars().any(char::is_control)
    {
        return Err("alert destination URL is invalid.");
    }
    let (secure, remainder) = if let Some(value) = value.strip_prefix("https://") {
        (true, value)
    } else if let Some(value) = value.strip_prefix("http://") {
        (false, value)
    } else {
        return Err("alert destination URLs must use https.");
    };
    let authority = remainder.split(['/', '?', '#']).next().unwrap_or_default();
    if authority.is_empty()
        || authority.contains('@')
        || authority.chars().any(char::is_whitespace)
        || value.contains('#')
    {
        return Err("alert destination URL is invalid.");
    }
    let host = authority.split(':').next().unwrap_or_default();
    let loopback = host.eq_ignore_ascii_case("localhost") || host == "127.0.0.1";
    if !secure && !loopback {
        return Err("remote alert destination URLs must use https.");
    }
    if label.is_empty() {
        return Err("alert destination URL label is invalid.");
    }
    Ok(())
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
        if !visible_text(&self.event_id, 160)
            || !visible_text(&self.resource_ref, 200)
            || !visible_text(&self.summary, 300)
            || self.detail.len() > 2_000
            || self
                .detail
                .chars()
                .any(|character| character.is_control() && !matches!(character, '\r' | '\n' | '\t'))
            || !visible_text(&self.source_section, 8)
            || self.source_section.len() < 2
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
        if self
            .acknowledged_by
            .as_deref()
            .is_some_and(|value| !visible_text(value, 120))
        {
            return Err("alert acknowledgment operator is invalid.");
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AlertNotificationKind {
    Firing,
    Realert,
    Resolved,
    Test,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AlertDeliveryState {
    Pending,
    Leased,
    Sent,
    DeadLetter,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AlertDelivery {
    pub delivery_id: String,
    pub event_id: String,
    pub alert_channel_id: String,
    pub notification_kind: AlertNotificationKind,
    pub state: AlertDeliveryState,
    pub attempts: u32,
    pub next_attempt_at_unix_ms: i64,
    pub lease_expires_at_unix_ms: Option<i64>,
    pub last_error: String,
    pub signature: Option<String>,
    pub created_at_unix_ms: i64,
    pub updated_at_unix_ms: i64,
    pub sent_at_unix_ms: Option<i64>,
}

impl AlertDelivery {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.delivery_id.trim().is_empty()
            || self.delivery_id.len() > 160
            || self.event_id.trim().is_empty()
            || self.event_id.len() > 160
            || self.alert_channel_id.trim().is_empty()
            || self.alert_channel_id.len() > 160
            || self.last_error.len() > 1_000
            || self.last_error.chars().any(char::is_control)
            || self.signature.as_ref().is_some_and(|value| {
                value.is_empty() || value.len() > 200 || value.chars().any(char::is_control)
            })
            || self.created_at_unix_ms < 0
            || self.updated_at_unix_ms < self.created_at_unix_ms
            || self.next_attempt_at_unix_ms < self.created_at_unix_ms
        {
            return Err("alert delivery fields are invalid.");
        }
        match self.state {
            AlertDeliveryState::Pending => {
                if self.lease_expires_at_unix_ms.is_some() || self.sent_at_unix_ms.is_some() {
                    return Err("a pending alert delivery cannot be leased or sent.");
                }
            }
            AlertDeliveryState::Leased => {
                if self.attempts == 0
                    || self
                        .lease_expires_at_unix_ms
                        .is_none_or(|value| value <= self.updated_at_unix_ms)
                    || self.sent_at_unix_ms.is_some()
                {
                    return Err("a leased alert delivery needs a future lease.");
                }
            }
            AlertDeliveryState::Sent => {
                if self.lease_expires_at_unix_ms.is_some()
                    || self
                        .sent_at_unix_ms
                        .is_none_or(|value| value < self.created_at_unix_ms)
                {
                    return Err("a sent alert delivery needs a valid sent time.");
                }
            }
            AlertDeliveryState::DeadLetter => {
                if self.attempts == 0
                    || self.lease_expires_at_unix_ms.is_some()
                    || self.sent_at_unix_ms.is_some()
                    || self.last_error.is_empty()
                {
                    return Err("a dead-letter alert delivery needs a recorded failure.");
                }
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AlertDeliveryPayload {
    pub notification_kind: AlertNotificationKind,
    pub event_id: String,
    pub condition: AlertConditionKind,
    pub severity: AlertSeverity,
    pub resource_ref: String,
    pub summary: String,
    pub detail: String,
    pub source_section: String,
    pub first_observed_at_unix_ms: i64,
    pub last_observed_at_unix_ms: i64,
    pub resolved_at_unix_ms: Option<i64>,
    pub occurrence_count: u64,
}

impl AlertDeliveryPayload {
    pub fn from_event(
        notification_kind: AlertNotificationKind,
        event: &AlertEvent,
    ) -> Result<Self, &'static str> {
        event.validate()?;
        match (notification_kind, event.state) {
            (AlertNotificationKind::Firing, AlertEventState::Firing)
            | (AlertNotificationKind::Realert, AlertEventState::Firing)
            | (AlertNotificationKind::Resolved, AlertEventState::Resolved)
            | (AlertNotificationKind::Test, AlertEventState::Resolved) => {}
            _ => return Err("alert notification kind does not match the event state."),
        }
        Ok(Self {
            notification_kind,
            event_id: event.event_id.clone(),
            condition: event.condition,
            severity: event.severity,
            resource_ref: event.resource_ref.clone(),
            summary: event.summary.clone(),
            detail: event.detail.clone(),
            source_section: event.source_section.clone(),
            first_observed_at_unix_ms: event.first_observed_at_unix_ms,
            last_observed_at_unix_ms: event.last_observed_at_unix_ms,
            resolved_at_unix_ms: event.resolved_at_unix_ms,
            occurrence_count: event.occurrence_count,
        })
    }

    pub fn validate(&self) -> Result<(), &'static str> {
        let state = match self.notification_kind {
            AlertNotificationKind::Firing | AlertNotificationKind::Realert => {
                AlertEventState::Firing
            }
            AlertNotificationKind::Resolved => AlertEventState::Resolved,
            AlertNotificationKind::Test => AlertEventState::Resolved,
        };
        AlertEvent {
            event_id: self.event_id.clone(),
            condition: self.condition,
            severity: self.severity,
            state,
            resource_ref: self.resource_ref.clone(),
            summary: self.summary.clone(),
            detail: self.detail.clone(),
            source_section: self.source_section.clone(),
            first_observed_at_unix_ms: self.first_observed_at_unix_ms,
            last_observed_at_unix_ms: self.last_observed_at_unix_ms,
            resolved_at_unix_ms: self.resolved_at_unix_ms,
            occurrence_count: self.occurrence_count,
            acknowledged_at_unix_ms: None,
            acknowledged_by: None,
        }
        .validate()
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

fn visible_text(value: &str, maximum: usize) -> bool {
    !value.trim().is_empty() && value.len() <= maximum && !value.chars().any(char::is_control)
}

fn is_utc_clock(value: &str) -> bool {
    value.len() == 5
        && value.as_bytes()[2] == b':'
        && value[..2].parse::<u8>().is_ok_and(|hours| hours < 24)
        && value[3..].parse::<u8>().is_ok_and(|minutes| minutes < 60)
}

#[cfg(test)]
mod runtime_tests {
    use super::*;

    #[test]
    fn alert_rule_enforces_bounded_realert_and_destination_identity() {
        let valid = AlertRule {
            rule_id: "off-air-default".into(),
            condition: AlertConditionKind::OffAir,
            enabled: true,
            severity: AlertSeverity::Critical,
            channel_ids: vec!["406798db-38d1-4f2f-b447-1f6558135ba0".into()],
            dedupe_window_seconds: 900,
            re_alert_after_seconds: 3_600,
            scope_channel_id: None,
            notify_on_resolve: true,
            updated_at_unix_ms: 1_000,
            updated_by: "system-seed".into(),
        };
        assert!(valid.validate().is_ok());

        let mut invalid = valid.clone();
        invalid.re_alert_after_seconds = 604_801;
        assert_eq!(invalid.validate(), Err("alert rule fields are invalid."));

        invalid = valid;
        invalid.channel_ids.push("not-a-uuid".into());
        assert_eq!(invalid.validate(), Err("alert rule fields are invalid."));
    }

    #[test]
    fn resource_sample_rejects_impossible_or_nonfinite_measurements() {
        let valid = SystemResourceSample {
            sample_id: None,
            sampled_at_unix_ms: 1_000,
            cpu_percent: Some(42.5),
            ram_used_gb: Some(8.0),
            ram_total_gb: Some(16.0),
            gpu_percent: None,
            gpu_vram_used_gb: None,
            media_volume_free_gb: Some(120.0),
            backup_volume_free_gb: Some(120.0),
            database_reachable: true,
            backup_volume_writable: true,
            service_running: true,
            clock_skew_seconds: None,
        };
        assert!(valid.validate().is_ok());
        let mut invalid = valid.clone();
        invalid.cpu_percent = Some(f64::NAN);
        assert_eq!(
            invalid.validate(),
            Err("system resource sample fields are invalid.")
        );
        invalid = valid;
        invalid.ram_used_gb = Some(17.0);
        assert_eq!(
            invalid.validate(),
            Err("system resource sample fields are invalid.")
        );
    }

    #[test]
    fn self_tests_require_honest_status_and_scheduled_period_identity() {
        let mut checks = std::collections::BTreeMap::new();
        checks.insert("database_integrity".into(), true);
        let valid = SystemSelfTest {
            self_test_id: "self-test-daily-1000".into(),
            kind: SelfTestKind::Daily,
            trigger: SelfTestTrigger::Scheduled,
            schedule_period_key: Some("2026-08-21".into()),
            started_at_unix_ms: 1_000,
            finished_at_unix_ms: 1_100,
            status: SelfTestStatus::Pass,
            checks,
            summary: "All implemented daily checks passed.".into(),
            evidence_path: None,
        };
        assert!(valid.validate().is_ok());
        let mut invalid = valid.clone();
        invalid.schedule_period_key = None;
        assert_eq!(
            invalid.validate(),
            Err("system self-test fields are invalid.")
        );
        invalid = valid.clone();
        invalid.checks.insert("backup_probe".into(), false);
        assert_eq!(
            invalid.validate(),
            Err("system self-test fields are invalid.")
        );

        let daily = SelfTestSchedule {
            kind: SelfTestKind::Daily,
            enabled: true,
            local_hour: 2,
            local_minute: 0,
            weekday: None,
            updated_at_unix_ms: 0,
            updated_by: "system-seed".into(),
        };
        assert!(daily.validate().is_ok());
        let mut weekly = daily;
        weekly.kind = SelfTestKind::Weekly;
        weekly.local_hour = 3;
        weekly.weekday = Some(0);
        assert!(weekly.validate().is_ok());
        weekly.weekday = None;
        assert_eq!(
            weekly.validate(),
            Err("self-test schedule fields are invalid.")
        );
    }

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
