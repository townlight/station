//! Trusted adapter from the durable worker journal to persisted as-run authority.
//!
//! The journal CRC detects accidental corruption, not authentication. This
//! adapter trusts the local worker-journal ownership boundary; its evidence hash
//! binds the complete canonical protocol frame (including channel, worker boot,
//! report and media-offset facts) plus configured journal identity.
use super::StationStore;
use sha2::{Digest, Sha256};
use station_media_journal::inspect_journal_bounded;
use station_media_protocol::{WorkerEvent, encode_event_frame};
use station_reporting::runtime::{
    AsRunProjectionRuntime, AsRunState, JournalTake, ProjectionInput, ReportingRuntime,
};
use std::path::Path;
const MAX_JOURNAL_EVENTS: usize = 10_000;
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AsRunJournalAdapterError {
    Invalid(String),
    Journal(String),
    Storage(String),
}

/// Projects only closed intervals. The newest start stays pending until another
/// durable timed event closes it, so no synthetic trailing interval can block a
/// later append/replay of the journal.
#[allow(clippy::too_many_arguments)] // Public fixed-scope adapter boundary; no browser input.
pub fn project_durable_journal(
    store: &StationStore,
    journal_path: impl AsRef<Path>,
    expected_worker_id: &str,
    channel_id: &str,
    station_timezone: &str,
    starts_at_unix_ms: i64,
    ends_at_unix_ms: i64,
    now_utc_ms: i64,
) -> Result<AsRunProjectionRuntime, AsRunJournalAdapterError> {
    if !uuid(channel_id)
        || expected_worker_id != format!("station-worker-{channel_id}")
        || starts_at_unix_ms < 0
        || ends_at_unix_ms <= starts_at_unix_ms
        || ends_at_unix_ms > now_utc_ms
    {
        return Err(AsRunJournalAdapterError::Invalid(
            "journal projection scope is invalid or extends beyond now".into(),
        ));
    };
    let inspection = inspect_journal_bounded(journal_path, MAX_JOURNAL_EVENTS)
        .map_err(|e| AsRunJournalAdapterError::Journal(format!("{e:?}")))?;
    let source_journal_id = inspection.worker_id.ok_or_else(|| {
        AsRunJournalAdapterError::Journal(
            "durable journal has no authoritative worker identity".into(),
        )
    })?;
    if source_journal_id != expected_worker_id
        || inspection.channel_id.as_deref() != Some(channel_id)
    {
        return Err(AsRunJournalAdapterError::Journal(
            "journal channel identity does not match the requested channel".into(),
        ));
    }
    let events = inspection.retained_events;
    let mut scheduled = Vec::new();
    let mut untimed_transitions = Vec::new();
    for envelope in events {
        if let WorkerEvent::ScheduledAssetOnAir {
            schedule_item_id,
            schedule_item_revision,
            asset_id,
            actual_on_air_at_unix_ms,
            ..
        } = &envelope.event
            && *actual_on_air_at_unix_ms >= starts_at_unix_ms
        {
            scheduled.push((
                envelope.sequence,
                schedule_item_id.clone(),
                *schedule_item_revision,
                asset_id.clone(),
                *actual_on_air_at_unix_ms,
                frame_hash(&source_journal_id, &envelope)?,
            ));
        }
        if matches!(
            envelope.event,
            WorkerEvent::OnAirChanged { .. }
                | WorkerEvent::LiveOnAir { .. }
                | WorkerEvent::LiveReturned { .. }
                | WorkerEvent::CgApplied { .. }
                | WorkerEvent::CgCleared
                | WorkerEvent::CaptionCueAccepted { .. }
                | WorkerEvent::CaptionCuesCleared
        ) {
            untimed_transitions.push(envelope.sequence);
        }
    }
    scheduled.sort_by_key(|entry| entry.4);
    let lookahead = scheduled
        .iter()
        .find(|entry| entry.4 >= ends_at_unix_ms)
        .cloned();
    scheduled.retain(|entry| entry.4 < ends_at_unix_ms);
    if let Some(next) = lookahead {
        scheduled.push(next);
    }
    if scheduled.len() < 2 {
        return Err(AsRunJournalAdapterError::Journal(
            "no closed journal-derived as-run interval is available yet".into(),
        ));
    };
    let mut takes = Vec::with_capacity(scheduled.len() - 1);
    for (index, (source_sequence, item, revision, asset, start, hash)) in
        scheduled.iter().enumerate().take(scheduled.len() - 1)
    {
        let end = scheduled[index + 1].4;
        let next_source_sequence = scheduled[index + 1].0;
        if untimed_transitions
            .iter()
            .any(|sequence| *sequence > *source_sequence && *sequence < next_source_sequence)
        {
            return Err(AsRunJournalAdapterError::Journal(
                "an untimed live, fallback, CG, or caption transition prevents an exact as-run interval; no airtime was fabricated".into(),
            ));
        }
        if *start < 0 || end <= *start || end > now_utc_ms {
            return Err(AsRunJournalAdapterError::Journal(
                "scheduled as-run journal timestamps are nonmonotonic or future".into(),
            ));
        };
        takes.push(JournalTake{journal_id:source_journal_id.clone(),journal_sequence:*source_sequence,channel_id:channel_id.into(),started_at_unix_ms:*start,ended_at_unix_ms:end,state:AsRunState::Program,content_id:Some(asset.clone()),franchise_id:None,category:None,title:None,catalog_revision:None,schedule_revision:None,journal_hash:hash.clone(),unavailable_reason:Some(format!("Exact immutable schedule/catalog snapshot authority is unavailable for journal item {item} revision {revision}; no mutable current-row substitution was used."))});
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
fn frame_hash(
    journal_id: &str,
    envelope: &station_media_protocol::WorkerEventEnvelope,
) -> Result<String, AsRunJournalAdapterError> {
    let frame = encode_event_frame(envelope).map_err(|e| {
        AsRunJournalAdapterError::Journal(format!("canonical event frame unavailable: {e:?}"))
    })?;
    let mut h = Sha256::new();
    h.update(b"townlight-asrun-journal-frame-v1\0");
    h.update(journal_id.as_bytes());
    h.update([0]);
    h.update(frame);
    Ok(format!("{:x}", h.finalize()))
}
fn uuid(v: &str) -> bool {
    v.len() == 36
        && v.as_bytes().iter().enumerate().all(|(i, b)| {
            if matches!(i, 8 | 13 | 18 | 23) {
                *b == b'-'
            } else if i == 14 {
                *b == b'4'
            } else if i == 19 {
                matches!(*b, b'8' | b'9' | b'a' | b'b')
            } else {
                b.is_ascii_digit() || matches!(*b, b'a'..=b'f')
            }
        })
}
