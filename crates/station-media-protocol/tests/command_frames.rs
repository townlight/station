use station_media_protocol::{
    ChannelCommand, CommandEnvelope, LiveArmToken, LiveInputConfiguration, LiveReturnTarget,
    LoudnessTarget, OutputTransportObservation, PROTOCOL_VERSION, ProtocolError, ProtocolVersion,
    SrtPassphrase, WorkerBootstrapEnvelope, WorkerEvent, WorkerEventEnvelope,
    WorkerOutputConfiguration, decode_bootstrap_frame, decode_command_frame, decode_event_frame,
    encode_bootstrap_frame, encode_command_frame, encode_event_frame,
};
use std::num::NonZeroU16;

fn live_input() -> LiveInputConfiguration {
    LiveInputConfiguration::new(
        "62a47b7e-2a03-48d6-8703-a6e5ce986527".into(),
        "71e2acfe-5d73-491d-9a1c-242cadc8e97d".into(),
        3,
        "127.0.0.1".into(),
        NonZeroU16::new(5500).unwrap(),
    )
    .unwrap()
}

fn live_token() -> LiveArmToken {
    LiveArmToken::new("arm-token-20260820-0001".into()).unwrap()
}

fn take_live_command() -> CommandEnvelope {
    CommandEnvelope {
        version: PROTOCOL_VERSION,
        channel_id: "62a47b7e-2a03-48d6-8703-a6e5ce986527".into(),
        command_id: "71e2acfe-5d73-491d-9a1c-242cadc8e97d".into(),
        expected_sequence: 42,
        command: ChannelCommand::TakeLive {
            session_id: live_input().session_id,
            arm_token: live_token(),
        },
    }
}

#[test]
fn round_trips_live_arm_and_lifecycle_commands() {
    for command in [
        ChannelCommand::ArmLive {
            input: live_input(),
            arm_token: live_token(),
        },
        ChannelCommand::TakeLive {
            session_id: live_input().session_id.clone(),
            arm_token: live_token(),
        },
        ChannelCommand::ReturnFromLive {
            session_id: live_input().session_id.clone(),
            target: LiveReturnTarget::Program {
                asset_id: "a".repeat(64),
                media_offset_ms: 12_345,
            },
        },
        ChannelCommand::DisarmLive {
            session_id: live_input().session_id,
        },
    ] {
        let envelope = CommandEnvelope {
            version: PROTOCOL_VERSION,
            channel_id: "channel-1".into(),
            command_id: "command-1".into(),
            expected_sequence: 1,
            command,
        };
        let frame = encode_command_frame(&envelope).unwrap();
        assert_eq!(decode_command_frame(&frame).unwrap(), envelope);
    }
}

#[test]
fn rejects_invalid_live_configuration_at_constructor_and_decode() {
    assert!(
        LiveInputConfiguration::new(
            "session".into(),
            "source".into(),
            0,
            "not-an-ip".into(),
            NonZeroU16::new(1).unwrap()
        )
        .is_err()
    );
    let token = live_token();
    let json = format!(
        r#"{{"version":{{"major":1,"minor":7}},"channel_id":"channel-1","command_id":"command-1","expected_sequence":1,"command":{{"type":"arm_live","input":{{"session_id":"bad id","source_id":"source","source_revision":1,"bind_address":"not-an-ip","port":0}},"arm_token":"{}"}}}}"#,
        token.expose()
    );
    let mut frame = (json.len() as u32).to_le_bytes().to_vec();
    frame.extend_from_slice(json.as_bytes());
    assert!(matches!(
        decode_command_frame(&frame),
        Err(ProtocolError::InvalidJson(_))
    ));
}

#[test]
fn encode_rejects_invalid_live_command_fields() {
    let command = CommandEnvelope {
        version: PROTOCOL_VERSION,
        channel_id: "channel-1".into(),
        command_id: "command-1".into(),
        expected_sequence: 1,
        command: ChannelCommand::ArmLive {
            input: LiveInputConfiguration {
                session_id: "not-a-uuid".into(),
                source_id: live_input().source_id,
                source_revision: 1,
                bind_address: "127.0.0.1".into(),
                port: NonZeroU16::new(5500).unwrap(),
            },
            arm_token: live_token(),
        },
    };
    assert!(matches!(
        encode_command_frame(&command),
        Err(ProtocolError::InvalidLiveCommand(_))
    ));
}

#[test]
fn live_token_debug_is_redacted_and_validation_is_bounded() {
    assert!(LiveArmToken::new("too-short".into()).is_err());
    assert!(LiveArmToken::new("x".repeat(129)).is_err());
    assert!(LiveArmToken::new("token with spaces-123".into()).is_err());
    let token = live_token();
    let diagnostic = format!("{token:?}");
    assert!(diagnostic.contains("[REDACTED]"));
    assert!(!diagnostic.contains(token.expose()));
}

#[test]
fn round_trips_a_typed_command_in_a_length_delimited_frame() {
    let command = take_live_command();
    let frame = encode_command_frame(&command).expect("command encodes");
    assert_eq!(decode_command_frame(&frame).unwrap(), command);
}

#[test]
fn round_trips_private_worker_bootstrap_without_process_arguments() {
    let bootstrap = WorkerBootstrapEnvelope {
        version: PROTOCOL_VERSION,
        worker_id: "ae819b97-2920-434b-9d96-0b51a8ea9abb".into(),
        channel_id: "62a47b7e-2a03-48d6-8703-a6e5ce986527".into(),
        output_profile: "atsc-720p5994".into(),
        cg_image_root: r"C:\ProgramData\TownLight Station\cg-images".into(),
        proof_artifact_root: r"C:\ProgramData\TownLight Station\output-proofs".into(),
        outputs: vec![
            WorkerOutputConfiguration::UdpTs {
                sink_id: "11111111-2222-4333-8444-555555555555".into(),
                destination: "127.0.0.1:5500".into(),
                loudness_target_lufs: LoudnessTarget::STREAMING,
                headend: None,
            },
            WorkerOutputConfiguration::UdpTs {
                sink_id: "22222222-3333-4444-8555-666666666666".into(),
                destination: "127.0.0.1:5501".into(),
                loudness_target_lufs: LoudnessTarget::STREAMING,
                headend: None,
            },
            WorkerOutputConfiguration::Srt {
                sink_id: "33333333-4444-4555-8666-777777777777".into(),
                uri: "srt://127.0.0.1:5600".into(),
                passphrase: Some(SrtPassphrase::new("private-srt-proof".into()).unwrap()),
                loudness_target_lufs: LoudnessTarget::STREAMING,
            },
        ],
    };
    let frame = encode_bootstrap_frame(&bootstrap).expect("bootstrap encodes");
    assert_eq!(decode_bootstrap_frame(&frame).unwrap(), bootstrap);
    let diagnostic = format!("{bootstrap:?}");
    assert!(diagnostic.contains("[REDACTED]"));
    assert!(!diagnostic.contains("private-srt-proof"));
}

#[test]
fn image_overlay_carries_identity_and_format_but_no_path() {
    let command = CommandEnvelope {
        version: PROTOCOL_VERSION,
        channel_id: "62a47b7e-2a03-48d6-8703-a6e5ce986527".into(),
        command_id: "image-command".into(),
        expected_sequence: 1,
        command: ChannelCommand::ApplyCgSnapshot {
            snapshot: station_media_protocol::CgSnapshot {
                board_id: "board-1".into(),
                board_revision: 1,
                overlays: vec![station_media_protocol::CgOverlay {
                    slot: 0,
                    rect: station_media_protocol::CgRect {
                        x: 0,
                        y: 0,
                        width: 1000,
                        height: 1000,
                    },
                    z_index: 0,
                    content: station_media_protocol::CgOverlayContent::Image {
                        asset_id: "a".repeat(64),
                        format: station_media_protocol::CgImageFormat::Png,
                    },
                }],
            },
        },
    };
    let frame = encode_command_frame(&command).unwrap();
    let json = String::from_utf8(frame[4..].to_vec()).unwrap();
    assert!(json.contains("\"asset_id\""));
    assert!(json.contains("\"format\":\"png\""));
    assert!(!json.contains("media_path"));
    assert!(!json.contains("cg_image_root"));
    assert_eq!(decode_command_frame(&frame).unwrap(), command);
}

#[test]
fn bootstrap_rejects_relative_traversing_or_wrong_managed_image_roots() {
    for root in [
        "cg-images",
        r"C:\ProgramData\TownLight Station\..\cg-images",
        r"C:\ProgramData\TownLight Station\images",
    ] {
        let bootstrap = WorkerBootstrapEnvelope {
            version: PROTOCOL_VERSION,
            worker_id: "worker".into(),
            channel_id: "channel".into(),
            output_profile: "profile".into(),
            cg_image_root: root.into(),
            proof_artifact_root: r"C:\ProgramData\TownLight Station\output-proofs".into(),
            outputs: Vec::new(),
        };
        assert!(matches!(
            encode_bootstrap_frame(&bootstrap),
            Err(ProtocolError::InvalidManagedCgImageRoot(_))
        ));
    }
}

#[test]
fn round_trips_asset_load_and_take_commands() {
    for command in [
        ChannelCommand::LoadAsset {
            asset_id: "a".repeat(64),
            media_path: r"C:\ProgramData\TownLight Station\media\asset.ts".into(),
        },
        ChannelCommand::TakeAsset {
            asset_id: "a".repeat(64),
            media_offset_ms: 12_345,
        },
    ] {
        let envelope = CommandEnvelope {
            version: PROTOCOL_VERSION,
            channel_id: "62a47b7e-2a03-48d6-8703-a6e5ce986527".into(),
            command_id: "asset-command".into(),
            expected_sequence: 1,
            command,
        };
        let frame = encode_command_frame(&envelope).unwrap();
        assert_eq!(decode_command_frame(&frame).unwrap(), envelope);
    }
}

#[test]
fn rejects_a_command_from_an_incompatible_future_major_version() {
    let mut command = take_live_command();
    let future_major = PROTOCOL_VERSION.major + 1;
    command.version = ProtocolVersion {
        major: future_major,
        minor: 0,
    };
    let frame = encode_command_frame(&command).expect("command encodes");
    assert_eq!(
        decode_command_frame(&frame),
        Err(ProtocolError::UnsupportedMajor(future_major))
    );
}

#[test]
fn rejects_a_truncated_frame_before_deserialization() {
    let command = take_live_command();
    let mut frame = encode_command_frame(&command).expect("command encodes");
    frame.pop();
    let declared = u32::from_le_bytes(frame[..4].try_into().unwrap()) as usize;
    assert_eq!(
        decode_command_frame(&frame),
        Err(ProtocolError::LengthMismatch {
            declared,
            actual: frame.len() - 4,
        })
    );
}

#[test]
fn round_trips_worker_events_and_rejects_an_incompatible_major() {
    let event = WorkerEventEnvelope {
        version: PROTOCOL_VERSION,
        worker_id: "ae819b97-2920-434b-9d96-0b51a8ea9abb".into(),
        channel_id: "62a47b7e-2a03-48d6-8703-a6e5ce986527".into(),
        sequence: 43,
        event: WorkerEvent::OnAirChanged {
            source_kind: "live".into(),
            source_id: "studio-a".into(),
        },
    };
    let frame = encode_event_frame(&event).expect("event encodes");
    assert_eq!(decode_event_frame(&frame).unwrap(), event);

    let heartbeat = WorkerEventEnvelope {
        version: PROTOCOL_VERSION,
        worker_id: "ae819b97-2920-434b-9d96-0b51a8ea9abb".into(),
        channel_id: "62a47b7e-2a03-48d6-8703-a6e5ce986527".into(),
        sequence: 44,
        event: WorkerEvent::Heartbeat {
            monotonic_milliseconds: 5_000,
            outputs: vec![
                OutputTransportObservation {
                    output_index: 0,
                    bytes_sent_total: 4_000_000,
                    audio_buffers_total: 4_000,
                },
                OutputTransportObservation {
                    output_index: 1,
                    bytes_sent_total: 4_000_100,
                    audio_buffers_total: 4_001,
                },
            ],
        },
    };
    let frame = encode_event_frame(&heartbeat).expect("heartbeat encodes");
    assert_eq!(decode_event_frame(&frame).unwrap(), heartbeat);

    let mut incompatible = event;
    incompatible.version = ProtocolVersion { major: 9, minor: 0 };
    let frame = encode_event_frame(&incompatible).expect("event encodes");
    assert_eq!(
        decode_event_frame(&frame),
        Err(ProtocolError::UnsupportedMajor(9))
    );
}

#[test]
fn rejects_invalid_live_events_on_encode_and_decode() {
    let invalid = WorkerEventEnvelope {
        version: PROTOCOL_VERSION,
        worker_id: "worker".into(),
        channel_id: "channel".into(),
        sequence: 1,
        event: WorkerEvent::LiveOnAir {
            session_id: live_input().session_id,
            source_id: live_input().source_id,
            source_revision: 0,
        },
    };
    assert!(matches!(
        encode_event_frame(&invalid),
        Err(ProtocolError::InvalidLiveEvent(_))
    ));

    let valid = WorkerEventEnvelope {
        version: PROTOCOL_VERSION,
        worker_id: "worker".into(),
        channel_id: "channel".into(),
        sequence: 1,
        event: WorkerEvent::LiveReturned {
            session_id: live_input().session_id,
            source_kind: "asset".into(),
            source_id: "a".repeat(64),
        },
    };
    let frame = encode_event_frame(&valid).unwrap();
    let mut json = String::from_utf8(frame[4..].to_vec()).unwrap();
    json = json.replace("62a47b7e-2a03-48d6-8703-a6e5ce986527", "bad-session");
    let mut frame = (json.len() as u32).to_le_bytes().to_vec();
    frame.extend_from_slice(json.as_bytes());
    assert!(matches!(
        decode_event_frame(&frame),
        Err(ProtocolError::InvalidLiveEvent(_))
    ));
}
