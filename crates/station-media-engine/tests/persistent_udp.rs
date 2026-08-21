use std::collections::HashMap;
use std::fs;
use std::net::UdpSocket;
use std::path::Path;
use std::time::{Duration, Instant};

use gst::prelude::*;
use gstreamer as gst;
use gstreamer_pbutils as gst_pbutils;
use station_media_assets::ingest_media;
use station_media_engine::{PersistentPlayout, SourceRole};
use station_schedule::OutputProfileId;

#[test]
fn fans_one_persistent_transport_out_to_two_independent_udp_sinks() {
    let primary = UdpSocket::bind("127.0.0.1:0").unwrap();
    let redundant = UdpSocket::bind("127.0.0.1:0").unwrap();
    for receiver in [&primary, &redundant] {
        receiver
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
    }
    let playout = PersistentPlayout::start_udp_fanout_with_profile(
        &[
            primary.local_addr().unwrap(),
            redundant.local_addr().unwrap(),
        ],
        OutputProfileId::Proof360p30,
    )
    .expect("one graph must start both transport branches");
    let mut primary_truth = ContinuityTracker::default();
    let mut redundant_truth = ContinuityTracker::default();
    assert_transport_stream(&primary, &mut primary_truth, 24);
    assert_transport_stream(&redundant, &mut redundant_truth, 24);
    for truth in [&primary_truth, &redundant_truth] {
        assert!(truth.video_pid.is_some());
        assert!(truth.audio_pid.is_some());
        assert!(truth.video_pts_samples >= 3);
        assert!(truth.audio_pts_samples >= 3);
    }
    playout.stop().unwrap();
}

#[test]
fn repeats_codec_configuration_for_a_receiver_that_joins_after_startup() {
    let primary_reservation = UdpSocket::bind("127.0.0.1:0").unwrap();
    let redundant_reservation = UdpSocket::bind("127.0.0.1:0").unwrap();
    let primary_destination = primary_reservation.local_addr().unwrap();
    let redundant_destination = redundant_reservation.local_addr().unwrap();
    drop(primary_reservation);
    drop(redundant_reservation);
    let playout = PersistentPlayout::start_udp_fanout_with_profile(
        &[primary_destination, redundant_destination],
        OutputProfileId::Atsc720p5994,
    )
    .expect("the persistent transport must start without receivers");

    std::thread::sleep(Duration::from_secs(3));
    let primary = UdpSocket::bind(primary_destination).unwrap();
    let redundant = UdpSocket::bind(redundant_destination).unwrap();
    let (primary_capture, redundant_capture) = std::thread::scope(|scope| {
        let primary_capture =
            scope.spawn(|| capture_transport_for(&primary, Duration::from_secs(5)));
        let redundant_capture =
            scope.spawn(|| capture_transport_for(&redundant, Duration::from_secs(5)));
        (
            primary_capture.join().unwrap(),
            redundant_capture.join().unwrap(),
        )
    });
    playout.stop().unwrap();

    for capture in [&primary_capture, &redundant_capture] {
        assert!(capture.len() > 4_000_000);
        assert_discovered_video_contract(capture, 1_280, 720);
    }
}

#[test]
fn emits_the_selected_720p5994_constant_bitrate_transport_contract() {
    let receiver = UdpSocket::bind("127.0.0.1:0").unwrap();
    receiver
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    let playout = PersistentPlayout::start_udp_with_profile(
        receiver.local_addr().unwrap(),
        OutputProfileId::Atsc720p5994,
    )
    .expect("the 720p59.94 graph must start");

    let mut datagram = [0_u8; 65_536];
    receiver
        .recv(&mut datagram)
        .expect("the profile must emit its first transport datagram");
    let started = Instant::now();
    let mut received_bytes = 0_u64;
    let mut null_packets = 0_u64;
    let mut transport_capture = Vec::new();
    while started.elapsed() < Duration::from_secs(2) {
        let received = receiver.recv(&mut datagram).unwrap();
        received_bytes += received as u64;
        transport_capture.extend_from_slice(&datagram[..received]);
        for packet in datagram[..received].chunks_exact(188) {
            let pid = (u16::from(packet[1] & 0x1f) << 8) | u16::from(packet[2]);
            if pid == 0x1fff {
                null_packets += 1;
            }
        }
    }
    let measured_bitrate =
        received_bytes.saturating_mul(8) as f64 / started.elapsed().as_secs_f64();
    assert!(
        (7_600_000.0..=8_400_000.0).contains(&measured_bitrate),
        "transport bitrate was {measured_bitrate:.0} bps"
    );
    assert!(
        null_packets > 0,
        "the CBR mux emitted no null padding packets"
    );
    playout.stop().unwrap();

    let mut timing = ContinuityTracker::default();
    for packet in transport_capture.chunks_exact(188) {
        timing.observe(packet);
    }
    assert!(
        timing.video_pts_delta_samples >= 100,
        "only {} video frame intervals were observable",
        timing.video_pts_delta_samples
    );
    let average_video_ticks =
        timing.video_pts_delta_total as f64 / timing.video_pts_delta_samples as f64;
    assert!(
        (1_500.0..=1_503.0).contains(&average_video_ticks),
        "average video frame interval was {average_video_ticks:.3} 90 kHz ticks"
    );
    assert_discovered_video_contract(&transport_capture, 1_280, 720);
}

#[test]
fn keeps_one_pipeline_running_while_switching_sources_and_emits_mpeg_ts() {
    let receiver = UdpSocket::bind("127.0.0.1:0").unwrap();
    receiver
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();

    let started = PersistentPlayout::start_udp(receiver.local_addr().unwrap());
    assert!(
        started.is_ok(),
        "persistent playout did not start: {:?}",
        started.err()
    );
    let mut playout = started.unwrap();
    let mut continuity = ContinuityTracker::default();
    assert_transport_stream(&receiver, &mut continuity, 12);
    assert_eq!(playout.active_source().unwrap(), SourceRole::Fallback);

    playout.select(SourceRole::Program).unwrap();
    assert_transport_stream(&receiver, &mut continuity, 12);
    assert_eq!(playout.active_source().unwrap(), SourceRole::Program);

    playout.select(SourceRole::Fallback).unwrap();
    assert_transport_stream(&receiver, &mut continuity, 12);
    assert_eq!(playout.active_source().unwrap(), SourceRole::Fallback);
    assert!(
        continuity.payload_packets > 0,
        "the stream carried no non-null payload packets"
    );
    assert!(
        continuity.video_pid.is_some(),
        "the PMT declared no H.264 video"
    );
    assert!(
        continuity.audio_pid.is_some(),
        "the PMT declared no AAC audio"
    );
    assert!(
        continuity.video_pts_samples >= 3,
        "too few video PTS samples: {}",
        continuity.video_pts_samples
    );
    assert!(
        continuity.audio_pts_samples >= 3,
        "too few audio PTS samples: {}",
        continuity.audio_pts_samples
    );
    assert!(
        continuity.max_av_delta_ticks <= 22_500,
        "A/V timing diverged by {} ms",
        continuity.max_av_delta_ticks * 1_000 / 90_000
    );
    playout.stop().unwrap();
}

#[test]
fn plays_a_validated_ingested_file_without_rebuilding_the_output_graph() {
    let root = test_root("file-playout");
    let source = root.join("incoming.ts");
    let second_source = root.join("second.ts");
    let library = root.join("library");
    fs::create_dir_all(&root).unwrap();
    generate_fixture(&source, "smpte");
    generate_fixture(&second_source, "snow");
    let asset = ingest_media(&source, &library).expect("the real A/V file must ingest");
    let second_asset =
        ingest_media(&second_source, &library).expect("the second real A/V file must ingest");
    assert_ne!(asset.asset_id, second_asset.asset_id);

    let receiver = UdpSocket::bind("127.0.0.1:0").unwrap();
    receiver
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let mut playout = PersistentPlayout::start_udp(receiver.local_addr().unwrap())
        .expect("the persistent fallback graph must start");
    let mut continuity = ContinuityTracker::default();

    assert_transport_stream(&receiver, &mut continuity, 12);
    assert_eq!(playout.active_source().unwrap(), SourceRole::Fallback);
    playout
        .load_file(&asset.stored_path)
        .expect("the validated stored asset must load into the running graph");
    playout.select(SourceRole::Program).unwrap();
    assert_transport_stream(&receiver, &mut continuity, 36);
    assert_eq!(playout.active_source().unwrap(), SourceRole::Program);
    playout.select(SourceRole::Fallback).unwrap();
    assert_transport_stream(&receiver, &mut continuity, 12);
    assert_eq!(playout.active_source().unwrap(), SourceRole::Fallback);
    playout
        .load_file(&second_asset.stored_path)
        .expect("a second stored asset must replace the prepared program leg");
    playout.select(SourceRole::Program).unwrap();
    assert_transport_stream(&receiver, &mut continuity, 36);
    assert_eq!(playout.active_source().unwrap(), SourceRole::Program);
    playout.select(SourceRole::Fallback).unwrap();
    assert_transport_stream(&receiver, &mut continuity, 12);
    assert_eq!(playout.active_source().unwrap(), SourceRole::Fallback);
    assert!(continuity.video_pts_samples >= 3);
    assert!(continuity.audio_pts_samples >= 3);
    assert!(continuity.max_av_delta_ticks <= 22_500);

    playout.stop().unwrap();
    fs::remove_dir_all(root).unwrap();
}

fn assert_discovered_video_contract(
    transport_stream: &[u8],
    expected_width: i32,
    expected_height: i32,
) {
    gst::init().unwrap();
    let path = test_root("profile-contract").with_extension("ts");
    fs::write(&path, transport_stream).unwrap();
    let canonical = fs::canonicalize(&path).unwrap();
    let uri = gst::glib::filename_to_uri(&canonical, None).unwrap();
    let discoverer = gst_pbutils::Discoverer::new(gst::ClockTime::from_seconds(15)).unwrap();
    let info = discoverer.discover_uri(uri.as_str()).unwrap();
    assert_eq!(info.result(), gst_pbutils::DiscovererResult::Ok);
    let video = info.video_streams();
    assert_eq!(video.len(), 1);
    assert_eq!(video[0].width(), expected_width as u32);
    assert_eq!(video[0].height(), expected_height as u32);
    fs::remove_file(path).unwrap();
}

fn assert_transport_stream(
    receiver: &UdpSocket,
    continuity: &mut ContinuityTracker,
    datagrams: usize,
) {
    let mut datagram = [0_u8; 65_536];
    for _ in 0..datagrams {
        let received = receiver.recv(&mut datagram).unwrap();
        assert!(received >= 188, "received only {received} MPEG-TS bytes");
        assert_eq!(received % 188, 0, "UDP payload was not whole TS packets");
        for packet in datagram[..received].chunks_exact(188) {
            continuity.observe(packet);
        }
    }
}

fn capture_transport_for(receiver: &UdpSocket, duration: Duration) -> Vec<u8> {
    receiver
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    let started = Instant::now();
    let mut datagram = [0_u8; 65_536];
    let mut capture = Vec::new();
    while started.elapsed() < duration {
        let received = receiver.recv(&mut datagram).unwrap();
        capture.extend_from_slice(&datagram[..received]);
    }
    capture
}

#[derive(Default)]
struct ContinuityTracker {
    last_payload_counter: HashMap<u16, u8>,
    last_pts: HashMap<u16, u64>,
    payload_packets: usize,
    pmt_pid: Option<u16>,
    video_pid: Option<u16>,
    audio_pid: Option<u16>,
    latest_video_pts: Option<u64>,
    latest_audio_pts: Option<u64>,
    video_pts_samples: usize,
    video_pts_delta_samples: usize,
    video_pts_delta_total: u64,
    audio_pts_samples: usize,
    max_av_delta_ticks: u64,
}

impl ContinuityTracker {
    fn observe(&mut self, packet: &[u8]) {
        assert_eq!(packet.len(), 188);
        assert_eq!(packet[0], 0x47, "MPEG-TS sync byte was missing");
        assert_eq!(packet[1] & 0x80, 0, "transport-error indicator was set");
        let pid = (u16::from(packet[1] & 0x1f) << 8) | u16::from(packet[2]);
        let adaptation_control = (packet[3] >> 4) & 0x03;
        assert_ne!(adaptation_control, 0, "reserved adaptation-field control");
        if matches!(adaptation_control, 2 | 3) && packet[4] > 0 {
            assert_eq!(
                packet[5] & 0x80,
                0,
                "PID {pid:#06x} declared a continuity discontinuity"
            );
        }
        let payload = payload(packet, adaptation_control);
        let payload_unit_start = packet[1] & 0x40 != 0;
        if payload_unit_start {
            if pid == 0 {
                self.observe_pat(payload);
            } else if Some(pid) == self.pmt_pid {
                self.observe_pmt(payload);
            } else if Some(pid) == self.video_pid || Some(pid) == self.audio_pid {
                self.observe_pts(pid, payload);
            }
        }
        if pid == 0x1fff || !matches!(adaptation_control, 1 | 3) {
            return;
        }

        let counter = packet[3] & 0x0f;
        if let Some(previous) = self.last_payload_counter.insert(pid, counter) {
            assert_eq!(
                counter,
                (previous + 1) & 0x0f,
                "PID {pid:#06x} continuity jumped from {previous} to {counter}"
            );
        }
        self.payload_packets += 1;
    }

    fn observe_pat(&mut self, payload: Option<&[u8]>) {
        let Some(section) = psi_section(payload) else {
            return;
        };
        if section.len() < 12 || section[0] != 0x00 {
            return;
        }
        let section_length = (usize::from(section[1] & 0x0f) << 8) | usize::from(section[2]);
        let end = 3 + section_length.saturating_sub(4);
        if end > section.len() {
            return;
        }
        for program in section[8..end].chunks_exact(4) {
            let program_number = u16::from_be_bytes([program[0], program[1]]);
            if program_number != 0 {
                self.pmt_pid = Some((u16::from(program[2] & 0x1f) << 8) | u16::from(program[3]));
                return;
            }
        }
    }

    fn observe_pmt(&mut self, payload: Option<&[u8]>) {
        let Some(section) = psi_section(payload) else {
            return;
        };
        if section.len() < 16 || section[0] != 0x02 {
            return;
        }
        let section_length = (usize::from(section[1] & 0x0f) << 8) | usize::from(section[2]);
        let end = 3 + section_length.saturating_sub(4);
        let program_info_length = (usize::from(section[10] & 0x0f) << 8) | usize::from(section[11]);
        let mut offset = 12 + program_info_length;
        while offset + 5 <= end && offset + 5 <= section.len() {
            let stream_type = section[offset];
            let pid = (u16::from(section[offset + 1] & 0x1f) << 8) | u16::from(section[offset + 2]);
            match stream_type {
                0x1b => self.video_pid = Some(pid),
                0x0f | 0x11 => self.audio_pid = Some(pid),
                _ => {}
            }
            let info_length =
                (usize::from(section[offset + 3] & 0x0f) << 8) | usize::from(section[offset + 4]);
            offset += 5 + info_length;
        }
    }

    fn observe_pts(&mut self, pid: u16, payload: Option<&[u8]>) {
        let Some(pts) = payload.and_then(pes_pts) else {
            return;
        };
        let previous = self.last_pts.insert(pid, pts);
        if let Some(previous) = previous {
            assert!(
                pts >= previous,
                "PID {pid:#06x} PTS moved backward from {previous} to {pts}"
            );
        }
        if Some(pid) == self.video_pid {
            if let Some(previous) = previous
                && pts > previous
            {
                self.video_pts_delta_samples += 1;
                self.video_pts_delta_total += pts - previous;
            }
            self.latest_video_pts = Some(pts);
            self.video_pts_samples += 1;
        }
        if Some(pid) == self.audio_pid {
            self.latest_audio_pts = Some(pts);
            self.audio_pts_samples += 1;
        }
        if let (Some(video), Some(audio)) = (self.latest_video_pts, self.latest_audio_pts) {
            self.max_av_delta_ticks = self.max_av_delta_ticks.max(video.abs_diff(audio));
        }
    }
}

fn payload(packet: &[u8], adaptation_control: u8) -> Option<&[u8]> {
    let offset = match adaptation_control {
        1 => 4,
        3 => 5 + usize::from(packet[4]),
        _ => return None,
    };
    (offset < packet.len()).then_some(&packet[offset..])
}

fn psi_section(payload: Option<&[u8]>) -> Option<&[u8]> {
    let payload = payload?;
    let offset = 1 + usize::from(*payload.first()?);
    (offset < payload.len()).then_some(&payload[offset..])
}

fn pes_pts(payload: &[u8]) -> Option<u64> {
    if payload.len() < 14 || payload[..3] != [0, 0, 1] || payload[7] & 0x80 == 0 {
        return None;
    }
    let value = &payload[9..14];
    Some(
        (u64::from((value[0] >> 1) & 0x07) << 30)
            | (u64::from(value[1]) << 22)
            | (u64::from((value[2] >> 1) & 0x7f) << 15)
            | (u64::from(value[3]) << 7)
            | u64::from((value[4] >> 1) & 0x7f),
    )
}

fn generate_fixture(path: &Path, pattern: &'static str) {
    gst::init().unwrap();
    let pipeline = gst::Pipeline::new();
    let video = gst::ElementFactory::make("videotestsrc")
        .property("num-buffers", 180_i32)
        .property_from_str("pattern", pattern)
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
        .property("num-buffers", 282_i32)
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
    for message in bus.iter_timed(gst::ClockTime::from_seconds(10)) {
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

fn test_root(label: &str) -> std::path::PathBuf {
    std::env::temp_dir().join(format!(
        "townlight-engine-{label}-{}-{}",
        std::process::id(),
        std::thread::current().name().unwrap_or("test")
    ))
}
