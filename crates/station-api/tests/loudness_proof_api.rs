use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::Value;
use station_api::Api;
use station_schedule::{ChannelConfiguration, OutputProfileId, OutputSinkConfiguration};

fn temporary_database(name: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is before Unix epoch")
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-station-{name}-{nonce}.db"))
}

fn configured_channel() -> ChannelConfiguration {
    ChannelConfiguration {
        channel_id: "11111111-2222-4333-8444-555555555555".into(),
        display_name: "Proof channel".into(),
        output_profile: OutputProfileId::Atsc720p5994,
        output_sinks: vec![
            OutputSinkConfiguration::required_udp(
                "22222222-3333-4444-8555-666666666666",
                "Cable output",
                "127.0.0.1:5500",
                -24.0,
            ),
            OutputSinkConfiguration::required_srt(
                "33333333-4444-4555-8666-777777777777",
                "Streaming output",
                "srt://127.0.0.1:5501",
                -16.0,
            ),
        ],
        enabled: true,
    }
}

fn save_channel(api: &Api, channel: &ChannelConfiguration) {
    let response = api.handle(
        "PUT",
        "/api/v1/channels",
        Some(&serde_json::to_vec(channel).expect("channel serializes")),
    );
    assert_eq!(
        response.status,
        200,
        "{}",
        String::from_utf8_lossy(&response.body)
    );
}

#[test]
fn returns_not_run_entries_for_enabled_udp_and_srt_sinks_in_configured_order() {
    let database = temporary_database("loudness-proof-api");
    let api = Api::open(&database).expect("database opens");
    let channel = configured_channel();
    save_channel(&api, &channel);

    let response = api.handle(
        "GET",
        &format!("/api/v1/channels/{}/loudness-proof", channel.channel_id),
        None,
    );
    assert_eq!(
        response.status,
        200,
        "{}",
        String::from_utf8_lossy(&response.body)
    );
    let body: Value = serde_json::from_slice(&response.body).expect("proof response is JSON");
    assert_eq!(body["channel_id"], channel.channel_id);
    let sinks = body["sinks"].as_array().expect("proof response has sinks");
    assert_eq!(sinks.len(), 2);
    assert_eq!(sinks[0]["sink_id"], channel.output_sinks[0].sink_id);
    assert_eq!(sinks[1]["sink_id"], channel.output_sinks[1].sink_id);
    assert_eq!(sinks[0]["configured_target_lufs"], -24.0);
    assert_eq!(sinks[1]["configured_target_lufs"], -16.0);
    assert_eq!(sinks[0]["measurement_status"], "not_run");
    assert_eq!(sinks[1]["measurement_status"], "not_run");
    assert_eq!(sinks[0]["measured_lufs"], Value::Null);
    assert_eq!(sinks[1]["measured_lufs"], Value::Null);
    assert_eq!(sinks[0]["receiver_acceptance"], "unknown");
    assert_eq!(sinks[1]["receiver_acceptance"], "unknown");

    let _ = std::fs::remove_file(database);
}

#[test]
fn rejects_invalid_or_unknown_loudness_proof_channel_paths() {
    let database = temporary_database("loudness-proof-api-path");
    let api = Api::open(&database).expect("database opens");

    let invalid = api.handle("GET", "/api/v1/channels/not-a-uuid/loudness-proof", None);
    assert_eq!(invalid.status, 400);

    let missing = api.handle(
        "GET",
        "/api/v1/channels/11111111-2222-4333-8444-555555555555/loudness-proof",
        None,
    );
    assert_eq!(missing.status, 404);

    let _ = std::fs::remove_file(database);
}
