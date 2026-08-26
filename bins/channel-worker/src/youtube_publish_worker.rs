//! The one-shot YouTube publisher is deliberately part of the installed media
//! worker rather than a shell wrapper. Its command and result are the sole
//! process boundary: no stream key ever travels in an argument, environment,
//! log, or diagnostic string.

use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use gstreamer as gst;
use gstreamer::prelude::*;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use zeroize::{Zeroize, Zeroizing};

const COMMAND_SCHEMA_VERSION: u8 = 1;
const MAX_COMMAND_BYTES: u64 = 16 * 1024;
const MAX_RESULT_BYTES: usize = 4 * 1024;
const MAX_STREAM_KEY_BYTES: usize = 256;
const APP_SOURCE_CHUNK_BYTES: usize = 64 * 1024;
const MAX_APP_SOURCE_QUEUED_BYTES: u64 = 1024 * 1024;
const PIPELINE_STALL_TIMEOUT: Duration = Duration::from_secs(30);
const MAX_EXPECTED_DURATION_MS: u64 = 12 * 60 * 60 * 1_000;
const MAX_PIPELINE_RUNTIME: Duration = Duration::from_secs(24 * 60 * 60);
const PIPELINE_RUNTIME_GRACE: Duration = Duration::from_secs(10 * 60);
const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
const FILE_SHARE_READ: u32 = 0x0000_0001;
const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x0000_0400;
const YOUTUBE_RTMPS_INGESTION_ADDRESS: &str = "rtmps://a.rtmp.youtube.com:443/live2";

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct YoutubePublishCommand {
    schema_version: u8,
    managed_media_root: String,
    media_path: String,
    expected_bytes: u64,
    expected_sha256: String,
    expected_duration_ms: u64,
    rtmps_ingestion_address: String,
    stream_key: String,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
struct YoutubePublishResult {
    schema_version: u8,
    outcome: YoutubePublishOutcome,
    media_bytes: u64,
    media_sha256: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    failure_code: Option<&'static str>,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum YoutubePublishOutcome {
    Published,
    Failed,
}

trait YoutubePublishPipeline {
    /// Streams only from the already-opened file handle. `Ok` means a healthy
    /// finite pipeline delivered EOS; it does *not* mean merely that a sink was
    /// constructed or that a connection attempt began.
    fn stream_to_eos(
        &mut self,
        media: &mut File,
        expected_media: &MediaIdentity,
        maximum_runtime: Duration,
        rtmps_location: &mut Zeroizing<String>,
    ) -> Result<(), &'static str>;
}

#[derive(PartialEq, Eq)]
struct MediaIdentity {
    bytes: u64,
    sha256: String,
}

struct GstreamerYoutubePublishPipeline;

impl YoutubePublishPipeline for GstreamerYoutubePublishPipeline {
    fn stream_to_eos(
        &mut self,
        media: &mut File,
        expected_media: &MediaIdentity,
        maximum_runtime: Duration,
        rtmps_location: &mut Zeroizing<String>,
    ) -> Result<(), &'static str> {
        stream_to_youtube_with_gstreamer(
            media,
            expected_media,
            maximum_runtime,
            rtmps_location.as_str(),
        )
    }
}

/// Runs the hidden one-shot mode. It always writes one secret-free JSON result
/// to stdout. Its caller may use the exit status as a transport signal, but
/// must use the result payload for the durable receipt decision.
pub(crate) fn run_from_stdio() -> i32 {
    let mut input = Zeroizing::new(Vec::new());
    let parsed = std::io::stdin()
        .lock()
        .take(MAX_COMMAND_BYTES + 1)
        .read_to_end(&mut input)
        .map_err(|_| "command_read_failed")
        .and_then(|_| decode_command(&input));
    input.zeroize();

    let mut pipeline = GstreamerYoutubePublishPipeline;
    let result = match parsed {
        Ok(command) => execute(command, &mut pipeline),
        Err(code) => failed_result(code),
    };
    let outcome = result.outcome == YoutubePublishOutcome::Published;
    let rendered = serde_json::to_vec(&result).unwrap_or_else(|_| {
        b"{\"schema_version\":1,\"outcome\":\"failed\",\"media_bytes\":0,\"media_sha256\":\"\",\"failure_code\":\"result_serialization_failed\"}".to_vec()
    });
    if rendered.len() > MAX_RESULT_BYTES {
        let _ = std::io::stdout().write_all(
            b"{\"schema_version\":1,\"outcome\":\"failed\",\"media_bytes\":0,\"media_sha256\":\"\",\"failure_code\":\"result_too_large\"}\n",
        );
        return 1;
    }
    let mut stdout = std::io::stdout().lock();
    if stdout.write_all(&rendered).is_err() || stdout.write_all(b"\n").is_err() {
        return 1;
    }
    if stdout.flush().is_err() {
        return 1;
    }
    if outcome { 0 } else { 1 }
}

fn decode_command(input: &[u8]) -> Result<YoutubePublishCommand, &'static str> {
    if input.is_empty() || input.len() as u64 > MAX_COMMAND_BYTES {
        return Err("invalid_command");
    }
    let mut deserializer = serde_json::Deserializer::from_slice(input);
    let command =
        YoutubePublishCommand::deserialize(&mut deserializer).map_err(|_| "invalid_command")?;
    deserializer.end().map_err(|_| "invalid_command")?;
    Ok(command)
}

fn execute(
    command: YoutubePublishCommand,
    pipeline: &mut impl YoutubePublishPipeline,
) -> YoutubePublishResult {
    if command.schema_version != COMMAND_SCHEMA_VERSION {
        return failed_result("unsupported_schema_version");
    }
    if !is_lower_sha256(&command.expected_sha256) {
        return failed_result("invalid_expected_sha256");
    }
    if command.expected_duration_ms == 0 || command.expected_duration_ms > MAX_EXPECTED_DURATION_MS
    {
        return failed_result("invalid_expected_duration");
    }
    if command.rtmps_ingestion_address != YOUTUBE_RTMPS_INGESTION_ADDRESS {
        return failed_result("invalid_rtmps_ingestion_address");
    }
    let stream_key = Zeroizing::new(command.stream_key);
    if !is_valid_stream_key(stream_key.as_str()) {
        return failed_result("invalid_stream_key");
    }
    let (mut media, _directory_pins) = match open_input_media(
        &command.managed_media_root,
        &command.media_path,
    ) {
        Ok(opened) => opened,
        Err(code) => return failed_result(code),
    };
    let actual_media = match media_identity(&mut media) {
        Ok(identity) => identity,
        Err(code) => return failed_result(code),
    };
    if command.expected_bytes != actual_media.bytes
        || command.expected_sha256 != actual_media.sha256
    {
        return failed_result_with_media(
            "media_identity_mismatch",
            actual_media.bytes,
            actual_media.sha256,
        );
    }

    // Both owned strings zero their bytes at scope exit. GStreamer receives a
    // transient property copy and the pipeline is forced to Null before this
    // function returns, so the plugin has no retained live location afterward.
    let mut rtmps_location = Zeroizing::new(format!(
        "{}/{}",
        command.rtmps_ingestion_address,
        stream_key.as_str()
    ));
    let stream_result = pipeline.stream_to_eos(
        &mut media,
        &actual_media,
        pipeline_eos_timeout(Duration::from_millis(command.expected_duration_ms)),
        &mut rtmps_location,
    );
    rtmps_location.zeroize();
    match stream_result {
        Ok(()) => published_result(actual_media.bytes, actual_media.sha256),
        Err(code) => failed_result_with_media(code, actual_media.bytes, actual_media.sha256),
    }
}

fn open_input_media(
    managed_root: &str,
    path: &str,
) -> Result<(File, Vec<File>), &'static str> {
    let managed_root = std::path::Path::new(managed_root);
    let path = std::path::Path::new(path);
    if !managed_root.is_absolute() || !path.is_absolute() {
        return Err("invalid_media_path");
    }
    let relative = path
        .strip_prefix(managed_root)
        .map_err(|_| "media_path_escape")?;
    if relative.as_os_str().is_empty()
        || relative.components().any(|component| {
            !matches!(component, std::path::Component::Normal(_))
        })
    {
        return Err("media_path_escape");
    }
    let mut pins = Vec::new();
    let mut directory = managed_root.to_path_buf();
    pins.push(open_directory_no_follow(&directory)?);
    if let Some(parent) = relative.parent() {
        for component in parent.components() {
            directory.push(component.as_os_str());
            pins.push(open_directory_no_follow(&directory)?);
        }
    }
    let metadata = std::fs::symlink_metadata(path).map_err(|_| "media_open_failed")?;
    if !metadata.is_file() || metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err("invalid_media_path");
    }
    let media = OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path)
        .map_err(|_| "media_open_failed")?;
    let metadata = media.metadata().map_err(|_| "media_open_failed")?;
    if !metadata.is_file() || metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err("invalid_media_path");
    }
    Ok((media, pins))
}

fn open_directory_no_follow(path: &std::path::Path) -> Result<File, &'static str> {
    const FILE_FLAG_BACKUP_SEMANTICS: u32 = 0x0200_0000;
    let directory = OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ)
        .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path)
        .map_err(|_| "managed_media_root_unavailable")?;
    let metadata = directory
        .metadata()
        .map_err(|_| "managed_media_root_unavailable")?;
    if !metadata.is_dir() || metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err("managed_media_root_unavailable");
    }
    Ok(directory)
}

fn media_identity(media: &mut File) -> Result<MediaIdentity, &'static str> {
    let expected_length = media.metadata().map_err(|_| "media_read_failed")?.len();
    media
        .seek(SeekFrom::Start(0))
        .map_err(|_| "media_read_failed")?;
    let mut hash = Sha256::new();
    let mut bytes_read = 0_u64;
    let mut buffer = [0_u8; APP_SOURCE_CHUNK_BYTES];
    loop {
        let read = media.read(&mut buffer).map_err(|_| "media_read_failed")?;
        if read == 0 {
            break;
        }
        bytes_read = bytes_read
            .checked_add(read as u64)
            .ok_or("media_read_failed")?;
        hash.update(&buffer[..read]);
    }
    if bytes_read != expected_length {
        return Err("media_changed_during_read");
    }
    media
        .seek(SeekFrom::Start(0))
        .map_err(|_| "media_read_failed")?;
    Ok(MediaIdentity {
        bytes: bytes_read,
        sha256: format!("{:x}", hash.finalize()),
    })
}

fn is_lower_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
}

fn is_valid_stream_key(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= MAX_STREAM_KEY_BYTES
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
}

fn pipeline_eos_timeout(expected_duration: Duration) -> Duration {
    expected_duration
        .saturating_mul(2)
        .saturating_add(PIPELINE_RUNTIME_GRACE)
        .min(MAX_PIPELINE_RUNTIME)
}

fn stream_to_youtube_with_gstreamer(
    media: &mut File,
    expected_media: &MediaIdentity,
    maximum_runtime: Duration,
    rtmps_location: &str,
) -> Result<(), &'static str> {
    gst::init().map_err(|_| "gstreamer_init_failed")?;
    let pipeline = gst::Pipeline::new();
    let app_source = gst::ElementFactory::make("appsrc")
        .name("youtube-media-source")
        .property("format", gst::Format::Bytes)
        .property("is-live", false)
        .property("block", false)
        .property("max-bytes", MAX_APP_SOURCE_QUEUED_BYTES)
        .build()
        .map_err(|_| "gstreamer_appsrc_unavailable")?;
    let typefind = make_element("typefind", "gstreamer_typefind_unavailable")?;
    let decoder = make_element("decodebin", "gstreamer_decodebin_unavailable")?;
    let video_queue = make_element("queue", "gstreamer_queue_unavailable")?;
    let video_convert = make_element("videoconvert", "gstreamer_videoconvert_unavailable")?;
    let video_rate = make_element("videorate", "gstreamer_videorate_unavailable")?;
    let video_caps = gst::ElementFactory::make("capsfilter")
        .property(
            "caps",
            gst::Caps::builder("video/x-raw")
                .field("format", "I420")
                .build(),
        )
        .build()
        .map_err(|_| "gstreamer_video_caps_unavailable")?;
    let video_encoder = gst::ElementFactory::make("openh264enc")
        .property("bitrate", 3_000_000_i32)
        .property("max-bitrate", 3_000_000_i32)
        .property("enable-frame-skip", false)
        .property_from_str("rate-control", "bitrate")
        .build()
        .map_err(|_| "gstreamer_openh264enc_unavailable")?;
    let video_parser = gst::ElementFactory::make("h264parse")
        .property("config-interval", 1_i32)
        .build()
        .map_err(|_| "gstreamer_h264parse_unavailable")?;
    let audio_queue = make_element("queue", "gstreamer_queue_unavailable")?;
    let audio_convert = make_element("audioconvert", "gstreamer_audioconvert_unavailable")?;
    let audio_resample = make_element("audioresample", "gstreamer_audioresample_unavailable")?;
    let audio_caps = gst::ElementFactory::make("capsfilter")
        .property(
            "caps",
            gst::Caps::builder("audio/x-raw")
                .field("format", "S16LE")
                .field("rate", 48_000_i32)
                .field("channels", 2_i32)
                .build(),
        )
        .build()
        .map_err(|_| "gstreamer_audio_caps_unavailable")?;
    let audio_encoder = gst::ElementFactory::make("voaacenc")
        .property("bitrate", 192_000_i32)
        .build()
        .map_err(|_| "gstreamer_voaacenc_unavailable")?;
    let audio_parser = make_element("aacparse", "gstreamer_aacparse_unavailable")?;
    let mux = gst::ElementFactory::make("flvmux")
        .property("streamable", true)
        .build()
        .map_err(|_| "gstreamer_flvmux_unavailable")?;
    let sink = gst::ElementFactory::make("rtmp2sink")
        .property("location", rtmps_location)
        .build()
        .map_err(|_| "gstreamer_rtmp2sink_unavailable")?;
    pipeline
        .add_many([
            &app_source,
            &typefind,
            &decoder,
            &video_queue,
            &video_convert,
            &video_rate,
            &video_caps,
            &video_encoder,
            &video_parser,
            &audio_queue,
            &audio_convert,
            &audio_resample,
            &audio_caps,
            &audio_encoder,
            &audio_parser,
            &mux,
            &sink,
        ])
        .map_err(|_| "gstreamer_pipeline_build_failed")?;
    app_source
        .link(&typefind)
        .and_then(|_| typefind.link(&decoder))
        .map_err(|_| "gstreamer_pipeline_link_failed")?;
    gst::Element::link_many([
        &video_queue,
        &video_convert,
        &video_rate,
        &video_caps,
        &video_encoder,
        &video_parser,
        &mux,
    ])
    .map_err(|_| "gstreamer_pipeline_link_failed")?;
    gst::Element::link_many([
        &audio_queue,
        &audio_convert,
        &audio_resample,
        &audio_caps,
        &audio_encoder,
        &audio_parser,
        &mux,
    ])
    .map_err(|_| "gstreamer_pipeline_link_failed")?;
    mux.link(&sink)
        .map_err(|_| "gstreamer_pipeline_link_failed")?;

    let video_pad = video_queue
        .static_pad("sink")
        .ok_or("gstreamer_pipeline_link_failed")?;
    let audio_pad = audio_queue
        .static_pad("sink")
        .ok_or("gstreamer_pipeline_link_failed")?;
    let video_linked = Arc::new(AtomicBool::new(false));
    let audio_linked = Arc::new(AtomicBool::new(false));
    let video_signal = Arc::clone(&video_linked);
    let audio_signal = Arc::clone(&audio_linked);
    decoder.connect_pad_added(move |_, source_pad| {
        let caps = source_pad
            .current_caps()
            .unwrap_or_else(|| source_pad.query_caps(None));
        let Some(media_type) = caps.structure(0).map(|structure| structure.name()) else {
            return;
        };
        if media_type.starts_with("video/") && !video_pad.is_linked() {
            if source_pad.link(&video_pad).is_ok() {
                video_signal.store(true, Ordering::Release);
            }
        } else if media_type.starts_with("audio/")
            && !audio_pad.is_linked()
            && source_pad.link(&audio_pad).is_ok()
        {
            audio_signal.store(true, Ordering::Release);
        }
    });

    let result = (|| {
        pipeline
            .set_state(gst::State::Playing)
            .map_err(|_| "gstreamer_start_failed")?;
        let bus = pipeline.bus().ok_or("gstreamer_bus_unavailable")?;
        let streamed_media = feed_media_to_appsrc(media, &app_source, &bus)?;
        if &streamed_media != expected_media {
            return Err("media_changed_during_stream");
        }
        app_source
            .emit_by_name::<gst::FlowReturn>("end-of-stream", &[])
            .into_result()
            .map_err(|_| "pipeline_end_of_stream_failed")?;
        wait_for_eos(&bus, &video_linked, &audio_linked, maximum_runtime)
    })();
    let stopped = stop_pipeline(&pipeline);
    result.and(stopped)
}

fn make_element(
    factory: &'static str,
    failure_code: &'static str,
) -> Result<gst::Element, &'static str> {
    gst::ElementFactory::make(factory)
        .build()
        .map_err(|_| failure_code)
}

fn feed_media_to_appsrc(
    media: &mut File,
    app_source: &gst::Element,
    bus: &gst::Bus,
) -> Result<MediaIdentity, &'static str> {
    let mut buffer = [0_u8; APP_SOURCE_CHUNK_BYTES];
    let mut stalled_since = Instant::now();
    let mut hash = Sha256::new();
    let mut bytes = 0_u64;
    loop {
        while app_source.property::<u64>("current-level-bytes") >= MAX_APP_SOURCE_QUEUED_BYTES {
            ensure_no_pipeline_error(bus)?;
            if stalled_since.elapsed() >= PIPELINE_STALL_TIMEOUT {
                return Err("pipeline_stalled");
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        let read = media.read(&mut buffer).map_err(|_| "media_read_failed")?;
        if read == 0 {
            break;
        }
        let gst_buffer = gst::Buffer::from_mut_slice(buffer[..read].to_vec());
        app_source
            .emit_by_name::<gst::FlowReturn>("push-buffer", &[&gst_buffer])
            .into_result()
            .map_err(|_| "pipeline_push_failed")?;
        bytes = bytes.checked_add(read as u64).ok_or("media_read_failed")?;
        hash.update(&buffer[..read]);
        stalled_since = Instant::now();
        ensure_no_pipeline_error(bus)?;
    }
    Ok(MediaIdentity {
        bytes,
        sha256: format!("{:x}", hash.finalize()),
    })
}

fn ensure_no_pipeline_error(bus: &gst::Bus) -> Result<(), &'static str> {
    if let Some(message) = bus.timed_pop_filtered(gst::ClockTime::ZERO, &[gst::MessageType::Error])
        && matches!(message.view(), gst::MessageView::Error(_))
    {
        return Err("pipeline_error");
    }
    Ok(())
}

fn wait_for_eos(
    bus: &gst::Bus,
    video_linked: &AtomicBool,
    audio_linked: &AtomicBool,
    maximum_runtime: Duration,
) -> Result<(), &'static str> {
    let deadline = Instant::now() + maximum_runtime;
    while Instant::now() < deadline {
        let remaining = deadline.saturating_duration_since(Instant::now());
        let Some(message) = bus.timed_pop(gst::ClockTime::from_nseconds(
            remaining.as_nanos().min(u128::from(u64::MAX)) as u64,
        )) else {
            break;
        };
        match message.view() {
            gst::MessageView::Eos(_)
                if video_linked.load(Ordering::Acquire) && audio_linked.load(Ordering::Acquire) =>
            {
                return Ok(());
            }
            gst::MessageView::Eos(_) => return Err("pipeline_missing_audio_or_video"),
            gst::MessageView::Error(_) => return Err("pipeline_error"),
            _ => {}
        }
    }
    Err("pipeline_eos_timeout")
}

fn stop_pipeline(pipeline: &gst::Pipeline) -> Result<(), &'static str> {
    pipeline
        .set_state(gst::State::Null)
        .map_err(|_| "gstreamer_stop_failed")?;
    pipeline
        .state(gst::ClockTime::from_seconds(5))
        .0
        .map(|_| ())
        .map_err(|_| "gstreamer_stop_failed")
}

fn published_result(media_bytes: u64, media_sha256: String) -> YoutubePublishResult {
    YoutubePublishResult {
        schema_version: COMMAND_SCHEMA_VERSION,
        outcome: YoutubePublishOutcome::Published,
        media_bytes,
        media_sha256,
        failure_code: None,
    }
}

fn failed_result(code: &'static str) -> YoutubePublishResult {
    failed_result_with_media(code, 0, String::new())
}

fn failed_result_with_media(
    code: &'static str,
    media_bytes: u64,
    media_sha256: String,
) -> YoutubePublishResult {
    YoutubePublishResult {
        schema_version: COMMAND_SCHEMA_VERSION,
        outcome: YoutubePublishOutcome::Failed,
        media_bytes,
        media_sha256,
        failure_code: Some(code),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::path::PathBuf;
    use std::time::{SystemTime, UNIX_EPOCH};

    const TEST_SHA256: &str = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08";

    struct FakePipeline {
        outcome: Result<(), &'static str>,
        called: bool,
        observed_media: Vec<u8>,
    }

    impl YoutubePublishPipeline for FakePipeline {
        fn stream_to_eos(
            &mut self,
            media: &mut File,
            _expected_media: &MediaIdentity,
            _maximum_runtime: Duration,
            _rtmps_location: &mut Zeroizing<String>,
        ) -> Result<(), &'static str> {
            self.called = true;
            media
                .read_to_end(&mut self.observed_media)
                .map_err(|_| "pipeline_test_read_failed")?;
            self.outcome
        }
    }

    fn command(path: PathBuf, sha256: &str) -> YoutubePublishCommand {
        YoutubePublishCommand {
            schema_version: COMMAND_SCHEMA_VERSION,
            managed_media_root: path
                .parent()
                .unwrap_or_else(|| std::path::Path::new("."))
                .to_string_lossy()
                .into_owned(),
            media_path: path.to_string_lossy().into_owned(),
            expected_bytes: 4,
            expected_sha256: sha256.into(),
            expected_duration_ms: 1_000,
            rtmps_ingestion_address: YOUTUBE_RTMPS_INGESTION_ADDRESS.into(),
            stream_key: "synthetic-stream-key-123".into(),
        }
    }

    fn temp_media() -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!("townlight-youtube-worker-{nonce}.ts"));
        let mut file = File::create(&path).unwrap();
        file.write_all(b"test").unwrap();
        path
    }

    fn fake(outcome: Result<(), &'static str>) -> FakePipeline {
        FakePipeline {
            outcome,
            called: false,
            observed_media: Vec::new(),
        }
    }

    #[test]
    fn rejects_wrong_media_hash_without_starting_pipeline() {
        let path = temp_media();
        let mut pipeline = fake(Ok(()));

        let result = execute(command(path.clone(), &"0".repeat(64)), &mut pipeline);

        assert_eq!(result.outcome, YoutubePublishOutcome::Failed);
        assert_eq!(result.failure_code, Some("media_identity_mismatch"));
        assert!(!pipeline.called);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn rejects_relative_media_path_without_starting_pipeline() {
        let mut pipeline = fake(Ok(()));
        let result = execute(
            command(PathBuf::from("relative.ts"), TEST_SHA256),
            &mut pipeline,
        );

        assert_eq!(result.failure_code, Some("invalid_media_path"));
        assert!(!pipeline.called);
    }

    #[test]
    fn rejects_media_outside_the_managed_root_without_starting_pipeline() {
        let path = temp_media();
        let mut value = command(path.clone(), TEST_SHA256);
        value.managed_media_root = path
            .parent()
            .expect("test media has a parent")
            .join("different-managed-root")
            .to_string_lossy()
            .into_owned();
        let mut pipeline = fake(Ok(()));

        let result = execute(value, &mut pipeline);

        assert_eq!(result.failure_code, Some("media_path_escape"));
        assert!(!pipeline.called);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn rejects_an_unsafe_stream_key_without_starting_pipeline() {
        let path = temp_media();
        let mut value = command(path.clone(), TEST_SHA256);
        value.stream_key = "key\\nlocation=attacker".into();
        let mut pipeline = fake(Ok(()));

        let result = execute(value, &mut pipeline);

        assert_eq!(result.failure_code, Some("invalid_stream_key"));
        assert!(!pipeline.called);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn reports_a_pipeline_error_without_leaking_the_stream_key() {
        let path = temp_media();
        let mut pipeline = fake(Err("pipeline_error"));
        let result = execute(command(path.clone(), TEST_SHA256), &mut pipeline);
        let json = serde_json::to_string(&result).unwrap();

        assert_eq!(result.failure_code, Some("pipeline_error"));
        assert!(pipeline.called);
        assert!(!json.contains("synthetic-stream-key-123"));
        assert_eq!(pipeline.observed_media, b"test");
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn publishes_only_after_the_pipeline_reaches_eos() {
        let path = temp_media();
        let mut pipeline = fake(Ok(()));
        let result = execute(command(path.clone(), TEST_SHA256), &mut pipeline);

        assert_eq!(result.outcome, YoutubePublishOutcome::Published);
        assert_eq!(result.media_bytes, 4);
        assert_eq!(result.media_sha256, TEST_SHA256);
        assert_eq!(result.failure_code, None);
        assert!(pipeline.called);
        assert_eq!(pipeline.observed_media, b"test");
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn command_parser_rejects_unknown_fields_and_trailing_json() {
        let unknown = br#"{"schema_version":1,"managed_media_root":"C:\\media","media_path":"C:\\media\\x.ts","expected_bytes":4,"expected_sha256":"9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08","expected_duration_ms":1000,"rtmps_ingestion_address":"rtmps://a.rtmp.youtube.com:443/live2","stream_key":"synthetic-stream-key-123","unexpected":true}"#;
        assert!(matches!(decode_command(unknown), Err("invalid_command")));

        let trailing = br#"{"schema_version":1,"managed_media_root":"C:\\media","media_path":"C:\\media\\x.ts","expected_bytes":4,"expected_sha256":"9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08","expected_duration_ms":1000,"rtmps_ingestion_address":"rtmps://a.rtmp.youtube.com:443/live2","stream_key":"synthetic-stream-key-123"} {}"#;
        assert!(matches!(decode_command(trailing), Err("invalid_command")));
    }

    #[test]
    fn command_parser_rejects_input_above_the_protocol_bound() {
        let oversized = vec![b' '; MAX_COMMAND_BYTES as usize + 1];
        assert!(matches!(decode_command(&oversized), Err("invalid_command")));
    }

    #[test]
    fn finite_media_timeout_derives_from_its_declared_duration() {
        assert_eq!(
            pipeline_eos_timeout(Duration::from_secs(60)),
            Duration::from_secs(12 * 60),
        );
    }

    #[test]
    fn rejects_an_unbounded_declared_duration_before_opening_media() {
        let path = temp_media();
        let mut value = command(path.clone(), TEST_SHA256);
        value.expected_duration_ms = MAX_EXPECTED_DURATION_MS + 1;
        let mut pipeline = fake(Ok(()));

        let result = execute(value, &mut pipeline);

        assert_eq!(result.failure_code, Some("invalid_expected_duration"));
        assert!(!pipeline.called);
        let _ = std::fs::remove_file(path);
    }
}
