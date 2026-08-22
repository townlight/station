use std::fs;
use std::io::{Read, Write};
use std::net::UdpSocket;
use std::num::NonZeroU16;
use std::path::PathBuf;
use std::process::{Child, Command, Output, Stdio};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use station_media_journal::read_events;
use station_media_protocol::{
    CgOverlay, CgOverlayContent, CgRect, CgSnapshot, ChannelCommand, CommandEnvelope, LiveArmToken,
    LiveInputConfiguration, LiveReturnTarget, PROTOCOL_VERSION, WorkerBootstrapEnvelope,
    WorkerEvent, WorkerEventEnvelope, WorkerOutputConfiguration, decode_event_frame,
    encode_bootstrap_frame, encode_command_frame,
};
use station_windows_ipc::PipeServer;

const WORKER_ID: &str = "ae819b97-2920-434b-9d96-0b51a8ea9abb";
const CHANNEL_ID: &str = "62a47b7e-2a03-48d6-8703-a6e5ce986527";

fn journal_path(name: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is before Unix epoch")
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-worker-{name}-{nonce}.tlj"))
}

fn command(command_id: &str, expected_sequence: u64, command: ChannelCommand) -> CommandEnvelope {
    CommandEnvelope {
        version: PROTOCOL_VERSION,
        channel_id: CHANNEL_ID.into(),
        command_id: command_id.into(),
        expected_sequence,
        command,
    }
}

fn read_event(stream: &mut impl Read) -> WorkerEventEnvelope {
    let mut length = [0_u8; 4];
    stream.read_exact(&mut length).unwrap();
    let payload_length = u32::from_le_bytes(length) as usize;
    let mut frame = Vec::with_capacity(4 + payload_length);
    frame.extend_from_slice(&length);
    frame.resize(4 + payload_length, 0);
    stream.read_exact(&mut frame[4..]).unwrap();
    decode_event_frame(&frame).unwrap()
}

fn read_command_event(stream: &mut impl Read) -> WorkerEventEnvelope {
    loop {
        let event = read_event(stream);
        if !matches!(event.event, WorkerEvent::Heartbeat { .. }) {
            return event;
        }
    }
}

fn run_worker(
    journal: &PathBuf,
    input: &[u8],
    expected_events: usize,
) -> (Output, Vec<WorkerEventEnvelope>) {
    let suffix = journal
        .file_stem()
        .unwrap()
        .to_string_lossy()
        .replace('.', "-");
    let server = PipeServer::bind(&suffix).unwrap();
    let pipe_name = server.name().to_string();
    let udp = UdpSocket::bind("127.0.0.1:0").unwrap();
    let child = Command::new(env!("CARGO_BIN_EXE_channel-worker"))
        .args([WORKER_ID, CHANNEL_ID])
        .arg(journal)
        .arg(pipe_name)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stream = server.accept().unwrap();
    let bootstrap = WorkerBootstrapEnvelope {
        version: PROTOCOL_VERSION,
        worker_id: WORKER_ID.into(),
        channel_id: CHANNEL_ID.into(),
        output_profile: "proof-360p30".into(),
        cg_image_root: journal
            .parent()
            .unwrap()
            .join("cg-images")
            .to_string_lossy()
            .into_owned(),
        outputs: vec![WorkerOutputConfiguration::UdpTs {
            destination: udp.local_addr().unwrap().to_string(),
        }],
    };
    stream
        .write_all(&encode_bootstrap_frame(&bootstrap).unwrap())
        .unwrap();
    stream.write_all(input).unwrap();
    let events = (0..expected_events)
        .map(|_| read_event(&mut stream))
        .collect();
    (child.wait_with_output().unwrap(), events)
}

struct ChildGuard {
    child: Child,
}

impl ChildGuard {
    fn new(child: Child) -> Self {
        Self { child }
    }

    fn try_wait(&mut self) -> std::io::Result<Option<std::process::ExitStatus>> {
        self.child.try_wait()
    }

    fn wait(&mut self) -> std::io::Result<std::process::ExitStatus> {
        self.child.wait()
    }
}

impl Drop for ChildGuard {
    fn drop(&mut self) {
        if self.child.try_wait().ok().flatten().is_none() {
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }
}

struct TempFiles(Vec<PathBuf>);

impl Drop for TempFiles {
    fn drop(&mut self) {
        for path in &self.0 {
            let _ = fs::remove_file(path);
        }
    }
}

fn start_ffmpeg_live_sender(port: u16) -> ChildGuard {
    ChildGuard::new(
        Command::new("ffmpeg.exe")
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
            .expect("FFmpeg is required for the MPEG-TS interoperability proof"),
    )
}

fn capture_transport_for(receiver: &UdpSocket, duration: Duration) -> Vec<u8> {
    receiver
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    let started = Instant::now();
    let mut datagram = [0_u8; 65_536];
    let mut capture = Vec::new();
    while started.elapsed() < duration {
        let received = receiver
            .recv(&mut datagram)
            .expect("worker UDP output stopped during live capture");
        capture.extend_from_slice(&datagram[..received]);
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
            Err(error) => panic!("could not clear worker UDP output backlog: {error}"),
        }
    }
    receiver.set_nonblocking(false).unwrap();
}

#[test]
fn spawned_worker_live_takeover_preserves_audible_audio() {
    let journal = journal_path("live-audio");
    let capture_path = journal.with_extension("ts");
    let _temp_files = TempFiles(vec![journal.clone(), capture_path.clone()]);
    let server = PipeServer::bind(
        &journal
            .file_stem()
            .unwrap()
            .to_string_lossy()
            .replace('.', "-"),
    )
    .unwrap();
    let output = UdpSocket::bind("127.0.0.1:0").unwrap();
    let child = Command::new(env!("CARGO_BIN_EXE_channel-worker"))
        .args([WORKER_ID, CHANNEL_ID])
        .arg(&journal)
        .arg(server.name())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let mut worker = ChildGuard::new(child);
    let mut stream = server.accept().unwrap();
    let bootstrap = WorkerBootstrapEnvelope {
        version: PROTOCOL_VERSION,
        worker_id: WORKER_ID.into(),
        channel_id: CHANNEL_ID.into(),
        output_profile: "proof-360p30".into(),
        cg_image_root: journal
            .parent()
            .unwrap()
            .join("cg-images")
            .to_string_lossy()
            .into_owned(),
        outputs: vec![WorkerOutputConfiguration::UdpTs {
            destination: output.local_addr().unwrap().to_string(),
        }],
    };
    stream
        .write_all(&encode_bootstrap_frame(&bootstrap).unwrap())
        .unwrap();
    let ready = read_event(&mut stream);
    assert!(matches!(
        ready.event,
        WorkerEvent::Ready { graph_revision: 0 }
    ));

    let input_probe = UdpSocket::bind("127.0.0.1:0").unwrap();
    let input_port = input_probe.local_addr().unwrap().port();
    drop(input_probe);
    let mut sender = start_ffmpeg_live_sender(input_port);
    std::thread::sleep(Duration::from_secs(10));
    assert!(sender.try_wait().unwrap().is_none());

    let session_id = "e8d9c07d-1d04-40cc-9fb7-5c1b13f0e961";
    let source_id = "f7f2d7c5-0f87-4fd0-8d2c-142b6f0d1a42";
    let token = LiveArmToken::new("worker-live-audio-proof-token".into()).unwrap();
    let input = LiveInputConfiguration::new(
        session_id.into(),
        source_id.into(),
        1,
        "127.0.0.1".into(),
        NonZeroU16::new(input_port).unwrap(),
    )
    .unwrap();
    stream
        .write_all(
            &encode_command_frame(&command(
                "arm-live",
                2,
                ChannelCommand::ArmLive {
                    input,
                    arm_token: token.clone(),
                },
            ))
            .unwrap(),
        )
        .unwrap();
    assert!(matches!(
        read_command_event(&mut stream).event,
        WorkerEvent::LiveArmed {
            session_id: ref event_session,
            ..
        }
            if event_session == session_id
    ));
    stream
        .write_all(
            &encode_command_frame(&command(
                "take-live",
                3,
                ChannelCommand::TakeLive {
                    session_id: session_id.into(),
                    arm_token: token,
                },
            ))
            .unwrap(),
        )
        .unwrap();
    let take_event = read_command_event(&mut stream);
    assert!(
        matches!(
        &take_event.event,
        WorkerEvent::LiveOnAir {
            session_id: event_session,
            ..
        }
            if event_session == session_id
        ),
        "unexpected take event: {:?}",
        take_event.event
    );

    clear_udp_backlog(&output);
    std::thread::sleep(Duration::from_millis(750));
    let transport_capture = capture_transport_for(&output, Duration::from_secs(4));
    assert!(
        transport_capture.len() > 100_000,
        "live capture was too short: {} bytes",
        transport_capture.len()
    );
    fs::write(&capture_path, &transport_capture).unwrap();

    stream
        .write_all(&encode_command_frame(&command("ping-live", 4, ChannelCommand::Ping)).unwrap())
        .unwrap();
    let heartbeat = read_event(&mut stream);
    assert!(
        matches!(heartbeat.event, WorkerEvent::Heartbeat { .. }),
        "live worker did not answer its health poll: {:?}",
        heartbeat.event
    );

    stream
        .write_all(
            &encode_command_frame(&command(
                "return-live",
                5,
                ChannelCommand::ReturnFromLive {
                    session_id: session_id.into(),
                    target: LiveReturnTarget::Fallback,
                },
            ))
            .unwrap(),
        )
        .unwrap();
    assert!(matches!(
        read_command_event(&mut stream).event,
        WorkerEvent::LiveReturned {
            session_id: ref event_session,
            ref source_kind,
            ref source_id,
        }
            if event_session == session_id
                && source_kind == "fallback"
                && source_id == "station-fallback"
    ));
    stream
        .write_all(
            &encode_command_frame(&command(
                "disarm-live",
                6,
                ChannelCommand::DisarmLive {
                    session_id: session_id.into(),
                },
            ))
            .unwrap(),
        )
        .unwrap();
    assert!(matches!(
        read_command_event(&mut stream).event,
        WorkerEvent::LiveDisarmed {
            session_id: ref event_session,
        }
            if event_session == session_id
    ));
    drop(sender);
    stream
        .write_all(
            &encode_command_frame(&command("shutdown", 7, ChannelCommand::Shutdown)).unwrap(),
        )
        .unwrap();
    assert!(matches!(
        read_command_event(&mut stream).event,
        WorkerEvent::ShutdownComplete
    ));
    let status = worker.wait().unwrap();
    assert!(status.success());

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
    assert!(
        count > 300_000
            && mean_abs > 100.0
            && chunk_means.len() >= 6
            && chunk_means.iter().all(|mean| *mean > 100.0),
        "FFmpeg live audio was not sustained: {count} decoded samples, mean abs {mean_abs:.2}, half-second chunks {chunk_means:?}"
    );
}

#[test]
fn journals_each_event_before_emitting_it_and_recovers_sequence_after_restart() {
    let journal = journal_path("lifecycle");
    let mut input = Vec::new();
    input.extend_from_slice(
        &encode_command_frame(&command("command-1", 2, ChannelCommand::Ping)).unwrap(),
    );
    input.extend_from_slice(
        &encode_command_frame(&command("command-2", 3, ChannelCommand::Shutdown)).unwrap(),
    );

    let (output, emitted) = run_worker(&journal, &input, 3);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );

    assert!(output.stdout.is_empty(), "worker leaked events to stdout");
    assert_eq!(emitted.len(), 3);
    assert!(matches!(
        emitted[0].event,
        WorkerEvent::Ready { graph_revision: 0 }
    ));
    assert!(matches!(emitted[1].event, WorkerEvent::Heartbeat { .. }));
    assert!(matches!(emitted[2].event, WorkerEvent::ShutdownComplete));
    assert_eq!(read_events(&journal).unwrap(), emitted);

    let restart_input =
        encode_command_frame(&command("command-3", 5, ChannelCommand::Shutdown)).unwrap();
    let (restart, restart_emitted) = run_worker(&journal, &restart_input, 2);
    assert!(
        restart.status.success(),
        "{}",
        String::from_utf8_lossy(&restart.stderr)
    );
    assert!(restart.stdout.is_empty(), "worker leaked events to stdout");
    assert_eq!(restart_emitted.len(), 2);
    assert_eq!(restart_emitted[0].sequence, 4);
    assert!(matches!(
        restart_emitted[0].event,
        WorkerEvent::Ready { graph_revision: 0 }
    ));
    assert_eq!(restart_emitted[1].sequence, 5);
    assert!(matches!(
        restart_emitted[1].event,
        WorkerEvent::ShutdownComplete
    ));

    let persisted = read_events(&journal).unwrap();
    assert_eq!(persisted.len(), 5);
    assert_eq!(&persisted[3..], restart_emitted.as_slice());

    let _ = std::fs::remove_file(journal);
}

#[test]
fn rejects_a_stale_command_and_durably_records_the_rejection() {
    let journal = journal_path("stale-command");
    let mut input =
        encode_command_frame(&command("stale-command", 99, ChannelCommand::Shutdown)).unwrap();
    input.extend_from_slice(
        &encode_command_frame(&command("shutdown", 3, ChannelCommand::Shutdown)).unwrap(),
    );
    let (output, emitted) = run_worker(&journal, &input, 3);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );

    assert!(output.stdout.is_empty(), "worker leaked events to stdout");
    assert_eq!(emitted.len(), 3);
    assert!(matches!(
        &emitted[1].event,
        WorkerEvent::CommandRejected { command_id, code, .. }
            if command_id == "stale-command" && code == "sequence_conflict"
    ));
    assert!(matches!(emitted[2].event, WorkerEvent::ShutdownComplete));
    assert_eq!(read_events(&journal).unwrap(), emitted);

    let _ = std::fs::remove_file(journal);
}

#[test]
fn applies_and_clears_cg_without_restarting_the_worker_graph() {
    let journal = journal_path("cg-lifecycle");
    let snapshot = CgSnapshot {
        board_id: "board-1".into(),
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
                text: "worker CG proof".into(),
            },
        }],
    };
    let mut input = Vec::new();
    input.extend_from_slice(
        &encode_command_frame(&command(
            "cg-apply",
            2,
            ChannelCommand::ApplyCgSnapshot { snapshot },
        ))
        .unwrap(),
    );
    input.extend_from_slice(
        &encode_command_frame(&command("cg-clear", 3, ChannelCommand::ClearCgSnapshot)).unwrap(),
    );
    input.extend_from_slice(
        &encode_command_frame(&command("shutdown", 4, ChannelCommand::Shutdown)).unwrap(),
    );

    let (output, emitted) = run_worker(&journal, &input, 4);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(matches!(
        &emitted[1].event,
        WorkerEvent::CgApplied {
            board_id,
            board_revision: 4
        } if board_id == "board-1"
    ));
    assert!(matches!(&emitted[2].event, WorkerEvent::CgCleared));
    assert!(matches!(emitted[3].event, WorkerEvent::ShutdownComplete));
    assert_eq!(read_events(&journal).unwrap(), emitted);
    let _ = std::fs::remove_file(journal);
}
