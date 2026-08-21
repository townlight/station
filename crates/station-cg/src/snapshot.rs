use super::{
    CgBoard, CgBulletin, CgBulletinContent, CgContentSource, CgFeedCacheEntry, CgFeedItem,
    CgFeedItemApproval, CgFeedSource, CgRect, CgTemplate, CgTemplateKind, CgZoneConfig, CgZoneKind,
};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CgPlaceholderSource {
    BulletinRotation,
    Emergency,
    Feed,
    Schedule,
    Image,
    LiveVideo,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CgPlaceholderReason {
    NoAirableBulletins,
    NoBulletinsMatchTags,
    NoActiveEmergency,
    MissingFeedSource,
    FeedSourceDisabled,
    FeedDataUnavailable,
    NoApprovedFeedItems,
    NoFeedItemsMatchTags,
    ScheduleDataUnavailable,
    ImageAssetReadinessUnknown,
    LiveSourceReadinessUnknown,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum CgResolvedContent {
    ManualText {
        text: String,
    },
    Bulletin {
        bulletin_id: String,
        content: CgBulletinContent,
    },
    Clock {
        format: String,
    },
    FeedItems {
        feed_source_id: String,
        items: Vec<CgFeedItem>,
        stale: bool,
        last_error: Option<String>,
    },
    Placeholder {
        source: CgPlaceholderSource,
        reason: CgPlaceholderReason,
    },
}

impl CgResolvedContent {
    pub fn validate(&self) -> Result<(), &'static str> {
        match self {
            Self::ManualText { text } => super::bounded_text(text, 2_000),
            Self::Bulletin {
                bulletin_id,
                content,
            } => {
                if !super::is_uuid(bulletin_id) {
                    return Err("resolved bulletin identity is invalid");
                }
                content.validate()
            }
            Self::Clock { format } => {
                if super::visible_text(format, 80)
                    && !format
                        .chars()
                        .any(|character| matches!(character, '\r' | '\n'))
                {
                    Ok(())
                } else {
                    Err("resolved clock format is invalid")
                }
            }
            Self::FeedItems {
                feed_source_id,
                items,
                last_error,
                ..
            } => {
                if !super::is_uuid(feed_source_id)
                    || last_error
                        .as_deref()
                        .is_some_and(|value| super::bounded_text(value, 1_000).is_err())
                {
                    return Err("resolved feed fields are invalid");
                }
                let mut item_ids = BTreeSet::new();
                for item in items {
                    item.validate()?;
                    if !item_ids.insert(item.item_id.as_str()) {
                        return Err("resolved feed item identities must be unique");
                    }
                }
                Ok(())
            }
            Self::Placeholder { source, reason } => {
                let valid = match source {
                    CgPlaceholderSource::BulletinRotation => matches!(
                        reason,
                        CgPlaceholderReason::NoAirableBulletins
                            | CgPlaceholderReason::NoBulletinsMatchTags
                    ),
                    CgPlaceholderSource::Emergency => {
                        matches!(reason, CgPlaceholderReason::NoActiveEmergency)
                    }
                    CgPlaceholderSource::Feed => matches!(
                        reason,
                        CgPlaceholderReason::MissingFeedSource
                            | CgPlaceholderReason::FeedSourceDisabled
                            | CgPlaceholderReason::FeedDataUnavailable
                            | CgPlaceholderReason::NoApprovedFeedItems
                            | CgPlaceholderReason::NoFeedItemsMatchTags
                    ),
                    CgPlaceholderSource::Schedule => {
                        matches!(reason, CgPlaceholderReason::ScheduleDataUnavailable)
                    }
                    CgPlaceholderSource::Image => {
                        matches!(reason, CgPlaceholderReason::ImageAssetReadinessUnknown)
                    }
                    CgPlaceholderSource::LiveVideo => {
                        matches!(reason, CgPlaceholderReason::LiveSourceReadinessUnknown)
                    }
                };
                valid
                    .then_some(())
                    .ok_or("placeholder source and reason do not match")
            }
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CgResolvedZone {
    pub zone_id: String,
    pub template_zone_key: String,
    pub zone_kind: CgZoneKind,
    pub bounds: CgRect,
    pub z_index: u16,
    pub content_source: CgContentSource,
    pub content: CgResolvedContent,
    pub revision: u64,
}

impl CgResolvedZone {
    pub fn validate(&self) -> Result<(), &'static str> {
        if !super::is_uuid(&self.zone_id)
            || !super::is_slug(&self.template_zone_key, 60)
            || self.revision == 0
        {
            return Err("resolved CG zone identity is invalid");
        }
        self.bounds.validate()?;
        self.content_source.validate()?;
        self.content.validate()?;
        let source_matches = matches!(
            (&self.content_source, &self.content),
            (
                CgContentSource::Manual { .. },
                CgResolvedContent::ManualText { .. }
            ) | (
                CgContentSource::BulletinRotation,
                CgResolvedContent::Bulletin { .. }
            ) | (
                CgContentSource::BulletinRotation,
                CgResolvedContent::Placeholder {
                    source: CgPlaceholderSource::BulletinRotation,
                    ..
                }
            ) | (
                CgContentSource::Emergency,
                CgResolvedContent::Placeholder {
                    source: CgPlaceholderSource::Emergency,
                    ..
                }
            ) | (
                CgContentSource::Schedule { .. },
                CgResolvedContent::Placeholder {
                    source: CgPlaceholderSource::Schedule,
                    ..
                }
            ) | (
                CgContentSource::Feed { .. },
                CgResolvedContent::FeedItems { .. }
            ) | (
                CgContentSource::Feed { .. },
                CgResolvedContent::Placeholder {
                    source: CgPlaceholderSource::Feed,
                    ..
                }
            ) | (
                CgContentSource::Image { .. },
                CgResolvedContent::Placeholder {
                    source: CgPlaceholderSource::Image,
                    ..
                }
            ) | (
                CgContentSource::Clock { .. },
                CgResolvedContent::Clock { .. }
            ) | (
                CgContentSource::LiveVideo { .. },
                CgResolvedContent::Placeholder {
                    source: CgPlaceholderSource::LiveVideo,
                    ..
                }
            )
        );
        source_matches
            .then_some(())
            .ok_or("resolved content does not match its source")
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CgResolvedSnapshot {
    pub board_id: String,
    pub channel_id: String,
    pub template_id: String,
    pub template_kind: CgTemplateKind,
    pub board_revision: u64,
    pub generated_at_unix_ms: i64,
    pub zones: Vec<CgResolvedZone>,
}

impl CgResolvedSnapshot {
    pub fn validate(&self) -> Result<(), &'static str> {
        if !super::is_uuid(&self.board_id)
            || !super::is_uuid(&self.channel_id)
            || !super::is_slug(&self.template_id, 80)
            || self.board_revision == 0
            || self.generated_at_unix_ms < 0
            || self.zones.is_empty()
        {
            return Err("resolved CG snapshot fields are invalid");
        }
        let mut zone_ids = BTreeSet::new();
        let mut template_zone_keys = BTreeSet::new();
        for zone in &self.zones {
            zone.validate()?;
            if !zone_ids.insert(zone.zone_id.as_str()) {
                return Err("resolved CG zone identities must be unique");
            }
            if !template_zone_keys.insert(zone.template_zone_key.as_str()) {
                return Err("resolved CG template-zone identities must be unique");
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum CgSnapshotError {
    InvalidTimestamp,
    BoardInvalid {
        reason: String,
    },
    BoardInactive,
    TemplateInvalid {
        reason: String,
    },
    TemplateMismatch,
    NoZones,
    SnapshotInvalid {
        reason: String,
    },
    InvalidZone {
        zone_id: String,
        reason: String,
    },
    DuplicateZone {
        zone_id: String,
    },
    DuplicateTemplateZone {
        template_zone_key: String,
    },
    ZoneNotInTemplate {
        zone_id: String,
        template_zone_key: String,
    },
    ZoneKindMismatch {
        zone_id: String,
    },
    InvalidFeed {
        feed_source_id: String,
        reason: String,
    },
    InvalidFeedCache {
        feed_source_id: String,
        reason: String,
    },
    InvalidFeedApproval {
        feed_source_id: String,
        reason: String,
    },
    DuplicateFeed {
        feed_source_id: String,
    },
    FeedChannelMismatch {
        feed_source_id: String,
    },
    InvalidBulletin {
        bulletin_id: String,
        reason: String,
    },
    DuplicateBulletin {
        bulletin_id: String,
    },
    BulletinChannelMismatch {
        bulletin_id: String,
    },
}

/// Resolve an immutable, deterministic CG render contract.
///
/// Feed, schedule, image, live-source, and emergency values are deliberately not
/// fabricated here. When their external/runtime data is unavailable, the snapshot
/// contains a typed placeholder and an explicit reason for S15/operator surfaces.
pub fn resolve_snapshot(
    board: &CgBoard,
    template: &CgTemplate,
    zones: &[CgZoneConfig],
    feed_sources: &[CgFeedSource],
    bulletins: &[CgBulletin],
    now_unix_ms: i64,
) -> Result<CgResolvedSnapshot, CgSnapshotError> {
    resolve_snapshot_with_feed_cache(
        board,
        template,
        zones,
        feed_sources,
        bulletins,
        &[],
        &[],
        now_unix_ms,
    )
}

#[allow(clippy::too_many_arguments)]
pub fn resolve_snapshot_with_feed_cache(
    board: &CgBoard,
    template: &CgTemplate,
    zones: &[CgZoneConfig],
    feed_sources: &[CgFeedSource],
    bulletins: &[CgBulletin],
    feed_cache: &[CgFeedCacheEntry],
    approvals: &[CgFeedItemApproval],
    now_unix_ms: i64,
) -> Result<CgResolvedSnapshot, CgSnapshotError> {
    if now_unix_ms < 0 {
        return Err(CgSnapshotError::InvalidTimestamp);
    }
    board
        .validate()
        .map_err(|reason| CgSnapshotError::BoardInvalid {
            reason: reason.into(),
        })?;
    if !board.active {
        return Err(CgSnapshotError::BoardInactive);
    }
    template
        .validate()
        .map_err(|reason| CgSnapshotError::TemplateInvalid {
            reason: reason.into(),
        })?;
    if board.template_id != template.template_id {
        return Err(CgSnapshotError::TemplateMismatch);
    }
    if zones.is_empty() {
        return Err(CgSnapshotError::NoZones);
    }

    let template_zones: BTreeMap<&str, &_> = template
        .zones
        .iter()
        .map(|template_zone| (template_zone.template_zone_key.as_str(), template_zone))
        .collect();
    let mut seen_zone_ids = BTreeSet::new();
    let mut seen_template_zone_keys = BTreeSet::new();
    let mut ordered_zones = Vec::with_capacity(zones.len());
    for zone in zones {
        zone.validate()
            .map_err(|reason| CgSnapshotError::InvalidZone {
                zone_id: zone.zone_id.clone(),
                reason: reason.into(),
            })?;
        if zone.board_id != board.board_id {
            return Err(CgSnapshotError::InvalidZone {
                zone_id: zone.zone_id.clone(),
                reason: "zone belongs to a different board".into(),
            });
        }
        if !seen_zone_ids.insert(zone.zone_id.as_str()) {
            return Err(CgSnapshotError::DuplicateZone {
                zone_id: zone.zone_id.clone(),
            });
        }
        if !seen_template_zone_keys.insert(zone.template_zone_key.as_str()) {
            return Err(CgSnapshotError::DuplicateTemplateZone {
                template_zone_key: zone.template_zone_key.clone(),
            });
        }
        let Some(template_zone) = template_zones.get(zone.template_zone_key.as_str()) else {
            return Err(CgSnapshotError::ZoneNotInTemplate {
                zone_id: zone.zone_id.clone(),
                template_zone_key: zone.template_zone_key.clone(),
            });
        };
        if template_zone.zone_kind != zone.zone_kind {
            return Err(CgSnapshotError::ZoneKindMismatch {
                zone_id: zone.zone_id.clone(),
            });
        }
        ordered_zones.push((zone, *template_zone));
    }
    ordered_zones.sort_by(|(left, left_template), (right, right_template)| {
        left_template
            .z_index
            .cmp(&right_template.z_index)
            .then_with(|| left.template_zone_key.cmp(&right.template_zone_key))
            .then_with(|| left.zone_id.cmp(&right.zone_id))
    });

    let mut feed_by_id = BTreeMap::new();
    for feed in feed_sources {
        feed.validate()
            .map_err(|reason| CgSnapshotError::InvalidFeed {
                feed_source_id: feed.feed_source_id.clone(),
                reason: reason.into(),
            })?;
        if !feed_by_id
            .insert(feed.feed_source_id.as_str(), feed)
            .is_none()
        {
            return Err(CgSnapshotError::DuplicateFeed {
                feed_source_id: feed.feed_source_id.clone(),
            });
        }
        if feed.channel_id != board.channel_id {
            return Err(CgSnapshotError::FeedChannelMismatch {
                feed_source_id: feed.feed_source_id.clone(),
            });
        }
    }

    let mut valid_bulletins = BTreeMap::new();
    for bulletin in bulletins {
        bulletin
            .validate()
            .map_err(|reason| CgSnapshotError::InvalidBulletin {
                bulletin_id: bulletin.bulletin_id.clone(),
                reason: reason.into(),
            })?;
        if bulletin.channel_id != board.channel_id {
            return Err(CgSnapshotError::BulletinChannelMismatch {
                bulletin_id: bulletin.bulletin_id.clone(),
            });
        }
        if valid_bulletins
            .insert(bulletin.bulletin_id.as_str(), bulletin)
            .is_some()
        {
            return Err(CgSnapshotError::DuplicateBulletin {
                bulletin_id: bulletin.bulletin_id.clone(),
            });
        }
    }

    let mut cache_by_id = BTreeMap::new();
    for cache in feed_cache {
        cache
            .validate()
            .map_err(|reason| CgSnapshotError::InvalidFeedCache {
                feed_source_id: cache.feed_source_id.clone(),
                reason: reason.into(),
            })?;
        if !cache_by_id
            .insert(cache.feed_source_id.as_str(), cache)
            .is_none()
        {
            return Err(CgSnapshotError::InvalidFeedCache {
                feed_source_id: cache.feed_source_id.clone(),
                reason: "duplicate feed-cache source".into(),
            });
        }
        if !feed_by_id.contains_key(cache.feed_source_id.as_str()) {
            return Err(CgSnapshotError::InvalidFeedCache {
                feed_source_id: cache.feed_source_id.clone(),
                reason: "feed-cache source is not registered on this board".into(),
            });
        }
    }
    let mut approval_ids = BTreeSet::new();
    for approval in approvals {
        approval
            .validate()
            .map_err(|reason| CgSnapshotError::InvalidFeedApproval {
                feed_source_id: approval.feed_source_id.clone(),
                reason: reason.into(),
            })?;
        let Some(feed) = feed_by_id.get(approval.feed_source_id.as_str()) else {
            return Err(CgSnapshotError::InvalidFeedApproval {
                feed_source_id: approval.feed_source_id.clone(),
                reason: "approval source is not registered on this board".into(),
            });
        };
        if feed.channel_id != board.channel_id {
            return Err(CgSnapshotError::InvalidFeedApproval {
                feed_source_id: approval.feed_source_id.clone(),
                reason: "approval source belongs to another channel".into(),
            });
        }
        if !approval_ids.insert((approval.feed_source_id.as_str(), approval.item_id.as_str())) {
            return Err(CgSnapshotError::InvalidFeedApproval {
                feed_source_id: approval.feed_source_id.clone(),
                reason: "duplicate feed-item approval".into(),
            });
        }
    }

    let resolved_zones = ordered_zones
        .into_iter()
        .map(|(zone, template_zone)| CgResolvedZone {
            zone_id: zone.zone_id.clone(),
            template_zone_key: zone.template_zone_key.clone(),
            zone_kind: zone.zone_kind,
            bounds: template_zone.bounds,
            z_index: template_zone.z_index,
            content_source: zone.content_source.clone(),
            content: resolve_zone_content(
                zone,
                &feed_by_id,
                &valid_bulletins,
                &cache_by_id,
                &approval_ids,
                now_unix_ms,
            ),
            revision: zone.revision,
        })
        .collect();

    let snapshot = CgResolvedSnapshot {
        board_id: board.board_id.clone(),
        channel_id: board.channel_id.clone(),
        template_id: template.template_id.clone(),
        template_kind: template.kind,
        board_revision: board.revision,
        generated_at_unix_ms: now_unix_ms,
        zones: resolved_zones,
    };
    snapshot
        .validate()
        .map_err(|reason| CgSnapshotError::SnapshotInvalid {
            reason: reason.into(),
        })?;
    Ok(snapshot)
}

fn resolve_zone_content(
    zone: &CgZoneConfig,
    feed_by_id: &BTreeMap<&str, &CgFeedSource>,
    bulletins: &BTreeMap<&str, &CgBulletin>,
    feed_cache: &BTreeMap<&str, &CgFeedCacheEntry>,
    approvals: &BTreeSet<(&str, &str)>,
    now_unix_ms: i64,
) -> CgResolvedContent {
    match &zone.content_source {
        CgContentSource::Manual { text } => CgResolvedContent::ManualText { text: text.clone() },
        CgContentSource::Clock { format } => CgResolvedContent::Clock {
            format: format.clone(),
        },
        CgContentSource::BulletinRotation => {
            let airable: Vec<&CgBulletin> = bulletins
                .values()
                .copied()
                .filter(|bulletin| bulletin.is_airable_at(now_unix_ms))
                .collect();
            let tagged: Vec<&CgBulletin> = airable
                .iter()
                .copied()
                .filter(|bulletin| {
                    zone.allowed_tags.is_empty()
                        || bulletin
                            .tags
                            .iter()
                            .any(|tag| zone.allowed_tags.iter().any(|allowed| allowed == tag))
                })
                .collect();
            if tagged.is_empty() {
                let reason = if airable.is_empty() {
                    CgPlaceholderReason::NoAirableBulletins
                } else {
                    CgPlaceholderReason::NoBulletinsMatchTags
                };
                return CgResolvedContent::Placeholder {
                    source: CgPlaceholderSource::BulletinRotation,
                    reason,
                };
            }
            let slot_seconds = u64::from(zone.rotation_seconds.unwrap_or_else(|| {
                tagged
                    .first()
                    .map_or(3, |bulletin| bulletin.display_seconds)
            }));
            let slot = u128::from(slot_seconds) * 1_000;
            let index = ((now_unix_ms as u128) / slot % tagged.len() as u128) as usize;
            let bulletin = tagged[index];
            CgResolvedContent::Bulletin {
                bulletin_id: bulletin.bulletin_id.clone(),
                content: bulletin.content.clone(),
            }
        }
        CgContentSource::Emergency => CgResolvedContent::Placeholder {
            source: CgPlaceholderSource::Emergency,
            reason: CgPlaceholderReason::NoActiveEmergency,
        },
        CgContentSource::Schedule { .. } => CgResolvedContent::Placeholder {
            source: CgPlaceholderSource::Schedule,
            reason: CgPlaceholderReason::ScheduleDataUnavailable,
        },
        CgContentSource::Feed { feed_source_id } => {
            let Some(feed) = feed_by_id.get(feed_source_id.as_str()) else {
                return CgResolvedContent::Placeholder {
                    source: CgPlaceholderSource::Feed,
                    reason: CgPlaceholderReason::MissingFeedSource,
                };
            };
            if !feed.enabled {
                return CgResolvedContent::Placeholder {
                    source: CgPlaceholderSource::Feed,
                    reason: CgPlaceholderReason::FeedSourceDisabled,
                };
            }
            let Some(cache) = feed_cache.get(feed_source_id.as_str()) else {
                return CgResolvedContent::Placeholder {
                    source: CgPlaceholderSource::Feed,
                    reason: CgPlaceholderReason::FeedDataUnavailable,
                };
            };
            let mut items: Vec<CgFeedItem> = cache.items.clone();
            if zone.approval_required {
                items.retain(|item| {
                    approvals.contains(&(feed_source_id.as_str(), item.item_id.as_str()))
                });
                if items.is_empty() {
                    return CgResolvedContent::Placeholder {
                        source: CgPlaceholderSource::Feed,
                        reason: CgPlaceholderReason::NoApprovedFeedItems,
                    };
                }
            }
            if !zone.allowed_tags.is_empty() {
                items.retain(|item| {
                    item.tags
                        .iter()
                        .any(|tag| zone.allowed_tags.iter().any(|allowed| allowed == tag))
                });
                if items.is_empty() {
                    return CgResolvedContent::Placeholder {
                        source: CgPlaceholderSource::Feed,
                        reason: CgPlaceholderReason::NoFeedItemsMatchTags,
                    };
                }
            }
            items.sort_by(|left, right| {
                left.starts_at_unix_ms
                    .cmp(&right.starts_at_unix_ms)
                    .then_with(|| left.item_id.cmp(&right.item_id))
            });
            CgResolvedContent::FeedItems {
                feed_source_id: feed_source_id.clone(),
                items,
                stale: cache.is_stale_at(now_unix_ms),
                last_error: cache.last_error.clone(),
            }
        }
        CgContentSource::Image { .. } => CgResolvedContent::Placeholder {
            source: CgPlaceholderSource::Image,
            reason: CgPlaceholderReason::ImageAssetReadinessUnknown,
        },
        CgContentSource::LiveVideo { .. } => CgResolvedContent::Placeholder {
            source: CgPlaceholderSource::LiveVideo,
            reason: CgPlaceholderReason::LiveSourceReadinessUnknown,
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{CgBulletinState, CgTemplateKind};

    const CHANNEL: &str = "5d85189a-cd1f-43dc-a619-66a2aeb09ac4";
    const BOARD: &str = "94a0dc01-d314-4b55-b2e4-cba39cc3f1dc";

    fn board(template_id: &str) -> CgBoard {
        CgBoard {
            board_id: BOARD.into(),
            channel_id: CHANNEL.into(),
            template_id: template_id.into(),
            active: true,
            background_audio_asset_id: None,
            revision: 3,
            created_at_unix_ms: 1,
            updated_at_unix_ms: 2,
            created_by: "operator".into(),
        }
    }

    fn zone(id: &str, key: &str, kind: CgZoneKind, source: CgContentSource) -> CgZoneConfig {
        CgZoneConfig {
            zone_id: id.into(),
            board_id: BOARD.into(),
            template_zone_key: key.into(),
            zone_kind: kind,
            content_source: source,
            rotation_seconds: None,
            approval_required: false,
            allowed_tags: Vec::new(),
            revision: 1,
            created_at_unix_ms: 1,
            updated_at_unix_ms: 1,
        }
    }

    fn bulletin(id: &str, state: CgBulletinState, tags: &[&str]) -> CgBulletin {
        CgBulletin {
            bulletin_id: id.into(),
            channel_id: CHANNEL.into(),
            content: CgBulletinContent::Text {
                title: format!("Title {id}"),
                body: format!("Body {id}"),
            },
            state,
            tags: tags.iter().map(|tag| (*tag).into()).collect(),
            requested_start_at_unix_ms: Some(1_000),
            requested_end_at_unix_ms: Some(20_000),
            display_seconds: 3,
            audio_asset_id: None,
            submitted_by: "submitter".into(),
            moderation_notes: (state != CgBulletinState::Submitted).then(|| "approved".into()),
            moderated_by: (state != CgBulletinState::Submitted).then(|| "moderator".into()),
            revision: 1,
            created_at_unix_ms: 1,
            updated_at_unix_ms: 1_000,
        }
    }

    fn feed(enabled: bool) -> CgFeedSource {
        CgFeedSource {
            feed_source_id: "796ca80a-9cc2-4fc1-84a2-2a88d8f92f24".into(),
            channel_id: CHANNEL.into(),
            kind: crate::CgFeedKind::Rss,
            label: "Notices".into(),
            source_url: "https://notices.example.test/rss".into(),
            trust_tier: crate::CgFeedTrustTier::Moderated,
            tags: Vec::new(),
            refresh_seconds: 300,
            enabled,
            revision: 1,
            created_at_unix_ms: 1,
            updated_at_unix_ms: 1,
            created_by: "operator".into(),
            last_fetched_at_unix_ms: None,
            last_fetch_error: None,
        }
    }

    fn feed_item(id: char, starts_at_unix_ms: i64, tags: &[&str]) -> CgFeedItem {
        CgFeedItem {
            item_id: id.to_string().repeat(64),
            title: format!("Notice {id}"),
            summary: None,
            starts_at_unix_ms: Some(starts_at_unix_ms),
            url: None,
            tags: tags.iter().map(|tag| (*tag).into()).collect(),
        }
    }

    #[test]
    fn resolution_is_deterministic_and_orders_zones_by_template_depth() {
        let template = crate::built_in_template("l-bar").unwrap();
        let zones = vec![
            zone(
                "8f4f0f70-5c30-48c6-819a-fb22b34353c5",
                "ticker",
                CgZoneKind::Ticker,
                CgContentSource::Clock {
                    format: "%H:%M".into(),
                },
            ),
            zone(
                "796ca80a-9cc2-4fc1-84a2-2a88d8f92f24",
                "primary",
                CgZoneKind::Primary,
                CgContentSource::Manual {
                    text: "Town meeting tonight".into(),
                },
            ),
        ];
        let first = resolve_snapshot(&board("l-bar"), &template, &zones, &[], &[], 5_000).unwrap();
        let second = resolve_snapshot(&board("l-bar"), &template, &zones, &[], &[], 5_000).unwrap();
        assert_eq!(first, second);
        assert!(first.validate().is_ok());
        assert_eq!(first.template_id, "l-bar");
        assert_eq!(first.zones[0].template_zone_key, "primary");
        assert_eq!(first.zones[1].template_zone_key, "ticker");
    }

    #[test]
    fn feed_resolution_enforces_approval_tags_staleness_and_ordering() {
        let template = crate::built_in_template("l-bar").unwrap();
        let mut ticker = zone(
            "8f4f0f70-5c30-48c6-819a-fb22b34353c5",
            "ticker",
            CgZoneKind::Ticker,
            CgContentSource::Feed {
                feed_source_id: "796ca80a-9cc2-4fc1-84a2-2a88d8f92f24".into(),
            },
        );
        ticker.approval_required = true;
        ticker.allowed_tags = vec!["community".into()];
        let items = vec![
            feed_item('b', 2_000, &["community"]),
            feed_item('a', 2_000, &["community"]),
            feed_item('c', 1_000, &["sports"]),
        ];
        let cache = CgFeedCacheEntry {
            feed_source_id: "796ca80a-9cc2-4fc1-84a2-2a88d8f92f24".into(),
            fetched_at_unix_ms: 1_000,
            expires_at_unix_ms: 1_500,
            etag: None,
            last_modified: None,
            last_error: Some("timeout".into()),
            items,
        };
        let empty = resolve_snapshot_with_feed_cache(
            &board("l-bar"),
            &template,
            &[ticker.clone()],
            &[feed(true)],
            &[],
            std::slice::from_ref(&cache),
            &[],
            2_000,
        )
        .unwrap();
        assert!(matches!(
            empty.zones[0].content,
            CgResolvedContent::Placeholder {
                reason: CgPlaceholderReason::NoApprovedFeedItems,
                ..
            }
        ));
        let approvals = vec![
            CgFeedItemApproval {
                feed_source_id: "796ca80a-9cc2-4fc1-84a2-2a88d8f92f24".into(),
                item_id: "b".repeat(64),
                approved_by: "operator".into(),
                approved_at_unix_ms: 1_100,
            },
            CgFeedItemApproval {
                feed_source_id: "796ca80a-9cc2-4fc1-84a2-2a88d8f92f24".into(),
                item_id: "a".repeat(64),
                approved_by: "operator".into(),
                approved_at_unix_ms: 1_100,
            },
        ];
        let resolved = resolve_snapshot_with_feed_cache(
            &board("l-bar"),
            &template,
            &[ticker],
            &[feed(true)],
            &[],
            std::slice::from_ref(&cache),
            &approvals,
            2_000,
        )
        .unwrap();
        let CgResolvedContent::FeedItems {
            items,
            stale,
            last_error,
            ..
        } = &resolved.zones[0].content
        else {
            panic!("expected feed items");
        };
        assert_eq!(
            items
                .iter()
                .map(|item| item.item_id.as_str())
                .collect::<Vec<_>>(),
            vec![
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            ]
        );
        assert!(*stale);
        assert_eq!(last_error.as_deref(), Some("timeout"));

        let disabled = resolve_snapshot_with_feed_cache(
            &board("l-bar"),
            &template,
            &[zone(
                "8f4f0f70-5c30-48c6-819a-fb22b34353c5",
                "ticker",
                CgZoneKind::Ticker,
                CgContentSource::Feed {
                    feed_source_id: "796ca80a-9cc2-4fc1-84a2-2a88d8f92f24".into(),
                },
            )],
            &[feed(false)],
            &[],
            std::slice::from_ref(&cache),
            &[],
            1_000,
        )
        .unwrap();
        assert!(matches!(
            disabled.zones[0].content,
            CgResolvedContent::Placeholder {
                reason: CgPlaceholderReason::FeedSourceDisabled,
                ..
            }
        ));
    }

    #[test]
    fn rejects_unknown_approval_sources_and_cache_header_controls() {
        let template = crate::built_in_template("l-bar").unwrap();
        let approval = CgFeedItemApproval {
            feed_source_id: "9ac46d05-09d8-4ef8-9607-75c365b3274e".into(),
            item_id: "a".repeat(64),
            approved_by: "operator".into(),
            approved_at_unix_ms: 1,
        };
        let zone = zone(
            "8f4f0f70-5c30-48c6-819a-fb22b34353c5",
            "ticker",
            CgZoneKind::Ticker,
            CgContentSource::Feed {
                feed_source_id: "796ca80a-9cc2-4fc1-84a2-2a88d8f92f24".into(),
            },
        );
        assert!(matches!(
            resolve_snapshot_with_feed_cache(
                &board("l-bar"),
                &template,
                &[zone],
                &[feed(true)],
                &[],
                &[],
                &[approval],
                1,
            ),
            Err(CgSnapshotError::InvalidFeedApproval { .. })
        ));
        let mut cache = CgFeedCacheEntry {
            feed_source_id: "796ca80a-9cc2-4fc1-84a2-2a88d8f92f24".into(),
            fetched_at_unix_ms: 1,
            expires_at_unix_ms: 2,
            etag: Some("bad\r\nheader".into()),
            last_modified: None,
            last_error: None,
            items: Vec::new(),
        };
        assert!(cache.validate().is_err());
        cache.etag = None;
        cache.last_modified = Some("bad\nheader".into());
        assert!(cache.validate().is_err());
    }

    #[test]
    fn rotation_filters_moderation_time_and_tags_before_selecting() {
        let template = crate::built_in_template("fullscreen").unwrap();
        let mut rotation_zone = zone(
            "8f4f0f70-5c30-48c6-819a-fb22b34353c5",
            "primary",
            CgZoneKind::Primary,
            CgContentSource::BulletinRotation,
        );
        rotation_zone.allowed_tags = vec!["community".into()];
        let bulletins = vec![
            bulletin(
                "00000000-0000-0000-0000-000000000001",
                CgBulletinState::Submitted,
                &["community"],
            ),
            bulletin(
                "00000000-0000-0000-0000-000000000002",
                CgBulletinState::Scheduled,
                &["community"],
            ),
            bulletin(
                "00000000-0000-0000-0000-000000000003",
                CgBulletinState::Scheduled,
                &["sports"],
            ),
        ];
        let snapshot = resolve_snapshot(
            &board("fullscreen"),
            &template,
            &[rotation_zone],
            &[],
            &bulletins,
            2_000,
        )
        .unwrap();
        assert!(matches!(
            snapshot.zones[0].content,
            CgResolvedContent::Bulletin { ref bulletin_id, .. }
                if bulletin_id == "00000000-0000-0000-0000-000000000002"
        ));
        let mut no_match_zone = zone(
            "8f4f0f70-5c30-48c6-819a-fb22b34353c5",
            "primary",
            CgZoneKind::Primary,
            CgContentSource::BulletinRotation,
        );
        no_match_zone.allowed_tags = vec!["community".into()];
        let no_match = resolve_snapshot(
            &board("fullscreen"),
            &template,
            &[no_match_zone],
            &[],
            &[bulletin(
                "00000000-0000-0000-0000-000000000002",
                CgBulletinState::Scheduled,
                &["sports"],
            )],
            2_000,
        )
        .unwrap();
        assert!(matches!(
            no_match.zones[0].content,
            CgResolvedContent::Placeholder {
                reason: CgPlaceholderReason::NoBulletinsMatchTags,
                ..
            }
        ));
    }

    #[test]
    fn unresolved_external_sources_are_explicit_and_missing_dependencies_are_safe() {
        let template = crate::built_in_template("l-bar").unwrap();
        let zones = vec![
            zone(
                "8f4f0f70-5c30-48c6-819a-fb22b34353c5",
                "primary",
                CgZoneKind::Primary,
                CgContentSource::Feed {
                    feed_source_id: "796ca80a-9cc2-4fc1-84a2-2a88d8f92f24".into(),
                },
            ),
            zone(
                "94a0dc01-d314-4b55-b2e4-cba39cc3f1dc",
                "sidebar",
                CgZoneKind::Sidebar,
                CgContentSource::Image {
                    asset_id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                        .into(),
                },
            ),
        ];
        let missing =
            resolve_snapshot(&board("l-bar"), &template, &zones, &[], &[], 1_000).unwrap();
        assert!(matches!(
            missing.zones[0].content,
            CgResolvedContent::Placeholder {
                reason: CgPlaceholderReason::MissingFeedSource,
                ..
            }
        ));
        assert!(matches!(
            missing.zones[1].content,
            CgResolvedContent::Placeholder {
                reason: CgPlaceholderReason::ImageAssetReadinessUnknown,
                ..
            }
        ));
    }

    #[test]
    fn invalid_zone_dependencies_fail_without_partial_snapshot() {
        let template = crate::built_in_template("fullscreen").unwrap();
        let invalid = zone(
            "8f4f0f70-5c30-48c6-819a-fb22b34353c5",
            "not-in-template",
            CgZoneKind::Primary,
            CgContentSource::Manual {
                text: "text".into(),
            },
        );
        assert!(matches!(
            resolve_snapshot(&board("fullscreen"), &template, &[invalid], &[], &[], 1_000),
            Err(CgSnapshotError::ZoneNotInTemplate { .. })
        ));
    }

    #[test]
    fn snapshot_retains_template_kind_for_renderer_contract() {
        let template = crate::built_in_template("emergency-overlay").unwrap();
        let zone = zone(
            "8f4f0f70-5c30-48c6-819a-fb22b34353c5",
            "alert",
            CgZoneKind::Alert,
            CgContentSource::Emergency,
        );
        let snapshot = resolve_snapshot(
            &board("emergency-overlay"),
            &template,
            &[zone],
            &[],
            &[],
            1_000,
        )
        .unwrap();
        assert_eq!(snapshot.zones[0].zone_kind, CgZoneKind::Alert);
        assert!(matches!(
            snapshot.zones[0].content,
            CgResolvedContent::Placeholder {
                source: CgPlaceholderSource::Emergency,
                reason: CgPlaceholderReason::NoActiveEmergency
            }
        ));
        assert_eq!(template.kind, CgTemplateKind::EmergencyOverlay);
    }
}
