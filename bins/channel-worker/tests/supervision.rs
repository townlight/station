use std::fs;
use std::net::UdpSocket;
use std::path::{Path, PathBuf};
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use station_media_journal::read_events;
use station_media_protocol::{ChannelCommand, WorkerEvent};
use station_orchestration::{ChannelController, DispatchTransition, run_until};
use station_runtime::WorkerSupervisor;
use station_schedule::{
    AssetReadiness, ChannelConfiguration, DispatchStatus, MediaAsset, ScheduleItem, ScheduleState,
};
use station_storage::StationStore;

const WORKER_ID: &str = "256d5a07-92d3-4718-aec9-05cad42fae7d";
const CHANNEL_ID: &str = "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e";

fn journal_path() -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is before Unix epoch")
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-supervisor-{nonce}.tlj"))
}

#[test]
fn launches_handshakes_commands_and_cleanly_stops_the_real_worker() {
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
    let launched = WorkerSupervisor::launch(
        PathBuf::from(env!("CARGO_BIN_EXE_channel-worker")).as_path(),
        WORKER_ID,
        CHANNEL_ID,
        &journal,
        output.local_addr().unwrap(),
        Duration::from_secs(2),
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
        WorkerEvent::Ready { graph_revision: 0 }
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
    assert!(matches!(heartbeat.event, WorkerEvent::Heartbeat { .. }));

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
            ChannelCommand::TakeAsset {
                asset_id: asset.asset_id.clone(),
            },
            Duration::from_secs(2),
        )
        .unwrap();
    assert_eq!(on_air.sequence, 5);
    assert!(matches!(
        &on_air.event,
        WorkerEvent::OnAirChanged { source_kind, source_id }
            if source_kind == "asset" && source_id == &asset.asset_id
    ));
    let received = output.recv(&mut datagram).unwrap();
    assert_eq!(received % 188, 0);

    let returned = supervisor
        .command(
            "return-1",
            ChannelCommand::ReturnToSchedule,
            Duration::from_secs(2),
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
            },
            Duration::from_secs(2),
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
            Duration::from_secs(2),
        )
        .unwrap();
    assert_eq!(second_returned.sequence, 9);

    let stopped = supervisor
        .shutdown("shutdown-1", Duration::from_secs(1))
        .unwrap();
    assert_eq!(stopped.sequence, 10);
    assert!(matches!(stopped.event, WorkerEvent::ShutdownComplete));

    let restarted = WorkerSupervisor::launch(
        PathBuf::from(env!("CARGO_BIN_EXE_channel-worker")).as_path(),
        WORKER_ID,
        CHANNEL_ID,
        &journal,
        output.local_addr().unwrap(),
        Duration::from_secs(2),
    )
    .unwrap();
    let restart_ready = restarted.ready().clone();
    assert_eq!(restart_ready.sequence, 11);
    let restart_stopped = restarted
        .shutdown("shutdown-2", Duration::from_secs(1))
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
        udp_destination: output.local_addr().unwrap().to_string(),
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
            duration_ms: 6_000,
            readiness: AssetReadiness::Ready,
        })
        .unwrap();
    store
        .put_schedule_item(&ScheduleItem {
            item_id: WORKER_ID.into(),
            channel_id: CHANNEL_ID.into(),
            asset_id: ingested.asset_id.clone(),
            title: "Automated council playback".into(),
            starts_at_unix_ms: starts_at,
            duration_ms: 1_000,
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
        controller.run_once(starts_at + 1_000).unwrap(),
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
            .filter(|event| matches!(event.event, WorkerEvent::OnAirChanged { ref source_kind, .. } if source_kind == "asset"))
            .count(),
        2
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
fn daemon_loop_discovers_an_enabled_channel_and_completes_its_committed_item() {
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
    let channel = ChannelConfiguration {
        channel_id: CHANNEL_ID.into(),
        display_name: "Daemon-owned channel".into(),
        udp_destination: output.local_addr().unwrap().to_string(),
        enabled: true,
    };
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64;
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
    store
        .put_schedule_item(&ScheduleItem {
            item_id: WORKER_ID.into(),
            channel_id: CHANNEL_ID.into(),
            asset_id: ingested.asset_id,
            title: "Daemon dispatch proof".into(),
            starts_at_unix_ms: now + 1_000,
            duration_ms: 1_000,
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

    let stop = Arc::new(AtomicBool::new(false));
    let runner_stop = Arc::clone(&stop);
    let runner_database = database.clone();
    let worker = PathBuf::from(env!("CARGO_BIN_EXE_channel-worker"));
    let runner = thread::spawn(move || run_until(&runner_database, &worker, runner_stop));
    let deadline = std::time::Instant::now() + Duration::from_secs(12);
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
    let events = read_events(
        root.join("channel-journals")
            .join(format!("{CHANNEL_ID}.tlj")),
    )
    .unwrap();
    assert!(events.iter().any(
        |event| matches!(event.event, WorkerEvent::OnAirChanged { ref source_kind, .. } if source_kind == "asset")
    ));
    assert!(events.iter().any(
        |event| matches!(event.event, WorkerEvent::OnAirChanged { ref source_kind, .. } if source_kind == "fallback")
    ));
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
