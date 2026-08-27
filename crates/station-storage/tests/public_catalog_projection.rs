use station_catalog::{CatalogAssetRecord, CatalogEditorialState};
use station_metadata::{
    AssetMetadataRecord, AssetMetadataValue, MetadataFieldDefinition, MetadataFieldType,
    MetadataFieldValue, MetadataProjectionValue, MetadataSchema,
};
use station_schedule::{AssetReadiness, MediaAsset};
use station_storage::{CustomMetadataSchemaWriteError, PublicCatalogFilter, StationStore};
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

fn public_schema(schema_id: &str, start: usize, count: usize) -> MetadataSchema {
    MetadataSchema {
        schema_id: schema_id.into(),
        revision: 1,
        fields: (start..start + count)
            .map(|index| MetadataFieldDefinition {
                field_id: format!("00000000-0000-4000-8000-{index:012x}"),
                key: format!("public_{index:03}"),
                label: format!("Public {index}"),
                description: None,
                required: false,
                public: true,
                faceted: true,
                field_type: MetadataFieldType::Boolean,
            })
            .collect(),
    }
}

#[test]
fn schema_writes_reject_global_public_facet_collisions_and_cardinality_overflow() {
    let path = database();
    let store = StationStore::open(&path).unwrap();
    let first = public_schema("10000000-0000-4000-8000-000000000001", 0, 64);
    store
        .write_custom_metadata_schema(&first, None, "actor", 1)
        .unwrap();

    let mut collision = public_schema("10000000-0000-4000-8000-000000000002", 64, 1);
    collision.fields[0].key = first.fields[0].key.clone();
    assert!(matches!(
        store.write_custom_metadata_schema(&collision, None, "actor", 2),
        Err(CustomMetadataSchemaWriteError::Invalid(message)) if message.contains("public facet")
    ));

    let second = public_schema("10000000-0000-4000-8000-000000000003", 64, 64);
    store
        .write_custom_metadata_schema(&second, None, "actor", 3)
        .unwrap();
    let overflow = public_schema("10000000-0000-4000-8000-000000000004", 128, 1);
    assert!(matches!(
        store.write_custom_metadata_schema(&overflow, None, "actor", 4),
        Err(CustomMetadataSchemaWriteError::Invalid(message)) if message.contains("128")
    ));
}

#[test]
fn current_schema_visibility_retires_historical_facets_before_key_reuse() {
    let path = database();
    let store = StationStore::open(&path).unwrap();
    let asset = "e".repeat(64);
    store
        .put_media_asset(&MediaAsset {
            asset_id: asset.clone(),
            media_path: r"C:\private\visibility.ts".into(),
            duration_ms: 1_000,
            readiness: AssetReadiness::Ready,
        })
        .unwrap();
    let a_id = "20000000-0000-4000-8000-000000000001";
    let b_id = "20000000-0000-4000-8000-000000000002";
    let a1 = public_schema(a_id, 0, 128);
    store
        .write_custom_metadata_schema(&a1, None, "actor", 1)
        .unwrap();
    let a_record = AssetMetadataRecord {
        metadata_id: "30000000-0000-4000-8000-000000000001".into(),
        asset_id: asset.clone(),
        schema_id: a_id.into(),
        schema_revision: 1,
        revision: 1,
        values: a1
            .fields
            .iter()
            .map(|field| AssetMetadataValue {
                field_id: field.field_id.clone(),
                value: MetadataFieldValue::Boolean(true),
            })
            .collect(),
    };
    store
        .write_asset_metadata_record(&a_record, None, "actor", 2)
        .unwrap();
    let mut a2 = a1.clone();
    a2.revision = 2;
    for field in &mut a2.fields {
        field.public = false;
    }
    store
        .write_custom_metadata_schema(&a2, Some(1), "actor", 3)
        .unwrap();
    let b1 = public_schema(b_id, 0, 128);
    store
        .write_custom_metadata_schema(&b1, None, "actor", 4)
        .unwrap();
    let b_record = AssetMetadataRecord {
        metadata_id: "30000000-0000-4000-8000-000000000002".into(),
        asset_id: asset.clone(),
        schema_id: b_id.into(),
        schema_revision: 1,
        revision: 1,
        values: b1
            .fields
            .iter()
            .map(|field| AssetMetadataValue {
                field_id: field.field_id.clone(),
                value: MetadataFieldValue::Boolean(false),
            })
            .collect(),
    };
    store
        .write_asset_metadata_record(&b_record, None, "actor", 5)
        .unwrap();
    let draft = CatalogAssetRecord {
        asset_sha256: asset.clone(),
        title: "Visibility".into(),
        meeting_body: None,
        editorial_state: CatalogEditorialState::Draft,
        published_at_utc_ms: None,
        revision: 1,
    };
    store
        .write_catalog_asset(&asset, &draft, None, "actor", 6)
        .unwrap();
    let mut published = draft;
    published.revision = 2;
    published.editorial_state = CatalogEditorialState::Published;
    published.published_at_utc_ms = Some(7);
    store
        .write_catalog_asset(&asset, &published, Some(1), "actor", 7)
        .unwrap();
    let page = store.read_public_catalog(None, 10, &[]).unwrap();
    assert_eq!(page.items[0].facets.len(), 128);
    assert!(
        page.items[0]
            .facets
            .iter()
            .all(|facet| facet.value == MetadataProjectionValue::Boolean(false))
    );
}

#[test]
fn anonymous_query_structure_is_bounded_and_never_full_scans_identity_rows() {
    let source = include_str!("../src/public_catalog_projection.rs");
    let snapshot = source
        .split("fn public_snapshot")
        .nth(1)
        .unwrap()
        .split("fn public_facets")
        .next()
        .unwrap();
    assert!(snapshot.contains("LEFT JOIN public_catalog_identities"));
    assert!(snapshot.contains("MAX_CANDIDATES + 1"));
    assert!(!snapshot.contains("LIMIT 100001"));
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
    let metadata = AssetMetadataRecord {
        metadata_id: RECORD.into(),
        asset_id: ASSET.into(),
        schema_id: SCHEMA.into(),
        schema_revision: 1,
        revision: 1,
        values: vec![AssetMetadataValue {
            field_id: FIELD.into(),
            value: MetadataFieldValue::Text("budget".into()),
        }],
    };
    store
        .write_asset_metadata_record(&metadata, None, "actor", 3)
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

    let mut schema_129 = schema;
    for revision in 2..=129 {
        schema_129.revision = revision;
        store
            .write_custom_metadata_schema(
                &schema_129,
                Some(revision - 1),
                "actor",
                1_000 + revision as i64,
            )
            .unwrap();
    }
    let mut metadata_129 = metadata;
    metadata_129.schema_revision = 129;
    for revision in 2..=129 {
        metadata_129.revision = revision;
        store
            .write_asset_metadata_record(
                &metadata_129,
                Some(revision - 1),
                "actor",
                2_000 + revision as i64,
            )
            .unwrap();
    }
    assert_eq!(
        store
            .read_public_catalog(None, 10, &[])
            .unwrap()
            .items
            .len(),
        2
    );

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
    assert_ne!(
        raw_sql_status(&path, "DELETE FROM public_catalog_identities"),
        SQLITE_OK
    );
    assert_eq!(
        reopened
            .read_public_catalog(None, 10, &[])
            .unwrap()
            .items
            .len(),
        2
    );
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
    let mapping_reader = StationStore::open(&path).unwrap();
    raw_sql(&path, "DROP TRIGGER public_discovery_identity_no_delete");
    raw_sql(
        &path,
        "DELETE FROM public_catalog_identities WHERE asset_sha256='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'",
    );
    assert!(mapping_reader.read_public_catalog(None, 10, &[]).is_err());
    drop(mapping_reader);
    let repaired = StationStore::open(&path).unwrap();
    raw_sql(
        &path,
        "UPDATE custom_metadata_schemas SET record_json=replace(record_json,'Topic','Tampered') WHERE schema_id='11111111-2222-4333-8444-555555555555'",
    );
    assert!(repaired.read_public_catalog(None, 10, &[]).is_err());
    drop(repaired);
    raw_sql(
        &path,
        "DROP TRIGGER public_discovery_catalog_h;CREATE TRIGGER public_discovery_catalog_h AFTER INSERT ON catalog_asset_history BEGIN SELECT 1;END",
    );
    let trigger_corrupt = StationStore::open(&path).unwrap();
    assert!(trigger_corrupt.read_public_catalog(None, 10, &[]).is_err());
}
