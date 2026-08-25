//! Pure, privacy-minimized analytics and audience authority contracts.
//!
//! Collection, persistence, dashboards, PDF rendering, and delivery are outside
//! this crate.

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

const MAX_DIMENSIONS: usize = 8;
const MAX_EVENTS_PER_ROLLUP: usize = 10_000;
const MAX_ROLLUPS_PER_SNAPSHOT: usize = 512;
const MAX_WINDOW_MS: i64 = 366 * 86_400_000;
const MIN_YEAR_GAP_MS: i64 = 365 * 86_400_000;
const MAX_YEAR_GAP_MS: i64 = 366 * 86_400_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AudienceDelivery {
    Live,
    Vod,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AnalyticsEventKind {
    ViewStarted,
    Heartbeat,
    ViewCompleted,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AnalyticsDimension {
    pub key: String,
    pub value: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OttBeaconTrust {
    pub source_id: String,
    pub revision: u64,
    pub enabled: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OttBeaconAssertion {
    pub source_id: String,
    pub source_revision: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AnalyticsEvent {
    pub event_id: String,
    pub channel_id: String,
    pub content_sha256: String,
    pub delivery: AudienceDelivery,
    pub kind: AnalyticsEventKind,
    pub occurred_at_utc_ms: i64,
    pub dimensions: Vec<AnalyticsDimension>,
    pub ott_beacon: Option<OttBeaconAssertion>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AudienceWindow {
    pub starts_at_utc_ms: i64,
    pub ends_at_utc_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AudienceRollup {
    pub rollup_id: String,
    pub channel_id: String,
    pub content_sha256: String,
    pub delivery: AudienceDelivery,
    pub kind: AnalyticsEventKind,
    pub window: AudienceWindow,
    pub dimensions: Vec<AnalyticsDimension>,
    pub event_count: u64,
    pub event_identity_sha256: String,
    pub revision: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AnalyticsSnapshot {
    pub snapshot_id: String,
    pub window: AudienceWindow,
    pub rollups: Vec<AudienceRollup>,
    pub revision: u64,
}

impl AudienceWindow {
    pub fn validate(&self) -> Result<(), String> {
        if self.starts_at_utc_ms < 0
            || self.ends_at_utc_ms <= self.starts_at_utc_ms
            || self.ends_at_utc_ms - self.starts_at_utc_ms > MAX_WINDOW_MS
        {
            return Err("analytics UTC window is invalid or ambiguous".into());
        }
        Ok(())
    }

    pub fn contains(&self, time: i64) -> bool {
        time >= self.starts_at_utc_ms && time < self.ends_at_utc_ms
    }
}

impl OttBeaconTrust {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.source_id, "OTT beacon source identity")?;
        if self.revision == 0 {
            return Err("OTT beacon trust revision must be nonzero".into());
        }
        Ok(())
    }
}

pub fn validate_event_intake(
    event: &AnalyticsEvent,
    trusted_ott_beacons: &[OttBeaconTrust],
) -> Result<(), String> {
    validate_event_shape(event)?;
    if let Some(assertion) = &event.ott_beacon {
        validate_uuid(&assertion.source_id, "OTT beacon assertion source identity")?;
        if assertion.source_revision == 0
            || !trusted_ott_beacons.iter().any(|trust| {
                trust.validate().is_ok()
                    && trust.enabled
                    && trust.source_id == assertion.source_id
                    && trust.revision == assertion.source_revision
            })
        {
            return Err(
                "OTT beacon assertion is not bound to current trusted source authority".into(),
            );
        }
    }
    Ok(())
}

fn validate_event_shape(event: &AnalyticsEvent) -> Result<(), String> {
    validate_uuid(&event.event_id, "analytics event identity")?;
    validate_uuid(&event.channel_id, "analytics event channel identity")?;
    if !is_sha256(&event.content_sha256) || event.occurred_at_utc_ms < 0 {
        return Err("analytics event content identity or UTC time is invalid".into());
    }
    validate_dimensions(&event.dimensions)?;
    Ok(())
}

impl AudienceRollup {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.channel_id, "analytics rollup channel identity")?;
        if self.revision == 0
            || self.event_count == 0
            || !is_sha256(&self.content_sha256)
            || !is_sha256(&self.event_identity_sha256)
        {
            return Err("analytics rollup revision, count, or identity is invalid".into());
        }
        self.window.validate()?;
        validate_dimensions(&self.dimensions)?;
        if self.rollup_id != derive_rollup_id(self) {
            return Err("analytics rollup identity is forged or inconsistent".into());
        }
        Ok(())
    }
}

impl AnalyticsSnapshot {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.snapshot_id, "analytics snapshot identity")?;
        if self.revision == 0
            || self.rollups.is_empty()
            || self.rollups.len() > MAX_ROLLUPS_PER_SNAPSHOT
        {
            return Err("analytics snapshot revision or bounded rollups are invalid".into());
        }
        self.window.validate()?;
        for (index, rollup) in self.rollups.iter().enumerate() {
            rollup.validate()?;
            if rollup.window != self.window
                || self.rollups[..index]
                    .iter()
                    .any(|prior| prior.rollup_id.as_str() >= rollup.rollup_id.as_str())
            {
                return Err(
                    "analytics snapshot rollup identity, order, or UTC window conflicts".into(),
                );
            }
        }
        Ok(())
    }
}

pub fn derive_utc_rollup(
    events: &[AnalyticsEvent],
    window: AudienceWindow,
    revision: u64,
) -> Result<AudienceRollup, String> {
    window.validate()?;
    if revision == 0 || events.is_empty() || events.len() > MAX_EVENTS_PER_ROLLUP {
        return Err("analytics rollup event count or revision is invalid or unbounded".into());
    }
    for (index, event) in events.iter().enumerate() {
        validate_event_shape(event)?;
        if !window.contains(event.occurred_at_utc_ms)
            || event.channel_id != events[0].channel_id
            || event.content_sha256 != events[0].content_sha256
            || event.delivery != events[0].delivery
            || event.kind != events[0].kind
            || event.dimensions != events[0].dimensions
            || events[..index]
                .iter()
                .any(|prior| prior.event_id == event.event_id)
        {
            return Err(
                "analytics rollup events do not share one exact aggregate authority".into(),
            );
        }
    }
    let event_identity_sha256 = derive_event_identity(events);
    let mut rollup = AudienceRollup {
        rollup_id: String::new(),
        channel_id: events[0].channel_id.clone(),
        content_sha256: events[0].content_sha256.clone(),
        delivery: events[0].delivery,
        kind: events[0].kind,
        window,
        dimensions: events[0].dimensions.clone(),
        event_count: events.len() as u64,
        event_identity_sha256,
        revision,
    };
    rollup.rollup_id = derive_rollup_id(&rollup);
    rollup.validate()?;
    Ok(rollup)
}

pub fn validate_snapshot_write(
    expected_revision: Option<u64>,
    current: Option<&AnalyticsSnapshot>,
    incoming: &AnalyticsSnapshot,
) -> Result<(), String> {
    incoming.validate()?;
    match (expected_revision, current) {
        (None, None) if incoming.revision == 1 => Ok(()),
        (Some(expected), Some(current))
            if expected == current.revision
                && incoming.revision == current.revision.saturating_add(1) =>
        {
            current.validate()?;
            if incoming.snapshot_id != current.snapshot_id || incoming.window != current.window {
                return Err("analytics snapshot identity or UTC window cannot be rebound".into());
            }
            Ok(())
        }
        _ => Err("analytics snapshot revision compare-and-set conflict".into()),
    }
}

pub fn validate_year_over_year_inputs(
    current: &AnalyticsSnapshot,
    prior: &AnalyticsSnapshot,
) -> Result<(), String> {
    current.validate()?;
    prior.validate()?;
    let current_duration = current.window.ends_at_utc_ms - current.window.starts_at_utc_ms;
    let prior_duration = prior.window.ends_at_utc_ms - prior.window.starts_at_utc_ms;
    let year_gap = current.window.starts_at_utc_ms - prior.window.starts_at_utc_ms;
    if current_duration != prior_duration
        || !(MIN_YEAR_GAP_MS..=MAX_YEAR_GAP_MS).contains(&year_gap)
    {
        return Err("year-over-year analytics windows are not comparable UTC inputs".into());
    }
    Ok(())
}

fn validate_dimensions(dimensions: &[AnalyticsDimension]) -> Result<(), String> {
    if dimensions.len() > MAX_DIMENSIONS {
        return Err("analytics dimensions exceed the privacy-minimized bound".into());
    }
    for (index, dimension) in dimensions.iter().enumerate() {
        if !matches!(
            dimension.key.as_str(),
            "platform" | "app" | "territory" | "device_class" | "content_category"
        ) || !is_safe_dimension_value(&dimension.value)
            || dimensions[..index]
                .iter()
                .any(|prior| prior.key.as_str() >= dimension.key.as_str())
        {
            return Err(
                "analytics dimensions violate privacy or deterministic key constraints".into(),
            );
        }
    }
    Ok(())
}

fn is_safe_dimension_value(value: &str) -> bool {
    (1..=64).contains(&value.len())
        && value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'-' | b'_')
        })
}

fn derive_event_identity(events: &[AnalyticsEvent]) -> String {
    let mut identities = events
        .iter()
        .map(|event| event.event_id.as_str())
        .collect::<Vec<_>>();
    identities.sort_unstable();
    let mut digest = Sha256::new();
    for identity in identities {
        hash_field(&mut digest, identity);
    }
    format!("{:x}", digest.finalize())
}

fn derive_rollup_id(rollup: &AudienceRollup) -> String {
    let mut digest = Sha256::new();
    hash_field(&mut digest, &rollup.channel_id);
    hash_field(&mut digest, &rollup.content_sha256);
    hash_field(&mut digest, delivery_name(rollup.delivery));
    hash_field(&mut digest, event_kind_name(rollup.kind));
    digest.update(rollup.window.starts_at_utc_ms.to_le_bytes());
    digest.update(rollup.window.ends_at_utc_ms.to_le_bytes());
    for dimension in &rollup.dimensions {
        hash_field(&mut digest, &dimension.key);
        hash_field(&mut digest, &dimension.value);
    }
    digest.update(rollup.event_count.to_le_bytes());
    hash_field(&mut digest, &rollup.event_identity_sha256);
    format!("{:x}", digest.finalize())
}

fn delivery_name(value: AudienceDelivery) -> &'static str {
    match value {
        AudienceDelivery::Live => "live",
        AudienceDelivery::Vod => "vod",
    }
}

fn event_kind_name(value: AnalyticsEventKind) -> &'static str {
    match value {
        AnalyticsEventKind::ViewStarted => "view_started",
        AnalyticsEventKind::Heartbeat => "heartbeat",
        AnalyticsEventKind::ViewCompleted => "view_completed",
    }
}

fn hash_field(digest: &mut Sha256, value: &str) {
    digest.update((value.len() as u64).to_le_bytes());
    digest.update(value.as_bytes());
}

fn validate_uuid(value: &str, field: &str) -> Result<(), String> {
    let bytes = value.as_bytes();
    if bytes.len() != 36
        || ![8, 13, 18, 23].iter().all(|&index| bytes[index] == b'-')
        || bytes.iter().enumerate().any(|(index, byte)| {
            index != 8
                && index != 13
                && index != 18
                && index != 23
                && !(byte.is_ascii_digit() || (b'a'..=b'f').contains(byte))
        })
    {
        return Err(format!("{field} must be a lowercase canonical UUID"));
    }
    Ok(())
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}
