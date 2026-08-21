use std::io::{Read, Write};
use std::path::PathBuf;
use std::process::{Command, Output, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

use station_media_journal::read_events;
use station_media_protocol::{
    ChannelCommand, CommandEnvelope, PROTOCOL_VERSION, WorkerEvent, WorkerEventEnvelope,
    decode_event_frame, encode_command_frame,
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
    stream.write_all(input).unwrap();
    let events = (0..expected_events)
        .map(|_| read_event(&mut stream))
        .collect();
    (child.wait_with_output().unwrap(), events)
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
