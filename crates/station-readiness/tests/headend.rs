use station_readiness::{
    BackupProbeInput, CapabilitySupport, ChannelOutputRuntimeProbe, ClockProbeInput,
    ClockSyncStatus, EngineProbeInput, ExternalReceiverState, HardwareProbeInput,
    HeadendChannelProbeInput, HeadendProbeInput, HeadendProfileProbeInput, HeadendSinkKind,
    HeadendSinkProbeInput, PegReadinessColor, ReadinessState, ReleaseProbeInput,
    StationBoxProbeInput, derive_headend_readiness, derive_station_box_profile,
};

const STATION_ID: &str = "3f5f721f-96c7-48b1-b061-1bf1ad1e62c2";
const CHANNEL_ID: &str = "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e";
const SINK_ID: &str = "22222222-3333-4444-8555-666666666666";
const NOW: i64 = 1_777_000_000_000;

fn station_box() -> station_readiness::StationBoxProfile {
    let probe = StationBoxProbeInput {
        generated_at_unix_ms: NOW,
        probe_collected_at_unix_ms: NOW,
        max_probe_age_ms: 300_000,
        product_version: "0.1.0".into(),
        source_commit: Some("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into()),
        station_id: Some(STATION_ID.into()),
        station_name: Some("KTLT Community Television".into()),
        timezone: Some("America/Denver".into()),
        hardware: HardwareProbeInput {
            logical_cpus: 16,
            ram_total_gb: 32.0,
            media_disk_free_gb: 500.0,
        },
        engine: EngineProbeInput {
            gstreamer_version: Some("1.28.6".into()),
            required_factories: vec![
                "input-selector".into(),
                "mpegtsmux".into(),
                "udpsink".into(),
            ],
            available_factories: vec![
                "input-selector".into(),
                "mpegtsmux".into(),
                "udpsink".into(),
            ],
            available_encoders: vec!["openh264enc".into()],
            playout: ReadinessState::Pass,
            runtime: ReadinessState::Pass,
            output: ReadinessState::Pass,
            udp: CapabilitySupport::Supported,
            srt: CapabilitySupport::Supported,
            sdi: CapabilitySupport::Unimplemented,
            ndi: CapabilitySupport::Unimplemented,
            interlaced: CapabilitySupport::Unimplemented,
        },
        clock: ClockProbeInput {
            configured_timezone: Some("America/Denver".into()),
            utc_offset_seconds: Some(-21_600),
            sync_status: ClockSyncStatus::Synchronized,
        },
        network: station_readiness::NetworkProbeInput {
            state: ReadinessState::Pass,
            hostname: Some("station-one".into()),
            interface_up: true,
            dns_working: Some(true),
            outbound_udp: CapabilitySupport::Supported,
            outbound_srt: CapabilitySupport::Supported,
        },
        backup: BackupProbeInput {
            state: ReadinessState::Pass,
            destination_configured: true,
            writable: Some(true),
            detail: Some("Synchronized write/read/delete probe passed.".into()),
        },
        release: ReleaseProbeInput {
            state: ReadinessState::Pass,
            candidate_verified: true,
            update_available: false,
            detail: Some("Installed receipt matches the active slot.".into()),
        },
    };
    derive_station_box_profile(probe).expect("fixture StationBox profile must be valid")
}

fn profile() -> HeadendProfileProbeInput {
    HeadendProfileProbeInput {
        profile_id: "atsc-720p5994".into(),
        width: 1_280,
        height: 720,
        frame_rate_numerator: 60_000,
        frame_rate_denominator: 1_001,
        interlaced: false,
        video_bitrate_bps: 6_000_000,
        audio_bitrate_bps: 192_000,
        transport_bitrate_bps: 8_000_000,
        audio_sample_rate_hz: 48_000,
        loudness_target_lufs: -24.0,
    }
}

fn runtime(state: ReadinessState, heartbeat_age_ms: Option<i64>) -> ChannelOutputRuntimeProbe {
    ChannelOutputRuntimeProbe {
        state,
        heartbeat_age_ms,
        evidence: "worker heartbeat proved all enabled local branches advanced".into(),
    }
}

fn udp_sink(receiver: ExternalReceiverState) -> HeadendSinkProbeInput {
    HeadendSinkProbeInput {
        sink_id: SINK_ID.into(),
        label: "Cable primary".into(),
        kind: HeadendSinkKind::UdpTs,
        required: true,
        enabled: true,
        external_receiver: receiver,
    }
}

fn channel(sinks: Vec<HeadendSinkProbeInput>) -> HeadendChannelProbeInput {
    HeadendChannelProbeInput {
        channel_id: CHANNEL_ID.into(),
        display_name: "Government".into(),
        enabled: true,
        profile: profile(),
        sinks,
        runtime: runtime(ReadinessState::Pass, Some(1_000)),
    }
}

fn input(channels: Vec<HeadendChannelProbeInput>) -> HeadendProbeInput {
    HeadendProbeInput {
        generated_at_unix_ms: NOW,
        channels,
    }
}

#[test]
fn local_udp_progress_without_receiver_proof_stays_yellow() {
    let readiness = derive_headend_readiness(
        &station_box(),
        input(vec![channel(vec![udp_sink(
            ExternalReceiverState::Unknown,
        )])]),
    )
    .unwrap();

    assert_eq!(readiness.rollup.color, PegReadinessColor::Yellow);
    assert_ne!(readiness.rollup.color, PegReadinessColor::Green);
    assert_eq!(
        readiness.channels[0].sinks[0].external_receiver,
        ExternalReceiverState::Unknown
    );
}

#[test]
fn only_verified_receiver_evidence_can_make_a_required_udp_sink_green() {
    let readiness = derive_headend_readiness(
        &station_box(),
        input(vec![channel(vec![udp_sink(
            ExternalReceiverState::Verified,
        )])]),
    )
    .unwrap();

    assert_eq!(readiness.rollup.color, PegReadinessColor::Green);
    assert_eq!(readiness.channels[0].state, ReadinessState::Pass);
    assert_eq!(readiness.channels[0].sinks[0].state, ReadinessState::Pass);
}

#[test]
fn srt_transport_health_does_not_claim_downstream_decode_acceptance() {
    let mut station = station_box();
    station.engine.srt = CapabilitySupport::Supported;
    let mut sink = udp_sink(ExternalReceiverState::Unknown);
    sink.kind = HeadendSinkKind::Srt;

    let readiness = derive_headend_readiness(&station, input(vec![channel(vec![sink])])).unwrap();

    assert_eq!(
        readiness.channels[0].sinks[0].local_transport,
        ReadinessState::Pass
    );
    assert_eq!(
        readiness.channels[0].sinks[0].external_receiver,
        ExternalReceiverState::Unknown
    );
    assert_ne!(readiness.rollup.color, PegReadinessColor::Green);
}

#[test]
fn stale_or_missing_runtime_blocks_an_enabled_channel() {
    for runtime_probe in [
        runtime(ReadinessState::Blocked, Some(16_000)),
        runtime(ReadinessState::Unknown, None),
    ] {
        let mut configured = channel(vec![udp_sink(ExternalReceiverState::Verified)]);
        configured.runtime = runtime_probe;
        let readiness = derive_headend_readiness(&station_box(), input(vec![configured])).unwrap();
        assert_eq!(readiness.channels[0].state, ReadinessState::Blocked);
        assert_eq!(readiness.rollup.color, PegReadinessColor::Red);
    }
}

#[test]
fn configured_unimplemented_sdi_ndi_and_interlaced_outputs_are_explicitly_blocked() {
    for kind in [HeadendSinkKind::Sdi, HeadendSinkKind::Ndi] {
        let mut configured = channel(vec![udp_sink(ExternalReceiverState::Verified)]);
        configured.sinks[0].kind = kind;
        let readiness = derive_headend_readiness(&station_box(), input(vec![configured])).unwrap();
        assert_eq!(readiness.channels[0].state, ReadinessState::Blocked);
        assert!(readiness.channels[0].detail.contains("unimplemented"));
    }

    let mut configured = channel(vec![udp_sink(ExternalReceiverState::Verified)]);
    configured.profile.profile_id = "atsc-1080i5994".into();
    configured.profile.width = 1_920;
    configured.profile.height = 1_080;
    configured.profile.frame_rate_numerator = 30_000;
    configured.profile.video_bitrate_bps = 10_000_000;
    configured.profile.audio_bitrate_bps = 384_000;
    configured.profile.transport_bitrate_bps = 12_000_000;
    configured.profile.interlaced = true;
    let readiness = derive_headend_readiness(&station_box(), input(vec![configured])).unwrap();
    assert_eq!(readiness.channels[0].state, ReadinessState::Blocked);
    assert!(readiness.channels[0].detail.contains("interlaced"));
}

#[test]
fn local_proof_profile_cannot_be_reported_as_headend_ready() {
    let mut configured = channel(vec![udp_sink(ExternalReceiverState::Verified)]);
    configured.profile = HeadendProfileProbeInput {
        profile_id: "proof-360p30".into(),
        width: 640,
        height: 360,
        frame_rate_numerator: 30,
        frame_rate_denominator: 1,
        interlaced: false,
        video_bitrate_bps: 1_000_000,
        audio_bitrate_bps: 128_000,
        transport_bitrate_bps: 1_500_000,
        audio_sample_rate_hz: 48_000,
        loudness_target_lufs: -16.0,
    };
    let readiness = derive_headend_readiness(&station_box(), input(vec![configured])).unwrap();

    assert_eq!(readiness.rollup.color, PegReadinessColor::Red);
    assert!(readiness.channels[0].detail.contains("local proof"));
}

#[test]
fn missing_but_unconfigured_capabilities_do_not_block_progressive_ip() {
    let readiness = derive_headend_readiness(
        &station_box(),
        input(vec![channel(vec![udp_sink(
            ExternalReceiverState::Verified,
        )])]),
    )
    .unwrap();

    assert_eq!(readiness.channels[0].state, ReadinessState::Pass);
    assert!(!readiness.detail.contains("SDI"));
}

#[test]
fn disabled_channels_are_optional_and_do_not_change_the_rollup() {
    let mut disabled = channel(vec![]);
    disabled.channel_id = "99999999-aaaa-4bbb-8ccc-dddddddddddd".into();
    disabled.enabled = false;
    let readiness = derive_headend_readiness(
        &station_box(),
        input(vec![
            disabled,
            channel(vec![udp_sink(ExternalReceiverState::Verified)]),
        ]),
    )
    .unwrap();

    assert_eq!(readiness.channels[0].state, ReadinessState::Optional);
    assert_eq!(readiness.channels[1].state, ReadinessState::Pass);
    assert_eq!(readiness.rollup.color, PegReadinessColor::Green);
}

#[test]
fn invalid_profiles_and_duplicate_sink_identities_are_rejected() {
    let mut invalid_profile = channel(vec![udp_sink(ExternalReceiverState::Verified)]);
    invalid_profile.profile.width = 0;
    assert!(derive_headend_readiness(&station_box(), input(vec![invalid_profile])).is_err());

    let duplicate = channel(vec![
        udp_sink(ExternalReceiverState::Verified),
        udp_sink(ExternalReceiverState::Verified),
    ]);
    assert!(derive_headend_readiness(&station_box(), input(vec![duplicate])).is_err());
}

#[test]
fn every_derived_channel_and_sink_result_has_detail_and_evidence() {
    let readiness = derive_headend_readiness(
        &station_box(),
        input(vec![channel(vec![udp_sink(
            ExternalReceiverState::Unknown,
        )])]),
    )
    .unwrap();

    for channel in readiness.channels {
        assert!(!channel.detail.trim().is_empty());
        assert!(!channel.evidence.trim().is_empty());
        for sink in channel.sinks {
            assert!(!sink.detail.trim().is_empty());
            assert!(!sink.evidence.trim().is_empty());
        }
    }
}

#[test]
fn one_unknown_required_sink_can_never_make_the_station_green() {
    let mut unknown = channel(vec![udp_sink(ExternalReceiverState::Unknown)]);
    unknown.sinks[0].required = true;
    let readiness = derive_headend_readiness(&station_box(), input(vec![unknown])).unwrap();

    assert_ne!(readiness.rollup.color, PegReadinessColor::Green);
    assert_ne!(readiness.channels[0].sinks[0].state, ReadinessState::Pass);
}
