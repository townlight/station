use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use station_media_journal::{JournalWriter, read_events};
use station_media_protocol::{PROTOCOL_VERSION, WorkerEvent, WorkerEventEnvelope};
use station_reporting::runtime::{AsRunState, JournalTake, ProjectionInput, ReportingRuntime};
use station_storage::{StationStore, project_durable_journal, station_local_day_window};

static SEQUENCE: AtomicU64 = AtomicU64::new(0);
const CHANNEL: &str = "00000000-0000-4000-8000-000000000001";

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
    let page = store.page_asrun_projection(CHANNEL, None, 1).unwrap().unwrap();
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
        initial.checkpoint.source_anchor_frame_hash,
        initial.checkpoint.journal_hash,
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
        rolled.checkpoint.reader_file_identity,
        initial.checkpoint.reader_file_identity,
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
    let first = store.page_asrun_projection(CHANNEL, None, 1).unwrap().unwrap();
    let cursor = first.next_cursor.clone().expect("first page continues");
    let second = store
        .page_asrun_projection(CHANNEL, Some(&cursor), 1)
        .unwrap()
        .unwrap();
    assert_eq!(first.entries[0].journal_sequence, 1);
    assert_eq!(second.entries[0].journal_sequence, 2);

    let mut window_changed = projection.clone();
    window_changed.window = Some(station_local_day_window("America/Denver", 1_700_000_000_000).unwrap());
    store
        .persist_asrun_projection(&window_changed, 1_700_000_180_000)
        .unwrap();
    assert!(store
        .page_asrun_projection(CHANNEL, Some(&cursor), 1)
        .is_err());

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
    store
        .persist_asrun_projection(&changed, 1_700_000_180_000)
        .unwrap();
    assert!(store
        .page_asrun_projection(CHANNEL, Some(&window_cursor), 1)
        .is_err());

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
    assert!(store
        .page_asrun_projection(other_channel, Some(&window_cursor), 1)
        .is_err());
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
