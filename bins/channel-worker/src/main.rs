use std::collections::HashMap;
use std::io::{ErrorKind, Read, Write};
use std::net::{IpAddr, SocketAddr};
use std::path::PathBuf;
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use station_media_engine::{
    ConfiguredTransportOutput, MpegTsMuxContract, OutputCaptureRequest, OutputCaptureStatus,
    PersistentPlayout, SourceRole,
};
use station_media_journal::{FallbackToneJournalEntry, JournalWriter};
use station_media_protocol::{
    ChannelCommand, CommandEnvelope, FallbackToneDeactivationReason, LiveArmToken,
    LiveReturnTarget, MAX_FRAME_BYTES, OutputTransportObservation, PROTOCOL_VERSION,
    WorkerBootstrapEnvelope, WorkerEvent, WorkerEventEnvelope, WorkerFallbackTone,
    WorkerOutputConfiguration, decode_bootstrap_frame, decode_command_frame, encode_event_frame,
};
use station_schedule::OutputProfileId;
use station_windows_ipc::PipeStream;

struct WorkerArguments {
    worker_id: String,
    channel_id: String,
    journal_path: PathBuf,
    pipe_name: String,
}

struct LiveState {
    session_id: String,
    source_id: String,
    source_revision: u64,
    arm_token: Option<LiveArmToken>,
    phase: LivePhase,
}

enum LivePhase {
    Armed,
    OnAir { health: LiveBufferHealth },
    Returned,
}

struct LiveBufferHealth {
    last_buffer_counts: Option<(u64, u64)>,
    stalled_since_milliseconds: Option<u64>,
    consecutive_stalls: u8,
}

enum LiveBufferStatus {
    Baseline,
    Advanced,
    PendingStall,
    Stalled,
}

const MAX_CONSECUTIVE_LIVE_STALLS: u8 = 2;
const MAX_LIVE_STALL_MILLISECONDS: u64 = 2_000;

struct ActiveFallbackTone {
    tone: WorkerFallbackTone,
    expires_at_monotonic_ms: u64,
}

enum WorkerInput {
    Command(Vec<u8>),
    ToneExpiry {
        activation_id: String,
        authority_revision: u64,
    },
    ControlClosed,
    ControlFailed(String),
}

impl LiveState {
    fn is_on_air(&self) -> bool {
        matches!(&self.phase, LivePhase::OnAir { .. })
    }

    fn accepts_take(&self, session_id: &str, arm_token: &LiveArmToken) -> bool {
        self.session_id.as_str() == session_id
            && matches!(&self.phase, LivePhase::Armed)
            && self
                .arm_token
                .as_ref()
                .is_some_and(|expected| expected == arm_token)
    }

    fn mark_on_air(&mut self) {
        self.arm_token = None;
        self.phase = LivePhase::OnAir {
            health: LiveBufferHealth {
                last_buffer_counts: None,
                stalled_since_milliseconds: None,
                consecutive_stalls: 0,
            },
        };
    }

    fn mark_returned(&mut self) {
        self.phase = LivePhase::Returned;
    }
}

fn live_buffers_advanced(previous: (u64, u64), current: (u64, u64)) -> bool {
    current.0 > previous.0 && current.1 > previous.1
}

impl LiveBufferHealth {
    fn observe(&mut self, current: (u64, u64), monotonic_milliseconds: u64) -> LiveBufferStatus {
        let Some(previous) = self.last_buffer_counts else {
            self.last_buffer_counts = Some(current);
            return LiveBufferStatus::Baseline;
        };
        if live_buffers_advanced(previous, current) {
            self.last_buffer_counts = Some(current);
            self.stalled_since_milliseconds = None;
            self.consecutive_stalls = 0;
            return LiveBufferStatus::Advanced;
        }
        self.consecutive_stalls = self.consecutive_stalls.saturating_add(1);
        let stalled_since = *self
            .stalled_since_milliseconds
            .get_or_insert(monotonic_milliseconds);
        if self.consecutive_stalls >= MAX_CONSECUTIVE_LIVE_STALLS
            || monotonic_milliseconds.saturating_sub(stalled_since) >= MAX_LIVE_STALL_MILLISECONDS
        {
            LiveBufferStatus::Stalled
        } else {
            LiveBufferStatus::PendingStall
        }
    }
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
    let mut journal = JournalWriter::open(&journal_path, &worker_id, &channel_id)
        .unwrap_or_else(|error| fatal(3, "open its journal", &format!("{error:?}")));
    let started = Instant::now();
    let worker_boot_id = generate_worker_boot_id()
        .unwrap_or_else(|error| fatal(10, "generate its worker boot identity", &error));
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
    std::fs::create_dir_all(&bootstrap.cg_image_root).unwrap_or_else(|error| {
        fatal(
            6,
            "create its managed CG image root",
            &format!("{}: {error}", bootstrap.cg_image_root),
        )
    });
    let cg_image_root = std::fs::canonicalize(&bootstrap.cg_image_root).unwrap_or_else(|error| {
        fatal(
            6,
            "validate its managed CG image root",
            &format!("{}: {error}", bootstrap.cg_image_root),
        )
    });
    if !cg_image_root.is_dir() {
        fatal(
            6,
            "validate its managed CG image root",
            "the managed CG image root is not a directory",
        );
    }
    std::fs::create_dir_all(&bootstrap.proof_artifact_root).unwrap_or_else(|error| {
        fatal(
            6,
            "create its managed output proof root",
            &format!("{}: {error}", bootstrap.proof_artifact_root),
        )
    });
    let proof_artifact_root =
        std::fs::canonicalize(&bootstrap.proof_artifact_root).unwrap_or_else(|error| {
            fatal(
                6,
                "validate its managed output proof root",
                &format!("{}: {error}", bootstrap.proof_artifact_root),
            )
        });
    if !proof_artifact_root.is_dir() {
        fatal(
            6,
            "validate its managed output proof root",
            "the managed output proof root is not a directory",
        );
    }
    let journal_directory = journal_path.parent().unwrap_or_else(|| {
        fatal(
            6,
            "validate its managed data root",
            "the journal path has no managed parent directory",
        )
    });
    let managed_data_root = if journal_directory.file_name().and_then(|name| name.to_str())
        == Some("channel-journals")
    {
        journal_directory.parent().unwrap_or(journal_directory)
    } else {
        journal_directory
    };
    let managed_data_root = std::fs::canonicalize(managed_data_root)
        .ok()
        .unwrap_or_else(|| {
            fatal(
                6,
                "validate its managed data root",
                "the journal path does not identify a readable managed data root",
            )
        });
    if proof_artifact_root.parent() != Some(managed_data_root.as_path()) {
        fatal(
            6,
            "validate its managed output proof root",
            "the output proof root escapes the journal-owned managed data root",
        );
    }
    remove_stale_proof_parts(&proof_artifact_root)
        .unwrap_or_else(|error| fatal(6, "clean stale output proof parts", &error));
    let output_profile = OutputProfileId::from_storage_name(&bootstrap.output_profile)
        .unwrap_or_else(|error| fatal(6, "validate its output profile", &format!("{error:?}")));
    if !output_profile.is_runnable() {
        fatal(
            6,
            "validate its output profile",
            "the staged output profile is not runnable by this worker",
        );
    }
    let outputs = bootstrap
        .outputs
        .iter()
        .cloned()
        .map(|output| match output {
            WorkerOutputConfiguration::UdpTs {
                sink_id,
                destination,
                loudness_target_lufs,
                headend,
            } => destination
                .parse::<SocketAddr>()
                .map(|destination| {
                    if let Some(headend) = headend {
                        ConfiguredTransportOutput::udp_identified_with_mux_contract(
                            sink_id,
                            destination,
                            loudness_target_lufs,
                            MpegTsMuxContract::new(
                                u64::from(headend.muxrate_kbps) * 1_000,
                                headend.packet_size_bytes,
                            ),
                        )
                    } else {
                        ConfiguredTransportOutput::udp_identified(
                            sink_id,
                            destination,
                            loudness_target_lufs,
                        )
                    }
                })
                .map_err(|error| error.to_string()),
            WorkerOutputConfiguration::Srt {
                sink_id,
                uri,
                passphrase,
                loudness_target_lufs,
            } => validate_srt_uri(&uri).map(|()| {
                ConfiguredTransportOutput::srt_identified(
                    sink_id,
                    uri,
                    passphrase.map(|value| value.expose().to_string()),
                    loudness_target_lufs,
                )
            }),
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
        PersistentPlayout::start_configured_outputs_with_profile_roots_and_authorities(
            &outputs,
            output_profile,
            Some(&cg_image_root),
            Some(&proof_artifact_root),
            bootstrap.fallback_slate.as_ref(),
            bootstrap.caption_policy.as_ref(),
        )
        .unwrap_or_else(|error| fatal(8, "start its fallback graph", &format!("{error:?}"))),
    );

    record_and_emit(
        &mut journal,
        &mut pipe,
        &worker_id,
        &channel_id,
        WorkerEvent::Ready { graph_revision: 1 },
    )
    .unwrap_or_else(|error| fatal(4, "record and emit readiness", &error));
    let mut loaded_asset = None;
    let mut live_state = None;
    let worker_instance_id = format!("{worker_id}-{}", std::process::id());
    let mut graph_revision = 1_u64;
    let mut proof_graph_revisions = HashMap::new();
    let mut fallback_tone: Option<ActiveFallbackTone> = None;
    let (input_sender, input_receiver) = mpsc::channel();
    let mut command_pipe = pipe
        .try_clone()
        .unwrap_or_else(|error| fatal(5, "duplicate its control pipe", &format!("{error:?}")));
    let reader_sender = input_sender.clone();
    thread::spawn(move || {
        loop {
            match read_command_frame(&mut command_pipe) {
                Ok(Some(frame)) => {
                    if reader_sender.send(WorkerInput::Command(frame)).is_err() {
                        break;
                    }
                }
                Ok(None) => {
                    let _ = reader_sender.send(WorkerInput::ControlClosed);
                    break;
                }
                Err(error) => {
                    let _ = reader_sender.send(WorkerInput::ControlFailed(error));
                    break;
                }
            }
        }
    });

    loop {
        let input = match input_receiver.recv() {
            Ok(input) => input,
            Err(_) => break,
        };
        match input {
            WorkerInput::ToneExpiry {
                activation_id,
                authority_revision,
            } => {
                let Some(active) = fallback_tone.as_ref() else {
                    continue;
                };
                if active.tone.activation_id != activation_id
                    || active.tone.authority_revision != authority_revision
                {
                    continue;
                }
                let now = started.elapsed().as_millis() as u64;
                media
                    .as_mut()
                    .expect("a running worker owns its media graph")
                    .deactivate_fallback_tone();
                append_tone_deactivation_audit(
                    &mut journal,
                    &worker_id,
                    &channel_id,
                    &active.tone,
                    FallbackToneDeactivationReason::Expired,
                    now,
                )
                .unwrap_or_else(|error| fatal(7, "record fallback tone expiry", &error));
                fallback_tone = None;
                continue;
            }
            WorkerInput::ControlClosed => break,
            WorkerInput::ControlFailed(error) => fatal(5, "read a command frame", &error),
            WorkerInput::Command(frame) => {
                // The expiry sender is autonomous, but a command that happens to win a
                // scheduling race must never observe an already-expired activation as live.
                // This is a second, fail-closed deadline check; it does not poll.
                let now = started.elapsed().as_millis() as u64;
                if let Some(tone) = fallback_tone.as_ref().and_then(|active| {
                    (now >= active.expires_at_monotonic_ms).then(|| active.tone.clone())
                }) {
                    media
                        .as_mut()
                        .expect("a running worker owns its media graph")
                        .deactivate_fallback_tone();
                    append_tone_deactivation_audit(
                        &mut journal,
                        &worker_id,
                        &channel_id,
                        &tone,
                        FallbackToneDeactivationReason::Expired,
                        now,
                    )
                    .unwrap_or_else(|error| fatal(7, "record fallback tone expiry", &error));
                    fallback_tone = None;
                }
                let command = decode_command_frame(&frame).unwrap_or_else(|error| {
                    fatal(6, "decode a command frame", &format!("{error:?}"))
                });
                let prior_tone = fallback_tone.as_ref().map(|active| active.tone.clone());
                let (event, should_stop) = decide_event(
                    &command,
                    &channel_id,
                    journal.next_sequence(),
                    started.elapsed().as_millis() as u64,
                    media
                        .as_mut()
                        .expect("a running worker owns its media graph"),
                    &mut loaded_asset,
                    &mut live_state,
                    &bootstrap,
                    &worker_instance_id,
                    &worker_boot_id,
                    &mut graph_revision,
                    &mut proof_graph_revisions,
                    &mut fallback_tone,
                    &input_sender,
                );
                let now = started.elapsed().as_millis() as u64;
                if let WorkerEvent::FallbackToneActivated {
                    tone: Some(tone),
                    activated_at_monotonic_ms: Some(activated_at_monotonic_ms),
                    ..
                } = &event
                {
                    append_tone_activation_audit(
                        &mut journal,
                        &worker_id,
                        &channel_id,
                        tone,
                        *activated_at_monotonic_ms,
                    )
                    .unwrap_or_else(|error| fatal(7, "record fallback tone activation", &error));
                }
                if let Some(tone) = prior_tone {
                    let reason = match &event {
                        WorkerEvent::FallbackToneDeactivated { .. } => {
                            Some(FallbackToneDeactivationReason::Explicit)
                        }
                        _ if media
                            .as_ref()
                            .expect("a running worker owns its media graph")
                            .active_source()
                            .ok()
                            .is_some_and(|source| source != SourceRole::Fallback) =>
                        {
                            Some(FallbackToneDeactivationReason::SourceTransition)
                        }
                        _ => None,
                    };
                    if let Some(reason) = reason {
                        append_tone_deactivation_audit(
                            &mut journal,
                            &worker_id,
                            &channel_id,
                            &tone,
                            reason,
                            now,
                        )
                        .unwrap_or_else(|error| {
                            fatal(7, "record fallback tone deactivation", &error)
                        });
                        fallback_tone = None;
                    }
                }
                if should_stop {
                    media
                        .take()
                        .expect("a running worker owns its media graph")
                        .stop()
                        .unwrap_or_else(|error| {
                            fatal(9, "stop its media graph", &format!("{error:?}"))
                        });
                }
                record_and_emit(&mut journal, &mut pipe, &worker_id, &channel_id, event)
                    .unwrap_or_else(|error| fatal(7, "record and emit an event", &error));
                if should_stop {
                    break;
                }
            }
        }
    }

    if let Some(media) = media.take() {
        media.stop().unwrap_or_else(|error| {
            fatal(
                9,
                "stop its media graph after the control pipe closed",
                &format!("{error:?}"),
            )
        });
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

fn remove_stale_proof_parts(root: &std::path::Path) -> Result<(), String> {
    for entry in std::fs::read_dir(root).map_err(|error| error.to_string())? {
        let entry = entry.map_err(|error| error.to_string())?;
        let file_type = entry.file_type().map_err(|error| error.to_string())?;
        if !file_type.is_file() {
            continue;
        }
        let name = entry.file_name();
        let Some(name) = name.to_str() else {
            continue;
        };
        let Some(proof_id) = name.strip_suffix(".ts.part") else {
            continue;
        };
        if is_canonical_uuid(proof_id) {
            std::fs::remove_file(entry.path()).map_err(|error| {
                format!("could not remove stale managed proof part {name}: {error}")
            })?;
        }
    }
    Ok(())
}

fn is_canonical_uuid(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 36
        && [8, 13, 18, 23].iter().all(|&index| bytes[index] == b'-')
        && bytes
            .iter()
            .enumerate()
            .all(|(index, byte)| matches!(index, 8 | 13 | 18 | 23) || byte.is_ascii_hexdigit())
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

fn activate_fallback_tone(
    command: &CommandEnvelope,
    media: &mut PersistentPlayout,
    active: &mut Option<ActiveFallbackTone>,
    tone: &WorkerFallbackTone,
    monotonic_milliseconds: u64,
    input_sender: &mpsc::Sender<WorkerInput>,
) -> (WorkerEvent, bool) {
    if media.active_source().ok() != Some(SourceRole::Fallback) {
        return (
            rejection(
                command,
                "fallback_not_selected",
                "Fallback tones may be activated only while the channel is on fallback.",
            ),
            false,
        );
    }
    if active.is_some() {
        return (
            rejection(
                command,
                "fallback_tone_active",
                "A fallback tone is already active; explicitly deactivate it before another activation.",
            ),
            false,
        );
    }
    match media.activate_fallback_tone(tone) {
        Ok(()) => {
            let Some(expires_at_monotonic_ms) =
                monotonic_milliseconds.checked_add(tone.duration_ms)
            else {
                media.deactivate_fallback_tone();
                return (
                    rejection(
                        command,
                        "fallback_tone_expiry_invalid",
                        "The fallback tone expiry overflowed.",
                    ),
                    false,
                );
            };
            *active = Some(ActiveFallbackTone {
                tone: tone.clone(),
                expires_at_monotonic_ms,
            });
            let expiry_sender = input_sender.clone();
            let activation_id = tone.activation_id.clone();
            let authority_revision = tone.authority_revision;
            let duration = Duration::from_millis(tone.duration_ms);
            thread::spawn(move || {
                thread::sleep(duration);
                let _ = expiry_sender.send(WorkerInput::ToneExpiry {
                    activation_id,
                    authority_revision,
                });
            });
            (
                WorkerEvent::FallbackToneActivated {
                    activation_id: tone.activation_id.clone(),
                    authority_revision: tone.authority_revision,
                    duration_ms: tone.duration_ms,
                    tone: Some(tone.clone()),
                    activated_at_monotonic_ms: Some(monotonic_milliseconds),
                },
                false,
            )
        }
        Err(error) => (
            rejection(
                command,
                "fallback_tone_activation_failed",
                &format!("The fallback tone could not be activated: {error:?}"),
            ),
            false,
        ),
    }
}

fn deactivate_fallback_tone(
    command: &CommandEnvelope,
    media: &mut PersistentPlayout,
    active: &mut Option<ActiveFallbackTone>,
    activation_id: &str,
    authority_revision: u64,
    monotonic_milliseconds: u64,
) -> (WorkerEvent, bool) {
    let Some(current) = active.as_ref() else {
        return (
            rejection(
                command,
                "fallback_tone_inactive",
                "No fallback tone is active.",
            ),
            false,
        );
    };
    if current.tone.activation_id != activation_id
        || current.tone.authority_revision != authority_revision
    {
        return (
            rejection(
                command,
                "fallback_tone_authority_mismatch",
                "The fallback tone activation identity or authority revision is stale.",
            ),
            false,
        );
    }
    let tone = current.tone.clone();
    media.deactivate_fallback_tone();
    *active = None;
    (
        WorkerEvent::FallbackToneDeactivated {
            activation_id: activation_id.into(),
            authority_revision,
            deactivation: Some(station_media_protocol::WorkerFallbackToneDeactivation {
                tone,
                reason: FallbackToneDeactivationReason::Explicit,
                deactivated_at_monotonic_ms: monotonic_milliseconds,
            }),
        },
        false,
    )
}

fn append_tone_activation_audit(
    journal: &mut JournalWriter,
    worker_id: &str,
    channel_id: &str,
    tone: &WorkerFallbackTone,
    activated_at_monotonic_ms: u64,
) -> Result<(), String> {
    journal
        .append_fallback_tone_audit(&FallbackToneJournalEntry::Activated {
            worker_id: worker_id.into(),
            channel_id: channel_id.into(),
            tone: tone.clone(),
            activated_at_monotonic_ms,
        })
        .map_err(|error| format!("{error:?}"))
}

fn append_tone_deactivation_audit(
    journal: &mut JournalWriter,
    worker_id: &str,
    channel_id: &str,
    tone: &WorkerFallbackTone,
    reason: FallbackToneDeactivationReason,
    deactivated_at_monotonic_ms: u64,
) -> Result<(), String> {
    journal
        .append_fallback_tone_audit(&FallbackToneJournalEntry::Deactivated {
            worker_id: worker_id.into(),
            channel_id: channel_id.into(),
            tone: tone.clone(),
            reason,
            deactivated_at_monotonic_ms,
        })
        .map_err(|error| format!("{error:?}"))
}

#[allow(clippy::too_many_arguments)]
fn decide_event(
    command: &CommandEnvelope,
    channel_id: &str,
    next_sequence: u64,
    monotonic_milliseconds: u64,
    media: &mut PersistentPlayout,
    loaded_asset: &mut Option<String>,
    live_state: &mut Option<LiveState>,
    bootstrap: &WorkerBootstrapEnvelope,
    worker_instance_id: &str,
    worker_boot_id: &str,
    graph_revision: &mut u64,
    proof_graph_revisions: &mut HashMap<String, u64>,
    fallback_tone: &mut Option<ActiveFallbackTone>,
    input_sender: &mpsc::Sender<WorkerInput>,
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
        ChannelCommand::Ping => {
            if let Some(LiveState {
                phase: LivePhase::OnAir { health, .. },
                ..
            }) = live_state.as_mut()
            {
                let Some(current) = media.live_buffer_counts() else {
                    return (
                        rejection(
                            command,
                            "live_input_stalled",
                            "Live input health could not be verified.",
                        ),
                        false,
                    );
                };
                match health.observe(current, monotonic_milliseconds) {
                    LiveBufferStatus::Baseline | LiveBufferStatus::Advanced => {}
                    LiveBufferStatus::PendingStall => {
                        return (
                            rejection(
                                command,
                                "live_input_stalling",
                                "Live input audio or video has not advanced over this heartbeat interval.",
                            ),
                            false,
                        );
                    }
                    LiveBufferStatus::Stalled => {
                        return (
                            rejection(
                                command,
                                "live_input_stalled",
                                "Live input audio or video has stopped advancing.",
                            ),
                            false,
                        );
                    }
                }
            }
            match media.output_health_observations().and_then(|observations| {
                observations
                    .into_iter()
                    .enumerate()
                    .map(|(output_index, observation)| {
                        u16::try_from(output_index)
                            .map(|output_index| OutputTransportObservation {
                                output_index,
                                bytes_sent_total: observation.bytes_sent_total,
                                audio_buffers_total: observation.audio_buffers_total,
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
            }
        }
        ChannelCommand::ApplyPlan { revision, .. } => {
            *graph_revision = (*revision).max(1);
            (
                WorkerEvent::Ready {
                    graph_revision: *graph_revision,
                },
                false,
            )
        }
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
        ChannelCommand::PushCaptionCue { cue } => match media.push_caption_cue(cue) {
            Ok(()) => (
                WorkerEvent::CaptionCueAccepted {
                    cue_id: cue.cue_id.clone(),
                },
                false,
            ),
            Err(error) => (
                rejection(
                    command,
                    "caption_cue_rejected",
                    &format!("The CEA-708 cue could not be scheduled: {error:?}"),
                ),
                false,
            ),
        },
        ChannelCommand::ClearCaptionCues => match media.clear_caption_cues() {
            Ok(()) => (WorkerEvent::CaptionCuesCleared, false),
            Err(error) => (
                rejection(
                    command,
                    "caption_clear_failed",
                    &format!("The CEA-708 caption state could not be cleared: {error:?}"),
                ),
                false,
            ),
        },
        ChannelCommand::ActivateFallbackTone { tone } => activate_fallback_tone(
            command,
            media,
            fallback_tone,
            tone,
            monotonic_milliseconds,
            input_sender,
        ),
        ChannelCommand::DeactivateFallbackTone {
            activation_id,
            authority_revision,
        } => deactivate_fallback_tone(
            command,
            media,
            fallback_tone,
            activation_id,
            *authority_revision,
            monotonic_milliseconds,
        ),
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
        ChannelCommand::TakeAsset {
            asset_id,
            media_offset_ms,
        } => {
            if live_state.as_ref().is_some_and(LiveState::is_on_air) {
                (
                    rejection(
                        command,
                        "live_active",
                        "The active live session must be returned before taking an asset.",
                    ),
                    false,
                )
            } else if loaded_asset.as_deref() != Some(asset_id.as_str()) {
                (
                    rejection(
                        command,
                        "asset_not_loaded",
                        "The requested asset is not loaded in this channel worker.",
                    ),
                    false,
                )
            } else {
                match media.take_program_at(*media_offset_ms) {
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
        ChannelCommand::TakeScheduledAsset {
            report_id,
            schedule_item_id,
            schedule_item_revision,
            asset_id,
            media_offset_ms,
        } => {
            if live_state.as_ref().is_some_and(LiveState::is_on_air) {
                (
                    rejection(
                        command,
                        "live_active",
                        "The active live session must be returned before taking an asset.",
                    ),
                    false,
                )
            } else if loaded_asset.as_deref() != Some(asset_id.as_str()) {
                (
                    rejection(
                        command,
                        "asset_not_loaded",
                        "The requested asset is not loaded in this channel worker.",
                    ),
                    false,
                )
            } else {
                match media.take_program_at(*media_offset_ms) {
                    Ok(()) => {
                        let actual_on_air_at_unix_ms =
                            actual_on_air_at_unix_ms().unwrap_or_else(|error| {
                                fatal(11, "capture the actual on-air wall time", &error)
                            });
                        (
                            WorkerEvent::ScheduledAssetOnAir {
                                report_id: report_id.clone(),
                                schedule_item_id: schedule_item_id.clone(),
                                schedule_item_revision: *schedule_item_revision,
                                asset_id: asset_id.clone(),
                                media_offset_ms: *media_offset_ms,
                                worker_boot_id: worker_boot_id.to_string(),
                                actual_on_air_at_unix_ms,
                            },
                            false,
                        )
                    }
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
        ChannelCommand::ReturnToSchedule => {
            if live_state.as_ref().is_some_and(LiveState::is_on_air) {
                (
                    rejection(
                        command,
                        "live_active",
                        "The active live session must be returned before returning to schedule.",
                    ),
                    false,
                )
            } else {
                match media.select(SourceRole::Fallback) {
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
                }
            }
        }
        ChannelCommand::StartOutputProof {
            proof_id,
            output_index,
            sink_id,
            loudness_target_lufs,
            duration_ms,
            max_bytes,
        } => {
            if let Err(message) = bootstrap.validate_output_proof_binding(
                *output_index,
                sink_id,
                *loudness_target_lufs,
            ) {
                return (
                    rejection(command, "output_proof_binding_invalid", &message),
                    false,
                );
            }
            match media.start_output_capture(OutputCaptureRequest {
                proof_id: proof_id.clone(),
                output_index: *output_index,
                sink_id: sink_id.clone(),
                loudness_target: *loudness_target_lufs,
                duration_ms: *duration_ms,
                max_bytes: *max_bytes,
            }) {
                Ok(started) => {
                    proof_graph_revisions.insert(proof_id.clone(), *graph_revision);
                    (
                        WorkerEvent::OutputProofStarted {
                            proof_id: proof_id.clone(),
                            output_index: *output_index,
                            sink_id: sink_id.clone(),
                            loudness_target_lufs: *loudness_target_lufs,
                            duration_ms: *duration_ms,
                            max_bytes: *max_bytes,
                            artifact_relative_path: started.artifact_relative_path,
                            worker_instance_id: worker_instance_id.into(),
                            graph_revision: *graph_revision,
                        },
                        false,
                    )
                }
                Err(error) => (
                    rejection(
                        command,
                        "output_proof_start_failed",
                        &format!("The output proof could not start: {error:?}"),
                    ),
                    false,
                ),
            }
        }
        ChannelCommand::PollOutputProof { proof_id } => {
            let capture_graph_revision = proof_graph_revisions
                .get(proof_id)
                .copied()
                .unwrap_or(*graph_revision);
            if capture_graph_revision != *graph_revision {
                let artifact_relative_path = media
                    .poll_output_capture(proof_id)
                    .ok()
                    .map(|status| match status {
                        OutputCaptureStatus::Pending {
                            artifact_relative_path,
                            ..
                        }
                        | OutputCaptureStatus::Failed {
                            artifact_relative_path,
                            ..
                        } => artifact_relative_path,
                        OutputCaptureStatus::Ready(ready) => ready.artifact_relative_path,
                    })
                    .unwrap_or_else(|| format!("{proof_id}.ts"));
                let _ = media.abort_output_capture(proof_id);
                proof_graph_revisions.remove(proof_id);
                return (
                    WorkerEvent::OutputProofFailed {
                        proof_id: proof_id.clone(),
                        artifact_relative_path,
                        worker_instance_id: worker_instance_id.into(),
                        graph_revision: capture_graph_revision,
                        code: "graph_revision_changed".into(),
                        message: "output graph revision changed during capture".into(),
                    },
                    false,
                );
            }
            match media.poll_output_capture(proof_id) {
                Ok(OutputCaptureStatus::Pending {
                    artifact_relative_path,
                    ..
                }) => (
                    WorkerEvent::OutputProofPending {
                        proof_id: proof_id.clone(),
                        artifact_relative_path,
                        worker_instance_id: worker_instance_id.into(),
                        graph_revision: capture_graph_revision,
                    },
                    false,
                ),
                Ok(OutputCaptureStatus::Ready(ready)) => {
                    proof_graph_revisions.remove(proof_id);
                    (
                        WorkerEvent::OutputProofReady {
                            proof_id: ready.proof_id,
                            output_index: ready.output_index,
                            sink_id: ready.sink_id,
                            loudness_target_lufs: ready.loudness_target,
                            artifact_relative_path: ready.artifact_relative_path,
                            worker_instance_id: worker_instance_id.into(),
                            graph_revision: capture_graph_revision,
                            size_bytes: ready.capture_bytes,
                            capture_sha256: ready.capture_sha256,
                            duration_ms: ready.capture_duration_ms,
                        },
                        false,
                    )
                }
                Ok(OutputCaptureStatus::Failed {
                    artifact_relative_path,
                    code,
                    message,
                    ..
                }) => {
                    proof_graph_revisions.remove(proof_id);
                    (
                        WorkerEvent::OutputProofFailed {
                            proof_id: proof_id.clone(),
                            artifact_relative_path,
                            worker_instance_id: worker_instance_id.into(),
                            graph_revision: capture_graph_revision,
                            code,
                            message,
                        },
                        false,
                    )
                }
                Err(error) => (
                    rejection(
                        command,
                        "output_proof_poll_failed",
                        &format!("The output proof could not be polled: {error:?}"),
                    ),
                    false,
                ),
            }
        }
        ChannelCommand::AbortOutputProof { proof_id } => {
            let capture_graph_revision = proof_graph_revisions
                .get(proof_id)
                .copied()
                .unwrap_or(*graph_revision);
            let artifact_relative_path = match media.poll_output_capture(proof_id) {
                Ok(OutputCaptureStatus::Pending {
                    artifact_relative_path,
                    ..
                }) => artifact_relative_path,
                Ok(OutputCaptureStatus::Failed { .. }) | Ok(OutputCaptureStatus::Ready(_)) => {
                    return (
                        rejection(
                            command,
                            "output_proof_already_terminal",
                            "The output proof reached a terminal state before it could be aborted.",
                        ),
                        false,
                    );
                }
                Err(error) => {
                    return (
                        rejection(
                            command,
                            "output_proof_abort_failed",
                            &format!("The output proof could not be located for abort: {error:?}"),
                        ),
                        false,
                    );
                }
            };
            match media.abort_output_capture(proof_id) {
                Ok(()) => {
                    proof_graph_revisions.remove(proof_id);
                    (
                        WorkerEvent::OutputProofFailed {
                            proof_id: proof_id.clone(),
                            artifact_relative_path,
                            worker_instance_id: worker_instance_id.into(),
                            graph_revision: capture_graph_revision,
                            code: "capture_aborted".into(),
                            message: "output capture was aborted".into(),
                        },
                        false,
                    )
                }
                Err(error) => (
                    rejection(
                        command,
                        "output_proof_abort_failed",
                        &format!("The output proof could not be aborted: {error:?}"),
                    ),
                    false,
                ),
            }
        }
        ChannelCommand::Shutdown => (WorkerEvent::ShutdownComplete, true),
        ChannelCommand::ArmLive { input, arm_token } => {
            if live_state.is_some() {
                return (
                    rejection(
                        command,
                        "live_already_armed",
                        "A live input is already armed.",
                    ),
                    false,
                );
            }
            let source = match input.bind_address.parse::<IpAddr>() {
                Ok(address) => SocketAddr::new(address, input.port.get()),
                Err(_) => {
                    return (
                        rejection(
                            command,
                            "live_input_invalid",
                            "The live input address is invalid.",
                        ),
                        false,
                    );
                }
            };
            match media.arm_live_udp(source) {
                Ok(()) => {
                    *live_state = Some(LiveState {
                        session_id: input.session_id.clone(),
                        source_id: input.source_id.clone(),
                        source_revision: input.source_revision,
                        arm_token: Some(arm_token.clone()),
                        phase: LivePhase::Armed,
                    });
                    (
                        WorkerEvent::LiveArmed {
                            session_id: input.session_id.clone(),
                            source_id: input.source_id.clone(),
                            source_revision: input.source_revision,
                        },
                        false,
                    )
                }
                Err(error) => (
                    rejection(
                        command,
                        "live_arm_failed",
                        &format!("The live input could not be armed: {error:?}"),
                    ),
                    false,
                ),
            }
        }
        ChannelCommand::TakeLive {
            session_id,
            arm_token,
        } => {
            let Some(state) = live_state.as_ref() else {
                return (
                    rejection(command, "live_not_armed", "No live input is armed."),
                    false,
                );
            };
            if state.session_id.as_str() != session_id.as_str() {
                return (
                    rejection(
                        command,
                        "live_session_mismatch",
                        "The live session does not match the armed input.",
                    ),
                    false,
                );
            }
            if state.is_on_air() {
                return (
                    rejection(
                        command,
                        "live_already_on_air",
                        "The live session is already on air.",
                    ),
                    false,
                );
            }
            if !state.accepts_take(session_id, arm_token) {
                return (
                    rejection(
                        command,
                        "live_token_invalid",
                        "The live arm token was not accepted.",
                    ),
                    false,
                );
            }
            let prior_source = match media.active_source() {
                Ok(role) => role,
                Err(error) => {
                    return (
                        rejection(
                            command,
                            "live_take_failed",
                            &format!("The live input could not be taken on air: {error:?}"),
                        ),
                        false,
                    );
                }
            };
            match media.select(SourceRole::Live) {
                Ok(()) => {
                    if media.live_buffer_counts().is_none() {
                        return match media.select(prior_source) {
                            Ok(()) => (
                                rejection(
                                    command,
                                    "live_take_failed",
                                    "The live input lost decoded audio or video; the prior source was restored.",
                                ),
                                false,
                            ),
                            Err(error) => {
                                live_state
                                    .as_mut()
                                    .expect("the armed live state remains present")
                                    .mark_on_air();
                                (
                                    rejection(
                                        command,
                                        "live_take_restore_failed",
                                        &format!(
                                            "The live input lost decoded audio or video and the prior source could not be restored: {error:?}"
                                        ),
                                    ),
                                    false,
                                )
                            }
                        };
                    }
                    let state = live_state
                        .as_mut()
                        .expect("the armed live state remains present");
                    state.mark_on_air();
                    (
                        WorkerEvent::LiveOnAir {
                            session_id: state.session_id.clone(),
                            source_id: state.source_id.clone(),
                            source_revision: state.source_revision,
                        },
                        false,
                    )
                }
                Err(error) => (
                    rejection(
                        command,
                        "live_take_failed",
                        &format!("The live input could not be taken on air: {error:?}"),
                    ),
                    false,
                ),
            }
        }
        ChannelCommand::ReturnFromLive { session_id, target } => {
            let Some(state) = live_state.as_ref() else {
                return (
                    rejection(command, "live_not_armed", "No live input is armed."),
                    false,
                );
            };
            if state.session_id.as_str() != session_id.as_str() {
                return (
                    rejection(
                        command,
                        "live_session_mismatch",
                        "The live session does not match the armed input.",
                    ),
                    false,
                );
            }
            let LivePhase::OnAir { .. } = &state.phase else {
                return (
                    rejection(
                        command,
                        "live_not_on_air",
                        "The live session is not on air.",
                    ),
                    false,
                );
            };
            let (return_result, source_kind, source_id) = match target {
                LiveReturnTarget::Fallback => (
                    media.select(SourceRole::Fallback),
                    "fallback",
                    "station-fallback".to_string(),
                ),
                LiveReturnTarget::Program {
                    asset_id,
                    media_offset_ms,
                } if loaded_asset.as_deref() == Some(asset_id.as_str()) => (
                    media.take_program_at(*media_offset_ms),
                    "asset",
                    asset_id.clone(),
                ),
                LiveReturnTarget::Program { .. } => {
                    return (
                        rejection(
                            command,
                            "asset_not_loaded",
                            "The scheduled handback asset is not loaded in this channel worker.",
                        ),
                        false,
                    );
                }
            };
            match return_result {
                Ok(()) => {
                    live_state
                        .as_mut()
                        .expect("the live state remains present")
                        .mark_returned();
                    (
                        WorkerEvent::LiveReturned {
                            session_id: session_id.clone(),
                            source_kind: source_kind.into(),
                            source_id,
                        },
                        false,
                    )
                }
                Err(error) => (
                    rejection(
                        command,
                        "live_return_failed",
                        &format!("The worker could not return from live: {error:?}"),
                    ),
                    false,
                ),
            }
        }
        ChannelCommand::DisarmLive { session_id } => {
            let Some(state) = live_state.as_ref() else {
                return (
                    rejection(command, "live_not_armed", "No live input is armed."),
                    false,
                );
            };
            if state.session_id.as_str() != session_id.as_str() {
                return (
                    rejection(
                        command,
                        "live_session_mismatch",
                        "The live session does not match the armed input.",
                    ),
                    false,
                );
            }
            if state.is_on_air() {
                return (
                    rejection(
                        command,
                        "live_active",
                        "The live session must be returned before disarming.",
                    ),
                    false,
                );
            }
            match media.disarm_live() {
                Ok(()) => {
                    let session_id = live_state
                        .take()
                        .expect("the armed live state remains present")
                        .session_id;
                    (WorkerEvent::LiveDisarmed { session_id }, false)
                }
                Err(error) => (
                    rejection(
                        command,
                        "live_disarm_failed",
                        &format!("The live input could not be disarmed: {error:?}"),
                    ),
                    false,
                ),
            }
        }
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

fn actual_on_air_at_unix_ms() -> Result<i64, String> {
    let elapsed = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| "system clock is before the Unix epoch".to_string())?;
    i64::try_from(elapsed.as_millis())
        .map_err(|_| "system clock exceeds the representable Unix-millisecond range".into())
}

fn generate_worker_boot_id() -> Result<String, String> {
    let mut bytes = [0_u8; 16];
    getrandom::fill(&mut bytes)
        .map_err(|error| format!("OS randomness is unavailable: {error}"))?;
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;

    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut value = String::with_capacity(36);
    for (index, byte) in bytes.into_iter().enumerate() {
        if matches!(index, 4 | 6 | 8 | 10) {
            value.push('-');
        }
        value.push(char::from(HEX[usize::from(byte >> 4)]));
        value.push(char::from(HEX[usize::from(byte & 0x0f)]));
    }
    Ok(value)
}

fn fatal(code: i32, action: &str, error: &str) -> ! {
    eprintln!("channel worker could not {action}: {error}");
    std::process::exit(code);
}

#[cfg(test)]
mod tests {
    use super::*;

    const SESSION: &str = "11111111-1111-4111-8111-111111111111";
    const SOURCE: &str = "22222222-2222-4222-8222-222222222222";

    fn armed_state() -> LiveState {
        LiveState {
            session_id: SESSION.into(),
            source_id: SOURCE.into(),
            source_revision: 1,
            arm_token: Some(LiveArmToken::new("test-token-123456".into()).unwrap()),
            phase: LivePhase::Armed,
        }
    }

    #[test]
    fn take_authorization_requires_the_exact_session_and_unconsumed_token() {
        let token = LiveArmToken::new("test-token-123456".into()).unwrap();
        let wrong_token = LiveArmToken::new("wrong-token-123456".into()).unwrap();
        let mut state = armed_state();

        assert!(state.accepts_take(SESSION, &token));
        assert!(!state.accepts_take("33333333-3333-4333-8333-333333333333", &token));
        assert!(!state.accepts_take(SESSION, &wrong_token));

        state.phase = LivePhase::OnAir {
            health: LiveBufferHealth {
                last_buffer_counts: Some((1, 1)),
                stalled_since_milliseconds: None,
                consecutive_stalls: 0,
            },
        };
        assert!(!state.accepts_take(SESSION, &token));
    }

    #[test]
    fn returned_live_session_cannot_retake_with_the_consumed_token() {
        let token = LiveArmToken::new("test-token-123456".into()).unwrap();
        let mut state = armed_state();

        state.mark_on_air();
        state.mark_returned();

        assert!(!state.is_on_air());
        assert!(!state.accepts_take(SESSION, &token));
        assert!(matches!(&state.phase, LivePhase::Returned));
    }

    #[test]
    fn failed_take_restoration_keeps_state_truthful_and_consumes_the_token() {
        let token = LiveArmToken::new("test-token-123456".into()).unwrap();
        let mut state = armed_state();

        state.mark_on_air();

        assert!(state.is_on_air());
        assert!(!state.accepts_take(SESSION, &token));
    }

    #[test]
    fn live_token_debug_output_is_redacted() {
        let token = LiveArmToken::new("test-token-123456".into()).unwrap();
        let debug = format!("{token:?}");
        assert!(debug.contains("[REDACTED]"));
        assert!(!debug.contains(token.expose()));
    }

    #[test]
    fn heartbeat_requires_both_live_legs_to_advance() {
        assert!(live_buffers_advanced((7, 9), (8, 10)));
        assert!(!live_buffers_advanced((7, 9), (7, 10)));
        assert!(!live_buffers_advanced((7, 9), (8, 9)));
        assert!(!live_buffers_advanced((7, 9), (6, 10)));
    }

    #[test]
    fn heartbeat_baselines_after_take_then_rejects_consecutive_stalls() {
        let mut health = LiveBufferHealth {
            last_buffer_counts: None,
            stalled_since_milliseconds: None,
            consecutive_stalls: 0,
        };

        assert!(matches!(
            health.observe((7, 9), 0),
            LiveBufferStatus::Baseline
        ));
        assert!(matches!(
            health.observe((7, 9), 500),
            LiveBufferStatus::PendingStall
        ));
        assert!(matches!(
            health.observe((7, 9), 1_000),
            LiveBufferStatus::Stalled
        ));
        assert!(matches!(
            health.observe((8, 10), 1_500),
            LiveBufferStatus::Advanced
        ));
    }

    #[test]
    fn worker_boot_identity_is_canonical_random_uuid_v4() {
        let first = generate_worker_boot_id().unwrap();
        let second = generate_worker_boot_id().unwrap();

        for value in [&first, &second] {
            let bytes = value.as_bytes();
            assert_eq!(bytes.len(), 36);
            assert_eq!(bytes[8], b'-');
            assert_eq!(bytes[13], b'-');
            assert_eq!(bytes[18], b'-');
            assert_eq!(bytes[23], b'-');
            assert_eq!(bytes[14], b'4');
            assert!(matches!(bytes[19], b'8' | b'9' | b'a' | b'b'));
            assert!(bytes.iter().enumerate().all(|(index, byte)| {
                matches!(index, 8 | 13 | 18 | 23)
                    || byte.is_ascii_digit()
                    || (b'a'..=b'f').contains(byte)
            }));
        }
        assert_ne!(first, second);
    }
}
