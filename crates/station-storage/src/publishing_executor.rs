use super::*;
use station_publishing::{
    PublishCandidateIdentity, PublishExecutionReceipt, PublishExecutionStage,
    PublishExecutionTargetConfig, PublishTargetKind, PublishTargetProfile,
    validate_receipt_successor,
};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use std::time::Duration;

mod youtube_live_runtime;

const MAX_PUBLISH_ATTEMPTS: u32 = 8;
const MAX_PUBLISH_LEASE_MS: i64 = 900_000;
const MAX_PROVIDER_RESPONSE_BYTES: usize = 1_048_576;

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum PublishExecutionJobState {
    Pending,
    Running,
    Completed,
    Failed,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
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

#[derive(Debug, Clone)]
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
               asset_id TEXT NOT NULL, media_sha256 TEXT NOT NULL, media_bytes INTEGER NOT NULL, requested_by TEXT NOT NULL,\
               created_at_unix_ms INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','running','completed','failed')),\
               attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at_unix_ms INTEGER NOT NULL, updated_at_unix_ms INTEGER NOT NULL,\
               lease_token TEXT, lease_expires_at_unix_ms INTEGER, completed_at_unix_ms INTEGER, failure_class TEXT);\
             CREATE INDEX IF NOT EXISTS publish_execution_jobs_claim ON publish_execution_jobs(state,next_attempt_at_unix_ms,created_at_unix_ms);\
             CREATE TABLE IF NOT EXISTS publish_execution_receipts (\
               receipt_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, stage TEXT NOT NULL, recorded_at_unix_ms INTEGER NOT NULL, record_json TEXT NOT NULL,\
               FOREIGN KEY(job_id) REFERENCES publish_execution_jobs(job_id));\
             CREATE INDEX IF NOT EXISTS publish_execution_receipts_job ON publish_execution_receipts(job_id,recorded_at_unix_ms,receipt_id);\
             CREATE TRIGGER IF NOT EXISTS publish_execution_configs_no_update BEFORE UPDATE ON publish_execution_target_configs BEGIN SELECT RAISE(ABORT,'publish execution configuration is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS publish_execution_configs_no_delete BEFORE DELETE ON publish_execution_target_configs BEGIN SELECT RAISE(ABORT,'publish execution configuration is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS publish_execution_receipts_no_update BEFORE UPDATE ON publish_execution_receipts BEGIN SELECT RAISE(ABORT,'publish execution receipt is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS publish_execution_receipts_no_delete BEFORE DELETE ON publish_execution_receipts BEGIN SELECT RAISE(ABORT,'publish execution receipt is append-only'); END;",
        )
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
            let statement = self.prepare("INSERT INTO publish_execution_jobs(job_id,account_id,account_revision,target_id,target_revision,target_kind,configuration_hash,credential_version,candidate_version,source_commit,install_generation,asset_id,media_sha256,media_bytes,requested_by,created_at_unix_ms,state,next_attempt_at_unix_ms,updated_at_unix_ms) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,'pending',?16,?16)")?;
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
                (&job.requested_by, 15, "publish actor"),
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
            ] {
                bind_i64(
                    self.database,
                    statement.raw,
                    index,
                    checked_i64(value, field)?,
                )?;
            }
            bind_i64(self.database, statement.raw, 16, job.created_at_unix_ms)?;
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
            let recover = self.prepare("UPDATE publish_execution_jobs SET state='pending',lease_token=NULL,lease_expires_at_unix_ms=NULL,next_attempt_at_unix_ms=?1,updated_at_unix_ms=?1,failure_class='lease_expired' WHERE state='running' AND lease_expires_at_unix_ms<=?1")?;
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
        let retry = transient && lease.record.attempts < MAX_PUBLISH_ATTEMPTS;
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
    now_unix_ms: i64,
    current_install: &mut dyn FnMut() -> Result<(String, String, u64), String>,
    clock: &mut dyn FnMut() -> Result<i64, String>,
) -> Result<bool, String> {
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
                occurred_at_unix_ms: now_unix_ms,
                recorded_at_unix_ms: now_unix_ms,
                detail: evidence.detail,
            })
            .collect::<Vec<_>>();
        store.complete_publish_execution_job(
            lease
                .as_ref()
                .expect("publish lease is retained until completion"),
            &receipts,
            now_unix_ms,
        )?;
        lease.take();
        Ok(())
    })();
    match execution {
        Ok(()) => Ok(true),
        Err(error) => {
            if let Some(lease) = lease.take() {
                store.fail_publish_execution_job(
                    lease,
                    classify_publish_failure(&error),
                    true,
                    now_unix_ms,
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
            PublishExecutionTargetConfig::LocalNas { managed_root } => {
                deliver_to_nas(job, media_path, Path::new(managed_root))?
            }
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

fn deliver_to_nas(
    job: &PublishExecutionJob,
    media_path: &Path,
    root: &Path,
) -> Result<ProviderStageEvidence, String> {
    reject_reparse_ancestry(root)?;
    let final_path = root.join(format!("{}.mp4", job.media_sha256));
    let partial = root.join(format!(".{}.{}.part", job.media_sha256, job.job_id));
    if final_path.exists() || partial.exists() {
        return Err("managed NAS destination already exists".into());
    }
    let mut source = File::open(media_path).map_err(|_| "publish media could not be opened")?;
    let mut destination = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&partial)
        .map_err(|_| "managed NAS partial could not be created")?;
    std::io::copy(&mut source, &mut destination).map_err(|_| "managed NAS copy failed")?;
    destination
        .flush()
        .map_err(|_| "managed NAS copy could not be flushed")?;
    destination
        .sync_all()
        .map_err(|_| "managed NAS copy could not be synchronized")?;
    drop(destination);
    let (bytes, digest) = hash_publish_media(&partial)?;
    if bytes != job.media_bytes || digest != job.media_sha256 {
        let _ = fs::remove_file(&partial);
        return Err("managed NAS copy did not match the exact media binding".into());
    }
    fs::rename(&partial, &final_path).map_err(|_| "managed NAS copy could not be finalized")?;
    Ok(ProviderStageEvidence {
        stage: PublishExecutionStage::Delivered,
        digest,
        detail: "Exact media bytes were atomically finalized and rehashed at the managed NAS destination.".into(),
    })
}

fn reject_reparse_ancestry(path: &Path) -> Result<(), String> {
    if !path.is_absolute() {
        return Err("managed NAS root is not absolute".into());
    }
    let mut current = PathBuf::new();
    for component in path.components() {
        current.push(component.as_os_str());
        if current.exists()
            && fs::symlink_metadata(&current)
                .map_err(|_| "managed NAS ancestry could not be inspected")?
                .file_type()
                .is_symlink()
        {
            return Err("managed NAS ancestry contains a reparse point".into());
        }
    }
    if !path.is_dir() {
        return Err("managed NAS root is unavailable".into());
    }
    Ok(())
}

fn hash_publish_media(path: &Path) -> Result<(u64, String), String> {
    let mut file = File::open(path).map_err(|_| "publish media could not be opened")?;
    let mut hasher = Sha256::new();
    let mut bytes = 0_u64;
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let read = file
            .read(&mut buffer)
            .map_err(|_| "publish media could not be read")?;
        if read == 0 {
            break;
        }
        bytes = bytes
            .checked_add(read as u64)
            .ok_or("publish media size overflowed")?;
        hasher.update(&buffer[..read]);
    }
    Ok((bytes, format!("{:x}", hasher.finalize())))
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
    "SELECT job_id,account_id,account_revision,target_id,target_revision,target_kind,configuration_hash,credential_version,candidate_version,source_commit,install_generation,asset_id,media_sha256,media_bytes,requested_by,created_at_unix_ms,state,attempts,next_attempt_at_unix_ms,updated_at_unix_ms,COALESCE(lease_expires_at_unix_ms,-1),COALESCE(completed_at_unix_ms,-1),COALESCE(failure_class,'') FROM publish_execution_jobs"
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
        requested_by: column_text(statement, 14)?,
        created_at_unix_ms: unsafe { sqlite3_column_int64(statement, 15) },
    };
    job.validate()?;
    let state = match column_text(statement, 16)?.as_str() {
        "pending" => PublishExecutionJobState::Pending,
        "running" => PublishExecutionJobState::Running,
        "completed" => PublishExecutionJobState::Completed,
        "failed" => PublishExecutionJobState::Failed,
        _ => return Err("publish job state is corrupt".into()),
    };
    Ok(PublishExecutionJobRecord {
        job,
        state,
        attempts: u32::try_from(column_u64(statement, 17, "publish attempts")?)
            .map_err(|_| "publish attempts overflowed")?,
        next_attempt_at_unix_ms: unsafe { sqlite3_column_int64(statement, 18) },
        updated_at_unix_ms: unsafe { sqlite3_column_int64(statement, 19) },
        lease_expires_at_unix_ms: optional_i64(statement, 20),
        completed_at_unix_ms: optional_i64(statement, 21),
        failure_class: optional_column_text(statement, 22)?,
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
            PublishExecutionJobState::Pending
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
        drop(store);
        let _ = std::fs::remove_file(path);
        let _ = std::fs::remove_file(media_path);
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
