use super::*;
use station_automation_policy::{AutomationExecutionReceipt, AutomationExecutionRequest};
use station_domain::{AlertConditionKind, AlertEvent, AlertEventState, AlertSeverity};
use station_maintenance::VerifiedInstallAuthority;
use std::ffi::OsString;
use std::io::Read;
use std::os::windows::io::AsRawHandle;
use std::path::Path;
use std::process::{Child, ChildStdout, Command, Stdio};
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use windows_sys::Win32::Foundation::{CloseHandle, HANDLE, INVALID_HANDLE_VALUE};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JobObjectExtendedLimitInformation,
    SetInformationJobObject, TerminateJobObject,
};

const AUTOMATION_EXECUTION_WORKER_ID: &str = "stationd-automation-execution";
const AUTOMATION_EXECUTION_LEASE_MS: i64 = 60_000;
const AUTOMATION_EXECUTION_RENEWAL_LEAD_MS: i64 = AUTOMATION_EXECUTION_LEASE_MS / 2;
const AUTOMATION_EXECUTION_POLL_INTERVAL: Duration = Duration::from_millis(250);
const AUTOMATION_EXECUTION_MAX_BACKOFF: Duration = Duration::from_secs(5);
const AUTOMATION_EXECUTION_CHILD_POLL_INTERVAL: Duration = Duration::from_millis(10);
const MAX_AUTOMATION_EXECUTION_STDOUT_BYTES: u64 = 1_048_576;
const MAX_AUTOMATION_EXECUTION_REQUEST_ENV_BYTES: usize = 32_768;
static NEVER_STOP_AUTOMATION_EXECUTION: AtomicBool = AtomicBool::new(false);

/// A typed adapter outcome deliberately carries only an approved categorical
/// code. Raw provider errors, prompts, and response bodies never become
/// durable execution authority.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AutomationExecutionAdapterFailure {
    RetryableTimeout,
    RetryableUnavailable,
    PermanentRejected,
    PermanentInvalidResponse,
}

impl AutomationExecutionAdapterFailure {
    fn code(self) -> &'static str {
        match self {
            Self::RetryableTimeout => "adapter_timeout",
            Self::RetryableUnavailable => "adapter_unavailable",
            Self::PermanentRejected => "adapter_rejected",
            Self::PermanentInvalidResponse => "invalid_provider_response",
        }
    }

    fn retryable(self) -> bool {
        matches!(self, Self::RetryableTimeout | Self::RetryableUnavailable)
    }
}

/// A declarative, installed worker program.  This is intentionally data, not
/// a callback interface: adapter code has no synchronous `poll` hook through
/// which it can hold stationd, its queue lease, or the install fence hostage.
/// The controller starts, observes, and terminates the one process tree it
/// owns.  There is currently no production program registered; that remains a
/// truthful `adapter_unavailable` retry rather than fabricated execution.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AutomationExecutionAdapter {
    adapter_id: String,
    executable: std::path::PathBuf,
    arguments: Vec<OsString>,
}

impl AutomationExecutionAdapter {
    pub fn new(
        adapter_id: impl Into<String>,
        executable: impl Into<std::path::PathBuf>,
        arguments: Vec<OsString>,
    ) -> Result<Self, String> {
        let adapter_id = adapter_id.into();
        let executable = executable.into();
        if !is_canonical_uuid(&adapter_id) || !executable.is_absolute() || arguments.len() > 64 {
            return Err(
                "automation execution program specification is invalid or unbounded".into(),
            );
        }
        if arguments.iter().any(|argument| argument.len() > 8_192) {
            return Err("automation execution program argument is oversized".into());
        }
        Ok(Self {
            adapter_id,
            executable,
            arguments,
        })
    }

    pub fn adapter_id(&self) -> &str {
        &self.adapter_id
    }
}

struct WindowsProcessTree(HANDLE);

impl WindowsProcessTree {
    fn new() -> Result<Self, AutomationExecutionAdapterFailure> {
        let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if handle == INVALID_HANDLE_VALUE || handle.is_null() {
            return Err(AutomationExecutionAdapterFailure::RetryableUnavailable);
        }
        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let configured = unsafe {
            SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                u32::try_from(std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>())
                    .expect("automation execution job limits fit u32"),
            )
        };
        if configured == 0 {
            unsafe { CloseHandle(handle) };
            return Err(AutomationExecutionAdapterFailure::RetryableUnavailable);
        }
        Ok(Self(handle))
    }

    fn assign(&self, child: &Child) -> Result<(), AutomationExecutionAdapterFailure> {
        if unsafe { AssignProcessToJobObject(self.0, child.as_raw_handle() as HANDLE) } == 0 {
            return Err(AutomationExecutionAdapterFailure::RetryableUnavailable);
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

/// The only execution resource the controller supervises.  It is created by
/// the host after it has acquired the durable claim, placed in a fresh Windows
/// Job Object immediately, and never exposes a blocking adapter callback.
struct OwnedAutomationExecutionProcess {
    child: Child,
    job: WindowsProcessTree,
    stdout: Option<ChildStdout>,
}

impl OwnedAutomationExecutionProcess {
    fn spawn(
        adapter: &AutomationExecutionAdapter,
        request: &AutomationExecutionRequest,
        claim_id: &str,
        deadline_unix_ms: i64,
    ) -> Result<Self, AutomationExecutionAdapterFailure> {
        let request_json = serde_json::to_string(request)
            .map_err(|_| AutomationExecutionAdapterFailure::PermanentInvalidResponse)?;
        if request_json.len() > MAX_AUTOMATION_EXECUTION_REQUEST_ENV_BYTES {
            return Err(AutomationExecutionAdapterFailure::PermanentInvalidResponse);
        }
        let job = WindowsProcessTree::new()?;
        let mut child = Command::new(&adapter.executable)
            .args(&adapter.arguments)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .env("TOWNLIGHT_AUTOMATION_EXECUTION_REQUEST_JSON", request_json)
            .env("TOWNLIGHT_AUTOMATION_EXECUTION_CLAIM_ID", claim_id)
            .env(
                "TOWNLIGHT_AUTOMATION_EXECUTION_DEADLINE_UNIX_MS",
                deadline_unix_ms.to_string(),
            )
            .spawn()
            .map_err(|_| AutomationExecutionAdapterFailure::RetryableUnavailable)?;
        if let Err(error) = job.assign(&child) {
            let _ = child.kill();
            let _ = child.wait();
            return Err(error);
        }
        let stdout = child
            .stdout
            .take()
            .ok_or(AutomationExecutionAdapterFailure::PermanentInvalidResponse)?;
        Ok(Self {
            child,
            job,
            stdout: Some(stdout),
        })
    }

    fn try_receipt(
        &mut self,
    ) -> Result<Option<AutomationExecutionReceipt>, AutomationExecutionAdapterFailure> {
        let Some(status) = self
            .child
            .try_wait()
            .map_err(|_| AutomationExecutionAdapterFailure::RetryableUnavailable)?
        else {
            return Ok(None);
        };
        // The child has exited, but descendants might retain stdout.  They are
        // all inside this exact Job Object, so revoke them before the bounded
        // read; no unowned process is ever targeted.
        self.job.terminate();
        let _ = self.child.wait();
        if !status.success() {
            return Err(AutomationExecutionAdapterFailure::PermanentRejected);
        }
        let mut stdout = self
            .stdout
            .take()
            .ok_or(AutomationExecutionAdapterFailure::PermanentInvalidResponse)?;
        let mut bytes = Vec::new();
        stdout
            .by_ref()
            .take(MAX_AUTOMATION_EXECUTION_STDOUT_BYTES + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| AutomationExecutionAdapterFailure::RetryableUnavailable)?;
        if bytes.len() as u64 > MAX_AUTOMATION_EXECUTION_STDOUT_BYTES {
            return Err(AutomationExecutionAdapterFailure::PermanentInvalidResponse);
        }
        let receipt = serde_json::from_slice(&bytes)
            .map_err(|_| AutomationExecutionAdapterFailure::PermanentInvalidResponse)?;
        Ok(Some(receipt))
    }

    fn terminate(&mut self) {
        self.job.terminate();
        let _ = self.child.wait();
        let _ = self.stdout.take();
    }
}

/// Runs one bounded queue decision. The store performs the authoritative
/// candidate, policy, adapter, configuration, connection-evidence, lease, and
/// receipt checks inside durable transactions; this controller never converts
/// an adapter return into success without that final commit.
pub fn run_automation_execution_controller_iteration(
    store: &StationStore,
    adapter: Option<&AutomationExecutionAdapter>,
    install_authority: &VerifiedInstallAuthority,
    worker_id: &str,
    clock: &mut dyn FnMut() -> Result<i64, String>,
) -> Result<bool, String> {
    run_automation_execution_controller_iteration_with_stop(
        store,
        adapter,
        install_authority,
        worker_id,
        clock,
        &NEVER_STOP_AUTOMATION_EXECUTION,
    )
}

pub fn run_automation_execution_controller_iteration_with_stop(
    store: &StationStore,
    adapter: Option<&AutomationExecutionAdapter>,
    install_authority: &VerifiedInstallAuthority,
    worker_id: &str,
    clock: &mut dyn FnMut() -> Result<i64, String>,
    stop: &AtomicBool,
) -> Result<bool, String> {
    let claimed_at_unix_ms = clock()?;
    let Some(claimed) = store.claim_next_automation_execution_job(
        worker_id,
        install_authority,
        claimed_at_unix_ms,
        AUTOMATION_EXECUTION_LEASE_MS,
    )?
    else {
        return Ok(false);
    };
    let mut deadline_unix_ms = claimed
        .lease_expires_at_unix_ms
        .ok_or_else(|| "claimed automation execution job lacks a deadline".to_string())?;
    let claim_id = claimed
        .claim_id
        .as_deref()
        .ok_or_else(|| "claimed automation execution job lacks a claim identity".to_string())?;

    let outcome = match adapter.filter(|adapter| adapter.adapter_id() == claimed.request.adapter_id)
    {
        Some(adapter) => {
            let action_started_at_unix_ms = clock()?;
            if action_started_at_unix_ms >= deadline_unix_ms || stop.load(Ordering::Acquire) {
                Err(AutomationExecutionAdapterFailure::RetryableTimeout)
            } else {
                (|| -> Result<Result<AutomationExecutionReceipt, AutomationExecutionAdapterFailure>, String> {
                    let mut process = match OwnedAutomationExecutionProcess::spawn(
                        adapter,
                        &claimed.request,
                        claim_id,
                        deadline_unix_ms,
                    ) {
                        Ok(process) => process,
                        Err(failure) => return Ok(Err(failure)),
                    };
                    let mut renewal_at_unix_ms = deadline_unix_ms
                        .checked_sub(AUTOMATION_EXECUTION_RENEWAL_LEAD_MS)
                        .ok_or_else(|| "automation execution renewal deadline overflowed".to_string())?;
                    loop {
                        if stop.load(Ordering::Acquire) {
                            process.terminate();
                            break Ok(Err(AutomationExecutionAdapterFailure::RetryableTimeout));
                        }
                        let now_unix_ms = clock()?;
                        if now_unix_ms >= deadline_unix_ms {
                            process.terminate();
                            break Ok(Err(AutomationExecutionAdapterFailure::RetryableTimeout));
                        }
                        if now_unix_ms >= renewal_at_unix_ms {
                            let renewed = store.renew_automation_execution_job_lease(
                                &claimed.request.job_id,
                                worker_id,
                                claim_id,
                                install_authority,
                                now_unix_ms,
                                AUTOMATION_EXECUTION_LEASE_MS,
                            )?;
                            deadline_unix_ms = renewed.lease_expires_at_unix_ms.ok_or_else(
                                || "renewed automation execution job lacks a deadline".to_string(),
                            )?;
                            renewal_at_unix_ms = deadline_unix_ms
                                .checked_sub(AUTOMATION_EXECUTION_RENEWAL_LEAD_MS)
                                .ok_or_else(|| "automation execution renewal deadline overflowed".to_string())?;
                        }
                        match process.try_receipt() {
                            Ok(Some(receipt)) => break Ok(Ok(receipt)),
                            Ok(None) => std::thread::sleep(AUTOMATION_EXECUTION_CHILD_POLL_INTERVAL),
                            Err(failure) => break Ok(Err(failure)),
                        }
                    }
                })()?
            }
        }
        None => Err(AutomationExecutionAdapterFailure::RetryableUnavailable),
    };

    let finished_at_unix_ms = clock()?;
    if finished_at_unix_ms >= deadline_unix_ms {
        // The owned process tree was already terminated at the deadline.  Do
        // not let an expired C1 write a failure after another controller may
        // have reclaimed C2; the next durable claim transaction recovers it.
        return Ok(true);
    }
    match outcome {
        Ok(receipt) => {
            store.complete_automation_execution_job(
                &claimed.request.job_id,
                worker_id,
                &receipt,
                install_authority,
                finished_at_unix_ms,
            )?;
            Ok(true)
        }
        Err(failure) => {
            store.record_automation_execution_attempt_failure(
                &claimed.request.job_id,
                worker_id,
                claim_id,
                failure.code(),
                failure.retryable(),
                finished_at_unix_ms,
            )?;
            Ok(true)
        }
    }
}

pub fn run_automation_execution_queue_controller(
    database: &Path,
    application_root: &Path,
    stop: Arc<AtomicBool>,
) -> Result<(), String> {
    let mut backoff = AUTOMATION_EXECUTION_POLL_INTERVAL;
    while !stop.load(Ordering::Acquire) {
        match run_automation_execution_queue_iteration(database, application_root, &stop) {
            Ok(_) => {
                resolve_automation_execution_controller_alert(database);
                backoff = AUTOMATION_EXECUTION_POLL_INTERVAL;
            }
            Err(_) => {
                observe_automation_execution_controller_alert(database);
                backoff = (backoff * 2).min(AUTOMATION_EXECUTION_MAX_BACKOFF);
            }
        }
        std::thread::sleep(backoff);
    }
    Ok(())
}

const AUTOMATION_EXECUTION_CONTROLLER_ALERT_RESOURCE: &str = "automation-execution-controller";

fn observe_automation_execution_controller_alert(database: &Path) {
    let Ok(now) = unix_time_millis() else {
        return;
    };
    let Ok(store) = StationStore::open(database) else {
        return;
    };
    // This durable health event deliberately excludes provider, database,
    // prompt, credential, and filesystem error detail. It tells the operator
    // which controller is unhealthy while bounded retry supervision continues.
    let _ = store.observe_alert_and_route(&AlertEvent {
        event_id: "automation-execution-controller-service-down".into(),
        condition: AlertConditionKind::ServiceDown,
        severity: AlertSeverity::Warning,
        state: AlertEventState::Firing,
        resource_ref: AUTOMATION_EXECUTION_CONTROLLER_ALERT_RESOURCE.into(),
        summary: "Automation execution controller needs operator attention.".into(),
        detail: "The controller could not complete a durable queue iteration and is retrying with bounded backoff.".into(),
        source_section: "S13".into(),
        first_observed_at_unix_ms: now,
        last_observed_at_unix_ms: now,
        resolved_at_unix_ms: None,
        occurrence_count: 1,
        acknowledged_at_unix_ms: None,
        acknowledged_by: None,
    });
}

fn resolve_automation_execution_controller_alert(database: &Path) {
    let Ok(now) = unix_time_millis() else {
        return;
    };
    let Ok(store) = StationStore::open(database) else {
        return;
    };
    let _ = store.resolve_alert(
        AlertConditionKind::ServiceDown,
        AUTOMATION_EXECUTION_CONTROLLER_ALERT_RESOURCE,
        now,
    );
}

fn run_automation_execution_queue_iteration(
    database: &Path,
    application_root: &Path,
    stop: &AtomicBool,
) -> Result<bool, String> {
    let data_root = database
        .parent()
        .ok_or_else(|| "automation execution data root is unavailable".to_string())?;
    let fence = station_maintenance::acquire_install_authority_fence(data_root)
        .map_err(|_| "automation execution install authority is busy".to_string())?;
    if fence.was_abandoned() {
        return Err("automation execution install authority requires recovery".into());
    }
    let install_authority =
        station_maintenance::read_verified_install_authority(data_root, application_root)?;
    let store = StationStore::open(database)?;
    let mut clock = unix_time_millis;
    // No production adapter is registered in this slice. The controller still
    // claims current durable work and records an exact retryable unavailable
    // attempt, rather than inventing model, provider, output, or receipt truth.
    run_automation_execution_controller_iteration_with_stop(
        &store,
        None,
        &install_authority,
        AUTOMATION_EXECUTION_WORKER_ID,
        &mut clock,
        stop,
    )
}

fn unix_time_millis() -> Result<i64, String> {
    let elapsed = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| "automation execution clock is before the Unix epoch".to_string())?;
    i64::try_from(elapsed.as_millis())
        .map_err(|_| "automation execution clock exceeds supported range".to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static TEST_SEQUENCE: AtomicU64 = AtomicU64::new(0);

    #[test]
    fn controller_authority_failure_alert_is_durable_and_resolves() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("test clock is after epoch")
            .as_nanos();
        let database = std::env::temp_dir().join(format!(
            "townlight-automation-controller-health-{nonce}-{}.db",
            TEST_SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ));
        let store = StationStore::open(&database).expect("open alert authority");
        drop(store);

        observe_automation_execution_controller_alert(&database);
        let reopened = StationStore::open(&database).expect("reopen alert authority");
        assert!(
            reopened
                .read_firing_alert(
                    AlertConditionKind::ServiceDown,
                    AUTOMATION_EXECUTION_CONTROLLER_ALERT_RESOURCE,
                )
                .expect("read controller alert")
                .is_some()
        );
        drop(reopened);

        resolve_automation_execution_controller_alert(&database);
        let reopened = StationStore::open(&database).expect("reopen resolved authority");
        assert!(
            reopened
                .read_firing_alert(
                    AlertConditionKind::ServiceDown,
                    AUTOMATION_EXECUTION_CONTROLLER_ALERT_RESOURCE,
                )
                .expect("read resolved controller alert")
                .is_none()
        );
        drop(reopened);
        let _ = std::fs::remove_file(database);
    }
}
