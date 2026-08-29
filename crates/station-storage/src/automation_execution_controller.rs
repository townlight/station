use super::*;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use station_automation_policy::{
    AutomationExecutionReceipt, AutomationExecutionRequest, AutomationFeature,
};
use station_domain::{AlertConditionKind, AlertEvent, AlertEventState, AlertSeverity};
use station_maintenance::{
    InstallAuthorityFence, VerifiedAutomationLaunchHelper, VerifiedInstallAuthority,
};
use std::ffi::OsString;
use std::io::{Read, Seek, Write};
use std::os::windows::ffi::OsStringExt;
use std::os::windows::fs::OpenOptionsExt;
use std::os::windows::io::{AsRawHandle, FromRawHandle, RawHandle};
use std::os::windows::process::CommandExt;
use std::path::{Component, Path, PathBuf};
use std::process::{Child, ChildStderr, ChildStdout, Command, Stdio};
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use windows_sys::Win32::Foundation::{CloseHandle, HANDLE, INVALID_HANDLE_VALUE};
use windows_sys::Win32::Security::SECURITY_ATTRIBUTES;
use windows_sys::Win32::Storage::FileSystem::{
    BY_HANDLE_FILE_INFORMATION, FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT,
    FILE_SHARE_READ, FILE_SHARE_WRITE, GetFileInformationByHandle,
};
use windows_sys::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, TH32CS_SNAPTHREAD, THREADENTRY32, Thread32First, Thread32Next,
};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JOB_OBJECT_LIMIT_ACTIVE_PROCESS,
    JOB_OBJECT_LIMIT_JOB_MEMORY, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JobObjectExtendedLimitInformation,
    SetInformationJobObject, TerminateJobObject,
};
use windows_sys::Win32::System::Threading::{
    CREATE_SUSPENDED, OpenThread, QueryFullProcessImageNameW, ResumeThread, THREAD_SUSPEND_RESUME,
};

#[link(name = "kernel32")]
unsafe extern "system" {
    fn CreatePipe(
        read_pipe: *mut HANDLE,
        write_pipe: *mut HANDLE,
        attributes: *const SECURITY_ATTRIBUTES,
        size: u32,
    ) -> i32;
}

#[link(name = "kernel32")]
unsafe extern "system" {
    fn PeekNamedPipe(
        handle: HANDLE,
        buffer: *mut core::ffi::c_void,
        buffer_size: u32,
        bytes_read: *mut u32,
        total_available: *mut u32,
        bytes_left: *mut u32,
    ) -> i32;
}

const AUTOMATION_EXECUTION_WORKER_ID: &str = "stationd-automation-execution";
const AUTOMATION_EXECUTION_LEASE_MS: i64 = 60_000;
// A lease keeps one durable claimant exclusive; it must never become an
// unbounded execution budget. Renewals can preserve ownership while a worker
// is being observed, but this absolute action deadline always starts at claim.
const AUTOMATION_EXECUTION_ACTION_MAX_MS: i64 = 60_000;
const AUTOMATION_EXECUTION_RENEWAL_LEAD_MS: i64 = AUTOMATION_EXECUTION_LEASE_MS / 2;
const AUTOMATION_EXECUTION_POLL_INTERVAL: Duration = Duration::from_millis(250);
const AUTOMATION_EXECUTION_MAX_BACKOFF: Duration = Duration::from_secs(5);
const AUTOMATION_EXECUTION_CHILD_POLL_INTERVAL: Duration = Duration::from_millis(10);
const AUTOMATION_EXECUTION_REAP_MAX: Duration = Duration::from_secs(2);
const MAX_AUTOMATION_EXECUTION_STDOUT_BYTES: u64 = 1_048_576;
const MAX_AUTOMATION_EXECUTION_REQUEST_ENV_BYTES: usize = 32_768;
const MAX_AUTOMATION_EXECUTION_STDERR_BYTES: u64 = 65_536;
const MAX_AUTOMATION_EXECUTION_ACTIVE_PROCESSES: u32 = 8;
const MAX_AUTOMATION_EXECUTION_JOB_MEMORY_BYTES: usize = 1_073_741_824;
#[allow(dead_code)]
const MAX_CLOUD_SUMMARY_INPUT_BYTES: u64 = 8 * 1024 * 1024;
#[allow(dead_code)]
const MAX_CLOUD_SUMMARY_OUTPUT_BYTES: u64 = 1024 * 1024;
#[allow(dead_code)]
const MAX_CLOUD_SUMMARY_CONTROL_BYTES: usize = 64 * 1024;
#[allow(dead_code)]
const MAX_CLOUD_SUMMARY_CREDENTIAL_BYTES: u64 = 16 * 1024;
#[allow(dead_code)]
const CLOUD_SUMMARY_CONTROL_WRITE_DEADLINE: Duration = Duration::from_secs(5);
#[allow(dead_code)]
const CLOUD_SUMMARY_ENDPOINT: &str = "https://api.openai.com/v1/chat/completions";
#[allow(dead_code)]
const CLOUD_SUMMARY_OUTPUT_MEDIA_TYPE: &str = "text/plain; charset=utf-8";
static NEVER_STOP_AUTOMATION_EXECUTION: AtomicBool = AtomicBool::new(false);

#[doc(hidden)]
pub fn automation_execution_process_limits_for_test() -> (u32, usize) {
    (
        MAX_AUTOMATION_EXECUTION_ACTIVE_PROCESSES,
        MAX_AUTOMATION_EXECUTION_JOB_MEMORY_BYTES,
    )
}

/// A typed adapter outcome deliberately carries only an approved categorical
/// code. Raw provider errors, prompts, and response bodies never become
/// durable execution authority.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AutomationExecutionAdapterFailure {
    RetryableTimeout,
    RetryableUnavailable,
    PermanentRejected,
    PermanentInvalidResponse,
}

impl AutomationExecutionAdapterFailure {
    fn code(self) -> &'static str {
        match self {
            Self::RetryableTimeout => "adapter_timeout",
            Self::RetryableUnavailable => "adapter_unavailable",
            Self::PermanentRejected => "adapter_rejected",
            Self::PermanentInvalidResponse => "invalid_provider_response",
        }
    }

    fn retryable(self) -> bool {
        matches!(self, Self::RetryableTimeout | Self::RetryableUnavailable)
    }
}

/// Narrow seam for the durable S13 authority lane. It deliberately does not
/// define storage tables: the future authority implementation must establish
/// the current summary policy, configuration, operator-selected cloud adapter,
/// credential availability, and immutable document handle before returning
/// this capability. Any missing or stale authority is unavailable work, never
/// an invitation to launch a helper with caller-shaped paths or secrets.
#[allow(dead_code)]
pub trait CloudSummaryExecutionAuthority {
    fn acquire_current_cloud_summary_material(
        &mut self,
        request: &AutomationExecutionRequest,
        claim_id: &str,
        hard_deadline: Instant,
    ) -> Result<CloudSummaryExecutionMaterial, AutomationExecutionAdapterFailure>;
}

/// Opaque execution material returned by `CloudSummaryExecutionAuthority`.
/// The constructor is crate-private so no API caller can turn paths or a raw
/// credential string into a production launch capability.
#[allow(dead_code)]
pub struct CloudSummaryExecutionMaterial {
    input: std::fs::File,
    output: std::fs::File,
    credential: std::fs::File,
    model: String,
}

#[allow(dead_code)]
impl CloudSummaryExecutionMaterial {
    #[allow(clippy::missing_const_for_fn)]
    pub(crate) fn from_verified_handles(
        input: std::fs::File,
        output: std::fs::File,
        credential: std::fs::File,
        model: String,
    ) -> Self {
        Self {
            input,
            output,
            credential,
            model,
        }
    }

    fn validate_against_request(
        &mut self,
        request: &AutomationExecutionRequest,
    ) -> Result<(), AutomationExecutionAdapterFailure> {
        if request.feature != AutomationFeature::Summaries
            || request.input.bytes == 0
            || request.input.bytes > MAX_CLOUD_SUMMARY_INPUT_BYTES
            || request.input.media_type != CLOUD_SUMMARY_OUTPUT_MEDIA_TYPE
            || !is_bounded_cloud_model(&self.model)
        {
            return Err(AutomationExecutionAdapterFailure::PermanentRejected);
        }
        let input = self
            .input
            .metadata()
            .map_err(|_| AutomationExecutionAdapterFailure::RetryableUnavailable)?;
        let output = self
            .output
            .metadata()
            .map_err(|_| AutomationExecutionAdapterFailure::RetryableUnavailable)?;
        if !input.is_file()
            || !output.is_file()
            || input.len() != request.input.bytes
            || output.len() != 0
            || same_windows_file_identity(&self.input, &self.output)?
        {
            return Err(AutomationExecutionAdapterFailure::PermanentRejected);
        }
        self.input
            .seek(std::io::SeekFrom::Start(0))
            .map_err(|_| AutomationExecutionAdapterFailure::RetryableUnavailable)?;
        let actual = hash_open_file(&mut self.input, Instant::now() + Duration::from_secs(5))
            .map_err(|_| AutomationExecutionAdapterFailure::RetryableUnavailable)?;
        if actual != request.input.artifact_sha256 {
            return Err(AutomationExecutionAdapterFailure::PermanentRejected);
        }
        let credential = self
            .credential
            .metadata()
            .map_err(|_| AutomationExecutionAdapterFailure::RetryableUnavailable)?;
        if credential.is_dir() || credential.len() > MAX_CLOUD_SUMMARY_CREDENTIAL_BYTES + 4 {
            return Err(AutomationExecutionAdapterFailure::PermanentRejected);
        }
        Ok(())
    }
}

/// The only non-durable result the controller may receive from the helper.
/// The durable storage lane must still bind it to the live claim/policy before
/// manufacturing an `AutomationExecutionReceipt`.
#[derive(Debug, Clone, PartialEq, Eq)]
#[allow(dead_code)]
pub struct CloudSummaryHelperResult {
    pub output_sha256: String,
    pub output_bytes: u64,
    pub output_word_count: u64,
    pub provider_request_id_sha256: String,
    pub provider_response_sha256: String,
}

/// A declarative, installed worker program.  This is intentionally data, not
/// a callback interface: adapter code has no synchronous `poll` hook through
/// which it can hold stationd, its queue lease, or the install fence hostage.
/// The controller starts, observes, and terminates the one process tree it
/// owns.  There is currently no production program registered; that remains a
/// truthful `adapter_unavailable` retry rather than fabricated execution.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AutomationExecutionAdapter {
    adapter_id: String,
    adapter_revision: u64,
    application_root: PathBuf,
    executable: PathBuf,
    executable_sha256: String,
    arguments: Vec<OsString>,
    setup_fault: Option<AutomationExecutionSetupFault>,
}

#[doc(hidden)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AutomationExecutionSetupFault {
    AssignProcess,
    ResumeThread,
}

impl AutomationExecutionAdapter {
    /// Constructs an executable adapter only from the helper capability read
    /// from the active candidate while the install authority fence is held.
    /// There is no production constructor accepting a caller-supplied root,
    /// executable, or digest.
    ///
    /// ```compile_fail
    /// use station_storage::AutomationExecutionAdapter;
    /// let _ = AutomationExecutionAdapter::new;
    /// ```
    pub fn from_verified_launch_helper(
        adapter_id: impl Into<String>,
        adapter_revision: u64,
        helper: VerifiedAutomationLaunchHelper,
        arguments: Vec<OsString>,
    ) -> Result<Self, String> {
        Self::new_unverified(
            adapter_id,
            adapter_revision,
            helper.application_root().to_path_buf(),
            helper.executable().to_path_buf(),
            helper.sha256().to_owned(),
            arguments,
        )
    }

    fn new_unverified(
        adapter_id: impl Into<String>,
        adapter_revision: u64,
        application_root: impl Into<PathBuf>,
        executable: impl Into<std::path::PathBuf>,
        executable_sha256: impl Into<String>,
        arguments: Vec<OsString>,
    ) -> Result<Self, String> {
        let adapter_id = adapter_id.into();
        let application_root = application_root.into();
        let executable = executable.into();
        let executable_sha256 = executable_sha256.into();
        if !is_canonical_uuid(&adapter_id)
            || adapter_revision == 0
            || !application_root.is_absolute()
            || !executable.is_absolute()
            || !is_lowercase_sha256(&executable_sha256)
            || arguments.len() > 64
        {
            return Err(
                "automation execution program specification is invalid or unbounded".into(),
            );
        }
        if arguments.iter().any(|argument| argument.len() > 8_192) {
            return Err("automation execution program argument is oversized".into());
        }
        Ok(Self {
            adapter_id,
            adapter_revision,
            application_root,
            executable,
            executable_sha256,
            arguments,
            setup_fault: None,
        })
    }

    /// Internal test-only construction seam for hostile process-boundary
    /// coverage. It is unavailable in every non-test dependency graph.
    #[cfg(test)]
    #[doc(hidden)]
    pub(crate) fn new_for_test(
        adapter_id: impl Into<String>,
        adapter_revision: u64,
        application_root: impl Into<PathBuf>,
        executable: impl Into<PathBuf>,
        executable_sha256: impl Into<String>,
        arguments: Vec<OsString>,
    ) -> Result<Self, String> {
        Self::new_unverified(
            adapter_id,
            adapter_revision,
            application_root,
            executable,
            executable_sha256,
            arguments,
        )
    }

    #[cfg(test)]
    #[doc(hidden)]
    pub(crate) fn with_test_setup_fault(mut self, fault: AutomationExecutionSetupFault) -> Self {
        self.setup_fault = Some(fault);
        self
    }

    #[cfg(test)]
    #[doc(hidden)]
    pub(crate) fn verify_program_for_test(&self, request: &AutomationExecutionRequest) -> bool {
        self.pin_for_request(request, Instant::now() + Duration::from_secs(60))
            .is_ok()
    }

    #[cfg(test)]
    #[doc(hidden)]
    pub(crate) fn launch_identity_for_test(&self) -> (&Path, &Path, &str) {
        (
            &self.application_root,
            &self.executable,
            &self.executable_sha256,
        )
    }

    #[cfg(test)]
    #[doc(hidden)]
    pub(crate) fn substitution_is_blocked_while_pinned_for_test(
        &self,
        request: &AutomationExecutionRequest,
        replacement: &Path,
    ) -> bool {
        let pinned = match self.pin_for_request(request, Instant::now() + Duration::from_secs(60)) {
            Ok(pinned) => pinned,
            // Failure to acquire the complete replacement-denying chain is
            // itself a fail-closed launch result: no pathname launch occurs.
            Err(_) => return true,
        };
        let overwrite_blocked = std::fs::copy(replacement, &self.executable).is_err();
        let leaf_rename_blocked = std::fs::rename(
            &self.executable,
            self.executable.with_extension("renamed-during-launch"),
        )
        .is_err();
        let ancestor_rename_blocked = std::fs::rename(
            &self.application_root,
            self.application_root
                .with_extension("renamed-during-launch"),
        )
        .is_err();
        let junction_insertion_blocked = Command::new("cmd.exe")
            .args(["/d", "/c", "mklink", "/J"])
            .arg(&self.application_root)
            .arg(replacement.parent().unwrap_or(replacement))
            .status()
            .map(|status| !status.success())
            .unwrap_or(true);
        let write_blocked = std::fs::OpenOptions::new()
            .write(true)
            .truncate(true)
            .open(&self.executable)
            .is_err();
        drop(pinned);
        overwrite_blocked
            && leaf_rename_blocked
            && ancestor_rename_blocked
            && junction_insertion_blocked
            && write_blocked
    }

    pub fn adapter_id(&self) -> &str {
        &self.adapter_id
    }

    fn pin_for_request(
        &self,
        request: &AutomationExecutionRequest,
        hard_deadline: Instant,
    ) -> Result<PinnedAutomationExecutable, AutomationExecutionAdapterFailure> {
        if self.adapter_id != request.adapter_id
            || self.adapter_revision != request.adapter_revision
        {
            return Err(AutomationExecutionAdapterFailure::PermanentRejected);
        }
        let relative = self
            .executable
            .strip_prefix(&self.application_root)
            .map_err(|_| AutomationExecutionAdapterFailure::PermanentRejected)?;
        if relative.as_os_str().is_empty()
            || relative
                .components()
                .any(|component| !matches!(component, Component::Normal(_)))
        {
            return Err(AutomationExecutionAdapterFailure::PermanentRejected);
        }
        let mut handles = Vec::new();
        // Inspect the supplied path itself, not a canonicalized replacement:
        // every existing ancestor from the volume root through the trusted
        // application root must be an ordinary non-reparse directory.
        let mut ancestors: Vec<_> = self.application_root.ancestors().collect();
        ancestors.reverse();
        for ancestor in ancestors.iter().copied() {
            if Instant::now() >= hard_deadline {
                return Err(AutomationExecutionAdapterFailure::RetryableTimeout);
            }
            if !ancestor.as_os_str().is_empty() {
                handles.push(
                    open_no_follow_directory(ancestor)
                        .map_err(|_| AutomationExecutionAdapterFailure::PermanentRejected)?,
                );
            }
        }
        let mut current = self.application_root.clone();
        let components: Vec<_> = relative.components().collect();
        for component in &components[..components.len() - 1] {
            if Instant::now() >= hard_deadline {
                return Err(AutomationExecutionAdapterFailure::RetryableTimeout);
            }
            current.push(component.as_os_str());
            handles.push(
                open_no_follow_directory(&current)
                    .map_err(|_| AutomationExecutionAdapterFailure::PermanentRejected)?,
            );
        }
        let mut file = open_no_follow_file(&self.executable)
            .map_err(|_| AutomationExecutionAdapterFailure::RetryableUnavailable)?;
        let metadata = file
            .metadata()
            .map_err(|_| AutomationExecutionAdapterFailure::RetryableUnavailable)?;
        if metadata.len() > 268_435_456 {
            return Err(AutomationExecutionAdapterFailure::PermanentRejected);
        }
        let actual = hash_open_file(&mut file, hard_deadline)
            .map_err(|_| AutomationExecutionAdapterFailure::RetryableUnavailable)?;
        if actual != self.executable_sha256 {
            return Err(AutomationExecutionAdapterFailure::PermanentRejected);
        }
        let file_identity = windows_file_identity(&file)?;
        if file_identity.0 == 0 || file_identity.1 == 0 {
            return Err(AutomationExecutionAdapterFailure::RetryableUnavailable);
        }
        handles.push(file);
        Ok(PinnedAutomationExecutable {
            executable: self.executable.clone(),
            _handles: handles,
            file_identity,
        })
    }
}

struct PinnedAutomationExecutable {
    executable: PathBuf,
    // Deny delete/write replacement of the verified leaf and retain no-follow
    // handles for the trusted-root-to-leaf chain through CreateProcess.
    _handles: Vec<std::fs::File>,
    file_identity: (u32, u64),
}

fn is_lowercase_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn open_no_follow_directory(path: &Path) -> Result<std::fs::File, String> {
    let file = std::fs::OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS)
        .open(path)
        .map_err(|_| "automation executable directory is unavailable")?;
    let metadata = file
        .metadata()
        .map_err(|_| "automation executable directory is unavailable")?;
    if !metadata.is_dir() || is_reparse_metadata(&metadata) {
        return Err("automation executable directory is reparse-backed".into());
    }
    Ok(file)
}

fn open_no_follow_file(path: &Path) -> Result<std::fs::File, String> {
    let file = std::fs::OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path)
        .map_err(|_| "automation executable is unavailable")?;
    let metadata = file
        .metadata()
        .map_err(|_| "automation executable is unavailable")?;
    if !metadata.is_file() || is_reparse_metadata(&metadata) {
        return Err("automation executable is not a regular file".into());
    }
    Ok(file)
}

fn hash_open_file(file: &mut std::fs::File, hard_deadline: Instant) -> Result<String, String> {
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        if Instant::now() >= hard_deadline {
            return Err("automation executable hash deadline expired".into());
        }
        let read = file
            .read(&mut buffer)
            .map_err(|_| "automation executable could not be read")?;
        if read == 0 {
            break;
        }
        digest.update(&buffer[..read]);
    }
    Ok(format!("{:x}", digest.finalize()))
}

#[cfg(windows)]
fn is_reparse_metadata(metadata: &std::fs::Metadata) -> bool {
    metadata.file_attributes()
        & windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_REPARSE_POINT
        != 0
}

#[cfg(not(windows))]
fn is_reparse_metadata(_metadata: &std::fs::Metadata) -> bool {
    false
}

struct WindowsProcessTree(HANDLE);

impl WindowsProcessTree {
    fn new() -> Result<Self, AutomationExecutionAdapterFailure> {
        let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if handle == INVALID_HANDLE_VALUE || handle.is_null() {
            return Err(AutomationExecutionAdapterFailure::RetryableUnavailable);
        }
        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | JOB_OBJECT_LIMIT_JOB_MEMORY;
        limits.BasicLimitInformation.ActiveProcessLimit = MAX_AUTOMATION_EXECUTION_ACTIVE_PROCESSES;
        limits.JobMemoryLimit = MAX_AUTOMATION_EXECUTION_JOB_MEMORY_BYTES;
        let configured = unsafe {
            SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                u32::try_from(std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>())
                    .expect("automation execution job limits fit u32"),
            )
        };
        if configured == 0 {
            unsafe { CloseHandle(handle) };
            return Err(AutomationExecutionAdapterFailure::RetryableUnavailable);
        }
        Ok(Self(handle))
    }

    fn assign(&self, child: &Child) -> Result<(), AutomationExecutionAdapterFailure> {
        if unsafe { AssignProcessToJobObject(self.0, child.as_raw_handle() as HANDLE) } == 0 {
            return Err(AutomationExecutionAdapterFailure::RetryableUnavailable);
        }
        Ok(())
    }

    fn terminate(&self) -> Result<(), AutomationExecutionAdapterFailure> {
        if unsafe { TerminateJobObject(self.0, 1) } == 0 {
            return Err(AutomationExecutionAdapterFailure::RetryableUnavailable);
        }
        Ok(())
    }
}

impl Drop for WindowsProcessTree {
    fn drop(&mut self) {
        unsafe { CloseHandle(self.0) };
    }
}

fn resume_suspended_process_primary_thread(
    process_id: u32,
) -> Result<(), AutomationExecutionAdapterFailure> {
    let snapshot = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0) };
    if snapshot == INVALID_HANDLE_VALUE {
        return Err(AutomationExecutionAdapterFailure::RetryableUnavailable);
    }
    let result = (|| {
        let mut entry = THREADENTRY32 {
            dwSize: u32::try_from(std::mem::size_of::<THREADENTRY32>())
                .expect("thread entry size fits u32"),
            ..Default::default()
        };
        let mut found = unsafe { Thread32First(snapshot, &mut entry) } != 0;
        while found {
            if entry.th32OwnerProcessID == process_id {
                let thread = unsafe { OpenThread(THREAD_SUSPEND_RESUME, 0, entry.th32ThreadID) };
                if thread.is_null() || thread == INVALID_HANDLE_VALUE {
                    return Err(AutomationExecutionAdapterFailure::RetryableUnavailable);
                }
                let resumed = unsafe { ResumeThread(thread) };
                unsafe { CloseHandle(thread) };
                if resumed == u32::MAX {
                    return Err(AutomationExecutionAdapterFailure::RetryableUnavailable);
                }
                return Ok(());
            }
            entry.dwSize = u32::try_from(std::mem::size_of::<THREADENTRY32>())
                .expect("thread entry size fits u32");
            found = unsafe { Thread32Next(snapshot, &mut entry) } != 0;
        }
        Err(AutomationExecutionAdapterFailure::RetryableUnavailable)
    })();
    unsafe { CloseHandle(snapshot) };
    result
}

trait PipeRead: Read + Send {
    fn pipe_handle(&self) -> HANDLE;
}
impl PipeRead for ChildStdout {
    fn pipe_handle(&self) -> HANDLE {
        self.as_raw_handle() as HANDLE
    }
}
impl PipeRead for ChildStderr {
    fn pipe_handle(&self) -> HANDLE {
        self.as_raw_handle() as HANDLE
    }
}
impl PipeRead for std::fs::File {
    fn pipe_handle(&self) -> HANDLE {
        self.as_raw_handle() as HANDLE
    }
}

struct BoundedPipeDrain {
    pipe: Box<dyn PipeRead>,
    limit: usize,
    bytes: Vec<u8>,
}

impl BoundedPipeDrain {
    fn new(pipe: impl PipeRead + 'static, limit: u64) -> Self {
        Self {
            pipe: Box::new(pipe),
            limit: limit as usize,
            bytes: Vec::new(),
        }
    }

    fn poll(&mut self) -> Result<(), AutomationExecutionAdapterFailure> {
        loop {
            let mut available = 0_u32;
            if unsafe {
                PeekNamedPipe(
                    self.pipe.pipe_handle(),
                    std::ptr::null_mut(),
                    0,
                    std::ptr::null_mut(),
                    &mut available,
                    std::ptr::null_mut(),
                )
            } == 0
            {
                return Ok(());
            }
            if available == 0 {
                return Ok(());
            }
            let remaining = self
                .limit
                .saturating_add(1)
                .saturating_sub(self.bytes.len());
            if remaining == 0 {
                return Err(AutomationExecutionAdapterFailure::PermanentInvalidResponse);
            }
            let take = remaining.min(available as usize).min(64 * 1024);
            let mut chunk = vec![0_u8; take];
            let read = self
                .pipe
                .read(&mut chunk)
                .map_err(|_| AutomationExecutionAdapterFailure::RetryableUnavailable)?;
            self.bytes.extend_from_slice(&chunk[..read]);
            if self.bytes.len() > self.limit {
                return Err(AutomationExecutionAdapterFailure::PermanentInvalidResponse);
            }
            if read == 0 {
                return Ok(());
            }
        }
    }

    fn finish(mut self) -> Result<Vec<u8>, AutomationExecutionAdapterFailure> {
        self.poll()?;
        Ok(self.bytes)
    }
}

/// The only execution resource the controller supervises.  It is created by
/// the host after it has acquired the durable claim, placed in a fresh Windows
/// Job Object immediately, and never exposes a blocking adapter callback.
struct OwnedAutomationExecutionProcess {
    child: Child,
    job: WindowsProcessTree,
    _pinned_executable: PinnedAutomationExecutable,
    stdout: Option<BoundedPipeDrain>,
    stderr: Option<BoundedPipeDrain>,
    drained_stdout: Option<Vec<u8>>,
}

impl OwnedAutomationExecutionProcess {
    fn spawn(
        adapter: &AutomationExecutionAdapter,
        request: &AutomationExecutionRequest,
        claim_id: &str,
        deadline_unix_ms: i64,
        hard_deadline: Instant,
    ) -> Result<Self, AutomationExecutionAdapterFailure> {
        let pinned_executable = adapter.pin_for_request(request, hard_deadline)?;
        if Instant::now() >= hard_deadline {
            return Err(AutomationExecutionAdapterFailure::RetryableTimeout);
        }
        let request_json = serde_json::to_string(request)
            .map_err(|_| AutomationExecutionAdapterFailure::PermanentInvalidResponse)?;
        if request_json.len() > MAX_AUTOMATION_EXECUTION_REQUEST_ENV_BYTES {
            return Err(AutomationExecutionAdapterFailure::PermanentInvalidResponse);
        }
        let job = WindowsProcessTree::new()?;
        let mut child = Command::new(&pinned_executable.executable)
            .args(&adapter.arguments)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            // The process cannot execute until it is assigned to the fresh
            // kill-on-close Job Object below. A suspended process has no
            // user-mode opportunity to create an escaping descendant.
            .creation_flags(CREATE_SUSPENDED)
            .env("TOWNLIGHT_AUTOMATION_EXECUTION_REQUEST_JSON", request_json)
            .env("TOWNLIGHT_AUTOMATION_EXECUTION_CLAIM_ID", claim_id)
            .env(
                "TOWNLIGHT_AUTOMATION_EXECUTION_DEADLINE_UNIX_MS",
                deadline_unix_ms.to_string(),
            )
            .spawn()
            .map_err(|_| AutomationExecutionAdapterFailure::RetryableUnavailable)?;
        if verify_spawned_image_identity(&child, pinned_executable.file_identity).is_err() {
            child
                .kill()
                .map_err(|_| AutomationExecutionAdapterFailure::RetryableUnavailable)?;
            bounded_reap_child(&mut child)?;
            return Err(AutomationExecutionAdapterFailure::PermanentRejected);
        }
        let assignment =
            if adapter.setup_fault == Some(AutomationExecutionSetupFault::AssignProcess) {
                Err(AutomationExecutionAdapterFailure::RetryableUnavailable)
            } else {
                job.assign(&child)
            };
        if let Err(error) = assignment {
            child
                .kill()
                .map_err(|_| AutomationExecutionAdapterFailure::RetryableUnavailable)?;
            bounded_reap_child(&mut child)?;
            return Err(error);
        }
        let stdout = child
            .stdout
            .take()
            .ok_or(AutomationExecutionAdapterFailure::PermanentInvalidResponse)?;
        let stderr = child
            .stderr
            .take()
            .ok_or(AutomationExecutionAdapterFailure::PermanentInvalidResponse)?;
        let stdout = BoundedPipeDrain::new(stdout, MAX_AUTOMATION_EXECUTION_STDOUT_BYTES);
        let stderr = BoundedPipeDrain::new(stderr, MAX_AUTOMATION_EXECUTION_STDERR_BYTES);
        let resume = if adapter.setup_fault == Some(AutomationExecutionSetupFault::ResumeThread) {
            Err(AutomationExecutionAdapterFailure::RetryableUnavailable)
        } else {
            resume_suspended_process_primary_thread(child.id())
        };
        if let Err(error) = resume {
            job.terminate()?;
            bounded_reap_child(&mut child)?;
            let _ = stdout.finish();
            let _ = stderr.finish();
            return Err(error);
        }
        Ok(Self {
            child,
            job,
            _pinned_executable: pinned_executable,
            stdout: Some(stdout),
            stderr: Some(stderr),
            drained_stdout: None,
        })
    }

    fn terminate_and_reap(&mut self) -> Result<(), AutomationExecutionAdapterFailure> {
        self.job.terminate()?;
        bounded_reap_child(&mut self.child)?;
        let stdout = self
            .stdout
            .take()
            .ok_or(AutomationExecutionAdapterFailure::RetryableUnavailable)?;
        let stdout = stdout.finish()?;
        let _stderr = self
            .stderr
            .take()
            .ok_or(AutomationExecutionAdapterFailure::RetryableUnavailable)?;
        let _stderr = _stderr.finish()?;
        // Retain only the success payload. stderr is concurrently drained so
        // a child cannot deadlock on it, but it is never persisted or exposed.
        self.drained_stdout = Some(stdout);
        Ok(())
    }

    fn try_receipt(
        &mut self,
    ) -> Result<Option<AutomationExecutionReceipt>, AutomationExecutionAdapterFailure> {
        self.stdout
            .as_mut()
            .ok_or(AutomationExecutionAdapterFailure::RetryableUnavailable)?
            .poll()?;
        self.stderr
            .as_mut()
            .ok_or(AutomationExecutionAdapterFailure::RetryableUnavailable)?
            .poll()?;
        let Some(status) = self
            .child
            .try_wait()
            .map_err(|_| AutomationExecutionAdapterFailure::RetryableUnavailable)?
        else {
            return Ok(None);
        };
        // The child may have exited while a descendant keeps either pipe open.
        // First revoke the exact Job Object, then require both its process
        // handle and the concurrent drains to finish before parsing output.
        self.terminate_and_reap()?;
        if !status.success() {
            return Err(AutomationExecutionAdapterFailure::PermanentRejected);
        }
        let bytes = self
            .drained_stdout
            .take()
            .ok_or(AutomationExecutionAdapterFailure::PermanentInvalidResponse)?;
        let receipt = serde_json::from_slice(&bytes)
            .map_err(|_| AutomationExecutionAdapterFailure::PermanentInvalidResponse)?;
        Ok(Some(receipt))
    }

    fn terminate(&mut self) -> Result<(), AutomationExecutionAdapterFailure> {
        self.terminate_and_reap()
    }
}

fn verify_spawned_image_identity(
    child: &Child,
    expected: (u32, u64),
) -> Result<(), AutomationExecutionAdapterFailure> {
    verify_spawned_image_identity_handle(child.as_raw_handle(), expected)
}

fn verify_spawned_image_identity_handle(
    process: RawHandle,
    expected: (u32, u64),
) -> Result<(), AutomationExecutionAdapterFailure> {
    let mut buffer = vec![0_u16; 32_768];
    let mut length = u32::try_from(buffer.len()).unwrap();
    if unsafe { QueryFullProcessImageNameW(process as HANDLE, 0, buffer.as_mut_ptr(), &mut length) }
        == 0
    {
        return Err(AutomationExecutionAdapterFailure::RetryableUnavailable);
    }
    let path = PathBuf::from(OsString::from_wide(&buffer[..length as usize]));
    let file = open_no_follow_file(&path)
        .map_err(|_| AutomationExecutionAdapterFailure::RetryableUnavailable)?;
    let actual = windows_file_identity(&file)?;
    if actual != expected {
        return Err(AutomationExecutionAdapterFailure::PermanentRejected);
    }
    Ok(())
}

fn windows_file_identity(
    file: &std::fs::File,
) -> Result<(u32, u64), AutomationExecutionAdapterFailure> {
    let mut info = BY_HANDLE_FILE_INFORMATION::default();
    if unsafe { GetFileInformationByHandle(file.as_raw_handle() as HANDLE, &mut info) } == 0 {
        return Err(AutomationExecutionAdapterFailure::RetryableUnavailable);
    }
    Ok((
        info.dwVolumeSerialNumber,
        (u64::from(info.nFileIndexHigh) << 32) | u64::from(info.nFileIndexLow),
    ))
}

#[allow(dead_code)]
fn same_windows_file_identity(
    first: &std::fs::File,
    second: &std::fs::File,
) -> Result<bool, AutomationExecutionAdapterFailure> {
    Ok(windows_file_identity(first)? == windows_file_identity(second)?)
}

#[allow(dead_code)]
fn is_bounded_cloud_model(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'-' | b'_' | b':' | b'/')
        })
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
#[allow(dead_code)]
struct CloudSummaryHelperControl<'a> {
    mode: &'static str,
    job_id: &'a str,
    claim_id: &'a str,
    adapter_id: &'a str,
    adapter_revision: u64,
    candidate_version: &'a str,
    candidate_commit: &'a str,
    install_generation: u64,
    configuration_sha256: &'a str,
    endpoint: &'static str,
    model: &'a str,
    input_sha256: &'a str,
    input_bytes: u64,
    output_media_type: &'static str,
    action_timeout_ms: u64,
    max_output_bytes: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[allow(dead_code)]
struct CloudSummaryHelperWireResponse {
    status: String,
    code: String,
    output_sha256: Option<String>,
    output_bytes: Option<u64>,
    output_word_count: Option<u64>,
    provider_request_id_sha256: Option<String>,
    provider_response_sha256: Option<String>,
}

/// Acquires the current durable authority through the supplied narrow trait,
/// independently rebinds the active installed helper under the existing
/// install fence, and starts only the exact `cloud_summary_v1` mode. The
/// durable queue intentionally does not call this yet: it has no document and
/// credential authority implementation, so its existing unavailable pacing is
/// preserved.
#[allow(dead_code)]
pub fn launch_current_cloud_summary_v1(
    authority: &mut dyn CloudSummaryExecutionAuthority,
    request: &AutomationExecutionRequest,
    claim_id: &str,
    fence: &InstallAuthorityFence,
    data_directory: &Path,
    application_root: &Path,
    hard_deadline: Instant,
) -> Result<OwnedCloudSummaryHelperProcess, AutomationExecutionAdapterFailure> {
    if Instant::now() >= hard_deadline || request.feature != AutomationFeature::Summaries {
        return Err(AutomationExecutionAdapterFailure::PermanentRejected);
    }
    let mut material =
        authority.acquire_current_cloud_summary_material(request, claim_id, hard_deadline)?;
    material.validate_against_request(request)?;
    let helper = station_maintenance::read_verified_automation_launch_helper(
        fence,
        data_directory,
        application_root,
    )
    .map_err(|_| AutomationExecutionAdapterFailure::RetryableUnavailable)?;
    OwnedCloudSummaryHelperProcess::spawn(helper, request, claim_id, material, hard_deadline)
}

/// An owned live helper and its containment. It retains the output handle for
/// post-exit rehashing; input/credential capabilities were inherited only by
/// the helper's exact Windows handle list and cannot be rediscovered by path.
#[allow(dead_code)]
pub struct OwnedCloudSummaryHelperProcess {
    child: station_windows_ipc::ContainedProcess,
    containment: Option<station_windows_ipc::ProcessContainment>,
    output: std::fs::File,
    stdout: Option<BoundedPipeDrain>,
    stderr: Option<BoundedPipeDrain>,
}

#[allow(dead_code)]
impl OwnedCloudSummaryHelperProcess {
    fn spawn(
        helper: VerifiedAutomationLaunchHelper,
        request: &AutomationExecutionRequest,
        claim_id: &str,
        material: CloudSummaryExecutionMaterial,
        hard_deadline: Instant,
    ) -> Result<Self, AutomationExecutionAdapterFailure> {
        if Instant::now() >= hard_deadline {
            return Err(AutomationExecutionAdapterFailure::RetryableTimeout);
        }
        let CloudSummaryExecutionMaterial {
            input,
            output,
            credential,
            model,
        } = material;
        let (control_read, control_write) = create_cloud_summary_pipe()?;
        let (response_read, response_write) = create_cloud_summary_pipe()?;
        let (stderr_read, stderr_write) = create_cloud_summary_pipe()?;
        let arguments = vec![
            OsString::from("--cloud-summary-v1"),
            OsString::from("--input-handle"),
            OsString::from((input.as_raw_handle() as usize).to_string()),
            OsString::from("--output-handle"),
            OsString::from((output.as_raw_handle() as usize).to_string()),
            OsString::from("--credential-handle"),
            OsString::from((credential.as_raw_handle() as usize).to_string()),
        ];
        let adapter = AutomationExecutionAdapter::from_verified_launch_helper(
            request.adapter_id.clone(),
            request.adapter_revision,
            helper,
            arguments,
        )
        .map_err(|_| AutomationExecutionAdapterFailure::PermanentRejected)?;
        let pinned = adapter.pin_for_request(request, hard_deadline)?;
        let argument_text = adapter
            .arguments
            .iter()
            .map(|argument| {
                argument
                    .to_str()
                    .ok_or(AutomationExecutionAdapterFailure::PermanentRejected)
            })
            .collect::<Result<Vec<_>, _>>()?;
        let expected_identity = pinned.file_identity;
        let standard = [
            control_read.as_raw_handle(),
            response_write.as_raw_handle(),
            stderr_write.as_raw_handle(),
        ];
        let additional = [
            input.as_raw_handle(),
            output.as_raw_handle(),
            credential.as_raw_handle(),
        ];
        let (child, containment) =
            station_windows_ipc::launch_contained_process_with_inherited_handles_and_limits(
                &pinned.executable,
                &argument_text,
                standard,
                &additional,
                Some(station_windows_ipc::ProcessContainmentLimits {
                    max_active_processes: MAX_AUTOMATION_EXECUTION_ACTIVE_PROCESSES,
                    max_job_memory_bytes: MAX_AUTOMATION_EXECUTION_JOB_MEMORY_BYTES,
                }),
                |process| {
                    verify_spawned_image_identity_handle(process, expected_identity)
                        .map_err(|_| station_windows_ipc::IpcError::InvalidName)
                },
            )
            .map_err(|_| AutomationExecutionAdapterFailure::RetryableUnavailable)?;
        drop(control_read);
        drop(response_write);
        drop(stderr_write);
        let control = CloudSummaryHelperControl {
            mode: "cloud_summary_v1",
            job_id: &request.job_id,
            claim_id,
            adapter_id: &request.adapter_id,
            adapter_revision: request.adapter_revision,
            candidate_version: &request.candidate.version,
            candidate_commit: &request.candidate.source_commit,
            install_generation: request.install_generation,
            configuration_sha256: &request.configuration_hash,
            endpoint: CLOUD_SUMMARY_ENDPOINT,
            model: &model,
            input_sha256: &request.input.artifact_sha256,
            input_bytes: request.input.bytes,
            output_media_type: CLOUD_SUMMARY_OUTPUT_MEDIA_TYPE,
            action_timeout_ms: u64::try_from(
                hard_deadline
                    .checked_duration_since(Instant::now())
                    .unwrap_or_default()
                    .as_millis(),
            )
            .unwrap_or(0)
            .min(AUTOMATION_EXECUTION_ACTION_MAX_MS as u64),
            max_output_bytes: MAX_CLOUD_SUMMARY_OUTPUT_BYTES,
        };
        let bytes = serde_json::to_vec(&control)
            .map_err(|_| AutomationExecutionAdapterFailure::PermanentInvalidResponse)?;
        if bytes.is_empty() || bytes.len() > MAX_CLOUD_SUMMARY_CONTROL_BYTES {
            drop(containment);
            return Err(AutomationExecutionAdapterFailure::PermanentInvalidResponse);
        }
        if let Err(error) = write_cloud_summary_frame(control_write, &bytes) {
            drop(containment);
            return Err(error);
        }
        Ok(Self {
            child,
            containment: Some(containment),
            output,
            stdout: Some(BoundedPipeDrain::new(
                response_read,
                MAX_AUTOMATION_EXECUTION_STDOUT_BYTES,
            )),
            stderr: Some(BoundedPipeDrain::new(
                stderr_read,
                MAX_AUTOMATION_EXECUTION_STDERR_BYTES,
            )),
        })
    }

    pub fn try_result(
        &mut self,
        hard_deadline: Instant,
    ) -> Result<Option<CloudSummaryHelperResult>, AutomationExecutionAdapterFailure> {
        if Instant::now() >= hard_deadline {
            self.terminate();
            return Err(AutomationExecutionAdapterFailure::RetryableTimeout);
        }
        self.stdout
            .as_mut()
            .ok_or(AutomationExecutionAdapterFailure::RetryableUnavailable)?
            .poll()?;
        self.stderr
            .as_mut()
            .ok_or(AutomationExecutionAdapterFailure::RetryableUnavailable)?
            .poll()?;
        let Some(exit) = self
            .child
            .try_wait()
            .map_err(|_| AutomationExecutionAdapterFailure::RetryableUnavailable)?
        else {
            return Ok(None);
        };
        drop(self.containment.take());
        if exit != 0 {
            return Err(AutomationExecutionAdapterFailure::PermanentRejected);
        }
        let stdout = self
            .stdout
            .take()
            .ok_or(AutomationExecutionAdapterFailure::RetryableUnavailable)?
            .finish()?;
        let _stderr = self
            .stderr
            .take()
            .ok_or(AutomationExecutionAdapterFailure::RetryableUnavailable)?
            .finish()?;
        let wire: CloudSummaryHelperWireResponse = serde_json::from_slice(&stdout)
            .map_err(|_| AutomationExecutionAdapterFailure::PermanentInvalidResponse)?;
        if wire.status != "completed" || wire.code != "completed" {
            return Err(match wire.code.as_str() {
                "provider_unavailable" => AutomationExecutionAdapterFailure::RetryableUnavailable,
                "launch_rejected"
                | "input_binding_failed"
                | "credential_rejected"
                | "provider_denied" => AutomationExecutionAdapterFailure::PermanentRejected,
                _ => AutomationExecutionAdapterFailure::PermanentInvalidResponse,
            });
        }
        let result = CloudSummaryHelperResult {
            output_sha256: wire
                .output_sha256
                .ok_or(AutomationExecutionAdapterFailure::PermanentInvalidResponse)?,
            output_bytes: wire
                .output_bytes
                .ok_or(AutomationExecutionAdapterFailure::PermanentInvalidResponse)?,
            output_word_count: wire
                .output_word_count
                .ok_or(AutomationExecutionAdapterFailure::PermanentInvalidResponse)?,
            provider_request_id_sha256: wire
                .provider_request_id_sha256
                .ok_or(AutomationExecutionAdapterFailure::PermanentInvalidResponse)?,
            provider_response_sha256: wire
                .provider_response_sha256
                .ok_or(AutomationExecutionAdapterFailure::PermanentInvalidResponse)?,
        };
        validate_cloud_summary_output(&mut self.output, &result, hard_deadline)?;
        Ok(Some(result))
    }

    pub fn terminate(&mut self) {
        drop(self.containment.take());
    }
}

#[allow(dead_code)]
fn create_cloud_summary_pipe()
-> Result<(std::fs::File, std::fs::File), AutomationExecutionAdapterFailure> {
    let mut read = std::ptr::null_mut();
    let mut write = std::ptr::null_mut();
    let attributes = SECURITY_ATTRIBUTES {
        nLength: u32::try_from(std::mem::size_of::<SECURITY_ATTRIBUTES>())
            .expect("security attributes size fits u32"),
        lpSecurityDescriptor: std::ptr::null_mut(),
        bInheritHandle: 1,
    };
    if unsafe { CreatePipe(&mut read, &mut write, &attributes, 0) } == 0 {
        return Err(AutomationExecutionAdapterFailure::RetryableUnavailable);
    }
    // SAFETY: CreatePipe returned two unique owned handles. File takes exact
    // ownership and closes them on every return path.
    let read = unsafe { std::fs::File::from_raw_handle(read as RawHandle) };
    let write = unsafe { std::fs::File::from_raw_handle(write as RawHandle) };
    Ok((read, write))
}

#[allow(dead_code)]
fn write_cloud_summary_frame(
    write: std::fs::File,
    body: &[u8],
) -> Result<(), AutomationExecutionAdapterFailure> {
    let mut framed = Vec::with_capacity(body.len() + 4);
    framed.extend_from_slice(
        &u32::try_from(body.len())
            .map_err(|_| AutomationExecutionAdapterFailure::PermanentInvalidResponse)?
            .to_le_bytes(),
    );
    framed.extend_from_slice(body);
    let (sender, receiver) = std::sync::mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let result = (|| {
            let mut write = write;
            write
                .write_all(&framed)
                .map_err(|_| AutomationExecutionAdapterFailure::RetryableUnavailable)
        })();
        let _ = sender.send(result);
    });
    receiver
        .recv_timeout(CLOUD_SUMMARY_CONTROL_WRITE_DEADLINE)
        .map_err(|_| AutomationExecutionAdapterFailure::RetryableTimeout)?
}

#[allow(dead_code)]
fn validate_cloud_summary_output(
    output: &mut std::fs::File,
    result: &CloudSummaryHelperResult,
    hard_deadline: Instant,
) -> Result<(), AutomationExecutionAdapterFailure> {
    if !is_lowercase_sha256(&result.output_sha256)
        || !is_lowercase_sha256(&result.provider_request_id_sha256)
        || !is_lowercase_sha256(&result.provider_response_sha256)
        || result.output_bytes == 0
        || result.output_bytes > MAX_CLOUD_SUMMARY_OUTPUT_BYTES
        || result.output_word_count == 0
    {
        return Err(AutomationExecutionAdapterFailure::PermanentInvalidResponse);
    }
    let metadata = output
        .metadata()
        .map_err(|_| AutomationExecutionAdapterFailure::RetryableUnavailable)?;
    if !metadata.is_file() || metadata.len() != result.output_bytes {
        return Err(AutomationExecutionAdapterFailure::PermanentInvalidResponse);
    }
    output
        .seek(std::io::SeekFrom::Start(0))
        .map_err(|_| AutomationExecutionAdapterFailure::RetryableUnavailable)?;
    let actual = hash_open_file(output, hard_deadline)
        .map_err(|_| AutomationExecutionAdapterFailure::RetryableUnavailable)?;
    if actual != result.output_sha256 {
        return Err(AutomationExecutionAdapterFailure::PermanentInvalidResponse);
    }
    Ok(())
}

fn bounded_reap_child(child: &mut Child) -> Result<(), AutomationExecutionAdapterFailure> {
    let deadline = Instant::now() + AUTOMATION_EXECUTION_REAP_MAX;
    loop {
        if child
            .try_wait()
            .map_err(|_| AutomationExecutionAdapterFailure::RetryableUnavailable)?
            .is_some()
        {
            return Ok(());
        }
        if Instant::now() >= deadline {
            return Err(AutomationExecutionAdapterFailure::RetryableUnavailable);
        }
        std::thread::sleep(AUTOMATION_EXECUTION_CHILD_POLL_INTERVAL);
    }
}

#[doc(hidden)]
pub fn retained_pipe_wait_is_bounded_for_test() -> bool {
    let mut external = match Command::new("cmd.exe")
        .args(["/d", "/c", "ping -n 30 127.0.0.1 >nul"])
        .stdout(Stdio::piped())
        .spawn()
    {
        Ok(child) => child,
        Err(_) => return false,
    };
    let pipe = match external.stdout.take() {
        Some(pipe) => pipe,
        None => return false,
    };
    let worker = BoundedPipeDrain::new(pipe, 1024);
    let started = Instant::now();
    let result = worker.finish();
    let _ = external.kill();
    let _ = bounded_reap_child(&mut external);
    result.is_ok() && started.elapsed() < Duration::from_secs(1)
}

/// Runs one bounded queue decision. The store performs the authoritative
/// candidate, policy, adapter, configuration, connection-evidence, lease, and
/// receipt checks inside durable transactions; this controller never converts
/// an adapter return into success without that final commit.
pub fn run_automation_execution_controller_iteration(
    store: &StationStore,
    adapter: Option<&AutomationExecutionAdapter>,
    install_authority: &VerifiedInstallAuthority,
    worker_id: &str,
    clock: &mut dyn FnMut() -> Result<i64, String>,
) -> Result<bool, String> {
    run_automation_execution_controller_iteration_with_stop(
        store,
        adapter,
        install_authority,
        worker_id,
        clock,
        &NEVER_STOP_AUTOMATION_EXECUTION,
    )
}

pub fn run_automation_execution_controller_iteration_with_stop(
    store: &StationStore,
    adapter: Option<&AutomationExecutionAdapter>,
    install_authority: &VerifiedInstallAuthority,
    worker_id: &str,
    clock: &mut dyn FnMut() -> Result<i64, String>,
    stop: &AtomicBool,
) -> Result<bool, String> {
    let action_started_monotonic = Instant::now();
    run_automation_execution_controller_iteration_with_stop_and_elapsed(
        store,
        adapter,
        install_authority,
        worker_id,
        clock,
        stop,
        &mut || action_started_monotonic.elapsed(),
    )
}

#[doc(hidden)]
pub fn run_automation_execution_controller_iteration_with_stop_and_elapsed(
    store: &StationStore,
    adapter: Option<&AutomationExecutionAdapter>,
    install_authority: &VerifiedInstallAuthority,
    worker_id: &str,
    clock: &mut dyn FnMut() -> Result<i64, String>,
    stop: &AtomicBool,
    elapsed: &mut dyn FnMut() -> Duration,
) -> Result<bool, String> {
    let hard_deadline =
        Instant::now() + Duration::from_millis(AUTOMATION_EXECUTION_ACTION_MAX_MS as u64);
    let claimed_at_unix_ms = clock()?;
    let Some(claimed) = store.claim_next_automation_execution_job(
        worker_id,
        install_authority,
        claimed_at_unix_ms,
        AUTOMATION_EXECUTION_LEASE_MS,
    )?
    else {
        return Ok(false);
    };
    let mut lease_deadline_unix_ms = claimed
        .lease_expires_at_unix_ms
        .ok_or_else(|| "claimed automation execution job lacks a deadline".to_string())?;
    let action_deadline_unix_ms = claimed_at_unix_ms
        .checked_add(AUTOMATION_EXECUTION_ACTION_MAX_MS)
        .ok_or_else(|| "automation execution action deadline overflowed".to_string())?;
    let claim_id = claimed
        .claim_id
        .as_deref()
        .ok_or_else(|| "claimed automation execution job lacks a claim identity".to_string())?;

    let outcome = match adapter.filter(|adapter| adapter.adapter_id() == claimed.request.adapter_id)
    {
        Some(adapter) => {
            let _action_started_at_unix_ms = clock()?;
            if elapsed() >= Duration::from_millis(AUTOMATION_EXECUTION_ACTION_MAX_MS as u64)
                || stop.load(Ordering::Acquire)
            {
                Err(AutomationExecutionAdapterFailure::RetryableTimeout)
            } else {
                (|| -> Result<Result<AutomationExecutionReceipt, AutomationExecutionAdapterFailure>, String> {
                    let mut process = match OwnedAutomationExecutionProcess::spawn(
                        adapter,
                        &claimed.request,
                        claim_id,
                        action_deadline_unix_ms,
                        hard_deadline,
                    ) {
                        Ok(process) => process,
                        Err(failure) => return Ok(Err(failure)),
                    };
                    let mut renewal_at_unix_ms = lease_deadline_unix_ms
                        .checked_sub(AUTOMATION_EXECUTION_RENEWAL_LEAD_MS)
                        .ok_or_else(|| "automation execution renewal deadline overflowed".to_string())?;
                    loop {
                        if stop.load(Ordering::Acquire) {
                            if process.terminate().is_err() {
                                break Ok(Err(AutomationExecutionAdapterFailure::RetryableUnavailable));
                            }
                            break Ok(Err(AutomationExecutionAdapterFailure::RetryableTimeout));
                        }
                        let now_unix_ms = clock()?;
                        if elapsed()
                            >= Duration::from_millis(AUTOMATION_EXECUTION_ACTION_MAX_MS as u64)
                        {
                            if process.terminate().is_err() {
                                break Ok(Err(AutomationExecutionAdapterFailure::RetryableUnavailable));
                            }
                            break Ok(Err(AutomationExecutionAdapterFailure::RetryableTimeout));
                        }
                        if now_unix_ms >= renewal_at_unix_ms {
                            let renewed = store.renew_automation_execution_job_lease(
                                &claimed.request.job_id,
                                worker_id,
                                claim_id,
                                install_authority,
                                now_unix_ms,
                                AUTOMATION_EXECUTION_LEASE_MS,
                            )?;
                            lease_deadline_unix_ms = renewed.lease_expires_at_unix_ms.ok_or_else(
                                || "renewed automation execution job lacks a deadline".to_string(),
                            )?;
                            renewal_at_unix_ms = lease_deadline_unix_ms
                                .checked_sub(AUTOMATION_EXECUTION_RENEWAL_LEAD_MS)
                                .ok_or_else(|| "automation execution renewal deadline overflowed".to_string())?;
                        }
                        match process.try_receipt() {
                            Ok(Some(receipt)) => break Ok(Ok(receipt)),
                            Ok(None) => std::thread::sleep(AUTOMATION_EXECUTION_CHILD_POLL_INTERVAL),
                            Err(failure) => break Ok(Err(failure)),
                        }
                    }
                })()?
            }
        }
        None => Err(AutomationExecutionAdapterFailure::RetryableUnavailable),
    };

    let finished_at_unix_ms = clock()?;
    if finished_at_unix_ms >= lease_deadline_unix_ms {
        // The owned process tree was already terminated at the deadline.  Do
        // not let an expired C1 write a failure after another controller may
        // have reclaimed C2; the next durable claim transaction recovers it.
        return Ok(true);
    }
    match outcome {
        Ok(receipt) => {
            store.complete_automation_execution_job(
                &claimed.request.job_id,
                worker_id,
                &receipt,
                install_authority,
                finished_at_unix_ms,
            )?;
            Ok(true)
        }
        Err(failure) => {
            store.record_automation_execution_attempt_failure(
                &claimed.request.job_id,
                worker_id,
                claim_id,
                failure.code(),
                failure.retryable(),
                finished_at_unix_ms,
            )?;
            Ok(true)
        }
    }
}

pub fn run_automation_execution_queue_controller(
    database: &Path,
    application_root: &Path,
    stop: Arc<AtomicBool>,
) -> Result<(), String> {
    let mut backoff = AUTOMATION_EXECUTION_POLL_INTERVAL;
    while !stop.load(Ordering::Acquire) {
        match run_automation_execution_queue_iteration(database, application_root, &stop) {
            Ok(_) => {
                resolve_automation_execution_controller_alert(database);
                backoff = AUTOMATION_EXECUTION_POLL_INTERVAL;
            }
            Err(_) => {
                observe_automation_execution_controller_alert(database);
                backoff = (backoff * 2).min(AUTOMATION_EXECUTION_MAX_BACKOFF);
            }
        }
        std::thread::sleep(backoff);
    }
    Ok(())
}

const AUTOMATION_EXECUTION_CONTROLLER_ALERT_RESOURCE: &str = "automation-execution-controller";

fn observe_automation_execution_controller_alert(database: &Path) {
    let Ok(now) = unix_time_millis() else {
        return;
    };
    let Ok(store) = StationStore::open(database) else {
        return;
    };
    // This durable health event deliberately excludes provider, database,
    // prompt, credential, and filesystem error detail. It tells the operator
    // which controller is unhealthy while bounded retry supervision continues.
    let _ = store.observe_alert_and_route(&AlertEvent {
        event_id: "automation-execution-controller-service-down".into(),
        condition: AlertConditionKind::ServiceDown,
        severity: AlertSeverity::Warning,
        state: AlertEventState::Firing,
        resource_ref: AUTOMATION_EXECUTION_CONTROLLER_ALERT_RESOURCE.into(),
        summary: "Automation execution controller needs operator attention.".into(),
        detail: "The controller could not complete a durable queue iteration and is retrying with bounded backoff.".into(),
        source_section: "S13".into(),
        first_observed_at_unix_ms: now,
        last_observed_at_unix_ms: now,
        resolved_at_unix_ms: None,
        occurrence_count: 1,
        acknowledged_at_unix_ms: None,
        acknowledged_by: None,
    });
}

fn resolve_automation_execution_controller_alert(database: &Path) {
    let Ok(now) = unix_time_millis() else {
        return;
    };
    let Ok(store) = StationStore::open(database) else {
        return;
    };
    let _ = store.resolve_alert(
        AlertConditionKind::ServiceDown,
        AUTOMATION_EXECUTION_CONTROLLER_ALERT_RESOURCE,
        now,
    );
}

#[doc(hidden)]
pub fn run_automation_execution_queue_iteration(
    database: &Path,
    application_root: &Path,
    stop: &AtomicBool,
) -> Result<bool, String> {
    let data_root = database
        .parent()
        .ok_or_else(|| "automation execution data root is unavailable".to_string())?;
    let fence = station_maintenance::acquire_install_authority_fence_for_application(
        data_root,
        application_root,
    )
    .map_err(|_| "automation execution install authority is busy".to_string())?;
    if fence.was_abandoned() {
        return Err("automation execution install authority requires recovery".into());
    }
    let install_authority =
        station_maintenance::read_verified_install_authority(data_root, application_root)?;
    let store = StationStore::open(database)?;
    let mut clock = unix_time_millis;
    // No production semantic adapter is registered in this slice. The
    // controller still claims current durable work and records an exact
    // retryable unavailable attempt, rather than invoking the launch boundary
    // as a fake adapter or inventing model, provider, output, or receipt truth.
    // The schema-2 helper is optional. A future semantic adapter path must
    // verify its identity only if that path actually elects to execute it.
    run_automation_execution_controller_iteration_with_stop(
        &store,
        None,
        &install_authority,
        AUTOMATION_EXECUTION_WORKER_ID,
        &mut clock,
        stop,
    )
}

fn unix_time_millis() -> Result<i64, String> {
    let elapsed = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| "automation execution clock is before the Unix epoch".to_string())?;
    i64::try_from(elapsed.as_millis())
        .map_err(|_| "automation execution clock exceeds supported range".to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use station_automation_policy::{
        AutomationCandidateIdentity, AutomationInputArtifact, AutomationInputKind,
    };
    use std::sync::atomic::{AtomicU64, Ordering};

    static TEST_SEQUENCE: AtomicU64 = AtomicU64::new(0);

    fn summary_request(input: &[u8]) -> AutomationExecutionRequest {
        AutomationExecutionRequest {
            job_id: "33333333-2222-4333-8444-555555555555".into(),
            feature: AutomationFeature::Summaries,
            policy_id: "44444444-2222-4333-8444-555555555555".into(),
            policy_revision: 1,
            adapter_id: "11111111-2222-4333-8444-555555555555".into(),
            adapter_revision: 1,
            configuration_hash: "a".repeat(64),
            candidate: AutomationCandidateIdentity {
                version: "0.1.0".into(),
                source_commit: "b".repeat(40),
            },
            install_generation: 1,
            input: AutomationInputArtifact {
                kind: AutomationInputKind::Transcript,
                artifact_sha256: format!("{:x}", Sha256::digest(input)),
                bytes: input.len() as u64,
                media_type: CLOUD_SUMMARY_OUTPUT_MEDIA_TYPE.into(),
                language: Some("en-US".into()),
            },
            requested_output_language: None,
            requested_at_unix_ms: 1,
        }
    }

    fn cloud_fixture_root(name: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "townlight-cloud-summary-controller-{name}-{}",
            TEST_SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ))
    }

    #[test]
    fn controller_authority_failure_alert_is_durable_and_resolves() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("test clock is after epoch")
            .as_nanos();
        let database = std::env::temp_dir().join(format!(
            "townlight-automation-controller-health-{nonce}-{}.db",
            TEST_SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ));
        let store = StationStore::open(&database).expect("open alert authority");
        drop(store);

        observe_automation_execution_controller_alert(&database);
        let reopened = StationStore::open(&database).expect("reopen alert authority");
        assert!(
            reopened
                .read_firing_alert(
                    AlertConditionKind::ServiceDown,
                    AUTOMATION_EXECUTION_CONTROLLER_ALERT_RESOURCE,
                )
                .expect("read controller alert")
                .is_some()
        );
        drop(reopened);

        resolve_automation_execution_controller_alert(&database);
        let reopened = StationStore::open(&database).expect("reopen resolved authority");
        assert!(
            reopened
                .read_firing_alert(
                    AlertConditionKind::ServiceDown,
                    AUTOMATION_EXECUTION_CONTROLLER_ALERT_RESOURCE,
                )
                .expect("read resolved controller alert")
                .is_none()
        );
        drop(reopened);
        let _ = std::fs::remove_file(database);
    }

    #[test]
    fn cloud_summary_material_rejects_handle_alias_and_input_rebinding() {
        let root = cloud_fixture_root("alias");
        std::fs::create_dir_all(&root).unwrap();
        let input_bytes = b"meeting transcript";
        let input_path = root.join("input.txt");
        let credential_path = root.join("credential.bin");
        std::fs::write(&input_path, input_bytes).unwrap();
        std::fs::write(&credential_path, [1_u8, 0, 0, 0, b'x']).unwrap();
        let input = std::fs::OpenOptions::new()
            .read(true)
            .open(&input_path)
            .unwrap();
        let output_alias = input.try_clone().unwrap();
        let credential = std::fs::OpenOptions::new()
            .read(true)
            .open(&credential_path)
            .unwrap();
        let mut material = CloudSummaryExecutionMaterial::from_verified_handles(
            input,
            output_alias,
            credential,
            "gpt-4o-mini".into(),
        );
        assert!(matches!(
            material.validate_against_request(&summary_request(input_bytes)),
            Err(AutomationExecutionAdapterFailure::PermanentRejected)
        ));
        drop(material);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn cloud_summary_control_is_framed_without_paths_or_credentials() {
        let input = b"meeting transcript";
        let request = summary_request(input);
        let control = CloudSummaryHelperControl {
            mode: "cloud_summary_v1",
            job_id: &request.job_id,
            claim_id: "77777777-2222-4333-8444-555555555555",
            adapter_id: &request.adapter_id,
            adapter_revision: request.adapter_revision,
            candidate_version: &request.candidate.version,
            candidate_commit: &request.candidate.source_commit,
            install_generation: request.install_generation,
            configuration_sha256: &request.configuration_hash,
            endpoint: CLOUD_SUMMARY_ENDPOINT,
            model: "gpt-4o-mini",
            input_sha256: &request.input.artifact_sha256,
            input_bytes: request.input.bytes,
            output_media_type: CLOUD_SUMMARY_OUTPUT_MEDIA_TYPE,
            action_timeout_ms: 5_000,
            max_output_bytes: MAX_CLOUD_SUMMARY_OUTPUT_BYTES,
        };
        let serialized = serde_json::to_string(&control).unwrap();
        assert!(!serialized.contains("path"));
        assert!(!serialized.contains("credential"));
        assert!(!serialized.contains("secret"));
        let (mut read, write) = create_cloud_summary_pipe().unwrap();
        write_cloud_summary_frame(write, serialized.as_bytes()).unwrap();
        let mut header = [0_u8; 4];
        read.read_exact(&mut header).unwrap();
        assert_eq!(u32::from_le_bytes(header) as usize, serialized.len());
        let mut body = vec![0_u8; serialized.len()];
        read.read_exact(&mut body).unwrap();
        assert_eq!(body, serialized.as_bytes());
    }

    #[test]
    fn cloud_summary_result_requires_retained_output_hash_and_bounds() {
        let root = cloud_fixture_root("output");
        std::fs::create_dir_all(&root).unwrap();
        let output_path = root.join("output.txt");
        let output_bytes = b"Short verified summary.";
        std::fs::write(&output_path, output_bytes).unwrap();
        let mut output = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(&output_path)
            .unwrap();
        let result = CloudSummaryHelperResult {
            output_sha256: format!("{:x}", Sha256::digest(output_bytes)),
            output_bytes: output_bytes.len() as u64,
            output_word_count: 3,
            provider_request_id_sha256: "c".repeat(64),
            provider_response_sha256: "d".repeat(64),
        };
        assert!(
            validate_cloud_summary_output(
                &mut output,
                &result,
                Instant::now() + Duration::from_secs(1)
            )
            .is_ok()
        );
        let mut forged = result;
        forged.output_sha256 = "e".repeat(64);
        assert!(matches!(
            validate_cloud_summary_output(
                &mut output,
                &forged,
                Instant::now() + Duration::from_secs(1)
            ),
            Err(AutomationExecutionAdapterFailure::PermanentInvalidResponse)
        ));
        drop(output);
        std::fs::remove_dir_all(root).unwrap();
    }
}
