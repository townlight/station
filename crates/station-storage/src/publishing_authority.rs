use super::*;
use station_publishing::{
    PublicationReadiness, PublishCandidateIdentity, PublishConnectionTestEvidence,
    PublishTargetAccount, PublishTargetKind, PublishTargetProfile, PublishTargetStatus,
    derive_publication_readiness, derive_publish_target_status,
};

const MAX_PUBLISHING_RECORDS: usize = 64;
const MAX_PUBLISHING_EVIDENCE: usize = 128;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PublishingAuthorityWriteError {
    Invalid(String),
    Conflict { current_revision: Option<u64> },
    Rebound,
    Storage(String),
}

impl From<String> for PublishingAuthorityWriteError {
    fn from(value: String) -> Self {
        Self::Storage(value)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PublishTargetAccountHistory {
    pub account: PublishTargetAccount,
    pub changed_by: String,
    pub occurred_at_unix_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PublishTargetProfileHistory {
    pub target: PublishTargetProfile,
    pub changed_by: String,
    pub occurred_at_unix_ms: i64,
}

impl StationStore {
    pub(super) fn initialize_publishing_authority_schema(&self) -> Result<(), String> {
        self.execute(
            "CREATE TABLE IF NOT EXISTS publish_target_accounts (\
               account_id TEXT PRIMARY KEY,\
               kind TEXT NOT NULL CHECK (kind IN ('portal','internet_archive','local_nas','youtube_live','syndication','podcast')),\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               record_json TEXT NOT NULL\
             );\
             CREATE TABLE IF NOT EXISTS publish_target_profiles (\
               target_id TEXT PRIMARY KEY,\
               account_id TEXT NOT NULL,\
               kind TEXT NOT NULL CHECK (kind IN ('portal','internet_archive','local_nas','youtube_live','syndication','podcast')),\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               configuration_hash TEXT NOT NULL CHECK (length(configuration_hash)=64),\
               record_json TEXT NOT NULL,\
               FOREIGN KEY(account_id) REFERENCES publish_target_accounts(account_id)\
             );\
             CREATE INDEX IF NOT EXISTS publish_target_profiles_account \
               ON publish_target_profiles(account_id, target_id);\
             CREATE TABLE IF NOT EXISTS publish_target_account_history (\
               account_id TEXT NOT NULL,\
               kind TEXT NOT NULL CHECK (kind IN ('portal','internet_archive','local_nas','youtube_live','syndication','podcast')),\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               record_json TEXT NOT NULL,\
               changed_by TEXT NOT NULL CHECK (length(changed_by) BETWEEN 1 AND 200 AND changed_by=trim(changed_by)),\
               occurred_at_unix_ms INTEGER NOT NULL CHECK (occurred_at_unix_ms >= 0),\
               PRIMARY KEY(account_id, revision)\
             );\
             CREATE TABLE IF NOT EXISTS publish_target_profile_history (\
               target_id TEXT NOT NULL,\
               account_id TEXT NOT NULL,\
               kind TEXT NOT NULL CHECK (kind IN ('portal','internet_archive','local_nas','youtube_live','syndication','podcast')),\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               configuration_hash TEXT NOT NULL CHECK (length(configuration_hash)=64),\
               record_json TEXT NOT NULL,\
               changed_by TEXT NOT NULL CHECK (length(changed_by) BETWEEN 1 AND 200 AND changed_by=trim(changed_by)),\
               occurred_at_unix_ms INTEGER NOT NULL CHECK (occurred_at_unix_ms >= 0),\
               PRIMARY KEY(target_id, revision)\
             );\
             CREATE TABLE IF NOT EXISTS publish_connection_test_evidence (\
               evidence_id TEXT PRIMARY KEY,\
               target_id TEXT NOT NULL,\
               account_revision INTEGER NOT NULL CHECK (account_revision >= 1),\
               target_revision INTEGER NOT NULL CHECK (target_revision >= 1),\
               configuration_hash TEXT NOT NULL CHECK (length(configuration_hash)=64),\
               candidate_version TEXT NOT NULL,\
               source_commit TEXT NOT NULL CHECK (length(source_commit)=40),\
               tested_at_unix_ms INTEGER NOT NULL CHECK (tested_at_unix_ms >= 0),\
               expires_at_unix_ms INTEGER NOT NULL CHECK (expires_at_unix_ms > tested_at_unix_ms),\
               record_json TEXT NOT NULL,\
               FOREIGN KEY(target_id) REFERENCES publish_target_profiles(target_id)\
             );\
             CREATE INDEX IF NOT EXISTS publish_connection_test_evidence_current \
               ON publish_connection_test_evidence(target_id, candidate_version, source_commit, tested_at_unix_ms DESC, evidence_id DESC);\
             CREATE TABLE IF NOT EXISTS publish_account_credentials (\
               account_id TEXT NOT NULL,\
               account_revision INTEGER NOT NULL CHECK (account_revision >= 1),\
               credential_version INTEGER NOT NULL CHECK (credential_version >= 1),\
               state TEXT NOT NULL CHECK (state IN ('active','deleted')),\
               protected_secret BLOB NOT NULL,\
               changed_by TEXT NOT NULL CHECK (length(changed_by) BETWEEN 1 AND 200 AND changed_by=trim(changed_by)),\
               occurred_at_unix_ms INTEGER NOT NULL CHECK (occurred_at_unix_ms >= 0),\
               PRIMARY KEY(account_id,credential_version),\
               FOREIGN KEY(account_id) REFERENCES publish_target_accounts(account_id)\
             );\
             CREATE INDEX IF NOT EXISTS publish_account_credentials_current \
               ON publish_account_credentials(account_id,credential_version DESC);\
             CREATE TABLE IF NOT EXISTS publish_authority_epoch (\
               singleton INTEGER PRIMARY KEY CHECK (singleton=1),\
               revision INTEGER NOT NULL CHECK (revision >= 0)\
             );\
             INSERT OR IGNORE INTO publish_authority_epoch(singleton,revision) VALUES(1,0);\
             CREATE TRIGGER IF NOT EXISTS publish_target_account_history_no_update \
               BEFORE UPDATE ON publish_target_account_history BEGIN SELECT RAISE(ABORT, 'publish target account history is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS publish_target_account_history_no_delete \
               BEFORE DELETE ON publish_target_account_history BEGIN SELECT RAISE(ABORT, 'publish target account history is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS publish_target_profile_history_no_update \
               BEFORE UPDATE ON publish_target_profile_history BEGIN SELECT RAISE(ABORT, 'publish target profile history is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS publish_target_profile_history_no_delete \
               BEFORE DELETE ON publish_target_profile_history BEGIN SELECT RAISE(ABORT, 'publish target profile history is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS publish_connection_test_evidence_no_update \
               BEFORE UPDATE ON publish_connection_test_evidence BEGIN SELECT RAISE(ABORT, 'publish connection evidence is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS publish_connection_test_evidence_no_delete \
               BEFORE DELETE ON publish_connection_test_evidence BEGIN SELECT RAISE(ABORT, 'publish connection evidence is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS publish_account_credentials_no_update \
               BEFORE UPDATE ON publish_account_credentials BEGIN SELECT RAISE(ABORT, 'publish credential audit is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS publish_account_credentials_no_delete \
               BEFORE DELETE ON publish_account_credentials BEGIN SELECT RAISE(ABORT, 'publish credential audit is append-only'); END;",
        )
    }

    pub fn read_publish_target_account(
        &self,
        account_id: &str,
    ) -> Result<Option<PublishTargetAccount>, String> {
        validate_publishing_id(account_id, "publish target account")?;
        let statement = self.prepare("SELECT account_id, kind, revision, record_json FROM publish_target_accounts WHERE account_id=?1")?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(account_id, "publish target account")?,
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let mut account = read_publish_account(statement.raw)?;
                self.validate_publish_account_snapshot(&account)?;
                account.credential_configured = self
                    .current_publish_credential_version(&account.account_id, account.revision)?
                    .is_some();
                Ok(Some(account))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_publish_target_accounts(&self) -> Result<Vec<PublishTargetAccount>, String> {
        let statement = self.prepare("SELECT account_id, kind, revision, record_json FROM publish_target_accounts ORDER BY account_id LIMIT 65")?;
        let mut accounts = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    let mut account = read_publish_account(statement.raw)?;
                    self.validate_publish_account_snapshot(&account)?;
                    account.credential_configured = self
                        .current_publish_credential_version(&account.account_id, account.revision)?
                        .is_some();
                    accounts.push(account);
                }
                SQLITE_DONE if accounts.len() <= MAX_PUBLISHING_RECORDS => break,
                SQLITE_DONE => {
                    return Err("publish target account authority exceeds 64 records".into());
                }
                _ => return Err(sqlite_error(self.database)),
            }
        }
        Ok(accounts)
    }

    pub fn provision_publish_account_credential(
        &self,
        account_id: &str,
        expected_account_revision: u64,
        secret: &[u8],
        changed_by: &str,
        occurred_at_unix_ms: i64,
    ) -> Result<u64, String> {
        validate_publishing_id(account_id, "publish target account")?;
        if !secret_field(changed_by, 200) || occurred_at_unix_ms < 0 {
            return Err("publish credential attribution is invalid".into());
        }
        if secret.len() < 8 || secret.len() > 16_384 || secret.iter().any(|byte| *byte == 0) {
            return Err("publish provider credential is empty, oversized, or invalid".into());
        }
        let protected = protect_local_machine(secret)?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let account = self
                .read_publish_target_account(account_id)?
                .ok_or_else(|| "publish target account is missing".to_string())?;
            if account.revision != expected_account_revision {
                return Err("publish target account changed before credential provisioning".into());
            }
            let version = self.next_publish_credential_version(account_id)?;
            let statement = self.prepare("INSERT INTO publish_account_credentials(account_id,account_revision,credential_version,state,protected_secret,changed_by,occurred_at_unix_ms) VALUES(?1,?2,?3,'active',?4,?5,?6)")?;
            bind_text(
                self.database,
                statement.raw,
                1,
                &c_string(account_id, "publish account")?,
            )?;
            bind_i64(
                self.database,
                statement.raw,
                2,
                checked_i64(expected_account_revision, "publish account revision")?,
            )?;
            bind_i64(
                self.database,
                statement.raw,
                3,
                checked_i64(version, "publish credential version")?,
            )?;
            bind_blob(self.database, statement.raw, 4, &protected)?;
            bind_text(
                self.database,
                statement.raw,
                5,
                &c_string(changed_by, "publish credential actor")?,
            )?;
            bind_i64(self.database, statement.raw, 6, occurred_at_unix_ms)?;
            expect_done(self.database, statement.raw)?;
            advance_publish_authority_epoch(self)?;
            Ok(version)
        })();
        self.finish_value_transaction(outcome)
    }

    pub fn delete_publish_account_credential(
        &self,
        account_id: &str,
        expected_account_revision: u64,
        changed_by: &str,
        occurred_at_unix_ms: i64,
    ) -> Result<u64, String> {
        validate_publishing_id(account_id, "publish target account")?;
        if !secret_field(changed_by, 200) || occurred_at_unix_ms < 0 {
            return Err("publish credential attribution is invalid".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let account = self
                .read_publish_target_account(account_id)?
                .ok_or_else(|| "publish target account is missing".to_string())?;
            if account.revision != expected_account_revision {
                return Err("publish target account changed before credential deletion".into());
            }
            if self
                .current_publish_credential_version(account_id, expected_account_revision)?
                .is_none()
            {
                return Err("publish provider credential is not configured".into());
            }
            let version = self.next_publish_credential_version(account_id)?;
            let statement = self.prepare("INSERT INTO publish_account_credentials(account_id,account_revision,credential_version,state,protected_secret,changed_by,occurred_at_unix_ms) VALUES(?1,?2,?3,'deleted',x'',?4,?5)")?;
            bind_text(
                self.database,
                statement.raw,
                1,
                &c_string(account_id, "publish account")?,
            )?;
            bind_i64(
                self.database,
                statement.raw,
                2,
                checked_i64(expected_account_revision, "publish account revision")?,
            )?;
            bind_i64(
                self.database,
                statement.raw,
                3,
                checked_i64(version, "publish credential version")?,
            )?;
            bind_text(
                self.database,
                statement.raw,
                4,
                &c_string(changed_by, "publish credential actor")?,
            )?;
            bind_i64(self.database, statement.raw, 5, occurred_at_unix_ms)?;
            expect_done(self.database, statement.raw)?;
            advance_publish_authority_epoch(self)?;
            Ok(version)
        })();
        self.finish_value_transaction(outcome)
    }

    pub(crate) fn read_current_publish_account_credential(
        &self,
        account_id: &str,
        account_revision: u64,
    ) -> Result<Option<(u64, Vec<u8>)>, String> {
        let statement = self.prepare("SELECT account_revision,credential_version,state,protected_secret FROM publish_account_credentials WHERE account_id=?1 ORDER BY credential_version DESC LIMIT 1")?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(account_id, "publish account")?,
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let bound_revision =
                    column_u64(statement.raw, 0, "publish credential account revision")?;
                let version = column_u64(statement.raw, 1, "publish credential version")?;
                let state = column_text(statement.raw, 2)?;
                if state == "deleted" || bound_revision != account_revision {
                    return Ok(None);
                }
                if state != "active" {
                    return Err("publish credential state is invalid".into());
                }
                Ok(Some((
                    version,
                    unprotect_local_machine(&column_blob(statement.raw, 3)?)?,
                )))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn current_publish_credential_version(
        &self,
        account_id: &str,
        account_revision: u64,
    ) -> Result<Option<u64>, String> {
        Ok(self
            .read_current_publish_account_credential(account_id, account_revision)?
            .map(|(version, mut secret)| {
                secret.fill(0);
                version
            }))
    }

    fn next_publish_credential_version(&self, account_id: &str) -> Result<u64, String> {
        let statement = self.prepare("SELECT COALESCE(MAX(credential_version),0) FROM publish_account_credentials WHERE account_id=?1")?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(account_id, "publish account")?,
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => column_u64(statement.raw, 0, "publish credential version")?
                .checked_add(1)
                .ok_or_else(|| "publish credential version overflowed".into()),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_publish_target_account_history(
        &self,
        account_id: &str,
        limit: usize,
    ) -> Result<Vec<PublishTargetAccountHistory>, String> {
        validate_publishing_id(account_id, "publish target account")?;
        validate_publishing_limit(limit, "publish target account history")?;
        let statement = self.prepare("SELECT account_id, kind, revision, record_json, changed_by, occurred_at_unix_ms FROM publish_target_account_history WHERE account_id=?1 ORDER BY revision DESC LIMIT ?2")?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(account_id, "publish target account")?,
        )?;
        bind_i64(self.database, statement.raw, 2, limit as i64)?;
        let mut entries = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => entries.push(read_publish_account_history(statement.raw)?),
                SQLITE_DONE => return Ok(entries),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn write_publish_target_account(
        &self,
        account: &PublishTargetAccount,
        expected_revision: Option<u64>,
        changed_by: &str,
        occurred_at_unix_ms: i64,
    ) -> Result<(), PublishingAuthorityWriteError> {
        account
            .validate()
            .map_err(PublishingAuthorityWriteError::Invalid)?;
        validate_publishing_attribution(changed_by, occurred_at_unix_ms)?;
        self.execute("BEGIN IMMEDIATE")
            .map_err(PublishingAuthorityWriteError::Storage)?;
        let outcome = (|| {
            let current = self
                .read_publish_target_account(&account.account_id)
                .map_err(PublishingAuthorityWriteError::Storage)?;
            validate_publishing_cas(
                current.as_ref().map(|item| item.revision),
                expected_revision,
                account.revision,
            )?;
            if current
                .as_ref()
                .is_some_and(|item| item.kind != account.kind)
            {
                return Err(PublishingAuthorityWriteError::Rebound);
            }
            let record_json = serde_json::to_string(account).map_err(|error| {
                PublishingAuthorityWriteError::Storage(format!(
                    "serialize publish target account: {error}"
                ))
            })?;
            let statement = self.prepare("INSERT INTO publish_target_accounts(account_id,kind,revision,record_json) VALUES(?1,?2,?3,?4) ON CONFLICT(account_id) DO UPDATE SET kind=excluded.kind,revision=excluded.revision,record_json=excluded.record_json").map_err(PublishingAuthorityWriteError::Storage)?;
            bind_text(
                self.database,
                statement.raw,
                1,
                &c_string(&account.account_id, "publish target account")?,
            )
            .map_err(PublishingAuthorityWriteError::Storage)?;
            bind_text(
                self.database,
                statement.raw,
                2,
                &c_string(publish_kind_name(account.kind), "publish target kind")?,
            )
            .map_err(PublishingAuthorityWriteError::Storage)?;
            bind_i64(
                self.database,
                statement.raw,
                3,
                checked_publish_revision(account.revision)?,
            )
            .map_err(PublishingAuthorityWriteError::Storage)?;
            bind_text(
                self.database,
                statement.raw,
                4,
                &c_string(&record_json, "publish target account record")?,
            )
            .map_err(PublishingAuthorityWriteError::Storage)?;
            expect_done(self.database, statement.raw)
                .map_err(PublishingAuthorityWriteError::Storage)?;
            let history = self.prepare("INSERT INTO publish_target_account_history(account_id,kind,revision,record_json,changed_by,occurred_at_unix_ms) VALUES(?1,?2,?3,?4,?5,?6)").map_err(PublishingAuthorityWriteError::Storage)?;
            bind_text(
                self.database,
                history.raw,
                1,
                &c_string(&account.account_id, "publish target account")?,
            )
            .map_err(PublishingAuthorityWriteError::Storage)?;
            bind_text(
                self.database,
                history.raw,
                2,
                &c_string(publish_kind_name(account.kind), "publish target kind")?,
            )
            .map_err(PublishingAuthorityWriteError::Storage)?;
            bind_i64(
                self.database,
                history.raw,
                3,
                checked_publish_revision(account.revision)?,
            )
            .map_err(PublishingAuthorityWriteError::Storage)?;
            bind_text(
                self.database,
                history.raw,
                4,
                &c_string(&record_json, "publish target account record")?,
            )
            .map_err(PublishingAuthorityWriteError::Storage)?;
            bind_text(
                self.database,
                history.raw,
                5,
                &c_string(changed_by, "publish target account actor")?,
            )
            .map_err(PublishingAuthorityWriteError::Storage)?;
            bind_i64(self.database, history.raw, 6, occurred_at_unix_ms)
                .map_err(PublishingAuthorityWriteError::Storage)?;
            expect_done(self.database, history.raw)
                .map_err(PublishingAuthorityWriteError::Storage)?;
            advance_publish_authority_epoch(self).map_err(PublishingAuthorityWriteError::Storage)
        })();
        finish_publishing_write(self, outcome)
    }

    pub fn read_publish_target_profile(
        &self,
        target_id: &str,
    ) -> Result<Option<PublishTargetProfile>, String> {
        validate_publishing_id(target_id, "publish target")?;
        let statement = self.prepare("SELECT target_id, account_id, kind, revision, configuration_hash, record_json FROM publish_target_profiles WHERE target_id=?1")?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(target_id, "publish target")?,
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let target = read_publish_target(statement.raw)?;
                self.validate_publish_target_snapshot(&target)?;
                Ok(Some(target))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_publish_target_profiles(&self) -> Result<Vec<PublishTargetProfile>, String> {
        let statement = self.prepare("SELECT target_id, account_id, kind, revision, configuration_hash, record_json FROM publish_target_profiles ORDER BY target_id LIMIT 65")?;
        let mut targets = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => targets.push(read_publish_target(statement.raw)?),
                SQLITE_DONE if targets.len() <= MAX_PUBLISHING_RECORDS => break,
                SQLITE_DONE => return Err("publish target authority exceeds 64 records".into()),
                _ => return Err(sqlite_error(self.database)),
            }
        }
        for target in &targets {
            self.validate_publish_target_snapshot(target)?;
        }
        Ok(targets)
    }

    pub fn list_publish_target_profile_history(
        &self,
        target_id: &str,
        limit: usize,
    ) -> Result<Vec<PublishTargetProfileHistory>, String> {
        validate_publishing_id(target_id, "publish target")?;
        validate_publishing_limit(limit, "publish target history")?;
        let statement = self.prepare("SELECT target_id, account_id, kind, revision, configuration_hash, record_json, changed_by, occurred_at_unix_ms FROM publish_target_profile_history WHERE target_id=?1 ORDER BY revision DESC LIMIT ?2")?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(target_id, "publish target")?,
        )?;
        bind_i64(self.database, statement.raw, 2, limit as i64)?;
        let mut entries = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => entries.push(read_publish_target_history(statement.raw)?),
                SQLITE_DONE => return Ok(entries),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn write_publish_target_profile(
        &self,
        target: &PublishTargetProfile,
        expected_revision: Option<u64>,
        changed_by: &str,
        occurred_at_unix_ms: i64,
    ) -> Result<(), PublishingAuthorityWriteError> {
        target
            .validate()
            .map_err(PublishingAuthorityWriteError::Invalid)?;
        validate_publishing_attribution(changed_by, occurred_at_unix_ms)?;
        self.execute("BEGIN IMMEDIATE")
            .map_err(PublishingAuthorityWriteError::Storage)?;
        let outcome = (|| {
            let account = self
                .read_publish_target_account(&target.account_id)
                .map_err(PublishingAuthorityWriteError::Storage)?
                .ok_or_else(|| {
                    PublishingAuthorityWriteError::Invalid(
                        "publish target account does not exist".into(),
                    )
                })?;
            if account.kind != target.kind {
                return Err(PublishingAuthorityWriteError::Invalid(
                    "publish target account kind does not match target kind".into(),
                ));
            }
            let current = self
                .read_publish_target_profile(&target.target_id)
                .map_err(PublishingAuthorityWriteError::Storage)?;
            validate_publishing_cas(
                current.as_ref().map(|item| item.revision),
                expected_revision,
                target.revision,
            )?;
            if current.as_ref().is_some_and(|item| {
                item.account_id != target.account_id || item.kind != target.kind
            }) {
                return Err(PublishingAuthorityWriteError::Rebound);
            }
            let record_json = serde_json::to_string(target).map_err(|error| {
                PublishingAuthorityWriteError::Storage(format!("serialize publish target: {error}"))
            })?;
            let statement = self.prepare("INSERT INTO publish_target_profiles(target_id,account_id,kind,revision,configuration_hash,record_json) VALUES(?1,?2,?3,?4,?5,?6) ON CONFLICT(target_id) DO UPDATE SET account_id=excluded.account_id,kind=excluded.kind,revision=excluded.revision,configuration_hash=excluded.configuration_hash,record_json=excluded.record_json").map_err(PublishingAuthorityWriteError::Storage)?;
            let values = publish_target_values(target, &record_json)?;
            bind_text(self.database, statement.raw, 1, &values[0])
                .map_err(PublishingAuthorityWriteError::Storage)?;
            bind_text(self.database, statement.raw, 2, &values[1])
                .map_err(PublishingAuthorityWriteError::Storage)?;
            bind_text(self.database, statement.raw, 3, &values[2])
                .map_err(PublishingAuthorityWriteError::Storage)?;
            bind_i64(
                self.database,
                statement.raw,
                4,
                checked_publish_revision(target.revision)?,
            )
            .map_err(PublishingAuthorityWriteError::Storage)?;
            bind_text(self.database, statement.raw, 5, &values[3])
                .map_err(PublishingAuthorityWriteError::Storage)?;
            bind_text(self.database, statement.raw, 6, &values[4])
                .map_err(PublishingAuthorityWriteError::Storage)?;
            expect_done(self.database, statement.raw)
                .map_err(PublishingAuthorityWriteError::Storage)?;
            let history = self.prepare("INSERT INTO publish_target_profile_history(target_id,account_id,kind,revision,configuration_hash,record_json,changed_by,occurred_at_unix_ms) VALUES(?1,?2,?3,?4,?5,?6,?7,?8)").map_err(PublishingAuthorityWriteError::Storage)?;
            bind_text(self.database, history.raw, 1, &values[0])
                .map_err(PublishingAuthorityWriteError::Storage)?;
            bind_text(self.database, history.raw, 2, &values[1])
                .map_err(PublishingAuthorityWriteError::Storage)?;
            bind_text(self.database, history.raw, 3, &values[2])
                .map_err(PublishingAuthorityWriteError::Storage)?;
            bind_i64(
                self.database,
                history.raw,
                4,
                checked_publish_revision(target.revision)?,
            )
            .map_err(PublishingAuthorityWriteError::Storage)?;
            bind_text(self.database, history.raw, 5, &values[3])
                .map_err(PublishingAuthorityWriteError::Storage)?;
            bind_text(self.database, history.raw, 6, &values[4])
                .map_err(PublishingAuthorityWriteError::Storage)?;
            bind_text(
                self.database,
                history.raw,
                7,
                &c_string(changed_by, "publish target actor")?,
            )
            .map_err(PublishingAuthorityWriteError::Storage)?;
            bind_i64(self.database, history.raw, 8, occurred_at_unix_ms)
                .map_err(PublishingAuthorityWriteError::Storage)?;
            expect_done(self.database, history.raw)
                .map_err(PublishingAuthorityWriteError::Storage)?;
            advance_publish_authority_epoch(self).map_err(PublishingAuthorityWriteError::Storage)
        })();
        finish_publishing_write(self, outcome)
    }

    pub fn append_publish_connection_test_evidence(
        &self,
        evidence: &PublishConnectionTestEvidence,
        current_candidate: &PublishCandidateIdentity,
        now_unix_ms: i64,
    ) -> Result<(), String> {
        evidence.validate()?;
        current_candidate.validate()?;
        if now_unix_ms < 0 || evidence.candidate != *current_candidate {
            return Err("publish connection evidence candidate binding is stale or invalid".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let target = self
                .read_publish_target_profile(&evidence.target_id)?
                .ok_or_else(|| "publish connection evidence target does not exist".to_string())?;
            let account = self
                .read_publish_target_account(&target.account_id)?
                .ok_or_else(|| "publish connection evidence account does not exist".to_string())?;
            if !matches!(
                derive_publish_target_status(
                    &account,
                    &target,
                    Some(evidence),
                    current_candidate,
                    now_unix_ms
                ),
                PublishTargetStatus::Passed | PublishTargetStatus::Failed
            ) {
                return Err("publish connection evidence is not current for account, target, configuration, candidate, or time".into());
            }
            let record_json = serde_json::to_string(evidence)
                .map_err(|error| format!("serialize publish connection evidence: {error}"))?;
            let statement = self.prepare("INSERT INTO publish_connection_test_evidence(evidence_id,target_id,account_revision,target_revision,configuration_hash,candidate_version,source_commit,tested_at_unix_ms,expires_at_unix_ms,record_json) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)")?;
            let values = [
                c_string(&evidence.evidence_id, "publish evidence identity")?,
                c_string(&evidence.target_id, "publish target")?,
                c_string(&evidence.configuration_hash, "publish configuration hash")?,
                c_string(&evidence.candidate.version, "publish candidate version")?,
                c_string(
                    &evidence.candidate.source_commit,
                    "publish candidate source commit",
                )?,
                c_string(&record_json, "publish connection evidence record")?,
            ];
            bind_text(self.database, statement.raw, 1, &values[0])?;
            bind_text(self.database, statement.raw, 2, &values[1])?;
            bind_i64(
                self.database,
                statement.raw,
                3,
                checked_publish_revision(evidence.account_revision)?,
            )?;
            bind_i64(
                self.database,
                statement.raw,
                4,
                checked_publish_revision(evidence.target_revision)?,
            )?;
            bind_text(self.database, statement.raw, 5, &values[2])?;
            bind_text(self.database, statement.raw, 6, &values[3])?;
            bind_text(self.database, statement.raw, 7, &values[4])?;
            bind_i64(self.database, statement.raw, 8, evidence.tested_at_unix_ms)?;
            bind_i64(self.database, statement.raw, 9, evidence.expires_at_unix_ms)?;
            bind_text(self.database, statement.raw, 10, &values[5])?;
            expect_done(self.database, statement.raw)?;
            advance_publish_authority_epoch(self)
        })();
        self.finish_transaction(outcome)
    }

    pub fn list_publish_connection_test_evidence(
        &self,
        target_id: &str,
        limit: usize,
    ) -> Result<Vec<PublishConnectionTestEvidence>, String> {
        validate_publishing_id(target_id, "publish target")?;
        validate_publishing_limit(limit, "publish connection evidence")?;
        let statement = self.prepare("SELECT evidence_id,target_id,account_revision,target_revision,configuration_hash,candidate_version,source_commit,tested_at_unix_ms,expires_at_unix_ms,record_json FROM publish_connection_test_evidence WHERE target_id=?1 ORDER BY tested_at_unix_ms DESC,evidence_id DESC LIMIT ?2")?;
        bind_text(
            self.database,
            statement.raw,
            1,
            &c_string(target_id, "publish target")?,
        )?;
        bind_i64(self.database, statement.raw, 2, limit as i64)?;
        let mut evidence = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => evidence.push(read_publish_evidence(statement.raw)?),
                SQLITE_DONE => return Ok(evidence),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn derive_current_publication_readiness(
        &self,
        current_candidate: &PublishCandidateIdentity,
        now_unix_ms: i64,
    ) -> Result<PublicationReadiness, String> {
        let epoch_before = self.read_publish_authority_epoch()?;
        let accounts = self.list_publish_target_accounts()?;
        let targets = self.list_publish_target_profiles()?;
        let mut evidence = Vec::with_capacity(targets.len());
        for target in &targets {
            evidence.extend(self.list_publish_connection_test_evidence(&target.target_id, 1)?);
        }
        let epoch_after = self.read_publish_authority_epoch()?;
        ensure_publish_authority_epoch_stable(epoch_before, epoch_after)?;
        derive_publication_readiness(
            &accounts,
            &targets,
            &evidence,
            current_candidate,
            now_unix_ms,
        )
    }

    fn read_publish_authority_epoch(&self) -> Result<u64, String> {
        let statement =
            self.prepare("SELECT revision FROM publish_authority_epoch WHERE singleton=1")?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => column_u64(statement.raw, 0, "publish authority epoch"),
            SQLITE_DONE => Err("publish authority epoch is missing".into()),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn validate_publish_account_snapshot(
        &self,
        account: &PublishTargetAccount,
    ) -> Result<(), String> {
        let history = self.list_publish_target_account_history(&account.account_id, 1)?;
        match history.first() {
            Some(latest) if latest.account == *account => Ok(()),
            _ => Err(
                "current publish target account disagrees with its latest historical snapshot"
                    .into(),
            ),
        }
    }

    fn validate_publish_target_snapshot(
        &self,
        target: &PublishTargetProfile,
    ) -> Result<(), String> {
        let history = self.list_publish_target_profile_history(&target.target_id, 1)?;
        match history.first() {
            Some(latest) if latest.target == *target => Ok(()),
            _ => Err("current publish target disagrees with its latest historical snapshot".into()),
        }
    }
}

fn read_publish_account(statement: *mut SqliteStatement) -> Result<PublishTargetAccount, String> {
    let account: PublishTargetAccount =
        read_publish_json(column_text(statement, 3)?, "publish target account")?;
    if account.account_id != column_text(statement, 0)?
        || publish_kind_name(account.kind) != column_text(statement, 1)?
        || account.revision != column_u64(statement, 2, "publish target account revision")?
    {
        return Err("publish target account row and record disagree".into());
    }
    account.validate()?;
    Ok(account)
}

fn read_publish_target(statement: *mut SqliteStatement) -> Result<PublishTargetProfile, String> {
    let target: PublishTargetProfile =
        read_publish_json(column_text(statement, 5)?, "publish target")?;
    if target.target_id != column_text(statement, 0)?
        || target.account_id != column_text(statement, 1)?
        || publish_kind_name(target.kind) != column_text(statement, 2)?
        || target.revision != column_u64(statement, 3, "publish target revision")?
        || target.configuration_hash != column_text(statement, 4)?
    {
        return Err("publish target row and record disagree".into());
    }
    target.validate()?;
    Ok(target)
}

fn read_publish_account_history(
    statement: *mut SqliteStatement,
) -> Result<PublishTargetAccountHistory, String> {
    let account = read_publish_account_at(statement, 0, 1, 2, 3)?;
    let changed_by = column_text(statement, 4)?;
    let occurred_at_unix_ms = unsafe { sqlite3_column_int64(statement, 5) };
    if !secret_field(&changed_by, 200) || occurred_at_unix_ms < 0 {
        return Err("publish target account history attribution is invalid".into());
    }
    Ok(PublishTargetAccountHistory {
        account,
        changed_by,
        occurred_at_unix_ms,
    })
}

fn read_publish_target_history(
    statement: *mut SqliteStatement,
) -> Result<PublishTargetProfileHistory, String> {
    let target = read_publish_target_at(statement, 0, 1, 2, 3, 4, 5)?;
    let changed_by = column_text(statement, 6)?;
    let occurred_at_unix_ms = unsafe { sqlite3_column_int64(statement, 7) };
    if !secret_field(&changed_by, 200) || occurred_at_unix_ms < 0 {
        return Err("publish target history attribution is invalid".into());
    }
    Ok(PublishTargetProfileHistory {
        target,
        changed_by,
        occurred_at_unix_ms,
    })
}

fn read_publish_account_at(
    statement: *mut SqliteStatement,
    account_index: i32,
    kind_index: i32,
    revision_index: i32,
    json_index: i32,
) -> Result<PublishTargetAccount, String> {
    let account: PublishTargetAccount = read_publish_json(
        column_text(statement, json_index)?,
        "publish target account history",
    )?;
    if account.account_id != column_text(statement, account_index)?
        || publish_kind_name(account.kind) != column_text(statement, kind_index)?
        || account.revision
            != column_u64(
                statement,
                revision_index,
                "publish target account history revision",
            )?
    {
        return Err("publish target account history row and record disagree".into());
    }
    account.validate()?;
    Ok(account)
}

fn read_publish_target_at(
    statement: *mut SqliteStatement,
    target_index: i32,
    account_index: i32,
    kind_index: i32,
    revision_index: i32,
    hash_index: i32,
    json_index: i32,
) -> Result<PublishTargetProfile, String> {
    let target: PublishTargetProfile = read_publish_json(
        column_text(statement, json_index)?,
        "publish target history",
    )?;
    if target.target_id != column_text(statement, target_index)?
        || target.account_id != column_text(statement, account_index)?
        || publish_kind_name(target.kind) != column_text(statement, kind_index)?
        || target.revision
            != column_u64(statement, revision_index, "publish target history revision")?
        || target.configuration_hash != column_text(statement, hash_index)?
    {
        return Err("publish target history row and record disagree".into());
    }
    target.validate()?;
    Ok(target)
}

fn read_publish_evidence(
    statement: *mut SqliteStatement,
) -> Result<PublishConnectionTestEvidence, String> {
    let evidence: PublishConnectionTestEvidence =
        read_publish_json(column_text(statement, 9)?, "publish connection evidence")?;
    if evidence.evidence_id != column_text(statement, 0)?
        || evidence.target_id != column_text(statement, 1)?
        || evidence.account_revision
            != column_u64(statement, 2, "publish evidence account revision")?
        || evidence.target_revision != column_u64(statement, 3, "publish evidence target revision")?
        || evidence.configuration_hash != column_text(statement, 4)?
        || evidence.candidate.version != column_text(statement, 5)?
        || evidence.candidate.source_commit != column_text(statement, 6)?
        || evidence.tested_at_unix_ms != unsafe { sqlite3_column_int64(statement, 7) }
        || evidence.expires_at_unix_ms != unsafe { sqlite3_column_int64(statement, 8) }
    {
        return Err("publish connection evidence row and record disagree".into());
    }
    evidence.validate()?;
    Ok(evidence)
}

fn publish_target_values(
    target: &PublishTargetProfile,
    record_json: &str,
) -> Result<[CString; 5], String> {
    Ok([
        c_string(&target.target_id, "publish target")?,
        c_string(&target.account_id, "publish target account")?,
        c_string(publish_kind_name(target.kind), "publish target kind")?,
        c_string(&target.configuration_hash, "publish configuration hash")?,
        c_string(record_json, "publish target record")?,
    ])
}

fn validate_publishing_id(value: &str, label: &str) -> Result<(), String> {
    if is_canonical_uuid(value) {
        Ok(())
    } else {
        Err(format!("{label} identity is invalid"))
    }
}
fn validate_publishing_limit(limit: usize, label: &str) -> Result<(), String> {
    if (1..=MAX_PUBLISHING_EVIDENCE).contains(&limit) {
        Ok(())
    } else {
        Err(format!("{label} query is invalid or unbounded"))
    }
}
fn validate_publishing_attribution(
    changed_by: &str,
    occurred_at_unix_ms: i64,
) -> Result<(), PublishingAuthorityWriteError> {
    if occurred_at_unix_ms >= 0 && secret_field(changed_by, 200) {
        Ok(())
    } else {
        Err(PublishingAuthorityWriteError::Invalid(
            "publish target change attribution is invalid".into(),
        ))
    }
}
fn checked_publish_revision(revision: u64) -> Result<i64, String> {
    if revision > i64::MAX as u64 {
        Err("publish target revision is invalid".into())
    } else {
        Ok(revision as i64)
    }
}
fn validate_publishing_cas(
    current: Option<u64>,
    expected: Option<u64>,
    incoming: u64,
) -> Result<(), PublishingAuthorityWriteError> {
    let next = current.and_then(|value| value.checked_add(1));
    if current != expected || incoming != next.unwrap_or(1) || next.is_none() && current.is_some() {
        Err(PublishingAuthorityWriteError::Conflict {
            current_revision: current,
        })
    } else {
        Ok(())
    }
}
fn finish_publishing_write(
    store: &StationStore,
    outcome: Result<(), PublishingAuthorityWriteError>,
) -> Result<(), PublishingAuthorityWriteError> {
    match outcome {
        Ok(()) => match store.execute("COMMIT") {
            Ok(()) => Ok(()),
            Err(error) => {
                let _ = store.execute("ROLLBACK");
                Err(PublishingAuthorityWriteError::Storage(error))
            }
        },
        Err(error) => {
            let _ = store.execute("ROLLBACK");
            Err(error)
        }
    }
}

fn advance_publish_authority_epoch(store: &StationStore) -> Result<(), String> {
    store.execute("UPDATE publish_authority_epoch SET revision=revision+1 WHERE singleton=1")?;
    if store.changes() == 1 {
        Ok(())
    } else {
        Err("publish authority epoch could not be advanced".into())
    }
}

fn ensure_publish_authority_epoch_stable(before: u64, after: u64) -> Result<(), String> {
    if before == after {
        Ok(())
    } else {
        Err("publish authority changed while readiness was being read".into())
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

fn read_publish_json<T>(record_json: String, record_name: &str) -> Result<T, String>
where
    T: DeserializeOwned,
{
    let record = serde_json::from_str::<T>(&record_json)
        .map_err(|error| format!("database returned invalid {record_name} JSON: {error}"))?;
    Ok(record)
}

#[cfg(test)]
mod credential_tests {
    use super::*;

    #[test]
    fn protected_publish_credential_rotates_deletes_and_rejects_tampering() {
        let path = std::env::temp_dir().join(format!(
            "townlight-publish-credential-{}.db",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let store = StationStore::open(&path).unwrap();
        let account_id = "11111111-2222-4333-8444-555555555555";
        let account = PublishTargetAccount {
            account_id: account_id.into(),
            kind: PublishTargetKind::Portal,
            label: "Credential test".into(),
            enabled: true,
            credential_configured: false,
            revision: 1,
        };
        store
            .write_publish_target_account(&account, None, "setup-admin", 1)
            .unwrap();
        assert_eq!(
            store
                .provision_publish_account_credential(
                    account_id,
                    1,
                    b"first-secret",
                    "setup-admin",
                    2
                )
                .unwrap(),
            1
        );
        assert!(
            store
                .read_publish_target_account(account_id)
                .unwrap()
                .unwrap()
                .credential_configured
        );
        assert_eq!(
            store
                .provision_publish_account_credential(
                    account_id,
                    1,
                    b"rotated-secret",
                    "setup-admin",
                    3
                )
                .unwrap(),
            2
        );
        let (version, mut secret) = store
            .read_current_publish_account_credential(account_id, 1)
            .unwrap()
            .unwrap();
        assert_eq!(version, 2);
        assert_eq!(secret, b"rotated-secret");
        secret.fill(0);
        store
            .execute("DROP TRIGGER publish_account_credentials_no_update")
            .unwrap();
        store
            .execute("UPDATE publish_account_credentials SET protected_secret=x'00' WHERE credential_version=2")
            .unwrap();
        assert!(
            store
                .read_current_publish_account_credential(account_id, 1)
                .is_err()
        );

        let clean_path = path.with_extension("delete.db");
        let clean = StationStore::open(&clean_path).unwrap();
        clean
            .write_publish_target_account(&account, None, "setup-admin", 1)
            .unwrap();
        clean
            .provision_publish_account_credential(
                account_id,
                1,
                b"deletable-secret",
                "setup-admin",
                2,
            )
            .unwrap();
        assert_eq!(
            clean
                .delete_publish_account_credential(account_id, 1, "setup-admin", 3)
                .unwrap(),
            2
        );
        assert!(
            !clean
                .read_publish_target_account(account_id)
                .unwrap()
                .unwrap()
                .credential_configured
        );
        assert!(
            clean
                .read_current_publish_account_credential(account_id, 1)
                .unwrap()
                .is_none()
        );
        drop(clean);
        drop(store);
        let _ = std::fs::remove_file(path);
        let _ = std::fs::remove_file(clean_path);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn epoch_fence_rejects_a_mixed_authority_read() {
        assert!(ensure_publish_authority_epoch_stable(7, 7).is_ok());
        assert_eq!(
            ensure_publish_authority_epoch_stable(7, 8).unwrap_err(),
            "publish authority changed while readiness was being read"
        );
    }

    #[test]
    fn failed_commit_rolls_back_the_publishing_transaction() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database =
            std::env::temp_dir().join(format!("townlight-publishing-commit-rollback-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        store.execute("BEGIN IMMEDIATE").unwrap();
        store.execute("PRAGMA defer_foreign_keys=ON").unwrap();
        store
            .execute(
                "INSERT INTO publish_target_profiles(target_id,account_id,kind,revision,configuration_hash,record_json) \
                 VALUES('22222222-2222-4333-8444-555555555555','11111111-2222-4333-8444-555555555555','portal',1,'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb','{}')",
            )
            .unwrap();

        assert!(matches!(
            finish_publishing_write(&store, Ok(())),
            Err(PublishingAuthorityWriteError::Storage(_))
        ));
        store.execute("BEGIN IMMEDIATE").unwrap();
        store.execute("ROLLBACK").unwrap();

        drop(store);
        std::fs::remove_file(database).unwrap();
    }
}
