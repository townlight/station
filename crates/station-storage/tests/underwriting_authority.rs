use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use station_schedule::{AssetReadiness, MediaAsset, ScheduleItem, ScheduleState};
use station_storage::{ScheduleItemWrite, StationStore, UnderwritingWriteError};
use station_underwriting::{
    PlacementConstraints, UnderwritingFlight, UnderwritingPlacementIntent, UnderwritingSpot,
};

const SPOT: &str = "11111111-1111-4111-8111-111111111111";
const SPONSOR: &str = "22222222-2222-4222-8222-222222222222";
const ASSET: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const FLIGHT: &str = "33333333-3333-4333-8333-333333333333";
const CHANNEL: &str = "44444444-4444-4444-8444-444444444444";
const PLACEMENT: &str = "55555555-5555-4555-8555-555555555555";
const PLACEMENT_TWO: &str = "66666666-6666-4666-8666-666666666666";
const SCHEDULE: &str = "77777777-7777-4777-8777-777777777777";
const SCHEDULE_TWO: &str = "88888888-8888-4888-8888-888888888888";
const SPOT_TWO: &str = "99999999-9999-4999-8999-999999999999";
const SPONSOR_TWO: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const FLIGHT_TWO: &str = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const PLACEMENT_THREE: &str = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";

fn database_path(label: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let directory = std::env::temp_dir().join(format!("townlight-underwriting-{label}-{nonce}"));
    std::fs::create_dir_all(&directory).unwrap();
    directory.join("station.db")
}

fn flight() -> UnderwritingFlight {
    UnderwritingFlight {
        flight_id: FLIGHT.into(),
        sponsor_id: SPONSOR.into(),
        spot_id: SPOT.into(),
        spot_revision: 1,
        starts_at_unix_ms: 0,
        ends_at_unix_ms: 1_000_000,
        channel_ids: vec![CHANNEL.into()],
        placement: PlacementConstraints {
            allowed_weekdays: 0b111_1111,
            daypart_start_minute: 0,
            daypart_end_minute: 1440,
            max_airings: 1,
            max_airings_per_day: 1,
            minimum_separation_ms: 60_000,
        },
        revision: 1,
    }
}

fn placement(id: &str, scheduled_at_unix_ms: i64) -> UnderwritingPlacementIntent {
    UnderwritingPlacementIntent {
        placement_id: id.into(),
        flight_id: FLIGHT.into(),
        flight_revision: 1,
        channel_id: CHANNEL.into(),
        scheduled_at_unix_ms,
        station_weekday: 0,
        station_minute_of_day: u16::try_from(scheduled_at_unix_ms / 60_000).unwrap(),
        revision: 1,
    }
}

fn spot(revision: u64) -> UnderwritingSpot {
    UnderwritingSpot {
        spot_id: SPOT.into(),
        sponsor_id: SPONSOR.into(),
        asset_id: ASSET.into(),
        label: "Civic underwriting".into(),
        duration_ms: 30_000,
        revision,
    }
}

#[test]
fn persists_current_and_append_only_spot_history_with_cas() {
    let store = StationStore::open(database_path("spot-history")).unwrap();
    store
        .write_underwriting_spot(&spot(1), None, "operator", 100)
        .unwrap();
    let mut revised = spot(2);
    revised.label = "Civic underwriting revised".into();
    store
        .write_underwriting_spot(&revised, Some(1), "operator", 101)
        .unwrap();

    assert_eq!(store.read_underwriting_spot(SPOT).unwrap(), Some(revised));
    assert_eq!(store.list_underwriting_spot_history(SPOT).unwrap().len(), 2);
    assert!(
        store
            .write_underwriting_spot(&spot(2), Some(1), "operator", 102)
            .is_err()
    );
}

#[test]
fn binds_placements_to_ready_drafts_and_reconciles_capacity() {
    let database = database_path("placement-binding");
    let store = StationStore::open(&database).unwrap();
    store
        .put_media_asset(&MediaAsset {
            asset_id: ASSET.into(),
            media_path: database.with_extension("ts").to_string_lossy().into_owned(),
            duration_ms: 30_000,
            readiness: AssetReadiness::Ready,
        })
        .unwrap();
    store
        .write_underwriting_spot(&spot(1), None, "operator", 100)
        .unwrap();
    store
        .write_underwriting_flight(&flight(), None, "operator", 101)
        .unwrap();
    let schedule = ScheduleItem {
        item_id: SCHEDULE.into(),
        revision: 1,
        channel_id: CHANNEL.into(),
        asset_id: ASSET.into(),
        title: "Underwriting".into(),
        starts_at_unix_ms: 120_000,
        duration_ms: 30_000,
        state: ScheduleState::Draft,
    };
    store
        .write_schedule_item(&ScheduleItemWrite {
            item: schedule,
            changed_by: "operator".into(),
            occurred_at_unix_ms: 102,
        })
        .unwrap();
    store
        .write_underwriting_placement(
            &placement(PLACEMENT, 120_000),
            SCHEDULE,
            1,
            None,
            "operator",
            103,
        )
        .unwrap();
    let second = ScheduleItem {
        item_id: SCHEDULE_TWO.into(),
        revision: 1,
        channel_id: CHANNEL.into(),
        asset_id: ASSET.into(),
        title: "Underwriting".into(),
        starts_at_unix_ms: 180_000,
        duration_ms: 30_000,
        state: ScheduleState::Draft,
    };
    store
        .write_schedule_item(&ScheduleItemWrite {
            item: second,
            changed_by: "operator".into(),
            occurred_at_unix_ms: 104,
        })
        .unwrap();
    assert!(matches!(
        store.write_underwriting_placement(
            &placement(PLACEMENT_TWO, 180_000),
            SCHEDULE_TWO,
            1,
            None,
            "operator",
            105
        ),
        Err(UnderwritingWriteError::Capacity)
    ));
}

#[test]
fn rejects_non_ready_media_even_when_the_draft_binding_matches() {
    let database = database_path("rejected-media");
    let store = StationStore::open(&database).unwrap();
    store
        .put_media_asset(&MediaAsset {
            asset_id: ASSET.into(),
            media_path: database.with_extension("ts").to_string_lossy().into_owned(),
            duration_ms: 30_000,
            readiness: AssetReadiness::Rejected,
        })
        .unwrap();
    store
        .write_underwriting_spot(&spot(1), None, "operator", 100)
        .unwrap();
    store
        .write_underwriting_flight(&flight(), None, "operator", 101)
        .unwrap();
    let schedule = ScheduleItem {
        item_id: SCHEDULE.into(),
        revision: 1,
        channel_id: CHANNEL.into(),
        asset_id: ASSET.into(),
        title: "Underwriting".into(),
        starts_at_unix_ms: 120_000,
        duration_ms: 30_000,
        state: ScheduleState::Draft,
    };
    store
        .write_schedule_item(&ScheduleItemWrite {
            item: schedule,
            changed_by: "operator".into(),
            occurred_at_unix_ms: 102,
        })
        .unwrap();
    assert!(
        matches!(store.write_underwriting_placement(&placement(PLACEMENT, 120_000), SCHEDULE, 1, None, "operator", 103), Err(UnderwritingWriteError::Invalid(message)) if message.contains("ready"))
    );
}

#[test]
fn placement_cas_uses_immutable_spot_revision_and_keeps_one_stable_schedule_owner() {
    let database = database_path("placement-cas-stable-schedule");
    let store = StationStore::open(&database).unwrap();
    store
        .put_media_asset(&MediaAsset {
            asset_id: ASSET.into(),
            media_path: database.with_extension("ts").to_string_lossy().into_owned(),
            duration_ms: 30_000,
            readiness: AssetReadiness::Ready,
        })
        .unwrap();
    store
        .write_underwriting_spot(&spot(1), None, "operator", 100)
        .unwrap();
    store
        .write_underwriting_flight(&flight(), None, "operator", 101)
        .unwrap();
    let schedule = ScheduleItem {
        item_id: SCHEDULE.into(),
        revision: 1,
        channel_id: CHANNEL.into(),
        asset_id: ASSET.into(),
        title: "Underwriting".into(),
        starts_at_unix_ms: 120_000,
        duration_ms: 30_000,
        state: ScheduleState::Draft,
    };
    store
        .write_schedule_item(&ScheduleItemWrite {
            item: schedule.clone(),
            changed_by: "operator".into(),
            occurred_at_unix_ms: 102,
        })
        .unwrap();
    store
        .write_underwriting_placement(
            &placement(PLACEMENT, 120_000),
            SCHEDULE,
            1,
            None,
            "operator",
            103,
        )
        .unwrap();

    let mut current_spot = spot(2);
    current_spot.duration_ms = 60_000;
    store
        .write_underwriting_spot(&current_spot, Some(1), "operator", 104)
        .unwrap();
    let mut schedule_v2 = schedule;
    schedule_v2.revision = 2;
    store
        .write_schedule_item(&ScheduleItemWrite {
            item: schedule_v2,
            changed_by: "operator".into(),
            occurred_at_unix_ms: 105,
        })
        .unwrap();
    let mut placement_v2 = placement(PLACEMENT, 120_000);
    placement_v2.revision = 2;
    store
        .write_underwriting_placement(&placement_v2, SCHEDULE, 2, Some(1), "operator", 106)
        .unwrap();
    assert_eq!(
        store
            .list_underwriting_placement_history(PLACEMENT)
            .unwrap()
            .iter()
            .map(|entry| entry.schedule_item_revision)
            .collect::<Vec<_>>(),
        vec![1, 2]
    );

    let second_spot = UnderwritingSpot {
        spot_id: SPOT_TWO.into(),
        sponsor_id: SPONSOR_TWO.into(),
        asset_id: ASSET.into(),
        label: "Second sponsor".into(),
        duration_ms: 30_000,
        revision: 1,
    };
    store
        .write_underwriting_spot(&second_spot, None, "operator", 107)
        .unwrap();
    let mut second_flight = flight();
    second_flight.flight_id = FLIGHT_TWO.into();
    second_flight.sponsor_id = SPONSOR_TWO.into();
    second_flight.spot_id = SPOT_TWO.into();
    store
        .write_underwriting_flight(&second_flight, None, "operator", 108)
        .unwrap();
    let mut second_placement = placement(PLACEMENT_THREE, 120_000);
    second_placement.flight_id = FLIGHT_TWO.into();
    assert!(matches!(
        store.write_underwriting_placement(&second_placement, SCHEDULE, 2, None, "operator", 109),
        Err(UnderwritingWriteError::Conflict { .. })
    ));
}

#[test]
fn audited_commit_gate_preserves_exact_underwriting_binding() {
    let database = database_path("commit-gate");
    let media_path = database.with_extension("ts");
    std::fs::write(&media_path, b"underwriting media").unwrap();
    let store = StationStore::open(&database).unwrap();
    store
        .put_media_asset(&MediaAsset {
            asset_id: ASSET.into(),
            media_path: media_path.to_string_lossy().into_owned(),
            duration_ms: 30_000,
            readiness: AssetReadiness::Ready,
        })
        .unwrap();
    store
        .write_underwriting_spot(&spot(1), None, "operator", 100)
        .unwrap();
    store
        .write_underwriting_flight(&flight(), None, "operator", 101)
        .unwrap();
    let schedule = ScheduleItem {
        item_id: SCHEDULE.into(),
        revision: 1,
        channel_id: CHANNEL.into(),
        asset_id: ASSET.into(),
        title: "Underwriting".into(),
        starts_at_unix_ms: 120_000,
        duration_ms: 30_000,
        state: ScheduleState::Draft,
    };
    store
        .write_schedule_item(&ScheduleItemWrite {
            item: schedule,
            changed_by: "operator".into(),
            occurred_at_unix_ms: 102,
        })
        .unwrap();
    store
        .write_underwriting_placement(
            &placement(PLACEMENT, 120_000),
            SCHEDULE,
            1,
            None,
            "operator",
            103,
        )
        .unwrap();

    let report = store
        .commit_schedule(
            "underwriting-report",
            "underwriting-plan",
            SCHEDULE,
            "operator",
            104,
            "Reviewed underwriting binding.",
        )
        .unwrap();
    assert_eq!(report.schedule_item_revision, 2);
    assert_eq!(
        store.read_schedule_item(SCHEDULE).unwrap().unwrap().state,
        ScheduleState::Committed
    );
    let mut placement_v2 = placement(PLACEMENT, 120_000);
    placement_v2.revision = 2;
    store
        .write_underwriting_placement(&placement_v2, SCHEDULE, 2, Some(1), "operator", 105)
        .unwrap();
    assert_eq!(
        store
            .list_underwriting_placement_history(PLACEMENT)
            .unwrap()
            .len(),
        2
    );
}

#[test]
fn flight_and_placement_backdated_cas_roll_back_without_history_change() {
    let database = database_path("backdated-cas");
    let store = StationStore::open(&database).unwrap();
    store
        .put_media_asset(&MediaAsset {
            asset_id: ASSET.into(),
            media_path: database.with_extension("ts").to_string_lossy().into_owned(),
            duration_ms: 30_000,
            readiness: AssetReadiness::Ready,
        })
        .unwrap();
    store
        .write_underwriting_spot(&spot(1), None, "operator", 100)
        .unwrap();
    store
        .write_underwriting_flight(&flight(), None, "operator", 110)
        .unwrap();
    let mut flight_v2 = flight();
    flight_v2.revision = 2;
    flight_v2.placement.max_airings = 2;
    assert!(
        matches!(store.write_underwriting_flight(&flight_v2, Some(1), "operator", 109), Err(UnderwritingWriteError::Invalid(message)) if message.contains("precedes"))
    );
    assert_eq!(
        store
            .read_underwriting_flight(FLIGHT)
            .unwrap()
            .unwrap()
            .revision,
        1
    );
    store
        .write_underwriting_flight(&flight_v2, Some(1), "operator", 111)
        .unwrap();

    let schedule = ScheduleItem {
        item_id: SCHEDULE.into(),
        revision: 1,
        channel_id: CHANNEL.into(),
        asset_id: ASSET.into(),
        title: "Underwriting".into(),
        starts_at_unix_ms: 120_000,
        duration_ms: 30_000,
        state: ScheduleState::Draft,
    };
    store
        .write_schedule_item(&ScheduleItemWrite {
            item: schedule.clone(),
            changed_by: "operator".into(),
            occurred_at_unix_ms: 112,
        })
        .unwrap();
    let mut placement_v1 = placement(PLACEMENT, 120_000);
    placement_v1.flight_revision = 2;
    store
        .write_underwriting_placement(&placement_v1, SCHEDULE, 1, None, "operator", 120)
        .unwrap();
    let mut schedule_v2 = schedule;
    schedule_v2.revision = 2;
    store
        .write_schedule_item(&ScheduleItemWrite {
            item: schedule_v2,
            changed_by: "operator".into(),
            occurred_at_unix_ms: 121,
        })
        .unwrap();
    let mut placement_v2 = placement_v1;
    placement_v2.revision = 2;
    assert!(
        matches!(store.write_underwriting_placement(&placement_v2, SCHEDULE, 2, Some(1), "operator", 119), Err(UnderwritingWriteError::Invalid(message)) if message.contains("precedes"))
    );
    assert_eq!(
        store
            .list_underwriting_placement_history(PLACEMENT)
            .unwrap()
            .len(),
        1
    );
    store
        .write_underwriting_placement(&placement_v2, SCHEDULE, 2, Some(1), "operator", 122)
        .unwrap();
    assert_eq!(
        store
            .list_underwriting_placement_history(PLACEMENT)
            .unwrap()
            .len(),
        2
    );
}

#[test]
fn classifies_only_creation_and_revision_fences_as_conflicts() {
    let database = database_path("error-classification");
    let store = StationStore::open(&database).unwrap();
    store
        .put_media_asset(&MediaAsset {
            asset_id: ASSET.into(),
            media_path: database.with_extension("ts").to_string_lossy().into_owned(),
            duration_ms: 30_000,
            readiness: AssetReadiness::Ready,
        })
        .unwrap();
    store
        .write_underwriting_spot(&spot(1), None, "operator", 100)
        .unwrap();

    let mut semantic_mismatch = flight();
    semantic_mismatch.sponsor_id = SPONSOR_TWO.into();
    assert!(
        matches!(store.write_underwriting_flight(&semantic_mismatch, None, "operator", 101), Err(UnderwritingWriteError::Invalid(message)) if message.contains("exact current sponsor spot revision"))
    );

    let mut wrong_creation_revision = flight();
    wrong_creation_revision.revision = 2;
    assert!(matches!(
        store.write_underwriting_flight(&wrong_creation_revision, None, "operator", 102),
        Err(UnderwritingWriteError::Conflict {
            current_revision: None
        })
    ));
    store
        .write_underwriting_flight(&flight(), None, "operator", 103)
        .unwrap();

    let second_spot = UnderwritingSpot {
        spot_id: SPOT_TWO.into(),
        sponsor_id: SPONSOR_TWO.into(),
        asset_id: ASSET.into(),
        label: "Second sponsor".into(),
        duration_ms: 30_000,
        revision: 1,
    };
    store
        .write_underwriting_spot(&second_spot, None, "operator", 104)
        .unwrap();
    let mut rebound = flight();
    rebound.revision = 2;
    rebound.sponsor_id = SPONSOR_TWO.into();
    rebound.spot_id = SPOT_TWO.into();
    assert!(matches!(
        store.write_underwriting_flight(&rebound, Some(1), "operator", 105),
        Err(UnderwritingWriteError::Rebound)
    ));

    let mut flight_v2 = flight();
    flight_v2.revision = 2;
    assert!(matches!(
        store.write_underwriting_flight(&flight_v2, Some(9), "operator", 106),
        Err(UnderwritingWriteError::Conflict {
            current_revision: Some(1)
        })
    ));

    let schedule = ScheduleItem {
        item_id: SCHEDULE.into(),
        revision: 1,
        channel_id: CHANNEL.into(),
        asset_id: ASSET.into(),
        title: "Underwriting".into(),
        starts_at_unix_ms: 120_000,
        duration_ms: 30_000,
        state: ScheduleState::Draft,
    };
    store
        .write_schedule_item(&ScheduleItemWrite {
            item: schedule,
            changed_by: "operator".into(),
            occurred_at_unix_ms: 107,
        })
        .unwrap();
    let mut wrong_channel = placement(PLACEMENT, 120_000);
    wrong_channel.channel_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd".into();
    assert!(
        matches!(store.write_underwriting_placement(&wrong_channel, SCHEDULE, 1, None, "operator", 108), Err(UnderwritingWriteError::Invalid(message)) if message.contains("violates"))
    );
}
