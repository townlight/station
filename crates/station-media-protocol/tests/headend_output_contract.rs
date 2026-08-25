use station_media_protocol::{
    LoudnessTarget, PROTOCOL_VERSION, WorkerBootstrapEnvelope, WorkerHeadendProfileId,
    WorkerHeadendTransportContract, WorkerOutputConfiguration, decode_bootstrap_frame,
    encode_bootstrap_frame,
};

const SINK_ID: &str = "11111111-2222-4333-8444-555555555555";

fn bootstrap(
    profile_id: WorkerHeadendProfileId,
    output_profile: &str,
    target: LoudnessTarget,
    muxrate_kbps: u32,
    packet_size_bytes: u16,
) -> WorkerBootstrapEnvelope {
    WorkerBootstrapEnvelope {
        version: PROTOCOL_VERSION,
        worker_id: "worker-a".into(),
        channel_id: "channel-a".into(),
        output_profile: output_profile.into(),
        cg_image_root: r"C:\ProgramData\TownLight Station\cg-images".into(),
        proof_artifact_root: r"C:\ProgramData\TownLight Station\output-proofs".into(),
        fallback_slate: None,
        caption_policy: None,
        outputs: vec![WorkerOutputConfiguration::UdpTs {
            sink_id: SINK_ID.into(),
            destination: "192.0.2.40:5500".into(),
            loudness_target_lufs: target,
            headend: Some(WorkerHeadendTransportContract {
                profile_id,
                muxrate_kbps,
                packet_size_bytes,
            }),
        }],
    }
}

#[test]
fn round_trips_both_locally_executable_headend_contracts() {
    for profile_id in [
        WorkerHeadendProfileId::GenericUdpSpts,
        WorkerHeadendProfileId::TelvueHypercasterIp,
    ] {
        let bootstrap = bootstrap(
            profile_id,
            "atsc-720p5994",
            LoudnessTarget::CABLE,
            8_000,
            1_316,
        );
        let frame = encode_bootstrap_frame(&bootstrap).expect("runnable contract encodes");
        assert_eq!(decode_bootstrap_frame(&frame).unwrap(), bootstrap);
    }
}

#[test]
fn rejects_headend_contracts_that_do_not_match_the_executable_transport() {
    let invalid = [
        bootstrap(
            WorkerHeadendProfileId::GenericUdpSpts,
            "proof-360p30",
            LoudnessTarget::CABLE,
            8_000,
            1_316,
        ),
        bootstrap(
            WorkerHeadendProfileId::GenericUdpSpts,
            "atsc-720p5994",
            LoudnessTarget::STREAMING,
            8_000,
            1_316,
        ),
        bootstrap(
            WorkerHeadendProfileId::GenericUdpSpts,
            "atsc-720p5994",
            LoudnessTarget::CABLE,
            8_001,
            1_316,
        ),
        bootstrap(
            WorkerHeadendProfileId::GenericUdpSpts,
            "atsc-720p5994",
            LoudnessTarget::CABLE,
            8_000,
            1_312,
        ),
    ];

    for bootstrap in invalid {
        assert!(encode_bootstrap_frame(&bootstrap).is_err());
    }
}

#[test]
fn rejects_headend_destinations_outside_the_runnable_profile_contract() {
    let mut multicast = bootstrap(
        WorkerHeadendProfileId::GenericUdpSpts,
        "atsc-720p5994",
        LoudnessTarget::CABLE,
        8_000,
        1_316,
    );
    let WorkerOutputConfiguration::UdpTs { destination, .. } = &mut multicast.outputs[0] else {
        unreachable!()
    };
    *destination = "239.1.1.1:5500".into();
    assert!(encode_bootstrap_frame(&multicast).is_err());

    let mut telvue_privileged_port = bootstrap(
        WorkerHeadendProfileId::TelvueHypercasterIp,
        "atsc-720p5994",
        LoudnessTarget::CABLE,
        8_000,
        1_316,
    );
    let WorkerOutputConfiguration::UdpTs { destination, .. } =
        &mut telvue_privileged_port.outputs[0]
    else {
        unreachable!()
    };
    *destination = "192.0.2.40:80".into();
    assert!(encode_bootstrap_frame(&telvue_privileged_port).is_err());

    let mut generic_zero_port = bootstrap(
        WorkerHeadendProfileId::GenericUdpSpts,
        "atsc-720p5994",
        LoudnessTarget::CABLE,
        8_000,
        1_316,
    );
    let WorkerOutputConfiguration::UdpTs { destination, .. } = &mut generic_zero_port.outputs[0]
    else {
        unreachable!()
    };
    *destination = "192.0.2.40:0".into();
    assert!(encode_bootstrap_frame(&generic_zero_port).is_err());
}

#[test]
fn rejects_non_runnable_profile_ids_and_keeps_ordinary_outputs_compatible() {
    let unsupported = serde_json::json!({
        "profile_id": "comcast-mtd-hd",
        "muxrate_kbps": 8000,
        "packet_size_bytes": 1316
    });
    assert!(serde_json::from_value::<WorkerHeadendTransportContract>(unsupported).is_err());

    let ordinary: WorkerOutputConfiguration = serde_json::from_value(serde_json::json!({
        "kind": "udp-ts",
        "sink_id": SINK_ID,
        "destination": "127.0.0.1:5500",
        "loudness_target_lufs": -16.0
    }))
    .expect("existing UDP bootstraps must remain valid without a headend contract");
    assert!(matches!(
        ordinary,
        WorkerOutputConfiguration::UdpTs { headend: None, .. }
    ));

    let srt: WorkerOutputConfiguration = serde_json::from_value(serde_json::json!({
        "kind": "srt",
        "sink_id": "22222222-3333-4444-8555-666666666666",
        "uri": "srt://127.0.0.1:5600",
        "loudness_target_lufs": -16.0
    }))
    .expect("existing SRT bootstraps must remain valid");
    assert!(matches!(srt, WorkerOutputConfiguration::Srt { .. }));
}
