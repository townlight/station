use std::time::{SystemTime, UNIX_EPOCH};

use station_readiness::{REFERENCE_STATION_CATALOG_VERSION, ReferenceStationTier};
use station_storage::{ReferenceStationTierWrite, ReferenceStationTierWriteError, StationStore};

fn database_path(label: &str) -> std::path::PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-reference-tier-{label}-{nonce}.db"))
}

fn request(
    expected_revision: Option<u64>,
    tier: ReferenceStationTier,
    actor: &str,
    occurred_at_unix_ms: i64,
) -> ReferenceStationTierWrite {
    ReferenceStationTierWrite {
        expected_revision,
        catalog_version: REFERENCE_STATION_CATALOG_VERSION,
        tier,
        actor: actor.into(),
        occurred_at_unix_ms,
    }
}

#[test]
fn absent_first_write_persists_across_reopen_with_one_audit_event() {
    let database = database_path("first-write");
    let store = StationStore::open(&database).unwrap();
    assert_eq!(store.read_reference_station_tier_selection().unwrap(), None);

    let selection = store
        .write_reference_station_tier_selection(&request(
            None,
            ReferenceStationTier::Tier1Streaming,
            "setup-admin",
            1_800_000_000_000,
        ))
        .unwrap();
    assert_eq!(selection.revision, 1);
    assert_eq!(selection.created_at_unix_ms, 1_800_000_000_000);
    assert_eq!(selection.updated_at_unix_ms, 1_800_000_000_000);
    assert!(selection.uses_current_catalog());
    assert_eq!(store.list_reference_station_tier_audit().unwrap().len(), 1);
    drop(store);

    let reopened = StationStore::open(&database).unwrap();
    assert_eq!(
        reopened.read_reference_station_tier_selection().unwrap(),
        Some(selection)
    );
    drop(reopened);
    let _ = std::fs::remove_file(database);
}

#[test]
fn stale_cas_is_rejected_without_an_extra_audit_event() {
    let database = database_path("stale-cas");
    let store = StationStore::open(&database).unwrap();
    let first = store
        .write_reference_station_tier_selection(&request(
            None,
            ReferenceStationTier::Tier0BatchStream,
            "setup-admin",
            100,
        ))
        .unwrap();
    assert_eq!(first.revision, 1);

    let error = store
        .write_reference_station_tier_selection(&request(
            None,
            ReferenceStationTier::Tier1Streaming,
            "setup-admin",
            101,
        ))
        .unwrap_err();
    assert_eq!(
        error,
        ReferenceStationTierWriteError::Conflict {
            current_revision: Some(1)
        }
    );
    assert_eq!(store.list_reference_station_tier_audit().unwrap().len(), 1);
    assert_eq!(
        store.read_reference_station_tier_selection().unwrap(),
        Some(first)
    );
    drop(store);
    let _ = std::fs::remove_file(database);
}

#[test]
fn update_preserves_created_time_and_appends_exactly_one_audit_event() {
    let database = database_path("update-audit");
    let store = StationStore::open(&database).unwrap();
    let first = store
        .write_reference_station_tier_selection(&request(
            None,
            ReferenceStationTier::Tier0BatchStream,
            "setup-admin",
            100,
        ))
        .unwrap();
    let updated = store
        .write_reference_station_tier_selection(&request(
            Some(first.revision),
            ReferenceStationTier::CableSdi,
            "station-engineer",
            250,
        ))
        .unwrap();
    assert_eq!(updated.revision, 2);
    assert_eq!(updated.created_at_unix_ms, 100);
    assert_eq!(updated.updated_at_unix_ms, 250);
    assert_eq!(updated.updated_by, "station-engineer");
    let audit = store.list_reference_station_tier_audit().unwrap();
    assert_eq!(audit.len(), 2);
    assert_eq!(audit[0].revision, 1);
    assert_eq!(audit[1].revision, 2);
    assert_eq!(audit[1].tier, ReferenceStationTier::CableSdi);
    assert_eq!(audit[1].actor, "station-engineer");
    drop(store);
    let _ = std::fs::remove_file(database);
}

#[test]
fn old_catalog_write_is_rejected_before_any_selection_or_audit_is_created() {
    let database = database_path("old-catalog");
    let store = StationStore::open(&database).unwrap();
    let error = store
        .write_reference_station_tier_selection(&ReferenceStationTierWrite {
            expected_revision: None,
            catalog_version: REFERENCE_STATION_CATALOG_VERSION - 1,
            tier: ReferenceStationTier::Tier0BatchStream,
            actor: "setup-admin".into(),
            occurred_at_unix_ms: 100,
        })
        .unwrap_err();
    assert_eq!(
        error,
        ReferenceStationTierWriteError::CatalogVersionMismatch {
            expected: REFERENCE_STATION_CATALOG_VERSION,
            actual: REFERENCE_STATION_CATALOG_VERSION - 1,
        }
    );
    assert_eq!(store.read_reference_station_tier_selection().unwrap(), None);
    assert!(
        store
            .list_reference_station_tier_audit()
            .unwrap()
            .is_empty()
    );
    drop(store);
    let _ = std::fs::remove_file(database);
}
