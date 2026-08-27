use super::*;
use station_ott_contract::{
    ClientPlaybackBeacon, OTT_CONTRACT_VERSION, PlatformCapabilityDeclaration, PlaybackBeaconEvent,
    PlaybackSessionAuthority, PublicCatalogPage,
};
use station_ott_runtime::{
    ADMISSION_WINDOW_MS, MAX_BEACON_CLOCK_SKEW_MS, MAX_BEACONS_PER_SESSION, MAX_BEACONS_PER_WINDOW,
    MAX_RECLAIMED_BEACONS_PER_REQUEST, MAX_RECLAIMED_SESSIONS_PER_REQUEST,
    MAX_SESSION_ISSUES_PER_WINDOW, MAX_TOTAL_BEACONS, MAX_TOTAL_SESSIONS, MIN_BEACON_INTERVAL_MS,
    PlaybackBeaconReceipt, PlaybackBeaconResponse, PlaybackSessionIssueRequest,
    PlaybackSessionIssueResponse, SESSION_RETENTION_AFTER_EXPIRY_MS, SESSION_TTL_MS,
    new_session_capability, new_uuid, validate_beacon_input, validate_issue_request,
};
use subtle::ConstantTimeEq;

pub use station_ott_runtime::{
    PlaybackBeaconReceipt as OttBeaconCollection, PlaybackSessionIssueResponse as OttSessionIssue,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OttAuthorityError {
    Unavailable,
    ReplayConflict,
    RateLimited(u64),
    InvalidCapability,
    Stale,
    InvalidSequence,
    Corrupt,
    Internal,
}

impl From<String> for OttAuthorityError {
    fn from(_: String) -> Self {
        Self::Internal
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OttCatalogHistoryEntry {
    pub page: PublicCatalogPage,
    pub changed_by: String,
    pub occurred_at_unix_ms: i64,
}

struct OttPlaybackState {
    last_event: String,
    last_position: u64,
    last_observed: i64,
    last_beacon_id: String,
    last_accepted_at: i64,
}

impl StationStore {
    pub(crate) fn initialize_ott_schema(&self) -> Result<(), String> {
        self.execute(
            r#"CREATE TABLE IF NOT EXISTS ott_catalog_current (
               singleton INTEGER PRIMARY KEY CHECK(singleton=1),
               catalog_revision INTEGER NOT NULL CHECK(catalog_revision >= 1),
               page_json TEXT NOT NULL,
               changed_by TEXT NOT NULL CHECK(length(changed_by) BETWEEN 1 AND 200),
               occurred_at_unix_ms INTEGER NOT NULL CHECK(occurred_at_unix_ms >= 0)
             );
             CREATE TABLE IF NOT EXISTS ott_catalog_history (
               catalog_revision INTEGER PRIMARY KEY CHECK(catalog_revision >= 1),
               page_json TEXT NOT NULL,
               changed_by TEXT NOT NULL CHECK(length(changed_by) BETWEEN 1 AND 200),
               occurred_at_unix_ms INTEGER NOT NULL CHECK(occurred_at_unix_ms >= 0)
             );
             CREATE TABLE IF NOT EXISTS ott_playback_sessions (
               session_id TEXT PRIMARY KEY,
               request_id TEXT NOT NULL UNIQUE,
               content_id TEXT NOT NULL,
               client_id TEXT NOT NULL,
               catalog_revision INTEGER NOT NULL CHECK(catalog_revision >= 1),
               session_revision INTEGER NOT NULL CHECK(session_revision >= 1),
               server_session_digest TEXT NOT NULL CHECK(length(server_session_digest)=64),
               issued_at_unix_ms INTEGER NOT NULL CHECK(issued_at_unix_ms >= 0),
               expires_at_unix_ms INTEGER NOT NULL CHECK(expires_at_unix_ms > issued_at_unix_ms),
               client_json TEXT NOT NULL,
               capability_ciphertext BLOB NOT NULL
             );
             CREATE TABLE IF NOT EXISTS ott_playback_beacons (
               beacon_id TEXT PRIMARY KEY,
               session_id TEXT NOT NULL,
               beacon_json TEXT NOT NULL,
               accepted_at_unix_ms INTEGER NOT NULL CHECK(accepted_at_unix_ms >= 0),
               FOREIGN KEY(session_id) REFERENCES ott_playback_sessions(session_id)
             );
             CREATE TABLE IF NOT EXISTS ott_playback_session_state (
               session_id TEXT PRIMARY KEY,
               last_event TEXT NOT NULL CHECK(last_event IN ('none','started','progress','completed')),
               last_position_ms INTEGER NOT NULL CHECK(last_position_ms >= 0),
               last_observed_at_unix_ms INTEGER NOT NULL CHECK(last_observed_at_unix_ms >= 0),
               last_beacon_id TEXT NOT NULL DEFAULT '',
               last_accepted_at_unix_ms INTEGER NOT NULL DEFAULT 0 CHECK(last_accepted_at_unix_ms >= 0),
               FOREIGN KEY(session_id) REFERENCES ott_playback_sessions(session_id)
             );
             CREATE TABLE IF NOT EXISTS ott_admission_windows (
               bucket TEXT PRIMARY KEY CHECK(bucket IN ('sessions','beacons')),
               window_started_at_unix_ms INTEGER NOT NULL CHECK(window_started_at_unix_ms >= 0),
               accepted_count INTEGER NOT NULL CHECK(accepted_count >= 1)
             );
             CREATE INDEX IF NOT EXISTS ott_playback_beacons_session_time
               ON ott_playback_beacons(session_id, accepted_at_unix_ms, beacon_id);
             CREATE INDEX IF NOT EXISTS ott_playback_sessions_expiry
               ON ott_playback_sessions(expires_at_unix_ms, session_id);
             CREATE TRIGGER IF NOT EXISTS ott_catalog_history_no_update BEFORE UPDATE ON ott_catalog_history
               BEGIN SELECT RAISE(ABORT, 'OTT catalog history is append-only'); END;
             CREATE TRIGGER IF NOT EXISTS ott_catalog_history_no_delete BEFORE DELETE ON ott_catalog_history
               BEGIN SELECT RAISE(ABORT, 'OTT catalog history is append-only'); END;
             CREATE TRIGGER IF NOT EXISTS ott_playback_sessions_no_update BEFORE UPDATE ON ott_playback_sessions
               BEGIN SELECT RAISE(ABORT, 'OTT playback sessions are immutable'); END;
             CREATE TRIGGER IF NOT EXISTS ott_playback_beacons_no_update BEFORE UPDATE ON ott_playback_beacons
               BEGIN SELECT RAISE(ABORT, 'OTT playback beacons are append-only'); END;
             DROP TRIGGER IF EXISTS ott_playback_sessions_no_delete;
             DROP TRIGGER IF EXISTS ott_playback_beacons_no_delete;"#,
        )?;
        if !self
            .sqlite_table_has_column("ott_playback_session_state", "last_observed_at_unix_ms")?
        {
            self.execute("ALTER TABLE ott_playback_session_state ADD COLUMN last_observed_at_unix_ms INTEGER NOT NULL DEFAULT 0; UPDATE ott_playback_session_state SET last_observed_at_unix_ms=COALESCE((SELECT issued_at_unix_ms FROM ott_playback_sessions WHERE session_id=ott_playback_session_state.session_id),0);")?;
        }
        if !self.sqlite_table_has_column("ott_playback_session_state", "last_beacon_id")? {
            self.execute("ALTER TABLE ott_playback_session_state ADD COLUMN last_beacon_id TEXT NOT NULL DEFAULT '';")?;
        }
        if !self
            .sqlite_table_has_column("ott_playback_session_state", "last_accepted_at_unix_ms")?
        {
            self.execute("ALTER TABLE ott_playback_session_state ADD COLUMN last_accepted_at_unix_ms INTEGER NOT NULL DEFAULT 0; UPDATE ott_playback_session_state SET last_accepted_at_unix_ms=COALESCE((SELECT issued_at_unix_ms FROM ott_playback_sessions WHERE session_id=ott_playback_session_state.session_id),0);")?;
        }
        Ok(())
    }

    /// Trusted in-process catalog publication. There is intentionally no
    /// browser route that can inject an OTT catalog or playback URL.
    pub fn write_ott_catalog_page(
        &self,
        page: &PublicCatalogPage,
        changed_by: &str,
        occurred_at_unix_ms: i64,
    ) -> Result<(), OttAuthorityError> {
        page.validate()?;
        if changed_by.is_empty()
            || changed_by.len() > 200
            || changed_by.trim() != changed_by
            || occurred_at_unix_ms < 0
        {
            return Err(OttAuthorityError::Internal);
        }
        if page.next_cursor.is_some() {
            return Err(OttAuthorityError::Internal);
        }
        let page_json = serde_json::to_string(page)
            .map_err(|error| format!("serialize OTT catalog: {error}"))?;
        self.execute("BEGIN IMMEDIATE")?;
        let result = (|| {
            let existing = self.read_ott_catalog_page()?;
            if let Some(existing) = existing
                && page.catalog_revision <= existing.catalog_revision
            {
                return Err(OttAuthorityError::ReplayConflict);
            }
            let revision = checked_i64(page.catalog_revision, "OTT catalog revision")?;
            let page_c = c_string(&page_json, "OTT catalog JSON")?;
            let actor_c = c_string(changed_by, "OTT catalog actor")?;
            let history = self.prepare("INSERT INTO ott_catalog_history(catalog_revision,page_json,changed_by,occurred_at_unix_ms) VALUES(?1,?2,?3,?4)")?;
            bind_i64(self.database, history.raw, 1, revision)?;
            bind_text(self.database, history.raw, 2, &page_c)?;
            bind_text(self.database, history.raw, 3, &actor_c)?;
            bind_i64(self.database, history.raw, 4, occurred_at_unix_ms)?;
            expect_done(self.database, history.raw)?;
            let current = self.prepare("INSERT INTO ott_catalog_current(singleton,catalog_revision,page_json,changed_by,occurred_at_unix_ms) VALUES(1,?1,?2,?3,?4) ON CONFLICT(singleton) DO UPDATE SET catalog_revision=excluded.catalog_revision,page_json=excluded.page_json,changed_by=excluded.changed_by,occurred_at_unix_ms=excluded.occurred_at_unix_ms")?;
            bind_i64(self.database, current.raw, 1, revision)?;
            bind_text(self.database, current.raw, 2, &page_c)?;
            bind_text(self.database, current.raw, 3, &actor_c)?;
            bind_i64(self.database, current.raw, 4, occurred_at_unix_ms)?;
            Ok(expect_done(self.database, current.raw)?)
        })();
        self.finish_ott_transaction(result)
    }

    pub fn read_ott_catalog_page(&self) -> Result<Option<PublicCatalogPage>, OttAuthorityError> {
        let statement = self.prepare("SELECT catalog_revision,page_json,changed_by,occurred_at_unix_ms FROM ott_catalog_current WHERE singleton=1")?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_DONE => {
                let history = self.prepare(
                    "SELECT 1 FROM ott_catalog_history ORDER BY catalog_revision DESC LIMIT 1",
                )?;
                if unsafe { sqlite3_step(history.raw) } == SQLITE_ROW {
                    return Err(OttAuthorityError::Corrupt);
                }
                Ok(None)
            }
            SQLITE_ROW => {
                let revision = column_u64(statement.raw, 0, "OTT catalog revision")?;
                let page: PublicCatalogPage = serde_json::from_str(&column_text(statement.raw, 1)?)
                    .map_err(|error| {
                        format!("database returned invalid OTT catalog JSON: {error}")
                    })?;
                page.validate()?;
                if page.catalog_revision != revision {
                    return Err(OttAuthorityError::Corrupt);
                }
                let history = self.prepare("SELECT catalog_revision,page_json,changed_by,occurred_at_unix_ms FROM ott_catalog_history ORDER BY catalog_revision DESC LIMIT 1")?;
                if unsafe { sqlite3_step(history.raw) } != SQLITE_ROW
                    || column_u64(history.raw, 0, "OTT catalog history revision")? != revision
                    || column_text(history.raw, 1)? != column_text(statement.raw, 1)?
                    || column_text(history.raw, 2)? != column_text(statement.raw, 2)?
                    || unsafe { sqlite3_column_int64(history.raw, 3) }
                        != unsafe { sqlite3_column_int64(statement.raw, 3) }
                {
                    return Err(OttAuthorityError::Corrupt);
                }
                Ok(Some(page))
            }
            _ => Err(sqlite_error(self.database).into()),
        }
    }

    pub fn issue_ott_playback_session(
        &self,
        request: &PlaybackSessionIssueRequest,
        now_unix_ms: i64,
    ) -> Result<PlaybackSessionIssueResponse, OttAuthorityError> {
        validate_issue_request(request)?;
        if now_unix_ms < 0 {
            return Err("OTT session time is invalid".to_string().into());
        }
        let client_json = serde_json::to_string(&request.client)
            .map_err(|error| format!("serialize OTT client: {error}"))?;
        let request_hash = format!("{:x}", Sha256::digest(client_json.as_bytes()));
        self.execute("BEGIN IMMEDIATE")?;
        let result = (|| {
            self.reclaim_expired_ott_sessions(now_unix_ms)?;
            let existing = self.prepare("SELECT session_id,content_id,client_id,catalog_revision,session_revision,server_session_digest,issued_at_unix_ms,expires_at_unix_ms,client_json,capability_ciphertext FROM ott_playback_sessions WHERE request_id=?1")?;
            let request_id = c_string(&request.request_id, "OTT session request identity")?;
            bind_text(self.database, existing.raw, 1, &request_id)?;
            match unsafe { sqlite3_step(existing.raw) } {
                SQLITE_ROW => {
                    let old_client = column_text(existing.raw, 8)?;
                    if column_text(existing.raw, 1)? != request.content_id
                        || column_text(existing.raw, 2)? != request.client.client_id
                        || format!("{:x}", Sha256::digest(old_client.as_bytes())) != request_hash
                    {
                        return Err(OttAuthorityError::ReplayConflict);
                    }
                    let session = read_session_row(existing.raw)?;
                    session.validate_shape()?;
                    if now_unix_ms >= session.expires_at_unix_ms {
                        return Err(OttAuthorityError::Stale);
                    }
                    let stored_client: PlatformCapabilityDeclaration =
                        serde_json::from_str(&old_client).map_err(|error| {
                            format!("database returned invalid OTT client JSON: {error}")
                        })?;
                    stored_client.validate()?;
                    if stored_client.client_id != column_text(existing.raw, 2)? {
                        return Err(OttAuthorityError::Corrupt);
                    }
                    let capability = unprotect_local_machine(&column_blob(existing.raw, 9)?)?;
                    let capability = String::from_utf8(capability)
                        .map_err(|_| "OTT session capability is invalid".to_string())?;
                    if !constant_time_hex_eq(
                        &format!("{:x}", Sha256::digest(capability.as_bytes())),
                        &session.server_session_digest,
                    ) {
                        return Err(OttAuthorityError::Corrupt);
                    }
                    let page = self
                        .read_ott_catalog_page()?
                        .ok_or(OttAuthorityError::Unavailable)?;
                    station_ott_contract::validate_session_issue(&session, &page, &stored_client)?;
                    return Ok(PlaybackSessionIssueResponse {
                        session,
                        session_capability: capability,
                        replayed: true,
                    });
                }
                SQLITE_DONE => {}
                _ => return Err(sqlite_error(self.database).into()),
            }
            let page = self
                .read_ott_catalog_page()?
                .ok_or(OttAuthorityError::Unavailable)?;
            let item = page
                .items
                .iter()
                .find(|item| item.content_id == request.content_id)
                .ok_or(OttAuthorityError::Unavailable)?;
            let expires = now_unix_ms
                .checked_add(SESSION_TTL_MS)
                .ok_or_else(|| "OTT session expiry overflow".to_string())?;
            let count = self.prepare("SELECT COUNT(*) FROM ott_playback_sessions")?;
            if unsafe { sqlite3_step(count.raw) } != SQLITE_ROW
                || unsafe { sqlite3_column_int64(count.raw, 0) }
                    >= i64::try_from(MAX_TOTAL_SESSIONS).unwrap_or(i64::MAX)
            {
                return Err(self.ott_capacity_retry(now_unix_ms)?);
            }
            self.consume_ott_admission_window(
                "sessions",
                now_unix_ms,
                MAX_SESSION_ISSUES_PER_WINDOW,
            )?;
            let capability = new_session_capability()?;
            let mut session = PlaybackSessionAuthority {
                contract_version: OTT_CONTRACT_VERSION,
                session_id: new_uuid()?,
                content_id: item.content_id.clone(),
                catalog_revision: page.catalog_revision,
                session_revision: 1,
                server_session_digest: String::new(),
                issued_at_unix_ms: now_unix_ms,
                expires_at_unix_ms: expires,
            };
            session.server_session_digest = format!("{:x}", Sha256::digest(capability.as_bytes()));
            let stmt = self.prepare("INSERT INTO ott_playback_sessions(session_id,request_id,content_id,client_id,catalog_revision,session_revision,server_session_digest,issued_at_unix_ms,expires_at_unix_ms,client_json,capability_ciphertext) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11)")?;
            let sid = c_string(&session.session_id, "OTT session identity")?;
            let content = c_string(&session.content_id, "OTT content identity")?;
            let client = c_string(&request.client.client_id, "OTT client identity")?;
            let digest = c_string(&session.server_session_digest, "OTT session digest")?;
            bind_text(self.database, stmt.raw, 1, &sid)?;
            bind_text(self.database, stmt.raw, 2, &request_id)?;
            bind_text(self.database, stmt.raw, 3, &content)?;
            bind_text(self.database, stmt.raw, 4, &client)?;
            let client_json_c = c_string(&client_json, "OTT client JSON")?;
            let capability_ciphertext = protect_local_machine(capability.as_bytes())?;
            bind_i64(
                self.database,
                stmt.raw,
                5,
                checked_i64(session.catalog_revision, "OTT catalog revision")?,
            )?;
            bind_i64(self.database, stmt.raw, 6, 1)?;
            bind_text(self.database, stmt.raw, 7, &digest)?;
            bind_i64(self.database, stmt.raw, 8, now_unix_ms)?;
            bind_i64(self.database, stmt.raw, 9, expires)?;
            bind_text(self.database, stmt.raw, 10, &client_json_c)?;
            bind_blob(self.database, stmt.raw, 11, &capability_ciphertext)?;
            expect_done(self.database, stmt.raw)?;
            let state = self.prepare("INSERT INTO ott_playback_session_state(session_id,last_event,last_position_ms,last_observed_at_unix_ms,last_beacon_id,last_accepted_at_unix_ms) VALUES(?1,'none',0,?2,'',?2)")?;
            bind_text(self.database, state.raw, 1, &sid)?;
            bind_i64(self.database, state.raw, 2, now_unix_ms)?;
            expect_done(self.database, state.raw)?;
            Ok(PlaybackSessionIssueResponse {
                session,
                session_capability: capability,
                replayed: false,
            })
        })();
        self.finish_ott_transaction(result)
    }

    pub fn collect_ott_playback_beacon(
        &self,
        beacon: &ClientPlaybackBeacon,
        now_unix_ms: i64,
    ) -> Result<PlaybackBeaconResponse, OttAuthorityError> {
        validate_beacon_input(beacon)?;
        if now_unix_ms < 0 {
            return Err("OTT beacon time is invalid".to_string().into());
        }
        self.execute("BEGIN IMMEDIATE")?;
        let result = (|| {
            self.reclaim_expired_ott_sessions(now_unix_ms)?;
            let existing = self.prepare("SELECT session_id,beacon_json,accepted_at_unix_ms FROM ott_playback_beacons WHERE beacon_id=?1")?;
            let beacon_id = c_string(&beacon.beacon_id, "OTT beacon identity")?;
            bind_text(self.database, existing.raw, 1, &beacon_id)?;
            match unsafe { sqlite3_step(existing.raw) } {
                SQLITE_ROW => {
                    let old_session = column_text(existing.raw, 0)?;
                    let old_json = column_text(existing.raw, 1)?;
                    let new_json = sanitized_beacon_json(beacon)?;
                    if old_session != beacon.session_id || old_json != new_json {
                        return Err(OttAuthorityError::ReplayConflict);
                    }
                    let session_stmt = self.prepare("SELECT session_id,content_id,catalog_revision,session_revision,server_session_digest,issued_at_unix_ms,expires_at_unix_ms,capability_ciphertext FROM ott_playback_sessions WHERE session_id=?1")?;
                    let old_sid = c_string(&old_session, "OTT session identity")?;
                    bind_text(self.database, session_stmt.raw, 1, &old_sid)?;
                    if unsafe { sqlite3_step(session_stmt.raw) } != SQLITE_ROW {
                        return Err(OttAuthorityError::Unavailable);
                    }
                    let session = read_session_row_without_client(session_stmt.raw)?;
                    session.validate_shape()?;
                    let capability = read_session_capability(session_stmt.raw, 7)?;
                    let mut stored_value: serde_json::Value = serde_json::from_str(&old_json)
                        .map_err(|error| {
                            format!("database returned invalid OTT beacon JSON: {error}")
                        })?;
                    stored_value
                        .as_object_mut()
                        .ok_or_else(|| "database returned invalid OTT beacon object".to_string())?
                        .insert(
                            "session_capability".into(),
                            serde_json::Value::String(capability.clone()),
                        );
                    let stored: ClientPlaybackBeacon = serde_json::from_value(stored_value)
                        .map_err(|error| {
                            format!("database returned invalid OTT beacon JSON: {error}")
                        })?;
                    validate_beacon_input(&stored)?;
                    if sanitized_beacon_json(&stored)? != new_json
                        || !constant_time_hex_eq(
                            &format!("{:x}", Sha256::digest(beacon.session_capability.as_bytes())),
                            &session.server_session_digest,
                        )
                        || beacon.session_capability != capability
                    {
                        return Err(OttAuthorityError::InvalidCapability);
                    }
                    validate_client_beacon(&stored, &session)?;
                    if now_unix_ms >= session.expires_at_unix_ms {
                        return Err(OttAuthorityError::Stale);
                    }
                    let current_catalog = self
                        .read_ott_catalog_page()?
                        .ok_or(OttAuthorityError::Unavailable)?;
                    if session.catalog_revision != current_catalog.catalog_revision
                        || !catalog_contains_content(&current_catalog, &session.content_id)
                    {
                        return Err(OttAuthorityError::Stale);
                    }
                    let state = self.prepare("SELECT last_event,last_position_ms,last_observed_at_unix_ms,last_beacon_id,last_accepted_at_unix_ms FROM ott_playback_session_state WHERE session_id=?1")?;
                    bind_text(self.database, state.raw, 1, &old_sid)?;
                    if unsafe { sqlite3_step(state.raw) } != SQLITE_ROW {
                        return Err(OttAuthorityError::Corrupt);
                    }
                    let last_event = column_text(state.raw, 0)?;
                    let last_position =
                        u64::try_from(unsafe { sqlite3_column_int64(state.raw, 1) })
                            .map_err(|_| "OTT playback state position is invalid".to_string())?;
                    let last_observed = unsafe { sqlite3_column_int64(state.raw, 2) };
                    let last_beacon_id = column_text(state.raw, 3)?;
                    let last_accepted_at = unsafe { sqlite3_column_int64(state.raw, 4) };
                    let state_snapshot = OttPlaybackState {
                        last_event: last_event.clone(),
                        last_position,
                        last_observed,
                        last_beacon_id: last_beacon_id.clone(),
                        last_accepted_at,
                    };
                    if last_observed < 0
                        || stored.observed_at_unix_ms < last_observed
                        || !replayed_beacon_is_reflected(&stored, &last_event, last_position)
                    {
                        return Err(OttAuthorityError::Corrupt);
                    }
                    validate_state_matches_latest_beacon(
                        self,
                        &old_session,
                        &session,
                        &capability,
                        &state_snapshot,
                    )?;
                    return Ok(PlaybackBeaconResponse {
                        receipt: PlaybackBeaconReceipt {
                            beacon_id: beacon.beacon_id.clone(),
                            session_id: old_session,
                            accepted_at_unix_ms: unsafe { sqlite3_column_int64(existing.raw, 2) },
                        },
                        replayed: true,
                    });
                }
                SQLITE_DONE => {}
                _ => return Err(sqlite_error(self.database).into()),
            }
            let session_stmt = self.prepare("SELECT session_id,content_id,catalog_revision,session_revision,server_session_digest,issued_at_unix_ms,expires_at_unix_ms,capability_ciphertext FROM ott_playback_sessions WHERE session_id=?1")?;
            let sid = c_string(&beacon.session_id, "OTT session identity")?;
            bind_text(self.database, session_stmt.raw, 1, &sid)?;
            if unsafe { sqlite3_step(session_stmt.raw) } != SQLITE_ROW {
                return Err(OttAuthorityError::Unavailable);
            }
            let session = read_session_row_without_client(session_stmt.raw)?;
            session.validate_shape()?;
            let capability = read_session_capability(session_stmt.raw, 7)?;
            if !constant_time_hex_eq(
                &format!("{:x}", Sha256::digest(beacon.session_capability.as_bytes())),
                &session.server_session_digest,
            ) || beacon.session_capability != capability
            {
                return Err(OttAuthorityError::InvalidCapability);
            }
            validate_client_beacon(beacon, &session)?;
            let total = self.prepare("SELECT COUNT(*) FROM ott_playback_beacons")?;
            if unsafe { sqlite3_step(total.raw) } != SQLITE_ROW
                || unsafe { sqlite3_column_int64(total.raw, 0) }
                    >= i64::try_from(MAX_TOTAL_BEACONS).unwrap_or(i64::MAX)
            {
                return Err(self.ott_capacity_retry(now_unix_ms)?);
            }
            if now_unix_ms < session.issued_at_unix_ms {
                return Err(OttAuthorityError::InvalidSequence);
            }
            if now_unix_ms >= session.expires_at_unix_ms {
                return Err(OttAuthorityError::Stale);
            }
            if beacon.observed_at_unix_ms > now_unix_ms.saturating_add(MAX_BEACON_CLOCK_SKEW_MS) {
                return Err(OttAuthorityError::InvalidSequence);
            }
            let current_catalog = self
                .read_ott_catalog_page()?
                .ok_or(OttAuthorityError::Unavailable)?;
            if session.catalog_revision != current_catalog.catalog_revision
                || !catalog_contains_content(&current_catalog, &session.content_id)
            {
                return Err(OttAuthorityError::Stale);
            }
            let state = self.prepare("SELECT last_event,last_position_ms,last_observed_at_unix_ms,last_beacon_id,last_accepted_at_unix_ms FROM ott_playback_session_state WHERE session_id=?1")?;
            bind_text(self.database, state.raw, 1, &sid)?;
            if unsafe { sqlite3_step(state.raw) } != SQLITE_ROW {
                return Err(OttAuthorityError::Corrupt);
            }
            let last_event = column_text(state.raw, 0)?;
            let last_position = u64::try_from(unsafe { sqlite3_column_int64(state.raw, 1) })
                .map_err(|_| "OTT playback state position is invalid".to_string())?;
            let last_observed = unsafe { sqlite3_column_int64(state.raw, 2) };
            let last_beacon_id = column_text(state.raw, 3)?;
            let last_accepted_at = unsafe { sqlite3_column_int64(state.raw, 4) };
            let state_snapshot = OttPlaybackState {
                last_event: last_event.clone(),
                last_position,
                last_observed,
                last_beacon_id: last_beacon_id.clone(),
                last_accepted_at,
            };
            validate_state_matches_latest_beacon(
                self,
                &beacon.session_id,
                &session,
                &capability,
                &state_snapshot,
            )?;
            let event_name = match beacon.event {
                PlaybackBeaconEvent::Started => "started",
                PlaybackBeaconEvent::Progress => "progress",
                PlaybackBeaconEvent::Completed => "completed",
            };
            let allowed = matches!(
                (last_event.as_str(), beacon.event),
                ("none", PlaybackBeaconEvent::Started)
                    | (
                        "started" | "progress",
                        PlaybackBeaconEvent::Progress | PlaybackBeaconEvent::Completed
                    )
            );
            if last_observed < 0
                || beacon.observed_at_unix_ms < last_observed
                || !allowed
                || beacon.position_ms < last_position
                || (matches!(beacon.event, PlaybackBeaconEvent::Started) && beacon.position_ms != 0)
            {
                return Err(OttAuthorityError::InvalidSequence);
            }
            if last_event != "none"
                && now_unix_ms < last_accepted_at.saturating_add(MIN_BEACON_INTERVAL_MS)
            {
                return Err(OttAuthorityError::RateLimited(retry_after_seconds(
                    now_unix_ms,
                    last_accepted_at.saturating_add(MIN_BEACON_INTERVAL_MS),
                )));
            }
            let count =
                self.prepare("SELECT COUNT(*) FROM ott_playback_beacons WHERE session_id=?1")?;
            bind_text(self.database, count.raw, 1, &sid)?;
            if unsafe { sqlite3_step(count.raw) } != SQLITE_ROW
                || unsafe { sqlite3_column_int64(count.raw, 0) }
                    >= i64::from(MAX_BEACONS_PER_SESSION)
            {
                return Err(OttAuthorityError::RateLimited(retry_after_seconds(
                    now_unix_ms,
                    session.expires_at_unix_ms,
                )));
            }
            self.consume_ott_admission_window("beacons", now_unix_ms, MAX_BEACONS_PER_WINDOW)?;
            let beacon_json = sanitized_beacon_json(beacon)?;
            let stmt = self.prepare("INSERT INTO ott_playback_beacons(beacon_id,session_id,beacon_json,accepted_at_unix_ms) VALUES(?1,?2,?3,?4)")?;
            let json = c_string(&beacon_json, "OTT beacon JSON")?;
            bind_text(self.database, stmt.raw, 1, &beacon_id)?;
            bind_text(self.database, stmt.raw, 2, &sid)?;
            bind_text(self.database, stmt.raw, 3, &json)?;
            bind_i64(self.database, stmt.raw, 4, now_unix_ms)?;
            expect_done(self.database, stmt.raw)?;
            let state = self.prepare("UPDATE ott_playback_session_state SET last_event=?1,last_position_ms=?2,last_observed_at_unix_ms=?3,last_beacon_id=?4,last_accepted_at_unix_ms=?5 WHERE session_id=?6")?;
            let event_name = c_string(event_name, "OTT beacon event")?;
            bind_text(self.database, state.raw, 1, &event_name)?;
            bind_i64(
                self.database,
                state.raw,
                2,
                checked_i64(beacon.position_ms, "OTT beacon position")?,
            )?;
            bind_i64(self.database, state.raw, 3, beacon.observed_at_unix_ms)?;
            let beacon_id_c = c_string(&beacon.beacon_id, "OTT beacon identity")?;
            bind_text(self.database, state.raw, 4, &beacon_id_c)?;
            bind_i64(self.database, state.raw, 5, now_unix_ms)?;
            bind_text(self.database, state.raw, 6, &sid)?;
            expect_done(self.database, state.raw)?;
            Ok(PlaybackBeaconResponse {
                receipt: PlaybackBeaconReceipt {
                    beacon_id: beacon.beacon_id.clone(),
                    session_id: beacon.session_id.clone(),
                    accepted_at_unix_ms: now_unix_ms,
                },
                replayed: false,
            })
        })();
        self.finish_ott_transaction(result)
    }
}

impl StationStore {
    fn finish_ott_transaction<T>(
        &self,
        outcome: Result<T, OttAuthorityError>,
    ) -> Result<T, OttAuthorityError> {
        match outcome {
            Ok(value) => match self.execute("COMMIT") {
                Ok(()) => Ok(value),
                Err(_) => {
                    let _ = self.execute("ROLLBACK");
                    Err(OttAuthorityError::Internal)
                }
            },
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    fn reclaim_expired_ott_sessions(&self, now_unix_ms: i64) -> Result<u64, OttAuthorityError> {
        let cutoff = now_unix_ms.saturating_sub(SESSION_RETENTION_AFTER_EXPIRY_MS);
        if cutoff <= 0 {
            return Ok(0);
        }
        let beacons = self.prepare("DELETE FROM ott_playback_beacons WHERE rowid IN (SELECT beacon.rowid FROM ott_playback_beacons beacon JOIN ott_playback_sessions session ON session.session_id=beacon.session_id WHERE session.expires_at_unix_ms<=?1 ORDER BY session.expires_at_unix_ms,session.session_id,beacon.rowid LIMIT ?2)")?;
        bind_i64(self.database, beacons.raw, 1, cutoff)?;
        bind_i64(
            self.database,
            beacons.raw,
            2,
            i64::from(MAX_RECLAIMED_BEACONS_PER_REQUEST),
        )?;
        expect_done(self.database, beacons.raw)?;

        let session_limit = i64::from(MAX_RECLAIMED_SESSIONS_PER_REQUEST);
        let state = self.prepare("DELETE FROM ott_playback_session_state WHERE session_id IN (SELECT session.session_id FROM ott_playback_sessions session WHERE session.expires_at_unix_ms<=?1 AND NOT EXISTS (SELECT 1 FROM ott_playback_beacons beacon WHERE beacon.session_id=session.session_id) ORDER BY session.expires_at_unix_ms,session.session_id LIMIT ?2)")?;
        bind_i64(self.database, state.raw, 1, cutoff)?;
        bind_i64(self.database, state.raw, 2, session_limit)?;
        expect_done(self.database, state.raw)?;

        let sessions = self.prepare("DELETE FROM ott_playback_sessions WHERE session_id IN (SELECT session.session_id FROM ott_playback_sessions session WHERE session.expires_at_unix_ms<=?1 AND NOT EXISTS (SELECT 1 FROM ott_playback_beacons beacon WHERE beacon.session_id=session.session_id) AND NOT EXISTS (SELECT 1 FROM ott_playback_session_state state WHERE state.session_id=session.session_id) ORDER BY session.expires_at_unix_ms,session.session_id LIMIT ?2)")?;
        bind_i64(self.database, sessions.raw, 1, cutoff)?;
        bind_i64(self.database, sessions.raw, 2, session_limit)?;
        expect_done(self.database, sessions.raw)?;
        u64::try_from(self.changes()).map_err(|_| OttAuthorityError::Internal)
    }

    fn consume_ott_admission_window(
        &self,
        bucket: &str,
        now_unix_ms: i64,
        maximum: u32,
    ) -> Result<(), OttAuthorityError> {
        let bucket_c = c_string(bucket, "OTT admission bucket")?;
        let statement = self.prepare("SELECT window_started_at_unix_ms,accepted_count FROM ott_admission_windows WHERE bucket=?1")?;
        bind_text(self.database, statement.raw, 1, &bucket_c)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_DONE => {
                let insert = self.prepare("INSERT INTO ott_admission_windows(bucket,window_started_at_unix_ms,accepted_count) VALUES(?1,?2,1)")?;
                bind_text(self.database, insert.raw, 1, &bucket_c)?;
                bind_i64(self.database, insert.raw, 2, now_unix_ms)?;
                expect_done(self.database, insert.raw)?;
                Ok(())
            }
            SQLITE_ROW => {
                let started = unsafe { sqlite3_column_int64(statement.raw, 0) };
                let count = unsafe { sqlite3_column_int64(statement.raw, 1) };
                if started < 0 || count < 1 || count > i64::from(maximum) || now_unix_ms < started {
                    return Err(OttAuthorityError::Corrupt);
                }
                let reset_at = started
                    .checked_add(ADMISSION_WINDOW_MS)
                    .ok_or(OttAuthorityError::Corrupt)?;
                let update = if now_unix_ms >= reset_at {
                    self.prepare("UPDATE ott_admission_windows SET window_started_at_unix_ms=?1,accepted_count=1 WHERE bucket=?2")?
                } else {
                    if count >= i64::from(maximum) {
                        return Err(OttAuthorityError::RateLimited(retry_after_seconds(
                            now_unix_ms,
                            reset_at,
                        )));
                    }
                    self.prepare("UPDATE ott_admission_windows SET accepted_count=accepted_count+1 WHERE window_started_at_unix_ms=?1 AND bucket=?2")?
                };
                bind_i64(
                    self.database,
                    update.raw,
                    1,
                    if now_unix_ms >= reset_at {
                        now_unix_ms
                    } else {
                        started
                    },
                )?;
                bind_text(self.database, update.raw, 2, &bucket_c)?;
                expect_done(self.database, update.raw)?;
                if self.changes() != 1 {
                    return Err(OttAuthorityError::Corrupt);
                }
                Ok(())
            }
            _ => Err(OttAuthorityError::Internal),
        }
    }

    fn ott_capacity_retry(&self, now_unix_ms: i64) -> Result<OttAuthorityError, OttAuthorityError> {
        let statement =
            self.prepare("SELECT COUNT(*),MIN(expires_at_unix_ms) FROM ott_playback_sessions")?;
        if unsafe { sqlite3_step(statement.raw) } != SQLITE_ROW
            || unsafe { sqlite3_column_int64(statement.raw, 0) } < 1
        {
            return Err(OttAuthorityError::Corrupt);
        }
        let earliest_expiry = unsafe { sqlite3_column_int64(statement.raw, 1) };
        let retry_at = earliest_expiry
            .checked_add(SESSION_RETENTION_AFTER_EXPIRY_MS)
            .ok_or(OttAuthorityError::Corrupt)?;
        Ok(OttAuthorityError::RateLimited(retry_after_seconds(
            now_unix_ms,
            retry_at,
        )))
    }
}

fn retry_after_seconds(now_unix_ms: i64, retry_at_unix_ms: i64) -> u64 {
    let remaining = retry_at_unix_ms.saturating_sub(now_unix_ms).max(1);
    u64::try_from(remaining.saturating_add(999) / 1_000).unwrap_or(u64::MAX)
}

fn read_session_row(statement: *mut SqliteStatement) -> Result<PlaybackSessionAuthority, String> {
    Ok(PlaybackSessionAuthority {
        contract_version: OTT_CONTRACT_VERSION,
        session_id: column_text(statement, 0)?,
        content_id: column_text(statement, 1)?,
        catalog_revision: column_u64(statement, 3, "OTT catalog revision")?,
        session_revision: column_u64(statement, 4, "OTT session revision")?,
        server_session_digest: column_text(statement, 5)?,
        issued_at_unix_ms: unsafe { sqlite3_column_int64(statement, 6) },
        expires_at_unix_ms: unsafe { sqlite3_column_int64(statement, 7) },
    })
}

fn read_session_row_without_client(
    statement: *mut SqliteStatement,
) -> Result<PlaybackSessionAuthority, String> {
    Ok(PlaybackSessionAuthority {
        contract_version: OTT_CONTRACT_VERSION,
        session_id: column_text(statement, 0)?,
        content_id: column_text(statement, 1)?,
        catalog_revision: column_u64(statement, 2, "OTT catalog revision")?,
        session_revision: column_u64(statement, 3, "OTT session revision")?,
        server_session_digest: column_text(statement, 4)?,
        issued_at_unix_ms: unsafe { sqlite3_column_int64(statement, 5) },
        expires_at_unix_ms: unsafe { sqlite3_column_int64(statement, 6) },
    })
}

fn validate_client_beacon(
    beacon: &ClientPlaybackBeacon,
    session: &PlaybackSessionAuthority,
) -> Result<(), String> {
    station_ott_contract::validate_client_beacon(beacon, session)
}

fn read_session_capability(
    statement: *mut SqliteStatement,
    column: c_int,
) -> Result<String, String> {
    let capability = String::from_utf8(unprotect_local_machine(&column_blob(statement, column)?)?)
        .map_err(|_| "database returned invalid OTT session capability".to_string())?;
    if capability.len() != 64
        || !capability
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err("database returned an invalid OTT session capability shape".to_string());
    }
    Ok(capability)
}

fn constant_time_hex_eq(left: &str, right: &str) -> bool {
    left.as_bytes().ct_eq(right.as_bytes()).unwrap_u8() == 1
}

fn catalog_contains_content(catalog: &PublicCatalogPage, content_id: &str) -> bool {
    catalog
        .items
        .iter()
        .any(|item| item.content_id == content_id)
}

fn sanitized_beacon_json(beacon: &ClientPlaybackBeacon) -> Result<String, String> {
    let mut value =
        serde_json::to_value(beacon).map_err(|error| format!("serialize OTT beacon: {error}"))?;
    value
        .as_object_mut()
        .ok_or_else(|| "OTT beacon must serialize as an object".to_string())?
        .remove("session_capability");
    serde_json::to_string(&value).map_err(|error| format!("serialize OTT beacon: {error}"))
}

/// The mutable state row is only an index/cache. Before accepting either a
/// replay or a new event, bind it to the newest immutable beacon append. This
/// makes tampering with state fail closed instead of becoming the baseline for
/// the next beacon.
fn validate_state_matches_latest_beacon(
    store: &StationStore,
    session_id: &str,
    session: &PlaybackSessionAuthority,
    capability: &str,
    state: &OttPlaybackState,
) -> Result<(), OttAuthorityError> {
    let statement = store.prepare(
        "SELECT beacon_id,beacon_json,accepted_at_unix_ms FROM ott_playback_beacons WHERE session_id=?1 ORDER BY rowid DESC LIMIT 1",
    )?;
    let session_id_c = c_string(session_id, "OTT session identity")?;
    bind_text(store.database, statement.raw, 1, &session_id_c)?;
    if unsafe { sqlite3_step(statement.raw) } == SQLITE_DONE {
        if state.last_event == "none"
            && state.last_position == 0
            && state.last_observed == session.issued_at_unix_ms
            && state.last_beacon_id.is_empty()
            && state.last_accepted_at == session.issued_at_unix_ms
        {
            return Ok(());
        }
        return Err(OttAuthorityError::Corrupt);
    }
    let beacon_id = column_text(statement.raw, 0)?;
    let sanitized_json = column_text(statement.raw, 1)?;
    let accepted_at = unsafe { sqlite3_column_int64(statement.raw, 2) };
    let mut value: serde_json::Value =
        serde_json::from_str(&sanitized_json).map_err(|_| OttAuthorityError::Corrupt)?;
    value
        .as_object_mut()
        .ok_or(OttAuthorityError::Corrupt)?
        .insert(
            "session_capability".into(),
            serde_json::Value::String(capability.to_owned()),
        );
    let latest: ClientPlaybackBeacon =
        serde_json::from_value(value).map_err(|_| OttAuthorityError::Corrupt)?;
    validate_beacon_input(&latest).map_err(|_| OttAuthorityError::Corrupt)?;
    validate_client_beacon(&latest, session).map_err(|_| OttAuthorityError::Corrupt)?;
    if sanitized_beacon_json(&latest).map_err(|_| OttAuthorityError::Corrupt)? != sanitized_json
        || latest.session_id != session_id
        || latest.beacon_id != beacon_id
        || latest.session_capability != capability
        || !constant_time_hex_eq(
            &format!("{:x}", Sha256::digest(capability.as_bytes())),
            &session.server_session_digest,
        )
        || latest.position_ms != state.last_position
        || latest.observed_at_unix_ms != state.last_observed
        || !replayed_beacon_is_reflected(&latest, &state.last_event, state.last_position)
        || event_name(latest.event) != state.last_event
        || state.last_beacon_id != beacon_id
        || state.last_accepted_at != accepted_at
    {
        return Err(OttAuthorityError::Corrupt);
    }
    Ok(())
}

fn event_name(event: PlaybackBeaconEvent) -> &'static str {
    match event {
        PlaybackBeaconEvent::Started => "started",
        PlaybackBeaconEvent::Progress => "progress",
        PlaybackBeaconEvent::Completed => "completed",
    }
}

fn replayed_beacon_is_reflected(
    beacon: &ClientPlaybackBeacon,
    last_event: &str,
    last_position: u64,
) -> bool {
    let event_reflected = matches!(
        (beacon.event, last_event),
        (
            PlaybackBeaconEvent::Started,
            "started" | "progress" | "completed"
        ) | (PlaybackBeaconEvent::Progress, "progress" | "completed")
            | (PlaybackBeaconEvent::Completed, "completed")
    );
    event_reflected && last_position >= beacon.position_ms
}

#[cfg(test)]
mod tests {
    use super::*;
    use station_ott_contract::{
        AccessibilitySemantics, OttCapability, OttPlatform, PlaybackProtocol, PlaybackSource,
        PublicCatalogItem, PublicContentIdentity, PublicSubtitleTrack, SubtitleKind,
    };
    use std::time::{SystemTime, UNIX_EPOCH};

    const CHANNEL: &str = "11111111-1111-4111-8111-111111111111";
    const CONTENT: &str = "22222222-2222-4222-8222-222222222222";
    const TRACK: &str = "33333333-3333-4333-8333-333333333333";
    const CLIENT: &str = "44444444-4444-4444-8444-444444444444";

    fn catalog() -> PublicCatalogPage {
        PublicCatalogPage {
            contract_version: OTT_CONTRACT_VERSION,
            catalog_revision: 1,
            items: vec![PublicCatalogItem {
                content_id: CONTENT.into(),
                identity: PublicContentIdentity::Live {
                    channel_id: CHANNEL.into(),
                },
                title: "Council meeting".into(),
                summary: "Accessible civic programming".into(),
                vod_duration_ms: None,
                playback: PlaybackSource {
                    protocol: PlaybackProtocol::Hls,
                    manifest_url: "https://cdn.example.test/live.m3u8".into(),
                },
                subtitles: vec![PublicSubtitleTrack {
                    track_id: TRACK.into(),
                    language: "en-US".into(),
                    kind: SubtitleKind::ClosedCaptions,
                    url: "https://cdn.example.test/live.vtt".into(),
                    is_default: true,
                }],
                accessibility: AccessibilitySemantics {
                    captions_available: true,
                    screen_reader_summary: "Council meeting".into(),
                    remote_navigation_supported: true,
                },
            }],
            next_cursor: None,
        }
    }

    fn client() -> PlatformCapabilityDeclaration {
        PlatformCapabilityDeclaration {
            contract_version: OTT_CONTRACT_VERSION,
            client_id: CLIENT.into(),
            platform: OttPlatform::Roku,
            capabilities: vec![
                OttCapability::HlsPlayback,
                OttCapability::WebVttSubtitles,
                OttCapability::ClosedCaptions,
                OttCapability::ScreenReaderSemantics,
                OttCapability::KeyboardOrRemoteNavigation,
                OttCapability::PlaybackBeacons,
            ],
        }
    }

    fn test_store() -> (StationStore, std::path::PathBuf) {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!("townlight-ott-{nonce}.db"));
        (StationStore::open(&path).unwrap(), path)
    }

    #[test]
    fn server_owns_session_and_beacon_replay_is_exact() {
        let (store, path) = test_store();
        store.write_ott_catalog_page(&catalog(), "test", 1).unwrap();
        let request = PlaybackSessionIssueRequest {
            request_id: "request-1".into(),
            content_id: CONTENT.into(),
            client: client(),
        };
        let issued = store
            .issue_ott_playback_session(&request, 1_700_000_000_000)
            .unwrap();
        assert!(!issued.replayed);
        assert_eq!(issued.session.catalog_revision, 1);
        assert_eq!(issued.session.issued_at_unix_ms, 1_700_000_000_000);
        let replay = store
            .issue_ott_playback_session(&request, 1_700_000_001_000)
            .unwrap();
        assert!(replay.replayed);
        assert_eq!(replay.session.session_id, issued.session.session_id);
        assert_eq!(
            store
                .issue_ott_playback_session(&request, issued.session.expires_at_unix_ms)
                .unwrap_err(),
            OttAuthorityError::Stale
        );
        let mut conflicting = request.clone();
        conflicting.content_id = CHANNEL.into();
        assert!(
            store
                .issue_ott_playback_session(&conflicting, 1_700_000_001_000)
                .is_err()
        );

        let beacon = ClientPlaybackBeacon {
            contract_version: OTT_CONTRACT_VERSION,
            beacon_id: "55555555-5555-4555-8555-555555555555".into(),
            session_id: issued.session.session_id.clone(),
            session_revision: issued.session.session_revision,
            session_capability: issued.session_capability.clone(),
            observed_at_unix_ms: issued.session.issued_at_unix_ms + 1,
            event: PlaybackBeaconEvent::Started,
            position_ms: 0,
        };
        let accepted = store
            .collect_ott_playback_beacon(&beacon, issued.session.issued_at_unix_ms + 2)
            .unwrap();
        assert!(!accepted.replayed);
        let replayed = store
            .collect_ott_playback_beacon(&beacon, issued.session.issued_at_unix_ms + 3)
            .unwrap();
        assert!(replayed.replayed);
        assert_eq!(accepted.receipt, replayed.receipt);
        let mut forged = beacon.clone();
        forged.session_id = CHANNEL.into();
        assert!(
            store
                .collect_ott_playback_beacon(&forged, issued.session.issued_at_unix_ms + 4)
                .is_err()
        );
        let mut expired = beacon;
        expired.beacon_id = "66666666-6666-4666-8666-666666666666".into();
        expired.observed_at_unix_ms = issued.session.expires_at_unix_ms;
        assert!(
            store
                .collect_ott_playback_beacon(&expired, issued.session.expires_at_unix_ms)
                .is_err()
        );
        drop(store);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn catalog_history_without_current_row_fails_closed() {
        let (store, path) = test_store();
        store.write_ott_catalog_page(&catalog(), "test", 1).unwrap();
        store.execute("DELETE FROM ott_catalog_current").unwrap();
        let error = store.read_ott_catalog_page().unwrap_err();
        assert_eq!(error, OttAuthorityError::Corrupt);
        drop(store);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn single_page_catalog_rejects_a_dead_continuation_cursor() {
        let (store, path) = test_store();
        let mut page = catalog();
        page.next_cursor = Some("next-page".into());
        let error = store.write_ott_catalog_page(&page, "test", 1).unwrap_err();
        assert_eq!(error, OttAuthorityError::Internal);
        drop(store);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn ott_authority_rejects_corrupt_rows_forged_bearers_and_invalid_state() {
        let (store, path) = test_store();
        store.write_ott_catalog_page(&catalog(), "test", 1).unwrap();

        let request = |id: &str| PlaybackSessionIssueRequest {
            request_id: id.into(),
            content_id: CONTENT.into(),
            client: client(),
        };
        let issued = store
            .issue_ott_playback_session(&request("corruption-1"), 1_700_000_000_000)
            .unwrap();
        let mut forged = ClientPlaybackBeacon {
            contract_version: OTT_CONTRACT_VERSION,
            beacon_id: "77777777-7777-4777-8777-777777777777".into(),
            session_id: issued.session.session_id.clone(),
            session_revision: issued.session.session_revision,
            session_capability: "a".repeat(64),
            observed_at_unix_ms: issued.session.issued_at_unix_ms + 1,
            event: PlaybackBeaconEvent::Started,
            position_ms: 0,
        };
        assert!(
            store
                .collect_ott_playback_beacon(&forged, issued.session.issued_at_unix_ms + 2)
                .is_err()
        );

        forged.session_capability = issued.session_capability.clone();
        store
            .collect_ott_playback_beacon(&forged, issued.session.issued_at_unix_ms + 2)
            .unwrap();
        let beacon_statement = store
            .prepare("SELECT beacon_json FROM ott_playback_beacons WHERE beacon_id=?1")
            .unwrap();
        let beacon_id = c_string(&forged.beacon_id, "beacon id").unwrap();
        bind_text(store.database, beacon_statement.raw, 1, &beacon_id).unwrap();
        assert_eq!(unsafe { sqlite3_step(beacon_statement.raw) }, SQLITE_ROW);
        let stored_json = column_text(beacon_statement.raw, 0).unwrap();
        assert!(!stored_json.contains("session_capability"));

        let issued_digest = store
            .issue_ott_playback_session(&request("corruption-digest"), 1_700_000_000_050)
            .unwrap();
        store
            .execute("DROP TRIGGER ott_playback_sessions_no_update; UPDATE ott_playback_sessions SET server_session_digest=hex(zeroblob(32)) WHERE request_id='corruption-digest';")
            .unwrap();
        assert!(
            store
                .issue_ott_playback_session(&request("corruption-digest"), 1_700_000_000_051)
                .is_err()
        );
        store
            .execute("CREATE TRIGGER ott_playback_sessions_no_update BEFORE UPDATE ON ott_playback_sessions BEGIN SELECT RAISE(ABORT, 'OTT playback sessions are immutable'); END;")
            .unwrap();
        drop(issued_digest);

        let issued_cipher = store
            .issue_ott_playback_session(&request("corruption-cipher"), 1_700_000_000_060)
            .unwrap();
        store
            .execute("DROP TRIGGER ott_playback_sessions_no_update; UPDATE ott_playback_sessions SET capability_ciphertext=X'00' WHERE request_id='corruption-cipher';")
            .unwrap();
        assert!(
            store
                .issue_ott_playback_session(&request("corruption-cipher"), 1_700_000_000_061)
                .is_err()
        );
        store
            .execute("CREATE TRIGGER ott_playback_sessions_no_update BEFORE UPDATE ON ott_playback_sessions BEGIN SELECT RAISE(ABORT, 'OTT playback sessions are immutable'); END;")
            .unwrap();
        drop(issued_cipher);

        store.execute("DROP TRIGGER ott_playback_beacons_no_update; UPDATE ott_playback_beacons SET beacon_json='{}' WHERE beacon_id='77777777-7777-4777-8777-777777777777';").unwrap();
        assert!(
            store
                .collect_ott_playback_beacon(&forged, issued.session.issued_at_unix_ms + 3)
                .is_err()
        );

        let issued_state = store
            .issue_ott_playback_session(&request("corruption-state"), 1_700_000_000_100)
            .unwrap();
        let mut state_beacon = forged.clone();
        state_beacon.beacon_id = "88888888-8888-4888-8888-888888888888".into();
        state_beacon.session_id = issued_state.session.session_id.clone();
        state_beacon.session_revision = issued_state.session.session_revision;
        state_beacon.session_capability = issued_state.session_capability.clone();
        state_beacon.observed_at_unix_ms = issued_state.session.issued_at_unix_ms + 1;
        store
            .collect_ott_playback_beacon(&state_beacon, issued_state.session.issued_at_unix_ms + 2)
            .unwrap();
        let state_corrupt = format!(
            "UPDATE ott_playback_session_state SET last_event='none' WHERE session_id='{}'",
            issued_state.session.session_id
        );
        store.execute(&state_corrupt).unwrap();
        assert!(
            store
                .collect_ott_playback_beacon(
                    &state_beacon,
                    issued_state.session.issued_at_unix_ms + 3
                )
                .is_err()
        );
        state_beacon.beacon_id = "99999999-8888-4888-8888-888888888888".into();
        assert_eq!(
            store
                .collect_ott_playback_beacon(
                    &state_beacon,
                    issued_state.session.issued_at_unix_ms + 3
                )
                .unwrap_err(),
            OttAuthorityError::Corrupt
        );
        store.execute(&format!(
            "UPDATE ott_playback_session_state SET last_event='started',last_position_ms=99,last_observed_at_unix_ms={} WHERE session_id='{}'",
            issued_state.session.issued_at_unix_ms + 99,
            issued_state.session.session_id
        )).unwrap();
        state_beacon.beacon_id = "aaaaaaaa-8888-4888-8888-888888888888".into();
        assert_eq!(
            store
                .collect_ott_playback_beacon(
                    &state_beacon,
                    issued_state.session.issued_at_unix_ms + 4
                )
                .unwrap_err(),
            OttAuthorityError::Corrupt
        );
        drop(store);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn ott_beacon_events_are_monotonic_and_clock_bounded() {
        let (store, path) = test_store();
        store.write_ott_catalog_page(&catalog(), "test", 1).unwrap();
        let request = PlaybackSessionIssueRequest {
            request_id: "events".into(),
            content_id: CONTENT.into(),
            client: client(),
        };
        let issued = store
            .issue_ott_playback_session(&request, 1_700_000_000_000)
            .unwrap();
        let mut beacon = ClientPlaybackBeacon {
            contract_version: OTT_CONTRACT_VERSION,
            beacon_id: "99999999-9999-4999-8999-999999999999".into(),
            session_id: issued.session.session_id.clone(),
            session_revision: 1,
            session_capability: issued.session_capability.clone(),
            observed_at_unix_ms: issued.session.issued_at_unix_ms + 1,
            event: PlaybackBeaconEvent::Started,
            position_ms: 0,
        };
        let mut before_issue = beacon.clone();
        before_issue.beacon_id = "88888888-9999-4999-8999-999999999999".into();
        assert_eq!(
            store
                .collect_ott_playback_beacon(&before_issue, issued.session.issued_at_unix_ms - 1)
                .unwrap_err(),
            OttAuthorityError::InvalidSequence
        );
        store
            .collect_ott_playback_beacon(&beacon, issued.session.issued_at_unix_ms + 2)
            .unwrap();
        beacon.beacon_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".into();
        beacon.event = PlaybackBeaconEvent::Progress;
        beacon.position_ms = 100;
        beacon.observed_at_unix_ms = issued.session.issued_at_unix_ms + 1_001;
        store
            .collect_ott_playback_beacon(&beacon, issued.session.issued_at_unix_ms + 1_002)
            .unwrap();
        beacon.beacon_id = "abababab-abab-4aba-8aba-abababababab".into();
        beacon.position_ms = 101;
        beacon.observed_at_unix_ms = issued.session.issued_at_unix_ms + 1;
        assert!(
            store
                .collect_ott_playback_beacon(&beacon, issued.session.issued_at_unix_ms + 2_002)
                .is_err()
        );
        beacon.beacon_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb".into();
        beacon.position_ms = 99;
        beacon.observed_at_unix_ms = issued.session.issued_at_unix_ms + 2_001;
        assert!(
            store
                .collect_ott_playback_beacon(&beacon, issued.session.issued_at_unix_ms + 2_002)
                .is_err()
        );
        beacon.beacon_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc".into();
        beacon.event = PlaybackBeaconEvent::Completed;
        beacon.position_ms = 100;
        store
            .collect_ott_playback_beacon(&beacon, issued.session.issued_at_unix_ms + 2_003)
            .unwrap();
        beacon.beacon_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd".into();
        beacon.event = PlaybackBeaconEvent::Progress;
        beacon.position_ms = 101;
        assert!(
            store
                .collect_ott_playback_beacon(&beacon, issued.session.issued_at_unix_ms + 3_003)
                .is_err()
        );
        beacon.beacon_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee".into();
        beacon.event = PlaybackBeaconEvent::Completed;
        beacon.observed_at_unix_ms =
            issued.session.issued_at_unix_ms + MAX_BEACON_CLOCK_SKEW_MS + 2;
        assert!(
            store
                .collect_ott_playback_beacon(&beacon, issued.session.issued_at_unix_ms + 3_004)
                .is_err()
        );
        drop(store);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn ott_session_and_beacon_global_capacity_boundaries_fail_closed() {
        let (store, path) = test_store();
        store.write_ott_catalog_page(&catalog(), "test", 1).unwrap();
        let session_request = PlaybackSessionIssueRequest {
            request_id: "capacity-session".into(),
            content_id: CONTENT.into(),
            client: client(),
        };
        let session_rows = format!(
            "WITH RECURSIVE seq(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM seq WHERE x < {count}) INSERT INTO ott_playback_sessions(session_id,request_id,content_id,client_id,catalog_revision,session_revision,server_session_digest,issued_at_unix_ms,expires_at_unix_ms,client_json,capability_ciphertext) SELECT printf('%08d-0000-4000-8000-000000000000',x),printf('capacity-%d',x),'{{}}','{{}}',1,1,printf('%064d',0),1,9999999999999,'{{}}',X'00' FROM seq",
            count = MAX_TOTAL_SESSIONS
        );
        store.execute(&session_rows).unwrap();
        assert!(matches!(
            store.issue_ott_playback_session(&session_request, 1_700_000_000_000),
            Err(OttAuthorityError::RateLimited(_))
        ));
        drop(store);
        let _ = std::fs::remove_file(path);

        let (store, path) = test_store();
        store.write_ott_catalog_page(&catalog(), "test", 1).unwrap();
        let issue = PlaybackSessionIssueRequest {
            request_id: "capacity-beacon-session".into(),
            content_id: CONTENT.into(),
            client: client(),
        };
        let issued = store
            .issue_ott_playback_session(&issue, 1_700_000_000_000)
            .unwrap();
        let beacon_rows = format!(
            "WITH RECURSIVE seq(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM seq WHERE x < {count}) INSERT INTO ott_playback_beacons(beacon_id,session_id,beacon_json,accepted_at_unix_ms) SELECT printf('%08d-5555-4555-8555-555555555555',x),'{session_id}','{{}}',1 FROM seq",
            count = MAX_TOTAL_BEACONS,
            session_id = issued.session.session_id
        );
        store.execute(&beacon_rows).unwrap();
        let beacon = ClientPlaybackBeacon {
            contract_version: OTT_CONTRACT_VERSION,
            beacon_id: "ffffffff-ffff-4fff-8fff-ffffffffffff".into(),
            session_id: issued.session.session_id,
            session_revision: 1,
            session_capability: issued.session_capability,
            observed_at_unix_ms: 1_700_000_000_001,
            event: PlaybackBeaconEvent::Started,
            position_ms: 0,
        };
        assert!(matches!(
            store.collect_ott_playback_beacon(&beacon, 1_700_000_000_002),
            Err(OttAuthorityError::RateLimited(_))
        ));
        drop(store);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn expired_session_reclamation_is_bounded_and_never_deletes_live_sessions() {
        let (store, path) = test_store();
        store.write_ott_catalog_page(&catalog(), "test", 1).unwrap();
        let expired_rows = "WITH RECURSIVE seq(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM seq WHERE x < 300) INSERT INTO ott_playback_sessions(session_id,request_id,content_id,client_id,catalog_revision,session_revision,server_session_digest,issued_at_unix_ms,expires_at_unix_ms,client_json,capability_ciphertext) SELECT printf('%08d-1000-4000-8000-000000000000',x),printf('expired-%d',x),'22222222-2222-4222-8222-222222222222','44444444-4444-4444-8444-444444444444',1,1,printf('%064d',0),1,2,'{}',X'00' FROM seq";
        store.execute(expired_rows).unwrap();
        store.execute("INSERT INTO ott_playback_sessions(session_id,request_id,content_id,client_id,catalog_revision,session_revision,server_session_digest,issued_at_unix_ms,expires_at_unix_ms,client_json,capability_ciphertext) VALUES('99999999-1000-4000-8000-000000000000','live-row','22222222-2222-4222-8222-222222222222','44444444-4444-4444-8444-444444444444',1,1,printf('%064d',0),1,9999999999999,'{}',X'00')").unwrap();
        let request = PlaybackSessionIssueRequest {
            request_id: "after-cleanup".into(),
            content_id: CONTENT.into(),
            client: client(),
        };
        store
            .issue_ott_playback_session(&request, 1_700_000_000_000)
            .unwrap();
        let count = store
            .prepare("SELECT COUNT(*) FROM ott_playback_sessions")
            .unwrap();
        assert_eq!(unsafe { sqlite3_step(count.raw) }, SQLITE_ROW);
        assert_eq!(
            unsafe { sqlite3_column_int64(count.raw, 0) },
            302 - i64::from(MAX_RECLAIMED_SESSIONS_PER_REQUEST)
        );
        let live = store
            .prepare("SELECT 1 FROM ott_playback_sessions WHERE request_id='live-row'")
            .unwrap();
        assert_eq!(unsafe { sqlite3_step(live.raw) }, SQLITE_ROW);
        drop(store);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn session_request_replay_is_stable_until_retention_then_can_be_reissued() {
        let (store, path) = test_store();
        store.write_ott_catalog_page(&catalog(), "test", 1).unwrap();
        let request = PlaybackSessionIssueRequest {
            request_id: "retained-replay".into(),
            content_id: CONTENT.into(),
            client: client(),
        };
        let first = store.issue_ott_playback_session(&request, 1_000).unwrap();
        let replay = store.issue_ott_playback_session(&request, 2_000).unwrap();
        assert_eq!(first.session.session_id, replay.session.session_id);
        assert_eq!(
            store
                .issue_ott_playback_session(&request, first.session.expires_at_unix_ms)
                .unwrap_err(),
            OttAuthorityError::Stale
        );
        let after_retention = store
            .issue_ott_playback_session(&request, first.session.expires_at_unix_ms + 86_400_000)
            .unwrap();
        assert_ne!(first.session.session_id, after_retention.session.session_id);
        drop(store);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn reclamation_bounds_beacon_work_and_removes_parent_only_after_children() {
        let (store, path) = test_store();
        store.write_ott_catalog_page(&catalog(), "test", 1).unwrap();
        let session_id = "77777777-1000-4000-8000-000000000000";
        store.execute(&format!("INSERT INTO ott_playback_sessions(session_id,request_id,content_id,client_id,catalog_revision,session_revision,server_session_digest,issued_at_unix_ms,expires_at_unix_ms,client_json,capability_ciphertext) VALUES('{session_id}','reclaim-with-beacons','{CONTENT}','{CLIENT}',1,1,printf('%064d',0),1,2,'{{}}',X'00'); INSERT INTO ott_playback_session_state(session_id,last_event,last_position_ms,last_observed_at_unix_ms,last_beacon_id,last_accepted_at_unix_ms) VALUES('{session_id}','none',0,1,'',1);")).unwrap();
        store.execute(&format!("WITH RECURSIVE seq(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM seq WHERE x < {count}) INSERT INTO ott_playback_beacons(beacon_id,session_id,beacon_json,accepted_at_unix_ms) SELECT printf('%08d-7777-4777-8777-777777777777',x),'{session_id}','{{}}',1 FROM seq", count = MAX_RECLAIMED_BEACONS_PER_REQUEST + 4)).unwrap();
        let issue = |request_id: &str| PlaybackSessionIssueRequest {
            request_id: request_id.into(),
            content_id: CONTENT.into(),
            client: client(),
        };
        let now = 1_700_000_000_000;
        store
            .issue_ott_playback_session(&issue("cleanup-beacon-pass-1"), now)
            .unwrap();
        let remaining = store
            .prepare(&format!(
                "SELECT COUNT(*) FROM ott_playback_beacons WHERE session_id='{session_id}'"
            ))
            .unwrap();
        assert_eq!(unsafe { sqlite3_step(remaining.raw) }, SQLITE_ROW);
        assert_eq!(unsafe { sqlite3_column_int64(remaining.raw, 0) }, 4);
        let parent = store
            .prepare(&format!(
                "SELECT 1 FROM ott_playback_sessions WHERE session_id='{session_id}'"
            ))
            .unwrap();
        assert_eq!(unsafe { sqlite3_step(parent.raw) }, SQLITE_ROW);

        store
            .issue_ott_playback_session(&issue("cleanup-beacon-pass-2"), now + 1)
            .unwrap();
        let parent = store
            .prepare(&format!(
                "SELECT 1 FROM ott_playback_sessions WHERE session_id='{session_id}'"
            ))
            .unwrap();
        assert_eq!(unsafe { sqlite3_step(parent.raw) }, SQLITE_DONE);
        drop(store);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn one_session_cannot_flood_progress_beacons() {
        let (store, path) = test_store();
        store.write_ott_catalog_page(&catalog(), "test", 1).unwrap();
        let issued = store
            .issue_ott_playback_session(
                &PlaybackSessionIssueRequest {
                    request_id: "paced-beacons".into(),
                    content_id: CONTENT.into(),
                    client: client(),
                },
                1_700_000_000_000,
            )
            .unwrap();
        let mut beacon = ClientPlaybackBeacon {
            contract_version: OTT_CONTRACT_VERSION,
            beacon_id: "11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa".into(),
            session_id: issued.session.session_id,
            session_revision: 1,
            session_capability: issued.session_capability,
            observed_at_unix_ms: 1_700_000_000_001,
            event: PlaybackBeaconEvent::Started,
            position_ms: 0,
        };
        store
            .collect_ott_playback_beacon(&beacon, 1_700_000_000_002)
            .unwrap();
        beacon.beacon_id = "22222222-aaaa-4aaa-8aaa-aaaaaaaaaaaa".into();
        beacon.event = PlaybackBeaconEvent::Progress;
        beacon.position_ms = 1;
        beacon.observed_at_unix_ms += 1;
        assert_eq!(
            store
                .collect_ott_playback_beacon(&beacon, 1_700_000_000_003)
                .unwrap_err(),
            OttAuthorityError::RateLimited(1)
        );
        drop(store);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn durable_session_admission_survives_restart_and_resets_at_exact_boundary() {
        let (store, path) = test_store();
        store.write_ott_catalog_page(&catalog(), "test", 1).unwrap();
        let now = 1_700_000_000_000;
        store.execute("BEGIN IMMEDIATE").unwrap();
        for _ in 1..MAX_SESSION_ISSUES_PER_WINDOW {
            store
                .consume_ott_admission_window("sessions", now, MAX_SESSION_ISSUES_PER_WINDOW)
                .unwrap();
        }
        store.finish_transaction(Ok(())).unwrap();
        let request = PlaybackSessionIssueRequest {
            request_id: "admission-last-accepted".into(),
            content_id: CONTENT.into(),
            client: client(),
        };
        let accepted = store.issue_ott_playback_session(&request, now).unwrap();

        let mut invalid = request.clone();
        invalid.request_id.clear();
        assert!(store.issue_ott_playback_session(&invalid, now + 1).is_err());
        let mut unavailable = request.clone();
        unavailable.request_id = "admission-unavailable".into();
        unavailable.content_id = CHANNEL.into();
        assert_eq!(
            store
                .issue_ott_playback_session(&unavailable, now + 1)
                .unwrap_err(),
            OttAuthorityError::Unavailable
        );
        let replay = store.issue_ott_playback_session(&request, now + 1).unwrap();
        assert_eq!(accepted.session.session_id, replay.session.session_id);
        drop(store);

        let store = StationStore::open(&path).unwrap();
        let blocked = PlaybackSessionIssueRequest {
            request_id: "admission-blocked-after-restart".into(),
            content_id: CONTENT.into(),
            client: client(),
        };
        assert_eq!(
            store
                .issue_ott_playback_session(&blocked, now + 1_000)
                .unwrap_err(),
            OttAuthorityError::RateLimited(59)
        );
        let reset = PlaybackSessionIssueRequest {
            request_id: "admission-exact-reset".into(),
            content_id: CONTENT.into(),
            client: client(),
        };
        store
            .issue_ott_playback_session(&reset, now + ADMISSION_WINDOW_MS)
            .unwrap();
        let window = store
            .prepare("SELECT window_started_at_unix_ms,accepted_count FROM ott_admission_windows WHERE bucket='sessions'")
            .unwrap();
        assert_eq!(unsafe { sqlite3_step(window.raw) }, SQLITE_ROW);
        assert_eq!(
            unsafe { sqlite3_column_int64(window.raw, 0) },
            now + ADMISSION_WINDOW_MS
        );
        assert_eq!(unsafe { sqlite3_column_int64(window.raw, 1) }, 1);
        drop(store);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn durable_global_beacon_window_rejects_flood_without_charging_replay_or_invalid_input() {
        let (store, path) = test_store();
        store.write_ott_catalog_page(&catalog(), "test", 1).unwrap();
        let now = 1_700_000_000_000;
        let issue = |request_id: &str| PlaybackSessionIssueRequest {
            request_id: request_id.into(),
            content_id: CONTENT.into(),
            client: client(),
        };
        let first = store
            .issue_ott_playback_session(&issue("beacon-window-1"), now - 1_000)
            .unwrap();
        let second = store
            .issue_ott_playback_session(&issue("beacon-window-2"), now - 999)
            .unwrap();
        store.execute("BEGIN IMMEDIATE").unwrap();
        for _ in 1..MAX_BEACONS_PER_WINDOW {
            store
                .consume_ott_admission_window("beacons", now, MAX_BEACONS_PER_WINDOW)
                .unwrap();
        }
        store.finish_transaction(Ok(())).unwrap();
        let beacon = |id: &str, issue: &PlaybackSessionIssueResponse| ClientPlaybackBeacon {
            contract_version: OTT_CONTRACT_VERSION,
            beacon_id: id.into(),
            session_id: issue.session.session_id.clone(),
            session_revision: issue.session.session_revision,
            session_capability: issue.session_capability.clone(),
            observed_at_unix_ms: now,
            event: PlaybackBeaconEvent::Started,
            position_ms: 0,
        };
        let accepted = beacon("33333333-aaaa-4aaa-8aaa-aaaaaaaaaaaa", &first);
        store.collect_ott_playback_beacon(&accepted, now).unwrap();
        assert!(
            store
                .collect_ott_playback_beacon(&accepted, now + 1)
                .unwrap()
                .replayed
        );
        let mut forged = beacon("44444444-aaaa-4aaa-8aaa-aaaaaaaaaaaa", &second);
        forged.session_capability = "a".repeat(64);
        assert_eq!(
            store.collect_ott_playback_beacon(&forged, now).unwrap_err(),
            OttAuthorityError::InvalidCapability
        );
        let blocked = beacon("55555555-aaaa-4aaa-8aaa-aaaaaaaaaaaa", &second);
        assert_eq!(
            store
                .collect_ott_playback_beacon(&blocked, now)
                .unwrap_err(),
            OttAuthorityError::RateLimited(60)
        );
        let window = store
            .prepare("SELECT accepted_count FROM ott_admission_windows WHERE bucket='beacons'")
            .unwrap();
        assert_eq!(unsafe { sqlite3_step(window.raw) }, SQLITE_ROW);
        assert_eq!(
            unsafe { sqlite3_column_int64(window.raw, 0) },
            i64::from(MAX_BEACONS_PER_WINDOW)
        );
        drop(store);
        let _ = std::fs::remove_file(path);
    }
}
