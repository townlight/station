use std::ffi::{CString, c_char, c_int, c_void};
use std::path::{Path, PathBuf};
use std::ptr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use station_domain::{ChannelRuntimeObservation, ChannelRuntimeState};
use station_schedule::{
    AssetReadiness, ChannelConfiguration, DispatchStatus, MediaAsset, OutputProfileId,
    OutputSinkConfiguration, ScheduleItem, ScheduleState,
};
use station_storage::{
    ActualAiringObservation, ActualAiringReadError, ActualAiringWriteError,
    ActualAiringWriteOutcome, MAX_ACTUAL_AIRING_QUERY_WINDOW_MS,
    MAX_ACTUAL_AIRING_RECEIVE_DELAY_MS, ScheduleItemWrite, ScheduledTakeAcknowledgement,
    ScheduledTakeAcknowledgementBinding, ScheduledTakeApplyError, ScheduledTakeApplyOutcome,
    StationStore,
};

const CHANNEL: &str = "11111111-1111-1111-1111-111111111111";
const OTHER_CHANNEL: &str = "22222222-2222-2222-2222-222222222222";
const BOOT: &str = "33333333-3333-3333-3333-333333333333";
const ASSET_A: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const ASSET_B: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const SCHEDULE_ITEM: &str = "44444444-4444-4444-4444-444444444444";
const SINK: &str = "55555555-5555-5555-5555-555555555555";
static SEQUENCE: AtomicU64 = AtomicU64::new(0);

const SQLITE_OK: c_int = 0;
const SQLITE_OPEN_READWRITE: c_int = 2;
const SQLITE_OPEN_CREATE: c_int = 4;

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
    // SAFETY: inputs are live C strings and the opened handle is closed below.
    let open = unsafe {
        sqlite3_open_v2(
            filename.as_ptr(),
            &mut database,
            SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE,
            ptr::null(),
        )
    };
    assert_eq!(open, SQLITE_OK);
    // SAFETY: the database handle is live and the SQL is a valid C string.
    let status = unsafe {
        sqlite3_exec(
            database,
            sql.as_ptr(),
            ptr::null_mut(),
            ptr::null_mut(),
            ptr::null_mut(),
        )
    };
    // SAFETY: this handle was opened above and has not yet been closed.
    assert_eq!(unsafe { sqlite3_close(database) }, SQLITE_OK);
    status
}

fn database(name: &str) -> PathBuf {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let sequence = SEQUENCE.fetch_add(1, Ordering::Relaxed);
    std::env::temp_dir().join(format!("townlight-airing-{name}-{now}-{sequence}.db"))
}

fn fact(
    airing_id: &str,
    channel_id: &str,
    asset_sha256: &str,
    aired_at_utc_ms: i64,
    worker_sequence: u64,
) -> ActualAiringObservation {
    ActualAiringObservation {
        airing_id: airing_id.into(),
        channel_id: channel_id.into(),
        asset_sha256: asset_sha256.into(),
        schedule_item_id: Some("44444444-4444-4444-4444-444444444444".into()),
        schedule_item_revision: Some(1),
        report_id: airing_id.into(),
        aired_at_utc_ms,
        media_offset_ms: 0,
        worker_boot_id: BOOT.into(),
        worker_sequence,
        worker_id: "worker-a".into(),
        worker_ack_id: airing_id.into(),
    }
}

fn now_utc_ms() -> i64 {
    i64::try_from(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis(),
    )
    .unwrap()
}

trait ScheduleStoreTestExt {
    fn put_schedule_item(&self, item: &ScheduleItem) -> Result<(), String>;
}

impl ScheduleStoreTestExt for StationStore {
    fn put_schedule_item(&self, item: &ScheduleItem) -> Result<(), String> {
        self.write_schedule_item(&ScheduleItemWrite {
            item: item.clone(),
            changed_by: "test-harness".into(),
            occurred_at_unix_ms: 1,
        })
    }
}

fn scheduled_take_fixture(
    label: &str,
) -> (PathBuf, PathBuf, StationStore, ScheduledTakeAcknowledgement) {
    scheduled_take_fixture_at(label, now_utc_ms() - 5_000)
}

fn scheduled_take_fixture_at(
    label: &str,
    starts_at_utc_ms: i64,
) -> (PathBuf, PathBuf, StationStore, ScheduledTakeAcknowledgement) {
    let path = database(label);
    let media_path = path.with_extension("ts");
    std::fs::write(&media_path, b"scheduled take media").unwrap();
    let store = StationStore::open(&path).unwrap();
    store
        .put_channel(&ChannelConfiguration {
            channel_id: CHANNEL.into(),
            display_name: "Scheduled take channel".into(),
            output_profile: OutputProfileId::Proof360p30,
            output_sinks: vec![OutputSinkConfiguration::required_udp(
                SINK,
                "Scheduled take output",
                "127.0.0.1:5500",
                -24.0,
            )],
            enabled: false,
        })
        .unwrap();
    store
        .put_media_asset(&MediaAsset {
            asset_id: ASSET_A.into(),
            media_path: media_path.to_string_lossy().into_owned(),
            duration_ms: 60_000,
            readiness: AssetReadiness::Ready,
        })
        .unwrap();
    store
        .put_schedule_item(&ScheduleItem {
            item_id: SCHEDULE_ITEM.into(),
            revision: 1,
            channel_id: CHANNEL.into(),
            asset_id: ASSET_A.into(),
            title: "Historical scheduled take".into(),
            starts_at_unix_ms: starts_at_utc_ms,
            duration_ms: 60_000,
            state: ScheduleState::Draft,
        })
        .unwrap();
    let report = store
        .commit_schedule(
            "report-1",
            "scheduled-take-plan",
            SCHEDULE_ITEM,
            "test-operator",
            starts_at_utc_ms - 1_000,
            "Atomic scheduled-take fixture.",
        )
        .unwrap();
    assert!(
        store
            .advance_dispatch_status(
                &report.report_id,
                DispatchStatus::Pending,
                DispatchStatus::Queued,
            )
            .unwrap()
    );
    let acknowledgement =
        ScheduledTakeAcknowledgement::from_binding(ScheduledTakeAcknowledgementBinding {
            report_id: report.report_id,
            channel_id: CHANNEL.into(),
            schedule_item_id: SCHEDULE_ITEM.into(),
            schedule_item_revision: report.schedule_item_revision,
            asset_sha256: ASSET_A.into(),
            aired_at_utc_ms: starts_at_utc_ms + 1_000,
            media_offset_ms: 1_000,
            worker_boot_id: BOOT.into(),
            worker_sequence: 1,
            worker_id: "worker-a".into(),
        })
        .unwrap();
    (path, media_path, store, acknowledgement)
}

fn scheduled_take_binding(
    acknowledgement: &ScheduledTakeAcknowledgement,
) -> ScheduledTakeAcknowledgementBinding {
    ScheduledTakeAcknowledgementBinding {
        report_id: acknowledgement.report_id.clone(),
        channel_id: acknowledgement.channel_id.clone(),
        schedule_item_id: acknowledgement.schedule_item_id.clone(),
        schedule_item_revision: acknowledgement.schedule_item_revision,
        asset_sha256: acknowledgement.asset_sha256.clone(),
        aired_at_utc_ms: acknowledgement.aired_at_utc_ms,
        media_offset_ms: acknowledgement.media_offset_ms,
        worker_boot_id: acknowledgement.worker_boot_id.clone(),
        worker_sequence: acknowledgement.worker_sequence,
        worker_id: acknowledgement.worker_id.clone(),
    }
}

#[test]
fn append_is_idempotent_and_identity_conflicts_are_rejected() {
    let path = database("idempotency");
    let store = StationStore::open(&path).unwrap();
    let now = now_utc_ms();
    let original = fact(
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        CHANNEL,
        ASSET_A,
        now - 100,
        1,
    );
    assert_eq!(
        store.append_actual_airing(&original),
        Ok(ActualAiringWriteOutcome::Inserted)
    );
    assert_eq!(
        store.append_actual_airing(&original),
        Ok(ActualAiringWriteOutcome::AlreadyRecorded)
    );
    let mut changed_offset = original.clone();
    changed_offset.media_offset_ms = 1;
    assert!(matches!(
        store.append_actual_airing(&changed_offset),
        Err(ActualAiringWriteError::Conflict(_))
    ));
    let mut changed = original.clone();
    changed.asset_sha256 = ASSET_B.into();
    assert!(matches!(
        store.append_actual_airing(&changed),
        Err(ActualAiringWriteError::Conflict(_))
    ));
    let mut sequence_collision = fact(
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        CHANNEL,
        ASSET_B,
        now - 99,
        1,
    );
    sequence_collision.report_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb".into();
    assert!(matches!(
        store.append_actual_airing(&sequence_collision),
        Err(ActualAiringWriteError::Conflict(_))
    ));
}

#[test]
fn window_is_half_open_bounded_and_canonical() {
    let path = database("window");
    let store = StationStore::open(&path).unwrap();
    let base = now_utc_ms() - 1_000;
    for (id, asset, time, sequence) in [
        (
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            ASSET_B,
            base + 20,
            2,
        ),
        (
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            ASSET_A,
            base + 20,
            3,
        ),
        (
            "cccccccc-cccc-cccc-cccc-cccccccccccc",
            ASSET_A,
            base + 10,
            4,
        ),
        (
            "dddddddd-dddd-dddd-dddd-dddddddddddd",
            ASSET_A,
            base + 30,
            5,
        ),
    ] {
        store
            .append_actual_airing(&fact(id, CHANNEL, asset, time, sequence))
            .unwrap();
    }
    store
        .append_actual_airing(&fact(
            "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
            OTHER_CHANNEL,
            ASSET_A,
            base + 20,
            6,
        ))
        .unwrap();
    let rows = store
        .list_actual_airings(CHANNEL, base + 10, base + 30, 3)
        .unwrap();
    assert_eq!(
        rows.iter()
            .map(|row| row.airing_id.as_str())
            .collect::<Vec<_>>(),
        vec![
            "cccccccc-cccc-cccc-cccc-cccccccccccc",
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        ]
    );
    assert!(matches!(
        store.list_actual_airings(CHANNEL, base + 30, base + 30, 1),
        Err(ActualAiringReadError::Invalid(_))
    ));
}

#[test]
fn append_only_and_indexed_json_parity_fail_closed() {
    let path = database("integrity");
    let store = StationStore::open(&path).unwrap();
    let now = now_utc_ms();
    let row = fact(
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        CHANNEL,
        ASSET_A,
        now - 100,
        1,
    );
    store.append_actual_airing(&row).unwrap();
    drop(store);
    assert_ne!(
        raw_sql_status(
            &path,
            "UPDATE actual_airings SET asset_sha256='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'"
        ),
        SQLITE_OK
    );
    assert_ne!(
        raw_sql_status(&path, "DELETE FROM actual_airings"),
        SQLITE_OK
    );
    assert_eq!(
        raw_sql_status(
            &path,
            "DROP TRIGGER actual_airings_no_update; UPDATE actual_airings SET record_json='{}';",
        ),
        SQLITE_OK
    );
    let reopened = StationStore::open(&path).unwrap();
    assert!(matches!(
        reopened.list_actual_airings(CHANNEL, now - 1_000, now + 1_000, 1),
        Err(ActualAiringReadError::Corrupt(_))
    ));
}

#[test]
fn transaction_seam_rejects_autocommit_callers() {
    let path = database("transaction-required");
    let store = StationStore::open(&path).unwrap();
    let now = now_utc_ms();
    let observation = fact(
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        CHANNEL,
        ASSET_A,
        now - 10,
        1,
    );
    assert_eq!(
        store.append_actual_airing_in_transaction(&observation, now),
        Err(ActualAiringWriteError::TransactionRequired)
    );
}

#[test]
fn complete_window_fails_instead_of_truncating_at_129_rows() {
    let path = database("window-overflow");
    let store = StationStore::open(&path).unwrap();
    let base = now_utc_ms() - 10_000;
    for index in 1_u64..=129 {
        let identity = format!("{index:08x}-0000-0000-0000-{index:012x}");
        store
            .append_actual_airing(&fact(
                &identity,
                CHANNEL,
                ASSET_A,
                base + i64::try_from(index).unwrap(),
                index,
            ))
            .unwrap();
    }
    assert_eq!(
        store.list_actual_airings(CHANNEL, base, base + 1_000, 128),
        Err(ActualAiringReadError::Capacity)
    );
}

#[test]
fn raw_airing_facts_remain_lossless_when_same_asset_is_confirmed_in_one_millisecond() {
    let path = database("s19-identity");
    let store = StationStore::open(&path).unwrap();
    let aired_at = now_utc_ms() - 100;
    let first = fact(
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        CHANNEL,
        ASSET_A,
        aired_at,
        1,
    );
    store.append_actual_airing(&first).unwrap();
    let duplicate = fact(
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        CHANNEL,
        ASSET_A,
        aired_at,
        2,
    );
    assert_eq!(
        store.append_actual_airing(&duplicate),
        Ok(ActualAiringWriteOutcome::Inserted)
    );
    assert_eq!(
        store
            .list_actual_airings(CHANNEL, aired_at - 1, aired_at + 1, 2)
            .unwrap()
            .len(),
        2
    );
}

#[test]
fn storage_derives_receive_time_and_rejects_future_or_stale_worker_times() {
    let path = database("receive-time");
    let store = StationStore::open(&path).unwrap();
    let now = now_utc_ms();
    let future = fact(
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        CHANNEL,
        ASSET_A,
        now + 60_000,
        1,
    );
    assert!(matches!(
        store.append_actual_airing(&future),
        Err(ActualAiringWriteError::Invalid(_))
    ));
    let stale = fact(
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        CHANNEL,
        ASSET_A,
        now - MAX_ACTUAL_AIRING_RECEIVE_DELAY_MS - 1_000,
        2,
    );
    assert!(matches!(
        store.append_actual_airing(&stale),
        Err(ActualAiringWriteError::Invalid(_))
    ));

    let valid = fact(
        "cccccccc-cccc-cccc-cccc-cccccccccccc",
        CHANNEL,
        ASSET_A,
        now - 100,
        3,
    );
    let before = now_utc_ms();
    store.append_actual_airing(&valid).unwrap();
    let after = now_utc_ms();
    let stored = store
        .list_actual_airings(CHANNEL, now - 1_000, now + 1_000, 1)
        .unwrap()
        .pop()
        .unwrap();
    assert!((before..=after).contains(&stored.received_at_utc_ms));
}

#[test]
fn prior_airing_query_window_is_bounded_to_s19_coverage() {
    let path = database("query-window");
    let store = StationStore::open(&path).unwrap();
    let end = now_utc_ms();
    assert!(matches!(
        store.list_actual_airings(CHANNEL, end - MAX_ACTUAL_AIRING_QUERY_WINDOW_MS - 1, end, 1,),
        Err(ActualAiringReadError::Invalid(_))
    ));
}

#[test]
fn legacy_uuid_report_schema_migrates_without_losing_receipts() {
    let path = database("report-id-migration");
    let now = now_utc_ms();
    let airing_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
    let record_json = serde_json::json!({
        "airing_id": airing_id,
        "channel_id": CHANNEL,
        "asset_sha256": ASSET_A,
        "schedule_item_id": SCHEDULE_ITEM,
        "schedule_item_revision": 1,
        "report_id": airing_id,
        "aired_at_utc_ms": now - 100,
        "received_at_utc_ms": now,
        "worker_boot_id": BOOT,
        "worker_sequence": 1,
        "worker_id": "worker-a",
        "worker_ack_id": airing_id,
    })
    .to_string();
    let legacy = format!(
        "CREATE TABLE actual_airings (\
           airing_id TEXT PRIMARY KEY CHECK (length(airing_id)=36),\
           channel_id TEXT NOT NULL CHECK (length(channel_id)=36),\
           asset_sha256 TEXT NOT NULL CHECK (length(asset_sha256)=64),\
           schedule_item_id TEXT NOT NULL DEFAULT '',\
           schedule_item_revision INTEGER NOT NULL DEFAULT -1 CHECK (schedule_item_revision >= -1),\
           report_id TEXT NOT NULL UNIQUE CHECK (length(report_id)=36),\
           aired_at_utc_ms INTEGER NOT NULL CHECK (aired_at_utc_ms >= 0),\
           received_at_utc_ms INTEGER NOT NULL CHECK (received_at_utc_ms >= 0),\
           worker_boot_id TEXT NOT NULL CHECK (length(worker_boot_id)=36),\
           worker_sequence INTEGER NOT NULL CHECK (worker_sequence >= 1),\
           worker_id TEXT NOT NULL,\
           worker_ack_id TEXT NOT NULL UNIQUE CHECK (length(worker_ack_id)=36),\
           record_json TEXT NOT NULL);\
         INSERT INTO actual_airings VALUES(\
           '{airing_id}','{CHANNEL}','{ASSET_A}','{SCHEDULE_ITEM}',1,'{airing_id}',\
           {},{},'{BOOT}',1,'worker-a','{airing_id}','{}');\
         CREATE INDEX actual_airings_channel_time_order ON actual_airings(channel_id,asset_sha256,aired_at_utc_ms,airing_id);\
         CREATE INDEX actual_airings_channel_time_window ON actual_airings(channel_id,aired_at_utc_ms,asset_sha256,airing_id);\
         CREATE UNIQUE INDEX actual_airings_worker_sequence ON actual_airings(channel_id,worker_boot_id,worker_sequence);\
         CREATE TRIGGER actual_airings_no_update BEFORE UPDATE ON actual_airings BEGIN SELECT RAISE(ABORT, 'legacy append only'); END;\
         CREATE TRIGGER actual_airings_no_delete BEFORE DELETE ON actual_airings BEGIN SELECT RAISE(ABORT, 'legacy append only'); END;",
        now - 100,
        now,
        record_json.replace('\'', "''"),
    );
    assert_eq!(raw_sql_status(&path, &legacy), SQLITE_OK);

    let store = StationStore::open(&path).unwrap();
    let migrated = store
        .list_actual_airings(CHANNEL, now - 1_000, now + 1_000, 1)
        .unwrap();
    assert_eq!(migrated.len(), 1);
    assert_eq!(migrated[0].airing_id, airing_id);
    assert_eq!(migrated[0].media_offset_ms, 0);
    drop(store);
    assert_ne!(
        raw_sql_status(
            &path,
            "UPDATE actual_airings SET record_json='{}' WHERE airing_id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'",
        ),
        SQLITE_OK
    );
    assert_ne!(
        raw_sql_status(&path, "DELETE FROM actual_airings"),
        SQLITE_OK
    );
    assert_ne!(
        raw_sql_status(
            &path,
            "INSERT INTO actual_airings(airing_id,channel_id,asset_sha256,schedule_item_id,schedule_item_revision,report_id,aired_at_utc_ms,media_offset_ms,received_at_utc_ms,worker_boot_id,worker_sequence,worker_id,worker_ack_id,record_json) SELECT 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',channel_id,asset_sha256,schedule_item_id,schedule_item_revision,'report-2',aired_at_utc_ms,media_offset_ms,received_at_utc_ms,worker_boot_id,worker_sequence,worker_id,'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',record_json FROM actual_airings WHERE airing_id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'",
        ),
        SQLITE_OK
    );
    let store = StationStore::open(&path).unwrap();
    let mut visible_report = fact(
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        CHANNEL,
        ASSET_B,
        now - 99,
        2,
    );
    visible_report.report_id = "report-1".into();
    store.append_actual_airing(&visible_report).unwrap();
    drop(store);
    let _ = std::fs::remove_file(path);
}

#[test]
fn scheduled_take_is_atomic_and_asserts_current_runtime_for_report_1() {
    let (path, media_path, store, acknowledgement) = scheduled_take_fixture("scheduled-success");
    assert_eq!(
        store.apply_scheduled_take_acknowledgement(&acknowledgement),
        Ok(ScheduledTakeApplyOutcome::Applied)
    );
    assert_eq!(
        store
            .read_commit_report(&acknowledgement.report_id)
            .unwrap()
            .unwrap()
            .dispatch_status,
        DispatchStatus::Acknowledged
    );
    let fact = store
        .list_actual_airings(
            CHANNEL,
            acknowledgement.aired_at_utc_ms - 1,
            acknowledgement.aired_at_utc_ms + 1,
            1,
        )
        .unwrap()
        .pop()
        .unwrap();
    assert_eq!(fact.report_id, "report-1");
    assert_eq!(
        fact.schedule_item_revision,
        Some(acknowledgement.schedule_item_revision)
    );
    let runtime = store.read_channel_runtime(CHANNEL).unwrap().unwrap();
    assert_eq!(runtime.state, ChannelRuntimeState::OnAir);
    assert_eq!(runtime.on_air_asset_id.as_deref(), Some(ASSET_A));
    assert_eq!(
        runtime.state_started_at_unix_ms,
        acknowledgement.aired_at_utc_ms
    );
    assert_eq!(runtime.heartbeat_at_unix_ms, fact.received_at_utc_ms);
    drop(store);
    let _ = std::fs::remove_file(path);
    let _ = std::fs::remove_file(media_path);
}

#[test]
fn late_journal_delivery_persists_the_fact_without_overwriting_current_runtime() {
    let (path, media_path, store, acknowledgement) = scheduled_take_fixture_at(
        "scheduled-historical",
        now_utc_ms() - 2 * 24 * 60 * 60 * 1_000,
    );
    assert_eq!(
        store.apply_scheduled_take_acknowledgement(&acknowledgement),
        Ok(ScheduledTakeApplyOutcome::AppliedHistorical)
    );
    assert_eq!(
        store
            .read_commit_report(&acknowledgement.report_id)
            .unwrap()
            .unwrap()
            .dispatch_status,
        DispatchStatus::Acknowledged
    );
    assert_eq!(
        store
            .list_actual_airings(
                CHANNEL,
                acknowledgement.aired_at_utc_ms - 1,
                acknowledgement.aired_at_utc_ms + 1,
                1,
            )
            .unwrap()
            .len(),
        1
    );
    assert!(store.read_channel_runtime(CHANNEL).unwrap().is_none());
    drop(store);
    let _ = std::fs::remove_file(path);
    let _ = std::fs::remove_file(media_path);
}

#[test]
fn exact_scheduled_take_replay_is_read_only_and_returns_already_applied() {
    let (path, media_path, store, acknowledgement) = scheduled_take_fixture("scheduled-replay");
    assert_eq!(
        store.apply_scheduled_take_acknowledgement(&acknowledgement),
        Ok(ScheduledTakeApplyOutcome::Applied)
    );
    let first = store
        .list_actual_airings(
            CHANNEL,
            acknowledgement.aired_at_utc_ms - 1,
            acknowledgement.aired_at_utc_ms + 1,
            1,
        )
        .unwrap()
        .pop()
        .unwrap();
    let advanced_runtime = ChannelRuntimeObservation {
        channel_id: CHANNEL.into(),
        state: ChannelRuntimeState::OnAir,
        on_air_asset_id: Some(ASSET_A.into()),
        state_started_at_unix_ms: acknowledgement.aired_at_utc_ms,
        heartbeat_at_unix_ms: first.received_at_utc_ms + 1,
        detail: Some("later worker heartbeat".into()),
    };
    store.put_channel_runtime(&advanced_runtime).unwrap();

    assert_eq!(
        store.apply_scheduled_take_acknowledgement(&acknowledgement),
        Ok(ScheduledTakeApplyOutcome::AlreadyApplied)
    );
    assert_eq!(
        store
            .list_actual_airings(
                CHANNEL,
                acknowledgement.aired_at_utc_ms - 1,
                acknowledgement.aired_at_utc_ms + 1,
                1,
            )
            .unwrap(),
        vec![first]
    );
    assert_eq!(
        store.read_channel_runtime(CHANNEL).unwrap(),
        Some(advanced_runtime)
    );
    let changed_offset =
        ScheduledTakeAcknowledgement::from_binding(ScheduledTakeAcknowledgementBinding {
            media_offset_ms: acknowledgement.media_offset_ms + 1,
            ..scheduled_take_binding(&acknowledgement)
        })
        .unwrap();
    assert!(matches!(
        store.apply_scheduled_take_acknowledgement(&changed_offset),
        Err(ScheduledTakeApplyError::Conflict(_))
    ));
    drop(store);
    let _ = std::fs::remove_file(path);
    let _ = std::fs::remove_file(media_path);
}

#[test]
fn exact_scheduled_take_replay_remains_idempotent_after_acknowledged_report_errors() {
    let (path, media_path, store, acknowledgement) =
        scheduled_take_fixture("scheduled-error-replay");
    assert_eq!(
        store.apply_scheduled_take_acknowledgement(&acknowledgement),
        Ok(ScheduledTakeApplyOutcome::Applied)
    );
    assert!(
        store
            .advance_dispatch_status(
                &acknowledgement.report_id,
                DispatchStatus::Acknowledged,
                DispatchStatus::Error,
            )
            .unwrap()
    );
    let original_fact = store
        .list_actual_airings(
            CHANNEL,
            acknowledgement.aired_at_utc_ms - 1,
            acknowledgement.aired_at_utc_ms + 1,
            1,
        )
        .unwrap();

    assert_eq!(
        store.apply_scheduled_take_acknowledgement(&acknowledgement),
        Ok(ScheduledTakeApplyOutcome::AlreadyApplied)
    );
    assert_eq!(
        store
            .read_commit_report(&acknowledgement.report_id)
            .unwrap()
            .unwrap()
            .dispatch_status,
        DispatchStatus::Error
    );
    assert_eq!(
        store
            .list_actual_airings(
                CHANNEL,
                acknowledgement.aired_at_utc_ms - 1,
                acknowledgement.aired_at_utc_ms + 1,
                1,
            )
            .unwrap(),
        original_fact
    );

    let changed = ScheduledTakeAcknowledgement::from_binding(ScheduledTakeAcknowledgementBinding {
        media_offset_ms: acknowledgement.media_offset_ms + 1,
        ..scheduled_take_binding(&acknowledgement)
    })
    .unwrap();
    assert!(matches!(
        store.apply_scheduled_take_acknowledgement(&changed),
        Err(ScheduledTakeApplyError::Conflict(_))
    ));

    drop(store);
    let _ = std::fs::remove_file(path);
    let _ = std::fs::remove_file(media_path);
}

#[test]
fn scheduled_take_identities_are_domain_separated_and_bind_media_offset() {
    let binding = ScheduledTakeAcknowledgementBinding {
        report_id: "report-1".into(),
        channel_id: CHANNEL.into(),
        schedule_item_id: SCHEDULE_ITEM.into(),
        schedule_item_revision: 2,
        asset_sha256: ASSET_A.into(),
        aired_at_utc_ms: 10_000,
        media_offset_ms: 1_000,
        worker_boot_id: BOOT.into(),
        worker_sequence: 1,
        worker_id: "worker-a".into(),
    };
    let first = ScheduledTakeAcknowledgement::from_binding(binding.clone()).unwrap();
    let retry = ScheduledTakeAcknowledgement::from_binding(binding.clone()).unwrap();
    assert_eq!(first, retry);
    assert_ne!(first.airing_id, first.worker_ack_id);
    let changed = ScheduledTakeAcknowledgement::from_binding(ScheduledTakeAcknowledgementBinding {
        media_offset_ms: binding.media_offset_ms + 1,
        ..binding
    })
    .unwrap();
    assert_ne!(first.airing_id, changed.airing_id);
    assert_ne!(first.worker_ack_id, changed.worker_ack_id);
}

#[test]
fn tampered_scheduled_take_binding_writes_nothing() {
    let (path, media_path, store, acknowledgement) = scheduled_take_fixture("scheduled-tamper");
    let acknowledgement =
        ScheduledTakeAcknowledgement::from_binding(ScheduledTakeAcknowledgementBinding {
            asset_sha256: ASSET_B.into(),
            ..scheduled_take_binding(&acknowledgement)
        })
        .unwrap();
    assert!(matches!(
        store.apply_scheduled_take_acknowledgement(&acknowledgement),
        Err(ScheduledTakeApplyError::Conflict(_))
    ));
    assert_eq!(
        store
            .read_commit_report(&acknowledgement.report_id)
            .unwrap()
            .unwrap()
            .dispatch_status,
        DispatchStatus::Queued
    );
    assert!(
        store
            .list_actual_airings(
                CHANNEL,
                acknowledgement.aired_at_utc_ms - 1,
                acknowledgement.aired_at_utc_ms + 1,
                1,
            )
            .unwrap()
            .is_empty()
    );
    assert!(store.read_channel_runtime(CHANNEL).unwrap().is_none());
    drop(store);
    let _ = std::fs::remove_file(path);
    let _ = std::fs::remove_file(media_path);
}

#[test]
fn queued_acknowledgement_is_preserved_as_historical_after_its_item_is_recommitted() {
    let (path, media_path, store, acknowledgement) = scheduled_take_fixture("scheduled-stale");
    let mut current = store
        .read_schedule_item(SCHEDULE_ITEM)
        .unwrap()
        .expect("fixture writes current schedule item");
    current.revision += 1;
    current.title = "Recommitted scheduled take".into();
    current.state = ScheduleState::Draft;
    store.put_schedule_item(&current).unwrap();
    store
        .commit_schedule(
            "report-recommit",
            "scheduled-take-plan-recommit",
            SCHEDULE_ITEM,
            "test-operator",
            acknowledgement.aired_at_utc_ms - 500,
            "Recommit the item so the queued historical report becomes stale.",
        )
        .unwrap();

    assert_eq!(
        store.apply_scheduled_take_acknowledgement(&acknowledgement),
        Ok(ScheduledTakeApplyOutcome::AppliedHistorical)
    );
    assert_eq!(
        store
            .read_commit_report(&acknowledgement.report_id)
            .unwrap()
            .unwrap()
            .dispatch_status,
        DispatchStatus::Acknowledged
    );
    assert_eq!(
        store
            .list_actual_airings(
                CHANNEL,
                acknowledgement.aired_at_utc_ms - 1,
                acknowledgement.aired_at_utc_ms + 1,
                1,
            )
            .unwrap()
            .len(),
        1
    );
    assert!(store.read_channel_runtime(CHANNEL).unwrap().is_none());
    assert_eq!(
        store.apply_scheduled_take_acknowledgement(&acknowledgement),
        Ok(ScheduledTakeApplyOutcome::AlreadyApplied)
    );
    drop(store);
    let _ = std::fs::remove_file(path);
    let _ = std::fs::remove_file(media_path);
}

#[test]
fn scheduled_take_rejects_media_offset_at_committed_duration() {
    let (path, media_path, store, acknowledgement) = scheduled_take_fixture("scheduled-offset");
    let acknowledgement =
        ScheduledTakeAcknowledgement::from_binding(ScheduledTakeAcknowledgementBinding {
            media_offset_ms: 60_000,
            ..scheduled_take_binding(&acknowledgement)
        })
        .unwrap();
    assert!(matches!(
        store.apply_scheduled_take_acknowledgement(&acknowledgement),
        Err(ScheduledTakeApplyError::Conflict(_))
    ));
    assert_eq!(
        store
            .read_commit_report(&acknowledgement.report_id)
            .unwrap()
            .unwrap()
            .dispatch_status,
        DispatchStatus::Queued
    );
    assert!(store.read_channel_runtime(CHANNEL).unwrap().is_none());
    drop(store);
    let _ = std::fs::remove_file(path);
    let _ = std::fs::remove_file(media_path);
}

#[test]
fn newer_runtime_preserves_acknowledgement_as_historical_without_replacing_projection() {
    let (path, media_path, store, acknowledgement) =
        scheduled_take_fixture("scheduled-runtime-rollback");
    let existing_runtime = ChannelRuntimeObservation {
        channel_id: CHANNEL.into(),
        state: ChannelRuntimeState::HealthyFallback,
        on_air_asset_id: None,
        state_started_at_unix_ms: acknowledgement.aired_at_utc_ms + 1,
        heartbeat_at_unix_ms: now_utc_ms() + 60_000,
        detail: Some("newer runtime authority".into()),
    };
    store.put_channel_runtime(&existing_runtime).unwrap();
    assert_eq!(
        store.apply_scheduled_take_acknowledgement(&acknowledgement),
        Ok(ScheduledTakeApplyOutcome::AppliedHistorical)
    );
    assert_eq!(
        store
            .read_commit_report(&acknowledgement.report_id)
            .unwrap()
            .unwrap()
            .dispatch_status,
        DispatchStatus::Acknowledged
    );
    assert_eq!(
        store
            .list_actual_airings(
                CHANNEL,
                acknowledgement.aired_at_utc_ms - 1,
                acknowledgement.aired_at_utc_ms + 1,
                1,
            )
            .unwrap()
            .len(),
        1
    );
    assert_eq!(
        store.read_channel_runtime(CHANNEL).unwrap(),
        Some(existing_runtime)
    );
    assert_eq!(
        store.apply_scheduled_take_acknowledgement(&acknowledgement),
        Ok(ScheduledTakeApplyOutcome::AlreadyApplied)
    );
    drop(store);
    let _ = std::fs::remove_file(path);
    let _ = std::fs::remove_file(media_path);
}

#[test]
fn future_worker_actual_time_is_rejected_without_creating_runtime_state() {
    let starts_at_utc_ms = now_utc_ms() + 60_000;
    let (path, media_path, store, acknowledgement) =
        scheduled_take_fixture_at("scheduled-future-time", starts_at_utc_ms);
    assert!(matches!(
        store.apply_scheduled_take_acknowledgement(&acknowledgement),
        Err(ScheduledTakeApplyError::Invalid(_))
    ));
    assert_eq!(
        store
            .read_commit_report(&acknowledgement.report_id)
            .unwrap()
            .unwrap()
            .dispatch_status,
        DispatchStatus::Queued
    );
    assert!(store.read_channel_runtime(CHANNEL).unwrap().is_none());
    drop(store);
    let _ = std::fs::remove_file(path);
    let _ = std::fs::remove_file(media_path);
}
