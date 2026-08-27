use super::{
    SQLITE_DONE, SQLITE_ROW, StationStore, bind_i64, bind_text, c_string, checked_i64, column_text,
    expect_done, sqlite_error,
};
use station_agenda::{
    Agenda, AgendaProvenance, AgendaWriteValidationError, validate_agenda_write_typed,
};

const MAX_HISTORY: usize = 128;
const MAX_AGENDAS: i64 = 256;

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct AgendaHistoryEntry {
    pub agenda: Agenda,
    pub changed_by: String,
    pub occurred_at_unix_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AgendaWriteError {
    Invalid(String),
    Storage(String),
    Conflict,
    Rebound,
    Capacity,
}

impl std::fmt::Display for AgendaWriteError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Invalid(s) => f.write_str(s),
            Self::Storage(s) => f.write_str(s),
            Self::Conflict => f.write_str("agenda revision compare-and-set conflict"),
            Self::Rebound => f.write_str("agenda identity or provenance cannot be rebound"),
            Self::Capacity => f.write_str("agenda capacity is bounded at 256 records"),
        }
    }
}
impl StationStore {
    pub(crate) fn initialize_agenda_schema(&self) -> Result<(), String> {
        self.execute("CREATE TABLE IF NOT EXISTS agendas(agenda_id TEXT PRIMARY KEY CHECK(length(agenda_id)=36),revision INTEGER NOT NULL CHECK(revision>=1),channel_id TEXT NOT NULL CHECK(length(channel_id)=36),meeting_id TEXT NOT NULL CHECK(length(meeting_id)=36),provenance_kind TEXT NOT NULL CHECK(provenance_kind IN ('staff_authored','text_import_claim','pdf_import_claim')),provenance_source_sha256 TEXT NOT NULL DEFAULT '',record_json TEXT NOT NULL,updated_by TEXT NOT NULL CHECK(length(updated_by)=36),updated_at_unix_ms INTEGER NOT NULL CHECK(updated_at_unix_ms>=0));CREATE TABLE IF NOT EXISTS agenda_history(agenda_id TEXT NOT NULL,revision INTEGER NOT NULL CHECK(revision>=1),channel_id TEXT NOT NULL CHECK(length(channel_id)=36),meeting_id TEXT NOT NULL CHECK(length(meeting_id)=36),provenance_kind TEXT NOT NULL CHECK(provenance_kind IN ('staff_authored','text_import_claim','pdf_import_claim')),provenance_source_sha256 TEXT NOT NULL DEFAULT '',record_json TEXT NOT NULL,changed_by TEXT NOT NULL CHECK(length(changed_by)=36),occurred_at_unix_ms INTEGER NOT NULL CHECK(occurred_at_unix_ms>=0),PRIMARY KEY(agenda_id,revision));CREATE INDEX IF NOT EXISTS agenda_history_time ON agenda_history(agenda_id,revision DESC);CREATE INDEX IF NOT EXISTS agendas_channel_time ON agendas(channel_id,meeting_id);CREATE TRIGGER IF NOT EXISTS agenda_history_no_update BEFORE UPDATE ON agenda_history BEGIN SELECT RAISE(ABORT,'agenda history is append-only'); END;CREATE TRIGGER IF NOT EXISTS agenda_history_no_delete BEFORE DELETE ON agenda_history BEGIN SELECT RAISE(ABORT,'agenda history is append-only'); END;")
    }
    pub fn read_current_agenda(&self, id: &str) -> Result<Option<Agenda>, String> {
        validate_uuid(id, "agenda identity")?;
        let s=self.prepare("SELECT revision,channel_id,meeting_id,provenance_kind,provenance_source_sha256,record_json,updated_by,updated_at_unix_ms FROM agendas WHERE agenda_id=?1")?;
        bind_text(self.database, s.raw, 1, &c_string(id, "agenda identity")?)?;
        match unsafe { super::sqlite3_step(s.raw) } {
            SQLITE_DONE => Ok(None),
            SQLITE_ROW => {
                let agenda = read_row(s.raw, id)?;
                let updated_by = column_text(s.raw, 6)?;
                validate_uuid(&updated_by, "agenda current actor")?;
                let updated_at = unsafe { super::sqlite3_column_int64(s.raw, 7) };
                if updated_at < 0 {
                    return Err("agenda current timestamp is invalid".into());
                }
                let history = self.prepare("SELECT revision,channel_id,meeting_id,provenance_kind,provenance_source_sha256,record_json,changed_by,occurred_at_unix_ms FROM agenda_history WHERE agenda_id=?1 ORDER BY revision DESC LIMIT 1")?;
                bind_text(
                    self.database,
                    history.raw,
                    1,
                    &c_string(id, "agenda identity")?,
                )?;
                if unsafe { super::sqlite3_step(history.raw) } != SQLITE_ROW {
                    return Err("agenda current has no matching immutable history row".into());
                }
                let historical = read_row(history.raw, id)?;
                let historical_actor = column_text(history.raw, 6)?;
                validate_uuid(&historical_actor, "agenda history actor")?;
                let historical_at = unsafe { super::sqlite3_column_int64(history.raw, 7) };
                if historical_at < 0
                    || historical_actor != updated_by
                    || historical_at != updated_at
                    || historical != agenda
                {
                    return Err("agenda current disagrees with latest immutable history".into());
                }

                let all_history = self.prepare(
                    "SELECT revision,channel_id,meeting_id,provenance_kind,provenance_source_sha256,record_json,changed_by,occurred_at_unix_ms FROM agenda_history WHERE agenda_id=?1 ORDER BY revision ASC",
                )?;
                bind_text(
                    self.database,
                    all_history.raw,
                    1,
                    &c_string(id, "agenda identity")?,
                )?;
                let mut expected_revision = 1_u64;
                loop {
                    match unsafe { super::sqlite3_step(all_history.raw) } {
                        SQLITE_DONE => {
                            if expected_revision != agenda.revision.saturating_add(1) {
                                return Err(
                                    "agenda immutable history is not contiguous through current revision"
                                        .into(),
                                );
                            }
                            break;
                        }
                        SQLITE_ROW => {
                            let revision = u64::try_from(unsafe {
                                super::sqlite3_column_int64(all_history.raw, 0)
                            })
                            .map_err(|_| "agenda history revision is invalid".to_string())?;
                            if revision != expected_revision {
                                return Err(
                                    "agenda immutable history is not contiguous through current revision"
                                        .into(),
                                );
                            }
                            let entry = read_row(all_history.raw, id)?;
                            let actor = column_text(all_history.raw, 6)?;
                            validate_uuid(&actor, "agenda history actor")?;
                            if unsafe { super::sqlite3_column_int64(all_history.raw, 7) } < 0 {
                                return Err("agenda history timestamp is invalid".into());
                            }
                            if entry.revision != revision {
                                return Err(
                                    "agenda history revision disagrees with record JSON".into()
                                );
                            }
                            expected_revision = expected_revision.saturating_add(1);
                        }
                        _ => return Err(sqlite_error(self.database)),
                    }
                }
                Ok(Some(agenda))
            }
            _ => Err(sqlite_error(self.database)),
        }
    }
    pub fn write_agenda(
        &self,
        expected: Option<u64>,
        agenda: &Agenda,
        actor: &str,
        at: i64,
    ) -> Result<(), AgendaWriteError> {
        validate_uuid(actor, "agenda actor identity").map_err(AgendaWriteError::Invalid)?;
        agenda.validate().map_err(AgendaWriteError::Invalid)?;
        if at < 0 {
            return Err(AgendaWriteError::Invalid(
                "agenda history time must be nonnegative".to_string(),
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(AgendaWriteError::Storage)?;
        let outcome = (|| {
            validate_sync_binding(self, agenda)?;
            let current = self
                .read_current_agenda(&agenda.agenda_id)
                .map_err(AgendaWriteError::Storage)?;
            validate_agenda_write_typed(expected, current.as_ref(), agenda)
                .map_err(map_validation_error)?;
            let json = serde_json::to_string(agenda)
                .map_err(|e| AgendaWriteError::Storage(format!("serialize agenda: {e}")))?;
            let (kind, source) = provenance_columns(&agenda.provenance);
            if let Some(expected) = expected {
                let s = self
                    .prepare("UPDATE agendas SET revision=?1,channel_id=?2,meeting_id=?3,provenance_kind=?4,provenance_source_sha256=?5,record_json=?6,updated_by=?7,updated_at_unix_ms=?8 WHERE agenda_id=?9 AND revision=?10")
                    .map_err(AgendaWriteError::Storage)?;
                bind_i64(
                    self.database,
                    s.raw,
                    1,
                    checked_i64(agenda.revision, "agenda revision")
                        .map_err(AgendaWriteError::Invalid)?,
                )
                .map_err(AgendaWriteError::Storage)?;
                bind_text(
                    self.database,
                    s.raw,
                    2,
                    &c_string(&agenda.channel_id, "agenda channel")
                        .map_err(AgendaWriteError::Invalid)?,
                )
                .map_err(AgendaWriteError::Storage)?;
                bind_text(
                    self.database,
                    s.raw,
                    3,
                    &c_string(&agenda.meeting_id, "agenda meeting")
                        .map_err(AgendaWriteError::Invalid)?,
                )
                .map_err(AgendaWriteError::Storage)?;
                bind_text(
                    self.database,
                    s.raw,
                    4,
                    &c_string(kind, "agenda provenance").map_err(AgendaWriteError::Invalid)?,
                )
                .map_err(AgendaWriteError::Storage)?;
                bind_text(
                    self.database,
                    s.raw,
                    5,
                    &c_string(source, "agenda source").map_err(AgendaWriteError::Invalid)?,
                )
                .map_err(AgendaWriteError::Storage)?;
                bind_text(
                    self.database,
                    s.raw,
                    6,
                    &c_string(&json, "agenda record").map_err(AgendaWriteError::Invalid)?,
                )
                .map_err(AgendaWriteError::Storage)?;
                bind_text(
                    self.database,
                    s.raw,
                    7,
                    &c_string(actor, "agenda actor").map_err(AgendaWriteError::Invalid)?,
                )
                .map_err(AgendaWriteError::Storage)?;
                bind_i64(self.database, s.raw, 8, at).map_err(AgendaWriteError::Storage)?;
                bind_text(
                    self.database,
                    s.raw,
                    9,
                    &c_string(&agenda.agenda_id, "agenda identity")
                        .map_err(AgendaWriteError::Invalid)?,
                )
                .map_err(AgendaWriteError::Storage)?;
                bind_i64(
                    self.database,
                    s.raw,
                    10,
                    checked_i64(expected, "expected revision")
                        .map_err(AgendaWriteError::Invalid)?,
                )
                .map_err(AgendaWriteError::Storage)?;
                expect_done(self.database, s.raw).map_err(AgendaWriteError::Storage)?;
                if self.changes() != 1 {
                    return Err(AgendaWriteError::Conflict);
                }
            } else {
                let count = self
                    .prepare("SELECT count(*) FROM agendas")
                    .map_err(AgendaWriteError::Storage)?;
                if unsafe { super::sqlite3_step(count.raw) } != SQLITE_ROW {
                    return Err(AgendaWriteError::Storage(sqlite_error(self.database)));
                }
                if unsafe { super::sqlite3_column_int64(count.raw, 0) } >= MAX_AGENDAS {
                    return Err(AgendaWriteError::Capacity);
                }
                let s = self
                    .prepare("INSERT INTO agendas(agenda_id,revision,channel_id,meeting_id,provenance_kind,provenance_source_sha256,record_json,updated_by,updated_at_unix_ms) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9)")
                    .map_err(AgendaWriteError::Storage)?;
                bind_text(
                    self.database,
                    s.raw,
                    1,
                    &c_string(&agenda.agenda_id, "agenda identity")
                        .map_err(AgendaWriteError::Invalid)?,
                )
                .map_err(AgendaWriteError::Storage)?;
                bind_i64(
                    self.database,
                    s.raw,
                    2,
                    checked_i64(agenda.revision, "agenda revision")
                        .map_err(AgendaWriteError::Invalid)?,
                )
                .map_err(AgendaWriteError::Storage)?;
                bind_text(
                    self.database,
                    s.raw,
                    3,
                    &c_string(&agenda.channel_id, "agenda channel")
                        .map_err(AgendaWriteError::Invalid)?,
                )
                .map_err(AgendaWriteError::Storage)?;
                bind_text(
                    self.database,
                    s.raw,
                    4,
                    &c_string(&agenda.meeting_id, "agenda meeting")
                        .map_err(AgendaWriteError::Invalid)?,
                )
                .map_err(AgendaWriteError::Storage)?;
                bind_text(
                    self.database,
                    s.raw,
                    5,
                    &c_string(kind, "agenda provenance").map_err(AgendaWriteError::Invalid)?,
                )
                .map_err(AgendaWriteError::Storage)?;
                bind_text(
                    self.database,
                    s.raw,
                    6,
                    &c_string(source, "agenda source").map_err(AgendaWriteError::Invalid)?,
                )
                .map_err(AgendaWriteError::Storage)?;
                bind_text(
                    self.database,
                    s.raw,
                    7,
                    &c_string(&json, "agenda record").map_err(AgendaWriteError::Invalid)?,
                )
                .map_err(AgendaWriteError::Storage)?;
                bind_text(
                    self.database,
                    s.raw,
                    8,
                    &c_string(actor, "agenda actor").map_err(AgendaWriteError::Invalid)?,
                )
                .map_err(AgendaWriteError::Storage)?;
                bind_i64(self.database, s.raw, 9, at).map_err(AgendaWriteError::Storage)?;
                expect_done(self.database, s.raw).map_err(AgendaWriteError::Storage)?;
            }
            let s = self
                .prepare("INSERT INTO agenda_history(agenda_id,revision,channel_id,meeting_id,provenance_kind,provenance_source_sha256,record_json,changed_by,occurred_at_unix_ms) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9)")
                .map_err(AgendaWriteError::Storage)?;
            bind_text(
                self.database,
                s.raw,
                1,
                &c_string(&agenda.agenda_id, "agenda identity")
                    .map_err(AgendaWriteError::Invalid)?,
            )
            .map_err(AgendaWriteError::Storage)?;
            bind_i64(
                self.database,
                s.raw,
                2,
                checked_i64(agenda.revision, "agenda revision")
                    .map_err(AgendaWriteError::Invalid)?,
            )
            .map_err(AgendaWriteError::Storage)?;
            bind_text(
                self.database,
                s.raw,
                3,
                &c_string(&agenda.channel_id, "agenda channel")
                    .map_err(AgendaWriteError::Invalid)?,
            )
            .map_err(AgendaWriteError::Storage)?;
            bind_text(
                self.database,
                s.raw,
                4,
                &c_string(&agenda.meeting_id, "agenda meeting")
                    .map_err(AgendaWriteError::Invalid)?,
            )
            .map_err(AgendaWriteError::Storage)?;
            bind_text(
                self.database,
                s.raw,
                5,
                &c_string(kind, "agenda provenance").map_err(AgendaWriteError::Invalid)?,
            )
            .map_err(AgendaWriteError::Storage)?;
            bind_text(
                self.database,
                s.raw,
                6,
                &c_string(source, "agenda source").map_err(AgendaWriteError::Invalid)?,
            )
            .map_err(AgendaWriteError::Storage)?;
            bind_text(
                self.database,
                s.raw,
                7,
                &c_string(&json, "agenda record").map_err(AgendaWriteError::Invalid)?,
            )
            .map_err(AgendaWriteError::Storage)?;
            bind_text(
                self.database,
                s.raw,
                8,
                &c_string(actor, "agenda actor").map_err(AgendaWriteError::Invalid)?,
            )
            .map_err(AgendaWriteError::Storage)?;
            bind_i64(self.database, s.raw, 9, at).map_err(AgendaWriteError::Storage)?;
            expect_done(self.database, s.raw).map_err(AgendaWriteError::Storage)?;
            Ok(())
        })();
        match outcome {
            Ok(()) => self.execute("COMMIT").map_err(AgendaWriteError::Storage),
            Err(e) => {
                let _ = self.execute("ROLLBACK");
                Err(e)
            }
        }
    }
    pub fn list_agenda_history(
        &self,
        id: &str,
        limit: usize,
    ) -> Result<Vec<AgendaHistoryEntry>, String> {
        validate_uuid(id, "agenda identity")?;
        if !(1..=MAX_HISTORY).contains(&limit) {
            return Err("agenda history limit must be between 1 and 128".into());
        }
        let s=self.prepare("SELECT revision,channel_id,meeting_id,provenance_kind,provenance_source_sha256,record_json,changed_by,occurred_at_unix_ms FROM agenda_history WHERE agenda_id=?1 ORDER BY revision DESC LIMIT ?2")?;
        bind_text(self.database, s.raw, 1, &c_string(id, "agenda identity")?)?;
        bind_i64(self.database, s.raw, 2, limit as i64)?;
        let mut out = Vec::new();
        loop {
            match unsafe { super::sqlite3_step(s.raw) } {
                SQLITE_DONE => return Ok(out),
                SQLITE_ROW => {
                    let agenda = read_row(s.raw, id)?;
                    let actor = column_text(s.raw, 6)?;
                    validate_uuid(&actor, "agenda history actor")?;
                    let at = unsafe { super::sqlite3_column_int64(s.raw, 7) };
                    if at < 0 {
                        return Err("agenda history timestamp is invalid".into());
                    }
                    out.push(AgendaHistoryEntry {
                        agenda,
                        changed_by: actor,
                        occurred_at_unix_ms: at,
                    })
                }
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }
}
fn provenance_columns(p: &AgendaProvenance) -> (&'static str, &str) {
    match p {
        AgendaProvenance::StaffAuthored { .. } => ("staff_authored", ""),
        AgendaProvenance::TextImportClaim { source_sha256, .. } => {
            ("text_import_claim", source_sha256)
        }
        AgendaProvenance::PdfImportClaim { source_sha256, .. } => {
            ("pdf_import_claim", source_sha256)
        }
    }
}

fn map_validation_error(error: AgendaWriteValidationError) -> AgendaWriteError {
    match error {
        AgendaWriteValidationError::Invalid(message) => AgendaWriteError::Invalid(message),
        AgendaWriteValidationError::Conflict => AgendaWriteError::Conflict,
        AgendaWriteValidationError::Rebound => AgendaWriteError::Rebound,
    }
}

fn validate_sync_binding(store: &StationStore, agenda: &Agenda) -> Result<(), AgendaWriteError> {
    let intent = &agenda.sync_intent;
    let schedule = store
        .read_schedule_item(&intent.schedule_item_id)
        .map_err(AgendaWriteError::Storage)?
        .ok_or_else(|| AgendaWriteError::Invalid("agenda schedule item does not exist".into()))?;
    if schedule.channel_id != agenda.channel_id
        || schedule.revision != intent.schedule_revision
        || schedule.state != super::ScheduleState::Committed
    {
        return Err(AgendaWriteError::Invalid(
            "agenda schedule binding is not the current committed revision".into(),
        ));
    }
    let media = store
        .read_media_asset(&schedule.asset_id)
        .map_err(AgendaWriteError::Storage)?
        .ok_or_else(|| AgendaWriteError::Invalid("agenda media asset does not exist".into()))?;
    if media.readiness != super::AssetReadiness::Ready
        || media.asset_id != intent.media_asset_sha256
        || i64::try_from(media.duration_ms).ok() != Some(intent.expected_media_duration_ms)
    {
        return Err(AgendaWriteError::Invalid(
            "agenda media binding is not an exact ready asset authority".into(),
        ));
    }
    Ok(())
}
fn read_row(s: *mut super::SqliteStatement, id: &str) -> Result<Agenda, String> {
    let rev = u64::try_from(unsafe { super::sqlite3_column_int64(s, 0) })
        .map_err(|_| "database returned invalid agenda revision".to_string())?;
    let channel = column_text(s, 1)?;
    let meeting = column_text(s, 2)?;
    let kind = column_text(s, 3)?;
    let source = column_text(s, 4)?;
    let json = column_text(s, 5)?;
    let agenda: Agenda = serde_json::from_str(&json)
        .map_err(|e| format!("database returned invalid agenda JSON: {e}"))?;
    if agenda.agenda_id != id
        || agenda.revision != rev
        || agenda.channel_id != channel
        || agenda.meeting_id != meeting
    {
        return Err("agenda indexed fields disagree with record JSON".into());
    }
    let (e_k, e_s) = provenance_columns(&agenda.provenance);
    if kind != e_k || source != e_s {
        return Err("agenda provenance index disagrees with record JSON".into());
    }
    agenda
        .validate()
        .map_err(|e| format!("database returned invalid agenda: {e}"))?;
    Ok(agenda)
}
fn validate_uuid(value: &str, field: &str) -> Result<(), String> {
    let b = value.as_bytes();
    if b.len() != 36
        || [8, 13, 18, 23].iter().any(|&i| b[i] != b'-')
        || b[14] != b'4'
        || !matches!(b[19], b'8'..=b'9' | b'a'..=b'b')
        || b.iter().enumerate().any(|(i, c)| {
            ![8, 13, 18, 23].contains(&i) && !(c.is_ascii_digit() || (b'a'..=b'f').contains(c))
        })
    {
        return Err(format!("{field} must be a lowercase canonical UUID"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use station_agenda::{
        AgendaActor, AgendaItem, AgendaItemVisibility, AgendaProvenance, AgendaSyncIntent,
    };
    use station_schedule::{AssetReadiness, MediaAsset, ScheduleItem, ScheduleState};
    fn id(s: &str) -> String {
        format!("00000000-0000-4000-8000-0000000000{s}")
    }
    fn fixture() -> Agenda {
        Agenda {
            agenda_id: id("01"),
            channel_id: id("02"),
            meeting_id: id("03"),
            title: "Town council meeting".into(),
            meeting_starts_at_unix_ms: 10,
            provenance: AgendaProvenance::StaffAuthored {
                author: AgendaActor { actor_id: id("04") },
                asserted_at_unix_ms: 1,
            },
            sync_intent: AgendaSyncIntent {
                channel_id: id("02"),
                schedule_item_id: id("05"),
                schedule_revision: 2,
                media_asset_sha256: "a".repeat(64),
                expected_media_duration_ms: 1000,
                requested_by: AgendaActor { actor_id: id("04") },
                requested_at_unix_ms: 2,
            },
            items: vec![AgendaItem {
                item_id: id("06"),
                ordinal: 1,
                title: "Call to order".into(),
                public_summary: Some("Opening".into()),
                staff_notes: Some("Confirm quorum".into()),
                visibility: AgendaItemVisibility::Public,
            }],
            chapters: Vec::new(),
            revision: 1,
        }
    }

    fn temp_db(name: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!("townlight-agenda-{name}-{}.db", std::process::id()))
    }

    fn write_fixture(store: &StationStore) -> Agenda {
        seed_sync_binding(store);
        let agenda = fixture();
        store
            .write_agenda(None, &agenda, &id("04"), 3)
            .expect("fixture agenda should write");
        agenda
    }

    fn seed_sync_binding(store: &StationStore) {
        let media_sha256 = "a".repeat(64);
        store
            .put_media_asset(&MediaAsset {
                asset_id: media_sha256.clone(),
                media_path: r"C:\Windows\System32\notepad.exe".into(),
                duration_ms: 1_000,
                readiness: AssetReadiness::Ready,
            })
            .unwrap();
        store
            .write_schedule_item(&crate::ScheduleItemWrite {
                changed_by: "test-harness".into(),
                occurred_at_unix_ms: 1,
                item: ScheduleItem {
                    item_id: id("05"),
                    revision: 1,
                    channel_id: id("02"),
                    asset_id: media_sha256,
                    title: "Meeting recording".into(),
                    starts_at_unix_ms: 1,
                    duration_ms: 1_000,
                    state: ScheduleState::Draft,
                },
            })
            .unwrap();
        store
            .commit_schedule(
                "agenda-report",
                "agenda-plan",
                &id("05"),
                &id("04"),
                1,
                "agenda fixture",
            )
            .unwrap();
    }

    fn capacity_agenda_id(index: i64) -> String {
        format!("{index:08x}-0000-4000-8000-000000000001")
    }
    #[test]
    fn current_and_history_round_trip_through_one_atomic_write() {
        let p = temp_db("round-trip");
        let _ = std::fs::remove_file(&p);
        let s = StationStore::open(&p).unwrap();
        let a = write_fixture(&s);
        assert_eq!(
            s.read_current_agenda(&a.agenda_id).unwrap(),
            Some(a.clone())
        );
        assert_eq!(s.list_agenda_history(&a.agenda_id, 10).unwrap().len(), 1);
        drop(s);
        let _ = std::fs::remove_file(p);
    }

    #[test]
    fn malformed_incoming_identity_is_invalid_not_storage() {
        let p = temp_db("invalid");
        let _ = std::fs::remove_file(&p);
        let store = StationStore::open(&p).unwrap();
        let mut agenda = fixture();
        agenda.agenda_id = "not-a-uuid".into();
        assert!(matches!(
            store.write_agenda(None, &agenda, &id("04"), 3),
            Err(AgendaWriteError::Invalid(message)) if message.contains("agenda identity")
        ));
        drop(store);
        let _ = std::fs::remove_file(p);
    }

    #[test]
    fn compare_and_set_and_rebinding_are_typed_boundaries() {
        let p = temp_db("cas");
        let _ = std::fs::remove_file(&p);
        let store = StationStore::open(&p).unwrap();
        let first = write_fixture(&store);
        let mut second = first.clone();
        second.revision = 2;
        second.title = "Updated".into();
        assert!(store.write_agenda(Some(1), &second, &id("04"), 4).is_ok());
        assert!(matches!(
            store.write_agenda(Some(1), &second, &id("04"), 5),
            Err(AgendaWriteError::Conflict)
        ));
        let mut rebound = second.clone();
        rebound.revision = 3;
        rebound.meeting_id = id("07");
        rebound.sync_intent.channel_id = rebound.channel_id.clone();
        assert!(matches!(
            store.write_agenda(Some(2), &rebound, &id("04"), 6),
            Err(AgendaWriteError::Rebound)
        ));
        drop(store);
        let _ = std::fs::remove_file(p);
    }

    #[test]
    fn current_read_rejects_json_index_actor_and_time_corruption() {
        for (name, sql) in [
            ("json", "UPDATE agendas SET record_json='{}'"),
            ("revision", "UPDATE agendas SET revision=2"),
            (
                "channel",
                "UPDATE agendas SET channel_id='00000000-0000-4000-8000-000000000007'",
            ),
            (
                "meeting",
                "UPDATE agendas SET meeting_id='00000000-0000-4000-8000-000000000007'",
            ),
            (
                "provenance",
                "UPDATE agendas SET provenance_kind='text_import_claim'",
            ),
            ("source", "UPDATE agendas SET provenance_source_sha256='b'"),
            (
                "actor",
                "UPDATE agendas SET updated_by='00000000-0000-4000-8000-000000000007'",
            ),
            ("time", "UPDATE agendas SET updated_at_unix_ms=99"),
        ] {
            let p = temp_db(name);
            let _ = std::fs::remove_file(&p);
            let store = StationStore::open(&p).unwrap();
            write_fixture(&store);
            store.execute(sql).unwrap();
            assert!(
                store.read_current_agenda(&id("01")).is_err(),
                "corruption {name} must fail closed"
            );
            drop(store);
            let _ = std::fs::remove_file(p);
        }
    }

    #[test]
    fn history_is_append_only_and_reopen_preserves_authority() {
        let p = temp_db("reopen");
        let _ = std::fs::remove_file(&p);
        let store = StationStore::open(&p).unwrap();
        let agenda = write_fixture(&store);
        assert!(
            store
                .execute("UPDATE agenda_history SET record_json='{}'")
                .is_err()
        );
        assert!(store.execute("DELETE FROM agenda_history").is_err());
        drop(store);
        let reopened = StationStore::open(&p).unwrap();
        assert_eq!(
            reopened.read_current_agenda(&agenda.agenda_id).unwrap(),
            Some(agenda)
        );
        drop(reopened);
        let _ = std::fs::remove_file(p);
    }

    #[test]
    fn current_read_rejects_missing_initial_or_middle_history_revision() {
        for (name, missing_revision) in [("missing-initial", 1_i64), ("missing-middle", 2)] {
            let p = temp_db(name);
            let _ = std::fs::remove_file(&p);
            let store = StationStore::open(&p).unwrap();
            let first = write_fixture(&store);
            let mut second = first.clone();
            second.revision = 2;
            second.title = "Updated once".into();
            store.write_agenda(Some(1), &second, &id("04"), 4).unwrap();
            if missing_revision == 2 {
                let mut third = second.clone();
                third.revision = 3;
                third.title = "Updated twice".into();
                store.write_agenda(Some(2), &third, &id("04"), 5).unwrap();
            }
            store
                .execute("DROP TRIGGER agenda_history_no_delete")
                .unwrap();
            store
                .execute(&format!(
                    "DELETE FROM agenda_history WHERE agenda_id='{}' AND revision={missing_revision}",
                    first.agenda_id
                ))
                .unwrap();
            assert!(
                store.read_current_agenda(&first.agenda_id).is_err(),
                "missing {name} history must fail closed"
            );
            drop(store);
            let _ = std::fs::remove_file(p);
        }
    }

    #[test]
    fn latest_history_corruption_is_not_accepted_as_current() {
        for (name, sql) in [
            ("history-json", "UPDATE agenda_history SET record_json='{}'"),
            (
                "history-channel",
                "UPDATE agenda_history SET channel_id='00000000-0000-4000-8000-000000000007'",
            ),
            (
                "history-provenance",
                "UPDATE agenda_history SET provenance_kind='text_import_claim'",
            ),
            (
                "history-source",
                "UPDATE agenda_history SET provenance_source_sha256='b'",
            ),
            (
                "history-actor",
                "UPDATE agenda_history SET changed_by='00000000-0000-4000-8000-000000000007'",
            ),
            (
                "history-time",
                "UPDATE agenda_history SET occurred_at_unix_ms=99",
            ),
        ] {
            let p = temp_db(name);
            let _ = std::fs::remove_file(&p);
            let store = StationStore::open(&p).unwrap();
            write_fixture(&store);
            store
                .execute("DROP TRIGGER agenda_history_no_update")
                .unwrap();
            store.execute(sql).unwrap();
            assert!(
                store.read_current_agenda(&id("01")).is_err(),
                "history corruption {name} must fail closed"
            );
            drop(store);
            let _ = std::fs::remove_file(p);
        }
    }

    #[test]
    fn transactional_sync_binding_rejects_changed_media_and_hash() {
        let p = temp_db("sync-binding");
        let _ = std::fs::remove_file(&p);
        let store = StationStore::open(&p).unwrap();
        seed_sync_binding(&store);
        let mut agenda = fixture();
        agenda.provenance = AgendaProvenance::StaffAuthored {
            author: AgendaActor { actor_id: id("04") },
            asserted_at_unix_ms: 1,
        };
        store
            .put_media_asset(&MediaAsset {
                asset_id: "a".repeat(64),
                media_path: r"C:\Windows\System32\notepad.exe".into(),
                duration_ms: 1_000,
                readiness: AssetReadiness::Missing,
            })
            .unwrap();
        assert!(matches!(
            store.write_agenda(None, &agenda, &id("04"), 3),
            Err(AgendaWriteError::Invalid(message)) if message.contains("exact ready")
        ));
        store
            .put_media_asset(&MediaAsset {
                asset_id: "a".repeat(64),
                media_path: r"C:\Windows\System32\notepad.exe".into(),
                duration_ms: 1_000,
                readiness: AssetReadiness::Ready,
            })
            .unwrap();
        agenda.sync_intent.media_asset_sha256 = "b".repeat(64);
        assert!(matches!(
            store.write_agenda(None, &agenda, &id("04"), 3),
            Err(AgendaWriteError::Invalid(message)) if message.contains("exact ready")
        ));
        drop(store);
        let _ = std::fs::remove_file(p);
    }

    #[test]
    fn agenda_count_capacity_is_bounded() {
        let p = temp_db("capacity");
        let _ = std::fs::remove_file(&p);
        let store = StationStore::open(&p).unwrap();
        seed_sync_binding(&store);
        for index in 0..MAX_AGENDAS {
            let mut agenda = fixture();
            agenda.agenda_id = capacity_agenda_id(index);
            assert!(store.write_agenda(None, &agenda, &id("04"), 3).is_ok());
        }
        let mut extra = fixture();
        extra.agenda_id = capacity_agenda_id(MAX_AGENDAS);
        assert!(matches!(
            store.write_agenda(None, &extra, &id("04"), 3),
            Err(AgendaWriteError::Capacity)
        ));
        drop(store);
        let _ = std::fs::remove_file(p);
    }
}
