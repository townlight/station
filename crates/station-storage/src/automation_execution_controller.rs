use super::*;
use sha2::{Digest, Sha256};
use station_automation_policy::{AutomationExecutionReceipt, AutomationExecutionRequest};
use station_domain::{AlertConditionKind, AlertEvent, AlertEventState, AlertSeverity};
use station_maintenance::VerifiedInstallAuthority;
use std::ffi::OsString;
use std::io::Read;
use std::os::windows::ffi::OsStringExt;
use std::os::windows::fs::OpenOptionsExt;
use std::os::windows::io::AsRawHandle;
use std::os::windows::process::CommandExt;
use std::path::{Component, Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
    mpsc::{self, Receiver},
};
use std::thread::JoinHandle;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use windows_sys::Win32::Foundation::{CloseHandle, HANDLE, INVALID_HANDLE_VALUE};
use windows_sys::Win32::Storage::FileSystem::{
    BY_HANDLE_FILE_INFORMATION, FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT,
    FILE_SHARE_READ, FILE_SHARE_WRITE, GetFileInformationByHandle,
};
use windows_sys::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, TH32CS_SNAPTHREAD, THREADENTRY32, Thread32First, Thread32Next,
};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JobObjectExtendedLimitInformation,
    SetInformationJobObject, TerminateJobObject,
};
use windows_sys::Win32::System::Threading::{
    CREATE_SUSPENDED, GetCurrentThreadId, OpenThread, QueryFullProcessImageNameW, ResumeThread,
    THREAD_SUSPEND_RESUME, THREAD_TERMINATE,
};

#[link(name = "kernel32")]
unsafe extern "system" {
    fn CancelSynchronousIo(thread: HANDLE) -> i32;
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
static NEVER_STOP_AUTOMATION_EXECUTION: AtomicBool = AtomicBool::new(false);
static DRAIN_NATIVE_HANDLES_CLOSED: std::sync::atomic::AtomicU64 =
    std::sync::atomic::AtomicU64::new(0);

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
    pub fn new(
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

    #[doc(hidden)]
    pub fn with_test_setup_fault(mut self, fault: AutomationExecutionSetupFault) -> Self {
        self.setup_fault = Some(fault);
        self
    }

    #[doc(hidden)]
    pub fn verify_program_for_test(&self, request: &AutomationExecutionRequest) -> bool {
        self.pin_for_request(request, Instant::now() + Duration::from_secs(60))
            .is_ok()
    }

    #[doc(hidden)]
    pub fn substitution_is_blocked_while_pinned_for_test(
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
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
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

fn spawn_bounded_pipe_drain<R: Read + Send + 'static>(mut pipe: R, limit: u64) -> DrainWorker {
    let (sender, receiver) = mpsc::sync_channel(1);
    let (id_sender, id_receiver) = mpsc::sync_channel(1);
    let join = std::thread::spawn(move || {
        let _ = id_sender.send(unsafe { GetCurrentThreadId() });
        let result = (|| {
            let mut bytes = Vec::new();
            pipe.by_ref()
                .take(limit + 1)
                .read_to_end(&mut bytes)
                .map_err(|_| AutomationExecutionAdapterFailure::RetryableUnavailable)?;
            if bytes.len() as u64 > limit {
                return Err(AutomationExecutionAdapterFailure::PermanentInvalidResponse);
            }
            Ok(bytes)
        })();
        let _ = sender.send(result);
    });
    let thread_id = id_receiver.recv().unwrap_or(0);
    let thread_handle = unsafe { OpenThread(THREAD_TERMINATE, 0, thread_id) };
    DrainWorker {
        receiver,
        join: Some(join),
        thread_handle: OwnedThreadHandle(thread_handle),
    }
}

struct DrainWorker {
    receiver: Receiver<Result<Vec<u8>, AutomationExecutionAdapterFailure>>,
    join: Option<JoinHandle<()>>,
    thread_handle: OwnedThreadHandle,
}

struct OwnedThreadHandle(HANDLE);

impl Drop for OwnedThreadHandle {
    fn drop(&mut self) {
        if !self.0.is_null() && self.0 != INVALID_HANDLE_VALUE {
            unsafe { CloseHandle(self.0) };
            DRAIN_NATIVE_HANDLES_CLOSED.fetch_add(1, Ordering::Relaxed);
        }
    }
}

/// The only execution resource the controller supervises.  It is created by
/// the host after it has acquired the durable claim, placed in a fresh Windows
/// Job Object immediately, and never exposes a blocking adapter callback.
struct OwnedAutomationExecutionProcess {
    child: Child,
    job: WindowsProcessTree,
    _pinned_executable: PinnedAutomationExecutable,
    stdout: Option<DrainWorker>,
    stderr: Option<DrainWorker>,
    drained_stdout: Option<Vec<u8>>,
    hard_deadline: Instant,
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
        let stdout = spawn_bounded_pipe_drain(stdout, MAX_AUTOMATION_EXECUTION_STDOUT_BYTES);
        let stderr = spawn_bounded_pipe_drain(stderr, MAX_AUTOMATION_EXECUTION_STDERR_BYTES);
        let resume = if adapter.setup_fault == Some(AutomationExecutionSetupFault::ResumeThread) {
            Err(AutomationExecutionAdapterFailure::RetryableUnavailable)
        } else {
            resume_suspended_process_primary_thread(child.id())
        };
        if let Err(error) = resume {
            job.terminate()?;
            bounded_reap_child(&mut child)?;
            let drain_deadline = hard_deadline.min(Instant::now() + AUTOMATION_EXECUTION_REAP_MAX);
            let _ = finish_drain(stdout, drain_deadline);
            let _ = finish_drain(stderr, drain_deadline);
            return Err(error);
        }
        Ok(Self {
            child,
            job,
            _pinned_executable: pinned_executable,
            stdout: Some(stdout),
            stderr: Some(stderr),
            drained_stdout: None,
            hard_deadline,
        })
    }

    fn terminate_and_reap(&mut self) -> Result<(), AutomationExecutionAdapterFailure> {
        self.job.terminate()?;
        bounded_reap_child(&mut self.child)?;
        let drain_deadline = self
            .hard_deadline
            .min(Instant::now() + AUTOMATION_EXECUTION_REAP_MAX);
        let stdout = self
            .stdout
            .take()
            .ok_or(AutomationExecutionAdapterFailure::RetryableUnavailable)?;
        let stdout = finish_drain(stdout, drain_deadline)?;
        let _stderr = self
            .stderr
            .take()
            .ok_or(AutomationExecutionAdapterFailure::RetryableUnavailable)?;
        let _stderr = finish_drain(_stderr, drain_deadline)?;
        // Retain only the success payload. stderr is concurrently drained so
        // a child cannot deadlock on it, but it is never persisted or exposed.
        self.drained_stdout = Some(stdout);
        Ok(())
    }

    fn try_receipt(
        &mut self,
    ) -> Result<Option<AutomationExecutionReceipt>, AutomationExecutionAdapterFailure> {
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
    let mut buffer = vec![0_u16; 32_768];
    let mut length = u32::try_from(buffer.len()).unwrap();
    if unsafe {
        QueryFullProcessImageNameW(
            child.as_raw_handle() as HANDLE,
            0,
            buffer.as_mut_ptr(),
            &mut length,
        )
    } == 0
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

fn finish_drain(
    mut worker: DrainWorker,
    deadline: Instant,
) -> Result<Vec<u8>, AutomationExecutionAdapterFailure> {
    finish_drain_with_fault(&mut worker, deadline, None)
}

#[derive(Clone, Copy)]
enum DrainFault {
    OpenThread,
    Cancel,
    PostCancelTimeout,
}

fn finish_drain_with_fault(
    worker: &mut DrainWorker,
    deadline: Instant,
    fault: Option<DrainFault>,
) -> Result<Vec<u8>, AutomationExecutionAdapterFailure> {
    let remaining = deadline.saturating_duration_since(Instant::now());
    let result = match worker.receiver.recv_timeout(remaining) {
        Ok(result) => result,
        Err(_) => {
            if worker.thread_handle.0.is_null()
                || worker.thread_handle.0 == INVALID_HANDLE_VALUE
                || unsafe { CancelSynchronousIo(worker.thread_handle.0) } == 0
            {
                return Err(AutomationExecutionAdapterFailure::RetryableUnavailable);
            }
            worker
                .receiver
                .recv_timeout(AUTOMATION_EXECUTION_REAP_MAX)
                .map_err(|_| AutomationExecutionAdapterFailure::RetryableUnavailable)?
        }
    };
    worker
        .join
        .take()
        .ok_or(AutomationExecutionAdapterFailure::RetryableUnavailable)?
        .join()
        .map_err(|_| AutomationExecutionAdapterFailure::RetryableUnavailable)?;
    if fault.is_some() {
        return Err(AutomationExecutionAdapterFailure::RetryableUnavailable);
    }
    result
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
    let worker = spawn_bounded_pipe_drain(pipe, 1024);
    let started = Instant::now();
    let result = finish_drain(worker, started + Duration::from_millis(25));
    let _ = external.kill();
    let _ = bounded_reap_child(&mut external);
    result.is_err() && started.elapsed() < Duration::from_secs(3)
}

#[doc(hidden)]
pub fn drain_fault_paths_close_native_handle_for_test() -> bool {
    [
        DrainFault::OpenThread,
        DrainFault::Cancel,
        DrainFault::PostCancelTimeout,
    ]
    .into_iter()
    .all(|fault| {
        let before = DRAIN_NATIVE_HANDLES_CLOSED.load(Ordering::Relaxed);
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
        let mut worker = spawn_bounded_pipe_drain(pipe, 1024);
        let result = finish_drain_with_fault(
            &mut worker,
            Instant::now() + Duration::from_millis(25),
            Some(fault),
        );
        drop(worker);
        let _ = external.kill();
        let _ = bounded_reap_child(&mut external);
        result.is_err() && DRAIN_NATIVE_HANDLES_CLOSED.load(Ordering::Relaxed) > before
    })
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

fn run_automation_execution_queue_iteration(
    database: &Path,
    application_root: &Path,
    stop: &AtomicBool,
) -> Result<bool, String> {
    let data_root = database
        .parent()
        .ok_or_else(|| "automation execution data root is unavailable".to_string())?;
    let fence = station_maintenance::acquire_install_authority_fence(data_root)
        .map_err(|_| "automation execution install authority is busy".to_string())?;
    if fence.was_abandoned() {
        return Err("automation execution install authority requires recovery".into());
    }
    let install_authority =
        station_maintenance::read_verified_install_authority(data_root, application_root)?;
    let store = StationStore::open(database)?;
    let mut clock = unix_time_millis;
    // No production adapter is registered in this slice. The controller still
    // claims current durable work and records an exact retryable unavailable
    // attempt, rather than inventing model, provider, output, or receipt truth.
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
    use std::sync::atomic::{AtomicU64, Ordering};

    static TEST_SEQUENCE: AtomicU64 = AtomicU64::new(0);

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
}
