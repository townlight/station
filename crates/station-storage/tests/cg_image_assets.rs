use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use image::ImageFormat;
use station_cg::{
    CgAuditEvent, CgAuditEventKind, CgBoard, CgContentSource, CgImageReadiness, CgResolvedContent,
    CgZoneConfig, CgZoneKind,
};
use station_schedule::{ChannelConfiguration, OutputProfileId, OutputSinkConfiguration};
use station_storage::StationStore;

const CHANNEL_ID: &str = "5d85189a-cd1f-43dc-a619-66a2aeb09ac4";
const BOARD_ID: &str = "94a0dc01-d314-4b55-b2e4-cba39cc3f1dc";
const ZONE_ID: &str = "8f4f0f70-5c30-48c6-819a-fb22b34353c5";

fn root(label: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-cg-image-{label}-{nonce}"))
}

fn png() -> Vec<u8> {
    let image = image::DynamicImage::new_rgba8(2, 3);
    let mut output = std::io::Cursor::new(Vec::new());
    image.write_to(&mut output, ImageFormat::Png).unwrap();
    output.into_inner()
}

fn jpeg() -> Vec<u8> {
    let image = image::DynamicImage::new_rgb8(3, 2);
    let mut output = std::io::Cursor::new(Vec::new());
    image.write_to(&mut output, ImageFormat::Jpeg).unwrap();
    output.into_inner()
}

fn open_store(path: &Path) -> StationStore {
    std::fs::create_dir_all(path).unwrap();
    StationStore::open(path.join("station.db")).unwrap()
}

fn channel(store: &StationStore) {
    store
        .put_channel(&ChannelConfiguration {
            channel_id: CHANNEL_ID.into(),
            display_name: "Image test".into(),
            output_profile: OutputProfileId::Proof360p30,
            output_sinks: vec![OutputSinkConfiguration::required_udp(
                "f103968a-640b-42dc-8084-3fe9e847ff7f",
                "Proof output",
                "127.0.0.1:5500",
                -16.0,
            )],
            enabled: false,
        })
        .unwrap();
}

fn audit(kind: CgAuditEventKind, id: &str, time: i64) -> CgAuditEvent {
    CgAuditEvent {
        audit_id: id.into(),
        board_id: Some(BOARD_ID.into()),
        channel_id: CHANNEL_ID.into(),
        event_kind: kind,
        operator_id: "test".into(),
        occurred_at_unix_ms: time,
        details: Default::default(),
    }
}

#[test]
fn admits_png_and_jpeg_idempotently_and_reopens_with_no_part_files() {
    let root = root("admit");
    let store = open_store(&root);
    let png = png();
    let jpeg = jpeg();
    let first = store.admit_cg_image_asset(&png).unwrap();
    assert_eq!(first.mime_type, "image/png");
    assert_eq!(store.admit_cg_image_asset(&png).unwrap(), first);
    let second = store.admit_cg_image_asset(&jpeg).unwrap();
    assert_eq!(second.mime_type, "image/jpeg");
    drop(store);
    let reopened = open_store(&root);
    assert_eq!(
        reopened.read_cg_image_asset(&first.asset_id).unwrap(),
        Some(first.clone())
    );
    assert_eq!(reopened.list_cg_image_assets().unwrap().len(), 2);
    let entries = std::fs::read_dir(root.join("cg-images")).unwrap();
    assert!(
        entries
            .flatten()
            .all(|entry| !entry.file_name().to_string_lossy().contains(".part-"))
    );
    let _ = std::fs::remove_dir_all(root);
}

#[test]
fn rejects_malformed_content_before_creating_a_managed_file() {
    let root = root("reject");
    let store = open_store(&root);
    assert!(store.admit_cg_image_asset(b"not-an-image").is_err());
    let mut oversized = png();
    oversized.resize(8 * 1024 * 1024 + 1, 0);
    assert!(store.admit_cg_image_asset(&oversized).is_err());
    assert!(
        root.join("cg-images")
            .read_dir()
            .map(|entries| entries.count() == 0)
            .unwrap_or(true)
    );
    let _ = std::fs::remove_dir_all(root);
}

#[test]
fn snapshot_downgrades_deleted_and_corrupt_ready_files() {
    let root = root("snapshot");
    let store = open_store(&root);
    channel(&store);
    let board = CgBoard {
        board_id: BOARD_ID.into(),
        channel_id: CHANNEL_ID.into(),
        template_id: "l-bar".into(),
        active: true,
        background_audio_asset_id: None,
        revision: 1,
        created_at_unix_ms: 1_000,
        updated_at_unix_ms: 1_000,
        created_by: "test".into(),
    };
    store
        .write_cg_board(
            &board,
            0,
            &audit(
                CgAuditEventKind::BoardCreated,
                "7bb32e14-fe69-47cb-a921-dc3fc84499bf",
                1_000,
            ),
        )
        .unwrap();
    let asset = store.admit_cg_image_asset(&png()).unwrap();
    let zone = CgZoneConfig {
        zone_id: ZONE_ID.into(),
        board_id: BOARD_ID.into(),
        template_zone_key: "sidebar".into(),
        zone_kind: CgZoneKind::Sidebar,
        content_source: CgContentSource::Image {
            asset_id: asset.asset_id.clone(),
        },
        rotation_seconds: None,
        approval_required: false,
        allowed_tags: Vec::new(),
        revision: 1,
        created_at_unix_ms: 1_000,
        updated_at_unix_ms: 1_000,
    };
    store
        .write_cg_zone(
            &zone,
            0,
            &audit(
                CgAuditEventKind::ZoneCreated,
                "31086e51-b940-450d-a9c2-5a31807ecc02",
                1_000,
            ),
        )
        .unwrap_or_else(|error| panic!("zone write: {error:?}"));
    let loaded = store
        .load_cg_snapshot_inputs(CHANNEL_ID, 2_000)
        .unwrap()
        .resolve()
        .unwrap();
    assert!(matches!(
        loaded.zones[0].content,
        CgResolvedContent::Image { .. }
    ));
    assert_eq!(store.cg_image_verification_count(), 1);
    let _ = store.load_cg_snapshot_inputs(CHANNEL_ID, 2_000).unwrap();
    assert_eq!(store.cg_image_verification_count(), 1);
    let image_path = root
        .join("cg-images")
        .join(format!("{}.png", asset.asset_id));
    std::fs::remove_file(&image_path).unwrap();
    let missing_inputs = store.load_cg_snapshot_inputs(CHANNEL_ID, 2_001).unwrap();
    assert_eq!(
        missing_inputs.image_assets[0].readiness,
        CgImageReadiness::Missing
    );
    let missing = missing_inputs.resolve().unwrap();
    assert!(matches!(
        missing.zones[0].content,
        CgResolvedContent::Placeholder { .. }
    ));
    std::fs::write(&image_path, b"corrupt").unwrap();
    drop(store);
    let store = open_store(&root);
    let rejected_inputs = store.load_cg_snapshot_inputs(CHANNEL_ID, 2_002).unwrap();
    assert_eq!(
        rejected_inputs.image_assets[0].readiness,
        CgImageReadiness::Rejected
    );
    assert_eq!(store.cg_image_verification_count(), 1);
    let rejected_again = store.load_cg_snapshot_inputs(CHANNEL_ID, 2_003).unwrap();
    assert_eq!(
        rejected_again.image_assets[0].readiness,
        CgImageReadiness::Rejected
    );
    assert_eq!(store.cg_image_verification_count(), 1);
    let rejected = rejected_inputs.resolve().unwrap();
    assert!(matches!(
        rejected.zones[0].content,
        CgResolvedContent::Placeholder { .. }
    ));
    drop(store);
    let _ = std::fs::remove_dir_all(root);
}
