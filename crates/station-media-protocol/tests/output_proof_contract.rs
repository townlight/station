use serde_json::json;
use station_media_protocol::{
    ChannelCommand, CommandEnvelope, LoudnessTarget, MAX_OUTPUT_PROOF_BYTES,
    MAX_OUTPUT_PROOF_DURATION_MS, MAX_OUTPUT_PROOF_SAMPLE_DURATION_MS,
    OUTPUT_PROOF_CAPTURE_HEADROOM_MS, PROTOCOL_VERSION, WorkerBootstrapEnvelope, WorkerEvent,
    WorkerEventEnvelope, WorkerOutputConfiguration, decode_bootstrap_frame, decode_command_frame,
    decode_event_frame, encode_bootstrap_frame, encode_command_frame, encode_event_frame,
};

const PROOF_ID: &str = "11111111-2222-4333-8444-555555555555";
const SINK_ID: &str = "22222222-3333-4444-8555-666666666666";
const ARTIFACT: &str = "proofs/run-1/output-0.ts";
const HASH: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

fn start_command() -> CommandEnvelope {
    CommandEnvelope {
        version: PROTOCOL_VERSION,
        channel_id: "channel-1".into(),
        command_id: "command-1".into(),
        expected_sequence: 8,
        command: ChannelCommand::StartOutputProof {
            proof_id: PROOF_ID.into(),
            output_index: 0,
            sink_id: SINK_ID.into(),
            loudness_target_lufs: LoudnessTarget::CABLE,
            duration_ms: 30_000,
            max_bytes: 1_000_000,
        },
    }
}

#[test]
fn physical_capture_bound_preserves_operator_limit_and_decode_headroom() {
    assert_eq!(MAX_OUTPUT_PROOF_SAMPLE_DURATION_MS, 600_000);
    assert_eq!(OUTPUT_PROOF_CAPTURE_HEADROOM_MS, 4_000);
    assert_eq!(MAX_OUTPUT_PROOF_DURATION_MS, 604_000);
    assert_eq!(MAX_OUTPUT_PROOF_BYTES, 1024 * 1024 * 1024);
}

#[test]
fn start_poll_and_abort_proof_commands_round_trip_without_blocking_shape() {
    let start = start_command();
    let decoded = decode_command_frame(&encode_command_frame(&start).unwrap()).unwrap();
    assert_eq!(decoded, start);

    let poll = CommandEnvelope {
        version: PROTOCOL_VERSION,
        channel_id: "channel-1".into(),
        command_id: "command-2".into(),
        expected_sequence: 8,
        command: ChannelCommand::PollOutputProof {
            proof_id: PROOF_ID.into(),
        },
    };
    assert_eq!(
        decode_command_frame(&encode_command_frame(&poll).unwrap()).unwrap(),
        poll
    );

    let abort = CommandEnvelope {
        version: PROTOCOL_VERSION,
        channel_id: "channel-1".into(),
        command_id: "command-3".into(),
        expected_sequence: 8,
        command: ChannelCommand::AbortOutputProof {
            proof_id: PROOF_ID.into(),
        },
    };
    assert_eq!(
        decode_command_frame(&encode_command_frame(&abort).unwrap()).unwrap(),
        abort
    );
}

#[test]
fn proof_replies_round_trip_with_worker_graph_and_artifact_metadata() {
    let replies = [
        WorkerEvent::OutputProofStarted {
            proof_id: PROOF_ID.into(),
            output_index: 0,
            sink_id: SINK_ID.into(),
            loudness_target_lufs: LoudnessTarget::CABLE,
            duration_ms: 30_000,
            max_bytes: 1_000_000,
            artifact_relative_path: ARTIFACT.into(),
            worker_instance_id: "worker-a".into(),
            graph_revision: 12,
        },
        WorkerEvent::OutputProofPending {
            proof_id: PROOF_ID.into(),
            artifact_relative_path: ARTIFACT.into(),
            worker_instance_id: "worker-a".into(),
            graph_revision: 12,
        },
        WorkerEvent::OutputProofReady {
            proof_id: PROOF_ID.into(),
            output_index: 0,
            sink_id: SINK_ID.into(),
            loudness_target_lufs: LoudnessTarget::CABLE,
            artifact_relative_path: ARTIFACT.into(),
            worker_instance_id: "worker-a".into(),
            graph_revision: 12,
            size_bytes: 999_000,
            capture_sha256: HASH.into(),
            duration_ms: 29_900,
        },
        WorkerEvent::OutputProofFailed {
            proof_id: PROOF_ID.into(),
            artifact_relative_path: ARTIFACT.into(),
            worker_instance_id: "worker-a".into(),
            graph_revision: 12,
            code: "capture_timeout".into(),
            message: "bounded capture did not complete".into(),
        },
    ];
    for (sequence, event) in replies.into_iter().enumerate() {
        let envelope = WorkerEventEnvelope {
            version: PROTOCOL_VERSION,
            worker_id: "worker-a".into(),
            channel_id: "channel-1".into(),
            sequence: sequence as u64 + 1,
            event,
        };
        let decoded = decode_event_frame(&encode_event_frame(&envelope).unwrap()).unwrap();
        assert_eq!(decoded, envelope);
    }
}

#[test]
fn malformed_proof_identity_bounds_and_artifact_paths_fail_closed() {
    let mut command = start_command();
    if let ChannelCommand::StartOutputProof { proof_id, .. } = &mut command.command {
        *proof_id = "not-a-uuid".into();
    }
    assert!(encode_command_frame(&command).is_err());

    let mut command = start_command();
    if let ChannelCommand::StartOutputProof { output_index, .. } = &mut command.command {
        *output_index = 256;
    }
    assert!(encode_command_frame(&command).is_err());

    let mut command = start_command();
    if let ChannelCommand::StartOutputProof { duration_ms, .. } = &mut command.command {
        *duration_ms = MAX_OUTPUT_PROOF_DURATION_MS + 1;
    }
    assert!(encode_command_frame(&command).is_err());

    let mut command = start_command();
    if let ChannelCommand::StartOutputProof { duration_ms, .. } = &mut command.command {
        *duration_ms = MAX_OUTPUT_PROOF_DURATION_MS;
    }
    assert!(encode_command_frame(&command).is_ok());

    let mut command = start_command();
    if let ChannelCommand::StartOutputProof { duration_ms, .. } = &mut command.command {
        *duration_ms = 2_999;
    }
    assert!(encode_command_frame(&command).is_err());

    let mut command = start_command();
    if let ChannelCommand::StartOutputProof { max_bytes, .. } = &mut command.command {
        *max_bytes = MAX_OUTPUT_PROOF_BYTES + 1;
    }
    assert!(encode_command_frame(&command).is_err());

    let mut poll = start_command();
    poll.command = ChannelCommand::PollOutputProof {
        proof_id: "bad-proof-id".into(),
    };
    assert!(encode_command_frame(&poll).is_err());

    let mut abort = start_command();
    abort.command = ChannelCommand::AbortOutputProof {
        proof_id: "BAD-PROOF-ID".into(),
    };
    assert!(encode_command_frame(&abort).is_err());
}

#[test]
fn unknown_proof_fields_are_rejected() {
    let mut value = serde_json::to_value(start_command()).unwrap();
    value["command"]["unexpected"] = json!(true);
    assert!(serde_json::from_value::<CommandEnvelope>(value).is_err());

    let mut value = serde_json::to_value(WorkerEventEnvelope {
        version: PROTOCOL_VERSION,
        worker_id: "worker-a".into(),
        channel_id: "channel-1".into(),
        sequence: 1,
        event: WorkerEvent::OutputProofPending {
            proof_id: PROOF_ID.into(),
            artifact_relative_path: ARTIFACT.into(),
            worker_instance_id: "worker-a".into(),
            graph_revision: 12,
        },
    })
    .unwrap();
    value["event"]["unexpected"] = json!(true);
    assert!(serde_json::from_value::<WorkerEventEnvelope>(value).is_err());
}

#[test]
fn ready_reply_rejects_bad_capture_hash_and_unbounded_duration() {
    let event = WorkerEventEnvelope {
        version: PROTOCOL_VERSION,
        worker_id: "worker-a".into(),
        channel_id: "channel-1".into(),
        sequence: 1,
        event: WorkerEvent::OutputProofReady {
            proof_id: PROOF_ID.into(),
            output_index: 0,
            sink_id: SINK_ID.into(),
            loudness_target_lufs: LoudnessTarget::STREAMING,
            artifact_relative_path: ARTIFACT.into(),
            worker_instance_id: "worker-a".into(),
            graph_revision: 12,
            size_bytes: 1,
            capture_sha256: "not-a-hash".into(),
            duration_ms: 0,
        },
    };
    assert!(encode_event_frame(&event).is_err());
}

#[test]
fn proof_start_must_bind_sink_and_target_to_bootstrap_output_index() {
    let bootstrap = WorkerBootstrapEnvelope {
        version: PROTOCOL_VERSION,
        worker_id: "worker-a".into(),
        channel_id: "channel-1".into(),
        output_profile: "proof-360p30".into(),
        cg_image_root: r"C:\ProgramData\TownLight Station\cg-images".into(),
        proof_artifact_root: r"C:\ProgramData\TownLight Station\output-proofs".into(),
        outputs: vec![
            WorkerOutputConfiguration::UdpTs {
                sink_id: SINK_ID.into(),
                destination: "127.0.0.1:5500".into(),
                loudness_target_lufs: LoudnessTarget::CABLE,
                headend: None,
            },
            WorkerOutputConfiguration::UdpTs {
                sink_id: "33333333-4444-4555-8666-777777777777".into(),
                destination: "127.0.0.1:5501".into(),
                loudness_target_lufs: LoudnessTarget::STREAMING,
                headend: None,
            },
        ],
    };
    let frame = encode_bootstrap_frame(&bootstrap).unwrap();
    let decoded = decode_bootstrap_frame(&frame).unwrap();
    decoded
        .validate_output_proof_binding(0, SINK_ID, LoudnessTarget::CABLE)
        .unwrap();
    assert!(
        decoded
            .validate_output_proof_binding(
                0,
                "33333333-4444-4555-8666-777777777777",
                LoudnessTarget::CABLE,
            )
            .is_err()
    );
    assert!(
        decoded
            .validate_output_proof_binding(0, SINK_ID, LoudnessTarget::STREAMING)
            .is_err()
    );
    assert!(
        decoded
            .validate_output_proof_binding(2, SINK_ID, LoudnessTarget::CABLE)
            .is_err()
    );
}

#[test]
fn bootstrap_rejects_unsafe_managed_proof_roots_and_duplicate_sink_ids() {
    let mut bootstrap = WorkerBootstrapEnvelope {
        version: PROTOCOL_VERSION,
        worker_id: "worker-a".into(),
        channel_id: "channel-1".into(),
        output_profile: "proof-360p30".into(),
        cg_image_root: r"C:\ProgramData\TownLight Station\cg-images".into(),
        proof_artifact_root: r"C:\ProgramData\TownLight Station\output-proofs\..\output-proofs"
            .into(),
        outputs: Vec::new(),
    };
    assert!(encode_bootstrap_frame(&bootstrap).is_err());

    bootstrap.proof_artifact_root = r"C:\ProgramData\TownLight Station\output-proofs".into();
    bootstrap.outputs = vec![
        WorkerOutputConfiguration::UdpTs {
            sink_id: SINK_ID.into(),
            destination: "127.0.0.1:5500".into(),
            loudness_target_lufs: LoudnessTarget::CABLE,
            headend: None,
        },
        WorkerOutputConfiguration::UdpTs {
            sink_id: SINK_ID.into(),
            destination: "127.0.0.1:5501".into(),
            loudness_target_lufs: LoudnessTarget::CABLE,
            headend: None,
        },
    ];
    assert!(encode_bootstrap_frame(&bootstrap).is_err());
}
