use std::collections::HashMap;
use std::ffi::OsString;
use std::fs::{File, OpenOptions};
use std::io::{ErrorKind, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
#[cfg(windows)]
use std::sync::{Mutex, OnceLock};

#[cfg(windows)]
use std::os::windows::{
    fs::OpenOptionsExt,
    io::{AsRawHandle, FromRawHandle},
};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use station_media_protocol::{
    FallbackToneDeactivationReason, MAX_FRAME_BYTES, OutputTransportObservation, ProtocolError,
    WorkerEvent, WorkerEventEnvelope, WorkerFallbackTone, decode_event_frame,
    decode_persisted_event_frame, encode_event_frame,
};

const MAGIC: [u8; 4] = *b"TLJR";
const LEGACY_JOURNAL_VERSION: u16 = 1;
const JOURNAL_VERSION: u16 = 2;
const HEADER_BYTES: usize = 16;
const MAX_EVENT_FRAME_BYTES: usize = MAX_FRAME_BYTES + 4;
const RECORD_KIND_EVENT: u16 = 0;
const RECORD_KIND_HEARTBEAT_SUMMARY: u16 = 1;
const RECORD_KIND_COMPACTION_COMMIT: u16 = 2;
const RECORD_KIND_FALLBACK_TONE_AUDIT: u16 = 3;
const COMPACT_AFTER_RAW_HEARTBEATS: usize = 512;
const RETAIN_RAW_HEARTBEATS: usize = 256;
const MAX_SUMMARY_OUTPUTS: usize = 64;
const COMPACTION_LAYOUT_RETAINED_THEN_COMMIT: u8 = 1;
const COMPACTION_LAYOUT_COMMIT_THEN_RETAINED: u8 = 2;
// Version two separates an immutable compacted generation from its mutable
// append segment. Version one selected a generation which the writer kept
// appending to, so accepting it as an immutable object would be unsound.
const GENERATION_MANIFEST_VERSION: u8 = 2;
const LEGACY_GENERATION_MANIFEST_VERSION: u8 = 1;

/// A manifest authority binds one exact Windows file handle, FileId, and
/// digest to a generation selection rather than trusting the pathname after
/// JSON parsing. Strict handles omit write/delete sharing. A reporter that
/// encounters a live writer duplicates the writer's exact no-delete source
/// handle, so its authority survives abrupt writer-process termination. The
/// publisher keeps its same source handle through handle-based rename/proof.
struct ManifestAuthorityHandle {
    file: File,
    file_identity: String,
    manifest_hash: String,
}

#[cfg(windows)]
struct VerifiedPrefixSession {
    /// The currently streaming object: legacy/sealed generation before the
    /// hand-off, then the append-only active segment.
    file: File,
    /// When `file` is active, retain the byte-range-locked immutable sealed
    /// object too. This keeps an in-place rewrite of the manifest's sealed
    /// authority from slipping through after the reader starts consuming the
    /// active suffix, while still never locking the writer's append target.
    sealed_file: Option<File>,
    sealed_path: Option<PathBuf>,
    /// Exact manifest cursor proven on this immutable generation. It remains
    /// after the durable consumer advances a smaller batch cursor, avoiding a
    /// second full proof during catch-up while retaining the full range lock.
    sealed_proof: Option<JournalCursor>,
    /// The small, process-visible lease protects the validation/read/persist
    /// transaction. Unlike the file handle it is deliberately released after
    /// the projection transaction commits, so a worker may publish a new
    /// immutable generation without depending on another process to release
    /// this process's range lock.
    lease: Option<File>,
    data_path: PathBuf,
    generation: u64,
    cursor: JournalCursor,
}

#[cfg(windows)]
fn verified_prefix_sessions() -> &'static Mutex<HashMap<PathBuf, VerifiedPrefixSession>> {
    static SESSIONS: OnceLock<Mutex<HashMap<PathBuf, VerifiedPrefixSession>>> = OnceLock::new();
    SESSIONS.get_or_init(|| Mutex::new(HashMap::new()))
}

#[cfg(windows)]
fn pending_reader_leases() -> &'static Mutex<HashMap<PathBuf, File>> {
    static LEASES: OnceLock<Mutex<HashMap<PathBuf, File>>> = OnceLock::new();
    LEASES.get_or_init(|| Mutex::new(HashMap::new()))
}

/// Reader transactions keep the manifest's locked authority handle only until the
/// caller acknowledges its durable projection persist. Prefix sessions remain
/// cached afterwards, but no longer block the next writer publication.
#[cfg(windows)]
fn retained_manifest_authorities() -> &'static Mutex<HashMap<PathBuf, ManifestAuthorityHandle>> {
    static AUTHORITIES: OnceLock<Mutex<HashMap<PathBuf, ManifestAuthorityHandle>>> =
        OnceLock::new();
    AUTHORITIES.get_or_init(|| Mutex::new(HashMap::new()))
}

/// A read transaction can resolve the manifest more than once while it holds
/// its short authority lease (for example when it changes from the sealed
/// generation to the active segment). Reuse a duplicate of the already
/// retained no-delete handle instead of reopening the pathname: reopening is
/// intentionally denied until acknowledgement.
#[cfg(windows)]
fn duplicate_retained_manifest_authority(
    root: &Path,
) -> Result<Option<(ManifestAuthorityHandle, Vec<u8>)>, JournalError> {
    let authority = {
        let authorities = retained_manifest_authorities()
            .lock()
            .expect("retained manifest authorities are not poisoned");
        let Some(authority) = authorities.get(root) else {
            return Ok(None);
        };
        ManifestAuthorityHandle {
            file: authority.file.try_clone().map_err(io_error)?,
            file_identity: authority.file_identity.clone(),
            manifest_hash: authority.manifest_hash.clone(),
        }
    };
    let bytes = read_file_bytes(&authority.file)?;
    if manifest_hash(&bytes) != authority.manifest_hash
        || journal_file_identity(&authority.file)? != authority.file_identity
    {
        return Err(corrupt(
            0,
            "retained journal generation manifest authority changed",
        ));
    }
    Ok(Some((authority, bytes)))
}

/// Returns the durable manifest binding held by this process's current
/// incremental-reader transaction. The retained handle itself denies delete
/// sharing, including after the writer process that supplied it exits.
#[cfg(windows)]
fn retained_manifest_authority_binding(root: &Path) -> Option<(String, String)> {
    retained_manifest_authorities()
        .lock()
        .expect("retained manifest authorities are not poisoned")
        .get(root)
        .map(|authority| {
            (
                authority.manifest_hash.clone(),
                authority.file_identity.clone(),
            )
        })
}

#[cfg(windows)]
fn mirror_manifest_authority_for_resolution(
    root: &Path,
    authority: &ManifestAuthorityHandle,
) -> Result<(), JournalError> {
    let mirror = ManifestAuthorityHandle {
        file: authority.file.try_clone().map_err(io_error)?,
        file_identity: authority.file_identity.clone(),
        manifest_hash: authority.manifest_hash.clone(),
    };
    let mut authorities = retained_manifest_authorities()
        .lock()
        .expect("retained manifest authorities are not poisoned");
    if authorities.contains_key(root) {
        return Err(corrupt(
            0,
            "writer cannot mirror a manifest authority while a reader is live",
        ));
    }
    authorities.insert(root.to_path_buf(), mirror);
    Ok(())
}

#[cfg(windows)]
fn clear_mirrored_manifest_authority(root: &Path) {
    retained_manifest_authorities()
        .lock()
        .expect("retained manifest authorities are not poisoned")
        .remove(root);
}

#[cfg(not(windows))]
fn mirror_manifest_authority_for_resolution(
    _root: &Path,
    _authority: &ManifestAuthorityHandle,
) -> Result<(), JournalError> {
    Ok(())
}

#[cfg(not(windows))]
fn clear_mirrored_manifest_authority(_root: &Path) {}

#[cfg(not(windows))]
fn duplicate_retained_manifest_authority(
    _root: &Path,
) -> Result<Option<(ManifestAuthorityHandle, Vec<u8>)>, JournalError> {
    Ok(None)
}

#[cfg(windows)]
struct EarlyReaderLease {
    root: PathBuf,
}

#[cfg(windows)]
impl Drop for EarlyReaderLease {
    fn drop(&mut self) {
        pending_reader_leases()
            .lock()
            .expect("pending reader leases are not poisoned")
            .remove(&self.root);
    }
}

fn begin_incremental_reader_lease(
    root: &Path,
    enabled: bool,
) -> Result<Option<EarlyReaderLease>, JournalError> {
    #[cfg(windows)]
    {
        if !enabled {
            return Ok(None);
        }
        if verified_prefix_sessions()
            .lock()
            .expect("verified prefix sessions are not poisoned")
            .get(root)
            .is_some_and(|session| session.lease.is_some())
        {
            return Ok(None);
        }
        let mut pending = pending_reader_leases()
            .lock()
            .expect("pending reader leases are not poisoned");
        if !pending.contains_key(root) {
            pending.insert(root.to_path_buf(), acquire_reader_lease(root)?);
        }
        Ok(Some(EarlyReaderLease {
            root: root.to_path_buf(),
        }))
    }
    #[cfg(not(windows))]
    {
        let _ = (root, enabled);
        Ok(None)
    }
}

#[cfg(windows)]
fn take_incremental_reader_lease(root: &Path) -> Result<File, JournalError> {
    if let Some(lease) = pending_reader_leases()
        .lock()
        .expect("pending reader leases are not poisoned")
        .remove(root)
    {
        return Ok(lease);
    }
    acquire_reader_lease(root)
}

fn release_verified_prefix_session(path: &Path) {
    #[cfg(windows)]
    {
        let removed = verified_prefix_sessions()
            .lock()
            .expect("verified prefix sessions are not poisoned")
            .remove(path);
        drop(removed);
        pending_reader_leases()
            .lock()
            .expect("pending reader leases are not poisoned")
            .remove(path);
        retained_manifest_authorities()
            .lock()
            .expect("retained manifest authorities are not poisoned")
            .remove(path);
        let lease = reader_lease_path(path);
        let _ = std::fs::remove_file(lease);
    }
    #[cfg(not(windows))]
    {
        let _ = path;
    }
}

/// Acknowledge the projection transaction without discarding this process's
/// authenticated byte-range session. The next pass on the same immutable
/// generation only proves its bounded suffix; a changed manifest retires the
/// stale session before it can advance authority.
pub fn acknowledge_incremental_reader(path: impl AsRef<Path>) {
    #[cfg(windows)]
    {
        let root = path.as_ref();
        let mut sessions = verified_prefix_sessions()
            .lock()
            .expect("verified prefix sessions are not poisoned");
        if let Some(session) = sessions.get_mut(root) {
            session.lease.take();
        }
        drop(sessions);
        pending_reader_leases()
            .lock()
            .expect("pending reader leases are not poisoned")
            .remove(root);
        retained_manifest_authorities()
            .lock()
            .expect("retained manifest authorities are not poisoned")
            .remove(root);
        let _ = std::fs::remove_file(reader_lease_path(root));
    }
    #[cfg(not(windows))]
    {
        let _ = path;
    }
}

#[cfg(windows)]
fn reader_lease_path(path: &Path) -> PathBuf {
    let mut lease: OsString = path.as_os_str().to_owned();
    lease.push(".reader-lease");
    lease.into()
}

fn generation_manifest_path(root: &Path) -> PathBuf {
    let mut path: OsString = root.as_os_str().to_owned();
    path.push(".generation-manifest");
    path.into()
}

fn generation_manifest_temp_path(root: &Path) -> PathBuf {
    let mut path: OsString = root.as_os_str().to_owned();
    path.push(".generation-manifest.tmp");
    path.into()
}

fn generation_path(root: &Path, generation: u64) -> PathBuf {
    let mut path: OsString = root.as_os_str().to_owned();
    path.push(format!(".generation-{generation}"));
    path.into()
}

fn generation_temp_path(root: &Path, generation: u64) -> PathBuf {
    let mut path: OsString = root.as_os_str().to_owned();
    path.push(format!(".generation-{generation}.compact.tmp"));
    path.into()
}

/// A durable intent makes the three-object publication (sealed generation,
/// empty active segment, manifest) recoverable at every rename boundary. It
/// is written and synced before either visible object is promoted, and is
/// removed only after the manifest becomes authoritative.
fn generation_publication_path(root: &Path, generation: u64) -> PathBuf {
    let mut path: OsString = root.as_os_str().to_owned();
    path.push(format!(".generation-{generation}.publication"));
    path.into()
}

fn generation_publication_temp_path(root: &Path, generation: u64) -> PathBuf {
    let mut path: OsString = root.as_os_str().to_owned();
    path.push(format!(".generation-{generation}.publication.tmp"));
    path.into()
}

/// Finds only this journal's generation artifacts.  Generations older than
/// the manifest can legitimately be awaiting a reporter-lease acknowledgement;
/// future generations beyond the single next candidate cannot arise from one
/// writer transaction and are therefore fail-closed.
fn generation_artifact_name(root: &Path, name: &std::ffi::OsStr) -> Option<(u64, bool)> {
    let root_name = root.file_name()?.to_string_lossy();
    let name = name.to_string_lossy();
    for (prefix, active) in [
        (format!("{root_name}.generation-"), false),
        (format!("{root_name}.active-"), true),
    ] {
        let Some(rest) = name.strip_prefix(&prefix) else {
            continue;
        };
        let digits_len = rest.bytes().take_while(u8::is_ascii_digit).count();
        if digits_len == 0 {
            return None;
        }
        let generation = rest[..digits_len].parse().ok()?;
        let suffix = &rest[digits_len..];
        let publication = !active && matches!(suffix, ".publication" | ".publication.tmp");
        let recognized = if active {
            matches!(suffix, "" | ".tmp")
        } else {
            matches!(
                suffix,
                "" | ".compact.tmp" | ".publication" | ".publication.tmp"
            )
        };
        if recognized {
            return Some((generation, publication));
        }
    }
    None
}

fn reject_ambiguous_generation_artifacts(
    root: &Path,
    current_generation: u64,
) -> Result<(), JournalError> {
    let expected_generation = current_generation
        .checked_add(1)
        .ok_or_else(|| corrupt(0, "journal generation exhausted during recovery"))?;
    let parent = root.parent().unwrap_or_else(|| Path::new("."));
    for entry in std::fs::read_dir(parent).map_err(io_error)? {
        let entry = entry.map_err(io_error)?;
        let Some((generation, publication)) = generation_artifact_name(root, &entry.file_name())
        else {
            continue;
        };
        if generation > expected_generation {
            return Err(corrupt(
                0,
                "journal has multiple future generation publication artifacts",
            ));
        }
        if publication && (generation < current_generation || generation == 0) {
            return Err(corrupt(
                0,
                "journal has a stale generation publication intent",
            ));
        }
    }
    Ok(())
}

fn active_segment_path(root: &Path, generation: u64) -> PathBuf {
    let mut path: OsString = root.as_os_str().to_owned();
    path.push(format!(".active-{generation}"));
    path.into()
}

fn active_segment_temp_path(root: &Path, generation: u64) -> PathBuf {
    let mut path: OsString = root.as_os_str().to_owned();
    path.push(format!(".active-{generation}.tmp"));
    path.into()
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct JournalGenerationManifest {
    version: u8,
    generation: u64,
    worker_id: String,
    channel_id: String,
    sealed_file_identity: String,
    active_file_identity: String,
    sealed_byte_offset: u64,
    sealed_raw_sequence: u64,
    sealed_prefix_hash: String,
    sealed_compaction_next_sequence: u64,
}

const GENERATION_PUBLICATION_VERSION: u8 = 1;

/// This is intentionally separate from the public manifest. It binds an
/// otherwise one-sided file to the exact currently published logical source
/// and to the exact bytes that were fsynced in the candidate. Therefore an
/// active-only artifact is never mistaken for authority, and a sealed-only
/// artifact can be completed without guessing.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct GenerationPublicationIntent {
    version: u8,
    generation: u64,
    source_generation: u64,
    worker_id: String,
    channel_id: String,
    source_next_sequence: u64,
    source_identity_worker_id: String,
    source_identity_channel_id: String,
    candidate_sha256: String,
}

/// The first immutable-generation rollout published this shape. It is read
/// only long enough for a writer holding the root ownership lock to derive a
/// fully authenticated v2 sealed proof and atomically replace the manifest.
/// A reporter never resumes an old byte cursor against it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct LegacyJournalGenerationManifest {
    version: u8,
    generation: u64,
    worker_id: String,
    channel_id: String,
    file_identity: String,
    sealed_next_sequence: u64,
}

#[derive(Debug, Clone)]
struct JournalLocation {
    /// The immutable compacted prefix selected by the manifest, or the
    /// pre-manifest root journal during rolling upgrade.
    data_path: PathBuf,
    /// A generation-specific append segment. It is never rewritten or
    /// replaced; compaction seals its contents into the next `data_path`.
    active_path: Option<PathBuf>,
    generation: u64,
    manifest_hash: String,
    manifest: Option<JournalGenerationManifest>,
    legacy_manifest: Option<LegacyJournalGenerationManifest>,
}

#[derive(Clone, Copy)]
struct JournalReadSegment<'a> {
    root: &'a Path,
    data_path: &'a Path,
    generation: u64,
    manifest_hash: &'a str,
    segment_kind: u8,
}

struct CursorSource<'a> {
    generation: u64,
    manifest_hash: &'a str,
    segment_kind: u8,
    file_identity: &'a str,
}

fn manifest_hash(bytes: &[u8]) -> String {
    let mut hash = Sha256::new();
    hash.update(b"townlight.journal-generation-manifest.v1\0");
    hash.update(bytes);
    format!("{:x}", hash.finalize())
}

/// The root journal path is a stable rendezvous point. Once compaction has
/// happened, a tiny atomically replaced manifest selects a generation file;
/// already-published generations are never overwritten. A missing manifest is
/// the legacy root journal (generation zero), retained for rolling upgrade.
fn resolve_journal_location(root: &Path) -> Result<JournalLocation, JournalError> {
    resolve_journal_location_with_authority(root, false, false)
}

/// Writer startup/recovery never accepts the incremental reader's permissive
/// fallback. It must acquire a strict no-delete manifest authority itself.
fn resolve_journal_location_strict(root: &Path) -> Result<JournalLocation, JournalError> {
    resolve_journal_location_with_authority(root, false, false)
}

/// Resolves a manifest from an exact locked handle. Production incremental
/// reads retain that handle through acknowledgement; every other resolver at
/// least holds it while parsing and validating the selected generation.
fn resolve_journal_location_with_authority(
    root: &Path,
    retain_authority: bool,
    allow_live_writer_handoff: bool,
) -> Result<JournalLocation, JournalError> {
    resolve_journal_location_with_authority_policy(
        root,
        retain_authority,
        allow_live_writer_handoff,
        true,
    )
}

fn resolve_journal_location_with_authority_policy(
    root: &Path,
    retain_authority: bool,
    allow_live_writer_handoff: bool,
    reuse_retained_authority: bool,
) -> Result<JournalLocation, JournalError> {
    let manifest_path = generation_manifest_path(root);
    if !manifest_path.try_exists().map_err(io_error)? {
        return Ok(JournalLocation {
            data_path: root.to_path_buf(),
            active_path: None,
            generation: 0,
            manifest_hash: String::new(),
            manifest: None,
            legacy_manifest: None,
        });
    }
    let (manifest_authority, manifest_bytes) = if reuse_retained_authority {
        if let Some(authority) = duplicate_retained_manifest_authority(root)? {
            authority
        } else {
            open_manifest_authority_handle(root, &manifest_path, None, allow_live_writer_handoff)?
        }
    } else {
        open_manifest_authority_handle(root, &manifest_path, None, allow_live_writer_handoff)?
    };
    let manifest: JournalGenerationManifest = match serde_json::from_slice(&manifest_bytes) {
        Ok(manifest) => manifest,
        Err(v2_error) => {
            let legacy: LegacyJournalGenerationManifest = serde_json::from_slice(&manifest_bytes)
                .map_err(|_| {
                corrupt(
                    0,
                    format!("journal generation manifest is invalid: {v2_error}"),
                )
            })?;
            let location = resolve_legacy_generation_location(root, manifest_bytes, legacy)?;
            if retain_authority {
                retain_manifest_authority(root, manifest_authority);
            }
            return Ok(location);
        }
    };
    if manifest.version != GENERATION_MANIFEST_VERSION
        || manifest.generation == 0
        || manifest.worker_id.trim().is_empty()
        || manifest.channel_id.trim().is_empty()
        || manifest.sealed_byte_offset == 0
        || manifest.sealed_raw_sequence == 0
        || !valid_prefix_hash(&manifest.sealed_prefix_hash)
        || manifest.sealed_compaction_next_sequence == 0
        || manifest.sealed_file_identity.trim().is_empty()
        || manifest.active_file_identity.trim().is_empty()
    {
        return Err(corrupt(0, "journal generation manifest is invalid"));
    }
    let data_path = generation_path(root, manifest.generation);
    let file = File::open(&data_path).map_err(|error| {
        corrupt(
            0,
            format!("published journal generation is unavailable: {error}"),
        )
    })?;
    if journal_file_identity(&file)? != manifest.sealed_file_identity {
        return Err(corrupt(0, "published journal generation identity changed"));
    }
    let active_path = active_segment_path(root, manifest.generation);
    let active = File::open(&active_path).map_err(|error| {
        corrupt(
            0,
            format!("published journal active segment is unavailable: {error}"),
        )
    })?;
    if journal_file_identity(&active)? != manifest.active_file_identity {
        return Err(corrupt(
            0,
            "published journal active segment identity changed",
        ));
    }
    // The writer fully validates and fsyncs the candidate before publication;
    // the manifest binds its immutable object identity and sealed boundary.
    // Do not replay this generation here: cache hits must only read the small
    // manifest plus their bounded journal suffix. A reader cache miss proves
    // the persisted cursor against the exact handle before authority moves.
    let location = JournalLocation {
        data_path,
        active_path: Some(active_path),
        generation: manifest.generation,
        manifest_hash: manifest_authority.manifest_hash.clone(),
        manifest: Some(manifest),
        legacy_manifest: None,
    };
    if retain_authority {
        retain_manifest_authority(root, manifest_authority);
    }
    Ok(location)
}

fn read_file_bytes(file: &File) -> Result<Vec<u8>, JournalError> {
    let mut file = file.try_clone().map_err(io_error)?;
    file.seek(SeekFrom::Start(0)).map_err(io_error)?;
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes).map_err(io_error)?;
    Ok(bytes)
}

/// Opens a temporary identity-check handle which is compatible with a live
/// no-delete publisher handle. This handle is never authority: its only role
/// is to compare the currently named object with the publisher's retained
/// FileId while that publisher still denies replacement.
fn open_manifest_identity_check_handle(path: &Path) -> Result<File, JournalError> {
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(windows)]
    // Existing publisher handle has DELETE and WRITE access. A second handle
    // must share those accesses to inspect its final pathname; it cannot
    // mutate/replace it because the retained publisher shares only READ.
    options.share_mode(0x0000_0001 | 0x0000_0002 | 0x0000_0004);
    options.open(path).map_err(io_error)
}

fn verify_manifest_path_matches_retained_handle(
    file: &File,
    path: &Path,
    expected_bytes: &[u8],
    expected_identity: &str,
) -> Result<(), JournalError> {
    let metadata = std::fs::symlink_metadata(path).map_err(io_error)?;
    if !metadata.is_file() || metadata.file_type().is_symlink() || is_reparse_point(&metadata) {
        return Err(corrupt(
            0,
            "published journal generation manifest is not a regular non-reparse file",
        ));
    }
    let current = open_manifest_identity_check_handle(path)?;
    let current_metadata = current.metadata().map_err(io_error)?;
    if !current_metadata.is_file() || is_reparse_point(&current_metadata) {
        return Err(corrupt(
            0,
            "published journal generation manifest is not a regular non-reparse file",
        ));
    }
    if journal_file_identity(&current)? != expected_identity
        || journal_file_identity(file)? != expected_identity
        || read_file_bytes(file)? != expected_bytes
    {
        return Err(corrupt(
            0,
            "published journal generation manifest changed during promotion",
        ));
    }
    Ok(())
}

fn open_manifest_authority_handle(
    root: &Path,
    path: &Path,
    expected_manifest_hash: Option<&str>,
    allow_live_writer_handoff: bool,
) -> Result<(ManifestAuthorityHandle, Vec<u8>), JournalError> {
    let metadata = std::fs::symlink_metadata(path).map_err(io_error)?;
    if !metadata.is_file() || metadata.file_type().is_symlink() || is_reparse_point(&metadata) {
        return Err(corrupt(
            0,
            "journal generation manifest is not a regular non-reparse file",
        ));
    }
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(windows)]
    // A live authority denies write/delete sharing. This exact handle is
    // retained through its consumer acknowledgement, so no later process can
    // replace the pathname between proof and projection persistence.
    options.share_mode(0x0000_0001);
    #[cfg(windows)]
    let file = match options.open(path) {
        Ok(file) => file,
        // Only an incremental reporter reaches this fallback, and it has
        // acquired the cross-process reader lease before opening anything. It
        // duplicates the publisher's exact no-delete source handle; opening a
        // second permissively shared pathname handle would become mutable if
        // the publisher process died before projection persistence.
        Err(error) if allow_live_writer_handoff && error.raw_os_error() == Some(32) => {
            let (authority, _) =
                duplicate_live_writer_manifest_authority(root)?.ok_or_else(|| {
                    corrupt(
                        0,
                        "live writer manifest authority cannot be duplicated for the reporter",
                    )
                })?;
            authority.file
        }
        Err(error) => return Err(io_error(error)),
    };
    #[cfg(not(windows))]
    let file = options.open(path).map_err(io_error)?;
    let opened = file.metadata().map_err(io_error)?;
    if !opened.is_file() || is_reparse_point(&opened) {
        return Err(corrupt(
            0,
            "journal generation manifest is not a regular non-reparse file",
        ));
    }
    let identity = journal_file_identity(&file)?;
    let bytes = read_file_bytes(&file)?;
    let digest = manifest_hash(&bytes);
    if expected_manifest_hash.is_some_and(|expected| expected != digest) {
        return Err(corrupt(
            0,
            "journal generation manifest changed while acquiring authority",
        ));
    }
    let named = std::fs::symlink_metadata(path).map_err(io_error)?;
    if !named.is_file() || named.file_type().is_symlink() || is_reparse_point(&named) {
        return Err(corrupt(
            0,
            "journal generation manifest changed while acquiring authority",
        ));
    }
    // The direct open or duplicated source handle omits FILE_SHARE_DELETE and
    // therefore prevents a later swap. Compare the currently named object to
    // the exact handle to detect a replacement which won before acquisition.
    let current = OpenOptions::new().read(true).open(path).map_err(io_error)?;
    if journal_file_identity(&current)? != identity {
        return Err(corrupt(
            0,
            "journal generation manifest path identity changed while acquiring authority",
        ));
    }
    Ok((
        ManifestAuthorityHandle {
            file,
            file_identity: identity,
            manifest_hash: digest,
        },
        bytes,
    ))
}

fn retain_manifest_authority(root: &Path, authority: ManifestAuthorityHandle) {
    #[cfg(windows)]
    {
        retained_manifest_authorities()
            .lock()
            .expect("retained manifest authorities are not poisoned")
            .insert(root.to_path_buf(), authority);
    }
    #[cfg(not(windows))]
    {
        let _ = (root, authority);
    }
}

fn resolve_legacy_generation_location(
    root: &Path,
    manifest_bytes: Vec<u8>,
    manifest: LegacyJournalGenerationManifest,
) -> Result<JournalLocation, JournalError> {
    if manifest.version != LEGACY_GENERATION_MANIFEST_VERSION
        || manifest.generation == 0
        || manifest.worker_id.trim().is_empty()
        || manifest.channel_id.trim().is_empty()
        || manifest.sealed_next_sequence == 0
        || manifest.file_identity.trim().is_empty()
    {
        return Err(corrupt(0, "legacy journal generation manifest is invalid"));
    }
    let data_path = generation_path(root, manifest.generation);
    let file = File::open(&data_path).map_err(|error| {
        corrupt(
            0,
            format!("legacy published journal generation is unavailable: {error}"),
        )
    })?;
    if journal_file_identity(&file)? != manifest.file_identity {
        return Err(corrupt(
            0,
            "legacy published journal generation identity changed",
        ));
    }
    Ok(JournalLocation {
        data_path,
        active_path: None,
        generation: manifest.generation,
        manifest_hash: manifest_hash(&manifest_bytes),
        manifest: None,
        legacy_manifest: Some(manifest),
    })
}

fn publish_generation_manifest(
    root: &Path,
    generation: u64,
    worker_id: &str,
    channel_id: &str,
    sealed: &JournalCursor,
    active: &VerifiedEmptyRegularFile,
) -> Result<ManifestAuthorityHandle, JournalError> {
    let data_path = generation_path(root, generation);
    let data = File::open(&data_path).map_err(io_error)?;
    let active_path = active_segment_path(root, generation);
    if active.path != active_path
        || active.file.metadata().map_err(io_error)?.len() != 0
        || file_sha256_file(&active.file)? != empty_file_sha256()
        || journal_file_identity(&active.file)? != active.identity
    {
        return Err(corrupt(
            0,
            "journal active segment changed before manifest publication",
        ));
    }
    if sealed.generation != 0
        || sealed.byte_offset == 0
        || sealed.raw_sequence == 0
        || !sealed.saw_heartbeat_summary
        || !sealed.saw_compaction_commit
        || sealed.compaction_next_sequence == 0
        || sealed.compaction_layout_version != COMPACTION_LAYOUT_COMMIT_THEN_RETAINED
        || sealed.worker_id.as_deref() != Some(worker_id)
        || sealed.channel_id.as_deref() != Some(channel_id)
    {
        return Err(corrupt(
            0,
            "sealed generation proof does not bind its manifest",
        ));
    }
    let manifest = JournalGenerationManifest {
        version: GENERATION_MANIFEST_VERSION,
        generation,
        worker_id: worker_id.into(),
        channel_id: channel_id.into(),
        sealed_file_identity: journal_file_identity(&data)?,
        // Retain the locked proof handle until the atomic replacement
        // below succeeds. Never re-open the active pathname here.
        active_file_identity: active.identity.clone(),
        sealed_byte_offset: sealed.byte_offset,
        sealed_raw_sequence: sealed.raw_sequence,
        sealed_prefix_hash: sealed.prefix_hash.clone(),
        sealed_compaction_next_sequence: sealed.compaction_next_sequence,
    };
    let temporary = generation_manifest_temp_path(root);
    if temporary.try_exists().map_err(io_error)? {
        // A manifest temp is never authority. Every caller reaches this point
        // only after proving the sealed candidate and durable active segment,
        // so a crash before its rename is recovered by discarding and
        // regenerating this byte-for-byte derived manifest.
        std::fs::remove_file(&temporary).map_err(io_error)?;
    }
    let manifest_bytes = serde_json::to_vec(&manifest).map_err(|error| {
        corrupt(
            0,
            format!("could not encode journal generation manifest: {error}"),
        )
    })?;
    let mut options = OpenOptions::new();
    options.create_new(true).read(true).write(true);
    #[cfg(windows)]
    {
        use windows_sys::Win32::Storage::FileSystem::FILE_SHARE_READ;

        // DELETE access is required for FileRenameInfo below. Deliberately do
        // not share DELETE: the exact source handle becomes the retained
        // no-delete final authority after its handle-based rename.
        const DELETE_ACCESS: u32 = 0x0001_0000;
        const GENERIC_READ: u32 = 0x8000_0000;
        const GENERIC_WRITE: u32 = 0x4000_0000;
        options.access_mode(GENERIC_READ | GENERIC_WRITE | DELETE_ACCESS);
        options.share_mode(FILE_SHARE_READ);
    }
    let mut output = options.open(&temporary).map_err(io_error)?;
    output.write_all(&manifest_bytes).map_err(io_error)?;
    output.flush().map_err(io_error)?;
    output.sync_all().map_err(io_error)?;
    replace_manifest_from_exact_handle(
        output,
        &temporary,
        &generation_manifest_path(root),
        &manifest_bytes,
    )
}

fn file_sha256(path: &Path) -> Result<String, JournalError> {
    let file = File::open(path).map_err(io_error)?;
    file_sha256_file(&file)
}

fn file_sha256_file(file: &File) -> Result<String, JournalError> {
    let mut file = file.try_clone().map_err(io_error)?;
    file.seek(SeekFrom::Start(0)).map_err(io_error)?;
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let read = file.read(&mut buffer).map_err(io_error)?;
        if read == 0 {
            break;
        }
        digest.update(&buffer[..read]);
    }
    Ok(format!("{:x}", digest.finalize()))
}

fn publication_intent(
    generation: u64,
    source_generation: u64,
    snapshot: &JournalSnapshot,
    worker_id: &str,
    channel_id: &str,
    candidate: &Path,
) -> Result<GenerationPublicationIntent, JournalError> {
    let (source_identity_worker_id, source_identity_channel_id) = snapshot
        .identity
        .as_ref()
        .ok_or_else(|| corrupt(0, "compaction source has no journal identity"))?;
    if source_identity_worker_id != worker_id || source_identity_channel_id != channel_id {
        return Err(JournalError::WrongIdentity);
    }
    if snapshot.next_sequence == 0 {
        return Err(corrupt(0, "compaction source has no sequence boundary"));
    }
    Ok(GenerationPublicationIntent {
        version: GENERATION_PUBLICATION_VERSION,
        generation,
        source_generation,
        worker_id: worker_id.into(),
        channel_id: channel_id.into(),
        source_next_sequence: snapshot.next_sequence,
        source_identity_worker_id: source_identity_worker_id.clone(),
        source_identity_channel_id: source_identity_channel_id.clone(),
        candidate_sha256: file_sha256(candidate)?,
    })
}

fn write_generation_publication_intent(
    root: &Path,
    intent: &GenerationPublicationIntent,
) -> Result<(), JournalError> {
    validate_generation_publication_intent(intent, intent.generation)?;
    let published = generation_publication_path(root, intent.generation);
    let temporary = generation_publication_temp_path(root, intent.generation);
    if published.try_exists().map_err(io_error)? || temporary.try_exists().map_err(io_error)? {
        return Err(corrupt(
            0,
            "journal generation publication intent already exists",
        ));
    }
    let mut output = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&temporary)
        .map_err(io_error)?;
    serde_json::to_writer(&mut output, intent).map_err(|error| {
        corrupt(
            0,
            format!("could not encode journal generation publication intent: {error}"),
        )
    })?;
    output.flush().map_err(io_error)?;
    output.sync_all().map_err(io_error)?;
    drop(output);
    // The publication sidecar itself is never authority. Its candidate is
    // re-proven from the final generation pathname (hash, identity, sequence
    // coverage, and source) before any manifest can be emitted, including on
    // crash recovery. Therefore this ordinary durable rename cannot advance
    // journal authority if its temp pathname is replaced.
    promote_generation(&temporary, &published).map_err(io_error)
}

fn read_generation_publication_intent(
    root: &Path,
    generation: u64,
) -> Result<Option<GenerationPublicationIntent>, JournalError> {
    let path = generation_publication_path(root, generation);
    let bytes = match std::fs::read(path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(io_error(error)),
    };
    let intent = parse_generation_publication_intent(&bytes, generation)?;
    Ok(Some(intent))
}

fn parse_generation_publication_intent(
    bytes: &[u8],
    generation: u64,
) -> Result<GenerationPublicationIntent, JournalError> {
    let intent: GenerationPublicationIntent = serde_json::from_slice(bytes).map_err(|error| {
        corrupt(
            0,
            format!("journal generation publication intent is invalid: {error}"),
        )
    })?;
    validate_generation_publication_intent(&intent, generation)?;
    Ok(intent)
}

fn validate_generation_publication_intent(
    intent: &GenerationPublicationIntent,
    generation: u64,
) -> Result<(), JournalError> {
    if intent.version != GENERATION_PUBLICATION_VERSION
        || intent.generation != generation
        || intent.generation == 0
        || intent.source_next_sequence == 0
        || intent.worker_id.trim().is_empty()
        || intent.channel_id.trim().is_empty()
        || intent.source_identity_worker_id != intent.worker_id
        || intent.source_identity_channel_id != intent.channel_id
        || intent.candidate_sha256.len() != 64
        || !intent
            .candidate_sha256
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit())
    {
        return Err(corrupt(
            0,
            "journal generation publication intent is invalid",
        ));
    }
    Ok(())
}

fn remove_generation_publication_intent(root: &Path, generation: u64) -> Result<(), JournalError> {
    let path = generation_publication_path(root, generation);
    match std::fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(()),
        Err(error) => Err(io_error(error)),
    }
}

fn read_generation_publication_temp(
    root: &Path,
    generation: u64,
) -> Result<Option<GenerationPublicationIntent>, JournalError> {
    const MAX_PUBLICATION_INTENT_BYTES: u64 = 4 * 1024;
    let path = generation_publication_temp_path(root, generation);
    let metadata = match std::fs::symlink_metadata(&path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(io_error(error)),
    };
    if !metadata.is_file() || metadata.file_type().is_symlink() || is_reparse_point(&metadata) {
        return Err(corrupt(
            0,
            "journal generation publication temporary is not a regular non-reparse file",
        ));
    }
    if metadata.len() == 0 || metadata.len() > MAX_PUBLICATION_INTENT_BYTES {
        return Err(corrupt(
            0,
            "journal generation publication temporary has an invalid size",
        ));
    }
    let file = File::open(&path).map_err(io_error)?;
    let identity = journal_file_identity(&file)?;
    let bytes = std::fs::read(&path).map_err(io_error)?;
    let reopened = File::open(&path).map_err(io_error)?;
    if journal_file_identity(&reopened)? != identity {
        return Err(corrupt(
            0,
            "journal generation publication temporary changed while reading",
        ));
    }
    parse_generation_publication_intent(&bytes, generation).map(Some)
}

fn validate_generation_publication_intent_source(
    root: &Path,
    location: &JournalLocation,
    intent: &GenerationPublicationIntent,
    worker_id: &str,
    channel_id: &str,
) -> Result<(), JournalError> {
    let expected_generation = location
        .generation
        .checked_add(1)
        .ok_or_else(|| corrupt(0, "journal generation exhausted during recovery"))?;
    if intent.generation != expected_generation
        || intent.source_generation != location.generation
        || intent.worker_id != worker_id
        || intent.channel_id != channel_id
        || intent.source_identity_worker_id != worker_id
        || intent.source_identity_channel_id != channel_id
    {
        return Err(corrupt(
            0,
            "journal generation publication intent does not bind the published source",
        ));
    }
    let current = read_snapshot_strict(root)?;
    if current.next_sequence != intent.source_next_sequence
        || current.identity.as_ref().is_none_or(|identity| {
            identity.0 != intent.source_identity_worker_id
                || identity.1 != intent.source_identity_channel_id
        })
    {
        return Err(corrupt(
            0,
            "journal generation publication source changed before manifest publication",
        ));
    }
    Ok(())
}

#[cfg(windows)]
fn acquire_reader_lease(path: &Path) -> Result<File, JournalError> {
    let mut options = OpenOptions::new();
    options.create(true).read(true).write(true);
    // An exclusive sidecar is a process-visible lease. A writer may continue
    // append-only I/O but must defer path replacement while this handle lives.
    options.share_mode(0);
    options.open(reader_lease_path(path)).map_err(io_error)
}

#[cfg(windows)]
fn reader_lease_is_active(path: &Path) -> Result<bool, JournalError> {
    let lease = reader_lease_path(path);
    let mut options = OpenOptions::new();
    options.create(true).read(true).write(true);
    options.share_mode(0);
    match options.open(&lease) {
        Ok(file) => {
            drop(file);
            let _ = std::fs::remove_file(lease);
            Ok(false)
        }
        Err(error)
            if error.kind() == ErrorKind::PermissionDenied || error.raw_os_error() == Some(32) =>
        {
            Ok(true)
        }
        Err(error) => Err(io_error(error)),
    }
}

/// Claims the short publication gate after a compacted image has been fully
/// prepared. Reporters take the same exclusive sidecar before opening any
/// generation, so either the reporter wins and publication defers, or the
/// writer wins and no reporter can race the old-manifest release/replace
/// boundary. This is a gate, not a long-lived writer lock: it is dropped as
/// soon as the manifest switch is durable.
#[cfg(windows)]
fn try_acquire_publication_gate(path: &Path) -> Result<Option<File>, JournalError> {
    let mut options = OpenOptions::new();
    options.create(true).read(true).write(true);
    options.share_mode(0);
    match options.open(reader_lease_path(path)) {
        Ok(gate) => Ok(Some(gate)),
        Err(error)
            if error.kind() == ErrorKind::PermissionDenied || error.raw_os_error() == Some(32) =>
        {
            Ok(None)
        }
        Err(error) => Err(io_error(error)),
    }
}

#[cfg(not(windows))]
fn try_acquire_publication_gate(_path: &Path) -> Result<Option<File>, JournalError> {
    Ok(None)
}

#[cfg(not(windows))]
fn reader_lease_is_active(path: &Path) -> Result<bool, JournalError> {
    let _ = path;
    Ok(false)
}

#[cfg(test)]
std::thread_local! {
    static STREAM_RECORD_READS: std::cell::Cell<u64> = const { std::cell::Cell::new(0) };
}

#[cfg(test)]
fn reset_stream_record_reads() {
    STREAM_RECORD_READS.with(|reads| reads.set(0));
}

#[cfg(test)]
fn stream_record_reads() -> u64 {
    STREAM_RECORD_READS.with(std::cell::Cell::get)
}

#[cfg(all(test, windows))]
fn forget_verified_prefix_session(path: &Path) {
    release_verified_prefix_session(path);
}

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
    pub fallback_tone_audit: Vec<FallbackToneJournalEntry>,
    pub next_sequence: u64,
}

/// Durable position of a streaming journal consumer. `prefix_hash` is a
/// domain-separated hash chain over every complete record before
/// `byte_offset`. It is deliberately persisted with the file identity and the
/// final covered raw sequence so a restarted reader can prove that it is not
/// resuming in a truncated, replaced, or modified prefix.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct JournalCursor {
    /// The immutable journal generation selected by the root manifest. Zero
    /// denotes the pre-manifest legacy root file during rolling upgrade.
    #[serde(default)]
    pub generation: u64,
    /// Digest of the exact manifest that selected this generation. A cache
    /// hit reads only that small manifest and rejects a changed binding before
    /// reusing its authenticated prefix session.
    #[serde(default)]
    pub manifest_hash: String,
    /// Zero is the immutable sealed generation (or legacy root); one is the
    /// generation-specific append segment following that sealed prefix.
    #[serde(default)]
    pub segment_kind: u8,
    pub byte_offset: u64,
    pub raw_sequence: u64,
    pub prefix_hash: String,
    pub file_identity: String,
    #[serde(default)]
    pub worker_id: Option<String>,
    #[serde(default)]
    pub channel_id: Option<String>,
    /// `next_sequence` declared by the compaction commit in this prefix.
    /// It remains with a cursor inside retained frames so a later same-file
    /// truncation cannot make an authenticated compacted suffix disappear.
    #[serde(default)]
    pub compaction_next_sequence: u64,
    /// Explicit physical compaction layout. Version one placed retained
    /// frames before its seal; version two writes the seal before them so a
    /// bounded reader can persist an authenticated cursor without a suffix
    /// scan.
    #[serde(default)]
    pub compaction_layout_version: u8,
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
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum FallbackToneJournalEntry {
    Activated {
        worker_id: String,
        channel_id: String,
        tone: WorkerFallbackTone,
        activated_at_monotonic_ms: u64,
    },
    Deactivated {
        worker_id: String,
        channel_id: String,
        tone: WorkerFallbackTone,
        reason: FallbackToneDeactivationReason,
        deactivated_at_monotonic_ms: u64,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct CompactionCommit {
    worker_id: String,
    channel_id: String,
    next_sequence: u64,
    #[serde(default)]
    layout_version: Option<u8>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum CompactionLayout {
    RetainedThenCommit,
    CommitThenRetained,
}

struct JournalSnapshot {
    events: Vec<WorkerEventEnvelope>,
    fallback_tone_audit: Vec<FallbackToneJournalEntry>,
    heartbeat_summary: Option<HeartbeatSummary>,
    identity: Option<(String, String)>,
    next_sequence: u64,
    raw_heartbeat_count: usize,
    compaction_committed: bool,
    compaction_layout: Option<CompactionLayout>,
    compaction_layout_is_explicit: bool,
    compaction_next_sequence: u64,
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
    InvalidFallbackToneAudit(String),
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
    /// Keeps the current manifest object from being replaced between one
    /// durable publication and the next. It is released only immediately
    /// before this writer's coordinated next publication.
    manifest_authority: Option<ManifestAuthorityHandle>,
    root_path: PathBuf,
    path: PathBuf,
    generation: u64,
    worker_id: String,
    channel_id: String,
    next_sequence: u64,
    raw_heartbeat_count: usize,
    active_fallback_tones: HashMap<String, (WorkerFallbackTone, u64)>,
}

impl JournalWriter {
    pub fn open(
        path: impl AsRef<Path>,
        worker_id: &str,
        channel_id: &str,
    ) -> Result<Self, JournalError> {
        let root_path = path.as_ref().to_path_buf();
        // Take exclusive, process-owned ownership before recovery or
        // inspection.  Otherwise an old worker and its replacement could both
        // observe the same next sequence and append independently.
        let mut ownership = acquire_writer_ownership(&root_path)?;
        recover_compaction_temp(&root_path, worker_id, channel_id)?;
        // If recovery itself publishes a manifest, retain that exact
        // FileRenameInfo source handle through writer activation. Reopening
        // by pathname here would recreate a publication-to-reopen gap.
        let mut published_manifest_authority =
            recover_orphan_generation(&root_path, worker_id, channel_id)?;
        let (mut location, mut snapshot) =
            if let Some(authority) = published_manifest_authority.as_ref() {
                mirror_manifest_authority_for_resolution(&root_path, authority)?;
                let resolved = (|| {
                    let location = resolve_journal_location_strict(&root_path)?;
                    let snapshot = read_snapshot_strict(&root_path)?;
                    Ok::<_, JournalError>((location, snapshot))
                })();
                clear_mirrored_manifest_authority(&root_path);
                resolved?
            } else {
                (
                    resolve_journal_location_strict(&root_path)?,
                    read_snapshot_strict(&root_path)?,
                )
            };
        if snapshot
            .identity
            .as_ref()
            .is_some_and(|identity| identity.0 != worker_id || identity.1 != channel_id)
        {
            return Err(JournalError::WrongIdentity);
        }
        if location.legacy_manifest.is_some() {
            // A v1 manifest selected a file that was still appendable. The
            // writer is the only process allowed to migrate it: authenticate
            // its exact final prefix, create a distinct empty active segment,
            // then atomically publish v2. Readers must re-derive a retained
            // anchor rather than resume their legacy byte cursor.
            let generation = location.generation;
            let sealed = prove_sealed_generation(&location.data_path, worker_id, channel_id)?;
            let active = create_empty_active_segment(&root_path, generation)?;
            let manifest_authority = publish_generation_manifest(
                &root_path, generation, worker_id, channel_id, &sealed, &active,
            )?;
            published_manifest_authority = Some(manifest_authority);
            drop(active);
            mirror_manifest_authority_for_resolution(
                &root_path,
                published_manifest_authority
                    .as_ref()
                    .expect("published authority is present"),
            )?;
            let refreshed = (|| {
                let location = resolve_journal_location_strict(&root_path)?;
                let snapshot = read_snapshot_strict(&root_path)?;
                Ok::<_, JournalError>((location, snapshot))
            })();
            clear_mirrored_manifest_authority(&root_path);
            (location, snapshot) = refreshed?;
        }
        if snapshot.compaction_layout == Some(CompactionLayout::RetainedThenCommit)
            || (snapshot.compaction_layout == Some(CompactionLayout::CommitThenRetained)
                && !snapshot.compaction_layout_is_explicit)
        {
            let generation = location.generation.max(1);
            let temporary = generation_temp_path(&root_path, generation);
            write_compacted_image(&temporary, &snapshot, worker_id, channel_id)?;
            let intent = publication_intent(
                generation,
                location.generation,
                &snapshot,
                worker_id,
                channel_id,
                &temporary,
            )?;
            write_generation_publication_intent(&root_path, &intent)?;
            promote_generation(&temporary, &generation_path(&root_path, generation))
                .map_err(io_error)?;
            let (_, authority) = complete_generation_publication(
                &root_path, &location, &intent, worker_id, channel_id,
            )?;
            published_manifest_authority = Some(authority);
            mirror_manifest_authority_for_resolution(
                &root_path,
                published_manifest_authority
                    .as_ref()
                    .expect("published authority is present"),
            )?;
            let refreshed = (|| {
                let location = resolve_journal_location_strict(&root_path)?;
                let snapshot = read_snapshot_strict(&root_path)?;
                Ok::<_, JournalError>((location, snapshot))
            })();
            clear_mirrored_manifest_authority(&root_path);
            (location, snapshot) = refreshed?;
        }
        let append_path = location
            .active_path
            .clone()
            .unwrap_or_else(|| location.data_path.clone());
        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&append_path)
            .map_err(io_error)?;
        let manifest_authority = if let Some(authority) = published_manifest_authority {
            if authority.manifest_hash != location.manifest_hash {
                return Err(corrupt(
                    0,
                    "published journal generation manifest changed before writer activation",
                ));
            }
            Some(authority)
        } else if location.manifest.is_some() {
            Some(
                open_manifest_authority_handle(
                    &root_path,
                    &generation_manifest_path(&root_path),
                    Some(&location.manifest_hash),
                    false,
                )?
                .0,
            )
        } else {
            None
        };
        #[cfg(windows)]
        record_writer_manifest_authority(&mut ownership, manifest_authority.as_ref())?;
        Ok(Self {
            file: Some(file),
            ownership: Some(ownership),
            manifest_authority,
            root_path,
            path: append_path,
            generation: location.generation,
            worker_id: worker_id.to_string(),
            channel_id: channel_id.to_string(),
            next_sequence: snapshot.next_sequence,
            raw_heartbeat_count: snapshot.raw_heartbeat_count,
            active_fallback_tones: fallback_tone_audit_state(&snapshot.fallback_tone_audit)
                .map_err(JournalError::InvalidFallbackToneAudit)?,
        })
    }

    pub fn next_sequence(&self) -> u64 {
        self.next_sequence
    }

    /// Durably invalidate the writer sidecar before this writer releases an
    /// authority handle. A shared reporter treats an absent record as a
    /// fail-closed handoff, never as permission to continue with stale proof.
    fn clear_writer_manifest_authority_record(&mut self) -> Result<(), JournalError> {
        #[cfg(windows)]
        {
            let ownership = self.ownership.as_mut().ok_or_else(|| {
                JournalError::Io("journal writer ownership is unavailable".into())
            })?;
            record_writer_manifest_authority(ownership, None)?;
        }
        Ok(())
    }

    /// A failure after the old manifest authority has been released cannot
    /// leave an append-capable writer with an unauthenticated sidecar state.
    /// Closing ownership forces restart recovery to re-prove the published or
    /// orphaned generation before any later append.
    fn retire_after_authority_release_failure(&mut self) {
        let _ = self.clear_writer_manifest_authority_record();
        self.file.take();
        self.manifest_authority.take();
        self.ownership.take();
    }

    /// A reporter acquires the root lease before it opens/proves a manifest
    /// and releases it only after its projection transaction is durable. On
    /// graceful writer retirement, keep the publisher's no-delete handle
    /// alive through that acknowledgement instead of leaving a verified
    /// reporter with only a DELETE-sharing handle.
    fn wait_for_reader_transaction_before_retirement(&self) -> Result<(), JournalError> {
        while reader_lease_is_active(&self.root_path)? {
            std::thread::sleep(std::time::Duration::from_millis(1));
        }
        Ok(())
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
        self.reclaim_retired_generations()?;
        Ok(())
    }

    pub fn append_fallback_tone_audit(
        &mut self,
        entry: &FallbackToneJournalEntry,
    ) -> Result<(), JournalError> {
        let (worker_id, channel_id) = fallback_tone_audit_identity(entry);
        if worker_id != self.worker_id || channel_id != self.channel_id {
            return Err(JournalError::WrongIdentity);
        }
        let mut next_active = self.active_fallback_tones.clone();
        apply_fallback_tone_audit_entry(entry, &mut next_active)
            .map_err(JournalError::InvalidFallbackToneAudit)?;
        let payload = serde_json::to_vec(entry)
            .map_err(|error| JournalError::Io(format!("serialize fallback tone audit: {error}")))?;
        let file = self.file.as_mut().ok_or_else(|| {
            JournalError::Io("journal writer is unavailable after replacement failure".into())
        })?;
        write_record(file, RECORD_KIND_FALLBACK_TONE_AUDIT, &payload)?;
        file.flush().map_err(io_error)?;
        file.sync_data().map_err(io_error)?;
        self.active_fallback_tones = next_active;
        Ok(())
    }

    /// Reconciles a tone that was durably activated by a prior worker but did
    /// not reach its deactivation record before that worker retired. A fresh
    /// media graph starts silent, so this closes the durable state before the
    /// replacement worker can report readiness. The prior worker's monotonic
    /// clock is not comparable to the new process; using the activation mark
    /// for the recovery record preserves the audit ordering without inventing
    /// a cross-process timestamp.
    pub fn recover_active_fallback_tones(
        &mut self,
    ) -> Result<Vec<(WorkerFallbackTone, u64)>, JournalError> {
        let active = self
            .active_fallback_tones
            .values()
            .map(|(tone, activated_at_monotonic_ms)| (tone.clone(), *activated_at_monotonic_ms))
            .collect::<Vec<_>>();
        let worker_id = self.worker_id.clone();
        let channel_id = self.channel_id.clone();
        for (tone, activated_at_monotonic_ms) in &active {
            self.append_fallback_tone_audit(&FallbackToneJournalEntry::Deactivated {
                worker_id: worker_id.clone(),
                channel_id: channel_id.clone(),
                tone: tone.clone(),
                reason: FallbackToneDeactivationReason::Recovery,
                deactivated_at_monotonic_ms: *activated_at_monotonic_ms,
            })?;
        }
        Ok(active)
    }

    fn compact_heartbeats(&mut self) -> Result<(), JournalError> {
        self.compact_heartbeats_with(promote_generation)
    }

    fn compact_heartbeats_with(
        &mut self,
        replacer: impl FnOnce(&Path, &Path) -> std::io::Result<()>,
    ) -> Result<(), JournalError> {
        // The root lease spans one reporter's authenticated read through its
        // projection transaction. Deferring publication here is safe: this
        // writer keeps appending its current generation and retries after the
        // reporter acknowledges. Unlike path replacement, a published new
        // generation never touches a reader's old locked object.
        if reader_lease_is_active(&self.root_path)? {
            return Ok(());
        }
        // Retry any post-manifest intent retirement (and reject a tampered
        // one) before preparing another generation. This prevents a harmless
        // cleanup sharing race from becoming a stale sidecar after a later
        // successful compaction.
        let (location, snapshot) = if let Some(authority) = self.manifest_authority.as_ref() {
            mirror_manifest_authority_for_resolution(&self.root_path, authority)?;
            let resolved = (|| {
                let _ =
                    recover_orphan_generation(&self.root_path, &self.worker_id, &self.channel_id)?;
                let location = resolve_journal_location_strict(&self.root_path)?;
                let snapshot = read_snapshot_strict(&self.root_path)?;
                Ok::<_, JournalError>((location, snapshot))
            })();
            clear_mirrored_manifest_authority(&self.root_path);
            resolved?
        } else {
            let _ = recover_orphan_generation(&self.root_path, &self.worker_id, &self.channel_id)?;
            let location = resolve_journal_location_strict(&self.root_path)?;
            let snapshot = read_snapshot_strict(&self.root_path)?;
            (location, snapshot)
        };
        if location.generation != self.generation {
            return Err(corrupt(
                0,
                "journal generation changed while its writer was active",
            ));
        }
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
        let fallback_tone_audit = snapshot.fallback_tone_audit.clone();
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

        let next_generation = self
            .generation
            .checked_add(1)
            .ok_or_else(|| corrupt(0, "journal generation exhausted"))?;
        let temporary = generation_temp_path(&self.root_path, next_generation);
        if temporary.try_exists().map_err(io_error)? {
            std::fs::remove_file(&temporary).map_err(io_error)?;
        }
        let compact_source = JournalSnapshot {
            events: retained,
            fallback_tone_audit,
            heartbeat_summary: Some(summary),
            identity: snapshot.identity.clone(),
            next_sequence: snapshot.next_sequence,
            raw_heartbeat_count: RETAIN_RAW_HEARTBEATS,
            compaction_committed: true,
            compaction_layout: Some(CompactionLayout::CommitThenRetained),
            compaction_layout_is_explicit: true,
            compaction_next_sequence: snapshot.next_sequence,
        };
        write_compacted_image(
            &temporary,
            &compact_source,
            &self.worker_id,
            &self.channel_id,
        )?;

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

        // A reporter may have started after the initial check while this
        // candidate generation was being prepared. Claim the same exclusive
        // sidecar the reporter acquires before resolution, so the old
        // manifest authority cannot be released and replaced in the gap
        // between a best-effort active-lease observation and publication.
        // If the reporter won, keep the verified temp private and retry on a
        // later heartbeat without turning an ordinary sharing race fatal.
        let _publication_gate = match try_acquire_publication_gate(&self.root_path)? {
            Some(gate) => gate,
            None => {
                let _ = std::fs::remove_file(&temporary);
                return Ok(());
            }
        };

        let intent = publication_intent(
            next_generation,
            self.generation,
            &compact_source,
            &self.worker_id,
            &self.channel_id,
            &temporary,
        )?;
        write_generation_publication_intent(&self.root_path, &intent)?;

        let generation_path = generation_path(&self.root_path, next_generation);
        if let Err(error) = replacer(&temporary, &generation_path) {
            // This is an in-process publication failure, not a crash. Keep
            // the diagnostic candidate (the historical behavior) but remove
            // its intent so a later append cannot make stale source bytes
            // look recoverable on restart.
            remove_generation_publication_intent(&self.root_path, next_generation)?;
            return Err(io_error(error));
        }
        // The manifest is the sole authority publication point. The durable
        // intent makes each preceding rename recoverable after a crash.
        // First durably invalidate the old sidecar, then release the old
        // no-delete handle. A reporter that interleaves here fails closed
        // rather than validating a stale sidecar against a mutable pathname.
        if let Err(error) = self.clear_writer_manifest_authority_record() {
            self.retire_after_authority_release_failure();
            return Err(error);
        }
        drop(self.manifest_authority.take());
        if let Err(error) = fail_after_manifest_authority_release(&self.root_path) {
            self.retire_after_authority_release_failure();
            return Err(error);
        }
        let (next_active, manifest_authority) = match complete_generation_publication(
            &self.root_path,
            &location,
            &intent,
            &self.worker_id,
            &self.channel_id,
        ) {
            Ok(published) => published,
            Err(error) => {
                self.retire_after_authority_release_failure();
                return Err(error);
            }
        };
        let previous_generation = self.generation;
        let previous_path = self.path.clone();
        self.manifest_authority = Some(manifest_authority);
        #[cfg(windows)]
        if let Err(error) = record_writer_manifest_authority(
            self.ownership.as_mut().ok_or_else(|| {
                JournalError::Io(
                    "journal writer ownership is unavailable during publication".into(),
                )
            })?,
            self.manifest_authority.as_ref(),
        ) {
            self.retire_after_authority_release_failure();
            return Err(error);
        }
        let next_file = match OpenOptions::new().append(true).open(&next_active) {
            Ok(file) => file,
            Err(error) => {
                self.retire_after_authority_release_failure();
                return Err(io_error(error));
            }
        };
        self.file.take();
        self.file = Some(next_file);
        self.path = next_active;
        self.generation = next_generation;
        self.raw_heartbeat_count = compacted.raw_heartbeat_count;
        // A reporter may acquire its lease in the tiny interval before the
        // manifest switch. Retire old objects only after a subsequent lease
        // observation says its validate/read/persist/ack transaction ended.
        // The retry in `append` makes this eventual without making a writer
        // fatal on an ordinary sharing race.
        if previous_generation > 0 {
            self.reclaim_retired_generations()?;
        } else if !reader_lease_is_active(&self.root_path)? {
            let _ = std::fs::remove_file(previous_path);
        }
        Ok(())
    }

    fn reclaim_retired_generations(&self) -> Result<(), JournalError> {
        if self.generation == 0 || reader_lease_is_active(&self.root_path)? {
            return Ok(());
        }
        for retired in 1..self.generation {
            let _ = std::fs::remove_file(generation_path(&self.root_path, retired));
            let _ = std::fs::remove_file(active_segment_path(&self.root_path, retired));
        }
        // Generation zero is the original root file. Once a manifest is
        // authoritative it is another retired object and may be reclaimed
        // only after the reader lease that could still name it has ended.
        let _ = std::fs::remove_file(&self.root_path);
        Ok(())
    }
}

fn write_compacted_image(
    temporary: &Path,
    snapshot: &JournalSnapshot,
    worker_id: &str,
    channel_id: &str,
) -> Result<(), JournalError> {
    if snapshot.compaction_committed && snapshot.compaction_next_sequence == 0 {
        return Err(corrupt(
            0,
            "compacted source has an invalid committed sequence boundary",
        ));
    }
    if temporary.try_exists().map_err(io_error)? {
        std::fs::remove_file(temporary).map_err(io_error)?;
    }
    let summary = snapshot
        .heartbeat_summary
        .as_ref()
        .ok_or_else(|| corrupt(0, "compacted image has no heartbeat summary"))?;
    let mut output = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(temporary)
        .map_err(io_error)?;
    for summary_payload in encode_summary_chunks(summary)? {
        write_record(&mut output, RECORD_KIND_HEARTBEAT_SUMMARY, &summary_payload)?;
    }
    let commit_payload = serde_json::to_vec(&CompactionCommit {
        worker_id: worker_id.into(),
        channel_id: channel_id.into(),
        next_sequence: snapshot.next_sequence,
        layout_version: Some(COMPACTION_LAYOUT_COMMIT_THEN_RETAINED),
    })
    .map_err(|error| corrupt(0, format!("could not encode compaction commit: {error}")))?;
    write_record(&mut output, RECORD_KIND_COMPACTION_COMMIT, &commit_payload)?;
    for event in &snapshot.events {
        let payload = encode_event_frame(event).map_err(JournalError::Protocol)?;
        write_record(&mut output, RECORD_KIND_EVENT, &payload)?;
    }
    for entry in &snapshot.fallback_tone_audit {
        let payload = serde_json::to_vec(entry).map_err(|error| {
            corrupt(0, format!("could not encode fallback tone audit: {error}"))
        })?;
        write_record(&mut output, RECORD_KIND_FALLBACK_TONE_AUDIT, &payload)?;
    }
    output.flush().map_err(io_error)?;
    output.sync_all().map_err(io_error)?;
    drop(output);
    let compacted = read_snapshot(temporary)?;
    if compacted.next_sequence != snapshot.next_sequence
        || compacted.identity != snapshot.identity
        || !compacted.compaction_committed
        || compacted.compaction_layout != Some(CompactionLayout::CommitThenRetained)
        || !compacted.compaction_layout_is_explicit
    {
        return Err(corrupt(
            0,
            "compacted journal did not preserve its contract",
        ));
    }
    Ok(())
}

fn promote_generation(source: &Path, destination: &Path) -> std::io::Result<()> {
    if destination.try_exists()? {
        return Err(std::io::Error::new(
            ErrorKind::AlreadyExists,
            "journal generation already exists",
        ));
    }
    promote_file_write_through(source, destination)
}

#[cfg(windows)]
fn promote_file_write_through(source: &Path, destination: &Path) -> std::io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{MOVEFILE_WRITE_THROUGH, MoveFileExW};

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
    // SAFETY: both vectors contain valid NUL-terminated UTF-16 paths. Without
    // REPLACE_EXISTING, a raced destination is rejected rather than replaced.
    let status = unsafe {
        MoveFileExW(
            source.as_ptr(),
            destination.as_ptr(),
            MOVEFILE_WRITE_THROUGH,
        )
    };
    if status == 0 {
        Err(std::io::Error::last_os_error())
    } else {
        Ok(())
    }
}

#[cfg(not(windows))]
fn promote_file_write_through(source: &Path, destination: &Path) -> std::io::Result<()> {
    std::fs::rename(source, destination)
}

#[cfg(windows)]
fn is_reparse_point(metadata: &std::fs::Metadata) -> bool {
    use std::os::windows::fs::MetadataExt;
    const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
    metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
}

#[cfg(not(windows))]
fn is_reparse_point(_metadata: &std::fs::Metadata) -> bool {
    false
}

/// A zero-share handle is the proof token for an empty active segment.  The
/// handle's FileId binds the object we examined, and Windows refuses a second
/// writer or a delete/rename while it lives.  We intentionally retain it from
/// the final post-rename proof through manifest publication.
struct VerifiedEmptyRegularFile {
    path: PathBuf,
    file: File,
    identity: String,
}

fn open_verified_empty_regular_file(
    path: &Path,
    description: &str,
    expected_identity: Option<&str>,
) -> Result<VerifiedEmptyRegularFile, JournalError> {
    let metadata = std::fs::symlink_metadata(path).map_err(io_error)?;
    if !metadata.is_file() || metadata.file_type().is_symlink() || is_reparse_point(&metadata) {
        return Err(corrupt(
            0,
            format!("{description} is not a regular non-reparse file"),
        ));
    }
    let mut options = OpenOptions::new();
    options.read(true).write(true);
    #[cfg(windows)]
    // This is a real cross-process exclusion, not a metadata assertion: no
    // process can write, delete, or rename the file object while its exact
    // contents and FileId are authenticated for authority publication.
    options.share_mode(0);
    let file = options.open(path).map_err(io_error)?;
    let opened_metadata = file.metadata().map_err(io_error)?;
    if !opened_metadata.is_file() || is_reparse_point(&opened_metadata) {
        return Err(corrupt(
            0,
            format!("{description} is not a regular non-reparse file"),
        ));
    }
    if opened_metadata.len() != 0 || file_sha256_file(&file)? != empty_file_sha256() {
        return Err(corrupt(0, format!("{description} is not exactly empty")));
    }
    let identity = journal_file_identity(&file)?;
    if expected_identity.is_some_and(|expected| expected != identity) {
        return Err(corrupt(
            0,
            format!("{description} changed before manifest publication"),
        ));
    }
    file.sync_all().map_err(io_error)?;

    // The zero-share handle excludes replacement after it opened. Check the
    // name while that exclusion is live as well, so an indirection that raced
    // just before open cannot make a regular target look like a valid active
    // segment. The retained handle, not this pathname metadata, is what is
    // carried into the manifest proof.
    let metadata = std::fs::symlink_metadata(path).map_err(io_error)?;
    if !metadata.is_file() || metadata.file_type().is_symlink() || is_reparse_point(&metadata) {
        return Err(corrupt(
            0,
            format!("{description} changed before manifest publication"),
        ));
    }
    Ok(VerifiedEmptyRegularFile {
        path: path.to_path_buf(),
        file,
        identity,
    })
}

fn empty_file_sha256() -> String {
    format!("{:x}", Sha256::digest([]))
}

/// Deterministic test seam for the otherwise tiny validation-to-rename race.
/// Production builds have no hook and never execute a path replacement here.
#[cfg(test)]
fn active_temp_promotion_replacement()
-> &'static std::sync::Mutex<std::collections::HashMap<PathBuf, PathBuf>> {
    use std::sync::{Mutex, OnceLock};

    static REPLACEMENTS: OnceLock<Mutex<std::collections::HashMap<PathBuf, PathBuf>>> =
        OnceLock::new();
    REPLACEMENTS.get_or_init(|| Mutex::new(std::collections::HashMap::new()))
}

#[cfg(test)]
fn replace_active_temp_at_promotion_boundary(path: &Path) {
    let mut replacements = active_temp_promotion_replacement()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let Some(source) = replacements.remove(path) else {
        return;
    };
    std::fs::remove_file(path).expect("test active temp exists before replacement");
    std::fs::rename(source, path).expect("test active temp replacement succeeds");
}

#[cfg(test)]
fn schedule_active_temp_replacement(path: PathBuf, replacement: PathBuf) {
    let mut scheduled = active_temp_promotion_replacement()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    assert!(
        scheduled.insert(path, replacement).is_none(),
        "one active temp replacement is already scheduled for this path"
    );
}

#[cfg(not(test))]
fn replace_active_temp_at_promotion_boundary(_path: &Path) {}

/// Test-only seam for proving that the manifest rename uses its retained file
/// handle rather than resolving the temporary pathname a second time.
#[cfg(test)]
fn manifest_temp_promotion_replacement()
-> &'static std::sync::Mutex<std::collections::HashMap<PathBuf, PathBuf>> {
    use std::sync::{Mutex, OnceLock};

    static REPLACEMENTS: OnceLock<Mutex<std::collections::HashMap<PathBuf, PathBuf>>> =
        OnceLock::new();
    REPLACEMENTS.get_or_init(|| Mutex::new(std::collections::HashMap::new()))
}

#[cfg(test)]
fn replace_manifest_temp_at_promotion_boundary(path: &Path) {
    let mut replacements = manifest_temp_promotion_replacement()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let Some(source) = replacements.remove(path) else {
        return;
    };
    assert_manifest_replacement_is_blocked(&source, path);
}

#[cfg(test)]
fn schedule_manifest_temp_replacement(path: PathBuf, replacement: PathBuf) {
    let mut scheduled = manifest_temp_promotion_replacement()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    assert!(
        scheduled.insert(path, replacement).is_none(),
        "one manifest temp replacement is already scheduled for this path"
    );
}

#[cfg(not(test))]
fn replace_manifest_temp_at_promotion_boundary(_path: &Path) {}

/// Deterministic seam for the critical retirement interleaving: a reporter
/// has already completed its read/initial verification and attempts the
/// acknowledgement proof after the writer invalidates its sidecar but before
/// the writer releases the manifest authority handle.
#[cfg(test)]
fn writer_authority_retirement_interleavings()
-> &'static std::sync::Mutex<std::collections::HashMap<PathBuf, JournalCursor>> {
    use std::sync::{Mutex, OnceLock};

    static INTERLEAVINGS: OnceLock<Mutex<std::collections::HashMap<PathBuf, JournalCursor>>> =
        OnceLock::new();
    INTERLEAVINGS.get_or_init(|| Mutex::new(std::collections::HashMap::new()))
}

#[cfg(test)]
fn schedule_writer_authority_retirement_interleaving(path: PathBuf, cursor: JournalCursor) {
    let previous = writer_authority_retirement_interleavings()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .insert(path, cursor);
    assert!(
        previous.is_none(),
        "one writer retirement interleaving is pending"
    );
}

#[cfg(test)]
fn observe_writer_authority_retirement_interleaving(path: &Path) {
    let cursor = writer_authority_retirement_interleavings()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .remove(path);
    if let Some(cursor) = cursor {
        #[cfg(windows)]
        if let Some((manifest_hash, manifest_identity)) = retained_manifest_authority_binding(path)
        {
            assert!(
                !writer_owns_matching_manifest_authority(
                    path,
                    Some(&manifest_hash),
                    Some(&manifest_identity),
                )
                .unwrap(),
                "the sidecar was not invalidated before authority release"
            );
        }
        assert!(
            verify_incremental_reader_authority(path, &cursor).is_err(),
            "the acknowledgement proof advanced through a retired writer handoff"
        );
    }
}

#[cfg(not(test))]
fn observe_writer_authority_retirement_interleaving(_path: &Path) {}

/// Fault injection for a failure after the old authority is deliberately
/// released but before the new manifest is published. The writer must become
/// terminal so restart recovery, not a later append, re-establishes trust.
#[cfg(test)]
fn post_authority_release_failures() -> &'static std::sync::Mutex<std::collections::HashSet<PathBuf>>
{
    use std::sync::{Mutex, OnceLock};

    static FAILURES: OnceLock<Mutex<std::collections::HashSet<PathBuf>>> = OnceLock::new();
    FAILURES.get_or_init(|| Mutex::new(std::collections::HashSet::new()))
}

#[cfg(test)]
fn schedule_post_authority_release_failure(path: PathBuf) {
    assert!(
        post_authority_release_failures()
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .insert(path),
        "one post-authority-release failure is already scheduled"
    );
}

#[cfg(test)]
fn fail_after_manifest_authority_release(path: &Path) -> Result<(), JournalError> {
    if post_authority_release_failures()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .remove(path)
    {
        return Err(corrupt(
            0,
            "injected failure after journal manifest authority release",
        ));
    }
    Ok(())
}

#[cfg(not(test))]
fn fail_after_manifest_authority_release(_path: &Path) -> Result<(), JournalError> {
    Ok(())
}

#[cfg(test)]
fn manifest_authority_replacement_attempts()
-> &'static std::sync::Mutex<std::collections::HashMap<PathBuf, Vec<PathBuf>>> {
    use std::sync::{Mutex, OnceLock};

    static ATTEMPTS: OnceLock<Mutex<std::collections::HashMap<PathBuf, Vec<PathBuf>>>> =
        OnceLock::new();
    ATTEMPTS.get_or_init(|| Mutex::new(std::collections::HashMap::new()))
}

#[cfg(test)]
const MANIFEST_AUTHORITY_SWAP_CHILD: &str = "TOWNLIGHT_JOURNAL_MANIFEST_AUTHORITY_SWAP_CHILD";
#[cfg(test)]
const MANIFEST_AUTHORITY_SWAP_SOURCE: &str = "TOWNLIGHT_JOURNAL_MANIFEST_AUTHORITY_SWAP_SOURCE";
#[cfg(test)]
const MANIFEST_AUTHORITY_SWAP_DESTINATION: &str =
    "TOWNLIGHT_JOURNAL_MANIFEST_AUTHORITY_SWAP_DESTINATION";

#[cfg(test)]
fn schedule_manifest_authority_replacement(path: PathBuf, replacement: PathBuf) {
    manifest_authority_replacement_attempts()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .entry(path)
        .or_default()
        .push(replacement);
}

#[cfg(test)]
fn assert_manifest_replacement_is_blocked_locally(source: &Path, destination: &Path) {
    let error = replace_file(source, destination)
        .expect_err("a locked manifest authority handle must reject replacement");
    assert!(
        error.kind() == ErrorKind::PermissionDenied || error.raw_os_error() == Some(32),
        "manifest replacement failed for an unexpected reason: {error}"
    );
    assert!(
        source.exists(),
        "a rejected manifest replacement keeps its source"
    );
}

/// The authority path must resist a raw replacement from a separate process,
/// not merely another handle in this test process. The child runs the exact
/// same named test in its small early-return branch below.
#[cfg(all(test, windows))]
fn assert_manifest_replacement_is_blocked(source: &Path, destination: &Path) {
    let status = std::process::Command::new(std::env::current_exe().expect("test executable"))
        .arg("--exact")
        .arg("tests::writer_locks_manifest_authority_across_every_promotion_boundary")
        .env(MANIFEST_AUTHORITY_SWAP_CHILD, "1")
        .env(MANIFEST_AUTHORITY_SWAP_SOURCE, source)
        .env(MANIFEST_AUTHORITY_SWAP_DESTINATION, destination)
        .status()
        .expect("manifest replacement child starts");
    assert!(
        status.success(),
        "a separate process unexpectedly replaced a live manifest authority"
    );
    assert!(
        source.exists(),
        "a rejected manifest replacement keeps its source"
    );
}

#[cfg(all(test, not(windows)))]
fn assert_manifest_replacement_is_blocked(source: &Path, destination: &Path) {
    assert_manifest_replacement_is_blocked_locally(source, destination);
}

#[cfg(test)]
fn reject_manifest_replacement_while_authoritative(path: &Path) {
    let source = manifest_authority_replacement_attempts()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .get_mut(path)
        .and_then(Vec::pop);
    if let Some(source) = source {
        assert_manifest_replacement_is_blocked(&source, path);
    }
}

#[cfg(not(test))]
fn reject_manifest_replacement_while_authoritative(_path: &Path) {}

fn promote_verified_empty_active_segment(
    temporary: &Path,
    active: &Path,
) -> Result<VerifiedEmptyRegularFile, JournalError> {
    // Windows cannot rename a source while an exclusive proof handle is
    // retained. Authenticate the temp, release only that source handle for
    // the write-through move, then prove the final name against the exact
    // FileId before it can be used as manifest authority. This closes the
    // validation-to-promotion pathname race without weakening write/delete
    // exclusion during either proof.
    let temporary_proof =
        open_verified_empty_regular_file(temporary, "journal active segment temporary", None)?;
    let expected_identity = temporary_proof.identity.clone();
    drop(temporary_proof);
    replace_active_temp_at_promotion_boundary(temporary);
    promote_generation(temporary, active).map_err(io_error)?;
    open_verified_empty_regular_file(active, "journal active segment", Some(&expected_identity))
}

fn create_empty_active_segment(
    root: &Path,
    generation: u64,
) -> Result<VerifiedEmptyRegularFile, JournalError> {
    let active = active_segment_path(root, generation);
    if active.try_exists().map_err(io_error)? {
        return Err(corrupt(0, "journal active segment already exists"));
    }
    let temporary = active_segment_temp_path(root, generation);
    if temporary.try_exists().map_err(io_error)? {
        // This is the crash point after the empty active contents are durable
        // but before the write-through rename. The path is generation-derived
        // and the intent/sealed proof was already validated by the caller.
        // Promote only an exact, synced empty regular file; nonempty, reparse,
        // or swapped artifacts remain fail-closed and are never overwritten.
        return promote_verified_empty_active_segment(&temporary, &active);
    }
    let file = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&temporary)
        .map_err(io_error)?;
    file.sync_all().map_err(io_error)?;
    drop(file);
    promote_verified_empty_active_segment(&temporary, &active)
}

fn prove_sealed_generation(
    path: &Path,
    worker_id: &str,
    channel_id: &str,
) -> Result<JournalCursor, JournalError> {
    let proof = read_journal_incremental_single(
        JournalReadSegment {
            root: path,
            data_path: path,
            generation: 0,
            manifest_hash: "",
            segment_kind: 0,
        },
        None,
        usize::MAX,
        false,
    )?
    .cursor;
    if proof.worker_id.as_deref() != Some(worker_id)
        || proof.channel_id.as_deref() != Some(channel_id)
        || !proof.saw_heartbeat_summary
        || !proof.saw_compaction_commit
        || proof.compaction_next_sequence == 0
        || proof.compaction_layout_version != COMPACTION_LAYOUT_COMMIT_THEN_RETAINED
    {
        return Err(corrupt(0, "sealed generation proof is incomplete"));
    }
    Ok(proof)
}

impl Drop for JournalWriter {
    fn drop(&mut self) {
        // Close the mutable generation first. The root ownership handle is
        // separate, so publication can switch generations without releasing
        // writer authority; it is released only when this writer retires.
        self.file.take();
        // Persist an empty sidecar before retiring the corresponding
        // no-delete authority. A shared reporter can therefore never observe
        // the old record after its handle is gone.
        if self.clear_writer_manifest_authority_record().is_err() {
            // We cannot report a Drop failure. Keep both durable authorities
            // alive in this process rather than opening a stale-sidecar gap;
            // normal process exit releases them together.
            if let Some(authority) = self.manifest_authority.take() {
                std::mem::forget(authority);
            }
            if let Some(ownership) = self.ownership.take() {
                std::mem::forget(ownership);
            }
            return;
        }
        observe_writer_authority_retirement_interleaving(&self.root_path);
        if self
            .wait_for_reader_transaction_before_retirement()
            .is_err()
        {
            // As above, an unobservable retirement failure must retain both
            // handles rather than opening a path-replacement window.
            if let Some(authority) = self.manifest_authority.take() {
                std::mem::forget(authority);
            }
            if let Some(ownership) = self.ownership.take() {
                std::mem::forget(ownership);
            }
            return;
        }
        self.manifest_authority.take();
        self.ownership.take();
    }
}

/// A separate root sidecar owns the writer identity across immutable
/// generation publication. Releasing an in-file lock during a switch would
/// create a window for a second worker to append independently.
struct JournalWriterOwnership {
    lock_handle: Option<File>,
    lock_path: PathBuf,
}

#[derive(Serialize, Deserialize)]
struct WriterManifestAuthority {
    manifest_hash: String,
    file_identity: String,
    /// The live writer process that owns `manifest_handle`. The reporter
    /// duplicates this exact handle rather than reopening the pathname with
    /// permissive DELETE sharing.
    process_id: u32,
    manifest_handle: u64,
}

impl Drop for JournalWriterOwnership {
    fn drop(&mut self) {
        // On Windows peers may inspect this read-shared status handle, but no
        // peer can acquire WRITE or DELETE access until it closes. If a
        // replacement wins the tiny close/remove race, its ownership handle
        // makes this remove fail harmlessly.
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
    // Only diagnostics/reporters may read this lock. No peer can obtain write
    // or delete access while the current writer owns it; the OS releases that
    // exclusion on process death.
    options.share_mode(0x0000_0001);
    let mut lock_handle = options.open(&lock_path).map_err(|error| {
        // ERROR_SHARING_VIOLATION (32) normally maps to PermissionDenied, but
        // retain the raw check for Windows runtimes that preserve it as a more
        // generic I/O kind.
        if error.kind() == ErrorKind::PermissionDenied || error.raw_os_error() == Some(32) {
            JournalError::WriterAlreadyActive
        } else {
            io_error(error)
        }
    })?;
    lock_handle.set_len(0).map_err(io_error)?;
    lock_handle.seek(SeekFrom::Start(0)).map_err(io_error)?;
    lock_handle.sync_all().map_err(io_error)?;
    Ok(JournalWriterOwnership {
        lock_handle: Some(lock_handle),
        lock_path,
    })
}

#[cfg(windows)]
fn record_writer_manifest_authority(
    ownership: &mut JournalWriterOwnership,
    authority: Option<&ManifestAuthorityHandle>,
) -> Result<(), JournalError> {
    let handle = ownership
        .lock_handle
        .as_mut()
        .ok_or_else(|| JournalError::Io("journal writer ownership is unavailable".into()))?;
    handle.set_len(0).map_err(io_error)?;
    handle.seek(SeekFrom::Start(0)).map_err(io_error)?;
    if let Some(authority) = authority {
        serde_json::to_writer(
            &mut *handle,
            &WriterManifestAuthority {
                manifest_hash: authority.manifest_hash.clone(),
                file_identity: authority.file_identity.clone(),
                // The writer lock is process-owned, so the PID/handle pair
                // is meaningful only while its exclusive status handle still
                // proves this writer live. A reporter validates both the
                // duplicated FileId and content digest before retaining it.
                process_id: unsafe { windows_sys::Win32::System::Threading::GetCurrentProcessId() },
                manifest_handle: authority.file.as_raw_handle() as usize as u64,
            },
        )
        .map_err(|error| {
            corrupt(
                0,
                format!("could not encode writer manifest authority: {error}"),
            )
        })?;
    }
    handle.flush().map_err(io_error)?;
    handle.sync_all().map_err(io_error)
}

/// Reads one writer-owned manifest binding only while the writer's exclusive
/// ownership sidecar remains live. The record carries a process-local handle
/// value, so it is never authority by itself: callers must duplicate and
/// revalidate the referenced file object before using it.
#[cfg(windows)]
fn live_writer_manifest_authority(
    root: &Path,
) -> Result<Option<WriterManifestAuthority>, JournalError> {
    let lock_path = writer_lock_path(root);
    let mut exclusive = OpenOptions::new();
    exclusive.read(true).write(true);
    exclusive.share_mode(0);
    match exclusive.open(&lock_path) {
        Ok(file) => {
            drop(file);
            return Ok(None);
        }
        Err(error) if error.raw_os_error() == Some(32) => {}
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(io_error(error)),
    }
    let mut reader = OpenOptions::new();
    reader.read(true);
    reader.share_mode(0x0000_0001 | 0x0000_0002 | 0x0000_0004);
    let mut reader = reader.open(&lock_path).map_err(io_error)?;
    let mut bytes = Vec::new();
    reader.read_to_end(&mut bytes).map_err(io_error)?;
    let Ok(authority) = serde_json::from_slice::<WriterManifestAuthority>(&bytes) else {
        return Ok(None);
    };
    if authority.manifest_hash.is_empty()
        || authority.file_identity.is_empty()
        || authority.process_id == 0
        || authority.manifest_handle == 0
    {
        return Ok(None);
    }
    let mut still_exclusive = OpenOptions::new();
    still_exclusive.read(true).write(true);
    still_exclusive.share_mode(0);
    match still_exclusive.open(&lock_path) {
        Ok(file) => {
            drop(file);
            Ok(None)
        }
        Err(error) if error.raw_os_error() == Some(32) => Ok(Some(authority)),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(None),
        Err(error) => Err(io_error(error)),
    }
}

/// Duplicates the writer's original manifest handle into the reporter. A
/// duplicate references the same Windows file object, retaining its omitted
/// FILE_SHARE_DELETE even if the writer process terminates immediately after
/// this call. The sidecar merely locates the handle; the FileId and digest
/// checks below bind it to the manifest selected by the reader.
#[cfg(windows)]
fn duplicate_live_writer_manifest_authority(
    root: &Path,
) -> Result<Option<(ManifestAuthorityHandle, Vec<u8>)>, JournalError> {
    use windows_sys::Win32::{
        Foundation::{CloseHandle, DUPLICATE_SAME_ACCESS, DuplicateHandle},
        System::Threading::{GetCurrentProcess, OpenProcess, PROCESS_DUP_HANDLE},
    };

    let Some(record) = live_writer_manifest_authority(root)? else {
        return Ok(None);
    };
    // SAFETY: `record.process_id` came from the still-live writer ownership
    // record above. Failure is fail-closed; successful acquisition is closed
    // below after the duplicate has been made.
    let source_process = unsafe { OpenProcess(PROCESS_DUP_HANDLE, 0, record.process_id) };
    if source_process.is_null() {
        return Err(corrupt(
            0,
            "live writer process cannot provide a durable manifest authority duplicate",
        ));
    }
    let mut duplicated = std::ptr::null_mut();
    // SAFETY: both process handles are valid for this call and the source
    // handle value is meaningful only in `source_process`. DUPLICATE_SAME_ACCESS
    // preserves the original no-delete file object's access/share state.
    let copied = unsafe {
        DuplicateHandle(
            source_process,
            record.manifest_handle as usize as isize as _,
            GetCurrentProcess(),
            &mut duplicated,
            0,
            0,
            DUPLICATE_SAME_ACCESS,
        )
    };
    // SAFETY: `source_process` is no longer needed after DuplicateHandle.
    unsafe { CloseHandle(source_process) };
    if copied == 0 || duplicated.is_null() {
        return Err(corrupt(
            0,
            "live writer manifest authority duplication failed",
        ));
    }
    // SAFETY: DuplicateHandle transferred a fresh owned handle into this
    // process. File owns and closes it on every subsequent error path.
    let file = unsafe { File::from_raw_handle(duplicated) };
    let identity = journal_file_identity(&file)?;
    let bytes = read_file_bytes(&file)?;
    let digest = manifest_hash(&bytes);
    if identity != record.file_identity || digest != record.manifest_hash {
        return Err(corrupt(
            0,
            "duplicated live writer manifest authority does not match its durable binding",
        ));
    }
    Ok(Some((
        ManifestAuthorityHandle {
            file,
            file_identity: identity,
            manifest_hash: digest,
        },
        bytes,
    )))
}

#[cfg(all(test, windows))]
fn writer_owns_matching_manifest_authority(
    root: &Path,
    manifest_hash: Option<&str>,
    file_identity: Option<&str>,
) -> Result<bool, JournalError> {
    let lock_path = writer_lock_path(root);
    let mut exclusive = OpenOptions::new();
    exclusive.read(true).write(true);
    exclusive.share_mode(0);
    match exclusive.open(&lock_path) {
        Ok(file) => {
            drop(file);
            return Ok(false);
        }
        Err(error) if error.raw_os_error() == Some(32) => {}
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(io_error(error)),
    }
    let mut reader = OpenOptions::new();
    reader.read(true);
    reader.share_mode(0x0000_0001 | 0x0000_0002 | 0x0000_0004);
    let mut reader = reader.open(&lock_path).map_err(io_error)?;
    let mut bytes = Vec::new();
    reader.read_to_end(&mut bytes).map_err(io_error)?;
    let Ok(authority) = serde_json::from_slice::<WriterManifestAuthority>(&bytes) else {
        return Ok(false);
    };
    if authority.manifest_hash.is_empty()
        || authority.file_identity.is_empty()
        || authority.process_id == 0
        || authority.manifest_handle == 0
    {
        return Ok(false);
    }
    if manifest_hash.is_some_and(|value| value != authority.manifest_hash)
        || file_identity.is_some_and(|value| value != authority.file_identity)
    {
        return Ok(false);
    }
    let mut still_exclusive = OpenOptions::new();
    still_exclusive.read(true).write(true);
    still_exclusive.share_mode(0);
    match still_exclusive.open(&lock_path) {
        Ok(file) => {
            drop(file);
            Ok(false)
        }
        Err(error) if error.raw_os_error() == Some(32) => Ok(true),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(false),
        Err(error) => Err(io_error(error)),
    }
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

/// Returns the immutable generation currently selected by a stable journal
/// root. This is intended for diagnostics and fault-injection tests; ordinary
/// readers must use the root path so manifest switches are checked on every
/// authority boundary.
pub fn current_journal_generation_path(path: impl AsRef<Path>) -> Result<PathBuf, JournalError> {
    Ok(resolve_journal_location(path.as_ref())?.data_path)
}

/// Retires this process's authenticated incremental-reader handle for a
/// journal during graceful shutdown. A process exit releases the Windows
/// range lock automatically; in-process restart harnesses and maintenance
/// tools should call this before replacing or deliberately corrupting the
/// journal object.
pub fn retire_incremental_reader(path: impl AsRef<Path>) {
    release_verified_prefix_session(path.as_ref());
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
    inspection_from_snapshot(read_snapshot(path.as_ref())?)
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
/// allocates nor rescans retained history. Callers that make durable output
/// decisions must request `verify_prefix` on every pass. On Windows, the
/// first pass after process start reconstructs and byte-range-locks the exact
/// authenticated prefix; later passes lock and revalidate only their bounded
/// suffix. This detects an in-place valid rewrite before the cursor even when
/// its suffix still parses without turning every minute into a historical
/// rescan. A changed file identity or a shorter file is rejected rather than
/// guessed at; callers can use
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
    let root = path.as_ref();
    // This is intentionally before the manifest/path resolution. A reporter
    // that is reconstructing a large cache miss must already block the
    // writer's publication/reclamation decision before it opens either
    // generation object.
    let _early_lease = begin_incremental_reader_lease(root, verify_prefix)?;
    let location = resolve_journal_location_with_authority(root, verify_prefix, verify_prefix)?;
    if location.manifest.is_none() {
        // Legacy root and v1-manifest journals are writer-migrated rather
        // than resumed as a durable projection authority.
        if let Some(cursor) = cursor
            && location.legacy_manifest.is_some()
        {
            return Err(corrupt(
                cursor.byte_offset,
                "legacy journal generation requires writer migration before resume",
            ));
        }
        return read_journal_incremental_single(
            JournalReadSegment {
                root,
                data_path: &location.data_path,
                generation: location.generation,
                manifest_hash: &location.manifest_hash,
                segment_kind: 0,
            },
            cursor,
            event_limit,
            verify_prefix,
        );
    }
    let manifest = location
        .manifest
        .as_ref()
        .expect("checked manifest presence");
    if let Some(cursor) = cursor
        && (cursor.generation != location.generation
            || cursor.manifest_hash != location.manifest_hash
            || cursor.segment_kind > 1)
    {
        return Err(corrupt(
            cursor.byte_offset,
            "journal generation manifest changed after the durable reader checkpoint",
        ));
    }
    if verify_prefix {
        // Establish the immutable manifest proof before any bounded batch is
        // eligible to become projection authority. The session records that
        // proof so subsequent catch-up batches do not repeat the scan.
        ensure_manifest_sealed_session(root, &location)?;
    }
    if cursor.is_some_and(|cursor| cursor.segment_kind == 1) {
        let active = location
            .active_path
            .as_ref()
            .expect("v2 manifest has active segment");
        return read_journal_incremental_single(
            JournalReadSegment {
                root,
                data_path: active,
                generation: location.generation,
                manifest_hash: &location.manifest_hash,
                segment_kind: 1,
            },
            cursor,
            event_limit,
            verify_prefix,
        );
    }

    let sealed = read_journal_incremental_single(
        JournalReadSegment {
            root,
            data_path: &location.data_path,
            generation: location.generation,
            manifest_hash: &location.manifest_hash,
            segment_kind: 0,
        },
        cursor,
        event_limit,
        verify_prefix,
    )?;
    if !cursor_matches_sealed_manifest(&sealed.cursor, manifest)
        || sealed.events.len() == event_limit
    {
        return Ok(sealed);
    }
    // The sealed generation is now proven through the exact manifest
    // boundary. Begin the append-only segment at that authenticated sequence
    // without pretending its file bytes are part of the sealed prefix hash.
    let active_path = location
        .active_path
        .as_ref()
        .expect("v2 manifest has active segment");
    let active_file = File::open(active_path).map_err(io_error)?;
    let active_cursor = JournalCursor {
        generation: location.generation,
        manifest_hash: location.manifest_hash.clone(),
        segment_kind: 1,
        byte_offset: 0,
        raw_sequence: sealed.cursor.raw_sequence,
        prefix_hash: initial_prefix_hash(),
        file_identity: journal_file_identity(&active_file)?,
        worker_id: sealed.cursor.worker_id.clone(),
        channel_id: sealed.cursor.channel_id.clone(),
        // These describe the logical sequence boundary inherited from the
        // sealed prefix; the byte prefix hash remains segment-local.
        compaction_next_sequence: sealed.cursor.compaction_next_sequence,
        compaction_layout_version: sealed.cursor.compaction_layout_version,
        saw_heartbeat_summary: sealed.cursor.saw_heartbeat_summary,
        saw_compaction_commit: sealed.cursor.saw_compaction_commit,
    };
    let active = read_journal_incremental_single(
        JournalReadSegment {
            root,
            data_path: active_path,
            generation: location.generation,
            manifest_hash: &location.manifest_hash,
            segment_kind: 1,
        },
        Some(&active_cursor),
        event_limit - sealed.events.len(),
        verify_prefix,
    )?;
    let mut events = sealed.events;
    events.extend(active.events);
    Ok(IncrementalJournalRead {
        worker_id: active.worker_id.or(sealed.worker_id),
        channel_id: active.channel_id.or(sealed.channel_id),
        events,
        cursor: active.cursor,
    })
}

/// Re-proves that a just-read incremental cursor is still selected by the
/// root manifest immediately before a caller makes its projection durable.
///
/// This is deliberately separate from [`read_journal_incremental`]: a
/// reporter can process a bounded batch without holding an SQLite transaction
/// open, but must not acknowledge that batch if a writer exited or switched
/// the manifest in the meantime. On Windows a reporter that encountered a
/// live writer retains a duplicate of that writer's exact no-delete manifest
/// handle, so the proof remains authority even if that writer then exits.
pub fn verify_incremental_reader_authority(
    path: impl AsRef<Path>,
    cursor: &JournalCursor,
) -> Result<(), JournalError> {
    let root = path.as_ref();
    #[cfg(windows)]
    let retained_binding = retained_manifest_authority_binding(root);

    // Do not reuse the process cache here: this is the acknowledgement
    // boundary, so resolve the current pathname anew and compare it with the
    // durable cursor. A retained no-delete authority is duplicated rather
    // than reopened by pathname while it remains live.
    let location = resolve_journal_location_with_authority_policy(root, false, true, false)?;
    if location.generation != cursor.generation || location.manifest_hash != cursor.manifest_hash {
        return Err(corrupt(
            cursor.byte_offset,
            "journal generation manifest changed before durable reader acknowledgement",
        ));
    }
    let selected_path = match cursor.segment_kind {
        0 => &location.data_path,
        1 => location.active_path.as_ref().ok_or_else(|| {
            corrupt(
                cursor.byte_offset,
                "journal active segment disappeared before durable reader acknowledgement",
            )
        })?,
        _ => {
            return Err(corrupt(
                cursor.byte_offset,
                "journal cursor has an invalid segment before durable reader acknowledgement",
            ));
        }
    };
    let selected = File::open(selected_path).map_err(io_error)?;
    if journal_file_identity(&selected)? != cursor.file_identity {
        return Err(corrupt(
            cursor.byte_offset,
            "journal segment changed before durable reader acknowledgement",
        ));
    }
    #[cfg(windows)]
    if let Some((manifest_hash, _manifest_identity)) = retained_binding
        && manifest_hash != location.manifest_hash
    {
        return Err(corrupt(
            cursor.byte_offset,
            "retained journal manifest authority changed before acknowledgement",
        ));
    }
    Ok(())
}

fn sealed_manifest_cursor(location: &JournalLocation) -> Result<JournalCursor, JournalError> {
    let manifest = location
        .manifest
        .as_ref()
        .ok_or_else(|| corrupt(0, "sealed manifest cursor requires v2 manifest"))?;
    Ok(JournalCursor {
        generation: location.generation,
        manifest_hash: location.manifest_hash.clone(),
        segment_kind: 0,
        byte_offset: manifest.sealed_byte_offset,
        raw_sequence: manifest.sealed_raw_sequence,
        prefix_hash: manifest.sealed_prefix_hash.clone(),
        file_identity: manifest.sealed_file_identity.clone(),
        worker_id: Some(manifest.worker_id.clone()),
        channel_id: Some(manifest.channel_id.clone()),
        compaction_next_sequence: manifest.sealed_compaction_next_sequence,
        compaction_layout_version: COMPACTION_LAYOUT_COMMIT_THEN_RETAINED,
        saw_heartbeat_summary: true,
        saw_compaction_commit: true,
    })
}

/// A restarted active-segment reader first proves the immutable sealed object
/// selected by the manifest. Once the process owns an active session, later
/// cadence passes retain that proof and only parse the active suffix.
fn ensure_manifest_sealed_session(
    root: &Path,
    location: &JournalLocation,
) -> Result<(), JournalError> {
    #[cfg(windows)]
    {
        let sealed = sealed_manifest_cursor(location)?;
        if verified_prefix_sessions()
            .lock()
            .expect("verified prefix sessions are not poisoned")
            .get(root)
            .is_some_and(|session| {
                session.generation == location.generation
                    && session.sealed_proof.as_ref() == Some(&sealed)
            })
        {
            return Ok(());
        }
    }
    let cursor = sealed_manifest_cursor(location)?;
    ensure_verified_prefix_session(root, &location.data_path, location.generation, &cursor)
}

#[cfg(windows)]
fn sealed_proof_for_cursor(root: &Path, cursor: &JournalCursor) -> Option<JournalCursor> {
    let location = resolve_journal_location(root).ok()?;
    let sealed = sealed_manifest_cursor(&location).ok()?;
    (sealed == *cursor).then_some(sealed)
}

fn cursor_matches_sealed_manifest(
    cursor: &JournalCursor,
    manifest: &JournalGenerationManifest,
) -> bool {
    cursor.segment_kind == 0
        && cursor.byte_offset == manifest.sealed_byte_offset
        && cursor.raw_sequence == manifest.sealed_raw_sequence
        && cursor.prefix_hash == manifest.sealed_prefix_hash
        && cursor.file_identity == manifest.sealed_file_identity
        && cursor.worker_id.as_deref() == Some(&manifest.worker_id)
        && cursor.channel_id.as_deref() == Some(&manifest.channel_id)
        && cursor.saw_heartbeat_summary
        && cursor.saw_compaction_commit
        && cursor.compaction_layout_version == COMPACTION_LAYOUT_COMMIT_THEN_RETAINED
        && cursor.compaction_next_sequence == manifest.sealed_compaction_next_sequence
}

fn read_journal_incremental_single(
    segment: JournalReadSegment<'_>,
    cursor: Option<&JournalCursor>,
    event_limit: usize,
    verify_prefix: bool,
) -> Result<IncrementalJournalRead, JournalError> {
    let JournalReadSegment {
        root,
        data_path: path,
        generation,
        manifest_hash: manifest_digest,
        segment_kind,
    } = segment;
    if event_limit == 0 {
        return Err(corrupt(0, "incremental event limit must be positive"));
    }
    if segment_kind > 1 {
        return Err(corrupt(0, "journal segment kind is invalid"));
    }
    let prior_cursor = cursor.cloned();
    let path_probe = File::open(path).map_err(io_error)?;
    let path_metadata = path_probe.metadata().map_err(io_error)?;
    let path_file_identity = journal_file_identity(&path_probe)?;
    let path_length = path_metadata.len();
    if let Some(cursor) = cursor {
        if cursor.generation != generation || cursor.segment_kind != segment_kind {
            return Err(corrupt(
                cursor.byte_offset,
                "journal generation or segment changed after the durable reader checkpoint",
            ));
        }
        if cursor.manifest_hash != manifest_digest {
            return Err(corrupt(
                cursor.byte_offset,
                "journal manifest changed after the durable reader checkpoint",
            ));
        }
        if cursor.byte_offset > path_length {
            return Err(corrupt(
                cursor.byte_offset,
                "journal was truncated before the durable reader checkpoint",
            ));
        }
        if cursor.file_identity != path_file_identity {
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
    }

    let mut file = match (cursor, verify_prefix) {
        (Some(cursor), true) => verified_prefix_session_file(root, path, generation, cursor)?,
        _ => File::open(path).map_err(io_error)?,
    };
    let metadata = file.metadata().map_err(io_error)?;
    let file_identity = journal_file_identity(&file)?;
    let length = metadata.len();
    if let Some(cursor) = cursor {
        if cursor.byte_offset > length {
            return Err(corrupt(
                cursor.byte_offset,
                "locked journal handle was truncated before the durable reader checkpoint",
            ));
        }
        if cursor.file_identity != file_identity {
            return Err(corrupt(
                cursor.byte_offset,
                "locked journal handle identity changed after the durable reader checkpoint",
            ));
        }
    }
    let mut offset = cursor.map_or(0, |value| value.byte_offset);
    file.seek(SeekFrom::Start(offset)).map_err(io_error)?;
    let mut prefix_hash = cursor
        .map(|value| value.prefix_hash.clone())
        .unwrap_or_else(initial_prefix_hash);
    let mut coverage =
        SequenceCoverage::after_raw(cursor.map_or(0, |value| value.raw_sequence), offset)?;
    let mut worker_id = cursor.and_then(|value| value.worker_id.clone());
    let mut channel_id = cursor.and_then(|value| value.channel_id.clone());
    if worker_id.is_some() != channel_id.is_some() {
        return Err(corrupt(offset, "journal cursor identity is incomplete"));
    }
    let mut events = Vec::new();
    let mut summary_seen = cursor.is_some_and(|value| value.saw_heartbeat_summary);
    let mut commit_seen = cursor.is_some_and(|value| value.saw_compaction_commit);
    let mut compaction_next_sequence = cursor.map_or(0, |value| value.compaction_next_sequence);
    let mut compaction_layout_version = cursor.map_or(0, |value| value.compaction_layout_version);
    if commit_seen != (compaction_next_sequence != 0) {
        return Err(corrupt(
            offset,
            "journal cursor compaction seal is incomplete",
        ));
    }
    if commit_seen && compaction_layout_version == 0 {
        // Checkpoints written by the first explicit-seal implementation did
        // not yet persist a layout marker. Their commit is necessarily before
        // their retained cursor, so retain that safe interpretation and let a
        // later writer rewrite the file with the explicit marker.
        compaction_layout_version = COMPACTION_LAYOUT_COMMIT_THEN_RETAINED;
    }
    if commit_seen && compaction_layout_version != COMPACTION_LAYOUT_COMMIT_THEN_RETAINED {
        return Err(corrupt(
            offset,
            "legacy compaction cursor requires writer migration",
        ));
    }
    let mut compaction_coverage_complete =
        commit_seen && coverage.next_sequence() >= compaction_next_sequence;
    let mut saw_event = cursor.is_some();
    let mut summary_ranges = Vec::new();
    let mut summary_range_index = 0_usize;
    // The commit precedes retained frames in layout version two. Therefore a
    // cursor at the event cap already binds its seal. Exact coverage advances
    // through the retained range over later bounded passes; EOF before the
    // declared next sequence fails closed without rescanning the suffix on
    // every batch.
    let mut bounded_cursor = None;
    while let Some(record) = read_stream_record(&mut file, offset)? {
        let record_end = offset
            .checked_add(record.total_bytes)
            .ok_or_else(|| corrupt(offset, "journal offset overflow"))?;
        prefix_hash = next_prefix_hash(&prefix_hash, &record.raw_bytes);
        match record.kind {
            StreamRecordKind::Event(event) => {
                if summary_seen && !commit_seen {
                    return Err(corrupt(
                        offset,
                        "retained events require a preceding compaction commit; heartbeat summary has no compaction commit",
                    ));
                }
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
                update_compaction_coverage(
                    &coverage,
                    commit_seen,
                    compaction_next_sequence,
                    &mut compaction_coverage_complete,
                    offset,
                )?;
                saw_event = true;
                if events.len() < event_limit {
                    events.push(event);
                    if events.len() == event_limit {
                        bounded_cursor = Some(stream_cursor(
                            CursorSource {
                                generation,
                                manifest_hash: manifest_digest,
                                segment_kind,
                                file_identity: &file_identity,
                            },
                            record_end,
                            &coverage,
                            &prefix_hash,
                            (&worker_id, &channel_id),
                            (
                                summary_seen,
                                commit_seen,
                                compaction_next_sequence,
                                compaction_layout_version,
                            ),
                        ));
                        offset = record_end;
                        break;
                    }
                }
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
                if !summary_seen || commit_seen || saw_event {
                    return Err(corrupt(
                        offset,
                        "compaction commit must seal heartbeat summaries before retained events",
                    ));
                }
                if worker_id.as_deref() != Some(&commit.worker_id)
                    || channel_id.as_deref() != Some(&commit.channel_id)
                    || commit.next_sequence == 0
                    || !matches!(
                        commit.layout_version,
                        None | Some(COMPACTION_LAYOUT_COMMIT_THEN_RETAINED)
                    )
                {
                    return Err(corrupt(offset, "compaction commit does not bind identity"));
                }
                commit_seen = true;
                compaction_next_sequence = commit.next_sequence;
                compaction_layout_version = COMPACTION_LAYOUT_COMMIT_THEN_RETAINED;
                update_compaction_coverage(
                    &coverage,
                    true,
                    compaction_next_sequence,
                    &mut compaction_coverage_complete,
                    offset,
                )?;
            }
            StreamRecordKind::FallbackToneAudit(entry) => {
                validate_fallback_tone_audit_record(&entry).map_err(|reason| {
                    corrupt(offset, format!("invalid fallback tone audit: {reason}"))
                })?;
                bind_stream_identity(
                    &mut worker_id,
                    &mut channel_id,
                    fallback_tone_audit_identity(&entry),
                    offset,
                )?;
            }
        }
        offset = record_end;
        if bounded_cursor.is_some() {
            break;
        }
    }
    if let Some(cursor) = bounded_cursor {
        if verify_prefix {
            advance_verified_prefix_session(
                root,
                path,
                generation,
                prior_cursor.as_ref(),
                &cursor,
            )?;
        }
        return Ok(IncrementalJournalRead {
            worker_id,
            channel_id,
            events,
            cursor,
        });
    }
    if summary_seen && !commit_seen {
        return Err(corrupt(0, "heartbeat summary has no compaction commit"));
    }
    if commit_seen && !compaction_coverage_complete {
        finish_summary_coverage(
            &mut coverage,
            &summary_ranges,
            &mut summary_range_index,
            offset,
        )?;
        update_compaction_coverage(
            &coverage,
            true,
            compaction_next_sequence,
            &mut compaction_coverage_complete,
            offset,
        )?;
    }
    if commit_seen && !compaction_coverage_complete {
        return Err(corrupt(
            offset,
            "compaction commit does not cover every retained sequence",
        ));
    }
    let final_cursor = bounded_cursor.unwrap_or_else(|| {
        stream_cursor(
            CursorSource {
                generation,
                manifest_hash: manifest_digest,
                segment_kind,
                file_identity: &file_identity,
            },
            offset,
            &coverage,
            &prefix_hash,
            (&worker_id, &channel_id),
            (
                summary_seen,
                commit_seen,
                compaction_next_sequence,
                compaction_layout_version,
            ),
        )
    });
    if verify_prefix {
        advance_verified_prefix_session(
            root,
            path,
            generation,
            prior_cursor.as_ref(),
            &final_cursor,
        )?;
    }
    Ok(IncrementalJournalRead {
        worker_id,
        channel_id,
        events,
        cursor: final_cursor,
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
    let location = resolve_journal_location(path.as_ref())?;
    let path = location.data_path.as_path();
    let mut file = File::open(path).map_err(io_error)?;
    let file_identity = journal_file_identity(&file)?;
    let mut offset = 0_u64;
    let mut coverage = SequenceCoverage::new();
    let mut prefix_hash = initial_prefix_hash();
    let mut worker_id = None;
    let mut channel_id = None;
    let mut summary_seen = false;
    let mut commit_seen = false;
    let mut compaction_next_sequence = 0_u64;
    let mut compaction_layout_version = 0_u8;
    let mut compaction_coverage_complete = false;
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
                if summary_seen && !commit_seen {
                    return Err(corrupt(
                        offset,
                        "retained events require a preceding compaction commit; heartbeat summary has no compaction commit",
                    ));
                }
                bind_stream_identity(
                    &mut worker_id,
                    &mut channel_id,
                    (event.worker_id.clone(), event.channel_id.clone()),
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
                update_compaction_coverage(
                    &coverage,
                    commit_seen,
                    compaction_next_sequence,
                    &mut compaction_coverage_complete,
                    offset,
                )?;
                saw_event = true;
                let cursor = stream_cursor(
                    CursorSource {
                        generation: location.generation,
                        manifest_hash: &location.manifest_hash,
                        segment_kind: 0,
                        file_identity: &file_identity,
                    },
                    record_end,
                    &coverage,
                    &prefix_hash,
                    (&worker_id, &channel_id),
                    (
                        summary_seen,
                        commit_seen,
                        compaction_next_sequence,
                        compaction_layout_version,
                    ),
                );
                if event.sequence == sequence {
                    found = Some((event, cursor));
                }
            }
            StreamRecordKind::HeartbeatSummary(summary) => {
                if saw_event || commit_seen {
                    return Err(corrupt(offset, "heartbeat summary records are invalid"));
                }
                validate_summary(&summary, offset)?;
                bind_stream_identity(
                    &mut worker_id,
                    &mut channel_id,
                    (summary.worker_id.clone(), summary.channel_id.clone()),
                    offset,
                )?;
                for range in &summary.ranges {
                    summary_ranges.push((range.first_sequence, range.last_sequence));
                }
                summary_seen = true;
            }
            StreamRecordKind::CompactionCommit(commit) => {
                if !summary_seen
                    || commit_seen
                    || saw_event
                    || commit.next_sequence == 0
                    || !matches!(
                        commit.layout_version,
                        None | Some(COMPACTION_LAYOUT_COMMIT_THEN_RETAINED)
                    )
                    || worker_id.as_deref() != Some(&commit.worker_id)
                    || channel_id.as_deref() != Some(&commit.channel_id)
                {
                    return Err(corrupt(
                        offset,
                        "compaction commit does not bind sequence and identity",
                    ));
                }
                commit_seen = true;
                compaction_next_sequence = commit.next_sequence;
                compaction_layout_version = COMPACTION_LAYOUT_COMMIT_THEN_RETAINED;
                update_compaction_coverage(
                    &coverage,
                    true,
                    compaction_next_sequence,
                    &mut compaction_coverage_complete,
                    offset,
                )?;
            }
            StreamRecordKind::FallbackToneAudit(entry) => {
                validate_fallback_tone_audit_record(&entry).map_err(|reason| {
                    corrupt(offset, format!("invalid fallback tone audit: {reason}"))
                })?;
                bind_stream_identity(
                    &mut worker_id,
                    &mut channel_id,
                    fallback_tone_audit_identity(&entry),
                    offset,
                )?;
            }
        }
        offset = record_end;
    }
    if summary_seen && !commit_seen {
        return Err(corrupt(0, "heartbeat summary has no compaction commit"));
    }
    if commit_seen && !compaction_coverage_complete {
        finish_summary_coverage(
            &mut coverage,
            &summary_ranges,
            &mut summary_range_index,
            offset,
        )?;
        update_compaction_coverage(
            &coverage,
            true,
            compaction_next_sequence,
            &mut compaction_coverage_complete,
            offset,
        )?;
    }
    if commit_seen && !compaction_coverage_complete {
        return Err(corrupt(
            offset,
            "compaction commit does not cover every retained sequence",
        ));
    }
    found.ok_or_else(|| {
        corrupt(
            offset,
            "durable reader anchor is absent after journal replacement",
        )
    })
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
        fallback_tone_audit: snapshot.fallback_tone_audit,
        next_sequence: snapshot.next_sequence,
    })
}

fn fallback_tone_audit_state(
    entries: &[FallbackToneJournalEntry],
) -> Result<HashMap<String, (WorkerFallbackTone, u64)>, String> {
    let mut active = HashMap::new();
    for entry in entries {
        apply_fallback_tone_audit_entry(entry, &mut active)?;
    }
    Ok(active)
}

fn apply_fallback_tone_audit_entry(
    entry: &FallbackToneJournalEntry,
    active: &mut HashMap<String, (WorkerFallbackTone, u64)>,
) -> Result<(), String> {
    match entry {
        FallbackToneJournalEntry::Activated {
            tone,
            activated_at_monotonic_ms,
            ..
        } => {
            tone.validate()?;
            // The worker owns one physical fallback-tone renderer, represented
            // by Option<ActiveFallbackTone>; durable replay must preserve that
            // same single-active invariant.
            if !active.is_empty() {
                return Err("fallback tone audit activates while another tone is active".into());
            }
            if active.contains_key(&tone.activation_id) {
                return Err("fallback tone audit activates an already-active identity".into());
            }
            active.insert(
                tone.activation_id.clone(),
                (tone.clone(), *activated_at_monotonic_ms),
            );
        }
        FallbackToneJournalEntry::Deactivated {
            tone,
            reason,
            deactivated_at_monotonic_ms,
            ..
        } => {
            tone.validate()?;
            let (activated, activated_at_monotonic_ms) =
                active.get(&tone.activation_id).ok_or_else(|| {
                    "fallback tone audit deactivates an inactive identity".to_string()
                })?;
            if activated != tone {
                return Err("fallback tone audit deactivation tone differs from activation".into());
            }
            match reason {
                FallbackToneDeactivationReason::Recovery
                    if *deactivated_at_monotonic_ms != *activated_at_monotonic_ms =>
                {
                    return Err(
                        "fallback tone recovery must preserve the prior activation mark".into(),
                    );
                }
                _ if *deactivated_at_monotonic_ms < *activated_at_monotonic_ms => {
                    return Err("fallback tone audit deactivation predates activation".into());
                }
                _ => {}
            }
            if matches!(reason, FallbackToneDeactivationReason::Expired) {
                let expires_at_monotonic_ms = activated_at_monotonic_ms
                    .checked_add(tone.duration_ms)
                    .ok_or_else(|| "fallback tone audit activation expiry overflows".to_string())?;
                if *deactivated_at_monotonic_ms < expires_at_monotonic_ms {
                    return Err(
                        "fallback tone audit expiry predates its configured duration".into(),
                    );
                }
            }
            active.remove(&tone.activation_id);
        }
    }
    Ok(())
}

enum StreamRecordKind {
    Event(WorkerEventEnvelope),
    HeartbeatSummary(HeartbeatSummary),
    CompactionCommit(CompactionCommit),
    FallbackToneAudit(FallbackToneJournalEntry),
}

struct StreamRecord {
    kind: StreamRecordKind,
    total_bytes: u64,
    raw_bytes: Vec<u8>,
}

fn read_stream_record(file: &mut File, offset: u64) -> Result<Option<StreamRecord>, JournalError> {
    #[cfg(test)]
    STREAM_RECORD_READS.with(|reads| reads.set(reads.get() + 1));
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
            RECORD_KIND_FALLBACK_TONE_AUDIT if version == JOURNAL_VERSION => {
                StreamRecordKind::FallbackToneAudit(serde_json::from_slice(&payload).map_err(
                    |error| corrupt(offset, format!("invalid fallback tone audit: {error}")),
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

/// Ensures that a production reader either holds a byte-range lock over its
/// exact authenticated prefix or has just reconstructed and locked it. The
/// lock lets normal appends proceed beyond the prefix while preventing a
/// CRC-valid rewrite of already-authoritative bytes between reporting passes.
fn ensure_verified_prefix_session(
    root: &Path,
    data_path: &Path,
    generation: u64,
    cursor: &JournalCursor,
) -> Result<(), JournalError> {
    #[cfg(windows)]
    {
        let sealed_to_active = {
            let mut sessions = verified_prefix_sessions()
                .lock()
                .expect("verified prefix sessions are not poisoned");
            if let Some(session) = sessions.get_mut(root)
                && session.data_path == data_path
                && session.generation == generation
                && session.cursor == *cursor
            {
                if session.lease.is_none() {
                    session.lease = Some(take_incremental_reader_lease(root)?);
                }
                ensure_current_generation_is_locked_file(
                    root,
                    data_path,
                    generation,
                    &session.file,
                    cursor,
                )?;
                ensure_retained_sealed_file(root, generation, session, cursor)?;
                return Ok(());
            }
            // The public v2 reader reaches the active segment only after its
            // sealed manifest cursor was fully authenticated. Move that exact
            // locked handle into the session instead of dropping it while
            // changing the streaming object.
            if cursor.segment_kind == 1
                && sessions.get(root).is_some_and(|session| {
                    session.generation == generation
                        && session.cursor.segment_kind == 0
                        && session.sealed_file.is_none()
                })
            {
                sessions.remove(root)
            } else {
                // A manifest switch can only occur after its preceding lease
                // was acknowledged. Retire stale objects now; the writer may
                // reclaim their names while retained handles remain valid.
                sessions.remove(root);
                None
            }
        };
        if let Some(mut sealed_session) = sealed_to_active {
            let location = resolve_journal_location(root)?;
            let sealed_cursor = sealed_manifest_cursor(&location)?;
            if sealed_session.cursor != sealed_cursor
                || location.generation != generation
                || location.active_path.as_deref() != Some(data_path)
            {
                return Err(corrupt(
                    cursor.byte_offset,
                    "active segment transition lacks the exact sealed manifest proof",
                ));
            }
            ensure_current_generation_is_locked_file(
                root,
                &sealed_session.data_path,
                generation,
                &sealed_session.file,
                &sealed_session.cursor,
            )?;
            let active_file = open_prefix_lock_handle(data_path)?;
            ensure_locked_file_matches_cursor(&active_file, cursor)?;
            verify_segment_cursor_prefix(root, data_path, generation, &active_file, cursor)?;
            lock_file_range(&active_file, 0, cursor.byte_offset)?;
            verify_segment_cursor_prefix(root, data_path, generation, &active_file, cursor)?;
            ensure_current_generation_is_locked_file(
                root,
                data_path,
                generation,
                &active_file,
                cursor,
            )?;
            sealed_session.sealed_path = Some(sealed_session.data_path.clone());
            sealed_session.sealed_file = Some(sealed_session.file);
            sealed_session.file = active_file;
            sealed_session.data_path = data_path.to_path_buf();
            sealed_session.cursor = cursor.clone();
            verified_prefix_sessions()
                .lock()
                .expect("verified prefix sessions are not poisoned")
                .insert(root.to_path_buf(), sealed_session);
            return Ok(());
        }
        // A restart/cache miss pays one complete proof before the prefix is
        // protected. Re-check after taking the lock to close that acquisition
        // race; later passes only inspect their bounded suffix.
        let lease = take_incremental_reader_lease(root)?;
        let file = match open_prefix_lock_handle(data_path) {
            Ok(file) => file,
            Err(error) => {
                drop(lease);
                let _ = std::fs::remove_file(reader_lease_path(root));
                return Err(error);
            }
        };
        ensure_locked_file_matches_cursor(&file, cursor)?;
        verify_segment_cursor_prefix(root, data_path, generation, &file, cursor)?;
        lock_file_range(&file, 0, cursor.byte_offset)?;
        verify_segment_cursor_prefix(root, data_path, generation, &file, cursor)?;
        ensure_current_generation_is_locked_file(root, data_path, generation, &file, cursor)?;
        verified_prefix_sessions()
            .lock()
            .expect("verified prefix sessions are not poisoned")
            .insert(
                root.to_path_buf(),
                VerifiedPrefixSession {
                    file,
                    sealed_file: None,
                    sealed_path: None,
                    sealed_proof: sealed_proof_for_cursor(root, cursor),
                    lease: Some(lease),
                    data_path: data_path.to_path_buf(),
                    generation,
                    cursor: cursor.clone(),
                },
            );
        Ok(())
    }
    #[cfg(not(windows))]
    {
        verify_journal_cursor_prefix(data_path, cursor)
    }
}

/// A v2 active segment starts its byte hash at zero, but its sequence coverage
/// starts exactly after the authenticated sealed generation. Reconstruct that
/// tiny anchor from the current manifest before replaying the active prefix.
#[cfg(windows)]
fn verify_segment_cursor_prefix(
    root: &Path,
    data_path: &Path,
    generation: u64,
    file: &File,
    cursor: &JournalCursor,
) -> Result<(), JournalError> {
    if cursor.segment_kind == 0 {
        return verify_journal_cursor_prefix_file(file, cursor);
    }
    let current = resolve_journal_location(root)?;
    let manifest = current
        .manifest
        .as_ref()
        .ok_or_else(|| corrupt(cursor.byte_offset, "active cursor has no v2 manifest"))?;
    if current.generation != generation
        || current.active_path.as_deref() != Some(data_path)
        || cursor.manifest_hash != current.manifest_hash
    {
        return Err(corrupt(
            cursor.byte_offset,
            "journal manifest changed while verifying active segment",
        ));
    }
    let anchor = JournalCursor {
        generation,
        manifest_hash: current.manifest_hash,
        segment_kind: 1,
        byte_offset: 0,
        raw_sequence: manifest.sealed_raw_sequence,
        prefix_hash: initial_prefix_hash(),
        file_identity: journal_file_identity(file)?,
        worker_id: Some(manifest.worker_id.clone()),
        channel_id: Some(manifest.channel_id.clone()),
        compaction_next_sequence: manifest.sealed_compaction_next_sequence,
        compaction_layout_version: COMPACTION_LAYOUT_COMMIT_THEN_RETAINED,
        saw_heartbeat_summary: true,
        saw_compaction_commit: true,
    };
    verify_journal_cursor_suffix(file, &anchor, cursor)
}

/// Returns a duplicate of the exact locked object that authenticated the
/// durable cursor. Reading this instead of reopening `path` prevents a
/// rename-replacement race from mixing a protected prefix with an unprotected
/// suffix from a different journal object.
fn verified_prefix_session_file(
    root: &Path,
    data_path: &Path,
    generation: u64,
    cursor: &JournalCursor,
) -> Result<File, JournalError> {
    #[cfg(windows)]
    {
        ensure_verified_prefix_session(root, data_path, generation, cursor)?;
        let sessions = verified_prefix_sessions()
            .lock()
            .expect("verified prefix sessions are not poisoned");
        let session = sessions.get(root).ok_or_else(|| {
            corrupt(
                cursor.byte_offset,
                "verified reader session disappeared before journal read",
            )
        })?;
        if session.cursor != *cursor {
            return Err(corrupt(
                cursor.byte_offset,
                "verified reader session does not match the durable checkpoint",
            ));
        }
        if session.data_path != data_path || session.generation != generation {
            return Err(corrupt(
                cursor.byte_offset,
                "verified reader session is for a different journal generation",
            ));
        }
        ensure_current_generation_is_locked_file(
            root,
            data_path,
            generation,
            &session.file,
            cursor,
        )?;
        ensure_retained_sealed_file(root, generation, session, cursor)?;
        session.file.try_clone().map_err(io_error)
    }
    #[cfg(not(windows))]
    {
        ensure_verified_prefix_session(root, data_path, generation, cursor)?;
        File::open(data_path).map_err(io_error)
    }
}

/// Extends a verified prefix lock only over the records consumed in this
/// invocation, then replays that small locked suffix against the prior cursor.
/// This preserves the prefix-chain proof without rereading an unbounded
/// already-locked history on every reporting pass.
fn advance_verified_prefix_session(
    root: &Path,
    data_path: &Path,
    generation: u64,
    prior: Option<&JournalCursor>,
    cursor: &JournalCursor,
) -> Result<(), JournalError> {
    #[cfg(windows)]
    {
        if prior.is_none() {
            let mut sessions = verified_prefix_sessions()
                .lock()
                .expect("verified prefix sessions are not poisoned");
            if let Some(session) = sessions.get_mut(root)
                && session.data_path == data_path
                && session.generation == generation
                && session.cursor.segment_kind == 0
                && cursor.segment_kind == 0
                && session
                    .sealed_proof
                    .as_ref()
                    .is_some_and(|proof| proof.byte_offset >= cursor.byte_offset)
            {
                // The manifest proof already authenticated this immutable
                // generation and the session holds its byte-range lock. A
                // first bounded replay from byte zero may persist an earlier
                // cursor without discarding that proof.
                ensure_current_generation_is_locked_file(
                    root,
                    data_path,
                    generation,
                    &session.file,
                    session.sealed_proof.as_ref().expect("checked sealed proof"),
                )?;
                session.cursor = cursor.clone();
                return Ok(());
            }
            drop(sessions);
            return ensure_verified_prefix_session(root, data_path, generation, cursor);
        }
        let prior = prior.expect("checked prior cursor");
        let mut sessions = verified_prefix_sessions()
            .lock()
            .expect("verified prefix sessions are not poisoned");
        let Some(session) = sessions.get_mut(root) else {
            drop(sessions);
            return ensure_verified_prefix_session(root, data_path, generation, cursor);
        };
        if session.cursor != *prior
            || session.data_path != data_path
            || session.generation != generation
        {
            drop(sessions);
            return ensure_verified_prefix_session(root, data_path, generation, cursor);
        }
        ensure_locked_file_matches_cursor(&session.file, cursor)?;
        ensure_current_generation_is_locked_file(
            root,
            data_path,
            generation,
            &session.file,
            cursor,
        )?;
        ensure_retained_sealed_file(root, generation, session, cursor)?;
        if cursor.byte_offset < prior.byte_offset {
            return Err(corrupt(
                cursor.byte_offset,
                "verified reader cursor moved backwards",
            ));
        }
        lock_file_range(&session.file, prior.byte_offset, cursor.byte_offset)?;
        verify_journal_cursor_suffix(&session.file, prior, cursor)?;
        ensure_current_generation_is_locked_file(
            root,
            data_path,
            generation,
            &session.file,
            cursor,
        )?;
        ensure_retained_sealed_file(root, generation, session, cursor)?;
        session.cursor = cursor.clone();
        Ok(())
    }
    #[cfg(not(windows))]
    {
        let _ = prior;
        verify_journal_cursor_prefix(data_path, cursor)
    }
}

/// Verifies only the records newly protected by a verified reader lock.
fn verify_journal_cursor_suffix(
    locked_file: &File,
    prior: &JournalCursor,
    cursor: &JournalCursor,
) -> Result<(), JournalError> {
    if prior.byte_offset == cursor.byte_offset {
        if prior != cursor {
            return Err(corrupt(
                cursor.byte_offset,
                "verified reader cursor changed without consuming records",
            ));
        }
        return Ok(());
    }
    let mut file = locked_file.try_clone().map_err(io_error)?;
    file.seek(SeekFrom::Start(prior.byte_offset))
        .map_err(io_error)?;
    let mut offset = prior.byte_offset;
    let mut prefix_hash = prior.prefix_hash.clone();
    let mut coverage = SequenceCoverage::after_raw(prior.raw_sequence, offset)?;
    let mut worker_id = prior.worker_id.clone();
    let mut channel_id = prior.channel_id.clone();
    let summary_seen = prior.saw_heartbeat_summary;
    let commit_seen = prior.saw_compaction_commit;
    let compaction_next_sequence = prior.compaction_next_sequence;
    let compaction_layout_version = prior.compaction_layout_version;
    if commit_seen != (compaction_next_sequence != 0)
        || (commit_seen && compaction_layout_version != COMPACTION_LAYOUT_COMMIT_THEN_RETAINED)
    {
        return Err(corrupt(
            offset,
            "verified reader compaction state is invalid",
        ));
    }
    while offset < cursor.byte_offset {
        let record = read_stream_record(&mut file, offset)?
            .ok_or_else(|| corrupt(offset, "journal ended before verified reader cursor"))?;
        let record_end = offset
            .checked_add(record.total_bytes)
            .ok_or_else(|| corrupt(offset, "journal offset overflow"))?;
        if record_end > cursor.byte_offset {
            return Err(corrupt(
                cursor.byte_offset,
                "verified reader cursor is not on a journal record boundary",
            ));
        }
        prefix_hash = next_prefix_hash(&prefix_hash, &record.raw_bytes);
        match record.kind {
            StreamRecordKind::Event(event) => {
                if summary_seen && !commit_seen {
                    return Err(corrupt(
                        offset,
                        "retained events require a preceding compaction commit",
                    ));
                }
                bind_stream_identity(
                    &mut worker_id,
                    &mut channel_id,
                    (event.worker_id, event.channel_id),
                    offset,
                )?;
                coverage.cover(event.sequence, event.sequence, offset)?;
            }
            StreamRecordKind::HeartbeatSummary(_) => {
                return Err(corrupt(
                    offset,
                    "heartbeat summary cannot appear after a verified reader cursor",
                ));
            }
            StreamRecordKind::CompactionCommit(_) => {
                return Err(corrupt(
                    offset,
                    "compaction commit cannot appear after a verified reader cursor",
                ));
            }
            StreamRecordKind::FallbackToneAudit(entry) => {
                validate_fallback_tone_audit_record(&entry).map_err(|reason| {
                    corrupt(offset, format!("invalid fallback tone audit: {reason}"))
                })?;
                bind_stream_identity(
                    &mut worker_id,
                    &mut channel_id,
                    fallback_tone_audit_identity(&entry),
                    offset,
                )?;
            }
        }
        offset = record_end;
    }
    if offset != cursor.byte_offset
        || coverage.raw_sequence() != cursor.raw_sequence
        || prefix_hash != cursor.prefix_hash
        || worker_id != cursor.worker_id
        || channel_id != cursor.channel_id
        || summary_seen != cursor.saw_heartbeat_summary
        || commit_seen != cursor.saw_compaction_commit
        || compaction_next_sequence != cursor.compaction_next_sequence
        || compaction_layout_version != cursor.compaction_layout_version
    {
        return Err(corrupt(
            cursor.byte_offset,
            "verified reader suffix integrity check failed",
        ));
    }
    Ok(())
}

fn fallback_tone_audit_identity(entry: &FallbackToneJournalEntry) -> (String, String) {
    match entry {
        FallbackToneJournalEntry::Activated {
            worker_id,
            channel_id,
            ..
        }
        | FallbackToneJournalEntry::Deactivated {
            worker_id,
            channel_id,
            ..
        } => (worker_id.clone(), channel_id.clone()),
    }
}

fn validate_fallback_tone_audit_record(entry: &FallbackToneJournalEntry) -> Result<(), String> {
    match entry {
        FallbackToneJournalEntry::Activated { tone, .. }
        | FallbackToneJournalEntry::Deactivated { tone, .. } => tone.validate(),
    }
}

#[cfg(windows)]
fn ensure_locked_file_matches_cursor(
    file: &File,
    cursor: &JournalCursor,
) -> Result<(), JournalError> {
    let metadata = file.metadata().map_err(io_error)?;
    if cursor.byte_offset > metadata.len() {
        return Err(corrupt(
            cursor.byte_offset,
            "locked journal handle was truncated before the durable reader checkpoint",
        ));
    }
    if journal_file_identity(file)? != cursor.file_identity {
        return Err(corrupt(
            cursor.byte_offset,
            "locked journal handle identity changed after the durable reader checkpoint",
        ));
    }
    Ok(())
}

/// A manifest can switch from one immutable generation to another while a
/// reader still owns the former object. The locked object remains safe to
/// inspect, but it must never advance authority unless the root still selects
/// this exact generation and FileIdInfo proves the named object is the one
/// retained by this process.
#[cfg(windows)]
fn ensure_current_generation_is_locked_file(
    root: &Path,
    data_path: &Path,
    generation: u64,
    locked_file: &File,
    cursor: &JournalCursor,
) -> Result<(), JournalError> {
    let current = resolve_journal_location(root)?;
    let selected_path =
        current.data_path == data_path || current.active_path.as_deref() == Some(data_path);
    if current.generation != generation
        || !selected_path
        || cursor.manifest_hash != current.manifest_hash
    {
        return Err(corrupt(
            cursor.byte_offset,
            "journal generation changed while the verified reader held its locked object",
        ));
    }
    let path_file = File::open(data_path).map_err(io_error)?;
    let path_identity = journal_file_identity(&path_file)?;
    let locked_identity = journal_file_identity(locked_file)?;
    if path_identity != locked_identity || locked_identity != cursor.file_identity {
        return Err(corrupt(
            cursor.byte_offset,
            "journal path changed while the verified reader held its locked object",
        ));
    }
    Ok(())
}

#[cfg(windows)]
fn ensure_retained_sealed_file(
    root: &Path,
    generation: u64,
    session: &VerifiedPrefixSession,
    cursor: &JournalCursor,
) -> Result<(), JournalError> {
    let Some(sealed_file) = session.sealed_file.as_ref() else {
        return Ok(());
    };
    let Some(sealed_path) = session.sealed_path.as_ref() else {
        return Err(corrupt(
            cursor.byte_offset,
            "verified active session lost its sealed generation path",
        ));
    };
    let location = resolve_journal_location_strict(root)?;
    let manifest = location.manifest.as_ref().ok_or_else(|| {
        corrupt(
            cursor.byte_offset,
            "verified active session lost v2 manifest",
        )
    })?;
    if location.generation != generation || location.data_path != *sealed_path {
        return Err(corrupt(
            cursor.byte_offset,
            "sealed generation changed while active reader session was retained",
        ));
    }
    if journal_file_identity(sealed_file)? != manifest.sealed_file_identity {
        return Err(corrupt(
            cursor.byte_offset,
            "sealed generation identity changed while active reader session was retained",
        ));
    }
    let reopened = File::open(sealed_path).map_err(io_error)?;
    if journal_file_identity(&reopened)? != journal_file_identity(sealed_file)? {
        return Err(corrupt(
            cursor.byte_offset,
            "sealed generation path changed while active reader session was retained",
        ));
    }
    Ok(())
}

#[cfg(windows)]
fn open_prefix_lock_handle(path: &Path) -> Result<File, JournalError> {
    const FILE_SHARE_READ: u32 = 0x0000_0001;
    const FILE_SHARE_WRITE: u32 = 0x0000_0002;
    const FILE_SHARE_DELETE: u32 = 0x0000_0004;
    let mut options = OpenOptions::new();
    options.read(true);
    options.share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE);
    options.open(path).map_err(io_error)
}

#[cfg(windows)]
#[repr(C)]
struct PrefixLockOverlapped {
    internal: usize,
    internal_high: usize,
    offset: u32,
    offset_high: u32,
    event: isize,
}

#[cfg(windows)]
#[link(name = "kernel32")]
unsafe extern "system" {
    fn GetFileInformationByHandleEx(
        file: isize,
        info_class: i32,
        info: *mut std::ffi::c_void,
        info_size: u32,
    ) -> i32;
    fn LockFileEx(
        file: isize,
        flags: u32,
        reserved: u32,
        byte_count_low: u32,
        byte_count_high: u32,
        overlapped: *mut PrefixLockOverlapped,
    ) -> i32;
}

#[cfg(windows)]
fn lock_file_range(file: &File, start: u64, end: u64) -> Result<(), JournalError> {
    if start == end {
        return Ok(());
    }
    let length = end
        .checked_sub(start)
        .ok_or_else(|| corrupt(start, "verified reader lock range underflow"))?;
    let mut overlapped = PrefixLockOverlapped {
        internal: 0,
        internal_high: 0,
        offset: start as u32,
        offset_high: (start >> 32) as u32,
        event: 0,
    };
    // SAFETY: the handle is valid while the session owns `file`; the stack
    // overlapped is used synchronously with FAIL_IMMEDIATELY.
    if unsafe {
        LockFileEx(
            file.as_raw_handle() as isize,
            0x0000_0001,
            0,
            length as u32,
            (length >> 32) as u32,
            &mut overlapped,
        )
    } == 0
    {
        return Err(io_error(std::io::Error::last_os_error()));
    }
    Ok(())
}

fn stream_cursor(
    source: CursorSource<'_>,
    byte_offset: u64,
    coverage: &SequenceCoverage,
    prefix_hash: &str,
    identity: (&Option<String>, &Option<String>),
    layout: (bool, bool, u64, u8),
) -> JournalCursor {
    JournalCursor {
        generation: source.generation,
        manifest_hash: source.manifest_hash.into(),
        segment_kind: source.segment_kind,
        byte_offset,
        raw_sequence: coverage.raw_sequence(),
        prefix_hash: prefix_hash.into(),
        file_identity: source.file_identity.into(),
        worker_id: identity.0.clone(),
        channel_id: identity.1.clone(),
        saw_heartbeat_summary: layout.0,
        saw_compaction_commit: layout.1,
        compaction_next_sequence: layout.2,
        compaction_layout_version: layout.3,
    }
}

fn update_compaction_coverage(
    coverage: &SequenceCoverage,
    commit_seen: bool,
    committed_next_sequence: u64,
    complete: &mut bool,
    offset: u64,
) -> Result<(), JournalError> {
    if !commit_seen || *complete {
        return Ok(());
    }
    match coverage.next_sequence().cmp(&committed_next_sequence) {
        std::cmp::Ordering::Less => Ok(()),
        std::cmp::Ordering::Equal => {
            *complete = true;
            Ok(())
        }
        std::cmp::Ordering::Greater => Err(corrupt(
            offset,
            "compaction commit next sequence is exceeded by retained coverage",
        )),
    }
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

#[cfg(not(windows))]
fn verify_journal_cursor_prefix(path: &Path, cursor: &JournalCursor) -> Result<(), JournalError> {
    let file = File::open(path).map_err(io_error)?;
    verify_journal_cursor_prefix_file(&file, cursor)
}

fn verify_journal_cursor_prefix_file(
    verified_file: &File,
    cursor: &JournalCursor,
) -> Result<(), JournalError> {
    let mut file = verified_file.try_clone().map_err(io_error)?;
    file.seek(SeekFrom::Start(0)).map_err(io_error)?;
    let mut offset = 0_u64;
    let mut coverage = SequenceCoverage::new();
    let mut prefix_hash = initial_prefix_hash();
    let mut worker_id = None;
    let mut channel_id = None;
    let mut summary_seen = false;
    let mut commit_seen = false;
    let mut compaction_next_sequence = 0_u64;
    let mut compaction_layout_version = 0_u8;
    let mut saw_event = false;
    let mut summary_ranges = Vec::new();
    let mut summary_range_index = 0_usize;
    let mut verified_records = 0_u64;
    while offset < cursor.byte_offset {
        let record = read_stream_record(&mut file, offset)?
            .ok_or_else(|| corrupt(offset, "journal ended before the durable reader checkpoint"))?;
        offset = offset
            .checked_add(record.total_bytes)
            .ok_or_else(|| corrupt(offset, "journal offset overflow"))?;
        verified_records = verified_records.saturating_add(1);
        #[cfg(test)]
        pause_prefix_proof_for_multiprocess_test(verified_records);
        if offset > cursor.byte_offset {
            return Err(corrupt(
                cursor.byte_offset,
                "durable reader checkpoint is not on a journal record boundary",
            ));
        }
        prefix_hash = next_prefix_hash(&prefix_hash, &record.raw_bytes);
        match record.kind {
            StreamRecordKind::Event(event) => {
                if summary_seen && !commit_seen {
                    return Err(corrupt(
                        offset,
                        "retained events require a preceding compaction commit; heartbeat summary has no compaction commit",
                    ));
                }
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
                if !summary_seen
                    || commit_seen
                    || saw_event
                    || commit.next_sequence == 0
                    || !matches!(
                        commit.layout_version,
                        None | Some(COMPACTION_LAYOUT_COMMIT_THEN_RETAINED)
                    )
                    || worker_id.as_deref() != Some(&commit.worker_id)
                    || channel_id.as_deref() != Some(&commit.channel_id)
                {
                    return Err(corrupt(
                        offset,
                        "compaction commit does not bind sequence and identity",
                    ));
                }
                commit_seen = true;
                compaction_next_sequence = commit.next_sequence;
                compaction_layout_version = COMPACTION_LAYOUT_COMMIT_THEN_RETAINED;
            }
            StreamRecordKind::FallbackToneAudit(entry) => {
                validate_fallback_tone_audit_record(&entry).map_err(|reason| {
                    corrupt(offset, format!("invalid fallback tone audit: {reason}"))
                })?;
                bind_stream_identity(
                    &mut worker_id,
                    &mut channel_id,
                    fallback_tone_audit_identity(&entry),
                    offset,
                )?;
            }
        }
    }
    if offset != cursor.byte_offset
        || coverage.raw_sequence() != cursor.raw_sequence
        || prefix_hash != cursor.prefix_hash
        || worker_id != cursor.worker_id
        || channel_id != cursor.channel_id
        || summary_seen != cursor.saw_heartbeat_summary
        || commit_seen != cursor.saw_compaction_commit
        || compaction_next_sequence != cursor.compaction_next_sequence
        || compaction_layout_version
            != if cursor.saw_compaction_commit && cursor.compaction_layout_version == 0 {
                COMPACTION_LAYOUT_COMMIT_THEN_RETAINED
            } else {
                cursor.compaction_layout_version
            }
    {
        return Err(corrupt(
            cursor.byte_offset,
            "durable reader checkpoint prefix integrity check failed",
        ));
    }
    Ok(())
}

/// Test-only deterministic barrier proving that a real process holds the
/// cross-process lease while it is still rebuilding a large authenticated
/// prefix, rather than only after the read has returned to its caller.
#[cfg(test)]
fn pause_prefix_proof_for_multiprocess_test(verified_records: u64) {
    use std::sync::atomic::{AtomicBool, Ordering};
    static PAUSED: AtomicBool = AtomicBool::new(false);
    let Some(after) = std::env::var_os("TOWNLIGHT_JOURNAL_PROOF_PAUSE_AFTER") else {
        return;
    };
    let Ok(after) = after.to_string_lossy().parse::<u64>() else {
        return;
    };
    if verified_records < after || PAUSED.swap(true, Ordering::SeqCst) {
        return;
    }
    let (Some(barrier), Some(release)) = (
        std::env::var_os("TOWNLIGHT_JOURNAL_LEASE_BARRIER"),
        std::env::var_os("TOWNLIGHT_JOURNAL_LEASE_RELEASE"),
    ) else {
        return;
    };
    let barrier = PathBuf::from(barrier);
    let release = PathBuf::from(release);
    File::create(barrier).expect("test proof barrier is writable");
    for _ in 0..500 {
        if release.exists() {
            return;
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    panic!("parent did not release the prefix-proof barrier");
}

#[cfg(windows)]
#[repr(C)]
struct WindowsFileId128 {
    bytes: [u8; 16],
}

#[cfg(windows)]
#[repr(C)]
struct WindowsFileIdInfo {
    volume_serial_number: u64,
    file_id: WindowsFileId128,
}

#[cfg(windows)]
fn journal_file_identity(file: &File) -> Result<String, JournalError> {
    const FILE_ID_INFO_CLASS: i32 = 18;
    let mut info = WindowsFileIdInfo {
        volume_serial_number: 0,
        file_id: WindowsFileId128 { bytes: [0; 16] },
    };
    // SAFETY: `file` is live, `info` is correctly sized for FileIdInfo, and
    // Windows fills the output synchronously.
    let result = unsafe {
        GetFileInformationByHandleEx(
            file.as_raw_handle() as isize,
            FILE_ID_INFO_CLASS,
            (&mut info as *mut WindowsFileIdInfo).cast(),
            std::mem::size_of::<WindowsFileIdInfo>() as u32,
        )
    };
    if result == 0 {
        return Err(io_error(std::io::Error::last_os_error()));
    }
    Ok(format!(
        "windows-file-id:{:016x}:{}",
        info.volume_serial_number,
        info.file_id
            .bytes
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>()
    ))
}

#[cfg(unix)]
fn journal_file_identity(file: &File) -> Result<String, JournalError> {
    use std::os::unix::fs::MetadataExt;
    let metadata = file.metadata().map_err(io_error)?;
    Ok(format!("unix:{}:{}", metadata.dev(), metadata.ino()))
}

#[cfg(not(any(windows, unix)))]
fn journal_file_identity(file: &File) -> Result<String, JournalError> {
    Ok(format!(
        "portable:{}",
        file.metadata().map_err(io_error)?.len()
    ))
}

fn read_snapshot(path: &Path) -> Result<JournalSnapshot, JournalError> {
    read_snapshot_bounded(path, usize::MAX)
}

fn read_snapshot_strict(path: &Path) -> Result<JournalSnapshot, JournalError> {
    read_snapshot_bounded_with_manifest_policy(path, usize::MAX, false)
}

fn read_snapshot_bounded(path: &Path, event_limit: usize) -> Result<JournalSnapshot, JournalError> {
    read_snapshot_bounded_with_manifest_policy(path, event_limit, false)
}

fn read_snapshot_bounded_with_manifest_policy(
    path: &Path,
    event_limit: usize,
    allow_live_writer_handoff: bool,
) -> Result<JournalSnapshot, JournalError> {
    let location = resolve_journal_location_with_authority(path, false, allow_live_writer_handoff)?;
    let Some(active_path) = location.active_path else {
        return read_snapshot_bounded_direct(&location.data_path, event_limit);
    };
    let mut sealed = read_snapshot_bounded_direct(&location.data_path, usize::MAX)?;
    let active = read_snapshot_bounded_direct_from(
        &active_path,
        usize::MAX,
        location
            .manifest
            .as_ref()
            .expect("active path requires v2 manifest")
            .sealed_raw_sequence,
    )?;
    if active
        .identity
        .as_ref()
        .is_some_and(|identity| sealed.identity.as_ref() != Some(identity))
    {
        return Err(corrupt(0, "journal active segment identity changed"));
    }
    if sealed.events.len().saturating_add(active.events.len()) > event_limit {
        return Err(JournalError::EventLimitExceeded { limit: event_limit });
    }
    sealed.events.extend(active.events);
    sealed
        .fallback_tone_audit
        .extend(active.fallback_tone_audit);
    fallback_tone_audit_state(&sealed.fallback_tone_audit)
        .map_err(|reason| corrupt(0, format!("invalid fallback tone audit: {reason}")))?;
    sealed.raw_heartbeat_count = sealed
        .events
        .iter()
        .filter(|event| matches!(event.event, WorkerEvent::Heartbeat { .. }))
        .count();
    sealed.next_sequence =
        sequence_after_coverage(sealed.heartbeat_summary.as_ref(), &sealed.events, 0)?;
    Ok(sealed)
}

fn read_snapshot_bounded_direct(
    path: &Path,
    event_limit: usize,
) -> Result<JournalSnapshot, JournalError> {
    read_snapshot_bounded_direct_from(path, event_limit, 0)
}

fn read_snapshot_bounded_direct_from(
    path: &Path,
    event_limit: usize,
    initial_raw_sequence: u64,
) -> Result<JournalSnapshot, JournalError> {
    let file = match File::open(path) {
        Ok(file) => file,
        Err(error) if error.kind() == ErrorKind::NotFound => {
            return Ok(JournalSnapshot {
                events: Vec::new(),
                fallback_tone_audit: Vec::new(),
                heartbeat_summary: None,
                identity: None,
                next_sequence: 1,
                raw_heartbeat_count: 0,
                compaction_committed: false,
                compaction_layout: None,
                compaction_layout_is_explicit: false,
                compaction_next_sequence: 0,
            });
        }
        Err(error) => return Err(io_error(error)),
    };
    read_snapshot_bounded_file(&file, event_limit, initial_raw_sequence)
}

fn read_snapshot_bounded_file(
    file: &File,
    event_limit: usize,
    initial_raw_sequence: u64,
) -> Result<JournalSnapshot, JournalError> {
    let mut file = file.try_clone().map_err(io_error)?;
    file.seek(SeekFrom::Start(0)).map_err(io_error)?;
    let mut events = Vec::new();
    let mut fallback_tone_audit = Vec::new();
    let mut offset = 0_u64;
    let mut identity: Option<(String, String)> = None;
    let mut heartbeat_summary: Option<HeartbeatSummary> = None;
    let mut compaction_commit: Option<CompactionCommit> = None;
    let mut compaction_layout = None;
    let mut saw_event = false;

    loop {
        #[cfg(test)]
        STREAM_RECORD_READS.with(|reads| reads.set(reads.get() + 1));
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
                if commit.next_sequence == 0 {
                    return Err(corrupt(offset, "compaction commit sequence is invalid"));
                }
                let declared_layout_version = commit
                    .layout_version
                    .unwrap_or(COMPACTION_LAYOUT_RETAINED_THEN_COMMIT);
                if !matches!(
                    declared_layout_version,
                    COMPACTION_LAYOUT_RETAINED_THEN_COMMIT | COMPACTION_LAYOUT_COMMIT_THEN_RETAINED
                ) {
                    return Err(corrupt(offset, "compaction commit layout is unsupported"));
                }
                let layout = if saw_event {
                    if declared_layout_version == COMPACTION_LAYOUT_COMMIT_THEN_RETAINED {
                        return Err(corrupt(
                            offset,
                            "compaction layout version requires commit before retained events",
                        ));
                    }
                    CompactionLayout::RetainedThenCommit
                } else {
                    // A v2 commit emitted by the rejected first layout change
                    // had no explicit layout field. Its position is still
                    // unambiguous and is migrated by the next writer open.
                    CompactionLayout::CommitThenRetained
                };
                compaction_commit = Some(commit);
                compaction_layout = Some(layout);
            }
            RECORD_KIND_FALLBACK_TONE_AUDIT if version == JOURNAL_VERSION => {
                let entry: FallbackToneJournalEntry =
                    serde_json::from_slice(&payload).map_err(|error| {
                        corrupt(offset, format!("invalid fallback tone audit: {error}"))
                    })?;
                validate_fallback_tone_audit_record(&entry).map_err(|reason| {
                    corrupt(offset, format!("invalid fallback tone audit: {reason}"))
                })?;
                let audit_identity = fallback_tone_audit_identity(&entry);
                match &identity {
                    Some(expected) if expected != &audit_identity => {
                        return Err(corrupt(offset, "worker or channel identity changed"));
                    }
                    None => identity = Some(audit_identity),
                    _ => {}
                }
                fallback_tone_audit.push(entry);
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
    let next_sequence = sequence_after_coverage_from(
        heartbeat_summary.as_ref(),
        &events,
        offset,
        initial_raw_sequence,
    )?;
    if let Some(commit) = &compaction_commit {
        // Events at or after the declared next sequence are ordinary appends
        // after the atomic compaction. Only the summary plus retained events
        // before that boundary belong to the sealed replacement image.
        let retained: Vec<_> = events
            .iter()
            .filter(|event| event.sequence < commit.next_sequence)
            .cloned()
            .collect();
        let committed_next = sequence_after_coverage_from(
            heartbeat_summary.as_ref(),
            &retained,
            offset,
            initial_raw_sequence,
        )?;
        if commit.next_sequence != committed_next {
            return Err(corrupt(
                offset,
                "compaction commit does not match committed sequence coverage",
            ));
        }
    }
    let raw_heartbeat_count = events
        .iter()
        .filter(|event| matches!(event.event, WorkerEvent::Heartbeat { .. }))
        .count();
    if initial_raw_sequence == 0 {
        fallback_tone_audit_state(&fallback_tone_audit)
            .map_err(|reason| corrupt(offset, format!("invalid fallback tone audit: {reason}")))?;
    }
    Ok(JournalSnapshot {
        events,
        fallback_tone_audit,
        heartbeat_summary,
        identity,
        next_sequence,
        raw_heartbeat_count,
        compaction_committed: compaction_commit.is_some(),
        compaction_layout,
        compaction_layout_is_explicit: compaction_commit
            .as_ref()
            .is_some_and(|commit| commit.layout_version.is_some()),
        compaction_next_sequence: compaction_commit.map_or(0, |commit| commit.next_sequence),
    })
}

fn sequence_after_coverage(
    heartbeat_summary: Option<&HeartbeatSummary>,
    events: &[WorkerEventEnvelope],
    offset: u64,
) -> Result<u64, JournalError> {
    sequence_after_coverage_from(heartbeat_summary, events, offset, 0)
}

fn sequence_after_coverage_from(
    heartbeat_summary: Option<&HeartbeatSummary>,
    events: &[WorkerEventEnvelope],
    offset: u64,
    initial_raw_sequence: u64,
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
    let mut coverage = SequenceCoverage::after_raw(initial_raw_sequence, offset)?;
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
    // Legacy `.compact.tmp` recovery predates immutable generations. Once a
    // manifest is authoritative, such a root-side temporary is unreachable
    // and must never be promoted over the stable rendezvous path.
    if generation_manifest_path(path)
        .try_exists()
        .map_err(io_error)?
    {
        return Ok(());
    }
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

/// Completes the intent sidecar's own fsync-before-rename boundary. An intent
/// carries recovery provenance but is not journal authority: the final
/// candidate is independently re-proven against it before the sole authority
/// operation (manifest publication). A missing or ambiguous candidate is
/// fail-closed.
fn recover_generation_publication_temp(
    root: &Path,
    location: &JournalLocation,
    worker_id: &str,
    channel_id: &str,
) -> Result<(), JournalError> {
    let generation = location
        .generation
        .checked_add(1)
        .ok_or_else(|| corrupt(0, "journal generation exhausted during recovery"))?;
    let Some(intent) = read_generation_publication_temp(root, generation)? else {
        return Ok(());
    };
    let published = generation_publication_path(root, generation);
    if published.try_exists().map_err(io_error)? {
        return Err(corrupt(
            0,
            "journal generation publication has both final and temporary intents",
        ));
    }
    validate_generation_publication_intent_source(root, location, &intent, worker_id, channel_id)?;
    let candidate = generation_path(root, generation);
    let candidate_temp = generation_temp_path(root, generation);
    let candidate_exists = candidate.try_exists().map_err(io_error)?;
    let candidate_temp_exists = candidate_temp.try_exists().map_err(io_error)?;
    if candidate_exists == candidate_temp_exists {
        return Err(corrupt(
            0,
            "journal generation publication temporary lacks one exact candidate",
        ));
    }
    let candidate = if candidate_exists {
        candidate
    } else {
        candidate_temp
    };
    let _ = validate_generation_publication_candidate(
        root, location, &intent, &candidate, worker_id, channel_id,
    )?;
    promote_generation(
        &generation_publication_temp_path(root, generation),
        &published,
    )
    .map_err(io_error)
}

fn open_verified_empty_active_segment(
    path: &Path,
) -> Result<VerifiedEmptyRegularFile, JournalError> {
    open_verified_empty_regular_file(path, "orphan journal active segment", None)
}

fn validate_generation_publication_candidate(
    root: &Path,
    location: &JournalLocation,
    intent: &GenerationPublicationIntent,
    candidate: &Path,
    worker_id: &str,
    channel_id: &str,
) -> Result<JournalCursor, JournalError> {
    validate_generation_publication_intent_source(root, location, intent, worker_id, channel_id)?;
    if file_sha256(candidate)? != intent.candidate_sha256 {
        return Err(corrupt(
            0,
            "journal generation publication candidate content changed",
        ));
    }
    let current = read_snapshot_strict(root)?;
    let candidate_snapshot = read_snapshot_bounded_direct(candidate, usize::MAX)?;
    let current_non_heartbeat: Vec<_> = current
        .events
        .iter()
        .filter(|event| !matches!(event.event, WorkerEvent::Heartbeat { .. }))
        .cloned()
        .collect();
    let candidate_non_heartbeat: Vec<_> = candidate_snapshot
        .events
        .iter()
        .filter(|event| !matches!(event.event, WorkerEvent::Heartbeat { .. }))
        .cloned()
        .collect();
    if candidate_snapshot.next_sequence != current.next_sequence
        || candidate_snapshot.identity != current.identity
        || candidate_non_heartbeat != current_non_heartbeat
        || !candidate_snapshot.compaction_committed
        || candidate_snapshot.compaction_layout != Some(CompactionLayout::CommitThenRetained)
        || !candidate_snapshot.compaction_layout_is_explicit
    {
        return Err(corrupt(
            0,
            "orphan journal generation does not exactly cover the published journal",
        ));
    }
    prove_sealed_generation(candidate, worker_id, channel_id)
}

fn complete_generation_publication(
    root: &Path,
    location: &JournalLocation,
    intent: &GenerationPublicationIntent,
    worker_id: &str,
    channel_id: &str,
) -> Result<(PathBuf, ManifestAuthorityHandle), JournalError> {
    let candidate = generation_path(root, intent.generation);
    if !candidate.try_exists().map_err(io_error)? {
        return Err(corrupt(
            0,
            "journal generation publication is missing its sealed segment",
        ));
    }
    let sealed = validate_generation_publication_candidate(
        root, location, intent, &candidate, worker_id, channel_id,
    )?;
    let active_path = active_segment_path(root, intent.generation);
    let active = if active_path.try_exists().map_err(io_error)? {
        open_verified_empty_active_segment(&active_path)?
    } else {
        create_empty_active_segment(root, intent.generation)?
    };
    // The active segment is durable before the manifest references it. The
    // manifest remains the sole authority publication point.
    let manifest_authority = publish_generation_manifest(
        root,
        intent.generation,
        worker_id,
        channel_id,
        &sealed,
        &active,
    )?;
    // The manifest is now durable authority. An intent-cleanup sharing error
    // must not leave this live writer appending to the old generation; restart
    // recovery will authenticate and retire the harmless published intent.
    let _ = remove_generation_publication_intent(root, intent.generation);
    Ok((active_path, manifest_authority))
}

/// Compatibility recovery for the pre-intent layout. Its old manifest could
/// be lost after a synced sealed copy was placed at the root, leaving the
/// former append suffix at `.active-N`. This does not publish that suffix as a
/// generation: after proving it is the exact contiguous continuation of the
/// restored sealed root, it atomically becomes part of that legacy root.
fn merge_verified_legacy_active_suffix(
    root: &Path,
    active: &Path,
    worker_id: &str,
    channel_id: &str,
) -> Result<(), JournalError> {
    let current = read_snapshot_strict(root)?;
    if !current.compaction_committed
        || current.compaction_layout != Some(CompactionLayout::CommitThenRetained)
        || !current.compaction_layout_is_explicit
        || current
            .identity
            .as_ref()
            .is_none_or(|identity| identity.0 != worker_id || identity.1 != channel_id)
    {
        return Err(corrupt(
            0,
            "legacy active-only recovery has no authenticated sealed root",
        ));
    }
    let suffix = read_snapshot_bounded_direct_from(
        active,
        usize::MAX,
        current.next_sequence.saturating_sub(1),
    )?;
    if suffix.heartbeat_summary.is_some()
        || suffix.compaction_committed
        || suffix.events.is_empty()
        || suffix.identity != current.identity
    {
        return Err(corrupt(
            0,
            "legacy active-only recovery has an invalid append suffix",
        ));
    }
    let mut expected = current.next_sequence;
    for event in &suffix.events {
        if event.sequence != expected {
            return Err(JournalError::NonMonotonic {
                expected,
                actual: event.sequence,
            });
        }
        expected = expected
            .checked_add(1)
            .ok_or_else(|| corrupt(0, "journal sequence exhausted during legacy recovery"))?;
    }
    let bytes = std::fs::read(active).map_err(io_error)?;
    let mut output = OpenOptions::new()
        .append(true)
        .open(root)
        .map_err(io_error)?;
    output.write_all(&bytes).map_err(io_error)?;
    output.flush().map_err(io_error)?;
    output.sync_all().map_err(io_error)?;
    drop(output);
    // Verify the fsynced merged file before retiring the suffix. A failure
    // leaves the suffix intact and no new authority is advertised.
    let merged = read_snapshot_strict(root)?;
    if merged.next_sequence != expected || merged.identity != current.identity {
        return Err(corrupt(
            0,
            "legacy active-only recovery did not preserve journal coverage",
        ));
    }
    std::fs::remove_file(active).map_err(io_error)
}

/// A completed generation can survive a power loss between its atomic rename
/// and manifest publication. Never reuse or overwrite that name: prove the
/// exact orphan against the currently published logical journal and either
/// publish it atomically or stop. Ambiguous debris is evidence of an
/// interrupted/tampered writer and is deliberately fail-closed.
fn recover_orphan_generation(
    root: &Path,
    worker_id: &str,
    channel_id: &str,
) -> Result<Option<ManifestAuthorityHandle>, JournalError> {
    let location = resolve_journal_location_strict(root)?;
    recover_generation_publication_temp(root, &location, worker_id, channel_id)?;
    reject_ambiguous_generation_artifacts(root, location.generation)?;

    // A power loss after manifest replacement but before intent cleanup has
    // already published authority. Check that the durable intent still binds
    // that authority, then retire only the intent. This is deliberately not a
    // second publication or a best-effort cleanup of unverified debris.
    if location.manifest.is_some()
        && let Some(intent) = read_generation_publication_intent(root, location.generation)?
    {
        let manifest = location.manifest.as_ref().expect("checked above");
        if intent.generation != location.generation
            || intent.source_generation.checked_add(1) != Some(location.generation)
            || intent.worker_id != worker_id
            || intent.channel_id != channel_id
            || intent.source_next_sequence != manifest.sealed_compaction_next_sequence
            || file_sha256(&location.data_path)? != intent.candidate_sha256
        {
            return Err(corrupt(
                0,
                "published journal generation does not match its publication intent",
            ));
        }
        // Manifest publication has already succeeded. A temporary sharing
        // failure while cleaning its sidecar is harmless and retried on a
        // later writer open.
        let _ = remove_generation_publication_intent(root, location.generation);
    }

    // A v1 manifest's generation was still appendable. An active sidecar
    // beside it is precisely the crash window after v2 preparation; validate
    // and publish v2 instead of trying to append to the old object again.
    if location.legacy_manifest.is_some() {
        let active = active_segment_path(root, location.generation);
        if !active.try_exists().map_err(io_error)? {
            return Ok(None);
        }
        let active = open_verified_empty_active_segment(&active)?;
        let sealed = prove_sealed_generation(&location.data_path, worker_id, channel_id)?;
        let authority = publish_generation_manifest(
            root,
            location.generation,
            worker_id,
            channel_id,
            &sealed,
            &active,
        )?;
        return Ok(Some(authority));
    }

    let candidate_generation = location
        .generation
        .checked_add(1)
        .ok_or_else(|| corrupt(0, "journal generation exhausted during recovery"))?;
    let candidate = generation_path(root, candidate_generation);
    let active = active_segment_path(root, candidate_generation);
    let temporary = generation_temp_path(root, candidate_generation);
    let candidate_exists = candidate.try_exists().map_err(io_error)?;
    let active_exists = active.try_exists().map_err(io_error)?;
    let temporary_exists = temporary.try_exists().map_err(io_error)?;
    let intent = read_generation_publication_intent(root, candidate_generation)?;
    if !candidate_exists && !active_exists && intent.is_none() {
        // A private compact temp may survive an ordinary in-process rename
        // failure or a crash before the durable intent. It carries no
        // publication authority and is intentionally ignored (not promoted).
        return Ok(None);
    }

    // A legacy pre-intent binary could leave both final files just before the
    // manifest write. That complete state has no one-sided ambiguity, so
    // preserve rolling recovery by deriving an in-memory intent from the
    // exact candidate and then applying the same full validation below.
    let intent = match intent {
        Some(intent) => intent,
        None if candidate_exists && active_exists && !temporary_exists => {
            let current = read_snapshot_strict(root)?;
            publication_intent(
                candidate_generation,
                location.generation,
                &current,
                worker_id,
                channel_id,
                &candidate,
            )?
        }
        None if candidate_exists
            && !active_exists
            && !temporary_exists
            && location.manifest.is_none()
            && root.try_exists().map_err(io_error)? =>
        {
            // A pre-intent writer could leave a sealed object beside a
            // restored legacy `.compact.tmp` after the old manifest was lost.
            // It has no active half and no intent, so it is never authority;
            // retain the validated root rather than trying to promote or
            // fabricate the stale candidate.
            return Ok(None);
        }
        None if !candidate_exists
            && active_exists
            && !temporary_exists
            && location.manifest.is_none() =>
        {
            merge_verified_legacy_active_suffix(root, &active, worker_id, channel_id)?;
            return Ok(None);
        }
        None => {
            return Err(corrupt(
                0,
                "orphan journal generation has no durable publication intent",
            ));
        }
    };

    if candidate_exists && temporary_exists {
        return Err(corrupt(
            0,
            "orphan journal generation has both sealed and temporary candidates",
        ));
    }

    if candidate_exists {
        let (_, authority) =
            complete_generation_publication(root, &location, &intent, worker_id, channel_id)?;
        return Ok(Some(authority));
    }

    if temporary_exists {
        // The candidate was fsynced and its intent was atomically published,
        // but its first rename did not happen. Validate it while private,
        // discard a proven-empty active-only artifact if one exists, then
        // promote exactly those bytes.
        if active_exists {
            let active_proof = open_verified_empty_active_segment(&active)?;
            drop(active_proof);
            std::fs::remove_file(&active).map_err(io_error)?;
        }
        let _ = validate_generation_publication_candidate(
            root, &location, &intent, &temporary, worker_id, channel_id,
        )?;
        promote_generation(&temporary, &candidate).map_err(io_error)?;
        let (_, authority) =
            complete_generation_publication(root, &location, &intent, worker_id, channel_id)?;
        return Ok(Some(authority));
    }

    if active_exists {
        // An active segment has no sealed authority. It can only be cleaned
        // when the synced intent proves it belongs to this exact pending
        // generation and the current source is unchanged. Never synthesize a
        // sealed generation from it.
        let current = read_snapshot_strict(root)?;
        let expected_generation = location
            .generation
            .checked_add(1)
            .ok_or_else(|| corrupt(0, "journal generation exhausted during recovery"))?;
        if intent.generation != expected_generation
            || intent.source_generation != location.generation
            || intent.worker_id != worker_id
            || intent.channel_id != channel_id
            || current.next_sequence != intent.source_next_sequence
            || current.identity.as_ref().is_none_or(|identity| {
                identity.0 != intent.source_identity_worker_id
                    || identity.1 != intent.source_identity_channel_id
            })
        {
            return Err(corrupt(
                0,
                "active-only journal generation does not have exact provenance",
            ));
        }
        let active_proof = open_verified_empty_active_segment(&active)?;
        drop(active_proof);
        std::fs::remove_file(&active).map_err(io_error)?;
        remove_generation_publication_intent(root, candidate_generation)?;
        return Ok(None);
    }

    Err(corrupt(
        0,
        "orphan journal generation publication has no recoverable candidate",
    ))
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

/// Atomically replaces the manifest using the exact file object that was
/// fsynced and authenticated above. A pathname `MoveFileEx` here would reopen
/// a brief source-substitution window after the manifest temp was validated.
#[cfg(windows)]
fn replace_manifest_from_exact_handle(
    file: File,
    temporary: &Path,
    destination: &Path,
    expected_bytes: &[u8],
) -> Result<ManifestAuthorityHandle, JournalError> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{
        FILE_RENAME_INFO, FileRenameInfo, SetFileInformationByHandle,
    };

    // This exact durable handle intentionally omits FILE_SHARE_DELETE. Its
    // no-delete sharing survives FileRenameInfo, so a process cannot swap
    // either the temp at the pre-rename boundary or the final manifest at
    // either post-rename boundary while this proof handle remains live.
    let source_identity = journal_file_identity(&file)?;
    replace_manifest_temp_at_promotion_boundary(temporary);
    let destination = if destination.is_absolute() {
        destination.to_path_buf()
    } else {
        std::env::current_dir().map_err(io_error)?.join(destination)
    };
    let name = destination.as_os_str().encode_wide().collect::<Vec<_>>();
    let header_bytes = std::mem::offset_of!(FILE_RENAME_INFO, FileName);
    let info_bytes = header_bytes
        .checked_add(
            name.len()
                .checked_mul(std::mem::size_of::<u16>())
                .ok_or_else(|| corrupt(0, "journal generation manifest path is too long"))?,
        )
        .ok_or_else(|| corrupt(0, "journal generation manifest path is too long"))?;
    let info_length = u32::try_from(info_bytes)
        .map_err(|_| corrupt(0, "journal generation manifest path is too long"))?;
    let words = info_bytes
        .checked_add(std::mem::size_of::<usize>() - 1)
        .ok_or_else(|| corrupt(0, "journal generation manifest path is too long"))?
        / std::mem::size_of::<usize>();
    // `FILE_RENAME_INFO` contains pointer-sized fields; keep its flexible
    // buffer suitably aligned instead of casting a byte-aligned allocation.
    let mut info = vec![0_usize; words];
    let rename = info.as_mut_ptr().cast::<FILE_RENAME_INFO>();
    // SAFETY: `info` is exactly the FILE_RENAME_INFO prefix plus the UTF-16
    // filename payload. The handle was opened with DELETE access, and the
    // destination is an absolute, non-NUL UTF-16 path.
    let status = unsafe {
        (*rename).Anonymous.ReplaceIfExists = true;
        (*rename).RootDirectory = std::ptr::null_mut();
        (*rename).FileNameLength = u32::try_from(name.len() * std::mem::size_of::<u16>())
            .expect("bounded manifest path length fits u32");
        std::ptr::copy_nonoverlapping(name.as_ptr(), (*rename).FileName.as_mut_ptr(), name.len());
        SetFileInformationByHandle(
            file.as_raw_handle(),
            FileRenameInfo,
            rename.cast(),
            info_length,
        )
    };
    if status == 0 {
        let error = std::io::Error::last_os_error();
        return Err(corrupt(
            0,
            format!("could not atomically publish journal generation manifest: {error}"),
        ));
    }
    // Flush the same handle after the rename, preserving the old
    // MoveFileEx(MOVEFILE_WRITE_THROUGH) durability boundary before readers
    // can treat the new manifest name as authority.
    file.sync_all().map_err(io_error)?;
    reject_manifest_replacement_while_authoritative(&destination);
    let expected_manifest_hash = manifest_hash(expected_bytes);
    verify_manifest_path_matches_retained_handle(
        &file,
        &destination,
        expected_bytes,
        &source_identity,
    )?;
    reject_manifest_replacement_while_authoritative(&destination);
    Ok(ManifestAuthorityHandle {
        file,
        file_identity: source_identity,
        manifest_hash: expected_manifest_hash,
    })
}

#[cfg(not(windows))]
fn replace_manifest_from_exact_handle(
    file: File,
    temporary: &Path,
    destination: &Path,
    expected_bytes: &[u8],
) -> Result<ManifestAuthorityHandle, JournalError> {
    replace_manifest_temp_at_promotion_boundary(temporary);
    replace_file(temporary, destination).map_err(io_error)?;
    if std::fs::read(destination).map_err(io_error)? != expected_bytes {
        return Err(corrupt(
            0,
            "published journal generation manifest changed during promotion",
        ));
    }
    let (authority, _) = open_manifest_authority_handle(
        destination,
        destination,
        Some(&manifest_hash(expected_bytes)),
        false,
    )?;
    Ok(authority)
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
    use std::process::Command;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::thread;
    use std::time::Duration;
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
    fn failed_generation_publication_keeps_the_old_writer_and_root_authoritative() {
        let path = test_path();
        let temporary = generation_temp_path(&path, 1);
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
        assert!(writer.file.is_some());
        assert!(path.is_file());
        assert!(temporary.is_file());
        writer
            .append(&event(514, WorkerEvent::ShutdownComplete))
            .unwrap();
        drop(writer);

        let reopened = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        assert_eq!(reopened.next_sequence(), 515);
        assert!(temporary.exists());
        drop(reopened);
        let _ = std::fs::remove_file(path);
        let _ = std::fs::remove_file(temporary);
    }

    #[test]
    fn post_authority_release_failure_retires_writer_until_restart_recovery() {
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

        schedule_post_authority_release_failure(path.clone());
        assert!(matches!(
            writer.append(&event(
                514,
                WorkerEvent::Heartbeat {
                    monotonic_milliseconds: 514 * 5_000,
                    outputs: Vec::new(),
                },
            )),
            Err(JournalError::Corrupt { reason, .. })
                if reason.contains("injected failure after journal manifest authority release")
        ));
        assert!(writer.file.is_none());
        assert!(writer.ownership.is_none());
        assert!(writer.manifest_authority.is_none());
        assert!(matches!(
            writer.append(&event(515, WorkerEvent::ShutdownComplete)),
            Err(JournalError::Io(reason)) if reason.contains("unavailable")
        ));
        drop(writer);

        // Restart is the sole recovery route after release: it authenticates
        // the intent/candidate pair and publishes a contiguous generation
        // before accepting any new append.
        let mut recovered = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        assert_eq!(recovered.next_sequence(), 515);
        recovered
            .append(&event(515, WorkerEvent::ShutdownComplete))
            .unwrap();
        drop(recovered);
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

        let snapshot = read_snapshot(&path).unwrap();
        assert_eq!(
            snapshot.compaction_layout,
            Some(CompactionLayout::CommitThenRetained)
        );
        assert!(snapshot.compaction_layout_is_explicit);
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
        let commit = serde_json::to_vec(&CompactionCommit {
            worker_id: WORKER_ID.into(),
            channel_id: CHANNEL_ID.into(),
            next_sequence: committed_next_sequence,
            layout_version: Some(COMPACTION_LAYOUT_COMMIT_THEN_RETAINED),
        })
        .unwrap();
        write_record(&mut file, RECORD_KIND_COMPACTION_COMMIT, &commit).unwrap();
        for envelope in retained {
            let frame = encode_event_frame(envelope).unwrap();
            write_record(&mut file, RECORD_KIND_EVENT, &frame).unwrap();
        }
        for envelope in tail {
            let frame = encode_event_frame(envelope).unwrap();
            write_record(&mut file, RECORD_KIND_EVENT, &frame).unwrap();
        }
        file.sync_all().unwrap();
    }

    fn write_legacy_compacted_fixture(
        path: &Path,
        retained: &[WorkerEventEnvelope],
        committed_next_sequence: u64,
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
            layout_version: Some(COMPACTION_LAYOUT_RETAINED_THEN_COMMIT),
        })
        .unwrap();
        write_record(&mut file, RECORD_KIND_COMPACTION_COMMIT, &commit).unwrap();
        file.sync_all().unwrap();
    }

    #[test]
    fn writer_open_migrates_a_legacy_v2_compaction_layout() {
        let path = test_path();
        write_legacy_compacted_fixture(
            &path,
            &[event(2, WorkerEvent::Ready { graph_revision: 1 })],
            3,
        );
        assert_eq!(read_events(&path).unwrap()[0].sequence, 2);
        let writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        assert_eq!(writer.next_sequence(), 3);
        drop(writer);
        let snapshot = read_snapshot(&path).unwrap();
        assert_eq!(
            snapshot.compaction_layout,
            Some(CompactionLayout::CommitThenRetained)
        );
        assert!(snapshot.compaction_layout_is_explicit);
        assert_eq!(snapshot.compaction_next_sequence, 3);
        assert_eq!(read_events(&path).unwrap()[0].sequence, 2);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn writer_recovers_and_migrates_a_legacy_v2_compaction_temp() {
        let path = test_path();
        let temporary = compaction_temp_path(&path);
        write_legacy_compacted_fixture(
            &temporary,
            &[event(2, WorkerEvent::Ready { graph_revision: 1 })],
            3,
        );
        let writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        assert_eq!(writer.next_sequence(), 3);
        drop(writer);
        assert!(path.is_file());
        assert!(!temporary.exists());
        assert_eq!(
            read_snapshot(&path).unwrap().compaction_layout,
            Some(CompactionLayout::CommitThenRetained)
        );
        assert!(read_snapshot(&path).unwrap().compaction_layout_is_explicit);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn writer_migrates_v1_generation_manifest_without_resuming_its_byte_cursor() {
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
        let location = resolve_journal_location(&path).unwrap();
        let sealed = sealed_manifest_cursor(&location).unwrap();
        let legacy = LegacyJournalGenerationManifest {
            version: LEGACY_GENERATION_MANIFEST_VERSION,
            generation: location.generation,
            worker_id: WORKER_ID.into(),
            channel_id: CHANNEL_ID.into(),
            file_identity: sealed.file_identity.clone(),
            sealed_next_sequence: sealed.compaction_next_sequence,
        };
        std::fs::remove_file(location.active_path.as_ref().unwrap()).unwrap();
        std::fs::write(
            generation_manifest_path(&path),
            serde_json::to_vec(&legacy).unwrap(),
        )
        .unwrap();
        let legacy_cursor = read_journal_incremental(&path, None, 1, false)
            .unwrap()
            .cursor;
        assert!(matches!(
            read_journal_incremental(&path, Some(&legacy_cursor), 1, true),
            Err(JournalError::Corrupt { reason, .. }) if reason.contains("requires writer migration")
        ));
        forget_verified_prefix_session(&path);

        let mut migrated = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        let after = migrated.next_sequence();
        migrated
            .append(&event(after, WorkerEvent::ShutdownComplete))
            .unwrap();
        drop(migrated);

        let location = resolve_journal_location(&path).unwrap();
        assert!(location.manifest.is_some());
        assert!(location.active_path.as_ref().unwrap().is_file());
        assert_eq!(read_events(&path).unwrap().last().unwrap().sequence, after);
    }

    #[test]
    fn writer_recovers_completed_orphan_generation_after_promotion_before_manifest() {
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
        let current = read_snapshot(&path).unwrap();
        let location = resolve_journal_location(&path).unwrap();
        let orphan_generation = location.generation + 1;
        let orphan = generation_path(&path, orphan_generation);
        // This is the durable state after candidate promotion and active
        // creation, but before the atomic manifest replacement.
        write_compacted_image(&orphan, &current, WORKER_ID, CHANNEL_ID).unwrap();
        create_empty_active_segment(&path, orphan_generation).unwrap();

        let mut recovered = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        assert_eq!(recovered.generation, orphan_generation);
        let after = recovered.next_sequence();
        recovered
            .append(&event(after, WorkerEvent::ShutdownComplete))
            .unwrap();
        drop(recovered);
        let resolved = resolve_journal_location(&path).unwrap();
        assert_eq!(resolved.generation, orphan_generation);
        assert!(resolved.manifest.is_some());
        assert_eq!(read_events(&path).unwrap().last().unwrap().sequence, after);
    }

    fn stage_generation_publication(
        path: &Path,
    ) -> (u64, GenerationPublicationIntent, JournalSnapshot) {
        let location = resolve_journal_location(path).unwrap();
        let current = read_snapshot(path).unwrap();
        let generation = location.generation + 1;
        let compact_source = JournalSnapshot {
            events: current
                .events
                .iter()
                .filter(|event| !matches!(event.event, WorkerEvent::Heartbeat { .. }))
                .cloned()
                .collect(),
            fallback_tone_audit: current.fallback_tone_audit.clone(),
            heartbeat_summary: Some(compacted_summary()),
            identity: current.identity.clone(),
            next_sequence: current.next_sequence,
            raw_heartbeat_count: 0,
            compaction_committed: true,
            compaction_layout: Some(CompactionLayout::CommitThenRetained),
            compaction_layout_is_explicit: true,
            compaction_next_sequence: current.next_sequence,
        };
        let candidate = generation_path(path, generation);
        write_compacted_image(&candidate, &compact_source, WORKER_ID, CHANNEL_ID).unwrap();
        let intent = publication_intent(
            generation,
            location.generation,
            &compact_source,
            WORKER_ID,
            CHANNEL_ID,
            &candidate,
        )
        .unwrap();
        write_generation_publication_intent(path, &intent).unwrap();
        (generation, intent, current)
    }

    #[test]
    fn writer_recovers_a_sealed_only_generation_after_promotion_before_active() {
        let path = test_path();
        let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        writer
            .append(&event(
                1,
                WorkerEvent::Heartbeat {
                    monotonic_milliseconds: 5_000,
                    outputs: Vec::new(),
                },
            ))
            .unwrap();
        writer
            .append(&event(2, WorkerEvent::Ready { graph_revision: 1 }))
            .unwrap();
        drop(writer);

        let (generation, _, current) = stage_generation_publication(&path);
        assert!(generation_path(&path, generation).exists());
        assert!(!active_segment_path(&path, generation).exists());

        let mut recovered = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        assert_eq!(recovered.generation, generation);
        assert_eq!(recovered.next_sequence(), current.next_sequence);
        recovered
            .append(&event(current.next_sequence, WorkerEvent::ShutdownComplete))
            .unwrap();
        drop(recovered);

        let resolved = resolve_journal_location(&path).unwrap();
        assert_eq!(resolved.generation, generation);
        assert!(resolved.active_path.unwrap().exists());
        assert!(!generation_publication_path(&path, generation).exists());
        assert_eq!(
            read_events(&path).unwrap().last().unwrap().sequence,
            current.next_sequence
        );
    }

    #[test]
    fn writer_recovers_an_empty_synced_active_temp_before_its_promotion() {
        let path = test_path();
        let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        writer
            .append(&event(
                1,
                WorkerEvent::Heartbeat {
                    monotonic_milliseconds: 5_000,
                    outputs: Vec::new(),
                },
            ))
            .unwrap();
        writer
            .append(&event(2, WorkerEvent::Ready { graph_revision: 1 }))
            .unwrap();
        drop(writer);

        let (generation, _, current) = stage_generation_publication(&path);
        let active_temp = active_segment_temp_path(&path, generation);
        let active_temp_file = File::create(&active_temp).unwrap();
        active_temp_file.sync_all().unwrap();
        drop(active_temp_file);

        let recovered = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        assert_eq!(recovered.generation, generation);
        assert_eq!(recovered.next_sequence(), current.next_sequence);
        drop(recovered);
        assert!(active_segment_path(&path, generation).exists());
        assert!(!active_temp.exists());
        assert!(!generation_publication_path(&path, generation).exists());
    }

    #[test]
    fn writer_rejects_active_temp_swapped_after_validation_before_manifest_publication() {
        let path = test_path();
        let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        writer
            .append(&event(
                1,
                WorkerEvent::Heartbeat {
                    monotonic_milliseconds: 5_000,
                    outputs: Vec::new(),
                },
            ))
            .unwrap();
        writer
            .append(&event(2, WorkerEvent::Ready { graph_revision: 1 }))
            .unwrap();
        drop(writer);

        let (generation, _, _) = stage_generation_publication(&path);
        let active_temp = active_segment_temp_path(&path, generation);
        let active_temp_file = File::create(&active_temp).unwrap();
        active_temp_file.sync_all().unwrap();
        drop(active_temp_file);
        // This is a CRC-valid journal, not merely garbage. Before the fix the
        // path-only temp check accepted the empty file, then renamed these
        // bytes into the active authority and wrote a manifest for them.
        let replacement = path.with_extension("active-temp-swap-journal");
        std::fs::copy(&path, &replacement).unwrap();
        schedule_active_temp_replacement(active_temp.clone(), replacement);

        assert!(matches!(
            JournalWriter::open(&path, WORKER_ID, CHANNEL_ID),
            Err(JournalError::Corrupt { reason, .. }) if reason.contains("not exactly empty")
                || reason.contains("changed before manifest publication")
        ));
        assert!(!generation_manifest_path(&path).exists());
        assert!(generation_publication_path(&path, generation).exists());
    }

    #[test]
    fn writer_rejects_an_empty_active_temp_replacement_with_a_different_file_id() {
        let path = test_path();
        let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        writer
            .append(&event(
                1,
                WorkerEvent::Heartbeat {
                    monotonic_milliseconds: 5_000,
                    outputs: Vec::new(),
                },
            ))
            .unwrap();
        writer
            .append(&event(2, WorkerEvent::Ready { graph_revision: 1 }))
            .unwrap();
        drop(writer);

        let (generation, _, _) = stage_generation_publication(&path);
        let active_temp = active_segment_temp_path(&path, generation);
        File::create(&active_temp).unwrap().sync_all().unwrap();
        let replacement = path.with_extension("active-temp-swap-empty");
        File::create(&replacement).unwrap().sync_all().unwrap();
        schedule_active_temp_replacement(active_temp, replacement);

        assert!(matches!(
            JournalWriter::open(&path, WORKER_ID, CHANNEL_ID),
            Err(JournalError::Corrupt { reason, .. })
                if reason.contains("changed before manifest publication")
        ));
        assert!(!generation_manifest_path(&path).exists());
    }

    #[test]
    fn writer_rejects_a_nonempty_active_temp_without_publishing_it() {
        let path = test_path();
        let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        writer
            .append(&event(
                1,
                WorkerEvent::Heartbeat {
                    monotonic_milliseconds: 5_000,
                    outputs: Vec::new(),
                },
            ))
            .unwrap();
        writer
            .append(&event(2, WorkerEvent::Ready { graph_revision: 1 }))
            .unwrap();
        drop(writer);

        let (generation, _, _) = stage_generation_publication(&path);
        let active_temp = active_segment_temp_path(&path, generation);
        let mut active_temp_file = File::create(&active_temp).unwrap();
        active_temp_file.write_all(b"not empty").unwrap();
        active_temp_file.sync_all().unwrap();
        drop(active_temp_file);

        assert!(matches!(
            JournalWriter::open(&path, WORKER_ID, CHANNEL_ID),
            Err(JournalError::Corrupt { reason, .. }) if reason.contains("not exactly empty")
        ));
        assert!(!active_segment_path(&path, generation).exists());
        assert!(active_temp.exists());
        assert!(generation_publication_path(&path, generation).exists());
        assert!(!generation_manifest_path(&path).exists());
    }

    #[test]
    fn writer_rejects_a_wrong_generation_active_temp_before_publication() {
        let path = test_path();
        let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        writer
            .append(&event(1, WorkerEvent::Ready { graph_revision: 1 }))
            .unwrap();
        drop(writer);

        let wrong_generation_temp = active_segment_temp_path(&path, 2);
        let wrong_generation_file = File::create(&wrong_generation_temp).unwrap();
        wrong_generation_file.sync_all().unwrap();
        drop(wrong_generation_file);

        assert!(matches!(
            JournalWriter::open(&path, WORKER_ID, CHANNEL_ID),
            Err(JournalError::Corrupt { reason, .. })
                if reason.contains("multiple future generation")
        ));
        assert!(wrong_generation_temp.exists());
        assert!(!generation_manifest_path(&path).exists());
    }

    #[cfg(windows)]
    #[test]
    fn writer_rejects_a_reparse_active_temp_without_publishing_it() {
        use std::os::windows::fs::symlink_file;

        let path = test_path();
        let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        writer
            .append(&event(
                1,
                WorkerEvent::Heartbeat {
                    monotonic_milliseconds: 5_000,
                    outputs: Vec::new(),
                },
            ))
            .unwrap();
        writer
            .append(&event(2, WorkerEvent::Ready { graph_revision: 1 }))
            .unwrap();
        drop(writer);

        let (generation, _, _) = stage_generation_publication(&path);
        let target = path.with_extension("active-temp-target");
        File::create(&target).unwrap().sync_all().unwrap();
        let active_temp = active_segment_temp_path(&path, generation);
        if let Err(error) = symlink_file(&target, &active_temp) {
            if error.raw_os_error() == Some(1314) {
                return;
            }
            panic!("could not create active-temp reparse fixture: {error}");
        }

        assert!(matches!(
            JournalWriter::open(&path, WORKER_ID, CHANNEL_ID),
            Err(JournalError::Corrupt { reason, .. })
                if reason.contains("regular non-reparse")
        ));
        assert!(!active_segment_path(&path, generation).exists());
        assert!(active_temp.exists());
        assert!(!generation_manifest_path(&path).exists());
    }

    #[test]
    fn writer_recovers_a_synced_intent_temp_before_its_promotion() {
        let path = test_path();
        let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        writer
            .append(&event(
                1,
                WorkerEvent::Heartbeat {
                    monotonic_milliseconds: 5_000,
                    outputs: Vec::new(),
                },
            ))
            .unwrap();
        writer
            .append(&event(2, WorkerEvent::Ready { graph_revision: 1 }))
            .unwrap();
        drop(writer);

        let (generation, _, current) = stage_generation_publication(&path);
        let candidate = generation_path(&path, generation);
        let candidate_temp = generation_temp_path(&path, generation);
        std::fs::rename(&candidate, &candidate_temp).unwrap();
        let intent = generation_publication_path(&path, generation);
        let intent_temp = generation_publication_temp_path(&path, generation);
        std::fs::rename(&intent, &intent_temp).unwrap();

        let recovered = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        assert_eq!(recovered.generation, generation);
        assert_eq!(recovered.next_sequence(), current.next_sequence);
        drop(recovered);
        assert!(generation_manifest_path(&path).exists());
        assert!(!candidate_temp.exists());
        assert!(!intent_temp.exists());
    }

    #[test]
    fn writer_regenerates_a_manifest_temp_only_after_reproving_the_candidate() {
        let path = test_path();
        let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        writer
            .append(&event(
                1,
                WorkerEvent::Heartbeat {
                    monotonic_milliseconds: 5_000,
                    outputs: Vec::new(),
                },
            ))
            .unwrap();
        writer
            .append(&event(2, WorkerEvent::Ready { graph_revision: 1 }))
            .unwrap();
        drop(writer);

        let (generation, _, _) = stage_generation_publication(&path);
        create_empty_active_segment(&path, generation).unwrap();
        let manifest_temp = generation_manifest_temp_path(&path);
        let mut file = File::create(&manifest_temp).unwrap();
        file.write_all(b"untrusted manifest temp").unwrap();
        file.sync_all().unwrap();
        drop(file);

        let recovered = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        assert_eq!(recovered.generation, generation);
        drop(recovered);
        assert!(generation_manifest_path(&path).exists());
        assert!(!manifest_temp.exists());
    }

    #[test]
    fn writer_locks_manifest_authority_across_every_promotion_boundary() {
        if std::env::var_os(MANIFEST_AUTHORITY_SWAP_CHILD).is_some() {
            let source = PathBuf::from(std::env::var_os(MANIFEST_AUTHORITY_SWAP_SOURCE).unwrap());
            let destination =
                PathBuf::from(std::env::var_os(MANIFEST_AUTHORITY_SWAP_DESTINATION).unwrap());
            assert_manifest_replacement_is_blocked_locally(&source, &destination);
            return;
        }
        let path = test_path();
        let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        writer
            .append(&event(
                1,
                WorkerEvent::Heartbeat {
                    monotonic_milliseconds: 5_000,
                    outputs: Vec::new(),
                },
            ))
            .unwrap();
        writer
            .append(&event(2, WorkerEvent::Ready { graph_revision: 1 }))
            .unwrap();
        drop(writer);

        let (generation, _, _) = stage_generation_publication(&path);
        let active = create_empty_active_segment(&path, generation).unwrap();
        let sealed =
            prove_sealed_generation(&generation_path(&path, generation), WORKER_ID, CHANNEL_ID)
                .unwrap();
        let temporary = generation_manifest_temp_path(&path);
        let replacement = path.with_extension("manifest-temp-swap");
        let mut replacement_file = File::create(&replacement).unwrap();
        replacement_file
            .write_all(b"untrusted manifest replacement")
            .unwrap();
        replacement_file.sync_all().unwrap();
        drop(replacement_file);
        schedule_manifest_temp_replacement(temporary.clone(), replacement);

        let after_rename = path.with_extension("manifest-post-rename-swap");
        std::fs::write(&after_rename, b"untrusted post-rename manifest replacement").unwrap();
        schedule_manifest_authority_replacement(
            generation_manifest_path(&path),
            after_rename.clone(),
        );
        let after_check = path.with_extension("manifest-post-check-swap");
        std::fs::write(&after_check, b"untrusted post-check manifest replacement").unwrap();
        schedule_manifest_authority_replacement(
            generation_manifest_path(&path),
            after_check.clone(),
        );

        let authority =
            publish_generation_manifest(&path, generation, WORKER_ID, CHANNEL_ID, &sealed, &active)
                .unwrap();
        drop(active);
        // The two child-process attempts above ran after rename/before proof
        // and after proof/before this release. Only after the final proof may
        // ordinary readers reopen the authority pathname.
        drop(authority);

        let manifest_bytes = std::fs::read(generation_manifest_path(&path)).unwrap();
        assert_ne!(manifest_bytes, b"untrusted manifest replacement");
        let location = resolve_journal_location(&path).unwrap();
        assert_eq!(location.generation, generation);
        assert!(!temporary.exists(), "the exact locked temp was promoted");
        assert!(after_rename.exists());
        assert!(after_check.exists());

        // Releasing the retained authority is the only point at which a
        // later normal publisher may replace the manifest. A second exact
        // publication succeeds after that release, proving the lock does not
        // wedge the next generation/update.
        let active = open_verified_empty_regular_file(
            &active_segment_path(&path, generation),
            "journal active segment",
            None,
        )
        .unwrap();
        let next_authority =
            publish_generation_manifest(&path, generation, WORKER_ID, CHANNEL_ID, &sealed, &active)
                .expect("the next legitimate manifest publisher runs after authority release");
        drop(next_authority);
        drop(active);

        // A normal long-lived worker retains the just-published manifest,
        // rather than leaving an at-rest replacement gap. A concurrent
        // reporter still opens the exact authority via its reader lease, but
        // a separate process cannot replace it until that worker releases the
        // generation for a coordinated future publication.
        let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        // Force a writer-owned publication. Its retained source handle has
        // DELETE access (needed for FileRenameInfo) but intentionally omits
        // DELETE sharing, so a concurrent reporter must duplicate the writer
        // authority rather than independently reopening the manifest.
        for sequence in 3..=515 {
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
        let published_generation = writer.generation;
        let while_writer_live = path.with_extension("manifest-writer-live-swap");
        std::fs::write(
            &while_writer_live,
            b"untrusted live-writer manifest replacement",
        )
        .unwrap();
        assert_manifest_replacement_is_blocked(
            &while_writer_live,
            &generation_manifest_path(&path),
        );
        let report = read_journal_incremental(&path, None, 8, true).unwrap();
        assert_eq!(report.cursor.generation, published_generation);
        // The reader duplicates the writer's exact no-delete handle after
        // checking the sidecar's FileId/hash. It remains authoritative across
        // writer retirement through the persistence acknowledgement.
        verify_incremental_reader_authority(&path, &report.cursor).unwrap();
        schedule_writer_authority_retirement_interleaving(path.clone(), report.cursor.clone());
        let retiring = thread::spawn(move || drop(writer));
        let (manifest_hash, manifest_identity) =
            retained_manifest_authority_binding(&path).expect("reporter holds manifest authority");
        for _ in 0..500 {
            if !writer_owns_matching_manifest_authority(
                &path,
                Some(&manifest_hash),
                Some(&manifest_identity),
            )
            .unwrap()
            {
                break;
            }
            thread::sleep(Duration::from_millis(1));
        }
        assert!(
            !writer_owns_matching_manifest_authority(
                &path,
                Some(&manifest_hash),
                Some(&manifest_identity),
            )
            .unwrap(),
            "writer retirement did not durably invalidate its sidecar"
        );
        // This models the exact time of check/time of persistence interleave:
        // the reporter has successfully verified its cursor, the writer has
        // retired its sidecar, and a separate process tries to replace the
        // manifest before projection commit/acknowledgement. The writer's
        // retained no-delete source authority must still deny the move.
        assert_manifest_replacement_is_blocked(
            &while_writer_live,
            &generation_manifest_path(&path),
        );
        // Acknowledgement is issued only after projection persistence in the
        // production adapter. It releases the lease, then writer retirement
        // may close the no-delete authority.
        acknowledge_incremental_reader(&path);
        retiring
            .join()
            .expect("writer retirement completes after acknowledgement");
    }

    #[cfg(windows)]
    #[test]
    fn writer_startup_refuses_an_untrusted_shared_manifest_handle() {
        let path = test_path();
        let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        for sequence in 1..=513 {
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

        let manifest = generation_manifest_path(&path);
        assert!(manifest.exists());
        let mut conflict = OpenOptions::new();
        conflict.read(true).write(true);
        // This is not a trusted writer authority. Startup must not fall back
        // to a permissive share mode merely because another process happened
        // to have WRITE/DELETE access open on the manifest.
        conflict.access_mode(0x8000_0000 | 0x4000_0000 | 0x0001_0000);
        conflict.share_mode(0x0000_0001 | 0x0000_0002 | 0x0000_0004);
        let conflict = conflict.open(&manifest).unwrap();
        assert!(matches!(
            JournalWriter::open(&path, WORKER_ID, CHANNEL_ID),
            Err(JournalError::Io(_))
        ));
        drop(conflict);

        let recovered = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        drop(recovered);
    }

    #[cfg(windows)]
    #[test]
    fn reporter_refuses_shared_manifest_without_a_live_writer_authority() {
        let path = test_path();
        let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        for sequence in 1..=513 {
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

        let manifest = generation_manifest_path(&path);
        let mut conflict = OpenOptions::new();
        conflict.read(true).write(true);
        conflict.access_mode(0x8000_0000 | 0x4000_0000 | 0x0001_0000);
        conflict.share_mode(0x0000_0001 | 0x0000_0002 | 0x0000_0004);
        let conflict = conflict.open(&manifest).unwrap();
        assert!(read_journal_incremental(&path, None, 8, true).is_err());
        drop(conflict);
    }

    #[test]
    fn writer_discards_a_proven_active_only_generation_without_fabricating_authority() {
        let path = test_path();
        let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        writer
            .append(&event(
                1,
                WorkerEvent::Heartbeat {
                    monotonic_milliseconds: 5_000,
                    outputs: Vec::new(),
                },
            ))
            .unwrap();
        writer
            .append(&event(2, WorkerEvent::Ready { graph_revision: 1 }))
            .unwrap();
        drop(writer);

        let (generation, _, current) = stage_generation_publication(&path);
        std::fs::remove_file(generation_path(&path, generation)).unwrap();
        create_empty_active_segment(&path, generation).unwrap();

        let mut recovered = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        assert_eq!(recovered.generation, 0);
        assert_eq!(recovered.next_sequence(), current.next_sequence);
        assert!(!active_segment_path(&path, generation).exists());
        assert!(!generation_publication_path(&path, generation).exists());
        recovered
            .append(&event(current.next_sequence, WorkerEvent::ShutdownComplete))
            .unwrap();
        drop(recovered);
        assert!(!generation_manifest_path(&path).exists());
        assert_eq!(
            read_events(&path).unwrap().last().unwrap().sequence,
            current.next_sequence
        );
    }

    #[test]
    fn writer_recovers_both_generation_files_before_manifest_from_the_publication_intent() {
        let path = test_path();
        let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        writer
            .append(&event(
                1,
                WorkerEvent::Heartbeat {
                    monotonic_milliseconds: 5_000,
                    outputs: Vec::new(),
                },
            ))
            .unwrap();
        writer
            .append(&event(2, WorkerEvent::Ready { graph_revision: 1 }))
            .unwrap();
        drop(writer);

        let (generation, _, current) = stage_generation_publication(&path);
        create_empty_active_segment(&path, generation).unwrap();

        let recovered = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        assert_eq!(recovered.generation, generation);
        assert_eq!(recovered.next_sequence(), current.next_sequence);
        drop(recovered);
        assert!(generation_manifest_path(&path).exists());
        assert!(!generation_publication_path(&path, generation).exists());
    }

    #[test]
    fn writer_retires_a_verified_intent_after_manifest_publication_crash_boundary() {
        let path = test_path();
        let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        writer
            .append(&event(
                1,
                WorkerEvent::Heartbeat {
                    monotonic_milliseconds: 5_000,
                    outputs: Vec::new(),
                },
            ))
            .unwrap();
        writer
            .append(&event(2, WorkerEvent::Ready { graph_revision: 1 }))
            .unwrap();
        drop(writer);

        let (generation, _, current) = stage_generation_publication(&path);
        let active = create_empty_active_segment(&path, generation).unwrap();
        let sealed =
            prove_sealed_generation(&generation_path(&path, generation), WORKER_ID, CHANNEL_ID)
                .unwrap();
        publish_generation_manifest(&path, generation, WORKER_ID, CHANNEL_ID, &sealed, &active)
            .unwrap();
        drop(active);

        let recovered = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        assert_eq!(recovered.generation, generation);
        assert_eq!(recovered.next_sequence(), current.next_sequence);
        drop(recovered);
        assert!(!generation_publication_path(&path, generation).exists());
        assert_eq!(read_events(&path).unwrap().len(), 1);
    }

    #[test]
    fn writer_fails_closed_for_stale_or_multiple_future_generation_artifacts() {
        let path = test_path();
        let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        writer
            .append(&event(1, WorkerEvent::Ready { graph_revision: 1 }))
            .unwrap();
        drop(writer);

        std::fs::write(generation_path(&path, 2), b"unreachable future").unwrap();
        assert!(matches!(
            JournalWriter::open(&path, WORKER_ID, CHANNEL_ID),
            Err(JournalError::Corrupt { reason, .. })
                if reason.contains("multiple future generation")
        ));
        std::fs::remove_file(generation_path(&path, 2)).unwrap();

        std::fs::write(generation_publication_path(&path, 0), b"stale").unwrap();
        assert!(matches!(
            JournalWriter::open(&path, WORKER_ID, CHANNEL_ID),
            Err(JournalError::Corrupt { reason, .. })
                if reason.contains("stale generation publication intent")
        ));
    }

    #[test]
    fn writer_marks_the_prior_implicit_seal_before_retained_layout_explicit() {
        let path = test_path();
        let mut file = File::create(&path).unwrap();
        write_record(
            &mut file,
            RECORD_KIND_HEARTBEAT_SUMMARY,
            &serde_json::to_vec(&compacted_summary()).unwrap(),
        )
        .unwrap();
        write_record(
            &mut file,
            RECORD_KIND_COMPACTION_COMMIT,
            &serde_json::to_vec(&CompactionCommit {
                worker_id: WORKER_ID.into(),
                channel_id: CHANNEL_ID.into(),
                next_sequence: 3,
                layout_version: None,
            })
            .unwrap(),
        )
        .unwrap();
        write_record(
            &mut file,
            RECORD_KIND_EVENT,
            &encode_event_frame(&event(2, WorkerEvent::Ready { graph_revision: 1 })).unwrap(),
        )
        .unwrap();
        file.sync_all().unwrap();
        drop(file);

        let writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        assert_eq!(writer.next_sequence(), 3);
        drop(writer);
        assert!(read_snapshot(&path).unwrap().compaction_layout_is_explicit);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn bounded_compaction_catch_up_advances_without_rescanning_many_retained_events() {
        let path = test_path();
        let retained: Vec<_> = (2..=10_001)
            .map(|sequence| event(sequence, WorkerEvent::Ready { graph_revision: 1 }))
            .collect();
        write_compacted_fixture(&path, &retained, 10_002, &[]);
        let file_length = std::fs::metadata(&path).unwrap().len();

        reset_stream_record_reads();
        let mut batch = read_journal_incremental(&path, None, 32, true).unwrap();
        let first_pass_reads = stream_record_reads();
        assert_eq!(batch.events.len(), 32);
        assert_eq!(batch.events[0].sequence, 2);
        assert_eq!(batch.cursor.raw_sequence, 33);
        assert!(
            first_pass_reads > 0,
            "production verification must account for physical journal records"
        );
        assert!(
            first_pass_reads <= 160,
            "the initial production pass may establish an authenticated prefix, but must stay bounded: {first_pass_reads} records"
        );
        assert!(batch.cursor.saw_compaction_commit);
        assert_eq!(
            batch.cursor.compaction_layout_version,
            COMPACTION_LAYOUT_COMMIT_THEN_RETAINED
        );
        assert!(
            batch.cursor.byte_offset < file_length / 100,
            "the first bounded batch must not scan the retained suffix"
        );

        let mut expected = 34_u64;
        let mut total_reads = first_pass_reads;
        loop {
            let reads_before = stream_record_reads();
            let next = read_journal_incremental(&path, Some(&batch.cursor), 32, true).unwrap();
            let pass_reads = stream_record_reads() - reads_before;
            assert!(
                pass_reads <= 80,
                "a production batch may read its bounded suffix twice, not replay history: {pass_reads} records"
            );
            total_reads += pass_reads;
            for event in &next.events {
                assert_eq!(event.sequence, expected);
                expected += 1;
            }
            batch = next;
            if batch.events.is_empty() {
                break;
            }
        }
        assert_eq!(expected, 10_002);
        assert_eq!(batch.cursor.raw_sequence, 10_001);
        assert!(
            total_reads <= retained.len() as u64 * 4 + 200,
            "production catch-up must be O(N), not prefix-rescan quadratic: {total_reads} records for {} retained events",
            retained.len()
        );
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn production_verified_reader_does_not_replay_a_locked_prefix_on_idle_or_append() {
        let path = test_path();
        let mut file = File::create(&path).unwrap();
        for envelope in [
            event(1, WorkerEvent::Ready { graph_revision: 1 }),
            event(2, WorkerEvent::ShutdownComplete),
        ] {
            write_record(
                &mut file,
                RECORD_KIND_EVENT,
                &encode_event_frame(&envelope).unwrap(),
            )
            .unwrap();
        }
        file.sync_all().unwrap();
        drop(file);

        let initial = read_journal_incremental(&path, None, 8, true).unwrap();
        assert_eq!(initial.events.len(), 2);

        reset_stream_record_reads();
        let idle = read_journal_incremental(&path, Some(&initial.cursor), 8, true).unwrap();
        assert!(idle.events.is_empty());
        assert_eq!(idle.cursor, initial.cursor);
        let idle_reads = stream_record_reads();
        assert!(
            (1..=2).contains(&idle_reads),
            "an idle production pass must not rescan its authenticated prefix"
        );

        let mut file = OpenOptions::new().append(true).open(&path).unwrap();
        write_record(
            &mut file,
            RECORD_KIND_EVENT,
            &encode_event_frame(&event(3, WorkerEvent::Ready { graph_revision: 2 })).unwrap(),
        )
        .unwrap();
        file.sync_all().unwrap();
        drop(file);

        reset_stream_record_reads();
        let appended = read_journal_incremental(&path, Some(&initial.cursor), 8, true).unwrap();
        assert_eq!(
            appended
                .events
                .iter()
                .map(|event| event.sequence)
                .collect::<Vec<_>>(),
            vec![3]
        );
        let append_reads = stream_record_reads();
        assert!(
            (2..=4).contains(&append_reads),
            "an append pass must inspect only the new suffix"
        );
        let _ = std::fs::remove_file(path);
    }

    #[cfg(windows)]
    #[test]
    fn manifest_generation_restart_proves_once_then_reads_only_bounded_suffixes() {
        let path = test_path();
        let mut file = File::create(&path).unwrap();
        // Avoid per-event fsync in the setup. The writer's next heartbeat
        // compacts this large retained history into generation one.
        for sequence in 1..=513 {
            write_record(
                &mut file,
                RECORD_KIND_EVENT,
                &encode_event_frame(&event(
                    sequence,
                    WorkerEvent::Heartbeat {
                        monotonic_milliseconds: sequence * 5_000,
                        outputs: Vec::new(),
                    },
                ))
                .unwrap(),
            )
            .unwrap();
        }
        for sequence in 514..=10_513 {
            write_record(
                &mut file,
                RECORD_KIND_EVENT,
                &encode_event_frame(&event(sequence, WorkerEvent::Ready { graph_revision: 1 }))
                    .unwrap(),
            )
            .unwrap();
        }
        file.sync_all().unwrap();
        drop(file);
        let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        writer
            .append(&event(
                10_514,
                WorkerEvent::Heartbeat {
                    monotonic_milliseconds: 10_514 * 5_000,
                    outputs: Vec::new(),
                },
            ))
            .unwrap();
        drop(writer);
        assert!(generation_manifest_path(&path).exists());

        let mut batch = read_journal_incremental(&path, None, 256, true).unwrap();
        while !batch.events.is_empty() {
            acknowledge_incremental_reader(&path);
            batch = read_journal_incremental(&path, Some(&batch.cursor), 256, true).unwrap();
        }
        let checkpoint = batch.cursor;
        assert_eq!(checkpoint.generation, 1);
        forget_verified_prefix_session(&path);

        reset_stream_record_reads();
        let restarted = read_journal_incremental(&path, Some(&checkpoint), 256, true).unwrap();
        let restart_reads = stream_record_reads();
        assert!(restarted.events.is_empty());
        assert!(
            restart_reads > 10_000,
            "a true restart must fully prove the large authenticated generation once: {restart_reads}"
        );
        acknowledge_incremental_reader(&path);

        reset_stream_record_reads();
        let idle = read_journal_incremental(&path, Some(&checkpoint), 256, true).unwrap();
        let idle_reads = stream_record_reads();
        assert!(idle.events.is_empty());
        assert!(
            (1..=2).contains(&idle_reads),
            "an idle manifest-generation pass must not rescan a historical prefix: {idle_reads}"
        );
        acknowledge_incremental_reader(&path);

        let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        writer
            .append(&event(10_515, WorkerEvent::ShutdownComplete))
            .unwrap();
        drop(writer);
        reset_stream_record_reads();
        let appended = read_journal_incremental(&path, Some(&checkpoint), 256, true).unwrap();
        let append_reads = stream_record_reads();
        assert_eq!(
            appended.events,
            vec![event(10_515, WorkerEvent::ShutdownComplete)]
        );
        assert!(
            (2..=4).contains(&append_reads),
            "an append pass must only read its new suffix: {append_reads}"
        );
        forget_verified_prefix_session(&path);
        let current = generation_path(&path, 1);
        let _ = std::fs::remove_file(current);
        let _ = std::fs::remove_file(generation_manifest_path(&path));
        let _ = std::fs::remove_file(path);
    }

    #[cfg(windows)]
    #[test]
    fn manifest_identity_rejects_a_replaced_generation_before_cursor_advance() {
        let path = test_path();
        let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
        for sequence in 1..=513 {
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
        writer
            .append(&event(514, WorkerEvent::Ready { graph_revision: 1 }))
            .unwrap();
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
        let checkpoint = read_journal_incremental(&path, None, 8, true)
            .unwrap()
            .cursor;
        forget_verified_prefix_session(&path);
        let active = generation_path(&path, 1);
        let replacement = test_path();
        std::fs::copy(&active, &replacement).unwrap();
        replace_file(&replacement, &active).unwrap();

        assert!(matches!(
            read_journal_incremental(&path, Some(&checkpoint), 8, true),
            Err(JournalError::Corrupt { reason, .. })
                if reason.contains("published journal generation identity changed")
        ));
        let _ = std::fs::remove_file(active);
        let _ = std::fs::remove_file(generation_manifest_path(&path));
        let _ = std::fs::remove_file(path);
    }

    #[cfg(windows)]
    #[test]
    fn production_verified_reader_rejects_a_crc_valid_pre_cursor_rewrite_after_restart() {
        let path = test_path();
        let first = event(1, WorkerEvent::Ready { graph_revision: 1 });
        let second = event(2, WorkerEvent::ShutdownComplete);
        let mut file = File::create(&path).unwrap();
        for envelope in [&first, &second] {
            write_record(
                &mut file,
                RECORD_KIND_EVENT,
                &encode_event_frame(envelope).unwrap(),
            )
            .unwrap();
        }
        file.sync_all().unwrap();
        drop(file);

        // This is the actual authority mode: the live reader locks the
        // authenticated prefix. Drop only the in-process handle to model a
        // process restart, when the next authority pass must rebuild that
        // proof before it can advance the persisted checkpoint.
        let checkpoint = read_journal_incremental(&path, None, 1, true)
            .unwrap()
            .cursor;
        forget_verified_prefix_session(&path);

        let mut file = File::create(&path).unwrap();
        write_record(
            &mut file,
            RECORD_KIND_EVENT,
            &encode_event_frame(&event(1, WorkerEvent::Ready { graph_revision: 9 })).unwrap(),
        )
        .unwrap();
        write_record(
            &mut file,
            RECORD_KIND_EVENT,
            &encode_event_frame(&second).unwrap(),
        )
        .unwrap();
        file.sync_all().unwrap();
        drop(file);

        reset_stream_record_reads();
        let result = read_journal_incremental(&path, Some(&checkpoint), 8, true);
        assert!(
            matches!(
                &result,
                Err(JournalError::Corrupt { reason, .. }) if reason.contains("prefix integrity")
            ),
            "expected authenticated pre-cursor rewrite rejection, got {result:?}"
        );
        assert!(
            stream_record_reads() > 0,
            "the restarted authority must authenticate the persisted prefix before state advance"
        );
        let _ = std::fs::remove_file(path);
    }

    #[cfg(windows)]
    #[test]
    fn verified_prefix_lock_blocks_mutation_allows_append_and_rejects_path_swap() {
        let path = test_path();
        let mut file = File::create(&path).unwrap();
        for envelope in [
            event(1, WorkerEvent::Ready { graph_revision: 1 }),
            event(2, WorkerEvent::ShutdownComplete),
        ] {
            write_record(
                &mut file,
                RECORD_KIND_EVENT,
                &encode_event_frame(&envelope).unwrap(),
            )
            .unwrap();
        }
        file.sync_all().unwrap();
        drop(file);

        let checkpoint = read_journal_incremental(&path, None, 1, true)
            .unwrap()
            .cursor;

        // LockFileEx must reject a second writer's direct in-range mutation,
        // while leaving the unprotected suffix available to the journal's
        // normal append-only writer.
        let mut mutator = OpenOptions::new().write(true).open(&path).unwrap();
        mutator.seek(SeekFrom::Start(0)).unwrap();
        assert!(
            mutator.write_all(b"X").is_err(),
            "the verified prefix lock must deny writes into authoritative bytes"
        );
        drop(mutator);

        let mut appender = OpenOptions::new().append(true).open(&path).unwrap();
        write_record(
            &mut appender,
            RECORD_KIND_EVENT,
            &encode_event_frame(&event(3, WorkerEvent::Ready { graph_revision: 2 })).unwrap(),
        )
        .unwrap();
        appender.sync_all().unwrap();
        drop(appender);

        // Windows refuses an atomic replace while the reader owns a locked
        // prefix. The regular writer avoids this operation by publishing a
        // new generation through the manifest; an attempted same-path swap
        // remains rejected and cannot bind a stale checkpoint.
        let replacement = test_path();
        let mut file = File::create(&replacement).unwrap();
        for envelope in [
            event(1, WorkerEvent::Ready { graph_revision: 1 }),
            event(2, WorkerEvent::ShutdownComplete),
            event(3, WorkerEvent::Ready { graph_revision: 2 }),
        ] {
            write_record(
                &mut file,
                RECORD_KIND_EVENT,
                &encode_event_frame(&envelope).unwrap(),
            )
            .unwrap();
        }
        file.sync_all().unwrap();
        drop(file);
        assert!(
            replace_file(&replacement, &path).is_err(),
            "a live verified prefix lock must prevent a replacement swap"
        );
        forget_verified_prefix_session(&path);
        replace_file(&replacement, &path).unwrap();

        assert!(matches!(
            read_journal_incremental(&path, Some(&checkpoint), 8, true),
            Err(JournalError::Corrupt { reason, .. }) if reason.contains("identity changed")
        ));
        let _ = std::fs::remove_file(path);
    }

    #[cfg(windows)]
    #[test]
    fn worker_defers_compaction_while_another_process_reader_lease_is_live() {
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

        // The exclusive sidecar is observed by an independently opened
        // writer handle exactly as it would be by stationd/channel-worker.
        let lease = acquire_reader_lease(&path).unwrap();
        writer
            .append(&event(
                514,
                WorkerEvent::Heartbeat {
                    monotonic_milliseconds: 514 * 5_000,
                    outputs: Vec::new(),
                },
            ))
            .unwrap();
        assert!(
            writer.raw_heartbeat_count > RETAIN_RAW_HEARTBEATS,
            "a live reporter lease defers replacement but never makes the worker fatal"
        );

        drop(lease);
        std::fs::remove_file(reader_lease_path(&path)).unwrap();
        writer
            .append(&event(
                515,
                WorkerEvent::Heartbeat {
                    monotonic_milliseconds: 515 * 5_000,
                    outputs: Vec::new(),
                },
            ))
            .unwrap();
        assert!(
            writer.raw_heartbeat_count <= RETAIN_RAW_HEARTBEATS,
            "compaction must reduce the writer's retained heartbeat count"
        );
        drop(writer);
        let compacted = read_snapshot(&path).unwrap();
        assert!(compacted.compaction_committed);
        assert!(compacted.raw_heartbeat_count <= RETAIN_RAW_HEARTBEATS);
        let _ = std::fs::remove_file(path);
    }

    #[cfg(windows)]
    #[test]
    fn multiprocess_reporter_lease_defers_then_allows_compaction() {
        const CHILD: &str = "TOWNLIGHT_JOURNAL_LEASE_CHILD";
        const WORKER: &str = "TOWNLIGHT_JOURNAL_WORKER_CHILD";
        const JOURNAL: &str = "TOWNLIGHT_JOURNAL_LEASE_PATH";
        const BARRIER: &str = "TOWNLIGHT_JOURNAL_LEASE_BARRIER";
        const RELEASE: &str = "TOWNLIGHT_JOURNAL_LEASE_RELEASE";
        const WORKER_DONE: &str = "TOWNLIGHT_JOURNAL_WORKER_DONE";
        const SEQUENCE: &str = "TOWNLIGHT_JOURNAL_WORKER_SEQUENCE";
        if std::env::var_os(WORKER).is_some() {
            let journal = PathBuf::from(std::env::var_os(JOURNAL).unwrap());
            let sequence = std::env::var(SEQUENCE).unwrap().parse::<u64>().unwrap();
            let release = PathBuf::from(std::env::var_os(RELEASE).unwrap());
            let done = PathBuf::from(std::env::var_os(WORKER_DONE).unwrap());
            let mut writer = JournalWriter::open(&journal, WORKER_ID, CHANNEL_ID).unwrap();
            writer
                .append(&event(
                    sequence,
                    WorkerEvent::Heartbeat {
                        monotonic_milliseconds: sequence * 5_000,
                        outputs: Vec::new(),
                    },
                ))
                .unwrap();
            // A writer that has deferred compaction must retain its manifest
            // authority until the reporter finishes its durable transaction.
            // Tell the parent that append returned, then let `Drop` run only
            // after the reporter acknowledgement has been requested.
            File::create(done).unwrap();
            for _ in 0..500 {
                if release.exists() {
                    return;
                }
                thread::sleep(Duration::from_millis(10));
            }
            panic!("parent did not release the reporter transaction");
        }
        if std::env::var_os(CHILD).is_some() {
            let journal = PathBuf::from(std::env::var_os(JOURNAL).unwrap());
            let barrier = PathBuf::from(std::env::var_os(BARRIER).unwrap());
            let release = PathBuf::from(std::env::var_os(RELEASE).unwrap());
            // Use the production reader, not a test-only sidecar. It holds a
            // cross-process lease and an authenticated generation session
            // from its read through the parent-controlled acknowledgement.
            let checkpoint = read_journal_incremental(&journal, None, usize::MAX, true).unwrap();
            assert!(checkpoint.events.len() > 10_000);
            File::create(barrier).unwrap();
            for _ in 0..500 {
                if release.exists() {
                    acknowledge_incremental_reader(&journal);
                    return;
                }
                thread::sleep(Duration::from_millis(10));
            }
            panic!("parent did not acknowledge the reporter lease");
        }

        let path = test_path();
        let barrier = test_path();
        let release = test_path();
        let worker_done = test_path();
        // A real process will restart against this large legacy root and
        // pause inside its full authenticated-prefix proof. The worker then
        // reaches a real heartbeat compaction while that lease is live.
        let mut source = File::create(&path).unwrap();
        for sequence in 1..=513 {
            write_record(
                &mut source,
                RECORD_KIND_EVENT,
                &encode_event_frame(&event(
                    sequence,
                    WorkerEvent::Heartbeat {
                        monotonic_milliseconds: sequence * 5_000,
                        outputs: Vec::new(),
                    },
                ))
                .unwrap(),
            )
            .unwrap();
        }
        for sequence in 514..=10_513 {
            write_record(
                &mut source,
                RECORD_KIND_EVENT,
                &encode_event_frame(&event(sequence, WorkerEvent::Ready { graph_revision: 1 }))
                    .unwrap(),
            )
            .unwrap();
        }
        source.sync_all().unwrap();
        drop(source);
        let mut child = Command::new(std::env::current_exe().unwrap())
            .arg("--exact")
            .arg("tests::multiprocess_reporter_lease_defers_then_allows_compaction")
            .env(CHILD, "1")
            .env(JOURNAL, &path)
            .env(BARRIER, &barrier)
            .env(RELEASE, &release)
            .env("TOWNLIGHT_JOURNAL_PROOF_PAUSE_AFTER", "128")
            .spawn()
            .unwrap();
        for _ in 0..500 {
            if barrier.exists() {
                break;
            }
            thread::sleep(Duration::from_millis(10));
        }
        assert!(barrier.exists(), "child reporter did not acquire its lease");

        let mut worker = Command::new(std::env::current_exe().unwrap())
            .arg("--exact")
            .arg("tests::multiprocess_reporter_lease_defers_then_allows_compaction")
            .env(WORKER, "1")
            .env(JOURNAL, &path)
            .env(RELEASE, &release)
            .env(WORKER_DONE, &worker_done)
            .env(SEQUENCE, "10514")
            .spawn()
            .unwrap();
        for _ in 0..500 {
            if worker_done.exists() {
                break;
            }
            thread::sleep(Duration::from_millis(10));
        }
        assert!(
            worker_done.exists(),
            "worker must defer compaction rather than fatal-exit"
        );
        assert!(read_snapshot(&path).unwrap().raw_heartbeat_count > RETAIN_RAW_HEARTBEATS);
        assert!(
            path.exists() && !generation_manifest_path(&path).exists(),
            "the old generation remains named while the reporter lease is live"
        );

        File::create(&release).unwrap();
        assert!(child.wait().unwrap().success());
        assert!(worker.wait().unwrap().success());
        let worker = Command::new(std::env::current_exe().unwrap())
            .arg("--exact")
            .arg("tests::multiprocess_reporter_lease_defers_then_allows_compaction")
            .env(WORKER, "1")
            .env(JOURNAL, &path)
            .env(RELEASE, &release)
            .env(WORKER_DONE, &worker_done)
            .env(SEQUENCE, "10515")
            .status()
            .unwrap();
        assert!(worker.success());
        let compacted = read_snapshot(&path).unwrap();
        assert!(compacted.compaction_committed);
        assert_eq!(compacted.next_sequence, 10_516);
        assert!(generation_manifest_path(&path).exists());
        assert!(generation_path(&path, 1).exists());
        assert!(
            !path.exists(),
            "the old root generation is reclaimed only after reporter acknowledgement"
        );
        let _ = std::fs::remove_file(generation_path(&path, 1));
        let _ = std::fs::remove_file(generation_manifest_path(&path));
        let _ = std::fs::remove_file(barrier);
        let _ = std::fs::remove_file(release);
        let _ = std::fs::remove_file(worker_done);
    }

    #[cfg(windows)]
    #[test]
    fn reporter_manifest_authority_survives_abrupt_writer_process_death() {
        const WRITER: &str = "TOWNLIGHT_JOURNAL_ABRUPT_WRITER";
        const JOURNAL: &str = "TOWNLIGHT_JOURNAL_ABRUPT_PATH";
        const READY: &str = "TOWNLIGHT_JOURNAL_ABRUPT_READY";
        if std::env::var_os(WRITER).is_some() {
            let journal = PathBuf::from(std::env::var_os(JOURNAL).unwrap());
            let ready = PathBuf::from(std::env::var_os(READY).unwrap());
            let mut writer = JournalWriter::open(&journal, WORKER_ID, CHANNEL_ID).unwrap();
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
            assert!(generation_manifest_path(&journal).exists());
            File::create(ready).unwrap();
            loop {
                thread::sleep(Duration::from_secs(1));
            }
        }

        let path = test_path();
        let ready = test_path();
        let mut writer = Command::new(std::env::current_exe().unwrap())
            .arg("--exact")
            .arg("tests::reporter_manifest_authority_survives_abrupt_writer_process_death")
            .env(WRITER, "1")
            .env(JOURNAL, &path)
            .env(READY, &ready)
            .spawn()
            .unwrap();
        for _ in 0..500 {
            if ready.exists() {
                break;
            }
            thread::sleep(Duration::from_millis(10));
        }
        assert!(ready.exists(), "writer child did not publish its manifest");

        // The reporter obtains a duplicate of the live writer's no-delete
        // source handle. The final authority proof succeeds before the
        // worker is terminated, exactly matching the verify-to-persist gap.
        let report = read_journal_incremental(&path, None, 8, true).unwrap();
        verify_incremental_reader_authority(&path, &report.cursor).unwrap();
        writer.kill().unwrap();
        let status = writer.wait().unwrap();
        assert!(!status.success(), "the writer must terminate without Drop");

        let replacement = path.with_extension("abrupt-writer-manifest-swap");
        std::fs::write(&replacement, b"untrusted manifest replacement").unwrap();
        // The source process is gone, so this is protected only by the
        // reporter's duplicated no-delete authority. A separate process must
        // still be unable to replace the manifest before durable commit/abort.
        assert_manifest_replacement_is_blocked(&replacement, &generation_manifest_path(&path));

        // A successful projection commit would acknowledge here. Once the
        // reporter releases its duplicate, normal replacement is possible.
        acknowledge_incremental_reader(&path);
        std::fs::rename(&replacement, generation_manifest_path(&path)).unwrap();

        let _ = std::fs::remove_file(generation_manifest_path(&path));
        let _ = std::fs::remove_file(generation_path(&path, 1));
        let _ = std::fs::remove_file(active_segment_path(&path, 1));
        let _ = std::fs::remove_file(writer_lock_path(&path));
        let _ = std::fs::remove_file(reader_lease_path(&path));
        let _ = std::fs::remove_file(path);
        let _ = std::fs::remove_file(ready);
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
    fn bounded_reader_never_releases_an_exact_limit_batch_before_compaction_is_sealed() {
        let path = test_path();
        let mut file = File::create(&path).unwrap();
        let summary = serde_json::to_vec(&compacted_summary()).unwrap();
        write_record(&mut file, RECORD_KIND_HEARTBEAT_SUMMARY, &summary).unwrap();
        let frame =
            encode_event_frame(&event(2, WorkerEvent::Ready { graph_revision: 1 })).unwrap();
        write_record(&mut file, RECORD_KIND_EVENT, &frame).unwrap();
        file.sync_all().unwrap();

        // The one retained frame is exactly the caller's bounded limit. A
        // naive reader would return it and let its durable consumer persist a
        // checkpoint before discovering that the compaction was never sealed.
        assert!(matches!(
            read_journal_incremental(&path, None, 1, false),
            Err(JournalError::Corrupt { reason, .. })
                if reason.contains("retained events require a preceding compaction commit")
        ));
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn bounded_reader_rejects_a_late_compaction_commit_before_checkpointing_the_exact_cap() {
        let path = test_path();
        let mut file = File::create(&path).unwrap();
        let summary = serde_json::to_vec(&compacted_summary()).unwrap();
        write_record(&mut file, RECORD_KIND_HEARTBEAT_SUMMARY, &summary).unwrap();
        write_record(
            &mut file,
            RECORD_KIND_EVENT,
            &encode_event_frame(&event(2, WorkerEvent::Ready { graph_revision: 1 })).unwrap(),
        )
        .unwrap();
        let late_commit = serde_json::to_vec(&CompactionCommit {
            worker_id: WORKER_ID.into(),
            channel_id: CHANNEL_ID.into(),
            next_sequence: 3,
            layout_version: Some(COMPACTION_LAYOUT_COMMIT_THEN_RETAINED),
        })
        .unwrap();
        write_record(&mut file, RECORD_KIND_COMPACTION_COMMIT, &late_commit).unwrap();
        file.sync_all().unwrap();

        // The retained event is exactly the caller's cap. A late, otherwise
        // CRC-valid seal must not let this call return a checkpoint whose
        // prefix does not authenticate that seal.
        assert!(matches!(
            read_journal_incremental(&path, None, 1, false),
            Err(JournalError::Corrupt { reason, .. })
                if reason.contains("retained events require a preceding compaction commit")
        ));
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn prefix_verification_rejects_a_crc_valid_same_file_rewrite_before_cursor() {
        let path = test_path();
        let first = event(1, WorkerEvent::Ready { graph_revision: 1 });
        let second = event(2, WorkerEvent::ShutdownComplete);
        let mut file = File::create(&path).unwrap();
        write_record(
            &mut file,
            RECORD_KIND_EVENT,
            &encode_event_frame(&first).unwrap(),
        )
        .unwrap();
        write_record(
            &mut file,
            RECORD_KIND_EVENT,
            &encode_event_frame(&second).unwrap(),
        )
        .unwrap();
        file.sync_all().unwrap();
        drop(file);
        let cursor = read_journal_incremental(&path, None, 1, false)
            .unwrap()
            .cursor;

        // Rewrite the existing file in place with independently CRC-valid
        // frames and preserve the valid suffix. The durable prefix hash, not
        // a parse error or file identity change, must catch this mutation.
        let replacement = event(1, WorkerEvent::Ready { graph_revision: 2 });
        let mut file = File::create(&path).unwrap();
        write_record(
            &mut file,
            RECORD_KIND_EVENT,
            &encode_event_frame(&replacement).unwrap(),
        )
        .unwrap();
        write_record(
            &mut file,
            RECORD_KIND_EVENT,
            &encode_event_frame(&second).unwrap(),
        )
        .unwrap();
        file.sync_all().unwrap();
        drop(file);
        assert!(matches!(
            read_journal_incremental(&path, Some(&cursor), 8, true),
            Err(JournalError::Corrupt { reason, .. })
                if reason.contains("prefix integrity")
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
