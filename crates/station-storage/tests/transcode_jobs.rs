use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use station_media_lifecycle::{
    AssetLoudnessAnalysis, IngestJobStatus, IngestSourceKind, LoudnessGateStatus, MediaIngestJob,
    TranscodeFormat, TranscodeJob, TranscodeJobStatus,
};
use station_schedule::{AssetReadiness, MediaAsset};
use station_storage::StationStore;

fn database_path(label: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-{label}-{nonce}.db"))
}

fn remove_database(path: &Path) {
    for candidate in [
        path.to_path_buf(),
        PathBuf::from(format!("{}-wal", path.display())),
        PathBuf::from(format!("{}-shm", path.display())),
    ] {
        let _ = std::fs::remove_file(candidate);
    }
}

fn pending_job() -> TranscodeJob {
    TranscodeJob {
        job_id: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee".into(),
        ingest_job_id: "11111111-2222-4333-8444-555555555555".into(),
        source_asset_id: "a".repeat(64),
        source_path: r"C:\ProgramData\TownLight Station\sources\meeting.mov".into(),
        output_format: TranscodeFormat::H264_720p4mbpsAac192kTs,
        status: TranscodeJobStatus::Pending,
        progress_percent: 0,
        created_at_unix_ms: 100,
        started_at_unix_ms: None,
        completed_at_unix_ms: None,
        output_asset_id: None,
        error_detail: None,
        attempts: 0,
        next_attempt_at_unix_ms: None,
    }
}

#[test]
fn persists_transcode_lifecycle_and_distinct_source_output_identity() {
    let database = database_path("transcode-lifecycle");
    let store = StationStore::open(&database).unwrap();
    let pending = pending_job();
    assert!(store.create_transcode_job(&pending).unwrap());
    assert!(!store.create_transcode_job(&pending).unwrap());
    assert!(
        store
            .complete_transcode_job(&pending.job_id, &"b".repeat(64), 300)
            .is_err()
    );

    let running = store.claim_next_transcode_job(200).unwrap().unwrap();
    assert_eq!(running.status, TranscodeJobStatus::Running);
    assert_eq!(running.progress_percent, 5);
    assert_eq!(running.attempts, 1);
    store
        .update_transcode_progress(&pending.job_id, 40)
        .unwrap();
    assert!(
        store
            .update_transcode_progress(&pending.job_id, 39)
            .is_err()
    );
    let output_asset_id = "b".repeat(64);
    store
        .complete_transcode_job(&pending.job_id, &output_asset_id, 300)
        .unwrap();
    drop(store);

    let reopened = StationStore::open(&database).unwrap();
    let completed = reopened
        .read_transcode_job(&pending.job_id)
        .unwrap()
        .unwrap();
    assert_eq!(completed.status, TranscodeJobStatus::Completed);
    assert_eq!(completed.progress_percent, 100);
    assert_eq!(completed.source_asset_id, "a".repeat(64));
    assert_eq!(completed.output_asset_id, Some(output_asset_id));
    assert_eq!(reopened.list_transcode_jobs().unwrap(), vec![completed]);
    drop(reopened);
    remove_database(&database);
}

#[test]
fn recovers_interrupted_transcode_for_a_new_attempt() {
    let database = database_path("transcode-recovery");
    let store = StationStore::open(&database).unwrap();
    let pending = pending_job();
    store.create_transcode_job(&pending).unwrap();
    store.claim_next_transcode_job(200).unwrap().unwrap();
    store
        .update_transcode_progress(&pending.job_id, 70)
        .unwrap();
    drop(store);

    let reopened = StationStore::open(&database).unwrap();
    assert_eq!(reopened.recover_interrupted_transcode_jobs().unwrap(), 1);
    let recovered = reopened
        .read_transcode_job(&pending.job_id)
        .unwrap()
        .unwrap();
    assert_eq!(recovered.status, TranscodeJobStatus::Pending);
    assert_eq!(recovered.progress_percent, 0);
    assert_eq!(recovered.attempts, 1);
    assert!(recovered.started_at_unix_ms.is_none());
    let reclaimed = reopened.claim_next_transcode_job(400).unwrap().unwrap();
    assert_eq!(reclaimed.attempts, 2);
    assert_eq!(reclaimed.source_asset_id, pending.source_asset_id);
    drop(reopened);
    remove_database(&database);
}

#[test]
fn atomically_hands_ingest_to_transcode_and_publishes_only_the_output_asset() {
    let database = database_path("transcode-handoff");
    let store = StationStore::open(&database).unwrap();
    let ingest = MediaIngestJob {
        job_id: "11111111-2222-4333-8444-555555555555".into(),
        source_kind: IngestSourceKind::OperatorPath,
        source_path: r"C:\incoming\meeting.mov".into(),
        status: IngestJobStatus::Pending,
        progress_percent: 0,
        created_at_unix_ms: 100,
        started_at_unix_ms: None,
        completed_at_unix_ms: None,
        error_detail: None,
        asset_id: None,
        attempts: 0,
        next_attempt_at_unix_ms: None,
    };
    store.create_ingest_job(&ingest).unwrap();
    store.claim_next_ingest_job(200).unwrap().unwrap();
    let transcode = pending_job();
    store.handoff_ingest_to_transcode(&transcode).unwrap();
    assert_eq!(
        store
            .read_ingest_job(&ingest.job_id)
            .unwrap()
            .unwrap()
            .status,
        IngestJobStatus::AwaitingTranscode
    );
    assert_eq!(
        store
            .read_transcode_job(&transcode.job_id)
            .unwrap()
            .unwrap(),
        transcode
    );
    store.claim_next_transcode_job(300).unwrap().unwrap();

    let output_asset_id = "b".repeat(64);
    let output = MediaAsset {
        asset_id: output_asset_id.clone(),
        media_path: r"C:\ProgramData\TownLight Station\media\output.ts".into(),
        duration_ms: 1_000,
        readiness: AssetReadiness::Ready,
    };
    let loudness = AssetLoudnessAnalysis {
        asset_id: output_asset_id.clone(),
        status: LoudnessGateStatus::Ok,
        measured_lufs_milli: Some(-24_000),
        target_lufs_milli: -24_000,
        detail: None,
    };
    store
        .complete_transcode_and_ingest(&transcode.job_id, &output, &loudness, 400)
        .unwrap();
    let completed_ingest = store.read_ingest_job(&ingest.job_id).unwrap().unwrap();
    assert_eq!(completed_ingest.status, IngestJobStatus::Completed);
    assert_eq!(completed_ingest.asset_id, Some(output_asset_id.clone()));
    assert_eq!(
        store
            .read_transcode_job(&transcode.job_id)
            .unwrap()
            .unwrap()
            .output_asset_id,
        Some(output_asset_id.clone())
    );
    assert_eq!(
        store.read_media_asset(&output_asset_id).unwrap(),
        Some(output)
    );
    assert!(
        store
            .read_media_asset(&transcode.source_asset_id)
            .unwrap()
            .is_none()
    );
    drop(store);
    remove_database(&database);
}
