use std::time::{SystemTime, UNIX_EPOCH};

use station_storage::StationStore;
use station_update::{
    PreparedSlotRecord, ReleaseManifest, StagedPackageRecord, UpdateCheckOutcome,
    UpdateCheckRecord, UpdateCheckTrigger,
};

fn record(checked_at_unix_ms: i64, version: &str) -> UpdateCheckRecord {
    UpdateCheckRecord {
        checked_at_unix_ms,
        next_automatic_check_at_unix_ms: checked_at_unix_ms + 3_600_000,
        trigger: UpdateCheckTrigger::Manual,
        outcome: UpdateCheckOutcome::UpdateAvailable,
        current_version: "0.1.0".into(),
        updater_version: "0.1.0".into(),
        release_channel: "stable".into(),
        offered_release: Some(ReleaseManifest {
            schema_version: 1,
            product: "TownLight Station".into(),
            release_channel: "stable".into(),
            key_id: "release-2026-a".into(),
            version: version.into(),
            source_commit: "0123456789abcdef0123456789abcdef01234567".into(),
            published_at_unix_ms: checked_at_unix_ms - 1_000,
            expires_at_unix_ms: checked_at_unix_ms + 86_400_000,
            minimum_updater_version: "0.1.0".into(),
            package_url: format!("https://releases.example.test/station/{version}.tlu"),
            package_size_bytes: 500_000,
            package_sha256: "A".repeat(64),
            release_notes_markdown: format!("# {version}\n\nRelease notes."),
        }),
        error: None,
    }
}

#[test]
fn persists_the_latest_verified_update_check_and_refuses_stale_overwrite() {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let root = std::env::temp_dir().join(format!("townlight-update-store-{nonce}"));
    std::fs::create_dir_all(&root).unwrap();
    let database = root.join("station.db");
    let store = StationStore::open(&database).unwrap();
    assert_eq!(store.read_update_check().unwrap(), None);

    let newest = record(1_800_000_000_000, "0.2.0");
    assert_eq!(store.write_update_check(&newest).unwrap(), newest);
    let stale = record(1_799_999_999_999, "0.1.9");
    assert!(store.write_update_check(&stale).is_err());
    assert_eq!(store.read_update_check().unwrap(), Some(newest.clone()));
    drop(store);

    let reopened = StationStore::open(&database).unwrap();
    assert_eq!(reopened.read_update_check().unwrap(), Some(newest));
    reopened.clear_update_check().unwrap();
    assert_eq!(reopened.read_update_check().unwrap(), None);
    drop(reopened);

    let reopened = StationStore::open(&database).unwrap();
    assert_eq!(reopened.read_update_check().unwrap(), None);
    drop(reopened);
    let _ = std::fs::remove_dir_all(root);
}

#[test]
fn stores_a_bounded_failure_without_forging_an_available_release() {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let root = std::env::temp_dir().join(format!("townlight-update-failure-{nonce}"));
    std::fs::create_dir_all(&root).unwrap();
    let database = root.join("station.db");
    let store = StationStore::open(&database).unwrap();
    let failure = UpdateCheckRecord {
        checked_at_unix_ms: 1_800_000_000_000,
        next_automatic_check_at_unix_ms: 1_800_003_600_000,
        trigger: UpdateCheckTrigger::Automatic,
        outcome: UpdateCheckOutcome::Failed,
        current_version: "0.1.0".into(),
        updater_version: "0.1.0".into(),
        release_channel: "stable".into(),
        offered_release: None,
        error: Some("release check failed before a trusted response was received".into()),
    };
    assert_eq!(store.write_update_check(&failure).unwrap(), failure);
    assert_eq!(store.read_update_check().unwrap(), Some(failure));
    drop(store);
    let _ = std::fs::remove_dir_all(root);
}

#[test]
fn persists_only_the_newest_valid_staged_package_record() {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let root = std::env::temp_dir().join(format!("townlight-update-stage-store-{nonce}"));
    std::fs::create_dir_all(&root).unwrap();
    let database = root.join("station.db");
    let store = StationStore::open(&database).unwrap();
    let manifest = record(1_800_000_000_000, "0.2.0").offered_release.unwrap();
    let staged = StagedPackageRecord {
        staged_at_unix_ms: 1_800_000_001_000,
        package_file_name: format!("{}.tlu", manifest.package_sha256.to_ascii_lowercase()),
        manifest,
    };
    assert_eq!(store.write_staged_package(&staged).unwrap(), staged);
    drop(store);

    let reopened = StationStore::open(&database).unwrap();
    assert_eq!(
        reopened.read_staged_package().unwrap(),
        Some(staged.clone())
    );
    let mut stale = staged.clone();
    stale.staged_at_unix_ms -= 1;
    assert!(reopened.write_staged_package(&stale).is_err());
    reopened.clear_staged_package().unwrap();
    assert_eq!(reopened.read_staged_package().unwrap(), None);
    drop(reopened);
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn persists_only_the_newest_valid_prepared_slot_record() {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let root = std::env::temp_dir().join(format!("townlight-update-prepare-store-{nonce}"));
    std::fs::create_dir_all(&root).unwrap();
    let database = root.join("station.db");
    let store = StationStore::open(&database).unwrap();
    let manifest = record(1_800_000_000_000, "0.2.0").offered_release.unwrap();
    let prepared = PreparedSlotRecord {
        prepared_at_unix_ms: 1_800_000_001_000,
        package_file_name: format!("{}.tlu", manifest.package_sha256.to_ascii_lowercase()),
        slot_directory_name: format!("slot-{}-{}", manifest.version, manifest.source_commit),
        manifest,
    };
    assert_eq!(store.write_prepared_slot(&prepared).unwrap(), prepared);
    drop(store);

    let reopened = StationStore::open(&database).unwrap();
    assert_eq!(
        reopened.read_prepared_slot().unwrap(),
        Some(prepared.clone())
    );
    let mut stale = prepared.clone();
    stale.prepared_at_unix_ms -= 1;
    assert!(reopened.write_prepared_slot(&stale).is_err());
    reopened.clear_prepared_slot().unwrap();
    assert_eq!(reopened.read_prepared_slot().unwrap(), None);
    drop(reopened);
    std::fs::remove_dir_all(root).unwrap();
}
