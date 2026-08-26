//! Bounded process and receiver adapters for the durable transport-proof authority.
//!
//! This layer never searches `PATH`, never constructs a shell command, and never
//! turns process output into persisted evidence.  Only parsed bounded facts and
//! digests reach the append-only authority in `station-storage`.

use std::fs::{self, File};
use std::io::Read;
use std::os::windows::fs::MetadataExt;
use std::os::windows::io::AsRawHandle;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStderr, ChildStdout, Command, Stdio};
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use std::time::{Duration, Instant};

use sha2::{Digest, Sha256};
use station_storage::{
    ProofOutcome, StationStore, TargetDeviceConfig, TargetDeviceReachabilityReceipt,
    TransportAnalysisReceipt, TransportFindings, TransportProofCaptureFacts,
};
use station_transport_proof::{MAX_PROOF_CAPTURE_BYTES, MAX_PROOF_DURATION_MS, TargetProtocol};
use windows_sys::Win32::Foundation::{CloseHandle, HANDLE, INVALID_HANDLE_VALUE};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JobObjectExtendedLimitInformation,
    SetInformationJobObject, TerminateJobObject,
};

const MAX_PROCESS_OUTPUT_BYTES: usize = 64 * 1024;
const MAX_RECEIVER_RESPONSE_BYTES: usize = 8 * 1024;
const MAX_ANALYSIS_ERRORS: u64 = 0;
const TSDUCK_ANALYZER_ID: &str = "tsduck-tsanalyze";
const RECEIVER_ADAPTER_ID: &str = "townlight-https-receiver-v1";

/// A capture namespace exclusively owned by stationd.  It rejects reparse
/// points from its root through the per-job directory and exposes no API for a
/// caller to nominate an absolute artifact path.
#[derive(Debug, Clone)]
pub struct ManagedCaptureRoot {
    root: PathBuf,
}

impl ManagedCaptureRoot {
    pub fn open(root: impl AsRef<Path>) -> Result<Self, String> {
        let root = root.as_ref();
        if !root.is_absolute() {
            return Err("transport artifact root must be absolute".into());
        }
        create_non_reparse_directory_chain(root)?;
        let root = root.to_path_buf();
        ensure_non_reparse_ancestry(&root)?;
        Ok(Self { root })
    }

    pub fn begin_job(&self, job_id: &str) -> Result<PathBuf, String> {
        if !is_canonical_uuid(job_id) {
            return Err("transport capture job identity is invalid".into());
        }
        ensure_non_reparse_ancestry(&self.root)?;
        let job = self.root.join(job_id);
        if job.exists() {
            ensure_non_reparse_ancestry(&job)?;
            let nonce = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map_err(|_| "transport artifact clock is unavailable")?
                .as_nanos();
            let quarantine = self.root.join(format!("{job_id}.quarantine-{nonce}"));
            fs::rename(&job, &quarantine)
                .map_err(|_| "stale transport capture could not be quarantined")?;
            ensure_non_reparse_ancestry(&quarantine)?;
        }
        fs::create_dir(&job).map_err(|_| "transport capture job directory could not be created")?;
        ensure_non_reparse_ancestry(&job)?;
        Ok(job.join("capture.partial.ts"))
    }

    /// Hashes the closed partial capture, then atomically promotes it to the
    /// only analysis name.  The returned bytes are retained by the controller
    /// for analysis, so no later pathname substitution can affect findings.
    pub fn seal_capture(
        &self,
        job_id: &str,
        started_at_unix_ms: i64,
        ended_at_unix_ms: i64,
        max_capture_bytes: u64,
    ) -> Result<(TransportProofCaptureFacts, Vec<u8>), String> {
        if !is_canonical_uuid(job_id) {
            return Err("transport capture job identity is invalid".into());
        }
        let job = self.root.join(job_id);
        ensure_non_reparse_ancestry(&job)?;
        let partial = job.join("capture.partial.ts");
        let final_path = job.join("capture.ts");
        if final_path.exists() {
            return Err("transport capture has already been sealed".into());
        }
        ensure_non_reparse_ancestry(&partial)?;
        let bytes = bounded_capture(&partial, max_capture_bytes)?;
        let facts = TransportProofCaptureFacts {
            artifact_relative_path: format!("{job_id}/capture.ts"),
            capture_bytes: u64::try_from(bytes.len())
                .map_err(|_| "transport capture size overflowed")?,
            capture_sha256: sha256(&bytes),
            started_at_unix_ms,
            ended_at_unix_ms,
        };
        facts.validate(max_capture_bytes)?;
        fs::rename(&partial, &final_path)
            .map_err(|_| "transport capture could not be atomically sealed")?;
        ensure_non_reparse_ancestry(&final_path)?;
        Ok((facts, bytes))
    }
}

/// Service-owned durable queue supervisor.  Each claim is revalidated against
/// the verified installed candidate and current output configuration before a
/// managed capture can start; every fault becomes a durable failure, never a
/// fabricated pass.
pub fn run_transport_proof_queue_controller(
    database: &Path,
    application_root: &Path,
    stop: Arc<AtomicBool>,
) -> Result<(), String> {
    while !stop.load(Ordering::Acquire) {
        let now = unix_time_millis()?;
        let store = StationStore::open(database)?;
        store.recover_expired_transport_proof_jobs(now)?;
        if let Some(job) = store.claim_next_transport_proof_job(now, 900_000)? {
            let result = execute_claimed_transport_job(&store, database, application_root, &job);
            if let Err(_) = result {
                // Details are deliberately static: process output, targets, and
                // credentials never enter durable evidence or error receipts.
                let _ = store.fail_transport_proof_job(
                    &job.job.job_id,
                    unix_time_millis().unwrap_or(now),
                    "Transport proof execution failed before a passing receipt could be produced.",
                );
            }
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    Ok(())
}

fn execute_claimed_transport_job(
    store: &StationStore,
    database: &Path,
    application_root: &Path,
    job: &station_storage::TransportProofJobRecord,
) -> Result<(), String> {
    let data_directory = database
        .parent()
        .ok_or("transport proof data directory is unavailable")?;
    let candidate =
        station_maintenance::read_verified_candidate_identity(data_directory, application_root)?;
    if candidate.version != job.job.candidate_version
        || candidate.source_commit != job.job.source_commit
    {
        return Err("transport proof candidate changed after request".into());
    }
    let snapshot = store
        .read_channel_loudness_proof_snapshot(&job.job.channel_id)?
        .ok_or("transport proof channel is unavailable")?;
    if snapshot.configuration_hash != job.job.configuration_hash {
        return Err("transport proof configuration changed after request".into());
    }
    let sink = snapshot
        .output_sinks
        .into_iter()
        .find(|sink| sink.sink_id == job.job.sink_id && sink.enabled)
        .ok_or("transport proof sink is unavailable")?;
    let (host, port) = parse_udp_destination(&sink.destination)?;
    let root = ManagedCaptureRoot::open(data_directory.join("artifacts").join("transport-proof"))?;
    let partial = root.begin_job(&job.job.job_id)?;
    let runtime = AttestedTsdDuckRuntime::from_verified_install(data_directory, application_root)?;
    let executor = TsdDuckTransportExecutor {
        runtime,
        runner: RealProcessRunner,
    };
    let capture = CaptureRequest {
        udp_host: host,
        udp_port: port,
        duration_ms: MAX_PROOF_DURATION_MS,
        max_bytes: job.job.max_capture_bytes,
        artifact_path: partial,
    };
    let started = unix_time_millis()?;
    executor.capture(&capture)?;
    let ended = unix_time_millis()?;
    let (facts, _) =
        root.seal_capture(&job.job.job_id, started, ended, job.job.max_capture_bytes)?;
    let analyzing =
        store.begin_transport_proof_analysis(&job.job.job_id, &facts, ended, 300_000)?;
    let final_capture = root.root.join(&facts.artifact_relative_path);
    if sha256(&bounded_capture(&final_capture, job.job.max_capture_bytes)?) != facts.capture_sha256
    {
        return Err("sealed transport capture hash changed before analysis".into());
    }
    let findings = executor.analyze_capture(&CaptureRequest {
        artifact_path: final_capture,
        ..capture
    })?;
    if sha256(&bounded_capture(
        &root.root.join(&facts.artifact_relative_path),
        job.job.max_capture_bytes,
    )?) != facts.capture_sha256
    {
        return Err("sealed transport capture hash changed during analysis".into());
    }
    let recorded_at_unix_ms = unix_time_millis()?;
    let receipt = TransportAnalysisReceipt {
        proof_id: analyzing.job.job_id.clone(),
        channel_id: analyzing.job.channel_id.clone(),
        sink_id: analyzing.job.sink_id.clone(),
        target_id: analyzing.job.target_id.clone(),
        target_revision: analyzing.job.target_revision,
        candidate_version: analyzing.job.candidate_version.clone(),
        source_commit: analyzing.job.source_commit.clone(),
        configuration_hash: analyzing.job.configuration_hash.clone(),
        analyzer_id: TSDUCK_ANALYZER_ID.into(),
        analyzer_version: executor.runtime.version.clone(),
        analyzer_sha256: executor.runtime.tsanalyze_sha256.clone(),
        outcome: ProofOutcome::Pass,
        findings,
        captured_at_unix_ms: ended,
        recorded_at_unix_ms,
        recorded_by: "stationd-transport-proof-controller".into(),
        detail: "Bounded TSDuck transport analysis passed.".into(),
    };
    store.complete_transport_proof_job_with_analysis(
        &analyzing.job.job_id,
        &receipt,
        recorded_at_unix_ms,
    )?;
    Ok(())
}

fn parse_udp_destination(value: &str) -> Result<(String, u16), String> {
    if value.len() > 253 || value.contains(['/', '\\', '?', '#', '@']) {
        return Err("transport sink destination is invalid".into());
    }
    let (host, port) = value
        .rsplit_once(':')
        .ok_or("transport sink destination has no port")?;
    let port = port
        .parse::<u16>()
        .map_err(|_| "transport sink port is invalid")?;
    if host.is_empty() || port == 0 {
        return Err("transport sink destination is invalid".into());
    }
    Ok((host.into(), port))
}

fn unix_time_millis() -> Result<i64, String> {
    let elapsed = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|_| "system clock is before the Unix epoch")?;
    i64::try_from(elapsed.as_millis())
        .map_err(|_| "system clock millisecond value overflowed".into())
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProofBinding {
    pub proof_id: String,
    pub channel_id: String,
    pub sink_id: String,
    pub candidate_version: String,
    pub source_commit: String,
    pub configuration_hash: String,
    pub recorded_by: String,
    pub now_unix_ms: i64,
}

impl ProofBinding {
    pub fn validate(&self) -> Result<(), String> {
        if self.proof_id.is_empty()
            || self.channel_id.is_empty()
            || self.sink_id.is_empty()
            || self.recorded_by.is_empty()
            || self.now_unix_ms < 0
            || self.source_commit.len() != 40
            || !is_sha256(&self.configuration_hash)
        {
            return Err("transport executor binding is incomplete or invalid".into());
        }
        Ok(())
    }
}

/// Identity is supplied by the already verified candidate authority.  The adapter
/// rehashes both executables at use time so an executable swap fails before spawn.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AttestedTsdDuckRuntime {
    root: PathBuf,
    version: String,
    tsp_sha256: String,
    tsanalyze_sha256: String,
    tstables_sha256: String,
}

impl AttestedTsdDuckRuntime {
    /// Production construction is deliberately rooted in the verified install
    /// receipt. Callers cannot nominate another root or executable identity.
    pub fn from_verified_install(
        data_directory: &Path,
        application_root: &Path,
    ) -> Result<Self, String> {
        let runtime =
            station_maintenance::read_verified_tsduck_runtime(data_directory, application_root)?;
        Ok(Self {
            root: runtime.root,
            version: runtime.version,
            tsp_sha256: runtime.tsp_sha256,
            tsanalyze_sha256: runtime.tsanalyze_sha256,
            tstables_sha256: runtime.tstables_sha256,
        })
    }

    pub fn verify(&self) -> Result<(PathBuf, PathBuf, PathBuf), String> {
        if self.version.is_empty()
            || !is_sha256(&self.tsp_sha256)
            || !is_sha256(&self.tsanalyze_sha256)
            || !is_sha256(&self.tstables_sha256)
        {
            return Err("attested TSDuck runtime identity is invalid".into());
        }
        let root = fs::canonicalize(&self.root)
            .map_err(|_| "attested TSDuck runtime root is unavailable")?;
        let tsp = root.join("bin").join("tsp.exe");
        let analyzer = root.join("bin").join("tsanalyze.exe");
        let tables = root.join("bin").join("tstables.exe");
        if !tsp.is_file()
            || !analyzer.is_file()
            || !tables.is_file()
            || hash_file(&tsp)? != self.tsp_sha256
            || hash_file(&analyzer)? != self.tsanalyze_sha256
            || hash_file(&tables)? != self.tstables_sha256
        {
            return Err(
                "attested TSDuck runtime executable identity changed or is incomplete".into(),
            );
        }
        Ok((tsp, analyzer, tables))
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CaptureRequest {
    pub udp_host: String,
    pub udp_port: u16,
    pub duration_ms: u64,
    pub max_bytes: u64,
    pub artifact_path: PathBuf,
}

impl CaptureRequest {
    pub fn validate(&self) -> Result<(), String> {
        if self.udp_host.is_empty()
            || self.udp_host.contains(['/', '\\', '?', '#', '@'])
            || self.udp_port == 0
            || self.duration_ms == 0
            || self.duration_ms > MAX_PROOF_DURATION_MS
            || self.max_bytes == 0
            || self.max_bytes > MAX_PROOF_CAPTURE_BYTES
        {
            return Err(
                "transport capture request is outside its bounded structured contract".into(),
            );
        }
        Ok(())
    }
}

mod sealed {
    pub trait Process {}
    pub trait Receiver {}
}
pub trait ProcessRunner: sealed::Process + Send + Sync {
    fn run(
        &self,
        executable: &Path,
        argv: &[String],
        timeout: Duration,
    ) -> Result<ProcessResult, String>;
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcessResult {
    pub status_success: bool,
    pub output: Vec<u8>,
}

/// The production runner receives an executable and an argv vector.  It does not
/// invoke a shell and drains stdout/stderr through a fixed-size evidence-free cap.
pub struct RealProcessRunner;
impl sealed::Process for RealProcessRunner {}
impl ProcessRunner for RealProcessRunner {
    fn run(
        &self,
        executable: &Path,
        argv: &[String],
        timeout: Duration,
    ) -> Result<ProcessResult, String> {
        let job = WindowsProcessTree::new()?;
        let mut child = Command::new(executable)
            .args(argv)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|_| "TSDuck process could not be spawned")?;
        job.assign(&child)?;
        let stdout = child
            .stdout
            .take()
            .ok_or("TSDuck stdout pipe was unavailable")?;
        let stderr = child
            .stderr
            .take()
            .ok_or("TSDuck stderr pipe was unavailable")?;
        let stdout_drain = std::thread::spawn(move || drain_stdout(stdout));
        let stderr_drain = std::thread::spawn(move || drain_stderr(stderr));
        let started = Instant::now();
        loop {
            if let Some(status) = child
                .try_wait()
                .map_err(|_| "TSDuck process status could not be read")?
            {
                let output = join_drain(stdout_drain, "stdout")?;
                join_drain(stderr_drain, "stderr")?;
                return Ok(ProcessResult {
                    status_success: status.success(),
                    output,
                });
            }
            if started.elapsed() >= timeout {
                job.terminate();
                let _ = child.wait();
                let _ = stdout_drain.join();
                let _ = stderr_drain.join();
                return Err("TSDuck process exceeded its deadline".into());
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    }
}

#[cfg(test)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FakeProcessRunner {
    pub result: Result<ProcessResult, String>,
}
#[cfg(test)]
impl sealed::Process for FakeProcessRunner {}
#[cfg(test)]
impl ProcessRunner for FakeProcessRunner {
    fn run(&self, _: &Path, _: &[String], _: Duration) -> Result<ProcessResult, String> {
        self.result.clone()
    }
}

pub struct TsdDuckTransportExecutor<R> {
    pub runtime: AttestedTsdDuckRuntime,
    pub runner: R,
}
impl<R: ProcessRunner> TsdDuckTransportExecutor<R> {
    pub fn analyze_capture(&self, capture: &CaptureRequest) -> Result<TransportFindings, String> {
        capture.validate()?;
        let (_, analyzer, tables) = self.runtime.verify()?;
        let bytes = bounded_capture(&capture.artifact_path, capture.max_bytes)?;
        let argv = vec![
            "--json".into(),
            "--deterministic".into(),
            "--error-analysis".into(),
            capture.artifact_path.to_string_lossy().into_owned(),
        ];
        let result =
            self.runner
                .run(&analyzer, &argv, Duration::from_millis(capture.duration_ms))?;
        if !result.status_success {
            return Err("TSDuck analysis process failed".into());
        }
        let findings =
            parse_tsanalyze_json(&result.output, bytes.len() as u64, capture.duration_ms)?;
        let (service_id, pmt_pid) = tsanalyze_service_binding(&result.output)?;
        let tables_output = capture.artifact_path.with_extension("tables.json");
        if tables_output.exists() {
            return Err("TSDuck tables output path already exists".into());
        }
        let tables_argv = vec![
            "--json-output".into(),
            tables_output.to_string_lossy().into_owned(),
            capture.artifact_path.to_string_lossy().into_owned(),
        ];
        let tables_result = self.runner.run(
            &tables,
            &tables_argv,
            Duration::from_millis(capture.duration_ms),
        )?;
        if !tables_result.status_success {
            return Err("TSDuck tables process failed".into());
        }
        let tables_bytes = bounded_capture(&tables_output, MAX_PROCESS_OUTPUT_BYTES as u64)?;
        let validation = validate_tstables_json(&tables_bytes, service_id, pmt_pid);
        let _ = fs::remove_file(&tables_output);
        validation?;
        Ok(findings)
    }

    pub fn capture_argv(&self, capture: &CaptureRequest) -> Result<Vec<String>, String> {
        capture.validate()?;
        self.runtime.verify()?;
        let max_packets = capture.max_bytes / 188;
        if max_packets == 0 {
            return Err("transport capture byte limit cannot contain one TS packet".into());
        }
        Ok(vec![
            "-I".into(),
            "ip".into(),
            format!("{}:{}", capture.udp_host, capture.udp_port),
            "-P".into(),
            "limit".into(),
            "--packet".into(),
            max_packets.to_string(),
            "-P".into(),
            "until".into(),
            "--seconds".into(),
            format!("{:.3}", capture.duration_ms as f64 / 1_000.0),
            "-O".into(),
            "file".into(),
            capture.artifact_path.to_string_lossy().into_owned(),
        ])
    }

    /// Capture terminates through TSDuck's packet and time plugins, with the
    /// process deadline as an independent fail-closed supervisor.
    pub fn capture(&self, capture: &CaptureRequest) -> Result<(), String> {
        capture.validate()?;
        if capture.artifact_path.exists() {
            return Err("transport capture artifact already exists".into());
        }
        let (tsp, _, _) = self.runtime.verify()?;
        let argv = self.capture_argv(capture)?;
        let result = self.runner.run(
            &tsp,
            &argv,
            Duration::from_millis(capture.duration_ms.saturating_add(5_000)),
        )?;
        if !result.status_success {
            return Err("TSDuck capture process failed".into());
        }
        bounded_capture(&capture.artifact_path, capture.max_bytes).map(|_| ())
    }

    pub fn persist_analysis(
        &self,
        store: &StationStore,
        binding: &ProofBinding,
        target: &TargetDeviceConfig,
        capture: &CaptureRequest,
    ) -> Result<(), String> {
        binding.validate()?;
        target.validate()?;
        let findings = self.analyze_capture(capture)?;
        let (_, analyzer, _) = self.runtime.verify()?;
        let receipt = TransportAnalysisReceipt {
            proof_id: binding.proof_id.clone(),
            channel_id: binding.channel_id.clone(),
            sink_id: binding.sink_id.clone(),
            target_id: target.target_id.clone(),
            target_revision: target.revision,
            candidate_version: binding.candidate_version.clone(),
            source_commit: binding.source_commit.clone(),
            configuration_hash: binding.configuration_hash.clone(),
            analyzer_id: TSDUCK_ANALYZER_ID.into(),
            analyzer_version: self.runtime.version.clone(),
            analyzer_sha256: hash_file(&analyzer)?,
            outcome: ProofOutcome::Pass,
            findings,
            captured_at_unix_ms: binding.now_unix_ms,
            recorded_at_unix_ms: binding.now_unix_ms,
            recorded_by: binding.recorded_by.clone(),
            detail: "Bounded TSDuck analysis passed.".into(),
        };
        store.append_transport_analysis_receipt(&receipt, binding.now_unix_ms)
    }
}

fn parse_tsanalyze_json(
    output: &[u8],
    bytes: u64,
    duration_ms: u64,
) -> Result<TransportFindings, String> {
    if output.is_empty() || output.len() > MAX_PROCESS_OUTPUT_BYTES {
        return Err("TSDuck analysis output is missing or oversized".into());
    }
    // TSDuck 3.44 writes `--error-analysis` diagnostics before the JSON report.
    // Keep that diagnostic channel bounded and accept only its documented logger
    // prefixes; arbitrary process chatter is never treated as analyzer evidence.
    let json_start = output
        .iter()
        .position(|byte| *byte == b'{')
        .ok_or_else(|| "TSDuck analysis JSON report is missing".to_string())?;
    let preamble = std::str::from_utf8(&output[..json_start])
        .map_err(|_| "TSDuck analysis preamble is malformed")?;
    if preamble.lines().any(|line| {
        !line.is_empty()
            && !(line.starts_with("TITLE: ")
                || line.starts_with("INFO: ")
                || line.starts_with("TS:")
                || line.starts_with("SUMMARY: "))
    }) {
        return Err("TSDuck analysis preamble is unexpected".into());
    }
    let report: serde_json::Value = serde_json::from_slice(&output[json_start..])
        .map_err(|_| "TSDuck analysis output is malformed")?;
    let pids = report["pids"]
        .as_array()
        .ok_or("TSDuck PID report is missing")?;
    let services = report["services"]
        .as_array()
        .ok_or("TSDuck service report is missing")?;
    let ts = report
        .get("ts")
        .ok_or("TSDuck transport report is missing")?;
    let packet_count = ts["packets"]["total"]
        .as_u64()
        .ok_or("TSDuck packet count is missing")?;
    let bitrate_bps = ts["bitrate"].as_u64().ok_or("TSDuck bitrate is missing")?;
    let pat = pids.iter().any(|pid| {
        pid["description"] == "PAT" && pid["packets"]["total"].as_u64().unwrap_or(0) > 0
    });
    let pmt = pids.iter().filter(|pid| pid["pmt"] == true).count() as u32;
    let video = pids.iter().any(|pid| {
        pid["video"] == true
            && pid["description"]
                .as_str()
                .is_some_and(|text| text.contains("AVC"))
    });
    let audio = pids.iter().any(|pid| {
        pid["audio"] == true
            && pid["description"]
                .as_str()
                .is_some_and(|text| text.contains("AAC"))
    });
    let pcr = pids
        .iter()
        .any(|pid| pid["packets"]["pcr"].as_u64().unwrap_or(0) > 0);
    let continuity_errors: u64 = pids
        .iter()
        .map(|pid| {
            pid["packets"]["discontinuities"]
                .as_u64()
                .unwrap_or(u64::MAX)
        })
        .sum();
    let pcr_errors: u64 = pids
        .iter()
        .map(|pid| pid["packets"]["pcr-leap"].as_u64().unwrap_or(u64::MAX))
        .sum();
    if !pat
        || pmt == 0
        || services.is_empty()
        || !video
        || !audio
        || !pcr
        || continuity_errors > MAX_ANALYSIS_ERRORS
        || pcr_errors > MAX_ANALYSIS_ERRORS
    {
        return Err("TSDuck analysis reported transport anomalies".into());
    }
    let findings = TransportFindings {
        packet_size_bytes: 188,
        packet_count,
        capture_duration_ms: duration_ms,
        bitrate_bps,
        pat_program_count: u32::from(pat),
        pmt_stream_count: pmt,
        continuity_errors,
        pcr_errors,
        analyzer_output_sha256: sha256(output),
    };
    findings.validate()?;
    if u64::from(findings.packet_size_bytes).checked_mul(findings.packet_count) != Some(bytes) {
        return Err("TSDuck analysis packet count does not match the bounded capture".into());
    }
    Ok(findings)
}

fn tsanalyze_service_binding(output: &[u8]) -> Result<(u64, u64), String> {
    let json_start = output
        .iter()
        .position(|byte| *byte == b'{')
        .ok_or("TSDuck analysis JSON report is missing")?;
    let report: serde_json::Value = serde_json::from_slice(&output[json_start..])
        .map_err(|_| "TSDuck analysis output is malformed")?;
    let services = report["services"]
        .as_array()
        .ok_or("TSDuck service report is missing")?;
    if services.len() != 1 {
        return Err("TSDuck analysis must identify exactly one service".into());
    }
    let service = &services[0];
    let service_id = service["id"]
        .as_u64()
        .ok_or("TSDuck service identifier is missing")?;
    let pmt_pid = service["pmt-pid"]
        .as_u64()
        .ok_or("TSDuck PMT PID is missing")?;
    if service_id == 0 || pmt_pid == 0 {
        return Err("TSDuck service binding is invalid".into());
    }
    Ok((service_id, pmt_pid))
}

/// Receiver contract deliberately supports only the authenticated HTTPS API profile.
/// SNMP/TLS remains fail-closed until an attested implementation exists.
pub trait AuthenticatedReceiver: sealed::Receiver + Send + Sync {
    fn probe(&self, target: &TargetDeviceConfig, deadline: Duration) -> Result<Vec<u8>, String>;
}
/// Production HTTPS adapter. The caller obtains the Authorization value from an
/// approved secret provider; it is never copied into an error or receipt. The
/// receiver contract has one fixed resource path, so target configuration cannot
/// smuggle a path, query, or credential into the request.
pub struct HttpsReceiverAdapter {
    authorization_value: String,
}
impl sealed::Receiver for HttpsReceiverAdapter {}
impl HttpsReceiverAdapter {
    pub fn new(authorization_value: String) -> Result<Self, String> {
        if authorization_value.is_empty()
            || authorization_value.len() > 8_192
            || authorization_value.chars().any(char::is_control)
        {
            return Err("receiver authorization material is unavailable".into());
        }
        Ok(Self {
            authorization_value,
        })
    }
}
impl AuthenticatedReceiver for HttpsReceiverAdapter {
    fn probe(&self, target: &TargetDeviceConfig, deadline: Duration) -> Result<Vec<u8>, String> {
        target.validate()?;
        if target.protocol != TargetProtocol::HttpsApi {
            return Err("receiver protocol is not implemented".into());
        }
        let url = format!(
            "https://{}:{}/api/v1/townlight/reachability",
            target.host, target.port
        );
        let agent: ureq::Agent = ureq::Agent::config_builder()
            .timeout_global(Some(deadline))
            .build()
            .into();
        let mut response = agent
            .get(&url)
            .header("Authorization", &self.authorization_value)
            .call()
            .map_err(|_| "authenticated receiver request failed")?;
        let mut body = Vec::new();
        response
            .body_mut()
            .as_reader()
            .take((MAX_RECEIVER_RESPONSE_BYTES + 1) as u64)
            .read_to_end(&mut body)
            .map_err(|_| "authenticated receiver response could not be read")?;
        if body.len() > MAX_RECEIVER_RESPONSE_BYTES {
            return Err("authenticated receiver response exceeded its bound".into());
        }
        Ok(body)
    }
}
#[cfg(test)]
#[derive(Debug, Clone)]
pub struct FakeAuthenticatedReceiver {
    pub response: Result<Vec<u8>, String>,
}
#[cfg(test)]
impl sealed::Receiver for FakeAuthenticatedReceiver {}
#[cfg(test)]
impl AuthenticatedReceiver for FakeAuthenticatedReceiver {
    fn probe(&self, _: &TargetDeviceConfig, _: Duration) -> Result<Vec<u8>, String> {
        self.response.clone()
    }
}
pub struct ReceiverProofExecutor<A> {
    pub adapter: A,
    pub adapter_version: String,
}
impl<A: AuthenticatedReceiver> ReceiverProofExecutor<A> {
    pub fn persist_reachability(
        &self,
        store: &StationStore,
        binding: &ProofBinding,
        target: &TargetDeviceConfig,
    ) -> Result<(), String> {
        binding.validate()?;
        target.validate()?;
        if target.protocol != TargetProtocol::HttpsApi || !target.credential_configured {
            return Err("target protocol or authenticated credential is not supported".into());
        }
        let started = Instant::now();
        let response = self.adapter.probe(target, Duration::from_secs(10))?;
        if response.is_empty() || response.len() > MAX_RECEIVER_RESPONSE_BYTES {
            return Err("authenticated receiver response is missing or oversized".into());
        }
        let receipt = TargetDeviceReachabilityReceipt {
            proof_id: binding.proof_id.clone(),
            channel_id: binding.channel_id.clone(),
            sink_id: binding.sink_id.clone(),
            target_id: target.target_id.clone(),
            target_revision: target.revision,
            candidate_version: binding.candidate_version.clone(),
            source_commit: binding.source_commit.clone(),
            configuration_hash: binding.configuration_hash.clone(),
            adapter_id: RECEIVER_ADAPTER_ID.into(),
            adapter_version: self.adapter_version.clone(),
            auth_method: "configured_https_credential".into(),
            outcome: ProofOutcome::Pass,
            response_identity_sha256: sha256(&response),
            latency_ms: started.elapsed().as_millis().try_into().unwrap_or(u64::MAX),
            observed_at_unix_ms: binding.now_unix_ms,
            recorded_at_unix_ms: binding.now_unix_ms,
            recorded_by: binding.recorded_by.clone(),
            detail: "Authenticated receiver reachability passed.".into(),
        };
        store.append_target_device_reachability_receipt(&receipt, binding.now_unix_ms)
    }
}

struct WindowsProcessTree(HANDLE);

impl WindowsProcessTree {
    fn new() -> Result<Self, String> {
        let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if handle == INVALID_HANDLE_VALUE || handle.is_null() {
            return Err("TSDuck process tree boundary could not be created".into());
        }
        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let ok = unsafe {
            SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                u32::try_from(std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>())
                    .expect("job object limit structure fits u32"),
            )
        };
        if ok == 0 {
            unsafe { CloseHandle(handle) };
            return Err("TSDuck process tree boundary could not be configured".into());
        }
        Ok(Self(handle))
    }

    fn assign(&self, child: &Child) -> Result<(), String> {
        let process = child.as_raw_handle() as HANDLE;
        if unsafe { AssignProcessToJobObject(self.0, process) } == 0 {
            self.terminate();
            return Err("TSDuck process could not be contained in its job object".into());
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

fn drain_stdout(stream: ChildStdout) -> Result<Vec<u8>, String> {
    drain_process_stream(stream).map_err(|_| "TSDuck stdout could not be read".into())
}

fn drain_stderr(stream: ChildStderr) -> Result<Vec<u8>, String> {
    drain_process_stream(stream).map_err(|_| "TSDuck stderr could not be read".into())
}

fn drain_process_stream<T: Read>(stream: T) -> Result<Vec<u8>, ()> {
    let mut output = Vec::new();
    stream
        .take((MAX_PROCESS_OUTPUT_BYTES + 1) as u64)
        .read_to_end(&mut output)
        .map_err(|_| ())?;
    if output.len() > MAX_PROCESS_OUTPUT_BYTES {
        return Err(());
    }
    Ok(output)
}

fn join_drain(
    drain: std::thread::JoinHandle<Result<Vec<u8>, String>>,
    stream: &str,
) -> Result<Vec<u8>, String> {
    drain
        .join()
        .map_err(|_| format!("TSDuck {stream} drain thread panicked"))?
        .map_err(|_| format!("TSDuck {stream} output exceeded its bound or could not be read"))
}
fn bounded_capture(path: &Path, max: u64) -> Result<Vec<u8>, String> {
    let meta = fs::metadata(path).map_err(|_| "TSDuck capture artifact is unavailable")?;
    if !meta.is_file() || meta.len() == 0 || meta.len() > max {
        return Err("TSDuck capture artifact is empty or exceeds its bound".into());
    }
    let mut out = Vec::with_capacity(
        usize::try_from(meta.len()).map_err(|_| "capture size cannot fit memory")?,
    );
    File::open(path)
        .map_err(|_| "TSDuck capture artifact cannot be opened")?
        .take(max + 1)
        .read_to_end(&mut out)
        .map_err(|_| "TSDuck capture artifact cannot be read")?;
    if out.len() as u64 != meta.len() {
        return Err("TSDuck capture artifact changed while being analyzed".into());
    }
    Ok(out)
}

fn ensure_non_reparse_ancestry(path: &Path) -> Result<(), String> {
    let mut current = Some(path);
    while let Some(candidate) = current {
        let metadata = fs::symlink_metadata(candidate)
            .map_err(|_| "transport artifact ancestry is unavailable")?;
        if metadata.file_attributes() & 0x0400 != 0 {
            return Err("transport artifact ancestry contains a reparse point".into());
        }
        current = candidate.parent();
    }
    Ok(())
}

/// Creates each missing component only after inspecting its original parent
/// path.  Deliberately never canonicalizes first: canonicalization follows a
/// junction and would validate a path outside the service-owned namespace.
fn create_non_reparse_directory_chain(path: &Path) -> Result<(), String> {
    let mut current = PathBuf::new();
    for component in path.components() {
        current.push(component.as_os_str());
        match fs::symlink_metadata(&current) {
            Ok(metadata) => {
                if metadata.file_attributes() & 0x0400 != 0 || !metadata.is_dir() {
                    return Err(
                        "transport artifact root contains a reparse point or non-directory".into(),
                    );
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                let parent = current
                    .parent()
                    .ok_or("transport artifact root has no parent")?;
                let parent_metadata = fs::symlink_metadata(parent)
                    .map_err(|_| "transport artifact root parent is unavailable")?;
                if parent_metadata.file_attributes() & 0x0400 != 0 || !parent_metadata.is_dir() {
                    return Err(
                        "transport artifact root parent is a reparse point or non-directory".into(),
                    );
                }
                fs::create_dir(&current)
                    .map_err(|_| "transport artifact root could not be created")?;
                let created = fs::symlink_metadata(&current)
                    .map_err(|_| "transport artifact root disappeared after creation")?;
                if created.file_attributes() & 0x0400 != 0 || !created.is_dir() {
                    return Err("transport artifact root changed after creation".into());
                }
            }
            Err(_) => return Err("transport artifact root could not be inspected".into()),
        }
    }
    Ok(())
}

fn is_canonical_uuid(value: &str) -> bool {
    value.len() == 36
        && value.bytes().enumerate().all(|(index, byte)| {
            matches!(index, 8 | 13 | 18 | 23) && byte == b'-'
                || !matches!(index, 8 | 13 | 18 | 23) && byte.is_ascii_hexdigit()
        })
}
fn hash_file(path: &Path) -> Result<String, String> {
    let mut file = File::open(path).map_err(|_| "attested executable cannot be opened")?;
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 8192];
    loop {
        let read = file
            .read(&mut buffer)
            .map_err(|_| "attested executable cannot be read")?;
        if read == 0 {
            break;
        }
        digest.update(&buffer[..read]);
    }
    Ok(format!("{:x}", digest.finalize()))
}
fn sha256(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn is_sha256(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|b| b.is_ascii_hexdigit())
}

fn validate_tstables_json(output: &[u8], service_id: u64, pmt_pid: u64) -> Result<(), String> {
    if output.is_empty() || output.len() > MAX_PROCESS_OUTPUT_BYTES {
        return Err("TSDuck tables output is missing or oversized".into());
    }
    let tables: Vec<serde_json::Value> =
        serde_json::from_slice(output).map_err(|_| "TSDuck tables output is malformed")?;
    if tables.len() > 128 {
        return Err("TSDuck tables output exceeds its table bound".into());
    }
    let pat = tables
        .iter()
        .find(|table| table["#name"] == "PAT")
        .ok_or("TSDuck PAT table is missing")?;
    let pmt = tables
        .iter()
        .find(|table| table["#name"] == "PMT" && table["service_id"].as_u64() == Some(service_id))
        .ok_or("TSDuck PMT table is missing")?;
    let pat_matches = pat["#nodes"].as_array().is_some_and(|nodes| {
        nodes.iter().any(|node| {
            node["#name"] == "service"
                && node["service_id"].as_u64() == Some(service_id)
                && node["program_map_pid"].as_u64() == Some(pmt_pid)
        })
    });
    if !pat_matches || pmt["pcr_pid"].as_u64().is_none() {
        return Err("TSDuck PAT and PMT tables are inconsistent".into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};
    fn root() -> PathBuf {
        let root = std::env::temp_dir().join(format!(
            "townlight-tsduck-executor-{}",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(root.join("bin")).unwrap();
        fs::write(root.join("bin/tsp.exe"), b"tsp").unwrap();
        fs::write(root.join("bin/tsanalyze.exe"), b"analyzer").unwrap();
        fs::write(root.join("bin/tstables.exe"), b"tables").unwrap();
        root
    }
    fn runtime(root: &Path) -> AttestedTsdDuckRuntime {
        AttestedTsdDuckRuntime {
            root: root.into(),
            version: "3.44-4676".into(),
            tsp_sha256: hash_file(&root.join("bin/tsp.exe")).unwrap(),
            tsanalyze_sha256: hash_file(&root.join("bin/tsanalyze.exe")).unwrap(),
            tstables_sha256: hash_file(&root.join("bin/tstables.exe")).unwrap(),
        }
    }
    #[test]
    fn structured_argv_never_uses_path_discovery_or_shell() {
        let root = root();
        let e = TsdDuckTransportExecutor {
            runtime: runtime(&root),
            runner: FakeProcessRunner {
                result: Err("unused".into()),
            },
        };
        let argv = e
            .capture_argv(&CaptureRequest {
                udp_host: "127.0.0.1".into(),
                udp_port: 5000,
                duration_ms: 1000,
                max_bytes: 188,
                artifact_path: root.join("capture.ts"),
            })
            .unwrap();
        assert_eq!(argv[0], "-I");
        assert!(!argv.join(" ").contains("cmd"));
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn malformed_or_anomalous_analysis_fails_closed() {
        let root = root();
        let capture = root.join("capture.ts");
        fs::write(&capture, vec![0; 188]).unwrap();
        let e=TsdDuckTransportExecutor{runtime:runtime(&root),runner:FakeProcessRunner{result:Ok(ProcessResult{status_success:true,output:br#"{\"packet_size_bytes\":188,\"packet_count\":1,\"bitrate_bps\":1,\"pat_program_count\":1,\"pmt_stream_count\":1,\"continuity_errors\":1,\"pcr_errors\":0}"#.to_vec()})}};
        assert!(
            e.analyze_capture(&CaptureRequest {
                udp_host: "127.0.0.1".into(),
                udp_port: 5,
                duration_ms: 1000,
                max_bytes: 188,
                artifact_path: capture
            })
            .is_err()
        );
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn receiver_rejects_unsupported_or_uncredentialed_target() {
        let target = TargetDeviceConfig {
            target_id: "target".into(),
            label: "target".into(),
            protocol: TargetProtocol::SnmpTls,
            host: "receiver.example.test".into(),
            port: 443,
            credential_configured: false,
            revision: 1,
            created_at_unix_ms: 1,
            updated_at_unix_ms: 1,
            updated_by: "admin".into(),
        };
        let b = ProofBinding {
            proof_id: "x".into(),
            channel_id: "x".into(),
            sink_id: "x".into(),
            candidate_version: "x".into(),
            source_commit: "a".repeat(40),
            configuration_hash: "b".repeat(64),
            recorded_by: "x".into(),
            now_unix_ms: 1,
        };
        let store = StationStore::open(
            std::env::temp_dir().join(format!("executor-test-{}.db", b.now_unix_ms)),
        )
        .unwrap();
        assert!(
            ReceiverProofExecutor {
                adapter: FakeAuthenticatedReceiver {
                    response: Ok(vec![1])
                },
                adapter_version: "1".into()
            }
            .persist_reachability(&store, &b, &target)
            .is_err()
        );
    }

    #[test]
    fn parses_the_retained_real_tsduck_344_h264_aac_report() {
        let output = include_bytes!("../tests/fixtures/tsduck-3.44-h264-aac-analysis.txt");
        let findings = parse_tsanalyze_json(output, 53_956, 2_000).unwrap();
        assert_eq!(findings.packet_size_bytes, 188);
        assert_eq!(findings.packet_count, 287);
        assert_eq!(findings.pat_program_count, 1);
        assert_eq!(findings.pmt_stream_count, 1);
    }

    #[test]
    fn validates_the_retained_pat_and_pmt_table_binding() {
        validate_tstables_json(
            include_bytes!("../tests/fixtures/tsduck-3.44-h264-aac-tables.json"),
            1,
            4096,
        )
        .unwrap();
    }

    #[test]
    fn rejects_unknown_or_oversized_tsdduck_preamble() {
        let fixture = include_str!("../tests/fixtures/tsduck-3.44-h264-aac-analysis.txt");
        assert!(
            parse_tsanalyze_json(format!("SECRET: no\\n{fixture}").as_bytes(), 53_956, 2_000)
                .is_err()
        );
        assert!(
            parse_tsanalyze_json(
                format!("{}{{}}", "TITLE: x\\n".repeat(10_000)).as_bytes(),
                53_956,
                2_000
            )
            .is_err()
        );
    }

    #[test]
    fn rejects_missing_required_real_report_evidence() {
        let fixture = include_str!("../tests/fixtures/tsduck-3.44-h264-aac-analysis.txt");
        for removed in ["PAT", "\"video\": true", "\"audio\": true", "\"pcr\": 20"] {
            assert!(
                parse_tsanalyze_json(
                    fixture
                        .replacen(removed, "\\\"removed\\\": true", 1)
                        .as_bytes(),
                    53_956,
                    2_000
                )
                .is_err(),
                "{removed}"
            );
        }
    }

    #[test]
    fn managed_capture_root_seals_one_hashed_ten_minute_capture() {
        let root = root();
        let managed = ManagedCaptureRoot::open(root.join("artifacts")).unwrap();
        let job_id = "11111111-2222-4333-8444-555555555555";
        let partial = managed.begin_job(job_id).unwrap();
        fs::write(&partial, vec![0x47; 188]).unwrap();
        let (facts, bytes) = managed.seal_capture(job_id, 1_000, 601_000, 1_024).unwrap();
        assert_eq!(facts.artifact_relative_path, format!("{job_id}/capture.ts"));
        assert_eq!(facts.capture_bytes, 188);
        assert_eq!(facts.capture_sha256, sha256(&bytes));
        let retry = managed.begin_job(job_id).unwrap();
        assert!(retry.ends_with("capture.partial.ts"));
        assert!(fs::read_dir(root.join("artifacts")).unwrap().any(|entry| {
            entry
                .unwrap()
                .file_name()
                .to_string_lossy()
                .starts_with(&format!("{job_id}.quarantine-"))
        }));
        fs::remove_dir_all(root).unwrap();
    }
}
