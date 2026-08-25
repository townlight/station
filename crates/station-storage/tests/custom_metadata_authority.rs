use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use station_metadata::{
    AssetMetadataRecord, AssetMetadataValue, MetadataFieldDefinition, MetadataFieldType,
    MetadataFieldValue, MetadataSchema,
};
use station_storage::StationStore;

const SCHEMA_ID: &str = "11111111-2222-4333-8444-555555555555";
const FIELD_ID: &str = "22222222-2222-4333-8444-555555555555";
const RECORD_ID: &str = "33333333-2222-4333-8444-555555555555";
const ASSET_ID: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
static DATABASE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

fn temporary_database(name: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let sequence = DATABASE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    std::env::temp_dir().join(format!("townlight-{name}-{nonce}-{sequence}.db"))
}

fn schema(revision: u64) -> MetadataSchema {
    MetadataSchema {
        schema_id: SCHEMA_ID.into(),
        revision,
        fields: vec![MetadataFieldDefinition {
            field_id: FIELD_ID.into(),
            key: "program_name".into(),
            label: "Program name".into(),
            description: None,
            required: true,
            public: true,
            faceted: true,
            field_type: MetadataFieldType::Text {
                min_chars: 1,
                max_chars: 120,
            },
        }],
    }
}

fn record(revision: u64, schema_revision: u64) -> AssetMetadataRecord {
    AssetMetadataRecord {
        metadata_id: RECORD_ID.into(),
        asset_id: ASSET_ID.into(),
        schema_id: SCHEMA_ID.into(),
        schema_revision,
        revision,
        values: vec![AssetMetadataValue {
            field_id: FIELD_ID.into(),
            value: MetadataFieldValue::Text("Town hall".into()),
        }],
    }
}

#[test]
fn schemas_and_exact_revision_values_round_trip_with_complete_history() {
    let database = temporary_database("custom-metadata-round-trip");
    let store = StationStore::open(&database).unwrap();
    store
        .write_custom_metadata_schema(&schema(1), None, "setup-admin", 10)
        .unwrap();
    store
        .write_asset_metadata_record(&record(1, 1), None, "setup-admin", 20)
        .unwrap();

    let mut revised_schema = schema(2);
    revised_schema.fields[0].label = "Program title".into();
    store
        .write_custom_metadata_schema(&revised_schema, Some(1), "setup-admin", 30)
        .unwrap();
    let mut revised_record = record(2, 2);
    revised_record.values[0].value = MetadataFieldValue::Text("Council meeting".into());
    store
        .write_asset_metadata_record(&revised_record, Some(1), "setup-admin", 40)
        .unwrap();

    assert_eq!(
        store.read_custom_metadata_schema(SCHEMA_ID).unwrap(),
        Some(revised_schema)
    );
    assert_eq!(store.list_custom_metadata_schemas().unwrap().len(), 1);
    assert_eq!(
        store
            .list_custom_metadata_schema_history(SCHEMA_ID, 128)
            .unwrap()
            .len(),
        2
    );
    assert_eq!(
        store.read_asset_metadata_record(RECORD_ID).unwrap(),
        Some(revised_record)
    );
    assert_eq!(
        store.list_asset_metadata_records(ASSET_ID).unwrap().len(),
        1
    );
    assert_eq!(
        store
            .list_asset_metadata_record_history(RECORD_ID, 128)
            .unwrap()
            .len(),
        2
    );

    drop(store);
    let reopened = StationStore::open(&database).unwrap();
    assert_eq!(
        reopened
            .read_asset_metadata_record(RECORD_ID)
            .unwrap()
            .unwrap()
            .revision,
        2
    );
    drop(reopened);
    for candidate in [
        database.clone(),
        PathBuf::from(format!("{}-wal", database.display())),
        PathBuf::from(format!("{}-shm", database.display())),
    ] {
        let _ = std::fs::remove_file(candidate);
    }
}

#[test]
fn metadata_authority_rejects_stale_rebinding_and_unknown_schema_revisions() {
    let database = temporary_database("custom-metadata-fail-closed");
    let store = StationStore::open(&database).unwrap();
    store
        .write_custom_metadata_schema(&schema(1), None, "setup-admin", 10)
        .unwrap();
    assert!(
        store
            .write_custom_metadata_schema(&schema(2), None, "setup-admin", 20)
            .is_err()
    );
    store
        .write_asset_metadata_record(&record(1, 1), None, "setup-admin", 20)
        .unwrap();
    assert!(
        store
            .write_asset_metadata_record(&record(2, 9), Some(1), "setup-admin", 30)
            .is_err()
    );
    let mut rebound = record(2, 1);
    rebound.asset_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb".into();
    assert!(
        store
            .write_asset_metadata_record(&rebound, Some(1), "setup-admin", 30)
            .is_err()
    );
    assert!(
        store
            .list_custom_metadata_schema_history(SCHEMA_ID, 0)
            .is_err()
    );
    assert!(
        store
            .list_asset_metadata_record_history(RECORD_ID, 129)
            .is_err()
    );
    drop(store);
    for candidate in [
        database.clone(),
        PathBuf::from(format!("{}-wal", database.display())),
        PathBuf::from(format!("{}-shm", database.display())),
    ] {
        let _ = std::fs::remove_file(candidate);
    }
}
