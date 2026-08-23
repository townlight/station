use std::net::UdpSocket;
use std::time::Duration;

use station_media_engine::{ConfiguredTransportOutput, PersistentPlayout};
use station_media_protocol::LoudnessTarget;
use station_schedule::OutputProfileId;

#[test]
fn different_targets_create_independent_transport_variants() {
    let cable = UdpSocket::bind("127.0.0.1:0").unwrap();
    let streaming = UdpSocket::bind("127.0.0.1:0").unwrap();
    for receiver in [&cable, &streaming] {
        receiver
            .set_read_timeout(Some(Duration::from_secs(8)))
            .unwrap();
    }
    let outputs = [
        ConfiguredTransportOutput::udp(cable.local_addr().unwrap(), LoudnessTarget::CABLE),
        ConfiguredTransportOutput::udp(streaming.local_addr().unwrap(), LoudnessTarget::STREAMING),
    ];

    let playout = PersistentPlayout::start_configured_outputs_with_profile(
        &outputs,
        OutputProfileId::Proof360p30,
    )
    .expect("one persistent graph must start both loudness variants");

    assert_eq!(playout.output_variant_count(), 2);
    assert_receives_transport(&cable);
    assert_receives_transport(&streaming);
    assert!(
        playout
            .output_health_observations()
            .unwrap()
            .iter()
            .all(|observation| observation.audio_buffers_total > 0),
        "every target group must prove encoded audio progress"
    );
    playout.stop().unwrap();
}

#[test]
fn equal_targets_share_one_encode_and_mux_variant() {
    let primary = UdpSocket::bind("127.0.0.1:0").unwrap();
    let redundant = UdpSocket::bind("127.0.0.1:0").unwrap();
    let outputs = [
        ConfiguredTransportOutput::udp(primary.local_addr().unwrap(), LoudnessTarget::CABLE),
        ConfiguredTransportOutput::udp(redundant.local_addr().unwrap(), LoudnessTarget::CABLE),
    ];

    let playout = PersistentPlayout::start_configured_outputs_with_profile(
        &outputs,
        OutputProfileId::Proof360p30,
    )
    .expect("same-target outputs must share one encoded transport variant");

    assert_eq!(playout.output_variant_count(), 1);
    std::thread::sleep(Duration::from_secs(5));
    assert!(
        playout
            .output_health_observations()
            .unwrap()
            .iter()
            .all(|observation| observation.audio_buffers_total > 0),
        "same-target fanout must retain audio progress for every sink index"
    );
    playout.stop().unwrap();
}

fn assert_receives_transport(receiver: &UdpSocket) {
    let mut datagram = [0_u8; 2_048];
    let size = receiver
        .recv(&mut datagram)
        .expect("configured output must emit transport packets");
    assert_eq!(
        size, 1_316,
        "UDP MPEG-TS payloads must contain seven packets"
    );
    assert_eq!(
        datagram[0], 0x47,
        "payload must begin on an MPEG-TS sync byte"
    );
}
