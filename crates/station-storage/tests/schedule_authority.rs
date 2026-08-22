use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use station_schedule::{AssetReadiness, DispatchStatus, MediaAsset, ScheduleItem, ScheduleState};
use station_storage::{CommitWriteError, StationStore};

const CHANNEL: &str = "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e";
const ITEM: &str = "256d5a07-92d3-4718-aec9-05cad42fae7d";
const ITEM_TWO: &str = "356d5a07-92d3-4718-aec9-05cad42fae7d";
const ITEM_THREE: &str = "456d5a07-92d3-4718-aec9-05cad42fae7d";
const CONFLICT: &str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
const ASSET: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

#[test]
fn reruns_the_gate_under_a_write_lock_and_persists_approval_before_dispatch() {
    let database = database_path("commit-race");
    let media = database.with_extension("ts");
    std::fs::write(&media, b"admitted bytes").unwrap();
    let store = StationStore::open(&database).unwrap();
    let mut asset = ready_asset();
    asset.media_path = media.to_string_lossy().into_owned();
    store.put_media_asset(&asset).unwrap();
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
    assert_eq!(
        store.list_commit_reports(CHANNEL).unwrap(),
        vec![report.clone()]
    );
    assert!(
        store
            .advance_dispatch_status("report-2", DispatchStatus::Pending, DispatchStatus::Queued)
            .unwrap()
    );
    assert!(
        store
            .advance_dispatch_status(
                "report-2",
                DispatchStatus::Queued,
                DispatchStatus::Acknowledged
            )
            .unwrap()
    );
    assert!(
        store
            .advance_dispatch_status(
                "report-2",
                DispatchStatus::Acknowledged,
                DispatchStatus::Completed
            )
            .unwrap()
    );
    assert_eq!(
        store.list_commit_reports(CHANNEL).unwrap()[0].dispatch_status,
        DispatchStatus::Completed
    );
    drop(store);

    let reopened = StationStore::open(&database).unwrap();
    let reopened_report = reopened.read_commit_report("report-2").unwrap().unwrap();
    assert_eq!(reopened_report.report_id, report.report_id);
    assert_eq!(reopened_report.dispatch_status, DispatchStatus::Completed);
    assert_eq!(reopened.list_channel_schedule(CHANNEL).unwrap().len(), 2);
    drop(reopened);
    remove_database(&database);
    std::fs::remove_file(media).unwrap();
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

#[test]
fn persists_missed_windows_with_compare_and_swap_transition_rules() {
    let database = database_path("missed-dispatch");
    let media = database.with_extension("ts");
    std::fs::write(&media, b"admitted bytes").unwrap();
    let store = StationStore::open(&database).unwrap();
    let mut asset = ready_asset();
    asset.media_path = media.to_string_lossy().into_owned();
    store.put_media_asset(&asset).unwrap();
    for (item_id, starts_at) in [(ITEM, 0), (ITEM_TWO, 60_000), (ITEM_THREE, 120_000)] {
        store
            .put_schedule_item(&schedule_item(item_id, starts_at, ScheduleState::Draft))
            .unwrap();
    }
    let pending = store
        .commit_schedule("missed-pending", "plan-pending", ITEM, "operator", 1, "")
        .unwrap();
    let queued = store
        .commit_schedule("missed-queued", "plan-queued", ITEM_TWO, "operator", 2, "")
        .unwrap();
    let acknowledged = store
        .commit_schedule(
            "missed-acknowledged",
            "plan-acknowledged",
            ITEM_THREE,
            "operator",
            3,
            "",
        )
        .unwrap();
    assert!(
        store
            .advance_dispatch_status(
                &pending.report_id,
                DispatchStatus::Pending,
                DispatchStatus::Missed,
            )
            .unwrap()
    );
    assert!(
        store
            .advance_dispatch_status(
                &queued.report_id,
                DispatchStatus::Pending,
                DispatchStatus::Queued,
            )
            .unwrap()
    );
    assert!(
        store
            .advance_dispatch_status(
                &queued.report_id,
                DispatchStatus::Queued,
                DispatchStatus::Missed,
            )
            .unwrap()
    );
    assert!(
        store
            .advance_dispatch_status(
                &acknowledged.report_id,
                DispatchStatus::Pending,
                DispatchStatus::Queued,
            )
            .unwrap()
    );
    assert!(
        store
            .advance_dispatch_status(
                &acknowledged.report_id,
                DispatchStatus::Queued,
                DispatchStatus::Acknowledged,
            )
            .unwrap()
    );
    assert!(matches!(
        store.advance_dispatch_status(
            &acknowledged.report_id,
            DispatchStatus::Acknowledged,
            DispatchStatus::Missed,
        ),
        Err(message) if message == "invalid dispatch status transition"
    ));
    assert_eq!(
        store
            .read_commit_report(&acknowledged.report_id)
            .unwrap()
            .unwrap()
            .dispatch_status,
        DispatchStatus::Acknowledged
    );
    drop(store);

    let reopened = StationStore::open(&database).unwrap();
    assert_eq!(
        reopened
            .read_commit_report(&pending.report_id)
            .unwrap()
            .unwrap()
            .dispatch_status,
        DispatchStatus::Missed
    );
    assert_eq!(
        reopened
            .read_commit_report(&queued.report_id)
            .unwrap()
            .unwrap()
            .dispatch_status,
        DispatchStatus::Missed
    );
    assert_eq!(
        reopened
            .read_commit_report(&acknowledged.report_id)
            .unwrap()
            .unwrap()
            .dispatch_status,
        DispatchStatus::Acknowledged
    );
    drop(reopened);
    remove_database(&database);
    std::fs::remove_file(&media).unwrap();
}

#[test]
fn fails_closed_when_a_ready_asset_file_has_disappeared() {
    let database = database_path("disappeared-media");
    let media = database.with_extension("ts");
    std::fs::write(&media, b"admitted bytes").unwrap();
    let store = StationStore::open(&database).unwrap();
    let mut asset = ready_asset();
    asset.media_path = media.to_string_lossy().into_owned();
    store.put_media_asset(&asset).unwrap();
    store
        .put_schedule_item(&schedule_item(ITEM, 100_000, ScheduleState::Draft))
        .unwrap();
    std::fs::remove_file(&media).unwrap();

    let plan = store
        .prepare_schedule_commit("missing-on-disk", ITEM)
        .unwrap();
    assert!(!plan.dry_run_passed);
    assert!(
        plan.missing_media_detail
            .as_deref()
            .unwrap()
            .contains("not ready")
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
