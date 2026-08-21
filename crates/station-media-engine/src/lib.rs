use std::net::SocketAddr;
use std::time::{Duration, Instant};

use gst::prelude::*;
use gstreamer as gst;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SourceRole {
    Fallback,
    Program,
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
}

pub struct SyntheticPlayout {
    pipeline: gst::Pipeline,
    video_selector: gst::Element,
    video_fallback_pad: gst::Pad,
    video_program_pad: gst::Pad,
    audio_selector: gst::Element,
    audio_fallback_pad: gst::Pad,
    audio_program_pad: gst::Pad,
    stopped: bool,
}

impl SyntheticPlayout {
    pub fn start_udp(destination: SocketAddr) -> Result<Self, EngineError> {
        gst::init().map_err(|error| EngineError::Initialization(error.to_string()))?;

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
        let convert = element("videoconvert", "output-convert")?;
        let caps = gst::Caps::builder("video/x-raw")
            .field("format", "I420")
            .field("width", 640_i32)
            .field("height", 360_i32)
            .field("framerate", gst::Fraction::new(30, 1))
            .build();
        let caps_filter = gst::ElementFactory::make("capsfilter")
            .name("canonical-video")
            .property("caps", &caps)
            .build()
            .map_err(|error| build_error("capsfilter", error))?;
        let encoder = element("openh264enc", "video-encoder")?;
        let parser = gst::ElementFactory::make("h264parse")
            .name("video-parser")
            .property("config-interval", -1_i32)
            .build()
            .map_err(|error| build_error("h264parse", error))?;
        let fallback_audio = gst::ElementFactory::make("audiotestsrc")
            .name("fallback-audio-source")
            .property("is-live", true)
            .property_from_str("wave", "silence")
            .build()
            .map_err(|error| build_error("audiotestsrc", error))?;
        let fallback_audio_convert = element("audioconvert", "fallback-audio-convert")?;
        let fallback_audio_resample = element("audioresample", "fallback-audio-resample")?;
        let fallback_audio_caps = audio_caps_filter("fallback-canonical-audio")?;
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
        let program_audio_caps = audio_caps_filter("program-canonical-audio")?;
        let program_audio_queue = element("queue", "program-audio-queue")?;
        let audio_selector = selector("audio-source-selector")?;
        let audio_rate = gst::ElementFactory::make("audiorate")
            .name("canonical-audio-rate")
            .property("skip-to-first", true)
            .property("tolerance", 0_u64)
            .build()
            .map_err(|error| build_error("audiorate", error))?;
        let audio_output_queue = element("queue", "audio-output-queue")?;
        let audio_encoder = gst::ElementFactory::make("voaacenc")
            .name("audio-encoder")
            .property("bitrate", 128_000_i32)
            .property("perfect-timestamp", true)
            .build()
            .map_err(|error| build_error("voaacenc", error))?;
        let audio_parser = element("aacparse", "audio-parser")?;
        let mux = gst::ElementFactory::make("mpegtsmux")
            .name("transport-mux")
            .property("alignment", 7_i32)
            .build()
            .map_err(|error| build_error("mpegtsmux", error))?;
        let sink = gst::ElementFactory::make("udpsink")
            .name("udp-output")
            .property("host", destination.ip().to_string())
            .property("port", destination.port() as i32)
            .property("sync", false)
            .property("async", false)
            .build()
            .map_err(|error| build_error("udpsink", error))?;

        pipeline
            .add_many([
                &fallback,
                &fallback_queue,
                &program,
                &program_queue,
                &video_selector,
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
                &audio_rate,
                &audio_output_queue,
                &audio_encoder,
                &audio_parser,
                &mux,
                &sink,
            ])
            .map_err(|error| EngineError::Pipeline(error.to_string()))?;
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
            &convert,
            &caps_filter,
            &encoder,
            &parser,
            &mux,
            &sink,
        ])
        .map_err(|_| EngineError::Link {
            from: "input-selector",
            to: "udpsink",
        })?;
        gst::Element::link_many([
            &audio_selector,
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
        let program_pad = connect_source(&program_queue, &video_selector, "program-queue")?;
        let audio_fallback_pad = connect_source(
            &fallback_audio_queue,
            &audio_selector,
            "fallback-audio-queue",
        )?;
        let audio_program_pad =
            connect_source(&program_audio_queue, &audio_selector, "program-audio-queue")?;
        video_selector.set_property("active-pad", Some(fallback_pad.clone()));
        audio_selector.set_property("active-pad", Some(audio_fallback_pad.clone()));
        pipeline
            .set_state(gst::State::Playing)
            .map_err(|error| EngineError::State(format!("{error:?}")))?;
        pipeline
            .state(gst::ClockTime::from_seconds(5))
            .0
            .map_err(|error| EngineError::State(format!("{error:?}")))?;

        let playout = Self {
            pipeline,
            video_selector,
            video_fallback_pad: fallback_pad,
            video_program_pad: program_pad,
            audio_selector,
            audio_fallback_pad,
            audio_program_pad,
            stopped: false,
        };
        playout.wait_for_source(SourceRole::Fallback, Duration::from_secs(2))?;
        Ok(playout)
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

    pub fn stop(mut self) -> Result<(), EngineError> {
        self.pipeline
            .set_state(gst::State::Null)
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

impl Drop for SyntheticPlayout {
    fn drop(&mut self) {
        if !self.stopped {
            let _ = self.pipeline.set_state(gst::State::Null);
        }
    }
}

fn element(factory: &'static str, name: &'static str) -> Result<gst::Element, EngineError> {
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

fn audio_caps_filter(name: &'static str) -> Result<gst::Element, EngineError> {
    let caps = gst::Caps::builder("audio/x-raw")
        .field("format", "S16LE")
        .field("layout", "interleaved")
        .field("rate", 48_000_i32)
        .field("channels", 2_i32)
        .build();
    gst::ElementFactory::make("capsfilter")
        .name(name)
        .property("caps", &caps)
        .build()
        .map_err(|error| build_error("capsfilter", error))
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
