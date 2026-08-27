//! Private, bounded YouTube Live execution boundary.
//!
//! Provider credentials, OAuth access tokens, provider-issued stream keys and
//! process output deliberately never cross this module's return boundary.

use super::{ProviderStageEvidence, PublishExecutionJob};
use sha2::{Digest, Sha256};
use station_publishing::{PublishExecutionStage, YouTubePrivacyStatus};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
use std::os::windows::io::AsRawHandle;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};
use windows_sys::Win32::Foundation::{CloseHandle, HANDLE, INVALID_HANDLE_VALUE};
use windows_sys::Win32::Storage::FileSystem::{FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_READ};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JobObjectExtendedLimitInformation,
    SetInformationJobObject, TerminateJobObject,
};
use zeroize::{Zeroize, Zeroizing};

const MAX_HTTP_BODY_BYTES: usize = 1_048_576;
const MAX_SECRET_BYTES: usize = 4_096;
const STREAM_ACTIVE_TIMEOUT: Duration = Duration::from_secs(60);
const BROADCAST_LIVE_TIMEOUT: Duration = Duration::from_secs(60);
const BROADCAST_COMPLETE_TIMEOUT: Duration = Duration::from_secs(60);
const POLL_INTERVAL: Duration = Duration::from_millis(500);
const PROCESS_DRAIN_TIMEOUT: Duration = Duration::from_secs(5);
const WORKER_RESULT_BYTES: usize = 4 * 1024;
const ACCESS_TOKEN_REFRESH_SKEW_MS: i64 = 10_000;
const MAX_ONE_SHOT_WORKER_RUNTIME: Duration = Duration::from_secs(24 * 60 * 60);
const ONE_SHOT_WORKER_RUNTIME_GRACE: Duration = Duration::from_secs(10 * 60);

#[derive(Debug, Clone, PartialEq, Eq)]
enum Endpoint {
    OAuthToken,
    Broadcast { id: String },
    Stream { id: String },
    TransitionLive { id: String },
    TransitionComplete { id: String },
}

struct HttpRequest {
    endpoint: Endpoint,
    authorization: Option<Zeroizing<String>>,
    form: Vec<(Zeroizing<String>, Zeroizing<String>)>,
}

struct HttpResponse {
    status: u16,
    location: Option<String>,
    body: Vec<u8>,
}

impl Drop for HttpResponse {
    fn drop(&mut self) {
        if let Some(location) = &mut self.location {
            zero_string(location);
        }
        self.body.fill(0);
    }
}

trait RunningFfmpeg {
    fn try_wait(&mut self) -> Result<Option<bool>, String>;
    fn wait_for_delivery(
        &mut self,
        maximum_runtime: Duration,
        heartbeat: &mut dyn FnMut() -> Result<(), String>,
    ) -> Result<WorkerDelivery, String>;
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
        command: &mut WorkerCommand,
    ) -> Result<Box<dyn RunningFfmpeg>, String>;
    fn now(&mut self) -> Result<i64, String>;
    fn sleep(&mut self, duration: Duration);
}

#[derive(Debug, Clone)]
struct VerifiedFfmpeg {
    executable: PathBuf,
    sha256: String,
}

struct WorkerCommand {
    schema_version: u8,
    managed_media_root: String,
    media_path: String,
    expected_bytes: u64,
    expected_sha256: String,
    expected_duration_ms: u64,
    rtmps_ingestion_address: String,
    stream_key: Zeroizing<String>,
}

impl serde::Serialize for WorkerCommand {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        use serde::ser::SerializeStruct;

        let mut state = serializer.serialize_struct("WorkerCommand", 8)?;
        state.serialize_field("schema_version", &self.schema_version)?;
        state.serialize_field("managed_media_root", &self.managed_media_root)?;
        state.serialize_field("media_path", &self.media_path)?;
        state.serialize_field("expected_bytes", &self.expected_bytes)?;
        state.serialize_field("expected_sha256", &self.expected_sha256)?;
        state.serialize_field("expected_duration_ms", &self.expected_duration_ms)?;
        state.serialize_field("rtmps_ingestion_address", &self.rtmps_ingestion_address)?;
        state.serialize_field("stream_key", self.stream_key.as_str())?;
        state.end()
    }
}

impl Drop for WorkerCommand {
    fn drop(&mut self) {
        zero_string(&mut self.rtmps_ingestion_address);
        zero_string(&mut self.stream_key);
    }
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct WorkerDelivery {
    schema_version: u8,
    outcome: String,
    media_bytes: u64,
    media_sha256: String,
    failure_code: Option<String>,
}

impl Drop for WorkerDelivery {
    fn drop(&mut self) {
        zero_string(&mut self.outcome);
        zero_string(&mut self.media_sha256);
        if let Some(failure_code) = &mut self.failure_code {
            zero_string(failure_code);
        }
    }
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
    let mut runtime = SystemRuntimeIo::new(application_root);
    execute_with(
        &mut runtime,
        job,
        channel_id,
        broadcast_id,
        stream_id,
        privacy_status,
        media_path,
        &data_root.join("media"),
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
    managed_media_root: &Path,
    credential: &[u8],
    ffmpeg: &VerifiedFfmpeg,
    heartbeat: &mut dyn FnMut() -> Result<(), String>,
) -> Result<ProviderStageEvidence, String> {
    heartbeat()?;
    // Keep this no-delete handle alive until after the child has been spawned
    // and reaped. The process path cannot be substituted between its verified
    // identity check and this controller's bounded execution.
    let _ffmpeg_pin = verify_ffmpeg_identity(ffmpeg)?;
    let oauth = OAuthCredential::parse(credential)?;
    let mut access_token = refresh_access_token(runtime, &oauth)?;
    let result = {
        let mut authorization = access_token.authorization();
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
            let broadcast_digest = broadcast.digest();
            broadcast.zeroize();
            let mut stream = get_stream(runtime, stream_id, &authorization)?;
            let mut ingestion = require_stream_binding(&mut stream, channel_id, stream_id, false)?;
            let stream_binding_digest = stream.digest();
            let mut command = WorkerCommand {
                schema_version: 1,
                managed_media_root: managed_media_root.to_string_lossy().into_owned(),
                media_path: media_path.to_string_lossy().into_owned(),
                expected_bytes: job.media_bytes,
                expected_sha256: job.media_sha256.clone(),
                expected_duration_ms: job.media_duration_ms,
                rtmps_ingestion_address: std::mem::take(&mut ingestion.address),
                stream_key: std::mem::take(&mut ingestion.stream_key),
            };
            ingestion.zeroize();
            stream.zeroize();
            let process = runtime.start_ffmpeg(&ffmpeg.executable, &mut command)?;
            drop(command);
            let mut process = Some(process);
            let outcome = (|| {
                let mut active_stream = poll_stream_active(
                    runtime,
                    process.as_deref_mut().expect("ffmpeg process retained"),
                    channel_id,
                    stream_id,
                    &oauth,
                    &mut access_token,
                    &mut authorization,
                    heartbeat,
                )?;
                let active_stream_digest = active_stream.digest();
                active_stream.zeroize();
                refresh_authorization_if_due(
                    runtime,
                    &oauth,
                    &mut access_token,
                    &mut authorization,
                )?;
                let mut transitioned =
                    transition_broadcast_live(runtime, broadcast_id, &authorization)?;
                require_broadcast_binding(
                    &transitioned,
                    channel_id,
                    broadcast_id,
                    stream_id,
                    privacy_status,
                    false,
                )?;
                let live_transition_digest = transitioned.digest();
                transitioned.zeroize();
                let mut observed = poll_broadcast_live(
                    runtime,
                    process.as_deref_mut().expect("ffmpeg process retained"),
                    channel_id,
                    broadcast_id,
                    stream_id,
                    privacy_status,
                    &oauth,
                    &mut access_token,
                    &mut authorization,
                    heartbeat,
                )?;
                let live_observation_digest = observed.digest();
                observed.zeroize();
                let maximum_runtime = bounded_worker_runtime(job.media_duration_ms);
                let delivery = process
                    .as_deref_mut()
                    .expect("publisher process retained")
                    .wait_for_delivery(maximum_runtime, heartbeat)?;
                validate_worker_delivery(&delivery, job)?;
                process.take();
                refresh_authorization_if_due(
                    runtime,
                    &oauth,
                    &mut access_token,
                    &mut authorization,
                )?;
                let mut completed =
                    transition_broadcast_complete(runtime, broadcast_id, &authorization)?;
                require_broadcast_lifecycle(
                    &completed,
                    channel_id,
                    broadcast_id,
                    stream_id,
                    privacy_status,
                    "complete",
                )?;
                let complete_transition_digest = completed.digest();
                completed.zeroize();
                let mut completed_observation = poll_broadcast_complete(
                    runtime,
                    channel_id,
                    broadcast_id,
                    stream_id,
                    privacy_status,
                    &oauth,
                    &mut access_token,
                    &mut authorization,
                    heartbeat,
                )?;
                let complete_observation_digest = completed_observation.digest();
                completed_observation.zeroize();
                let terminal_observed_at_monotonic_ms = runtime.now()?;
                let evidence = format!(
                    "youtube-live-v3|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}",
                    broadcast_digest,
                    stream_binding_digest,
                    active_stream_digest,
                    live_transition_digest,
                    live_observation_digest,
                    complete_transition_digest,
                    complete_observation_digest,
                    delivery.schema_version,
                    delivery.outcome,
                    delivery.media_sha256,
                    delivery.media_bytes,
                    delivery.failure_code.as_deref().unwrap_or(""),
                    terminal_observed_at_monotonic_ms,
                    job.media_sha256,
                    job.media_bytes,
                );
                Ok::<_, String>(format!("{:x}", Sha256::digest(evidence.as_bytes())))
            })();
            let cleanup = if let Some(mut process) = process.take() {
                process.terminate_and_join(heartbeat)
            } else {
                Ok(())
            };
            match (outcome, cleanup) {
                (Ok(digest), Ok(())) => Ok(digest),
                (Err(primary), Ok(())) => Err(primary),
                (Ok(_), Err(cleanup)) => Err(format!("YouTube ffmpeg teardown failed: {cleanup}")),
                (Err(primary), Err(cleanup)) => Err(format!(
                    "{primary}; YouTube ffmpeg teardown failed: {cleanup}"
                )),
            }
        })();
        authorization.zeroize();
        provider_result
    };
    access_token.zeroize();
    result.map(|digest| ProviderStageEvidence {
        stage: PublishExecutionStage::Published,
        digest,
        detail: "YouTube observed the exact configured broadcast complete after exact full-media delivery on the active, healthy configured stream.".into(),
    })
}

/// The parent-owned deadline is the containment limit for the one-shot
/// process, including library initialization and teardown. It never exceeds
/// one day even when a malformed or future caller supplies a larger duration.
fn bounded_worker_runtime(media_duration_ms: u64) -> Duration {
    Duration::from_millis(media_duration_ms)
        .saturating_mul(2)
        .saturating_add(ONE_SHOT_WORKER_RUNTIME_GRACE)
        .min(MAX_ONE_SHOT_WORKER_RUNTIME)
}

struct OAuthCredential {
    client_id: Zeroizing<String>,
    client_secret: Zeroizing<String>,
    refresh_token: Zeroizing<String>,
}

/// A parsed secret always owns zeroizing storage, including while Serde is
/// unwinding a partially-valid OAuth payload.
struct SecretText(Zeroizing<String>);

impl<'de> serde::Deserialize<'de> for SecretText {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        String::deserialize(deserializer).map(|value| Self(Zeroizing::new(value)))
    }
}

impl OAuthCredential {
    fn parse(bytes: &[u8]) -> Result<Self, String> {
        #[derive(serde::Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Wire {
            client_id: SecretText,
            client_secret: SecretText,
            refresh_token: SecretText,
        }
        let mut wire: Wire = serde_json::from_slice(bytes)
            .map_err(|_| "YouTube credential authority is malformed")?;
        let valid = [
            &wire.client_id.0,
            &wire.client_secret.0,
            &wire.refresh_token.0,
        ]
        .iter()
        .all(|secret| {
            !(secret.is_empty()
                || secret.len() > MAX_SECRET_BYTES
                || secret.chars().any(char::is_control))
        });
        if !valid {
            wire.client_id.0.zeroize();
            wire.client_secret.0.zeroize();
            wire.refresh_token.0.zeroize();
            return Err("YouTube credential authority is invalid or unbounded".into());
        }
        Ok(Self {
            client_id: wire.client_id.0,
            client_secret: wire.client_secret.0,
            refresh_token: wire.refresh_token.0,
        })
    }

    fn zeroize(&mut self) {
        self.client_id.zeroize();
        self.client_secret.zeroize();
        self.refresh_token.zeroize();
    }
}

impl Drop for OAuthCredential {
    fn drop(&mut self) {
        self.zeroize();
    }
}

struct OAuthAccessToken {
    value: Zeroizing<String>,
    refresh_at_monotonic_ms: i64,
}

impl OAuthAccessToken {
    fn authorization(&self) -> Zeroizing<String> {
        Zeroizing::new(format!("Bearer {}", self.value.as_str()))
    }

    fn needs_refresh(&self, now_monotonic_ms: i64) -> bool {
        now_monotonic_ms >= self.refresh_at_monotonic_ms
    }

    fn zeroize(&mut self) {
        self.value.zeroize();
    }
}

impl Drop for OAuthAccessToken {
    fn drop(&mut self) {
        self.zeroize();
    }
}

fn refresh_authorization_if_due<R: RuntimeIo>(
    runtime: &mut R,
    credential: &OAuthCredential,
    access_token: &mut OAuthAccessToken,
    authorization: &mut Zeroizing<String>,
) -> Result<(), String> {
    if access_token.needs_refresh(runtime.now()?) {
        authorization.zeroize();
        *access_token = refresh_access_token(runtime, credential)?;
        *authorization = access_token.authorization();
    }
    Ok(())
}

fn refresh_access_token<R: RuntimeIo>(
    runtime: &mut R,
    credential: &OAuthCredential,
) -> Result<OAuthAccessToken, String> {
    let mut response = checked_response(
        request_and_zeroize(
            runtime,
            HttpRequest {
                endpoint: Endpoint::OAuthToken,
                authorization: None,
                form: vec![
                    (
                        Zeroizing::new("client_id".into()),
                        credential.client_id.clone(),
                    ),
                    (
                        Zeroizing::new("client_secret".into()),
                        credential.client_secret.clone(),
                    ),
                    (
                        Zeroizing::new("refresh_token".into()),
                        credential.refresh_token.clone(),
                    ),
                    (
                        Zeroizing::new("grant_type".into()),
                        Zeroizing::new("refresh_token".into()),
                    ),
                ],
            },
        )?,
        "YouTube OAuth refresh",
    )?;
    #[derive(serde::Deserialize)]
    #[serde(deny_unknown_fields)]
    struct Token {
        access_token: SecretText,
        expires_in: u64,
        token_type: String,
        #[serde(default)]
        scope: Option<SecretText>,
    }
    let parsed = serde_json::from_slice(&response.body);
    response.body.fill(0);
    let mut token: Token = parsed.map_err(|_| "YouTube OAuth response was malformed")?;
    if token.token_type != "Bearer"
        || token.access_token.0.is_empty()
        || token.access_token.0.len() > MAX_SECRET_BYTES
        || token.access_token.0.chars().any(char::is_control)
        || !(60..=86_400).contains(&token.expires_in)
    {
        token.access_token.0.zeroize();
        zero_string(&mut token.token_type);
        if let Some(scope) = &mut token.scope {
            scope.0.zeroize();
        }
        return Err("YouTube OAuth response was invalid or unbounded".into());
    }
    let access_token = token.access_token.0;
    zero_string(&mut token.token_type);
    if let Some(scope) = &mut token.scope {
        scope.0.zeroize();
    }
    let now_monotonic_ms = runtime.now()?;
    let expires_in_ms = i64::try_from(token.expires_in)
        .map_err(|_| "YouTube OAuth expiry is out of range")?
        .checked_mul(1_000)
        .ok_or("YouTube OAuth expiry is out of range")?;
    let refresh_after_ms = expires_in_ms
        .checked_sub(ACCESS_TOKEN_REFRESH_SKEW_MS.min(expires_in_ms / 2))
        .ok_or("YouTube OAuth expiry is out of range")?;
    let refresh_at_monotonic_ms = now_monotonic_ms
        .checked_add(refresh_after_ms)
        .ok_or("YouTube OAuth expiry is out of range")?;
    Ok(OAuthAccessToken {
        value: access_token,
        refresh_at_monotonic_ms,
    })
}

fn get_broadcast<R: RuntimeIo>(
    runtime: &mut R,
    broadcast_id: &str,
    authorization: &Zeroizing<String>,
) -> Result<YouTubeDocument, String> {
    let mut response = checked_response(
        request_and_zeroize(
            runtime,
            HttpRequest {
                endpoint: Endpoint::Broadcast {
                    id: broadcast_id.into(),
                },
                authorization: Some(authorization.clone()),
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
    authorization: &Zeroizing<String>,
) -> Result<YouTubeDocument, String> {
    let mut response = checked_response(
        request_and_zeroize(
            runtime,
            HttpRequest {
                endpoint: Endpoint::Stream {
                    id: stream_id.into(),
                },
                authorization: Some(authorization.clone()),
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

fn transition_broadcast_live<R: RuntimeIo>(
    runtime: &mut R,
    broadcast_id: &str,
    authorization: &Zeroizing<String>,
) -> Result<YouTubeDocument, String> {
    let mut response = checked_response(
        request_and_zeroize(
            runtime,
            HttpRequest {
                endpoint: Endpoint::TransitionLive {
                    id: broadcast_id.into(),
                },
                authorization: Some(authorization.clone()),
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

fn transition_broadcast_complete<R: RuntimeIo>(
    runtime: &mut R,
    broadcast_id: &str,
    authorization: &Zeroizing<String>,
) -> Result<YouTubeDocument, String> {
    let mut response = checked_response(
        request_and_zeroize(
            runtime,
            HttpRequest {
                endpoint: Endpoint::TransitionComplete {
                    id: broadcast_id.into(),
                },
                authorization: Some(authorization.clone()),
                form: Vec::new(),
            },
        )?,
        "YouTube broadcast completion transition",
    )?;
    YouTubeDocument::parse(
        std::mem::take(&mut response.body),
        "YouTube completion transition response",
    )
}

fn checked_response(mut response: HttpResponse, label: &str) -> Result<HttpResponse, String> {
    if response.location.is_some() || (300..400).contains(&response.status) {
        response.body.fill(0);
        return Err(format!(
            "{label} redirected; authorization was not forwarded"
        ));
    }
    if response.status == 403 && youtube_retryable_forbidden(&response.body) {
        response.body.fill(0);
        return Err(format!("{label} hit a provider quota or retryable failure"));
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

fn youtube_retryable_forbidden(body: &[u8]) -> bool {
    #[derive(serde::Deserialize)]
    struct ErrorEntry {
        reason: String,
    }
    #[derive(serde::Deserialize)]
    struct ErrorBody {
        errors: Vec<ErrorEntry>,
    }
    #[derive(serde::Deserialize)]
    struct Document {
        error: ErrorBody,
    }
    let Ok(mut document) = serde_json::from_slice::<Document>(body) else {
        return false;
    };
    let retryable = document.error.errors.iter().any(|error| {
        matches!(
            error.reason.as_str(),
            "rateLimitExceeded" | "quotaExceeded" | "concurrentBroadcasts"
        )
    });
    for error in &mut document.error.errors {
        zero_string(&mut error.reason);
    }
    retryable
}

fn request_and_zeroize<R: RuntimeIo>(
    runtime: &mut R,
    mut request: HttpRequest,
) -> Result<HttpResponse, String> {
    let result = runtime.request(&mut request);
    if let Some(authorization) = &mut request.authorization {
        authorization.zeroize();
    }
    for (name, value) in &mut request.form {
        name.zeroize();
        value.zeroize();
    }
    result
}

impl Drop for HttpRequest {
    fn drop(&mut self) {
        if let Some(authorization) = &mut self.authorization {
            authorization.zeroize();
        }
        for (name, value) in &mut self.form {
            name.zeroize();
            value.zeroize();
        }
    }
}

struct YouTubeDocument {
    value: serde_json::Value,
    response_sha256: String,
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
        let response_sha256 = format!("{:x}", Sha256::digest(&body));
        let parsed = serde_json::from_slice(&body);
        body.fill(0);
        let value = parsed.map_err(|_| format!("{label} was malformed"))?;
        Ok(Self {
            value,
            response_sha256,
        })
    }

    fn digest(&self) -> String {
        self.response_sha256.clone()
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

fn require_broadcast_lifecycle(
    document: &YouTubeDocument,
    channel_id: &str,
    broadcast_id: &str,
    stream_id: &str,
    privacy_status: YouTubePrivacyStatus,
    lifecycle: &str,
) -> Result<(), String> {
    require_broadcast_binding(
        document,
        channel_id,
        broadcast_id,
        stream_id,
        privacy_status,
        false,
    )?;
    if document
        .only_item("YouTube broadcast")?
        .pointer("/status/lifeCycleStatus")
        .and_then(serde_json::Value::as_str)
        != Some(lifecycle)
    {
        return Err("YouTube broadcast did not reach the exact required lifecycle".into());
    }
    Ok(())
}

fn validate_worker_delivery(
    delivery: &WorkerDelivery,
    job: &PublishExecutionJob,
) -> Result<(), String> {
    if delivery.schema_version != 1
        || delivery.outcome != "published"
        || delivery.failure_code.is_some()
        || delivery.media_bytes != job.media_bytes
        || delivery.media_sha256 != job.media_sha256
    {
        return Err("YouTube media worker did not prove exact full-media delivery".into());
    }
    Ok(())
}

struct Ingestion {
    address: String,
    stream_key: Zeroizing<String>,
}

impl Ingestion {
    fn zeroize(&mut self) {
        zero_string(&mut self.address);
        self.stream_key.zeroize();
    }
}

impl Drop for Ingestion {
    fn drop(&mut self) {
        self.zeroize();
    }
}

fn require_stream_binding(
    document: &mut YouTubeDocument,
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
    let health = item
        .pointer("/status/healthStatus/status")
        .and_then(serde_json::Value::as_str);
    if require_active && health != Some("good") {
        return Err("YouTube active stream is not in good provider health".into());
    }
    if require_active {
        let issues = item
            .pointer("/status/healthStatus/configurationIssues")
            .and_then(serde_json::Value::as_array)
            .ok_or_else(|| {
                "YouTube active stream health omitted its configuration issues".to_string()
            })?;
        if !issues.is_empty() {
            return Err("YouTube active stream reports configuration issues".into());
        }
    }
    let address = item
        .pointer("/cdn/ingestionInfo/rtmpsIngestionAddress")
        .and_then(serde_json::Value::as_str)
        .ok_or_else(|| "YouTube stream is missing a provider-issued RTMPS address".to_string())?
        .to_owned();
    let stream_key = item
        .pointer("/cdn/ingestionInfo/streamName")
        .and_then(serde_json::Value::as_str)
        .ok_or_else(|| "YouTube stream is missing a provider-issued stream key".to_string())?;
    if address != "rtmps://a.rtmp.youtube.com:443/live2"
        || address.len() > 512
        || address.chars().any(char::is_control)
        || stream_key.is_empty()
        || stream_key.len() > 512
        || stream_key.chars().any(char::is_control)
    {
        return Err("YouTube provider-issued RTMPS ingestion authority is invalid".into());
    }
    let stream_key = match document
        .value
        .pointer_mut("/items/0/cdn/ingestionInfo/streamName")
    {
        Some(serde_json::Value::String(value)) => Zeroizing::new(std::mem::take(value)),
        _ => return Err("YouTube stream is missing a provider-issued stream key".into()),
    };
    Ok(Ingestion {
        address,
        stream_key,
    })
}

#[allow(clippy::too_many_arguments)]
fn poll_stream_active<R: RuntimeIo>(
    runtime: &mut R,
    process: &mut dyn RunningFfmpeg,
    channel_id: &str,
    stream_id: &str,
    credential: &OAuthCredential,
    access_token: &mut OAuthAccessToken,
    authorization: &mut Zeroizing<String>,
    heartbeat: &mut dyn FnMut() -> Result<(), String>,
) -> Result<YouTubeDocument, String> {
    let deadline = runtime
        .now()?
        .checked_add(STREAM_ACTIVE_TIMEOUT.as_millis() as i64)
        .ok_or_else(|| "YouTube stream activation deadline overflowed".to_string())?;
    loop {
        heartbeat()?;
        if process.try_wait()?.is_some() {
            return Err("YouTube ffmpeg stopped before the configured stream became active".into());
        }
        refresh_authorization_if_due(runtime, credential, access_token, authorization)?;
        let mut document = get_stream(runtime, stream_id, authorization)?;
        let state = require_stream_binding(&mut document, channel_id, stream_id, true);
        match state {
            Ok(_) => return Ok(document),
            Err(error) if error == "YouTube stream is not active yet" => document.zeroize(),
            Err(error) => {
                document.zeroize();
                return Err(error);
            }
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
    credential: &OAuthCredential,
    access_token: &mut OAuthAccessToken,
    authorization: &mut Zeroizing<String>,
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
        refresh_authorization_if_due(runtime, credential, access_token, authorization)?;
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

#[allow(clippy::too_many_arguments)]
fn poll_broadcast_complete<R: RuntimeIo>(
    runtime: &mut R,
    channel_id: &str,
    broadcast_id: &str,
    stream_id: &str,
    privacy_status: YouTubePrivacyStatus,
    credential: &OAuthCredential,
    access_token: &mut OAuthAccessToken,
    authorization: &mut Zeroizing<String>,
    heartbeat: &mut dyn FnMut() -> Result<(), String>,
) -> Result<YouTubeDocument, String> {
    let deadline = runtime
        .now()?
        .checked_add(BROADCAST_COMPLETE_TIMEOUT.as_millis() as i64)
        .ok_or_else(|| "YouTube completion observation deadline overflowed".to_string())?;
    loop {
        heartbeat()?;
        refresh_authorization_if_due(runtime, credential, access_token, authorization)?;
        let mut document = get_broadcast(runtime, broadcast_id, authorization)?;
        match require_broadcast_lifecycle(
            &document,
            channel_id,
            broadcast_id,
            stream_id,
            privacy_status,
            "complete",
        ) {
            Ok(()) => return Ok(document),
            Err(error)
                if error == "YouTube broadcast did not reach the exact required lifecycle" =>
            {
                document.zeroize();
            }
            Err(error) => {
                document.zeroize();
                return Err(error);
            }
        }
        if runtime.now()? >= deadline {
            return Err("YouTube broadcast did not complete before its bounded deadline".into());
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
    // This validates the complete active application before selecting the
    // already-required channel worker from that exact signed package manifest.
    let _authority =
        station_maintenance::read_verified_install_authority(data_root, application_root)
            .map_err(|_| "YouTube installed candidate authority is unavailable")?;
    let active = station_maintenance::read_active_slot_receipt(
        &data_root.join(station_maintenance::ACTIVE_SLOT_RECEIPT_NAME),
    )?;
    let active_directory = active.active.directory(application_root)?;
    let worker = active
        .active
        .package_manifest
        .files
        .iter()
        .find(|file| file.path == "channel-worker.exe")
        .ok_or_else(|| "YouTube installed package lacks its media worker".to_string())?;
    let sha256 = worker.sha256.clone();
    let executable = active_directory.join("channel-worker.exe");
    if !executable.starts_with(application_root) {
        return Err("YouTube media worker escaped the application root".into());
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
    started: Instant,
    gstreamer_runtime: PathBuf,
}

impl SystemRuntimeIo {
    fn new(application_root: &Path) -> Self {
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
            started: Instant::now(),
            gstreamer_runtime: application_root.join("runtime").join("gstreamer"),
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
                let mut form = encode_secret_form(&request.form);
                let result = self
                    .agent
                    .post(&url)
                    .header("Content-Type", "application/x-www-form-urlencoded")
                    .send(form.as_slice())
                    .map_err(|_| "YouTube OAuth refresh transport failed");
                form.fill(0);
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
            Endpoint::TransitionLive { .. } | Endpoint::TransitionComplete { .. } => self
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
        command: &mut WorkerCommand,
    ) -> Result<Box<dyn RunningFfmpeg>, String> {
        SystemFfmpegProcess::spawn(executable, command, &self.gstreamer_runtime)
            .map(|process| Box::new(process) as Box<dyn RunningFfmpeg>)
    }

    fn now(&mut self) -> Result<i64, String> {
        self.started
            .elapsed()
            .as_millis()
            .try_into()
            .map_err(|_| "YouTube monotonic runtime exceeded its bound".into())
    }

    fn sleep(&mut self, duration: Duration) {
        std::thread::sleep(duration);
    }
}

fn encode_secret_form(fields: &[(Zeroizing<String>, Zeroizing<String>)]) -> Vec<u8> {
    let mut encoded = Vec::new();
    for (index, (name, value)) in fields.iter().enumerate() {
        if index != 0 {
            encoded.push(b'&');
        }
        encode_form_bytes(name.as_bytes(), &mut encoded);
        encoded.push(b'=');
        encode_form_bytes(value.as_bytes(), &mut encoded);
    }
    encoded
}

fn encode_form_bytes(value: &[u8], output: &mut Vec<u8>) {
    const HEX: &[u8; 16] = b"0123456789ABCDEF";
    for &byte in value {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b'~') {
            output.push(byte);
        } else {
            output.extend_from_slice(&[b'%', HEX[(byte >> 4) as usize], HEX[(byte & 15) as usize]]);
        }
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
        Endpoint::TransitionLive { id } => Ok(format!(
            "https://www.googleapis.com/youtube/v3/liveBroadcasts/transition?broadcastStatus=live&id={}&part=status,snippet,contentDetails",
            form_component(id)
        )),
        Endpoint::TransitionComplete { id } => Ok(format!(
            "https://www.googleapis.com/youtube/v3/liveBroadcasts/transition?broadcastStatus=complete&id={}&part=status,snippet,contentDetails",
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
    stdout: Option<std::process::ChildStdout>,
}

impl SystemFfmpegProcess {
    fn spawn(
        executable: &Path,
        command: &mut WorkerCommand,
        gstreamer_runtime: &Path,
    ) -> Result<Self, String> {
        let job = WindowsProcessTree::new()?;
        let plugin_path = gstreamer_runtime.join("lib").join("gstreamer-1.0");
        let mut path = gstreamer_runtime.join("bin").into_os_string();
        path.push(";");
        path.push(std::env::var_os("PATH").unwrap_or_default());
        let mut child = Command::new(executable)
            .arg("--youtube-publish-worker")
            .env("PATH", path)
            .env("GST_PLUGIN_PATH_1_0", &plugin_path)
            .env("GST_PLUGIN_SYSTEM_PATH_1_0", &plugin_path)
            .env("GST_DEBUG", "0")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|_| "YouTube ffmpeg process could not be started")?;
        if let Err(error) = job.assign(&child) {
            let _ = child.kill();
            let _ = child.wait();
            return Err(error);
        }
        let mut stdin = child.stdin.take().ok_or_else(|| {
            job.terminate();
            let _ = child.wait();
            "YouTube publisher stdin pipe was unavailable"
        })?;
        let mut encoded = match serde_json::to_vec(command) {
            Ok(encoded) => encoded,
            Err(_) => {
                job.terminate();
                let _ = child.wait();
                return Err("YouTube publisher command could not be encoded".into());
            }
        };
        let write = stdin.write_all(&encoded).and_then(|_| stdin.flush());
        encoded.fill(0);
        drop(stdin);
        if write.is_err() {
            job.terminate();
            let _ = child.wait();
            return Err("YouTube publisher command could not be delivered".into());
        }
        let stdout = child.stdout.take().ok_or_else(|| {
            job.terminate();
            let _ = child.wait();
            "YouTube publisher stdout pipe was unavailable"
        })?;
        Ok(Self {
            child,
            job,
            stdout: Some(stdout),
        })
    }

    fn read_result(&mut self) -> Result<Vec<u8>, String> {
        let stdout = self
            .stdout
            .take()
            .ok_or_else(|| "YouTube publisher result was already consumed".to_string())?;
        drain_bytes(stdout, WORKER_RESULT_BYTES)
    }
}

impl RunningFfmpeg for SystemFfmpegProcess {
    fn try_wait(&mut self) -> Result<Option<bool>, String> {
        self.child
            .try_wait()
            .map(|status| status.map(|value| value.success()))
            .map_err(|_| "YouTube ffmpeg status could not be read".into())
    }

    fn wait_for_delivery(
        &mut self,
        maximum_runtime: Duration,
        heartbeat: &mut dyn FnMut() -> Result<(), String>,
    ) -> Result<WorkerDelivery, String> {
        let deadline = Instant::now() + maximum_runtime;
        let success = loop {
            match self.child.try_wait() {
                Ok(Some(status)) => break status.success(),
                Ok(None) if Instant::now() < deadline => {
                    if let Err(heartbeat_error) = heartbeat() {
                        let teardown = self.terminate_and_join(&mut || Ok(()));
                        return Err(match teardown {
                            Ok(()) => heartbeat_error,
                            Err(teardown_error) => {
                                format!("{heartbeat_error}; {teardown_error}")
                            }
                        });
                    }
                    std::thread::sleep(Duration::from_millis(100));
                }
                Ok(None) => {
                    self.terminate_and_join(heartbeat)?;
                    return Err("YouTube publisher exceeded its bounded media deadline".into());
                }
                Err(_) => {
                    self.terminate_and_join(heartbeat)?;
                    return Err("YouTube publisher status could not be read".into());
                }
            }
        };
        let mut output = self.read_result()?;
        let parsed = serde_json::from_slice::<WorkerDelivery>(&output)
            .map_err(|_| "YouTube publisher returned a malformed result".to_string());
        output.fill(0);
        let delivery = parsed?;
        if !success {
            let safe_code = delivery.failure_code.as_deref().is_some_and(|code| {
                !code.is_empty()
                    && code.len() <= 100
                    && code
                        .bytes()
                        .all(|byte| byte.is_ascii_lowercase() || byte == b'_')
            });
            return Err(if safe_code {
                "YouTube publisher failed before exact full-media delivery".into()
            } else {
                "YouTube publisher failed with an invalid result".into()
            });
        }
        Ok(delivery)
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
                    self.job.terminate();
                    let _ = self.child.kill();
                    if self.child.wait().is_err() {
                        return Err("YouTube publisher could not be definitively reaped".into());
                    }
                    wait_error.get_or_insert_with(|| {
                        "YouTube publisher exceeded its bounded teardown deadline".into()
                    });
                    break;
                }
                Err(_) => {
                    self.job.terminate();
                    let _ = self.child.kill();
                    self.child
                        .wait()
                        .map_err(|_| "YouTube publisher could not be definitively reaped")?;
                    return Err("YouTube publisher status could not be read".into());
                }
            }
        }
        let result = self.read_result();
        match (wait_error, result) {
            (None, Ok(mut output)) => {
                output.fill(0);
                Ok(())
            }
            (Some(wait), Ok(mut output)) => {
                output.fill(0);
                Err(wait)
            }
            (None, Err(result)) => Err(result),
            (Some(wait), Err(result)) => Err(format!("{wait}; {result}")),
        }
    }
}

fn drain_bytes<T: Read>(stream: T, maximum: usize) -> Result<Vec<u8>, String> {
    let mut output = Vec::new();
    if stream
        .take((maximum + 1) as u64)
        .read_to_end(&mut output)
        .is_err()
    {
        output.fill(0);
        return Err("YouTube publisher result could not be read".into());
    }
    if output.len() > maximum {
        output.fill(0);
        return Err("YouTube publisher result exceeded its bound".into());
    }
    Ok(output)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;
    use std::sync::{
        Arc,
        atomic::{AtomicBool, AtomicI64, AtomicU64, Ordering},
    };

    struct FakeProcess {
        terminated: Arc<AtomicBool>,
        maximum_runtime_ms: Arc<AtomicU64>,
        delivery_finished_at_ms: Option<(Arc<AtomicI64>, i64)>,
    }

    impl RunningFfmpeg for FakeProcess {
        fn try_wait(&mut self) -> Result<Option<bool>, String> {
            Ok(None)
        }
        fn wait_for_delivery(
            &mut self,
            maximum_runtime: Duration,
            _: &mut dyn FnMut() -> Result<(), String>,
        ) -> Result<WorkerDelivery, String> {
            self.maximum_runtime_ms
                .store(maximum_runtime.as_millis() as u64, Ordering::Release);
            if let Some((clock, finished_at_ms)) = &self.delivery_finished_at_ms {
                clock.store(*finished_at_ms, Ordering::Release);
            }
            Ok(WorkerDelivery {
                schema_version: 1,
                outcome: "published".into(),
                media_bytes: 1,
                media_sha256: "c".repeat(64),
                failure_code: None,
            })
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
        now: Arc<AtomicI64>,
        executable: Option<PathBuf>,
        worker_command_seen: bool,
        terminated: Arc<AtomicBool>,
        worker_maximum_runtime_ms: Arc<AtomicU64>,
        worker_delivery_finished_at_ms: Option<i64>,
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
            command: &mut WorkerCommand,
        ) -> Result<Box<dyn RunningFfmpeg>, String> {
            self.executable = Some(executable.to_path_buf());
            self.worker_command_seen = command.schema_version == 1
                && command.expected_duration_ms == 1_000
                && Path::new(&command.managed_media_root).is_absolute();
            Ok(Box::new(FakeProcess {
                terminated: Arc::clone(&self.terminated),
                maximum_runtime_ms: Arc::clone(&self.worker_maximum_runtime_ms),
                delivery_finished_at_ms: self
                    .worker_delivery_finished_at_ms
                    .map(|finished_at_ms| (Arc::clone(&self.now), finished_at_ms)),
            }))
        }
        fn now(&mut self) -> Result<i64, String> {
            Ok(self.now.load(Ordering::Acquire))
        }
        fn sleep(&mut self, duration: Duration) {
            self.now
                .fetch_add(duration.as_millis() as i64, Ordering::AcqRel);
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
            media_duration_ms: 1_000,
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
            serde_json::json!({"items":[{"id":"stream","snippet":{"channelId":"channel"},"cdn":{"ingestionType":"rtmp","ingestionInfo":{"rtmpsIngestionAddress":"rtmps://a.rtmp.youtube.com:443/live2","streamName":"never-durable-key"}},"status":{"streamStatus":status,"healthStatus":{"status":"good","configurationIssues":[]}}}]}),
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
            now: Arc::new(AtomicI64::new(0)),
            executable: None,
            worker_command_seen: false,
            terminated: Arc::new(AtomicBool::new(false)),
            worker_maximum_runtime_ms: Arc::new(AtomicU64::new(0)),
            worker_delivery_finished_at_ms: None,
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
            media.parent().expect("test media has a managed root"),
            br#"{"client_id":"id","client_secret":"secret","refresh_token":"refresh"}"#,
            &ffmpeg(),
            &mut || Ok(()),
        )
    }

    #[test]
    fn one_shot_worker_runtime_is_hard_capped_by_the_controller() {
        assert_eq!(
            bounded_worker_runtime(24 * 60 * 60 * 1_000),
            Duration::from_secs(24 * 60 * 60),
        );
        assert_eq!(bounded_worker_runtime(60_000), Duration::from_secs(12 * 60),);
    }

    #[test]
    fn one_shot_launch_has_no_product_diagnostic_egress() {
        let source = include_str!("youtube_live_runtime.rs");
        let stderr_discard = [".stderr", "(Stdio::null())"].concat();
        let gstreamer_quiet = [".env(\"GST_DEBUG\", ", "\"0\")"].concat();
        let kill_on_close = ["JOB_OBJECT_LIMIT_", "KILL_ON_JOB_CLOSE"].concat();

        assert!(source.contains(&stderr_discard));
        assert!(source.contains(&gstreamer_quiet));
        assert!(source.contains(&kill_on_close));
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
            broadcast("complete"),
            broadcast("complete"),
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
            media.parent().expect("test media has a managed root"),
            br#"{"client_id":"id","client_secret":"secret","refresh_token":"refresh"}"#,
            &ffmpeg(),
            &mut || {
                beats += 1;
                Ok(())
            },
        );
        let _ = fs::remove_file(media);
        let result = result.unwrap();
        assert_eq!(result.stage, PublishExecutionStage::Published);
        assert!(runtime.worker_command_seen);
        assert_eq!(
            runtime.worker_maximum_runtime_ms.load(Ordering::Acquire),
            602_000,
        );
        assert!(runtime.requests[0].0.contains("OAuthToken") && !runtime.requests[0].1);
        assert!(
            runtime
                .requests
                .iter()
                .skip(1)
                .all(|(_, authorized)| *authorized)
        );
        assert!(!format!("{result:?}").contains("never-durable-key"));
        assert!(!runtime.terminated.load(Ordering::Acquire));
    }

    #[test]
    fn published_evidence_binds_the_post_terminal_observation_time() {
        let media = std::env::temp_dir().join("townlight-youtube-runtime-observation-time.mp4");
        fs::write(&media, [0]).unwrap();
        let responses = || {
            vec![
                response(
                    serde_json::json!({"access_token":"test-access","expires_in":3600,"token_type":"Bearer"}),
                ),
                broadcast("ready"),
                stream("ready"),
                stream("active"),
                broadcast("live"),
                broadcast("live"),
                broadcast("complete"),
                broadcast("complete"),
            ]
        };
        let mut first = runtime(responses());
        let first_result = invoke(&mut first, &media).unwrap();
        let mut later = runtime(responses());
        later.now.store(1, Ordering::Release);
        let later_result = invoke(&mut later, &media).unwrap();
        let _ = fs::remove_file(media);

        assert_ne!(first_result.digest, later_result.digest);
    }

    #[test]
    fn long_delivery_refreshes_access_before_completion() {
        let media = std::env::temp_dir().join("townlight-youtube-runtime-refresh.mp4");
        fs::write(&media, [0]).unwrap();
        let mut fake = runtime(vec![
            response(
                serde_json::json!({"access_token":"test-access","expires_in":60,"token_type":"Bearer"}),
            ),
            broadcast("ready"),
            stream("ready"),
            stream("active"),
            broadcast("live"),
            broadcast("live"),
            response(
                serde_json::json!({"access_token":"refreshed-access","expires_in":3600,"token_type":"Bearer"}),
            ),
            broadcast("complete"),
            broadcast("complete"),
        ]);
        fake.worker_delivery_finished_at_ms = Some(60_000);

        let result = invoke(&mut fake, &media);

        let _ = fs::remove_file(media);
        assert_eq!(result.unwrap().stage, PublishExecutionStage::Published);
        assert_eq!(
            fake.requests
                .iter()
                .filter(|(endpoint, _)| endpoint.contains("OAuthToken"))
                .count(),
            2
        );
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
