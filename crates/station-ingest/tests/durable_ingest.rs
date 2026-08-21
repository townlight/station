use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use gst::prelude::*;
use gstreamer as gst;
use station_ingest::{IngestOutcome, IngestWorker, pending_job};
use station_media_lifecycle::IngestJobStatus;
use station_schedule::AssetReadiness;
use station_storage::StationStore;

#[test]
fn completes_real_media_fails_invalid_media_and_recovers_an_interrupted_job() {
    let root = test_root();
    let database = root.join("station.db");
    let library = root.join("media");
    let valid = root.join("valid.ts");
    let invalid = root.join("invalid.txt");
    fs::create_dir_all(&root).unwrap();
    generate_fixture(&valid);
    fs::write(&invalid, b"not media").unwrap();
    let store = StationStore::open(&database).unwrap();
    store
        .create_ingest_job(&pending_job(
            "11111111-2222-4333-8444-555555555555".into(),
            valid.to_string_lossy().into_owned(),
            1,
        ))
        .unwrap();
    store
        .create_ingest_job(&pending_job(
            "22222222-3333-4444-8555-666666666666".into(),
            invalid.to_string_lossy().into_owned(),
            2,
        ))
        .unwrap();
    drop(store);

    let worker = IngestWorker::new(&database, &library);
    let completed = worker.run_once().unwrap().unwrap();
    let asset_id = match completed {
        IngestOutcome::Completed { job_id, asset_id } => {
            assert_eq!(job_id, "11111111-2222-4333-8444-555555555555");
            asset_id
        }
        other => panic!("valid media did not complete: {other:?}"),
    };
    let store = StationStore::open(&database).unwrap();
    let completed_job = store
        .read_ingest_job("11111111-2222-4333-8444-555555555555")
        .unwrap()
        .unwrap();
    assert_eq!(completed_job.status, IngestJobStatus::Completed);
    assert_eq!(completed_job.progress_percent, 100);
    assert_eq!(completed_job.asset_id.as_deref(), Some(asset_id.as_str()));
    assert_eq!(completed_job.attempts, 1);
    assert_eq!(
        store
            .read_media_asset(&asset_id)
            .unwrap()
            .unwrap()
            .readiness,
        AssetReadiness::Ready
    );
    let stored_objects = fs::read_dir(&library).unwrap().count();
    drop(store);

    let failed = worker.run_once().unwrap().unwrap();
    assert!(matches!(
        failed,
        IngestOutcome::Failed { ref job_id, .. }
            if job_id == "22222222-3333-4444-8555-666666666666"
    ));
    let store = StationStore::open(&database).unwrap();
    let failed_job = store
        .read_ingest_job("22222222-3333-4444-8555-666666666666")
        .unwrap()
        .unwrap();
    assert_eq!(failed_job.status, IngestJobStatus::Failed);
    assert_eq!(failed_job.progress_percent, 10);
    assert!(
        failed_job
            .error_detail
            .unwrap()
            .to_ascii_lowercase()
            .contains("media")
    );
    assert!(failed_job.asset_id.is_none());
    assert_eq!(fs::read_dir(&library).unwrap().count(), stored_objects);

    store
        .create_ingest_job(&pending_job(
            "33333333-4444-4555-8666-777777777777".into(),
            valid.to_string_lossy().into_owned(),
            3,
        ))
        .unwrap();
    let claimed = store.claim_next_ingest_job(4).unwrap().unwrap();
    assert_eq!(claimed.status, IngestJobStatus::Running);
    assert_eq!(claimed.attempts, 1);
    drop(store);
    assert_eq!(worker.recover_interrupted().unwrap(), 1);
    assert!(matches!(
        worker.run_once().unwrap(),
        Some(IngestOutcome::Completed { ref job_id, .. })
            if job_id == "33333333-4444-4555-8666-777777777777"
    ));
    let recovered = StationStore::open(&database)
        .unwrap()
        .read_ingest_job("33333333-4444-4555-8666-777777777777")
        .unwrap()
        .unwrap();
    assert_eq!(recovered.status, IngestJobStatus::Completed);
    assert_eq!(recovered.attempts, 2);
    assert_eq!(worker.run_once().unwrap(), None);

    remove_directory(&root);
}

fn generate_fixture(path: &Path) {
    gst::init().unwrap();
    let pipeline = gst::Pipeline::new();
    let video = gst::ElementFactory::make("videotestsrc")
        .property("num-buffers", 90_i32)
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
        .property("num-buffers", 141_i32)
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
    for message in bus.iter_timed(gst::ClockTime::from_seconds(10)) {
        match message.view() {
            gst::MessageView::Eos(_) => break,
            gst::MessageView::Error(error) => panic!("fixture pipeline failed: {}", error.error()),
            _ => {}
        }
    }
    pipeline.set_state(gst::State::Null).unwrap();
    assert!(path.is_file());
}

fn test_root() -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-durable-ingest-{nonce}"))
}

fn remove_directory(path: &Path) {
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
    loop {
        match fs::remove_dir_all(path) {
            Ok(()) => return,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return,
            Err(_) if std::time::Instant::now() < deadline => {
                std::thread::sleep(std::time::Duration::from_millis(25));
            }
            Err(error) => panic!("could not remove {}: {error}", path.display()),
        }
    }
}
