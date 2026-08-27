//! Durable, fail-closed underwriting spot authority.  This is deliberately a
//! station-owned SQLite boundary: actor and occurrence time are audited with
//! each immutable history revision, while the pure contract owns shape rules.

use super::*;
use station_schedule::{AssetReadiness, ScheduleItem, ScheduleState};
use station_underwriting::{
    UnderwritingFlight, UnderwritingPlacementIntent, UnderwritingSpot, validate_flight_write,
    validate_placement_write, validate_spot_write,
};

const MAX_UNDERWRITING_HISTORY: usize = 1_000;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum UnderwritingWriteError {
    Invalid(String),
    Conflict { current_revision: Option<u64> },
    Rebound,
    Capacity,
    Storage(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnderwritingHistoryEntry {
    pub spot: UnderwritingSpot,
    pub changed_by: String,
    pub occurred_at_unix_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnderwritingPlacementHistoryEntry {
    pub placement: UnderwritingPlacementIntent,
    pub schedule_item_id: String,
    pub schedule_item_revision: u64,
    pub changed_by: String,
    pub occurred_at_unix_ms: i64,
}

impl StationStore {
    pub(super) fn initialize_underwriting_authority_schema(&self) -> Result<(), String> {
        self.execute(
            "CREATE TABLE IF NOT EXISTS underwriting_spots (\
               spot_id TEXT PRIMARY KEY CHECK(length(spot_id)=36),\
               sponsor_id TEXT NOT NULL CHECK(length(sponsor_id)=36),\
               asset_id TEXT NOT NULL CHECK(length(asset_id)=64),\
               revision INTEGER NOT NULL CHECK(revision>=1),\
               record_json TEXT NOT NULL\
             );\
             CREATE TABLE IF NOT EXISTS underwriting_spot_history (\
               spot_id TEXT NOT NULL CHECK(length(spot_id)=36),\
               sponsor_id TEXT NOT NULL CHECK(length(sponsor_id)=36),\
               asset_id TEXT NOT NULL CHECK(length(asset_id)=64),\
               revision INTEGER NOT NULL CHECK(revision>=1),\
               record_json TEXT NOT NULL,\
               changed_by TEXT NOT NULL CHECK(length(changed_by) BETWEEN 1 AND 200 AND changed_by=trim(changed_by)),\
               occurred_at_unix_ms INTEGER NOT NULL CHECK(occurred_at_unix_ms>=0),\
               PRIMARY KEY(spot_id,revision)\
             );\
             CREATE INDEX IF NOT EXISTS underwriting_spots_sponsor ON underwriting_spots(sponsor_id,spot_id);\
             CREATE INDEX IF NOT EXISTS underwriting_spot_history_spot_revision ON underwriting_spot_history(spot_id,revision DESC);\
             CREATE TRIGGER IF NOT EXISTS underwriting_spot_history_no_update BEFORE UPDATE ON underwriting_spot_history BEGIN SELECT RAISE(ABORT,'underwriting spot history is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS underwriting_spot_history_no_delete BEFORE DELETE ON underwriting_spot_history BEGIN SELECT RAISE(ABORT,'underwriting spot history is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS underwriting_spots_no_delete BEFORE DELETE ON underwriting_spots BEGIN SELECT RAISE(ABORT,'underwriting spots cannot be deleted'); END;",
        )
        .and_then(|_| self.execute(
            "CREATE TABLE IF NOT EXISTS underwriting_flights(flight_id TEXT PRIMARY KEY,sponsor_id TEXT NOT NULL,spot_id TEXT NOT NULL,revision INTEGER NOT NULL,record_json TEXT NOT NULL);\
             CREATE TABLE IF NOT EXISTS underwriting_flight_history(flight_id TEXT NOT NULL,revision INTEGER NOT NULL,record_json TEXT NOT NULL,changed_by TEXT NOT NULL,occurred_at_unix_ms INTEGER NOT NULL,PRIMARY KEY(flight_id,revision));\
             CREATE INDEX IF NOT EXISTS underwriting_flights_spot ON underwriting_flights(spot_id,flight_id);\
             CREATE TRIGGER IF NOT EXISTS underwriting_flight_history_no_update BEFORE UPDATE ON underwriting_flight_history BEGIN SELECT RAISE(ABORT,'underwriting flight history is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS underwriting_flight_history_no_delete BEFORE DELETE ON underwriting_flight_history BEGIN SELECT RAISE(ABORT,'underwriting flight history is append-only'); END;\
             CREATE TABLE IF NOT EXISTS underwriting_placements(placement_id TEXT PRIMARY KEY,flight_id TEXT NOT NULL,channel_id TEXT NOT NULL,scheduled_at_unix_ms INTEGER NOT NULL,revision INTEGER NOT NULL,schedule_item_id TEXT NOT NULL,schedule_item_revision INTEGER NOT NULL,record_json TEXT NOT NULL);\
             CREATE TABLE IF NOT EXISTS underwriting_placement_history(placement_id TEXT NOT NULL,revision INTEGER NOT NULL,schedule_item_id TEXT NOT NULL,schedule_item_revision INTEGER NOT NULL,record_json TEXT NOT NULL,changed_by TEXT NOT NULL,occurred_at_unix_ms INTEGER NOT NULL,PRIMARY KEY(placement_id,revision));\
             CREATE INDEX IF NOT EXISTS underwriting_placements_flight_time ON underwriting_placements(flight_id,scheduled_at_unix_ms,placement_id);\
             DROP INDEX IF EXISTS underwriting_placements_schedule;\
             CREATE UNIQUE INDEX underwriting_placements_schedule ON underwriting_placements(schedule_item_id);\
             CREATE TRIGGER IF NOT EXISTS underwriting_placement_history_no_update BEFORE UPDATE ON underwriting_placement_history BEGIN SELECT RAISE(ABORT,'underwriting placement history is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS underwriting_placement_history_no_delete BEFORE DELETE ON underwriting_placement_history BEGIN SELECT RAISE(ABORT,'underwriting placement history is append-only'); END;"
        ))
    }

    pub fn read_underwriting_spot(
        &self,
        spot_id: &str,
    ) -> Result<Option<UnderwritingSpot>, String> {
        validate_underwriting_id(spot_id)?;
        let statement = self.prepare("SELECT spot_id,sponsor_id,asset_id,revision,record_json FROM underwriting_spots WHERE spot_id=?1")?;
        let identity = c_string(spot_id, "underwriting spot identity")?;
        bind_text(self.database, statement.raw, 1, &identity)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_DONE => Ok(None),
            SQLITE_ROW => {
                let spot = read_spot_row(statement.raw, 0)?;
                let latest = self
                    .read_underwriting_spot_history_entry(spot_id, spot.revision)?
                    .ok_or("current underwriting spot has no exact history snapshot")?;
                if latest.spot != spot {
                    return Err(
                        "current underwriting spot indexed authority and history disagree".into(),
                    );
                }
                Ok(Some(spot))
            }
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_underwriting_spot_history(
        &self,
        spot_id: &str,
    ) -> Result<Vec<UnderwritingHistoryEntry>, String> {
        validate_underwriting_id(spot_id)?;
        let statement = self.prepare("SELECT spot_id,sponsor_id,asset_id,revision,record_json,changed_by,occurred_at_unix_ms FROM underwriting_spot_history WHERE spot_id=?1 ORDER BY revision ASC LIMIT ?2")?;
        let identity = c_string(spot_id, "underwriting spot identity")?;
        bind_text(self.database, statement.raw, 1, &identity)?;
        bind_i64(
            self.database,
            statement.raw,
            2,
            MAX_UNDERWRITING_HISTORY as i64,
        )?;
        let mut entries = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_DONE => return Ok(entries),
                SQLITE_ROW => entries.push(read_history_row(statement.raw)?),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn write_underwriting_spot(
        &self,
        proposed: &UnderwritingSpot,
        expected_revision: Option<u64>,
        changed_by: &str,
        occurred_at_unix_ms: i64,
    ) -> Result<(), UnderwritingWriteError> {
        proposed
            .validate()
            .map_err(UnderwritingWriteError::Invalid)?;
        if !valid_actor(changed_by) || occurred_at_unix_ms < 0 {
            return Err(UnderwritingWriteError::Invalid(
                "underwriting change attribution is invalid".into(),
            ));
        }
        self.execute("BEGIN IMMEDIATE")
            .map_err(UnderwritingWriteError::Storage)?;
        let result = (|| {
            let current = self
                .read_underwriting_spot(&proposed.spot_id)
                .map_err(UnderwritingWriteError::Storage)?;
            match validate_spot_write(current.as_ref(), expected_revision, proposed) {
                Ok(()) => {}
                Err(error) if error.contains("cannot be rebound") => {
                    return Err(UnderwritingWriteError::Rebound);
                }
                Err(_) => {
                    return Err(UnderwritingWriteError::Conflict {
                        current_revision: current.as_ref().map(|spot| spot.revision),
                    });
                }
            }
            if let Some(current) = &current {
                let latest = self
                    .read_underwriting_spot_history_entry(&current.spot_id, current.revision)
                    .map_err(UnderwritingWriteError::Storage)?
                    .ok_or_else(|| {
                        UnderwritingWriteError::Storage(
                            "current underwriting spot has no exact history snapshot".into(),
                        )
                    })?;
                if occurred_at_unix_ms < latest.occurred_at_unix_ms {
                    return Err(UnderwritingWriteError::Invalid(
                        "underwriting occurrence time precedes latest history".into(),
                    ));
                }
            }
            let json = serde_json::to_string(proposed)
                .map_err(|e| UnderwritingWriteError::Storage(e.to_string()))?;
            let record = c_string(&json, "underwriting spot record")
                .map_err(UnderwritingWriteError::Storage)?;
            let spot_id = c_string(&proposed.spot_id, "underwriting spot identity")
                .map_err(UnderwritingWriteError::Storage)?;
            let sponsor = c_string(&proposed.sponsor_id, "underwriting sponsor identity")
                .map_err(UnderwritingWriteError::Storage)?;
            let asset = c_string(&proposed.asset_id, "underwriting asset identity")
                .map_err(UnderwritingWriteError::Storage)?;
            let revision = checked_i64(proposed.revision, "underwriting revision")
                .map_err(UnderwritingWriteError::Invalid)?;
            let statement = if current.is_some() { self.prepare("UPDATE underwriting_spots SET sponsor_id=?2,asset_id=?3,revision=?4,record_json=?5 WHERE spot_id=?1 AND revision=?6") } else { self.prepare("INSERT INTO underwriting_spots(spot_id,sponsor_id,asset_id,revision,record_json) VALUES(?1,?2,?3,?4,?5)") }.map_err(UnderwritingWriteError::Storage)?;
            bind_text(self.database, statement.raw, 1, &spot_id)
                .map_err(UnderwritingWriteError::Storage)?;
            bind_text(self.database, statement.raw, 2, &sponsor)
                .map_err(UnderwritingWriteError::Storage)?;
            bind_text(self.database, statement.raw, 3, &asset)
                .map_err(UnderwritingWriteError::Storage)?;
            bind_i64(self.database, statement.raw, 4, revision)
                .map_err(UnderwritingWriteError::Storage)?;
            bind_text(self.database, statement.raw, 5, &record)
                .map_err(UnderwritingWriteError::Storage)?;
            if let Some(current) = &current {
                bind_i64(
                    self.database,
                    statement.raw,
                    6,
                    checked_i64(current.revision, "underwriting revision")
                        .map_err(UnderwritingWriteError::Invalid)?,
                )
                .map_err(UnderwritingWriteError::Storage)?;
            }
            expect_done(self.database, statement.raw).map_err(UnderwritingWriteError::Storage)?;
            if self.changes() != 1 {
                return Err(UnderwritingWriteError::Conflict {
                    current_revision: current.map(|spot| spot.revision),
                });
            }
            let history = self.prepare("INSERT INTO underwriting_spot_history(spot_id,sponsor_id,asset_id,revision,record_json,changed_by,occurred_at_unix_ms) VALUES(?1,?2,?3,?4,?5,?6,?7)").map_err(UnderwritingWriteError::Storage)?;
            let actor = c_string(changed_by, "underwriting actor")
                .map_err(UnderwritingWriteError::Invalid)?;
            bind_text(self.database, history.raw, 1, &spot_id)
                .map_err(UnderwritingWriteError::Storage)?;
            bind_text(self.database, history.raw, 2, &sponsor)
                .map_err(UnderwritingWriteError::Storage)?;
            bind_text(self.database, history.raw, 3, &asset)
                .map_err(UnderwritingWriteError::Storage)?;
            bind_i64(self.database, history.raw, 4, revision)
                .map_err(UnderwritingWriteError::Storage)?;
            bind_text(self.database, history.raw, 5, &record)
                .map_err(UnderwritingWriteError::Storage)?;
            bind_text(self.database, history.raw, 6, &actor)
                .map_err(UnderwritingWriteError::Storage)?;
            bind_i64(self.database, history.raw, 7, occurred_at_unix_ms)
                .map_err(UnderwritingWriteError::Storage)?;
            expect_done(self.database, history.raw).map_err(UnderwritingWriteError::Storage)
        })();
        match result {
            Ok(()) => self
                .execute("COMMIT")
                .map_err(UnderwritingWriteError::Storage),
            Err(error) => {
                let _ = self.execute("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn read_underwriting_flight(
        &self,
        flight_id: &str,
    ) -> Result<Option<UnderwritingFlight>, String> {
        validate_underwriting_id(flight_id)?;
        let s = self.prepare(
            "SELECT flight_id,revision,record_json FROM underwriting_flights WHERE flight_id=?1",
        )?;
        let id = c_string(flight_id, "underwriting flight identity")?;
        bind_text(self.database, s.raw, 1, &id)?;
        match unsafe { sqlite3_step(s.raw) } {
            SQLITE_DONE => Ok(None),
            SQLITE_ROW => {
                let value = read_flight_row(s.raw, 0)?;
                let latest = self
                    .read_underwriting_flight_revision(flight_id, value.revision)?
                    .ok_or("current underwriting flight has no exact history snapshot")?;
                if latest != value {
                    return Err("current underwriting flight and exact history disagree".into());
                }
                Ok(Some(value))
            }
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn write_underwriting_flight(
        &self,
        proposed: &UnderwritingFlight,
        expected_revision: Option<u64>,
        changed_by: &str,
        occurred_at_unix_ms: i64,
    ) -> Result<(), UnderwritingWriteError> {
        proposed
            .validate()
            .map_err(UnderwritingWriteError::Invalid)?;
        validate_attribution(changed_by, occurred_at_unix_ms)?;
        self.execute("BEGIN IMMEDIATE")
            .map_err(UnderwritingWriteError::Storage)?;
        let result = (|| {
            let current = self
                .read_underwriting_flight(&proposed.flight_id)
                .map_err(UnderwritingWriteError::Storage)?;
            let spot = self
                .read_underwriting_spot(&proposed.spot_id)
                .map_err(UnderwritingWriteError::Storage)?
                .ok_or_else(|| {
                    UnderwritingWriteError::Invalid(
                        "underwriting flight spot does not exist".into(),
                    )
                })?;
            validate_flight_write(current.as_ref(), expected_revision, proposed, &spot)
                .map_err(|error| map_contract_error(error, current.as_ref().map(|v| v.revision)))?;
            if let Some(current) = &current {
                let latest = self
                    .read_underwriting_flight_history_attribution(
                        &current.flight_id,
                        current.revision,
                    )
                    .map_err(UnderwritingWriteError::Storage)?
                    .ok_or_else(|| {
                        UnderwritingWriteError::Storage(
                            "current underwriting flight has no exact history snapshot".into(),
                        )
                    })?;
                if occurred_at_unix_ms < latest {
                    return Err(UnderwritingWriteError::Invalid(
                        "underwriting occurrence time precedes latest flight history".into(),
                    ));
                }
            }
            let json = serde_json::to_string(proposed)
                .map_err(|e| UnderwritingWriteError::Storage(e.to_string()))?;
            let ids = [
                c_string(&proposed.flight_id, "flight").map_err(UnderwritingWriteError::Storage)?,
                c_string(&proposed.sponsor_id, "sponsor")
                    .map_err(UnderwritingWriteError::Storage)?,
                c_string(&proposed.spot_id, "spot").map_err(UnderwritingWriteError::Storage)?,
                c_string(&json, "flight JSON").map_err(UnderwritingWriteError::Storage)?,
            ];
            let rev = checked_i64(proposed.revision, "flight revision")
                .map_err(UnderwritingWriteError::Invalid)?;
            let s=if current.is_some(){self.prepare("UPDATE underwriting_flights SET sponsor_id=?2,spot_id=?3,revision=?4,record_json=?5 WHERE flight_id=?1 AND revision=?6")}else{self.prepare("INSERT INTO underwriting_flights(flight_id,sponsor_id,spot_id,revision,record_json)VALUES(?1,?2,?3,?4,?5)")}.map_err(UnderwritingWriteError::Storage)?;
            bind_text(self.database, s.raw, 1, &ids[0]).map_err(UnderwritingWriteError::Storage)?;
            bind_text(self.database, s.raw, 2, &ids[1]).map_err(UnderwritingWriteError::Storage)?;
            bind_text(self.database, s.raw, 3, &ids[2]).map_err(UnderwritingWriteError::Storage)?;
            bind_i64(self.database, s.raw, 4, rev).map_err(UnderwritingWriteError::Storage)?;
            bind_text(self.database, s.raw, 5, &ids[3]).map_err(UnderwritingWriteError::Storage)?;
            if let Some(v) = &current {
                bind_i64(
                    self.database,
                    s.raw,
                    6,
                    checked_i64(v.revision, "flight revision")
                        .map_err(UnderwritingWriteError::Invalid)?,
                )
                .map_err(UnderwritingWriteError::Storage)?
            }
            expect_done(self.database, s.raw).map_err(UnderwritingWriteError::Storage)?;
            let h=self.prepare("INSERT INTO underwriting_flight_history(flight_id,revision,record_json,changed_by,occurred_at_unix_ms)VALUES(?1,?2,?3,?4,?5)").map_err(UnderwritingWriteError::Storage)?;
            let actor = c_string(changed_by, "actor").map_err(UnderwritingWriteError::Invalid)?;
            bind_text(self.database, h.raw, 1, &ids[0]).map_err(UnderwritingWriteError::Storage)?;
            bind_i64(self.database, h.raw, 2, rev).map_err(UnderwritingWriteError::Storage)?;
            bind_text(self.database, h.raw, 3, &ids[3]).map_err(UnderwritingWriteError::Storage)?;
            bind_text(self.database, h.raw, 4, &actor).map_err(UnderwritingWriteError::Storage)?;
            bind_i64(self.database, h.raw, 5, occurred_at_unix_ms)
                .map_err(UnderwritingWriteError::Storage)?;
            expect_done(self.database, h.raw).map_err(UnderwritingWriteError::Storage)
        })();
        finish_underwriting(self, result)
    }

    pub fn write_underwriting_placement(
        &self,
        proposed: &UnderwritingPlacementIntent,
        schedule_item_id: &str,
        schedule_item_revision: u64,
        expected_revision: Option<u64>,
        changed_by: &str,
        occurred_at_unix_ms: i64,
    ) -> Result<(), UnderwritingWriteError> {
        proposed
            .validate_shape()
            .map_err(UnderwritingWriteError::Invalid)?;
        validate_attribution(changed_by, occurred_at_unix_ms)?;
        self.execute("BEGIN IMMEDIATE")
            .map_err(UnderwritingWriteError::Storage)?;
        let result = (|| {
            let flight = self
                .read_underwriting_flight(&proposed.flight_id)
                .map_err(UnderwritingWriteError::Storage)?
                .ok_or_else(|| {
                    UnderwritingWriteError::Invalid(
                        "underwriting placement flight does not exist".into(),
                    )
                })?;
            let current = self
                .read_underwriting_placement(&proposed.placement_id)
                .map_err(UnderwritingWriteError::Storage)?;
            validate_placement_write(
                current.as_ref().map(|v| &v.0),
                expected_revision,
                proposed,
                &flight,
            )
            .map_err(|e| map_contract_error(e, current.as_ref().map(|v| v.0.revision)))?;
            let spot = self
                .read_underwriting_spot_revision(&flight.spot_id, flight.spot_revision)
                .map_err(UnderwritingWriteError::Storage)?
                .ok_or_else(|| {
                    UnderwritingWriteError::Invalid(
                        "underwriting flight's exact spot revision does not exist".into(),
                    )
                })?;
            if spot.sponsor_id != flight.sponsor_id {
                return Err(UnderwritingWriteError::Rebound);
            }
            let item = self
                .read_schedule_item(schedule_item_id)
                .map_err(UnderwritingWriteError::Storage)?
                .ok_or_else(|| {
                    UnderwritingWriteError::Invalid("bound schedule item does not exist".into())
                })?;
            let media = self
                .read_media_asset(&spot.asset_id)
                .map_err(UnderwritingWriteError::Storage)?
                .ok_or_else(|| {
                    UnderwritingWriteError::Invalid("underwriting spot media does not exist".into())
                })?;
            if media.readiness != AssetReadiness::Ready || media.duration_ms != spot.duration_ms {
                return Err(UnderwritingWriteError::Invalid(
                    "underwriting placement requires ready media with the exact spot duration"
                        .into(),
                ));
            }
            if current.is_none() {
                validate_schedule_binding(&item, schedule_item_revision, proposed, &spot)?;
            } else {
                validate_schedule_successor_binding(
                    &item,
                    schedule_item_revision,
                    proposed,
                    &spot,
                )?;
            }
            if let Some((_, bound_schedule_item_id, _)) = &current
                && bound_schedule_item_id != schedule_item_id
            {
                return Err(UnderwritingWriteError::Rebound);
            }
            if let Some((current_placement, _, _)) = &current {
                let latest = self
                    .read_latest_underwriting_placement_history(&current_placement.placement_id)
                    .map_err(UnderwritingWriteError::Storage)?
                    .ok_or_else(|| {
                        UnderwritingWriteError::Storage(
                            "current underwriting placement has no exact history snapshot".into(),
                        )
                    })?;
                if occurred_at_unix_ms < latest.occurred_at_unix_ms {
                    return Err(UnderwritingWriteError::Invalid(
                        "underwriting occurrence time precedes latest placement history".into(),
                    ));
                }
            }
            if let Some(owner) = self
                .read_schedule_placement_owner(schedule_item_id)
                .map_err(UnderwritingWriteError::Storage)?
                && owner != proposed.placement_id
            {
                return Err(UnderwritingWriteError::Conflict {
                    current_revision: None,
                });
            }
            let placements = self
                .list_flight_placements(&proposed.flight_id)
                .map_err(UnderwritingWriteError::Storage)?;
            let active: Vec<_> = placements
                .iter()
                .filter(|(p, _, _)| p.placement_id != proposed.placement_id)
                .collect();
            if active.len() >= flight.placement.max_airings as usize {
                return Err(UnderwritingWriteError::Capacity);
            };
            for (other, _, _) in active {
                let gap = proposed
                    .scheduled_at_unix_ms
                    .abs_diff(other.scheduled_at_unix_ms);
                if gap < flight.placement.minimum_separation_ms {
                    return Err(UnderwritingWriteError::Capacity);
                }
            }
            let json = serde_json::to_string(proposed)
                .map_err(|e| UnderwritingWriteError::Storage(e.to_string()))?;
            let values = [
                c_string(&proposed.placement_id, "placement")
                    .map_err(UnderwritingWriteError::Storage)?,
                c_string(&proposed.flight_id, "flight").map_err(UnderwritingWriteError::Storage)?,
                c_string(&proposed.channel_id, "channel")
                    .map_err(UnderwritingWriteError::Storage)?,
                c_string(schedule_item_id, "schedule").map_err(UnderwritingWriteError::Storage)?,
                c_string(&json, "placement JSON").map_err(UnderwritingWriteError::Storage)?,
            ];
            let rev = checked_i64(proposed.revision, "placement revision")
                .map_err(UnderwritingWriteError::Invalid)?;
            let schedrev = checked_i64(schedule_item_revision, "schedule revision")
                .map_err(UnderwritingWriteError::Invalid)?;
            let s=if current.is_some(){self.prepare("UPDATE underwriting_placements SET flight_id=?2,channel_id=?3,scheduled_at_unix_ms=?4,revision=?5,schedule_item_id=?6,schedule_item_revision=?7,record_json=?8 WHERE placement_id=?1 AND revision=?9")}else{self.prepare("INSERT INTO underwriting_placements(placement_id,flight_id,channel_id,scheduled_at_unix_ms,revision,schedule_item_id,schedule_item_revision,record_json)VALUES(?1,?2,?3,?4,?5,?6,?7,?8)")}.map_err(UnderwritingWriteError::Storage)?;
            bind_text(self.database, s.raw, 1, &values[0])
                .map_err(UnderwritingWriteError::Storage)?;
            bind_text(self.database, s.raw, 2, &values[1])
                .map_err(UnderwritingWriteError::Storage)?;
            bind_text(self.database, s.raw, 3, &values[2])
                .map_err(UnderwritingWriteError::Storage)?;
            bind_i64(self.database, s.raw, 4, proposed.scheduled_at_unix_ms)
                .map_err(UnderwritingWriteError::Storage)?;
            bind_i64(self.database, s.raw, 5, rev).map_err(UnderwritingWriteError::Storage)?;
            bind_text(self.database, s.raw, 6, &values[3])
                .map_err(UnderwritingWriteError::Storage)?;
            bind_i64(self.database, s.raw, 7, schedrev).map_err(UnderwritingWriteError::Storage)?;
            bind_text(self.database, s.raw, 8, &values[4])
                .map_err(UnderwritingWriteError::Storage)?;
            if let Some((current_placement, _, _)) = &current {
                bind_i64(
                    self.database,
                    s.raw,
                    9,
                    checked_i64(current_placement.revision, "placement revision")
                        .map_err(UnderwritingWriteError::Invalid)?,
                )
                .map_err(UnderwritingWriteError::Storage)?;
            }
            expect_done(self.database, s.raw).map_err(UnderwritingWriteError::Storage)?;
            if self.changes() != 1 {
                return Err(UnderwritingWriteError::Conflict {
                    current_revision: current.as_ref().map(|entry| entry.0.revision),
                });
            }
            let h=self.prepare("INSERT INTO underwriting_placement_history(placement_id,revision,schedule_item_id,schedule_item_revision,record_json,changed_by,occurred_at_unix_ms)VALUES(?1,?2,?3,?4,?5,?6,?7)").map_err(UnderwritingWriteError::Storage)?;
            let actor = c_string(changed_by, "actor").map_err(UnderwritingWriteError::Invalid)?;
            bind_text(self.database, h.raw, 1, &values[0])
                .map_err(UnderwritingWriteError::Storage)?;
            bind_i64(self.database, h.raw, 2, rev).map_err(UnderwritingWriteError::Storage)?;
            bind_text(self.database, h.raw, 3, &values[3])
                .map_err(UnderwritingWriteError::Storage)?;
            bind_i64(self.database, h.raw, 4, schedrev).map_err(UnderwritingWriteError::Storage)?;
            bind_text(self.database, h.raw, 5, &values[4])
                .map_err(UnderwritingWriteError::Storage)?;
            bind_text(self.database, h.raw, 6, &actor).map_err(UnderwritingWriteError::Storage)?;
            bind_i64(self.database, h.raw, 7, occurred_at_unix_ms)
                .map_err(UnderwritingWriteError::Storage)?;
            expect_done(self.database, h.raw).map_err(UnderwritingWriteError::Storage)
        })();
        finish_underwriting(self, result)
    }

    pub fn list_underwriting_placement_history(
        &self,
        placement_id: &str,
    ) -> Result<Vec<UnderwritingPlacementHistoryEntry>, String> {
        validate_underwriting_id(placement_id)?;
        let statement = self.prepare("SELECT placement_id,revision,record_json,schedule_item_id,schedule_item_revision,changed_by,occurred_at_unix_ms FROM underwriting_placement_history WHERE placement_id=?1 ORDER BY revision ASC LIMIT ?2")?;
        let id = c_string(placement_id, "underwriting placement identity")?;
        bind_text(self.database, statement.raw, 1, &id)?;
        bind_i64(
            self.database,
            statement.raw,
            2,
            MAX_UNDERWRITING_HISTORY as i64,
        )?;
        let mut entries = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_DONE => return Ok(entries),
                SQLITE_ROW => {
                    entries.push(read_placement_history_row(statement.raw)?);
                }
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    fn read_underwriting_placement(
        &self,
        id: &str,
    ) -> Result<Option<(UnderwritingPlacementIntent, String, u64)>, String> {
        let s=self.prepare("SELECT placement_id,flight_id,channel_id,scheduled_at_unix_ms,revision,schedule_item_id,schedule_item_revision,record_json FROM underwriting_placements WHERE placement_id=?1")?;
        let id = c_string(id, "placement")?;
        bind_text(self.database, s.raw, 1, &id)?;
        match unsafe { sqlite3_step(s.raw) } {
            SQLITE_DONE => Ok(None),
            SQLITE_ROW => {
                let current = read_placement_row(s.raw)?;
                let latest = self
                    .read_latest_underwriting_placement_history(
                        id.to_str().map_err(|_| "placement identity is invalid")?,
                    )?
                    .ok_or("current underwriting placement has no exact history snapshot")?;
                if latest.placement != current.0
                    || latest.schedule_item_id != current.1
                    || latest.schedule_item_revision != current.2
                {
                    return Err(
                        "current underwriting placement and exact latest history disagree".into(),
                    );
                }
                Ok(Some(current))
            }
            _ => Err(sqlite_error(self.database)),
        }
    }
    fn list_flight_placements(
        &self,
        id: &str,
    ) -> Result<Vec<(UnderwritingPlacementIntent, String, u64)>, String> {
        let s=self.prepare("SELECT placement_id,flight_id,channel_id,scheduled_at_unix_ms,revision,schedule_item_id,schedule_item_revision,record_json FROM underwriting_placements WHERE flight_id=?1 ORDER BY scheduled_at_unix_ms,placement_id LIMIT 100001")?;
        let id = c_string(id, "flight")?;
        bind_text(self.database, s.raw, 1, &id)?;
        let mut out = Vec::new();
        loop {
            match unsafe { sqlite3_step(s.raw) } {
                SQLITE_DONE => return Ok(out),
                SQLITE_ROW => out.push(read_placement_row(s.raw)?),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    fn read_schedule_placement_owner(
        &self,
        schedule_item_id: &str,
    ) -> Result<Option<String>, String> {
        let statement = self.prepare(
            "SELECT placement_id FROM underwriting_placements WHERE schedule_item_id=?1",
        )?;
        let id = c_string(schedule_item_id, "schedule item identity")?;
        bind_text(self.database, statement.raw, 1, &id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_DONE => Ok(None),
            SQLITE_ROW => Ok(Some(column_text(statement.raw, 0)?)),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub(super) fn validate_underwriting_schedule_successor(
        &self,
        item: &ScheduleItem,
    ) -> Result<(), String> {
        let s=self.prepare("SELECT record_json,schedule_item_revision FROM underwriting_placements WHERE schedule_item_id=?1")?;
        let id = c_string(&item.item_id, "schedule")?;
        bind_text(self.database, s.raw, 1, &id)?;
        match unsafe { sqlite3_step(s.raw) } {
            SQLITE_DONE => Ok(()),
            SQLITE_ROW => {
                let p: UnderwritingPlacementIntent = serde_json::from_str(&column_text(s.raw, 0)?)
                    .map_err(|_| "stored placement corrupt")?;
                let flight = self
                    .read_underwriting_flight_revision(&p.flight_id, p.flight_revision)?
                    .ok_or("placement flight revision missing")?;
                let spot = self
                    .read_underwriting_spot_revision(&flight.spot_id, flight.spot_revision)?
                    .ok_or("placement spot revision missing")?;
                validate_schedule_successor_binding(item, item.revision, &p, &spot).map_err(|e| {
                    format!("schedule mutation would invalidate underwriting authority: {e:?}")
                })
            }
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn read_underwriting_spot_history_entry(
        &self,
        spot_id: &str,
        revision: u64,
    ) -> Result<Option<UnderwritingHistoryEntry>, String> {
        let statement=self.prepare("SELECT spot_id,sponsor_id,asset_id,revision,record_json,changed_by,occurred_at_unix_ms FROM underwriting_spot_history WHERE spot_id=?1 AND revision=?2")?;
        let id = c_string(spot_id, "underwriting spot identity")?;
        bind_text(self.database, statement.raw, 1, &id)?;
        bind_i64(
            self.database,
            statement.raw,
            2,
            checked_i64(revision, "underwriting revision")?,
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_DONE => Ok(None),
            SQLITE_ROW => Ok(Some(read_history_row(statement.raw)?)),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn read_underwriting_spot_revision(
        &self,
        spot_id: &str,
        revision: u64,
    ) -> Result<Option<UnderwritingSpot>, String> {
        Ok(self
            .read_underwriting_spot_history_entry(spot_id, revision)?
            .map(|entry| entry.spot))
    }

    fn read_underwriting_flight_revision(
        &self,
        flight_id: &str,
        revision: u64,
    ) -> Result<Option<UnderwritingFlight>, String> {
        let statement = self.prepare(
            "SELECT flight_id,revision,record_json FROM underwriting_flight_history WHERE flight_id=?1 AND revision=?2",
        )?;
        let id = c_string(flight_id, "underwriting flight identity")?;
        bind_text(self.database, statement.raw, 1, &id)?;
        bind_i64(
            self.database,
            statement.raw,
            2,
            checked_i64(revision, "underwriting flight revision")?,
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_DONE => Ok(None),
            SQLITE_ROW => Ok(Some(read_flight_row(statement.raw, 0)?)),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn read_underwriting_flight_history_attribution(
        &self,
        flight_id: &str,
        revision: u64,
    ) -> Result<Option<i64>, String> {
        let statement = self.prepare("SELECT occurred_at_unix_ms FROM underwriting_flight_history WHERE flight_id=?1 AND revision=?2")?;
        let id = c_string(flight_id, "underwriting flight identity")?;
        bind_text(self.database, statement.raw, 1, &id)?;
        bind_i64(
            self.database,
            statement.raw,
            2,
            checked_i64(revision, "flight revision")?,
        )?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_DONE => Ok(None),
            SQLITE_ROW => {
                let value = unsafe { sqlite3_column_int64(statement.raw, 0) };
                if value < 0 {
                    Err("stored flight history attribution is corrupt".into())
                } else {
                    Ok(Some(value))
                }
            }
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn read_latest_underwriting_placement_history(
        &self,
        placement_id: &str,
    ) -> Result<Option<UnderwritingPlacementHistoryEntry>, String> {
        let statement = self.prepare("SELECT placement_id,revision,record_json,schedule_item_id,schedule_item_revision,changed_by,occurred_at_unix_ms FROM underwriting_placement_history WHERE placement_id=?1 ORDER BY revision DESC LIMIT 1")?;
        let id = c_string(placement_id, "underwriting placement identity")?;
        bind_text(self.database, statement.raw, 1, &id)?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_DONE => Ok(None),
            SQLITE_ROW => Ok(Some(read_placement_history_row(statement.raw)?)),
            _ => Err(sqlite_error(self.database)),
        }
    }
}

fn valid_actor(actor: &str) -> bool {
    !actor.trim().is_empty()
        && actor.trim() == actor
        && actor.len() <= 200
        && !actor.chars().any(char::is_control)
}
fn validate_attribution(actor: &str, time: i64) -> Result<(), UnderwritingWriteError> {
    if valid_actor(actor) && time >= 0 {
        Ok(())
    } else {
        Err(UnderwritingWriteError::Invalid(
            "underwriting change attribution is invalid".into(),
        ))
    }
}
fn map_contract_error(error: String, current_revision: Option<u64>) -> UnderwritingWriteError {
    if error.contains("cannot be rebound") {
        UnderwritingWriteError::Rebound
    } else {
        UnderwritingWriteError::Conflict { current_revision }
    }
}
fn finish_underwriting(
    store: &StationStore,
    result: Result<(), UnderwritingWriteError>,
) -> Result<(), UnderwritingWriteError> {
    match result {
        Ok(()) => store
            .execute("COMMIT")
            .map_err(UnderwritingWriteError::Storage),
        Err(e) => {
            let _ = store.execute("ROLLBACK");
            Err(e)
        }
    }
}
fn validate_schedule_binding(
    item: &ScheduleItem,
    revision: u64,
    p: &UnderwritingPlacementIntent,
    spot: &UnderwritingSpot,
) -> Result<(), UnderwritingWriteError> {
    if item.revision != revision
        || item.state != ScheduleState::Draft
        || item.channel_id != p.channel_id
        || item.asset_id != spot.asset_id
        || item.starts_at_unix_ms != p.scheduled_at_unix_ms
        || item.duration_ms != spot.duration_ms
    {
        return Err(UnderwritingWriteError::Invalid(
            "placement requires an exact ready Draft schedule binding".into(),
        ));
    }
    Ok(())
}
fn validate_schedule_successor_binding(
    item: &ScheduleItem,
    revision: u64,
    placement: &UnderwritingPlacementIntent,
    spot: &UnderwritingSpot,
) -> Result<(), UnderwritingWriteError> {
    if item.revision != revision
        || !matches!(item.state, ScheduleState::Draft | ScheduleState::Committed)
        || item.channel_id != placement.channel_id
        || item.asset_id != spot.asset_id
        || item.starts_at_unix_ms != placement.scheduled_at_unix_ms
        || item.duration_ms != spot.duration_ms
    {
        return Err(UnderwritingWriteError::Invalid(
            "schedule successor does not preserve exact underwriting authority".into(),
        ));
    }
    Ok(())
}
fn validate_underwriting_id(value: &str) -> Result<(), String> {
    if is_canonical_uuid(value) {
        Ok(())
    } else {
        Err("underwriting spot identity is invalid".into())
    }
}
fn read_spot_row(
    statement: *mut SqliteStatement,
    offset: c_int,
) -> Result<UnderwritingSpot, String> {
    let spot: UnderwritingSpot = serde_json::from_str(&column_text(statement, offset + 4)?)
        .map_err(|e| format!("stored underwriting spot is corrupt: {e}"))?;
    spot.validate()?;
    if spot.spot_id != column_text(statement, offset)?
        || spot.sponsor_id != column_text(statement, offset + 1)?
        || spot.asset_id != column_text(statement, offset + 2)?
        || spot.revision != column_u64(statement, offset + 3, "underwriting revision")?
    {
        return Err("stored underwriting spot indexed fields disagree with JSON".into());
    }
    Ok(spot)
}
fn read_history_row(statement: *mut SqliteStatement) -> Result<UnderwritingHistoryEntry, String> {
    let spot = read_spot_row(statement, 0)?;
    let changed_by = column_text(statement, 5)?;
    let occurred_at_unix_ms = unsafe { sqlite3_column_int64(statement, 6) };
    if !valid_actor(&changed_by) || occurred_at_unix_ms < 0 {
        return Err("stored underwriting history attribution is corrupt".into());
    };
    Ok(UnderwritingHistoryEntry {
        spot,
        changed_by,
        occurred_at_unix_ms,
    })
}

fn read_flight_row(
    statement: *mut SqliteStatement,
    offset: c_int,
) -> Result<UnderwritingFlight, String> {
    let flight: UnderwritingFlight = serde_json::from_str(&column_text(statement, offset + 2)?)
        .map_err(|error| format!("stored underwriting flight is corrupt: {error}"))?;
    flight.validate()?;
    if flight.flight_id != column_text(statement, offset)?
        || flight.revision != column_u64(statement, offset + 1, "flight revision")?
    {
        return Err("stored underwriting flight indexed fields disagree with JSON".into());
    }
    Ok(flight)
}

fn read_placement_row(
    statement: *mut SqliteStatement,
) -> Result<(UnderwritingPlacementIntent, String, u64), String> {
    let placement: UnderwritingPlacementIntent = serde_json::from_str(&column_text(statement, 7)?)
        .map_err(|error| format!("stored underwriting placement is corrupt: {error}"))?;
    placement.validate_shape()?;
    if placement.placement_id != column_text(statement, 0)?
        || placement.flight_id != column_text(statement, 1)?
        || placement.channel_id != column_text(statement, 2)?
        || placement.scheduled_at_unix_ms != unsafe { sqlite3_column_int64(statement, 3) }
        || placement.revision != column_u64(statement, 4, "placement revision")?
    {
        return Err("stored underwriting placement indexed fields disagree with JSON".into());
    }
    Ok((
        placement,
        column_text(statement, 5)?,
        column_u64(statement, 6, "schedule revision")?,
    ))
}

fn read_placement_history_row(
    statement: *mut SqliteStatement,
) -> Result<UnderwritingPlacementHistoryEntry, String> {
    let placement: UnderwritingPlacementIntent = serde_json::from_str(&column_text(statement, 2)?)
        .map_err(|error| format!("stored placement history is corrupt: {error}"))?;
    placement.validate_shape()?;
    if placement.placement_id != column_text(statement, 0)?
        || placement.revision != column_u64(statement, 1, "placement revision")?
    {
        return Err("stored placement history indexed fields disagree with JSON".into());
    }
    let changed_by = column_text(statement, 5)?;
    let occurred_at_unix_ms = unsafe { sqlite3_column_int64(statement, 6) };
    if !valid_actor(&changed_by) || occurred_at_unix_ms < 0 {
        return Err("stored placement history attribution is corrupt".into());
    }
    Ok(UnderwritingPlacementHistoryEntry {
        placement,
        schedule_item_id: column_text(statement, 3)?,
        schedule_item_revision: column_u64(statement, 4, "schedule revision")?,
        changed_by,
        occurred_at_unix_ms,
    })
}

#[cfg(test)]
mod corruption_tests {
    use super::*;
    use station_underwriting::PlacementConstraints;

    const SPOT: &str = "11111111-1111-4111-8111-111111111111";
    const SPONSOR: &str = "22222222-2222-4222-8222-222222222222";
    const FLIGHT: &str = "33333333-3333-4333-8333-333333333333";
    const CHANNEL: &str = "44444444-4444-4444-8444-444444444444";
    const PLACEMENT: &str = "55555555-5555-4555-8555-555555555555";
    const SCHEDULE: &str = "77777777-7777-4777-8777-777777777777";
    const ASSET: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

    fn open_store(label: &str) -> StationStore {
        let nonce = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        StationStore::open(std::env::temp_dir().join(format!(
            "townlight-underwriting-corruption-{label}-{nonce}.db"
        )))
        .unwrap()
    }

    fn spot() -> UnderwritingSpot {
        UnderwritingSpot {
            spot_id: SPOT.into(),
            sponsor_id: SPONSOR.into(),
            asset_id: ASSET.into(),
            label: "Sponsor".into(),
            duration_ms: 30_000,
            revision: 1,
        }
    }
    fn flight() -> UnderwritingFlight {
        UnderwritingFlight {
            flight_id: FLIGHT.into(),
            sponsor_id: SPONSOR.into(),
            spot_id: SPOT.into(),
            spot_revision: 1,
            starts_at_unix_ms: 0,
            ends_at_unix_ms: 1_000_000,
            channel_ids: vec![CHANNEL.into()],
            placement: PlacementConstraints {
                allowed_weekdays: 0b111_1111,
                daypart_start_minute: 0,
                daypart_end_minute: 1440,
                max_airings: 1,
                max_airings_per_day: 1,
                minimum_separation_ms: 0,
            },
            revision: 1,
        }
    }

    #[test]
    fn flight_and_placement_readers_reject_index_json_and_latest_history_drift() {
        let store = open_store("readers");
        store
            .put_media_asset(&MediaAsset {
                asset_id: ASSET.into(),
                media_path: r"C:\TownLight\spot.ts".into(),
                duration_ms: 30_000,
                readiness: AssetReadiness::Ready,
            })
            .unwrap();
        store
            .write_underwriting_spot(&spot(), None, "operator", 1)
            .unwrap();
        store
            .write_underwriting_flight(&flight(), None, "operator", 2)
            .unwrap();
        store.execute("UPDATE underwriting_flights SET revision=2 WHERE flight_id='33333333-3333-4333-8333-333333333333'").unwrap();
        assert!(store.read_underwriting_flight(FLIGHT).is_err());
        store.execute("UPDATE underwriting_flights SET revision=1 WHERE flight_id='33333333-3333-4333-8333-333333333333'").unwrap();
        store
            .write_schedule_item(&ScheduleItemWrite {
                item: ScheduleItem {
                    item_id: SCHEDULE.into(),
                    revision: 1,
                    channel_id: CHANNEL.into(),
                    asset_id: ASSET.into(),
                    title: "Spot".into(),
                    starts_at_unix_ms: 120_000,
                    duration_ms: 30_000,
                    state: ScheduleState::Draft,
                },
                changed_by: "operator".into(),
                occurred_at_unix_ms: 3,
            })
            .unwrap();
        let placement = UnderwritingPlacementIntent {
            placement_id: PLACEMENT.into(),
            flight_id: FLIGHT.into(),
            flight_revision: 1,
            channel_id: CHANNEL.into(),
            scheduled_at_unix_ms: 120_000,
            station_weekday: 0,
            station_minute_of_day: 2,
            revision: 1,
        };
        store
            .write_underwriting_placement(&placement, SCHEDULE, 1, None, "operator", 4)
            .unwrap();
        store.execute("UPDATE underwriting_placements SET revision=2 WHERE placement_id='55555555-5555-4555-8555-555555555555'").unwrap();
        assert!(store.read_underwriting_placement(PLACEMENT).is_err());
    }
}
