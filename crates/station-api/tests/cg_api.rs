use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use station_api::Api;
use station_cg::{
    CgAuditEvent, CgAuditEventKind, CgBoard, CgBulletin, CgBulletinContent, CgBulletinState,
    CgContentSource, CgFeedItem, CgFeedItemApproval, CgFeedKind, CgFeedSource, CgFeedTrustTier,
    CgResolvedContent, CgResolvedSnapshot, CgTemplate, CgZoneConfig, CgZoneKind,
};
use station_schedule::{ChannelConfiguration, OutputProfileId, OutputSinkConfiguration};
use station_storage::StationStore;

const CHANNEL_ID: &str = "5d85189a-cd1f-43dc-a619-66a2aeb09ac4";
const BOARD_ID: &str = "94a0dc01-d314-4b55-b2e4-cba39cc3f1dc";
const ZONE_ID: &str = "8f4f0f70-5c30-48c6-819a-fb22b34353c5";
const FEED_ID: &str = "796ca80a-9cc2-4fc1-84a2-2a88d8f92f24";

#[test]
fn manages_cg_authority_through_the_operator_api() {
    let database = database_path("cg-api");
    put_channel(&database);
    let api = Api::open(&database).unwrap();

    let templates = api.handle("GET", "/api/v1/cg/templates", None);
    assert_eq!(templates.status, 200);
    let templates: Vec<CgTemplate> = serde_json::from_slice(&templates.body).unwrap();
    assert_eq!(templates.len(), 6);

    let board = CgBoard {
        board_id: BOARD_ID.into(),
        channel_id: CHANNEL_ID.into(),
        template_id: "l-bar".into(),
        active: true,
        background_audio_asset_id: None,
        revision: 1,
        created_at_unix_ms: 1_000,
        updated_at_unix_ms: 1_000,
        created_by: "station-operator".into(),
    };
    let response = api.handle(
        "PUT",
        "/api/v1/cg/board",
        Some(&write_body(
            "board",
            &board,
            0,
            &audit(
                "7bb32e14-fe69-47cb-a921-dc3fc84499bf",
                CgAuditEventKind::BoardCreated,
                1_000,
            ),
        )),
    );
    assert_eq!(response.status, 200, "{}", body_text(&response.body));
    assert_eq!(
        serde_json::from_slice::<CgBoard>(&response.body).unwrap(),
        board
    );

    let mut feed = CgFeedSource {
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
    let response = api.handle(
        "PUT",
        "/api/v1/cg/feeds",
        Some(&write_body(
            "feed",
            &feed,
            0,
            &audit(
                "2c60c2ac-fc66-4e88-acb4-060127f0ea90",
                CgAuditEventKind::FeedCreated,
                1_100,
            ),
        )),
    );
    assert_eq!(response.status, 200, "{}", body_text(&response.body));

    StationStore::open(&database)
        .unwrap()
        .replace_cg_feed_cache(
            FEED_ID,
            1_350,
            &[CgFeedItem {
                item_id: "a".repeat(64),
                title: "Town notice".into(),
                summary: None,
                starts_at_unix_ms: None,
                url: None,
                tags: vec!["public-safety".into()],
            }],
            None,
            None,
        )
        .unwrap();
    feed.last_fetched_at_unix_ms = Some(1_350);

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
        allowed_tags: vec!["public-safety".into()],
        revision: 1,
        created_at_unix_ms: 1_200,
        updated_at_unix_ms: 1_200,
    };
    let response = api.handle(
        "PUT",
        "/api/v1/cg/zones",
        Some(&write_body(
            "zone",
            &zone,
            0,
            &audit(
                "31086e51-b940-450d-a9c2-5a31807ecc02",
                CgAuditEventKind::ZoneCreated,
                1_200,
            ),
        )),
    );
    assert_eq!(response.status, 200, "{}", body_text(&response.body));

    let bulletin = CgBulletin {
        bulletin_id: "597f52ae-f16c-4ff8-ab32-60379cd950c4".into(),
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
        created_at_unix_ms: 1_300,
        updated_at_unix_ms: 1_300,
    };
    let response = api.handle(
        "PUT",
        "/api/v1/cg/bulletins",
        Some(&write_body(
            "bulletin",
            &bulletin,
            0,
            &audit(
                "88a6d930-bbfc-472c-a60a-58f2327bb8b0",
                CgAuditEventKind::BulletinSubmitted,
                1_300,
            ),
        )),
    );
    assert_eq!(response.status, 200, "{}", body_text(&response.body));

    let approval = CgFeedItemApproval {
        feed_source_id: FEED_ID.into(),
        item_id: "a".repeat(64),
        approved_by: "station-operator".into(),
        approved_at_unix_ms: 1_400,
    };
    let approval_body = serde_json::to_vec(&serde_json::json!({
        "approval": approval,
        "audit": audit(
            "f8a1f8d7-3808-45e6-9775-399b6bf7808f",
            CgAuditEventKind::FeedItemApproved,
            1_400,
        ),
    }))
    .unwrap();
    let response = api.handle(
        "POST",
        "/api/v1/cg/feed-items/approve",
        Some(&approval_body),
    );
    assert_eq!(response.status, 200, "{}", body_text(&response.body));

    let snapshot: CgResolvedSnapshot = response_json(&api.handle(
        "GET",
        &format!("/api/v1/cg/snapshot?channel_id={CHANNEL_ID}"),
        None,
    ));
    assert!(matches!(
        snapshot.zones[0].content,
        CgResolvedContent::FeedItems { stale: true, .. }
    ));
    let preview = api.handle(
        "GET",
        &format!("/api/v1/cg/preview?channel_id={CHANNEL_ID}&at_unix_ms=2000"),
        None,
    );
    assert_eq!(preview.status, 200);
    let preview: CgResolvedSnapshot = response_json(&preview);
    assert_eq!(preview.generated_at_unix_ms, 2_000);
    for path in [
        "/api/v1/cg/snapshot",
        "/api/v1/cg/snapshot?channel_id=bad&channel_id=duplicate",
        "/api/v1/cg/snapshot?channel_id=bad&extra=value",
        "/api/v1/cg/preview?channel_id=bad",
        "/api/v1/cg/preview?channel_id=bad&at_unix_ms=-1",
        "/api/v1/cg/preview?channel_id=bad%20value",
    ] {
        assert_eq!(api.handle("GET", path, None).status, 400, "{path}");
    }

    let board_response = api.handle(
        "GET",
        &format!("/api/v1/cg/board?channel_id={CHANNEL_ID}"),
        None,
    );
    assert_eq!(board_response.status, 200);
    assert_eq!(
        serde_json::from_slice::<CgBoard>(&board_response.body).unwrap(),
        board
    );
    let zones: Vec<CgZoneConfig> = response_json(&api.handle(
        "GET",
        &format!("/api/v1/cg/zones?board_id={BOARD_ID}"),
        None,
    ));
    assert_eq!(zones, vec![zone]);
    let feeds: Vec<CgFeedSource> = response_json(&api.handle(
        "GET",
        &format!("/api/v1/cg/feeds?channel_id={CHANNEL_ID}"),
        None,
    ));
    assert_eq!(feeds, vec![feed]);
    let bulletins: Vec<CgBulletin> = response_json(&api.handle(
        "GET",
        &format!("/api/v1/cg/bulletins?channel_id={CHANNEL_ID}"),
        None,
    ));
    assert_eq!(bulletins, vec![bulletin]);
    let audit_events: Vec<CgAuditEvent> = response_json(&api.handle(
        "GET",
        &format!("/api/v1/cg/audit?channel_id={CHANNEL_ID}&limit=20"),
        None,
    ));
    assert_eq!(audit_events.len(), 5);

    let conflict = api.handle(
        "PUT",
        "/api/v1/cg/board",
        Some(&write_body(
            "board",
            &board,
            0,
            &audit(
                "919ccff6-f20c-4787-8916-a4b96101ec8e",
                CgAuditEventKind::BoardUpdated,
                1_500,
            ),
        )),
    );
    assert_eq!(conflict.status, 409);
    assert!(body_text(&conflict.body).contains("revision_conflict"));
    assert_eq!(
        api.handle(
            "GET",
            &format!("/api/v1/cg/feeds?channel_id={CHANNEL_ID}&unexpected=true"),
            None,
        )
        .status,
        400
    );

    let removed_zone = api.handle(
        "DELETE",
        &format!("/api/v1/cg/zones/{ZONE_ID}"),
        Some(&delete_body(
            1,
            &audit(
                "5ee514a0-362a-4aa1-bcf3-1615129a546e",
                CgAuditEventKind::ZoneRemoved,
                1_600,
            ),
        )),
    );
    assert_eq!(
        removed_zone.status,
        204,
        "{}",
        body_text(&removed_zone.body)
    );
    let removed_feed = api.handle(
        "DELETE",
        &format!("/api/v1/cg/feeds/{FEED_ID}"),
        Some(&delete_body(
            1,
            &audit(
                "8087b430-33f0-4e89-9405-2478e0445e8b",
                CgAuditEventKind::FeedRemoved,
                1_700,
            ),
        )),
    );
    assert_eq!(
        removed_feed.status,
        204,
        "{}",
        body_text(&removed_feed.body)
    );
    assert!(
        response_json::<Vec<CgZoneConfig>>(&api.handle(
            "GET",
            &format!("/api/v1/cg/zones?board_id={BOARD_ID}"),
            None,
        ))
        .is_empty()
    );
    assert!(
        response_json::<Vec<CgFeedSource>>(&api.handle(
            "GET",
            &format!("/api/v1/cg/feeds?channel_id={CHANNEL_ID}"),
            None,
        ))
        .is_empty()
    );

    drop(api);
    remove_database(&database);
}

fn write_body<T: serde::Serialize>(
    name: &str,
    value: &T,
    expected_revision: u64,
    audit: &CgAuditEvent,
) -> Vec<u8> {
    let mut object = serde_json::Map::new();
    object.insert(name.into(), serde_json::to_value(value).unwrap());
    object.insert("expected_revision".into(), expected_revision.into());
    object.insert("audit".into(), serde_json::to_value(audit).unwrap());
    serde_json::to_vec(&object).unwrap()
}

fn delete_body(expected_revision: u64, audit: &CgAuditEvent) -> Vec<u8> {
    serde_json::to_vec(&serde_json::json!({
        "expected_revision": expected_revision,
        "audit": audit,
    }))
    .unwrap()
}

fn response_json<T: serde::de::DeserializeOwned>(response: &station_api::ApiResponse) -> T {
    assert_eq!(response.status, 200, "{}", body_text(&response.body));
    serde_json::from_slice(&response.body).unwrap()
}

fn body_text(body: &[u8]) -> String {
    String::from_utf8_lossy(body).into_owned()
}

fn put_channel(database: &Path) {
    StationStore::open(database)
        .unwrap()
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
