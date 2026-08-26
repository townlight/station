#![forbid(unsafe_code)]

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

pub use station_domain::AUTOMATIC_SCHEDULE_MATERIALIZATION_ACTOR_ID as SYSTEM_ACTOR_ID;
pub const UTC_DAY_MS: i64 = 86_400_000;
pub const MAX_CHANNELS: usize = 256;
pub const MAX_WORK_INTERVAL: Duration = Duration::from_secs(60);
pub const MAX_STOP_CHECK_INTERVAL: Duration = Duration::from_millis(250);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ClockFailure {
    Negative {
        observed_utc_ms: i64,
    },
    Regression {
        previous_utc_ms: i64,
        observed_utc_ms: i64,
    },
    Overflow,
    Unavailable,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BackendReadError {
    Unavailable,
    CorruptNewestCheckpoint,
    DuplicateOrUnboundedAuthority,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChannelMaterializationError {
    ChannelDisabled,
    StaleAuthority,
    InvalidDerivedInput,
    Capacity,
    Integrity,
    Storage,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MaterializationAlertError {
    Storage,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ControlError {
    InvalidConfiguration,
    Clock(ClockFailure),
    BackendRead(BackendReadError),
    Alerting(MaterializationAlertError),
    InvalidCheckpoint,
    InvalidChannelAuthority,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ValidatedRunCheckpoint {
    pub channel_id: String,
    pub run_id: String,
    pub result_sha256: String,
    pub completed_at_utc_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChannelCheckpoint {
    pub channel_id: String,
    pub last_successful_run: Option<ValidatedRunCheckpoint>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MaterializationApplyOutcome {
    Applied(ValidatedRunCheckpoint),
    AlreadyApplied(ValidatedRunCheckpoint),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DailyDecision {
    Due { utc_day: i64 },
    AlreadyCompleted { utc_day: i64 },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SupervisorConfig {
    pub work_interval: Duration,
    pub stop_check_interval: Duration,
    pub initial_backoff: Duration,
    pub maximum_backoff: Duration,
}

impl Default for SupervisorConfig {
    fn default() -> Self {
        Self {
            work_interval: Duration::from_secs(60),
            stop_check_interval: Duration::from_millis(100),
            initial_backoff: Duration::from_secs(30),
            maximum_backoff: Duration::from_secs(15 * 60),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChannelFailure {
    pub channel_id: String,
    pub error: ChannelMaterializationError,
    pub retry_not_before_utc_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct CycleReport {
    pub observed_at_utc_ms: i64,
    pub attempted_channels: Vec<String>,
    pub succeeded_channels: Vec<String>,
    pub already_completed_channels: Vec<String>,
    pub backing_off_channels: Vec<String>,
    pub failures: Vec<ChannelFailure>,
}

pub trait UtcClock {
    fn now_utc_ms(&self) -> Result<i64, ClockFailure>;
}

/// Trusted integration boundary for automatic materialization.
///
/// The checkpoint read must be one bounded snapshot containing every eligible
/// channel (enabled with at least one enabled recurring rule) and its newest
/// fully validated durable run. It must fail rather than skipping a corrupt
/// newest run in favor of an older one. The write must re-check channel
/// enablement in the transaction that writes the run. Failure observation and
/// resolution run only after that write has returned. The controller never
/// supplies rules, windows, evidence, or time.
pub trait AutomaticMaterializationBackend<A> {
    fn enabled_channel_checkpoints(&self) -> Result<Vec<ChannelCheckpoint>, BackendReadError>;

    fn materialize_channel(
        &self,
        channel_id: &str,
        actor_id: &str,
        boot_authority: &Arc<A>,
    ) -> Result<MaterializationApplyOutcome, ChannelMaterializationError>;

    fn observe_materialization_failure(
        &self,
        channel_id: &str,
        error: ChannelMaterializationError,
        observed_at_utc_ms: i64,
    ) -> Result<(), MaterializationAlertError>;

    fn resolve_materialization_failure(
        &self,
        channel_id: &str,
        resolved_at_utc_ms: i64,
    ) -> Result<(), MaterializationAlertError>;
}

pub fn daily_materialization_decision(
    last_successful_at_utc_ms: Option<i64>,
    now_utc_ms: i64,
) -> Result<DailyDecision, ClockFailure> {
    validate_nonnegative(now_utc_ms)?;
    let utc_day = now_utc_ms / UTC_DAY_MS;
    let Some(last) = last_successful_at_utc_ms else {
        return Ok(DailyDecision::Due { utc_day });
    };
    validate_nonnegative(last)?;
    if last > now_utc_ms {
        return Err(ClockFailure::Regression {
            previous_utc_ms: last,
            observed_utc_ms: now_utc_ms,
        });
    }
    if last / UTC_DAY_MS == utc_day {
        Ok(DailyDecision::AlreadyCompleted { utc_day })
    } else {
        Ok(DailyDecision::Due { utc_day })
    }
}

#[derive(Debug, Clone)]
struct ChannelControlState {
    last_successful_run: Option<ValidatedRunCheckpoint>,
    consecutive_failures: u32,
    retry_not_before_utc_ms: i64,
    failure_alert_reconciled: bool,
}

pub struct AutomaticMaterializationSupervisor<C, B, A> {
    clock: C,
    backend: B,
    boot_authority: Arc<A>,
    config: SupervisorConfig,
    previous_utc_ms: Option<i64>,
    channels: BTreeMap<String, ChannelControlState>,
}

impl<C, B, A> AutomaticMaterializationSupervisor<C, B, A>
where
    C: UtcClock,
    B: AutomaticMaterializationBackend<A>,
{
    pub fn new(
        clock: C,
        backend: B,
        boot_authority: Arc<A>,
        config: SupervisorConfig,
    ) -> Result<Self, ControlError> {
        validate_config(config)?;
        Ok(Self {
            clock,
            backend,
            boot_authority,
            config,
            previous_utc_ms: None,
            channels: BTreeMap::new(),
        })
    }

    pub fn run_cycle(&mut self) -> Result<CycleReport, ControlError> {
        self.run_cycle_with_stop(&AtomicBool::new(false))
    }

    pub fn run_cycle_with_stop(&mut self, stop: &AtomicBool) -> Result<CycleReport, ControlError> {
        if stop.load(Ordering::Acquire) {
            return Ok(CycleReport::default());
        }
        let initial_now = self.read_clock_after(self.previous_utc_ms)?;
        let snapshot = self
            .backend
            .enabled_channel_checkpoints()
            .map_err(ControlError::BackendRead)?;
        let authority = validate_snapshot(snapshot, initial_now, &self.channels)?;

        // Validate the complete authority before mutating state or invoking work.
        self.previous_utc_ms = Some(initial_now);
        let enabled: BTreeSet<_> = authority.keys().cloned().collect();
        let no_longer_eligible = self
            .channels
            .keys()
            .filter(|channel_id| !enabled.contains(*channel_id))
            .cloned()
            .collect::<Vec<_>>();
        for channel_id in no_longer_eligible {
            self.backend
                .resolve_materialization_failure(&channel_id, initial_now)
                .map_err(ControlError::Alerting)?;
        }
        self.channels.retain(|id, _| enabled.contains(id));
        for (channel_id, checkpoint) in &authority {
            let state =
                self.channels
                    .entry(channel_id.clone())
                    .or_insert_with(|| ChannelControlState {
                        last_successful_run: None,
                        consecutive_failures: 0,
                        retry_not_before_utc_ms: 0,
                        failure_alert_reconciled: false,
                    });
            if checkpoint.is_some() && state.last_successful_run.as_ref() != checkpoint.as_ref() {
                state.last_successful_run.clone_from(checkpoint);
                state.failure_alert_reconciled = false;
            }
        }

        let mut report = CycleReport {
            observed_at_utc_ms: initial_now,
            ..CycleReport::default()
        };
        let mut cycle_now = initial_now;
        for channel_id in authority.keys() {
            if stop.load(Ordering::Acquire) {
                break;
            }
            let (last_successful_at_utc_ms, retry_not_before_utc_ms, failure_alert_reconciled) = {
                let state = self
                    .channels
                    .get(channel_id)
                    .expect("validated channel state exists");
                (
                    state
                        .last_successful_run
                        .as_ref()
                        .map(|run| run.completed_at_utc_ms),
                    state.retry_not_before_utc_ms,
                    state.failure_alert_reconciled,
                )
            };
            match daily_materialization_decision(last_successful_at_utc_ms, cycle_now)
                .map_err(ControlError::Clock)?
            {
                DailyDecision::AlreadyCompleted { .. } => {
                    if !failure_alert_reconciled {
                        self.backend
                            .resolve_materialization_failure(channel_id, cycle_now)
                            .map_err(ControlError::Alerting)?;
                        self.channels
                            .get_mut(channel_id)
                            .expect("validated channel state exists")
                            .failure_alert_reconciled = true;
                    }
                    report.already_completed_channels.push(channel_id.clone());
                    continue;
                }
                DailyDecision::Due { .. } => {}
            }
            if cycle_now < retry_not_before_utc_ms {
                report.backing_off_channels.push(channel_id.clone());
                continue;
            }

            report.attempted_channels.push(channel_id.clone());
            let outcome =
                self.backend
                    .materialize_channel(channel_id, SYSTEM_ACTOR_ID, &self.boot_authority);
            let post_call_now = self.read_clock_after(Some(cycle_now))?;
            self.previous_utc_ms = Some(post_call_now);
            cycle_now = post_call_now;
            match outcome {
                Ok(MaterializationApplyOutcome::Applied(run))
                | Ok(MaterializationApplyOutcome::AlreadyApplied(run)) => {
                    validate_checkpoint(channel_id, &run, cycle_now)?;
                    if run.completed_at_utc_ms / UTC_DAY_MS != cycle_now / UTC_DAY_MS {
                        return Err(ControlError::InvalidCheckpoint);
                    }
                    validate_checkpoint_advance(
                        authority.get(channel_id).and_then(Option::as_ref),
                        &run,
                    )?;
                    let state = self
                        .channels
                        .get_mut(channel_id)
                        .expect("validated channel state exists");
                    state.last_successful_run = Some(run);
                    state.consecutive_failures = 0;
                    state.retry_not_before_utc_ms = 0;
                    state.failure_alert_reconciled = false;
                    report.succeeded_channels.push(channel_id.clone());
                    self.backend
                        .resolve_materialization_failure(channel_id, cycle_now)
                        .map_err(ControlError::Alerting)?;
                    self.channels
                        .get_mut(channel_id)
                        .expect("validated channel state exists")
                        .failure_alert_reconciled = true;
                }
                Err(error) => {
                    let state = self
                        .channels
                        .get_mut(channel_id)
                        .expect("validated channel state exists");
                    state.consecutive_failures = state
                        .consecutive_failures
                        .checked_add(1)
                        .ok_or(ControlError::Clock(ClockFailure::Overflow))?;
                    let delay = retry_delay(self.config, state.consecutive_failures)?;
                    let delay_ms = i64::try_from(delay.as_millis())
                        .map_err(|_| ControlError::Clock(ClockFailure::Overflow))?;
                    state.retry_not_before_utc_ms = cycle_now
                        .checked_add(delay_ms)
                        .ok_or(ControlError::Clock(ClockFailure::Overflow))?;
                    state.failure_alert_reconciled = false;
                    report.failures.push(ChannelFailure {
                        channel_id: channel_id.clone(),
                        error,
                        retry_not_before_utc_ms: state.retry_not_before_utc_ms,
                    });
                    self.backend
                        .observe_materialization_failure(channel_id, error, cycle_now)
                        .map_err(ControlError::Alerting)?;
                }
            }
        }
        Ok(report)
    }

    pub fn run_until_stopped(&mut self, stop: &AtomicBool) -> Result<(), ControlError> {
        let mut next_work = Instant::now();
        loop {
            if stop.load(Ordering::Acquire) {
                return Ok(());
            }
            let now = Instant::now();
            if now >= next_work {
                self.run_cycle_with_stop(stop)?;
                if stop.load(Ordering::Acquire) {
                    return Ok(());
                }
                next_work = Instant::now()
                    .checked_add(self.config.work_interval)
                    .ok_or(ControlError::Clock(ClockFailure::Overflow))?;
                continue;
            }
            let sleep_for = self
                .config
                .stop_check_interval
                .min(next_work.saturating_duration_since(now));
            std::thread::park_timeout(sleep_for);
        }
    }

    fn read_clock_after(&self, previous: Option<i64>) -> Result<i64, ControlError> {
        let observed = self.clock.now_utc_ms().map_err(ControlError::Clock)?;
        validate_nonnegative(observed).map_err(ControlError::Clock)?;
        if let Some(previous) = previous
            && observed < previous
        {
            return Err(ControlError::Clock(ClockFailure::Regression {
                previous_utc_ms: previous,
                observed_utc_ms: observed,
            }));
        }
        Ok(observed)
    }
}

fn validate_config(config: SupervisorConfig) -> Result<(), ControlError> {
    if config.work_interval.is_zero()
        || config.work_interval > MAX_WORK_INTERVAL
        || config.stop_check_interval.is_zero()
        || config.stop_check_interval > MAX_STOP_CHECK_INTERVAL
        || config.initial_backoff.is_zero()
        || config.initial_backoff > config.maximum_backoff
    {
        return Err(ControlError::InvalidConfiguration);
    }
    Ok(())
}

fn validate_snapshot(
    snapshot: Vec<ChannelCheckpoint>,
    now_utc_ms: i64,
    local: &BTreeMap<String, ChannelControlState>,
) -> Result<BTreeMap<String, Option<ValidatedRunCheckpoint>>, ControlError> {
    if snapshot.len() > MAX_CHANNELS {
        return Err(ControlError::InvalidChannelAuthority);
    }
    let mut validated = BTreeMap::new();
    for channel in snapshot {
        if !canonical_uuid(&channel.channel_id) || validated.contains_key(&channel.channel_id) {
            return Err(ControlError::InvalidChannelAuthority);
        }
        if let Some(run) = &channel.last_successful_run {
            validate_checkpoint(&channel.channel_id, run, now_utc_ms)?;
        }
        if let Some(previous) = local
            .get(&channel.channel_id)
            .and_then(|state| state.last_successful_run.as_ref())
        {
            let current = channel
                .last_successful_run
                .as_ref()
                .ok_or(ControlError::InvalidCheckpoint)?;
            if current.completed_at_utc_ms < previous.completed_at_utc_ms
                || (current.completed_at_utc_ms == previous.completed_at_utc_ms
                    && current != previous)
            {
                return Err(ControlError::InvalidCheckpoint);
            }
        }
        validated.insert(channel.channel_id, channel.last_successful_run);
    }
    Ok(validated)
}

fn validate_checkpoint(
    channel_id: &str,
    run: &ValidatedRunCheckpoint,
    now_utc_ms: i64,
) -> Result<(), ControlError> {
    if run.channel_id != channel_id
        || !canonical_uuid(&run.run_id)
        || !lower_hex_sha256(&run.result_sha256)
        || run.completed_at_utc_ms < 0
        || run.completed_at_utc_ms > now_utc_ms
    {
        return Err(ControlError::InvalidCheckpoint);
    }
    Ok(())
}

fn validate_checkpoint_advance(
    previous: Option<&ValidatedRunCheckpoint>,
    current: &ValidatedRunCheckpoint,
) -> Result<(), ControlError> {
    if previous.is_some_and(|previous| {
        current.completed_at_utc_ms < previous.completed_at_utc_ms
            || (current.completed_at_utc_ms == previous.completed_at_utc_ms && current != previous)
    }) {
        return Err(ControlError::InvalidCheckpoint);
    }
    Ok(())
}

fn retry_delay(
    config: SupervisorConfig,
    consecutive_failures: u32,
) -> Result<Duration, ControlError> {
    let exponent = consecutive_failures.saturating_sub(1).min(31);
    let multiplier = 1_u32
        .checked_shl(exponent)
        .ok_or(ControlError::Clock(ClockFailure::Overflow))?;
    Ok(config
        .initial_backoff
        .checked_mul(multiplier)
        .ok_or(ControlError::Clock(ClockFailure::Overflow))?
        .min(config.maximum_backoff))
}

fn validate_nonnegative(observed_utc_ms: i64) -> Result<(), ClockFailure> {
    if observed_utc_ms < 0 {
        Err(ClockFailure::Negative { observed_utc_ms })
    } else {
        Ok(())
    }
}

fn canonical_uuid(value: &str) -> bool {
    value.len() == 36
        && value.bytes().enumerate().all(|(index, byte)| match index {
            8 | 13 | 18 | 23 => byte == b'-',
            _ => byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte),
        })
}

fn lower_hex_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}
