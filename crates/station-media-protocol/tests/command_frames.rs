use station_media_protocol::{
    CaptionCue, CgImageFormat, ChannelCommand, CommandEnvelope, FallbackToneKind, LiveArmToken,
    LiveInputConfiguration, LiveReturnTarget, LoudnessTarget, OutputTransportObservation,
    PROTOCOL_VERSION, ProtocolError, ProtocolVersion, SrtPassphrase, WorkerBootstrapEnvelope,
    WorkerCaptionMode, WorkerCaptionPolicy, WorkerEvent, WorkerEventEnvelope, WorkerFallbackSlate,
    WorkerFallbackTone, WorkerOutputConfiguration, decode_bootstrap_frame, decode_command_frame,
    decode_event_frame, decode_persisted_event_frame, encode_bootstrap_frame, encode_command_frame,
    encode_event_frame,
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

fn scheduled_take_command() -> CommandEnvelope {
    CommandEnvelope {
        version: PROTOCOL_VERSION,
        channel_id: "62a47b7e-2a03-48d6-8703-a6e5ce986527".into(),
        command_id: "71e2acfe-5d73-491d-9a1c-242cadc8e97d".into(),
        expected_sequence: 42,
        command: ChannelCommand::TakeScheduledAsset {
            report_id: "scheduled-report-1".into(),
            schedule_item_id: "b5c2a5d5-c0b3-4d8b-9cb6-7c3a8f1d2e44".into(),
            schedule_item_revision: 7,
            asset_id: "a".repeat(64),
            media_offset_ms: 12_345,
        },
    }
}

fn scheduled_asset_on_air_event() -> WorkerEventEnvelope {
    WorkerEventEnvelope {
        version: PROTOCOL_VERSION,
        worker_id: "ae819b97-2920-434b-9d96-0b51a8ea9abb".into(),
        channel_id: "62a47b7e-2a03-48d6-8703-a6e5ce986527".into(),
        sequence: 43,
        event: WorkerEvent::ScheduledAssetOnAir {
            report_id: "scheduled-report-1".into(),
            schedule_item_id: "b5c2a5d5-c0b3-4d8b-9cb6-7c3a8f1d2e44".into(),
            schedule_item_revision: 7,
            asset_id: "a".repeat(64),
            media_offset_ms: 12_345,
            worker_boot_id: "f2e6f9a1-6a5d-4de2-9a2b-8f4c1d7e3b55".into(),
            actual_on_air_at_unix_ms: 1_767_225_600_000,
        },
    }
}

fn replace_frame_json(frame: &[u8], from: &str, to: &str) -> Vec<u8> {
    let json = String::from_utf8(frame[4..].to_vec()).unwrap();
    let tampered = json.replacen(from, to, 1);
    assert_ne!(tampered, json);
    let mut result = (tampered.len() as u32).to_le_bytes().to_vec();
    result.extend_from_slice(tampered.as_bytes());
    result
}

#[test]
fn round_trips_scheduled_take_command_and_on_air_event() {
    let command = scheduled_take_command();
    let frame = encode_command_frame(&command).unwrap();
    assert_eq!(decode_command_frame(&frame).unwrap(), command);

    let event = scheduled_asset_on_air_event();
    let frame = encode_event_frame(&event).unwrap();
    assert_eq!(decode_event_frame(&frame).unwrap(), event);
}

#[test]
fn protocol_requires_the_tone_capability_major() {
    // A worker that cannot enforce the finite fallback-tone contract must not
    // participate in a mixed-version station.
    assert_eq!(PROTOCOL_VERSION.major, 11);
}

fn fallback_tone() -> WorkerFallbackTone {
    WorkerFallbackTone {
        activation_id: "1c63ef7e-6e4b-4fa6-b5d8-c15e2b735c05".into(),
        authority_revision: 7,
        kind: FallbackToneKind::Test,
        frequency_hz: 1_000,
        level_milli_dbfs: -20_000,
        duration_ms: 1_000,
    }
}

fn fallback_tone_activation_event() -> WorkerEventEnvelope {
    WorkerEventEnvelope {
        version: PROTOCOL_VERSION,
        worker_id: "ae819b97-2920-434b-9d96-0b51a8ea9abb".into(),
        channel_id: "62a47b7e-2a03-48d6-8703-a6e5ce986527".into(),
        sequence: 43,
        event: WorkerEvent::FallbackToneActivated {
            activation_id: fallback_tone().activation_id,
            authority_revision: 7,
            duration_ms: 1_000,
            tone: Some(fallback_tone()),
            activated_at_monotonic_ms: Some(500),
        },
    }
}

#[test]
fn current_major_requires_complete_fallback_tone_audit_fields() {
    let mut activation = fallback_tone_activation_event();
    if let WorkerEvent::FallbackToneActivated {
        tone,
        activated_at_monotonic_ms,
        ..
    } = &mut activation.event
    {
        *tone = None;
        *activated_at_monotonic_ms = None;
    }
    assert!(matches!(
        encode_event_frame(&activation),
        Err(ProtocolError::InvalidFallbackTone(_))
    ));
    let payload = serde_json::to_vec(&activation).unwrap();
    let mut raw_activation = (payload.len() as u32).to_le_bytes().to_vec();
    raw_activation.extend_from_slice(&payload);
    assert!(matches!(
        decode_event_frame(&raw_activation),
        Err(ProtocolError::InvalidFallbackTone(_))
    ));

    let deactivation = WorkerEventEnvelope {
        event: WorkerEvent::FallbackToneDeactivated {
            activation_id: fallback_tone().activation_id,
            authority_revision: 7,
            deactivation: None,
        },
        ..fallback_tone_activation_event()
    };
    assert!(matches!(
        encode_event_frame(&deactivation),
        Err(ProtocolError::InvalidFallbackTone(_))
    ));
}

#[test]
fn immediately_prior_persisted_major_may_omit_fallback_tone_audit_fields() {
    let mut legacy = fallback_tone_activation_event();
    legacy.version.major -= 1;
    if let WorkerEvent::FallbackToneActivated {
        tone,
        activated_at_monotonic_ms,
        ..
    } = &mut legacy.event
    {
        *tone = None;
        *activated_at_monotonic_ms = None;
    }
    let payload = serde_json::to_vec(&legacy).unwrap();
    let mut frame = (payload.len() as u32).to_le_bytes().to_vec();
    frame.extend_from_slice(&payload);

    assert_eq!(decode_persisted_event_frame(&frame).unwrap(), legacy);
}

#[test]
fn fallback_tone_commands_are_typed_bounded_and_versioned() {
    let activation = CommandEnvelope {
        version: PROTOCOL_VERSION,
        channel_id: "62a47b7e-2a03-48d6-8703-a6e5ce986527".into(),
        command_id: "71e2acfe-5d73-491d-9a1c-242cadc8e97d".into(),
        expected_sequence: 42,
        command: ChannelCommand::ActivateFallbackTone {
            tone: fallback_tone(),
        },
    };
    let frame = encode_command_frame(&activation).unwrap();
    assert_eq!(decode_command_frame(&frame).unwrap(), activation);

    let deactivation = CommandEnvelope {
        command: ChannelCommand::DeactivateFallbackTone {
            activation_id: fallback_tone().activation_id,
            authority_revision: 7,
        },
        ..activation
    };
    assert_eq!(
        decode_command_frame(&encode_command_frame(&deactivation).unwrap()).unwrap(),
        deactivation
    );
}

#[test]
fn fallback_tone_rejects_unbounded_or_unsafe_values() {
    for (frequency_hz, level_milli_dbfs, duration_ms) in [
        (439, -20_000, 1_000),
        (1_000, -11_999, 1_000),
        (1_000, -20_000, 10_001),
    ] {
        let mut tone = fallback_tone();
        tone.frequency_hz = frequency_hz;
        tone.level_milli_dbfs = level_milli_dbfs;
        tone.duration_ms = duration_ms;
        let command = CommandEnvelope {
            version: PROTOCOL_VERSION,
            channel_id: "62a47b7e-2a03-48d6-8703-a6e5ce986527".into(),
            command_id: "71e2acfe-5d73-491d-9a1c-242cadc8e97d".into(),
            expected_sequence: 42,
            command: ChannelCommand::ActivateFallbackTone { tone },
        };
        assert!(matches!(
            encode_command_frame(&command),
            Err(ProtocolError::InvalidFallbackTone(_))
        ));
    }
}

#[test]
fn decode_rejects_hostile_raw_fallback_tone_frames() {
    let command = CommandEnvelope {
        version: PROTOCOL_VERSION,
        channel_id: "62a47b7e-2a03-48d6-8703-a6e5ce986527".into(),
        command_id: "71e2acfe-5d73-491d-9a1c-242cadc8e97d".into(),
        expected_sequence: 42,
        command: ChannelCommand::ActivateFallbackTone {
            tone: fallback_tone(),
        },
    };
    let frame = encode_command_frame(&command).unwrap();
    for (from, to) in [
        ("\"frequency_hz\":1000", "\"frequency_hz\":1501"),
        ("\"level_milli_dbfs\":-20000", "\"level_milli_dbfs\":-11999"),
        ("\"duration_ms\":1000", "\"duration_ms\":10001"),
    ] {
        assert!(matches!(
            decode_command_frame(&replace_frame_json(&frame, from, to)),
            Err(ProtocolError::InvalidFallbackTone(_))
        ));
    }
}

#[test]
fn rejects_invalid_scheduled_take_command_fields() {
    let mut cases = Vec::new();

    let mut invalid = scheduled_take_command();
    if let ChannelCommand::TakeScheduledAsset {
        schedule_item_revision,
        ..
    } = &mut invalid.command
    {
        *schedule_item_revision = 0;
    }
    cases.push(invalid);

    let mut invalid = scheduled_take_command();
    if let ChannelCommand::TakeScheduledAsset {
        schedule_item_revision,
        ..
    } = &mut invalid.command
    {
        *schedule_item_revision = i64::MAX as u64 + 1;
    }
    cases.push(invalid);

    let mut invalid = scheduled_take_command();
    if let ChannelCommand::TakeScheduledAsset { asset_id, .. } = &mut invalid.command {
        asset_id.replace_range(..1, "A");
    }
    cases.push(invalid);

    let mut invalid = scheduled_take_command();
    if let ChannelCommand::TakeScheduledAsset {
        schedule_item_id, ..
    } = &mut invalid.command
    {
        *schedule_item_id = "not-a-uuid".into();
    }
    cases.push(invalid);

    let oversized_report_id = "r".repeat(201);
    for report_id in ["", "report\ncontrol", oversized_report_id.as_str()] {
        let mut invalid = scheduled_take_command();
        if let ChannelCommand::TakeScheduledAsset {
            report_id: value, ..
        } = &mut invalid.command
        {
            *value = report_id.into();
        }
        cases.push(invalid);
    }

    let mut invalid = scheduled_take_command();
    invalid.channel_id = "not-a-uuid".into();
    cases.push(invalid);

    for invalid in cases {
        assert!(matches!(
            encode_command_frame(&invalid),
            Err(ProtocolError::InvalidScheduledTake(_))
        ));
    }
}

#[test]
fn rejects_invalid_scheduled_asset_on_air_event_fields() {
    let mut invalid = scheduled_asset_on_air_event();
    if let WorkerEvent::ScheduledAssetOnAir { worker_boot_id, .. } = &mut invalid.event {
        *worker_boot_id = "malformed-boot-id".into();
    }
    assert!(matches!(
        encode_event_frame(&invalid),
        Err(ProtocolError::InvalidScheduledTake(_))
    ));

    let mut invalid = scheduled_asset_on_air_event();
    if let WorkerEvent::ScheduledAssetOnAir {
        actual_on_air_at_unix_ms,
        ..
    } = &mut invalid.event
    {
        *actual_on_air_at_unix_ms = -1;
    }
    assert!(matches!(
        encode_event_frame(&invalid),
        Err(ProtocolError::InvalidScheduledTake(_))
    ));
}

#[test]
fn decode_rejects_tampered_scheduled_revision_and_actual_time() {
    let command = scheduled_take_command();
    let frame = encode_command_frame(&command).unwrap();
    let tampered = replace_frame_json(
        &frame,
        "schedule_item_revision\":7",
        "schedule_item_revision\":0",
    );
    assert!(matches!(
        decode_command_frame(&tampered),
        Err(ProtocolError::InvalidScheduledTake(_))
    ));

    let event = scheduled_asset_on_air_event();
    let frame = encode_event_frame(&event).unwrap();
    let tampered = replace_frame_json(
        &frame,
        "actual_on_air_at_unix_ms\":1767225600000",
        "actual_on_air_at_unix_ms\":-1",
    );
    assert!(matches!(
        decode_event_frame(&tampered),
        Err(ProtocolError::InvalidScheduledTake(_))
    ));
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
        fallback_slate: Some(WorkerFallbackSlate {
            asset_id: "a".repeat(64),
            format: CgImageFormat::Png,
        }),
        caption_policy: Some(WorkerCaptionPolicy {
            mode: WorkerCaptionMode::Cea708,
            service_number: 2,
            language: "es-US".into(),
            revision: 4,
        }),
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
fn bootstrap_rejects_invalid_caption_policy() {
    let mut bootstrap = WorkerBootstrapEnvelope {
        version: PROTOCOL_VERSION,
        worker_id: "worker".into(),
        channel_id: "channel".into(),
        output_profile: "proof-360p30".into(),
        cg_image_root: r"C:\ProgramData\TownLight Station\cg-images".into(),
        proof_artifact_root: r"C:\ProgramData\TownLight Station\output-proofs".into(),
        fallback_slate: None,
        caption_policy: Some(WorkerCaptionPolicy {
            mode: WorkerCaptionMode::Cea708,
            service_number: 64,
            language: "en-US".into(),
            revision: 1,
        }),
        outputs: Vec::new(),
    };
    assert!(matches!(
        encode_bootstrap_frame(&bootstrap),
        Err(ProtocolError::InvalidCaptionPolicy(_))
    ));
    bootstrap.caption_policy.as_mut().unwrap().service_number = 1;
    bootstrap.caption_policy.as_mut().unwrap().language = "not a language".into();
    assert!(matches!(
        encode_bootstrap_frame(&bootstrap),
        Err(ProtocolError::InvalidCaptionPolicy(_))
    ));
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
fn bootstrap_rejects_a_noncanonical_fallback_slate_identity() {
    let bootstrap = WorkerBootstrapEnvelope {
        version: PROTOCOL_VERSION,
        worker_id: "worker".into(),
        channel_id: "channel".into(),
        output_profile: "proof-360p30".into(),
        cg_image_root: r"C:\ProgramData\TownLight Station\cg-images".into(),
        proof_artifact_root: r"C:\ProgramData\TownLight Station\output-proofs".into(),
        fallback_slate: Some(WorkerFallbackSlate {
            asset_id: "A".repeat(64),
            format: CgImageFormat::Png,
        }),
        caption_policy: None,
        outputs: Vec::new(),
    };
    assert!(matches!(
        encode_bootstrap_frame(&bootstrap),
        Err(ProtocolError::InvalidFallbackSlate(_))
    ));
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
            fallback_slate: None,
            caption_policy: None,
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
fn round_trips_a_bounded_typed_cea708_caption_cue() {
    let envelope = CommandEnvelope {
        version: PROTOCOL_VERSION,
        channel_id: "62a47b7e-2a03-48d6-8703-a6e5ce986527".into(),
        command_id: "caption-command".into(),
        expected_sequence: 7,
        command: ChannelCommand::PushCaptionCue {
            cue: CaptionCue {
                cue_id: "4fefcd4e-cfa1-4f62-a810-3d4fa53d2cb6".into(),
                start_after_ms: 500,
                duration_ms: 2_000,
                text: "TownLight CEA-708 proof".into(),
            },
        },
    };
    let frame = encode_command_frame(&envelope).expect("caption cue encodes");
    assert_eq!(decode_command_frame(&frame).unwrap(), envelope);
}

#[test]
fn rejects_invalid_caption_cue_identity_timing_and_text() {
    for cue in [
        CaptionCue {
            cue_id: "not-a-uuid".into(),
            start_after_ms: 500,
            duration_ms: 2_000,
            text: "caption".into(),
        },
        CaptionCue {
            cue_id: "4fefcd4e-cfa1-4f62-a810-3d4fa53d2cb6".into(),
            start_after_ms: 60_001,
            duration_ms: 2_000,
            text: "caption".into(),
        },
        CaptionCue {
            cue_id: "4fefcd4e-cfa1-4f62-a810-3d4fa53d2cb6".into(),
            start_after_ms: 500,
            duration_ms: 0,
            text: "caption".into(),
        },
        CaptionCue {
            cue_id: "4fefcd4e-cfa1-4f62-a810-3d4fa53d2cb6".into(),
            start_after_ms: 500,
            duration_ms: 2_000,
            text: "\n".into(),
        },
    ] {
        let envelope = CommandEnvelope {
            version: PROTOCOL_VERSION,
            channel_id: "channel-1".into(),
            command_id: "caption-command".into(),
            expected_sequence: 1,
            command: ChannelCommand::PushCaptionCue { cue },
        };
        assert!(matches!(
            encode_command_frame(&envelope),
            Err(ProtocolError::InvalidCaptionCue(_))
        ));
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
fn rejects_a_pre_caption_clear_major_version() {
    let mut command = take_live_command();
    command.version = ProtocolVersion { major: 7, minor: 0 };
    let frame = encode_command_frame(&command).expect("command encodes");
    assert_eq!(
        decode_command_frame(&frame),
        Err(ProtocolError::UnsupportedMajor(7))
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

    let caption = WorkerEventEnvelope {
        version: PROTOCOL_VERSION,
        worker_id: "ae819b97-2920-434b-9d96-0b51a8ea9abb".into(),
        channel_id: "62a47b7e-2a03-48d6-8703-a6e5ce986527".into(),
        sequence: 45,
        event: WorkerEvent::CaptionCueAccepted {
            cue_id: "4fefcd4e-cfa1-4f62-a810-3d4fa53d2cb6".into(),
        },
    };
    let frame = encode_event_frame(&caption).expect("caption event encodes");
    assert_eq!(decode_event_frame(&frame).unwrap(), caption);

    let mut incompatible = event;
    let future_major = PROTOCOL_VERSION.major + 1;
    incompatible.version = ProtocolVersion {
        major: future_major,
        minor: 0,
    };
    let frame = encode_event_frame(&incompatible).expect("event encodes");
    assert_eq!(
        decode_event_frame(&frame),
        Err(ProtocolError::UnsupportedMajor(future_major))
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

#[test]
fn rejects_an_invalid_caption_acknowledgment_identity() {
    let invalid = WorkerEventEnvelope {
        version: PROTOCOL_VERSION,
        worker_id: "worker".into(),
        channel_id: "channel".into(),
        sequence: 1,
        event: WorkerEvent::CaptionCueAccepted {
            cue_id: "not-a-uuid".into(),
        },
    };
    assert!(matches!(
        encode_event_frame(&invalid),
        Err(ProtocolError::InvalidCaptionCue(_))
    ));
}
