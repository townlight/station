use std::net::SocketAddr;
use std::path::Path;
use std::sync::{
    Arc,
    atomic::{AtomicBool, AtomicU64, Ordering},
};
use std::time::{Duration, Instant};

use gst::prelude::*;
use gstreamer as gst;
use station_media_protocol::{CgOverlayContent, CgSnapshot, MAX_CG_SLOTS, MAX_CG_TEXT_BYTES};
use station_schedule::{OutputProfile, OutputProfileId};

static FILE_PROGRAM_SEQUENCE: AtomicU64 = AtomicU64::new(1);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SourceRole {
    Fallback,
    Program,
}

#[derive(Clone, PartialEq, Eq)]
pub enum TransportOutput {
    Udp(SocketAddr),
    Srt {
        uri: String,
        passphrase: Option<String>,
    },
}

#[derive(Debug)]
pub enum EngineError {
    Initialization(String),
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
    LoadRequiresFallback,
    CgInvalid(String),
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
    output_profile: OutputProfileId,
    file_program: Option<FileProgram>,
    cg_slots: Vec<CgSlot>,
    cg_revision: Option<CgSnapshot>,
    stopped: bool,
}

struct CgSlot {
    text_overlay: gst::Element,
    clock_overlay: gst::Element,
    pad: gst::Pad,
}

enum OutputCounter {
    Udp(gst::Element),
    Srt(gst::Element),
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
        if outputs.is_empty() {
            return Err(EngineError::Pipeline(
                "at least one transport output is required".into(),
            ));
        }
        Self::start_outputs_with_program(outputs, None, output_profile)
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
        Self::start_outputs_with_program(
            &[TransportOutput::Udp(destination)],
            Some(media_path.as_ref()),
            output_profile,
        )
    }

    fn start_outputs_with_program(
        outputs: &[TransportOutput],
        media_path: Option<&Path>,
        output_profile: OutputProfileId,
    ) -> Result<Self, EngineError> {
        gst::init().map_err(|error| EngineError::Initialization(error.to_string()))?;
        let profile = output_profile.profile();

        let pipeline = gst::Pipeline::with_name("townlight-synthetic-playout");
        let fallback = gst::ElementFactory::make("videotestsrc")
            .name("fallback-source")
            .property("is-live", true)
            .property_from_str("pattern", "black")
            .build()
            .map_err(|error| build_error("videotestsrc", error))?;
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
        let mut cg_slots = Vec::with_capacity(MAX_CG_SLOTS);
        for slot in 0..MAX_CG_SLOTS {
            let source = gst::ElementFactory::make("videotestsrc")
                .name(format!("cg-source-{slot}"))
                .property("is-live", true)
                .property_from_str("pattern", "black")
                .build()
                .map_err(|error| build_error("videotestsrc", error))?;
            let caps = gst::ElementFactory::make("capsfilter")
                .name(format!("cg-caps-{slot}"))
                .property("caps", &cg_video_caps(profile)?)
                .build()
                .map_err(|error| build_error("capsfilter", error))?;
            let alpha = gst::ElementFactory::make("alpha")
                .name(format!("cg-alpha-{slot}"))
                .property("alpha", 1.0_f64)
                .property_from_str("method", "custom")
                .property("target-r", 0_u32)
                .property("target-g", 0_u32)
                .property("target-b", 0_u32)
                .build()
                .map_err(|error| build_error("alpha", error))?;
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
            let queue = element("queue", format!("cg-queue-{slot}"))?;
            pipeline
                .add_many([
                    &source,
                    &caps,
                    &alpha,
                    &text_overlay,
                    &clock_overlay,
                    &queue,
                ])
                .map_err(|error| EngineError::Pipeline(error.to_string()))?;
            gst::Element::link_many([
                &source,
                &caps,
                &text_overlay,
                &clock_overlay,
                &alpha,
                &queue,
            ])
            .map_err(|_| EngineError::Link {
                from: "cg-source",
                to: "cg-queue",
            })?;
            let pad = connect_to_compositor(&queue, &cg_compositor, 1 + slot)?;
            cg_slots.push(CgSlot {
                text_overlay,
                clock_overlay,
                pad,
            });
        }
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
            .property("tolerance", 0_u64)
            .build()
            .map_err(|error| build_error("audiorate", error))?;
        let audio_output_queue = element("queue", "audio-output-queue")?;
        let audio_encoder = gst::ElementFactory::make("voaacenc")
            .name("audio-encoder")
            .property("bitrate", profile.audio_bitrate_bps as i32)
            .property("perfect-timestamp", true)
            .build()
            .map_err(|error| build_error("voaacenc", error))?;
        let audio_parser = element("aacparse", "audio-parser")?;
        let mux = gst::ElementFactory::make("mpegtsmux")
            .name("transport-mux")
            .property("alignment", 7_i32)
            .property("bitrate", profile.transport_bitrate_bps)
            .property("pat-interval", 9_000_u32)
            .property("pmt-interval", 9_000_u32)
            .property("pcr-interval", 3_600_u32)
            .build()
            .map_err(|error| build_error("mpegtsmux", error))?;
        let transport_fanout = element("tee", "transport-fanout")?;
        let mut output_branches = Vec::with_capacity(outputs.len());
        let mut output_counters = Vec::with_capacity(outputs.len());
        let mut srt_sinks = Vec::new();
        for (index, output) in outputs.iter().enumerate() {
            let queue = element("queue", format!("transport-output-queue-{index}"))?;
            let sink = match output {
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
            match output {
                TransportOutput::Udp(_) => output_counters.push(OutputCounter::Udp(sink.clone())),
                TransportOutput::Srt { .. } => {
                    srt_sinks.push(sink.clone());
                    output_counters.push(OutputCounter::Srt(sink.clone()));
                }
            }
            output_branches.push((queue, sink));
        }

        pipeline
            .add_many([
                &fallback,
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
                &audio_encoder,
                &audio_parser,
                &mux,
                &transport_fanout,
            ])
            .map_err(|error| EngineError::Pipeline(error.to_string()))?;
        for (queue, sink) in &output_branches {
            pipeline
                .add_many([queue, sink])
                .map_err(|error| EngineError::Pipeline(error.to_string()))?;
        }
        link(&fallback, &fallback_queue, "videotestsrc", "queue")?;
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
            &mux,
            &transport_fanout,
        ])
        .map_err(|_| EngineError::Link {
            from: "cg-compositor",
            to: "transport-fanout",
        })?;
        for (queue, sink) in &output_branches {
            gst::Element::link_many([&transport_fanout, queue, sink]).map_err(|_| {
                EngineError::Link {
                    from: "transport-fanout",
                    to: "udpsink",
                }
            })?;
        }
        gst::Element::link_many([
            &audio_selector,
            &audio_timeline,
            &audio_rate,
            &audio_output_queue,
            &audio_encoder,
            &audio_parser,
            &mux,
        ])
        .map_err(|_| EngineError::Link {
            from: "input-selector",
            to: "mpegtsmux",
        })?;

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
        let file_program = media_path
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
        pipeline
            .set_state(gst::State::Playing)
            .map_err(|error| EngineError::State(format!("{error:?}")))?;
        pipeline
            .state(gst::ClockTime::from_seconds(5))
            .0
            .map_err(|error| EngineError::State(format!("{error:?}")))?;
        wait_for_srt_connections(&srt_sinks, &pipeline, Duration::from_secs(5))?;
        if let Some(program) = &file_program {
            wait_for_file_decode(program, &pipeline, Duration::from_secs(5))?;
        }

        let playout = Self {
            pipeline,
            video_selector,
            video_fallback_pad: fallback_pad,
            video_program_pad: program_pad,
            audio_selector,
            audio_fallback_pad,
            audio_program_pad,
            output_counters,
            output_profile,
            file_program,
            cg_slots,
            cg_revision: None,
            stopped: false,
        };
        playout.wait_for_source(SourceRole::Fallback, Duration::from_secs(2))?;
        for slot in &playout.cg_slots {
            slot.pad.set_property("alpha", 0.0_f64);
        }
        Ok(playout)
    }

    pub fn output_bytes_sent(&self) -> Result<Vec<u64>, EngineError> {
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
            .map(|counter| match counter {
                OutputCounter::Udp(sink) => Ok(sink.property::<u64>("bytes-served")),
                OutputCounter::Srt(sink) => sink
                    .property::<gst::Structure>("stats")
                    .get::<u64>("bytes-sent-total")
                    .map_err(|error| {
                        EngineError::State(format!(
                            "could not read required SRT output statistics: {error}"
                        ))
                    }),
            })
            .collect()
    }

    pub fn apply_cg_snapshot(
        &mut self,
        snapshot: &station_media_protocol::CgSnapshot,
    ) -> Result<(), EngineError> {
        validate_cg_snapshot(snapshot)?;
        if self.cg_revision.as_ref() == Some(snapshot) {
            return Ok(());
        }
        for slot in &self.cg_slots {
            slot.pad.set_property("alpha", 0.0_f64);
        }
        for overlay in &snapshot.overlays {
            let slot = &self.cg_slots[usize::from(overlay.slot)];
            configure_overlay_geometry(&slot.text_overlay, &overlay.rect)?;
            configure_overlay_geometry(&slot.clock_overlay, &overlay.rect)?;
            match &overlay.content {
                CgOverlayContent::Text { text } => {
                    slot.text_overlay.set_property("text", sanitize_text(text)?);
                    slot.text_overlay.set_property("silent", false);
                    slot.clock_overlay.set_property("silent", true);
                    slot.pad.set_property(
                        "zorder",
                        u32::try_from(overlay.z_index)
                            .unwrap_or(0)
                            .saturating_add(1),
                    );
                    slot.pad.set_property("alpha", 1.0_f64);
                }
                CgOverlayContent::Clock { format } => {
                    slot.clock_overlay
                        .set_property("time-format", sanitize_text(format)?);
                    slot.text_overlay.set_property("silent", true);
                    slot.clock_overlay.set_property("silent", false);
                    slot.pad.set_property(
                        "zorder",
                        u32::try_from(overlay.z_index)
                            .unwrap_or(0)
                            .saturating_add(1),
                    );
                    slot.pad.set_property("alpha", 1.0_f64);
                }
            }
        }
        self.cg_revision = Some(snapshot.clone());
        Ok(())
    }

    pub fn clear_cg_snapshot(&mut self) {
        for slot in &self.cg_slots {
            slot.pad.set_property("alpha", 0.0_f64);
            slot.text_overlay.set_property("silent", true);
            slot.clock_overlay.set_property("silent", true);
        }
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
        )?;
        let audio = active_role(
            &self.audio_selector,
            &self.audio_fallback_pad,
            &self.audio_program_pad,
        )?;
        if video == audio {
            Ok(video)
        } else {
            Err(EngineError::SourceSelectorsDisagree { video, audio })
        }
    }

    pub fn select(&mut self, role: SourceRole) -> Result<(), EngineError> {
        let (video_pad, audio_pad) = match role {
            SourceRole::Fallback => (&self.video_fallback_pad, &self.audio_fallback_pad),
            SourceRole::Program => (&self.video_program_pad, &self.audio_program_pad),
        };
        self.video_selector
            .set_property("active-pad", Some(video_pad.clone()));
        self.audio_selector
            .set_property("active-pad", Some(audio_pad.clone()));
        self.wait_for_source(role, Duration::from_secs(2))
    }

    pub fn load_file(&mut self, media_path: impl AsRef<Path>) -> Result<(), EngineError> {
        self.load_validated_file(media_path)
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
        if self.active_source()? != SourceRole::Fallback {
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
        let running_time = self
            .pipeline
            .current_running_time()
            .ok_or_else(|| EngineError::State("the running pipeline has no clock time".into()))?;
        let offset = i64::try_from(running_time.nseconds())
            .map_err(|_| EngineError::State("pipeline running time exceeded i64".into()))?;
        program.video_source_pad.set_offset(offset);
        program.audio_source_pad.set_offset(offset);
        for element in &program.elements {
            if let Err(error) = element.sync_state_with_parent() {
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

    pub fn stop(mut self) -> Result<(), EngineError> {
        self.pipeline
            .set_state(gst::State::Null)
            .map_err(|error| EngineError::State(format!("{error:?}")))?;
        self.pipeline
            .state(gst::ClockTime::from_seconds(5))
            .0
            .map_err(|error| EngineError::State(format!("{error:?}")))?;
        self.stopped = true;
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
}

fn wait_for_srt_connections(
    sinks: &[gst::Element],
    pipeline: &gst::Pipeline,
    timeout: Duration,
) -> Result<(), EngineError> {
    if sinks.is_empty() {
        return Ok(());
    }
    let started = Instant::now();
    while sinks.iter().any(|sink| {
        sink.property::<gst::Structure>("stats")
            .get::<u64>("bytes-sent-total")
            .unwrap_or(0)
            == 0
    }) {
        if started.elapsed() >= timeout {
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
    Ok(())
}

struct FileProgram {
    video_pad: gst::Pad,
    audio_pad: gst::Pad,
    video_ready: Arc<AtomicBool>,
    audio_ready: Arc<AtomicBool>,
    video_source_pad: gst::Pad,
    audio_source_pad: gst::Pad,
    elements: Vec<gst::Element>,
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
        .property("tolerance", 0_u64)
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
    for element in &program.elements {
        let _ = element.state(gst::ClockTime::from_seconds(5));
    }
    for element in program.elements.iter().rev() {
        let _ = pipeline.remove(element);
    }
}

fn active_role(
    selector: &gst::Element,
    fallback_pad: &gst::Pad,
    program_pad: &gst::Pad,
) -> Result<SourceRole, EngineError> {
    let active: Option<gst::Pad> = selector.property("active-pad");
    let actual = active.as_ref().map(|pad| pad.name().to_string());
    let fallback = fallback_pad.name().to_string();
    let program = program_pad.name().to_string();
    if actual.as_ref() == Some(&fallback) {
        Ok(SourceRole::Fallback)
    } else if actual.as_ref() == Some(&program) {
        Ok(SourceRole::Program)
    } else {
        Err(EngineError::UnknownActivePad {
            actual,
            fallback,
            program,
        })
    }
}

impl Drop for PersistentPlayout {
    fn drop(&mut self) {
        if !self.stopped {
            let _ = self.pipeline.set_state(gst::State::Null);
        }
    }
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
