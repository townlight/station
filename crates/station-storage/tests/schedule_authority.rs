use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use station_schedule::{AssetReadiness, DispatchStatus, MediaAsset, ScheduleItem, ScheduleState};
use station_storage::{CommitWriteError, StationStore};

const CHANNEL: &str = "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e";
const ITEM: &str = "256d5a07-92d3-4718-aec9-05cad42fae7d";
const CONFLICT: &str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
const ASSET: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

#[test]
fn reruns_the_gate_under_a_write_lock_and_persists_approval_before_dispatch() {
    let database = database_path("commit-race");
    let store = StationStore::open(&database).unwrap();
    store.put_media_asset(&ready_asset()).unwrap();
    store
        .put_schedule_item(&schedule_item(ITEM, 100_000, ScheduleState::Draft))
        .unwrap();

    let preview = store.prepare_schedule_commit("plan-1", ITEM).unwrap();
    assert!(preview.dry_run_passed);

    store
        .put_schedule_item(&schedule_item(CONFLICT, 110_000, ScheduleState::Committed))
        .unwrap();
    let rejected = store.commit_schedule(
        "report-1",
        "plan-1",
        ITEM,
        "operator-scott",
        200_000,
        "Reviewed in the schedule console.",
    );
    assert!(matches!(
        rejected,
        Err(CommitWriteError::GateFailed(plan)) if plan.conflicts.len() == 1
    ));
    assert!(store.read_commit_report("report-1").unwrap().is_none());
    assert_eq!(
        store.read_schedule_item(ITEM).unwrap().unwrap().state,
        ScheduleState::Draft
    );

    store
        .put_schedule_item(&schedule_item(CONFLICT, 110_000, ScheduleState::Cancelled))
        .unwrap();
    let report = store
        .commit_schedule(
            "report-2",
            "plan-2",
            ITEM,
            "operator-scott",
            201_000,
            "Conflict resolved.",
        )
        .unwrap();
    assert_eq!(report.dispatch_status, DispatchStatus::Pending);
    assert_eq!(
        store.read_schedule_item(ITEM).unwrap().unwrap().state,
        ScheduleState::Committed
    );
    drop(store);

    let reopened = StationStore::open(&database).unwrap();
    assert_eq!(
        reopened.read_commit_report("report-2").unwrap(),
        Some(report)
    );
    assert_eq!(reopened.list_channel_schedule(CHANNEL).unwrap().len(), 2);
    drop(reopened);
    remove_database(&database);
}

#[test]
fn persists_an_honest_missing_media_preview_without_creating_a_report() {
    let database = database_path("missing-media");
    let store = StationStore::open(&database).unwrap();
    store
        .put_schedule_item(&schedule_item(ITEM, 100_000, ScheduleState::Draft))
        .unwrap();
    let plan = store.prepare_schedule_commit("missing", ITEM).unwrap();
    assert!(!plan.dry_run_passed);
    assert!(plan.missing_media_detail.is_some());
    assert!(matches!(
        store.commit_schedule("missing-report", "missing", ITEM, "operator", 200_000, ""),
        Err(CommitWriteError::GateFailed(_))
    ));
    assert!(
        store
            .read_commit_report("missing-report")
            .unwrap()
            .is_none()
    );
    drop(store);
    remove_database(&database);
}

fn ready_asset() -> MediaAsset {
    MediaAsset {
        asset_id: ASSET.into(),
        media_path: r"C:\ProgramData\TownLight Station\media\asset.ts".into(),
        duration_ms: 60_000,
        readiness: AssetReadiness::Ready,
    }
}

fn schedule_item(id: &str, starts_at_unix_ms: i64, state: ScheduleState) -> ScheduleItem {
    ScheduleItem {
        item_id: id.into(),
        channel_id: CHANNEL.into(),
        asset_id: ASSET.into(),
        title: "City Council".into(),
        starts_at_unix_ms,
        duration_ms: 60_000,
        state,
    }
}

fn database_path(label: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-schedule-{label}-{nonce}.db"))
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
