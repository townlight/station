use station_media_protocol::{WorkerHeadendProfileId, WorkerOutputConfiguration};
use station_orchestration::worker_output_configurations;
use station_schedule::{
    ChannelConfiguration, HeadendProfileId, HeadendSinkContract, OutputProfileId,
    OutputSinkConfiguration,
};
use station_storage::StationStore;
use std::time::{SystemTime, UNIX_EPOCH};

const CHANNEL_ID: &str = "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e";

fn mixed_output_channel() -> ChannelConfiguration {
    ChannelConfiguration {
        channel_id: CHANNEL_ID.into(),
        display_name: "Government".into(),
        output_profile: OutputProfileId::Atsc720p5994,
        output_sinks: vec![
            OutputSinkConfiguration::required_udp(
                "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "Cable primary",
                "192.0.2.40:5500",
                -24.0,
            ),
            OutputSinkConfiguration::required_udp(
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "Streaming backup",
                "192.0.2.41:5500",
                -16.0,
            ),
            OutputSinkConfiguration::required_udp(
                "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                "Cable redundant",
                "192.0.2.42:5500",
                -24.0,
            ),
        ],
        enabled: false,
    }
}

#[test]
fn worker_bootstrap_preserves_mixed_per_sink_loudness_and_stable_configuration_order() {
    let database = std::env::temp_dir().join(format!(
        "townlight-bootstrap-output-test-{}-{}.db",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    let store = StationStore::open(&database).unwrap();
    let outputs = worker_output_configurations(&mixed_output_channel(), &store)
        .expect("configured output branches must become a worker bootstrap configuration");

    assert_eq!(outputs.len(), 3);
    let expected = [
        ("192.0.2.40:5500", -24.0_f64),
        ("192.0.2.41:5500", -16.0_f64),
        ("192.0.2.42:5500", -24.0_f64),
    ];
    for (output, (destination, loudness)) in outputs.iter().zip(expected) {
        match output {
            WorkerOutputConfiguration::UdpTs {
                destination: actual_destination,
                loudness_target_lufs: actual_loudness,
                headend,
                ..
            } => {
                assert_eq!(actual_destination, destination);
                assert_eq!(actual_loudness.as_lufs(), loudness);
                assert_eq!(headend, &None);
            }
            other => panic!("expected UDP output in stable channel order, got {other:?}"),
        }
    }
    drop(store);
    for suffix in ["", "-wal", "-shm"] {
        let _ = std::fs::remove_file(format!("{}{}", database.display(), suffix));
    }
}

#[test]
fn worker_bootstrap_carries_the_durable_runnable_headend_contract() {
    let database = std::env::temp_dir().join(format!(
        "townlight-bootstrap-headend-test-{}-{}.db",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    let store = StationStore::open(&database).unwrap();
    let mut sink = OutputSinkConfiguration::required_udp(
        "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        "Cable headend",
        "192.0.2.50:5500",
        -24.0,
    );
    sink.headend = Some(HeadendSinkContract {
        profile_id: HeadendProfileId::TelvueHypercasterIp,
        muxrate_kbps: 8_000,
        packet_size_bytes: 1_316,
    });
    let channel = ChannelConfiguration {
        channel_id: CHANNEL_ID.into(),
        display_name: "Government".into(),
        output_profile: OutputProfileId::Atsc720p5994,
        output_sinks: vec![sink],
        enabled: false,
    };

    let outputs = worker_output_configurations(&channel, &store).unwrap();
    let WorkerOutputConfiguration::UdpTs {
        headend: Some(headend),
        ..
    } = &outputs[0]
    else {
        panic!("expected a typed headend contract on the UDP worker output")
    };
    assert_eq!(
        headend.profile_id,
        WorkerHeadendProfileId::TelvueHypercasterIp
    );
    assert_eq!(headend.muxrate_kbps, 8_000);
    assert_eq!(headend.packet_size_bytes, 1_316);

    drop(store);
    for suffix in ["", "-wal", "-shm"] {
        let _ = std::fs::remove_file(format!("{}{}", database.display(), suffix));
    }
}

#[test]
fn worker_bootstrap_rejects_a_stored_headend_contract_the_worker_cannot_emit() {
    let database = std::env::temp_dir().join(format!(
        "townlight-bootstrap-invalid-headend-test-{}-{}.db",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    let store = StationStore::open(&database).unwrap();
    let mut sink = OutputSinkConfiguration::required_udp(
        "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
        "Cable headend",
        "192.0.2.51:5500",
        -24.0,
    );
    sink.headend = Some(HeadendSinkContract {
        profile_id: HeadendProfileId::GenericUdpSpts,
        muxrate_kbps: 8_001,
        packet_size_bytes: 1_316,
    });
    let channel = ChannelConfiguration {
        channel_id: CHANNEL_ID.into(),
        display_name: "Government".into(),
        output_profile: OutputProfileId::Atsc720p5994,
        output_sinks: vec![sink],
        enabled: false,
    };

    let error = worker_output_configurations(&channel, &store).unwrap_err();
    assert!(format!("{error:?}").contains("8000 kbps"));

    drop(store);
    for suffix in ["", "-wal", "-shm"] {
        let _ = std::fs::remove_file(format!("{}{}", database.display(), suffix));
    }
}
