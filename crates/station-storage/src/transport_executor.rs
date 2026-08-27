//! Bounded process and receiver adapters for the durable transport-proof authority.
//!
//! This layer never searches `PATH`, never constructs a shell command, and never
//! turns process output into persisted evidence.  Only parsed bounded facts and
//! digests reach the append-only authority in `station-storage`.

use std::fs::{self, File};
use std::io::{Read, Seek, SeekFrom, Write};
use std::os::windows::fs::MetadataExt;
use std::os::windows::fs::OpenOptionsExt;
use std::os::windows::io::AsRawHandle;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStderr, ChildStdout, Command, Stdio};
#[cfg(test)]
use std::sync::Mutex;
#[cfg(test)]
use std::sync::atomic::AtomicUsize;
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
    mpsc,
};
use std::time::{Duration, Instant};

use sha2::{Digest, Sha256};
use station_domain::{AlertConditionKind, AlertEvent, AlertEventState, AlertSeverity};
use station_storage::{
    ProofOutcome, StationStore, TargetDeviceConfig, TargetDeviceReachabilityReceipt,
    TransportAnalysisReceipt, TransportFindings, TransportProofCaptureFacts,
    TransportProofExecution,
};
use station_transport_proof::{MAX_PROOF_CAPTURE_BYTES, MAX_PROOF_DURATION_MS, TargetProtocol};
use windows_sys::Win32::Foundation::{CloseHandle, HANDLE, INVALID_HANDLE_VALUE};
use windows_sys::Win32::Storage::FileSystem::{
    FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_READ, FILE_SHARE_WRITE,
};
use windows_sys::Win32::System::IO::CancelSynchronousIo;
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JobObjectExtendedLimitInformation,
    SetInformationJobObject, TerminateJobObject,
};

const MAX_PROCESS_OUTPUT_BYTES: usize = 64 * 1024;
const MAX_RECEIVER_RESPONSE_BYTES: usize = 8 * 1024;
const MAX_ANALYSIS_ERRORS: u64 = 0;
/// Scheduler and process teardown may add a small, measured tail to the
/// ten-minute requested capture.  The receipt records that measured value.
const MAX_CAPTURE_ELAPSED_MS: u64 = MAX_PROOF_DURATION_MS + 5_000;
const CLAIM_LEASE_MS: i64 = 1_900_000;
const ANALYSIS_LEASE_MS: i64 = 1_200_000;
const ANALYSIS_LEASE_HEARTBEAT_INTERVAL: Duration = Duration::from_secs(30);
const POST_EXIT_DRAIN_TIMEOUT: Duration = Duration::from_secs(5);
const MAX_QUARANTINE_ENTRIES: usize = 8;
const MAX_QUARANTINE_BYTES: u64 = 2 * 1024 * 1024 * 1024;
const QUARANTINE_MAX_AGE: Duration = Duration::from_secs(24 * 60 * 60);
const QUARANTINE_MARKER: &str = ".townlight-transport-owner-v1";
const MAX_QUARANTINE_TREE_ENTRIES: usize = 10_000;
const MAX_QUARANTINE_TREE_DEPTH: usize = 32;
const QUARANTINE_SCAN_TIMEOUT: Duration = Duration::from_secs(2);
const TSDUCK_ANALYZER_ID: &str = "tsduck-tsanalyze";
const RECEIVER_ADAPTER_ID: &str = "townlight-https-receiver-v1";
const RECEIVER_RESOURCE_PATH: &str = "/api/v1/townlight/reachability";

#[cfg(test)]
static FORCE_JOB_ASSIGN_FAILURE: AtomicBool = AtomicBool::new(false);
#[cfg(test)]
static FORCED_CANCEL_FAILURES: AtomicUsize = AtomicUsize::new(0);
#[cfg(test)]
static FORCED_CAPTURE_DRAIN_DELAY_MS: AtomicUsize = AtomicUsize::new(0);
#[cfg(test)]
static FORCED_RUNNER_FAILURE_POINT: AtomicUsize = AtomicUsize::new(0);
#[cfg(test)]
static ACTIVE_PROCESS_DRAINS: AtomicUsize = AtomicUsize::new(0);
#[cfg(test)]
static FORCED_STDERR_DRAIN_DELAY_MS: AtomicUsize = AtomicUsize::new(0);
#[cfg(test)]
static RUNNER_FAULT_TEST_LOCK: Mutex<()> = Mutex::new(());

/// A capture namespace exclusively owned by stationd.  It rejects reparse
/// points from its root through the per-job directory and exposes no API for a
/// caller to nominate an absolute artifact path.
#[derive(Debug, Clone)]
pub(crate) struct ManagedCaptureRoot {
    root: PathBuf,
}

struct PinnedAnalysisInput {
    path: PathBuf,
    file: File,
    _directories: Vec<File>,
}

struct PinnedCaptureOutput {
    path: PathBuf,
    file: File,
    _directories: Vec<File>,
}

impl PinnedAnalysisInput {
    fn bytes(&self, max: u64) -> Result<Vec<u8>, String> {
        let mut file = self
            .file
            .try_clone()
            .map_err(|_| "transport analysis input handle could not be cloned")?;
        file.seek(SeekFrom::Start(0))
            .map_err(|_| "transport analysis input handle could not be rewound")?;
        bounded_capture_file(file, max)
    }
}

impl ManagedCaptureRoot {
    pub(crate) fn open(root: impl AsRef<Path>) -> Result<Self, String> {
        let root = root.as_ref();
        if !root.is_absolute() {
            return Err("transport artifact root must be absolute".into());
        }
        create_non_reparse_directory_chain(root)?;
        let root = root.to_path_buf();
        ensure_non_reparse_ancestry(&root)?;
        Ok(Self { root })
    }

    fn begin_job(&self, job_id: &str) -> Result<PinnedCaptureOutput, String> {
        if !is_canonical_uuid(job_id) {
            return Err("transport capture job identity is invalid".into());
        }
        ensure_non_reparse_ancestry(&self.root)?;
        self.cleanup_quarantine()?;
        let job = self.root.join(job_id);
        if job.exists() {
            ensure_non_reparse_ancestry(&job)?;
            verify_quarantine_owner(&job, job_id)?;
            let nonce = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map_err(|_| "transport artifact clock is unavailable")?
                .as_nanos();
            let quarantine = self.root.join(format!("{job_id}.quarantine-{nonce}"));
            fs::rename(&job, &quarantine)
                .map_err(|_| "stale transport capture could not be quarantined")?;
            ensure_non_reparse_ancestry(&quarantine)?;
        }
        self.cleanup_quarantine()?;
        fs::create_dir(&job).map_err(|_| "transport capture job directory could not be created")?;
        ensure_non_reparse_ancestry(&job)?;
        write_quarantine_owner(&job, job_id)?;
        let path = job.join("capture.ts");
        let file = fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .share_mode(0)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
            .open(&path)
            .map_err(|_| "transport capture output handle could not be created")?;
        let directories = pin_directory_ancestry(&self.root, &job)?;
        Ok(PinnedCaptureOutput {
            path,
            file,
            _directories: directories,
        })
    }

    /// Quarantine is service-owned and bounded.  Every candidate is inspected
    /// for reparse points before recursive size accounting or removal; an
    /// anomalous entry causes the next job to fail closed instead of following
    /// it outside the artifact namespace.
    fn cleanup_quarantine(&self) -> Result<(), String> {
        let now = std::time::SystemTime::now();
        let scan_deadline = Instant::now() + QUARANTINE_SCAN_TIMEOUT;
        let mut entries = Vec::new();
        let mut root_entries = 0_usize;
        for entry in fs::read_dir(&self.root)
            .map_err(|_| "transport artifact root could not be enumerated")?
        {
            let entry = entry.map_err(|_| "transport artifact root entry could not be read")?;
            root_entries += 1;
            if root_entries > MAX_QUARANTINE_TREE_ENTRIES || Instant::now() >= scan_deadline {
                return Err("transport artifact root scan exceeded its bound".into());
            }
            let name = entry.file_name();
            let name = name.to_string_lossy();
            if name.starts_with(".deleting-") {
                let path = entry.path();
                ensure_non_reparse_ancestry(&path)?;
                let owner = read_quarantine_owner(&path)?;
                verify_quarantine_owner(&path, &owner)?;
                quarantine_tree_bytes(&path, scan_deadline)?;
                fs::remove_dir_all(&path)
                    .map_err(|_| "transport quarantine tombstone could not be recovered")?;
                continue;
            }
            let Some((job_id, _)) = name.split_once(".quarantine-") else {
                continue;
            };
            if !is_canonical_uuid(job_id) {
                return Err("transport quarantine name is not service-owned".into());
            }
            let path = entry.path();
            ensure_non_reparse_ancestry(&path)?;
            let metadata = fs::symlink_metadata(&path)
                .map_err(|_| "transport quarantine could not be inspected")?;
            if !metadata.is_dir() {
                return Err("transport quarantine is not a directory".into());
            }
            verify_quarantine_owner(&path, job_id)?;
            let modified = metadata.modified().unwrap_or(std::time::UNIX_EPOCH);
            entries.push((
                path,
                modified,
                quarantine_tree_bytes(&entry.path(), scan_deadline)?,
            ));
        }
        entries.sort_by_key(|(_, modified, _)| *modified);
        let mut total = entries.iter().try_fold(0_u64, |sum, (_, _, bytes)| {
            sum.checked_add(*bytes)
                .ok_or("transport quarantine size overflowed")
        })?;
        while let Some((path, modified, bytes)) = entries.first().cloned() {
            let expired = now.duration_since(modified).unwrap_or_default() > QUARANTINE_MAX_AGE;
            if entries.len() <= MAX_QUARANTINE_ENTRIES && total <= MAX_QUARANTINE_BYTES && !expired
            {
                break;
            }
            ensure_non_reparse_ancestry(&path)?;
            let deleting = self.root.join(format!(
                ".deleting-{}-{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map_err(|_| "transport quarantine clock is unavailable")?
                    .as_nanos()
            ));
            fs::rename(&path, &deleting)
                .map_err(|_| "transport quarantine could not be claimed for deletion")?;
            ensure_non_reparse_ancestry(&deleting)?;
            verify_quarantine_owner(
                &deleting,
                path.file_name()
                    .and_then(|name| name.to_str())
                    .and_then(|name| name.split_once(".quarantine-"))
                    .map(|pair| pair.0)
                    .ok_or("transport quarantine identity was lost")?,
            )?;
            quarantine_tree_bytes(&deleting, Instant::now() + QUARANTINE_SCAN_TIMEOUT)?;
            fs::remove_dir_all(&deleting)
                .map_err(|_| "transport quarantine could not be removed safely")?;
            total = total
                .checked_sub(bytes)
                .ok_or("transport quarantine accounting underflowed")?;
            entries.remove(0);
        }
        if entries.len() > MAX_QUARANTINE_ENTRIES || total > MAX_QUARANTINE_BYTES {
            return Err("transport quarantine exceeds its service-owned bounds".into());
        }
        Ok(())
    }

    /// Hashes the closed partial capture, then atomically promotes it to the
    /// only analysis name.  The returned bytes are retained by the controller
    /// for analysis, so no later pathname substitution can affect findings.
    fn seal_capture(
        &self,
        capture: &mut PinnedCaptureOutput,
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
        let final_path = job.join("capture.ts");
        if capture.path != final_path {
            return Err("transport capture handle is not bound to this job".into());
        }
        capture
            .file
            .flush()
            .map_err(|_| "transport capture could not be flushed")?;
        capture
            .file
            .sync_all()
            .map_err(|_| "transport capture could not be committed")?;
        capture
            .file
            .seek(SeekFrom::Start(0))
            .map_err(|_| "transport capture could not be rewound")?;
        let bytes = bounded_capture_file(
            capture
                .file
                .try_clone()
                .map_err(|_| "transport capture handle could not be cloned")?,
            max_capture_bytes,
        )?;
        let facts = TransportProofCaptureFacts {
            artifact_relative_path: format!("{job_id}/capture.ts"),
            capture_bytes: u64::try_from(bytes.len())
                .map_err(|_| "transport capture size overflowed")?,
            capture_sha256: sha256(&bytes),
            started_at_unix_ms,
            ended_at_unix_ms,
        };
        facts.validate(max_capture_bytes)?;
        Ok((facts, bytes))
    }

    /// TSDuck receives an attempt-private input copy, not the retained capture
    /// pathname.  The controller has already closed, bounded, and hashed these
    /// bytes; `create_new` refuses a pre-existing substitution.
    fn write_analysis_copy(
        &self,
        job_id: &str,
        facts: &TransportProofCaptureFacts,
        bytes: &[u8],
    ) -> Result<PinnedAnalysisInput, String> {
        if !is_canonical_uuid(job_id)
            || facts.capture_bytes
                != u64::try_from(bytes.len()).map_err(|_| "capture size overflowed")?
            || facts.capture_sha256 != sha256(bytes)
        {
            return Err("transport analysis copy does not match sealed capture facts".into());
        }
        let job = self.root.join(job_id);
        ensure_non_reparse_ancestry(&job)?;
        let path = job.join("analysis-input.ts");
        let mut copy = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
            .map_err(|_| "transport analysis input could not be created")?;
        copy.write_all(bytes)
            .map_err(|_| "transport analysis input could not be written")?;
        copy.sync_all()
            .map_err(|_| "transport analysis input could not be sealed")?;
        drop(copy);
        ensure_non_reparse_ancestry(&path)?;
        let copied = bounded_capture(&path, facts.capture_bytes)?;
        if copied != bytes {
            return Err("transport analysis input changed after sealing".into());
        }
        let directories = pin_directory_ancestry(&self.root, &job)?;
        let file = fs::OpenOptions::new()
            .read(true)
            // TSDuck may open the same leaf for reading. No writer or deleter
            // can open/replace it while this handle remains alive.
            .share_mode(FILE_SHARE_READ)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
            .open(&path)
            .map_err(|_| "transport analysis input could not be pinned")?;
        let pinned = PinnedAnalysisInput {
            path,
            file,
            _directories: directories,
        };
        if pinned.bytes(facts.capture_bytes)? != bytes {
            return Err("transport analysis input changed before handle pinning".into());
        }
        Ok(pinned)
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
    let mut backoff = Duration::from_millis(250);
    while !stop.load(Ordering::Acquire) {
        match run_transport_proof_queue_iteration(database, application_root) {
            Ok(()) => {
                // Only a successful open, recovery, claim, and any claimed
                // job's durable terminalization constitutes recovery. A job
                // failure is itself durable evidence, not a dead controller.
                resolve_transport_controller_alert(database);
                backoff = Duration::from_millis(250);
            }
            Err(_) => {
                observe_transport_controller_alert(database);
                backoff = (backoff * 2).min(Duration::from_secs(5));
            }
        }
        std::thread::sleep(backoff);
    }
    Ok(())
}

fn run_transport_proof_queue_iteration(
    database: &Path,
    application_root: &Path,
) -> Result<(), String> {
    let now = unix_time_millis()?;
    let store = StationStore::open(database)?;
    store.recover_expired_transport_proof_jobs(now)?;
    if let Some(execution) = store.claim_next_transport_proof_job(now, CLAIM_LEASE_MS)?
        && execute_claimed_transport_job(&store, database, application_root, &execution).is_err()
    {
        // Details are deliberately static: process output, targets, and
        // credentials never enter durable evidence or error receipts.
        store.fail_transport_proof_job(
            &execution,
            unix_time_millis().unwrap_or(now),
            "Transport proof execution failed before a passing receipt could be produced.",
        )?;
    }
    Ok(())
}

const TRANSPORT_CONTROLLER_ALERT_RESOURCE: &str = "transport-proof-controller";

fn observe_transport_controller_alert(database: &Path) {
    let Ok(now) = unix_time_millis() else {
        return;
    };
    let Ok(store) = StationStore::open(database) else {
        return;
    };
    // Do not persist raw DB, process, target, or authorization errors. The
    // typed alert tells the operator what is actionable while preserving the
    // actual failure boundary.
    let _ = store.observe_alert_and_route(&AlertEvent {
        event_id: "transport-proof-controller-service-down".into(),
        condition: AlertConditionKind::ServiceDown,
        severity: AlertSeverity::Warning,
        state: AlertEventState::Firing,
        resource_ref: TRANSPORT_CONTROLLER_ALERT_RESOURCE.into(),
        summary: "Transport proof controller needs operator attention.".into(),
        detail: "The controller could not complete a durable queue iteration and is retrying with bounded backoff.".into(),
        source_section: "S11".into(),
        first_observed_at_unix_ms: now,
        last_observed_at_unix_ms: now,
        resolved_at_unix_ms: None,
        occurrence_count: 1,
        acknowledged_at_unix_ms: None,
        acknowledged_by: None,
    });
}

fn resolve_transport_controller_alert(database: &Path) {
    let Ok(now) = unix_time_millis() else {
        return;
    };
    let Ok(store) = StationStore::open(database) else {
        return;
    };
    let _ = store.resolve_alert(
        AlertConditionKind::ServiceDown,
        TRANSPORT_CONTROLLER_ALERT_RESOURCE,
        now,
    );
}

fn execute_claimed_transport_job(
    store: &StationStore,
    database: &Path,
    application_root: &Path,
    execution: &TransportProofExecution,
) -> Result<(), String> {
    let mut clock = unix_time_millis;
    execute_claimed_transport_job_with(
        store,
        database,
        application_root,
        execution,
        TransportExecutionAdapters {
            clock: &mut clock,
            runner: RealProcessRunner,
            receiver_factory: HttpsReceiverAdapter::new,
            post_probe_action: PostProbeAction::None,
        },
    )
}

struct TransportExecutionAdapters<'a, R, F> {
    clock: &'a mut dyn FnMut() -> Result<i64, String>,
    runner: R,
    receiver_factory: F,
    post_probe_action: PostProbeAction,
}

#[derive(Clone, Copy)]
enum PostProbeAction {
    None,
    #[cfg(feature = "test-support")]
    Mutate(InjectedAuthorityMutation),
}

fn execute_claimed_transport_job_with<R, A, F>(
    store: &StationStore,
    database: &Path,
    application_root: &Path,
    execution: &TransportProofExecution,
    adapters: TransportExecutionAdapters<'_, R, F>,
) -> Result<(), String>
where
    R: CaptureProcessRunner,
    A: AuthenticatedReceiver,
    F: FnOnce(String) -> Result<A, String>,
{
    let TransportExecutionAdapters {
        clock,
        runner,
        receiver_factory,
        post_probe_action,
    } = adapters;
    let job = execution.job();
    let data_directory = database
        .parent()
        .ok_or("transport proof data directory is unavailable")?;
    ensure_current_candidate(data_directory, application_root, &job.job)?;
    let snapshot = store
        .read_channel_loudness_proof_snapshot(&job.job.channel_id)?
        .ok_or("transport proof channel is unavailable")?;
    if snapshot.configuration_hash != job.job.configuration_hash {
        return Err("transport proof configuration changed after request".into());
    }
    let target = store
        .read_transport_target_config(&job.job.channel_id, &job.job.sink_id)?
        .ok_or("transport proof target is unavailable")?;
    if target.target_id != job.job.target_id || target.revision != job.job.target_revision {
        return Err("transport proof target changed after request".into());
    }
    let sink = snapshot
        .output_sinks
        .into_iter()
        .find(|sink| sink.sink_id == job.job.sink_id && sink.enabled)
        .ok_or("transport proof sink is unavailable")?;
    let (host, port) = parse_udp_destination(&sink.destination)?;
    let root = ManagedCaptureRoot::open(data_directory.join("artifacts").join("transport-proof"))?;
    let mut capture_output = root.begin_job(&job.job.job_id)?;
    let runtime = AttestedTsdDuckRuntime::from_verified_install(data_directory, application_root)?;
    let executor = TsdDuckTransportExecutor { runtime, runner };
    let capture = CaptureRequest {
        udp_host: host,
        udp_port: port,
        duration_ms: MAX_PROOF_DURATION_MS,
        max_bytes: job.job.max_capture_bytes,
        artifact_path: capture_output.path.clone(),
    };
    let started = clock()?;
    executor.capture_to_pinned_handle(&capture, &capture_output.file)?;
    let ended = clock()?;
    let (facts, sealed_bytes) = root.seal_capture(
        &mut capture_output,
        &job.job.job_id,
        started,
        ended,
        job.job.max_capture_bytes,
    )?;
    let analyzing =
        store.begin_transport_proof_analysis(execution, &facts, ended, ANALYSIS_LEASE_MS)?;
    let analysis_input = root.write_analysis_copy(&job.job.job_id, &facts, &sealed_bytes)?;
    let measured_duration_ms = u64::try_from(
        facts
            .ended_at_unix_ms
            .checked_sub(facts.started_at_unix_ms)
            .ok_or("transport capture clock ordering is invalid")?,
    )
    .map_err(|_| "transport capture duration is invalid")?;
    let mut analysis_lease = AnalysisLeaseHeartbeat::new(store, execution, ended);
    let findings = executor.analyze_pinned_capture_with_heartbeat(
        &CaptureRequest {
            artifact_path: analysis_input.path.clone(),
            duration_ms: measured_duration_ms,
            ..capture
        },
        &analysis_input,
        &mut || analysis_lease.renew_if_due(),
    )?;
    analysis_lease.renew_now()?;
    store.retain_transport_proof_tables_output(
        execution,
        &findings.tables_output_sha256,
        unix_time_millis()?,
    )?;
    // Renew immediately before and after removing the analyzer input. A slow
    // filesystem teardown cannot turn a still-running controller into an
    // expired lease that a second controller may recover.
    analysis_lease.renew_now()?;
    let analysis_input_path = analysis_input.path.clone();
    drop(analysis_input);
    fs::remove_file(&analysis_input_path)
        .map_err(|_| "transport analysis input could not be removed")?;
    analysis_lease.renew_now()?;
    let recorded_at_unix_ms = clock()?;
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
        capture_bytes: facts.capture_bytes,
        capture_sha256: facts.capture_sha256.clone(),
        outcome: ProofOutcome::Pass,
        findings,
        captured_at_unix_ms: ended,
        recorded_at_unix_ms,
        recorded_by: "stationd-transport-proof-controller".into(),
        detail: "Bounded TSDuck transport analysis passed.".into(),
    };
    // The receiver is deliberately probed before the installation fence is
    // acquired so a bounded network timeout never blocks an update.  Its
    // response is only committed after the fence closes the candidate/target
    // race and both authorities are re-read.
    let authorization = store.read_transport_target_authorization(
        &job.job.channel_id,
        &job.job.sink_id,
        &target,
    )?;
    let receiver = ReceiverProofExecutor {
        adapter: receiver_factory(authorization)?,
        adapter_version: "1".into(),
    };
    let receiver_binding = ProofBinding {
        proof_id: analyzing.job.job_id.clone(),
        channel_id: analyzing.job.channel_id.clone(),
        sink_id: analyzing.job.sink_id.clone(),
        candidate_version: analyzing.job.candidate_version.clone(),
        source_commit: analyzing.job.source_commit.clone(),
        configuration_hash: analyzing.job.configuration_hash.clone(),
        recorded_by: "stationd-transport-proof-controller".into(),
        now_unix_ms: recorded_at_unix_ms,
    };
    let reachability = receiver.probe_receipt(&receiver_binding, &target)?;
    apply_post_probe_action(post_probe_action, store, &target, data_directory, &job.job)?;
    // The closure is executed by storage after BEGIN IMMEDIATE, alongside its
    // current target/configuration revalidation. The shared installation fence
    // remains held through that transaction, so activation cannot replace the
    // active-slot receipt after this verified read and before the durable
    // completion decision.
    let install_authority_fence =
        station_maintenance::acquire_install_authority_fence(data_directory)?;
    if install_authority_fence.was_abandoned() {
        return Err("installation authority fence was abandoned; transport evidence requires update recovery".into());
    }
    ensure_current_candidate(data_directory, application_root, &job.job)?;
    let current_target = store
        .read_transport_target_config(&job.job.channel_id, &job.job.sink_id)?
        .ok_or("transport proof target is unavailable")?;
    if current_target != target {
        return Err("transport proof target changed during receiver verification".into());
    }
    store.complete_transport_proof_job_with_analysis(
        execution,
        &receipt,
        &reachability,
        recorded_at_unix_ms,
        || station_maintenance::read_verified_install_authority(data_directory, application_root),
    )?;
    Ok(())
}

fn apply_post_probe_action(
    action: PostProbeAction,
    _store: &StationStore,
    _target: &TargetDeviceConfig,
    _data_directory: &Path,
    _job: &station_storage::TransportProofJob,
) -> Result<(), String> {
    match action {
        PostProbeAction::None => Ok(()),
        #[cfg(feature = "test-support")]
        PostProbeAction::Mutate(mutation) => {
            let (store, target, data_directory, job) = (_store, _target, _data_directory, _job);
            match mutation {
                InjectedAuthorityMutation::None => {}
                InjectedAuthorityMutation::TargetRevision => {
                    let mut changed = target.clone();
                    changed.updated_at_unix_ms = changed.updated_at_unix_ms.saturating_add(1);
                    changed.credential_configured = false;
                    store.write_transport_target_config(
                        &job.channel_id,
                        &job.sink_id,
                        Some(target.revision),
                        &changed,
                    )?;
                }
                InjectedAuthorityMutation::OutputConfiguration => {
                    let mut channel = store
                        .list_channels()?
                        .into_iter()
                        .find(|channel| channel.channel_id == job.channel_id)
                        .ok_or("injected controller channel disappeared")?;
                    let sink = channel
                        .output_sinks
                        .iter_mut()
                        .find(|sink| sink.sink_id == job.sink_id)
                        .ok_or("injected controller sink disappeared")?;
                    sink.destination = "127.0.0.1:5501".into();
                    store.put_channel(&channel)?;
                }
                InjectedAuthorityMutation::InstallGeneration => {
                    let active_path =
                        data_directory.join(station_maintenance::ACTIVE_SLOT_RECEIPT_NAME);
                    let mut active: serde_json::Value = serde_json::from_slice(
                        &fs::read(&active_path)
                            .map_err(|_| "injected active slot could not be read")?,
                    )
                    .map_err(|_| "injected active slot is invalid")?;
                    active["generation"] = serde_json::json!(
                        active["generation"].as_u64().unwrap_or(0).saturating_add(1)
                    );
                    fs::write(&active_path, active.to_string())
                        .map_err(|_| "injected active slot could not be mutated")?;
                }
            }
            Ok(())
        }
    }
}

struct AnalysisLeaseHeartbeat<'a> {
    store: &'a StationStore,
    execution: &'a TransportProofExecution,
    last_renewed_at_unix_ms: i64,
}

impl<'a> AnalysisLeaseHeartbeat<'a> {
    fn new(
        store: &'a StationStore,
        execution: &'a TransportProofExecution,
        started_at_unix_ms: i64,
    ) -> Self {
        Self {
            store,
            execution,
            last_renewed_at_unix_ms: started_at_unix_ms,
        }
    }

    fn renew_if_due(&mut self) -> Result<(), String> {
        let now = unix_time_millis()?;
        let elapsed = now
            .checked_sub(self.last_renewed_at_unix_ms)
            .ok_or("transport proof analysis clock moved backwards")?;
        if elapsed
            >= i64::try_from(ANALYSIS_LEASE_HEARTBEAT_INTERVAL.as_millis())
                .expect("analysis heartbeat interval fits i64")
        {
            self.renew_at(now)?;
        }
        Ok(())
    }

    fn renew_now(&mut self) -> Result<(), String> {
        self.renew_at(unix_time_millis()?)
    }

    fn renew_at(&mut self, now_unix_ms: i64) -> Result<(), String> {
        self.store.renew_transport_proof_analysis_lease(
            self.execution,
            now_unix_ms,
            ANALYSIS_LEASE_MS,
        )?;
        self.last_renewed_at_unix_ms = now_unix_ms;
        Ok(())
    }
}

fn ensure_current_candidate(
    data_directory: &Path,
    application_root: &Path,
    job: &station_storage::TransportProofJob,
) -> Result<(), String> {
    let candidate =
        station_maintenance::read_verified_install_authority(data_directory, application_root)?;
    if candidate.version() != job.candidate_version
        || candidate.source_commit() != job.source_commit
        || job.install_generation != Some(candidate.install_generation())
    {
        return Err("transport proof candidate changed after request".into());
    }
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
pub(crate) struct ProofBinding {
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
struct AttestedTsdDuckRuntime {
    root: PathBuf,
    version: String,
    tsp_sha256: String,
    tsanalyze_sha256: String,
    tstables_sha256: String,
}

impl AttestedTsdDuckRuntime {
    /// Production construction is deliberately rooted in the verified install
    /// receipt. Callers cannot nominate another root or executable identity.
    fn from_verified_install(
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

    fn verify(&self) -> Result<(PathBuf, PathBuf, PathBuf), String> {
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
struct CaptureRequest {
    pub udp_host: String,
    pub udp_port: u16,
    pub duration_ms: u64,
    pub max_bytes: u64,
    pub artifact_path: PathBuf,
}

impl CaptureRequest {
    fn validate(&self) -> Result<(), String> {
        if self.udp_host.is_empty()
            || self.udp_host.contains(['/', '\\', '?', '#', '@'])
            || self.udp_port == 0
            || self.duration_ms == 0
            || self.duration_ms > MAX_CAPTURE_ELAPSED_MS
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
trait ProcessRunner: sealed::Process + Send + Sync {
    fn run(
        &self,
        executable: &Path,
        argv: &[String],
        timeout: Duration,
    ) -> Result<ProcessResult, String>;

    /// The default keeps deterministic fake runners honest: a caller gets a
    /// renewal before and after every process operation. The real runner
    /// overrides this to renew continuously while a child and its pipe
    /// teardown are in flight.
    fn run_with_heartbeat(
        &self,
        executable: &Path,
        argv: &[String],
        timeout: Duration,
        heartbeat: &mut dyn FnMut() -> Result<(), String>,
    ) -> Result<ProcessResult, String> {
        heartbeat()?;
        let result = self.run(executable, argv, timeout);
        heartbeat()?;
        result
    }
}

trait CaptureProcessRunner: ProcessRunner {
    fn capture_to_handle(
        &self,
        executable: &Path,
        argv: &[String],
        timeout: Duration,
        output: &File,
    ) -> Result<ProcessResult, String>;
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ProcessResult {
    pub status_success: bool,
    pub output: Vec<u8>,
}

/// The production runner receives an executable and an argv vector.  It does not
/// invoke a shell and drains stdout/stderr through a fixed-size evidence-free cap.
struct RealProcessRunner;
impl sealed::Process for RealProcessRunner {}
impl ProcessRunner for RealProcessRunner {
    fn run(
        &self,
        executable: &Path,
        argv: &[String],
        timeout: Duration,
    ) -> Result<ProcessResult, String> {
        self.run_with_heartbeat(executable, argv, timeout, &mut || Ok(()))
    }

    fn run_with_heartbeat(
        &self,
        executable: &Path,
        argv: &[String],
        timeout: Duration,
        heartbeat: &mut dyn FnMut() -> Result<(), String>,
    ) -> Result<ProcessResult, String> {
        heartbeat()?;
        let _executable_pin = open_attested_executable_unverified(executable)?;
        let job = WindowsProcessTree::new()?;
        let mut child = Command::new(executable)
            .args(argv)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|_| "TSDuck process could not be spawned")?;
        if let Err(error) = job.assign(&child) {
            // The child is not in the job when assignment itself fails, so a
            // JobObject termination would not reach it.  Reap it explicitly
            // before returning; this is the non-suspended spawn fallback.
            let _ = child.kill();
            let _ = child.wait();
            return Err(error);
        }
        #[cfg(test)]
        if FORCED_RUNNER_FAILURE_POINT.load(Ordering::Acquire) == 1 {
            drop(child.stdout.take());
        }
        let stdout = match child.stdout.take() {
            Some(stdout) => stdout,
            None => {
                job.terminate();
                let _ = child.wait();
                return Err("TSDuck stdout pipe was unavailable".into());
            }
        };
        #[cfg(test)]
        if FORCED_RUNNER_FAILURE_POINT.load(Ordering::Acquire) == 2 {
            drop(child.stderr.take());
        }
        let stderr = match child.stderr.take() {
            Some(stderr) => stderr,
            None => {
                job.terminate();
                let _ = child.wait();
                drop(stdout);
                return Err("TSDuck stderr pipe was unavailable".into());
            }
        };
        let (stdout_tx, stdout_rx) = mpsc::sync_channel(1);
        let (stderr_tx, stderr_rx) = mpsc::sync_channel(1);
        let stdout_drain = std::thread::spawn(move || {
            #[cfg(test)]
            ACTIVE_PROCESS_DRAINS.fetch_add(1, Ordering::AcqRel);
            #[cfg(test)]
            if FORCED_RUNNER_FAILURE_POINT.load(Ordering::Acquire) == 4 {
                ACTIVE_PROCESS_DRAINS.fetch_sub(1, Ordering::AcqRel);
                panic!("injected stdout drain panic");
            }
            let _ = stdout_tx.send(drain_stdout(stdout));
            #[cfg(test)]
            ACTIVE_PROCESS_DRAINS.fetch_sub(1, Ordering::AcqRel);
        });
        let stderr_drain = std::thread::spawn(move || {
            #[cfg(test)]
            ACTIVE_PROCESS_DRAINS.fetch_add(1, Ordering::AcqRel);
            #[cfg(test)]
            {
                let delay = FORCED_STDERR_DRAIN_DELAY_MS.swap(0, Ordering::AcqRel);
                if delay > 0 {
                    std::thread::sleep(Duration::from_millis(delay as u64));
                }
            }
            let _ = stderr_tx.send(drain_stderr(stderr));
            #[cfg(test)]
            ACTIVE_PROCESS_DRAINS.fetch_sub(1, Ordering::AcqRel);
        });
        let started = Instant::now();
        let mut last_heartbeat = Instant::now();
        let process_outcome = loop {
            if last_heartbeat.elapsed() >= ANALYSIS_LEASE_HEARTBEAT_INTERVAL {
                if let Err(error) = heartbeat() {
                    break Err(error);
                }
                last_heartbeat = Instant::now();
            }
            #[cfg(test)]
            if FORCED_RUNNER_FAILURE_POINT.load(Ordering::Acquire) == 3 {
                break Err("TSDuck process status could not be read".into());
            }
            match child.try_wait() {
                Ok(Some(status)) => break Ok(status),
                Ok(None) => {}
                Err(_) => break Err("TSDuck process status could not be read".into()),
            }
            if started.elapsed() >= timeout {
                break match heartbeat() {
                    Ok(()) => Err("TSDuck process exceeded its deadline".into()),
                    Err(error) => Err(error),
                };
            }
            std::thread::sleep(Duration::from_millis(10));
        };
        if process_outcome.is_err() {
            job.terminate();
        }
        let _ = child.wait();
        let drains = collect_process_drains(
            &job,
            stdout_rx,
            stderr_rx,
            stdout_drain,
            stderr_drain,
            heartbeat,
        );
        let status = process_outcome?;
        let output = drains?;
        heartbeat()?;
        Ok(ProcessResult {
            status_success: status.success(),
            output,
        })
    }
}

impl RealProcessRunner {
    fn run_capture_to_handle(
        &self,
        executable: &Path,
        argv: &[String],
        timeout: Duration,
        output: &File,
    ) -> Result<ProcessResult, String> {
        let _executable_pin = open_attested_executable_unverified(executable)?;
        let job = WindowsProcessTree::new()?;
        let output = output
            .try_clone()
            .map_err(|_| "transport capture output handle could not be inherited")?;
        let mut child = Command::new(executable)
            .args(argv)
            .stdin(Stdio::null())
            .stdout(Stdio::from(output))
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|_| "TSDuck capture process could not be spawned")?;
        if let Err(error) = job.assign(&child) {
            let _ = child.kill();
            let _ = child.wait();
            return Err(error);
        }
        let stderr = child.stderr.take().ok_or_else(|| {
            job.terminate();
            let _ = child.wait();
            "TSDuck stderr pipe was unavailable"
        })?;
        let (tx, rx) = mpsc::sync_channel(1);
        let drain = std::thread::spawn(move || {
            #[cfg(test)]
            {
                let delay = FORCED_CAPTURE_DRAIN_DELAY_MS.swap(0, Ordering::AcqRel);
                if delay > 0 {
                    std::thread::sleep(Duration::from_millis(delay as u64));
                }
            }
            let _ = tx.send(drain_stderr(stderr));
        });
        let started = Instant::now();
        let status = loop {
            match child.try_wait() {
                Ok(Some(status)) => break status,
                Ok(None) if started.elapsed() < timeout => {
                    std::thread::sleep(Duration::from_millis(10));
                }
                Ok(None) => {
                    return terminate_capture_and_collect_stderr(
                        &job,
                        &mut child,
                        rx,
                        drain,
                        "TSDuck capture process exceeded its deadline",
                    );
                }
                Err(_) => {
                    return terminate_capture_and_collect_stderr(
                        &job,
                        &mut child,
                        rx,
                        drain,
                        "TSDuck capture status could not be read",
                    );
                }
            }
        };
        collect_capture_stderr(&job, &mut child, rx, drain)?;
        Ok(ProcessResult {
            status_success: status.success(),
            output: Vec::new(),
        })
    }
}

impl CaptureProcessRunner for RealProcessRunner {
    fn capture_to_handle(
        &self,
        executable: &Path,
        argv: &[String],
        timeout: Duration,
        output: &File,
    ) -> Result<ProcessResult, String> {
        self.run_capture_to_handle(executable, argv, timeout, output)
    }
}

#[cfg(test)]
#[derive(Debug, Clone, PartialEq, Eq)]
struct FakeProcessRunner {
    result: Result<ProcessResult, String>,
}
#[cfg(test)]
impl sealed::Process for FakeProcessRunner {}
#[cfg(test)]
impl ProcessRunner for FakeProcessRunner {
    fn run(&self, _: &Path, _: &[String], _: Duration) -> Result<ProcessResult, String> {
        self.result.clone()
    }
}

struct TsdDuckTransportExecutor<R> {
    pub runtime: AttestedTsdDuckRuntime,
    pub runner: R,
}
impl<R: ProcessRunner> TsdDuckTransportExecutor<R> {
    #[cfg(test)]
    fn analyze_capture(&self, capture: &CaptureRequest) -> Result<TransportFindings, String> {
        self.analyze_capture_with_heartbeat(capture, &mut || Ok(()))
    }

    #[cfg(test)]
    fn analyze_capture_with_heartbeat(
        &self,
        capture: &CaptureRequest,
        heartbeat: &mut dyn FnMut() -> Result<(), String>,
    ) -> Result<TransportFindings, String> {
        let file = open_private_capture_no_follow(&capture.artifact_path)?;
        let input = PinnedAnalysisInput {
            path: capture.artifact_path.clone(),
            file,
            _directories: Vec::new(),
        };
        self.analyze_pinned_capture_with_heartbeat(capture, &input, heartbeat)
    }

    fn analyze_pinned_capture_with_heartbeat(
        &self,
        capture: &CaptureRequest,
        input: &PinnedAnalysisInput,
        heartbeat: &mut dyn FnMut() -> Result<(), String>,
    ) -> Result<TransportFindings, String> {
        capture.validate()?;
        if capture.artifact_path != input.path {
            return Err("transport analyzer input handle does not match its request".into());
        }
        let (_, analyzer, tables) = self.runtime.verify()?;
        let _analyzer_pin = open_attested_executable(&analyzer, &self.runtime.tsanalyze_sha256)?;
        let bytes = input.bytes(capture.max_bytes)?;
        let argv = vec![
            "--json".into(),
            "--deterministic".into(),
            "--error-analysis".into(),
            capture.artifact_path.to_string_lossy().into_owned(),
        ];
        let result = self.runner.run_with_heartbeat(
            &analyzer,
            &argv,
            Duration::from_millis(capture.duration_ms),
            heartbeat,
        )?;
        if !result.status_success {
            return Err("TSDuck analysis process failed".into());
        }
        let mut findings =
            parse_tsanalyze_json(&result.output, bytes.len() as u64, capture.duration_ms)?;
        let _tables_pin = open_attested_executable(&tables, &self.runtime.tstables_sha256)?;
        let tables_argv = vec![
            "--json-output".into(),
            "-".into(),
            capture.artifact_path.to_string_lossy().into_owned(),
        ];
        let tables_result = self.runner.run_with_heartbeat(
            &tables,
            &tables_argv,
            Duration::from_millis(capture.duration_ms),
            heartbeat,
        )?;
        if !tables_result.status_success {
            return Err("TSDuck tables process failed".into());
        }
        heartbeat()?;
        let tables_bytes = tables_result.output;
        if tables_bytes.is_empty() || tables_bytes.len() > MAX_PROCESS_OUTPUT_BYTES {
            return Err("TSDuck tables output is missing or oversized".into());
        }
        let validation = validate_tstables_json(&tables_bytes, &findings);
        heartbeat()?;
        validation?;
        findings.tables_output_sha256 = sha256(&tables_bytes);
        Ok(findings)
    }

    fn capture_argv(&self, capture: &CaptureRequest) -> Result<Vec<String>, String> {
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
}

impl<R: CaptureProcessRunner> TsdDuckTransportExecutor<R> {
    fn capture_to_pinned_handle(
        &self,
        capture: &CaptureRequest,
        output: &File,
    ) -> Result<(), String> {
        capture.validate()?;
        let (tsp, _, _) = self.runtime.verify()?;
        let _tsp_pin = open_attested_executable(&tsp, &self.runtime.tsp_sha256)?;
        let mut argv = self.capture_argv(capture)?;
        let destination = argv.last_mut().ok_or("TSDuck capture output is missing")?;
        *destination = "-".into();
        let result = self.runner.capture_to_handle(
            &tsp,
            &argv,
            Duration::from_millis(capture.duration_ms.saturating_add(5_000)),
            output,
        )?;
        if !result.status_success {
            return Err("TSDuck capture process failed".into());
        }
        Ok(())
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
    if pids.len() > 8_192 || services.len() != 1 {
        return Err("TSDuck analysis has ambiguous or oversized stream evidence".into());
    }
    let service = &services[0];
    let service_id = service["id"]
        .as_u64()
        .filter(|id| *id > 0)
        .ok_or("TSDuck service identifier is missing")?;
    let pmt_pid = service["pmt-pid"]
        .as_u64()
        .filter(|pid| (1..0x2000).contains(pid))
        .ok_or("TSDuck PMT PID is missing")?;
    let pcr_pid = service["pcr-pid"]
        .as_u64()
        .filter(|pid| (1..0x2000).contains(pid))
        .ok_or("TSDuck PCR PID is missing")?;
    let service_has_pid = |pid: &serde_json::Value| {
        pid["services"]
            .as_array()
            .is_some_and(|ids| ids.len() == 1 && ids[0].as_u64() == Some(service_id))
    };
    let pat_pid_count = pids
        .iter()
        .filter(|pid| {
            pid["description"] == "PAT" && pid["packets"]["total"].as_u64().unwrap_or(0) > 0
        })
        .count();
    let pmt_pid_count = pids
        .iter()
        .filter(|pid| {
            pid["pmt"] == true && pid["id"].as_u64() == Some(pmt_pid) && service_has_pid(pid)
        })
        .count();
    let video_pids: Vec<u64> = pids
        .iter()
        .filter_map(|pid| {
            (pid["video"] == true
                && service_has_pid(pid)
                && pid["description"]
                    .as_str()
                    .is_some_and(|text| text.contains("AVC")))
            .then(|| pid["id"].as_u64())
            .flatten()
        })
        .collect();
    let audio_pids: Vec<u64> = pids
        .iter()
        .filter_map(|pid| {
            (pid["audio"] == true
                && service_has_pid(pid)
                && pid["description"]
                    .as_str()
                    .is_some_and(|text| text.contains("AAC")))
            .then(|| pid["id"].as_u64())
            .flatten()
        })
        .collect();
    let pcr = pids.iter().any(|pid| {
        pid["id"].as_u64() == Some(pcr_pid)
            && service_has_pid(pid)
            && pid["packets"]["pcr"].as_u64().unwrap_or(0) > 0
    });
    let continuity_errors: u64 = pids
        .iter()
        .map(|pid| {
            pid["packets"]["discontinuities"]
                .as_u64()
                .unwrap_or(u64::MAX)
        })
        .try_fold(0_u64, |total, count| total.checked_add(count))
        .ok_or("TSDuck continuity count overflows")?;
    let pcr_errors: u64 = pids
        .iter()
        .map(|pid| pid["packets"]["pcr-leap"].as_u64().unwrap_or(u64::MAX))
        .try_fold(0_u64, |total, count| total.checked_add(count))
        .ok_or("TSDuck PCR error count overflows")?;
    let elementary_pids: Vec<u64> = pids
        .iter()
        .filter_map(|pid| {
            (service_has_pid(pid) && pid["pmt"] != true)
                .then(|| pid["id"].as_u64())
                .flatten()
        })
        .collect();
    if pat_pid_count != 1
        || pmt_pid_count != 1
        || video_pids.len() != 1
        || audio_pids.len() != 1
        || elementary_pids.len() != 2
        || !elementary_pids.contains(&video_pids[0])
        || !elementary_pids.contains(&audio_pids[0])
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
        pat_program_count: u32::try_from(services.len())
            .map_err(|_| "TSDuck service count overflows")?,
        pmt_stream_count: u32::try_from(elementary_pids.len())
            .map_err(|_| "TSDuck component count overflows")?,
        continuity_errors,
        pcr_errors,
        analyzer_output_sha256: sha256(output),
        tables_output_sha256: "0".repeat(64),
        service_id,
        pmt_pid,
        pcr_pid,
        video_pid: video_pids[0],
        audio_pid: audio_pids[0],
    };
    findings.validate()?;
    if u64::from(findings.packet_size_bytes).checked_mul(findings.packet_count) != Some(bytes) {
        return Err("TSDuck analysis packet count does not match the bounded capture".into());
    }
    Ok(findings)
}

/// Receiver contract deliberately supports only the authenticated HTTPS API profile.
/// SNMP/TLS remains fail-closed until an attested implementation exists.
pub(crate) trait AuthenticatedReceiver: sealed::Receiver + Send + Sync {
    fn probe(&self, target: &TargetDeviceConfig, deadline: Duration) -> Result<Vec<u8>, String>;
}
/// Production HTTPS adapter. The caller obtains the Authorization value from an
/// approved secret provider; it is never copied into an error or receipt. The
/// receiver contract has one fixed resource path, so target configuration cannot
/// smuggle a path, query, or credential into the request.
struct HttpsReceiverAdapter {
    authorization_value: String,
}
impl sealed::Receiver for HttpsReceiverAdapter {}
impl HttpsReceiverAdapter {
    fn new(authorization_value: String) -> Result<Self, String> {
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
            "https://{}:{}{RECEIVER_RESOURCE_PATH}",
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
#[cfg(any(test, feature = "test-support"))]
#[derive(Debug, Clone)]
struct FakeAuthenticatedReceiver {
    pub response: Result<Vec<u8>, String>,
}
#[cfg(any(test, feature = "test-support"))]
impl sealed::Receiver for FakeAuthenticatedReceiver {}
#[cfg(any(test, feature = "test-support"))]
impl AuthenticatedReceiver for FakeAuthenticatedReceiver {
    fn probe(&self, _: &TargetDeviceConfig, _: Duration) -> Result<Vec<u8>, String> {
        self.response.clone()
    }
}

#[cfg(feature = "test-support")]
struct InjectedFixtureRunner;

#[cfg(feature = "test-support")]
impl sealed::Process for InjectedFixtureRunner {}

#[cfg(feature = "test-support")]
impl ProcessRunner for InjectedFixtureRunner {
    fn run(&self, _: &Path, argv: &[String], _: Duration) -> Result<ProcessResult, String> {
        if argv
            .first()
            .is_some_and(|argument| argument == "--json-output")
        {
            if argv.get(1).map(String::as_str) != Some("-") || argv.len() != 3 {
                return Err(
                    "injected tstables invocation escaped its fixed stdout contract".into(),
                );
            }
            Ok(ProcessResult {
                status_success: true,
                output: include_bytes!("../tests/fixtures/tsduck-3.44-h264-aac-tables.json")
                    .to_vec(),
            })
        } else {
            if argv.len() != 4
                || argv[0] != "--json"
                || argv[1] != "--deterministic"
                || argv[2] != "--error-analysis"
            {
                return Err("injected tsanalyze invocation escaped its fixed argv contract".into());
            }
            Ok(ProcessResult {
                status_success: true,
                output: include_bytes!("../tests/fixtures/tsduck-3.44-h264-aac-analysis.txt")
                    .to_vec(),
            })
        }
    }
}

#[cfg(feature = "test-support")]
impl CaptureProcessRunner for InjectedFixtureRunner {
    fn capture_to_handle(
        &self,
        _: &Path,
        argv: &[String],
        _: Duration,
        output: &File,
    ) -> Result<ProcessResult, String> {
        if argv.last().map(String::as_str) != Some("-") {
            return Err("injected capture escaped its pinned stdout handle".into());
        }
        let mut output = output
            .try_clone()
            .map_err(|_| "injected capture handle could not be cloned")?;
        output
            .write_all(include_bytes!("../tests/fixtures/tsduck-3.44-h264-aac.ts"))
            .map_err(|_| "injected capture fixture could not be written")?;
        output
            .flush()
            .map_err(|_| "injected capture fixture could not be flushed")?;
        Ok(ProcessResult {
            status_success: true,
            output: Vec::new(),
        })
    }
}

#[cfg(feature = "test-support")]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InjectedAuthorityMutation {
    None,
    TargetRevision,
    OutputConfiguration,
    InstallGeneration,
}

/// Release-excluded proof harness for the private controller authority. The
/// caller can enqueue only through the authenticated API; this function claims
/// that durable row itself, runs fixed fake analyzer/table/receiver adapters,
/// and invokes the same private CAS transitions as production. No receipt or
/// outcome can be supplied by the caller.
#[cfg(feature = "test-support")]
pub fn run_injected_transport_controller_iteration(
    database: &Path,
    application_root: &Path,
    expected_authorization: &str,
    receiver_response: &[u8],
    mutation: InjectedAuthorityMutation,
) -> Result<(), String> {
    let now = unix_time_millis()?;
    let store = StationStore::open(database)?;
    store.recover_expired_transport_proof_jobs(now)?;
    let execution = store
        .claim_next_transport_proof_job(now, CLAIM_LEASE_MS)?
        .ok_or("injected controller found no queued transport proof job")?;
    let expected_authorization = expected_authorization.to_string();
    let response = receiver_response.to_vec();
    let mut clock_calls = 0_u8;
    let mut clock = || {
        clock_calls = clock_calls.saturating_add(1);
        if clock_calls == 1 {
            now.checked_sub(i64::try_from(MAX_PROOF_DURATION_MS).expect("duration fits i64"))
                .ok_or_else(|| "injected capture time underflowed".to_string())
        } else {
            Ok(now)
        }
    };
    let outcome = execute_claimed_transport_job_with(
        &store,
        database,
        application_root,
        &execution,
        TransportExecutionAdapters {
            clock: &mut clock,
            runner: InjectedFixtureRunner,
            receiver_factory: move |authorization| {
                if authorization != expected_authorization
                    || RECEIVER_RESOURCE_PATH != "/api/v1/townlight/reachability"
                {
                    return Err(
                        "injected receiver authority did not match its exact contract".into(),
                    );
                }
                Ok(FakeAuthenticatedReceiver {
                    response: Ok(response),
                })
            },
            post_probe_action: PostProbeAction::Mutate(mutation),
        },
    );
    if outcome.is_err() {
        let _ = store.fail_transport_proof_job(
            &execution,
            unix_time_millis().unwrap_or(now),
            "Transport proof execution failed before a passing receipt could be produced.",
        );
    }
    outcome
}
struct ReceiverProofExecutor<A> {
    pub adapter: A,
    pub adapter_version: String,
}
impl<A: AuthenticatedReceiver> ReceiverProofExecutor<A> {
    fn probe_receipt(
        &self,
        binding: &ProofBinding,
        target: &TargetDeviceConfig,
    ) -> Result<TargetDeviceReachabilityReceipt, String> {
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
        Ok(TargetDeviceReachabilityReceipt {
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
        })
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
        #[cfg(test)]
        if FORCE_JOB_ASSIGN_FAILURE.load(Ordering::Acquire) {
            return Err("TSDuck process could not be contained in its job object".into());
        }
        let process = child.as_raw_handle() as HANDLE;
        if unsafe { AssignProcessToJobObject(self.0, process) } == 0 {
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

/// Capture has a single pipe, but it needs the same teardown promise as the
/// analysis process: every status, timeout, receive, and drain failure kills
/// the owned job, cancels the exact blocking read, waits only a bounded time,
/// and joins the drain thread before returning.  Dropping a JoinHandle on an
/// error would leave a thread (and potentially an inherited pipe) behind.
fn collect_capture_stderr(
    job: &WindowsProcessTree,
    child: &mut Child,
    rx: mpsc::Receiver<Result<Vec<u8>, String>>,
    drain: std::thread::JoinHandle<()>,
) -> Result<(), String> {
    let stderr_result = rx.recv_timeout(POST_EXIT_DRAIN_TIMEOUT);
    let mut cancellation_failed = false;
    if stderr_result.is_err() || !drain.is_finished() {
        job.terminate();
        let _ = child.wait();
        cancellation_failed = cancel_until_finished(&drain, Duration::from_secs(1));
    }
    // The owned job has been terminated and waited before this point, so every
    // inherited write handle is closed. Even if CancelSynchronousIo raced or
    // transiently failed, the reader must observe EOF. Retain the JoinHandle
    // and join unconditionally; never detach it on an error branch.
    drain
        .join()
        .map_err(|_| "TSDuck capture stderr drain panicked".to_string())?;
    if cancellation_failed {
        return Err("TSDuck capture stderr cancellation raced before the drain joined".into());
    }
    match stderr_result {
        Ok(Ok(_)) => Ok(()),
        Ok(Err(_)) => Err("TSDuck capture stderr could not be read".into()),
        Err(mpsc::RecvTimeoutError::Timeout) => {
            Err("TSDuck capture stderr drain exceeded its deadline".into())
        }
        Err(mpsc::RecvTimeoutError::Disconnected) => {
            Err("TSDuck capture stderr drain thread stopped unexpectedly".into())
        }
    }
}

fn terminate_capture_and_collect_stderr(
    job: &WindowsProcessTree,
    child: &mut Child,
    rx: mpsc::Receiver<Result<Vec<u8>, String>>,
    drain: std::thread::JoinHandle<()>,
    primary_error: &str,
) -> Result<ProcessResult, String> {
    job.terminate();
    let _ = child.wait();
    match collect_capture_stderr(job, child, rx, drain) {
        Ok(()) => Err(primary_error.into()),
        Err(cleanup) => Err(format!(
            "{primary_error}; bounded stderr teardown failed: {cleanup}"
        )),
    }
}

fn collect_process_drains(
    job: &WindowsProcessTree,
    stdout_rx: mpsc::Receiver<Result<Vec<u8>, String>>,
    stderr_rx: mpsc::Receiver<Result<Vec<u8>, String>>,
    stdout_thread: std::thread::JoinHandle<()>,
    stderr_thread: std::thread::JoinHandle<()>,
    heartbeat: &mut dyn FnMut() -> Result<(), String>,
) -> Result<Vec<u8>, String> {
    let deadline = Instant::now() + POST_EXIT_DRAIN_TIMEOUT;
    let receive = |receiver: &mpsc::Receiver<Result<Vec<u8>, String>>,
                   stream: &str,
                   heartbeat: &mut dyn FnMut() -> Result<(), String>|
     -> Result<Vec<u8>, String> {
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err(format!("TSDuck {stream} pipe drain exceeded its deadline"));
            }
            match receiver.recv_timeout(remaining.min(Duration::from_millis(100))) {
                Ok(result) => {
                    return result.map_err(|_| {
                        format!("TSDuck {stream} output exceeded its bound or could not be read")
                    });
                }
                Err(mpsc::RecvTimeoutError::Timeout) => heartbeat()?,
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    return Err(format!("TSDuck {stream} drain thread stopped unexpectedly"));
                }
            }
        }
    };
    let stdout_result = receive(&stdout_rx, "stdout", heartbeat);
    let stderr_result = receive(&stderr_rx, "stderr", heartbeat);
    let mut cancellation_failed = false;
    if stdout_result.is_err() || stderr_result.is_err() {
        // A descendant retaining an inherited pipe must not leave controller
        // resources behind after the bounded drain deadline. Cancel the exact
        // synchronous reads before joining their owning threads.
        job.terminate();
        cancellation_failed |= cancel_until_finished(&stdout_thread, Duration::from_secs(1));
        cancellation_failed |= cancel_until_finished(&stderr_thread, Duration::from_secs(1));
    }
    let join_deadline = Instant::now() + Duration::from_secs(1);
    let mut heartbeat_error = None;
    while (!stdout_thread.is_finished() || !stderr_thread.is_finished())
        && Instant::now() < join_deadline
    {
        if heartbeat_error.is_none()
            && let Err(error) = heartbeat()
        {
            heartbeat_error = Some(error);
            job.terminate();
            cancellation_failed |=
                cancel_drain_thread(&stdout_thread) == 0 && !stdout_thread.is_finished();
            cancellation_failed |=
                cancel_drain_thread(&stderr_thread) == 0 && !stderr_thread.is_finished();
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    // TerminateJobObject closes every descendant write handle. Retain and join
    // both owned readers even when cancellation reported a race/failure.
    let stdout_join = stdout_thread.join();
    let stderr_join = stderr_thread.join();
    if stdout_join.is_err() {
        return Err("TSDuck stdout drain thread panicked".into());
    }
    if stderr_join.is_err() {
        return Err("TSDuck stderr drain thread panicked".into());
    }
    if cancellation_failed {
        return Err("TSDuck pipe drain cancellation raced before both drains joined".into());
    }
    if let Some(error) = heartbeat_error {
        return Err(error);
    }
    stderr_result?;
    stdout_result
}

fn cancel_until_finished(thread: &std::thread::JoinHandle<()>, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    let mut cancellation_failed = false;
    while !thread.is_finished() && Instant::now() < deadline {
        cancellation_failed |= cancel_drain_thread(thread) == 0 && !thread.is_finished();
        std::thread::sleep(Duration::from_millis(10));
    }
    cancellation_failed
}

fn cancel_drain_thread(thread: &std::thread::JoinHandle<()>) -> i32 {
    #[cfg(test)]
    if FORCED_CANCEL_FAILURES
        .fetch_update(Ordering::AcqRel, Ordering::Acquire, |remaining| {
            remaining.checked_sub(1)
        })
        .is_ok()
    {
        return 0;
    }
    unsafe { CancelSynchronousIo(thread.as_raw_handle() as HANDLE) }
}
fn bounded_capture(path: &Path, max: u64) -> Result<Vec<u8>, String> {
    let file = open_private_capture_no_follow(path)?;
    bounded_capture_file(file, max)
}

fn bounded_capture_file(file: File, max: u64) -> Result<Vec<u8>, String> {
    let meta = file
        .metadata()
        .map_err(|_| "TSDuck capture artifact is unavailable")?;
    if !meta.is_file() || meta.len() == 0 || meta.len() > max {
        return Err("TSDuck capture artifact is empty or exceeds its bound".into());
    }
    let mut out = Vec::with_capacity(
        usize::try_from(meta.len()).map_err(|_| "capture size cannot fit memory")?,
    );
    file.take(max + 1)
        .read_to_end(&mut out)
        .map_err(|_| "TSDuck capture artifact cannot be read")?;
    if out.len() as u64 != meta.len() {
        return Err("TSDuck capture artifact changed while being analyzed".into());
    }
    Ok(out)
}

fn pin_directory_ancestry(root: &Path, leaf_directory: &Path) -> Result<Vec<File>, String> {
    if !leaf_directory.starts_with(root) {
        return Err("transport analysis directory escaped its managed root".into());
    }
    let mut paths = vec![root.to_path_buf()];
    let mut current = root.to_path_buf();
    for component in leaf_directory
        .strip_prefix(root)
        .map_err(|_| "transport analysis directory escaped its managed root")?
        .components()
    {
        current.push(component.as_os_str());
        paths.push(current.clone());
    }
    paths
        .into_iter()
        .map(|path| {
            fs::OpenOptions::new()
                .read(true)
                .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE)
                .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
                .open(&path)
                .and_then(|file| {
                    let metadata = file.metadata()?;
                    if metadata.file_attributes() & 0x0400 != 0 || !metadata.is_dir() {
                        Err(std::io::Error::other("directory is not a real directory"))
                    } else {
                        Ok(file)
                    }
                })
                .map_err(|_| "transport analysis ancestry could not be pinned".to_string())
        })
        .collect()
}

/// Opens the leaf through a Windows no-follow handle.  `FILE_FLAG_OPEN_REPARSE_POINT`
/// prevents the open from traversing a substituted leaf; checking attributes on
/// that same handle closes the inspect-then-open race for evidence files.
fn open_private_capture_no_follow(path: &Path) -> Result<File, String> {
    let file = fs::OpenOptions::new()
        .read(true)
        .share_mode(0)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path)
        .map_err(|_| "TSDuck capture artifact cannot be opened")?;
    let metadata = file
        .metadata()
        .map_err(|_| "TSDuck capture artifact cannot be inspected")?;
    if metadata.file_attributes() & 0x0400 != 0 || !metadata.is_file() {
        return Err("TSDuck capture artifact is a reparse point or not a file".into());
    }
    Ok(file)
}

fn open_attested_executable_unverified(path: &Path) -> Result<File, String> {
    let file = fs::OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path)
        .map_err(|_| "attested executable could not be pinned")?;
    let metadata = file
        .metadata()
        .map_err(|_| "attested executable could not be inspected")?;
    if metadata.file_attributes() & 0x0400 != 0 || !metadata.is_file() {
        return Err("attested executable is a reparse point or not a file".into());
    }
    Ok(file)
}

fn open_attested_executable(path: &Path, expected_sha256: &str) -> Result<File, String> {
    let mut file = open_attested_executable_unverified(path)?;
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 8192];
    loop {
        let count = file
            .read(&mut buffer)
            .map_err(|_| "attested executable could not be read")?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    if format!("{:x}", digest.finalize()) != expected_sha256 {
        return Err("attested executable handle does not match its verified identity".into());
    }
    file.seek(SeekFrom::Start(0))
        .map_err(|_| "attested executable could not be rewound")?;
    Ok(file)
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

fn write_quarantine_owner(directory: &Path, job_id: &str) -> Result<(), String> {
    let mut marker = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(directory.join(QUARANTINE_MARKER))
        .map_err(|_| "transport capture ownership marker could not be created")?;
    marker
        .write_all(format!("TownLight transport capture v1\n{job_id}\n").as_bytes())
        .and_then(|()| marker.sync_all())
        .map_err(|_| "transport capture ownership marker could not be sealed".to_string())
}

fn verify_quarantine_owner(directory: &Path, job_id: &str) -> Result<(), String> {
    if read_quarantine_owner(directory)? != job_id {
        return Err("transport quarantine ownership marker is invalid".into());
    }
    Ok(())
}

fn read_quarantine_owner(directory: &Path) -> Result<String, String> {
    let marker = open_private_capture_no_follow(&directory.join(QUARANTINE_MARKER))?;
    let mut bytes = Vec::new();
    marker
        .take(256)
        .read_to_end(&mut bytes)
        .map_err(|_| "transport quarantine ownership marker could not be read")?;
    let text = std::str::from_utf8(&bytes)
        .map_err(|_| "transport quarantine ownership marker is invalid")?;
    let job_id = text
        .strip_prefix("TownLight transport capture v1\n")
        .and_then(|value| value.strip_suffix('\n'))
        .filter(|value| is_canonical_uuid(value))
        .ok_or("transport quarantine ownership marker is invalid")?;
    if bytes != format!("TownLight transport capture v1\n{job_id}\n").as_bytes() {
        return Err("transport quarantine ownership marker is invalid".into());
    }
    Ok(job_id.to_string())
}

fn quarantine_tree_bytes(path: &Path, deadline: Instant) -> Result<u64, String> {
    let mut stack = vec![(path.to_path_buf(), 0_usize)];
    let mut total = 0_u64;
    let mut entries = 0_usize;
    while let Some((candidate, depth)) = stack.pop() {
        if Instant::now() >= deadline {
            return Err("transport quarantine scan exceeded its deadline".into());
        }
        entries = entries
            .checked_add(1)
            .ok_or("transport quarantine entry count overflowed")?;
        if entries > MAX_QUARANTINE_TREE_ENTRIES || depth > MAX_QUARANTINE_TREE_DEPTH {
            return Err("transport quarantine tree exceeds its structural bounds".into());
        }
        let metadata = fs::symlink_metadata(&candidate)
            .map_err(|_| "transport quarantine entry could not be inspected")?;
        if metadata.file_attributes() & 0x0400 != 0 {
            return Err("transport quarantine contains a reparse point".into());
        }
        if metadata.is_file() {
            total = total
                .checked_add(metadata.len())
                .ok_or("transport quarantine size overflowed")?;
            if total > MAX_QUARANTINE_BYTES {
                return Err("transport quarantine scan exceeded its byte bound".into());
            }
        } else if metadata.is_dir() {
            for entry in fs::read_dir(&candidate)
                .map_err(|_| "transport quarantine directory could not be read")?
            {
                stack.push((
                    entry
                        .map_err(|_| "transport quarantine entry could not be read")?
                        .path(),
                    depth + 1,
                ));
            }
        } else {
            return Err("transport quarantine contains an unsupported entry".into());
        }
    }
    Ok(total)
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

fn validate_tstables_json(output: &[u8], findings: &TransportFindings) -> Result<(), String> {
    findings.validate()?;
    if output.is_empty() || output.len() > MAX_PROCESS_OUTPUT_BYTES {
        return Err("TSDuck tables output is missing or oversized".into());
    }
    let tables: Vec<serde_json::Value> =
        serde_json::from_slice(output).map_err(|_| "TSDuck tables output is malformed")?;
    if tables.len() > 128 {
        return Err("TSDuck tables output exceeds its table bound".into());
    }
    let pats: Vec<&serde_json::Value> = tables
        .iter()
        .filter(|table| table["#name"] == "PAT")
        .collect();
    if pats.len() != 1 {
        return Err("TSDuck tables must contain exactly one PAT".into());
    }
    let all_pmts: Vec<&serde_json::Value> = tables
        .iter()
        .filter(|table| table["#name"] == "PMT")
        .collect();
    let pmts: Vec<&serde_json::Value> = all_pmts
        .iter()
        .copied()
        .filter(|table| {
            table["#name"] == "PMT" && table["service_id"].as_u64() == Some(findings.service_id)
        })
        .collect();
    if all_pmts.len() != 1 || pmts.len() != 1 {
        return Err("TSDuck tables must contain exactly one PMT for the selected service".into());
    }
    let pat_nodes = pats[0]["#nodes"]
        .as_array()
        .ok_or("TSDuck PAT nodes are missing")?;
    if pat_nodes.len() > 512 {
        return Err("TSDuck PAT nodes exceed their bound".into());
    }
    let pat_services: Vec<&serde_json::Value> = pat_nodes
        .iter()
        .filter(|node| node["#name"] == "service")
        .collect();
    let pat_matches = pat_services.len() == findings.pat_program_count as usize
        && pat_services.len() == 1
        && pat_services[0]["service_id"].as_u64() == Some(findings.service_id)
        && pat_services[0]["program_map_pid"].as_u64() == Some(findings.pmt_pid);
    let pmt = pmts[0];
    let components = pmt["#nodes"]
        .as_array()
        .ok_or("TSDuck PMT nodes are missing")?;
    if components.len() > 512 {
        return Err("TSDuck PMT nodes exceed their bound".into());
    }
    let stream_components: Vec<&serde_json::Value> = components
        .iter()
        .filter(|node| node["#name"] == "component")
        .collect();
    let pmt_metadata: Vec<&serde_json::Value> = components
        .iter()
        .filter(|node| node["#name"] == "metadata")
        .collect();
    let component = |pid, stream_type| {
        stream_components
            .iter()
            .filter(|node| {
                node["elementary_pid"].as_u64() == Some(pid)
                    && node["stream_type"].as_u64() == Some(stream_type)
            })
            .count()
            == 1
    };
    if !pat_matches
        || pmt_metadata.len() != 1
        || pmt_metadata[0]["pid"].as_u64() != Some(findings.pmt_pid)
        || pmt["pcr_pid"].as_u64() != Some(findings.pcr_pid)
        || findings.pcr_pid != findings.video_pid
        || stream_components.len() != findings.pmt_stream_count as usize
        || stream_components.len() != 2
        || !component(findings.video_pid, 27)
        || !component(findings.audio_pid, 15)
    {
        return Err("TSDuck PAT and PMT tables are inconsistent".into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn verified_candidate(root: &Path) -> (PathBuf, PathBuf) {
        const VERSION: &str = "0.2.0";
        const COMMIT: &str = "0123456789abcdef0123456789abcdef01234567";
        const RUNTIME_SHA: &str =
            "abcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcd";
        const TSDUCK_SHA: &str = "b0ca0f963fcd77488b8c32d6f9d85030daa891ed753c36e57bbb458807a44eb1";
        let data = root.join("data");
        let application = root.join("application");
        let runtime = application.join("runtime/gstreamer");
        let tsduck = application.join("runtime/tsduck");
        fs::create_dir_all(runtime.join("bin")).unwrap();
        fs::create_dir_all(runtime.join("lib/gstreamer-1.0")).unwrap();
        fs::create_dir_all(runtime.join("lib/gst-validate-launcher/python/launcher/apps")).unwrap();
        for (path, bytes) in [
            ("bin/gstreamer.dll", b"runtime".as_slice()),
            ("lib/gstreamer-1.0/plugin.dll", b"plugin".as_slice()),
            (
                "lib/gst-validate-launcher/python/launcher/apps/empty.py",
                b"".as_slice(),
            ),
        ] {
            fs::write(runtime.join(path), bytes).unwrap();
        }
        for (path, bytes) in [
            ("LICENSE.txt", b"license".as_slice()),
            ("OTHERS.txt", b"notices".as_slice()),
            ("bin/tsp.exe", b"tsp".as_slice()),
            ("bin/tsanalyze.exe", b"analyze".as_slice()),
            ("bin/tstables.exe", b"tables".as_slice()),
            ("bin/tsduck.dll", b"tsduck".as_slice()),
            ("bin/tscore.dll", b"tscore".as_slice()),
        ] {
            let destination = tsduck.join(path);
            fs::create_dir_all(destination.parent().unwrap()).unwrap();
            fs::write(destination, bytes).unwrap();
        }
        let runtime_manifest_sha = station_maintenance::write_installed_runtime_manifest(
            &runtime,
            &data.join(station_maintenance::INSTALLED_RUNTIME_MANIFEST_NAME),
            VERSION,
            COMMIT,
            RUNTIME_SHA,
        )
        .unwrap();
        let tsduck_manifest_sha = station_maintenance::write_installed_runtime_manifest(
            &tsduck,
            &data.join(station_maintenance::TSDUCK_RUNTIME_MANIFEST_NAME),
            VERSION,
            COMMIT,
            TSDUCK_SHA,
        )
        .unwrap();
        let files = [
            ("stationd.exe", b"stationd".as_slice()),
            ("channel-worker.exe", b"worker".as_slice()),
        ];
        let manifest = station_update::UpdatePackageManifest {
            schema_version: 2,
            product: "TownLight Station".into(),
            version: VERSION.into(),
            source_commit: COMMIT.into(),
            files: files
                .iter()
                .map(|(path, bytes)| station_update::UpdatePackageFile {
                    path: (*path).into(),
                    size_bytes: bytes.len() as u64,
                    sha256: sha256(bytes),
                })
                .collect(),
        };
        for (path, bytes) in files {
            fs::write(application.join(path), bytes).unwrap();
        }
        fs::write(
            data.join(station_maintenance::ACTIVE_SLOT_RECEIPT_NAME),
            serde_json::json!({"schema_version":1,"generation":7,"activated_at_unix_ms":1,"transaction_id":"bootstrap","active":{"relative_directory":".","package_manifest":manifest},"previous":null}).to_string(),
        ).unwrap();
        fs::write(
            data.join(station_maintenance::INSTALL_RECEIPT_NAME),
            serde_json::json!({"product":"TownLight Station","version":VERSION,"source_commit":COMMIT,"media_runtime_sha256":RUNTIME_SHA,"runtime_manifest_sha256":runtime_manifest_sha,"tsduck_version":"3.44-4676","tsduck_source_url":"https://github.com/tsduck/tsduck/releases/download/v3.44-4676/TSDuck-Win64-3.44-4676-Portable.zip","tsduck_archive_name":"TSDuck-Win64-3.44-4676-Portable.zip","tsduck_runtime_sha256":TSDUCK_SHA,"tsduck_runtime_manifest_sha256":tsduck_manifest_sha,"installed_at_unix_ms":1,"health_uri":"http://127.0.0.1:4070/health"}).to_string(),
        ).unwrap();
        (data, application)
    }

    struct RetainedFixtureRunner;

    impl sealed::Process for RetainedFixtureRunner {}

    impl ProcessRunner for RetainedFixtureRunner {
        fn run(&self, _: &Path, argv: &[String], _: Duration) -> Result<ProcessResult, String> {
            if argv.first().is_some_and(|arg| arg == "--json-output") {
                if argv.get(1).map(String::as_str) != Some("-") {
                    return Err("tables output is not bound to stdout".into());
                }
                Ok(ProcessResult {
                    status_success: true,
                    output: include_bytes!("../tests/fixtures/tsduck-3.44-h264-aac-tables.json")
                        .to_vec(),
                })
            } else {
                Ok(ProcessResult {
                    status_success: true,
                    output: include_bytes!("../tests/fixtures/tsduck-3.44-h264-aac-analysis.txt")
                        .to_vec(),
                })
            }
        }
    }
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
        assert!(
            ReceiverProofExecutor {
                adapter: FakeAuthenticatedReceiver {
                    response: Ok(vec![1])
                },
                adapter_version: "1".into()
            }
            .probe_receipt(&b, &target)
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
        assert_eq!(findings.pmt_stream_count, 2);
        assert_eq!(findings.service_id, 1);
        assert_eq!(findings.pmt_pid, 4096);
        assert_eq!(findings.pcr_pid, 256);
        assert_eq!(findings.video_pid, 256);
        assert_eq!(findings.audio_pid, 257);
    }

    #[test]
    fn validates_the_retained_pat_and_pmt_table_binding() {
        let findings = parse_tsanalyze_json(
            include_bytes!("../tests/fixtures/tsduck-3.44-h264-aac-analysis.txt"),
            53_956,
            2_000,
        )
        .unwrap();
        validate_tstables_json(
            include_bytes!("../tests/fixtures/tsduck-3.44-h264-aac-tables.json"),
            &findings,
        )
        .unwrap();
    }

    #[test]
    fn production_analyzer_path_writes_and_binds_the_retained_tables_artifact() {
        let root = root();
        let capture = root.join("capture.ts");
        fs::write(
            &capture,
            include_bytes!("../tests/fixtures/tsduck-3.44-h264-aac.ts"),
        )
        .unwrap();
        let executor = TsdDuckTransportExecutor {
            runtime: runtime(&root),
            runner: RetainedFixtureRunner,
        };
        let findings = executor
            .analyze_capture(&CaptureRequest {
                udp_host: "127.0.0.1".into(),
                udp_port: 5000,
                duration_ms: 2_000,
                max_bytes: 100_000,
                artifact_path: capture.clone(),
            })
            .unwrap();
        assert_eq!(
            findings.tables_output_sha256,
            sha256(include_bytes!(
                "../tests/fixtures/tsduck-3.44-h264-aac-tables.json"
            ))
        );
        assert!(!capture.with_extension("tables.json").exists());
    }

    #[test]
    fn pinned_analysis_handles_block_leaf_and_ancestor_replacement() {
        let base = root();
        let managed = ManagedCaptureRoot::open(base.join("managed")).unwrap();
        let job_id = "11111111-2222-4333-8444-555555555555";
        let mut partial = managed.begin_job(job_id).unwrap();
        partial.file.write_all(&[0x47; 188]).unwrap();
        let (facts, bytes) = managed
            .seal_capture(&mut partial, job_id, 1, 600_001, 1_024)
            .unwrap();
        let input = managed.write_analysis_copy(job_id, &facts, &bytes).unwrap();
        assert!(fs::rename(&input.path, input.path.with_extension("replaced")).is_err());
        let job_directory = input.path.parent().unwrap();
        assert!(fs::rename(job_directory, job_directory.with_extension("replaced")).is_err());
        assert!(
            fs::OpenOptions::new()
                .write(true)
                .open(&input.path)
                .is_err()
        );
        drop(input);
        drop(partial);
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn quarantine_recovery_removes_only_owned_tombstones_and_rejects_unowned_trees() {
        let base = root();
        let managed = ManagedCaptureRoot::open(base.join("managed")).unwrap();
        let job_id = "11111111-2222-4333-8444-555555555555";
        let mut partial = managed.begin_job(job_id).unwrap();
        partial.file.write_all(b"partial").unwrap();
        drop(partial);
        let job = managed.root.join(job_id);
        let tombstone = managed.root.join(".deleting-test-owned");
        fs::rename(&job, &tombstone).unwrap();
        managed.cleanup_quarantine().unwrap();
        assert!(!tombstone.exists());

        let unowned = managed
            .root
            .join("22222222-3333-4444-8555-666666666666.quarantine-hostile");
        fs::create_dir(&unowned).unwrap();
        assert!(
            managed.cleanup_quarantine().is_err(),
            "an unmarked lookalike must never be recursively deleted"
        );
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn private_controller_authority_completes_exact_bound_evidence() {
        let base = root();
        let (data, application) = verified_candidate(&base);
        let store = StationStore::open(data.join("station.db")).unwrap();
        let channel_id = "11111111-2222-4333-8444-555555555555";
        let sink_id = "22222222-3333-4444-8555-666666666666";
        store
            .put_channel(&station_schedule::ChannelConfiguration {
                channel_id: channel_id.into(),
                display_name: "Transport controller".into(),
                output_profile: station_schedule::OutputProfileId::Proof360p30,
                output_sinks: vec![station_schedule::OutputSinkConfiguration::required_udp(
                    sink_id,
                    "Proof output",
                    "127.0.0.1:5500",
                    -16.0,
                )],
                enabled: true,
            })
            .unwrap();
        store
            .write_transport_target_config(
                channel_id,
                sink_id,
                None,
                &TargetDeviceConfig {
                    target_id: "receiver-1".into(),
                    label: "Receiver".into(),
                    protocol: TargetProtocol::HttpsApi,
                    host: "receiver.example.test".into(),
                    port: 443,
                    credential_configured: true,
                    revision: 1,
                    created_at_unix_ms: 1,
                    updated_at_unix_ms: 1,
                    updated_by: "setup-admin".into(),
                },
            )
            .unwrap();
        let job = station_storage::TransportProofJob {
            job_id: "33333333-4444-4555-8666-777777777777".into(),
            channel_id: channel_id.into(),
            sink_id: sink_id.into(),
            target_id: "receiver-1".into(),
            target_revision: 1,
            candidate_version: "0.2.0".into(),
            source_commit: "0123456789abcdef0123456789abcdef01234567".into(),
            install_generation: Some(7),
            configuration_hash: store
                .current_channel_output_configuration_hash(channel_id)
                .unwrap(),
            requested_by: "setup-admin".into(),
            created_at_unix_ms: 1,
            max_capture_bytes: 100_000,
        };
        assert!(
            store
                .enqueue_transport_proof_job(&job, || {
                    station_maintenance::read_verified_install_authority(&data, &application)
                })
                .unwrap()
        );
        let execution = store
            .claim_next_transport_proof_job(2, 1_200_000)
            .unwrap()
            .unwrap();
        let capture = TransportProofCaptureFacts {
            artifact_relative_path: format!("{}/capture.ts", job.job_id),
            capture_bytes: 53_956,
            capture_sha256: "a".repeat(64),
            started_at_unix_ms: 1,
            ended_at_unix_ms: 600_001,
        };
        store
            .begin_transport_proof_analysis(&execution, &capture, 600_001, 1_200_000)
            .unwrap();
        let mut findings = parse_tsanalyze_json(
            include_bytes!("../tests/fixtures/tsduck-3.44-h264-aac-analysis.txt"),
            capture.capture_bytes,
            600_000,
        )
        .unwrap();
        findings.tables_output_sha256 = sha256(include_bytes!(
            "../tests/fixtures/tsduck-3.44-h264-aac-tables.json"
        ));
        let receipt = TransportAnalysisReceipt {
            proof_id: job.job_id.clone(),
            channel_id: channel_id.into(),
            sink_id: sink_id.into(),
            target_id: "receiver-1".into(),
            target_revision: 1,
            candidate_version: job.candidate_version.clone(),
            source_commit: job.source_commit.clone(),
            configuration_hash: job.configuration_hash.clone(),
            analyzer_id: TSDUCK_ANALYZER_ID.into(),
            analyzer_version: "3.44-4676".into(),
            analyzer_sha256: "b".repeat(64),
            capture_bytes: capture.capture_bytes,
            capture_sha256: capture.capture_sha256.clone(),
            outcome: ProofOutcome::Pass,
            findings,
            captured_at_unix_ms: capture.ended_at_unix_ms,
            recorded_at_unix_ms: 600_002,
            recorded_by: "stationd-transport-proof-controller".into(),
            detail: "Bounded TSDuck transport analysis passed.".into(),
        };
        let reachability = TargetDeviceReachabilityReceipt {
            proof_id: job.job_id.clone(),
            channel_id: channel_id.into(),
            sink_id: sink_id.into(),
            target_id: "receiver-1".into(),
            target_revision: 1,
            candidate_version: job.candidate_version.clone(),
            source_commit: job.source_commit.clone(),
            configuration_hash: job.configuration_hash.clone(),
            adapter_id: RECEIVER_ADAPTER_ID.into(),
            adapter_version: "1".into(),
            auth_method: "configured_https_credential".into(),
            outcome: ProofOutcome::Pass,
            latency_ms: 1,
            response_identity_sha256: "c".repeat(64),
            observed_at_unix_ms: 600_002,
            recorded_at_unix_ms: 600_002,
            recorded_by: "stationd-transport-proof-controller".into(),
            detail: "Authenticated receiver reachability passed.".into(),
        };
        let _fence = station_maintenance::acquire_install_authority_fence(&data).unwrap();
        store
            .retain_transport_proof_tables_output(
                &execution,
                &receipt.findings.tables_output_sha256,
                600_002,
            )
            .unwrap();
        let mut unrelated_tables = receipt.clone();
        unrelated_tables.findings.tables_output_sha256 = "d".repeat(64);
        assert!(
            store
                .complete_transport_proof_job_with_analysis(
                    &execution,
                    &unrelated_tables,
                    &reachability,
                    600_002,
                    || station_maintenance::read_verified_install_authority(&data, &application),
                )
                .is_err(),
            "a valid-looking receipt must bind to the tables evidence retained for this capture"
        );
        assert_eq!(
            store
                .read_transport_proof_job(&job.job_id)
                .unwrap()
                .unwrap()
                .state,
            station_storage::TransportProofJobState::Analyzing
        );
        let mut unrelated_capture = receipt.clone();
        unrelated_capture.capture_sha256 = "e".repeat(64);
        assert!(
            store
                .complete_transport_proof_job_with_analysis(
                    &execution,
                    &unrelated_capture,
                    &reachability,
                    600_002,
                    || station_maintenance::read_verified_install_authority(&data, &application),
                )
                .is_err(),
            "a valid-looking receipt must not complete for unrelated capture bytes"
        );
        let mut unrelated_duration = receipt.clone();
        unrelated_duration.findings.capture_duration_ms = 599_999;
        assert!(
            store
                .complete_transport_proof_job_with_analysis(
                    &execution,
                    &unrelated_duration,
                    &reachability,
                    600_002,
                    || station_maintenance::read_verified_install_authority(&data, &application),
                )
                .is_err(),
            "a receipt duration must be measured from the sealed capture facts"
        );
        let completed = store
            .complete_transport_proof_job_with_analysis(
                &execution,
                &receipt,
                &reachability,
                600_002,
                || station_maintenance::read_verified_install_authority(&data, &application),
            )
            .unwrap();
        assert_eq!(
            completed.state,
            station_storage::TransportProofJobState::Completed
        );
        assert_eq!(
            completed.analyzer_output_sha256,
            Some(receipt.findings.analyzer_output_sha256)
        );
        assert_eq!(
            completed.tables_output_sha256,
            Some(receipt.findings.tables_output_sha256)
        );
        drop(store);
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn analyzer_heartbeats_before_and_after_each_bounded_process_and_teardown() {
        let root = root();
        let capture = root.join("capture.ts");
        fs::write(
            &capture,
            include_bytes!("../tests/fixtures/tsduck-3.44-h264-aac.ts"),
        )
        .unwrap();
        let executor = TsdDuckTransportExecutor {
            runtime: runtime(&root),
            runner: RetainedFixtureRunner,
        };
        let mut heartbeats = 0_u32;
        executor
            .analyze_capture_with_heartbeat(
                &CaptureRequest {
                    udp_host: "127.0.0.1".into(),
                    udp_port: 5000,
                    duration_ms: 2_000,
                    max_bytes: 100_000,
                    artifact_path: capture,
                },
                &mut || {
                    heartbeats += 1;
                    Ok(())
                },
            )
            .unwrap();
        assert!(
            heartbeats >= 6,
            "both analyzers and table-file teardown renew"
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn assignment_failure_explicitly_reaps_the_uncontained_child() {
        let _guard = RUNNER_FAULT_TEST_LOCK.lock().unwrap();
        FORCE_JOB_ASSIGN_FAILURE.store(true, Ordering::Release);
        let executable = std::env::current_exe().unwrap();
        let result = RealProcessRunner.run(&executable, &["--help".into()], Duration::from_secs(1));
        FORCE_JOB_ASSIGN_FAILURE.store(false, Ordering::Release);
        assert!(result.is_err());
    }

    #[test]
    fn real_runner_post_assignment_failures_leave_no_owned_drains() {
        let _guard = RUNNER_FAULT_TEST_LOCK.lock().unwrap();
        let executable = std::env::var_os("COMSPEC")
            .map(PathBuf::from)
            .expect("Windows command interpreter is available");
        for (point, expected) in [
            (1, "stdout pipe"),
            (2, "stderr pipe"),
            (3, "status could not be read"),
        ] {
            FORCED_RUNNER_FAILURE_POINT.store(point, Ordering::Release);
            let result = RealProcessRunner.run(
                &executable,
                &["/C".into(), "ping 127.0.0.1 -n 30".into()],
                Duration::from_secs(1),
            );
            FORCED_RUNNER_FAILURE_POINT.store(0, Ordering::Release);
            assert!(
                result
                    .as_ref()
                    .err()
                    .is_some_and(|error| error.contains(expected)),
                "unexpected injected runner result at point {point}: {result:?}"
            );
            assert_eq!(ACTIVE_PROCESS_DRAINS.load(Ordering::Acquire), 0);
        }
    }

    #[test]
    fn real_runner_heartbeat_failures_finalize_before_return() {
        let _guard = RUNNER_FAULT_TEST_LOCK.lock().unwrap();
        let executable = std::env::var_os("COMSPEC")
            .map(PathBuf::from)
            .expect("Windows command interpreter is available");
        for (command, timeout) in [
            ("exit 0", Duration::from_secs(1)),
            ("ping 127.0.0.1 -n 30", Duration::from_millis(20)),
        ] {
            let mut calls = 0_u8;
            let result = RealProcessRunner.run_with_heartbeat(
                &executable,
                &["/C".into(), command.into()],
                timeout,
                &mut || {
                    calls = calls.saturating_add(1);
                    if calls > 1 {
                        Err("injected heartbeat failure".into())
                    } else {
                        Ok(())
                    }
                },
            );
            assert_eq!(result.unwrap_err(), "injected heartbeat failure");
            assert_eq!(ACTIVE_PROCESS_DRAINS.load(Ordering::Acquire), 0);
        }
    }

    #[test]
    fn real_runner_joins_sibling_after_one_drain_panics() {
        let _guard = RUNNER_FAULT_TEST_LOCK.lock().unwrap();
        let executable = std::env::var_os("COMSPEC")
            .map(PathBuf::from)
            .expect("Windows command interpreter is available");
        FORCED_RUNNER_FAILURE_POINT.store(4, Ordering::Release);
        FORCED_STDERR_DRAIN_DELAY_MS.store(150, Ordering::Release);
        let started = Instant::now();
        let result = RealProcessRunner.run(
            &executable,
            &["/C".into(), "ping 127.0.0.1 -n 30".into()],
            Duration::from_millis(20),
        );
        FORCED_RUNNER_FAILURE_POINT.store(0, Ordering::Release);
        assert_eq!(result.unwrap_err(), "TSDuck process exceeded its deadline");
        assert!(
            started.elapsed() >= Duration::from_millis(150),
            "the runner returned before joining the delayed stderr sibling"
        );
        assert_eq!(
            ACTIVE_PROCESS_DRAINS.load(Ordering::Acquire),
            0,
            "the live stderr sibling must be joined before the stdout panic is surfaced"
        );
    }

    #[test]
    fn capture_timeout_terminates_and_joins_its_stderr_drain() {
        let root = root();
        let capture = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(root.join("capture.ts"))
            .unwrap();
        let command = std::env::var_os("COMSPEC")
            .map(PathBuf::from)
            .expect("Windows command interpreter is available");
        // Keep stderr open well past the runner deadline. The test would hang
        // or retain its drain thread if the timeout path merely returned.
        FORCED_CANCEL_FAILURES.store(2, Ordering::Release);
        FORCED_CAPTURE_DRAIN_DELAY_MS.store(5_200, Ordering::Release);
        let result = RealProcessRunner.run_capture_to_handle(
            &command,
            &["/C".into(), "ping 127.0.0.1 -n 30 1>&2".into()],
            Duration::from_millis(20),
            &capture,
        );
        assert!(
            result
                .as_ref()
                .err()
                .is_some_and(|error| error.contains("deadline")),
            "unexpected capture result: {result:?}"
        );
        assert_eq!(
            FORCED_CANCEL_FAILURES.load(Ordering::Acquire),
            0,
            "the real collector must retry injected cancellation failures"
        );
        // A second open proves the child and its inherited output handle have
        // been closed before this function returned.
        drop(capture);
        fs::OpenOptions::new()
            .write(true)
            .open(root.join("capture.ts"))
            .unwrap();
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn rejects_ambiguous_or_mismatched_tables_for_the_selected_service() {
        let findings = parse_tsanalyze_json(
            include_bytes!("../tests/fixtures/tsduck-3.44-h264-aac-analysis.txt"),
            53_956,
            2_000,
        )
        .unwrap();
        let mut tables: Vec<serde_json::Value> = serde_json::from_slice(include_bytes!(
            "../tests/fixtures/tsduck-3.44-h264-aac-tables.json"
        ))
        .unwrap();
        tables.push(tables[1].clone());
        assert!(validate_tstables_json(&serde_json::to_vec(&tables).unwrap(), &findings).is_err());
        let mut tables: Vec<serde_json::Value> = serde_json::from_slice(include_bytes!(
            "../tests/fixtures/tsduck-3.44-h264-aac-tables.json"
        ))
        .unwrap();
        tables[2]["pcr_pid"] = serde_json::json!(257);
        assert!(validate_tstables_json(&serde_json::to_vec(&tables).unwrap(), &findings).is_err());

        let mut tables: Vec<serde_json::Value> = serde_json::from_slice(include_bytes!(
            "../tests/fixtures/tsduck-3.44-h264-aac-tables.json"
        ))
        .unwrap();
        tables[1]["#nodes"]
            .as_array_mut()
            .unwrap()
            .push(serde_json::json!({
                "#name": "service",
                "program_map_pid": 4097,
                "service_id": 2
            }));
        assert!(validate_tstables_json(&serde_json::to_vec(&tables).unwrap(), &findings).is_err());

        let mut tables: Vec<serde_json::Value> = serde_json::from_slice(include_bytes!(
            "../tests/fixtures/tsduck-3.44-h264-aac-tables.json"
        ))
        .unwrap();
        tables[2]["#nodes"]
            .as_array_mut()
            .unwrap()
            .push(serde_json::json!({
                "#name": "component",
                "elementary_pid": 258,
                "stream_type": 15
            }));
        assert!(validate_tstables_json(&serde_json::to_vec(&tables).unwrap(), &findings).is_err());

        let mut inconsistent = findings.clone();
        inconsistent.pmt_stream_count = 3;
        assert!(
            validate_tstables_json(
                include_bytes!("../tests/fixtures/tsduck-3.44-h264-aac-tables.json"),
                &inconsistent
            )
            .is_err()
        );

        let mut tables: Vec<serde_json::Value> = serde_json::from_slice(include_bytes!(
            "../tests/fixtures/tsduck-3.44-h264-aac-tables.json"
        ))
        .unwrap();
        tables[2]["#nodes"][0]["pid"] = serde_json::json!(4097);
        assert!(
            validate_tstables_json(&serde_json::to_vec(&tables).unwrap(), &findings).is_err(),
            "the PMT must be carried on the exact PID named by the selected PAT service"
        );

        let mut invalid_pcr = findings.clone();
        invalid_pcr.pcr_pid = 0;
        invalid_pcr.video_pid = 0;
        let mut tables: Vec<serde_json::Value> = serde_json::from_slice(include_bytes!(
            "../tests/fixtures/tsduck-3.44-h264-aac-tables.json"
        ))
        .unwrap();
        tables[2]["pcr_pid"] = serde_json::json!(0);
        tables[2]["#nodes"][1]["elementary_pid"] = serde_json::json!(0);
        assert!(
            validate_tstables_json(&serde_json::to_vec(&tables).unwrap(), &invalid_pcr).is_err(),
            "a table correlation must reject the reserved PCR PID zero"
        );
    }

    #[test]
    fn rejects_extra_analyzer_programs_and_components() {
        let fixture = include_bytes!("../tests/fixtures/tsduck-3.44-h264-aac-analysis.txt");
        let json_start = fixture.iter().position(|byte| *byte == b'{').unwrap();
        let mut report: serde_json::Value = serde_json::from_slice(&fixture[json_start..]).unwrap();
        let duplicate_service = report["services"][0].clone();
        report["services"]
            .as_array_mut()
            .unwrap()
            .push(duplicate_service);
        assert!(
            parse_tsanalyze_json(&serde_json::to_vec(&report).unwrap(), 53_956, 2_000).is_err()
        );

        let mut report: serde_json::Value = serde_json::from_slice(&fixture[json_start..]).unwrap();
        report["pids"]
            .as_array_mut()
            .unwrap()
            .push(serde_json::json!({
                "audio": false,
                "description": "private component",
                "id": 258,
                "packets": {"discontinuities": 0, "pcr-leap": 0, "total": 1},
                "pmt": false,
                "services": [1],
                "video": false
            }));
        assert!(
            parse_tsanalyze_json(&serde_json::to_vec(&report).unwrap(), 53_956, 2_000).is_err()
        );
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
        let mut partial = managed.begin_job(job_id).unwrap();
        assert!(fs::rename(&partial.path, partial.path.with_extension("hostile")).is_err());
        let job_directory = partial.path.parent().unwrap();
        assert!(fs::rename(job_directory, job_directory.with_extension("hostile")).is_err());
        assert!(
            fs::OpenOptions::new()
                .write(true)
                .open(&partial.path)
                .is_err()
        );
        partial.file.write_all(&[0x47; 188]).unwrap();
        let (facts, bytes) = managed
            .seal_capture(&mut partial, job_id, 1_000, 601_000, 1_024)
            .unwrap();
        assert_eq!(facts.artifact_relative_path, format!("{job_id}/capture.ts"));
        assert_eq!(facts.capture_bytes, 188);
        assert_eq!(facts.capture_sha256, sha256(&bytes));
        drop(partial);
        let retry = managed.begin_job(job_id).unwrap();
        assert!(retry.path.ends_with("capture.ts"));
        drop(retry);
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
