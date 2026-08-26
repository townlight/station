use std::fs;
use std::net::UdpSocket;
use std::path::{Path, PathBuf};
use std::sync::{
    Arc, Mutex, MutexGuard,
    atomic::{AtomicBool, Ordering},
};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use station_captions::{
    AssetCaptionCue, AssetCaptionTrackWrite, CaptionEgressProofJob, CaptionTrackSourceKind,
    canonical_caption_text_sha256,
};
use station_domain::{AlertConditionKind, AlertEventState, ChannelRuntimeState};
use station_live::{LiveEndpoint, LiveSource, TakeoverSession, TakeoverState};
use station_media_journal::read_events;
use station_media_protocol::{ChannelCommand, WorkerEvent};
use station_orchestration::{ChannelController, DispatchTransition, run_until};
use station_runtime::WorkerSupervisor;
use station_schedule::{
    AssetReadiness, ChannelConfiguration, DispatchStatus, MediaAsset, OutputProfileId,
    OutputSinkConfiguration, ScheduleItem, ScheduleState,
};
use station_storage::{ChannelCaptionMode, ScheduleItemWrite, StationStore};

const WORKER_ID: &str = "256d5a07-92d3-4718-aec9-05cad42fae7d";
const CHANNEL_ID: &str = "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e";
const CAPTION_PROOF_JOB_ID: &str = "9a1044ce-1845-49a9-82f1-783ae0573aa8";
static SUPERVISION_TEST_LOCK: Mutex<()> = Mutex::new(());

trait ScheduleStoreTestExt {
    fn put_schedule_item(&self, item: &ScheduleItem) -> Result<(), String>;
}

impl ScheduleStoreTestExt for StationStore {
    fn put_schedule_item(&self, item: &ScheduleItem) -> Result<(), String> {
        self.write_schedule_item(&ScheduleItemWrite {
            item: item.clone(),
            changed_by: "channel-worker-test".into(),
            occurred_at_unix_ms: 1,
        })
    }
}

fn supervision_test_guard() -> MutexGuard<'static, ()> {
    SUPERVISION_TEST_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn journal_path() -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is before Unix epoch")
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-supervisor-{nonce}.tlj"))
}

#[test]
fn launches_handshakes_commands_and_cleanly_stops_the_real_worker() {
    let _guard = supervision_test_guard();
    let journal = journal_path();
    let media_root = journal.with_extension("media-test");
    fs::create_dir_all(&media_root).unwrap();
    let source = media_root.join("incoming.ts");
    let second_source = media_root.join("second.ts");
    generate_fixture(&source, "smpte");
    generate_fixture(&second_source, "snow");
    let asset = ingest_media(&source, media_root.join("library")).unwrap();
    let second_asset = ingest_media(&second_source, media_root.join("library")).unwrap();
    let output = UdpSocket::bind("127.0.0.1:0").unwrap();
    output
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let launched = WorkerSupervisor::launch_with_output_profile(
        PathBuf::from(env!("CARGO_BIN_EXE_channel-worker")).as_path(),
        WORKER_ID,
        CHANNEL_ID,
        &journal,
        output.local_addr().unwrap(),
        OutputProfileId::Atsc720p5994,
        Duration::from_secs(30),
    );
    assert!(
        launched.is_ok(),
        "supervisor failed to launch the worker: {:?}",
        launched.err()
    );
    let mut supervisor = launched.unwrap();
    let ready = supervisor.ready().clone();
    assert_eq!(ready.sequence, 1);
    assert!(matches!(
        ready.event,
        WorkerEvent::Ready { graph_revision: 1 }
    ));
    let mut datagram = [0_u8; 65_536];
    let received = output.recv(&mut datagram).unwrap();
    assert!(received >= 188);
    assert_eq!(received % 188, 0);
    assert_eq!(datagram[0], 0x47);

    let heartbeat = supervisor
        .command("ping-1", ChannelCommand::Ping, Duration::from_secs(1))
        .unwrap();
    assert_eq!(heartbeat.sequence, 2);
    let WorkerEvent::Heartbeat { ref outputs, .. } = heartbeat.event else {
        panic!("the real worker did not return a transport heartbeat");
    };
    assert_eq!(outputs.len(), 1);
    assert_eq!(outputs[0].output_index, 0);
    assert!(outputs[0].bytes_sent_total > 0);

    let applied = supervisor
        .command(
            "plan-1",
            ChannelCommand::ApplyPlan {
                plan_id: "weekday".into(),
                revision: 7,
            },
            Duration::from_secs(1),
        )
        .unwrap();
    assert_eq!(applied.sequence, 3);
    assert!(matches!(
        applied.event,
        WorkerEvent::Ready { graph_revision: 7 }
    ));

    let loaded = supervisor
        .command(
            "load-asset-1",
            ChannelCommand::LoadAsset {
                asset_id: asset.asset_id.clone(),
                media_path: asset.stored_path.to_string_lossy().into_owned(),
            },
            Duration::from_secs(5),
        )
        .unwrap();
    assert_eq!(loaded.sequence, 4);
    assert!(matches!(
        &loaded.event,
        WorkerEvent::AssetLoaded { asset_id } if asset_id == &asset.asset_id
    ));

    let on_air = supervisor
        .command(
            "take-asset-1",
            ChannelCommand::TakeScheduledAsset {
                report_id: "report-1".into(),
                schedule_item_id: "30d42a8e-07d2-4c90-b02e-b29c71a66d6b".into(),
                schedule_item_revision: 1,
                asset_id: asset.asset_id.clone(),
                media_offset_ms: 0,
            },
            Duration::from_secs(7),
        )
        .unwrap();
    assert_eq!(on_air.sequence, 5);
    let WorkerEvent::ScheduledAssetOnAir {
        report_id,
        schedule_item_id,
        schedule_item_revision,
        asset_id,
        media_offset_ms,
        worker_boot_id,
        actual_on_air_at_unix_ms,
    } = &on_air.event
    else {
        panic!("the scheduled take did not produce an authoritative on-air event");
    };
    assert_eq!(report_id, "report-1");
    assert_eq!(schedule_item_id, "30d42a8e-07d2-4c90-b02e-b29c71a66d6b");
    assert_eq!(*schedule_item_revision, 1);
    assert_eq!(asset_id, &asset.asset_id);
    assert_eq!(*media_offset_ms, 0);
    assert_eq!(worker_boot_id.len(), 36);
    assert_eq!(worker_boot_id.as_bytes()[8], b'-');
    assert_eq!(worker_boot_id.as_bytes()[13], b'-');
    assert_eq!(worker_boot_id.as_bytes()[18], b'-');
    assert_eq!(worker_boot_id.as_bytes()[23], b'-');
    assert!(*actual_on_air_at_unix_ms >= 0);
    let received = output.recv(&mut datagram).unwrap();
    assert_eq!(received % 188, 0);

    let returned = supervisor
        .command(
            "return-1",
            ChannelCommand::ReturnToSchedule,
            Duration::from_secs(30),
        )
        .unwrap();
    assert_eq!(returned.sequence, 6);
    assert!(matches!(
        &returned.event,
        WorkerEvent::OnAirChanged { source_kind, .. } if source_kind == "fallback"
    ));

    let second_loaded = supervisor
        .command(
            "load-asset-2",
            ChannelCommand::LoadAsset {
                asset_id: second_asset.asset_id.clone(),
                media_path: second_asset.stored_path.to_string_lossy().into_owned(),
            },
            Duration::from_secs(5),
        )
        .unwrap();
    assert_eq!(second_loaded.sequence, 7);
    assert!(matches!(
        &second_loaded.event,
        WorkerEvent::AssetLoaded { asset_id } if asset_id == &second_asset.asset_id
    ));
    let second_on_air = supervisor
        .command(
            "take-asset-2",
            ChannelCommand::TakeAsset {
                asset_id: second_asset.asset_id.clone(),
                media_offset_ms: 0,
            },
            Duration::from_secs(7),
        )
        .unwrap();
    assert_eq!(second_on_air.sequence, 8);
    assert!(matches!(
        &second_on_air.event,
        WorkerEvent::OnAirChanged { source_kind, source_id }
            if source_kind == "asset" && source_id == &second_asset.asset_id
    ));
    let second_returned = supervisor
        .command(
            "return-2",
            ChannelCommand::ReturnToSchedule,
            Duration::from_secs(30),
        )
        .unwrap();
    assert_eq!(second_returned.sequence, 9);

    let stopped = supervisor
        .shutdown("shutdown-1", Duration::from_secs(3))
        .unwrap();
    assert_eq!(stopped.sequence, 10);
    assert!(matches!(stopped.event, WorkerEvent::ShutdownComplete));

    let restarted = WorkerSupervisor::launch(
        PathBuf::from(env!("CARGO_BIN_EXE_channel-worker")).as_path(),
        WORKER_ID,
        CHANNEL_ID,
        &journal,
        output.local_addr().unwrap(),
        Duration::from_secs(30),
    )
    .unwrap();
    let restart_ready = restarted.ready().clone();
    assert_eq!(restart_ready.sequence, 11);
    let restart_stopped = restarted
        .shutdown("shutdown-2", Duration::from_secs(3))
        .unwrap();
    assert_eq!(restart_stopped.sequence, 12);

    assert_eq!(
        read_events(&journal).unwrap(),
        vec![
            ready,
            heartbeat,
            applied,
            loaded,
            on_air,
            returned,
            second_loaded,
            second_on_air,
            second_returned,
            stopped,
            restart_ready,
            restart_stopped
        ]
    );
    let _ = std::fs::remove_file(journal);
    remove_directory(&media_root);
}

#[test]
fn committed_schedule_is_loaded_aired_and_completed_only_after_worker_acknowledgments() {
    let _guard = supervision_test_guard();
    let journal = journal_path();
    let database = journal.with_extension("dispatch.db");
    let media_root = journal.with_extension("dispatch-media");
    fs::create_dir_all(&media_root).unwrap();
    let source = media_root.join("scheduled.ts");
    generate_fixture(&source, "ball");
    let ingested = ingest_media(&source, media_root.join("library")).unwrap();
    let output = UdpSocket::bind("127.0.0.1:0").unwrap();
    output
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let channel = ChannelConfiguration {
        channel_id: CHANNEL_ID.into(),
        display_name: "Primary Cable Channel".into(),
        output_profile: Default::default(),
        output_sinks: vec![OutputSinkConfiguration::required_udp(
            CHANNEL_ID,
            "Primary transport",
            output.local_addr().unwrap().to_string(),
            -16.0,
        )],
        enabled: true,
    };
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64;
    let starts_at = now + 1_000;
    let store = StationStore::open(&database).unwrap();
    store.put_channel(&channel).unwrap();
    store
        .put_media_asset(&MediaAsset {
            asset_id: ingested.asset_id.clone(),
            media_path: ingested.stored_path.to_string_lossy().into_owned(),
            duration_ms: 60_000,
            readiness: AssetReadiness::Ready,
        })
        .unwrap();
    store
        .put_schedule_item(&ScheduleItem {
            item_id: WORKER_ID.into(),
            revision: 1,
            channel_id: CHANNEL_ID.into(),
            asset_id: ingested.asset_id.clone(),
            title: "Automated council playback".into(),
            starts_at_unix_ms: starts_at,
            duration_ms: 60_000,
            state: ScheduleState::Draft,
        })
        .unwrap();
    store
        .commit_schedule(
            "dispatch-report-1",
            "dispatch-plan-1",
            WORKER_ID,
            "operator-test",
            now,
            "Approved for automated dispatch.",
        )
        .unwrap();
    drop(store);

    let mut controller = ChannelController::launch(
        PathBuf::from(env!("CARGO_BIN_EXE_channel-worker")).as_path(),
        &database,
        &journal,
        &channel,
        Duration::from_secs(7),
    )
    .unwrap();
    let mut datagram = [0_u8; 65_536];
    assert_eq!(output.recv(&mut datagram).unwrap() % 188, 0);

    assert!(matches!(
        controller.run_once(now).unwrap(),
        Some(DispatchTransition::Loaded { .. })
    ));
    assert_eq!(
        StationStore::open(&database)
            .unwrap()
            .read_commit_report("dispatch-report-1")
            .unwrap()
            .unwrap()
            .dispatch_status,
        DispatchStatus::Queued
    );
    assert_eq!(controller.run_once(starts_at - 1).unwrap(), None);
    controller.shutdown(Duration::from_secs(3)).unwrap();
    let mut controller = ChannelController::launch(
        PathBuf::from(env!("CARGO_BIN_EXE_channel-worker")).as_path(),
        &database,
        &journal,
        &channel,
        Duration::from_secs(7),
    )
    .unwrap();
    assert!(matches!(
        controller.run_once(starts_at).unwrap(),
        Some(DispatchTransition::Loaded { .. })
    ));
    assert!(matches!(
        controller.run_once(starts_at).unwrap(),
        Some(DispatchTransition::OnAir { .. })
    ));
    assert_eq!(
        StationStore::open(&database)
            .unwrap()
            .read_commit_report("dispatch-report-1")
            .unwrap()
            .unwrap()
            .dispatch_status,
        DispatchStatus::Acknowledged
    );
    controller.shutdown(Duration::from_secs(3)).unwrap();
    let mut controller = ChannelController::launch(
        PathBuf::from(env!("CARGO_BIN_EXE_channel-worker")).as_path(),
        &database,
        &journal,
        &channel,
        Duration::from_secs(7),
    )
    .unwrap();
    assert!(matches!(
        controller.run_once(starts_at + 500).unwrap(),
        Some(DispatchTransition::Loaded { .. })
    ));
    assert!(matches!(
        controller.run_once(starts_at + 500).unwrap(),
        Some(DispatchTransition::OnAir { .. })
    ));
    assert!(matches!(
        controller.run_once(starts_at + 60_000).unwrap(),
        Some(DispatchTransition::Completed { .. })
    ));
    assert_eq!(
        StationStore::open(&database)
            .unwrap()
            .read_commit_report("dispatch-report-1")
            .unwrap()
            .unwrap()
            .dispatch_status,
        DispatchStatus::Completed
    );
    controller.shutdown(Duration::from_secs(3)).unwrap();
    let events = read_events(&journal).unwrap();
    assert_eq!(
        events
            .iter()
            .filter(|event| matches!(event.event, WorkerEvent::AssetLoaded { .. }))
            .count(),
        3
    );
    assert_eq!(
        events
            .iter()
            .filter(|event| matches!(event.event, WorkerEvent::ScheduledAssetOnAir { .. }))
            .count(),
        1
    );
    assert_eq!(
        events
            .iter()
            .filter(|event| matches!(event.event, WorkerEvent::OnAirChanged { ref source_kind, .. } if source_kind == "asset"))
            .count(),
        1
    );
    assert!(events.iter().any(
        |event| matches!(event.event, WorkerEvent::OnAirChanged { ref source_kind, .. } if source_kind == "fallback")
    ));
    assert_eq!(
        events
            .iter()
            .filter(|event| matches!(event.event, WorkerEvent::ShutdownComplete))
            .count(),
        3
    );

    let _ = fs::remove_file(journal);
    for candidate in [
        database.clone(),
        PathBuf::from(format!("{}-wal", database.display())),
        PathBuf::from(format!("{}-shm", database.display())),
    ] {
        let _ = fs::remove_file(candidate);
    }
    remove_directory(&media_root);
}

#[test]
fn caption_proof_binding_rejects_a_recommitted_schedule_revision() {
    let _guard = supervision_test_guard();
    let journal = journal_path();
    let database = journal.with_extension("caption-stale.db");
    let media_root = journal.with_extension("caption-stale-media");
    fs::create_dir_all(&media_root).unwrap();
    let media_path = media_root.join("captioned.ts");
    fs::write(&media_path, b"caption proof stale revision fixture").unwrap();
    let output = UdpSocket::bind("127.0.0.1:0").unwrap();
    let mut channel = ChannelConfiguration {
        channel_id: CHANNEL_ID.into(),
        display_name: "Caption stale revision".into(),
        output_profile: Default::default(),
        output_sinks: vec![OutputSinkConfiguration::required_udp(
            CHANNEL_ID,
            "Primary transport",
            output.local_addr().unwrap().to_string(),
            -16.0,
        )],
        enabled: false,
    };
    let store = StationStore::open(&database).unwrap();
    let asset_id = "a".repeat(64);
    store.put_channel(&channel).unwrap();
    store
        .put_media_asset(&MediaAsset {
            asset_id: asset_id.clone(),
            media_path: media_path.to_string_lossy().into_owned(),
            duration_ms: 60_000,
            readiness: AssetReadiness::Ready,
        })
        .unwrap();
    store
        .write_channel_caption_policy(
            CHANNEL_ID,
            ChannelCaptionMode::Cea708,
            1,
            "en-US",
            0,
            "setup-admin",
            90_001,
            "eaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        )
        .unwrap();
    let track = store
        .write_asset_caption_track(&AssetCaptionTrackWrite {
            track_id: "faaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee".into(),
            asset_id: asset_id.clone(),
            language: "en-US".into(),
            service_number: 1,
            source_kind: CaptionTrackSourceKind::External,
            label: "Reviewed English".into(),
            cues: vec![AssetCaptionCue {
                cue_id: "abaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee".into(),
                start_ms: 1_000,
                duration_ms: 2_000,
                text: "Town Council".into(),
            }],
            expected_revision: 0,
            actor: "setup-admin".into(),
            occurred_at_unix_ms: 90_002,
            audit_id: "acaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee".into(),
        })
        .unwrap();
    store
        .put_schedule_item(&ScheduleItem {
            item_id: WORKER_ID.into(),
            revision: 1,
            channel_id: CHANNEL_ID.into(),
            asset_id: asset_id.clone(),
            title: "Caption proof stale revision".into(),
            starts_at_unix_ms: 100_000,
            duration_ms: 60_000,
            state: ScheduleState::Draft,
        })
        .unwrap();
    store
        .commit_schedule(
            "caption-stale-report",
            "caption-stale-plan",
            WORKER_ID,
            "operator-test",
            90_000,
            "Caption proof stale revision fixture",
        )
        .unwrap();
    store
        .advance_dispatch_status(
            "caption-stale-report",
            DispatchStatus::Pending,
            DispatchStatus::Queued,
        )
        .unwrap();
    store
        .advance_dispatch_status(
            "caption-stale-report",
            DispatchStatus::Queued,
            DispatchStatus::Acknowledged,
        )
        .unwrap();
    let configuration_hash = store
        .current_channel_output_configuration_hash(CHANNEL_ID)
        .unwrap();
    let expected_text_sha256 = canonical_caption_text_sha256(&["Town Council".into()]).unwrap();
    store
        .create_caption_egress_job(&CaptionEgressProofJob {
            job_id: CAPTION_PROOF_JOB_ID.into(),
            channel_id: CHANNEL_ID.into(),
            sink_id: CHANNEL_ID.into(),
            report_id: "caption-stale-report".into(),
            asset_id: asset_id.clone(),
            track_id: track.track_id,
            track_revision: track.revision,
            cues_sha256: track.cues_sha256,
            caption_policy_revision: 1,
            language: "en-US".into(),
            service_number: 1,
            candidate_version: "0.1.0".into(),
            source_commit: "a".repeat(40),
            configuration_hash,
            capture_started_asset_offset_ms: 0,
            capture_duration_ms: 3_000,
            max_capture_bytes: 1_000_000,
            expected_cue_count: 1,
            expected_text_sha256,
            created_at_unix_ms: 90_003,
        })
        .unwrap();
    channel.enabled = true;
    store.put_channel(&channel).unwrap();

    let current = store.read_schedule_item(WORKER_ID).unwrap().unwrap();
    let mut draft = current.clone();
    draft.revision += 1;
    draft.state = ScheduleState::Draft;
    draft.title = "Caption proof stale revision two".into();
    store.put_schedule_item(&draft).unwrap();
    store
        .commit_schedule(
            "caption-stale-recommit",
            "caption-stale-plan-two",
            WORKER_ID,
            "operator-test",
            90_004,
            "Recommitted after caption proof request",
        )
        .unwrap();
    store
        .advance_dispatch_status(
            "caption-stale-recommit",
            DispatchStatus::Pending,
            DispatchStatus::Cancelled,
        )
        .unwrap();
    drop(store);

    let mut controller = ChannelController::launch(
        PathBuf::from(env!("CARGO_BIN_EXE_channel-worker")).as_path(),
        &database,
        &journal,
        &channel,
        Duration::from_secs(7),
    )
    .unwrap();
    let error = controller.run_once(100_000).unwrap_err();
    assert!(
        matches!(error, station_orchestration::OrchestrationError::Storage(ref detail)
            if detail.contains("stale schedule item revision"))
    );
    controller.shutdown(Duration::from_secs(3)).unwrap();

    let store = StationStore::open(&database).unwrap();
    let job = store
        .read_caption_egress_job(CAPTION_PROOF_JOB_ID)
        .unwrap()
        .unwrap();
    assert_ne!(
        job.state,
        station_captions::CaptionEgressProofJobState::Completed
    );

    let _ = fs::remove_file(journal);
    for candidate in [
        database.clone(),
        PathBuf::from(format!("{}-wal", database.display())),
        PathBuf::from(format!("{}-shm", database.display())),
    ] {
        let _ = fs::remove_file(candidate);
    }
    remove_directory(&media_root);
}

#[test]
fn expired_unstarted_item_is_missed_and_current_item_takes_at_catch_up_time() {
    let _guard = supervision_test_guard();
    const CURRENT_ITEM: &str = "d2b12490-04bf-466f-9317-a19953229642";
    let journal = journal_path();
    let database = journal.with_extension("catch-up.db");
    let media_root = journal.with_extension("catch-up-media");
    fs::create_dir_all(&media_root).unwrap();
    let stale_source = media_root.join("stale.ts");
    let current_source = media_root.join("current.ts");
    generate_fixture(&stale_source, "smpte");
    generate_fixture(&current_source, "snow");
    let stale_asset = ingest_media(&stale_source, media_root.join("library")).unwrap();
    let current_asset = ingest_media(&current_source, media_root.join("library")).unwrap();
    let output = UdpSocket::bind("127.0.0.1:0").unwrap();
    output
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let channel = ChannelConfiguration {
        channel_id: CHANNEL_ID.into(),
        display_name: "Catch-up channel".into(),
        output_profile: Default::default(),
        output_sinks: vec![OutputSinkConfiguration::required_udp(
            CHANNEL_ID,
            "Primary transport",
            output.local_addr().unwrap().to_string(),
            -16.0,
        )],
        enabled: true,
    };
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64;
    let store = StationStore::open(&database).unwrap();
    store.put_channel(&channel).unwrap();
    for asset in [&stale_asset, &current_asset] {
        store
            .put_media_asset(&MediaAsset {
                asset_id: asset.asset_id.clone(),
                media_path: asset.stored_path.to_string_lossy().into_owned(),
                duration_ms: 60_000,
                readiness: AssetReadiness::Ready,
            })
            .unwrap();
    }
    for (item_id, asset_id, start, duration, report_id) in [
        (
            WORKER_ID,
            stale_asset.asset_id.as_str(),
            now - 4_000,
            1_000,
            "catch-up-stale-report",
        ),
        (
            CURRENT_ITEM,
            current_asset.asset_id.as_str(),
            now - 500,
            60_000,
            "catch-up-current-report",
        ),
    ] {
        store
            .put_schedule_item(&ScheduleItem {
                item_id: item_id.into(),
                revision: 1,
                channel_id: CHANNEL_ID.into(),
                asset_id: asset_id.into(),
                title: report_id.into(),
                starts_at_unix_ms: start,
                duration_ms: duration,
                state: ScheduleState::Draft,
            })
            .unwrap();
        store
            .commit_schedule(
                report_id,
                &format!("{report_id}-plan"),
                item_id,
                "operator-test",
                now - 5_000,
                "Approved before live interruption.",
            )
            .unwrap();
    }
    drop(store);

    let mut controller = ChannelController::launch(
        PathBuf::from(env!("CARGO_BIN_EXE_channel-worker")).as_path(),
        &database,
        &journal,
        &channel,
        Duration::from_secs(7),
    )
    .unwrap();
    assert!(matches!(
        controller.run_once(now).unwrap(),
        Some(DispatchTransition::Loaded { ref report_id })
            if report_id == "catch-up-current-report"
    ));
    let store = StationStore::open(&database).unwrap();
    assert_eq!(
        store
            .read_commit_report("catch-up-stale-report")
            .unwrap()
            .unwrap()
            .dispatch_status,
        DispatchStatus::Missed
    );
    drop(store);
    assert!(matches!(
        controller.run_once(now).unwrap(),
        Some(DispatchTransition::OnAir { ref report_id })
            if report_id == "catch-up-current-report"
    ));
    controller.shutdown(Duration::from_secs(3)).unwrap();

    let events = read_events(&journal).unwrap();
    assert!(!events.iter().any(|event| {
        matches!(
            &event.event,
            WorkerEvent::ScheduledAssetOnAir { asset_id, .. }
                if asset_id == &stale_asset.asset_id
        )
    }));
    assert!(events.iter().any(|event| {
        matches!(
            &event.event,
            WorkerEvent::ScheduledAssetOnAir { asset_id, .. }
                if asset_id == &current_asset.asset_id
        )
    }));

    let _ = fs::remove_file(journal);
    for candidate in [
        database.clone(),
        PathBuf::from(format!("{}-wal", database.display())),
        PathBuf::from(format!("{}-shm", database.display())),
    ] {
        let _ = fs::remove_file(candidate);
    }
    remove_directory(&media_root);
}

#[test]
fn long_live_takeover_hands_back_to_the_schedule_item_valid_now() {
    let _guard = supervision_test_guard();
    const NEXT_ITEM: &str = "d2b12490-04bf-466f-9317-a19953229642";
    const LIVE_SOURCE: &str = "3f5f721c-bdf8-419a-8a2e-b0a7caa1ff7e";
    const LIVE_SESSION: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    let journal = journal_path();
    let database = journal.with_extension("live-catch-up.db");
    let media_root = journal.with_extension("live-catch-up-media");
    fs::create_dir_all(&media_root).unwrap();
    let first_source = media_root.join("first.ts");
    let next_source = media_root.join("next.ts");
    generate_fixture(&first_source, "smpte");
    generate_fixture(&next_source, "snow");
    let first_asset = ingest_media(&first_source, media_root.join("library")).unwrap();
    let next_asset = ingest_media(&next_source, media_root.join("library")).unwrap();
    let output = UdpSocket::bind("127.0.0.1:0").unwrap();
    output
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let live_probe = UdpSocket::bind("127.0.0.1:0").unwrap();
    let live_port = live_probe.local_addr().unwrap().port();
    drop(live_probe);
    let live_sender = start_udp_live_sender(live_port);
    thread::sleep(Duration::from_millis(500));
    let channel = ChannelConfiguration {
        channel_id: CHANNEL_ID.into(),
        display_name: "Live catch-up channel".into(),
        output_profile: Default::default(),
        output_sinks: vec![OutputSinkConfiguration::required_udp(
            CHANNEL_ID,
            "Primary transport",
            output.local_addr().unwrap().to_string(),
            -16.0,
        )],
        enabled: true,
    };
    let starts_at = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64
        - 1_000;
    let next_starts_at = starts_at + 60_000;
    let store = StationStore::open(&database).unwrap();
    store.put_channel(&channel).unwrap();
    store
        .write_live_source(
            &LiveSource {
                source_id: LIVE_SOURCE.into(),
                channel_id: CHANNEL_ID.into(),
                label: "Council chamber".into(),
                enabled: true,
                endpoint: LiveEndpoint::UdpMpegTs {
                    bind_ip: "127.0.0.1".into(),
                    port: live_port,
                },
                revision: 1,
            },
            0,
        )
        .unwrap();
    for asset in [&first_asset, &next_asset] {
        store
            .put_media_asset(&MediaAsset {
                asset_id: asset.asset_id.clone(),
                media_path: asset.stored_path.to_string_lossy().into_owned(),
                duration_ms: 120_000,
                readiness: AssetReadiness::Ready,
            })
            .unwrap();
    }
    for (item_id, asset_id, start, duration, report_id) in [
        (
            WORKER_ID,
            first_asset.asset_id.as_str(),
            starts_at,
            60_000,
            "live-catch-up-first",
        ),
        (
            NEXT_ITEM,
            next_asset.asset_id.as_str(),
            next_starts_at,
            60_000,
            "live-catch-up-next",
        ),
    ] {
        store
            .put_schedule_item(&ScheduleItem {
                item_id: item_id.into(),
                revision: 1,
                channel_id: CHANNEL_ID.into(),
                asset_id: asset_id.into(),
                title: report_id.into(),
                starts_at_unix_ms: start,
                duration_ms: duration,
                state: ScheduleState::Draft,
            })
            .unwrap();
        store
            .commit_schedule(
                report_id,
                &format!("{report_id}-plan"),
                item_id,
                "operator-test",
                starts_at - 1,
                "Approved before live interruption.",
            )
            .unwrap();
    }
    drop(store);

    let mut controller = ChannelController::launch(
        PathBuf::from(env!("CARGO_BIN_EXE_channel-worker")).as_path(),
        &database,
        &journal,
        &channel,
        Duration::from_secs(7),
    )
    .unwrap();
    assert!(matches!(
        controller.run_once(starts_at).unwrap(),
        Some(DispatchTransition::Loaded { .. })
    ));
    assert!(matches!(
        controller.run_once(starts_at).unwrap(),
        Some(DispatchTransition::OnAir { .. })
    ));
    let store = StationStore::open(&database).unwrap();
    store
        .request_takeover(
            &TakeoverSession {
                session_id: LIVE_SESSION.into(),
                channel_id: CHANNEL_ID.into(),
                source_id: LIVE_SOURCE.into(),
                source_revision: 1,
                authority_revision: 1,
                operator_id: "operator-test".into(),
                reason: Some("Live council meeting".into()),
                requested_at_unix_ms: starts_at + 100,
                expires_at_unix_ms: starts_at + 180_100,
                taken_at_unix_ms: None,
                returned_at_unix_ms: None,
                state: TakeoverState::Arming,
                failure_detail: None,
            },
            0,
        )
        .unwrap();
    drop(store);
    assert!(matches!(
        controller.run_once(starts_at + 100).unwrap(),
        Some(DispatchTransition::LiveArmed { .. })
    ));
    let store = StationStore::open(&database).unwrap();
    store
        .transition_takeover(
            LIVE_SESSION,
            TakeoverState::Armed,
            TakeoverState::TakeRequested,
            starts_at + 200,
            None,
            "operator-test",
            2,
        )
        .unwrap();
    drop(store);
    assert!(matches!(
        controller.run_once(starts_at + 200).unwrap(),
        Some(DispatchTransition::LiveOnAir { .. })
    ));
    let live_runtime = controller.runtime_observation(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis() as i64,
    );
    assert_eq!(live_runtime.state, ChannelRuntimeState::Live);
    assert_eq!(live_runtime.on_air_asset_id, None);
    StationStore::open(&database)
        .unwrap()
        .put_channel_runtime(&live_runtime)
        .unwrap();
    assert_eq!(controller.run_once(next_starts_at - 500).unwrap(), None);
    let store = StationStore::open(&database).unwrap();
    store
        .transition_takeover(
            LIVE_SESSION,
            TakeoverState::OnAir,
            TakeoverState::ReturnRequested,
            next_starts_at + 500,
            None,
            "operator-test",
            4,
        )
        .unwrap();
    drop(store);
    assert!(matches!(
        controller.run_once(next_starts_at + 500).unwrap(),
        Some(DispatchTransition::LiveCompleted { .. })
    ));
    let store = StationStore::open(&database).unwrap();
    assert_eq!(
        store
            .read_commit_report("live-catch-up-first")
            .unwrap()
            .unwrap()
            .dispatch_status,
        DispatchStatus::Completed
    );
    assert_eq!(
        store
            .read_commit_report("live-catch-up-next")
            .unwrap()
            .unwrap()
            .dispatch_status,
        DispatchStatus::Acknowledged
    );
    let completed = store.read_takeover_session(LIVE_SESSION).unwrap().unwrap();
    assert_eq!(completed.state, TakeoverState::Completed);
    assert!(
        store
            .list_takeover_audit(LIVE_SESSION)
            .unwrap()
            .last()
            .unwrap()
            .detail
            .as_deref()
            .is_some_and(|detail| detail.contains(&next_asset.asset_id))
    );
    drop(store);
    controller.shutdown(Duration::from_secs(3)).unwrap();
    let events = read_events(&journal).unwrap();
    assert!(events.iter().any(|event| {
        matches!(
            &event.event,
            WorkerEvent::LiveReturned { source_kind, source_id, .. }
                if source_kind == "asset" && source_id == &next_asset.asset_id
        )
    }));

    live_sender.set_state(gst::State::Null).unwrap();
    let _ = fs::remove_file(journal);
    for candidate in [
        database.clone(),
        PathBuf::from(format!("{}-wal", database.display())),
        PathBuf::from(format!("{}-shm", database.display())),
    ] {
        let _ = fs::remove_file(candidate);
    }
    remove_directory(&media_root);
}

#[test]
fn daemon_loop_discovers_an_enabled_channel_and_completes_its_committed_item() {
    let _guard = supervision_test_guard();
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let root = std::env::temp_dir().join(format!("townlight-daemon-dispatch-{nonce}"));
    fs::create_dir_all(&root).unwrap();
    let database = root.join("station.db");
    let source = root.join("scheduled.ts");
    generate_fixture(&source, "zone-plate");
    let ingested = ingest_media(&source, root.join("library")).unwrap();
    let output = UdpSocket::bind("127.0.0.1:0").unwrap();
    let redundant_output = UdpSocket::bind("127.0.0.1:0").unwrap();
    for receiver in [&output, &redundant_output] {
        receiver
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
    }
    let channel = ChannelConfiguration {
        channel_id: CHANNEL_ID.into(),
        display_name: "Daemon-owned channel".into(),
        output_profile: Default::default(),
        output_sinks: vec![
            OutputSinkConfiguration::required_udp(
                CHANNEL_ID,
                "Primary transport",
                output.local_addr().unwrap().to_string(),
                -16.0,
            ),
            OutputSinkConfiguration::required_udp(
                "44444444-5555-4666-8777-888888888888",
                "Redundant transport",
                redundant_output.local_addr().unwrap().to_string(),
                -16.0,
            ),
        ],
        enabled: true,
    };
    let store = StationStore::open(&database).unwrap();
    store.put_channel(&channel).unwrap();
    store
        .put_media_asset(&MediaAsset {
            asset_id: ingested.asset_id.clone(),
            media_path: ingested.stored_path.to_string_lossy().into_owned(),
            duration_ms: 6_000,
            readiness: AssetReadiness::Ready,
        })
        .unwrap();
    let asset_id = ingested.asset_id;
    drop(store);

    let stop = Arc::new(AtomicBool::new(false));
    let runner_stop = Arc::clone(&stop);
    let runner_database = database.clone();
    let worker = PathBuf::from(env!("CARGO_BIN_EXE_channel-worker"));
    let runner = thread::spawn(move || run_until(&runner_database, &worker, runner_stop));
    let startup_deadline = std::time::Instant::now() + Duration::from_secs(60);
    loop {
        let runtime = StationStore::open(&database)
            .unwrap()
            .read_channel_runtime(CHANNEL_ID)
            .unwrap();
        if runtime.is_some_and(|runtime| runtime.state == ChannelRuntimeState::HealthyFallback) {
            break;
        }
        assert!(
            std::time::Instant::now() < startup_deadline,
            "daemon did not reach healthy fallback before scheduling"
        );
        thread::sleep(Duration::from_millis(50));
    }
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64;
    let store = StationStore::open(&database).unwrap();
    store
        .put_schedule_item(&ScheduleItem {
            item_id: WORKER_ID.into(),
            revision: 1,
            channel_id: CHANNEL_ID.into(),
            asset_id,
            title: "Daemon dispatch proof".into(),
            starts_at_unix_ms: now + 1_000,
            duration_ms: 2_000,
            state: ScheduleState::Draft,
        })
        .unwrap();
    store
        .commit_schedule(
            "daemon-report-1",
            "daemon-plan-1",
            WORKER_ID,
            "operator-test",
            now,
            "Prove daemon discovery.",
        )
        .unwrap();
    drop(store);
    let deadline = std::time::Instant::now() + Duration::from_secs(10);
    loop {
        let status = StationStore::open(&database)
            .unwrap()
            .read_commit_report("daemon-report-1")
            .unwrap()
            .unwrap()
            .dispatch_status;
        if status == DispatchStatus::Completed {
            break;
        }
        assert!(
            std::time::Instant::now() < deadline,
            "daemon dispatch stalled in {status:?}"
        );
        thread::sleep(Duration::from_millis(50));
    }
    stop.store(true, Ordering::Release);
    runner.join().unwrap().unwrap();
    for receiver in [&output, &redundant_output] {
        let mut datagram = [0_u8; 65_536];
        let received = receiver.recv(&mut datagram).unwrap();
        assert!(received >= 188);
        assert_eq!(received % 188, 0);
        assert_eq!(datagram[0], 0x47);
    }
    let events = read_events(
        root.join("channel-journals")
            .join(format!("{CHANNEL_ID}.tlj")),
    )
    .unwrap();
    assert!(
        events
            .iter()
            .any(|event| matches!(event.event, WorkerEvent::ScheduledAssetOnAir { .. }))
    );
    assert!(events.iter().any(
        |event| matches!(event.event, WorkerEvent::OnAirChanged { ref source_kind, .. } if source_kind == "fallback")
    ));
    remove_directory(&root);
}

#[test]
fn daemon_records_one_deduplicated_incident_per_condition_when_worker_cannot_start() {
    let _guard = supervision_test_guard();
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let root = std::env::temp_dir().join(format!("townlight-daemon-alert-{nonce}"));
    fs::create_dir_all(&root).unwrap();
    let database = root.join("station.db");
    let store = StationStore::open(&database).unwrap();
    store
        .put_channel(&ChannelConfiguration {
            channel_id: CHANNEL_ID.into(),
            display_name: "Failure proof channel".into(),
            output_profile: Default::default(),
            output_sinks: vec![OutputSinkConfiguration::required_udp(
                CHANNEL_ID,
                "Primary transport",
                "127.0.0.1:5519",
                -16.0,
            )],
            enabled: true,
        })
        .unwrap();
    drop(store);

    let stop = Arc::new(AtomicBool::new(false));
    let runner_stop = Arc::clone(&stop);
    let runner_database = database.clone();
    let missing_worker = root.join("missing-channel-worker.exe");
    let runner = thread::spawn(move || run_until(&runner_database, &missing_worker, runner_stop));
    thread::sleep(Duration::from_millis(1_300));
    stop.store(true, Ordering::Release);
    runner.join().unwrap().unwrap();

    let store = StationStore::open(&database).unwrap();
    let runtime = store.read_channel_runtime(CHANNEL_ID).unwrap().unwrap();
    assert_eq!(runtime.state, ChannelRuntimeState::Error);
    let alerts = store.list_alert_events().unwrap();
    assert_eq!(alerts.len(), 2);
    assert!(
        alerts
            .iter()
            .all(|event| event.state == AlertEventState::Firing)
    );
    assert!(alerts.iter().all(|event| event.occurrence_count >= 2));
    assert!(alerts.iter().all(|event| {
        event
            .detail
            .contains("automatic restart attempts are active")
    }));
    assert!(
        alerts
            .iter()
            .any(|event| event.condition == AlertConditionKind::EncoderDeath)
    );
    assert!(
        alerts
            .iter()
            .any(|event| event.condition == AlertConditionKind::OffAir)
    );
    drop(store);
    remove_directory(&root);
}

fn remove_directory(path: &Path) {
    let deadline = std::time::Instant::now() + Duration::from_secs(2);
    loop {
        match fs::remove_dir_all(path) {
            Ok(()) => return,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return,
            Err(_) if std::time::Instant::now() < deadline => {
                thread::sleep(Duration::from_millis(25));
            }
            Err(error) => panic!(
                "could not remove {} after worker exit: {error}",
                path.display()
            ),
        }
    }
}

fn start_udp_live_sender(port: u16) -> gst::Pipeline {
    gst::init().unwrap();
    let pipeline = gst::Pipeline::new();
    let video = gst::ElementFactory::make("videotestsrc")
        .property("is-live", true)
        .property_from_str("pattern", "zone-plate")
        .build()
        .unwrap();
    let video_convert = gst::ElementFactory::make("videoconvert").build().unwrap();
    let video_caps = gst::ElementFactory::make("capsfilter")
        .property(
            "caps",
            gst::Caps::builder("video/x-raw")
                .field("format", "I420")
                .field("width", 320_i32)
                .field("height", 180_i32)
                .field("framerate", gst::Fraction::new(30, 1))
                .build(),
        )
        .build()
        .unwrap();
    let video_encoder = gst::ElementFactory::make("openh264enc").build().unwrap();
    let video_parser = gst::ElementFactory::make("h264parse").build().unwrap();
    let audio = gst::ElementFactory::make("audiotestsrc")
        .property("is-live", true)
        .property_from_str("wave", "sine")
        .build()
        .unwrap();
    let audio_convert = gst::ElementFactory::make("audioconvert").build().unwrap();
    let audio_resample = gst::ElementFactory::make("audioresample").build().unwrap();
    let audio_encoder = gst::ElementFactory::make("voaacenc").build().unwrap();
    let audio_parser = gst::ElementFactory::make("aacparse").build().unwrap();
    let mux = gst::ElementFactory::make("mpegtsmux").build().unwrap();
    let sink = gst::ElementFactory::make("udpsink")
        .property("host", "127.0.0.1")
        .property("port", i32::from(port))
        .property("sync", true)
        .property("async", false)
        .build()
        .unwrap();
    pipeline
        .add_many([
            &video,
            &video_convert,
            &video_caps,
            &video_encoder,
            &video_parser,
            &audio,
            &audio_convert,
            &audio_resample,
            &audio_encoder,
            &audio_parser,
            &mux,
            &sink,
        ])
        .unwrap();
    gst::Element::link_many([
        &video,
        &video_convert,
        &video_caps,
        &video_encoder,
        &video_parser,
        &mux,
        &sink,
    ])
    .unwrap();
    gst::Element::link_many([
        &audio,
        &audio_convert,
        &audio_resample,
        &audio_encoder,
        &audio_parser,
        &mux,
    ])
    .unwrap();
    pipeline.set_state(gst::State::Playing).unwrap();
    pipeline
}

fn generate_fixture(path: &Path, pattern: &'static str) {
    gst::init().unwrap();
    let pipeline = gst::Pipeline::new();
    let video = gst::ElementFactory::make("videotestsrc")
        .property("num-buffers", 180_i32)
        .property_from_str("pattern", pattern)
        .build()
        .unwrap();
    let video_convert = gst::ElementFactory::make("videoconvert").build().unwrap();
    let video_caps = gst::ElementFactory::make("capsfilter")
        .property(
            "caps",
            gst::Caps::builder("video/x-raw")
                .field("format", "I420")
                .field("width", 320_i32)
                .field("height", 180_i32)
                .field("framerate", gst::Fraction::new(30, 1))
                .build(),
        )
        .build()
        .unwrap();
    let video_encoder = gst::ElementFactory::make("openh264enc").build().unwrap();
    let video_parser = gst::ElementFactory::make("h264parse").build().unwrap();
    let audio = gst::ElementFactory::make("audiotestsrc")
        .property("num-buffers", 282_i32)
        .build()
        .unwrap();
    let audio_convert = gst::ElementFactory::make("audioconvert").build().unwrap();
    let audio_resample = gst::ElementFactory::make("audioresample").build().unwrap();
    let audio_encoder = gst::ElementFactory::make("voaacenc").build().unwrap();
    let audio_parser = gst::ElementFactory::make("aacparse").build().unwrap();
    let mux = gst::ElementFactory::make("mpegtsmux").build().unwrap();
    let sink = gst::ElementFactory::make("filesink")
        .property("location", path.to_string_lossy().as_ref())
        .build()
        .unwrap();
    pipeline
        .add_many([
            &video,
            &video_convert,
            &video_caps,
            &video_encoder,
            &video_parser,
            &audio,
            &audio_convert,
            &audio_resample,
            &audio_encoder,
            &audio_parser,
            &mux,
            &sink,
        ])
        .unwrap();
    gst::Element::link_many([
        &video,
        &video_convert,
        &video_caps,
        &video_encoder,
        &video_parser,
        &mux,
        &sink,
    ])
    .unwrap();
    gst::Element::link_many([
        &audio,
        &audio_convert,
        &audio_resample,
        &audio_encoder,
        &audio_parser,
        &mux,
    ])
    .unwrap();
    pipeline.set_state(gst::State::Playing).unwrap();
    let bus = pipeline.bus().unwrap();
    let mut reached_eos = false;
    for message in bus.iter_timed(gst::ClockTime::from_seconds(10)) {
        match message.view() {
            gst::MessageView::Eos(_) => {
                reached_eos = true;
                break;
            }
            gst::MessageView::Error(error) => panic!("fixture pipeline failed: {}", error.error()),
            _ => {}
        }
    }
    pipeline.set_state(gst::State::Null).unwrap();
    assert!(reached_eos, "fixture generation timed out");
}
use gst::prelude::*;
use gstreamer as gst;
use station_media_assets::ingest_media;
