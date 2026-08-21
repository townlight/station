use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::Path;
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use std::time::Duration;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::Deserialize;
use station_domain::StationProfile;
use station_schedule::{MediaAsset, ScheduleItem};
use station_storage::{CommitWriteError, ProfileWriteError, StationStore};

#[derive(Deserialize)]
struct PutStationProfile {
    #[serde(flatten)]
    profile: StationProfile,
    #[serde(default)]
    expected_revision: u64,
}

#[derive(Deserialize)]
struct PrepareScheduleCommit {
    plan_id: String,
    schedule_item_id: String,
}

#[derive(Deserialize)]
struct CommitSchedule {
    report_id: String,
    plan_id: String,
    schedule_item_id: String,
    approved_by: String,
    #[serde(default)]
    operator_notes: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ApiResponse {
    pub status: u16,
    pub body: Vec<u8>,
}

pub struct Api {
    storage: StationStore,
}

impl Api {
    pub fn open(database_path: impl AsRef<Path>) -> Result<Self, String> {
        Ok(Self {
            storage: StationStore::open(database_path)?,
        })
    }

    pub fn handle(&self, method: &str, path: &str, body: Option<&[u8]>) -> ApiResponse {
        match (method, path) {
            ("GET", "/health") => {
                json_response(200, br#"{"database":"ready","status":"ready"}"#.to_vec())
            }
            ("GET", "/api/v1/station") => match self.storage.read_profile() {
                Ok(Some(profile)) => json_response(
                    200,
                    serde_json::to_vec(&profile).expect("serializing a profile cannot fail"),
                ),
                Ok(None) => error_response(
                    404,
                    "not_commissioned",
                    "The station profile has not been configured.",
                ),
                Err(message) => error_response(500, "storage_error", &message),
            },
            ("PUT", "/api/v1/station") => self.put_profile(body),
            ("PUT", "/api/v1/assets") => self.put_media_asset(body),
            ("PUT", "/api/v1/schedule/items") => self.put_schedule_item(body),
            ("POST", "/api/v1/schedule/prepare") => self.prepare_schedule_commit(body),
            ("POST", "/api/v1/schedule/commit") => self.commit_schedule(body),
            ("GET", path) if path.starts_with("/api/v1/schedule/items?") => {
                self.list_schedule(path)
            }
            ("GET", path) if path.starts_with("/api/v1/schedule/commits/") => {
                self.read_commit_report(path)
            }
            _ => error_response(404, "not_found", "The requested route does not exist."),
        }
    }

    fn put_profile(&self, body: Option<&[u8]>) -> ApiResponse {
        let Some(body) = body else {
            return error_response(400, "invalid_json", "A JSON request body is required.");
        };
        let command: PutStationProfile = match serde_json::from_slice(body) {
            Ok(command) => command,
            Err(_) => {
                return error_response(
                    400,
                    "invalid_json",
                    "The request body is not a station profile.",
                );
            }
        };
        if let Err(message) = command.profile.validate() {
            return error_response(422, "validation_failed", message);
        }
        match self
            .storage
            .write_profile(&command.profile, command.expected_revision)
        {
            Ok(document) => json_response(
                200,
                serde_json::to_vec(&document).expect("serializing a profile cannot fail"),
            ),
            Err(ProfileWriteError::Conflict(current_revision)) => error_response(
                409,
                "revision_conflict",
                &format!(
                    "The station profile changed; expected revision {}, current revision {}.",
                    command.expected_revision, current_revision
                ),
            ),
            Err(ProfileWriteError::Storage(message)) => {
                error_response(500, "storage_error", &message)
            }
        }
    }

    fn put_media_asset(&self, body: Option<&[u8]>) -> ApiResponse {
        let Some(asset) = parse_body::<MediaAsset>(body) else {
            return error_response(400, "invalid_json", "A media asset body is required.");
        };
        if let Err(error) = asset.validate() {
            return error_response(422, "validation_failed", &format!("{error:?}"));
        }
        match self.storage.put_media_asset(&asset) {
            Ok(()) => json_response(
                200,
                serde_json::to_vec(&asset).expect("serializing a media asset cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn put_schedule_item(&self, body: Option<&[u8]>) -> ApiResponse {
        let Some(item) = parse_body::<ScheduleItem>(body) else {
            return error_response(400, "invalid_json", "A schedule item body is required.");
        };
        if let Err(error) = item.validate() {
            return error_response(422, "validation_failed", &format!("{error:?}"));
        }
        match self.storage.put_schedule_item(&item) {
            Ok(()) => json_response(
                200,
                serde_json::to_vec(&item).expect("serializing a schedule item cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn prepare_schedule_commit(&self, body: Option<&[u8]>) -> ApiResponse {
        let Some(command) = parse_body::<PrepareScheduleCommit>(body) else {
            return error_response(
                400,
                "invalid_json",
                "A commit preparation body is required.",
            );
        };
        match self
            .storage
            .prepare_schedule_commit(&command.plan_id, &command.schedule_item_id)
        {
            Ok(plan) => json_response(
                200,
                serde_json::to_vec(&plan).expect("serializing a commit plan cannot fail"),
            ),
            Err(CommitWriteError::NotFound) => error_response(
                404,
                "schedule_item_not_found",
                "The schedule item does not exist.",
            ),
            Err(CommitWriteError::Invalid(message)) => {
                error_response(422, "validation_failed", message)
            }
            Err(CommitWriteError::GateFailed(_)) => unreachable!("prepare returns a plan"),
            Err(CommitWriteError::Storage(message)) => {
                error_response(500, "storage_error", &message)
            }
        }
    }

    fn commit_schedule(&self, body: Option<&[u8]>) -> ApiResponse {
        let Some(command) = parse_body::<CommitSchedule>(body) else {
            return error_response(400, "invalid_json", "A schedule commit body is required.");
        };
        let approved_at = match system_time_millis() {
            Ok(value) => value,
            Err(message) => return error_response(500, "clock_error", &message),
        };
        match self.storage.commit_schedule(
            &command.report_id,
            &command.plan_id,
            &command.schedule_item_id,
            &command.approved_by,
            approved_at,
            &command.operator_notes,
        ) {
            Ok(report) => json_response(
                201,
                serde_json::to_vec(&report).expect("serializing a commit report cannot fail"),
            ),
            Err(CommitWriteError::NotFound) => error_response(
                404,
                "schedule_item_not_found",
                "The schedule item does not exist.",
            ),
            Err(CommitWriteError::GateFailed(plan)) => {
                let status = if plan.missing_media_detail.is_some() {
                    422
                } else {
                    409
                };
                json_response(
                    status,
                    serde_json::to_vec(&plan).expect("serializing a failed gate cannot fail"),
                )
            }
            Err(CommitWriteError::Invalid(message)) => {
                error_response(422, "validation_failed", message)
            }
            Err(CommitWriteError::Storage(message)) => {
                error_response(500, "storage_error", &message)
            }
        }
    }

    fn list_schedule(&self, path: &str) -> ApiResponse {
        let Some(channel_id) = path.strip_prefix("/api/v1/schedule/items?channel_id=") else {
            return error_response(400, "invalid_query", "channel_id is required.");
        };
        if channel_id.is_empty() || channel_id.contains('&') {
            return error_response(400, "invalid_query", "channel_id is invalid.");
        }
        match self.storage.list_channel_schedule(channel_id) {
            Ok(items) => json_response(
                200,
                serde_json::to_vec(&items).expect("serializing schedule items cannot fail"),
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }

    fn read_commit_report(&self, path: &str) -> ApiResponse {
        let Some(report_id) = path.strip_prefix("/api/v1/schedule/commits/") else {
            unreachable!("the route prefix was checked")
        };
        if report_id.is_empty() || report_id.contains('/') {
            return error_response(400, "invalid_path", "The report identity is invalid.");
        }
        match self.storage.read_commit_report(report_id) {
            Ok(Some(report)) => json_response(
                200,
                serde_json::to_vec(&report).expect("serializing a commit report cannot fail"),
            ),
            Ok(None) => error_response(
                404,
                "commit_report_not_found",
                "The commit report does not exist.",
            ),
            Err(message) => error_response(500, "storage_error", &message),
        }
    }
}

fn parse_body<T: serde::de::DeserializeOwned>(body: Option<&[u8]>) -> Option<T> {
    body.and_then(|body| serde_json::from_slice(body).ok())
}

fn system_time_millis() -> Result<i64, String> {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| error.to_string())?
        .as_millis();
    i64::try_from(millis).map_err(|_| "system time exceeds the supported range".into())
}

pub fn serve_one(listener: TcpListener, database_path: impl AsRef<Path>) -> Result<(), String> {
    let api = Api::open(database_path)?;
    let (mut stream, _) = listener.accept().map_err(|error| error.to_string())?;
    serve_stream(&mut stream, &api)
}

pub fn serve(listener: TcpListener, database_path: impl AsRef<Path>) -> Result<(), String> {
    serve_until(listener, database_path, Arc::new(AtomicBool::new(false)))
}

pub fn serve_until(
    listener: TcpListener,
    database_path: impl AsRef<Path>,
    stop: Arc<AtomicBool>,
) -> Result<(), String> {
    let api = Api::open(database_path)?;
    listener
        .set_nonblocking(true)
        .map_err(|error| error.to_string())?;
    while !stop.load(Ordering::Acquire) {
        match listener.accept() {
            Ok((mut stream, _)) => {
                stream
                    .set_nonblocking(false)
                    .map_err(|error| error.to_string())?;
                serve_stream(&mut stream, &api)?;
            }
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                std::thread::sleep(Duration::from_millis(10));
            }
            Err(error) => return Err(error.to_string()),
        }
    }
    Ok(())
}

fn serve_stream(stream: &mut TcpStream, api: &Api) -> Result<(), String> {
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .map_err(|error| error.to_string())?;
    let response = match read_http_request(stream) {
        Ok(request) => dispatch_http(api, &request),
        Err(response) => response,
    };
    let reason = match response.status {
        200 => "OK",
        201 => "Created",
        400 => "Bad Request",
        404 => "Not Found",
        409 => "Conflict",
        413 => "Content Too Large",
        422 => "Unprocessable Content",
        _ => "Internal Server Error",
    };
    let headers = format!(
        "HTTP/1.1 {} {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        response.status,
        reason,
        response.body.len()
    );
    stream
        .write_all(headers.as_bytes())
        .map_err(|error| error.to_string())?;
    stream
        .write_all(&response.body)
        .map_err(|error| error.to_string())
}

fn read_http_request(stream: &mut TcpStream) -> Result<Vec<u8>, ApiResponse> {
    let mut request = Vec::new();
    let mut chunk = [0_u8; 4096];
    let mut continue_sent = false;
    loop {
        let count = match stream.read(&mut chunk) {
            Ok(0) => {
                return Err(error_response(
                    400,
                    "malformed_request",
                    "The HTTP request ended before it was complete.",
                ));
            }
            Ok(count) => count,
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock
                ) =>
            {
                return Err(error_response(
                    400,
                    "request_timeout",
                    "The HTTP request was not completed within five seconds.",
                ));
            }
            Err(error) => return Err(error_response(400, "read_failed", &error.to_string())),
        };
        request.extend_from_slice(&chunk[..count]);
        if request.len() > 65_536 {
            return Err(error_response(
                413,
                "request_too_large",
                "Requests may not exceed 64 KiB.",
            ));
        }

        let Some(header_end) = request.windows(4).position(|window| window == b"\r\n\r\n") else {
            continue;
        };
        let headers = std::str::from_utf8(&request[..header_end])
            .map_err(|_| error_response(400, "malformed_request", "HTTP headers must be UTF-8."))?;
        let mut content_length = None;
        let mut expects_continue = false;
        for line in headers.split("\r\n").skip(1) {
            let Some((name, value)) = line.split_once(':') else {
                return Err(error_response(
                    400,
                    "malformed_request",
                    "An HTTP header is invalid.",
                ));
            };
            if name.eq_ignore_ascii_case("content-length") {
                if content_length.is_some() {
                    return Err(error_response(
                        400,
                        "malformed_request",
                        "Content-Length may appear only once.",
                    ));
                }
                content_length = Some(value.trim().parse::<usize>().map_err(|_| {
                    error_response(400, "malformed_request", "Content-Length is invalid.")
                })?);
            }
            if name.eq_ignore_ascii_case("expect") {
                if value.trim().eq_ignore_ascii_case("100-continue") {
                    expects_continue = true;
                } else {
                    return Err(error_response(
                        400,
                        "unsupported_expectation",
                        "Only Expect: 100-continue is supported.",
                    ));
                }
            }
        }
        let expected_length = header_end + 4 + content_length.unwrap_or(0);
        if expected_length > 65_536 {
            return Err(error_response(
                413,
                "request_too_large",
                "Requests may not exceed 64 KiB.",
            ));
        }
        if expects_continue && !continue_sent && request.len() < expected_length {
            stream
                .write_all(b"HTTP/1.1 100 Continue\r\n\r\n")
                .map_err(|error| error_response(400, "write_failed", &error.to_string()))?;
            stream
                .flush()
                .map_err(|error| error_response(400, "write_failed", &error.to_string()))?;
            continue_sent = true;
        }
        if request.len() >= expected_length {
            request.truncate(expected_length);
            return Ok(request);
        }
    }
}

fn dispatch_http(api: &Api, request: &[u8]) -> ApiResponse {
    let Some(header_end) = request.windows(4).position(|window| window == b"\r\n\r\n") else {
        return error_response(400, "malformed_request", "The HTTP headers are incomplete.");
    };
    let headers = match std::str::from_utf8(&request[..header_end]) {
        Ok(headers) => headers,
        Err(_) => return error_response(400, "malformed_request", "HTTP headers must be UTF-8."),
    };
    let mut lines = headers.split("\r\n");
    let Some(request_line) = lines.next() else {
        return error_response(400, "malformed_request", "The request line is missing.");
    };
    let mut request_parts = request_line.split_whitespace();
    let (Some(method), Some(path), Some(version), None) = (
        request_parts.next(),
        request_parts.next(),
        request_parts.next(),
        request_parts.next(),
    ) else {
        return error_response(400, "malformed_request", "The request line is invalid.");
    };
    if version != "HTTP/1.1" {
        return error_response(400, "malformed_request", "Only HTTP/1.1 is supported.");
    }

    let mut content_length = 0usize;
    for line in lines {
        let Some((name, value)) = line.split_once(':') else {
            return error_response(400, "malformed_request", "An HTTP header is invalid.");
        };
        if name.eq_ignore_ascii_case("content-length") {
            content_length = match value.trim().parse() {
                Ok(length) => length,
                Err(_) => {
                    return error_response(400, "malformed_request", "Content-Length is invalid.");
                }
            };
        }
    }
    let body = &request[header_end + 4..];
    if body.len() != content_length {
        return error_response(
            400,
            "malformed_request",
            "Content-Length does not match the request body.",
        );
    }
    api.handle(method, path, (!body.is_empty()).then_some(body))
}

fn json_response(status: u16, body: Vec<u8>) -> ApiResponse {
    ApiResponse { status, body }
}

fn error_response(status: u16, code: &str, message: &str) -> ApiResponse {
    json_response(
        status,
        serde_json::to_vec(&serde_json::json!({ "error": { "code": code, "message": message } }))
            .expect("serializing an error response cannot fail"),
    )
}
