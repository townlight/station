use std::ffi::{CString, c_char, c_int, c_void};
use std::path::{Path, PathBuf};
use std::ptr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use station_catalog::{CatalogAssetRecord, CatalogEditorialState};
use station_schedule::{AssetReadiness, MediaAsset};
use station_storage::{CatalogAssetWriteError, StationStore};

const ASSET: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const OTHER_ASSET: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
static SEQUENCE: AtomicU64 = AtomicU64::new(0);

const SQLITE_OK: c_int = 0;
const SQLITE_OPEN_READWRITE: c_int = 2;

#[link(name = "winsqlite3")]
unsafe extern "C" {
    fn sqlite3_open_v2(
        filename: *const c_char,
        database: *mut *mut c_void,
        flags: c_int,
        virtual_file_system: *const c_char,
    ) -> c_int;
    fn sqlite3_exec(
        database: *mut c_void,
        sql: *const c_char,
        callback: *mut c_void,
        callback_argument: *mut c_void,
        error_message: *mut *mut c_char,
    ) -> c_int;
    fn sqlite3_close(database: *mut c_void) -> c_int;
}

fn raw_sql_status(database_path: &Path, sql: &str) -> c_int {
    let filename = CString::new(database_path.to_string_lossy().as_bytes()).unwrap();
    let sql = CString::new(sql).unwrap();
    let mut database = ptr::null_mut();
    // SAFETY: inputs are live C strings and the opened handle is closed below.
    let open = unsafe {
        sqlite3_open_v2(
            filename.as_ptr(),
            &mut database,
            SQLITE_OPEN_READWRITE,
            ptr::null(),
        )
    };
    assert_eq!(open, SQLITE_OK);
    // SAFETY: the database handle is live, the SQL is a valid C string, and no callback is used.
    let status = unsafe {
        sqlite3_exec(
            database,
            sql.as_ptr(),
            ptr::null_mut(),
            ptr::null_mut(),
            ptr::null_mut(),
        )
    };
    // SAFETY: this handle was opened above and has not yet been closed.
    assert_eq!(unsafe { sqlite3_close(database) }, SQLITE_OK);
    status
}

fn temporary_database(name: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let sequence = SEQUENCE.fetch_add(1, Ordering::Relaxed);
    std::env::temp_dir().join(format!("townlight-catalog-{name}-{nonce}-{sequence}.db"))
}

fn draft(revision: u64) -> CatalogAssetRecord {
    draft_for(ASSET, revision)
}

fn draft_for(asset_sha256: &str, revision: u64) -> CatalogAssetRecord {
    CatalogAssetRecord {
        asset_sha256: asset_sha256.into(),
        title: "City Council".into(),
        meeting_body: Some("City Council".into()),
        editorial_state: CatalogEditorialState::Draft,
        published_at_utc_ms: None,
        revision,
    }
}

fn admit_media(store: &StationStore, asset_sha256: &str) {
    store
        .put_media_asset(&MediaAsset {
            asset_id: asset_sha256.into(),
            media_path: format!(r"C:\media\{asset_sha256}.ts"),
            duration_ms: 1_000,
            readiness: AssetReadiness::Ready,
        })
        .unwrap();
}

#[test]
fn current_exact_history_and_first_publication_time_round_trip() {
    let store = StationStore::open(temporary_database("roundtrip")).unwrap();
    admit_media(&store, ASSET);
    store
        .write_catalog_asset(ASSET, &draft(1), None, "catalog-admin", 10)
        .unwrap();

    let mut published = draft(2);
    published.editorial_state = CatalogEditorialState::Published;
    published.published_at_utc_ms = Some(20);
    store
        .write_catalog_asset(ASSET, &published, Some(1), "catalog-admin", 20)
        .unwrap();

    let mut withdrawn = published.clone();
    withdrawn.editorial_state = CatalogEditorialState::Withdrawn;
    withdrawn.revision = 3;
    store
        .write_catalog_asset(ASSET, &withdrawn, Some(2), "meeting-operator", 30)
        .unwrap();

    let mut republished = withdrawn.clone();
    republished.editorial_state = CatalogEditorialState::Published;
    republished.revision = 4;
    store
        .write_catalog_asset(ASSET, &republished, Some(3), "catalog-admin", 40)
        .unwrap();

    assert_eq!(
        store.read_catalog_asset(ASSET).unwrap(),
        Some(republished.clone())
    );
    assert_eq!(
        store.list_catalog_assets(8).unwrap(),
        vec![republished.clone()]
    );
    assert_eq!(
        store.read_catalog_asset_revision(ASSET, 1).unwrap(),
        Some(draft(1))
    );
    let history = store.list_catalog_asset_history(ASSET, 8).unwrap();
    assert_eq!(history.len(), 4);
    assert_eq!(history[0].changed_by, "catalog-admin");
    assert_eq!(history[0].occurred_at_utc_ms, 40);
    assert_eq!(history[0].record, republished);
    assert_eq!(history[3].changed_by, "catalog-admin");
    assert_eq!(history[3].occurred_at_utc_ms, 10);
    assert_eq!(history[3].record, draft(1));

    let newest_two = store.list_catalog_asset_history(ASSET, 2).unwrap();
    assert_eq!(newest_two.len(), 2);
    assert_eq!(newest_two[0].record.revision, 4);
    assert_eq!(newest_two[1].record.revision, 3);
}

#[test]
fn catalog_writes_reject_assets_without_media_dependencies() {
    let store = StationStore::open(temporary_database("missing-media")).unwrap();
    assert_eq!(
        store.write_catalog_asset(ASSET, &draft(1), None, "catalog-admin", 10),
        Err(CatalogAssetWriteError::Invalid(
            "catalog asset media dependency does not exist".into()
        ))
    );
    assert_eq!(store.read_catalog_asset(ASSET).unwrap(), None);
}

#[test]
fn writes_fail_closed_for_cas_rebound_invalid_time_and_invalid_authority() {
    let store = StationStore::open(temporary_database("write-failures")).unwrap();
    admit_media(&store, ASSET);
    assert_eq!(
        store.write_catalog_asset(ASSET, &draft(1), Some(1), "catalog-admin", 10),
        Err(CatalogAssetWriteError::Conflict {
            current_revision: None
        })
    );
    assert_eq!(
        store.write_catalog_asset(OTHER_ASSET, &draft(1), None, "catalog-admin", 10),
        Err(CatalogAssetWriteError::Rebound)
    );
    store
        .write_catalog_asset(ASSET, &draft(1), None, "catalog-admin", 10)
        .unwrap();

    let mut next = draft(2);
    next.title = "City Council meeting".into();
    assert_eq!(
        store.write_catalog_asset(ASSET, &next, Some(0), "catalog-admin", 20),
        Err(CatalogAssetWriteError::Conflict {
            current_revision: Some(1)
        })
    );
    assert!(matches!(
        store.write_catalog_asset(ASSET, &next, Some(1), "catalog-admin", 9),
        Err(CatalogAssetWriteError::Invalid(_))
    ));
    assert!(matches!(
        store.write_catalog_asset(ASSET, &next, Some(1), " bad actor", 20),
        Err(CatalogAssetWriteError::Invalid(_))
    ));

    let mut forged_publication = draft(2);
    forged_publication.editorial_state = CatalogEditorialState::Published;
    forged_publication.published_at_utc_ms = Some(19);
    assert!(matches!(
        store.write_catalog_asset(ASSET, &forged_publication, Some(1), "catalog-admin", 20),
        Err(CatalogAssetWriteError::Invalid(_))
    ));
    assert_eq!(store.read_catalog_asset(ASSET).unwrap(), Some(draft(1)));
}

#[test]
fn read_queries_are_canonical_and_bounded() {
    let store = StationStore::open(temporary_database("bounded-reads")).unwrap();
    assert!(store.read_catalog_asset(&ASSET.to_uppercase()).is_err());
    assert!(store.read_catalog_asset_revision(ASSET, 0).is_err());
    assert!(store.list_catalog_assets(0).is_err());
    assert!(store.list_catalog_assets(1_001).is_err());
    assert!(
        store
            .list_catalog_asset_page(Some(&ASSET.to_uppercase()), 1)
            .is_err()
    );
    assert!(store.list_catalog_asset_page(None, 0).is_err());
    assert!(store.list_catalog_asset_page(None, 1_001).is_err());
    assert!(store.list_catalog_asset_history(ASSET, 0).is_err());
    assert!(store.list_catalog_asset_history(ASSET, 1_001).is_err());
}

#[test]
fn current_catalog_pages_are_exclusive_ordered_and_lossless() {
    let store = StationStore::open(temporary_database("current-pages")).unwrap();
    let third_asset = "c".repeat(64);
    for asset_sha256 in [ASSET, OTHER_ASSET, third_asset.as_str()] {
        admit_media(&store, asset_sha256);
        store
            .write_catalog_asset(
                asset_sha256,
                &draft_for(asset_sha256, 1),
                None,
                "catalog-admin",
                10,
            )
            .unwrap();
    }

    let first = store.list_catalog_asset_page(None, 1).unwrap();
    assert_eq!(first.records, vec![draft_for(ASSET, 1)]);
    assert_eq!(first.next_after_asset_sha256.as_deref(), Some(ASSET));

    let second = store
        .list_catalog_asset_page(first.next_after_asset_sha256.as_deref(), 1)
        .unwrap();
    assert_eq!(second.records, vec![draft_for(OTHER_ASSET, 1)]);
    assert_eq!(second.next_after_asset_sha256.as_deref(), Some(OTHER_ASSET));

    let third = store
        .list_catalog_asset_page(second.next_after_asset_sha256.as_deref(), 1)
        .unwrap();
    assert_eq!(third.records, vec![draft_for(&third_asset, 1)]);
    assert_eq!(third.next_after_asset_sha256, None);

    let traversed = first
        .records
        .iter()
        .chain(&second.records)
        .chain(&third.records)
        .map(|record| record.asset_sha256.as_str())
        .collect::<Vec<_>>();
    assert_eq!(traversed, vec![ASSET, OTHER_ASSET, third_asset.as_str()]);
}

#[test]
fn current_and_history_rows_detect_tampering_and_history_is_append_only() {
    let database = temporary_database("tampering");
    {
        let store = StationStore::open(&database).unwrap();
        admit_media(&store, ASSET);
        store
            .write_catalog_asset(ASSET, &draft(1), None, "catalog-admin", 10)
            .unwrap();
    }
    assert_ne!(
        raw_sql_status(
            &database,
            "UPDATE catalog_asset_history SET changed_by='tamper'"
        ),
        SQLITE_OK
    );
    assert_ne!(
        raw_sql_status(&database, "DELETE FROM catalog_asset_history"),
        SQLITE_OK
    );
    assert_eq!(
        raw_sql_status(&database, "UPDATE catalog_assets SET title='tampered'"),
        SQLITE_OK
    );
    let store = StationStore::open(&database).unwrap();
    assert!(
        store
            .read_catalog_asset(ASSET)
            .unwrap_err()
            .contains("disagree")
    );
}

#[test]
fn current_must_equal_the_latest_fully_validated_history_snapshot() {
    let database = temporary_database("history-parity");
    {
        let store = StationStore::open(&database).unwrap();
        admit_media(&store, ASSET);
        store
            .write_catalog_asset(ASSET, &draft(1), None, "catalog-admin", 10)
            .unwrap();
    }
    assert_eq!(
        raw_sql_status(
            &database,
            "DROP TRIGGER catalog_asset_history_no_update;\
             UPDATE catalog_asset_history SET title='History title', \
               record_json=replace(record_json,'\"title\":\"City Council\"','\"title\":\"History title\"')"
        ),
        SQLITE_OK
    );
    let store = StationStore::open(&database).unwrap();
    assert!(
        store
            .read_catalog_asset(ASSET)
            .unwrap_err()
            .contains("historical snapshot")
    );
    assert!(store.list_catalog_assets(8).is_err());
}
