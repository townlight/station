use std::collections::HashMap;
use std::fs;
use std::io::Cursor;
use std::net::UdpSocket;
use std::ops::Deref;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc::TryRecvError;
use std::sync::{Mutex, MutexGuard};
use std::time::{Duration, Instant};

use gst::prelude::*;
use gstreamer as gst;
use gstreamer_pbutils as gst_pbutils;
use image::{ImageBuffer, ImageFormat, Rgba};
use sha2::{Digest, Sha256};
use station_captions::canonical_caption_text_sha256;
use station_media_assets::{decode_output_captions, ingest_media};
use station_media_engine::{
    ConfiguredTransportOutput, EngineError, OutputCaptureRequest, OutputCaptureStatus,
    PersistentPlayout, SourceRole, TransportOutput,
};
use station_media_protocol::{
    CaptionCue, CgImageFormat, CgOverlay, CgOverlayContent, CgRect, CgSnapshot, FallbackToneKind,
    LoudnessTarget, WorkerCaptionMode, WorkerCaptionPolicy, WorkerFallbackSlate,
    WorkerFallbackTone,
};
use station_schedule::OutputProfileId;

static PERSISTENT_GRAPH_TEST_LOCK: Mutex<()> = Mutex::new(());

fn persistent_graph_test_guard() -> MutexGuard<'static, ()> {
    PERSISTENT_GRAPH_TEST_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

#[test]
fn fans_one_persistent_transport_out_to_two_independent_udp_sinks() {
    let _guard = persistent_graph_test_guard();
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
    let first_counters = playout.output_bytes_sent().unwrap();
    std::thread::sleep(Duration::from_millis(300));
    let second_counters = playout.output_bytes_sent().unwrap();
    assert_eq!(first_counters.len(), 2);
    assert!(
        second_counters
            .iter()
            .zip(first_counters)
            .all(|(current, previous)| *current > previous),
        "every UDP output counter must advance while the graph is healthy"
    );
    for truth in [&primary_truth, &redundant_truth] {
        assert!(truth.video_pid.is_some());
        assert!(truth.audio_pid.is_some());
        assert!(truth.video_pts_samples >= 3);
        assert!(truth.audio_pts_samples >= 3);
    }
    playout.stop().unwrap();
}

#[test]
fn fallback_tone_reuses_the_running_fallback_audio_branch() {
    let _guard = persistent_graph_test_guard();
    let receiver = UdpSocket::bind("127.0.0.1:0").unwrap();
    receiver
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let mut playout = PersistentPlayout::start_udp_fanout_with_profile(
        &[receiver.local_addr().unwrap()],
        OutputProfileId::Proof360p30,
    )
    .expect("persistent fallback graph starts");
    let before = playout.output_bytes_sent().unwrap()[0];
    playout
        .activate_fallback_tone(&WorkerFallbackTone {
            activation_id: "1c63ef7e-6e4b-4fa6-b5d8-c15e2b735c05".into(),
            authority_revision: 1,
            kind: FallbackToneKind::Test,
            frequency_hz: 1_000,
            level_milli_dbfs: -20_000,
            duration_ms: 1_000,
        })
        .expect("tone changes only the existing fallback source");
    std::thread::sleep(Duration::from_millis(100));
    assert!(playout.output_bytes_sent().unwrap()[0] > before);
    playout.deactivate_fallback_tone();
    assert_eq!(playout.active_source().unwrap(), SourceRole::Fallback);
    playout.stop().unwrap();
}

#[test]
fn decoded_bounded_fallback_tone_has_its_requested_frequency_and_stays_silent_after_a_source_round_trip()
 {
    let _guard = persistent_graph_test_guard();
    let root = test_root("fallback-tone-audio-proof");
    fs::create_dir_all(&root).unwrap();
    let source = root.join("program.ts");
    generate_fixture(&source, "smpte");
    let asset = ingest_media(&source, root.join("library")).unwrap();
    let receiver = UdpSocket::bind("127.0.0.1:0").unwrap();
    receiver
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let tone_capture_path = root.join("tone.ts");
    let rejected_capture_path = root.join("rejected-tone.ts");
    let silent_capture_path = root.join("silent-after-transition.ts");
    let mut playout = PersistentPlayout::start_udp_with_profile(
        receiver.local_addr().unwrap(),
        OutputProfileId::Proof360p30,
    )
    .unwrap();

    assert!(
        playout
            .activate_fallback_tone(&WorkerFallbackTone {
                activation_id: "1c63ef7e-6e4b-4fa6-b5d8-c15e2b735c05".into(),
                authority_revision: 7,
                kind: FallbackToneKind::Test,
                frequency_hz: 1_501,
                level_milli_dbfs: -12_000,
                duration_ms: 1_000,
            })
            .is_err()
    );
    std::thread::sleep(Duration::from_millis(350));
    clear_udp_backlog(&receiver);
    fs::write(
        &rejected_capture_path,
        capture_transport_for(&receiver, Duration::from_secs(2)),
    )
    .unwrap();
    assert!(
        mean_absolute_sample(&decode_transport_audio(&rejected_capture_path)) < 100.0,
        "a rejected tone leaked non-silent fallback audio before validation completed"
    );

    playout
        .activate_fallback_tone(&WorkerFallbackTone {
            activation_id: "1c63ef7e-6e4b-4fa6-b5d8-c15e2b735c05".into(),
            authority_revision: 7,
            kind: FallbackToneKind::Test,
            frequency_hz: 1_000,
            level_milli_dbfs: -20_000,
            duration_ms: 1_000,
        })
        .expect("the bounded tone is rendered by the existing fallback branch");
    std::thread::sleep(Duration::from_millis(350));
    clear_udp_backlog(&receiver);
    fs::write(
        &tone_capture_path,
        capture_transport_for(&receiver, Duration::from_secs(3)),
    )
    .unwrap();
    let tone_samples = decode_transport_audio(&tone_capture_path);
    let tone_mean_abs = maximum_half_second_mean_absolute_sample(&tone_samples);
    let tone_frequency_hz = dominant_left_channel_frequency_hz(&tone_samples, 48_000);
    assert!(
        (900.0..=1_100.0).contains(&tone_frequency_hz),
        "decoded fallback tone frequency was {tone_frequency_hz:.1}Hz, not the requested 1000Hz"
    );
    assert!(
        (500.0..=10_000.0).contains(&tone_mean_abs),
        "decoded fallback tone amplitude was {tone_mean_abs:.1}, outside the bounded audible range"
    );

    prepare_while_draining_output(&receiver, &mut playout, |playout| {
        playout.load_file(&asset.stored_path)
    })
    .unwrap();
    playout.select(SourceRole::Program).unwrap();
    assert_eq!(playout.active_source().unwrap(), SourceRole::Program);
    playout.select(SourceRole::Fallback).unwrap();
    assert_eq!(playout.active_source().unwrap(), SourceRole::Fallback);
    std::thread::sleep(Duration::from_millis(350));
    clear_udp_backlog(&receiver);
    fs::write(
        &silent_capture_path,
        capture_transport_for(&receiver, Duration::from_secs(2)),
    )
    .unwrap();
    let silent_mean_abs = mean_absolute_sample(&decode_transport_audio(&silent_capture_path));
    assert!(
        // AAC transport decode retains a small codec-noise floor even for the
        // audiotestsrc silence waveform; this is materially below the bounded
        // tone's measured half-second amplitude.
        silent_mean_abs < 350.0,
        "a stale fallback tone reappeared after the source transition: decoded mean amplitude {silent_mean_abs:.1}"
    );

    playout.stop().unwrap();
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn accepts_a_typed_caption_cue_without_interrupting_transport() {
    let _guard = persistent_graph_test_guard();
    let receiver = UdpSocket::bind("127.0.0.1:0").unwrap();
    receiver
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let root = test_root("cea708-output-proof");
    fs::create_dir_all(&root).unwrap();
    let proof_root = root.join("output-proofs");
    let sink_id = "7ef1430d-a35c-49f7-91f0-d03cc96bf4af";
    let mut playout =
        PersistentPlayout::start_configured_outputs_with_profile_roots_and_authorities(
            &[ConfiguredTransportOutput::udp_identified(
                sink_id,
                receiver.local_addr().unwrap(),
                LoudnessTarget::CABLE,
            )],
            OutputProfileId::Proof360p30,
            None,
            Some(&proof_root),
            None,
            Some(&WorkerCaptionPolicy {
                mode: WorkerCaptionMode::Cea708,
                service_number: 1,
                language: "en-US".into(),
                revision: 1,
            }),
        )
        .expect("caption-capable output graph starts");
    let before = playout.output_bytes_sent().unwrap()[0];
    let proof_id = "a1f80660-c73a-4ac3-b815-ed9e863c14e6";
    playout
        .start_output_capture(OutputCaptureRequest {
            proof_id: proof_id.into(),
            output_index: 0,
            sink_id: sink_id.into(),
            loudness_target: LoudnessTarget::CABLE,
            duration_ms: 4_000,
            max_bytes: 8 * 1024 * 1024,
        })
        .expect("exact post-mux caption capture starts");
    playout
        .push_caption_cue(&CaptionCue {
            cue_id: "4fefcd4e-cfa1-4f62-a810-3d4fa53d2cb6".into(),
            start_after_ms: 250,
            duration_ms: 1_500,
            text: "TownLight CEA-708 proof".into(),
        })
        .expect("the bounded caption cue enters the persistent graph");
    let deadline = Instant::now() + Duration::from_secs(12);
    let ready = loop {
        assert!(Instant::now() < deadline, "caption capture did not finish");
        match playout.poll_output_capture(proof_id).unwrap() {
            OutputCaptureStatus::Pending { .. } => {
                std::thread::sleep(Duration::from_millis(50));
            }
            OutputCaptureStatus::Ready(ready) => break ready,
            OutputCaptureStatus::Failed { code, message, .. } => {
                panic!("caption capture failed: {code}: {message}")
            }
        }
    };
    let after = playout.output_bytes_sent().unwrap()[0];
    assert!(after > before, "caption injection must not stall transport");
    playout.stop().unwrap();
    let artifact = proof_root.join(&ready.artifact_relative_path);
    assert!(artifact.is_file(), "caption proof artifact must exist");
    let decoded = decode_output_captions(&artifact, ffmpeg_executable())
        .expect("independent bounded caption decode-back succeeds");
    let expected = canonical_caption_text_sha256(&["TownLight CEA-708 proof".into()]).unwrap();
    assert_eq!(decoded.cue_count, 1);
    assert_eq!(decoded.text_sha256, expected);
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn clears_a_prequeued_caption_across_a_program_to_fallback_transition_without_stalling_transport() {
    let _guard = persistent_graph_test_guard();
    let root = test_root("caption-transition-clear");
    let source = root.join("incoming.ts");
    let library = root.join("library");
    let proof_root = root.join("output-proofs");
    fs::create_dir_all(&root).unwrap();
    generate_fixture(&source, "smpte");
    let asset = ingest_media(&source, &library).expect("the real A/V file must ingest");

    let receiver = UdpSocket::bind("127.0.0.1:0").unwrap();
    receiver
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let sink_id = "7ef1430d-a35c-49f7-91f0-d03cc96bf4af";
    let mut playout =
        PersistentPlayout::start_configured_outputs_with_profile_roots_and_authorities(
            &[ConfiguredTransportOutput::udp_identified(
                sink_id,
                receiver.local_addr().unwrap(),
                LoudnessTarget::CABLE,
            )],
            OutputProfileId::Proof360p30,
            None,
            Some(&proof_root),
            None,
            Some(&WorkerCaptionPolicy {
                mode: WorkerCaptionMode::Cea708,
                service_number: 1,
                language: "en-US".into(),
                revision: 1,
            }),
        )
        .expect("caption-capable output graph starts");
    prepare_while_draining_output(&receiver, &mut playout, |playout| {
        playout.load_asset(&asset.asset_id, &asset.stored_path)
    })
    .expect("the validated stored asset must load");
    playout.take_program_at(0).unwrap();

    let proof_id = "a1f80660-c73a-4ac3-b815-ed9e863c14e7";
    playout
        .start_output_capture(OutputCaptureRequest {
            proof_id: proof_id.into(),
            output_index: 0,
            sink_id: sink_id.into(),
            loudness_target: LoudnessTarget::CABLE,
            duration_ms: 4_000,
            max_bytes: 8 * 1024 * 1024,
        })
        .expect("post-mux capture starts before the source transition");
    playout
        .push_caption_cue(&CaptionCue {
            cue_id: "4fefcd4e-cfa1-4f62-a810-3d4fa53d2cb7".into(),
            start_after_ms: 1_200,
            duration_ms: 1_000,
            text: "stale program caption".into(),
        })
        .expect("the future program caption queues");
    let bytes_before_switch = playout.output_bytes_sent().unwrap()[0];
    playout
        .clear_caption_cues()
        .expect("caption-only reset is accepted before the source transition");
    playout.select(SourceRole::Fallback).unwrap();
    playout
        .push_caption_cue(&CaptionCue {
            cue_id: "4fefcd4e-cfa1-4f62-a810-3d4fa53d2cb8".into(),
            start_after_ms: 250,
            duration_ms: 1_000,
            text: "post-switch caption".into(),
        })
        .expect("the caption branch accepts a cue after its clear");

    let deadline = Instant::now() + Duration::from_secs(12);
    let ready = loop {
        assert!(Instant::now() < deadline, "caption capture did not finish");
        match playout.poll_output_capture(proof_id).unwrap() {
            OutputCaptureStatus::Pending { .. } => std::thread::sleep(Duration::from_millis(50)),
            OutputCaptureStatus::Ready(ready) => break ready,
            OutputCaptureStatus::Failed { code, message, .. } => {
                panic!("caption capture failed: {code}: {message}")
            }
        }
    };
    assert!(
        playout.output_bytes_sent().unwrap()[0] > bytes_before_switch,
        "caption clear must not stall post-switch transport"
    );
    playout.stop().unwrap();
    let artifact = proof_root.join(&ready.artifact_relative_path);
    let decoded = decode_output_captions(&artifact, ffmpeg_executable())
        .expect("independent caption decode-back succeeds");
    let expected = canonical_caption_text_sha256(&["post-switch caption".into()]).unwrap();
    assert_eq!(decoded.cue_count, 1, "a pre-switch cue must not leak");
    assert_eq!(decoded.text_sha256, expected);
    fs::remove_dir_all(root).unwrap();
}

fn ffmpeg_executable() -> PathBuf {
    if let Some(path) = std::env::var_os("TOWNLIGHT_FFMPEG") {
        return PathBuf::from(path);
    }
    let located = Command::new("where.exe")
        .arg("ffmpeg.exe")
        .output()
        .expect("where.exe locates FFmpeg");
    assert!(
        located.status.success(),
        "FFmpeg must be installed for proof tests"
    );
    String::from_utf8(located.stdout)
        .expect("where.exe output is UTF-8")
        .lines()
        .find(|line| !line.trim().is_empty())
        .map(|line| PathBuf::from(line.trim()))
        .expect("where.exe returned an FFmpeg path")
}

#[test]
fn disabled_bootstrap_caption_policy_rejects_cues() {
    let _guard = persistent_graph_test_guard();
    let receiver = UdpSocket::bind("127.0.0.1:0").unwrap();
    let mut playout =
        PersistentPlayout::start_configured_outputs_with_profile_roots_and_authorities(
            &[ConfiguredTransportOutput::udp_identified(
                "7ef1430d-a35c-49f7-91f0-d03cc96bf4af",
                receiver.local_addr().unwrap(),
                LoudnessTarget::CABLE,
            )],
            OutputProfileId::Proof360p30,
            None,
            None,
            None,
            Some(&WorkerCaptionPolicy {
                mode: WorkerCaptionMode::Disabled,
                service_number: 1,
                language: "en-US".into(),
                revision: 1,
            }),
        )
        .expect("disabled caption policy still permits ordinary output");
    assert!(matches!(
        playout.push_caption_cue(&CaptionCue {
            cue_id: "4fefcd4e-cfa1-4f62-a810-3d4fa53d2cb6".into(),
            start_after_ms: 0,
            duration_ms: 1_000,
            text: "must not enter disabled transport".into(),
        }),
        Err(EngineError::CaptionDisabled)
    ));
    playout.stop().unwrap();
}

#[test]
fn sends_the_persistent_transport_to_an_srt_listener() {
    let _guard = persistent_graph_test_guard();
    gst::init().unwrap();
    let reservation = UdpSocket::bind("127.0.0.1:0").unwrap();
    let destination = reservation.local_addr().unwrap();
    drop(reservation);
    let capture_path = test_root("srt-transport").with_extension("ts");
    let receiver = gst::Pipeline::new();
    let source = gst::ElementFactory::make("srtsrc")
        .property("uri", format!("srt://{destination}"))
        .property_from_str("mode", "listener")
        .property("localaddress", "127.0.0.1")
        .property("localport", u32::from(destination.port()))
        .property("latency", 200_i32)
        .build()
        .unwrap();
    let sink = gst::ElementFactory::make("filesink")
        .property("location", capture_path.to_string_lossy().as_ref())
        .property("sync", false)
        .property("async", false)
        .build()
        .unwrap();
    receiver.add_many([&source, &sink]).unwrap();
    source.link(&sink).unwrap();
    receiver.set_state(gst::State::Playing).unwrap();

    let playout = PersistentPlayout::start_outputs_with_profile(
        &[TransportOutput::Srt {
            uri: format!("srt://{destination}"),
            passphrase: None,
        }],
        OutputProfileId::Proof360p30,
    )
    .expect("the SRT caller output must start");
    let first_counter = playout.output_bytes_sent().unwrap();
    std::thread::sleep(Duration::from_secs(10));
    let second_counter = playout.output_bytes_sent().unwrap();
    assert_eq!(first_counter.len(), 1);
    assert!(second_counter[0] > first_counter[0]);
    playout.stop().unwrap();
    receiver.set_state(gst::State::Null).unwrap();
    receiver.state(gst::ClockTime::from_seconds(5)).0.unwrap();

    let capture = fs::read(&capture_path).unwrap();
    assert!(capture.len() > 500_000);
    let mut continuity = ContinuityTracker::default();
    // srtsink sends the muxer's PAT/PMT streamheader once at connection before
    // the continuous transport buffers. Start continuity tracking after that
    // standards-compatible bootstrap duplicate.
    for packet in capture.chunks_exact(188).skip(32) {
        continuity.observe(packet);
    }
    assert!(continuity.video_pid.is_some());
    assert!(continuity.audio_pid.is_some());
    assert!(continuity.video_pts_samples >= 3);
    fs::remove_file(capture_path).unwrap();
}

#[test]
fn sends_an_aes256_srt_transport_only_to_a_matching_listener() {
    let _guard = persistent_graph_test_guard();
    gst::init().unwrap();
    let reservation = UdpSocket::bind("127.0.0.1:0").unwrap();
    let destination = reservation.local_addr().unwrap();
    drop(reservation);
    let capture_path = test_root("encrypted-srt-transport").with_extension("ts");
    let passphrase = "townlight-srt-proof-secret";
    let receiver = gst::Pipeline::new();
    let source = gst::ElementFactory::make("srtsrc")
        .property("uri", format!("srt://{destination}"))
        .property_from_str("mode", "listener")
        .property("localaddress", "127.0.0.1")
        .property("localport", u32::from(destination.port()))
        .property("latency", 200_i32)
        .property("passphrase", passphrase)
        .property_from_str("pbkeylen", "32")
        .build()
        .unwrap();
    let sink = gst::ElementFactory::make("filesink")
        .property("location", capture_path.to_string_lossy().as_ref())
        .property("sync", false)
        .property("async", false)
        .build()
        .unwrap();
    receiver.add_many([&source, &sink]).unwrap();
    source.link(&sink).unwrap();
    receiver.set_state(gst::State::Playing).unwrap();

    let playout = PersistentPlayout::start_outputs_with_profile(
        &[TransportOutput::Srt {
            uri: format!("srt://{destination}"),
            passphrase: Some(passphrase.into()),
        }],
        OutputProfileId::Proof360p30,
    )
    .expect("matching AES-256 SRT peers must start");
    std::thread::sleep(Duration::from_secs(4));
    playout.stop().unwrap();
    receiver.set_state(gst::State::Null).unwrap();
    receiver.state(gst::ClockTime::from_seconds(5)).0.unwrap();

    let capture = fs::read(&capture_path).unwrap();
    assert!(capture.len() > 500_000);
    assert_eq!(capture.len() % 188, 0);
    fs::remove_file(capture_path).unwrap();
}

#[test]
fn refuses_encrypted_srt_readiness_for_a_mismatched_passphrase() {
    let _guard = persistent_graph_test_guard();
    gst::init().unwrap();
    let reservation = UdpSocket::bind("127.0.0.1:0").unwrap();
    let destination = reservation.local_addr().unwrap();
    drop(reservation);
    let receiver = gst::Pipeline::new();
    let source = gst::ElementFactory::make("srtsrc")
        .property("uri", format!("srt://{destination}"))
        .property_from_str("mode", "listener")
        .property("localaddress", "127.0.0.1")
        .property("localport", u32::from(destination.port()))
        .property("passphrase", "listener-secret-value")
        .property_from_str("pbkeylen", "32")
        .build()
        .unwrap();
    let sink = gst::ElementFactory::make("fakesink").build().unwrap();
    receiver.add_many([&source, &sink]).unwrap();
    source.link(&sink).unwrap();
    receiver.set_state(gst::State::Playing).unwrap();

    let started = Instant::now();
    let result = PersistentPlayout::start_outputs_with_profile(
        &[TransportOutput::Srt {
            uri: format!("srt://{destination}"),
            passphrase: Some("different-caller-secret".into()),
        }],
        OutputProfileId::Proof360p30,
    );
    assert!(
        matches!(result, Err(EngineError::State(message)) if message.contains("did not connect"))
    );
    assert!(started.elapsed() < Duration::from_secs(8));
    receiver.set_state(gst::State::Null).unwrap();
}

#[test]
fn refuses_srt_readiness_when_the_required_listener_is_absent() {
    let _guard = persistent_graph_test_guard();
    let reservation = UdpSocket::bind("127.0.0.1:0").unwrap();
    let destination = reservation.local_addr().unwrap();
    drop(reservation);
    let started = Instant::now();
    let error = match PersistentPlayout::start_outputs_with_profile(
        &[TransportOutput::Srt {
            uri: format!("srt://{destination}"),
            passphrase: None,
        }],
        OutputProfileId::Proof360p30,
    ) {
        Err(error) => error,
        Ok(playout) => {
            playout.stop().unwrap();
            panic!("an absent required SRT listener must fail closed");
        }
    };
    assert!(matches!(error, EngineError::State(message) if message.contains("did not connect")));
    assert!(started.elapsed() < Duration::from_secs(8));
}

#[test]
fn repeats_codec_configuration_for_a_receiver_that_joins_after_startup() {
    let _guard = persistent_graph_test_guard();
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
    let _guard = persistent_graph_test_guard();
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
    // Keep the hot UDP receive loop allocation-free. At this profile's
    // 8 Mbps contract, growing the capture Vec can briefly stop reads long
    // enough for Windows to drop a datagram and manufacture a continuity gap
    // in the proof harness rather than in the emitted transport.
    let mut transport_capture = Vec::with_capacity(2_500_000);
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
    let _guard = persistent_graph_test_guard();
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
fn applies_and_clears_cg_while_transport_counters_continue() {
    let _guard = persistent_graph_test_guard();
    let receiver = UdpSocket::bind("127.0.0.1:0").unwrap();
    receiver
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let mut playout = PersistentPlayout::start_udp_with_profile(
        receiver.local_addr().unwrap(),
        OutputProfileId::Proof360p30,
    )
    .unwrap();
    let snapshot = CgSnapshot {
        board_id: "board-proof".into(),
        board_revision: 1,
        overlays: vec![CgOverlay {
            slot: 0,
            rect: CgRect {
                x: 10,
                y: 700,
                width: 800,
                height: 200,
            },
            z_index: 1,
            content: CgOverlayContent::Text {
                text: "on-air proof".into(),
            },
        }],
    };
    let before = playout.output_bytes_sent().unwrap()[0];
    playout.apply_cg_snapshot(&snapshot).unwrap();
    std::thread::sleep(Duration::from_millis(500));
    let after_apply = playout.output_bytes_sent().unwrap()[0];
    assert!(after_apply > before);
    playout.clear_cg_snapshot();
    std::thread::sleep(Duration::from_millis(500));
    let after_clear = playout.output_bytes_sent().unwrap()[0];
    assert!(after_clear > after_apply);
    playout.stop().unwrap();
}

#[test]
fn cg_text_overlay_changes_pixels_in_its_declared_region() {
    let _guard = persistent_graph_test_guard();
    let reservation = UdpSocket::bind("127.0.0.1:0").unwrap();
    let port = reservation.local_addr().unwrap().port();
    drop(reservation);
    gst::init().unwrap();
    let (decoder, sink) = start_udp_decoder(port);
    decoder.set_state(gst::State::Playing).unwrap();
    let mut playout = PersistentPlayout::start_udp_with_profile(
        format!("127.0.0.1:{port}").parse().unwrap(),
        OutputProfileId::Proof360p30,
    )
    .unwrap();
    let without_text = pull_decoded_frame(&sink);
    playout
        .apply_cg_snapshot(&CgSnapshot {
            board_id: "pixel-proof".into(),
            board_revision: 1,
            overlays: vec![CgOverlay {
                slot: 0,
                rect: CgRect {
                    x: 10,
                    y: 700,
                    width: 800,
                    height: 200,
                },
                z_index: 1,
                content: CgOverlayContent::Text {
                    text: "VISIBLE".into(),
                },
            }],
        })
        .unwrap();
    std::thread::sleep(Duration::from_secs(5));
    let with_text = pull_decoded_frame(&sink);
    let after_first_apply_bytes = playout.output_bytes_sent().unwrap()[0];
    playout.clear_cg_snapshot();
    std::thread::sleep(Duration::from_secs(5));
    let after_clear = pull_decoded_frame(&sink);
    let after_clear_bytes = playout.output_bytes_sent().unwrap()[0];
    assert_eq!(
        (without_text.width, without_text.height),
        (with_text.width, with_text.height)
    );
    assert_eq!(without_text.stride, without_text.width as usize * 4);
    let rect = (6, 252, 518, 324);
    let changed_pixels = without_text
        .bytes
        .chunks_exact(4)
        .zip(with_text.bytes.chunks_exact(4))
        .enumerate()
        .filter(|(index, (before, after))| {
            let x = index % without_text.width as usize;
            let y = index / without_text.width as usize;
            (rect.0..=rect.2).contains(&(x as u32))
                && (rect.1..=rect.3).contains(&(y as u32))
                && before[..3] != after[..3]
        })
        .count();
    let all_changed_pixels = without_text
        .bytes
        .chunks_exact(4)
        .zip(with_text.bytes.chunks_exact(4))
        .filter(|(before, after)| before[..3] != after[..3])
        .count();
    assert!(
        changed_pixels > 20,
        "decoded CG output changed too few region pixels: {changed_pixels}; all changed: {all_changed_pixels}"
    );
    let restored_pixels = without_text
        .bytes
        .chunks_exact(4)
        .zip(after_clear.bytes.chunks_exact(4))
        .enumerate()
        .filter(|(index, (before, after))| {
            let x = index % without_text.width as usize;
            let y = index / without_text.width as usize;
            (rect.0..=rect.2).contains(&(x as u32))
                && (rect.1..=rect.3).contains(&(y as u32))
                && before[..3] != after[..3]
        })
        .count();
    assert!(
        restored_pixels < changed_pixels / 2,
        "CG clear did not restore the region sufficiently: {restored_pixels} vs {changed_pixels}"
    );

    // Reapplying after clear must produce fresh decoded pixels and preserve transport progress.
    playout
        .apply_cg_snapshot(&CgSnapshot {
            board_id: "pixel-proof".into(),
            board_revision: 2,
            overlays: vec![CgOverlay {
                slot: 0,
                rect: CgRect {
                    x: 10,
                    y: 700,
                    width: 800,
                    height: 200,
                },
                z_index: 1,
                content: CgOverlayContent::Text {
                    text: "AGAIN".into(),
                },
            }],
        })
        .unwrap();
    std::thread::sleep(Duration::from_secs(5));
    let applied_again = pull_decoded_frame(&sink);
    let after_second_apply_bytes = playout.output_bytes_sent().unwrap()[0];
    let again_pixels = without_text
        .bytes
        .chunks_exact(4)
        .zip(applied_again.bytes.chunks_exact(4))
        .enumerate()
        .filter(|(index, (before, after))| {
            let x = index % without_text.width as usize;
            let y = index / without_text.width as usize;
            (rect.0..=rect.2).contains(&(x as u32))
                && (rect.1..=rect.3).contains(&(y as u32))
                && before[..3] != after[..3]
        })
        .count();
    assert!(
        again_pixels > 20,
        "reapplied CG changed too few pixels: {again_pixels}"
    );
    assert!(after_first_apply_bytes > 0);
    assert!(after_clear_bytes > after_first_apply_bytes);
    assert!(after_second_apply_bytes > after_clear_bytes);

    // Replacing two overlays with one must silence the vacated physical layer.
    let second_rect = (6, 36, 518, 108);
    playout
        .apply_cg_snapshot(&CgSnapshot {
            board_id: "pixel-proof".into(),
            board_revision: 3,
            overlays: vec![
                CgOverlay {
                    slot: 0,
                    rect: CgRect {
                        x: 10,
                        y: 700,
                        width: 800,
                        height: 200,
                    },
                    z_index: 1,
                    content: CgOverlayContent::Text {
                        text: "KEEP".into(),
                    },
                },
                CgOverlay {
                    slot: 1,
                    rect: CgRect {
                        x: 10,
                        y: 100,
                        width: 800,
                        height: 200,
                    },
                    z_index: 2,
                    content: CgOverlayContent::Text {
                        text: "STALE".into(),
                    },
                },
            ],
        })
        .unwrap();
    std::thread::sleep(Duration::from_secs(5));
    let with_two = pull_decoded_frame(&sink);
    playout
        .apply_cg_snapshot(&CgSnapshot {
            board_id: "pixel-proof".into(),
            board_revision: 4,
            overlays: vec![CgOverlay {
                slot: 0,
                rect: CgRect {
                    x: 10,
                    y: 700,
                    width: 800,
                    height: 200,
                },
                z_index: 1,
                content: CgOverlayContent::Text {
                    text: "KEEP".into(),
                },
            }],
        })
        .unwrap();
    std::thread::sleep(Duration::from_secs(5));
    let with_one = pull_decoded_frame(&sink);
    let stale_pixels = after_clear
        .bytes
        .chunks_exact(4)
        .zip(with_two.bytes.chunks_exact(4))
        .enumerate()
        .filter(|(index, (before, after))| {
            let x = index % after_clear.width as usize;
            let y = index / after_clear.width as usize;
            (second_rect.0..=second_rect.2).contains(&(x as u32))
                && (second_rect.1..=second_rect.3).contains(&(y as u32))
                && before[..3] != after[..3]
        })
        .count();
    let stale_remaining = after_clear
        .bytes
        .chunks_exact(4)
        .zip(with_one.bytes.chunks_exact(4))
        .enumerate()
        .filter(|(index, (before, after))| {
            let x = index % after_clear.width as usize;
            let y = index / after_clear.width as usize;
            (second_rect.0..=second_rect.2).contains(&(x as u32))
                && (second_rect.1..=second_rect.3).contains(&(y as u32))
                && before[..3] != after[..3]
        })
        .count();
    assert!(
        stale_pixels > 20,
        "two-overlay proof did not render the upper layer"
    );
    assert!(
        stale_remaining < stale_pixels / 2,
        "fewer-overlay replacement left stale pixels: {stale_remaining} vs {stale_pixels}"
    );
    playout.clear_cg_snapshot();
    playout.stop().unwrap();
    stop_test_pipeline(&decoder);
}

#[test]
fn managed_png_jpeg_image_apply_clear_reapply_and_invalid_retain_pixels() {
    let _guard = persistent_graph_test_guard();
    gst::init().unwrap();
    let root = test_root("cg-images");
    fs::create_dir_all(&root).unwrap();
    let (png_id, _) = write_image_fixture(&root, ImageFormat::Png, [0, 255, 0, 255]);
    let (jpeg_id, _) = write_image_fixture(&root, ImageFormat::Jpeg, [255, 0, 0, 255]);
    let reservation = UdpSocket::bind("127.0.0.1:0").unwrap();
    let port = reservation.local_addr().unwrap().port();
    drop(reservation);
    let (decoder, sink) = start_udp_decoder(port);
    decoder.set_state(gst::State::Playing).unwrap();
    let mut playout = PersistentPlayout::start_outputs_with_profile_and_cg_root(
        &[TransportOutput::Udp(
            format!("127.0.0.1:{port}").parse().unwrap(),
        )],
        OutputProfileId::Proof360p30,
        Some(&root),
    )
    .unwrap();
    playout.select(SourceRole::Program).unwrap();
    std::thread::sleep(Duration::from_secs(5));
    let baseline = pull_decoded_frame(&sink);
    let before_bytes = playout.output_bytes_sent().unwrap()[0];
    let image_snapshot = |asset_id: String, format: CgImageFormat, revision| CgSnapshot {
        board_id: "image-proof".into(),
        board_revision: revision,
        overlays: vec![CgOverlay {
            slot: 0,
            rect: CgRect {
                x: 100,
                y: 100,
                width: 300,
                height: 300,
            },
            z_index: 1,
            content: CgOverlayContent::Image { asset_id, format },
        }],
    };
    playout
        .apply_cg_snapshot(&image_snapshot(png_id.clone(), CgImageFormat::Png, 1))
        .unwrap();
    std::thread::sleep(Duration::from_secs(5));
    let png_frame = pull_decoded_frame(&sink);
    let sample = |frame: &DecodedFrame, x: usize, y: usize| -> [u8; 4] {
        let index = (y * frame.width as usize + x) * 4;
        frame.bytes[index..index + 4].try_into().unwrap()
    };
    let black_sample = sample(&png_frame, 90, 80);
    let green_sample = sample(&png_frame, 220, 80);
    let baseline_sample = sample(&baseline, 90, 80);
    assert!(baseline_sample[..3].iter().any(|channel| *channel > 45));
    assert!(black_sample[..3].iter().all(|channel| *channel < 35));
    assert!(green_sample[1] > green_sample[0] + 40 && green_sample[1] > green_sample[2] + 40);
    let outside_before = sample(&baseline, 20, 20);
    let outside_after = sample(&png_frame, 20, 20);
    assert!(
        outside_before
            .iter()
            .zip(outside_after)
            .all(|(before, after)| before.abs_diff(after) < 80)
    );
    let region_diff = |left: &DecodedFrame, right: &DecodedFrame| {
        left.bytes
            .chunks_exact(4)
            .zip(right.bytes.chunks_exact(4))
            .enumerate()
            .filter(|(index, (before, after))| {
                let x = index % left.width as usize;
                let y = index / left.width as usize;
                (64..=255).contains(&(x as u32))
                    && (36..=143).contains(&(y as u32))
                    && before[..3] != after[..3]
            })
            .count()
    };
    assert!(region_diff(&baseline, &png_frame) > 20);
    playout.clear_cg_snapshot();
    std::thread::sleep(Duration::from_secs(5));
    let cleared = pull_decoded_frame(&sink);
    let cleared_sample = sample(&cleared, 90, 80);
    assert!(cleared_sample[..3].iter().any(|channel| *channel > 45));
    let after_clear_bytes = playout.output_bytes_sent().unwrap()[0];
    playout
        .apply_cg_snapshot(&image_snapshot(jpeg_id, CgImageFormat::Jpeg, 2))
        .unwrap();
    std::thread::sleep(Duration::from_secs(5));
    let jpeg_frame = pull_decoded_frame(&sink);
    assert!(region_diff(&baseline, &jpeg_frame) > 20);
    let after_jpeg_bytes = playout.output_bytes_sent().unwrap()[0];
    let invalid = image_snapshot("f".repeat(64), CgImageFormat::Png, 3);
    assert!(playout.apply_cg_snapshot(&invalid).is_err());
    assert!(
        playout
            .apply_cg_snapshot(&image_snapshot(png_id, CgImageFormat::Jpeg, 3))
            .is_err()
    );
    let missing = image_snapshot("e".repeat(64), CgImageFormat::Png, 4);
    assert!(playout.apply_cg_snapshot(&missing).is_err());
    assert_eq!(playout.current_cg_revision(), Some(("image-proof", 2)));
    std::thread::sleep(Duration::from_secs(5));
    let after_invalid = pull_decoded_frame(&sink);
    let jpeg_center = sample(&jpeg_frame, 220, 80);
    let retained_center = sample(&after_invalid, 220, 80);
    assert!(
        retained_center[2] > retained_center[1] + 30
            && retained_center[2] > retained_center[0] + 30
            && jpeg_center[2] > jpeg_center[1] + 20
    );
    let after_invalid_bytes = playout.output_bytes_sent().unwrap()[0];
    assert!(after_clear_bytes > before_bytes);
    assert!(after_jpeg_bytes > after_clear_bytes);
    assert!(after_invalid_bytes > after_jpeg_bytes);
    playout.stop().unwrap();
    stop_test_pipeline(&decoder);
    let _ = fs::remove_dir_all(root);
}

#[test]
fn configured_fallback_slate_is_aspect_fitted_and_visible_in_decoded_output() {
    let _guard = persistent_graph_test_guard();
    gst::init().unwrap();
    let root = test_root("fallback-slate-pixels");
    fs::create_dir_all(&root).unwrap();
    let (asset_id, _) = write_image_fixture(&root, ImageFormat::Png, [0, 255, 0, 255]);
    let reservation = UdpSocket::bind("127.0.0.1:0").unwrap();
    let port = reservation.local_addr().unwrap().port();
    drop(reservation);
    let (decoder, sink) = start_udp_decoder(port);
    decoder.set_state(gst::State::Playing).unwrap();
    let output = ConfiguredTransportOutput::udp(
        format!("127.0.0.1:{port}").parse().unwrap(),
        LoudnessTarget::STREAMING,
    );
    let playout =
        PersistentPlayout::start_configured_outputs_with_profile_roots_and_fallback_slate(
            &[output],
            OutputProfileId::Proof360p30,
            Some(&root),
            None,
            Some(&WorkerFallbackSlate {
                asset_id,
                format: CgImageFormat::Png,
            }),
        )
        .unwrap();
    std::thread::sleep(Duration::from_secs(5));
    let frame = pull_decoded_frame(&sink);
    let sample = |x: usize, y: usize| -> [u8; 4] {
        let index = y * frame.stride + x * 4;
        frame.bytes[index..index + 4].try_into().unwrap()
    };
    let outside = sample(50, 180);
    let slate_right = sample(430, 180);
    assert!(outside[..3].iter().all(|channel| *channel < 35));
    assert!(
        slate_right[1] > slate_right[0] + 40 && slate_right[1] > slate_right[2] + 40,
        "decoded fallback center did not contain the selected green slate: {slate_right:?}"
    );
    assert!(playout.output_bytes_sent().unwrap()[0] > 0);
    playout.stop().unwrap();
    stop_test_pipeline(&decoder);
    let _ = fs::remove_dir_all(root);
}

fn write_image_fixture(
    root: &Path,
    format: ImageFormat,
    color: [u8; 4],
) -> (String, std::path::PathBuf) {
    let image = ImageBuffer::from_fn(96, 96, |x, _| {
        if format == ImageFormat::Png && x < 48 {
            Rgba([0, 0, 0, 255])
        } else {
            Rgba(color)
        }
    });
    let mut bytes = Cursor::new(Vec::new());
    image::DynamicImage::ImageRgba8(image)
        .write_to(&mut bytes, format)
        .unwrap();
    let bytes = bytes.into_inner();
    let id = format!("{:x}", Sha256::digest(&bytes));
    let extension = match format {
        ImageFormat::Png => "png",
        ImageFormat::Jpeg => "jpg",
        _ => unreachable!(),
    };
    let path = root.join(format!("{id}.{extension}"));
    fs::write(&path, bytes).unwrap();
    (id, path)
}

struct TestPipeline {
    pipeline: gst::Pipeline,
}

impl Deref for TestPipeline {
    type Target = gst::Pipeline;

    fn deref(&self) -> &Self::Target {
        &self.pipeline
    }
}

impl Drop for TestPipeline {
    fn drop(&mut self) {
        let _ = self.pipeline.set_state(gst::State::Null);
        let _ = self.pipeline.state(gst::ClockTime::from_seconds(5)).0;
    }
}

fn start_udp_decoder(port: u16) -> (TestPipeline, gst::Element) {
    let pipeline = gst::Pipeline::new();
    let source = gst::ElementFactory::make("udpsrc")
        .property("port", i32::from(port))
        .property(
            "caps",
            gst::Caps::builder("video/mpegts")
                .field("systemstream", true)
                .field("packetsize", 188_i32)
                .build(),
        )
        .build()
        .unwrap();
    let demux = gst::ElementFactory::make("tsdemux")
        .name("cg-demux")
        .build()
        .unwrap();
    let queue = gst::ElementFactory::make("queue")
        .name("cg-decode-queue")
        .build()
        .unwrap();
    let audio_queue = gst::ElementFactory::make("queue")
        .name("cg-audio-discard-queue")
        .build()
        .unwrap();
    let audio_sink = gst::ElementFactory::make("fakesink")
        .name("cg-audio-discard-sink")
        .property("sync", false)
        .property("async", false)
        .build()
        .unwrap();
    let parser = gst::ElementFactory::make("h264parse").build().unwrap();
    let decoder = gst::ElementFactory::make("openh264dec").build().unwrap();
    let convert = gst::ElementFactory::make("videoconvert").build().unwrap();
    let caps = gst::ElementFactory::make("capsfilter")
        .property(
            "caps",
            gst::Caps::builder("video/x-raw")
                .field("format", "BGRA")
                .build(),
        )
        .build()
        .unwrap();
    let sink = gst::ElementFactory::make("appsink")
        .name("frame-sink")
        .property("sync", false)
        .property("max-buffers", 1_u32)
        .property("drop", true)
        .build()
        .unwrap();
    pipeline
        .add_many([
            &source,
            &demux,
            &queue,
            &parser,
            &decoder,
            &convert,
            &caps,
            &sink,
            &audio_queue,
            &audio_sink,
        ])
        .unwrap();
    source.link(&demux).unwrap();
    gst::Element::link_many([&queue, &parser, &decoder, &convert, &caps, &sink]).unwrap();
    audio_queue.link(&audio_sink).unwrap();
    let queue_sink = queue.static_pad("sink").unwrap();
    let audio_queue_sink = audio_queue.static_pad("sink").unwrap();
    demux.connect_pad_added(move |_, pad| {
        let caps = pad.current_caps().unwrap_or_else(|| pad.query_caps(None));
        let media_type = caps
            .structure(0)
            .map(|structure| structure.name().to_string())
            .unwrap_or_default();
        let pad_name = pad.name();
        let sink = if media_type.starts_with("video/") || pad_name.starts_with("video_") {
            Some(&queue_sink)
        } else if media_type.starts_with("audio/") || pad_name.starts_with("audio_") {
            Some(&audio_queue_sink)
        } else {
            None
        };
        if let Some(sink) = sink {
            if let Some(previous) = sink.peer() {
                let _ = previous.unlink(sink);
            }
            let _ = pad.link(sink);
        }
    });
    (TestPipeline { pipeline }, sink)
}

fn stop_test_pipeline(pipeline: &TestPipeline) {
    pipeline.pipeline.set_state(gst::State::Null).unwrap();
    pipeline
        .pipeline
        .state(gst::ClockTime::from_seconds(5))
        .0
        .unwrap();
}

fn start_udp_live_sender(port: u16) -> gst::Pipeline {
    gst::init().unwrap();
    let pipeline = gst::Pipeline::new();
    let video = gst::ElementFactory::make("videotestsrc")
        .property("is-live", true)
        .property_from_str("pattern", "snow")
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
        .property("is-live", true)
        .property_from_str("wave", "sine")
        .build()
        .unwrap();
    let audio_convert = gst::ElementFactory::make("audioconvert").build().unwrap();
    let audio_resample = gst::ElementFactory::make("audioresample").build().unwrap();
    let audio_encoder = gst::ElementFactory::make("voaacenc").build().unwrap();
    let audio_parser = gst::ElementFactory::make("aacparse").build().unwrap();
    let mux = gst::ElementFactory::make("mpegtsmux").build().unwrap();
    let sink = gst::ElementFactory::make("udpsink")
        .property("host", "127.0.0.1")
        .property("port", i32::from(port))
        .property("sync", true)
        .property("async", false)
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
    pipeline
}

struct FfmpegLiveSender {
    child: Child,
}

impl FfmpegLiveSender {
    fn try_wait(&mut self) -> std::io::Result<Option<std::process::ExitStatus>> {
        self.child.try_wait()
    }
}

impl Drop for FfmpegLiveSender {
    fn drop(&mut self) {
        if self.child.try_wait().ok().flatten().is_none() {
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }
}

fn start_ffmpeg_live_sender(port: u16) -> FfmpegLiveSender {
    let child = Command::new("ffmpeg.exe")
        .args([
            "-nostdin",
            "-loglevel",
            "error",
            "-re",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30",
            "-re",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=997:sample_rate=48000",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-g",
            "30",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-f",
            "mpegts",
            &format!("udp://127.0.0.1:{port}?pkt_size=1316"),
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("FFmpeg is required for the MPEG-TS interoperability proof");
    FfmpegLiveSender { child }
}

#[test]
fn invalid_cg_update_keeps_the_last_valid_revision_active() {
    let _guard = persistent_graph_test_guard();
    let receiver = UdpSocket::bind("127.0.0.1:0").unwrap();
    let mut playout = PersistentPlayout::start_udp_with_profile(
        receiver.local_addr().unwrap(),
        OutputProfileId::Proof360p30,
    )
    .unwrap();
    let valid = CgSnapshot {
        board_id: "board-valid".into(),
        board_revision: 1,
        overlays: vec![CgOverlay {
            slot: 0,
            rect: CgRect {
                x: 10,
                y: 700,
                width: 800,
                height: 200,
            },
            z_index: 1,
            content: CgOverlayContent::Text {
                text: "valid".into(),
            },
        }],
    };
    playout.apply_cg_snapshot(&valid).unwrap();
    let mut changed = valid.clone();
    changed.overlays[0].content = CgOverlayContent::Text {
        text: "changed".into(),
    };
    playout.apply_cg_snapshot(&changed).unwrap();
    assert_eq!(playout.current_cg_snapshot(), Some(&changed));
    let invalid = CgSnapshot {
        board_id: "board-invalid".into(),
        board_revision: 2,
        overlays: vec![CgOverlay {
            slot: 0,
            rect: valid.overlays[0].rect.clone(),
            z_index: 1,
            content: CgOverlayContent::Text {
                text: "bad\u{0000}text".into(),
            },
        }],
    };
    assert!(playout.apply_cg_snapshot(&invalid).is_err());
    assert_eq!(playout.current_cg_revision(), Some(("board-valid", 1)));
    playout.stop().unwrap();
}

struct DecodedFrame {
    bytes: Vec<u8>,
    width: u32,
    height: u32,
    stride: usize,
}

fn pull_decoded_frame(sink: &gst::Element) -> DecodedFrame {
    let sample = (0..50)
        .find_map(|_| {
            sink.emit_by_name::<Option<gst::Sample>>("try-pull-sample", &[&100_000_000_u64])
        })
        .unwrap_or_else(|| {
            let detail = sink
                .parent()
                .and_then(|parent| parent.downcast::<gst::Pipeline>().ok())
                .and_then(|pipeline| pipeline.bus())
                .and_then(|bus| bus.pop_filtered(&[gst::MessageType::Error]))
                .map(|message| format!("{message:?}"))
                .unwrap_or_else(|| "no GStreamer bus error".into());
            panic!("decoded CG output did not produce a frame: {detail}");
        });
    let caps = sample.caps().expect("decoded frame has no caps");
    let structure = caps.structure(0).expect("decoded frame has no video caps");
    let width = structure.get::<i32>("width").unwrap() as u32;
    let height = structure.get::<i32>("height").unwrap() as u32;
    let map = sample.buffer().unwrap().map_readable().unwrap();
    let bytes = map.as_slice().to_vec();
    let stride = bytes.len() / height as usize;
    DecodedFrame {
        bytes,
        width,
        height,
        stride,
    }
}

fn frame_signature(frame: &DecodedFrame) -> u64 {
    frame
        .bytes
        .chunks_exact(4)
        .step_by(97)
        .map(|pixel| u64::from(pixel[0]) + u64::from(pixel[1]) + u64::from(pixel[2]))
        .sum()
}

#[test]
fn plays_a_validated_ingested_file_without_rebuilding_the_output_graph() {
    let _guard = persistent_graph_test_guard();
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
    prepare_while_draining_output(&receiver, &mut playout, |playout| {
        playout.load_file(&asset.stored_path)
    })
    .expect("the validated stored asset must load into the running graph");
    clear_udp_backlog(&receiver);
    continuity = ContinuityTracker::default();
    playout.select(SourceRole::Program).unwrap();
    // 120 datagrams can cover fewer than three complete video PES timestamps
    // when sampling begins near a mux boundary. Keep a bounded wider window so
    // the continuity assertion is deterministic on loaded machines.
    assert_transport_stream(&receiver, &mut continuity, 240);
    assert_eq!(playout.active_source().unwrap(), SourceRole::Program);
    playout.select(SourceRole::Fallback).unwrap();
    assert_transport_stream(&receiver, &mut continuity, 12);
    assert_eq!(playout.active_source().unwrap(), SourceRole::Fallback);
    prepare_while_draining_output(&receiver, &mut playout, |playout| {
        playout.load_file(&second_asset.stored_path)
    })
    .expect("a second stored asset must replace the prepared program leg");
    clear_udp_backlog(&receiver);
    continuity = ContinuityTracker::default();
    playout.select(SourceRole::Program).unwrap();
    assert_transport_stream(&receiver, &mut continuity, 240);
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

fn prepare_while_draining_output<T>(
    receiver: &UdpSocket,
    playout: &mut PersistentPlayout,
    prepare: impl FnOnce(&mut PersistentPlayout) -> T,
) -> T {
    let drain_socket = receiver.try_clone().unwrap();
    drain_socket
        .set_read_timeout(Some(Duration::from_millis(100)))
        .unwrap();
    let baseline = playout.output_bytes_sent().unwrap()[0];
    let (stop_tx, stop_rx) = std::sync::mpsc::channel::<()>();
    let (result, advanced) = std::thread::scope(|scope| {
        let drain = scope.spawn(move || {
            let mut datagram = [0_u8; 65_536];
            loop {
                match stop_rx.try_recv() {
                    Ok(()) | Err(TryRecvError::Disconnected) => break,
                    Err(TryRecvError::Empty) => {}
                }
                match drain_socket.recv(&mut datagram) {
                    Ok(_) => {}
                    Err(error)
                        if matches!(
                            error.kind(),
                            std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                        ) => {}
                    Err(error) => panic!("could not drain proof output: {error}"),
                }
            }
        });
        let result = prepare(playout);
        let started = Instant::now();
        let advanced = loop {
            if playout.output_bytes_sent().unwrap()[0] > baseline {
                break true;
            }
            if started.elapsed() >= Duration::from_secs(2) {
                break false;
            }
            std::thread::sleep(Duration::from_millis(10));
        };
        let _ = stop_tx.send(());
        drain.join().unwrap();
        (result, advanced)
    });
    receiver
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    assert!(
        advanced,
        "fallback transport did not advance within 2s while program preparation output was drained"
    );
    result
}

#[test]
fn prepared_file_stays_parked_and_can_take_at_a_wall_clock_offset() {
    let _guard = persistent_graph_test_guard();
    let root = test_root("parked-offset-playout");
    let source = root.join("timed-source.ts");
    let library = root.join("library");
    fs::create_dir_all(&root).unwrap();
    generate_black_then_white_fixture(&source);
    let asset = ingest_media(&source, &library).expect("the timed A/V file must ingest");

    let output_probe = UdpSocket::bind("127.0.0.1:0").unwrap();
    let output_port = output_probe.local_addr().unwrap().port();
    drop(output_probe);
    let (decoder, frame_sink) = start_udp_decoder(output_port);
    decoder.set_state(gst::State::Playing).unwrap();
    let mut playout = PersistentPlayout::start_udp_with_profile(
        ([127, 0, 0, 1], output_port).into(),
        OutputProfileId::Proof360p30,
    )
    .unwrap();

    playout
        .load_asset(&asset.asset_id, &asset.stored_path)
        .unwrap();
    std::thread::sleep(Duration::from_secs(3));
    playout.take_program_at(0).unwrap();
    let from_start = pull_frame_matching(&frame_sink, Duration::from_secs(8), |frame| {
        center_luma(frame) < 40
    });
    assert!(center_luma(&from_start) < 40);

    playout.select(SourceRole::Fallback).unwrap();
    playout
        .load_asset(&asset.asset_id, &asset.stored_path)
        .unwrap();
    playout.take_program_at(2_500).unwrap();
    let caught_up = pull_frame_matching(&frame_sink, Duration::from_secs(8), |frame| {
        center_luma(frame) > 200
    });
    assert!(center_luma(&caught_up) > 200);

    playout.stop().unwrap();
    stop_test_pipeline(&decoder);
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn arms_udp_live_av_takes_it_without_rebuilding_output_and_returns_to_fallback() {
    let _guard = persistent_graph_test_guard();
    gst::init().unwrap();
    let output_probe = UdpSocket::bind("127.0.0.1:0").unwrap();
    let output_port = output_probe.local_addr().unwrap().port();
    drop(output_probe);
    let (decoder, frame_sink) = start_udp_decoder(output_port);
    decoder.set_state(gst::State::Playing).unwrap();
    let mut playout = PersistentPlayout::start_udp_with_profile(
        ([127, 0, 0, 1], output_port).into(),
        OutputProfileId::Proof360p30,
    )
    .unwrap();
    std::thread::sleep(Duration::from_millis(750));
    let fallback = pull_decoded_frame(&frame_sink);

    let unavailable = UdpSocket::bind("127.0.0.1:0").unwrap();
    let unavailable_port = unavailable.local_addr().unwrap().port();
    drop(unavailable);
    assert!(
        playout
            .arm_live_udp(([127, 0, 0, 1], unavailable_port).into())
            .is_err()
    );
    assert_eq!(playout.active_source().unwrap(), SourceRole::Fallback);
    assert!(matches!(
        playout.select(SourceRole::Live),
        Err(EngineError::LiveNotArmed)
    ));

    let input_probe = UdpSocket::bind("127.0.0.1:0").unwrap();
    let input_port = input_probe.local_addr().unwrap().port();
    drop(input_probe);
    let sender = start_udp_live_sender(input_port);
    playout
        .arm_live_udp(([127, 0, 0, 1], input_port).into())
        .expect("live UDP arm must wait for decoded video and audio");
    assert_eq!(playout.active_source().unwrap(), SourceRole::Fallback);

    playout
        .select(SourceRole::Live)
        .expect("a successfully armed live source must be takeable");
    let live_deadline = Instant::now() + Duration::from_secs(8);
    let live = loop {
        let candidate = pull_decoded_frame(&frame_sink);
        if frame_signature(&candidate) != frame_signature(&fallback)
            || Instant::now() >= live_deadline
        {
            break candidate;
        }
        std::thread::sleep(Duration::from_millis(100));
    };
    assert_ne!(frame_signature(&fallback), frame_signature(&live));
    assert!(playout.output_bytes_sent().unwrap()[0] > 0);

    let failed_replacement = UdpSocket::bind("127.0.0.1:0").unwrap();
    let failed_replacement_port = failed_replacement.local_addr().unwrap().port();
    drop(failed_replacement);
    assert!(
        playout
            .arm_live_udp(([127, 0, 0, 1], failed_replacement_port).into())
            .is_err()
    );
    assert_eq!(playout.active_source().unwrap(), SourceRole::Live);

    playout
        .select(SourceRole::Fallback)
        .expect("fallback must remain takeable after live");
    let return_deadline = Instant::now() + Duration::from_secs(8);
    let returned = loop {
        let candidate = pull_decoded_frame(&frame_sink);
        if frame_signature(&candidate) == frame_signature(&fallback)
            || Instant::now() >= return_deadline
        {
            break candidate;
        }
        std::thread::sleep(Duration::from_millis(100));
    };
    assert_eq!(frame_signature(&fallback), frame_signature(&returned));
    sender.set_state(gst::State::Null).unwrap();
    sender.state(gst::ClockTime::from_seconds(5)).0.unwrap();
    let drain_deadline = Instant::now() + Duration::from_secs(10);
    let mut last_counts = playout.live_buffer_counts().unwrap();
    let mut stable_since = Instant::now();
    loop {
        std::thread::sleep(Duration::from_millis(100));
        let counts = playout.live_buffer_counts().unwrap();
        if counts != last_counts {
            last_counts = counts;
            stable_since = Instant::now();
        } else if stable_since.elapsed() >= Duration::from_millis(500) {
            break;
        }
        assert!(
            Instant::now() < drain_deadline,
            "live decoder buffers did not quiesce after sender stop"
        );
    }
    let stalled_take = playout.select(SourceRole::Live);
    assert!(
        matches!(stalled_take, Err(EngineError::LiveInputStalled { .. })),
        "unexpected stalled-take result: {stalled_take:?}"
    );
    assert_eq!(playout.active_source().unwrap(), SourceRole::Fallback);
    assert!(playout.disarm_live().is_ok());
    assert_eq!(playout.live_buffer_counts(), None);
    assert!(matches!(
        playout.select(SourceRole::Live),
        Err(EngineError::LiveNotArmed)
    ));

    stop_test_pipeline(&decoder);
    playout.stop().unwrap();
}

#[test]
fn ffmpeg_udp_live_takeover_emits_audible_audio() {
    let _guard = persistent_graph_test_guard();
    gst::init().unwrap();
    let root = test_root("ffmpeg-live-program-return");
    fs::create_dir_all(&root).unwrap();
    let source = root.join("scheduled.ts");
    generate_ffmpeg_color_tone_fixture(&source, "blue", 997, 20);
    let asset = ingest_media(&source, root.join("library")).unwrap();
    let receiver = UdpSocket::bind("127.0.0.1:0").unwrap();
    receiver
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    let output_address = receiver.local_addr().unwrap();
    let capture_path = test_root("ffmpeg-live-audio-proof").with_extension("ts");
    let _capture_cleanup = ProofCapture::new(capture_path.clone());
    let returned_capture_path = test_root("ffmpeg-returned-audio-proof").with_extension("ts");
    let _returned_capture_cleanup = ProofCapture::new(returned_capture_path.clone());
    let mut playout =
        PersistentPlayout::start_udp_with_profile(output_address, OutputProfileId::Proof360p30)
            .unwrap();

    let input_probe = UdpSocket::bind("127.0.0.1:0").unwrap();
    let input_port = input_probe.local_addr().unwrap().port();
    drop(input_probe);
    let mut sender = start_ffmpeg_live_sender(input_port);
    // Let the external sender establish a non-zero running PTS before the
    // live branch is armed; this is the installed-machine reproduction case.
    std::thread::sleep(Duration::from_secs(4));
    assert!(sender.try_wait().unwrap().is_none());
    playout
        .arm_live_udp(([127, 0, 0, 1], input_port).into())
        .unwrap();
    playout.select(SourceRole::Live).unwrap();
    let live_counts_before_capture = playout.live_buffer_counts().unwrap();
    clear_udp_backlog(&receiver);
    std::thread::sleep(Duration::from_millis(750));
    // A transport capture beginning at an arbitrary mux boundary can lose
    // leading audio during FFmpeg discovery. Eight seconds leaves a stable
    // six-half-second decoded window without weakening the sustained-audio
    // requirement below.
    let transport_capture = capture_transport_for(&receiver, Duration::from_secs(8));
    assert!(
        transport_capture.len() > 100_000,
        "live capture was too short: {} bytes",
        transport_capture.len()
    );
    fs::write(&capture_path, &transport_capture).unwrap();

    let health_started = Instant::now();
    let (video_buffers, audio_buffers) = playout
        .live_buffer_counts()
        .expect("selected live branch must retain health counters");
    let video_delta = video_buffers.saturating_sub(live_counts_before_capture.0);
    let audio_delta = audio_buffers.saturating_sub(live_counts_before_capture.1);
    assert!(
        video_delta > 90,
        "live video advanced only {video_delta} buffers"
    );
    assert!(
        audio_delta > 20,
        "live audio advanced only {audio_delta} buffers"
    );
    let output_bytes = playout
        .output_bytes_sent()
        .expect("selected live branch must retain queryable output health");
    assert!(output_bytes.iter().all(|bytes| *bytes > 0));
    assert!(
        health_started.elapsed() < Duration::from_secs(2),
        "selected live output health query blocked for {:?}",
        health_started.elapsed()
    );

    let teardown_started = Instant::now();
    playout
        .load_asset(&asset.asset_id, &asset.stored_path)
        .unwrap();
    playout.take_program_at(4_900).unwrap();
    playout.disarm_live().unwrap();
    eprintln!(
        "active FFmpeg live branch teardown: {:?}",
        teardown_started.elapsed()
    );
    assert!(
        teardown_started.elapsed() < Duration::from_secs(15),
        "active FFmpeg live branch teardown exceeded the controller budget: {:?}",
        teardown_started.elapsed()
    );
    let rebound = UdpSocket::bind(("127.0.0.1", input_port))
        .expect("live input UDP port must be reusable after teardown");
    drop(rebound);
    clear_udp_backlog(&receiver);
    std::thread::sleep(Duration::from_millis(750));
    let returned_capture = capture_transport_for(&receiver, Duration::from_secs(5));
    assert!(
        returned_capture.len() > 50_000,
        "returned program capture was too short: {} bytes",
        returned_capture.len()
    );
    fs::write(&returned_capture_path, &returned_capture).unwrap();
    drop(sender);
    playout.stop().unwrap();
    assert_sustained_audible_audio(&returned_capture_path, 200_000, 4);
    assert_sustained_audible_audio(&capture_path, 300_000, 6);
    for _ in 0..20 {
        if fs::remove_dir_all(&root).is_ok() || !root.exists() {
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(
        !root.exists(),
        "test media remained locked after playout stop"
    );
}

fn decode_transport_audio(capture_path: &Path) -> Vec<i16> {
    let decoded = Command::new("ffmpeg.exe")
        .args([
            "-nostdin",
            "-loglevel",
            "error",
            "-i",
            capture_path.to_string_lossy().as_ref(),
            "-map",
            "0:a:0",
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "48000",
            "-ac",
            "2",
            "pipe:1",
        ])
        .output()
        .expect("FFmpeg is required to decode the captured MPEG-TS audio proof");
    assert!(
        decoded.status.success(),
        "FFmpeg failed to decode captured audio: {}",
        String::from_utf8_lossy(&decoded.stderr)
    );
    decoded
        .stdout
        .chunks_exact(2)
        .map(|bytes| i16::from_le_bytes([bytes[0], bytes[1]]))
        .collect()
}

fn mean_absolute_sample(samples: &[i16]) -> f64 {
    let (sum, count) = samples.iter().fold((0_u64, 0_u64), |(sum, count), sample| {
        (sum + u64::from(sample.unsigned_abs()), count + 1)
    });
    sum as f64 / count.max(1) as f64
}

fn maximum_half_second_mean_absolute_sample(samples: &[i16]) -> f64 {
    // PCM is stereo at 48 kHz; use a short window so mux-boundary silence
    // cannot understate the amplitude of an otherwise continuous tone.
    samples
        .chunks(48_000)
        .map(mean_absolute_sample)
        .max_by(f64::total_cmp)
        .unwrap_or(0.0)
}

fn dominant_left_channel_frequency_hz(samples: &[i16], sample_rate_hz: u32) -> f64 {
    let left = samples.iter().step_by(2).copied().collect::<Vec<_>>();
    // Transport capture begins at an arbitrary mux point. Select the loudest
    // one-second window first so AAC silence/noise before or after the tone
    // cannot dilute a zero-crossing measurement.
    let window_frames = sample_rate_hz as usize;
    let (window_start, window) = if left.len() <= window_frames {
        (0, left.as_slice())
    } else {
        let mut best = (0_usize, 0_u64);
        for start in (0..=left.len() - window_frames).step_by(window_frames / 4) {
            let energy = left[start..start + window_frames]
                .iter()
                .map(|sample| u64::from(sample.unsigned_abs()))
                .sum();
            if energy > best.1 {
                best = (start, energy);
            }
        }
        (best.0, &left[best.0..best.0 + window_frames])
    };
    let (frequency_hz, _) = (440_u16..=1_500_u16)
        .step_by(10)
        .map(|frequency_hz| {
            let angular =
                2.0 * std::f64::consts::PI * f64::from(frequency_hz) / f64::from(sample_rate_hz);
            let (real, imaginary) = window.iter().enumerate().fold(
                (0.0_f64, 0.0_f64),
                |(real, imaginary), (index, sample)| {
                    let phase = angular * index as f64;
                    (
                        real + f64::from(*sample) * phase.cos(),
                        imaginary + f64::from(*sample) * phase.sin(),
                    )
                },
            );
            (frequency_hz, real.mul_add(real, imaginary * imaginary))
        })
        .max_by(|(_, left_power), (_, right_power)| left_power.total_cmp(right_power))
        .expect("the safe fallback frequency range is nonempty");
    eprintln!(
        "fallback tone spectrum window begins at frame {window_start}, dominant frequency {frequency_hz}Hz"
    );
    f64::from(frequency_hz)
}

fn assert_sustained_audible_audio(
    capture_path: &Path,
    minimum_samples: u64,
    minimum_half_second_chunks: usize,
) {
    let decoded = Command::new("ffmpeg.exe")
        .args([
            "-nostdin",
            "-loglevel",
            "error",
            "-i",
            capture_path.to_string_lossy().as_ref(),
            "-map",
            "0:a:0",
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "48000",
            "-ac",
            "2",
            "pipe:1",
        ])
        .output()
        .expect("FFmpeg is required to decode the captured MPEG-TS audio proof");
    assert!(
        decoded.status.success(),
        "FFmpeg failed to decode captured audio: {}",
        String::from_utf8_lossy(&decoded.stderr)
    );
    let (sum, count) = decoded
        .stdout
        .chunks_exact(2)
        .map(|bytes| i16::from_le_bytes([bytes[0], bytes[1]]))
        .fold((0_u64, 0_u64), |(sum, count), sample| {
            (sum + u64::from(sample.unsigned_abs()), count + 1)
        });
    let mean_abs = sum as f64 / count.max(1) as f64;
    let chunk_means = decoded
        .stdout
        .chunks_exact(96_000)
        .map(|chunk| {
            let (sum, count) = chunk
                .chunks_exact(2)
                .map(|bytes| i16::from_le_bytes([bytes[0], bytes[1]]))
                .fold((0_u64, 0_u64), |(sum, count), sample| {
                    (sum + u64::from(sample.unsigned_abs()), count + 1)
                });
            sum as f64 / count.max(1) as f64
        })
        .collect::<Vec<_>>();
    eprintln!(
        "decoded audio in {}: {count} samples, mean abs {mean_abs:.2}, half-second chunks {chunk_means:?}",
        capture_path.display()
    );
    assert!(
        count > minimum_samples
            && mean_abs > 100.0
            && chunk_means.len() >= minimum_half_second_chunks
            && chunk_means.iter().all(|mean| *mean > 100.0),
        "FFmpeg audio in {} was not sustained: {count} decoded samples, mean abs {mean_abs:.2}, half-second chunks {chunk_means:?}",
        capture_path.display()
    );
}

struct ProofCapture {
    path: std::path::PathBuf,
}

impl ProofCapture {
    fn new(path: std::path::PathBuf) -> Self {
        Self { path }
    }
}

impl Drop for ProofCapture {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
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
        .set_read_timeout(Some(Duration::from_secs(8)))
        .unwrap();
    let mut datagram = [0_u8; 65_536];
    let received = receiver
        .recv(&mut datagram)
        .expect("output did not produce its first MPEG-TS datagram within eight seconds");
    let mut capture = Vec::from(&datagram[..received]);
    receiver
        .set_read_timeout(Some(Duration::from_millis(500)))
        .unwrap();
    let started = Instant::now();
    while started.elapsed() < duration {
        match receiver.recv(&mut datagram) {
            Ok(received) => capture.extend_from_slice(&datagram[..received]),
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                ) => {}
            Err(error) => panic!("could not capture active MPEG-TS output: {error}"),
        }
    }
    capture
}

fn clear_udp_backlog(receiver: &UdpSocket) {
    receiver.set_nonblocking(true).unwrap();
    let mut datagram = [0_u8; 65_536];
    loop {
        match receiver.recv(&mut datagram) {
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => break,
            Err(error) => panic!("could not clear UDP output backlog: {error}"),
        }
    }
    receiver.set_nonblocking(false).unwrap();
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

fn generate_black_then_white_fixture(path: &Path) {
    let output = Command::new("ffmpeg.exe")
        .args([
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:r=30:d=2",
            "-f",
            "lavfi",
            "-i",
            "color=c=white:s=320x180:r=30:d=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=5",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            "-map",
            "2:a:0",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-g",
            "30",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-f",
            "mpegts",
            path.to_string_lossy().as_ref(),
        ])
        .output()
        .expect("FFmpeg is required to generate the offset fixture");
    assert!(
        output.status.success(),
        "FFmpeg offset fixture generation failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}

fn generate_ffmpeg_color_tone_fixture(
    path: &Path,
    color: &str,
    frequency_hz: u32,
    duration_seconds: u32,
) {
    let output = Command::new("ffmpeg.exe")
        .args([
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            &format!("color=c={color}:s=320x180:r=30"),
            "-f",
            "lavfi",
            "-i",
            &format!("sine=frequency={frequency_hz}:sample_rate=48000"),
            "-t",
            &duration_seconds.to_string(),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-g",
            "30",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-f",
            "mpegts",
            path.to_string_lossy().as_ref(),
        ])
        .output()
        .expect("FFmpeg is required to generate the long catch-up fixture");
    assert!(
        output.status.success(),
        "FFmpeg long catch-up fixture generation failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}

fn center_luma(frame: &DecodedFrame) -> u8 {
    let x = frame.width as usize / 2;
    let y = frame.height as usize / 2;
    frame.bytes[y * frame.stride + x * 4]
}

fn pull_frame_matching(
    sink: &gst::Element,
    timeout: Duration,
    predicate: impl Fn(&DecodedFrame) -> bool,
) -> DecodedFrame {
    let deadline = Instant::now() + timeout;
    loop {
        let frame = pull_decoded_frame(sink);
        if predicate(&frame) {
            return frame;
        }
        assert!(
            Instant::now() < deadline,
            "decoded output never reached the expected timed fixture region"
        );
    }
}

fn test_root(label: &str) -> std::path::PathBuf {
    std::env::temp_dir().join(format!(
        "townlight-engine-{label}-{}-{}",
        std::process::id(),
        std::thread::current().name().unwrap_or("test")
    ))
}
#[test]
fn refuses_a_staged_interlaced_profile_before_building_a_graph() {
    let _guard = persistent_graph_test_guard();
    let result = PersistentPlayout::start_udp_with_profile(
        "127.0.0.1:5500".parse().unwrap(),
        OutputProfileId::Atsc1080i5994,
    );
    assert!(matches!(
        result,
        Err(station_media_engine::EngineError::ProfileNotRunnable { profile_id })
            if profile_id == "atsc-1080i5994"
    ));
}
