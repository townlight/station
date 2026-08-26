use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
    mpsc,
};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

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
use station_media_lifecycle::{
    AssetLoudnessAnalysis, IngestJobStatus, IngestSourceKind, LoudnessGateStatus, MediaIngestJob,
    WatchFolderConfig,
};
use station_schedule::{
    AssetReadiness, ChannelConfiguration, DispatchStatus, MediaAsset, OutputProfileId,
    OutputSinkConfiguration, ScheduleItem, ScheduleState,
};
use station_storage::{ScheduleItemWrite, StationStore};

trait ScheduleStoreTestExt {
    fn put_schedule_item(&self, item: &station_schedule::ScheduleItem) -> Result<(), String>;
}

impl ScheduleStoreTestExt for StationStore {
    fn put_schedule_item(&self, item: &station_schedule::ScheduleItem) -> Result<(), String> {
        if item.state != ScheduleState::Draft {
            return Err(
                "test schedule fixtures must seed drafts through the public write path".into(),
            );
        }
        self.write_schedule_item(&ScheduleItemWrite {
            item: item.clone(),
            changed_by: "test-harness".into(),
            occurred_at_unix_ms: 1,
        })
    }
}

fn temporary_database(name: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is before Unix epoch")
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-station-{name}-{nonce}.db"))
}

fn remove_directory(path: &Path) {
    // Windows media/GStreamer teardown can release the final mapped file after
    // the pipeline has reached NULL. Keep cleanup bounded, but allow that
    // asynchronous handle release instead of turning it into a product failure.
    let deadline = Instant::now() + Duration::from_secs(30);
    loop {
        match std::fs::remove_dir_all(path) {
            Ok(()) => return,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return,
            Err(_) if Instant::now() < deadline => thread::sleep(Duration::from_millis(25)),
            Err(error) => panic!("could not remove {}: {error}", path.display()),
        }
    }
}

fn http_request(database: &Path, request: &[u8]) -> Vec<u8> {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let database = database.to_path_buf();
    let server = thread::spawn(move || serve_one(listener, database));
    let mut client = TcpStream::connect(address).unwrap();
    client
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let header_end = request
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .map(|index| index + 4)
        .unwrap_or(request.len());
    let header = String::from_utf8(request[..header_end].to_vec())
        .unwrap()
        .replace("Host: localhost", &format!("Host: {address}"))
        .replace(
            "Origin: http://localhost",
            &format!("Origin: http://{address}"),
        );
    let mut bound_request = header.into_bytes();
    bound_request.extend_from_slice(&request[header_end..]);
    client.write_all(&bound_request).unwrap();
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

fn http_request_to(address: std::net::SocketAddr, request: &[u8]) -> Vec<u8> {
    let mut client = TcpStream::connect(address).unwrap();
    client
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    client.write_all(request).unwrap();
    let mut response = Vec::new();
    client.read_to_end(&mut response).unwrap();
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
    let put = api.handle_in_process(
        "PUT",
        "/api/v1/station",
        Some(&serde_json::to_vec(&profile).unwrap()),
    );
    assert_eq!(put.status, 200);
    drop(api);
    let restarted = Api::open(&database).expect("database reopens");
    let get = restarted.handle_in_process("GET", "/api/v1/station", None);
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
        api.handle_in_process("PUT", "/api/v1/station", Some(valid))
            .status,
        200
    );
    let invalid = br#"{"station_id":"not-an-id","display_name":"","timezone":"UTC+7"}"#;
    let response = api.handle_in_process("PUT", "/api/v1/station", Some(invalid));
    assert_eq!(response.status, 422);
    assert!(
        String::from_utf8(response.body)
            .unwrap()
            .contains("validation_failed")
    );
    let get = api.handle_in_process("GET", "/api/v1/station", None);
    let stored: StationProfile = serde_json::from_slice(&get.body).unwrap();
    assert_eq!(stored.display_name, "KTLT");
    let _ = std::fs::remove_file(database);
}

#[test]
fn health_reports_database_readiness_and_unknown_routes_are_not_found() {
    let database = temporary_database("health");
    let api = Api::open(&database).expect("database opens");
    let health = api.handle_in_process("GET", "/health", None);
    assert_eq!(health.status, 200);
    assert_eq!(
        health.body,
        br#"{"database":"ready","orchestration":{"status":"ready"},"status":"ready"}"#
    );
    assert_eq!(
        api.handle_in_process("GET", "/not-a-route", None).status,
        404
    );
    let _ = std::fs::remove_file(database);
}

#[test]
fn commissioning_report_is_ordered_and_does_not_claim_field_proof() {
    let database = temporary_database("commissioning-report");
    let api = Api::open(&database).expect("database opens");
    let empty = api.handle_in_process("GET", "/api/v1/commissioning/report", None);
    assert_eq!(empty.status, 200);
    let report: serde_json::Value = serde_json::from_slice(&empty.body).unwrap();
    assert_eq!(report["schema_version"], 2);
    assert_eq!(report["overall_status"], "blocked");
    assert_eq!(report["current_step_id"], "station_profile");
    assert_eq!(report["core_complete"], false);
    assert_eq!(report["core_progress"]["total"], 19);
    let checks = report["checks"].as_array().unwrap();
    assert_eq!(
        checks
            .iter()
            .map(|check| check["id"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec![
            "station_profile",
            "operator_account_auth",
            "runtime_api",
            "station_box_storage",
            "station_time",
            "station_backup",
            "channel_watch_folder",
            "fallback_slate",
            "cea708_captions",
            "publish_targets",
            "automation_model_policy",
            "headend_output_setup",
            "cable_transport_validation",
            "guided_ingest",
            "guided_schedule",
            "guided_air_proof",
            "selected_sink_ten_minute_proof",
            "support_bundle",
            "first_broadcast_report",
            "external_receiver_proof",
            "clean_machine_install_proof",
            "one_station_week_proof",
            "three_station_week_proof",
        ]
    );
    let account = checks
        .iter()
        .find(|check| check["id"] == "operator_account_auth")
        .unwrap();
    assert_eq!(account["status"], "blocked");
    assert_eq!(account["required"], true);
    assert_eq!(account["blocking"], true);
    assert_eq!(account["boundary"], "local_authority");
    let headend = checks
        .iter()
        .find(|check| check["id"] == "headend_output_setup")
        .expect("commissioning must include the authoritative headend gate");
    assert_eq!(headend["status"], "blocked");
    assert_eq!(checks.last().unwrap()["id"], "three_station_week_proof");
    assert_eq!(checks.last().unwrap()["status"], "not_verified");
    assert_eq!(checks.last().unwrap()["blocking"], false);
    assert_eq!(checks.last().unwrap()["required"], false);
    assert_eq!(checks.last().unwrap()["boundary"], "external_evidence");
    assert!(
        checks.last().unwrap()["detail"]
            .as_str()
            .unwrap()
            .contains("not verified")
    );
    let receiver = checks
        .iter()
        .find(|check| check["id"] == "external_receiver_proof")
        .unwrap();
    assert_eq!(receiver["status"], "not_verified");

    let publish_targets = checks
        .iter()
        .find(|check| check["id"] == "publish_targets")
        .unwrap();
    assert_eq!(publish_targets["status"], "blocked");
    assert!(
        publish_targets["detail"]
            .as_str()
            .unwrap()
            .contains("No current-candidate publish-target connection authority")
    );
    assert!(
        publish_targets["evidence"]
            .as_str()
            .unwrap()
            .contains("connection-test authority only")
    );

    let automation = checks
        .iter()
        .find(|check| check["id"] == "automation_model_policy")
        .unwrap();
    assert_eq!(automation["status"], "blocked");
    assert!(
        automation["detail"]
            .as_str()
            .unwrap()
            .contains("No current-candidate automation connection authority")
    );
    assert!(
        automation["evidence"]
            .as_str()
            .unwrap()
            .contains("connection tests only")
    );

    let profile = br#"{"station_id":"3f5f721f-96c7-48b1-b061-1bf1ad1e62c2","display_name":"KTLT","timezone":"America/Denver"}"#;
    assert_eq!(
        api.handle_in_process("PUT", "/api/v1/station", Some(profile))
            .status,
        200
    );
    let commissioned = api.handle_in_process("GET", "/api/v1/commissioning/report", None);
    assert_eq!(
        commissioned.status,
        200,
        "{}",
        String::from_utf8_lossy(&commissioned.body)
    );
    let report: serde_json::Value = serde_json::from_slice(&commissioned.body).unwrap();
    assert_eq!(report["checks"][0]["status"], "pass");
    assert_eq!(report["overall_status"], "blocked");
    let _ = std::fs::remove_file(database);
}

#[test]
fn commissioning_watch_folder_requires_durable_success_and_present_directory_after_restart() {
    let database = temporary_database("commissioning-watch-folder");
    let watch_directory = database.with_extension("watch");
    std::fs::create_dir_all(&watch_directory).unwrap();
    let config_id = "9d4bd9b9-65df-4f79-9f6e-056af667c20d";
    let api = Api::open(&database).unwrap();
    let config = WatchFolderConfig {
        config_id: config_id.into(),
        display_name: "Commissioning intake".into(),
        monitor_path: watch_directory.to_string_lossy().into_owned(),
        extensions: vec![".ts".into()],
        settle_seconds: 5,
        enabled: true,
        last_scan_at_unix_ms: None,
        last_error: None,
    };
    assert_eq!(
        api.handle_in_process(
            "PUT",
            "/api/v1/watch-folders",
            Some(&serde_json::to_vec(&config).unwrap()),
        )
        .status,
        200
    );
    let pending = api.handle_in_process("GET", "/api/v1/commissioning/report", None);
    let pending: serde_json::Value = serde_json::from_slice(&pending.body).unwrap();
    let pending = pending["checks"]
        .as_array()
        .unwrap()
        .iter()
        .find(|check| check["id"] == "channel_watch_folder")
        .unwrap();
    assert_eq!(pending["status"], "blocked");
    drop(api);

    StationStore::open(&database)
        .unwrap()
        .record_watch_folder_scan(config_id, system_time_millis_for_test(), None)
        .unwrap();
    let restarted = Api::open(&database).unwrap();
    let passed = restarted.handle_in_process("GET", "/api/v1/commissioning/report", None);
    let passed: serde_json::Value = serde_json::from_slice(&passed.body).unwrap();
    let passed = passed["checks"]
        .as_array()
        .unwrap()
        .iter()
        .find(|check| check["id"] == "channel_watch_folder")
        .unwrap();
    assert_eq!(passed["status"], "pass");
    assert!(passed["evidence"].as_str().unwrap().contains(config_id));
    drop(restarted);

    remove_directory(&watch_directory);
    let missing = Api::open(&database).unwrap();
    let missing = missing.handle_in_process("GET", "/api/v1/commissioning/report", None);
    let missing: serde_json::Value = serde_json::from_slice(&missing.body).unwrap();
    let missing = missing["checks"]
        .as_array()
        .unwrap()
        .iter()
        .find(|check| check["id"] == "channel_watch_folder")
        .unwrap();
    assert_eq!(missing["status"], "blocked");

    let _ = std::fs::remove_file(database);
}

#[test]
fn commissioning_report_separates_schedule_authority_from_dispatch_execution() {
    let database = temporary_database("commissioning-progression");
    let media = database.with_extension("ts");
    std::fs::write(&media, b"admitted media").unwrap();
    let channel_id = "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e";
    let asset_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    let item_id = "256d5a07-92d3-4718-aec9-05cad42fae7d";
    let api = Api::open(&database).unwrap();
    let profile = br#"{"station_id":"3f5f721f-96c7-48b1-b061-1bf1ad1e62c2","display_name":"KTLT","timezone":"America/Denver"}"#;
    assert_eq!(
        api.handle_in_process("PUT", "/api/v1/station", Some(profile))
            .status,
        200
    );
    let channel = ChannelConfiguration {
        channel_id: channel_id.into(),
        display_name: "Primary".into(),
        output_profile: Default::default(),
        output_sinks: vec![OutputSinkConfiguration::required_udp(
            "22222222-3333-4444-8555-666666666666",
            "Primary",
            "127.0.0.1:5500",
            -16.0,
        )],
        enabled: true,
    };
    assert_eq!(
        api.handle_in_process(
            "PUT",
            "/api/v1/channels",
            Some(&serde_json::to_vec(&channel).unwrap()),
        )
        .status,
        200
    );
    drop(api);
    let now = system_time_millis_for_test();
    let store = StationStore::open(&database).unwrap();
    store
        .put_channel_runtime(&ChannelRuntimeObservation {
            channel_id: channel_id.into(),
            state: ChannelRuntimeState::HealthyFallback,
            on_air_asset_id: None,
            state_started_at_unix_ms: now - 1_000,
            heartbeat_at_unix_ms: now,
            detail: None,
        })
        .unwrap();
    store
        .put_media_asset(&MediaAsset {
            asset_id: asset_id.into(),
            media_path: media.to_string_lossy().into_owned(),
            duration_ms: 60_000,
            readiness: AssetReadiness::Ready,
        })
        .unwrap();
    store
        .put_schedule_item(&ScheduleItem {
            item_id: item_id.into(),
            revision: 1,
            channel_id: channel_id.into(),
            asset_id: asset_id.into(),
            title: "City Council".into(),
            starts_at_unix_ms: now + 60_000,
            duration_ms: 60_000,
            state: ScheduleState::Draft,
        })
        .unwrap();
    let report = store
        .commit_schedule(
            "report-commissioning",
            "plan-commissioning",
            item_id,
            "operator",
            now,
            "",
        )
        .unwrap();
    assert_eq!(report.dispatch_status, DispatchStatus::Pending);
    drop(store);

    let api = Api::open(&database).unwrap();
    let response = api.handle_in_process("GET", "/api/v1/commissioning/report", None);
    assert_eq!(response.status, 200);
    let report: serde_json::Value = serde_json::from_slice(&response.body).unwrap();
    let checks = report["checks"].as_array().unwrap();
    let find = |id: &str| checks.iter().find(|check| check["id"] == id).unwrap();
    assert_eq!(find("publish_targets")["status"], "blocked");
    assert_eq!(find("guided_ingest")["status"], "blocked");
    assert_eq!(find("guided_schedule")["status"], "blocked");
    assert_eq!(find("guided_air_proof")["status"], "blocked");
    drop(api);
    let store = StationStore::open(&database).unwrap();
    assert!(
        store
            .advance_dispatch_status(
                "report-commissioning",
                DispatchStatus::Pending,
                DispatchStatus::Queued
            )
            .unwrap()
    );
    assert!(
        store
            .advance_dispatch_status(
                "report-commissioning",
                DispatchStatus::Queued,
                DispatchStatus::Acknowledged
            )
            .unwrap()
    );
    assert!(
        store
            .advance_dispatch_status(
                "report-commissioning",
                DispatchStatus::Acknowledged,
                DispatchStatus::Completed
            )
            .unwrap()
    );
    drop(store);
    let api = Api::open(&database).unwrap();
    let completed = api.handle_in_process("GET", "/api/v1/commissioning/report", None);
    assert_eq!(
        completed.status,
        200,
        "{}",
        String::from_utf8_lossy(&completed.body)
    );
    let completed: serde_json::Value = serde_json::from_slice(&completed.body).unwrap();
    let completed_dispatch = completed["checks"]
        .as_array()
        .unwrap()
        .iter()
        .find(|check| check["id"] == "guided_air_proof")
        .unwrap();
    assert_eq!(completed_dispatch["status"], "blocked");
    drop(api);
    let _ = std::fs::remove_file(database);
    let _ = std::fs::remove_file(media);
}

#[test]
fn commissioning_guided_air_proof_requires_one_persisted_causal_chain() {
    let database = temporary_database("commissioning-causal-chain");
    let historical_media = database.with_extension("historical.ts");
    let guided_media = database.with_extension("guided.ts");
    std::fs::write(&historical_media, b"historical media").unwrap();
    std::fs::write(&guided_media, b"guided managed media").unwrap();
    let channel_id = "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e";
    let guided_asset_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
    let now = system_time_millis_for_test();
    let api = Api::open(&database).unwrap();
    assert_eq!(
        api.handle_in_process(
            "PUT",
            "/api/v1/station",
            Some(br#"{"station_id":"3f5f721f-96c7-48b1-b061-1bf1ad1e62c2","display_name":"KTLT","timezone":"America/Denver"}"#),
        )
        .status,
        200
    );
    assert_eq!(
        api.handle_in_process(
            "PUT",
            "/api/v1/channels",
            Some(
                &serde_json::to_vec(&ChannelConfiguration {
                    channel_id: channel_id.into(),
                    display_name: "Primary".into(),
                    output_profile: Default::default(),
                    output_sinks: vec![OutputSinkConfiguration::required_udp(
                        "22222222-3333-4444-8555-666666666666",
                        "Primary",
                        "127.0.0.1:5500",
                        -16.0,
                    )],
                    enabled: true,
                })
                .unwrap(),
            ),
        )
        .status,
        200
    );
    drop(api);

    let store = StationStore::open(&database).unwrap();
    store
        .put_channel_runtime(&ChannelRuntimeObservation {
            channel_id: channel_id.into(),
            state: ChannelRuntimeState::HealthyFallback,
            on_air_asset_id: None,
            state_started_at_unix_ms: now,
            heartbeat_at_unix_ms: now,
            detail: None,
        })
        .unwrap();
    store
        .put_media_asset(&MediaAsset {
            asset_id: guided_asset_id.into(),
            media_path: historical_media.to_string_lossy().into_owned(),
            duration_ms: 30_000,
            readiness: AssetReadiness::Ready,
        })
        .unwrap();
    store
        .put_schedule_item(&ScheduleItem {
            item_id: "256d5a07-92d3-4718-aec9-05cad42fae7d".into(),
            revision: 1,
            channel_id: channel_id.into(),
            asset_id: guided_asset_id.into(),
            title: "Unrelated historical item".into(),
            starts_at_unix_ms: now + 60_000,
            duration_ms: 30_000,
            state: ScheduleState::Draft,
        })
        .unwrap();
    store
        .commit_schedule(
            "report-historical",
            "plan-historical",
            "256d5a07-92d3-4718-aec9-05cad42fae7d",
            "operator",
            now,
            "",
        )
        .unwrap();
    assert!(
        store
            .advance_dispatch_status(
                "report-historical",
                DispatchStatus::Pending,
                DispatchStatus::Queued,
            )
            .unwrap()
    );
    assert!(
        store
            .advance_dispatch_status(
                "report-historical",
                DispatchStatus::Queued,
                DispatchStatus::Acknowledged,
            )
            .unwrap()
    );
    assert!(
        store
            .advance_dispatch_status(
                "report-historical",
                DispatchStatus::Acknowledged,
                DispatchStatus::Completed,
            )
            .unwrap()
    );

    store
        .create_ingest_job(&MediaIngestJob {
            job_id: "388f654d-a5dd-4641-8f28-84048c4dbe09".into(),
            source_kind: IngestSourceKind::OperatorPath,
            source_path: guided_media.to_string_lossy().into_owned(),
            status: IngestJobStatus::Pending,
            progress_percent: 0,
            created_at_unix_ms: now + 1,
            started_at_unix_ms: None,
            completed_at_unix_ms: None,
            error_detail: None,
            asset_id: None,
            attempts: 0,
            next_attempt_at_unix_ms: None,
        })
        .unwrap();
    assert_eq!(
        store
            .claim_next_ingest_job(now + 2)
            .unwrap()
            .unwrap()
            .job_id,
        "388f654d-a5dd-4641-8f28-84048c4dbe09"
    );
    store
        .complete_ingest_job(
            "388f654d-a5dd-4641-8f28-84048c4dbe09",
            &MediaAsset {
                asset_id: guided_asset_id.into(),
                media_path: guided_media.to_string_lossy().into_owned(),
                duration_ms: 30_000,
                readiness: AssetReadiness::Ready,
            },
            &AssetLoudnessAnalysis {
                asset_id: guided_asset_id.into(),
                status: LoudnessGateStatus::NotChecked,
                measured_lufs_milli: None,
                target_lufs_milli: -24_000,
                detail: None,
            },
            now + 3,
        )
        .unwrap();
    drop(store);

    let read_status = |id: &str| {
        let api = Api::open(&database).unwrap();
        let response = api.handle_in_process("GET", "/api/v1/commissioning/report", None);
        assert_eq!(response.status, 200);
        let report: serde_json::Value = serde_json::from_slice(&response.body).unwrap();
        report["checks"]
            .as_array()
            .unwrap()
            .iter()
            .find(|check| check["id"] == id)
            .unwrap()["status"]
            .as_str()
            .unwrap()
            .to_string()
    };
    assert_eq!(read_status("guided_ingest"), "pass");
    assert_eq!(read_status("guided_schedule"), "blocked");
    assert_eq!(read_status("guided_air_proof"), "blocked");
    assert_eq!(read_status("first_broadcast_report"), "blocked");

    let store = StationStore::open(&database).unwrap();
    store
        .put_schedule_item(&ScheduleItem {
            item_id: "7e6e36ae-c6ea-4986-99d7-e848f4248815".into(),
            revision: 1,
            channel_id: channel_id.into(),
            asset_id: guided_asset_id.into(),
            title: "Guided first broadcast".into(),
            starts_at_unix_ms: now + 180_000,
            duration_ms: 30_000,
            state: ScheduleState::Draft,
        })
        .unwrap();
    store
        .commit_schedule(
            "report-guided",
            "plan-guided",
            "7e6e36ae-c6ea-4986-99d7-e848f4248815",
            "operator",
            now + 4,
            "",
        )
        .unwrap();
    drop(store);
    assert_eq!(read_status("guided_schedule"), "pass");
    assert_eq!(read_status("guided_air_proof"), "blocked");

    let store = StationStore::open(&database).unwrap();
    assert!(
        store
            .advance_dispatch_status(
                "report-guided",
                DispatchStatus::Pending,
                DispatchStatus::Queued,
            )
            .unwrap()
    );
    assert!(
        store
            .advance_dispatch_status(
                "report-guided",
                DispatchStatus::Queued,
                DispatchStatus::Acknowledged,
            )
            .unwrap()
    );
    drop(store);
    assert_eq!(read_status("guided_air_proof"), "pass");
    assert_eq!(read_status("first_broadcast_report"), "blocked");

    let store = StationStore::open(&database).unwrap();
    assert!(
        store
            .advance_dispatch_status(
                "report-guided",
                DispatchStatus::Acknowledged,
                DispatchStatus::Completed,
            )
            .unwrap()
    );
    drop(store);
    assert_eq!(read_status("first_broadcast_report"), "pass");
    assert_eq!(read_status("support_bundle"), "blocked");
    let api = Api::open(&database).unwrap();
    let response = api.handle_in_process("GET", "/api/v1/commissioning/report", None);
    let report: serde_json::Value = serde_json::from_slice(&response.body).unwrap();
    let air_proof = report["checks"]
        .as_array()
        .unwrap()
        .iter()
        .find(|check| check["id"] == "guided_air_proof")
        .unwrap();
    assert!(
        air_proof["evidence"]
            .as_str()
            .unwrap()
            .contains("report-guided")
    );
    assert!(
        air_proof["evidence"]
            .as_str()
            .unwrap()
            .contains(guided_asset_id)
    );
    drop(api);

    let api = Api::open(&database).unwrap();
    let created = api.handle_in_process("POST", "/api/v1/support-bundles", None);
    assert_eq!(
        created.status,
        201,
        "{}",
        String::from_utf8_lossy(&created.body)
    );
    let bundle: serde_json::Value = serde_json::from_slice(&created.body).unwrap();
    assert_eq!(bundle["commissioning_report_id"], "report-guided");
    let bundle_path = bundle["path"].as_str().unwrap().to_owned();
    drop(api);
    assert_eq!(read_status("support_bundle"), "pass");

    let mut corrupted = std::fs::read(&bundle_path).unwrap();
    corrupted[0] ^= 0xff;
    std::fs::write(&bundle_path, corrupted).unwrap();
    assert_eq!(read_status("support_bundle"), "blocked");
    std::fs::remove_file(&bundle_path).unwrap();

    // A later recommit advances the schedule binding. The completed report
    // for the prior revision must no longer satisfy guided-air evidence.
    let store = StationStore::open(&database).unwrap();
    let current = store
        .read_schedule_item("7e6e36ae-c6ea-4986-99d7-e848f4248815")
        .unwrap()
        .unwrap();
    let mut draft = current.clone();
    draft.revision += 1;
    draft.state = ScheduleState::Draft;
    draft.title = "Guided first broadcast revision two".into();
    store.put_schedule_item(&draft).unwrap();
    store
        .commit_schedule(
            "report-guided-recommit",
            "plan-guided-recommit",
            &draft.item_id,
            "operator",
            now + 5,
            "Recommitted after the first report.",
        )
        .unwrap();
    drop(store);
    assert_eq!(read_status("guided_air_proof"), "blocked");

    let _ = std::fs::remove_file(database);
    let _ = std::fs::remove_file(historical_media);
    let _ = std::fs::remove_file(guided_media);
}

fn system_time_millis_for_test() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64
}

#[test]
fn serves_the_embedded_operator_console_from_the_station_origin() {
    let database = temporary_database("operator-console");
    let api = Api::open(&database).expect("database opens");
    let shell = api.handle_in_process("GET", "/", None);
    assert_eq!(shell.status, 200);
    assert_eq!(shell.content_type, "text/html; charset=utf-8");
    let html = String::from_utf8(shell.body).unwrap();
    assert!(html.contains("TownLight Station"));
    assert!(html.contains("/operator.js"));
    let deep_link = api.handle_in_process("GET", "/?view=health", None);
    assert_eq!(deep_link.status, 200);
    assert_eq!(deep_link.content_type, "text/html; charset=utf-8");
    let script = api.handle_in_process("GET", "/operator.js", None);
    assert_eq!(script.status, 200);
    assert_eq!(script.content_type, "text/javascript; charset=utf-8");
    assert!(script.body.len() > 1_000);
    let styles = api.handle_in_process("GET", "/operator.css", None);
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
        output_profile: OutputProfileId::Atsc720p5994,
        output_sinks: vec![
            OutputSinkConfiguration::required_udp(
                "11111111-2222-4333-8444-555555555555",
                "Primary transport",
                "127.0.0.1:5500",
                -24.0,
            ),
            OutputSinkConfiguration::required_udp(
                "33333333-4444-4555-8666-777777777777",
                "Redundant transport",
                "127.0.0.1:5501",
                -24.0,
            ),
            OutputSinkConfiguration::required_srt(
                "44444444-5555-4666-8777-888888888888",
                "Remote SRT caller",
                "srt://127.0.0.1:5600",
                -24.0,
            ),
        ],
        enabled: true,
    };
    let put = api.handle_in_process(
        "PUT",
        "/api/v1/channels",
        Some(&serde_json::to_vec(&channel).unwrap()),
    );
    assert_eq!(put.status, 200);
    let listed = api.handle_in_process("GET", "/api/v1/channels", None);
    assert_eq!(listed.status, 200);
    assert_eq!(
        serde_json::from_slice::<Vec<ChannelConfiguration>>(&listed.body).unwrap(),
        vec![channel.clone()]
    );
    let profiles = api.handle_in_process("GET", "/api/v1/output-profiles", None);
    assert_eq!(profiles.status, 200);
    let profiles: serde_json::Value = serde_json::from_slice(&profiles.body).unwrap();
    assert_eq!(profiles.as_array().unwrap().len(), 2);
    assert_eq!(profiles[1]["profile_id"], "atsc-720p5994");
    assert_eq!(profiles[1]["width"], 1_280);
    assert_eq!(profiles[1]["frame_rate_numerator"], 60_000);
    assert_eq!(profiles[1]["frame_rate_denominator"], 1_001);
    let mut rerouted = channel.clone();
    rerouted.output_sinks[1].destination = "127.0.0.1:5502".into();
    let blocked_reroute = api.handle_in_process(
        "PUT",
        "/api/v1/channels",
        Some(&serde_json::to_vec(&rerouted).unwrap()),
    );
    assert_eq!(blocked_reroute.status, 409);
    let mut changed = channel.clone();
    changed.output_profile = OutputProfileId::Proof360p30;
    for sink in &mut changed.output_sinks {
        sink.loudness_target_lufs = -16.0;
    }
    let blocked = api.handle_in_process(
        "PUT",
        "/api/v1/channels",
        Some(&serde_json::to_vec(&changed).unwrap()),
    );
    assert_eq!(blocked.status, 409);
    let mut stopped = channel.clone();
    stopped.enabled = false;
    assert_eq!(
        api.handle_in_process(
            "PUT",
            "/api/v1/channels",
            Some(&serde_json::to_vec(&stopped).unwrap())
        )
        .status,
        200
    );
    changed.enabled = false;
    assert_eq!(
        api.handle_in_process(
            "PUT",
            "/api/v1/channels",
            Some(&serde_json::to_vec(&changed).unwrap())
        )
        .status,
        200
    );
    let invalid = api.handle_in_process(
        "PUT",
        "/api/v1/channels",
        Some(br#"{"channel_id":"not-a-uuid","display_name":"Bad","output_profile":"proof-360p30","output_sinks":[{"sink_id":"11111111-2222-4333-8444-555555555555","label":"Bad transport","kind":"udp-ts","destination":"nowhere","loudness_target_lufs":-16.0,"required":true,"enabled":true}],"enabled":true}"#),
    );
    assert_eq!(invalid.status, 422);
    let mut no_live_sink = changed;
    no_live_sink.enabled = true;
    for sink in &mut no_live_sink.output_sinks {
        sink.enabled = false;
    }
    assert_eq!(
        api.handle_in_process(
            "PUT",
            "/api/v1/channels",
            Some(&serde_json::to_vec(&no_live_sink).unwrap())
        )
        .status,
        422
    );
    let _ = std::fs::remove_file(database);
}

#[test]
fn stores_srt_passphrases_with_machine_protection_and_returns_only_presence() {
    let database = temporary_database("encrypted-srt-output");
    let passphrase = "srt-proof-passphrase-2026";
    let replacement = "replacement-srt-secret";
    let mut channel = ChannelConfiguration {
        channel_id: "93045fad-e4fa-4229-8f34-188dc9b14981".into(),
        display_name: "Encrypted contribution".into(),
        output_profile: OutputProfileId::Atsc720p5994,
        output_sinks: vec![OutputSinkConfiguration::required_srt(
            "be767862-a514-401f-9b27-01594a50664a",
            "Encrypted SRT caller",
            "srt://127.0.0.1:5602",
            -24.0,
        )],
        enabled: true,
    };
    channel.output_sinks[0].encryption_configured = true;
    let api = Api::open(&database).unwrap();
    let mut command = serde_json::to_value(&channel).unwrap();
    command["srt_passphrases"] = serde_json::json!({
        channel.output_sinks[0].sink_id.clone(): passphrase
    });
    let put = api.handle_in_process(
        "PUT",
        "/api/v1/channels",
        Some(&serde_json::to_vec(&command).unwrap()),
    );
    assert_eq!(put.status, 200, "{}", String::from_utf8_lossy(&put.body));
    let public = String::from_utf8(put.body).unwrap();
    assert!(!public.contains(passphrase));
    assert!(!public.contains("srt_passphrases"));
    assert!(public.contains("\"encryption_configured\":true"));
    let stored = StationStore::open(&database)
        .unwrap()
        .read_srt_output_secret(&channel.channel_id, &channel.output_sinks[0].sink_id)
        .unwrap()
        .unwrap();
    assert_eq!(stored.expose(), passphrase);

    command["srt_passphrases"] = serde_json::json!({
        channel.output_sinks[0].sink_id.clone(): replacement
    });
    assert_eq!(
        api.handle_in_process(
            "PUT",
            "/api/v1/channels",
            Some(&serde_json::to_vec(&command).unwrap())
        )
        .status,
        409,
        "a live channel must reject secret rotation"
    );
    channel.enabled = false;
    assert_eq!(
        api.handle_in_process(
            "PUT",
            "/api/v1/channels",
            Some(&serde_json::to_vec(&channel).unwrap())
        )
        .status,
        200,
        "the operator must first stop the channel while preserving its secret"
    );
    let mut legacy_client = serde_json::to_value(&channel).unwrap();
    legacy_client["output_sinks"][0]["encryption_configured"] = serde_json::json!(false);
    let preserved = api.handle_in_process(
        "PUT",
        "/api/v1/channels",
        Some(&serde_json::to_vec(&legacy_client).unwrap()),
    );
    assert_eq!(preserved.status, 200);
    assert!(
        String::from_utf8(preserved.body)
            .unwrap()
            .contains("\"encryption_configured\":true")
    );
    let mut stopped = serde_json::to_value(&channel).unwrap();
    stopped["srt_passphrases"] = serde_json::json!({
        channel.output_sinks[0].sink_id.clone(): replacement
    });
    assert_eq!(
        api.handle_in_process(
            "PUT",
            "/api/v1/channels",
            Some(&serde_json::to_vec(&stopped).unwrap())
        )
        .status,
        200
    );
    assert_eq!(
        StationStore::open(&database)
            .unwrap()
            .read_srt_output_secret(&channel.channel_id, &channel.output_sinks[0].sink_id)
            .unwrap()
            .unwrap()
            .expose(),
        replacement
    );
    drop(api);

    for candidate in [
        database.clone(),
        PathBuf::from(format!("{}-wal", database.display())),
        PathBuf::from(format!("{}-shm", database.display())),
    ] {
        if let Ok(bytes) = std::fs::read(&candidate) {
            for secret in [passphrase, replacement] {
                assert!(
                    !bytes
                        .windows(secret.len())
                        .any(|window| window == secret.as_bytes())
                );
            }
        }
        let _ = std::fs::remove_file(candidate);
    }
}

#[test]
fn clears_a_stopped_srt_passphrase_only_when_explicitly_requested() {
    let database = temporary_database("clear-encrypted-srt-output");
    let mut channel = ChannelConfiguration {
        channel_id: "8a198412-c8d3-4c69-a040-d2dc1d9cbcea".into(),
        display_name: "Encrypted contribution".into(),
        output_profile: OutputProfileId::Atsc720p5994,
        output_sinks: vec![OutputSinkConfiguration::required_srt(
            "50831fe4-806a-4d88-b29a-e4fe3ee71b7d",
            "Encrypted SRT caller",
            "srt://127.0.0.1:5603",
            -24.0,
        )],
        enabled: false,
    };
    channel.output_sinks[0].encryption_configured = true;
    let api = Api::open(&database).unwrap();
    let mut create = serde_json::to_value(&channel).unwrap();
    create["srt_passphrases"] = serde_json::json!({
        channel.output_sinks[0].sink_id.clone(): "clear-me-safely"
    });
    assert_eq!(
        api.handle_in_process(
            "PUT",
            "/api/v1/channels",
            Some(&serde_json::to_vec(&create).unwrap())
        )
        .status,
        200
    );

    let mut clear = serde_json::to_value(&channel).unwrap();
    clear["clear_srt_passphrases"] = serde_json::json!([channel.output_sinks[0].sink_id.clone()]);
    let response = api.handle_in_process(
        "PUT",
        "/api/v1/channels",
        Some(&serde_json::to_vec(&clear).unwrap()),
    );
    assert_eq!(
        response.status,
        200,
        "{}",
        String::from_utf8_lossy(&response.body)
    );
    assert!(
        String::from_utf8(response.body)
            .unwrap()
            .contains("\"encryption_configured\":false")
    );
    assert!(
        StationStore::open(&database)
            .unwrap()
            .read_srt_output_secret(&channel.channel_id, &channel.output_sinks[0].sink_id)
            .unwrap()
            .is_none()
    );

    drop(api);
    for candidate in [
        database.clone(),
        PathBuf::from(format!("{}-wal", database.display())),
        PathBuf::from(format!("{}-shm", database.display())),
    ] {
        let _ = std::fs::remove_file(candidate);
    }
}

#[test]
fn exposes_runtime_safe_to_air_and_acknowledges_durable_alerts() {
    let database = temporary_database("runtime-alerts");
    let channel = ChannelConfiguration {
        channel_id: "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e".into(),
        display_name: "Primary Cable Channel".into(),
        output_profile: Default::default(),
        output_sinks: vec![OutputSinkConfiguration::required_udp(
            "22222222-3333-4444-8555-666666666666",
            "Primary transport",
            "127.0.0.1:5500",
            -16.0,
        )],
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
    let runtime = api.handle_in_process("GET", "/api/v1/runtime-safe-to-air", None);
    assert_eq!(runtime.status, 200);
    let runtime: RuntimeSafeToAirStatus = serde_json::from_slice(&runtime.body).unwrap();
    assert_eq!(runtime.color, SafeToAirColor::Yellow);
    assert!(runtime.channels[0].on_healthy_fallback);
    assert_eq!(runtime.active_warning_alerts, 1);

    let listed = api.handle_in_process("GET", "/api/v1/alerts", None);
    let alerts: Vec<AlertEvent> = serde_json::from_slice(&listed.body).unwrap();
    assert_eq!(alerts.len(), 1);
    assert!(alerts[0].acknowledged_at_unix_ms.is_none());

    let acknowledged = api.handle_in_process(
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
    let configured = api.handle_in_process(
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
    let listed = restarted.handle_in_process("GET", "/api/v1/alert-channels", None);
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
        api.handle_in_process("PUT", "/api/v1/alert-channels", Some(&body))
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
    let response = api.handle_in_process(
        "PUT",
        "/api/v1/alert-channels",
        Some(
            br#"{"channel_id":"566c7e2a-661d-40fd-8078-7df64586c363","kind":"webhook","label":"Unsafe pager","enabled":true,"target":"http://alerts.example.org/callback","credential":"secret"}"#,
        ),
    );
    assert_eq!(response.status, 422);
    assert!(String::from_utf8(response.body).unwrap().contains("https"));
    let listed = api.handle_in_process("GET", "/api/v1/alert-channels", None);
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
    let response = api.handle_in_process("PUT", "/api/v1/alert-channels", Some(&body));
    assert_eq!(
        response.status,
        422,
        "{}",
        String::from_utf8_lossy(&response.body)
    );
    assert_eq!(
        api.handle_in_process("GET", "/api/v1/alert-channels", None)
            .body,
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
    let response = api.handle_in_process("PUT", "/api/v1/alert-channels", Some(&body));
    assert_eq!(
        response.status,
        422,
        "{}",
        String::from_utf8_lossy(&response.body)
    );
    assert_eq!(
        api.handle_in_process("GET", "/api/v1/alert-channels", None)
            .body,
        b"[]"
    );
    let _ = std::fs::remove_file(database);
}

#[test]
fn queues_a_visible_send_test_without_exposing_destination_secrets() {
    let database = temporary_database("alert-channel-send-test");
    let api = Api::open(&database).unwrap();
    let channel_id = "24fcb252-8f00-4e15-a4c9-7de8637beb3c";
    let configured = api.handle_in_process(
        "PUT",
        "/api/v1/alert-channels",
        Some(
            br#"{"channel_id":"24fcb252-8f00-4e15-a4c9-7de8637beb3c","kind":"webhook","label":"Test pager","enabled":false,"target":"http://127.0.0.1:47883/operator-alert","credential":"send-test-secret"}"#,
        ),
    );
    assert_eq!(configured.status, 200);

    let queued = api.handle_in_process(
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

    let listed = api.handle_in_process("GET", "/api/v1/alert-deliveries", None);
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
    let listed = api.handle_in_process("GET", "/api/v1/alert-rules", None);
    assert_eq!(listed.status, 200);
    let rules: Vec<AlertRule> = serde_json::from_slice(&listed.body).unwrap();
    let rule = rules
        .iter()
        .find(|rule| rule.condition == AlertConditionKind::OffAir)
        .unwrap();
    assert_eq!(rule.severity, AlertSeverity::Critical);
    assert_eq!(rule.re_alert_after_seconds, 3_600);

    let edited = api.handle_in_process(
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
    let persisted: Vec<AlertRule> = serde_json::from_slice(
        &reopened
            .handle_in_process("GET", "/api/v1/alert-rules", None)
            .body,
    )
    .unwrap();
    assert!(persisted.contains(&edited));
    let _ = std::fs::remove_file(database);
}

#[test]
fn exposes_the_latest_persisted_system_resource_sample() {
    let database = temporary_database("system-resources-api");
    let api = Api::open(&database).unwrap();
    assert_eq!(
        api.handle_in_process("GET", "/api/v1/system-resources/latest", None)
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
    let response = api.handle_in_process("GET", "/api/v1/system-resources/latest", None);
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
    let schedules: Vec<SelfTestSchedule> = serde_json::from_slice(
        &api.handle_in_process("GET", "/api/v1/self-test-schedules", None)
            .body,
    )
    .unwrap();
    assert_eq!(schedules.len(), 2);
    assert_eq!(schedules[0].kind, SelfTestKind::Daily);
    assert_eq!(schedules[1].kind, SelfTestKind::Weekly);

    let edited = api.handle_in_process(
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
        api.handle_in_process(
            "PUT",
            "/api/v1/self-test-schedules/daily",
            Some(
                br#"{"enabled":true,"local_hour":1,"local_minute":30,"weekday":2,"updated_by":"station-operator"}"#,
            ),
        )
        .status,
        422
    );

    let run = api.handle_in_process("POST", "/api/v1/self-tests/run?kind=daily", None);
    assert_eq!(run.status, 201, "{}", String::from_utf8_lossy(&run.body));
    let record: SystemSelfTest = serde_json::from_slice(&run.body).unwrap();
    assert_eq!(record.kind, SelfTestKind::Daily);
    assert_eq!(record.trigger, SelfTestTrigger::Manual);
    assert_eq!(record.schedule_period_key, None);
    let history: Vec<SystemSelfTest> = serde_json::from_slice(
        &api.handle_in_process("GET", "/api/v1/self-tests", None)
            .body,
    )
    .unwrap();
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

    let created = api.handle_in_process("POST", "/api/v1/support-bundles", None);
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

    let downloaded = api.handle_in_process("GET", &record.download_uri, None);
    assert_eq!(downloaded.status, 200);
    assert_eq!(downloaded.content_type, "application/zip");
    assert_eq!(
        downloaded.attachment_name.as_deref(),
        Some(record.file_name.as_str())
    );
    assert!(downloaded.body.starts_with(b"PK"));
    let archive = zip::ZipArchive::new(std::io::Cursor::new(&downloaded.body)).unwrap();
    assert!(archive.file_names().any(|name| name == "manifest.json"));

    let escaped =
        api.handle_in_process("GET", "/api/v1/support-bundles/..%2Fstation/download", None);
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
    assert!(headers.starts_with("HTTP/1.1 428 Precondition Required\r\n"));
    assert!(headers.contains("Content-Type: application/json\r\n"));
    assert!(!headers.contains("Content-Disposition:"));
    assert!(headers.contains("Cache-Control: no-store\r\n"));
    assert!(headers.contains("X-Content-Type-Options: nosniff\r\n"));

    drop(api);
    remove_directory(&root);
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

    let status = api.handle_in_process("GET", "/api/v1/updates/status", None);
    assert_eq!(status.status, 200);
    let document: serde_json::Value = serde_json::from_slice(&status.body).unwrap();
    assert_eq!(document["configured"], false);
    assert_eq!(document["current_version"], "0.1.0");
    assert_eq!(document["latest_check"], serde_json::Value::Null);
    assert_eq!(document["staged_package"], serde_json::Value::Null);
    assert_eq!(document["prepared_slot"], serde_json::Value::Null);
    assert!(
        document["next_step"]
            .as_str()
            .unwrap()
            .contains("trust file")
    );

    let check = api.handle_in_process("POST", "/api/v1/updates/check", None);
    assert_eq!(check.status, 409);
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&check.body).unwrap()["error"]["code"],
        "updates_not_configured"
    );
    let stage = api.handle_in_process("POST", "/api/v1/updates/stage", None);
    assert_eq!(stage.status, 409);
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&stage.body).unwrap()["error"]["code"],
        "updates_not_configured"
    );
    let prepare = api.handle_in_process("POST", "/api/v1/updates/prepare", None);
    assert_eq!(prepare.status, 409);
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&prepare.body).unwrap()["error"]["code"],
        "updates_not_configured"
    );
    let notes = api.handle_in_process("GET", "/api/v1/updates/release-notes?version=0.2.0", None);
    assert_eq!(notes.status, 404);

    drop(api);
    remove_directory(&root);
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
    let stored = api.handle_in_process(
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
    let listed = api.handle_in_process("GET", "/api/v1/watch-folders", None);
    assert_eq!(listed.status, 200);
    assert_eq!(
        serde_json::from_slice::<Vec<WatchFolderConfig>>(&listed.body).unwrap(),
        vec![config.clone()]
    );

    let mut forged = config;
    forged.last_error = Some("Everything is fine".into());
    assert_eq!(
        api.handle_in_process(
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
    let admitted = api.handle_in_process("POST", "/api/v1/assets/ingest", Some(&body));
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
    let fetched = api.handle_in_process(
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
    let queued_transcode = api.handle_in_process(
        "GET",
        &format!("/api/v1/assets/transcode/jobs/{transcode_job_id}"),
        None,
    );
    assert_eq!(queued_transcode.status, 200);
    let queued_transcode: serde_json::Value =
        serde_json::from_slice(&queued_transcode.body).unwrap();
    assert_eq!(queued_transcode["status"], "pending");
    let processing = api.handle_in_process("GET", "/api/v1/assets/readiness-dashboard", None);
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
    let completed = api.handle_in_process(
        "GET",
        "/api/v1/assets/ingest/jobs/7a49c96c-12da-4c3c-a144-739049eb19af",
        None,
    );
    let completed: serde_json::Value = serde_json::from_slice(&completed.body).unwrap();
    assert_eq!(completed["status"], "completed");
    assert_eq!(completed["progress_percent"], 100);
    assert_eq!(completed["asset_id"], asset_id);
    let fetched_asset = api.handle_in_process("GET", &format!("/api/v1/assets/{asset_id}"), None);
    let asset: MediaAsset = serde_json::from_slice(&fetched_asset.body).unwrap();
    assert_eq!(fetched_asset.status, 200);
    assert_eq!(asset.readiness, AssetReadiness::Ready);
    assert!(Path::new(&asset.media_path).starts_with(std::fs::canonicalize(&library).unwrap()));
    let readiness =
        api.handle_in_process("GET", &format!("/api/v1/assets/{asset_id}/readiness"), None);
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
        api.handle_in_process(
            "PUT",
            "/api/v1/assets",
            Some(&serde_json::to_vec(&forged).unwrap()),
        )
        .status,
        404
    );
    assert_eq!(
        api.handle_in_process(
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
        api.handle_in_process("POST", "/api/v1/assets/ingest", Some(&invalid_body))
            .status,
        202
    );
    assert!(matches!(
        IngestWorker::new(&database, &library).run_once().unwrap(),
        Some(IngestOutcome::Failed { .. })
    ));
    let failed = api.handle_in_process(
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
    let scheduled_media = root.join("scheduled-missing.ts");
    std::fs::write(&scheduled_media, b"temporarily admitted media").unwrap();
    let mut scheduled_missing = forged.clone();
    scheduled_missing.media_path = scheduled_media.to_string_lossy().into_owned();
    store.put_media_asset(&scheduled_missing).unwrap();
    store
        .put_schedule_item(&ScheduleItem {
            item_id: "185dc897-a14c-45f0-bd6a-899fd90cd196".into(),
            revision: 1,
            channel_id: "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e".into(),
            asset_id: forged.asset_id.clone(),
            title: "Missing council recording".into(),
            starts_at_unix_ms: now + 60_000,
            duration_ms: 30_000,
            state: ScheduleState::Draft,
        })
        .unwrap();
    store
        .commit_schedule(
            "missing-scheduled-report",
            "missing-scheduled-plan",
            "185dc897-a14c-45f0-bd6a-899fd90cd196",
            "test-harness",
            now,
            "missing scheduled fixture",
        )
        .unwrap();
    std::fs::remove_file(&scheduled_media).unwrap();
    drop(store);
    let dashboard = api.handle_in_process("GET", "/api/v1/assets/readiness-dashboard", None);
    assert_eq!(dashboard.status, 200);
    let dashboard: serde_json::Value = serde_json::from_slice(&dashboard.body).unwrap();
    assert_eq!(dashboard["ready_count"], 1);
    assert_eq!(dashboard["missing_count"], 1);
    assert_eq!(dashboard["failed_ingest_count"], 1);
    let missing = api.handle_in_process(
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
        api.handle_in_process(
            "GET",
            "/api/v1/assets/missing-scheduled?now_unix_ms=1",
            None,
        )
        .status,
        400
    );
    assert_eq!(
        api.handle_in_process(
            "GET",
            "/api/v1/assets/missing-scheduled?now_unix_ms=1&horizon_ms=2678400001",
            None,
        )
        .status,
        422
    );
    assert_eq!(
        api.handle_in_process("POST", "/api/v1/assets/ingest", Some(&body))
            .status,
        409
    );
    let invalid_job = serde_json::to_vec(&serde_json::json!({
        "job_id": "not-a-uuid",
        "source_path": source.to_string_lossy()
    }))
    .unwrap();
    assert_eq!(
        api.handle_in_process("POST", "/api/v1/assets/ingest", Some(&invalid_job))
            .status,
        422
    );
    let missing_source = serde_json::to_vec(&serde_json::json!({
        "job_id": "89a1cfac-81a6-4500-b712-6688fb71bda7",
        "source_path": root.join("missing.ts").to_string_lossy()
    }))
    .unwrap();
    assert_eq!(
        api.handle_in_process("POST", "/api/v1/assets/ingest", Some(&missing_source),)
            .status,
        422
    );
    assert_eq!(
        api.handle_in_process("GET", "/api/v1/assets/not-a-digest", None)
            .status,
        400
    );
    drop(api);
    let _ = std::fs::remove_file(database);
    remove_directory(&root);
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
fn schedule_item_mutation_requires_authenticated_authority_at_the_public_boundary() {
    let database = temporary_database("schedule-commit");
    let api = Api::open(&database).unwrap();
    let item = ScheduleItem {
        item_id: "256d5a07-92d3-4718-aec9-05cad42fae7d".into(),
        revision: 1,
        channel_id: "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e".into(),
        asset_id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into(),
        title: "City Council".into(),
        starts_at_unix_ms: 100_000,
        duration_ms: 60_000,
        state: ScheduleState::Draft,
    };
    let item_json = serde_json::to_vec(&item).unwrap();
    assert_eq!(
        api.handle_in_process("PUT", "/api/v1/schedule/items", Some(&item_json))
            .status,
        403
    );
    drop(api);
    let _ = std::fs::remove_file(database);
}

#[test]
fn production_http_boundary_requires_auth_for_station_profile_mutation() {
    let database = temporary_database("http");
    let body = br#"{"station_id":"3f5f721f-96c7-48b1-b061-1bf1ad1e62c2","display_name":"KTLT","timezone":"America/Denver"}"#;
    let request = format!(
        "PUT /api/v1/station HTTP/1.1\r\nHost: localhost\r\nOrigin: http://localhost\r\nSec-Fetch-Site: same-origin\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n",
        body.len()
    );
    let mut bytes = request.into_bytes();
    bytes.extend_from_slice(body);
    let response = String::from_utf8(http_request(&database, &bytes)).unwrap();
    assert!(response.starts_with("HTTP/1.1 428 Precondition Required\r\n"));
    assert!(response.contains("\r\nContent-Type: application/json\r\n"));
    let response_body = response.split("\r\n\r\n").nth(1).unwrap();
    let error: serde_json::Value = serde_json::from_str(response_body).unwrap();
    assert_eq!(error["error"]["code"], "operator_bootstrap_required");
    let _ = std::fs::remove_file(database);
}

#[test]
fn bootstrap_session_survives_across_real_tcp_connections() {
    let database = temporary_database("tcp-bootstrap-session");
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64;
    let capability = station_auth::generate_secret_token().unwrap();
    let bootstrap = station_auth::BootstrapCredential::new(
        station_auth::digest_secret(&capability),
        now.saturating_sub(1_000),
        now.saturating_add(5 * 60 * 1_000),
        None,
    )
    .unwrap();
    assert!(
        StationStore::open(&database)
            .unwrap()
            .provision_operator_bootstrap(&bootstrap)
            .unwrap()
    );

    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let stop = Arc::new(AtomicBool::new(false));
    let server_stop = Arc::clone(&stop);
    let server_database = database.clone();
    let (finished_tx, finished_rx) = mpsc::channel();
    thread::spawn(move || {
        let _ = finished_tx.send(serve_until(listener, server_database, server_stop));
    });

    let body = serde_json::to_vec(&serde_json::json!({
        "capability": capability.expose_secret(),
        "username": "setup.admin",
        "display_name": "Setup Administrator",
        "password": "A-strong-bootstrap-password-47!"
    }))
    .unwrap();
    let mut request = format!(
        "POST /api/v1/auth/bootstrap HTTP/1.1\r\nHost: {address}\r\nOrigin: http://{address}\r\nSec-Fetch-Site: same-origin\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    )
    .into_bytes();
    request.extend_from_slice(&body);
    let issued = String::from_utf8(http_request_to(address, &request)).unwrap();
    assert!(issued.starts_with("HTTP/1.1 201 Created\r\n"), "{issued}");
    let cookies = issued
        .split("\r\n\r\n")
        .next()
        .unwrap()
        .lines()
        .filter_map(|line| line.strip_prefix("Set-Cookie: "))
        .filter_map(|line| line.split(';').next())
        .collect::<Vec<_>>();
    let session = cookies
        .iter()
        .find(|cookie| cookie.starts_with("townlight_session_v1="))
        .unwrap();
    let csrf = cookies
        .iter()
        .find(|cookie| cookie.starts_with("townlight_csrf_v1="))
        .unwrap();
    let request = format!(
        "GET /api/v1/auth/session HTTP/1.1\r\nHost: {address}\r\nCookie: {session}; {csrf}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    );
    let confirmed = String::from_utf8(http_request_to(address, request.as_bytes())).unwrap();
    assert!(confirmed.starts_with("HTTP/1.1 200 OK\r\n"), "{confirmed}");

    stop.store(true, Ordering::Release);
    assert!(
        finished_rx
            .recv_timeout(Duration::from_secs(3))
            .unwrap()
            .is_ok()
    );
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
        "PUT /api/v1/station HTTP/1.1\r\nHost: {address}\r\nOrigin: http://{address}\r\nSec-Fetch-Site: same-origin\r\nContent-Type: application/json\r\nContent-Length: {}\r\nExpect: 100-continue\r\n\r\n",
        body.len(),
    );
    let mut client = TcpStream::connect(address).unwrap();
    client
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    client.write_all(headers.as_bytes()).unwrap();

    let mut interim = [0_u8; 25];
    client.read_exact(&mut interim).unwrap();
    assert_eq!(&interim, b"HTTP/1.1 100 Continue\r\n\r\n");

    client.write_all(body).unwrap();
    let mut response = Vec::new();
    client.read_to_end(&mut response).unwrap();
    assert!(response.starts_with(b"HTTP/1.1 428 Precondition Required\r\n"));
    server.join().unwrap().unwrap();
    let _ = std::fs::remove_file(database);
}

#[test]
fn rejects_a_stale_profile_revision_without_losing_the_winning_update() {
    let database = temporary_database("revision");
    let api = Api::open(&database).expect("database opens");
    let initial = br#"{"station_id":"3f5f721f-96c7-48b1-b061-1bf1ad1e62c2","display_name":"KTLT","timezone":"America/Denver","expected_revision":0}"#;
    assert_eq!(
        api.handle_in_process("PUT", "/api/v1/station", Some(initial))
            .status,
        200
    );
    let winning = br#"{"station_id":"3f5f721f-96c7-48b1-b061-1bf1ad1e62c2","display_name":"KTLT Updated","timezone":"America/Denver","expected_revision":1}"#;
    assert_eq!(
        api.handle_in_process("PUT", "/api/v1/station", Some(winning))
            .status,
        200
    );
    let stale = br#"{"station_id":"3f5f721f-96c7-48b1-b061-1bf1ad1e62c2","display_name":"KTLT Stale","timezone":"America/Denver","expected_revision":1}"#;
    let conflict = api.handle_in_process("PUT", "/api/v1/station", Some(stale));
    assert_eq!(conflict.status, 409);
    assert!(
        String::from_utf8(conflict.body)
            .unwrap()
            .contains("revision_conflict")
    );
    let stored: serde_json::Value =
        serde_json::from_slice(&api.handle_in_process("GET", "/api/v1/station", None).body)
            .unwrap();
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
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    client
        .write_all(format!("GET /health HTTP/1.1\r\nHost: {address}\r\n\r\n").as_bytes())
        .unwrap();
    let mut response = Vec::new();
    let _ = client.read_to_end(&mut response);
    stop.store(true, Ordering::Release);
    let server_result = finished_rx.recv_timeout(Duration::from_secs(3)).unwrap();

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
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    thread::sleep(Duration::from_millis(100));
    client
        .write_all(format!("GET /health HTTP/1.1\r\nHost: {address}\r\n\r\n").as_bytes())
        .unwrap();
    let mut response = Vec::new();
    client.read_to_end(&mut response).unwrap();
    assert!(response.starts_with(b"HTTP/1.1 200 OK\r\n"));

    stop.store(true, Ordering::Release);
    assert!(
        finished_rx
            .recv_timeout(Duration::from_secs(3))
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
        .write_all(format!("POST /api/v1/updates/stage HTTP/1.1\r\nHost: {address}\r\n").as_bytes())
        .unwrap();
    thread::sleep(Duration::from_millis(50));

    let mut health = TcpStream::connect(address).unwrap();
    health
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    health
        .write_all(format!("GET /health HTTP/1.1\r\nHost: {address}\r\n\r\n").as_bytes())
        .unwrap();
    let mut response = Vec::new();
    health.read_to_end(&mut response).unwrap();
    assert!(response.starts_with(b"HTTP/1.1 200 OK\r\n"));

    drop(stalled);
    stop.store(true, Ordering::Release);
    assert!(
        finished_rx
            .recv_timeout(Duration::from_secs(3))
            .unwrap()
            .is_ok()
    );
    let _ = std::fs::remove_file(database);
}
