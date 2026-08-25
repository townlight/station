use jiff::{civil::DateTime, tz::TimeZone};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

const MAX_TERMS: usize = 8;
const MAX_TERM_LEN: usize = 64;
const MAX_LABEL_LEN: usize = 80;
const MAX_WINDOW_MS: i64 = 14 * 86_400_000;
const MAX_SLOTS: usize = 64;
const HASH_SCHEMA_VERSION: u16 = 1;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SavedSearch {
    pub search_id: String,
    pub channel_id: String,
    pub owner_operator_id: String,
    pub terms: Vec<String>,
    pub revision: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DaypartBlock {
    pub block_id: String,
    pub channel_id: String,
    pub label: String,
    pub starts_local_minute: u16,
    pub ends_local_minute: u16,
    pub revision: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum DstApplication {
    Standard,
    EarlierFold,
    LaterFold,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum LocalResolution {
    Normal,
    EarlierFold,
    LaterFold,
    Gap,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum RecurrenceTimeZone {
    Utc,
    Named {
        iana_name: String,
        dst: DstApplication,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct LocalDate {
    pub year: u16,
    pub month: u8,
    pub day: u8,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RecurringRule {
    pub rule_id: String,
    pub channel_id: String,
    pub saved_search_id: String,
    pub saved_search_revision: u64,
    pub asset_id: String,
    pub asset_revision: u64,
    pub block_id: String,
    pub block_revision: u64,
    pub weekday_mask: u8,
    pub interval_weeks: u8,
    pub anchor_date: LocalDate,
    pub timezone: RecurrenceTimeZone,
    pub local_start_minute: u16,
    pub duration_minutes: u16,
    pub revision: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MaterializationWindow {
    pub starts_at_utc_ms: i64,
    pub ends_at_utc_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedSlot {
    local_date: LocalDate,
    local_minute: u16,
    iana_zone: String,
    utc_offset_seconds: i32,
    starts_at_utc_ms: i64,
    ends_at_utc_ms: i64,
    resolution: LocalResolution,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MaterializationIntent {
    pub hash_schema_version: u16,
    pub materialization_id: String,
    pub rule_id: String,
    pub rule_revision: u64,
    pub channel_id: String,
    pub asset_id: String,
    pub asset_revision: u64,
    pub block_id: String,
    pub block_revision: u64,
    pub window: MaterializationWindow,
    pub slots: Vec<ResolvedSlot>,
    pub source_authority_sha256: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MaterializationAdmission {
    Create(Box<MaterializationIntent>),
    AlreadyPresent,
}

impl SavedSearch {
    pub fn validate(&self) -> Result<(), String> {
        ids(&[&self.search_id, &self.channel_id, &self.owner_operator_id])?;
        if self.revision == 0
            || self.terms.is_empty()
            || self.terms.len() > MAX_TERMS
            || self.terms.iter().any(|term| !safe_term(term))
            || self.terms.windows(2).any(|pair| pair[0] >= pair[1])
        {
            return Err("saved search authority is unbounded, unordered, or malformed".into());
        }
        Ok(())
    }
    pub fn validate_successor_of(&self, current: &Self, expected: u64) -> Result<(), String> {
        self.validate()?;
        current.validate()?;
        if expected != current.revision
            || self.revision
                != current
                    .revision
                    .checked_add(1)
                    .ok_or("search revision overflow")?
            || self.search_id != current.search_id
            || self.channel_id != current.channel_id
            || self.owner_operator_id != current.owner_operator_id
        {
            return Err("saved search CAS or identity rebinding failed".into());
        }
        Ok(())
    }
}

impl DaypartBlock {
    pub fn validate(&self) -> Result<(), String> {
        ids(&[&self.block_id, &self.channel_id])?;
        if self.revision == 0
            || !safe_label(&self.label)
            || self.starts_local_minute >= self.ends_local_minute
            || self.ends_local_minute > 1_440
        {
            return Err("daypart authority is malformed or unbounded".into());
        }
        Ok(())
    }
    pub fn validate_successor_of(&self, current: &Self, expected: u64) -> Result<(), String> {
        self.validate()?;
        current.validate()?;
        if expected != current.revision
            || self.revision
                != current
                    .revision
                    .checked_add(1)
                    .ok_or("block revision overflow")?
            || self.block_id != current.block_id
            || self.channel_id != current.channel_id
        {
            return Err("daypart CAS or identity rebinding failed".into());
        }
        Ok(())
    }
}

impl RecurringRule {
    pub fn validate_against(
        &self,
        search: &SavedSearch,
        block: &DaypartBlock,
    ) -> Result<(), String> {
        self.validate()?;
        search.validate()?;
        block.validate()?;
        if self.channel_id != search.channel_id
            || self.channel_id != block.channel_id
            || self.saved_search_id != search.search_id
            || self.saved_search_revision != search.revision
            || self.block_id != block.block_id
            || self.block_revision != block.revision
            || self.local_start_minute < block.starts_local_minute
            || self
                .local_start_minute
                .checked_add(self.duration_minutes)
                .ok_or("rule duration overflow")?
                > block.ends_local_minute
        {
            return Err(
                "recurring rule search, channel, asset, or block authority is stale or rebound"
                    .into(),
            );
        }
        Ok(())
    }
    fn validate(&self) -> Result<(), String> {
        ids(&[
            &self.rule_id,
            &self.channel_id,
            &self.saved_search_id,
            &self.asset_id,
            &self.block_id,
        ])?;
        self.anchor_date.validate()?;
        validate_timezone(&self.timezone)?;
        if self.revision == 0
            || self.saved_search_revision == 0
            || self.asset_revision == 0
            || self.block_revision == 0
            || self.weekday_mask == 0
            || self.weekday_mask & !0b0111_1111 != 0
            || !(1..=4).contains(&self.interval_weeks)
            || self.local_start_minute >= 1_440
            || self.duration_minutes == 0
            || self.duration_minutes > 360
        {
            return Err("recurring rule recurrence is unbounded or malformed".into());
        }
        Ok(())
    }
    pub fn validate_successor_of(
        &self,
        current: &Self,
        expected: u64,
        search: &SavedSearch,
        block: &DaypartBlock,
    ) -> Result<(), String> {
        self.validate_against(search, block)?;
        current.validate()?;
        if expected != current.revision
            || self.revision
                != current
                    .revision
                    .checked_add(1)
                    .ok_or("rule revision overflow")?
            || self.rule_id != current.rule_id
            || self.channel_id != current.channel_id
            || self.asset_id != current.asset_id
        {
            return Err("recurring rule CAS or channel/asset rebinding failed".into());
        }
        Ok(())
    }
}

pub fn derive_materialization_intent(
    search: &SavedSearch,
    block: &DaypartBlock,
    rule: &RecurringRule,
    window: MaterializationWindow,
) -> Result<MaterializationIntent, String> {
    rule.validate_against(search, block)?;
    window.validate()?;
    let slots = resolve_slots(rule, &window)?;
    validate_slots(rule, &window, &slots)?;
    let source_authority_sha256 = authority_hash(search, block, rule);
    let mut intent = MaterializationIntent {
        hash_schema_version: HASH_SCHEMA_VERSION,
        materialization_id: String::new(),
        rule_id: rule.rule_id.clone(),
        rule_revision: rule.revision,
        channel_id: rule.channel_id.clone(),
        asset_id: rule.asset_id.clone(),
        asset_revision: rule.asset_revision,
        block_id: block.block_id.clone(),
        block_revision: block.revision,
        window,
        slots,
        source_authority_sha256,
    };
    intent.materialization_id = intent_hash(&intent);
    intent.validate_against(search, block, rule)?;
    Ok(intent)
}

pub fn admit_materialization(
    existing: &[MaterializationIntent],
    candidate: MaterializationIntent,
    search: &SavedSearch,
    block: &DaypartBlock,
    rule: &RecurringRule,
) -> Result<MaterializationAdmission, String> {
    if existing.len() > MAX_SLOTS {
        return Err("materialization ledger is unbounded".into());
    }
    for (index, prior) in existing.iter().enumerate() {
        prior.validate_against(search, block, rule)?;
        if existing[..index]
            .iter()
            .any(|older| older.materialization_id == prior.materialization_id)
        {
            return Err("duplicate materialization identity is not appendable".into());
        }
    }
    candidate.validate_against(search, block, rule)?;
    if let Some(prior) = existing
        .iter()
        .find(|prior| prior.materialization_id == candidate.materialization_id)
    {
        return if prior == &candidate {
            Ok(MaterializationAdmission::AlreadyPresent)
        } else {
            Err("materialization identity collision is forged".into())
        };
    }
    Ok(MaterializationAdmission::Create(Box::new(candidate)))
}

impl MaterializationWindow {
    fn validate(&self) -> Result<(), String> {
        if self.starts_at_utc_ms < 0
            || self.ends_at_utc_ms <= self.starts_at_utc_ms
            || self.ends_at_utc_ms - self.starts_at_utc_ms > MAX_WINDOW_MS
        {
            Err("materialization UTC window is ambiguous or unbounded".into())
        } else {
            Ok(())
        }
    }
}

impl MaterializationIntent {
    pub fn resolved_slots(&self) -> Vec<(i64, i32, LocalResolution)> {
        self.slots
            .iter()
            .map(|slot| {
                (
                    slot.starts_at_utc_ms,
                    slot.utc_offset_seconds,
                    slot.resolution,
                )
            })
            .collect()
    }
    pub fn validate_against(
        &self,
        search: &SavedSearch,
        block: &DaypartBlock,
        rule: &RecurringRule,
    ) -> Result<(), String> {
        self.validate()?;
        rule.validate_against(search, block)?;
        if self.rule_id != rule.rule_id
            || self.rule_revision != rule.revision
            || self.channel_id != rule.channel_id
            || self.asset_id != rule.asset_id
            || self.asset_revision != rule.asset_revision
            || self.block_id != block.block_id
            || self.block_revision != block.revision
            || self.source_authority_sha256 != authority_hash(search, block, rule)
        {
            return Err("materialization source authority is stale or rebound".into());
        }
        validate_slots(rule, &self.window, &self.slots)
    }
    fn validate(&self) -> Result<(), String> {
        ids(&[
            &self.rule_id,
            &self.channel_id,
            &self.asset_id,
            &self.block_id,
        ])?;
        self.window.validate()?;
        if self.rule_revision == 0
            || self.asset_revision == 0
            || self.block_revision == 0
            || self.slots.is_empty()
            || self.hash_schema_version != HASH_SCHEMA_VERSION
            || self.slots.len() > MAX_SLOTS
            || !sha(&self.source_authority_sha256)
            || self.materialization_id != intent_hash(self)
        {
            return Err("materialization authority is forged or unbounded".into());
        }
        for (index, slot) in self.slots.iter().enumerate() {
            slot.local_date.validate()?;
            if slot.starts_at_utc_ms < self.window.starts_at_utc_ms
                || slot.ends_at_utc_ms > self.window.ends_at_utc_ms
                || slot.ends_at_utc_ms <= slot.starts_at_utc_ms
                || (index > 0 && self.slots[index - 1].starts_at_utc_ms >= slot.starts_at_utc_ms)
            {
                return Err("materialization slot shape is invalid or unordered".into());
            }
        }
        Ok(())
    }
}

fn resolve_slots(
    rule: &RecurringRule,
    window: &MaterializationWindow,
) -> Result<Vec<ResolvedSlot>, String> {
    let (zone_name, zone) = match &rule.timezone {
        RecurrenceTimeZone::Utc => ("UTC", TimeZone::UTC),
        RecurrenceTimeZone::Named { iana_name, .. } => (
            iana_name.as_str(),
            TimeZone::get(iana_name).map_err(|error| format!("timezone lookup failed: {error}"))?,
        ),
    };
    let mut date = rule.anchor_date.clone();
    let mut slots = Vec::new();
    while date.year <= 2100 {
        if rule.weekday_mask & (1 << date.weekday()) != 0
            && ((date.days_since_epoch() - rule.anchor_date.days_since_epoch()) / 7)
                % i64::from(rule.interval_weeks)
                == 0
        {
            let civil: DateTime = format!(
                "{:04}-{:02}-{:02}T{:02}:{:02}:00",
                date.year,
                date.month,
                date.day,
                rule.local_start_minute / 60,
                rule.local_start_minute % 60
            )
            .parse()
            .map_err(|error| format!("local recurrence datetime failed: {error}"))?;
            let normal = zone.to_ambiguous_zoned(civil).unambiguous();
            let (zoned, resolution) = if let Ok(value) = normal {
                (value, LocalResolution::Normal)
            } else {
                let earlier = zone
                    .to_ambiguous_zoned(civil)
                    .earlier()
                    .map_err(|error| format!("earlier fold resolution failed: {error}"))?;
                let later = zone
                    .to_ambiguous_zoned(civil)
                    .later()
                    .map_err(|error| format!("later fold resolution failed: {error}"))?;
                if earlier.datetime() != civil || later.datetime() != civil {
                    let near_window = [
                        earlier.timestamp().as_millisecond(),
                        later.timestamp().as_millisecond(),
                    ]
                    .iter()
                    .any(|instant| {
                        *instant >= window.starts_at_utc_ms && *instant < window.ends_at_utc_ms
                    });
                    if near_window {
                        return Err(
                            "nonexistent local recurrence time falls in materialization window"
                                .into(),
                        );
                    }
                    date = date.next_day()?;
                    continue;
                }
                match &rule.timezone {
                    RecurrenceTimeZone::Named {
                        dst: DstApplication::EarlierFold,
                        ..
                    } => (earlier, LocalResolution::EarlierFold),
                    RecurrenceTimeZone::Named {
                        dst: DstApplication::LaterFold,
                        ..
                    } => (later, LocalResolution::LaterFold),
                    _ => return Err("ambiguous local recurrence lacks explicit fold policy".into()),
                }
            };
            let starts_at_utc_ms = zoned.timestamp().as_millisecond();
            if starts_at_utc_ms >= window.ends_at_utc_ms
                && date.days_since_epoch() > rule.anchor_date.days_since_epoch()
            {
                break;
            }
            if starts_at_utc_ms >= window.starts_at_utc_ms
                && starts_at_utc_ms < window.ends_at_utc_ms
            {
                slots.push(ResolvedSlot {
                    local_date: date.clone(),
                    local_minute: rule.local_start_minute,
                    iana_zone: zone_name.to_owned(),
                    utc_offset_seconds: zoned.offset().seconds(),
                    starts_at_utc_ms,
                    ends_at_utc_ms: starts_at_utc_ms + i64::from(rule.duration_minutes) * 60_000,
                    resolution,
                });
                if slots.len() > MAX_SLOTS {
                    return Err("resolved recurrence exceeds bounded slot count".into());
                }
            }
        }
        date = date.next_day()?;
    }
    Ok(slots)
}

fn validate_slots(
    rule: &RecurringRule,
    window: &MaterializationWindow,
    slots: &[ResolvedSlot],
) -> Result<(), String> {
    if slots.is_empty() || slots.len() > MAX_SLOTS {
        return Err("materialization slots are unbounded".into());
    }
    for (index, slot) in slots.iter().enumerate() {
        slot.local_date.validate()?;
        let zone_ok = match &rule.timezone {
            RecurrenceTimeZone::Utc => {
                slot.iana_zone == "UTC"
                    && slot.utc_offset_seconds == 0
                    && slot.resolution == LocalResolution::Normal
            }
            RecurrenceTimeZone::Named { iana_name, dst } => {
                slot.iana_zone == *iana_name
                    && matches!(
                        (dst, slot.resolution),
                        (_, LocalResolution::Normal)
                            | (DstApplication::EarlierFold, LocalResolution::EarlierFold)
                            | (DstApplication::LaterFold, LocalResolution::LaterFold)
                    )
            }
        };
        let local_ms =
            slot.local_date.days_since_epoch() * 86_400_000 + i64::from(slot.local_minute) * 60_000;
        if slot.starts_at_utc_ms < window.starts_at_utc_ms
            || slot.ends_at_utc_ms > window.ends_at_utc_ms
            || slot.ends_at_utc_ms - slot.starts_at_utc_ms
                != i64::from(rule.duration_minutes) * 60_000
            || slot.local_date.days_since_epoch() < rule.anchor_date.days_since_epoch()
            || slot.local_minute != rule.local_start_minute
            || !(-50_400..=50_400).contains(&slot.utc_offset_seconds)
            || slot.starts_at_utc_ms != local_ms - i64::from(slot.utc_offset_seconds) * 1_000
            || !zone_ok
            || slot.resolution == LocalResolution::Gap
            || rule.weekday_mask & (1 << slot.local_date.weekday()) == 0
            || ((slot.local_date.days_since_epoch() - rule.anchor_date.days_since_epoch()) / 7)
                % i64::from(rule.interval_weeks)
                != 0
            || (index > 0
                && (slots[index - 1].starts_at_utc_ms >= slot.starts_at_utc_ms
                    || slots[index - 1].local_date >= slot.local_date))
        {
            return Err("materialization slot does not deterministically match bounded recurrence authority".into());
        }
    }
    Ok(())
}

impl LocalDate {
    fn validate(&self) -> Result<(), String> {
        if !(1970..=2100).contains(&self.year)
            || self.month == 0
            || self.month > 12
            || self.day == 0
            || self.day > month_days(self.year, self.month)
        {
            Err("local recurrence date is invalid".into())
        } else {
            Ok(())
        }
    }
    fn days_since_epoch(&self) -> i64 {
        let years = (1970..self.year)
            .map(|year| if leap(year) { 366 } else { 365 })
            .sum::<i64>();
        years
            + (1..self.month)
                .map(|month| i64::from(month_days(self.year, month)))
                .sum::<i64>()
            + i64::from(self.day - 1)
    }
    fn weekday(&self) -> u8 {
        ((3 + self.days_since_epoch()) % 7) as u8
    }
    fn next_day(&self) -> Result<Self, String> {
        self.validate()?;
        let mut next = self.clone();
        if next.day < month_days(next.year, next.month) {
            next.day += 1;
        } else if next.month < 12 {
            next.month += 1;
            next.day = 1;
        } else {
            next.year = next.year.checked_add(1).ok_or("local date overflow")?;
            next.month = 1;
            next.day = 1;
        }
        Ok(next)
    }
}

fn validate_timezone(value: &RecurrenceTimeZone) -> Result<(), String> {
    match value {
        RecurrenceTimeZone::Utc => Ok(()),
        RecurrenceTimeZone::Named {
            iana_name,
            dst: DstApplication::EarlierFold | DstApplication::LaterFold,
        } if matches!(
            iana_name.as_str(),
            "America/Denver" | "America/Chicago" | "America/New_York" | "America/Los_Angeles"
        ) =>
        {
            Ok(())
        }
        _ => Err("recurrence timezone or DST fold policy is ambiguous".into()),
    }
}
fn month_days(year: u16, month: u8) -> u8 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if leap(year) => 29,
        _ => 28,
    }
}
fn leap(year: u16) -> bool {
    year.is_multiple_of(4) && (!year.is_multiple_of(100) || year.is_multiple_of(400))
}
fn safe_term(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= MAX_TERM_LEN
        && value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b' ' | b'-')
        })
}
fn safe_label(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= MAX_LABEL_LEN
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b' ' | b'-'))
}
fn ids(values: &[&str]) -> Result<(), String> {
    for value in values {
        if !uuid(value) {
            return Err("schedule automation identity must be a UUID".into());
        }
    }
    Ok(())
}
fn uuid(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 36
        && [8, 13, 18, 23].iter().all(|index| bytes[*index] == b'-')
        && bytes
            .iter()
            .enumerate()
            .all(|(index, byte)| [8, 13, 18, 23].contains(&index) || byte.is_ascii_hexdigit())
}
fn sha(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}
trait CanonicalSink {
    fn write(&mut self, bytes: impl AsRef<[u8]>);
}
impl CanonicalSink for Sha256 {
    fn write(&mut self, bytes: impl AsRef<[u8]>) {
        Digest::update(self, bytes);
    }
}
impl CanonicalSink for Vec<u8> {
    fn write(&mut self, bytes: impl AsRef<[u8]>) {
        self.extend_from_slice(bytes.as_ref());
    }
}
fn put(sink: &mut impl CanonicalSink, value: &str) {
    sink.write((value.len() as u64).to_le_bytes());
    sink.write(value.as_bytes());
}
fn authority_hash(search: &SavedSearch, block: &DaypartBlock, rule: &RecurringRule) -> String {
    let mut digest = Sha256::new();
    encode_source(&mut digest, search, block, rule);
    format!("{:x}", digest.finalize())
}
pub fn canonical_source_bytes(
    search: &SavedSearch,
    block: &DaypartBlock,
    rule: &RecurringRule,
) -> Result<Vec<u8>, String> {
    rule.validate_against(search, block)?;
    let mut bytes = Vec::new();
    encode_source(&mut bytes, search, block, rule);
    Ok(bytes)
}
fn encode_source(
    digest: &mut impl CanonicalSink,
    search: &SavedSearch,
    block: &DaypartBlock,
    rule: &RecurringRule,
) {
    put(digest, "townlight.schedule-source.v1");
    digest.write(HASH_SCHEMA_VERSION.to_le_bytes());
    for value in [
        &search.search_id,
        &search.channel_id,
        &search.owner_operator_id,
    ] {
        put(digest, value);
    }
    digest.write(search.revision.to_le_bytes());
    digest.write((search.terms.len() as u32).to_le_bytes());
    for term in &search.terms {
        put(digest, term);
    }
    for value in [&block.block_id, &block.channel_id, &block.label] {
        put(digest, value);
    }
    digest.write(block.starts_local_minute.to_le_bytes());
    digest.write(block.ends_local_minute.to_le_bytes());
    digest.write(block.revision.to_le_bytes());
    for value in [
        &rule.rule_id,
        &rule.channel_id,
        &rule.saved_search_id,
        &rule.asset_id,
        &rule.block_id,
    ] {
        put(digest, value);
    }
    digest.write(rule.saved_search_revision.to_le_bytes());
    digest.write(rule.asset_revision.to_le_bytes());
    digest.write(rule.block_revision.to_le_bytes());
    digest.write([rule.weekday_mask, rule.interval_weeks]);
    digest.write(rule.anchor_date.year.to_le_bytes());
    digest.write([rule.anchor_date.month, rule.anchor_date.day]);
    match &rule.timezone {
        RecurrenceTimeZone::Utc => put(digest, "utc"),
        RecurrenceTimeZone::Named { iana_name, dst } => {
            put(digest, "named");
            put(digest, iana_name);
            put(
                digest,
                match dst {
                    DstApplication::EarlierFold => "earlier",
                    DstApplication::LaterFold => "later",
                    DstApplication::Standard => "invalid",
                },
            );
        }
    }
    digest.write(rule.local_start_minute.to_le_bytes());
    digest.write(rule.duration_minutes.to_le_bytes());
    digest.write(rule.revision.to_le_bytes());
}
fn intent_hash(intent: &MaterializationIntent) -> String {
    let mut digest = Sha256::new();
    encode_intent(&mut digest, intent);
    format!("{:x}", digest.finalize())
}
pub fn canonical_intent_bytes(intent: &MaterializationIntent) -> Vec<u8> {
    let mut bytes = Vec::new();
    encode_intent(&mut bytes, intent);
    bytes
}
fn encode_intent(digest: &mut impl CanonicalSink, intent: &MaterializationIntent) {
    put(digest, "townlight.materialization-intent.v1");
    digest.write(intent.hash_schema_version.to_le_bytes());
    for value in [
        &intent.rule_id,
        &intent.channel_id,
        &intent.asset_id,
        &intent.block_id,
    ] {
        put(digest, value);
    }
    digest.write(intent.rule_revision.to_le_bytes());
    digest.write(intent.asset_revision.to_le_bytes());
    digest.write(intent.block_revision.to_le_bytes());
    digest.write(intent.window.starts_at_utc_ms.to_le_bytes());
    digest.write(intent.window.ends_at_utc_ms.to_le_bytes());
    put(digest, &intent.source_authority_sha256);
    digest.write((intent.slots.len() as u32).to_le_bytes());
    for slot in &intent.slots {
        digest.write(slot.local_date.year.to_le_bytes());
        digest.write([slot.local_date.month, slot.local_date.day]);
        digest.write(slot.local_minute.to_le_bytes());
        put(digest, &slot.iana_zone);
        digest.write(slot.utc_offset_seconds.to_le_bytes());
        digest.write(slot.starts_at_utc_ms.to_le_bytes());
        digest.write(slot.ends_at_utc_ms.to_le_bytes());
        digest.write([match slot.resolution {
            LocalResolution::Normal => 0,
            LocalResolution::EarlierFold => 1,
            LocalResolution::LaterFold => 2,
            LocalResolution::Gap => 3,
        }]);
    }
}
