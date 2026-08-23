use std::net::UdpSocket;
use std::time::Duration;

use station_media_engine::{
    ConfiguredTransportOutput, EngineError, MpegTsMuxContract, PersistentPlayout,
};
use station_media_protocol::LoudnessTarget;
use station_schedule::OutputProfileId;

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
