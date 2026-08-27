use super::*;
use station_automation_policy::{AutomationExecutionReceipt, AutomationExecutionRequest};
use station_domain::{AlertConditionKind, AlertEvent, AlertEventState, AlertSeverity};
use station_maintenance::VerifiedInstallAuthority;
use std::cell::Cell;
use std::path::Path;
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const AUTOMATION_EXECUTION_WORKER_ID: &str = "stationd-automation-execution";
const AUTOMATION_EXECUTION_LEASE_MS: i64 = 60_000;
const AUTOMATION_EXECUTION_POLL_INTERVAL: Duration = Duration::from_millis(250);
const AUTOMATION_EXECUTION_MAX_BACKOFF: Duration = Duration::from_secs(5);

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

/// A controller-owned attempt is a non-blocking, cancellable boundary around
/// provider work.  Implementations must put provider execution in a managed
/// child/job boundary before returning this object.  `poll` is a scheduling
/// probe, never a place to perform unbounded provider I/O; `terminate` must
/// synchronously revoke that managed child before the claim can be released.
/// This deliberately prevents an arbitrary synchronous adapter call from
/// keeping stationd alive after its durable authority has expired.
pub trait AutomationExecutionAttempt: Send {
    fn poll(
        &mut self,
        deadline_unix_ms: i64,
        heartbeat: &mut dyn FnMut() -> Result<(), String>,
    ) -> Result<AutomationExecutionAttemptState, AutomationExecutionAdapterFailure>;

    fn terminate(&mut self) -> Result<(), String>;
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AutomationExecutionAttemptState {
    Completed(Box<AutomationExecutionReceipt>),
    Running,
}

/// The controller gives adapters only a claimed request and a bounded
/// execution boundary. Implementations must return a cancellable managed
/// attempt, never run provider/model work synchronously on the stationd
/// controller thread.
pub trait AutomationExecutionAdapter {
    fn adapter_id(&self) -> &str;

    fn begin(
        &self,
        request: &AutomationExecutionRequest,
        claim_id: &str,
        deadline_unix_ms: i64,
    ) -> Result<Box<dyn AutomationExecutionAttempt>, AutomationExecutionAdapterFailure>;
}

/// Runs one bounded queue decision. The store performs the authoritative
/// candidate, policy, adapter, configuration, connection-evidence, lease, and
/// receipt checks inside durable transactions; this controller never converts
/// an adapter return into success without that final commit.
pub fn run_automation_execution_controller_iteration(
    store: &StationStore,
    adapter: Option<&dyn AutomationExecutionAdapter>,
    install_authority: &VerifiedInstallAuthority,
    worker_id: &str,
    clock: &mut dyn FnMut() -> Result<i64, String>,
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
            if action_started_at_unix_ms >= deadline_unix_ms {
                Err(AutomationExecutionAdapterFailure::RetryableTimeout)
            } else {
                match adapter.begin(&claimed.request, claim_id, deadline_unix_ms) {
                    Err(failure) => Err(failure),
                    Ok(mut attempt) => {
                        let renewed_deadline = Cell::new(deadline_unix_ms);
                        let mut heartbeat = || {
                            let now_unix_ms = clock()?;
                            if now_unix_ms >= renewed_deadline.get() {
                                return Err(
                                    "automation execution adapter exceeded its bounded lease"
                                        .into(),
                                );
                            }
                            let renewed = store.renew_automation_execution_job_lease(
                                &claimed.request.job_id,
                                worker_id,
                                install_authority,
                                now_unix_ms,
                                AUTOMATION_EXECUTION_LEASE_MS,
                            )?;
                            renewed_deadline.set(renewed.lease_expires_at_unix_ms.ok_or_else(
                                || "renewed automation execution job lacks a deadline".to_string(),
                            )?);
                            Ok(())
                        };
                        let result = match attempt.poll(renewed_deadline.get(), &mut heartbeat) {
                            Err(failure) => Err(failure),
                            Ok(AutomationExecutionAttemptState::Completed(receipt)) => Ok(*receipt),
                            Ok(AutomationExecutionAttemptState::Running) => {
                                // A controller iteration does not retain an attempt across
                                // authority/fence lifetimes. It must prove the managed child
                                // was terminated before releasing this claim for a paced retry.
                                match attempt.terminate() {
                                    Ok(()) => {
                                        Err(AutomationExecutionAdapterFailure::RetryableTimeout)
                                    }
                                    Err(_) => Err(
                                        AutomationExecutionAdapterFailure::PermanentInvalidResponse,
                                    ),
                                }
                            }
                        };
                        deadline_unix_ms = renewed_deadline.get();
                        result
                    }
                }
            }
        }
        None => Err(AutomationExecutionAdapterFailure::RetryableUnavailable),
    };

    let finished_at_unix_ms = clock()?;
    if finished_at_unix_ms >= deadline_unix_ms {
        store.record_automation_execution_attempt_failure(
            &claimed.request.job_id,
            worker_id,
            AutomationExecutionAdapterFailure::RetryableTimeout.code(),
            true,
            finished_at_unix_ms,
        )?;
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
        match run_automation_execution_queue_iteration(database, application_root) {
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
    run_automation_execution_controller_iteration(
        &store,
        None,
        &install_authority,
        AUTOMATION_EXECUTION_WORKER_ID,
        &mut clock,
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
