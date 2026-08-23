use std::collections::HashMap;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use station_schedule::{
    ChannelConfiguration, HeadendProfileApplyRequest, OutputProfileId, OutputSinkConfiguration,
    apply_headend_profile, headend_profile,
};
use station_storage::{ChannelWriteError, StationStore};

const CHANNEL_ID: &str = "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e";

fn temporary_database() -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is before Unix epoch")
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-headend-profile-{nonce}.db"))
}

#[test]
fn stale_generic_channel_write_cannot_remove_a_newly_staged_headend_contract() {
    let database = temporary_database();
    let store = StationStore::open(&database).unwrap();
    let stale = ChannelConfiguration {
        channel_id: CHANNEL_ID.into(),
        display_name: "Government".into(),
        output_profile: OutputProfileId::Proof360p30,
        output_sinks: vec![OutputSinkConfiguration::required_udp(
            CHANNEL_ID,
            "Streaming backup",
            "192.0.2.20:5500",
            -16.0,
        )],
        enabled: false,
    };
    store.put_channel(&stale).unwrap();
    store
        .stage_headend_profile(
            CHANNEL_ID,
            &HeadendProfileApplyRequest {
                profile_id: "generic-udp-spts".into(),
                destination_uri: "udp://192.0.2.10:9000".into(),
                muxrate_kbps: None,
                keep_existing_sinks: true,
            },
        )
        .unwrap();

    assert_eq!(
        store.put_channel_with_srt_passphrases_if_current(&stale, &HashMap::new(), Some(&stale),),
        Err(ChannelWriteError::Conflict)
    );
    assert!(
        store.list_channels().unwrap()[0]
            .output_sinks
            .iter()
            .any(|sink| sink.headend.is_some())
    );
    drop(store);
    let _ = std::fs::remove_file(database);
}

#[test]
fn applied_headend_profile_round_trips_without_losing_per_sink_loudness_or_proof_boundary() {
    let database = temporary_database();
    let store = StationStore::open(&database).unwrap();
    let channel = ChannelConfiguration {
        channel_id: CHANNEL_ID.into(),
        display_name: "Government".into(),
        output_profile: OutputProfileId::Proof360p30,
        output_sinks: vec![OutputSinkConfiguration::required_udp(
            CHANNEL_ID,
            "Streaming backup",
            "192.0.2.20:5500",
            -16.0,
        )],
        enabled: false,
    };
    let request = HeadendProfileApplyRequest {
        profile_id: "generic-udp-spts".into(),
        destination_uri: "udp://192.0.2.10:9000".into(),
        muxrate_kbps: Some(8_000),
        keep_existing_sinks: true,
    };
    let applied = apply_headend_profile(&channel, &request).unwrap();
    store.put_channel(&applied.channel).unwrap();
    drop(store);

    let reopened = StationStore::open(&database).unwrap();
    let round_tripped = reopened
        .list_channels()
        .unwrap()
        .into_iter()
        .find(|value| value.channel_id == CHANNEL_ID)
        .expect("applied channel must persist");
    assert_eq!(round_tripped.output_profile, applied.channel.output_profile);
    assert_eq!(
        round_tripped.output_sinks.len(),
        applied.channel.output_sinks.len()
    );
    for (actual, expected) in round_tripped
        .output_sinks
        .iter()
        .zip(applied.channel.output_sinks.iter())
    {
        assert_eq!(actual.sink_id, expected.sink_id);
        assert_eq!(actual.destination, expected.destination);
        assert_eq!(actual.loudness_target_lufs, expected.loudness_target_lufs);
        assert_eq!(actual.headend, expected.headend);
    }
    let contract = round_tripped
        .output_sinks
        .iter()
        .find_map(|sink| sink.headend.as_ref())
        .expect("the applied sink must retain its headend contract");
    assert!(
        headend_profile(contract.profile_id.storage_name())
            .unwrap()
            .not_claimed
            .iter()
            .any(|item| item.to_ascii_lowercase().contains("field"))
    );
    let _ = std::fs::remove_file(database);
}
