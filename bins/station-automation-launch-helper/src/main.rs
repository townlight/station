//! Separately killable, deliberately fail-closed launch-helper boundary.
//!
//! `stationd` must only start this fixed installed helper in a fresh
//! kill-on-close Job Object. It has no registered semantic adapter, never
//! opens request-named artifacts, and never creates an output or emits a
//! success receipt.

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::io::{Read, Write};
use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Component, Path, PathBuf};
use std::sync::mpsc;
use std::time::{Duration, Instant};
use windows_sys::Win32::Storage::FileSystem::{
    FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_READ,
};

const MAX_REQUEST_BYTES: usize = 64 * 1024;
const MAX_ARTIFACT_BYTES: u64 = 128 * 1024 * 1024 * 1024;
const REQUEST_READ_DEADLINE: Duration = Duration::from_secs(5);
const MAX_ACTION_MS: u64 = 60_000;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct LaunchRequest {
    job_id: String,
    claim_id: String,
    adapter_id: String,
    adapter_revision: u64,
    candidate_version: String,
    candidate_commit: String,
    install_generation: u64,
    configuration_sha256: String,
    root_authority_sha256: String,
    input_relative_path: PathBuf,
    input_sha256: String,
    input_bytes: u64,
    output_relative_path: PathBuf,
    action_timeout_ms: u64,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ManagedRootAuthority {
    authority_id: String,
    revision: u64,
    input_root: PathBuf,
    output_root: PathBuf,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "snake_case")]
enum LaunchStatus {
    AdapterUnavailable,
    Rejected,
}

#[derive(Debug, Serialize)]
struct LaunchResponse {
    status: LaunchStatus,
    code: &'static str,
}

fn main() {
    let started = Instant::now();
    let response = match read_root_authority()
        .and_then(|authority| read_request().map(|request| (authority, request)))
        .and_then(|(authority, request)| {
            validate_request(request).map(|request| (authority, request))
        })
        .and_then(|(authority, request)| validate_unavailable_request(authority, request, started))
    {
        Ok(()) => LaunchResponse {
            status: LaunchStatus::AdapterUnavailable,
            code: "adapter_unavailable",
        },
        Err(()) => LaunchResponse {
            status: LaunchStatus::Rejected,
            code: "launch_rejected",
        },
    };
    // The protocol has no diagnostic field. Never echo paths, prompts,
    // secrets, provider messages, or serialized request content.
    let _ = std::io::stdout()
        .write_all(&serde_json::to_vec(&response).expect("fixed response serializes"));
}

fn read_request() -> Result<LaunchRequest, ()> {
    // The framed stdin read is independently watchdog-bounded inside this
    // helper process. Root-authority, directory, hash, and sync I/O can still
    // block inside the helper; only a containing outer Job host can impose the
    // complete hard deadline by terminating and reaping this process tree.
    let (sender, receiver) = mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let result = (|| {
            let mut header = [0_u8; 4];
            std::io::stdin().read_exact(&mut header).map_err(|_| ())?;
            let length = usize::try_from(u32::from_le_bytes(header)).map_err(|_| ())?;
            if length == 0 || length > MAX_REQUEST_BYTES {
                return Err(());
            }
            let mut bytes = vec![0_u8; length];
            std::io::stdin().read_exact(&mut bytes).map_err(|_| ())?;
            serde_json::from_slice(&bytes).map_err(|_| ())
        })();
        let _ = sender.send(result);
    });
    receiver
        .recv_timeout(REQUEST_READ_DEADLINE)
        .map_err(|_| ())?
}

fn read_root_authority() -> Result<(ManagedRootAuthority, String), ()> {
    let mut args = std::env::args_os().skip(1);
    if args.next().as_deref() != Some(std::ffi::OsStr::new("--root-authority")) {
        return Err(());
    }
    let path = PathBuf::from(args.next().ok_or(())?);
    if args.next().as_deref() != Some(std::ffi::OsStr::new("--root-authority-sha256")) {
        return Err(());
    }
    let expected = args.next().ok_or(())?.to_string_lossy().into_owned();
    if args.next().is_some() || !path.is_absolute() || !is_sha(&expected) {
        return Err(());
    }
    let file = std::fs::OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path)
        .map_err(|_| ())?;
    if is_reparse(&file.metadata().map_err(|_| ())?) {
        return Err(());
    }
    let mut bytes = Vec::new();
    file.take((MAX_REQUEST_BYTES + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|_| ())?;
    if bytes.is_empty()
        || bytes.len() > MAX_REQUEST_BYTES
        || format!("{:x}", Sha256::digest(&bytes)) != expected
    {
        return Err(());
    }
    let authority: ManagedRootAuthority = serde_json::from_slice(&bytes).map_err(|_| ())?;
    if !is_uuid(&authority.authority_id)
        || authority.revision == 0
        || !safe_managed_root(&authority.input_root)
        || !safe_managed_root(&authority.output_root)
    {
        return Err(());
    }
    Ok((authority, expected))
}

fn validate_request(request: LaunchRequest) -> Result<LaunchRequest, ()> {
    if !is_uuid(&request.job_id)
        || !is_uuid(&request.claim_id)
        || !is_uuid(&request.adapter_id)
        || request.adapter_revision == 0
        || request.install_generation == 0
        || !is_sha(&request.configuration_sha256)
        || !is_sha(&request.input_sha256)
        || !is_commit(&request.candidate_commit)
        || !is_bounded_version(&request.candidate_version)
        || !is_sha(&request.root_authority_sha256)
        || request.action_timeout_ms == 0
        || request.action_timeout_ms > MAX_ACTION_MS
        || request.input_bytes == 0
        || request.input_bytes > MAX_ARTIFACT_BYTES
        || !safe_relative(&request.input_relative_path)
        || !safe_relative(&request.output_relative_path)
    {
        return Err(());
    }
    Ok(request)
}

fn validate_unavailable_request(
    authority: (ManagedRootAuthority, String),
    request: LaunchRequest,
    started: Instant,
) -> Result<(), ()> {
    let (_authority, actual_authority_sha256) = authority;
    if request.root_authority_sha256 != actual_authority_sha256 {
        return Err(());
    }
    let deadline = started
        .checked_add(Duration::from_millis(request.action_timeout_ms))
        .ok_or(())?;
    if Instant::now() < deadline {
        Ok(())
    } else {
        Err(())
    }
}
fn safe_relative(path: &Path) -> bool {
    if path.as_os_str().is_empty() || path.as_os_str().len() > 1024 {
        return false;
    }
    let components: Vec<_> = path.components().collect();
    if components.len() < 2 {
        return false;
    }
    components.into_iter().all(|part| {
        let Component::Normal(segment) = part else {
            return false;
        };
        segment.to_str().is_some_and(safe_windows_segment)
    })
}
fn safe_windows_segment(segment: &str) -> bool {
    if segment.is_empty()
        || segment.len() > 240
        || segment.ends_with(['.', ' '])
        || segment.chars().any(|character| {
            character.is_control()
                || matches!(
                    character,
                    '<' | '>' | ':' | '"' | '/' | '\\' | '|' | '?' | '*'
                )
        })
    {
        return false;
    }
    let stem = segment
        .split('.')
        .next()
        .unwrap_or(segment)
        .to_ascii_uppercase();
    !matches!(stem.as_str(), "CON" | "PRN" | "AUX" | "NUL")
        && !(stem.len() == 4
            && (stem.starts_with("COM") || stem.starts_with("LPT"))
            && matches!(stem.as_bytes()[3], b'1'..=b'9'))
}
fn safe_managed_root(path: &Path) -> bool {
    use std::path::Prefix;
    let mut components = path.components();
    let Some(Component::Prefix(prefix)) = components.next() else {
        return false;
    };
    if !matches!(prefix.kind(), Prefix::Disk(letter) if letter.is_ascii_alphabetic())
        || !matches!(components.next(), Some(Component::RootDir))
    {
        return false;
    }
    let mut normal_count = 0_usize;
    for component in components {
        let Component::Normal(segment) = component else {
            return false;
        };
        let Some(segment) = segment.to_str() else {
            return false;
        };
        if !safe_windows_segment(segment) {
            return false;
        }
        normal_count += 1;
    }
    normal_count >= 2 && path.as_os_str().len() <= 1024
}
fn is_reparse(metadata: &std::fs::Metadata) -> bool {
    metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
}
fn is_sha(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}
fn is_commit(value: &str) -> bool {
    value.len() == 40
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}
fn is_uuid(value: &str) -> bool {
    value.len() == 36
        && value.as_bytes()[14] == b'4'
        && matches!(value.as_bytes()[19], b'8' | b'9' | b'a' | b'b')
        && value.bytes().enumerate().all(|(i, byte)| {
            if matches!(i, 8 | 13 | 18 | 23) {
                byte == b'-'
            } else {
                byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)
            }
        })
}
fn is_bounded_version(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'-' | b'_'))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn malformed_paths_and_identifiers_fail_closed() {
        assert!(!safe_relative(Path::new("..\\secret")));
        assert!(!safe_relative(Path::new("C:\\outside")));
        for hostile in [
            "NUL",
            "con.txt",
            "file:stream",
            "a*",
            "tail.",
            "tail ",
            "COM1",
        ] {
            assert!(!safe_relative(Path::new(hostile)), "accepted {hostile}");
        }
        assert!(!is_sha("SECRET"));
        assert!(!is_uuid("not-an-id"));
        assert!(!is_uuid("AAAAAAAA-2222-4333-8444-555555555555"));
        assert!(!is_uuid("aaaaaaaa-2222-3333-8444-555555555555"));
        assert!(!is_bounded_version("contains control\n"));
        assert!(!is_bounded_version(&"v".repeat(65)));
        for hostile_root in [
            r"\\server\share\managed",
            r"\\?\C:\managed\root",
            r"\\.\C:\managed\root",
            r"\??\C:\managed\root",
            r"C:\managed\NUL",
            r"C:\managed\bad:stream",
            r"C:\managed\tail. ",
            r"C:\managed\..\escape",
            r"C:\only-one",
        ] {
            assert!(
                !safe_managed_root(Path::new(hostile_root)),
                "accepted {hostile_root}"
            );
        }
        assert!(safe_managed_root(Path::new(
            r"C:\ProgramData\TownLight Station"
        )));
    }
}
