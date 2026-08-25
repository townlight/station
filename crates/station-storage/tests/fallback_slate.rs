use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use image::ImageFormat;
use station_schedule::{ChannelConfiguration, OutputProfileId, OutputSinkConfiguration};
use station_storage::{FallbackSlateWriteError, StationStore};

const CHANNEL_ID: &str = "6bbf3d44-9c13-46ee-9e23-7628fd54e505";

fn root(label: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-fallback-slate-{label}-{nonce}"))
}

fn open_store(path: &Path) -> StationStore {
    std::fs::create_dir_all(path).unwrap();
    StationStore::open(path.join("station.db")).unwrap()
}

fn png(red: u8) -> Vec<u8> {
    let image = image::RgbaImage::from_pixel(4, 3, image::Rgba([red, 20, 30, 255]));
    let mut output = std::io::Cursor::new(Vec::new());
    image.write_to(&mut output, ImageFormat::Png).unwrap();
    output.into_inner()
}

fn put_channel(store: &StationStore, enabled: bool) {
    store
        .put_channel(&ChannelConfiguration {
            channel_id: CHANNEL_ID.into(),
            display_name: "Fallback slate test".into(),
            output_profile: OutputProfileId::Proof360p30,
            output_sinks: vec![OutputSinkConfiguration::required_udp(
                "2355d9d5-cda6-4f65-83ae-236121984885",
                "Proof output",
                "127.0.0.1:5500",
                -16.0,
            )],
            enabled,
        })
        .unwrap();
}

#[test]
fn fallback_slate_is_cas_versioned_append_audited_and_reopens() {
    let root = root("round-trip");
    let store = open_store(&root);
    put_channel(&store, false);
    let first_asset = store.admit_cg_image_asset(&png(10)).unwrap();
    let second_asset = store.admit_cg_image_asset(&png(40)).unwrap();

    let first = store
        .write_channel_fallback_slate(
            CHANNEL_ID,
            &first_asset.asset_id,
            0,
            "setup-admin",
            1_000,
            "0a0fb8b7-65a0-482b-a24c-f22615bf26c7",
        )
        .unwrap();
    assert_eq!(first.revision, 1);
    assert_eq!(first.image_asset, first_asset);

    let stale = store
        .write_channel_fallback_slate(
            CHANNEL_ID,
            &second_asset.asset_id,
            0,
            "setup-admin",
            1_001,
            "14a22e1c-d8d3-4bfe-ac29-6ba7b485e156",
        )
        .unwrap_err();
    assert_eq!(stale, FallbackSlateWriteError::Conflict(1));
    assert_eq!(
        store
            .list_channel_fallback_slate_audit(CHANNEL_ID)
            .unwrap()
            .len(),
        1
    );

    let second = store
        .write_channel_fallback_slate(
            CHANNEL_ID,
            &second_asset.asset_id,
            1,
            "setup-admin",
            2_000,
            "9d584a70-8329-4ccb-a233-b5bb5c3f99b1",
        )
        .unwrap();
    assert_eq!(second.revision, 2);
    assert_eq!(second.created_at_unix_ms, 1_000);
    assert_eq!(second.updated_at_unix_ms, 2_000);
    assert_eq!(
        store
            .list_channel_fallback_slate_audit(CHANNEL_ID)
            .unwrap()
            .len(),
        2
    );

    drop(store);
    let reopened = open_store(&root);
    assert_eq!(
        reopened.read_channel_fallback_slate(CHANNEL_ID).unwrap(),
        Some(second)
    );
    drop(reopened);
    let _ = std::fs::remove_dir_all(root);
}

#[test]
fn fallback_slate_rejects_running_channel_and_nonready_asset_without_mutation() {
    let root = root("guards");
    let store = open_store(&root);
    put_channel(&store, true);
    let asset = store.admit_cg_image_asset(&png(70)).unwrap();
    assert_eq!(
        store
            .write_channel_fallback_slate(
                CHANNEL_ID,
                &asset.asset_id,
                0,
                "setup-admin",
                1_000,
                "01bdfb16-22c2-44e4-9a27-09c3062ea6a4",
            )
            .unwrap_err(),
        FallbackSlateWriteError::MustBeStopped
    );

    put_channel(&store, false);
    let path = root
        .join("cg-images")
        .join(format!("{}.png", asset.asset_id));
    std::fs::remove_file(path).unwrap();
    assert_eq!(
        store
            .write_channel_fallback_slate(
                CHANNEL_ID,
                &asset.asset_id,
                0,
                "setup-admin",
                1_001,
                "cd1f16e2-9712-4df5-b9c2-d187bcc87f4c",
            )
            .unwrap_err(),
        FallbackSlateWriteError::ImageNotReady
    );
    assert_eq!(store.read_channel_fallback_slate(CHANNEL_ID).unwrap(), None);
    assert!(
        store
            .list_channel_fallback_slate_audit(CHANNEL_ID)
            .unwrap()
            .is_empty()
    );
    drop(store);
    let _ = std::fs::remove_dir_all(root);
}
