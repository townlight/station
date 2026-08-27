use super::*;
use station_catalog::{CatalogAssetRecord, CatalogAuthorityError, CatalogEditorialState};

pub(super) const MAX_CATALOG_ASSETS: usize = 100_000;
const MAX_CATALOG_QUERY: usize = 1_000;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CatalogAssetWriteError {
    Invalid(String),
    Conflict { current_revision: Option<u64> },
    Rebound,
    Capacity,
    Storage(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CatalogAssetHistoryEntry {
    pub record: CatalogAssetRecord,
    pub changed_by: String,
    pub occurred_at_utc_ms: i64,
}

/// One bounded, keyset-paginated current catalog result.
///
/// `next_after_asset_sha256`, when present, is the exclusive cursor for the
/// next page. The cursor is always the final record returned in this page, so
/// callers can traverse the canonical asset-identity order without gaps or
/// duplicates.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CatalogAssetPage {
    pub records: Vec<CatalogAssetRecord>,
    pub next_after_asset_sha256: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScheduledRecordingFinalizationEvidence {
    pub recording_id: String,
    pub worker_instance_id: String,
    pub worker_sequence: u64,
    pub graph_revision: u64,
    pub output_index: u16,
    pub sink_id: String,
    pub artifact_relative_path: String,
    pub capture_bytes: u64,
    pub capture_sha256: String,
    pub captured_duration_ms: u64,
    pub final_media_path: String,
    pub recorded_at_unix_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScheduledRecordingCaptureEvidence {
    pub recording_id: String,
    pub worker_instance_id: String,
    pub start_worker_sequence: u64,
    pub graph_revision: u64,
    pub output_index: u16,
    pub sink_id: String,
    pub artifact_relative_path: String,
    pub duration_ms: u64,
    pub max_bytes: u64,
}

impl ScheduledRecordingCaptureEvidence {
    pub fn validate(&self) -> Result<(), String> {
        if !is_canonical_uuid(&self.recording_id)
            || !is_canonical_uuid(&self.sink_id)
            || self.worker_instance_id.trim().is_empty()
            || self.worker_instance_id.len() > 200
            || self.worker_instance_id.chars().any(char::is_control)
            || self.start_worker_sequence == 0
            || self.graph_revision == 0
            || self.artifact_relative_path != format!("{}.ts", self.recording_id)
            || !(station_media_protocol::MIN_SCHEDULED_RECORDING_DURATION_MS
                ..=station_media_protocol::MAX_SCHEDULED_RECORDING_DURATION_MS)
                .contains(&self.duration_ms)
            || !(1..=station_media_protocol::MAX_SCHEDULED_RECORDING_BYTES)
                .contains(&self.max_bytes)
        {
            return Err("scheduled recording capture evidence is invalid".into());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScheduledRecordingReadyEvidence {
    pub capture: ScheduledRecordingCaptureEvidence,
    pub terminal_worker_sequence: u64,
    pub capture_bytes: u64,
    pub capture_sha256: String,
    pub captured_duration_ms: u64,
}

impl ScheduledRecordingReadyEvidence {
    pub fn validate(&self) -> Result<(), String> {
        self.capture.validate()?;
        if self.terminal_worker_sequence <= self.capture.start_worker_sequence
            || self.capture_bytes == 0
            || self.capture_bytes > self.capture.max_bytes
            || self.captured_duration_ms == 0
            || self.captured_duration_ms > self.capture.duration_ms
            || validate_asset_identity(&self.capture_sha256).is_err()
        {
            return Err("scheduled recording ready evidence is invalid".into());
        }
        Ok(())
    }
}

impl ScheduledRecordingFinalizationEvidence {
    pub fn validate(&self) -> Result<(), String> {
        if !is_canonical_uuid(&self.recording_id)
            || !is_canonical_uuid(&self.sink_id)
            || self.worker_instance_id.trim().is_empty()
            || self.worker_instance_id.len() > 200
            || self.worker_instance_id.chars().any(char::is_control)
            || self.worker_sequence == 0
            || self.graph_revision == 0
            || self.artifact_relative_path != format!("{}.ts", self.recording_id)
            || self.capture_bytes == 0
            || self.captured_duration_ms == 0
            || validate_asset_identity(&self.capture_sha256).is_err()
            || !Path::new(&self.final_media_path).is_absolute()
            || self.final_media_path.trim() != self.final_media_path
            || self.final_media_path.chars().any(char::is_control)
            || self.recorded_at_unix_ms < 0
        {
            return Err("scheduled recording finalization evidence is invalid".into());
        }
        Ok(())
    }
}

impl StationStore {
    pub(super) fn initialize_catalog_authority_schema(&self) -> Result<(), String> {
        self.execute(
            "CREATE TABLE IF NOT EXISTS catalog_assets (\
               asset_sha256 TEXT PRIMARY KEY,\
               title TEXT NOT NULL,\
               meeting_body TEXT NOT NULL,\
               editorial_state TEXT NOT NULL CHECK (editorial_state IN ('draft','published','withdrawn')),\
               published_at_utc_ms INTEGER NOT NULL CHECK (published_at_utc_ms >= -1),\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               record_json TEXT NOT NULL,\
               CHECK ((editorial_state='draft' AND published_at_utc_ms=-1) OR \
                       (editorial_state IN ('published','withdrawn') AND published_at_utc_ms>=0))\
             );\
             CREATE INDEX IF NOT EXISTS catalog_assets_editorial_order \
               ON catalog_assets(editorial_state, published_at_utc_ms DESC, asset_sha256);\
             CREATE TABLE IF NOT EXISTS catalog_asset_history (\
               asset_sha256 TEXT NOT NULL,\
               title TEXT NOT NULL,\
               meeting_body TEXT NOT NULL,\
               editorial_state TEXT NOT NULL CHECK (editorial_state IN ('draft','published','withdrawn')),\
               published_at_utc_ms INTEGER NOT NULL CHECK (published_at_utc_ms >= -1),\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               record_json TEXT NOT NULL,\
               changed_by TEXT NOT NULL CHECK (length(changed_by) BETWEEN 1 AND 200 AND changed_by=trim(changed_by)),\
               occurred_at_utc_ms INTEGER NOT NULL CHECK (occurred_at_utc_ms >= 0),\
               PRIMARY KEY(asset_sha256, revision),\
               CHECK ((editorial_state='draft' AND published_at_utc_ms=-1) OR \
                       (editorial_state IN ('published','withdrawn') AND published_at_utc_ms>=0))\
             );\
             CREATE INDEX IF NOT EXISTS catalog_asset_history_asset_revision \
               ON catalog_asset_history(asset_sha256, revision);\
             CREATE TRIGGER IF NOT EXISTS catalog_assets_no_delete \
               BEFORE DELETE ON catalog_assets BEGIN \
                 SELECT RAISE(ABORT, 'catalog assets cannot be deleted'); \
               END;\
             CREATE TRIGGER IF NOT EXISTS catalog_asset_history_no_update \
               BEFORE UPDATE ON catalog_asset_history BEGIN \
                 SELECT RAISE(ABORT, 'catalog asset history is append-only'); \
               END;\
             CREATE TRIGGER IF NOT EXISTS catalog_asset_history_no_delete \
               BEFORE DELETE ON catalog_asset_history BEGIN \
                 SELECT RAISE(ABORT, 'catalog asset history is append-only'); \
               END;",
        )
    }

    pub fn read_catalog_asset(
        &self,
        asset_sha256: &str,
    ) -> Result<Option<CatalogAssetRecord>, String> {
        validate_asset_identity(asset_sha256)?;
        let statement = self.prepare(
            "SELECT asset_sha256,title,meeting_body,editorial_state,published_at_utc_ms,revision,record_json \
             FROM catalog_assets WHERE asset_sha256=?1",
        )?;
        bind_asset_identity(self, statement.raw, 1, asset_sha256)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let record = read_catalog_asset_row(statement.raw)?;
                self.validate_current_catalog_snapshot(&record)?;
                Ok(Some(record))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn read_catalog_asset_revision(
        &self,
        asset_sha256: &str,
        revision: u64,
    ) -> Result<Option<CatalogAssetRecord>, String> {
        validate_catalog_revision_query(asset_sha256, revision)?;
        let statement = self.prepare(
            "SELECT asset_sha256,title,meeting_body,editorial_state,published_at_utc_ms,revision,record_json \
             FROM catalog_asset_history WHERE asset_sha256=?1 AND revision=?2",
        )?;
        bind_asset_identity(self, statement.raw, 1, asset_sha256)?;
        bind_catalog_revision(self, statement.raw, 2, revision)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_catalog_asset_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_catalog_assets(&self, limit: usize) -> Result<Vec<CatalogAssetRecord>, String> {
        Ok(self.list_catalog_asset_page(None, limit)?.records)
    }

    /// Lists current catalog authority in canonical ascending asset-SHA order.
    ///
    /// The optional cursor is exclusive and must be a canonical lowercase
    /// SHA-256 identity. This deliberately fetches one additional row so the
    /// page can carry a lossless next cursor without exposing unbounded reads.
    pub fn list_catalog_asset_page(
        &self,
        after_asset_sha256: Option<&str>,
        limit: usize,
    ) -> Result<CatalogAssetPage, String> {
        validate_catalog_limit(limit, "catalog asset")?;
        if let Some(after_asset_sha256) = after_asset_sha256 {
            validate_asset_identity(after_asset_sha256)?;
        }
        let statement = self.prepare(
            "SELECT asset_sha256,title,meeting_body,editorial_state,published_at_utc_ms,revision,record_json \
             FROM catalog_assets WHERE asset_sha256>?1 ORDER BY asset_sha256 LIMIT ?2",
        )?;
        // Every valid identity is a 64-byte lowercase hexadecimal string, so
        // the empty first-page sentinel sorts before every possible asset.
        let after = c_string(
            after_asset_sha256.unwrap_or_default(),
            "catalog asset page cursor",
        )?;
        bind_text(self.database, statement.raw, 1, &after)?;
        bind_i64(self.database, statement.raw, 2, (limit + 1) as i64)?;
        let mut records = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => records.push(read_catalog_asset_row(statement.raw)?),
                SQLITE_DONE => break,
                _ => return Err(sqlite_error(self.database)),
            }
        }
        let has_more = records.len() > limit;
        if has_more {
            records.pop();
        }
        for record in &records {
            self.validate_current_catalog_snapshot(record)?;
        }
        Ok(CatalogAssetPage {
            next_after_asset_sha256: has_more.then(|| {
                records
                    .last()
                    .expect("nonempty paginated page")
                    .asset_sha256
                    .clone()
            }),
            records,
        })
    }

    pub fn list_catalog_asset_history(
        &self,
        asset_sha256: &str,
        limit: usize,
    ) -> Result<Vec<CatalogAssetHistoryEntry>, String> {
        validate_asset_identity(asset_sha256)?;
        validate_catalog_limit(limit, "catalog asset history")?;
        let statement = self.prepare(
            "SELECT asset_sha256,title,meeting_body,editorial_state,published_at_utc_ms,revision,record_json,changed_by,occurred_at_utc_ms \
             FROM catalog_asset_history WHERE asset_sha256=?1 ORDER BY revision DESC LIMIT ?2",
        )?;
        bind_asset_identity(self, statement.raw, 1, asset_sha256)?;
        bind_i64(self.database, statement.raw, 2, limit as i64)?;
        let mut entries = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => entries.push(read_catalog_history_entry(statement.raw)?),
                SQLITE_DONE => return Ok(entries),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn write_catalog_asset(
        &self,
        asset_sha256: &str,
        value: &CatalogAssetRecord,
        expected_revision: Option<u64>,
        changed_by: &str,
        occurred_at_utc_ms: i64,
    ) -> Result<(), CatalogAssetWriteError> {
        validate_write_identity(asset_sha256, value)?;
        validate_catalog_attribution(changed_by, occurred_at_utc_ms)?;
        value.validate().map_err(map_catalog_authority_error)?;
        self.execute("BEGIN IMMEDIATE")
            .map_err(CatalogAssetWriteError::Storage)?;
        let outcome = (|| {
            let current = self
                .read_catalog_asset(asset_sha256)
                .map_err(CatalogAssetWriteError::Storage)?;
            validate_catalog_cas(
                current.as_ref().map(|record| record.revision),
                expected_revision,
                value.revision,
            )?;
            // Catalog authority is only meaningful for an admitted media asset.
            // Recheck this dependency after BEGIN IMMEDIATE so every API and
            // trusted in-process write path observes one atomic authority view.
            ensure_catalog_media_dependency(self, asset_sha256)?;
            if let Some(current) = &current {
                let latest_history = self
                    .read_latest_catalog_history_entry(asset_sha256)
                    .map_err(CatalogAssetWriteError::Storage)?
                    .ok_or_else(|| {
                        CatalogAssetWriteError::Storage(
                            "current catalog asset has no exact historical snapshot".into(),
                        )
                    })?;
                if occurred_at_utc_ms < latest_history.occurred_at_utc_ms {
                    return Err(CatalogAssetWriteError::Invalid(
                        "catalog change occurrence time precedes latest history".into(),
                    ));
                }
                value
                    .validate_successor_of(
                        current,
                        expected_revision.expect("existing authority has an expected revision"),
                        occurred_at_utc_ms,
                    )
                    .map_err(map_catalog_authority_error)?;
            } else {
                value
                    .validate_creation()
                    .map_err(map_catalog_authority_error)?;
                ensure_catalog_capacity(self)?;
            }
            write_catalog_rows(
                self,
                value,
                current.as_ref().map(|record| record.revision),
                changed_by,
                occurred_at_utc_ms,
            )
        })();
        finish_catalog_write(self, outcome)
    }

    /// Atomically advances an armed recording to Capturing and persists the
    /// exact worker acknowledgement that caused the transition. The evidence
    /// is immutable and is the sole restart authority for later Ready facts.
    pub fn start_scheduled_recording_capture(
        &self,
        recording: &ScheduledRecording,
        receipt: &RecordingTransitionReceipt,
        evidence: &ScheduledRecordingCaptureEvidence,
    ) -> Result<(), String> {
        recording.validate()?;
        validate_scheduled_recording_transition_receipt(recording, receipt)?;
        evidence.validate()?;
        if receipt.kind != RecordingTransitionKind::StartCapture
            || !matches!(recording.lifecycle, RecordingLifecycle::Capturing { .. })
            || evidence.recording_id != recording.definition.recording_id
        {
            return Err("scheduled recording start evidence does not bind its transition".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let execution = scheduled_recording_execution(recording).ok_or_else(|| {
                "scheduled recording start has no execution authority".to_string()
            })?;
            if self.current_channel_output_configuration_hash(&recording.definition.channel_id)?
                != execution.channel_configuration_hash
            {
                return Err("scheduled recording start configuration authority is stale".into());
            }
            let current = self
                .read_scheduled_recording(&recording.definition.recording_id)?
                .ok_or_else(|| "scheduled recording does not exist".to_string())?;
            if current == *recording {
                if self.read_scheduled_recording_capture(&evidence.recording_id)?
                    != Some(evidence.clone())
                {
                    return Err("scheduled recording start retry disagrees with authority".into());
                }
                return Ok(());
            }
            self.write_scheduled_recording_transition_rows(recording, receipt, true)?;
            let start_json = serde_json::to_string(evidence)
                .map_err(|error| format!("serialize recording start evidence: {error}"))?;
            let statement = self.prepare(
                "INSERT INTO scheduled_recording_captures(recording_id,worker_instance_id,start_worker_sequence,graph_revision,output_index,sink_id,artifact_relative_path,duration_ms,max_bytes,terminal_worker_sequence,capture_bytes,capture_sha256,captured_duration_ms,start_json,ready_json) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,0,0,'',0,?10,'')",
            )?;
            let values = [
                c_string(&evidence.recording_id, "recording capture identity")?,
                c_string(&evidence.worker_instance_id, "recording worker instance")?,
                c_string(&evidence.sink_id, "recording capture sink")?,
                c_string(&evidence.artifact_relative_path, "recording capture path")?,
                c_string(&start_json, "recording start evidence JSON")?,
            ];
            bind_text(self.database, statement.raw, 1, &values[0])?;
            bind_text(self.database, statement.raw, 2, &values[1])?;
            bind_i64(
                self.database,
                statement.raw,
                3,
                checked_i64(evidence.start_worker_sequence, "recording start sequence")?,
            )?;
            bind_i64(
                self.database,
                statement.raw,
                4,
                checked_i64(evidence.graph_revision, "recording graph revision")?,
            )?;
            bind_i64(
                self.database,
                statement.raw,
                5,
                i64::from(evidence.output_index),
            )?;
            bind_text(self.database, statement.raw, 6, &values[2])?;
            bind_text(self.database, statement.raw, 7, &values[3])?;
            bind_i64(
                self.database,
                statement.raw,
                8,
                checked_i64(evidence.duration_ms, "recording duration")?,
            )?;
            bind_i64(
                self.database,
                statement.raw,
                9,
                checked_i64(evidence.max_bytes, "recording byte limit")?,
            )?;
            bind_text(self.database, statement.raw, 10, &values[4])?;
            expect_done(self.database, statement.raw)
        })();
        self.finish_transaction(outcome)
    }

    /// Atomically advances Capturing to Finalizing while binding the terminal
    /// Ready facts to the immutable start acknowledgement.
    pub fn ready_scheduled_recording_for_finalization(
        &self,
        recording: &ScheduledRecording,
        receipt: &RecordingTransitionReceipt,
        evidence: &ScheduledRecordingReadyEvidence,
    ) -> Result<(), String> {
        recording.validate()?;
        validate_scheduled_recording_transition_receipt(recording, receipt)?;
        evidence.validate()?;
        if receipt.kind != RecordingTransitionKind::BeginFinalization
            || !matches!(recording.lifecycle, RecordingLifecycle::Finalizing { .. })
            || evidence.capture.recording_id != recording.definition.recording_id
        {
            return Err("scheduled recording ready evidence does not bind its transition".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let execution = scheduled_recording_execution(recording).ok_or_else(|| {
                "scheduled recording Ready event has no execution authority".to_string()
            })?;
            if self.current_channel_output_configuration_hash(&recording.definition.channel_id)?
                != execution.channel_configuration_hash
            {
                return Err("scheduled recording Ready configuration authority is stale".into());
            }
            let current = self
                .read_scheduled_recording(&recording.definition.recording_id)?
                .ok_or_else(|| "scheduled recording does not exist".to_string())?;
            if current == *recording {
                if self.read_scheduled_recording_ready(&evidence.capture.recording_id)?
                    != Some(evidence.clone())
                {
                    return Err("scheduled recording Ready retry disagrees with authority".into());
                }
                return Ok(());
            }
            if self.read_scheduled_recording_capture(&evidence.capture.recording_id)?
                != Some(evidence.capture.clone())
            {
                return Err(
                    "scheduled recording Ready event is not bound to its start evidence".into(),
                );
            }
            self.write_scheduled_recording_transition_rows(recording, receipt, true)?;
            let ready_json = serde_json::to_string(evidence)
                .map_err(|error| format!("serialize recording Ready evidence: {error}"))?;
            let statement = self.prepare(
                "UPDATE scheduled_recording_captures SET terminal_worker_sequence=?1,capture_bytes=?2,capture_sha256=?3,captured_duration_ms=?4,ready_json=?5 WHERE recording_id=?6 AND ready_json=''",
            )?;
            let digest = c_string(&evidence.capture_sha256, "recording capture hash")?;
            let ready_json = c_string(&ready_json, "recording Ready evidence JSON")?;
            let recording_id =
                c_string(&evidence.capture.recording_id, "recording capture identity")?;
            bind_i64(
                self.database,
                statement.raw,
                1,
                checked_i64(
                    evidence.terminal_worker_sequence,
                    "recording terminal sequence",
                )?,
            )?;
            bind_i64(
                self.database,
                statement.raw,
                2,
                checked_i64(evidence.capture_bytes, "recording capture bytes")?,
            )?;
            bind_text(self.database, statement.raw, 3, &digest)?;
            bind_i64(
                self.database,
                statement.raw,
                4,
                checked_i64(evidence.captured_duration_ms, "recording capture duration")?,
            )?;
            bind_text(self.database, statement.raw, 5, &ready_json)?;
            bind_text(self.database, statement.raw, 6, &recording_id)?;
            expect_done(self.database, statement.raw)?;
            if self.changes() != 1 {
                return Err("scheduled recording Ready evidence lost its authority race".into());
            }
            Ok(())
        })();
        self.finish_transaction(outcome)
    }

    pub fn read_scheduled_recording_capture(
        &self,
        recording_id: &str,
    ) -> Result<Option<ScheduledRecordingCaptureEvidence>, String> {
        if !is_canonical_uuid(recording_id) {
            return Err("scheduled recording capture identity is invalid".into());
        }
        let statement = self.prepare(
            "SELECT worker_instance_id,start_worker_sequence,graph_revision,output_index,sink_id,artifact_relative_path,duration_ms,max_bytes,start_json FROM scheduled_recording_captures WHERE recording_id=?1",
        )?;
        let identity = c_string(recording_id, "recording capture identity")?;
        bind_text(self.database, statement.raw, 1, &identity)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_DONE => Ok(None),
            SQLITE_ROW => {
                let evidence: ScheduledRecordingCaptureEvidence =
                    serde_json::from_str(&column_text(statement.raw, 8)?)
                        .map_err(|error| format!("decode recording start evidence: {error}"))?;
                evidence.validate()?;
                let indexed = ScheduledRecordingCaptureEvidence {
                    recording_id: recording_id.into(),
                    worker_instance_id: column_text(statement.raw, 0)?,
                    start_worker_sequence: column_u64(
                        statement.raw,
                        1,
                        "recording start sequence",
                    )?,
                    graph_revision: column_u64(statement.raw, 2, "recording graph revision")?,
                    output_index: u16::try_from(column_u64(
                        statement.raw,
                        3,
                        "recording output index",
                    )?)
                    .map_err(|_| "database returned invalid recording output index".to_string())?,
                    sink_id: column_text(statement.raw, 4)?,
                    artifact_relative_path: column_text(statement.raw, 5)?,
                    duration_ms: column_u64(statement.raw, 6, "recording duration")?,
                    max_bytes: column_u64(statement.raw, 7, "recording byte limit")?,
                };
                if evidence != indexed {
                    return Err("recording start indexed authority and JSON disagree".into());
                }
                Ok(Some(evidence))
            }
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn read_scheduled_recording_ready(
        &self,
        recording_id: &str,
    ) -> Result<Option<ScheduledRecordingReadyEvidence>, String> {
        let capture = match self.read_scheduled_recording_capture(recording_id)? {
            Some(capture) => capture,
            None => return Ok(None),
        };
        let statement = self.prepare(
            "SELECT terminal_worker_sequence,capture_bytes,capture_sha256,captured_duration_ms,ready_json FROM scheduled_recording_captures WHERE recording_id=?1",
        )?;
        let identity = c_string(recording_id, "recording capture identity")?;
        bind_text(self.database, statement.raw, 1, &identity)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let ready_json = column_text(statement.raw, 4)?;
                if ready_json.is_empty() {
                    return Ok(None);
                }
                let evidence: ScheduledRecordingReadyEvidence =
                    serde_json::from_str(&ready_json)
                        .map_err(|error| format!("decode recording Ready evidence: {error}"))?;
                evidence.validate()?;
                let indexed = ScheduledRecordingReadyEvidence {
                    capture,
                    terminal_worker_sequence: column_u64(
                        statement.raw,
                        0,
                        "recording terminal sequence",
                    )?,
                    capture_bytes: column_u64(statement.raw, 1, "recording capture bytes")?,
                    capture_sha256: column_text(statement.raw, 2)?,
                    captured_duration_ms: column_u64(
                        statement.raw,
                        3,
                        "recording capture duration",
                    )?,
                };
                if evidence != indexed {
                    return Err("recording Ready indexed authority and JSON disagree".into());
                }
                Ok(Some(evidence))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    /// Atomically publishes one already-sealed capture into media/catalog
    /// authority and completes its recording lifecycle. Filesystem publication
    /// occurs before this call; every database fact either commits together or
    /// rolls back together. An exact retry after an ambiguous commit is
    /// idempotent, while any rebound fails closed.
    pub fn finalize_scheduled_recording(
        &self,
        recording: &ScheduledRecording,
        receipt: &RecordingTransitionReceipt,
        evidence: &ScheduledRecordingFinalizationEvidence,
        asset: &MediaAsset,
        catalog: &CatalogAssetRecord,
    ) -> Result<(), String> {
        recording.validate()?;
        validate_scheduled_recording_transition_receipt(recording, receipt)?;
        evidence.validate()?;
        asset
            .validate()
            .map_err(|error| format!("scheduled recording media asset is invalid: {error:?}"))?;
        catalog
            .validate_creation()
            .map_err(|error| format!("scheduled recording catalog draft is invalid: {error:?}"))?;
        let (captured_asset_hash, captured_duration_ms, execution) = match &recording.lifecycle {
            RecordingLifecycle::Completed {
                captured_asset_hash,
                captured_duration_ms,
                execution,
                ..
            } => (captured_asset_hash, captured_duration_ms, execution),
            _ => {
                return Err("scheduled recording finalization requires completed authority".into());
            }
        };
        if receipt.kind != RecordingTransitionKind::FinalizeSuccess
            || evidence.recording_id != recording.definition.recording_id
            || evidence.capture_sha256 != *captured_asset_hash
            || i64::try_from(evidence.captured_duration_ms).ok() != Some(*captured_duration_ms)
            || asset.asset_id != evidence.capture_sha256
            || asset.duration_ms != evidence.captured_duration_ms
            || asset.media_path != evidence.final_media_path
            || asset.readiness != AssetReadiness::Ready
            || catalog.asset_sha256 != evidence.capture_sha256
            || catalog.title.trim() != catalog.title
        {
            return Err("scheduled recording finalization bindings do not match".into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let outcome = (|| {
            let ready = self
                .read_scheduled_recording_ready(&recording.definition.recording_id)?
                .ok_or_else(|| {
                    "scheduled recording completion has no durable Ready evidence".to_string()
                })?;
            if ready.capture.worker_instance_id != evidence.worker_instance_id
                || ready.terminal_worker_sequence != evidence.worker_sequence
                || ready.capture.graph_revision != evidence.graph_revision
                || ready.capture.output_index != evidence.output_index
                || ready.capture.sink_id != evidence.sink_id
                || ready.capture.artifact_relative_path != evidence.artifact_relative_path
                || ready.capture_bytes != evidence.capture_bytes
                || ready.capture_sha256 != evidence.capture_sha256
                || ready.captured_duration_ms != evidence.captured_duration_ms
            {
                return Err(
                    "scheduled recording completion disagrees with durable Ready evidence".into(),
                );
            }
            let current_configuration =
                self.current_channel_output_configuration_hash(&recording.definition.channel_id)?;
            if current_configuration != execution.channel_configuration_hash {
                return Err(
                    "scheduled recording finalization configuration authority is stale".into(),
                );
            }
            let current = self
                .read_scheduled_recording(&recording.definition.recording_id)?
                .ok_or_else(|| "scheduled recording does not exist".to_string())?;
            if current == *recording {
                let stored = self
                    .read_scheduled_recording_finalization(&evidence.recording_id)?
                    .ok_or_else(|| {
                        "completed scheduled recording is missing finalization evidence".to_string()
                    })?;
                if stored != *evidence
                    || self.read_media_asset(&asset.asset_id)? != Some(asset.clone())
                    || self.read_catalog_asset(&catalog.asset_sha256)? != Some(catalog.clone())
                {
                    return Err(
                        "scheduled recording finalization retry disagrees with authority".into(),
                    );
                }
                return Ok(());
            }
            self.write_scheduled_recording_transition_rows(recording, receipt, true)?;
            match self.read_media_asset(&asset.asset_id)? {
                Some(existing) if existing == *asset => {}
                Some(_) => return Err("scheduled recording media identity is rebound".into()),
                None => self.put_media_asset(asset)?,
            }
            match self.read_catalog_asset(&catalog.asset_sha256)? {
                Some(existing) if existing == *catalog => {}
                Some(_) => return Err("scheduled recording catalog identity is rebound".into()),
                None => {
                    ensure_catalog_capacity(self).map_err(|error| format!("{error:?}"))?;
                    write_catalog_rows(
                        self,
                        catalog,
                        None,
                        &receipt.actor.actor_id,
                        receipt.occurred_at_unix_ms,
                    )
                    .map_err(|error| format!("{error:?}"))?;
                }
            }
            let record_json = serde_json::to_string(evidence)
                .map_err(|error| format!("serialize recording finalization evidence: {error}"))?;
            let statement = self.prepare(
                "INSERT INTO scheduled_recording_finalizations(recording_id,worker_instance_id,worker_sequence,graph_revision,output_index,sink_id,artifact_relative_path,capture_bytes,capture_sha256,captured_duration_ms,final_media_path,recorded_at_unix_ms,record_json) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13)",
            )?;
            let values = [
                c_string(&evidence.recording_id, "recording finalization identity")?,
                c_string(&evidence.worker_instance_id, "recording worker instance")?,
                c_string(&evidence.sink_id, "recording finalization sink")?,
                c_string(&evidence.artifact_relative_path, "recording artifact path")?,
                c_string(&evidence.capture_sha256, "recording capture hash")?,
                c_string(&evidence.final_media_path, "recording final media path")?,
                c_string(&record_json, "recording finalization JSON")?,
            ];
            bind_text(self.database, statement.raw, 1, &values[0])?;
            bind_text(self.database, statement.raw, 2, &values[1])?;
            bind_i64(
                self.database,
                statement.raw,
                3,
                checked_i64(evidence.worker_sequence, "recording worker sequence")?,
            )?;
            bind_i64(
                self.database,
                statement.raw,
                4,
                checked_i64(evidence.graph_revision, "recording graph revision")?,
            )?;
            bind_i64(
                self.database,
                statement.raw,
                5,
                i64::from(evidence.output_index),
            )?;
            bind_text(self.database, statement.raw, 6, &values[2])?;
            bind_text(self.database, statement.raw, 7, &values[3])?;
            bind_i64(
                self.database,
                statement.raw,
                8,
                checked_i64(evidence.capture_bytes, "recording capture bytes")?,
            )?;
            bind_text(self.database, statement.raw, 9, &values[4])?;
            bind_i64(
                self.database,
                statement.raw,
                10,
                checked_i64(evidence.captured_duration_ms, "recording capture duration")?,
            )?;
            bind_text(self.database, statement.raw, 11, &values[5])?;
            bind_i64(
                self.database,
                statement.raw,
                12,
                evidence.recorded_at_unix_ms,
            )?;
            bind_text(self.database, statement.raw, 13, &values[6])?;
            expect_done(self.database, statement.raw)
        })();
        self.finish_transaction(outcome)
    }

    pub fn read_scheduled_recording_finalization(
        &self,
        recording_id: &str,
    ) -> Result<Option<ScheduledRecordingFinalizationEvidence>, String> {
        if !is_canonical_uuid(recording_id) {
            return Err("scheduled recording finalization identity is invalid".into());
        }
        let statement = self.prepare(
            "SELECT worker_instance_id,worker_sequence,graph_revision,output_index,sink_id,artifact_relative_path,capture_bytes,capture_sha256,captured_duration_ms,final_media_path,recorded_at_unix_ms,record_json FROM scheduled_recording_finalizations WHERE recording_id=?1",
        )?;
        let identity = c_string(recording_id, "recording finalization identity")?;
        bind_text(self.database, statement.raw, 1, &identity)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_DONE => Ok(None),
            SQLITE_ROW => {
                let evidence: ScheduledRecordingFinalizationEvidence =
                    serde_json::from_str(&column_text(statement.raw, 11)?).map_err(|error| {
                        format!("decode recording finalization evidence: {error}")
                    })?;
                evidence.validate()?;
                let indexed = ScheduledRecordingFinalizationEvidence {
                    recording_id: recording_id.into(),
                    worker_instance_id: column_text(statement.raw, 0)?,
                    worker_sequence: column_u64(statement.raw, 1, "recording worker sequence")?,
                    graph_revision: column_u64(statement.raw, 2, "recording graph revision")?,
                    output_index: u16::try_from(column_u64(
                        statement.raw,
                        3,
                        "recording output index",
                    )?)
                    .map_err(|_| "database returned invalid recording output index".to_string())?,
                    sink_id: column_text(statement.raw, 4)?,
                    artifact_relative_path: column_text(statement.raw, 5)?,
                    capture_bytes: column_u64(statement.raw, 6, "recording capture bytes")?,
                    capture_sha256: column_text(statement.raw, 7)?,
                    captured_duration_ms: column_u64(
                        statement.raw,
                        8,
                        "recording capture duration",
                    )?,
                    final_media_path: column_text(statement.raw, 9)?,
                    recorded_at_unix_ms: unsafe { sqlite3_column_int64(statement.raw, 10) },
                };
                if evidence != indexed {
                    return Err("recording finalization indexed authority and JSON disagree".into());
                }
                Ok(Some(evidence))
            }
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub(super) fn validate_current_catalog_snapshot(
        &self,
        current: &CatalogAssetRecord,
    ) -> Result<(), String> {
        let historical = self
            .read_latest_catalog_history_entry(&current.asset_sha256)?
            .ok_or_else(|| "current catalog asset has no exact historical snapshot".to_string())?;
        if historical.record == *current {
            Ok(())
        } else {
            Err("current catalog asset disagrees with its latest historical snapshot".into())
        }
    }

    fn read_latest_catalog_history_entry(
        &self,
        asset_sha256: &str,
    ) -> Result<Option<CatalogAssetHistoryEntry>, String> {
        let statement = self.prepare(
            "SELECT asset_sha256,title,meeting_body,editorial_state,published_at_utc_ms,revision,record_json,changed_by,occurred_at_utc_ms \
             FROM catalog_asset_history WHERE asset_sha256=?1 ORDER BY revision DESC LIMIT 1",
        )?;
        bind_asset_identity(self, statement.raw, 1, asset_sha256)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_catalog_history_entry(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }
}

fn ensure_catalog_media_dependency(
    store: &StationStore,
    asset_sha256: &str,
) -> Result<(), CatalogAssetWriteError> {
    match store
        .read_media_asset(asset_sha256)
        .map_err(CatalogAssetWriteError::Storage)?
    {
        Some(_) => Ok(()),
        None => Err(CatalogAssetWriteError::Invalid(
            "catalog asset media dependency does not exist".into(),
        )),
    }
}

fn validate_asset_identity(asset_sha256: &str) -> Result<(), String> {
    if asset_sha256.len() == 64
        && asset_sha256
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        Ok(())
    } else {
        Err("catalog asset identity is invalid".into())
    }
}

fn validate_catalog_revision_query(asset_sha256: &str, revision: u64) -> Result<(), String> {
    validate_asset_identity(asset_sha256)?;
    if revision == 0 || revision > i64::MAX as u64 {
        Err("catalog asset revision is invalid".into())
    } else {
        Ok(())
    }
}

fn validate_catalog_limit(limit: usize, kind: &str) -> Result<(), String> {
    if (1..=MAX_CATALOG_QUERY).contains(&limit) {
        Ok(())
    } else {
        Err(format!("{kind} query is invalid or unbounded"))
    }
}

fn validate_write_identity(
    asset_sha256: &str,
    value: &CatalogAssetRecord,
) -> Result<(), CatalogAssetWriteError> {
    validate_asset_identity(asset_sha256).map_err(CatalogAssetWriteError::Invalid)?;
    if value.asset_sha256 == asset_sha256 {
        Ok(())
    } else {
        Err(CatalogAssetWriteError::Rebound)
    }
}

fn validate_catalog_attribution(
    changed_by: &str,
    occurred_at_utc_ms: i64,
) -> Result<(), CatalogAssetWriteError> {
    if occurred_at_utc_ms >= 0 && secret_field(changed_by, 200) {
        Ok(())
    } else {
        Err(CatalogAssetWriteError::Invalid(
            "catalog change attribution is invalid".into(),
        ))
    }
}

fn validate_catalog_cas(
    current_revision: Option<u64>,
    expected_revision: Option<u64>,
    incoming_revision: u64,
) -> Result<(), CatalogAssetWriteError> {
    let next_revision = current_revision.and_then(|revision| revision.checked_add(1));
    if current_revision != expected_revision
        || incoming_revision != next_revision.unwrap_or(1)
        || next_revision.is_none() && current_revision.is_some()
    {
        Err(CatalogAssetWriteError::Conflict { current_revision })
    } else {
        Ok(())
    }
}

fn map_catalog_authority_error(error: CatalogAuthorityError) -> CatalogAssetWriteError {
    match error {
        CatalogAuthorityError::IdentityRebound => CatalogAssetWriteError::Rebound,
        CatalogAuthorityError::RevisionConflict {
            current_revision, ..
        } => CatalogAssetWriteError::Conflict {
            current_revision: Some(current_revision),
        },
        other => {
            CatalogAssetWriteError::Invalid(format!("catalog authority is invalid: {other:?}"))
        }
    }
}

fn finish_catalog_write(
    store: &StationStore,
    outcome: Result<(), CatalogAssetWriteError>,
) -> Result<(), CatalogAssetWriteError> {
    match outcome {
        Ok(()) => store
            .execute("COMMIT")
            .map_err(CatalogAssetWriteError::Storage)
            .inspect_err(|_| {
                let _ = store.execute("ROLLBACK");
            }),
        Err(error) => {
            let _ = store.execute("ROLLBACK");
            Err(error)
        }
    }
}

fn ensure_catalog_capacity(store: &StationStore) -> Result<(), CatalogAssetWriteError> {
    let statement = store
        .prepare(&format!(
            "SELECT 1 FROM catalog_assets LIMIT {}",
            MAX_CATALOG_ASSETS + 1
        ))
        .map_err(CatalogAssetWriteError::Storage)?;
    let mut count = 0usize;
    loop {
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => count += 1,
            SQLITE_DONE if count < MAX_CATALOG_ASSETS => return Ok(()),
            SQLITE_DONE => return Err(CatalogAssetWriteError::Capacity),
            _ => {
                return Err(CatalogAssetWriteError::Storage(sqlite_error(
                    store.database,
                )));
            }
        }
    }
}

fn bind_asset_identity(
    store: &StationStore,
    statement: *mut SqliteStatement,
    index: c_int,
    value: &str,
) -> Result<(), String> {
    let value = c_string(value, "catalog asset identity")?;
    bind_text(store.database, statement, index, &value)
}

fn bind_catalog_revision(
    store: &StationStore,
    statement: *mut SqliteStatement,
    index: c_int,
    revision: u64,
) -> Result<(), String> {
    bind_i64(
        store.database,
        statement,
        index,
        checked_i64(revision, "catalog asset revision")?,
    )
}

fn read_catalog_history_entry(
    statement: *mut SqliteStatement,
) -> Result<CatalogAssetHistoryEntry, String> {
    let record = read_catalog_asset_row(statement)?;
    let changed_by = column_text(statement, 7)?;
    let occurred_at_utc_ms = unsafe { sqlite3_column_int64(statement, 8) };
    if !secret_field(&changed_by, 200) || occurred_at_utc_ms < 0 {
        return Err("database returned invalid catalog history attribution".into());
    }
    Ok(CatalogAssetHistoryEntry {
        record,
        changed_by,
        occurred_at_utc_ms,
    })
}

pub(super) fn read_catalog_asset_row(
    statement: *mut SqliteStatement,
) -> Result<CatalogAssetRecord, String> {
    let record: CatalogAssetRecord = serde_json::from_str(&column_text(statement, 6)?)
        .map_err(|error| format!("decode catalog asset authority: {error}"))?;
    record
        .validate()
        .map_err(|error| format!("database returned invalid catalog asset authority: {error:?}"))?;

    let meeting_body = column_text(statement, 2)?;
    let meeting_body = if meeting_body.is_empty() {
        None
    } else {
        Some(meeting_body)
    };
    let published_at_utc_ms = match unsafe { sqlite3_column_int64(statement, 4) } {
        -1 => None,
        value @ 0.. => Some(value),
        _ => return Err("database returned invalid catalog publication time".into()),
    };
    if record.asset_sha256 != column_text(statement, 0)?
        || record.title != column_text(statement, 1)?
        || record.meeting_body != meeting_body
        || record.editorial_state != parse_editorial_state(&column_text(statement, 3)?)?
        || record.published_at_utc_ms != published_at_utc_ms
        || record.revision
            != u64::try_from(unsafe { sqlite3_column_int64(statement, 5) })
                .map_err(|_| "database returned invalid catalog asset revision".to_string())?
    {
        return Err("catalog asset indexed authority and JSON disagree".into());
    }
    Ok(record)
}

fn editorial_state_name(state: CatalogEditorialState) -> &'static str {
    match state {
        CatalogEditorialState::Draft => "draft",
        CatalogEditorialState::Published => "published",
        CatalogEditorialState::Withdrawn => "withdrawn",
    }
}

fn parse_editorial_state(value: &str) -> Result<CatalogEditorialState, String> {
    match value {
        "draft" => Ok(CatalogEditorialState::Draft),
        "published" => Ok(CatalogEditorialState::Published),
        "withdrawn" => Ok(CatalogEditorialState::Withdrawn),
        _ => Err("database returned invalid catalog editorial state".into()),
    }
}

fn write_catalog_rows(
    store: &StationStore,
    value: &CatalogAssetRecord,
    prior_revision: Option<u64>,
    changed_by: &str,
    occurred_at_utc_ms: i64,
) -> Result<(), CatalogAssetWriteError> {
    let json = serde_json::to_string(value).map_err(|error| {
        CatalogAssetWriteError::Storage(format!("serialize catalog asset authority: {error}"))
    })?;
    let statement = if prior_revision.is_some() {
        store.prepare(
            "UPDATE catalog_assets SET title=?2,meeting_body=?3,editorial_state=?4,published_at_utc_ms=?5,revision=?6,record_json=?7 \
             WHERE asset_sha256=?1 AND revision=?8",
        )
    } else {
        store.prepare(
            "INSERT INTO catalog_assets(asset_sha256,title,meeting_body,editorial_state,published_at_utc_ms,revision,record_json) \
             VALUES(?1,?2,?3,?4,?5,?6,?7)",
        )
    }
    .map_err(CatalogAssetWriteError::Storage)?;
    bind_catalog_record(store, statement.raw, value, &json)?;
    if let Some(prior_revision) = prior_revision {
        bind_catalog_revision(store, statement.raw, 8, prior_revision)
            .map_err(CatalogAssetWriteError::Storage)?;
    }
    expect_done(store.database, statement.raw).map_err(CatalogAssetWriteError::Storage)?;
    if store.changes() != 1 {
        return Err(CatalogAssetWriteError::Conflict {
            current_revision: prior_revision,
        });
    }

    let history = store
        .prepare(
            "INSERT INTO catalog_asset_history(asset_sha256,title,meeting_body,editorial_state,published_at_utc_ms,revision,record_json,changed_by,occurred_at_utc_ms) \
             VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9)",
        )
        .map_err(CatalogAssetWriteError::Storage)?;
    bind_catalog_record(store, history.raw, value, &json)?;
    let actor =
        c_string(changed_by, "catalog history actor").map_err(CatalogAssetWriteError::Invalid)?;
    bind_text(store.database, history.raw, 8, &actor).map_err(CatalogAssetWriteError::Storage)?;
    bind_i64(store.database, history.raw, 9, occurred_at_utc_ms)
        .map_err(CatalogAssetWriteError::Storage)?;
    expect_done(store.database, history.raw).map_err(CatalogAssetWriteError::Storage)
}

fn bind_catalog_record(
    store: &StationStore,
    statement: *mut SqliteStatement,
    value: &CatalogAssetRecord,
    json: &str,
) -> Result<(), CatalogAssetWriteError> {
    bind_asset_identity(store, statement, 1, &value.asset_sha256)
        .map_err(CatalogAssetWriteError::Storage)?;
    let title = c_string(&value.title, "catalog title").map_err(CatalogAssetWriteError::Storage)?;
    bind_text(store.database, statement, 2, &title).map_err(CatalogAssetWriteError::Storage)?;
    let meeting_body = c_string(
        value.meeting_body.as_deref().unwrap_or_default(),
        "catalog meeting body",
    )
    .map_err(CatalogAssetWriteError::Storage)?;
    bind_text(store.database, statement, 3, &meeting_body)
        .map_err(CatalogAssetWriteError::Storage)?;
    let state = c_string(
        editorial_state_name(value.editorial_state),
        "catalog editorial state",
    )
    .map_err(CatalogAssetWriteError::Storage)?;
    bind_text(store.database, statement, 4, &state).map_err(CatalogAssetWriteError::Storage)?;
    bind_i64(
        store.database,
        statement,
        5,
        value.published_at_utc_ms.unwrap_or(-1),
    )
    .map_err(CatalogAssetWriteError::Storage)?;
    bind_catalog_revision(store, statement, 6, value.revision)
        .map_err(CatalogAssetWriteError::Storage)?;
    let json = c_string(json, "catalog authority JSON").map_err(CatalogAssetWriteError::Storage)?;
    bind_text(store.database, statement, 7, &json).map_err(CatalogAssetWriteError::Storage)
}
