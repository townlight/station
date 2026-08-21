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
use station_domain::StationProfile;
use station_ingest::{IngestOutcome, IngestWorker};
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
    let asset_id = match outcome {
        IngestOutcome::Completed { asset_id, .. } => asset_id,
        other => panic!("real media did not complete: {other:?}"),
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
