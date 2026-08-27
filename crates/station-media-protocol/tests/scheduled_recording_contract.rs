use station_media_protocol::{
    ChannelCommand, CommandEnvelope, LoudnessTarget, MAX_SCHEDULED_RECORDING_BYTES,
    MAX_SCHEDULED_RECORDING_DURATION_MS, MIN_SCHEDULED_RECORDING_DURATION_MS, PROTOCOL_VERSION,
    WorkerBootstrapEnvelope, WorkerEvent, WorkerEventEnvelope, WorkerOutputConfiguration,
    decode_bootstrap_frame, decode_command_frame, decode_event_frame, encode_bootstrap_frame,
    encode_command_frame, encode_event_frame,
};

const RECORDING_ID: &str = "11111111-2222-4333-8444-555555555555";
const SINK_ID: &str = "22222222-3333-4444-8555-666666666666";
const HASH: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

fn command(command: ChannelCommand) -> CommandEnvelope {
    CommandEnvelope {
        version: PROTOCOL_VERSION,
        channel_id: "channel-1".into(),
        command_id: "command-1".into(),
        expected_sequence: 8,
        command,
    }
}

#[test]
fn scheduled_recording_commands_are_separate_and_bounded_for_a_full_day() {
    assert_eq!(MIN_SCHEDULED_RECORDING_DURATION_MS, 3_000);
    assert_eq!(MAX_SCHEDULED_RECORDING_DURATION_MS, 86_400_000);
    assert_eq!(MAX_SCHEDULED_RECORDING_BYTES, 128 * 1024 * 1024 * 1024);

    for value in [
        command(ChannelCommand::StartScheduledRecording {
            recording_id: RECORDING_ID.into(),
            output_index: 0,
            sink_id: SINK_ID.into(),
            duration_ms: MAX_SCHEDULED_RECORDING_DURATION_MS,
            max_bytes: MAX_SCHEDULED_RECORDING_BYTES,
        }),
        command(ChannelCommand::PollScheduledRecording {
            recording_id: RECORDING_ID.into(),
        }),
        command(ChannelCommand::AbortScheduledRecording {
            recording_id: RECORDING_ID.into(),
        }),
    ] {
        assert_eq!(
            decode_command_frame(&encode_command_frame(&value).unwrap()).unwrap(),
            value
        );
    }
}

#[test]
fn scheduled_recording_replies_bind_the_exact_worker_graph_and_artifact() {
    let events = [
        WorkerEvent::ScheduledRecordingStarted {
            recording_id: RECORDING_ID.into(),
            output_index: 0,
            sink_id: SINK_ID.into(),
            duration_ms: 3_000,
            max_bytes: 8_000_000,
            artifact_relative_path: "recordings/11111111-2222-4333-8444-555555555555.ts".into(),
            worker_instance_id: "worker-a".into(),
            graph_revision: 12,
        },
        WorkerEvent::ScheduledRecordingPending {
            recording_id: RECORDING_ID.into(),
            artifact_relative_path: "recordings/11111111-2222-4333-8444-555555555555.ts".into(),
            worker_instance_id: "worker-a".into(),
            graph_revision: 12,
        },
        WorkerEvent::ScheduledRecordingReady {
            recording_id: RECORDING_ID.into(),
            output_index: 0,
            sink_id: SINK_ID.into(),
            artifact_relative_path: "recordings/11111111-2222-4333-8444-555555555555.ts".into(),
            worker_instance_id: "worker-a".into(),
            graph_revision: 12,
            size_bytes: 7_000_000,
            capture_sha256: HASH.into(),
            duration_ms: 3_000,
        },
        WorkerEvent::ScheduledRecordingFailed {
            recording_id: RECORDING_ID.into(),
            artifact_relative_path: "recordings/11111111-2222-4333-8444-555555555555.ts".into(),
            worker_instance_id: "worker-a".into(),
            graph_revision: 12,
            code: "capture_interrupted".into(),
            message: "capture stopped before its deadline".into(),
        },
    ];
    for (index, event) in events.into_iter().enumerate() {
        let envelope = WorkerEventEnvelope {
            version: PROTOCOL_VERSION,
            worker_id: "worker-a".into(),
            channel_id: "channel-1".into(),
            sequence: index as u64 + 1,
            event,
        };
        assert_eq!(
            decode_event_frame(&encode_event_frame(&envelope).unwrap()).unwrap(),
            envelope
        );
    }
}

#[test]
fn scheduled_recording_rejects_invalid_identity_duration_bytes_and_ready_facts() {
    let mut start = command(ChannelCommand::StartScheduledRecording {
        recording_id: "not-a-uuid".into(),
        output_index: 0,
        sink_id: SINK_ID.into(),
        duration_ms: MIN_SCHEDULED_RECORDING_DURATION_MS,
        max_bytes: 1,
    });
    assert!(encode_command_frame(&start).is_err());

    start.command = ChannelCommand::StartScheduledRecording {
        recording_id: RECORDING_ID.into(),
        output_index: 0,
        sink_id: SINK_ID.into(),
        duration_ms: MAX_SCHEDULED_RECORDING_DURATION_MS + 1,
        max_bytes: MAX_SCHEDULED_RECORDING_BYTES + 1,
    };
    assert!(encode_command_frame(&start).is_err());

    let invalid = WorkerEventEnvelope {
        version: PROTOCOL_VERSION,
        worker_id: "worker-a".into(),
        channel_id: "channel-1".into(),
        sequence: 1,
        event: WorkerEvent::ScheduledRecordingReady {
            recording_id: RECORDING_ID.into(),
            output_index: 0,
            sink_id: SINK_ID.into(),
            artifact_relative_path: "../outside.ts".into(),
            worker_instance_id: "worker-a".into(),
            graph_revision: 12,
            size_bytes: 0,
            capture_sha256: "bad".into(),
            duration_ms: 0,
        },
    };
    assert!(encode_event_frame(&invalid).is_err());
}

#[test]
fn scheduled_recording_start_must_bind_the_exact_bootstrap_output() {
    let bootstrap = WorkerBootstrapEnvelope {
        version: PROTOCOL_VERSION,
        worker_id: "worker-a".into(),
        channel_id: "channel-1".into(),
        output_profile: "proof-360p30".into(),
        cg_image_root: r"C:\ProgramData\TownLight Station\cg-images".into(),
        proof_artifact_root: r"C:\ProgramData\TownLight Station\output-proofs".into(),
        fallback_slate: None,
        caption_policy: None,
        outputs: vec![WorkerOutputConfiguration::UdpTs {
            sink_id: SINK_ID.into(),
            destination: "127.0.0.1:5500".into(),
            loudness_target_lufs: LoudnessTarget::CABLE,
            headend: None,
        }],
    };
    let decoded = decode_bootstrap_frame(&encode_bootstrap_frame(&bootstrap).unwrap()).unwrap();
    decoded
        .validate_scheduled_recording_binding(0, SINK_ID)
        .unwrap();
    assert!(
        decoded
            .validate_scheduled_recording_binding(0, "33333333-4444-4555-8666-777777777777")
            .is_err()
    );
    assert!(
        decoded
            .validate_scheduled_recording_binding(1, SINK_ID)
            .is_err()
    );
}
