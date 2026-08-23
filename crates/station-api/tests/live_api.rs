use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{Value, json};
use station_api::Api;
use station_live::{LiveEndpoint, LiveSource, TakeoverState};
use station_schedule::{ChannelConfiguration, OutputProfileId, OutputSinkConfiguration};
use station_storage::StationStore;

const CHANNEL: &str = "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e";
const SOURCE: &str = "11111111-2222-4333-8444-555555555555";
const SESSION: &str = "33333333-4444-4555-8666-777777777777";

fn database(name: &str) -> std::path::PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-live-api-{name}-{nonce}.db"))
}

fn setup(name: &str) -> (Api, std::path::PathBuf) {
    let path = database(name);
    let api = Api::open(&path).unwrap();
    let channel = ChannelConfiguration {
        channel_id: CHANNEL.into(),
        display_name: "Live channel".into(),
        output_profile: OutputProfileId::Atsc720p5994,
        output_sinks: vec![OutputSinkConfiguration::required_udp(
            "44444444-5555-4666-8777-888888888888",
            "Primary",
            "127.0.0.1:5500",
            -24.0,
        )],
        enabled: true,
    };
    assert_eq!(
        api.handle_in_process(
            "PUT",
            "/api/v1/channels",
            Some(&serde_json::to_vec(&channel).unwrap())
        )
        .status,
        200
    );
    (api, path)
}

fn source_body(revision: u64, expected_revision: u64) -> Vec<u8> {
    serde_json::to_vec(&json!({
        "source_id": SOURCE,
        "channel_id": CHANNEL,
        "label": "Council feed",
        "enabled": true,
        "endpoint": {"UdpMpegTs": {"bind_ip": "239.1.2.3", "port": 5000}},
        "revision": revision,
        "expected_revision": expected_revision,
    }))
    .unwrap()
}

fn response_json(response: &station_api::ApiResponse) -> Value {
    serde_json::from_slice(&response.body).unwrap()
}

#[test]
fn exposes_revisioned_sources_and_rejects_stale_source_writes() {
    let (api, path) = setup("sources");
    assert_eq!(
        api.handle_in_process(
            "PUT",
            &format!("/api/v1/live-sources/{SOURCE}"),
            Some(&source_body(1, 0)),
        )
        .status,
        200
    );
    let listed = api.handle_in_process(
        "GET",
        &format!("/api/v1/channels/{CHANNEL}/live-sources"),
        None,
    );
    assert_eq!(listed.status, 200);
    let sources: Vec<LiveSource> = serde_json::from_slice(&listed.body).unwrap();
    assert_eq!(
        sources[0].endpoint,
        LiveEndpoint::UdpMpegTs {
            bind_ip: "239.1.2.3".into(),
            port: 5000
        }
    );
    assert_eq!(
        api.handle_in_process(
            "PUT",
            &format!("/api/v1/live-sources/{SOURCE}"),
            Some(&source_body(2, 0)),
        )
        .status,
        409
    );
    let authority = api.handle_in_process(
        "GET",
        &format!("/api/v1/channels/{CHANNEL}/live-authority"),
        None,
    );
    assert_eq!(response_json(&authority)["authority_revision"], 0);
    let _ = std::fs::remove_file(path);
}

#[test]
fn requires_arming_then_take_and_on_air_then_return_with_audited_actors() {
    let (api, path) = setup("takeover");
    assert_eq!(
        api.handle_in_process(
            "PUT",
            &format!("/api/v1/live-sources/{SOURCE}"),
            Some(&source_body(1, 0)),
        )
        .status,
        200
    );
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64;
    let requested = api.handle_in_process(
        "POST",
        "/api/v1/live-takeovers",
        Some(
            &serde_json::to_vec(&json!({
                "session_id": SESSION,
                "channel_id": CHANNEL,
                "source_id": SOURCE,
                "operator_id": "operator-scott",
                "reason": "council emergency",
                "expires_at_unix_ms": now + 300_000,
                "expected_authority_revision": 0,
            }))
            .unwrap(),
        ),
    );
    assert_eq!(requested.status, 201);
    let active = api.handle_in_process(
        "GET",
        &format!("/api/v1/channels/{CHANNEL}/active-live-takeover"),
        None,
    );
    assert_eq!(active.status, 200);
    assert_eq!(response_json(&active)["session_id"], SESSION);
    let take_before_armed = api.handle_in_process(
        "POST",
        &format!("/api/v1/live-takeovers/{SESSION}/take"),
        Some(
            &serde_json::to_vec(
                &json!({"actor_id":"operator-scott","expected_authority_revision":1}),
            )
            .unwrap(),
        ),
    );
    assert_eq!(take_before_armed.status, 409);

    let store = StationStore::open(&path).unwrap();
    store
        .transition_takeover(
            SESSION,
            TakeoverState::Arming,
            TakeoverState::Armed,
            now + 1,
            None,
            "worker",
            1,
        )
        .unwrap();
    let taken = api.handle_in_process(
        "POST",
        &format!("/api/v1/live-takeovers/{SESSION}/take"),
        Some(
            &serde_json::to_vec(
                &json!({"actor_id":"operator-scott","expected_authority_revision":2}),
            )
            .unwrap(),
        ),
    );
    assert_eq!(taken.status, 200);
    let return_before_on_air = api.handle_in_process(
        "POST",
        &format!("/api/v1/live-takeovers/{SESSION}/return"),
        Some(
            &serde_json::to_vec(
                &json!({"actor_id":"operator-scott","expected_authority_revision":3}),
            )
            .unwrap(),
        ),
    );
    assert_eq!(return_before_on_air.status, 409);
    store
        .transition_takeover(
            SESSION,
            TakeoverState::TakeRequested,
            TakeoverState::OnAir,
            now + 2,
            None,
            "worker",
            3,
        )
        .unwrap();
    assert_eq!(
        api.handle_in_process(
            "POST",
            &format!("/api/v1/live-takeovers/{SESSION}/return"),
            Some(
                &serde_json::to_vec(
                    &json!({"actor_id":"operator-scott","expected_authority_revision":4})
                )
                .unwrap()
            ),
        )
        .status,
        200
    );
    assert_eq!(
        api.handle_in_process(
            "POST",
            &format!("/api/v1/live-takeovers/{SESSION}/return"),
            Some(
                &serde_json::to_vec(
                    &json!({"actor_id":"operator-scott","expected_authority_revision":4})
                )
                .unwrap()
            ),
        )
        .status,
        409
    );
    store
        .transition_takeover(
            SESSION,
            TakeoverState::ReturnRequested,
            TakeoverState::Returning,
            now + 3,
            None,
            "worker",
            5,
        )
        .unwrap();
    store
        .transition_takeover(
            SESSION,
            TakeoverState::Returning,
            TakeoverState::Completed,
            now + 4,
            None,
            "worker",
            6,
        )
        .unwrap();
    let active = api.handle_in_process(
        "GET",
        &format!("/api/v1/channels/{CHANNEL}/active-live-takeover"),
        None,
    );
    assert_eq!(active.status, 200);
    assert_eq!(response_json(&active), Value::Null);
    let audit = api.handle_in_process(
        "GET",
        &format!("/api/v1/live-takeovers/{SESSION}/audit"),
        None,
    );
    assert_eq!(audit.status, 200);
    let audit = response_json(&audit).as_array().unwrap().clone();
    assert!(
        audit
            .iter()
            .any(|event| event["operator_id"] == "operator-scott"
                && event["detail"] == "council emergency")
    );
    assert!(audit.iter().any(|event| event["operator_id"] == "worker"));
    let _ = std::fs::remove_file(path);
}
