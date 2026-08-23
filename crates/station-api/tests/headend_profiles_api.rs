use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use station_api::Api;
use station_schedule::{
    ChannelConfiguration, HeadendProfileId, OutputProfileId, OutputSinkConfiguration,
};
use station_storage::StationStore;

const CHANNEL_ID: &str = "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e";

fn temporary_database() -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-headend-api-{nonce}.db"))
}

fn seed_stopped_channel(database: &PathBuf) {
    StationStore::open(database)
        .unwrap()
        .put_channel(&ChannelConfiguration {
            channel_id: CHANNEL_ID.into(),
            display_name: "Government".into(),
            output_profile: OutputProfileId::Proof360p30,
            output_sinks: vec![OutputSinkConfiguration::required_udp(
                "22222222-3333-4444-8555-666666666666",
                "Streaming backup",
                "192.0.2.20:5500",
                -16.0,
            )],
            enabled: false,
        })
        .unwrap();
}

#[test]
fn lists_the_six_static_profiles_before_station_commissioning() {
    let database = temporary_database();
    let api = Api::open(&database).unwrap();

    let response = api.handle("GET", "/api/v1/headend-profiles", None);
    assert_eq!(response.status, 200);
    let profiles: serde_json::Value = serde_json::from_slice(&response.body).unwrap();
    assert_eq!(profiles.as_array().unwrap().len(), 6);
    assert!(profiles.as_array().unwrap().iter().all(|profile| {
        !profile["source_urls"].as_array().unwrap().is_empty()
            && !profile["operator_must_supply"]
                .as_array()
                .unwrap()
                .is_empty()
            && !profile["not_claimed"].as_array().unwrap().is_empty()
    }));
    let _ = std::fs::remove_file(database);
}

#[test]
fn applies_and_persists_a_profile_only_to_a_stopped_channel() {
    let database = temporary_database();
    seed_stopped_channel(&database);
    let api = Api::open(&database).unwrap();
    let body = br#"{
        "profile_id":"generic-udp-spts",
        "destination_uri":"udp://192.0.2.10:9000",
        "muxrate_kbps":9000,
        "keep_existing_sinks":true
    }"#;

    let response = api.handle(
        "POST",
        &format!("/api/v1/channels/{CHANNEL_ID}/headend-profile"),
        Some(body),
    );
    assert_eq!(
        response.status,
        200,
        "{}",
        String::from_utf8_lossy(&response.body)
    );
    let application: serde_json::Value = serde_json::from_slice(&response.body).unwrap();
    assert_eq!(application["profile_id"], "generic-udp-spts");
    assert_eq!(application["muxrate_kbps"], 9_000);
    assert_eq!(
        application["channel"]["output_sinks"]
            .as_array()
            .unwrap()
            .len(),
        2
    );

    let stored = StationStore::open(&database)
        .unwrap()
        .list_channels()
        .unwrap()
        .pop()
        .unwrap();
    let headend = stored
        .output_sinks
        .iter()
        .find_map(|sink| sink.headend.as_ref())
        .unwrap();
    assert_eq!(headend.profile_id, HeadendProfileId::GenericUdpSpts);
    assert_eq!(headend.muxrate_kbps, 9_000);
    assert!(!stored.enabled);

    let mut forged = serde_json::to_value(&stored).unwrap();
    let sinks = forged["output_sinks"].as_array_mut().unwrap();
    let headend_sink = sinks
        .iter_mut()
        .find(|sink| !sink["headend"].is_null())
        .unwrap();
    headend_sink["headend"]["muxrate_kbps"] = 10_000.into();
    let forged = api.handle(
        "PUT",
        "/api/v1/channels",
        Some(&serde_json::to_vec(&forged).unwrap()),
    );
    assert_eq!(forged.status, 422);
    assert!(String::from_utf8_lossy(&forged.body).contains("headend profile"));
    let _ = std::fs::remove_file(database);
}

#[test]
fn rejects_unknown_channels_and_unimplemented_file_drop_without_writing() {
    let database = temporary_database();
    seed_stopped_channel(&database);
    let api = Api::open(&database).unwrap();
    let body = br#"{
        "profile_id":"leightronix-file-drop",
        "destination_uri":"C:\\\\Headend\\Drop",
        "muxrate_kbps":null,
        "keep_existing_sinks":false
    }"#;

    let missing = api.handle(
        "POST",
        "/api/v1/channels/11111111-2222-4333-8444-555555555555/headend-profile",
        Some(body),
    );
    assert_eq!(missing.status, 404);
    let unsupported = api.handle(
        "POST",
        &format!("/api/v1/channels/{CHANNEL_ID}/headend-profile"),
        Some(body),
    );
    assert_eq!(unsupported.status, 422);
    assert!(String::from_utf8_lossy(&unsupported.body).contains("not implemented"));
    assert!(
        StationStore::open(&database)
            .unwrap()
            .list_channels()
            .unwrap()[0]
            .output_sinks
            .iter()
            .all(|sink| sink.headend.is_none())
    );
    let _ = std::fs::remove_file(database);
}

#[test]
fn active_channel_profile_application_returns_conflict_without_writing() {
    let database = temporary_database();
    let mut channel = ChannelConfiguration {
        channel_id: CHANNEL_ID.into(),
        display_name: "Government".into(),
        output_profile: OutputProfileId::Atsc720p5994,
        output_sinks: vec![OutputSinkConfiguration::required_udp(
            "22222222-3333-4444-8555-666666666666",
            "Existing output",
            "192.0.2.20:5500",
            -24.0,
        )],
        enabled: true,
    };
    StationStore::open(&database)
        .unwrap()
        .put_channel(&channel)
        .unwrap();
    let api = Api::open(&database).unwrap();
    let response = api.handle(
        "POST",
        &format!("/api/v1/channels/{CHANNEL_ID}/headend-profile"),
        Some(
            br#"{"profile_id":"generic-udp-spts","destination_uri":"udp://192.0.2.10:9000","muxrate_kbps":null,"keep_existing_sinks":false}"#,
        ),
    );
    assert_eq!(response.status, 409);
    channel = StationStore::open(&database)
        .unwrap()
        .list_channels()
        .unwrap()
        .pop()
        .unwrap();
    assert!(channel.enabled);
    assert!(
        channel
            .output_sinks
            .iter()
            .all(|sink| sink.headend.is_none())
    );
    let _ = std::fs::remove_file(database);
}
