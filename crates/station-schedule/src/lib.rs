use serde::{Deserialize, Serialize};
use std::collections::HashSet;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub enum OutputProfileId {
    #[default]
    #[serde(rename = "proof-360p30")]
    Proof360p30,
    #[serde(rename = "atsc-720p5994")]
    Atsc720p5994,
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize)]
pub struct OutputProfile {
    pub profile_id: OutputProfileId,
    pub display_name: &'static str,
    pub intended_use: &'static str,
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

pub const OUTPUT_PROFILES: [OutputProfile; 2] = [
    OutputProfile {
        profile_id: OutputProfileId::Proof360p30,
        display_name: "Local proof — 360p30",
        intended_use: "Loopback commissioning and low-load machine proof only",
        width: 640,
        height: 360,
        frame_rate_numerator: 30,
        frame_rate_denominator: 1,
        interlaced: false,
        video_bitrate_bps: 1_000_000,
        audio_bitrate_bps: 128_000,
        transport_bitrate_bps: 1_500_000,
        audio_sample_rate_hz: 48_000,
        loudness_target_lufs: -16.0,
    },
    OutputProfile {
        profile_id: OutputProfileId::Atsc720p5994,
        display_name: "ATSC/IP cable — 720p59.94",
        intended_use: "Progressive HD MPEG-TS handoff to an IP-capable headend",
        width: 1_280,
        height: 720,
        frame_rate_numerator: 60_000,
        frame_rate_denominator: 1_001,
        interlaced: false,
        video_bitrate_bps: 6_000_000,
        audio_bitrate_bps: 192_000,
        transport_bitrate_bps: 8_000_000,
        audio_sample_rate_hz: 48_000,
        loudness_target_lufs: -24.0,
    },
];

impl OutputProfileId {
    pub fn profile(self) -> &'static OutputProfile {
        OUTPUT_PROFILES
            .iter()
            .find(|profile| profile.profile_id == self)
            .expect("every output profile identifier has one specification")
    }

    pub const fn storage_name(self) -> &'static str {
        match self {
            Self::Proof360p30 => "proof-360p30",
            Self::Atsc720p5994 => "atsc-720p5994",
        }
    }

    pub fn from_storage_name(value: &str) -> Result<Self, ScheduleError> {
        match value {
            "proof-360p30" => Ok(Self::Proof360p30),
            "atsc-720p5994" => Ok(Self::Atsc720p5994),
            _ => Err(ScheduleError::Invalid("output_profile is unsupported")),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum OutputSinkKind {
    UdpTs,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OutputSinkConfiguration {
    pub sink_id: String,
    pub label: String,
    pub kind: OutputSinkKind,
    pub destination: String,
    pub loudness_target_lufs: f32,
    pub required: bool,
    pub enabled: bool,
}

impl OutputSinkConfiguration {
    pub fn required_udp(
        sink_id: impl Into<String>,
        label: impl Into<String>,
        destination: impl Into<String>,
        loudness_target_lufs: f32,
    ) -> Self {
        Self {
            sink_id: sink_id.into(),
            label: label.into(),
            kind: OutputSinkKind::UdpTs,
            destination: destination.into(),
            loudness_target_lufs,
            required: true,
            enabled: true,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MediaAsset {
    pub asset_id: String,
    pub media_path: String,
    pub duration_ms: u64,
    pub readiness: AssetReadiness,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AssetReadiness {
    Ready,
    Missing,
    Rejected,
    Processing,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ScheduleItem {
    pub item_id: String,
    pub channel_id: String,
    pub asset_id: String,
    pub title: String,
    pub starts_at_unix_ms: i64,
    pub duration_ms: u64,
    pub state: ScheduleState,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ChannelConfiguration {
    pub channel_id: String,
    pub display_name: String,
    #[serde(default)]
    pub output_profile: OutputProfileId,
    pub output_sinks: Vec<OutputSinkConfiguration>,
    pub enabled: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ScheduleState {
    Draft,
    Committed,
    Cancelled,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ScheduleConflict {
    pub existing_item_id: String,
    pub overlap_ms: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ScheduleGap {
    pub prior_item_id: String,
    pub gap_ms: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CommitPlan {
    pub plan_id: String,
    pub channel_id: String,
    pub schedule_item_id: String,
    pub dry_run_passed: bool,
    pub conflicts: Vec<ScheduleConflict>,
    pub missing_media_detail: Option<String>,
    pub gaps: Vec<ScheduleGap>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CommitReport {
    pub report_id: String,
    pub plan_id: String,
    pub channel_id: String,
    pub schedule_item_id: String,
    pub asset_id: String,
    pub approved_by: String,
    pub approved_at_unix_ms: i64,
    pub dispatch_status: DispatchStatus,
    pub operator_notes: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DispatchStatus {
    Pending,
    Queued,
    Acknowledged,
    Completed,
    Error,
    Cancelled,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DispatchJob {
    pub report: CommitReport,
    pub item: ScheduleItem,
    pub asset: MediaAsset,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ScheduleError {
    Invalid(&'static str),
    TimeOverflow,
}

impl MediaAsset {
    pub fn validate(&self) -> Result<(), ScheduleError> {
        if !is_asset_id(&self.asset_id) {
            return Err(ScheduleError::Invalid(
                "asset_id must be a lowercase SHA-256 digest",
            ));
        }
        if self.media_path.trim().is_empty() || self.media_path.chars().any(char::is_control) {
            return Err(ScheduleError::Invalid("media_path must be a visible path"));
        }
        if self.duration_ms == 0 {
            return Err(ScheduleError::Invalid("asset duration must be positive"));
        }
        Ok(())
    }
}

impl ScheduleItem {
    pub fn validate(&self) -> Result<(), ScheduleError> {
        if !is_uuid(&self.item_id) || !is_uuid(&self.channel_id) {
            return Err(ScheduleError::Invalid(
                "item_id and channel_id must be canonical UUIDs",
            ));
        }
        if !is_asset_id(&self.asset_id) {
            return Err(ScheduleError::Invalid(
                "asset_id must be a lowercase SHA-256 digest",
            ));
        }
        if self.title.trim().is_empty()
            || self.title.len() > 240
            || self.title.chars().any(char::is_control)
        {
            return Err(ScheduleError::Invalid(
                "title must contain 1 to 240 visible characters",
            ));
        }
        if self.starts_at_unix_ms < 0 || self.duration_ms == 0 {
            return Err(ScheduleError::Invalid(
                "start time must be nonnegative and duration must be positive",
            ));
        }
        self.ends_at_unix_ms()?;
        Ok(())
    }

    pub fn ends_at_unix_ms(&self) -> Result<i64, ScheduleError> {
        let duration = i64::try_from(self.duration_ms).map_err(|_| ScheduleError::TimeOverflow)?;
        self.starts_at_unix_ms
            .checked_add(duration)
            .ok_or(ScheduleError::TimeOverflow)
    }
}

impl ChannelConfiguration {
    pub fn validate(&self) -> Result<(), ScheduleError> {
        if !is_uuid(&self.channel_id) {
            return Err(ScheduleError::Invalid(
                "channel_id must be a canonical UUID",
            ));
        }
        if self.display_name.trim().is_empty()
            || self.display_name.len() > 120
            || self.display_name.chars().any(char::is_control)
        {
            return Err(ScheduleError::Invalid(
                "display_name must contain 1 to 120 visible characters",
            ));
        }
        let profile = self.output_profile.profile();
        if profile.interlaced
            || profile.width == 0
            || profile.height == 0
            || profile.frame_rate_numerator == 0
            || profile.frame_rate_denominator == 0
            || profile.video_bitrate_bps == 0
            || profile.audio_bitrate_bps == 0
            || profile.transport_bitrate_bps
                <= u64::from(profile.video_bitrate_bps) + u64::from(profile.audio_bitrate_bps)
            || profile.audio_sample_rate_hz != 48_000
        {
            return Err(ScheduleError::Invalid(
                "output_profile does not define a runnable broadcast contract",
            ));
        }
        if self.output_sinks.is_empty() {
            return Err(ScheduleError::Invalid(
                "a channel must define at least one output sink",
            ));
        }
        let mut sink_ids = HashSet::new();
        let mut labels = HashSet::new();
        for sink in &self.output_sinks {
            if !is_uuid(&sink.sink_id) || !sink_ids.insert(sink.sink_id.as_str()) {
                return Err(ScheduleError::Invalid(
                    "output sink identities must be unique canonical UUIDs",
                ));
            }
            if sink.label.trim() != sink.label
                || sink.label.is_empty()
                || sink.label.len() > 120
                || sink.label.chars().any(char::is_control)
                || !labels.insert(sink.label.to_lowercase())
            {
                return Err(ScheduleError::Invalid(
                    "output sink labels must be unique, trimmed, and 1-120 visible characters",
                ));
            }
            match sink.kind {
                OutputSinkKind::UdpTs => {
                    let destination =
                        sink.destination
                            .parse::<std::net::SocketAddr>()
                            .map_err(|_| {
                                ScheduleError::Invalid(
                                    "UDP transport destinations must be an IP address and port",
                                )
                            })?;
                    if destination.port() == 0 {
                        return Err(ScheduleError::Invalid(
                            "UDP transport destination ports must be nonzero",
                        ));
                    }
                }
            }
            if !sink.loudness_target_lufs.is_finite()
                || !(-70.0..=-5.0).contains(&sink.loudness_target_lufs)
            {
                return Err(ScheduleError::Invalid(
                    "output sink loudness targets must be finite and between -70 and -5 LUFS",
                ));
            }
            if (sink.loudness_target_lufs - profile.loudness_target_lufs).abs() > f32::EPSILON {
                return Err(ScheduleError::Invalid(
                    "UDP transport sink loudness must match the selected encoded profile",
                ));
            }
        }
        if self.enabled && !self.output_sinks.iter().any(|sink| sink.enabled) {
            return Err(ScheduleError::Invalid(
                "an enabled channel must have at least one enabled output sink",
            ));
        }
        Ok(())
    }
}

pub fn prepare_commit(
    plan_id: impl Into<String>,
    proposed: &ScheduleItem,
    asset: Option<&MediaAsset>,
    channel_items: &[ScheduleItem],
) -> Result<CommitPlan, ScheduleError> {
    proposed.validate()?;
    let proposed_end = proposed.ends_at_unix_ms()?;
    let missing_media_detail = match asset {
        None => Some(format!("Asset {} does not exist.", proposed.asset_id)),
        Some(asset) if asset.validate().is_err() => Some("The asset record is invalid.".into()),
        Some(asset) if asset.asset_id != proposed.asset_id => {
            Some("The schedule item and asset identities do not match.".into())
        }
        Some(asset) if asset.readiness != AssetReadiness::Ready => Some(format!(
            "Asset {} is not ready for air ({:?}).",
            asset.asset_id, asset.readiness
        )),
        Some(asset) if asset.duration_ms < proposed.duration_ms => Some(format!(
            "Asset {} is shorter than the scheduled duration.",
            asset.asset_id
        )),
        Some(_) => None,
    };

    let mut conflicts = Vec::new();
    let mut prior: Option<(&ScheduleItem, i64)> = None;
    for existing in channel_items {
        if existing.channel_id != proposed.channel_id
            || existing.item_id == proposed.item_id
            || existing.state == ScheduleState::Cancelled
        {
            continue;
        }
        existing.validate()?;
        let existing_end = existing.ends_at_unix_ms()?;
        if proposed.starts_at_unix_ms < existing_end && existing.starts_at_unix_ms < proposed_end {
            let overlap_start = proposed.starts_at_unix_ms.max(existing.starts_at_unix_ms);
            let overlap_end = proposed_end.min(existing_end);
            conflicts.push(ScheduleConflict {
                existing_item_id: existing.item_id.clone(),
                overlap_ms: u64::try_from(overlap_end - overlap_start)
                    .map_err(|_| ScheduleError::TimeOverflow)?,
            });
        }
        if existing_end <= proposed.starts_at_unix_ms
            && prior.is_none_or(|(_, prior_end)| existing_end > prior_end)
        {
            prior = Some((existing, existing_end));
        }
    }
    conflicts.sort_by(|left, right| left.existing_item_id.cmp(&right.existing_item_id));
    let gaps = prior
        .and_then(|(item, end)| {
            let gap = proposed.starts_at_unix_ms - end;
            (gap > 1_000).then(|| ScheduleGap {
                prior_item_id: item.item_id.clone(),
                gap_ms: gap as u64,
            })
        })
        .into_iter()
        .collect();

    Ok(CommitPlan {
        plan_id: plan_id.into(),
        channel_id: proposed.channel_id.clone(),
        schedule_item_id: proposed.item_id.clone(),
        dry_run_passed: missing_media_detail.is_none() && conflicts.is_empty(),
        conflicts,
        missing_media_detail,
        gaps,
    })
}

fn is_asset_id(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn is_uuid(value: &str) -> bool {
    value.len() == 36
        && value.bytes().enumerate().all(|(index, byte)| match index {
            8 | 13 | 18 | 23 => byte == b'-',
            _ => byte.is_ascii_hexdigit(),
        })
}
