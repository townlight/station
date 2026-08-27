use std::fs;
use std::io::{Read, Write};
use std::net::UdpSocket;
use std::num::NonZeroU16;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Output, Stdio};
use std::sync::{Mutex, MutexGuard};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use station_media_journal::{
    FallbackToneJournalEntry, JournalWriter, inspect_journal, read_events,
};
use station_media_protocol::{
    CgImageFormat, CgOverlay, CgOverlayContent, CgRect, CgSnapshot, ChannelCommand,
    CommandEnvelope, FallbackToneDeactivationReason, FallbackToneKind, LiveArmToken,
    LiveInputConfiguration, LiveReturnTarget, LoudnessTarget, PROTOCOL_VERSION,
    WorkerBootstrapEnvelope, WorkerEvent, WorkerEventEnvelope, WorkerFallbackSlate,
    WorkerFallbackTone, WorkerHeadendProfileId, WorkerHeadendTransportContract,
    WorkerOutputConfiguration, decode_event_frame, encode_bootstrap_frame, encode_command_frame,
};
use station_storage::StationStore;
use station_windows_ipc::PipeServer;

const WORKER_ID: &str = "ae819b97-2920-434b-9d96-0b51a8ea9abb";
const CHANNEL_ID: &str = "62a47b7e-2a03-48d6-8703-a6e5ce986527";
const WORKER_EXIT_TIMEOUT: Duration = Duration::from_secs(8);
static PROCESS_CONTRACT_TEST_LOCK: Mutex<()> = Mutex::new(());
const FALLBACK_PNG_1X1: &[u8] = &[
    137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 68, 82, 0, 0, 0, 1, 0, 0, 0, 1, 8, 6, 0,
    0, 0, 31, 21, 196, 137, 0, 0, 0, 13, 73, 68, 65, 84, 120, 156, 99, 248, 207, 192, 240, 31, 0,
    5, 0, 1, 255, 137, 153, 61, 29, 0, 0, 0, 0, 73, 69, 78, 68, 174, 66, 96, 130,
];

fn live_audio_is_sustained(half_second_means: &[f64]) -> bool {
    const MINIMUM_DECODED_CHUNKS: usize = 12;
    const AUDIBLE_MEAN_ABS: f64 = 100.0;

    half_second_means.len() >= MINIMUM_DECODED_CHUNKS
        && half_second_means
            .iter()
            .skip(1)
            .all(|mean| mean.is_finite() && *mean > AUDIBLE_MEAN_ABS)
}

#[test]
fn live_audio_gate_rejects_a_long_silent_transition_followed_by_tone() {
    let silent_then_tone = [
        0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 150.0, 150.0, 150.0, 150.0, 150.0, 150.0,
    ];

    assert!(!live_audio_is_sustained(&silent_then_tone));
}

#[test]
fn live_audio_gate_allows_only_one_partial_discovery_chunk() {
    let mut boundary_partial_then_tone = vec![0.0];
    boundary_partial_then_tone.extend([150.0; 11]);
    assert!(live_audio_is_sustained(&boundary_partial_then_tone));

    boundary_partial_then_tone[6] = 0.0;
    assert!(!live_audio_is_sustained(&boundary_partial_then_tone));
}

fn process_contract_test_guard() -> MutexGuard<'static, ()> {
    PROCESS_CONTRACT_TEST_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

#[test]
fn worker_loads_the_exact_managed_fallback_slate_before_ready() {
    let _guard = process_contract_test_guard();
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is before Unix epoch")
        .as_nanos();
    let root = std::env::temp_dir().join(format!(
        "townlight-worker-fallback-slate-{}-{nonce}",
        std::process::id()
    ));
    fs::create_dir_all(&root).unwrap();
    let _temp_dir = TempDirectory(root.clone());
    let image = StationStore::open(root.join("station.db"))
        .unwrap()
        .admit_cg_image_asset(FALLBACK_PNG_1X1)
        .unwrap();
    let journal = root.join("worker.tlj");
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
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut worker = ChildGuard::new(child);
    let mut stream = server.accept_timeout(Duration::from_secs(10)).unwrap();
    let bootstrap = WorkerBootstrapEnvelope {
        version: PROTOCOL_VERSION,
        worker_id: WORKER_ID.into(),
        channel_id: CHANNEL_ID.into(),
        output_profile: "proof-360p30".into(),
        cg_image_root: root.join("cg-images").to_string_lossy().into_owned(),
        proof_artifact_root: root.join("output-proofs").to_string_lossy().into_owned(),
        fallback_slate: Some(WorkerFallbackSlate {
            asset_id: image.asset_id,
            format: CgImageFormat::Png,
        }),
        caption_policy: None,
        outputs: vec![WorkerOutputConfiguration::UdpTs {
            sink_id: "22222222-3333-4444-8555-666666666666".into(),
            destination: output.local_addr().unwrap().to_string(),
            loudness_target_lufs: LoudnessTarget::STREAMING,
            headend: None,
        }],
    };
    stream
        .write_all(&encode_bootstrap_frame(&bootstrap).unwrap())
        .unwrap();
    assert!(matches!(
        read_event(&mut stream).event,
        WorkerEvent::Ready { graph_revision: 1 }
    ));
    output
        .set_read_timeout(Some(Duration::from_secs(8)))
        .unwrap();
    let mut datagram = [0_u8; 65_536];
    assert!(output.recv(&mut datagram).unwrap() > 0);
    stream
        .write_all(
            &encode_command_frame(&command("shutdown-fallback", 2, ChannelCommand::Shutdown))
                .unwrap(),
        )
        .unwrap();
    assert!(matches!(
        read_command_event(&mut stream).event,
        WorkerEvent::ShutdownComplete
    ));
    assert_clean_worker_exit(&mut worker, WORKER_EXIT_TIMEOUT);
}

#[test]
fn worker_refuses_ready_when_the_selected_fallback_slate_is_missing() {
    let _guard = process_contract_test_guard();
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is before Unix epoch")
        .as_nanos();
    let root = std::env::temp_dir().join(format!(
        "townlight-worker-missing-fallback-slate-{}-{nonce}",
        std::process::id()
    ));
    fs::create_dir_all(&root).unwrap();
    let _temp_dir = TempDirectory(root.clone());
    let journal = root.join("worker.tlj");
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
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stream = server.accept_timeout(Duration::from_secs(10)).unwrap();
    let managed_root = journal.parent().unwrap();
    let bootstrap = WorkerBootstrapEnvelope {
        version: PROTOCOL_VERSION,
        worker_id: WORKER_ID.into(),
        channel_id: CHANNEL_ID.into(),
        output_profile: "proof-360p30".into(),
        cg_image_root: managed_root
            .join("cg-images")
            .to_string_lossy()
            .into_owned(),
        proof_artifact_root: managed_root
            .join("output-proofs")
            .to_string_lossy()
            .into_owned(),
        fallback_slate: Some(WorkerFallbackSlate {
            asset_id: "a".repeat(64),
            format: CgImageFormat::Png,
        }),
        caption_policy: None,
        outputs: vec![WorkerOutputConfiguration::UdpTs {
            sink_id: "22222222-3333-4444-8555-666666666666".into(),
            destination: output.local_addr().unwrap().to_string(),
            loudness_target_lufs: LoudnessTarget::STREAMING,
            headend: None,
        }],
    };
    stream
        .write_all(&encode_bootstrap_frame(&bootstrap).unwrap())
        .unwrap();
    drop(stream);
    let result = wait_for_worker_output(child, WORKER_EXIT_TIMEOUT);
    assert_eq!(result.status.code(), Some(8));
    let stderr = String::from_utf8_lossy(&result.stderr);
    assert!(stderr.contains("start its fallback graph"), "{stderr}");
    assert!(stderr.contains("managed image is unreadable"), "{stderr}");
}

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

fn read_event_timeout(
    stream: &mut station_windows_ipc::PipeStream,
    timeout: Duration,
) -> WorkerEventEnvelope {
    let started = Instant::now();
    let mut length = [0_u8; 4];
    stream.read_exact_timeout(&mut length, timeout).unwrap();
    let payload_length = u32::from_le_bytes(length) as usize;
    let mut frame = Vec::with_capacity(4 + payload_length);
    frame.extend_from_slice(&length);
    frame.resize(4 + payload_length, 0);
    stream
        .read_exact_timeout(&mut frame[4..], timeout.saturating_sub(started.elapsed()))
        .unwrap();
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
        proof_artifact_root: journal
            .parent()
            .unwrap()
            .join("output-proofs")
            .to_string_lossy()
            .into_owned(),
        fallback_slate: None,
        caption_policy: None,
        outputs: vec![WorkerOutputConfiguration::UdpTs {
            sink_id: "22222222-3333-4444-8555-666666666666".into(),
            destination: udp.local_addr().unwrap().to_string(),
            loudness_target_lufs: LoudnessTarget::STREAMING,
            headend: None,
        }],
    };
    stream
        .write_all(&encode_bootstrap_frame(&bootstrap).unwrap())
        .unwrap();
    stream.write_all(input).unwrap();
    let events = (0..expected_events)
        .map(|_| read_event(&mut stream))
        .collect();
    drop(stream);
    (wait_for_worker_output(child, WORKER_EXIT_TIMEOUT), events)
}

#[test]
fn worker_rejects_a_staged_profile_before_starting_the_media_graph() {
    let _guard = process_contract_test_guard();
    let journal = journal_path("staged-profile-rejected");
    let _temp_files = TempFiles(vec![journal.clone()]);
    let server = PipeServer::bind(
        &journal
            .file_stem()
            .unwrap()
            .to_string_lossy()
            .replace('.', "-"),
    )
    .unwrap();
    let udp = UdpSocket::bind("127.0.0.1:0").unwrap();
    let child = Command::new(env!("CARGO_BIN_EXE_channel-worker"))
        .args([WORKER_ID, CHANNEL_ID])
        .arg(&journal)
        .arg(server.name())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stream = server.accept().unwrap();
    let bootstrap = WorkerBootstrapEnvelope {
        version: PROTOCOL_VERSION,
        worker_id: WORKER_ID.into(),
        channel_id: CHANNEL_ID.into(),
        output_profile: "atsc-1080i5994".into(),
        cg_image_root: journal
            .parent()
            .unwrap()
            .join("cg-images")
            .to_string_lossy()
            .into_owned(),
        proof_artifact_root: journal
            .parent()
            .unwrap()
            .join("output-proofs")
            .to_string_lossy()
            .into_owned(),
        fallback_slate: None,
        caption_policy: None,
        outputs: vec![WorkerOutputConfiguration::UdpTs {
            sink_id: "22222222-3333-4444-8555-666666666666".into(),
            destination: udp.local_addr().unwrap().to_string(),
            loudness_target_lufs: LoudnessTarget::CABLE,
            headend: None,
        }],
    };
    stream
        .write_all(&encode_bootstrap_frame(&bootstrap).unwrap())
        .unwrap();
    drop(stream);
    let output = child.wait_with_output().unwrap();
    assert_eq!(output.status.code(), Some(6));
    assert!(String::from_utf8_lossy(&output.stderr).contains("not runnable"));
}

#[test]
fn spawned_worker_stops_cleanly_after_its_control_pipe_closes() {
    let _guard = process_contract_test_guard();
    let journal = journal_path("control-pipe-eof");
    let _temp_files = TempFiles(vec![journal.clone()]);

    let (output, emitted) = run_worker(&journal, &[], 1);

    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(emitted.len(), 1);
    assert!(matches!(
        emitted[0].event,
        WorkerEvent::Ready { graph_revision: 1 }
    ));
}

#[test]
fn worker_executes_only_a_finite_revision_bound_fallback_tone() {
    let _guard = process_contract_test_guard();
    let journal = journal_path("finite-fallback-tone");
    let _temp_files = TempFiles(vec![journal.clone()]);
    let activation_id = "1c63ef7e-6e4b-4fa6-b5d8-c15e2b735c05";
    let mut input = encode_command_frame(&command(
        "tone-activate",
        2,
        ChannelCommand::ActivateFallbackTone {
            tone: WorkerFallbackTone {
                activation_id: activation_id.into(),
                authority_revision: 3,
                kind: FallbackToneKind::Test,
                frequency_hz: 1_000,
                level_milli_dbfs: -20_000,
                duration_ms: 1_000,
            },
        },
    ))
    .unwrap();
    input.extend(
        encode_command_frame(&command(
            "tone-deactivate",
            3,
            ChannelCommand::DeactivateFallbackTone {
                activation_id: activation_id.into(),
                authority_revision: 3,
            },
        ))
        .unwrap(),
    );
    let (output, emitted) = run_worker(&journal, &input, 3);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(matches!(emitted[0].event, WorkerEvent::Ready { .. }));
    assert!(matches!(
        emitted[1].event,
        WorkerEvent::FallbackToneActivated { ref activation_id, authority_revision: 3, duration_ms: 1_000, .. }
            if activation_id == "1c63ef7e-6e4b-4fa6-b5d8-c15e2b735c05"
    ));
    assert!(matches!(
        emitted[2].event,
        WorkerEvent::FallbackToneDeactivated { ref activation_id, authority_revision: 3, .. }
            if activation_id == "1c63ef7e-6e4b-4fa6-b5d8-c15e2b735c05"
    ));
}

#[test]
fn shutdown_durably_deactivates_an_active_fallback_tone_before_stopping() {
    let _guard = process_contract_test_guard();
    let journal = journal_path("shutdown-active-fallback-tone");
    let _temp_files = TempFiles(vec![journal.clone()]);
    let tone = WorkerFallbackTone {
        activation_id: "1c63ef7e-6e4b-4fa6-b5d8-c15e2b735c05".into(),
        authority_revision: 3,
        kind: FallbackToneKind::Test,
        frequency_hz: 1_000,
        level_milli_dbfs: -20_000,
        duration_ms: 10_000,
    };
    let mut input = encode_command_frame(&command(
        "activate-before-shutdown",
        2,
        ChannelCommand::ActivateFallbackTone { tone: tone.clone() },
    ))
    .unwrap();
    input.extend(
        encode_command_frame(&command(
            "shutdown-active-tone",
            3,
            ChannelCommand::Shutdown,
        ))
        .unwrap(),
    );
    let (output, emitted) = run_worker(&journal, &input, 4);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(matches!(
        emitted[1].event,
        WorkerEvent::FallbackToneActivated { .. }
    ));
    assert!(matches!(
        &emitted[2].event,
        WorkerEvent::FallbackToneDeactivated {
            deactivation: Some(deactivation), ..
        } if deactivation.tone == tone && deactivation.reason == FallbackToneDeactivationReason::Shutdown
    ));
    assert!(matches!(emitted[3].event, WorkerEvent::ShutdownComplete));
    assert!(
        inspect_journal(&journal)
            .unwrap()
            .fallback_tone_audit
            .iter()
            .any(|entry| matches!(
                entry,
                FallbackToneJournalEntry::Deactivated {
                    tone: recorded_tone,
                    reason: FallbackToneDeactivationReason::Shutdown,
                    ..
                } if recorded_tone == &tone
            ))
    );
}

#[test]
fn failed_live_take_keeps_the_active_fallback_tone_audited_until_shutdown() {
    let _guard = process_contract_test_guard();
    let journal = journal_path("failed-live-keeps-fallback-tone");
    let _temp_files = TempFiles(vec![journal.clone()]);
    let tone = WorkerFallbackTone {
        activation_id: "1c63ef7e-6e4b-4fa6-b5d8-c15e2b735c05".into(),
        authority_revision: 3,
        kind: FallbackToneKind::Test,
        frequency_hz: 1_000,
        level_milli_dbfs: -20_000,
        duration_ms: 10_000,
    };
    let mut input = encode_command_frame(&command(
        "activate-before-failed-live",
        2,
        ChannelCommand::ActivateFallbackTone { tone: tone.clone() },
    ))
    .unwrap();
    input.extend(
        encode_command_frame(&command(
            "failed-live-take",
            3,
            ChannelCommand::TakeLive {
                session_id: "e8d9c07d-1d04-40cc-9fb7-5c1b13f0e961".into(),
                arm_token: LiveArmToken::new("unarmed-live-token".into()).unwrap(),
            },
        ))
        .unwrap(),
    );
    input.extend(
        encode_command_frame(&command(
            "shutdown-after-failed-live",
            4,
            ChannelCommand::Shutdown,
        ))
        .unwrap(),
    );
    let (output, emitted) = run_worker(&journal, &input, 5);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(matches!(
        &emitted[2].event,
        WorkerEvent::CommandRejected { code, .. } if code == "live_not_armed"
    ));
    assert!(matches!(
        &emitted[3].event,
        WorkerEvent::FallbackToneDeactivated {
            deactivation: Some(deactivation), ..
        } if deactivation.tone == tone && deactivation.reason == FallbackToneDeactivationReason::Shutdown
    ));
    let audit = inspect_journal(&journal).unwrap().fallback_tone_audit;
    assert_eq!(
        audit.len(),
        2,
        "failed take must not create a false deactivation"
    );
    assert!(
        matches!(audit[0], FallbackToneJournalEntry::Activated { tone: ref recorded_tone, .. } if recorded_tone == &tone)
    );
    assert!(matches!(
        audit[1],
        FallbackToneJournalEntry::Deactivated {
            reason: FallbackToneDeactivationReason::Shutdown,
            ..
        }
    ));
}

#[test]
fn worker_expires_a_fallback_tone_while_the_supervisor_is_silent() {
    let _guard = process_contract_test_guard();
    let journal = journal_path("autonomous-fallback-tone-expiry");
    let _temp_files = TempFiles(vec![journal.clone()]);
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
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut worker = ChildGuard::new(child);
    let mut stream = server.accept_timeout(Duration::from_secs(10)).unwrap();
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
        proof_artifact_root: journal
            .parent()
            .unwrap()
            .join("output-proofs")
            .to_string_lossy()
            .into_owned(),
        fallback_slate: None,
        caption_policy: None,
        outputs: vec![WorkerOutputConfiguration::UdpTs {
            sink_id: "22222222-3333-4444-8555-666666666666".into(),
            destination: output.local_addr().unwrap().to_string(),
            loudness_target_lufs: LoudnessTarget::STREAMING,
            headend: None,
        }],
    };
    stream
        .write_all(&encode_bootstrap_frame(&bootstrap).unwrap())
        .unwrap();
    assert!(matches!(
        read_event(&mut stream).event,
        WorkerEvent::Ready { .. }
    ));
    let mut tone = WorkerFallbackTone {
        activation_id: "1c63ef7e-6e4b-4fa6-b5d8-c15e2b735c05".into(),
        authority_revision: 7,
        kind: FallbackToneKind::Test,
        frequency_hz: 1_000,
        level_milli_dbfs: -20_000,
        duration_ms: 100,
    };
    stream
        .write_all(
            &encode_command_frame(&command(
                "tone-activate-autonomous-expiry",
                2,
                ChannelCommand::ActivateFallbackTone { tone: tone.clone() },
            ))
            .unwrap(),
        )
        .unwrap();
    assert!(matches!(
        read_command_event(&mut stream).event,
        WorkerEvent::FallbackToneActivated { .. }
    ));

    stream
        .write_all(
            &encode_command_frame(&command(
                "tone-deactivate-before-reuse",
                3,
                ChannelCommand::DeactivateFallbackTone {
                    activation_id: tone.activation_id.clone(),
                    authority_revision: tone.authority_revision,
                },
            ))
            .unwrap(),
        )
        .unwrap();
    assert!(matches!(
        read_command_event(&mut stream).event,
        WorkerEvent::FallbackToneDeactivated {
            deactivation: Some(ref deactivation),
            ..
        } if deactivation.reason == FallbackToneDeactivationReason::Explicit
    ));

    // Reusing authority supplied by the supervisor must not let the old
    // activation's former timer expire the newer activation. The worker owns
    // one deadline and replaces it atomically with this later activation.
    tone.duration_ms = 500;
    stream
        .write_all(
            &encode_command_frame(&command(
                "tone-reactivate-after-reuse",
                4,
                ChannelCommand::ActivateFallbackTone { tone: tone.clone() },
            ))
            .unwrap(),
        )
        .unwrap();
    assert!(matches!(
        read_command_event(&mut stream).event,
        WorkerEvent::FallbackToneActivated {
            duration_ms: 500,
            ..
        }
    ));
    let mut unexpected_event_length = [0_u8; 4];
    assert!(matches!(
        stream.read_exact_timeout(&mut unexpected_event_length, Duration::from_millis(250)),
        Err(station_windows_ipc::IpcError::TimedOut { .. })
    ));

    // Keep the pipe open and send no heartbeat or another command. The worker
    // must run the deadline autonomously and publish the resulting
    // deactivation rather than leaving the supervisor to infer it from a
    // journal side effect.
    assert!(matches!(
        read_event_timeout(&mut stream, Duration::from_secs(2)).event,
        WorkerEvent::FallbackToneDeactivated {
            activation_id: ref event_activation_id,
            authority_revision: 7,
            deactivation: Some(ref deactivation),
        } if event_activation_id == &tone.activation_id
            && deactivation.tone == tone
            && deactivation.reason == FallbackToneDeactivationReason::Expired
    ));
    stream
        .write_all(
            &encode_command_frame(&command(
                "shutdown-after-expiry",
                6,
                ChannelCommand::Shutdown,
            ))
            .unwrap(),
        )
        .unwrap();
    assert!(matches!(
        read_command_event(&mut stream).event,
        WorkerEvent::ShutdownComplete
    ));
    assert_clean_worker_exit(&mut worker, WORKER_EXIT_TIMEOUT);
    let inspection = inspect_journal(&journal).unwrap();
    assert!(inspection.fallback_tone_audit.iter().any(|entry| matches!(
        entry,
        FallbackToneJournalEntry::Deactivated {
            tone: recorded_tone,
            reason: FallbackToneDeactivationReason::Expired,
            ..
        } if recorded_tone == &tone
    )));
}

#[test]
fn spawned_worker_enforces_a_runnable_headend_transport_contract() {
    let _guard = process_contract_test_guard();
    let journal = journal_path("runnable-headend-contract");
    let _temp_files = TempFiles(vec![journal.clone()]);
    let server = PipeServer::bind(
        &journal
            .file_stem()
            .unwrap()
            .to_string_lossy()
            .replace('.', "-"),
    )
    .unwrap();
    let receiver = UdpSocket::bind("127.0.0.1:0").unwrap();
    receiver
        .set_read_timeout(Some(Duration::from_secs(15)))
        .unwrap();
    let child = Command::new(env!("CARGO_BIN_EXE_channel-worker"))
        .args([WORKER_ID, CHANNEL_ID])
        .arg(&journal)
        .arg(server.name())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut worker = ChildGuard::new(child);
    let mut stream = match server.accept_timeout(Duration::from_secs(30)) {
        Ok(stream) => stream,
        Err(error) => {
            let status = worker.try_wait().unwrap();
            let mut stderr = String::new();
            if status.is_some() {
                worker
                    .child
                    .stderr
                    .as_mut()
                    .unwrap()
                    .read_to_string(&mut stderr)
                    .unwrap();
            }
            panic!(
                "headend worker did not connect to its control pipe: {error:?}; status={status:?}; stderr={stderr}"
            );
        }
    };
    let bootstrap = WorkerBootstrapEnvelope {
        version: PROTOCOL_VERSION,
        worker_id: WORKER_ID.into(),
        channel_id: CHANNEL_ID.into(),
        output_profile: "atsc-720p5994".into(),
        cg_image_root: journal
            .parent()
            .unwrap()
            .join("cg-images")
            .to_string_lossy()
            .into_owned(),
        proof_artifact_root: journal
            .parent()
            .unwrap()
            .join("output-proofs")
            .to_string_lossy()
            .into_owned(),
        fallback_slate: None,
        caption_policy: None,
        outputs: vec![WorkerOutputConfiguration::UdpTs {
            sink_id: "22222222-3333-4444-8555-666666666666".into(),
            destination: receiver.local_addr().unwrap().to_string(),
            loudness_target_lufs: LoudnessTarget::CABLE,
            headend: Some(WorkerHeadendTransportContract {
                profile_id: WorkerHeadendProfileId::GenericUdpSpts,
                muxrate_kbps: 8_000,
                packet_size_bytes: 1_316,
            }),
        }],
    };
    stream
        .write_all(&encode_bootstrap_frame(&bootstrap).unwrap())
        .unwrap();
    assert!(matches!(
        read_event(&mut stream).event,
        WorkerEvent::Ready { graph_revision: 1 }
    ));

    let mut datagram = [0_u8; 2_048];
    let received = receiver
        .recv(&mut datagram)
        .expect("runnable headend bootstrap must emit MPEG-TS UDP");
    assert_eq!(received, 1_316);
    assert_eq!(datagram[0], 0x47);

    stream
        .write_all(
            &encode_command_frame(&command("shutdown-headend", 2, ChannelCommand::Shutdown))
                .unwrap(),
        )
        .unwrap();
    assert!(matches!(
        read_event(&mut stream).event,
        WorkerEvent::ShutdownComplete
    ));
    assert_clean_worker_exit(&mut worker, WORKER_EXIT_TIMEOUT);
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
}

fn assert_clean_worker_exit(worker: &mut ChildGuard, timeout: Duration) {
    let started = Instant::now();
    let status = loop {
        match worker.try_wait().expect("query worker process exit") {
            Some(status) => break status,
            None if started.elapsed() < timeout => std::thread::sleep(Duration::from_millis(10)),
            None => panic!(
                "worker acknowledged shutdown but did not exit within {timeout:?}; native teardown remained asynchronous"
            ),
        }
    };
    let mut stderr = String::new();
    worker
        .child
        .stderr
        .as_mut()
        .expect("worker stderr is captured")
        .read_to_string(&mut stderr)
        .expect("read worker stderr after exit");
    assert!(
        status.success(),
        "worker exited unsuccessfully after shutdown: {status:?}; stderr: {stderr}"
    );
    assert!(
        stderr.trim().is_empty(),
        "worker emitted native loader or teardown diagnostics: {stderr}"
    );
}

fn wait_for_worker_output(mut child: Child, timeout: Duration) -> Output {
    let started = Instant::now();
    loop {
        match child.try_wait().expect("query worker process exit") {
            Some(_) => return child.wait_with_output().expect("collect worker output"),
            None if started.elapsed() < timeout => std::thread::sleep(Duration::from_millis(10)),
            None => {
                child.kill().expect("terminate unresponsive worker");
                let output = child
                    .wait_with_output()
                    .expect("collect terminated worker output");
                panic!(
                    "worker did not exit within {timeout:?} after its control pipe closed; stderr: {}",
                    String::from_utf8_lossy(&output.stderr)
                );
            }
        }
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

struct TempDirectory(PathBuf);

impl Drop for TempDirectory {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
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

fn capture_transport_for(
    receiver: &UdpSocket,
    worker: &mut ChildGuard,
    duration: Duration,
) -> Vec<u8> {
    receiver
        .set_read_timeout(Some(Duration::from_secs(8)))
        .unwrap();
    let mut datagram = [0_u8; 65_536];
    let received = receiver.recv(&mut datagram).unwrap_or_else(|error| {
        let worker_status = worker.try_wait().map_or_else(
            |status_error| format!("status error: {status_error}"),
            |status| format!("{status:?}"),
        );
        panic!(
            "worker produced no first MPEG-TS datagram within eight seconds: {error}; worker status: {worker_status}"
        )
    });
    let mut capture = Vec::from(&datagram[..received]);
    receiver
        .set_read_timeout(Some(Duration::from_millis(500)))
        .unwrap();
    let started = Instant::now();
    while started.elapsed() < duration {
        match receiver.recv(&mut datagram) {
            Ok(received) => capture.extend_from_slice(&datagram[..received]),
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                ) => {}
            Err(error) => panic!("could not capture active worker MPEG-TS output: {error}"),
        }
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

fn certutil_sha256(path: &Path) -> String {
    let output = Command::new("certutil.exe")
        .args(["-hashfile", path.to_string_lossy().as_ref(), "SHA256"])
        .output()
        .expect("CertUtil is required to independently verify the output proof hash");
    assert!(
        output.status.success(),
        "CertUtil failed to hash output proof: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .map(str::trim)
        .find(|line| line.len() == 64 && line.bytes().all(|byte| byte.is_ascii_hexdigit()))
        .expect("CertUtil did not return a SHA-256 digest")
        .to_ascii_lowercase()
}

#[test]
fn spawned_worker_recording_and_output_proof_finish_independently_with_exact_hashes() {
    let _guard = process_contract_test_guard();
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is before Unix epoch")
        .as_nanos();
    let root = std::env::temp_dir().join(format!(
        "townlight-worker-output-proof-{}-{nonce}",
        std::process::id()
    ));
    fs::create_dir_all(&root).unwrap();
    let _temp_dir = TempDirectory(root.clone());
    let journal = root.join("worker.tlj");
    let proof_root = root.join("output-proofs");
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
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut worker = ChildGuard::new(child);
    let mut stream = server.accept_timeout(Duration::from_secs(10)).unwrap();
    let sink_id = "22222222-3333-4444-8555-666666666666";
    let bootstrap = WorkerBootstrapEnvelope {
        version: PROTOCOL_VERSION,
        worker_id: WORKER_ID.into(),
        channel_id: CHANNEL_ID.into(),
        output_profile: "proof-360p30".into(),
        cg_image_root: root.join("cg-images").to_string_lossy().into_owned(),
        proof_artifact_root: proof_root.to_string_lossy().into_owned(),
        fallback_slate: None,
        caption_policy: None,
        outputs: vec![WorkerOutputConfiguration::UdpTs {
            sink_id: sink_id.into(),
            destination: output.local_addr().unwrap().to_string(),
            loudness_target_lufs: LoudnessTarget::CABLE,
            headend: None,
        }],
    };
    stream
        .write_all(&encode_bootstrap_frame(&bootstrap).unwrap())
        .unwrap();
    let ready = read_event(&mut stream);
    assert!(matches!(
        ready.event,
        WorkerEvent::Ready { graph_revision: 1 }
    ));

    let mut expected_sequence = 2;
    let health_deadline = Instant::now() + Duration::from_secs(12);
    let health_before = loop {
        assert!(
            Instant::now() < health_deadline,
            "worker output health did not advance"
        );
        let command_id = format!("health-before-{expected_sequence}");
        stream
            .write_all(
                &encode_command_frame(&command(
                    &command_id,
                    expected_sequence,
                    ChannelCommand::Ping,
                ))
                .unwrap(),
            )
            .unwrap();
        let event = read_event(&mut stream);
        let health = match event.event {
            WorkerEvent::Heartbeat { outputs, .. } => outputs
                .into_iter()
                .next()
                .expect("worker heartbeat omitted configured output"),
            other => panic!("unexpected health event: {other:?}"),
        };
        expected_sequence += 1;
        if health.bytes_sent_total > 0 && health.audio_buffers_total > 0 {
            break health;
        }
        std::thread::sleep(Duration::from_millis(100));
    };

    let recording_id = "44444444-5555-4666-8777-888888888888";
    stream
        .write_all(
            &encode_command_frame(&command(
                "start-scheduled-recording",
                expected_sequence,
                ChannelCommand::StartScheduledRecording {
                    recording_id: recording_id.into(),
                    output_index: 0,
                    sink_id: sink_id.into(),
                    duration_ms: 3_000,
                    max_bytes: 16 * 1024 * 1024,
                },
            ))
            .unwrap(),
        )
        .unwrap();
    let recording_artifact_relative_path = match read_event(&mut stream).event {
        WorkerEvent::ScheduledRecordingStarted {
            recording_id: event_recording_id,
            output_index,
            sink_id: event_sink_id,
            graph_revision,
            artifact_relative_path,
            ..
        } => {
            assert_eq!(event_recording_id, recording_id);
            assert_eq!(output_index, 0);
            assert_eq!(event_sink_id, sink_id);
            assert_eq!(graph_revision, 1);
            artifact_relative_path
        }
        other => panic!("unexpected scheduled recording start event: {other:?}"),
    };
    expected_sequence += 1;

    let proof_id = "11111111-2222-4333-8444-555555555555";
    stream
        .write_all(
            &encode_command_frame(&command(
                "start-output-proof",
                expected_sequence,
                ChannelCommand::StartOutputProof {
                    proof_id: proof_id.into(),
                    output_index: 0,
                    sink_id: sink_id.into(),
                    loudness_target_lufs: LoudnessTarget::CABLE,
                    duration_ms: 3_000,
                    max_bytes: 16 * 1024 * 1024,
                },
            ))
            .unwrap(),
        )
        .unwrap();
    let started = read_event(&mut stream);
    let artifact_relative_path = match started.event {
        WorkerEvent::OutputProofStarted {
            proof_id: event_proof_id,
            output_index,
            sink_id: event_sink_id,
            loudness_target_lufs,
            graph_revision,
            artifact_relative_path,
            ..
        } => {
            assert_eq!(event_proof_id, proof_id);
            assert_eq!(output_index, 0);
            assert_eq!(event_sink_id, sink_id);
            assert_eq!(loudness_target_lufs, LoudnessTarget::CABLE);
            assert_eq!(graph_revision, 1);
            artifact_relative_path
        }
        other => panic!("unexpected proof start event: {other:?}"),
    };
    expected_sequence += 1;

    let deadline = Instant::now() + Duration::from_secs(12);
    let ready_proof = loop {
        assert!(
            Instant::now() < deadline,
            "output proof did not reach terminal state"
        );
        stream
            .write_all(
                &encode_command_frame(&command(
                    "poll-output-proof",
                    expected_sequence,
                    ChannelCommand::PollOutputProof {
                        proof_id: proof_id.into(),
                    },
                ))
                .unwrap(),
            )
            .unwrap();
        let event = read_event(&mut stream);
        expected_sequence += 1;
        match event.event {
            WorkerEvent::OutputProofPending {
                proof_id: event_proof_id,
                graph_revision,
                ..
            } => {
                assert_eq!(event_proof_id, proof_id);
                assert_eq!(graph_revision, 1);
                std::thread::sleep(Duration::from_millis(100));
            }
            WorkerEvent::OutputProofReady {
                proof_id: event_proof_id,
                output_index,
                sink_id: event_sink_id,
                loudness_target_lufs,
                artifact_relative_path: event_path,
                graph_revision,
                size_bytes,
                capture_sha256,
                duration_ms,
                ..
            } => {
                assert_eq!(event_proof_id, proof_id);
                assert_eq!(output_index, 0);
                assert_eq!(event_sink_id, sink_id);
                assert_eq!(loudness_target_lufs, LoudnessTarget::CABLE);
                assert_eq!(event_path, artifact_relative_path);
                assert_eq!(graph_revision, 1);
                assert!(size_bytes > 0 && size_bytes <= 16 * 1024 * 1024);
                assert!(duration_ms >= 3_000);
                break (event_path, size_bytes, capture_sha256, duration_ms);
            }
            WorkerEvent::OutputProofFailed { code, message, .. } => {
                panic!("output proof failed: {code}: {message}")
            }
            other => panic!("unexpected output proof event: {other:?}"),
        }
    };
    let (artifact_relative_path, size_bytes, capture_sha256, duration_ms) = ready_proof;
    assert!(duration_ms >= 3_000);
    let artifact = proof_root.join(&artifact_relative_path);
    assert!(artifact.is_file(), "proof artifact was not published");
    let bytes = fs::read(&artifact).unwrap();
    assert_eq!(bytes.len() as u64, size_bytes);
    assert_eq!(certutil_sha256(&artifact), capture_sha256);
    assert!(health_before.bytes_sent_total > 0);

    let recording_deadline = Instant::now() + Duration::from_secs(12);
    let ready_recording = loop {
        assert!(
            Instant::now() < recording_deadline,
            "scheduled recording did not reach terminal state"
        );
        stream
            .write_all(
                &encode_command_frame(&command(
                    "poll-scheduled-recording",
                    expected_sequence,
                    ChannelCommand::PollScheduledRecording {
                        recording_id: recording_id.into(),
                    },
                ))
                .unwrap(),
            )
            .unwrap();
        expected_sequence += 1;
        match read_event(&mut stream).event {
            WorkerEvent::ScheduledRecordingPending {
                recording_id: event_recording_id,
                graph_revision,
                ..
            } => {
                assert_eq!(event_recording_id, recording_id);
                assert_eq!(graph_revision, 1);
                std::thread::sleep(Duration::from_millis(100));
            }
            WorkerEvent::ScheduledRecordingReady {
                recording_id: event_recording_id,
                output_index,
                sink_id: event_sink_id,
                artifact_relative_path,
                graph_revision,
                size_bytes,
                capture_sha256,
                duration_ms,
                ..
            } => {
                assert_eq!(event_recording_id, recording_id);
                assert_eq!(output_index, 0);
                assert_eq!(event_sink_id, sink_id);
                assert_eq!(artifact_relative_path, recording_artifact_relative_path);
                assert_eq!(graph_revision, 1);
                assert!(size_bytes > 0 && size_bytes <= 16 * 1024 * 1024);
                assert!(duration_ms >= 3_000);
                break (artifact_relative_path, size_bytes, capture_sha256);
            }
            WorkerEvent::ScheduledRecordingFailed { code, message, .. } => {
                panic!("scheduled recording failed: {code}: {message}")
            }
            other => panic!("unexpected scheduled recording event: {other:?}"),
        }
    };
    let (recording_path, recording_size, recording_sha256) = ready_recording;
    let recording_artifact = root
        .join("recordings")
        .join(CHANNEL_ID)
        .join(recording_path);
    assert!(recording_artifact.is_file());
    assert_eq!(
        fs::metadata(&recording_artifact).unwrap().len(),
        recording_size
    );
    assert_eq!(certutil_sha256(&recording_artifact), recording_sha256);

    // Let the capture seal before changing the worker graph revision. The
    // worker must report that exact Ready authority, not contradict the
    // published artifact with a later graph-drift failure.
    let fenced_recording_id = "66666666-7777-4888-8999-aaaaaaaaaaaa";
    stream
        .write_all(
            &encode_command_frame(&command(
                "start-fenced-scheduled-recording",
                expected_sequence,
                ChannelCommand::StartScheduledRecording {
                    recording_id: fenced_recording_id.into(),
                    output_index: 0,
                    sink_id: sink_id.into(),
                    duration_ms: 3_000,
                    max_bytes: 16 * 1024 * 1024,
                },
            ))
            .unwrap(),
        )
        .unwrap();
    let fenced_artifact_relative_path = match read_event(&mut stream).event {
        WorkerEvent::ScheduledRecordingStarted {
            recording_id,
            artifact_relative_path,
            graph_revision,
            ..
        } => {
            assert_eq!(recording_id, fenced_recording_id);
            assert_eq!(graph_revision, 1);
            artifact_relative_path
        }
        other => panic!("unexpected fenced recording start event: {other:?}"),
    };
    expected_sequence += 1;
    std::thread::sleep(Duration::from_millis(3_500));
    stream
        .write_all(
            &encode_command_frame(&command(
                "change-graph-after-recording-ready",
                expected_sequence,
                ChannelCommand::ApplyPlan {
                    plan_id: "graph-drift-fence".into(),
                    revision: 2,
                },
            ))
            .unwrap(),
        )
        .unwrap();
    assert!(matches!(
        read_event(&mut stream).event,
        WorkerEvent::Ready { graph_revision: 2 }
    ));
    expected_sequence += 1;
    stream
        .write_all(
            &encode_command_frame(&command(
                "poll-fenced-scheduled-recording",
                expected_sequence,
                ChannelCommand::PollScheduledRecording {
                    recording_id: fenced_recording_id.into(),
                },
            ))
            .unwrap(),
        )
        .unwrap();
    let (fenced_size, fenced_sha256) = match read_event(&mut stream).event {
        WorkerEvent::ScheduledRecordingReady {
            recording_id,
            artifact_relative_path,
            graph_revision,
            size_bytes,
            capture_sha256,
            ..
        } => {
            assert_eq!(recording_id, fenced_recording_id);
            assert_eq!(artifact_relative_path, fenced_artifact_relative_path);
            assert_eq!(graph_revision, 1);
            (size_bytes, capture_sha256)
        }
        other => panic!("sealed recording must remain Ready across graph drift: {other:?}"),
    };
    expected_sequence += 1;
    let fenced_artifact = root
        .join("recordings")
        .join(CHANNEL_ID)
        .join(fenced_artifact_relative_path);
    assert_eq!(fs::metadata(&fenced_artifact).unwrap().len(), fenced_size);
    assert_eq!(certutil_sha256(&fenced_artifact), fenced_sha256);

    let aborted_recording_id = "55555555-6666-4777-8888-999999999999";
    stream
        .write_all(
            &encode_command_frame(&command(
                "start-aborted-scheduled-recording",
                expected_sequence,
                ChannelCommand::StartScheduledRecording {
                    recording_id: aborted_recording_id.into(),
                    output_index: 0,
                    sink_id: sink_id.into(),
                    duration_ms: 30_000,
                    max_bytes: 16 * 1024 * 1024,
                },
            ))
            .unwrap(),
        )
        .unwrap();
    let aborted_recording_artifact = match read_event(&mut stream).event {
        WorkerEvent::ScheduledRecordingStarted {
            recording_id,
            artifact_relative_path,
            ..
        } => {
            assert_eq!(recording_id, aborted_recording_id);
            artifact_relative_path
        }
        other => panic!("unexpected aborted recording start event: {other:?}"),
    };
    expected_sequence += 1;
    stream
        .write_all(
            &encode_command_frame(&command(
                "change-graph-while-recording-is-capturing",
                expected_sequence,
                ChannelCommand::ApplyPlan {
                    plan_id: "graph-drift-pending-fence".into(),
                    revision: 3,
                },
            ))
            .unwrap(),
        )
        .unwrap();
    assert!(matches!(
        read_event(&mut stream).event,
        WorkerEvent::Ready { graph_revision: 3 }
    ));
    expected_sequence += 1;
    stream
        .write_all(
            &encode_command_frame(&command(
                "poll-graph-fenced-scheduled-recording",
                expected_sequence,
                ChannelCommand::PollScheduledRecording {
                    recording_id: aborted_recording_id.into(),
                },
            ))
            .unwrap(),
        )
        .unwrap();
    match read_event(&mut stream).event {
        WorkerEvent::ScheduledRecordingFailed {
            recording_id,
            artifact_relative_path,
            code,
            graph_revision,
            ..
        } => {
            assert_eq!(recording_id, aborted_recording_id);
            assert_eq!(artifact_relative_path, aborted_recording_artifact);
            assert_eq!(code, "graph_revision_changed");
            assert_eq!(graph_revision, 2);
        }
        other => panic!("graph-fenced recording must not report Ready: {other:?}"),
    }
    expected_sequence += 1;

    stream
        .write_all(
            &encode_command_frame(&command(
                "health-after",
                expected_sequence,
                ChannelCommand::Ping,
            ))
            .unwrap(),
        )
        .unwrap();
    let health_after_event = read_event(&mut stream);
    let health_after = match health_after_event.event {
        WorkerEvent::Heartbeat { outputs, .. } => outputs
            .into_iter()
            .next()
            .expect("worker heartbeat omitted configured output"),
        other => panic!("unexpected post-proof health event: {other:?}"),
    };
    expected_sequence += 1;
    assert!(health_after.bytes_sent_total > health_before.bytes_sent_total);
    assert!(health_after.audio_buffers_total > health_before.audio_buffers_total);

    let aborted_proof_id = "33333333-4444-4555-8666-777777777777";
    stream
        .write_all(
            &encode_command_frame(&command(
                "start-aborted-output-proof",
                expected_sequence,
                ChannelCommand::StartOutputProof {
                    proof_id: aborted_proof_id.into(),
                    output_index: 0,
                    sink_id: sink_id.into(),
                    loudness_target_lufs: LoudnessTarget::CABLE,
                    duration_ms: 30_000,
                    max_bytes: 16 * 1024 * 1024,
                },
            ))
            .unwrap(),
        )
        .unwrap();
    let aborted_artifact = match read_event(&mut stream).event {
        WorkerEvent::OutputProofStarted {
            proof_id,
            artifact_relative_path,
            ..
        } => {
            assert_eq!(proof_id, aborted_proof_id);
            artifact_relative_path
        }
        other => panic!("unexpected aborted proof start event: {other:?}"),
    };
    expected_sequence += 1;
    stream
        .write_all(
            &encode_command_frame(&command(
                "abort-output-proof",
                expected_sequence,
                ChannelCommand::AbortOutputProof {
                    proof_id: aborted_proof_id.into(),
                },
            ))
            .unwrap(),
        )
        .unwrap();
    match read_event(&mut stream).event {
        WorkerEvent::OutputProofFailed {
            proof_id,
            artifact_relative_path,
            code,
            graph_revision,
            ..
        } => {
            assert_eq!(proof_id, aborted_proof_id);
            assert_eq!(artifact_relative_path, aborted_artifact);
            assert_eq!(code, "capture_aborted");
            assert_eq!(graph_revision, 3);
        }
        other => panic!("unexpected output proof abort event: {other:?}"),
    }
    expected_sequence += 1;
    let abort_cleanup_deadline = Instant::now() + Duration::from_secs(2);
    let aborted_part = proof_root.join(format!("{aborted_proof_id}.ts.part"));
    while aborted_part.exists() && Instant::now() < abort_cleanup_deadline {
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(!aborted_part.exists(), "aborted proof part was not removed");
    assert!(
        !proof_root.join(&aborted_artifact).exists(),
        "aborted proof artifact was published"
    );

    stream
        .write_all(
            &encode_command_frame(&command(
                "shutdown",
                expected_sequence,
                ChannelCommand::Shutdown,
            ))
            .unwrap(),
        )
        .unwrap();
    assert!(matches!(
        read_event(&mut stream).event,
        WorkerEvent::ShutdownComplete
    ));
    assert_clean_worker_exit(&mut worker, WORKER_EXIT_TIMEOUT);
}

#[test]
fn spawned_worker_live_takeover_preserves_audible_audio() {
    let _guard = process_contract_test_guard();
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
        .stderr(Stdio::piped())
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
        proof_artifact_root: journal
            .parent()
            .unwrap()
            .join("output-proofs")
            .to_string_lossy()
            .into_owned(),
        fallback_slate: None,
        caption_policy: None,
        outputs: vec![WorkerOutputConfiguration::UdpTs {
            sink_id: "22222222-3333-4444-8555-666666666666".into(),
            destination: output.local_addr().unwrap().to_string(),
            loudness_target_lufs: LoudnessTarget::STREAMING,
            headend: None,
        }],
    };
    stream
        .write_all(&encode_bootstrap_frame(&bootstrap).unwrap())
        .unwrap();
    let ready = read_event(&mut stream);
    assert!(matches!(
        ready.event,
        WorkerEvent::Ready { graph_revision: 1 }
    ));

    let input_probe = UdpSocket::bind("127.0.0.1:0").unwrap();
    let input_port = input_probe.local_addr().unwrap().port();
    input_probe
        .set_read_timeout(Some(Duration::from_secs(30)))
        .unwrap();
    let mut sender = start_ffmpeg_live_sender(input_port);
    let mut sender_probe = [0_u8; 65_536];
    assert!(
        input_probe.recv(&mut sender_probe).unwrap() > 0,
        "the live sender must emit a datagram before the worker arms its receiver"
    );
    drop(input_probe);
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
    let arm_event = read_command_event(&mut stream);
    assert!(
        matches!(
            arm_event.event,
            WorkerEvent::LiveArmed {
                session_id: ref event_session,
                ..
            }
                if event_session == session_id
        ),
        "unexpected arm event: {:?}",
        arm_event.event
    );
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
    // A transport capture beginning at an arbitrary mux boundary may decode
    // one partial leading chunk. Eight seconds must still yield at least six
    // seconds of decoded audio with no later silent half-second gap.
    let transport_capture = capture_transport_for(&output, &mut worker, Duration::from_secs(8));
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
    assert_clean_worker_exit(&mut worker, WORKER_EXIT_TIMEOUT);

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
    let sustained_audio = live_audio_is_sustained(&chunk_means);
    assert!(
        count > 300_000 && mean_abs > 100.0 && sustained_audio,
        "FFmpeg live audio was not sustained: {count} decoded samples, mean abs {mean_abs:.2}, half-second chunks {chunk_means:?}"
    );
}

#[test]
fn journals_each_event_before_emitting_it_and_recovers_sequence_after_restart() {
    let _guard = process_contract_test_guard();
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
        WorkerEvent::Ready { graph_revision: 1 }
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
        WorkerEvent::Ready { graph_revision: 1 }
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
fn spawned_worker_recovers_sequence_from_a_compacted_heartbeat_journal() {
    let _guard = process_contract_test_guard();
    let journal = journal_path("compacted-heartbeat-restart");
    let mut writer = JournalWriter::open(&journal, WORKER_ID, CHANNEL_ID).unwrap();
    writer
        .append(&WorkerEventEnvelope {
            version: PROTOCOL_VERSION,
            worker_id: WORKER_ID.into(),
            channel_id: CHANNEL_ID.into(),
            sequence: 1,
            event: WorkerEvent::Ready { graph_revision: 1 },
        })
        .unwrap();
    for sequence in 2..=514 {
        writer
            .append(&WorkerEventEnvelope {
                version: PROTOCOL_VERSION,
                worker_id: WORKER_ID.into(),
                channel_id: CHANNEL_ID.into(),
                sequence,
                event: WorkerEvent::Heartbeat {
                    monotonic_milliseconds: sequence * 5_000,
                    outputs: Vec::new(),
                },
            })
            .unwrap();
    }
    drop(writer);
    assert!(
        read_events(&journal).unwrap().len() < 514,
        "the preloaded journal did not compact"
    );

    let input = encode_command_frame(&command(
        "shutdown-after-compaction",
        516,
        ChannelCommand::Shutdown,
    ))
    .unwrap();
    let (output, emitted) = run_worker(&journal, &input, 2);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(emitted[0].sequence, 515);
    assert!(matches!(
        emitted[0].event,
        WorkerEvent::Ready { graph_revision: 1 }
    ));
    assert_eq!(emitted[1].sequence, 516);
    assert!(matches!(emitted[1].event, WorkerEvent::ShutdownComplete));

    let reopened = JournalWriter::open(&journal, WORKER_ID, CHANNEL_ID).unwrap();
    assert_eq!(reopened.next_sequence(), 517);
    drop(reopened);
    let _ = std::fs::remove_file(journal);
}

#[test]
fn rejects_a_stale_command_and_durably_records_the_rejection() {
    let _guard = process_contract_test_guard();
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
    let _guard = process_contract_test_guard();
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
