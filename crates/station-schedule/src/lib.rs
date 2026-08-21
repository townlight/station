use serde::{Deserialize, Serialize};

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

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChannelConfiguration {
    pub channel_id: String,
    pub display_name: String,
    pub udp_destination: String,
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
        let destination = self
            .udp_destination
            .parse::<std::net::SocketAddr>()
            .map_err(|_| {
                ScheduleError::Invalid("udp_destination must be an IP address and port")
            })?;
        if destination.port() == 0 {
            return Err(ScheduleError::Invalid(
                "udp_destination port must be nonzero",
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
