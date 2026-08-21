use std::io::{ErrorKind, Read, Write};
use std::path::PathBuf;
use std::time::Instant;

use station_media_journal::JournalWriter;
use station_media_protocol::{
    ChannelCommand, CommandEnvelope, MAX_FRAME_BYTES, PROTOCOL_VERSION, WorkerEvent,
    WorkerEventEnvelope, decode_command_frame, encode_event_frame,
};

fn main() {
    let (worker_id, channel_id, journal_path) = arguments().unwrap_or_else(|message| {
        eprintln!("{message}");
        std::process::exit(2);
    });
    let mut journal = JournalWriter::open(journal_path, &worker_id, &channel_id)
        .unwrap_or_else(|error| fatal(3, "open its journal", &format!("{error:?}")));
    let started = Instant::now();
    let stdout = std::io::stdout();
    let mut output = stdout.lock();

    record_and_emit(
        &mut journal,
        &mut output,
        &worker_id,
        &channel_id,
        WorkerEvent::Ready { graph_revision: 0 },
    )
    .unwrap_or_else(|error| fatal(4, "record and emit readiness", &error));

    let stdin = std::io::stdin();
    let mut input = stdin.lock();
    loop {
        let frame = match read_command_frame(&mut input) {
            Ok(Some(frame)) => frame,
            Ok(None) => break,
            Err(error) => fatal(5, "read a command frame", &error),
        };
        let command = decode_command_frame(&frame)
            .unwrap_or_else(|error| fatal(6, "decode a command frame", &format!("{error:?}")));
        let (event, should_stop) = decide_event(
            &command,
            &channel_id,
            journal.next_sequence(),
            started.elapsed().as_millis() as u64,
        );
        record_and_emit(&mut journal, &mut output, &worker_id, &channel_id, event)
            .unwrap_or_else(|error| fatal(7, "record and emit an event", &error));
        if should_stop {
            break;
        }
    }
}

fn arguments() -> Result<(String, String, PathBuf), &'static str> {
    let mut arguments = std::env::args_os().skip(1);
    let Some(worker_id) = arguments.next().and_then(|value| value.into_string().ok()) else {
        return Err("usage: channel-worker <worker-id> <channel-id> <journal-path>");
    };
    let Some(channel_id) = arguments.next().and_then(|value| value.into_string().ok()) else {
        return Err("usage: channel-worker <worker-id> <channel-id> <journal-path>");
    };
    let Some(journal_path) = arguments.next().map(PathBuf::from) else {
        return Err("usage: channel-worker <worker-id> <channel-id> <journal-path>");
    };
    if arguments.next().is_some() {
        return Err("usage: channel-worker <worker-id> <channel-id> <journal-path>");
    }
    Ok((worker_id, channel_id, journal_path))
}

fn read_command_frame(input: &mut impl Read) -> Result<Option<Vec<u8>>, String> {
    let mut length = [0_u8; 4];
    match input.read(&mut length[..1]) {
        Ok(0) => return Ok(None),
        Ok(1) => {}
        Ok(_) => unreachable!("the read buffer has one byte"),
        Err(error) => return Err(error.to_string()),
    }
    input
        .read_exact(&mut length[1..])
        .map_err(|error| incomplete_frame(error, "command length"))?;
    let payload_length = u32::from_le_bytes(length) as usize;
    if payload_length > MAX_FRAME_BYTES {
        return Err(format!("command frame is too large: {payload_length}"));
    }
    let mut frame = Vec::with_capacity(4 + payload_length);
    frame.extend_from_slice(&length);
    frame.resize(4 + payload_length, 0);
    input
        .read_exact(&mut frame[4..])
        .map_err(|error| incomplete_frame(error, "command payload"))?;
    Ok(Some(frame))
}

fn decide_event(
    command: &CommandEnvelope,
    channel_id: &str,
    next_sequence: u64,
    monotonic_milliseconds: u64,
) -> (WorkerEvent, bool) {
    if command.channel_id != channel_id {
        return (
            rejection(
                command,
                "wrong_channel",
                "The command targets another channel.",
            ),
            false,
        );
    }
    if command.expected_sequence != next_sequence {
        return (
            rejection(
                command,
                "sequence_conflict",
                &format!(
                    "The command expected worker sequence {}, but the next sequence is {}.",
                    command.expected_sequence, next_sequence
                ),
            ),
            false,
        );
    }
    match &command.command {
        ChannelCommand::Ping => (
            WorkerEvent::Heartbeat {
                monotonic_milliseconds,
            },
            false,
        ),
        ChannelCommand::ApplyPlan { revision, .. } => (
            WorkerEvent::Ready {
                graph_revision: *revision,
            },
            false,
        ),
        ChannelCommand::Shutdown => (WorkerEvent::ShutdownComplete, true),
        ChannelCommand::ArmLive { .. }
        | ChannelCommand::TakeLive { .. }
        | ChannelCommand::ReturnToSchedule => (
            rejection(
                command,
                "not_implemented",
                "This media transition is not implemented in the current worker.",
            ),
            false,
        ),
    }
}

fn rejection(command: &CommandEnvelope, code: &str, message: &str) -> WorkerEvent {
    WorkerEvent::CommandRejected {
        command_id: command.command_id.clone(),
        code: code.to_string(),
        message: message.to_string(),
    }
}

fn record_and_emit(
    journal: &mut JournalWriter,
    output: &mut impl Write,
    worker_id: &str,
    channel_id: &str,
    event: WorkerEvent,
) -> Result<(), String> {
    let envelope = WorkerEventEnvelope {
        version: PROTOCOL_VERSION,
        worker_id: worker_id.to_string(),
        channel_id: channel_id.to_string(),
        sequence: journal.next_sequence(),
        event,
    };
    journal
        .append(&envelope)
        .map_err(|error| format!("{error:?}"))?;
    let frame = encode_event_frame(&envelope).map_err(|error| format!("{error:?}"))?;
    output
        .write_all(&frame)
        .map_err(|error| error.to_string())?;
    output.flush().map_err(|error| error.to_string())
}

fn incomplete_frame(error: std::io::Error, field: &str) -> String {
    if error.kind() == ErrorKind::UnexpectedEof {
        format!("incomplete {field}")
    } else {
        error.to_string()
    }
}

fn fatal(code: i32, action: &str, error: &str) -> ! {
    eprintln!("channel worker could not {action}: {error}");
    std::process::exit(code);
}
