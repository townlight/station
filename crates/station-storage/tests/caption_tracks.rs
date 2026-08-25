use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use station_captions::{
    AssetCaptionCue, AssetCaptionTrackWrite, CaptionTrackSourceKind, CaptionTrackWriteError,
};
use station_domain::{ChannelRuntimeObservation, ChannelRuntimeState};
use station_schedule::{
    AssetReadiness, ChannelConfiguration, MediaAsset, OutputProfileId, OutputSinkConfiguration,
};
use station_storage::StationStore;

const ASSET_ID: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const TRACK_ID: &str = "65a26343-0339-4654-a92c-4bf9384f18df";
const CHANNEL_ID: &str = "169206ab-0e2d-4907-a3c7-a8ddc052f664";

fn root(label: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-caption-tracks-{label}-{nonce}"))
}

fn open_store(path: &Path) -> StationStore {
    std::fs::create_dir_all(path).unwrap();
    StationStore::open(path.join("station.db")).unwrap()
}

fn ready_asset() -> MediaAsset {
    MediaAsset {
        asset_id: ASSET_ID.into(),
        media_path: r"C:\TownLight\media\council.ts".into(),
        duration_ms: 10_000,
        readiness: AssetReadiness::Ready,
    }
}

fn write(expected_revision: u64, audit_id: &str) -> AssetCaptionTrackWrite {
    AssetCaptionTrackWrite {
        track_id: TRACK_ID.into(),
        asset_id: ASSET_ID.into(),
        language: "en-US".into(),
        service_number: 1,
        source_kind: CaptionTrackSourceKind::External,
        label: "Reviewed English".into(),
        cues: vec![
            AssetCaptionCue {
                cue_id: "bf0aa2ca-1488-42d2-8c1b-5d9179ee5d30".into(),
                start_ms: 1_000,
                duration_ms: 2_000,
                text: "Town Council".into(),
            },
            AssetCaptionCue {
                cue_id: "19a07d83-940a-455d-868e-dade63a4e201".into(),
                start_ms: 4_000,
                duration_ms: 1_000,
                text: "Regular Meeting".into(),
            },
        ],
        expected_revision,
        actor: "setup-admin".into(),
        occurred_at_unix_ms: 1_000 + expected_revision as i64,
        audit_id: audit_id.into(),
    }
}

fn put_channel(store: &StationStore) {
    store
        .put_channel(&ChannelConfiguration {
            channel_id: CHANNEL_ID.into(),
            display_name: "Caption test channel".into(),
            output_profile: OutputProfileId::Proof360p30,
            output_sinks: vec![OutputSinkConfiguration::required_udp(
                "23658df8-5401-473d-80b7-95bc84b44103",
                "Proof output",
                "127.0.0.1:5500",
                -24.0,
            )],
            enabled: true,
        })
        .unwrap();
}

#[test]
fn caption_track_is_asset_bound_cas_versioned_and_reopens() {
    let root = root("round-trip");
    let store = open_store(&root);
    store.put_media_asset(&ready_asset()).unwrap();
    let persisted = store
        .write_asset_caption_track(&write(0, "54691f24-e034-45bd-9ed5-31d9227a78aa"))
        .unwrap();

    assert_eq!(persisted.revision, 1);
    assert_eq!(
        store.read_asset_caption_track(TRACK_ID).unwrap(),
        Some(persisted.clone())
    );

    drop(store);
    let reopened = open_store(&root);
    assert_eq!(
        reopened.read_asset_caption_track(TRACK_ID).unwrap(),
        Some(persisted)
    );
    drop(reopened);
    let _ = std::fs::remove_dir_all(root);
}

#[test]
fn caption_track_updates_snapshot_every_revision_and_reject_stale_writes() {
    let root = root("audit");
    let store = open_store(&root);
    store.put_media_asset(&ready_asset()).unwrap();
    let first = write(0, "54691f24-e034-45bd-9ed5-31d9227a78aa");
    store.write_asset_caption_track(&first).unwrap();

    let mut second = write(1, "21a007bc-aa62-45e7-b534-fe885a70c4d8");
    second.cues[1].text = "Regular Meeting — approved".into();
    let updated = store.write_asset_caption_track(&second).unwrap();
    assert_eq!(updated.revision, 2);

    let stale = write(0, "3c978ab6-ce91-4d7d-96ea-d7a5efe00a2f");
    assert_eq!(
        store.write_asset_caption_track(&stale).unwrap_err(),
        CaptionTrackWriteError::Conflict(2)
    );

    let audit = store.list_asset_caption_track_audit(TRACK_ID).unwrap();
    assert_eq!(audit.len(), 2);
    assert_eq!(audit[0].revision, 1);
    assert_eq!(audit[0].cues, first.cues);
    assert_eq!(audit[1].revision, 2);
    assert_eq!(audit[1].cues, second.cues);
    assert_ne!(audit[0].cues_sha256, audit[1].cues_sha256);

    drop(store);
    let _ = std::fs::remove_dir_all(root);
}

#[test]
fn lists_every_language_and_service_for_the_exact_asset() {
    let root = root("list");
    let store = open_store(&root);
    store.put_media_asset(&ready_asset()).unwrap();
    store
        .write_asset_caption_track(&write(0, "54691f24-e034-45bd-9ed5-31d9227a78aa"))
        .unwrap();

    let mut spanish = write(0, "21a007bc-aa62-45e7-b534-fe885a70c4d8");
    spanish.track_id = "31f220a9-27d2-4b40-85bd-3ddb57869341".into();
    spanish.language = "es-US".into();
    spanish.service_number = 2;
    spanish.label = "Español revisado".into();
    store.write_asset_caption_track(&spanish).unwrap();

    let tracks = store.list_asset_caption_tracks(ASSET_ID).unwrap();
    assert_eq!(tracks.len(), 2);
    assert_eq!(tracks[0].service_number, 1);
    assert_eq!(tracks[1].service_number, 2);

    drop(store);
    let _ = std::fs::remove_dir_all(root);
}

#[test]
fn caption_track_writes_fail_closed_for_nonready_on_air_or_rebound_assets() {
    let root = root("guards");
    let store = open_store(&root);

    let mut missing_asset = ready_asset();
    missing_asset.asset_id = "b".repeat(64);
    missing_asset.readiness = AssetReadiness::Missing;
    store.put_media_asset(&missing_asset).unwrap();
    let mut missing_write = write(0, "cd2ce90c-e999-4b1f-979d-24e628061025");
    missing_write.asset_id = missing_asset.asset_id.clone();
    assert_eq!(
        store.write_asset_caption_track(&missing_write).unwrap_err(),
        CaptionTrackWriteError::AssetNotReady
    );

    store.put_media_asset(&ready_asset()).unwrap();
    store
        .write_asset_caption_track(&write(0, "54691f24-e034-45bd-9ed5-31d9227a78aa"))
        .unwrap();

    let mut occupied = write(0, "21a007bc-aa62-45e7-b534-fe885a70c4d8");
    occupied.track_id = "31f220a9-27d2-4b40-85bd-3ddb57869341".into();
    assert_eq!(
        store.write_asset_caption_track(&occupied).unwrap_err(),
        CaptionTrackWriteError::SlotOccupied {
            track_id: TRACK_ID.into(),
            revision: 1,
        }
    );

    let mut second_asset = ready_asset();
    second_asset.asset_id = "c".repeat(64);
    store.put_media_asset(&second_asset).unwrap();
    let mut rebound = write(1, "3c978ab6-ce91-4d7d-96ea-d7a5efe00a2f");
    rebound.asset_id = second_asset.asset_id;
    assert!(matches!(
        store.write_asset_caption_track(&rebound),
        Err(CaptionTrackWriteError::Invalid(_))
    ));

    put_channel(&store);
    store
        .put_channel_runtime(&ChannelRuntimeObservation {
            channel_id: CHANNEL_ID.into(),
            state: ChannelRuntimeState::OnAir,
            on_air_asset_id: Some(ASSET_ID.into()),
            state_started_at_unix_ms: 2_000,
            heartbeat_at_unix_ms: 2_001,
            detail: Some("caption track guard".into()),
        })
        .unwrap();
    assert_eq!(
        store
            .write_asset_caption_track(&write(1, "10b18a15-3376-430d-b0f8-a22344016df1",))
            .unwrap_err(),
        CaptionTrackWriteError::AssetOnAir
    );
    assert_eq!(
        store
            .read_asset_caption_track(TRACK_ID)
            .unwrap()
            .unwrap()
            .revision,
        1
    );

    drop(store);
    let _ = std::fs::remove_dir_all(root);
}

#[test]
fn failed_audit_append_rolls_back_the_entire_caption_revision() {
    let root = root("rollback");
    let store = open_store(&root);
    store.put_media_asset(&ready_asset()).unwrap();
    let first = write(0, "54691f24-e034-45bd-9ed5-31d9227a78aa");
    let persisted = store.write_asset_caption_track(&first).unwrap();

    let mut duplicate_audit = write(1, &first.audit_id);
    duplicate_audit.cues[0].text = "This must roll back".into();
    assert!(matches!(
        store.write_asset_caption_track(&duplicate_audit),
        Err(CaptionTrackWriteError::Storage(_))
    ));
    assert_eq!(
        store.read_asset_caption_track(TRACK_ID).unwrap(),
        Some(persisted)
    );
    assert_eq!(
        store
            .list_asset_caption_track_audit(TRACK_ID)
            .unwrap()
            .len(),
        1
    );

    drop(store);
    let _ = std::fs::remove_dir_all(root);
}
