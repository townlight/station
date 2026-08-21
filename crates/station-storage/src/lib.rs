use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::ffi::{CStr, CString, c_char, c_int, c_longlong, c_void};
use std::os::windows::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::ptr;
use std::{
    fs,
    io::Write,
    sync::{
        Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::{Instant, SystemTime},
};

use sha2::{Digest, Sha256};
use station_cg::{
    CgAuditEvent, CgAuditEventKind, CgBoard, CgBulletin, CgBulletinContent, CgBulletinState,
    CgContentSource, CgFeedCacheEntry, CgFeedItem, CgFeedItemApproval, CgFeedKind, CgFeedSource,
    CgFeedTrustTier, CgImageAsset, CgImageReadiness, CgSnapshotInputs, CgZoneConfig, CgZoneKind,
    built_in_template, inspect_cg_image, managed_cg_image_path,
};
use station_domain::{
    AlertChannel, AlertChannelKind, AlertConditionKind, AlertDelivery, AlertDeliveryPayload,
    AlertDeliveryState, AlertEvent, AlertEventState, AlertNotificationKind, AlertRule,
    AlertSeverity, ChannelRuntimeObservation, ChannelRuntimeState, SelfTestKind, SelfTestSchedule,
    SelfTestStatus, SelfTestTrigger, StationProfile, StationProfileDocument, SystemResourceSample,
    SystemSelfTest, redact_alert_channel_target, validate_alert_channel_target,
    validate_secure_url,
};
use station_media_lifecycle::{
    AssetLoudnessAnalysis, IngestJobStatus, IngestSourceKind, LoudnessGateStatus, MediaIngestJob,
    TranscodeFormat, TranscodeJob, TranscodeJobStatus, WatchFileObservation, WatchFolderConfig,
};
use station_schedule::{
    AssetReadiness, ChannelConfiguration, CommitPlan, CommitReport, DispatchJob, DispatchStatus,
    MediaAsset, OutputProfileId, OutputSinkConfiguration, OutputSinkKind, ScheduleItem,
    ScheduleState, prepare_commit,
};
use station_update::{
    PreparedSlotRecord, StagedPackageRecord, UpdateCheckOutcome, UpdateCheckRecord,
    UpdateCheckTrigger,
};

const SQLITE_OK: c_int = 0;
const SQLITE_ROW: c_int = 100;
const SQLITE_DONE: c_int = 101;
const SQLITE_OPEN_READWRITE: c_int = 0x0000_0002;
const SQLITE_OPEN_CREATE: c_int = 0x0000_0004;
const SQLITE_OPEN_FULLMUTEX: c_int = 0x0001_0000;
const CG_IMAGE_VERIFICATION_CACHE_LIMIT: usize = 256;
const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x0000_0400;
static CG_IMAGE_TEMP_SEQUENCE: AtomicU64 = AtomicU64::new(1);

#[repr(C)]
struct Sqlite3 {
    _private: [u8; 0],
}

#[repr(C)]
struct SqliteStatement {
    _private: [u8; 0],
}

#[link(name = "winsqlite3")]
unsafe extern "C" {
    fn sqlite3_open_v2(
        filename: *const c_char,
        database: *mut *mut Sqlite3,
        flags: c_int,
        virtual_file_system: *const c_char,
    ) -> c_int;
    fn sqlite3_close(database: *mut Sqlite3) -> c_int;
    fn sqlite3_exec(
        database: *mut Sqlite3,
        sql: *const c_char,
        callback: *mut c_void,
        callback_argument: *mut c_void,
        error_message: *mut *mut c_char,
    ) -> c_int;
    fn sqlite3_prepare_v2(
        database: *mut Sqlite3,
        sql: *const c_char,
        byte_count: c_int,
        statement: *mut *mut SqliteStatement,
        tail: *mut *const c_char,
    ) -> c_int;
    fn sqlite3_bind_text(
        statement: *mut SqliteStatement,
        index: c_int,
        value: *const c_char,
        byte_count: c_int,
        destructor: *mut c_void,
    ) -> c_int;
    fn sqlite3_bind_int64(
        statement: *mut SqliteStatement,
        index: c_int,
        value: c_longlong,
    ) -> c_int;
    fn sqlite3_bind_blob(
        statement: *mut SqliteStatement,
        index: c_int,
        value: *const c_void,
        byte_count: c_int,
        destructor: *mut c_void,
    ) -> c_int;
    fn sqlite3_step(statement: *mut SqliteStatement) -> c_int;
    fn sqlite3_column_text(statement: *mut SqliteStatement, column: c_int) -> *const u8;
    fn sqlite3_column_blob(statement: *mut SqliteStatement, column: c_int) -> *const c_void;
    fn sqlite3_column_bytes(statement: *mut SqliteStatement, column: c_int) -> c_int;
    fn sqlite3_column_int64(statement: *mut SqliteStatement, column: c_int) -> c_longlong;
    fn sqlite3_finalize(statement: *mut SqliteStatement) -> c_int;
    fn sqlite3_errmsg(database: *mut Sqlite3) -> *const c_char;
}

#[repr(C)]
struct DataBlob {
    byte_count: u32,
    data: *mut u8,
}

#[link(name = "crypt32")]
unsafe extern "system" {
    fn CryptProtectData(
        input: *mut DataBlob,
        description: *const u16,
        entropy: *mut DataBlob,
        reserved: *mut c_void,
        prompt: *mut c_void,
        flags: u32,
        output: *mut DataBlob,
    ) -> i32;
    fn CryptUnprotectData(
        input: *mut DataBlob,
        description: *mut *mut u16,
        entropy: *mut DataBlob,
        reserved: *mut c_void,
        prompt: *mut c_void,
        flags: u32,
        output: *mut DataBlob,
    ) -> i32;
}

#[link(name = "kernel32")]
unsafe extern "system" {
    fn LocalFree(memory: *mut c_void) -> *mut c_void;
    fn GetLastError() -> u32;
}

const CRYPTPROTECT_UI_FORBIDDEN: u32 = 0x1;
const CRYPTPROTECT_LOCAL_MACHINE: u32 = 0x4;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProfileWriteError {
    Conflict(u64),
    Storage(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CommitWriteError {
    NotFound,
    GateFailed(Box<CommitPlan>),
    Invalid(&'static str),
    Storage(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CgWriteError {
    NotFound,
    Conflict(u64),
    Invalid(&'static str),
    Storage(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CgSnapshotReadError {
    InvalidChannel,
    InvalidTimestamp,
    ChannelNotFound,
    BoardNotFound,
    BoardInactive,
    InvalidData(String),
    Storage(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AlertChannelSecret {
    pub target: String,
    pub endpoint: Option<String>,
    pub sender: Option<String>,
    pub identity: Option<String>,
    pub credential: String,
}

impl AlertChannelSecret {
    pub fn validate(&self, kind: AlertChannelKind) -> Result<(), String> {
        validate_alert_channel_target(kind, &self.target).map_err(str::to_string)?;
        if !secret_field(&self.credential, 2_000) {
            return Err("alert destination credential is invalid".into());
        }
        match kind {
            AlertChannelKind::Webhook => {
                if self.endpoint.is_some() || self.sender.is_some() || self.identity.is_some() {
                    return Err("webhook destinations do not accept mail or SMS fields".into());
                }
            }
            AlertChannelKind::Email => {
                let endpoint = self
                    .endpoint
                    .as_deref()
                    .ok_or("email destinations require an SMTP endpoint")?;
                if !valid_smtp_endpoint(endpoint)
                    || !self.sender.as_deref().is_some_and(|value| {
                        validate_alert_channel_target(AlertChannelKind::Email, value).is_ok()
                    })
                    || !self
                        .identity
                        .as_deref()
                        .is_some_and(|value| secret_field(value, 500))
                {
                    return Err("email destination delivery fields are invalid".into());
                }
            }
            AlertChannelKind::Sms => {
                let endpoint = self
                    .endpoint
                    .as_deref()
                    .ok_or("SMS destinations require a provider endpoint")?;
                if validate_secure_url(endpoint, "SMS provider").is_err()
                    || !self.sender.as_deref().is_some_and(|value| {
                        validate_alert_channel_target(AlertChannelKind::Sms, value).is_ok()
                    })
                    || !self
                        .identity
                        .as_deref()
                        .is_some_and(|value| secret_field(value, 500))
                {
                    return Err("SMS destination delivery fields are invalid".into());
                }
            }
        }
        Ok(())
    }
}

pub struct SrtOutputSecret {
    passphrase: String,
}

impl SrtOutputSecret {
    pub fn expose(&self) -> &str {
        &self.passphrase
    }
}

pub struct StationStore {
    database: *mut Sqlite3,
    _data_root: PathBuf,
    cg_image_root: PathBuf,
    cg_image_verification_cache: Mutex<HashMap<String, CachedCgImageVerification>>,
    cg_image_verification_count: AtomicU64,
}

struct CachedCgImageVerification {
    byte_len: u64,
    modified: Option<SystemTime>,
    checked_at: Instant,
    authority: CgImageAsset,
    asset: CgImageAsset,
}

impl StationStore {
    pub fn open(database_path: impl AsRef<Path>) -> Result<Self, String> {
        let database_path = database_path.as_ref();
        let data_root = station_data_root(database_path)?;
        fs::create_dir_all(&data_root).map_err(|error| format!("create data root: {error}"))?;
        let data_root = fs::canonicalize(&data_root)
            .map_err(|error| format!("canonicalize data root: {error}"))?;
        let cg_image_root = data_root.join("cg-images");
        fs::create_dir_all(&cg_image_root)
            .map_err(|error| format!("create CG image root: {error}"))?;
        let cg_image_root = fs::canonicalize(&cg_image_root)
            .map_err(|error| format!("canonicalize CG image root: {error}"))?;
        if cg_image_root.parent() != Some(data_root.as_path()) {
            return Err("CG image root escapes station data root".into());
        }
        let filename = CString::new(database_path.to_string_lossy().as_bytes())
            .map_err(|_| "database path contains a null byte".to_string())?;
        let mut database = ptr::null_mut();
        let flags = SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX;
        // SAFETY: `filename` is a valid C string, `database` points to writable storage, and the
        // returned handle is owned by `StationStore` until `Drop` closes it.
        let status =
            unsafe { sqlite3_open_v2(filename.as_ptr(), &mut database, flags, ptr::null()) };
        if status != SQLITE_OK {
            let message = sqlite_error(database);
            if !database.is_null() {
                // SAFETY: SQLite returned this handle and it has not been closed.
                unsafe { sqlite3_close(database) };
            }
            return Err(message);
        }

        let store = Self {
            database,
            _data_root: data_root,
            cg_image_root,
            cg_image_verification_cache: Mutex::new(HashMap::new()),
            cg_image_verification_count: AtomicU64::new(0),
        };
        store.execute("PRAGMA busy_timeout=5000;")?;
        store.execute(
            "PRAGMA journal_mode=WAL;\
             PRAGMA foreign_keys=ON;\
             CREATE TABLE IF NOT EXISTS station_profile (\
               singleton INTEGER PRIMARY KEY CHECK (singleton = 1),\
               station_id TEXT NOT NULL,\
               display_name TEXT NOT NULL,\
               timezone TEXT NOT NULL,\
               revision INTEGER NOT NULL DEFAULT 1\
             );\
             CREATE TABLE IF NOT EXISTS media_assets (\
               asset_id TEXT PRIMARY KEY,\
               media_path TEXT NOT NULL,\
               duration_ms INTEGER NOT NULL CHECK (duration_ms > 0),\
               readiness TEXT NOT NULL CHECK (readiness IN ('ready','missing','rejected','processing'))\
             );\
             CREATE TABLE IF NOT EXISTS asset_loudness_analysis (\
               asset_id TEXT PRIMARY KEY,\
               status TEXT NOT NULL CHECK (status IN ('ok','failed','not_checked')),\
               measured_lufs_milli INTEGER NOT NULL DEFAULT -2147483648,\
               target_lufs_milli INTEGER NOT NULL,\
               detail TEXT NOT NULL DEFAULT '',\
               FOREIGN KEY(asset_id) REFERENCES media_assets(asset_id) ON DELETE CASCADE\
             );\
             CREATE TABLE IF NOT EXISTS media_ingest_jobs (\
               job_id TEXT PRIMARY KEY,\
               source_kind TEXT NOT NULL CHECK (source_kind IN ('operator_path','watch_folder','live_finalization')),\
               source_path TEXT NOT NULL,\
               status TEXT NOT NULL CHECK (status IN ('pending','running','awaiting_transcode','completed','failed','cancelled')),\
               progress_percent INTEGER NOT NULL CHECK (progress_percent BETWEEN 0 AND 100),\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               started_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (started_at_unix_ms >= -1),\
               completed_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (completed_at_unix_ms >= -1),\
               error_detail TEXT NOT NULL DEFAULT '',\
               asset_id TEXT NOT NULL DEFAULT '',\
               attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),\
               next_attempt_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (next_attempt_at_unix_ms >= -1)\
             );\
             CREATE INDEX IF NOT EXISTS media_ingest_jobs_status_time \
               ON media_ingest_jobs(status, created_at_unix_ms);\
             CREATE TABLE IF NOT EXISTS transcode_jobs (\
               job_id TEXT PRIMARY KEY,\
               ingest_job_id TEXT NOT NULL UNIQUE,\
               source_asset_id TEXT NOT NULL,\
               source_path TEXT NOT NULL,\
               output_format TEXT NOT NULL CHECK (output_format IN ('h264_720p4mbps_aac192k_ts')),\
               status TEXT NOT NULL CHECK (status IN ('pending','running','completed','failed','cancelled')),\
               progress_percent INTEGER NOT NULL CHECK (progress_percent BETWEEN 0 AND 100),\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               started_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (started_at_unix_ms >= -1),\
               completed_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (completed_at_unix_ms >= -1),\
               output_asset_id TEXT NOT NULL DEFAULT '',\
               error_detail TEXT NOT NULL DEFAULT '',\
               attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),\
               next_attempt_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (next_attempt_at_unix_ms >= -1)\
             );\
             CREATE INDEX IF NOT EXISTS transcode_jobs_status_time \
               ON transcode_jobs(status, next_attempt_at_unix_ms, created_at_unix_ms);\
             CREATE TABLE IF NOT EXISTS watch_folder_configs (\
               config_id TEXT PRIMARY KEY,\
               display_name TEXT NOT NULL,\
               monitor_path TEXT NOT NULL,\
               extensions_json TEXT NOT NULL,\
               settle_seconds INTEGER NOT NULL CHECK (settle_seconds BETWEEN 1 AND 3600),\
               enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),\
               last_scan_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (last_scan_at_unix_ms >= -1),\
               last_error TEXT NOT NULL DEFAULT ''\
             );\
             CREATE TABLE IF NOT EXISTS watch_file_observations (\
               config_id TEXT NOT NULL,\
               source_path TEXT NOT NULL,\
               size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),\
               modified_at_unix_ms INTEGER NOT NULL CHECK (modified_at_unix_ms >= 0),\
               unchanged_since_unix_ms INTEGER NOT NULL CHECK (unchanged_since_unix_ms >= 0),\
               observation_count INTEGER NOT NULL CHECK (observation_count >= 1),\
               queued_job_id TEXT NOT NULL DEFAULT '',\
               PRIMARY KEY(config_id, source_path),\
               FOREIGN KEY(config_id) REFERENCES watch_folder_configs(config_id) ON DELETE CASCADE\
             );\
             CREATE TABLE IF NOT EXISTS channels (\
               channel_id TEXT PRIMARY KEY,\
               display_name TEXT NOT NULL,\
               udp_destination TEXT NOT NULL,\
               output_profile TEXT NOT NULL DEFAULT 'proof-360p30',\
               enabled INTEGER NOT NULL CHECK (enabled IN (0,1))\
             );\
             CREATE TABLE IF NOT EXISTS channel_runtime (\
               channel_id TEXT PRIMARY KEY,\
               state TEXT NOT NULL CHECK (state IN ('starting','healthy_fallback','on_air','error','stopped')),\
               on_air_asset_id TEXT NOT NULL DEFAULT '',\
               state_started_at_unix_ms INTEGER NOT NULL CHECK (state_started_at_unix_ms >= 0),\
               heartbeat_at_unix_ms INTEGER NOT NULL CHECK (heartbeat_at_unix_ms >= state_started_at_unix_ms),\
               detail TEXT NOT NULL DEFAULT '',\
               FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE\
             );\
             CREATE TABLE IF NOT EXISTS channel_output_sinks (\
               channel_id TEXT NOT NULL,\
               sink_id TEXT NOT NULL,\
               label TEXT NOT NULL,\
               kind TEXT NOT NULL CHECK (kind IN ('udp-ts','srt')),\
               destination TEXT NOT NULL,\
               loudness_target_milli INTEGER NOT NULL,\
               required INTEGER NOT NULL CHECK (required IN (0,1)),\
               enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),\
               PRIMARY KEY(channel_id, sink_id),\
               UNIQUE(channel_id, label COLLATE NOCASE),\
               FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE\
             );\
             CREATE TABLE IF NOT EXISTS alert_events (\
               event_id TEXT PRIMARY KEY,\
               condition TEXT NOT NULL,\
               severity TEXT NOT NULL CHECK (severity IN ('critical','warning','info')),\
               state TEXT NOT NULL CHECK (state IN ('firing','resolved')),\
               resource_ref TEXT NOT NULL,\
               summary TEXT NOT NULL,\
               detail TEXT NOT NULL DEFAULT '',\
               source_section TEXT NOT NULL,\
               first_observed_at_unix_ms INTEGER NOT NULL CHECK (first_observed_at_unix_ms >= 0),\
               last_observed_at_unix_ms INTEGER NOT NULL CHECK (last_observed_at_unix_ms >= first_observed_at_unix_ms),\
               resolved_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (resolved_at_unix_ms >= -1),\
               occurrence_count INTEGER NOT NULL CHECK (occurrence_count >= 1),\
               acknowledged_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (acknowledged_at_unix_ms >= -1),\
               acknowledged_by TEXT NOT NULL DEFAULT ''\
             );\
             CREATE UNIQUE INDEX IF NOT EXISTS alert_events_one_firing_condition_resource \
               ON alert_events(condition, resource_ref) WHERE state='firing';\
             CREATE INDEX IF NOT EXISTS alert_events_state_severity_time \
               ON alert_events(state, severity, last_observed_at_unix_ms DESC);\
             CREATE TABLE IF NOT EXISTS alert_rules (\
               rule_id TEXT PRIMARY KEY,\
               condition TEXT NOT NULL UNIQUE,\
               enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),\
               severity TEXT NOT NULL CHECK (severity IN ('critical','warning','info')),\
               channel_ids_json TEXT NOT NULL,\
               dedupe_window_seconds INTEGER NOT NULL CHECK (dedupe_window_seconds BETWEEN 0 AND 86400),\
               re_alert_after_seconds INTEGER NOT NULL CHECK (re_alert_after_seconds BETWEEN 0 AND 604800),\
               scope_channel_id TEXT NOT NULL DEFAULT '',\
               notify_on_resolve INTEGER NOT NULL CHECK (notify_on_resolve IN (0,1)),\
               updated_at_unix_ms INTEGER NOT NULL CHECK (updated_at_unix_ms >= 0),\
               updated_by TEXT NOT NULL\
             );\
             CREATE TABLE IF NOT EXISTS system_resource_samples (\
               sample_id INTEGER PRIMARY KEY AUTOINCREMENT,\
               sampled_at_unix_ms INTEGER NOT NULL CHECK (sampled_at_unix_ms >= 0),\
               cpu_milli_percent INTEGER NOT NULL DEFAULT -1,\
               ram_used_milli_gb INTEGER NOT NULL DEFAULT -1,\
               ram_total_milli_gb INTEGER NOT NULL DEFAULT -1,\
               gpu_milli_percent INTEGER NOT NULL DEFAULT -1,\
               gpu_vram_used_milli_gb INTEGER NOT NULL DEFAULT -1,\
               media_volume_free_milli_gb INTEGER NOT NULL DEFAULT -1,\
               backup_volume_free_milli_gb INTEGER NOT NULL DEFAULT -1,\
               database_reachable INTEGER NOT NULL CHECK (database_reachable IN (0,1)),\
               backup_volume_writable INTEGER NOT NULL CHECK (backup_volume_writable IN (0,1)),\
               service_running INTEGER NOT NULL CHECK (service_running IN (0,1)),\
               clock_skew_milliseconds INTEGER NOT NULL DEFAULT -9223372036854775808\
             );\
             CREATE INDEX IF NOT EXISTS system_resource_samples_time \
               ON system_resource_samples(sampled_at_unix_ms DESC, sample_id DESC);\
             CREATE TABLE IF NOT EXISTS self_test_schedules (\
               kind TEXT PRIMARY KEY CHECK (kind IN ('daily','weekly')),\
               enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),\
               local_hour INTEGER NOT NULL CHECK (local_hour BETWEEN 0 AND 23),\
               local_minute INTEGER NOT NULL CHECK (local_minute BETWEEN 0 AND 59),\
               weekday INTEGER NOT NULL DEFAULT -1 CHECK (weekday BETWEEN -1 AND 6),\
               updated_at_unix_ms INTEGER NOT NULL CHECK (updated_at_unix_ms >= 0),\
               updated_by TEXT NOT NULL\
             );\
             CREATE TABLE IF NOT EXISTS system_self_tests (\
               self_test_id TEXT PRIMARY KEY,\
               kind TEXT NOT NULL CHECK (kind IN ('daily','weekly')),\
               trigger_kind TEXT NOT NULL CHECK (trigger_kind IN ('scheduled','manual')),\
               schedule_period_key TEXT NOT NULL DEFAULT '',\
               started_at_unix_ms INTEGER NOT NULL CHECK (started_at_unix_ms >= 0),\
               finished_at_unix_ms INTEGER NOT NULL CHECK (finished_at_unix_ms >= started_at_unix_ms),\
               status TEXT NOT NULL CHECK (status IN ('pass','warn','fail')),\
               checks_json TEXT NOT NULL,\
               summary TEXT NOT NULL,\
               evidence_path TEXT NOT NULL DEFAULT ''\
             );\
             CREATE UNIQUE INDEX IF NOT EXISTS system_self_tests_one_scheduled_period \
               ON system_self_tests(kind, schedule_period_key) \
               WHERE trigger_kind='scheduled';\
             CREATE INDEX IF NOT EXISTS system_self_tests_time \
               ON system_self_tests(started_at_unix_ms DESC, self_test_id DESC);\
             CREATE TABLE IF NOT EXISTS update_check_status (\
               singleton INTEGER PRIMARY KEY CHECK (singleton = 1),\
               checked_at_unix_ms INTEGER NOT NULL CHECK (checked_at_unix_ms >= 0),\
               next_automatic_check_at_unix_ms INTEGER NOT NULL \
                 CHECK (next_automatic_check_at_unix_ms > checked_at_unix_ms),\
               trigger_kind TEXT NOT NULL CHECK (trigger_kind IN ('automatic','manual')),\
               outcome TEXT NOT NULL CHECK (outcome IN ('up_to_date','update_available','updater_too_old','failed')),\
               record_json TEXT NOT NULL\
             );\
             CREATE TABLE IF NOT EXISTS update_stage_status (\
               singleton INTEGER PRIMARY KEY CHECK (singleton = 1),\
               staged_at_unix_ms INTEGER NOT NULL CHECK (staged_at_unix_ms >= 0),\
               version TEXT NOT NULL,\
               source_commit TEXT NOT NULL,\
               record_json TEXT NOT NULL\
             );\
             CREATE TABLE IF NOT EXISTS update_prepare_status (\
               singleton INTEGER PRIMARY KEY CHECK (singleton = 1),\
               prepared_at_unix_ms INTEGER NOT NULL CHECK (prepared_at_unix_ms >= 0),\
               version TEXT NOT NULL,\
               source_commit TEXT NOT NULL,\
               record_json TEXT NOT NULL\
             );\
             CREATE TABLE IF NOT EXISTS alert_channels (\
               channel_id TEXT PRIMARY KEY,\
               kind TEXT NOT NULL CHECK (kind IN ('email','sms','webhook')),\
               label TEXT NOT NULL,\
               enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),\
               target_redacted TEXT NOT NULL,\
               credential_configured INTEGER NOT NULL CHECK (credential_configured IN (0,1)),\
               quiet_hours_start_utc TEXT NOT NULL DEFAULT '',\
               quiet_hours_end_utc TEXT NOT NULL DEFAULT '',\
               encrypted_configuration BLOB NOT NULL,\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               updated_at_unix_ms INTEGER NOT NULL CHECK (updated_at_unix_ms >= created_at_unix_ms)\
             );\
             CREATE TABLE IF NOT EXISTS alert_deliveries (\
               delivery_id TEXT PRIMARY KEY,\
               event_id TEXT NOT NULL,\
               alert_channel_id TEXT NOT NULL,\
               notification_kind TEXT NOT NULL CHECK (notification_kind IN ('firing','realert','resolved','test')),\
               state TEXT NOT NULL CHECK (state IN ('pending','leased','sent','dead_letter')),\
               attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),\
               next_attempt_at_unix_ms INTEGER NOT NULL CHECK (next_attempt_at_unix_ms >= 0),\
               lease_expires_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (lease_expires_at_unix_ms >= -1),\
               last_error TEXT NOT NULL DEFAULT '',\
               signature TEXT NOT NULL DEFAULT '',\
               payload_json TEXT NOT NULL,\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               updated_at_unix_ms INTEGER NOT NULL CHECK (updated_at_unix_ms >= created_at_unix_ms),\
               sent_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (sent_at_unix_ms >= -1),\
               FOREIGN KEY(event_id) REFERENCES alert_events(event_id),\
               FOREIGN KEY(alert_channel_id) REFERENCES alert_channels(channel_id)\
             );\
             CREATE UNIQUE INDEX IF NOT EXISTS alert_deliveries_one_initial_kind \
               ON alert_deliveries(event_id, alert_channel_id, notification_kind) \
               WHERE notification_kind IN ('firing','resolved','test');\
             CREATE INDEX IF NOT EXISTS alert_deliveries_due \
               ON alert_deliveries(state, next_attempt_at_unix_ms, created_at_unix_ms);\
             CREATE TABLE IF NOT EXISTS schedule_items (\
               item_id TEXT PRIMARY KEY,\
               channel_id TEXT NOT NULL,\
               asset_id TEXT NOT NULL,\
               title TEXT NOT NULL,\
               starts_at_unix_ms INTEGER NOT NULL CHECK (starts_at_unix_ms >= 0),\
               duration_ms INTEGER NOT NULL CHECK (duration_ms > 0),\
               state TEXT NOT NULL CHECK (state IN ('draft','committed','cancelled'))\
             );\
             CREATE INDEX IF NOT EXISTS schedule_items_channel_time \
               ON schedule_items(channel_id, starts_at_unix_ms);\
             CREATE TABLE IF NOT EXISTS commit_reports (\
               report_id TEXT PRIMARY KEY,\
               plan_id TEXT NOT NULL,\
               channel_id TEXT NOT NULL,\
               schedule_item_id TEXT NOT NULL,\
               asset_id TEXT NOT NULL,\
               approved_by TEXT NOT NULL,\
               approved_at_unix_ms INTEGER NOT NULL,\
               dispatch_status TEXT NOT NULL CHECK (dispatch_status IN ('pending','queued','acknowledged','completed','error','cancelled')),\
               operator_notes TEXT NOT NULL\
             );\
             CREATE INDEX IF NOT EXISTS commit_reports_channel_time \
               ON commit_reports(channel_id, approved_at_unix_ms);",
        )?;
        store.migrate_ingest_retry_schedule()?;
        store.migrate_ingest_transcode_status()?;
        store.migrate_commit_report_statuses()?;
        store.migrate_alert_delivery_realerts()?;
        store.migrate_channel_output_profile()?;
        store.migrate_channel_output_sink_kinds()?;
        store.migrate_channel_output_sinks()?;
        store.initialize_cg_schema()?;
        store.migrate_cg_feed_cache_schema()?;
        store.execute(
            "CREATE TABLE IF NOT EXISTS srt_output_secrets (\
               channel_id TEXT NOT NULL,\
               sink_id TEXT NOT NULL,\
               encrypted_passphrase BLOB NOT NULL,\
               PRIMARY KEY(channel_id, sink_id),\
               FOREIGN KEY(channel_id, sink_id) REFERENCES channel_output_sinks(channel_id, sink_id) ON DELETE CASCADE\
             );",
        )?;
        store.seed_default_alert_rules()?;
        store.seed_default_self_test_schedules()?;
        Ok(store)
    }

    pub fn write_profile(
        &self,
        profile: &StationProfile,
        expected_revision: u64,
    ) -> Result<StationProfileDocument, ProfileWriteError> {
        self.execute("BEGIN IMMEDIATE")
            .map_err(ProfileWriteError::Storage)?;
        let outcome = (|| {
            let current = self.read_profile().map_err(ProfileWriteError::Storage)?;
            let current_revision = current.as_ref().map_or(0, |document| document.revision);
            if current_revision != expected_revision {
                return Err(ProfileWriteError::Conflict(current_revision));
            }
            self.upsert_profile(profile)
                .map_err(ProfileWriteError::Storage)?;
            Ok(StationProfileDocument {
                profile: profile.clone(),
                revision: current_revision + 1,
            })
        })();

        match outcome {
            Ok(document) => match self.execute("COMMIT") {
                Ok(()) => Ok(document),
                Err(message) => {
                    let _ = self.execute("ROLLBACK");
                    Err(ProfileWriteError::Storage(message))
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn read_profile(&self) -> Result<Option<StationProfileDocument>, String> {
        let statement = self.prepare(
            "SELECT station_id, display_name, timezone, revision FROM station_profile WHERE singleton=1",
        )?;
        // SAFETY: the prepared statement is live.
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(StationProfileDocument {
                profile: StationProfile {
                    station_id: column_text(statement.raw, 0)?,
                    display_name: column_text(statement.raw, 1)?,
                    timezone: column_text(statement.raw, 2)?,
                },
                revision: u64::try_from(unsafe { sqlite3_column_int64(statement.raw, 3) })
                    .map_err(|_| "database returned an invalid profile revision".to_string())?,
            })),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn put_media_asset(&self, asset: &MediaAsset) -> Result<(), String> {
        asset.validate().map_err(|error| format!("{error:?}"))?;
        let statement = self.prepare(
            "INSERT INTO media_assets(asset_id, media_path, duration_ms, readiness) VALUES(?1, ?2, ?3, ?4) \
             ON CONFLICT(asset_id) DO UPDATE SET media_path=excluded.media_path, duration_ms=excluded.duration_ms, readiness=excluded.readiness",
        )?;
        let asset_id = c_string(&asset.asset_id, "asset identity")?;
        let media_path = c_string(&asset.media_path, "media path")?;
        let readiness = c_string(readiness_name(asset.readiness), "asset readiness")?;
        bind_text(self.database, statement.raw, 1, &asset_id)?;
        bind_text(self.database, statement.raw, 2, &media_path)?;
        bind_i64(
            self.database,
            statement.raw,
            3,
            checked_i64(asset.duration_ms, "asset duration")?,
        )?;
        bind_text(self.database, statement.raw, 4, &readiness)?;
        expect_done(self.database, statement.raw)
    }

    pub fn create_ingest_job(&self, job: &MediaIngestJob) -> Result<bool, String> {
        job.validate().map_err(|error| format!("{error:?}"))?;
        if job.status != IngestJobStatus::Pending
            || job.progress_percent != 0
            || job.started_at_unix_ms.is_some()
            || job.completed_at_unix_ms.is_some()
            || job.error_detail.is_some()
            || job.asset_id.is_some()
            || job.attempts != 0
            || job.next_attempt_at_unix_ms.is_some()
        {
            return Err("new ingest jobs must begin in the pending state".into());
        }
        let statement = self.prepare(
            "INSERT OR IGNORE INTO media_ingest_jobs(job_id, source_kind, source_path, status, progress_percent, \
             created_at_unix_ms, started_at_unix_ms, completed_at_unix_ms, error_detail, asset_id, attempts, next_attempt_at_unix_ms) \
             VALUES(?1, ?2, ?3, 'pending', 0, ?4, -1, -1, '', '', 0, -1)",
        )?;
        let job_id = c_string(&job.job_id, "ingest job identity")?;
        let source_kind = c_string(
            ingest_source_kind_name(job.source_kind),
            "ingest source kind",
        )?;
        let source_path = c_string(&job.source_path, "ingest source path")?;
        bind_text(self.database, statement.raw, 1, &job_id)?;
        bind_text(self.database, statement.raw, 2, &source_kind)?;
        bind_text(self.database, statement.raw, 3, &source_path)?;
        bind_i64(self.database, statement.raw, 4, job.created_at_unix_ms)?;
        expect_done(self.database, statement.raw)?;
        Ok(self.changes() == 1)
    }

    pub fn read_ingest_job(&self, job_id: &str) -> Result<Option<MediaIngestJob>, String> {
        let statement = self.prepare(
             "SELECT job_id, source_kind, source_path, status, progress_percent, created_at_unix_ms, \
             started_at_unix_ms, completed_at_unix_ms, error_detail, asset_id, attempts, next_attempt_at_unix_ms \
             FROM media_ingest_jobs WHERE job_id=?1",
        )?;
        let job_id = c_string(job_id, "ingest job identity")?;
        bind_text(self.database, statement.raw, 1, &job_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_ingest_job_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn recover_interrupted_ingest_jobs(&self) -> Result<u64, String> {
        let statement = self.prepare(
            "UPDATE media_ingest_jobs SET status='pending', progress_percent=0, started_at_unix_ms=-1, \
             error_detail='', next_attempt_at_unix_ms=-1 WHERE status='running'",
        )?;
        expect_done(self.database, statement.raw)?;
        u64::try_from(self.changes()).map_err(|_| "SQLite returned invalid changes".into())
    }

    pub fn claim_next_ingest_job(
        &self,
        started_at_unix_ms: i64,
    ) -> Result<Option<MediaIngestJob>, String> {
        if started_at_unix_ms < 0 {
            return Err("ingest start time must be nonnegative".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let statement = self.prepare(
                "SELECT job_id FROM media_ingest_jobs WHERE status='pending' \
                 AND (next_attempt_at_unix_ms=-1 OR next_attempt_at_unix_ms<=?1) \
                 ORDER BY created_at_unix_ms, job_id LIMIT 1",
            )?;
            bind_i64(self.database, statement.raw, 1, started_at_unix_ms)?;
            let job_id = match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => column_text(statement.raw, 0)?,
                SQLITE_DONE => return Ok(None),
                _ => return Err(sqlite_error(self.database)),
            };
            drop(statement);
            let update = self.prepare(
                "UPDATE media_ingest_jobs SET status='running', progress_percent=10, started_at_unix_ms=?1, \
                 completed_at_unix_ms=-1, error_detail='', asset_id='', attempts=attempts+1, next_attempt_at_unix_ms=-1 \
                 WHERE job_id=?2 AND status='pending'",
            )?;
            let id = c_string(&job_id, "ingest job identity")?;
            bind_i64(self.database, update.raw, 1, started_at_unix_ms)?;
            bind_text(self.database, update.raw, 2, &id)?;
            expect_done(self.database, update.raw)?;
            if self.changes() != 1 {
                return Err("ingest job was claimed concurrently".into());
            }
            self.read_ingest_job(&job_id)?
                .ok_or_else(|| "claimed ingest job disappeared".into())
                .map(Some)
        })();
        match outcome {
            Ok(job) => match self.execute("COMMIT") {
                Ok(()) => Ok(job),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(error)
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn complete_ingest_job(
        &self,
        job_id: &str,
        asset: &MediaAsset,
        loudness: &AssetLoudnessAnalysis,
        completed_at_unix_ms: i64,
    ) -> Result<(), String> {
        if completed_at_unix_ms < 0 {
            return Err("ingest completion time must be nonnegative".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            self.put_media_asset(asset)?;
            self.put_asset_loudness(loudness)?;
            let statement = self.prepare(
                "UPDATE media_ingest_jobs SET status='completed', progress_percent=100, completed_at_unix_ms=?1, \
                 error_detail='', asset_id=?2 WHERE job_id=?3 AND status='running'",
            )?;
            let asset_id = c_string(&asset.asset_id, "asset identity")?;
            let job_id = c_string(job_id, "ingest job identity")?;
            bind_i64(self.database, statement.raw, 1, completed_at_unix_ms)?;
            bind_text(self.database, statement.raw, 2, &asset_id)?;
            bind_text(self.database, statement.raw, 3, &job_id)?;
            expect_done(self.database, statement.raw)?;
            if self.changes() == 1 {
                Ok(())
            } else {
                Err("ingest job is not running".into())
            }
        })();
        self.finish_transaction(outcome)
    }

    pub fn fail_ingest_job(
        &self,
        job_id: &str,
        completed_at_unix_ms: i64,
        error_detail: &str,
    ) -> Result<(), String> {
        if completed_at_unix_ms < 0
            || error_detail.trim().is_empty()
            || error_detail.len() > 4_000
            || error_detail.chars().any(char::is_control)
        {
            return Err("ingest failure time or detail is invalid".into());
        }
        let statement = self.prepare(
            "UPDATE media_ingest_jobs SET status='failed', completed_at_unix_ms=?1, error_detail=?2 \
             WHERE job_id=?3 AND status='running'",
        )?;
        let detail = c_string(error_detail, "ingest error detail")?;
        let job_id = c_string(job_id, "ingest job identity")?;
        bind_i64(self.database, statement.raw, 1, completed_at_unix_ms)?;
        bind_text(self.database, statement.raw, 2, &detail)?;
        bind_text(self.database, statement.raw, 3, &job_id)?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("ingest job is not running".into())
        }
    }

    pub fn retry_ingest_job(
        &self,
        job_id: &str,
        next_attempt_at_unix_ms: i64,
        error_detail: &str,
    ) -> Result<(), String> {
        if next_attempt_at_unix_ms < 0
            || error_detail.trim().is_empty()
            || error_detail.len() > 4_000
            || error_detail.chars().any(char::is_control)
        {
            return Err("ingest retry schedule is invalid".into());
        }
        let statement = self.prepare(
            "UPDATE media_ingest_jobs SET status='pending', progress_percent=0, started_at_unix_ms=-1, \
             completed_at_unix_ms=-1, error_detail=?1, asset_id='', next_attempt_at_unix_ms=?2 \
             WHERE job_id=?3 AND status='running'",
        )?;
        let detail = c_string(error_detail, "ingest retry detail")?;
        let job_id = c_string(job_id, "ingest job identity")?;
        bind_text(self.database, statement.raw, 1, &detail)?;
        bind_i64(self.database, statement.raw, 2, next_attempt_at_unix_ms)?;
        bind_text(self.database, statement.raw, 3, &job_id)?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("ingest job is not running".into())
        }
    }

    pub fn create_transcode_job(&self, job: &TranscodeJob) -> Result<bool, String> {
        job.validate().map_err(|error| format!("{error:?}"))?;
        if job.status != TranscodeJobStatus::Pending
            || job.progress_percent != 0
            || job.started_at_unix_ms.is_some()
            || job.completed_at_unix_ms.is_some()
            || job.output_asset_id.is_some()
            || job.error_detail.is_some()
            || job.attempts != 0
            || job.next_attempt_at_unix_ms.is_some()
        {
            return Err("new transcode jobs must begin in the pending state".into());
        }
        let statement = self.prepare(
            "INSERT OR IGNORE INTO transcode_jobs(job_id, ingest_job_id, source_asset_id, source_path, \
             output_format, status, progress_percent, created_at_unix_ms, started_at_unix_ms, \
             completed_at_unix_ms, output_asset_id, error_detail, attempts, next_attempt_at_unix_ms) \
             VALUES(?1, ?2, ?3, ?4, ?5, 'pending', 0, ?6, -1, -1, '', '', 0, -1)",
        )?;
        let values = [
            c_string(&job.job_id, "transcode job identity")?,
            c_string(&job.ingest_job_id, "transcode ingest identity")?,
            c_string(&job.source_asset_id, "transcode source identity")?,
            c_string(&job.source_path, "transcode source path")?,
            c_string(transcode_format_name(job.output_format), "transcode format")?,
        ];
        for (offset, value) in values.iter().enumerate() {
            bind_text(self.database, statement.raw, (offset + 1) as c_int, value)?;
        }
        bind_i64(self.database, statement.raw, 6, job.created_at_unix_ms)?;
        expect_done(self.database, statement.raw)?;
        Ok(self.changes() == 1)
    }

    pub fn read_transcode_job(&self, job_id: &str) -> Result<Option<TranscodeJob>, String> {
        let statement = self.prepare(
            "SELECT job_id, ingest_job_id, source_asset_id, source_path, output_format, status, \
             progress_percent, created_at_unix_ms, started_at_unix_ms, completed_at_unix_ms, \
             output_asset_id, error_detail, attempts, next_attempt_at_unix_ms \
             FROM transcode_jobs WHERE job_id=?1",
        )?;
        let job_id = c_string(job_id, "transcode job identity")?;
        bind_text(self.database, statement.raw, 1, &job_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_transcode_job_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_transcode_jobs(&self) -> Result<Vec<TranscodeJob>, String> {
        let statement = self.prepare(
            "SELECT job_id, ingest_job_id, source_asset_id, source_path, output_format, status, \
             progress_percent, created_at_unix_ms, started_at_unix_ms, completed_at_unix_ms, \
             output_asset_id, error_detail, attempts, next_attempt_at_unix_ms \
             FROM transcode_jobs ORDER BY created_at_unix_ms DESC, job_id",
        )?;
        let mut jobs = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => jobs.push(read_transcode_job_row(statement.raw)?),
                SQLITE_DONE => return Ok(jobs),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn recover_interrupted_transcode_jobs(&self) -> Result<u64, String> {
        let statement = self.prepare(
            "UPDATE transcode_jobs SET status='pending', progress_percent=0, started_at_unix_ms=-1, \
             completed_at_unix_ms=-1, output_asset_id='', error_detail='interrupted before completion', \
             next_attempt_at_unix_ms=-1 WHERE status='running'",
        )?;
        expect_done(self.database, statement.raw)?;
        u64::try_from(self.changes()).map_err(|_| "SQLite returned invalid changes".into())
    }

    pub fn claim_next_transcode_job(
        &self,
        started_at_unix_ms: i64,
    ) -> Result<Option<TranscodeJob>, String> {
        if started_at_unix_ms < 0 {
            return Err("transcode start time must be nonnegative".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let statement = self.prepare(
                "SELECT job_id FROM transcode_jobs WHERE status='pending' \
                 AND (next_attempt_at_unix_ms=-1 OR next_attempt_at_unix_ms<=?1) \
                 ORDER BY created_at_unix_ms, job_id LIMIT 1",
            )?;
            bind_i64(self.database, statement.raw, 1, started_at_unix_ms)?;
            let job_id = match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => column_text(statement.raw, 0)?,
                SQLITE_DONE => return Ok(None),
                _ => return Err(sqlite_error(self.database)),
            };
            drop(statement);
            let update = self.prepare(
                "UPDATE transcode_jobs SET status='running', progress_percent=5, started_at_unix_ms=?1, \
                 completed_at_unix_ms=-1, output_asset_id='', error_detail='', attempts=attempts+1, \
                 next_attempt_at_unix_ms=-1 WHERE job_id=?2 AND status='pending'",
            )?;
            let id = c_string(&job_id, "transcode job identity")?;
            bind_i64(self.database, update.raw, 1, started_at_unix_ms)?;
            bind_text(self.database, update.raw, 2, &id)?;
            expect_done(self.database, update.raw)?;
            if self.changes() != 1 {
                return Err("transcode job was claimed concurrently".into());
            }
            self.read_transcode_job(&job_id)?
                .ok_or_else(|| "claimed transcode job disappeared".into())
                .map(Some)
        })();
        match outcome {
            Ok(job) => match self.execute("COMMIT") {
                Ok(()) => Ok(job),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(error)
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn update_transcode_progress(
        &self,
        job_id: &str,
        progress_percent: u8,
    ) -> Result<(), String> {
        if !(1..100).contains(&progress_percent) {
            return Err("running transcode progress must be between 1 and 99".into());
        }
        let statement = self.prepare(
            "UPDATE transcode_jobs SET progress_percent=?1 WHERE job_id=?2 AND status='running' \
             AND progress_percent<?1",
        )?;
        let job_id = c_string(job_id, "transcode job identity")?;
        bind_i64(self.database, statement.raw, 1, i64::from(progress_percent))?;
        bind_text(self.database, statement.raw, 2, &job_id)?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("transcode job is not running or progress did not advance".into())
        }
    }

    pub fn complete_transcode_job(
        &self,
        job_id: &str,
        output_asset_id: &str,
        completed_at_unix_ms: i64,
    ) -> Result<(), String> {
        if completed_at_unix_ms < 0 || !is_sha256_identity(output_asset_id) {
            return Err("transcode completion time or output identity is invalid".into());
        }
        let statement = self.prepare(
            "UPDATE transcode_jobs SET status='completed', progress_percent=100, \
             completed_at_unix_ms=?1, output_asset_id=?2, error_detail='', next_attempt_at_unix_ms=-1 \
             WHERE job_id=?3 AND status='running' AND started_at_unix_ms<=?1",
        )?;
        let output_asset_id = c_string(output_asset_id, "transcode output identity")?;
        let job_id = c_string(job_id, "transcode job identity")?;
        bind_i64(self.database, statement.raw, 1, completed_at_unix_ms)?;
        bind_text(self.database, statement.raw, 2, &output_asset_id)?;
        bind_text(self.database, statement.raw, 3, &job_id)?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("transcode job is not running or completion predates its start".into())
        }
    }

    pub fn fail_transcode_job(
        &self,
        job_id: &str,
        completed_at_unix_ms: i64,
        error_detail: &str,
    ) -> Result<(), String> {
        if completed_at_unix_ms < 0
            || error_detail.trim().is_empty()
            || error_detail.len() > 4_000
            || error_detail.chars().any(char::is_control)
        {
            return Err("transcode failure time or detail is invalid".into());
        }
        let statement = self.prepare(
            "UPDATE transcode_jobs SET status='failed', completed_at_unix_ms=?1, error_detail=?2, \
             output_asset_id='', next_attempt_at_unix_ms=-1 WHERE job_id=?3 AND status='running' \
             AND started_at_unix_ms<=?1",
        )?;
        let detail = c_string(error_detail, "transcode error detail")?;
        let job_id = c_string(job_id, "transcode job identity")?;
        bind_i64(self.database, statement.raw, 1, completed_at_unix_ms)?;
        bind_text(self.database, statement.raw, 2, &detail)?;
        bind_text(self.database, statement.raw, 3, &job_id)?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("transcode job is not running or failure predates its start".into())
        }
    }

    pub fn retry_transcode_job(
        &self,
        job_id: &str,
        next_attempt_at_unix_ms: i64,
        error_detail: &str,
    ) -> Result<(), String> {
        if next_attempt_at_unix_ms < 0
            || error_detail.trim().is_empty()
            || error_detail.len() > 4_000
            || error_detail.chars().any(char::is_control)
        {
            return Err("transcode retry schedule is invalid".into());
        }
        let statement = self.prepare(
            "UPDATE transcode_jobs SET status='pending', progress_percent=0, started_at_unix_ms=-1, \
             completed_at_unix_ms=-1, output_asset_id='', error_detail=?1, next_attempt_at_unix_ms=?2 \
             WHERE job_id=?3 AND status='running'",
        )?;
        let detail = c_string(error_detail, "transcode retry detail")?;
        let job_id = c_string(job_id, "transcode job identity")?;
        bind_text(self.database, statement.raw, 1, &detail)?;
        bind_i64(self.database, statement.raw, 2, next_attempt_at_unix_ms)?;
        bind_text(self.database, statement.raw, 3, &job_id)?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("transcode job is not running".into())
        }
    }

    pub fn handoff_ingest_to_transcode(&self, job: &TranscodeJob) -> Result<(), String> {
        job.validate().map_err(|error| format!("{error:?}"))?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            if !self.create_transcode_job(job)? {
                return Err("transcode job or ingest handoff already exists".into());
            }
            let statement = self.prepare(
                "UPDATE media_ingest_jobs SET status='awaiting_transcode', progress_percent=60, \
                 completed_at_unix_ms=-1, error_detail='', asset_id='', next_attempt_at_unix_ms=-1 \
                 WHERE job_id=?1 AND status='running'",
            )?;
            let ingest_job_id = c_string(&job.ingest_job_id, "transcode ingest identity")?;
            bind_text(self.database, statement.raw, 1, &ingest_job_id)?;
            expect_done(self.database, statement.raw)?;
            if self.changes() == 1 {
                Ok(())
            } else {
                Err("ingest job is not running".into())
            }
        })();
        self.finish_transaction(outcome)
    }

    pub fn complete_transcode_and_ingest(
        &self,
        transcode_job_id: &str,
        output: &MediaAsset,
        loudness: &AssetLoudnessAnalysis,
        completed_at_unix_ms: i64,
    ) -> Result<(), String> {
        output.validate().map_err(|error| format!("{error:?}"))?;
        loudness.validate().map_err(|error| format!("{error:?}"))?;
        if output.readiness != AssetReadiness::Ready
            || loudness.asset_id != output.asset_id
            || loudness.status != LoudnessGateStatus::Ok
        {
            return Err(
                "transcode output and passing loudness identities must match and be ready".into(),
            );
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let job = self
                .read_transcode_job(transcode_job_id)?
                .ok_or_else(|| "transcode job does not exist".to_string())?;
            if job.status != TranscodeJobStatus::Running {
                return Err("transcode job is not running".into());
            }
            self.put_media_asset(output)?;
            self.put_asset_loudness(loudness)?;
            self.complete_transcode_job(transcode_job_id, &output.asset_id, completed_at_unix_ms)?;
            let statement = self.prepare(
                "UPDATE media_ingest_jobs SET status='completed', progress_percent=100, \
                 completed_at_unix_ms=?1, error_detail='', asset_id=?2, next_attempt_at_unix_ms=-1 \
                 WHERE job_id=?3 AND status='awaiting_transcode' AND started_at_unix_ms<=?1",
            )?;
            let output_asset_id = c_string(&output.asset_id, "transcode output identity")?;
            let ingest_job_id = c_string(&job.ingest_job_id, "transcode ingest identity")?;
            bind_i64(self.database, statement.raw, 1, completed_at_unix_ms)?;
            bind_text(self.database, statement.raw, 2, &output_asset_id)?;
            bind_text(self.database, statement.raw, 3, &ingest_job_id)?;
            expect_done(self.database, statement.raw)?;
            if self.changes() == 1 {
                Ok(())
            } else {
                Err("ingest job is not awaiting this transcode".into())
            }
        })();
        self.finish_transaction(outcome)
    }

    pub fn fail_transcode_and_ingest(
        &self,
        transcode_job_id: &str,
        completed_at_unix_ms: i64,
        error_detail: &str,
    ) -> Result<(), String> {
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let job = self
                .read_transcode_job(transcode_job_id)?
                .ok_or_else(|| "transcode job does not exist".to_string())?;
            self.fail_transcode_job(transcode_job_id, completed_at_unix_ms, error_detail)?;
            let statement = self.prepare(
                "UPDATE media_ingest_jobs SET status='failed', completed_at_unix_ms=?1, \
                 error_detail=?2, asset_id='', next_attempt_at_unix_ms=-1 \
                 WHERE job_id=?3 AND status='awaiting_transcode' AND started_at_unix_ms<=?1",
            )?;
            let detail = c_string(error_detail, "transcode error detail")?;
            let ingest_job_id = c_string(&job.ingest_job_id, "transcode ingest identity")?;
            bind_i64(self.database, statement.raw, 1, completed_at_unix_ms)?;
            bind_text(self.database, statement.raw, 2, &detail)?;
            bind_text(self.database, statement.raw, 3, &ingest_job_id)?;
            expect_done(self.database, statement.raw)?;
            if self.changes() == 1 {
                Ok(())
            } else {
                Err("ingest job is not awaiting this transcode".into())
            }
        })();
        self.finish_transaction(outcome)
    }

    pub fn put_channel(&self, channel: &ChannelConfiguration) -> Result<(), String> {
        self.put_channel_with_srt_passphrases(channel, &HashMap::new())
    }

    pub fn put_channel_with_srt_passphrases(
        &self,
        channel: &ChannelConfiguration,
        passphrases: &HashMap<String, String>,
    ) -> Result<(), String> {
        channel.validate().map_err(|error| format!("{error:?}"))?;
        for (sink_id, passphrase) in passphrases {
            let valid_sink = channel.output_sinks.iter().any(|sink| {
                sink.sink_id == *sink_id
                    && sink.kind == OutputSinkKind::Srt
                    && sink.encryption_configured
            });
            if !valid_sink {
                return Err("SRT passphrases must identify an encrypted SRT output sink".into());
            }
            validate_srt_passphrase(passphrase)?;
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = self.put_channel_transaction(channel, passphrases);
        self.finish_transaction(outcome)
    }

    fn put_channel_transaction(
        &self,
        channel: &ChannelConfiguration,
        passphrases: &HashMap<String, String>,
    ) -> Result<(), String> {
        let prior_passphrases = self.list_encrypted_srt_passphrases(&channel.channel_id)?;
        let statement = self.prepare(
            "INSERT INTO channels(channel_id, display_name, udp_destination, output_profile, enabled) VALUES(?1, ?2, ?3, ?4, ?5) \
             ON CONFLICT(channel_id) DO UPDATE SET display_name=excluded.display_name, udp_destination=excluded.udp_destination, output_profile=excluded.output_profile, enabled=excluded.enabled",
        )?;
        let channel_id = c_string(&channel.channel_id, "channel identity")?;
        let display_name = c_string(&channel.display_name, "channel display name")?;
        let destination = c_string(
            &channel.output_sinks[0].destination,
            "primary output destination",
        )?;
        let output_profile = c_string(
            channel.output_profile.storage_name(),
            "channel output profile",
        )?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        bind_text(self.database, statement.raw, 2, &display_name)?;
        bind_text(self.database, statement.raw, 3, &destination)?;
        bind_text(self.database, statement.raw, 4, &output_profile)?;
        bind_i64(self.database, statement.raw, 5, i64::from(channel.enabled))?;
        expect_done(self.database, statement.raw)?;
        drop(statement);
        let delete = self.prepare("DELETE FROM channel_output_sinks WHERE channel_id=?1")?;
        bind_text(self.database, delete.raw, 1, &channel_id)?;
        expect_done(self.database, delete.raw)?;
        drop(delete);
        for sink in &channel.output_sinks {
            let insert = self.prepare(
                "INSERT INTO channel_output_sinks(channel_id, sink_id, label, kind, destination, loudness_target_milli, required, enabled) \
                 VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            )?;
            let sink_id = c_string(&sink.sink_id, "output sink identity")?;
            let label = c_string(&sink.label, "output sink label")?;
            let kind = c_string(output_sink_kind_name(sink.kind), "output sink kind")?;
            let destination = c_string(&sink.destination, "output sink destination")?;
            bind_text(self.database, insert.raw, 1, &channel_id)?;
            bind_text(self.database, insert.raw, 2, &sink_id)?;
            bind_text(self.database, insert.raw, 3, &label)?;
            bind_text(self.database, insert.raw, 4, &kind)?;
            bind_text(self.database, insert.raw, 5, &destination)?;
            bind_i64(
                self.database,
                insert.raw,
                6,
                (sink.loudness_target_lufs * 1_000.0).round() as i64,
            )?;
            bind_i64(self.database, insert.raw, 7, i64::from(sink.required))?;
            bind_i64(self.database, insert.raw, 8, i64::from(sink.enabled))?;
            expect_done(self.database, insert.raw)?;
            drop(insert);
            if sink.kind == OutputSinkKind::Srt && sink.encryption_configured {
                let encrypted = if let Some(passphrase) = passphrases.get(&sink.sink_id) {
                    protect_local_machine(passphrase.as_bytes())?
                } else {
                    prior_passphrases
                        .get(&sink.sink_id)
                        .cloned()
                        .ok_or("an encrypted SRT output requires a write-only passphrase")?
                };
                let secret = self.prepare(
                    "INSERT INTO srt_output_secrets(channel_id, sink_id, encrypted_passphrase) \
                     VALUES(?1, ?2, ?3)",
                )?;
                bind_text(self.database, secret.raw, 1, &channel_id)?;
                bind_text(self.database, secret.raw, 2, &sink_id)?;
                bind_blob(self.database, secret.raw, 3, &encrypted)?;
                expect_done(self.database, secret.raw)?;
            }
        }
        Ok(())
    }

    fn list_encrypted_srt_passphrases(
        &self,
        channel_id: &str,
    ) -> Result<HashMap<String, Vec<u8>>, String> {
        let statement = self.prepare(
            "SELECT sink_id, encrypted_passphrase FROM srt_output_secrets WHERE channel_id=?1",
        )?;
        let channel_id = c_string(channel_id, "channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        let mut secrets = HashMap::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    secrets.insert(
                        column_text(statement.raw, 0)?,
                        column_blob(statement.raw, 1)?,
                    );
                }
                SQLITE_DONE => return Ok(secrets),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn list_channels(&self) -> Result<Vec<ChannelConfiguration>, String> {
        let statement = self.prepare(
            "SELECT channel_id, display_name, output_profile, enabled FROM channels ORDER BY channel_id",
        )?;
        let mut channels = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => channels.push(ChannelConfiguration {
                    channel_id: column_text(statement.raw, 0)?,
                    display_name: column_text(statement.raw, 1)?,
                    output_profile: OutputProfileId::from_storage_name(&column_text(
                        statement.raw,
                        2,
                    )?)
                    .map_err(|error| format!("{error:?}"))?,
                    output_sinks: self
                        .list_channel_output_sinks(&column_text(statement.raw, 0)?)?,
                    enabled: unsafe { sqlite3_column_int64(statement.raw, 3) } == 1,
                }),
                SQLITE_DONE => return Ok(channels),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn load_cg_snapshot_inputs(
        &self,
        channel_id: &str,
        generated_at_unix_ms: i64,
    ) -> Result<CgSnapshotInputs, CgSnapshotReadError> {
        if channel_id.trim().is_empty() {
            return Err(CgSnapshotReadError::InvalidChannel);
        }
        if generated_at_unix_ms < 0 {
            return Err(CgSnapshotReadError::InvalidTimestamp);
        }
        self.execute("BEGIN DEFERRED")
            .map_err(CgSnapshotReadError::Storage)?;
        let outcome = (|| {
            let channel_exists = self
                .list_channels()
                .map_err(CgSnapshotReadError::Storage)?
                .into_iter()
                .any(|channel| channel.channel_id == channel_id);
            if !channel_exists {
                return Err(CgSnapshotReadError::ChannelNotFound);
            }
            let board = self
                .read_cg_board(channel_id)
                .map_err(CgSnapshotReadError::Storage)?
                .ok_or(CgSnapshotReadError::BoardNotFound)?;
            if !board.active {
                return Err(CgSnapshotReadError::BoardInactive);
            }
            let template = built_in_template(&board.template_id).ok_or_else(|| {
                CgSnapshotReadError::InvalidData("CG board template is unavailable".into())
            })?;
            let zones = self
                .list_cg_zones(&board.board_id)
                .map_err(CgSnapshotReadError::Storage)?;
            let feed_sources = self
                .list_cg_feed_sources(channel_id)
                .map_err(CgSnapshotReadError::Storage)?;
            let mut feed_cache = Vec::new();
            let mut approvals = Vec::new();
            for feed in &feed_sources {
                if let Some(cache) = self
                    .read_cg_feed_cache(&feed.feed_source_id)
                    .map_err(CgSnapshotReadError::Storage)?
                {
                    feed_cache.push(cache);
                }
                approvals.extend(
                    self.list_cg_feed_item_approvals(&feed.feed_source_id)
                        .map_err(CgSnapshotReadError::Storage)?,
                );
            }
            let bulletins = self
                .list_airable_cg_bulletins(channel_id, generated_at_unix_ms)
                .map_err(CgSnapshotReadError::Storage)?;
            let referenced_image_ids = zones
                .iter()
                .filter_map(|zone| match &zone.content_source {
                    CgContentSource::Image { asset_id } => Some(asset_id.as_str()),
                    _ => None,
                })
                .collect::<BTreeSet<_>>();
            let image_assets = referenced_image_ids
                .iter()
                .map(|asset_id| {
                    self.read_cg_image_asset(asset_id)
                        .map_err(CgSnapshotReadError::Storage)
                })
                .collect::<Result<Vec<_>, _>>()?
                .into_iter()
                .flatten()
                .map(|asset| self.verify_cg_image_asset(asset))
                .collect();
            Ok(CgSnapshotInputs {
                board,
                template,
                zones,
                feed_sources,
                feed_cache,
                approvals,
                image_assets,
                bulletins,
                generated_at_unix_ms,
            })
        })();
        match outcome {
            Ok(inputs) => match self.execute("COMMIT") {
                Ok(()) => Ok(inputs),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(CgSnapshotReadError::Storage(error))
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn cg_image_verification_count(&self) -> u64 {
        self.cg_image_verification_count.load(Ordering::Relaxed)
    }

    fn verify_cg_image_asset(&self, mut asset: CgImageAsset) -> CgImageAsset {
        if asset.readiness != CgImageReadiness::Ready {
            return asset;
        }
        let image_root = match self.validated_cg_image_root() {
            Ok(root) => root,
            Err(_) => {
                asset.readiness = CgImageReadiness::Rejected;
                return asset;
            }
        };
        let path = match managed_cg_image_path(&image_root, &asset.asset_id, &asset.mime_type) {
            Ok(path) => path,
            Err(_) => {
                asset.readiness = CgImageReadiness::Rejected;
                return asset;
            }
        };
        let metadata = match fs::symlink_metadata(&path) {
            Ok(metadata)
                if metadata.is_file()
                    && metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT == 0 =>
            {
                metadata
            }
            _ => {
                self.cg_image_verification_cache
                    .lock()
                    .unwrap()
                    .remove(&asset.asset_id);
                asset.readiness = CgImageReadiness::Missing;
                return asset;
            }
        };
        let byte_len = metadata.len();
        let modified = metadata.modified().ok();
        if let Some(cached) = self
            .cg_image_verification_cache
            .lock()
            .unwrap()
            .get(&asset.asset_id)
            && cached.byte_len == byte_len
            && cached.modified == modified
            && cached.checked_at.elapsed() <= std::time::Duration::from_secs(60)
            && cached.authority == asset
        {
            return cached.asset.clone();
        }
        self.cg_image_verification_count
            .fetch_add(1, Ordering::Relaxed);
        let authority = asset.clone();
        let verified = match fs::read(&path)
            .map_err(|error| error.to_string())
            .and_then(|bytes| {
                inspect_cg_image(&asset.asset_id, &bytes).map_err(|error| error.to_string())
            }) {
            Ok(candidate) if candidate == asset => asset,
            _ => {
                asset.readiness = CgImageReadiness::Rejected;
                asset
            }
        };
        let mut cache = self.cg_image_verification_cache.lock().unwrap();
        if cache.len() >= CG_IMAGE_VERIFICATION_CACHE_LIMIT
            && !cache.contains_key(&verified.asset_id)
            && let Some(oldest) = cache
                .iter()
                .min_by_key(|(_, entry)| entry.checked_at)
                .map(|(asset_id, _)| asset_id.clone())
        {
            cache.remove(&oldest);
        }
        cache.insert(
            verified.asset_id.clone(),
            CachedCgImageVerification {
                byte_len,
                modified,
                checked_at: Instant::now(),
                authority,
                asset: verified.clone(),
            },
        );
        verified
    }

    fn validated_cg_image_root(&self) -> Result<PathBuf, String> {
        let current = fs::canonicalize(&self.cg_image_root)
            .map_err(|error| format!("canonicalize CG image root: {error}"))?;
        if current != self.cg_image_root {
            return Err("CG image root authority changed after station startup".into());
        }
        Ok(current)
    }

    pub fn write_cg_board(
        &self,
        board: &CgBoard,
        expected_revision: u64,
        audit: &CgAuditEvent,
    ) -> Result<CgBoard, CgWriteError> {
        board.validate().map_err(CgWriteError::Invalid)?;
        audit.validate().map_err(CgWriteError::Invalid)?;
        if audit.channel_id != board.channel_id
            || audit.board_id.as_deref() != Some(board.board_id.as_str())
            || audit.operator_id.trim().is_empty()
        {
            return Err(CgWriteError::Invalid(
                "CG board audit evidence does not identify the proposed board.",
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(CgWriteError::Storage)?;
        let outcome = (|| {
            let current = self
                .read_cg_board(&board.channel_id)
                .map_err(CgWriteError::Storage)?;
            let current_revision = current.as_ref().map_or(0, |value| value.revision);
            if current_revision != expected_revision {
                return Err(CgWriteError::Conflict(current_revision));
            }
            if board.revision != expected_revision.saturating_add(1) {
                return Err(CgWriteError::Invalid(
                    "CG board revision must be exactly one greater than the expected revision.",
                ));
            }
            match current {
                None if audit.event_kind != CgAuditEventKind::BoardCreated => {
                    return Err(CgWriteError::Invalid(
                        "A new CG board requires a board-created audit event.",
                    ));
                }
                Some(existing)
                    if audit.event_kind != CgAuditEventKind::BoardUpdated
                        || existing.board_id != board.board_id
                        || existing.created_at_unix_ms != board.created_at_unix_ms
                        || existing.created_by != board.created_by
                        || board.updated_at_unix_ms < existing.updated_at_unix_ms =>
                {
                    return Err(CgWriteError::Invalid(
                        "A CG board update cannot replace identity, provenance, or move time backward.",
                    ));
                }
                _ => {}
            }
            if let Some(asset_id) = &board.background_audio_asset_id {
                let asset = self
                    .read_media_asset(asset_id)
                    .map_err(CgWriteError::Storage)?
                    .ok_or(CgWriteError::NotFound)?;
                if asset.readiness != AssetReadiness::Ready {
                    return Err(CgWriteError::Invalid(
                        "CG background audio must identify an air-ready managed asset.",
                    ));
                }
            }
            let template = built_in_template(&board.template_id).ok_or(CgWriteError::Invalid(
                "CG board refers to an unavailable template.",
            ))?;
            for zone in self
                .list_cg_zones(&board.board_id)
                .map_err(CgWriteError::Storage)?
            {
                let Some(template_zone) = template
                    .zones
                    .iter()
                    .find(|candidate| candidate.template_zone_key == zone.template_zone_key)
                else {
                    return Err(CgWriteError::Invalid(
                        "The selected CG template does not contain an existing configured zone.",
                    ));
                };
                if template_zone.zone_kind != zone.zone_kind {
                    return Err(CgWriteError::Invalid(
                        "The selected CG template changes an existing zone kind.",
                    ));
                }
            }
            self.upsert_cg_board(board).map_err(CgWriteError::Storage)?;
            self.insert_cg_audit(audit).map_err(CgWriteError::Storage)?;
            Ok(board.clone())
        })();
        finish_cg_transaction(self, outcome)
    }

    pub fn read_cg_board(&self, channel_id: &str) -> Result<Option<CgBoard>, String> {
        let statement = self.prepare(
            "SELECT board_id, channel_id, template_id, active, background_audio_asset_id, revision, \
             created_at_unix_ms, updated_at_unix_ms, created_by FROM cg_boards WHERE channel_id=?1",
        )?;
        let channel_id = c_string(channel_id, "CG board channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_cg_board_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn write_cg_zone(
        &self,
        zone: &CgZoneConfig,
        expected_revision: u64,
        audit: &CgAuditEvent,
    ) -> Result<CgZoneConfig, CgWriteError> {
        zone.validate().map_err(CgWriteError::Invalid)?;
        audit.validate().map_err(CgWriteError::Invalid)?;
        if audit.board_id.as_deref() != Some(zone.board_id.as_str()) {
            return Err(CgWriteError::Invalid(
                "CG zone audit evidence does not identify the proposed board.",
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(CgWriteError::Storage)?;
        let outcome = (|| {
            let board = self
                .read_cg_board_by_id(&zone.board_id)
                .map_err(CgWriteError::Storage)?
                .ok_or(CgWriteError::NotFound)?;
            if audit.channel_id != board.channel_id {
                return Err(CgWriteError::Invalid(
                    "CG zone audit evidence identifies the wrong channel.",
                ));
            }
            let template = built_in_template(&board.template_id).ok_or(CgWriteError::Invalid(
                "CG board refers to an unavailable template.",
            ))?;
            let template_zone = template
                .zones
                .iter()
                .find(|candidate| candidate.template_zone_key == zone.template_zone_key)
                .ok_or(CgWriteError::Invalid(
                    "CG zone does not belong to the board template.",
                ))?;
            if template_zone.zone_kind != zone.zone_kind {
                return Err(CgWriteError::Invalid(
                    "CG zone kind does not match its template region.",
                ));
            }
            match &zone.content_source {
                CgContentSource::Feed { feed_source_id } => {
                    let feed = self
                        .read_cg_feed_source(feed_source_id)
                        .map_err(CgWriteError::Storage)?
                        .ok_or(CgWriteError::NotFound)?;
                    if feed.channel_id != board.channel_id || !feed.enabled {
                        return Err(CgWriteError::Invalid(
                            "CG zone feed must be enabled and belong to the board channel.",
                        ));
                    }
                }
                CgContentSource::Image { asset_id } => {
                    let asset = self
                        .read_cg_image_asset(asset_id)
                        .map_err(CgWriteError::Storage)?
                        .ok_or(CgWriteError::NotFound)?;
                    if asset.readiness != CgImageReadiness::Ready {
                        return Err(CgWriteError::Invalid(
                            "CG zone image must identify a ready managed asset.",
                        ));
                    }
                }
                _ => {}
            }
            let current = self
                .read_cg_zone(&zone.zone_id)
                .map_err(CgWriteError::Storage)?;
            let current_revision = current.as_ref().map_or(0, |value| value.revision);
            if current_revision != expected_revision {
                return Err(CgWriteError::Conflict(current_revision));
            }
            if zone.revision != expected_revision.saturating_add(1) {
                return Err(CgWriteError::Invalid(
                    "CG zone revision must be exactly one greater than the expected revision.",
                ));
            }
            match current {
                None if audit.event_kind != CgAuditEventKind::ZoneCreated => {
                    return Err(CgWriteError::Invalid(
                        "A new CG zone requires a zone-created audit event.",
                    ));
                }
                Some(existing)
                    if audit.event_kind != CgAuditEventKind::ZoneUpdated
                        || existing.board_id != zone.board_id
                        || existing.created_at_unix_ms != zone.created_at_unix_ms
                        || zone.updated_at_unix_ms < existing.updated_at_unix_ms =>
                {
                    return Err(CgWriteError::Invalid(
                        "A CG zone update cannot replace identity, provenance, or move time backward.",
                    ));
                }
                _ => {}
            }
            self.upsert_cg_zone(zone).map_err(CgWriteError::Storage)?;
            self.insert_cg_audit(audit).map_err(CgWriteError::Storage)?;
            Ok(zone.clone())
        })();
        finish_cg_transaction(self, outcome)
    }

    pub fn read_cg_zone(&self, zone_id: &str) -> Result<Option<CgZoneConfig>, String> {
        let statement = self.prepare(
            "SELECT zone_id, board_id, template_zone_key, zone_kind, content_source_json, \
             rotation_seconds, approval_required, allowed_tags_json, revision, created_at_unix_ms, \
             updated_at_unix_ms FROM cg_zone_configs WHERE zone_id=?1",
        )?;
        let zone_id = c_string(zone_id, "CG zone identity")?;
        bind_text(self.database, statement.raw, 1, &zone_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_cg_zone_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_cg_zones(&self, board_id: &str) -> Result<Vec<CgZoneConfig>, String> {
        let statement = self.prepare(
            "SELECT zone_id, board_id, template_zone_key, zone_kind, content_source_json, \
             rotation_seconds, approval_required, allowed_tags_json, revision, created_at_unix_ms, \
             updated_at_unix_ms FROM cg_zone_configs WHERE board_id=?1 \
             ORDER BY template_zone_key, zone_id",
        )?;
        let board_id = c_string(board_id, "CG board identity")?;
        bind_text(self.database, statement.raw, 1, &board_id)?;
        let mut zones = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => zones.push(read_cg_zone_row(statement.raw)?),
                SQLITE_DONE => return Ok(zones),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn delete_cg_zone(
        &self,
        zone_id: &str,
        expected_revision: u64,
        audit: &CgAuditEvent,
    ) -> Result<(), CgWriteError> {
        audit.validate().map_err(CgWriteError::Invalid)?;
        if audit.event_kind != CgAuditEventKind::ZoneRemoved {
            return Err(CgWriteError::Invalid(
                "CG zone deletion requires zone-removed audit evidence.",
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(CgWriteError::Storage)?;
        let outcome = (|| {
            let zone = self
                .read_cg_zone(zone_id)
                .map_err(CgWriteError::Storage)?
                .ok_or(CgWriteError::NotFound)?;
            if zone.revision != expected_revision {
                return Err(CgWriteError::Conflict(zone.revision));
            }
            let board = self
                .read_cg_board_by_id(&zone.board_id)
                .map_err(CgWriteError::Storage)?
                .ok_or(CgWriteError::NotFound)?;
            if audit.board_id.as_deref() != Some(zone.board_id.as_str())
                || audit.channel_id != board.channel_id
                || audit.occurred_at_unix_ms < zone.updated_at_unix_ms
            {
                return Err(CgWriteError::Invalid(
                    "CG zone deletion audit evidence does not match the current zone.",
                ));
            }
            let statement = self
                .prepare("DELETE FROM cg_zone_configs WHERE zone_id=?1 AND revision=?2")
                .map_err(CgWriteError::Storage)?;
            let zone_id = c_string(zone_id, "CG zone identity").map_err(CgWriteError::Storage)?;
            bind_text(self.database, statement.raw, 1, &zone_id).map_err(CgWriteError::Storage)?;
            bind_i64(
                self.database,
                statement.raw,
                2,
                checked_i64(expected_revision, "CG zone revision")
                    .map_err(CgWriteError::Storage)?,
            )
            .map_err(CgWriteError::Storage)?;
            expect_done(self.database, statement.raw).map_err(CgWriteError::Storage)?;
            drop(statement);
            if self.changes() != 1 {
                return Err(CgWriteError::Conflict(zone.revision));
            }
            self.insert_cg_audit(audit).map_err(CgWriteError::Storage)
        })();
        finish_cg_transaction(self, outcome)
    }

    pub fn list_cg_audit_events(
        &self,
        channel_id: &str,
        limit: u32,
    ) -> Result<Vec<CgAuditEvent>, String> {
        if !(1..=1_000).contains(&limit) {
            return Err("CG audit limit must be between 1 and 1000".into());
        }
        let statement = self.prepare(
            "SELECT audit_id, board_id, channel_id, event_kind, operator_id, occurred_at_unix_ms, \
             details_json FROM cg_audit_events WHERE channel_id=?1 \
             ORDER BY occurred_at_unix_ms DESC, audit_id DESC LIMIT ?2",
        )?;
        let channel_id = c_string(channel_id, "CG audit channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        bind_i64(self.database, statement.raw, 2, i64::from(limit))?;
        let mut events = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => events.push(read_cg_audit_row(statement.raw)?),
                SQLITE_DONE => return Ok(events),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn write_cg_feed_source(
        &self,
        feed: &CgFeedSource,
        expected_revision: u64,
        audit: &CgAuditEvent,
    ) -> Result<CgFeedSource, CgWriteError> {
        feed.validate().map_err(CgWriteError::Invalid)?;
        audit.validate().map_err(CgWriteError::Invalid)?;
        if audit.channel_id != feed.channel_id {
            return Err(CgWriteError::Invalid(
                "CG feed audit evidence identifies the wrong channel.",
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(CgWriteError::Storage)?;
        let outcome = (|| {
            let current = self
                .read_cg_feed_source(&feed.feed_source_id)
                .map_err(CgWriteError::Storage)?;
            let current_revision = current.as_ref().map_or(0, |value| value.revision);
            if current_revision != expected_revision {
                return Err(CgWriteError::Conflict(current_revision));
            }
            if feed.revision != expected_revision.saturating_add(1) {
                return Err(CgWriteError::Invalid(
                    "CG feed revision must be exactly one greater than the expected revision.",
                ));
            }
            match current {
                None if audit.event_kind != CgAuditEventKind::FeedCreated => {
                    return Err(CgWriteError::Invalid(
                        "A new CG feed requires a feed-created audit event.",
                    ));
                }
                Some(existing)
                    if !matches!(
                        audit.event_kind,
                        CgAuditEventKind::FeedUpdated | CgAuditEventKind::FeedRemoved
                    ) || (audit.event_kind == CgAuditEventKind::FeedRemoved && feed.enabled)
                        || existing.channel_id != feed.channel_id
                        || existing.created_at_unix_ms != feed.created_at_unix_ms
                        || existing.created_by != feed.created_by
                        || feed.updated_at_unix_ms < existing.updated_at_unix_ms =>
                {
                    return Err(CgWriteError::Invalid(
                        "A CG feed update cannot replace identity, provenance, move time backward, or forge removal.",
                    ));
                }
                _ => {}
            }
            self.upsert_cg_feed_source(feed)
                .map_err(CgWriteError::Storage)?;
            self.insert_cg_audit(audit).map_err(CgWriteError::Storage)?;
            Ok(feed.clone())
        })();
        finish_cg_transaction(self, outcome)
    }

    pub fn read_cg_feed_source(
        &self,
        feed_source_id: &str,
    ) -> Result<Option<CgFeedSource>, String> {
        let statement = self.prepare(
            "SELECT feed_source_id, channel_id, kind, label, source_url, trust_tier, tags_json, refresh_seconds, \
             enabled, revision, created_at_unix_ms, updated_at_unix_ms, created_by, \
             last_fetched_at_unix_ms, last_fetch_error FROM cg_feed_sources WHERE feed_source_id=?1",
        )?;
        let feed_source_id = c_string(feed_source_id, "CG feed identity")?;
        bind_text(self.database, statement.raw, 1, &feed_source_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_cg_feed_source_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_cg_feed_sources(&self, channel_id: &str) -> Result<Vec<CgFeedSource>, String> {
        let statement = self.prepare(
            "SELECT feed_source_id, channel_id, kind, label, source_url, trust_tier, tags_json, refresh_seconds, \
             enabled, revision, created_at_unix_ms, updated_at_unix_ms, created_by, \
             last_fetched_at_unix_ms, last_fetch_error FROM cg_feed_sources WHERE channel_id=?1 \
             ORDER BY label COLLATE NOCASE, feed_source_id",
        )?;
        let channel_id = c_string(channel_id, "CG feed channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        let mut feeds = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => feeds.push(read_cg_feed_source_row(statement.raw)?),
                SQLITE_DONE => return Ok(feeds),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn list_all_cg_feed_sources(&self) -> Result<Vec<CgFeedSource>, String> {
        let mut feeds = Vec::new();
        for channel in self.list_channels()? {
            feeds.extend(self.list_cg_feed_sources(&channel.channel_id)?);
        }
        Ok(feeds)
    }

    pub fn replace_cg_feed_cache(
        &self,
        feed_source_id: &str,
        fetched_at_unix_ms: i64,
        items: &[CgFeedItem],
        etag: Option<&str>,
        last_modified: Option<&str>,
    ) -> Result<CgFeedCacheEntry, CgWriteError> {
        if fetched_at_unix_ms < 0 {
            return Err(CgWriteError::Invalid(
                "CG feed-cache time cannot be negative.",
            ));
        }
        let feed_source_id_value = feed_source_id.to_string();
        self.execute("BEGIN IMMEDIATE")
            .map_err(CgWriteError::Storage)?;
        let outcome = (|| {
            let current = self
                .read_cg_feed_source(feed_source_id)
                .map_err(CgWriteError::Storage)?
                .ok_or(CgWriteError::NotFound)?;
            if !current.enabled {
                return Err(CgWriteError::Invalid(
                    "CG feed cache cannot be replaced for a disabled source.",
                ));
            }
            if fetched_at_unix_ms < current.created_at_unix_ms
                || current
                    .last_fetched_at_unix_ms
                    .is_some_and(|last| fetched_at_unix_ms < last)
            {
                return Err(CgWriteError::Invalid(
                    "CG feed-cache time cannot move backward.",
                ));
            }
            let expires_at_unix_ms = fetched_at_unix_ms
                .checked_add(i64::from(current.refresh_seconds) * 1_000)
                .ok_or(CgWriteError::Invalid(
                    "CG feed-cache expiry exceeds the supported range.",
                ))?;
            let entry = CgFeedCacheEntry {
                feed_source_id: feed_source_id_value.clone(),
                fetched_at_unix_ms,
                expires_at_unix_ms,
                etag: etag.map(str::to_string),
                last_modified: last_modified.map(str::to_string),
                last_error: None,
                items: items.to_vec(),
            };
            entry.validate().map_err(CgWriteError::Invalid)?;
            let source = c_string(feed_source_id, "CG feed-cache source identity")
                .map_err(CgWriteError::Storage)?;
            let delete_items = self
                .prepare("DELETE FROM cg_feed_cache_items WHERE feed_source_id=?1")
                .map_err(CgWriteError::Storage)?;
            bind_text(self.database, delete_items.raw, 1, &source)
                .map_err(CgWriteError::Storage)?;
            expect_done(self.database, delete_items.raw).map_err(CgWriteError::Storage)?;

            for item in &entry.items {
                let insert = self
                    .prepare(
                        "INSERT INTO cg_feed_cache_items(feed_source_id, item_id, title, summary, \
                         starts_at_unix_ms, url, tags_json, fetched_at_unix_ms) \
                         VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
                    )
                    .map_err(CgWriteError::Storage)?;
                let item_id = c_string(&item.item_id, "CG feed item identity")
                    .map_err(CgWriteError::Storage)?;
                let title =
                    c_string(&item.title, "CG feed item title").map_err(CgWriteError::Storage)?;
                let summary = c_string(
                    item.summary.as_deref().unwrap_or_default(),
                    "CG feed item summary",
                )
                .map_err(CgWriteError::Storage)?;
                let url = c_string(item.url.as_deref().unwrap_or_default(), "CG feed item URL")
                    .map_err(CgWriteError::Storage)?;
                let tags = c_string(
                    &serde_json::to_string(&item.tags)
                        .map_err(|error| CgWriteError::Storage(error.to_string()))?,
                    "CG feed item tags",
                )
                .map_err(CgWriteError::Storage)?;
                bind_text(self.database, insert.raw, 1, &source).map_err(CgWriteError::Storage)?;
                bind_text(self.database, insert.raw, 2, &item_id).map_err(CgWriteError::Storage)?;
                bind_text(self.database, insert.raw, 3, &title).map_err(CgWriteError::Storage)?;
                bind_text(self.database, insert.raw, 4, &summary).map_err(CgWriteError::Storage)?;
                bind_i64(
                    self.database,
                    insert.raw,
                    5,
                    item.starts_at_unix_ms.unwrap_or(-1),
                )
                .map_err(CgWriteError::Storage)?;
                bind_text(self.database, insert.raw, 6, &url).map_err(CgWriteError::Storage)?;
                bind_text(self.database, insert.raw, 7, &tags).map_err(CgWriteError::Storage)?;
                bind_i64(self.database, insert.raw, 8, fetched_at_unix_ms)
                    .map_err(CgWriteError::Storage)?;
                expect_done(self.database, insert.raw).map_err(CgWriteError::Storage)?;
            }
            let meta = self
                .prepare(
                    "INSERT INTO cg_feed_cache_meta(feed_source_id, fetched_at_unix_ms, \
                     expires_at_unix_ms, etag, last_modified, last_error) VALUES(?1, ?2, ?3, ?4, ?5, '') \
                     ON CONFLICT(feed_source_id) DO UPDATE SET fetched_at_unix_ms=excluded.fetched_at_unix_ms, \
                     expires_at_unix_ms=excluded.expires_at_unix_ms, etag=excluded.etag, \
                     last_modified=excluded.last_modified, last_error=''",
                )
                .map_err(CgWriteError::Storage)?;
            let etag =
                c_string(etag.unwrap_or_default(), "CG ETag").map_err(CgWriteError::Storage)?;
            let last_modified = c_string(last_modified.unwrap_or_default(), "CG Last-Modified")
                .map_err(CgWriteError::Storage)?;
            bind_text(self.database, meta.raw, 1, &source).map_err(CgWriteError::Storage)?;
            bind_i64(self.database, meta.raw, 2, fetched_at_unix_ms)
                .map_err(CgWriteError::Storage)?;
            bind_i64(self.database, meta.raw, 3, expires_at_unix_ms)
                .map_err(CgWriteError::Storage)?;
            bind_text(self.database, meta.raw, 4, &etag).map_err(CgWriteError::Storage)?;
            bind_text(self.database, meta.raw, 5, &last_modified).map_err(CgWriteError::Storage)?;
            expect_done(self.database, meta.raw).map_err(CgWriteError::Storage)?;
            let update = self
                .prepare(
                    "UPDATE cg_feed_sources SET last_fetched_at_unix_ms=?1, last_fetch_error='' \
                     WHERE feed_source_id=?2",
                )
                .map_err(CgWriteError::Storage)?;
            bind_i64(self.database, update.raw, 1, fetched_at_unix_ms)
                .map_err(CgWriteError::Storage)?;
            bind_text(self.database, update.raw, 2, &source).map_err(CgWriteError::Storage)?;
            expect_done(self.database, update.raw).map_err(CgWriteError::Storage)?;
            Ok(entry.clone())
        })();
        finish_cg_transaction(self, outcome)
    }

    pub fn refresh_cg_feed_cache(
        &self,
        feed_source_id: &str,
        fetched_at_unix_ms: i64,
    ) -> Result<CgFeedCacheEntry, CgWriteError> {
        if fetched_at_unix_ms < 0 {
            return Err(CgWriteError::Invalid(
                "CG feed-cache refresh time cannot be negative.",
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(CgWriteError::Storage)?;
        let outcome = (|| {
            let feed = self
                .read_cg_feed_source(feed_source_id)
                .map_err(CgWriteError::Storage)?
                .ok_or(CgWriteError::NotFound)?;
            if !feed.enabled {
                return Err(CgWriteError::Invalid(
                    "CG feed cache cannot be refreshed for a disabled source.",
                ));
            }
            if fetched_at_unix_ms < feed.created_at_unix_ms
                || feed
                    .last_fetched_at_unix_ms
                    .is_some_and(|last| fetched_at_unix_ms < last)
            {
                return Err(CgWriteError::Invalid(
                    "CG feed-cache refresh time cannot move backward.",
                ));
            }
            let current = self
                .read_cg_feed_cache(feed_source_id)
                .map_err(CgWriteError::Storage)?
                .ok_or(CgWriteError::NotFound)?;
            let expires_at_unix_ms = fetched_at_unix_ms
                .checked_add(i64::from(feed.refresh_seconds) * 1_000)
                .ok_or(CgWriteError::Invalid(
                    "CG feed-cache expiry exceeds the supported range.",
                ))?;
            let source = c_string(feed_source_id, "CG feed-cache source identity")
                .map_err(CgWriteError::Storage)?;
            let meta = self
                .prepare(
                    "UPDATE cg_feed_cache_meta SET fetched_at_unix_ms=?1, \
                     expires_at_unix_ms=?2, last_error='' WHERE feed_source_id=?3",
                )
                .map_err(CgWriteError::Storage)?;
            bind_i64(self.database, meta.raw, 1, fetched_at_unix_ms)
                .map_err(CgWriteError::Storage)?;
            bind_i64(self.database, meta.raw, 2, expires_at_unix_ms)
                .map_err(CgWriteError::Storage)?;
            bind_text(self.database, meta.raw, 3, &source).map_err(CgWriteError::Storage)?;
            expect_done(self.database, meta.raw).map_err(CgWriteError::Storage)?;
            let source_update = self
                .prepare(
                    "UPDATE cg_feed_sources SET last_fetched_at_unix_ms=?1, last_fetch_error='' \
                     WHERE feed_source_id=?2",
                )
                .map_err(CgWriteError::Storage)?;
            bind_i64(self.database, source_update.raw, 1, fetched_at_unix_ms)
                .map_err(CgWriteError::Storage)?;
            bind_text(self.database, source_update.raw, 2, &source)
                .map_err(CgWriteError::Storage)?;
            expect_done(self.database, source_update.raw).map_err(CgWriteError::Storage)?;
            Ok(CgFeedCacheEntry {
                feed_source_id: feed_source_id.into(),
                fetched_at_unix_ms,
                expires_at_unix_ms,
                etag: current.etag,
                last_modified: current.last_modified,
                last_error: None,
                items: current.items,
            })
        })();
        finish_cg_transaction(self, outcome)
    }

    pub fn record_cg_feed_fetch_failure(
        &self,
        feed_source_id: &str,
        failed_at_unix_ms: i64,
        error: &str,
    ) -> Result<(), CgWriteError> {
        if failed_at_unix_ms < 0 || error.trim().is_empty() || error.len() > 1_000 {
            return Err(CgWriteError::Invalid(
                "CG feed-fetch failure evidence is invalid.",
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(CgWriteError::Storage)?;
        let outcome = (|| {
            let feed = self
                .read_cg_feed_source(feed_source_id)
                .map_err(CgWriteError::Storage)?
                .ok_or(CgWriteError::NotFound)?;
            if failed_at_unix_ms < feed.created_at_unix_ms
                || feed
                    .last_fetched_at_unix_ms
                    .is_some_and(|last| failed_at_unix_ms < last)
            {
                return Err(CgWriteError::Invalid(
                    "CG feed-fetch time cannot move backward.",
                ));
            }
            let statement = self
                .prepare(
                    "UPDATE cg_feed_sources SET last_fetched_at_unix_ms=?1, last_fetch_error=?2 \
                     WHERE feed_source_id=?3",
                )
                .map_err(CgWriteError::Storage)?;
            let error = c_string(error, "CG feed-fetch error").map_err(CgWriteError::Storage)?;
            let id = c_string(feed_source_id, "CG feed identity").map_err(CgWriteError::Storage)?;
            bind_i64(self.database, statement.raw, 1, failed_at_unix_ms)
                .map_err(CgWriteError::Storage)?;
            bind_text(self.database, statement.raw, 2, &error).map_err(CgWriteError::Storage)?;
            bind_text(self.database, statement.raw, 3, &id).map_err(CgWriteError::Storage)?;
            expect_done(self.database, statement.raw).map_err(CgWriteError::Storage)?;
            let cache_error = self
                .prepare("UPDATE cg_feed_cache_meta SET last_error=?1 WHERE feed_source_id=?2")
                .map_err(CgWriteError::Storage)?;
            bind_text(self.database, cache_error.raw, 1, &error).map_err(CgWriteError::Storage)?;
            bind_text(self.database, cache_error.raw, 2, &id).map_err(CgWriteError::Storage)?;
            expect_done(self.database, cache_error.raw).map_err(CgWriteError::Storage)
        })();
        finish_cg_transaction(self, outcome)
    }

    pub fn read_cg_feed_cache(
        &self,
        feed_source_id: &str,
    ) -> Result<Option<CgFeedCacheEntry>, String> {
        let meta = self.prepare(
            "SELECT fetched_at_unix_ms, expires_at_unix_ms, etag, last_modified, last_error \
             FROM cg_feed_cache_meta WHERE feed_source_id=?1",
        )?;
        let source = c_string(feed_source_id, "CG feed-cache source identity")?;
        bind_text(self.database, meta.raw, 1, &source)?;
        let (fetched, expires, etag, last_modified, last_error) =
            match unsafe { sqlite3_step(meta.raw) } {
                SQLITE_ROW => (
                    unsafe { sqlite3_column_int64(meta.raw, 0) },
                    unsafe { sqlite3_column_int64(meta.raw, 1) },
                    column_text(meta.raw, 2)?,
                    column_text(meta.raw, 3)?,
                    column_text(meta.raw, 4)?,
                ),
                SQLITE_DONE => return Ok(None),
                _ => return Err(sqlite_error(self.database)),
            };
        let statement = self.prepare(
            "SELECT item_id, title, summary, starts_at_unix_ms, url, tags_json \
             FROM cg_feed_cache_items WHERE feed_source_id=?1 ORDER BY starts_at_unix_ms, item_id",
        )?;
        bind_text(self.database, statement.raw, 1, &source)?;
        let mut items = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    let summary = column_text(statement.raw, 2)?;
                    let url = column_text(statement.raw, 4)?;
                    items.push(CgFeedItem {
                        item_id: column_text(statement.raw, 0)?,
                        title: column_text(statement.raw, 1)?,
                        summary: (!summary.is_empty()).then_some(summary),
                        starts_at_unix_ms: {
                            let value = unsafe { sqlite3_column_int64(statement.raw, 3) };
                            (value >= 0).then_some(value)
                        },
                        url: (!url.is_empty()).then_some(url),
                        tags: serde_json::from_str(&column_text(statement.raw, 5)?).map_err(
                            |error| format!("database returned invalid CG cache tags: {error}"),
                        )?,
                    });
                }
                SQLITE_DONE => break,
                _ => return Err(sqlite_error(self.database)),
            }
        }
        let entry = CgFeedCacheEntry {
            feed_source_id: feed_source_id.into(),
            fetched_at_unix_ms: fetched,
            expires_at_unix_ms: expires,
            etag: (!etag.is_empty()).then_some(etag),
            last_modified: (!last_modified.is_empty()).then_some(last_modified),
            last_error: (!last_error.is_empty()).then_some(last_error),
            items,
        };
        entry.validate().map_err(str::to_string)?;
        Ok(Some(entry))
    }

    pub fn list_cg_approved_feed_item_ids(
        &self,
        feed_source_id: &str,
    ) -> Result<BTreeSet<String>, String> {
        let statement = self.prepare(
            "SELECT item_id FROM cg_feed_item_approvals WHERE feed_source_id=?1 ORDER BY item_id",
        )?;
        let source = c_string(feed_source_id, "CG approval feed identity")?;
        bind_text(self.database, statement.raw, 1, &source)?;
        let mut ids = BTreeSet::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    ids.insert(column_text(statement.raw, 0)?);
                }
                SQLITE_DONE => return Ok(ids),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn list_cg_feed_item_approvals(
        &self,
        feed_source_id: &str,
    ) -> Result<Vec<CgFeedItemApproval>, String> {
        let statement = self.prepare(
            "SELECT feed_source_id, item_id, approved_by, approved_at_unix_ms \
             FROM cg_feed_item_approvals WHERE feed_source_id=?1 ORDER BY item_id",
        )?;
        let source = c_string(feed_source_id, "CG approval feed identity")?;
        bind_text(self.database, statement.raw, 1, &source)?;
        let mut approvals = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => approvals.push(read_cg_feed_item_approval_row(statement.raw)?),
                SQLITE_DONE => return Ok(approvals),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn delete_cg_feed_source(
        &self,
        feed_source_id: &str,
        expected_revision: u64,
        audit: &CgAuditEvent,
    ) -> Result<(), CgWriteError> {
        audit.validate().map_err(CgWriteError::Invalid)?;
        if audit.event_kind != CgAuditEventKind::FeedRemoved {
            return Err(CgWriteError::Invalid(
                "CG feed deletion requires feed-removed audit evidence.",
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(CgWriteError::Storage)?;
        let outcome = (|| {
            let feed = self
                .read_cg_feed_source(feed_source_id)
                .map_err(CgWriteError::Storage)?
                .ok_or(CgWriteError::NotFound)?;
            if feed.revision != expected_revision {
                return Err(CgWriteError::Conflict(feed.revision));
            }
            if audit.channel_id != feed.channel_id
                || audit.occurred_at_unix_ms < feed.updated_at_unix_ms
            {
                return Err(CgWriteError::Invalid(
                    "CG feed deletion audit evidence does not match the current feed.",
                ));
            }
            if let Some(board) = self
                .read_cg_board(&feed.channel_id)
                .map_err(CgWriteError::Storage)?
            {
                if audit.board_id.as_deref() != Some(board.board_id.as_str()) {
                    return Err(CgWriteError::Invalid(
                        "CG feed deletion audit evidence identifies the wrong board.",
                    ));
                }
                if self
                    .list_cg_zones(&board.board_id)
                    .map_err(CgWriteError::Storage)?
                    .iter()
                    .any(|zone| {
                        matches!(
                            &zone.content_source,
                            CgContentSource::Feed { feed_source_id: used_id }
                                if used_id == feed_source_id
                        )
                    })
                {
                    return Err(CgWriteError::Invalid(
                        "CG feed cannot be removed while a board zone still uses it.",
                    ));
                }
            } else if audit.board_id.is_some() {
                return Err(CgWriteError::Invalid(
                    "CG feed deletion audit evidence identifies an unavailable board.",
                ));
            }
            let statement = self
                .prepare("DELETE FROM cg_feed_sources WHERE feed_source_id=?1 AND revision=?2")
                .map_err(CgWriteError::Storage)?;
            let feed_source_id =
                c_string(feed_source_id, "CG feed identity").map_err(CgWriteError::Storage)?;
            bind_text(self.database, statement.raw, 1, &feed_source_id)
                .map_err(CgWriteError::Storage)?;
            bind_i64(
                self.database,
                statement.raw,
                2,
                checked_i64(expected_revision, "CG feed revision")
                    .map_err(CgWriteError::Storage)?,
            )
            .map_err(CgWriteError::Storage)?;
            expect_done(self.database, statement.raw).map_err(CgWriteError::Storage)?;
            drop(statement);
            if self.changes() != 1 {
                return Err(CgWriteError::Conflict(feed.revision));
            }
            self.insert_cg_audit(audit).map_err(CgWriteError::Storage)
        })();
        finish_cg_transaction(self, outcome)
    }

    pub fn write_cg_bulletin(
        &self,
        bulletin: &CgBulletin,
        expected_revision: u64,
        audit: &CgAuditEvent,
    ) -> Result<CgBulletin, CgWriteError> {
        bulletin.validate().map_err(CgWriteError::Invalid)?;
        audit.validate().map_err(CgWriteError::Invalid)?;
        if audit.channel_id != bulletin.channel_id {
            return Err(CgWriteError::Invalid(
                "CG bulletin audit evidence identifies the wrong channel.",
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(CgWriteError::Storage)?;
        let outcome = (|| {
            let mut asset_ids = Vec::new();
            match &bulletin.content {
                CgBulletinContent::UploadedImage { asset_id, .. }
                | CgBulletinContent::FullscreenSlide { asset_id, .. } => {
                    asset_ids.push(asset_id.as_str());
                }
                _ => {}
            }
            if let Some(asset_id) = bulletin.audio_asset_id.as_deref() {
                asset_ids.push(asset_id);
            }
            for asset_id in asset_ids {
                let asset = self
                    .read_media_asset(asset_id)
                    .map_err(CgWriteError::Storage)?
                    .ok_or(CgWriteError::NotFound)?;
                if asset.readiness != AssetReadiness::Ready {
                    return Err(CgWriteError::Invalid(
                        "CG bulletin media must identify ready managed assets.",
                    ));
                }
            }
            let current = self
                .read_cg_bulletin(&bulletin.bulletin_id)
                .map_err(CgWriteError::Storage)?;
            let current_revision = current.as_ref().map_or(0, |value| value.revision);
            if current_revision != expected_revision {
                return Err(CgWriteError::Conflict(current_revision));
            }
            if bulletin.revision != expected_revision.saturating_add(1) {
                return Err(CgWriteError::Invalid(
                    "CG bulletin revision must be exactly one greater than the expected revision.",
                ));
            }
            match current {
                None if bulletin.state != CgBulletinState::Submitted
                    || audit.event_kind != CgAuditEventKind::BulletinSubmitted =>
                {
                    return Err(CgWriteError::Invalid(
                        "A new CG bulletin must begin submitted with submission audit evidence.",
                    ));
                }
                Some(existing)
                    if existing.channel_id != bulletin.channel_id
                        || existing.created_at_unix_ms != bulletin.created_at_unix_ms
                        || existing.submitted_by != bulletin.submitted_by
                        || bulletin.updated_at_unix_ms < existing.updated_at_unix_ms
                        || !valid_cg_bulletin_transition(existing.state, bulletin.state)
                        || !audit_matches_cg_bulletin_state(audit.event_kind, bulletin.state) =>
                {
                    return Err(CgWriteError::Invalid(
                        "CG bulletin transition, identity, provenance, time, or audit evidence is invalid.",
                    ));
                }
                _ => {}
            }
            self.upsert_cg_bulletin(bulletin)
                .map_err(CgWriteError::Storage)?;
            self.insert_cg_audit(audit).map_err(CgWriteError::Storage)?;
            Ok(bulletin.clone())
        })();
        finish_cg_transaction(self, outcome)
    }

    pub fn read_cg_bulletin(&self, bulletin_id: &str) -> Result<Option<CgBulletin>, String> {
        let statement = self.prepare(
            "SELECT bulletin_id, channel_id, content_json, state, tags_json, \
             requested_start_at_unix_ms, requested_end_at_unix_ms, display_seconds, audio_asset_id, \
             submitted_by, moderation_notes, moderated_by, revision, created_at_unix_ms, updated_at_unix_ms \
             FROM cg_bulletins WHERE bulletin_id=?1",
        )?;
        let bulletin_id = c_string(bulletin_id, "CG bulletin identity")?;
        bind_text(self.database, statement.raw, 1, &bulletin_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_cg_bulletin_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_cg_bulletins(&self, channel_id: &str) -> Result<Vec<CgBulletin>, String> {
        let statement = self.prepare(
            "SELECT bulletin_id, channel_id, content_json, state, tags_json, \
             requested_start_at_unix_ms, requested_end_at_unix_ms, display_seconds, audio_asset_id, \
             submitted_by, moderation_notes, moderated_by, revision, created_at_unix_ms, updated_at_unix_ms \
             FROM cg_bulletins WHERE channel_id=?1 ORDER BY updated_at_unix_ms DESC, bulletin_id",
        )?;
        let channel_id = c_string(channel_id, "CG bulletin channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        let mut bulletins = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => bulletins.push(read_cg_bulletin_row(statement.raw)?),
                SQLITE_DONE => return Ok(bulletins),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn list_airable_cg_bulletins(
        &self,
        channel_id: &str,
        now_unix_ms: i64,
    ) -> Result<Vec<CgBulletin>, String> {
        if now_unix_ms < 0 {
            return Err("CG bulletin resolution time must be nonnegative".into());
        }
        Ok(self
            .list_cg_bulletins(channel_id)?
            .into_iter()
            .filter(|bulletin| bulletin.is_airable_at(now_unix_ms))
            .collect())
    }

    /// Expire scheduled bulletins whose requested end has arrived.
    ///
    /// This is deliberately an explicit, channel-scoped operation. It does not
    /// run a scheduler; callers supply the observation time and operator
    /// identity, and the state changes plus audit rows commit together.
    pub fn expire_due_cg_bulletins(
        &self,
        channel_id: &str,
        now_unix_ms: i64,
        operator_id: &str,
    ) -> Result<Vec<CgBulletin>, CgWriteError> {
        if now_unix_ms < 0 {
            return Err(CgWriteError::Invalid(
                "CG bulletin expiration time must be nonnegative.",
            ));
        }
        let operator_id = operator_id.trim();
        let validation_audit = CgAuditEvent {
            audit_id: deterministic_cg_expiration_audit_id(channel_id, channel_id, now_unix_ms),
            board_id: None,
            channel_id: channel_id.into(),
            event_kind: CgAuditEventKind::BulletinExpired,
            operator_id: operator_id.into(),
            occurred_at_unix_ms: now_unix_ms,
            details: BTreeMap::new(),
        };
        validation_audit.validate().map_err(CgWriteError::Invalid)?;

        self.execute("BEGIN IMMEDIATE")
            .map_err(CgWriteError::Storage)?;
        let outcome = (|| {
            let channel = self
                .prepare("SELECT 1 FROM channels WHERE channel_id=?1")
                .map_err(CgWriteError::Storage)?;
            let channel_id_value = c_string(channel_id, "CG bulletin expiration channel identity")
                .map_err(CgWriteError::Storage)?;
            bind_text(self.database, channel.raw, 1, &channel_id_value)
                .map_err(CgWriteError::Storage)?;
            if unsafe { sqlite3_step(channel.raw) } != SQLITE_ROW {
                return Err(CgWriteError::NotFound);
            }
            drop(channel);

            let due: Vec<CgBulletin> = self
                .list_cg_bulletins(channel_id)
                .map_err(CgWriteError::Storage)?
                .into_iter()
                .filter(|bulletin| {
                    bulletin.channel_id == channel_id
                        && bulletin.state == CgBulletinState::Scheduled
                        && bulletin
                            .requested_end_at_unix_ms
                            .is_some_and(|end| end <= now_unix_ms)
                })
                .collect();
            let mut expired = Vec::with_capacity(due.len());
            for bulletin in due {
                if bulletin.updated_at_unix_ms > now_unix_ms {
                    return Err(CgWriteError::Invalid(
                        "CG bulletin expiration time cannot precede the bulletin update time.",
                    ));
                }
                let revision = bulletin
                    .revision
                    .checked_add(1)
                    .ok_or(CgWriteError::Invalid(
                        "CG bulletin revision cannot be incremented.",
                    ))?;
                let mut updated = bulletin.clone();
                updated.state = CgBulletinState::Expired;
                updated.revision = revision;
                updated.updated_at_unix_ms = now_unix_ms;
                updated.validate().map_err(CgWriteError::Invalid)?;
                let statement = self
                    .prepare(
                        "UPDATE cg_bulletins SET state='expired', revision=?1, updated_at_unix_ms=?2 \
                         WHERE bulletin_id=?3 AND channel_id=?4 AND state='scheduled' \
                         AND requested_end_at_unix_ms>=0 AND requested_end_at_unix_ms<=?2 AND revision=?5",
                    )
                    .map_err(CgWriteError::Storage)?;
                bind_i64(
                    self.database,
                    statement.raw,
                    1,
                    checked_i64(revision, "CG bulletin revision").map_err(CgWriteError::Storage)?,
                )
                .map_err(CgWriteError::Storage)?;
                bind_i64(self.database, statement.raw, 2, now_unix_ms)
                    .map_err(CgWriteError::Storage)?;
                let bulletin_id =
                    c_string(&bulletin.bulletin_id, "CG bulletin expiration identity")
                        .map_err(CgWriteError::Storage)?;
                bind_text(self.database, statement.raw, 3, &bulletin_id)
                    .map_err(CgWriteError::Storage)?;
                bind_text(self.database, statement.raw, 4, &channel_id_value)
                    .map_err(CgWriteError::Storage)?;
                bind_i64(
                    self.database,
                    statement.raw,
                    5,
                    checked_i64(bulletin.revision, "prior CG bulletin revision")
                        .map_err(CgWriteError::Storage)?,
                )
                .map_err(CgWriteError::Storage)?;
                expect_done(self.database, statement.raw).map_err(CgWriteError::Storage)?;
                if self.changes() != 1 {
                    return Err(CgWriteError::Conflict(bulletin.revision));
                }
                drop(statement);

                let mut details = BTreeMap::new();
                details.insert("bulletin_id".into(), bulletin.bulletin_id.clone());
                details.insert("prior_revision".into(), bulletin.revision.to_string());
                details.insert(
                    "requested_end_at_unix_ms".into(),
                    bulletin
                        .requested_end_at_unix_ms
                        .expect("eligible bulletin has an end time")
                        .to_string(),
                );
                let audit = CgAuditEvent {
                    audit_id: deterministic_cg_expiration_audit_id(
                        channel_id,
                        &bulletin.bulletin_id,
                        now_unix_ms,
                    ),
                    board_id: None,
                    channel_id: channel_id.into(),
                    event_kind: CgAuditEventKind::BulletinExpired,
                    operator_id: operator_id.into(),
                    occurred_at_unix_ms: now_unix_ms,
                    details,
                };
                audit.validate().map_err(CgWriteError::Invalid)?;
                self.insert_cg_audit(&audit)
                    .map_err(CgWriteError::Storage)?;
                expired.push(updated);
            }
            Ok(expired)
        })();
        finish_cg_transaction(self, outcome)
    }

    pub fn approve_cg_feed_item(
        &self,
        approval: &CgFeedItemApproval,
        audit: &CgAuditEvent,
    ) -> Result<CgFeedItemApproval, CgWriteError> {
        approval.validate().map_err(CgWriteError::Invalid)?;
        audit.validate().map_err(CgWriteError::Invalid)?;
        if audit.event_kind != CgAuditEventKind::FeedItemApproved {
            return Err(CgWriteError::Invalid(
                "CG feed-item approval requires matching audit evidence.",
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(CgWriteError::Storage)?;
        let outcome = (|| {
            let feed = self
                .read_cg_feed_source(&approval.feed_source_id)
                .map_err(CgWriteError::Storage)?
                .ok_or(CgWriteError::NotFound)?;
            if feed.channel_id != audit.channel_id || approval.approved_by != audit.operator_id {
                return Err(CgWriteError::Invalid(
                    "CG feed-item approval and audit identities disagree.",
                ));
            }
            if let Some(existing) = self
                .read_cg_feed_item_approval(&approval.feed_source_id, &approval.item_id)
                .map_err(CgWriteError::Storage)?
            {
                if existing != *approval {
                    return Err(CgWriteError::Invalid(
                        "A CG feed-item approval is immutable once recorded.",
                    ));
                }
                return Ok(existing);
            }
            let observed = self
                .prepare("SELECT 1 FROM cg_feed_cache_items WHERE feed_source_id=?1 AND item_id=?2")
                .map_err(CgWriteError::Storage)?;
            let feed_id = c_string(&approval.feed_source_id, "CG approval feed identity")
                .map_err(CgWriteError::Storage)?;
            let item_id = c_string(&approval.item_id, "CG approval item identity")
                .map_err(CgWriteError::Storage)?;
            bind_text(self.database, observed.raw, 1, &feed_id).map_err(CgWriteError::Storage)?;
            bind_text(self.database, observed.raw, 2, &item_id).map_err(CgWriteError::Storage)?;
            if unsafe { sqlite3_step(observed.raw) } != SQLITE_ROW {
                return Err(CgWriteError::NotFound);
            }
            let statement = self
                .prepare(
                    "INSERT INTO cg_feed_item_approvals(feed_source_id, item_id, approved_by, \
                     approved_at_unix_ms) VALUES(?1, ?2, ?3, ?4)",
                )
                .map_err(CgWriteError::Storage)?;
            let values = [
                c_string(&approval.feed_source_id, "CG approval feed identity")
                    .map_err(CgWriteError::Storage)?,
                c_string(&approval.item_id, "CG approval item identity")
                    .map_err(CgWriteError::Storage)?,
                c_string(&approval.approved_by, "CG approval operator")
                    .map_err(CgWriteError::Storage)?,
            ];
            for (index, value) in values.iter().enumerate() {
                bind_text(self.database, statement.raw, (index + 1) as c_int, value)
                    .map_err(CgWriteError::Storage)?;
            }
            bind_i64(
                self.database,
                statement.raw,
                4,
                approval.approved_at_unix_ms,
            )
            .map_err(CgWriteError::Storage)?;
            expect_done(self.database, statement.raw).map_err(CgWriteError::Storage)?;
            drop(statement);
            self.insert_cg_audit(audit).map_err(CgWriteError::Storage)?;
            Ok(approval.clone())
        })();
        finish_cg_transaction(self, outcome)
    }

    pub fn read_cg_feed_item_approval(
        &self,
        feed_source_id: &str,
        item_id: &str,
    ) -> Result<Option<CgFeedItemApproval>, String> {
        let statement = self.prepare(
            "SELECT feed_source_id, item_id, approved_by, approved_at_unix_ms \
             FROM cg_feed_item_approvals WHERE feed_source_id=?1 AND item_id=?2",
        )?;
        let feed_source_id = c_string(feed_source_id, "CG approval feed identity")?;
        let item_id = c_string(item_id, "CG approval item identity")?;
        bind_text(self.database, statement.raw, 1, &feed_source_id)?;
        bind_text(self.database, statement.raw, 2, &item_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_cg_feed_item_approval_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn read_cg_board_by_id(&self, board_id: &str) -> Result<Option<CgBoard>, String> {
        let statement = self.prepare(
            "SELECT board_id, channel_id, template_id, active, background_audio_asset_id, revision, \
             created_at_unix_ms, updated_at_unix_ms, created_by FROM cg_boards WHERE board_id=?1",
        )?;
        let board_id = c_string(board_id, "CG board identity")?;
        bind_text(self.database, statement.raw, 1, &board_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_cg_board_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn upsert_cg_board(&self, board: &CgBoard) -> Result<(), String> {
        let statement = self.prepare(
            "INSERT INTO cg_boards(board_id, channel_id, template_id, active, background_audio_asset_id, \
             revision, created_at_unix_ms, updated_at_unix_ms, created_by) \
             VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9) \
             ON CONFLICT(board_id) DO UPDATE SET template_id=excluded.template_id, active=excluded.active, \
             background_audio_asset_id=excluded.background_audio_asset_id, revision=excluded.revision, \
             updated_at_unix_ms=excluded.updated_at_unix_ms \
             WHERE cg_boards.channel_id=excluded.channel_id AND cg_boards.revision=?10",
        )?;
        let values = [
            c_string(&board.board_id, "CG board identity")?,
            c_string(&board.channel_id, "CG board channel identity")?,
            c_string(&board.template_id, "CG board template identity")?,
            c_string(
                board
                    .background_audio_asset_id
                    .as_deref()
                    .unwrap_or_default(),
                "CG board background audio identity",
            )?,
            c_string(&board.created_by, "CG board creator")?,
        ];
        bind_text(self.database, statement.raw, 1, &values[0])?;
        bind_text(self.database, statement.raw, 2, &values[1])?;
        bind_text(self.database, statement.raw, 3, &values[2])?;
        bind_i64(self.database, statement.raw, 4, i64::from(board.active))?;
        bind_text(self.database, statement.raw, 5, &values[3])?;
        bind_i64(
            self.database,
            statement.raw,
            6,
            checked_i64(board.revision, "CG board revision")?,
        )?;
        bind_i64(self.database, statement.raw, 7, board.created_at_unix_ms)?;
        bind_i64(self.database, statement.raw, 8, board.updated_at_unix_ms)?;
        bind_text(self.database, statement.raw, 9, &values[4])?;
        bind_i64(
            self.database,
            statement.raw,
            10,
            checked_i64(board.revision - 1, "prior CG board revision")?,
        )?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("CG board write lost its revision authority".into())
        }
    }

    fn upsert_cg_zone(&self, zone: &CgZoneConfig) -> Result<(), String> {
        let statement = self.prepare(
            "INSERT INTO cg_zone_configs(zone_id, board_id, template_zone_key, zone_kind, \
             content_source_json, rotation_seconds, approval_required, allowed_tags_json, revision, \
             created_at_unix_ms, updated_at_unix_ms) VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11) \
             ON CONFLICT(zone_id) DO UPDATE SET template_zone_key=excluded.template_zone_key, \
             zone_kind=excluded.zone_kind, content_source_json=excluded.content_source_json, \
             rotation_seconds=excluded.rotation_seconds, approval_required=excluded.approval_required, \
             allowed_tags_json=excluded.allowed_tags_json, revision=excluded.revision, \
             updated_at_unix_ms=excluded.updated_at_unix_ms \
             WHERE cg_zone_configs.board_id=excluded.board_id AND cg_zone_configs.revision=?12",
        )?;
        let values = [
            c_string(&zone.zone_id, "CG zone identity")?,
            c_string(&zone.board_id, "CG zone board identity")?,
            c_string(&zone.template_zone_key, "CG template zone key")?,
            c_string(cg_zone_kind_name(zone.zone_kind), "CG zone kind")?,
            c_string(
                &serde_json::to_string(&zone.content_source)
                    .map_err(|error| format!("could not serialize CG content source: {error}"))?,
                "CG content source",
            )?,
            c_string(
                &serde_json::to_string(&zone.allowed_tags)
                    .map_err(|error| format!("could not serialize CG zone tags: {error}"))?,
                "CG zone tags",
            )?,
        ];
        for (index, value) in values[..5].iter().enumerate() {
            bind_text(self.database, statement.raw, (index + 1) as c_int, value)?;
        }
        bind_i64(
            self.database,
            statement.raw,
            6,
            zone.rotation_seconds.map_or(-1, i64::from),
        )?;
        bind_i64(
            self.database,
            statement.raw,
            7,
            i64::from(zone.approval_required),
        )?;
        bind_text(self.database, statement.raw, 8, &values[5])?;
        bind_i64(
            self.database,
            statement.raw,
            9,
            checked_i64(zone.revision, "CG zone revision")?,
        )?;
        bind_i64(self.database, statement.raw, 10, zone.created_at_unix_ms)?;
        bind_i64(self.database, statement.raw, 11, zone.updated_at_unix_ms)?;
        bind_i64(
            self.database,
            statement.raw,
            12,
            checked_i64(zone.revision - 1, "prior CG zone revision")?,
        )?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("CG zone write lost its revision authority".into())
        }
    }

    fn upsert_cg_feed_source(&self, feed: &CgFeedSource) -> Result<(), String> {
        let statement = self.prepare(
            "INSERT INTO cg_feed_sources(feed_source_id, channel_id, kind, label, source_url, \
             trust_tier, tags_json, refresh_seconds, enabled, revision, created_at_unix_ms, updated_at_unix_ms, \
             created_by, last_fetched_at_unix_ms, last_fetch_error) \
             VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15) \
             ON CONFLICT(feed_source_id) DO UPDATE SET kind=excluded.kind, label=excluded.label, \
             source_url=excluded.source_url, trust_tier=excluded.trust_tier, \
             tags_json=excluded.tags_json, refresh_seconds=excluded.refresh_seconds, enabled=excluded.enabled, \
             revision=excluded.revision, updated_at_unix_ms=excluded.updated_at_unix_ms, \
             last_fetched_at_unix_ms=excluded.last_fetched_at_unix_ms, \
             last_fetch_error=excluded.last_fetch_error \
             WHERE cg_feed_sources.channel_id=excluded.channel_id AND cg_feed_sources.revision=?16",
        )?;
        let values = [
            c_string(&feed.feed_source_id, "CG feed identity")?,
            c_string(&feed.channel_id, "CG feed channel identity")?,
            c_string(cg_feed_kind_name(feed.kind), "CG feed kind")?,
            c_string(&feed.label, "CG feed label")?,
            c_string(&feed.source_url, "CG feed source URL")?,
            c_string(
                cg_feed_trust_tier_name(feed.trust_tier),
                "CG feed trust tier",
            )?,
            c_string(
                &serde_json::to_string(&feed.tags)
                    .map_err(|error| format!("could not serialize CG feed tags: {error}"))?,
                "CG feed tags",
            )?,
            c_string(&feed.created_by, "CG feed creator")?,
            c_string(
                feed.last_fetch_error.as_deref().unwrap_or_default(),
                "CG feed fetch error",
            )?,
        ];
        for (index, value) in values[..7].iter().enumerate() {
            bind_text(self.database, statement.raw, (index + 1) as c_int, value)?;
        }
        bind_i64(
            self.database,
            statement.raw,
            8,
            i64::from(feed.refresh_seconds),
        )?;
        bind_i64(self.database, statement.raw, 9, i64::from(feed.enabled))?;
        bind_i64(
            self.database,
            statement.raw,
            10,
            checked_i64(feed.revision, "CG feed revision")?,
        )?;
        bind_i64(self.database, statement.raw, 11, feed.created_at_unix_ms)?;
        bind_i64(self.database, statement.raw, 12, feed.updated_at_unix_ms)?;
        bind_text(self.database, statement.raw, 13, &values[7])?;
        bind_i64(
            self.database,
            statement.raw,
            14,
            feed.last_fetched_at_unix_ms.unwrap_or(-1),
        )?;
        bind_text(self.database, statement.raw, 15, &values[8])?;
        bind_i64(
            self.database,
            statement.raw,
            16,
            checked_i64(feed.revision - 1, "prior CG feed revision")?,
        )?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("CG feed write lost its revision authority".into())
        }
    }

    fn upsert_cg_bulletin(&self, bulletin: &CgBulletin) -> Result<(), String> {
        let statement = self.prepare(
            "INSERT INTO cg_bulletins(bulletin_id, channel_id, content_json, state, tags_json, \
             requested_start_at_unix_ms, requested_end_at_unix_ms, display_seconds, audio_asset_id, \
             submitted_by, moderation_notes, moderated_by, revision, created_at_unix_ms, updated_at_unix_ms) \
             VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15) \
             ON CONFLICT(bulletin_id) DO UPDATE SET content_json=excluded.content_json, \
             state=excluded.state, tags_json=excluded.tags_json, \
             requested_start_at_unix_ms=excluded.requested_start_at_unix_ms, \
             requested_end_at_unix_ms=excluded.requested_end_at_unix_ms, \
             display_seconds=excluded.display_seconds, audio_asset_id=excluded.audio_asset_id, \
             moderation_notes=excluded.moderation_notes, moderated_by=excluded.moderated_by, \
             revision=excluded.revision, updated_at_unix_ms=excluded.updated_at_unix_ms \
             WHERE cg_bulletins.channel_id=excluded.channel_id AND cg_bulletins.revision=?16",
        )?;
        let values = [
            c_string(&bulletin.bulletin_id, "CG bulletin identity")?,
            c_string(&bulletin.channel_id, "CG bulletin channel identity")?,
            c_string(
                &serde_json::to_string(&bulletin.content)
                    .map_err(|error| format!("could not serialize CG bulletin content: {error}"))?,
                "CG bulletin content",
            )?,
            c_string(cg_bulletin_state_name(bulletin.state), "CG bulletin state")?,
            c_string(
                &serde_json::to_string(&bulletin.tags)
                    .map_err(|error| format!("could not serialize CG bulletin tags: {error}"))?,
                "CG bulletin tags",
            )?,
            c_string(
                bulletin.audio_asset_id.as_deref().unwrap_or_default(),
                "CG bulletin audio identity",
            )?,
            c_string(&bulletin.submitted_by, "CG bulletin submitter")?,
            c_string(
                bulletin.moderation_notes.as_deref().unwrap_or_default(),
                "CG bulletin moderation notes",
            )?,
            c_string(
                bulletin.moderated_by.as_deref().unwrap_or_default(),
                "CG bulletin moderator",
            )?,
        ];
        for (index, value) in values[..5].iter().enumerate() {
            bind_text(self.database, statement.raw, (index + 1) as c_int, value)?;
        }
        bind_i64(
            self.database,
            statement.raw,
            6,
            bulletin.requested_start_at_unix_ms.unwrap_or(-1),
        )?;
        bind_i64(
            self.database,
            statement.raw,
            7,
            bulletin.requested_end_at_unix_ms.unwrap_or(-1),
        )?;
        bind_i64(
            self.database,
            statement.raw,
            8,
            i64::from(bulletin.display_seconds),
        )?;
        for (parameter, value) in [
            (9, &values[5]),
            (10, &values[6]),
            (11, &values[7]),
            (12, &values[8]),
        ] {
            bind_text(self.database, statement.raw, parameter, value)?;
        }
        bind_i64(
            self.database,
            statement.raw,
            13,
            checked_i64(bulletin.revision, "CG bulletin revision")?,
        )?;
        bind_i64(
            self.database,
            statement.raw,
            14,
            bulletin.created_at_unix_ms,
        )?;
        bind_i64(
            self.database,
            statement.raw,
            15,
            bulletin.updated_at_unix_ms,
        )?;
        bind_i64(
            self.database,
            statement.raw,
            16,
            checked_i64(bulletin.revision - 1, "prior CG bulletin revision")?,
        )?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("CG bulletin write lost its revision authority".into())
        }
    }

    fn insert_cg_audit(&self, audit: &CgAuditEvent) -> Result<(), String> {
        let statement = self.prepare(
            "INSERT INTO cg_audit_events(audit_id, board_id, channel_id, event_kind, operator_id, \
             occurred_at_unix_ms, details_json) VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        )?;
        let values = [
            c_string(&audit.audit_id, "CG audit identity")?,
            c_string(
                audit.board_id.as_deref().unwrap_or_default(),
                "CG audit board identity",
            )?,
            c_string(&audit.channel_id, "CG audit channel identity")?,
            c_string(
                cg_audit_event_kind_name(audit.event_kind),
                "CG audit event kind",
            )?,
            c_string(&audit.operator_id, "CG audit operator identity")?,
            c_string(
                &serde_json::to_string(&audit.details)
                    .map_err(|error| format!("could not serialize CG audit details: {error}"))?,
                "CG audit details",
            )?,
        ];
        for (index, value) in values[..5].iter().enumerate() {
            bind_text(self.database, statement.raw, (index + 1) as c_int, value)?;
        }
        bind_i64(self.database, statement.raw, 6, audit.occurred_at_unix_ms)?;
        bind_text(self.database, statement.raw, 7, &values[5])?;
        expect_done(self.database, statement.raw)
    }

    fn list_channel_output_sinks(
        &self,
        channel_id: &str,
    ) -> Result<Vec<OutputSinkConfiguration>, String> {
        let statement = self.prepare(
            "SELECT sink_id, label, kind, destination, loudness_target_milli, required, enabled, \
             EXISTS(SELECT 1 FROM srt_output_secrets secret \
                    WHERE secret.channel_id=channel_output_sinks.channel_id \
                      AND secret.sink_id=channel_output_sinks.sink_id) \
             FROM channel_output_sinks WHERE channel_id=?1 ORDER BY sink_id",
        )?;
        let channel_id = c_string(channel_id, "channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        let mut sinks = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => sinks.push(OutputSinkConfiguration {
                    sink_id: column_text(statement.raw, 0)?,
                    label: column_text(statement.raw, 1)?,
                    kind: parse_output_sink_kind(&column_text(statement.raw, 2)?)?,
                    destination: column_text(statement.raw, 3)?,
                    loudness_target_lufs: unsafe { sqlite3_column_int64(statement.raw, 4) } as f32
                        / 1_000.0,
                    required: unsafe { sqlite3_column_int64(statement.raw, 5) } == 1,
                    enabled: unsafe { sqlite3_column_int64(statement.raw, 6) } == 1,
                    encryption_configured: unsafe { sqlite3_column_int64(statement.raw, 7) } == 1,
                }),
                SQLITE_DONE => return Ok(sinks),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn read_srt_output_secret(
        &self,
        channel_id: &str,
        sink_id: &str,
    ) -> Result<Option<SrtOutputSecret>, String> {
        let statement = self.prepare(
            "SELECT encrypted_passphrase FROM srt_output_secrets WHERE channel_id=?1 AND sink_id=?2",
        )?;
        let channel_id = c_string(channel_id, "channel identity")?;
        let sink_id = c_string(sink_id, "output sink identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        bind_text(self.database, statement.raw, 2, &sink_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let decrypted = unprotect_local_machine(&column_blob(statement.raw, 0)?)?;
                let passphrase = String::from_utf8(decrypted)
                    .map_err(|_| "stored SRT passphrase is not valid UTF-8".to_string())?;
                validate_srt_passphrase(&passphrase)?;
                Ok(Some(SrtOutputSecret { passphrase }))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn put_channel_runtime(
        &self,
        observation: &ChannelRuntimeObservation,
    ) -> Result<(), String> {
        observation.validate().map_err(str::to_string)?;
        let statement = self.prepare(
            "INSERT INTO channel_runtime(channel_id, state, on_air_asset_id, \
             state_started_at_unix_ms, heartbeat_at_unix_ms, detail) VALUES(?1, ?2, ?3, ?4, ?5, ?6) \
             ON CONFLICT(channel_id) DO UPDATE SET state=excluded.state, \
             on_air_asset_id=excluded.on_air_asset_id, state_started_at_unix_ms=excluded.state_started_at_unix_ms, \
             heartbeat_at_unix_ms=excluded.heartbeat_at_unix_ms, detail=excluded.detail \
             WHERE excluded.heartbeat_at_unix_ms >= channel_runtime.heartbeat_at_unix_ms",
        )?;
        let channel_id = c_string(&observation.channel_id, "runtime channel identity")?;
        let state = c_string(
            channel_runtime_state_name(observation.state),
            "runtime state",
        )?;
        let asset_id = c_string(
            observation.on_air_asset_id.as_deref().unwrap_or_default(),
            "runtime asset identity",
        )?;
        let detail = c_string(
            observation.detail.as_deref().unwrap_or_default(),
            "runtime detail",
        )?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        bind_text(self.database, statement.raw, 2, &state)?;
        bind_text(self.database, statement.raw, 3, &asset_id)?;
        bind_i64(
            self.database,
            statement.raw,
            4,
            observation.state_started_at_unix_ms,
        )?;
        bind_i64(
            self.database,
            statement.raw,
            5,
            observation.heartbeat_at_unix_ms,
        )?;
        bind_text(self.database, statement.raw, 6, &detail)?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("channel runtime heartbeat moved backward".into())
        }
    }

    pub fn read_channel_runtime(
        &self,
        channel_id: &str,
    ) -> Result<Option<ChannelRuntimeObservation>, String> {
        let statement = self.prepare(
            "SELECT channel_id, state, on_air_asset_id, state_started_at_unix_ms, \
             heartbeat_at_unix_ms, detail FROM channel_runtime WHERE channel_id=?1",
        )?;
        let channel_id = c_string(channel_id, "runtime channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_channel_runtime_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_channel_runtime(&self) -> Result<Vec<ChannelRuntimeObservation>, String> {
        let statement = self.prepare(
            "SELECT channel_id, state, on_air_asset_id, state_started_at_unix_ms, \
             heartbeat_at_unix_ms, detail FROM channel_runtime ORDER BY channel_id",
        )?;
        let mut observations = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => observations.push(read_channel_runtime_row(statement.raw)?),
                SQLITE_DONE => return Ok(observations),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn record_system_resource_sample(
        &self,
        sample: &SystemResourceSample,
    ) -> Result<SystemResourceSample, String> {
        sample.validate().map_err(str::to_string)?;
        if sample.sample_id.is_some() {
            return Err("a new system resource sample cannot supply its database identity".into());
        }
        let statement = self.prepare(
            "INSERT INTO system_resource_samples(sampled_at_unix_ms, cpu_milli_percent, \
             ram_used_milli_gb, ram_total_milli_gb, gpu_milli_percent, gpu_vram_used_milli_gb, \
             media_volume_free_milli_gb, backup_volume_free_milli_gb, database_reachable, \
             backup_volume_writable, service_running, clock_skew_milliseconds) \
             VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)",
        )?;
        bind_i64(self.database, statement.raw, 1, sample.sampled_at_unix_ms)?;
        for (index, value) in [
            sample.cpu_percent,
            sample.ram_used_gb,
            sample.ram_total_gb,
            sample.gpu_percent,
            sample.gpu_vram_used_gb,
            sample.media_volume_free_gb,
            sample.backup_volume_free_gb,
        ]
        .into_iter()
        .enumerate()
        {
            bind_i64(
                self.database,
                statement.raw,
                c_int::try_from(index + 2).map_err(|_| "resource metric index overflowed")?,
                scaled_metric(value)?,
            )?;
        }
        bind_i64(
            self.database,
            statement.raw,
            9,
            i64::from(sample.database_reachable),
        )?;
        bind_i64(
            self.database,
            statement.raw,
            10,
            i64::from(sample.backup_volume_writable),
        )?;
        bind_i64(
            self.database,
            statement.raw,
            11,
            i64::from(sample.service_running),
        )?;
        bind_i64(
            self.database,
            statement.raw,
            12,
            scaled_signed_metric(sample.clock_skew_seconds)?,
        )?;
        expect_done(self.database, statement.raw)?;
        self.execute(
            "DELETE FROM system_resource_samples WHERE sample_id NOT IN (\
               SELECT sample_id FROM system_resource_samples ORDER BY sample_id DESC LIMIT 10080)",
        )?;
        self.latest_system_resource_sample()?
            .ok_or_else(|| "recorded system resource sample disappeared".into())
    }

    pub fn latest_system_resource_sample(&self) -> Result<Option<SystemResourceSample>, String> {
        let statement = self.prepare(
            "SELECT sample_id, sampled_at_unix_ms, cpu_milli_percent, ram_used_milli_gb, \
             ram_total_milli_gb, gpu_milli_percent, gpu_vram_used_milli_gb, \
             media_volume_free_milli_gb, backup_volume_free_milli_gb, database_reachable, \
             backup_volume_writable, service_running, clock_skew_milliseconds \
             FROM system_resource_samples ORDER BY sample_id DESC LIMIT 1",
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_system_resource_sample_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_system_resource_samples(
        &self,
        limit: u32,
    ) -> Result<Vec<SystemResourceSample>, String> {
        if !(1..=10_080).contains(&limit) {
            return Err("resource sample limit must be between 1 and 10080".into());
        }
        let statement = self.prepare(
            "SELECT sample_id, sampled_at_unix_ms, cpu_milli_percent, ram_used_milli_gb, \
             ram_total_milli_gb, gpu_milli_percent, gpu_vram_used_milli_gb, \
             media_volume_free_milli_gb, backup_volume_free_milli_gb, database_reachable, \
             backup_volume_writable, service_running, clock_skew_milliseconds \
             FROM system_resource_samples ORDER BY sample_id DESC LIMIT ?1",
        )?;
        bind_i64(self.database, statement.raw, 1, i64::from(limit))?;
        let mut samples = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => samples.push(read_system_resource_sample_row(statement.raw)?),
                SQLITE_DONE => return Ok(samples),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn put_self_test_schedule(
        &self,
        schedule: &SelfTestSchedule,
    ) -> Result<SelfTestSchedule, String> {
        schedule.validate().map_err(str::to_string)?;
        if let Some(existing) = self.read_self_test_schedule(schedule.kind)?
            && schedule.updated_at_unix_ms < existing.updated_at_unix_ms
        {
            return Err("self-test schedule update time moved backward".into());
        }
        let statement = self.prepare(
            "INSERT INTO self_test_schedules(kind, enabled, local_hour, local_minute, weekday, \
             updated_at_unix_ms, updated_by) VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7) \
             ON CONFLICT(kind) DO UPDATE SET enabled=excluded.enabled, local_hour=excluded.local_hour, \
             local_minute=excluded.local_minute, weekday=excluded.weekday, \
             updated_at_unix_ms=excluded.updated_at_unix_ms, updated_by=excluded.updated_by",
        )?;
        let kind = c_string(
            self_test_kind_name(schedule.kind),
            "self-test schedule kind",
        )?;
        let updated_by = c_string(&schedule.updated_by, "self-test schedule operator")?;
        bind_text(self.database, statement.raw, 1, &kind)?;
        bind_i64(self.database, statement.raw, 2, i64::from(schedule.enabled))?;
        bind_i64(
            self.database,
            statement.raw,
            3,
            i64::from(schedule.local_hour),
        )?;
        bind_i64(
            self.database,
            statement.raw,
            4,
            i64::from(schedule.local_minute),
        )?;
        bind_i64(
            self.database,
            statement.raw,
            5,
            schedule.weekday.map_or(-1, i64::from),
        )?;
        bind_i64(self.database, statement.raw, 6, schedule.updated_at_unix_ms)?;
        bind_text(self.database, statement.raw, 7, &updated_by)?;
        expect_done(self.database, statement.raw)?;
        self.read_self_test_schedule(schedule.kind)?
            .ok_or_else(|| "stored self-test schedule disappeared".into())
    }

    pub fn read_self_test_schedule(
        &self,
        kind: SelfTestKind,
    ) -> Result<Option<SelfTestSchedule>, String> {
        let statement = self.prepare(
            "SELECT kind, enabled, local_hour, local_minute, weekday, updated_at_unix_ms, updated_by \
             FROM self_test_schedules WHERE kind=?1",
        )?;
        let kind = c_string(self_test_kind_name(kind), "self-test schedule kind")?;
        bind_text(self.database, statement.raw, 1, &kind)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_self_test_schedule_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_self_test_schedules(&self) -> Result<Vec<SelfTestSchedule>, String> {
        let statement = self.prepare(
            "SELECT kind, enabled, local_hour, local_minute, weekday, updated_at_unix_ms, updated_by \
             FROM self_test_schedules ORDER BY CASE kind WHEN 'daily' THEN 0 ELSE 1 END",
        )?;
        let mut schedules = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => schedules.push(read_self_test_schedule_row(statement.raw)?),
                SQLITE_DONE => return Ok(schedules),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn record_system_self_test(
        &self,
        record: &SystemSelfTest,
    ) -> Result<SystemSelfTest, String> {
        record.validate().map_err(str::to_string)?;
        let checks_json = serde_json::to_string(&record.checks)
            .map_err(|error| format!("could not encode self-test checks: {error}"))?;
        let statement = self.prepare(
            "INSERT INTO system_self_tests(self_test_id, kind, trigger_kind, schedule_period_key, \
             started_at_unix_ms, finished_at_unix_ms, status, checks_json, summary, evidence_path) \
             VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
        )?;
        let values = [
            c_string(&record.self_test_id, "self-test identity")?,
            c_string(self_test_kind_name(record.kind), "self-test kind")?,
            c_string(self_test_trigger_name(record.trigger), "self-test trigger")?,
            c_string(
                record.schedule_period_key.as_deref().unwrap_or_default(),
                "self-test schedule period",
            )?,
            c_string(self_test_status_name(record.status), "self-test status")?,
            c_string(&checks_json, "self-test checks")?,
            c_string(&record.summary, "self-test summary")?,
            c_string(
                record.evidence_path.as_deref().unwrap_or_default(),
                "self-test evidence path",
            )?,
        ];
        bind_text(self.database, statement.raw, 1, &values[0])?;
        bind_text(self.database, statement.raw, 2, &values[1])?;
        bind_text(self.database, statement.raw, 3, &values[2])?;
        bind_text(self.database, statement.raw, 4, &values[3])?;
        bind_i64(self.database, statement.raw, 5, record.started_at_unix_ms)?;
        bind_i64(self.database, statement.raw, 6, record.finished_at_unix_ms)?;
        bind_text(self.database, statement.raw, 7, &values[4])?;
        bind_text(self.database, statement.raw, 8, &values[5])?;
        bind_text(self.database, statement.raw, 9, &values[6])?;
        bind_text(self.database, statement.raw, 10, &values[7])?;
        expect_done(self.database, statement.raw)?;
        self.execute(
            "DELETE FROM system_self_tests WHERE self_test_id NOT IN (\
               SELECT self_test_id FROM system_self_tests ORDER BY started_at_unix_ms DESC, \
               self_test_id DESC LIMIT 400)",
        )?;
        self.read_system_self_test(&record.self_test_id)?
            .ok_or_else(|| "stored self-test record disappeared".into())
    }

    pub fn list_system_self_tests(&self, limit: u32) -> Result<Vec<SystemSelfTest>, String> {
        if !(1..=1_000).contains(&limit) {
            return Err("self-test history limit must be between 1 and 1000".into());
        }
        let statement = self.prepare(
            "SELECT self_test_id, kind, trigger_kind, schedule_period_key, started_at_unix_ms, \
             finished_at_unix_ms, status, checks_json, summary, evidence_path \
             FROM system_self_tests ORDER BY started_at_unix_ms DESC, self_test_id DESC LIMIT ?1",
        )?;
        bind_i64(self.database, statement.raw, 1, i64::from(limit))?;
        let mut records = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => records.push(read_system_self_test_row(statement.raw)?),
                SQLITE_DONE => return Ok(records),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn write_update_check(
        &self,
        record: &UpdateCheckRecord,
    ) -> Result<UpdateCheckRecord, String> {
        record.validate().map_err(str::to_string)?;
        let record_json = serde_json::to_string(record)
            .map_err(|error| format!("could not encode update-check record: {error}"))?;
        let statement = self.prepare(
            "INSERT INTO update_check_status(singleton, checked_at_unix_ms, \
             next_automatic_check_at_unix_ms, trigger_kind, outcome, record_json) \
             VALUES(1, ?1, ?2, ?3, ?4, ?5) \
             ON CONFLICT(singleton) DO UPDATE SET \
               checked_at_unix_ms=excluded.checked_at_unix_ms, \
               next_automatic_check_at_unix_ms=excluded.next_automatic_check_at_unix_ms, \
               trigger_kind=excluded.trigger_kind, outcome=excluded.outcome, \
               record_json=excluded.record_json \
             WHERE update_check_status.checked_at_unix_ms <= excluded.checked_at_unix_ms",
        )?;
        let trigger = c_string(
            update_check_trigger_name(record.trigger),
            "update-check trigger",
        )?;
        let outcome = c_string(
            update_check_outcome_name(record.outcome),
            "update-check outcome",
        )?;
        let record_json = c_string(&record_json, "update-check record")?;
        bind_i64(self.database, statement.raw, 1, record.checked_at_unix_ms)?;
        bind_i64(
            self.database,
            statement.raw,
            2,
            record.next_automatic_check_at_unix_ms,
        )?;
        bind_text(self.database, statement.raw, 3, &trigger)?;
        bind_text(self.database, statement.raw, 4, &outcome)?;
        bind_text(self.database, statement.raw, 5, &record_json)?;
        expect_done(self.database, statement.raw)?;
        let stored = self
            .read_update_check()?
            .ok_or_else(|| "stored update-check record disappeared".to_string())?;
        if stored != *record {
            return Err("a newer update-check record already exists".into());
        }
        Ok(stored)
    }

    pub fn read_update_check(&self) -> Result<Option<UpdateCheckRecord>, String> {
        let statement =
            self.prepare("SELECT record_json FROM update_check_status WHERE singleton=1")?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let json = column_text(statement.raw, 0)?;
                let record: UpdateCheckRecord = serde_json::from_str(&json)
                    .map_err(|error| format!("stored update-check record is invalid: {error}"))?;
                record.validate().map_err(str::to_string)?;
                Ok(Some(record))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn clear_update_check(&self) -> Result<(), String> {
        let statement = self.prepare("DELETE FROM update_check_status WHERE singleton=1")?;
        expect_done(self.database, statement.raw)
    }

    pub fn write_staged_package(
        &self,
        record: &StagedPackageRecord,
    ) -> Result<StagedPackageRecord, String> {
        record.validate().map_err(str::to_string)?;
        let record_json = serde_json::to_string(record)
            .map_err(|error| format!("could not encode staged-package record: {error}"))?;
        let statement = self.prepare(
            "INSERT INTO update_stage_status(singleton, staged_at_unix_ms, version, \
             source_commit, record_json) VALUES(1, ?1, ?2, ?3, ?4) \
             ON CONFLICT(singleton) DO UPDATE SET \
               staged_at_unix_ms=excluded.staged_at_unix_ms, version=excluded.version, \
               source_commit=excluded.source_commit, record_json=excluded.record_json \
             WHERE update_stage_status.staged_at_unix_ms <= excluded.staged_at_unix_ms",
        )?;
        let version = c_string(&record.manifest.version, "staged-package version")?;
        let commit = c_string(
            &record.manifest.source_commit,
            "staged-package source commit",
        )?;
        let record_json = c_string(&record_json, "staged-package record")?;
        bind_i64(self.database, statement.raw, 1, record.staged_at_unix_ms)?;
        bind_text(self.database, statement.raw, 2, &version)?;
        bind_text(self.database, statement.raw, 3, &commit)?;
        bind_text(self.database, statement.raw, 4, &record_json)?;
        expect_done(self.database, statement.raw)?;
        let stored = self
            .read_staged_package()?
            .ok_or_else(|| "stored staged-package record disappeared".to_string())?;
        if stored != *record {
            return Err("a newer staged-package record already exists".into());
        }
        Ok(stored)
    }

    pub fn read_staged_package(&self) -> Result<Option<StagedPackageRecord>, String> {
        let statement =
            self.prepare("SELECT record_json FROM update_stage_status WHERE singleton=1")?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let json = column_text(statement.raw, 0)?;
                let record: StagedPackageRecord = serde_json::from_str(&json)
                    .map_err(|error| format!("stored staged-package record is invalid: {error}"))?;
                record.validate().map_err(str::to_string)?;
                Ok(Some(record))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn clear_staged_package(&self) -> Result<(), String> {
        let statement = self.prepare("DELETE FROM update_stage_status WHERE singleton=1")?;
        expect_done(self.database, statement.raw)
    }

    pub fn write_prepared_slot(
        &self,
        record: &PreparedSlotRecord,
    ) -> Result<PreparedSlotRecord, String> {
        record.validate().map_err(str::to_string)?;
        let record_json = serde_json::to_string(record)
            .map_err(|error| format!("could not encode prepared-slot record: {error}"))?;
        let statement = self.prepare(
            "INSERT INTO update_prepare_status(singleton, prepared_at_unix_ms, version, \
             source_commit, record_json) VALUES(1, ?1, ?2, ?3, ?4) \
             ON CONFLICT(singleton) DO UPDATE SET \
               prepared_at_unix_ms=excluded.prepared_at_unix_ms, version=excluded.version, \
               source_commit=excluded.source_commit, record_json=excluded.record_json \
             WHERE update_prepare_status.prepared_at_unix_ms <= excluded.prepared_at_unix_ms",
        )?;
        let version = c_string(&record.manifest.version, "prepared-slot version")?;
        let commit = c_string(
            &record.manifest.source_commit,
            "prepared-slot source commit",
        )?;
        let record_json = c_string(&record_json, "prepared-slot record")?;
        bind_i64(self.database, statement.raw, 1, record.prepared_at_unix_ms)?;
        bind_text(self.database, statement.raw, 2, &version)?;
        bind_text(self.database, statement.raw, 3, &commit)?;
        bind_text(self.database, statement.raw, 4, &record_json)?;
        expect_done(self.database, statement.raw)?;
        let stored = self
            .read_prepared_slot()?
            .ok_or_else(|| "stored prepared-slot record disappeared".to_string())?;
        if stored != *record {
            return Err("a newer prepared-slot record already exists".into());
        }
        Ok(stored)
    }

    pub fn read_prepared_slot(&self) -> Result<Option<PreparedSlotRecord>, String> {
        let statement =
            self.prepare("SELECT record_json FROM update_prepare_status WHERE singleton=1")?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let json = column_text(statement.raw, 0)?;
                let record: PreparedSlotRecord = serde_json::from_str(&json)
                    .map_err(|error| format!("stored prepared-slot record is invalid: {error}"))?;
                record.validate().map_err(str::to_string)?;
                Ok(Some(record))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn clear_prepared_slot(&self) -> Result<(), String> {
        let statement = self.prepare("DELETE FROM update_prepare_status WHERE singleton=1")?;
        expect_done(self.database, statement.raw)
    }

    pub fn self_test_exists_for_period(
        &self,
        kind: SelfTestKind,
        period_key: &str,
    ) -> Result<bool, String> {
        if period_key.trim().is_empty() || period_key.len() > 40 {
            return Err("self-test period identity is invalid".into());
        }
        let statement = self.prepare(
            "SELECT 1 FROM system_self_tests WHERE kind=?1 AND trigger_kind='scheduled' \
             AND schedule_period_key=?2 LIMIT 1",
        )?;
        let kind = c_string(self_test_kind_name(kind), "self-test kind")?;
        let period_key = c_string(period_key, "self-test period identity")?;
        bind_text(self.database, statement.raw, 1, &kind)?;
        bind_text(self.database, statement.raw, 2, &period_key)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(true),
            SQLITE_DONE => Ok(false),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn database_integrity_check(&self) -> Result<bool, String> {
        let statement = self.prepare("PRAGMA quick_check")?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(column_text(statement.raw, 0)? == "ok"),
            SQLITE_DONE => Ok(false),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn prove_database_restore_round_trip(&self, target: &Path) -> Result<bool, String> {
        if target.exists() {
            return Err("database restore rehearsal target already exists".into());
        }
        if let Some(parent) = target.parent() {
            std::fs::create_dir_all(parent).map_err(|error| {
                format!("could not create restore rehearsal directory: {error}")
            })?;
        }
        let statement = self.prepare("VACUUM INTO ?1")?;
        let target_text = c_string(
            target.to_string_lossy().as_ref(),
            "database restore rehearsal path",
        )?;
        bind_text(self.database, statement.raw, 1, &target_text)?;
        let outcome = (|| {
            expect_done(self.database, statement.raw)?;
            let restored = StationStore::open(target)?;
            let healthy = restored.database_integrity_check()?;
            drop(restored);
            Ok(healthy)
        })();
        let mut cleanup_error = None;
        for candidate in [
            target.to_path_buf(),
            PathBuf::from(format!("{}-wal", target.display())),
            PathBuf::from(format!("{}-shm", target.display())),
        ] {
            if candidate.exists()
                && let Err(error) = std::fs::remove_file(&candidate)
            {
                cleanup_error = Some(format!(
                    "could not remove restore rehearsal artifact {}: {error}",
                    candidate.display()
                ));
                break;
            }
        }
        match (outcome, cleanup_error) {
            (Ok(healthy), None) => Ok(healthy),
            (Err(error), _) | (_, Some(error)) => Err(error),
        }
    }

    fn read_system_self_test(&self, self_test_id: &str) -> Result<Option<SystemSelfTest>, String> {
        let statement = self.prepare(
            "SELECT self_test_id, kind, trigger_kind, schedule_period_key, started_at_unix_ms, \
             finished_at_unix_ms, status, checks_json, summary, evidence_path \
             FROM system_self_tests WHERE self_test_id=?1",
        )?;
        let self_test_id = c_string(self_test_id, "self-test identity")?;
        bind_text(self.database, statement.raw, 1, &self_test_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_system_self_test_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn put_alert_rule(&self, rule: &AlertRule) -> Result<AlertRule, String> {
        rule.validate().map_err(str::to_string)?;
        for channel_id in &rule.channel_ids {
            if self.read_alert_channel(channel_id)?.is_none() {
                return Err(format!(
                    "alert rule references missing destination {channel_id}"
                ));
            }
        }
        if let Some(existing) = self.read_alert_rule(&rule.rule_id)? {
            if existing.condition != rule.condition {
                return Err("an alert rule condition cannot be changed".into());
            }
            if rule.updated_at_unix_ms < existing.updated_at_unix_ms {
                return Err("alert rule update time moved backward".into());
            }
        }
        let channel_ids_json = serde_json::to_string(&rule.channel_ids)
            .map_err(|error| format!("could not encode alert rule destinations: {error}"))?;
        let statement = self.prepare(
            "INSERT INTO alert_rules(rule_id, condition, enabled, severity, channel_ids_json, \
             dedupe_window_seconds, re_alert_after_seconds, scope_channel_id, notify_on_resolve, \
             updated_at_unix_ms, updated_by) VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11) \
             ON CONFLICT(rule_id) DO UPDATE SET enabled=excluded.enabled, severity=excluded.severity, \
             channel_ids_json=excluded.channel_ids_json, dedupe_window_seconds=excluded.dedupe_window_seconds, \
             re_alert_after_seconds=excluded.re_alert_after_seconds, scope_channel_id=excluded.scope_channel_id, \
             notify_on_resolve=excluded.notify_on_resolve, updated_at_unix_ms=excluded.updated_at_unix_ms, \
             updated_by=excluded.updated_by",
        )?;
        let values = [
            c_string(&rule.rule_id, "alert rule identity")?,
            c_string(alert_condition_name(rule.condition), "alert rule condition")?,
            c_string(alert_severity_name(rule.severity), "alert rule severity")?,
            c_string(&channel_ids_json, "alert rule destinations")?,
            c_string(
                rule.scope_channel_id.as_deref().unwrap_or_default(),
                "alert rule scope",
            )?,
            c_string(&rule.updated_by, "alert rule operator")?,
        ];
        bind_text(self.database, statement.raw, 1, &values[0])?;
        bind_text(self.database, statement.raw, 2, &values[1])?;
        bind_i64(self.database, statement.raw, 3, i64::from(rule.enabled))?;
        bind_text(self.database, statement.raw, 4, &values[2])?;
        bind_text(self.database, statement.raw, 5, &values[3])?;
        bind_i64(
            self.database,
            statement.raw,
            6,
            i64::from(rule.dedupe_window_seconds),
        )?;
        bind_i64(
            self.database,
            statement.raw,
            7,
            i64::from(rule.re_alert_after_seconds),
        )?;
        bind_text(self.database, statement.raw, 8, &values[4])?;
        bind_i64(
            self.database,
            statement.raw,
            9,
            i64::from(rule.notify_on_resolve),
        )?;
        bind_i64(self.database, statement.raw, 10, rule.updated_at_unix_ms)?;
        bind_text(self.database, statement.raw, 11, &values[5])?;
        expect_done(self.database, statement.raw)?;
        self.read_alert_rule(&rule.rule_id)?
            .ok_or_else(|| "stored alert rule disappeared".into())
    }

    pub fn read_alert_rule(&self, rule_id: &str) -> Result<Option<AlertRule>, String> {
        let statement = self.prepare(
            "SELECT rule_id, condition, enabled, severity, channel_ids_json, dedupe_window_seconds, \
             re_alert_after_seconds, scope_channel_id, notify_on_resolve, updated_at_unix_ms, updated_by \
             FROM alert_rules WHERE rule_id=?1",
        )?;
        let rule_id = c_string(rule_id, "alert rule identity")?;
        bind_text(self.database, statement.raw, 1, &rule_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_alert_rule_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_alert_rules(&self) -> Result<Vec<AlertRule>, String> {
        let statement = self.prepare(
            "SELECT rule_id, condition, enabled, severity, channel_ids_json, dedupe_window_seconds, \
             re_alert_after_seconds, scope_channel_id, notify_on_resolve, updated_at_unix_ms, updated_by \
             FROM alert_rules ORDER BY condition, rule_id",
        )?;
        let mut rules = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => rules.push(read_alert_rule_row(statement.raw)?),
                SQLITE_DONE => return Ok(rules),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    fn alert_rule_for_condition(
        &self,
        condition: AlertConditionKind,
    ) -> Result<Option<AlertRule>, String> {
        let statement = self.prepare(
            "SELECT rule_id, condition, enabled, severity, channel_ids_json, dedupe_window_seconds, \
             re_alert_after_seconds, scope_channel_id, notify_on_resolve, updated_at_unix_ms, updated_by \
             FROM alert_rules WHERE condition=?1",
        )?;
        let condition = c_string(alert_condition_name(condition), "alert rule condition")?;
        bind_text(self.database, statement.raw, 1, &condition)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_alert_rule_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn put_alert_channel(
        &self,
        channel: &AlertChannel,
        secret: &AlertChannelSecret,
    ) -> Result<AlertChannel, String> {
        channel.validate().map_err(str::to_string)?;
        secret.validate(channel.kind)?;
        let redacted =
            redact_alert_channel_target(channel.kind, &secret.target).map_err(str::to_string)?;
        if channel.target_redacted != redacted || !channel.credential_configured {
            return Err("alert channel redaction or credential state is inconsistent".into());
        }
        let plaintext = serde_json::to_vec(&serde_json::json!({
            "target": secret.target,
            "endpoint": secret.endpoint,
            "sender": secret.sender,
            "identity": secret.identity,
            "credential": secret.credential,
        }))
        .map_err(|error| format!("could not encode alert destination secret: {error}"))?;
        let encrypted = protect_local_machine(&plaintext)?;
        let statement = self.prepare(
            "INSERT INTO alert_channels(channel_id, kind, label, enabled, target_redacted, \
             credential_configured, quiet_hours_start_utc, quiet_hours_end_utc, encrypted_configuration, \
             created_at_unix_ms, updated_at_unix_ms) VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11) \
             ON CONFLICT(channel_id) DO UPDATE SET kind=excluded.kind, label=excluded.label, \
             enabled=excluded.enabled, target_redacted=excluded.target_redacted, \
             credential_configured=excluded.credential_configured, \
             quiet_hours_start_utc=excluded.quiet_hours_start_utc, \
             quiet_hours_end_utc=excluded.quiet_hours_end_utc, \
             encrypted_configuration=excluded.encrypted_configuration, \
             updated_at_unix_ms=excluded.updated_at_unix_ms",
        )?;
        let values = [
            c_string(&channel.channel_id, "alert channel identity")?,
            c_string(alert_channel_kind_name(channel.kind), "alert channel kind")?,
            c_string(&channel.label, "alert channel label")?,
            c_string(&channel.target_redacted, "redacted alert target")?,
            c_string(
                channel.quiet_hours_start_utc.as_deref().unwrap_or_default(),
                "quiet-hours start",
            )?,
            c_string(
                channel.quiet_hours_end_utc.as_deref().unwrap_or_default(),
                "quiet-hours end",
            )?,
        ];
        bind_text(self.database, statement.raw, 1, &values[0])?;
        bind_text(self.database, statement.raw, 2, &values[1])?;
        bind_text(self.database, statement.raw, 3, &values[2])?;
        bind_i64(self.database, statement.raw, 4, i64::from(channel.enabled))?;
        bind_text(self.database, statement.raw, 5, &values[3])?;
        bind_i64(
            self.database,
            statement.raw,
            6,
            i64::from(channel.credential_configured),
        )?;
        bind_text(self.database, statement.raw, 7, &values[4])?;
        bind_text(self.database, statement.raw, 8, &values[5])?;
        bind_blob(self.database, statement.raw, 9, &encrypted)?;
        bind_i64(self.database, statement.raw, 10, channel.created_at_unix_ms)?;
        bind_i64(self.database, statement.raw, 11, channel.updated_at_unix_ms)?;
        expect_done(self.database, statement.raw)?;
        self.read_alert_channel(&channel.channel_id)?
            .ok_or_else(|| "stored alert channel disappeared".into())
    }

    pub fn read_alert_channel(&self, channel_id: &str) -> Result<Option<AlertChannel>, String> {
        let statement = self.prepare(
            "SELECT channel_id, kind, label, enabled, target_redacted, credential_configured, \
             quiet_hours_start_utc, quiet_hours_end_utc, created_at_unix_ms, updated_at_unix_ms \
             FROM alert_channels WHERE channel_id=?1",
        )?;
        let channel_id = c_string(channel_id, "alert channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_alert_channel_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_alert_channels(&self) -> Result<Vec<AlertChannel>, String> {
        let statement = self.prepare(
            "SELECT channel_id, kind, label, enabled, target_redacted, credential_configured, \
             quiet_hours_start_utc, quiet_hours_end_utc, created_at_unix_ms, updated_at_unix_ms \
             FROM alert_channels ORDER BY label, channel_id",
        )?;
        let mut channels = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => channels.push(read_alert_channel_row(statement.raw)?),
                SQLITE_DONE => return Ok(channels),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn read_alert_channel_secret(
        &self,
        channel_id: &str,
    ) -> Result<Option<AlertChannelSecret>, String> {
        let statement = self.prepare(
            "SELECT kind, encrypted_configuration FROM alert_channels WHERE channel_id=?1",
        )?;
        let channel_id = c_string(channel_id, "alert channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {}
            SQLITE_DONE => return Ok(None),
            _ => return Err(sqlite_error(self.database)),
        }
        let kind = parse_alert_channel_kind(&column_text(statement.raw, 0)?)?;
        let decrypted = unprotect_local_machine(&column_blob(statement.raw, 1)?)?;
        let value: serde_json::Value = serde_json::from_slice(&decrypted)
            .map_err(|error| format!("stored alert destination secret is invalid: {error}"))?;
        let optional = |name: &str| {
            value
                .get(name)
                .and_then(serde_json::Value::as_str)
                .map(str::to_string)
        };
        let secret = AlertChannelSecret {
            target: optional("target").ok_or("stored alert target is missing")?,
            endpoint: optional("endpoint"),
            sender: optional("sender"),
            identity: optional("identity"),
            credential: optional("credential").ok_or("stored alert credential is missing")?,
        };
        secret.validate(kind)?;
        Ok(Some(secret))
    }

    pub fn enqueue_alert_delivery(
        &self,
        delivery_id: &str,
        event: &AlertEvent,
        alert_channel_id: &str,
        notification_kind: AlertNotificationKind,
        not_before_unix_ms: i64,
        created_at_unix_ms: i64,
    ) -> Result<AlertDelivery, String> {
        event.validate().map_err(str::to_string)?;
        if self.read_alert_event(&event.event_id)?.as_ref() != Some(event) {
            return Err("alert delivery event is not the current durable event".into());
        }
        let channel = self
            .read_alert_channel(alert_channel_id)?
            .ok_or_else(|| "alert delivery channel does not exist".to_string())?;
        if !channel.enabled {
            return Err("alert delivery channel is disabled".into());
        }
        let payload =
            AlertDeliveryPayload::from_event(notification_kind, event).map_err(str::to_string)?;
        let delivery = AlertDelivery {
            delivery_id: delivery_id.into(),
            event_id: event.event_id.clone(),
            alert_channel_id: alert_channel_id.into(),
            notification_kind,
            state: AlertDeliveryState::Pending,
            attempts: 0,
            next_attempt_at_unix_ms: not_before_unix_ms,
            lease_expires_at_unix_ms: None,
            last_error: String::new(),
            signature: None,
            created_at_unix_ms,
            updated_at_unix_ms: created_at_unix_ms,
            sent_at_unix_ms: None,
        };
        delivery.validate().map_err(str::to_string)?;
        self.insert_alert_delivery(&delivery, &payload)?;
        self.read_alert_delivery(delivery_id)?
            .ok_or_else(|| "queued alert delivery disappeared".into())
    }

    pub fn enqueue_alert_channel_test(
        &self,
        event_id: &str,
        delivery_id: &str,
        alert_channel_id: &str,
        created_at_unix_ms: i64,
    ) -> Result<AlertDelivery, String> {
        let channel = self
            .read_alert_channel(alert_channel_id)?
            .ok_or_else(|| "alert test channel does not exist".to_string())?;
        let event = AlertEvent {
            event_id: event_id.into(),
            condition: AlertConditionKind::ServiceDown,
            severity: AlertSeverity::Info,
            state: AlertEventState::Resolved,
            resource_ref: alert_channel_id.into(),
            summary: "Operator requested an alert-channel test.".into(),
            detail: format!(
                "Test delivery for the configured {} destination.",
                alert_channel_kind_name(channel.kind)
            ),
            source_section: "S8".into(),
            first_observed_at_unix_ms: created_at_unix_ms,
            last_observed_at_unix_ms: created_at_unix_ms,
            resolved_at_unix_ms: Some(created_at_unix_ms),
            occurrence_count: 1,
            acknowledged_at_unix_ms: None,
            acknowledged_by: None,
        };
        event.validate().map_err(str::to_string)?;
        let payload = AlertDeliveryPayload::from_event(AlertNotificationKind::Test, &event)
            .map_err(str::to_string)?;
        let delivery = AlertDelivery {
            delivery_id: delivery_id.into(),
            event_id: event_id.into(),
            alert_channel_id: alert_channel_id.into(),
            notification_kind: AlertNotificationKind::Test,
            state: AlertDeliveryState::Pending,
            attempts: 0,
            next_attempt_at_unix_ms: created_at_unix_ms,
            lease_expires_at_unix_ms: None,
            last_error: String::new(),
            signature: None,
            created_at_unix_ms,
            updated_at_unix_ms: created_at_unix_ms,
            sent_at_unix_ms: None,
        };
        delivery.validate().map_err(str::to_string)?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            self.insert_alert(&event)?;
            self.insert_alert_delivery(&delivery, &payload)
        })();
        self.finish_transaction(outcome)?;
        self.read_alert_delivery(delivery_id)?
            .ok_or_else(|| "queued alert-channel test disappeared".into())
    }

    fn insert_alert_delivery(
        &self,
        delivery: &AlertDelivery,
        payload: &AlertDeliveryPayload,
    ) -> Result<(), String> {
        let payload_json = serde_json::to_string(&payload)
            .map_err(|error| format!("could not encode alert delivery payload: {error}"))?;
        let statement = self.prepare(
            "INSERT INTO alert_deliveries(delivery_id, event_id, alert_channel_id, notification_kind, \
             state, attempts, next_attempt_at_unix_ms, lease_expires_at_unix_ms, last_error, \
             signature, payload_json, created_at_unix_ms, updated_at_unix_ms, sent_at_unix_ms) \
             VALUES(?1, ?2, ?3, ?4, 'pending', 0, ?5, -1, '', '', ?6, ?7, ?7, -1)",
        )?;
        let values = [
            c_string(&delivery.delivery_id, "alert delivery identity")?,
            c_string(&delivery.event_id, "alert event identity")?,
            c_string(&delivery.alert_channel_id, "alert channel identity")?,
            c_string(
                alert_notification_kind_name(delivery.notification_kind),
                "alert notification kind",
            )?,
            c_string(&payload_json, "alert delivery payload")?,
        ];
        bind_text(self.database, statement.raw, 1, &values[0])?;
        bind_text(self.database, statement.raw, 2, &values[1])?;
        bind_text(self.database, statement.raw, 3, &values[2])?;
        bind_text(self.database, statement.raw, 4, &values[3])?;
        bind_i64(
            self.database,
            statement.raw,
            5,
            delivery.next_attempt_at_unix_ms,
        )?;
        bind_text(self.database, statement.raw, 6, &values[4])?;
        bind_i64(self.database, statement.raw, 7, delivery.created_at_unix_ms)?;
        expect_done(self.database, statement.raw)
    }

    pub fn read_alert_delivery(&self, delivery_id: &str) -> Result<Option<AlertDelivery>, String> {
        let statement = self.prepare(
            "SELECT delivery_id, event_id, alert_channel_id, notification_kind, state, attempts, \
             next_attempt_at_unix_ms, lease_expires_at_unix_ms, last_error, signature, \
             created_at_unix_ms, updated_at_unix_ms, sent_at_unix_ms \
             FROM alert_deliveries WHERE delivery_id=?1",
        )?;
        let delivery_id = c_string(delivery_id, "alert delivery identity")?;
        bind_text(self.database, statement.raw, 1, &delivery_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_alert_delivery_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_alert_deliveries(&self) -> Result<Vec<AlertDelivery>, String> {
        let statement = self.prepare(
            "SELECT delivery_id, event_id, alert_channel_id, notification_kind, state, attempts, \
             next_attempt_at_unix_ms, lease_expires_at_unix_ms, last_error, signature, \
             created_at_unix_ms, updated_at_unix_ms, sent_at_unix_ms \
             FROM alert_deliveries ORDER BY created_at_unix_ms, delivery_id",
        )?;
        let mut deliveries = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => deliveries.push(read_alert_delivery_row(statement.raw)?),
                SQLITE_DONE => return Ok(deliveries),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn list_recent_alert_deliveries(&self, limit: u32) -> Result<Vec<AlertDelivery>, String> {
        if !(1..=1_000).contains(&limit) {
            return Err("alert delivery limit must be between 1 and 1000".into());
        }
        let statement = self.prepare(
            "SELECT delivery_id, event_id, alert_channel_id, notification_kind, state, attempts, \
             next_attempt_at_unix_ms, lease_expires_at_unix_ms, last_error, signature, \
             created_at_unix_ms, updated_at_unix_ms, sent_at_unix_ms \
             FROM alert_deliveries ORDER BY created_at_unix_ms DESC, delivery_id DESC LIMIT ?1",
        )?;
        bind_i64(self.database, statement.raw, 1, i64::from(limit))?;
        let mut deliveries = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => deliveries.push(read_alert_delivery_row(statement.raw)?),
                SQLITE_DONE => return Ok(deliveries),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn read_alert_delivery_payload(
        &self,
        delivery_id: &str,
    ) -> Result<Option<AlertDeliveryPayload>, String> {
        let statement =
            self.prepare("SELECT payload_json FROM alert_deliveries WHERE delivery_id=?1")?;
        let delivery_id = c_string(delivery_id, "alert delivery identity")?;
        bind_text(self.database, statement.raw, 1, &delivery_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let payload: AlertDeliveryPayload =
                    serde_json::from_str(&column_text(statement.raw, 0)?).map_err(|error| {
                        format!("stored alert delivery payload is invalid: {error}")
                    })?;
                payload.validate().map_err(str::to_string)?;
                Ok(Some(payload))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn claim_due_alert_delivery(
        &self,
        now_unix_ms: i64,
        lease_duration_ms: i64,
    ) -> Result<Option<AlertDelivery>, String> {
        if now_unix_ms < 0 || !(1_000..=300_000).contains(&lease_duration_ms) {
            return Err("alert delivery lease request is invalid".into());
        }
        let lease_expires_at_unix_ms = now_unix_ms
            .checked_add(lease_duration_ms)
            .ok_or_else(|| "alert delivery lease time overflowed".to_string())?;
        self.execute("BEGIN IMMEDIATE")?;
        let mut claimed = None;
        let outcome = (|| {
            let recovery = self.prepare(
                "UPDATE alert_deliveries SET state='pending', lease_expires_at_unix_ms=-1, \
                 last_error=CASE WHEN last_error='' THEN 'delivery lease expired before completion' \
                 ELSE substr(last_error || '; delivery lease expired before completion', 1, 1000) END, \
                 updated_at_unix_ms=?1 WHERE state='leased' AND lease_expires_at_unix_ms<=?1",
            )?;
            bind_i64(self.database, recovery.raw, 1, now_unix_ms)?;
            expect_done(self.database, recovery.raw)?;

            let due = self.prepare(
                "SELECT delivery_id FROM alert_deliveries WHERE state='pending' \
                 AND next_attempt_at_unix_ms<=?1 ORDER BY next_attempt_at_unix_ms, created_at_unix_ms, \
                 delivery_id LIMIT 1",
            )?;
            bind_i64(self.database, due.raw, 1, now_unix_ms)?;
            if unsafe { sqlite3_step(due.raw) } == SQLITE_DONE {
                return Ok(());
            }
            let delivery_id = column_text(due.raw, 0)?;
            let update = self.prepare(
                "UPDATE alert_deliveries SET state='leased', attempts=attempts+1, \
                 lease_expires_at_unix_ms=?1, updated_at_unix_ms=?2 \
                 WHERE delivery_id=?3 AND state='pending' AND next_attempt_at_unix_ms<=?2",
            )?;
            let delivery_id_value = c_string(&delivery_id, "alert delivery identity")?;
            bind_i64(self.database, update.raw, 1, lease_expires_at_unix_ms)?;
            bind_i64(self.database, update.raw, 2, now_unix_ms)?;
            bind_text(self.database, update.raw, 3, &delivery_id_value)?;
            expect_done(self.database, update.raw)?;
            if self.changes() != 1 {
                return Err("due alert delivery could not be leased".into());
            }
            claimed = self.read_alert_delivery(&delivery_id)?;
            Ok(())
        })();
        self.finish_transaction(outcome)?;
        Ok(claimed)
    }

    pub fn complete_alert_delivery(
        &self,
        delivery_id: &str,
        sent_at_unix_ms: i64,
        signature: Option<&str>,
    ) -> Result<AlertDelivery, String> {
        if sent_at_unix_ms < 0
            || signature.is_some_and(|value| {
                value.is_empty() || value.len() > 200 || value.chars().any(char::is_control)
            })
        {
            return Err("alert delivery completion is invalid".into());
        }
        let statement = self.prepare(
            "UPDATE alert_deliveries SET state='sent', lease_expires_at_unix_ms=-1, signature=?1, \
             updated_at_unix_ms=?2, sent_at_unix_ms=?2 WHERE delivery_id=?3 AND state='leased' \
             AND updated_at_unix_ms<=?2",
        )?;
        let signature = c_string(signature.unwrap_or_default(), "alert delivery signature")?;
        let delivery_id = c_string(delivery_id, "alert delivery identity")?;
        bind_text(self.database, statement.raw, 1, &signature)?;
        bind_i64(self.database, statement.raw, 2, sent_at_unix_ms)?;
        bind_text(self.database, statement.raw, 3, &delivery_id)?;
        expect_done(self.database, statement.raw)?;
        if self.changes() != 1 {
            return Err("only a leased alert delivery can be completed".into());
        }
        self.read_alert_delivery(delivery_id.to_str().map_err(|_| "invalid alert identity")?)?
            .ok_or_else(|| "completed alert delivery disappeared".into())
    }

    pub fn fail_alert_delivery(
        &self,
        delivery_id: &str,
        failed_at_unix_ms: i64,
        error: &str,
        maximum_attempts: u32,
        base_backoff_ms: i64,
    ) -> Result<AlertDelivery, String> {
        if failed_at_unix_ms < 0
            || error.trim().is_empty()
            || error.len() > 1_000
            || error.chars().any(char::is_control)
            || !(1..=20).contains(&maximum_attempts)
            || !(1_000..=86_400_000).contains(&base_backoff_ms)
        {
            return Err("alert delivery failure is invalid".into());
        }
        let current = self
            .read_alert_delivery(delivery_id)?
            .ok_or_else(|| "alert delivery does not exist".to_string())?;
        if current.state != AlertDeliveryState::Leased
            || current.attempts == 0
            || failed_at_unix_ms < current.updated_at_unix_ms
        {
            return Err("only a current leased alert delivery can fail".into());
        }
        let dead_letter = current.attempts >= maximum_attempts;
        let exponent = current.attempts.saturating_sub(1).min(20);
        let backoff_ms = base_backoff_ms
            .saturating_mul(1_i64 << exponent)
            .min(86_400_000);
        let next_attempt_at_unix_ms = if dead_letter {
            current.next_attempt_at_unix_ms
        } else {
            failed_at_unix_ms
                .checked_add(backoff_ms)
                .ok_or_else(|| "alert delivery retry time overflowed".to_string())?
        };
        let statement = self.prepare(
            "UPDATE alert_deliveries SET state=?1, next_attempt_at_unix_ms=?2, \
             lease_expires_at_unix_ms=-1, last_error=?3, updated_at_unix_ms=?4 \
             WHERE delivery_id=?5 AND state='leased' AND attempts=?6",
        )?;
        let state = c_string(
            if dead_letter {
                "dead_letter"
            } else {
                "pending"
            },
            "alert delivery state",
        )?;
        let error = c_string(error, "alert delivery error")?;
        let delivery_id = c_string(delivery_id, "alert delivery identity")?;
        bind_text(self.database, statement.raw, 1, &state)?;
        bind_i64(self.database, statement.raw, 2, next_attempt_at_unix_ms)?;
        bind_text(self.database, statement.raw, 3, &error)?;
        bind_i64(self.database, statement.raw, 4, failed_at_unix_ms)?;
        bind_text(self.database, statement.raw, 5, &delivery_id)?;
        bind_i64(self.database, statement.raw, 6, i64::from(current.attempts))?;
        expect_done(self.database, statement.raw)?;
        if self.changes() != 1 {
            return Err("alert delivery lease changed before failure was recorded".into());
        }
        self.read_alert_delivery(delivery_id.to_str().map_err(|_| "invalid alert identity")?)?
            .ok_or_else(|| "failed alert delivery disappeared".into())
    }

    pub fn observe_alert(&self, proposed: &AlertEvent) -> Result<AlertEvent, String> {
        validate_new_alert_observation(proposed)?;
        self.execute("BEGIN IMMEDIATE")?;
        let mut recorded = None;
        let outcome = (|| {
            let (event, _) = self.observe_alert_locked(proposed)?;
            recorded = Some(event);
            Ok(())
        })();
        self.finish_transaction(outcome)?;
        recorded.ok_or_else(|| "alert transaction produced no event".into())
    }

    pub fn observe_alert_and_route(&self, proposed: &AlertEvent) -> Result<AlertEvent, String> {
        validate_new_alert_observation(proposed)?;
        self.execute("BEGIN IMMEDIATE")?;
        let mut recorded = None;
        let outcome = (|| {
            let rule = self
                .alert_rule_for_condition(proposed.condition)?
                .ok_or_else(|| "alert condition has no configured rule".to_string())?;
            if !rule.enabled
                || rule
                    .scope_channel_id
                    .as_deref()
                    .is_some_and(|scope| scope != proposed.resource_ref)
            {
                recorded = Some(proposed.clone());
                return Ok(());
            }
            let mut ruled = proposed.clone();
            ruled.severity = rule.severity;
            let (event, is_first_observation) = self.observe_alert_locked(&ruled)?;
            if is_first_observation {
                self.route_alert_locked(
                    &event,
                    &rule,
                    AlertNotificationKind::Firing,
                    event.first_observed_at_unix_ms,
                )?;
            } else if rule.re_alert_after_seconds > 0 {
                self.route_due_realerts_locked(&event, &rule, event.last_observed_at_unix_ms)?;
            }
            recorded = Some(event);
            Ok(())
        })();
        self.finish_transaction(outcome)?;
        recorded.ok_or_else(|| "alert routing transaction produced no event".into())
    }

    fn observe_alert_locked(&self, proposed: &AlertEvent) -> Result<(AlertEvent, bool), String> {
        if let Some(mut current) =
            self.read_firing_alert(proposed.condition, &proposed.resource_ref)?
        {
            if proposed.last_observed_at_unix_ms < current.last_observed_at_unix_ms {
                return Err("alert observation time moved backward".into());
            }
            let statement = self.prepare(
                "UPDATE alert_events SET severity=?1, summary=?2, detail=?3, source_section=?4, \
                 last_observed_at_unix_ms=?5, occurrence_count=occurrence_count+1 WHERE event_id=?6 AND state='firing'",
            )?;
            let severity = c_string(alert_severity_name(proposed.severity), "alert severity")?;
            let summary = c_string(&proposed.summary, "alert summary")?;
            let detail = c_string(&proposed.detail, "alert detail")?;
            let source = c_string(&proposed.source_section, "alert source")?;
            let event_id = c_string(&current.event_id, "alert identity")?;
            bind_text(self.database, statement.raw, 1, &severity)?;
            bind_text(self.database, statement.raw, 2, &summary)?;
            bind_text(self.database, statement.raw, 3, &detail)?;
            bind_text(self.database, statement.raw, 4, &source)?;
            bind_i64(
                self.database,
                statement.raw,
                5,
                proposed.last_observed_at_unix_ms,
            )?;
            bind_text(self.database, statement.raw, 6, &event_id)?;
            expect_done(self.database, statement.raw)?;
            current.severity = proposed.severity;
            current.summary.clone_from(&proposed.summary);
            current.detail.clone_from(&proposed.detail);
            current.source_section.clone_from(&proposed.source_section);
            current.last_observed_at_unix_ms = proposed.last_observed_at_unix_ms;
            current.occurrence_count = current
                .occurrence_count
                .checked_add(1)
                .ok_or_else(|| "alert occurrence count overflowed".to_string())?;
            return Ok((current, false));
        }
        self.insert_alert(proposed)?;
        Ok((proposed.clone(), true))
    }

    fn route_alert_locked(
        &self,
        event: &AlertEvent,
        rule: &AlertRule,
        notification_kind: AlertNotificationKind,
        observed_at_unix_ms: i64,
    ) -> Result<(), String> {
        for channel in self.list_alert_channels()?.into_iter().filter(|channel| {
            channel.enabled
                && (rule.channel_ids.is_empty() || rule.channel_ids.contains(&channel.channel_id))
        }) {
            let not_before_unix_ms =
                quiet_hours_release(&channel, event.severity, observed_at_unix_ms)?;
            let delivery_id = deterministic_alert_delivery_id(
                &event.event_id,
                &channel.channel_id,
                notification_kind,
                observed_at_unix_ms,
            );
            self.enqueue_alert_delivery(
                &delivery_id,
                event,
                &channel.channel_id,
                notification_kind,
                not_before_unix_ms,
                observed_at_unix_ms,
            )?;
        }
        Ok(())
    }

    fn route_due_realerts_locked(
        &self,
        event: &AlertEvent,
        rule: &AlertRule,
        observed_at_unix_ms: i64,
    ) -> Result<(), String> {
        let interval_ms = i64::from(rule.re_alert_after_seconds) * 1_000;
        for channel in self.list_alert_channels()?.into_iter().filter(|channel| {
            channel.enabled
                && (rule.channel_ids.is_empty() || rule.channel_ids.contains(&channel.channel_id))
        }) {
            if !self.realert_is_due(
                &event.event_id,
                &channel.channel_id,
                observed_at_unix_ms,
                interval_ms,
            )? {
                continue;
            }
            let not_before_unix_ms =
                quiet_hours_release(&channel, event.severity, observed_at_unix_ms)?;
            let delivery_id = deterministic_alert_delivery_id(
                &event.event_id,
                &channel.channel_id,
                AlertNotificationKind::Realert,
                observed_at_unix_ms,
            );
            self.enqueue_alert_delivery(
                &delivery_id,
                event,
                &channel.channel_id,
                AlertNotificationKind::Realert,
                not_before_unix_ms,
                observed_at_unix_ms,
            )?;
        }
        Ok(())
    }

    fn realert_is_due(
        &self,
        event_id: &str,
        alert_channel_id: &str,
        observed_at_unix_ms: i64,
        interval_ms: i64,
    ) -> Result<bool, String> {
        let statement = self.prepare(
            "SELECT state, sent_at_unix_ms, updated_at_unix_ms FROM alert_deliveries \
             WHERE event_id=?1 AND alert_channel_id=?2 AND notification_kind IN ('firing','realert') \
             ORDER BY created_at_unix_ms DESC, delivery_id DESC LIMIT 1",
        )?;
        let event_id = c_string(event_id, "alert event identity")?;
        let channel_id = c_string(alert_channel_id, "alert channel identity")?;
        bind_text(self.database, statement.raw, 1, &event_id)?;
        bind_text(self.database, statement.raw, 2, &channel_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let state = parse_alert_delivery_state(&column_text(statement.raw, 0)?)?;
                let sent_at = unsafe { sqlite3_column_int64(statement.raw, 1) };
                let updated_at = unsafe { sqlite3_column_int64(statement.raw, 2) };
                let anchor = match state {
                    AlertDeliveryState::Sent if sent_at >= 0 => Some(sent_at),
                    AlertDeliveryState::DeadLetter => Some(updated_at),
                    AlertDeliveryState::Pending | AlertDeliveryState::Leased => None,
                    AlertDeliveryState::Sent => None,
                };
                Ok(anchor.is_some_and(|anchor| {
                    observed_at_unix_ms.saturating_sub(anchor) >= interval_ms
                }))
            }
            SQLITE_DONE => Ok(false),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn acknowledge_alert(
        &self,
        event_id: &str,
        acknowledged_at_unix_ms: i64,
        acknowledged_by: &str,
    ) -> Result<Option<AlertEvent>, String> {
        if acknowledged_at_unix_ms < 0
            || acknowledged_by.trim().is_empty()
            || acknowledged_by.len() > 120
        {
            return Err("alert acknowledgment is invalid".into());
        }
        let statement = self.prepare(
            "UPDATE alert_events SET acknowledged_at_unix_ms=?1, acknowledged_by=?2 \
             WHERE event_id=?3 AND acknowledged_at_unix_ms=-1",
        )?;
        let operator = c_string(acknowledged_by, "alert operator")?;
        let event_id = c_string(event_id, "alert identity")?;
        bind_i64(self.database, statement.raw, 1, acknowledged_at_unix_ms)?;
        bind_text(self.database, statement.raw, 2, &operator)?;
        bind_text(self.database, statement.raw, 3, &event_id)?;
        expect_done(self.database, statement.raw)?;
        self.read_alert_event(event_id.to_str().map_err(|_| "invalid alert identity")?)
    }

    pub fn resolve_alert(
        &self,
        condition: AlertConditionKind,
        resource_ref: &str,
        resolved_at_unix_ms: i64,
    ) -> Result<Option<AlertEvent>, String> {
        if resolved_at_unix_ms < 0 {
            return Err("alert resolution time must be nonnegative".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let mut resolved = None;
        let outcome = (|| {
            let statement = self.prepare(
                "UPDATE alert_events SET state='resolved', resolved_at_unix_ms=?1 \
                 WHERE condition=?2 AND resource_ref=?3 AND state='firing' AND last_observed_at_unix_ms<=?1",
            )?;
            let condition = c_string(alert_condition_name(condition), "alert condition")?;
            let resource_ref = c_string(resource_ref, "alert resource")?;
            bind_i64(self.database, statement.raw, 1, resolved_at_unix_ms)?;
            bind_text(self.database, statement.raw, 2, &condition)?;
            bind_text(self.database, statement.raw, 3, &resource_ref)?;
            expect_done(self.database, statement.raw)?;
            if self.changes() == 0 {
                return Ok(());
            }
            let statement = self.prepare(
                "SELECT event_id, condition, severity, state, resource_ref, summary, detail, source_section, \
                 first_observed_at_unix_ms, last_observed_at_unix_ms, resolved_at_unix_ms, occurrence_count, \
                 acknowledged_at_unix_ms, acknowledged_by FROM alert_events \
                 WHERE condition=?1 AND resource_ref=?2 AND resolved_at_unix_ms=?3 ORDER BY rowid DESC LIMIT 1",
            )?;
            bind_text(self.database, statement.raw, 1, &condition)?;
            bind_text(self.database, statement.raw, 2, &resource_ref)?;
            bind_i64(self.database, statement.raw, 3, resolved_at_unix_ms)?;
            let event = match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => read_alert_event_row(statement.raw)?,
                _ => return Err("resolved alert disappeared".into()),
            };
            if let Some(rule) = self.alert_rule_for_condition(event.condition)?
                && rule.enabled
                && rule.notify_on_resolve
                && !rule
                    .scope_channel_id
                    .as_deref()
                    .is_some_and(|scope| scope != event.resource_ref)
            {
                self.route_alert_locked(
                    &event,
                    &rule,
                    AlertNotificationKind::Resolved,
                    resolved_at_unix_ms,
                )?;
            }
            resolved = Some(event);
            Ok(())
        })();
        self.finish_transaction(outcome)?;
        Ok(resolved)
    }

    pub fn read_alert_event(&self, event_id: &str) -> Result<Option<AlertEvent>, String> {
        let statement = self.prepare(
            "SELECT event_id, condition, severity, state, resource_ref, summary, detail, source_section, \
             first_observed_at_unix_ms, last_observed_at_unix_ms, resolved_at_unix_ms, occurrence_count, \
             acknowledged_at_unix_ms, acknowledged_by FROM alert_events WHERE event_id=?1",
        )?;
        let event_id = c_string(event_id, "alert identity")?;
        bind_text(self.database, statement.raw, 1, &event_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_alert_event_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_alert_events(&self) -> Result<Vec<AlertEvent>, String> {
        let statement = self.prepare(
            "SELECT event_id, condition, severity, state, resource_ref, summary, detail, source_section, \
             first_observed_at_unix_ms, last_observed_at_unix_ms, resolved_at_unix_ms, occurrence_count, \
             acknowledged_at_unix_ms, acknowledged_by FROM alert_events \
             ORDER BY CASE state WHEN 'firing' THEN 0 ELSE 1 END, last_observed_at_unix_ms DESC, event_id",
        )?;
        let mut events = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => events.push(read_alert_event_row(statement.raw)?),
                SQLITE_DONE => return Ok(events),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn list_recent_alert_events(&self, limit: u32) -> Result<Vec<AlertEvent>, String> {
        if !(1..=1_000).contains(&limit) {
            return Err("alert event limit must be between 1 and 1000".into());
        }
        let statement = self.prepare(
            "SELECT event_id, condition, severity, state, resource_ref, summary, detail, source_section, \
             first_observed_at_unix_ms, last_observed_at_unix_ms, resolved_at_unix_ms, occurrence_count, \
             acknowledged_at_unix_ms, acknowledged_by FROM alert_events \
             ORDER BY last_observed_at_unix_ms DESC, event_id DESC LIMIT ?1",
        )?;
        bind_i64(self.database, statement.raw, 1, i64::from(limit))?;
        let mut events = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => events.push(read_alert_event_row(statement.raw)?),
                SQLITE_DONE => return Ok(events),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    fn read_firing_alert(
        &self,
        condition: AlertConditionKind,
        resource_ref: &str,
    ) -> Result<Option<AlertEvent>, String> {
        let statement = self.prepare(
            "SELECT event_id, condition, severity, state, resource_ref, summary, detail, source_section, \
             first_observed_at_unix_ms, last_observed_at_unix_ms, resolved_at_unix_ms, occurrence_count, \
             acknowledged_at_unix_ms, acknowledged_by FROM alert_events \
             WHERE condition=?1 AND resource_ref=?2 AND state='firing'",
        )?;
        let condition = c_string(alert_condition_name(condition), "alert condition")?;
        let resource_ref = c_string(resource_ref, "alert resource")?;
        bind_text(self.database, statement.raw, 1, &condition)?;
        bind_text(self.database, statement.raw, 2, &resource_ref)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_alert_event_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn insert_alert(&self, event: &AlertEvent) -> Result<(), String> {
        let statement = self.prepare(
            "INSERT INTO alert_events(event_id, condition, severity, state, resource_ref, summary, \
             detail, source_section, first_observed_at_unix_ms, last_observed_at_unix_ms, \
             resolved_at_unix_ms, occurrence_count, acknowledged_at_unix_ms, acknowledged_by) \
             VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14)",
        )?;
        let values = [
            c_string(&event.event_id, "alert identity")?,
            c_string(alert_condition_name(event.condition), "alert condition")?,
            c_string(alert_severity_name(event.severity), "alert severity")?,
            c_string(alert_event_state_name(event.state), "alert state")?,
            c_string(&event.resource_ref, "alert resource")?,
            c_string(&event.summary, "alert summary")?,
            c_string(&event.detail, "alert detail")?,
            c_string(&event.source_section, "alert source")?,
        ];
        for (offset, value) in values.iter().enumerate() {
            bind_text(
                self.database,
                statement.raw,
                i32::try_from(offset + 1).unwrap(),
                value,
            )?;
        }
        bind_i64(
            self.database,
            statement.raw,
            9,
            event.first_observed_at_unix_ms,
        )?;
        bind_i64(
            self.database,
            statement.raw,
            10,
            event.last_observed_at_unix_ms,
        )?;
        bind_i64(
            self.database,
            statement.raw,
            11,
            event.resolved_at_unix_ms.unwrap_or(-1),
        )?;
        bind_i64(
            self.database,
            statement.raw,
            12,
            checked_i64(event.occurrence_count, "alert occurrence count")?,
        )?;
        bind_i64(
            self.database,
            statement.raw,
            13,
            event.acknowledged_at_unix_ms.unwrap_or(-1),
        )?;
        let acknowledged_by = c_string(
            event.acknowledged_by.as_deref().unwrap_or_default(),
            "alert operator",
        )?;
        bind_text(self.database, statement.raw, 14, &acknowledged_by)?;
        expect_done(self.database, statement.raw)
    }

    pub fn read_media_asset(&self, asset_id: &str) -> Result<Option<MediaAsset>, String> {
        let statement = self.prepare(
            "SELECT asset_id, media_path, duration_ms, readiness FROM media_assets WHERE asset_id=?1",
        )?;
        let asset_id = c_string(asset_id, "asset identity")?;
        bind_text(self.database, statement.raw, 1, &asset_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_media_asset_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    /// Admit an immutable, content-addressed PNG/JPEG into the CG image library.
    /// The database stores only the path derived from `managed_cg_image_path`; a caller-supplied
    /// path is never accepted as authority.
    pub fn admit_cg_image_asset(&self, bytes: &[u8]) -> Result<CgImageAsset, String> {
        let asset_id = format!("{:x}", Sha256::digest(bytes));
        let mime_type = if bytes.starts_with(b"\x89PNG\r\n\x1a\n") {
            "image/png"
        } else if bytes.starts_with(&[0xff, 0xd8, 0xff]) {
            "image/jpeg"
        } else {
            return Err("only PNG and JPEG images are supported".into());
        };
        let image_dir = self.validated_cg_image_root()?;
        let path = managed_cg_image_path(&image_dir, &asset_id, mime_type)?;
        let asset = inspect_cg_image(&asset_id, bytes)?;

        if path.exists() {
            let metadata = fs::symlink_metadata(&path)
                .map_err(|error| format!("inspect immutable image: {error}"))?;
            if !metadata.is_file() || metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
            {
                return Err("managed image path is not a regular non-reparse file".into());
            }
            let existing =
                fs::read(&path).map_err(|error| format!("read immutable image: {error}"))?;
            if existing != bytes {
                return Err("managed image path already contains different bytes".into());
            }
        } else {
            let sequence = CG_IMAGE_TEMP_SEQUENCE.fetch_add(1, Ordering::Relaxed);
            let temp_path = image_dir.join(format!(
                ".{}.part-{}-{}",
                asset.asset_id,
                std::process::id(),
                sequence
            ));
            let mut file = fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&temp_path)
                .map_err(|error| format!("create immutable image: {error}"))?;
            if let Err(error) = file.write_all(bytes).and_then(|_| file.sync_all()) {
                let _ = fs::remove_file(&temp_path);
                return Err(format!("write immutable image: {error}"));
            }
            if let Err(error) = fs::rename(&temp_path, &path) {
                let _ = fs::remove_file(&temp_path);
                if !path.exists() {
                    return Err(format!("publish immutable image: {error}"));
                }
                let existing = fs::read(&path)
                    .map_err(|read_error| format!("read raced image: {read_error}"))?;
                if existing != bytes {
                    return Err("managed image path already contains different bytes".into());
                }
            }
        }

        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            if let Some(existing) = self.read_cg_image_asset(&asset.asset_id)? {
                if existing != asset {
                    return Err("CG image identity already has different metadata".into());
                }
                return Ok(existing);
            }
            let statement = self.prepare(
                "INSERT INTO cg_image_assets(asset_id, mime_type, byte_size, width, height, readiness) VALUES(?1, ?2, ?3, ?4, ?5, ?6)",
            )?;
            let asset_id = c_string(&asset.asset_id, "image identity")?;
            let mime_type = c_string(&asset.mime_type, "image MIME")?;
            let readiness = c_string(cg_image_readiness_name(asset.readiness), "image readiness")?;
            bind_text(self.database, statement.raw, 1, &asset_id)?;
            bind_text(self.database, statement.raw, 2, &mime_type)?;
            bind_i64(
                self.database,
                statement.raw,
                3,
                checked_i64(asset.byte_size, "image byte size")?,
            )?;
            bind_i64(self.database, statement.raw, 4, i64::from(asset.width))?;
            bind_i64(self.database, statement.raw, 5, i64::from(asset.height))?;
            bind_text(self.database, statement.raw, 6, &readiness)?;
            expect_done(self.database, statement.raw)?;
            Ok(asset)
        })();
        match outcome {
            Ok(asset) => match self.execute("COMMIT") {
                Ok(()) => Ok(asset),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(error)
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn read_cg_image_asset(&self, asset_id: &str) -> Result<Option<CgImageAsset>, String> {
        let statement = self.prepare(
            "SELECT asset_id, mime_type, byte_size, width, height, readiness FROM cg_image_assets WHERE asset_id=?1",
        )?;
        let asset_id = c_string(asset_id, "image identity")?;
        bind_text(self.database, statement.raw, 1, &asset_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_cg_image_asset_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_cg_image_assets(&self) -> Result<Vec<CgImageAsset>, String> {
        let statement = self.prepare(
            "SELECT asset_id, mime_type, byte_size, width, height, readiness FROM cg_image_assets ORDER BY asset_id",
        )?;
        let mut assets = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => assets.push(read_cg_image_asset_row(statement.raw)?),
                SQLITE_DONE => return Ok(assets),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn list_media_assets(&self) -> Result<Vec<MediaAsset>, String> {
        let statement = self.prepare(
            "SELECT asset_id, media_path, duration_ms, readiness FROM media_assets ORDER BY asset_id",
        )?;
        let mut assets = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => assets.push(read_media_asset_row(statement.raw)?),
                SQLITE_DONE => return Ok(assets),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn put_asset_loudness(&self, analysis: &AssetLoudnessAnalysis) -> Result<(), String> {
        analysis.validate().map_err(|error| format!("{error:?}"))?;
        let statement = self.prepare(
            "INSERT INTO asset_loudness_analysis(asset_id, status, measured_lufs_milli, \
             target_lufs_milli, detail) VALUES(?1, ?2, ?3, ?4, ?5) \
             ON CONFLICT(asset_id) DO UPDATE SET status=excluded.status, \
             measured_lufs_milli=excluded.measured_lufs_milli, target_lufs_milli=excluded.target_lufs_milli, \
             detail=excluded.detail",
        )?;
        let asset_id = c_string(&analysis.asset_id, "loudness asset identity")?;
        let status = c_string(loudness_status_name(analysis.status), "loudness status")?;
        let detail = c_string(
            analysis.detail.as_deref().unwrap_or_default(),
            "loudness detail",
        )?;
        bind_text(self.database, statement.raw, 1, &asset_id)?;
        bind_text(self.database, statement.raw, 2, &status)?;
        bind_i64(
            self.database,
            statement.raw,
            3,
            analysis
                .measured_lufs_milli
                .map_or(i64::from(i32::MIN), i64::from),
        )?;
        bind_i64(
            self.database,
            statement.raw,
            4,
            i64::from(analysis.target_lufs_milli),
        )?;
        bind_text(self.database, statement.raw, 5, &detail)?;
        expect_done(self.database, statement.raw)
    }

    pub fn read_asset_loudness(
        &self,
        asset_id: &str,
    ) -> Result<Option<AssetLoudnessAnalysis>, String> {
        let statement = self.prepare(
            "SELECT asset_id, status, measured_lufs_milli, target_lufs_milli, detail \
             FROM asset_loudness_analysis WHERE asset_id=?1",
        )?;
        let asset_id = c_string(asset_id, "loudness asset identity")?;
        bind_text(self.database, statement.raw, 1, &asset_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_asset_loudness_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_asset_loudness(&self) -> Result<Vec<AssetLoudnessAnalysis>, String> {
        let statement = self.prepare(
            "SELECT asset_id, status, measured_lufs_milli, target_lufs_milli, detail \
             FROM asset_loudness_analysis ORDER BY asset_id",
        )?;
        let mut analyses = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => analyses.push(read_asset_loudness_row(statement.raw)?),
                SQLITE_DONE => return Ok(analyses),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn list_ingest_jobs(&self) -> Result<Vec<MediaIngestJob>, String> {
        let statement = self.prepare(
             "SELECT job_id, source_kind, source_path, status, progress_percent, created_at_unix_ms, \
             started_at_unix_ms, completed_at_unix_ms, error_detail, asset_id, attempts, next_attempt_at_unix_ms \
             FROM media_ingest_jobs ORDER BY created_at_unix_ms DESC, job_id",
        )?;
        let mut jobs = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => jobs.push(read_ingest_job_row(statement.raw)?),
                SQLITE_DONE => return Ok(jobs),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn put_watch_folder(&self, config: &WatchFolderConfig) -> Result<(), String> {
        config.validate().map_err(|error| format!("{error:?}"))?;
        if config.last_scan_at_unix_ms.is_some() || config.last_error.is_some() {
            return Err("watch-folder scan state is read-only".into());
        }
        let statement = self.prepare(
            "INSERT INTO watch_folder_configs(config_id, display_name, monitor_path, extensions_json, \
             settle_seconds, enabled, last_scan_at_unix_ms, last_error) VALUES(?1, ?2, ?3, ?4, ?5, ?6, -1, '') \
             ON CONFLICT(config_id) DO UPDATE SET display_name=excluded.display_name, \
             monitor_path=excluded.monitor_path, extensions_json=excluded.extensions_json, \
             settle_seconds=excluded.settle_seconds, enabled=excluded.enabled",
        )?;
        let values = [
            c_string(&config.config_id, "watch-folder identity")?,
            c_string(&config.display_name, "watch-folder display name")?,
            c_string(&config.monitor_path, "watch-folder path")?,
            c_string(
                &serde_json::to_string(&config.extensions).map_err(|error| {
                    format!("could not serialize watch-folder extensions: {error}")
                })?,
                "watch-folder extensions",
            )?,
        ];
        for (index, value) in values.iter().enumerate() {
            bind_text(self.database, statement.raw, (index + 1) as c_int, value)?;
        }
        bind_i64(
            self.database,
            statement.raw,
            5,
            i64::from(config.settle_seconds),
        )?;
        bind_i64(self.database, statement.raw, 6, i64::from(config.enabled))?;
        expect_done(self.database, statement.raw)
    }

    pub fn list_watch_folders(&self) -> Result<Vec<WatchFolderConfig>, String> {
        let statement = self.prepare(
            "SELECT config_id, display_name, monitor_path, extensions_json, settle_seconds, enabled, \
             last_scan_at_unix_ms, last_error FROM watch_folder_configs ORDER BY display_name, config_id",
        )?;
        let mut configs = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => configs.push(read_watch_folder_row(statement.raw)?),
                SQLITE_DONE => return Ok(configs),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn record_watch_folder_scan(
        &self,
        config_id: &str,
        scanned_at_unix_ms: i64,
        error_detail: Option<&str>,
    ) -> Result<(), String> {
        if scanned_at_unix_ms < 0
            || error_detail.is_some_and(|value| {
                value.trim().is_empty()
                    || value.len() > 4_000
                    || value.chars().any(char::is_control)
            })
        {
            return Err("watch-folder scan result is invalid".into());
        }
        let statement = self.prepare(
            "UPDATE watch_folder_configs SET last_scan_at_unix_ms=?1, last_error=?2 WHERE config_id=?3",
        )?;
        let error = c_string(error_detail.unwrap_or_default(), "watch-folder scan error")?;
        let config_id = c_string(config_id, "watch-folder identity")?;
        bind_i64(self.database, statement.raw, 1, scanned_at_unix_ms)?;
        bind_text(self.database, statement.raw, 2, &error)?;
        bind_text(self.database, statement.raw, 3, &config_id)?;
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("watch-folder configuration does not exist".into())
        }
    }

    pub fn observe_watch_file(
        &self,
        config_id: &str,
        source_path: &str,
        size_bytes: u64,
        modified_at_unix_ms: i64,
        observed_at_unix_ms: i64,
    ) -> Result<WatchFileObservation, String> {
        if source_path.trim().is_empty()
            || source_path.chars().any(char::is_control)
            || modified_at_unix_ms < 0
            || observed_at_unix_ms < 0
        {
            return Err("watch-file observation is invalid".into());
        }
        let existing = self.read_watch_file_observation(config_id, source_path)?;
        let unchanged = existing.as_ref().is_some_and(|value| {
            value.size_bytes == size_bytes && value.modified_at_unix_ms == modified_at_unix_ms
        });
        let unchanged_since = if unchanged {
            existing
                .as_ref()
                .expect("unchanged requires an observation")
                .unchanged_since_unix_ms
        } else {
            observed_at_unix_ms
        };
        let observation_count = if unchanged {
            existing
                .as_ref()
                .expect("unchanged requires an observation")
                .observation_count
                .saturating_add(1)
        } else {
            1
        };
        let queued_job_id = if unchanged {
            existing.and_then(|value| value.queued_job_id)
        } else {
            None
        };
        let statement = self.prepare(
            "INSERT INTO watch_file_observations(config_id, source_path, size_bytes, modified_at_unix_ms, \
             unchanged_since_unix_ms, observation_count, queued_job_id) VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7) \
             ON CONFLICT(config_id, source_path) DO UPDATE SET size_bytes=excluded.size_bytes, \
             modified_at_unix_ms=excluded.modified_at_unix_ms, unchanged_since_unix_ms=excluded.unchanged_since_unix_ms, \
             observation_count=excluded.observation_count, queued_job_id=excluded.queued_job_id",
        )?;
        let config = c_string(config_id, "watch-folder identity")?;
        let source = c_string(source_path, "watch-file path")?;
        let queued = c_string(
            queued_job_id.as_deref().unwrap_or_default(),
            "watch-file job",
        )?;
        bind_text(self.database, statement.raw, 1, &config)?;
        bind_text(self.database, statement.raw, 2, &source)?;
        bind_i64(
            self.database,
            statement.raw,
            3,
            checked_i64(size_bytes, "watch-file size")?,
        )?;
        bind_i64(self.database, statement.raw, 4, modified_at_unix_ms)?;
        bind_i64(self.database, statement.raw, 5, unchanged_since)?;
        bind_i64(
            self.database,
            statement.raw,
            6,
            i64::from(observation_count),
        )?;
        bind_text(self.database, statement.raw, 7, &queued)?;
        expect_done(self.database, statement.raw)?;
        Ok(WatchFileObservation {
            config_id: config_id.into(),
            source_path: source_path.into(),
            size_bytes,
            modified_at_unix_ms,
            unchanged_since_unix_ms: unchanged_since,
            observation_count,
            queued_job_id,
        })
    }

    pub fn mark_watch_file_queued(
        &self,
        config_id: &str,
        source_path: &str,
        job_id: &str,
    ) -> Result<(), String> {
        let statement = self.prepare(
            "UPDATE watch_file_observations SET queued_job_id=?1 WHERE config_id=?2 AND source_path=?3 \
             AND (queued_job_id='' OR queued_job_id=?1)",
        )?;
        let values = [
            c_string(job_id, "watch-file job identity")?,
            c_string(config_id, "watch-folder identity")?,
            c_string(source_path, "watch-file path")?,
        ];
        for (index, value) in values.iter().enumerate() {
            bind_text(self.database, statement.raw, (index + 1) as c_int, value)?;
        }
        expect_done(self.database, statement.raw)?;
        if self.changes() == 1 {
            Ok(())
        } else {
            Err("watch-file observation was not queueable".into())
        }
    }

    fn read_watch_file_observation(
        &self,
        config_id: &str,
        source_path: &str,
    ) -> Result<Option<WatchFileObservation>, String> {
        let statement = self.prepare(
            "SELECT config_id, source_path, size_bytes, modified_at_unix_ms, unchanged_since_unix_ms, \
             observation_count, queued_job_id FROM watch_file_observations WHERE config_id=?1 AND source_path=?2",
        )?;
        let config = c_string(config_id, "watch-folder identity")?;
        let source = c_string(source_path, "watch-file path")?;
        bind_text(self.database, statement.raw, 1, &config)?;
        bind_text(self.database, statement.raw, 2, &source)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_watch_file_observation_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn put_schedule_item(&self, item: &ScheduleItem) -> Result<(), String> {
        item.validate().map_err(|error| format!("{error:?}"))?;
        let statement = self.prepare(
            "INSERT INTO schedule_items(item_id, channel_id, asset_id, title, starts_at_unix_ms, duration_ms, state) \
             VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7) \
             ON CONFLICT(item_id) DO UPDATE SET channel_id=excluded.channel_id, asset_id=excluded.asset_id, title=excluded.title, \
             starts_at_unix_ms=excluded.starts_at_unix_ms, duration_ms=excluded.duration_ms, state=excluded.state",
        )?;
        let values = [
            c_string(&item.item_id, "schedule item identity")?,
            c_string(&item.channel_id, "channel identity")?,
            c_string(&item.asset_id, "asset identity")?,
            c_string(&item.title, "schedule title")?,
            c_string(schedule_state_name(item.state), "schedule state")?,
        ];
        for (index, value) in values[..4].iter().enumerate() {
            bind_text(self.database, statement.raw, (index + 1) as c_int, value)?;
        }
        bind_i64(self.database, statement.raw, 5, item.starts_at_unix_ms)?;
        bind_i64(
            self.database,
            statement.raw,
            6,
            checked_i64(item.duration_ms, "schedule duration")?,
        )?;
        bind_text(self.database, statement.raw, 7, &values[4])?;
        expect_done(self.database, statement.raw)
    }

    pub fn read_schedule_item(&self, item_id: &str) -> Result<Option<ScheduleItem>, String> {
        let statement = self.prepare(
            "SELECT item_id, channel_id, asset_id, title, starts_at_unix_ms, duration_ms, state \
             FROM schedule_items WHERE item_id=?1",
        )?;
        let item_id = c_string(item_id, "schedule item identity")?;
        bind_text(self.database, statement.raw, 1, &item_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_schedule_item_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_channel_schedule(&self, channel_id: &str) -> Result<Vec<ScheduleItem>, String> {
        let statement = self.prepare(
            "SELECT item_id, channel_id, asset_id, title, starts_at_unix_ms, duration_ms, state \
             FROM schedule_items WHERE channel_id=?1 ORDER BY starts_at_unix_ms, item_id",
        )?;
        let channel_id = c_string(channel_id, "channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        let mut items = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => items.push(read_schedule_item_row(statement.raw)?),
                SQLITE_DONE => return Ok(items),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn list_schedule_window(
        &self,
        starts_at_unix_ms: i64,
        ends_before_unix_ms: i64,
    ) -> Result<Vec<ScheduleItem>, String> {
        if starts_at_unix_ms < 0 || ends_before_unix_ms <= starts_at_unix_ms {
            return Err("schedule window is invalid".into());
        }
        let statement = self.prepare(
            "SELECT item_id, channel_id, asset_id, title, starts_at_unix_ms, duration_ms, state \
             FROM schedule_items WHERE state='committed' AND starts_at_unix_ms>=?1 \
             AND starts_at_unix_ms<?2 ORDER BY starts_at_unix_ms, item_id",
        )?;
        bind_i64(self.database, statement.raw, 1, starts_at_unix_ms)?;
        bind_i64(self.database, statement.raw, 2, ends_before_unix_ms)?;
        let mut items = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => items.push(read_schedule_item_row(statement.raw)?),
                SQLITE_DONE => return Ok(items),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn prepare_schedule_commit(
        &self,
        plan_id: &str,
        item_id: &str,
    ) -> Result<CommitPlan, CommitWriteError> {
        let item = self
            .read_schedule_item(item_id)
            .map_err(CommitWriteError::Storage)?
            .ok_or(CommitWriteError::NotFound)?;
        let mut asset = self
            .read_media_asset(&item.asset_id)
            .map_err(CommitWriteError::Storage)?;
        if let Some(asset) = asset.as_mut()
            && asset.readiness == AssetReadiness::Ready
            && !Path::new(&asset.media_path).is_file()
        {
            asset.readiness = AssetReadiness::Missing;
        }
        let channel_items = self
            .list_channel_schedule(&item.channel_id)
            .map_err(CommitWriteError::Storage)?;
        prepare_commit(plan_id, &item, asset.as_ref(), &channel_items)
            .map_err(|error| CommitWriteError::Storage(format!("{error:?}")))
    }

    pub fn commit_schedule(
        &self,
        report_id: &str,
        plan_id: &str,
        item_id: &str,
        approved_by: &str,
        approved_at_unix_ms: i64,
        operator_notes: &str,
    ) -> Result<CommitReport, CommitWriteError> {
        if report_id.trim().is_empty()
            || plan_id.trim().is_empty()
            || approved_by.trim().is_empty()
            || approved_by.chars().any(char::is_control)
            || operator_notes.len() > 4_000
            || operator_notes.chars().any(char::is_control)
            || approved_at_unix_ms < 0
        {
            return Err(CommitWriteError::Invalid(
                "commit identity, operator, timestamp, or notes are invalid",
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(CommitWriteError::Storage)?;
        let outcome = (|| {
            let plan = self.prepare_schedule_commit(plan_id, item_id)?;
            if !plan.dry_run_passed {
                return Err(CommitWriteError::GateFailed(Box::new(plan)));
            }
            let item = self
                .read_schedule_item(item_id)
                .map_err(CommitWriteError::Storage)?
                .ok_or(CommitWriteError::NotFound)?;
            let report = CommitReport {
                report_id: report_id.to_string(),
                plan_id: plan_id.to_string(),
                channel_id: item.channel_id.clone(),
                schedule_item_id: item.item_id.clone(),
                asset_id: item.asset_id.clone(),
                approved_by: approved_by.to_string(),
                approved_at_unix_ms,
                dispatch_status: DispatchStatus::Pending,
                operator_notes: operator_notes.to_string(),
            };
            self.insert_commit_report(&report)
                .map_err(CommitWriteError::Storage)?;
            self.set_schedule_state(item_id, ScheduleState::Committed)
                .map_err(CommitWriteError::Storage)?;
            Ok(report)
        })();
        match outcome {
            Ok(report) => match self.execute("COMMIT") {
                Ok(()) => Ok(report),
                Err(message) => {
                    let _ = self.execute("ROLLBACK");
                    Err(CommitWriteError::Storage(message))
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn read_commit_report(&self, report_id: &str) -> Result<Option<CommitReport>, String> {
        let statement = self.prepare(
            "SELECT report_id, plan_id, channel_id, schedule_item_id, asset_id, approved_by, \
             approved_at_unix_ms, dispatch_status, operator_notes FROM commit_reports WHERE report_id=?1",
        )?;
        let report_id = c_string(report_id, "commit report identity")?;
        bind_text(self.database, statement.raw, 1, &report_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_commit_report_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_dispatch_jobs(&self, channel_id: &str) -> Result<Vec<DispatchJob>, String> {
        let statement = self.prepare(
            "SELECT r.report_id, r.plan_id, r.channel_id, r.schedule_item_id, r.asset_id, r.approved_by, \
             r.approved_at_unix_ms, r.dispatch_status, r.operator_notes, \
             s.item_id, s.channel_id, s.asset_id, s.title, s.starts_at_unix_ms, s.duration_ms, s.state, \
             a.asset_id, a.media_path, a.duration_ms, a.readiness \
             FROM commit_reports r JOIN schedule_items s ON s.item_id=r.schedule_item_id \
             JOIN media_assets a ON a.asset_id=r.asset_id WHERE r.channel_id=?1 \
             AND r.dispatch_status IN ('pending','queued','acknowledged') \
             ORDER BY s.starts_at_unix_ms, r.approved_at_unix_ms, r.report_id",
        )?;
        let channel_id = c_string(channel_id, "channel identity")?;
        bind_text(self.database, statement.raw, 1, &channel_id)?;
        let mut jobs = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => jobs.push(DispatchJob {
                    report: read_commit_report_row(statement.raw)?,
                    item: ScheduleItem {
                        item_id: column_text(statement.raw, 9)?,
                        channel_id: column_text(statement.raw, 10)?,
                        asset_id: column_text(statement.raw, 11)?,
                        title: column_text(statement.raw, 12)?,
                        starts_at_unix_ms: unsafe { sqlite3_column_int64(statement.raw, 13) },
                        duration_ms: column_u64(statement.raw, 14, "schedule duration")?,
                        state: parse_schedule_state(&column_text(statement.raw, 15)?)?,
                    },
                    asset: MediaAsset {
                        asset_id: column_text(statement.raw, 16)?,
                        media_path: column_text(statement.raw, 17)?,
                        duration_ms: column_u64(statement.raw, 18, "asset duration")?,
                        readiness: parse_readiness(&column_text(statement.raw, 19)?)?,
                    },
                }),
                SQLITE_DONE => return Ok(jobs),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn advance_dispatch_status(
        &self,
        report_id: &str,
        expected: DispatchStatus,
        next: DispatchStatus,
    ) -> Result<bool, String> {
        if !valid_dispatch_transition(expected, next) {
            return Err("invalid dispatch status transition".into());
        }
        let statement = self.prepare(
            "UPDATE commit_reports SET dispatch_status=?1 WHERE report_id=?2 AND dispatch_status=?3",
        )?;
        let next = c_string(dispatch_status_name(next), "next dispatch status")?;
        let report_id = c_string(report_id, "report identity")?;
        let expected = c_string(dispatch_status_name(expected), "expected dispatch status")?;
        bind_text(self.database, statement.raw, 1, &next)?;
        bind_text(self.database, statement.raw, 2, &report_id)?;
        bind_text(self.database, statement.raw, 3, &expected)?;
        expect_done(self.database, statement.raw)?;
        Ok(self.changes() == 1)
    }

    fn insert_commit_report(&self, report: &CommitReport) -> Result<(), String> {
        let statement = self.prepare(
            "INSERT INTO commit_reports(report_id, plan_id, channel_id, schedule_item_id, asset_id, approved_by, \
             approved_at_unix_ms, dispatch_status, operator_notes) VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
        )?;
        let values = [
            c_string(&report.report_id, "report identity")?,
            c_string(&report.plan_id, "plan identity")?,
            c_string(&report.channel_id, "channel identity")?,
            c_string(&report.schedule_item_id, "schedule item identity")?,
            c_string(&report.asset_id, "asset identity")?,
            c_string(&report.approved_by, "operator identity")?,
            c_string(
                dispatch_status_name(report.dispatch_status),
                "dispatch status",
            )?,
            c_string(&report.operator_notes, "operator notes")?,
        ];
        for (index, value) in values[..6].iter().enumerate() {
            bind_text(self.database, statement.raw, (index + 1) as c_int, value)?;
        }
        bind_i64(self.database, statement.raw, 7, report.approved_at_unix_ms)?;
        bind_text(self.database, statement.raw, 8, &values[6])?;
        bind_text(self.database, statement.raw, 9, &values[7])?;
        expect_done(self.database, statement.raw)
    }

    fn set_schedule_state(&self, item_id: &str, state: ScheduleState) -> Result<(), String> {
        let statement = self.prepare("UPDATE schedule_items SET state=?1 WHERE item_id=?2")?;
        let state = c_string(schedule_state_name(state), "schedule state")?;
        let item_id = c_string(item_id, "schedule item identity")?;
        bind_text(self.database, statement.raw, 1, &state)?;
        bind_text(self.database, statement.raw, 2, &item_id)?;
        expect_done(self.database, statement.raw)
    }

    fn execute(&self, sql: &str) -> Result<(), String> {
        let sql = CString::new(sql).map_err(|_| "SQL contains a null byte".to_string())?;
        // SAFETY: the database handle is live, SQL is a valid C string, and no callback is used.
        let status = unsafe {
            sqlite3_exec(
                self.database,
                sql.as_ptr(),
                ptr::null_mut(),
                ptr::null_mut(),
                ptr::null_mut(),
            )
        };
        if status == SQLITE_OK {
            Ok(())
        } else {
            Err(sqlite_error(self.database))
        }
    }

    fn initialize_cg_schema(&self) -> Result<(), String> {
        self.execute(
            "CREATE TABLE IF NOT EXISTS cg_boards (\
               board_id TEXT PRIMARY KEY,\
               channel_id TEXT NOT NULL UNIQUE,\
               template_id TEXT NOT NULL,\
               active INTEGER NOT NULL CHECK (active IN (0,1)),\
               background_audio_asset_id TEXT NOT NULL DEFAULT '',\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               updated_at_unix_ms INTEGER NOT NULL CHECK (updated_at_unix_ms >= created_at_unix_ms),\
               created_by TEXT NOT NULL,\
               FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE\
             );\
             CREATE TABLE IF NOT EXISTS cg_zone_configs (\
               zone_id TEXT PRIMARY KEY,\
               board_id TEXT NOT NULL,\
               template_zone_key TEXT NOT NULL,\
               zone_kind TEXT NOT NULL,\
               content_source_json TEXT NOT NULL,\
               rotation_seconds INTEGER NOT NULL DEFAULT -1 CHECK (rotation_seconds BETWEEN -1 AND 3600),\
               approval_required INTEGER NOT NULL CHECK (approval_required IN (0,1)),\
               allowed_tags_json TEXT NOT NULL,\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               updated_at_unix_ms INTEGER NOT NULL CHECK (updated_at_unix_ms >= created_at_unix_ms),\
               UNIQUE(board_id, template_zone_key),\
               FOREIGN KEY(board_id) REFERENCES cg_boards(board_id) ON DELETE CASCADE\
             );\
             CREATE TABLE IF NOT EXISTS cg_feed_sources (\
               feed_source_id TEXT PRIMARY KEY,\
               channel_id TEXT NOT NULL,\
             kind TEXT NOT NULL CHECK (kind IN ('rss','ical','caldav','weather','social')),\
             label TEXT NOT NULL,\
             source_url TEXT NOT NULL,\
             trust_tier TEXT NOT NULL CHECK (trust_tier IN ('station','trusted','moderated')),\
             tags_json TEXT NOT NULL DEFAULT '[]',\
             refresh_seconds INTEGER NOT NULL CHECK (refresh_seconds BETWEEN 60 AND 86400),\
               enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               updated_at_unix_ms INTEGER NOT NULL CHECK (updated_at_unix_ms >= created_at_unix_ms),\
               created_by TEXT NOT NULL,\
               last_fetched_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (last_fetched_at_unix_ms >= -1),\
               last_fetch_error TEXT NOT NULL DEFAULT '',\
               UNIQUE(channel_id, label COLLATE NOCASE),\
               FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE\
             );\
             CREATE TABLE IF NOT EXISTS cg_bulletins (\
               bulletin_id TEXT PRIMARY KEY,\
               channel_id TEXT NOT NULL,\
               content_json TEXT NOT NULL,\
               state TEXT NOT NULL CHECK (state IN ('submitted','needs_changes','accepted','scheduled','declined','expired')),\
               tags_json TEXT NOT NULL,\
               requested_start_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (requested_start_at_unix_ms >= -1),\
               requested_end_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (requested_end_at_unix_ms >= -1),\
               display_seconds INTEGER NOT NULL CHECK (display_seconds BETWEEN 3 AND 300),\
               audio_asset_id TEXT NOT NULL DEFAULT '',\
               submitted_by TEXT NOT NULL,\
               moderation_notes TEXT NOT NULL DEFAULT '',\
               moderated_by TEXT NOT NULL DEFAULT '',\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               updated_at_unix_ms INTEGER NOT NULL CHECK (updated_at_unix_ms >= created_at_unix_ms),\
               FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE\
             );\
             CREATE INDEX IF NOT EXISTS cg_bulletins_channel_state_time \
               ON cg_bulletins(channel_id, state, requested_start_at_unix_ms, requested_end_at_unix_ms);\
             CREATE TABLE IF NOT EXISTS cg_feed_item_approvals (\
               feed_source_id TEXT NOT NULL,\
               item_id TEXT NOT NULL,\
               approved_by TEXT NOT NULL,\
               approved_at_unix_ms INTEGER NOT NULL CHECK (approved_at_unix_ms >= 0),\
               PRIMARY KEY(feed_source_id, item_id),\
               FOREIGN KEY(feed_source_id) REFERENCES cg_feed_sources(feed_source_id) ON DELETE CASCADE\
             );\
             CREATE TABLE IF NOT EXISTS cg_audit_events (\
               audit_id TEXT PRIMARY KEY,\
               board_id TEXT NOT NULL DEFAULT '',\
               channel_id TEXT NOT NULL,\
               event_kind TEXT NOT NULL,\
               operator_id TEXT NOT NULL,\
               occurred_at_unix_ms INTEGER NOT NULL CHECK (occurred_at_unix_ms >= 0),\
               details_json TEXT NOT NULL\
             );\
             CREATE INDEX IF NOT EXISTS cg_audit_events_channel_time \
               ON cg_audit_events(channel_id, occurred_at_unix_ms DESC, audit_id);",
        )?;
        self.execute(
            "CREATE TABLE IF NOT EXISTS cg_image_assets (\
               asset_id TEXT PRIMARY KEY,\
               mime_type TEXT NOT NULL CHECK (mime_type IN ('image/png','image/jpeg')),\
               byte_size INTEGER NOT NULL CHECK (byte_size BETWEEN 1 AND 8388608),\
               width INTEGER NOT NULL CHECK (width BETWEEN 1 AND 4096),\
               height INTEGER NOT NULL CHECK (height BETWEEN 1 AND 4096),\
               readiness TEXT NOT NULL CHECK (readiness IN ('ready','missing','rejected','processing'))\
             );\
             CREATE INDEX IF NOT EXISTS cg_image_assets_readiness ON cg_image_assets(readiness, asset_id);",
        )
    }

    fn migrate_cg_feed_cache_schema(&self) -> Result<(), String> {
        let statement = self.prepare("PRAGMA table_info(cg_feed_sources)")?;
        let mut has_tags = false;
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    if column_text(statement.raw, 1)? == "tags_json" {
                        has_tags = true;
                    }
                }
                SQLITE_DONE => break,
                _ => return Err(sqlite_error(self.database)),
            }
        }
        drop(statement);
        if !has_tags {
            self.execute(
                "ALTER TABLE cg_feed_sources ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'",
            )?;
        }
        self.execute(
            r#"CREATE TABLE IF NOT EXISTS cg_feed_cache_meta (
               feed_source_id TEXT PRIMARY KEY,
               fetched_at_unix_ms INTEGER NOT NULL CHECK (fetched_at_unix_ms >= 0),
               expires_at_unix_ms INTEGER NOT NULL CHECK (expires_at_unix_ms >= fetched_at_unix_ms),
               etag TEXT NOT NULL DEFAULT '',
               last_modified TEXT NOT NULL DEFAULT '',
               last_error TEXT NOT NULL DEFAULT '',
               FOREIGN KEY(feed_source_id) REFERENCES cg_feed_sources(feed_source_id) ON DELETE CASCADE
             );
             CREATE TABLE IF NOT EXISTS cg_feed_cache_items (
               feed_source_id TEXT NOT NULL,
               item_id TEXT NOT NULL,
               title TEXT NOT NULL,
               summary TEXT NOT NULL DEFAULT '',
               starts_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (starts_at_unix_ms >= -1),
               url TEXT NOT NULL DEFAULT '',
               tags_json TEXT NOT NULL DEFAULT '[]',
               fetched_at_unix_ms INTEGER NOT NULL CHECK (fetched_at_unix_ms >= 0),
               PRIMARY KEY(feed_source_id, item_id),
               FOREIGN KEY(feed_source_id) REFERENCES cg_feed_sources(feed_source_id) ON DELETE CASCADE
             );
             CREATE INDEX IF NOT EXISTS cg_feed_cache_items_source_start
               ON cg_feed_cache_items(feed_source_id, starts_at_unix_ms, item_id);"#,
        )
    }

    fn migrate_commit_report_statuses(&self) -> Result<(), String> {
        let statement = self.prepare(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='commit_reports'",
        )?;
        let definition = match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => column_text(statement.raw, 0)?,
            SQLITE_DONE => return Err("commit_reports schema is missing".into()),
            _ => return Err(sqlite_error(self.database)),
        };
        drop(statement);
        if definition.contains("'completed'") {
            return Ok(());
        }
        self.execute(
            "BEGIN IMMEDIATE;\
             ALTER TABLE commit_reports RENAME TO commit_reports_legacy;\
             CREATE TABLE commit_reports (\
               report_id TEXT PRIMARY KEY,\
               plan_id TEXT NOT NULL,\
               channel_id TEXT NOT NULL,\
               schedule_item_id TEXT NOT NULL,\
               asset_id TEXT NOT NULL,\
               approved_by TEXT NOT NULL,\
               approved_at_unix_ms INTEGER NOT NULL,\
               dispatch_status TEXT NOT NULL CHECK (dispatch_status IN ('pending','queued','acknowledged','completed','error','cancelled')),\
               operator_notes TEXT NOT NULL\
             );\
             INSERT INTO commit_reports SELECT * FROM commit_reports_legacy;\
             DROP TABLE commit_reports_legacy;\
             CREATE INDEX commit_reports_channel_time ON commit_reports(channel_id, approved_at_unix_ms);\
             COMMIT;",
        )
        .inspect_err(|_| {
            let _ = self.execute("ROLLBACK");
        })
    }

    fn migrate_alert_delivery_realerts(&self) -> Result<(), String> {
        let statement = self.prepare(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='alert_deliveries'",
        )?;
        let definition = match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => column_text(statement.raw, 0)?,
            SQLITE_DONE => return Err("alert_deliveries schema is missing".into()),
            _ => return Err(sqlite_error(self.database)),
        };
        drop(statement);
        if definition.contains("'realert'") {
            return Ok(());
        }
        self.execute(
            "BEGIN IMMEDIATE;\
             DROP INDEX IF EXISTS alert_deliveries_due;\
             ALTER TABLE alert_deliveries RENAME TO alert_deliveries_legacy;\
             CREATE TABLE alert_deliveries (\
               delivery_id TEXT PRIMARY KEY,\
               event_id TEXT NOT NULL,\
               alert_channel_id TEXT NOT NULL,\
               notification_kind TEXT NOT NULL CHECK (notification_kind IN ('firing','realert','resolved','test')),\
               state TEXT NOT NULL CHECK (state IN ('pending','leased','sent','dead_letter')),\
               attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),\
               next_attempt_at_unix_ms INTEGER NOT NULL CHECK (next_attempt_at_unix_ms >= 0),\
               lease_expires_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (lease_expires_at_unix_ms >= -1),\
               last_error TEXT NOT NULL DEFAULT '',\
               signature TEXT NOT NULL DEFAULT '',\
               payload_json TEXT NOT NULL,\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               updated_at_unix_ms INTEGER NOT NULL CHECK (updated_at_unix_ms >= created_at_unix_ms),\
               sent_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (sent_at_unix_ms >= -1),\
               FOREIGN KEY(event_id) REFERENCES alert_events(event_id),\
               FOREIGN KEY(alert_channel_id) REFERENCES alert_channels(channel_id)\
             );\
             INSERT INTO alert_deliveries SELECT * FROM alert_deliveries_legacy;\
             DROP TABLE alert_deliveries_legacy;\
             CREATE UNIQUE INDEX alert_deliveries_one_initial_kind \
               ON alert_deliveries(event_id, alert_channel_id, notification_kind) \
               WHERE notification_kind IN ('firing','resolved','test');\
             CREATE INDEX alert_deliveries_due \
               ON alert_deliveries(state, next_attempt_at_unix_ms, created_at_unix_ms);\
             COMMIT;",
        )
        .inspect_err(|_| {
            let _ = self.execute("ROLLBACK");
        })
    }

    fn seed_default_alert_rules(&self) -> Result<(), String> {
        for rule in default_alert_rules() {
            if self.read_alert_rule(&rule.rule_id)?.is_none() {
                self.put_alert_rule(&rule)?;
            }
        }
        Ok(())
    }

    fn seed_default_self_test_schedules(&self) -> Result<(), String> {
        for schedule in [
            SelfTestSchedule {
                kind: SelfTestKind::Daily,
                enabled: true,
                local_hour: 2,
                local_minute: 0,
                weekday: None,
                updated_at_unix_ms: 0,
                updated_by: "system-seed".into(),
            },
            SelfTestSchedule {
                kind: SelfTestKind::Weekly,
                enabled: true,
                local_hour: 3,
                local_minute: 0,
                weekday: Some(0),
                updated_at_unix_ms: 0,
                updated_by: "system-seed".into(),
            },
        ] {
            if self.read_self_test_schedule(schedule.kind)?.is_none() {
                self.put_self_test_schedule(&schedule)?;
            }
        }
        Ok(())
    }

    fn migrate_ingest_retry_schedule(&self) -> Result<(), String> {
        if self.ingest_retry_column_exists()? {
            return Ok(());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            if !self.ingest_retry_column_exists()? {
                self.execute(
                    "ALTER TABLE media_ingest_jobs ADD COLUMN next_attempt_at_unix_ms INTEGER NOT NULL \
                     DEFAULT -1 CHECK (next_attempt_at_unix_ms >= -1)",
                )?;
            }
            Ok(())
        })();
        self.finish_transaction(outcome)
    }

    fn migrate_channel_output_profile(&self) -> Result<(), String> {
        let statement = self.prepare(
            "SELECT COUNT(*) FROM pragma_table_info('channels') WHERE name='output_profile'",
        )?;
        let exists = match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => (unsafe { sqlite3_column_int64(statement.raw, 0) }) == 1,
            _ => return Err(sqlite_error(self.database)),
        };
        drop(statement);
        if !exists {
            self.execute(
                "ALTER TABLE channels ADD COLUMN output_profile TEXT NOT NULL \
                 DEFAULT 'proof-360p30'",
            )?;
        }
        Ok(())
    }

    fn migrate_channel_output_sink_kinds(&self) -> Result<(), String> {
        let statement = self.prepare(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='channel_output_sinks'",
        )?;
        let definition = match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => column_text(statement.raw, 0)?,
            SQLITE_DONE => return Err("channel_output_sinks schema is missing".into()),
            _ => return Err(sqlite_error(self.database)),
        };
        drop(statement);
        if definition.contains("'srt'") {
            return Ok(());
        }
        self.execute(
            "BEGIN IMMEDIATE;\
             ALTER TABLE channel_output_sinks RENAME TO channel_output_sinks_udp_legacy;\
             CREATE TABLE channel_output_sinks (\
               channel_id TEXT NOT NULL,\
               sink_id TEXT NOT NULL,\
               label TEXT NOT NULL,\
               kind TEXT NOT NULL CHECK (kind IN ('udp-ts','srt')),\
               destination TEXT NOT NULL,\
               loudness_target_milli INTEGER NOT NULL,\
               required INTEGER NOT NULL CHECK (required IN (0,1)),\
               enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),\
               PRIMARY KEY(channel_id, sink_id),\
               UNIQUE(channel_id, label COLLATE NOCASE),\
               FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE\
             ); \
             INSERT INTO channel_output_sinks(\
               channel_id, sink_id, label, kind, destination, loudness_target_milli, required, enabled) \
             SELECT channel_id, sink_id, label, kind, destination, loudness_target_milli, required, enabled \
             FROM channel_output_sinks_udp_legacy; \
             DROP TABLE channel_output_sinks_udp_legacy; \
             COMMIT",
        )
    }

    fn migrate_channel_output_sinks(&self) -> Result<(), String> {
        self.execute(
            "INSERT INTO channel_output_sinks(\
               channel_id, sink_id, label, kind, destination, loudness_target_milli, required, enabled) \
             SELECT channel_id, channel_id, 'Primary transport', 'udp-ts', udp_destination,\
               CASE output_profile WHEN 'atsc-720p5994' THEN -24000 ELSE -16000 END, 1, 1 \
             FROM channels WHERE NOT EXISTS (\
               SELECT 1 FROM channel_output_sinks WHERE channel_output_sinks.channel_id=channels.channel_id)",
        )
    }

    fn migrate_ingest_transcode_status(&self) -> Result<(), String> {
        let statement = self.prepare(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='media_ingest_jobs'",
        )?;
        let definition = match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => column_text(statement.raw, 0)?,
            SQLITE_DONE => return Err("media_ingest_jobs schema is missing".into()),
            _ => return Err(sqlite_error(self.database)),
        };
        drop(statement);
        if definition.contains("'awaiting_transcode'") {
            return Ok(());
        }
        self.execute(
            "BEGIN IMMEDIATE;\
             ALTER TABLE media_ingest_jobs RENAME TO media_ingest_jobs_legacy;\
             CREATE TABLE media_ingest_jobs (\
               job_id TEXT PRIMARY KEY,\
               source_kind TEXT NOT NULL CHECK (source_kind IN ('operator_path','watch_folder','live_finalization')),\
               source_path TEXT NOT NULL,\
               status TEXT NOT NULL CHECK (status IN ('pending','running','awaiting_transcode','completed','failed','cancelled')),\
               progress_percent INTEGER NOT NULL CHECK (progress_percent BETWEEN 0 AND 100),\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               started_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (started_at_unix_ms >= -1),\
               completed_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (completed_at_unix_ms >= -1),\
               error_detail TEXT NOT NULL DEFAULT '',\
               asset_id TEXT NOT NULL DEFAULT '',\
               attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),\
               next_attempt_at_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (next_attempt_at_unix_ms >= -1)\
             );\
             INSERT INTO media_ingest_jobs SELECT * FROM media_ingest_jobs_legacy;\
             DROP TABLE media_ingest_jobs_legacy;\
             CREATE INDEX media_ingest_jobs_status_time ON media_ingest_jobs(status, created_at_unix_ms);\
             COMMIT;",
        )
        .inspect_err(|_| {
            let _ = self.execute("ROLLBACK");
        })
    }

    fn ingest_retry_column_exists(&self) -> Result<bool, String> {
        let statement = self.prepare(
            "SELECT COUNT(*) FROM pragma_table_info('media_ingest_jobs') \
             WHERE name='next_attempt_at_unix_ms'",
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok((unsafe { sqlite3_column_int64(statement.raw, 0) }) == 1),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn upsert_profile(&self, profile: &StationProfile) -> Result<(), String> {
        let sql = "INSERT INTO station_profile(singleton, station_id, display_name, timezone) VALUES(1, ?1, ?2, ?3) \
                   ON CONFLICT(singleton) DO UPDATE SET station_id=excluded.station_id, display_name=excluded.display_name, \
                   timezone=excluded.timezone, revision=station_profile.revision+1";
        let statement = self.prepare(sql)?;
        let values = [
            CString::new(profile.station_id.as_str()),
            CString::new(profile.display_name.as_str()),
            CString::new(profile.timezone.as_str()),
        ];
        for (offset, value) in values.iter().enumerate() {
            let value = value
                .as_ref()
                .map_err(|_| "profile contains a null byte".to_string())?;
            // SAFETY: the statement is live and each CString outlives `sqlite3_step` below.
            let status = unsafe {
                sqlite3_bind_text(
                    statement.raw,
                    (offset + 1) as c_int,
                    value.as_ptr(),
                    -1,
                    ptr::null_mut(),
                )
            };
            if status != SQLITE_OK {
                return Err(sqlite_error(self.database));
            }
        }
        // SAFETY: all parameters are bound and the statement is live.
        let status = unsafe { sqlite3_step(statement.raw) };
        if status == SQLITE_DONE {
            Ok(())
        } else {
            Err(sqlite_error(self.database))
        }
    }

    fn prepare(&self, sql: &str) -> Result<Statement, String> {
        let sql = CString::new(sql).map_err(|_| "SQL contains a null byte".to_string())?;
        let mut statement = ptr::null_mut();
        // SAFETY: the database handle is live and the SQL is a valid C string.
        let status = unsafe {
            sqlite3_prepare_v2(
                self.database,
                sql.as_ptr(),
                -1,
                &mut statement,
                ptr::null_mut(),
            )
        };
        if status == SQLITE_OK {
            Ok(Statement { raw: statement })
        } else {
            Err(sqlite_error(self.database))
        }
    }

    fn changes(&self) -> i64 {
        let statement = self
            .prepare("SELECT changes()")
            .expect("SELECT changes is valid");
        let status = unsafe { sqlite3_step(statement.raw) };
        debug_assert_eq!(status, SQLITE_ROW);
        unsafe { sqlite3_column_int64(statement.raw, 0) }
    }

    fn finish_transaction(&self, outcome: Result<(), String>) -> Result<(), String> {
        match outcome {
            Ok(()) => match self.execute("COMMIT") {
                Ok(()) => Ok(()),
                Err(error) => {
                    let _ = self.execute("ROLLBACK");
                    Err(error)
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }
}

fn station_data_root(database_path: &Path) -> Result<PathBuf, String> {
    match database_path
        .parent()
        .filter(|path| !path.as_os_str().is_empty())
    {
        Some(path) => Ok(path.to_path_buf()),
        None => std::env::current_dir().map_err(|error| format!("resolve data root: {error}")),
    }
}

impl Drop for StationStore {
    fn drop(&mut self) {
        // SAFETY: `StationStore` owns this live handle and drops it exactly once.
        unsafe { sqlite3_close(self.database) };
    }
}

fn finish_cg_transaction<T>(
    store: &StationStore,
    outcome: Result<T, CgWriteError>,
) -> Result<T, CgWriteError> {
    match outcome {
        Ok(value) => match store.execute("COMMIT") {
            Ok(()) => Ok(value),
            Err(error) => {
                let _ = store.execute("ROLLBACK");
                Err(CgWriteError::Storage(error))
            }
        },
        Err(error) => {
            let _ = store.execute("ROLLBACK");
            Err(error)
        }
    }
}

struct Statement {
    raw: *mut SqliteStatement,
}

impl Drop for Statement {
    fn drop(&mut self) {
        // SAFETY: `Statement` owns this live handle and drops it exactly once.
        unsafe { sqlite3_finalize(self.raw) };
    }
}

fn bind_text(
    database: *mut Sqlite3,
    statement: *mut SqliteStatement,
    index: c_int,
    value: &CString,
) -> Result<(), String> {
    let status =
        unsafe { sqlite3_bind_text(statement, index, value.as_ptr(), -1, ptr::null_mut()) };
    if status == SQLITE_OK {
        Ok(())
    } else {
        Err(sqlite_error(database))
    }
}

fn bind_i64(
    database: *mut Sqlite3,
    statement: *mut SqliteStatement,
    index: c_int,
    value: i64,
) -> Result<(), String> {
    let status = unsafe { sqlite3_bind_int64(statement, index, value) };
    if status == SQLITE_OK {
        Ok(())
    } else {
        Err(sqlite_error(database))
    }
}

fn bind_blob(
    database: *mut Sqlite3,
    statement: *mut SqliteStatement,
    index: c_int,
    value: &[u8],
) -> Result<(), String> {
    let byte_count = c_int::try_from(value.len())
        .map_err(|_| "encrypted alert configuration exceeds SQLite's blob limit".to_string())?;
    let status = unsafe {
        sqlite3_bind_blob(
            statement,
            index,
            value.as_ptr().cast(),
            byte_count,
            ptr::null_mut(),
        )
    };
    if status == SQLITE_OK {
        Ok(())
    } else {
        Err(sqlite_error(database))
    }
}

fn expect_done(database: *mut Sqlite3, statement: *mut SqliteStatement) -> Result<(), String> {
    if unsafe { sqlite3_step(statement) } == SQLITE_DONE {
        Ok(())
    } else {
        Err(sqlite_error(database))
    }
}

fn c_string(value: &str, field: &str) -> Result<CString, String> {
    CString::new(value).map_err(|_| format!("{field} contains a null byte"))
}

fn checked_i64(value: u64, field: &str) -> Result<i64, String> {
    i64::try_from(value).map_err(|_| format!("{field} exceeds SQLite's integer range"))
}

fn read_media_asset_row(statement: *mut SqliteStatement) -> Result<MediaAsset, String> {
    Ok(MediaAsset {
        asset_id: column_text(statement, 0)?,
        media_path: column_text(statement, 1)?,
        duration_ms: column_u64(statement, 2, "asset duration")?,
        readiness: parse_readiness(&column_text(statement, 3)?)?,
    })
}

fn read_cg_image_asset_row(statement: *mut SqliteStatement) -> Result<CgImageAsset, String> {
    let asset = CgImageAsset {
        asset_id: column_text(statement, 0)?,
        mime_type: column_text(statement, 1)?,
        byte_size: u64::try_from(unsafe { sqlite3_column_int64(statement, 2) })
            .map_err(|_| "database returned an invalid CG image byte size".to_string())?,
        width: u32::try_from(unsafe { sqlite3_column_int64(statement, 3) })
            .map_err(|_| "database returned an invalid CG image width".to_string())?,
        height: u32::try_from(unsafe { sqlite3_column_int64(statement, 4) })
            .map_err(|_| "database returned an invalid CG image height".to_string())?,
        readiness: cg_image_readiness_from_name(&column_text(statement, 5)?)?,
    };
    asset.validate().map_err(str::to_string)?;
    Ok(asset)
}

fn read_channel_runtime_row(
    statement: *mut SqliteStatement,
) -> Result<ChannelRuntimeObservation, String> {
    let asset_id = column_text(statement, 2)?;
    let detail = column_text(statement, 5)?;
    let observation = ChannelRuntimeObservation {
        channel_id: column_text(statement, 0)?,
        state: parse_channel_runtime_state(&column_text(statement, 1)?)?,
        on_air_asset_id: (!asset_id.is_empty()).then_some(asset_id),
        state_started_at_unix_ms: unsafe { sqlite3_column_int64(statement, 3) },
        heartbeat_at_unix_ms: unsafe { sqlite3_column_int64(statement, 4) },
        detail: (!detail.is_empty()).then_some(detail),
    };
    observation.validate().map_err(str::to_string)?;
    Ok(observation)
}

fn read_cg_board_row(statement: *mut SqliteStatement) -> Result<CgBoard, String> {
    let background_audio = column_text(statement, 4)?;
    let board = CgBoard {
        board_id: column_text(statement, 0)?,
        channel_id: column_text(statement, 1)?,
        template_id: column_text(statement, 2)?,
        active: unsafe { sqlite3_column_int64(statement, 3) } == 1,
        background_audio_asset_id: (!background_audio.is_empty()).then_some(background_audio),
        revision: column_u64(statement, 5, "CG board revision")?,
        created_at_unix_ms: unsafe { sqlite3_column_int64(statement, 6) },
        updated_at_unix_ms: unsafe { sqlite3_column_int64(statement, 7) },
        created_by: column_text(statement, 8)?,
    };
    board.validate().map_err(str::to_string)?;
    Ok(board)
}

fn read_cg_zone_row(statement: *mut SqliteStatement) -> Result<CgZoneConfig, String> {
    let content_source_json = column_text(statement, 4)?;
    let tags_json = column_text(statement, 7)?;
    let rotation = unsafe { sqlite3_column_int64(statement, 5) };
    let zone = CgZoneConfig {
        zone_id: column_text(statement, 0)?,
        board_id: column_text(statement, 1)?,
        template_zone_key: column_text(statement, 2)?,
        zone_kind: parse_cg_zone_kind(&column_text(statement, 3)?)?,
        content_source: serde_json::from_str::<CgContentSource>(&content_source_json)
            .map_err(|error| format!("database returned an invalid CG content source: {error}"))?,
        rotation_seconds: (rotation >= 0)
            .then(|| u32::try_from(rotation))
            .transpose()
            .map_err(|_| "database returned an invalid CG rotation interval".to_string())?,
        approval_required: unsafe { sqlite3_column_int64(statement, 6) } == 1,
        allowed_tags: serde_json::from_str(&tags_json)
            .map_err(|error| format!("database returned invalid CG zone tags: {error}"))?,
        revision: column_u64(statement, 8, "CG zone revision")?,
        created_at_unix_ms: unsafe { sqlite3_column_int64(statement, 9) },
        updated_at_unix_ms: unsafe { sqlite3_column_int64(statement, 10) },
    };
    zone.validate().map_err(str::to_string)?;
    Ok(zone)
}

fn read_cg_feed_source_row(statement: *mut SqliteStatement) -> Result<CgFeedSource, String> {
    let last_fetch_error = column_text(statement, 14)?;
    let last_fetched = unsafe { sqlite3_column_int64(statement, 13) };
    let feed = CgFeedSource {
        feed_source_id: column_text(statement, 0)?,
        channel_id: column_text(statement, 1)?,
        kind: parse_cg_feed_kind(&column_text(statement, 2)?)?,
        label: column_text(statement, 3)?,
        source_url: column_text(statement, 4)?,
        trust_tier: parse_cg_feed_trust_tier(&column_text(statement, 5)?)?,
        tags: serde_json::from_str(&column_text(statement, 6)?)
            .map_err(|error| format!("database returned invalid CG feed tags: {error}"))?,
        refresh_seconds: u32::try_from(unsafe { sqlite3_column_int64(statement, 7) })
            .map_err(|_| "database returned an invalid CG feed refresh interval".to_string())?,
        enabled: unsafe { sqlite3_column_int64(statement, 8) } == 1,
        revision: column_u64(statement, 9, "CG feed revision")?,
        created_at_unix_ms: unsafe { sqlite3_column_int64(statement, 10) },
        updated_at_unix_ms: unsafe { sqlite3_column_int64(statement, 11) },
        created_by: column_text(statement, 12)?,
        last_fetched_at_unix_ms: (last_fetched >= 0).then_some(last_fetched),
        last_fetch_error: (!last_fetch_error.is_empty()).then_some(last_fetch_error),
    };
    feed.validate().map_err(str::to_string)?;
    Ok(feed)
}

fn read_cg_bulletin_row(statement: *mut SqliteStatement) -> Result<CgBulletin, String> {
    let content_json = column_text(statement, 2)?;
    let tags_json = column_text(statement, 4)?;
    let requested_start = unsafe { sqlite3_column_int64(statement, 5) };
    let requested_end = unsafe { sqlite3_column_int64(statement, 6) };
    let audio_asset_id = column_text(statement, 8)?;
    let moderation_notes = column_text(statement, 10)?;
    let moderated_by = column_text(statement, 11)?;
    let bulletin = CgBulletin {
        bulletin_id: column_text(statement, 0)?,
        channel_id: column_text(statement, 1)?,
        content: serde_json::from_str::<CgBulletinContent>(&content_json)
            .map_err(|error| format!("database returned invalid CG bulletin content: {error}"))?,
        state: parse_cg_bulletin_state(&column_text(statement, 3)?)?,
        tags: serde_json::from_str(&tags_json)
            .map_err(|error| format!("database returned invalid CG bulletin tags: {error}"))?,
        requested_start_at_unix_ms: (requested_start >= 0).then_some(requested_start),
        requested_end_at_unix_ms: (requested_end >= 0).then_some(requested_end),
        display_seconds: u32::try_from(unsafe { sqlite3_column_int64(statement, 7) })
            .map_err(|_| "database returned an invalid CG bulletin duration".to_string())?,
        audio_asset_id: (!audio_asset_id.is_empty()).then_some(audio_asset_id),
        submitted_by: column_text(statement, 9)?,
        moderation_notes: (!moderation_notes.is_empty()).then_some(moderation_notes),
        moderated_by: (!moderated_by.is_empty()).then_some(moderated_by),
        revision: column_u64(statement, 12, "CG bulletin revision")?,
        created_at_unix_ms: unsafe { sqlite3_column_int64(statement, 13) },
        updated_at_unix_ms: unsafe { sqlite3_column_int64(statement, 14) },
    };
    bulletin.validate().map_err(str::to_string)?;
    Ok(bulletin)
}

fn read_cg_feed_item_approval_row(
    statement: *mut SqliteStatement,
) -> Result<CgFeedItemApproval, String> {
    let approval = CgFeedItemApproval {
        feed_source_id: column_text(statement, 0)?,
        item_id: column_text(statement, 1)?,
        approved_by: column_text(statement, 2)?,
        approved_at_unix_ms: unsafe { sqlite3_column_int64(statement, 3) },
    };
    approval.validate().map_err(str::to_string)?;
    Ok(approval)
}

fn read_cg_audit_row(statement: *mut SqliteStatement) -> Result<CgAuditEvent, String> {
    let board_id = column_text(statement, 1)?;
    let details_json = column_text(statement, 6)?;
    let audit = CgAuditEvent {
        audit_id: column_text(statement, 0)?,
        board_id: (!board_id.is_empty()).then_some(board_id),
        channel_id: column_text(statement, 2)?,
        event_kind: parse_cg_audit_event_kind(&column_text(statement, 3)?)?,
        operator_id: column_text(statement, 4)?,
        occurred_at_unix_ms: unsafe { sqlite3_column_int64(statement, 5) },
        details: serde_json::from_str::<BTreeMap<String, String>>(&details_json)
            .map_err(|error| format!("database returned invalid CG audit details: {error}"))?,
    };
    audit.validate().map_err(str::to_string)?;
    Ok(audit)
}

fn read_system_resource_sample_row(
    statement: *mut SqliteStatement,
) -> Result<SystemResourceSample, String> {
    let sample = SystemResourceSample {
        sample_id: Some(column_u64(statement, 0, "system resource sample identity")?),
        sampled_at_unix_ms: unsafe { sqlite3_column_int64(statement, 1) },
        cpu_percent: unscaled_metric(unsafe { sqlite3_column_int64(statement, 2) }),
        ram_used_gb: unscaled_metric(unsafe { sqlite3_column_int64(statement, 3) }),
        ram_total_gb: unscaled_metric(unsafe { sqlite3_column_int64(statement, 4) }),
        gpu_percent: unscaled_metric(unsafe { sqlite3_column_int64(statement, 5) }),
        gpu_vram_used_gb: unscaled_metric(unsafe { sqlite3_column_int64(statement, 6) }),
        media_volume_free_gb: unscaled_metric(unsafe { sqlite3_column_int64(statement, 7) }),
        backup_volume_free_gb: unscaled_metric(unsafe { sqlite3_column_int64(statement, 8) }),
        database_reachable: unsafe { sqlite3_column_int64(statement, 9) } == 1,
        backup_volume_writable: unsafe { sqlite3_column_int64(statement, 10) } == 1,
        service_running: unsafe { sqlite3_column_int64(statement, 11) } == 1,
        clock_skew_seconds: unscaled_signed_metric(unsafe { sqlite3_column_int64(statement, 12) }),
    };
    sample.validate().map_err(str::to_string)?;
    Ok(sample)
}

fn read_self_test_schedule_row(
    statement: *mut SqliteStatement,
) -> Result<SelfTestSchedule, String> {
    let weekday = unsafe { sqlite3_column_int64(statement, 4) };
    let schedule = SelfTestSchedule {
        kind: parse_self_test_kind(&column_text(statement, 0)?)?,
        enabled: unsafe { sqlite3_column_int64(statement, 1) } == 1,
        local_hour: u8::try_from(unsafe { sqlite3_column_int64(statement, 2) })
            .map_err(|_| "database returned an invalid self-test hour".to_string())?,
        local_minute: u8::try_from(unsafe { sqlite3_column_int64(statement, 3) })
            .map_err(|_| "database returned an invalid self-test minute".to_string())?,
        weekday: (weekday >= 0)
            .then(|| u8::try_from(weekday))
            .transpose()
            .map_err(|_| "database returned an invalid self-test weekday".to_string())?,
        updated_at_unix_ms: unsafe { sqlite3_column_int64(statement, 5) },
        updated_by: column_text(statement, 6)?,
    };
    schedule.validate().map_err(str::to_string)?;
    Ok(schedule)
}

fn read_system_self_test_row(statement: *mut SqliteStatement) -> Result<SystemSelfTest, String> {
    let period_key = column_text(statement, 3)?;
    let evidence_path = column_text(statement, 9)?;
    let checks_json = column_text(statement, 7)?;
    let record = SystemSelfTest {
        self_test_id: column_text(statement, 0)?,
        kind: parse_self_test_kind(&column_text(statement, 1)?)?,
        trigger: parse_self_test_trigger(&column_text(statement, 2)?)?,
        schedule_period_key: (!period_key.is_empty()).then_some(period_key),
        started_at_unix_ms: unsafe { sqlite3_column_int64(statement, 4) },
        finished_at_unix_ms: unsafe { sqlite3_column_int64(statement, 5) },
        status: parse_self_test_status(&column_text(statement, 6)?)?,
        checks: serde_json::from_str(&checks_json)
            .map_err(|error| format!("database returned invalid self-test checks: {error}"))?,
        summary: column_text(statement, 8)?,
        evidence_path: (!evidence_path.is_empty()).then_some(evidence_path),
    };
    record.validate().map_err(str::to_string)?;
    Ok(record)
}

fn read_alert_event_row(statement: *mut SqliteStatement) -> Result<AlertEvent, String> {
    let resolved = unsafe { sqlite3_column_int64(statement, 10) };
    let acknowledged = unsafe { sqlite3_column_int64(statement, 12) };
    let acknowledged_by = column_text(statement, 13)?;
    let event = AlertEvent {
        event_id: column_text(statement, 0)?,
        condition: parse_alert_condition(&column_text(statement, 1)?)?,
        severity: parse_alert_severity(&column_text(statement, 2)?)?,
        state: parse_alert_event_state(&column_text(statement, 3)?)?,
        resource_ref: column_text(statement, 4)?,
        summary: column_text(statement, 5)?,
        detail: column_text(statement, 6)?,
        source_section: column_text(statement, 7)?,
        first_observed_at_unix_ms: unsafe { sqlite3_column_int64(statement, 8) },
        last_observed_at_unix_ms: unsafe { sqlite3_column_int64(statement, 9) },
        resolved_at_unix_ms: (resolved >= 0).then_some(resolved),
        occurrence_count: column_u64(statement, 11, "alert occurrence count")?,
        acknowledged_at_unix_ms: (acknowledged >= 0).then_some(acknowledged),
        acknowledged_by: (!acknowledged_by.is_empty()).then_some(acknowledged_by),
    };
    event.validate().map_err(str::to_string)?;
    Ok(event)
}

fn read_alert_rule_row(statement: *mut SqliteStatement) -> Result<AlertRule, String> {
    let channel_ids_json = column_text(statement, 4)?;
    let channel_ids = serde_json::from_str(&channel_ids_json)
        .map_err(|error| format!("database returned invalid alert rule destinations: {error}"))?;
    let scope_channel_id = column_text(statement, 7)?;
    let rule = AlertRule {
        rule_id: column_text(statement, 0)?,
        condition: parse_alert_condition(&column_text(statement, 1)?)?,
        enabled: unsafe { sqlite3_column_int64(statement, 2) } == 1,
        severity: parse_alert_severity(&column_text(statement, 3)?)?,
        channel_ids,
        dedupe_window_seconds: u32::try_from(unsafe { sqlite3_column_int64(statement, 5) })
            .map_err(|_| "database returned an invalid alert-rule dedupe window".to_string())?,
        re_alert_after_seconds: u32::try_from(unsafe { sqlite3_column_int64(statement, 6) })
            .map_err(|_| "database returned an invalid alert-rule re-alert window".to_string())?,
        scope_channel_id: (!scope_channel_id.is_empty()).then_some(scope_channel_id),
        notify_on_resolve: unsafe { sqlite3_column_int64(statement, 8) } == 1,
        updated_at_unix_ms: unsafe { sqlite3_column_int64(statement, 9) },
        updated_by: column_text(statement, 10)?,
    };
    rule.validate().map_err(str::to_string)?;
    Ok(rule)
}

fn read_alert_channel_row(statement: *mut SqliteStatement) -> Result<AlertChannel, String> {
    let quiet_start = column_text(statement, 6)?;
    let quiet_end = column_text(statement, 7)?;
    let channel = AlertChannel {
        channel_id: column_text(statement, 0)?,
        kind: parse_alert_channel_kind(&column_text(statement, 1)?)?,
        label: column_text(statement, 2)?,
        enabled: unsafe { sqlite3_column_int64(statement, 3) } == 1,
        target_redacted: column_text(statement, 4)?,
        credential_configured: unsafe { sqlite3_column_int64(statement, 5) } == 1,
        quiet_hours_start_utc: (!quiet_start.is_empty()).then_some(quiet_start),
        quiet_hours_end_utc: (!quiet_end.is_empty()).then_some(quiet_end),
        created_at_unix_ms: unsafe { sqlite3_column_int64(statement, 8) },
        updated_at_unix_ms: unsafe { sqlite3_column_int64(statement, 9) },
    };
    channel.validate().map_err(str::to_string)?;
    Ok(channel)
}

fn read_alert_delivery_row(statement: *mut SqliteStatement) -> Result<AlertDelivery, String> {
    let lease_expires = unsafe { sqlite3_column_int64(statement, 7) };
    let signature = column_text(statement, 9)?;
    let sent_at = unsafe { sqlite3_column_int64(statement, 12) };
    let delivery = AlertDelivery {
        delivery_id: column_text(statement, 0)?,
        event_id: column_text(statement, 1)?,
        alert_channel_id: column_text(statement, 2)?,
        notification_kind: parse_alert_notification_kind(&column_text(statement, 3)?)?,
        state: parse_alert_delivery_state(&column_text(statement, 4)?)?,
        attempts: u32::try_from(unsafe { sqlite3_column_int64(statement, 5) })
            .map_err(|_| "database returned an invalid alert delivery attempt count".to_string())?,
        next_attempt_at_unix_ms: unsafe { sqlite3_column_int64(statement, 6) },
        lease_expires_at_unix_ms: (lease_expires >= 0).then_some(lease_expires),
        last_error: column_text(statement, 8)?,
        signature: (!signature.is_empty()).then_some(signature),
        created_at_unix_ms: unsafe { sqlite3_column_int64(statement, 10) },
        updated_at_unix_ms: unsafe { sqlite3_column_int64(statement, 11) },
        sent_at_unix_ms: (sent_at >= 0).then_some(sent_at),
    };
    delivery.validate().map_err(str::to_string)?;
    Ok(delivery)
}

fn validate_new_alert_observation(proposed: &AlertEvent) -> Result<(), String> {
    proposed.validate().map_err(str::to_string)?;
    if proposed.state != AlertEventState::Firing
        || proposed.occurrence_count != 1
        || proposed.first_observed_at_unix_ms != proposed.last_observed_at_unix_ms
        || proposed.acknowledged_at_unix_ms.is_some()
        || proposed.acknowledged_by.is_some()
    {
        return Err("a new alert observation must be an unacknowledged first firing".into());
    }
    Ok(())
}

fn scaled_metric(value: Option<f64>) -> Result<i64, String> {
    match value {
        None => Ok(-1),
        Some(value) if value.is_finite() && value >= 0.0 => {
            let scaled = value * 1_000.0;
            if scaled > i64::MAX as f64 {
                Err("system resource metric is too large".into())
            } else {
                Ok(scaled.round() as i64)
            }
        }
        Some(_) => Err("system resource metric is invalid".into()),
    }
}

fn unscaled_metric(value: i64) -> Option<f64> {
    (value >= 0).then_some(value as f64 / 1_000.0)
}

fn scaled_signed_metric(value: Option<f64>) -> Result<i64, String> {
    match value {
        None => Ok(i64::MIN),
        Some(value) if value.is_finite() => {
            let scaled = value * 1_000.0;
            if scaled <= i64::MIN as f64 || scaled > i64::MAX as f64 {
                Err("signed system resource metric is too large".into())
            } else {
                Ok(scaled.round() as i64)
            }
        }
        Some(_) => Err("signed system resource metric is invalid".into()),
    }
}

fn unscaled_signed_metric(value: i64) -> Option<f64> {
    (value != i64::MIN).then_some(value as f64 / 1_000.0)
}

fn deterministic_alert_delivery_id(
    event_id: &str,
    alert_channel_id: &str,
    notification_kind: AlertNotificationKind,
    observed_at_unix_ms: i64,
) -> String {
    let mut digest = Sha256::new();
    digest.update(event_id.as_bytes());
    digest.update([0]);
    digest.update(alert_channel_id.as_bytes());
    digest.update([0]);
    digest.update(alert_notification_kind_name(notification_kind).as_bytes());
    if notification_kind == AlertNotificationKind::Realert {
        digest.update([0]);
        digest.update(observed_at_unix_ms.to_le_bytes());
    }
    format!("alert-delivery-{:x}", digest.finalize())
}

fn deterministic_cg_expiration_audit_id(
    channel_id: &str,
    bulletin_id: &str,
    now_unix_ms: i64,
) -> String {
    let mut digest = Sha256::new();
    digest.update(b"townlight-cg-bulletin-expired");
    digest.update([0]);
    digest.update(channel_id.as_bytes());
    digest.update([0]);
    digest.update(bulletin_id.as_bytes());
    digest.update([0]);
    digest.update(now_unix_ms.to_le_bytes());
    let hex = format!("{:x}", digest.finalize());
    format!(
        "{}-{}-{}-{}-{}",
        &hex[0..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..32]
    )
}

fn quiet_hours_release(
    channel: &AlertChannel,
    severity: AlertSeverity,
    observed_at_unix_ms: i64,
) -> Result<i64, String> {
    if severity == AlertSeverity::Critical {
        return Ok(observed_at_unix_ms);
    }
    let (Some(start), Some(end)) = (
        channel.quiet_hours_start_utc.as_deref(),
        channel.quiet_hours_end_utc.as_deref(),
    ) else {
        return Ok(observed_at_unix_ms);
    };
    let start_ms = i64::from(parse_utc_minutes(start)?) * 60_000;
    let end_ms = i64::from(parse_utc_minutes(end)?) * 60_000;
    const DAY_MS: i64 = 86_400_000;
    let current_ms = observed_at_unix_ms.rem_euclid(DAY_MS);
    let release_delta = if start_ms < end_ms && (start_ms..end_ms).contains(&current_ms) {
        Some(end_ms - current_ms)
    } else if start_ms > end_ms && current_ms >= start_ms {
        Some(DAY_MS - current_ms + end_ms)
    } else if start_ms > end_ms && current_ms < end_ms {
        Some(end_ms - current_ms)
    } else {
        None
    };
    match release_delta {
        Some(delta) => observed_at_unix_ms
            .checked_add(delta)
            .ok_or_else(|| "quiet-hours release time overflowed".to_string()),
        None => Ok(observed_at_unix_ms),
    }
}

fn parse_utc_minutes(value: &str) -> Result<u16, String> {
    let (hours, minutes) = value
        .split_once(':')
        .ok_or_else(|| "quiet-hours clock is invalid".to_string())?;
    let hours = hours
        .parse::<u16>()
        .map_err(|_| "quiet-hours clock is invalid".to_string())?;
    let minutes = minutes
        .parse::<u16>()
        .map_err(|_| "quiet-hours clock is invalid".to_string())?;
    if hours >= 24 || minutes >= 60 {
        return Err("quiet-hours clock is invalid".into());
    }
    Ok(hours * 60 + minutes)
}

fn read_asset_loudness_row(
    statement: *mut SqliteStatement,
) -> Result<AssetLoudnessAnalysis, String> {
    let measured = unsafe { sqlite3_column_int64(statement, 2) };
    let detail = column_text(statement, 4)?;
    let analysis = AssetLoudnessAnalysis {
        asset_id: column_text(statement, 0)?,
        status: parse_loudness_status(&column_text(statement, 1)?)?,
        measured_lufs_milli: if measured == i64::from(i32::MIN) {
            None
        } else {
            Some(
                i32::try_from(measured)
                    .map_err(|_| "database returned invalid measured loudness".to_string())?,
            )
        },
        target_lufs_milli: i32::try_from(unsafe { sqlite3_column_int64(statement, 3) })
            .map_err(|_| "database returned invalid target loudness".to_string())?,
        detail: (!detail.is_empty()).then_some(detail),
    };
    analysis.validate().map_err(|error| format!("{error:?}"))?;
    Ok(analysis)
}

fn read_ingest_job_row(statement: *mut SqliteStatement) -> Result<MediaIngestJob, String> {
    let started = unsafe { sqlite3_column_int64(statement, 6) };
    let completed = unsafe { sqlite3_column_int64(statement, 7) };
    let error_detail = column_text(statement, 8)?;
    let asset_id = column_text(statement, 9)?;
    let next_attempt = unsafe { sqlite3_column_int64(statement, 11) };
    Ok(MediaIngestJob {
        job_id: column_text(statement, 0)?,
        source_kind: parse_ingest_source_kind(&column_text(statement, 1)?)?,
        source_path: column_text(statement, 2)?,
        status: parse_ingest_job_status(&column_text(statement, 3)?)?,
        progress_percent: u8::try_from(unsafe { sqlite3_column_int64(statement, 4) })
            .map_err(|_| "database returned invalid ingest progress".to_string())?,
        created_at_unix_ms: unsafe { sqlite3_column_int64(statement, 5) },
        started_at_unix_ms: (started >= 0).then_some(started),
        completed_at_unix_ms: (completed >= 0).then_some(completed),
        error_detail: (!error_detail.is_empty()).then_some(error_detail),
        asset_id: (!asset_id.is_empty()).then_some(asset_id),
        attempts: u32::try_from(unsafe { sqlite3_column_int64(statement, 10) })
            .map_err(|_| "database returned invalid ingest attempts".to_string())?,
        next_attempt_at_unix_ms: (next_attempt >= 0).then_some(next_attempt),
    })
}

fn read_transcode_job_row(statement: *mut SqliteStatement) -> Result<TranscodeJob, String> {
    let started = unsafe { sqlite3_column_int64(statement, 8) };
    let completed = unsafe { sqlite3_column_int64(statement, 9) };
    let output_asset_id = column_text(statement, 10)?;
    let error_detail = column_text(statement, 11)?;
    let next_attempt = unsafe { sqlite3_column_int64(statement, 13) };
    let job = TranscodeJob {
        job_id: column_text(statement, 0)?,
        ingest_job_id: column_text(statement, 1)?,
        source_asset_id: column_text(statement, 2)?,
        source_path: column_text(statement, 3)?,
        output_format: parse_transcode_format(&column_text(statement, 4)?)?,
        status: parse_transcode_job_status(&column_text(statement, 5)?)?,
        progress_percent: u8::try_from(unsafe { sqlite3_column_int64(statement, 6) })
            .map_err(|_| "database returned invalid transcode progress".to_string())?,
        created_at_unix_ms: unsafe { sqlite3_column_int64(statement, 7) },
        started_at_unix_ms: (started >= 0).then_some(started),
        completed_at_unix_ms: (completed >= 0).then_some(completed),
        output_asset_id: (!output_asset_id.is_empty()).then_some(output_asset_id),
        error_detail: (!error_detail.is_empty()).then_some(error_detail),
        attempts: u32::try_from(unsafe { sqlite3_column_int64(statement, 12) })
            .map_err(|_| "database returned invalid transcode attempts".to_string())?,
        next_attempt_at_unix_ms: (next_attempt >= 0).then_some(next_attempt),
    };
    job.validate().map_err(|error| format!("{error:?}"))?;
    Ok(job)
}

fn read_watch_folder_row(statement: *mut SqliteStatement) -> Result<WatchFolderConfig, String> {
    let last_scan = unsafe { sqlite3_column_int64(statement, 6) };
    let last_error = column_text(statement, 7)?;
    let config = WatchFolderConfig {
        config_id: column_text(statement, 0)?,
        display_name: column_text(statement, 1)?,
        monitor_path: column_text(statement, 2)?,
        extensions: serde_json::from_str(&column_text(statement, 3)?).map_err(|error| {
            format!("database returned invalid watch-folder extensions: {error}")
        })?,
        settle_seconds: u32::try_from(unsafe { sqlite3_column_int64(statement, 4) })
            .map_err(|_| "database returned invalid watch-folder settle time".to_string())?,
        enabled: unsafe { sqlite3_column_int64(statement, 5) } == 1,
        last_scan_at_unix_ms: (last_scan >= 0).then_some(last_scan),
        last_error: (!last_error.is_empty()).then_some(last_error),
    };
    config.validate().map_err(|error| format!("{error:?}"))?;
    Ok(config)
}

fn read_watch_file_observation_row(
    statement: *mut SqliteStatement,
) -> Result<WatchFileObservation, String> {
    let queued = column_text(statement, 6)?;
    Ok(WatchFileObservation {
        config_id: column_text(statement, 0)?,
        source_path: column_text(statement, 1)?,
        size_bytes: column_u64(statement, 2, "watch-file size")?,
        modified_at_unix_ms: unsafe { sqlite3_column_int64(statement, 3) },
        unchanged_since_unix_ms: unsafe { sqlite3_column_int64(statement, 4) },
        observation_count: u32::try_from(unsafe { sqlite3_column_int64(statement, 5) })
            .map_err(|_| "database returned invalid watch-file observation count".to_string())?,
        queued_job_id: (!queued.is_empty()).then_some(queued),
    })
}

fn read_schedule_item_row(statement: *mut SqliteStatement) -> Result<ScheduleItem, String> {
    Ok(ScheduleItem {
        item_id: column_text(statement, 0)?,
        channel_id: column_text(statement, 1)?,
        asset_id: column_text(statement, 2)?,
        title: column_text(statement, 3)?,
        starts_at_unix_ms: unsafe { sqlite3_column_int64(statement, 4) },
        duration_ms: column_u64(statement, 5, "schedule duration")?,
        state: parse_schedule_state(&column_text(statement, 6)?)?,
    })
}

fn read_commit_report_row(statement: *mut SqliteStatement) -> Result<CommitReport, String> {
    Ok(CommitReport {
        report_id: column_text(statement, 0)?,
        plan_id: column_text(statement, 1)?,
        channel_id: column_text(statement, 2)?,
        schedule_item_id: column_text(statement, 3)?,
        asset_id: column_text(statement, 4)?,
        approved_by: column_text(statement, 5)?,
        approved_at_unix_ms: unsafe { sqlite3_column_int64(statement, 6) },
        dispatch_status: parse_dispatch_status(&column_text(statement, 7)?)?,
        operator_notes: column_text(statement, 8)?,
    })
}

fn column_u64(statement: *mut SqliteStatement, column: c_int, field: &str) -> Result<u64, String> {
    u64::try_from(unsafe { sqlite3_column_int64(statement, column) })
        .map_err(|_| format!("database returned an invalid {field}"))
}

fn readiness_name(value: AssetReadiness) -> &'static str {
    match value {
        AssetReadiness::Ready => "ready",
        AssetReadiness::Missing => "missing",
        AssetReadiness::Rejected => "rejected",
        AssetReadiness::Processing => "processing",
    }
}

fn cg_image_readiness_name(value: CgImageReadiness) -> &'static str {
    match value {
        CgImageReadiness::Ready => "ready",
        CgImageReadiness::Missing => "missing",
        CgImageReadiness::Rejected => "rejected",
        CgImageReadiness::Processing => "processing",
    }
}

fn cg_image_readiness_from_name(value: &str) -> Result<CgImageReadiness, String> {
    match value {
        "ready" => Ok(CgImageReadiness::Ready),
        "missing" => Ok(CgImageReadiness::Missing),
        "rejected" => Ok(CgImageReadiness::Rejected),
        "processing" => Ok(CgImageReadiness::Processing),
        _ => Err("database returned an invalid CG image readiness".into()),
    }
}

fn output_sink_kind_name(value: OutputSinkKind) -> &'static str {
    match value {
        OutputSinkKind::UdpTs => "udp-ts",
        OutputSinkKind::Srt => "srt",
    }
}

fn cg_zone_kind_name(value: CgZoneKind) -> &'static str {
    match value {
        CgZoneKind::Primary => "primary",
        CgZoneKind::Sidebar => "sidebar",
        CgZoneKind::LowerThird => "lower_third",
        CgZoneKind::Ticker => "ticker",
        CgZoneKind::Schedule => "schedule",
        CgZoneKind::Sponsor => "sponsor",
        CgZoneKind::Logo => "logo",
        CgZoneKind::Alert => "alert",
        CgZoneKind::Clock => "clock",
        CgZoneKind::LiveVideo => "live_video",
    }
}

fn parse_cg_zone_kind(value: &str) -> Result<CgZoneKind, String> {
    match value {
        "primary" => Ok(CgZoneKind::Primary),
        "sidebar" => Ok(CgZoneKind::Sidebar),
        "lower_third" => Ok(CgZoneKind::LowerThird),
        "ticker" => Ok(CgZoneKind::Ticker),
        "schedule" => Ok(CgZoneKind::Schedule),
        "sponsor" => Ok(CgZoneKind::Sponsor),
        "logo" => Ok(CgZoneKind::Logo),
        "alert" => Ok(CgZoneKind::Alert),
        "clock" => Ok(CgZoneKind::Clock),
        "live_video" => Ok(CgZoneKind::LiveVideo),
        _ => Err(format!("database returned unknown CG zone kind: {value}")),
    }
}

fn cg_feed_kind_name(value: CgFeedKind) -> &'static str {
    match value {
        CgFeedKind::Rss => "rss",
        CgFeedKind::Ical => "ical",
        CgFeedKind::Caldav => "caldav",
        CgFeedKind::Weather => "weather",
        CgFeedKind::Social => "social",
    }
}

fn parse_cg_feed_kind(value: &str) -> Result<CgFeedKind, String> {
    match value {
        "rss" => Ok(CgFeedKind::Rss),
        "ical" => Ok(CgFeedKind::Ical),
        "caldav" => Ok(CgFeedKind::Caldav),
        "weather" => Ok(CgFeedKind::Weather),
        "social" => Ok(CgFeedKind::Social),
        _ => Err(format!("database returned unknown CG feed kind: {value}")),
    }
}

fn cg_feed_trust_tier_name(value: CgFeedTrustTier) -> &'static str {
    match value {
        CgFeedTrustTier::Station => "station",
        CgFeedTrustTier::Trusted => "trusted",
        CgFeedTrustTier::Moderated => "moderated",
    }
}

fn parse_cg_feed_trust_tier(value: &str) -> Result<CgFeedTrustTier, String> {
    match value {
        "station" => Ok(CgFeedTrustTier::Station),
        "trusted" => Ok(CgFeedTrustTier::Trusted),
        "moderated" => Ok(CgFeedTrustTier::Moderated),
        _ => Err(format!(
            "database returned unknown CG feed trust tier: {value}"
        )),
    }
}

fn cg_bulletin_state_name(value: CgBulletinState) -> &'static str {
    match value {
        CgBulletinState::Submitted => "submitted",
        CgBulletinState::NeedsChanges => "needs_changes",
        CgBulletinState::Accepted => "accepted",
        CgBulletinState::Scheduled => "scheduled",
        CgBulletinState::Declined => "declined",
        CgBulletinState::Expired => "expired",
    }
}

fn parse_cg_bulletin_state(value: &str) -> Result<CgBulletinState, String> {
    match value {
        "submitted" => Ok(CgBulletinState::Submitted),
        "needs_changes" => Ok(CgBulletinState::NeedsChanges),
        "accepted" => Ok(CgBulletinState::Accepted),
        "scheduled" => Ok(CgBulletinState::Scheduled),
        "declined" => Ok(CgBulletinState::Declined),
        "expired" => Ok(CgBulletinState::Expired),
        _ => Err(format!(
            "database returned unknown CG bulletin state: {value}"
        )),
    }
}

fn valid_cg_bulletin_transition(from: CgBulletinState, to: CgBulletinState) -> bool {
    matches!(
        (from, to),
        (CgBulletinState::Submitted, CgBulletinState::Submitted)
            | (CgBulletinState::Submitted, CgBulletinState::NeedsChanges)
            | (CgBulletinState::Submitted, CgBulletinState::Accepted)
            | (CgBulletinState::Submitted, CgBulletinState::Scheduled)
            | (CgBulletinState::Submitted, CgBulletinState::Declined)
            | (CgBulletinState::NeedsChanges, CgBulletinState::Submitted)
            | (CgBulletinState::Accepted, CgBulletinState::Accepted)
            | (CgBulletinState::Accepted, CgBulletinState::Scheduled)
            | (CgBulletinState::Accepted, CgBulletinState::Declined)
            | (CgBulletinState::Accepted, CgBulletinState::Expired)
            | (CgBulletinState::Scheduled, CgBulletinState::Accepted)
            | (CgBulletinState::Scheduled, CgBulletinState::Scheduled)
            | (CgBulletinState::Scheduled, CgBulletinState::Declined)
            | (CgBulletinState::Scheduled, CgBulletinState::Expired)
    )
}

fn audit_matches_cg_bulletin_state(event_kind: CgAuditEventKind, state: CgBulletinState) -> bool {
    matches!(
        (event_kind, state),
        (
            CgAuditEventKind::BulletinSubmitted,
            CgBulletinState::Submitted
        ) | (
            CgAuditEventKind::BulletinModerated,
            CgBulletinState::NeedsChanges
                | CgBulletinState::Accepted
                | CgBulletinState::Scheduled
                | CgBulletinState::Declined
        ) | (CgAuditEventKind::BulletinExpired, CgBulletinState::Expired)
    )
}

fn cg_audit_event_kind_name(value: CgAuditEventKind) -> &'static str {
    match value {
        CgAuditEventKind::BoardCreated => "board_created",
        CgAuditEventKind::BoardUpdated => "board_updated",
        CgAuditEventKind::ZoneCreated => "zone_created",
        CgAuditEventKind::ZoneUpdated => "zone_updated",
        CgAuditEventKind::ZoneRemoved => "zone_removed",
        CgAuditEventKind::FeedCreated => "feed_created",
        CgAuditEventKind::FeedUpdated => "feed_updated",
        CgAuditEventKind::FeedRemoved => "feed_removed",
        CgAuditEventKind::BulletinSubmitted => "bulletin_submitted",
        CgAuditEventKind::BulletinModerated => "bulletin_moderated",
        CgAuditEventKind::BulletinExpired => "bulletin_expired",
        CgAuditEventKind::FeedItemApproved => "feed_item_approved",
    }
}

fn parse_cg_audit_event_kind(value: &str) -> Result<CgAuditEventKind, String> {
    match value {
        "board_created" => Ok(CgAuditEventKind::BoardCreated),
        "board_updated" => Ok(CgAuditEventKind::BoardUpdated),
        "zone_created" => Ok(CgAuditEventKind::ZoneCreated),
        "zone_updated" => Ok(CgAuditEventKind::ZoneUpdated),
        "zone_removed" => Ok(CgAuditEventKind::ZoneRemoved),
        "feed_created" => Ok(CgAuditEventKind::FeedCreated),
        "feed_updated" => Ok(CgAuditEventKind::FeedUpdated),
        "feed_removed" => Ok(CgAuditEventKind::FeedRemoved),
        "bulletin_submitted" => Ok(CgAuditEventKind::BulletinSubmitted),
        "bulletin_moderated" => Ok(CgAuditEventKind::BulletinModerated),
        "bulletin_expired" => Ok(CgAuditEventKind::BulletinExpired),
        "feed_item_approved" => Ok(CgAuditEventKind::FeedItemApproved),
        _ => Err(format!(
            "database returned unknown CG audit event kind: {value}"
        )),
    }
}

fn parse_output_sink_kind(value: &str) -> Result<OutputSinkKind, String> {
    match value {
        "udp-ts" => Ok(OutputSinkKind::UdpTs),
        "srt" => Ok(OutputSinkKind::Srt),
        _ => Err(format!("unknown output sink kind: {value}")),
    }
}

fn channel_runtime_state_name(value: ChannelRuntimeState) -> &'static str {
    match value {
        ChannelRuntimeState::Starting => "starting",
        ChannelRuntimeState::HealthyFallback => "healthy_fallback",
        ChannelRuntimeState::OnAir => "on_air",
        ChannelRuntimeState::Error => "error",
        ChannelRuntimeState::Stopped => "stopped",
    }
}

fn alert_channel_kind_name(value: AlertChannelKind) -> &'static str {
    match value {
        AlertChannelKind::Email => "email",
        AlertChannelKind::Sms => "sms",
        AlertChannelKind::Webhook => "webhook",
    }
}

fn parse_alert_channel_kind(value: &str) -> Result<AlertChannelKind, String> {
    match value {
        "email" => Ok(AlertChannelKind::Email),
        "sms" => Ok(AlertChannelKind::Sms),
        "webhook" => Ok(AlertChannelKind::Webhook),
        _ => Err(format!(
            "database returned invalid alert channel kind {value}"
        )),
    }
}

fn alert_notification_kind_name(value: AlertNotificationKind) -> &'static str {
    match value {
        AlertNotificationKind::Firing => "firing",
        AlertNotificationKind::Realert => "realert",
        AlertNotificationKind::Resolved => "resolved",
        AlertNotificationKind::Test => "test",
    }
}

fn parse_alert_notification_kind(value: &str) -> Result<AlertNotificationKind, String> {
    match value {
        "firing" => Ok(AlertNotificationKind::Firing),
        "realert" => Ok(AlertNotificationKind::Realert),
        "resolved" => Ok(AlertNotificationKind::Resolved),
        "test" => Ok(AlertNotificationKind::Test),
        _ => Err(format!(
            "database returned invalid alert notification kind {value}"
        )),
    }
}

fn parse_alert_delivery_state(value: &str) -> Result<AlertDeliveryState, String> {
    match value {
        "pending" => Ok(AlertDeliveryState::Pending),
        "leased" => Ok(AlertDeliveryState::Leased),
        "sent" => Ok(AlertDeliveryState::Sent),
        "dead_letter" => Ok(AlertDeliveryState::DeadLetter),
        _ => Err(format!(
            "database returned invalid alert delivery state {value}"
        )),
    }
}

fn parse_channel_runtime_state(value: &str) -> Result<ChannelRuntimeState, String> {
    match value {
        "starting" => Ok(ChannelRuntimeState::Starting),
        "healthy_fallback" => Ok(ChannelRuntimeState::HealthyFallback),
        "on_air" => Ok(ChannelRuntimeState::OnAir),
        "error" => Ok(ChannelRuntimeState::Error),
        "stopped" => Ok(ChannelRuntimeState::Stopped),
        _ => Err(format!(
            "database returned invalid channel runtime state {value}"
        )),
    }
}

fn self_test_kind_name(value: SelfTestKind) -> &'static str {
    match value {
        SelfTestKind::Daily => "daily",
        SelfTestKind::Weekly => "weekly",
    }
}

fn parse_self_test_kind(value: &str) -> Result<SelfTestKind, String> {
    match value {
        "daily" => Ok(SelfTestKind::Daily),
        "weekly" => Ok(SelfTestKind::Weekly),
        _ => Err(format!("database returned invalid self-test kind {value}")),
    }
}

fn self_test_trigger_name(value: SelfTestTrigger) -> &'static str {
    match value {
        SelfTestTrigger::Scheduled => "scheduled",
        SelfTestTrigger::Manual => "manual",
    }
}

fn parse_self_test_trigger(value: &str) -> Result<SelfTestTrigger, String> {
    match value {
        "scheduled" => Ok(SelfTestTrigger::Scheduled),
        "manual" => Ok(SelfTestTrigger::Manual),
        _ => Err(format!(
            "database returned invalid self-test trigger {value}"
        )),
    }
}

fn self_test_status_name(value: SelfTestStatus) -> &'static str {
    match value {
        SelfTestStatus::Pass => "pass",
        SelfTestStatus::Warn => "warn",
        SelfTestStatus::Fail => "fail",
    }
}

fn update_check_trigger_name(value: UpdateCheckTrigger) -> &'static str {
    match value {
        UpdateCheckTrigger::Automatic => "automatic",
        UpdateCheckTrigger::Manual => "manual",
    }
}

fn update_check_outcome_name(value: UpdateCheckOutcome) -> &'static str {
    match value {
        UpdateCheckOutcome::UpToDate => "up_to_date",
        UpdateCheckOutcome::UpdateAvailable => "update_available",
        UpdateCheckOutcome::UpdaterTooOld => "updater_too_old",
        UpdateCheckOutcome::Failed => "failed",
    }
}

fn parse_self_test_status(value: &str) -> Result<SelfTestStatus, String> {
    match value {
        "pass" => Ok(SelfTestStatus::Pass),
        "warn" => Ok(SelfTestStatus::Warn),
        "fail" => Ok(SelfTestStatus::Fail),
        _ => Err(format!(
            "database returned invalid self-test status {value}"
        )),
    }
}

fn alert_condition_name(value: AlertConditionKind) -> &'static str {
    match value {
        AlertConditionKind::OffAir => "off-air",
        AlertConditionKind::OutputStall => "output-stall",
        AlertConditionKind::EncoderDeath => "encoder-death",
        AlertConditionKind::ServerCrash => "server-crash",
        AlertConditionKind::SchemaDrift => "schema-drift",
        AlertConditionKind::RelayBlocked => "relay-blocked",
        AlertConditionKind::ComplianceProbeFail => "compliance-probe-fail",
        AlertConditionKind::MissingMedia => "missing-media",
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
    }
}

fn parse_alert_condition(value: &str) -> Result<AlertConditionKind, String> {
    match value {
        "off-air" => Ok(AlertConditionKind::OffAir),
        "output-stall" => Ok(AlertConditionKind::OutputStall),
        "encoder-death" => Ok(AlertConditionKind::EncoderDeath),
        "server-crash" => Ok(AlertConditionKind::ServerCrash),
        "schema-drift" => Ok(AlertConditionKind::SchemaDrift),
        "relay-blocked" => Ok(AlertConditionKind::RelayBlocked),
        "compliance-probe-fail" => Ok(AlertConditionKind::ComplianceProbeFail),
        "missing-media" => Ok(AlertConditionKind::MissingMedia),
        "commit-failure" => Ok(AlertConditionKind::CommitFailure),
        "takeover-stuck-2h" => Ok(AlertConditionKind::TakeoverStuck2h),
        "ai-runtime-down" => Ok(AlertConditionKind::AiRuntimeDown),
        "disk-low" => Ok(AlertConditionKind::DiskLow),
        "clock-skew" => Ok(AlertConditionKind::ClockSkew),
        "database-unreachable" => Ok(AlertConditionKind::DatabaseUnreachable),
        "service-down" => Ok(AlertConditionKind::ServiceDown),
        "self-test-fail" => Ok(AlertConditionKind::SelfTestFail),
        "update-available" => Ok(AlertConditionKind::UpdateAvailable),
        "update-check-failed" => Ok(AlertConditionKind::UpdateCheckFailed),
        "update-apply-failed" => Ok(AlertConditionKind::UpdateApplyFailed),
        _ => Err(format!("database returned invalid alert condition {value}")),
    }
}

fn default_alert_rules() -> Vec<AlertRule> {
    let definitions = [
        (AlertConditionKind::OffAir, AlertSeverity::Critical, 3_600),
        (
            AlertConditionKind::OutputStall,
            AlertSeverity::Critical,
            3_600,
        ),
        (
            AlertConditionKind::EncoderDeath,
            AlertSeverity::Critical,
            3_600,
        ),
        (AlertConditionKind::ServerCrash, AlertSeverity::Critical, 0),
        (
            AlertConditionKind::SchemaDrift,
            AlertSeverity::Warning,
            21_600,
        ),
        (
            AlertConditionKind::RelayBlocked,
            AlertSeverity::Critical,
            1_800,
        ),
        (
            AlertConditionKind::ComplianceProbeFail,
            AlertSeverity::Warning,
            3_600,
        ),
        (AlertConditionKind::MissingMedia, AlertSeverity::Critical, 0),
        (
            AlertConditionKind::CommitFailure,
            AlertSeverity::Warning,
            1_800,
        ),
        (
            AlertConditionKind::TakeoverStuck2h,
            AlertSeverity::Warning,
            7_200,
        ),
        (
            AlertConditionKind::AiRuntimeDown,
            AlertSeverity::Info,
            21_600,
        ),
        (AlertConditionKind::DiskLow, AlertSeverity::Warning, 3_600),
        (AlertConditionKind::ClockSkew, AlertSeverity::Warning, 3_600),
        (
            AlertConditionKind::DatabaseUnreachable,
            AlertSeverity::Critical,
            1_800,
        ),
        (
            AlertConditionKind::ServiceDown,
            AlertSeverity::Critical,
            1_800,
        ),
        (
            AlertConditionKind::SelfTestFail,
            AlertSeverity::Warning,
            86_400,
        ),
        (
            AlertConditionKind::UpdateAvailable,
            AlertSeverity::Info,
            86_400,
        ),
        (
            AlertConditionKind::UpdateCheckFailed,
            AlertSeverity::Warning,
            21_600,
        ),
        (
            AlertConditionKind::UpdateApplyFailed,
            AlertSeverity::Critical,
            3_600,
        ),
    ];
    definitions
        .into_iter()
        .map(|(condition, severity, re_alert_after_seconds)| AlertRule {
            rule_id: format!("default-{}", alert_condition_name(condition)),
            condition,
            enabled: true,
            severity,
            channel_ids: Vec::new(),
            dedupe_window_seconds: 900,
            re_alert_after_seconds,
            scope_channel_id: None,
            notify_on_resolve: true,
            updated_at_unix_ms: 0,
            updated_by: "system-seed".into(),
        })
        .collect()
}

fn alert_severity_name(value: AlertSeverity) -> &'static str {
    match value {
        AlertSeverity::Critical => "critical",
        AlertSeverity::Warning => "warning",
        AlertSeverity::Info => "info",
    }
}

fn parse_alert_severity(value: &str) -> Result<AlertSeverity, String> {
    match value {
        "critical" => Ok(AlertSeverity::Critical),
        "warning" => Ok(AlertSeverity::Warning),
        "info" => Ok(AlertSeverity::Info),
        _ => Err(format!("database returned invalid alert severity {value}")),
    }
}

fn alert_event_state_name(value: AlertEventState) -> &'static str {
    match value {
        AlertEventState::Firing => "firing",
        AlertEventState::Resolved => "resolved",
    }
}

fn parse_alert_event_state(value: &str) -> Result<AlertEventState, String> {
    match value {
        "firing" => Ok(AlertEventState::Firing),
        "resolved" => Ok(AlertEventState::Resolved),
        _ => Err(format!(
            "database returned invalid alert event state {value}"
        )),
    }
}

fn loudness_status_name(value: LoudnessGateStatus) -> &'static str {
    match value {
        LoudnessGateStatus::Ok => "ok",
        LoudnessGateStatus::Failed => "failed",
        LoudnessGateStatus::NotChecked => "not_checked",
    }
}

fn parse_loudness_status(value: &str) -> Result<LoudnessGateStatus, String> {
    match value {
        "ok" => Ok(LoudnessGateStatus::Ok),
        "failed" => Ok(LoudnessGateStatus::Failed),
        "not_checked" => Ok(LoudnessGateStatus::NotChecked),
        _ => Err(format!("database returned invalid loudness status {value}")),
    }
}

fn ingest_source_kind_name(value: IngestSourceKind) -> &'static str {
    match value {
        IngestSourceKind::OperatorPath => "operator_path",
        IngestSourceKind::WatchFolder => "watch_folder",
        IngestSourceKind::LiveFinalization => "live_finalization",
    }
}

fn parse_ingest_source_kind(value: &str) -> Result<IngestSourceKind, String> {
    match value {
        "operator_path" => Ok(IngestSourceKind::OperatorPath),
        "watch_folder" => Ok(IngestSourceKind::WatchFolder),
        "live_finalization" => Ok(IngestSourceKind::LiveFinalization),
        _ => Err(format!(
            "database returned invalid ingest source kind {value}"
        )),
    }
}

fn parse_ingest_job_status(value: &str) -> Result<IngestJobStatus, String> {
    match value {
        "pending" => Ok(IngestJobStatus::Pending),
        "running" => Ok(IngestJobStatus::Running),
        "awaiting_transcode" => Ok(IngestJobStatus::AwaitingTranscode),
        "completed" => Ok(IngestJobStatus::Completed),
        "failed" => Ok(IngestJobStatus::Failed),
        "cancelled" => Ok(IngestJobStatus::Cancelled),
        _ => Err(format!(
            "database returned invalid ingest job status {value}"
        )),
    }
}

fn transcode_format_name(value: TranscodeFormat) -> &'static str {
    match value {
        TranscodeFormat::H264_720p4mbpsAac192kTs => "h264_720p4mbps_aac192k_ts",
    }
}

fn parse_transcode_format(value: &str) -> Result<TranscodeFormat, String> {
    match value {
        "h264_720p4mbps_aac192k_ts" => Ok(TranscodeFormat::H264_720p4mbpsAac192kTs),
        _ => Err(format!(
            "database returned invalid transcode format {value}"
        )),
    }
}

fn parse_transcode_job_status(value: &str) -> Result<TranscodeJobStatus, String> {
    match value {
        "pending" => Ok(TranscodeJobStatus::Pending),
        "running" => Ok(TranscodeJobStatus::Running),
        "completed" => Ok(TranscodeJobStatus::Completed),
        "failed" => Ok(TranscodeJobStatus::Failed),
        "cancelled" => Ok(TranscodeJobStatus::Cancelled),
        _ => Err(format!(
            "database returned invalid transcode job status {value}"
        )),
    }
}

fn is_sha256_identity(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn parse_readiness(value: &str) -> Result<AssetReadiness, String> {
    match value {
        "ready" => Ok(AssetReadiness::Ready),
        "missing" => Ok(AssetReadiness::Missing),
        "rejected" => Ok(AssetReadiness::Rejected),
        "processing" => Ok(AssetReadiness::Processing),
        _ => Err(format!("database returned invalid asset readiness {value}")),
    }
}

fn schedule_state_name(value: ScheduleState) -> &'static str {
    match value {
        ScheduleState::Draft => "draft",
        ScheduleState::Committed => "committed",
        ScheduleState::Cancelled => "cancelled",
    }
}

fn parse_schedule_state(value: &str) -> Result<ScheduleState, String> {
    match value {
        "draft" => Ok(ScheduleState::Draft),
        "committed" => Ok(ScheduleState::Committed),
        "cancelled" => Ok(ScheduleState::Cancelled),
        _ => Err(format!("database returned invalid schedule state {value}")),
    }
}

fn dispatch_status_name(value: DispatchStatus) -> &'static str {
    match value {
        DispatchStatus::Pending => "pending",
        DispatchStatus::Queued => "queued",
        DispatchStatus::Acknowledged => "acknowledged",
        DispatchStatus::Completed => "completed",
        DispatchStatus::Error => "error",
        DispatchStatus::Cancelled => "cancelled",
    }
}

fn parse_dispatch_status(value: &str) -> Result<DispatchStatus, String> {
    match value {
        "pending" => Ok(DispatchStatus::Pending),
        "queued" => Ok(DispatchStatus::Queued),
        "acknowledged" => Ok(DispatchStatus::Acknowledged),
        "completed" => Ok(DispatchStatus::Completed),
        "error" => Ok(DispatchStatus::Error),
        "cancelled" => Ok(DispatchStatus::Cancelled),
        _ => Err(format!("database returned invalid dispatch status {value}")),
    }
}

fn valid_dispatch_transition(expected: DispatchStatus, next: DispatchStatus) -> bool {
    matches!(
        (expected, next),
        (DispatchStatus::Pending, DispatchStatus::Queued)
            | (DispatchStatus::Pending, DispatchStatus::Error)
            | (DispatchStatus::Pending, DispatchStatus::Cancelled)
            | (DispatchStatus::Queued, DispatchStatus::Acknowledged)
            | (DispatchStatus::Queued, DispatchStatus::Error)
            | (DispatchStatus::Queued, DispatchStatus::Cancelled)
            | (DispatchStatus::Acknowledged, DispatchStatus::Completed)
            | (DispatchStatus::Acknowledged, DispatchStatus::Error)
    )
}

fn column_text(statement: *mut SqliteStatement, column: c_int) -> Result<String, String> {
    // SAFETY: caller provides a live statement positioned on a row and a valid column index.
    let value = unsafe { sqlite3_column_text(statement, column) };
    if value.is_null() {
        return Err("database returned an unexpected null value".into());
    }
    // SAFETY: SQLite returns a null-terminated UTF-8 buffer valid until the next step/finalize.
    Ok(unsafe { CStr::from_ptr(value.cast()) }
        .to_string_lossy()
        .into_owned())
}

fn column_blob(statement: *mut SqliteStatement, column: c_int) -> Result<Vec<u8>, String> {
    let byte_count = unsafe { sqlite3_column_bytes(statement, column) };
    if byte_count <= 0 {
        return Err("database returned an empty encrypted secret".into());
    }
    let value = unsafe { sqlite3_column_blob(statement, column) };
    if value.is_null() {
        return Err("database returned an invalid encrypted secret".into());
    }
    let length = usize::try_from(byte_count)
        .map_err(|_| "database returned an oversized encrypted secret".to_string())?;
    Ok(unsafe { std::slice::from_raw_parts(value.cast::<u8>(), length) }.to_vec())
}

fn protect_local_machine(plaintext: &[u8]) -> Result<Vec<u8>, String> {
    let byte_count =
        u32::try_from(plaintext.len()).map_err(|_| "station secret is too large".to_string())?;
    let mut input = DataBlob {
        byte_count,
        data: plaintext.as_ptr().cast_mut(),
    };
    let mut output = DataBlob {
        byte_count: 0,
        data: ptr::null_mut(),
    };
    let status = unsafe {
        CryptProtectData(
            &mut input,
            ptr::null(),
            ptr::null_mut(),
            ptr::null_mut(),
            ptr::null_mut(),
            CRYPTPROTECT_UI_FORBIDDEN | CRYPTPROTECT_LOCAL_MACHINE,
            &mut output,
        )
    };
    if status == 0 {
        return Err(format!(
            "Windows could not protect the station secret (error {}).",
            unsafe { GetLastError() }
        ));
    }
    if output.data.is_null() || output.byte_count == 0 {
        return Err("Windows returned an empty protected station secret.".into());
    }
    let protected = unsafe {
        std::slice::from_raw_parts(output.data.cast_const(), output.byte_count as usize).to_vec()
    };
    unsafe {
        LocalFree(output.data.cast());
    }
    Ok(protected)
}

fn unprotect_local_machine(protected: &[u8]) -> Result<Vec<u8>, String> {
    let byte_count = u32::try_from(protected.len())
        .map_err(|_| "encrypted station secret is too large".to_string())?;
    let mut input = DataBlob {
        byte_count,
        data: protected.as_ptr().cast_mut(),
    };
    let mut output = DataBlob {
        byte_count: 0,
        data: ptr::null_mut(),
    };
    let status = unsafe {
        CryptUnprotectData(
            &mut input,
            ptr::null_mut(),
            ptr::null_mut(),
            ptr::null_mut(),
            ptr::null_mut(),
            CRYPTPROTECT_UI_FORBIDDEN,
            &mut output,
        )
    };
    if status == 0 {
        return Err(format!(
            "Windows could not unlock the station secret (error {}).",
            unsafe { GetLastError() }
        ));
    }
    if output.data.is_null() || output.byte_count == 0 {
        return Err("Windows returned an empty unlocked station secret.".into());
    }
    let plaintext = unsafe {
        std::slice::from_raw_parts(output.data.cast_const(), output.byte_count as usize).to_vec()
    };
    unsafe {
        LocalFree(output.data.cast());
    }
    Ok(plaintext)
}

fn secret_field(value: &str, maximum: usize) -> bool {
    value.trim() == value
        && !value.is_empty()
        && value.len() <= maximum
        && !value.chars().any(char::is_control)
}

fn validate_srt_passphrase(value: &str) -> Result<(), String> {
    if !(10..=79).contains(&value.len()) || !value.bytes().all(|byte| (0x21..=0x7e).contains(&byte))
    {
        return Err(
            "SRT passphrases must contain 10 to 79 printable ASCII characters without spaces"
                .into(),
        );
    }
    Ok(())
}

fn valid_smtp_endpoint(value: &str) -> bool {
    if value.trim() != value
        || value.is_empty()
        || value.len() > 2048
        || value.chars().any(char::is_control)
    {
        return false;
    }
    let (secure, remainder) = if let Some(value) = value.strip_prefix("smtps://") {
        (true, value)
    } else if let Some(value) = value.strip_prefix("smtp://") {
        (false, value)
    } else {
        return false;
    };
    if remainder.contains(['/', '?', '#']) {
        return false;
    }
    let authority = remainder;
    let host = authority.split(':').next().unwrap_or_default();
    !authority.is_empty()
        && !authority.contains('@')
        && !authority.chars().any(char::is_whitespace)
        && (secure || host.eq_ignore_ascii_case("localhost") || host == "127.0.0.1")
}

fn sqlite_error(database: *mut Sqlite3) -> String {
    if database.is_null() {
        return "SQLite failed before creating a database handle".into();
    }
    // SAFETY: SQLite owns a null-terminated error buffer associated with this live handle.
    unsafe { CStr::from_ptr(sqlite3_errmsg(database)) }
        .to_string_lossy()
        .into_owned()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn bare_database_filename_uses_the_current_directory_as_its_data_root() {
        assert_eq!(
            station_data_root(Path::new("station.db")).unwrap(),
            std::env::current_dir().unwrap()
        );
    }

    #[test]
    fn adds_the_default_output_profile_to_existing_channels_without_losing_rows() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-profile-migration-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        store
            .execute(
                "DROP TABLE channels;\
                 CREATE TABLE channels (\
                   channel_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,\
                   udp_destination TEXT NOT NULL, enabled INTEGER NOT NULL CHECK (enabled IN (0,1)));\
                 INSERT INTO channels VALUES(\
                   '8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e','Government','127.0.0.1:5500',1);",
            )
            .unwrap();
        drop(store);

        let migrated = StationStore::open(&database).unwrap();
        let channels = migrated.list_channels().unwrap();
        assert_eq!(channels.len(), 1);
        assert_eq!(channels[0].display_name, "Government");
        assert_eq!(channels[0].output_profile, OutputProfileId::Proof360p30);
        assert_eq!(channels[0].output_sinks.len(), 1);
        assert_eq!(channels[0].output_sinks[0].sink_id, channels[0].channel_id);
        assert_eq!(channels[0].output_sinks[0].destination, "127.0.0.1:5500");
        assert_eq!(channels[0].output_sinks[0].loudness_target_lufs, -16.0);
        drop(migrated);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn expands_the_existing_udp_sink_table_for_srt_without_losing_rows() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-srt-migration-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        store
            .execute(
                "DROP TABLE channel_output_sinks;\
                 CREATE TABLE channel_output_sinks (\
                   channel_id TEXT NOT NULL, sink_id TEXT NOT NULL, label TEXT NOT NULL,\
                   kind TEXT NOT NULL CHECK (kind IN ('udp-ts')), destination TEXT NOT NULL,\
                   loudness_target_milli INTEGER NOT NULL,\
                   required INTEGER NOT NULL CHECK (required IN (0,1)),\
                   enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),\
                   PRIMARY KEY(channel_id, sink_id), UNIQUE(channel_id, label),\
                   FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE);\
                 INSERT INTO channels(channel_id, display_name, udp_destination, output_profile, enabled)\
                   VALUES('11111111-2222-4333-8444-555555555555','Preserved UDP',\
                     '127.0.0.1:5500','atsc-720p5994',0);\
                 INSERT INTO channel_output_sinks VALUES(\
                   '11111111-2222-4333-8444-555555555555',\
                   '11111111-2222-4333-8444-555555555555',\
                   'Primary transport','udp-ts','127.0.0.1:5500',-24000,1,1)",
            )
            .unwrap();
        drop(store);

        let migrated = StationStore::open(&database).unwrap();
        let srt = ChannelConfiguration {
            channel_id: "22222222-3333-4444-8555-666666666666".into(),
            display_name: "SRT output".into(),
            output_profile: OutputProfileId::Atsc720p5994,
            output_sinks: vec![OutputSinkConfiguration::required_srt(
                "33333333-4444-4555-8666-777777777777",
                "SRT caller",
                "srt://127.0.0.1:5600",
                -24.0,
            )],
            enabled: false,
        };
        migrated.put_channel(&srt).unwrap();
        let channels = migrated.list_channels().unwrap();
        assert_eq!(channels.len(), 2);
        assert!(channels.iter().any(|channel| {
            channel.display_name == "Preserved UDP"
                && channel.output_sinks[0].kind == OutputSinkKind::UdpTs
        }));
        assert!(channels.iter().any(|channel| channel == &srt));
        drop(migrated);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn migrates_the_pre_completion_dispatch_constraint_without_losing_rows() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-migration-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        store
            .execute(
                "DROP TABLE commit_reports;\
                 CREATE TABLE commit_reports (\
                   report_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, channel_id TEXT NOT NULL,\
                   schedule_item_id TEXT NOT NULL, asset_id TEXT NOT NULL, approved_by TEXT NOT NULL,\
                   approved_at_unix_ms INTEGER NOT NULL, dispatch_status TEXT NOT NULL CHECK \
                   (dispatch_status IN ('pending','queued','acknowledged','error','cancelled')),\
                   operator_notes TEXT NOT NULL);\
                 INSERT INTO commit_reports VALUES('legacy','plan','channel','item','asset','operator',1,'acknowledged','kept');",
            )
            .unwrap();
        drop(store);

        let migrated = StationStore::open(&database).unwrap();
        let report = migrated.read_commit_report("legacy").unwrap().unwrap();
        assert_eq!(report.operator_notes, "kept");
        assert!(
            migrated
                .advance_dispatch_status(
                    "legacy",
                    DispatchStatus::Acknowledged,
                    DispatchStatus::Completed,
                )
                .unwrap()
        );
        assert_eq!(
            migrated
                .read_commit_report("legacy")
                .unwrap()
                .unwrap()
                .dispatch_status,
            DispatchStatus::Completed
        );
        drop(migrated);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn adds_retry_scheduling_to_an_existing_ingest_database_without_losing_jobs() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-ingest-migration-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        store
            .execute(
                "DROP TABLE media_ingest_jobs;\
                 CREATE TABLE media_ingest_jobs (\
                   job_id TEXT PRIMARY KEY, source_kind TEXT NOT NULL, source_path TEXT NOT NULL,\
                   status TEXT NOT NULL, progress_percent INTEGER NOT NULL, created_at_unix_ms INTEGER NOT NULL,\
                   started_at_unix_ms INTEGER NOT NULL, completed_at_unix_ms INTEGER NOT NULL,\
                   error_detail TEXT NOT NULL, asset_id TEXT NOT NULL, attempts INTEGER NOT NULL);\
                 INSERT INTO media_ingest_jobs VALUES(\
                   '11111111-2222-4333-8444-555555555555','watch_folder','C:\\incoming\\meeting.ts',\
                   'pending',0,1,-1,-1,'','','0');",
            )
            .unwrap();
        drop(store);

        let migrated = StationStore::open(&database).unwrap();
        let job = migrated
            .read_ingest_job("11111111-2222-4333-8444-555555555555")
            .unwrap()
            .unwrap();
        assert_eq!(job.source_kind, IngestSourceKind::WatchFolder);
        assert_eq!(job.next_attempt_at_unix_ms, None);
        drop(migrated);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn migrates_existing_alert_deliveries_and_seeds_rules_without_losing_history() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database =
            std::env::temp_dir().join(format!("townlight-alert-rule-migration-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        store
            .execute(
                "INSERT INTO alert_events VALUES(\
                   'legacy-event','off-air','critical','resolved','legacy-channel','Legacy alert','Kept','S8',1,2,2,1,-1,'');\
                 INSERT INTO alert_channels VALUES(\
                   '5eb469af-f3c7-4cc5-b26e-865807b3e934','webhook','Legacy pager',0,\
                   'https://alerts.example.org/…',1,'','',X'01',1,1);\
                 DROP INDEX alert_deliveries_due;\
                 DROP TABLE alert_deliveries;\
                 CREATE TABLE alert_deliveries (\
                   delivery_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, alert_channel_id TEXT NOT NULL,\
                   notification_kind TEXT NOT NULL CHECK (notification_kind IN ('firing','resolved','test')),\
                   state TEXT NOT NULL CHECK (state IN ('pending','leased','sent','dead_letter')), attempts INTEGER NOT NULL,\
                   next_attempt_at_unix_ms INTEGER NOT NULL, lease_expires_at_unix_ms INTEGER NOT NULL,\
                   last_error TEXT NOT NULL, signature TEXT NOT NULL, payload_json TEXT NOT NULL,\
                   created_at_unix_ms INTEGER NOT NULL, updated_at_unix_ms INTEGER NOT NULL, sent_at_unix_ms INTEGER NOT NULL,\
                   UNIQUE(event_id, alert_channel_id, notification_kind));\
                 INSERT INTO alert_deliveries VALUES(\
                   'legacy-delivery','legacy-event','5eb469af-f3c7-4cc5-b26e-865807b3e934','resolved','sent',1,2,-1,'','proof',\
                   '{\"notification_kind\":\"resolved\",\"event_id\":\"legacy-event\",\"condition\":\"off-air\",\"severity\":\"critical\",\"resource_ref\":\"legacy-channel\",\"summary\":\"Legacy alert\",\"detail\":\"Kept\",\"source_section\":\"S8\",\"first_observed_at_unix_ms\":1,\"last_observed_at_unix_ms\":2,\"resolved_at_unix_ms\":2,\"occurrence_count\":1}',2,2,2);",
            )
            .unwrap();
        drop(store);

        let migrated = StationStore::open(&database).unwrap();
        let delivery = migrated
            .read_alert_delivery("legacy-delivery")
            .unwrap()
            .unwrap();
        assert_eq!(delivery.notification_kind, AlertNotificationKind::Resolved);
        assert_eq!(delivery.state, AlertDeliveryState::Sent);
        assert_eq!(migrated.list_alert_rules().unwrap().len(), 19);
        drop(migrated);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }
}
