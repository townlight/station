use station_readiness::{
    BackupProbeInput, CapabilitySupport, ClockProbeInput, ClockSyncStatus, EngineProbeInput,
    EngineTierVerdict, HardwareProbeInput, NetworkProbeInput, PegReadinessColor,
    ReadinessDimensionName, ReadinessState, ReleaseProbeInput, StationBoxProbeInput,
    derive_station_box_profile,
};

fn ready_probe() -> StationBoxProbeInput {
    StationBoxProbeInput {
        generated_at_unix_ms: 1_777_000_000_000,
        probe_collected_at_unix_ms: 1_777_000_000_000,
        max_probe_age_ms: 300_000,
        product_version: "0.1.0".into(),
        source_commit: Some("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into()),
        station_id: Some("3f5f721f-96c7-48b1-b061-1bf1ad1e62c2".into()),
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
            sdi: CapabilitySupport::Supported,
            ndi: CapabilitySupport::Supported,
            interlaced: CapabilitySupport::Supported,
        },
        clock: ClockProbeInput {
            configured_timezone: Some("America/Denver".into()),
            utc_offset_seconds: Some(-21_600),
            sync_status: ClockSyncStatus::Synchronized,
        },
        network: NetworkProbeInput {
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
    }
}

#[test]
fn only_complete_live_facts_are_green() {
    let profile = derive_station_box_profile(ready_probe()).unwrap();

    assert_eq!(profile.schema_version, 1);
    assert_eq!(
        profile.engine_tier_verdict,
        EngineTierVerdict::FullBroadcast
    );
    assert_eq!(profile.peg_readiness.color, PegReadinessColor::Green);
    assert!(profile.peg_readiness.dimensions.iter().all(|dimension| {
        dimension.state == ReadinessState::Pass
            && !dimension.detail.is_empty()
            && !dimension.evidence.is_empty()
    }));
}

#[test]
fn current_ip_only_capabilities_are_yellow_and_explicit() {
    let mut probe = ready_probe();
    probe.engine.sdi = CapabilitySupport::Unimplemented;
    probe.engine.ndi = CapabilitySupport::Unimplemented;
    probe.engine.interlaced = CapabilitySupport::Unimplemented;

    let profile = derive_station_box_profile(probe).unwrap();

    assert_eq!(profile.engine_tier_verdict, EngineTierVerdict::IpBroadcast);
    assert_eq!(profile.peg_readiness.color, PegReadinessColor::Yellow);
    let output = profile
        .peg_readiness
        .dimensions
        .iter()
        .find(|dimension| dimension.name == ReadinessDimensionName::OutputCapabilities)
        .unwrap();
    assert_eq!(output.state, ReadinessState::Recoverable);
    for name in ["interlaced", "SDI", "NDI"] {
        assert!(
            output.detail.contains(name),
            "missing honest output: {name}"
        );
    }
}

#[test]
fn missing_identity_engine_or_required_network_is_red() {
    for mutate in 0..3 {
        let mut probe = ready_probe();
        match mutate {
            0 => probe.station_id = None,
            1 => {
                probe.engine.gstreamer_version = None;
                probe.engine.missing_for_test("mpegtsmux");
            }
            2 => {
                probe.network.state = ReadinessState::Blocked;
                probe.network.interface_up = false;
            }
            _ => unreachable!(),
        }
        let profile = derive_station_box_profile(probe).unwrap();
        assert_eq!(profile.peg_readiness.color, PegReadinessColor::Red);
    }
}

#[test]
fn low_resources_unknown_clock_and_unverified_release_do_not_go_green() {
    let mut probe = ready_probe();
    probe.hardware.ram_total_gb = 4.0;
    probe.hardware.media_disk_free_gb = 80.0;
    probe.clock.sync_status = ClockSyncStatus::Unknown;
    probe.release.candidate_verified = false;
    probe.release.state = ReadinessState::Unknown;

    let profile = derive_station_box_profile(probe).unwrap();

    assert_eq!(profile.peg_readiness.color, PegReadinessColor::Yellow);
    assert_eq!(profile.hardware.state, ReadinessState::Recoverable);
}

#[test]
fn impossible_stale_or_ambiguous_probe_facts_fail_closed() {
    let mut probe = ready_probe();
    probe.hardware.ram_total_gb = f64::NAN;
    assert!(derive_station_box_profile(probe).is_err());

    let mut probe = ready_probe();
    probe.probe_collected_at_unix_ms -= 300_001;
    assert!(derive_station_box_profile(probe).is_err());

    let mut probe = ready_probe();
    probe.engine.required_factories.push("mpegtsmux".into());
    assert!(derive_station_box_profile(probe).is_err());

    let mut probe = ready_probe();
    probe.clock.utc_offset_seconds = Some(100_000);
    assert!(derive_station_box_profile(probe).is_err());

    let mut probe = ready_probe();
    probe.station_id = Some("not-an-id".into());
    assert!(derive_station_box_profile(probe).is_err());

    let mut probe = ready_probe();
    probe.network.outbound_udp = CapabilitySupport::Unknown;
    assert!(derive_station_box_profile(probe).is_err());

    let mut probe = ready_probe();
    probe.backup.writable = Some(false);
    assert!(derive_station_box_profile(probe).is_err());

    let mut probe = ready_probe();
    probe.release.candidate_verified = false;
    assert!(derive_station_box_profile(probe).is_err());
}

trait MissingFactoryForTest {
    fn missing_for_test(&mut self, name: &str);
}

impl MissingFactoryForTest for EngineProbeInput {
    fn missing_for_test(&mut self, name: &str) {
        self.available_factories.retain(|factory| factory != name);
    }
}
