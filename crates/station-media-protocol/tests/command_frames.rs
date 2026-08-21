use station_media_protocol::{
    ChannelCommand, CommandEnvelope, PROTOCOL_VERSION, ProtocolError, ProtocolVersion, WorkerEvent,
    WorkerEventEnvelope, decode_command_frame, decode_event_frame, encode_command_frame,
    encode_event_frame,
};

fn take_live_command() -> CommandEnvelope {
    CommandEnvelope {
        version: PROTOCOL_VERSION,
        channel_id: "62a47b7e-2a03-48d6-8703-a6e5ce986527".into(),
        command_id: "71e2acfe-5d73-491d-9a1c-242cadc8e97d".into(),
        expected_sequence: 42,
        command: ChannelCommand::TakeLive {
            arm_token: "arm-20260820-0001".into(),
        },
    }
}

#[test]
fn round_trips_a_typed_command_in_a_length_delimited_frame() {
    let command = take_live_command();
    let frame = encode_command_frame(&command).expect("command encodes");
    assert_eq!(decode_command_frame(&frame).unwrap(), command);
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
    command.version = ProtocolVersion { major: 2, minor: 0 };
    let frame = encode_command_frame(&command).expect("command encodes");
    assert_eq!(
        decode_command_frame(&frame),
        Err(ProtocolError::UnsupportedMajor(2))
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

    let mut incompatible = event;
    incompatible.version = ProtocolVersion { major: 9, minor: 0 };
    let frame = encode_event_frame(&incompatible).expect("event encodes");
    assert_eq!(
        decode_event_frame(&frame),
        Err(ProtocolError::UnsupportedMajor(9))
    );
}
