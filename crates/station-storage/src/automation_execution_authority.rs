use super::*;
use station_automation_policy::{AutomationExecutionReceipt, AutomationExecutionRequest};
use station_maintenance::VerifiedInstallAuthority;

const MAX_AUTOMATION_EXECUTION_ATTEMPTS: u32 = 10;
const MAX_AUTOMATION_EXECUTION_LEASE_MS: i64 = 900_000;
// Retry timing is durable authority, not an in-memory controller preference.  In
// particular, an installed station with no registered production adapter must
// not consume all ten attempts in one tight service loop.
const AUTOMATION_EXECUTION_RETRY_INITIAL_MS: i64 = 60_000;
const AUTOMATION_EXECUTION_RETRY_MAX_MS: i64 = 3_600_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AutomationExecutionJobState {
    Pending,
    Claimed,
    Completed,
    Failed,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AutomationExecutionJobRecord {
    pub request: AutomationExecutionRequest,
    pub state: AutomationExecutionJobState,
    pub attempt_count: u32,
    pub lease_owner: Option<String>,
    pub lease_expires_at_unix_ms: Option<i64>,
    pub claim_id: Option<String>,
    pub claimed_at_unix_ms: Option<i64>,
    pub receipt: Option<AutomationExecutionReceipt>,
    pub last_failure_code: Option<String>,
    pub created_at_unix_ms: i64,
    pub next_attempt_at_unix_ms: i64,
    pub completed_at_unix_ms: Option<i64>,
}

impl StationStore {
    pub(super) fn initialize_automation_execution_schema(&self) -> Result<(), String> {
        let expected_objects = [
            ("table", "automation_execution_jobs"),
            ("index", "automation_execution_jobs_pending"),
            ("table", "automation_execution_receipts"),
            ("trigger", "automation_execution_receipts_no_update"),
            ("trigger", "automation_execution_receipts_no_delete"),
            ("trigger", "automation_execution_jobs_binding_immutable"),
            ("trigger", "automation_execution_jobs_terminal_immutable"),
            ("trigger", "automation_execution_jobs_no_delete"),
        ];
        let mut existing_objects = 0_usize;
        for (kind, name) in expected_objects {
            existing_objects += usize::from(self.sqlite_schema_object_sql(kind, name)?.is_some());
        }
        if existing_objects != 0 {
            self.migrate_automation_execution_schema_v1_to_v2()?;
            return self.attest_automation_execution_schema_v2();
        }
        self.execute(
            "CREATE TABLE IF NOT EXISTS automation_execution_jobs (\
               job_id TEXT PRIMARY KEY,\
               feature TEXT NOT NULL CHECK (feature IN ('captions','summaries','translation')),\
               policy_id TEXT NOT NULL,\
               policy_revision INTEGER NOT NULL CHECK (policy_revision >= 1),\
               adapter_id TEXT NOT NULL,\
               adapter_revision INTEGER NOT NULL CHECK (adapter_revision >= 1),\
               configuration_hash TEXT NOT NULL CHECK (length(configuration_hash)=64),\
               candidate_version TEXT NOT NULL,\
               source_commit TEXT NOT NULL CHECK (length(source_commit)=40),\
               install_generation INTEGER NOT NULL CHECK (install_generation >= 1),\
               input_sha256 TEXT NOT NULL CHECK (length(input_sha256)=64),\
               input_bytes INTEGER NOT NULL CHECK (input_bytes >= 1),\
               state TEXT NOT NULL CHECK (state IN ('pending','claimed','completed','failed')),\
               attempt_count INTEGER NOT NULL CHECK (attempt_count BETWEEN 0 AND 10),\
               lease_owner TEXT NOT NULL,\
               lease_expires_at_unix_ms INTEGER NOT NULL CHECK (lease_expires_at_unix_ms >= -1),\
               claim_id TEXT NOT NULL,\
               claimed_at_unix_ms INTEGER NOT NULL CHECK (claimed_at_unix_ms >= -1),\
               receipt_id TEXT NOT NULL,\
               last_failure_code TEXT NOT NULL,\
               created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               completed_at_unix_ms INTEGER NOT NULL CHECK (completed_at_unix_ms >= -1),\
               request_json TEXT NOT NULL,\
               next_attempt_at_unix_ms INTEGER NOT NULL DEFAULT 0 CHECK (next_attempt_at_unix_ms >= 0),\
               FOREIGN KEY(policy_id) REFERENCES automation_feature_policies(policy_id),\
               FOREIGN KEY(adapter_id) REFERENCES automation_adapter_descriptors(adapter_id)\
             );\
             CREATE INDEX IF NOT EXISTS automation_execution_jobs_pending \
               ON automation_execution_jobs(state,next_attempt_at_unix_ms,created_at_unix_ms,job_id);\
             CREATE TABLE IF NOT EXISTS automation_execution_receipts (\
               receipt_id TEXT PRIMARY KEY,\
               job_id TEXT NOT NULL UNIQUE,\
               install_generation INTEGER NOT NULL CHECK (install_generation >= 1),\
               claim_id TEXT NOT NULL,\
               output_sha256 TEXT NOT NULL CHECK (length(output_sha256)=64),\
               output_bytes INTEGER NOT NULL CHECK (output_bytes >= 1),\
               completed_at_unix_ms INTEGER NOT NULL CHECK (completed_at_unix_ms >= 0),\
               record_json TEXT NOT NULL,\
               FOREIGN KEY(job_id) REFERENCES automation_execution_jobs(job_id)\
             );\
             CREATE TRIGGER IF NOT EXISTS automation_execution_receipts_no_update \
               BEFORE UPDATE ON automation_execution_receipts \
               BEGIN SELECT RAISE(ABORT, 'automation execution receipts are append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS automation_execution_receipts_no_delete \
               BEFORE DELETE ON automation_execution_receipts \
               BEGIN SELECT RAISE(ABORT, 'automation execution receipts are append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS automation_execution_jobs_binding_immutable \
               BEFORE UPDATE OF job_id,feature,policy_id,policy_revision,adapter_id,adapter_revision,configuration_hash,candidate_version,source_commit,install_generation,input_sha256,input_bytes,created_at_unix_ms,request_json \
               ON automation_execution_jobs \
               BEGIN SELECT RAISE(ABORT, 'automation execution job binding is immutable'); END;\
             CREATE TRIGGER IF NOT EXISTS automation_execution_jobs_terminal_immutable \
               BEFORE UPDATE ON automation_execution_jobs \
               WHEN OLD.state IN ('completed','failed') \
               BEGIN SELECT RAISE(ABORT, 'terminal automation execution jobs are immutable'); END;\
             CREATE TRIGGER IF NOT EXISTS automation_execution_jobs_no_delete \
               BEFORE DELETE ON automation_execution_jobs \
               BEGIN SELECT RAISE(ABORT, 'automation execution jobs are durable history'); END;",
        )?;
        self.attest_automation_execution_schema_v2()
    }

    fn migrate_automation_execution_schema_v1_to_v2(&self) -> Result<(), String> {
        let table = self
            .sqlite_schema_object_sql("table", "automation_execution_jobs")?
            .ok_or_else(|| "automation execution schema is missing its jobs table".to_string())?;
        if !table
            .to_ascii_lowercase()
            .contains("next_attempt_at_unix_ms")
        {
            self.execute(
                "ALTER TABLE automation_execution_jobs ADD COLUMN next_attempt_at_unix_ms INTEGER NOT NULL DEFAULT 0 CHECK (next_attempt_at_unix_ms >= 0)",
            )?;
            // v1 had no retry clock: a pending row was eligible no earlier
            // than its creation time.  Preserve that authority explicitly;
            // the SQL default only exists for the ALTER TABLE operation and
            // must not make historical work claimable before it was created.
            self.execute(
                "UPDATE automation_execution_jobs SET next_attempt_at_unix_ms=created_at_unix_ms WHERE state='pending'",
            )?;
            self.execute("DROP INDEX automation_execution_jobs_pending")?;
            self.execute(
                "CREATE INDEX automation_execution_jobs_pending ON automation_execution_jobs(state,next_attempt_at_unix_ms,created_at_unix_ms,job_id)",
            )?;
        }
        Ok(())
    }

    fn attest_automation_execution_schema_v2(&self) -> Result<(), String> {
        let objects = [
            (
                "table",
                "automation_execution_jobs",
                "CREATE TABLE automation_execution_jobs (
                   job_id TEXT PRIMARY KEY,
                   feature TEXT NOT NULL CHECK (feature IN ('captions','summaries','translation')),
                   policy_id TEXT NOT NULL,
                   policy_revision INTEGER NOT NULL CHECK (policy_revision >= 1),
                   adapter_id TEXT NOT NULL,
                   adapter_revision INTEGER NOT NULL CHECK (adapter_revision >= 1),
                   configuration_hash TEXT NOT NULL CHECK (length(configuration_hash)=64),
                   candidate_version TEXT NOT NULL,
                   source_commit TEXT NOT NULL CHECK (length(source_commit)=40),
                   install_generation INTEGER NOT NULL CHECK (install_generation >= 1),
                   input_sha256 TEXT NOT NULL CHECK (length(input_sha256)=64),
                   input_bytes INTEGER NOT NULL CHECK (input_bytes >= 1),
                   state TEXT NOT NULL CHECK (state IN ('pending','claimed','completed','failed')),
                   attempt_count INTEGER NOT NULL CHECK (attempt_count BETWEEN 0 AND 10),
                   lease_owner TEXT NOT NULL,
                   lease_expires_at_unix_ms INTEGER NOT NULL CHECK (lease_expires_at_unix_ms >= -1),
                   claim_id TEXT NOT NULL,
                   claimed_at_unix_ms INTEGER NOT NULL CHECK (claimed_at_unix_ms >= -1),
                   receipt_id TEXT NOT NULL,
                   last_failure_code TEXT NOT NULL,
                   created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),
                   completed_at_unix_ms INTEGER NOT NULL CHECK (completed_at_unix_ms >= -1),
                   request_json TEXT NOT NULL,
                   next_attempt_at_unix_ms INTEGER NOT NULL DEFAULT 0 CHECK (next_attempt_at_unix_ms >= 0),
                   FOREIGN KEY(policy_id) REFERENCES automation_feature_policies(policy_id),
                   FOREIGN KEY(adapter_id) REFERENCES automation_adapter_descriptors(adapter_id)
                 )",
            ),
            (
                "index",
                "automation_execution_jobs_pending",
                "CREATE INDEX automation_execution_jobs_pending ON automation_execution_jobs(state,next_attempt_at_unix_ms,created_at_unix_ms,job_id)",
            ),
            (
                "table",
                "automation_execution_receipts",
                "CREATE TABLE automation_execution_receipts (
                   receipt_id TEXT PRIMARY KEY,
                   job_id TEXT NOT NULL UNIQUE,
                   install_generation INTEGER NOT NULL CHECK (install_generation >= 1),
                   claim_id TEXT NOT NULL,
                   output_sha256 TEXT NOT NULL CHECK (length(output_sha256)=64),
                   output_bytes INTEGER NOT NULL CHECK (output_bytes >= 1),
                   completed_at_unix_ms INTEGER NOT NULL CHECK (completed_at_unix_ms >= 0),
                   record_json TEXT NOT NULL,
                   FOREIGN KEY(job_id) REFERENCES automation_execution_jobs(job_id)
                 )",
            ),
            (
                "trigger",
                "automation_execution_receipts_no_update",
                "CREATE TRIGGER automation_execution_receipts_no_update BEFORE UPDATE ON automation_execution_receipts BEGIN SELECT RAISE(ABORT, 'automation execution receipts are append-only'); END",
            ),
            (
                "trigger",
                "automation_execution_receipts_no_delete",
                "CREATE TRIGGER automation_execution_receipts_no_delete BEFORE DELETE ON automation_execution_receipts BEGIN SELECT RAISE(ABORT, 'automation execution receipts are append-only'); END",
            ),
            (
                "trigger",
                "automation_execution_jobs_binding_immutable",
                "CREATE TRIGGER automation_execution_jobs_binding_immutable BEFORE UPDATE OF job_id,feature,policy_id,policy_revision,adapter_id,adapter_revision,configuration_hash,candidate_version,source_commit,install_generation,input_sha256,input_bytes,created_at_unix_ms,request_json ON automation_execution_jobs BEGIN SELECT RAISE(ABORT, 'automation execution job binding is immutable'); END",
            ),
            (
                "trigger",
                "automation_execution_jobs_terminal_immutable",
                "CREATE TRIGGER automation_execution_jobs_terminal_immutable BEFORE UPDATE ON automation_execution_jobs WHEN OLD.state IN ('completed','failed') BEGIN SELECT RAISE(ABORT, 'terminal automation execution jobs are immutable'); END",
            ),
            (
                "trigger",
                "automation_execution_jobs_no_delete",
                "CREATE TRIGGER automation_execution_jobs_no_delete BEFORE DELETE ON automation_execution_jobs BEGIN SELECT RAISE(ABORT, 'automation execution jobs are durable history'); END",
            ),
        ];
        for (kind, name, expected) in objects {
            let actual = self
                .sqlite_schema_object_sql(kind, name)?
                .ok_or_else(|| format!("automation execution schema is missing {kind} {name}"))?;
            if canonical_automation_schema_sql(&actual) != canonical_automation_schema_sql(expected)
            {
                return Err(format!(
                    "automation execution schema v1 {kind} {name} is not canonical"
                ));
            }
        }
        Ok(())
    }

    pub fn enqueue_automation_execution_job(
        &self,
        request: &AutomationExecutionRequest,
        install_authority: &VerifiedInstallAuthority,
        now_unix_ms: i64,
    ) -> Result<AutomationExecutionJobRecord, String> {
        request.validate_shape()?;
        validate_execution_install_authority(request, install_authority)?;
        if now_unix_ms < 0 || request.requested_at_unix_ms != now_unix_ms {
            return Err("automation execution request time is not server-current".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            if let Some(existing) = self.read_automation_execution_job(&request.job_id)? {
                if existing.request == *request {
                    return Ok(existing);
                }
                return Err("automation execution job identity is already bound".into());
            }
            self.validate_automation_execution_request_current(
                request,
                install_authority,
                now_unix_ms,
            )?;
            let request_json = serde_json::to_string(request)
                .map_err(|error| format!("serialize automation execution request: {error}"))?;
            let statement = self.prepare(
                "INSERT INTO automation_execution_jobs(job_id,feature,policy_id,policy_revision,adapter_id,adapter_revision,configuration_hash,candidate_version,source_commit,install_generation,input_sha256,input_bytes,state,attempt_count,lease_owner,lease_expires_at_unix_ms,claim_id,claimed_at_unix_ms,receipt_id,last_failure_code,created_at_unix_ms,completed_at_unix_ms,request_json,next_attempt_at_unix_ms) \
                 VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,'pending',0,'',-1,'',-1,'','',?13,-1,?14,?15)",
            )?;
            let values = [
                c_string(&request.job_id, "automation execution job identity")?,
                c_string(
                    automation_feature_name(request.feature),
                    "automation execution feature",
                )?,
                c_string(&request.policy_id, "automation execution policy identity")?,
                c_string(&request.adapter_id, "automation execution adapter identity")?,
                c_string(
                    &request.configuration_hash,
                    "automation execution configuration hash",
                )?,
                c_string(
                    &request.candidate.version,
                    "automation execution candidate version",
                )?,
                c_string(
                    &request.candidate.source_commit,
                    "automation execution source commit",
                )?,
                c_string(
                    &request.input.artifact_sha256,
                    "automation execution input digest",
                )?,
                c_string(&request_json, "automation execution request record")?,
            ];
            bind_text(self.database, statement.raw, 1, &values[0])?;
            bind_text(self.database, statement.raw, 2, &values[1])?;
            bind_text(self.database, statement.raw, 3, &values[2])?;
            bind_i64(
                self.database,
                statement.raw,
                4,
                checked_i64(
                    request.policy_revision,
                    "automation execution policy revision",
                )?,
            )?;
            bind_text(self.database, statement.raw, 5, &values[3])?;
            bind_i64(
                self.database,
                statement.raw,
                6,
                checked_i64(
                    request.adapter_revision,
                    "automation execution adapter revision",
                )?,
            )?;
            bind_text(self.database, statement.raw, 7, &values[4])?;
            bind_text(self.database, statement.raw, 8, &values[5])?;
            bind_text(self.database, statement.raw, 9, &values[6])?;
            bind_i64(
                self.database,
                statement.raw,
                10,
                checked_i64(
                    request.install_generation,
                    "automation execution install generation",
                )?,
            )?;
            bind_text(self.database, statement.raw, 11, &values[7])?;
            bind_i64(
                self.database,
                statement.raw,
                12,
                checked_i64(request.input.bytes, "automation execution input bytes")?,
            )?;
            bind_i64(self.database, statement.raw, 13, now_unix_ms)?;
            bind_text(self.database, statement.raw, 14, &values[8])?;
            bind_i64(self.database, statement.raw, 15, now_unix_ms)?;
            expect_done(self.database, statement.raw)?;
            self.read_automation_execution_job(&request.job_id)?
                .ok_or_else(|| "automation execution job disappeared after enqueue".into())
        })();
        self.finish_automation_execution_transaction(outcome)
    }

    pub fn read_automation_execution_job(
        &self,
        job_id: &str,
    ) -> Result<Option<AutomationExecutionJobRecord>, String> {
        if !is_canonical_uuid(job_id) {
            return Err("automation execution job identity is invalid".into());
        }
        let statement = self.prepare(
            "SELECT job_id,feature,policy_id,policy_revision,adapter_id,adapter_revision,configuration_hash,candidate_version,source_commit,install_generation,input_sha256,input_bytes,state,attempt_count,lease_owner,lease_expires_at_unix_ms,claim_id,claimed_at_unix_ms,receipt_id,last_failure_code,created_at_unix_ms,completed_at_unix_ms,request_json,next_attempt_at_unix_ms \
             FROM automation_execution_jobs WHERE job_id=?1",
        )?;
        let identity = c_string(job_id, "automation execution job identity")?;
        bind_text(self.database, statement.raw, 1, &identity)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(self.read_automation_execution_job_row(statement.raw)?)),
            SQLITE_DONE => {
                if self.automation_execution_receipt_exists_for_job(job_id)? {
                    Err("automation execution receipt has no durable parent job".into())
                } else {
                    Ok(None)
                }
            }
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn claim_automation_execution_job(
        &self,
        job_id: &str,
        worker_id: &str,
        install_authority: &VerifiedInstallAuthority,
        now_unix_ms: i64,
        lease_ms: i64,
    ) -> Result<AutomationExecutionJobRecord, String> {
        if !is_canonical_uuid(job_id)
            || !secret_field(worker_id, 200)
            || now_unix_ms < 0
            || !(1..=MAX_AUTOMATION_EXECUTION_LEASE_MS).contains(&lease_ms)
        {
            return Err("automation execution claim is invalid or unbounded".into());
        }
        validate_verified_install_authority(install_authority)?;
        let lease_expires = now_unix_ms
            .checked_add(lease_ms)
            .ok_or_else(|| "automation execution lease overflowed".to_string())?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = self.claim_automation_execution_job_in_transaction(
            job_id,
            worker_id,
            install_authority,
            now_unix_ms,
            lease_expires,
        );
        self.finish_automation_execution_transaction(outcome)
    }

    pub fn claim_next_automation_execution_job(
        &self,
        worker_id: &str,
        install_authority: &VerifiedInstallAuthority,
        now_unix_ms: i64,
        lease_ms: i64,
    ) -> Result<Option<AutomationExecutionJobRecord>, String> {
        if !secret_field(worker_id, 200)
            || now_unix_ms < 0
            || !(1..=MAX_AUTOMATION_EXECUTION_LEASE_MS).contains(&lease_ms)
        {
            return Err("automation execution next claim is invalid or unbounded".into());
        }
        validate_verified_install_authority(install_authority)?;
        let lease_expires = now_unix_ms
            .checked_add(lease_ms)
            .ok_or_else(|| "automation execution lease overflowed".to_string())?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let exhausted = self.prepare(
                "UPDATE automation_execution_jobs SET state='failed',lease_owner='',lease_expires_at_unix_ms=-1,last_failure_code='attempt_limit_exhausted',completed_at_unix_ms=?1 \
                 WHERE state='claimed' AND attempt_count>=10 AND lease_expires_at_unix_ms<=?1",
            )?;
            bind_i64(self.database, exhausted.raw, 1, now_unix_ms)?;
            expect_done(self.database, exhausted.raw)?;
            for _ in 0..256 {
                let select = self.prepare(
                    "SELECT job_id FROM automation_execution_jobs \
                     WHERE attempt_count<10 AND ((state='pending' AND next_attempt_at_unix_ms<=?1) OR (state='claimed' AND lease_expires_at_unix_ms<=?1)) \
                     ORDER BY created_at_unix_ms,job_id LIMIT 1",
                )?;
                bind_i64(self.database, select.raw, 1, now_unix_ms)?;
                let job_id = match unsafe { sqlite3_step(select.raw) } {
                    SQLITE_ROW => column_text(select.raw, 0)?,
                    SQLITE_DONE => return Ok(None),
                    _ => return Err(sqlite_error(self.database)),
                };
                drop(select);
                let current = self
                    .read_automation_execution_job(&job_id)?
                    .ok_or_else(|| "selected automation execution job disappeared".to_string())?;
                if self.automation_execution_request_is_current(
                    &current.request,
                    install_authority,
                    now_unix_ms,
                )? {
                    return self
                        .claim_automation_execution_job_in_transaction(
                            &job_id,
                            worker_id,
                            install_authority,
                            now_unix_ms,
                            lease_expires,
                        )
                        .map(Some);
                }
                let terminal = self.prepare(
                    "UPDATE automation_execution_jobs SET state='failed',lease_owner='',lease_expires_at_unix_ms=-1,last_failure_code='authority_changed',completed_at_unix_ms=?1 \
                     WHERE job_id=?2 AND ((state='pending' AND next_attempt_at_unix_ms<=?1) OR (state='claimed' AND lease_expires_at_unix_ms<=?1))",
                )?;
                bind_i64(self.database, terminal.raw, 1, now_unix_ms)?;
                bind_text(
                    self.database,
                    terminal.raw,
                    2,
                    &c_string(&job_id, "automation execution job identity")?,
                )?;
                expect_done(self.database, terminal.raw)?;
                if self.changes() != 1 {
                    return Err(
                        "stale automation execution terminalization lost its write lock".into(),
                    );
                }
            }
            Ok(None)
        })();
        self.finish_automation_execution_transaction(outcome)
    }

    pub fn complete_automation_execution_job(
        &self,
        job_id: &str,
        worker_id: &str,
        receipt: &AutomationExecutionReceipt,
        install_authority: &VerifiedInstallAuthority,
        now_unix_ms: i64,
    ) -> Result<AutomationExecutionJobRecord, String> {
        if !is_canonical_uuid(job_id) || !secret_field(worker_id, 200) || now_unix_ms < 0 {
            return Err("automation execution completion authority is invalid".into());
        }
        validate_verified_install_authority(install_authority)?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let current = self
                .read_automation_execution_job(job_id)?
                .ok_or_else(|| "automation execution job does not exist".to_string())?;
            if current.state != AutomationExecutionJobState::Claimed
                || current.lease_owner.as_deref() != Some(worker_id)
                || current
                    .lease_expires_at_unix_ms
                    .is_none_or(|expiry| expiry <= now_unix_ms)
                || receipt.completed_at_unix_ms > now_unix_ms
            {
                return Err("automation execution completion lacks a live exact lease".into());
            }
            let (policy, adapter) =
                self.current_automation_execution_authority(&current.request)?;
            self.validate_automation_execution_request_current(
                &current.request,
                install_authority,
                now_unix_ms,
            )?;
            let current_candidate = automation_candidate_from_install(install_authority);
            receipt.validate_against(&current.request, &policy, &adapter, &current_candidate)?;
            if current.claim_id.as_deref() != Some(receipt.claim_id.as_str())
                || current
                    .claimed_at_unix_ms
                    .is_none_or(|claimed_at| receipt.started_at_unix_ms < claimed_at)
            {
                return Err(
                    "automation execution receipt predates or mismatches its live claim".into(),
                );
            }
            let record_json = serde_json::to_string(receipt)
                .map_err(|error| format!("serialize automation execution receipt: {error}"))?;
            let insert = self.prepare(
                "INSERT INTO automation_execution_receipts(receipt_id,job_id,install_generation,claim_id,output_sha256,output_bytes,completed_at_unix_ms,record_json) VALUES(?1,?2,?3,?4,?5,?6,?7,?8)",
            )?;
            let values = [
                c_string(&receipt.receipt_id, "automation execution receipt identity")?,
                c_string(job_id, "automation execution job identity")?,
                c_string(&receipt.claim_id, "automation execution claim identity")?,
                c_string(
                    &receipt.output.artifact_sha256,
                    "automation execution output digest",
                )?,
                c_string(&record_json, "automation execution receipt record")?,
            ];
            bind_text(self.database, insert.raw, 1, &values[0])?;
            bind_text(self.database, insert.raw, 2, &values[1])?;
            bind_i64(
                self.database,
                insert.raw,
                3,
                checked_i64(
                    receipt.install_generation,
                    "automation execution receipt install generation",
                )?,
            )?;
            bind_text(self.database, insert.raw, 4, &values[2])?;
            bind_text(self.database, insert.raw, 5, &values[3])?;
            bind_i64(
                self.database,
                insert.raw,
                6,
                checked_i64(receipt.output.bytes, "automation execution output bytes")?,
            )?;
            bind_i64(self.database, insert.raw, 7, receipt.completed_at_unix_ms)?;
            bind_text(self.database, insert.raw, 8, &values[4])?;
            expect_done(self.database, insert.raw)?;

            // `worker_id` is stable across controller restarts.  The claim id is
            // the per-lease capability: without it, a worker that lost C1 could
            // complete a job reclaimed as C2 under the same worker identity.
            let update = self.prepare(
                "UPDATE automation_execution_jobs SET state='completed',lease_owner='',lease_expires_at_unix_ms=-1,receipt_id=?1,completed_at_unix_ms=?2 \
                 WHERE job_id=?3 AND state='claimed' AND lease_owner=?4 AND claim_id=?5 AND lease_expires_at_unix_ms>?6",
            )?;
            bind_text(self.database, update.raw, 1, &values[0])?;
            bind_i64(self.database, update.raw, 2, now_unix_ms)?;
            bind_text(self.database, update.raw, 3, &values[1])?;
            let worker = c_string(worker_id, "automation execution worker identity")?;
            bind_text(self.database, update.raw, 4, &worker)?;
            bind_text(self.database, update.raw, 5, &values[2])?;
            bind_i64(self.database, update.raw, 6, now_unix_ms)?;
            expect_done(self.database, update.raw)?;
            let completed = self.read_automation_execution_job(job_id)?.ok_or_else(|| {
                "automation execution job disappeared after completion".to_string()
            })?;
            if completed.state != AutomationExecutionJobState::Completed
                || completed.receipt.as_ref() != Some(receipt)
            {
                return Err("automation execution completion was not durably exact".into());
            }
            Ok(completed)
        })();
        self.finish_automation_execution_transaction(outcome)
    }

    pub fn renew_automation_execution_job_lease(
        &self,
        job_id: &str,
        worker_id: &str,
        claim_id: &str,
        install_authority: &VerifiedInstallAuthority,
        now_unix_ms: i64,
        lease_ms: i64,
    ) -> Result<AutomationExecutionJobRecord, String> {
        if !is_canonical_uuid(job_id)
            || !secret_field(worker_id, 200)
            || !is_canonical_uuid(claim_id)
            || now_unix_ms < 0
            || !(1..=MAX_AUTOMATION_EXECUTION_LEASE_MS).contains(&lease_ms)
        {
            return Err("automation execution lease renewal is invalid or unbounded".into());
        }
        validate_verified_install_authority(install_authority)?;
        let lease_expires = now_unix_ms
            .checked_add(lease_ms)
            .ok_or_else(|| "automation execution lease overflowed".to_string())?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let current = self
                .read_automation_execution_job(job_id)?
                .ok_or_else(|| "automation execution job does not exist".to_string())?;
            if current.state != AutomationExecutionJobState::Claimed
                || current.lease_owner.as_deref() != Some(worker_id)
                || current.claim_id.as_deref() != Some(claim_id)
                || current
                    .lease_expires_at_unix_ms
                    .is_none_or(|expiry| expiry <= now_unix_ms)
            {
                return Err("automation execution lease is not live for this worker".into());
            }
            self.validate_automation_execution_request_current(
                &current.request,
                install_authority,
                now_unix_ms,
            )?;
            let update = self.prepare(
                "UPDATE automation_execution_jobs SET lease_expires_at_unix_ms=?1 WHERE job_id=?2 AND state='claimed' AND lease_owner=?3 AND claim_id=?4 AND lease_expires_at_unix_ms>?5",
            )?;
            let job = c_string(job_id, "automation execution job identity")?;
            let worker = c_string(worker_id, "automation execution worker identity")?;
            let claim = c_string(claim_id, "automation execution claim identity")?;
            bind_i64(self.database, update.raw, 1, lease_expires)?;
            bind_text(self.database, update.raw, 2, &job)?;
            bind_text(self.database, update.raw, 3, &worker)?;
            bind_text(self.database, update.raw, 4, &claim)?;
            bind_i64(self.database, update.raw, 5, now_unix_ms)?;
            expect_done(self.database, update.raw)?;
            let renewed = self.read_automation_execution_job(job_id)?.ok_or_else(|| {
                "automation execution job disappeared after lease renewal".to_string()
            })?;
            if renewed.state != AutomationExecutionJobState::Claimed
                || renewed.lease_owner.as_deref() != Some(worker_id)
                || renewed.claim_id.as_deref() != Some(claim_id)
                || renewed.lease_expires_at_unix_ms != Some(lease_expires)
            {
                return Err("automation execution lease renewal was not exact".into());
            }
            Ok(renewed)
        })();
        self.finish_automation_execution_transaction(outcome)
    }

    pub fn record_automation_execution_attempt_failure(
        &self,
        job_id: &str,
        worker_id: &str,
        claim_id: &str,
        failure_code: &str,
        retryable: bool,
        now_unix_ms: i64,
    ) -> Result<AutomationExecutionJobRecord, String> {
        if !is_canonical_uuid(job_id)
            || !secret_field(worker_id, 200)
            || !is_canonical_uuid(claim_id)
            || !valid_automation_execution_failure_code(failure_code)
            || now_unix_ms < 0
        {
            return Err("automation execution failure report is invalid or unbounded".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let current = self
                .read_automation_execution_job(job_id)?
                .ok_or_else(|| "automation execution job does not exist".to_string())?;
            if current.state != AutomationExecutionJobState::Claimed
                || current.lease_owner.as_deref() != Some(worker_id)
                || current.claim_id.as_deref() != Some(claim_id)
                || current
                    .lease_expires_at_unix_ms
                    .is_none_or(|expiry| expiry <= now_unix_ms)
            {
                return Err("automation execution failure lacks a live exact lease".into());
            }
            let terminal = !retryable || current.attempt_count >= MAX_AUTOMATION_EXECUTION_ATTEMPTS;
            let update = if terminal {
                self.prepare(
                    "UPDATE automation_execution_jobs SET state='failed',lease_owner='',lease_expires_at_unix_ms=-1,last_failure_code=?1,completed_at_unix_ms=?2 \
                     WHERE job_id=?3 AND state='claimed' AND lease_owner=?4 AND claim_id=?5 AND lease_expires_at_unix_ms>?2",
                )?
            } else {
                self.prepare(
                    "UPDATE automation_execution_jobs SET state='pending',lease_owner='',lease_expires_at_unix_ms=-1,claim_id='',claimed_at_unix_ms=-1,last_failure_code=?1,next_attempt_at_unix_ms=?5 \
                     WHERE job_id=?3 AND state='claimed' AND lease_owner=?4 AND claim_id=?6 AND lease_expires_at_unix_ms>?2",
                )?
            };
            let code = c_string(failure_code, "automation execution failure code")?;
            let job = c_string(job_id, "automation execution job identity")?;
            let worker = c_string(worker_id, "automation execution worker identity")?;
            let claim = c_string(claim_id, "automation execution claim identity")?;
            bind_text(self.database, update.raw, 1, &code)?;
            bind_i64(self.database, update.raw, 2, now_unix_ms)?;
            bind_text(self.database, update.raw, 3, &job)?;
            bind_text(self.database, update.raw, 4, &worker)?;
            if terminal {
                bind_text(self.database, update.raw, 5, &claim)?;
            } else {
                bind_i64(
                    self.database,
                    update.raw,
                    5,
                    automation_execution_retry_at(now_unix_ms, current.attempt_count)?,
                )?;
                bind_text(self.database, update.raw, 6, &claim)?;
            }
            expect_done(self.database, update.raw)?;
            if self.changes() != 1 {
                return Err("automation execution failure compare-and-set was lost".into());
            }
            let updated = self
                .read_automation_execution_job(job_id)?
                .ok_or_else(|| "automation execution job disappeared after failure".to_string())?;
            let expected_state = if terminal {
                AutomationExecutionJobState::Failed
            } else {
                AutomationExecutionJobState::Pending
            };
            if updated.state != expected_state
                || updated.last_failure_code.as_deref() != Some(failure_code)
                || updated.lease_owner.is_some()
                || updated.lease_expires_at_unix_ms.is_some()
                || terminal != updated.completed_at_unix_ms.is_some()
            {
                return Err("automation execution failure was not durably exact".into());
            }
            Ok(updated)
        })();
        self.finish_automation_execution_transaction(outcome)
    }

    fn finish_automation_execution_transaction<T>(
        &self,
        outcome: Result<T, String>,
    ) -> Result<T, String> {
        match outcome {
            Ok(value) => match self.execute("COMMIT") {
                Ok(()) => Ok(value),
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

    fn claim_automation_execution_job_in_transaction(
        &self,
        job_id: &str,
        worker_id: &str,
        install_authority: &VerifiedInstallAuthority,
        now_unix_ms: i64,
        lease_expires: i64,
    ) -> Result<AutomationExecutionJobRecord, String> {
        let current = self
            .read_automation_execution_job(job_id)?
            .ok_or_else(|| "automation execution job does not exist".to_string())?;
        if current.state != AutomationExecutionJobState::Pending
            && !(current.state == AutomationExecutionJobState::Claimed
                && current
                    .lease_expires_at_unix_ms
                    .is_some_and(|expiry| expiry <= now_unix_ms))
        {
            return Err("automation execution job is not claimable".into());
        }
        if current.attempt_count >= MAX_AUTOMATION_EXECUTION_ATTEMPTS {
            return Err("automation execution attempt limit is exhausted".into());
        }
        self.validate_automation_execution_request_current(
            &current.request,
            install_authority,
            now_unix_ms,
        )?;
        let claim_id = generate_automation_execution_claim_id()?;
        let update = self.prepare(
            "UPDATE automation_execution_jobs SET state='claimed',attempt_count=attempt_count+1,lease_owner=?1,lease_expires_at_unix_ms=?2,claim_id=?3,claimed_at_unix_ms=?4,last_failure_code='',next_attempt_at_unix_ms=?4 \
             WHERE job_id=?5 AND ((state='pending' AND next_attempt_at_unix_ms<=?4) OR (state='claimed' AND lease_expires_at_unix_ms<=?6))",
        )?;
        let worker = c_string(worker_id, "automation execution worker identity")?;
        let claim = c_string(&claim_id, "automation execution claim identity")?;
        let job = c_string(job_id, "automation execution job identity")?;
        bind_text(self.database, update.raw, 1, &worker)?;
        bind_i64(self.database, update.raw, 2, lease_expires)?;
        bind_text(self.database, update.raw, 3, &claim)?;
        bind_i64(self.database, update.raw, 4, now_unix_ms)?;
        bind_text(self.database, update.raw, 5, &job)?;
        bind_i64(self.database, update.raw, 6, now_unix_ms)?;
        expect_done(self.database, update.raw)?;
        if self.changes() != 1 {
            return Err("automation execution claim lost its write lock".into());
        }
        let claimed = self
            .read_automation_execution_job(job_id)?
            .ok_or_else(|| "automation execution job disappeared after claim".to_string())?;
        if claimed.state != AutomationExecutionJobState::Claimed
            || claimed.lease_owner.as_deref() != Some(worker_id)
            || claimed.lease_expires_at_unix_ms != Some(lease_expires)
            || claimed.claim_id.as_deref() != Some(claim_id.as_str())
            || claimed.claimed_at_unix_ms != Some(now_unix_ms)
        {
            return Err("automation execution claim did not acquire exact authority".into());
        }
        Ok(claimed)
    }

    fn validate_automation_execution_request_current(
        &self,
        request: &AutomationExecutionRequest,
        install_authority: &VerifiedInstallAuthority,
        now_unix_ms: i64,
    ) -> Result<(), String> {
        if !self.automation_execution_request_is_current(request, install_authority, now_unix_ms)? {
            return Err("automation execution request is no longer current".into());
        }
        Ok(())
    }

    fn automation_execution_request_is_current(
        &self,
        request: &AutomationExecutionRequest,
        install_authority: &VerifiedInstallAuthority,
        now_unix_ms: i64,
    ) -> Result<bool, String> {
        let policy = match self.read_automation_feature_policy(&request.policy_id)? {
            Some(policy) => policy,
            None => return Ok(false),
        };
        let adapter = match self.read_automation_adapter_descriptor(&request.adapter_id)? {
            Some(adapter) => adapter,
            None => return Ok(false),
        };
        if validate_execution_install_authority(request, install_authority).is_err() {
            return Ok(false);
        }
        let current_candidate = automation_candidate_from_install(install_authority);
        if request
            .validate_against(&policy, &adapter, &current_candidate)
            .is_err()
        {
            return Ok(false);
        }
        let evidence = self
            .list_automation_connection_test_evidence(&request.policy_id, 1)?
            .into_iter()
            .next();
        Ok(derive_feature_automation_status(
            &policy,
            std::slice::from_ref(&adapter),
            evidence.as_ref(),
            &current_candidate,
            now_unix_ms,
        ) == FeatureAutomationStatus::Passed)
    }

    fn current_automation_execution_authority(
        &self,
        request: &AutomationExecutionRequest,
    ) -> Result<(AutomationFeaturePolicy, AutomationAdapterDescriptor), String> {
        let policy = self
            .read_automation_feature_policy(&request.policy_id)?
            .ok_or_else(|| "automation execution policy does not exist".to_string())?;
        let adapter = self
            .read_automation_adapter_descriptor(&request.adapter_id)?
            .ok_or_else(|| "automation execution adapter does not exist".to_string())?;
        Ok((policy, adapter))
    }

    fn read_automation_execution_job_row(
        &self,
        statement: *mut SqliteStatement,
    ) -> Result<AutomationExecutionJobRecord, String> {
        let request: AutomationExecutionRequest =
            serde_json::from_str(&column_text(statement, 22)?).map_err(|error| {
                format!("database returned invalid automation execution request JSON: {error}")
            })?;
        request.validate_shape()?;
        let state = parse_automation_execution_state(&column_text(statement, 12)?)?;
        let attempts_u64 = column_u64(statement, 13, "automation execution attempt count")?;
        let attempt_count = u32::try_from(attempts_u64)
            .map_err(|_| "automation execution attempt count is invalid".to_string())?;
        let lease_owner = column_text(statement, 14)?;
        let lease_expires = unsafe { sqlite3_column_int64(statement, 15) };
        let claim_id = column_text(statement, 16)?;
        let claimed_at = unsafe { sqlite3_column_int64(statement, 17) };
        let receipt_id = column_text(statement, 18)?;
        let last_failure_code =
            bounded_automation_column_text(statement, 19, 80, "automation execution failure code")?;
        let created_at_unix_ms = unsafe { sqlite3_column_int64(statement, 20) };
        let completed_at = unsafe { sqlite3_column_int64(statement, 21) };
        let next_attempt_at_unix_ms = unsafe { sqlite3_column_int64(statement, 23) };
        if request.job_id != column_text(statement, 0)?
            || automation_feature_name(request.feature) != column_text(statement, 1)?
            || request.policy_id != column_text(statement, 2)?
            || request.policy_revision
                != column_u64(statement, 3, "automation execution policy revision")?
            || request.adapter_id != column_text(statement, 4)?
            || request.adapter_revision
                != column_u64(statement, 5, "automation execution adapter revision")?
            || request.configuration_hash != column_text(statement, 6)?
            || request.candidate.version != column_text(statement, 7)?
            || request.candidate.source_commit != column_text(statement, 8)?
            || request.install_generation
                != column_u64(statement, 9, "automation execution install generation")?
            || request.input.artifact_sha256 != column_text(statement, 10)?
            || request.input.bytes != column_u64(statement, 11, "automation execution input bytes")?
            || attempt_count > MAX_AUTOMATION_EXECUTION_ATTEMPTS
            || created_at_unix_ms < 0
            || next_attempt_at_unix_ms < 0
            || (!last_failure_code.is_empty()
                && !valid_automation_execution_failure_code(&last_failure_code))
        {
            return Err("automation execution job row and request disagree".into());
        }
        let durable_receipt = self.read_automation_execution_receipt_for_job(&request)?;
        let receipt = if receipt_id.is_empty() {
            if durable_receipt.is_some() {
                return Err(
                    "automation execution receipt is orphaned from its durable job pointer".into(),
                );
            }
            None
        } else {
            let receipt = durable_receipt.ok_or_else(|| {
                "automation execution job references a missing receipt".to_string()
            })?;
            if receipt.receipt_id != receipt_id {
                return Err("automation execution job and receipt identities disagree".into());
            }
            Some(receipt)
        };
        let record = AutomationExecutionJobRecord {
            request,
            state,
            attempt_count,
            lease_owner: (!lease_owner.is_empty()).then_some(lease_owner),
            lease_expires_at_unix_ms: (lease_expires >= 0).then_some(lease_expires),
            claim_id: (!claim_id.is_empty()).then_some(claim_id),
            claimed_at_unix_ms: (claimed_at >= 0).then_some(claimed_at),
            receipt,
            last_failure_code: (!last_failure_code.is_empty()).then_some(last_failure_code),
            created_at_unix_ms,
            next_attempt_at_unix_ms,
            completed_at_unix_ms: (completed_at >= 0).then_some(completed_at),
        };
        validate_automation_execution_job_state(&record)?;
        Ok(record)
    }

    fn read_automation_execution_receipt_for_job(
        &self,
        request: &AutomationExecutionRequest,
    ) -> Result<Option<AutomationExecutionReceipt>, String> {
        let statement = self.prepare(
            "SELECT receipt_id,job_id,install_generation,claim_id,output_sha256,output_bytes,completed_at_unix_ms,record_json FROM automation_execution_receipts WHERE job_id=?1",
        )?;
        let identity = c_string(&request.job_id, "automation execution job identity")?;
        bind_text(self.database, statement.raw, 1, &identity)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let receipt: AutomationExecutionReceipt =
                    serde_json::from_str(&column_text(statement.raw, 7)?).map_err(|error| {
                        format!(
                            "database returned invalid automation execution receipt JSON: {error}"
                        )
                    })?;
                receipt.validate_shape_against_request(request)?;
                if receipt.receipt_id != column_text(statement.raw, 0)?
                    || receipt.job_id != column_text(statement.raw, 1)?
                    || receipt.install_generation
                        != column_u64(
                            statement.raw,
                            2,
                            "automation execution receipt install generation",
                        )?
                    || receipt.claim_id != column_text(statement.raw, 3)?
                    || receipt.output.artifact_sha256 != column_text(statement.raw, 4)?
                    || receipt.output.bytes
                        != column_u64(statement.raw, 5, "automation execution output bytes")?
                    || receipt.completed_at_unix_ms
                        != unsafe { sqlite3_column_int64(statement.raw, 6) }
                {
                    return Err("automation execution receipt row and record disagree".into());
                }
                Ok(Some(receipt))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn automation_execution_receipt_exists_for_job(&self, job_id: &str) -> Result<bool, String> {
        let statement =
            self.prepare("SELECT 1 FROM automation_execution_receipts WHERE job_id=?1 LIMIT 1")?;
        let identity = c_string(job_id, "automation execution job identity")?;
        bind_text(self.database, statement.raw, 1, &identity)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(true),
            SQLITE_DONE => Ok(false),
            _ => Err(sqlite_error(self.database)),
        }
    }
}

fn parse_automation_execution_state(value: &str) -> Result<AutomationExecutionJobState, String> {
    match value {
        "pending" => Ok(AutomationExecutionJobState::Pending),
        "claimed" => Ok(AutomationExecutionJobState::Claimed),
        "completed" => Ok(AutomationExecutionJobState::Completed),
        "failed" => Ok(AutomationExecutionJobState::Failed),
        _ => Err("database returned invalid automation execution job state".into()),
    }
}

fn canonical_automation_schema_sql(value: &str) -> String {
    #[derive(Clone, Copy, PartialEq, Eq)]
    enum Quote {
        None,
        Single,
        Double,
        Backtick,
        Bracket,
    }

    let mut normalized = String::with_capacity(value.len());
    let mut quote = Quote::None;
    let mut pending_space = false;
    let mut characters = value.chars().peekable();
    while let Some(character) = characters.next() {
        match quote {
            Quote::None if character.is_whitespace() => pending_space = true,
            Quote::None => {
                let prior = normalized.chars().next_back();
                let token_edge = |value: char| {
                    value.is_alphanumeric()
                        || value == '_'
                        || value == '$'
                        || matches!(value, '\'' | '"' | '`' | ']')
                };
                if pending_space
                    && prior.is_some_and(token_edge)
                    && (token_edge(character) || character == '[')
                {
                    normalized.push(' ');
                }
                pending_space = false;
                match character {
                    '\'' => {
                        quote = Quote::Single;
                        normalized.push(character);
                    }
                    '"' => {
                        quote = Quote::Double;
                        normalized.push(character);
                    }
                    '`' => {
                        quote = Quote::Backtick;
                        normalized.push(character);
                    }
                    '[' => {
                        quote = Quote::Bracket;
                        normalized.push(character);
                    }
                    _ => normalized.extend(character.to_lowercase()),
                }
            }
            Quote::Single | Quote::Double | Quote::Backtick => {
                normalized.push(character);
                let delimiter = match quote {
                    Quote::Single => '\'',
                    Quote::Double => '"',
                    Quote::Backtick => '`',
                    Quote::None | Quote::Bracket => unreachable!(),
                };
                if character == delimiter {
                    if characters.peek() == Some(&delimiter) {
                        normalized.push(characters.next().unwrap());
                    } else {
                        quote = Quote::None;
                    }
                }
            }
            Quote::Bracket => {
                normalized.push(character);
                if character == ']' {
                    quote = Quote::None;
                }
            }
        }
    }
    normalized
}

fn valid_automation_execution_failure_code(value: &str) -> bool {
    (1..=80).contains(&value.len())
        && value
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_')
}

fn automation_execution_retry_at(now_unix_ms: i64, attempt_count: u32) -> Result<i64, String> {
    let exponent = attempt_count.saturating_sub(1).min(5);
    let delay = AUTOMATION_EXECUTION_RETRY_INITIAL_MS
        .checked_mul(1_i64 << exponent)
        .ok_or_else(|| "automation execution retry delay overflowed".to_string())?
        .min(AUTOMATION_EXECUTION_RETRY_MAX_MS);
    now_unix_ms
        .checked_add(delay)
        .ok_or_else(|| "automation execution retry time overflowed".to_string())
}

fn automation_candidate_from_install(
    authority: &VerifiedInstallAuthority,
) -> AutomationCandidateIdentity {
    AutomationCandidateIdentity {
        version: authority.version().into(),
        source_commit: authority.source_commit().into(),
    }
}

fn validate_verified_install_authority(authority: &VerifiedInstallAuthority) -> Result<(), String> {
    let candidate = automation_candidate_from_install(authority);
    candidate.validate()?;
    if authority.install_generation() == 0 {
        return Err("automation execution install authority has no active generation".into());
    }
    Ok(())
}

fn validate_execution_install_authority(
    request: &AutomationExecutionRequest,
    authority: &VerifiedInstallAuthority,
) -> Result<(), String> {
    validate_verified_install_authority(authority)?;
    if request.candidate != automation_candidate_from_install(authority)
        || request.install_generation != authority.install_generation()
    {
        return Err(
            "automation execution request does not match verified install authority".into(),
        );
    }
    Ok(())
}

fn generate_automation_execution_claim_id() -> Result<String, String> {
    let mut bytes = [0_u8; 16];
    getrandom::fill(&mut bytes)
        .map_err(|_| "automation execution claim identity could not be issued".to_string())?;
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    Ok(format!(
        "{:02x}{:02x}{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}",
        bytes[0],
        bytes[1],
        bytes[2],
        bytes[3],
        bytes[4],
        bytes[5],
        bytes[6],
        bytes[7],
        bytes[8],
        bytes[9],
        bytes[10],
        bytes[11],
        bytes[12],
        bytes[13],
        bytes[14],
        bytes[15],
    ))
}

fn bounded_automation_column_text(
    statement: *mut SqliteStatement,
    column: c_int,
    max_bytes: usize,
    field: &str,
) -> Result<String, String> {
    // SAFETY: caller provides a live statement positioned on a row and valid column index.
    let bytes = unsafe { sqlite3_column_bytes(statement, column) };
    if bytes < 0 || usize::try_from(bytes).map_or(true, |bytes| bytes > max_bytes) {
        return Err(format!("database returned unbounded {field}"));
    }
    column_text(statement, column)
}

fn validate_automation_execution_job_state(
    record: &AutomationExecutionJobRecord,
) -> Result<(), String> {
    match record.state {
        AutomationExecutionJobState::Pending => {
            if record.lease_owner.is_some()
                || record.lease_expires_at_unix_ms.is_some()
                || record.claim_id.is_some()
                || record.claimed_at_unix_ms.is_some()
                || record.receipt.is_some()
                || record.completed_at_unix_ms.is_some()
            {
                return Err(
                    "pending automation execution job has terminal or lease authority".into(),
                );
            }
        }
        AutomationExecutionJobState::Claimed => {
            if record.lease_owner.is_none()
                || record.lease_expires_at_unix_ms.is_none()
                || record.claim_id.is_none()
                || record.claimed_at_unix_ms.is_none()
                || record.receipt.is_some()
                || record.completed_at_unix_ms.is_some()
            {
                return Err("claimed automation execution job has invalid lease authority".into());
            }
        }
        AutomationExecutionJobState::Completed => {
            if record.lease_owner.is_some()
                || record.lease_expires_at_unix_ms.is_some()
                || record.claim_id.is_none()
                || record.claimed_at_unix_ms.is_none()
                || record.receipt.is_none()
                || record.completed_at_unix_ms.is_none()
                || record.last_failure_code.is_some()
            {
                return Err(
                    "completed automation execution job lacks exact receipt authority".into(),
                );
            }
        }
        AutomationExecutionJobState::Failed => {
            if record.lease_owner.is_some()
                || record.lease_expires_at_unix_ms.is_some()
                || record.claim_id.is_some() != record.claimed_at_unix_ms.is_some()
                || record.receipt.is_some()
                || record.completed_at_unix_ms.is_none()
                || record.last_failure_code.is_none()
            {
                return Err("failed automation execution job lacks failure authority".into());
            }
        }
    }
    Ok(())
}
