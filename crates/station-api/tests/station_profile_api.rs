use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use station_api::{Api, serve_one};
use station_domain::StationProfile;

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
