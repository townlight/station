use std::net::UdpSocket;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use gst::prelude::*;
use gstreamer as gst;
use station_media_assets::ingest_media;
use station_orchestration::{CandidateAuthority, ChannelController, DispatchTransition};
use station_readiness::CandidateIdentity;
use station_schedule::{
    AssetReadiness, ChannelConfiguration, MediaAsset, OutputProfileId, OutputSinkConfiguration,
    ScheduleItem, ScheduleState,
};
use station_storage::{OutputLoudnessProofJob, OutputLoudnessProofJobState, StationStore};

const CHANNEL_ID: &str = "11111111-2222-4333-8444-555555555555";
const SINK_ID: &str = "22222222-3333-4444-8555-666666666666";
const JOB_ID: &str = "33333333-4444-4555-8666-777777777777";
const ITEM_ID: &str = "44444444-5555-4666-8777-888888888888";
const SOURCE_COMMIT: &str = "0123456789abcdef0123456789abcdef01234567";

struct TestCandidateAuthority;

impl CandidateAuthority for TestCandidateAuthority {
    fn current_candidate(&self) -> Result<CandidateIdentity, String> {
        Ok(CandidateIdentity {
            version: "0.1.0".into(),
            source_commit: Some(SOURCE_COMMIT.into()),
        })
    }
}

struct TempRoot(PathBuf);

impl Drop for TempRoot {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

#[test]
fn controller_executes_a_durable_proof_job_through_atomic_completion() {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is before Unix epoch")
        .as_nanos();
    let root = TempRoot(std::env::temp_dir().join(format!(
        "townlight-output-proof-executor-{}-{nonce}",
        std::process::id()
    )));
    std::fs::create_dir_all(root.0.join("channel-journals")).unwrap();
    let database = root.0.join("station.db");
    let journal = root.0.join("channel-journals").join("worker.tlj");
    let source = root.0.join("scheduled.ts");
    generate_fixture(&source);
    let ingested = ingest_media(&source, root.0.join("library")).unwrap();
    let receiver = UdpSocket::bind("127.0.0.1:0").unwrap();
    let channel = ChannelConfiguration {
        channel_id: CHANNEL_ID.into(),
        display_name: "Proof executor".into(),
        output_profile: OutputProfileId::Proof360p30,
        output_sinks: vec![OutputSinkConfiguration::required_udp(
            SINK_ID,
            "Cable proof",
            receiver.local_addr().unwrap().to_string(),
            -24.0,
        )],
        enabled: true,
    };
    let store = StationStore::open(&database).unwrap();
    store.put_channel(&channel).unwrap();
    let created_at = unix_millis();
    let starts_at = created_at + 1_000;
    store
        .put_media_asset(&MediaAsset {
            asset_id: ingested.asset_id.clone(),
            media_path: ingested.stored_path.to_string_lossy().into_owned(),
            duration_ms: 20_000,
            readiness: AssetReadiness::Ready,
        })
        .unwrap();
    store
        .put_schedule_item(&ScheduleItem {
            item_id: ITEM_ID.into(),
            channel_id: CHANNEL_ID.into(),
            asset_id: ingested.asset_id,
            title: "Audible proof program".into(),
            starts_at_unix_ms: starts_at,
            duration_ms: 20_000,
            state: ScheduleState::Draft,
        })
        .unwrap();
    store
        .commit_schedule(
            "proof-dispatch-report",
            "proof-dispatch-plan",
            ITEM_ID,
            "proof-test",
            created_at,
            "Exercise active output proof against audible scheduled program.",
        )
        .unwrap();
    let configuration_hash = store
        .current_channel_output_configuration_hash(CHANNEL_ID)
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
    controller.set_candidate_authority(Arc::new(TestCandidateAuthority));
    assert!(matches!(
        controller.run_once(created_at).unwrap(),
        Some(DispatchTransition::Loaded { .. })
    ));
    assert!(matches!(
        controller.run_once(starts_at).unwrap(),
        Some(DispatchTransition::OnAir { .. })
    ));
    let store = StationStore::open(&database).unwrap();
    assert!(
        store
            .create_output_loudness_job(&OutputLoudnessProofJob {
                job_id: JOB_ID.into(),
                channel_id: CHANNEL_ID.into(),
                target_group: "cable".into(),
                sink_id: SINK_ID.into(),
                target_lufs_milli: -24_000,
                candidate_version: "0.1.0".into(),
                source_commit: Some(SOURCE_COMMIT.into()),
                configuration_hash,
                sample_duration_ms: 10_000,
                max_capture_bytes: 16 * 1024 * 1024,
                created_at_unix_ms: starts_at + 1,
            })
            .unwrap()
    );
    drop(store);

    let deadline = Instant::now() + Duration::from_secs(45);
    loop {
        assert!(
            Instant::now() < deadline,
            "durable proof executor timed out"
        );
        controller.run_once(unix_millis()).unwrap();
        let store = StationStore::open(&database).unwrap();
        let job = store.read_output_loudness_job(JOB_ID).unwrap().unwrap();
        match job.state {
            OutputLoudnessProofJobState::Completed => {
                let proof = store
                    .read_latest_sink_loudness_proof(CHANNEL_ID, SINK_ID)
                    .unwrap()
                    .expect("atomic completion must publish an immutable proof");
                assert_eq!(proof.proof.proof_id, JOB_ID);
                assert_eq!(proof.proof.configuration_hash, job.configuration_hash);
                assert_eq!(
                    proof.artifact_relative_path.as_deref(),
                    job.capture
                        .as_ref()
                        .map(|capture| capture.artifact_relative_path.as_str())
                );
                break;
            }
            OutputLoudnessProofJobState::Failed => {
                panic!("durable proof executor failed: {:?}", job.failure_detail)
            }
            _ => std::thread::sleep(Duration::from_millis(50)),
        }
    }

    controller.shutdown(Duration::from_secs(3)).unwrap();
}

fn unix_millis() -> i64 {
    i64::try_from(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock is before Unix epoch")
            .as_millis(),
    )
    .unwrap()
}

fn generate_fixture(path: &std::path::Path) {
    gst::init().unwrap();
    let pipeline = gst::Pipeline::new();
    let video = gst::ElementFactory::make("videotestsrc")
        .property("num-buffers", 600_i32)
        .property_from_str("pattern", "ball")
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
        .property("num-buffers", 940_i32)
        .property_from_str("wave", "sine")
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
    for message in bus.iter_timed(gst::ClockTime::from_seconds(30)) {
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
