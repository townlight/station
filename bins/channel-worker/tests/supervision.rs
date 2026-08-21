use std::fs;
use std::net::UdpSocket;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use station_media_journal::read_events;
use station_media_protocol::{ChannelCommand, WorkerEvent};
use station_runtime::WorkerSupervisor;

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
    fs::remove_dir_all(media_root).unwrap();
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
