use std::fs;
use std::net::UdpSocket;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use station_media_assets::{measure_output_loudness, transcode_playout_asset};
use station_media_engine::{
    ConfiguredTransportOutput, OutputCaptureRequest, OutputCaptureStatus, PersistentPlayout,
};
use station_media_protocol::LoudnessTarget;
use station_schedule::OutputProfileId;

const PROOF_ID: &str = "11111111-2222-4333-8444-555555555555";
const SINK_ID: &str = "22222222-3333-4444-8555-666666666666";
const SECOND_SINK_ID: &str = "33333333-4444-4555-8666-777777777777";
const MAX_DURATION_PROOF_ID: &str = "44444444-5555-4666-8777-888888888888";

#[test]
fn post_mux_capture_is_bounded_decodable_and_does_not_interrupt_playout() {
    let receiver = UdpSocket::bind("127.0.0.1:0").unwrap();
    let second_receiver = UdpSocket::bind("127.0.0.1:0").unwrap();
    let root = temporary_root("post-mux-capture");
    fs::create_dir_all(&root).unwrap();
    let source = root.join("source.ts");
    generate_ffmpeg_color_tone_fixture(&source, 25);
    let asset = transcode_playout_asset(&source, root.join("library"))
        .expect("the source must become a canonical -24 LUFS playout asset");
    let canonical_measurement = measure_output_loudness(&asset.stored_path, 20_000)
        .expect("the canonical file must carry independently measurable audio");
    assert!(
        (canonical_measurement.integrated_lufs - LoudnessTarget::CABLE.as_lufs()).abs() <= 2.0,
        "canonical input measured {:.3} LUFS instead of the expected {:.1} LUFS",
        canonical_measurement.integrated_lufs,
        LoudnessTarget::CABLE.as_lufs()
    );
    let proof_root = root.join("output-proofs");
    let outputs = [
        ConfiguredTransportOutput::udp_identified(
            SINK_ID,
            receiver.local_addr().unwrap(),
            LoudnessTarget::STREAMING,
        ),
        ConfiguredTransportOutput::udp_identified(
            SECOND_SINK_ID,
            second_receiver.local_addr().unwrap(),
            LoudnessTarget::STREAMING,
        ),
    ];
    let mut playout = PersistentPlayout::start_configured_outputs_with_profile_and_roots(
        &outputs,
        OutputProfileId::Proof360p30,
        None,
        Some(&proof_root),
    )
    .expect("the persistent proof graph must start");
    assert_eq!(playout.output_variant_count(), 1);
    playout
        .load_asset(&asset.asset_id, &asset.stored_path)
        .expect("the canonical asset must load while fallback remains on air");
    playout
        .take_program_at(0)
        .expect("the decoded file program must go on air");

    let before = playout.output_health_observations().unwrap()[0];
    let started = playout
        .start_output_capture(OutputCaptureRequest {
            proof_id: PROOF_ID.into(),
            output_index: 0,
            sink_id: SINK_ID.into(),
            loudness_target: LoudnessTarget::STREAMING,
            duration_ms: 12_000,
            max_bytes: 16 * 1024 * 1024,
        })
        .expect("capture must arm without rebuilding the graph");
    assert_eq!(started.artifact_relative_path, format!("{PROOF_ID}.ts"));
    assert!(
        playout
            .start_output_capture(OutputCaptureRequest {
                proof_id: "44444444-5555-4666-8777-888888888888".into(),
                output_index: 1,
                sink_id: SECOND_SINK_ID.into(),
                loudness_target: LoudnessTarget::STREAMING,
                duration_ms: 12_000,
                max_bytes: 16 * 1024 * 1024,
            })
            .is_err(),
        "same-target sinks must share one active target-group capture"
    );

    let deadline = Instant::now() + Duration::from_secs(25);
    let ready = loop {
        assert!(Instant::now() < deadline, "bounded capture did not finish");
        match playout.poll_output_capture(PROOF_ID).unwrap() {
            OutputCaptureStatus::Pending { .. } => {
                std::thread::sleep(Duration::from_millis(50));
            }
            OutputCaptureStatus::Ready(ready) => break ready,
            OutputCaptureStatus::Failed { code, message, .. } => {
                panic!("capture failed: {code}: {message}")
            }
        }
    };
    assert_eq!(ready.output_index, 0);
    assert_eq!(ready.sink_id, SINK_ID);
    assert_eq!(ready.loudness_target, LoudnessTarget::STREAMING);
    assert!(ready.capture_bytes > 0);
    assert!(ready.capture_bytes <= 16 * 1024 * 1024);
    assert_eq!(ready.capture_sha256.len(), 64);
    assert_eq!(ready.capture_duration_ms, 12_000);

    let observations = playout.output_health_observations().unwrap();
    let during = observations[0];
    assert!(during.bytes_sent_total > before.bytes_sent_total);
    assert!(during.audio_buffers_total > before.audio_buffers_total);
    assert!(observations[1].bytes_sent_total > 0);
    assert!(observations[1].audio_buffers_total > 0);

    let artifact = proof_root.join(&ready.artifact_relative_path);
    let measured = measure_output_loudness(&artifact, 10_000)
        .expect("captured post-mux bytes must independently decode and measure");
    assert!(
        (measured.integrated_lufs - LoudnessTarget::STREAMING.as_lufs()).abs() <= 2.0,
        "post-mux output measured {:.3} LUFS instead of the configured {:.1} LUFS target",
        measured.integrated_lufs,
        LoudnessTarget::STREAMING.as_lufs()
    );
    assert!(measured.analyzed_duration_ms >= 10_000);

    playout
        .start_output_capture(OutputCaptureRequest {
            proof_id: MAX_DURATION_PROOF_ID.into(),
            output_index: 0,
            sink_id: SINK_ID.into(),
            loudness_target: LoudnessTarget::STREAMING,
            duration_ms: 304_000,
            max_bytes: 16 * 1024 * 1024,
        })
        .expect("the 300-second sample plus decode headroom must be accepted");
    playout
        .abort_output_capture(MAX_DURATION_PROOF_ID)
        .expect("the maximum-duration validation capture must abort cleanly");
    assert!(matches!(
        playout.poll_output_capture(MAX_DURATION_PROOF_ID).unwrap(),
        OutputCaptureStatus::Failed { ref code, .. } if code == "capture_aborted"
    ));

    let before_after = playout.output_health_observations().unwrap()[0];
    std::thread::sleep(Duration::from_millis(300));
    let after = playout.output_health_observations().unwrap()[0];
    assert!(after.bytes_sent_total > before_after.bytes_sent_total);
    assert!(after.audio_buffers_total > before_after.audio_buffers_total);
    playout.stop().unwrap();

    fs::remove_dir_all(root).unwrap();
}

fn generate_ffmpeg_color_tone_fixture(path: &Path, duration_seconds: u32) {
    let output = Command::new("ffmpeg.exe")
        .args([
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x180:r=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=997:sample_rate=48000",
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
        .expect("FFmpeg is required to generate the file-backed loudness fixture");
    assert!(
        output.status.success(),
        "FFmpeg loudness fixture generation failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}

fn temporary_root(name: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    std::env::temp_dir().join(format!(
        "townlight-engine-{name}-{}-{nonce}",
        std::process::id()
    ))
}
