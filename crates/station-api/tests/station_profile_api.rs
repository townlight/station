use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
    mpsc,
};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use gst::prelude::*;
use gstreamer as gst;
use station_api::{Api, serve_one, serve_until};
use station_domain::{
    AlertConditionKind, AlertDelivery, AlertDeliveryState, AlertEvent, AlertEventState,
    AlertNotificationKind, AlertRule, AlertSeverity, ChannelRuntimeObservation,
    ChannelRuntimeState, RuntimeSafeToAirStatus, SafeToAirColor, SelfTestKind, SelfTestSchedule,
    SelfTestTrigger, StationProfile, SystemResourceSample, SystemSelfTest,
};
use station_ingest::{IngestOutcome, IngestWorker, TranscodeOutcome, TranscodeWorker};
use station_media_lifecycle::WatchFolderConfig;
use station_schedule::{
    AssetReadiness, ChannelConfiguration, CommitPlan, CommitReport, DispatchStatus, MediaAsset,
    ScheduleItem, ScheduleState,
};
use station_storage::StationStore;

fn temporary_database(name: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is before Unix epoch")
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-station-{name}-{nonce}.db"))
}

fn http_request(database: &Path, request: &[u8]) -> Vec<u8> {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let database = database.to_path_buf();
    let server = thread::spawn(move || serve_one(listener, database));
    let mut client = TcpStream::connect(address).unwrap();
    client
        .set_read_timeout(Some(Duration::from_secs(1)))
        .unwrap();
    client.write_all(request).unwrap();
    let mut response = Vec::new();
    match client.read_to_end(&mut response) {
        Ok(_) => {}
        Err(error)
            if matches!(
                error.kind(),
                std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock
            ) => {}
        Err(error) => panic!("reading HTTP response failed: {error}"),
    }
    drop(client);
    let _ = server.join();
    response
}

#[test]
fn persists_a_valid_station_profile_and_reads_it_back() {
    let database = temporary_database("persist");
    let api = Api::open(&database).expect("database opens");
    let profile = StationProfile {
        station_id: "3f5f721f-96c7-48b1-b061-1bf1ad1e62c2".into(),
        display_name: "KTLT Community Television".into(),
        timezone: "America/Denver".into(),
    };
    let put = api.handle(
        "PUT",
        "/api/v1/station",
        Some(&serde_json::to_vec(&profile).unwrap()),
    );
    assert_eq!(put.status, 200);
    drop(api);
    let restarted = Api::open(&database).expect("database reopens");
    let get = restarted.handle("GET", "/api/v1/station", None);
    assert_eq!(get.status, 200);
    assert_eq!(
        serde_json::from_slice::<StationProfile>(&get.body).unwrap(),
        profile
    );
    let _ = std::fs::remove_file(database);
}

#[test]
fn rejects_an_invalid_station_profile_without_replacing_the_current_profile() {
    let database = temporary_database("reject");
    let api = Api::open(&database).expect("database opens");
    let valid = br#"{"station_id":"3f5f721f-96c7-48b1-b061-1bf1ad1e62c2","display_name":"KTLT","timezone":"America/Denver"}"#;
    assert_eq!(
        api.handle("PUT", "/api/v1/station", Some(valid)).status,
        200
    );
    let invalid = br#"{"station_id":"not-an-id","display_name":"","timezone":"UTC+7"}"#;
    let response = api.handle("PUT", "/api/v1/station", Some(invalid));
    assert_eq!(response.status, 422);
    assert!(
        String::from_utf8(response.body)
            .unwrap()
            .contains("validation_failed")
    );
    let get = api.handle("GET", "/api/v1/station", None);
    let stored: StationProfile = serde_json::from_slice(&get.body).unwrap();
    assert_eq!(stored.display_name, "KTLT");
    let _ = std::fs::remove_file(database);
}

#[test]
fn health_reports_database_readiness_and_unknown_routes_are_not_found() {
    let database = temporary_database("health");
    let api = Api::open(&database).expect("database opens");
    let health = api.handle("GET", "/health", None);
    assert_eq!(health.status, 200);
    assert_eq!(health.body, br#"{"database":"ready","status":"ready"}"#);
    assert_eq!(api.handle("GET", "/not-a-route", None).status, 404);
    let _ = std::fs::remove_file(database);
}

#[test]
fn serves_the_embedded_operator_console_from_the_station_origin() {
    let database = temporary_database("operator-console");
    let api = Api::open(&database).expect("database opens");
    let shell = api.handle("GET", "/", None);
    assert_eq!(shell.status, 200);
    assert_eq!(shell.content_type, "text/html; charset=utf-8");
    let html = String::from_utf8(shell.body).unwrap();
    assert!(html.contains("TownLight Station"));
    assert!(html.contains("/operator.js"));
    let deep_link = api.handle("GET", "/?view=health", None);
    assert_eq!(deep_link.status, 200);
    assert_eq!(deep_link.content_type, "text/html; charset=utf-8");
    let script = api.handle("GET", "/operator.js", None);
    assert_eq!(script.status, 200);
    assert_eq!(script.content_type, "text/javascript; charset=utf-8");
    assert!(script.body.len() > 1_000);
    let styles = api.handle("GET", "/operator.css", None);
    assert_eq!(styles.status, 200);
    assert_eq!(styles.content_type, "text/css; charset=utf-8");
    assert!(styles.body.len() > 1_000);
    let _ = std::fs::remove_file(database);
}

#[test]
fn persists_validated_channel_output_configuration() {
    let database = temporary_database("channels");
    let api = Api::open(&database).unwrap();
    let channel = ChannelConfiguration {
        channel_id: "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e".into(),
        display_name: "Primary Cable Channel".into(),
        udp_destination: "127.0.0.1:5500".into(),
        enabled: true,
    };
    let put = api.handle(
        "PUT",
        "/api/v1/channels",
        Some(&serde_json::to_vec(&channel).unwrap()),
    );
    assert_eq!(put.status, 200);
    let listed = api.handle("GET", "/api/v1/channels", None);
    assert_eq!(listed.status, 200);
    assert_eq!(
        serde_json::from_slice::<Vec<ChannelConfiguration>>(&listed.body).unwrap(),
        vec![channel]
    );
    let invalid = api.handle(
        "PUT",
        "/api/v1/channels",
        Some(br#"{"channel_id":"not-a-uuid","display_name":"Bad","udp_destination":"nowhere","enabled":true}"#),
    );
    assert_eq!(invalid.status, 422);
    let _ = std::fs::remove_file(database);
}

#[test]
fn exposes_runtime_safe_to_air_and_acknowledges_durable_alerts() {
    let database = temporary_database("runtime-alerts");
    let channel = ChannelConfiguration {
        channel_id: "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e".into(),
        display_name: "Primary Cable Channel".into(),
        udp_destination: "127.0.0.1:5500".into(),
        enabled: true,
    };
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64;
    let store = StationStore::open(&database).unwrap();
    store.put_channel(&channel).unwrap();
    store
        .put_channel_runtime(&ChannelRuntimeObservation {
            channel_id: channel.channel_id.clone(),
            state: ChannelRuntimeState::HealthyFallback,
            on_air_asset_id: None,
            state_started_at_unix_ms: now - 10_000,
            heartbeat_at_unix_ms: now,
            detail: None,
        })
        .unwrap();
    store
        .observe_alert(&AlertEvent {
            event_id: "alert-runtime-test".into(),
            condition: AlertConditionKind::CommitFailure,
            severity: AlertSeverity::Warning,
            state: AlertEventState::Firing,
            resource_ref: channel.channel_id.clone(),
            summary: "Commit needs operator review.".into(),
            detail: String::new(),
            source_section: "S4".into(),
            first_observed_at_unix_ms: now,
            last_observed_at_unix_ms: now,
            resolved_at_unix_ms: None,
            occurrence_count: 1,
            acknowledged_at_unix_ms: None,
            acknowledged_by: None,
        })
        .unwrap();
    drop(store);

    let api = Api::open(&database).unwrap();
    let runtime = api.handle("GET", "/api/v1/runtime-safe-to-air", None);
    assert_eq!(runtime.status, 200);
    let runtime: RuntimeSafeToAirStatus = serde_json::from_slice(&runtime.body).unwrap();
    assert_eq!(runtime.color, SafeToAirColor::Yellow);
    assert!(runtime.channels[0].on_healthy_fallback);
    assert_eq!(runtime.active_warning_alerts, 1);

    let listed = api.handle("GET", "/api/v1/alerts", None);
    let alerts: Vec<AlertEvent> = serde_json::from_slice(&listed.body).unwrap();
    assert_eq!(alerts.len(), 1);
    assert!(alerts[0].acknowledged_at_unix_ms.is_none());

    let acknowledged = api.handle(
        "POST",
        "/api/v1/alerts/alert-runtime-test/acknowledge",
        Some(br#"{"acknowledged_by":"station-operator"}"#),
    );
    assert_eq!(acknowledged.status, 200);
    let acknowledged: AlertEvent = serde_json::from_slice(&acknowledged.body).unwrap();
    assert_eq!(
        acknowledged.acknowledged_by.as_deref(),
        Some("station-operator")
    );
    let _ = std::fs::remove_file(database);
}

#[test]
fn stores_and_lists_only_a_redacted_alert_destination_across_restart() {
    let database = temporary_database("alert-channel-redaction");
    let api = Api::open(&database).unwrap();
    let configured = api.handle(
        "PUT",
        "/api/v1/alert-channels",
        Some(
            br#"{"channel_id":"22500b92-555b-4ef8-a1de-4f8177d5df23","kind":"webhook","label":"On-call webhook","enabled":true,"target":"https://alerts.example.org/private/station","credential":"proof-signing-secret","quiet_hours_start_utc":"22:00","quiet_hours_end_utc":"07:00"}"#,
        ),
    );
    assert_eq!(
        configured.status,
        200,
        "{}",
        String::from_utf8_lossy(&configured.body)
    );
    let public: serde_json::Value = serde_json::from_slice(&configured.body).unwrap();
    assert_eq!(public["kind"], "webhook");
    assert_eq!(public["target_redacted"], "https://alerts.example.org/…");
    assert_eq!(public["credential_configured"], true);
    assert!(public.get("target").is_none());
    assert!(public.get("credential").is_none());
    drop(api);

    let restarted = Api::open(&database).unwrap();
    let listed = restarted.handle("GET", "/api/v1/alert-channels", None);
    assert_eq!(listed.status, 200);
    let listed: serde_json::Value = serde_json::from_slice(&listed.body).unwrap();
    assert_eq!(listed.as_array().unwrap().len(), 1);
    assert_eq!(listed[0], public);
    drop(restarted);
    let secret = StationStore::open(&database)
        .unwrap()
        .read_alert_channel_secret("22500b92-555b-4ef8-a1de-4f8177d5df23")
        .unwrap()
        .unwrap();
    assert_eq!(secret.target, "https://alerts.example.org/private/station");
    assert_eq!(secret.credential, "proof-signing-secret");
    let _ = std::fs::remove_file(database);
}

#[test]
fn alert_destination_plaintext_never_reaches_sqlite_files() {
    let database = temporary_database("alert-channel-encryption");
    let target = "https://pager.example.net/private/callback";
    let secret = "never-store-this-webhook-secret";
    let api = Api::open(&database).unwrap();
    let body = serde_json::to_vec(&serde_json::json!({
        "channel_id": "6a6c10af-dfdf-4db2-b8a0-b39ab4040233",
        "kind": "webhook",
        "label": "Encrypted pager",
        "enabled": true,
        "target": target,
        "credential": secret
    }))
    .unwrap();
    assert_eq!(
        api.handle("PUT", "/api/v1/alert-channels", Some(&body))
            .status,
        200
    );
    drop(api);

    for candidate in [
        database.clone(),
        PathBuf::from(format!("{}-wal", database.display())),
        PathBuf::from(format!("{}-shm", database.display())),
    ] {
        if let Ok(bytes) = std::fs::read(&candidate) {
            assert!(
                !bytes
                    .windows(target.len())
                    .any(|window| window == target.as_bytes())
            );
            assert!(
                !bytes
                    .windows(secret.len())
                    .any(|window| window == secret.as_bytes())
            );
        }
        let _ = std::fs::remove_file(candidate);
    }
}

#[test]
fn rejects_insecure_remote_webhook_destinations() {
    let database = temporary_database("alert-channel-insecure");
    let api = Api::open(&database).unwrap();
    let response = api.handle(
        "PUT",
        "/api/v1/alert-channels",
        Some(
            br#"{"channel_id":"566c7e2a-661d-40fd-8078-7df64586c363","kind":"webhook","label":"Unsafe pager","enabled":true,"target":"http://alerts.example.org/callback","credential":"secret"}"#,
        ),
    );
    assert_eq!(response.status, 422);
    assert!(String::from_utf8(response.body).unwrap().contains("https"));
    let listed = api.handle("GET", "/api/v1/alert-channels", None);
    assert_eq!(listed.status, 200);
    assert_eq!(listed.body, b"[]");
    let _ = std::fs::remove_file(database);
}

#[test]
fn rejects_control_characters_in_alert_provider_endpoints() {
    let database = temporary_database("alert-channel-control-character");
    let api = Api::open(&database).unwrap();
    let body = serde_json::to_vec(&serde_json::json!({
        "channel_id": "cdb30e41-e649-4bcd-b9e3-161ebd6cd20c",
        "kind": "sms",
        "label": "Unsafe SMS provider",
        "enabled": true,
        "target": "+15551234567",
        "endpoint": "https://sms.example.org/send\nInjected: true",
        "sender": "+15557654321",
        "identity": "provider-account",
        "credential": "provider-auth-token"
    }))
    .unwrap();
    let response = api.handle("PUT", "/api/v1/alert-channels", Some(&body));
    assert_eq!(
        response.status,
        422,
        "{}",
        String::from_utf8_lossy(&response.body)
    );
    assert_eq!(
        api.handle("GET", "/api/v1/alert-channels", None).body,
        b"[]"
    );
    let _ = std::fs::remove_file(database);
}

#[test]
fn rejects_control_characters_in_smtp_endpoints() {
    let database = temporary_database("alert-channel-smtp-control-character");
    let api = Api::open(&database).unwrap();
    let body = serde_json::to_vec(&serde_json::json!({
        "channel_id": "5b34c64b-a659-48f7-8707-4ed33f28ba0d",
        "kind": "email",
        "label": "Unsafe SMTP provider",
        "enabled": true,
        "target": "operator@example.org",
        "endpoint": "smtps://smtp.example.org/mail\nInjected: true",
        "sender": "station@example.org",
        "identity": "station-account",
        "credential": "smtp-password"
    }))
    .unwrap();
    let response = api.handle("PUT", "/api/v1/alert-channels", Some(&body));
    assert_eq!(
        response.status,
        422,
        "{}",
        String::from_utf8_lossy(&response.body)
    );
    assert_eq!(
        api.handle("GET", "/api/v1/alert-channels", None).body,
        b"[]"
    );
    let _ = std::fs::remove_file(database);
}

#[test]
fn queues_a_visible_send_test_without_exposing_destination_secrets() {
    let database = temporary_database("alert-channel-send-test");
    let api = Api::open(&database).unwrap();
    let channel_id = "24fcb252-8f00-4e15-a4c9-7de8637beb3c";
    let configured = api.handle(
        "PUT",
        "/api/v1/alert-channels",
        Some(
            br#"{"channel_id":"24fcb252-8f00-4e15-a4c9-7de8637beb3c","kind":"webhook","label":"Test pager","enabled":false,"target":"http://127.0.0.1:47883/operator-alert","credential":"send-test-secret"}"#,
        ),
    );
    assert_eq!(configured.status, 200);

    let queued = api.handle(
        "POST",
        &format!("/api/v1/alert-channels/{channel_id}/test"),
        None,
    );
    assert_eq!(
        queued.status,
        202,
        "{}",
        String::from_utf8_lossy(&queued.body)
    );
    let delivery: AlertDelivery = serde_json::from_slice(&queued.body).unwrap();
    assert_eq!(delivery.state, AlertDeliveryState::Pending);
    assert_eq!(delivery.notification_kind, AlertNotificationKind::Test);
    assert_eq!(delivery.alert_channel_id, channel_id);
    assert!(!String::from_utf8_lossy(&queued.body).contains("send-test-secret"));
    assert!(!String::from_utf8_lossy(&queued.body).contains("127.0.0.1"));

    let listed = api.handle("GET", "/api/v1/alert-deliveries", None);
    assert_eq!(listed.status, 200);
    let deliveries: Vec<AlertDelivery> = serde_json::from_slice(&listed.body).unwrap();
    assert_eq!(deliveries, vec![delivery]);
    let events = StationStore::open(&database)
        .unwrap()
        .list_alert_events()
        .unwrap();
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].state, AlertEventState::Resolved);
    assert_eq!(events[0].severity, AlertSeverity::Info);
    let _ = std::fs::remove_file(database);
}

#[test]
fn lists_seeded_alert_rules_and_persists_an_operator_policy_edit() {
    let database = temporary_database("alert-rule-edit");
    let api = Api::open(&database).unwrap();
    let listed = api.handle("GET", "/api/v1/alert-rules", None);
    assert_eq!(listed.status, 200);
    let rules: Vec<AlertRule> = serde_json::from_slice(&listed.body).unwrap();
    let rule = rules
        .iter()
        .find(|rule| rule.condition == AlertConditionKind::OffAir)
        .unwrap();
    assert_eq!(rule.severity, AlertSeverity::Critical);
    assert_eq!(rule.re_alert_after_seconds, 3_600);

    let edited = api.handle(
        "PUT",
        &format!("/api/v1/alert-rules/{}", rule.rule_id),
        Some(
            br#"{"condition":"off-air","enabled":true,"severity":"critical","channel_ids":[],"dedupe_window_seconds":300,"re_alert_after_seconds":1800,"scope_channel_id":null,"notify_on_resolve":false,"updated_by":"station-operator"}"#,
        ),
    );
    assert_eq!(
        edited.status,
        200,
        "{}",
        String::from_utf8_lossy(&edited.body)
    );
    let edited: AlertRule = serde_json::from_slice(&edited.body).unwrap();
    assert_eq!(edited.rule_id, rule.rule_id);
    assert_eq!(edited.dedupe_window_seconds, 300);
    assert_eq!(edited.re_alert_after_seconds, 1_800);
    assert!(!edited.notify_on_resolve);
    assert_eq!(edited.updated_by, "station-operator");
    assert!(edited.updated_at_unix_ms > 0);

    let reopened = Api::open(&database).unwrap();
    let persisted: Vec<AlertRule> =
        serde_json::from_slice(&reopened.handle("GET", "/api/v1/alert-rules", None).body).unwrap();
    assert!(persisted.contains(&edited));
    let _ = std::fs::remove_file(database);
}

#[test]
fn exposes_the_latest_persisted_system_resource_sample() {
    let database = temporary_database("system-resources-api");
    let api = Api::open(&database).unwrap();
    assert_eq!(
        api.handle("GET", "/api/v1/system-resources/latest", None)
            .body,
        b"null"
    );
    let recorded = StationStore::open(&database)
        .unwrap()
        .record_system_resource_sample(&SystemResourceSample {
            sample_id: None,
            sampled_at_unix_ms: 5_000,
            cpu_percent: Some(25.0),
            ram_used_gb: Some(8.0),
            ram_total_gb: Some(16.0),
            gpu_percent: None,
            gpu_vram_used_gb: None,
            media_volume_free_gb: Some(100.0),
            backup_volume_free_gb: Some(90.0),
            database_reachable: true,
            backup_volume_writable: true,
            service_running: true,
            clock_skew_seconds: None,
        })
        .unwrap();
    let response = api.handle("GET", "/api/v1/system-resources/latest", None);
    assert_eq!(response.status, 200);
    assert_eq!(
        serde_json::from_slice::<SystemResourceSample>(&response.body).unwrap(),
        recorded
    );
    let _ = std::fs::remove_file(database);
}

#[test]
fn exposes_editable_self_test_schedules_and_runs_a_durable_manual_check() {
    let database = temporary_database("self-tests-api");
    let api = Api::open(&database).unwrap();
    let schedules: Vec<SelfTestSchedule> =
        serde_json::from_slice(&api.handle("GET", "/api/v1/self-test-schedules", None).body)
            .unwrap();
    assert_eq!(schedules.len(), 2);
    assert_eq!(schedules[0].kind, SelfTestKind::Daily);
    assert_eq!(schedules[1].kind, SelfTestKind::Weekly);

    let edited = api.handle(
        "PUT",
        "/api/v1/self-test-schedules/daily",
        Some(
            br#"{"enabled":true,"local_hour":1,"local_minute":30,"weekday":null,"updated_by":"station-operator"}"#,
        ),
    );
    assert_eq!(edited.status, 200);
    let edited: SelfTestSchedule = serde_json::from_slice(&edited.body).unwrap();
    assert_eq!(edited.local_hour, 1);
    assert_eq!(edited.local_minute, 30);
    assert_eq!(edited.updated_by, "station-operator");
    assert_eq!(
        api.handle(
            "PUT",
            "/api/v1/self-test-schedules/daily",
            Some(
                br#"{"enabled":true,"local_hour":1,"local_minute":30,"weekday":2,"updated_by":"station-operator"}"#,
            ),
        )
        .status,
        422
    );

    let run = api.handle("POST", "/api/v1/self-tests/run?kind=daily", None);
    assert_eq!(run.status, 201, "{}", String::from_utf8_lossy(&run.body));
    let record: SystemSelfTest = serde_json::from_slice(&run.body).unwrap();
    assert_eq!(record.kind, SelfTestKind::Daily);
    assert_eq!(record.trigger, SelfTestTrigger::Manual);
    assert_eq!(record.schedule_period_key, None);
    let history: Vec<SystemSelfTest> =
        serde_json::from_slice(&api.handle("GET", "/api/v1/self-tests", None).body).unwrap();
    assert_eq!(history, vec![record.clone()]);

    drop(api);
    let _ = std::fs::remove_file(record.evidence_path.unwrap());
    let _ = std::fs::remove_file(database);
}

#[test]
fn creates_and_downloads_a_manifested_support_bundle_without_path_escape() {
    let root = temporary_database("support-bundle-root");
    std::fs::create_dir_all(&root).unwrap();
    let database = root.join("station.db");
    let api = Api::open(&database).expect("database opens");

    let created = api.handle("POST", "/api/v1/support-bundles", None);
    assert_eq!(created.status, 201);
    let record: station_support::SupportBundleRecord =
        serde_json::from_slice(&created.body).unwrap();
    assert!(record.entries.iter().any(|name| name == "manifest.json"));
    assert_eq!(record.sha256.len(), 64);
    assert!(record.size_bytes > 0);
    assert!(
        record
            .excludes
            .iter()
            .any(|item| item.contains("credentials"))
    );

    let downloaded = api.handle("GET", &record.download_uri, None);
    assert_eq!(downloaded.status, 200);
    assert_eq!(downloaded.content_type, "application/zip");
    assert_eq!(
        downloaded.attachment_name.as_deref(),
        Some(record.file_name.as_str())
    );
    assert!(downloaded.body.starts_with(b"PK"));
    let archive = zip::ZipArchive::new(std::io::Cursor::new(&downloaded.body)).unwrap();
    assert!(archive.file_names().any(|name| name == "manifest.json"));

    let escaped = api.handle("GET", "/api/v1/support-bundles/..%2Fstation/download", None);
    assert_eq!(escaped.status, 404);

    let wire = http_request(
        &database,
        format!(
            "GET {} HTTP/1.1\r\nHost: localhost\r\n\r\n",
            record.download_uri
        )
        .as_bytes(),
    );
    let header_end = wire
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .unwrap();
    let headers = String::from_utf8(wire[..header_end].to_vec()).unwrap();
    assert!(headers.starts_with("HTTP/1.1 200 OK\r\n"));
    assert!(headers.contains("Content-Type: application/zip\r\n"));
    assert!(headers.contains(&format!(
        "Content-Disposition: attachment; filename=\"{}\"\r\n",
        record.file_name
    )));
    assert!(headers.contains("Cache-Control: no-store\r\n"));
    assert!(headers.contains("X-Content-Type-Options: nosniff\r\n"));

    drop(api);
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn reports_an_honest_unconfigured_update_state_and_refuses_a_network_check() {
    let root = temporary_database("updates-unconfigured");
    let application = root.join("application");
    let data = root.join("data");
    std::fs::create_dir_all(&application).unwrap();
    std::fs::create_dir_all(&data).unwrap();
    let database = data.join("station.db");
    std::fs::write(
        data.join("install-receipt.json"),
        br#"{"product":"TownLight Station","version":"0.1.0","source_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}"#,
    )
    .unwrap();
    let api = Api::open_with_application_directory(&database, &application).unwrap();

    let status = api.handle("GET", "/api/v1/updates/status", None);
    assert_eq!(status.status, 200);
    let document: serde_json::Value = serde_json::from_slice(&status.body).unwrap();
    assert_eq!(document["configured"], false);
    assert_eq!(document["current_version"], "0.1.0");
    assert_eq!(document["latest_check"], serde_json::Value::Null);
    assert_eq!(document["staged_package"], serde_json::Value::Null);
    assert!(
        document["next_step"]
            .as_str()
            .unwrap()
            .contains("trust file")
    );

    let check = api.handle("POST", "/api/v1/updates/check", None);
    assert_eq!(check.status, 409);
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&check.body).unwrap()["error"]["code"],
        "updates_not_configured"
    );
    let stage = api.handle("POST", "/api/v1/updates/stage", None);
    assert_eq!(stage.status, 409);
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&stage.body).unwrap()["error"]["code"],
        "updates_not_configured"
    );
    let notes = api.handle("GET", "/api/v1/updates/release-notes?version=0.2.0", None);
    assert_eq!(notes.status, 404);

    drop(api);
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn configures_watch_folders_without_allowing_clients_to_forge_scan_health() {
    let database = temporary_database("watch-folders");
    let api = Api::open(&database).unwrap();
    let config = WatchFolderConfig {
        config_id: "780934dc-96e3-46bd-83c4-a2c10ec5c33d".into(),
        display_name: "Council recordings".into(),
        monitor_path: database
            .with_extension("incoming")
            .to_string_lossy()
            .into_owned(),
        extensions: vec![".mov".into(), ".mp4".into(), ".ts".into()],
        settle_seconds: 10,
        enabled: true,
        last_scan_at_unix_ms: None,
        last_error: None,
    };
    let stored = api.handle(
        "PUT",
        "/api/v1/watch-folders",
        Some(&serde_json::to_vec(&config).unwrap()),
    );
    assert_eq!(
        stored.status,
        200,
        "{}",
        String::from_utf8_lossy(&stored.body)
    );
    let listed = api.handle("GET", "/api/v1/watch-folders", None);
    assert_eq!(listed.status, 200);
    assert_eq!(
        serde_json::from_slice::<Vec<WatchFolderConfig>>(&listed.body).unwrap(),
        vec![config.clone()]
    );

    let mut forged = config;
    forged.last_error = Some("Everything is fine".into());
    assert_eq!(
        api.handle(
            "PUT",
            "/api/v1/watch-folders",
            Some(&serde_json::to_vec(&forged).unwrap()),
        )
        .status,
        422
    );

    let _ = std::fs::remove_file(database);
}

#[test]
fn admits_real_media_into_the_managed_library_and_refuses_declared_readiness() {
    let marker = temporary_database("managed-ingest");
    let root = marker.with_extension("ingest-root");
    let database = root.join("station.db");
    let library = root.join("media");
    let source = root.join("incoming.ts");
    std::fs::create_dir_all(&root).unwrap();
    generate_media_fixture(&source);
    let api = Api::open(&database).unwrap();
    let body = serde_json::to_vec(&serde_json::json!({
        "job_id": "7a49c96c-12da-4c3c-a144-739049eb19af",
        "source_path": source.to_string_lossy()
    }))
    .unwrap();
    let admitted = api.handle("POST", "/api/v1/assets/ingest", Some(&body));
    assert_eq!(
        admitted.status,
        202,
        "{}",
        String::from_utf8_lossy(&admitted.body)
    );
    let job: serde_json::Value = serde_json::from_slice(&admitted.body).unwrap();
    assert_eq!(job["status"], "pending");
    assert_eq!(job["progress_percent"], 0);
    assert!(
        !library.exists(),
        "the API request must not perform ingest work"
    );
    let fetched = api.handle(
        "GET",
        "/api/v1/assets/ingest/jobs/7a49c96c-12da-4c3c-a144-739049eb19af",
        None,
    );
    assert_eq!(fetched.status, 200);
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&fetched.body).unwrap(),
        job
    );
    let outcome = IngestWorker::new(&database, &library)
        .run_once()
        .unwrap()
        .unwrap();
    let transcode_job_id = match outcome {
        IngestOutcome::AwaitingTranscode {
            transcode_job_id, ..
        } => transcode_job_id,
        other => panic!("real media did not reach transcode: {other:?}"),
    };
    let queued_transcode = api.handle(
        "GET",
        &format!("/api/v1/assets/transcode/jobs/{transcode_job_id}"),
        None,
    );
    assert_eq!(queued_transcode.status, 200);
    let queued_transcode: serde_json::Value =
        serde_json::from_slice(&queued_transcode.body).unwrap();
    assert_eq!(queued_transcode["status"], "pending");
    let processing = api.handle("GET", "/api/v1/assets/readiness-dashboard", None);
    let processing: serde_json::Value = serde_json::from_slice(&processing.body).unwrap();
    assert_eq!(processing["awaiting_transcode_ingest_count"], 1);
    assert_eq!(processing["pending_transcode_count"], 1);
    assert_eq!(processing["running_transcode_count"], 0);
    assert_eq!(processing["failed_transcode_count"], 0);
    assert_eq!(processing["transcode_jobs"][0]["job_id"], transcode_job_id);
    let asset_id = match TranscodeWorker::new(&database, &library)
        .run_once()
        .unwrap()
        .unwrap()
    {
        TranscodeOutcome::Completed { asset_id, .. } => asset_id,
        other => panic!("real media did not complete canonical transcode: {other:?}"),
    };
    let completed = api.handle(
        "GET",
        "/api/v1/assets/ingest/jobs/7a49c96c-12da-4c3c-a144-739049eb19af",
        None,
    );
    let completed: serde_json::Value = serde_json::from_slice(&completed.body).unwrap();
    assert_eq!(completed["status"], "completed");
    assert_eq!(completed["progress_percent"], 100);
    assert_eq!(completed["asset_id"], asset_id);
    let fetched_asset = api.handle("GET", &format!("/api/v1/assets/{asset_id}"), None);
    let asset: MediaAsset = serde_json::from_slice(&fetched_asset.body).unwrap();
    assert_eq!(fetched_asset.status, 200);
    assert_eq!(asset.readiness, AssetReadiness::Ready);
    assert!(Path::new(&asset.media_path).starts_with(std::fs::canonicalize(&library).unwrap()));
    let readiness = api.handle("GET", &format!("/api/v1/assets/{asset_id}/readiness"), None);
    assert_eq!(readiness.status, 200);
    let readiness: serde_json::Value = serde_json::from_slice(&readiness.body).unwrap();
    assert_eq!(readiness["loudness_status"], "ok");
    assert!(readiness["measured_lufs"].as_f64().is_some());
    assert_eq!(readiness["target_lufs"], -24.0);

    let forged = MediaAsset {
        asset_id: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb".into(),
        media_path: r"C:\not-managed\forged.ts".into(),
        duration_ms: 60_000,
        readiness: AssetReadiness::Ready,
    };
    assert_eq!(
        api.handle(
            "PUT",
            "/api/v1/assets",
            Some(&serde_json::to_vec(&forged).unwrap()),
        )
        .status,
        404
    );
    assert_eq!(
        api.handle(
            "POST",
            "/api/v1/assets/ingest",
            Some(
                br#"{"job_id":"0c433632-f893-49bb-b689-57bfac44d29b","source_path":"relative.ts"}"#
            ),
        )
        .status,
        422
    );
    let stored_before_rejection = std::fs::read_dir(&library).unwrap().count();
    let invalid_source = root.join("not-media.txt");
    std::fs::write(&invalid_source, b"not a media asset").unwrap();
    let invalid_body = serde_json::to_vec(&serde_json::json!({
        "job_id": "640068de-2886-46f5-981a-0144597ce2aa",
        "source_path": invalid_source.to_string_lossy()
    }))
    .unwrap();
    assert_eq!(
        api.handle("POST", "/api/v1/assets/ingest", Some(&invalid_body))
            .status,
        202
    );
    assert!(matches!(
        IngestWorker::new(&database, &library).run_once().unwrap(),
        Some(IngestOutcome::Failed { .. })
    ));
    let failed = api.handle(
        "GET",
        "/api/v1/assets/ingest/jobs/640068de-2886-46f5-981a-0144597ce2aa",
        None,
    );
    let failed: serde_json::Value = serde_json::from_slice(&failed.body).unwrap();
    assert_eq!(failed["status"], "failed");
    assert!(failed["error_detail"].as_str().unwrap().contains("Media"));
    assert_eq!(
        std::fs::read_dir(&library).unwrap().count(),
        stored_before_rejection
    );
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64;
    let store = StationStore::open(&database).unwrap();
    store.put_media_asset(&forged).unwrap();
    store
        .put_schedule_item(&ScheduleItem {
            item_id: "185dc897-a14c-45f0-bd6a-899fd90cd196".into(),
            channel_id: "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e".into(),
            asset_id: forged.asset_id.clone(),
            title: "Missing council recording".into(),
            starts_at_unix_ms: now + 60_000,
            duration_ms: 30_000,
            state: ScheduleState::Committed,
        })
        .unwrap();
    drop(store);
    let dashboard = api.handle("GET", "/api/v1/assets/readiness-dashboard", None);
    assert_eq!(dashboard.status, 200);
    let dashboard: serde_json::Value = serde_json::from_slice(&dashboard.body).unwrap();
    assert_eq!(dashboard["ready_count"], 1);
    assert_eq!(dashboard["missing_count"], 1);
    assert_eq!(dashboard["failed_ingest_count"], 1);
    let missing = api.handle(
        "GET",
        &format!("/api/v1/assets/missing-scheduled?now_unix_ms={now}&horizon_ms=604800000"),
        None,
    );
    assert_eq!(missing.status, 200);
    let missing: serde_json::Value = serde_json::from_slice(&missing.body).unwrap();
    assert_eq!(missing.as_array().unwrap().len(), 1);
    assert_eq!(
        missing[0]["schedule_item_id"],
        "185dc897-a14c-45f0-bd6a-899fd90cd196"
    );
    assert_eq!(missing[0]["readiness_state"], "missing_file");
    assert_eq!(
        api.handle(
            "GET",
            "/api/v1/assets/missing-scheduled?now_unix_ms=1",
            None,
        )
        .status,
        400
    );
    assert_eq!(
        api.handle(
            "GET",
            "/api/v1/assets/missing-scheduled?now_unix_ms=1&horizon_ms=2678400001",
            None,
        )
        .status,
        422
    );
    assert_eq!(
        api.handle("POST", "/api/v1/assets/ingest", Some(&body))
            .status,
        409
    );
    let invalid_job = serde_json::to_vec(&serde_json::json!({
        "job_id": "not-a-uuid",
        "source_path": source.to_string_lossy()
    }))
    .unwrap();
    assert_eq!(
        api.handle("POST", "/api/v1/assets/ingest", Some(&invalid_job))
            .status,
        422
    );
    let missing_source = serde_json::to_vec(&serde_json::json!({
        "job_id": "89a1cfac-81a6-4500-b712-6688fb71bda7",
        "source_path": root.join("missing.ts").to_string_lossy()
    }))
    .unwrap();
    assert_eq!(
        api.handle("POST", "/api/v1/assets/ingest", Some(&missing_source),)
            .status,
        422
    );
    assert_eq!(
        api.handle("GET", "/api/v1/assets/not-a-digest", None)
            .status,
        400
    );
    drop(api);
    let _ = std::fs::remove_file(database);
    std::fs::remove_dir_all(root).unwrap();
}

fn generate_media_fixture(path: &Path) {
    gst::init().unwrap();
    let pipeline = gst::Pipeline::new();
    let video = gst::ElementFactory::make("videotestsrc")
        .property("num-buffers", 90_i32)
        .build()
        .unwrap();
    let video_convert = gst::ElementFactory::make("videoconvert").build().unwrap();
    let video_caps = gst::ElementFactory::make("capsfilter")
        .property(
            "caps",
            gst::Caps::builder("video/x-raw")
                .field("format", "I420")
                .field("width", 320_i32)
                .field("height", 180_i32)
                .field("framerate", gst::Fraction::new(30, 1))
                .build(),
        )
        .build()
        .unwrap();
    let video_encoder = gst::ElementFactory::make("openh264enc").build().unwrap();
    let video_parser = gst::ElementFactory::make("h264parse").build().unwrap();
    let audio = gst::ElementFactory::make("audiotestsrc")
        .property("num-buffers", 141_i32)
        .build()
        .unwrap();
    let audio_convert = gst::ElementFactory::make("audioconvert").build().unwrap();
    let audio_resample = gst::ElementFactory::make("audioresample").build().unwrap();
    let audio_encoder = gst::ElementFactory::make("voaacenc").build().unwrap();
    let audio_parser = gst::ElementFactory::make("aacparse").build().unwrap();
    let mux = gst::ElementFactory::make("mpegtsmux").build().unwrap();
    let sink = gst::ElementFactory::make("filesink")
        .property("location", path.to_string_lossy().as_ref())
        .build()
        .unwrap();
    pipeline
        .add_many([
            &video,
            &video_convert,
            &video_caps,
            &video_encoder,
            &video_parser,
            &audio,
            &audio_convert,
            &audio_resample,
            &audio_encoder,
            &audio_parser,
            &mux,
            &sink,
        ])
        .unwrap();
    gst::Element::link_many([
        &video,
        &video_convert,
        &video_caps,
        &video_encoder,
        &video_parser,
        &mux,
        &sink,
    ])
    .unwrap();
    gst::Element::link_many([
        &audio,
        &audio_convert,
        &audio_resample,
        &audio_encoder,
        &audio_parser,
        &mux,
    ])
    .unwrap();
    pipeline.set_state(gst::State::Playing).unwrap();
    let bus = pipeline.bus().unwrap();
    for message in bus.iter_timed(gst::ClockTime::from_seconds(10)) {
        match message.view() {
            gst::MessageView::Eos(_) => break,
            gst::MessageView::Error(error) => panic!("fixture pipeline failed: {}", error.error()),
            _ => {}
        }
    }
    pipeline.set_state(gst::State::Null).unwrap();
    assert!(path.is_file());
}

#[test]
fn prepares_commits_and_persists_an_operator_approval_through_the_api() {
    let database = temporary_database("schedule-commit");
    let api = Api::open(&database).unwrap();
    let scheduled_media = database.with_extension("scheduled.ts");
    std::fs::write(&scheduled_media, b"admitted bytes").unwrap();
    let asset = MediaAsset {
        asset_id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into(),
        media_path: scheduled_media.to_string_lossy().into_owned(),
        duration_ms: 60_000,
        readiness: AssetReadiness::Ready,
    };
    StationStore::open(&database)
        .unwrap()
        .put_media_asset(&asset)
        .unwrap();
    let item = ScheduleItem {
        item_id: "256d5a07-92d3-4718-aec9-05cad42fae7d".into(),
        channel_id: "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e".into(),
        asset_id: asset.asset_id.clone(),
        title: "City Council".into(),
        starts_at_unix_ms: 100_000,
        duration_ms: 60_000,
        state: ScheduleState::Draft,
    };
    assert_eq!(
        api.handle(
            "PUT",
            "/api/v1/schedule/items",
            Some(&serde_json::to_vec(&item).unwrap())
        )
        .status,
        200
    );
    let prepare = api.handle(
        "POST",
        "/api/v1/schedule/prepare",
        Some(br#"{"plan_id":"plan-1","schedule_item_id":"256d5a07-92d3-4718-aec9-05cad42fae7d"}"#),
    );
    assert_eq!(prepare.status, 200);
    assert!(
        serde_json::from_slice::<CommitPlan>(&prepare.body)
            .unwrap()
            .dry_run_passed
    );

    let commit = api.handle(
        "POST",
        "/api/v1/schedule/commit",
        Some(br#"{"report_id":"report-1","plan_id":"plan-1","schedule_item_id":"256d5a07-92d3-4718-aec9-05cad42fae7d","approved_by":"operator-scott","operator_notes":"Reviewed."}"#),
    );
    assert_eq!(commit.status, 201);
    let report: CommitReport = serde_json::from_slice(&commit.body).unwrap();
    assert_eq!(report.dispatch_status, DispatchStatus::Pending);
    let fetched = api.handle("GET", "/api/v1/schedule/commits/report-1", None);
    assert_eq!(fetched.status, 200);
    assert_eq!(
        serde_json::from_slice::<CommitReport>(&fetched.body).unwrap(),
        report
    );
    let listed = api.handle(
        "GET",
        "/api/v1/schedule/items?channel_id=8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e",
        None,
    );
    let items: Vec<ScheduleItem> = serde_json::from_slice(&listed.body).unwrap();
    assert_eq!(items.len(), 1);
    assert_eq!(items[0].state, ScheduleState::Committed);

    let _ = std::fs::remove_file(database);
    std::fs::remove_file(scheduled_media).unwrap();
}

#[test]
fn serves_the_station_profile_through_the_http_boundary() {
    let database = temporary_database("http");
    let body = br#"{"station_id":"3f5f721f-96c7-48b1-b061-1bf1ad1e62c2","display_name":"KTLT","timezone":"America/Denver"}"#;
    let request = format!(
        "PUT /api/v1/station HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n",
        body.len()
    );
    let mut bytes = request.into_bytes();
    bytes.extend_from_slice(body);
    let response = String::from_utf8(http_request(&database, &bytes)).unwrap();
    assert!(response.starts_with("HTTP/1.1 200 OK\r\n"));
    assert!(response.contains("\r\nContent-Type: application/json\r\n"));
    let response_body = response.split("\r\n\r\n").nth(1).unwrap();
    let stored: serde_json::Value = serde_json::from_str(response_body).unwrap();
    assert_eq!(stored["display_name"], "KTLT");
    assert_eq!(stored["revision"], 1);
    let _ = std::fs::remove_file(database);
}

#[test]
fn acknowledges_expect_continue_before_waiting_for_the_request_body() {
    let database = temporary_database("expect-continue");
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let server_database = database.clone();
    let server = thread::spawn(move || serve_one(listener, server_database));
    let body = br#"{"station_id":"3f5f721f-96c7-48b1-b061-1bf1ad1e62c2","display_name":"KTLT","timezone":"America/Denver"}"#;
    let headers = format!(
        "PUT /api/v1/station HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {}\r\nExpect: 100-continue\r\n\r\n",
        body.len()
    );
    let mut client = TcpStream::connect(address).unwrap();
    client
        .set_read_timeout(Some(Duration::from_secs(1)))
        .unwrap();
    client.write_all(headers.as_bytes()).unwrap();

    let mut interim = [0_u8; 25];
    client.read_exact(&mut interim).unwrap();
    assert_eq!(&interim, b"HTTP/1.1 100 Continue\r\n\r\n");

    client.write_all(body).unwrap();
    let mut response = Vec::new();
    client.read_to_end(&mut response).unwrap();
    assert!(response.starts_with(b"HTTP/1.1 200 OK\r\n"));
    server.join().unwrap().unwrap();
    let _ = std::fs::remove_file(database);
}

#[test]
fn rejects_a_stale_profile_revision_without_losing_the_winning_update() {
    let database = temporary_database("revision");
    let api = Api::open(&database).expect("database opens");
    let initial = br#"{"station_id":"3f5f721f-96c7-48b1-b061-1bf1ad1e62c2","display_name":"KTLT","timezone":"America/Denver","expected_revision":0}"#;
    assert_eq!(
        api.handle("PUT", "/api/v1/station", Some(initial)).status,
        200
    );
    let winning = br#"{"station_id":"3f5f721f-96c7-48b1-b061-1bf1ad1e62c2","display_name":"KTLT Updated","timezone":"America/Denver","expected_revision":1}"#;
    assert_eq!(
        api.handle("PUT", "/api/v1/station", Some(winning)).status,
        200
    );
    let stale = br#"{"station_id":"3f5f721f-96c7-48b1-b061-1bf1ad1e62c2","display_name":"KTLT Stale","timezone":"America/Denver","expected_revision":1}"#;
    let conflict = api.handle("PUT", "/api/v1/station", Some(stale));
    assert_eq!(conflict.status, 409);
    assert!(
        String::from_utf8(conflict.body)
            .unwrap()
            .contains("revision_conflict")
    );
    let stored: serde_json::Value =
        serde_json::from_slice(&api.handle("GET", "/api/v1/station", None).body).unwrap();
    assert_eq!(stored["display_name"], "KTLT Updated");
    assert_eq!(stored["revision"], 2);
    drop(api);
    let _ = std::fs::remove_file(database);
}

#[test]
fn serves_requests_until_a_cooperative_stop_is_requested() {
    let database = temporary_database("cooperative-stop");
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let stop = Arc::new(AtomicBool::new(false));
    let server_stop = Arc::clone(&stop);
    let server_database = database.clone();
    let (finished_tx, finished_rx) = mpsc::channel();
    thread::spawn(move || {
        let _ = finished_tx.send(serve_until(listener, server_database, server_stop));
    });

    let mut client = TcpStream::connect(address).unwrap();
    client
        .set_read_timeout(Some(Duration::from_millis(500)))
        .unwrap();
    client
        .write_all(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
        .unwrap();
    let mut response = Vec::new();
    let _ = client.read_to_end(&mut response);
    stop.store(true, Ordering::Release);
    let server_result = finished_rx.recv_timeout(Duration::from_secs(1)).unwrap();

    assert!(server_result.is_ok(), "server returned {server_result:?}");
    assert!(response.starts_with(b"HTTP/1.1 200 OK\r\n"));
    let _ = std::fs::remove_file(database);
}

#[test]
fn allows_an_accepted_client_time_to_send_its_request() {
    let database = temporary_database("delayed-client-write");
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let stop = Arc::new(AtomicBool::new(false));
    let server_stop = Arc::clone(&stop);
    let server_database = database.clone();
    let (finished_tx, finished_rx) = mpsc::channel();
    thread::spawn(move || {
        let _ = finished_tx.send(serve_until(listener, server_database, server_stop));
    });

    let mut client = TcpStream::connect(address).unwrap();
    client
        .set_read_timeout(Some(Duration::from_secs(1)))
        .unwrap();
    thread::sleep(Duration::from_millis(100));
    client
        .write_all(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
        .unwrap();
    let mut response = Vec::new();
    client.read_to_end(&mut response).unwrap();
    assert!(response.starts_with(b"HTTP/1.1 200 OK\r\n"));

    stop.store(true, Ordering::Release);
    assert!(
        finished_rx
            .recv_timeout(Duration::from_secs(1))
            .unwrap()
            .is_ok()
    );
    let _ = std::fs::remove_file(database);
}

#[test]
fn keeps_health_responsive_while_another_request_is_stalled() {
    let database = temporary_database("concurrent-health");
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let stop = Arc::new(AtomicBool::new(false));
    let server_stop = Arc::clone(&stop);
    let server_database = database.clone();
    let (finished_tx, finished_rx) = mpsc::channel();
    thread::spawn(move || {
        let _ = finished_tx.send(serve_until(listener, server_database, server_stop));
    });

    let mut stalled = TcpStream::connect(address).unwrap();
    stalled
        .write_all(b"POST /api/v1/updates/stage HTTP/1.1\r\nHost: localhost\r\n")
        .unwrap();
    thread::sleep(Duration::from_millis(50));

    let mut health = TcpStream::connect(address).unwrap();
    health
        .set_read_timeout(Some(Duration::from_secs(1)))
        .unwrap();
    health
        .write_all(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
        .unwrap();
    let mut response = Vec::new();
    health.read_to_end(&mut response).unwrap();
    assert!(response.starts_with(b"HTTP/1.1 200 OK\r\n"));

    drop(stalled);
    stop.store(true, Ordering::Release);
    assert!(
        finished_rx
            .recv_timeout(Duration::from_secs(1))
            .unwrap()
            .is_ok()
    );
    let _ = std::fs::remove_file(database);
}
