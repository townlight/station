use std::ffi::{CString, c_char, c_int, c_void};
use std::path::{Path, PathBuf};
use std::ptr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use station_schedule_automation::{
    AssetPickStrategy, AssetPredicate, CustomMetadataPredicate, DaypartBlock, LocalDate,
    MetadataValue, RecurrenceTimeZone, RecurringRule, SavedSearch, SavedSearchOrder,
    SavedSearchQuery,
};
use station_storage::{ScheduleAutomationWriteError, StationStore};

const CHANNEL: &str = "11111111-1111-4111-8111-111111111111";
const SECOND_CHANNEL: &str = "66666666-6666-4666-8666-666666666666";
const OWNER: &str = "22222222-2222-4222-8222-222222222222";
const SEARCH: &str = "33333333-3333-4333-8333-333333333333";
const BLOCK: &str = "44444444-4444-4444-8444-444444444444";
const RULE: &str = "55555555-5555-4555-8555-555555555555";
const SECOND_BLOCK: &str = "77777777-7777-4777-8777-777777777777";
const SECOND_RULE: &str = "88888888-8888-4888-8888-888888888888";
static SEQUENCE: AtomicU64 = AtomicU64::new(0);

const SQLITE_OK: c_int = 0;
const SQLITE_OPEN_READWRITE: c_int = 2;

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
    fn sqlite3_close(database: *mut c_void) -> c_int;
}

fn raw_sql_status(database_path: &Path, sql: &str) -> c_int {
    let filename = CString::new(database_path.to_string_lossy().as_bytes()).unwrap();
    let sql = CString::new(sql).unwrap();
    let mut database = ptr::null_mut();
    // SAFETY: both inputs are live C strings, the output slot is valid, and the
    // handle is closed before this function returns.
    let open = unsafe {
        sqlite3_open_v2(
            filename.as_ptr(),
            &mut database,
            SQLITE_OPEN_READWRITE,
            ptr::null(),
        )
    };
    assert_eq!(open, SQLITE_OK);
    // SAFETY: the handle is live, SQL is a valid C string, and no callback is used.
    let status = unsafe {
        sqlite3_exec(
            database,
            sql.as_ptr(),
            ptr::null_mut(),
            ptr::null_mut(),
            ptr::null_mut(),
        )
    };
    // SAFETY: the handle was opened above and has not yet been closed.
    assert_eq!(unsafe { sqlite3_close(database) }, SQLITE_OK);
    status
}

fn temporary_database(name: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let sequence = SEQUENCE.fetch_add(1, Ordering::Relaxed);
    std::env::temp_dir().join(format!("townlight-s19-{name}-{nonce}-{sequence}.db"))
}

fn search(revision: u64) -> SavedSearch {
    SavedSearch {
        search_id: SEARCH.into(),
        name: "Meeting programs".into(),
        owner_operator_id: OWNER.into(),
        created_at_utc_ms: 1_725_000_000_000,
        query: SavedSearchQuery {
            all_of: vec![AssetPredicate::Custom {
                field: "program_type".into(),
                predicate: CustomMetadataPredicate::Equals(MetadataValue::Choice("meeting".into())),
            }],
            any_of: vec![],
            order_by: SavedSearchOrder::PublishedNewest,
            limit: 20,
        },
        revision,
    }
}

fn block(revision: u64) -> DaypartBlock {
    DaypartBlock {
        block_id: BLOCK.into(),
        channel_id: CHANNEL.into(),
        label: "Evening meetings".into(),
        starts_local_minute: 1_080,
        ends_local_minute: 1_320,
        weekday_mask: 0b0111_1111,
        season_starts_on: None,
        season_ends_on: None,
        allow_rule_ids: vec![RULE.into()],
        revision,
    }
}

fn rule(revision: u64, search_revision: u64, block_revision: u64) -> RecurringRule {
    RecurringRule {
        rule_id: RULE.into(),
        channel_id: CHANNEL.into(),
        saved_search_id: SEARCH.into(),
        saved_search_revision: search_revision,
        pick_strategy: AssetPickStrategy::LeastRecentlyAired,
        rolling_window_days: 21,
        avoid_repeat_within_days: Some(14),
        enabled: true,
        priority: 10,
        block_id: BLOCK.into(),
        block_revision,
        weekday_mask: 0b0000_0010,
        interval_weeks: 1,
        anchor_date: LocalDate {
            year: 2026,
            month: 8,
            day: 24,
        },
        timezone: RecurrenceTimeZone::Utc,
        local_start_minute: 1_140,
        duration_minutes: 60,
        revision,
    }
}

fn second_block(revision: u64) -> DaypartBlock {
    let mut value = block(revision);
    value.block_id = SECOND_BLOCK.into();
    value.channel_id = SECOND_CHANNEL.into();
    value.allow_rule_ids = vec![SECOND_RULE.into()];
    value
}

fn second_rule(revision: u64, search_revision: u64, block_revision: u64) -> RecurringRule {
    let mut value = rule(revision, search_revision, block_revision);
    value.rule_id = SECOND_RULE.into();
    value.channel_id = SECOND_CHANNEL.into();
    value.block_id = SECOND_BLOCK.into();
    value
}

fn seed(store: &StationStore) {
    store
        .write_saved_search(&search(1), None, "operator", 10)
        .unwrap();
    store
        .write_daypart_block(&block(1), None, "operator", 11)
        .unwrap();
    store
        .write_recurring_rule(&rule(1, 1, 1), None, "operator", 12)
        .unwrap();
}

#[test]
fn current_and_exact_historical_authority_round_trip() {
    let store = StationStore::open(temporary_database("roundtrip")).unwrap();
    seed(&store);
    let mut next_search = search(2);
    next_search.query.limit = 12;
    store
        .write_saved_search(&next_search, Some(1), "operator", 20)
        .unwrap();
    let mut next_block = block(2);
    next_block.label = "Prime meetings".into();
    store
        .write_daypart_block(&next_block, Some(1), "operator", 21)
        .unwrap();
    let next_rule = rule(2, 2, 2);
    store
        .write_recurring_rule(&next_rule, Some(1), "operator", 22)
        .unwrap();

    assert_eq!(store.read_saved_search(SEARCH).unwrap(), Some(next_search));
    assert_eq!(
        store.read_saved_search_revision(SEARCH, 1).unwrap(),
        Some(search(1))
    );
    assert_eq!(
        store.read_daypart_block_revision(BLOCK, 1).unwrap(),
        Some(block(1))
    );
    assert_eq!(
        store.read_recurring_rule_revision(RULE, 1).unwrap(),
        Some(rule(1, 1, 1))
    );
    assert_eq!(
        store.list_saved_search_history(SEARCH, 8).unwrap()[0].changed_by,
        "operator"
    );
    assert_eq!(store.list_daypart_block_history(BLOCK, 8).unwrap().len(), 2);
    assert_eq!(store.list_recurring_rule_history(RULE, 8).unwrap().len(), 2);
    assert_eq!(store.list_saved_searches(8).unwrap().len(), 1);
    assert_eq!(store.list_daypart_blocks(CHANNEL, 8).unwrap().len(), 1);
    assert_eq!(
        store.list_recurring_rules(CHANNEL, 8).unwrap(),
        vec![next_rule]
    );
}

#[test]
fn saved_searches_are_station_global_and_keep_creation_identity() {
    let store = StationStore::open(temporary_database("station-global-search")).unwrap();
    store
        .write_saved_search(&search(1), None, "operator", 10)
        .unwrap();
    store
        .write_daypart_block(&block(1), None, "operator", 11)
        .unwrap();
    store
        .write_daypart_block(&second_block(1), None, "operator", 12)
        .unwrap();
    store
        .write_recurring_rule(&rule(1, 1, 1), None, "operator", 13)
        .unwrap();
    store
        .write_recurring_rule(&second_rule(1, 1, 1), None, "operator", 14)
        .unwrap();
    assert_eq!(store.list_saved_searches(8).unwrap(), vec![search(1)]);
    assert_eq!(store.list_recurring_rules(CHANNEL, 8).unwrap().len(), 1);
    assert_eq!(
        store.list_recurring_rules(SECOND_CHANNEL, 8).unwrap().len(),
        1
    );

    let mut renamed = search(2);
    renamed.name = "Public meetings".into();
    store
        .write_saved_search(&renamed, Some(1), "operator", 20)
        .unwrap();
    assert_eq!(
        store.read_saved_search(SEARCH).unwrap(),
        Some(renamed.clone())
    );

    let mut rebound_owner = renamed.clone();
    rebound_owner.revision = 3;
    rebound_owner.owner_operator_id = SECOND_CHANNEL.into();
    assert_eq!(
        store.write_saved_search(&rebound_owner, Some(2), "operator", 21),
        Err(ScheduleAutomationWriteError::Rebound)
    );
    let mut rebound_created = renamed;
    rebound_created.revision = 3;
    rebound_created.created_at_utc_ms += 1;
    assert_eq!(
        store.write_saved_search(&rebound_created, Some(2), "operator", 22),
        Err(ScheduleAutomationWriteError::Rebound)
    );
}

#[test]
fn stale_cas_rebinding_stale_dependencies_and_unbounded_queries_fail_closed() {
    let store = StationStore::open(temporary_database("fail-closed")).unwrap();
    seed(&store);
    assert!(matches!(
        store.write_saved_search(&search(2), Some(0), "operator", 20),
        Err(ScheduleAutomationWriteError::Conflict { .. })
    ));
    let mut rebound = search(2);
    rebound.owner_operator_id = SECOND_CHANNEL.into();
    assert_eq!(
        store.write_saved_search(&rebound, Some(1), "operator", 20),
        Err(ScheduleAutomationWriteError::Rebound)
    );
    let stale_rule = rule(2, 2, 1);
    assert!(matches!(
        store.write_recurring_rule(&stale_rule, Some(1), "operator", 20),
        Err(ScheduleAutomationWriteError::DependencyConflict { .. })
    ));
    assert!(store.list_saved_searches(0).is_err());
    assert!(store.list_daypart_block_history(BLOCK, 129).is_err());
    assert!(matches!(
        store.write_daypart_block(&block(2), Some(1), " bad ", 20),
        Err(ScheduleAutomationWriteError::Invalid(_))
    ));
    let mut malformed_rule = rule(2, 1, 1);
    malformed_rule.saved_search_id = "not-a-uuid".into();
    assert!(matches!(
        store.write_recurring_rule(&malformed_rule, Some(1), "operator", 20),
        Err(ScheduleAutomationWriteError::Invalid(_))
    ));
    let mut zero_dependency_revision = rule(2, 0, 1);
    zero_dependency_revision.saved_search_revision = 0;
    assert!(matches!(
        store.write_recurring_rule(&zero_dependency_revision, Some(1), "operator", 20),
        Err(ScheduleAutomationWriteError::Invalid(_))
    ));
    let missing = "77777777-7777-4777-8777-777777777777";
    let mut absent_update = search(2);
    absent_update.search_id = missing.into();
    assert_eq!(
        store.write_saved_search(&absent_update, Some(1), "operator", 20),
        Err(ScheduleAutomationWriteError::NotFound {
            authority: "saved_search"
        })
    );
}

#[test]
fn duplicate_creation_and_capacity_are_typed() {
    let store = StationStore::open(temporary_database("capacity")).unwrap();
    store
        .write_saved_search(&search(1), None, "operator", 1)
        .unwrap();
    assert!(matches!(
        store.write_saved_search(&search(1), None, "operator", 2),
        Err(ScheduleAutomationWriteError::Conflict { .. })
    ));
    for index in 1..64u128 {
        let mut value = search(1);
        value.search_id = format!("{index:08x}-3333-4333-8333-333333333333");
        store
            .write_saved_search(&value, None, "operator", 3)
            .unwrap();
    }
    let mut overflow = search(1);
    overflow.search_id = "aaaaaaaa-3333-4333-8333-333333333333".into();
    assert_eq!(
        store.write_saved_search(&overflow, None, "operator", 4),
        Err(ScheduleAutomationWriteError::Capacity)
    );
}

#[test]
fn indexed_current_tampering_fails_closed_for_every_authority() {
    for (field, value, read_id) in [
        ("search_id", SECOND_CHANNEL, SECOND_CHANNEL),
        ("name", "tampered name", SEARCH),
        ("owner_operator_id", SECOND_CHANNEL, SEARCH),
        ("created_at_unix_ms", "1725000000001", SEARCH),
        ("revision", "2", SEARCH),
    ] {
        let database = temporary_database("saved-search-current-tamper");
        {
            let store = StationStore::open(&database).unwrap();
            seed(&store);
        }
        assert_eq!(
            raw_sql_status(
                &database,
                &format!("UPDATE schedule_automation_saved_searches SET {field}='{value}'"),
            ),
            SQLITE_OK
        );
        let store = StationStore::open(&database).unwrap();
        assert!(
            store
                .read_saved_search(read_id)
                .unwrap_err()
                .contains("disagree"),
            "tampering {field} must be detected"
        );
    }

    let database = temporary_database("other-current-tamper");
    {
        let store = StationStore::open(&database).unwrap();
        seed(&store);
    }
    assert_eq!(
        raw_sql_status(
            &database,
            &format!(
                "UPDATE schedule_automation_daypart_blocks SET channel_id='{SECOND_CHANNEL}';\
                 UPDATE schedule_automation_recurring_rules SET channel_id='{SECOND_CHANNEL}';"
            ),
        ),
        SQLITE_OK
    );
    let store = StationStore::open(&database).unwrap();
    assert!(
        store
            .read_daypart_block(BLOCK)
            .unwrap_err()
            .contains("disagree")
    );
    assert!(
        store
            .read_recurring_rule(RULE)
            .unwrap_err()
            .contains("disagree")
    );
}

#[test]
fn all_history_is_append_only_and_snapshot_tampering_fails_closed() {
    let database = temporary_database("history-tamper");
    {
        let store = StationStore::open(&database).unwrap();
        seed(&store);
    }
    for table in [
        "schedule_automation_saved_search_history",
        "schedule_automation_daypart_block_history",
        "schedule_automation_recurring_rule_history",
    ] {
        assert_ne!(
            raw_sql_status(
                &database,
                &format!("UPDATE {table} SET changed_by='tamper'")
            ),
            SQLITE_OK
        );
        assert_ne!(
            raw_sql_status(&database, &format!("DELETE FROM {table}")),
            SQLITE_OK
        );
    }

    let other_channel = SECOND_CHANNEL;
    assert_eq!(
        raw_sql_status(
            &database,
            &format!(
                "DROP TRIGGER schedule_automation_saved_search_history_no_update;\
                 DROP TRIGGER schedule_automation_daypart_block_history_no_update;\
                 DROP TRIGGER schedule_automation_recurring_rule_history_no_update;\
                 UPDATE schedule_automation_saved_search_history SET name='tampered name';\
                 UPDATE schedule_automation_daypart_block_history SET channel_id='{other_channel}';\
                 UPDATE schedule_automation_recurring_rule_history SET channel_id='{other_channel}';"
            ),
        ),
        SQLITE_OK
    );
    let store = StationStore::open(&database).unwrap();
    assert!(
        store
            .read_saved_search_revision(SEARCH, 1)
            .unwrap_err()
            .contains("disagree")
    );
    assert!(
        store
            .read_daypart_block_revision(BLOCK, 1)
            .unwrap_err()
            .contains("disagree")
    );
    assert!(
        store
            .read_recurring_rule_revision(RULE, 1)
            .unwrap_err()
            .contains("disagree")
    );
}

#[test]
fn saved_search_history_tampering_of_each_indexed_scalar_fails_closed() {
    for (field, value, read_id, read_revision) in [
        ("search_id", SECOND_CHANNEL, SECOND_CHANNEL, 1),
        ("name", "tampered name", SEARCH, 1),
        ("owner_operator_id", SECOND_CHANNEL, SEARCH, 1),
        ("created_at_unix_ms", "1725000000001", SEARCH, 1),
        ("revision", "2", SEARCH, 2),
    ] {
        let database = temporary_database("saved-search-history-tamper");
        {
            let store = StationStore::open(&database).unwrap();
            seed(&store);
        }
        assert_eq!(
            raw_sql_status(
                &database,
                &format!(
                    "DROP TRIGGER schedule_automation_saved_search_history_no_update;\
                     UPDATE schedule_automation_saved_search_history SET {field}='{value}'"
                ),
            ),
            SQLITE_OK
        );
        let store = StationStore::open(&database).unwrap();
        assert!(
            store
                .read_saved_search_revision(read_id, read_revision)
                .unwrap_err()
                .contains("disagree"),
            "tampering history {field} must be detected"
        );
    }
}

#[test]
fn current_authority_must_equal_its_latest_valid_history_snapshot() {
    let database = temporary_database("current-history-parity");
    {
        let store = StationStore::open(&database).unwrap();
        seed(&store);
    }
    let other_owner = "88888888-8888-4888-8888-888888888888";
    assert_eq!(
        raw_sql_status(
            &database,
            &format!(
                "DROP TRIGGER schedule_automation_saved_search_history_no_update;\
                 DROP TRIGGER schedule_automation_daypart_block_history_no_update;\
                 DROP TRIGGER schedule_automation_recurring_rule_history_no_update;\
                 UPDATE schedule_automation_saved_search_history \
                   SET owner_operator_id='{other_owner}', record_json=replace(record_json,'{OWNER}','{other_owner}');\
                 UPDATE schedule_automation_daypart_block_history \
                   SET record_json=replace(record_json,'Evening meetings','Morning meetings');\
                 UPDATE schedule_automation_recurring_rule_history \
                   SET record_json=replace(record_json,'\"enabled\":true','\"enabled\":false');"
            ),
        ),
        SQLITE_OK
    );
    let store = StationStore::open(&database).unwrap();
    assert!(
        store
            .read_saved_search(SEARCH)
            .unwrap_err()
            .contains("historical snapshot")
    );
    assert!(
        store
            .read_daypart_block(BLOCK)
            .unwrap_err()
            .contains("historical snapshot")
    );
    assert!(
        store
            .read_recurring_rule(RULE)
            .unwrap_err()
            .contains("historical snapshot")
    );
    assert!(store.list_saved_searches(8).is_err());
    assert!(store.list_daypart_blocks(CHANNEL, 8).is_err());
    assert!(store.list_recurring_rules(CHANNEL, 8).is_err());
}
