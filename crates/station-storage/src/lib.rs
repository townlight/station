use std::ffi::{CStr, CString, c_char, c_int, c_longlong, c_void};
use std::path::Path;
use std::ptr;

use station_domain::{StationProfile, StationProfileDocument};
use station_schedule::{
    AssetReadiness, CommitPlan, CommitReport, DispatchStatus, MediaAsset, ScheduleItem,
    ScheduleState, prepare_commit,
};

const SQLITE_OK: c_int = 0;
const SQLITE_ROW: c_int = 100;
const SQLITE_DONE: c_int = 101;
const SQLITE_OPEN_READWRITE: c_int = 0x0000_0002;
const SQLITE_OPEN_CREATE: c_int = 0x0000_0004;
const SQLITE_OPEN_FULLMUTEX: c_int = 0x0001_0000;

#[repr(C)]
struct Sqlite3 {
    _private: [u8; 0],
}

#[repr(C)]
struct SqliteStatement {
    _private: [u8; 0],
}

#[link(name = "winsqlite3")]
unsafe extern "C" {
    fn sqlite3_open_v2(
        filename: *const c_char,
        database: *mut *mut Sqlite3,
        flags: c_int,
        virtual_file_system: *const c_char,
    ) -> c_int;
    fn sqlite3_close(database: *mut Sqlite3) -> c_int;
    fn sqlite3_exec(
        database: *mut Sqlite3,
        sql: *const c_char,
        callback: *mut c_void,
        callback_argument: *mut c_void,
        error_message: *mut *mut c_char,
    ) -> c_int;
    fn sqlite3_prepare_v2(
        database: *mut Sqlite3,
        sql: *const c_char,
        byte_count: c_int,
        statement: *mut *mut SqliteStatement,
        tail: *mut *const c_char,
    ) -> c_int;
    fn sqlite3_bind_text(
        statement: *mut SqliteStatement,
        index: c_int,
        value: *const c_char,
        byte_count: c_int,
        destructor: *mut c_void,
    ) -> c_int;
    fn sqlite3_bind_int64(
        statement: *mut SqliteStatement,
        index: c_int,
        value: c_longlong,
    ) -> c_int;
    fn sqlite3_step(statement: *mut SqliteStatement) -> c_int;
    fn sqlite3_column_text(statement: *mut SqliteStatement, column: c_int) -> *const u8;
    fn sqlite3_column_int64(statement: *mut SqliteStatement, column: c_int) -> c_longlong;
    fn sqlite3_finalize(statement: *mut SqliteStatement) -> c_int;
    fn sqlite3_errmsg(database: *mut Sqlite3) -> *const c_char;
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProfileWriteError {
    Conflict(u64),
    Storage(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CommitWriteError {
    NotFound,
    GateFailed(Box<CommitPlan>),
    Invalid(&'static str),
    Storage(String),
}

pub struct StationStore {
    database: *mut Sqlite3,
}

impl StationStore {
    pub fn open(database_path: impl AsRef<Path>) -> Result<Self, String> {
        let filename = CString::new(database_path.as_ref().to_string_lossy().as_bytes())
            .map_err(|_| "database path contains a null byte".to_string())?;
        let mut database = ptr::null_mut();
        let flags = SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX;
        // SAFETY: `filename` is a valid C string, `database` points to writable storage, and the
        // returned handle is owned by `StationStore` until `Drop` closes it.
        let status =
            unsafe { sqlite3_open_v2(filename.as_ptr(), &mut database, flags, ptr::null()) };
        if status != SQLITE_OK {
            let message = sqlite_error(database);
            if !database.is_null() {
                // SAFETY: SQLite returned this handle and it has not been closed.
                unsafe { sqlite3_close(database) };
            }
            return Err(message);
        }

        let store = Self { database };
        store.execute(
            "PRAGMA journal_mode=WAL;\
             PRAGMA busy_timeout=5000;\
             PRAGMA foreign_keys=ON;\
             CREATE TABLE IF NOT EXISTS station_profile (\
               singleton INTEGER PRIMARY KEY CHECK (singleton = 1),\
               station_id TEXT NOT NULL,\
               display_name TEXT NOT NULL,\
               timezone TEXT NOT NULL,\
               revision INTEGER NOT NULL DEFAULT 1\
             );\
             CREATE TABLE IF NOT EXISTS media_assets (\
               asset_id TEXT PRIMARY KEY,\
               media_path TEXT NOT NULL,\
               duration_ms INTEGER NOT NULL CHECK (duration_ms > 0),\
               readiness TEXT NOT NULL CHECK (readiness IN ('ready','missing','rejected','processing'))\
             );\
             CREATE TABLE IF NOT EXISTS schedule_items (\
               item_id TEXT PRIMARY KEY,\
               channel_id TEXT NOT NULL,\
               asset_id TEXT NOT NULL,\
               title TEXT NOT NULL,\
               starts_at_unix_ms INTEGER NOT NULL CHECK (starts_at_unix_ms >= 0),\
               duration_ms INTEGER NOT NULL CHECK (duration_ms > 0),\
               state TEXT NOT NULL CHECK (state IN ('draft','committed','cancelled'))\
             );\
             CREATE INDEX IF NOT EXISTS schedule_items_channel_time \
               ON schedule_items(channel_id, starts_at_unix_ms);\
             CREATE TABLE IF NOT EXISTS commit_reports (\
               report_id TEXT PRIMARY KEY,\
               plan_id TEXT NOT NULL,\
               channel_id TEXT NOT NULL,\
               schedule_item_id TEXT NOT NULL,\
               asset_id TEXT NOT NULL,\
               approved_by TEXT NOT NULL,\
               approved_at_unix_ms INTEGER NOT NULL,\
               dispatch_status TEXT NOT NULL CHECK (dispatch_status IN ('pending','queued','acknowledged','error','cancelled')),\
               operator_notes TEXT NOT NULL\
             );\
             CREATE INDEX IF NOT EXISTS commit_reports_channel_time \
               ON commit_reports(channel_id, approved_at_unix_ms);",
        )?;
        Ok(store)
    }

    pub fn write_profile(
        &self,
        profile: &StationProfile,
        expected_revision: u64,
    ) -> Result<StationProfileDocument, ProfileWriteError> {
        self.execute("BEGIN IMMEDIATE")
            .map_err(ProfileWriteError::Storage)?;
        let outcome = (|| {
            let current = self.read_profile().map_err(ProfileWriteError::Storage)?;
            let current_revision = current.as_ref().map_or(0, |document| document.revision);
            if current_revision != expected_revision {
                return Err(ProfileWriteError::Conflict(current_revision));
            }
            self.upsert_profile(profile)
                .map_err(ProfileWriteError::Storage)?;
            Ok(StationProfileDocument {
                profile: profile.clone(),
                revision: current_revision + 1,
            })
        })();

        match outcome {
            Ok(document) => match self.execute("COMMIT") {
                Ok(()) => Ok(document),
                Err(message) => {
                    let _ = self.execute("ROLLBACK");
                    Err(ProfileWriteError::Storage(message))
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn read_profile(&self) -> Result<Option<StationProfileDocument>, String> {
        let statement = self.prepare(
            "SELECT station_id, display_name, timezone, revision FROM station_profile WHERE singleton=1",
        )?;
        // SAFETY: the prepared statement is live.
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(StationProfileDocument {
                profile: StationProfile {
                    station_id: column_text(statement.raw, 0)?,
                    display_name: column_text(statement.raw, 1)?,
                    timezone: column_text(statement.raw, 2)?,
                },
                revision: u64::try_from(unsafe { sqlite3_column_int64(statement.raw, 3) })
                    .map_err(|_| "database returned an invalid profile revision".to_string())?,
            })),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn put_media_asset(&self, asset: &MediaAsset) -> Result<(), String> {
        asset.validate().map_err(|error| format!("{error:?}"))?;
        let statement = self.prepare(
            "INSERT INTO media_assets(asset_id, media_path, duration_ms, readiness) VALUES(?1, ?2, ?3, ?4) \
             ON CONFLICT(asset_id) DO UPDATE SET media_path=excluded.media_path, duration_ms=excluded.duration_ms, readiness=excluded.readiness",
        )?;
        let asset_id = c_string(&asset.asset_id, "asset identity")?;
        let media_path = c_string(&asset.media_path, "media path")?;
        let readiness = c_string(readiness_name(asset.readiness), "asset readiness")?;
        bind_text(self.database, statement.raw, 1, &asset_id)?;
        bind_text(self.database, statement.raw, 2, &media_path)?;
        bind_i64(
            self.database,
            statement.raw,
            3,
            checked_i64(asset.duration_ms, "asset duration")?,
        )?;
        bind_text(self.database, statement.raw, 4, &readiness)?;
        expect_done(self.database, statement.raw)
    }

    pub fn read_media_asset(&self, asset_id: &str) -> Result<Option<MediaAsset>, String> {
        let statement = self.prepare(
            "SELECT asset_id, media_path, duration_ms, readiness FROM media_assets WHERE asset_id=?1",
        )?;
        let asset_id = c_string(asset_id, "asset identity")?;
        bind_text(self.database, statement.raw, 1, &asset_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_media_asset_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn put_schedule_item(&self, item: &ScheduleItem) -> Result<(), String> {
        item.validate().map_err(|error| format!("{error:?}"))?;
        let statement = self.prepare(
            "INSERT INTO schedule_items(item_id, channel_id, asset_id, title, starts_at_unix_ms, duration_ms, state) \
             VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7) \
             ON CONFLICT(item_id) DO UPDATE SET channel_id=excluded.channel_id, asset_id=excluded.asset_id, title=excluded.title, \
             starts_at_unix_ms=excluded.starts_at_unix_ms, duration_ms=excluded.duration_ms, state=excluded.state",
        )?;
        let values = [
            c_string(&item.item_id, "schedule item identity")?,
            c_string(&item.channel_id, "channel identity")?,
            c_string(&item.asset_id, "asset identity")?,
            c_string(&item.title, "schedule title")?,
            c_string(schedule_state_name(item.state), "schedule state")?,
        ];
        for (index, value) in values[..4].iter().enumerate() {
            bind_text(self.database, statement.raw, (index + 1) as c_int, value)?;
        }
        bind_i64(self.database, statement.raw, 5, item.starts_at_unix_ms)?;
        bind_i64(
            self.database,
            statement.raw,
            6,
            checked_i64(item.duration_ms, "schedule duration")?,
        )?;
        bind_text(self.database, statement.raw, 7, &values[4])?;
        expect_done(self.database, statement.raw)
    }

    pub fn read_schedule_item(&self, item_id: &str) -> Result<Option<ScheduleItem>, String> {
        let statement = self.prepare(
            "SELECT item_id, channel_id, asset_id, title, starts_at_unix_ms, duration_ms, state \
             FROM schedule_items WHERE item_id=?1",
        )?;
        let item_id = c_string(item_id, "schedule item identity")?;
        bind_text(self.database, statement.raw, 1, &item_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_schedule_item_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_channel_schedule(&self, channel_id: &str) -> Result<Vec<ScheduleItem>, String> {
        let statement = self.prepare(
            "SELECT item_id, channel_id, asset_id, title, starts_at_unix_ms, duration_ms, state \
             FROM schedule_items WHERE channel_id=?1 ORDER BY starts_at_unix_ms, item_id",
        )?;
        let channel_id = c_string(channel_id, "channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        let mut items = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => items.push(read_schedule_item_row(statement.raw)?),
                SQLITE_DONE => return Ok(items),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn prepare_schedule_commit(
        &self,
        plan_id: &str,
        item_id: &str,
    ) -> Result<CommitPlan, CommitWriteError> {
        let item = self
            .read_schedule_item(item_id)
            .map_err(CommitWriteError::Storage)?
            .ok_or(CommitWriteError::NotFound)?;
        let asset = self
            .read_media_asset(&item.asset_id)
            .map_err(CommitWriteError::Storage)?;
        let channel_items = self
            .list_channel_schedule(&item.channel_id)
            .map_err(CommitWriteError::Storage)?;
        prepare_commit(plan_id, &item, asset.as_ref(), &channel_items)
            .map_err(|error| CommitWriteError::Storage(format!("{error:?}")))
    }

    pub fn commit_schedule(
        &self,
        report_id: &str,
        plan_id: &str,
        item_id: &str,
        approved_by: &str,
        approved_at_unix_ms: i64,
        operator_notes: &str,
    ) -> Result<CommitReport, CommitWriteError> {
        if report_id.trim().is_empty()
            || plan_id.trim().is_empty()
            || approved_by.trim().is_empty()
            || approved_by.chars().any(char::is_control)
            || operator_notes.len() > 4_000
            || operator_notes.chars().any(char::is_control)
            || approved_at_unix_ms < 0
        {
            return Err(CommitWriteError::Invalid(
                "commit identity, operator, timestamp, or notes are invalid",
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(CommitWriteError::Storage)?;
        let outcome = (|| {
            let plan = self.prepare_schedule_commit(plan_id, item_id)?;
            if !plan.dry_run_passed {
                return Err(CommitWriteError::GateFailed(Box::new(plan)));
            }
            let item = self
                .read_schedule_item(item_id)
                .map_err(CommitWriteError::Storage)?
                .ok_or(CommitWriteError::NotFound)?;
            let report = CommitReport {
                report_id: report_id.to_string(),
                plan_id: plan_id.to_string(),
                channel_id: item.channel_id.clone(),
                schedule_item_id: item.item_id.clone(),
                asset_id: item.asset_id.clone(),
                approved_by: approved_by.to_string(),
                approved_at_unix_ms,
                dispatch_status: DispatchStatus::Pending,
                operator_notes: operator_notes.to_string(),
            };
            self.insert_commit_report(&report)
                .map_err(CommitWriteError::Storage)?;
            self.set_schedule_state(item_id, ScheduleState::Committed)
                .map_err(CommitWriteError::Storage)?;
            Ok(report)
        })();
        match outcome {
            Ok(report) => match self.execute("COMMIT") {
                Ok(()) => Ok(report),
                Err(message) => {
                    let _ = self.execute("ROLLBACK");
                    Err(CommitWriteError::Storage(message))
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn read_commit_report(&self, report_id: &str) -> Result<Option<CommitReport>, String> {
        let statement = self.prepare(
            "SELECT report_id, plan_id, channel_id, schedule_item_id, asset_id, approved_by, \
             approved_at_unix_ms, dispatch_status, operator_notes FROM commit_reports WHERE report_id=?1",
        )?;
        let report_id = c_string(report_id, "commit report identity")?;
        bind_text(self.database, statement.raw, 1, &report_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_commit_report_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn insert_commit_report(&self, report: &CommitReport) -> Result<(), String> {
        let statement = self.prepare(
            "INSERT INTO commit_reports(report_id, plan_id, channel_id, schedule_item_id, asset_id, approved_by, \
             approved_at_unix_ms, dispatch_status, operator_notes) VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
        )?;
        let values = [
            c_string(&report.report_id, "report identity")?,
            c_string(&report.plan_id, "plan identity")?,
            c_string(&report.channel_id, "channel identity")?,
            c_string(&report.schedule_item_id, "schedule item identity")?,
            c_string(&report.asset_id, "asset identity")?,
            c_string(&report.approved_by, "operator identity")?,
            c_string(
                dispatch_status_name(report.dispatch_status),
                "dispatch status",
            )?,
            c_string(&report.operator_notes, "operator notes")?,
        ];
        for (index, value) in values[..6].iter().enumerate() {
            bind_text(self.database, statement.raw, (index + 1) as c_int, value)?;
        }
        bind_i64(self.database, statement.raw, 7, report.approved_at_unix_ms)?;
        bind_text(self.database, statement.raw, 8, &values[6])?;
        bind_text(self.database, statement.raw, 9, &values[7])?;
        expect_done(self.database, statement.raw)
    }

    fn set_schedule_state(&self, item_id: &str, state: ScheduleState) -> Result<(), String> {
        let statement = self.prepare("UPDATE schedule_items SET state=?1 WHERE item_id=?2")?;
        let state = c_string(schedule_state_name(state), "schedule state")?;
        let item_id = c_string(item_id, "schedule item identity")?;
        bind_text(self.database, statement.raw, 1, &state)?;
        bind_text(self.database, statement.raw, 2, &item_id)?;
        expect_done(self.database, statement.raw)
    }

    fn execute(&self, sql: &str) -> Result<(), String> {
        let sql = CString::new(sql).map_err(|_| "SQL contains a null byte".to_string())?;
        // SAFETY: the database handle is live, SQL is a valid C string, and no callback is used.
        let status = unsafe {
            sqlite3_exec(
                self.database,
                sql.as_ptr(),
                ptr::null_mut(),
                ptr::null_mut(),
                ptr::null_mut(),
            )
        };
        if status == SQLITE_OK {
            Ok(())
        } else {
            Err(sqlite_error(self.database))
        }
    }

    fn upsert_profile(&self, profile: &StationProfile) -> Result<(), String> {
        let sql = "INSERT INTO station_profile(singleton, station_id, display_name, timezone) VALUES(1, ?1, ?2, ?3) \
                   ON CONFLICT(singleton) DO UPDATE SET station_id=excluded.station_id, display_name=excluded.display_name, \
                   timezone=excluded.timezone, revision=station_profile.revision+1";
        let statement = self.prepare(sql)?;
        let values = [
            CString::new(profile.station_id.as_str()),
            CString::new(profile.display_name.as_str()),
            CString::new(profile.timezone.as_str()),
        ];
        for (offset, value) in values.iter().enumerate() {
            let value = value
                .as_ref()
                .map_err(|_| "profile contains a null byte".to_string())?;
            // SAFETY: the statement is live and each CString outlives `sqlite3_step` below.
            let status = unsafe {
                sqlite3_bind_text(
                    statement.raw,
                    (offset + 1) as c_int,
                    value.as_ptr(),
                    -1,
                    ptr::null_mut(),
                )
            };
            if status != SQLITE_OK {
                return Err(sqlite_error(self.database));
            }
        }
        // SAFETY: all parameters are bound and the statement is live.
        let status = unsafe { sqlite3_step(statement.raw) };
        if status == SQLITE_DONE {
            Ok(())
        } else {
            Err(sqlite_error(self.database))
        }
    }

    fn prepare(&self, sql: &str) -> Result<Statement, String> {
        let sql = CString::new(sql).map_err(|_| "SQL contains a null byte".to_string())?;
        let mut statement = ptr::null_mut();
        // SAFETY: the database handle is live and the SQL is a valid C string.
        let status = unsafe {
            sqlite3_prepare_v2(
                self.database,
                sql.as_ptr(),
                -1,
                &mut statement,
                ptr::null_mut(),
            )
        };
        if status == SQLITE_OK {
            Ok(Statement { raw: statement })
        } else {
            Err(sqlite_error(self.database))
        }
    }
}

impl Drop for StationStore {
    fn drop(&mut self) {
        // SAFETY: `StationStore` owns this live handle and drops it exactly once.
        unsafe { sqlite3_close(self.database) };
    }
}

struct Statement {
    raw: *mut SqliteStatement,
}

impl Drop for Statement {
    fn drop(&mut self) {
        // SAFETY: `Statement` owns this live handle and drops it exactly once.
        unsafe { sqlite3_finalize(self.raw) };
    }
}

fn bind_text(
    database: *mut Sqlite3,
    statement: *mut SqliteStatement,
    index: c_int,
    value: &CString,
) -> Result<(), String> {
    let status =
        unsafe { sqlite3_bind_text(statement, index, value.as_ptr(), -1, ptr::null_mut()) };
    if status == SQLITE_OK {
        Ok(())
    } else {
        Err(sqlite_error(database))
    }
}

fn bind_i64(
    database: *mut Sqlite3,
    statement: *mut SqliteStatement,
    index: c_int,
    value: i64,
) -> Result<(), String> {
    let status = unsafe { sqlite3_bind_int64(statement, index, value) };
    if status == SQLITE_OK {
        Ok(())
    } else {
        Err(sqlite_error(database))
    }
}

fn expect_done(database: *mut Sqlite3, statement: *mut SqliteStatement) -> Result<(), String> {
    if unsafe { sqlite3_step(statement) } == SQLITE_DONE {
        Ok(())
    } else {
        Err(sqlite_error(database))
    }
}

fn c_string(value: &str, field: &str) -> Result<CString, String> {
    CString::new(value).map_err(|_| format!("{field} contains a null byte"))
}

fn checked_i64(value: u64, field: &str) -> Result<i64, String> {
    i64::try_from(value).map_err(|_| format!("{field} exceeds SQLite's integer range"))
}

fn read_media_asset_row(statement: *mut SqliteStatement) -> Result<MediaAsset, String> {
    Ok(MediaAsset {
        asset_id: column_text(statement, 0)?,
        media_path: column_text(statement, 1)?,
        duration_ms: column_u64(statement, 2, "asset duration")?,
        readiness: parse_readiness(&column_text(statement, 3)?)?,
    })
}

fn read_schedule_item_row(statement: *mut SqliteStatement) -> Result<ScheduleItem, String> {
    Ok(ScheduleItem {
        item_id: column_text(statement, 0)?,
        channel_id: column_text(statement, 1)?,
        asset_id: column_text(statement, 2)?,
        title: column_text(statement, 3)?,
        starts_at_unix_ms: unsafe { sqlite3_column_int64(statement, 4) },
        duration_ms: column_u64(statement, 5, "schedule duration")?,
        state: parse_schedule_state(&column_text(statement, 6)?)?,
    })
}

fn read_commit_report_row(statement: *mut SqliteStatement) -> Result<CommitReport, String> {
    Ok(CommitReport {
        report_id: column_text(statement, 0)?,
        plan_id: column_text(statement, 1)?,
        channel_id: column_text(statement, 2)?,
        schedule_item_id: column_text(statement, 3)?,
        asset_id: column_text(statement, 4)?,
        approved_by: column_text(statement, 5)?,
        approved_at_unix_ms: unsafe { sqlite3_column_int64(statement, 6) },
        dispatch_status: parse_dispatch_status(&column_text(statement, 7)?)?,
        operator_notes: column_text(statement, 8)?,
    })
}

fn column_u64(statement: *mut SqliteStatement, column: c_int, field: &str) -> Result<u64, String> {
    u64::try_from(unsafe { sqlite3_column_int64(statement, column) })
        .map_err(|_| format!("database returned an invalid {field}"))
}

fn readiness_name(value: AssetReadiness) -> &'static str {
    match value {
        AssetReadiness::Ready => "ready",
        AssetReadiness::Missing => "missing",
        AssetReadiness::Rejected => "rejected",
        AssetReadiness::Processing => "processing",
    }
}

fn parse_readiness(value: &str) -> Result<AssetReadiness, String> {
    match value {
        "ready" => Ok(AssetReadiness::Ready),
        "missing" => Ok(AssetReadiness::Missing),
        "rejected" => Ok(AssetReadiness::Rejected),
        "processing" => Ok(AssetReadiness::Processing),
        _ => Err(format!("database returned invalid asset readiness {value}")),
    }
}

fn schedule_state_name(value: ScheduleState) -> &'static str {
    match value {
        ScheduleState::Draft => "draft",
        ScheduleState::Committed => "committed",
        ScheduleState::Cancelled => "cancelled",
    }
}

fn parse_schedule_state(value: &str) -> Result<ScheduleState, String> {
    match value {
        "draft" => Ok(ScheduleState::Draft),
        "committed" => Ok(ScheduleState::Committed),
        "cancelled" => Ok(ScheduleState::Cancelled),
        _ => Err(format!("database returned invalid schedule state {value}")),
    }
}

fn dispatch_status_name(value: DispatchStatus) -> &'static str {
    match value {
        DispatchStatus::Pending => "pending",
        DispatchStatus::Queued => "queued",
        DispatchStatus::Acknowledged => "acknowledged",
        DispatchStatus::Error => "error",
        DispatchStatus::Cancelled => "cancelled",
    }
}

fn parse_dispatch_status(value: &str) -> Result<DispatchStatus, String> {
    match value {
        "pending" => Ok(DispatchStatus::Pending),
        "queued" => Ok(DispatchStatus::Queued),
        "acknowledged" => Ok(DispatchStatus::Acknowledged),
        "error" => Ok(DispatchStatus::Error),
        "cancelled" => Ok(DispatchStatus::Cancelled),
        _ => Err(format!("database returned invalid dispatch status {value}")),
    }
}

fn column_text(statement: *mut SqliteStatement, column: c_int) -> Result<String, String> {
    // SAFETY: caller provides a live statement positioned on a row and a valid column index.
    let value = unsafe { sqlite3_column_text(statement, column) };
    if value.is_null() {
        return Err("database returned an unexpected null value".into());
    }
    // SAFETY: SQLite returns a null-terminated UTF-8 buffer valid until the next step/finalize.
    Ok(unsafe { CStr::from_ptr(value.cast()) }
        .to_string_lossy()
        .into_owned())
}

fn sqlite_error(database: *mut Sqlite3) -> String {
    if database.is_null() {
        return "SQLite failed before creating a database handle".into();
    }
    // SAFETY: SQLite owns a null-terminated error buffer associated with this live handle.
    unsafe { CStr::from_ptr(sqlite3_errmsg(database)) }
        .to_string_lossy()
        .into_owned()
}
