use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use station_cg::{
    CgAuditEvent, CgAuditEventKind, CgBoard, CgBulletin, CgBulletinContent, CgBulletinState,
    CgContentSource, CgFeedItem, CgFeedItemApproval, CgFeedKind, CgFeedSource, CgFeedTrustTier,
    CgZoneConfig, CgZoneKind,
};
use station_schedule::{ChannelConfiguration, OutputProfileId, OutputSinkConfiguration};
use station_storage::{CgWriteError, StationStore};

const CHANNEL_ID: &str = "5d85189a-cd1f-43dc-a619-66a2aeb09ac4";
const BOARD_ID: &str = "94a0dc01-d314-4b55-b2e4-cba39cc3f1dc";
const ZONE_ID: &str = "8f4f0f70-5c30-48c6-819a-fb22b34353c5";
const FEED_ID: &str = "796ca80a-9cc2-4fc1-84a2-2a88d8f92f24";
const BULLETIN_ID: &str = "597f52ae-f16c-4ff8-ab32-60379cd950c4";
const SECOND_FEED_ID: &str = "9ac46d05-09d8-4ef8-9607-75c365b3274e";

#[test]
fn atomically_persists_revisioned_board_and_zone_authority_with_audit() {
    let database = database_path("cg-authority");
    let store = StationStore::open(&database).unwrap();
    put_channel(&store);

    let board = board(1, "l-bar", 1_000);
    let created = audit(
        "7bb32e14-fe69-47cb-a921-dc3fc84499bf",
        CgAuditEventKind::BoardCreated,
        1_000,
    );
    assert_eq!(store.write_cg_board(&board, 0, &created).unwrap(), board);
    assert_eq!(
        store.read_cg_board(CHANNEL_ID).unwrap(),
        Some(board.clone())
    );

    let zone = CgZoneConfig {
        zone_id: ZONE_ID.into(),
        board_id: BOARD_ID.into(),
        template_zone_key: "ticker".into(),
        zone_kind: CgZoneKind::Ticker,
        content_source: CgContentSource::Manual {
            text: "Road closure at 5th and Main until 8 PM.".into(),
        },
        rotation_seconds: None,
        approval_required: false,
        allowed_tags: vec!["public-safety".into()],
        revision: 1,
        created_at_unix_ms: 1_100,
        updated_at_unix_ms: 1_100,
    };
    let zone_created = audit(
        "31086e51-b940-450d-a9c2-5a31807ecc02",
        CgAuditEventKind::ZoneCreated,
        1_100,
    );
    assert_eq!(store.write_cg_zone(&zone, 0, &zone_created).unwrap(), zone);
    assert_eq!(store.list_cg_zones(BOARD_ID).unwrap(), vec![zone.clone()]);

    let mut updated = zone.clone();
    updated.content_source = CgContentSource::Manual {
        text: "Road reopened. Use normal routes.".into(),
    };
    updated.revision = 2;
    updated.updated_at_unix_ms = 1_200;
    let zone_updated = audit(
        "cc567945-5fc2-4344-8213-c8dab9adb045",
        CgAuditEventKind::ZoneUpdated,
        1_200,
    );
    store.write_cg_zone(&updated, 1, &zone_updated).unwrap();
    assert!(matches!(
        store.write_cg_zone(&updated, 1, &zone_updated),
        Err(CgWriteError::Conflict(2))
    ));
    let events = store.list_cg_audit_events(CHANNEL_ID, 20).unwrap();
    assert_eq!(events.len(), 3);
    assert_eq!(events[0], zone_updated);
    drop(store);

    let reopened = StationStore::open(&database).unwrap();
    assert_eq!(reopened.read_cg_board(CHANNEL_ID).unwrap(), Some(board));
    assert_eq!(reopened.read_cg_zone(ZONE_ID).unwrap(), Some(updated));
    assert_eq!(
        reopened.list_cg_audit_events(CHANNEL_ID, 20).unwrap().len(),
        3
    );
    drop(reopened);
    remove_database(&database);
}

#[test]
fn rejects_template_zone_mismatch_and_rolls_back_duplicate_audit() {
    let database = database_path("cg-invariants");
    let store = StationStore::open(&database).unwrap();
    put_channel(&store);
    let board = board(1, "fullscreen", 1_000);
    let board_audit = audit(
        "733df4e6-77a1-45c5-a751-fce51e7907c9",
        CgAuditEventKind::BoardCreated,
        1_000,
    );
    store.write_cg_board(&board, 0, &board_audit).unwrap();

    let invalid_zone = CgZoneConfig {
        zone_id: ZONE_ID.into(),
        board_id: BOARD_ID.into(),
        template_zone_key: "ticker".into(),
        zone_kind: CgZoneKind::Ticker,
        content_source: CgContentSource::BulletinRotation,
        rotation_seconds: Some(10),
        approval_required: true,
        allowed_tags: Vec::new(),
        revision: 1,
        created_at_unix_ms: 1_100,
        updated_at_unix_ms: 1_100,
    };
    let zone_audit = audit(
        "11ad1b62-5a62-4458-ae6a-39085646aa99",
        CgAuditEventKind::ZoneCreated,
        1_100,
    );
    assert!(matches!(
        store.write_cg_zone(&invalid_zone, 0, &zone_audit),
        Err(CgWriteError::Invalid(
            "CG zone does not belong to the board template."
        ))
    ));
    assert!(store.list_cg_zones(BOARD_ID).unwrap().is_empty());
    assert_eq!(store.list_cg_audit_events(CHANNEL_ID, 20).unwrap().len(), 1);

    let mut update = board.clone();
    update.active = false;
    update.revision = 2;
    update.updated_at_unix_ms = 1_200;
    let duplicate_audit = CgAuditEvent {
        event_kind: CgAuditEventKind::BoardUpdated,
        occurred_at_unix_ms: 1_200,
        ..board_audit
    };
    assert!(matches!(
        store.write_cg_board(&update, 1, &duplicate_audit),
        Err(CgWriteError::Storage(_))
    ));
    assert_eq!(store.read_cg_board(CHANNEL_ID).unwrap(), Some(board));
    drop(store);
    remove_database(&database);
}

#[test]
fn persists_moderated_feeds_and_bulletin_lifecycle_with_append_only_evidence() {
    let database = database_path("cg-content-lifecycle");
    let store = StationStore::open(&database).unwrap();
    put_channel(&store);
    store
        .write_cg_board(
            &board(1, "l-bar", 1_000),
            0,
            &audit(
                "0a1cba70-4fd4-44bf-9aab-968157021da7",
                CgAuditEventKind::BoardCreated,
                1_000,
            ),
        )
        .unwrap();

    let feed = CgFeedSource {
        feed_source_id: FEED_ID.into(),
        channel_id: CHANNEL_ID.into(),
        kind: CgFeedKind::Rss,
        label: "Town notices".into(),
        source_url: "https://notices.example.test/rss".into(),
        trust_tier: CgFeedTrustTier::Moderated,
        tags: Vec::new(),
        refresh_seconds: 300,
        enabled: true,
        revision: 1,
        created_at_unix_ms: 1_100,
        updated_at_unix_ms: 1_100,
        created_by: "station-operator".into(),
        last_fetched_at_unix_ms: None,
        last_fetch_error: None,
    };
    store
        .write_cg_feed_source(
            &feed,
            0,
            &audit(
                "2c60c2ac-fc66-4e88-acb4-060127f0ea90",
                CgAuditEventKind::FeedCreated,
                1_100,
            ),
        )
        .unwrap();
    assert_eq!(store.list_cg_feed_sources(CHANNEL_ID).unwrap(), vec![feed]);

    store
        .replace_cg_feed_cache(
            FEED_ID,
            1_140,
            &[CgFeedItem {
                item_id: "a".repeat(64),
                title: "Town notice".into(),
                summary: None,
                starts_at_unix_ms: None,
                url: None,
                tags: Vec::new(),
            }],
            None,
            None,
        )
        .unwrap();

    let item_approval = CgFeedItemApproval {
        feed_source_id: FEED_ID.into(),
        item_id: "a".repeat(64),
        approved_by: "station-operator".into(),
        approved_at_unix_ms: 1_150,
    };
    store
        .approve_cg_feed_item(
            &item_approval,
            &audit(
                "f8a1f8d7-3808-45e6-9775-399b6bf7808f",
                CgAuditEventKind::FeedItemApproved,
                1_150,
            ),
        )
        .unwrap();
    assert_eq!(
        store
            .read_cg_feed_item_approval(FEED_ID, &"a".repeat(64))
            .unwrap(),
        Some(item_approval)
    );

    let submitted = CgBulletin {
        bulletin_id: BULLETIN_ID.into(),
        channel_id: CHANNEL_ID.into(),
        content: CgBulletinContent::Text {
            title: "Library board meeting".into(),
            body: "Tuesday at 6 PM in the community room.".into(),
        },
        state: CgBulletinState::Submitted,
        tags: vec!["community".into()],
        requested_start_at_unix_ms: Some(2_000),
        requested_end_at_unix_ms: Some(3_000),
        display_seconds: 12,
        audio_asset_id: None,
        submitted_by: "library-clerk".into(),
        moderation_notes: None,
        moderated_by: None,
        revision: 1,
        created_at_unix_ms: 1_200,
        updated_at_unix_ms: 1_200,
    };
    store
        .write_cg_bulletin(
            &submitted,
            0,
            &audit(
                "88a6d930-bbfc-472c-a60a-58f2327bb8b0",
                CgAuditEventKind::BulletinSubmitted,
                1_200,
            ),
        )
        .unwrap();
    assert!(
        store
            .list_airable_cg_bulletins(CHANNEL_ID, 2_500)
            .unwrap()
            .is_empty()
    );

    let mut scheduled = submitted.clone();
    scheduled.state = CgBulletinState::Scheduled;
    scheduled.moderated_by = Some("station-operator".into());
    scheduled.revision = 2;
    scheduled.updated_at_unix_ms = 1_300;
    store
        .write_cg_bulletin(
            &scheduled,
            1,
            &audit(
                "360b1974-79e6-4dcb-b216-55939d9e532b",
                CgAuditEventKind::BulletinModerated,
                1_300,
            ),
        )
        .unwrap();
    assert_eq!(
        store.list_airable_cg_bulletins(CHANNEL_ID, 2_500).unwrap(),
        vec![scheduled.clone()]
    );

    let mut expired = scheduled;
    expired.state = CgBulletinState::Expired;
    expired.revision = 3;
    expired.updated_at_unix_ms = 3_000;
    store
        .write_cg_bulletin(
            &expired,
            2,
            &audit(
                "aaab2501-11c2-48e0-b310-79bfc6bc563b",
                CgAuditEventKind::BulletinExpired,
                3_000,
            ),
        )
        .unwrap();
    assert_eq!(store.read_cg_bulletin(BULLETIN_ID).unwrap(), Some(expired));
    assert!(
        store
            .list_airable_cg_bulletins(CHANNEL_ID, 3_000)
            .unwrap()
            .is_empty()
    );
    assert_eq!(store.list_cg_audit_events(CHANNEL_ID, 20).unwrap().len(), 6);
    drop(store);
    remove_database(&database);
}

#[test]
fn rejects_missing_feed_zone_and_terminal_bulletin_revival() {
    let database = database_path("cg-content-invariants");
    let store = StationStore::open(&database).unwrap();
    put_channel(&store);
    store
        .write_cg_board(
            &board(1, "l-bar", 1_000),
            0,
            &audit(
                "a42db639-cb17-4dbb-9974-5d87eedc801d",
                CgAuditEventKind::BoardCreated,
                1_000,
            ),
        )
        .unwrap();
    let missing_feed_zone = CgZoneConfig {
        zone_id: ZONE_ID.into(),
        board_id: BOARD_ID.into(),
        template_zone_key: "ticker".into(),
        zone_kind: CgZoneKind::Ticker,
        content_source: CgContentSource::Feed {
            feed_source_id: FEED_ID.into(),
        },
        rotation_seconds: Some(10),
        approval_required: true,
        allowed_tags: Vec::new(),
        revision: 1,
        created_at_unix_ms: 1_100,
        updated_at_unix_ms: 1_100,
    };
    assert_eq!(
        store.write_cg_zone(
            &missing_feed_zone,
            0,
            &audit(
                "160c745e-2816-4205-b299-7c3f72c2ef29",
                CgAuditEventKind::ZoneCreated,
                1_100,
            ),
        ),
        Err(CgWriteError::NotFound)
    );

    let declined = CgBulletin {
        bulletin_id: BULLETIN_ID.into(),
        channel_id: CHANNEL_ID.into(),
        content: CgBulletinContent::Text {
            title: "Unverified notice".into(),
            body: "Submitted for moderation.".into(),
        },
        state: CgBulletinState::Submitted,
        tags: Vec::new(),
        requested_start_at_unix_ms: None,
        requested_end_at_unix_ms: None,
        display_seconds: 10,
        audio_asset_id: None,
        submitted_by: "resident".into(),
        moderation_notes: None,
        moderated_by: None,
        revision: 1,
        created_at_unix_ms: 1_200,
        updated_at_unix_ms: 1_200,
    };
    store
        .write_cg_bulletin(
            &declined,
            0,
            &audit(
                "739a7e1f-f38e-47f5-8f76-c12f2535cd52",
                CgAuditEventKind::BulletinSubmitted,
                1_200,
            ),
        )
        .unwrap();
    let mut declined = declined;
    declined.state = CgBulletinState::Declined;
    declined.moderated_by = Some("station-operator".into());
    declined.moderation_notes = Some("Source could not be verified.".into());
    declined.revision = 2;
    declined.updated_at_unix_ms = 1_300;
    store
        .write_cg_bulletin(
            &declined,
            1,
            &audit(
                "1d5bc4cb-35e2-4778-8f65-5b1e06ba762c",
                CgAuditEventKind::BulletinModerated,
                1_300,
            ),
        )
        .unwrap();
    let mut revived = declined.clone();
    revived.state = CgBulletinState::Accepted;
    revived.revision = 3;
    revived.updated_at_unix_ms = 1_400;
    assert!(matches!(
        store.write_cg_bulletin(
            &revived,
            2,
            &audit(
                "47e977c8-069e-4d3e-bfc9-020098208e6d",
                CgAuditEventKind::BulletinModerated,
                1_400,
            ),
        ),
        Err(CgWriteError::Invalid(_))
    ));
    assert_eq!(store.read_cg_bulletin(BULLETIN_ID).unwrap(), Some(declined));
    drop(store);
    remove_database(&database);
}

#[test]
fn deletes_zones_and_feeds_with_revision_audit_and_dependency_rollback() {
    let database = database_path("cg-deletion");
    let store = StationStore::open(&database).unwrap();
    put_channel(&store);
    store
        .write_cg_board(
            &board(1, "l-bar", 1_000),
            0,
            &audit(
                "ad9e73b7-cfc0-49ed-8712-e5ebad47f8b0",
                CgAuditEventKind::BoardCreated,
                1_000,
            ),
        )
        .unwrap();
    let feed = CgFeedSource {
        feed_source_id: FEED_ID.into(),
        channel_id: CHANNEL_ID.into(),
        kind: CgFeedKind::Rss,
        label: "Town notices".into(),
        source_url: "https://notices.example.test/rss".into(),
        trust_tier: CgFeedTrustTier::Moderated,
        tags: Vec::new(),
        refresh_seconds: 300,
        enabled: true,
        revision: 1,
        created_at_unix_ms: 1_100,
        updated_at_unix_ms: 1_100,
        created_by: "station-operator".into(),
        last_fetched_at_unix_ms: None,
        last_fetch_error: None,
    };
    store
        .write_cg_feed_source(
            &feed,
            0,
            &audit(
                "fa2d0308-557b-47ad-a4c8-f60ea4d9386a",
                CgAuditEventKind::FeedCreated,
                1_100,
            ),
        )
        .unwrap();
    let zone = CgZoneConfig {
        zone_id: ZONE_ID.into(),
        board_id: BOARD_ID.into(),
        template_zone_key: "ticker".into(),
        zone_kind: CgZoneKind::Ticker,
        content_source: CgContentSource::Feed {
            feed_source_id: FEED_ID.into(),
        },
        rotation_seconds: Some(10),
        approval_required: true,
        allowed_tags: Vec::new(),
        revision: 1,
        created_at_unix_ms: 1_200,
        updated_at_unix_ms: 1_200,
    };
    store
        .write_cg_zone(
            &zone,
            0,
            &audit(
                "e2cb9b23-f34e-45a2-bc32-09cf952d35b0",
                CgAuditEventKind::ZoneCreated,
                1_200,
            ),
        )
        .unwrap();

    // A referenced feed cannot be removed, and the failed transaction must not
    // consume the removal audit or delete the feed.
    let in_use_audit = audit(
        "8bf8d3b8-0cf2-4d6f-a89d-00b5303e99d9",
        CgAuditEventKind::FeedRemoved,
        1_300,
    );
    assert!(matches!(
        store.delete_cg_feed_source(FEED_ID, 1, &in_use_audit),
        Err(CgWriteError::Invalid(message)) if message.contains("still uses it")
    ));
    assert!(store.read_cg_feed_source(FEED_ID).unwrap().is_some());
    assert_eq!(store.list_cg_audit_events(CHANNEL_ID, 20).unwrap().len(), 3);

    // The optimistic revision check rejects stale deletion without changing
    // either entity or its append-only evidence.
    let zone_removed = audit(
        "d0e01d06-e909-4c89-bd13-cf3f8df89f72",
        CgAuditEventKind::ZoneRemoved,
        1_400,
    );
    assert!(matches!(
        store.delete_cg_zone(ZONE_ID, 2, &zone_removed),
        Err(CgWriteError::Conflict(1))
    ));
    assert!(store.read_cg_zone(ZONE_ID).unwrap().is_some());

    store.delete_cg_zone(ZONE_ID, 1, &zone_removed).unwrap();
    assert!(store.read_cg_zone(ZONE_ID).unwrap().is_none());
    store
        .delete_cg_feed_source(FEED_ID, 1, &in_use_audit)
        .unwrap();
    assert!(store.read_cg_feed_source(FEED_ID).unwrap().is_none());
    assert_eq!(store.list_cg_audit_events(CHANNEL_ID, 20).unwrap().len(), 5);
    drop(store);
    remove_database(&database);
}

#[test]
fn feed_cache_failure_preserves_last_success_and_approval_requires_observed_item() {
    let database = database_path("cg-feed-cache");
    let store = StationStore::open(&database).unwrap();
    put_channel(&store);
    store
        .write_cg_feed_source(
            &feed(FEED_ID, "Town notices"),
            0,
            &audit(
                "47c70ce1-f4f9-4f3f-a83b-4b220f9ba2a7",
                CgAuditEventKind::FeedCreated,
                1_000,
            ),
        )
        .unwrap();
    store
        .write_cg_feed_source(
            &feed(SECOND_FEED_ID, "Town notices 2"),
            0,
            &audit(
                "fd75c0d7-93ca-4f04-80cd-9c76b40ff76c",
                CgAuditEventKind::FeedCreated,
                1_000,
            ),
        )
        .unwrap();
    let item = CgFeedItem {
        item_id: "a".repeat(64),
        title: "Library meeting".into(),
        summary: Some("Tuesday at six".into()),
        starts_at_unix_ms: Some(2_000),
        url: Some("https://notices.example.test/library".into()),
        tags: vec!["community".into()],
    };
    let replaced = store
        .replace_cg_feed_cache(FEED_ID, 2_000, std::slice::from_ref(&item), None, None)
        .unwrap();
    assert_eq!(replaced.items, vec![item.clone()]);
    assert_eq!(replaced.last_error, None);
    assert_eq!(
        store
            .read_cg_feed_source(FEED_ID)
            .unwrap()
            .unwrap()
            .last_fetched_at_unix_ms,
        Some(2_000)
    );
    let second_item = CgFeedItem {
        item_id: "b".repeat(64),
        title: "Park event".into(),
        summary: None,
        starts_at_unix_ms: Some(2_100),
        url: None,
        tags: vec!["community".into()],
    };
    store
        .replace_cg_feed_cache(
            FEED_ID,
            2_100,
            &[item.clone(), second_item.clone()],
            Some("v1"),
            None,
        )
        .unwrap();
    drop(store);
    let store = StationStore::open(&database).unwrap();
    assert_eq!(
        store.read_cg_feed_cache(FEED_ID).unwrap().unwrap().items,
        vec![item.clone(), second_item.clone()]
    );
    let duplicate = vec![second_item.clone(), second_item.clone()];
    assert!(matches!(
        store.replace_cg_feed_cache(FEED_ID, 2_200, &duplicate, None, None),
        Err(CgWriteError::Invalid(_))
    ));
    let preserved = store.read_cg_feed_cache(FEED_ID).unwrap().unwrap();
    assert_eq!(preserved.items, vec![item.clone(), second_item.clone()]);
    assert_eq!(preserved.etag.as_deref(), Some("v1"));
    store
        .record_cg_feed_fetch_failure(FEED_ID, 3_000, "timeout")
        .unwrap();
    let cached = store.read_cg_feed_cache(FEED_ID).unwrap().unwrap();
    assert_eq!(cached.items, vec![item, second_item]);
    assert_eq!(cached.last_error.as_deref(), Some("timeout"));
    assert_eq!(
        store
            .read_cg_feed_source(FEED_ID)
            .unwrap()
            .unwrap()
            .last_fetched_at_unix_ms,
        Some(3_000)
    );

    let unknown = CgFeedItemApproval {
        feed_source_id: FEED_ID.into(),
        item_id: "c".repeat(64),
        approved_by: "station-operator".into(),
        approved_at_unix_ms: 3_100,
    };
    assert_eq!(
        store.approve_cg_feed_item(
            &unknown,
            &audit(
                "c4b55b25-2697-41d5-a9d5-9ce8dd05d972",
                CgAuditEventKind::FeedItemApproved,
                3_100,
            ),
        ),
        Err(CgWriteError::NotFound)
    );
    let known = CgFeedItemApproval {
        item_id: "a".repeat(64),
        ..unknown
    };
    store
        .approve_cg_feed_item(
            &known,
            &audit(
                "720c6322-6ecc-463e-9607-9ad9cfbb70c1",
                CgAuditEventKind::FeedItemApproved,
                3_200,
            ),
        )
        .unwrap();
    assert!(
        store
            .list_cg_approved_feed_item_ids(FEED_ID)
            .unwrap()
            .contains(&known.item_id)
    );

    store
        .record_cg_feed_fetch_failure(SECOND_FEED_ID, 4_000, "unreachable")
        .unwrap();
    let second = store.read_cg_feed_source(SECOND_FEED_ID).unwrap().unwrap();
    assert_eq!(second.last_fetched_at_unix_ms, Some(4_000));
    assert_eq!(second.last_fetch_error.as_deref(), Some("unreachable"));
    assert!(store.read_cg_feed_cache(SECOND_FEED_ID).unwrap().is_none());

    let mut disabled = feed(FEED_ID, "Town notices");
    disabled.enabled = false;
    disabled.revision = 2;
    disabled.updated_at_unix_ms = 3_300;
    store
        .write_cg_feed_source(
            &disabled,
            1,
            &audit(
                "02f140aa-a720-4b3b-b6ea-a0dedcde5c52",
                CgAuditEventKind::FeedUpdated,
                3_300,
            ),
        )
        .unwrap();
    assert!(matches!(
        store.replace_cg_feed_cache(FEED_ID, 4_000, &[], None, None),
        Err(CgWriteError::Invalid(message)) if message.contains("disabled")
    ));
    assert_eq!(
        store
            .read_cg_feed_cache(FEED_ID)
            .unwrap()
            .unwrap()
            .items
            .len(),
        2
    );
    drop(store);
    remove_database(&database);
}

fn put_channel(store: &StationStore) {
    store
        .put_channel(&ChannelConfiguration {
            channel_id: CHANNEL_ID.into(),
            display_name: "Government Channel".into(),
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

fn board(revision: u64, template_id: &str, updated_at_unix_ms: i64) -> CgBoard {
    CgBoard {
        board_id: BOARD_ID.into(),
        channel_id: CHANNEL_ID.into(),
        template_id: template_id.into(),
        active: true,
        background_audio_asset_id: None,
        revision,
        created_at_unix_ms: 1_000,
        updated_at_unix_ms,
        created_by: "station-operator".into(),
    }
}

fn feed(feed_source_id: &str, label: &str) -> CgFeedSource {
    CgFeedSource {
        feed_source_id: feed_source_id.into(),
        channel_id: CHANNEL_ID.into(),
        kind: CgFeedKind::Rss,
        label: label.into(),
        source_url: "https://notices.example.test/rss".into(),
        trust_tier: CgFeedTrustTier::Moderated,
        tags: Vec::new(),
        refresh_seconds: 300,
        enabled: true,
        revision: 1,
        created_at_unix_ms: 1_000,
        updated_at_unix_ms: 1_000,
        created_by: "station-operator".into(),
        last_fetched_at_unix_ms: None,
        last_fetch_error: None,
    }
}

fn audit(audit_id: &str, event_kind: CgAuditEventKind, occurred_at_unix_ms: i64) -> CgAuditEvent {
    CgAuditEvent {
        audit_id: audit_id.into(),
        board_id: Some(BOARD_ID.into()),
        channel_id: CHANNEL_ID.into(),
        event_kind,
        operator_id: "station-operator".into(),
        occurred_at_unix_ms,
        details: BTreeMap::new(),
    }
}

fn database_path(label: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-{label}-{nonce}.db"))
}

fn remove_database(path: &Path) {
    for candidate in [
        path.to_path_buf(),
        PathBuf::from(format!("{}-wal", path.display())),
        PathBuf::from(format!("{}-shm", path.display())),
    ] {
        let _ = std::fs::remove_file(candidate);
    }
}
