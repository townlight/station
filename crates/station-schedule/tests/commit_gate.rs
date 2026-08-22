use station_schedule::{
    AssetReadiness, ChannelConfiguration, MediaAsset, OUTPUT_PROFILES, OutputProfileId,
    OutputSinkConfiguration, ScheduleItem, ScheduleState, ScheduleWindow, prepare_commit,
};

const CHANNEL: &str = "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e";
const ITEM: &str = "256d5a07-92d3-4718-aec9-05cad42fae7d";
const ASSET: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

#[test]
fn output_profiles_are_explicit_runnable_transport_contracts() {
    assert_eq!(OUTPUT_PROFILES.len(), 2);
    let cable = OutputProfileId::Atsc720p5994.profile();
    assert_eq!((cable.width, cable.height), (1_280, 720));
    assert_eq!(
        (cable.frame_rate_numerator, cable.frame_rate_denominator),
        (60_000, 1_001)
    );
    assert_eq!(cable.transport_bitrate_bps, 8_000_000);
    assert_eq!(cable.loudness_target_lufs, -24.0);
    ChannelConfiguration {
        channel_id: CHANNEL.into(),
        display_name: "Government".into(),
        output_profile: OutputProfileId::Atsc720p5994,
        output_sinks: vec![OutputSinkConfiguration::required_udp(
            CHANNEL,
            "Primary transport",
            "127.0.0.1:5500",
            -24.0,
        )],
        enabled: true,
    }
    .validate()
    .unwrap();
    ChannelConfiguration {
        channel_id: CHANNEL.into(),
        display_name: "Remote contribution".into(),
        output_profile: OutputProfileId::Atsc720p5994,
        output_sinks: vec![OutputSinkConfiguration::required_srt(
            CHANNEL,
            "SRT caller",
            "srt://127.0.0.1:5600",
            -24.0,
        )],
        enabled: true,
    }
    .validate()
    .unwrap();
    let mut unsafe_uri = OutputSinkConfiguration::required_srt(
        CHANNEL,
        "SRT caller",
        "srt://127.0.0.1:5600?passphrase=leaked",
        -24.0,
    );
    unsafe_uri.enabled = false;
    assert!(
        ChannelConfiguration {
            channel_id: CHANNEL.into(),
            display_name: "Unsafe remote contribution".into(),
            output_profile: OutputProfileId::Atsc720p5994,
            output_sinks: vec![unsafe_uri],
            enabled: false,
        }
        .validate()
        .is_err()
    );
    assert!(OutputProfileId::from_storage_name("unknown").is_err());
}

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

#[test]
fn wall_clock_window_is_half_open_and_reports_the_resume_offset() {
    let scheduled = item(ITEM, 100_000, 60_000);
    assert_eq!(scheduled.window_at(99_999).unwrap(), ScheduleWindow::Future);
    assert_eq!(
        scheduled.window_at(100_000).unwrap(),
        ScheduleWindow::Active { media_offset_ms: 0 }
    );
    assert_eq!(
        scheduled.window_at(112_345).unwrap(),
        ScheduleWindow::Active {
            media_offset_ms: 12_345
        }
    );
    assert_eq!(scheduled.window_at(160_000).unwrap(), ScheduleWindow::Ended);
}
