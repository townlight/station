use std::path::PathBuf;

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

fn database_path(label: &str) -> PathBuf {
    let directory = std::env::temp_dir().join(format!("townlight-underwriting-{label}"));
    let _ = std::fs::remove_dir_all(&directory);
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
