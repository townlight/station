use super::*;
use station_automation_policy::{AutomationExecutionReceipt, AutomationExecutionRequest};
use station_maintenance::VerifiedInstallAuthority;
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

/// The controller gives adapters only a claimed request, a bounded deadline,
/// and a cooperative lease heartbeat. Implementations must construct an exact
/// receipt or return one of the typed failures above.
pub trait AutomationExecutionAdapter {
    fn adapter_id(&self) -> &str;

    fn execute(
        &self,
        request: &AutomationExecutionRequest,
        claim_id: &str,
        deadline_unix_ms: i64,
        heartbeat: &mut dyn FnMut() -> Result<(), String>,
    ) -> Result<AutomationExecutionReceipt, AutomationExecutionAdapterFailure>;
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
    let deadline_unix_ms = claimed
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
                let mut heartbeat = || {
                    let now_unix_ms = clock()?;
                    if now_unix_ms >= deadline_unix_ms {
                        return Err(
                            "automation execution adapter exceeded its bounded lease".into()
                        );
                    }
                    store
                        .renew_automation_execution_job_lease(
                            &claimed.request.job_id,
                            worker_id,
                            install_authority,
                            now_unix_ms,
                            AUTOMATION_EXECUTION_LEASE_MS,
                        )
                        .map(|_| ())
                };
                adapter.execute(&claimed.request, claim_id, deadline_unix_ms, &mut heartbeat)
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
            Ok(_) => backoff = AUTOMATION_EXECUTION_POLL_INTERVAL,
            Err(_) => backoff = (backoff * 2).min(AUTOMATION_EXECUTION_MAX_BACKOFF),
        }
        std::thread::sleep(backoff);
    }
    Ok(())
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
