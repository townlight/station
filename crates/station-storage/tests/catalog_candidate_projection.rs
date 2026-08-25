use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use station_catalog::{CatalogAssetRecord, CatalogEditorialState};
use station_metadata::{
    AssetMetadataRecord, AssetMetadataValue, ControlledChoice, MetadataFieldDefinition,
    MetadataFieldType, MetadataFieldValue, MetadataSchema,
};
use station_schedule::{AssetReadiness, MediaAsset};
use station_schedule_automation::MetadataValue;
use station_storage::StationStore;

static SEQUENCE: AtomicU64 = AtomicU64::new(0);

fn temporary_database(name: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let sequence = SEQUENCE.fetch_add(1, Ordering::Relaxed);
    std::env::temp_dir().join(format!("townlight-candidate-{name}-{nonce}-{sequence}.db"))
}

fn sha(letter: char) -> String {
    letter.to_string().repeat(64)
}

fn id(value: u32) -> String {
    format!("00000000-0000-4000-8000-{value:012}")
}

fn draft(asset_sha256: &str) -> CatalogAssetRecord {
    CatalogAssetRecord {
        asset_sha256: asset_sha256.into(),
        title: "Council meeting".into(),
        meeting_body: Some("City Council".into()),
        editorial_state: CatalogEditorialState::Draft,
        published_at_utc_ms: None,
        revision: 1,
    }
}

fn admit_media(store: &StationStore, asset_sha256: &str, readiness: AssetReadiness) {
    store
        .put_media_asset(&MediaAsset {
            asset_id: asset_sha256.into(),
            media_path: format!("C:\\station\\{asset_sha256}.ts"),
            duration_ms: 3_600_000,
            readiness,
        })
        .unwrap();
}

fn publish(store: &StationStore, asset_sha256: &str, ready: bool) {
    admit_media(
        store,
        asset_sha256,
        if ready {
            AssetReadiness::Ready
        } else {
            AssetReadiness::Missing
        },
    );
    let draft = draft(asset_sha256);
    store
        .write_catalog_asset(asset_sha256, &draft, None, "catalog-admin", 10)
        .unwrap();
    let mut published = draft;
    published.editorial_state = CatalogEditorialState::Published;
    published.published_at_utc_ms = Some(20);
    published.revision = 2;
    store
        .write_catalog_asset(asset_sha256, &published, Some(1), "catalog-admin", 20)
        .unwrap();
}

fn schema(asset_sha256: &str) -> (MetadataSchema, AssetMetadataRecord) {
    let field = |value: u32, key: &str, field_type| MetadataFieldDefinition {
        field_id: id(value),
        key: key.into(),
        label: key.into(),
        description: None,
        required: true,
        public: false,
        faceted: true,
        field_type,
    };
    let schema = MetadataSchema {
        schema_id: id(100),
        revision: 1,
        fields: vec![
            field(
                101,
                "notes",
                MetadataFieldType::Text {
                    min_chars: 1,
                    max_chars: 2_048,
                },
            ),
            field(
                102,
                "signed_time",
                MetadataFieldType::DateTime {
                    minimum_unix_ms: Some(-1_000),
                    maximum_unix_ms: Some(1_000),
                },
            ),
            field(
                103,
                "topic",
                MetadataFieldType::ControlledChoice {
                    choices: vec![ControlledChoice {
                        choice_id: id(104),
                        key: "council".into(),
                        label: "Council".into(),
                    }],
                },
            ),
        ],
    };
    let record = AssetMetadataRecord {
        metadata_id: id(105),
        asset_id: asset_sha256.into(),
        schema_id: schema.schema_id.clone(),
        schema_revision: 1,
        revision: 1,
        values: vec![
            AssetMetadataValue {
                field_id: id(101),
                value: MetadataFieldValue::Text("x".repeat(2_048)),
            },
            AssetMetadataValue {
                field_id: id(102),
                value: MetadataFieldValue::DateTimeUnixMs(-1),
            },
            AssetMetadataValue {
                field_id: id(103),
                value: MetadataFieldValue::ControlledChoice(id(104)),
            },
        ],
    };
    (schema, record)
}

#[test]
fn snapshot_maps_published_ready_s22_facets_and_omits_ineligible_assets() {
    let store = StationStore::open(temporary_database("mapping")).unwrap();
    let included = sha('a');
    let draft_only = sha('b');
    let unready_media = sha('c');
    let withdrawn = sha('d');
    publish(&store, &included, true);

    admit_media(&store, &draft_only, AssetReadiness::Ready);
    store
        .write_catalog_asset(&draft_only, &draft(&draft_only), None, "catalog-admin", 10)
        .unwrap();
    publish(&store, &unready_media, false);
    publish(&store, &withdrawn, true);
    let mut withdrawn_record = store.read_catalog_asset(&withdrawn).unwrap().unwrap();
    withdrawn_record.editorial_state = CatalogEditorialState::Withdrawn;
    withdrawn_record.revision = 3;
    store
        .write_catalog_asset(&withdrawn, &withdrawn_record, Some(2), "catalog-admin", 30)
        .unwrap();

    let (schema, record) = schema(&included);
    store
        .write_custom_metadata_schema(&schema, None, "setup-admin", 10)
        .unwrap();
    store
        .write_asset_metadata_record(&record, None, "setup-admin", 20)
        .unwrap();

    let first = store.snapshot_catalog_candidates().unwrap();
    let second = store.snapshot_catalog_candidates().unwrap();
    assert_eq!(first, second);
    assert_eq!(first.candidates.len(), 1);
    let candidate = &first.candidates[0];
    assert_eq!(candidate.asset_sha256, included);
    assert_eq!(candidate.duration_ms, 3_600_000);
    assert!(candidate.ready && candidate.published);
    assert_eq!(candidate.published_at_utc_ms, 20);
    assert_eq!(
        candidate.indexed_custom_metadata,
        BTreeMap::from([
            ("notes".into(), MetadataValue::Text("x".repeat(2_048))),
            ("signed_time".into(), MetadataValue::DateTimeUnixMs(-1)),
            ("topic".into(), MetadataValue::Choice("council".into())),
        ])
    );

    let original_provenance = candidate.custom_metadata_authority_sha256.clone();
    let mut revised = store.read_catalog_asset(&included).unwrap().unwrap();
    revised.title = "Council meeting revised".into();
    revised.revision = 3;
    store
        .write_catalog_asset(&included, &revised, Some(2), "catalog-admin", 30)
        .unwrap();
    let changed = store.snapshot_catalog_candidates().unwrap();
    assert_ne!(
        changed.candidates[0].custom_metadata_authority_sha256,
        original_provenance
    );
    assert_ne!(
        changed.candidate_projection_sha256,
        first.candidate_projection_sha256
    );
}

#[test]
fn duplicate_facet_keys_across_schemas_fail_closed() {
    let store = StationStore::open(temporary_database("duplicate-facet")).unwrap();
    let asset = sha('a');
    publish(&store, &asset, true);
    let (first_schema, first_record) = schema(&asset);
    store
        .write_custom_metadata_schema(&first_schema, None, "setup-admin", 10)
        .unwrap();
    store
        .write_asset_metadata_record(&first_record, None, "setup-admin", 20)
        .unwrap();

    let second_schema = MetadataSchema {
        schema_id: id(200),
        revision: 1,
        fields: vec![MetadataFieldDefinition {
            field_id: id(201),
            key: "topic".into(),
            label: "Topic".into(),
            description: None,
            required: true,
            public: false,
            faceted: true,
            field_type: MetadataFieldType::Text {
                min_chars: 1,
                max_chars: 32,
            },
        }],
    };
    let second_record = AssetMetadataRecord {
        metadata_id: id(202),
        asset_id: asset,
        schema_id: second_schema.schema_id.clone(),
        schema_revision: 1,
        revision: 1,
        values: vec![AssetMetadataValue {
            field_id: id(201),
            value: MetadataFieldValue::Text("duplicate".into()),
        }],
    };
    store
        .write_custom_metadata_schema(&second_schema, None, "setup-admin", 30)
        .unwrap();
    store
        .write_asset_metadata_record(&second_record, None, "setup-admin", 40)
        .unwrap();

    assert!(
        store
            .snapshot_catalog_candidates()
            .unwrap_err()
            .contains("duplicate metadata facet")
    );
}
