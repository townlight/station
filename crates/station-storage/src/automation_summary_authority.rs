//! Durable authority for operator-imported transcript documents and cloud
//! summary jobs.  This module deliberately stops at storage: no provider,
//! network, helper process, or model execution is performed here.

use super::*;
use station_automation_policy::CloudSummaryProviderConfig;
use std::io::Write;

const MAX_TRANSCRIPT_BYTES: usize = 4 * 1024 * 1024;
const MAX_SUMMARY_BYTES: usize = 4 * 1024 * 1024;
const MAX_SUMMARY_WORDS: u32 = 1_000_000;
const MAX_SUMMARY_ATTEMPTS: u32 = 10;
const MAX_SUMMARY_JOB_LEASE_MS: i64 = 900_000;

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AutomationSummaryTranscript {
    pub document_id: String,
    pub sha256: String,
    pub bytes: u64,
    pub media_type: String,
    pub language: Option<String>,
    pub imported_by: String,
    pub imported_at_unix_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CloudSummaryProviderConfigHistory {
    pub config: CloudSummaryProviderConfig,
    pub configuration_hash: String,
    pub changed_by: String,
    pub occurred_at_unix_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AutomationSummaryCredentialStatus {
    pub config_id: String,
    pub config_revision: u64,
    pub credential_version: u64,
    pub configured: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AutomationSummaryJobRequest {
    pub job_id: String,
    pub transcript_sha256: String,
    pub transcript_bytes: u64,
    pub config_id: String,
    pub config_revision: u64,
    pub configuration_hash: String,
    pub credential_version: u64,
    pub authority_digest: String,
    pub requested_by: String,
    pub requested_at_unix_ms: i64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AutomationSummaryJobState {
    Pending,
    Claimed,
    Completed,
    Failed,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AutomationSummaryOutput {
    pub output_sha256: String,
    pub bytes: u64,
    pub word_count: u32,
    pub media_type: String,
    pub provider_request_id_sha256: String,
    pub provider_response_sha256: String,
    pub completed_at_unix_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AutomationSummaryCompletion<'a> {
    pub output: &'a [u8],
    pub provider_request_id_sha256: &'a str,
    pub provider_response_sha256: &'a str,
    pub completed_at_unix_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AutomationSummaryJobRecord {
    pub request: AutomationSummaryJobRequest,
    pub state: AutomationSummaryJobState,
    pub attempt_count: u32,
    pub lease_owner: Option<String>,
    pub lease_expires_at_unix_ms: Option<i64>,
    pub claim_id: Option<String>,
    pub output: Option<AutomationSummaryOutput>,
    pub failure_code: Option<String>,
    pub created_at_unix_ms: i64,
    pub completed_at_unix_ms: Option<i64>,
}

impl StationStore {
    pub(super) fn initialize_automation_summary_schema(&self) -> Result<(), String> {
        let root = self.summary_root()?;
        fs::create_dir_all(root.join("transcripts"))
            .map_err(|error| format!("create summary transcript root: {error}"))?;
        fs::create_dir_all(root.join("outputs"))
            .map_err(|error| format!("create summary output root: {error}"))?;
        self.ensure_summary_directory(&root)?;
        self.ensure_summary_directory(&root.join("transcripts"))?;
        self.ensure_summary_directory(&root.join("outputs"))?;
        self.execute(
            "CREATE TABLE IF NOT EXISTS automation_summary_configs (\
               config_id TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK (revision >= 1),\
               configuration_hash TEXT NOT NULL CHECK (length(configuration_hash)=64),\
               record_json TEXT NOT NULL\
             );\
             CREATE TABLE IF NOT EXISTS automation_summary_config_history (\
               config_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK (revision >= 1),\
               configuration_hash TEXT NOT NULL CHECK (length(configuration_hash)=64),\
               record_json TEXT NOT NULL, changed_by TEXT NOT NULL,\
               occurred_at_unix_ms INTEGER NOT NULL CHECK (occurred_at_unix_ms >= 0),\
               PRIMARY KEY(config_id,revision), FOREIGN KEY(config_id) REFERENCES automation_summary_configs(config_id)\
             );\
             CREATE TABLE IF NOT EXISTS automation_summary_credentials (\
               config_id TEXT NOT NULL, config_revision INTEGER NOT NULL CHECK (config_revision >= 1),\
               credential_version INTEGER NOT NULL CHECK (credential_version >= 1),\
               state TEXT NOT NULL CHECK (state IN ('active','deleted')), protected_secret BLOB NOT NULL,\
               changed_by TEXT NOT NULL, occurred_at_unix_ms INTEGER NOT NULL CHECK (occurred_at_unix_ms >= 0),\
               PRIMARY KEY(config_id,credential_version), FOREIGN KEY(config_id) REFERENCES automation_summary_configs(config_id)\
             );\
             CREATE INDEX IF NOT EXISTS automation_summary_credentials_current ON automation_summary_credentials(config_id,credential_version DESC);\
             CREATE TABLE IF NOT EXISTS automation_summary_transcripts (\
               sha256 TEXT PRIMARY KEY CHECK (length(sha256)=64), bytes INTEGER NOT NULL CHECK (bytes >= 1),\
               record_json TEXT NOT NULL\
             );\
             CREATE TABLE IF NOT EXISTS automation_summary_jobs (\
               job_id TEXT PRIMARY KEY, transcript_sha256 TEXT NOT NULL, transcript_bytes INTEGER NOT NULL CHECK (transcript_bytes >= 1),\
               config_id TEXT NOT NULL, config_revision INTEGER NOT NULL CHECK (config_revision >= 1),\
               configuration_hash TEXT NOT NULL CHECK (length(configuration_hash)=64), credential_version INTEGER NOT NULL CHECK (credential_version >= 1), authority_digest TEXT NOT NULL CHECK (length(authority_digest)=64),\
               state TEXT NOT NULL CHECK (state IN ('pending','claimed','completed','failed')), attempt_count INTEGER NOT NULL CHECK (attempt_count BETWEEN 0 AND 10),\
               lease_owner TEXT NOT NULL, lease_expires_at_unix_ms INTEGER NOT NULL CHECK (lease_expires_at_unix_ms >= -1), claim_id TEXT NOT NULL,\
               output_sha256 TEXT NOT NULL, failure_code TEXT NOT NULL, created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),\
               completed_at_unix_ms INTEGER NOT NULL CHECK (completed_at_unix_ms >= -1), request_json TEXT NOT NULL,\
               FOREIGN KEY(transcript_sha256) REFERENCES automation_summary_transcripts(sha256), FOREIGN KEY(config_id) REFERENCES automation_summary_configs(config_id)\
             );\
             CREATE TABLE IF NOT EXISTS automation_summary_outputs (\
               output_sha256 TEXT PRIMARY KEY CHECK (length(output_sha256)=64), job_id TEXT NOT NULL UNIQUE, bytes INTEGER NOT NULL CHECK (bytes >= 1),\
               record_json TEXT NOT NULL, FOREIGN KEY(job_id) REFERENCES automation_summary_jobs(job_id)\
             );\
             CREATE TRIGGER IF NOT EXISTS automation_summary_config_history_no_update BEFORE UPDATE ON automation_summary_config_history BEGIN SELECT RAISE(ABORT,'summary configuration history is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS automation_summary_config_history_no_delete BEFORE DELETE ON automation_summary_config_history BEGIN SELECT RAISE(ABORT,'summary configuration history is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS automation_summary_credentials_no_update BEFORE UPDATE ON automation_summary_credentials BEGIN SELECT RAISE(ABORT,'summary credentials are append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS automation_summary_credentials_no_delete BEFORE DELETE ON automation_summary_credentials BEGIN SELECT RAISE(ABORT,'summary credentials are append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS automation_summary_transcripts_no_update BEFORE UPDATE ON automation_summary_transcripts BEGIN SELECT RAISE(ABORT,'summary transcripts are immutable'); END;\
             CREATE TRIGGER IF NOT EXISTS automation_summary_transcripts_no_delete BEFORE DELETE ON automation_summary_transcripts BEGIN SELECT RAISE(ABORT,'summary transcripts are immutable'); END;\
             CREATE TRIGGER IF NOT EXISTS automation_summary_outputs_no_update BEFORE UPDATE ON automation_summary_outputs BEGIN SELECT RAISE(ABORT,'summary outputs are immutable'); END;\
             CREATE TRIGGER IF NOT EXISTS automation_summary_outputs_no_delete BEFORE DELETE ON automation_summary_outputs BEGIN SELECT RAISE(ABORT,'summary outputs are immutable'); END;\
             CREATE TRIGGER IF NOT EXISTS automation_summary_jobs_binding_immutable BEFORE UPDATE OF job_id,transcript_sha256,transcript_bytes,config_id,config_revision,configuration_hash,credential_version,authority_digest,created_at_unix_ms,request_json ON automation_summary_jobs BEGIN SELECT RAISE(ABORT,'summary job binding is immutable'); END;\
             CREATE TRIGGER IF NOT EXISTS automation_summary_jobs_terminal_immutable BEFORE UPDATE ON automation_summary_jobs WHEN OLD.state IN ('completed','failed') BEGIN SELECT RAISE(ABORT,'terminal summary jobs are immutable'); END;\
             CREATE TRIGGER IF NOT EXISTS automation_summary_jobs_no_delete BEFORE DELETE ON automation_summary_jobs BEGIN SELECT RAISE(ABORT,'summary jobs are durable history'); END;",
        )
    }

    pub fn read_cloud_summary_provider_config(
        &self,
        config_id: &str,
    ) -> Result<Option<(CloudSummaryProviderConfig, String)>, String> {
        validate_summary_uuid(config_id, "cloud summary configuration identity")?;
        let statement = self.prepare("SELECT revision,configuration_hash,record_json FROM automation_summary_configs WHERE config_id=?1")?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(config_id, "summary config")?,
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let config: CloudSummaryProviderConfig =
                    serde_json::from_str(&column_text(statement.raw, 2)?).map_err(|error| {
                        format!("summary configuration JSON is invalid: {error}")
                    })?;
                let hash = column_text(statement.raw, 1)?;
                config.validate()?;
                if config.config_id != config_id
                    || config.revision != column_u64(statement.raw, 0, "summary config revision")?
                    || canonical_summary_hash(&config)? != hash
                {
                    return Err("summary configuration row and record disagree".into());
                }
                Ok(Some((config, hash)))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn write_cloud_summary_provider_config(
        &self,
        config: &CloudSummaryProviderConfig,
        expected_revision: Option<u64>,
        changed_by: &str,
        occurred_at_unix_ms: i64,
    ) -> Result<String, String> {
        config.validate()?;
        validate_summary_actor(changed_by)?;
        if occurred_at_unix_ms < 0 {
            return Err("summary configuration time is invalid".into());
        }
        let hash = canonical_summary_hash(config)?;
        let record_json = serde_json::to_string(config)
            .map_err(|error| format!("serialize summary configuration: {error}"))?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let current = self.read_cloud_summary_provider_config(&config.config_id)?;
            let current_revision = current.as_ref().map(|(value, _)| value.revision);
            if current_revision != expected_revision {
                return Err("summary configuration CAS conflict".into());
            }
            if config.revision != current_revision.unwrap_or(0).saturating_add(1) {
                return Err("summary configuration revision is not the next revision".into());
            }
            let statement = self.prepare("INSERT INTO automation_summary_configs(config_id,revision,configuration_hash,record_json) VALUES(?1,?2,?3,?4)")?;
            bind_text(
                self.database,
                statement.raw,
                1,
                &c_string(&config.config_id, "summary config")?,
            )?;
            bind_i64(
                self.database,
                statement.raw,
                2,
                checked_i64(config.revision, "summary config revision")?,
            )?;
            bind_text(
                self.database,
                statement.raw,
                3,
                &c_string(&hash, "summary config hash")?,
            )?;
            bind_text(
                self.database,
                statement.raw,
                4,
                &c_string(&record_json, "summary config JSON")?,
            )?;
            expect_done(self.database, statement.raw)?;
            let history = self.prepare("INSERT INTO automation_summary_config_history(config_id,revision,configuration_hash,record_json,changed_by,occurred_at_unix_ms) VALUES(?1,?2,?3,?4,?5,?6)")?;
            bind_text(
                self.database,
                history.raw,
                1,
                &c_string(&config.config_id, "summary config")?,
            )?;
            bind_i64(
                self.database,
                history.raw,
                2,
                checked_i64(config.revision, "summary config revision")?,
            )?;
            bind_text(
                self.database,
                history.raw,
                3,
                &c_string(&hash, "summary config hash")?,
            )?;
            bind_text(
                self.database,
                history.raw,
                4,
                &c_string(&record_json, "summary config JSON")?,
            )?;
            bind_text(
                self.database,
                history.raw,
                5,
                &c_string(changed_by, "summary config actor")?,
            )?;
            bind_i64(self.database, history.raw, 6, occurred_at_unix_ms)?;
            expect_done(self.database, history.raw)?;
            Ok(hash)
        })();
        finish_summary_transaction(self, outcome)
    }

    pub fn list_cloud_summary_provider_config_history(
        &self,
        config_id: &str,
        limit: usize,
    ) -> Result<Vec<CloudSummaryProviderConfigHistory>, String> {
        validate_summary_uuid(config_id, "cloud summary configuration identity")?;
        if !(1..=128).contains(&limit) {
            return Err("summary configuration history limit is invalid".into());
        }
        let statement = self.prepare("SELECT configuration_hash,record_json,changed_by,occurred_at_unix_ms FROM automation_summary_config_history WHERE config_id=?1 ORDER BY revision DESC LIMIT ?2")?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(config_id, "summary config")?,
        )?;
        bind_i64(self.database, statement.raw, 2, limit as i64)?;
        let mut output = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    let config: CloudSummaryProviderConfig =
                        serde_json::from_str(&column_text(statement.raw, 1)?).map_err(|_| {
                            "summary configuration history JSON is invalid".to_string()
                        })?;
                    config.validate()?;
                    let hash = column_text(statement.raw, 0)?;
                    if config.config_id != config_id || canonical_summary_hash(&config)? != hash {
                        return Err("summary configuration history is inconsistent".into());
                    }
                    output.push(CloudSummaryProviderConfigHistory {
                        config,
                        configuration_hash: hash,
                        changed_by: column_text(statement.raw, 2)?,
                        occurred_at_unix_ms: unsafe { sqlite3_column_int64(statement.raw, 3) },
                    });
                }
                SQLITE_DONE => return Ok(output),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn provision_cloud_summary_credential(
        &self,
        config_id: &str,
        config_revision: u64,
        secret: &[u8],
        changed_by: &str,
        occurred_at_unix_ms: i64,
    ) -> Result<u64, String> {
        validate_summary_uuid(config_id, "cloud summary configuration identity")?;
        validate_summary_actor(changed_by)?;
        if occurred_at_unix_ms < 0 || !(8..=16_384).contains(&secret.len()) || secret.contains(&0) {
            return Err("cloud summary credential is empty, oversized, or invalid".into());
        }
        let (config, _) = self
            .read_cloud_summary_provider_config(config_id)?
            .ok_or_else(|| "cloud summary configuration is missing".to_string())?;
        if config.revision != config_revision {
            return Err(
                "cloud summary configuration changed before credential provisioning".into(),
            );
        }
        let protected = protect_local_machine(secret)?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let version = self.next_cloud_summary_credential_version(config_id)?;
            let statement = self.prepare("INSERT INTO automation_summary_credentials(config_id,config_revision,credential_version,state,protected_secret,changed_by,occurred_at_unix_ms) VALUES(?1,?2,?3,'active',?4,?5,?6)")?;
            bind_text(
                self.database,
                statement.raw,
                1,
                &c_string(config_id, "summary config")?,
            )?;
            bind_i64(
                self.database,
                statement.raw,
                2,
                checked_i64(config_revision, "summary config revision")?,
            )?;
            bind_i64(
                self.database,
                statement.raw,
                3,
                checked_i64(version, "summary credential version")?,
            )?;
            bind_blob(self.database, statement.raw, 4, &protected)?;
            bind_text(
                self.database,
                statement.raw,
                5,
                &c_string(changed_by, "summary credential actor")?,
            )?;
            bind_i64(self.database, statement.raw, 6, occurred_at_unix_ms)?;
            expect_done(self.database, statement.raw)?;
            Ok(version)
        })();
        finish_summary_transaction(self, outcome)
    }

    pub fn delete_cloud_summary_credential(
        &self,
        config_id: &str,
        config_revision: u64,
        changed_by: &str,
        occurred_at_unix_ms: i64,
    ) -> Result<u64, String> {
        validate_summary_uuid(config_id, "cloud summary configuration identity")?;
        validate_summary_actor(changed_by)?;
        if occurred_at_unix_ms < 0 {
            return Err("cloud summary credential time is invalid".into());
        }
        let (config, _) = self
            .read_cloud_summary_provider_config(config_id)?
            .ok_or_else(|| "cloud summary configuration is missing".to_string())?;
        if config.revision != config_revision {
            return Err("cloud summary configuration changed before credential deletion".into());
        }
        if !self
            .cloud_summary_credential_status(config_id, config_revision)?
            .configured
        {
            return Err("cloud summary credential is not configured".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let version = self.next_cloud_summary_credential_version(config_id)?;
            let statement = self.prepare("INSERT INTO automation_summary_credentials(config_id,config_revision,credential_version,state,protected_secret,changed_by,occurred_at_unix_ms) VALUES(?1,?2,?3,'deleted',x'00',?4,?5)")?;
            bind_text(
                self.database,
                statement.raw,
                1,
                &c_string(config_id, "summary config")?,
            )?;
            bind_i64(
                self.database,
                statement.raw,
                2,
                checked_i64(config_revision, "summary config revision")?,
            )?;
            bind_i64(
                self.database,
                statement.raw,
                3,
                checked_i64(version, "summary credential version")?,
            )?;
            bind_text(
                self.database,
                statement.raw,
                4,
                &c_string(changed_by, "summary credential actor")?,
            )?;
            bind_i64(self.database, statement.raw, 5, occurred_at_unix_ms)?;
            expect_done(self.database, statement.raw)?;
            Ok(version)
        })();
        finish_summary_transaction(self, outcome)
    }

    pub fn cloud_summary_credential_status(
        &self,
        config_id: &str,
        config_revision: u64,
    ) -> Result<AutomationSummaryCredentialStatus, String> {
        validate_summary_uuid(config_id, "cloud summary configuration identity")?;
        if config_revision == 0 {
            return Err("cloud summary configuration revision is invalid".into());
        }
        let statement = self.prepare("SELECT config_revision,credential_version,state,protected_secret FROM automation_summary_credentials WHERE config_id=?1 ORDER BY credential_version DESC LIMIT 1")?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(config_id, "summary config")?,
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let bound = column_u64(statement.raw, 0, "summary credential config revision")?;
                let version = column_u64(statement.raw, 1, "summary credential version")?;
                let state = column_text(statement.raw, 2)?;
                if bound != config_revision || !matches!(state.as_str(), "active" | "deleted") {
                    return Ok(AutomationSummaryCredentialStatus {
                        config_id: config_id.into(),
                        config_revision,
                        credential_version: version,
                        configured: false,
                    });
                }
                let protected = column_blob(statement.raw, 3)?;
                if state == "active" {
                    let mut plaintext = unprotect_local_machine(&protected)
                        .map_err(|_| "cloud summary credential failed integrity verification")?;
                    plaintext.fill(0);
                } else if protected != [0] {
                    return Err(
                        "deleted cloud summary credential contains invalid secret material".into(),
                    );
                }
                Ok(AutomationSummaryCredentialStatus {
                    config_id: config_id.into(),
                    config_revision,
                    credential_version: version,
                    configured: state == "active",
                })
            }
            SQLITE_DONE => Ok(AutomationSummaryCredentialStatus {
                config_id: config_id.into(),
                config_revision,
                credential_version: 0,
                configured: false,
            }),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn import_automation_summary_transcript(
        &self,
        bytes: &[u8],
        media_type: &str,
        language: Option<&str>,
        imported_by: &str,
        imported_at_unix_ms: i64,
    ) -> Result<AutomationSummaryTranscript, String> {
        validate_summary_actor(imported_by)?;
        if imported_at_unix_ms < 0
            || bytes.is_empty()
            || bytes.len() > MAX_TRANSCRIPT_BYTES
            || !valid_summary_media_type(media_type)
        {
            return Err("summary transcript is empty, oversized, or has invalid metadata".into());
        }
        let text = std::str::from_utf8(bytes)
            .map_err(|_| "summary transcript must be valid UTF-8".to_string())?;
        validate_summary_text(text, "summary transcript")?;
        validate_summary_language(language)?;
        let sha256 = format!("{:x}", Sha256::digest(bytes));
        let document = AutomationSummaryTranscript {
            document_id: sha256.clone(),
            sha256: sha256.clone(),
            bytes: bytes.len() as u64,
            media_type: media_type.into(),
            language: language.map(str::to_owned),
            imported_by: imported_by.into(),
            imported_at_unix_ms,
        };
        let path = self.summary_content_path("transcripts", &sha256)?;
        self.ensure_summary_content(&path, bytes, "summary transcript")?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            if let Some(existing) = self.read_summary_transcript_record(&sha256)? {
                if existing != document {
                    return Err(
                        "summary transcript identity is already bound to different metadata".into(),
                    );
                }
                return Ok(existing);
            }
            let json = serde_json::to_string(&document)
                .map_err(|error| format!("serialize summary transcript: {error}"))?;
            let statement = self.prepare("INSERT INTO automation_summary_transcripts(sha256,bytes,record_json) VALUES(?1,?2,?3)")?;
            bind_text(
                self.database,
                statement.raw,
                1,
                &c_string(&sha256, "summary transcript hash")?,
            )?;
            bind_i64(self.database, statement.raw, 2, bytes.len() as i64)?;
            bind_text(
                self.database,
                statement.raw,
                3,
                &c_string(&json, "summary transcript JSON")?,
            )?;
            expect_done(self.database, statement.raw)?;
            Ok(document)
        })();
        finish_summary_transaction(self, outcome)
    }

    pub fn read_automation_summary_transcript(&self, sha256: &str) -> Result<Vec<u8>, String> {
        validate_summary_sha(sha256, "summary transcript hash")?;
        let record = self
            .read_summary_transcript_record(sha256)?
            .ok_or_else(|| "summary transcript is missing".to_string())?;
        let path = self.summary_content_path("transcripts", sha256)?;
        let bytes =
            self.read_verified_summary_content(&path, sha256, record.bytes, "summary transcript")?;
        std::str::from_utf8(&bytes)
            .map_err(|_| "managed summary transcript is not UTF-8".to_string())?;
        Ok(bytes)
    }

    pub fn enqueue_automation_summary_job(
        &self,
        request: &AutomationSummaryJobRequest,
    ) -> Result<AutomationSummaryJobRecord, String> {
        validate_summary_job_request(request)?;
        let (config, hash) = self
            .read_cloud_summary_provider_config(&request.config_id)?
            .ok_or_else(|| "summary configuration is missing".to_string())?;
        let credential =
            self.cloud_summary_credential_status(&request.config_id, config.revision)?;
        let transcript = self
            .read_summary_transcript_record(&request.transcript_sha256)?
            .ok_or_else(|| "summary transcript is missing".to_string())?;
        if hash != request.configuration_hash
            || credential.credential_version != request.credential_version
            || request.authority_digest
                != cloud_summary_authority_digest(&hash, request.credential_version, true)?
            || !credential.configured
            || transcript.bytes != request.transcript_bytes
        {
            return Err("summary job authority is stale or incomplete".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let (current_config, current_hash) = self
                .read_cloud_summary_provider_config(&request.config_id)?
                .ok_or_else(|| "summary configuration is missing".to_string())?;
            let current_credential =
                self.cloud_summary_credential_status(&request.config_id, current_config.revision)?;
            let current_transcript = self
                .read_summary_transcript_record(&request.transcript_sha256)?
                .ok_or_else(|| "summary transcript is missing".to_string())?;
            if current_config.revision != request.config_revision
                || current_hash != request.configuration_hash
                || current_credential.credential_version != request.credential_version
                || request.authority_digest
                    != cloud_summary_authority_digest(
                        &current_hash,
                        request.credential_version,
                        true,
                    )?
                || !current_credential.configured
                || current_transcript.bytes != request.transcript_bytes
            {
                return Err("summary job authority is stale or incomplete".into());
            }
            if let Some(existing) = self.read_automation_summary_job(&request.job_id)? {
                if existing.request == *request {
                    return Ok(existing);
                }
                return Err("summary job identity is already bound".into());
            }
            let json = serde_json::to_string(request)
                .map_err(|error| format!("serialize summary job request: {error}"))?;
            let statement = self.prepare("INSERT INTO automation_summary_jobs(job_id,transcript_sha256,transcript_bytes,config_id,config_revision,configuration_hash,credential_version,authority_digest,state,attempt_count,lease_owner,lease_expires_at_unix_ms,claim_id,output_sha256,failure_code,created_at_unix_ms,completed_at_unix_ms,request_json) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,'pending',0,'',-1,'','','',?9,-1,?10)")?;
            for (index, value) in [
                (1, request.job_id.as_str()),
                (2, request.transcript_sha256.as_str()),
                (4, request.config_id.as_str()),
                (6, request.configuration_hash.as_str()),
                (8, request.authority_digest.as_str()),
            ] {
                bind_text(
                    self.database,
                    statement.raw,
                    index,
                    &c_string(value, "summary job field")?,
                )?;
            }
            bind_i64(
                self.database,
                statement.raw,
                3,
                checked_i64(request.transcript_bytes, "summary transcript bytes")?,
            )?;
            bind_i64(
                self.database,
                statement.raw,
                5,
                checked_i64(request.config_revision, "summary config revision")?,
            )?;
            bind_i64(
                self.database,
                statement.raw,
                7,
                checked_i64(request.credential_version, "summary credential version")?,
            )?;
            bind_i64(
                self.database,
                statement.raw,
                9,
                request.requested_at_unix_ms,
            )?;
            bind_text(
                self.database,
                statement.raw,
                10,
                &c_string(&json, "summary job JSON")?,
            )?;
            expect_done(self.database, statement.raw)?;
            self.read_automation_summary_job(&request.job_id)?
                .ok_or_else(|| "summary job disappeared after insert".into())
        })();
        finish_summary_transaction(self, outcome)
    }

    pub fn claim_automation_summary_job(
        &self,
        worker_id: &str,
        now_unix_ms: i64,
    ) -> Result<Option<AutomationSummaryJobRecord>, String> {
        validate_summary_actor(worker_id)?;
        if now_unix_ms < 0 {
            return Err("summary worker time is invalid".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let statement = self.prepare("SELECT job_id FROM automation_summary_jobs WHERE state='pending' AND created_at_unix_ms<=?1 ORDER BY created_at_unix_ms,job_id LIMIT 1")?;
            bind_i64(self.database, statement.raw, 1, now_unix_ms)?;
            let job_id = match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => column_text(statement.raw, 0)?,
                SQLITE_DONE => return Ok(None),
                _ => return Err(sqlite_error(self.database)),
            };
            let claim_id = new_summary_uuid()?;
            let lease = now_unix_ms
                .checked_add(MAX_SUMMARY_JOB_LEASE_MS)
                .ok_or("summary lease overflow")?;
            let update = self.prepare("UPDATE automation_summary_jobs SET state='claimed',attempt_count=attempt_count+1,lease_owner=?1,lease_expires_at_unix_ms=?2,claim_id=?3 WHERE job_id=?4 AND state='pending' AND attempt_count < 10")?;
            bind_text(
                self.database,
                update.raw,
                1,
                &c_string(worker_id, "summary worker")?,
            )?;
            bind_i64(self.database, update.raw, 2, lease)?;
            bind_text(
                self.database,
                update.raw,
                3,
                &c_string(&claim_id, "summary claim")?,
            )?;
            bind_text(
                self.database,
                update.raw,
                4,
                &c_string(&job_id, "summary job")?,
            )?;
            expect_done(self.database, update.raw)?;
            let claimed = self.read_automation_summary_job(&job_id)?;
            if claimed
                .as_ref()
                .is_some_and(|job| job.state != AutomationSummaryJobState::Claimed)
            {
                return Ok(None);
            }
            Ok(claimed)
        })();
        finish_summary_transaction(self, outcome)
    }

    pub fn renew_automation_summary_job(
        &self,
        job_id: &str,
        worker_id: &str,
        claim_id: &str,
        now_unix_ms: i64,
    ) -> Result<AutomationSummaryJobRecord, String> {
        validate_summary_uuid(job_id, "summary job identity")?;
        validate_summary_actor(worker_id)?;
        validate_summary_uuid(claim_id, "summary claim identity")?;
        if now_unix_ms < 0 {
            return Err("summary worker time is invalid".into());
        }
        let lease = now_unix_ms
            .checked_add(MAX_SUMMARY_JOB_LEASE_MS)
            .ok_or("summary lease overflow")?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let update = self.prepare("UPDATE automation_summary_jobs SET lease_expires_at_unix_ms=?1 WHERE job_id=?2 AND state='claimed' AND lease_owner=?3 AND claim_id=?4 AND lease_expires_at_unix_ms>?5")?;
            bind_i64(self.database, update.raw, 1, lease)?;
            bind_text(
                self.database,
                update.raw,
                2,
                &c_string(job_id, "summary job")?,
            )?;
            bind_text(
                self.database,
                update.raw,
                3,
                &c_string(worker_id, "summary worker")?,
            )?;
            bind_text(
                self.database,
                update.raw,
                4,
                &c_string(claim_id, "summary claim")?,
            )?;
            bind_i64(self.database, update.raw, 5, now_unix_ms)?;
            expect_done(self.database, update.raw)?;
            let job = self
                .read_automation_summary_job(job_id)?
                .ok_or_else(|| "summary job is missing".to_string())?;
            if job.state != AutomationSummaryJobState::Claimed
                || job.lease_owner.as_deref() != Some(worker_id)
                || job.claim_id.as_deref() != Some(claim_id)
                || job.lease_expires_at_unix_ms != Some(lease)
            {
                return Err("summary job lease is stale or invalid".into());
            }
            Ok(job)
        })();
        finish_summary_transaction(self, outcome)
    }

    pub fn recover_expired_automation_summary_jobs(&self, now_unix_ms: i64) -> Result<(), String> {
        if now_unix_ms < 0 {
            return Err("summary recovery time is invalid".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let update = self.prepare("UPDATE automation_summary_jobs SET state=CASE WHEN attempt_count >= 10 THEN 'failed' ELSE 'pending' END,lease_owner='',lease_expires_at_unix_ms=-1,claim_id='',failure_code=CASE WHEN attempt_count >= 10 THEN 'attempts_exhausted' ELSE 'lease_expired' END WHERE state='claimed' AND lease_expires_at_unix_ms>=0 AND lease_expires_at_unix_ms<=?1")?;
            bind_i64(self.database, update.raw, 1, now_unix_ms)?;
            expect_done(self.database, update.raw)
        })();
        finish_summary_transaction(self, outcome)
    }

    pub fn complete_automation_summary_job(
        &self,
        job_id: &str,
        worker_id: &str,
        claim_id: &str,
        completion: &AutomationSummaryCompletion<'_>,
    ) -> Result<AutomationSummaryJobRecord, String> {
        validate_summary_uuid(job_id, "summary job identity")?;
        validate_summary_actor(worker_id)?;
        validate_summary_uuid(claim_id, "summary claim identity")?;
        validate_summary_sha(
            completion.provider_request_id_sha256,
            "provider request digest",
        )?;
        validate_summary_sha(
            completion.provider_response_sha256,
            "provider response digest",
        )?;
        if completion.completed_at_unix_ms < 0
            || completion.output.is_empty()
            || completion.output.len() > MAX_SUMMARY_BYTES
        {
            return Err("summary output is empty, oversized, or invalid".into());
        }
        let text = std::str::from_utf8(completion.output)
            .map_err(|_| "summary output must be valid UTF-8".to_string())?;
        validate_summary_text(text, "summary output")?;
        let word_count = text.split_whitespace().count();
        if word_count == 0 || word_count > MAX_SUMMARY_WORDS as usize {
            return Err("summary output word count is invalid".into());
        }
        let job = self
            .read_automation_summary_job(job_id)?
            .ok_or_else(|| "summary job is missing".to_string())?;
        if job.state != AutomationSummaryJobState::Claimed
            || job.lease_owner.as_deref() != Some(worker_id)
            || job.claim_id.as_deref() != Some(claim_id)
        {
            return Err("summary job claim is stale or invalid".into());
        }
        let (config, hash) = self
            .read_cloud_summary_provider_config(&job.request.config_id)?
            .ok_or_else(|| "summary configuration is missing".to_string())?;
        let credential =
            self.cloud_summary_credential_status(&job.request.config_id, config.revision)?;
        if config.revision != job.request.config_revision
            || hash != job.request.configuration_hash
            || credential.credential_version != job.request.credential_version
            || job.request.authority_digest
                != cloud_summary_authority_digest(&hash, job.request.credential_version, true)?
            || !credential.configured
        {
            return Err("summary job authority changed before completion".into());
        }
        let sha256 = format!("{:x}", Sha256::digest(completion.output));
        let managed = self.summary_content_path("outputs", &sha256)?;
        self.ensure_summary_content(&managed, completion.output, "summary output")?;
        let record = AutomationSummaryOutput {
            output_sha256: sha256.clone(),
            bytes: completion.output.len() as u64,
            word_count: word_count as u32,
            media_type: "text/plain; charset=utf-8".into(),
            provider_request_id_sha256: completion.provider_request_id_sha256.into(),
            provider_response_sha256: completion.provider_response_sha256.into(),
            completed_at_unix_ms: completion.completed_at_unix_ms,
        };
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let live_job = self
                .read_automation_summary_job(job_id)?
                .ok_or_else(|| "summary job is missing".to_string())?;
            let (live_config, live_hash) = self
                .read_cloud_summary_provider_config(&live_job.request.config_id)?
                .ok_or_else(|| "summary configuration is missing".to_string())?;
            let live_credential = self.cloud_summary_credential_status(
                &live_job.request.config_id,
                live_config.revision,
            )?;
            if live_job.state != AutomationSummaryJobState::Claimed
                || live_job.lease_owner.as_deref() != Some(worker_id)
                || live_job.claim_id.as_deref() != Some(claim_id)
                || live_config.revision != live_job.request.config_revision
                || live_hash != live_job.request.configuration_hash
                || live_credential.credential_version != live_job.request.credential_version
                || live_job.request.authority_digest
                    != cloud_summary_authority_digest(
                        &live_hash,
                        live_job.request.credential_version,
                        true,
                    )?
                || !live_credential.configured
            {
                return Err("summary job authority changed before completion".into());
            }
            let json = serde_json::to_string(&record)
                .map_err(|error| format!("serialize summary output: {error}"))?;
            let insert = self.prepare("INSERT INTO automation_summary_outputs(output_sha256,job_id,bytes,record_json) VALUES(?1,?2,?3,?4)")?;
            bind_text(
                self.database,
                insert.raw,
                1,
                &c_string(&sha256, "summary output hash")?,
            )?;
            bind_text(
                self.database,
                insert.raw,
                2,
                &c_string(job_id, "summary job")?,
            )?;
            bind_i64(self.database, insert.raw, 3, completion.output.len() as i64)?;
            bind_text(
                self.database,
                insert.raw,
                4,
                &c_string(&json, "summary output JSON")?,
            )?;
            expect_done(self.database, insert.raw)?;
            let update = self.prepare("UPDATE automation_summary_jobs SET state='completed',lease_owner='',lease_expires_at_unix_ms=-1,claim_id='',output_sha256=?1,completed_at_unix_ms=?2 WHERE job_id=?3 AND state='claimed' AND lease_owner=?4 AND claim_id=?5")?;
            bind_text(
                self.database,
                update.raw,
                1,
                &c_string(&sha256, "summary output hash")?,
            )?;
            bind_i64(
                self.database,
                update.raw,
                2,
                completion.completed_at_unix_ms,
            )?;
            bind_text(
                self.database,
                update.raw,
                3,
                &c_string(job_id, "summary job")?,
            )?;
            bind_text(
                self.database,
                update.raw,
                4,
                &c_string(worker_id, "summary worker")?,
            )?;
            bind_text(
                self.database,
                update.raw,
                5,
                &c_string(claim_id, "summary claim")?,
            )?;
            expect_done(self.database, update.raw)?;
            self.read_automation_summary_job(job_id)?
                .ok_or_else(|| "summary job disappeared after completion".into())
        })();
        finish_summary_transaction(self, outcome)
    }

    pub fn read_automation_summary_job(
        &self,
        job_id: &str,
    ) -> Result<Option<AutomationSummaryJobRecord>, String> {
        validate_summary_uuid(job_id, "summary job identity")?;
        let statement = self.prepare("SELECT request_json,state,attempt_count,lease_owner,lease_expires_at_unix_ms,claim_id,output_sha256,failure_code,created_at_unix_ms,completed_at_unix_ms,authority_digest FROM automation_summary_jobs WHERE job_id=?1")?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(job_id, "summary job")?,
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let request: AutomationSummaryJobRequest =
                    serde_json::from_str(&column_text(statement.raw, 0)?)
                        .map_err(|_| "summary job request JSON is invalid".to_string())?;
                validate_summary_job_request(&request)?;
                if request.job_id != job_id {
                    return Err("summary job identity and request disagree".into());
                }
                if request.authority_digest != column_text(statement.raw, 10)? {
                    return Err("summary job authority digest disagrees with request".into());
                }
                let state = parse_summary_job_state(&column_text(statement.raw, 1)?)?;
                let attempt = column_u64(statement.raw, 2, "summary attempt count")?;
                if attempt > u64::from(MAX_SUMMARY_ATTEMPTS) {
                    return Err("summary attempt count is invalid".into());
                }
                let lease = unsafe { sqlite3_column_int64(statement.raw, 4) };
                let completed = unsafe { sqlite3_column_int64(statement.raw, 9) };
                let output_sha = column_text(statement.raw, 6)?;
                let output = if output_sha.is_empty() {
                    None
                } else {
                    self.read_summary_output(job_id, &output_sha)?
                };
                let record = AutomationSummaryJobRecord {
                    request,
                    state,
                    attempt_count: u32::try_from(attempt)
                        .map_err(|_| "summary attempt count is invalid".to_string())?,
                    lease_owner: (!column_text(statement.raw, 3)?.is_empty())
                        .then(|| column_text(statement.raw, 3).unwrap()),
                    lease_expires_at_unix_ms: (lease >= 0).then_some(lease),
                    claim_id: {
                        let value = column_text(statement.raw, 5)?;
                        (!value.is_empty()).then_some(value)
                    },
                    output,
                    failure_code: {
                        let value = column_text(statement.raw, 7)?;
                        (!value.is_empty()).then_some(value)
                    },
                    created_at_unix_ms: unsafe { sqlite3_column_int64(statement.raw, 8) },
                    completed_at_unix_ms: (completed >= 0).then_some(completed),
                };
                validate_summary_job_state(&record)?;
                Ok(Some(record))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn read_summary_output(
        &self,
        job_id: &str,
        output_sha: &str,
    ) -> Result<Option<AutomationSummaryOutput>, String> {
        validate_summary_sha(output_sha, "summary output hash")?;
        let statement = self.prepare("SELECT bytes,record_json FROM automation_summary_outputs WHERE job_id=?1 AND output_sha256=?2")?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(job_id, "summary job")?,
        )?;
        bind_text(
            self.database,
            statement.raw,
            2,
            &c_string(output_sha, "summary output hash")?,
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let output: AutomationSummaryOutput =
                    serde_json::from_str(&column_text(statement.raw, 1)?)
                        .map_err(|_| "summary output JSON is invalid".to_string())?;
                validate_summary_sha(
                    &output.provider_request_id_sha256,
                    "provider request digest",
                )?;
                validate_summary_sha(&output.provider_response_sha256, "provider response digest")?;
                if output.output_sha256 != output_sha
                    || output.bytes != column_u64(statement.raw, 0, "summary output bytes")?
                    || output.bytes == 0
                    || output.word_count == 0
                    || output.word_count > MAX_SUMMARY_WORDS
                    || output.completed_at_unix_ms < 0
                    || !valid_summary_media_type(&output.media_type)
                {
                    return Err("summary output row is inconsistent".into());
                }
                let path = self.summary_content_path("outputs", output_sha)?;
                let _ = self.read_verified_summary_content(
                    &path,
                    output_sha,
                    output.bytes,
                    "summary output",
                )?;
                Ok(Some(output))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn read_summary_transcript_record(
        &self,
        sha256: &str,
    ) -> Result<Option<AutomationSummaryTranscript>, String> {
        let statement = self.prepare(
            "SELECT bytes,record_json FROM automation_summary_transcripts WHERE sha256=?1",
        )?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(sha256, "summary transcript hash")?,
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let record: AutomationSummaryTranscript =
                    serde_json::from_str(&column_text(statement.raw, 1)?)
                        .map_err(|_| "summary transcript JSON is invalid".to_string())?;
                if record.sha256 != sha256
                    || record.document_id != sha256
                    || record.bytes != column_u64(statement.raw, 0, "summary transcript bytes")?
                {
                    return Err("summary transcript row is inconsistent".into());
                }
                Ok(Some(record))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn next_cloud_summary_credential_version(&self, config_id: &str) -> Result<u64, String> {
        let statement=self.prepare("SELECT COALESCE(MAX(credential_version),0) FROM automation_summary_credentials WHERE config_id=?1")?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(config_id, "summary config")?,
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => column_u64(statement.raw, 0, "summary credential version")?
                .checked_add(1)
                .ok_or_else(|| "summary credential version overflowed".into()),
            _ => Err(sqlite_error(self.database)),
        }
    }
    fn summary_root(&self) -> Result<PathBuf, String> {
        Ok(self._data_root.join("automation-summaries"))
    }
    fn ensure_summary_directory(&self, path: &Path) -> Result<(), String> {
        let metadata = fs::symlink_metadata(path)
            .map_err(|error| format!("inspect managed summary directory: {error}"))?;
        if !metadata.is_dir() || metadata.file_attributes() & 0x400 != 0 {
            return Err("managed summary directory is a reparse point or not a directory".into());
        }
        let canonical = fs::canonicalize(path)
            .map_err(|error| format!("canonicalize managed summary directory: {error}"))?;
        if canonical != path {
            return Err("managed summary directory escapes station data root".into());
        }
        Ok(())
    }
    fn summary_content_path(&self, kind: &str, sha256: &str) -> Result<PathBuf, String> {
        validate_summary_sha(sha256, "managed summary hash")?;
        if !matches!(kind, "transcripts" | "outputs") {
            return Err("managed summary namespace is invalid".into());
        }
        let summary_root = self.summary_root()?;
        self.ensure_summary_directory(&summary_root)?;
        let root = summary_root.join(kind);
        self.ensure_summary_directory(&root)?;
        Ok(root.join(sha256))
    }
    fn ensure_summary_content(&self, path: &Path, bytes: &[u8], label: &str) -> Result<(), String> {
        if let Ok(metadata) = fs::symlink_metadata(path) {
            if !metadata.is_file() || metadata.file_attributes() & 0x400 != 0 {
                return Err(format!("managed {label} is a reparse point or not a file"));
            }
            let existing = fs::read(path)
                .map_err(|error| format!("read existing managed {label}: {error}"))?;
            if existing != bytes {
                return Err(format!(
                    "managed {label} identity is already bound to different bytes"
                ));
            }
            return Ok(());
        }
        let parent = path
            .parent()
            .ok_or("managed summary content has no parent")?;
        let mut entropy = [0_u8; 16];
        getrandom::fill(&mut entropy).map_err(|_| "summary content entropy is unavailable")?;
        let temp = parent.join(format!(".staging-{:x}", u128::from_le_bytes(entropy)));
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temp)
            .map_err(|error| format!("create managed {label} staging file: {error}"))?;
        file.write_all(bytes)
            .map_err(|error| format!("write managed {label}: {error}"))?;
        file.sync_all()
            .map_err(|error| format!("flush managed {label}: {error}"))?;
        drop(file);
        match fs::rename(&temp, path) {
            Ok(()) => Ok(()),
            Err(_) => {
                let _ = fs::remove_file(&temp);
                let existing = fs::read(path)
                    .map_err(|error| format!("managed {label} publish failed: {error}"))?;
                if existing == bytes {
                    Ok(())
                } else {
                    Err(format!(
                        "managed {label} identity is already bound to different bytes"
                    ))
                }
            }
        }
    }

    fn read_verified_summary_content(
        &self,
        path: &Path,
        expected_sha256: &str,
        expected_bytes: u64,
        label: &str,
    ) -> Result<Vec<u8>, String> {
        let metadata =
            fs::symlink_metadata(path).map_err(|_| format!("managed {label} is missing"))?;
        if !metadata.is_file() || metadata.file_attributes() & 0x400 != 0 {
            return Err(format!("managed {label} is a reparse point or not a file"));
        }
        let bytes = fs::read(path).map_err(|_| format!("read managed {label} failed"))?;
        if bytes.len() as u64 != expected_bytes
            || format!("{:x}", Sha256::digest(&bytes)) != expected_sha256
        {
            return Err(format!("managed {label} failed content verification"));
        }
        Ok(bytes)
    }
}

fn finish_summary_transaction<T>(
    store: &StationStore,
    outcome: Result<T, String>,
) -> Result<T, String> {
    match outcome {
        Ok(value) => match store.execute("COMMIT") {
            Ok(()) => Ok(value),
            Err(error) => {
                let _ = store.execute("ROLLBACK");
                Err(error)
            }
        },
        Err(error) => {
            let _ = store.execute("ROLLBACK");
            Err(error)
        }
    }
}
fn canonical_summary_hash<T: serde::Serialize>(value: &T) -> Result<String, String> {
    let json = serde_json::to_vec(value)
        .map_err(|error| format!("serialize summary authority value: {error}"))?;
    Ok(format!("{:x}", Sha256::digest(json)))
}
pub fn cloud_summary_authority_digest(
    configuration_hash: &str,
    credential_version: u64,
    credential_configured: bool,
) -> Result<String, String> {
    validate_summary_sha(configuration_hash, "summary configuration hash")?;
    if credential_version == 0 {
        return Err("summary credential version is invalid".into());
    }
    canonical_summary_hash(&(
        configuration_hash,
        credential_version,
        credential_configured,
    ))
}
fn validate_summary_uuid(value: &str, field: &str) -> Result<(), String> {
    if !is_canonical_uuid(value) {
        Err(format!("{field} is invalid"))
    } else {
        Ok(())
    }
}
fn validate_summary_sha(value: &str, field: &str) -> Result<(), String> {
    if value.len() != 64
        || !value.bytes().all(|b| b.is_ascii_hexdigit())
        || value.bytes().any(|b| b.is_ascii_uppercase())
    {
        Err(format!("{field} is invalid"))
    } else {
        Ok(())
    }
}
fn validate_summary_actor(value: &str) -> Result<(), String> {
    if !secret_field(value, 200) {
        Err("summary actor is invalid".into())
    } else {
        Ok(())
    }
}
fn valid_summary_media_type(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 120
        && value.trim() == value
        && !value.chars().any(char::is_control)
}
fn validate_summary_language(value: Option<&str>) -> Result<(), String> {
    if value.is_some_and(|v| {
        v.is_empty() || v.len() > 35 || v.trim() != v || v.chars().any(char::is_control)
    }) {
        Err("summary language is invalid".into())
    } else {
        Ok(())
    }
}
fn validate_summary_text(value: &str, field: &str) -> Result<(), String> {
    if value
        .chars()
        .any(|c| c.is_control() && !matches!(c, '\n' | '\r' | '\t'))
    {
        Err(format!("{field} contains disallowed control characters"))
    } else {
        Ok(())
    }
}
fn validate_summary_job_request(value: &AutomationSummaryJobRequest) -> Result<(), String> {
    validate_summary_uuid(&value.job_id, "summary job identity")?;
    validate_summary_sha(&value.transcript_sha256, "summary transcript hash")?;
    validate_summary_uuid(&value.config_id, "summary config identity")?;
    validate_summary_sha(&value.configuration_hash, "summary config hash")?;
    validate_summary_sha(&value.authority_digest, "summary authority digest")?;
    if value.transcript_bytes == 0
        || value.config_revision == 0
        || value.credential_version == 0
        || value.requested_at_unix_ms < 0
    {
        return Err("summary job request is invalid".into());
    }
    validate_summary_actor(&value.requested_by)
}
fn parse_summary_job_state(value: &str) -> Result<AutomationSummaryJobState, String> {
    match value {
        "pending" => Ok(AutomationSummaryJobState::Pending),
        "claimed" => Ok(AutomationSummaryJobState::Claimed),
        "completed" => Ok(AutomationSummaryJobState::Completed),
        "failed" => Ok(AutomationSummaryJobState::Failed),
        _ => Err("summary job state is invalid".into()),
    }
}
fn validate_summary_job_state(value: &AutomationSummaryJobRecord) -> Result<(), String> {
    match value.state {
        AutomationSummaryJobState::Pending => {
            if value.lease_owner.is_some() || value.output.is_some() {
                Err("pending summary job state is inconsistent".into())
            } else {
                Ok(())
            }
        }
        AutomationSummaryJobState::Claimed => {
            if value.lease_owner.is_none()
                || value.claim_id.is_none()
                || value.lease_expires_at_unix_ms.is_none()
            {
                Err("claimed summary job state is inconsistent".into())
            } else {
                Ok(())
            }
        }
        AutomationSummaryJobState::Completed => {
            if value.output.is_none() || value.completed_at_unix_ms.is_none() {
                Err("completed summary job state is inconsistent".into())
            } else {
                Ok(())
            }
        }
        AutomationSummaryJobState::Failed => Ok(()),
    }
}
fn new_summary_uuid() -> Result<String, String> {
    let mut b = [0_u8; 16];
    getrandom::fill(&mut b).map_err(|_| "summary identity entropy is unavailable".to_string())?;
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    Ok(format!(
        "{:02x}{:02x}{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}",
        b[0],
        b[1],
        b[2],
        b[3],
        b[4],
        b[5],
        b[6],
        b[7],
        b[8],
        b[9],
        b[10],
        b[11],
        b[12],
        b[13],
        b[14],
        b[15]
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_store() -> (StationStore, PathBuf) {
        let path = std::env::temp_dir().join(format!(
            "townlight-summary-authority-{}.db",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        (StationStore::open(&path).unwrap(), path)
    }

    fn config() -> CloudSummaryProviderConfig {
        CloudSummaryProviderConfig {
            config_id: "11111111-2222-4333-8444-555555555555".into(),
            revision: 1,
            endpoint: "https://api.example.test/v1".into(),
            model: "summary-model".into(),
            prompt_revision: 1,
            max_input_bytes: 1_000_000,
            max_output_tokens: 1_024,
            temperature_milli: 200,
            external_processing_acknowledged: true,
        }
    }

    #[test]
    fn config_cas_and_machine_credential_are_distinct_and_redacted() {
        let (store, path) = temp_store();
        let hash = store
            .write_cloud_summary_provider_config(&config(), None, "setup-admin", 10)
            .unwrap();
        assert!(
            !store
                .cloud_summary_credential_status(&config().config_id, 1)
                .unwrap()
                .configured
        );
        assert!(
            store
                .write_cloud_summary_provider_config(&config(), None, "setup-admin", 11)
                .is_err()
        );
        assert_eq!(
            store
                .provision_cloud_summary_credential(
                    &config().config_id,
                    1,
                    b"provider-secret",
                    "setup-admin",
                    12,
                )
                .unwrap(),
            1
        );
        let status = store
            .cloud_summary_credential_status(&config().config_id, 1)
            .unwrap();
        assert!(status.configured);
        assert_eq!(status.credential_version, 1);
        assert_eq!(
            store
                .read_cloud_summary_provider_config(&config().config_id)
                .unwrap()
                .unwrap()
                .1,
            hash
        );
        drop(store);
        let _ = fs::remove_file(path);
    }

    #[test]
    fn transcript_and_summary_job_are_content_addressed_and_immutable() {
        let (store, path) = temp_store();
        let config_hash = store
            .write_cloud_summary_provider_config(&config(), None, "setup-admin", 10)
            .unwrap();
        store
            .provision_cloud_summary_credential(
                &config().config_id,
                1,
                b"provider-secret",
                "setup-admin",
                11,
            )
            .unwrap();
        let transcript = store
            .import_automation_summary_transcript(
                b"Meeting transcript\nwith public testimony.",
                "text/plain; charset=utf-8",
                Some("en"),
                "setup-admin",
                12,
            )
            .unwrap();
        assert_eq!(
            store
                .read_automation_summary_transcript(&transcript.sha256)
                .unwrap(),
            b"Meeting transcript\nwith public testimony."
        );
        let request = AutomationSummaryJobRequest {
            job_id: "22222222-3333-4333-8444-555555555555".into(),
            transcript_sha256: transcript.sha256,
            transcript_bytes: transcript.bytes,
            config_id: config().config_id,
            config_revision: 1,
            configuration_hash: config_hash.clone(),
            credential_version: 1,
            authority_digest: cloud_summary_authority_digest(&config_hash, 1, true).unwrap(),
            requested_by: "setup-admin".into(),
            requested_at_unix_ms: 20,
        };
        let pending = store.enqueue_automation_summary_job(&request).unwrap();
        assert_eq!(pending.state, AutomationSummaryJobState::Pending);
        assert_eq!(
            store.enqueue_automation_summary_job(&request).unwrap(),
            pending
        );
        let claimed = store
            .claim_automation_summary_job("summary-worker", 21)
            .unwrap()
            .unwrap();
        let claim_id = claimed.claim_id.clone().unwrap();
        let completed = store
            .complete_automation_summary_job(
                &request.job_id,
                "summary-worker",
                &claim_id,
                &AutomationSummaryCompletion {
                    output: b"A concise public summary.",
                    provider_request_id_sha256: &"a".repeat(64),
                    provider_response_sha256: &"b".repeat(64),
                    completed_at_unix_ms: 22,
                },
            )
            .unwrap();
        assert_eq!(completed.state, AutomationSummaryJobState::Completed);
        assert_eq!(completed.output.unwrap().word_count, 4);
        assert!(
            store
                .complete_automation_summary_job(
                    &request.job_id,
                    "summary-worker",
                    &claim_id,
                    &AutomationSummaryCompletion {
                        output: b"replay",
                        provider_request_id_sha256: &"a".repeat(64),
                        provider_response_sha256: &"b".repeat(64),
                        completed_at_unix_ms: 23,
                    },
                )
                .is_err()
        );
        drop(store);
        let _ = fs::remove_file(path);
    }

    #[test]
    fn transcript_rejects_non_utf8_controls_and_oversize() {
        let (store, path) = temp_store();
        assert!(
            store
                .import_automation_summary_transcript(b"\xff", "text/plain", None, "operator", 1)
                .is_err()
        );
        assert!(
            store
                .import_automation_summary_transcript(b"bad\x01", "text/plain", None, "operator", 1)
                .is_err()
        );
        assert!(
            store
                .import_automation_summary_transcript(
                    &vec![b'x'; MAX_TRANSCRIPT_BYTES + 1],
                    "text/plain",
                    None,
                    "operator",
                    1
                )
                .is_err()
        );
        drop(store);
        let _ = fs::remove_file(path);
    }
}
