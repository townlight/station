use std::fs::{self, OpenOptions};
use std::io::{BufWriter, Write};
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::sync::{
    Arc, Mutex, TryLockError,
    atomic::{AtomicBool, AtomicI64, AtomicU64, Ordering},
    mpsc::{self, SyncSender, TrySendError},
};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use gdk_pixbuf::Pixbuf;
use gst::prelude::*;
use gstreamer as gst;
use image::{ImageReader, Limits};
use sha2::{Digest, Sha256};
use station_media_protocol::{
    CaptionCue, CgImageFormat, CgOverlayContent, CgSnapshot, LoudnessTarget,
    MAX_CAPTION_DURATION_MS, MAX_CAPTION_START_AFTER_MS, MAX_CAPTION_TEXT_BYTES, MAX_CG_SLOTS,
    MAX_CG_TEXT_BYTES, MAX_OUTPUT_PROOF_BYTES, MAX_OUTPUT_PROOF_DURATION_MS,
    MIN_OUTPUT_PROOF_DURATION_MS, WorkerFallbackSlate,
};
use station_schedule::{OutputProfile, OutputProfileId};

static FILE_PROGRAM_SEQUENCE: AtomicU64 = AtomicU64::new(1);
static LIVE_PROGRAM_SEQUENCE: AtomicU64 = AtomicU64::new(1);
const AUDIO_TIMESTAMP_TOLERANCE_NS: u64 = 40_000_000;
const MPEG_TS_PACKET_BYTES: u16 = 188;
const DEFAULT_MPEG_TS_PACKET_SIZE_BYTES: u16 = 1_316;
const MAX_UDP_PAYLOAD_BYTES: u16 = 65_507;
const MAX_TRANSPORT_BITRATE_BPS: u64 = 100_000_000;
// The executable 8 Mb/s headend contract emits 1,316-byte buffers. 4,096
// buffers retain at most 5,390,336 of those bytes (about 5.4 seconds), while
// the independent byte ceiling keeps larger legal UDP payloads below 8 MiB.
// Either bound overflowing remains a terminal capture failure.
const OUTPUT_CAPTURE_QUEUE_BUFFERS: usize = 4_096;
const OUTPUT_CAPTURE_QUEUE_BYTES: u64 = 8 * 1024 * 1024;
const OUTPUT_CAPTURE_WRITE_BUFFER_BYTES: usize = 256 * 1024;
const OUTPUT_CAPTURE_WRITER_READY_TIMEOUT: Duration = Duration::from_secs(1);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SourceRole {
    Fallback,
    Program,
    Live,
}

#[derive(Clone, PartialEq, Eq)]
pub enum TransportOutput {
    Udp(SocketAddr),
    Srt {
        uri: String,
        passphrase: Option<String>,
    },
}

#[derive(Clone, PartialEq, Eq)]
pub struct ConfiguredTransportOutput {
    transport: TransportOutput,
    loudness_target: LoudnessTarget,
    sink_id: Option<String>,
    mux_contract: Option<MpegTsMuxContract>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MpegTsMuxContract {
    transport_bitrate_bps: u64,
    packet_size_bytes: u16,
}

impl MpegTsMuxContract {
    pub const fn new(transport_bitrate_bps: u64, packet_size_bytes: u16) -> Self {
        Self {
            transport_bitrate_bps,
            packet_size_bytes,
        }
    }

    pub const fn transport_bitrate_bps(self) -> u64 {
        self.transport_bitrate_bps
    }

    pub const fn packet_size_bytes(self) -> u16 {
        self.packet_size_bytes
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct OutputHealthObservation {
    pub bytes_sent_total: u64,
    pub audio_buffers_total: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OutputCaptureRequest {
    pub proof_id: String,
    pub output_index: u16,
    pub sink_id: String,
    pub loudness_target: LoudnessTarget,
    pub duration_ms: u64,
    pub max_bytes: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OutputCaptureStarted {
    pub proof_id: String,
    pub artifact_relative_path: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OutputCaptureReady {
    pub proof_id: String,
    pub output_index: u16,
    pub sink_id: String,
    pub loudness_target: LoudnessTarget,
    pub artifact_relative_path: String,
    pub capture_bytes: u64,
    pub capture_sha256: String,
    pub capture_duration_ms: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OutputCaptureStatus {
    Pending {
        proof_id: String,
        artifact_relative_path: String,
    },
    Ready(OutputCaptureReady),
    Failed {
        proof_id: String,
        artifact_relative_path: String,
        code: String,
        message: String,
    },
}

impl ConfiguredTransportOutput {
    pub fn udp(destination: SocketAddr, loudness_target: LoudnessTarget) -> Self {
        Self {
            transport: TransportOutput::Udp(destination),
            loudness_target,
            sink_id: None,
            mux_contract: None,
        }
    }

    pub fn udp_with_mux_contract(
        destination: SocketAddr,
        loudness_target: LoudnessTarget,
        mux_contract: MpegTsMuxContract,
    ) -> Self {
        Self {
            transport: TransportOutput::Udp(destination),
            loudness_target,
            sink_id: None,
            mux_contract: Some(mux_contract),
        }
    }

    pub fn udp_identified(
        sink_id: impl Into<String>,
        destination: SocketAddr,
        loudness_target: LoudnessTarget,
    ) -> Self {
        Self {
            transport: TransportOutput::Udp(destination),
            loudness_target,
            sink_id: Some(sink_id.into()),
            mux_contract: None,
        }
    }

    pub fn udp_identified_with_mux_contract(
        sink_id: impl Into<String>,
        destination: SocketAddr,
        loudness_target: LoudnessTarget,
        mux_contract: MpegTsMuxContract,
    ) -> Self {
        Self {
            transport: TransportOutput::Udp(destination),
            loudness_target,
            sink_id: Some(sink_id.into()),
            mux_contract: Some(mux_contract),
        }
    }

    pub fn srt(
        uri: impl Into<String>,
        passphrase: Option<String>,
        loudness_target: LoudnessTarget,
    ) -> Self {
        Self {
            transport: TransportOutput::Srt {
                uri: uri.into(),
                passphrase,
            },
            loudness_target,
            sink_id: None,
            mux_contract: None,
        }
    }

    pub fn srt_identified(
        sink_id: impl Into<String>,
        uri: impl Into<String>,
        passphrase: Option<String>,
        loudness_target: LoudnessTarget,
    ) -> Self {
        Self {
            transport: TransportOutput::Srt {
                uri: uri.into(),
                passphrase,
            },
            loudness_target,
            sink_id: Some(sink_id.into()),
            mux_contract: None,
        }
    }
}

#[derive(Debug)]
pub enum EngineError {
    Initialization(String),
    ProfileNotRunnable {
        profile_id: String,
    },
    ElementBuild {
        factory: &'static str,
        message: String,
    },
    Pipeline(String),
    Link {
        from: &'static str,
        to: &'static str,
    },
    MissingPad {
        element: &'static str,
        pad: &'static str,
    },
    PadLink(String),
    State(String),
    UnknownActivePad {
        actual: Option<String>,
        fallback: String,
        program: String,
        live: Option<String>,
    },
    SourceSelectorsDisagree {
        video: SourceRole,
        audio: SourceRole,
    },
    SwitchTimedOut {
        requested: SourceRole,
        actual: Option<SourceRole>,
    },
    MediaRejected(String),
    ProgramDecodeTimedOut {
        video_ready: bool,
        audio_ready: bool,
    },
    ProgramAlreadyStarted,
    LoadRequiresFallback,
    LiveNotArmed,
    LiveActive,
    LiveInputStalled {
        video_buffers: u64,
        audio_buffers: u64,
    },
    CgInvalid(String),
    UnsupportedLoudnessTarget {
        target_milli_lufs: i32,
    },
    UnsupportedMuxContract {
        transport_bitrate_bps: u64,
        packet_size_bytes: u16,
        reason: &'static str,
    },
    OutputCapture(String),
    CaptionInvalid(String),
    CaptionPush(String),
}

pub struct PersistentPlayout {
    pipeline: gst::Pipeline,
    video_selector: gst::Element,
    video_fallback_pad: gst::Pad,
    video_program_pad: gst::Pad,
    audio_selector: gst::Element,
    audio_fallback_pad: gst::Pad,
    audio_program_pad: gst::Pad,
    output_counters: Vec<OutputCounter>,
    output_audio_counters: Vec<Arc<AtomicU64>>,
    output_profile: OutputProfileId,
    file_program: Option<FileProgram>,
    live_program: Option<LiveProgram>,
    cg_slots: Vec<CgSlot>,
    cg_canvas_pad: gst::Pad,
    cg_image_root: Option<PathBuf>,
    cg_revision: Option<CgSnapshot>,
    output_variant_count: usize,
    output_capture_bindings: Vec<OutputCaptureBinding>,
    output_capture_ingresses: Vec<Arc<CaptureIngress>>,
    proof_artifact_root: Option<PathBuf>,
    caption_source: gst::Element,
    stopped: bool,
}

#[derive(Clone)]
struct OutputCaptureBinding {
    sink_id: Option<String>,
    capture_index: usize,
    loudness_target: LoudnessTarget,
}

struct CaptureIngress {
    active: AtomicBool,
    aborted: AtomicBool,
    generation: AtomicU64,
    callback_failed: AtomicBool,
    queued_bytes: AtomicU64,
    sender: Mutex<Option<SyncSender<(u64, gst::Buffer)>>>,
    proof: Mutex<Option<ActiveOutputCapture>>,
    writer: Mutex<Option<JoinHandle<()>>>,
}

struct ActiveOutputCapture {
    proof_id: String,
    status: Arc<Mutex<OutputCaptureStatus>>,
}

struct CgSlot {
    text_overlay: gst::Element,
    clock_overlay: gst::Element,
    image_overlays: [gst::Element; 2],
    active_image: Option<(usize, String)>,
}

enum OutputCounter {
    Udp(gst::Element),
    Srt(gst::Element),
}

struct OutputBranch {
    queue: gst::Element,
    sink: gst::Element,
    variant_index: usize,
    capture_ingress: Arc<CaptureIngress>,
}

struct LoudnessVariant {
    video_queue: gst::Element,
    audio_queue: gst::Element,
    audio_convert_in: gst::Element,
    audio_resample_in: gst::Element,
    audio_input_caps: gst::Element,
    normalizer: gst::Element,
    audio_convert_out: gst::Element,
    audio_resample_out: gst::Element,
    audio_output_caps: gst::Element,
    audio_encoder: gst::Element,
    audio_parser: gst::Element,
    audio_buffers: Arc<AtomicU64>,
    mux: gst::Element,
    transport_fanout: gst::Element,
}

impl PersistentPlayout {
    pub fn start_udp(destination: SocketAddr) -> Result<Self, EngineError> {
        Self::start_udp_with_profile(destination, OutputProfileId::default())
    }

    pub fn start_udp_with_profile(
        destination: SocketAddr,
        output_profile: OutputProfileId,
    ) -> Result<Self, EngineError> {
        Self::start_udp_fanout_with_profile(&[destination], output_profile)
    }

    pub fn start_udp_fanout_with_profile(
        destinations: &[SocketAddr],
        output_profile: OutputProfileId,
    ) -> Result<Self, EngineError> {
        let outputs = destinations
            .iter()
            .copied()
            .map(TransportOutput::Udp)
            .collect::<Vec<_>>();
        Self::start_outputs_with_profile(&outputs, output_profile)
    }

    pub fn start_outputs_with_profile(
        outputs: &[TransportOutput],
        output_profile: OutputProfileId,
    ) -> Result<Self, EngineError> {
        Self::start_outputs_with_profile_and_cg_root(outputs, output_profile, None)
    }

    pub fn start_outputs_with_profile_and_cg_root(
        outputs: &[TransportOutput],
        output_profile: OutputProfileId,
        cg_image_root: Option<&Path>,
    ) -> Result<Self, EngineError> {
        if outputs.is_empty() {
            return Err(EngineError::Pipeline(
                "at least one transport output is required".into(),
            ));
        }
        let loudness_target = profile_loudness_target(output_profile.profile())?;
        let configured_outputs = outputs
            .iter()
            .cloned()
            .map(|transport| ConfiguredTransportOutput {
                transport,
                loudness_target,
                sink_id: None,
                mux_contract: None,
            })
            .collect::<Vec<_>>();
        Self::start_outputs_with_program(
            &configured_outputs,
            None,
            output_profile,
            cg_image_root,
            None,
            None,
        )
    }

    pub fn start_file_udp(
        media_path: impl AsRef<Path>,
        destination: SocketAddr,
    ) -> Result<Self, EngineError> {
        Self::start_file_udp_with_profile(media_path, destination, OutputProfileId::default())
    }

    pub fn start_file_udp_with_profile(
        media_path: impl AsRef<Path>,
        destination: SocketAddr,
        output_profile: OutputProfileId,
    ) -> Result<Self, EngineError> {
        station_media_assets::validate_media(media_path.as_ref())
            .map_err(|error| EngineError::MediaRejected(format!("{error:?}")))?;
        let loudness_target = profile_loudness_target(output_profile.profile())?;
        Self::start_outputs_with_program(
            &[ConfiguredTransportOutput::udp(destination, loudness_target)],
            Some(media_path.as_ref()),
            output_profile,
            None,
            None,
            None,
        )
    }

    pub fn start_configured_outputs_with_profile(
        outputs: &[ConfiguredTransportOutput],
        output_profile: OutputProfileId,
    ) -> Result<Self, EngineError> {
        Self::start_configured_outputs_with_profile_and_cg_root(outputs, output_profile, None)
    }

    pub fn start_configured_outputs_with_profile_and_cg_root(
        outputs: &[ConfiguredTransportOutput],
        output_profile: OutputProfileId,
        cg_image_root: Option<&Path>,
    ) -> Result<Self, EngineError> {
        if outputs.is_empty() {
            return Err(EngineError::Pipeline(
                "at least one transport output is required".into(),
            ));
        }
        Self::start_outputs_with_program(outputs, None, output_profile, cg_image_root, None, None)
    }

    pub fn start_configured_outputs_with_profile_and_roots(
        outputs: &[ConfiguredTransportOutput],
        output_profile: OutputProfileId,
        cg_image_root: Option<&Path>,
        proof_artifact_root: Option<&Path>,
    ) -> Result<Self, EngineError> {
        if outputs.is_empty() {
            return Err(EngineError::Pipeline(
                "at least one transport output is required".into(),
            ));
        }
        Self::start_outputs_with_program(
            outputs,
            None,
            output_profile,
            cg_image_root,
            proof_artifact_root,
            None,
        )
    }

    pub fn start_configured_outputs_with_profile_roots_and_fallback_slate(
        outputs: &[ConfiguredTransportOutput],
        output_profile: OutputProfileId,
        cg_image_root: Option<&Path>,
        proof_artifact_root: Option<&Path>,
        fallback_slate: Option<&WorkerFallbackSlate>,
    ) -> Result<Self, EngineError> {
        if outputs.is_empty() {
            return Err(EngineError::Pipeline(
                "at least one transport output is required".into(),
            ));
        }
        Self::start_outputs_with_program(
            outputs,
            None,
            output_profile,
            cg_image_root,
            proof_artifact_root,
            fallback_slate,
        )
    }

    fn start_outputs_with_program(
        outputs: &[ConfiguredTransportOutput],
        media_path: Option<&Path>,
        output_profile: OutputProfileId,
        cg_image_root: Option<&Path>,
        proof_artifact_root: Option<&Path>,
        fallback_slate: Option<&WorkerFallbackSlate>,
    ) -> Result<Self, EngineError> {
        if !output_profile.is_runnable() {
            return Err(EngineError::ProfileNotRunnable {
                profile_id: output_profile.storage_name().into(),
            });
        }
        let profile = output_profile.profile();
        let variant_contracts = output_variant_contracts(outputs, profile)?;
        let startup_started = Instant::now();
        gst::init().map_err(|error| EngineError::Initialization(error.to_string()))?;
        let proof_artifact_root = proof_artifact_root
            .map(validate_proof_artifact_root)
            .transpose()?;
        let fallback_pixbuf = fallback_slate
            .map(|slate| {
                let root = cg_image_root.ok_or_else(|| {
                    EngineError::CgInvalid(
                        "a fallback slate requires the managed CG image root".into(),
                    )
                })?;
                load_managed_cg_image(root, &slate.asset_id, slate.format)
            })
            .transpose()?;

        let pipeline = gst::Pipeline::with_name("townlight-synthetic-playout");
        let fallback = gst::ElementFactory::make("videotestsrc")
            .name("fallback-source")
            .property("is-live", true)
            .property_from_str("pattern", "black")
            .build()
            .map_err(|error| build_error("videotestsrc", error))?;
        let fallback_convert = element("videoconvert", "fallback-video-convert")?;
        let fallback_overlay = gst::ElementFactory::make("gdkpixbufoverlay")
            .name("fallback-slate-overlay")
            .property("alpha", if fallback_pixbuf.is_some() { 1.0 } else { 0.0 })
            .build()
            .map_err(|error| build_error("gdkpixbufoverlay", error))?;
        if let Some(pixbuf) = fallback_pixbuf.as_ref() {
            let (x, y, width, height) = fallback_slate_geometry(
                profile.width,
                profile.height,
                u32::try_from(pixbuf.width()).map_err(|_| {
                    EngineError::CgInvalid("fallback slate width is invalid".into())
                })?,
                u32::try_from(pixbuf.height()).map_err(|_| {
                    EngineError::CgInvalid("fallback slate height is invalid".into())
                })?,
            )?;
            fallback_overlay.set_property("pixbuf", pixbuf);
            fallback_overlay.set_property(
                "offset-x",
                i32::try_from(x).map_err(|_| {
                    EngineError::CgInvalid("fallback slate x offset exceeds range".into())
                })?,
            );
            fallback_overlay.set_property(
                "offset-y",
                i32::try_from(y).map_err(|_| {
                    EngineError::CgInvalid("fallback slate y offset exceeds range".into())
                })?,
            );
            fallback_overlay.set_property(
                "overlay-width",
                i32::try_from(width).map_err(|_| {
                    EngineError::CgInvalid("fallback slate width exceeds range".into())
                })?,
            );
            fallback_overlay.set_property(
                "overlay-height",
                i32::try_from(height).map_err(|_| {
                    EngineError::CgInvalid("fallback slate height exceeds range".into())
                })?,
            );
        }
        let fallback_queue = element("queue", "fallback-queue")?;
        let program = gst::ElementFactory::make("videotestsrc")
            .name("program-source")
            .property("is-live", true)
            .property_from_str("pattern", "smpte")
            .build()
            .map_err(|error| build_error("videotestsrc", error))?;
        let program_queue = element("queue", "program-queue")?;
        let video_selector = selector("video-source-selector")?;
        let video_timeline = gst::ElementFactory::make("identity")
            .name("continuous-video-timeline")
            .property("single-segment", true)
            .build()
            .map_err(|error| build_error("identity", error))?;
        let video_rate = gst::ElementFactory::make("videorate")
            .name("canonical-video-rate")
            .property("skip-to-first", true)
            .build()
            .map_err(|error| build_error("videorate", error))?;
        let convert = element("videoconvert", "output-convert")?;
        let caps = video_caps(profile)?;
        let caps_filter = gst::ElementFactory::make("capsfilter")
            .name("canonical-video")
            .property("caps", &caps)
            .build()
            .map_err(|error| build_error("capsfilter", error))?;
        let encoder = gst::ElementFactory::make("openh264enc")
            .name("video-encoder")
            .property("bitrate", profile.video_bitrate_bps)
            .property("max-bitrate", profile.video_bitrate_bps)
            .property("gop-size", two_second_gop(profile))
            .property("enable-frame-skip", false)
            .property_from_str("rate-control", "bitrate")
            .build()
            .map_err(|error| build_error("openh264enc", error))?;
        let parser = gst::ElementFactory::make("h264parse")
            .name("video-parser")
            .property("config-interval", 1_i32)
            .build()
            .map_err(|error| build_error("h264parse", error))?;
        let caption_source = gst::ElementFactory::make("appsrc")
            .name("cea708-caption-source")
            .property(
                "caps",
                gst::Caps::builder("text/x-raw")
                    .field("format", "utf8")
                    .build(),
            )
            .property("is-live", true)
            .property("format", gst::Format::Time)
            .property("block", false)
            .build()
            .map_err(|error| build_error("appsrc", error))?;
        let caption_encoder = gst::ElementFactory::make("tttocea708")
            .name("cea708-caption-encoder")
            .property_from_str("mode", "pop-on")
            .property("service-number", 1_u32)
            .property("cea608-channel", 1_u32)
            .build()
            .map_err(|error| build_error("tttocea708", error))?;
        let caption_caps = gst::ElementFactory::make("capsfilter")
            .name("cea708-caption-caps")
            .property(
                "caps",
                gst::Caps::builder("closedcaption/x-cea-708")
                    .field("format", "cc_data")
                    .field(
                        "framerate",
                        gst::Fraction::new(
                            i32::try_from(profile.frame_rate_numerator).map_err(|_| {
                                EngineError::CaptionInvalid(
                                    "caption frame-rate numerator exceeds range".into(),
                                )
                            })?,
                            i32::try_from(profile.frame_rate_denominator).map_err(|_| {
                                EngineError::CaptionInvalid(
                                    "caption frame-rate denominator exceeds range".into(),
                                )
                            })?,
                        ),
                    )
                    .build(),
            )
            .build()
            .map_err(|error| build_error("capsfilter", error))?;
        let caption_queue = bounded_queue("cea708-caption-queue")?;
        let caption_combiner = gst::ElementFactory::make("cccombiner")
            .name("cea708-caption-combiner")
            .property("schedule", true)
            .property("output-padding", true)
            .build()
            .map_err(|error| build_error("cccombiner", error))?;
        let caption_inserter = gst::ElementFactory::make("h264ccinserter")
            .name("cea708-h264-inserter")
            .property("remove-caption-meta", true)
            .build()
            .map_err(|error| build_error("h264ccinserter", error))?;
        let post_caption_parser = gst::ElementFactory::make("h264parse")
            .name("post-caption-video-parser")
            .property("config-interval", 1_i32)
            .build()
            .map_err(|error| build_error("h264parse", error))?;
        let cg_input_convert = element("videoconvert", "cg-input-convert")?;
        let cg_input_caps = gst::ElementFactory::make("capsfilter")
            .name("cg-input-caps")
            .property("caps", &cg_video_caps(profile)?)
            .build()
            .map_err(|error| build_error("capsfilter", error))?;
        let cg_compositor = gst::ElementFactory::make("compositor")
            .name("cg-compositor")
            .property("ignore-inactive-pads", true)
            .build()
            .map_err(|error| build_error("compositor", error))?;
        pipeline
            .add_many([&cg_input_convert, &cg_input_caps, &cg_compositor])
            .map_err(|error| EngineError::Pipeline(error.to_string()))?;
        let cg_base_pad = connect_to_compositor(&cg_input_caps, &cg_compositor, 0)?;
        cg_base_pad.set_property("alpha", 1.0_f64);
        let fallback_audio = gst::ElementFactory::make("audiotestsrc")
            .name("fallback-audio-source")
            .property("is-live", true)
            .property_from_str("wave", "silence")
            .build()
            .map_err(|error| build_error("audiotestsrc", error))?;
        let fallback_audio_convert = element("audioconvert", "fallback-audio-convert")?;
        let fallback_audio_resample = element("audioresample", "fallback-audio-resample")?;
        let fallback_audio_caps = audio_caps_filter("fallback-canonical-audio", profile)?;
        let fallback_audio_queue = element("queue", "fallback-audio-queue")?;
        let program_audio = gst::ElementFactory::make("audiotestsrc")
            .name("program-audio-source")
            .property("is-live", true)
            .property_from_str("wave", "sine")
            .property("freq", 1_000.0_f64)
            .property("volume", 0.10_f64)
            .build()
            .map_err(|error| build_error("audiotestsrc", error))?;
        let program_audio_convert = element("audioconvert", "program-audio-convert")?;
        let program_audio_resample = element("audioresample", "program-audio-resample")?;
        let program_audio_caps = audio_caps_filter("program-canonical-audio", profile)?;
        let program_audio_queue = element("queue", "program-audio-queue")?;
        let audio_selector = selector("audio-source-selector")?;
        let audio_timeline = gst::ElementFactory::make("identity")
            .name("continuous-audio-timeline")
            .property("single-segment", true)
            .build()
            .map_err(|error| build_error("identity", error))?;
        let audio_rate = gst::ElementFactory::make("audiorate")
            .name("canonical-audio-rate")
            .property("skip-to-first", true)
            .property("tolerance", AUDIO_TIMESTAMP_TOLERANCE_NS)
            .build()
            .map_err(|error| build_error("audiorate", error))?;
        let audio_output_queue = element("queue", "audio-output-queue")?;
        let audio_variant_fanout = element("tee", "audio-variant-fanout")?;
        let loudness_variants = variant_contracts
            .iter()
            .map(|contract| build_loudness_variant(*contract, profile))
            .collect::<Result<Vec<_>, _>>()?;
        let video_variant_fanout = element("tee", "video-variant-fanout")?;
        let mut output_branches = Vec::with_capacity(outputs.len());
        let mut output_counters = Vec::with_capacity(outputs.len());
        let mut output_audio_counters = Vec::with_capacity(outputs.len());
        let mut srt_sinks = Vec::new();
        for (index, output) in outputs.iter().enumerate() {
            let queue = element("queue", format!("transport-output-queue-{index}"))?;
            let capture_ingress = install_output_capture_ingress(&queue)?;
            let sink = match &output.transport {
                TransportOutput::Udp(destination) => gst::ElementFactory::make("udpsink")
                    .name(format!("udp-output-{index}"))
                    .property("host", destination.ip().to_string())
                    .property("port", i32::from(destination.port()))
                    .property("sync", false)
                    .property("async", false)
                    .build()
                    .map_err(|error| build_error("udpsink", error))?,
                TransportOutput::Srt { uri, passphrase } => {
                    let builder = gst::ElementFactory::make("srtsink")
                        .name(format!("srt-output-{index}"))
                        .property("uri", uri)
                        .property_from_str("mode", "caller")
                        .property("latency", 200_i32)
                        .property("wait-for-connection", false)
                        .property("auto-reconnect", true)
                        .property("sync", false)
                        .property("async", false);
                    let builder = if let Some(passphrase) = passphrase {
                        builder
                            .property("passphrase", passphrase)
                            .property_from_str("pbkeylen", "32")
                    } else {
                        builder
                    };
                    builder
                        .build()
                        .map_err(|error| build_error("srtsink", error))?
                }
            };
            match &output.transport {
                TransportOutput::Udp(_) => output_counters.push(OutputCounter::Udp(sink.clone())),
                TransportOutput::Srt { .. } => {
                    srt_sinks.push(sink.clone());
                    output_counters.push(OutputCounter::Srt(sink.clone()));
                }
            }
            let output_contract = effective_output_variant_contract(output, profile);
            let variant_index = variant_contracts
                .iter()
                .position(|contract| contract == &output_contract)
                .ok_or_else(|| EngineError::Pipeline("missing output variant".into()))?;
            output_audio_counters.push(Arc::clone(&loudness_variants[variant_index].audio_buffers));
            output_branches.push(OutputBranch {
                queue,
                sink,
                variant_index,
                capture_ingress,
            });
        }

        pipeline
            .add_many([
                &fallback,
                &fallback_convert,
                &fallback_overlay,
                &fallback_queue,
                &program,
                &program_queue,
                &video_selector,
                &video_timeline,
                &video_rate,
                &convert,
                &caps_filter,
                &encoder,
                &parser,
                &caption_source,
                &caption_encoder,
                &caption_caps,
                &caption_queue,
                &caption_combiner,
                &caption_inserter,
                &post_caption_parser,
                &fallback_audio,
                &fallback_audio_convert,
                &fallback_audio_resample,
                &fallback_audio_caps,
                &fallback_audio_queue,
                &program_audio,
                &program_audio_convert,
                &program_audio_resample,
                &program_audio_caps,
                &program_audio_queue,
                &audio_selector,
                &audio_timeline,
                &audio_rate,
                &audio_output_queue,
                &audio_variant_fanout,
                &video_variant_fanout,
            ])
            .map_err(|error| EngineError::Pipeline(error.to_string()))?;
        for branch in &output_branches {
            pipeline
                .add_many([&branch.queue, &branch.sink])
                .map_err(|error| EngineError::Pipeline(error.to_string()))?;
        }
        for variant in &loudness_variants {
            pipeline
                .add_many([
                    &variant.video_queue,
                    &variant.audio_queue,
                    &variant.audio_convert_in,
                    &variant.audio_resample_in,
                    &variant.audio_input_caps,
                    &variant.normalizer,
                    &variant.audio_convert_out,
                    &variant.audio_resample_out,
                    &variant.audio_output_caps,
                    &variant.audio_encoder,
                    &variant.audio_parser,
                    &variant.mux,
                    &variant.transport_fanout,
                ])
                .map_err(|error| EngineError::Pipeline(error.to_string()))?;
        }
        gst::Element::link_many([
            &fallback,
            &fallback_convert,
            &fallback_overlay,
            &fallback_queue,
        ])
        .map_err(|_| EngineError::Link {
            from: "fallback-source",
            to: "fallback-queue",
        })?;
        link(&program, &program_queue, "videotestsrc", "queue")?;
        gst::Element::link_many([
            &fallback_audio,
            &fallback_audio_convert,
            &fallback_audio_resample,
            &fallback_audio_caps,
            &fallback_audio_queue,
        ])
        .map_err(|_| EngineError::Link {
            from: "audiotestsrc",
            to: "fallback-audio-queue",
        })?;
        gst::Element::link_many([
            &program_audio,
            &program_audio_convert,
            &program_audio_resample,
            &program_audio_caps,
            &program_audio_queue,
        ])
        .map_err(|_| EngineError::Link {
            from: "audiotestsrc",
            to: "program-audio-queue",
        })?;
        gst::Element::link_many([
            &video_selector,
            &video_timeline,
            &video_rate,
            &cg_input_convert,
            &cg_input_caps,
        ])
        .map_err(|_| EngineError::Link {
            from: "input-selector",
            to: "cg-input-caps",
        })?;
        gst::Element::link_many([
            &cg_compositor,
            &convert,
            &caps_filter,
            &encoder,
            &parser,
            &caption_combiner,
            &caption_inserter,
            &post_caption_parser,
            &video_variant_fanout,
        ])
        .map_err(|_| EngineError::Link {
            from: "cg-compositor",
            to: "video-variant-fanout",
        })?;
        gst::Element::link_many([
            &caption_source,
            &caption_encoder,
            &caption_caps,
            &caption_queue,
        ])
        .map_err(|_| EngineError::Link {
            from: "appsrc",
            to: "tttocea708",
        })?;
        let caption_source_pad =
            caption_queue
                .static_pad("src")
                .ok_or(EngineError::MissingPad {
                    element: "queue",
                    pad: "src",
                })?;
        let caption_sink_pad =
            caption_combiner
                .request_pad_simple("caption")
                .ok_or(EngineError::MissingPad {
                    element: "cccombiner",
                    pad: "caption",
                })?;
        caption_source_pad
            .link(&caption_sink_pad)
            .map_err(|error| EngineError::PadLink(format!("{error:?}")))?;
        for variant in &loudness_variants {
            gst::Element::link_many([&video_variant_fanout, &variant.video_queue, &variant.mux])
                .map_err(|_| EngineError::Link {
                    from: "video-variant-fanout",
                    to: "mpegtsmux",
                })?;
            gst::Element::link_many([
                &audio_variant_fanout,
                &variant.audio_queue,
                &variant.audio_convert_in,
                &variant.audio_resample_in,
                &variant.audio_input_caps,
                &variant.normalizer,
                &variant.audio_convert_out,
                &variant.audio_resample_out,
                &variant.audio_output_caps,
                &variant.audio_encoder,
                &variant.audio_parser,
                &variant.mux,
            ])
            .map_err(|_| EngineError::Link {
                from: "audio-variant-fanout",
                to: "mpegtsmux",
            })?;
            gst::Element::link_many([&variant.mux, &variant.transport_fanout]).map_err(|_| {
                EngineError::Link {
                    from: "mpegtsmux",
                    to: "transport-fanout",
                }
            })?;
        }
        gst::Element::link_many([
            &audio_selector,
            &audio_timeline,
            &audio_rate,
            &audio_output_queue,
            &audio_variant_fanout,
        ])
        .map_err(|_| EngineError::Link {
            from: "input-selector",
            to: "audio-variant-fanout",
        })?;
        for branch in &output_branches {
            let variant = &loudness_variants[branch.variant_index];
            gst::Element::link_many([&variant.transport_fanout, &branch.queue, &branch.sink])
                .map_err(|_| EngineError::Link {
                    from: "transport-fanout",
                    to: "transport-output",
                })?;
        }

        let fallback_pad = connect_source(&fallback_queue, &video_selector, "fallback-queue")?;
        let synthetic_program_pad =
            connect_source(&program_queue, &video_selector, "program-queue")?;
        let audio_fallback_pad = connect_source(
            &fallback_audio_queue,
            &audio_selector,
            "fallback-audio-queue",
        )?;
        let synthetic_audio_program_pad =
            connect_source(&program_audio_queue, &audio_selector, "program-audio-queue")?;
        let mut file_program = media_path
            .map(|path| {
                install_file_program(&pipeline, path, &video_selector, &audio_selector, profile)
            })
            .transpose()?;
        let (program_pad, audio_program_pad) = file_program
            .as_ref()
            .map(|program| (program.video_pad.clone(), program.audio_pad.clone()))
            .unwrap_or((synthetic_program_pad, synthetic_audio_program_pad));
        video_selector.set_property("active-pad", Some(fallback_pad.clone()));
        audio_selector.set_property("active-pad", Some(audio_fallback_pad.clone()));
        let early_cg_branch = if srt_sinks.is_empty() {
            Some(install_cg_branch(&pipeline, &cg_compositor, profile)?)
        } else {
            None
        };
        let srt_deadline = startup_started + Duration::from_secs(5);
        pipeline
            .set_state(gst::State::Playing)
            .map_err(|error| EngineError::State(format!("{error:?}")))?;
        wait_for_srt_connections(&srt_sinks, &pipeline, srt_deadline)?;
        pipeline
            .state(gst::ClockTime::from_seconds(5))
            .0
            .map_err(|error| EngineError::State(format!("{error:?}")))?;
        if let Some(program) = &file_program {
            wait_for_file_decode(program, &pipeline, Duration::from_secs(5))?;
        }
        if let Some(program) = file_program.as_mut() {
            program.started = true;
        }

        let branch_result = match early_cg_branch {
            Some(branch) => Ok(branch),
            None => install_cg_branch(&pipeline, &cg_compositor, profile),
        };
        let (cg_slots, cg_canvas_pad) = match branch_result {
            Ok(branch) => branch,
            Err(error) => {
                let _ = pipeline.set_state(gst::State::Null);
                return Err(error);
            }
        };

        let output_capture_bindings = outputs
            .iter()
            .enumerate()
            .map(|(capture_index, output)| OutputCaptureBinding {
                sink_id: output.sink_id.clone(),
                capture_index,
                loudness_target: output.loudness_target,
            })
            .collect();
        let output_capture_ingresses = output_branches
            .iter()
            .map(|branch| Arc::clone(&branch.capture_ingress))
            .collect();
        let output_variant_count = loudness_variants.len();
        let playout = Self {
            pipeline,
            video_selector,
            video_fallback_pad: fallback_pad,
            video_program_pad: program_pad,
            audio_selector,
            audio_fallback_pad,
            audio_program_pad,
            output_counters,
            output_audio_counters,
            output_profile,
            file_program,
            live_program: None,
            cg_slots,
            cg_canvas_pad,
            cg_image_root: cg_image_root.map(Path::to_path_buf),
            cg_revision: None,
            output_variant_count,
            output_capture_bindings,
            output_capture_ingresses,
            proof_artifact_root,
            caption_source,
            stopped: false,
        };
        playout.cg_canvas_pad.set_property("alpha", 0.0_f64);
        playout.wait_for_source(SourceRole::Fallback, Duration::from_secs(2))?;
        playout.wait_for_output_startup(Duration::from_secs(12))?;
        Ok(playout)
    }

    fn wait_for_output_startup(&self, timeout: Duration) -> Result<(), EngineError> {
        let deadline = Instant::now() + timeout;
        loop {
            let observations = self.output_health_observations()?;
            if observations.iter().all(|observation| {
                observation.bytes_sent_total > 0 && observation.audio_buffers_total > 0
            }) {
                return Ok(());
            }
            if Instant::now() >= deadline {
                return Err(EngineError::State(
                    "required output audio and transport did not start before the readiness deadline"
                        .into(),
                ));
            }
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    pub fn output_variant_count(&self) -> usize {
        self.output_variant_count
    }

    /// Schedules one bounded UTF-8 cue against the worker's running graph clock.
    /// The server supplies only a relative delay; browser and wall-clock values
    /// never become direct media timestamps.
    pub fn push_caption_cue(&mut self, cue: &CaptionCue) -> Result<(), EngineError> {
        if self.stopped {
            return Err(EngineError::CaptionPush(
                "caption cue requires a running graph".into(),
            ));
        }
        validate_caption_cue(cue)?;
        let running_time = self
            .pipeline
            .current_running_time()
            .ok_or_else(|| EngineError::CaptionPush("caption graph has no running time".into()))?;
        let pts = running_time
            .checked_add(gst::ClockTime::from_mseconds(cue.start_after_ms))
            .ok_or_else(|| EngineError::CaptionPush("caption timestamp overflowed".into()))?;
        let mut buffer = gst::Buffer::from_mut_slice(cue.text.as_bytes().to_vec());
        {
            let writable = buffer
                .get_mut()
                .ok_or_else(|| EngineError::CaptionPush("caption buffer is not writable".into()))?;
            writable.set_pts(pts);
            writable.set_duration(gst::ClockTime::from_mseconds(cue.duration_ms));
        }
        let flow = self
            .caption_source
            .emit_by_name::<gst::FlowReturn>("push-buffer", &[&buffer]);
        flow.into_result().map(|_| ()).map_err(|error| {
            EngineError::CaptionPush(format!("caption source rejected cue: {error:?}"))
        })
    }

    pub fn output_bytes_sent(&self) -> Result<Vec<u64>, EngineError> {
        self.output_health_observations().map(|observations| {
            observations
                .into_iter()
                .map(|observation| observation.bytes_sent_total)
                .collect()
        })
    }

    pub fn output_health_observations(&self) -> Result<Vec<OutputHealthObservation>, EngineError> {
        if let Some(message) = self
            .pipeline
            .bus()
            .and_then(|bus| bus.pop_filtered(&[gst::MessageType::Error]))
            && let gst::MessageView::Error(error) = message.view()
        {
            return Err(EngineError::State(format!(
                "output graph reported an error: {}",
                error.error()
            )));
        }
        self.output_counters
            .iter()
            .zip(&self.output_audio_counters)
            .map(|(counter, audio_buffers)| {
                let bytes_sent_total = match counter {
                    OutputCounter::Udp(sink) => sink.property::<u64>("bytes-served"),
                    OutputCounter::Srt(sink) => sink
                        .property::<gst::Structure>("stats")
                        .get::<u64>("bytes-sent-total")
                        .map_err(|error| {
                            EngineError::State(format!(
                                "could not read required SRT output statistics: {error}"
                            ))
                        })?,
                };
                Ok(OutputHealthObservation {
                    bytes_sent_total,
                    audio_buffers_total: audio_buffers.load(Ordering::Acquire),
                })
            })
            .collect()
    }

    pub fn start_output_capture(
        &mut self,
        request: OutputCaptureRequest,
    ) -> Result<OutputCaptureStarted, EngineError> {
        if self.stopped {
            return Err(EngineError::OutputCapture(
                "output capture requires a running graph".into(),
            ));
        }
        if !is_canonical_uuid(&request.proof_id) || !is_canonical_uuid(&request.sink_id) {
            return Err(EngineError::OutputCapture(
                "output capture identities must be canonical UUIDs".into(),
            ));
        }
        if !(MIN_OUTPUT_PROOF_DURATION_MS..=MAX_OUTPUT_PROOF_DURATION_MS)
            .contains(&request.duration_ms)
        {
            return Err(EngineError::OutputCapture(format!(
                "output capture duration must be between {MIN_OUTPUT_PROOF_DURATION_MS} and {MAX_OUTPUT_PROOF_DURATION_MS} milliseconds"
            )));
        }
        if !(1..=MAX_OUTPUT_PROOF_BYTES).contains(&request.max_bytes) {
            return Err(EngineError::OutputCapture(format!(
                "output capture byte limit must be between 1 and {MAX_OUTPUT_PROOF_BYTES}"
            )));
        }
        let output_index = usize::from(request.output_index);
        let binding = self
            .output_capture_bindings
            .get(output_index)
            .ok_or_else(|| {
                EngineError::OutputCapture("output capture index is not configured".into())
            })?;
        if binding.sink_id.as_deref() != Some(request.sink_id.as_str()) {
            return Err(EngineError::OutputCapture(
                "output capture sink does not match the configured output index".into(),
            ));
        }
        if binding.loudness_target != request.loudness_target {
            return Err(EngineError::OutputCapture(
                "output capture target does not match the configured output".into(),
            ));
        }
        let root = self.proof_artifact_root.as_ref().ok_or_else(|| {
            EngineError::OutputCapture("managed output proof root is unavailable".into())
        })?;
        self.output_capture_ingresses[binding.capture_index].begin(request, root)
    }

    pub fn poll_output_capture(
        &mut self,
        proof_id: &str,
    ) -> Result<OutputCaptureStatus, EngineError> {
        if !is_canonical_uuid(proof_id) {
            return Err(EngineError::OutputCapture(
                "output capture identity must be a canonical UUID".into(),
            ));
        }
        for ingress in &self.output_capture_ingresses {
            if let Some(status) = ingress.status_for(proof_id)? {
                return Ok(status);
            }
        }
        Err(EngineError::OutputCapture(
            "output capture identity is not active in this graph".into(),
        ))
    }

    pub fn abort_output_capture(&mut self, proof_id: &str) -> Result<(), EngineError> {
        for ingress in &self.output_capture_ingresses {
            if ingress.abort_if_matches(
                proof_id,
                "capture_aborted",
                "output capture was aborted",
            )? {
                return Ok(());
            }
        }
        Err(EngineError::OutputCapture(
            "output capture identity is not active in this graph".into(),
        ))
    }

    pub fn apply_cg_snapshot(
        &mut self,
        snapshot: &station_media_protocol::CgSnapshot,
    ) -> Result<(), EngineError> {
        validate_cg_snapshot(snapshot)?;
        if self.cg_revision.as_ref() == Some(snapshot) {
            return Ok(());
        }
        let canvas_pad = &self.cg_canvas_pad;
        let mut overlays = snapshot.overlays.iter().collect::<Vec<_>>();
        overlays.sort_by_key(|overlay| (overlay.z_index, overlay.slot));
        let mut prepared_images = Vec::new();
        for overlay in &overlays {
            if let CgOverlayContent::Image { asset_id, format } = &overlay.content {
                let root = self.cg_image_root.as_deref().ok_or_else(|| {
                    EngineError::CgInvalid("managed CG image root is unavailable".into())
                })?;
                prepared_images.push((
                    overlay.slot,
                    load_managed_cg_image(root, asset_id, *format)?,
                ));
            }
        }
        let mut image_commits = Vec::new();
        for slot in &self.cg_slots {
            slot.text_overlay.set_property("silent", true);
            slot.clock_overlay.set_property("silent", true);
        }
        for (physical_slot, overlay) in overlays.into_iter().enumerate() {
            let slot = &self.cg_slots[physical_slot];
            match &overlay.content {
                CgOverlayContent::Text { text } => {
                    configure_overlay_geometry(&slot.text_overlay, &overlay.rect)?;
                    configure_overlay_geometry(&slot.clock_overlay, &overlay.rect)?;
                    for image in &slot.image_overlays {
                        image.set_property("alpha", 0.0_f64);
                    }
                    slot.text_overlay.set_property("text", sanitize_text(text)?);
                    slot.text_overlay.set_property("silent", false);
                    slot.clock_overlay.set_property("silent", true);
                }
                CgOverlayContent::Clock { format } => {
                    configure_overlay_geometry(&slot.text_overlay, &overlay.rect)?;
                    configure_overlay_geometry(&slot.clock_overlay, &overlay.rect)?;
                    for image in &slot.image_overlays {
                        image.set_property("alpha", 0.0_f64);
                    }
                    slot.clock_overlay
                        .set_property("time-format", sanitize_text(format)?);
                    slot.text_overlay.set_property("silent", true);
                    slot.clock_overlay.set_property("silent", false);
                }
                CgOverlayContent::Image { .. } => {
                    let (_, pixbuf) = prepared_images
                        .iter()
                        .find(|(slot, _)| *slot == overlay.slot)
                        .expect("image was prepared before mutation");
                    let bank = slot
                        .active_image
                        .as_ref()
                        .map_or(0, |(active, _)| 1 - *active);
                    configure_image_geometry(
                        &slot.image_overlays[bank],
                        &overlay.rect,
                        self.output_profile.profile(),
                    )?;
                    slot.image_overlays[bank].set_property("pixbuf", pixbuf);
                    image_commits.push((physical_slot, bank, overlay_asset_id(overlay)?));
                }
            }
        }
        for (physical_slot, bank, asset_id) in image_commits {
            let slot = &mut self.cg_slots[physical_slot];
            if let Some((active, _)) = slot.active_image {
                slot.image_overlays[active].set_property("alpha", 0.0_f64);
            }
            slot.image_overlays[bank].set_property("alpha", 1.0_f64);
            slot.active_image = Some((bank, asset_id));
        }
        for slot in self.cg_slots.iter().skip(snapshot.overlays.len()) {
            for image in &slot.image_overlays {
                image.set_property("alpha", 0.0_f64);
            }
        }
        canvas_pad.set_property("zorder", 1_u32);
        if !snapshot.overlays.is_empty() {
            canvas_pad.set_property("alpha", 1.0_f64);
        } else {
            canvas_pad.set_property("alpha", 0.0_f64);
        }
        self.cg_revision = Some(snapshot.clone());
        Ok(())
    }

    pub fn clear_cg_snapshot(&mut self) {
        for slot in &self.cg_slots {
            slot.text_overlay.set_property("silent", true);
            slot.clock_overlay.set_property("silent", true);
        }
        self.cg_canvas_pad.set_property("alpha", 0.0_f64);
        self.cg_revision = None;
    }

    pub fn current_cg_revision(&self) -> Option<(&str, u64)> {
        self.cg_revision
            .as_ref()
            .map(|snapshot| (snapshot.board_id.as_str(), snapshot.board_revision))
    }

    pub fn current_cg_snapshot(&self) -> Option<&CgSnapshot> {
        self.cg_revision.as_ref()
    }

    pub fn active_source(&self) -> Result<SourceRole, EngineError> {
        let video = active_role(
            &self.video_selector,
            &self.video_fallback_pad,
            &self.video_program_pad,
            self.live_program.as_ref().map(|program| &program.video_pad),
        )?;
        let audio = active_role(
            &self.audio_selector,
            &self.audio_fallback_pad,
            &self.audio_program_pad,
            self.live_program.as_ref().map(|program| &program.audio_pad),
        )?;
        if video == audio {
            Ok(video)
        } else {
            Err(EngineError::SourceSelectorsDisagree { video, audio })
        }
    }

    pub fn select(&mut self, role: SourceRole) -> Result<(), EngineError> {
        if role == SourceRole::Program
            && self
                .file_program
                .as_ref()
                .is_some_and(|program| !program.started)
        {
            self.start_prepared_program(0)?;
        }
        if role == SourceRole::Live {
            let before = self.live_buffer_counts().ok_or(EngineError::LiveNotArmed)?;
            self.wait_for_live_activity(before, Duration::from_secs(2))?;
        }
        let previous_video: Option<gst::Pad> = self.video_selector.property("active-pad");
        let previous_audio: Option<gst::Pad> = self.audio_selector.property("active-pad");
        let (video_pad, audio_pad) = match role {
            SourceRole::Fallback => (&self.video_fallback_pad, &self.audio_fallback_pad),
            SourceRole::Program => (&self.video_program_pad, &self.audio_program_pad),
            SourceRole::Live => {
                let program = self
                    .live_program
                    .as_ref()
                    .ok_or(EngineError::LiveNotArmed)?;
                (&program.video_pad, &program.audio_pad)
            }
        };
        self.set_active_pads(video_pad, audio_pad)?;
        if let Err(error) = self.wait_for_source(role, Duration::from_secs(2)) {
            self.restore_active_pads(previous_video, previous_audio);
            return Err(error);
        }
        Ok(())
    }

    pub fn load_file(&mut self, media_path: impl AsRef<Path>) -> Result<(), EngineError> {
        self.load_validated_file(media_path)
    }

    /// Starts the prepared program at an explicit media position and takes it
    /// to air. The prepared decoder remains parked until this call, so an
    /// early load cannot consume a future schedule item off air.
    pub fn take_program_at(&mut self, media_offset_ms: u64) -> Result<(), EngineError> {
        self.start_prepared_program(media_offset_ms)?;
        self.select(SourceRole::Program)
    }

    /// Arms a typed UDP MPEG-TS input without taking it to air. The branch is
    /// admitted only after both decoded A/V legs have produced buffers; any
    /// failure tears down only the candidate branch and leaves the existing
    /// fallback/program/live selection untouched.
    pub fn arm_live_udp(&mut self, source: SocketAddr) -> Result<(), EngineError> {
        let candidate = install_live_program(
            &self.pipeline,
            source,
            &self.video_selector,
            &self.audio_selector,
            self.output_profile.profile(),
        )?;
        for element in &candidate.elements {
            if let Err(error) = element.sync_state_with_parent() {
                remove_live_program(
                    &self.pipeline,
                    &self.video_selector,
                    &self.audio_selector,
                    &candidate,
                );
                return Err(EngineError::State(error.to_string()));
            }
        }
        if let Err(error) = wait_for_live_decode(&candidate, Duration::from_secs(5)) {
            remove_live_program(
                &self.pipeline,
                &self.video_selector,
                &self.audio_selector,
                &candidate,
            );
            return Err(error);
        }

        let was_live = match self.active_source() {
            Ok(role) => role == SourceRole::Live,
            Err(error) => {
                cleanup_live_install(
                    &self.pipeline,
                    &self.video_selector,
                    &self.audio_selector,
                    LiveInstallParts::from_program(&candidate),
                );
                return Err(error);
            }
        };
        let candidate_video_pad = candidate.video_pad.clone();
        let candidate_audio_pad = candidate.audio_pad.clone();
        let previous = self.live_program.replace(candidate);
        if was_live
            && let Err(error) = self.set_active_pads(&candidate_video_pad, &candidate_audio_pad)
        {
            let failed = self
                .live_program
                .take()
                .expect("candidate live branch is installed");
            self.live_program = previous;
            remove_live_program(
                &self.pipeline,
                &self.video_selector,
                &self.audio_selector,
                &failed,
            );
            return Err(error);
        }
        if let Some(previous) = previous {
            remove_live_program(
                &self.pipeline,
                &self.video_selector,
                &self.audio_selector,
                &previous,
            );
        }
        Ok(())
    }

    /// Removes the armed live branch. The caller must take fallback or
    /// program first so removing a selected pad can never blank the output.
    pub fn disarm_live(&mut self) -> Result<(), EngineError> {
        if self.active_source()? == SourceRole::Live {
            return Err(EngineError::LiveActive);
        }
        if let Some(program) = self.live_program.take() {
            remove_live_program(
                &self.pipeline,
                &self.video_selector,
                &self.audio_selector,
                &program,
            );
        }
        Ok(())
    }

    /// Returns decoded live video/audio buffer counts for worker health and
    /// take evidence. Counts are monotonic for one armed branch.
    pub fn live_buffer_counts(&self) -> Option<(u64, u64)> {
        self.live_program.as_ref().map(|program| {
            (
                program.video_buffers.load(Ordering::Acquire),
                program.audio_buffers.load(Ordering::Acquire),
            )
        })
    }

    pub fn load_asset(
        &mut self,
        asset_id: &str,
        media_path: impl AsRef<Path>,
    ) -> Result<(), EngineError> {
        station_media_assets::verify_media_identity(media_path.as_ref(), asset_id)
            .map_err(|error| EngineError::MediaRejected(format!("{error:?}")))?;
        self.load_validated_file(media_path)
    }

    fn load_validated_file(&mut self, media_path: impl AsRef<Path>) -> Result<(), EngineError> {
        if self.active_source()? == SourceRole::Program {
            return Err(EngineError::LoadRequiresFallback);
        }
        station_media_assets::validate_media(media_path.as_ref())
            .map_err(|error| EngineError::MediaRejected(format!("{error:?}")))?;
        let program = install_file_program(
            &self.pipeline,
            media_path.as_ref(),
            &self.video_selector,
            &self.audio_selector,
            self.output_profile.profile(),
        )?;
        for element in &program.elements {
            if let Err(error) = element.set_state(gst::State::Paused) {
                remove_file_program(
                    &self.pipeline,
                    &self.video_selector,
                    &self.audio_selector,
                    &program,
                );
                return Err(EngineError::State(error.to_string()));
            }
        }
        if let Err(error) = wait_for_file_decode(&program, &self.pipeline, Duration::from_secs(5)) {
            remove_file_program(
                &self.pipeline,
                &self.video_selector,
                &self.audio_selector,
                &program,
            );
            return Err(error);
        }
        self.video_program_pad = program.video_pad.clone();
        self.audio_program_pad = program.audio_pad.clone();
        if let Some(previous) = self.file_program.replace(program) {
            remove_file_program(
                &self.pipeline,
                &self.video_selector,
                &self.audio_selector,
                &previous,
            );
        }
        Ok(())
    }

    fn start_prepared_program(&mut self, media_offset_ms: u64) -> Result<(), EngineError> {
        let active_source = self.active_source()?;
        let program = self.file_program.as_mut().ok_or_else(|| {
            EngineError::State("no validated program is prepared for take".into())
        })?;
        if program.started {
            return if active_source == SourceRole::Live {
                Ok(())
            } else {
                Err(EngineError::ProgramAlreadyStarted)
            };
        }
        let media_offset = gst::ClockTime::from_mseconds(media_offset_ms);
        program
            .decoder
            .seek_simple(
                gst::SeekFlags::FLUSH | gst::SeekFlags::ACCURATE,
                media_offset,
            )
            .map_err(|error| EngineError::State(format!("program seek failed: {error}")))?;
        let running_time = self
            .pipeline
            .current_running_time()
            .ok_or_else(|| EngineError::State("the running pipeline has no clock time".into()))?;
        let timeline_offset = i64::try_from(running_time.nseconds())
            .map_err(|_| EngineError::State("program timeline offset exceeded i64".into()))?;
        program.video_source_pad.set_offset(timeline_offset);
        program.audio_source_pad.set_offset(timeline_offset);
        for element in &program.elements {
            if let Err(error) = element.sync_state_with_parent() {
                for candidate in &program.elements {
                    let _ = candidate.set_state(gst::State::Paused);
                }
                program.video_source_pad.set_offset(0);
                program.audio_source_pad.set_offset(0);
                return Err(EngineError::State(error.to_string()));
            }
        }
        program.started = true;
        Ok(())
    }

    pub fn stop(mut self) -> Result<(), EngineError> {
        let capture_result = self.abort_all_output_captures(Duration::from_secs(2));
        self.stop_pipeline()?;
        self.stopped = true;
        capture_result
    }

    fn stop_pipeline(&self) -> Result<(), EngineError> {
        self.pipeline
            .set_state(gst::State::Null)
            .map_err(|error| EngineError::State(format!("{error:?}")))?;
        self.pipeline
            .state(gst::ClockTime::from_seconds(5))
            .0
            .map(|_| ())
            .map_err(|error| EngineError::State(format!("{error:?}")))
    }

    fn abort_all_output_captures(&mut self, timeout: Duration) -> Result<(), EngineError> {
        let deadline = Instant::now() + timeout;
        for ingress in &self.output_capture_ingresses {
            ingress.abort_any("graph_stopped", "output graph stopped during capture")?;
        }
        for ingress in &self.output_capture_ingresses {
            ingress.join_finished_by(deadline)?;
        }
        Ok(())
    }

    fn wait_for_source(&self, requested: SourceRole, timeout: Duration) -> Result<(), EngineError> {
        let started = Instant::now();
        loop {
            if self.active_source().ok() == Some(requested) {
                return Ok(());
            }
            if started.elapsed() >= timeout {
                return Err(EngineError::SwitchTimedOut {
                    requested,
                    actual: self.active_source().ok(),
                });
            }
            std::thread::sleep(Duration::from_millis(2));
        }
    }

    fn set_active_pads(
        &self,
        video_pad: &gst::Pad,
        audio_pad: &gst::Pad,
    ) -> Result<(), EngineError> {
        let previous_video: Option<gst::Pad> = self.video_selector.property("active-pad");
        let previous_audio: Option<gst::Pad> = self.audio_selector.property("active-pad");
        self.video_selector
            .set_property("active-pad", Some(video_pad.clone()));
        self.audio_selector
            .set_property("active-pad", Some(audio_pad.clone()));
        let started = Instant::now();
        let result = loop {
            let video = active_role(
                &self.video_selector,
                &self.video_fallback_pad,
                &self.video_program_pad,
                self.live_program.as_ref().map(|program| &program.video_pad),
            );
            let audio = active_role(
                &self.audio_selector,
                &self.audio_fallback_pad,
                &self.audio_program_pad,
                self.live_program.as_ref().map(|program| &program.audio_pad),
            );
            match (video, audio) {
                (Ok(video), Ok(audio)) if video == audio => break Ok(()),
                (Ok(video), Ok(audio)) if started.elapsed() >= Duration::from_secs(2) => {
                    break Err(EngineError::SourceSelectorsDisagree { video, audio });
                }
                (Err(error), _) | (_, Err(error))
                    if started.elapsed() >= Duration::from_secs(2) =>
                {
                    break Err(error);
                }
                _ => std::thread::sleep(Duration::from_millis(2)),
            }
        };
        if result.is_err() {
            self.video_selector
                .set_property("active-pad", previous_video);
            self.audio_selector
                .set_property("active-pad", previous_audio);
        }
        result
    }

    fn restore_active_pads(&self, video: Option<gst::Pad>, audio: Option<gst::Pad>) {
        self.video_selector.set_property("active-pad", video);
        self.audio_selector.set_property("active-pad", audio);
    }

    fn wait_for_live_activity(
        &self,
        before: (u64, u64),
        timeout: Duration,
    ) -> Result<(), EngineError> {
        let started = Instant::now();
        loop {
            let current = self.live_buffer_counts().unwrap_or((0, 0));
            if current.0 > before.0 && current.1 > before.1 {
                return Ok(());
            }
            if started.elapsed() >= timeout {
                return Err(EngineError::LiveInputStalled {
                    video_buffers: current.0,
                    audio_buffers: current.1,
                });
            }
            std::thread::sleep(Duration::from_millis(2));
        }
    }
}

fn install_cg_branch(
    pipeline: &gst::Pipeline,
    compositor: &gst::Element,
    profile: &OutputProfile,
) -> Result<(Vec<CgSlot>, gst::Pad), EngineError> {
    let canvas_source = gst::ElementFactory::make("videotestsrc")
        .name("cg-canvas-source")
        .property("is-live", true)
        .property_from_str("pattern", "solid-color")
        .property("foreground-color", 0_u32)
        .build()
        .map_err(|error| build_error("videotestsrc", error))?;
    let canvas_caps = gst::ElementFactory::make("capsfilter")
        .name("cg-canvas-caps")
        .property("caps", &cg_video_caps(profile)?)
        .build()
        .map_err(|error| build_error("capsfilter", error))?;
    let mut elements = vec![canvas_source.clone(), canvas_caps.clone()];
    pipeline
        .add_many([&canvas_source, &canvas_caps])
        .map_err(|error| EngineError::Pipeline(error.to_string()))?;
    link(
        &canvas_source,
        &canvas_caps,
        "cg-canvas-source",
        "cg-canvas-caps",
    )?;
    let mut previous = canvas_caps.clone();
    let mut slots = Vec::with_capacity(MAX_CG_SLOTS);
    for slot in 0..MAX_CG_SLOTS {
        let image_a = gst::ElementFactory::make("gdkpixbufoverlay")
            .name(format!("cg-image-overlay-{slot}-a"))
            .property("alpha", 0.0_f64)
            .build()
            .map_err(|error| build_error("gdkpixbufoverlay", error))?;
        let image_b = gst::ElementFactory::make("gdkpixbufoverlay")
            .name(format!("cg-image-overlay-{slot}-b"))
            .property("alpha", 0.0_f64)
            .build()
            .map_err(|error| build_error("gdkpixbufoverlay", error))?;
        let image_overlays = [image_a.clone(), image_b.clone()];
        let text_overlay = gst::ElementFactory::make("textoverlay")
            .name(format!("cg-text-overlay-{slot}"))
            .property("text", "")
            .property("silent", true)
            .property("shaded-background", false)
            .property_from_str("halignment", "position")
            .property_from_str("valignment", "position")
            .build()
            .map_err(|error| build_error("textoverlay", error))?;
        let clock_overlay = gst::ElementFactory::make("clockoverlay")
            .name(format!("cg-clock-overlay-{slot}"))
            .property("time-format", "%H:%M:%S")
            .property("silent", true)
            .property("shaded-background", false)
            .property_from_str("halignment", "position")
            .property_from_str("valignment", "position")
            .build()
            .map_err(|error| build_error("clockoverlay", error))?;
        pipeline
            .add_many([&image_a, &image_b, &text_overlay, &clock_overlay])
            .map_err(|error| EngineError::Pipeline(error.to_string()))?;
        link(&previous, &image_a, "cg-canvas", "cg-image-overlay")?;
        link(&image_a, &image_b, "cg-image-overlay", "cg-image-overlay")?;
        link(
            &image_b,
            &text_overlay,
            "cg-image-overlay",
            "cg-text-overlay",
        )?;
        link(
            &text_overlay,
            &clock_overlay,
            "cg-text-overlay",
            "cg-clock-overlay",
        )?;
        previous = clock_overlay.clone();
        elements.extend([
            image_a.clone(),
            image_b.clone(),
            text_overlay.clone(),
            clock_overlay.clone(),
        ]);
        slots.push(CgSlot {
            text_overlay,
            clock_overlay,
            image_overlays,
            active_image: None,
        });
    }
    let canvas_alpha = gst::ElementFactory::make("identity")
        .name("cg-canvas-key")
        .build()
        .map_err(|error| build_error("identity", error))?;
    let canvas_queue = element("queue", "cg-canvas-queue")?;
    pipeline
        .add_many([&canvas_alpha, &canvas_queue])
        .map_err(|error| EngineError::Pipeline(error.to_string()))?;
    link(
        &previous,
        &canvas_alpha,
        "cg-clock-overlay",
        "cg-canvas-key",
    )?;
    link(
        &canvas_alpha,
        &canvas_queue,
        "cg-canvas-key",
        "cg-canvas-queue",
    )?;
    elements.extend([canvas_alpha, canvas_queue.clone()]);
    let canvas_pad = connect_to_compositor(&canvas_queue, compositor, 1)?;
    canvas_pad.set_property("zorder", 1_u32);
    canvas_pad.set_property("alpha", 0.0_f64);
    for element in elements.iter().rev() {
        element
            .sync_state_with_parent()
            .map_err(|error| EngineError::State(format!("CG branch sync failed: {error:?}")))?;
    }
    Ok((slots, canvas_pad))
}

fn wait_for_srt_connections(
    sinks: &[gst::Element],
    pipeline: &gst::Pipeline,
    deadline: Instant,
) -> Result<(), EngineError> {
    if sinks.is_empty() {
        return Ok(());
    }
    while sinks.iter().any(|sink| {
        sink.property::<gst::Structure>("stats")
            .get::<u64>("bytes-sent-total")
            .unwrap_or(0)
            == 0
    }) {
        if Instant::now() >= deadline {
            let _ = pipeline.set_state(gst::State::Null);
            return Err(EngineError::State(
                "required SRT output did not connect and send within the startup deadline".into(),
            ));
        }
        if let Some(message) = pipeline.bus().and_then(|bus| {
            bus.timed_pop_filtered(
                gst::ClockTime::from_mseconds(25),
                &[gst::MessageType::Error],
            )
        }) && let gst::MessageView::Error(error) = message.view()
        {
            let _ = pipeline.set_state(gst::State::Null);
            return Err(EngineError::State(format!(
                "SRT output failed before readiness: {}",
                error.error()
            )));
        }
    }
    if Instant::now() > deadline {
        let _ = pipeline.set_state(gst::State::Null);
        return Err(EngineError::State(
            "required SRT output did not connect and send within the startup deadline".into(),
        ));
    }
    Ok(())
}

struct LiveProgram {
    video_pad: gst::Pad,
    audio_pad: gst::Pad,
    video_ready: Arc<AtomicBool>,
    audio_ready: Arc<AtomicBool>,
    video_buffers: Arc<AtomicU64>,
    audio_buffers: Arc<AtomicU64>,
    video_source_pad: gst::Pad,
    audio_source_pad: gst::Pad,
    elements: Vec<gst::Element>,
}

fn install_live_program(
    pipeline: &gst::Pipeline,
    source: SocketAddr,
    video_selector: &gst::Element,
    audio_selector: &gst::Element,
    profile: &OutputProfile,
) -> Result<LiveProgram, EngineError> {
    let sequence = LIVE_PROGRAM_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    let name = |component: &str| format!("program-live-{component}-{sequence}");
    let source_element = gst::ElementFactory::make("udpsrc")
        .name(name("udp-source"))
        .property("address", source.ip().to_string())
        .property("port", i32::from(source.port()))
        .property(
            "caps",
            gst::Caps::builder("video/mpegts")
                .field("systemstream", true)
                .field("packetsize", 188_i32)
                .build(),
        )
        .build()
        .map_err(|error| build_error("udpsrc", error))?;
    let demux = gst::ElementFactory::make("tsdemux")
        .name(name("demux"))
        .build()
        .map_err(|error| build_error("tsdemux", error))?;

    let video_decode_queue = element("queue", name("video-decode-queue"))?;
    let video_parse = element("h264parse", name("video-parse"))?;
    let video_decode = element("openh264dec", name("video-decode"))?;
    let video_convert = element("videoconvert", name("video-convert"))?;
    let video_scale = element("videoscale", name("video-scale"))?;
    let video_rate = gst::ElementFactory::make("videorate")
        .name(name("video-rate"))
        .property("skip-to-first", true)
        .build()
        .map_err(|error| build_error("videorate", error))?;
    let video_caps = video_caps_filter(name("canonical-video"), profile)?;
    let video_clock = gst::ElementFactory::make("clocksync")
        .name(name("video-clock"))
        .property("sync-to-first", true)
        .build()
        .map_err(|error| build_error("clocksync", error))?;
    let video_output_queue = element("queue", name("video-output-queue"))?;

    let audio_decode_queue = element("queue", name("audio-decode-queue"))?;
    let audio_parse = element("aacparse", name("audio-parse"))?;
    let audio_decode = element("avdec_aac", name("audio-decode"))?;
    let audio_convert = element("audioconvert", name("audio-convert"))?;
    let audio_resample = element("audioresample", name("audio-resample"))?;
    let audio_caps = audio_caps_filter(name("canonical-audio"), profile)?;
    let audio_rate = gst::ElementFactory::make("audiorate")
        .name(name("audio-rate"))
        .property("skip-to-first", true)
        .property("tolerance", AUDIO_TIMESTAMP_TOLERANCE_NS)
        .build()
        .map_err(|error| build_error("audiorate", error))?;
    let audio_clock = gst::ElementFactory::make("clocksync")
        .name(name("audio-clock"))
        .property("sync-to-first", true)
        .build()
        .map_err(|error| build_error("clocksync", error))?;
    let audio_output_queue = element("queue", name("audio-output-queue"))?;

    let elements = vec![
        source_element.clone(),
        demux.clone(),
        video_decode_queue.clone(),
        video_parse.clone(),
        video_decode.clone(),
        video_convert.clone(),
        video_scale.clone(),
        video_rate.clone(),
        video_caps.clone(),
        video_clock.clone(),
        video_output_queue.clone(),
        audio_decode_queue.clone(),
        audio_parse.clone(),
        audio_decode.clone(),
        audio_convert.clone(),
        audio_resample.clone(),
        audio_caps.clone(),
        audio_rate.clone(),
        audio_clock.clone(),
        audio_output_queue.clone(),
    ];
    let element_refs = elements.iter().collect::<Vec<_>>();
    if let Err(error) = pipeline.add_many(element_refs.as_slice()) {
        for element in elements.iter().rev() {
            let _ = element.set_state(gst::State::Null);
            let _ = pipeline.remove(element);
        }
        return Err(EngineError::Pipeline(error.to_string()));
    }
    let video_source_pad = match video_output_queue.static_pad("src") {
        Some(pad) => pad,
        None => {
            for element in elements.iter().rev() {
                let _ = element.set_state(gst::State::Null);
                let _ = pipeline.remove(element);
            }
            return Err(EngineError::MissingPad {
                element: "live-video-output-queue",
                pad: "src",
            });
        }
    };
    let audio_source_pad = match audio_output_queue.static_pad("src") {
        Some(pad) => pad,
        None => {
            for element in elements.iter().rev() {
                let _ = element.set_state(gst::State::Null);
                let _ = pipeline.remove(element);
            }
            return Err(EngineError::MissingPad {
                element: "live-audio-output-queue",
                pad: "src",
            });
        }
    };
    let mut video_selector_pad = None;
    let mut audio_selector_pad = None;
    let install_result = (|| -> Result<LiveProgram, EngineError> {
        link(&source_element, &demux, "udpsrc", "tsdemux")?;
        gst::Element::link_many([
            &video_decode_queue,
            &video_parse,
            &video_decode,
            &video_convert,
            &video_scale,
            &video_rate,
            &video_caps,
            &video_clock,
            &video_output_queue,
        ])
        .map_err(|_| EngineError::Link {
            from: "live-video-decode-queue",
            to: "live-video-output-queue",
        })?;
        gst::Element::link_many([
            &audio_decode_queue,
            &audio_parse,
            &audio_decode,
            &audio_convert,
            &audio_resample,
            &audio_caps,
            &audio_rate,
            &audio_clock,
            &audio_output_queue,
        ])
        .map_err(|_| EngineError::Link {
            from: "live-audio-decode-queue",
            to: "live-audio-output-queue",
        })?;

        let video_sink = video_decode_queue
            .static_pad("sink")
            .ok_or(EngineError::MissingPad {
                element: "live-video-decode-queue",
                pad: "sink",
            })?;
        let audio_sink = audio_decode_queue
            .static_pad("sink")
            .ok_or(EngineError::MissingPad {
                element: "live-audio-decode-queue",
                pad: "sink",
            })?;
        let timeline_offset = Arc::new(AtomicI64::new(i64::MIN));
        let video_pipeline = pipeline.clone();
        let video_timeline_offset = Arc::clone(&timeline_offset);
        video_sink.add_probe(gst::PadProbeType::BUFFER, move |_, info| {
            rebase_live_buffer(info, &video_pipeline, &video_timeline_offset);
            gst::PadProbeReturn::Ok
        });
        let audio_pipeline = pipeline.clone();
        let audio_timeline_offset = Arc::clone(&timeline_offset);
        audio_sink.add_probe(gst::PadProbeType::BUFFER, move |_, info| {
            rebase_live_buffer(info, &audio_pipeline, &audio_timeline_offset);
            gst::PadProbeReturn::Ok
        });
        demux.connect_pad_added(move |_, source_pad| {
            let caps = source_pad
                .current_caps()
                .unwrap_or_else(|| source_pad.query_caps(None));
            let media_type = caps
                .structure(0)
                .map(|value| value.name().to_string())
                .unwrap_or_default();
            if media_type.starts_with("video/") && !video_sink.is_linked() {
                let _ = source_pad.link(&video_sink);
            } else if media_type.starts_with("audio/") && !audio_sink.is_linked() {
                let _ = source_pad.link(&audio_sink);
            }
        });

        let video_pad = connect_source(
            &video_output_queue,
            video_selector,
            "live-video-output-queue",
        )?;
        video_selector_pad = Some(video_pad.clone());
        let audio_pad = connect_source(
            &audio_output_queue,
            audio_selector,
            "live-audio-output-queue",
        )?;
        audio_selector_pad = Some(audio_pad.clone());
        let video_ready = Arc::new(AtomicBool::new(false));
        let audio_ready = Arc::new(AtomicBool::new(false));
        let video_buffers = Arc::new(AtomicU64::new(0));
        let audio_buffers = Arc::new(AtomicU64::new(0));
        let video_health_pad = video_scale
            .static_pad("src")
            .ok_or(EngineError::MissingPad {
                element: "live-video-scale",
                pad: "src",
            })?;
        let audio_health_pad = audio_caps
            .static_pad("src")
            .ok_or(EngineError::MissingPad {
                element: "live-audio-canonical-caps",
                pad: "src",
            })?;
        let video_signal = Arc::clone(&video_ready);
        let video_count = Arc::clone(&video_buffers);
        video_health_pad.add_probe(gst::PadProbeType::BUFFER, move |_, _| {
            video_signal.store(true, Ordering::Release);
            video_count.fetch_add(1, Ordering::AcqRel);
            gst::PadProbeReturn::Ok
        });
        let audio_signal = Arc::clone(&audio_ready);
        let audio_count = Arc::clone(&audio_buffers);
        audio_health_pad.add_probe(gst::PadProbeType::BUFFER, move |_, _| {
            audio_signal.store(true, Ordering::Release);
            audio_count.fetch_add(1, Ordering::AcqRel);
            gst::PadProbeReturn::Ok
        });

        Ok(LiveProgram {
            video_pad,
            audio_pad,
            video_ready,
            audio_ready,
            video_buffers,
            audio_buffers,
            video_source_pad: video_source_pad.clone(),
            audio_source_pad: audio_source_pad.clone(),
            elements: elements.clone(),
        })
    })();
    if install_result.is_err() {
        cleanup_live_install(
            pipeline,
            video_selector,
            audio_selector,
            LiveInstallParts {
                elements: &elements,
                video_source_pad: &video_source_pad,
                audio_source_pad: &audio_source_pad,
                video_selector_pad: video_selector_pad.as_ref(),
                audio_selector_pad: audio_selector_pad.as_ref(),
            },
        );
    }
    install_result
}

fn wait_for_live_decode(program: &LiveProgram, timeout: Duration) -> Result<(), EngineError> {
    let started = Instant::now();
    loop {
        let video_ready = program.video_ready.load(Ordering::Acquire);
        let audio_ready = program.audio_ready.load(Ordering::Acquire);
        if video_ready && audio_ready {
            return Ok(());
        }
        if started.elapsed() >= timeout {
            return Err(EngineError::ProgramDecodeTimedOut {
                video_ready,
                audio_ready,
            });
        }
        std::thread::sleep(Duration::from_millis(2));
    }
}

fn remove_live_program(
    pipeline: &gst::Pipeline,
    video_selector: &gst::Element,
    audio_selector: &gst::Element,
    program: &LiveProgram,
) {
    cleanup_live_install(
        pipeline,
        video_selector,
        audio_selector,
        LiveInstallParts::from_program(program),
    );
}

struct LiveInstallParts<'a> {
    elements: &'a [gst::Element],
    video_source_pad: &'a gst::Pad,
    audio_source_pad: &'a gst::Pad,
    video_selector_pad: Option<&'a gst::Pad>,
    audio_selector_pad: Option<&'a gst::Pad>,
}

impl<'a> LiveInstallParts<'a> {
    fn from_program(program: &'a LiveProgram) -> Self {
        Self {
            elements: &program.elements,
            video_source_pad: &program.video_source_pad,
            audio_source_pad: &program.audio_source_pad,
            video_selector_pad: Some(&program.video_pad),
            audio_selector_pad: Some(&program.audio_pad),
        }
    }
}

fn cleanup_live_install(
    pipeline: &gst::Pipeline,
    video_selector: &gst::Element,
    audio_selector: &gst::Element,
    parts: LiveInstallParts<'_>,
) {
    for element in parts.elements {
        let _ = element.set_state(gst::State::Null);
    }
    wait_for_branch_elements_to_stop(parts.elements);
    if let Some(pad) = parts.video_selector_pad {
        let _ = parts.video_source_pad.unlink(pad);
        video_selector.release_request_pad(pad);
    }
    if let Some(pad) = parts.audio_selector_pad {
        let _ = parts.audio_source_pad.unlink(pad);
        audio_selector.release_request_pad(pad);
    }
    for element in parts.elements.iter().rev() {
        let _ = pipeline.remove(element);
    }
}

struct FileProgram {
    decoder: gst::Element,
    video_pad: gst::Pad,
    audio_pad: gst::Pad,
    video_ready: Arc<AtomicBool>,
    audio_ready: Arc<AtomicBool>,
    video_source_pad: gst::Pad,
    audio_source_pad: gst::Pad,
    elements: Vec<gst::Element>,
    started: bool,
}

fn install_file_program(
    pipeline: &gst::Pipeline,
    media_path: &Path,
    video_selector: &gst::Element,
    audio_selector: &gst::Element,
    profile: &OutputProfile,
) -> Result<FileProgram, EngineError> {
    let sequence = FILE_PROGRAM_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    let name = |component: &str| format!("program-file-{component}-{sequence}");
    let canonical = std::fs::canonicalize(media_path)
        .map_err(|error| EngineError::MediaRejected(error.to_string()))?;
    let uri = gst::glib::filename_to_uri(&canonical, None)
        .map_err(|error| EngineError::MediaRejected(error.to_string()))?;
    let decoder = gst::ElementFactory::make("uridecodebin")
        .name(name("decoder"))
        .property("uri", uri.as_str())
        .property("expose-all-streams", false)
        .build()
        .map_err(|error| build_error("uridecodebin", error))?;

    let video_decode_queue = element("queue", name("video-decode-queue"))?;
    let video_convert = element("videoconvert", name("video-convert"))?;
    let video_scale = element("videoscale", name("video-scale"))?;
    let video_rate = element("videorate", name("video-rate"))?;
    let video_caps = video_caps_filter(name("canonical-video"), profile)?;
    let video_clock = gst::ElementFactory::make("clocksync")
        .name(name("video-clock"))
        .property("sync-to-first", true)
        .build()
        .map_err(|error| build_error("clocksync", error))?;
    let video_output_queue = element("queue", name("video-output-queue"))?;

    let audio_decode_queue = element("queue", name("audio-decode-queue"))?;
    let audio_convert = element("audioconvert", name("audio-convert"))?;
    let audio_resample = element("audioresample", name("audio-resample"))?;
    let audio_caps = audio_caps_filter(name("canonical-audio"), profile)?;
    let audio_rate = gst::ElementFactory::make("audiorate")
        .name(name("audio-rate"))
        .property("skip-to-first", true)
        .property("tolerance", AUDIO_TIMESTAMP_TOLERANCE_NS)
        .build()
        .map_err(|error| build_error("audiorate", error))?;
    let audio_clock = gst::ElementFactory::make("clocksync")
        .name(name("audio-clock"))
        .property("sync-to-first", true)
        .build()
        .map_err(|error| build_error("clocksync", error))?;
    let audio_output_queue = element("queue", name("audio-output-queue"))?;

    pipeline
        .add_many([
            &decoder,
            &video_decode_queue,
            &video_convert,
            &video_scale,
            &video_rate,
            &video_caps,
            &video_clock,
            &video_output_queue,
            &audio_decode_queue,
            &audio_convert,
            &audio_resample,
            &audio_caps,
            &audio_rate,
            &audio_clock,
            &audio_output_queue,
        ])
        .map_err(|error| EngineError::Pipeline(error.to_string()))?;
    gst::Element::link_many([
        &video_decode_queue,
        &video_convert,
        &video_scale,
        &video_rate,
        &video_caps,
        &video_clock,
        &video_output_queue,
    ])
    .map_err(|_| EngineError::Link {
        from: "program-file-video-decode-queue",
        to: "program-file-video-output-queue",
    })?;
    gst::Element::link_many([
        &audio_decode_queue,
        &audio_convert,
        &audio_resample,
        &audio_caps,
        &audio_rate,
        &audio_clock,
        &audio_output_queue,
    ])
    .map_err(|_| EngineError::Link {
        from: "program-file-audio-decode-queue",
        to: "program-file-audio-output-queue",
    })?;

    let video_pad = connect_source(
        &video_output_queue,
        video_selector,
        "program-file-video-output-queue",
    )?;
    let audio_pad = connect_source(
        &audio_output_queue,
        audio_selector,
        "program-file-audio-output-queue",
    )?;
    let video_source_pad = video_output_queue
        .static_pad("src")
        .ok_or(EngineError::MissingPad {
            element: "program-file-video-output-queue",
            pad: "src",
        })?;
    let audio_source_pad = audio_output_queue
        .static_pad("src")
        .ok_or(EngineError::MissingPad {
            element: "program-file-audio-output-queue",
            pad: "src",
        })?;
    let video_ready = Arc::new(AtomicBool::new(false));
    let audio_ready = Arc::new(AtomicBool::new(false));
    let video_sink = video_decode_queue
        .static_pad("sink")
        .ok_or(EngineError::MissingPad {
            element: "program-file-video-decode-queue",
            pad: "sink",
        })?;
    let audio_sink = audio_decode_queue
        .static_pad("sink")
        .ok_or(EngineError::MissingPad {
            element: "program-file-audio-decode-queue",
            pad: "sink",
        })?;
    let video_ready_signal = Arc::clone(&video_ready);
    let audio_ready_signal = Arc::clone(&audio_ready);
    decoder.connect_pad_added(move |_, source_pad| {
        let caps = source_pad
            .current_caps()
            .unwrap_or_else(|| source_pad.query_caps(None));
        let Some(media_type) = caps.structure(0).map(|value| value.name()) else {
            return;
        };
        if media_type.starts_with("video/") && !video_sink.is_linked() {
            if source_pad.link(&video_sink).is_ok() {
                video_ready_signal.store(true, Ordering::Release);
            }
        } else if media_type.starts_with("audio/")
            && !audio_sink.is_linked()
            && source_pad.link(&audio_sink).is_ok()
        {
            audio_ready_signal.store(true, Ordering::Release);
        }
    });

    Ok(FileProgram {
        decoder: decoder.clone(),
        video_pad,
        audio_pad,
        video_ready,
        audio_ready,
        video_source_pad,
        audio_source_pad,
        elements: vec![
            decoder,
            video_decode_queue,
            video_convert,
            video_scale,
            video_rate,
            video_caps,
            video_clock,
            video_output_queue,
            audio_decode_queue,
            audio_convert,
            audio_resample,
            audio_caps,
            audio_rate,
            audio_clock,
            audio_output_queue,
        ],
        started: false,
    })
}

fn wait_for_file_decode(
    program: &FileProgram,
    pipeline: &gst::Pipeline,
    timeout: Duration,
) -> Result<(), EngineError> {
    let started = Instant::now();
    loop {
        let video_ready = program.video_ready.load(Ordering::Acquire);
        let audio_ready = program.audio_ready.load(Ordering::Acquire);
        if video_ready && audio_ready {
            return Ok(());
        }
        if let Some(message) = pipeline.bus().and_then(|bus| {
            bus.timed_pop_filtered(gst::ClockTime::ZERO, &[gst::MessageType::Error])
        }) && let gst::MessageView::Error(error) = message.view()
        {
            return Err(EngineError::Pipeline(format!(
                "{} ({:?})",
                error.error(),
                error.debug()
            )));
        }
        if started.elapsed() >= timeout {
            return Err(EngineError::ProgramDecodeTimedOut {
                video_ready,
                audio_ready,
            });
        }
        std::thread::sleep(Duration::from_millis(2));
    }
}

fn remove_file_program(
    pipeline: &gst::Pipeline,
    video_selector: &gst::Element,
    audio_selector: &gst::Element,
    program: &FileProgram,
) {
    let _ = program.video_source_pad.unlink(&program.video_pad);
    let _ = program.audio_source_pad.unlink(&program.audio_pad);
    video_selector.release_request_pad(&program.video_pad);
    audio_selector.release_request_pad(&program.audio_pad);
    for element in &program.elements {
        let _ = element.set_state(gst::State::Null);
    }
    wait_for_branch_elements_to_stop(&program.elements);
    for element in program.elements.iter().rev() {
        let _ = pipeline.remove(element);
    }
}

fn wait_for_branch_elements_to_stop(elements: &[gst::Element]) {
    let cleanup_started = Instant::now();
    let cleanup_budget = Duration::from_secs(5);
    for element in elements {
        let remaining = cleanup_budget.saturating_sub(cleanup_started.elapsed());
        if remaining.is_zero() {
            break;
        }
        let timeout_ns = u64::try_from(remaining.as_nanos()).unwrap_or(u64::MAX);
        let _ = element.state(gst::ClockTime::from_nseconds(timeout_ns));
    }
}

fn active_role(
    selector: &gst::Element,
    fallback_pad: &gst::Pad,
    program_pad: &gst::Pad,
    live_pad: Option<&gst::Pad>,
) -> Result<SourceRole, EngineError> {
    let active: Option<gst::Pad> = selector.property("active-pad");
    let actual = active.as_ref().map(|pad| pad.name().to_string());
    let fallback = fallback_pad.name().to_string();
    let program = program_pad.name().to_string();
    if actual.as_ref() == Some(&fallback) {
        Ok(SourceRole::Fallback)
    } else if actual.as_ref() == Some(&program) {
        Ok(SourceRole::Program)
    } else if live_pad.is_some_and(|pad| actual.as_ref() == Some(&pad.name().to_string())) {
        Ok(SourceRole::Live)
    } else {
        Err(EngineError::UnknownActivePad {
            actual,
            fallback,
            program,
            live: live_pad.map(|pad| pad.name().to_string()),
        })
    }
}

impl Drop for PersistentPlayout {
    fn drop(&mut self) {
        if !self.stopped {
            for ingress in &self.output_capture_ingresses {
                let _ = ingress.abort_any("graph_dropped", "output graph dropped during capture");
            }
            let deadline = Instant::now() + Duration::from_secs(2);
            for ingress in &self.output_capture_ingresses {
                let _ = ingress.join_finished_by(deadline);
            }
            if let Err(error) = self.stop_pipeline() {
                eprintln!("station media graph teardown did not reach Null: {error:?}");
            }
        }
    }
}

impl CaptureIngress {
    fn new() -> Self {
        Self {
            active: AtomicBool::new(false),
            aborted: AtomicBool::new(false),
            generation: AtomicU64::new(0),
            callback_failed: AtomicBool::new(false),
            queued_bytes: AtomicU64::new(0),
            sender: Mutex::new(None),
            proof: Mutex::new(None),
            writer: Mutex::new(None),
        }
    }

    fn offer(&self, buffer: &gst::BufferRef) {
        let generation = self.generation.load(Ordering::Acquire);
        if !self.active.load(Ordering::Acquire) {
            return;
        }
        match self.sender.try_lock() {
            Ok(sender) => {
                // Recheck while holding the same lock used to deactivate an
                // ingress. This closes the natural-completion race between
                // the fast-path atomic read above and sender removal.
                if !self.active.load(Ordering::Acquire)
                    || self.generation.load(Ordering::Acquire) != generation
                {
                    return;
                }
                let Some(sender) = sender.as_ref() else {
                    self.callback_failed.store(true, Ordering::Release);
                    return;
                };
                let buffer_bytes = buffer.size() as u64;
                if !self.reserve_queued_bytes(buffer_bytes) {
                    self.callback_failed.store(true, Ordering::Release);
                    return;
                }
                if matches!(
                    sender.try_send((generation, buffer.to_owned())),
                    Err(TrySendError::Full(_) | TrySendError::Disconnected(_))
                ) {
                    self.release_queued_bytes(buffer_bytes);
                    self.callback_failed.store(true, Ordering::Release);
                }
            }
            Err(TryLockError::WouldBlock | TryLockError::Poisoned(_)) => {
                if self.active.load(Ordering::Acquire)
                    && self.generation.load(Ordering::Acquire) == generation
                {
                    self.callback_failed.store(true, Ordering::Release);
                }
            }
        }
    }

    fn reserve_queued_bytes(&self, buffer_bytes: u64) -> bool {
        let mut current = self.queued_bytes.load(Ordering::Acquire);
        loop {
            let Some(next) = current.checked_add(buffer_bytes) else {
                return false;
            };
            if next > OUTPUT_CAPTURE_QUEUE_BYTES {
                return false;
            }
            match self.queued_bytes.compare_exchange_weak(
                current,
                next,
                Ordering::AcqRel,
                Ordering::Acquire,
            ) {
                Ok(_) => return true,
                Err(observed) => current = observed,
            }
        }
    }

    fn release_queued_bytes(&self, buffer_bytes: u64) {
        self.queued_bytes.fetch_sub(buffer_bytes, Ordering::AcqRel);
    }

    fn begin(
        self: &Arc<Self>,
        request: OutputCaptureRequest,
        root: &Path,
    ) -> Result<OutputCaptureStarted, EngineError> {
        self.reap_writer()?;
        if self.active.load(Ordering::Acquire) {
            return Err(EngineError::OutputCapture(
                "a proof capture is already active for this selected sink branch".into(),
            ));
        }
        if self
            .writer
            .lock()
            .map_err(|_| EngineError::OutputCapture("capture writer lock is poisoned".into()))?
            .is_some()
        {
            return Err(EngineError::OutputCapture(
                "the previous selected-sink capture is still finalizing".into(),
            ));
        }

        fs::create_dir_all(root).map_err(|error| {
            EngineError::OutputCapture(format!("could not create managed proof root: {error}"))
        })?;
        let artifact_relative_path = format!("{}.ts", request.proof_id);
        let final_path = root.join(&artifact_relative_path);
        let part_path = root.join(format!("{}.ts.part", request.proof_id));
        if final_path.exists() || part_path.exists() {
            return Err(EngineError::OutputCapture(
                "proof artifact identity already exists in the managed root".into(),
            ));
        }
        let file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&part_path)
            .map_err(|error| {
                EngineError::OutputCapture(format!("could not create proof part file: {error}"))
            })?;
        let pending = OutputCaptureStatus::Pending {
            proof_id: request.proof_id.clone(),
            artifact_relative_path: artifact_relative_path.clone(),
        };
        let status = Arc::new(Mutex::new(pending));
        let (sender, receiver) = mpsc::sync_channel(OUTPUT_CAPTURE_QUEUE_BUFFERS);
        let (ready_sender, ready_receiver) = mpsc::sync_channel(0);
        let (start_sender, start_receiver) = mpsc::channel();
        let mut sender_guard = self
            .sender
            .lock()
            .map_err(|_| EngineError::OutputCapture("capture sender lock is poisoned".into()))?;
        self.callback_failed.store(false, Ordering::Release);
        self.queued_bytes.store(0, Ordering::Release);
        self.aborted.store(false, Ordering::Release);
        let generation = self.generation.fetch_add(1, Ordering::AcqRel) + 1;
        *sender_guard = Some(sender);
        *self
            .proof
            .lock()
            .map_err(|_| EngineError::OutputCapture("capture state lock is poisoned".into()))? =
            Some(ActiveOutputCapture {
                proof_id: request.proof_id.clone(),
                status: Arc::clone(&status),
            });
        drop(sender_guard);

        let started = OutputCaptureStarted {
            proof_id: request.proof_id.clone(),
            artifact_relative_path: artifact_relative_path.clone(),
        };
        let cleanup_part_path = part_path.clone();
        let ingress = Arc::clone(self);
        let handle = std::thread::Builder::new()
            .name(format!("output-proof-{}", request.proof_id))
            .spawn(move || {
                if ready_sender.send(()).is_err() || start_receiver.recv().is_err() {
                    return;
                }
                run_capture_writer(
                    ingress,
                    request,
                    artifact_relative_path,
                    part_path,
                    final_path,
                    file,
                    receiver,
                    generation,
                    status,
                );
            })
            .map_err(|error| {
                self.active.store(false, Ordering::Release);
                let _ = self.sender.lock().map(|mut sender| sender.take());
                let _ = fs::remove_file(&cleanup_part_path);
                EngineError::OutputCapture(format!("could not start capture writer: {error}"))
            })?;
        *self
            .writer
            .lock()
            .map_err(|_| EngineError::OutputCapture("capture writer lock is poisoned".into()))? =
            Some(handle);
        if ready_receiver
            .recv_timeout(OUTPUT_CAPTURE_WRITER_READY_TIMEOUT)
            .is_err()
        {
            let _ = self.abort_any(
                "capture_writer_unavailable",
                "output capture writer did not become ready before its deadline",
            );
            let _ = self.join_finished_by(Instant::now() + OUTPUT_CAPTURE_WRITER_READY_TIMEOUT);
            let _ = fs::remove_file(&cleanup_part_path);
            return Err(EngineError::OutputCapture(
                "capture writer did not become ready before its deadline".into(),
            ));
        }
        self.active.store(true, Ordering::Release);
        if start_sender.send(()).is_err() {
            let _ = self.abort_any(
                "capture_writer_unavailable",
                "output capture writer stopped before capture began",
            );
            let _ = self.join_finished_by(Instant::now() + OUTPUT_CAPTURE_WRITER_READY_TIMEOUT);
            let _ = fs::remove_file(&cleanup_part_path);
            return Err(EngineError::OutputCapture(
                "capture writer stopped before capture began".into(),
            ));
        }
        Ok(started)
    }

    fn status_for(&self, proof_id: &str) -> Result<Option<OutputCaptureStatus>, EngineError> {
        let proof = self
            .proof
            .lock()
            .map_err(|_| EngineError::OutputCapture("capture state lock is poisoned".into()))?;
        let Some(proof) = proof.as_ref().filter(|proof| proof.proof_id == proof_id) else {
            return Ok(None);
        };
        let status = proof
            .status
            .lock()
            .map_err(|_| EngineError::OutputCapture("capture result lock is poisoned".into()))?
            .clone();
        Ok(Some(status))
    }

    fn abort_if_matches(
        &self,
        proof_id: &str,
        code: &str,
        message: &str,
    ) -> Result<bool, EngineError> {
        let matches = self
            .proof
            .lock()
            .map_err(|_| EngineError::OutputCapture("capture state lock is poisoned".into()))?
            .as_ref()
            .is_some_and(|proof| proof.proof_id == proof_id);
        if matches {
            self.abort_any(code, message)?;
        }
        Ok(matches)
    }

    fn abort_any(&self, code: &str, message: &str) -> Result<(), EngineError> {
        let proof = self
            .proof
            .lock()
            .map_err(|_| EngineError::OutputCapture("capture state lock is poisoned".into()))?
            .as_ref()
            .map(|proof| (proof.proof_id.clone(), Arc::clone(&proof.status)));
        let mut aborted_pending = false;
        if let Some((proof_id, status)) = proof {
            let mut status = status.lock().map_err(|_| {
                EngineError::OutputCapture("capture result lock is poisoned".into())
            })?;
            if let OutputCaptureStatus::Pending {
                artifact_relative_path,
                ..
            } = &*status
            {
                let artifact_relative_path = artifact_relative_path.clone();
                // The abort flag and terminal status are committed under the
                // same mutex used by the writer. Whichever side acquires this
                // lock first owns the terminal transition.
                self.aborted.store(true, Ordering::Release);
                *status = OutputCaptureStatus::Failed {
                    proof_id,
                    artifact_relative_path,
                    code: code.into(),
                    message: message.into(),
                };
                aborted_pending = true;
            }
        }
        if !aborted_pending {
            return Ok(());
        }
        self.callback_failed.store(true, Ordering::Release);
        let mut sender = self
            .sender
            .lock()
            .map_err(|_| EngineError::OutputCapture("capture sender lock is poisoned".into()))?;
        self.active.store(false, Ordering::Release);
        sender.take();
        drop(sender);
        Ok(())
    }

    fn reap_writer(&self) -> Result<(), EngineError> {
        let handle = self
            .writer
            .lock()
            .map_err(|_| EngineError::OutputCapture("capture writer lock is poisoned".into()))?
            .take();
        if let Some(handle) = handle {
            if !handle.is_finished() {
                *self.writer.lock().map_err(|_| {
                    EngineError::OutputCapture("capture writer lock is poisoned".into())
                })? = Some(handle);
                return Ok(());
            }
            handle
                .join()
                .map_err(|_| EngineError::OutputCapture("capture writer thread panicked".into()))?;
        }
        Ok(())
    }

    fn join_finished_by(&self, deadline: Instant) -> Result<(), EngineError> {
        loop {
            self.reap_writer()?;
            if self
                .writer
                .lock()
                .map_err(|_| EngineError::OutputCapture("capture writer lock is poisoned".into()))?
                .is_none()
            {
                return Ok(());
            }
            if Instant::now() >= deadline {
                return Err(EngineError::OutputCapture(
                    "capture writer did not stop before the shutdown deadline".into(),
                ));
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn run_capture_writer(
    ingress: Arc<CaptureIngress>,
    request: OutputCaptureRequest,
    artifact_relative_path: String,
    part_path: PathBuf,
    final_path: PathBuf,
    file: std::fs::File,
    receiver: mpsc::Receiver<(u64, gst::Buffer)>,
    generation: u64,
    status: Arc<Mutex<OutputCaptureStatus>>,
) {
    let mut file = BufWriter::with_capacity(OUTPUT_CAPTURE_WRITE_BUFFER_BYTES, file);
    let started = Instant::now();
    let deadline = started + Duration::from_millis(request.duration_ms);
    let mut digest = Sha256::new();
    let mut capture_bytes = 0_u64;
    let failure_proof_id = request.proof_id.clone();
    let failure_artifact_relative_path = artifact_relative_path.clone();
    let result = (|| -> Result<OutputCaptureReady, (&'static str, String)> {
        loop {
            if ingress.callback_failed.load(Ordering::Acquire) {
                return Err((
                    "capture_ingress_failed",
                    "post-mux capture ingress overflowed or became unavailable".into(),
                ));
            }
            let now = Instant::now();
            if now >= deadline {
                break;
            }
            let remaining = deadline.saturating_duration_since(now);
            match receiver.recv_timeout(remaining.min(Duration::from_millis(50))) {
                Ok((buffer_generation, buffer)) => {
                    ingress.release_queued_bytes(buffer.size() as u64);
                    if buffer_generation == generation {
                        write_capture_buffer(
                            &mut file,
                            &mut digest,
                            &mut capture_bytes,
                            request.max_bytes,
                            &buffer,
                        )?;
                    }
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {}
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    return Err((
                        "capture_interrupted",
                        "post-mux capture ingress disconnected before its deadline".into(),
                    ));
                }
            }
        }

        // Publish deactivation before taking the sender lock. A streaming
        // callback that loses this lock race can now distinguish ordinary
        // finalization from an active-ingress contention failure.
        ingress.active.store(false, Ordering::Release);
        let mut sender = ingress.sender.lock().map_err(|_| {
            (
                "capture_state_failed",
                "capture sender lock is poisoned".into(),
            )
        })?;
        sender.take();
        drop(sender);
        while let Ok((buffer_generation, buffer)) = receiver.try_recv() {
            ingress.release_queued_bytes(buffer.size() as u64);
            if buffer_generation == generation {
                write_capture_buffer(
                    &mut file,
                    &mut digest,
                    &mut capture_bytes,
                    request.max_bytes,
                    &buffer,
                )?;
            }
        }
        if ingress.callback_failed.load(Ordering::Acquire) {
            return Err((
                "capture_ingress_failed",
                "post-mux capture ingress overflowed or became unavailable".into(),
            ));
        }
        if capture_bytes == 0 {
            return Err((
                "capture_empty",
                "post-mux capture produced no transport bytes".into(),
            ));
        }
        file.flush().map_err(|error| {
            (
                "capture_write_failed",
                format!("could not flush proof artifact: {error}"),
            )
        })?;
        file.get_ref().sync_all().map_err(|error| {
            (
                "capture_sync_failed",
                format!("could not sync proof artifact: {error}"),
            )
        })?;
        drop(file);
        if ingress.aborted.load(Ordering::Acquire) {
            return Err((
                "capture_interrupted",
                "post-mux capture was aborted before publication".into(),
            ));
        }
        if final_path.exists() {
            return Err((
                "capture_identity_conflict",
                "final proof artifact already exists".into(),
            ));
        }
        fs::rename(&part_path, &final_path).map_err(|error| {
            (
                "capture_publish_failed",
                format!("could not publish proof artifact atomically: {error}"),
            )
        })?;
        if ingress.aborted.load(Ordering::Acquire) {
            let _ = fs::remove_file(&final_path);
            return Err((
                "capture_interrupted",
                "post-mux capture was aborted during publication".into(),
            ));
        }
        Ok(OutputCaptureReady {
            proof_id: request.proof_id,
            output_index: request.output_index,
            sink_id: request.sink_id,
            loudness_target: request.loudness_target,
            artifact_relative_path,
            capture_bytes,
            capture_sha256: format!("{:x}", digest.finalize()),
            // A ready capture proves that the writer remained active until the
            // requested lower bound elapsed. Report that bounded contractual
            // sample duration; independent decode measures the artifact's
            // actual media duration before any proof is accepted.
            capture_duration_ms: request.duration_ms,
        })
    })();

    ingress.active.store(false, Ordering::Release);
    let _ = ingress.sender.lock().map(|mut sender| sender.take());
    if result.is_err() {
        let _ = fs::remove_file(&part_path);
    }
    let mut remove_published_artifact = false;
    if let Ok(mut current) = status.lock() {
        if matches!(*current, OutputCaptureStatus::Pending { .. }) {
            if ingress.aborted.load(Ordering::Acquire) {
                remove_published_artifact = result.is_ok();
                *current = OutputCaptureStatus::Failed {
                    proof_id: failure_proof_id,
                    artifact_relative_path: failure_artifact_relative_path,
                    code: "capture_interrupted".into(),
                    message: "post-mux capture was aborted before terminal publication".into(),
                };
            } else {
                *current = match result {
                    Ok(ready) => OutputCaptureStatus::Ready(ready),
                    Err((code, message)) => OutputCaptureStatus::Failed {
                        proof_id: failure_proof_id,
                        artifact_relative_path: failure_artifact_relative_path,
                        code: code.into(),
                        message,
                    },
                };
            }
        } else if result.is_ok() {
            remove_published_artifact = true;
        }
    }
    if remove_published_artifact {
        let _ = fs::remove_file(&final_path);
    }
}

fn write_capture_buffer(
    file: &mut impl Write,
    digest: &mut Sha256,
    capture_bytes: &mut u64,
    max_bytes: u64,
    buffer: &gst::Buffer,
) -> Result<(), (&'static str, String)> {
    let map = buffer.map_readable().map_err(|_| {
        (
            "capture_map_failed",
            "post-mux transport buffer could not be mapped".into(),
        )
    })?;
    let bytes = map.as_slice();
    let next = capture_bytes
        .checked_add(bytes.len() as u64)
        .ok_or(("capture_too_large", "capture byte count overflowed".into()))?;
    if next > max_bytes {
        return Err((
            "capture_too_large",
            "post-mux capture exceeded its hard byte limit".into(),
        ));
    }
    file.write_all(bytes).map_err(|error| {
        (
            "capture_write_failed",
            format!("could not write proof artifact: {error}"),
        )
    })?;
    digest.update(bytes);
    *capture_bytes = next;
    Ok(())
}

fn validate_proof_artifact_root(root: &Path) -> Result<PathBuf, EngineError> {
    use std::path::Component;

    if !root.is_absolute()
        || root.file_name().and_then(|name| name.to_str()) != Some("output-proofs")
        || root.to_string_lossy().chars().any(char::is_control)
        || root
            .components()
            .any(|component| matches!(component, Component::ParentDir | Component::CurDir))
    {
        return Err(EngineError::OutputCapture(
            "proof artifact root must be an absolute managed output-proofs directory".into(),
        ));
    }
    Ok(root.to_path_buf())
}

fn is_canonical_uuid(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 36
        && [8, 13, 18, 23].iter().all(|&index| bytes[index] == b'-')
        && bytes
            .iter()
            .enumerate()
            .all(|(index, byte)| matches!(index, 8 | 13 | 18 | 23) || byte.is_ascii_hexdigit())
}

fn validate_caption_cue(cue: &CaptionCue) -> Result<(), EngineError> {
    if !is_canonical_uuid(&cue.cue_id) || cue.cue_id != cue.cue_id.to_ascii_lowercase() {
        return Err(EngineError::CaptionInvalid(
            "caption cue identity must be a lowercase canonical UUID".into(),
        ));
    }
    if cue.start_after_ms > MAX_CAPTION_START_AFTER_MS {
        return Err(EngineError::CaptionInvalid(
            "caption cue start delay exceeds the bounded range".into(),
        ));
    }
    if cue.duration_ms == 0 || cue.duration_ms > MAX_CAPTION_DURATION_MS {
        return Err(EngineError::CaptionInvalid(
            "caption cue duration is outside the bounded range".into(),
        ));
    }
    if cue.text.trim().is_empty()
        || cue.text.len() > MAX_CAPTION_TEXT_BYTES
        || cue
            .text
            .chars()
            .any(|character| character.is_control() && character != '\n')
    {
        return Err(EngineError::CaptionInvalid(
            "caption cue text is empty, oversized, or contains control characters".into(),
        ));
    }
    Ok(())
}

pub type SyntheticPlayout = PersistentPlayout;

fn element(
    factory: &'static str,
    name: impl Into<gst::glib::GString>,
) -> Result<gst::Element, EngineError> {
    gst::ElementFactory::make(factory)
        .name(name)
        .build()
        .map_err(|error| build_error(factory, error))
}

fn rebase_live_buffer(
    info: &mut gst::PadProbeInfo<'_>,
    pipeline: &gst::Pipeline,
    timeline_offset: &AtomicI64,
) {
    let Some(buffer) = info.buffer_mut() else {
        return;
    };
    let Some(pts) = buffer.pts() else {
        return;
    };

    let mut offset = timeline_offset.load(Ordering::Acquire);
    if offset == i64::MIN
        && let Some(running_time) = pipeline.current_running_time()
    {
        let candidate = i128::from(running_time.nseconds()) - i128::from(pts.nseconds());
        if let Ok(candidate) = i64::try_from(candidate) {
            let _ = timeline_offset.compare_exchange(
                i64::MIN,
                candidate,
                Ordering::AcqRel,
                Ordering::Acquire,
            );
            offset = timeline_offset.load(Ordering::Acquire);
        }
    }
    if offset == i64::MIN {
        return;
    }

    let buffer = buffer.make_mut();
    buffer.set_pts(shift_clock_time(pts, offset));
    if let Some(dts) = buffer.dts() {
        buffer.set_dts(shift_clock_time(dts, offset));
    }
}

fn shift_clock_time(value: gst::ClockTime, offset: i64) -> gst::ClockTime {
    let shifted = i128::from(value.nseconds()) + i128::from(offset);
    let bounded = shifted.clamp(0, i128::from(u64::MAX));
    gst::ClockTime::from_nseconds(bounded as u64)
}

fn selector(name: &'static str) -> Result<gst::Element, EngineError> {
    gst::ElementFactory::make("input-selector")
        .name(name)
        .property("sync-streams", true)
        .property("cache-buffers", true)
        .property("drop-backwards", true)
        .property_from_str("sync-mode", "clock")
        .build()
        .map_err(|error| build_error("input-selector", error))
}

fn audio_caps_filter(
    name: impl Into<gst::glib::GString>,
    profile: &OutputProfile,
) -> Result<gst::Element, EngineError> {
    let sample_rate = i32::try_from(profile.audio_sample_rate_hz)
        .map_err(|_| EngineError::Pipeline("audio sample rate exceeds GStreamer range".into()))?;
    let caps = gst::Caps::builder("audio/x-raw")
        .field("format", "S16LE")
        .field("layout", "interleaved")
        .field("rate", sample_rate)
        .field("channels", 2_i32)
        .build();
    gst::ElementFactory::make("capsfilter")
        .name(name)
        .property("caps", &caps)
        .build()
        .map_err(|error| build_error("capsfilter", error))
}

fn profile_loudness_target(profile: &OutputProfile) -> Result<LoudnessTarget, EngineError> {
    let target = LoudnessTarget::from_lufs(profile.loudness_target_lufs as f64).map_err(|_| {
        EngineError::UnsupportedLoudnessTarget {
            target_milli_lufs: (profile.loudness_target_lufs * 1_000.0) as i32,
        }
    })?;
    validate_loudness_target(target)?;
    Ok(target)
}

fn validate_loudness_target(target: LoudnessTarget) -> Result<(), EngineError> {
    if target != LoudnessTarget::CABLE && target != LoudnessTarget::STREAMING {
        return Err(EngineError::UnsupportedLoudnessTarget {
            target_milli_lufs: target.milli_lufs(),
        });
    }
    Ok(())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct OutputVariantContract {
    loudness_target: LoudnessTarget,
    mux: MpegTsMuxContract,
}

fn effective_output_variant_contract(
    output: &ConfiguredTransportOutput,
    profile: &OutputProfile,
) -> OutputVariantContract {
    OutputVariantContract {
        loudness_target: output.loudness_target,
        mux: output.mux_contract.unwrap_or_else(|| {
            MpegTsMuxContract::new(
                profile.transport_bitrate_bps,
                DEFAULT_MPEG_TS_PACKET_SIZE_BYTES,
            )
        }),
    }
}

fn output_variant_contracts(
    outputs: &[ConfiguredTransportOutput],
    profile: &OutputProfile,
) -> Result<Vec<OutputVariantContract>, EngineError> {
    let mut contracts = Vec::new();
    for output in outputs {
        validate_loudness_target(output.loudness_target)?;
        let contract = effective_output_variant_contract(output, profile);
        validate_mux_contract(contract.mux, profile)?;
        if !contracts.contains(&contract) {
            contracts.push(contract);
        }
    }
    contracts.sort_by_key(|contract| {
        (
            contract.loudness_target.milli_lufs(),
            contract.mux.transport_bitrate_bps,
            contract.mux.packet_size_bytes,
        )
    });
    Ok(contracts)
}

fn validate_mux_contract(
    contract: MpegTsMuxContract,
    profile: &OutputProfile,
) -> Result<(), EngineError> {
    let encoded_bitrate_bps =
        u64::from(profile.video_bitrate_bps) + u64::from(profile.audio_bitrate_bps);
    let reason = if contract.transport_bitrate_bps <= encoded_bitrate_bps {
        Some("transport bitrate must exceed the encoded elementary-stream bitrate")
    } else if contract.transport_bitrate_bps > MAX_TRANSPORT_BITRATE_BPS {
        Some("transport bitrate exceeds the supported 100 Mbps ceiling")
    } else if contract.packet_size_bytes < MPEG_TS_PACKET_BYTES
        || !contract
            .packet_size_bytes
            .is_multiple_of(MPEG_TS_PACKET_BYTES)
    {
        Some("packet size must contain whole 188-byte MPEG-TS packets")
    } else if contract.packet_size_bytes > MAX_UDP_PAYLOAD_BYTES {
        Some("packet size exceeds the maximum UDP payload")
    } else {
        None
    };
    if let Some(reason) = reason {
        return Err(EngineError::UnsupportedMuxContract {
            transport_bitrate_bps: contract.transport_bitrate_bps,
            packet_size_bytes: contract.packet_size_bytes,
            reason,
        });
    }
    Ok(())
}

fn bounded_queue(name: impl Into<gst::glib::GString>) -> Result<gst::Element, EngineError> {
    gst::ElementFactory::make("queue")
        .name(name)
        .property("max-size-buffers", 0_u32)
        .property("max-size-bytes", 4_u32 * 1024 * 1024)
        .property("max-size-time", 2_000_000_000_u64)
        .build()
        .map_err(|error| build_error("queue", error))
}

fn loudness_input_caps(name: impl Into<gst::glib::GString>) -> Result<gst::Element, EngineError> {
    let caps = gst::Caps::builder("audio/x-raw")
        .field("format", "F64LE")
        .field("layout", "interleaved")
        .field("rate", 192_000_i32)
        .field("channels", 2_i32)
        .build();
    gst::ElementFactory::make("capsfilter")
        .name(name)
        .property("caps", &caps)
        .build()
        .map_err(|error| build_error("capsfilter", error))
}

fn install_output_capture_ingress(
    output_queue: &gst::Element,
) -> Result<Arc<CaptureIngress>, EngineError> {
    let capture_ingress = Arc::new(CaptureIngress::new());
    let capture_pad = output_queue
        .static_pad("src")
        .ok_or(EngineError::MissingPad {
            element: "transport-output-queue",
            pad: "src",
        })?;
    let ingress = Arc::clone(&capture_ingress);
    capture_pad.add_probe(
        gst::PadProbeType::BUFFER | gst::PadProbeType::BUFFER_LIST,
        move |_, info| {
            if !ingress.active.load(Ordering::Acquire) {
                return gst::PadProbeReturn::Ok;
            }
            if let Some(buffer) = info.buffer() {
                ingress.offer(buffer.as_ref());
            } else if let Some(list) = info.buffer_list() {
                for buffer in list.iter() {
                    ingress.offer(buffer);
                }
            } else {
                ingress.callback_failed.store(true, Ordering::Release);
            }
            gst::PadProbeReturn::Ok
        },
    );
    Ok(capture_ingress)
}

fn build_loudness_variant(
    contract: OutputVariantContract,
    profile: &OutputProfile,
) -> Result<LoudnessVariant, EngineError> {
    let target = contract.loudness_target;
    validate_loudness_target(target)?;
    validate_mux_contract(contract.mux, profile)?;
    let prefix = format!(
        "variant-{}-{}-{}",
        target.milli_lufs().unsigned_abs(),
        contract.mux.transport_bitrate_bps,
        contract.mux.packet_size_bytes
    );
    let normalizer = gst::ElementFactory::make("audioloudnorm")
        .name(format!("{prefix}-normalizer"))
        .property("loudness-target", target.as_lufs())
        .property("max-true-peak", -2.0_f64)
        .property("loudness-range-target", 7.0_f64)
        .build()
        .map_err(|error| build_error("audioloudnorm", error))?;
    let audio_output_caps = audio_caps_filter(format!("{prefix}-output-caps"), profile)?;
    let mux = gst::ElementFactory::make("mpegtsmux")
        .name(format!("{prefix}-mux"))
        .property(
            "alignment",
            i32::from(contract.mux.packet_size_bytes / MPEG_TS_PACKET_BYTES),
        )
        .property("bitrate", contract.mux.transport_bitrate_bps)
        .property("pat-interval", 9_000_u32)
        .property("pmt-interval", 9_000_u32)
        .property("pcr-interval", 3_600_u32)
        .build()
        .map_err(|error| build_error("mpegtsmux", error))?;
    let audio_parser = element("aacparse", format!("{prefix}-audio-parser"))?;
    let audio_buffers = Arc::new(AtomicU64::new(0));
    let audio_health_pad = audio_parser
        .static_pad("src")
        .ok_or(EngineError::MissingPad {
            element: "aacparse",
            pad: "src",
        })?;
    let audio_count = Arc::clone(&audio_buffers);
    audio_health_pad.add_probe(gst::PadProbeType::BUFFER, move |_, _| {
        audio_count.fetch_add(1, Ordering::AcqRel);
        gst::PadProbeReturn::Ok
    });
    let transport_fanout = element("tee", format!("{prefix}-transport-fanout"))?;
    Ok(LoudnessVariant {
        video_queue: bounded_queue(format!("{prefix}-video-queue"))?,
        audio_queue: bounded_queue(format!("{prefix}-audio-queue"))?,
        audio_convert_in: element("audioconvert", format!("{prefix}-convert-in"))?,
        audio_resample_in: element("audioresample", format!("{prefix}-resample-in"))?,
        audio_input_caps: loudness_input_caps(format!("{prefix}-input-caps"))?,
        normalizer,
        audio_convert_out: element("audioconvert", format!("{prefix}-convert-out"))?,
        audio_resample_out: element("audioresample", format!("{prefix}-resample-out"))?,
        audio_output_caps,
        audio_encoder: gst::ElementFactory::make("voaacenc")
            .name(format!("{prefix}-audio-encoder"))
            .property("bitrate", profile.audio_bitrate_bps as i32)
            .property("perfect-timestamp", true)
            .build()
            .map_err(|error| build_error("voaacenc", error))?,
        audio_parser,
        audio_buffers,
        mux,
        transport_fanout,
    })
}

fn video_caps(profile: &OutputProfile) -> Result<gst::Caps, EngineError> {
    let width = i32::try_from(profile.width)
        .map_err(|_| EngineError::Pipeline("output width exceeds GStreamer range".into()))?;
    let height = i32::try_from(profile.height)
        .map_err(|_| EngineError::Pipeline("output height exceeds GStreamer range".into()))?;
    let numerator = i32::try_from(profile.frame_rate_numerator).map_err(|_| {
        EngineError::Pipeline("frame-rate numerator exceeds GStreamer range".into())
    })?;
    let denominator = i32::try_from(profile.frame_rate_denominator).map_err(|_| {
        EngineError::Pipeline("frame-rate denominator exceeds GStreamer range".into())
    })?;
    Ok(gst::Caps::builder("video/x-raw")
        .field("format", "I420")
        .field("width", width)
        .field("height", height)
        .field("framerate", gst::Fraction::new(numerator, denominator))
        .build())
}

fn cg_video_caps(profile: &OutputProfile) -> Result<gst::Caps, EngineError> {
    let width = i32::try_from(profile.width)
        .map_err(|_| EngineError::Pipeline("CG width exceeds GStreamer range".into()))?;
    let height = i32::try_from(profile.height)
        .map_err(|_| EngineError::Pipeline("CG height exceeds GStreamer range".into()))?;
    let numerator = i32::try_from(profile.frame_rate_numerator)
        .map_err(|_| EngineError::Pipeline("CG frame-rate numerator exceeds range".into()))?;
    let denominator = i32::try_from(profile.frame_rate_denominator)
        .map_err(|_| EngineError::Pipeline("CG frame-rate denominator exceeds range".into()))?;
    Ok(gst::Caps::builder("video/x-raw")
        .field("format", "BGRA")
        .field("width", width)
        .field("height", height)
        .field("framerate", gst::Fraction::new(numerator, denominator))
        .build())
}

fn connect_to_compositor(
    source: &gst::Element,
    compositor: &gst::Element,
    index: usize,
) -> Result<gst::Pad, EngineError> {
    let source_pad = source.static_pad("src").ok_or(EngineError::MissingPad {
        element: "cg-overlay-queue",
        pad: "src",
    })?;
    let sink = compositor
        .request_pad_simple("sink_%u")
        .ok_or(EngineError::MissingPad {
            element: "cg-compositor",
            pad: "sink_%u",
        })?;
    sink.set_property("zorder", u32::try_from(index).unwrap_or(0));
    sink.set_property("alpha", 0.0_f64);
    source_pad
        .link(&sink)
        .map_err(|error| EngineError::PadLink(format!("{error:?}")))?;
    Ok(sink)
}

fn validate_cg_snapshot(snapshot: &CgSnapshot) -> Result<(), EngineError> {
    if snapshot.board_id.is_empty() || snapshot.board_id.len() > 128 {
        return Err(EngineError::CgInvalid(
            "board id is empty or too long".into(),
        ));
    }
    if snapshot.board_revision == 0 {
        return Err(EngineError::CgInvalid(
            "board revision must be positive".into(),
        ));
    }
    if snapshot.overlays.len() > MAX_CG_SLOTS {
        return Err(EngineError::CgInvalid("too many CG overlay slots".into()));
    }
    let mut seen = [false; MAX_CG_SLOTS];
    for overlay in &snapshot.overlays {
        let slot = usize::from(overlay.slot);
        if slot >= MAX_CG_SLOTS || seen[slot] {
            return Err(EngineError::CgInvalid(
                "overlay slot is invalid or duplicated".into(),
            ));
        }
        seen[slot] = true;
        if overlay.z_index < 0 {
            return Err(EngineError::CgInvalid(
                "overlay z-index must be nonnegative".into(),
            ));
        }
        let rect = &overlay.rect;
        if rect.width == 0
            || rect.height == 0
            || u32::from(rect.x) + u32::from(rect.width) > 1000
            || u32::from(rect.y) + u32::from(rect.height) > 1000
        {
            return Err(EngineError::CgInvalid(
                "overlay rectangle is outside 0..1000".into(),
            ));
        }
        let text_len = match &overlay.content {
            CgOverlayContent::Text { text } => sanitize_text(text)?.len(),
            CgOverlayContent::Clock { format } => sanitize_text(format)?.len(),
            CgOverlayContent::Image { asset_id, .. } => {
                if asset_id.len() != 64
                    || asset_id != &asset_id.to_ascii_lowercase()
                    || !asset_id.bytes().all(|byte| byte.is_ascii_hexdigit())
                {
                    return Err(EngineError::CgInvalid(
                        "managed image asset id must be a SHA-256 digest".into(),
                    ));
                }
                0
            }
        };
        if text_len > MAX_CG_TEXT_BYTES {
            return Err(EngineError::CgInvalid("overlay text is too long".into()));
        }
    }
    let encoded = serde_json::to_vec(snapshot).map_err(|error| {
        EngineError::CgInvalid(format!("snapshot is not serializable: {error}"))
    })?;
    if encoded.len() > 48 * 1024 {
        return Err(EngineError::CgInvalid(
            "snapshot exceeds the bounded CG payload".into(),
        ));
    }
    Ok(())
}

fn overlay_asset_id(overlay: &station_media_protocol::CgOverlay) -> Result<String, EngineError> {
    match &overlay.content {
        CgOverlayContent::Image { asset_id, .. } => Ok(asset_id.clone()),
        _ => Err(EngineError::CgInvalid("overlay is not an image".into())),
    }
}

fn configure_image_geometry(
    image: &gst::Element,
    rect: &station_media_protocol::CgRect,
    profile: &OutputProfile,
) -> Result<(), EngineError> {
    let x = u32::from(rect.x) * profile.width / 1000;
    let y = u32::from(rect.y) * profile.height / 1000;
    let width = (u32::from(rect.width) * profile.width / 1000).max(1);
    let height = (u32::from(rect.height) * profile.height / 1000).max(1);
    image.set_property(
        "offset-x",
        i32::try_from(x).map_err(|_| {
            EngineError::CgInvalid("image x geometry exceeds GStreamer range".into())
        })?,
    );
    image.set_property(
        "offset-y",
        i32::try_from(y).map_err(|_| {
            EngineError::CgInvalid("image y geometry exceeds GStreamer range".into())
        })?,
    );
    image.set_property(
        "overlay-width",
        i32::try_from(width)
            .map_err(|_| EngineError::CgInvalid("image width exceeds GStreamer range".into()))?,
    );
    image.set_property(
        "overlay-height",
        i32::try_from(height)
            .map_err(|_| EngineError::CgInvalid("image height exceeds GStreamer range".into()))?,
    );
    Ok(())
}

fn load_managed_cg_image(
    root: &Path,
    asset_id: &str,
    format: CgImageFormat,
) -> Result<Pixbuf, EngineError> {
    if asset_id.len() != 64 || !asset_id.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(EngineError::CgInvalid(
            "invalid managed image identity".into(),
        ));
    }
    let extension = match format {
        CgImageFormat::Png => "png",
        CgImageFormat::Jpeg => "jpg",
    };
    let path = root.join(format!("{}.{}", asset_id.to_ascii_lowercase(), extension));
    let canonical_root = std::fs::canonicalize(root).map_err(|error| {
        EngineError::CgInvalid(format!("managed image root is unreadable: {error}"))
    })?;
    let canonical_path = std::fs::canonicalize(&path)
        .map_err(|error| EngineError::CgInvalid(format!("managed image is unreadable: {error}")))?;
    if canonical_path.parent() != Some(canonical_root.as_path()) {
        return Err(EngineError::CgInvalid(
            "managed image escapes its trusted root".into(),
        ));
    }
    let metadata = std::fs::metadata(&canonical_path).map_err(|error| {
        EngineError::CgInvalid(format!("managed image metadata failed: {error}"))
    })?;
    if !metadata.is_file() || !(1..=8 * 1024 * 1024).contains(&metadata.len()) {
        return Err(EngineError::CgInvalid(
            "managed image must be a regular file between 1 and 8 MiB".into(),
        ));
    }
    let bytes = std::fs::read(&canonical_path)
        .map_err(|error| EngineError::CgInvalid(format!("managed image is unreadable: {error}")))?;
    let digest = format!("{:x}", Sha256::digest(&bytes));
    if digest != asset_id.to_ascii_lowercase() {
        return Err(EngineError::CgInvalid("managed image hash mismatch".into()));
    }
    let detected = image::guess_format(&bytes).map_err(|error| {
        EngineError::CgInvalid(format!("managed image format is invalid: {error}"))
    })?;
    let expected = match format {
        CgImageFormat::Png => image::ImageFormat::Png,
        CgImageFormat::Jpeg => image::ImageFormat::Jpeg,
    };
    if detected != expected {
        return Err(EngineError::CgInvalid(
            "managed image format does not match identity".into(),
        ));
    }
    let mut reader = ImageReader::new(std::io::Cursor::new(bytes.clone()));
    let mut limits = Limits::default();
    limits.max_image_width = Some(4096);
    limits.max_image_height = Some(4096);
    limits.max_alloc = Some(32 * 1024 * 1024);
    reader.limits(limits);
    let decoded = reader
        .with_guessed_format()
        .map_err(|error| {
            EngineError::CgInvalid(format!("managed image format is invalid: {error}"))
        })?
        .decode()
        .map_err(|error| EngineError::CgInvalid(format!("managed image decode failed: {error}")))?;
    if decoded.width() > 4096 || decoded.height() > 4096 {
        return Err(EngineError::CgInvalid(
            "managed image dimensions exceed 4096 pixels".into(),
        ));
    }
    Pixbuf::from_read(std::io::Cursor::new(bytes))
        .map_err(|error| EngineError::CgInvalid(format!("managed image decode failed: {error}")))
}

fn fallback_slate_geometry(
    frame_width: u32,
    frame_height: u32,
    image_width: u32,
    image_height: u32,
) -> Result<(u32, u32, u32, u32), EngineError> {
    if frame_width == 0 || frame_height == 0 || image_width == 0 || image_height == 0 {
        return Err(EngineError::CgInvalid(
            "fallback slate geometry cannot be empty".into(),
        ));
    }
    let (width, height) = if u64::from(frame_width) * u64::from(image_height)
        <= u64::from(frame_height) * u64::from(image_width)
    {
        (
            frame_width,
            u32::try_from(
                u64::from(image_height) * u64::from(frame_width) / u64::from(image_width),
            )
            .map_err(|_| EngineError::CgInvalid("fallback slate height overflow".into()))?
            .max(1),
        )
    } else {
        (
            u32::try_from(
                u64::from(image_width) * u64::from(frame_height) / u64::from(image_height),
            )
            .map_err(|_| EngineError::CgInvalid("fallback slate width overflow".into()))?
            .max(1),
            frame_height,
        )
    };
    Ok((
        (frame_width - width) / 2,
        (frame_height - height) / 2,
        width,
        height,
    ))
}

fn sanitize_text(text: &str) -> Result<String, EngineError> {
    if text.is_empty() {
        return Err(EngineError::CgInvalid(
            "overlay text cannot be empty".into(),
        ));
    }
    if text.chars().any(|character| {
        character.is_control() && character != '\n' && character != '\r' && character != '\t'
    }) {
        return Err(EngineError::CgInvalid(
            "overlay text contains control characters".into(),
        ));
    }
    Ok(text.replace("\r\n", "\n").replace('\r', "\n"))
}

fn configure_overlay_geometry(
    overlay: &gst::Element,
    rect: &station_media_protocol::CgRect,
) -> Result<(), EngineError> {
    overlay.set_property("x-absolute", f64::from(rect.x + rect.width / 2) / 1000.0);
    overlay.set_property("y-absolute", f64::from(rect.y + rect.height / 2) / 1000.0);
    overlay.set_property(
        "font-desc",
        format!("Sans Bold {}", (rect.height / 8).clamp(16, 96)),
    );
    Ok(())
}

fn video_caps_filter(
    name: impl Into<gst::glib::GString>,
    profile: &OutputProfile,
) -> Result<gst::Element, EngineError> {
    let caps = video_caps(profile)?;
    gst::ElementFactory::make("capsfilter")
        .name(name)
        .property("caps", &caps)
        .build()
        .map_err(|error| build_error("capsfilter", error))
}

fn two_second_gop(profile: &OutputProfile) -> u32 {
    profile
        .frame_rate_numerator
        .saturating_mul(2)
        .div_ceil(profile.frame_rate_denominator)
}

fn build_error(factory: &'static str, error: gst::glib::BoolError) -> EngineError {
    EngineError::ElementBuild {
        factory,
        message: error.to_string(),
    }
}

fn link(
    from: &gst::Element,
    to: &gst::Element,
    from_name: &'static str,
    to_name: &'static str,
) -> Result<(), EngineError> {
    from.link(to).map_err(|_| EngineError::Link {
        from: from_name,
        to: to_name,
    })
}

fn connect_source(
    source: &gst::Element,
    selector: &gst::Element,
    source_name: &'static str,
) -> Result<gst::Pad, EngineError> {
    let source_pad = source.static_pad("src").ok_or(EngineError::MissingPad {
        element: source_name,
        pad: "src",
    })?;
    let selector_pad = selector
        .request_pad_simple("sink_%u")
        .ok_or(EngineError::MissingPad {
            element: "input-selector",
            pad: "sink_%u",
        })?;
    source_pad
        .link(&selector_pad)
        .map_err(|error| EngineError::PadLink(format!("{error:?}")))?;
    Ok(selector_pad)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn capture_ingress_byte_reservation_is_bounded_and_releasable() {
        let ingress = CaptureIngress::new();

        assert!(ingress.reserve_queued_bytes(OUTPUT_CAPTURE_QUEUE_BYTES));
        assert!(!ingress.reserve_queued_bytes(1));

        ingress.release_queued_bytes(OUTPUT_CAPTURE_QUEUE_BYTES);
        assert!(ingress.reserve_queued_bytes(1));
    }
}
