use std::ffi::{CStr, CString, c_char, c_int, c_longlong, c_void};
use std::path::Path;
use std::ptr;

use station_domain::{StationProfile, StationProfileDocument};

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
             );",
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
