use station_schedule::{AssetReadiness, MediaAsset, ScheduleItem, ScheduleState, prepare_commit};

const CHANNEL: &str = "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e";
const ITEM: &str = "256d5a07-92d3-4718-aec9-05cad42fae7d";
const ASSET: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

fn asset(readiness: AssetReadiness) -> MediaAsset {
    MediaAsset {
        asset_id: ASSET.into(),
        media_path: r"C:\ProgramData\TownLight Station\media\asset.ts".into(),
        duration_ms: 60_000,
        readiness,
    }
}

fn item(id: &str, starts_at_unix_ms: i64, duration_ms: u64) -> ScheduleItem {
    ScheduleItem {
        item_id: id.into(),
        channel_id: CHANNEL.into(),
        asset_id: ASSET.into(),
        title: "City Council".into(),
        starts_at_unix_ms,
        duration_ms,
        state: ScheduleState::Draft,
    }
}

#[test]
fn passes_a_ready_nonconflicting_item() {
    let plan = prepare_commit(
        "plan-1",
        &item(ITEM, 100_000, 60_000),
        Some(&asset(AssetReadiness::Ready)),
        &[],
    )
    .unwrap();
    assert!(plan.dry_run_passed);
    assert!(plan.conflicts.is_empty());
    assert!(plan.missing_media_detail.is_none());
}

#[test]
fn fails_closed_for_missing_processing_or_short_media() {
    let proposed = item(ITEM, 100_000, 60_000);
    assert!(
        !prepare_commit("missing", &proposed, None, &[])
            .unwrap()
            .dry_run_passed
    );
    assert!(
        !prepare_commit(
            "processing",
            &proposed,
            Some(&asset(AssetReadiness::Processing)),
            &[]
        )
        .unwrap()
        .dry_run_passed
    );
    let mut short = asset(AssetReadiness::Ready);
    short.duration_ms = 59_999;
    assert!(
        !prepare_commit("short", &proposed, Some(&short), &[])
            .unwrap()
            .dry_run_passed
    );
}

#[test]
fn actively_detects_every_overlap_and_ignores_cancelled_items() {
    let proposed = item(ITEM, 100_000, 60_000);
    let mut cancelled = item("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", 110_000, 10_000);
    cancelled.state = ScheduleState::Cancelled;
    let existing = vec![
        item("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", 90_000, 20_000),
        item("cccccccc-cccc-cccc-cccc-cccccccccccc", 150_000, 20_000),
        cancelled,
    ];
    let plan = prepare_commit(
        "conflicts",
        &proposed,
        Some(&asset(AssetReadiness::Ready)),
        &existing,
    )
    .unwrap();
    assert!(!plan.dry_run_passed);
    assert_eq!(plan.conflicts.len(), 2);
    assert_eq!(plan.conflicts[0].overlap_ms, 10_000);
    assert_eq!(plan.conflicts[1].overlap_ms, 10_000);
}

#[test]
fn reports_only_the_nearest_gap_and_does_not_fail_for_it() {
    let proposed = item(ITEM, 100_000, 60_000);
    let existing = vec![
        item("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", 20_000, 20_000),
        item("cccccccc-cccc-cccc-cccc-cccccccccccc", 70_000, 20_000),
    ];
    let plan = prepare_commit(
        "gap",
        &proposed,
        Some(&asset(AssetReadiness::Ready)),
        &existing,
    )
    .unwrap();
    assert!(plan.dry_run_passed);
    assert_eq!(plan.gaps.len(), 1);
    assert_eq!(plan.gaps[0].prior_item_id, existing[1].item_id);
    assert_eq!(plan.gaps[0].gap_ms, 10_000);
}

#[test]
fn adjacent_items_have_no_conflict_or_reportable_gap() {
    let proposed = item(ITEM, 100_000, 60_000);
    let existing = vec![item("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", 40_000, 60_000)];
    let plan = prepare_commit(
        "adjacent",
        &proposed,
        Some(&asset(AssetReadiness::Ready)),
        &existing,
    )
    .unwrap();
    assert!(plan.dry_run_passed);
    assert!(plan.conflicts.is_empty());
    assert!(plan.gaps.is_empty());
}
