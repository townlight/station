//! Private, bounded YouTube Live execution boundary.
//!
//! Provider credentials, OAuth access tokens, provider-issued stream keys and
//! process output deliberately never cross this module's return boundary.

use super::{ProviderStageEvidence, PublishExecutionJob};
use sha2::{Digest, Sha256};
use station_publishing::{PublishExecutionStage, YouTubePrivacyStatus};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom};
use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
use std::os::windows::io::AsRawHandle;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc;
use std::thread::JoinHandle;
use std::time::{Duration, Instant};
use windows_sys::Win32::Foundation::{CloseHandle, HANDLE, INVALID_HANDLE_VALUE};
use windows_sys::Win32::Storage::FileSystem::{FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_READ};
use windows_sys::Win32::System::IO::CancelSynchronousIo;
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JobObjectExtendedLimitInformation,
    SetInformationJobObject, TerminateJobObject,
};

const MAX_HTTP_BODY_BYTES: usize = 1_048_576;
const MAX_SECRET_BYTES: usize = 4_096;
const STREAM_ACTIVE_TIMEOUT: Duration = Duration::from_secs(60);
const BROADCAST_LIVE_TIMEOUT: Duration = Duration::from_secs(60);
const POLL_INTERVAL: Duration = Duration::from_millis(500);
const PROCESS_DRAIN_TIMEOUT: Duration = Duration::from_secs(5);
const PROCESS_OUTPUT_BYTES: usize = 64 * 1024;

#[derive(Debug, Clone, PartialEq, Eq)]
enum Endpoint {
    OAuthToken,
    Broadcast { id: String },
    Stream { id: String },
    Transition { id: String },
}

struct HttpRequest {
    endpoint: Endpoint,
    authorization: Option<String>,
    form: Vec<(String, String)>,
}

#[derive(Debug, Clone)]
struct HttpResponse {
    status: u16,
    location: Option<String>,
    body: Vec<u8>,
}

impl Drop for HttpResponse {
    fn drop(&mut self) {
        self.body.fill(0);
    }
}

trait RunningFfmpeg {
    fn try_wait(&mut self) -> Result<Option<bool>, String>;
    fn terminate_and_join(
        &mut self,
        heartbeat: &mut dyn FnMut() -> Result<(), String>,
    ) -> Result<(), String>;
}

trait RuntimeIo {
    fn request(&mut self, request: &mut HttpRequest) -> Result<HttpResponse, String>;
    fn start_ffmpeg(
        &mut self,
        executable: &Path,
        argv: &[String],
    ) -> Result<Box<dyn RunningFfmpeg>, String>;
    fn now(&mut self) -> Result<i64, String>;
    fn sleep(&mut self, duration: Duration);
}

#[derive(Debug, Clone)]
struct VerifiedFfmpeg {
    executable: PathBuf,
    sha256: String,
}

#[allow(clippy::too_many_arguments)]
pub(super) fn execute_system(
    job: &PublishExecutionJob,
    channel_id: &str,
    broadcast_id: &str,
    stream_id: &str,
    privacy_status: YouTubePrivacyStatus,
    media_path: &Path,
    credential: &[u8],
    application_root: &Path,
    data_root: &Path,
    heartbeat: &mut dyn FnMut() -> Result<(), String>,
) -> Result<ProviderStageEvidence, String> {
    let ffmpeg = verify_installed_ffmpeg(application_root, data_root)?;
    let mut runtime = SystemRuntimeIo::new();
    execute_with(
        &mut runtime,
        job,
        channel_id,
        broadcast_id,
        stream_id,
        privacy_status,
        media_path,
        credential,
        &ffmpeg,
        heartbeat,
    )
}

#[allow(clippy::too_many_arguments)]
fn execute_with<R: RuntimeIo>(
    runtime: &mut R,
    job: &PublishExecutionJob,
    channel_id: &str,
    broadcast_id: &str,
    stream_id: &str,
    privacy_status: YouTubePrivacyStatus,
    media_path: &Path,
    credential: &[u8],
    ffmpeg: &VerifiedFfmpeg,
    heartbeat: &mut dyn FnMut() -> Result<(), String>,
) -> Result<ProviderStageEvidence, String> {
    heartbeat()?;
    // Keep this no-delete handle alive until after the child has been spawned
    // and reaped. The process path cannot be substituted between its verified
    // identity check and this controller's bounded execution.
    let _ffmpeg_pin = verify_ffmpeg_identity(ffmpeg)?;
    let mut oauth = OAuthCredential::parse(credential)?;
    let mut access_token = refresh_access_token(runtime, &oauth)?;
    oauth.zeroize();
    let result = {
        let mut authorization = format!("Bearer {access_token}");
        let provider_result = (|| {
            let mut broadcast = get_broadcast(runtime, broadcast_id, &authorization)?;
            require_broadcast_binding(
                &broadcast,
                channel_id,
                broadcast_id,
                stream_id,
                privacy_status,
                false,
            )?;
            broadcast.zeroize();
            let mut stream = get_stream(runtime, stream_id, &authorization)?;
            let mut ingestion = require_stream_binding(&stream, channel_id, stream_id, false)?;
            let mut argv = ffmpeg_argv(media_path, &ingestion)?;
            ingestion.zeroize();
            stream.zeroize();
            let process = runtime.start_ffmpeg(&ffmpeg.executable, &argv)?;
            // The child owns a Windows command-line copy. Wipe our only copy that
            // contains the provider-issued stream name before polling or returning.
            for argument in &mut argv {
                zero_string(argument);
            }
            let mut process = Some(process);
            let outcome = (|| {
                poll_stream_active(
                    runtime,
                    process.as_deref_mut().expect("ffmpeg process retained"),
                    channel_id,
                    stream_id,
                    &authorization,
                    heartbeat,
                )?;
                let mut transitioned = transition_broadcast(runtime, broadcast_id, &authorization)?;
                require_broadcast_binding(
                    &transitioned,
                    channel_id,
                    broadcast_id,
                    stream_id,
                    privacy_status,
                    false,
                )?;
                transitioned.zeroize();
                let mut observed = poll_broadcast_live(
                    runtime,
                    process.as_deref_mut().expect("ffmpeg process retained"),
                    channel_id,
                    broadcast_id,
                    stream_id,
                    privacy_status,
                    &authorization,
                    heartbeat,
                )?;
                observed.zeroize();
                // Never digest or retain raw provider JSON: stream documents carry
                // the provider-issued stream key. This contains only exact safe
                // bindings and the media identity already held by the durable job.
                let evidence = format!(
                    "youtube-live-v1|{}|{}|{}|{}|{}",
                    channel_id,
                    broadcast_id,
                    stream_id,
                    match privacy_status {
                        YouTubePrivacyStatus::Public => "public",
                        YouTubePrivacyStatus::Unlisted => "unlisted",
                    },
                    job.media_sha256,
                );
                Ok::<_, String>(format!("{:x}", Sha256::digest(evidence.as_bytes())))
            })();
            let cleanup = process
                .take()
                .expect("ffmpeg process retained")
                .terminate_and_join(heartbeat);
            match (outcome, cleanup) {
                (Ok(digest), Ok(())) => Ok(digest),
                (Err(primary), Ok(())) => Err(primary),
                (Ok(_), Err(cleanup)) => Err(format!("YouTube ffmpeg teardown failed: {cleanup}")),
                (Err(primary), Err(cleanup)) => Err(format!(
                    "{primary}; YouTube ffmpeg teardown failed: {cleanup}"
                )),
            }
        })();
        zero_string(&mut authorization);
        provider_result
    };
    zero_string(&mut access_token);
    result.map(|digest| ProviderStageEvidence {
        stage: PublishExecutionStage::Published,
        digest,
        detail: "YouTube observed the exact configured broadcast live with the required privacy status after the exact stream became active.".into(),
    })
}

#[derive(Debug)]
struct OAuthCredential {
    client_id: String,
    client_secret: String,
    refresh_token: String,
}

impl OAuthCredential {
    fn parse(bytes: &[u8]) -> Result<Self, String> {
        #[derive(serde::Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Wire {
            client_id: String,
            client_secret: String,
            refresh_token: String,
        }
        let mut wire: Wire = serde_json::from_slice(bytes)
            .map_err(|_| "YouTube credential authority is malformed")?;
        let valid = [&wire.client_id, &wire.client_secret, &wire.refresh_token]
            .iter()
            .all(|secret| {
                !(secret.is_empty()
                    || secret.len() > MAX_SECRET_BYTES
                    || secret.chars().any(char::is_control))
            });
        if !valid {
            zero_string(&mut wire.client_id);
            zero_string(&mut wire.client_secret);
            zero_string(&mut wire.refresh_token);
            return Err("YouTube credential authority is invalid or unbounded".into());
        }
        Ok(Self {
            client_id: wire.client_id,
            client_secret: wire.client_secret,
            refresh_token: wire.refresh_token,
        })
    }

    fn zeroize(&mut self) {
        zero_string(&mut self.client_id);
        zero_string(&mut self.client_secret);
        zero_string(&mut self.refresh_token);
    }
}

impl Drop for OAuthCredential {
    fn drop(&mut self) {
        self.zeroize();
    }
}

fn refresh_access_token<R: RuntimeIo>(
    runtime: &mut R,
    credential: &OAuthCredential,
) -> Result<String, String> {
    let mut response = checked_response(
        request_and_zeroize(
            runtime,
            HttpRequest {
                endpoint: Endpoint::OAuthToken,
                authorization: None,
                form: vec![
                    ("client_id".into(), credential.client_id.clone()),
                    ("client_secret".into(), credential.client_secret.clone()),
                    ("refresh_token".into(), credential.refresh_token.clone()),
                    ("grant_type".into(), "refresh_token".into()),
                ],
            },
        )?,
        "YouTube OAuth refresh",
    )?;
    #[derive(serde::Deserialize)]
    #[serde(deny_unknown_fields)]
    struct Token {
        access_token: String,
        expires_in: u64,
        token_type: String,
        #[serde(default)]
        scope: Option<String>,
    }
    let parsed = serde_json::from_slice(&response.body);
    response.body.fill(0);
    let mut token: Token = parsed.map_err(|_| "YouTube OAuth response was malformed")?;
    if token.token_type != "Bearer"
        || token.access_token.is_empty()
        || token.access_token.len() > MAX_SECRET_BYTES
        || token.access_token.chars().any(char::is_control)
        || !(60..=86_400).contains(&token.expires_in)
    {
        zero_string(&mut token.access_token);
        zero_string(&mut token.token_type);
        if let Some(scope) = &mut token.scope {
            zero_string(scope);
        }
        return Err("YouTube OAuth response was invalid or unbounded".into());
    }
    let access_token = std::mem::take(&mut token.access_token);
    zero_string(&mut token.token_type);
    if let Some(scope) = &mut token.scope {
        zero_string(scope);
    }
    Ok(access_token)
}

fn get_broadcast<R: RuntimeIo>(
    runtime: &mut R,
    broadcast_id: &str,
    authorization: &str,
) -> Result<YouTubeDocument, String> {
    let mut response = checked_response(
        request_and_zeroize(
            runtime,
            HttpRequest {
                endpoint: Endpoint::Broadcast {
                    id: broadcast_id.into(),
                },
                authorization: Some(authorization.into()),
                form: Vec::new(),
            },
        )?,
        "YouTube broadcast request",
    )?;
    YouTubeDocument::parse(
        std::mem::take(&mut response.body),
        "YouTube broadcast response",
    )
}

fn get_stream<R: RuntimeIo>(
    runtime: &mut R,
    stream_id: &str,
    authorization: &str,
) -> Result<YouTubeDocument, String> {
    let mut response = checked_response(
        request_and_zeroize(
            runtime,
            HttpRequest {
                endpoint: Endpoint::Stream {
                    id: stream_id.into(),
                },
                authorization: Some(authorization.into()),
                form: Vec::new(),
            },
        )?,
        "YouTube stream request",
    )?;
    YouTubeDocument::parse(
        std::mem::take(&mut response.body),
        "YouTube stream response",
    )
}

fn transition_broadcast<R: RuntimeIo>(
    runtime: &mut R,
    broadcast_id: &str,
    authorization: &str,
) -> Result<YouTubeDocument, String> {
    let mut response = checked_response(
        request_and_zeroize(
            runtime,
            HttpRequest {
                endpoint: Endpoint::Transition {
                    id: broadcast_id.into(),
                },
                authorization: Some(authorization.into()),
                form: Vec::new(),
            },
        )?,
        "YouTube broadcast transition",
    )?;
    YouTubeDocument::parse(
        std::mem::take(&mut response.body),
        "YouTube transition response",
    )
}

fn checked_response(mut response: HttpResponse, label: &str) -> Result<HttpResponse, String> {
    if response.location.is_some() || (300..400).contains(&response.status) {
        response.body.fill(0);
        return Err(format!(
            "{label} redirected; authorization was not forwarded"
        ));
    }
    if response.status == 401 || response.status == 403 {
        response.body.fill(0);
        return Err(format!("{label} was rejected by provider authentication"));
    }
    if response.status == 429 || response.status >= 500 {
        response.body.fill(0);
        return Err(format!("{label} hit a provider quota or retryable failure"));
    }
    if !(200..300).contains(&response.status) {
        response.body.fill(0);
        return Err(format!("{label} returned an unexpected provider status"));
    }
    if response.body.is_empty() || response.body.len() > MAX_HTTP_BODY_BYTES {
        response.body.fill(0);
        return Err(format!("{label} response is empty or oversized"));
    }
    Ok(response)
}

fn request_and_zeroize<R: RuntimeIo>(
    runtime: &mut R,
    mut request: HttpRequest,
) -> Result<HttpResponse, String> {
    let result = runtime.request(&mut request);
    if let Some(authorization) = &mut request.authorization {
        zero_string(authorization);
    }
    for (name, value) in &mut request.form {
        zero_string(name);
        zero_string(value);
    }
    result
}

impl Drop for HttpRequest {
    fn drop(&mut self) {
        if let Some(authorization) = &mut self.authorization {
            zero_string(authorization);
        }
        for (name, value) in &mut self.form {
            zero_string(name);
            zero_string(value);
        }
    }
}

struct YouTubeDocument {
    value: serde_json::Value,
}

impl YouTubeDocument {
    fn zeroize(&mut self) {
        zero_json(&mut self.value);
    }
}

impl Drop for YouTubeDocument {
    fn drop(&mut self) {
        self.zeroize();
    }
}

fn zero_json(value: &mut serde_json::Value) {
    match value {
        serde_json::Value::String(text) => zero_string(text),
        serde_json::Value::Array(values) => values.iter_mut().for_each(zero_json),
        serde_json::Value::Object(values) => {
            for (_, value) in values.iter_mut() {
                zero_json(value);
            }
        }
        _ => {}
    }
}

impl YouTubeDocument {
    fn parse(mut body: Vec<u8>, label: &str) -> Result<Self, String> {
        let parsed = serde_json::from_slice(&body);
        body.fill(0);
        let value = parsed.map_err(|_| format!("{label} was malformed"))?;
        Ok(Self { value })
    }

    fn only_item(&self, label: &str) -> Result<&serde_json::Value, String> {
        let items = self
            .value
            .get("items")
            .and_then(serde_json::Value::as_array)
            .ok_or_else(|| format!("{label} did not contain a bounded item list"))?;
        if items.len() != 1 {
            return Err(format!(
                "{label} did not return exactly one configured resource"
            ));
        }
        Ok(&items[0])
    }
}

fn require_broadcast_binding(
    document: &YouTubeDocument,
    channel_id: &str,
    broadcast_id: &str,
    stream_id: &str,
    privacy_status: YouTubePrivacyStatus,
    require_live: bool,
) -> Result<(), String> {
    let item = document.only_item("YouTube broadcast")?;
    let privacy = match privacy_status {
        YouTubePrivacyStatus::Public => "public",
        YouTubePrivacyStatus::Unlisted => "unlisted",
    };
    let lifecycle = item
        .pointer("/status/lifeCycleStatus")
        .and_then(serde_json::Value::as_str);
    if item.get("id").and_then(serde_json::Value::as_str) != Some(broadcast_id)
        || item
            .pointer("/snippet/channelId")
            .and_then(serde_json::Value::as_str)
            != Some(channel_id)
        || item
            .pointer("/contentDetails/boundStreamId")
            .and_then(serde_json::Value::as_str)
            != Some(stream_id)
        || item
            .pointer("/status/privacyStatus")
            .and_then(serde_json::Value::as_str)
            != Some(privacy)
    {
        return Err(
            "YouTube broadcast is not the exact configured channel, stream, and privacy binding"
                .into(),
        );
    }
    if require_live && lifecycle != Some("live") {
        return Err("YouTube broadcast is not live yet".into());
    }
    Ok(())
}

struct Ingestion {
    address: String,
    stream_key: String,
}

impl Ingestion {
    fn zeroize(&mut self) {
        zero_string(&mut self.address);
        zero_string(&mut self.stream_key);
    }
}

impl Drop for Ingestion {
    fn drop(&mut self) {
        self.zeroize();
    }
}

fn require_stream_binding(
    document: &YouTubeDocument,
    channel_id: &str,
    stream_id: &str,
    require_active: bool,
) -> Result<Ingestion, String> {
    let item = document.only_item("YouTube stream")?;
    let status = item
        .pointer("/status/streamStatus")
        .and_then(serde_json::Value::as_str);
    if item.get("id").and_then(serde_json::Value::as_str) != Some(stream_id)
        || item
            .pointer("/snippet/channelId")
            .and_then(serde_json::Value::as_str)
            != Some(channel_id)
        || item
            .pointer("/cdn/ingestionType")
            .and_then(serde_json::Value::as_str)
            != Some("rtmp")
    {
        return Err("YouTube stream is not the exact configured channel and RTMP binding".into());
    }
    if require_active && status != Some("active") {
        return Err("YouTube stream is not active yet".into());
    }
    if !require_active && !matches!(status, Some("ready" | "active")) {
        return Err("YouTube stream is not ready for the configured broadcast".into());
    }
    let address = item
        .pointer("/cdn/ingestionInfo/ingestionAddress")
        .and_then(serde_json::Value::as_str)
        .ok_or_else(|| "YouTube stream is missing a provider-issued RTMPS address".to_string())?
        .to_owned();
    let stream_key = item
        .pointer("/cdn/ingestionInfo/streamName")
        .and_then(serde_json::Value::as_str)
        .ok_or_else(|| "YouTube stream is missing a provider-issued stream key".to_string())?
        .to_owned();
    if address != "rtmps://a.rtmp.youtube.com/live2"
        || address.len() > 512
        || address.chars().any(char::is_control)
        || stream_key.is_empty()
        || stream_key.len() > 512
        || stream_key.chars().any(char::is_control)
    {
        return Err("YouTube provider-issued RTMPS ingestion authority is invalid".into());
    }
    Ok(Ingestion {
        address,
        stream_key,
    })
}

fn ffmpeg_argv(media_path: &Path, ingestion: &Ingestion) -> Result<Vec<String>, String> {
    if !media_path.is_absolute() || !media_path.is_file() {
        return Err("YouTube media path is unavailable".into());
    }
    let endpoint = format!(
        "{}/{}",
        ingestion.address.trim_end_matches('/'),
        ingestion.stream_key
    );
    Ok(vec![
        "-nostdin".into(),
        "-hide_banner".into(),
        "-loglevel".into(),
        "error".into(),
        "-re".into(),
        "-i".into(),
        media_path.to_string_lossy().into_owned(),
        "-map".into(),
        "0:v?".into(),
        "-map".into(),
        "0:a?".into(),
        "-c".into(),
        "copy".into(),
        "-f".into(),
        "flv".into(),
        endpoint,
    ])
}

fn poll_stream_active<R: RuntimeIo>(
    runtime: &mut R,
    process: &mut dyn RunningFfmpeg,
    channel_id: &str,
    stream_id: &str,
    authorization: &str,
    heartbeat: &mut dyn FnMut() -> Result<(), String>,
) -> Result<(), String> {
    let deadline = runtime
        .now()?
        .checked_add(STREAM_ACTIVE_TIMEOUT.as_millis() as i64)
        .ok_or_else(|| "YouTube stream activation deadline overflowed".to_string())?;
    loop {
        heartbeat()?;
        if process.try_wait()?.is_some() {
            return Err("YouTube ffmpeg stopped before the configured stream became active".into());
        }
        let mut document = get_stream(runtime, stream_id, authorization)?;
        let state = require_stream_binding(&document, channel_id, stream_id, true);
        document.zeroize();
        match state {
            Ok(_) => return Ok(()),
            Err(error) if error == "YouTube stream is not active yet" => {}
            Err(error) => return Err(error),
        }
        if runtime.now()? >= deadline {
            return Err("YouTube stream did not become active before its bounded deadline".into());
        }
        runtime.sleep(POLL_INTERVAL);
    }
}

#[allow(clippy::too_many_arguments)]
fn poll_broadcast_live<R: RuntimeIo>(
    runtime: &mut R,
    process: &mut dyn RunningFfmpeg,
    channel_id: &str,
    broadcast_id: &str,
    stream_id: &str,
    privacy_status: YouTubePrivacyStatus,
    authorization: &str,
    heartbeat: &mut dyn FnMut() -> Result<(), String>,
) -> Result<YouTubeDocument, String> {
    let deadline = runtime
        .now()?
        .checked_add(BROADCAST_LIVE_TIMEOUT.as_millis() as i64)
        .ok_or_else(|| "YouTube broadcast observation deadline overflowed".to_string())?;
    loop {
        heartbeat()?;
        if process.try_wait()?.is_some() {
            return Err(
                "YouTube ffmpeg stopped before the configured broadcast became live".into(),
            );
        }
        let mut document = get_broadcast(runtime, broadcast_id, authorization)?;
        let state = require_broadcast_binding(
            &document,
            channel_id,
            broadcast_id,
            stream_id,
            privacy_status,
            true,
        );
        match state {
            Ok(()) => return Ok(document),
            Err(error) if error == "YouTube broadcast is not live yet" => document.zeroize(),
            Err(error) => {
                document.zeroize();
                return Err(error);
            }
        }
        if runtime.now()? >= deadline {
            return Err("YouTube broadcast did not become live before its bounded deadline".into());
        }
        runtime.sleep(POLL_INTERVAL);
    }
}

fn verify_ffmpeg_identity(ffmpeg: &VerifiedFfmpeg) -> Result<File, String> {
    if !ffmpeg.executable.is_absolute() || !is_sha256(&ffmpeg.sha256) {
        return Err("YouTube ffmpeg runtime identity is invalid".into());
    }
    let metadata = fs::symlink_metadata(&ffmpeg.executable)
        .map_err(|_| "YouTube ffmpeg executable is unavailable")?;
    if !metadata.is_file() || metadata.file_attributes() & 0x0400 != 0 {
        return Err("YouTube ffmpeg executable is not a regular installed file".into());
    }
    let mut file = OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(&ffmpeg.executable)
        .map_err(|_| "YouTube ffmpeg executable could not be pinned")?;
    let pinned = file
        .metadata()
        .map_err(|_| "YouTube ffmpeg executable could not be inspected")?;
    if !pinned.is_file() || pinned.file_attributes() & 0x0400 != 0 {
        return Err("YouTube ffmpeg executable is a reparse point or not a file".into());
    }
    let digest = hash_open_file(&mut file)?;
    if digest != ffmpeg.sha256 {
        return Err(
            "YouTube ffmpeg executable does not match its verified installed identity".into(),
        );
    }
    Ok(file)
}

fn verify_installed_ffmpeg(
    application_root: &Path,
    data_root: &Path,
) -> Result<VerifiedFfmpeg, String> {
    if !application_root.is_absolute() || !data_root.is_absolute() {
        return Err("YouTube installed runtime roots must be absolute".into());
    }
    // This validates the installation/active-slot authority before reading the
    // decoder identity written by the same installer transaction.
    let _authority =
        station_maintenance::read_verified_install_authority(data_root, application_root)
            .map_err(|_| "YouTube installed candidate authority is unavailable")?;
    let receipt_path = data_root.join("install-receipt.json");
    let bytes =
        fs::read(&receipt_path).map_err(|_| "YouTube installation receipt is unavailable")?;
    if bytes.is_empty() || bytes.len() > 64 * 1024 {
        return Err("YouTube installation receipt is invalid or oversized".into());
    }
    let document: serde_json::Value =
        serde_json::from_slice(&bytes).map_err(|_| "YouTube installation receipt is malformed")?;
    let sha256 = document
        .get("caption_decoder_sha256")
        .and_then(serde_json::Value::as_str)
        .filter(|value| is_sha256(value))
        .ok_or_else(|| {
            "YouTube installation receipt lacks the private ffmpeg identity".to_string()
        })?
        .to_owned();
    let executable = application_root
        .join("runtime")
        .join("caption-decoder")
        .join("ffmpeg.exe");
    if !executable.starts_with(application_root) {
        return Err("YouTube ffmpeg escaped the application root".into());
    }
    Ok(VerifiedFfmpeg { executable, sha256 })
}

fn hash_open_file(file: &mut File) -> Result<String, String> {
    file.seek(SeekFrom::Start(0))
        .map_err(|_| "YouTube ffmpeg executable could not be rewound")?;
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 8192];
    loop {
        let count = file
            .read(&mut buffer)
            .map_err(|_| "YouTube ffmpeg executable could not be read")?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    Ok(format!("{:x}", digest.finalize()))
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn zero_string(value: &mut String) {
    // `String::as_mut_vec` is safe here because replacement bytes preserve UTF-8.
    unsafe { value.as_mut_vec().fill(0) };
    value.clear();
}

struct SystemRuntimeIo {
    agent: ureq::Agent,
}

impl SystemRuntimeIo {
    fn new() -> Self {
        Self {
            agent: ureq::Agent::config_builder()
                .timeout_global(Some(Duration::from_secs(30)))
                // We inspect every provider status ourselves so a 429/5xx is
                // classified as bounded retryable work and never becomes a
                // generic transport error.
                .http_status_as_error(false)
                .max_redirects(0)
                .max_redirects_will_error(true)
                .build()
                .into(),
        }
    }
}

impl RuntimeIo for SystemRuntimeIo {
    fn request(&mut self, request: &mut HttpRequest) -> Result<HttpResponse, String> {
        let url = endpoint_url(&request.endpoint)?;
        let response = match request.endpoint {
            Endpoint::OAuthToken => {
                if request.authorization.is_some() {
                    return Err("YouTube OAuth request attempted to carry authorization".into());
                }
                let mut form = request
                    .form
                    .iter()
                    .map(|(key, value)| {
                        format!("{}={}", form_component(key), form_component(value))
                    })
                    .collect::<Vec<_>>()
                    .join("&");
                let result = self
                    .agent
                    .post(&url)
                    .header("Content-Type", "application/x-www-form-urlencoded")
                    .send(form.as_bytes())
                    .map_err(|_| "YouTube OAuth refresh transport failed");
                zero_string(&mut form);
                result?
            }
            Endpoint::Broadcast { .. } | Endpoint::Stream { .. } => self
                .agent
                .get(&url)
                .header(
                    "Authorization",
                    request
                        .authorization
                        .as_deref()
                        .ok_or("YouTube request lacks authorization")?,
                )
                .call()
                .map_err(|_| "YouTube live-resource transport failed")?,
            Endpoint::Transition { .. } => self
                .agent
                .post(&url)
                .header(
                    "Authorization",
                    request
                        .authorization
                        .as_deref()
                        .ok_or("YouTube transition lacks authorization")?,
                )
                .send_empty()
                .map_err(|_| "YouTube transition transport failed")?,
        };
        let status = response.status().as_u16();
        let location = response
            .headers()
            .get("Location")
            .and_then(|value| value.to_str().ok())
            .map(ToOwned::to_owned);
        let mut response = response;
        let mut body = Vec::new();
        response
            .body_mut()
            .as_reader()
            .take((MAX_HTTP_BODY_BYTES + 1) as u64)
            .read_to_end(&mut body)
            .map_err(|_| "YouTube provider response could not be read")?;
        Ok(HttpResponse {
            status,
            location,
            body,
        })
    }

    fn start_ffmpeg(
        &mut self,
        executable: &Path,
        argv: &[String],
    ) -> Result<Box<dyn RunningFfmpeg>, String> {
        SystemFfmpegProcess::spawn(executable, argv)
            .map(|process| Box::new(process) as Box<dyn RunningFfmpeg>)
    }

    fn now(&mut self) -> Result<i64, String> {
        super::unix_time_millis()
    }

    fn sleep(&mut self, duration: Duration) {
        std::thread::sleep(duration);
    }
}

fn endpoint_url(endpoint: &Endpoint) -> Result<String, String> {
    match endpoint {
        Endpoint::OAuthToken => Ok("https://oauth2.googleapis.com/token".into()),
        Endpoint::Broadcast { id } => Ok(format!(
            "https://www.googleapis.com/youtube/v3/liveBroadcasts?part=snippet,status,contentDetails&id={}",
            form_component(id)
        )),
        Endpoint::Stream { id } => Ok(format!(
            "https://www.googleapis.com/youtube/v3/liveStreams?part=snippet,cdn,status&id={}",
            form_component(id)
        )),
        Endpoint::Transition { id } => Ok(format!(
            "https://www.googleapis.com/youtube/v3/liveBroadcasts/transition?broadcastStatus=live&id={}&part=status,snippet,contentDetails",
            form_component(id)
        )),
    }
}

fn form_component(value: &str) -> String {
    let mut encoded = String::with_capacity(value.len());
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b'~') {
            encoded.push(char::from(byte));
        } else {
            encoded.push('%');
            encoded.push_str(&format!("{byte:02X}"));
        }
    }
    encoded
}

struct WindowsProcessTree(HANDLE);

impl WindowsProcessTree {
    fn new() -> Result<Self, String> {
        let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if handle == INVALID_HANDLE_VALUE || handle.is_null() {
            return Err("YouTube ffmpeg process tree boundary could not be created".into());
        }
        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let ok = unsafe {
            SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                u32::try_from(std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>())
                    .expect("fits"),
            )
        };
        if ok == 0 {
            unsafe { CloseHandle(handle) };
            return Err("YouTube ffmpeg process tree boundary could not be configured".into());
        }
        Ok(Self(handle))
    }

    fn assign(&self, child: &Child) -> Result<(), String> {
        if unsafe { AssignProcessToJobObject(self.0, child.as_raw_handle() as HANDLE) } == 0 {
            return Err("YouTube ffmpeg process could not be contained".into());
        }
        Ok(())
    }

    fn terminate(&self) {
        unsafe { TerminateJobObject(self.0, 1) };
    }
}

impl Drop for WindowsProcessTree {
    fn drop(&mut self) {
        unsafe { CloseHandle(self.0) };
    }
}

struct SystemFfmpegProcess {
    child: Child,
    job: WindowsProcessTree,
    stdout: mpsc::Receiver<Result<(), String>>,
    stderr: mpsc::Receiver<Result<(), String>>,
    stdout_drain: Option<JoinHandle<()>>,
    stderr_drain: Option<JoinHandle<()>>,
}

impl SystemFfmpegProcess {
    fn spawn(executable: &Path, argv: &[String]) -> Result<Self, String> {
        let job = WindowsProcessTree::new()?;
        let mut child = Command::new(executable)
            .args(argv)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|_| "YouTube ffmpeg process could not be started")?;
        if let Err(error) = job.assign(&child) {
            let _ = child.kill();
            let _ = child.wait();
            return Err(error);
        }
        let stdout = child.stdout.take().ok_or_else(|| {
            job.terminate();
            let _ = child.wait();
            "YouTube ffmpeg stdout pipe was unavailable"
        })?;
        let stderr = child.stderr.take().ok_or_else(|| {
            job.terminate();
            let _ = child.wait();
            "YouTube ffmpeg stderr pipe was unavailable"
        })?;
        let (stdout_tx, stdout_rx) = mpsc::sync_channel(1);
        let (stderr_tx, stderr_rx) = mpsc::sync_channel(1);
        let stdout_drain = std::thread::spawn(move || {
            let _ = stdout_tx.send(drain(stdout));
        });
        let stderr_drain = std::thread::spawn(move || {
            let _ = stderr_tx.send(drain(stderr));
        });
        Ok(Self {
            child,
            job,
            stdout: stdout_rx,
            stderr: stderr_rx,
            stdout_drain: Some(stdout_drain),
            stderr_drain: Some(stderr_drain),
        })
    }

    fn collect_drains(
        &mut self,
        heartbeat: &mut dyn FnMut() -> Result<(), String>,
    ) -> Result<(), String> {
        let deadline = Instant::now() + PROCESS_DRAIN_TIMEOUT;
        let receive = |receiver: &mpsc::Receiver<Result<(), String>>,
                       label: &str,
                       heartbeat: &mut dyn FnMut() -> Result<(), String>|
         -> Result<(), String> {
            loop {
                let left = deadline.saturating_duration_since(Instant::now());
                if left.is_zero() {
                    return Err(format!(
                        "YouTube ffmpeg {label} drain exceeded its deadline"
                    ));
                }
                match receiver.recv_timeout(left.min(Duration::from_millis(100))) {
                    Ok(value) => return value,
                    Err(mpsc::RecvTimeoutError::Timeout) => heartbeat()?,
                    Err(mpsc::RecvTimeoutError::Disconnected) => {
                        return Err(format!("YouTube ffmpeg {label} drain stopped unexpectedly"));
                    }
                }
            }
        };
        let stdout = receive(&self.stdout, "stdout", heartbeat);
        let stderr = receive(&self.stderr, "stderr", heartbeat);
        if stdout.is_err() || stderr.is_err() {
            self.job.terminate();
        }
        self.join_drains(heartbeat)?;
        stdout?;
        stderr
    }

    fn join_drains(
        &mut self,
        heartbeat: &mut dyn FnMut() -> Result<(), String>,
    ) -> Result<(), String> {
        let mut heartbeat_error = None;
        for drain in [&mut self.stdout_drain, &mut self.stderr_drain] {
            let Some(handle) = drain.as_ref() else {
                continue;
            };
            let deadline = Instant::now() + Duration::from_secs(1);
            while !handle.is_finished() && Instant::now() < deadline {
                self.job.terminate();
                unsafe { CancelSynchronousIo(handle.as_raw_handle() as HANDLE) };
                if heartbeat_error.is_none()
                    && let Err(error) = heartbeat()
                {
                    heartbeat_error = Some(error);
                }
                std::thread::sleep(Duration::from_millis(10));
            }
        }
        let mut join_panicked = false;
        for drain in [&mut self.stdout_drain, &mut self.stderr_drain] {
            if let Some(handle) = drain.take() {
                join_panicked |= handle.join().is_err();
            }
        }
        if join_panicked {
            return Err("YouTube ffmpeg pipe drain panicked".into());
        }
        if let Some(error) = heartbeat_error {
            return Err(error);
        }
        Ok(())
    }
}

impl RunningFfmpeg for SystemFfmpegProcess {
    fn try_wait(&mut self) -> Result<Option<bool>, String> {
        self.child
            .try_wait()
            .map(|status| status.map(|value| value.success()))
            .map_err(|_| "YouTube ffmpeg status could not be read".into())
    }

    fn terminate_and_join(
        &mut self,
        heartbeat: &mut dyn FnMut() -> Result<(), String>,
    ) -> Result<(), String> {
        self.job.terminate();
        let deadline = Instant::now() + PROCESS_DRAIN_TIMEOUT;
        let mut wait_error = None;
        loop {
            match self.child.try_wait() {
                Ok(Some(_)) => break,
                Ok(None) if Instant::now() < deadline => {
                    if wait_error.is_none()
                        && let Err(error) = heartbeat()
                    {
                        wait_error = Some(error);
                    }
                    std::thread::sleep(Duration::from_millis(10));
                }
                Ok(None) => {
                    wait_error.get_or_insert_with(|| {
                        "YouTube ffmpeg did not exit before its bounded teardown deadline".into()
                    });
                    break;
                }
                Err(_) => {
                    wait_error
                        .get_or_insert_with(|| "YouTube ffmpeg status could not be read".into());
                    break;
                }
            }
        }
        let drains = self.collect_drains(heartbeat);
        match (wait_error, drains) {
            (None, Ok(())) => Ok(()),
            (Some(wait), Ok(())) => Err(wait),
            (None, Err(drains)) => Err(drains),
            (Some(wait), Err(drains)) => Err(format!("{wait}; {drains}")),
        }
    }
}

fn drain<T: Read>(stream: T) -> Result<(), String> {
    let mut output = Vec::new();
    stream
        .take((PROCESS_OUTPUT_BYTES + 1) as u64)
        .read_to_end(&mut output)
        .map_err(|_| "YouTube ffmpeg process output could not be read")?;
    if output.len() > PROCESS_OUTPUT_BYTES {
        return Err("YouTube ffmpeg process output exceeded its bound".into());
    }
    output.fill(0);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;
    use std::sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    };

    struct FakeProcess {
        terminated: Arc<AtomicBool>,
    }

    impl RunningFfmpeg for FakeProcess {
        fn try_wait(&mut self) -> Result<Option<bool>, String> {
            Ok(None)
        }
        fn terminate_and_join(
            &mut self,
            _: &mut dyn FnMut() -> Result<(), String>,
        ) -> Result<(), String> {
            self.terminated.store(true, Ordering::Release);
            Ok(())
        }
    }

    struct FakeRuntime {
        responses: VecDeque<HttpResponse>,
        requests: Vec<(String, bool)>,
        now: i64,
        executable: Option<PathBuf>,
        argv: Vec<String>,
        terminated: Arc<AtomicBool>,
    }

    impl RuntimeIo for FakeRuntime {
        fn request(&mut self, request: &mut HttpRequest) -> Result<HttpResponse, String> {
            self.requests.push((
                format!("{:?}", request.endpoint),
                request.authorization.is_some(),
            ));
            self.responses
                .pop_front()
                .ok_or_else(|| "missing controlled response".into())
        }
        fn start_ffmpeg(
            &mut self,
            executable: &Path,
            argv: &[String],
        ) -> Result<Box<dyn RunningFfmpeg>, String> {
            self.executable = Some(executable.to_path_buf());
            self.argv = argv.to_vec();
            Ok(Box::new(FakeProcess {
                terminated: Arc::clone(&self.terminated),
            }))
        }
        fn now(&mut self) -> Result<i64, String> {
            Ok(self.now)
        }
        fn sleep(&mut self, duration: Duration) {
            self.now += duration.as_millis() as i64;
        }
    }

    fn job() -> PublishExecutionJob {
        PublishExecutionJob {
            job_id: "11111111-2222-4333-8444-555555555555".into(),
            account_id: "22222222-2222-4333-8444-555555555555".into(),
            account_revision: 1,
            target_id: "33333333-2222-4333-8444-555555555555".into(),
            target_revision: 1,
            target_kind: station_publishing::PublishTargetKind::YouTubeLive,
            configuration_hash: "a".repeat(64),
            credential_version: 1,
            candidate_version: "0.1.0".into(),
            source_commit: "b".repeat(40),
            install_generation: 1,
            asset_id: "c".repeat(64),
            media_sha256: "c".repeat(64),
            media_bytes: 1,
            requested_by: "test".into(),
            created_at_unix_ms: 1,
        }
    }

    fn response(value: serde_json::Value) -> HttpResponse {
        HttpResponse {
            status: 200,
            location: None,
            body: serde_json::to_vec(&value).unwrap(),
        }
    }

    fn broadcast(lifecycle: &str) -> HttpResponse {
        response(
            serde_json::json!({"items":[{"id":"broadcast","snippet":{"channelId":"channel"},"contentDetails":{"boundStreamId":"stream"},"status":{"privacyStatus":"unlisted","lifeCycleStatus":lifecycle}}]}),
        )
    }

    fn stream(status: &str) -> HttpResponse {
        response(
            serde_json::json!({"items":[{"id":"stream","snippet":{"channelId":"channel"},"cdn":{"ingestionType":"rtmp","ingestionInfo":{"ingestionAddress":"rtmps://a.rtmp.youtube.com/live2","streamName":"never-durable-key"}},"status":{"streamStatus":status}}]}),
        )
    }

    fn ffmpeg() -> VerifiedFfmpeg {
        let executable = std::env::current_exe().unwrap();
        let sha256 = hash_open_file(&mut File::open(&executable).unwrap()).unwrap();
        VerifiedFfmpeg { executable, sha256 }
    }

    fn runtime(responses: Vec<HttpResponse>) -> FakeRuntime {
        FakeRuntime {
            responses: responses.into(),
            requests: Vec::new(),
            now: 0,
            executable: None,
            argv: Vec::new(),
            terminated: Arc::new(AtomicBool::new(false)),
        }
    }

    fn invoke(runtime: &mut FakeRuntime, media: &Path) -> Result<ProviderStageEvidence, String> {
        execute_with(
            runtime,
            &job(),
            "channel",
            "broadcast",
            "stream",
            YouTubePrivacyStatus::Unlisted,
            media,
            br#"{"client_id":"id","client_secret":"secret","refresh_token":"refresh"}"#,
            &ffmpeg(),
            &mut || Ok(()),
        )
    }

    #[test]
    fn exact_success_observes_active_stream_then_live_broadcast_without_durably_retaining_secrets()
    {
        let media = std::env::temp_dir().join("townlight-youtube-runtime-test.mp4");
        fs::write(&media, [0]).unwrap();
        let mut runtime = runtime(vec![
            response(
                serde_json::json!({"access_token":"test-access","expires_in":3600,"token_type":"Bearer"}),
            ),
            broadcast("ready"),
            stream("ready"),
            stream("active"),
            broadcast("live"),
            broadcast("live"),
        ]);
        let mut beats = 0;
        let result = execute_with(
            &mut runtime,
            &job(),
            "channel",
            "broadcast",
            "stream",
            YouTubePrivacyStatus::Unlisted,
            &media,
            br#"{"client_id":"id","client_secret":"secret","refresh_token":"refresh"}"#,
            &ffmpeg(),
            &mut || {
                beats += 1;
                Ok(())
            },
        );
        let _ = fs::remove_file(media);
        let result = result.expect("exact controlled YouTube lifecycle must publish");
        assert_eq!(result.stage, PublishExecutionStage::Published);
        assert!(result.detail.contains("exact configured broadcast live"));
        assert!(runtime.requests[0].0.contains("OAuthToken") && !runtime.requests[0].1);
        assert!(
            runtime
                .requests
                .iter()
                .skip(1)
                .all(|(_, authorized)| *authorized)
        );
        assert!(
            runtime
                .argv
                .iter()
                .any(|argument| argument.contains("never-durable-key"))
        );
        assert!(!format!("{result:?}").contains("never-durable-key"));
        assert!(runtime.terminated.load(Ordering::Acquire));
        for argument in &mut runtime.argv {
            zero_string(argument);
        }
    }

    #[test]
    fn rejects_oauth_redirect_before_any_authorized_request_or_process() {
        let media = std::env::temp_dir().join("townlight-youtube-oauth-redirect.mp4");
        fs::write(&media, [0]).unwrap();
        let mut fake = runtime(vec![HttpResponse {
            status: 302,
            location: Some("https://evil.invalid".into()),
            body: b"redirect".to_vec(),
        }]);
        let error = invoke(&mut fake, &media).unwrap_err();
        let _ = fs::remove_file(media);
        assert!(error.contains("redirected"));
        assert_eq!(fake.requests.len(), 1);
        assert!(!fake.requests[0].1);
        assert!(fake.executable.is_none());
    }

    #[test]
    fn rejects_provider_redirect_without_forwarding_authorization_or_starting_ffmpeg() {
        let media = std::env::temp_dir().join("townlight-youtube-resource-redirect.mp4");
        fs::write(&media, [0]).unwrap();
        let mut fake = runtime(vec![
            response(
                serde_json::json!({"access_token":"test-access","expires_in":3600,"token_type":"Bearer"}),
            ),
            broadcast("ready"),
            HttpResponse {
                status: 302,
                location: Some("https://evil.invalid".into()),
                body: b"redirect".to_vec(),
            },
        ]);
        let error = invoke(&mut fake, &media).unwrap_err();
        let _ = fs::remove_file(media);
        assert!(error.contains("redirected"));
        assert_eq!(fake.requests.len(), 3);
        assert!(!fake.requests[0].1);
        assert!(fake.requests[1].1 && fake.requests[2].1);
        assert!(fake.executable.is_none());
    }

    #[test]
    fn rejects_wrong_bound_identifier_without_starting_ffmpeg() {
        let media = std::env::temp_dir().join("townlight-youtube-wrong-id.mp4");
        fs::write(&media, [0]).unwrap();
        let mut wrong = broadcast("ready");
        let mut value: serde_json::Value = serde_json::from_slice(&wrong.body).unwrap();
        value["items"][0]["contentDetails"]["boundStreamId"] = serde_json::json!("other");
        wrong.body = serde_json::to_vec(&value).unwrap();
        let mut fake = runtime(vec![
            response(
                serde_json::json!({"access_token":"test-access","expires_in":3600,"token_type":"Bearer"}),
            ),
            wrong,
        ]);
        let error = invoke(&mut fake, &media).unwrap_err();
        let _ = fs::remove_file(media);
        assert!(error.contains("exact configured"));
        assert!(fake.executable.is_none());
    }

    #[test]
    fn provider_quota_is_classified_as_retryable_and_process_is_not_started() {
        let media = std::env::temp_dir().join("townlight-youtube-quota.mp4");
        fs::write(&media, [0]).unwrap();
        let mut fake = runtime(vec![
            response(
                serde_json::json!({"access_token":"test-access","expires_in":3600,"token_type":"Bearer"}),
            ),
            HttpResponse {
                status: 429,
                location: None,
                body: b"quota".to_vec(),
            },
        ]);
        let error = invoke(&mut fake, &media).unwrap_err();
        let _ = fs::remove_file(media);
        assert!(error.contains("quota or retryable"));
        assert!(fake.executable.is_none());
    }

    #[test]
    fn active_stream_timeout_always_terminates_the_owned_ffmpeg_process() {
        let media = std::env::temp_dir().join("townlight-youtube-active-timeout.mp4");
        fs::write(&media, [0]).unwrap();
        let mut responses = vec![
            response(
                serde_json::json!({"access_token":"test-access","expires_in":3600,"token_type":"Bearer"}),
            ),
            broadcast("ready"),
            stream("ready"),
        ];
        responses.extend((0..122).map(|_| stream("ready")));
        let mut fake = runtime(responses);
        let error = invoke(&mut fake, &media).unwrap_err();
        let _ = fs::remove_file(media);
        assert!(error.contains("did not become active"));
        assert!(fake.terminated.load(Ordering::Acquire));
    }

    #[test]
    fn live_broadcast_timeout_always_terminates_the_owned_ffmpeg_process() {
        let media = std::env::temp_dir().join("townlight-youtube-live-timeout.mp4");
        fs::write(&media, [0]).unwrap();
        let mut responses = vec![
            response(
                serde_json::json!({"access_token":"test-access","expires_in":3600,"token_type":"Bearer"}),
            ),
            broadcast("ready"),
            stream("ready"),
            stream("active"),
            broadcast("live"),
        ];
        responses.extend((0..122).map(|_| broadcast("ready")));
        let mut fake = runtime(responses);
        let error = invoke(&mut fake, &media).unwrap_err();
        let _ = fs::remove_file(media);
        assert!(error.contains("did not become live"));
        assert!(fake.terminated.load(Ordering::Acquire));
    }
}
