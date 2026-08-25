use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use station_captions::{
    AssetCaptionCue, AssetCaptionTrackWrite, CaptionEgressProof, CaptionProofOutcome,
    CaptionTrackSourceKind,
};
use station_schedule::{
    AssetReadiness, ChannelConfiguration, MediaAsset, OutputProfileId, OutputSinkConfiguration,
    ScheduleItem, ScheduleState,
};
use station_storage::{ChannelCaptionMode, StationStore};

const CHANNEL_ID: &str = "169206ab-0e2d-4907-a3c7-a8ddc052f664";
const SINK_ID: &str = "23658df8-5401-473d-80b7-95bc84b44103";
const ASSET_ID: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const TRACK_ID: &str = "65a26343-0339-4654-a92c-4bf9384f18df";
const ITEM_ID: &str = "256d5a07-92d3-4718-aec9-05cad42fae7d";
const REPORT_ID: &str = "44444444-5555-4666-8777-888888888888";

fn root(label: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-caption-proof-{label}-{nonce}"))
}

fn setup(path: &Path) -> (StationStore, CaptionEgressProof) {
    std::fs::create_dir_all(path).unwrap();
    let database = path.join("station.db");
    let media = path.join("asset.ts");
    std::fs::write(&media, b"admitted media bytes").unwrap();
    let store = StationStore::open(database).unwrap();
    store
        .put_channel(&ChannelConfiguration {
            channel_id: CHANNEL_ID.into(),
            display_name: "Caption proof channel".into(),
            output_profile: OutputProfileId::Proof360p30,
            output_sinks: vec![OutputSinkConfiguration::required_udp(
                SINK_ID,
                "Proof output",
                "127.0.0.1:5500",
                -24.0,
            )],
            enabled: false,
        })
        .unwrap();
    store
        .put_media_asset(&MediaAsset {
            asset_id: ASSET_ID.into(),
            media_path: media.to_string_lossy().into_owned(),
            duration_ms: 60_000,
            readiness: AssetReadiness::Ready,
        })
        .unwrap();
    store
        .put_schedule_item(&ScheduleItem {
            item_id: ITEM_ID.into(),
            channel_id: CHANNEL_ID.into(),
            asset_id: ASSET_ID.into(),
            title: "City Council".into(),
            starts_at_unix_ms: 100_000,
            duration_ms: 60_000,
            state: ScheduleState::Draft,
        })
        .unwrap();
    store
        .commit_schedule(
            REPORT_ID,
            "caption-proof-plan",
            ITEM_ID,
            "setup-admin",
            90_000,
            "Approved for caption proof.",
        )
        .unwrap();
    let policy = store
        .write_channel_caption_policy(
            CHANNEL_ID,
            ChannelCaptionMode::Cea708,
            1,
            "en-US",
            0,
            "setup-admin",
            90_001,
            "9d747cc3-9c69-4c29-8b46-cbca6e09cd26",
        )
        .unwrap();
    let track = store
        .write_asset_caption_track(&AssetCaptionTrackWrite {
            track_id: TRACK_ID.into(),
            asset_id: ASSET_ID.into(),
            language: "en-US".into(),
            service_number: 1,
            source_kind: CaptionTrackSourceKind::External,
            label: "Reviewed English".into(),
            cues: vec![AssetCaptionCue {
                cue_id: "bf0aa2ca-1488-42d2-8c1b-5d9179ee5d30".into(),
                start_ms: 1_000,
                duration_ms: 2_000,
                text: "Town Council".into(),
            }],
            expected_revision: 0,
            actor: "setup-admin".into(),
            occurred_at_unix_ms: 90_002,
            audit_id: "54691f24-e034-45bd-9ed5-31d9227a78aa".into(),
        })
        .unwrap();
    let configuration_hash = store
        .current_channel_output_configuration_hash(CHANNEL_ID)
        .unwrap();
    let proof = CaptionEgressProof {
        proof_id: "11111111-2222-4333-8444-555555555555".into(),
        channel_id: CHANNEL_ID.into(),
        sink_id: SINK_ID.into(),
        report_id: REPORT_ID.into(),
        asset_id: ASSET_ID.into(),
        track_id: TRACK_ID.into(),
        track_revision: track.revision,
        cues_sha256: track.cues_sha256,
        caption_policy_revision: policy.revision,
        language: policy.language,
        service_number: policy.service_number,
        candidate_version: "0.3.0".into(),
        source_commit: "0123456789abcdef0123456789abcdef01234567".into(),
        configuration_hash,
        capture_started_asset_offset_ms: 0,
        capture_duration_ms: 20_000,
        capture_bytes: 1_000_000,
        capture_sha256: "d".repeat(64),
        artifact_relative_path: "caption-proofs/11111111-2222-4333-8444-555555555555.ts".into(),
        expected_cue_count: 1,
        decoded_cue_count: 1,
        expected_text_sha256: "e".repeat(64),
        decoded_text_sha256: "e".repeat(64),
        decoder_engine: "GStreamer 1.28.6".into(),
        decoder_method: "independent MPEG-TS CEA-708 decode-back".into(),
        outcome: CaptionProofOutcome::Verified,
        captured_at_unix_ms: 110_000,
    };
    (store, proof)
}

#[test]
fn append_only_caption_proof_round_trips_exact_bound_evidence() {
    let root = root("round-trip");
    let (store, proof) = setup(&root);

    store.append_caption_egress_proof(&proof).unwrap();
    assert_eq!(
        store
            .list_caption_egress_proofs(CHANNEL_ID, Some(SINK_ID), 10)
            .unwrap(),
        vec![proof.clone()]
    );
    assert!(store.append_caption_egress_proof(&proof).is_err());

    drop(store);
    let reopened = StationStore::open(root.join("station.db")).unwrap();
    assert_eq!(
        reopened
            .list_caption_egress_proofs(CHANNEL_ID, None, 10)
            .unwrap(),
        vec![proof]
    );
    drop(reopened);
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn stale_policy_track_schedule_and_output_bindings_fail_closed() {
    let root = root("stale-bindings");
    let (store, proof) = setup(&root);

    let mut stale = proof.clone();
    stale.caption_policy_revision += 1;
    assert!(store.append_caption_egress_proof(&stale).is_err());

    let mut stale = proof.clone();
    stale.track_revision += 1;
    assert!(store.append_caption_egress_proof(&stale).is_err());

    let mut stale = proof.clone();
    stale.asset_id = "b".repeat(64);
    assert!(store.append_caption_egress_proof(&stale).is_err());

    let mut stale = proof;
    stale.configuration_hash = "f".repeat(64);
    assert!(store.append_caption_egress_proof(&stale).is_err());

    drop(store);
    std::fs::remove_dir_all(root).unwrap();
}
