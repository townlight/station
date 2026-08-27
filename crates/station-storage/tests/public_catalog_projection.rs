use station_catalog::{CatalogAssetRecord, CatalogEditorialState};
use station_metadata::{
    AssetMetadataRecord, AssetMetadataValue, MetadataFieldDefinition, MetadataFieldType,
    MetadataFieldValue, MetadataProjectionValue, MetadataSchema,
};
use station_schedule::{AssetReadiness, MediaAsset};
use station_storage::{PublicCatalogFilter, StationStore};
use std::ffi::{CString, c_char, c_int, c_void};
use std::path::{Path, PathBuf};
use std::ptr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

const ASSET: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const SCHEMA: &str = "11111111-2222-4333-8444-555555555555";
const FIELD: &str = "22222222-2222-4333-8444-555555555555";
const RECORD: &str = "33333333-2222-4333-8444-555555555555";
static SEQUENCE: AtomicU64 = AtomicU64::new(0);
const SQLITE_OK: c_int = 0;
const SQLITE_OPEN_READWRITE: c_int = 2;
#[link(name = "winsqlite3")]
unsafe extern "C" {
    fn sqlite3_open_v2(
        filename: *const c_char,
        database: *mut *mut c_void,
        flags: c_int,
        vfs: *const c_char,
    ) -> c_int;
    fn sqlite3_exec(
        database: *mut c_void,
        sql: *const c_char,
        callback: *mut c_void,
        arg: *mut c_void,
        error: *mut *mut c_char,
    ) -> c_int;
    fn sqlite3_close(database: *mut c_void) -> c_int;
}
fn raw_sql(path: &Path, sql: &str) {
    assert_eq!(raw_sql_status(path, sql), SQLITE_OK);
}

fn raw_sql_status(path: &Path, sql: &str) -> c_int {
    let filename = CString::new(path.to_string_lossy().as_bytes()).unwrap();
    let sql = CString::new(sql).unwrap();
    let mut db = ptr::null_mut();
    assert_eq!(
        unsafe {
            sqlite3_open_v2(
                filename.as_ptr(),
                &mut db,
                SQLITE_OPEN_READWRITE,
                ptr::null(),
            )
        },
        SQLITE_OK
    );
    let status = unsafe {
        sqlite3_exec(
            db,
            sql.as_ptr(),
            ptr::null_mut(),
            ptr::null_mut(),
            ptr::null_mut(),
        )
    };
    assert_eq!(unsafe { sqlite3_close(db) }, SQLITE_OK);
    status
}

fn database() -> PathBuf {
    std::env::temp_dir().join(format!(
        "townlight-public-catalog-{}-{}.db",
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos(),
        SEQUENCE.fetch_add(1, Ordering::Relaxed)
    ))
}

#[test]
fn generation_ledger_rejects_rollback_and_capacity_overflow_atomically() {
    let path = database();
    let store = StationStore::open(&path).unwrap();
    store.begin_public_catalog_boot_epoch().unwrap();
    raw_sql(
        &path,
        "UPDATE public_catalog_generation SET generation=1000001 WHERE singleton=1",
    );
    assert_ne!(
        raw_sql_status(
            &path,
            "UPDATE public_catalog_generation SET generation=1 WHERE singleton=1"
        ),
        SQLITE_OK
    );
    let asset = "c".repeat(64);
    assert!(
        store
            .put_media_asset(&MediaAsset {
                asset_id: asset.clone(),
                media_path: r"C:\private\capacity.ts".into(),
                duration_ms: 1_000,
                readiness: AssetReadiness::Ready,
            })
            .unwrap_err()
            .contains("capacity")
    );
    assert_eq!(store.read_media_asset(&asset).unwrap(), None);
}

#[test]
fn projects_only_published_ready_assets_with_exact_public_facets() {
    let path = database();
    let store = StationStore::open(&path).unwrap();
    store
        .put_media_asset(&MediaAsset {
            asset_id: ASSET.into(),
            media_path: r"C:\private\meeting.ts".into(),
            duration_ms: 1_000,
            readiness: AssetReadiness::Ready,
        })
        .unwrap();
    let draft = CatalogAssetRecord {
        asset_sha256: ASSET.into(),
        title: "Council".into(),
        meeting_body: Some("Secret body".into()),
        editorial_state: CatalogEditorialState::Draft,
        published_at_utc_ms: None,
        revision: 1,
    };
    store
        .write_catalog_asset(ASSET, &draft, None, "actor", 1)
        .unwrap();
    let schema = MetadataSchema {
        schema_id: SCHEMA.into(),
        revision: 1,
        fields: vec![MetadataFieldDefinition {
            field_id: FIELD.into(),
            key: "topic".into(),
            label: "Topic".into(),
            description: None,
            required: true,
            public: true,
            faceted: true,
            field_type: MetadataFieldType::Text {
                min_chars: 1,
                max_chars: 20,
            },
        }],
    };
    store
        .write_custom_metadata_schema(&schema, None, "actor", 2)
        .unwrap();
    store
        .write_asset_metadata_record(
            &AssetMetadataRecord {
                metadata_id: RECORD.into(),
                asset_id: ASSET.into(),
                schema_id: SCHEMA.into(),
                schema_revision: 1,
                revision: 1,
                values: vec![AssetMetadataValue {
                    field_id: FIELD.into(),
                    value: MetadataFieldValue::Text("budget".into()),
                }],
            },
            None,
            "actor",
            3,
        )
        .unwrap();
    let mut published = draft;
    published.revision = 2;
    published.editorial_state = CatalogEditorialState::Published;
    published.published_at_utc_ms = Some(4);
    store
        .write_catalog_asset(ASSET, &published, Some(1), "actor", 4)
        .unwrap();

    let second = "b".repeat(64);
    store
        .put_media_asset(&MediaAsset {
            asset_id: second.clone(),
            media_path: r"C:\private\second.ts".into(),
            duration_ms: 2_000,
            readiness: AssetReadiness::Ready,
        })
        .unwrap();
    let second_draft = CatalogAssetRecord {
        asset_sha256: second.clone(),
        title: "Second".into(),
        meeting_body: None,
        editorial_state: CatalogEditorialState::Draft,
        published_at_utc_ms: None,
        revision: 1,
    };
    store
        .write_catalog_asset(&second, &second_draft, None, "actor", 5)
        .unwrap();
    let mut second_published = second_draft;
    second_published.revision = 2;
    second_published.editorial_state = CatalogEditorialState::Published;
    second_published.published_at_utc_ms = Some(6);
    store
        .write_catalog_asset(&second, &second_published, Some(1), "actor", 6)
        .unwrap();
    let first_page = store.read_public_catalog(None, 1, &[]).unwrap();
    let continuation = first_page.next_cursor.clone().unwrap();
    drop(store);
    let store = StationStore::open(&path).unwrap();
    assert_eq!(
        store
            .read_public_catalog(Some(&continuation), 1, &[])
            .unwrap()
            .items
            .len(),
        1
    );
    let mut changed = store.read_catalog_asset(&second).unwrap().unwrap();
    changed.revision = 3;
    changed.title = "Changed".into();
    store
        .write_catalog_asset(&second, &changed, Some(2), "actor", 7)
        .unwrap();
    assert!(
        store
            .read_public_catalog(Some(&continuation), 1, &[])
            .unwrap_err()
            .contains("cursor stale")
    );

    let page = store.read_public_catalog(None, 10, &[]).unwrap();
    assert_eq!(page.items.len(), 2);
    assert_eq!(page.next_cursor, None);
    assert_ne!(page.items[0].asset_id, ASSET);
    let council = page
        .items
        .iter()
        .find(|item| item.title == "Council")
        .unwrap();
    assert_eq!(council.duration_ms, 1_000);
    assert_eq!(council.facets[0].field_key, "topic");
    let filtered = store
        .read_public_catalog(
            None,
            10,
            &[PublicCatalogFilter {
                field_key: "topic".into(),
                value: MetadataProjectionValue::Text("budget".into()),
            }],
        )
        .unwrap();
    assert_eq!(filtered.items, vec![council.clone()]);

    drop(store);
    let reopened = StationStore::open(&path).unwrap();
    assert_eq!(
        reopened
            .read_public_catalog(None, 10, &[])
            .unwrap()
            .items
            .len(),
        2
    );
    raw_sql(&path, "DELETE FROM public_catalog_identities");
    assert!(reopened.read_public_catalog(None, 10, &[]).is_err());
    drop(reopened);
    let repaired = StationStore::open(&path).unwrap();
    assert_eq!(
        repaired
            .read_public_catalog(None, 10, &[])
            .unwrap()
            .items
            .len(),
        2
    );
    drop(repaired);
    raw_sql(
        &path,
        "DROP TRIGGER public_discovery_catalog_h;CREATE TRIGGER public_discovery_catalog_h AFTER INSERT ON catalog_asset_history BEGIN SELECT 1;END",
    );
    let trigger_corrupt = StationStore::open(&path).unwrap();
    assert!(trigger_corrupt.read_public_catalog(None, 10, &[]).is_err());
}
