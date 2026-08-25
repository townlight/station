//! Versioned, public, cross-platform OTT interchange contracts.
//!
//! These contracts do not implement Roku, Apple, Android, or Fire clients, and do
//! not authorize a client to grant itself server access.

use std::collections::HashSet;

use serde::{Deserialize, Serialize};

pub const OTT_CONTRACT_VERSION: u16 = 1;
pub const MAX_CATALOG_ITEMS: usize = 100;
pub const MAX_PAGE_CURSOR_BYTES: usize = 256;
pub const MAX_TEXT_BYTES: usize = 500;
pub const MAX_URL_BYTES: usize = 2_048;
pub const MAX_SUBTITLES_PER_ITEM: usize = 16;
pub const MAX_SESSION_LIFETIME_MS: i64 = 86_400_000;
pub const MAX_VOD_DURATION_MS: u64 = 24 * 60 * 60 * 1_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OttPlatform {
    Roku,
    Apple,
    Android,
    FireTv,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OttCapability {
    HlsPlayback,
    WebVttSubtitles,
    ClosedCaptions,
    ScreenReaderSemantics,
    KeyboardOrRemoteNavigation,
    PlaybackBeacons,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlatformCapabilityDeclaration {
    pub contract_version: u16,
    pub client_id: String,
    pub platform: OttPlatform,
    pub capabilities: Vec<OttCapability>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum PublicContentIdentity {
    Live { channel_id: String },
    Vod { asset_id: String },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PlaybackProtocol {
    Hls,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlaybackSource {
    pub protocol: PlaybackProtocol,
    pub manifest_url: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SubtitleKind {
    ClosedCaptions,
    Subtitles,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PublicSubtitleTrack {
    pub track_id: String,
    pub language: String,
    pub kind: SubtitleKind,
    pub url: String,
    pub is_default: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AccessibilitySemantics {
    pub captions_available: bool,
    pub screen_reader_summary: String,
    pub remote_navigation_supported: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PublicCatalogItem {
    pub content_id: String,
    pub identity: PublicContentIdentity,
    pub title: String,
    pub summary: String,
    pub vod_duration_ms: Option<u64>,
    pub playback: PlaybackSource,
    pub subtitles: Vec<PublicSubtitleTrack>,
    pub accessibility: AccessibilitySemantics,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PublicCatalogPage {
    pub contract_version: u16,
    pub catalog_revision: u64,
    pub items: Vec<PublicCatalogItem>,
    pub next_cursor: Option<String>,
}

/// Server-issued session authority. A client only references this record; it cannot
/// provide entitlement/access fields or create an equivalent client authority.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlaybackSessionAuthority {
    pub contract_version: u16,
    pub session_id: String,
    pub content_id: String,
    pub catalog_revision: u64,
    pub session_revision: u64,
    pub server_session_digest: String,
    pub issued_at_unix_ms: i64,
    pub expires_at_unix_ms: i64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PlaybackBeaconEvent {
    Started,
    Progress,
    Completed,
}

/// A client observation only. It cannot carry account, plan, entitlement, grant, or
/// server-issued-session fields; validation requires an independently supplied session.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ClientPlaybackBeacon {
    pub contract_version: u16,
    pub beacon_id: String,
    pub session_id: String,
    pub session_revision: u64,
    pub observed_at_unix_ms: i64,
    pub event: PlaybackBeaconEvent,
    pub position_ms: u64,
}

impl PlatformCapabilityDeclaration {
    pub fn validate(&self) -> Result<(), String> {
        validate_version(self.contract_version)?;
        validate_uuid(&self.client_id, "OTT client identity")?;
        if self.capabilities.is_empty() || self.capabilities.len() > 16 {
            return Err("OTT platform capability count is invalid or unbounded".into());
        }
        let mut capabilities = HashSet::with_capacity(self.capabilities.len());
        for capability in &self.capabilities {
            if !capabilities.insert(capability) {
                return Err("OTT platform capabilities must be unique".into());
            }
        }
        for required in [
            OttCapability::HlsPlayback,
            OttCapability::WebVttSubtitles,
            OttCapability::ClosedCaptions,
            OttCapability::ScreenReaderSemantics,
            OttCapability::KeyboardOrRemoteNavigation,
            OttCapability::PlaybackBeacons,
        ] {
            if !capabilities.contains(&required) {
                return Err("OTT platform declaration is missing a required playback or accessibility capability".into());
            }
        }
        Ok(())
    }
}

impl PlaybackSource {
    pub fn validate(&self) -> Result<(), String> {
        validate_public_https_url(&self.manifest_url, "OTT playback manifest URL")
    }
}

impl PublicSubtitleTrack {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.track_id, "OTT subtitle track identity")?;
        validate_language(&self.language)?;
        validate_public_https_url(&self.url, "OTT subtitle URL")
    }
}

impl AccessibilitySemantics {
    pub fn validate(&self) -> Result<(), String> {
        if !self.captions_available || !self.remote_navigation_supported {
            return Err(
                "OTT accessibility semantics require captions and remote navigation support".into(),
            );
        }
        validate_visible(
            &self.screen_reader_summary,
            MAX_TEXT_BYTES,
            "OTT screen-reader summary",
        )
    }
}

impl PublicCatalogItem {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.content_id, "OTT content identity")?;
        validate_visible(&self.title, MAX_TEXT_BYTES, "OTT content title")?;
        validate_visible(&self.summary, MAX_TEXT_BYTES, "OTT content summary")?;
        match (&self.identity, self.vod_duration_ms) {
            (PublicContentIdentity::Live { channel_id }, None) => {
                validate_uuid(channel_id, "OTT live channel identity")?
            }
            (PublicContentIdentity::Vod { asset_id }, Some(duration))
                if duration > 0 && duration <= MAX_VOD_DURATION_MS =>
            {
                validate_uuid(asset_id, "OTT VOD asset identity")?
            }
            _ => return Err("OTT live/VOD identity or duration is ambiguous or invalid".into()),
        }
        self.playback.validate()?;
        self.accessibility.validate()?;
        if self.subtitles.is_empty() || self.subtitles.len() > MAX_SUBTITLES_PER_ITEM {
            return Err("OTT subtitle count is invalid or unbounded".into());
        }
        let mut track_ids = HashSet::with_capacity(self.subtitles.len());
        let mut default_tracks = 0_u8;
        let mut has_closed_captions = false;
        for subtitle in &self.subtitles {
            subtitle.validate()?;
            if !track_ids.insert(subtitle.track_id.as_str()) {
                return Err("OTT subtitle identities must be unique".into());
            }
            if subtitle.is_default {
                default_tracks = default_tracks.saturating_add(1);
            }
            has_closed_captions |= subtitle.kind == SubtitleKind::ClosedCaptions;
        }
        if default_tracks != 1 || !has_closed_captions {
            return Err(
                "OTT item requires one default subtitle and closed-caption semantics".into(),
            );
        }
        Ok(())
    }
}

impl PublicCatalogPage {
    pub fn validate(&self) -> Result<(), String> {
        validate_version(self.contract_version)?;
        if self.catalog_revision == 0
            || self.items.is_empty()
            || self.items.len() > MAX_CATALOG_ITEMS
        {
            return Err("OTT catalog revision or page count is invalid or unbounded".into());
        }
        if let Some(cursor) = &self.next_cursor {
            validate_visible(cursor, MAX_PAGE_CURSOR_BYTES, "OTT catalog cursor")?;
        }
        let mut content_ids = HashSet::with_capacity(self.items.len());
        let mut live_or_vod_ids = HashSet::with_capacity(self.items.len());
        for item in &self.items {
            item.validate()?;
            if !content_ids.insert(item.content_id.as_str())
                || !live_or_vod_ids.insert(content_identity_key(&item.identity))
            {
                return Err("OTT catalog identities must be unique and unambiguous".into());
            }
        }
        Ok(())
    }
}

impl PlaybackSessionAuthority {
    pub fn validate_shape(&self) -> Result<(), String> {
        validate_version(self.contract_version)?;
        validate_uuid(&self.session_id, "OTT playback session identity")?;
        validate_uuid(&self.content_id, "OTT playback session content identity")?;
        if self.catalog_revision == 0
            || self.session_revision == 0
            || !is_sha256(&self.server_session_digest)
            || self.issued_at_unix_ms < 0
            || self.expires_at_unix_ms <= self.issued_at_unix_ms
            || self.expires_at_unix_ms - self.issued_at_unix_ms > MAX_SESSION_LIFETIME_MS
        {
            return Err("OTT playback session authority is invalid, stale, or unbounded".into());
        }
        Ok(())
    }
}

impl ClientPlaybackBeacon {
    pub fn validate_shape(&self) -> Result<(), String> {
        validate_version(self.contract_version)?;
        validate_uuid(&self.beacon_id, "OTT playback beacon identity")?;
        validate_uuid(&self.session_id, "OTT playback beacon session identity")?;
        if self.session_revision == 0 || self.observed_at_unix_ms < 0 {
            return Err("OTT playback beacon revision or time is invalid".into());
        }
        Ok(())
    }
}

pub fn validate_session_issue(
    session: &PlaybackSessionAuthority,
    catalog: &PublicCatalogPage,
    client: &PlatformCapabilityDeclaration,
) -> Result<(), String> {
    session.validate_shape()?;
    catalog.validate()?;
    client.validate()?;
    if session.catalog_revision != catalog.catalog_revision
        || !catalog
            .items
            .iter()
            .any(|item| item.content_id == session.content_id)
    {
        return Err(
            "OTT session authority must bind an exact current catalog item and revision".into(),
        );
    }
    Ok(())
}

pub fn validate_client_beacon(
    beacon: &ClientPlaybackBeacon,
    server_session: &PlaybackSessionAuthority,
) -> Result<(), String> {
    beacon.validate_shape()?;
    server_session.validate_shape()?;
    if beacon.session_id != server_session.session_id
        || beacon.session_revision != server_session.session_revision
        || beacon.observed_at_unix_ms < server_session.issued_at_unix_ms
        || beacon.observed_at_unix_ms >= server_session.expires_at_unix_ms
    {
        return Err(
            "OTT client beacon must bind the independently supplied current server session".into(),
        );
    }
    Ok(())
}

fn content_identity_key(identity: &PublicContentIdentity) -> (&'static str, &str) {
    match identity {
        PublicContentIdentity::Live { channel_id } => ("live", channel_id),
        PublicContentIdentity::Vod { asset_id } => ("vod", asset_id),
    }
}

fn validate_version(version: u16) -> Result<(), String> {
    if version != OTT_CONTRACT_VERSION {
        return Err("OTT contract version is unsupported or stale".into());
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

fn validate_language(value: &str) -> Result<(), String> {
    if value.len() < 2
        || value.len() > 16
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphabetic() || byte == b'-')
    {
        return Err("OTT subtitle language must be a bounded BCP-47-like tag".into());
    }
    Ok(())
}

fn validate_public_https_url(value: &str, field: &str) -> Result<(), String> {
    if value.len() > MAX_URL_BYTES
        || !value.starts_with("https://")
        || value.contains(['@', '\\', ' ', '\t', '\r', '\n'])
        || value.contains('?')
        || value.contains('#')
        || value.len() == "https://".len()
    {
        return Err(format!(
            "{field} must be a bounded public HTTPS URL without credentials, query, or fragment"
        ));
    }
    Ok(())
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

#[cfg(test)]
mod tests {
    use super::*;

    const LIVE: &str = "11111111-1111-4111-8111-111111111111";
    const VOD: &str = "22222222-2222-4222-8222-222222222222";
    const CONTENT_LIVE: &str = "33333333-3333-4333-8333-333333333333";
    const CONTENT_VOD: &str = "44444444-4444-4444-8444-444444444444";

    fn subtitle() -> PublicSubtitleTrack {
        PublicSubtitleTrack {
            track_id: "55555555-5555-4555-8555-555555555555".into(),
            language: "en-US".into(),
            kind: SubtitleKind::ClosedCaptions,
            url: "https://cdn.example.test/subtitles/primary.vtt".into(),
            is_default: true,
        }
    }
    fn accessibility() -> AccessibilitySemantics {
        AccessibilitySemantics {
            captions_available: true,
            screen_reader_summary: "City council coverage".into(),
            remote_navigation_supported: true,
        }
    }
    fn item(content_id: &str, identity: PublicContentIdentity) -> PublicCatalogItem {
        let duration = matches!(&identity, PublicContentIdentity::Vod { .. }).then_some(1_800_000);
        PublicCatalogItem {
            content_id: content_id.into(),
            identity,
            title: "Council meeting".into(),
            summary: "Accessible civic programming".into(),
            vod_duration_ms: duration,
            playback: PlaybackSource {
                protocol: PlaybackProtocol::Hls,
                manifest_url: "https://cdn.example.test/stream/master.m3u8".into(),
            },
            subtitles: vec![subtitle()],
            accessibility: accessibility(),
        }
    }
    fn catalog() -> PublicCatalogPage {
        PublicCatalogPage {
            contract_version: OTT_CONTRACT_VERSION,
            catalog_revision: 1,
            items: vec![
                item(
                    CONTENT_LIVE,
                    PublicContentIdentity::Live {
                        channel_id: LIVE.into(),
                    },
                ),
                item(
                    CONTENT_VOD,
                    PublicContentIdentity::Vod {
                        asset_id: VOD.into(),
                    },
                ),
            ],
            next_cursor: None,
        }
    }
    fn client() -> PlatformCapabilityDeclaration {
        PlatformCapabilityDeclaration {
            contract_version: OTT_CONTRACT_VERSION,
            client_id: "66666666-6666-4666-8666-666666666666".into(),
            platform: OttPlatform::Roku,
            capabilities: vec![
                OttCapability::HlsPlayback,
                OttCapability::WebVttSubtitles,
                OttCapability::ClosedCaptions,
                OttCapability::ScreenReaderSemantics,
                OttCapability::KeyboardOrRemoteNavigation,
                OttCapability::PlaybackBeacons,
            ],
        }
    }
    fn session() -> PlaybackSessionAuthority {
        PlaybackSessionAuthority {
            contract_version: OTT_CONTRACT_VERSION,
            session_id: "77777777-7777-4777-8777-777777777777".into(),
            content_id: CONTENT_LIVE.into(),
            catalog_revision: 1,
            session_revision: 1,
            server_session_digest: "a".repeat(64),
            issued_at_unix_ms: 1_700_000_000_000,
            expires_at_unix_ms: 1_700_000_060_000,
        }
    }

    #[test]
    fn validates_versioned_live_vod_catalog_platform_session_and_beacon_contracts() {
        let catalog = catalog();
        let client = client();
        let session = session();
        let beacon = ClientPlaybackBeacon {
            contract_version: OTT_CONTRACT_VERSION,
            beacon_id: "88888888-8888-4888-8888-888888888888".into(),
            session_id: session.session_id.clone(),
            session_revision: 1,
            observed_at_unix_ms: 1_700_000_001_000,
            event: PlaybackBeaconEvent::Started,
            position_ms: 0,
        };
        assert!(catalog.validate().is_ok());
        assert!(validate_session_issue(&session, &catalog, &client).is_ok());
        assert!(validate_client_beacon(&beacon, &session).is_ok());
    }

    #[test]
    fn rejects_unsafe_urls_missing_caption_semantics_and_ambiguous_content() {
        let mut unsafe_item = item(
            CONTENT_LIVE,
            PublicContentIdentity::Live {
                channel_id: LIVE.into(),
            },
        );
        unsafe_item.playback.manifest_url = "http://user@example.test/a.m3u8?token=no".into();
        assert!(unsafe_item.validate().is_err());
        let mut missing_captions = item(
            CONTENT_LIVE,
            PublicContentIdentity::Live {
                channel_id: LIVE.into(),
            },
        );
        missing_captions.subtitles[0].kind = SubtitleKind::Subtitles;
        assert!(missing_captions.validate().is_err());
        let mut ambiguous = item(
            CONTENT_LIVE,
            PublicContentIdentity::Live {
                channel_id: LIVE.into(),
            },
        );
        ambiguous.vod_duration_ms = Some(1);
        assert!(ambiguous.validate().is_err());
    }

    #[test]
    fn rejects_stale_versions_missing_platform_capabilities_and_unbounded_pages() {
        let mut stale = client();
        stale.contract_version = OTT_CONTRACT_VERSION + 1;
        assert!(stale.validate().is_err());
        let mut incomplete = client();
        incomplete.capabilities.pop();
        assert!(incomplete.validate().is_err());
        let mut page = catalog();
        page.items = vec![
            item(
                CONTENT_LIVE,
                PublicContentIdentity::Live {
                    channel_id: LIVE.into()
                }
            );
            MAX_CATALOG_ITEMS + 1
        ];
        assert!(page.validate().is_err());
    }

    #[test]
    fn rejects_client_forged_session_authority_and_unknown_fields() {
        let session = session();
        let forged = ClientPlaybackBeacon {
            contract_version: OTT_CONTRACT_VERSION,
            beacon_id: "88888888-8888-4888-8888-888888888888".into(),
            session_id: "99999999-9999-4999-8999-999999999999".into(),
            session_revision: 1,
            observed_at_unix_ms: 1_700_000_001_000,
            event: PlaybackBeaconEvent::Progress,
            position_ms: 1_000,
        };
        assert!(validate_client_beacon(&forged, &session).is_err());
        assert!(serde_json::from_str::<ClientPlaybackBeacon>(r#"{"contract_version":1,"beacon_id":"88888888-8888-4888-8888-888888888888","session_id":"77777777-7777-4777-8777-777777777777","session_revision":1,"observed_at_unix_ms":1,"event":"started","position_ms":0,"entitlement_granted":true}"#).is_err());
    }

    #[test]
    fn rejects_duplicate_or_cross_kind_catalog_identities() {
        let mut page = catalog();
        page.items.push(item(
            "99999999-9999-4999-8999-999999999999",
            PublicContentIdentity::Live {
                channel_id: LIVE.into(),
            },
        ));
        assert!(page.validate().is_err());
        assert!(serde_json::from_str::<PublicContentIdentity>(r#"{"kind":"live","channel_id":"11111111-1111-4111-8111-111111111111","asset_id":"22222222-2222-4222-8222-222222222222"}"#).is_err());
    }
}
