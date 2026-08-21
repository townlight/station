use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use station_domain::{
    AlertChannel, AlertChannelKind, AlertConditionKind, AlertDeliveryState, AlertEvent,
    AlertEventState, AlertNotificationKind, AlertRule, AlertSeverity, ChannelRuntimeObservation,
    ChannelRuntimeState, SelfTestKind, SelfTestSchedule, SelfTestStatus, SelfTestTrigger,
    SystemResourceSample, SystemSelfTest, redact_alert_channel_target,
};
use station_schedule::ChannelConfiguration;
use station_storage::{AlertChannelSecret, StationStore};

const CHANNEL_ID: &str = "8ea48bc4-7f2e-4f89-9bbc-2c58472f0e8e";
const ALERT_CHANNEL_ID: &str = "5eb469af-f3c7-4cc5-b26e-865807b3e934";

fn database_path(label: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-{label}-{nonce}.db"))
}

fn remove_database(path: &Path) {
    for candidate in [
        path.to_path_buf(),
        PathBuf::from(format!("{}-wal", path.display())),
        PathBuf::from(format!("{}-shm", path.display())),
    ] {
        let _ = std::fs::remove_file(candidate);
    }
}

fn alert(event_id: &str, observed_at_unix_ms: i64) -> AlertEvent {
    AlertEvent {
        event_id: event_id.into(),
        condition: AlertConditionKind::EncoderDeath,
        severity: AlertSeverity::Critical,
        state: AlertEventState::Firing,
        resource_ref: CHANNEL_ID.into(),
        summary: "Channel worker exited unexpectedly.".into(),
        detail: "The supervisor will restart it and keep fallback on air.".into(),
        source_section: "S8".into(),
        first_observed_at_unix_ms: observed_at_unix_ms,
        last_observed_at_unix_ms: observed_at_unix_ms,
        resolved_at_unix_ms: None,
        occurrence_count: 1,
        acknowledged_at_unix_ms: None,
        acknowledged_by: None,
    }
}

fn put_webhook_channel(store: &StationStore, channel_id: &str) {
    put_webhook_channel_with_quiet_hours(store, channel_id, None, None);
}

fn put_webhook_channel_with_quiet_hours(
    store: &StationStore,
    channel_id: &str,
    quiet_hours_start_utc: Option<&str>,
    quiet_hours_end_utc: Option<&str>,
) {
    let target = "https://alerts.example.org/townlight";
    store
        .put_alert_channel(
            &AlertChannel {
                channel_id: channel_id.into(),
                kind: AlertChannelKind::Webhook,
                label: "Station pager".into(),
                enabled: true,
                target_redacted: redact_alert_channel_target(AlertChannelKind::Webhook, target)
                    .unwrap(),
                credential_configured: true,
                quiet_hours_start_utc: quiet_hours_start_utc.map(str::to_owned),
                quiet_hours_end_utc: quiet_hours_end_utc.map(str::to_owned),
                created_at_unix_ms: 500,
                updated_at_unix_ms: 500,
            },
            &AlertChannelSecret {
                target: target.into(),
                endpoint: None,
                sender: None,
                identity: None,
                credential: "delivery-signing-secret".into(),
            },
        )
        .unwrap();
}

#[test]
fn persists_monotonic_channel_runtime_truth() {
    let database = database_path("channel-runtime");
    let store = StationStore::open(&database).unwrap();
    store
        .put_channel(&ChannelConfiguration {
            channel_id: CHANNEL_ID.into(),
            display_name: "Government Channel".into(),
            udp_destination: "127.0.0.1:5500".into(),
            enabled: true,
        })
        .unwrap();
    let fallback = ChannelRuntimeObservation {
        channel_id: CHANNEL_ID.into(),
        state: ChannelRuntimeState::HealthyFallback,
        on_air_asset_id: None,
        state_started_at_unix_ms: 1_000,
        heartbeat_at_unix_ms: 2_000,
        detail: None,
    };
    store.put_channel_runtime(&fallback).unwrap();
    let mut stale = fallback.clone();
    stale.state = ChannelRuntimeState::Error;
    stale.heartbeat_at_unix_ms = 1_999;
    stale.detail = Some("stale write".into());
    assert!(store.put_channel_runtime(&stale).is_err());
    assert_eq!(
        store.read_channel_runtime(CHANNEL_ID).unwrap(),
        Some(fallback.clone())
    );
    drop(store);

    let reopened = StationStore::open(&database).unwrap();
    assert_eq!(reopened.list_channel_runtime().unwrap(), vec![fallback]);
    drop(reopened);
    remove_database(&database);
}

#[test]
fn persists_latest_bounded_precision_system_resource_truth() {
    let database = database_path("system-resources");
    let store = StationStore::open(&database).unwrap();
    let recorded = store
        .record_system_resource_sample(&SystemResourceSample {
            sample_id: None,
            sampled_at_unix_ms: 1_000,
            cpu_percent: Some(32.1254),
            ram_used_gb: Some(7.5555),
            ram_total_gb: Some(16.0),
            gpu_percent: None,
            gpu_vram_used_gb: None,
            media_volume_free_gb: Some(88.25),
            backup_volume_free_gb: Some(77.5),
            database_reachable: true,
            backup_volume_writable: true,
            service_running: true,
            clock_skew_seconds: Some(-0.125),
        })
        .unwrap();
    assert_eq!(recorded.sample_id, Some(1));
    assert_eq!(recorded.cpu_percent, Some(32.125));
    assert_eq!(recorded.ram_used_gb, Some(7.556));
    assert_eq!(recorded.clock_skew_seconds, Some(-0.125));
    drop(store);

    let reopened = StationStore::open(&database).unwrap();
    assert_eq!(
        reopened.latest_system_resource_sample().unwrap(),
        Some(recorded)
    );
    drop(reopened);
    remove_database(&database);
}

#[test]
fn deduplicates_acknowledges_and_resolves_alerts_durably() {
    let database = database_path("alerts");
    let store = StationStore::open(&database).unwrap();
    let first = store.observe_alert(&alert("alert-first", 1_000)).unwrap();
    assert_eq!(first.occurrence_count, 1);

    let repeated = store.observe_alert(&alert("alert-ignored", 2_000)).unwrap();
    assert_eq!(repeated.event_id, first.event_id);
    assert_eq!(repeated.occurrence_count, 2);
    assert_eq!(store.list_alert_events().unwrap().len(), 1);

    let acknowledged = store
        .acknowledge_alert(&first.event_id, 2_100, "station-operator")
        .unwrap()
        .unwrap();
    assert_eq!(
        acknowledged.acknowledged_by.as_deref(),
        Some("station-operator")
    );
    let resolved = store
        .resolve_alert(AlertConditionKind::EncoderDeath, CHANNEL_ID, 3_000)
        .unwrap()
        .unwrap();
    assert_eq!(resolved.state, AlertEventState::Resolved);

    let fresh = store.observe_alert(&alert("alert-fresh", 4_000)).unwrap();
    assert_eq!(fresh.event_id, "alert-fresh");
    assert_eq!(store.list_alert_events().unwrap().len(), 2);
    drop(store);
    remove_database(&database);
}

#[test]
fn leases_retries_and_dead_letters_alert_delivery_durably() {
    let database = database_path("alert-delivery-retry");
    let store = StationStore::open(&database).unwrap();
    put_webhook_channel(&store, ALERT_CHANNEL_ID);
    let event = store
        .observe_alert(&alert("alert-delivery", 1_000))
        .unwrap();
    let queued = store
        .enqueue_alert_delivery(
            "delivery-alert-pager-firing",
            &event,
            ALERT_CHANNEL_ID,
            AlertNotificationKind::Firing,
            1_100,
            1_100,
        )
        .unwrap();
    assert_eq!(queued.state, AlertDeliveryState::Pending);
    assert_eq!(queued.attempts, 0);
    assert!(
        store
            .claim_due_alert_delivery(1_099, 5_000)
            .unwrap()
            .is_none()
    );

    let first = store
        .claim_due_alert_delivery(1_100, 5_000)
        .unwrap()
        .unwrap();
    assert_eq!(first.state, AlertDeliveryState::Leased);
    assert_eq!(first.attempts, 1);
    assert_eq!(first.lease_expires_at_unix_ms, Some(6_100));
    assert!(
        store
            .claim_due_alert_delivery(2_000, 5_000)
            .unwrap()
            .is_none()
    );
    drop(store);

    let reopened = StationStore::open(&database).unwrap();
    let recovered = reopened
        .claim_due_alert_delivery(6_101, 5_000)
        .unwrap()
        .unwrap();
    assert_eq!(recovered.delivery_id, first.delivery_id);
    assert_eq!(recovered.attempts, 2);
    assert!(recovered.last_error.contains("lease expired"));
    let payload = reopened
        .read_alert_delivery_payload(&recovered.delivery_id)
        .unwrap()
        .unwrap();
    assert_eq!(payload.event_id, event.event_id);
    assert_eq!(payload.summary, event.summary);
    assert_eq!(payload.notification_kind, AlertNotificationKind::Firing);

    let retry = reopened
        .fail_alert_delivery(
            &recovered.delivery_id,
            6_200,
            "provider unavailable",
            3,
            120_000,
        )
        .unwrap();
    assert_eq!(retry.state, AlertDeliveryState::Pending);
    assert_eq!(retry.next_attempt_at_unix_ms, 246_200);
    assert!(
        reopened
            .claim_due_alert_delivery(246_199, 5_000)
            .unwrap()
            .is_none()
    );
    let final_attempt = reopened
        .claim_due_alert_delivery(246_200, 5_000)
        .unwrap()
        .unwrap();
    assert_eq!(final_attempt.attempts, 3);
    let dead_letter = reopened
        .fail_alert_delivery(
            &final_attempt.delivery_id,
            246_300,
            "still unavailable",
            3,
            120_000,
        )
        .unwrap();
    assert_eq!(dead_letter.state, AlertDeliveryState::DeadLetter);
    assert!(dead_letter.next_attempt_at_unix_ms == 246_200);
    assert!(
        reopened
            .claim_due_alert_delivery(1_000_000, 5_000)
            .unwrap()
            .is_none()
    );
    drop(reopened);
    remove_database(&database);
}

#[test]
fn prevents_duplicate_delivery_and_records_sent_terminal_state() {
    let database = database_path("alert-delivery-sent");
    let store = StationStore::open(&database).unwrap();
    put_webhook_channel(&store, ALERT_CHANNEL_ID);
    let event = store.observe_alert(&alert("alert-sent", 2_000)).unwrap();
    store
        .enqueue_alert_delivery(
            "delivery-one",
            &event,
            ALERT_CHANNEL_ID,
            AlertNotificationKind::Firing,
            2_100,
            2_100,
        )
        .unwrap();
    assert!(
        store
            .enqueue_alert_delivery(
                "delivery-two",
                &event,
                ALERT_CHANNEL_ID,
                AlertNotificationKind::Firing,
                2_100,
                2_100,
            )
            .is_err()
    );
    let leased = store
        .claim_due_alert_delivery(2_100, 5_000)
        .unwrap()
        .unwrap();
    let sent = store
        .complete_alert_delivery(&leased.delivery_id, 2_200, Some("sha256=proof"))
        .unwrap();
    assert_eq!(sent.state, AlertDeliveryState::Sent);
    assert_eq!(sent.sent_at_unix_ms, Some(2_200));
    assert_eq!(sent.signature.as_deref(), Some("sha256=proof"));
    assert!(
        store
            .fail_alert_delivery(&leased.delivery_id, 2_300, "late failure", 3, 120_000)
            .is_err()
    );
    drop(store);

    let reopened = StationStore::open(&database).unwrap();
    assert_eq!(reopened.list_alert_deliveries().unwrap(), vec![sent]);
    drop(reopened);
    remove_database(&database);
}

#[test]
fn atomically_routes_first_failure_and_resolution_without_duplicate_pages() {
    let database = database_path("alert-routing-lifecycle");
    let store = StationStore::open(&database).unwrap();
    put_webhook_channel_with_quiet_hours(&store, ALERT_CHANNEL_ID, Some("22:00"), Some("07:00"));
    let first = alert("alert-routed", 82_800_000);
    let recorded = store.observe_alert_and_route(&first).unwrap();
    assert_eq!(recorded.event_id, first.event_id);
    let firing = store.list_alert_deliveries().unwrap();
    assert_eq!(firing.len(), 1);
    assert_eq!(firing[0].notification_kind, AlertNotificationKind::Firing);
    assert_eq!(firing[0].next_attempt_at_unix_ms, 82_800_000);

    let repeated = store
        .observe_alert_and_route(&alert("ignored-duplicate-id", 82_801_000))
        .unwrap();
    assert_eq!(repeated.event_id, first.event_id);
    assert_eq!(repeated.occurrence_count, 2);
    assert_eq!(store.list_alert_deliveries().unwrap().len(), 1);

    let resolved = store
        .resolve_alert(AlertConditionKind::EncoderDeath, CHANNEL_ID, 82_802_000)
        .unwrap()
        .unwrap();
    assert_eq!(resolved.state, AlertEventState::Resolved);
    let deliveries = store.list_alert_deliveries().unwrap();
    assert_eq!(deliveries.len(), 2);
    assert_eq!(
        deliveries[1].notification_kind,
        AlertNotificationKind::Resolved
    );
    assert_eq!(deliveries[1].next_attempt_at_unix_ms, 82_802_000);
    drop(store);
    remove_database(&database);
}

#[test]
fn holds_noncritical_alerts_until_quiet_hours_end() {
    let database = database_path("alert-routing-quiet-hours");
    let store = StationStore::open(&database).unwrap();
    put_webhook_channel_with_quiet_hours(&store, ALERT_CHANNEL_ID, Some("22:00"), Some("07:00"));
    let mut warning = alert("warning-routed", 82_800_000);
    warning.condition = AlertConditionKind::CommitFailure;
    warning.severity = AlertSeverity::Warning;
    store.observe_alert_and_route(&warning).unwrap();
    let delivery = store.list_alert_deliveries().unwrap().pop().unwrap();
    assert_eq!(delivery.next_attempt_at_unix_ms, 111_600_000);
    assert!(
        store
            .claim_due_alert_delivery(111_599_999, 5_000)
            .unwrap()
            .is_none()
    );
    assert!(
        store
            .claim_due_alert_delivery(111_600_000, 5_000)
            .unwrap()
            .is_some()
    );
    drop(store);
    remove_database(&database);
}

#[test]
fn seeds_editable_rules_and_routes_bounded_realerts_to_selected_destinations() {
    let database = database_path("alert-rules-realert");
    let store = StationStore::open(&database).unwrap();
    let defaults = store.list_alert_rules().unwrap();
    let default = defaults
        .iter()
        .find(|rule| rule.condition == AlertConditionKind::EncoderDeath)
        .unwrap();
    assert_eq!(default.severity, AlertSeverity::Critical);
    assert_eq!(default.re_alert_after_seconds, 3_600);

    let selected_channel = "70c12dc7-b29e-426f-a26d-8678102ffb10";
    let excluded_channel = "bf45c318-27a9-4c6c-82f6-d35f430cfaeb";
    put_webhook_channel(&store, selected_channel);
    put_webhook_channel(&store, excluded_channel);
    let rule = AlertRule {
        rule_id: default.rule_id.clone(),
        condition: AlertConditionKind::EncoderDeath,
        enabled: true,
        severity: AlertSeverity::Warning,
        channel_ids: vec![selected_channel.into()],
        dedupe_window_seconds: 900,
        re_alert_after_seconds: 60,
        scope_channel_id: Some(CHANNEL_ID.into()),
        notify_on_resolve: false,
        updated_at_unix_ms: 900,
        updated_by: "station-operator".into(),
    };
    assert_eq!(store.put_alert_rule(&rule).unwrap(), rule);

    let event = store
        .observe_alert_and_route(&alert("rule-owned-event", 1_000))
        .unwrap();
    assert_eq!(event.severity, AlertSeverity::Warning);
    let initial = store.list_alert_deliveries().unwrap();
    assert_eq!(initial.len(), 1);
    assert_eq!(initial[0].alert_channel_id, selected_channel);
    let leased = store
        .claim_due_alert_delivery(1_000, 5_000)
        .unwrap()
        .unwrap();
    store
        .complete_alert_delivery(&leased.delivery_id, 1_001, None)
        .unwrap();

    store
        .observe_alert_and_route(&alert("suppressed-repeat", 60_999))
        .unwrap();
    assert_eq!(store.list_alert_deliveries().unwrap().len(), 1);
    store
        .observe_alert_and_route(&alert("due-repeat", 61_001))
        .unwrap();
    let deliveries = store.list_alert_deliveries().unwrap();
    assert_eq!(deliveries.len(), 2);
    assert_eq!(
        deliveries[1].notification_kind,
        AlertNotificationKind::Realert
    );
    assert_eq!(deliveries[1].alert_channel_id, selected_channel);
    store
        .observe_alert_and_route(&alert("repeat-while-page-pending", 121_001))
        .unwrap();
    assert_eq!(store.list_alert_deliveries().unwrap().len(), 2);
    let leased_realert = store
        .claim_due_alert_delivery(121_001, 5_000)
        .unwrap()
        .unwrap();
    assert_eq!(
        leased_realert.notification_kind,
        AlertNotificationKind::Realert
    );
    store
        .complete_alert_delivery(&leased_realert.delivery_id, 121_002, None)
        .unwrap();
    store
        .observe_alert_and_route(&alert("next-window-realert", 181_002))
        .unwrap();
    assert_eq!(store.list_alert_deliveries().unwrap().len(), 3);

    store
        .resolve_alert(AlertConditionKind::EncoderDeath, CHANNEL_ID, 182_000)
        .unwrap()
        .unwrap();
    assert_eq!(store.list_alert_deliveries().unwrap().len(), 3);
    drop(store);
    remove_database(&database);
}

#[test]
fn seeds_schedules_and_persists_one_scheduled_self_test_per_period() {
    let database = database_path("self-tests");
    let store = StationStore::open(&database).unwrap();
    let schedules = store.list_self_test_schedules().unwrap();
    assert_eq!(schedules.len(), 2);
    assert_eq!(schedules[0].kind, SelfTestKind::Daily);
    assert_eq!(schedules[0].local_hour, 2);
    assert_eq!(schedules[0].weekday, None);
    assert_eq!(schedules[1].kind, SelfTestKind::Weekly);
    assert_eq!(schedules[1].local_hour, 3);
    assert_eq!(schedules[1].weekday, Some(0));

    let edited = SelfTestSchedule {
        kind: SelfTestKind::Daily,
        enabled: true,
        local_hour: 1,
        local_minute: 30,
        weekday: None,
        updated_at_unix_ms: 500,
        updated_by: "station-operator".into(),
    };
    assert_eq!(store.put_self_test_schedule(&edited).unwrap(), edited);

    let record = SystemSelfTest {
        self_test_id: "self-test-daily-1000".into(),
        kind: SelfTestKind::Daily,
        trigger: SelfTestTrigger::Scheduled,
        schedule_period_key: Some("2026-08-21".into()),
        started_at_unix_ms: 1_000,
        finished_at_unix_ms: 1_250,
        status: SelfTestStatus::Warn,
        checks: [
            ("database_integrity".into(), true),
            ("output_health".into(), false),
        ]
        .into_iter()
        .collect(),
        summary: "Core checks passed; output is not commissioned.".into(),
        evidence_path: Some("C:\\ProgramData\\TownLight Station\\self-tests\\daily.json".into()),
    };
    assert_eq!(store.record_system_self_test(&record).unwrap(), record);
    assert!(
        store
            .self_test_exists_for_period(SelfTestKind::Daily, "2026-08-21")
            .unwrap()
    );
    assert!(
        !store
            .self_test_exists_for_period(SelfTestKind::Weekly, "2026-08-21")
            .unwrap()
    );
    assert_eq!(store.list_system_self_tests(10).unwrap(), vec![record]);
    assert!(store.database_integrity_check().unwrap());
    let rehearsal = database.with_extension("restore-proof.db");
    assert!(store.prove_database_restore_round_trip(&rehearsal).unwrap());
    assert!(!rehearsal.exists());
    assert!(!PathBuf::from(format!("{}-wal", rehearsal.display())).exists());
    assert!(!PathBuf::from(format!("{}-shm", rehearsal.display())).exists());

    drop(store);
    let reopened = StationStore::open(&database).unwrap();
    assert_eq!(reopened.list_self_test_schedules().unwrap()[0], edited);
    assert_eq!(reopened.list_system_self_tests(10).unwrap().len(), 1);
    drop(reopened);
    remove_database(&database);
}
