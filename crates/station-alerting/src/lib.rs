//! Durable operator alert delivery.

use std::collections::BTreeMap;
use std::io::{Read, Write};
use std::os::windows::ffi::OsStrExt;
use std::path::{Path, PathBuf};
use std::sync::{
    Arc,
    atomic::{AtomicBool, AtomicU64, Ordering},
};
use std::time::Duration;
use std::time::{SystemTime, UNIX_EPOCH};

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
use hmac::{Hmac, Mac};
use jiff::{Timestamp, tz::TimeZone};
use lettre::message::header::ContentType;
use lettre::transport::smtp::authentication::Credentials;
use lettre::{Message, SmtpTransport, Transport};
use serde::Serialize;
use sha2::Sha256;
use station_domain::{
    AlertChannel, AlertConditionKind, AlertDelivery, AlertDeliveryPayload, AlertEvent,
    AlertEventState, AlertSeverity, ChannelRuntimeState, SelfTestKind, SelfTestStatus,
    SelfTestTrigger, SystemResourceSample, SystemSelfTest,
};
use station_storage::{AlertChannelSecret, StationStore};
use ureq::Agent;
use ureq::tls::{RootCerts, TlsConfig};
use windows_sys::Win32::Foundation::FILETIME;
use windows_sys::Win32::Storage::FileSystem::GetDiskFreeSpaceExW;
use windows_sys::Win32::System::SystemInformation::{GlobalMemoryStatusEx, MEMORYSTATUSEX};
use windows_sys::Win32::System::Threading::GetSystemTimes;

const DEFAULT_LEASE_DURATION_MS: i64 = 30_000;
const DEFAULT_MAXIMUM_ATTEMPTS: u32 = 5;
const DEFAULT_BASE_BACKOFF_MS: i64 = 120_000;
const RESOURCE_SAMPLE_INTERVAL_MS: i64 = 60_000;
const SELF_TEST_SCHEDULE_CHECK_INTERVAL_MS: i64 = 30_000;
const LOW_DISK_GB: f64 = 10.0;
static SELF_TEST_NONCE: AtomicU64 = AtomicU64::new(1);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LocalStationTime {
    pub year: u16,
    pub month: u8,
    pub day: u8,
    pub weekday: u8,
    pub hour: u8,
    pub minute: u8,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeliveryEnvelope {
    pub delivery: AlertDelivery,
    pub channel: AlertChannel,
    pub secret: AlertChannelSecret,
    pub payload: AlertDeliveryPayload,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeliveryReceipt {
    pub signature: Option<String>,
}

pub trait AlertTransport: Send + Sync {
    fn deliver(&self, envelope: &DeliveryEnvelope) -> Result<DeliveryReceipt, String>;
}

pub struct OperatorAlertTransport {
    webhook: WebhookTransport,
    sms: SmsTransport,
    email: EmailTransport,
}

impl OperatorAlertTransport {
    pub fn new(timeout: Duration) -> Self {
        Self {
            webhook: WebhookTransport::new(timeout),
            sms: SmsTransport::new(timeout),
            email: EmailTransport::new(timeout),
        }
    }
}

impl AlertTransport for OperatorAlertTransport {
    fn deliver(&self, envelope: &DeliveryEnvelope) -> Result<DeliveryReceipt, String> {
        match envelope.channel.kind {
            station_domain::AlertChannelKind::Webhook => self.webhook.deliver(envelope),
            station_domain::AlertChannelKind::Sms => self.sms.deliver(envelope),
            station_domain::AlertChannelKind::Email => self.email.deliver(envelope),
        }
    }
}

pub fn run_until(database_path: &Path, stop: Arc<AtomicBool>) -> Result<(), String> {
    let transport = OperatorAlertTransport::new(Duration::from_secs(5));
    let worker = AlertDeliveryWorker::new(database_path, &transport);
    let mut next_resource_sample_at = 0;
    let mut next_self_test_schedule_check_at = 0;
    while !stop.load(Ordering::Acquire) {
        let now_unix_ms = i64::try_from(
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map_err(|_| "system clock is before the Unix epoch".to_string())?
                .as_millis(),
        )
        .map_err(|_| "system time exceeds the alerting clock".to_string())?;
        if now_unix_ms >= next_resource_sample_at {
            sample_and_record_system_resources(database_path, now_unix_ms)?;
            next_resource_sample_at = now_unix_ms.saturating_add(RESOURCE_SAMPLE_INTERVAL_MS);
        }
        if now_unix_ms >= next_self_test_schedule_check_at {
            run_due_system_self_tests(database_path, now_unix_ms)?;
            next_self_test_schedule_check_at =
                now_unix_ms.saturating_add(SELF_TEST_SCHEDULE_CHECK_INTERVAL_MS);
        }
        if !worker.run_once(now_unix_ms)? {
            std::thread::sleep(Duration::from_millis(250));
        }
    }
    Ok(())
}

pub fn run_due_system_self_tests(
    database_path: &Path,
    now_unix_ms: i64,
) -> Result<Vec<SystemSelfTest>, String> {
    let application_directory = std::env::current_exe()
        .map_err(|error| format!("could not locate the station application: {error}"))?
        .parent()
        .ok_or_else(|| "station application has no parent directory".to_string())?
        .to_path_buf();
    run_due_system_self_tests_at(
        database_path,
        now_unix_ms,
        station_local_time(database_path, now_unix_ms)?,
        &application_directory,
    )
}

pub fn run_due_system_self_tests_at(
    database_path: &Path,
    now_unix_ms: i64,
    local: LocalStationTime,
    application_directory: &Path,
) -> Result<Vec<SystemSelfTest>, String> {
    validate_local_station_time(local)?;
    let store = StationStore::open(database_path)?;
    let schedules = store.list_self_test_schedules()?;
    let mut due = Vec::new();
    for schedule in schedules {
        if !schedule.enabled
            || (local.hour, local.minute) < (schedule.local_hour, schedule.local_minute)
            || schedule
                .weekday
                .is_some_and(|weekday| weekday != local.weekday)
        {
            continue;
        }
        let period_key = format!("{:04}-{:02}-{:02}", local.year, local.month, local.day);
        if !store.self_test_exists_for_period(schedule.kind, &period_key)? {
            due.push((schedule.kind, period_key));
        }
    }
    drop(store);
    let mut records = Vec::with_capacity(due.len());
    for (kind, period_key) in due {
        records.push(run_system_self_test_at(
            database_path,
            kind,
            SelfTestTrigger::Scheduled,
            Some(period_key),
            now_unix_ms,
            application_directory,
        )?);
    }
    Ok(records)
}

pub fn run_system_self_test(
    database_path: &Path,
    kind: SelfTestKind,
    trigger: SelfTestTrigger,
    schedule_period_key: Option<String>,
    started_at_unix_ms: i64,
) -> Result<SystemSelfTest, String> {
    let application_directory = std::env::current_exe()
        .map_err(|error| format!("could not locate the station application: {error}"))?
        .parent()
        .ok_or_else(|| "station application has no parent directory".to_string())?
        .to_path_buf();
    run_system_self_test_at(
        database_path,
        kind,
        trigger,
        schedule_period_key,
        started_at_unix_ms,
        &application_directory,
    )
}

pub fn run_system_self_test_at(
    database_path: &Path,
    kind: SelfTestKind,
    trigger: SelfTestTrigger,
    schedule_period_key: Option<String>,
    started_at_unix_ms: i64,
    application_directory: &Path,
) -> Result<SystemSelfTest, String> {
    if started_at_unix_ms < 0
        || (trigger == SelfTestTrigger::Scheduled) != schedule_period_key.is_some()
    {
        return Err("self-test trigger fields are invalid".into());
    }
    let store = StationStore::open(database_path)?;
    let data_directory = database_path
        .parent()
        .ok_or_else(|| "station database has no parent directory".to_string())?;
    let backup_directory = data_directory.join("backups");
    let mut checks = BTreeMap::new();
    checks.insert(
        "database_integrity".into(),
        store.database_integrity_check().unwrap_or(false),
    );
    checks.insert(
        "backup_write_read_delete".into(),
        backup_write_probe(&backup_directory),
    );
    checks.insert(
        "station_service_binary".into(),
        application_directory.join("stationd.exe").is_file(),
    );
    checks.insert(
        "channel_worker_binary".into(),
        application_directory.join("channel-worker.exe").is_file(),
    );
    checks.insert(
        "private_media_runtime".into(),
        application_directory
            .join("runtime/gstreamer/bin/gstreamer-1.0-0.dll")
            .is_file(),
    );
    checks.insert(
        "station_profile_configured".into(),
        store.read_profile().is_ok_and(|profile| profile.is_some()),
    );
    let enabled_channels = store
        .list_channels()
        .unwrap_or_default()
        .into_iter()
        .filter(|channel| channel.enabled)
        .collect::<Vec<_>>();
    checks.insert(
        "enabled_output_configured".into(),
        !enabled_channels.is_empty(),
    );
    checks.insert(
        "enabled_output_health".into(),
        !enabled_channels.is_empty()
            && enabled_channels.iter().all(|channel| {
                store
                    .read_channel_runtime(&channel.channel_id)
                    .is_ok_and(|observation| {
                        observation.is_some_and(|observation| {
                            matches!(
                                observation.state,
                                ChannelRuntimeState::HealthyFallback | ChannelRuntimeState::OnAir
                            ) && started_at_unix_ms
                                .checked_sub(observation.heartbeat_at_unix_ms)
                                .is_some_and(|age| (0..=15_000).contains(&age))
                        })
                    })
            }),
    );
    checks.insert(
        "recent_resource_sample".into(),
        store.latest_system_resource_sample().is_ok_and(|sample| {
            sample.is_some_and(|sample| {
                sample.database_reachable
                    && sample.backup_volume_writable
                    && sample.service_running
                    && started_at_unix_ms
                        .checked_sub(sample.sampled_at_unix_ms)
                        .is_some_and(|age| (0..=120_000).contains(&age))
            })
        }),
    );

    if kind == SelfTestKind::Weekly {
        let rehearsal_path = data_directory.join("self-tests/database-restore-rehearsal.db");
        checks.insert(
            "database_restore_rehearsal".into(),
            store
                .prove_database_restore_round_trip(&rehearsal_path)
                .unwrap_or(false),
        );
        let destinations = store
            .list_alert_channels()
            .unwrap_or_default()
            .into_iter()
            .filter(|channel| channel.enabled)
            .collect::<Vec<_>>();
        let nonce = SELF_TEST_NONCE.fetch_add(1, Ordering::Relaxed);
        let tests_queued = !destinations.is_empty()
            && destinations.iter().enumerate().all(|(index, channel)| {
                let event_id = format!("weekly-self-test-{started_at_unix_ms}-{nonce}-{index}");
                let delivery_id =
                    format!("weekly-self-test-delivery-{started_at_unix_ms}-{nonce}-{index}");
                store
                    .enqueue_alert_channel_test(
                        &event_id,
                        &delivery_id,
                        &channel.channel_id,
                        started_at_unix_ms,
                    )
                    .is_ok()
            });
        checks.insert("alert_destination_test_queued".into(), tests_queued);
    }

    let mandatory = [
        "database_integrity",
        "backup_write_read_delete",
        "station_service_binary",
        "channel_worker_binary",
        "private_media_runtime",
    ];
    let mandatory_failed = mandatory
        .iter()
        .any(|name| checks.get(*name) != Some(&true))
        || (kind == SelfTestKind::Weekly
            && checks.get("database_restore_rehearsal") != Some(&true));
    let status = if mandatory_failed {
        SelfTestStatus::Fail
    } else if checks.values().any(|passed| !passed) {
        SelfTestStatus::Warn
    } else {
        SelfTestStatus::Pass
    };
    let passed = checks.values().filter(|passed| **passed).count();
    let summary = match status {
        SelfTestStatus::Pass => format!("All {passed} implemented {kind:?} checks passed."),
        SelfTestStatus::Warn => format!(
            "{passed} of {} implemented {kind:?} checks passed; commissioning attention remains.",
            checks.len()
        ),
        SelfTestStatus::Fail => format!(
            "{passed} of {} implemented {kind:?} checks passed; a core station check failed.",
            checks.len()
        ),
    };
    let nonce = SELF_TEST_NONCE.fetch_add(1, Ordering::Relaxed);
    let kind_name = match kind {
        SelfTestKind::Daily => "daily",
        SelfTestKind::Weekly => "weekly",
    };
    let self_test_id = format!("self-test-{kind_name}-{started_at_unix_ms}-{nonce}");
    let evidence_directory = data_directory.join("self-tests");
    std::fs::create_dir_all(&evidence_directory)
        .map_err(|error| format!("could not create self-test evidence directory: {error}"))?;
    let evidence_path = evidence_directory.join(format!("{self_test_id}.json"));
    let finished_at_unix_ms = unix_time_milliseconds()?.max(started_at_unix_ms);
    let record = SystemSelfTest {
        self_test_id,
        kind,
        trigger,
        schedule_period_key,
        started_at_unix_ms,
        finished_at_unix_ms,
        status,
        checks,
        summary,
        evidence_path: Some(evidence_path.to_string_lossy().into_owned()),
    };
    record.validate().map_err(str::to_string)?;
    write_self_test_evidence(&evidence_path, &record)?;
    let stored = match store.record_system_self_test(&record) {
        Ok(stored) => stored,
        Err(error) => {
            let _ = std::fs::remove_file(&evidence_path);
            return Err(error);
        }
    };
    let resource_ref = format!("system-self-test-{kind_name}");
    if status == SelfTestStatus::Fail {
        store.observe_alert_and_route(&AlertEvent {
            event_id: format!("self-test-failure-{}", stored.self_test_id),
            condition: AlertConditionKind::SelfTestFail,
            severity: AlertSeverity::Warning,
            state: AlertEventState::Firing,
            resource_ref,
            summary: format!("The {kind_name} station self-test failed."),
            detail: stored.summary.clone(),
            source_section: "S8".into(),
            first_observed_at_unix_ms: stored.finished_at_unix_ms,
            last_observed_at_unix_ms: stored.finished_at_unix_ms,
            resolved_at_unix_ms: None,
            occurrence_count: 1,
            acknowledged_at_unix_ms: None,
            acknowledged_by: None,
        })?;
    } else {
        store.resolve_alert(
            AlertConditionKind::SelfTestFail,
            &resource_ref,
            stored.finished_at_unix_ms,
        )?;
    }
    Ok(stored)
}

fn write_self_test_evidence(path: &Path, record: &SystemSelfTest) -> Result<(), String> {
    let temporary = path.with_extension("json.tmp");
    let bytes = serde_json::to_vec_pretty(record)
        .map_err(|error| format!("could not encode self-test evidence: {error}"))?;
    let publish = (|| {
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)
            .map_err(|error| format!("could not create self-test evidence: {error}"))?;
        file.write_all(&bytes)
            .and_then(|()| file.sync_all())
            .map_err(|error| format!("could not durably write self-test evidence: {error}"))?;
        drop(file);
        std::fs::rename(&temporary, path)
            .map_err(|error| format!("could not publish self-test evidence: {error}"))
    })();
    if publish.is_err() {
        let _ = std::fs::remove_file(&temporary);
    }
    publish
}

fn unix_time_milliseconds() -> Result<i64, String> {
    i64::try_from(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| "system clock is before the Unix epoch".to_string())?
            .as_millis(),
    )
    .map_err(|_| "system time exceeds the station clock".to_string())
}

fn station_local_time(database_path: &Path, now_unix_ms: i64) -> Result<LocalStationTime, String> {
    let store = StationStore::open(database_path)?;
    let timezone = match store.read_profile()? {
        Some(profile) => TimeZone::get(&profile.profile.timezone).map_err(|error| {
            format!(
                "station timezone {} is unavailable: {error}",
                profile.profile.timezone
            )
        })?,
        None => TimeZone::system(),
    };
    let timestamp = Timestamp::from_millisecond(now_unix_ms)
        .map_err(|error| format!("station schedule time is invalid: {error}"))?;
    let local = timestamp.to_zoned(timezone);
    Ok(LocalStationTime {
        year: u16::try_from(local.year())
            .map_err(|_| "station local year is invalid".to_string())?,
        month: u8::try_from(local.month())
            .map_err(|_| "station local month is invalid".to_string())?,
        day: u8::try_from(local.day()).map_err(|_| "station local day is invalid".to_string())?,
        weekday: u8::try_from(local.weekday().to_sunday_zero_offset())
            .map_err(|_| "station local weekday is invalid".to_string())?,
        hour: u8::try_from(local.hour())
            .map_err(|_| "station local hour is invalid".to_string())?,
        minute: u8::try_from(local.minute())
            .map_err(|_| "station local minute is invalid".to_string())?,
    })
}

fn validate_local_station_time(value: LocalStationTime) -> Result<(), String> {
    if value.year < 1970
        || !(1..=12).contains(&value.month)
        || !(1..=31).contains(&value.day)
        || value.weekday > 6
        || value.hour > 23
        || value.minute > 59
    {
        return Err("local station time is invalid".into());
    }
    Ok(())
}

pub fn sample_and_record_system_resources(
    database_path: &Path,
    sampled_at_unix_ms: i64,
) -> Result<SystemResourceSample, String> {
    let sample = probe_system_resources(database_path, sampled_at_unix_ms)?;
    let media_volume_free_gb = sample
        .media_volume_free_gb
        .expect("the Windows resource probe always measures media free space");
    let backup_volume_free_gb = sample
        .backup_volume_free_gb
        .expect("the Windows resource probe always measures backup free space");
    let backup_volume_writable = sample.backup_volume_writable;
    let store = StationStore::open(database_path)?;
    let recorded = store.record_system_resource_sample(&sample)?;
    let disk_unhealthy = media_volume_free_gb < LOW_DISK_GB
        || backup_volume_free_gb < LOW_DISK_GB
        || !backup_volume_writable;
    if disk_unhealthy {
        store.observe_alert_and_route(&AlertEvent {
            event_id: format!("system-resource-disk-{sampled_at_unix_ms}"),
            condition: AlertConditionKind::DiskLow,
            severity: AlertSeverity::Warning,
            state: AlertEventState::Firing,
            resource_ref: "station-storage".into(),
            summary: "Station storage needs operator attention.".into(),
            detail: format!(
                "Media free {:.1} GB; backup free {:.1} GB; backup writable {}.",
                media_volume_free_gb, backup_volume_free_gb, backup_volume_writable
            ),
            source_section: "S8".into(),
            first_observed_at_unix_ms: sampled_at_unix_ms,
            last_observed_at_unix_ms: sampled_at_unix_ms,
            resolved_at_unix_ms: None,
            occurrence_count: 1,
            acknowledged_at_unix_ms: None,
            acknowledged_by: None,
        })?;
    } else {
        store.resolve_alert(
            AlertConditionKind::DiskLow,
            "station-storage",
            sampled_at_unix_ms,
        )?;
    }
    Ok(recorded)
}

pub fn probe_system_resources(
    database_path: &Path,
    sampled_at_unix_ms: i64,
) -> Result<SystemResourceSample, String> {
    let (ram_used_gb, ram_total_gb) = memory_usage()?;
    let cpu_percent = cpu_usage()?;
    let data_directory = database_path
        .parent()
        .ok_or_else(|| "station database has no parent directory".to_string())?;
    let media_volume_free_gb = disk_free_gb(data_directory)?;
    let backup_directory = data_directory.join("backups");
    let backup_volume_writable = backup_write_probe(&backup_directory);
    let backup_volume_free_gb = disk_free_gb(data_directory)?;
    let sample = SystemResourceSample {
        sample_id: None,
        sampled_at_unix_ms,
        cpu_percent: Some(cpu_percent),
        ram_used_gb: Some(ram_used_gb),
        ram_total_gb: Some(ram_total_gb),
        gpu_percent: None,
        gpu_vram_used_gb: None,
        media_volume_free_gb: Some(media_volume_free_gb),
        backup_volume_free_gb: Some(backup_volume_free_gb),
        database_reachable: true,
        backup_volume_writable,
        service_running: true,
        clock_skew_seconds: None,
    };
    sample.validate().map_err(str::to_string)?;
    Ok(sample)
}

fn memory_usage() -> Result<(f64, f64), String> {
    let mut status: MEMORYSTATUSEX = unsafe { std::mem::zeroed() };
    status.dwLength = u32::try_from(std::mem::size_of::<MEMORYSTATUSEX>())
        .map_err(|_| "memory status structure is too large".to_string())?;
    if unsafe { GlobalMemoryStatusEx(&mut status) } == 0 {
        return Err("Windows could not read memory status".into());
    }
    const GIB: f64 = 1_073_741_824.0;
    let total = status.ullTotalPhys as f64 / GIB;
    let available = status.ullAvailPhys as f64 / GIB;
    Ok(((total - available).max(0.0), total))
}

fn cpu_usage() -> Result<f64, String> {
    let first = system_times()?;
    std::thread::sleep(Duration::from_millis(50));
    let second = system_times()?;
    let total = second.1.saturating_sub(first.1) + second.2.saturating_sub(first.2);
    let idle = second.0.saturating_sub(first.0);
    if total == 0 {
        return Ok(0.0);
    }
    Ok((100.0 * (total.saturating_sub(idle)) as f64 / total as f64).clamp(0.0, 100.0))
}

fn system_times() -> Result<(u64, u64, u64), String> {
    let mut idle: FILETIME = unsafe { std::mem::zeroed() };
    let mut kernel: FILETIME = unsafe { std::mem::zeroed() };
    let mut user: FILETIME = unsafe { std::mem::zeroed() };
    if unsafe { GetSystemTimes(&mut idle, &mut kernel, &mut user) } == 0 {
        return Err("Windows could not read processor times".into());
    }
    Ok((
        filetime_ticks(idle),
        filetime_ticks(kernel),
        filetime_ticks(user),
    ))
}

fn filetime_ticks(value: FILETIME) -> u64 {
    (u64::from(value.dwHighDateTime) << 32) | u64::from(value.dwLowDateTime)
}

fn disk_free_gb(path: &Path) -> Result<f64, String> {
    let wide: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
    let mut available = 0_u64;
    if unsafe {
        GetDiskFreeSpaceExW(
            wide.as_ptr(),
            &mut available,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
        )
    } == 0
    {
        return Err(format!(
            "Windows could not read free space for {}",
            path.display()
        ));
    }
    Ok(available as f64 / 1_073_741_824.0)
}

fn backup_write_probe(directory: &Path) -> bool {
    if std::fs::create_dir_all(directory).is_err() {
        return false;
    }
    let path = directory.join(format!(".write-probe-{}", std::process::id()));
    let outcome = (|| -> std::io::Result<()> {
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)?;
        file.write_all(b"townlight-storage-proof")?;
        file.sync_all()?;
        drop(file);
        let mut content = Vec::new();
        std::fs::File::open(&path)?.read_to_end(&mut content)?;
        if content != b"townlight-storage-proof" {
            return Err(std::io::Error::other("backup write probe mismatch"));
        }
        Ok(())
    })();
    let removed = std::fs::remove_file(&path).is_ok();
    outcome.is_ok() && removed
}

pub struct WebhookTransport {
    agent: Agent,
}

impl WebhookTransport {
    pub fn new(timeout: Duration) -> Self {
        let agent = Agent::config_builder()
            .timeout_global(Some(timeout))
            .max_redirects(0)
            .tls_config(
                TlsConfig::builder()
                    .root_certs(RootCerts::PlatformVerifier)
                    .build(),
            )
            .build()
            .new_agent();
        Self { agent }
    }
}

#[derive(Serialize)]
struct WebhookBody<'a> {
    schema_version: u32,
    delivery_id: &'a str,
    notification_kind: station_domain::AlertNotificationKind,
    event: &'a AlertDeliveryPayload,
}

impl AlertTransport for WebhookTransport {
    fn deliver(&self, envelope: &DeliveryEnvelope) -> Result<DeliveryReceipt, String> {
        envelope.delivery.validate().map_err(str::to_string)?;
        envelope.channel.validate().map_err(str::to_string)?;
        envelope.payload.validate().map_err(str::to_string)?;
        envelope.secret.validate(envelope.channel.kind)?;
        if envelope.channel.kind != station_domain::AlertChannelKind::Webhook
            || envelope.delivery.alert_channel_id != envelope.channel.channel_id
            || envelope.delivery.event_id != envelope.payload.event_id
            || envelope.delivery.notification_kind != envelope.payload.notification_kind
        {
            return Err("webhook delivery envelope is inconsistent".into());
        }
        let body = serde_json::to_vec(&WebhookBody {
            schema_version: 1,
            delivery_id: &envelope.delivery.delivery_id,
            notification_kind: envelope.delivery.notification_kind,
            event: &envelope.payload,
        })
        .map_err(|_| "webhook payload could not be encoded".to_string())?;
        let signature = sign_webhook_payload(envelope.secret.credential.as_bytes(), &body);
        let response = self
            .agent
            .post(&envelope.secret.target)
            .header("Content-Type", "application/json")
            .header("X-TownLight-Delivery", &envelope.delivery.delivery_id)
            .header("X-TownLight-Signature", &signature)
            .send(&body)
            .map_err(webhook_request_error)?;
        if !response.status().is_success() {
            return Err(format!(
                "webhook provider returned HTTP {}",
                response.status().as_u16()
            ));
        }
        Ok(DeliveryReceipt {
            signature: Some(signature),
        })
    }
}

pub struct SmsTransport {
    agent: Agent,
}

impl SmsTransport {
    pub fn new(timeout: Duration) -> Self {
        let agent = Agent::config_builder()
            .timeout_global(Some(timeout))
            .max_redirects(0)
            .tls_config(
                TlsConfig::builder()
                    .root_certs(RootCerts::PlatformVerifier)
                    .build(),
            )
            .build()
            .new_agent();
        Self { agent }
    }
}

impl AlertTransport for SmsTransport {
    fn deliver(&self, envelope: &DeliveryEnvelope) -> Result<DeliveryReceipt, String> {
        envelope.delivery.validate().map_err(str::to_string)?;
        envelope.channel.validate().map_err(str::to_string)?;
        envelope.payload.validate().map_err(str::to_string)?;
        envelope.secret.validate(envelope.channel.kind)?;
        if envelope.channel.kind != station_domain::AlertChannelKind::Sms
            || envelope.delivery.alert_channel_id != envelope.channel.channel_id
            || envelope.delivery.event_id != envelope.payload.event_id
            || envelope.delivery.notification_kind != envelope.payload.notification_kind
        {
            return Err("SMS delivery envelope is inconsistent".into());
        }
        let endpoint = envelope
            .secret
            .endpoint
            .as_deref()
            .ok_or_else(|| "SMS provider endpoint is missing".to_string())?;
        let sender = envelope
            .secret
            .sender
            .as_deref()
            .ok_or_else(|| "SMS provider sender is missing".to_string())?;
        let identity = envelope
            .secret
            .identity
            .as_deref()
            .ok_or_else(|| "SMS provider identity is missing".to_string())?;
        let authorization = format!(
            "Basic {}",
            BASE64_STANDARD.encode(format!("{identity}:{}", envelope.secret.credential))
        );
        let message = build_sms_message(&envelope.payload);
        let form = [
            ("To", envelope.secret.target.as_str()),
            ("From", sender),
            ("Body", message.as_str()),
        ];
        let response = self
            .agent
            .post(endpoint)
            .header("Authorization", &authorization)
            .send_form(form)
            .map_err(sms_request_error)?;
        if !response.status().is_success() {
            return Err(format!(
                "SMS provider returned HTTP {}",
                response.status().as_u16()
            ));
        }
        Ok(DeliveryReceipt { signature: None })
    }
}

pub struct EmailTransport {
    timeout: Duration,
}

impl EmailTransport {
    pub fn new(timeout: Duration) -> Self {
        Self { timeout }
    }
}

impl AlertTransport for EmailTransport {
    fn deliver(&self, envelope: &DeliveryEnvelope) -> Result<DeliveryReceipt, String> {
        envelope.delivery.validate().map_err(str::to_string)?;
        envelope.channel.validate().map_err(str::to_string)?;
        envelope.payload.validate().map_err(str::to_string)?;
        envelope.secret.validate(envelope.channel.kind)?;
        if envelope.channel.kind != station_domain::AlertChannelKind::Email
            || envelope.delivery.alert_channel_id != envelope.channel.channel_id
            || envelope.delivery.event_id != envelope.payload.event_id
            || envelope.delivery.notification_kind != envelope.payload.notification_kind
        {
            return Err("email delivery envelope is inconsistent".into());
        }
        let endpoint = envelope
            .secret
            .endpoint
            .as_deref()
            .ok_or_else(|| "SMTP endpoint is missing".to_string())?;
        let sender = envelope
            .secret
            .sender
            .as_deref()
            .ok_or_else(|| "SMTP sender is missing".to_string())?;
        let identity = envelope
            .secret
            .identity
            .as_deref()
            .ok_or_else(|| "SMTP identity is missing".to_string())?;
        let (secure, host, port) = parse_smtp_endpoint(endpoint)?;
        let severity = alert_severity_label(envelope.payload.severity);
        let condition = alert_condition_label(envelope.payload.condition);
        let subject = format!(
            "[TownLight {severity}] {condition} — {}",
            envelope.payload.resource_ref
        );
        let state = match envelope.payload.notification_kind {
            station_domain::AlertNotificationKind::Firing => "FIRING",
            station_domain::AlertNotificationKind::Realert => "STILL FIRING",
            station_domain::AlertNotificationKind::Resolved => "RESOLVED",
            station_domain::AlertNotificationKind::Test => "TEST",
        };
        let body = format!(
            "TownLight Station operational alert\n\nState: {state}\nSeverity: {severity}\nCondition: {condition}\nResource: {}\n\n{}\n\n{}\n",
            envelope.payload.resource_ref, envelope.payload.summary, envelope.payload.detail
        );
        let message = Message::builder()
            .from(
                sender
                    .parse()
                    .map_err(|_| "SMTP sender address is invalid".to_string())?,
            )
            .to(envelope
                .secret
                .target
                .parse()
                .map_err(|_| "SMTP target address is invalid".to_string())?)
            .subject(subject)
            .header(ContentType::TEXT_PLAIN)
            .body(body)
            .map_err(|_| "alert email could not be constructed".to_string())?;
        let credentials = Credentials::new(identity.into(), envelope.secret.credential.clone());
        let mailer = if secure {
            SmtpTransport::relay(&host)
                .map_err(|_| "secure SMTP relay configuration is invalid".to_string())?
                .port(port)
                .credentials(credentials)
                .timeout(Some(self.timeout))
                .build()
        } else {
            SmtpTransport::builder_dangerous(&host)
                .port(port)
                .credentials(credentials)
                .timeout(Some(self.timeout))
                .build()
        };
        mailer.send(&message).map_err(|_| {
            "SMTP transport failed before the relay accepted the message".to_string()
        })?;
        Ok(DeliveryReceipt { signature: None })
    }
}

fn parse_smtp_endpoint(endpoint: &str) -> Result<(bool, String, u16), String> {
    let (secure, authority, default_port) = if let Some(value) = endpoint.strip_prefix("smtps://") {
        (true, value, 465)
    } else if let Some(value) = endpoint.strip_prefix("smtp://") {
        (false, value, 25)
    } else {
        return Err("SMTP endpoint scheme is invalid".into());
    };
    if authority.is_empty()
        || authority.contains(['/', '?', '#', '@'])
        || authority.chars().any(char::is_whitespace)
    {
        return Err("SMTP endpoint authority is invalid".into());
    }
    let (host, port) = match authority.rsplit_once(':') {
        Some((host, port))
            if !host.is_empty() && port.bytes().all(|byte| byte.is_ascii_digit()) =>
        {
            let port = port
                .parse::<u16>()
                .ok()
                .filter(|value| *value > 0)
                .ok_or_else(|| "SMTP endpoint port is invalid".to_string())?;
            (host, port)
        }
        Some(_) => return Err("SMTP endpoint authority is invalid".into()),
        None => (authority, default_port),
    };
    let loopback = host.eq_ignore_ascii_case("localhost") || host == "127.0.0.1";
    if !secure && !loopback {
        return Err("remote SMTP endpoints must use smtps".into());
    }
    Ok((secure, host.into(), port))
}

fn alert_severity_label(severity: station_domain::AlertSeverity) -> &'static str {
    match severity {
        station_domain::AlertSeverity::Critical => "CRITICAL",
        station_domain::AlertSeverity::Warning => "WARNING",
        station_domain::AlertSeverity::Info => "INFO",
    }
}

fn alert_condition_label(condition: station_domain::AlertConditionKind) -> &'static str {
    use station_domain::AlertConditionKind;
    match condition {
        AlertConditionKind::OffAir => "off-air",
        AlertConditionKind::OutputStall => "output-stall",
        AlertConditionKind::EncoderDeath => "encoder-death",
        AlertConditionKind::ServerCrash => "server-crash",
        AlertConditionKind::SchemaDrift => "schema-drift",
        AlertConditionKind::RelayBlocked => "relay-blocked",
        AlertConditionKind::ComplianceProbeFail => "compliance-probe-fail",
        AlertConditionKind::MissingMedia => "missing-media",
        AlertConditionKind::ScheduleNoMatch => "schedule-no-match",
        AlertConditionKind::CommitFailure => "commit-failure",
        AlertConditionKind::TakeoverStuck2h => "takeover-stuck-2h",
        AlertConditionKind::AiRuntimeDown => "ai-runtime-down",
        AlertConditionKind::DiskLow => "disk-low",
        AlertConditionKind::ClockSkew => "clock-skew",
        AlertConditionKind::DatabaseUnreachable => "database-unreachable",
        AlertConditionKind::ServiceDown => "service-down",
        AlertConditionKind::SelfTestFail => "self-test-fail",
        AlertConditionKind::UpdateAvailable => "update-available",
        AlertConditionKind::UpdateCheckFailed => "update-check-failed",
        AlertConditionKind::UpdateApplyFailed => "update-apply-failed",
        AlertConditionKind::ScheduleMaterializationFailure => "schedule-materialization-failure",
        AlertConditionKind::AsRunProjectionFailure => "as-run-projection-failure",
    }
}

pub fn build_sms_message(payload: &AlertDeliveryPayload) -> String {
    let severity = match payload.severity {
        station_domain::AlertSeverity::Critical => "CRITICAL",
        station_domain::AlertSeverity::Warning => "WARNING",
        station_domain::AlertSeverity::Info => "INFO",
    };
    let message = format!(
        "TownLight {severity}: {} [{}]",
        payload.summary, payload.resource_ref
    );
    if message.chars().count() <= 140 {
        return message;
    }
    let mut shortened: String = message.chars().take(139).collect();
    shortened.push('…');
    shortened
}

pub fn sign_webhook_payload(key: &[u8], payload: &[u8]) -> String {
    let mut mac = Hmac::<Sha256>::new_from_slice(key).expect("HMAC accepts keys of any size");
    mac.update(payload);
    mac.finalize()
        .into_bytes()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn webhook_request_error(error: ureq::Error) -> String {
    match error {
        ureq::Error::StatusCode(status) => {
            format!("webhook provider returned HTTP {status}")
        }
        _ => "webhook transport failed before a successful provider response".into(),
    }
}

fn sms_request_error(error: ureq::Error) -> String {
    match error {
        ureq::Error::StatusCode(status) => {
            format!("SMS provider returned HTTP {status}")
        }
        _ => "SMS transport failed before a successful provider response".into(),
    }
}

pub struct AlertDeliveryWorker<'a, T: AlertTransport + ?Sized> {
    database_path: PathBuf,
    transport: &'a T,
    lease_duration_ms: i64,
    maximum_attempts: u32,
    base_backoff_ms: i64,
}

impl<'a, T: AlertTransport + ?Sized> AlertDeliveryWorker<'a, T> {
    pub fn new(database_path: impl AsRef<Path>, transport: &'a T) -> Self {
        Self {
            database_path: database_path.as_ref().to_path_buf(),
            transport,
            lease_duration_ms: DEFAULT_LEASE_DURATION_MS,
            maximum_attempts: DEFAULT_MAXIMUM_ATTEMPTS,
            base_backoff_ms: DEFAULT_BASE_BACKOFF_MS,
        }
    }

    pub fn run_once(&self, now_unix_ms: i64) -> Result<bool, String> {
        let store = StationStore::open(&self.database_path)?;
        let Some(delivery) = store.claim_due_alert_delivery(now_unix_ms, self.lease_duration_ms)?
        else {
            return Ok(false);
        };
        let delivery_id = delivery.delivery_id.clone();
        let outcome = self
            .prepare_envelope(&store, delivery)
            .and_then(|envelope| {
                self.transport
                    .deliver(&envelope)
                    .map(|receipt| (envelope, receipt))
            });
        match outcome {
            Ok((envelope, receipt)) => {
                store.complete_alert_delivery(
                    &envelope.delivery.delivery_id,
                    now_unix_ms,
                    receipt.signature.as_deref(),
                )?;
            }
            Err(error) => {
                let error = safe_error(&error);
                store.fail_alert_delivery(
                    &delivery_id,
                    now_unix_ms,
                    &error,
                    self.maximum_attempts,
                    self.base_backoff_ms,
                )?;
            }
        }
        Ok(true)
    }

    fn prepare_envelope(
        &self,
        store: &StationStore,
        delivery: AlertDelivery,
    ) -> Result<DeliveryEnvelope, String> {
        let channel = store
            .read_alert_channel(&delivery.alert_channel_id)?
            .ok_or_else(|| "alert delivery channel disappeared".to_string())?;
        if !channel.enabled
            && delivery.notification_kind != station_domain::AlertNotificationKind::Test
        {
            return Err("alert delivery channel is disabled".into());
        }
        let secret = store
            .read_alert_channel_secret(&delivery.alert_channel_id)?
            .ok_or_else(|| "alert delivery secret disappeared".to_string())?;
        let payload = store
            .read_alert_delivery_payload(&delivery.delivery_id)?
            .ok_or_else(|| "alert delivery payload disappeared".to_string())?;
        Ok(DeliveryEnvelope {
            delivery,
            channel,
            secret,
            payload,
        })
    }
}

fn safe_error(error: &str) -> String {
    let mut value: String = error
        .chars()
        .map(|character| {
            if character.is_control() {
                ' '
            } else {
                character
            }
        })
        .take(1_000)
        .collect();
    if value.trim().is_empty() {
        value = "alert transport failed without a usable error".into();
    }
    value
}
