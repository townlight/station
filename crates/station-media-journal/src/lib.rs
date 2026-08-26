use std::ffi::OsString;
use std::fs::{File, OpenOptions};
use std::io::{ErrorKind, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

#[cfg(windows)]
use std::os::windows::fs::OpenOptionsExt;

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use station_media_protocol::{
    MAX_FRAME_BYTES, OutputTransportObservation, ProtocolError, WorkerEvent, WorkerEventEnvelope,
    decode_event_frame, decode_persisted_event_frame, encode_event_frame,
};

const MAGIC: [u8; 4] = *b"TLJR";
const LEGACY_JOURNAL_VERSION: u16 = 1;
const JOURNAL_VERSION: u16 = 2;
const HEADER_BYTES: usize = 16;
const MAX_EVENT_FRAME_BYTES: usize = MAX_FRAME_BYTES + 4;
const RECORD_KIND_EVENT: u16 = 0;
const RECORD_KIND_HEARTBEAT_SUMMARY: u16 = 1;
const RECORD_KIND_COMPACTION_COMMIT: u16 = 2;
const COMPACT_AFTER_RAW_HEARTBEATS: usize = 512;
const RETAIN_RAW_HEARTBEATS: usize = 256;
const MAX_SUMMARY_OUTPUTS: usize = 64;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CompactedHeartbeatRange {
    pub first_sequence: u64,
    pub last_sequence: u64,
    pub sample_count: u64,
    pub first_monotonic_milliseconds: u64,
    pub last_monotonic_milliseconds: u64,
    pub first_outputs: Vec<OutputTransportObservation>,
    pub last_outputs: Vec<OutputTransportObservation>,
    pub digest_chain: [u8; 32],
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct HeartbeatSummary {
    worker_id: String,
    channel_id: String,
    ranges: Vec<CompactedHeartbeatRange>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JournalInspection {
    /// Identity established by either retained events or compacted heartbeat
    /// summaries. Both values are absent only for a truly empty journal.
    pub worker_id: Option<String>,
    pub channel_id: Option<String>,
    pub retained_events: Vec<WorkerEventEnvelope>,
    pub compacted_heartbeat_ranges: Vec<CompactedHeartbeatRange>,
    pub next_sequence: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct CompactionCommit {
    worker_id: String,
    channel_id: String,
    next_sequence: u64,
}

struct JournalSnapshot {
    events: Vec<WorkerEventEnvelope>,
    heartbeat_summary: Option<HeartbeatSummary>,
    identity: Option<(String, String)>,
    next_sequence: u64,
    raw_heartbeat_count: usize,
    compaction_committed: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum JournalError {
    Io(String),
    /// Another process still owns the writer lock for this journal.  A
    /// replacement worker must not start until the previous worker's process
    /// handle has been retired and this lock is released by Windows.
    WriterAlreadyActive,
    /// This product only permits a journal writer on Windows, where the lock
    /// is an OS-owned handle and therefore releases automatically if the
    /// worker crashes.  Refusing a non-Windows writer is safer than silently
    /// accepting concurrent appenders on a platform without that contract.
    WriterOwnershipUnavailable,
    Corrupt {
        offset: u64,
        reason: String,
    },
    WrongIdentity,
    NonMonotonic {
        expected: u64,
        actual: u64,
    },
    Protocol(ProtocolError),
    /// A caller-owned bounded reader reached its event cap before allocating an
    /// unbounded replay vector.
    EventLimitExceeded {
        limit: usize,
    },
}

pub struct JournalWriter {
    file: Option<File>,
    ownership: Option<JournalWriterOwnership>,
    path: PathBuf,
    worker_id: String,
    channel_id: String,
    next_sequence: u64,
    raw_heartbeat_count: usize,
}

impl JournalWriter {
    pub fn open(
        path: impl AsRef<Path>,
        worker_id: &str,
        channel_id: &str,
    ) -> Result<Self, JournalError> {
        let path = path.as_ref().to_path_buf();
        // Take exclusive, process-owned ownership before recovery or
        // inspection.  Otherwise an old worker and its replacement could both
        // observe the same next sequence and append independently.
        let ownership = acquire_writer_ownership(&path)?;
        recover_compaction_temp(&path, worker_id, channel_id)?;
        let snapshot = read_snapshot(&path)?;
        if snapshot
            .identity
            .as_ref()
            .is_some_and(|identity| identity.0 != worker_id || identity.1 != channel_id)
        {
            return Err(JournalError::WrongIdentity);
        }
        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)
            .map_err(io_error)?;
        Ok(Self {
            file: Some(file),
            ownership: Some(ownership),
            path,
            worker_id: worker_id.to_string(),
            channel_id: channel_id.to_string(),
            next_sequence: snapshot.next_sequence,
            raw_heartbeat_count: snapshot.raw_heartbeat_count,
        })
    }

    pub fn next_sequence(&self) -> u64 {
        self.next_sequence
    }

    pub fn append(&mut self, event: &WorkerEventEnvelope) -> Result<(), JournalError> {
        if event.worker_id != self.worker_id || event.channel_id != self.channel_id {
            return Err(JournalError::WrongIdentity);
        }
        if event.sequence != self.next_sequence {
            return Err(JournalError::NonMonotonic {
                expected: self.next_sequence,
                actual: event.sequence,
            });
        }

        let payload = encode_event_frame(event).map_err(JournalError::Protocol)?;
        decode_event_frame(&payload).map_err(JournalError::Protocol)?;
        let file = self.file.as_mut().ok_or_else(|| {
            JournalError::Io("journal writer is unavailable after replacement failure".into())
        })?;
        write_record(file, RECORD_KIND_EVENT, &payload)?;
        file.flush().map_err(io_error)?;
        file.sync_data().map_err(io_error)?;
        self.next_sequence = self
            .next_sequence
            .checked_add(1)
            .ok_or_else(|| corrupt(0, "journal sequence exhausted"))?;
        if matches!(event.event, WorkerEvent::Heartbeat { .. }) {
            self.raw_heartbeat_count += 1;
            if self.raw_heartbeat_count > COMPACT_AFTER_RAW_HEARTBEATS {
                self.compact_heartbeats()?;
            }
        }
        Ok(())
    }

    fn compact_heartbeats(&mut self) -> Result<(), JournalError> {
        self.compact_heartbeats_with(replace_file)
    }

    fn compact_heartbeats_with(
        &mut self,
        replacer: impl FnOnce(&Path, &Path) -> std::io::Result<()>,
    ) -> Result<(), JournalError> {
        let snapshot = read_snapshot(&self.path)?;
        let remove_count = snapshot
            .raw_heartbeat_count
            .saturating_sub(RETAIN_RAW_HEARTBEATS);
        if remove_count == 0 {
            return Ok(());
        }
        let mut summary = snapshot
            .heartbeat_summary
            .unwrap_or_else(|| HeartbeatSummary {
                worker_id: self.worker_id.clone(),
                channel_id: self.channel_id.clone(),
                ranges: Vec::new(),
            });
        let mut removed = 0_usize;
        let mut retained = Vec::with_capacity(snapshot.events.len() - remove_count);
        for event in snapshot.events {
            if removed < remove_count
                && let WorkerEvent::Heartbeat {
                    monotonic_milliseconds,
                    ref outputs,
                } = event.event
            {
                add_compacted_heartbeat(&mut summary, &event, monotonic_milliseconds, outputs)?;
                removed += 1;
            } else {
                retained.push(event);
            }
        }
        if removed != remove_count {
            return Err(corrupt(
                0,
                "journal heartbeat count changed during compaction",
            ));
        }

        let temporary = compaction_temp_path(&self.path);
        if temporary.try_exists().map_err(io_error)? {
            std::fs::remove_file(&temporary).map_err(io_error)?;
        }
        let mut output = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&temporary)
            .map_err(io_error)?;
        for summary_payload in encode_summary_chunks(&summary)? {
            write_record(&mut output, RECORD_KIND_HEARTBEAT_SUMMARY, &summary_payload)?;
        }
        for event in &retained {
            let payload = encode_event_frame(event).map_err(JournalError::Protocol)?;
            write_record(&mut output, RECORD_KIND_EVENT, &payload)?;
        }
        let commit_payload = serde_json::to_vec(&CompactionCommit {
            worker_id: self.worker_id.clone(),
            channel_id: self.channel_id.clone(),
            next_sequence: snapshot.next_sequence,
        })
        .map_err(|error| corrupt(0, format!("could not encode compaction commit: {error}")))?;
        write_record(&mut output, RECORD_KIND_COMPACTION_COMMIT, &commit_payload)?;
        output.flush().map_err(io_error)?;
        output.sync_all().map_err(io_error)?;
        drop(output);

        let compacted = read_snapshot(&temporary)?;
        if compacted.next_sequence != snapshot.next_sequence
            || compacted.identity != snapshot.identity
            || compacted.raw_heartbeat_count > RETAIN_RAW_HEARTBEATS
            || !compacted.compaction_committed
        {
            return Err(corrupt(
                0,
                "compacted journal did not preserve its contract",
            ));
        }

        drop(self.file.take());
        if let Err(error) = replacer(&temporary, &self.path) {
            return Err(io_error(error));
        }
        self.file = Some(
            OpenOptions::new()
                .append(true)
                .open(&self.path)
                .map_err(io_error)?,
        );
        self.raw_heartbeat_count = compacted.raw_heartbeat_count;
        Ok(())
    }
}

impl Drop for JournalWriter {
    fn drop(&mut self) {
        // Close the mutable journal first.  The ownership handle is separate
        // so compaction can replace the journal file without releasing
        // writer authority; it is released only when this writer retires.
        self.file.take();
        self.ownership.take();
    }
}

/// A separate sidecar is intentionally used instead of locking the journal
/// file itself.  Heartbeat compaction atomically replaces the journal path;
/// releasing a lock on that path during replacement would create a window for
/// a second worker to append to the new file.
struct JournalWriterOwnership {
    lock_handle: Option<File>,
    lock_path: PathBuf,
}

impl Drop for JournalWriterOwnership {
    fn drop(&mut self) {
        // On Windows a new owner cannot open the sidecar until this exclusive
        // handle is closed. If a replacement wins the tiny close/remove race,
        // its zero-share handle makes this remove fail harmlessly.
        self.lock_handle.take();
        let _ = std::fs::remove_file(&self.lock_path);
    }
}

#[cfg(windows)]
fn writer_lock_path(path: &Path) -> PathBuf {
    let mut lock: OsString = path.as_os_str().to_owned();
    lock.push(".writer.lock");
    lock.into()
}

#[cfg(windows)]
fn acquire_writer_ownership(path: &Path) -> Result<JournalWriterOwnership, JournalError> {
    let lock_path = writer_lock_path(path);
    let mut options = OpenOptions::new();
    options.create(true).read(true).write(true);
    // A zero share mode asks Windows for an exclusive file handle.  The OS
    // releases it on process death, unlike an advisory in-file marker.
    options.share_mode(0);
    let lock_handle = options.open(&lock_path).map_err(|error| {
        // ERROR_SHARING_VIOLATION (32) normally maps to PermissionDenied, but
        // retain the raw check for Windows runtimes that preserve it as a more
        // generic I/O kind.
        if error.kind() == ErrorKind::PermissionDenied || error.raw_os_error() == Some(32) {
            JournalError::WriterAlreadyActive
        } else {
            io_error(error)
        }
    })?;
    Ok(JournalWriterOwnership {
        lock_handle: Some(lock_handle),
        lock_path,
    })
}

#[cfg(not(windows))]
fn acquire_writer_ownership(_path: &Path) -> Result<JournalWriterOwnership, JournalError> {
    // Do not fall back to an advisory or best-effort lock: that would let two
    // workers concurrently append durable command evidence.  TownLight
    // Station's worker product is Windows-only, and non-Windows builds remain
    // compile-safe while failing closed at writer startup.
    Err(JournalError::WriterOwnershipUnavailable)
}

pub fn read_events(path: impl AsRef<Path>) -> Result<Vec<WorkerEventEnvelope>, JournalError> {
    Ok(read_snapshot(path.as_ref())?.events)
}

/// Reads a CRC-validated journal while refusing to allocate more than `limit`
/// retained event envelopes. Frame allocation remains bounded by the protocol's
/// fixed maximum, so callers do not need a racy file-size precheck.
pub fn read_events_bounded(
    path: impl AsRef<Path>,
    limit: usize,
) -> Result<Vec<WorkerEventEnvelope>, JournalError> {
    Ok(read_snapshot_bounded(path.as_ref(), limit)?.events)
}

pub fn inspect_journal(path: impl AsRef<Path>) -> Result<JournalInspection, JournalError> {
    let snapshot = read_snapshot(path.as_ref())?;
    inspection_from_snapshot(snapshot)
}

pub fn inspect_journal_bounded(
    path: impl AsRef<Path>,
    limit: usize,
) -> Result<JournalInspection, JournalError> {
    inspection_from_snapshot(read_snapshot_bounded(path.as_ref(), limit)?)
}

fn inspection_from_snapshot(snapshot: JournalSnapshot) -> Result<JournalInspection, JournalError> {
    let (worker_id, channel_id) = snapshot
        .identity
        .map_or((None, None), |(worker_id, channel_id)| {
            (Some(worker_id), Some(channel_id))
        });
    Ok(JournalInspection {
        worker_id,
        channel_id,
        retained_events: snapshot.events,
        compacted_heartbeat_ranges: snapshot
            .heartbeat_summary
            .map_or_else(Vec::new, |summary| summary.ranges),
        next_sequence: snapshot.next_sequence,
    })
}

fn read_snapshot(path: &Path) -> Result<JournalSnapshot, JournalError> {
    read_snapshot_bounded(path, usize::MAX)
}

fn read_snapshot_bounded(path: &Path, event_limit: usize) -> Result<JournalSnapshot, JournalError> {
    let mut file = match File::open(path) {
        Ok(file) => file,
        Err(error) if error.kind() == ErrorKind::NotFound => {
            return Ok(JournalSnapshot {
                events: Vec::new(),
                heartbeat_summary: None,
                identity: None,
                next_sequence: 1,
                raw_heartbeat_count: 0,
                compaction_committed: false,
            });
        }
        Err(error) => return Err(io_error(error)),
    };
    file.seek(SeekFrom::Start(0)).map_err(io_error)?;
    let mut events = Vec::new();
    let mut offset = 0_u64;
    let mut identity: Option<(String, String)> = None;
    let mut heartbeat_summary: Option<HeartbeatSummary> = None;
    let mut compaction_commit: Option<(CompactionCommit, usize)> = None;
    let mut saw_event = false;

    loop {
        let mut header = [0_u8; HEADER_BYTES];
        match file.read(&mut header[..1]) {
            Ok(0) => break,
            Ok(1) => {}
            Ok(_) => unreachable!("the read buffer has one byte"),
            Err(error) => return Err(io_error(error)),
        }
        read_exact_record(
            &mut file,
            &mut header[1..],
            offset,
            "truncated record header",
        )?;
        if header[..4] != MAGIC {
            return Err(corrupt(offset, "invalid journal magic"));
        }
        let version = u16::from_le_bytes(header[4..6].try_into().expect("two header bytes"));
        if !matches!(version, LEGACY_JOURNAL_VERSION | JOURNAL_VERSION) {
            return Err(corrupt(
                offset,
                format!("unsupported journal version {version}"),
            ));
        }
        let record_kind = u16::from_le_bytes(header[6..8].try_into().expect("two header bytes"));
        if version == LEGACY_JOURNAL_VERSION && record_kind != RECORD_KIND_EVENT {
            return Err(corrupt(offset, "reserved header bits are not zero"));
        }
        let payload_length =
            u32::from_le_bytes(header[8..12].try_into().expect("four header bytes")) as usize;
        if payload_length > MAX_EVENT_FRAME_BYTES {
            return Err(corrupt(
                offset,
                format!("event frame is too large: {payload_length}"),
            ));
        }
        let expected_checksum =
            u32::from_le_bytes(header[12..16].try_into().expect("four header bytes"));
        let mut payload = vec![0_u8; payload_length];
        read_exact_record(&mut file, &mut payload, offset, "truncated event frame")?;
        if crc32(&payload) != expected_checksum {
            return Err(corrupt(offset, "event frame checksum mismatch"));
        }
        match record_kind {
            RECORD_KIND_EVENT => {
                let event =
                    decode_persisted_event_frame(&payload).map_err(JournalError::Protocol)?;
                let event_identity = (event.worker_id.clone(), event.channel_id.clone());
                match &identity {
                    Some(expected) if expected != &event_identity => {
                        return Err(corrupt(offset, "worker or channel identity changed"));
                    }
                    None => identity = Some(event_identity),
                    _ => {}
                }
                if events.last().is_some_and(|previous: &WorkerEventEnvelope| {
                    event.sequence <= previous.sequence
                }) {
                    return Err(JournalError::NonMonotonic {
                        expected: events
                            .last()
                            .and_then(|previous| previous.sequence.checked_add(1))
                            .unwrap_or(u64::MAX),
                        actual: event.sequence,
                    });
                }
                saw_event = true;
                if events.len() >= event_limit {
                    return Err(JournalError::EventLimitExceeded { limit: event_limit });
                }
                events.push(event);
            }
            RECORD_KIND_HEARTBEAT_SUMMARY if version == JOURNAL_VERSION => {
                if saw_event || compaction_commit.is_some() {
                    return Err(corrupt(
                        offset,
                        "heartbeat summary records must be contiguous and precede events",
                    ));
                }
                let summary: HeartbeatSummary =
                    serde_json::from_slice(&payload).map_err(|error| {
                        corrupt(offset, format!("invalid heartbeat summary: {error}"))
                    })?;
                validate_summary(&summary, offset)?;
                let summary_identity = (summary.worker_id.clone(), summary.channel_id.clone());
                match &identity {
                    Some(expected) if expected != &summary_identity => {
                        return Err(corrupt(offset, "worker or channel identity changed"));
                    }
                    None => identity = Some(summary_identity),
                    _ => {}
                }
                if let Some(existing) = heartbeat_summary.as_mut() {
                    existing.ranges.extend(summary.ranges);
                    validate_summary(existing, offset)?;
                } else {
                    heartbeat_summary = Some(summary);
                }
            }
            RECORD_KIND_COMPACTION_COMMIT if version == JOURNAL_VERSION => {
                if heartbeat_summary.is_none() || compaction_commit.is_some() {
                    return Err(corrupt(
                        offset,
                        "compaction commit requires one preceding heartbeat summary",
                    ));
                }
                let commit: CompactionCommit =
                    serde_json::from_slice(&payload).map_err(|error| {
                        corrupt(offset, format!("invalid compaction commit: {error}"))
                    })?;
                let commit_identity = (commit.worker_id.clone(), commit.channel_id.clone());
                if identity.as_ref() != Some(&commit_identity) {
                    return Err(corrupt(offset, "compaction commit identity changed"));
                }
                compaction_commit = Some((commit, events.len()));
            }
            _ => {
                return Err(corrupt(
                    offset,
                    format!("unsupported journal record kind {record_kind}"),
                ));
            }
        }
        offset +=
            u64::try_from(HEADER_BYTES + payload_length).expect("record length always fits in u64");
    }

    if heartbeat_summary.is_some() && compaction_commit.is_none() {
        return Err(corrupt(0, "heartbeat summary has no compaction commit"));
    }
    if let Some((commit, event_count)) = &compaction_commit {
        let committed_next =
            sequence_after_coverage(heartbeat_summary.as_ref(), &events[..*event_count], offset)?;
        if commit.next_sequence != committed_next {
            return Err(corrupt(
                offset,
                "compaction commit does not match committed sequence coverage",
            ));
        }
    }
    let next_sequence = sequence_after_coverage(heartbeat_summary.as_ref(), &events, offset)?;
    let raw_heartbeat_count = events
        .iter()
        .filter(|event| matches!(event.event, WorkerEvent::Heartbeat { .. }))
        .count();
    Ok(JournalSnapshot {
        events,
        heartbeat_summary,
        identity,
        next_sequence,
        raw_heartbeat_count,
        compaction_committed: compaction_commit.is_some(),
    })
}

fn sequence_after_coverage(
    heartbeat_summary: Option<&HeartbeatSummary>,
    events: &[WorkerEventEnvelope],
    offset: u64,
) -> Result<u64, JournalError> {
    let mut coverage = Vec::with_capacity(
        events.len() + heartbeat_summary.map_or(0, |summary| summary.ranges.len()),
    );
    if let Some(summary) = heartbeat_summary {
        coverage.extend(
            summary
                .ranges
                .iter()
                .map(|range| (range.first_sequence, range.last_sequence)),
        );
    }
    coverage.extend(events.iter().map(|event| (event.sequence, event.sequence)));
    coverage.sort_unstable_by_key(|range| range.0);
    let mut next_sequence = 1_u64;
    for (first, last) in coverage {
        if first != next_sequence {
            return Err(JournalError::NonMonotonic {
                expected: next_sequence,
                actual: first,
            });
        }
        next_sequence = last
            .checked_add(1)
            .ok_or_else(|| corrupt(offset, "journal sequence exhausted"))?;
    }
    Ok(next_sequence)
}

fn validate_summary(summary: &HeartbeatSummary, offset: u64) -> Result<(), JournalError> {
    if summary.worker_id.is_empty() || summary.channel_id.is_empty() || summary.ranges.is_empty() {
        return Err(corrupt(
            offset,
            "heartbeat summary identity or ranges are empty",
        ));
    }
    let mut previous_last = None;
    for range in &summary.ranges {
        let expected_count = range
            .last_sequence
            .checked_sub(range.first_sequence)
            .and_then(|difference| difference.checked_add(1))
            .ok_or_else(|| corrupt(offset, "heartbeat summary range is invalid"))?;
        if range.sample_count != expected_count
            || range.last_monotonic_milliseconds < range.first_monotonic_milliseconds
            || previous_last.is_some_and(|last| range.first_sequence <= last)
        {
            return Err(corrupt(offset, "heartbeat summary range is inconsistent"));
        }
        if range.first_outputs.len() > MAX_SUMMARY_OUTPUTS
            || range.first_outputs.len() != range.last_outputs.len()
            || range
                .first_outputs
                .iter()
                .zip(&range.last_outputs)
                .any(|(first, last)| {
                    first.output_index != last.output_index
                        || last.bytes_sent_total < first.bytes_sent_total
                })
            || range
                .first_outputs
                .windows(2)
                .any(|outputs| outputs[0].output_index >= outputs[1].output_index)
        {
            return Err(corrupt(
                offset,
                "heartbeat summary output endpoints are inconsistent",
            ));
        }
        previous_last = Some(range.last_sequence);
    }
    Ok(())
}

fn encode_summary_chunks(summary: &HeartbeatSummary) -> Result<Vec<Vec<u8>>, JournalError> {
    let mut encoded = Vec::new();
    let mut ranges = Vec::new();
    for range in &summary.ranges {
        ranges.push(range.clone());
        let candidate = HeartbeatSummary {
            worker_id: summary.worker_id.clone(),
            channel_id: summary.channel_id.clone(),
            ranges: ranges.clone(),
        };
        let payload = serde_json::to_vec(&candidate)
            .map_err(|error| corrupt(0, format!("could not encode heartbeat summary: {error}")))?;
        if payload.len() <= MAX_EVENT_FRAME_BYTES {
            continue;
        }
        ranges.pop();
        if ranges.is_empty() {
            return Err(corrupt(
                0,
                "one heartbeat summary range exceeds the record bound",
            ));
        }
        let chunk = HeartbeatSummary {
            worker_id: summary.worker_id.clone(),
            channel_id: summary.channel_id.clone(),
            ranges: std::mem::take(&mut ranges),
        };
        encoded.push(
            serde_json::to_vec(&chunk).map_err(|error| {
                corrupt(0, format!("could not encode heartbeat summary: {error}"))
            })?,
        );
        ranges.push(range.clone());
    }
    if !ranges.is_empty() {
        let chunk = HeartbeatSummary {
            worker_id: summary.worker_id.clone(),
            channel_id: summary.channel_id.clone(),
            ranges,
        };
        let payload = serde_json::to_vec(&chunk)
            .map_err(|error| corrupt(0, format!("could not encode heartbeat summary: {error}")))?;
        if payload.len() > MAX_EVENT_FRAME_BYTES {
            return Err(corrupt(
                0,
                "one heartbeat summary range exceeds the record bound",
            ));
        }
        encoded.push(payload);
    }
    Ok(encoded)
}

fn write_record(
    output: &mut impl Write,
    record_kind: u16,
    payload: &[u8],
) -> Result<(), JournalError> {
    if payload.len() > MAX_EVENT_FRAME_BYTES {
        return Err(corrupt(
            0,
            format!("journal record is too large: {}", payload.len()),
        ));
    }
    let payload_length =
        u32::try_from(payload.len()).expect("a bounded journal record always fits in a u32");
    let mut record = Vec::with_capacity(HEADER_BYTES + payload.len());
    record.extend_from_slice(&MAGIC);
    record.extend_from_slice(&JOURNAL_VERSION.to_le_bytes());
    record.extend_from_slice(&record_kind.to_le_bytes());
    record.extend_from_slice(&payload_length.to_le_bytes());
    record.extend_from_slice(&crc32(payload).to_le_bytes());
    record.extend_from_slice(payload);
    output.write_all(&record).map_err(io_error)
}

fn add_compacted_heartbeat(
    summary: &mut HeartbeatSummary,
    event: &WorkerEventEnvelope,
    monotonic_milliseconds: u64,
    outputs: &[OutputTransportObservation],
) -> Result<(), JournalError> {
    let payload = encode_event_frame(event).map_err(JournalError::Protocol)?;
    if let Some(range) = summary.ranges.last_mut()
        && range.last_sequence.checked_add(1) == Some(event.sequence)
    {
        range.last_sequence = event.sequence;
        range.sample_count = range
            .sample_count
            .checked_add(1)
            .ok_or_else(|| corrupt(0, "heartbeat summary sample count exhausted"))?;
        range.last_monotonic_milliseconds = monotonic_milliseconds;
        range.last_outputs = outputs.to_vec();
        range.digest_chain = heartbeat_digest(Some(range.digest_chain), &payload);
        return Ok(());
    }
    summary.ranges.push(CompactedHeartbeatRange {
        first_sequence: event.sequence,
        last_sequence: event.sequence,
        sample_count: 1,
        first_monotonic_milliseconds: monotonic_milliseconds,
        last_monotonic_milliseconds: monotonic_milliseconds,
        first_outputs: outputs.to_vec(),
        last_outputs: outputs.to_vec(),
        digest_chain: heartbeat_digest(None, &payload),
    });
    Ok(())
}

fn heartbeat_digest(previous: Option<[u8; 32]>, payload: &[u8]) -> [u8; 32] {
    let mut digest = Sha256::new();
    if let Some(previous) = previous {
        digest.update(previous);
    }
    digest.update(payload);
    digest.finalize().into()
}

fn compaction_temp_path(path: &Path) -> PathBuf {
    let mut temporary: OsString = path.as_os_str().to_owned();
    temporary.push(".compact.tmp");
    temporary.into()
}

fn recover_compaction_temp(
    path: &Path,
    worker_id: &str,
    channel_id: &str,
) -> Result<(), JournalError> {
    let temporary = compaction_temp_path(path);
    if !temporary.try_exists().map_err(io_error)? {
        return Ok(());
    }
    if path.try_exists().map_err(io_error)? {
        read_snapshot(path)?;
        std::fs::remove_file(temporary).map_err(io_error)?;
        return Ok(());
    }
    let temporary_snapshot = read_snapshot(&temporary)?;
    if !temporary_snapshot.compaction_committed {
        return Err(corrupt(0, "temporary journal has no committed compaction"));
    }
    if temporary_snapshot
        .identity
        .as_ref()
        .is_some_and(|identity| identity.0 != worker_id || identity.1 != channel_id)
    {
        return Err(JournalError::WrongIdentity);
    }
    replace_file(&temporary, path).map_err(io_error)
}

#[cfg(windows)]
fn replace_file(source: &Path, destination: &Path) -> std::io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{
        MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH, MoveFileExW,
    };

    let source = source
        .as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect::<Vec<_>>();
    let destination = destination
        .as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect::<Vec<_>>();
    // SAFETY: both vectors contain valid NUL-terminated UTF-16 strings for the call.
    let status = unsafe {
        MoveFileExW(
            source.as_ptr(),
            destination.as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    };
    if status == 0 {
        Err(std::io::Error::last_os_error())
    } else {
        Ok(())
    }
}

#[cfg(not(windows))]
fn replace_file(source: &Path, destination: &Path) -> std::io::Result<()> {
    if destination.try_exists()? {
        std::fs::remove_file(destination)?;
    }
    std::fs::rename(source, destination)
}

fn read_exact_record(
    file: &mut File,
    bytes: &mut [u8],
    offset: u64,
    truncated_reason: &str,
) -> Result<(), JournalError> {
    match file.read_exact(bytes) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == ErrorKind::UnexpectedEof => {
            Err(corrupt(offset, truncated_reason))
        }
        Err(error) => Err(io_error(error)),
    }
}

fn crc32(bytes: &[u8]) -> u32 {
    let mut checksum = u32::MAX;
    for byte in bytes {
        checksum ^= u32::from(*byte);
        for _ in 0..8 {
            let mask = 0_u32.wrapping_sub(checksum & 1);
            checksum = (checksum >> 1) ^ (0xedb8_8320 & mask);
        }
    }
    !checksum
}

fn corrupt(offset: u64, reason: impl Into<String>) -> JournalError {
    JournalError::Corrupt {
        offset,
        reason: reason.into(),
    }
}

fn io_error(error: std::io::Error) -> JournalError {
    JournalError::Io(error.to_string())
}

#[cfg(test)]
mod tests {
    use std::time::{SystemTime, UNIX_EPOCH};

    use station_media_protocol::PROTOCOL_VERSION;

    use super::*;

    const WORKER_ID: &str = "d4af49c4-a230-44c5-8f55-c9a0224846b4";
    const CHANNEL_ID: &str = "1500c96b-26c2-4534-a97f-2a8c2ed7fa5e";

    fn test_path() -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock is before Unix epoch")
            .as_nanos();
        std::env::temp_dir().join(format!("townlight-replace-failure-{nonce}.tlj"))
    }

    fn event(sequence: u64, event: WorkerEvent) -> WorkerEventEnvelope {
        WorkerEventEnvelope {
            version: PROTOCOL_VERSION,
            worker_id: WORKER_ID.into(),
            channel_id: CHANNEL_ID.into(),
            sequence,
            event,
        }
    }

    #[test]
    fn replacement_failure_poisoned_the_writer_without_creating_a_new_journal() {
        let path = test_path();
        let temporary = compaction_temp_path(&path);
        let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        writer
            .append(&event(1, WorkerEvent::Ready { graph_revision: 1 }))
            .unwrap();
        for sequence in 2..=513 {
            writer
                .append(&event(
                    sequence,
                    WorkerEvent::Heartbeat {
                        monotonic_milliseconds: sequence * 5_000,
                        outputs: Vec::new(),
                    },
                ))
                .unwrap();
        }

        assert!(matches!(
            writer.compact_heartbeats_with(|_, _| Err(std::io::Error::other("injected"))),
            Err(JournalError::Io(reason)) if reason.contains("injected")
        ));
        assert!(writer.file.is_none());
        assert!(path.is_file());
        assert!(temporary.is_file());
        assert!(matches!(
            writer.append(&event(514, WorkerEvent::ShutdownComplete)),
            Err(JournalError::Io(reason)) if reason.contains("unavailable")
        ));
        drop(writer);

        let reopened = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        assert_eq!(reopened.next_sequence(), 514);
        assert!(!temporary.exists());
        drop(reopened);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn scheduled_asset_on_air_survives_heartbeat_compaction_in_replay_order() {
        let path = test_path();
        let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        writer
            .append(&event(1, WorkerEvent::Ready { graph_revision: 1 }))
            .unwrap();
        for sequence in 2..=513 {
            writer
                .append(&event(
                    sequence,
                    WorkerEvent::Heartbeat {
                        monotonic_milliseconds: sequence * 5_000,
                        outputs: Vec::new(),
                    },
                ))
                .unwrap();
        }
        let confirmation = event(
            514,
            WorkerEvent::ScheduledAssetOnAir {
                report_id: "report-1".into(),
                schedule_item_id: "11111111-1111-4111-8111-111111111111".into(),
                schedule_item_revision: 1,
                asset_id: "a".repeat(64),
                media_offset_ms: 12_345,
                worker_boot_id: "22222222-2222-4222-8222-222222222222".into(),
                actual_on_air_at_unix_ms: 1_700_000_000_000,
            },
        );
        writer.append(&confirmation).unwrap();
        writer
            .append(&event(
                515,
                WorkerEvent::Heartbeat {
                    monotonic_milliseconds: 515 * 5_000,
                    outputs: Vec::new(),
                },
            ))
            .unwrap();
        drop(writer);

        let inspection = inspect_journal(&path).unwrap();
        assert!(
            inspection
                .compacted_heartbeat_ranges
                .iter()
                .any(|range| range.first_sequence == 2 && range.last_sequence == 258)
        );
        assert!(
            inspection
                .retained_events
                .windows(2)
                .all(|pair| pair[0].sequence < pair[1].sequence)
        );
        assert_eq!(
            inspection.retained_events.last(),
            Some(&event(
                515,
                WorkerEvent::Heartbeat {
                    monotonic_milliseconds: 515 * 5_000,
                    outputs: Vec::new(),
                },
            ))
        );

        let replayed = read_events(&path).unwrap();
        let confirmation_index = replayed
            .iter()
            .position(|event| event == &confirmation)
            .expect("scheduled acknowledgement remains replayable after compaction");
        assert_eq!(replayed[confirmation_index].sequence, 514);
        assert!(
            replayed[..confirmation_index]
                .iter()
                .all(|event| event.sequence < confirmation.sequence)
        );
        assert!(
            replayed[confirmation_index + 1..]
                .iter()
                .all(|event| event.sequence > confirmation.sequence)
        );

        let _ = std::fs::remove_file(path);
    }
}
