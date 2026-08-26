//! Trusted adapter from the durable worker journal to persisted as-run authority.
use std::path::Path;

use sha2::{Digest, Sha256};
use station_media_journal::read_events;
use station_media_protocol::WorkerEvent;
use station_reporting::runtime::{
    AsRunProjectionRuntime, AsRunState, JournalTake, ProjectionInput, ReportingRuntime,
};

use super::StationStore;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AsRunJournalAdapterError {
    Invalid(String),
    Journal(String),
    Storage(String),
}

/// Reads only durable journal frames. The caller supplies a fixed reporting
/// window and journal identity from station configuration; no browser input is
/// accepted by this boundary. Untimed worker events cannot establish an aired
/// interval and are therefore represented by explicit gap intervals.
pub fn project_durable_journal(
    store: &StationStore,
    journal_path: impl AsRef<Path>,
    journal_id: &str,
    channel_id: &str,
    station_timezone: &str,
    starts_at_unix_ms: i64,
    ends_at_unix_ms: i64,
    now_utc_ms: i64,
) -> Result<AsRunProjectionRuntime, AsRunJournalAdapterError> {
    if !uuid(journal_id)
        || !uuid(channel_id)
        || starts_at_unix_ms < 0
        || ends_at_unix_ms <= starts_at_unix_ms
        || now_utc_ms < 0
    {
        return Err(AsRunJournalAdapterError::Invalid(
            "journal projection scope is invalid".into(),
        ));
    }
    let events = read_events(journal_path)
        .map_err(|e| AsRunJournalAdapterError::Journal(format!("{e:?}")))?;
    if events.iter().any(|event| event.channel_id != channel_id) {
        return Err(AsRunJournalAdapterError::Journal(
            "journal channel identity does not match the requested channel".into(),
        ));
    }
    let mut takes = Vec::new();
    let mut cursor = starts_at_unix_ms;
    let mut projection_sequence = 1_u64;
    let scheduled: Vec<_> = events
        .into_iter()
        .filter_map(|envelope| match envelope.event {
            WorkerEvent::ScheduledAssetOnAir {
                report_id: _,
                schedule_item_id,
                schedule_item_revision,
                asset_id,
                actual_on_air_at_unix_ms,
                ..
            } => Some((
                envelope.sequence,
                schedule_item_id,
                schedule_item_revision,
                asset_id,
                actual_on_air_at_unix_ms,
            )),
            _ => None,
        })
        .filter(|item| item.4 >= starts_at_unix_ms && item.4 < ends_at_unix_ms)
        .collect();
    for (index, (raw_sequence, item_id, item_revision, asset_id, actual_start)) in
        scheduled.iter().enumerate()
    {
        if *actual_start < cursor {
            return Err(AsRunJournalAdapterError::Journal(
                "scheduled as-run journal timestamps are nonmonotonic".into(),
            ));
        }
        if *actual_start > cursor {
            takes.push(gap(
                journal_id,
                projection_sequence,
                channel_id,
                cursor,
                *actual_start,
                raw_sequence,
            ));
            projection_sequence += 1;
        }
        let end = scheduled
            .get(index + 1)
            .map(|next| next.4)
            .unwrap_or(ends_at_unix_ms);
        if end <= *actual_start {
            return Err(AsRunJournalAdapterError::Journal(
                "scheduled as-run journal interval is invalid".into(),
            ));
        }
        let (title, catalog_revision, category, reason) = match store.read_schedule_item(item_id).map_err(AsRunJournalAdapterError::Storage)? {
            Some(schedule) if schedule.revision == *item_revision && schedule.channel_id == channel_id && schedule.asset_id == *asset_id => match store.read_catalog_asset(asset_id).map_err(AsRunJournalAdapterError::Storage)? { Some(catalog) => (catalog.title, catalog.revision, "unavailable".into(), Some("Franchise and category authority are unavailable in the current catalog schema.".into())), None => (String::new(),0,"unavailable".into(),Some("Catalog authority is unavailable for this journal-derived asset.".into())) },
            _ => (String::new(),0,"unavailable".into(),Some("Exact schedule item/revision authority is unavailable for this journal-derived event.".into())),
        };
        takes.push(JournalTake {
            journal_id: journal_id.into(),
            journal_sequence: projection_sequence,
            channel_id: channel_id.into(),
            started_at_unix_ms: *actual_start,
            ended_at_unix_ms: end,
            state: AsRunState::Program,
            content_id: asset_id.clone(),
            franchise_id: String::new(),
            category,
            title,
            catalog_revision: catalog_revision.max(1),
            schedule_revision: (*item_revision).max(1),
            journal_hash: event_hash(
                *raw_sequence,
                item_id,
                item_revision,
                asset_id,
                *actual_start,
            ),
            unavailable_reason: reason,
        });
        projection_sequence += 1;
        cursor = end;
    }
    if cursor < ends_at_unix_ms {
        takes.push(gap(
            journal_id,
            projection_sequence,
            channel_id,
            cursor,
            ends_at_unix_ms,
            &0,
        ));
    }
    let mut runtime = ReportingRuntime::new();
    let projection = runtime
        .project(
            ProjectionInput::new(channel_id, station_timezone, takes)
                .map_err(AsRunJournalAdapterError::Invalid)?,
        )
        .map_err(AsRunJournalAdapterError::Invalid)?;
    store
        .persist_asrun_projection(&projection, now_utc_ms)
        .map_err(|e| AsRunJournalAdapterError::Storage(format!("{e:?}")))?;
    Ok(projection)
}
fn gap(
    journal_id: &str,
    sequence: u64,
    channel_id: &str,
    start: i64,
    end: i64,
    raw: &u64,
) -> JournalTake {
    JournalTake{journal_id:journal_id.into(),journal_sequence:sequence,channel_id:channel_id.into(),started_at_unix_ms:start,ended_at_unix_ms:end,state:AsRunState::Gap,content_id:String::new(),franchise_id:String::new(),category:"unavailable".into(),title:String::new(),catalog_revision:1,schedule_revision:1,journal_hash:event_hash(*raw,"",&0,"",start),unavailable_reason:Some("No timed scheduled-airing journal event covers this interval; no aired content is asserted.".into())}
}
fn event_hash(sequence: u64, item: &str, revision: &u64, asset: &str, time: i64) -> String {
    let mut h = Sha256::new();
    h.update(sequence.to_le_bytes());
    h.update(item.as_bytes());
    h.update(revision.to_le_bytes());
    h.update(asset.as_bytes());
    h.update(time.to_le_bytes());
    format!("{:x}", h.finalize())
}
fn uuid(value: &str) -> bool {
    value.len() == 36
        && value
            .as_bytes()
            .iter()
            .enumerate()
            .all(|(i, b)| matches!(i, 8 | 13 | 18 | 23) && *b == b'-' || b.is_ascii_hexdigit())
}
