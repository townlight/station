use std::ffi::{CString, c_char, c_int, c_void};
use std::path::{Path, PathBuf};
use std::ptr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::to_string;
use sha2::{Digest, Sha256};
use station_catalog::{CatalogAssetRecord, CatalogEditorialState};
use station_domain::{
    AUTOMATIC_SCHEDULE_MATERIALIZATION_ACTOR_ID, AlertChannel, AlertChannelKind,
    AlertConditionKind, AlertEventState, redact_alert_channel_target,
};
use station_schedule::{
    AssetReadiness, ChannelConfiguration, MediaAsset, OutputProfileId, OutputSinkConfiguration,
    ScheduleItem, ScheduleState,
};
use station_schedule_automation::{
    AssetPickStrategy, AssetPredicate, DaypartBlock, LocalDate, MaterializationWindow,
    RecurrenceTimeZone, RecurringRule, SavedSearch, SavedSearchOrder, SavedSearchQuery,
    SourceEvidenceAuthority, SourceEvidenceKind, SourceEvidenceReceipt, SourceEvidenceVerifier,
};
use station_storage::{
    AlertChannelSecret, ScheduleItemWrite, ScheduleMaterializationApplyOutcome,
    ScheduleMaterializationFailureClass, ScheduleMaterializationHistoryCursor,
    ScheduleMaterializationHistoryReadError, ScheduleMaterializationOutcome,
    ScheduleMaterializationRun, ScheduleMaterializationRunOrigin,
    ScheduleMaterializationWriteError, StationStore,
};

const CHANNEL: &str = "11111111-1111-4111-8111-111111111111";
const OWNER: &str = "22222222-2222-4222-8222-222222222222";
const SEARCH: &str = "33333333-3333-4333-8333-333333333333";
const BLOCK: &str = "44444444-4444-4444-8444-444444444444";
const RULE: &str = "55555555-5555-4555-8555-555555555555";
const ASSET: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const OTHER_ASSET: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const ALERT_CHANNEL: &str = "77777777-7777-4777-8777-777777777777";
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
    observed_at_utc_ms: i64,
}

impl TestEvidenceAuthority {
    fn new() -> Self {
        Self::at(AUTHORITY_NOW)
    }

    fn at(observed_at_utc_ms: i64) -> Self {
        Self {
            sequence: AtomicU64::new(1),
            observed_at_utc_ms,
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
        Ok(self.observed_at_utc_ms)
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
            issued_at_utc_ms: self.observed_at_utc_ms,
            expires_at_utc_ms: self.observed_at_utc_ms + lifetime_ms,
            scope_sha256: scope_sha256.into(),
            attestation_sha256: Self::attestation(kind, &receipt_id, scope_sha256, payload_sha256),
        })
    }
}

fn seed_channel(store: &StationStore, enabled: bool) {
    store
        .put_channel(&ChannelConfiguration {
            channel_id: CHANNEL.into(),
            display_name: "Government".into(),
            output_profile: OutputProfileId::Proof360p30,
            output_sinks: vec![OutputSinkConfiguration::required_udp(
                CHANNEL,
                "Primary",
                "127.0.0.1:5500",
                -24.0,
            )],
            enabled,
        })
        .unwrap();
}

fn seed_raw_eligible_channels(store: &StationStore, database: &Path, count: usize) {
    store
        .write_saved_search(&search(), None, "operator", 1)
        .unwrap();
    let mut sql = String::new();
    for index in 0..count {
        let channel_id = format!("10000000-0000-4000-8000-{index:012}");
        let block_id = format!("20000000-0000-4000-8000-{index:012}");
        let rule_id = format!("30000000-0000-4000-8000-{index:012}");
        let block = DaypartBlock {
            block_id: block_id.clone(),
            channel_id: channel_id.clone(),
            label: format!("Automatic block {index}"),
            starts_local_minute: 0,
            ends_local_minute: 1_440,
            weekday_mask: 0b0111_1111,
            season_starts_on: None,
            season_ends_on: None,
            allow_rule_ids: vec![rule_id.clone()],
            revision: 1,
        };
        let rule = RecurringRule {
            rule_id: rule_id.clone(),
            channel_id: channel_id.clone(),
            saved_search_id: SEARCH.into(),
            saved_search_revision: 1,
            pick_strategy: AssetPickStrategy::Top,
            rolling_window_days: 14,
            avoid_repeat_within_days: None,
            enabled: true,
            priority: 1,
            block_id: block_id.clone(),
            block_revision: 1,
            weekday_mask: 0b0111_1111,
            interval_weeks: 1,
            anchor_date: LocalDate {
                year: 2099,
                month: 1,
                day: 1,
            },
            timezone: RecurrenceTimeZone::Utc,
            local_start_minute: 60,
            duration_minutes: 60,
            revision: 1,
        };
        let block_json = sql_quote(&to_string(&block).unwrap());
        let rule_json = sql_quote(&to_string(&rule).unwrap());
        sql.push_str(&format!(
            "INSERT INTO channels(channel_id,display_name,udp_destination,output_profile,enabled) \
             VALUES('{channel_id}','Channel {index}','127.0.0.1:5500','proof-360p30',1);\
             INSERT INTO schedule_automation_daypart_blocks(block_id,channel_id,revision,record_json) \
             VALUES('{block_id}','{channel_id}',1,'{block_json}');\
             INSERT INTO schedule_automation_daypart_block_history(block_id,channel_id,revision,record_json,changed_by,occurred_at_unix_ms) \
             VALUES('{block_id}','{channel_id}',1,'{block_json}','seed',1);\
             INSERT INTO schedule_automation_recurring_rules(rule_id,channel_id,saved_search_id,saved_search_revision,block_id,block_revision,revision,record_json) \
             VALUES('{rule_id}','{channel_id}','{search}',1,'{block_id}',1,1,'{rule_json}');\
             INSERT INTO schedule_automation_recurring_rule_history(rule_id,channel_id,saved_search_id,saved_search_revision,block_id,block_revision,revision,record_json,changed_by,occurred_at_unix_ms) \
             VALUES('{rule_id}','{channel_id}','{search}',1,'{block_id}',1,1,'{rule_json}','seed',1);",
            search = SEARCH,
        ));
    }
    assert_eq!(raw_sql_status(database, &sql), SQLITE_OK);
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
        .materialize_schedule_batch_with_explicit_window(CHANNEL, &window(), OWNER, authority)
        .unwrap()
}

fn run_window(
    store: &StationStore,
    authority: &TestEvidenceAuthority,
    starts_at_utc_ms: i64,
    actor: &str,
) -> ScheduleMaterializationRun {
    let outcome = store
        .materialize_schedule_batch_with_explicit_window(
            CHANNEL,
            &MaterializationWindow {
                starts_at_utc_ms,
                ends_at_utc_ms: starts_at_utc_ms + DAY_MS,
            },
            actor,
            authority,
        )
        .unwrap();
    run_from(&outcome).clone()
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

fn withdraw_candidate(store: &StationStore) {
    let mut withdrawn = store.read_catalog_asset(ASSET).unwrap().unwrap();
    withdrawn.editorial_state = CatalogEditorialState::Withdrawn;
    withdrawn.revision += 1;
    store
        .write_catalog_asset(
            ASSET,
            &withdrawn,
            Some(withdrawn.revision - 1),
            "catalog",
            7,
        )
        .unwrap();
}

fn put_alert_destination(store: &StationStore) {
    let target = "https://alerts.example.org/townlight";
    store
        .put_alert_channel(
            &AlertChannel {
                channel_id: ALERT_CHANNEL.into(),
                kind: AlertChannelKind::Webhook,
                label: "Schedule pager".into(),
                enabled: true,
                target_redacted: redact_alert_channel_target(AlertChannelKind::Webhook, target)
                    .unwrap(),
                credential_configured: true,
                quiet_hours_start_utc: None,
                quiet_hours_end_utc: None,
                created_at_unix_ms: 1,
                updated_at_unix_ms: 1,
            },
            &AlertChannelSecret {
                target: target.into(),
                endpoint: None,
                sender: None,
                identity: None,
                credential: "test-signing-secret".into(),
            },
        )
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
        .materialize_schedule_batch_with_explicit_window(CHANNEL, &window(), OWNER, &authority)
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
        .materialize_schedule_batch_with_explicit_window(CHANNEL, &window(), OWNER, &authority)
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

#[test]
fn no_match_fires_a_channel_scoped_s19_alert_and_a_later_match_resolves_it() {
    let database = temporary_database("no-match-alert");
    let store = StationStore::open(&database).unwrap();
    seed_authority(&store, 1);
    put_alert_destination(&store);
    let authority = TestEvidenceAuthority::new();

    let unmatched = run_once(&store, &authority);
    assert!(run_from(&unmatched).outcomes.iter().any(|record| matches!(
        record.outcome,
        ScheduleMaterializationOutcome::NoMatch { .. }
    )));
    let events = store.list_alert_events().unwrap();
    let firing = events
        .iter()
        .find(|event| {
            event.condition == AlertConditionKind::ScheduleNoMatch
                && event.resource_ref == CHANNEL
                && event.state == AlertEventState::Firing
        })
        .expect("a durable channel-scoped no-match alert must fire");
    let first_event_id = firing.event_id.clone();
    let first_occurrence_count = firing.occurrence_count;
    assert_eq!(firing.source_section, "S19");
    let operator_copy = format!("{} {}", firing.summary, firing.detail).to_ascii_lowercase();
    assert!(operator_copy.contains("no matching asset"));
    assert!(!operator_copy.contains("filler"));
    assert!(!operator_copy.contains("schedule gap"));
    assert!(!operator_copy.contains("missing media"));
    assert_eq!(
        raw_i64(&database, "SELECT COUNT(*) FROM alert_deliveries"),
        1
    );

    let exact_retry = run_once(&store, &authority);
    assert!(matches!(
        exact_retry,
        ScheduleMaterializationApplyOutcome::AlreadyApplied(_)
    ));
    let firing_after_retry = store
        .list_alert_events()
        .unwrap()
        .into_iter()
        .find(|event| event.event_id == first_event_id)
        .unwrap();
    assert_eq!(firing_after_retry.occurrence_count, first_occurrence_count);
    assert_eq!(
        raw_i64(&database, "SELECT COUNT(*) FROM alert_deliveries"),
        1
    );

    publish_candidate(&store, &database);
    let matched = run_once(&store, &authority);
    assert!(run_from(&matched).outcomes.iter().all(|record| !matches!(
        record.outcome,
        ScheduleMaterializationOutcome::NoMatch { .. }
    )));
    let events = store.list_alert_events().unwrap();
    assert!(!events.iter().any(|event| {
        event.condition == AlertConditionKind::ScheduleNoMatch
            && event.resource_ref == CHANNEL
            && event.state == AlertEventState::Firing
    }));
    assert!(events.iter().any(|event| {
        event.condition == AlertConditionKind::ScheduleNoMatch
            && event.resource_ref == CHANNEL
            && event.state == AlertEventState::Resolved
    }));
    assert_eq!(
        raw_i64(&database, "SELECT COUNT(*) FROM alert_deliveries"),
        2
    );

    withdraw_candidate(&store);
    let reopened = run_once(&store, &authority);
    assert!(matches!(
        reopened,
        ScheduleMaterializationApplyOutcome::AlreadyApplied(_)
    ));
    assert_eq!(run_from(&reopened).run_id, run_from(&unmatched).run_id);
    let events = store.list_alert_events().unwrap();
    let second_firing = events
        .iter()
        .find(|event| {
            event.condition == AlertConditionKind::ScheduleNoMatch
                && event.resource_ref == CHANNEL
                && event.state == AlertEventState::Firing
        })
        .expect("the identical durable no-match result must open a new alert episode");
    assert_ne!(second_firing.event_id, first_event_id);
    assert_eq!(second_firing.occurrence_count, 1);
    assert_eq!(
        events
            .iter()
            .filter(|event| event.condition == AlertConditionKind::ScheduleNoMatch)
            .count(),
        2
    );
    assert_eq!(
        raw_i64(&database, "SELECT COUNT(*) FROM alert_deliveries"),
        3
    );
}

#[test]
fn automatic_materialization_failures_are_channel_scoped_typed_deduplicated_and_resolved() {
    let database = temporary_database("automatic-failure-alert");
    let store = StationStore::open(&database).unwrap();
    put_alert_destination(&store);
    let failed_channel = CHANNEL;
    let unaffected_channel = "88888888-8888-4888-8888-888888888888";

    let default_rule = store
        .read_alert_rule("default-schedule-materialization-failure")
        .unwrap()
        .expect("the automatic materialization failure rule is seeded");
    assert_eq!(
        default_rule.severity,
        station_domain::AlertSeverity::Warning
    );
    assert!(default_rule.notify_on_resolve);

    let first = store
        .observe_schedule_materialization_failure_and_route(
            failed_channel,
            ScheduleMaterializationFailureClass::Storage,
            1_000,
        )
        .unwrap();
    assert_eq!(
        first.condition,
        AlertConditionKind::ScheduleMaterializationFailure
    );
    assert_eq!(first.resource_ref, failed_channel);
    assert_eq!(first.source_section, "S19");
    assert_eq!(first.severity, station_domain::AlertSeverity::Warning);
    let operator_copy = format!("{} {}", first.summary, first.detail).to_ascii_lowercase();
    assert!(operator_copy.contains("storage"));
    assert!(!operator_copy.contains("c:\\"));
    assert!(!operator_copy.contains("\\\\"));
    assert_eq!(
        raw_i64(&database, "SELECT COUNT(*) FROM alert_deliveries"),
        1
    );

    let retry = store
        .observe_schedule_materialization_failure_and_route(
            failed_channel,
            ScheduleMaterializationFailureClass::Storage,
            2_000,
        )
        .unwrap();
    assert_eq!(retry.event_id, first.event_id);
    assert_eq!(retry.occurrence_count, 1);
    assert_eq!(
        raw_i64(&database, "SELECT COUNT(*) FROM alert_deliveries"),
        1
    );

    let changed_class = store
        .observe_schedule_materialization_failure_and_route(
            failed_channel,
            ScheduleMaterializationFailureClass::Capacity,
            2_500,
        )
        .unwrap();
    assert_eq!(changed_class.event_id, first.event_id);
    assert_eq!(changed_class.occurrence_count, 2);
    assert_eq!(
        changed_class.detail,
        "Typed automatic materialization failure class: capacity."
    );
    assert_eq!(
        raw_i64(&database, "SELECT COUNT(*) FROM alert_deliveries"),
        1
    );

    let other = store
        .observe_schedule_materialization_failure_and_route(
            unaffected_channel,
            ScheduleMaterializationFailureClass::Capacity,
            2_000,
        )
        .unwrap();
    assert_ne!(other.event_id, first.event_id);
    assert_eq!(other.resource_ref, unaffected_channel);
    assert_eq!(
        raw_i64(&database, "SELECT COUNT(*) FROM alert_deliveries"),
        2
    );

    let resolved = store
        .resolve_alert(
            AlertConditionKind::ScheduleMaterializationFailure,
            failed_channel,
            3_000,
        )
        .unwrap()
        .expect("the next successful automatic run resolves its channel alert");
    assert_eq!(resolved.state, AlertEventState::Resolved);
    assert_eq!(
        raw_i64(&database, "SELECT COUNT(*) FROM alert_deliveries"),
        3
    );
    let events = store.list_alert_events().unwrap();
    assert!(events.iter().any(|event| {
        event.event_id == first.event_id && event.state == AlertEventState::Resolved
    }));
    assert!(events.iter().any(|event| {
        event.event_id == other.event_id && event.state == AlertEventState::Firing
    }));

    let reopened = store
        .observe_schedule_materialization_failure_and_route(
            failed_channel,
            ScheduleMaterializationFailureClass::Storage,
            4_000,
        )
        .unwrap();
    assert_ne!(reopened.event_id, first.event_id);
    assert_eq!(reopened.occurrence_count, 1);
    assert_eq!(
        raw_i64(&database, "SELECT COUNT(*) FROM alert_deliveries"),
        4
    );
}

#[test]
fn automatic_materialization_failure_outbox_rollback_leaves_no_durable_alert() {
    let database = temporary_database("automatic-failure-alert-rollback");
    let store = StationStore::open(&database).unwrap();
    put_alert_destination(&store);
    assert_eq!(
        raw_sql_status(
            &database,
            "CREATE TRIGGER force_automatic_failure_delivery_failure BEFORE INSERT ON alert_deliveries \
             WHEN NEW.notification_kind='firing' BEGIN SELECT RAISE(ABORT,'forced alert delivery failure'); END",
        ),
        SQLITE_OK
    );

    assert!(
        store
            .observe_schedule_materialization_failure_and_route(
                CHANNEL,
                ScheduleMaterializationFailureClass::Storage,
                1_000,
            )
            .is_err()
    );
    assert_eq!(raw_i64(&database, "SELECT COUNT(*) FROM alert_events"), 0);
    assert_eq!(
        raw_i64(&database, "SELECT COUNT(*) FROM alert_deliveries"),
        0
    );
}

#[test]
fn automatic_materialization_failure_resolution_rolls_back_then_can_be_retried() {
    let database = temporary_database("automatic-failure-resolution-retry");
    let store = StationStore::open(&database).unwrap();
    put_alert_destination(&store);
    let firing = store
        .observe_schedule_materialization_failure_and_route(
            CHANNEL,
            ScheduleMaterializationFailureClass::Storage,
            1_000,
        )
        .unwrap();
    assert_eq!(
        raw_sql_status(
            &database,
            "CREATE TRIGGER force_automatic_failure_resolution_delivery_failure BEFORE INSERT ON alert_deliveries \
             WHEN NEW.notification_kind='resolved' BEGIN SELECT RAISE(ABORT,'forced resolved delivery failure'); END",
        ),
        SQLITE_OK
    );

    assert!(
        store
            .resolve_alert(
                AlertConditionKind::ScheduleMaterializationFailure,
                CHANNEL,
                2_000,
            )
            .is_err()
    );
    let still_firing = store.read_alert_event(&firing.event_id).unwrap().unwrap();
    assert_eq!(still_firing.state, AlertEventState::Firing);
    assert_eq!(
        raw_i64(&database, "SELECT COUNT(*) FROM alert_deliveries"),
        1
    );

    assert_eq!(
        raw_sql_status(
            &database,
            "DROP TRIGGER force_automatic_failure_resolution_delivery_failure",
        ),
        SQLITE_OK
    );
    let resolved = store
        .resolve_alert(
            AlertConditionKind::ScheduleMaterializationFailure,
            CHANNEL,
            2_000,
        )
        .unwrap()
        .unwrap();
    assert_eq!(resolved.state, AlertEventState::Resolved);
    assert_eq!(
        raw_i64(&database, "SELECT COUNT(*) FROM alert_deliveries"),
        2
    );
}

#[test]
fn automatic_materialization_failure_realert_rollback_preserves_existing_firing_event() {
    let database = temporary_database("automatic-failure-realert-rollback");
    let store = StationStore::open(&database).unwrap();
    put_alert_destination(&store);
    let first = store
        .observe_schedule_materialization_failure_and_route(
            CHANNEL,
            ScheduleMaterializationFailureClass::Storage,
            1_000,
        )
        .unwrap();
    let delivery = store
        .claim_due_alert_delivery(1_000, 1_000)
        .unwrap()
        .expect("the firing delivery is due");
    store
        .complete_alert_delivery(&delivery.delivery_id, 1_000, None)
        .unwrap();
    assert_eq!(
        raw_sql_status(
            &database,
            "CREATE TRIGGER force_automatic_failure_realert_delivery_failure BEFORE INSERT ON alert_deliveries \
             WHEN NEW.notification_kind='realert' BEGIN SELECT RAISE(ABORT,'forced realert delivery failure'); END",
        ),
        SQLITE_OK
    );

    assert!(
        store
            .observe_schedule_materialization_failure_and_route(
                CHANNEL,
                ScheduleMaterializationFailureClass::Capacity,
                1_802_000,
            )
            .is_err()
    );
    let unchanged = store.read_alert_event(&first.event_id).unwrap().unwrap();
    assert_eq!(unchanged.state, AlertEventState::Firing);
    assert_eq!(unchanged.occurrence_count, 1);
    assert_eq!(
        unchanged.detail,
        "Typed automatic materialization failure class: storage."
    );
    assert_eq!(
        raw_i64(&database, "SELECT COUNT(*) FROM alert_deliveries"),
        1
    );
}

#[test]
fn multiple_no_match_slots_are_aggregated_into_one_alert_observation() {
    let database = temporary_database("no-match-alert-aggregate");
    let store = StationStore::open(&database).unwrap();
    seed_authority(&store, 1);
    let authority = TestEvidenceAuthority::new();
    let long_window = MaterializationWindow {
        starts_at_utc_ms: WINDOW_START,
        ends_at_utc_ms: WINDOW_START + 14 * DAY_MS,
    };

    let applied = store
        .materialize_schedule_batch_with_explicit_window(CHANNEL, &long_window, OWNER, &authority)
        .unwrap();

    let no_match_count = run_from(&applied)
        .outcomes
        .iter()
        .filter(|record| {
            matches!(
                record.outcome,
                ScheduleMaterializationOutcome::NoMatch { .. }
            )
        })
        .count();
    assert!(no_match_count > 1);
    let alerts = store
        .list_alert_events()
        .unwrap()
        .into_iter()
        .filter(|event| event.condition == AlertConditionKind::ScheduleNoMatch)
        .collect::<Vec<_>>();
    assert_eq!(alerts.len(), 1);
    assert_eq!(alerts[0].occurrence_count, 1);
    assert!(alerts[0].summary.contains(&no_match_count.to_string()));
}

#[test]
fn no_match_alert_routing_failure_rolls_back_the_complete_materialization() {
    let database = temporary_database("no-match-alert-rollback");
    let store = StationStore::open(&database).unwrap();
    seed_authority(&store, 1);
    assert_eq!(
        raw_sql_status(
            &database,
            "DELETE FROM alert_rules WHERE condition='schedule-no-match'",
        ),
        SQLITE_OK
    );
    let authority = TestEvidenceAuthority::new();

    let error = store
        .materialize_schedule_batch_with_explicit_window(CHANNEL, &window(), OWNER, &authority)
        .unwrap_err();

    assert!(matches!(
        error,
        ScheduleMaterializationWriteError::Storage(_)
    ));
    drop(store);
    assert_eq!(
        raw_i64(
            &database,
            "SELECT COUNT(*) FROM schedule_materialization_runs"
        ),
        0
    );
    assert_eq!(raw_i64(&database, "SELECT COUNT(*) FROM alert_events"), 0);
}

#[test]
fn resolution_outbox_failure_rolls_back_the_new_run_and_keeps_no_match_firing() {
    let database = temporary_database("no-match-resolution-rollback");
    let store = StationStore::open(&database).unwrap();
    seed_authority(&store, 1);
    put_alert_destination(&store);
    let authority = TestEvidenceAuthority::new();
    let unmatched = run_once(&store, &authority);
    assert!(matches!(
        unmatched,
        ScheduleMaterializationApplyOutcome::Applied(_)
    ));
    publish_candidate(&store, &database);
    assert_eq!(
        raw_sql_status(
            &database,
            "CREATE TRIGGER force_resolved_delivery_failure BEFORE INSERT ON alert_deliveries \
             WHEN NEW.notification_kind='resolved' BEGIN SELECT RAISE(ABORT,'forced resolved delivery failure'); END",
        ),
        SQLITE_OK
    );

    let error = store
        .materialize_schedule_batch_with_explicit_window(CHANNEL, &window(), OWNER, &authority)
        .unwrap_err();

    assert!(matches!(
        error,
        ScheduleMaterializationWriteError::Storage(_)
    ));
    assert_eq!(
        raw_i64(
            &database,
            "SELECT COUNT(*) FROM schedule_materialization_runs"
        ),
        1
    );
    assert_eq!(raw_i64(&database, "SELECT COUNT(*) FROM schedule_items"), 0);
    assert_eq!(
        raw_i64(&database, "SELECT COUNT(*) FROM alert_deliveries"),
        1
    );
    let events = store.list_alert_events().unwrap();
    assert!(events.iter().any(|event| {
        event.condition == AlertConditionKind::ScheduleNoMatch
            && event.resource_ref == CHANNEL
            && event.state == AlertEventState::Firing
    }));
    assert!(!events.iter().any(|event| {
        event.condition == AlertConditionKind::ScheduleNoMatch
            && event.state == AlertEventState::Resolved
    }));
}

#[test]
fn production_materialization_rechecks_enabled_channel_inside_its_write_transaction() {
    let database = temporary_database("disabled-production-channel");
    let store = StationStore::open(&database).unwrap();
    seed_channel(&store, true);
    seed_authority(&store, 1);
    let enumerated = store
        .list_automatic_materialization_channel_checkpoints()
        .unwrap();
    assert_eq!(enumerated.len(), 1);
    assert_eq!(enumerated[0].channel_id, CHANNEL);
    seed_channel(&store, false);
    let authority = TestEvidenceAuthority::at(current_utc_ms());

    let error = store
        .materialize_scheduled_channel(
            CHANNEL,
            AUTOMATIC_SCHEDULE_MATERIALIZATION_ACTOR_ID,
            &authority,
        )
        .unwrap_err();

    assert!(matches!(
        error,
        ScheduleMaterializationWriteError::StaleAuthority(_)
    ));
    assert_eq!(
        raw_i64(
            &database,
            "SELECT COUNT(*) FROM schedule_materialization_runs"
        ),
        0
    );
    assert_eq!(raw_i64(&database, "SELECT COUNT(*) FROM schedule_items"), 0);
    assert_eq!(raw_i64(&database, "SELECT COUNT(*) FROM alert_events"), 0);
}

#[test]
fn automatic_checkpoint_scan_is_complete_beyond_128_valid_channels() {
    let database = temporary_database("automatic-checkpoint-over-128");
    let store = StationStore::open(&database).unwrap();
    seed_raw_eligible_channels(&store, &database, 129);

    let checkpoints = store
        .list_automatic_materialization_channel_checkpoints()
        .unwrap();

    assert_eq!(checkpoints.len(), 129);
    assert!(
        checkpoints
            .iter()
            .all(|checkpoint| checkpoint.latest_run.is_none())
    );
}

#[test]
fn automatic_checkpoint_scan_rejects_257_eligible_channels_without_partial_results() {
    let database = temporary_database("automatic-checkpoint-over-channel-cap");
    let store = StationStore::open(&database).unwrap();
    seed_raw_eligible_channels(&store, &database, 257);

    let error = store
        .list_automatic_materialization_channel_checkpoints()
        .unwrap_err();

    assert!(error.contains("channel capacity"));
}

#[test]
fn automatic_checkpoint_scan_rejects_the_16385th_rule_before_parsing_partial_authority() {
    let database = temporary_database("automatic-checkpoint-over-rule-cap");
    let store = StationStore::open(&database).unwrap();
    assert_eq!(
        raw_sql_status(
            &database,
            "WITH RECURSIVE n(x) AS (SELECT 0 UNION ALL SELECT x+1 FROM n WHERE x<16384) \
             INSERT INTO schedule_automation_recurring_rules(\
               rule_id,channel_id,saved_search_id,saved_search_revision,block_id,block_revision,revision,record_json) \
             SELECT printf('rule-%05d',x),'channel','search',1,'block',1,1,'{}' FROM n",
        ),
        SQLITE_OK
    );

    let error = store
        .list_automatic_materialization_channel_checkpoints()
        .unwrap_err();

    assert!(error.contains("recurring-rule capacity"));
}

#[test]
fn automatic_checkpoint_list_includes_a_manual_run_and_survives_reopen() {
    let database = temporary_database("automatic-checkpoint-reopen");
    let store = StationStore::open(&database).unwrap();
    seed_channel(&store, true);
    seed_authority(&store, 1);
    let empty = store
        .list_automatic_materialization_channel_checkpoints()
        .unwrap();
    assert_eq!(empty.len(), 1);
    assert_eq!(empty[0].channel_id, CHANNEL);
    assert!(empty[0].latest_run.is_none());
    let authority = TestEvidenceAuthority::at(current_utc_ms());
    let manual = store
        .materialize_scheduled_channel(CHANNEL, OWNER, &authority)
        .unwrap();
    let run_id = run_from(&manual).run_id.clone();

    let checkpoints = store
        .list_automatic_materialization_channel_checkpoints()
        .unwrap();
    assert_eq!(checkpoints.len(), 1);
    assert_eq!(checkpoints[0].channel_id, CHANNEL);
    assert_eq!(checkpoints[0].latest_run.as_ref().unwrap().run_id, run_id);
    drop(store);

    let reopened = StationStore::open(&database).unwrap();
    let checkpoints = reopened
        .list_automatic_materialization_channel_checkpoints()
        .unwrap();
    assert_eq!(checkpoints.len(), 1);
    assert_eq!(checkpoints[0].latest_run.as_ref().unwrap().run_id, run_id);
}

#[test]
fn corrupt_newest_checkpoint_fails_instead_of_falling_back_to_an_older_run() {
    let database = temporary_database("automatic-checkpoint-corrupt-newest");
    let store = StationStore::open(&database).unwrap();
    seed_channel(&store, true);
    seed_authority(&store, 1);
    let now = current_utc_ms();
    let first = store
        .materialize_scheduled_channel(CHANNEL, OWNER, &TestEvidenceAuthority::at(now))
        .unwrap();
    publish_candidate(&store, &database);
    let second = store
        .materialize_scheduled_channel(CHANNEL, OWNER, &TestEvidenceAuthority::at(now + 1))
        .unwrap();
    assert_ne!(run_from(&first).run_id, run_from(&second).run_id);
    let newest_run_id = run_from(&second).run_id.clone();
    drop(store);
    let sql = format!(
        "DROP TRIGGER schedule_materialization_plans_no_update;\
         UPDATE schedule_materialization_plans SET record_json=replace(record_json,'{rule}','bad') \
         WHERE run_id='{run_id}'",
        rule = RULE,
        run_id = newest_run_id,
    );
    assert_eq!(raw_sql_status(&database, &sql), SQLITE_OK);

    let reopened = StationStore::open(&database).unwrap();
    assert!(
        reopened
            .list_automatic_materialization_channel_checkpoints()
            .is_err(),
        "newest checkpoint corruption must fail closed, never return the older valid run"
    );
}

#[test]
fn rebound_newest_checkpoint_binding_fails_instead_of_falling_back() {
    let database = temporary_database("automatic-checkpoint-rebound-binding");
    let store = StationStore::open(&database).unwrap();
    seed_channel(&store, true);
    seed_authority(&store, 1);
    let now = current_utc_ms();
    let first = store
        .materialize_scheduled_channel(CHANNEL, OWNER, &TestEvidenceAuthority::at(now))
        .unwrap();
    publish_candidate(&store, &database);
    let second = store
        .materialize_scheduled_channel(CHANNEL, OWNER, &TestEvidenceAuthority::at(now + 1))
        .unwrap();
    assert_ne!(run_from(&first).run_id, run_from(&second).run_id);
    let newest_run_id = run_from(&second).run_id.clone();
    let rebound_channel = "99999999-0000-4000-8000-999999999999";
    let sql = format!(
        "DROP TRIGGER schedule_materialization_run_channels_no_update;\
         UPDATE schedule_materialization_run_channels SET channel_id='{rebound}' WHERE run_id='{run_id}'",
        rebound = rebound_channel,
        run_id = newest_run_id,
    );
    assert_eq!(raw_sql_status(&database, &sql), SQLITE_OK);

    assert!(
        store
            .list_automatic_materialization_channel_checkpoints()
            .is_err(),
        "a rebound newest binding must not expose the older checkpoint"
    );
}

#[test]
fn deleted_newest_checkpoint_binding_fails_instead_of_falling_back() {
    let database = temporary_database("automatic-checkpoint-deleted-binding");
    let store = StationStore::open(&database).unwrap();
    seed_channel(&store, true);
    seed_authority(&store, 1);
    let now = current_utc_ms();
    let first = store
        .materialize_scheduled_channel(CHANNEL, OWNER, &TestEvidenceAuthority::at(now))
        .unwrap();
    publish_candidate(&store, &database);
    let second = store
        .materialize_scheduled_channel(CHANNEL, OWNER, &TestEvidenceAuthority::at(now + 1))
        .unwrap();
    assert_ne!(run_from(&first).run_id, run_from(&second).run_id);
    let newest_run_id = run_from(&second).run_id.clone();
    let sql = format!(
        "DROP TRIGGER schedule_materialization_run_channels_no_delete;\
         DELETE FROM schedule_materialization_run_channels WHERE run_id='{newest_run_id}'",
    );
    assert_eq!(raw_sql_status(&database, &sql), SQLITE_OK);

    assert!(
        store
            .list_automatic_materialization_channel_checkpoints()
            .is_err(),
        "a deleted newest binding must not expose the older checkpoint"
    );
}

#[test]
fn tampered_newest_checkpoint_binding_time_fails_closed() {
    let database = temporary_database("automatic-checkpoint-tampered-binding-time");
    let store = StationStore::open(&database).unwrap();
    seed_channel(&store, true);
    seed_authority(&store, 1);
    let run = store
        .materialize_scheduled_channel(CHANNEL, OWNER, &TestEvidenceAuthority::at(current_utc_ms()))
        .unwrap();
    let run_id = run_from(&run).run_id.clone();
    let sql = format!(
        "DROP TRIGGER schedule_materialization_run_channels_no_update;\
         UPDATE schedule_materialization_run_channels SET occurred_at_unix_ms=occurred_at_unix_ms+1 WHERE run_id='{run_id}'",
    );
    assert_eq!(raw_sql_status(&database, &sql), SQLITE_OK);

    assert!(
        store
            .list_automatic_materialization_channel_checkpoints()
            .is_err(),
        "a binding timestamp contradiction must fail closed"
    );
}

fn current_utc_ms() -> i64 {
    i64::try_from(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis(),
    )
    .unwrap()
}

#[test]
fn materialization_history_uses_exclusive_keyset_pagination_for_timestamp_ties() {
    let database = temporary_database("history-keyset-ties");
    let store = seed_materializable(&database);
    let authority = TestEvidenceAuthority::new();
    let runs = [0_i64, 1, 2]
        .into_iter()
        .map(|day| run_window(&store, &authority, WINDOW_START + day * DAY_MS, OWNER))
        .collect::<Vec<_>>();
    let mut expected = runs
        .iter()
        .map(|run| (run.occurred_at_unix_ms, run.run_id.clone()))
        .collect::<Vec<_>>();
    expected.sort_by(|left, right| right.cmp(left));

    let first = store
        .list_schedule_materialization_history(CHANNEL, 1, None)
        .unwrap();
    let second = store
        .list_schedule_materialization_history(CHANNEL, 1, first.next_cursor.as_ref())
        .unwrap();
    let third = store
        .list_schedule_materialization_history(CHANNEL, 1, second.next_cursor.as_ref())
        .unwrap();
    let actual = [&first, &second, &third]
        .into_iter()
        .map(|page| {
            assert_eq!(page.entries.len(), 1);
            (
                page.entries[0].run.occurred_at_unix_ms,
                page.entries[0].run.run_id.clone(),
            )
        })
        .collect::<Vec<_>>();
    assert_eq!(actual, expected);
    assert_eq!(third.next_cursor, None);
}

#[test]
fn materialization_history_rejects_invalid_limits_and_cursors() {
    let database = temporary_database("history-limits");
    let store = seed_materializable(&database);
    assert!(matches!(
        store.list_schedule_materialization_history(CHANNEL, 0, None),
        Err(ScheduleMaterializationHistoryReadError::Invalid(_))
    ));
    assert!(matches!(
        store.list_schedule_materialization_history(CHANNEL, 129, None),
        Err(ScheduleMaterializationHistoryReadError::Invalid(_))
    ));
    assert!(matches!(
        store.list_schedule_materialization_history(
            CHANNEL,
            1,
            Some(&ScheduleMaterializationHistoryCursor {
                occurred_at_unix_ms: -1,
                run_id: "11111111-1111-4111-8111-111111111111".into(),
            }),
        ),
        Err(ScheduleMaterializationHistoryReadError::Invalid(_))
    ));
    assert!(matches!(
        store.list_schedule_materialization_history(
            CHANNEL,
            1,
            Some(&ScheduleMaterializationHistoryCursor {
                occurred_at_unix_ms: 0,
                run_id: "11111111-1111-4111-8111-111111111111".into(),
            }),
        ),
        Err(ScheduleMaterializationHistoryReadError::Invalid(_))
    ));
}

#[test]
fn materialization_requires_canonical_actor_and_accepts_the_reserved_automatic_actor() {
    let database = temporary_database("canonical-materialization-actor");
    let store = seed_materializable(&database);
    let authority = TestEvidenceAuthority::new();

    assert!(matches!(
        store.materialize_schedule_batch_with_explicit_window(
            CHANNEL,
            &window(),
            "manual-operator",
            &authority,
        ),
        Err(ScheduleMaterializationWriteError::Invalid(_))
    ));
    seed_channel(&store, true);
    assert!(matches!(
        store.materialize_scheduled_channel(
            CHANNEL,
            "manual-operator",
            &TestEvidenceAuthority::at(current_utc_ms()),
        ),
        Err(ScheduleMaterializationWriteError::Invalid(_))
    ));

    let automatic = store
        .materialize_schedule_batch_with_explicit_window(
            CHANNEL,
            &window(),
            AUTOMATIC_SCHEDULE_MATERIALIZATION_ACTOR_ID,
            &authority,
        )
        .unwrap();
    assert_eq!(
        run_from(&automatic).changed_by,
        AUTOMATIC_SCHEDULE_MATERIALIZATION_ACTOR_ID
    );
}

#[test]
fn durable_materialization_run_reader_rejects_a_noncanonical_actor() {
    let database = temporary_database("noncanonical-durable-materialization-actor");
    let store = seed_materializable(&database);
    let run = run_once(&store, &TestEvidenceAuthority::new());
    let run_id = run_from(&run).run_id.clone();
    let sql = format!(
        "DROP TRIGGER schedule_materialization_runs_no_update;\
         UPDATE schedule_materialization_runs SET changed_by='manual-operator' WHERE run_id='{run_id}'"
    );
    assert_eq!(raw_sql_status(&database, &sql), SQLITE_OK);

    assert!(store.read_schedule_materialization_run(&run_id).is_err());
    assert!(matches!(
        store.list_schedule_materialization_history(CHANNEL, 1, None),
        Err(ScheduleMaterializationHistoryReadError::Integrity(_))
    ));
}

#[test]
fn materialization_history_preserves_a_forced_sqlite_failure_as_storage() {
    let database = temporary_database("history-storage-error");
    let store = seed_materializable(&database);
    assert_eq!(
        raw_sql_status(&database, "DROP TABLE schedule_materialization_plans"),
        SQLITE_OK
    );
    assert!(matches!(
        store.list_schedule_materialization_history(CHANNEL, 1, None),
        Err(ScheduleMaterializationHistoryReadError::Storage(_))
    ));
}

#[test]
fn materialization_history_preserves_nested_sqlite_failures_as_storage() {
    for (suffix, table) in [
        ("full-run", "schedule_materialization_outcomes"),
        ("draft-origin", "schedule_materialization_drafts"),
        ("current-schedule", "schedule_items"),
    ] {
        let database = temporary_database(&format!("history-storage-error-{suffix}"));
        let store = seed_materializable(&database);
        run_once(&store, &TestEvidenceAuthority::new());
        assert_eq!(
            raw_sql_status(&database, &format!("DROP TABLE {table}")),
            SQLITE_OK
        );
        assert!(matches!(
            store.list_schedule_materialization_history(CHANNEL, 1, None),
            Err(ScheduleMaterializationHistoryReadError::Storage(_))
        ));
    }
}

#[test]
fn materialization_history_exposes_manual_and_reserved_automatic_origin_truthfully() {
    let database = temporary_database("history-origins");
    let store = seed_materializable(&database);
    let authority = TestEvidenceAuthority::new();
    let manual = run_window(&store, &authority, WINDOW_START, OWNER);
    let automatic = run_window(
        &store,
        &authority,
        WINDOW_START + DAY_MS,
        AUTOMATIC_SCHEDULE_MATERIALIZATION_ACTOR_ID,
    );

    let page = store
        .list_schedule_materialization_history(CHANNEL, 2, None)
        .unwrap();
    let manual_entry = page
        .entries
        .iter()
        .find(|entry| entry.run.run_id == manual.run_id)
        .unwrap();
    assert_eq!(
        manual_entry.origin,
        ScheduleMaterializationRunOrigin::Operator
    );
    assert_eq!(manual_entry.run.changed_by, OWNER);
    let automatic_entry = page
        .entries
        .iter()
        .find(|entry| entry.run.run_id == automatic.run_id)
        .unwrap();
    assert_eq!(
        automatic_entry.origin,
        ScheduleMaterializationRunOrigin::Automatic
    );
    assert_eq!(
        automatic_entry.run.changed_by,
        AUTOMATIC_SCHEDULE_MATERIALIZATION_ACTOR_ID
    );
}

#[test]
fn materialization_history_fails_closed_for_rebound_channel_binding_and_does_not_leak() {
    let database = temporary_database("history-rebound-channel");
    let store = seed_materializable(&database);
    let run = run_once(&store, &TestEvidenceAuthority::new());
    let run_id = run_from(&run).run_id.clone();
    let other_channel = "99999999-0000-4000-8000-999999999999";
    assert!(
        store
            .list_schedule_materialization_history(other_channel, 1, None)
            .unwrap()
            .entries
            .is_empty()
    );
    let sql = format!(
        "DROP TRIGGER schedule_materialization_run_channels_no_update;\
         UPDATE schedule_materialization_run_channels SET channel_id='{other_channel}' \
         WHERE run_id='{run_id}'"
    );
    assert_eq!(raw_sql_status(&database, &sql), SQLITE_OK);

    assert!(matches!(
        store.list_schedule_materialization_history(other_channel, 1, None),
        Err(ScheduleMaterializationHistoryReadError::Integrity(_))
    ));
}

#[test]
fn materialization_history_does_not_skip_a_run_with_a_deleted_channel_binding() {
    let database = temporary_database("history-deleted-binding");
    let store = seed_materializable(&database);
    let run = run_once(&store, &TestEvidenceAuthority::new());
    let run_id = run_from(&run).run_id.clone();
    let sql = format!(
        "DROP TRIGGER schedule_materialization_run_channels_no_delete;\
         DELETE FROM schedule_materialization_run_channels WHERE run_id='{run_id}'"
    );
    assert_eq!(raw_sql_status(&database, &sql), SQLITE_OK);

    assert!(matches!(
        store.list_schedule_materialization_history(CHANNEL, 1, None),
        Err(ScheduleMaterializationHistoryReadError::Integrity(_))
    ));
}

#[test]
fn opening_fails_when_a_legacy_run_is_unbound_or_its_plan_count_is_incomplete() {
    let database = temporary_database("history-open-audit-unbound");
    let store = seed_materializable(&database);
    let run = run_once(&store, &TestEvidenceAuthority::new());
    let run_id = run_from(&run).run_id.clone();
    let sql = format!(
        "DROP TRIGGER schedule_materialization_run_channels_no_delete;\
         DROP TRIGGER schedule_materialization_plans_no_delete;\
         DELETE FROM schedule_materialization_run_channels WHERE run_id='{run_id}';\
         DELETE FROM schedule_materialization_plans WHERE run_id='{run_id}'"
    );
    assert_eq!(raw_sql_status(&database, &sql), SQLITE_OK);
    drop(store);
    assert!(
        StationStore::open(&database).is_err(),
        "a run with neither binding nor plan evidence must not disappear from history"
    );

    let database = temporary_database("history-open-audit-plan-count");
    let store = seed_materializable(&database);
    let run = run_once(&store, &TestEvidenceAuthority::new());
    let run_id = run_from(&run).run_id.clone();
    let sql = format!(
        "DROP TRIGGER schedule_materialization_run_channels_no_delete;\
         DROP TRIGGER schedule_materialization_runs_no_update;\
         DELETE FROM schedule_materialization_run_channels WHERE run_id='{run_id}';\
         UPDATE schedule_materialization_runs SET plan_count=2 WHERE run_id='{run_id}'"
    );
    assert_eq!(raw_sql_status(&database, &sql), SQLITE_OK);
    drop(store);
    assert!(
        StationStore::open(&database).is_err(),
        "an incomplete legacy plan count must not receive a guessed channel binding"
    );
}

#[test]
fn materialization_history_never_skips_a_corrupt_newest_run() {
    let database = temporary_database("history-corrupt-run");
    let store = seed_materializable(&database);
    let authority = TestEvidenceAuthority::new();
    let older = run_window(&store, &authority, WINDOW_START, OWNER);
    let newest = run_window(&store, &authority, WINDOW_START + DAY_MS, OWNER);
    let sql = format!(
        "DROP TRIGGER schedule_materialization_plans_no_update;\
         UPDATE schedule_materialization_plans SET record_json='{{}}' \
         WHERE run_id='{}'",
        newest.run_id
    );
    assert_eq!(raw_sql_status(&database, &sql), SQLITE_OK);

    assert!(matches!(
        store.list_schedule_materialization_history(CHANNEL, 1, None),
        Err(ScheduleMaterializationHistoryReadError::Integrity(_))
    ));
    assert!(
        store
            .read_schedule_materialization_run(&older.run_id)
            .unwrap()
            .is_some()
    );
}

#[test]
fn materialization_history_rejects_corrupt_draft_origin_and_current_schedule_linkage() {
    let database = temporary_database("history-draft-integrity");
    let store = seed_materializable(&database);
    let first = run_once(&store, &TestEvidenceAuthority::new());
    let first_run = run_from(&first);
    let (_, first_item_id) = proposed_item(first_run);
    let origin_sql = format!(
        "DROP TRIGGER schedule_materialization_drafts_no_update;\
         UPDATE schedule_materialization_drafts SET record_json='{{}}' \
         WHERE schedule_item_id='{first_item_id}'"
    );
    assert_eq!(raw_sql_status(&database, &origin_sql), SQLITE_OK);
    assert!(matches!(
        store.list_schedule_materialization_history(CHANNEL, 1, None),
        Err(ScheduleMaterializationHistoryReadError::Integrity(_))
    ));
    drop(store);

    let database = temporary_database("history-current-linkage");
    let store = seed_materializable(&database);
    let run = run_once(&store, &TestEvidenceAuthority::new());
    let (_, item_id) = proposed_item(run_from(&run));
    let linkage_sql = format!(
        "UPDATE schedule_items SET channel_id='99999999-0000-4000-8000-999999999999' \
         WHERE item_id='{item_id}'"
    );
    assert_eq!(raw_sql_status(&database, &linkage_sql), SQLITE_OK);
    assert!(matches!(
        store.list_schedule_materialization_history(CHANNEL, 1, None),
        Err(ScheduleMaterializationHistoryReadError::Integrity(_))
    ));
}
