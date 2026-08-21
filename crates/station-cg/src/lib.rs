use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

mod snapshot;

pub use snapshot::{
    CgPlaceholderReason, CgPlaceholderSource, CgResolvedContent, CgResolvedSnapshot,
    CgResolvedZone, CgSnapshotError, resolve_snapshot,
};

pub const CG_LAYOUT_SCALE: u16 = 1_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum CgTemplateKind {
    Fullscreen,
    LBar,
    LowerThird,
    Bug,
    Ticker,
    EmergencyOverlay,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CgZoneKind {
    Primary,
    Sidebar,
    LowerThird,
    Ticker,
    Schedule,
    Sponsor,
    Logo,
    Alert,
    Clock,
    LiveVideo,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct CgRect {
    pub x: u16,
    pub y: u16,
    pub width: u16,
    pub height: u16,
}

impl CgRect {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.width == 0
            || self.height == 0
            || u32::from(self.x) + u32::from(self.width) > u32::from(CG_LAYOUT_SCALE)
            || u32::from(self.y) + u32::from(self.height) > u32::from(CG_LAYOUT_SCALE)
        {
            return Err("CG zone bounds must be nonempty and remain inside the 1000-unit canvas.");
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CgTemplateZone {
    pub template_zone_key: String,
    pub zone_kind: CgZoneKind,
    pub bounds: CgRect,
    pub z_index: u16,
}

impl CgTemplateZone {
    pub fn validate(&self) -> Result<(), &'static str> {
        if !is_slug(&self.template_zone_key, 60) || self.z_index > 100 {
            return Err("CG template-zone identity or z-index is invalid.");
        }
        self.bounds.validate()
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CgTemplate {
    pub template_id: String,
    pub display_name: String,
    pub kind: CgTemplateKind,
    pub zones: Vec<CgTemplateZone>,
}

impl CgTemplate {
    pub fn validate(&self) -> Result<(), &'static str> {
        if !is_slug(&self.template_id, 80)
            || !visible_text(&self.display_name, 120)
            || self.zones.is_empty()
            || self.zones.len() > 16
        {
            return Err("CG template fields are invalid.");
        }
        let mut keys = BTreeSet::new();
        for zone in &self.zones {
            zone.validate()?;
            if !keys.insert(zone.template_zone_key.as_str()) {
                return Err("CG template zone keys must be unique.");
            }
        }
        Ok(())
    }
}

pub fn built_in_templates() -> Vec<CgTemplate> {
    let template = |template_id: &str,
                    display_name: &str,
                    kind: CgTemplateKind,
                    zones: Vec<CgTemplateZone>| CgTemplate {
        template_id: template_id.into(),
        display_name: display_name.into(),
        kind,
        zones,
    };
    let zone = |key: &str,
                zone_kind: CgZoneKind,
                x: u16,
                y: u16,
                width: u16,
                height: u16,
                z_index: u16| CgTemplateZone {
        template_zone_key: key.into(),
        zone_kind,
        bounds: CgRect {
            x,
            y,
            width,
            height,
        },
        z_index,
    };

    vec![
        template(
            "fullscreen",
            "Fullscreen bulletin",
            CgTemplateKind::Fullscreen,
            vec![zone("primary", CgZoneKind::Primary, 0, 0, 1_000, 1_000, 0)],
        ),
        template(
            "l-bar",
            "L-bar with sidebar and ticker",
            CgTemplateKind::LBar,
            vec![
                zone("primary", CgZoneKind::Primary, 0, 0, 750, 750, 0),
                zone("sidebar", CgZoneKind::Sidebar, 750, 0, 250, 750, 1),
                zone("ticker", CgZoneKind::Ticker, 0, 750, 1_000, 250, 2),
            ],
        ),
        template(
            "lower-third",
            "Lower third",
            CgTemplateKind::LowerThird,
            vec![
                zone("primary", CgZoneKind::Primary, 0, 0, 1_000, 820, 0),
                zone("lower-third", CgZoneKind::LowerThird, 0, 820, 1_000, 180, 1),
            ],
        ),
        template(
            "bug",
            "Corner logo bug",
            CgTemplateKind::Bug,
            vec![
                zone("primary", CgZoneKind::Primary, 0, 0, 1_000, 1_000, 0),
                zone("logo", CgZoneKind::Logo, 820, 40, 140, 140, 1),
            ],
        ),
        template(
            "ticker",
            "Full-width ticker",
            CgTemplateKind::Ticker,
            vec![
                zone("primary", CgZoneKind::Primary, 0, 0, 1_000, 900, 0),
                zone("ticker", CgZoneKind::Ticker, 0, 900, 1_000, 100, 1),
            ],
        ),
        template(
            "emergency-overlay",
            "Emergency overlay",
            CgTemplateKind::EmergencyOverlay,
            vec![
                zone("primary", CgZoneKind::Primary, 0, 0, 1_000, 1_000, 0),
                zone("alert", CgZoneKind::Alert, 0, 650, 1_000, 350, 10),
            ],
        ),
    ]
}

pub fn built_in_template(template_id: &str) -> Option<CgTemplate> {
    built_in_templates()
        .into_iter()
        .find(|template| template.template_id == template_id)
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CgBoard {
    pub board_id: String,
    pub channel_id: String,
    pub template_id: String,
    pub active: bool,
    pub background_audio_asset_id: Option<String>,
    pub revision: u64,
    pub created_at_unix_ms: i64,
    pub updated_at_unix_ms: i64,
    pub created_by: String,
}

impl CgBoard {
    pub fn validate(&self) -> Result<(), &'static str> {
        if !is_uuid(&self.board_id)
            || !is_uuid(&self.channel_id)
            || built_in_template(&self.template_id).is_none()
            || self
                .background_audio_asset_id
                .as_deref()
                .is_some_and(|value| !is_sha256(value))
            || self.revision == 0
            || self.created_at_unix_ms < 0
            || self.updated_at_unix_ms < self.created_at_unix_ms
            || !visible_text(&self.created_by, 120)
        {
            return Err("CG board fields are invalid.");
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ScheduleDisplayMode {
    NextThree,
    ComingUpNext,
    JustWatched,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum CgContentSource {
    Manual { text: String },
    BulletinRotation,
    Feed { feed_source_id: String },
    Schedule { mode: ScheduleDisplayMode },
    Emergency,
    Image { asset_id: String },
    Clock { format: String },
    LiveVideo { source_id: String },
}

impl CgContentSource {
    pub fn validate(&self) -> Result<(), &'static str> {
        match self {
            Self::Manual { text } => bounded_text(text, 2_000),
            Self::BulletinRotation | Self::Schedule { .. } | Self::Emergency => Ok(()),
            Self::Feed { feed_source_id } => is_uuid(feed_source_id)
                .then_some(())
                .ok_or("CG feed identity must be a canonical UUID."),
            Self::Image { asset_id } => is_sha256(asset_id)
                .then_some(())
                .ok_or("CG image identity must be a lowercase SHA-256 digest."),
            Self::Clock { format } => {
                if visible_text(format, 80)
                    && !format
                        .chars()
                        .any(|character| matches!(character, '\r' | '\n'))
                {
                    Ok(())
                } else {
                    Err("CG clock format is invalid.")
                }
            }
            Self::LiveVideo { source_id } => is_uuid(source_id)
                .then_some(())
                .ok_or("CG live-source identity must be a canonical UUID."),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CgZoneConfig {
    pub zone_id: String,
    pub board_id: String,
    pub template_zone_key: String,
    pub zone_kind: CgZoneKind,
    pub content_source: CgContentSource,
    pub rotation_seconds: Option<u32>,
    pub approval_required: bool,
    pub allowed_tags: Vec<String>,
    pub revision: u64,
    pub created_at_unix_ms: i64,
    pub updated_at_unix_ms: i64,
}

impl CgZoneConfig {
    pub fn validate(&self) -> Result<(), &'static str> {
        if !is_uuid(&self.zone_id)
            || !is_uuid(&self.board_id)
            || !is_slug(&self.template_zone_key, 60)
            || self
                .rotation_seconds
                .is_some_and(|seconds| !(3..=3_600).contains(&seconds))
            || self.revision == 0
            || self.created_at_unix_ms < 0
            || self.updated_at_unix_ms < self.created_at_unix_ms
        {
            return Err("CG zone fields are invalid.");
        }
        self.content_source.validate()?;
        if self.approval_required
            && !matches!(
                &self.content_source,
                CgContentSource::Feed { .. } | CgContentSource::BulletinRotation
            )
        {
            return Err("CG approval gates apply only to feed or bulletin-rotation zones.");
        }
        validate_tags(&self.allowed_tags)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CgFeedKind {
    Rss,
    Ical,
    Caldav,
    Weather,
    Social,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CgFeedTrustTier {
    Station,
    Trusted,
    Moderated,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CgFeedSource {
    pub feed_source_id: String,
    pub channel_id: String,
    pub kind: CgFeedKind,
    pub label: String,
    pub source_url: String,
    pub trust_tier: CgFeedTrustTier,
    pub refresh_seconds: u32,
    pub enabled: bool,
    pub revision: u64,
    pub created_at_unix_ms: i64,
    pub updated_at_unix_ms: i64,
    pub created_by: String,
    pub last_fetched_at_unix_ms: Option<i64>,
    pub last_fetch_error: Option<String>,
}

impl CgFeedSource {
    pub fn validate(&self) -> Result<(), &'static str> {
        if !is_uuid(&self.feed_source_id)
            || !is_uuid(&self.channel_id)
            || !visible_text(&self.label, 160)
            || !valid_https_url(&self.source_url)
            || !(60..=86_400).contains(&self.refresh_seconds)
            || self.revision == 0
            || self.created_at_unix_ms < 0
            || self.updated_at_unix_ms < self.created_at_unix_ms
            || !visible_text(&self.created_by, 120)
            || self
                .last_fetched_at_unix_ms
                .is_some_and(|value| value < self.created_at_unix_ms)
            || self
                .last_fetch_error
                .as_deref()
                .is_some_and(|value| bounded_text(value, 1_000).is_err())
        {
            return Err("CG feed-source fields are invalid.");
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CgBulletinState {
    Submitted,
    NeedsChanges,
    Accepted,
    Scheduled,
    Declined,
    Expired,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum CgBulletinContent {
    Text { title: String, body: String },
    UploadedImage { asset_id: String, alt_text: String },
    FullscreenSlide { asset_id: String, alt_text: String },
    LiveVideo { source_id: String, title: String },
}

impl CgBulletinContent {
    pub fn validate(&self) -> Result<(), &'static str> {
        match self {
            Self::Text { title, body } => {
                if visible_text(title, 160) {
                    bounded_text(body, 4_000)
                } else {
                    Err("CG bulletin title is invalid.")
                }
            }
            Self::UploadedImage { asset_id, alt_text }
            | Self::FullscreenSlide { asset_id, alt_text } => {
                if is_sha256(asset_id) && visible_text(alt_text, 500) {
                    Ok(())
                } else {
                    Err("CG bulletin image fields are invalid.")
                }
            }
            Self::LiveVideo { source_id, title } => {
                if is_uuid(source_id) && visible_text(title, 160) {
                    Ok(())
                } else {
                    Err("CG live-video bulletin fields are invalid.")
                }
            }
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CgBulletin {
    pub bulletin_id: String,
    pub channel_id: String,
    pub content: CgBulletinContent,
    pub state: CgBulletinState,
    pub tags: Vec<String>,
    pub requested_start_at_unix_ms: Option<i64>,
    pub requested_end_at_unix_ms: Option<i64>,
    pub display_seconds: u32,
    pub audio_asset_id: Option<String>,
    pub submitted_by: String,
    pub moderation_notes: Option<String>,
    pub moderated_by: Option<String>,
    pub revision: u64,
    pub created_at_unix_ms: i64,
    pub updated_at_unix_ms: i64,
}

impl CgBulletin {
    pub fn validate(&self) -> Result<(), &'static str> {
        if !is_uuid(&self.bulletin_id)
            || !is_uuid(&self.channel_id)
            || !(3..=300).contains(&self.display_seconds)
            || self
                .audio_asset_id
                .as_deref()
                .is_some_and(|value| !is_sha256(value))
            || !visible_text(&self.submitted_by, 120)
            || self.revision == 0
            || self.created_at_unix_ms < 0
            || self.updated_at_unix_ms < self.created_at_unix_ms
            || self
                .moderation_notes
                .as_deref()
                .is_some_and(|value| bounded_text(value, 2_000).is_err())
            || self
                .moderated_by
                .as_deref()
                .is_some_and(|value| !visible_text(value, 120))
        {
            return Err("CG bulletin fields are invalid.");
        }
        self.content.validate()?;
        validate_tags(&self.tags)?;
        if let Some(start) = self.requested_start_at_unix_ms
            && start < 0
        {
            return Err("CG bulletin schedule timestamps must be nonnegative.");
        }
        if let Some(end) = self.requested_end_at_unix_ms
            && (end < 0
                || self
                    .requested_start_at_unix_ms
                    .is_some_and(|start| end <= start))
        {
            return Err("CG bulletin end time must be after its start time.");
        }
        match self.state {
            CgBulletinState::Submitted
                if self.moderated_by.is_some() || self.moderation_notes.is_some() =>
            {
                Err("A submitted CG bulletin cannot claim moderation.")
            }
            CgBulletinState::NeedsChanges | CgBulletinState::Declined
                if self.moderated_by.is_none() || self.moderation_notes.is_none() =>
            {
                Err("A declined or changes-requested CG bulletin needs moderator evidence.")
            }
            CgBulletinState::Accepted if self.moderated_by.is_none() => {
                Err("An accepted CG bulletin needs moderator evidence.")
            }
            CgBulletinState::Scheduled
                if self.moderated_by.is_none()
                    || self.requested_start_at_unix_ms.is_none()
                    || self.requested_end_at_unix_ms.is_none() =>
            {
                Err("A scheduled CG bulletin needs moderator evidence and a complete time window.")
            }
            CgBulletinState::Expired
                if self.moderated_by.is_none()
                    || self
                        .requested_end_at_unix_ms
                        .is_none_or(|end| end > self.updated_at_unix_ms) =>
            {
                Err("An expired CG bulletin needs elapsed end-time and moderation evidence.")
            }
            _ => Ok(()),
        }
    }

    pub fn is_airable_at(&self, now_unix_ms: i64) -> bool {
        now_unix_ms >= 0
            && matches!(
                self.state,
                CgBulletinState::Accepted | CgBulletinState::Scheduled
            )
            && self
                .requested_start_at_unix_ms
                .is_none_or(|start| start <= now_unix_ms)
            && self
                .requested_end_at_unix_ms
                .is_none_or(|end| now_unix_ms < end)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CgFeedItemApproval {
    pub feed_source_id: String,
    pub item_id: String,
    pub approved_by: String,
    pub approved_at_unix_ms: i64,
}

impl CgFeedItemApproval {
    pub fn validate(&self) -> Result<(), &'static str> {
        if !is_uuid(&self.feed_source_id)
            || !is_sha256(&self.item_id)
            || !visible_text(&self.approved_by, 120)
            || self.approved_at_unix_ms < 0
        {
            return Err("CG feed-item approval fields are invalid.");
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CgAuditEventKind {
    BoardCreated,
    BoardUpdated,
    ZoneCreated,
    ZoneUpdated,
    ZoneRemoved,
    FeedCreated,
    FeedUpdated,
    FeedRemoved,
    BulletinSubmitted,
    BulletinModerated,
    BulletinExpired,
    FeedItemApproved,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CgAuditEvent {
    pub audit_id: String,
    pub board_id: Option<String>,
    pub channel_id: String,
    pub event_kind: CgAuditEventKind,
    pub operator_id: String,
    pub occurred_at_unix_ms: i64,
    pub details: BTreeMap<String, String>,
}

impl CgAuditEvent {
    pub fn validate(&self) -> Result<(), &'static str> {
        if !is_uuid(&self.audit_id)
            || self
                .board_id
                .as_deref()
                .is_some_and(|value| !is_uuid(value))
            || !is_uuid(&self.channel_id)
            || !visible_text(&self.operator_id, 120)
            || self.occurred_at_unix_ms < 0
            || self.details.len() > 32
            || self.details.iter().any(|(key, value)| {
                !is_slug(key, 60) || value.len() > 1_000 || has_disallowed_control(value)
            })
        {
            return Err("CG audit-event fields are invalid.");
        }
        Ok(())
    }
}

fn validate_tags(tags: &[String]) -> Result<(), &'static str> {
    if tags.len() > 32
        || tags.iter().any(|tag| !is_slug(tag, 40))
        || tags.iter().collect::<BTreeSet<_>>().len() != tags.len()
    {
        return Err("CG tags must be unique lowercase slugs.");
    }
    Ok(())
}

fn is_uuid(value: &str) -> bool {
    value.len() == 36
        && value.bytes().enumerate().all(|(index, byte)| match index {
            8 | 13 | 18 | 23 => byte == b'-',
            _ => byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte),
        })
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn is_slug(value: &str, maximum: usize) -> bool {
    !value.is_empty()
        && value.len() <= maximum
        && value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'-' | b'_')
        })
}

fn visible_text(value: &str, maximum: usize) -> bool {
    !value.trim().is_empty() && value.len() <= maximum && !value.chars().any(char::is_control)
}

fn bounded_text(value: &str, maximum: usize) -> Result<(), &'static str> {
    if !value.trim().is_empty() && value.len() <= maximum && !has_disallowed_control(value) {
        Ok(())
    } else {
        Err("CG text is empty, too long, or contains unsupported control characters.")
    }
}

fn has_disallowed_control(value: &str) -> bool {
    value
        .chars()
        .any(|character| character.is_control() && !matches!(character, '\r' | '\n' | '\t'))
}

fn valid_https_url(value: &str) -> bool {
    value.len() <= 500
        && !value.chars().any(char::is_control)
        && value
            .strip_prefix("https://")
            .is_some_and(|rest| !rest.is_empty() && !rest.starts_with('/') && !rest.contains('@'))
}

#[cfg(test)]
mod tests {
    use super::*;

    const CHANNEL_ID: &str = "5d85189a-cd1f-43dc-a619-66a2aeb09ac4";
    const BOARD_ID: &str = "94a0dc01-d314-4b55-b2e4-cba39cc3f1dc";

    #[test]
    fn ships_all_six_valid_built_in_templates() {
        let templates = built_in_templates();
        assert_eq!(templates.len(), 6);
        assert!(templates.iter().all(|template| template.validate().is_ok()));
        assert_eq!(
            templates
                .iter()
                .map(|template| template.kind)
                .collect::<BTreeSet<_>>(),
            BTreeSet::from([
                CgTemplateKind::Fullscreen,
                CgTemplateKind::LBar,
                CgTemplateKind::LowerThird,
                CgTemplateKind::Bug,
                CgTemplateKind::Ticker,
                CgTemplateKind::EmergencyOverlay,
            ])
        );
    }

    #[test]
    fn rejects_zones_outside_the_canvas_and_unknown_templates() {
        let invalid = CgRect {
            x: 900,
            y: 0,
            width: 101,
            height: 100,
        };
        assert!(invalid.validate().is_err());
        let board = CgBoard {
            board_id: BOARD_ID.into(),
            channel_id: CHANNEL_ID.into(),
            template_id: "operator-uploaded-html".into(),
            active: true,
            background_audio_asset_id: None,
            revision: 1,
            created_at_unix_ms: 1,
            updated_at_unix_ms: 1,
            created_by: "operator".into(),
        };
        assert_eq!(board.validate(), Err("CG board fields are invalid."));
    }

    #[test]
    fn enforces_bulletin_moderation_and_air_windows() {
        let bulletin = CgBulletin {
            bulletin_id: "597f52ae-f16c-4ff8-ab32-60379cd950c4".into(),
            channel_id: CHANNEL_ID.into(),
            content: CgBulletinContent::Text {
                title: "Library board meeting".into(),
                body: "Tuesday at 6 PM in the community room.".into(),
            },
            state: CgBulletinState::Scheduled,
            tags: vec!["community".into()],
            requested_start_at_unix_ms: Some(1_000),
            requested_end_at_unix_ms: Some(2_000),
            display_seconds: 12,
            audio_asset_id: None,
            submitted_by: "library-clerk".into(),
            moderation_notes: None,
            moderated_by: Some("station-operator".into()),
            revision: 2,
            created_at_unix_ms: 100,
            updated_at_unix_ms: 900,
        };
        assert!(bulletin.validate().is_ok());
        assert!(!bulletin.is_airable_at(999));
        assert!(bulletin.is_airable_at(1_000));
        assert!(!bulletin.is_airable_at(2_000));

        let mut invalid = bulletin;
        invalid.moderated_by = None;
        assert!(invalid.validate().is_err());
    }

    #[test]
    fn restricts_approval_gates_and_remote_feed_urls() {
        let zone = CgZoneConfig {
            zone_id: "8f4f0f70-5c30-48c6-819a-fb22b34353c5".into(),
            board_id: BOARD_ID.into(),
            template_zone_key: "ticker".into(),
            zone_kind: CgZoneKind::Ticker,
            content_source: CgContentSource::Manual {
                text: "Road closure at 5th and Main".into(),
            },
            rotation_seconds: None,
            approval_required: true,
            allowed_tags: Vec::new(),
            revision: 1,
            created_at_unix_ms: 1,
            updated_at_unix_ms: 1,
        };
        assert_eq!(
            zone.validate(),
            Err("CG approval gates apply only to feed or bulletin-rotation zones.")
        );

        let feed = CgFeedSource {
            feed_source_id: "796ca80a-9cc2-4fc1-84a2-2a88d8f92f24".into(),
            channel_id: CHANNEL_ID.into(),
            kind: CgFeedKind::Rss,
            label: "Town notices".into(),
            source_url: "http://notices.example.test/rss".into(),
            trust_tier: CgFeedTrustTier::Moderated,
            refresh_seconds: 300,
            enabled: true,
            revision: 1,
            created_at_unix_ms: 1,
            updated_at_unix_ms: 1,
            created_by: "operator".into(),
            last_fetched_at_unix_ms: None,
            last_fetch_error: None,
        };
        assert_eq!(feed.validate(), Err("CG feed-source fields are invalid."));
    }
}
