//! Pure, fail-closed meeting-agenda authority contracts.
//!
//! Import parsing, persistence, schedule synchronization, publication, and UI
//! boundaries are deliberately outside this crate.

use serde::{Deserialize, Serialize};

const MAX_TEXT: usize = 200;
const MAX_SUMMARY: usize = 2_000;
const MAX_STAFF_NOTES: usize = 4_000;
const MAX_ITEMS: usize = 128;
const MAX_CHAPTERS: usize = 512;
const MAX_MEDIA_DURATION_MS: i64 = 86_400_000;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgendaActor {
    pub actor_id: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", tag = "kind", deny_unknown_fields)]
pub enum AgendaProvenance {
    StaffAuthored {
        author: AgendaActor,
        asserted_at_unix_ms: i64,
    },
    TextImportClaim {
        importer: AgendaActor,
        source_sha256: String,
        asserted_at_unix_ms: i64,
    },
    PdfImportClaim {
        importer: AgendaActor,
        source_sha256: String,
        asserted_at_unix_ms: i64,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AgendaItemVisibility {
    Public,
    StaffOnly,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgendaItem {
    pub item_id: String,
    pub ordinal: u16,
    pub title: String,
    pub public_summary: Option<String>,
    pub staff_notes: Option<String>,
    pub visibility: AgendaItemVisibility,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgendaChapter {
    pub chapter_id: String,
    pub agenda_item_id: String,
    pub ordinal: u16,
    pub title: String,
    pub starts_at_media_offset_ms: i64,
    pub ends_at_media_offset_ms: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgendaSyncIntent {
    pub channel_id: String,
    pub schedule_item_id: String,
    pub schedule_revision: u64,
    pub media_asset_sha256: String,
    pub expected_media_duration_ms: i64,
    pub requested_by: AgendaActor,
    pub requested_at_unix_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Agenda {
    pub agenda_id: String,
    pub channel_id: String,
    pub meeting_id: String,
    pub title: String,
    pub meeting_starts_at_unix_ms: i64,
    pub provenance: AgendaProvenance,
    pub sync_intent: AgendaSyncIntent,
    pub items: Vec<AgendaItem>,
    pub chapters: Vec<AgendaChapter>,
    pub revision: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PublicAgendaChapter {
    pub ordinal: u16,
    pub title: String,
    pub starts_at_media_offset_ms: i64,
    pub ends_at_media_offset_ms: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PublicAgendaItem {
    pub ordinal: u16,
    pub title: String,
    pub summary: Option<String>,
    pub chapters: Vec<PublicAgendaChapter>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PublicAgendaProjection {
    pub agenda_id: String,
    pub meeting_id: String,
    pub title: String,
    pub meeting_starts_at_unix_ms: i64,
    pub items: Vec<PublicAgendaItem>,
}

impl AgendaActor {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.actor_id, "agenda actor identity")
    }
}

impl AgendaProvenance {
    pub fn validate(&self) -> Result<(), String> {
        match self {
            Self::StaffAuthored {
                author,
                asserted_at_unix_ms,
            } => validate_actor_time(author, *asserted_at_unix_ms, "staff agenda provenance"),
            Self::TextImportClaim {
                importer,
                source_sha256,
                asserted_at_unix_ms,
            }
            | Self::PdfImportClaim {
                importer,
                source_sha256,
                asserted_at_unix_ms,
            } => {
                validate_actor_time(importer, *asserted_at_unix_ms, "agenda import claim")?;
                if !is_sha256(source_sha256) {
                    return Err("agenda import claim source must be a SHA-256 hash".into());
                }
                Ok(())
            }
        }
    }
}

impl AgendaSyncIntent {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.channel_id, "agenda sync channel identity")?;
        validate_uuid(&self.schedule_item_id, "agenda sync schedule item identity")?;
        if self.schedule_revision == 0
            || !is_sha256(&self.media_asset_sha256)
            || !(1..=MAX_MEDIA_DURATION_MS).contains(&self.expected_media_duration_ms)
        {
            return Err(
                "agenda sync intent revision, media identity, or duration is invalid".into(),
            );
        }
        validate_actor_time(
            &self.requested_by,
            self.requested_at_unix_ms,
            "agenda sync intent",
        )
    }
}

impl Agenda {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.agenda_id, "agenda identity")?;
        validate_uuid(&self.channel_id, "agenda channel identity")?;
        validate_uuid(&self.meeting_id, "agenda meeting identity")?;
        validate_visible(&self.title, MAX_TEXT, "agenda title")?;
        if self.meeting_starts_at_unix_ms < 0 || self.revision == 0 {
            return Err("agenda meeting time or revision is invalid".into());
        }
        self.provenance.validate()?;
        self.sync_intent.validate()?;
        if self.sync_intent.channel_id != self.channel_id {
            return Err("agenda sync intent channel does not match agenda channel".into());
        }
        validate_items(&self.items)?;
        validate_chapters(
            &self.items,
            &self.chapters,
            self.sync_intent.expected_media_duration_ms,
        )
    }

    pub fn public_projection(&self) -> Result<PublicAgendaProjection, String> {
        self.validate()?;
        let items = self
            .items
            .iter()
            .filter(|item| item.visibility == AgendaItemVisibility::Public)
            .map(|item| PublicAgendaItem {
                ordinal: item.ordinal,
                title: item.title.clone(),
                summary: item.public_summary.clone(),
                chapters: self
                    .chapters
                    .iter()
                    .filter(|chapter| chapter.agenda_item_id == item.item_id)
                    .map(|chapter| PublicAgendaChapter {
                        ordinal: chapter.ordinal,
                        title: chapter.title.clone(),
                        starts_at_media_offset_ms: chapter.starts_at_media_offset_ms,
                        ends_at_media_offset_ms: chapter.ends_at_media_offset_ms,
                    })
                    .collect(),
            })
            .collect();
        Ok(PublicAgendaProjection {
            agenda_id: self.agenda_id.clone(),
            meeting_id: self.meeting_id.clone(),
            title: self.title.clone(),
            meeting_starts_at_unix_ms: self.meeting_starts_at_unix_ms,
            items,
        })
    }
}

pub fn validate_agenda_write(
    expected_revision: Option<u64>,
    current: Option<&Agenda>,
    incoming: &Agenda,
) -> Result<(), String> {
    incoming.validate()?;
    match (expected_revision, current) {
        (None, None) if incoming.revision == 1 => Ok(()),
        (Some(expected), Some(current))
            if expected == current.revision
                && incoming.revision == current.revision.saturating_add(1) =>
        {
            current.validate()?;
            if incoming.agenda_id != current.agenda_id
                || incoming.channel_id != current.channel_id
                || incoming.meeting_id != current.meeting_id
            {
                return Err("agenda identity cannot be rebound".into());
            }
            if incoming.provenance != current.provenance {
                return Err("agenda provenance cannot be rebound".into());
            }
            Ok(())
        }
        _ => Err("agenda revision compare-and-set conflict".into()),
    }
}

fn validate_items(items: &[AgendaItem]) -> Result<(), String> {
    if items.is_empty() || items.len() > MAX_ITEMS {
        return Err("agenda items must be nonempty and bounded".into());
    }
    for (index, item) in items.iter().enumerate() {
        validate_uuid(&item.item_id, "agenda item identity")?;
        if item.ordinal != u16::try_from(index + 1).map_err(|_| "agenda item order is invalid")? {
            return Err("agenda item order must be contiguous and deterministic".into());
        }
        validate_visible(&item.title, MAX_TEXT, "agenda item title")?;
        if let Some(summary) = &item.public_summary {
            validate_visible(summary, MAX_SUMMARY, "agenda item public summary")?;
        }
        if let Some(notes) = &item.staff_notes {
            validate_visible(notes, MAX_STAFF_NOTES, "agenda item staff notes")?;
        }
        if items[..index]
            .iter()
            .any(|prior| prior.item_id == item.item_id)
        {
            return Err("agenda item identities must be unique".into());
        }
    }
    Ok(())
}

fn validate_chapters(
    items: &[AgendaItem],
    chapters: &[AgendaChapter],
    media_duration_ms: i64,
) -> Result<(), String> {
    if chapters.len() > MAX_CHAPTERS {
        return Err("agenda chapters exceed the bounded limit".into());
    }
    let mut previous_start = None;
    let mut previous_end = None;
    let mut previous_item_ordinal = 0_u16;
    for (index, chapter) in chapters.iter().enumerate() {
        validate_uuid(&chapter.chapter_id, "agenda chapter identity")?;
        if chapter.ordinal
            != u16::try_from(index + 1).map_err(|_| "agenda chapter order is invalid")?
        {
            return Err("agenda chapter order must be contiguous and deterministic".into());
        }
        validate_visible(&chapter.title, MAX_TEXT, "agenda chapter title")?;
        let item = items
            .iter()
            .find(|item| item.item_id == chapter.agenda_item_id)
            .ok_or_else(|| "agenda chapter references an unknown agenda item".to_string())?;
        if item.ordinal < previous_item_ordinal
            || previous_start.is_some_and(|previous| chapter.starts_at_media_offset_ms < previous)
            || previous_end.is_some_and(|previous| chapter.starts_at_media_offset_ms < previous)
            || chapter.starts_at_media_offset_ms < 0
            || chapter.starts_at_media_offset_ms >= media_duration_ms
        {
            return Err("agenda chapter order or media offset is invalid".into());
        }
        if let Some(end) = chapter.ends_at_media_offset_ms
            && (end <= chapter.starts_at_media_offset_ms || end > media_duration_ms)
        {
            return Err("agenda chapter end offset is invalid".into());
        }
        if chapters[..index]
            .iter()
            .any(|prior| prior.chapter_id == chapter.chapter_id)
        {
            return Err("agenda chapter identities must be unique".into());
        }
        previous_start = Some(chapter.starts_at_media_offset_ms);
        previous_end = chapter.ends_at_media_offset_ms.or(previous_end);
        previous_item_ordinal = item.ordinal;
    }
    Ok(())
}

fn validate_actor_time(actor: &AgendaActor, time: i64, field: &str) -> Result<(), String> {
    actor.validate()?;
    if time < 0 {
        return Err(format!("{field} time must be nonnegative"));
    }
    Ok(())
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

fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}
