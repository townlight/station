use std::ffi::{CString, c_char, c_int, c_void};
use std::path::Path;
use std::ptr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use station_media_journal::{JournalWriter, read_events};
use station_media_protocol::{PROTOCOL_VERSION, WorkerEvent, WorkerEventEnvelope};
use station_reporting::runtime::{
    ASRUN_CHECKPOINT_VERSION, AsRunState, JournalTake, ProjectionInput, ReportingRuntime,
};
use station_storage::{StationStore, project_durable_journal, station_local_day_window};

static SEQUENCE: AtomicU64 = AtomicU64::new(0);
const CHANNEL: &str = "00000000-0000-4000-8000-000000000001";
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

fn database() -> std::path::PathBuf {
    std::env::temp_dir().join(format!(
        "townlight-asrun-{}-{}.db",
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos(),
        SEQUENCE.fetch_add(1, Ordering::Relaxed)
    ))
}

fn raw_sql_status(database_path: &Path, sql: &str) -> c_int {
    let filename = CString::new(database_path.to_string_lossy().as_bytes()).unwrap();
    let sql = CString::new(sql).unwrap();
    let mut database = ptr::null_mut();
    // SAFETY: the C strings live until the SQLite handle is closed below.
    let open = unsafe {
        sqlite3_open_v2(
            filename.as_ptr(),
            &mut database,
            SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE,
            ptr::null(),
        )
    };
    assert_eq!(open, SQLITE_OK);
    // SAFETY: the open handle and SQL C string are valid for this call.
    let result = unsafe {
        sqlite3_exec(
            database,
            sql.as_ptr(),
            ptr::null_mut(),
            ptr::null_mut(),
            ptr::null_mut(),
        )
    };
    // SAFETY: the handle was opened above and is not used after this point.
    assert_eq!(unsafe { sqlite3_close(database) }, SQLITE_OK);
    result
}
fn projection() -> station_reporting::runtime::AsRunProjectionRuntime {
    let take = JournalTake {
        journal_id: "station-worker-00000000-0000-4000-8000-000000000001".into(),
        journal_sequence: 1,
        channel_id: CHANNEL.into(),
        started_at_unix_ms: 1_700_000_000_000,
        ended_at_unix_ms: 1_700_000_060_000,
        state: AsRunState::Program,
        content_id: Some("00000000-0000-4000-8000-000000000003".into()),
        franchise_id: Some("00000000-0000-4000-8000-000000000004".into()),
        category: Some("program".into()),
        title: Some("Council".into()),
        catalog_revision: Some(1),
        schedule_revision: Some(1),
        journal_hash: "a".repeat(64),
        unavailable_reason: None,
    };
    ReportingRuntime::new()
        .project(ProjectionInput::new(CHANNEL, "America/Denver", vec![take]).unwrap())
        .unwrap()
}
#[test]
fn durable_projection_replays_idempotently_and_survives_restart() {
    let path = database();
    let store = StationStore::open(&path).unwrap();
    let value = projection();
    store
        .persist_asrun_projection(&value, 1_700_000_060_000)
        .unwrap();
    store
        .persist_asrun_projection(&value, 1_700_000_060_000)
        .unwrap();
    assert_eq!(
        store
            .page_asrun_projection(CHANNEL, None, 1)
            .unwrap()
            .unwrap()
            .entries
            .len(),
        1
    );
    drop(store);
    let reopened = StationStore::open(&path).unwrap();
    let page = reopened
        .page_asrun_projection(CHANNEL, None, 1)
        .unwrap()
        .unwrap();
    assert_eq!(page.checkpoint_sequence, 1);
    assert_eq!(page.station_timezone, "America/Denver");
}

#[test]
fn legacy_evidence_schema_migrates_to_interval_unique_segments_without_losing_rows() {
    let path = database();
    let expected = projection().entries.remove(0);
    let record = serde_json::to_string(&expected)
        .unwrap()
        .replace('\'', "''");
    let legacy_schema = format!(
        "CREATE TABLE asrun_epg_projection_evidence(\
           event_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL,\
           journal_sequence INTEGER NOT NULL, record_json TEXT NOT NULL);\
         INSERT INTO asrun_epg_projection_evidence VALUES(\
           '{}','{}',{},'{}');",
        expected.event_id, CHANNEL, expected.journal_sequence, record
    );
    assert_eq!(raw_sql_status(&path, &legacy_schema), SQLITE_OK);

    let store = StationStore::open(&path).unwrap();
    assert_eq!(
        store.list_asrun_projection_evidence(CHANNEL).unwrap(),
        vec![expected]
    );
    drop(store);
    assert_ne!(
        raw_sql_status(
            &path,
            "UPDATE asrun_epg_projection_evidence SET record_json='{}'",
        ),
        SQLITE_OK,
        "the recreated evidence table retains its append-only trigger"
    );
}

#[test]
fn durable_journal_adapter_replays_and_keeps_missing_authority_explicit() {
    let path = database();
    let journal = path.with_extension("tlj");
    let mut writer =
        JournalWriter::open(&journal, &format!("station-worker-{CHANNEL}"), CHANNEL).unwrap();
    writer
        .append(&WorkerEventEnvelope {
            version: PROTOCOL_VERSION,
            worker_id: format!("station-worker-{CHANNEL}"),
            channel_id: CHANNEL.into(),
            sequence: 1,
            event: WorkerEvent::ScheduledAssetOnAir {
                report_id: "report".into(),
                schedule_item_id: "00000000-0000-4000-8000-000000000020".into(),
                schedule_item_revision: 1,
                asset_id: "a".repeat(64),
                media_offset_ms: 0,
                worker_boot_id: "00000000-0000-4000-8000-000000000030".into(),
                actual_on_air_at_unix_ms: 1_700_000_000_000,
            },
        })
        .unwrap();
    writer
        .append(&WorkerEventEnvelope {
            version: PROTOCOL_VERSION,
            worker_id: format!("station-worker-{CHANNEL}"),
            channel_id: CHANNEL.into(),
            sequence: 2,
            event: WorkerEvent::ScheduledAssetOnAir {
                report_id: "report-2".into(),
                schedule_item_id: "00000000-0000-4000-8000-000000000021".into(),
                schedule_item_revision: 1,
                asset_id: "b".repeat(64),
                media_offset_ms: 0,
                worker_boot_id: "00000000-0000-4000-8000-000000000030".into(),
                // This is a bounded lookahead exactly at the window boundary:
                // it closes the first interval but is never emitted as a row
                // outside the requested half-open window.
                actual_on_air_at_unix_ms: 1_700_000_060_000,
            },
        })
        .unwrap();
    drop(writer);
    let store = StationStore::open(&path).unwrap();
    let first = project_durable_journal(
        &store,
        &journal,
        &format!("station-worker-{CHANNEL}"),
        CHANNEL,
        "America/Denver",
        1_700_000_060_000,
        true,
    )
    .unwrap();
    assert!(
        first.entries[0]
            .unavailable_reason
            .as_deref()
            .unwrap()
            .contains("schedule")
    );
    assert_eq!(first.entries.len(), 1);
    assert_eq!(first.entries[0].ended_at_unix_ms, 1_700_000_060_000);
    let second = project_durable_journal(
        &store,
        &journal,
        &format!("station-worker-{CHANNEL}"),
        CHANNEL,
        "America/Denver",
        1_700_000_060_000,
        false,
    )
    .unwrap();
    assert_eq!(first.entries, second.entries);
    let page = store
        .page_asrun_projection(CHANNEL, None, 1)
        .unwrap()
        .unwrap();
    assert_eq!(page.source_journal_id, format!("station-worker-{CHANNEL}"));
    assert_eq!(
        page.projection_window
            .as_ref()
            .map(|value| value.local_date.as_str()),
        Some("2023-11-14")
    );
}

#[test]
fn station_local_day_window_preserves_denver_dst_calendar_days() {
    let spring = station_local_day_window("America/Denver", 1_710_097_200_000).unwrap();
    assert_eq!(spring.local_date, "2024-03-10");
    assert_eq!(spring.starts_at_utc_ms, 1_710_054_000_000);
    assert_eq!(spring.ends_at_utc_ms, 1_710_136_800_000);
    assert_eq!(
        spring.ends_at_utc_ms - spring.starts_at_utc_ms,
        23 * 60 * 60 * 1_000
    );

    let fall = station_local_day_window("America/Denver", 1_730_656_000_000).unwrap();
    assert_eq!(fall.local_date, "2024-11-03");
    assert_eq!(fall.starts_at_utc_ms, 1_730_613_600_000);
    assert_eq!(fall.ends_at_utc_ms, 1_730_703_600_000);
    assert_eq!(
        fall.ends_at_utc_ms - fall.starts_at_utc_ms,
        25 * 60 * 60 * 1_000
    );
}

#[test]
fn rollover_splits_one_source_transition_without_gap_or_overlap_on_denver_dst_days() {
    for (label, instant, expected_day_ms) in [
        ("spring", 1_710_097_200_000, 23 * 60 * 60 * 1_000),
        ("fall", 1_730_656_000_000, 25 * 60 * 60 * 1_000),
    ] {
        let path = database();
        let journal = path.with_extension(format!("{label}.tlj"));
        let window = station_local_day_window("America/Denver", instant).unwrap();
        assert_eq!(
            window.ends_at_utc_ms - window.starts_at_utc_ms,
            expected_day_ms
        );
        let mut writer =
            JournalWriter::open(&journal, &format!("station-worker-{CHANNEL}"), CHANNEL).unwrap();
        append_scheduled(&mut writer, 1, window.starts_at_utc_ms, "0");
        drop(writer);

        let store = StationStore::open(&path).unwrap();
        project_durable_journal(
            &store,
            &journal,
            &format!("station-worker-{CHANNEL}"),
            CHANNEL,
            "America/Denver",
            window.starts_at_utc_ms + 1,
            true,
        )
        .unwrap();

        // First reporting pass in the next local day closes the prior
        // segment even though the successor source transition has not yet
        // arrived. The current-day state remains open for that successor.
        project_durable_journal(
            &store,
            &journal,
            &format!("station-worker-{CHANNEL}"),
            CHANNEL,
            "America/Denver",
            window.ends_at_utc_ms + 1,
            true,
        )
        .unwrap();
        let next_start = window.ends_at_utc_ms;
        let successor_at = next_start + 60_000;
        let mut writer =
            JournalWriter::open(&journal, &format!("station-worker-{CHANNEL}"), CHANNEL).unwrap();
        append_scheduled(&mut writer, 2, successor_at, "1");
        drop(writer);
        project_durable_journal(
            &store,
            &journal,
            &format!("station-worker-{CHANNEL}"),
            CHANNEL,
            "America/Denver",
            successor_at,
            true,
        )
        .unwrap();

        let evidence = store.list_asrun_projection_evidence(CHANNEL).unwrap();
        assert_eq!(evidence.len(), 2, "{label} rollover writes two segments");
        assert_eq!(evidence[0].journal_sequence, 1);
        assert_eq!(evidence[1].journal_sequence, 1);
        assert_ne!(evidence[0].event_id, evidence[1].event_id);
        assert_eq!(evidence[0].started_at_unix_ms, window.starts_at_utc_ms);
        assert_eq!(evidence[0].ended_at_unix_ms, next_start);
        assert_eq!(evidence[1].started_at_unix_ms, next_start);
        assert_eq!(evidence[1].ended_at_unix_ms, successor_at);
        assert_eq!(
            evidence[0].ended_at_unix_ms, evidence[1].started_at_unix_ms,
            "{label} rollover has neither overlap nor gap"
        );
        assert_eq!(
            evidence[1].ended_at_unix_ms - evidence[0].started_at_unix_ms,
            successor_at - window.starts_at_utc_ms
        );
    }
}

#[test]
fn legacy_checkpoint_rederives_an_authenticated_anchor_before_v2_persistence() {
    let path = database();
    let journal = path.with_extension("tlj");
    let start = 1_700_000_000_000;
    let mut writer =
        JournalWriter::open(&journal, &format!("station-worker-{CHANNEL}"), CHANNEL).unwrap();
    append_scheduled(&mut writer, 1, start, "0");
    append_scheduled(&mut writer, 2, start + 60_000, "1");
    append_scheduled(&mut writer, 3, start + 120_000, "2");
    drop(writer);
    let store = StationStore::open(&path).unwrap();
    let original = project_durable_journal(
        &store,
        &journal,
        &format!("station-worker-{CHANNEL}"),
        CHANNEL,
        "America/Denver",
        start + 120_000,
        true,
    )
    .unwrap();
    let stale_cursor = store
        .page_asrun_projection(CHANNEL, None, 1)
        .unwrap()
        .unwrap()
        .next_cursor
        .expect("two materialized rows require a continuation");
    let mut legacy = original.clone();
    legacy.checkpoint.checkpoint_version = 0;
    // These old byte-position fields deliberately cannot be used for resume.
    legacy.checkpoint.reader_byte_offset = 1;
    legacy.checkpoint.reader_raw_sequence = 999;
    legacy.checkpoint.reader_prefix_hash = "f".repeat(64);
    legacy.checkpoint.reader_file_identity = "forged-legacy-byte-cursor".into();
    legacy.checkpoint.reader_worker_id.clear();
    legacy.checkpoint.reader_channel_id.clear();
    store
        .persist_asrun_projection(&legacy, start + 120_000)
        .unwrap();

    let migrated = project_durable_journal(
        &store,
        &journal,
        &format!("station-worker-{CHANNEL}"),
        CHANNEL,
        "America/Denver",
        start + 120_000,
        true,
    )
    .unwrap();
    assert_eq!(
        migrated.checkpoint.checkpoint_version,
        ASRUN_CHECKPOINT_VERSION
    );
    assert_eq!(migrated.checkpoint.reader_raw_sequence, 3);
    assert_ne!(
        migrated.checkpoint.reader_file_identity,
        "forged-legacy-byte-cursor"
    );
    assert_eq!(
        migrated.checkpoint.source_anchor_frame_hash,
        migrated
            .checkpoint
            .pending_start
            .as_ref()
            .expect("re-derived pending transition")
            .journal_hash
    );
    assert!(
        store
            .page_asrun_projection(CHANNEL, Some(&stale_cursor), 1)
            .is_err(),
        "a cursor issued before checkpoint migration cannot skip or duplicate rows"
    );
    drop(store);
    let reopened = StationStore::open(&path).unwrap();
    let replay = project_durable_journal(
        &reopened,
        &journal,
        &format!("station-worker-{CHANNEL}"),
        CHANNEL,
        "America/Denver",
        start + 120_000,
        true,
    )
    .unwrap();
    assert_eq!(replay, migrated);
}

#[test]
fn legacy_checkpoint_without_a_verified_anchor_leaves_durable_state_unchanged() {
    let path = database();
    let journal = path.with_extension("tlj");
    let start = 1_700_000_000_000;
    let mut writer =
        JournalWriter::open(&journal, &format!("station-worker-{CHANNEL}"), CHANNEL).unwrap();
    append_scheduled(&mut writer, 1, start, "0");
    drop(writer);
    let store = StationStore::open(&path).unwrap();
    let initial = project_durable_journal(
        &store,
        &journal,
        &format!("station-worker-{CHANNEL}"),
        CHANNEL,
        "America/Denver",
        start,
        true,
    )
    .unwrap();
    let mut legacy = initial.clone();
    legacy.entries.clear();
    legacy.checkpoint.checkpoint_version = 0;
    legacy.checkpoint.pending_start = None;
    legacy.checkpoint.pending_is_uncovered = false;
    legacy.checkpoint.materialized_sequence = 0;
    legacy.checkpoint.materialized_hash.clear();
    legacy.checkpoint.source_anchor_sequence = 0;
    legacy.checkpoint.source_anchor_frame_hash.clear();
    legacy.checkpoint.reader_byte_offset = 1;
    legacy.checkpoint.reader_raw_sequence = 1;
    legacy.checkpoint.reader_prefix_hash = "a".repeat(64);
    legacy.checkpoint.reader_file_identity = "untrusted-legacy-offset".into();
    store.persist_asrun_projection(&legacy, start).unwrap();

    assert!(
        project_durable_journal(
            &store,
            &journal,
            &format!("station-worker-{CHANNEL}"),
            CHANNEL,
            "America/Denver",
            start,
            true,
        )
        .is_err()
    );
    assert_eq!(store.read_asrun_projection(CHANNEL).unwrap(), Some(legacy));
}

#[test]
fn legacy_pending_checkpoint_migrates_safely_while_closing_an_elapsed_local_day() {
    let path = database();
    let journal = path.with_extension("tlj");
    let start = 1_700_000_000_000;
    let first_window = station_local_day_window("America/Denver", start).unwrap();
    let mut writer =
        JournalWriter::open(&journal, &format!("station-worker-{CHANNEL}"), CHANNEL).unwrap();
    append_scheduled(&mut writer, 1, start, "0");
    drop(writer);
    let store = StationStore::open(&path).unwrap();
    let initial = project_durable_journal(
        &store,
        &journal,
        &format!("station-worker-{CHANNEL}"),
        CHANNEL,
        "America/Denver",
        start,
        true,
    )
    .unwrap();
    let mut legacy = initial;
    legacy.checkpoint.checkpoint_version = 0;
    legacy.checkpoint.reader_byte_offset = 1;
    legacy.checkpoint.reader_raw_sequence = 99;
    legacy.checkpoint.reader_prefix_hash = "f".repeat(64);
    legacy.checkpoint.reader_file_identity = "untrusted-pre-v2-rollover".into();
    store.persist_asrun_projection(&legacy, start).unwrap();

    let migrated = project_durable_journal(
        &store,
        &journal,
        &format!("station-worker-{CHANNEL}"),
        CHANNEL,
        "America/Denver",
        first_window.ends_at_utc_ms + 1,
        true,
    )
    .unwrap();
    assert_eq!(
        migrated.checkpoint.checkpoint_version,
        ASRUN_CHECKPOINT_VERSION
    );
    assert_eq!(migrated.checkpoint.reader_raw_sequence, 1);
    let evidence = store.list_asrun_projection_evidence(CHANNEL).unwrap();
    assert_eq!(evidence.len(), 1);
    assert_eq!(evidence[0].started_at_unix_ms, start);
    assert_eq!(evidence[0].ended_at_unix_ms, first_window.ends_at_utc_ms);
    drop(store);
    let reopened = StationStore::open(&path).unwrap();
    assert_eq!(
        reopened
            .read_asrun_projection(CHANNEL)
            .unwrap()
            .unwrap()
            .checkpoint
            .checkpoint_version,
        ASRUN_CHECKPOINT_VERSION
    );
}

#[test]
fn incremental_reader_survives_more_than_ten_thousand_lifetime_events_and_restart() {
    let path = database();
    let journal = path.with_extension("tlj");
    let mut writer =
        JournalWriter::open(&journal, &format!("station-worker-{CHANNEL}"), CHANNEL).unwrap();
    let start = 1_700_000_000_000;
    append_scheduled(&mut writer, 1, start, "0");
    for sequence in 2..=10_002 {
        writer
            .append(&WorkerEventEnvelope {
                version: PROTOCOL_VERSION,
                worker_id: format!("station-worker-{CHANNEL}"),
                channel_id: CHANNEL.into(),
                sequence,
                event: WorkerEvent::Heartbeat {
                    monotonic_milliseconds: sequence * 5_000,
                    outputs: Vec::new(),
                },
            })
            .unwrap();
    }
    append_scheduled(&mut writer, 10_003, start + 60_000, "1");
    drop(writer);

    let store = StationStore::open(&path).unwrap();
    let first = project_durable_journal(
        &store,
        &journal,
        &format!("station-worker-{CHANNEL}"),
        CHANNEL,
        "America/Denver",
        start + 60_000,
        true,
    )
    .unwrap();
    assert_eq!(first.entries.len(), 1);
    assert!(first.checkpoint.reader_raw_sequence > 10_000);
    drop(store);

    let reopened = StationStore::open(&path).unwrap();
    let replay = project_durable_journal(
        &reopened,
        &journal,
        &format!("station-worker-{CHANNEL}"),
        CHANNEL,
        "America/Denver",
        start + 60_000,
        true,
    )
    .unwrap();
    assert_eq!(replay.entries.len(), 1);
    assert_eq!(replay.checkpoint.reader_raw_sequence, 10_003);
}

#[test]
fn restart_prefix_verification_fails_closed_on_pre_checkpoint_corruption() {
    let path = database();
    let journal = path.with_extension("tlj");
    let mut writer =
        JournalWriter::open(&journal, &format!("station-worker-{CHANNEL}"), CHANNEL).unwrap();
    let start = 1_700_000_000_000;
    append_scheduled(&mut writer, 1, start, "0");
    append_scheduled(&mut writer, 2, start + 60_000, "1");
    drop(writer);
    let store = StationStore::open(&path).unwrap();
    project_durable_journal(
        &store,
        &journal,
        &format!("station-worker-{CHANNEL}"),
        CHANNEL,
        "America/Denver",
        start + 60_000,
        true,
    )
    .unwrap();
    drop(store);

    let mut bytes = std::fs::read(&journal).unwrap();
    bytes[0] ^= 0x01;
    std::fs::write(&journal, bytes).unwrap();
    let reopened = StationStore::open(&path).unwrap();
    assert!(
        project_durable_journal(
            &reopened,
            &journal,
            &format!("station-worker-{CHANNEL}"),
            CHANNEL,
            "America/Denver",
            start + 60_000,
            true,
        )
        .is_err()
    );
}

#[test]
fn checkpoint_recovers_across_heartbeat_compaction_from_retained_transition_anchor() {
    let path = database();
    let journal = path.with_extension("tlj");
    let start = 1_700_000_000_000;
    let mut writer =
        JournalWriter::open(&journal, &format!("station-worker-{CHANNEL}"), CHANNEL).unwrap();
    append_scheduled(&mut writer, 1, start, "0");
    append_scheduled(&mut writer, 2, start + 60_000, "1");
    drop(writer);
    let store = StationStore::open(&path).unwrap();
    project_durable_journal(
        &store,
        &journal,
        &format!("station-worker-{CHANNEL}"),
        CHANNEL,
        "America/Denver",
        start + 60_000,
        true,
    )
    .unwrap();

    let mut writer =
        JournalWriter::open(&journal, &format!("station-worker-{CHANNEL}"), CHANNEL).unwrap();
    for sequence in 3..=515 {
        writer
            .append(&WorkerEventEnvelope {
                version: PROTOCOL_VERSION,
                worker_id: format!("station-worker-{CHANNEL}"),
                channel_id: CHANNEL.into(),
                sequence,
                event: WorkerEvent::Heartbeat {
                    monotonic_milliseconds: sequence * 5_000,
                    outputs: Vec::new(),
                },
            })
            .unwrap();
    }
    append_scheduled(&mut writer, 516, start + 120_000, "2");
    drop(writer);

    let recovered = project_durable_journal(
        &store,
        &journal,
        &format!("station-worker-{CHANNEL}"),
        CHANNEL,
        "America/Denver",
        start + 120_000,
        true,
    )
    .unwrap();
    assert_eq!(recovered.entries.len(), 2);
    assert_eq!(recovered.entries[1].journal_sequence, 2);
}

#[test]
fn pending_anchor_survives_513_heartbeat_compaction_and_local_day_rollover() {
    let path = database();
    let journal = path.with_extension("tlj");
    let start = 1_700_000_000_000;
    let mut writer =
        JournalWriter::open(&journal, &format!("station-worker-{CHANNEL}"), CHANNEL).unwrap();
    append_scheduled(&mut writer, 1, start, "0");
    drop(writer);

    let store = StationStore::open(&path).unwrap();
    let initial = project_durable_journal(
        &store,
        &journal,
        &format!("station-worker-{CHANNEL}"),
        CHANNEL,
        "America/Denver",
        start,
        true,
    )
    .unwrap();
    assert!(initial.entries.is_empty());
    assert_eq!(initial.checkpoint.source_anchor_sequence, 1);
    assert_eq!(
        initial.checkpoint.source_anchor_frame_hash,
        initial
            .checkpoint
            .pending_start
            .as_ref()
            .unwrap()
            .journal_hash
    );
    assert_ne!(
        initial.checkpoint.source_anchor_frame_hash, initial.checkpoint.journal_hash,
        "the event-frame anchor must remain independent of the reader prefix hash"
    );

    let mut writer =
        JournalWriter::open(&journal, &format!("station-worker-{CHANNEL}"), CHANNEL).unwrap();
    for sequence in 2..=514 {
        writer
            .append(&WorkerEventEnvelope {
                version: PROTOCOL_VERSION,
                worker_id: format!("station-worker-{CHANNEL}"),
                channel_id: CHANNEL.into(),
                sequence,
                event: WorkerEvent::Heartbeat {
                    monotonic_milliseconds: sequence * 5_000,
                    outputs: Vec::new(),
                },
            })
            .unwrap();
    }
    drop(writer);
    assert!(
        read_events(&journal)
            .unwrap()
            .iter()
            .any(|event| event.sequence == 1),
        "the pending scheduled transition remains physically retained after compaction"
    );
    // Simulate a reporting-process crash after the journal's atomic
    // compaction replacement but before the next local-day pass.
    drop(store);
    let store = StationStore::open(&path).unwrap();

    let next_day = start + 24 * 60 * 60 * 1_000;
    let rolled = project_durable_journal(
        &store,
        &journal,
        &format!("station-worker-{CHANNEL}"),
        CHANNEL,
        "America/Denver",
        next_day,
        true,
    )
    .unwrap();
    assert!(rolled.entries.is_empty());
    assert_ne!(
        rolled.checkpoint.reader_file_identity, initial.checkpoint.reader_file_identity,
        "heartbeat compaction atomically replaced the journal before recovery"
    );
    assert_eq!(rolled.checkpoint.source_anchor_sequence, 1);
    assert_eq!(
        rolled
            .checkpoint
            .pending_start
            .as_ref()
            .unwrap()
            .journal_sequence,
        1
    );

    let mut writer =
        JournalWriter::open(&journal, &format!("station-worker-{CHANNEL}"), CHANNEL).unwrap();
    append_scheduled(&mut writer, 515, next_day, "1");
    drop(writer);
    let closed = project_durable_journal(
        &store,
        &journal,
        &format!("station-worker-{CHANNEL}"),
        CHANNEL,
        "America/Denver",
        next_day,
        true,
    )
    .unwrap();
    assert_eq!(closed.entries.len(), 1);
    assert_eq!(closed.entries[0].state, AsRunState::Program);
    assert_eq!(
        closed.entries[0].started_at_unix_ms,
        station_local_day_window("America/Denver", next_day)
            .unwrap()
            .starts_at_utc_ms
    );
    let evidence = store.list_asrun_projection_evidence(CHANNEL).unwrap();
    assert_eq!(evidence.len(), 2);
    assert_eq!(evidence[0].journal_sequence, 1);
    assert_eq!(evidence[1].journal_sequence, 1);
    assert_eq!(evidence[0].ended_at_unix_ms, evidence[1].started_at_unix_ms);
}

#[test]
fn hostile_late_compaction_seal_cannot_advance_projection_authority() {
    let path = database();
    let journal = path.with_extension("tlj");
    let start = 1_700_000_000_000;
    let mut writer =
        JournalWriter::open(&journal, &format!("station-worker-{CHANNEL}"), CHANNEL).unwrap();
    append_scheduled(&mut writer, 1, start, "0");
    for sequence in 2..=514 {
        writer
            .append(&WorkerEventEnvelope {
                version: PROTOCOL_VERSION,
                worker_id: format!("station-worker-{CHANNEL}"),
                channel_id: CHANNEL.into(),
                sequence,
                event: WorkerEvent::Heartbeat {
                    monotonic_milliseconds: sequence * 5_000,
                    outputs: Vec::new(),
                },
            })
            .unwrap();
    }
    drop(writer);

    // Reorder a valid compacted image into Summary -> retained Events ->
    // Commit. Every frame and CRC remains valid, but the seal is deliberately
    // after a consumer's bounded first retained batch.
    let bytes = std::fs::read(&journal).unwrap();
    let mut offset = 0_usize;
    let mut summaries = Vec::new();
    let mut events = Vec::new();
    let mut commits = Vec::new();
    while offset < bytes.len() {
        let payload = u32::from_le_bytes(bytes[offset + 8..offset + 12].try_into().unwrap());
        let end = offset + 16 + payload as usize;
        match u16::from_le_bytes(bytes[offset + 6..offset + 8].try_into().unwrap()) {
            0 => events.extend_from_slice(&bytes[offset..end]),
            1 => summaries.extend_from_slice(&bytes[offset..end]),
            2 => commits.extend_from_slice(&bytes[offset..end]),
            kind => panic!("unexpected journal record kind {kind}"),
        }
        offset = end;
    }
    summaries.extend_from_slice(&events);
    summaries.extend_from_slice(&commits);
    std::fs::write(&journal, summaries).unwrap();

    let store = StationStore::open(&path).unwrap();
    assert!(
        project_durable_journal(
            &store,
            &journal,
            &format!("station-worker-{CHANNEL}"),
            CHANNEL,
            "America/Denver",
            start,
            true,
        )
        .is_err()
    );
    assert!(
        store.read_asrun_projection(CHANNEL).unwrap().is_none(),
        "a rejected unsealed batch must not transactionally advance authority"
    );
}

#[test]
fn untimed_transition_becomes_explicit_uncovered_gap_not_program_airtime() {
    let path = database();
    let journal = path.with_extension("tlj");
    let start = 1_700_000_000_000;
    let mut writer =
        JournalWriter::open(&journal, &format!("station-worker-{CHANNEL}"), CHANNEL).unwrap();
    append_scheduled(&mut writer, 1, start, "0");
    writer
        .append(&WorkerEventEnvelope {
            version: PROTOCOL_VERSION,
            worker_id: format!("station-worker-{CHANNEL}"),
            channel_id: CHANNEL.into(),
            sequence: 2,
            event: WorkerEvent::LiveOnAir {
                session_id: "00000000-0000-4000-8000-000000000040".into(),
                source_id: "00000000-0000-4000-8000-000000000041".into(),
                source_revision: 1,
            },
        })
        .unwrap();
    append_scheduled(&mut writer, 3, start + 60_000, "1");
    drop(writer);
    let store = StationStore::open(&path).unwrap();
    let projection = project_durable_journal(
        &store,
        &journal,
        &format!("station-worker-{CHANNEL}"),
        CHANNEL,
        "America/Denver",
        start + 60_000,
        true,
    )
    .unwrap();
    assert_eq!(projection.entries[0].state, AsRunState::Gap);
    assert!(
        projection.entries[0]
            .unavailable_reason
            .as_deref()
            .unwrap()
            .contains("uncovered")
    );
}

#[test]
fn cg_and_caption_overlays_do_not_make_scheduled_airtime_uncovered() {
    let path = database();
    let journal = path.with_extension("tlj");
    let start = 1_700_000_000_000;
    let mut writer =
        JournalWriter::open(&journal, &format!("station-worker-{CHANNEL}"), CHANNEL).unwrap();
    append_scheduled(&mut writer, 1, start, "0");
    for (sequence, event) in [
        (
            2,
            WorkerEvent::CgApplied {
                board_id: "board-a".into(),
                board_revision: 1,
            },
        ),
        (3, WorkerEvent::CgCleared),
        (
            4,
            WorkerEvent::CaptionCueAccepted {
                cue_id: "00000000-0000-4000-8000-000000000050".into(),
            },
        ),
        (5, WorkerEvent::CaptionCuesCleared),
    ] {
        writer
            .append(&WorkerEventEnvelope {
                version: PROTOCOL_VERSION,
                worker_id: format!("station-worker-{CHANNEL}"),
                channel_id: CHANNEL.into(),
                sequence,
                event,
            })
            .unwrap();
    }
    append_scheduled(&mut writer, 6, start + 60_000, "1");
    drop(writer);

    let store = StationStore::open(&path).unwrap();
    let projection = project_durable_journal(
        &store,
        &journal,
        &format!("station-worker-{CHANNEL}"),
        CHANNEL,
        "America/Denver",
        start + 60_000,
        true,
    )
    .unwrap();
    assert_eq!(projection.entries[0].state, AsRunState::Program);
}

#[test]
fn opaque_page_cursor_prevents_skip_or_duplication_after_projection_change() {
    let path = database();
    let store = StationStore::open(&path).unwrap();
    let mut takes = Vec::new();
    for (sequence, hash) in [(1, "a"), (2, "b"), (3, "c")] {
        takes.push(JournalTake {
            journal_id: format!("station-worker-{CHANNEL}"),
            journal_sequence: sequence,
            channel_id: CHANNEL.into(),
            started_at_unix_ms: 1_700_000_000_000 + (sequence as i64 - 1) * 60_000,
            ended_at_unix_ms: 1_700_000_000_000 + sequence as i64 * 60_000,
            state: AsRunState::Program,
            content_id: Some("00000000-0000-4000-8000-000000000003".into()),
            franchise_id: Some("00000000-0000-4000-8000-000000000004".into()),
            category: Some("program".into()),
            title: Some("Council".into()),
            catalog_revision: Some(1),
            schedule_revision: Some(1),
            journal_hash: hash.repeat(64),
            unavailable_reason: None,
        });
    }
    let projection = ReportingRuntime::new()
        .project(ProjectionInput::new(CHANNEL, "America/Denver", takes).unwrap())
        .unwrap();
    store
        .persist_asrun_projection(&projection, 1_700_000_180_000)
        .unwrap();
    let first = store
        .page_asrun_projection(CHANNEL, None, 1)
        .unwrap()
        .unwrap();
    let cursor = first.next_cursor.clone().expect("first page continues");
    let second = store
        .page_asrun_projection(CHANNEL, Some(&cursor), 1)
        .unwrap()
        .unwrap();
    assert_eq!(first.entries[0].journal_sequence, 1);
    assert_eq!(second.entries[0].journal_sequence, 2);

    let mut window_changed = projection.clone();
    window_changed.window =
        Some(station_local_day_window("America/Denver", 1_700_000_000_000).unwrap());
    store
        .persist_asrun_projection(&window_changed, 1_700_000_180_000)
        .unwrap();
    assert!(
        store
            .page_asrun_projection(CHANNEL, Some(&cursor), 1)
            .is_err()
    );

    let window_cursor = store
        .page_asrun_projection(CHANNEL, None, 1)
        .unwrap()
        .unwrap()
        .next_cursor
        .expect("windowed page continues");
    let mut changed = window_changed.clone();
    changed.checkpoint.journal_sequence = 4;
    changed.checkpoint.journal_hash = "e".repeat(64);
    changed.checkpoint.reader_byte_offset = 1;
    changed.checkpoint.reader_raw_sequence = 4;
    changed.checkpoint.reader_prefix_hash = "d".repeat(64);
    changed.checkpoint.reader_file_identity = "page-source-revision-2".into();
    changed.checkpoint.reader_worker_id = format!("station-worker-{CHANNEL}");
    changed.checkpoint.reader_channel_id = CHANNEL.into();
    store
        .persist_asrun_projection(&changed, 1_700_000_180_000)
        .unwrap();
    assert!(
        store
            .page_asrun_projection(CHANNEL, Some(&window_cursor), 1)
            .is_err()
    );

    let other_channel = "00000000-0000-4000-8000-000000000002";
    let other = ReportingRuntime::new()
        .project(
            ProjectionInput::new(
                other_channel,
                "America/Denver",
                vec![JournalTake {
                    journal_id: format!("station-worker-{other_channel}"),
                    journal_sequence: 1,
                    channel_id: other_channel.into(),
                    started_at_unix_ms: 1_700_000_000_000,
                    ended_at_unix_ms: 1_700_000_060_000,
                    state: AsRunState::Program,
                    content_id: Some("00000000-0000-4000-8000-000000000003".into()),
                    franchise_id: Some("00000000-0000-4000-8000-000000000004".into()),
                    category: Some("program".into()),
                    title: Some("Council".into()),
                    catalog_revision: Some(1),
                    schedule_revision: Some(1),
                    journal_hash: "f".repeat(64),
                    unavailable_reason: None,
                }],
            )
            .unwrap(),
        )
        .unwrap();
    store
        .persist_asrun_projection(&other, 1_700_000_180_000)
        .unwrap();
    assert!(
        store
            .page_asrun_projection(other_channel, Some(&window_cursor), 1)
            .is_err()
    );
}

#[test]
fn projection_failure_alert_is_deduplicated_and_resolves_after_catch_up() {
    let path = database();
    let store = StationStore::open(&path).unwrap();
    store
        .observe_asrun_projection_failure_and_route(CHANNEL, "journal prefix is corrupt", 1_000)
        .unwrap();
    store
        .observe_asrun_projection_failure_and_route(CHANNEL, "journal prefix is corrupt", 2_000)
        .unwrap();
    let firing: Vec<_> = store
        .list_alert_events()
        .unwrap()
        .into_iter()
        .filter(|event| event.resource_ref == CHANNEL)
        .collect();
    assert_eq!(firing.len(), 1);
    assert_eq!(
        firing[0].condition,
        station_domain::AlertConditionKind::AsRunProjectionFailure
    );
    store
        .resolve_asrun_projection_failure_after_catch_up(CHANNEL, 3_000)
        .unwrap();
    assert_eq!(
        store
            .read_alert_event(&firing[0].event_id)
            .unwrap()
            .unwrap()
            .state,
        station_domain::AlertEventState::Resolved
    );
}

fn append_scheduled(
    writer: &mut JournalWriter,
    sequence: u64,
    actual_on_air_at_unix_ms: i64,
    suffix: &str,
) {
    writer
        .append(&WorkerEventEnvelope {
            version: PROTOCOL_VERSION,
            worker_id: format!("station-worker-{CHANNEL}"),
            channel_id: CHANNEL.into(),
            sequence,
            event: WorkerEvent::ScheduledAssetOnAir {
                report_id: format!("report-{suffix}"),
                schedule_item_id: format!("00000000-0000-4000-8000-00000000002{suffix}"),
                schedule_item_revision: 1,
                asset_id: if suffix == "0" { "a" } else { "b" }.repeat(64),
                media_offset_ms: 0,
                worker_boot_id: "00000000-0000-4000-8000-000000000030".into(),
                actual_on_air_at_unix_ms,
            },
        })
        .unwrap();
}
