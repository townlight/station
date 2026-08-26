//! Trusted incremental adapter from the durable worker journal to persisted
//! as-run authority. The journal CRC detects accidental corruption, not
//! authentication; this adapter binds the reader cursor to worker identity.
use super::StationStore;
use jiff::{Timestamp, civil::DateTime, tz::TimeZone};
use sha2::{Digest, Sha256};
use station_media_journal::{
    JournalCursor, JournalError, find_journal_event_cursor, read_journal_incremental,
};
use station_media_protocol::{WorkerEvent, WorkerEventEnvelope, encode_event_frame};
use station_reporting::runtime::{
    AsRunProjectionRuntime, AsRunState, JournalTake, PendingScheduledTake, ProjectionCheckpoint,
    ProjectionInput, ProjectionWindow, ReportingRuntime,
};
use std::path::Path;

const MAX_INCREMENTAL_JOURNAL_EVENTS: usize = 2_048;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AsRunJournalAdapterError {
    Invalid(String),
    Journal(String),
    Storage(String),
}

/// Exact half-open UTC bounds for the current station-local calendar day.
/// `TimeZone::get` validates the configured IANA name. The UTC length is
/// intentionally not fixed: it is 23 or 25 hours around America/Denver DST.
pub fn station_local_day_window(
    station_timezone: &str,
    now_utc_ms: i64,
) -> Result<ProjectionWindow, AsRunJournalAdapterError> {
    if now_utc_ms < 0 || station_timezone.trim().is_empty() || station_timezone.len() > 80 {
        return Err(AsRunJournalAdapterError::Invalid(
            "station timezone or projection time is invalid".into(),
        ));
    }
    let zone = TimeZone::get(station_timezone).map_err(|error| {
        AsRunJournalAdapterError::Invalid(format!("station IANA timezone is unavailable: {error}"))
    })?;
    let timestamp = Timestamp::from_millisecond(now_utc_ms).map_err(|error| {
        AsRunJournalAdapterError::Invalid(format!("projection timestamp is invalid: {error}"))
    })?;
    let local = timestamp.to_zoned(zone.clone());
    let local_date = format!(
        "{:04}-{:02}-{:02}",
        local.year(),
        local.month(),
        local.day()
    );
    let start = local_midnight(&zone, local.year(), local.month(), local.day())?;
    let tomorrow = local.date().tomorrow().map_err(|error| {
        AsRunJournalAdapterError::Invalid(format!("local calendar date is invalid: {error}"))
    })?;
    let end = local_midnight(&zone, tomorrow.year(), tomorrow.month(), tomorrow.day())?;
    if start < 0 || end <= start {
        return Err(AsRunJournalAdapterError::Invalid(
            "station-local calendar day has invalid UTC bounds".into(),
        ));
    }
    Ok(ProjectionWindow {
        local_date,
        starts_at_utc_ms: start,
        ends_at_utc_ms: end,
    })
}

fn local_midnight(
    zone: &TimeZone,
    year: i16,
    month: i8,
    day: i8,
) -> Result<i64, AsRunJournalAdapterError> {
    let civil: DateTime = format!("{year:04}-{month:02}-{day:02}T00:00:00")
        .parse()
        .map_err(|error| {
            AsRunJournalAdapterError::Invalid(format!("local midnight is invalid: {error}"))
        })?;
    let zoned = match zone.to_ambiguous_zoned(civil).unambiguous() {
        Ok(value) => value,
        Err(_) => {
            let earlier = zone.to_ambiguous_zoned(civil).earlier().map_err(|error| {
                AsRunJournalAdapterError::Invalid(format!(
                    "local midnight fold is invalid: {error}"
                ))
            })?;
            if earlier.datetime() == civil {
                earlier
            } else {
                zone.to_ambiguous_zoned(civil).later().map_err(|error| {
                    AsRunJournalAdapterError::Invalid(format!(
                        "local midnight gap is invalid: {error}"
                    ))
                })?
            }
        }
    };
    Ok(zoned.timestamp().as_millisecond())
}

/// Incrementally projects closed scheduled intervals. Cursor and output are
/// written by one SQLite transaction, so a crash replays at most a suffix and
/// cannot skip unpersisted projection output.
pub fn project_durable_journal(
    store: &StationStore,
    journal_path: impl AsRef<Path>,
    expected_worker_id: &str,
    channel_id: &str,
    station_timezone: &str,
    now_utc_ms: i64,
    verify_checkpoint_prefix: bool,
) -> Result<AsRunProjectionRuntime, AsRunJournalAdapterError> {
    if !uuid(channel_id)
        || expected_worker_id != format!("station-worker-{channel_id}")
        || now_utc_ms < 0
    {
        return Err(AsRunJournalAdapterError::Invalid(
            "journal projection identity or time is invalid".into(),
        ));
    }
    let window = station_local_day_window(station_timezone, now_utc_ms)?;
    let existing = store
        .read_asrun_projection(channel_id)
        .map_err(AsRunJournalAdapterError::Storage)?;
    let continues_window = existing.as_ref().is_some_and(|projection| {
        projection.station_timezone == station_timezone
            && projection.window.as_ref() == Some(&window)
    });
    let mut entries = if continues_window {
        existing
            .as_ref()
            .map_or_else(Vec::new, |projection| projection.entries.clone())
    } else {
        Vec::new()
    };
    let prior = existing.as_ref().map(|projection| &projection.checkpoint);
    let mut pending = prior.and_then(|checkpoint| checkpoint.pending_start.clone());
    let mut pending_is_uncovered = prior.is_some_and(|checkpoint| checkpoint.pending_is_uncovered);
    let mut committed_sequence = if continues_window {
        prior.map_or(0, materialized_sequence)
    } else {
        0
    };
    let mut committed_hash = if continues_window {
        prior.map_or_else(String::new, materialized_hash)
    } else {
        String::new()
    };
    let source_cursor = prior.and_then(|checkpoint| {
        (checkpoint.reader_byte_offset > 0).then(|| JournalCursor {
            byte_offset: checkpoint.reader_byte_offset,
            raw_sequence: checkpoint.reader_raw_sequence,
            prefix_hash: checkpoint.reader_prefix_hash.clone(),
            file_identity: checkpoint.reader_file_identity.clone(),
        })
    });
    let path = journal_path.as_ref();
    let read = match read_journal_incremental(
        path,
        source_cursor.as_ref(),
        MAX_INCREMENTAL_JOURNAL_EVENTS,
        verify_checkpoint_prefix && source_cursor.is_some(),
    ) {
        Ok(read) => read,
        Err(error) if source_cursor.is_some() && compaction_recovery_allowed(&error) => {
            let checkpoint = prior.expect("cursor requires projection checkpoint");
            let anchor_sequence = materialized_sequence(checkpoint);
            if anchor_sequence == 0 {
                read_journal_incremental(path, None, MAX_INCREMENTAL_JOURNAL_EVENTS, false)
                    .map_err(journal_error)?
            } else {
                let (anchor, anchor_cursor) =
                    find_journal_event_cursor(path, anchor_sequence).map_err(journal_error)?;
                if anchor.worker_id != expected_worker_id
                    || anchor.channel_id != channel_id
                    || frame_hash(expected_worker_id, &anchor)? != materialized_hash(checkpoint)
                {
                    return Err(AsRunJournalAdapterError::Journal(
                        "journal replacement does not retain the materialized transition anchor"
                            .into(),
                    ));
                }
                pending = scheduled_pending(&anchor, expected_worker_id)?;
                pending_is_uncovered = false;
                read_journal_incremental(
                    path,
                    Some(&anchor_cursor),
                    MAX_INCREMENTAL_JOURNAL_EVENTS,
                    false,
                )
                .map_err(journal_error)?
            }
        }
        Err(error) => return Err(journal_error(error)),
    };
    if let Some(worker_id) = &read.worker_id
        && (worker_id != expected_worker_id || read.channel_id.as_deref() != Some(channel_id))
    {
        return Err(AsRunJournalAdapterError::Journal(
            "journal channel identity does not match the configured channel".into(),
        ));
    }

    for envelope in &read.events {
        if envelope.worker_id != expected_worker_id || envelope.channel_id != channel_id {
            return Err(AsRunJournalAdapterError::Journal(
                "journal event identity does not match the configured channel".into(),
            ));
        }
        match &envelope.event {
            WorkerEvent::ScheduledAssetOnAir { .. } => {
                let next = scheduled_pending(envelope, expected_worker_id)?
                    .expect("scheduled event produces pending transition");
                if let Some(open) = pending.take() {
                    if next.started_at_unix_ms <= open.started_at_unix_ms
                        || next.started_at_unix_ms > now_utc_ms
                    {
                        return Err(AsRunJournalAdapterError::Journal(
                            "scheduled as-run journal timestamps are nonmonotonic or future".into(),
                        ));
                    }
                    let start = open.started_at_unix_ms.max(window.starts_at_utc_ms);
                    let end = next.started_at_unix_ms.min(window.ends_at_utc_ms);
                    if start < end {
                        let take = closed_take(&open, channel_id, start, end, pending_is_uncovered);
                        append_or_verify(&mut entries, &take)?;
                        committed_sequence = take.journal_sequence;
                        committed_hash = take.journal_hash.clone();
                    }
                }
                pending = Some(next);
                pending_is_uncovered = false;
            }
            event if is_untimed_transition(event) && pending.is_some() => {
                pending_is_uncovered = true;
            }
            _ => {}
        }
    }
    if read.cursor.raw_sequence == 0 {
        return Err(AsRunJournalAdapterError::Journal(
            "no durable journal records are available for projection yet".into(),
        ));
    }
    let checkpoint = ProjectionCheckpoint {
        channel_id: channel_id.into(),
        journal_id: expected_worker_id.into(),
        journal_sequence: read.cursor.raw_sequence,
        journal_hash: read.cursor.prefix_hash.clone(),
        materialized_sequence: committed_sequence,
        materialized_hash: committed_hash,
        reader_byte_offset: read.cursor.byte_offset,
        reader_raw_sequence: read.cursor.raw_sequence,
        reader_prefix_hash: read.cursor.prefix_hash,
        reader_file_identity: read.cursor.file_identity,
        pending_start: pending,
        pending_is_uncovered,
    };
    let projection = projection_from_entries(
        channel_id,
        station_timezone,
        expected_worker_id,
        entries,
        checkpoint,
        window,
    )?;
    store
        .persist_asrun_projection(&projection, now_utc_ms)
        .map_err(|error| AsRunJournalAdapterError::Storage(format!("{error:?}")))?;
    Ok(projection)
}

fn projection_from_entries(
    channel_id: &str,
    station_timezone: &str,
    journal_id: &str,
    entries: Vec<station_reporting::runtime::ProjectedEntry>,
    checkpoint: ProjectionCheckpoint,
    window: ProjectionWindow,
) -> Result<AsRunProjectionRuntime, AsRunJournalAdapterError> {
    if entries.is_empty() {
        return Ok(AsRunProjectionRuntime {
            channel_id: channel_id.into(),
            station_timezone: station_timezone.into(),
            entries,
            checkpoint,
            window: Some(window),
        });
    }
    let takes = entries
        .iter()
        .map(|entry| JournalTake {
            journal_id: journal_id.into(),
            journal_sequence: entry.journal_sequence,
            channel_id: channel_id.into(),
            started_at_unix_ms: entry.started_at_unix_ms,
            ended_at_unix_ms: entry.ended_at_unix_ms,
            state: entry.state,
            content_id: entry.content_id.clone(),
            franchise_id: entry.franchise_id.clone(),
            category: entry.category.clone(),
            title: entry.title.clone(),
            catalog_revision: entry.catalog_revision,
            schedule_revision: entry.schedule_revision,
            journal_hash: entry.journal_hash.clone(),
            unavailable_reason: entry.unavailable_reason.clone(),
        })
        .collect();
    let mut projection = ReportingRuntime::new()
        .project(
            ProjectionInput::new(channel_id, station_timezone, takes)
                .map_err(AsRunJournalAdapterError::Invalid)?,
        )
        .map_err(AsRunJournalAdapterError::Invalid)?;
    projection.checkpoint = checkpoint;
    projection.window = Some(window);
    Ok(projection)
}

fn append_or_verify(
    entries: &mut Vec<station_reporting::runtime::ProjectedEntry>,
    take: &JournalTake,
) -> Result<(), AsRunJournalAdapterError> {
    let candidate = ReportingRuntime::new()
        .project(
            ProjectionInput::new(&take.channel_id, "UTC", vec![take.clone()])
                .map_err(AsRunJournalAdapterError::Invalid)?,
        )
        .map_err(AsRunJournalAdapterError::Invalid)?
        .entries
        .into_iter()
        .next()
        .expect("one take produces one row");
    if let Some(existing) = entries.last() {
        if existing.journal_sequence == candidate.journal_sequence {
            if existing != &candidate {
                return Err(AsRunJournalAdapterError::Journal(
                    "journal replay conflicts with immutable as-run projection evidence".into(),
                ));
            }
            return Ok(());
        }
        if candidate.journal_sequence <= existing.journal_sequence
            || candidate.started_at_unix_ms != existing.ended_at_unix_ms
        {
            return Err(AsRunJournalAdapterError::Journal(
                "journal projection is noncontiguous; an uncovered interval cannot be fabricated"
                    .into(),
            ));
        }
    }
    entries.push(candidate);
    Ok(())
}

fn closed_take(
    open: &PendingScheduledTake,
    channel_id: &str,
    started_at_unix_ms: i64,
    ended_at_unix_ms: i64,
    uncovered: bool,
) -> JournalTake {
    if uncovered {
        return JournalTake {
            journal_id: format!("station-worker-{channel_id}"),
            journal_sequence: open.journal_sequence,
            channel_id: channel_id.into(),
            started_at_unix_ms,
            ended_at_unix_ms,
            state: AsRunState::Gap,
            content_id: None,
            franchise_id: None,
            category: None,
            title: None,
            catalog_revision: None,
            schedule_revision: None,
            journal_hash: open.journal_hash.clone(),
            unavailable_reason: Some(
                "Untimed live, fallback, CG, or caption transition leaves this interval uncovered; no program airtime was fabricated.".into(),
            ),
        };
    }
    JournalTake {
        journal_id: format!("station-worker-{channel_id}"),
        journal_sequence: open.journal_sequence,
        channel_id: channel_id.into(),
        started_at_unix_ms,
        ended_at_unix_ms,
        state: AsRunState::Program,
        content_id: Some(open.asset_id.clone()),
        franchise_id: None,
        category: None,
        title: None,
        catalog_revision: None,
        schedule_revision: None,
        journal_hash: open.journal_hash.clone(),
        unavailable_reason: Some(format!(
            "Exact immutable schedule/catalog snapshot authority is unavailable for journal item {} revision {}; no mutable current-row substitution was used.",
            open.schedule_item_id, open.schedule_item_revision
        )),
    }
}

fn scheduled_pending(
    envelope: &WorkerEventEnvelope,
    journal_id: &str,
) -> Result<Option<PendingScheduledTake>, AsRunJournalAdapterError> {
    let WorkerEvent::ScheduledAssetOnAir {
        schedule_item_id,
        schedule_item_revision,
        asset_id,
        actual_on_air_at_unix_ms,
        ..
    } = &envelope.event
    else {
        return Ok(None);
    };
    if *actual_on_air_at_unix_ms < 0 || *schedule_item_revision == 0 {
        return Err(AsRunJournalAdapterError::Journal(
            "scheduled as-run journal transition is invalid".into(),
        ));
    }
    Ok(Some(PendingScheduledTake {
        journal_sequence: envelope.sequence,
        schedule_item_id: schedule_item_id.clone(),
        schedule_item_revision: *schedule_item_revision,
        asset_id: asset_id.clone(),
        started_at_unix_ms: *actual_on_air_at_unix_ms,
        journal_hash: frame_hash(journal_id, envelope)?,
    }))
}

fn is_untimed_transition(event: &WorkerEvent) -> bool {
    matches!(
        event,
        WorkerEvent::OnAirChanged { .. }
            | WorkerEvent::LiveOnAir { .. }
            | WorkerEvent::LiveReturned { .. }
            | WorkerEvent::CgApplied { .. }
            | WorkerEvent::CgCleared
            | WorkerEvent::CaptionCueAccepted { .. }
            | WorkerEvent::CaptionCuesCleared
    )
}

fn materialized_sequence(checkpoint: &ProjectionCheckpoint) -> u64 {
    if checkpoint.materialized_sequence == 0 {
        checkpoint.journal_sequence
    } else {
        checkpoint.materialized_sequence
    }
}

fn materialized_hash(checkpoint: &ProjectionCheckpoint) -> String {
    if checkpoint.materialized_hash.is_empty() {
        checkpoint.journal_hash.clone()
    } else {
        checkpoint.materialized_hash.clone()
    }
}

fn compaction_recovery_allowed(error: &JournalError) -> bool {
    matches!(
        error,
        JournalError::Corrupt { reason, .. }
            if reason.contains("truncated") || reason.contains("file identity changed")
    )
}

fn journal_error(error: JournalError) -> AsRunJournalAdapterError {
    AsRunJournalAdapterError::Journal(format!("{error:?}"))
}

fn frame_hash(
    journal_id: &str,
    envelope: &WorkerEventEnvelope,
) -> Result<String, AsRunJournalAdapterError> {
    let frame = encode_event_frame(envelope).map_err(|error| {
        AsRunJournalAdapterError::Journal(format!("canonical event frame unavailable: {error:?}"))
    })?;
    let mut hash = Sha256::new();
    hash.update(b"townlight-asrun-journal-frame-v1\0");
    hash.update(journal_id.as_bytes());
    hash.update([0]);
    hash.update(frame);
    Ok(format!("{:x}", hash.finalize()))
}

fn uuid(value: &str) -> bool {
    value.len() == 36
        && value.as_bytes().iter().enumerate().all(|(index, byte)| {
            if matches!(index, 8 | 13 | 18 | 23) {
                *byte == b'-'
            } else if index == 14 {
                *byte == b'4'
            } else if index == 19 {
                matches!(*byte, b'8' | b'9' | b'a' | b'b')
            } else {
                byte.is_ascii_digit() || matches!(*byte, b'a'..=b'f')
            }
        })
}
