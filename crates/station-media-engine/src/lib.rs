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
    SwitchTimedOut {
        requested: SourceRole,
        actual: Option<SourceRole>,
    },
}

pub struct SyntheticPlayout {
    pipeline: gst::Pipeline,
    selector: gst::Element,
    fallback_pad: gst::Pad,
    program_pad: gst::Pad,
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
        let selector = gst::ElementFactory::make("input-selector")
            .name("source-selector")
            .property("sync-streams", true)
            .property("cache-buffers", true)
            .build()
            .map_err(|error| build_error("input-selector", error))?;
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
                &selector,
                &convert,
                &caps_filter,
                &encoder,
                &parser,
                &mux,
                &sink,
            ])
            .map_err(|error| EngineError::Pipeline(error.to_string()))?;
        link(&fallback, &fallback_queue, "videotestsrc", "queue")?;
        link(&program, &program_queue, "videotestsrc", "queue")?;
        gst::Element::link_many([
            &selector,
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

        let fallback_pad = connect_source(&fallback_queue, &selector, "fallback-queue")?;
        let program_pad = connect_source(&program_queue, &selector, "program-queue")?;
        pipeline
            .set_state(gst::State::Playing)
            .map_err(|error| EngineError::State(format!("{error:?}")))?;
        pipeline
            .state(gst::ClockTime::from_seconds(5))
            .0
            .map_err(|error| EngineError::State(format!("{error:?}")))?;
        selector.set_property("active-pad", Some(fallback_pad.clone()));

        let playout = Self {
            pipeline,
            selector,
            fallback_pad,
            program_pad,
            stopped: false,
        };
        playout.wait_for_source(SourceRole::Fallback, Duration::from_secs(2))?;
        Ok(playout)
    }

    pub fn active_source(&self) -> Result<SourceRole, EngineError> {
        let active: Option<gst::Pad> = self.selector.property("active-pad");
        let actual = active.as_ref().map(|pad| pad.name().to_string());
        let fallback = self.fallback_pad.name().to_string();
        let program = self.program_pad.name().to_string();
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

    pub fn select(&mut self, role: SourceRole) -> Result<(), EngineError> {
        let pad = match role {
            SourceRole::Fallback => &self.fallback_pad,
            SourceRole::Program => &self.program_pad,
        };
        self.selector.set_property("active-pad", Some(pad.clone()));
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
