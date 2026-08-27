use std::time::{SystemTime, UNIX_EPOCH};

use station_media_lifecycle::{
    AssetRecoveryAudit, AssetReplacementLink, AssetRetentionPolicy, AssetThumbnail,
    RetentionDisposition,
};
use station_schedule::{AssetReadiness, MediaAsset};
use station_storage::StationStore;

fn fixture_store(name: &str) -> (std::path::PathBuf, StationStore) {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let path = std::env::temp_dir().join(format!("townlight-media-lifecycle-{name}-{nonce}.db"));
    let store = StationStore::open(&path).unwrap();
    (path, store)
}

fn asset(identity: char, readiness: AssetReadiness) -> MediaAsset {
    MediaAsset {
        asset_id: identity.to_string().repeat(64),
        media_path: format!(r"C:\TownLight\media\{}.ts", identity),
        duration_ms: 1_000,
        readiness,
    }
}

fn indexed_asset(index: usize, readiness: AssetReadiness) -> MediaAsset {
    let asset_id = format!("{index:064x}");
    MediaAsset {
        media_path: format!(r"C:\TownLight\media\{asset_id}.ts"),
        asset_id,
        duration_ms: 1_000,
        readiness,
    }
}

#[test]
fn retention_is_cas_versioned_append_only_and_scanner_only_flags_review() {
    let (path, store) = fixture_store("retention");
    let media = asset('a', AssetReadiness::Ready);
    store.put_media_asset(&media).unwrap();
    let policy = AssetRetentionPolicy {
        asset_id: media.asset_id.clone(),
        revision: 1,
        permanent: false,
        review_after_unix_ms: Some(10),
        legal_hold: false,
        disposition: RetentionDisposition::Retained,
    };
    store
        .write_asset_retention_policy(&policy, 0, "setup-admin", 1)
        .unwrap();
    assert!(
        store
            .write_asset_retention_policy(&policy, 0, "setup-admin", 2)
            .is_err()
    );
    let scan = store.scan_retention_due(10, "retention-scanner").unwrap();
    assert_eq!(scan.review_required_asset_ids, vec![media.asset_id.clone()]);
    assert!(scan.held_asset_ids.is_empty());
    let current = store
        .read_asset_retention_policy(&media.asset_id)
        .unwrap()
        .unwrap();
    assert_eq!(current.revision, 2);
    assert_eq!(current.disposition, RetentionDisposition::ReviewRequired);
    assert_eq!(
        store
            .list_asset_retention_history(&media.asset_id, 8)
            .unwrap()
            .len(),
        2
    );
    assert_eq!(
        store
            .read_media_asset(&media.asset_id)
            .unwrap()
            .unwrap()
            .readiness,
        AssetReadiness::Ready
    );
    drop(store);
    let _ = std::fs::remove_file(path);
}

#[test]
fn retention_scanner_cannot_starve_due_work_behind_more_than_one_batch_of_permanent_assets() {
    let (path, store) = fixture_store("retention-starvation");
    for index in 1..=129 {
        let media = indexed_asset(index, AssetReadiness::Ready);
        store.put_media_asset(&media).unwrap();
        store
            .write_asset_retention_policy(
                &AssetRetentionPolicy {
                    asset_id: media.asset_id,
                    revision: 1,
                    permanent: true,
                    review_after_unix_ms: None,
                    legal_hold: false,
                    disposition: RetentionDisposition::Retained,
                },
                0,
                "setup-admin",
                1,
            )
            .unwrap();
    }
    let due = indexed_asset(130, AssetReadiness::Ready);
    store.put_media_asset(&due).unwrap();
    store
        .write_asset_retention_policy(
            &AssetRetentionPolicy {
                asset_id: due.asset_id.clone(),
                revision: 1,
                permanent: false,
                review_after_unix_ms: Some(10),
                legal_hold: false,
                disposition: RetentionDisposition::Retained,
            },
            0,
            "setup-admin",
            1,
        )
        .unwrap();

    let scan = store.scan_retention_due(10, "retention-scanner").unwrap();
    assert_eq!(scan.review_required_asset_ids, vec![due.asset_id]);

    drop(store);
    let _ = std::fs::remove_file(path);
}

#[test]
fn replacement_thumbnail_and_missing_relink_preserve_immutable_identity() {
    let (path, store) = fixture_store("identity");
    let predecessor = asset('a', AssetReadiness::Ready);
    let successor = asset('b', AssetReadiness::Ready);
    let missing = asset('c', AssetReadiness::Missing);
    store.put_media_asset(&predecessor).unwrap();
    store.put_media_asset(&successor).unwrap();
    store.put_media_asset(&missing).unwrap();
    let link = AssetReplacementLink {
        predecessor_asset_id: predecessor.asset_id.clone(),
        successor_asset_id: successor.asset_id.clone(),
        revision: 1,
        changed_by: "setup-admin".into(),
        occurred_at_unix_ms: 10,
    };
    store.create_asset_replacement_link(&link, 0).unwrap();
    assert!(store.create_asset_replacement_link(&link, 0).is_err());
    let thumbnail = AssetThumbnail {
        asset_id: successor.asset_id.clone(),
        thumbnail_sha256: "c".repeat(64),
        source_asset_sha256: successor.asset_id.clone(),
        generated_at_unix_ms: 12,
        byte_count: 77,
    };
    store.record_asset_thumbnail(&thumbnail).unwrap();
    store.record_asset_thumbnail(&thumbnail).unwrap();
    let changed_thumbnail = AssetThumbnail {
        thumbnail_sha256: "d".repeat(64),
        ..thumbnail.clone()
    };
    assert!(store.record_asset_thumbnail(&changed_thumbnail).is_err());
    let recovery = AssetRecoveryAudit {
        asset_id: missing.asset_id.clone(),
        revision: 1,
        recovered_asset_sha256: missing.asset_id.clone(),
        managed_path: r"C:\TownLight\media\aaaaaaaa.ts".into(),
        byte_count: 4_096,
        duration_ms: 1_000,
        changed_by: "setup-admin".into(),
        occurred_at_unix_ms: 13,
    };
    let relinked = store.relink_missing_media_asset(&recovery).unwrap();
    assert_eq!(relinked.asset_id, missing.asset_id);
    assert_eq!(relinked.readiness, AssetReadiness::Ready);
    assert_eq!(
        store
            .list_asset_recovery_history(&missing.asset_id, 8)
            .unwrap()
            .len(),
        1
    );
    assert!(
        store
            .relink_missing_media_asset(&AssetRecoveryAudit {
                revision: 2,
                managed_path: r"C:\TownLight\media\wrong.ts".into(),
                occurred_at_unix_ms: 14,
                ..recovery
            })
            .is_err()
    );
    drop(store);
    let _ = std::fs::remove_file(path);
}
