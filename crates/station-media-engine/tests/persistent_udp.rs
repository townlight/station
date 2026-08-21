use std::collections::HashMap;
use std::net::UdpSocket;
use std::time::Duration;

use station_media_engine::{SourceRole, SyntheticPlayout};

#[test]
fn keeps_one_pipeline_running_while_switching_sources_and_emits_mpeg_ts() {
    let receiver = UdpSocket::bind("127.0.0.1:0").unwrap();
    receiver
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();

    let started = SyntheticPlayout::start_udp(receiver.local_addr().unwrap());
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
    playout.stop().unwrap();
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

#[derive(Default)]
struct ContinuityTracker {
    last_payload_counter: HashMap<u16, u8>,
    payload_packets: usize,
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
}
