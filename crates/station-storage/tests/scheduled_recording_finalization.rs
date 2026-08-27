use std::time::{SystemTime, UNIX_EPOCH};

use station_catalog::{CatalogAssetRecord, CatalogEditorialState};
use station_recording::{
    RecordingActor, RecordingActorRole, RecordingCandidateIdentity, RecordingDefinition,
    RecordingEvent, RecordingExecutionAuthority, RecordingLifecycle, RecordingTransitionRequest,
    ScheduledRecording,
};
use station_schedule::{
    AssetReadiness, ChannelConfiguration, MediaAsset, OutputProfileId, OutputSinkConfiguration,
    ScheduleItem, ScheduleState,
};
use station_storage::{ScheduleItemWrite, ScheduledRecordingFinalizationEvidence, StationStore};

const CHANNEL_ID: &str = "11111111-1111-4111-8111-111111111111";
const SINK_ID: &str = "22222222-2222-4222-8222-222222222222";
const ITEM_ID: &str = "33333333-3333-4333-8333-333333333333";
const RECORDING_ID: &str = "44444444-4444-4444-8444-444444444444";
const OPERATOR_ID: &str = "55555555-5555-4555-8555-555555555555";
const WORKER_ID: &str = "00000000-0000-4000-8000-000000000021";

fn transition(
    store: &StationStore,
    recording: &ScheduledRecording,
    actor: RecordingActor,
    time: i64,
    execution: Option<RecordingExecutionAuthority>,
    event: RecordingEvent,
) -> ScheduledRecording {
    let outcome = recording
        .transition(RecordingTransitionRequest {
            expected_revision: recording.revision,
            actor,
            occurred_at_unix_ms: time,
            execution,
            event,
        })
        .unwrap();
    store
        .write_scheduled_recording_transition(&outcome.recording, &outcome.receipt)
        .unwrap();
    outcome.recording
}

#[test]
fn finalization_atomically_commits_media_catalog_evidence_and_lifecycle() {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let root = std::env::temp_dir().join(format!("townlight-recording-finalization-{nonce}"));
    std::fs::create_dir_all(&root).unwrap();
    let database = root.join("station.db");
    let source_path = root.join("source.ts");
    let final_path = root.join("media").join(format!("{}.ts", "c".repeat(64)));
    std::fs::create_dir_all(final_path.parent().unwrap()).unwrap();
    std::fs::write(&source_path, b"source schedule media").unwrap();
    std::fs::write(&final_path, b"sealed recording media").unwrap();
    let store = StationStore::open(&database).unwrap();
    let channel = ChannelConfiguration {
        channel_id: CHANNEL_ID.into(),
        display_name: "Council".into(),
        output_profile: OutputProfileId::Atsc720p5994,
        output_sinks: vec![OutputSinkConfiguration::required_udp(
            SINK_ID,
            "Primary",
            "127.0.0.1:5001",
            -24.0,
        )],
        enabled: true,
    };
    store.put_channel(&channel).unwrap();
    let source_asset = MediaAsset {
        asset_id: "a".repeat(64),
        media_path: source_path.to_string_lossy().into_owned(),
        duration_ms: 60_000,
        readiness: AssetReadiness::Ready,
    };
    store.put_media_asset(&source_asset).unwrap();
    let schedule = ScheduleItem {
        item_id: ITEM_ID.into(),
        revision: 1,
        channel_id: CHANNEL_ID.into(),
        asset_id: source_asset.asset_id,
        title: "Recorded Council Meeting".into(),
        starts_at_unix_ms: 10_000,
        duration_ms: 60_000,
        state: ScheduleState::Draft,
    };
    store
        .write_schedule_item(&ScheduleItemWrite {
            item: schedule.clone(),
            changed_by: "test-harness".into(),
            occurred_at_unix_ms: 500,
        })
        .unwrap();
    store
        .commit_schedule("report", "plan", ITEM_ID, "test", 1, "fixture")
        .unwrap();
    let operator = RecordingActor {
        actor_id: OPERATOR_ID.into(),
        role: RecordingActorRole::Operator,
    };
    let worker = RecordingActor {
        actor_id: WORKER_ID.into(),
        role: RecordingActorRole::RecordingWorker,
    };
    let draft = ScheduledRecording::new(RecordingDefinition {
        recording_id: RECORDING_ID.into(),
        channel_id: CHANNEL_ID.into(),
        schedule_item_id: ITEM_ID.into(),
        schedule_revision: 2,
        scheduled_start_unix_ms: 10_000,
        scheduled_end_unix_ms: 70_000,
        requested_by: operator.clone(),
        requested_at_unix_ms: 1_000,
    })
    .unwrap();
    store.create_scheduled_recording(&draft).unwrap();
    let execution = RecordingExecutionAuthority {
        candidate: RecordingCandidateIdentity {
            version: "0.1.0".into(),
            source_commit: "b".repeat(40),
        },
        channel_configuration_hash: store
            .current_channel_output_configuration_hash(CHANNEL_ID)
            .unwrap(),
    };
    let armed = transition(
        &store,
        &draft,
        operator,
        2_000,
        Some(execution.clone()),
        RecordingEvent::Arm,
    );
    let capturing = transition(
        &store,
        &armed,
        worker.clone(),
        10_000,
        Some(execution.clone()),
        RecordingEvent::StartCapture,
    );
    let finalizing = transition(
        &store,
        &capturing,
        worker.clone(),
        70_000,
        Some(execution.clone()),
        RecordingEvent::BeginFinalization,
    );
    let completed = finalizing
        .transition(RecordingTransitionRequest {
            expected_revision: finalizing.revision,
            actor: worker,
            occurred_at_unix_ms: 70_100,
            execution: Some(execution),
            event: RecordingEvent::FinalizeSuccess {
                captured_asset_hash: "c".repeat(64),
                captured_duration_ms: 60_000,
            },
        })
        .unwrap();
    let asset = MediaAsset {
        asset_id: "c".repeat(64),
        media_path: final_path.to_string_lossy().into_owned(),
        duration_ms: 60_000,
        readiness: AssetReadiness::Ready,
    };
    let catalog = CatalogAssetRecord {
        asset_sha256: asset.asset_id.clone(),
        title: "Recorded Council Meeting".into(),
        meeting_body: None,
        editorial_state: CatalogEditorialState::Draft,
        published_at_utc_ms: None,
        revision: 1,
    };
    let evidence = ScheduledRecordingFinalizationEvidence {
        recording_id: RECORDING_ID.into(),
        worker_instance_id: "worker-boot-one".into(),
        worker_sequence: 19,
        graph_revision: 3,
        output_index: 0,
        sink_id: SINK_ID.into(),
        artifact_relative_path: format!("{RECORDING_ID}.ts"),
        capture_bytes: 22,
        capture_sha256: asset.asset_id.clone(),
        captured_duration_ms: 60_000,
        final_media_path: asset.media_path.clone(),
        recorded_at_unix_ms: 70_100,
    };
    store
        .finalize_scheduled_recording(
            &completed.recording,
            &completed.receipt,
            &evidence,
            &asset,
            &catalog,
        )
        .unwrap();
    assert!(matches!(
        store
            .read_scheduled_recording(RECORDING_ID)
            .unwrap()
            .unwrap()
            .lifecycle,
        RecordingLifecycle::Completed { .. }
    ));
    assert_eq!(
        store.read_media_asset(&asset.asset_id).unwrap(),
        Some(asset.clone())
    );
    assert_eq!(
        store.read_catalog_asset(&asset.asset_id).unwrap(),
        Some(catalog.clone())
    );
    assert_eq!(
        store
            .read_scheduled_recording_finalization(RECORDING_ID)
            .unwrap(),
        Some(evidence.clone())
    );
    assert_eq!(
        store
            .list_scheduled_recording_history(RECORDING_ID, 8)
            .unwrap()
            .len(),
        5
    );
    store
        .finalize_scheduled_recording(
            &completed.recording,
            &completed.receipt,
            &evidence,
            &asset,
            &catalog,
        )
        .unwrap();
    assert_eq!(
        store
            .list_scheduled_recording_history(RECORDING_ID, 8)
            .unwrap()
            .len(),
        5
    );
    drop(store);
    std::fs::remove_dir_all(root).unwrap();
}
