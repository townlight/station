use serde_json::json;
use station_media_protocol::{
    LoudnessTarget, PROTOCOL_VERSION, ProtocolVersion, WorkerBootstrapEnvelope, WorkerEvent,
    WorkerEventEnvelope, WorkerOutputConfiguration,
};

const CABLE_TARGET_LUFS: f64 = -24.0;
const STREAMING_TARGET_LUFS: f64 = -16.0;

fn bootstrap_with_per_sink_targets() -> serde_json::Value {
    json!({
        "version": PROTOCOL_VERSION,
        "worker_id": "worker-1",
        "channel_id": "channel-1",
        "output_profile": "atsc-720p5994",
        "cg_image_root": "C:\\ProgramData\\TownLight Station\\cg-images",
        "proof_artifact_root": "C:\\ProgramData\\TownLight Station\\output-proofs",
        "outputs": [
            {
                "kind": "udp-ts",
                "sink_id": "11111111-2222-4333-8444-555555555555",
                "destination": "127.0.0.1:5500",
                "loudness_target_lufs": CABLE_TARGET_LUFS
            },
            {
                "kind": "srt",
                "sink_id": "22222222-3333-4444-8555-666666666666",
                "uri": "srt://127.0.0.1:5600",
                "loudness_target_lufs": STREAMING_TARGET_LUFS
            }
        ]
    })
}

#[test]
fn worker_bootstrap_preserves_distinct_per_sink_loudness_targets() {
    let input = bootstrap_with_per_sink_targets();
    let decoded: WorkerBootstrapEnvelope =
        serde_json::from_value(input).expect("a worker must accept the per-sink loudness contract");
    let round_trip =
        serde_json::to_value(decoded).expect("worker bootstrap serialization must remain valid");

    assert_eq!(
        round_trip["outputs"][0]["loudness_target_lufs"], CABLE_TARGET_LUFS,
        "the cable branch must retain its -24 LUFS target"
    );
    assert_eq!(
        round_trip["outputs"][1]["loudness_target_lufs"], STREAMING_TARGET_LUFS,
        "the streaming branch must retain its -16 LUFS target"
    );
    assert_ne!(
        round_trip["outputs"][0]["loudness_target_lufs"],
        round_trip["outputs"][1]["loudness_target_lufs"],
        "different targets must not silently collapse into one branch setting"
    );
}

#[test]
fn output_failure_observation_remains_attributable_to_the_targeted_sink_index() {
    let input = bootstrap_with_per_sink_targets();
    let decoded: WorkerBootstrapEnvelope = serde_json::from_value(input)
        .expect("a worker must accept both independently configured outputs");
    let output_count = decoded.outputs.len();
    assert_eq!(output_count, 2);

    let heartbeat = WorkerEventEnvelope {
        version: PROTOCOL_VERSION,
        worker_id: "worker-1".into(),
        channel_id: "channel-1".into(),
        sequence: 7,
        event: WorkerEvent::Heartbeat {
            monotonic_milliseconds: 7000,
            outputs: vec![
                station_media_protocol::OutputTransportObservation {
                    output_index: 0,
                    bytes_sent_total: 100,
                    audio_buffers_total: 10,
                },
                station_media_protocol::OutputTransportObservation {
                    output_index: 1,
                    bytes_sent_total: 0,
                    audio_buffers_total: 0,
                },
            ],
        },
    };
    let wire = serde_json::to_value(heartbeat).expect("heartbeat must serialize");
    let round_trip: WorkerEventEnvelope = serde_json::from_value(wire)
        .expect("heartbeat must deserialize without losing output identity");

    let WorkerEvent::Heartbeat { outputs, .. } = round_trip.event else {
        panic!("expected a heartbeat event");
    };
    assert_eq!(outputs.len(), output_count);
    assert_eq!(outputs[0].output_index, 0);
    assert_eq!(outputs[1].output_index, 1);
    assert_eq!(outputs[1].bytes_sent_total, 0);
}

#[test]
fn output_configuration_variants_keep_their_transport_identity_with_targets() {
    let input = bootstrap_with_per_sink_targets();
    let decoded: WorkerBootstrapEnvelope =
        serde_json::from_value(input).expect("output variants must deserialize");

    assert!(matches!(
        decoded.outputs[0],
        WorkerOutputConfiguration::UdpTs { .. }
    ));
    assert!(matches!(
        decoded.outputs[1],
        WorkerOutputConfiguration::Srt { .. }
    ));

    let version: ProtocolVersion = decoded.version;
    assert_eq!(version, PROTOCOL_VERSION);
}

#[test]
fn loudness_target_rejects_non_finite_out_of_range_and_overprecise_values() {
    for invalid in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
        assert!(
            LoudnessTarget::from_lufs(invalid).is_err(),
            "non-finite target {invalid:?} must be rejected"
        );
    }
    for invalid in [-70.001, -4.999] {
        assert!(
            LoudnessTarget::from_lufs(invalid).is_err(),
            "out-of-range target {invalid} must be rejected"
        );
    }
    assert!(
        LoudnessTarget::from_lufs(-16.0001).is_err(),
        "targets with more than three decimal places must be rejected"
    );
    assert_eq!(
        LoudnessTarget::from_lufs(-16.001)
            .expect("three-decimal target is valid")
            .milli_lufs(),
        -16_001
    );
}

#[test]
fn worker_rejects_an_output_missing_its_loudness_target() {
    let mut input = bootstrap_with_per_sink_targets();
    input["outputs"][0]
        .as_object_mut()
        .expect("output is an object")
        .remove("loudness_target_lufs");

    assert!(
        serde_json::from_value::<WorkerBootstrapEnvelope>(input).is_err(),
        "missing per-sink loudness must fail closed rather than default silently"
    );
}
