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
    last_pts: HashMap<u16, u64>,
    payload_packets: usize,
    pmt_pid: Option<u16>,
    video_pid: Option<u16>,
    audio_pid: Option<u16>,
    latest_video_pts: Option<u64>,
    latest_audio_pts: Option<u64>,
    video_pts_samples: usize,
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
        if let Some(previous) = self.last_pts.insert(pid, pts) {
            assert!(
                pts >= previous,
                "PID {pid:#06x} PTS moved backward from {previous} to {pts}"
            );
        }
        if Some(pid) == self.video_pid {
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
