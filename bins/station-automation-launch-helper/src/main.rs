//! Separately killable, deliberately fail-closed launch-helper boundary.
//!
//! `stationd` must only start this fixed installed helper in a fresh
//! kill-on-close Job Object. The legacy root-authority protocol has no
//! registered semantic adapter and remains deliberately unavailable.  The
//! separate `cloud_summary_v1` protocol accepts only inherited handles: it
//! never receives an artifact path and it never opens a request-named file.

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::io::{Read, Seek, SeekFrom, Write};
use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
use std::os::windows::io::{AsRawHandle, FromRawHandle, RawHandle};
use std::path::{Component, Path, PathBuf};
use std::sync::mpsc;
use std::time::{Duration, Instant};
use url::Url;
use windows_sys::Win32::Storage::FileSystem::{
    BY_HANDLE_FILE_INFORMATION, FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_OPEN_REPARSE_POINT,
    FILE_SHARE_READ, GetFileInformationByHandle,
};
use zeroize::Zeroize;

const MAX_REQUEST_BYTES: usize = 64 * 1024;
const MAX_ARTIFACT_BYTES: u64 = 128 * 1024 * 1024 * 1024;
const REQUEST_READ_DEADLINE: Duration = Duration::from_secs(5);
const MAX_ACTION_MS: u64 = 60_000;
const MAX_CLOUD_INPUT_BYTES: u64 = 8 * 1024 * 1024;
const MAX_CLOUD_OUTPUT_BYTES: usize = 1024 * 1024;
const MAX_CREDENTIAL_BYTES: usize = 16 * 1024;
const MAX_PROVIDER_REQUEST_BYTES: usize = MAX_CLOUD_INPUT_BYTES as usize + 32 * 1024;
const MAX_PROVIDER_RESPONSE_BYTES: usize = 2 * 1024 * 1024;
const MAX_PROVIDER_REQUEST_ID_BYTES: usize = 1024;
const CLOUD_SUMMARY_PATH: &str = "/v1/chat/completions";
const CLOUD_SUMMARY_INSTRUCTION: &str =
    "Summarize the supplied meeting transcript. Return plain text only.";

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct LaunchRequest {
    job_id: String,
    claim_id: String,
    adapter_id: String,
    adapter_revision: u64,
    candidate_version: String,
    candidate_commit: String,
    install_generation: u64,
    configuration_sha256: String,
    root_authority_sha256: String,
    input_relative_path: PathBuf,
    input_sha256: String,
    input_bytes: u64,
    output_relative_path: PathBuf,
    action_timeout_ms: u64,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ManagedRootAuthority {
    authority_id: String,
    revision: u64,
    input_root: PathBuf,
    output_root: PathBuf,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CloudSummaryRequest {
    mode: String,
    job_id: String,
    claim_id: String,
    adapter_id: String,
    adapter_revision: u64,
    candidate_version: String,
    candidate_commit: String,
    install_generation: u64,
    configuration_sha256: String,
    endpoint: String,
    model: String,
    input_sha256: String,
    input_bytes: u64,
    output_media_type: String,
    action_timeout_ms: u64,
    max_output_bytes: u64,
}

#[derive(Debug)]
struct CloudHttpRequest {
    endpoint: Url,
    authorization: String,
    body: Vec<u8>,
    deadline: Duration,
}

impl Drop for CloudHttpRequest {
    fn drop(&mut self) {
        self.authorization.zeroize();
    }
}

#[derive(Debug)]
struct CloudHttpResponse {
    status: u16,
    request_id: Option<String>,
    body: Vec<u8>,
}

#[derive(Debug, Clone, Copy)]
enum CloudFailure {
    Rejected,
    InputBindingFailed,
    CredentialRejected,
    ProviderDenied,
    ProviderUnavailable,
    ProviderResponseInvalid,
    OutputWriteFailed,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "snake_case")]
enum LaunchStatus {
    AdapterUnavailable,
    Rejected,
}

#[derive(Debug, Serialize)]
struct LaunchResponse {
    status: LaunchStatus,
    code: &'static str,
}

#[derive(Debug, Serialize)]
struct CloudSummaryResponse {
    status: &'static str,
    code: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    output_sha256: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    output_bytes: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    output_word_count: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    provider_request_id_sha256: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    provider_response_sha256: Option<String>,
}

fn main() {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    if args
        .first()
        .is_some_and(|argument| argument == "--cloud-summary-v1")
    {
        let response = run_cloud_summary_mode(&args);
        let _ = std::io::stdout()
            .write_all(&serde_json::to_vec(&response).expect("fixed cloud response serializes"));
        return;
    }
    let started = Instant::now();
    let response = match read_root_authority(&args)
        .and_then(|authority| read_request().map(|request| (authority, request)))
        .and_then(|(authority, request)| {
            validate_request(request).map(|request| (authority, request))
        })
        .and_then(|(authority, request)| validate_unavailable_request(authority, request, started))
    {
        Ok(()) => LaunchResponse {
            status: LaunchStatus::AdapterUnavailable,
            code: "adapter_unavailable",
        },
        Err(()) => LaunchResponse {
            status: LaunchStatus::Rejected,
            code: "launch_rejected",
        },
    };
    // The protocol has no diagnostic field. Never echo paths, prompts,
    // secrets, provider messages, or serialized request content.
    let _ = std::io::stdout()
        .write_all(&serde_json::to_vec(&response).expect("fixed response serializes"));
}

fn read_request() -> Result<LaunchRequest, ()> {
    // The framed stdin read is independently watchdog-bounded inside this
    // helper process. Root-authority, directory, hash, and sync I/O can still
    // block inside the helper; only a containing outer Job host can impose the
    // complete hard deadline by terminating and reaping this process tree.
    let (sender, receiver) = mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let result = (|| {
            let mut header = [0_u8; 4];
            std::io::stdin().read_exact(&mut header).map_err(|_| ())?;
            let length = usize::try_from(u32::from_le_bytes(header)).map_err(|_| ())?;
            if length == 0 || length > MAX_REQUEST_BYTES {
                return Err(());
            }
            let mut bytes = vec![0_u8; length];
            std::io::stdin().read_exact(&mut bytes).map_err(|_| ())?;
            serde_json::from_slice(&bytes).map_err(|_| ())
        })();
        let _ = sender.send(result);
    });
    receiver
        .recv_timeout(REQUEST_READ_DEADLINE)
        .map_err(|_| ())?
}

fn read_root_authority(args: &[std::ffi::OsString]) -> Result<(ManagedRootAuthority, String), ()> {
    let mut args = args.iter();
    if args.next().map(std::ffi::OsString::as_os_str)
        != Some(std::ffi::OsStr::new("--root-authority"))
    {
        return Err(());
    }
    let path = PathBuf::from(args.next().ok_or(())?);
    if args.next().map(std::ffi::OsString::as_os_str)
        != Some(std::ffi::OsStr::new("--root-authority-sha256"))
    {
        return Err(());
    }
    let expected = args.next().ok_or(())?.to_string_lossy().into_owned();
    if args.next().is_some() || !path.is_absolute() || !is_sha(&expected) {
        return Err(());
    }
    let file = std::fs::OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path)
        .map_err(|_| ())?;
    if is_reparse(&file.metadata().map_err(|_| ())?) {
        return Err(());
    }
    let mut bytes = Vec::new();
    file.take((MAX_REQUEST_BYTES + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|_| ())?;
    if bytes.is_empty()
        || bytes.len() > MAX_REQUEST_BYTES
        || format!("{:x}", Sha256::digest(&bytes)) != expected
    {
        return Err(());
    }
    let authority: ManagedRootAuthority = serde_json::from_slice(&bytes).map_err(|_| ())?;
    if !is_uuid(&authority.authority_id)
        || authority.revision == 0
        || !safe_managed_root(&authority.input_root)
        || !safe_managed_root(&authority.output_root)
    {
        return Err(());
    }
    Ok((authority, expected))
}

fn validate_request(request: LaunchRequest) -> Result<LaunchRequest, ()> {
    if !is_uuid(&request.job_id)
        || !is_uuid(&request.claim_id)
        || !is_uuid(&request.adapter_id)
        || request.adapter_revision == 0
        || request.install_generation == 0
        || !is_sha(&request.configuration_sha256)
        || !is_sha(&request.input_sha256)
        || !is_commit(&request.candidate_commit)
        || !is_bounded_version(&request.candidate_version)
        || !is_sha(&request.root_authority_sha256)
        || request.action_timeout_ms == 0
        || request.action_timeout_ms > MAX_ACTION_MS
        || request.input_bytes == 0
        || request.input_bytes > MAX_ARTIFACT_BYTES
        || !safe_relative(&request.input_relative_path)
        || !safe_relative(&request.output_relative_path)
    {
        return Err(());
    }
    Ok(request)
}

fn validate_unavailable_request(
    authority: (ManagedRootAuthority, String),
    request: LaunchRequest,
    started: Instant,
) -> Result<(), ()> {
    let (_authority, actual_authority_sha256) = authority;
    if request.root_authority_sha256 != actual_authority_sha256 {
        return Err(());
    }
    let deadline = started
        .checked_add(Duration::from_millis(request.action_timeout_ms))
        .ok_or(())?;
    if Instant::now() < deadline {
        Ok(())
    } else {
        Err(())
    }
}

/// The outer controller owns the kill-on-close Job Object.  This helper does
/// not spawn a process, and accepts only three explicitly inherited handles.
/// The controller must use PROC_THREAD_ATTRIBUTE_HANDLE_LIST so no other
/// inheritable handle can reach this process.
fn run_cloud_summary_mode(args: &[std::ffi::OsString]) -> CloudSummaryResponse {
    let result = (|| {
        let (input_handle, output_handle, credential_handle) = parse_cloud_handles(args)?;
        let mut input = unsafe { file_from_inherited_handle(input_handle)? };
        let mut output = unsafe { file_from_inherited_handle(output_handle)? };
        if same_file_identity(&input, &output)? {
            return Err(CloudFailure::Rejected);
        }
        let credential = unsafe { credential_from_inherited_handle(credential_handle)? };
        let request = read_cloud_request()?;
        execute_cloud_summary(
            request,
            &mut input,
            &mut output,
            credential,
            perform_cloud_http_request,
        )
    })();
    match result {
        Ok(response) => response,
        Err(failure) => cloud_failure_response(failure),
    }
}

fn parse_cloud_handles(args: &[std::ffi::OsString]) -> Result<(usize, usize, usize), CloudFailure> {
    let expected = [
        "--cloud-summary-v1",
        "--input-handle",
        "--output-handle",
        "--credential-handle",
    ];
    if args.len() != 7
        || args[0] != expected[0]
        || args[1] != expected[1]
        || args[3] != expected[2]
        || args[5] != expected[3]
    {
        return Err(CloudFailure::Rejected);
    }
    let input = parse_inherited_handle(&args[2])?;
    let output = parse_inherited_handle(&args[4])?;
    let credential = parse_inherited_handle(&args[6])?;
    if input == output || input == credential || output == credential {
        return Err(CloudFailure::Rejected);
    }
    Ok((input, output, credential))
}

fn parse_inherited_handle(value: &std::ffi::OsString) -> Result<usize, CloudFailure> {
    let value = value.to_str().ok_or(CloudFailure::Rejected)?;
    if value.is_empty() || value.len() > 20 || !value.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(CloudFailure::Rejected);
    }
    let handle = value.parse::<usize>().map_err(|_| CloudFailure::Rejected)?;
    if handle == 0 || handle == usize::MAX {
        return Err(CloudFailure::Rejected);
    }
    Ok(handle)
}

/// SAFETY: ownership of an explicitly inherited Windows HANDLE is transferred
/// to this helper exactly once after duplicate-value validation.
unsafe fn file_from_inherited_handle(handle: usize) -> Result<std::fs::File, CloudFailure> {
    let file = unsafe { std::fs::File::from_raw_handle(handle as RawHandle) };
    if !file.metadata().is_ok_and(|metadata| metadata.is_file()) {
        return Err(CloudFailure::Rejected);
    }
    Ok(file)
}

/// SAFETY: ownership of the dedicated inherited credential-pipe HANDLE is
/// transferred to this helper exactly once. Unlike artifact handles, the
/// credential source may be a Windows pipe and therefore need not be a file.
unsafe fn credential_from_inherited_handle(handle: usize) -> Result<std::fs::File, CloudFailure> {
    let file = unsafe { std::fs::File::from_raw_handle(handle as RawHandle) };
    if file.metadata().is_ok_and(|metadata| metadata.is_dir()) {
        return Err(CloudFailure::Rejected);
    }
    Ok(file)
}

fn same_file_identity(first: &std::fs::File, second: &std::fs::File) -> Result<bool, CloudFailure> {
    Ok(file_identity(first)? == file_identity(second)?)
}

fn file_identity(file: &std::fs::File) -> Result<(u32, u32, u32), CloudFailure> {
    // SAFETY: the File owns a valid Windows file HANDLE for this call; the
    // structure is zero-initialized exactly as required by the Windows API.
    let mut information: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
    if unsafe { GetFileInformationByHandle(file.as_raw_handle() as _, &mut information) } == 0 {
        return Err(CloudFailure::Rejected);
    }
    Ok((
        information.dwVolumeSerialNumber,
        information.nFileIndexHigh,
        information.nFileIndexLow,
    ))
}

fn read_cloud_request() -> Result<CloudSummaryRequest, CloudFailure> {
    read_framed_stdin(MAX_REQUEST_BYTES)
        .and_then(|bytes| serde_json::from_slice(&bytes).map_err(|_| CloudFailure::Rejected))
}

fn read_framed_stdin(maximum: usize) -> Result<Vec<u8>, CloudFailure> {
    let (sender, receiver) = mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let _ = sender.send(read_framed_bytes(std::io::stdin(), maximum));
    });
    receiver
        .recv_timeout(REQUEST_READ_DEADLINE)
        .map_err(|_| CloudFailure::Rejected)?
}

fn read_framed_bytes(mut reader: impl Read, maximum: usize) -> Result<Vec<u8>, CloudFailure> {
    let mut header = [0_u8; 4];
    reader
        .read_exact(&mut header)
        .map_err(|_| CloudFailure::Rejected)?;
    let length = usize::try_from(u32::from_le_bytes(header)).map_err(|_| CloudFailure::Rejected)?;
    if length == 0 || length > maximum {
        return Err(CloudFailure::Rejected);
    }
    let mut bytes = vec![0_u8; length];
    reader
        .read_exact(&mut bytes)
        .map_err(|_| CloudFailure::Rejected)?;
    Ok(bytes)
}

fn validate_cloud_request(
    request: CloudSummaryRequest,
) -> Result<(CloudSummaryRequest, Url), CloudFailure> {
    if request.mode != "cloud_summary_v1"
        || !is_uuid(&request.job_id)
        || !is_uuid(&request.claim_id)
        || !is_uuid(&request.adapter_id)
        || request.adapter_revision == 0
        || request.install_generation == 0
        || !is_sha(&request.configuration_sha256)
        || !is_commit(&request.candidate_commit)
        || !is_bounded_version(&request.candidate_version)
        || !is_sha(&request.input_sha256)
        || request.input_bytes == 0
        || request.input_bytes > MAX_CLOUD_INPUT_BYTES
        || request.output_media_type != "text/plain; charset=utf-8"
        || request.action_timeout_ms == 0
        || request.action_timeout_ms > MAX_ACTION_MS
        || request.max_output_bytes == 0
        || request.max_output_bytes > MAX_CLOUD_OUTPUT_BYTES as u64
        || !is_bounded_model(&request.model)
    {
        return Err(CloudFailure::Rejected);
    }
    let endpoint = Url::parse(&request.endpoint).map_err(|_| CloudFailure::Rejected)?;
    if endpoint.scheme() != "https"
        || endpoint.host_str() != Some("api.openai.com")
        || !endpoint.username().is_empty()
        || endpoint.password().is_some()
        || endpoint.port_or_known_default() != Some(443)
        || endpoint.path() != CLOUD_SUMMARY_PATH
        || endpoint.query().is_some()
        || endpoint.fragment().is_some()
    {
        return Err(CloudFailure::Rejected);
    }
    Ok((request, endpoint))
}

fn execute_cloud_summary<F>(
    request: CloudSummaryRequest,
    input: &mut std::fs::File,
    output: &mut std::fs::File,
    credential: std::fs::File,
    transport: F,
) -> Result<CloudSummaryResponse, CloudFailure>
where
    F: FnOnce(CloudHttpRequest) -> Result<CloudHttpResponse, CloudFailure>,
{
    let started = Instant::now();
    let (request, endpoint) = validate_cloud_request(request)?;
    let transcript = read_exact_transcript(input, &request)?;
    let mut credential = read_credential(credential)?;
    if started.elapsed() >= Duration::from_millis(request.action_timeout_ms) {
        credential.fill(0);
        return Err(CloudFailure::ProviderUnavailable);
    }
    let http_request = build_cloud_http_request(
        endpoint,
        &request.model,
        &transcript,
        &credential,
        request.action_timeout_ms,
    )?;
    credential.fill(0);
    let response = transport(http_request)?;
    if started.elapsed() >= Duration::from_millis(request.action_timeout_ms) {
        return Err(CloudFailure::ProviderUnavailable);
    }
    decode_cloud_response(response, output, request.max_output_bytes)
}

fn read_exact_transcript(
    input: &mut std::fs::File,
    request: &CloudSummaryRequest,
) -> Result<String, CloudFailure> {
    let metadata = input
        .metadata()
        .map_err(|_| CloudFailure::InputBindingFailed)?;
    if metadata.len() != request.input_bytes || metadata.len() > MAX_CLOUD_INPUT_BYTES {
        return Err(CloudFailure::InputBindingFailed);
    }
    input
        .seek(SeekFrom::Start(0))
        .map_err(|_| CloudFailure::InputBindingFailed)?;
    let mut bytes = Vec::with_capacity(request.input_bytes as usize);
    input
        .take(request.input_bytes.saturating_add(1))
        .read_to_end(&mut bytes)
        .map_err(|_| CloudFailure::InputBindingFailed)?;
    if bytes.len() as u64 != request.input_bytes
        || format!("{:x}", Sha256::digest(&bytes)) != request.input_sha256
    {
        return Err(CloudFailure::InputBindingFailed);
    }
    let transcript = String::from_utf8(bytes).map_err(|_| CloudFailure::InputBindingFailed)?;
    if transcript.is_empty()
        || transcript
            .chars()
            .any(|character| character.is_control() && !matches!(character, '\n' | '\r' | '\t'))
    {
        return Err(CloudFailure::InputBindingFailed);
    }
    Ok(transcript)
}

fn read_credential(credential: std::fs::File) -> Result<Vec<u8>, CloudFailure> {
    let (sender, receiver) = mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let _ = sender.send(read_framed_bytes(credential, MAX_CREDENTIAL_BYTES));
    });
    let bytes = receiver
        .recv_timeout(REQUEST_READ_DEADLINE)
        .map_err(|_| CloudFailure::CredentialRejected)?
        .map_err(|_| CloudFailure::CredentialRejected)?;
    if bytes.is_empty()
        || bytes
            .iter()
            .any(|byte| byte.is_ascii_control() || byte.is_ascii_whitespace())
        || std::str::from_utf8(&bytes).is_err()
    {
        return Err(CloudFailure::CredentialRejected);
    }
    Ok(bytes)
}

fn build_cloud_http_request(
    endpoint: Url,
    model: &str,
    transcript: &str,
    credential: &[u8],
    action_timeout_ms: u64,
) -> Result<CloudHttpRequest, CloudFailure> {
    let authorization = format!(
        "Bearer {}",
        std::str::from_utf8(credential).map_err(|_| CloudFailure::CredentialRejected)?
    );
    let body = serde_json::to_vec(&serde_json::json!({
        "model": model,
        "messages": [
            {"role": "developer", "content": CLOUD_SUMMARY_INSTRUCTION},
            {"role": "user", "content": transcript}
        ],
        "n": 1,
        "stream": false,
        "store": false
    }))
    .map_err(|_| CloudFailure::Rejected)?;
    if body.len() > MAX_PROVIDER_REQUEST_BYTES {
        return Err(CloudFailure::Rejected);
    }
    Ok(CloudHttpRequest {
        endpoint,
        authorization,
        body,
        deadline: Duration::from_millis(action_timeout_ms),
    })
}

fn perform_cloud_http_request(
    request: CloudHttpRequest,
) -> Result<CloudHttpResponse, CloudFailure> {
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .timeout_global(Some(request.deadline))
        .http_status_as_error(false)
        .max_redirects(0)
        .max_redirects_will_error(true)
        .build()
        .into();
    let mut response = agent
        .post(request.endpoint.as_str())
        .header("Authorization", &request.authorization)
        .header("Content-Type", "application/json")
        .header("Accept", "application/json")
        .send(request.body.as_slice())
        .map_err(|_| CloudFailure::ProviderUnavailable)?;
    let status = response.status().as_u16();
    let request_id = response
        .headers()
        .get("x-request-id")
        .and_then(|value| value.to_str().ok())
        .map(ToOwned::to_owned);
    let body = response
        .body_mut()
        .with_config()
        .limit((MAX_PROVIDER_RESPONSE_BYTES + 1) as u64)
        .read_to_vec()
        .map_err(|_| CloudFailure::ProviderUnavailable)?;
    Ok(CloudHttpResponse {
        status,
        request_id,
        body,
    })
}

fn decode_cloud_response(
    response: CloudHttpResponse,
    output: &mut std::fs::File,
    max_output_bytes: u64,
) -> Result<CloudSummaryResponse, CloudFailure> {
    if response.status == 401 || response.status == 403 {
        return Err(CloudFailure::ProviderDenied);
    }
    if !(200..300).contains(&response.status) {
        return Err(CloudFailure::ProviderUnavailable);
    }
    if response.body.is_empty() || response.body.len() > MAX_PROVIDER_RESPONSE_BYTES {
        return Err(CloudFailure::ProviderResponseInvalid);
    }
    let request_id = response
        .request_id
        .ok_or(CloudFailure::ProviderResponseInvalid)?;
    if request_id.is_empty()
        || request_id.len() > MAX_PROVIDER_REQUEST_ID_BYTES
        || request_id.chars().any(char::is_control)
    {
        return Err(CloudFailure::ProviderResponseInvalid);
    }
    let provider_response_sha256 = format!("{:x}", Sha256::digest(&response.body));
    let provider_request_id_sha256 = format!("{:x}", Sha256::digest(request_id.as_bytes()));
    let value: serde_json::Value = serde_json::from_slice(&response.body)
        .map_err(|_| CloudFailure::ProviderResponseInvalid)?;
    let choices = value
        .get("choices")
        .and_then(serde_json::Value::as_array)
        .ok_or(CloudFailure::ProviderResponseInvalid)?;
    if choices.len() != 1 {
        return Err(CloudFailure::ProviderResponseInvalid);
    }
    let content = choices[0]
        .get("message")
        .and_then(serde_json::Value::as_object)
        .and_then(|message| message.get("content"))
        .and_then(serde_json::Value::as_str)
        .ok_or(CloudFailure::ProviderResponseInvalid)?;
    let bytes = content.as_bytes();
    if bytes.is_empty()
        || bytes.len() > max_output_bytes as usize
        || bytes.len() > MAX_CLOUD_OUTPUT_BYTES
        || content
            .chars()
            .any(|character| character.is_control() && !matches!(character, '\n' | '\r' | '\t'))
    {
        return Err(CloudFailure::ProviderResponseInvalid);
    }
    let word_count = content.split_whitespace().count() as u64;
    if word_count == 0 {
        return Err(CloudFailure::ProviderResponseInvalid);
    }
    output
        .seek(SeekFrom::Start(0))
        .and_then(|_| output.set_len(0))
        .and_then(|_| output.write_all(bytes))
        .and_then(|_| output.sync_all())
        .map_err(|_| CloudFailure::OutputWriteFailed)?;
    let mut written = output
        .try_clone()
        .map_err(|_| CloudFailure::OutputWriteFailed)?;
    written
        .seek(SeekFrom::Start(0))
        .map_err(|_| CloudFailure::OutputWriteFailed)?;
    let mut actual = Vec::new();
    written
        .take(bytes.len().saturating_add(1) as u64)
        .read_to_end(&mut actual)
        .map_err(|_| CloudFailure::OutputWriteFailed)?;
    if actual != bytes {
        return Err(CloudFailure::OutputWriteFailed);
    }
    Ok(CloudSummaryResponse {
        status: "completed",
        code: "completed",
        output_sha256: Some(format!("{:x}", Sha256::digest(bytes))),
        output_bytes: Some(bytes.len() as u64),
        output_word_count: Some(word_count),
        provider_request_id_sha256: Some(provider_request_id_sha256),
        provider_response_sha256: Some(provider_response_sha256),
    })
}

fn cloud_failure_response(failure: CloudFailure) -> CloudSummaryResponse {
    let code = match failure {
        CloudFailure::Rejected => "launch_rejected",
        CloudFailure::InputBindingFailed => "input_binding_failed",
        CloudFailure::CredentialRejected => "credential_rejected",
        CloudFailure::ProviderDenied => "provider_denied",
        CloudFailure::ProviderUnavailable => "provider_unavailable",
        CloudFailure::ProviderResponseInvalid => "provider_response_invalid",
        CloudFailure::OutputWriteFailed => "output_write_failed",
    };
    CloudSummaryResponse {
        status: "failed",
        code,
        output_sha256: None,
        output_bytes: None,
        output_word_count: None,
        provider_request_id_sha256: None,
        provider_response_sha256: None,
    }
}
fn safe_relative(path: &Path) -> bool {
    if path.as_os_str().is_empty() || path.as_os_str().len() > 1024 {
        return false;
    }
    let components: Vec<_> = path.components().collect();
    if components.len() < 2 {
        return false;
    }
    components.into_iter().all(|part| {
        let Component::Normal(segment) = part else {
            return false;
        };
        segment.to_str().is_some_and(safe_windows_segment)
    })
}
fn safe_windows_segment(segment: &str) -> bool {
    if segment.is_empty()
        || segment.len() > 240
        || segment.ends_with(['.', ' '])
        || segment.chars().any(|character| {
            character.is_control()
                || matches!(
                    character,
                    '<' | '>' | ':' | '"' | '/' | '\\' | '|' | '?' | '*'
                )
        })
    {
        return false;
    }
    let stem = segment
        .split('.')
        .next()
        .unwrap_or(segment)
        .to_ascii_uppercase();
    !matches!(stem.as_str(), "CON" | "PRN" | "AUX" | "NUL")
        && !(stem.len() == 4
            && (stem.starts_with("COM") || stem.starts_with("LPT"))
            && matches!(stem.as_bytes()[3], b'1'..=b'9'))
}
fn safe_managed_root(path: &Path) -> bool {
    use std::path::Prefix;
    let mut components = path.components();
    let Some(Component::Prefix(prefix)) = components.next() else {
        return false;
    };
    if !matches!(prefix.kind(), Prefix::Disk(letter) if letter.is_ascii_alphabetic())
        || !matches!(components.next(), Some(Component::RootDir))
    {
        return false;
    }
    let mut normal_count = 0_usize;
    for component in components {
        let Component::Normal(segment) = component else {
            return false;
        };
        let Some(segment) = segment.to_str() else {
            return false;
        };
        if !safe_windows_segment(segment) {
            return false;
        }
        normal_count += 1;
    }
    normal_count >= 2 && path.as_os_str().len() <= 1024
}
fn is_reparse(metadata: &std::fs::Metadata) -> bool {
    metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
}
fn is_sha(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}
fn is_commit(value: &str) -> bool {
    value.len() == 40
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}
fn is_uuid(value: &str) -> bool {
    value.len() == 36
        && value.as_bytes()[14] == b'4'
        && matches!(value.as_bytes()[19], b'8' | b'9' | b'a' | b'b')
        && value.bytes().enumerate().all(|(i, byte)| {
            if matches!(i, 8 | 13 | 18 | 23) {
                byte == b'-'
            } else {
                byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)
            }
        })
}
fn is_bounded_version(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'-' | b'_'))
}
fn is_bounded_model(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'-' | b'_' | b':' | b'/')
        })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs::OpenOptions;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn cloud_request(input: &[u8]) -> CloudSummaryRequest {
        CloudSummaryRequest {
            mode: "cloud_summary_v1".into(),
            job_id: "33333333-2222-4333-8444-555555555555".into(),
            claim_id: "77777777-2222-4333-8444-555555555555".into(),
            adapter_id: "11111111-2222-4333-8444-555555555555".into(),
            adapter_revision: 1,
            candidate_version: "0.1.0".into(),
            candidate_commit: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into(),
            install_generation: 7,
            configuration_sha256: "b".repeat(64),
            endpoint: "https://api.openai.com/v1/chat/completions".into(),
            model: "example-summary-v1".into(),
            input_sha256: format!("{:x}", Sha256::digest(input)),
            input_bytes: input.len() as u64,
            output_media_type: "text/plain; charset=utf-8".into(),
            action_timeout_ms: 5_000,
            max_output_bytes: 1024,
        }
    }

    fn cloud_files(
        name: &str,
        input: &[u8],
        credential: &[u8],
    ) -> (PathBuf, std::fs::File, std::fs::File, std::fs::File) {
        let root = std::env::temp_dir().join(format!(
            "townlight-cloud-summary-helper-{name}-{}",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&root).unwrap();
        let input_path = root.join("input.txt");
        let output_path = root.join("output.txt");
        let credential_path = root.join("credential.bin");
        std::fs::write(&input_path, input).unwrap();
        let mut frame = Vec::new();
        frame.extend_from_slice(&(credential.len() as u32).to_le_bytes());
        frame.extend_from_slice(credential);
        std::fs::write(&credential_path, frame).unwrap();
        (
            root,
            OpenOptions::new().read(true).open(input_path).unwrap(),
            OpenOptions::new()
                .read(true)
                .write(true)
                .create_new(true)
                .open(output_path)
                .unwrap(),
            OpenOptions::new().read(true).open(credential_path).unwrap(),
        )
    }
    #[test]
    fn malformed_paths_and_identifiers_fail_closed() {
        assert!(!safe_relative(Path::new("..\\secret")));
        assert!(!safe_relative(Path::new("C:\\outside")));
        for hostile in [
            "NUL",
            "con.txt",
            "file:stream",
            "a*",
            "tail.",
            "tail ",
            "COM1",
        ] {
            assert!(!safe_relative(Path::new(hostile)), "accepted {hostile}");
        }
        assert!(!is_sha("SECRET"));
        assert!(!is_uuid("not-an-id"));
        assert!(!is_uuid("AAAAAAAA-2222-4333-8444-555555555555"));
        assert!(!is_uuid("aaaaaaaa-2222-3333-8444-555555555555"));
        assert!(!is_bounded_version("contains control\n"));
        assert!(!is_bounded_version(&"v".repeat(65)));
        for hostile_root in [
            r"\\server\share\managed",
            r"\\?\C:\managed\root",
            r"\\.\C:\managed\root",
            r"\??\C:\managed\root",
            r"C:\managed\NUL",
            r"C:\managed\bad:stream",
            r"C:\managed\tail. ",
            r"C:\managed\..\escape",
            r"C:\only-one",
        ] {
            assert!(
                !safe_managed_root(Path::new(hostile_root)),
                "accepted {hostile_root}"
            );
        }
        assert!(safe_managed_root(Path::new(
            r"C:\ProgramData\TownLight Station"
        )));
    }

    #[test]
    fn synthetic_cloud_protocol_fixture_writes_only_verified_summary_and_digests() {
        let transcript = b"The board approved the annual budget.";
        let (root, mut input, mut output, credential) =
            cloud_files("success", transcript, b"synthetic-test-token");
        let expected_request_id = format!("{:x}", Sha256::digest(b"fixture-request-01"));
        let expected_response_body =
            br#"{"choices":[{"message":{"content":"Budget was approved."}}]}"#;
        let expected_response_digest = format!("{:x}", Sha256::digest(expected_response_body));
        let response = execute_cloud_summary(
            cloud_request(transcript),
            &mut input,
            &mut output,
            credential,
            |request| {
                assert_eq!(
                    request.endpoint.as_str(),
                    "https://api.openai.com/v1/chat/completions"
                );
                assert_eq!(request.authorization, "Bearer synthetic-test-token");
                assert_eq!(request.deadline, Duration::from_secs(5));
                let body: serde_json::Value = serde_json::from_slice(&request.body).unwrap();
                assert_eq!(body["model"], "example-summary-v1");
                assert_eq!(body["stream"], false);
                assert_eq!(body["store"], false);
                assert_eq!(body["n"], 1);
                assert_eq!(body["messages"][0]["role"], "developer");
                assert_eq!(body["messages"][0]["content"], CLOUD_SUMMARY_INSTRUCTION);
                assert_eq!(body["messages"][1]["role"], "user");
                assert_eq!(
                    body["messages"][1]["content"],
                    "The board approved the annual budget."
                );
                Ok(CloudHttpResponse {
                    status: 200,
                    request_id: Some("fixture-request-01".into()),
                    body: expected_response_body.to_vec(),
                })
            },
        )
        .unwrap();
        assert_eq!(response.status, "completed");
        assert_eq!(response.code, "completed");
        assert_eq!(response.output_bytes, Some(20));
        assert_eq!(response.output_word_count, Some(3));
        assert_eq!(
            response.output_sha256,
            Some(format!("{:x}", Sha256::digest(b"Budget was approved.")))
        );
        assert_eq!(
            response.provider_request_id_sha256,
            Some(expected_request_id)
        );
        assert_eq!(
            response.provider_response_sha256,
            Some(expected_response_digest)
        );
        output.seek(SeekFrom::Start(0)).unwrap();
        let mut written = Vec::new();
        output.read_to_end(&mut written).unwrap();
        assert_eq!(written, b"Budget was approved.");
        let serialized = serde_json::to_string(&response).unwrap();
        assert!(!serialized.contains("synthetic-test-token"));
        assert!(!serialized.contains("Budget was approved."));
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn cloud_protocol_rejects_path_authority_unknown_fields_and_noncanonical_endpoints() {
        let request = serde_json::json!({
            "mode":"cloud_summary_v1",
            "job_id":"33333333-2222-4333-8444-555555555555",
            "claim_id":"77777777-2222-4333-8444-555555555555",
            "adapter_id":"11111111-2222-4333-8444-555555555555",
            "adapter_revision":1,
            "candidate_version":"0.1.0",
            "candidate_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "install_generation":7,
            "configuration_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "endpoint":"https://api.openai.com/v1/chat/completions",
            "model":"example-summary-v1",
            "input_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            "input_bytes":1,
            "output_media_type":"text/plain; charset=utf-8",
            "action_timeout_ms":1000,
            "max_output_bytes":1024,
            "input_path":"C:\\secret.txt"
        });
        assert!(serde_json::from_value::<CloudSummaryRequest>(request).is_err());
        for endpoint in [
            "http://api.openai.com/v1/chat/completions",
            "https://user@api.openai.com/v1/chat/completions",
            "https://api.openai.com:8443/v1/chat/completions",
            "https://api.openai.com/v1/responses",
            "https://api.openai.com/v1/chat/completions?redirect=1",
            "https://cloud.example.test/v1/chat/completions",
        ] {
            let mut request = cloud_request(b"a");
            request.endpoint = endpoint.into();
            assert!(
                validate_cloud_request(request).is_err(),
                "accepted {endpoint}"
            );
        }
        assert!(
            parse_cloud_handles(&[
                "--cloud-summary-v1".into(),
                "--input-handle".into(),
                "7".into(),
                "--output-handle".into(),
                "7".into(),
                "--credential-handle".into(),
                "9".into(),
            ])
            .is_err()
        );
    }

    #[test]
    fn cloud_protocol_failures_are_categorical_and_do_not_write_untrusted_output() {
        let transcript = b"Meeting transcript.";
        let (root, mut input, mut output, credential) =
            cloud_files("bad-provider", transcript, b"synthetic-test-token");
        let error = execute_cloud_summary(
            cloud_request(transcript),
            &mut input,
            &mut output,
            credential,
            |_| {
                Ok(CloudHttpResponse {
                    status: 200,
                    request_id: Some("fixture-request-02".into()),
                    body: br#"{"choices":[{"message":{"content":"secret\u0000payload"}}]}"#
                        .to_vec(),
                })
            },
        )
        .unwrap_err();
        assert!(matches!(error, CloudFailure::ProviderResponseInvalid));
        let response = cloud_failure_response(error);
        assert_eq!(
            serde_json::to_value(response).unwrap(),
            serde_json::json!({"status":"failed","code":"provider_response_invalid"})
        );
        output.seek(SeekFrom::Start(0)).unwrap();
        assert_eq!(output.metadata().unwrap().len(), 0);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn cloud_protocol_binds_input_before_transport_and_redacts_provider_denial() {
        let transcript = b"Meeting transcript.";
        let (root, mut input, mut output, credential) =
            cloud_files("binding", transcript, b"synthetic-test-token");
        let mut mismatched = cloud_request(transcript);
        mismatched.input_sha256 = "d".repeat(64);
        let error = execute_cloud_summary(mismatched, &mut input, &mut output, credential, |_| {
            panic!("transport must not receive an unbound input")
        })
        .unwrap_err();
        assert!(matches!(error, CloudFailure::InputBindingFailed));
        assert_eq!(output.metadata().unwrap().len(), 0);
        drop(input);
        drop(output);

        let (denied_root, mut input, mut output, credential) =
            cloud_files("denied", transcript, b"synthetic-test-token");
        let error = execute_cloud_summary(
            cloud_request(transcript),
            &mut input,
            &mut output,
            credential,
            |_| {
                Ok(CloudHttpResponse {
                    status: 401,
                    request_id: Some("do-not-serialize-me".into()),
                    body: b"provider-secret-error".to_vec(),
                })
            },
        )
        .unwrap_err();
        assert!(matches!(error, CloudFailure::ProviderDenied));
        assert_eq!(
            serde_json::to_string(&cloud_failure_response(error)).unwrap(),
            "{\"status\":\"failed\",\"code\":\"provider_denied\"}"
        );
        drop(input);
        drop(output);
        std::fs::remove_dir_all(root).unwrap();
        std::fs::remove_dir_all(denied_root).unwrap();
    }
}
