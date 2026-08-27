use super::*;
use station_media_lifecycle::{
    AssetReplacementLink, AssetRetentionPolicy, AssetThumbnail, RetentionDisposition,
    RetentionScanAction,
};

const MAX_MEDIA_LIFECYCLE_HISTORY: usize = 256;
const MAX_RETENTION_SCAN_BATCH: usize = 128;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AssetRetentionHistoryEntry {
    pub policy: AssetRetentionPolicy,
    pub changed_by: String,
    pub occurred_at_unix_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MediaLifecycleScanResult {
    pub review_required_asset_ids: Vec<String>,
    pub held_asset_ids: Vec<String>,
}

impl StationStore {
    pub(crate) fn initialize_media_lifecycle_schema(&self) -> Result<(), String> {
        self.execute(
            "CREATE TABLE IF NOT EXISTS asset_retention_policies (\
               asset_id TEXT PRIMARY KEY CHECK (length(asset_id) = 64),\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               permanent INTEGER NOT NULL CHECK (permanent IN (0,1)),\
               review_after_unix_ms INTEGER NOT NULL DEFAULT -1 CHECK (review_after_unix_ms >= -1),\
               legal_hold INTEGER NOT NULL CHECK (legal_hold IN (0,1)),\
               disposition TEXT NOT NULL CHECK (disposition IN ('retained','review_required','legal_hold')),\
               record_json TEXT NOT NULL,\
               FOREIGN KEY(asset_id) REFERENCES media_assets(asset_id)\
             );\
             CREATE TABLE IF NOT EXISTS asset_retention_policy_history (\
               asset_id TEXT NOT NULL CHECK (length(asset_id) = 64),\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               record_json TEXT NOT NULL,\
               changed_by TEXT NOT NULL CHECK (length(changed_by) BETWEEN 1 AND 200),\
               occurred_at_unix_ms INTEGER NOT NULL CHECK (occurred_at_unix_ms >= 0),\
               PRIMARY KEY(asset_id, revision),\
               FOREIGN KEY(asset_id) REFERENCES media_assets(asset_id)\
             );\
             CREATE TABLE IF NOT EXISTS asset_replacement_links (\
               predecessor_asset_id TEXT PRIMARY KEY CHECK (length(predecessor_asset_id) = 64),\
               successor_asset_id TEXT NOT NULL UNIQUE CHECK (length(successor_asset_id) = 64),\
               revision INTEGER NOT NULL CHECK (revision = 1),\
               record_json TEXT NOT NULL,\
               FOREIGN KEY(predecessor_asset_id) REFERENCES media_assets(asset_id),\
               FOREIGN KEY(successor_asset_id) REFERENCES media_assets(asset_id),\
               CHECK (predecessor_asset_id <> successor_asset_id)\
             );\
             CREATE TABLE IF NOT EXISTS asset_thumbnails (\
               asset_id TEXT PRIMARY KEY CHECK (length(asset_id) = 64),\
               thumbnail_sha256 TEXT NOT NULL CHECK (length(thumbnail_sha256) = 64),\
               record_json TEXT NOT NULL,\
               FOREIGN KEY(asset_id) REFERENCES media_assets(asset_id)\
             );\
             CREATE INDEX IF NOT EXISTS asset_retention_scan ON asset_retention_policies(legal_hold, permanent, review_after_unix_ms, asset_id);\
             CREATE TRIGGER IF NOT EXISTS asset_retention_history_no_update BEFORE UPDATE ON asset_retention_policy_history BEGIN SELECT RAISE(ABORT, 'asset retention history is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS asset_retention_history_no_delete BEFORE DELETE ON asset_retention_policy_history BEGIN SELECT RAISE(ABORT, 'asset retention history is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS asset_replacement_links_no_update BEFORE UPDATE ON asset_replacement_links BEGIN SELECT RAISE(ABORT, 'asset replacement links are append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS asset_replacement_links_no_delete BEFORE DELETE ON asset_replacement_links BEGIN SELECT RAISE(ABORT, 'asset replacement links are append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS asset_thumbnails_no_update BEFORE UPDATE ON asset_thumbnails BEGIN SELECT RAISE(ABORT, 'asset thumbnails are immutable'); END;\
             CREATE TRIGGER IF NOT EXISTS asset_thumbnails_no_delete BEFORE DELETE ON asset_thumbnails BEGIN SELECT RAISE(ABORT, 'asset thumbnails are immutable'); END;",
        )
    }

    pub fn read_asset_retention_policy(
        &self,
        asset_id: &str,
    ) -> Result<Option<AssetRetentionPolicy>, String> {
        if !is_sha256_identity(asset_id) {
            return Err("retention asset identity is invalid".into());
        }
        let statement =
            self.prepare("SELECT record_json FROM asset_retention_policies WHERE asset_id=?1")?;
        let asset_id = c_string(asset_id, "retention asset identity")?;
        bind_text(self.database, statement.raw, 1, &asset_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_lifecycle_json(column_text(statement.raw, 0)?)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn write_asset_retention_policy(
        &self,
        policy: &AssetRetentionPolicy,
        expected_revision: u64,
        changed_by: &str,
        occurred_at_unix_ms: i64,
    ) -> Result<AssetRetentionPolicy, String> {
        policy.validate().map_err(|error| format!("{error:?}"))?;
        if !secret_field(changed_by, 200) || occurred_at_unix_ms < 0 {
            return Err("retention policy attribution is invalid".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            if self.read_media_asset(&policy.asset_id)?.is_none() {
                return Err("retention asset does not exist".into());
            }
            let current = self.read_asset_retention_policy(&policy.asset_id)?;
            let actual_revision = current.as_ref().map_or(0, |value| value.revision);
            if actual_revision != expected_revision {
                return Err(format!(
                    "retention revision conflict: current is {actual_revision}"
                ));
            }
            if policy.revision != expected_revision.saturating_add(1) {
                return Err("retention policy revision must advance exactly once".into());
            }
            let json = serde_json::to_string(policy)
                .map_err(|error| format!("serialize retention policy: {error}"))?;
            self.upsert_retention_policy(policy, &json)?;
            self.insert_retention_history(policy, &json, changed_by, occurred_at_unix_ms)?;
            Ok(policy.clone())
        })();
        self.finish_value_transaction(outcome)
    }

    pub fn list_asset_retention_history(
        &self,
        asset_id: &str,
        limit: usize,
    ) -> Result<Vec<AssetRetentionHistoryEntry>, String> {
        if !is_sha256_identity(asset_id) || !(1..=MAX_MEDIA_LIFECYCLE_HISTORY).contains(&limit) {
            return Err("retention history query is invalid or unbounded".into());
        }
        let statement = self.prepare(
            "SELECT record_json, changed_by, occurred_at_unix_ms FROM asset_retention_policy_history WHERE asset_id=?1 ORDER BY revision LIMIT ?2",
        )?;
        let asset_id = c_string(asset_id, "retention asset identity")?;
        bind_text(self.database, statement.raw, 1, &asset_id)?;
        bind_i64(self.database, statement.raw, 2, limit as i64)?;
        let mut values = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    let policy = read_lifecycle_json(column_text(statement.raw, 0)?)?;
                    let changed_by = column_text(statement.raw, 1)?;
                    let occurred_at_unix_ms = unsafe { sqlite3_column_int64(statement.raw, 2) };
                    if !secret_field(&changed_by, 200) || occurred_at_unix_ms < 0 {
                        return Err("retention history attribution is invalid".into());
                    }
                    values.push(AssetRetentionHistoryEntry {
                        policy,
                        changed_by,
                        occurred_at_unix_ms,
                    });
                }
                SQLITE_DONE => return Ok(values),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    /// The scanner only turns a due retention intent into a durable review task.
    /// It has no deletion capability and does not touch media paths or schedules.
    pub fn scan_retention_due(
        &self,
        now_unix_ms: i64,
        scanner_actor: &str,
    ) -> Result<MediaLifecycleScanResult, String> {
        if now_unix_ms < 0 || !secret_field(scanner_actor, 200) {
            return Err("retention scanner input is invalid".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let statement = self.prepare(
                "SELECT record_json FROM asset_retention_policies ORDER BY asset_id LIMIT ?1",
            )?;
            bind_i64(
                self.database,
                statement.raw,
                1,
                MAX_RETENTION_SCAN_BATCH as i64,
            )?;
            let mut policies = Vec::new();
            loop {
                match unsafe { sqlite3_step(statement.raw) } {
                    SQLITE_ROW => {
                        policies.push(read_lifecycle_json(column_text(statement.raw, 0)?)?)
                    }
                    SQLITE_DONE => break,
                    _ => return Err(sqlite_error(self.database)),
                }
            }
            let mut result = MediaLifecycleScanResult {
                review_required_asset_ids: Vec::new(),
                held_asset_ids: Vec::new(),
            };
            for mut policy in policies {
                match policy
                    .scan_action_at(now_unix_ms)
                    .map_err(|error| format!("{error:?}"))?
                {
                    RetentionScanAction::NoAction => {}
                    RetentionScanAction::Held => result.held_asset_ids.push(policy.asset_id),
                    RetentionScanAction::FlagForReview => {
                        if policy.disposition == RetentionDisposition::Retained {
                            policy.revision = policy
                                .revision
                                .checked_add(1)
                                .ok_or("retention revision overflow")?;
                            policy.disposition = RetentionDisposition::ReviewRequired;
                            let json = serde_json::to_string(&policy).map_err(|error| {
                                format!("serialize retention scan policy: {error}")
                            })?;
                            self.upsert_retention_policy(&policy, &json)?;
                            self.insert_retention_history(
                                &policy,
                                &json,
                                scanner_actor,
                                now_unix_ms,
                            )?;
                        }
                        result.review_required_asset_ids.push(policy.asset_id);
                    }
                }
            }
            Ok(result)
        })();
        self.finish_value_transaction(outcome)
    }

    pub fn create_asset_replacement_link(
        &self,
        link: &AssetReplacementLink,
        expected_predecessor_revision: u64,
    ) -> Result<(), String> {
        link.validate().map_err(|error| format!("{error:?}"))?;
        if expected_predecessor_revision != 0 || link.revision != 1 {
            return Err(
                "a replacement link can be created only once at predecessor revision zero".into(),
            );
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let predecessor = self
                .read_media_asset(&link.predecessor_asset_id)?
                .ok_or("replacement predecessor asset does not exist")?;
            let successor = self
                .read_media_asset(&link.successor_asset_id)?
                .ok_or("replacement successor asset does not exist")?;
            if predecessor.readiness != AssetReadiness::Ready
                || successor.readiness != AssetReadiness::Ready
            {
                return Err("replacement requires both immutable assets to be ready".into());
            }
            if self
                .read_asset_replacement_link(&link.predecessor_asset_id)?
                .is_some()
            {
                return Err(
                    "replacement predecessor already has immutable successor authority".into(),
                );
            }
            let json = serde_json::to_string(link)
                .map_err(|error| format!("serialize replacement link: {error}"))?;
            let statement = self.prepare(
                "INSERT INTO asset_replacement_links(predecessor_asset_id, successor_asset_id, revision, record_json) VALUES(?1, ?2, 1, ?3)",
            )?;
            let predecessor = c_string(&link.predecessor_asset_id, "replacement predecessor")?;
            let successor = c_string(&link.successor_asset_id, "replacement successor")?;
            let json = c_string(&json, "replacement link")?;
            bind_text(self.database, statement.raw, 1, &predecessor)?;
            bind_text(self.database, statement.raw, 2, &successor)?;
            bind_text(self.database, statement.raw, 3, &json)?;
            expect_done(self.database, statement.raw)
        })();
        self.finish_value_transaction(outcome)
    }

    pub fn read_asset_replacement_link(
        &self,
        predecessor_asset_id: &str,
    ) -> Result<Option<AssetReplacementLink>, String> {
        if !is_sha256_identity(predecessor_asset_id) {
            return Err("replacement predecessor identity is invalid".into());
        }
        let statement = self.prepare(
            "SELECT record_json FROM asset_replacement_links WHERE predecessor_asset_id=?1",
        )?;
        let predecessor = c_string(predecessor_asset_id, "replacement predecessor")?;
        bind_text(self.database, statement.raw, 1, &predecessor)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_replacement_json(column_text(statement.raw, 0)?)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn record_asset_thumbnail(&self, thumbnail: &AssetThumbnail) -> Result<(), String> {
        thumbnail.validate().map_err(|error| format!("{error:?}"))?;
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            if self.read_media_asset(&thumbnail.asset_id)?.is_none() {
                return Err("thumbnail source asset does not exist".into());
            }
            let existing = self.read_asset_thumbnail(&thumbnail.asset_id)?;
            if let Some(existing) = existing {
                if existing == *thumbnail {
                    return Ok(());
                }
                return Err("thumbnail authority is immutable; a new thumbnail cannot replace existing evidence".into());
            }
            let json = serde_json::to_string(thumbnail)
                .map_err(|error| format!("serialize thumbnail: {error}"))?;
            let statement = self.prepare("INSERT INTO asset_thumbnails(asset_id, thumbnail_sha256, record_json) VALUES(?1, ?2, ?3)")?;
            let asset = c_string(&thumbnail.asset_id, "thumbnail asset identity")?;
            let digest = c_string(&thumbnail.thumbnail_sha256, "thumbnail digest")?;
            let json = c_string(&json, "thumbnail record")?;
            bind_text(self.database, statement.raw, 1, &asset)?;
            bind_text(self.database, statement.raw, 2, &digest)?;
            bind_text(self.database, statement.raw, 3, &json)?;
            expect_done(self.database, statement.raw)
        })();
        self.finish_value_transaction(outcome)
    }

    pub fn read_asset_thumbnail(&self, asset_id: &str) -> Result<Option<AssetThumbnail>, String> {
        if !is_sha256_identity(asset_id) {
            return Err("thumbnail asset identity is invalid".into());
        }
        let statement =
            self.prepare("SELECT record_json FROM asset_thumbnails WHERE asset_id=?1")?;
        let asset_id = c_string(asset_id, "thumbnail asset identity")?;
        bind_text(self.database, statement.raw, 1, &asset_id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_thumbnail_json(column_text(statement.raw, 0)?)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    /// This internal operation is intentionally not a general asset update:
    /// it can only restore an asset currently marked missing, with its existing
    /// SHA-256 identity, after an admission worker has produced `managed_path`.
    pub fn relink_missing_media_asset(
        &self,
        asset_id: &str,
        managed_path: &str,
        duration_ms: u64,
    ) -> Result<MediaAsset, String> {
        if !is_sha256_identity(asset_id) || !visible_path(managed_path) || duration_ms == 0 {
            return Err("media relink input is invalid".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let current = self
                .read_media_asset(asset_id)?
                .ok_or_else(|| "missing asset does not exist".to_string())?;
            if current.readiness != AssetReadiness::Missing {
                return Err("only a missing asset may be relinked".to_string());
            }
            let statement = self.prepare(
                "UPDATE media_assets SET media_path=?1, duration_ms=?2, readiness='ready' WHERE asset_id=?3 AND readiness='missing'",
            )?;
            let path = c_string(managed_path, "managed media relink path")?;
            let id = c_string(asset_id, "media relink identity")?;
            bind_text(self.database, statement.raw, 1, &path)?;
            bind_i64(
                self.database,
                statement.raw,
                2,
                checked_i64(duration_ms, "media relink duration")?,
            )?;
            bind_text(self.database, statement.raw, 3, &id)?;
            expect_done(self.database, statement.raw)?;
            if self.changes() != 1 {
                return Err("media relink raced with a newer asset state".to_string());
            }
            self.read_media_asset(asset_id)?
                .ok_or_else(|| "media relink did not persist".to_string())
        })();
        self.finish_value_transaction(outcome)
    }

    fn upsert_retention_policy(
        &self,
        policy: &AssetRetentionPolicy,
        json: &str,
    ) -> Result<(), String> {
        let statement = self.prepare(
            "INSERT INTO asset_retention_policies(asset_id, revision, permanent, review_after_unix_ms, legal_hold, disposition, record_json) VALUES(?1, ?2, ?3, ?4, ?5, ?6, ?7) \
             ON CONFLICT(asset_id) DO UPDATE SET revision=excluded.revision, permanent=excluded.permanent, review_after_unix_ms=excluded.review_after_unix_ms, legal_hold=excluded.legal_hold, disposition=excluded.disposition, record_json=excluded.record_json",
        )?;
        let asset = c_string(&policy.asset_id, "retention asset identity")?;
        let disposition = c_string(
            retention_disposition_name(policy.disposition),
            "retention disposition",
        )?;
        let json = c_string(json, "retention policy")?;
        bind_text(self.database, statement.raw, 1, &asset)?;
        bind_i64(
            self.database,
            statement.raw,
            2,
            checked_i64(policy.revision, "retention revision")?,
        )?;
        bind_i64(self.database, statement.raw, 3, i64::from(policy.permanent))?;
        bind_i64(
            self.database,
            statement.raw,
            4,
            policy.review_after_unix_ms.unwrap_or(-1),
        )?;
        bind_i64(
            self.database,
            statement.raw,
            5,
            i64::from(policy.legal_hold),
        )?;
        bind_text(self.database, statement.raw, 6, &disposition)?;
        bind_text(self.database, statement.raw, 7, &json)?;
        expect_done(self.database, statement.raw)
    }

    fn insert_retention_history(
        &self,
        policy: &AssetRetentionPolicy,
        json: &str,
        changed_by: &str,
        occurred_at_unix_ms: i64,
    ) -> Result<(), String> {
        let statement = self.prepare(
            "INSERT INTO asset_retention_policy_history(asset_id, revision, record_json, changed_by, occurred_at_unix_ms) VALUES(?1, ?2, ?3, ?4, ?5)",
        )?;
        let asset = c_string(&policy.asset_id, "retention asset identity")?;
        let json = c_string(json, "retention policy")?;
        let actor = c_string(changed_by, "retention actor")?;
        bind_text(self.database, statement.raw, 1, &asset)?;
        bind_i64(
            self.database,
            statement.raw,
            2,
            checked_i64(policy.revision, "retention revision")?,
        )?;
        bind_text(self.database, statement.raw, 3, &json)?;
        bind_text(self.database, statement.raw, 4, &actor)?;
        bind_i64(self.database, statement.raw, 5, occurred_at_unix_ms)?;
        expect_done(self.database, statement.raw)
    }
}

fn retention_disposition_name(value: RetentionDisposition) -> &'static str {
    match value {
        RetentionDisposition::Retained => "retained",
        RetentionDisposition::ReviewRequired => "review_required",
        RetentionDisposition::LegalHold => "legal_hold",
    }
}

fn read_lifecycle_json(value: String) -> Result<AssetRetentionPolicy, String> {
    let policy = serde_json::from_str::<AssetRetentionPolicy>(&value)
        .map_err(|error| format!("stored retention policy JSON is invalid: {error}"))?;
    policy
        .validate()
        .map_err(|error| format!("stored retention policy is invalid: {error:?}"))?;
    Ok(policy)
}

fn read_replacement_json(value: String) -> Result<AssetReplacementLink, String> {
    let link = serde_json::from_str::<AssetReplacementLink>(&value)
        .map_err(|error| format!("stored replacement link JSON is invalid: {error}"))?;
    link.validate()
        .map_err(|error| format!("stored replacement link is invalid: {error:?}"))?;
    Ok(link)
}

fn read_thumbnail_json(value: String) -> Result<AssetThumbnail, String> {
    let thumbnail = serde_json::from_str::<AssetThumbnail>(&value)
        .map_err(|error| format!("stored thumbnail JSON is invalid: {error}"))?;
    thumbnail
        .validate()
        .map_err(|error| format!("stored thumbnail is invalid: {error:?}"))?;
    Ok(thumbnail)
}

fn visible_path(value: &str) -> bool {
    !value.trim().is_empty() && value.len() <= 2_000 && !value.chars().any(char::is_control)
}
