use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::Path;
use std::time::Duration;

use serde::Deserialize;
use station_domain::StationProfile;
use station_storage::{ProfileWriteError, StationStore};

#[derive(Deserialize)]
struct PutStationProfile {
    #[serde(flatten)]
    profile: StationProfile,
    #[serde(default)]
    expected_revision: u64,
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
}

pub fn serve_one(listener: TcpListener, database_path: impl AsRef<Path>) -> Result<(), String> {
    let api = Api::open(database_path)?;
    let (mut stream, _) = listener.accept().map_err(|error| error.to_string())?;
    serve_stream(&mut stream, &api)
}

pub fn serve(listener: TcpListener, database_path: impl AsRef<Path>) -> Result<(), String> {
    let api = Api::open(database_path)?;
    for connection in listener.incoming() {
        let mut stream = connection.map_err(|error| error.to_string())?;
        serve_stream(&mut stream, &api)?;
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
        }
        let expected_length = header_end + 4 + content_length.unwrap_or(0);
        if expected_length > 65_536 {
            return Err(error_response(
                413,
                "request_too_large",
                "Requests may not exceed 64 KiB.",
            ));
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
