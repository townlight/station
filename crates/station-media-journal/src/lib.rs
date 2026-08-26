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

/// Durable position of a streaming journal consumer. `prefix_hash` is a
/// domain-separated hash chain over every complete record before
/// `byte_offset`. It is deliberately persisted with the file identity and the
/// final covered raw sequence so a restarted reader can prove that it is not
/// resuming in a truncated, replaced, or modified prefix.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct JournalCursor {
    pub byte_offset: u64,
    pub raw_sequence: u64,
    pub prefix_hash: String,
    pub file_identity: String,
    /// Layout facts are part of the durable position. They let a bounded
    /// reader validate the eventual compaction commit even when a retained
    /// event batch ends before that commit record.
    #[serde(default)]
    pub saw_heartbeat_summary: bool,
    #[serde(default)]
    pub saw_compaction_commit: bool,
}

/// One bounded streaming read. Summary and compaction records advance the
/// cursor but are not surfaced as worker events; callers never need to retain
/// the whole journal merely to move their checkpoint forward.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IncrementalJournalRead {
    pub worker_id: Option<String>,
    pub channel_id: Option<String>,
    pub events: Vec<WorkerEventEnvelope>,
    pub cursor: JournalCursor,
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

/// Reads at most `event_limit` newly appended worker events after `cursor`.
///
/// The normal path seeks directly to the durable byte offset; it neither
/// allocates nor rescans retained history. A caller should request
/// `verify_prefix` once after process restart. That one recovery check streams
/// the already-consumed prefix and compares the durable hash chain before any
/// new output is accepted. Subsequent calls retain the checkpoint proof while
/// doing only incremental work. A changed file identity or a shorter file is
/// rejected rather than guessed at; callers can use
/// [`find_journal_event_cursor`] to make an explicit, validated compaction
/// recovery from a retained transition anchor.
pub fn read_journal_incremental(
    path: impl AsRef<Path>,
    cursor: Option<&JournalCursor>,
    event_limit: usize,
    verify_prefix: bool,
) -> Result<IncrementalJournalRead, JournalError> {
    if event_limit == 0 {
        return Err(corrupt(0, "incremental event limit must be positive"));
    }
    let path = path.as_ref();
    let metadata = std::fs::metadata(path).map_err(io_error)?;
    let file_identity = journal_file_identity(&metadata);
    let length = metadata.len();
    if let Some(cursor) = cursor {
        if cursor.byte_offset > length {
            return Err(corrupt(
                cursor.byte_offset,
                "journal was truncated before the durable reader checkpoint",
            ));
        }
        if cursor.file_identity != file_identity {
            return Err(corrupt(
                cursor.byte_offset,
                "journal file identity changed after the durable reader checkpoint",
            ));
        }
        if !valid_prefix_hash(&cursor.prefix_hash) {
            return Err(corrupt(
                cursor.byte_offset,
                "journal cursor prefix hash is invalid",
            ));
        }
        if verify_prefix {
            verify_journal_cursor_prefix(path, cursor)?;
        }
    }

    let mut file = File::open(path).map_err(io_error)?;
    let mut offset = cursor.map_or(0, |value| value.byte_offset);
    file.seek(SeekFrom::Start(offset)).map_err(io_error)?;
    let mut prefix_hash = cursor
        .map(|value| value.prefix_hash.clone())
        .unwrap_or_else(initial_prefix_hash);
    let mut coverage = SequenceCoverage::after_raw(cursor.map_or(0, |value| value.raw_sequence), offset)?;
    let mut worker_id = None;
    let mut channel_id = None;
    let mut events = Vec::new();
    let mut summary_seen = cursor.is_some_and(|value| value.saw_heartbeat_summary);
    let mut commit_seen = cursor.is_some_and(|value| value.saw_compaction_commit);
    let mut saw_event = cursor.is_some();
    let mut summary_ranges = Vec::new();
    let mut summary_range_index = 0_usize;

    while events.len() < event_limit {
        let Some(record) = read_stream_record(&mut file, offset)? else {
            break;
        };
        let record_end = offset
            .checked_add(record.total_bytes)
            .ok_or_else(|| corrupt(offset, "journal offset overflow"))?;
        prefix_hash = next_prefix_hash(&prefix_hash, &record.raw_bytes);
        match record.kind {
            StreamRecordKind::Event(event) => {
                let identity = (event.worker_id.clone(), event.channel_id.clone());
                bind_stream_identity(&mut worker_id, &mut channel_id, identity, offset)?;
                // This is deliberately equality, not merely monotonicity: a
                // CRC-valid journal with a deleted frame must never be
                // accepted across a persisted reader boundary.
                if cursor.is_none() {
                    apply_summary_coverage(
                        &mut coverage,
                        &summary_ranges,
                        &mut summary_range_index,
                        offset,
                    )?;
                }
                coverage.cover(event.sequence, event.sequence, offset)?;
                if cursor.is_none() {
                    apply_summary_coverage(
                        &mut coverage,
                        &summary_ranges,
                        &mut summary_range_index,
                        offset,
                    )?;
                }
                saw_event = true;
                events.push(event);
            }
            StreamRecordKind::HeartbeatSummary(summary) => {
                if saw_event || commit_seen || cursor.is_some() {
                    return Err(corrupt(
                        offset,
                        "heartbeat summary records must be contiguous and precede events",
                    ));
                }
                validate_summary(&summary, offset)?;
                let identity = (summary.worker_id.clone(), summary.channel_id.clone());
                bind_stream_identity(&mut worker_id, &mut channel_id, identity, offset)?;
                for range in &summary.ranges {
                    summary_ranges.push((range.first_sequence, range.last_sequence));
                }
                summary_seen = true;
            }
            StreamRecordKind::CompactionCommit(commit) => {
                if cursor.is_none() {
                    finish_summary_coverage(
                        &mut coverage,
                        &summary_ranges,
                        &mut summary_range_index,
                        offset,
                    )?;
                }
                if !summary_seen || commit_seen {
                    return Err(corrupt(
                        offset,
                        "compaction commit requires one preceding heartbeat summary",
                    ));
                }
                if worker_id.as_deref() != Some(&commit.worker_id)
                    || channel_id.as_deref() != Some(&commit.channel_id)
                    || commit.next_sequence != coverage.next_sequence()
                {
                    return Err(corrupt(
                        offset,
                        "compaction commit does not bind sequence and identity",
                    ));
                }
                commit_seen = true;
            }
        }
        offset = record_end;
    }
    if summary_seen && !commit_seen && events.len() < event_limit {
        return Err(corrupt(0, "heartbeat summary has no compaction commit"));
    }
    Ok(IncrementalJournalRead {
        worker_id,
        channel_id,
        events,
        cursor: JournalCursor {
            byte_offset: offset,
            raw_sequence: coverage.raw_sequence(),
            prefix_hash,
            file_identity,
            saw_heartbeat_summary: summary_seen,
            saw_compaction_commit: commit_seen,
        },
    })
}

/// Streams a journal from the beginning until it finds the requested retained
/// transition. This is intentionally an explicit recovery operation for a
/// detected atomic compaction/replacement, not the steady-state read path.
/// It is allocation bounded and verifies record checksums, identities, and
/// sequence continuity as it walks the prefix.
pub fn find_journal_event_cursor(
    path: impl AsRef<Path>,
    sequence: u64,
) -> Result<(WorkerEventEnvelope, JournalCursor), JournalError> {
    if sequence == 0 {
        return Err(corrupt(0, "journal recovery sequence is invalid"));
    }
    let path = path.as_ref();
    let metadata = std::fs::metadata(path).map_err(io_error)?;
    let file_identity = journal_file_identity(&metadata);
    let mut file = File::open(path).map_err(io_error)?;
    let mut offset = 0_u64;
    let mut coverage = SequenceCoverage::new();
    let mut prefix_hash = initial_prefix_hash();
    let mut identity: Option<(String, String)> = None;
    let mut summary_seen = false;
    let mut commit_seen = false;
    let mut saw_event = false;
    let mut summary_ranges = Vec::new();
    let mut summary_range_index = 0_usize;
    let mut found = None;
    while let Some(record) = read_stream_record(&mut file, offset)? {
        let record_end = offset
            .checked_add(record.total_bytes)
            .ok_or_else(|| corrupt(offset, "journal offset overflow"))?;
        prefix_hash = next_prefix_hash(&prefix_hash, &record.raw_bytes);
        match record.kind {
            StreamRecordKind::Event(event) => {
                let event_identity = (event.worker_id.clone(), event.channel_id.clone());
                if identity
                    .as_ref()
                    .is_some_and(|value| value != &event_identity)
                {
                    return Err(corrupt(offset, "worker or channel identity changed"));
                }
                identity.get_or_insert(event_identity);
                apply_summary_coverage(
                    &mut coverage,
                    &summary_ranges,
                    &mut summary_range_index,
                    offset,
                )?;
                coverage.cover(event.sequence, event.sequence, offset)?;
                apply_summary_coverage(
                    &mut coverage,
                    &summary_ranges,
                    &mut summary_range_index,
                    offset,
                )?;
                saw_event = true;
                let cursor = JournalCursor {
                    byte_offset: record_end,
                    raw_sequence: coverage.raw_sequence(),
                    prefix_hash: prefix_hash.clone(),
                    file_identity: file_identity.clone(),
                    saw_heartbeat_summary: summary_seen,
                    saw_compaction_commit: commit_seen,
                };
                if event.sequence == sequence {
                    found = Some((event, cursor));
                }
            }
            StreamRecordKind::HeartbeatSummary(summary) => {
                if saw_event || commit_seen {
                    return Err(corrupt(offset, "heartbeat summary records are invalid"));
                }
                validate_summary(&summary, offset)?;
                let summary_identity = (summary.worker_id.clone(), summary.channel_id.clone());
                if identity
                    .as_ref()
                    .is_some_and(|value| value != &summary_identity)
                {
                    return Err(corrupt(offset, "worker or channel identity changed"));
                }
                identity.get_or_insert(summary_identity);
                for range in &summary.ranges {
                    summary_ranges.push((range.first_sequence, range.last_sequence));
                }
                summary_seen = true;
            }
            StreamRecordKind::CompactionCommit(commit) => {
                finish_summary_coverage(
                    &mut coverage,
                    &summary_ranges,
                    &mut summary_range_index,
                    offset,
                )?;
                if !summary_seen
                    || commit_seen
                    || identity.as_ref()
                        != Some(&(commit.worker_id.clone(), commit.channel_id.clone()))
                    || commit.next_sequence != coverage.next_sequence()
                {
                    return Err(corrupt(
                        offset,
                        "compaction commit does not bind sequence and identity",
                    ));
                }
                commit_seen = true;
            }
        }
        offset = record_end;
    }
    if summary_seen && !commit_seen {
        return Err(corrupt(0, "heartbeat summary has no compaction commit"));
    }
    found.ok_or_else(|| corrupt(offset, "durable reader anchor is absent after journal replacement"))
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

enum StreamRecordKind {
    Event(WorkerEventEnvelope),
    HeartbeatSummary(HeartbeatSummary),
    CompactionCommit(CompactionCommit),
}

struct StreamRecord {
    kind: StreamRecordKind,
    total_bytes: u64,
    raw_bytes: Vec<u8>,
}

fn read_stream_record(file: &mut File, offset: u64) -> Result<Option<StreamRecord>, JournalError> {
    let mut header = [0_u8; HEADER_BYTES];
    match file.read(&mut header[..1]) {
        Ok(0) => return Ok(None),
        Ok(1) => {}
        Ok(_) => unreachable!("the read buffer has one byte"),
        Err(error) => return Err(io_error(error)),
    }
    read_exact_record(file, &mut header[1..], offset, "truncated record header")?;
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
    read_exact_record(file, &mut payload, offset, "truncated event frame")?;
    if crc32(&payload) != expected_checksum {
        return Err(corrupt(offset, "event frame checksum mismatch"));
    }
    let kind =
        match record_kind {
            RECORD_KIND_EVENT => StreamRecordKind::Event(
                decode_persisted_event_frame(&payload).map_err(JournalError::Protocol)?,
            ),
            RECORD_KIND_HEARTBEAT_SUMMARY if version == JOURNAL_VERSION => {
                StreamRecordKind::HeartbeatSummary(serde_json::from_slice(&payload).map_err(
                    |error| corrupt(offset, format!("invalid heartbeat summary: {error}")),
                )?)
            }
            RECORD_KIND_COMPACTION_COMMIT if version == JOURNAL_VERSION => {
                StreamRecordKind::CompactionCommit(serde_json::from_slice(&payload).map_err(
                    |error| corrupt(offset, format!("invalid compaction commit: {error}")),
                )?)
            }
            _ => {
                return Err(corrupt(
                    offset,
                    format!("unsupported journal record kind {record_kind}"),
                ));
            }
        };
    let mut raw_bytes = Vec::with_capacity(HEADER_BYTES + payload.len());
    raw_bytes.extend_from_slice(&header);
    raw_bytes.extend_from_slice(&payload);
    Ok(Some(StreamRecord {
        kind,
        total_bytes: u64::try_from(raw_bytes.len()).expect("bounded record fits in u64"),
        raw_bytes,
    }))
}

fn initial_prefix_hash() -> String {
    let mut hash = Sha256::new();
    hash.update(b"townlight.incremental-journal-prefix.v1\0");
    format!("{:x}", hash.finalize())
}

fn next_prefix_hash(previous: &str, record: &[u8]) -> String {
    let mut hash = Sha256::new();
    hash.update(b"townlight.incremental-journal-prefix.v1\0");
    hash.update(previous.as_bytes());
    hash.update(record);
    format!("{:x}", hash.finalize())
}

fn valid_prefix_hash(value: &str) -> bool {
    value.len() == 64 && value.as_bytes().iter().all(u8::is_ascii_hexdigit)
}

/// The journal is an exact sequence, not merely a monotonically ordered
/// collection. Keeping this small streaming accumulator shared with snapshot
/// inspection avoids accepting a CRC-valid omission between a compaction
/// summary, retained frame, or persisted reader boundary.
struct SequenceCoverage {
    next: u64,
}

impl SequenceCoverage {
    fn new() -> Self {
        Self { next: 1 }
    }

    fn after_raw(raw_sequence: u64, offset: u64) -> Result<Self, JournalError> {
        Ok(Self {
            next: raw_sequence
                .checked_add(1)
                .ok_or_else(|| corrupt(offset, "journal sequence exhausted"))?,
        })
    }

    fn cover(&mut self, first: u64, last: u64, offset: u64) -> Result<(), JournalError> {
        if first != self.next || last < first {
            return Err(JournalError::NonMonotonic {
                expected: self.next,
                actual: first,
            });
        }
        self.next = last
            .checked_add(1)
            .ok_or_else(|| corrupt(offset, "journal sequence exhausted"))?;
        Ok(())
    }

    fn next_sequence(&self) -> u64 {
        self.next
    }

    fn raw_sequence(&self) -> u64 {
        self.next - 1
    }
}

/// Compaction summaries are physically written before all retained frames,
/// even when a retained non-heartbeat has an earlier raw sequence. Merge the
/// bounded summary ranges into physical event streaming only when they extend
/// the exact contiguous logical prefix.
fn apply_summary_coverage(
    coverage: &mut SequenceCoverage,
    summary_ranges: &[(u64, u64)],
    summary_range_index: &mut usize,
    offset: u64,
) -> Result<(), JournalError> {
    while let Some((first, last)) = summary_ranges.get(*summary_range_index).copied() {
        if first > coverage.next_sequence() {
            break;
        }
        coverage.cover(first, last, offset)?;
        *summary_range_index += 1;
    }
    Ok(())
}

fn finish_summary_coverage(
    coverage: &mut SequenceCoverage,
    summary_ranges: &[(u64, u64)],
    summary_range_index: &mut usize,
    offset: u64,
) -> Result<(), JournalError> {
    while let Some((first, last)) = summary_ranges.get(*summary_range_index).copied() {
        coverage.cover(first, last, offset)?;
        *summary_range_index += 1;
    }
    Ok(())
}

fn bind_stream_identity(
    worker_id: &mut Option<String>,
    channel_id: &mut Option<String>,
    identity: (String, String),
    offset: u64,
) -> Result<(), JournalError> {
    match (&worker_id, &channel_id) {
        (Some(worker), Some(channel)) if (worker, channel) != (&identity.0, &identity.1) => {
            Err(corrupt(offset, "worker or channel identity changed"))
        }
        (None, None) => {
            *worker_id = Some(identity.0);
            *channel_id = Some(identity.1);
            Ok(())
        }
        (Some(_), Some(_)) => Ok(()),
        _ => Err(corrupt(offset, "journal identity is incomplete")),
    }
}

fn verify_journal_cursor_prefix(path: &Path, cursor: &JournalCursor) -> Result<(), JournalError> {
    let mut file = File::open(path).map_err(io_error)?;
    let mut offset = 0_u64;
    let mut coverage = SequenceCoverage::new();
    let mut prefix_hash = initial_prefix_hash();
    let mut worker_id = None;
    let mut channel_id = None;
    let mut summary_seen = false;
    let mut commit_seen = false;
    let mut saw_event = false;
    let mut summary_ranges = Vec::new();
    let mut summary_range_index = 0_usize;
    while offset < cursor.byte_offset {
        let record = read_stream_record(&mut file, offset)?
            .ok_or_else(|| corrupt(offset, "journal ended before the durable reader checkpoint"))?;
        offset = offset
            .checked_add(record.total_bytes)
            .ok_or_else(|| corrupt(offset, "journal offset overflow"))?;
        if offset > cursor.byte_offset {
            return Err(corrupt(
                cursor.byte_offset,
                "durable reader checkpoint is not on a journal record boundary",
            ));
        }
        prefix_hash = next_prefix_hash(&prefix_hash, &record.raw_bytes);
        match record.kind {
            StreamRecordKind::Event(event) => {
                bind_stream_identity(
                    &mut worker_id,
                    &mut channel_id,
                    (event.worker_id, event.channel_id),
                    offset,
                )?;
                apply_summary_coverage(
                    &mut coverage,
                    &summary_ranges,
                    &mut summary_range_index,
                    offset,
                )?;
                coverage.cover(event.sequence, event.sequence, offset)?;
                apply_summary_coverage(
                    &mut coverage,
                    &summary_ranges,
                    &mut summary_range_index,
                    offset,
                )?;
                saw_event = true;
            }
            StreamRecordKind::HeartbeatSummary(summary) => {
                if saw_event || commit_seen {
                    return Err(corrupt(offset, "heartbeat summary records are invalid"));
                }
                validate_summary(&summary, offset)?;
                bind_stream_identity(
                    &mut worker_id,
                    &mut channel_id,
                    (summary.worker_id, summary.channel_id),
                    offset,
                )?;
                for range in &summary.ranges {
                    summary_ranges.push((range.first_sequence, range.last_sequence));
                }
                summary_seen = true;
            }
            StreamRecordKind::CompactionCommit(commit) => {
                finish_summary_coverage(
                    &mut coverage,
                    &summary_ranges,
                    &mut summary_range_index,
                    offset,
                )?;
                if !summary_seen
                    || commit_seen
                    || worker_id.as_deref() != Some(&commit.worker_id)
                    || channel_id.as_deref() != Some(&commit.channel_id)
                    || commit.next_sequence != coverage.next_sequence()
                {
                    return Err(corrupt(
                        offset,
                        "compaction commit does not match complete sequence coverage",
                    ));
                }
                commit_seen = true;
            }
        }
    }
    if offset != cursor.byte_offset
        || coverage.raw_sequence() != cursor.raw_sequence
        || prefix_hash != cursor.prefix_hash
        || summary_seen != cursor.saw_heartbeat_summary
        || commit_seen != cursor.saw_compaction_commit
    {
        return Err(corrupt(
            cursor.byte_offset,
            "durable reader checkpoint prefix integrity check failed",
        ));
    }
    Ok(())
}

#[cfg(windows)]
fn journal_file_identity(metadata: &std::fs::Metadata) -> String {
    use std::os::windows::fs::MetadataExt;
    // Creation time survives ordinary appends, while atomic replacement gives
    // the replacement a distinct identity. The cursor's prefix hash covers
    // in-place modification when the reader is restarted.
    format!(
        "windows:{}:{}",
        metadata.creation_time(),
        metadata.file_attributes()
    )
}

#[cfg(unix)]
fn journal_file_identity(metadata: &std::fs::Metadata) -> String {
    use std::os::unix::fs::MetadataExt;
    format!("unix:{}:{}", metadata.dev(), metadata.ino())
}

#[cfg(not(any(windows, unix)))]
fn journal_file_identity(metadata: &std::fs::Metadata) -> String {
    format!("portable:{}", metadata.len())
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
    let mut ranges = Vec::with_capacity(
        events.len() + heartbeat_summary.map_or(0, |summary| summary.ranges.len()),
    );
    if let Some(summary) = heartbeat_summary {
        ranges.extend(
            summary
                .ranges
                .iter()
                .map(|range| (range.first_sequence, range.last_sequence)),
        );
    }
    ranges.extend(events.iter().map(|event| (event.sequence, event.sequence)));
    ranges.sort_unstable_by_key(|range| range.0);
    let mut coverage = SequenceCoverage::new();
    for (first, last) in ranges {
        coverage.cover(first, last, offset)?;
    }
    Ok(coverage.next_sequence())
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
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{SystemTime, UNIX_EPOCH};

    use station_media_protocol::PROTOCOL_VERSION;

    use super::*;

    const WORKER_ID: &str = "d4af49c4-a230-44c5-8f55-c9a0224846b4";
    const CHANNEL_ID: &str = "1500c96b-26c2-4534-a97f-2a8c2ed7fa5e";
    static NEXT_TEST_PATH: AtomicU64 = AtomicU64::new(0);

    fn test_path() -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock is before Unix epoch")
            .as_nanos();
        let sequence = NEXT_TEST_PATH.fetch_add(1, Ordering::Relaxed);
        std::env::temp_dir().join(format!(
            "townlight-replace-failure-{}-{nonce}-{sequence}.tlj",
            std::process::id()
        ))
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

    #[test]
    fn bounded_reader_covers_every_retained_sequence_through_compaction_commit() {
        let path = test_path();
        let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        writer
            .append(&event(1, WorkerEvent::Ready { graph_revision: 1 }))
            .unwrap();
        for sequence in 2..=514 {
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
        drop(writer);

        let mut batch = read_journal_incremental(&path, None, 1, false).unwrap();
        assert_eq!(batch.events[0].sequence, 1);
        // The compacted summary is logically covered even though the retained
        // Ready frame is physically written after it.
        assert_eq!(batch.cursor.raw_sequence, 258);
        loop {
            let prior_raw_sequence = batch.cursor.raw_sequence;
            let next = read_journal_incremental(&path, Some(&batch.cursor), 17, false).unwrap();
            if next.events.is_empty() {
                batch = next;
                break;
            }
            assert!(next.events[0].sequence > prior_raw_sequence);
            batch = next;
        }
        assert!(batch.cursor.saw_compaction_commit);
        assert_eq!(
            batch.cursor.raw_sequence + 1,
            inspect_journal(&path).unwrap().next_sequence
        );
        let _ = std::fs::remove_file(path);
    }

    fn compacted_summary() -> HeartbeatSummary {
        HeartbeatSummary {
            worker_id: WORKER_ID.into(),
            channel_id: CHANNEL_ID.into(),
            ranges: vec![CompactedHeartbeatRange {
                first_sequence: 1,
                last_sequence: 1,
                sample_count: 1,
                first_monotonic_milliseconds: 5_000,
                last_monotonic_milliseconds: 5_000,
                first_outputs: Vec::new(),
                last_outputs: Vec::new(),
                digest_chain: [0; 32],
            }],
        }
    }

    fn write_compacted_fixture(
        path: &Path,
        retained: &[WorkerEventEnvelope],
        committed_next_sequence: u64,
        tail: &[WorkerEventEnvelope],
    ) {
        let mut file = File::create(path).unwrap();
        let summary = serde_json::to_vec(&compacted_summary()).unwrap();
        write_record(&mut file, RECORD_KIND_HEARTBEAT_SUMMARY, &summary).unwrap();
        for envelope in retained {
            let frame = encode_event_frame(envelope).unwrap();
            write_record(&mut file, RECORD_KIND_EVENT, &frame).unwrap();
        }
        let commit = serde_json::to_vec(&CompactionCommit {
            worker_id: WORKER_ID.into(),
            channel_id: CHANNEL_ID.into(),
            next_sequence: committed_next_sequence,
        })
        .unwrap();
        write_record(&mut file, RECORD_KIND_COMPACTION_COMMIT, &commit).unwrap();
        for envelope in tail {
            let frame = encode_event_frame(envelope).unwrap();
            write_record(&mut file, RECORD_KIND_EVENT, &frame).unwrap();
        }
        file.sync_all().unwrap();
    }

    #[test]
    fn incremental_cursor_rejects_a_crc_valid_omission_after_its_boundary() {
        let path = test_path();
        let first = event(1, WorkerEvent::Ready { graph_revision: 1 });
        let third = event(3, WorkerEvent::ShutdownComplete);
        let mut file = File::create(&path).unwrap();
        let frame = encode_event_frame(&first).unwrap();
        write_record(&mut file, RECORD_KIND_EVENT, &frame).unwrap();
        let frame = encode_event_frame(&third).unwrap();
        write_record(&mut file, RECORD_KIND_EVENT, &frame).unwrap();
        file.sync_all().unwrap();

        // Obtain the exact prefix-chain value through the public reader; a
        // sequence number alone is not a valid durable cursor.
        let first_batch = read_journal_incremental(&path, None, 1, false).unwrap();
        assert_eq!(first_batch.cursor.raw_sequence, 1);
        assert!(matches!(
            read_journal_incremental(&path, Some(&first_batch.cursor), 8, false),
            Err(JournalError::NonMonotonic {
                expected: 2,
                actual: 3
            })
        ));
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn incremental_reader_rejects_bad_compaction_commit_against_complete_coverage() {
        let path = test_path();
        write_compacted_fixture(
            &path,
            &[event(2, WorkerEvent::Ready { graph_revision: 1 })],
            4,
            &[],
        );
        assert!(matches!(
            read_journal_incremental(&path, None, 8, false),
            Err(JournalError::Corrupt { reason, .. })
                if reason.contains("compaction commit")
        ));
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn anchor_recovery_validates_coverage_after_the_retained_anchor() {
        let path = test_path();
        write_compacted_fixture(
            &path,
            &[event(2, WorkerEvent::Ready { graph_revision: 1 })],
            3,
            &[event(5, WorkerEvent::ShutdownComplete)],
        );
        assert!(matches!(
            find_journal_event_cursor(&path, 2),
            Err(JournalError::NonMonotonic {
                expected: 3,
                actual: 5
            })
        ));
        let _ = std::fs::remove_file(path);
    }
}
