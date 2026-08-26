use std::collections::BTreeMap;
use std::sync::atomic::{AtomicBool, AtomicI64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use station_materialization_control::{
    AutomaticMaterializationBackend, AutomaticMaterializationSupervisor, BackendReadError,
    ChannelCheckpoint, ChannelMaterializationError, ClockFailure, ControlError, DailyDecision,
    MAX_CHANNELS, MAX_STOP_CHECK_INTERVAL, MAX_WORK_INTERVAL, MaterializationApplyOutcome,
    SYSTEM_ACTOR_ID, SupervisorConfig, UTC_DAY_MS, UtcClock, ValidatedRunCheckpoint,
    daily_materialization_decision,
};

fn channel(suffix: u64) -> String {
    format!("10000000-0000-4000-8000-{suffix:012}")
}

fn checkpoint(channel_id: &str, ordinal: u64, completed_at_utc_ms: i64) -> ValidatedRunCheckpoint {
    ValidatedRunCheckpoint {
        channel_id: channel_id.into(),
        run_id: format!("20000000-0000-4000-8000-{ordinal:012}"),
        result_sha256: format!("{ordinal:064x}"),
        completed_at_utc_ms,
    }
}

#[derive(Clone)]
struct TestClock(Arc<AtomicI64>);

impl TestClock {
    fn new(now: i64) -> Self {
        Self(Arc::new(AtomicI64::new(now)))
    }

    fn set(&self, now: i64) {
        self.0.store(now, Ordering::SeqCst);
    }
}

impl UtcClock for TestClock {
    fn now_utc_ms(&self) -> Result<i64, ClockFailure> {
        Ok(self.0.load(Ordering::SeqCst))
    }
}

#[derive(Clone)]
struct TestAuthority {
    marker: u64,
    now: Arc<AtomicI64>,
}

#[derive(Default)]
struct BackendState {
    enabled: Vec<String>,
    checkpoints: BTreeMap<String, ValidatedRunCheckpoint>,
    remaining_failures: BTreeMap<String, u32>,
    attempts: Vec<(String, String, u64, usize)>,
    observed_failures: Vec<(String, ChannelMaterializationError, i64)>,
    resolved_failures: Vec<(String, i64)>,
    fail_failure_alert_write: bool,
    fail_resolution_alert_write: bool,
    reads: usize,
    stop_after_attempt: Option<Arc<AtomicBool>>,
    snapshot_override: Option<Vec<ChannelCheckpoint>>,
    advance_clock_on_attempt: Option<i64>,
    completion_override: Option<i64>,
}

#[derive(Clone, Default)]
struct TestBackend(Arc<Mutex<BackendState>>);

impl AutomaticMaterializationBackend<TestAuthority> for TestBackend {
    fn enabled_channel_checkpoints(&self) -> Result<Vec<ChannelCheckpoint>, BackendReadError> {
        let mut state = self.0.lock().unwrap();
        state.reads += 1;
        if let Some(snapshot) = &state.snapshot_override {
            return Ok(snapshot.clone());
        }
        Ok(state
            .enabled
            .iter()
            .map(|channel_id| ChannelCheckpoint {
                channel_id: channel_id.clone(),
                last_successful_run: state.checkpoints.get(channel_id).cloned(),
            })
            .collect())
    }

    fn materialize_channel(
        &self,
        channel_id: &str,
        actor_id: &str,
        authority: &Arc<TestAuthority>,
    ) -> Result<MaterializationApplyOutcome, ChannelMaterializationError> {
        let mut state = self.0.lock().unwrap();
        state.attempts.push((
            channel_id.into(),
            actor_id.into(),
            authority.marker,
            Arc::as_ptr(authority) as usize,
        ));
        if let Some(stop) = &state.stop_after_attempt {
            stop.store(true, Ordering::Release);
        }
        if let Some(next) = state.advance_clock_on_attempt {
            authority.now.store(next, Ordering::SeqCst);
        }
        if state
            .remaining_failures
            .get_mut(channel_id)
            .is_some_and(|remaining| {
                if *remaining == 0 {
                    false
                } else {
                    *remaining -= 1;
                    true
                }
            })
        {
            return Err(ChannelMaterializationError::Storage);
        }
        let run = checkpoint(
            channel_id,
            state.attempts.len() as u64,
            state
                .completion_override
                .unwrap_or_else(|| authority.now.load(Ordering::SeqCst)),
        );
        state.checkpoints.insert(channel_id.into(), run.clone());
        Ok(MaterializationApplyOutcome::Applied(run))
    }

    fn observe_materialization_failure(
        &self,
        channel_id: &str,
        error: ChannelMaterializationError,
        observed_at_utc_ms: i64,
    ) -> Result<(), station_materialization_control::MaterializationAlertError> {
        let mut state = self.0.lock().unwrap();
        state
            .observed_failures
            .push((channel_id.into(), error, observed_at_utc_ms));
        if state.fail_failure_alert_write {
            Err(station_materialization_control::MaterializationAlertError::Storage)
        } else {
            Ok(())
        }
    }

    fn resolve_materialization_failure(
        &self,
        channel_id: &str,
        resolved_at_utc_ms: i64,
    ) -> Result<(), station_materialization_control::MaterializationAlertError> {
        let mut state = self.0.lock().unwrap();
        state
            .resolved_failures
            .push((channel_id.into(), resolved_at_utc_ms));
        if state.fail_resolution_alert_write {
            Err(station_materialization_control::MaterializationAlertError::Storage)
        } else {
            Ok(())
        }
    }
}

fn config() -> SupervisorConfig {
    SupervisorConfig {
        work_interval: Duration::from_secs(60),
        stop_check_interval: Duration::from_millis(20),
        initial_backoff: Duration::from_millis(1_000),
        maximum_backoff: Duration::from_millis(8_000),
    }
}

fn supervisor(
    now: i64,
    backend: TestBackend,
    config: SupervisorConfig,
) -> (
    TestClock,
    AutomaticMaterializationSupervisor<TestClock, TestBackend, TestAuthority>,
    usize,
) {
    let clock = TestClock::new(now);
    let authority = Arc::new(TestAuthority {
        marker: 7,
        now: Arc::clone(&clock.0),
    });
    let authority_pointer = Arc::as_ptr(&authority) as usize;
    let supervisor =
        AutomaticMaterializationSupervisor::new(clock.clone(), backend, authority, config).unwrap();
    (clock, supervisor, authority_pointer)
}

#[test]
fn daily_policy_is_once_per_utc_day_and_rejects_invalid_time() {
    let day = 12 * UTC_DAY_MS;
    assert_eq!(
        daily_materialization_decision(None, day + 1).unwrap(),
        DailyDecision::Due { utc_day: 12 }
    );
    assert_eq!(
        daily_materialization_decision(Some(day), day + UTC_DAY_MS - 1).unwrap(),
        DailyDecision::AlreadyCompleted { utc_day: 12 }
    );
    assert_eq!(
        daily_materialization_decision(Some(day), day + UTC_DAY_MS).unwrap(),
        DailyDecision::Due { utc_day: 13 }
    );
    assert!(matches!(
        daily_materialization_decision(None, -1),
        Err(ClockFailure::Negative { .. })
    ));
    assert!(matches!(
        daily_materialization_decision(Some(day + 2), day + 1),
        Err(ClockFailure::Regression { .. })
    ));
}

#[test]
fn supervisor_uses_fixed_actor_boot_authority_and_durable_daily_checkpoint() {
    let now = 20 * UTC_DAY_MS + 1_000;
    let backend = TestBackend::default();
    backend.0.lock().unwrap().enabled = vec![channel(1)];
    let (clock, mut supervisor, authority_pointer) = supervisor(now, backend.clone(), config());

    let first = supervisor.run_cycle().unwrap();
    assert_eq!(first.succeeded_channels, vec![channel(1)]);
    assert_eq!(backend.0.lock().unwrap().attempts.len(), 1);
    let same_day = supervisor.run_cycle().unwrap();
    assert_eq!(same_day.already_completed_channels, vec![channel(1)]);
    assert_eq!(backend.0.lock().unwrap().attempts.len(), 1);

    clock.set(21 * UTC_DAY_MS + 1_000);
    assert_eq!(
        supervisor.run_cycle().unwrap().succeeded_channels,
        vec![channel(1)]
    );
    let state = backend.0.lock().unwrap();
    assert_eq!(state.attempts.len(), 2);
    assert!(state.attempts.iter().all(|(_, actor, marker, pointer)| {
        actor == SYSTEM_ACTOR_ID && *marker == 7 && *pointer == authority_pointer
    }));
}

#[test]
fn one_channel_failure_is_isolated_and_backed_off_without_blocking_others() {
    let now = 30 * UTC_DAY_MS + 5_000;
    let first = channel(1);
    let second = channel(2);
    let backend = TestBackend::default();
    {
        let mut state = backend.0.lock().unwrap();
        state.enabled = vec![second.clone(), first.clone()];
        state.remaining_failures.insert(first.clone(), 1);
    }
    let (clock, mut supervisor, _) = supervisor(now, backend.clone(), config());

    let initial = supervisor.run_cycle().unwrap();
    assert_eq!(initial.succeeded_channels, vec![second.clone()]);
    assert_eq!(initial.failures.len(), 1);
    assert_eq!(initial.failures[0].channel_id, first);
    assert_eq!(initial.failures[0].retry_not_before_utc_ms, now + 1_000);

    clock.set(now + 999);
    let waiting = supervisor.run_cycle().unwrap();
    assert_eq!(waiting.backing_off_channels, vec![first.clone()]);
    assert_eq!(backend.0.lock().unwrap().attempts.len(), 2);

    clock.set(now + 1_000);
    assert_eq!(
        supervisor.run_cycle().unwrap().succeeded_channels,
        vec![first]
    );
    assert_eq!(backend.0.lock().unwrap().attempts.len(), 3);
}

#[test]
fn failed_channels_emit_one_typed_alert_and_success_resolves_only_that_channel() {
    let now = 31 * UTC_DAY_MS + 5_000;
    let failed = channel(1);
    let healthy = channel(2);
    let backend = TestBackend::default();
    {
        let mut state = backend.0.lock().unwrap();
        state.enabled = vec![healthy.clone(), failed.clone()];
        state.remaining_failures.insert(failed.clone(), 1);
    }
    let (clock, mut supervisor, _) = supervisor(now, backend.clone(), config());

    let initial = supervisor.run_cycle().unwrap();
    assert_eq!(initial.succeeded_channels, vec![healthy.clone()]);
    assert_eq!(initial.failures.len(), 1);
    assert_eq!(
        backend.0.lock().unwrap().observed_failures,
        vec![(failed.clone(), ChannelMaterializationError::Storage, now)]
    );
    assert_eq!(
        backend.0.lock().unwrap().resolved_failures,
        vec![(healthy, now)]
    );

    clock.set(now + 999);
    assert_eq!(
        supervisor.run_cycle().unwrap().backing_off_channels,
        vec![failed.clone()]
    );
    assert_eq!(backend.0.lock().unwrap().observed_failures.len(), 1);

    clock.set(now + 1_000);
    assert_eq!(
        supervisor.run_cycle().unwrap().succeeded_channels,
        vec![failed.clone()]
    );
    assert_eq!(
        backend.0.lock().unwrap().resolved_failures,
        vec![(channel(2), now), (failed, now + 1_000)]
    );
}

#[test]
fn a_required_failure_alert_write_error_fails_closed() {
    let now = 32 * UTC_DAY_MS + 5_000;
    let failed = channel(1);
    let unaffected = channel(2);
    let backend = TestBackend::default();
    {
        let mut state = backend.0.lock().unwrap();
        state.enabled = vec![failed.clone(), unaffected];
        state.remaining_failures.insert(failed.clone(), 1);
        state.fail_failure_alert_write = true;
    }
    let (_, mut supervisor, _) = supervisor(now, backend.clone(), config());

    assert_eq!(
        supervisor.run_cycle(),
        Err(ControlError::Alerting(
            station_materialization_control::MaterializationAlertError::Storage
        ))
    );
    assert_eq!(
        backend.0.lock().unwrap().observed_failures,
        vec![(failed, ChannelMaterializationError::Storage, now)]
    );
    assert_eq!(backend.0.lock().unwrap().attempts.len(), 1);
}

#[test]
fn restart_retries_failed_resolution_for_an_already_completed_channel() {
    let now = 33 * UTC_DAY_MS + 5_000;
    let target = channel(1);
    let backend = TestBackend::default();
    {
        let mut state = backend.0.lock().unwrap();
        state.enabled = vec![target.clone()];
        state.fail_resolution_alert_write = true;
    }
    let (_, mut first_boot, _) = supervisor(now, backend.clone(), config());

    assert_eq!(
        first_boot.run_cycle(),
        Err(ControlError::Alerting(
            station_materialization_control::MaterializationAlertError::Storage
        ))
    );
    assert!(backend.0.lock().unwrap().checkpoints.contains_key(&target));

    backend.0.lock().unwrap().fail_resolution_alert_write = false;
    let (_, mut restarted, _) = supervisor(now, backend.clone(), config());
    assert_eq!(
        restarted.run_cycle().unwrap().already_completed_channels,
        vec![target.clone()]
    );
    assert_eq!(
        backend.0.lock().unwrap().resolved_failures,
        vec![(target, now), (channel(1), now)]
    );
    assert_eq!(
        restarted.run_cycle().unwrap().already_completed_channels,
        vec![channel(1)]
    );
    assert_eq!(backend.0.lock().unwrap().resolved_failures.len(), 2);
}

#[test]
fn leaving_eligible_authority_resolves_a_previously_failing_channel() {
    let now = 34 * UTC_DAY_MS + 5_000;
    let target = channel(1);
    let backend = TestBackend::default();
    {
        let mut state = backend.0.lock().unwrap();
        state.enabled = vec![target.clone()];
        state.remaining_failures.insert(target.clone(), 1);
    }
    let (_, mut supervisor, _) = supervisor(now, backend.clone(), config());

    assert_eq!(supervisor.run_cycle().unwrap().failures.len(), 1);
    backend.0.lock().unwrap().enabled.clear();
    assert!(
        supervisor
            .run_cycle()
            .unwrap()
            .attempted_channels
            .is_empty()
    );
    assert_eq!(
        backend.0.lock().unwrap().resolved_failures,
        vec![(target, now)]
    );
}

#[test]
fn clock_regression_and_backoff_overflow_fail_closed() {
    let backend = TestBackend::default();
    let (clock, mut running, _) = supervisor(1_000, backend, config());
    running.run_cycle().unwrap();
    clock.set(999);
    assert_eq!(
        running.run_cycle(),
        Err(ControlError::Clock(ClockFailure::Regression {
            previous_utc_ms: 1_000,
            observed_utc_ms: 999,
        }))
    );

    let backend = TestBackend::default();
    let target = channel(9);
    {
        let mut state = backend.0.lock().unwrap();
        state.enabled = vec![target.clone()];
        state.remaining_failures.insert(target, 1);
    }
    let (_, mut supervisor, _) = supervisor(i64::MAX, backend, config());
    assert_eq!(
        supervisor.run_cycle(),
        Err(ControlError::Clock(ClockFailure::Overflow))
    );
}

#[test]
fn work_cadence_is_separate_from_stop_check_and_stop_is_observed_under_250ms() {
    let backend = TestBackend::default();
    let state = Arc::clone(&backend.0);
    let mut bounded = config();
    bounded.work_interval = Duration::from_millis(200);
    bounded.stop_check_interval = Duration::from_millis(20);
    let (_, mut supervisor, _) = supervisor(1_000, backend, bounded);
    let stop = Arc::new(AtomicBool::new(false));
    let thread_stop = Arc::clone(&stop);
    let started = Instant::now();
    let handle = std::thread::spawn(move || supervisor.run_until_stopped(&thread_stop));
    std::thread::sleep(Duration::from_millis(70));
    stop.store(true, Ordering::Release);
    handle.join().unwrap().unwrap();

    assert!(started.elapsed() < Duration::from_millis(250));
    assert_eq!(state.lock().unwrap().reads, 1);
}

#[test]
fn configuration_bounds_work_and_stop_cadences_independently() {
    let backend = TestBackend::default();
    let clock = TestClock::new(0);
    let authority = TestAuthority {
        marker: 7,
        now: Arc::clone(&clock.0),
    };
    for invalid in [
        SupervisorConfig {
            work_interval: Duration::ZERO,
            ..config()
        },
        SupervisorConfig {
            work_interval: MAX_WORK_INTERVAL + Duration::from_millis(1),
            ..config()
        },
        SupervisorConfig {
            stop_check_interval: Duration::ZERO,
            ..config()
        },
        SupervisorConfig {
            stop_check_interval: MAX_STOP_CHECK_INTERVAL + Duration::from_millis(1),
            ..config()
        },
        SupervisorConfig {
            initial_backoff: Duration::from_secs(2),
            maximum_backoff: Duration::from_secs(1),
            ..config()
        },
    ] {
        assert!(matches!(
            AutomaticMaterializationSupervisor::new(
                clock.clone(),
                backend.clone(),
                Arc::new(authority.clone()),
                invalid,
            ),
            Err(ControlError::InvalidConfiguration)
        ));
    }
}

#[test]
fn complete_checkpoint_authority_is_validated_before_attempt_or_state_change() {
    let now = 40 * UTC_DAY_MS + 1_000;
    let first = channel(1);
    let second = channel(2);
    let backend = TestBackend::default();
    backend.0.lock().unwrap().snapshot_override = Some(vec![
        ChannelCheckpoint {
            channel_id: first.clone(),
            last_successful_run: Some(checkpoint(&first, 1, now)),
        },
        ChannelCheckpoint {
            channel_id: second,
            last_successful_run: Some(ValidatedRunCheckpoint {
                result_sha256: "NOT-A-DIGEST".into(),
                ..checkpoint(&channel(2), 2, now)
            }),
        },
    ]);
    let (_, mut supervisor, _) = supervisor(now, backend.clone(), config());
    assert_eq!(supervisor.run_cycle(), Err(ControlError::InvalidCheckpoint));
    assert!(backend.0.lock().unwrap().attempts.is_empty());

    let mut state = backend.0.lock().unwrap();
    state.snapshot_override = None;
    state.enabled = vec![first.clone()];
    drop(state);
    assert_eq!(
        supervisor.run_cycle().unwrap().succeeded_channels,
        vec![first]
    );
}

#[test]
fn duplicate_and_unbounded_channel_authority_fail_closed() {
    let now = 41 * UTC_DAY_MS;
    let backend = TestBackend::default();
    backend.0.lock().unwrap().enabled = vec![channel(1), channel(1)];
    let (_, mut duplicate, _) = supervisor(now, backend.clone(), config());
    assert_eq!(
        duplicate.run_cycle(),
        Err(ControlError::InvalidChannelAuthority)
    );
    assert!(backend.0.lock().unwrap().attempts.is_empty());

    let backend = TestBackend::default();
    backend.0.lock().unwrap().enabled = (0..=MAX_CHANNELS)
        .map(|index| channel(index as u64))
        .collect();
    let (_, mut unbounded, _) = supervisor(now, backend, config());
    assert_eq!(
        unbounded.run_cycle(),
        Err(ControlError::InvalidChannelAuthority)
    );
}

#[test]
fn durable_checkpoint_cannot_disappear_regress_or_change_at_same_time() {
    let now = 42 * UTC_DAY_MS + 2_000;
    for replacement in [
        None,
        Some(checkpoint(&channel(1), 9, now - 1)),
        Some(checkpoint(&channel(1), 9, now)),
    ] {
        let target = channel(1);
        let backend = TestBackend::default();
        backend.0.lock().unwrap().enabled = vec![target.clone()];
        let (_, mut supervisor, _) = supervisor(now, backend.clone(), config());
        supervisor.run_cycle().unwrap();
        match replacement.clone() {
            Some(run) => {
                backend
                    .0
                    .lock()
                    .unwrap()
                    .checkpoints
                    .insert(target.clone(), run);
            }
            None => {
                backend.0.lock().unwrap().checkpoints.remove(&target);
            }
        }
        assert_eq!(supervisor.run_cycle(), Err(ControlError::InvalidCheckpoint));
    }
}

#[test]
fn preset_stop_avoids_read_and_stop_after_first_avoids_second_channel() {
    let now = 43 * UTC_DAY_MS;
    let stop = Arc::new(AtomicBool::new(true));
    let backend = TestBackend::default();
    backend.0.lock().unwrap().enabled = vec![channel(1)];
    let (_, mut supervisor, _) = supervisor(now, backend.clone(), config());
    assert_eq!(
        supervisor.run_cycle_with_stop(&stop).unwrap(),
        Default::default()
    );
    assert_eq!(backend.0.lock().unwrap().reads, 0);

    stop.store(false, Ordering::Release);
    {
        let mut state = backend.0.lock().unwrap();
        state.enabled = vec![channel(1), channel(2)];
        state.stop_after_attempt = Some(Arc::clone(&stop));
    }
    let report = supervisor.run_cycle_with_stop(&stop).unwrap();
    assert_eq!(report.attempted_channels, vec![channel(1)]);
    assert_eq!(backend.0.lock().unwrap().attempts.len(), 1);
}

#[test]
fn post_call_clock_accepts_valid_cross_midnight_completion() {
    let before_midnight = 50 * UTC_DAY_MS - 1;
    let after_midnight = 50 * UTC_DAY_MS + 5;
    let target = channel(1);
    let backend = TestBackend::default();
    {
        let mut state = backend.0.lock().unwrap();
        state.enabled = vec![target.clone()];
        state.advance_clock_on_attempt = Some(after_midnight);
    }
    let (_, mut supervisor, _) = supervisor(before_midnight, backend.clone(), config());
    assert_eq!(
        supervisor.run_cycle().unwrap().succeeded_channels,
        vec![target.clone()]
    );
    assert_eq!(
        backend.0.lock().unwrap().checkpoints[&target].completed_at_utc_ms,
        after_midnight
    );
    assert_eq!(
        supervisor.run_cycle().unwrap().already_completed_channels,
        vec![target]
    );
}

#[test]
fn successful_outcome_from_an_old_utc_day_is_rejected() {
    let now = 52 * UTC_DAY_MS + 1_000;
    let target = channel(1);
    let backend = TestBackend::default();
    {
        let mut state = backend.0.lock().unwrap();
        state.enabled = vec![target];
        state.completion_override = Some(51 * UTC_DAY_MS + 1_000);
    }
    let (_, mut supervisor, _) = supervisor(now, backend, config());
    assert_eq!(supervisor.run_cycle(), Err(ControlError::InvalidCheckpoint));
}
