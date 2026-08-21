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

use station_api::{Api, serve_one, serve_until};
use station_domain::StationProfile;
use station_schedule::{
    AssetReadiness, ChannelConfiguration, CommitPlan, CommitReport, DispatchStatus, MediaAsset,
    ScheduleItem, ScheduleState,
};

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
fn prepares_commits_and_persists_an_operator_approval_through_the_api() {
    let database = temporary_database("schedule-commit");
    let api = Api::open(&database).unwrap();
    let asset = MediaAsset {
        asset_id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into(),
        media_path: r"C:\ProgramData\TownLight Station\media\asset.ts".into(),
        duration_ms: 60_000,
        readiness: AssetReadiness::Ready,
    };
    assert_eq!(
        api.handle(
            "PUT",
            "/api/v1/assets",
            Some(&serde_json::to_vec(&asset).unwrap())
        )
        .status,
        200
    );
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
