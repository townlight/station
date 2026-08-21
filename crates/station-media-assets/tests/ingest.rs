use std::fs;
use std::path::Path;

use gst::prelude::*;
use gstreamer as gst;
use station_media_assets::{ingest_media, validate_media, verify_media_identity};

#[test]
fn validates_and_atomically_ingests_real_audio_video_media() {
    let root = test_root("valid");
    let source = root.join("source.ts");
    let library = root.join("library");
    fs::create_dir_all(&root).unwrap();
    generate_fixture(&source);

    let metadata = validate_media(&source).expect("the generated A/V fixture must validate");
    assert!(metadata.duration_ns >= 2_000_000_000);
    assert_eq!(metadata.video_streams, 1);
    assert_eq!(metadata.audio_streams, 1);
    assert!(!metadata.video_codec.is_empty());
    assert!(!metadata.audio_codec.is_empty());

    let first = ingest_media(&source, &library).expect("first ingest must succeed");
    let second = ingest_media(&source, &library).expect("duplicate ingest must be idempotent");
    assert_eq!(first, second);
    assert_eq!(first.asset_id.len(), 64);
    assert!(
        first
            .stored_path
            .starts_with(fs::canonicalize(&library).unwrap())
    );
    assert_eq!(
        fs::read(&source).unwrap(),
        fs::read(&first.stored_path).unwrap()
    );
    assert_eq!(
        verify_media_identity(&first.stored_path, &first.asset_id).unwrap(),
        first.metadata
    );
    assert!(verify_media_identity(&first.stored_path, &"0".repeat(64)).is_err());
    assert_eq!(
        fs::read_dir(&library).unwrap().count(),
        1,
        "a duplicate import must not create a second stored object"
    );

    fs::remove_dir_all(root).unwrap();
}

#[test]
fn rejects_non_media_without_polluting_the_library() {
    let root = test_root("invalid");
    let source = root.join("not-media.txt");
    let library = root.join("library");
    fs::create_dir_all(&root).unwrap();
    fs::write(&source, b"this is not media").unwrap();

    assert!(validate_media(&source).is_err());
    assert!(ingest_media(&source, &library).is_err());
    assert!(!library.exists() || fs::read_dir(&library).unwrap().next().is_none());

    fs::remove_dir_all(root).unwrap();
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
    assert!(path.exists());
}

fn test_root(label: &str) -> std::path::PathBuf {
    std::env::temp_dir().join(format!(
        "townlight-media-{label}-{}-{}",
        std::process::id(),
        std::thread::current().name().unwrap_or("test")
    ))
}
