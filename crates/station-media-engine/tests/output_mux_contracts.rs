use std::fs;
use std::net::UdpSocket;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use station_media_engine::{
    ConfiguredTransportOutput, EngineError, MpegTsMuxContract, OutputCaptureRequest,
    OutputCaptureStatus, PersistentPlayout,
};
use station_media_protocol::LoudnessTarget;
use station_schedule::OutputProfileId;

const GENERIC_CAPTURE_PROOF_ID: &str = "11111111-2222-4333-8444-555555555555";
const GENERIC_CAPTURE_SINK_ID: &str = "22222222-3333-4444-8555-666666666666";

#[test]
fn mux_semantics_are_part_of_transport_variant_identity() {
    let default_receiver = receiver();
    let custom_receiver = receiver();
    let outputs = [
        ConfiguredTransportOutput::udp(
            default_receiver.local_addr().unwrap(),
            LoudnessTarget::CABLE,
        ),
        ConfiguredTransportOutput::udp_with_mux_contract(
            custom_receiver.local_addr().unwrap(),
            LoudnessTarget::CABLE,
            MpegTsMuxContract::new(1_600_000, 752),
        ),
    ];

    let playout = PersistentPlayout::start_configured_outputs_with_profile(
        &outputs,
        OutputProfileId::Proof360p30,
    )
    .expect("valid distinct mux contracts must start");

    assert_eq!(
        playout.output_variant_count(),
        2,
        "equal loudness must not collapse outputs with different mux semantics"
    );
    assert_datagram_size(&default_receiver, 1_316);
    assert_datagram_size(&custom_receiver, 752);
    playout.stop().unwrap();
}

#[test]
fn an_explicit_default_contract_shares_the_default_variant() {
    let implicit_receiver = receiver();
    let explicit_receiver = receiver();
    let outputs = [
        ConfiguredTransportOutput::udp(
            implicit_receiver.local_addr().unwrap(),
            LoudnessTarget::CABLE,
        ),
        ConfiguredTransportOutput::udp_with_mux_contract(
            explicit_receiver.local_addr().unwrap(),
            LoudnessTarget::CABLE,
            MpegTsMuxContract::new(1_500_000, 1_316),
        ),
    ];

    let playout = PersistentPlayout::start_configured_outputs_with_profile(
        &outputs,
        OutputProfileId::Proof360p30,
    )
    .expect("explicit profile-default mux contract must start");

    assert_eq!(playout.output_variant_count(), 1);
    assert_datagram_size(&implicit_receiver, 1_316);
    assert_datagram_size(&explicit_receiver, 1_316);
    playout.stop().unwrap();
}

#[test]
fn generic_headend_mux_supports_bounded_post_mux_capture_without_stalling_output() {
    let receiver = receiver();
    let test_root = std::env::temp_dir().join(format!(
        "townlight-generic-headend-capture-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    let proof_root = test_root.join("output-proofs");
    fs::create_dir_all(&proof_root).unwrap();
    let output = ConfiguredTransportOutput::udp_identified_with_mux_contract(
        GENERIC_CAPTURE_SINK_ID,
        receiver.local_addr().unwrap(),
        LoudnessTarget::CABLE,
        MpegTsMuxContract::new(8_000_000, 1_316),
    );
    let mut playout = PersistentPlayout::start_configured_outputs_with_profile_and_roots(
        &[output],
        OutputProfileId::Atsc720p5994,
        None,
        Some(&proof_root),
    )
    .expect("the runnable Generic headend graph must start");
    assert_datagram_size(&receiver, 1_316);

    let before = playout.output_health_observations().unwrap()[0];
    playout
        .start_output_capture(OutputCaptureRequest {
            proof_id: GENERIC_CAPTURE_PROOF_ID.into(),
            output_index: 0,
            sink_id: GENERIC_CAPTURE_SINK_ID.into(),
            loudness_target: LoudnessTarget::CABLE,
            duration_ms: 3_000,
            max_bytes: 8 * 1024 * 1024,
        })
        .expect("the Generic post-mux capture must start");

    let deadline = Instant::now() + Duration::from_secs(15);
    let ready = loop {
        assert!(
            Instant::now() < deadline,
            "Generic post-mux capture timed out"
        );
        match playout
            .poll_output_capture(GENERIC_CAPTURE_PROOF_ID)
            .unwrap()
        {
            OutputCaptureStatus::Pending { .. } => {
                std::thread::sleep(Duration::from_millis(50));
            }
            OutputCaptureStatus::Ready(ready) => break ready,
            OutputCaptureStatus::Failed { code, message, .. } => {
                panic!("Generic post-mux capture failed: {code}: {message}")
            }
        }
    };
    assert_eq!(ready.output_index, 0);
    assert_eq!(ready.sink_id, GENERIC_CAPTURE_SINK_ID);
    assert_eq!(ready.loudness_target, LoudnessTarget::CABLE);
    assert!(ready.capture_bytes > 0);
    assert!(ready.capture_bytes <= 8 * 1024 * 1024);
    assert_eq!(ready.capture_bytes % 188, 0);
    assert_eq!(ready.capture_duration_ms, 3_000);
    assert!(proof_root.join(ready.artifact_relative_path).is_file());

    let after = playout.output_health_observations().unwrap()[0];
    assert!(after.bytes_sent_total > before.bytes_sent_total);
    assert!(after.audio_buffers_total > before.audio_buffers_total);
    playout.stop().unwrap();
    fs::remove_dir_all(test_root).unwrap();
}

#[test]
fn mux_contracts_reject_insufficient_transport_bitrate_before_graph_startup() {
    let output = ConfiguredTransportOutput::udp_with_mux_contract(
        "127.0.0.1:23001".parse().unwrap(),
        LoudnessTarget::CABLE,
        MpegTsMuxContract::new(1_128_000, 1_316),
    );

    let error = match PersistentPlayout::start_configured_outputs_with_profile(
        &[output],
        OutputProfileId::Proof360p30,
    ) {
        Ok(playout) => {
            playout.stop().unwrap();
            panic!("transport bitrate must exceed the encoded elementary streams");
        }
        Err(error) => error,
    };

    assert!(matches!(
        error,
        EngineError::UnsupportedMuxContract {
            transport_bitrate_bps: 1_128_000,
            packet_size_bytes: 1_316,
            ..
        }
    ));
}

#[test]
fn mux_contracts_reject_non_transport_packet_alignment_before_graph_startup() {
    let output = ConfiguredTransportOutput::udp_with_mux_contract(
        "127.0.0.1:23002".parse().unwrap(),
        LoudnessTarget::CABLE,
        MpegTsMuxContract::new(1_500_000, 1_315),
    );

    let error = match PersistentPlayout::start_configured_outputs_with_profile(
        &[output],
        OutputProfileId::Proof360p30,
    ) {
        Ok(playout) => {
            playout.stop().unwrap();
            panic!("packet size must contain whole 188-byte MPEG-TS packets");
        }
        Err(error) => error,
    };

    assert!(matches!(
        error,
        EngineError::UnsupportedMuxContract {
            transport_bitrate_bps: 1_500_000,
            packet_size_bytes: 1_315,
            ..
        }
    ));
}

fn receiver() -> UdpSocket {
    let receiver = UdpSocket::bind("127.0.0.1:0").unwrap();
    receiver
        .set_read_timeout(Some(Duration::from_secs(8)))
        .unwrap();
    receiver
}

fn assert_datagram_size(receiver: &UdpSocket, expected: usize) {
    let mut datagram = [0_u8; 2_048];
    let size = receiver
        .recv(&mut datagram)
        .expect("configured output must emit transport packets");
    assert_eq!(size, expected);
    assert_eq!(datagram[0], 0x47);
}
