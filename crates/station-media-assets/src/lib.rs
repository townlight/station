use std::fs::{self, File, OpenOptions};
use std::io::{BufReader, BufWriter, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use gst::prelude::*;
use gst_pbutils::prelude::*;
use gstreamer as gst;
use gstreamer_pbutils as gst_pbutils;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

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

#[derive(Debug, Clone, PartialEq)]
pub struct LoudnessMeasurement {
    pub integrated_lufs: f64,
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

pub fn measure_loudness(path: impl AsRef<Path>) -> Result<LoudnessMeasurement, AssetError> {
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
    let result = collect_loudness(&pipeline, &level, &audio_linked);
    let _ = pipeline.set_state(gst::State::Null);
    result
}

fn collect_loudness(
    pipeline: &gst::Pipeline,
    level: &gst::Element,
    audio_linked: &AtomicBool,
) -> Result<LoudnessMeasurement, AssetError> {
    let bus = pipeline
        .bus()
        .ok_or_else(|| AssetError::Discovery("loudness pipeline has no bus".into()))?;
    let deadline = Instant::now() + Duration::from_secs(120);
    let mut integrated_lufs = None;
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
                    && let Ok(value) = structure.get::<f64>("global-loudness")
                    && value.is_finite()
                {
                    integrated_lufs = Some(value);
                }
            }
            gst::MessageView::Eos(_) => {
                if !audio_linked.load(Ordering::Acquire) {
                    return Err(AssetError::UnsupportedMedia(
                        "media contains no decodable audio stream".into(),
                    ));
                }
                return integrated_lufs
                    .map(|integrated_lufs| LoudnessMeasurement { integrated_lufs })
                    .ok_or_else(|| {
                        AssetError::Discovery("loudness analyzer produced no measurement".into())
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
