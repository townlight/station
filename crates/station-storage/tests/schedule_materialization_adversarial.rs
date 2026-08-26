use std::ffi::{CString, c_char, c_int, c_void};
use std::path::{Path, PathBuf};
use std::ptr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::to_string;
use sha2::{Digest, Sha256};
use station_catalog::{CatalogAssetRecord, CatalogEditorialState};
use station_schedule::{AssetReadiness, MediaAsset, ScheduleItem, ScheduleState};
use station_schedule_automation::{
    AssetPickStrategy, AssetPredicate, DaypartBlock, LocalDate, MaterializationWindow,
    RecurrenceTimeZone, RecurringRule, SavedSearch, SavedSearchOrder, SavedSearchQuery,
    SourceEvidenceAuthority, SourceEvidenceKind, SourceEvidenceReceipt, SourceEvidenceVerifier,
};
use station_storage::{
    ScheduleItemWrite, ScheduleMaterializationApplyOutcome, ScheduleMaterializationOutcome,
    ScheduleMaterializationRun, ScheduleMaterializationWriteError, StationStore,
};

const CHANNEL: &str = "11111111-1111-4111-8111-111111111111";
const OWNER: &str = "22222222-2222-4222-8222-222222222222";
const SEARCH: &str = "33333333-3333-4333-8333-333333333333";
const BLOCK: &str = "44444444-4444-4444-8444-444444444444";
const RULE: &str = "55555555-5555-4555-8555-555555555555";
const ASSET: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const OTHER_ASSET: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const WINDOW_START: i64 = 4_070_908_800_000; // 2099-01-01T00:00:00Z
const DAY_MS: i64 = 86_400_000;
const AUTHORITY_NOW: i64 = 2_000_000_000_000;
const SQLITE_OK: c_int = 0;
const SQLITE_ROW: c_int = 100;
const SQLITE_OPEN_READWRITE: c_int = 2;

static DATABASE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

#[link(name = "winsqlite3")]
unsafe extern "C" {
    fn sqlite3_open_v2(
        filename: *const c_char,
        database: *mut *mut c_void,
        flags: c_int,
        virtual_file_system: *const c_char,
    ) -> c_int;
    fn sqlite3_exec(
        database: *mut c_void,
        sql: *const c_char,
        callback: *mut c_void,
        callback_argument: *mut c_void,
        error_message: *mut *mut c_char,
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

struct TestEvidenceAuthority {
    sequence: AtomicU64,
}

impl TestEvidenceAuthority {
    fn new() -> Self {
        Self {
            sequence: AtomicU64::new(1),
        }
    }

    fn attestation(
        kind: SourceEvidenceKind,
        receipt_id: &str,
        scope_sha256: &str,
        payload_sha256: &str,
    ) -> String {
        let mut digest = Sha256::new();
        digest.update(match kind {
            SourceEvidenceKind::CandidateProjection => b"candidate".as_slice(),
            SourceEvidenceKind::PriorAirings => b"prior".as_slice(),
        });
        for value in [receipt_id, scope_sha256, payload_sha256] {
            digest.update((value.len() as u64).to_le_bytes());
            digest.update(value.as_bytes());
        }
        format!("{:x}", digest.finalize())
    }
}

impl SourceEvidenceVerifier for TestEvidenceAuthority {
    fn verify(
        &self,
        kind: SourceEvidenceKind,
        receipt: &SourceEvidenceReceipt,
        scope_sha256: &str,
        payload_sha256: &str,
        observed_at_utc_ms: i64,
    ) -> Result<(), String> {
        if receipt.issuer_id != "99999999-9999-4999-8999-999999999999"
            || receipt.scope_sha256 != scope_sha256
            || receipt.issued_at_utc_ms > observed_at_utc_ms
            || receipt.expires_at_utc_ms <= observed_at_utc_ms
            || receipt.attestation_sha256
                != Self::attestation(kind, &receipt.receipt_id, scope_sha256, payload_sha256)
        {
            return Err("test evidence failed exact authentication".into());
        }
        Ok(())
    }
}

impl SourceEvidenceAuthority for TestEvidenceAuthority {
    fn observed_at_utc_ms(&self) -> Result<i64, String> {
        Ok(AUTHORITY_NOW)
    }

    fn issue_source_evidence(
        &self,
        kind: SourceEvidenceKind,
        scope_sha256: &str,
        payload_sha256: &str,
        lifetime_ms: i64,
    ) -> Result<SourceEvidenceReceipt, String> {
        let sequence = self.sequence.fetch_add(1, Ordering::Relaxed);
        let receipt_id = format!("88888888-8888-4888-8888-{sequence:012}");
        Ok(SourceEvidenceReceipt {
            issuer_id: "99999999-9999-4999-8999-999999999999".into(),
            receipt_id: receipt_id.clone(),
            issued_at_utc_ms: AUTHORITY_NOW,
            expires_at_utc_ms: AUTHORITY_NOW + lifetime_ms,
            scope_sha256: scope_sha256.into(),
            attestation_sha256: Self::attestation(kind, &receipt_id, scope_sha256, payload_sha256),
        })
    }
}

fn temporary_database(name: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let sequence = DATABASE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    std::env::temp_dir().join(format!(
        "townlight-materialization-adversarial-{name}-{nonce}-{sequence}.db"
    ))
}

fn window() -> MaterializationWindow {
    MaterializationWindow {
        starts_at_utc_ms: WINDOW_START,
        ends_at_utc_ms: WINDOW_START + DAY_MS,
    }
}

fn search() -> SavedSearch {
    SavedSearch {
        search_id: SEARCH.into(),
        name: "Council programs".into(),
        owner_operator_id: OWNER.into(),
        created_at_utc_ms: 1_900_000_000_000,
        query: SavedSearchQuery {
            all_of: vec![AssetPredicate::TitleContains("Council".into())],
            any_of: vec![],
            order_by: SavedSearchOrder::PublishedNewest,
            limit: 20,
        },
        revision: 1,
    }
}

fn block() -> DaypartBlock {
    DaypartBlock {
        block_id: BLOCK.into(),
        channel_id: CHANNEL.into(),
        label: "All-day schedule".into(),
        starts_local_minute: 0,
        ends_local_minute: 1_440,
        weekday_mask: 0b0111_1111,
        season_starts_on: None,
        season_ends_on: None,
        allow_rule_ids: vec![RULE.into()],
        revision: 1,
    }
}

fn rule(anchor_day: u8) -> RecurringRule {
    RecurringRule {
        rule_id: RULE.into(),
        channel_id: CHANNEL.into(),
        saved_search_id: SEARCH.into(),
        saved_search_revision: 1,
        pick_strategy: AssetPickStrategy::Top,
        rolling_window_days: 14,
        avoid_repeat_within_days: None,
        enabled: true,
        priority: 10,
        block_id: BLOCK.into(),
        block_revision: 1,
        weekday_mask: 0b0111_1111,
        interval_weeks: 1,
        anchor_date: LocalDate {
            year: 2099,
            month: 1,
            day: anchor_day,
        },
        timezone: RecurrenceTimeZone::Utc,
        local_start_minute: 60,
        duration_minutes: 60,
        revision: 1,
    }
}

fn seed_authority(store: &StationStore, anchor_day: u8) {
    store
        .write_saved_search(&search(), None, "operator", 1)
        .unwrap();
    store
        .write_daypart_block(&block(), None, "operator", 2)
        .unwrap();
    store
        .write_recurring_rule(&rule(anchor_day), None, "operator", 3)
        .unwrap();
}

fn publish_candidate(store: &StationStore, database: &Path) -> PathBuf {
    let media_path = database.with_extension("candidate.ts");
    std::fs::write(&media_path, b"materialization candidate").unwrap();
    store
        .put_media_asset(&MediaAsset {
            asset_id: ASSET.into(),
            media_path: media_path.to_string_lossy().into_owned(),
            duration_ms: 3_600_000,
            readiness: AssetReadiness::Ready,
        })
        .unwrap();
    let draft = CatalogAssetRecord {
        asset_sha256: ASSET.into(),
        title: "Council meeting".into(),
        meeting_body: Some("City Council".into()),
        editorial_state: CatalogEditorialState::Draft,
        published_at_utc_ms: None,
        revision: 1,
    };
    store
        .write_catalog_asset(ASSET, &draft, None, "catalog", 4)
        .unwrap();
    let published = CatalogAssetRecord {
        editorial_state: CatalogEditorialState::Published,
        published_at_utc_ms: Some(5),
        revision: 2,
        ..draft
    };
    store
        .write_catalog_asset(ASSET, &published, Some(1), "catalog", 5)
        .unwrap();
    media_path
}

fn seed_materializable(database: &Path) -> StationStore {
    let store = StationStore::open(database).unwrap();
    seed_authority(&store, 1);
    publish_candidate(&store, database);
    store
}

fn run_once(
    store: &StationStore,
    authority: &TestEvidenceAuthority,
) -> ScheduleMaterializationApplyOutcome {
    store
        .materialize_schedule_batch_with_explicit_window(CHANNEL, &window(), "operator", authority)
        .unwrap()
}

fn run_from(outcome: &ScheduleMaterializationApplyOutcome) -> &ScheduleMaterializationRun {
    match outcome {
        ScheduleMaterializationApplyOutcome::Applied(run)
        | ScheduleMaterializationApplyOutcome::AlreadyApplied(run) => run,
    }
}

fn proposed_item(run: &ScheduleMaterializationRun) -> (String, String) {
    run.outcomes
        .iter()
        .find_map(|record| match &record.outcome {
            ScheduleMaterializationOutcome::Proposed {
                occurrence_id,
                schedule_item_id,
                ..
            } => Some((occurrence_id.clone(), schedule_item_id.clone())),
            _ => None,
        })
        .expect("fixture must produce a proposed occurrence")
}

fn raw_sql_status(database_path: &Path, sql: &str) -> c_int {
    let filename = CString::new(database_path.to_string_lossy().as_bytes()).unwrap();
    let sql = CString::new(sql).unwrap();
    let mut database = ptr::null_mut();
    // SAFETY: inputs are live C strings and every opened handle is closed below.
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
    // SAFETY: the database handle and SQL string are live; no callback is used.
    let status = unsafe {
        sqlite3_exec(
            database,
            sql.as_ptr(),
            ptr::null_mut(),
            ptr::null_mut(),
            ptr::null_mut(),
        )
    };
    // SAFETY: the handle is live and no statement remains open.
    assert_eq!(unsafe { sqlite3_close(database) }, SQLITE_OK);
    status
}

fn raw_i64(database_path: &Path, sql: &str) -> i64 {
    let filename = CString::new(database_path.to_string_lossy().as_bytes()).unwrap();
    let sql = CString::new(sql).unwrap();
    let mut database = ptr::null_mut();
    let mut statement = ptr::null_mut();
    // SAFETY: inputs and output slots remain live for the complete query.
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
    // SAFETY: the SQL is null terminated and statement output slot is valid.
    assert_eq!(
        unsafe { sqlite3_prepare_v2(database, sql.as_ptr(), -1, &mut statement, ptr::null_mut()) },
        SQLITE_OK
    );
    // SAFETY: the prepared scalar query is live and its first column is an integer.
    assert_eq!(unsafe { sqlite3_step(statement) }, SQLITE_ROW);
    let value = unsafe { sqlite3_column_int64(statement, 0) };
    // SAFETY: statement and database are live and finalized/closed exactly once.
    assert_eq!(unsafe { sqlite3_finalize(statement) }, SQLITE_OK);
    assert_eq!(unsafe { sqlite3_close(database) }, SQLITE_OK);
    value
}

fn sql_quote(value: &str) -> String {
    value.replace('\'', "''")
}

fn revise_catalog_title(store: &StationStore) {
    let mut revised = store.read_catalog_asset(ASSET).unwrap().unwrap();
    revised.title = "Council meeting revised".into();
    revised.revision = 3;
    store
        .write_catalog_asset(ASSET, &revised, Some(2), "catalog", 6)
        .unwrap();
}

fn cancel_item_with_valid_history(database: &Path, item: &ScheduleItem) {
    let cancelled = ScheduleItem {
        revision: item.revision + 1,
        state: ScheduleState::Cancelled,
        ..item.clone()
    };
    let record_json = sql_quote(&to_string(&cancelled).unwrap());
    let sql = format!(
        "UPDATE schedule_items SET revision={revision},state='cancelled' WHERE item_id='{item_id}';\
         INSERT INTO schedule_item_history(item_id,revision,record_json,changed_by,occurred_at_unix_ms) \
         VALUES('{item_id}',{revision},'{record_json}','operator',7)",
        revision = cancelled.revision,
        item_id = cancelled.item_id,
    );
    assert_eq!(raw_sql_status(database, &sql), SQLITE_OK);
}

fn seed_historical_committed_rows(database: &Path, count: usize) {
    let mut sql = String::new();
    for index in 0..count {
        let item = ScheduleItem {
            item_id: format!("10000000-0000-4000-8000-{index:012}"),
            revision: 1,
            channel_id: CHANNEL.into(),
            asset_id: OTHER_ASSET.into(),
            title: format!("Historical item {index}"),
            starts_at_unix_ms: 1_000 + i64::try_from(index).unwrap() * 2_000,
            duration_ms: 1_000,
            state: ScheduleState::Committed,
        };
        let json = sql_quote(&to_string(&item).unwrap());
        sql.push_str(&format!(
            "INSERT INTO schedule_items(item_id,revision,channel_id,asset_id,title,starts_at_unix_ms,duration_ms,state) \
             VALUES('{id}',1,'{channel}','{asset}','{title}',{start},1000,'committed');\
             INSERT INTO schedule_item_history(item_id,revision,record_json,changed_by,occurred_at_unix_ms) \
             VALUES('{id}',1,'{json}','history-seed',1);",
            id = item.item_id,
            channel = item.channel_id,
            asset = item.asset_id,
            title = item.title,
            start = item.starts_at_unix_ms,
        ));
    }
    assert_eq!(raw_sql_status(database, &sql), SQLITE_OK);
}

#[test]
fn same_source_and_unchanged_state_is_already_applied() {
    let database = temporary_database("same-state");
    let store = seed_materializable(&database);
    let authority = TestEvidenceAuthority::new();

    let first = run_once(&store, &authority);
    let second = run_once(&store, &authority);

    assert!(matches!(
        first,
        ScheduleMaterializationApplyOutcome::Applied(_)
    ));
    assert!(
        matches!(
            &second,
            ScheduleMaterializationApplyOutcome::AlreadyApplied(_)
        ),
        "unchanged replay must be AlreadyApplied, got {second:?}"
    );
    assert_eq!(run_from(&first).run_id, run_from(&second).run_id);
}

#[test]
fn same_source_after_automation_draft_edit_creates_changed_disposition_run() {
    let database = temporary_database("edited-draft");
    let store = seed_materializable(&database);
    let authority = TestEvidenceAuthority::new();
    let first = run_once(&store, &authority);
    let (_, item_id) = proposed_item(run_from(&first));
    let mut edited = store.read_schedule_item(&item_id).unwrap().unwrap();
    edited.revision += 1;
    edited.title = "Operator-edited Council title".into();
    store
        .write_schedule_item(&ScheduleItemWrite {
            item: edited,
            changed_by: "operator".into(),
            occurred_at_unix_ms: AUTHORITY_NOW + 1,
        })
        .unwrap();

    let second = run_once(&store, &authority);

    assert!(matches!(
        second,
        ScheduleMaterializationApplyOutcome::Applied(_)
    ));
    assert_ne!(run_from(&first).run_id, run_from(&second).run_id);
    assert!(run_from(&second).outcomes.iter().any(|record| matches!(
        record.outcome,
        ScheduleMaterializationOutcome::ExistingAutomationDraftChanged { .. }
    )));
}

#[test]
fn same_source_after_commit_creates_committed_disposition_run() {
    let database = temporary_database("committed");
    let store = seed_materializable(&database);
    let authority = TestEvidenceAuthority::new();
    let first = run_once(&store, &authority);
    let (_, item_id) = proposed_item(run_from(&first));
    store
        .commit_schedule(
            "materialization-report",
            "materialization-plan",
            &item_id,
            "operator",
            AUTHORITY_NOW + 1,
            "Approved in adversarial fixture",
        )
        .unwrap();

    let second = run_once(&store, &authority);

    assert!(matches!(
        second,
        ScheduleMaterializationApplyOutcome::Applied(_)
    ));
    assert_ne!(run_from(&first).run_id, run_from(&second).run_id);
    assert!(run_from(&second).outcomes.iter().any(|record| matches!(
        record.outcome,
        ScheduleMaterializationOutcome::ExistingCommitted { .. }
    )));
}

#[test]
fn same_source_after_cancel_creates_cancelled_disposition_run() {
    let database = temporary_database("cancelled");
    let store = seed_materializable(&database);
    let authority = TestEvidenceAuthority::new();
    let first = run_once(&store, &authority);
    let (_, item_id) = proposed_item(run_from(&first));
    let item = store.read_schedule_item(&item_id).unwrap().unwrap();
    drop(store);
    cancel_item_with_valid_history(&database, &item);
    let store = StationStore::open(&database).unwrap();

    let second = run_once(&store, &authority);

    assert!(matches!(
        second,
        ScheduleMaterializationApplyOutcome::Applied(_)
    ));
    assert_ne!(run_from(&first).run_id, run_from(&second).run_id);
    assert!(run_from(&second).outcomes.iter().any(|record| matches!(
        record.outcome,
        ScheduleMaterializationOutcome::CancelledOccurrence { .. }
    )));
}

#[test]
fn corrupted_durable_result_fails_closed_on_reload() {
    let database = temporary_database("result-corruption");
    let store = seed_materializable(&database);
    let authority = TestEvidenceAuthority::new();
    let applied = run_once(&store, &authority);
    let run_id = run_from(&applied).run_id.clone();
    drop(store);
    let sql = format!(
        "DROP TRIGGER schedule_materialization_outcomes_no_update;\
         UPDATE schedule_materialization_outcomes SET record_json=replace(record_json,'{asset}','bad')",
        asset = ASSET,
    );
    assert_eq!(raw_sql_status(&database, &sql), SQLITE_OK);

    let reopened = StationStore::open(&database).unwrap();
    assert!(reopened.read_schedule_materialization_run(&run_id).is_err());
}

#[test]
fn zero_outcome_plan_is_durable_and_reloadable() {
    let database = temporary_database("zero-outcome");
    let store = StationStore::open(&database).unwrap();
    seed_authority(&store, 2);
    let authority = TestEvidenceAuthority::new();

    let applied = run_once(&store, &authority);
    let run = run_from(&applied);
    assert_eq!(run.plans.len(), 1);
    assert!(run.outcomes.is_empty());
    let run_id = run.run_id.clone();
    drop(store);

    let reopened = StationStore::open(&database).unwrap();
    let reloaded = reopened
        .read_schedule_materialization_run(&run_id)
        .unwrap()
        .unwrap();
    assert_eq!(reloaded.plans.len(), 1);
    assert!(reloaded.outcomes.is_empty());
}

#[test]
fn historical_non_overlapping_committed_rows_do_not_consume_overlap_capacity() {
    let database = temporary_database("historical-overlap");
    let store = seed_materializable(&database);
    drop(store);
    seed_historical_committed_rows(&database, 130);
    let store = StationStore::open(&database).unwrap();
    let authority = TestEvidenceAuthority::new();

    let outcome = run_once(&store, &authority);

    assert!(matches!(
        outcome,
        ScheduleMaterializationApplyOutcome::Applied(_)
    ));
    assert!(run_from(&outcome).outcomes.iter().any(|record| matches!(
        record.outcome,
        ScheduleMaterializationOutcome::Proposed { .. }
    )));
}

#[test]
fn tampered_origin_identity_fails_and_rolls_back_the_new_batch() {
    let database = temporary_database("origin-tamper");
    let store = seed_materializable(&database);
    let authority = TestEvidenceAuthority::new();
    let first = run_once(&store, &authority);
    let (occurrence_id, _) = proposed_item(run_from(&first));
    drop(store);
    let forged_occurrence = "f".repeat(64);
    let sql = format!(
        "DROP TRIGGER schedule_materialization_drafts_no_update;\
         UPDATE schedule_materialization_drafts SET occurrence_id='{forged}',\
           record_json=replace(record_json,'{original}','{forged}')",
        forged = forged_occurrence,
        original = occurrence_id,
    );
    assert_eq!(raw_sql_status(&database, &sql), SQLITE_OK);
    let runs_before = raw_i64(
        &database,
        "SELECT COUNT(*) FROM schedule_materialization_runs",
    );
    let outcomes_before = raw_i64(
        &database,
        "SELECT COUNT(*) FROM schedule_materialization_outcomes",
    );
    let store = StationStore::open(&database).unwrap();
    revise_catalog_title(&store);

    let error = store
        .materialize_schedule_batch_with_explicit_window(CHANNEL, &window(), "operator", &authority)
        .unwrap_err();

    assert!(matches!(
        error,
        ScheduleMaterializationWriteError::Collision(_)
    ));
    drop(store);
    assert_eq!(
        raw_i64(
            &database,
            "SELECT COUNT(*) FROM schedule_materialization_runs"
        ),
        runs_before
    );
    assert_eq!(
        raw_i64(
            &database,
            "SELECT COUNT(*) FROM schedule_materialization_outcomes"
        ),
        outcomes_before
    );
}

#[test]
fn manual_occupation_of_deterministic_uuid_fails_and_rolls_back_the_new_batch() {
    let database = temporary_database("manual-identity");
    let store = seed_materializable(&database);
    let authority = TestEvidenceAuthority::new();
    run_once(&store, &authority);
    drop(store);
    assert_eq!(
        raw_sql_status(
            &database,
            "DROP TRIGGER schedule_materialization_drafts_no_delete;DELETE FROM schedule_materialization_drafts",
        ),
        SQLITE_OK
    );
    let runs_before = raw_i64(
        &database,
        "SELECT COUNT(*) FROM schedule_materialization_runs",
    );
    let items_before = raw_i64(&database, "SELECT COUNT(*) FROM schedule_items");
    let store = StationStore::open(&database).unwrap();
    revise_catalog_title(&store);

    let error = store
        .materialize_schedule_batch_with_explicit_window(CHANNEL, &window(), "operator", &authority)
        .unwrap_err();

    assert!(matches!(
        error,
        ScheduleMaterializationWriteError::Collision(_)
    ));
    drop(store);
    assert_eq!(
        raw_i64(
            &database,
            "SELECT COUNT(*) FROM schedule_materialization_runs"
        ),
        runs_before
    );
    assert_eq!(
        raw_i64(&database, "SELECT COUNT(*) FROM schedule_items"),
        items_before
    );
}
