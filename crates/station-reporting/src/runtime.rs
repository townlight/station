//! Bounded, replay-safe as-run projection primitives.
//!
//! The adapter input is deliberately an enriched journal take.  An upstream
//! station boundary must join the immutable journal record to exact catalog and
//! schedule revisions before calling this module.  This is an authority for the
//! projection only: it does not attest transport delivery, receiver acceptance,
//! or field observation.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

pub const MAX_RUNTIME_ENTRIES: usize = 10_000;
pub const MAX_RUNTIME_CHANNELS: usize = 128;
pub const MAX_EXPORT_ENTRIES: usize = 10_000;
pub const MAX_TITLE_BYTES: usize = 240;
pub const MAX_FIELD_BYTES: usize = 240;
pub const MAX_UNAVAILABLE_REASON_BYTES: usize = 512;
pub const MAX_RENDERED_EXPORT_BYTES: usize = 4 * 1024 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AsRunState {
    Program,
    Fallback,
    Live,
    Cg,
    Caption,
    Gap,
}

impl AsRunState {
    fn as_str(self) -> &'static str {
        match self {
            Self::Program => "program",
            Self::Fallback => "fallback",
            Self::Live => "live",
            Self::Cg => "cg",
            Self::Caption => "caption",
            Self::Gap => "gap",
        }
    }

    fn unavailable_reason(self) -> Option<&'static str> {
        match self {
            Self::Gap => {
                Some("Journal has an explicit contiguous gap; no aired content is asserted.")
            }
            Self::Fallback => {
                Some("Fallback state is journal-derived; content identity is unavailable.")
            }
            Self::Live => Some("Live state is journal-derived; program metadata is unavailable."),
            Self::Cg => Some("CG state is journal-derived; it is not a program airing."),
            Self::Caption => {
                Some("Caption state is journal-derived; caption delivery is not asserted.")
            }
            Self::Program => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct JournalTake {
    pub journal_id: String,
    pub journal_sequence: u64,
    pub channel_id: String,
    pub started_at_unix_ms: i64,
    pub ended_at_unix_ms: i64,
    pub state: AsRunState,
    pub content_id: Option<String>,
    pub franchise_id: Option<String>,
    pub category: Option<String>,
    pub title: Option<String>,
    pub catalog_revision: Option<u64>,
    pub schedule_revision: Option<u64>,
    pub journal_hash: String,
    pub unavailable_reason: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProjectionInput {
    pub channel_id: String,
    pub station_timezone: String,
    pub takes: Vec<JournalTake>,
}

impl ProjectionInput {
    pub fn new(
        channel_id: &str,
        station_timezone: &str,
        takes: Vec<JournalTake>,
    ) -> Result<Self, String> {
        let value = Self {
            channel_id: channel_id.into(),
            station_timezone: station_timezone.into(),
            takes,
        };
        value.validate()?;
        Ok(value)
    }
    fn validate(&self) -> Result<(), String> {
        if !uuid(&self.channel_id)
            || self.station_timezone.trim().is_empty()
            || self.station_timezone.len() > 80
            || self.takes.is_empty()
            || self.takes.len() > MAX_RUNTIME_ENTRIES
        {
            return Err("as-run projection input is invalid or unbounded".into());
        }
        let mut previous: Option<&JournalTake> = None;
        for take in &self.takes {
            if !uuid(&take.journal_id)
                || !uuid(&take.channel_id)
                || take.channel_id != self.channel_id
                || take.journal_sequence == 0
                || take.started_at_unix_ms < 0
                || take.ended_at_unix_ms <= take.started_at_unix_ms
                || !sha256(&take.journal_hash)
                || !metadata_valid(take)
            {
                return Err("journal take has an invalid authority binding".into());
            }
            if let Some(prior) = previous
                && (take.journal_sequence <= prior.journal_sequence
                    || take.started_at_unix_ms != prior.ended_at_unix_ms)
            {
                return Err(
                    "journal projection is noncontiguous; explicit gap records are required".into(),
                );
            }
            previous = Some(take);
        }
        Ok(())
    }
}

fn metadata_valid(take: &JournalTake) -> bool {
    let valid = [
        &take.content_id,
        &take.franchise_id,
        &take.category,
        &take.title,
    ]
    .into_iter()
    .all(|field| {
        field
            .as_deref()
            .is_none_or(|value| bounded(value, MAX_FIELD_BYTES))
    });
    if !valid
        || take
            .unavailable_reason
            .as_deref()
            .is_some_and(|value| !bounded(value, MAX_UNAVAILABLE_REASON_BYTES))
    {
        return false;
    }
    if !matches!(take.state, AsRunState::Program) {
        return take.content_id.is_none()
            && take.franchise_id.is_none()
            && take.category.is_none()
            && take.title.is_none()
            && take.catalog_revision.is_none()
            && take.schedule_revision.is_none()
            && take
                .unavailable_reason
                .as_deref()
                .is_some_and(|value| !value.trim().is_empty());
    }
    if take.catalog_revision == Some(0)
        || take.schedule_revision == Some(0)
        || take.category.as_deref().is_some_and(|value| {
            !matches!(
                value,
                "program" | "news" | "public_affairs" | "underwriting" | "interstitial"
            )
        })
    {
        return false;
    }
    let incomplete = take.content_id.is_none()
        || take.franchise_id.is_none()
        || take.category.is_none()
        || take.title.is_none()
        || take.catalog_revision.is_none()
        || take.schedule_revision.is_none();
    !incomplete
        || take
            .unavailable_reason
            .as_deref()
            .is_some_and(|value| !value.trim().is_empty())
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProjectedEntry {
    pub event_id: String,
    pub journal_sequence: u64,
    pub started_at_unix_ms: i64,
    pub ended_at_unix_ms: i64,
    pub state: AsRunState,
    pub content_id: Option<String>,
    pub franchise_id: Option<String>,
    pub category: Option<String>,
    pub title: Option<String>,
    pub catalog_revision: Option<u64>,
    pub schedule_revision: Option<u64>,
    pub journal_hash: String,
    pub unavailable_reason: Option<String>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProjectionCheckpoint {
    pub channel_id: String,
    pub journal_id: String,
    pub journal_sequence: u64,
    pub journal_hash: String,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AsRunProjectionRuntime {
    pub channel_id: String,
    pub station_timezone: String,
    pub entries: Vec<ProjectedEntry>,
    pub checkpoint: ProjectionCheckpoint,
}
#[derive(Debug, Default)]
pub struct ReportingRuntime {
    projections: BTreeMap<String, AsRunProjectionRuntime>,
}

impl ReportingRuntime {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn project(&mut self, input: ProjectionInput) -> Result<AsRunProjectionRuntime, String> {
        input.validate()?;
        if !self.projections.contains_key(&input.channel_id)
            && self.projections.len() >= MAX_RUNTIME_CHANNELS
        {
            return Err("as-run runtime channel capacity exceeded".into());
        }
        let entries: Vec<_> = input
            .takes
            .iter()
            .map(|take| ProjectedEntry {
                event_id: event_id(take),
                journal_sequence: take.journal_sequence,
                started_at_unix_ms: take.started_at_unix_ms,
                ended_at_unix_ms: take.ended_at_unix_ms,
                state: take.state,
                content_id: take.content_id.clone(),
                franchise_id: take.franchise_id.clone(),
                category: take.category.clone(),
                title: take.title.clone(),
                catalog_revision: take.catalog_revision,
                schedule_revision: take.schedule_revision,
                journal_hash: take.journal_hash.clone(),
                unavailable_reason: take
                    .unavailable_reason
                    .clone()
                    .or_else(|| take.state.unavailable_reason().map(str::to_owned)),
            })
            .collect();
        let last = input.takes.last().expect("validated non-empty");
        let projected = AsRunProjectionRuntime {
            channel_id: input.channel_id.clone(),
            station_timezone: input.station_timezone,
            entries,
            checkpoint: ProjectionCheckpoint {
                channel_id: input.channel_id.clone(),
                journal_id: last.journal_id.clone(),
                journal_sequence: last.journal_sequence,
                journal_hash: last.journal_hash.clone(),
            },
        };
        if let Some(previous) = self.projections.get(&input.channel_id)
            && previous.checkpoint.journal_sequence > projected.checkpoint.journal_sequence
        {
            return Err("projection checkpoint cannot move backwards".into());
        }
        self.projections.insert(input.channel_id, projected.clone());
        Ok(projected)
    }
}

pub fn render_csv(projection: &AsRunProjectionRuntime) -> Result<String, String> {
    validate_renderable(projection)?;
    let mut out = String::from(
        "journal_sequence,started_at_utc,ended_at_utc,state,title,content_id,franchise_id,category,availability\n",
    );
    for entry in &projection.entries {
        out.push_str(&format!(
            "{},{},{},{},{},{},{},{},{}\n",
            entry.journal_sequence,
            csv(&utc(entry.started_at_unix_ms))?,
            csv(&utc(entry.ended_at_unix_ms))?,
            csv(entry.state.as_str())?,
            csv(entry.title.as_deref().unwrap_or(""))?,
            csv(entry.content_id.as_deref().unwrap_or(""))?,
            csv(entry.franchise_id.as_deref().unwrap_or(""))?,
            csv(entry.category.as_deref().unwrap_or(""))?,
            csv(entry
                .unavailable_reason
                .as_deref()
                .unwrap_or("authoritative journal-derived as-run fact"))?
        ));
        if out.len() > MAX_RENDERED_EXPORT_BYTES {
            return Err("export rendered byte limit exceeded".into());
        }
    }
    Ok(out)
}
pub fn render_xmltv(
    projection: &AsRunProjectionRuntime,
    service_name: &str,
) -> Result<String, String> {
    validate_renderable(projection)?;
    if !bounded(service_name, 200) {
        return Err("XMLTV export input is invalid or unbounded".into());
    }
    let mut out = format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?><tv><channel id=\"{}\"><display-name>{}</display-name></channel>",
        xml(&projection.channel_id)?,
        xml(service_name)?
    );
    for entry in &projection.entries {
        out.push_str(&format!("<programme channel=\"{}\" start=\"{}\" stop=\"{}\"><title>{}</title><desc>{}</desc></programme>", xml(&projection.channel_id)?, xmltv_time(entry.started_at_unix_ms), xmltv_time(entry.ended_at_unix_ms), xml(entry.title.as_deref().unwrap_or("Unavailable"))?, xml(entry.unavailable_reason.as_deref().unwrap_or("Authoritative journal-derived as-run fact; delivery and field proof unavailable."))?));
        if out.len() > MAX_RENDERED_EXPORT_BYTES {
            return Err("export rendered byte limit exceeded".into());
        }
    }
    out.push_str("</tv>");
    Ok(out)
}
fn event_id(t: &JournalTake) -> String {
    let mut h = Sha256::new();
    h.update(t.journal_id.as_bytes());
    h.update(t.journal_sequence.to_le_bytes());
    h.update(t.journal_hash.as_bytes());
    let mut d = h.finalize();
    d[6] = (d[6] & 0x0f) | 0x40;
    d[8] = (d[8] & 0x3f) | 0x80;
    format!(
        "{:08x}-{:04x}-{:04x}-{:04x}-{:012x}",
        u32::from_be_bytes(d[0..4].try_into().unwrap()),
        u16::from_be_bytes(d[4..6].try_into().unwrap()),
        u16::from_be_bytes(d[6..8].try_into().unwrap()),
        u16::from_be_bytes(d[8..10].try_into().unwrap()),
        u64::from_be_bytes(d[10..18].try_into().unwrap()) & 0xffffffffffff
    )
}
fn uuid(v: &str) -> bool {
    v.len() == 36
        && v.as_bytes().iter().enumerate().all(|(i, b)| {
            if matches!(i, 8 | 13 | 18 | 23) {
                *b == b'-'
            } else if i == 14 {
                *b == b'4'
            } else if i == 19 {
                matches!(*b, b'8' | b'9' | b'a' | b'b')
            } else {
                b.is_ascii_digit() || matches!(*b, b'a'..=b'f')
            }
        })
}
fn sha256(v: &str) -> bool {
    v.len() == 64 && v.as_bytes().iter().all(u8::is_ascii_hexdigit)
}
fn csv(v: &str) -> Result<String, String> {
    if v.chars().any(char::is_control) {
        return Err("CSV fields cannot contain control characters".into());
    }
    let protected = if v.starts_with(['=', '+', '-', '@']) {
        format!("'{v}")
    } else {
        v.to_owned()
    };
    Ok(format!("\"{}\"", protected.replace('"', "\"\"")))
}
fn xml(v: &str) -> Result<String, String> {
    if v.chars().any(char::is_control) {
        return Err("XML fields cannot contain control characters".into());
    }
    Ok(v.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;"))
}
fn bounded(value: &str, max: usize) -> bool {
    !value.trim().is_empty() && value.len() <= max && !value.chars().any(char::is_control)
}
fn validate_renderable(projection: &AsRunProjectionRuntime) -> Result<(), String> {
    if projection.entries.len() > MAX_EXPORT_ENTRIES {
        return Err("export row count is unbounded".into());
    }
    if projection
        .entries
        .iter()
        .any(|entry| entry.started_at_unix_ms < 0 || entry.ended_at_unix_ms < 0)
    {
        return Err("export contains invalid timestamp".into());
    }
    Ok(())
}
/// Converts a non-negative Unix millisecond instant without consulting the host
/// clock or locale.  Export timestamps are always exact UTC; the configured
/// station zone stays with the projection for operator presentation.
fn utc(ms: i64) -> String {
    let (year, month, day, hour, minute, second, millis) = civil_utc(ms);
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}.{millis:03}Z")
}
fn xmltv_time(ms: i64) -> String {
    let (year, month, day, hour, minute, second, _) = civil_utc(ms);
    format!("{year:04}{month:02}{day:02}{hour:02}{minute:02}{second:02} +0000")
}
fn civil_utc(ms: i64) -> (i64, i64, i64, i64, i64, i64, i64) {
    let seconds = ms / 1_000;
    let millis = ms % 1_000;
    let days = seconds / 86_400;
    let daytime = seconds % 86_400;
    // Howard Hinnant's proleptic Gregorian civil-date conversion.
    let z = days + 719_468;
    let era = z / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let mut year = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = mp + if mp < 10 { 3 } else { -9 };
    year += if month <= 2 { 1 } else { 0 };
    (
        year,
        month,
        day,
        daytime / 3_600,
        (daytime % 3_600) / 60,
        daytime % 60,
        millis,
    )
}
