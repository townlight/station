use super::*;
use station_publishing::{
    PublishCandidateIdentity, PublishExecutionReceipt, PublishExecutionStage,
    PublishExecutionTargetConfig, PublishTargetKind, PublishTargetProfile,
    validate_receipt_successor,
};
use std::fs::File;
use std::io::{Read, Seek, SeekFrom, Write};
#[cfg(windows)]
use std::os::windows::ffi::OsStrExt;
#[cfg(windows)]
use std::os::windows::fs::OpenOptionsExt;
#[cfg(windows)]
use std::os::windows::io::AsRawHandle;
#[cfg(windows)]
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
#[cfg(windows)]
use std::process::{Child, Command, Stdio};
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use std::time::Duration;

mod youtube_live_runtime;

const MAX_PUBLISH_ATTEMPTS: u32 = 8;
const MAX_PUBLISH_LEASE_MS: i64 = 900_000;
const MAX_PROVIDER_RESPONSE_BYTES: usize = 1_048_576;
const MAX_NAS_COPY_DURATION: Duration = Duration::from_secs(24 * 60 * 60);
const NAS_COPY_BUFFER_BYTES: usize = 1024 * 1024;
const MAX_NAS_WORKER_REQUEST_BYTES: usize = 4 * 1024;
const MAX_NAS_WORKER_RESULT_BYTES: usize = 2 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum PublishExecutionJobState {
    Pending,
    Running,
    Completed,
    Failed,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct PublishExecutionJob {
    pub job_id: String,
    pub account_id: String,
    pub account_revision: u64,
    pub target_id: String,
    pub target_revision: u64,
    pub target_kind: PublishTargetKind,
    pub configuration_hash: String,
    pub credential_version: u64,
    pub candidate_version: String,
    pub source_commit: String,
    pub install_generation: u64,
    pub asset_id: String,
    pub media_sha256: String,
    pub media_bytes: u64,
    pub media_duration_ms: u64,
    pub requested_by: String,
    pub created_at_unix_ms: i64,
}

impl PublishExecutionJob {
    fn validate(&self) -> Result<(), String> {
        if !is_canonical_uuid(&self.job_id)
            || !is_canonical_uuid(&self.account_id)
            || !is_canonical_uuid(&self.target_id)
            || self.account_revision == 0
            || self.target_revision == 0
            || self.credential_version == 0
            || self.install_generation == 0
            || !is_sha256_identity(&self.configuration_hash)
            || !is_sha256_identity(&self.asset_id)
            || self.asset_id != self.media_sha256
            || self.media_bytes == 0
            || !(1..=86_400_000).contains(&self.media_duration_ms)
            || !secret_field(&self.candidate_version, 200)
            || !is_source_commit_identity(&self.source_commit)
            || !secret_field(&self.requested_by, 200)
            || self.created_at_unix_ms < 0
        {
            return Err("publish execution job binding is invalid or unbounded".into());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct PublishExecutionJobRecord {
    pub job: PublishExecutionJob,
    pub state: PublishExecutionJobState,
    pub attempts: u32,
    pub next_attempt_at_unix_ms: i64,
    pub updated_at_unix_ms: i64,
    pub lease_expires_at_unix_ms: Option<i64>,
    pub completed_at_unix_ms: Option<i64>,
    pub external_effect_started_at_unix_ms: Option<i64>,
    pub failure_class: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct PublishExecutionReadiness {
    pub canonical_portal: bool,
    pub internet_archive: bool,
    pub local_nas: bool,
    pub reach_capacity: bool,
    pub proven_target_count: usize,
}

pub(crate) struct PublishExecutionLease {
    record: PublishExecutionJobRecord,
    lease_token: String,
}

impl PublishExecutionLease {
    pub(crate) fn record(&self) -> &PublishExecutionJobRecord {
        &self.record
    }
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
struct ProviderStageEvidence {
    stage: PublishExecutionStage,
    digest: String,
    detail: String,
}

trait PublishProviderAdapter {
    fn execute(
        &self,
        job: &PublishExecutionJob,
        config: &PublishExecutionTargetConfig,
        media_path: &Path,
        credential: &[u8],
        heartbeat: &mut dyn FnMut() -> Result<(), String>,
    ) -> Result<Vec<ProviderStageEvidence>, String>;
}

struct SystemPublishProviderAdapter<'a> {
    application_root: Option<&'a Path>,
    data_root: Option<&'a Path>,
    nas_worker_sha256: Option<String>,
}

impl StationStore {
    pub(super) fn initialize_publishing_executor_schema(&self) -> Result<(), String> {
        self.execute(
            "CREATE TABLE IF NOT EXISTS publish_execution_target_configs (\
               target_id TEXT NOT NULL, target_revision INTEGER NOT NULL CHECK(target_revision>=1),\
               configuration_hash TEXT NOT NULL CHECK(length(configuration_hash)=64),\
               record_json TEXT NOT NULL, changed_by TEXT NOT NULL, occurred_at_unix_ms INTEGER NOT NULL CHECK(occurred_at_unix_ms>=0),\
               PRIMARY KEY(target_id,target_revision), FOREIGN KEY(target_id) REFERENCES publish_target_profiles(target_id));\
             CREATE TABLE IF NOT EXISTS publish_execution_jobs (\
               job_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, account_revision INTEGER NOT NULL,\
               target_id TEXT NOT NULL, target_revision INTEGER NOT NULL, target_kind TEXT NOT NULL,\
               configuration_hash TEXT NOT NULL, credential_version INTEGER NOT NULL,\
               candidate_version TEXT NOT NULL, source_commit TEXT NOT NULL, install_generation INTEGER NOT NULL,\
               asset_id TEXT NOT NULL, media_sha256 TEXT NOT NULL, media_bytes INTEGER NOT NULL, media_duration_ms INTEGER NOT NULL CHECK(media_duration_ms BETWEEN 1 AND 86400000), requested_by TEXT NOT NULL,\
               created_at_unix_ms INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','running','completed','failed')),\
               attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at_unix_ms INTEGER NOT NULL, updated_at_unix_ms INTEGER NOT NULL,\
               lease_token TEXT, lease_expires_at_unix_ms INTEGER, completed_at_unix_ms INTEGER, external_effect_started_at_unix_ms INTEGER, failure_class TEXT);\
             CREATE INDEX IF NOT EXISTS publish_execution_jobs_claim ON publish_execution_jobs(state,next_attempt_at_unix_ms,created_at_unix_ms);\
             CREATE TABLE IF NOT EXISTS publish_execution_receipts (\
               receipt_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, stage TEXT NOT NULL, recorded_at_unix_ms INTEGER NOT NULL, record_json TEXT NOT NULL,\
               FOREIGN KEY(job_id) REFERENCES publish_execution_jobs(job_id));\
             CREATE INDEX IF NOT EXISTS publish_execution_receipts_job ON publish_execution_receipts(job_id,recorded_at_unix_ms,receipt_id);\
             CREATE TRIGGER IF NOT EXISTS publish_execution_configs_no_update BEFORE UPDATE ON publish_execution_target_configs BEGIN SELECT RAISE(ABORT,'publish execution configuration is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS publish_execution_configs_no_delete BEFORE DELETE ON publish_execution_target_configs BEGIN SELECT RAISE(ABORT,'publish execution configuration is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS publish_execution_receipts_no_update BEFORE UPDATE ON publish_execution_receipts BEGIN SELECT RAISE(ABORT,'publish execution receipt is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS publish_execution_receipts_no_delete BEFORE DELETE ON publish_execution_receipts BEGIN SELECT RAISE(ABORT,'publish execution receipt is append-only'); END;",
        )?;
        if !self.sqlite_table_has_column("publish_execution_jobs", "media_duration_ms")? {
            self.execute(
                "ALTER TABLE publish_execution_jobs ADD COLUMN media_duration_ms INTEGER NOT NULL DEFAULT -1;\
                 UPDATE publish_execution_jobs SET media_duration_ms=COALESCE((SELECT duration_ms FROM media_assets WHERE media_assets.asset_id=publish_execution_jobs.asset_id),-1)",
            )?;
        }
        if !self.sqlite_table_has_column(
            "publish_execution_jobs",
            "external_effect_started_at_unix_ms",
        )? {
            self.execute(
                "ALTER TABLE publish_execution_jobs ADD COLUMN external_effect_started_at_unix_ms INTEGER",
            )?;
        }
        self.migrate_publish_execution_duration_bound()?;
        Ok(())
    }

    fn migrate_publish_execution_duration_bound(&self) -> Result<(), String> {
        let definition = {
            let statement = self.prepare(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='publish_execution_jobs'",
            )?;
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => column_text(statement.raw, 0)?,
                _ => return Err("publish execution job schema is unavailable".into()),
            }
        };
        if definition.contains("86400000") {
            return Ok(());
        }
        if !definition.contains("43200000") {
            return Err("publish execution job duration bound is unrecognized".into());
        }
        // A receipt references this parent table. Retain its declared parent
        // name while replacing the bounded CHECK constraint, then prove the
        // foreign-key graph before opening the store for work.
        self.execute("PRAGMA foreign_keys=OFF")?;
        self.execute("PRAGMA legacy_alter_table=ON")?;
        let migration = self.execute(
            "BEGIN IMMEDIATE;\
             DROP INDEX IF EXISTS publish_execution_jobs_claim;\
             ALTER TABLE publish_execution_jobs RENAME TO publish_execution_jobs_legacy;\
             CREATE TABLE publish_execution_jobs (\
               job_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, account_revision INTEGER NOT NULL,\
               target_id TEXT NOT NULL, target_revision INTEGER NOT NULL, target_kind TEXT NOT NULL,\
               configuration_hash TEXT NOT NULL, credential_version INTEGER NOT NULL,\
               candidate_version TEXT NOT NULL, source_commit TEXT NOT NULL, install_generation INTEGER NOT NULL,\
               asset_id TEXT NOT NULL, media_sha256 TEXT NOT NULL, media_bytes INTEGER NOT NULL, media_duration_ms INTEGER NOT NULL CHECK(media_duration_ms BETWEEN 1 AND 86400000), requested_by TEXT NOT NULL,\
               created_at_unix_ms INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','running','completed','failed')),\
               attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at_unix_ms INTEGER NOT NULL, updated_at_unix_ms INTEGER NOT NULL,\
               lease_token TEXT, lease_expires_at_unix_ms INTEGER, completed_at_unix_ms INTEGER, external_effect_started_at_unix_ms INTEGER, failure_class TEXT\
             );\
             INSERT INTO publish_execution_jobs(job_id,account_id,account_revision,target_id,target_revision,target_kind,configuration_hash,credential_version,candidate_version,source_commit,install_generation,asset_id,media_sha256,media_bytes,media_duration_ms,requested_by,created_at_unix_ms,state,attempts,next_attempt_at_unix_ms,updated_at_unix_ms,lease_token,lease_expires_at_unix_ms,completed_at_unix_ms,external_effect_started_at_unix_ms,failure_class) \
             SELECT job_id,account_id,account_revision,target_id,target_revision,target_kind,configuration_hash,credential_version,candidate_version,source_commit,install_generation,asset_id,media_sha256,media_bytes,media_duration_ms,requested_by,created_at_unix_ms,state,attempts,next_attempt_at_unix_ms,updated_at_unix_ms,lease_token,lease_expires_at_unix_ms,completed_at_unix_ms,external_effect_started_at_unix_ms,failure_class FROM publish_execution_jobs_legacy;\
             DROP TABLE publish_execution_jobs_legacy;\
             CREATE INDEX publish_execution_jobs_claim ON publish_execution_jobs(state,next_attempt_at_unix_ms,created_at_unix_ms);\
             COMMIT;",
        );
        if migration.is_err() {
            let _ = self.execute("ROLLBACK");
        }
        let legacy_reset = self.execute("PRAGMA legacy_alter_table=OFF");
        let foreign_key_reset = self.execute("PRAGMA foreign_keys=ON");
        migration.and(legacy_reset).and(foreign_key_reset)?;
        let statement = self.prepare("PRAGMA foreign_key_check")?;
        if unsafe { sqlite3_step(statement.raw) } != SQLITE_DONE {
            return Err(
                "publish execution job duration migration broke foreign-key integrity".into(),
            );
        }
        Ok(())
    }

    pub fn write_publish_execution_target_config(
        &self,
        target_id: &str,
        expected_target_revision: u64,
        config: &PublishExecutionTargetConfig,
        changed_by: &str,
        occurred_at_unix_ms: i64,
    ) -> Result<String, String> {
        config.validate()?;
        if !secret_field(changed_by, 200) || occurred_at_unix_ms < 0 {
            return Err("publish execution configuration attribution is invalid".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = self.insert_publish_execution_target_config(
            target_id,
            expected_target_revision,
            config,
            changed_by,
            occurred_at_unix_ms,
        );
        self.finish_value_transaction(outcome)
    }

    fn insert_publish_execution_target_config(
        &self,
        target_id: &str,
        expected_target_revision: u64,
        config: &PublishExecutionTargetConfig,
        changed_by: &str,
        occurred_at_unix_ms: i64,
    ) -> Result<String, String> {
        let record_json = serde_json::to_string(config)
            .map_err(|error| format!("serialize publish execution configuration: {error}"))?;
        let configuration_hash = format!("{:x}", Sha256::digest(record_json.as_bytes()));
        (|| {
            let target = self
                .read_publish_target_profile(target_id)?
                .ok_or_else(|| "publish target is missing".to_string())?;
            if target.revision != expected_target_revision
                || target.kind != config.kind()
                || target.configuration_hash != configuration_hash
            {
                return Err(
                    "publish target changed or its configuration digest does not match".into(),
                );
            }
            let statement = self.prepare("INSERT INTO publish_execution_target_configs(target_id,target_revision,configuration_hash,record_json,changed_by,occurred_at_unix_ms) VALUES(?1,?2,?3,?4,?5,?6)")?;
            for (value, index, field) in [
                (target_id, 1, "publish target"),
                (&configuration_hash, 3, "publish configuration hash"),
                (&record_json, 4, "publish configuration"),
                (changed_by, 5, "publish configuration actor"),
            ] {
                bind_text(
                    self.database,
                    statement.raw,
                    index,
                    &c_string(value, field)?,
                )?;
            }
            bind_i64(
                self.database,
                statement.raw,
                2,
                checked_i64(expected_target_revision, "publish target revision")?,
            )?;
            bind_i64(self.database, statement.raw, 6, occurred_at_unix_ms)?;
            expect_done(self.database, statement.raw)?;
            Ok(configuration_hash.clone())
        })()
    }

    pub fn write_publish_target_profile_with_execution_config(
        &self,
        target: &PublishTargetProfile,
        expected_revision: Option<u64>,
        config: &PublishExecutionTargetConfig,
        changed_by: &str,
        occurred_at_unix_ms: i64,
    ) -> Result<(), PublishingAuthorityWriteError> {
        target
            .validate()
            .map_err(PublishingAuthorityWriteError::Invalid)?;
        config
            .validate()
            .map_err(PublishingAuthorityWriteError::Invalid)?;
        if target.kind != config.kind() || !secret_field(changed_by, 200) || occurred_at_unix_ms < 0
        {
            return Err(PublishingAuthorityWriteError::Invalid(
                "publish execution target configuration is invalid".into(),
            ));
        }
        let config_json = serde_json::to_string(config).map_err(|error| {
            PublishingAuthorityWriteError::Storage(format!(
                "serialize publish execution configuration: {error}"
            ))
        })?;
        if target.configuration_hash != format!("{:x}", Sha256::digest(config_json.as_bytes())) {
            return Err(PublishingAuthorityWriteError::Invalid(
                "publish target configuration digest does not match typed configuration".into(),
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(PublishingAuthorityWriteError::Storage)?;
        let outcome = self
            .write_publish_target_profile_in_transaction(
                target,
                expected_revision,
                changed_by,
                occurred_at_unix_ms,
            )
            .and_then(|_| {
                self.insert_publish_execution_target_config(
                    &target.target_id,
                    target.revision,
                    config,
                    changed_by,
                    occurred_at_unix_ms,
                )
                .map(|_| ())
                .map_err(PublishingAuthorityWriteError::Storage)
            });
        crate::publishing_authority::finish_publishing_write(self, outcome)
    }

    pub fn read_publish_execution_target_config(
        &self,
        target_id: &str,
        target_revision: u64,
    ) -> Result<Option<PublishExecutionTargetConfig>, String> {
        let statement = self.prepare("SELECT configuration_hash,record_json FROM publish_execution_target_configs WHERE target_id=?1 AND target_revision=?2")?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(target_id, "publish target")?,
        )?;
        bind_i64(
            self.database,
            statement.raw,
            2,
            checked_i64(target_revision, "publish target revision")?,
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let hash = column_text(statement.raw, 0)?;
                let json = column_text(statement.raw, 1)?;
                if format!("{:x}", Sha256::digest(json.as_bytes())) != hash {
                    return Err("publish execution configuration digest is corrupt".into());
                }
                let config: PublishExecutionTargetConfig = serde_json::from_str(&json)
                    .map_err(|_| "publish execution configuration is corrupt".to_string())?;
                config.validate()?;
                Ok(Some(config))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn enqueue_publish_execution_job(&self, job: &PublishExecutionJob) -> Result<(), String> {
        job.validate()?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            self.validate_publish_execution_job_current(job)?;
            let statement = self.prepare("INSERT INTO publish_execution_jobs(job_id,account_id,account_revision,target_id,target_revision,target_kind,configuration_hash,credential_version,candidate_version,source_commit,install_generation,asset_id,media_sha256,media_bytes,media_duration_ms,requested_by,created_at_unix_ms,state,next_attempt_at_unix_ms,updated_at_unix_ms) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,'pending',?17,?17)")?;
            let target_kind = publish_kind_name(job.target_kind).to_string();
            let values = [
                (&job.job_id, 1, "publish job"),
                (&job.account_id, 2, "publish account"),
                (&job.target_id, 4, "publish target"),
                (&target_kind, 6, "publish kind"),
                (&job.configuration_hash, 7, "publish configuration"),
                (&job.candidate_version, 9, "publish candidate"),
                (&job.source_commit, 10, "publish source commit"),
                (&job.asset_id, 12, "publish asset"),
                (&job.media_sha256, 13, "publish media hash"),
                (&job.requested_by, 16, "publish actor"),
            ];
            for (value, index, field) in values {
                bind_text(
                    self.database,
                    statement.raw,
                    index,
                    &c_string(value, field)?,
                )?;
            }
            for (value, index, field) in [
                (job.account_revision, 3, "account revision"),
                (job.target_revision, 5, "target revision"),
                (job.credential_version, 8, "credential version"),
                (job.install_generation, 11, "install generation"),
                (job.media_bytes, 14, "media bytes"),
                (job.media_duration_ms, 15, "media duration"),
            ] {
                bind_i64(
                    self.database,
                    statement.raw,
                    index,
                    checked_i64(value, field)?,
                )?;
            }
            bind_i64(self.database, statement.raw, 17, job.created_at_unix_ms)?;
            expect_done(self.database, statement.raw)
        })();
        self.finish_value_transaction(outcome)
    }

    pub fn read_publish_execution_job(
        &self,
        job_id: &str,
    ) -> Result<Option<PublishExecutionJobRecord>, String> {
        let statement = self.prepare(&format!("{} WHERE job_id=?1", publish_job_select()))?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(job_id, "publish job")?,
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_publish_job(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_publish_execution_jobs(
        &self,
        target_id: &str,
        limit: usize,
    ) -> Result<Vec<PublishExecutionJobRecord>, String> {
        if !(1..=128).contains(&limit) {
            return Err("publish job history limit is invalid".into());
        }
        let statement = self.prepare(&format!(
            "{} WHERE target_id=?1 ORDER BY created_at_unix_ms DESC,job_id DESC LIMIT ?2",
            publish_job_select()
        ))?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(target_id, "publish target")?,
        )?;
        bind_i64(self.database, statement.raw, 2, limit as i64)?;
        let mut records = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => records.push(read_publish_job(statement.raw)?),
                SQLITE_DONE => return Ok(records),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn list_publish_execution_receipts(
        &self,
        job_id: &str,
        limit: usize,
    ) -> Result<Vec<PublishExecutionReceipt>, String> {
        if !is_canonical_uuid(job_id) || !(1..=128).contains(&limit) {
            return Err("publish receipt query is invalid or unbounded".into());
        }
        let statement = self.prepare("SELECT record_json FROM publish_execution_receipts WHERE job_id=?1 ORDER BY recorded_at_unix_ms,receipt_id LIMIT ?2")?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(job_id, "publish job")?,
        )?;
        bind_i64(self.database, statement.raw, 2, limit as i64)?;
        let mut receipts = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    let json = column_text(statement.raw, 0)?;
                    let receipt: PublishExecutionReceipt = serde_json::from_str(&json)
                        .map_err(|_| "publish execution receipt is corrupt".to_string())?;
                    receipt.validate()?;
                    if receipt.job_id != job_id {
                        return Err("publish execution receipt job binding is corrupt".into());
                    }
                    receipts.push(receipt);
                }
                SQLITE_DONE => return Ok(receipts),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn derive_current_publish_execution_readiness(
        &self,
        candidate_version: &str,
        source_commit: &str,
        install_generation: u64,
    ) -> Result<PublishExecutionReadiness, String> {
        if !secret_field(candidate_version, 200)
            || !is_source_commit_identity(source_commit)
            || install_generation == 0
        {
            return Err("publish execution candidate binding is invalid".into());
        }
        let targets = self.list_publish_target_profiles()?;
        let mut proven = Vec::new();
        for target in targets.iter().filter(|target| target.enabled) {
            let jobs = self.list_publish_execution_jobs(&target.target_id, 128)?;
            let mut target_proven = false;
            for record in jobs {
                if record.state != PublishExecutionJobState::Completed
                    || record.job.candidate_version != candidate_version
                    || record.job.source_commit != source_commit
                    || record.job.install_generation != install_generation
                    || self
                        .validate_publish_execution_job_current(&record.job)
                        .is_err()
                {
                    continue;
                }
                let receipts = self.list_publish_execution_receipts(&record.job.job_id, 128)?;
                if receipts
                    .last()
                    .is_some_and(PublishExecutionReceipt::satisfies_required_delivery)
                {
                    target_proven = true;
                    break;
                }
            }
            if target_proven {
                proven.push(target.kind);
            }
        }
        let required_kind = |kind| {
            let matching = targets
                .iter()
                .filter(|target| target.enabled && target.kind == kind)
                .count();
            matching > 0 && proven.iter().filter(|value| **value == kind).count() == matching
        };
        Ok(PublishExecutionReadiness {
            canonical_portal: required_kind(PublishTargetKind::Portal),
            internet_archive: required_kind(PublishTargetKind::InternetArchive),
            local_nas: required_kind(PublishTargetKind::LocalNas),
            reach_capacity: required_kind(PublishTargetKind::YouTubeLive),
            proven_target_count: proven.len(),
        })
    }

    pub(crate) fn claim_publish_execution_job(
        &self,
        now_unix_ms: i64,
    ) -> Result<Option<PublishExecutionLease>, String> {
        if now_unix_ms < 0 {
            return Err("publish controller time is invalid".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let recover_unknown = self.prepare("UPDATE publish_execution_jobs SET state='failed',lease_token=NULL,lease_expires_at_unix_ms=NULL,next_attempt_at_unix_ms=?1,updated_at_unix_ms=?1,failure_class='external_outcome_unknown' WHERE state='running' AND external_effect_started_at_unix_ms IS NOT NULL AND lease_expires_at_unix_ms<=?1")?;
            bind_i64(self.database, recover_unknown.raw, 1, now_unix_ms)?;
            expect_done(self.database, recover_unknown.raw)?;
            let recover = self.prepare("UPDATE publish_execution_jobs SET state='pending',lease_token=NULL,lease_expires_at_unix_ms=NULL,next_attempt_at_unix_ms=?1,updated_at_unix_ms=?1,failure_class='lease_expired' WHERE state='running' AND external_effect_started_at_unix_ms IS NULL AND lease_expires_at_unix_ms<=?1")?;
            bind_i64(self.database, recover.raw, 1, now_unix_ms)?;
            expect_done(self.database, recover.raw)?;
            let statement = self.prepare(&format!("{} WHERE state='pending' AND next_attempt_at_unix_ms<=?1 AND attempts<?2 ORDER BY next_attempt_at_unix_ms,created_at_unix_ms,job_id LIMIT 1", publish_job_select()))?;
            bind_i64(self.database, statement.raw, 1, now_unix_ms)?;
            bind_i64(
                self.database,
                statement.raw,
                2,
                i64::from(MAX_PUBLISH_ATTEMPTS),
            )?;
            let mut record = match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => read_publish_job(statement.raw)?,
                SQLITE_DONE => return Ok(None),
                _ => return Err(sqlite_error(self.database)),
            };
            self.validate_publish_execution_job_current(&record.job)?;
            let attempts = record
                .attempts
                .checked_add(1)
                .ok_or("publish attempt count overflowed")?;
            let lease_expires = now_unix_ms
                .checked_add(MAX_PUBLISH_LEASE_MS)
                .ok_or("publish lease time overflowed")?;
            let lease_token = format!("{}-{attempts}", record.job.job_id);
            let update = self.prepare("UPDATE publish_execution_jobs SET state='running',attempts=?1,lease_token=?2,lease_expires_at_unix_ms=?3,updated_at_unix_ms=?4,failure_class=NULL WHERE job_id=?5 AND state='pending' AND attempts=?6")?;
            bind_i64(self.database, update.raw, 1, i64::from(attempts))?;
            bind_text(
                self.database,
                update.raw,
                2,
                &c_string(&lease_token, "publish lease")?,
            )?;
            bind_i64(self.database, update.raw, 3, lease_expires)?;
            bind_i64(self.database, update.raw, 4, now_unix_ms)?;
            bind_text(
                self.database,
                update.raw,
                5,
                &c_string(&record.job.job_id, "publish job")?,
            )?;
            bind_i64(self.database, update.raw, 6, i64::from(record.attempts))?;
            expect_done(self.database, update.raw)?;
            if self.changes() != 1 {
                return Err("publish job claim compare-and-set failed".into());
            }
            record.state = PublishExecutionJobState::Running;
            record.attempts = attempts;
            record.updated_at_unix_ms = now_unix_ms;
            record.lease_expires_at_unix_ms = Some(lease_expires);
            record.failure_class = None;
            Ok(Some(PublishExecutionLease {
                record,
                lease_token,
            }))
        })();
        self.finish_value_transaction(outcome)
    }

    pub(crate) fn renew_publish_execution_lease(
        &self,
        lease: &mut PublishExecutionLease,
        now_unix_ms: i64,
    ) -> Result<(), String> {
        let expires = now_unix_ms
            .checked_add(MAX_PUBLISH_LEASE_MS)
            .ok_or("publish lease time overflowed")?;
        let update = self.prepare("UPDATE publish_execution_jobs SET lease_expires_at_unix_ms=?1,updated_at_unix_ms=?2 WHERE job_id=?3 AND state='running' AND lease_token=?4 AND lease_expires_at_unix_ms>?2")?;
        bind_i64(self.database, update.raw, 1, expires)?;
        bind_i64(self.database, update.raw, 2, now_unix_ms)?;
        bind_text(
            self.database,
            update.raw,
            3,
            &c_string(&lease.record.job.job_id, "publish job")?,
        )?;
        bind_text(
            self.database,
            update.raw,
            4,
            &c_string(&lease.lease_token, "publish lease")?,
        )?;
        expect_done(self.database, update.raw)?;
        if self.changes() != 1 {
            return Err("publish job lease is stale or expired".into());
        }
        lease.record.lease_expires_at_unix_ms = Some(expires);
        lease.record.updated_at_unix_ms = now_unix_ms;
        Ok(())
    }

    pub(crate) fn mark_publish_execution_external_effect_started(
        &self,
        lease: &mut PublishExecutionLease,
        now_unix_ms: i64,
    ) -> Result<(), String> {
        if now_unix_ms < 0 {
            return Err("publish external-effect time is invalid".into());
        }
        let update = self.prepare("UPDATE publish_execution_jobs SET external_effect_started_at_unix_ms=?1,updated_at_unix_ms=?1 WHERE job_id=?2 AND state='running' AND lease_token=?3 AND lease_expires_at_unix_ms>?1 AND external_effect_started_at_unix_ms IS NULL")?;
        bind_i64(self.database, update.raw, 1, now_unix_ms)?;
        bind_text(
            self.database,
            update.raw,
            2,
            &c_string(&lease.record.job.job_id, "publish job")?,
        )?;
        bind_text(
            self.database,
            update.raw,
            3,
            &c_string(&lease.lease_token, "publish lease")?,
        )?;
        expect_done(self.database, update.raw)?;
        if self.changes() != 1 {
            return Err("publish job external-effect marker compare-and-set failed".into());
        }
        lease.record.external_effect_started_at_unix_ms = Some(now_unix_ms);
        lease.record.updated_at_unix_ms = now_unix_ms;
        Ok(())
    }

    pub(crate) fn fail_publish_execution_job(
        &self,
        lease: PublishExecutionLease,
        failure_class: &str,
        transient: bool,
        now_unix_ms: i64,
    ) -> Result<(), String> {
        if !secret_field(failure_class, 100) {
            return Err("publish failure class is invalid".into());
        }
        let retry = transient
            && lease.record.external_effect_started_at_unix_ms.is_none()
            && lease.record.attempts < MAX_PUBLISH_ATTEMPTS;
        let state = if retry { "pending" } else { "failed" };
        let delay = 1_000_i64
            .checked_mul(1_i64 << lease.record.attempts.min(8))
            .ok_or("publish retry delay overflowed")?;
        let next = if retry {
            now_unix_ms
                .checked_add(delay.min(300_000))
                .ok_or("publish retry time overflowed")?
        } else {
            now_unix_ms
        };
        let update = self.prepare("UPDATE publish_execution_jobs SET state=?1,next_attempt_at_unix_ms=?2,updated_at_unix_ms=?3,lease_token=NULL,lease_expires_at_unix_ms=NULL,failure_class=?4 WHERE job_id=?5 AND state='running' AND lease_token=?6")?;
        for (value, index, field) in [
            (state, 1, "publish state"),
            (failure_class, 4, "publish failure"),
            (&lease.record.job.job_id, 5, "publish job"),
            (&lease.lease_token, 6, "publish lease"),
        ] {
            bind_text(self.database, update.raw, index, &c_string(value, field)?)?;
        }
        bind_i64(self.database, update.raw, 2, next)?;
        bind_i64(self.database, update.raw, 3, now_unix_ms)?;
        expect_done(self.database, update.raw)?;
        if self.changes() != 1 {
            return Err("publish job failure compare-and-set failed".into());
        }
        Ok(())
    }

    pub(crate) fn complete_publish_execution_job(
        &self,
        lease: &PublishExecutionLease,
        receipts: &[PublishExecutionReceipt],
        now_unix_ms: i64,
    ) -> Result<(), String> {
        if receipts.is_empty() || receipts.len() > 5 {
            return Err("publish completion receipt chain is empty or unbounded".into());
        }
        for receipt in receipts {
            receipt.validate()?;
        }
        for pair in receipts.windows(2) {
            validate_receipt_successor(&pair[0], &pair[1])?;
        }
        let job = &lease.record.job;
        if receipts
            .iter()
            .any(|receipt| !receipt_matches_job(receipt, job))
            || !receipt_chain_proves_required_delivery(job.target_kind, receipts)
        {
            return Err("publish receipts do not prove required delivery for the exact job".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            self.validate_publish_execution_job_current(job)?;
            for receipt in receipts {
                let json = serde_json::to_string(receipt)
                    .map_err(|error| format!("serialize publish receipt: {error}"))?;
                let insert = self.prepare("INSERT INTO publish_execution_receipts(receipt_id,job_id,stage,recorded_at_unix_ms,record_json) VALUES(?1,?2,?3,?4,?5)")?;
                let stage = publish_stage_name(receipt.stage).to_string();
                for (value, index, field) in [
                    (&receipt.receipt_id, 1, "publish receipt"),
                    (&receipt.job_id, 2, "publish job"),
                    (&stage, 3, "publish stage"),
                    (&json, 5, "publish receipt json"),
                ] {
                    bind_text(self.database, insert.raw, index, &c_string(value, field)?)?;
                }
                bind_i64(self.database, insert.raw, 4, receipt.recorded_at_unix_ms)?;
                expect_done(self.database, insert.raw)?;
            }
            let update = self.prepare("UPDATE publish_execution_jobs SET state='completed',completed_at_unix_ms=?1,updated_at_unix_ms=?1,lease_token=NULL,lease_expires_at_unix_ms=NULL,failure_class=NULL WHERE job_id=?2 AND state='running' AND lease_token=?3")?;
            bind_i64(self.database, update.raw, 1, now_unix_ms)?;
            bind_text(
                self.database,
                update.raw,
                2,
                &c_string(&job.job_id, "publish job")?,
            )?;
            bind_text(
                self.database,
                update.raw,
                3,
                &c_string(&lease.lease_token, "publish lease")?,
            )?;
            expect_done(self.database, update.raw)?;
            if self.changes() != 1 {
                return Err("publish completion compare-and-set failed".into());
            }
            Ok(())
        })();
        self.finish_value_transaction(outcome)
    }

    fn validate_publish_execution_job_current(
        &self,
        job: &PublishExecutionJob,
    ) -> Result<(), String> {
        let account = self
            .read_publish_target_account(&job.account_id)?
            .ok_or("publish account is missing")?;
        let target = self
            .read_publish_target_profile(&job.target_id)?
            .ok_or("publish target is missing")?;
        let credential =
            self.read_current_publish_account_credential(&job.account_id, job.account_revision)?;
        let config =
            self.read_publish_execution_target_config(&job.target_id, job.target_revision)?;
        let asset = self
            .read_media_asset(&job.asset_id)?
            .ok_or("publish media asset is missing")?;
        if account.revision != job.account_revision
            || !account.enabled
            || account.kind != job.target_kind
            || target.account_id != job.account_id
            || target.revision != job.target_revision
            || !target.enabled
            || target.kind != job.target_kind
            || target.configuration_hash != job.configuration_hash
            || credential.as_ref().map(|value| value.0) != Some(job.credential_version)
            || config.as_ref().map(PublishExecutionTargetConfig::kind) != Some(job.target_kind)
            || asset.asset_id != job.media_sha256
            || asset.duration_ms != job.media_duration_ms
            || asset.readiness != AssetReadiness::Ready
        {
            return Err("publish execution job is stale for current account, target, credential, configuration, or media".into());
        }
        Ok(())
    }
}

fn receipt_chain_proves_required_delivery(
    kind: PublishTargetKind,
    receipts: &[PublishExecutionReceipt],
) -> bool {
    match kind {
        PublishTargetKind::Portal | PublishTargetKind::YouTubeLive => receipts
            .iter()
            .any(|receipt| receipt.stage == PublishExecutionStage::Published),
        PublishTargetKind::InternetArchive | PublishTargetKind::LocalNas => receipts
            .iter()
            .any(|receipt| receipt.stage == PublishExecutionStage::Delivered),
        PublishTargetKind::Syndication | PublishTargetKind::Podcast => false,
    }
}

pub fn run_publish_controller_iteration<F>(
    store: &StationStore,
    now_unix_ms: i64,
    mut current_install: F,
) -> Result<bool, String>
where
    F: FnMut() -> Result<(String, String, u64), String>,
{
    let mut clock = unix_time_millis;
    let adapter = SystemPublishProviderAdapter {
        application_root: None,
        data_root: None,
        nas_worker_sha256: None,
    };
    run_publish_controller_iteration_with(
        store,
        &adapter,
        now_unix_ms,
        &mut current_install,
        &mut clock,
    )
}

pub fn run_publish_execution_queue_controller(
    database: &Path,
    application_root: &Path,
    stop: Arc<AtomicBool>,
) -> Result<(), String> {
    let mut backoff = Duration::from_millis(250);
    while !stop.load(Ordering::Acquire) {
        match run_publish_execution_queue_iteration(database, application_root) {
            Ok(_) => {
                resolve_publish_controller_alert(database);
                backoff = Duration::from_millis(250);
            }
            Err(_) => {
                observe_publish_controller_alert(database);
                backoff = (backoff * 2).min(Duration::from_secs(5));
            }
        }
        std::thread::sleep(backoff);
    }
    Ok(())
}

fn run_publish_execution_queue_iteration(
    database: &Path,
    application_root: &Path,
) -> Result<bool, String> {
    let data_root = database
        .parent()
        .ok_or_else(|| "publishing data root is unavailable".to_string())?;
    let fence = station_maintenance::acquire_install_authority_fence(data_root)
        .map_err(|_| "publishing install authority is busy".to_string())?;
    if fence.was_abandoned() {
        return Err("publishing install authority requires recovery".into());
    }
    let store = StationStore::open(database)?;
    let now = unix_time_millis()?;
    let adapter = SystemPublishProviderAdapter {
        application_root: Some(application_root),
        data_root: Some(data_root),
        // Existing installations may predate the optional helper. Keep the
        // other provider lanes update-compatible; a NAS job fails closed when
        // this attested helper hash is unavailable.
        nas_worker_sha256: station_maintenance::read_verified_active_application_file_hash(
            data_root,
            application_root,
            "nas-publisher.exe",
        )
        .ok(),
    };
    let mut clock = unix_time_millis;
    run_publish_controller_iteration_with(
        &store,
        &adapter,
        now,
        &mut || {
            let candidate =
                station_maintenance::read_verified_install_authority(data_root, application_root)?;
            Ok((
                candidate.version().into(),
                candidate.source_commit().into(),
                candidate.install_generation(),
            ))
        },
        &mut clock,
    )
}

const PUBLISH_CONTROLLER_ALERT_RESOURCE: &str = "publishing-execution-controller";

fn observe_publish_controller_alert(database: &Path) {
    let Ok(now) = unix_time_millis() else {
        return;
    };
    let Ok(store) = StationStore::open(database) else {
        return;
    };
    let _ = store.observe_alert_and_route(&AlertEvent {
        event_id: "publishing-execution-controller-service-down".into(),
        condition: AlertConditionKind::ServiceDown,
        severity: AlertSeverity::Warning,
        state: AlertEventState::Firing,
        resource_ref: PUBLISH_CONTROLLER_ALERT_RESOURCE.into(),
        summary: "Publishing execution controller needs operator attention.".into(),
        detail: "The controller could not complete a durable queue iteration and is retrying with bounded backoff.".into(),
        source_section: "S3".into(),
        first_observed_at_unix_ms: now,
        last_observed_at_unix_ms: now,
        resolved_at_unix_ms: None,
        occurrence_count: 1,
        acknowledged_at_unix_ms: None,
        acknowledged_by: None,
    });
}

fn resolve_publish_controller_alert(database: &Path) {
    let Ok(now) = unix_time_millis() else {
        return;
    };
    let Ok(store) = StationStore::open(database) else {
        return;
    };
    let _ = store.resolve_alert(
        AlertConditionKind::ServiceDown,
        PUBLISH_CONTROLLER_ALERT_RESOURCE,
        now,
    );
}

fn run_publish_controller_iteration_with<A: PublishProviderAdapter>(
    store: &StationStore,
    adapter: &A,
    _scheduled_now_unix_ms: i64,
    current_install: &mut dyn FnMut() -> Result<(String, String, u64), String>,
    clock: &mut dyn FnMut() -> Result<i64, String>,
) -> Result<bool, String> {
    // Lease expiry recovery and retry scheduling are evaluated against the
    // controller's current authority, never a scheduler tick retained before
    // a provider operation or restart.
    let now_unix_ms = clock()?;
    let Some(claimed) = store.claim_publish_execution_job(now_unix_ms)? else {
        return Ok(false);
    };
    let mut lease = Some(claimed);
    let execution: Result<(), String> = (|| {
        let job = lease
            .as_ref()
            .expect("publish lease is retained")
            .record()
            .job
            .clone();
        ensure_install_matches(&job, &current_install()?)?;
        let config = store
            .read_publish_execution_target_config(&job.target_id, job.target_revision)?
            .ok_or_else(|| "publish execution configuration is missing".to_string())?;
        let asset = store
            .read_media_asset(&job.asset_id)?
            .ok_or_else(|| "publish media asset is missing".to_string())?;
        let media_path = PathBuf::from(&asset.media_path);
        let (bytes, digest) = hash_publish_media(&media_path)?;
        if bytes != job.media_bytes || digest != job.media_sha256 {
            return Err("publish media changed after enqueue".into());
        }
        let (credential_version, mut credential) = store
            .read_current_publish_account_credential(&job.account_id, job.account_revision)?
            .ok_or_else(|| "publish credential is unavailable".to_string())?;
        if credential_version != job.credential_version {
            credential.fill(0);
            return Err("publish credential rotated after enqueue".into());
        }
        let external_effect_started_at_unix_ms = clock()?;
        store.mark_publish_execution_external_effect_started(
            lease
                .as_mut()
                .expect("publish lease is retained until completion"),
            external_effect_started_at_unix_ms,
        )?;
        let mut heartbeat = || {
            let now = clock()?;
            store.renew_publish_execution_lease(
                lease
                    .as_mut()
                    .expect("publish lease is retained until completion"),
                now,
            )
        };
        let evidence = adapter.execute(&job, &config, &media_path, &credential, &mut heartbeat);
        credential.fill(0);
        let evidence = evidence?;
        let completed_at_unix_ms = clock()?;
        ensure_install_matches(&job, &current_install()?)?;
        let receipts = evidence
            .into_iter()
            .enumerate()
            .map(|(index, evidence)| PublishExecutionReceipt {
                receipt_id: deterministic_receipt_id(&job.job_id, index, evidence.stage),
                job_id: job.job_id.clone(),
                account_id: job.account_id.clone(),
                account_revision: job.account_revision,
                target_id: job.target_id.clone(),
                target_revision: job.target_revision,
                target_kind: job.target_kind,
                configuration_hash: job.configuration_hash.clone(),
                credential_version: job.credential_version,
                candidate: PublishCandidateIdentity {
                    version: job.candidate_version.clone(),
                    source_commit: job.source_commit.clone(),
                },
                media_sha256: job.media_sha256.clone(),
                media_bytes: job.media_bytes,
                stage: evidence.stage,
                delivery_identity_sha256: evidence.digest,
                occurred_at_unix_ms: completed_at_unix_ms,
                recorded_at_unix_ms: completed_at_unix_ms,
                detail: evidence.detail,
            })
            .collect::<Vec<_>>();
        store.complete_publish_execution_job(
            lease
                .as_ref()
                .expect("publish lease is retained until completion"),
            &receipts,
            completed_at_unix_ms,
        )?;
        lease.take();
        Ok(())
    })();
    match execution {
        Ok(()) => Ok(true),
        Err(error) => {
            if let Some(lease) = lease.take() {
                let failure_time = clock()?;
                let external_outcome_unknown =
                    lease.record.external_effect_started_at_unix_ms.is_some();
                store.fail_publish_execution_job(
                    lease,
                    if external_outcome_unknown {
                        "external_outcome_unknown"
                    } else {
                        classify_publish_failure(&error)
                    },
                    !external_outcome_unknown,
                    failure_time,
                )?;
            }
            Err(error)
        }
    }
}

fn classify_publish_failure(error: &str) -> &'static str {
    if error.contains("credential") {
        "credential_unavailable"
    } else if error.contains("quota") || error.contains("retryable") {
        "provider_quota_retry"
    } else if error.contains("candidate") || error.contains("stale") || error.contains("changed") {
        "authority_changed"
    } else if error.contains("media") || error.contains("NAS") {
        "media_delivery_failed"
    } else {
        "provider_request_failed"
    }
}

impl PublishProviderAdapter for SystemPublishProviderAdapter<'_> {
    fn execute(
        &self,
        job: &PublishExecutionJob,
        config: &PublishExecutionTargetConfig,
        media_path: &Path,
        credential: &[u8],
        heartbeat: &mut dyn FnMut() -> Result<(), String>,
    ) -> Result<Vec<ProviderStageEvidence>, String> {
        heartbeat()?;
        let attempted = ProviderStageEvidence {
            stage: PublishExecutionStage::Attempted,
            digest: job.media_sha256.clone(),
            detail: "Provider execution started for the exact media binding.".into(),
        };
        let terminal = match config {
            PublishExecutionTargetConfig::LocalNas { managed_root } => deliver_to_nas_for_adapter(
                job,
                media_path,
                Path::new(managed_root),
                self.application_root,
                self.nas_worker_sha256.as_deref(),
                heartbeat,
            )?,
            PublishExecutionTargetConfig::Portal { endpoint } => http_provider_delivery(
                endpoint,
                media_path,
                credential,
                PublishExecutionStage::Published,
                "Portal accepted the exact bounded publication request.",
            )?,
            PublishExecutionTargetConfig::InternetArchive { item_identifier } => {
                let endpoint = format!(
                    "https://s3.us.archive.org/{item_identifier}/{}.mp4",
                    job.media_sha256
                );
                http_provider_delivery(
                    &endpoint,
                    media_path,
                    credential,
                    PublishExecutionStage::Delivered,
                    "Internet Archive accepted the exact bounded object delivery.",
                )?
            }
            PublishExecutionTargetConfig::YouTubeLive {
                channel_id,
                broadcast_id,
                stream_id,
                privacy_status,
            } => youtube_live_runtime::execute_system(
                job,
                channel_id,
                broadcast_id,
                stream_id,
                *privacy_status,
                media_path,
                credential,
                self.application_root.ok_or_else(|| {
                    "YouTube production execution requires the verified application root"
                        .to_string()
                })?,
                self.data_root.ok_or_else(|| {
                    "YouTube production execution requires the verified data root".to_string()
                })?,
                heartbeat,
            )?,
        };
        heartbeat()?;
        Ok(vec![attempted, terminal])
    }
}

fn http_provider_delivery(
    endpoint: &str,
    media_path: &Path,
    credential: &[u8],
    stage: PublishExecutionStage,
    detail: &str,
) -> Result<ProviderStageEvidence, String> {
    let authorization = std::str::from_utf8(credential)
        .map_err(|_| "publish credential is not valid header material")?;
    if authorization.chars().any(char::is_control) {
        return Err("publish credential is not valid header material".into());
    }
    let media = File::open(media_path).map_err(|_| "publish media could not be opened")?;
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .timeout_global(Some(Duration::from_secs(600)))
        .max_redirects(0)
        .max_redirects_will_error(true)
        .build()
        .into();
    let mut response = agent
        .put(endpoint)
        .header("Authorization", authorization)
        .header("Content-Type", "video/mp4")
        .send(media)
        .map_err(|_| "authenticated publishing request failed")?;
    let mut body = Vec::new();
    response
        .body_mut()
        .as_reader()
        .take((MAX_PROVIDER_RESPONSE_BYTES + 1) as u64)
        .read_to_end(&mut body)
        .map_err(|_| "publishing response could not be read")?;
    if body.is_empty() || body.len() > MAX_PROVIDER_RESPONSE_BYTES {
        return Err("publishing response is empty or oversized".into());
    }
    Ok(ProviderStageEvidence {
        stage,
        digest: format!("{:x}", Sha256::digest(&body)),
        detail: detail.into(),
    })
}

#[allow(dead_code)]
fn deliver_to_nas(
    job: &PublishExecutionJob,
    media_path: &Path,
    root: &Path,
) -> Result<ProviderStageEvidence, String> {
    let mut heartbeat = || Ok(());
    deliver_to_nas_with_heartbeat(job, media_path, root, &mut heartbeat)
}

fn deliver_to_nas_with_heartbeat(
    job: &PublishExecutionJob,
    media_path: &Path,
    root: &Path,
    heartbeat: &mut dyn FnMut() -> Result<(), String>,
) -> Result<ProviderStageEvidence, String> {
    if job.target_kind != PublishTargetKind::LocalNas {
        return Err("NAS delivery job has the wrong target kind".into());
    }
    validate_managed_nas_root(root)?;
    let started = std::time::Instant::now();
    let (root_pins, destination) = prepare_nas_destination(root, &job.media_sha256)?;
    let root_handle = root_pins
        .last()
        .ok_or("managed NAS root ancestry was not retained")?;
    let destination_name = destination
        .file_name()
        .ok_or("NAS destination leaf is unavailable")?;
    if let Some(mut existing) = open_nas_final_relative(root_handle, destination_name)? {
        return reconcile_existing_nas_destination(
            &mut existing,
            &destination,
            root,
            root_handle,
            job,
            started,
        );
    }

    let mut source = open_publish_source_no_follow(media_path)?;
    let source_metadata = source
        .metadata()
        .map_err(|_| "publish media could not be inspected")?;
    if source_metadata.len() == 0 || source_metadata.len() != job.media_bytes {
        return Err("publish media bytes do not match the exact job binding".into());
    }
    source
        .seek(SeekFrom::Start(0))
        .map_err(|_| "publish media could not be positioned")?;

    let temporary = unique_nas_temp_path(root, &job.job_id)?;
    #[cfg(windows)]
    let temporary_name = temporary
        .file_name()
        .ok_or("NAS temporary leaf is unavailable")?;
    #[cfg(windows)]
    let mut output = open_nas_temp_relative(root_handle, temporary_name)?;
    #[cfg(not(windows))]
    let mut output = open_nas_temp(&temporary)?;
    let mut finalized = false;
    let mut hasher = Sha256::new();
    let mut copied = 0_u64;
    let mut buffer = vec![0_u8; NAS_COPY_BUFFER_BYTES];
    let copy_result = (|| {
        loop {
            heartbeat()?;
            if started.elapsed() >= MAX_NAS_COPY_DURATION {
                return Err("NAS publication exceeded its bounded deadline".into());
            }
            let read = source
                .read(&mut buffer)
                .map_err(|_| "publish media could not be read from its retained handle")?;
            if read == 0 {
                break;
            }
            copied = copied
                .checked_add(read as u64)
                .ok_or("publish media size overflowed")?;
            if copied > job.media_bytes {
                return Err("publish media grew beyond the exact job binding".into());
            }
            hasher.update(&buffer[..read]);
            output
                .write_all(&buffer[..read])
                .map_err(|_| "NAS temporary media could not be written")?;
        }
        if copied != job.media_bytes {
            return Err("publish media ended before the exact job byte count".into());
        }
        let digest = format!("{:x}", hasher.finalize());
        if digest != job.media_sha256 {
            return Err("publish media changed on its retained handle".into());
        }
        output
            .flush()
            .and_then(|()| output.sync_all())
            .map_err(|_| "NAS temporary media could not be durably flushed")?;
        heartbeat()?;
        if started.elapsed() >= MAX_NAS_COPY_DURATION {
            return Err("NAS publication exceeded its bounded deadline".into());
        }
        rename_nas_temp_from_handle(&output, &destination, root_handle)?;
        // From this point the handle names the final destination. It is no
        // longer an owned temporary and must never be passed to cleanup, even
        // when a post-rename durability/evidence check fails.
        finalized = true;
        output
            .sync_all()
            .map_err(|_| "NAS finalized media could not be durably flushed")?;
        output
            .seek(SeekFrom::Start(0))
            .map_err(|_| "NAS finalized media could not be positioned")?;
        let (final_bytes, final_digest) = hash_publish_handle_until(&mut output, Some(started))?;
        if final_bytes != job.media_bytes || final_digest != job.media_sha256 {
            return Err("NAS finalized media does not match the exact job binding".into());
        }
        verify_nas_root_file_id(root, root_handle)?;
        Ok(())
    })();
    if let Err(error) = copy_result {
        if finalized {
            drop(output);
            return Err(error);
        }
        let cleanup = discard_owned_nas_temp(&output);
        drop(output);
        return match cleanup {
            Ok(()) => Err(error),
            Err(cleanup_error) => Err(format!(
                "{error}; NAS temporary cleanup failed: {cleanup_error}"
            )),
        };
    }
    drop(output);
    Ok(ProviderStageEvidence {
        stage: PublishExecutionStage::Delivered,
        digest: job.media_sha256.clone(),
        detail: format!(
            "Exact media delivered atomically to the configured NAS root at {}.",
            destination.display()
        ),
    })
}

/// Reconcile a final path left behind when the controller lost the helper
/// result after the atomic rename. The path is accepted only when a fresh
/// no-follow handle proves it is a regular file with the exact bound bytes and
/// digest; mismatches remain a hard overwrite/rebind failure.
fn reconcile_existing_nas_destination(
    file: &mut File,
    destination: &Path,
    root: &Path,
    root_handle: &File,
    job: &PublishExecutionJob,
    started: std::time::Instant,
) -> Result<ProviderStageEvidence, String> {
    let metadata = file
        .metadata()
        .map_err(|_| "existing NAS destination could not be inspected")?;
    if metadata.len() != job.media_bytes {
        return Err(
            "NAS destination already exists with different bytes; overwrite or rebind is forbidden"
                .into(),
        );
    }
    file.seek(SeekFrom::Start(0))
        .map_err(|_| "existing NAS destination could not be positioned")?;
    let (bytes, digest) = hash_publish_handle_until(file, Some(started))?;
    if bytes != job.media_bytes || digest != job.media_sha256 {
        return Err("NAS destination already exists with different content; overwrite or rebind is forbidden".into());
    }
    verify_nas_root_file_id(root, root_handle)?;
    Ok(ProviderStageEvidence {
        stage: PublishExecutionStage::Delivered,
        digest: job.media_sha256.clone(),
        detail: format!(
            "Exact media was reconciled at the configured NAS root after atomic finalization at {}.",
            destination.display()
        ),
    })
}

fn validate_managed_nas_root(root: &Path) -> Result<(), String> {
    let value = root
        .to_str()
        .ok_or("managed NAS root is not valid Unicode")?;
    station_publishing::validate_local_nas_root(value)
}

#[cfg(windows)]
fn verify_nas_root_file_id(path: &Path, retained: &File) -> Result<(), String> {
    use windows_sys::Win32::Storage::FileSystem::{
        BY_HANDLE_FILE_INFORMATION, GetFileInformationByHandle,
    };
    let mut current = BY_HANDLE_FILE_INFORMATION::default();
    let status = unsafe { GetFileInformationByHandle(retained.as_raw_handle(), &mut current) };
    if status == 0 {
        return Err("managed NAS root identity could not be read from its retained handle".into());
    }
    let fresh = open_nas_directory_no_follow(path)?;
    let mut rebound = BY_HANDLE_FILE_INFORMATION::default();
    let status = unsafe { GetFileInformationByHandle(fresh.as_raw_handle(), &mut rebound) };
    if status == 0
        || current.dwVolumeSerialNumber != rebound.dwVolumeSerialNumber
        || current.nFileIndexHigh != rebound.nFileIndexHigh
        || current.nFileIndexLow != rebound.nFileIndexLow
    {
        return Err("configured NAS root was rebound after acquisition".into());
    }
    Ok(())
}

#[cfg(not(windows))]
fn verify_nas_root_file_id(_path: &Path, _retained: &File) -> Result<(), String> {
    Ok(())
}

fn prepare_nas_destination(
    root: &Path,
    media_sha256: &str,
) -> Result<(Vec<File>, PathBuf), String> {
    let mut existing = root.to_path_buf();
    let mut missing = Vec::new();
    loop {
        match std::fs::symlink_metadata(&existing) {
            Ok(metadata) => {
                if metadata.file_attributes() & 0x0400 != 0 || !metadata.is_dir() {
                    return Err("managed NAS root contains a reparse point or non-directory".into());
                }
                break;
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                let name = existing
                    .file_name()
                    .ok_or("managed NAS root has no existing ancestor")?;
                missing.push(name.to_os_string());
                existing = existing
                    .parent()
                    .ok_or("managed NAS root has no existing ancestor")?
                    .to_path_buf();
            }
            Err(_) => return Err("managed NAS root could not be inspected".into()),
        }
    }
    if !missing.is_empty() {
        return Err(
            "managed NAS root must already exist; directory creation is not permitted".into(),
        );
    }
    let current = existing;
    // Pin every directory from the volume/share root through the configured
    // root with handle-relative, no-follow opens. Higher ancestors may allow
    // delete sharing because they are not the configured authority; the
    // configured root itself remains non-delete-shared. A final fresh-path
    // FileId recheck rejects a root rebind before any receipt is emitted.
    // Failure to pin any link fails closed rather than falling back to a
    // nearest pathname anchor.
    let mut chain = Vec::new();
    let mut ancestor = current.clone();
    loop {
        chain.push(ancestor.clone());
        let parent = ancestor.parent().map(Path::to_path_buf);
        match parent {
            Some(parent) if parent != ancestor => ancestor = parent,
            _ => break,
        }
    }
    chain.reverse();
    let mut pins = Vec::with_capacity(chain.len());
    for (index, path) in chain.iter().enumerate() {
        if index == 0 {
            pins.push(open_nas_directory_no_follow(path)?);
            continue;
        }
        let leaf = path
            .file_name()
            .ok_or("managed NAS ancestry component is unavailable")?;
        let parent = pins
            .last()
            .ok_or("managed NAS ancestry parent was not retained")?;
        pins.push(open_nas_directory_relative(parent, leaf, path != &current)?);
    }
    let destination = current.join(format!("{media_sha256}.mp4"));
    Ok((pins, destination))
}

#[derive(Debug, serde::Serialize, serde::Deserialize)]
struct NasWorkerRequest {
    job: PublishExecutionJob,
    media_path: String,
    managed_root: String,
}

fn deliver_to_nas_for_adapter(
    job: &PublishExecutionJob,
    media_path: &Path,
    root: &Path,
    application_root: Option<&Path>,
    nas_worker_sha256: Option<&str>,
    heartbeat: &mut dyn FnMut() -> Result<(), String>,
) -> Result<ProviderStageEvidence, String> {
    #[cfg(windows)]
    if application_root.is_some() {
        return deliver_to_nas_in_worker_process(
            job,
            media_path,
            root,
            nas_worker_sha256,
            heartbeat,
        );
    }
    let _ = application_root;
    deliver_to_nas_with_heartbeat(job, media_path, root, heartbeat)
}

/// Entry point for the separately installed NAS publisher helper. It is kept
/// deliberately tiny: all durable authority remains in the parent controller.
#[allow(dead_code)]
pub fn run_local_nas_publisher_worker_from_env() -> Result<(), String> {
    let request = std::env::var("TOWNLIGHT_NAS_PUBLISH_REQUEST_JSON")
        .map_err(|_| "NAS publisher worker request is missing")?;
    if request.len() > MAX_NAS_WORKER_REQUEST_BYTES {
        return Err("NAS publisher worker request is oversized".into());
    }
    let request: NasWorkerRequest =
        serde_json::from_str(&request).map_err(|_| "NAS publisher worker request is invalid")?;
    if request.media_path.len() > 2_000 || request.managed_root.len() > 500 {
        return Err("NAS publisher worker request path is oversized".into());
    }
    let expected_helper_hash = std::env::var("TOWNLIGHT_NAS_PUBLISH_HELPER_SHA256")
        .map_err(|_| "NAS publisher worker helper identity is missing")?;
    let helper_path = std::env::current_exe()
        .map_err(|_| "NAS publisher worker executable path is unavailable")?;
    let mut helper = open_publish_source_no_follow(&helper_path)?;
    let (_, helper_hash) = hash_publish_handle(&mut helper)?;
    if helper_hash != expected_helper_hash {
        return Err("NAS publisher worker executable changed after install verification".into());
    }
    let evidence = deliver_to_nas(
        &request.job,
        Path::new(&request.media_path),
        Path::new(&request.managed_root),
    )?;
    let encoded = serde_json::to_vec(&evidence)
        .map_err(|_| "NAS publisher worker result could not be encoded")?;
    if encoded.len() > MAX_NAS_WORKER_RESULT_BYTES {
        return Err("NAS publisher worker result is oversized".into());
    }
    std::io::stdout()
        .write_all(&encoded)
        .map_err(|_| "NAS publisher worker result could not be written")?;
    Ok(())
}

#[cfg(windows)]
fn deliver_to_nas_in_worker_process(
    job: &PublishExecutionJob,
    media_path: &Path,
    root: &Path,
    expected_helper_hash: Option<&str>,
    heartbeat: &mut dyn FnMut() -> Result<(), String>,
) -> Result<ProviderStageEvidence, String> {
    let executable = std::env::current_exe()
        .map_err(|_| "NAS publisher helper path is unavailable")?
        .with_file_name("nas-publisher.exe");
    if !executable.is_file() {
        return Err("NAS publisher helper is missing from the installed application".into());
    }
    let request = NasWorkerRequest {
        job: job.clone(),
        media_path: media_path
            .to_str()
            .ok_or("publish media path is not valid Unicode")?
            .into(),
        managed_root: root
            .to_str()
            .ok_or("managed NAS root is not valid Unicode")?
            .into(),
    };
    let encoded = serde_json::to_string(&request)
        .map_err(|_| "NAS publisher worker request could not be encoded")?;
    if encoded.len() > MAX_NAS_WORKER_REQUEST_BYTES {
        return Err("NAS publisher worker request is oversized".into());
    }
    let expected_helper_hash =
        expected_helper_hash.ok_or("NAS publisher helper has no verified installed hash")?;
    let mut helper_pin = open_publish_source_no_follow(&executable)?;
    let (_, actual_helper_hash) = hash_publish_handle(&mut helper_pin)?;
    if actual_helper_hash != expected_helper_hash {
        return Err("NAS publisher helper does not match the installed manifest".into());
    }
    let mut worker =
        NasWorkerProcess::spawn(&executable, encoded, expected_helper_hash, helper_pin)?;
    // Drain stdout while the helper is running. Even though the protocol is
    // capped below the documented result bound, draining concurrently keeps
    // the parent independent of the platform's anonymous-pipe capacity and
    // guarantees the reader is joined after every reap path.
    let stdout = worker
        .child
        .stdout
        .take()
        .ok_or("NAS publisher worker result pipe is unavailable")?;
    let result_reader = std::thread::spawn(move || {
        let stdout = stdout;
        let mut bytes = Vec::new();
        stdout
            .take((MAX_NAS_WORKER_RESULT_BYTES + 1) as u64)
            .read_to_end(&mut bytes)
            .map_err(|_| "NAS publisher worker result could not be read".to_string())?;
        Ok::<Vec<u8>, String>(bytes)
    });
    let started = std::time::Instant::now();
    let result = (|| {
        loop {
            if let Some(status) = worker
                .child
                .try_wait()
                .map_err(|_| "NAS publisher worker status could not be read")?
            {
                if !status.success() {
                    return Err("NAS publisher worker failed before finalization".into());
                }
                break;
            }
            heartbeat()?;
            if started.elapsed() >= MAX_NAS_COPY_DURATION {
                return Err("NAS publication exceeded its hard worker deadline".into());
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        Ok(())
    })();
    match result {
        Ok(()) => {
            let reap = worker.reap();
            let reader = result_reader.join();
            reap?;
            let bytes =
                reader.map_err(|_| "NAS publisher worker result reader panicked".to_string())??;
            if bytes.len() > MAX_NAS_WORKER_RESULT_BYTES {
                return Err("NAS publisher worker result is oversized".into());
            }
            let evidence: ProviderStageEvidence = serde_json::from_slice(&bytes)
                .map_err(|_| "NAS publisher worker result is invalid")?;
            if evidence.stage != PublishExecutionStage::Delivered
                || evidence.digest != job.media_sha256
            {
                return Err(
                    "NAS publisher worker result is not bound to the exact job media".into(),
                );
            }
            Ok(evidence)
        }
        Err(error) => {
            let reap = worker.terminate_and_reap();
            let reader = result_reader
                .join()
                .map_err(|_| "NAS publisher worker result reader panicked".to_string());
            if let Err(reap_error) = reap {
                return Err(format!("{error}; NAS worker reap failed: {reap_error}"));
            }
            if reader.is_err() {
                return Err(format!("{error}; NAS worker result reader failed"));
            }
            Err(error)
        }
    }
}

#[cfg(windows)]
struct NasWorkerProcess {
    child: Child,
    job: NasWorkerJobObject,
    _helper_pin: File,
}

#[cfg(windows)]
impl NasWorkerProcess {
    fn spawn(
        executable: &Path,
        request: String,
        helper_hash: &str,
        helper_pin: File,
    ) -> Result<Self, String> {
        let job = NasWorkerJobObject::new()?;
        let mut child = Command::new(executable)
            .arg("--nas-publisher-worker")
            .env("TOWNLIGHT_NAS_PUBLISH_REQUEST_JSON", request)
            .env("TOWNLIGHT_NAS_PUBLISH_HELPER_SHA256", helper_hash)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .creation_flags(windows_sys::Win32::System::Threading::CREATE_SUSPENDED)
            .spawn()
            .map_err(|_| "NAS publisher worker could not be spawned")?;
        if unsafe {
            windows_sys::Win32::System::JobObjects::AssignProcessToJobObject(
                job.handle,
                child.as_raw_handle() as windows_sys::Win32::Foundation::HANDLE,
            )
        } == 0
        {
            let _ = child.kill();
            let _ = child.wait();
            return Err("NAS publisher worker could not be contained".into());
        }
        if let Err(error) = resume_process(child.id()) {
            let _ = job.terminate();
            let _ = child.wait();
            return Err(error);
        }
        Ok(Self {
            child,
            job,
            _helper_pin: helper_pin,
        })
    }

    fn reap(&mut self) -> Result<(), String> {
        self.child
            .wait()
            .map(|_| ())
            .map_err(|_| "NAS publisher worker could not be reaped".into())
    }

    fn terminate_and_reap(&mut self) -> Result<(), String> {
        let _ = self.job.terminate();
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        loop {
            if self
                .child
                .try_wait()
                .map_err(|_| "NAS publisher worker status could not be read")?
                .is_some()
            {
                return Ok(());
            }
            if std::time::Instant::now() >= deadline {
                return Err("NAS publisher worker did not terminate within the reap bound".into());
            }
            std::thread::sleep(Duration::from_millis(25));
        }
    }
}

#[cfg(windows)]
struct NasWorkerJobObject {
    handle: windows_sys::Win32::Foundation::HANDLE,
}

#[cfg(windows)]
impl NasWorkerJobObject {
    fn new() -> Result<Self, String> {
        use windows_sys::Win32::System::JobObjects::{
            CreateJobObjectW, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JobObjectExtendedLimitInformation,
            SetInformationJobObject,
        };
        let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if handle.is_null() {
            return Err("NAS publisher worker job object could not be created".into());
        }
        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let configured = unsafe {
            SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                u32::try_from(std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>())
                    .map_err(|_| "NAS publisher worker job limits overflowed")?,
            )
        };
        if configured == 0 {
            unsafe { windows_sys::Win32::Foundation::CloseHandle(handle) };
            return Err("NAS publisher worker job object could not be configured".into());
        }
        Ok(Self { handle })
    }

    fn terminate(&self) -> Result<(), String> {
        if unsafe { windows_sys::Win32::System::JobObjects::TerminateJobObject(self.handle, 1) }
            == 0
        {
            return Err("NAS publisher worker job could not be terminated".into());
        }
        Ok(())
    }
}

#[cfg(windows)]
impl Drop for NasWorkerJobObject {
    fn drop(&mut self) {
        unsafe { windows_sys::Win32::Foundation::CloseHandle(self.handle) };
    }
}

#[cfg(windows)]
fn resume_process(process_id: u32) -> Result<(), String> {
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, TH32CS_SNAPTHREAD, THREADENTRY32, Thread32First, Thread32Next,
    };
    use windows_sys::Win32::System::Threading::{OpenThread, ResumeThread, THREAD_SUSPEND_RESUME};
    let snapshot = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0) };
    if snapshot == windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE {
        return Err("NAS publisher worker primary thread could not be found".into());
    }
    let result = (|| {
        let mut entry = THREADENTRY32 {
            dwSize: u32::try_from(std::mem::size_of::<THREADENTRY32>())
                .map_err(|_| "NAS publisher worker thread record overflowed")?,
            ..Default::default()
        };
        let mut found = unsafe { Thread32First(snapshot, &mut entry) } != 0;
        while found {
            if entry.th32OwnerProcessID == process_id {
                let thread = unsafe { OpenThread(THREAD_SUSPEND_RESUME, 0, entry.th32ThreadID) };
                if thread.is_null() {
                    return Err("NAS publisher worker primary thread could not be opened".into());
                }
                let resumed = unsafe { ResumeThread(thread) };
                unsafe { windows_sys::Win32::Foundation::CloseHandle(thread) };
                return if resumed == u32::MAX {
                    Err("NAS publisher worker primary thread could not be resumed".into())
                } else {
                    Ok(())
                };
            }
            entry.dwSize = u32::try_from(std::mem::size_of::<THREADENTRY32>())
                .map_err(|_| "NAS publisher worker thread record overflowed")?;
            found = unsafe { Thread32Next(snapshot, &mut entry) } != 0;
        }
        Err("NAS publisher worker primary thread was not found".into())
    })();
    unsafe { windows_sys::Win32::Foundation::CloseHandle(snapshot) };
    result
}

#[cfg(windows)]
fn open_nas_directory_no_follow(path: &Path) -> Result<File, String> {
    open_nas_directory_no_follow_with_share(path, false)
}

#[cfg(windows)]
fn open_nas_directory_relative(
    parent: &File,
    leaf: &std::ffi::OsStr,
    share_delete: bool,
) -> Result<File, String> {
    use std::os::windows::io::{FromRawHandle, RawHandle};
    use windows_sys::Wdk::Foundation::OBJECT_ATTRIBUTES;
    use windows_sys::Wdk::Storage::FileSystem::{
        FILE_DIRECTORY_FILE, FILE_OPEN, FILE_OPEN_REPARSE_POINT, FILE_SYNCHRONOUS_IO_NONALERT,
        NtCreateFile,
    };
    use windows_sys::Win32::Foundation::{STATUS_SUCCESS, UNICODE_STRING};
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_ATTRIBUTE_NORMAL, FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE,
    };
    use windows_sys::Win32::System::IO::IO_STATUS_BLOCK;

    let name = leaf.encode_wide().collect::<Vec<_>>();
    let byte_length = u16::try_from(name.len().saturating_mul(std::mem::size_of::<u16>()))
        .map_err(|_| "managed NAS ancestry component is too long")?;
    let unicode = UNICODE_STRING {
        Length: byte_length,
        MaximumLength: byte_length,
        Buffer: name.as_ptr() as *mut u16,
    };
    let attributes = OBJECT_ATTRIBUTES {
        Length: std::mem::size_of::<OBJECT_ATTRIBUTES>() as u32,
        RootDirectory: parent.as_raw_handle(),
        ObjectName: &unicode,
        Attributes: 0,
        SecurityDescriptor: std::ptr::null(),
        SecurityQualityOfService: std::ptr::null(),
    };
    let mut iosb = IO_STATUS_BLOCK::default();
    let mut handle = std::ptr::null_mut();
    let status = unsafe {
        NtCreateFile(
            &mut handle,
            // NtCreateFile requires traverse rights on a retained directory
            // handle when it is used as OBJECT_ATTRIBUTES.RootDirectory;
            // FILE_LIST_DIRECTORY is deliberately not requested.
            0x0000_0080 | 0x0000_0020 | 0x0010_0000,
            &attributes,
            &mut iosb,
            std::ptr::null(),
            FILE_ATTRIBUTE_NORMAL,
            FILE_SHARE_READ | FILE_SHARE_WRITE | if share_delete { FILE_SHARE_DELETE } else { 0 },
            FILE_OPEN,
            FILE_DIRECTORY_FILE | FILE_OPEN_REPARSE_POINT | FILE_SYNCHRONOUS_IO_NONALERT,
            std::ptr::null(),
            0,
        )
    };
    if status != STATUS_SUCCESS || handle.is_null() {
        return Err(format!(
            "managed NAS directory could not be pinned relative to its retained parent ({}): NTSTATUS 0x{status:08x}",
            leaf.to_string_lossy()
        ));
    }
    let file = unsafe { File::from_raw_handle(handle as RawHandle) };
    let metadata = file
        .metadata()
        .map_err(|_| "managed NAS directory could not be inspected")?;
    if metadata.file_attributes() & 0x0400 != 0 || !metadata.is_dir() {
        return Err("managed NAS ancestry component is a reparse point or not a directory".into());
    }
    Ok(file)
}

#[cfg(windows)]
fn open_nas_directory_no_follow_with_share(
    path: &Path,
    share_delete: bool,
) -> Result<File, String> {
    const FILE_READ_ATTRIBUTES: u32 = 0x0000_0080;
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_DELETE,
        FILE_SHARE_READ, FILE_SHARE_WRITE,
    };
    let file = fs::OpenOptions::new()
        // Attribute access is sufficient to pin a directory and avoids
        // requesting list/read permission from SMB shares. The configured
        // root uses no delete sharing; higher ancestors may allow it solely
        // for ordinary local roots already held by other processes. Every
        // ancestor remains no-follow pinned and the configured root is
        // rechecked by FileId before a receipt.
        .access_mode(FILE_READ_ATTRIBUTES)
        .share_mode(
            FILE_SHARE_READ | FILE_SHARE_WRITE | if share_delete { FILE_SHARE_DELETE } else { 0 },
        )
        .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path)
        .map_err(|error| {
            format!(
                "managed NAS directory could not be pinned at {}: {error}",
                path.display()
            )
        })?;
    let metadata = file
        .metadata()
        .map_err(|_| "managed NAS directory could not be inspected")?;
    if metadata.file_attributes() & 0x0400 != 0 || !metadata.is_dir() {
        return Err(format!(
            "managed NAS directory is a reparse point or not a directory: {}",
            path.display()
        ));
    }
    Ok(file)
}

#[cfg(not(windows))]
fn open_nas_directory_no_follow(path: &Path) -> Result<File, String> {
    let file = File::open(path).map_err(|_| "managed NAS directory could not be pinned")?;
    if !file
        .metadata()
        .map_err(|_| "managed NAS directory could not be inspected")?
        .is_dir()
    {
        return Err("managed NAS directory is not a directory".into());
    }
    Ok(file)
}

#[cfg(windows)]
fn open_publish_source_no_follow(path: &Path) -> Result<File, String> {
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_READ, FILE_SHARE_WRITE,
    };
    let file = fs::OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path)
        .map_err(|_| "publish media could not be opened")?;
    let metadata = file
        .metadata()
        .map_err(|_| "publish media could not be inspected")?;
    if metadata.file_attributes() & 0x0400 != 0 || !metadata.is_file() {
        return Err("publish media is a reparse point or not a regular file".into());
    }
    Ok(file)
}

#[cfg(not(windows))]
fn open_publish_source_no_follow(path: &Path) -> Result<File, String> {
    File::open(path).map_err(|_| "publish media could not be opened")
}

fn unique_nas_temp_path(root: &Path, job_id: &str) -> Result<PathBuf, String> {
    let mut entropy = [0_u8; 16];
    getrandom::fill(&mut entropy).map_err(|_| "NAS temporary identity entropy is unavailable")?;
    let suffix = entropy
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    Ok(root.join(format!(".townlight-publish-{job_id}-{suffix}.tmp")))
}

#[cfg(windows)]
fn open_nas_temp_relative(root: &File, leaf: &std::ffi::OsStr) -> Result<File, String> {
    use std::os::windows::io::{FromRawHandle, RawHandle};
    use windows_sys::Wdk::Foundation::OBJECT_ATTRIBUTES;
    use windows_sys::Wdk::Storage::FileSystem::{
        FILE_CREATE, FILE_NON_DIRECTORY_FILE, FILE_OPEN_REPARSE_POINT,
        FILE_SYNCHRONOUS_IO_NONALERT, NtCreateFile,
    };
    use windows_sys::Win32::Foundation::{
        GENERIC_READ, GENERIC_WRITE, STATUS_SUCCESS, UNICODE_STRING,
    };
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_ATTRIBUTE_NORMAL, FILE_SHARE_READ, FILE_SHARE_WRITE,
    };
    use windows_sys::Win32::System::IO::IO_STATUS_BLOCK;

    let name = leaf.encode_wide().collect::<Vec<_>>();
    let byte_length = u16::try_from(name.len().saturating_mul(std::mem::size_of::<u16>()))
        .map_err(|_| "NAS temporary leaf is too long")?;
    let unicode = UNICODE_STRING {
        Length: byte_length,
        MaximumLength: byte_length,
        Buffer: name.as_ptr() as *mut u16,
    };
    let attributes = OBJECT_ATTRIBUTES {
        Length: std::mem::size_of::<OBJECT_ATTRIBUTES>() as u32,
        RootDirectory: root.as_raw_handle(),
        ObjectName: &unicode,
        Attributes: 0,
        SecurityDescriptor: std::ptr::null(),
        SecurityQualityOfService: std::ptr::null(),
    };
    let mut iosb = IO_STATUS_BLOCK::default();
    let mut handle = std::ptr::null_mut();
    let status = unsafe {
        NtCreateFile(
            &mut handle,
            GENERIC_READ | GENERIC_WRITE | 0x0001_0000 | 0x0010_0000,
            &attributes,
            &mut iosb,
            std::ptr::null(),
            FILE_ATTRIBUTE_NORMAL,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            FILE_CREATE,
            FILE_NON_DIRECTORY_FILE | FILE_OPEN_REPARSE_POINT | FILE_SYNCHRONOUS_IO_NONALERT,
            std::ptr::null(),
            0,
        )
    };
    if status != STATUS_SUCCESS {
        return Err(format!(
            "NAS temporary media could not be created relative to its retained root: NTSTATUS 0x{status:08x}"
        ));
    }
    if handle.is_null() {
        return Err("NAS temporary media returned an invalid handle".into());
    }
    Ok(unsafe { File::from_raw_handle(handle as RawHandle) })
}

#[cfg(windows)]
fn open_nas_final_relative(root: &File, leaf: &std::ffi::OsStr) -> Result<Option<File>, String> {
    use std::os::windows::io::{FromRawHandle, RawHandle};
    use windows_sys::Wdk::Foundation::OBJECT_ATTRIBUTES;
    use windows_sys::Wdk::Storage::FileSystem::{
        FILE_NON_DIRECTORY_FILE, FILE_OPEN, FILE_OPEN_REPARSE_POINT, FILE_SYNCHRONOUS_IO_NONALERT,
        NtCreateFile,
    };
    use windows_sys::Win32::Foundation::{
        GENERIC_READ, STATUS_OBJECT_NAME_NOT_FOUND, STATUS_SUCCESS, UNICODE_STRING,
    };
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_ATTRIBUTE_NORMAL, FILE_SHARE_READ, FILE_SHARE_WRITE,
    };
    use windows_sys::Win32::System::IO::IO_STATUS_BLOCK;

    let name = leaf.encode_wide().collect::<Vec<_>>();
    let byte_length = u16::try_from(name.len().saturating_mul(std::mem::size_of::<u16>()))
        .map_err(|_| "NAS destination leaf is too long")?;
    let unicode = UNICODE_STRING {
        Length: byte_length,
        MaximumLength: byte_length,
        Buffer: name.as_ptr() as *mut u16,
    };
    let attributes = OBJECT_ATTRIBUTES {
        Length: std::mem::size_of::<OBJECT_ATTRIBUTES>() as u32,
        RootDirectory: root.as_raw_handle(),
        ObjectName: &unicode,
        Attributes: 0,
        SecurityDescriptor: std::ptr::null(),
        SecurityQualityOfService: std::ptr::null(),
    };
    let mut iosb = IO_STATUS_BLOCK::default();
    let mut handle = std::ptr::null_mut();
    let status = unsafe {
        NtCreateFile(
            &mut handle,
            GENERIC_READ | 0x0010_0000,
            &attributes,
            &mut iosb,
            std::ptr::null(),
            FILE_ATTRIBUTE_NORMAL,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            FILE_OPEN,
            FILE_NON_DIRECTORY_FILE | FILE_OPEN_REPARSE_POINT | FILE_SYNCHRONOUS_IO_NONALERT,
            std::ptr::null(),
            0,
        )
    };
    if status == STATUS_OBJECT_NAME_NOT_FOUND {
        return Ok(None);
    }
    if status != STATUS_SUCCESS || handle.is_null() {
        return Err(format!(
            "NAS destination could not be opened relative to its retained root: NTSTATUS 0x{status:08x}"
        ));
    }
    let file = unsafe { File::from_raw_handle(handle as RawHandle) };
    let metadata = file
        .metadata()
        .map_err(|_| "existing NAS destination could not be inspected")?;
    if metadata.file_attributes() & 0x0400 != 0 || !metadata.is_file() {
        return Err("existing NAS destination is a reparse point or not a regular file".into());
    }
    Ok(Some(file))
}

#[cfg(not(windows))]
fn open_nas_final_relative(_root: &File, _leaf: &std::ffi::OsStr) -> Result<Option<File>, String> {
    Ok(None)
}

#[cfg(not(windows))]
fn open_nas_temp(path: &Path) -> Result<File, String> {
    fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|_| "NAS temporary media could not be created".into())
}

fn hash_publish_handle(file: &mut File) -> Result<(u64, String), String> {
    hash_publish_handle_until(file, None)
}

fn hash_publish_handle_until(
    file: &mut File,
    started: Option<std::time::Instant>,
) -> Result<(u64, String), String> {
    let mut hasher = Sha256::new();
    let mut bytes = 0_u64;
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        if started.is_some_and(|started| started.elapsed() >= MAX_NAS_COPY_DURATION) {
            return Err("NAS publication exceeded its bounded deadline".into());
        }
        let read = file
            .read(&mut buffer)
            .map_err(|_| "NAS finalized media could not be read")?;
        if read == 0 {
            break;
        }
        bytes = bytes
            .checked_add(read as u64)
            .ok_or("NAS finalized media size overflowed")?;
        hasher.update(&buffer[..read]);
    }
    Ok((bytes, format!("{:x}", hasher.finalize())))
}

#[cfg(windows)]
fn rename_nas_temp_from_handle(file: &File, destination: &Path, root: &File) -> Result<(), String> {
    use windows_sys::Wdk::Storage::FileSystem::{
        FILE_RENAME_INFORMATION, FileRenameInformation, NtSetInformationFile,
    };
    use windows_sys::Win32::Foundation::STATUS_SUCCESS;
    use windows_sys::Win32::System::IO::IO_STATUS_BLOCK;
    let name = destination
        .file_name()
        .ok_or("NAS destination leaf is unavailable")?
        .encode_wide()
        .collect::<Vec<_>>();
    // Use the native rename primitive: unlike the Win32 convenience wrapper,
    // NtSetInformationFile applies RootDirectory to the retained directory
    // handle and therefore never reparses the configured path during the
    // final transition.
    let header_bytes = std::mem::offset_of!(FILE_RENAME_INFORMATION, FileName);
    let info_bytes = header_bytes
        .checked_add(
            name.len()
                .checked_mul(std::mem::size_of::<u16>())
                .ok_or("NAS destination path is too long")?,
        )
        .ok_or("NAS destination path is too long")?;
    let info_length = u32::try_from(info_bytes).map_err(|_| "NAS destination path is too long")?;
    let words = info_bytes
        .checked_add(std::mem::size_of::<usize>() - 1)
        .ok_or("NAS destination path is too long")?
        / std::mem::size_of::<usize>();
    let mut info = vec![0_usize; words];
    let rename = info.as_mut_ptr().cast::<FILE_RENAME_INFORMATION>();
    // ReplaceIfExists is deliberately false: a pre-existing destination or
    // a leaf created after the initial check must make the operation fail.
    unsafe {
        (*rename).Anonymous.ReplaceIfExists = false;
        (*rename).RootDirectory = root.as_raw_handle();
        (*rename).FileNameLength = u32::try_from(name.len() * std::mem::size_of::<u16>())
            .map_err(|_| "NAS destination path is too long")?;
        std::ptr::copy_nonoverlapping(name.as_ptr(), (*rename).FileName.as_mut_ptr(), name.len());
        let mut iosb = IO_STATUS_BLOCK::default();
        let status = NtSetInformationFile(
            file.as_raw_handle(),
            &mut iosb,
            rename.cast(),
            info_length,
            FileRenameInformation,
        );
        if status != STATUS_SUCCESS {
            return Err(format!(
                "NAS atomic finalization failed: NTSTATUS 0x{status:08x} ({})",
                std::io::Error::last_os_error()
            ));
        }
    }
    Ok(())
}

#[cfg(not(windows))]
fn rename_nas_temp_from_handle(
    _file: &File,
    destination: &Path,
    _root: &File,
) -> Result<(), String> {
    Err(format!(
        "NAS handle-anchored finalization is unavailable on this platform ({})",
        destination.display()
    ))
}

#[cfg(windows)]
fn discard_owned_nas_temp(file: &File) -> Result<(), String> {
    discard_nas_temp_by_handle(file)
}

#[cfg(windows)]
fn discard_nas_temp_by_handle(file: &File) -> Result<(), String> {
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_DISPOSITION_INFO, FileDispositionInfo, SetFileInformationByHandle,
    };
    let mut disposition = FILE_DISPOSITION_INFO { DeleteFile: true };
    // The temp was opened with DELETE access and without FILE_SHARE_DELETE.
    // Deleting through this exact handle cannot affect a replacement pathname.
    let status = unsafe {
        SetFileInformationByHandle(
            file.as_raw_handle(),
            FileDispositionInfo,
            (&mut disposition as *mut FILE_DISPOSITION_INFO).cast(),
            std::mem::size_of::<FILE_DISPOSITION_INFO>() as u32,
        )
    };
    if status == 0 {
        return Err(format!(
            "NAS temporary media could not be removed: {}",
            std::io::Error::last_os_error()
        ));
    }
    Ok(())
}

#[cfg(not(windows))]
fn discard_owned_nas_temp(_file: &File) -> Result<(), String> {
    Ok(())
}

fn hash_publish_media(path: &Path) -> Result<(u64, String), String> {
    let mut file = open_publish_source_no_follow(path)?;
    hash_publish_handle(&mut file).map_err(|_| "publish media could not be read".into())
}

fn ensure_install_matches(
    job: &PublishExecutionJob,
    current: &(String, String, u64),
) -> Result<(), String> {
    if current.0 != job.candidate_version
        || current.1 != job.source_commit
        || current.2 != job.install_generation
    {
        return Err("installed candidate changed during publishing execution".into());
    }
    Ok(())
}

fn deterministic_receipt_id(job_id: &str, ordinal: usize, stage: PublishExecutionStage) -> String {
    let digest = format!(
        "{:x}",
        Sha256::digest(format!("{job_id}:{ordinal}:{}", publish_stage_name(stage)).as_bytes())
    );
    format!(
        "{}-{}-{}-{}-{}",
        &digest[0..8],
        &digest[8..12],
        &digest[12..16],
        &digest[16..20],
        &digest[20..32]
    )
}

fn unix_time_millis() -> Result<i64, String> {
    SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|_| "system clock is before Unix epoch".to_string())?
        .as_millis()
        .try_into()
        .map_err(|_| "system clock exceeds supported range".to_string())
}

fn publish_job_select() -> &'static str {
    "SELECT job_id,account_id,account_revision,target_id,target_revision,target_kind,configuration_hash,credential_version,candidate_version,source_commit,install_generation,asset_id,media_sha256,media_bytes,media_duration_ms,requested_by,created_at_unix_ms,state,attempts,next_attempt_at_unix_ms,updated_at_unix_ms,COALESCE(lease_expires_at_unix_ms,-1),COALESCE(completed_at_unix_ms,-1),COALESCE(external_effect_started_at_unix_ms,-1),COALESCE(failure_class,'') FROM publish_execution_jobs"
}

fn read_publish_job(statement: *mut SqliteStatement) -> Result<PublishExecutionJobRecord, String> {
    let job = PublishExecutionJob {
        job_id: column_text(statement, 0)?,
        account_id: column_text(statement, 1)?,
        account_revision: column_u64(statement, 2, "account revision")?,
        target_id: column_text(statement, 3)?,
        target_revision: column_u64(statement, 4, "target revision")?,
        target_kind: parse_publish_kind(&column_text(statement, 5)?)?,
        configuration_hash: column_text(statement, 6)?,
        credential_version: column_u64(statement, 7, "credential version")?,
        candidate_version: column_text(statement, 8)?,
        source_commit: column_text(statement, 9)?,
        install_generation: column_u64(statement, 10, "install generation")?,
        asset_id: column_text(statement, 11)?,
        media_sha256: column_text(statement, 12)?,
        media_bytes: column_u64(statement, 13, "media bytes")?,
        media_duration_ms: column_u64(statement, 14, "media duration")?,
        requested_by: column_text(statement, 15)?,
        created_at_unix_ms: unsafe { sqlite3_column_int64(statement, 16) },
    };
    job.validate()?;
    let state = match column_text(statement, 17)?.as_str() {
        "pending" => PublishExecutionJobState::Pending,
        "running" => PublishExecutionJobState::Running,
        "completed" => PublishExecutionJobState::Completed,
        "failed" => PublishExecutionJobState::Failed,
        _ => return Err("publish job state is corrupt".into()),
    };
    Ok(PublishExecutionJobRecord {
        job,
        state,
        attempts: u32::try_from(column_u64(statement, 18, "publish attempts")?)
            .map_err(|_| "publish attempts overflowed")?,
        next_attempt_at_unix_ms: unsafe { sqlite3_column_int64(statement, 19) },
        updated_at_unix_ms: unsafe { sqlite3_column_int64(statement, 20) },
        lease_expires_at_unix_ms: optional_i64(statement, 21),
        completed_at_unix_ms: optional_i64(statement, 22),
        external_effect_started_at_unix_ms: optional_i64(statement, 23),
        failure_class: optional_column_text(statement, 24)?,
    })
}

fn optional_column_text(
    statement: *mut SqliteStatement,
    column: i32,
) -> Result<Option<String>, String> {
    let value = column_text(statement, column)?;
    Ok((!value.is_empty()).then_some(value))
}

fn parse_publish_kind(value: &str) -> Result<PublishTargetKind, String> {
    match value {
        "portal" => Ok(PublishTargetKind::Portal),
        "internet_archive" => Ok(PublishTargetKind::InternetArchive),
        "local_nas" => Ok(PublishTargetKind::LocalNas),
        "youtube_live" => Ok(PublishTargetKind::YouTubeLive),
        "syndication" => Ok(PublishTargetKind::Syndication),
        "podcast" => Ok(PublishTargetKind::Podcast),
        _ => Err("publish target kind is corrupt".into()),
    }
}
fn publish_stage_name(stage: station_publishing::PublishExecutionStage) -> &'static str {
    match stage {
        station_publishing::PublishExecutionStage::Attempted => "attempted",
        station_publishing::PublishExecutionStage::Uploaded => "uploaded",
        station_publishing::PublishExecutionStage::Delivered => "delivered",
        station_publishing::PublishExecutionStage::Published => "published",
        station_publishing::PublishExecutionStage::ExternallyVerified => "externally_verified",
    }
}
fn receipt_matches_job(receipt: &PublishExecutionReceipt, job: &PublishExecutionJob) -> bool {
    receipt.job_id == job.job_id
        && receipt.account_id == job.account_id
        && receipt.account_revision == job.account_revision
        && receipt.target_id == job.target_id
        && receipt.target_revision == job.target_revision
        && receipt.target_kind == job.target_kind
        && receipt.configuration_hash == job.configuration_hash
        && receipt.credential_version == job.credential_version
        && receipt.candidate.version == job.candidate_version
        && receipt.candidate.source_commit == job.source_commit
        && receipt.media_sha256 == job.media_sha256
        && receipt.media_bytes == job.media_bytes
}

#[cfg(test)]
#[allow(clippy::items_after_test_module)]
mod tests {
    use super::*;
    use station_publishing::{PublishExecutionStage, PublishTargetAccount, PublishTargetProfile};
    use std::sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    };

    struct FakeProvider;

    #[test]
    fn youtube_capability_evidence_cannot_skip_published_delivery() {
        let receipt = PublishExecutionReceipt {
            receipt_id: "11111111-2222-4333-8444-555555555555".into(),
            job_id: "22222222-2222-4333-8444-555555555555".into(),
            account_id: "33333333-2222-4333-8444-555555555555".into(),
            account_revision: 1,
            target_id: "44444444-2222-4333-8444-555555555555".into(),
            target_revision: 1,
            target_kind: PublishTargetKind::YouTubeLive,
            configuration_hash: "a".repeat(64),
            credential_version: 1,
            candidate: PublishCandidateIdentity {
                version: "0.2.0".into(),
                source_commit: "1".repeat(40),
            },
            media_sha256: "b".repeat(64),
            media_bytes: 1,
            stage: PublishExecutionStage::ExternallyVerified,
            delivery_identity_sha256: "c".repeat(64),
            occurred_at_unix_ms: 1,
            recorded_at_unix_ms: 1,
            detail: "Exact channel capability only; no media was published.".into(),
        };
        assert!(!receipt_chain_proves_required_delivery(
            PublishTargetKind::YouTubeLive,
            &[receipt]
        ));
    }

    impl PublishProviderAdapter for FakeProvider {
        fn execute(
            &self,
            job: &PublishExecutionJob,
            _: &PublishExecutionTargetConfig,
            _: &Path,
            _: &[u8],
            heartbeat: &mut dyn FnMut() -> Result<(), String>,
        ) -> Result<Vec<ProviderStageEvidence>, String> {
            heartbeat()?;
            Ok(vec![
                ProviderStageEvidence {
                    stage: PublishExecutionStage::Attempted,
                    digest: job.media_sha256.clone(),
                    detail: "Exact controlled attempt.".into(),
                },
                ProviderStageEvidence {
                    stage: PublishExecutionStage::Published,
                    digest: "b".repeat(64),
                    detail: "Controlled provider published exact media.".into(),
                },
            ])
        }
    }

    struct RotatingProvider<'a> {
        store: &'a StationStore,
        account_id: &'a str,
    }

    struct UnknownExternalOutcomeProvider {
        calls: Arc<AtomicUsize>,
    }

    impl PublishProviderAdapter for UnknownExternalOutcomeProvider {
        fn execute(
            &self,
            _: &PublishExecutionJob,
            _: &PublishExecutionTargetConfig,
            _: &Path,
            _: &[u8],
            _: &mut dyn FnMut() -> Result<(), String>,
        ) -> Result<Vec<ProviderStageEvidence>, String> {
            self.calls.fetch_add(1, Ordering::AcqRel);
            Err("provider outcome became unknown after execution began".into())
        }
    }

    impl PublishProviderAdapter for RotatingProvider<'_> {
        fn execute(
            &self,
            job: &PublishExecutionJob,
            _: &PublishExecutionTargetConfig,
            _: &Path,
            _: &[u8],
            _: &mut dyn FnMut() -> Result<(), String>,
        ) -> Result<Vec<ProviderStageEvidence>, String> {
            self.store.provision_publish_account_credential(
                self.account_id,
                job.account_revision,
                b"rotated-during-provider-call",
                "setup",
                9,
            )?;
            Ok(vec![
                ProviderStageEvidence {
                    stage: PublishExecutionStage::Attempted,
                    digest: job.media_sha256.clone(),
                    detail: "Controlled attempt before authority mutation.".into(),
                },
                ProviderStageEvidence {
                    stage: PublishExecutionStage::Published,
                    digest: "c".repeat(64),
                    detail: "Controlled stale response.".into(),
                },
            ])
        }
    }

    #[test]
    fn queue_claim_and_atomic_completion_bind_current_authority() {
        let path = std::env::temp_dir().join(format!(
            "townlight-publish-queue-{}.db",
            SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let media_path = path.with_extension("mp4");
        std::fs::write(&media_path, b"bounded-publish-media").unwrap();
        let media_sha = format!("{:x}", Sha256::digest(b"bounded-publish-media"));
        let store = StationStore::open(&path).unwrap();
        let account_id = "11111111-2222-4333-8444-555555555555";
        let target_id = "22222222-2222-4333-8444-555555555555";
        store
            .write_publish_target_account(
                &PublishTargetAccount {
                    account_id: account_id.into(),
                    kind: PublishTargetKind::Portal,
                    label: "Portal".into(),
                    enabled: true,
                    credential_configured: false,
                    revision: 1,
                },
                None,
                "setup",
                1,
            )
            .unwrap();
        let credential_version = store
            .provision_publish_account_credential(account_id, 1, b"provider-secret", "setup", 2)
            .unwrap();
        let config = PublishExecutionTargetConfig::Portal {
            endpoint: "https://portal.example.test/api/v1/media".into(),
        };
        let config_json = serde_json::to_string(&config).unwrap();
        let config_hash = format!("{:x}", Sha256::digest(config_json.as_bytes()));
        store
            .write_publish_target_profile_with_execution_config(
                &PublishTargetProfile {
                    target_id: target_id.into(),
                    account_id: account_id.into(),
                    kind: PublishTargetKind::Portal,
                    label: "Portal target".into(),
                    enabled: true,
                    configuration_hash: config_hash.clone(),
                    revision: 1,
                },
                None,
                &config,
                "setup",
                3,
            )
            .unwrap();
        store
            .put_media_asset(&MediaAsset {
                asset_id: media_sha.clone(),
                media_path: media_path.to_string_lossy().into_owned(),
                duration_ms: 1_000,
                readiness: AssetReadiness::Ready,
            })
            .unwrap();
        let job = PublishExecutionJob {
            job_id: "33333333-2222-4333-8444-555555555555".into(),
            account_id: account_id.into(),
            account_revision: 1,
            target_id: target_id.into(),
            target_revision: 1,
            target_kind: PublishTargetKind::Portal,
            configuration_hash: config_hash,
            credential_version,
            candidate_version: "0.1.0".into(),
            source_commit: "a".repeat(40),
            install_generation: 1,
            asset_id: media_sha.clone(),
            media_sha256: media_sha,
            media_bytes: 21,
            media_duration_ms: 1_000,
            requested_by: "setup".into(),
            created_at_unix_ms: 5,
        };
        store.enqueue_publish_execution_job(&job).unwrap();
        let mut install = || Ok(("0.1.0".into(), "a".repeat(40), 1));
        let mut clock = || Ok(7);
        assert!(
            run_publish_controller_iteration_with(
                &store,
                &FakeProvider,
                6,
                &mut install,
                &mut clock
            )
            .unwrap()
        );
        assert_eq!(
            store
                .read_publish_execution_job(&job.job_id)
                .unwrap()
                .unwrap()
                .state,
            PublishExecutionJobState::Completed
        );
        let credential_version = store
            .provision_publish_account_credential(account_id, 1, b"second-job-secret", "setup", 8)
            .unwrap();
        let mut stale_job = job.clone();
        stale_job.job_id = "33333333-2222-4333-8444-555555555556".into();
        stale_job.credential_version = credential_version;
        stale_job.created_at_unix_ms = 8;
        store.enqueue_publish_execution_job(&stale_job).unwrap();
        let mut install = || Ok(("0.1.0".into(), "a".repeat(40), 1));
        let mut clock = || Ok(9);
        assert!(
            run_publish_controller_iteration_with(
                &store,
                &RotatingProvider {
                    store: &store,
                    account_id,
                },
                9,
                &mut install,
                &mut clock,
            )
            .is_err()
        );
        assert_eq!(
            store
                .read_publish_execution_job(&stale_job.job_id)
                .unwrap()
                .unwrap()
                .state,
            PublishExecutionJobState::Failed
        );
        let statement = store
            .prepare("SELECT COUNT(*) FROM publish_execution_receipts WHERE job_id=?1")
            .unwrap();
        bind_text(
            store.database,
            statement.raw,
            1,
            &c_string(&stale_job.job_id, "publish job").unwrap(),
        )
        .unwrap();
        assert_eq!(unsafe { sqlite3_step(statement.raw) }, SQLITE_ROW);
        assert_eq!(unsafe { sqlite3_column_int64(statement.raw, 0) }, 0);

        let mut unknown_job = job.clone();
        unknown_job.job_id = "33333333-2222-4333-8444-555555555557".into();
        unknown_job.credential_version = store
            .current_publish_account_credential_version(account_id, 1)
            .unwrap()
            .unwrap();
        unknown_job.created_at_unix_ms = 10;
        store.enqueue_publish_execution_job(&unknown_job).unwrap();
        let calls = Arc::new(AtomicUsize::new(0));
        let provider = UnknownExternalOutcomeProvider {
            calls: Arc::clone(&calls),
        };
        let mut install = || Ok(("0.1.0".into(), "a".repeat(40), 1));
        let mut clock = || Ok(10_000);
        assert!(
            run_publish_controller_iteration_with(&store, &provider, 11, &mut install, &mut clock,)
                .is_err()
        );
        let record = store
            .read_publish_execution_job(&unknown_job.job_id)
            .unwrap()
            .unwrap();
        assert_eq!(record.state, PublishExecutionJobState::Failed);
        assert_eq!(
            record.failure_class.as_deref(),
            Some("external_outcome_unknown")
        );
        assert_eq!(record.updated_at_unix_ms, 10_000);
        let mut install = || Ok(("0.1.0".into(), "a".repeat(40), 1));
        let mut clock = || Ok(20_000);
        assert!(
            !run_publish_controller_iteration_with(
                &store,
                &provider,
                20_000,
                &mut install,
                &mut clock,
            )
            .unwrap()
        );
        assert_eq!(calls.load(Ordering::Acquire), 1);
        drop(store);
        let _ = std::fs::remove_file(path);
        let _ = std::fs::remove_file(media_path);
    }

    #[test]
    fn local_nas_execution_publishes_exact_bytes_without_overwrite() {
        let nonce = SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("townlight-nas-delivery-{nonce}"));
        let media = root.with_extension("media");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(&media, b"exact media").unwrap();
        let digest = format!("{:x}", Sha256::digest(b"exact media"));
        let job = PublishExecutionJob {
            job_id: "33333333-2222-4333-8444-555555555558".into(),
            account_id: "11111111-2222-4333-8444-555555555555".into(),
            account_revision: 1,
            target_id: "22222222-2222-4333-8444-555555555555".into(),
            target_revision: 1,
            target_kind: PublishTargetKind::LocalNas,
            configuration_hash: "a".repeat(64),
            credential_version: 1,
            candidate_version: "0.1.0".into(),
            source_commit: "a".repeat(40),
            install_generation: 1,
            asset_id: digest.clone(),
            media_sha256: digest,
            media_bytes: 11,
            media_duration_ms: 1_000,
            requested_by: "setup".into(),
            created_at_unix_ms: 1,
        };

        let configured_root = root.clone();
        let result = deliver_to_nas(&job, &media, &configured_root).unwrap();
        assert_eq!(result.stage, PublishExecutionStage::Delivered);
        let destination = root.join(format!("{}.mp4", job.media_sha256));
        assert_eq!(std::fs::read(&destination).unwrap(), b"exact media");
        let retry = deliver_to_nas(&job, &media, &configured_root).unwrap();
        assert!(retry.detail.contains("reconciled"));
        let _ = std::fs::remove_file(media);
        let _ = std::fs::remove_file(destination);
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn local_nas_missing_root_never_creates_directories() {
        let nonce = SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir()
            .join(format!("townlight-nas-missing-{nonce}"))
            .join("not-created");
        assert!(!root.exists());
        assert!(prepare_nas_destination(&root, &"a".repeat(64)).is_err());
        assert!(!root.exists());
    }

    #[cfg(windows)]
    #[test]
    fn local_nas_rejects_reparse_ancestor_and_leaf_without_touching_target() {
        use std::os::windows::fs::symlink_dir;
        let nonce = SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("townlight-nas-reparse-{nonce}"));
        let source_root = root.join("source");
        let configured_root = root.join("configured");
        std::fs::create_dir_all(&source_root).unwrap();
        std::fs::write(root.join("media"), b"exact media").unwrap();
        if let Err(error) = symlink_dir(&source_root, &configured_root) {
            if error.raw_os_error() != Some(1314) {
                panic!("could not create junction fixture: {error}");
            }
            let status = std::process::Command::new("cmd.exe")
                .args(["/d", "/c", "mklink", "/J"])
                .arg(&configured_root)
                .arg(&source_root)
                .status()
                .unwrap();
            assert!(status.success());
        }
        let media = root.join("media");
        let digest = format!("{:x}", Sha256::digest(b"exact media"));
        let job = PublishExecutionJob {
            job_id: "33333333-2222-4333-8444-555555555559".into(),
            account_id: "11111111-2222-4333-8444-555555555555".into(),
            account_revision: 1,
            target_id: "22222222-2222-4333-8444-555555555555".into(),
            target_revision: 1,
            target_kind: PublishTargetKind::LocalNas,
            configuration_hash: "a".repeat(64),
            credential_version: 1,
            candidate_version: "0.1.0".into(),
            source_commit: "a".repeat(40),
            install_generation: 1,
            asset_id: digest.clone(),
            media_sha256: digest,
            media_bytes: 11,
            media_duration_ms: 1_000,
            requested_by: "setup".into(),
            created_at_unix_ms: 1,
        };
        assert!(deliver_to_nas(&job, &media, &configured_root).is_err());
        assert!(
            !source_root
                .join(format!("{}.mp4", job.media_sha256))
                .exists()
        );
        let _ = std::fs::remove_file(media);
        let _ = std::fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    #[test]
    fn local_nas_interruption_cleans_only_its_owned_temp() {
        let nonce = SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("townlight-nas-interrupt-{nonce}"));
        std::fs::create_dir_all(&root).unwrap();
        let media = root.join("media");
        std::fs::write(&media, b"exact media").unwrap();
        let digest = format!("{:x}", Sha256::digest(b"exact media"));
        let job = PublishExecutionJob {
            job_id: "33333333-2222-4333-8444-555555555560".into(),
            account_id: "11111111-2222-4333-8444-555555555555".into(),
            account_revision: 1,
            target_id: "22222222-2222-4333-8444-555555555555".into(),
            target_revision: 1,
            target_kind: PublishTargetKind::LocalNas,
            configuration_hash: "a".repeat(64),
            credential_version: 1,
            candidate_version: "0.1.0".into(),
            source_commit: "a".repeat(40),
            install_generation: 1,
            asset_id: digest.clone(),
            media_sha256: digest,
            media_bytes: 11,
            media_duration_ms: 1_000,
            requested_by: "setup".into(),
            created_at_unix_ms: 1,
        };
        let configured_root = root.clone();
        let mut calls = 0;
        let mut heartbeat = || {
            calls += 1;
            Err::<(), String>("cancelled".into())
        };
        assert!(
            deliver_to_nas_with_heartbeat(&job, &media, &configured_root, &mut heartbeat).is_err()
        );
        assert!(
            std::fs::read_dir(&root)
                .unwrap()
                .filter_map(Result::ok)
                .all(|entry| entry.file_name() == "media")
        );
        assert_eq!(
            deliver_to_nas(&job, &media, &configured_root)
                .unwrap()
                .stage,
            PublishExecutionStage::Delivered
        );
        assert_eq!(
            std::fs::read(root.join(format!("{}.mp4", job.media_sha256))).unwrap(),
            b"exact media"
        );
        let _ = std::fs::remove_file(media);
        let _ = std::fs::remove_file(root.join(format!("{}.mp4", job.media_sha256)));
        let _ = std::fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    #[test]
    fn local_nas_rejects_a_substituted_destination_leaf_without_overwrite() {
        use std::os::windows::fs::symlink_dir;
        let nonce = SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("townlight-nas-leaf-{nonce}"));
        std::fs::create_dir_all(&root).unwrap();
        let media = root.join("media");
        let victim = root.join("victim");
        std::fs::write(&media, b"exact media").unwrap();
        std::fs::create_dir(&victim).unwrap();
        let digest = format!("{:x}", Sha256::digest(b"exact media"));
        let destination = root.join(format!("{digest}.mp4"));
        if let Err(error) = symlink_dir(&victim, &destination) {
            if error.raw_os_error() != Some(1314) {
                panic!("could not create leaf reparse fixture: {error}");
            }
            let status = std::process::Command::new("cmd.exe")
                .args(["/d", "/c", "mklink", "/J"])
                .arg(&destination)
                .arg(&victim)
                .status()
                .unwrap();
            assert!(status.success());
        }
        let job = PublishExecutionJob {
            job_id: "33333333-2222-4333-8444-555555555561".into(),
            account_id: "11111111-2222-4333-8444-555555555555".into(),
            account_revision: 1,
            target_id: "22222222-2222-4333-8444-555555555555".into(),
            target_revision: 1,
            target_kind: PublishTargetKind::LocalNas,
            configuration_hash: "a".repeat(64),
            credential_version: 1,
            candidate_version: "0.1.0".into(),
            source_commit: "a".repeat(40),
            install_generation: 1,
            asset_id: digest.clone(),
            media_sha256: digest,
            media_bytes: 11,
            media_duration_ms: 1_000,
            requested_by: "setup".into(),
            created_at_unix_ms: 1,
        };
        let configured_root = root.clone();
        assert!(deliver_to_nas(&job, &media, &configured_root).is_err());
        assert!(victim.is_dir());
        let _ = std::fs::remove_file(media);
        let _ = std::fs::remove_file(destination);
        let _ = std::fs::remove_dir_all(victim);
        let _ = std::fs::remove_dir_all(root);
    }
}

fn publish_kind_name(kind: PublishTargetKind) -> &'static str {
    match kind {
        PublishTargetKind::Portal => "portal",
        PublishTargetKind::InternetArchive => "internet_archive",
        PublishTargetKind::LocalNas => "local_nas",
        PublishTargetKind::YouTubeLive => "youtube_live",
        PublishTargetKind::Syndication => "syndication",
        PublishTargetKind::Podcast => "podcast",
    }
}
