use std::fs::{self, File, OpenOptions};
use std::io::{BufReader, BufWriter, Read, Write};
use std::os::windows::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use gst::prelude::*;
use gst_pbutils::prelude::*;
use gstreamer as gst;
use gstreamer_pbutils as gst_pbutils;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use station_captions::{MAX_CAPTION_PROOF_CAPTURE_BYTES, canonical_caption_text_sha256};

const MAX_DECODED_CAPTION_SRT_BYTES: u64 = 32 * 1024 * 1024;
const MAX_REVIEW_THUMBNAIL_BYTES: u64 = 8 * 1024 * 1024;
const REVIEW_THUMBNAIL_TIMEOUT: Duration = Duration::from_secs(30);

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MediaMetadata {
    pub duration_ns: u64,
    pub video_streams: u32,
    pub audio_streams: u32,
    pub container: Option<String>,
    pub video_codec: String,
    pub audio_codec: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct IngestedAsset {
    pub asset_id: String,
    pub stored_path: PathBuf,
    pub size_bytes: u64,
    pub metadata: MediaMetadata,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ThumbnailArtifact {
    pub source_asset_id: String,
    pub thumbnail_sha256: String,
    pub stored_path: PathBuf,
    pub size_bytes: u64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct LoudnessMeasurement {
    pub integrated_lufs: f64,
    /// Rounded-up running time covered by valid ebur128 analyzer messages.
    pub analyzed_duration_ms: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DecodedCaptionEvidence {
    pub cue_count: u32,
    pub text_sha256: String,
    pub decoder_engine: String,
    pub decoder_method: String,
}

#[derive(Debug)]
pub enum AssetError {
    InvalidSource(String),
    UnsupportedMedia(String),
    Io(String),
    Discovery(String),
}

pub fn validate_media(path: impl AsRef<Path>) -> Result<MediaMetadata, AssetError> {
    let path = path.as_ref();
    let file = fs::metadata(path).map_err(|error| AssetError::InvalidSource(error.to_string()))?;
    if !file.is_file() || file.len() == 0 {
        return Err(AssetError::InvalidSource(
            "the source must be a non-empty regular file".into(),
        ));
    }

    gst::init().map_err(|error| AssetError::Discovery(error.to_string()))?;
    let canonical = fs::canonicalize(path).map_err(|error| AssetError::Io(error.to_string()))?;
    let uri = gst::glib::filename_to_uri(&canonical, None)
        .map_err(|error| AssetError::InvalidSource(error.to_string()))?;
    let discoverer = gst_pbutils::Discoverer::new(gst::ClockTime::from_seconds(15))
        .map_err(|error| AssetError::Discovery(error.to_string()))?;
    let info = discoverer
        .discover_uri(uri.as_str())
        .map_err(|error| AssetError::Discovery(error.to_string()))?;
    if info.result() != gst_pbutils::DiscovererResult::Ok {
        return Err(AssetError::UnsupportedMedia(format!(
            "media discovery did not complete successfully: {:?}",
            info.result()
        )));
    }
    let missing = info.missing_elements_installer_details();
    if !missing.is_empty() {
        return Err(AssetError::UnsupportedMedia(format!(
            "required decoder elements are unavailable: {}",
            missing.join(", ")
        )));
    }

    let video = info.video_streams();
    let audio = info.audio_streams();
    if video.is_empty() || audio.is_empty() {
        return Err(AssetError::UnsupportedMedia(
            "playout assets must contain at least one video and one audio stream".into(),
        ));
    }
    let duration = info
        .duration()
        .filter(|value| !value.is_zero())
        .ok_or_else(|| {
            AssetError::UnsupportedMedia("playout assets must have a finite duration".into())
        })?;

    Ok(MediaMetadata {
        duration_ns: duration.nseconds(),
        video_streams: video.len() as u32,
        audio_streams: audio.len() as u32,
        container: info
            .container_streams()
            .first()
            .and_then(|stream| stream.caps())
            .and_then(|caps| caps.structure(0).map(|value| value.name().to_string())),
        video_codec: codec_name(&video[0])?,
        audio_codec: codec_name(&audio[0])?,
    })
}

pub fn ingest_media(
    source: impl AsRef<Path>,
    library: impl AsRef<Path>,
) -> Result<IngestedAsset, AssetError> {
    let source = source.as_ref();
    let metadata = validate_media(source)?;
    fs::create_dir_all(library.as_ref()).map_err(|error| AssetError::Io(error.to_string()))?;
    let library =
        fs::canonicalize(library.as_ref()).map_err(|error| AssetError::Io(error.to_string()))?;
    let temporary = library.join(format!(
        ".ingest-{}-{}.part",
        std::process::id(),
        std::thread::current().name().unwrap_or("worker")
    ));
    let copied = copy_and_hash(source, &temporary);
    let (asset_id, size_bytes) = match copied {
        Ok(copied) => copied,
        Err(error) => {
            let _ = fs::remove_file(&temporary);
            return Err(error);
        }
    };
    let extension = source
        .extension()
        .and_then(|value| value.to_str())
        .filter(|value| {
            !value.is_empty()
                && value.len() <= 12
                && value.bytes().all(|byte| byte.is_ascii_alphanumeric())
        })
        .map(str::to_ascii_lowercase)
        .unwrap_or_else(|| "media".into());
    let stored_path = library.join(format!("{asset_id}.{extension}"));

    if stored_path.exists() {
        fs::remove_file(&temporary).map_err(|error| AssetError::Io(error.to_string()))?;
        let (stored_id, stored_size) = hash_file(&stored_path)?;
        if stored_id != asset_id || stored_size != size_bytes {
            return Err(AssetError::Io(
                "the content-addressed library object is corrupt".into(),
            ));
        }
    } else if let Err(error) = fs::rename(&temporary, &stored_path) {
        let _ = fs::remove_file(&temporary);
        return Err(AssetError::Io(error.to_string()));
    }

    let (stored_id, stored_size) = hash_file(&stored_path)?;
    if stored_id != asset_id || stored_size != size_bytes {
        return Err(AssetError::Io(
            "the copied library object failed its post-copy identity and size verification".into(),
        ));
    }

    let stored_metadata = validate_media(&stored_path)?;
    if stored_metadata != metadata {
        return Err(AssetError::Io(
            "the stored media metadata differs from the validated source".into(),
        ));
    }
    Ok(IngestedAsset {
        asset_id,
        stored_path,
        size_bytes,
        metadata: stored_metadata,
    })
}

pub fn verify_media_identity(
    path: impl AsRef<Path>,
    expected_asset_id: &str,
) -> Result<MediaMetadata, AssetError> {
    if expected_asset_id.len() != 64
        || !expected_asset_id
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        return Err(AssetError::InvalidSource(
            "asset identity must be a lowercase SHA-256 digest".into(),
        ));
    }
    let (actual_asset_id, _) = hash_file(path.as_ref())?;
    if actual_asset_id != expected_asset_id {
        return Err(AssetError::InvalidSource(format!(
            "asset identity mismatch: expected {expected_asset_id}, measured {actual_asset_id}"
        )));
    }
    validate_media(path)
}

/// Decodes at most one scaled video frame from an identity-verified managed
/// source. The caller supplies only a managed root; both the temporary and
/// final output names are generated from server-owned identity facts.
pub fn derive_review_thumbnail(
    source: impl AsRef<Path>,
    thumbnail_root: impl AsRef<Path>,
    expected_asset_id: &str,
) -> Result<ThumbnailArtifact, AssetError> {
    let source = source.as_ref();
    validate_asset_id(expected_asset_id)?;
    let source_metadata = fs::symlink_metadata(source)
        .map_err(|error| AssetError::InvalidSource(error.to_string()))?;
    if source_metadata.file_attributes() & 0x0400 != 0 || !source_metadata.is_file() {
        return Err(AssetError::InvalidSource(
            "thumbnail source must be a non-reparse regular managed file".into(),
        ));
    }
    // Hold this handle through the copy.  The decoder never consumes the
    // caller's pathname: it consumes a verified, worker-owned snapshot.
    let source_handle = File::open(source).map_err(|error| AssetError::Io(error.to_string()))?;
    if !source_handle
        .metadata()
        .map_err(|error| AssetError::Io(error.to_string()))?
        .is_file()
    {
        return Err(AssetError::InvalidSource(
            "thumbnail source handle is not a regular file".into(),
        ));
    }
    gst::init().map_err(|error| AssetError::Discovery(error.to_string()))?;
    create_non_reparse_directory_chain(thumbnail_root.as_ref())?;
    ensure_non_reparse_ancestry(thumbnail_root.as_ref())?;
    let root = fs::canonicalize(thumbnail_root.as_ref())
        .map_err(|error| AssetError::Io(error.to_string()))?;
    ensure_non_reparse_ancestry(&root)?;
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| AssetError::Io(error.to_string()))?
        .as_nanos();
    let pinned_source = root.join(format!(
        ".thumbnail-source-{expected_asset_id}-{nonce}.part"
    ));
    let temporary = root.join(format!(".thumbnail-{expected_asset_id}-{nonce}.part.png"));
    let (measured_id, _) = copy_open_file_and_hash(source_handle, &pinned_source)?;
    if measured_id != expected_asset_id {
        let _ = fs::remove_file(&pinned_source);
        return Err(AssetError::InvalidSource(format!(
            "asset identity mismatch: expected {expected_asset_id}, measured {measured_id}"
        )));
    }
    ensure_non_reparse_ancestry(&pinned_source)?;
    let uri = gst::glib::filename_to_uri(&pinned_source, None)
        .map_err(|error| AssetError::InvalidSource(error.to_string()))?;
    let pipeline = gst::Pipeline::new();
    let decoder = gst::ElementFactory::make("uridecodebin")
        .property("uri", uri.as_str())
        .property("expose-all-streams", false)
        .build()
        .map_err(|error| AssetError::Discovery(error.to_string()))?;
    let queue = make_element("queue")?;
    let convert = make_element("videoconvert")?;
    let scale = make_element("videoscale")?;
    let caps = gst::ElementFactory::make("capsfilter")
        .property(
            "caps",
            gst::Caps::builder("video/x-raw")
                .field("format", "RGBA")
                .field("width", 320_i32)
                .field("height", 180_i32)
                .build(),
        )
        .build()
        .map_err(|error| AssetError::Discovery(error.to_string()))?;
    let encoder = gst::ElementFactory::make("pngenc")
        .property("snapshot", true)
        .build()
        .map_err(|error| AssetError::Discovery(error.to_string()))?;
    let sink = gst::ElementFactory::make("filesink")
        .property("location", temporary.to_string_lossy().as_ref())
        .property("sync", false)
        .build()
        .map_err(|error| AssetError::Discovery(error.to_string()))?;
    pipeline
        .add_many([&decoder, &queue, &convert, &scale, &caps, &encoder, &sink])
        .map_err(|error| AssetError::Discovery(error.to_string()))?;
    gst::Element::link_many([&queue, &convert, &scale, &caps, &encoder, &sink])
        .map_err(|error| AssetError::Discovery(error.to_string()))?;
    let queue_pad = queue
        .static_pad("sink")
        .ok_or_else(|| AssetError::Discovery("thumbnail queue has no sink pad".into()))?;
    let linked = Arc::new(AtomicBool::new(false));
    let linked_signal = Arc::clone(&linked);
    decoder.connect_pad_added(move |_, source_pad| {
        let caps = source_pad
            .current_caps()
            .unwrap_or_else(|| source_pad.query_caps(None));
        if caps
            .structure(0)
            .is_some_and(|structure| structure.name().starts_with("video/"))
            && !queue_pad.is_linked()
            && source_pad.link(&queue_pad).is_ok()
        {
            linked_signal.store(true, Ordering::Release);
        }
    });
    pipeline
        .set_state(gst::State::Playing)
        .map_err(|error| AssetError::Discovery(error.to_string()))?;
    let result = wait_for_thumbnail(&pipeline, &linked);
    let stopped = stop_pipeline(&pipeline);
    let _ = fs::remove_file(&pinned_source);
    if let Err(error) = result {
        let _ = fs::remove_file(&temporary);
        return Err(error);
    }
    if let Err(error) = stopped {
        let _ = fs::remove_file(&temporary);
        return Err(error);
    }
    ensure_non_reparse_ancestry(&temporary)?;
    let (thumbnail_sha256, size_bytes) = hash_file(&temporary)?;
    if size_bytes > MAX_REVIEW_THUMBNAIL_BYTES {
        let _ = fs::remove_file(&temporary);
        return Err(AssetError::Io(
            "derived thumbnail exceeds the 8 MiB bound".into(),
        ));
    }
    let final_path = root.join(format!("{thumbnail_sha256}.png"));
    if final_path.exists() {
        let (existing_hash, existing_bytes) = hash_file(&final_path)?;
        let _ = fs::remove_file(&temporary);
        if existing_hash != thumbnail_sha256 || existing_bytes != size_bytes {
            return Err(AssetError::Io(
                "content-addressed thumbnail path is corrupt".into(),
            ));
        }
    } else if let Err(error) = fs::rename(&temporary, &final_path) {
        let _ = fs::remove_file(&temporary);
        return Err(AssetError::Io(error.to_string()));
    }
    Ok(ThumbnailArtifact {
        source_asset_id: expected_asset_id.into(),
        thumbnail_sha256,
        stored_path: final_path,
        size_bytes,
    })
}

pub fn measure_loudness(path: impl AsRef<Path>) -> Result<LoudnessMeasurement, AssetError> {
    measure_loudness_with_minimum(path, None)
}

/// Measures decoded MPEG-TS or other media loudness and requires a minimum
/// amount of analyzer-covered running time before returning evidence.
pub fn measure_output_loudness(
    path: impl AsRef<Path>,
    minimum_duration_ms: u64,
) -> Result<LoudnessMeasurement, AssetError> {
    if minimum_duration_ms == 0 {
        return Err(AssetError::InvalidSource(
            "minimum analyzed loudness duration must be positive".into(),
        ));
    }
    let metadata = fs::metadata(path.as_ref())
        .map_err(|error| AssetError::InvalidSource(error.to_string()))?;
    if !metadata.is_file() || metadata.len() == 0 {
        return Err(AssetError::InvalidSource(
            "the output capture must be a non-empty regular file".into(),
        ));
    }
    // Output proof is stricter than general loudness analysis: discover the
    // captured transport first so a missing audio program fails explicitly
    // instead of leaving an unlinked analyzer branch to reach its deadline.
    validate_media(path.as_ref())?;
    measure_loudness_with_minimum(path, Some(minimum_duration_ms))
}

/// Independently decodes CEA captions from a bounded MPEG-TS capture through
/// an exact FFmpeg executable. The output is staged beside the proof-owned
/// capture, bounded before reading, parsed as SRT, and removed on every path.
pub fn decode_output_captions(
    capture_path: impl AsRef<Path>,
    ffmpeg_executable: impl AsRef<Path>,
) -> Result<DecodedCaptionEvidence, AssetError> {
    let capture_path = capture_path.as_ref();
    let capture =
        fs::metadata(capture_path).map_err(|error| AssetError::InvalidSource(error.to_string()))?;
    if !capture.is_file() || capture.len() == 0 || capture.len() > MAX_CAPTION_PROOF_CAPTURE_BYTES {
        return Err(AssetError::InvalidSource(
            "caption proof capture must be a bounded nonempty regular file".into(),
        ));
    }
    let canonical_capture = fs::canonicalize(capture_path)
        .map_err(|error| AssetError::InvalidSource(error.to_string()))?;
    let ffmpeg_executable = ffmpeg_executable.as_ref();
    if !ffmpeg_executable.is_absolute() {
        return Err(AssetError::InvalidSource(
            "caption decoder executable path must be absolute".into(),
        ));
    }
    let decoder = fs::metadata(ffmpeg_executable)
        .map_err(|error| AssetError::InvalidSource(error.to_string()))?;
    if !decoder.is_file() || decoder.len() == 0 {
        return Err(AssetError::InvalidSource(
            "caption decoder executable must be a nonempty regular file".into(),
        ));
    }
    let canonical_decoder = fs::canonicalize(ffmpeg_executable)
        .map_err(|error| AssetError::InvalidSource(error.to_string()))?;
    let decoder_sha256 = hash_file(&canonical_decoder)?.0;
    let capture_parent = canonical_capture
        .parent()
        .ok_or_else(|| AssetError::InvalidSource("caption capture has no parent".into()))?;
    let capture_name = canonical_capture
        .file_name()
        .and_then(|value| value.to_str())
        .filter(|value| {
            !value
                .chars()
                .any(|character| matches!(character, ';' | '[' | ']'))
        })
        .ok_or_else(|| AssetError::InvalidSource("caption capture filename is unsafe".into()))?;
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| AssetError::Io(error.to_string()))?
        .as_nanos();
    let output_name = format!(".caption-decode-{}-{nonce}.srt.part", std::process::id());
    let output_path = capture_parent.join(&output_name);
    let _cleanup = TemporaryDecodeOutput(output_path.clone());
    let error_path = capture_parent.join(format!(
        ".caption-decode-{}-{nonce}.stderr.part",
        std::process::id()
    ));
    let error_file = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&error_path)
        .map_err(|error| AssetError::Io(format!("create caption decoder error log: {error}")))?;
    let _error_cleanup = TemporaryDecodeOutput(error_path.clone());
    let movie = format!("movie={capture_name}[out0+subcc]");
    let mut child = Command::new(&canonical_decoder)
        .current_dir(capture_parent)
        .args([
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-n",
            "-f",
            "lavfi",
            "-i",
            &movie,
            "-map",
            "0:1",
            "-f",
            "srt",
            &output_name,
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::from(error_file))
        .spawn()
        .map_err(|error| AssetError::Discovery(format!("start caption decoder: {error}")))?;
    let deadline = Instant::now() + Duration::from_secs(30);
    let status = loop {
        match child
            .try_wait()
            .map_err(|error| AssetError::Discovery(format!("poll caption decoder: {error}")))?
        {
            Some(status) => break status,
            None if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(20)),
            None => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(AssetError::Discovery(
                    "caption decoder exceeded its 30-second deadline".into(),
                ));
            }
        }
    };
    if !status.success() {
        let detail = read_bounded_decoder_error(&error_path);
        return Err(AssetError::Discovery(format!(
            "caption decoder exited unsuccessfully: {status}: {detail}"
        )));
    }
    let output = fs::metadata(&output_path).map_err(|error| {
        AssetError::Discovery(format!("inspect caption decoder output: {error}"))
    })?;
    if !output.is_file() || output.len() == 0 || output.len() > MAX_DECODED_CAPTION_SRT_BYTES {
        return Err(AssetError::Discovery(
            "caption decoder produced empty or unbounded SRT evidence".into(),
        ));
    }
    let mut bytes = Vec::with_capacity(output.len() as usize);
    File::open(&output_path)
        .and_then(|file| {
            file.take(MAX_DECODED_CAPTION_SRT_BYTES + 1)
                .read_to_end(&mut bytes)
        })
        .map_err(|error| AssetError::Io(format!("read caption decoder output: {error}")))?;
    if bytes.len() as u64 > MAX_DECODED_CAPTION_SRT_BYTES {
        return Err(AssetError::Discovery(
            "caption decoder output exceeded its read bound".into(),
        ));
    }
    let srt = String::from_utf8(bytes)
        .map_err(|_| AssetError::Discovery("caption decoder output is not UTF-8 SRT".into()))?;
    let texts = parse_srt_caption_texts(&srt)?;
    let cue_count = u32::try_from(texts.len())
        .map_err(|_| AssetError::Discovery("decoded caption cue count overflowed".into()))?;
    let text_sha256 = canonical_caption_text_sha256(&texts).map_err(AssetError::Discovery)?;
    Ok(DecodedCaptionEvidence {
        cue_count,
        text_sha256,
        decoder_engine: format!("ffmpeg-sha256={decoder_sha256}"),
        decoder_method: "lavfi movie subcc to bounded SRT canonical text v1".into(),
    })
}

fn read_bounded_decoder_error(path: &Path) -> String {
    let mut bytes = Vec::with_capacity(4_096);
    if File::open(path)
        .and_then(|file| file.take(4_096).read_to_end(&mut bytes))
        .is_err()
    {
        return "decoder error detail unavailable".into();
    }
    String::from_utf8_lossy(&bytes)
        .chars()
        .filter(|character| !character.is_control() || character.is_whitespace())
        .collect::<String>()
        .trim()
        .to_string()
}

fn parse_srt_caption_texts(srt: &str) -> Result<Vec<String>, AssetError> {
    let normalized = srt.replace("\r\n", "\n").replace('\r', "\n");
    let mut texts = Vec::new();
    for block in normalized.split("\n\n") {
        let lines = block.lines().collect::<Vec<_>>();
        if lines.iter().all(|line| line.trim().is_empty()) {
            continue;
        }
        let timestamp_index = lines
            .iter()
            .position(|line| line.contains("-->"))
            .ok_or_else(|| {
                AssetError::Discovery("caption decoder returned malformed SRT".into())
            })?;
        let raw_text = lines[timestamp_index + 1..]
            .iter()
            .map(|line| line.trim())
            .filter(|line| !line.is_empty())
            .collect::<Vec<_>>()
            .join("\n");
        let text = normalize_ffmpeg_srt_presentation(&raw_text);
        if text.is_empty() {
            if is_ffmpeg_cea708_clear_window_record(&raw_text) {
                // tttocea708 ClearWindows becomes this exact FFmpeg SRT
                // presentation-only record. It is a valid state transition,
                // not caption text, so omit it from canonical evidence.
                continue;
            }
            return Err(AssetError::Discovery(
                "caption decoder returned an SRT cue without text".into(),
            ));
        }
        texts.push(text);
    }
    if texts.is_empty() {
        return Err(AssetError::Discovery(
            "caption decoder returned no caption cues".into(),
        ));
    }
    Ok(texts)
}

fn normalize_ffmpeg_srt_presentation(text: &str) -> String {
    let text = text
        .strip_prefix("<font face=\"Monospace\">")
        .unwrap_or(text);
    let text = text.strip_suffix("</font>").unwrap_or(text);
    text.strip_prefix("{\\an7}").unwrap_or(text).to_string()
}

fn is_ffmpeg_cea708_clear_window_record(text: &str) -> bool {
    text.trim() == "<font face=\"Monospace\">{\\an7}</font>"
}

struct TemporaryDecodeOutput(PathBuf);

impl Drop for TemporaryDecodeOutput {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

fn measure_loudness_with_minimum(
    path: impl AsRef<Path>,
    minimum_duration_ms: Option<u64>,
) -> Result<LoudnessMeasurement, AssetError> {
    gst::init().map_err(|error| AssetError::Discovery(error.to_string()))?;
    let canonical = fs::canonicalize(path).map_err(|error| AssetError::Io(error.to_string()))?;
    let uri = gst::glib::filename_to_uri(&canonical, None)
        .map_err(|error| AssetError::InvalidSource(error.to_string()))?;
    let pipeline = gst::Pipeline::new();
    let decoder = gst::ElementFactory::make("uridecodebin")
        .property("uri", uri.as_str())
        .property("expose-all-streams", false)
        .build()
        .map_err(|error| AssetError::Discovery(error.to_string()))?;
    let audio_queue = make_element("queue")?;
    let audio_convert = make_element("audioconvert")?;
    let audio_resample = make_element("audioresample")?;
    let level = gst::ElementFactory::make("ebur128level")
        .property("interval", 1_000_000_000_u64)
        .property("post-messages", true)
        .build()
        .map_err(|error| AssetError::Discovery(error.to_string()))?;
    let audio_sink = gst::ElementFactory::make("fakesink")
        .property("sync", false)
        .build()
        .map_err(|error| AssetError::Discovery(error.to_string()))?;
    let video_queue = make_element("queue")?;
    let video_sink = gst::ElementFactory::make("fakesink")
        .property("sync", false)
        .build()
        .map_err(|error| AssetError::Discovery(error.to_string()))?;
    pipeline
        .add_many([
            &decoder,
            &audio_queue,
            &audio_convert,
            &audio_resample,
            &level,
            &audio_sink,
            &video_queue,
            &video_sink,
        ])
        .map_err(|error| AssetError::Discovery(error.to_string()))?;
    gst::Element::link_many([
        &audio_queue,
        &audio_convert,
        &audio_resample,
        &level,
        &audio_sink,
    ])
    .map_err(|error| AssetError::Discovery(error.to_string()))?;
    video_queue
        .link(&video_sink)
        .map_err(|error| AssetError::Discovery(error.to_string()))?;
    let audio_pad = audio_queue
        .static_pad("sink")
        .ok_or_else(|| AssetError::Discovery("audio queue has no sink pad".into()))?;
    let video_pad = video_queue
        .static_pad("sink")
        .ok_or_else(|| AssetError::Discovery("video queue has no sink pad".into()))?;
    let audio_linked = Arc::new(AtomicBool::new(false));
    let audio_linked_signal = Arc::clone(&audio_linked);
    decoder.connect_pad_added(move |_, source_pad| {
        let caps = source_pad
            .current_caps()
            .unwrap_or_else(|| source_pad.query_caps(None));
        let Some(media_type) = caps.structure(0).map(|value| value.name()) else {
            return;
        };
        if media_type.starts_with("audio/") && !audio_pad.is_linked() {
            if source_pad.link(&audio_pad).is_ok() {
                audio_linked_signal.store(true, Ordering::Release);
            }
        } else if media_type.starts_with("video/") && !video_pad.is_linked() {
            let _ = source_pad.link(&video_pad);
        }
    });
    pipeline
        .set_state(gst::State::Playing)
        .map_err(|error| AssetError::Discovery(error.to_string()))?;
    let result = collect_loudness(&pipeline, &level, &audio_linked, minimum_duration_ms);
    let stopped = stop_pipeline(&pipeline);
    match (result, stopped) {
        (Err(error), _) | (Ok(_), Err(error)) => Err(error),
        (Ok(measurement), Ok(())) => Ok(measurement),
    }
}

pub fn transcode_playout_asset(
    source: impl AsRef<Path>,
    library: impl AsRef<Path>,
) -> Result<IngestedAsset, AssetError> {
    validate_media(&source)?;
    gst::init().map_err(|error| AssetError::Discovery(error.to_string()))?;
    fs::create_dir_all(&library).map_err(|error| AssetError::Io(error.to_string()))?;
    let library = fs::canonicalize(library).map_err(|error| AssetError::Io(error.to_string()))?;
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| AssetError::Io(error.to_string()))?
        .as_nanos();
    let temporary = library.join(format!(".transcode-{}-{nonce}.ts", std::process::id()));
    let canonical = fs::canonicalize(source).map_err(|error| AssetError::Io(error.to_string()))?;
    let uri = gst::glib::filename_to_uri(&canonical, None)
        .map_err(|error| AssetError::InvalidSource(error.to_string()))?;
    let pipeline = gst::Pipeline::new();
    let decoder = gst::ElementFactory::make("uridecodebin")
        .property("uri", uri.as_str())
        .property("expose-all-streams", false)
        .build()
        .map_err(|error| AssetError::Discovery(error.to_string()))?;
    let video_queue = make_element("queue")?;
    let video_convert = make_element("videoconvert")?;
    let video_scale = make_element("videoscale")?;
    let video_rate = make_element("videorate")?;
    let video_caps = gst::ElementFactory::make("capsfilter")
        .property(
            "caps",
            gst::Caps::builder("video/x-raw")
                .field("format", "I420")
                .field("width", 1280_i32)
                .field("height", 720_i32)
                .field("framerate", gst::Fraction::new(30, 1))
                .build(),
        )
        .build()
        .map_err(|error| AssetError::Discovery(error.to_string()))?;
    let video_encoder = gst::ElementFactory::make("openh264enc")
        .property("bitrate", 4_000_000_u32)
        .build()
        .map_err(|error| AssetError::Discovery(error.to_string()))?;
    let video_parser = make_element("h264parse")?;
    let video_output_queue = make_element("queue")?;
    let audio_queue = make_element("queue")?;
    let audio_convert_in = make_element("audioconvert")?;
    let audio_resample_in = make_element("audioresample")?;
    let loudness_input_caps = gst::ElementFactory::make("capsfilter")
        .property(
            "caps",
            gst::Caps::builder("audio/x-raw")
                .field("format", "F64LE")
                .field("rate", 192_000_i32)
                .field("channels", 2_i32)
                .build(),
        )
        .build()
        .map_err(|error| AssetError::Discovery(error.to_string()))?;
    let normalizer = gst::ElementFactory::make("audioloudnorm")
        .property("loudness-target", -24.0_f64)
        .property("max-true-peak", -2.0_f64)
        .property("loudness-range-target", 7.0_f64)
        .build()
        .map_err(|error| AssetError::Discovery(error.to_string()))?;
    let audio_convert_out = make_element("audioconvert")?;
    let audio_resample_out = make_element("audioresample")?;
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
        .map_err(|error| AssetError::Discovery(error.to_string()))?;
    let audio_encoder = gst::ElementFactory::make("voaacenc")
        .property("bitrate", 192_000_i32)
        .build()
        .map_err(|error| AssetError::Discovery(error.to_string()))?;
    let audio_parser = make_element("aacparse")?;
    let audio_output_queue = make_element("queue")?;
    let mux = make_element("mpegtsmux")?;
    let sink = gst::ElementFactory::make("filesink")
        .property("location", temporary.to_string_lossy().as_ref())
        .build()
        .map_err(|error| AssetError::Discovery(error.to_string()))?;
    pipeline
        .add_many([
            &decoder,
            &video_queue,
            &video_convert,
            &video_scale,
            &video_rate,
            &video_caps,
            &video_encoder,
            &video_parser,
            &video_output_queue,
            &audio_queue,
            &audio_convert_in,
            &audio_resample_in,
            &loudness_input_caps,
            &normalizer,
            &audio_convert_out,
            &audio_resample_out,
            &audio_caps,
            &audio_encoder,
            &audio_parser,
            &audio_output_queue,
            &mux,
            &sink,
        ])
        .map_err(|error| AssetError::Discovery(error.to_string()))?;
    gst::Element::link_many([
        &video_queue,
        &video_convert,
        &video_scale,
        &video_rate,
        &video_caps,
        &video_encoder,
        &video_parser,
        &video_output_queue,
        &mux,
        &sink,
    ])
    .map_err(|error| AssetError::Discovery(error.to_string()))?;
    gst::Element::link_many([
        &audio_queue,
        &audio_convert_in,
        &audio_resample_in,
        &loudness_input_caps,
        &normalizer,
        &audio_convert_out,
        &audio_resample_out,
        &audio_caps,
        &audio_encoder,
        &audio_parser,
        &audio_output_queue,
        &mux,
    ])
    .map_err(|error| AssetError::Discovery(error.to_string()))?;
    let video_pad = video_queue
        .static_pad("sink")
        .ok_or_else(|| AssetError::Discovery("video queue has no sink pad".into()))?;
    let audio_pad = audio_queue
        .static_pad("sink")
        .ok_or_else(|| AssetError::Discovery("audio queue has no sink pad".into()))?;
    let video_linked = Arc::new(AtomicBool::new(false));
    let audio_linked = Arc::new(AtomicBool::new(false));
    let video_signal = Arc::clone(&video_linked);
    let audio_signal = Arc::clone(&audio_linked);
    decoder.connect_pad_added(move |_, source_pad| {
        let caps = source_pad
            .current_caps()
            .unwrap_or_else(|| source_pad.query_caps(None));
        let Some(media_type) = caps.structure(0).map(|value| value.name()) else {
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
    pipeline
        .set_state(gst::State::Playing)
        .map_err(|error| AssetError::Discovery(error.to_string()))?;
    let transcode = wait_for_transcode(&pipeline, &video_linked, &audio_linked);
    let stopped = stop_pipeline(&pipeline);
    if let Err(error) = transcode {
        let _ = fs::remove_file(&temporary);
        return Err(error);
    }
    if let Err(error) = stopped {
        let _ = fs::remove_file(&temporary);
        return Err(error);
    }
    let output = ingest_media(&temporary, &library);
    remove_file_bounded(&temporary, Duration::from_secs(5))?;
    output
}

fn remove_file_bounded(path: &Path, timeout: Duration) -> Result<(), AssetError> {
    let deadline = Instant::now() + timeout;
    loop {
        match fs::remove_file(path) {
            Ok(()) => return Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(_error) if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(25));
            }
            Err(error) => {
                return Err(AssetError::Io(format!(
                    "the temporary media file was not released within {} ms: {error}",
                    timeout.as_millis()
                )));
            }
        }
    }
}

fn stop_pipeline(pipeline: &gst::Pipeline) -> Result<(), AssetError> {
    pipeline
        .set_state(gst::State::Null)
        .map_err(|error| AssetError::Discovery(format!("media pipeline stop failed: {error:?}")))?;
    pipeline
        .state(gst::ClockTime::from_seconds(5))
        .0
        .map(|_| ())
        .map_err(|error| {
            AssetError::Discovery(format!(
                "media pipeline did not reach the stopped state: {error:?}"
            ))
        })
}

fn wait_for_transcode(
    pipeline: &gst::Pipeline,
    video_linked: &AtomicBool,
    audio_linked: &AtomicBool,
) -> Result<(), AssetError> {
    let bus = pipeline
        .bus()
        .ok_or_else(|| AssetError::Discovery("transcode pipeline has no bus".into()))?;
    let deadline = Instant::now() + Duration::from_secs(300);
    while Instant::now() < deadline {
        let remaining = deadline.saturating_duration_since(Instant::now());
        let Some(message) = bus.timed_pop(gst::ClockTime::from_nseconds(
            remaining.as_nanos().min(u128::from(u64::MAX)) as u64,
        )) else {
            break;
        };
        match message.view() {
            gst::MessageView::Eos(_) => {
                if video_linked.load(Ordering::Acquire) && audio_linked.load(Ordering::Acquire) {
                    return Ok(());
                }
                return Err(AssetError::UnsupportedMedia(
                    "canonical transcode did not receive both audio and video".into(),
                ));
            }
            gst::MessageView::Error(error) => {
                return Err(AssetError::Discovery(format!(
                    "canonical transcode failed: {} ({:?})",
                    error.error(),
                    error.debug()
                )));
            }
            _ => {}
        }
    }
    Err(AssetError::Discovery(
        "canonical transcode exceeded its 300-second deadline".into(),
    ))
}

fn wait_for_thumbnail(
    pipeline: &gst::Pipeline,
    video_linked: &AtomicBool,
) -> Result<(), AssetError> {
    let bus = pipeline
        .bus()
        .ok_or_else(|| AssetError::Discovery("thumbnail pipeline has no bus".into()))?;
    let deadline = Instant::now() + REVIEW_THUMBNAIL_TIMEOUT;
    while Instant::now() < deadline {
        let remaining = deadline.saturating_duration_since(Instant::now());
        let Some(message) = bus.timed_pop(gst::ClockTime::from_nseconds(
            remaining.as_nanos().min(u128::from(u64::MAX)) as u64,
        )) else {
            break;
        };
        match message.view() {
            gst::MessageView::Eos(_) if video_linked.load(Ordering::Acquire) => return Ok(()),
            gst::MessageView::Eos(_) => {
                return Err(AssetError::UnsupportedMedia(
                    "thumbnail worker received no decodable video stream".into(),
                ));
            }
            gst::MessageView::Error(error) => {
                return Err(AssetError::Discovery(format!(
                    "thumbnail worker failed: {} ({:?})",
                    error.error(),
                    error.debug()
                )));
            }
            _ => {}
        }
    }
    Err(AssetError::Discovery(
        "thumbnail worker exceeded its 30-second deadline".into(),
    ))
}

fn collect_loudness(
    pipeline: &gst::Pipeline,
    level: &gst::Element,
    audio_linked: &AtomicBool,
    minimum_duration_ms: Option<u64>,
) -> Result<LoudnessMeasurement, AssetError> {
    let bus = pipeline
        .bus()
        .ok_or_else(|| AssetError::Discovery("loudness pipeline has no bus".into()))?;
    let deadline = Instant::now() + Duration::from_secs(120);
    let mut integrated_lufs = None;
    let mut analyzed_duration_ns = None;
    while Instant::now() < deadline {
        let remaining = deadline.saturating_duration_since(Instant::now());
        let Some(message) = bus.timed_pop(gst::ClockTime::from_nseconds(
            remaining.as_nanos().min(u128::from(u64::MAX)) as u64,
        )) else {
            break;
        };
        match message.view() {
            gst::MessageView::Element(element) => {
                let Some(structure) = element.structure() else {
                    continue;
                };
                if structure.name() == "ebur128-level"
                    && message
                        .src()
                        .is_some_and(|source| source == level.upcast_ref::<gst::Object>())
                {
                    if let Ok(value) = structure.get::<f64>("global-loudness")
                        && value.is_finite()
                    {
                        integrated_lufs = Some(value);
                    }
                    if let Ok(value) = structure.get::<u64>("running-time")
                        && value != u64::MAX
                    {
                        analyzed_duration_ns = Some(analyzed_duration_ns.unwrap_or(0).max(value));
                    }
                }
            }
            gst::MessageView::Eos(_) => {
                if !audio_linked.load(Ordering::Acquire) {
                    return Err(AssetError::UnsupportedMedia(
                        "media contains no decodable audio stream".into(),
                    ));
                }
                let integrated_lufs = integrated_lufs.ok_or_else(|| {
                    AssetError::Discovery("loudness analyzer produced no measurement".into())
                })?;
                let analyzed_duration_ns = analyzed_duration_ns.ok_or_else(|| {
                    AssetError::Discovery("loudness analyzer produced no duration".into())
                })?;
                let analyzed_duration_ms = analyzed_duration_ns
                    .saturating_add(999_999)
                    .checked_div(1_000_000)
                    .unwrap_or(u64::MAX);
                if let Some(minimum_duration_ms) = minimum_duration_ms
                    && analyzed_duration_ms < minimum_duration_ms
                {
                    return Err(AssetError::Discovery(format!(
                        "loudness evidence covered {analyzed_duration_ms} ms, below the required {minimum_duration_ms} ms"
                    )));
                }
                return Ok(LoudnessMeasurement {
                    integrated_lufs,
                    analyzed_duration_ms,
                });
            }
            gst::MessageView::Error(error) => {
                return Err(AssetError::Discovery(format!(
                    "loudness pipeline failed: {} ({:?})",
                    error.error(),
                    error.debug()
                )));
            }
            _ => {}
        }
    }
    Err(AssetError::Discovery(
        "loudness measurement exceeded its 120-second processing deadline".into(),
    ))
}

fn make_element(factory: &'static str) -> Result<gst::Element, AssetError> {
    gst::ElementFactory::make(factory)
        .build()
        .map_err(|error| AssetError::Discovery(error.to_string()))
}

fn codec_name(stream: &impl IsA<gst_pbutils::DiscovererStreamInfo>) -> Result<String, AssetError> {
    stream
        .caps()
        .and_then(|caps| caps.structure(0).map(|value| value.name().to_string()))
        .ok_or_else(|| AssetError::UnsupportedMedia("a stream has no declared codec".into()))
}

fn copy_and_hash(source: &Path, destination: &Path) -> Result<(String, u64), AssetError> {
    let input = File::open(source).map_err(|error| AssetError::Io(error.to_string()))?;
    copy_open_file_and_hash(input, destination)
}

fn copy_open_file_and_hash(input: File, destination: &Path) -> Result<(String, u64), AssetError> {
    let output = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(destination)
        .map_err(|error| AssetError::Io(error.to_string()))?;
    let mut reader = BufReader::new(input);
    let mut writer = BufWriter::new(output);
    let mut digest = Sha256::new();
    let mut size = 0_u64;
    let mut buffer = vec![0_u8; 1024 * 1024];
    loop {
        let count = reader
            .read(&mut buffer)
            .map_err(|error| AssetError::Io(error.to_string()))?;
        if count == 0 {
            break;
        }
        writer
            .write_all(&buffer[..count])
            .map_err(|error| AssetError::Io(error.to_string()))?;
        digest.update(&buffer[..count]);
        size += count as u64;
    }
    writer
        .flush()
        .map_err(|error| AssetError::Io(error.to_string()))?;
    writer
        .get_ref()
        .sync_all()
        .map_err(|error| AssetError::Io(error.to_string()))?;
    Ok((format!("{:x}", digest.finalize()), size))
}

fn validate_asset_id(expected_asset_id: &str) -> Result<(), AssetError> {
    if expected_asset_id.len() != 64
        || !expected_asset_id
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        return Err(AssetError::InvalidSource(
            "asset identity must be a lowercase SHA-256 digest".into(),
        ));
    }
    Ok(())
}

fn ensure_non_reparse_ancestry(path: &Path) -> Result<(), AssetError> {
    let mut current = Some(path);
    while let Some(candidate) = current {
        let metadata = fs::symlink_metadata(candidate)
            .map_err(|_| AssetError::Io("managed thumbnail ancestry is unavailable".into()))?;
        if metadata.file_attributes() & 0x0400 != 0 {
            return Err(AssetError::InvalidSource(
                "managed thumbnail ancestry contains a reparse point".into(),
            ));
        }
        current = candidate.parent();
    }
    Ok(())
}

fn create_non_reparse_directory_chain(path: &Path) -> Result<(), AssetError> {
    let mut current = PathBuf::new();
    for component in path.components() {
        current.push(component.as_os_str());
        match fs::symlink_metadata(&current) {
            Ok(metadata) if metadata.file_attributes() & 0x0400 == 0 && metadata.is_dir() => {}
            Ok(_) => {
                return Err(AssetError::InvalidSource(
                    "managed thumbnail root contains a reparse point or non-directory".into(),
                ));
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                let parent = current
                    .parent()
                    .ok_or_else(|| AssetError::Io("managed thumbnail root has no parent".into()))?;
                let parent_metadata = fs::symlink_metadata(parent).map_err(|_| {
                    AssetError::Io("managed thumbnail root parent is unavailable".into())
                })?;
                if parent_metadata.file_attributes() & 0x0400 != 0 || !parent_metadata.is_dir() {
                    return Err(AssetError::InvalidSource(
                        "managed thumbnail root parent is a reparse point or non-directory".into(),
                    ));
                }
                fs::create_dir(&current).map_err(|error| AssetError::Io(error.to_string()))?;
                let created = fs::symlink_metadata(&current)
                    .map_err(|error| AssetError::Io(error.to_string()))?;
                if created.file_attributes() & 0x0400 != 0 || !created.is_dir() {
                    return Err(AssetError::InvalidSource(
                        "managed thumbnail root changed after creation".into(),
                    ));
                }
            }
            Err(error) => return Err(AssetError::Io(error.to_string())),
        }
    }
    Ok(())
}

fn hash_file(path: &Path) -> Result<(String, u64), AssetError> {
    let input = File::open(path).map_err(|error| AssetError::Io(error.to_string()))?;
    let mut reader = BufReader::new(input);
    let mut digest = Sha256::new();
    let mut size = 0_u64;
    let mut buffer = vec![0_u8; 1024 * 1024];
    loop {
        let count = reader
            .read(&mut buffer)
            .map_err(|error| AssetError::Io(error.to_string()))?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
        size += count as u64;
    }
    Ok((format!("{:x}", digest.finalize()), size))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_bounded_srt_into_ordered_caption_text() {
        let texts = parse_srt_caption_texts(
            "1\r\n00:00:00,100 --> 00:00:01,000\r\nTown Council\r\n\r\n2\r\n00:00:01,500 --> 00:00:02,000\r\nAgenda item 2\r\ncontinued\r\n",
        )
        .unwrap();
        assert_eq!(texts, vec!["Town Council", "Agenda item 2\ncontinued"]);
        assert_eq!(canonical_caption_text_sha256(&texts).unwrap().len(), 64);
    }

    #[test]
    fn removes_only_ffmpeg_generated_cea708_presentation_wrappers() {
        let texts = parse_srt_caption_texts(
            "1\n00:00:00,100 --> 00:00:01,000\n<font face=\"Monospace\">{\\an7}TownLight CEA-708 proof</font>\n",
        )
        .unwrap();
        assert_eq!(texts, vec!["TownLight CEA-708 proof"]);
        assert_eq!(
            normalize_ffmpeg_srt_presentation("<b>operator-authored markup</b>"),
            "<b>operator-authored markup</b>"
        );
    }

    #[test]
    fn ignores_a_cea708_clear_window_record_before_textual_caption_evidence() {
        let texts = parse_srt_caption_texts(
            "1\n00:00:00,100 --> 00:00:00,101\n<font face=\"Monospace\">{\\an7}</font>\n\n2\n00:00:00,350 --> 00:00:01,350\n<font face=\"Monospace\">{\\an7}post-switch caption</font>\n",
        )
        .unwrap();
        assert_eq!(texts, vec!["post-switch caption"]);
    }

    #[test]
    fn rejects_an_srt_stream_containing_only_cea708_clear_window_records() {
        let error = parse_srt_caption_texts(
            "1\n00:00:00,100 --> 00:00:00,101\n<font face=\"Monospace\">{\\an7}</font>\n",
        )
        .unwrap_err();
        assert!(matches!(
            error,
            AssetError::Discovery(ref detail) if detail == "caption decoder returned no caption cues"
        ));
    }

    #[test]
    fn rejects_empty_or_malformed_decoder_output() {
        assert!(parse_srt_caption_texts("").is_err());
        assert!(parse_srt_caption_texts("1\nno timestamp\ntext\n").is_err());
        assert!(parse_srt_caption_texts("1\n00:00:00,100 --> 00:00:01,000\n\n").is_err());
        assert!(parse_srt_caption_texts(
            "1\n00:00:00,100 --> 00:00:00,101\n\n2\n00:00:00,350 --> 00:00:01,350\ntext after malformed block\n"
        )
        .is_err());
    }

    #[test]
    fn thumbnail_worker_rejects_an_untrusted_identity_before_opening_any_path() {
        let error = derive_review_thumbnail("not-used", "not-used", "not-a-sha").unwrap_err();
        assert!(
            matches!(error, AssetError::InvalidSource(message) if message.contains("lowercase SHA-256"))
        );
    }
}
