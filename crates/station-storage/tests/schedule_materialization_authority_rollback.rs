include!("schedule_materialization.rs");

use std::ffi::{CString, c_char, c_int, c_void};
use std::ptr;

use station_storage::ScheduleMaterializationWriteError;

const SQLITE_OK: c_int = 0;
const SQLITE_ROW: c_int = 100;
const SQLITE_OPEN_READWRITE: c_int = 2;

#[link(name = "winsqlite3")]
unsafe extern "C" {
    fn sqlite3_open_v2(
        filename: *const c_char,
        database: *mut *mut c_void,
        flags: c_int,
        virtual_file_system: *const c_char,
    ) -> c_int;
    fn sqlite3_prepare_v2(
        database: *mut c_void,
        sql: *const c_char,
        byte_count: c_int,
        statement: *mut *mut c_void,
        tail: *mut *const c_char,
    ) -> c_int;
    fn sqlite3_step(statement: *mut c_void) -> c_int;
    fn sqlite3_column_int64(statement: *mut c_void, column: c_int) -> i64;
    fn sqlite3_finalize(statement: *mut c_void) -> c_int;
    fn sqlite3_close(database: *mut c_void) -> c_int;
}

struct FailSecondEvidenceIssue {
    now: i64,
    issue_count: AtomicU64,
}

impl SourceEvidenceVerifier for FailSecondEvidenceIssue {
    fn verify(
        &self,
        _kind: SourceEvidenceKind,
        _receipt: &SourceEvidenceReceipt,
        _scope_sha256: &str,
        _payload_sha256: &str,
        _observed_at_utc_ms: i64,
    ) -> Result<(), String> {
        Err("verification must not follow the injected issuance failure".into())
    }
}

impl SourceEvidenceAuthority for FailSecondEvidenceIssue {
    fn observed_at_utc_ms(&self) -> Result<i64, String> {
        Ok(self.now)
    }

    fn issue_source_evidence(
        &self,
        _kind: SourceEvidenceKind,
        scope_sha256: &str,
        payload_sha256: &str,
        lifetime_ms: i64,
    ) -> Result<SourceEvidenceReceipt, String> {
        let issue = self.issue_count.fetch_add(1, Ordering::Relaxed) + 1;
        if issue == 2 {
            return Err("injected second evidence issuance failure".into());
        }
        Ok(SourceEvidenceReceipt {
            issuer_id: "66666666-6666-4666-8666-666666666666".into(),
            receipt_id: format!("88888888-8888-4888-8888-{issue:012}"),
            issued_at_utc_ms: self.now,
            expires_at_utc_ms: self.now + lifetime_ms,
            scope_sha256: scope_sha256.into(),
            attestation_sha256: payload_sha256.into(),
        })
    }
}

fn raw_row_count(database_path: &Path, table: &str) -> i64 {
    let filename = CString::new(database_path.to_string_lossy().as_bytes()).unwrap();
    let sql = CString::new(format!("SELECT COUNT(*) FROM {table}")).unwrap();
    let mut database = ptr::null_mut();
    let mut statement = ptr::null_mut();
    // SAFETY: all C strings and output slots remain live through the query.
    assert_eq!(
        unsafe {
            sqlite3_open_v2(
                filename.as_ptr(),
                &mut database,
                SQLITE_OPEN_READWRITE,
                ptr::null(),
            )
        },
        SQLITE_OK
    );
    // SAFETY: the database and SQL string are live and the output slot is valid.
    assert_eq!(
        unsafe { sqlite3_prepare_v2(database, sql.as_ptr(), -1, &mut statement, ptr::null_mut()) },
        SQLITE_OK
    );
    // SAFETY: this scalar COUNT query returns one integer row.
    assert_eq!(unsafe { sqlite3_step(statement) }, SQLITE_ROW);
    let count = unsafe { sqlite3_column_int64(statement, 0) };
    // SAFETY: both handles are live and released exactly once.
    assert_eq!(unsafe { sqlite3_finalize(statement) }, SQLITE_OK);
    assert_eq!(unsafe { sqlite3_close(database) }, SQLITE_OK);
    count
}

#[test]
fn partial_evidence_failure_rolls_back_every_row_and_connection_is_reusable() {
    let path = temporary_database("authority-rollback");
    let store = StationStore::open(&path).unwrap();
    seed_authorities(&store);
    publish_council_asset(&store);
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64;
    let window_start = future_midnight(now);
    let window = MaterializationWindow {
        starts_at_utc_ms: window_start,
        ends_at_utc_ms: window_start + 2 * 3_600_000,
    };
    let failing_authority = FailSecondEvidenceIssue {
        now,
        issue_count: AtomicU64::new(0),
    };

    let error = store
        .materialize_schedule_batch_with_explicit_window(
            CHANNEL,
            &window,
            OWNER,
            &failing_authority,
        )
        .unwrap_err();
    assert!(matches!(
        error,
        ScheduleMaterializationWriteError::StaleAuthority(_)
    ));
    assert_eq!(failing_authority.issue_count.load(Ordering::Relaxed), 2);
    for table in [
        "schedule_materialization_runs",
        "schedule_materialization_plans",
        "schedule_materialization_outcomes",
        "schedule_materialization_drafts",
        "schedule_items",
        "schedule_item_history",
    ] {
        assert_eq!(raw_row_count(&path, table), 0, "leaked row in {table}");
    }

    let healthy_authority = TestAuthority {
        now,
        sequence: AtomicU64::new(0),
    };
    let applied = store
        .materialize_schedule_batch_with_explicit_window(
            CHANNEL,
            &window,
            OWNER,
            &healthy_authority,
        )
        .unwrap();
    let ScheduleMaterializationApplyOutcome::Applied(run) = applied else {
        panic!("the same connection must apply after rollback")
    };
    let schedule_item_id = run
        .outcomes
        .iter()
        .find_map(|record| match &record.outcome {
            ScheduleMaterializationOutcome::Proposed {
                schedule_item_id, ..
            } => Some(schedule_item_id),
            _ => None,
        })
        .expect("healthy retry must create a Draft");
    assert!(
        store
            .read_schedule_item(schedule_item_id)
            .unwrap()
            .is_some()
    );
    assert_eq!(
        store
            .list_schedule_item_history(schedule_item_id, 8)
            .unwrap()
            .len(),
        1
    );
    drop(store);
    cleanup(&path);
}
