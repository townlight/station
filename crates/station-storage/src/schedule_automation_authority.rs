use super::*;
use station_schedule_automation::{DaypartBlock, RecurringRule, SavedSearch};

const MAX_SAVED_SEARCHES: usize = 64;
const MAX_DAYPART_BLOCKS: usize = 64;
const MAX_RECURRING_RULES: usize = 128;
const MAX_AUTHORITY_QUERY: usize = 128;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ScheduleAutomationWriteError {
    Invalid(String),
    Conflict {
        current_revision: Option<u64>,
    },
    DependencyConflict {
        authority: &'static str,
        requested_revision: u64,
        current_revision: Option<u64>,
    },
    NotFound {
        authority: &'static str,
    },
    Rebound,
    Capacity,
    Storage(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SavedSearchHistoryEntry {
    pub search: SavedSearch,
    pub changed_by: String,
    pub occurred_at_unix_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DaypartBlockHistoryEntry {
    pub block: DaypartBlock,
    pub changed_by: String,
    pub occurred_at_unix_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecurringRuleHistoryEntry {
    pub rule: RecurringRule,
    pub changed_by: String,
    pub occurred_at_unix_ms: i64,
}

impl StationStore {
    pub(super) fn initialize_schedule_automation_schema(&self) -> Result<(), String> {
        self.execute(
            "CREATE TABLE IF NOT EXISTS schedule_automation_saved_searches (\
               search_id TEXT PRIMARY KEY,\
               channel_id TEXT NOT NULL,\
               owner_operator_id TEXT NOT NULL,\
               revision INTEGER NOT NULL CHECK (revision >= 1),\
               record_json TEXT NOT NULL\
             );\
             CREATE INDEX IF NOT EXISTS schedule_automation_saved_searches_channel \
               ON schedule_automation_saved_searches(channel_id, search_id);\
             CREATE TABLE IF NOT EXISTS schedule_automation_saved_search_history (\
               search_id TEXT NOT NULL, channel_id TEXT NOT NULL, owner_operator_id TEXT NOT NULL,\
               revision INTEGER NOT NULL CHECK (revision >= 1), record_json TEXT NOT NULL,\
               changed_by TEXT NOT NULL CHECK (length(changed_by) BETWEEN 1 AND 200 AND changed_by=trim(changed_by)),\
               occurred_at_unix_ms INTEGER NOT NULL CHECK (occurred_at_unix_ms >= 0),\
               PRIMARY KEY(search_id, revision)\
             );\
             CREATE INDEX IF NOT EXISTS schedule_automation_saved_search_history_channel \
               ON schedule_automation_saved_search_history(channel_id, search_id, revision);\
             CREATE TABLE IF NOT EXISTS schedule_automation_daypart_blocks (\
               block_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL,\
               revision INTEGER NOT NULL CHECK (revision >= 1), record_json TEXT NOT NULL\
             );\
             CREATE INDEX IF NOT EXISTS schedule_automation_daypart_blocks_channel \
               ON schedule_automation_daypart_blocks(channel_id, block_id);\
             CREATE TABLE IF NOT EXISTS schedule_automation_daypart_block_history (\
               block_id TEXT NOT NULL, channel_id TEXT NOT NULL,\
               revision INTEGER NOT NULL CHECK (revision >= 1), record_json TEXT NOT NULL,\
               changed_by TEXT NOT NULL CHECK (length(changed_by) BETWEEN 1 AND 200 AND changed_by=trim(changed_by)),\
               occurred_at_unix_ms INTEGER NOT NULL CHECK (occurred_at_unix_ms >= 0),\
               PRIMARY KEY(block_id, revision)\
             );\
             CREATE INDEX IF NOT EXISTS schedule_automation_daypart_block_history_channel \
               ON schedule_automation_daypart_block_history(channel_id, block_id, revision);\
             CREATE TABLE IF NOT EXISTS schedule_automation_recurring_rules (\
               rule_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL,\
               saved_search_id TEXT NOT NULL, saved_search_revision INTEGER NOT NULL CHECK (saved_search_revision >= 1),\
               block_id TEXT NOT NULL, block_revision INTEGER NOT NULL CHECK (block_revision >= 1),\
               revision INTEGER NOT NULL CHECK (revision >= 1), record_json TEXT NOT NULL\
             );\
             CREATE INDEX IF NOT EXISTS schedule_automation_recurring_rules_channel \
               ON schedule_automation_recurring_rules(channel_id, rule_id);\
             CREATE INDEX IF NOT EXISTS schedule_automation_recurring_rules_search \
               ON schedule_automation_recurring_rules(saved_search_id, rule_id);\
             CREATE TABLE IF NOT EXISTS schedule_automation_recurring_rule_history (\
               rule_id TEXT NOT NULL, channel_id TEXT NOT NULL,\
               saved_search_id TEXT NOT NULL, saved_search_revision INTEGER NOT NULL CHECK (saved_search_revision >= 1),\
               block_id TEXT NOT NULL, block_revision INTEGER NOT NULL CHECK (block_revision >= 1),\
               revision INTEGER NOT NULL CHECK (revision >= 1), record_json TEXT NOT NULL,\
               changed_by TEXT NOT NULL CHECK (length(changed_by) BETWEEN 1 AND 200 AND changed_by=trim(changed_by)),\
               occurred_at_unix_ms INTEGER NOT NULL CHECK (occurred_at_unix_ms >= 0),\
               PRIMARY KEY(rule_id, revision)\
             );\
             CREATE INDEX IF NOT EXISTS schedule_automation_recurring_rule_history_channel \
               ON schedule_automation_recurring_rule_history(channel_id, rule_id, revision);\
             CREATE TRIGGER IF NOT EXISTS schedule_automation_saved_searches_no_delete BEFORE DELETE ON schedule_automation_saved_searches BEGIN SELECT RAISE(ABORT, 'saved searches cannot be deleted'); END;\
             CREATE TRIGGER IF NOT EXISTS schedule_automation_daypart_blocks_no_delete BEFORE DELETE ON schedule_automation_daypart_blocks BEGIN SELECT RAISE(ABORT, 'daypart blocks cannot be deleted'); END;\
             CREATE TRIGGER IF NOT EXISTS schedule_automation_recurring_rules_no_delete BEFORE DELETE ON schedule_automation_recurring_rules BEGIN SELECT RAISE(ABORT, 'recurring rules cannot be deleted'); END;\
             CREATE TRIGGER IF NOT EXISTS schedule_automation_saved_search_history_no_update BEFORE UPDATE ON schedule_automation_saved_search_history BEGIN SELECT RAISE(ABORT, 'saved search history is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS schedule_automation_saved_search_history_no_delete BEFORE DELETE ON schedule_automation_saved_search_history BEGIN SELECT RAISE(ABORT, 'saved search history is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS schedule_automation_daypart_block_history_no_update BEFORE UPDATE ON schedule_automation_daypart_block_history BEGIN SELECT RAISE(ABORT, 'daypart block history is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS schedule_automation_daypart_block_history_no_delete BEFORE DELETE ON schedule_automation_daypart_block_history BEGIN SELECT RAISE(ABORT, 'daypart block history is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS schedule_automation_recurring_rule_history_no_update BEFORE UPDATE ON schedule_automation_recurring_rule_history BEGIN SELECT RAISE(ABORT, 'recurring rule history is append-only'); END;\
             CREATE TRIGGER IF NOT EXISTS schedule_automation_recurring_rule_history_no_delete BEFORE DELETE ON schedule_automation_recurring_rule_history BEGIN SELECT RAISE(ABORT, 'recurring rule history is append-only'); END;",
        )
    }

    pub fn read_saved_search(&self, search_id: &str) -> Result<Option<SavedSearch>, String> {
        validate_authority_id(search_id, "saved search")?;
        let statement = self.prepare("SELECT search_id, channel_id, owner_operator_id, revision, record_json FROM schedule_automation_saved_searches WHERE search_id=?1")?;
        bind_authority_id(self, statement.raw, 1, search_id, "saved search")?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let search = read_saved_search_row(statement.raw)?;
                self.validate_current_saved_search_snapshot(&search)?;
                Ok(Some(search))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn read_saved_search_revision(
        &self,
        search_id: &str,
        revision: u64,
    ) -> Result<Option<SavedSearch>, String> {
        validate_authority_revision(search_id, revision, "saved search")?;
        let statement = self.prepare("SELECT search_id, channel_id, owner_operator_id, revision, record_json FROM schedule_automation_saved_search_history WHERE search_id=?1 AND revision=?2")?;
        bind_authority_id(self, statement.raw, 1, search_id, "saved search")?;
        bind_revision(self, statement.raw, 2, revision, "saved search")?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_saved_search_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_saved_searches(
        &self,
        channel_id: &str,
        limit: usize,
    ) -> Result<Vec<SavedSearch>, String> {
        validate_authority_query(channel_id, limit, "saved search")?;
        let statement = self.prepare("SELECT search_id, channel_id, owner_operator_id, revision, record_json FROM schedule_automation_saved_searches WHERE channel_id=?1 ORDER BY search_id LIMIT ?2")?;
        bind_authority_id(self, statement.raw, 1, channel_id, "channel")?;
        bind_i64(self.database, statement.raw, 2, limit as i64)?;
        let rows = collect_rows(self, statement.raw, read_saved_search_row)?;
        for search in &rows {
            self.validate_current_saved_search_snapshot(search)?;
        }
        Ok(rows)
    }

    pub fn list_saved_search_history(
        &self,
        search_id: &str,
        limit: usize,
    ) -> Result<Vec<SavedSearchHistoryEntry>, String> {
        validate_history_query(search_id, limit, "saved search")?;
        let statement = self.prepare("SELECT search_id, channel_id, owner_operator_id, revision, record_json, changed_by, occurred_at_unix_ms FROM schedule_automation_saved_search_history WHERE search_id=?1 ORDER BY revision LIMIT ?2")?;
        bind_authority_id(self, statement.raw, 1, search_id, "saved search")?;
        bind_i64(self.database, statement.raw, 2, limit as i64)?;
        let mut rows = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => rows.push(SavedSearchHistoryEntry {
                    search: read_saved_search_row(statement.raw)?,
                    changed_by: read_actor(statement.raw, 5)?,
                    occurred_at_unix_ms: read_time(statement.raw, 6)?,
                }),
                SQLITE_DONE => return Ok(rows),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn write_saved_search(
        &self,
        value: &SavedSearch,
        expected_revision: Option<u64>,
        changed_by: &str,
        occurred_at_unix_ms: i64,
    ) -> Result<(), ScheduleAutomationWriteError> {
        value
            .validate()
            .map_err(ScheduleAutomationWriteError::Invalid)?;
        validate_write_attribution(changed_by, occurred_at_unix_ms)?;
        self.execute("BEGIN IMMEDIATE")
            .map_err(ScheduleAutomationWriteError::Storage)?;
        let outcome = (|| {
            let current = self
                .read_saved_search(&value.search_id)
                .map_err(ScheduleAutomationWriteError::Storage)?;
            validate_cas(
                "saved_search",
                current.as_ref().map(|v| v.revision),
                expected_revision,
                value.revision,
            )?;
            if let Some(current) = &current {
                value
                    .validate_successor_of(current, expected_revision.unwrap())
                    .map_err(map_successor_error)?;
            } else {
                ensure_creation_revision(value.revision)?;
                ensure_capacity(
                    self,
                    "schedule_automation_saved_searches",
                    MAX_SAVED_SEARCHES,
                )?;
            }
            write_saved_search_rows(
                self,
                value,
                current.as_ref().map(|v| v.revision),
                changed_by,
                occurred_at_unix_ms,
            )
        })();
        finish_write(self, outcome)
    }

    pub fn read_daypart_block(&self, block_id: &str) -> Result<Option<DaypartBlock>, String> {
        validate_authority_id(block_id, "daypart block")?;
        let statement = self.prepare("SELECT block_id, channel_id, revision, record_json FROM schedule_automation_daypart_blocks WHERE block_id=?1")?;
        bind_authority_id(self, statement.raw, 1, block_id, "daypart block")?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let block = read_daypart_block_row(statement.raw)?;
                self.validate_current_daypart_snapshot(&block)?;
                Ok(Some(block))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn read_daypart_block_revision(
        &self,
        block_id: &str,
        revision: u64,
    ) -> Result<Option<DaypartBlock>, String> {
        validate_authority_revision(block_id, revision, "daypart block")?;
        let statement = self.prepare("SELECT block_id, channel_id, revision, record_json FROM schedule_automation_daypart_block_history WHERE block_id=?1 AND revision=?2")?;
        bind_authority_id(self, statement.raw, 1, block_id, "daypart block")?;
        bind_revision(self, statement.raw, 2, revision, "daypart block")?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_daypart_block_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_daypart_blocks(
        &self,
        channel_id: &str,
        limit: usize,
    ) -> Result<Vec<DaypartBlock>, String> {
        validate_authority_query(channel_id, limit, "daypart block")?;
        let statement = self.prepare("SELECT block_id, channel_id, revision, record_json FROM schedule_automation_daypart_blocks WHERE channel_id=?1 ORDER BY block_id LIMIT ?2")?;
        bind_authority_id(self, statement.raw, 1, channel_id, "channel")?;
        bind_i64(self.database, statement.raw, 2, limit as i64)?;
        let rows = collect_rows(self, statement.raw, read_daypart_block_row)?;
        for block in &rows {
            self.validate_current_daypart_snapshot(block)?;
        }
        Ok(rows)
    }

    pub fn list_daypart_block_history(
        &self,
        block_id: &str,
        limit: usize,
    ) -> Result<Vec<DaypartBlockHistoryEntry>, String> {
        validate_history_query(block_id, limit, "daypart block")?;
        let statement = self.prepare("SELECT block_id, channel_id, revision, record_json, changed_by, occurred_at_unix_ms FROM schedule_automation_daypart_block_history WHERE block_id=?1 ORDER BY revision LIMIT ?2")?;
        bind_authority_id(self, statement.raw, 1, block_id, "daypart block")?;
        bind_i64(self.database, statement.raw, 2, limit as i64)?;
        let mut rows = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => rows.push(DaypartBlockHistoryEntry {
                    block: read_daypart_block_row(statement.raw)?,
                    changed_by: read_actor(statement.raw, 4)?,
                    occurred_at_unix_ms: read_time(statement.raw, 5)?,
                }),
                SQLITE_DONE => return Ok(rows),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn write_daypart_block(
        &self,
        value: &DaypartBlock,
        expected_revision: Option<u64>,
        changed_by: &str,
        occurred_at_unix_ms: i64,
    ) -> Result<(), ScheduleAutomationWriteError> {
        value
            .validate()
            .map_err(ScheduleAutomationWriteError::Invalid)?;
        validate_write_attribution(changed_by, occurred_at_unix_ms)?;
        self.execute("BEGIN IMMEDIATE")
            .map_err(ScheduleAutomationWriteError::Storage)?;
        let outcome = (|| {
            let current = self
                .read_daypart_block(&value.block_id)
                .map_err(ScheduleAutomationWriteError::Storage)?;
            validate_cas(
                "daypart_block",
                current.as_ref().map(|v| v.revision),
                expected_revision,
                value.revision,
            )?;
            if let Some(current) = &current {
                value
                    .validate_successor_of(current, expected_revision.unwrap())
                    .map_err(map_successor_error)?;
            } else {
                ensure_creation_revision(value.revision)?;
                ensure_capacity(
                    self,
                    "schedule_automation_daypart_blocks",
                    MAX_DAYPART_BLOCKS,
                )?;
            }
            write_daypart_block_rows(
                self,
                value,
                current.as_ref().map(|v| v.revision),
                changed_by,
                occurred_at_unix_ms,
            )
        })();
        finish_write(self, outcome)
    }

    pub fn read_recurring_rule(&self, rule_id: &str) -> Result<Option<RecurringRule>, String> {
        validate_authority_id(rule_id, "recurring rule")?;
        let statement = self.prepare("SELECT rule_id, channel_id, saved_search_id, saved_search_revision, block_id, block_revision, revision, record_json FROM schedule_automation_recurring_rules WHERE rule_id=?1")?;
        bind_authority_id(self, statement.raw, 1, rule_id, "recurring rule")?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let rule = read_recurring_rule_row(statement.raw)?;
                self.validate_stored_recurring_rule(&rule)?;
                self.validate_current_recurring_rule_snapshot(&rule)?;
                Ok(Some(rule))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn read_recurring_rule_revision(
        &self,
        rule_id: &str,
        revision: u64,
    ) -> Result<Option<RecurringRule>, String> {
        validate_authority_revision(rule_id, revision, "recurring rule")?;
        let statement = self.prepare("SELECT rule_id, channel_id, saved_search_id, saved_search_revision, block_id, block_revision, revision, record_json FROM schedule_automation_recurring_rule_history WHERE rule_id=?1 AND revision=?2")?;
        bind_authority_id(self, statement.raw, 1, rule_id, "recurring rule")?;
        bind_revision(self, statement.raw, 2, revision, "recurring rule")?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let rule = read_recurring_rule_row(statement.raw)?;
                self.validate_stored_recurring_rule(&rule)?;
                Ok(Some(rule))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    pub fn list_recurring_rules(
        &self,
        channel_id: &str,
        limit: usize,
    ) -> Result<Vec<RecurringRule>, String> {
        validate_authority_query(channel_id, limit, "recurring rule")?;
        let statement = self.prepare("SELECT rule_id, channel_id, saved_search_id, saved_search_revision, block_id, block_revision, revision, record_json FROM schedule_automation_recurring_rules WHERE channel_id=?1 ORDER BY rule_id LIMIT ?2")?;
        bind_authority_id(self, statement.raw, 1, channel_id, "channel")?;
        bind_i64(self.database, statement.raw, 2, limit as i64)?;
        let mut rows = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    let rule = read_recurring_rule_row(statement.raw)?;
                    self.validate_stored_recurring_rule(&rule)?;
                    self.validate_current_recurring_rule_snapshot(&rule)?;
                    rows.push(rule);
                }
                SQLITE_DONE => return Ok(rows),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn list_recurring_rule_history(
        &self,
        rule_id: &str,
        limit: usize,
    ) -> Result<Vec<RecurringRuleHistoryEntry>, String> {
        validate_history_query(rule_id, limit, "recurring rule")?;
        let statement = self.prepare("SELECT rule_id, channel_id, saved_search_id, saved_search_revision, block_id, block_revision, revision, record_json, changed_by, occurred_at_unix_ms FROM schedule_automation_recurring_rule_history WHERE rule_id=?1 ORDER BY revision LIMIT ?2")?;
        bind_authority_id(self, statement.raw, 1, rule_id, "recurring rule")?;
        bind_i64(self.database, statement.raw, 2, limit as i64)?;
        let mut rows = Vec::new();
        loop {
            match unsafe { sqlite3_step(statement.raw) } {
                SQLITE_ROW => {
                    let rule = read_recurring_rule_row(statement.raw)?;
                    self.validate_stored_recurring_rule(&rule)?;
                    rows.push(RecurringRuleHistoryEntry {
                        rule,
                        changed_by: read_actor(statement.raw, 8)?,
                        occurred_at_unix_ms: read_time(statement.raw, 9)?,
                    });
                }
                SQLITE_DONE => return Ok(rows),
                _ => return Err(sqlite_error(self.database)),
            }
        }
    }

    pub fn write_recurring_rule(
        &self,
        value: &RecurringRule,
        expected_revision: Option<u64>,
        changed_by: &str,
        occurred_at_unix_ms: i64,
    ) -> Result<(), ScheduleAutomationWriteError> {
        value
            .validate()
            .map_err(ScheduleAutomationWriteError::Invalid)?;
        validate_write_attribution(changed_by, occurred_at_unix_ms)?;
        self.execute("BEGIN IMMEDIATE")
            .map_err(ScheduleAutomationWriteError::Storage)?;
        let outcome = (|| {
            let search = self
                .read_saved_search(&value.saved_search_id)
                .map_err(ScheduleAutomationWriteError::Storage)?;
            validate_dependency(
                "saved_search",
                value.saved_search_revision,
                search.as_ref().map(|v| v.revision),
            )?;
            let block = self
                .read_daypart_block(&value.block_id)
                .map_err(ScheduleAutomationWriteError::Storage)?;
            validate_dependency(
                "daypart_block",
                value.block_revision,
                block.as_ref().map(|v| v.revision),
            )?;
            let search = search.unwrap();
            let block = block.unwrap();
            value
                .validate_against(&search, &block)
                .map_err(ScheduleAutomationWriteError::Invalid)?;
            let current = self
                .read_recurring_rule(&value.rule_id)
                .map_err(ScheduleAutomationWriteError::Storage)?;
            validate_cas(
                "recurring_rule",
                current.as_ref().map(|v| v.revision),
                expected_revision,
                value.revision,
            )?;
            if let Some(current) = &current {
                if value.rule_id != current.rule_id
                    || value.channel_id != current.channel_id
                    || value.saved_search_id != current.saved_search_id
                    || value.block_id != current.block_id
                {
                    return Err(ScheduleAutomationWriteError::Rebound);
                }
                let current_search = self
                    .read_saved_search_revision(
                        &current.saved_search_id,
                        current.saved_search_revision,
                    )
                    .map_err(ScheduleAutomationWriteError::Storage)?
                    .ok_or_else(|| {
                        ScheduleAutomationWriteError::Storage(
                            "recurring rule historical saved-search authority is missing".into(),
                        )
                    })?;
                let current_block = self
                    .read_daypart_block_revision(&current.block_id, current.block_revision)
                    .map_err(ScheduleAutomationWriteError::Storage)?
                    .ok_or_else(|| {
                        ScheduleAutomationWriteError::Storage(
                            "recurring rule historical daypart authority is missing".into(),
                        )
                    })?;
                value
                    .validate_successor_of(
                        current,
                        expected_revision.unwrap(),
                        &current_search,
                        &current_block,
                        &search,
                        &block,
                    )
                    .map_err(map_successor_error)?;
            } else {
                ensure_creation_revision(value.revision)?;
                ensure_capacity(
                    self,
                    "schedule_automation_recurring_rules",
                    MAX_RECURRING_RULES,
                )?;
            }
            write_recurring_rule_rows(
                self,
                value,
                current.as_ref().map(|v| v.revision),
                changed_by,
                occurred_at_unix_ms,
            )
        })();
        finish_write(self, outcome)
    }

    fn validate_stored_recurring_rule(&self, rule: &RecurringRule) -> Result<(), String> {
        let search = self
            .read_saved_search_revision(&rule.saved_search_id, rule.saved_search_revision)?
            .ok_or_else(|| {
                "recurring rule references unavailable saved-search history".to_string()
            })?;
        let block = self
            .read_daypart_block_revision(&rule.block_id, rule.block_revision)?
            .ok_or_else(|| "recurring rule references unavailable daypart history".to_string())?;
        rule.validate_against(&search, &block)
            .map_err(|error| format!("database returned invalid recurring-rule authority: {error}"))
    }

    fn validate_current_saved_search_snapshot(&self, current: &SavedSearch) -> Result<(), String> {
        let historical = self
            .read_latest_saved_search_snapshot(&current.search_id)?
            .ok_or_else(|| "current saved search has no exact historical snapshot".to_string())?;
        if historical == *current {
            Ok(())
        } else {
            Err("current saved search disagrees with its latest historical snapshot".into())
        }
    }

    fn validate_current_daypart_snapshot(&self, current: &DaypartBlock) -> Result<(), String> {
        let historical = self
            .read_latest_daypart_snapshot(&current.block_id)?
            .ok_or_else(|| "current daypart block has no exact historical snapshot".to_string())?;
        if historical == *current {
            Ok(())
        } else {
            Err("current daypart block disagrees with its latest historical snapshot".into())
        }
    }

    fn validate_current_recurring_rule_snapshot(
        &self,
        current: &RecurringRule,
    ) -> Result<(), String> {
        let historical = self
            .read_latest_recurring_rule_snapshot(&current.rule_id)?
            .ok_or_else(|| "current recurring rule has no exact historical snapshot".to_string())?;
        if historical == *current {
            Ok(())
        } else {
            Err("current recurring rule disagrees with its latest historical snapshot".into())
        }
    }

    fn read_latest_saved_search_snapshot(
        &self,
        search_id: &str,
    ) -> Result<Option<SavedSearch>, String> {
        let statement = self.prepare("SELECT search_id, channel_id, owner_operator_id, revision, record_json FROM schedule_automation_saved_search_history WHERE search_id=?1 ORDER BY revision DESC LIMIT 1")?;
        bind_authority_id(self, statement.raw, 1, search_id, "saved search")?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_saved_search_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn read_latest_daypart_snapshot(&self, block_id: &str) -> Result<Option<DaypartBlock>, String> {
        let statement = self.prepare("SELECT block_id, channel_id, revision, record_json FROM schedule_automation_daypart_block_history WHERE block_id=?1 ORDER BY revision DESC LIMIT 1")?;
        bind_authority_id(self, statement.raw, 1, block_id, "daypart block")?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => Ok(Some(read_daypart_block_row(statement.raw)?)),
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }

    fn read_latest_recurring_rule_snapshot(
        &self,
        rule_id: &str,
    ) -> Result<Option<RecurringRule>, String> {
        let statement = self.prepare("SELECT rule_id, channel_id, saved_search_id, saved_search_revision, block_id, block_revision, revision, record_json FROM schedule_automation_recurring_rule_history WHERE rule_id=?1 ORDER BY revision DESC LIMIT 1")?;
        bind_authority_id(self, statement.raw, 1, rule_id, "recurring rule")?;
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => {
                let rule = read_recurring_rule_row(statement.raw)?;
                self.validate_stored_recurring_rule(&rule)?;
                Ok(Some(rule))
            }
            SQLITE_DONE => Ok(None),
            _ => Err(sqlite_error(self.database)),
        }
    }
}

fn validate_authority_id(value: &str, kind: &str) -> Result<(), String> {
    if is_canonical_uuid(value) {
        Ok(())
    } else {
        Err(format!("{kind} identity is invalid"))
    }
}
fn validate_authority_revision(value: &str, revision: u64, kind: &str) -> Result<(), String> {
    validate_authority_id(value, kind)?;
    if revision == 0 {
        Err(format!("{kind} revision is invalid"))
    } else {
        Ok(())
    }
}
fn validate_authority_query(channel_id: &str, limit: usize, kind: &str) -> Result<(), String> {
    if is_canonical_uuid(channel_id) && (1..=MAX_AUTHORITY_QUERY).contains(&limit) {
        Ok(())
    } else {
        Err(format!("{kind} query is invalid or unbounded"))
    }
}
fn validate_history_query(id: &str, limit: usize, kind: &str) -> Result<(), String> {
    if is_canonical_uuid(id) && (1..=MAX_AUTHORITY_QUERY).contains(&limit) {
        Ok(())
    } else {
        Err(format!("{kind} history query is invalid or unbounded"))
    }
}

fn validate_write_attribution(
    actor: &str,
    occurred_at: i64,
) -> Result<(), ScheduleAutomationWriteError> {
    if occurred_at < 0 || !secret_field(actor, 200) {
        Err(ScheduleAutomationWriteError::Invalid(
            "schedule automation change attribution is invalid".into(),
        ))
    } else {
        Ok(())
    }
}

fn validate_cas(
    authority: &'static str,
    current: Option<u64>,
    expected: Option<u64>,
    incoming: u64,
) -> Result<(), ScheduleAutomationWriteError> {
    if current.is_none() && expected.is_some() {
        return Err(ScheduleAutomationWriteError::NotFound { authority });
    }
    if current != expected
        || incoming != current.map_or(1, |revision| revision.checked_add(1).unwrap_or(0))
    {
        Err(ScheduleAutomationWriteError::Conflict {
            current_revision: current,
        })
    } else {
        Ok(())
    }
}
fn ensure_creation_revision(revision: u64) -> Result<(), ScheduleAutomationWriteError> {
    if revision == 1 {
        Ok(())
    } else {
        Err(ScheduleAutomationWriteError::Conflict {
            current_revision: None,
        })
    }
}
fn validate_dependency(
    authority: &'static str,
    requested: u64,
    current: Option<u64>,
) -> Result<(), ScheduleAutomationWriteError> {
    match current {
        None => Err(ScheduleAutomationWriteError::NotFound { authority }),
        Some(current_revision) if current_revision != requested => {
            Err(ScheduleAutomationWriteError::DependencyConflict {
                authority,
                requested_revision: requested,
                current_revision: Some(current_revision),
            })
        }
        Some(_) => Ok(()),
    }
}
fn map_successor_error(message: String) -> ScheduleAutomationWriteError {
    if message.contains("rebind") {
        ScheduleAutomationWriteError::Rebound
    } else if message.contains("CAS") || message.contains("revision") {
        ScheduleAutomationWriteError::Conflict {
            current_revision: None,
        }
    } else {
        ScheduleAutomationWriteError::Invalid(message)
    }
}

fn finish_write(
    store: &StationStore,
    outcome: Result<(), ScheduleAutomationWriteError>,
) -> Result<(), ScheduleAutomationWriteError> {
    match outcome {
        Ok(()) => store
            .execute("COMMIT")
            .map_err(ScheduleAutomationWriteError::Storage)
            .inspect_err(|_| {
                let _ = store.execute("ROLLBACK");
            }),
        Err(error) => {
            let _ = store.execute("ROLLBACK");
            Err(error)
        }
    }
}

fn ensure_capacity(
    store: &StationStore,
    table: &str,
    maximum: usize,
) -> Result<(), ScheduleAutomationWriteError> {
    let statement = store
        .prepare(&format!("SELECT 1 FROM {table} LIMIT {}", maximum + 1))
        .map_err(ScheduleAutomationWriteError::Storage)?;
    let mut count = 0usize;
    loop {
        match unsafe { sqlite3_step(statement.raw) } {
            SQLITE_ROW => count += 1,
            SQLITE_DONE if count < maximum => return Ok(()),
            SQLITE_DONE => return Err(ScheduleAutomationWriteError::Capacity),
            _ => {
                return Err(ScheduleAutomationWriteError::Storage(sqlite_error(
                    store.database,
                )));
            }
        }
    }
}

fn bind_authority_id(
    store: &StationStore,
    statement: *mut SqliteStatement,
    index: c_int,
    value: &str,
    field: &str,
) -> Result<(), String> {
    let value = c_string(value, field)?;
    bind_text(store.database, statement, index, &value)
}
fn bind_revision(
    store: &StationStore,
    statement: *mut SqliteStatement,
    index: c_int,
    revision: u64,
    field: &str,
) -> Result<(), String> {
    bind_i64(
        store.database,
        statement,
        index,
        checked_i64(revision, field)?,
    )
}
fn read_actor(statement: *mut SqliteStatement, column: c_int) -> Result<String, String> {
    let actor = column_text(statement, column)?;
    if secret_field(&actor, 200) {
        Ok(actor)
    } else {
        Err("schedule automation history actor is invalid".into())
    }
}
fn read_time(statement: *mut SqliteStatement, column: c_int) -> Result<i64, String> {
    let value = unsafe { sqlite3_column_int64(statement, column) };
    if value >= 0 {
        Ok(value)
    } else {
        Err("schedule automation history time is invalid".into())
    }
}
fn collect_rows<T>(
    store: &StationStore,
    statement: *mut SqliteStatement,
    read: fn(*mut SqliteStatement) -> Result<T, String>,
) -> Result<Vec<T>, String> {
    let mut rows = Vec::new();
    loop {
        match unsafe { sqlite3_step(statement) } {
            SQLITE_ROW => rows.push(read(statement)?),
            SQLITE_DONE => return Ok(rows),
            _ => return Err(sqlite_error(store.database)),
        }
    }
}

fn read_saved_search_row(statement: *mut SqliteStatement) -> Result<SavedSearch, String> {
    let value: SavedSearch = serde_json::from_str(&column_text(statement, 4)?)
        .map_err(|e| format!("decode saved-search authority: {e}"))?;
    value
        .validate()
        .map_err(|e| format!("database returned invalid saved-search authority: {e}"))?;
    if value.search_id != column_text(statement, 0)?
        || value.channel_id != column_text(statement, 1)?
        || value.owner_operator_id != column_text(statement, 2)?
        || value.revision != column_u64(statement, 3, "saved search revision")?
    {
        return Err("saved-search indexed authority and JSON disagree".into());
    }
    Ok(value)
}
fn read_daypart_block_row(statement: *mut SqliteStatement) -> Result<DaypartBlock, String> {
    let value: DaypartBlock = serde_json::from_str(&column_text(statement, 3)?)
        .map_err(|e| format!("decode daypart authority: {e}"))?;
    value
        .validate()
        .map_err(|e| format!("database returned invalid daypart authority: {e}"))?;
    if value.block_id != column_text(statement, 0)?
        || value.channel_id != column_text(statement, 1)?
        || value.revision != column_u64(statement, 2, "daypart revision")?
    {
        return Err("daypart indexed authority and JSON disagree".into());
    }
    Ok(value)
}
fn read_recurring_rule_row(statement: *mut SqliteStatement) -> Result<RecurringRule, String> {
    let value: RecurringRule = serde_json::from_str(&column_text(statement, 7)?)
        .map_err(|e| format!("decode recurring-rule authority: {e}"))?;
    if value.rule_id != column_text(statement, 0)?
        || value.channel_id != column_text(statement, 1)?
        || value.saved_search_id != column_text(statement, 2)?
        || value.saved_search_revision != column_u64(statement, 3, "rule saved-search revision")?
        || value.block_id != column_text(statement, 4)?
        || value.block_revision != column_u64(statement, 5, "rule block revision")?
        || value.revision != column_u64(statement, 6, "rule revision")?
    {
        return Err("recurring-rule indexed authority and JSON disagree".into());
    }
    Ok(value)
}

fn write_saved_search_rows(
    store: &StationStore,
    value: &SavedSearch,
    prior: Option<u64>,
    actor: &str,
    at: i64,
) -> Result<(), ScheduleAutomationWriteError> {
    let json = serde_json::to_string(value).map_err(|e| {
        ScheduleAutomationWriteError::Storage(format!("serialize saved search: {e}"))
    })?;
    write_current(
        store,
        "schedule_automation_saved_searches",
        &[
            "search_id",
            "channel_id",
            "owner_operator_id",
            "revision",
            "record_json",
        ],
        &[
            &value.search_id,
            &value.channel_id,
            &value.owner_operator_id,
        ],
        value.revision,
        &json,
        prior,
    )?;
    insert_history(
        store,
        "schedule_automation_saved_search_history",
        &[
            "search_id",
            "channel_id",
            "owner_operator_id",
            "revision",
            "record_json",
            "changed_by",
            "occurred_at_unix_ms",
        ],
        &[
            &value.search_id,
            &value.channel_id,
            &value.owner_operator_id,
        ],
        value.revision,
        &json,
        (actor, at),
    )
}
fn write_daypart_block_rows(
    store: &StationStore,
    value: &DaypartBlock,
    prior: Option<u64>,
    actor: &str,
    at: i64,
) -> Result<(), ScheduleAutomationWriteError> {
    let json = serde_json::to_string(value)
        .map_err(|e| ScheduleAutomationWriteError::Storage(format!("serialize daypart: {e}")))?;
    write_current(
        store,
        "schedule_automation_daypart_blocks",
        &["block_id", "channel_id", "revision", "record_json"],
        &[&value.block_id, &value.channel_id],
        value.revision,
        &json,
        prior,
    )?;
    insert_history(
        store,
        "schedule_automation_daypart_block_history",
        &[
            "block_id",
            "channel_id",
            "revision",
            "record_json",
            "changed_by",
            "occurred_at_unix_ms",
        ],
        &[&value.block_id, &value.channel_id],
        value.revision,
        &json,
        (actor, at),
    )
}

fn write_recurring_rule_rows(
    store: &StationStore,
    value: &RecurringRule,
    prior: Option<u64>,
    actor: &str,
    at: i64,
) -> Result<(), ScheduleAutomationWriteError> {
    let json = serde_json::to_string(value).map_err(|e| {
        ScheduleAutomationWriteError::Storage(format!("serialize recurring rule: {e}"))
    })?;
    let table = "schedule_automation_recurring_rules";
    let statement = if prior.is_some() { store.prepare("UPDATE schedule_automation_recurring_rules SET channel_id=?2, saved_search_id=?3, saved_search_revision=?4, block_id=?5, block_revision=?6, revision=?7, record_json=?8 WHERE rule_id=?1 AND revision=?9") } else { store.prepare("INSERT INTO schedule_automation_recurring_rules(rule_id,channel_id,saved_search_id,saved_search_revision,block_id,block_revision,revision,record_json) VALUES(?1,?2,?3,?4,?5,?6,?7,?8)") }.map_err(ScheduleAutomationWriteError::Storage)?;
    for (i, text) in [
        &value.rule_id,
        &value.channel_id,
        &value.saved_search_id,
        &value.block_id,
    ]
    .iter()
    .enumerate()
    {
        let bind_index = [1, 2, 3, 5][i];
        bind_authority_id(store, statement.raw, bind_index, text, table)
            .map_err(ScheduleAutomationWriteError::Storage)?;
    }
    bind_revision(
        store,
        statement.raw,
        4,
        value.saved_search_revision,
        "saved search revision",
    )
    .map_err(ScheduleAutomationWriteError::Storage)?;
    bind_revision(
        store,
        statement.raw,
        6,
        value.block_revision,
        "block revision",
    )
    .map_err(ScheduleAutomationWriteError::Storage)?;
    bind_revision(store, statement.raw, 7, value.revision, "rule revision")
        .map_err(ScheduleAutomationWriteError::Storage)?;
    let json_c = c_string(&json, "rule JSON").map_err(ScheduleAutomationWriteError::Storage)?;
    bind_text(store.database, statement.raw, 8, &json_c)
        .map_err(ScheduleAutomationWriteError::Storage)?;
    if let Some(prior) = prior {
        bind_revision(store, statement.raw, 9, prior, "prior rule revision")
            .map_err(ScheduleAutomationWriteError::Storage)?;
    }
    expect_done(store.database, statement.raw).map_err(ScheduleAutomationWriteError::Storage)?;
    if store.changes() != 1 {
        return Err(ScheduleAutomationWriteError::Conflict {
            current_revision: prior,
        });
    }
    let history = store.prepare("INSERT INTO schedule_automation_recurring_rule_history(rule_id,channel_id,saved_search_id,saved_search_revision,block_id,block_revision,revision,record_json,changed_by,occurred_at_unix_ms) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)").map_err(ScheduleAutomationWriteError::Storage)?;
    for (i, text) in [
        &value.rule_id,
        &value.channel_id,
        &value.saved_search_id,
        &value.block_id,
    ]
    .iter()
    .enumerate()
    {
        bind_authority_id(store, history.raw, [1, 2, 3, 5][i], text, "rule history")
            .map_err(ScheduleAutomationWriteError::Storage)?;
    }
    bind_revision(
        store,
        history.raw,
        4,
        value.saved_search_revision,
        "saved search revision",
    )
    .map_err(ScheduleAutomationWriteError::Storage)?;
    bind_revision(
        store,
        history.raw,
        6,
        value.block_revision,
        "block revision",
    )
    .map_err(ScheduleAutomationWriteError::Storage)?;
    bind_revision(store, history.raw, 7, value.revision, "rule revision")
        .map_err(ScheduleAutomationWriteError::Storage)?;
    bind_text(store.database, history.raw, 8, &json_c)
        .map_err(ScheduleAutomationWriteError::Storage)?;
    let actor_c = c_string(actor, "actor").map_err(ScheduleAutomationWriteError::Invalid)?;
    bind_text(store.database, history.raw, 9, &actor_c)
        .map_err(ScheduleAutomationWriteError::Storage)?;
    bind_i64(store.database, history.raw, 10, at).map_err(ScheduleAutomationWriteError::Storage)?;
    expect_done(store.database, history.raw).map_err(ScheduleAutomationWriteError::Storage)
}

fn write_current(
    store: &StationStore,
    table: &str,
    columns: &[&str],
    texts: &[&str],
    revision: u64,
    json: &str,
    prior: Option<u64>,
) -> Result<(), ScheduleAutomationWriteError> {
    let sql = if prior.is_some() {
        format!(
            "UPDATE {table} SET {} WHERE {}=?1 AND revision=?{}",
            columns[1..]
                .iter()
                .enumerate()
                .map(|(i, c)| format!("{c}=?{}", i + 2))
                .collect::<Vec<_>>()
                .join(","),
            columns[0],
            columns.len() + 1
        )
    } else {
        format!(
            "INSERT INTO {table}({}) VALUES({})",
            columns.join(","),
            (1..=columns.len())
                .map(|i| format!("?{i}"))
                .collect::<Vec<_>>()
                .join(",")
        )
    };
    let statement = store
        .prepare(&sql)
        .map_err(ScheduleAutomationWriteError::Storage)?;
    for (index, text) in texts.iter().enumerate() {
        bind_authority_id(store, statement.raw, (index + 1) as c_int, text, table)
            .map_err(ScheduleAutomationWriteError::Storage)?;
    }
    bind_revision(
        store,
        statement.raw,
        (texts.len() + 1) as c_int,
        revision,
        "authority revision",
    )
    .map_err(ScheduleAutomationWriteError::Storage)?;
    let json_c = c_string(json, "authority JSON").map_err(ScheduleAutomationWriteError::Storage)?;
    bind_text(
        store.database,
        statement.raw,
        (texts.len() + 2) as c_int,
        &json_c,
    )
    .map_err(ScheduleAutomationWriteError::Storage)?;
    if let Some(prior) = prior {
        bind_revision(
            store,
            statement.raw,
            (columns.len() + 1) as c_int,
            prior,
            "prior revision",
        )
        .map_err(ScheduleAutomationWriteError::Storage)?;
    }
    expect_done(store.database, statement.raw).map_err(ScheduleAutomationWriteError::Storage)?;
    if store.changes() == 1 {
        Ok(())
    } else {
        Err(ScheduleAutomationWriteError::Conflict {
            current_revision: prior,
        })
    }
}

fn insert_history(
    store: &StationStore,
    table: &str,
    columns: &[&str],
    texts: &[&str],
    revision: u64,
    json: &str,
    audit: (&str, i64),
) -> Result<(), ScheduleAutomationWriteError> {
    let (actor, at) = audit;
    let sql = format!(
        "INSERT INTO {table}({}) VALUES({})",
        columns.join(","),
        (1..=columns.len())
            .map(|i| format!("?{i}"))
            .collect::<Vec<_>>()
            .join(",")
    );
    let statement = store
        .prepare(&sql)
        .map_err(ScheduleAutomationWriteError::Storage)?;
    for (index, text) in texts.iter().enumerate() {
        bind_authority_id(store, statement.raw, (index + 1) as c_int, text, table)
            .map_err(ScheduleAutomationWriteError::Storage)?;
    }
    bind_revision(
        store,
        statement.raw,
        (texts.len() + 1) as c_int,
        revision,
        "history revision",
    )
    .map_err(ScheduleAutomationWriteError::Storage)?;
    let json_c = c_string(json, "history JSON").map_err(ScheduleAutomationWriteError::Storage)?;
    bind_text(
        store.database,
        statement.raw,
        (texts.len() + 2) as c_int,
        &json_c,
    )
    .map_err(ScheduleAutomationWriteError::Storage)?;
    let actor_c =
        c_string(actor, "history actor").map_err(ScheduleAutomationWriteError::Invalid)?;
    bind_text(
        store.database,
        statement.raw,
        (texts.len() + 3) as c_int,
        &actor_c,
    )
    .map_err(ScheduleAutomationWriteError::Storage)?;
    bind_i64(
        store.database,
        statement.raw,
        (texts.len() + 4) as c_int,
        at,
    )
    .map_err(ScheduleAutomationWriteError::Storage)?;
    expect_done(store.database, statement.raw).map_err(ScheduleAutomationWriteError::Storage)
}
