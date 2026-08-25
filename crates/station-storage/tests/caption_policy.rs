use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use station_schedule::{ChannelConfiguration, OutputProfileId, OutputSinkConfiguration};
use station_storage::{CaptionPolicyWriteError, ChannelCaptionMode, StationStore};

const CHANNEL_ID: &str = "169206ab-0e2d-4907-a3c7-a8ddc052f664";

fn root(label: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-caption-policy-{label}-{nonce}"))
}

fn open_store(path: &Path) -> StationStore {
    std::fs::create_dir_all(path).unwrap();
    StationStore::open(path.join("station.db")).unwrap()
}

fn put_channel(store: &StationStore, enabled: bool) {
    store
        .put_channel(&ChannelConfiguration {
            channel_id: CHANNEL_ID.into(),
            display_name: "Caption policy test".into(),
            output_profile: OutputProfileId::Proof360p30,
            output_sinks: vec![OutputSinkConfiguration::required_udp(
                "23658df8-5401-473d-80b7-95bc84b44103",
                "Proof output",
                "127.0.0.1:5500",
                -24.0,
            )],
            enabled,
        })
        .unwrap();
}

#[test]
fn caption_policy_is_cas_versioned_audited_and_reopens() {
    let root = root("round-trip");
    let store = open_store(&root);
    put_channel(&store, false);
    let first = store
        .write_channel_caption_policy(
            CHANNEL_ID,
            ChannelCaptionMode::Cea708,
            1,
            "en-US",
            0,
            "setup-admin",
            1_000,
            "9d747cc3-9c69-4c29-8b46-cbca6e09cd26",
        )
        .unwrap();
    assert_eq!(first.revision, 1);
    assert_eq!(first.mode, ChannelCaptionMode::Cea708);
    assert_eq!(first.service_number, 1);
    assert_eq!(first.language, "en-US");

    assert_eq!(
        store
            .write_channel_caption_policy(
                CHANNEL_ID,
                ChannelCaptionMode::Disabled,
                1,
                "en-US",
                0,
                "setup-admin",
                1_001,
                "915d4da9-c547-4027-aac4-49eb51f89c41",
            )
            .unwrap_err(),
        CaptionPolicyWriteError::Conflict(1)
    );
    assert_eq!(
        store
            .list_channel_caption_policy_audit(CHANNEL_ID)
            .unwrap()
            .len(),
        1
    );

    drop(store);
    let reopened = open_store(&root);
    assert_eq!(
        reopened.read_channel_caption_policy(CHANNEL_ID).unwrap(),
        Some(first)
    );
    drop(reopened);
    let _ = std::fs::remove_dir_all(root);
}

#[test]
fn caption_policy_requires_a_stopped_channel_and_bounded_fields() {
    let root = root("guards");
    let store = open_store(&root);
    put_channel(&store, true);
    assert_eq!(
        store
            .write_channel_caption_policy(
                CHANNEL_ID,
                ChannelCaptionMode::Cea708,
                1,
                "en-US",
                0,
                "setup-admin",
                1_000,
                "9d747cc3-9c69-4c29-8b46-cbca6e09cd26",
            )
            .unwrap_err(),
        CaptionPolicyWriteError::MustBeStopped
    );

    put_channel(&store, false);
    assert!(matches!(
        store.write_channel_caption_policy(
            CHANNEL_ID,
            ChannelCaptionMode::Cea708,
            64,
            "not a language",
            0,
            "setup-admin",
            1_000,
            "9d747cc3-9c69-4c29-8b46-cbca6e09cd26",
        ),
        Err(CaptionPolicyWriteError::Invalid(_))
    ));
    assert_eq!(store.read_channel_caption_policy(CHANNEL_ID).unwrap(), None);
    drop(store);
    let _ = std::fs::remove_dir_all(root);
}
