use std::io::{ErrorKind, Read, Write};
use std::net::SocketAddr;
use std::path::PathBuf;
use std::time::Instant;

use station_media_engine::{PersistentPlayout, SourceRole, TransportOutput};
use station_media_journal::JournalWriter;
use station_media_protocol::{
    ChannelCommand, CommandEnvelope, MAX_FRAME_BYTES, OutputTransportObservation, PROTOCOL_VERSION,
    WorkerEvent, WorkerEventEnvelope, WorkerOutputConfiguration, decode_bootstrap_frame,
    decode_command_frame, encode_event_frame,
};
use station_schedule::OutputProfileId;
use station_windows_ipc::PipeStream;

struct WorkerArguments {
    worker_id: String,
    channel_id: String,
    journal_path: PathBuf,
    pipe_name: String,
}

fn main() {
    let WorkerArguments {
        worker_id,
        channel_id,
        journal_path,
        pipe_name,
    } = arguments().unwrap_or_else(|message| {
        eprintln!("{message}");
        std::process::exit(2);
    });
    let mut journal = JournalWriter::open(journal_path, &worker_id, &channel_id)
        .unwrap_or_else(|error| fatal(3, "open its journal", &format!("{error:?}")));
    let started = Instant::now();
    let mut pipe = PipeStream::connect(&pipe_name)
        .unwrap_or_else(|error| fatal(5, "connect to its control pipe", &format!("{error:?}")));
    let bootstrap_frame = read_command_frame(&mut pipe)
        .unwrap_or_else(|error| fatal(6, "read its bootstrap frame", &error))
        .unwrap_or_else(|| fatal(6, "read its bootstrap frame", "the supervisor disconnected"));
    let bootstrap = decode_bootstrap_frame(&bootstrap_frame)
        .unwrap_or_else(|error| fatal(6, "decode its bootstrap frame", &format!("{error:?}")));
    if bootstrap.worker_id != worker_id || bootstrap.channel_id != channel_id {
        fatal(
            6,
            "validate its bootstrap identity",
            "worker or channel identity did not match the process identity",
        );
    }
    let output_profile = OutputProfileId::from_storage_name(&bootstrap.output_profile)
        .unwrap_or_else(|error| fatal(6, "validate its output profile", &format!("{error:?}")));
    let outputs = bootstrap
        .outputs
        .into_iter()
        .map(|output| match output {
            WorkerOutputConfiguration::UdpTs { destination } => destination
                .parse::<SocketAddr>()
                .map(TransportOutput::Udp)
                .map_err(|error| error.to_string()),
            WorkerOutputConfiguration::Srt { uri, passphrase } => {
                validate_srt_uri(&uri).map(|()| TransportOutput::Srt {
                    uri,
                    passphrase: passphrase.map(|value| value.expose().to_string()),
                })
            }
        })
        .collect::<Result<Vec<_>, _>>()
        .unwrap_or_else(|error| fatal(6, "validate its output destinations", &error.to_string()));
    if outputs.is_empty() {
        fatal(
            6,
            "validate its output destinations",
            "at least one output is required",
        );
    }
    let mut media = Some(
        PersistentPlayout::start_outputs_with_profile(&outputs, output_profile)
            .unwrap_or_else(|error| fatal(8, "start its fallback graph", &format!("{error:?}"))),
    );

    record_and_emit(
        &mut journal,
        &mut pipe,
        &worker_id,
        &channel_id,
        WorkerEvent::Ready { graph_revision: 0 },
    )
    .unwrap_or_else(|error| fatal(4, "record and emit readiness", &error));
    let mut loaded_asset = None;

    loop {
        let frame = match read_command_frame(&mut pipe) {
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
            media
                .as_mut()
                .expect("a running worker owns its media graph"),
            &mut loaded_asset,
        );
        if should_stop {
            media
                .take()
                .expect("a running worker owns its media graph")
                .stop()
                .unwrap_or_else(|error| fatal(9, "stop its media graph", &format!("{error:?}")));
        }
        record_and_emit(&mut journal, &mut pipe, &worker_id, &channel_id, event)
            .unwrap_or_else(|error| fatal(7, "record and emit an event", &error));
        if should_stop {
            break;
        }
    }
}

fn validate_srt_uri(uri: &str) -> Result<(), String> {
    let authority = uri
        .strip_prefix("srt://")
        .ok_or_else(|| "SRT output URI must start with srt://".to_string())?;
    if authority.contains(['?', '#', '@']) {
        return Err("SRT output URI cannot contain options or credentials".into());
    }
    let destination = authority
        .parse::<SocketAddr>()
        .map_err(|_| "SRT output URI must contain an IP address and port".to_string())?;
    if destination.port() == 0 {
        return Err("SRT output port must be nonzero".into());
    }
    Ok(())
}

fn arguments() -> Result<WorkerArguments, String> {
    const USAGE: &str = "usage: channel-worker <worker-id> <channel-id> <journal-path> <pipe-name>";
    let mut arguments = std::env::args_os().skip(1);
    let Some(worker_id) = arguments.next().and_then(|value| value.into_string().ok()) else {
        return Err(USAGE.into());
    };
    let Some(channel_id) = arguments.next().and_then(|value| value.into_string().ok()) else {
        return Err(USAGE.into());
    };
    let Some(journal_path) = arguments.next().map(PathBuf::from) else {
        return Err(USAGE.into());
    };
    let Some(pipe_name) = arguments.next().and_then(|value| value.into_string().ok()) else {
        return Err(USAGE.into());
    };
    if arguments.next().is_some() {
        return Err(USAGE.into());
    }
    Ok(WorkerArguments {
        worker_id,
        channel_id,
        journal_path,
        pipe_name,
    })
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
    media: &mut PersistentPlayout,
    loaded_asset: &mut Option<String>,
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
        ChannelCommand::Ping => match media.output_bytes_sent().and_then(|bytes_sent| {
            bytes_sent
                .into_iter()
                .enumerate()
                .map(|(output_index, bytes_sent_total)| {
                    u16::try_from(output_index)
                        .map(|output_index| OutputTransportObservation {
                            output_index,
                            bytes_sent_total,
                        })
                        .map_err(|_| {
                            station_media_engine::EngineError::State(
                                "output count exceeds the heartbeat protocol limit".into(),
                            )
                        })
                })
                .collect::<Result<Vec<_>, _>>()
        }) {
            Ok(outputs) => (
                WorkerEvent::Heartbeat {
                    monotonic_milliseconds,
                    outputs,
                },
                false,
            ),
            Err(error) => (
                rejection(
                    command,
                    "output_health_failed",
                    &format!("Required output health could not be verified: {error:?}"),
                ),
                false,
            ),
        },
        ChannelCommand::ApplyPlan { revision, .. } => (
            WorkerEvent::Ready {
                graph_revision: *revision,
            },
            false,
        ),
        ChannelCommand::ApplyCgSnapshot { snapshot } => match media.apply_cg_snapshot(snapshot) {
            Ok(()) => (
                WorkerEvent::CgApplied {
                    board_id: snapshot.board_id.clone(),
                    board_revision: snapshot.board_revision,
                },
                false,
            ),
            Err(error) => (
                rejection(
                    command,
                    "cg_apply_failed",
                    &format!("The CG snapshot could not be applied: {error:?}"),
                ),
                false,
            ),
        },
        ChannelCommand::ClearCgSnapshot => {
            media.clear_cg_snapshot();
            (WorkerEvent::CgCleared, false)
        }
        ChannelCommand::LoadAsset {
            asset_id,
            media_path,
        } => match media.load_asset(asset_id, media_path) {
            Ok(()) => {
                *loaded_asset = Some(asset_id.clone());
                (
                    WorkerEvent::AssetLoaded {
                        asset_id: asset_id.clone(),
                    },
                    false,
                )
            }
            Err(error) => (
                rejection(
                    command,
                    "asset_load_failed",
                    &format!("The asset could not be loaded: {error:?}"),
                ),
                false,
            ),
        },
        ChannelCommand::TakeAsset { asset_id } => {
            if loaded_asset.as_deref() != Some(asset_id.as_str()) {
                (
                    rejection(
                        command,
                        "asset_not_loaded",
                        "The requested asset is not loaded in this channel worker.",
                    ),
                    false,
                )
            } else {
                match media.select(SourceRole::Program) {
                    Ok(()) => (
                        WorkerEvent::OnAirChanged {
                            source_kind: "asset".into(),
                            source_id: asset_id.clone(),
                        },
                        false,
                    ),
                    Err(error) => (
                        rejection(
                            command,
                            "take_failed",
                            &format!("The asset could not be taken on air: {error:?}"),
                        ),
                        false,
                    ),
                }
            }
        }
        ChannelCommand::ReturnToSchedule => match media.select(SourceRole::Fallback) {
            Ok(()) => (
                WorkerEvent::OnAirChanged {
                    source_kind: "fallback".into(),
                    source_id: "station-fallback".into(),
                },
                false,
            ),
            Err(error) => (
                rejection(
                    command,
                    "return_failed",
                    &format!("The worker could not return to fallback: {error:?}"),
                ),
                false,
            ),
        },
        ChannelCommand::Shutdown => (WorkerEvent::ShutdownComplete, true),
        ChannelCommand::ArmLive { .. } | ChannelCommand::TakeLive { .. } => (
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
