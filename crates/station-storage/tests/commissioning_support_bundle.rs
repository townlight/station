use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use station_schedule::{
    AssetReadiness, ChannelConfiguration, DispatchStatus, MediaAsset, OutputProfileId,
    OutputSinkConfiguration, ScheduleItem, ScheduleState,
};
use station_storage::{CommissioningSupportBundleReceipt, StationStore};

const CHANNEL_ID: &str = "11111111-2222-4333-8444-555555555555";
const ASSET_ID: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const ITEM_ID: &str = "22222222-3333-4444-8555-666666666666";
const REPORT_ID: &str = "report-guided";
const BUNDLE_ID: &str = "support-bundle-1234";
const BUNDLE_SHA256: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

fn temporary_root(name: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-{name}-{nonce}"))
}

fn completed_report(store: &StationStore, root: &std::path::Path) {
    let media = root.join("guided.ts");
    std::fs::write(&media, b"guided-media").unwrap();
    store
        .put_channel(&ChannelConfiguration {
            channel_id: CHANNEL_ID.into(),
            display_name: "Commissioning channel".into(),
            output_profile: OutputProfileId::Proof360p30,
            output_sinks: vec![OutputSinkConfiguration::required_udp(
                "33333333-4444-4555-8666-777777777777",
                "Proof",
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
            duration_ms: 30_000,
            readiness: AssetReadiness::Ready,
        })
        .unwrap();
    store
        .put_schedule_item(&ScheduleItem {
            item_id: ITEM_ID.into(),
            channel_id: CHANNEL_ID.into(),
            asset_id: ASSET_ID.into(),
            title: "Guided broadcast".into(),
            starts_at_unix_ms: 10_000,
            duration_ms: 30_000,
            state: ScheduleState::Draft,
        })
        .unwrap();
    store
        .commit_schedule(REPORT_ID, "plan-guided", ITEM_ID, "operator", 1_000, "")
        .unwrap();
    assert!(
        store
            .advance_dispatch_status(REPORT_ID, DispatchStatus::Pending, DispatchStatus::Queued,)
            .unwrap()
    );
    assert!(
        store
            .advance_dispatch_status(
                REPORT_ID,
                DispatchStatus::Queued,
                DispatchStatus::Acknowledged,
            )
            .unwrap()
    );
    assert!(
        store
            .advance_dispatch_status(
                REPORT_ID,
                DispatchStatus::Acknowledged,
                DispatchStatus::Completed,
            )
            .unwrap()
    );
}

#[test]
fn commissioning_bundle_receipt_is_append_only_and_report_bound() {
    let root = temporary_root("commissioning-support-receipt");
    std::fs::create_dir_all(&root).unwrap();
    let database = root.join("station.db");
    let store = StationStore::open(&database).unwrap();
    completed_report(&store, &root);

    let receipt = CommissioningSupportBundleReceipt {
        bundle_id: BUNDLE_ID.into(),
        report_id: REPORT_ID.into(),
        archive_sha256: BUNDLE_SHA256.into(),
        archive_size_bytes: 4_096,
        created_at_unix_ms: 2_000,
        created_by: "setup-admin".into(),
    };
    store
        .append_commissioning_support_bundle_receipt(&receipt)
        .unwrap();
    assert_eq!(
        store
            .list_commissioning_support_bundle_receipts(10)
            .unwrap(),
        vec![receipt.clone()]
    );
    assert!(
        store
            .append_commissioning_support_bundle_receipt(&receipt)
            .unwrap_err()
            .contains("UNIQUE")
    );

    let mut missing_report = receipt;
    missing_report.bundle_id = "support-bundle-missing-report".into();
    missing_report.report_id = "missing-report".into();
    assert!(
        store
            .append_commissioning_support_bundle_receipt(&missing_report)
            .unwrap_err()
            .contains("existing commit report")
    );

    drop(store);
    std::fs::remove_dir_all(root).unwrap();
}
