use std::ffi::c_void;
use std::fs::File;
use std::io::{Read, Write};
use std::os::windows::ffi::OsStrExt;
use std::os::windows::io::{AsRawHandle, FromRawHandle, RawHandle};
use std::path::Path;
use std::ptr;
use std::time::{Duration, Instant};

type Bool = i32;
type Dword = u32;
type Handle = *mut c_void;

const PIPE_PREFIX: &str = r"\\.\pipe\townlight-station\";
const INVALID_HANDLE_VALUE: Handle = -1_isize as Handle;
const PIPE_ACCESS_DUPLEX: Dword = 0x0000_0003;
const FILE_FLAG_FIRST_PIPE_INSTANCE: Dword = 0x0008_0000;
const PIPE_TYPE_BYTE: Dword = 0x0000_0000;
const PIPE_READMODE_BYTE: Dword = 0x0000_0000;
const PIPE_WAIT: Dword = 0x0000_0000;
const PIPE_NOWAIT: Dword = 0x0000_0001;
const PIPE_REJECT_REMOTE_CLIENTS: Dword = 0x0000_0008;
const GENERIC_READ: Dword = 0x8000_0000;
const GENERIC_WRITE: Dword = 0x4000_0000;
const OPEN_EXISTING: Dword = 3;
const SECURITY_SQOS_PRESENT: Dword = 0x0010_0000;
const SECURITY_IDENTIFICATION: Dword = 0x0001_0000;
const SECURITY_DESCRIPTOR_REVISION: Dword = 1;
const ERROR_PIPE_BUSY: Dword = 231;
const ERROR_NO_DATA: Dword = 232;
const ERROR_PIPE_CONNECTED: Dword = 535;
const ERROR_PIPE_LISTENING: Dword = 536;
const PIPE_BUFFER_BYTES: Dword = 65_540;
const PIPE_CONNECT_TIMEOUT_MILLISECONDS: Dword = 5_000;
const PIPE_SDDL: &str = "D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;OW)";
const JOB_OBJECT_EXTENDED_LIMIT_INFORMATION: i32 = 9;
const JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: Dword = 0x0000_2000;
const CREATE_SUSPENDED: Dword = 0x0000_0004;
const EXTENDED_STARTUPINFO_PRESENT: Dword = 0x0008_0000;
const STARTF_USESTDHANDLES: Dword = 0x0000_0100;
const HANDLE_FLAG_INHERIT: Dword = 0x0000_0001;
const PROC_THREAD_ATTRIBUTE_HANDLE_LIST: usize = 0x0002_0002;
const ERROR_INSUFFICIENT_BUFFER: Dword = 122;
const WAIT_OBJECT_0: Dword = 0;
const WAIT_TIMEOUT: Dword = 258;
const WAIT_FAILED: Dword = Dword::MAX;

#[repr(C)]
struct SecurityAttributes {
    length: Dword,
    security_descriptor: *mut c_void,
    inherit_handle: Bool,
}

#[repr(C)]
struct StartupInfoW {
    cb: Dword,
    reserved: *mut u16,
    desktop: *mut u16,
    title: *mut u16,
    x: Dword,
    y: Dword,
    x_size: Dword,
    y_size: Dword,
    x_count_chars: Dword,
    y_count_chars: Dword,
    fill_attribute: Dword,
    flags: Dword,
    show_window: u16,
    reserved2_count: u16,
    reserved2: *mut u8,
    std_input: Handle,
    std_output: Handle,
    std_error: Handle,
}

#[repr(C)]
struct ProcessInformation {
    process: Handle,
    thread: Handle,
    process_id: Dword,
    thread_id: Dword,
}

#[repr(C)]
struct StartupInfoExW {
    startup_info: StartupInfoW,
    attribute_list: *mut c_void,
}

#[repr(C)]
struct JobObjectBasicLimitInformation {
    per_process_user_time_limit: i64,
    per_job_user_time_limit: i64,
    limit_flags: Dword,
    minimum_working_set_size: usize,
    maximum_working_set_size: usize,
    active_process_limit: Dword,
    affinity: usize,
    priority_class: Dword,
    scheduling_class: Dword,
}

#[repr(C)]
struct JobObjectIoCounters {
    read_operation_count: u64,
    write_operation_count: u64,
    other_operation_count: u64,
    read_transfer_count: u64,
    write_transfer_count: u64,
    other_transfer_count: u64,
}

#[repr(C)]
struct JobObjectExtendedLimitInformation {
    basic_limit_information: JobObjectBasicLimitInformation,
    io_info: JobObjectIoCounters,
    process_memory_limit: usize,
    job_memory_limit: usize,
    peak_process_memory_used: usize,
    peak_job_memory_used: usize,
}

#[link(name = "kernel32")]
unsafe extern "system" {
    fn CreateNamedPipeW(
        name: *const u16,
        open_mode: Dword,
        pipe_mode: Dword,
        max_instances: Dword,
        output_buffer_size: Dword,
        input_buffer_size: Dword,
        default_timeout: Dword,
        security_attributes: *mut SecurityAttributes,
    ) -> Handle;
    fn ConnectNamedPipe(pipe: Handle, overlapped: *mut c_void) -> Bool;
    fn CreateFileW(
        name: *const u16,
        desired_access: Dword,
        share_mode: Dword,
        security_attributes: *mut c_void,
        creation_disposition: Dword,
        flags_and_attributes: Dword,
        template_file: Handle,
    ) -> Handle;
    fn WaitNamedPipeW(name: *const u16, timeout: Dword) -> Bool;
    fn SetNamedPipeHandleState(
        pipe: Handle,
        mode: *mut Dword,
        maximum_collection_count: *mut Dword,
        collection_data_timeout: *mut Dword,
    ) -> Bool;
    fn PeekNamedPipe(
        pipe: Handle,
        buffer: *mut c_void,
        buffer_size: Dword,
        bytes_read: *mut Dword,
        total_bytes_available: *mut Dword,
        bytes_left_in_message: *mut Dword,
    ) -> Bool;
    fn WriteFile(
        file: Handle,
        buffer: *const c_void,
        bytes_to_write: Dword,
        bytes_written: *mut Dword,
        overlapped: *mut c_void,
    ) -> Bool;
    fn CreateJobObjectW(attributes: *mut SecurityAttributes, name: *const u16) -> Handle;
    fn SetInformationJobObject(
        job: Handle,
        information_class: i32,
        information: *mut c_void,
        information_length: Dword,
    ) -> Bool;
    fn AssignProcessToJobObject(job: Handle, process: Handle) -> Bool;
    fn CreateProcessW(
        application_name: *const u16,
        command_line: *mut u16,
        process_attributes: *mut SecurityAttributes,
        thread_attributes: *mut SecurityAttributes,
        inherit_handles: Bool,
        creation_flags: Dword,
        environment: *mut c_void,
        current_directory: *const u16,
        startup_info: *mut StartupInfoW,
        process_information: *mut ProcessInformation,
    ) -> Bool;
    fn ResumeThread(thread: Handle) -> Dword;
    fn TerminateProcess(process: Handle, exit_code: Dword) -> Bool;
    fn GetExitCodeProcess(process: Handle, exit_code: *mut Dword) -> Bool;
    fn WaitForSingleObject(handle: Handle, milliseconds: Dword) -> Dword;
    fn SetHandleInformation(handle: Handle, mask: Dword, flags: Dword) -> Bool;
    fn InitializeProcThreadAttributeList(
        attribute_list: *mut c_void,
        attribute_count: Dword,
        flags: Dword,
        size: *mut usize,
    ) -> Bool;
    fn UpdateProcThreadAttribute(
        attribute_list: *mut c_void,
        flags: Dword,
        attribute: usize,
        value: *mut c_void,
        size: usize,
        previous_value: *mut c_void,
        return_size: *mut usize,
    ) -> Bool;
    fn DeleteProcThreadAttributeList(attribute_list: *mut c_void);
    fn CloseHandle(handle: Handle) -> Bool;
    fn GetLastError() -> Dword;
    fn LocalFree(memory: *mut c_void) -> *mut c_void;
}

#[link(name = "advapi32")]
unsafe extern "system" {
    fn ConvertStringSecurityDescriptorToSecurityDescriptorW(
        string_security_descriptor: *const u16,
        string_revision: Dword,
        security_descriptor: *mut *mut c_void,
        security_descriptor_size: *mut Dword,
    ) -> Bool;
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum IpcError {
    InvalidName,
    InvalidByteCount,
    TimedOut { operation: &'static str },
    Os { operation: &'static str, code: u32 },
}

pub struct PipeServer {
    name: String,
    file: Option<File>,
}

impl PipeServer {
    pub fn bind(suffix: &str) -> Result<Self, IpcError> {
        if !valid_suffix(suffix) {
            return Err(IpcError::InvalidName);
        }
        let name = format!("{PIPE_PREFIX}{suffix}");
        let wide_name = wide_null(&name);
        let security = SecurityDescriptor::station_pipe()?;
        let mut attributes = SecurityAttributes {
            length: std::mem::size_of::<SecurityAttributes>() as Dword,
            security_descriptor: security.raw,
            inherit_handle: 0,
        };
        // SAFETY: both UTF-16 name and security descriptor remain live for the call, the
        // attributes structure has the Windows ABI layout, and the returned handle is owned here.
        let handle = unsafe {
            CreateNamedPipeW(
                wide_name.as_ptr(),
                PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE,
                PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_NOWAIT | PIPE_REJECT_REMOTE_CLIENTS,
                1,
                PIPE_BUFFER_BYTES,
                PIPE_BUFFER_BYTES,
                0,
                &mut attributes,
            )
        };
        if handle == INVALID_HANDLE_VALUE {
            return Err(last_error("CreateNamedPipeW"));
        }
        // SAFETY: `CreateNamedPipeW` returned a unique owned handle. `File` closes it once.
        let file = unsafe { File::from_raw_handle(handle as RawHandle) };
        Ok(Self {
            name,
            file: Some(file),
        })
    }

    pub fn name(&self) -> &str {
        &self.name
    }

    pub fn accept(self) -> Result<PipeStream, IpcError> {
        self.connect(None)
    }

    pub fn accept_timeout(self, timeout: Duration) -> Result<PipeStream, IpcError> {
        self.connect(Some(timeout))
    }

    fn connect(mut self, timeout: Option<Duration>) -> Result<PipeStream, IpcError> {
        let file = self.file.take().expect("a pipe server owns its handle");
        let started = Instant::now();
        loop {
            // SAFETY: the file owns a live nonblocking named-pipe server handle and no
            // OVERLAPPED operation is used.
            let connected =
                unsafe { ConnectNamedPipe(file.as_raw_handle() as Handle, ptr::null_mut()) };
            if connected != 0 {
                break;
            }
            // SAFETY: `GetLastError` reads the calling thread's error value.
            let code = unsafe { GetLastError() };
            if code == ERROR_PIPE_CONNECTED {
                break;
            }
            if code != ERROR_PIPE_LISTENING {
                return Err(IpcError::Os {
                    operation: "ConnectNamedPipe",
                    code,
                });
            }
            if timeout.is_some_and(|limit| started.elapsed() >= limit) {
                return Err(IpcError::TimedOut {
                    operation: "ConnectNamedPipe",
                });
            }
            std::thread::sleep(Duration::from_millis(2));
        }

        let mut blocking_mode = PIPE_READMODE_BYTE | PIPE_WAIT;
        // SAFETY: the handle is a connected named pipe and the mode pointer is valid for the call.
        if unsafe {
            SetNamedPipeHandleState(
                file.as_raw_handle() as Handle,
                &mut blocking_mode,
                ptr::null_mut(),
                ptr::null_mut(),
            )
        } == 0
        {
            return Err(last_error("SetNamedPipeHandleState"));
        }
        Ok(PipeStream { file })
    }
}

pub struct PipeStream {
    file: File,
}

/// Starts a process with its first thread suspended, assigns it to a dedicated
/// kill-on-close Job Object, and only then resumes it.
///
/// This is deliberately the only public worker-launch contract in this crate.
/// `std::process::Command::spawn` followed by `AssignProcessToJobObject` has a
/// real escape interval: an immediately-created descendant can outlive the
/// job assignment. Here no user-mode instruction in the child can run before
/// assignment succeeds. If any step fails, the not-yet-resumed process is
/// terminated or the assigned job is closed before this function returns.
pub fn launch_contained_process(
    executable: &Path,
    arguments: &[&str],
) -> Result<(ContainedProcess, ProcessContainment), IpcError> {
    let containment = ProcessContainment::new()?;
    let mut suspended = SuspendedProcess::create(executable, arguments)?;
    if let Err(error) = containment.assign_process(suspended.process_handle()) {
        suspended.abort();
        return Err(error);
    }
    if let Err(error) = suspended.resume() {
        drop(containment);
        suspended.abort();
        return Err(error);
    }
    Ok((suspended.into_process(), containment))
}

struct SuspendedProcess {
    process: Option<Handle>,
    primary_thread: Option<Handle>,
}

impl SuspendedProcess {
    fn create(executable: &Path, arguments: &[&str]) -> Result<Self, IpcError> {
        if executable.as_os_str().is_empty() {
            return Err(IpcError::InvalidName);
        }
        if executable.as_os_str().encode_wide().any(|unit| unit == 0)
            || arguments
                .iter()
                .any(|argument| argument.encode_utf16().any(|unit| unit == 0))
        {
            return Err(IpcError::InvalidName);
        }
        // A full application name avoids ambiguous path lookup in production.
        // Bare names (used by focused Windows tests and supported by Command)
        // must instead be resolved from the command line by CreateProcessW.
        let application_name = executable.is_absolute().then(|| wide_path(executable));
        let mut command_line = windows_command_line(executable, arguments);
        let stdio = NullStandardHandles::open()?;
        let handles = [stdio.stdin, stdio.stdout, stdio.stderr];
        let mut attributes = ThreadAttributeList::for_inherited_handles(&handles)?;
        let mut startup = StartupInfoExW {
            startup_info: StartupInfoW {
                cb: std::mem::size_of::<StartupInfoExW>() as Dword,
                reserved: ptr::null_mut(),
                desktop: ptr::null_mut(),
                title: ptr::null_mut(),
                x: 0,
                y: 0,
                x_size: 0,
                y_size: 0,
                x_count_chars: 0,
                y_count_chars: 0,
                fill_attribute: 0,
                flags: STARTF_USESTDHANDLES,
                show_window: 0,
                reserved2_count: 0,
                reserved2: ptr::null_mut(),
                std_input: stdio.stdin,
                std_output: stdio.stdout,
                std_error: stdio.stderr,
            },
            attribute_list: attributes.as_mut_ptr(),
        };
        let mut information: ProcessInformation = unsafe { std::mem::zeroed() };
        // SAFETY: application and command-line UTF-16 buffers remain live and
        // mutable for the call. The attribute list admits exactly the three
        // inheritable NUL standard handles; the process begins suspended.
        let created = unsafe {
            CreateProcessW(
                application_name
                    .as_ref()
                    .map_or(ptr::null(), |name| name.as_ptr()),
                command_line.as_mut_ptr(),
                ptr::null_mut(),
                ptr::null_mut(),
                1,
                CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT,
                ptr::null_mut(),
                ptr::null(),
                (&raw mut startup.startup_info).cast(),
                &mut information,
            )
        };
        drop(attributes);
        drop(stdio);
        if created == 0 {
            return Err(last_error("CreateProcessW"));
        }
        // CreateProcessW returned unique owned process and primary-thread
        // handles. This object owns and closes both unless ownership is
        // transferred after the containment-and-resume sequence completes.
        Ok(Self {
            process: Some(information.process),
            primary_thread: Some(information.thread),
        })
    }

    fn process_handle(&self) -> RawHandle {
        *self
            .process
            .as_ref()
            .expect("a suspended process owns its child until handoff") as RawHandle
    }

    fn resume(&mut self) -> Result<(), IpcError> {
        let thread = self
            .primary_thread
            .take()
            .expect("a suspended process primary thread is resumed once");
        // SAFETY: the owned primary-thread handle remains valid until closed
        // below. CREATE_SUSPENDED guarantees its initial suspend count is one.
        let result = unsafe { ResumeThread(thread) };
        let resume_error = (result != 1).then(|| {
            if result == Dword::MAX {
                last_error("ResumeThread")
            } else {
                IpcError::Os {
                    operation: "ResumeThread unexpected suspend count",
                    code: result,
                }
            }
        });
        // SAFETY: this object owns the primary thread handle exactly once.
        let close_result = unsafe { CloseHandle(thread) };
        if let Some(error) = resume_error {
            return Err(error);
        }
        if close_result == 0 {
            return Err(last_error("CloseHandle primary thread"));
        }
        Ok(())
    }

    fn abort(&mut self) {
        if let Some(process) = self.process.take() {
            // SAFETY: this object owns the process handle. TerminateProcess is
            // nonblocking and is used only while launch has failed.
            unsafe {
                TerminateProcess(process, 1);
                CloseHandle(process);
            }
        }
        if let Some(thread) = self.primary_thread.take() {
            // SAFETY: this object owns the un-resumed primary thread handle.
            unsafe { CloseHandle(thread) };
        }
    }

    fn into_process(mut self) -> ContainedProcess {
        let process = self
            .process
            .take()
            .expect("a successfully resumed process owns its child");
        ContainedProcess { process }
    }
}

impl Drop for SuspendedProcess {
    fn drop(&mut self) {
        self.abort();
    }
}

/// Owns the direct Windows process handle returned by the suspended launch
/// contract. Windows process handles do not require a Unix-style reap; callers
/// query the exit code and close the handle on drop.
pub struct ContainedProcess {
    process: Handle,
}

impl ContainedProcess {
    pub fn try_wait(&self) -> Result<Option<u32>, IpcError> {
        // A process may legitimately exit with code 259 (STILL_ACTIVE), so
        // GetExitCodeProcess alone cannot establish liveness. Ask the kernel
        // whether the process object is signaled first.
        let wait = unsafe { WaitForSingleObject(self.process, 0) };
        if wait == WAIT_TIMEOUT {
            return Ok(None);
        }
        if wait == WAIT_FAILED {
            return Err(last_error("WaitForSingleObject process"));
        }
        if wait != WAIT_OBJECT_0 {
            return Err(IpcError::Os {
                operation: "WaitForSingleObject unexpected result",
                code: wait,
            });
        }
        let mut exit_code = 0;
        // SAFETY: this object owns a live process handle and `exit_code` is
        // writable storage for the documented API result.
        if unsafe { GetExitCodeProcess(self.process, &mut exit_code) } == 0 {
            return Err(last_error("GetExitCodeProcess"));
        }
        Ok(Some(exit_code))
    }

    pub fn terminate(&self) -> Result<(), IpcError> {
        // SAFETY: this object owns a live process handle. TerminateProcess is
        // a nonblocking request; the supervisor performs its own bounded poll.
        if unsafe { TerminateProcess(self.process, 1) } == 0 {
            return Err(last_error("TerminateProcess"));
        }
        Ok(())
    }
}

impl Drop for ContainedProcess {
    fn drop(&mut self) {
        // SAFETY: this object uniquely owns the process handle.
        unsafe { CloseHandle(self.process) };
    }
}

struct NullStandardHandles {
    stdin: Handle,
    stdout: Handle,
    stderr: Handle,
}

impl NullStandardHandles {
    fn open() -> Result<Self, IpcError> {
        let stdin = open_inheritable_null(GENERIC_READ)?;
        let stdout = match open_inheritable_null(GENERIC_WRITE) {
            Ok(handle) => handle,
            Err(error) => {
                // SAFETY: the first handle is owned on this error path.
                unsafe { CloseHandle(stdin) };
                return Err(error);
            }
        };
        let stderr = match open_inheritable_null(GENERIC_WRITE) {
            Ok(handle) => handle,
            Err(error) => {
                // SAFETY: these handles are owned on this error path.
                unsafe {
                    CloseHandle(stdin);
                    CloseHandle(stdout);
                }
                return Err(error);
            }
        };
        Ok(Self {
            stdin,
            stdout,
            stderr,
        })
    }
}

impl Drop for NullStandardHandles {
    fn drop(&mut self) {
        // SAFETY: these three handles were opened uniquely for launch and have
        // been inherited by the child (if creation succeeded) before closure.
        unsafe {
            CloseHandle(self.stdin);
            CloseHandle(self.stdout);
            CloseHandle(self.stderr);
        }
    }
}

struct ThreadAttributeList {
    bytes: Vec<u8>,
}

impl ThreadAttributeList {
    fn for_inherited_handles(handles: &[Handle]) -> Result<Self, IpcError> {
        let mut bytes_required = 0;
        // SAFETY: this sizing call intentionally passes null; Windows returns
        // ERROR_INSUFFICIENT_BUFFER and the required attribute-list byte size.
        let initialized = unsafe {
            InitializeProcThreadAttributeList(ptr::null_mut(), 1, 0, &mut bytes_required)
        };
        if initialized != 0 || unsafe { GetLastError() } != ERROR_INSUFFICIENT_BUFFER {
            return Err(last_error("InitializeProcThreadAttributeList size"));
        }
        let mut bytes = vec![0_u8; bytes_required];
        // SAFETY: `bytes` has the exact size Windows requested and remains
        // alive for initialization and CreateProcessW.
        if unsafe {
            InitializeProcThreadAttributeList(bytes.as_mut_ptr().cast(), 1, 0, &mut bytes_required)
        } == 0
        {
            return Err(last_error("InitializeProcThreadAttributeList"));
        }
        // SAFETY: the initialized list, handle slice, and byte count all have
        // the required ABI representation. The list restricts inheritance to
        // NUL stdin/stdout/stderr rather than leaking parent handles.
        if unsafe {
            UpdateProcThreadAttribute(
                bytes.as_mut_ptr().cast(),
                0,
                PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                handles.as_ptr().cast_mut().cast(),
                std::mem::size_of_val(handles),
                ptr::null_mut(),
                ptr::null_mut(),
            )
        } == 0
        {
            // SAFETY: initialization succeeded, so Windows requires deletion.
            unsafe { DeleteProcThreadAttributeList(bytes.as_mut_ptr().cast()) };
            return Err(last_error("UpdateProcThreadAttribute handle list"));
        }
        Ok(Self { bytes })
    }

    fn as_mut_ptr(&mut self) -> *mut c_void {
        self.bytes.as_mut_ptr().cast()
    }
}

impl Drop for ThreadAttributeList {
    fn drop(&mut self) {
        // SAFETY: a successful constructor initialized this list once.
        unsafe { DeleteProcThreadAttributeList(self.bytes.as_mut_ptr().cast()) };
    }
}

fn open_inheritable_null(access: Dword) -> Result<Handle, IpcError> {
    let nul = wide_null("NUL");
    let mut attributes = SecurityAttributes {
        length: std::mem::size_of::<SecurityAttributes>() as Dword,
        security_descriptor: ptr::null_mut(),
        inherit_handle: 1,
    };
    // SAFETY: the NUL path and inheritable SECURITY_ATTRIBUTES remain live for
    // the call; this process owns any returned handle.
    let handle = unsafe {
        CreateFileW(
            nul.as_ptr(),
            access,
            0,
            (&raw mut attributes).cast(),
            OPEN_EXISTING,
            0,
            ptr::null_mut(),
        )
    };
    if handle == INVALID_HANDLE_VALUE {
        return Err(last_error("CreateFileW NUL"));
    }
    // SAFETY: the process owns the just-opened handle. This is intentionally
    // repeated even though SECURITY_ATTRIBUTES requested inheritance.
    if unsafe { SetHandleInformation(handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT) } == 0 {
        let error = last_error("SetHandleInformation NUL");
        // SAFETY: this error path still owns the handle.
        unsafe { CloseHandle(handle) };
        return Err(error);
    }
    Ok(handle)
}

fn windows_command_line(executable: &Path, arguments: &[&str]) -> Vec<u16> {
    let mut command_line = quote_windows_argument(&executable.to_string_lossy());
    for argument in arguments {
        command_line.push(' ');
        command_line.push_str(&quote_windows_argument(argument));
    }
    wide_null(&command_line)
}

fn quote_windows_argument(value: &str) -> String {
    if !value.is_empty()
        && !value
            .bytes()
            .any(|byte| byte.is_ascii_whitespace() || byte == b'\"')
    {
        return value.to_string();
    }
    let mut quoted = String::with_capacity(value.len() + 2);
    quoted.push('"');
    let mut slashes = 0;
    for character in value.chars() {
        match character {
            '\\' => slashes += 1,
            '"' => {
                quoted.extend(std::iter::repeat_n('\\', slashes * 2 + 1));
                quoted.push('"');
                slashes = 0;
            }
            _ => {
                quoted.extend(std::iter::repeat_n('\\', slashes));
                quoted.push(character);
                slashes = 0;
            }
        }
    }
    quoted.extend(std::iter::repeat_n('\\', slashes * 2));
    quoted.push('"');
    quoted
}

fn wide_path(path: &Path) -> Vec<u16> {
    path.as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect()
}

/// Owns a Windows Job Object which kills every assigned process when it is
/// dropped. A supervisor must retain this for the entire worker lifetime; the
/// kernel, not a best-effort background thread, then owns teardown.
pub struct ProcessContainment {
    job: Handle,
}

impl ProcessContainment {
    /// Creates an empty dedicated kill-on-close job. It is created before the
    /// worker process so the suspended-launch contract can assign before any
    /// child instruction is allowed to run.
    pub fn new() -> Result<Self, IpcError> {
        // SAFETY: null attributes/name request a private unnamed job object.
        let job = unsafe { CreateJobObjectW(ptr::null_mut(), ptr::null()) };
        if job.is_null() || job == INVALID_HANDLE_VALUE {
            return Err(last_error("CreateJobObjectW"));
        }
        let mut limits: JobObjectExtendedLimitInformation = unsafe { std::mem::zeroed() };
        limits.basic_limit_information.limit_flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        // SAFETY: `job` is a newly-created live job handle and `limits` is a
        // correctly sized, initialized ABI structure for this information class.
        if unsafe {
            SetInformationJobObject(
                job,
                JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                (&raw mut limits).cast(),
                std::mem::size_of::<JobObjectExtendedLimitInformation>() as Dword,
            )
        } == 0
        {
            let error = last_error("SetInformationJobObject");
            // SAFETY: this function owns the job handle after successful creation.
            unsafe { CloseHandle(job) };
            return Err(error);
        }
        Ok(Self { job })
    }

    /// Assigns the still-suspended process to this existing job. This remains
    /// private so production callers cannot recreate the unsafe old
    /// spawn-then-assign contract.
    fn assign_process(&self, process: RawHandle) -> Result<(), IpcError> {
        // SAFETY: `process` is an owned, live process handle borrowed for this
        // call; the job remains owned by this containment object.
        if unsafe { AssignProcessToJobObject(self.job, process as Handle) } == 0 {
            return Err(last_error("AssignProcessToJobObject"));
        }
        Ok(())
    }
}

impl Drop for ProcessContainment {
    fn drop(&mut self) {
        // SAFETY: this object uniquely owns the job handle. KILL_ON_JOB_CLOSE
        // makes closing it a non-blocking kernel termination request for every
        // assigned worker and descendant process.
        unsafe { CloseHandle(self.job) };
    }
}

impl PipeStream {
    pub fn connect(name: &str) -> Result<Self, IpcError> {
        let Some(suffix) = name.strip_prefix(PIPE_PREFIX) else {
            return Err(IpcError::InvalidName);
        };
        if !valid_suffix(suffix) {
            return Err(IpcError::InvalidName);
        }
        let wide_name = wide_null(name);
        loop {
            // SAFETY: the UTF-16 name remains live for the call; no inheritable security
            // attributes or template handle are supplied; the returned handle is owned here.
            let handle = unsafe {
                CreateFileW(
                    wide_name.as_ptr(),
                    GENERIC_READ | GENERIC_WRITE,
                    0,
                    ptr::null_mut(),
                    OPEN_EXISTING,
                    SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION,
                    ptr::null_mut(),
                )
            };
            if handle != INVALID_HANDLE_VALUE {
                // SAFETY: `CreateFileW` returned a unique owned handle. `File` closes it once.
                let file = unsafe { File::from_raw_handle(handle as RawHandle) };
                return Ok(Self { file });
            }
            // SAFETY: `GetLastError` reads the calling thread's error value.
            let code = unsafe { GetLastError() };
            if code != ERROR_PIPE_BUSY {
                return Err(IpcError::Os {
                    operation: "CreateFileW",
                    code,
                });
            }
            // SAFETY: the UTF-16 name remains live for the call.
            if unsafe { WaitNamedPipeW(wide_name.as_ptr(), PIPE_CONNECT_TIMEOUT_MILLISECONDS) } == 0
            {
                return Err(last_error("WaitNamedPipeW"));
            }
        }
    }

    pub fn wait_for_bytes(&self, minimum: usize, timeout: Duration) -> Result<(), IpcError> {
        let minimum = Dword::try_from(minimum).map_err(|_| IpcError::InvalidByteCount)?;
        let started = Instant::now();
        loop {
            let mut available = 0;
            // SAFETY: the handle is a connected named pipe and `available` points to writable
            // storage. No buffer is requested, so all other optional pointers are null.
            if unsafe {
                PeekNamedPipe(
                    self.file.as_raw_handle() as Handle,
                    ptr::null_mut(),
                    0,
                    ptr::null_mut(),
                    &mut available,
                    ptr::null_mut(),
                )
            } == 0
            {
                return Err(last_error("PeekNamedPipe"));
            }
            if available >= minimum {
                return Ok(());
            }
            if started.elapsed() >= timeout {
                return Err(IpcError::TimedOut {
                    operation: "PeekNamedPipe",
                });
            }
            std::thread::sleep(Duration::from_millis(2));
        }
    }

    /// Reads an exact byte count under a wall-clock deadline.
    ///
    /// The availability check deliberately waits for the whole frame before
    /// issuing `ReadFile`. A byte-mode named pipe can otherwise return a
    /// successful *partial* read; retrying that read would leave a caller with
    /// an incomplete frame and make a timeout destructive. With one reader per
    /// station pipe, bytes reported by `PeekNamedPipe` remain available for the
    /// immediate exact read.
    pub fn read_exact_timeout(
        &mut self,
        buffer: &mut [u8],
        timeout: Duration,
    ) -> Result<(), IpcError> {
        if buffer.is_empty() {
            return Ok(());
        }
        match self.wait_for_bytes(buffer.len(), timeout) {
            Ok(()) => {}
            Err(IpcError::TimedOut { .. }) => {
                return Err(IpcError::TimedOut {
                    operation: "ReadFile",
                });
            }
            Err(error) => return Err(error),
        }
        self.file.read_exact(buffer).map_err(|error| IpcError::Os {
            operation: "ReadFile",
            code: error.raw_os_error().unwrap_or_default() as Dword,
        })
    }

    /// Writes every byte under a wall-clock deadline. Named-pipe `File` values
    /// have no Rust-side buffer, so this is the command publication boundary;
    /// callers must not follow it with `FlushFileBuffers`, which can wait for a
    /// non-reading peer indefinitely.
    pub fn write_all_timeout(&mut self, buffer: &[u8], timeout: Duration) -> Result<(), IpcError> {
        let mut copy = buffer.to_vec();
        self.write_timeout(&mut copy, timeout)
    }

    fn write_timeout(&mut self, buffer: &mut [u8], timeout: Duration) -> Result<(), IpcError> {
        if buffer.is_empty() {
            return Ok(());
        }
        let started = Instant::now();
        self.set_wait_mode(PIPE_NOWAIT)?;
        let result = (|| {
            let mut offset = 0;
            while offset < buffer.len() {
                let remaining = &mut buffer[offset..];
                let byte_count =
                    Dword::try_from(remaining.len()).map_err(|_| IpcError::InvalidByteCount)?;
                let mut transferred = 0;
                // SAFETY: the connected pipe handle and source slice remain live
                // for the call; synchronous nonblocking I/O uses a null OVERLAPPED.
                let completed = unsafe {
                    WriteFile(
                        self.file.as_raw_handle() as Handle,
                        remaining.as_ptr().cast(),
                        byte_count,
                        &mut transferred,
                        ptr::null_mut(),
                    )
                };
                if completed != 0 {
                    if transferred == 0 {
                        // A byte-mode pipe in PIPE_NOWAIT may report a
                        // successful zero-byte write while its buffer is
                        // full. Treat that as backpressure under the same
                        // wall-clock deadline, not as publication success.
                        if started.elapsed() >= timeout {
                            return Err(IpcError::TimedOut {
                                operation: "WriteFile",
                            });
                        }
                        std::thread::sleep(Duration::from_millis(2));
                        continue;
                    }
                    offset += transferred as usize;
                    continue;
                }
                // SAFETY: `GetLastError` reads the calling thread's error value.
                let code = unsafe { GetLastError() };
                if code != ERROR_NO_DATA && code != ERROR_PIPE_BUSY {
                    return Err(IpcError::Os {
                        operation: "WriteFile",
                        code,
                    });
                }
                if started.elapsed() >= timeout {
                    return Err(IpcError::TimedOut {
                        operation: "WriteFile",
                    });
                }
                std::thread::sleep(Duration::from_millis(2));
            }
            Ok(())
        })();
        let restore = self.set_wait_mode(PIPE_WAIT);
        match (result, restore) {
            (Ok(()), Ok(())) => Ok(()),
            // A successful write is not a successful protocol operation if
            // this handle cannot be restored to its documented blocking mode.
            (Ok(()), Err(error)) => Err(error),
            // When both operations fail, preserve the write failure: it is the
            // causal error visible to the caller and has the useful deadline
            // classification. The handle is subsequently dropped by the
            // caller's fail-closed worker session.
            (Err(error), _) => Err(error),
        }
    }

    fn set_wait_mode(&self, mode: Dword) -> Result<(), IpcError> {
        let mut mode = PIPE_READMODE_BYTE | mode;
        // SAFETY: the handle is a connected named pipe and the mode pointer is
        // valid. Changing PIPE_WAIT only affects this handle's synchronous I/O.
        if unsafe {
            SetNamedPipeHandleState(
                self.file.as_raw_handle() as Handle,
                &mut mode,
                ptr::null_mut(),
                ptr::null_mut(),
            )
        } == 0
        {
            Err(last_error("SetNamedPipeHandleState"))
        } else {
            Ok(())
        }
    }
}

impl Read for PipeStream {
    fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
        self.file.read(buffer)
    }
}

impl Write for PipeStream {
    fn write(&mut self, buffer: &[u8]) -> std::io::Result<usize> {
        self.file.write(buffer)
    }

    fn flush(&mut self) -> std::io::Result<()> {
        self.file.flush()
    }
}

struct SecurityDescriptor {
    raw: *mut c_void,
}

impl SecurityDescriptor {
    fn station_pipe() -> Result<Self, IpcError> {
        let sddl = wide_null(PIPE_SDDL);
        let mut raw = ptr::null_mut();
        // SAFETY: the SDDL string is null-terminated and `raw` points to writable storage.
        let converted = unsafe {
            ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl.as_ptr(),
                SECURITY_DESCRIPTOR_REVISION,
                &mut raw,
                ptr::null_mut(),
            )
        };
        if converted == 0 {
            Err(last_error(
                "ConvertStringSecurityDescriptorToSecurityDescriptorW",
            ))
        } else {
            Ok(Self { raw })
        }
    }
}

impl Drop for SecurityDescriptor {
    fn drop(&mut self) {
        // SAFETY: the conversion API allocated this descriptor with `LocalAlloc`; it is freed once.
        unsafe { LocalFree(self.raw) };
    }
}

fn valid_suffix(suffix: &str) -> bool {
    !suffix.is_empty()
        && suffix.len() <= 120
        && suffix
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
}

fn wide_null(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(std::iter::once(0)).collect()
}

fn last_error(operation: &'static str) -> IpcError {
    // SAFETY: `GetLastError` reads the calling thread's error value.
    let code = unsafe { GetLastError() };
    IpcError::Os { operation, code }
}

#[cfg(all(test, windows))]
mod containment_tests {
    use super::*;
    use std::fs;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::thread;

    static TEST_PATH_SEQUENCE: AtomicU64 = AtomicU64::new(1);

    fn unique_test_path(name: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!(
            "townlight-station-{name}-{}-{}",
            std::process::id(),
            TEST_PATH_SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ))
    }

    fn wait_for_path(path: &Path, timeout: Duration) -> bool {
        let started = Instant::now();
        while started.elapsed() < timeout {
            if path.exists() {
                return true;
            }
            thread::sleep(Duration::from_millis(10));
        }
        path.exists()
    }

    fn powershell_write_script(path: &Path) -> String {
        format!(
            "Set-Content -LiteralPath '{}' -Value ran",
            path.to_string_lossy().replace('\'', "''")
        )
    }

    #[test]
    fn suspended_child_cannot_execute_before_job_assignment_and_resume() {
        let marker = unique_test_path("suspended-marker");
        let _ = fs::remove_file(&marker);
        let script = powershell_write_script(&marker);
        let mut suspended = SuspendedProcess::create(
            Path::new("powershell.exe"),
            &[
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                &script,
            ],
        )
        .expect("create suspended test child");

        thread::sleep(Duration::from_millis(150));
        assert!(
            !marker.exists(),
            "CREATE_SUSPENDED must prevent observable child code before assignment"
        );

        let containment = ProcessContainment::new().expect("create kill-on-close job");
        containment
            .assign_process(suspended.process_handle())
            .expect("assign still-suspended child before resume");
        thread::sleep(Duration::from_millis(50));
        assert!(
            !marker.exists(),
            "assignment alone must not resume the primary thread"
        );

        suspended.resume().expect("resume contained child");
        let child = suspended.into_process();
        assert!(
            wait_for_path(&marker, Duration::from_secs(3)),
            "the child must run only after the explicit resume"
        );
        drop(containment);
        let started = Instant::now();
        while child.try_wait().expect("query contained child").is_none()
            && started.elapsed() < Duration::from_secs(3)
        {
            thread::sleep(Duration::from_millis(10));
        }
        let _ = fs::remove_file(marker);
    }

    #[test]
    fn closing_containment_kills_a_resumed_descendant_tree() {
        let child_pid = unique_test_path("descendant-pid");
        let descendant_marker = unique_test_path("descendant-marker");
        let _ = fs::remove_file(&child_pid);
        let _ = fs::remove_file(&descendant_marker);
        let child_path = child_pid.to_string_lossy().replace('\'', "''");
        let marker_path = descendant_marker.to_string_lossy().replace('\'', "''");
        let script = format!(
            "$descendant = Start-Process -FilePath powershell.exe -WindowStyle Hidden -ArgumentList '-NoLogo','-NoProfile','-NonInteractive','-Command','Start-Sleep -Seconds 3; Set-Content -LiteralPath ''{marker_path}'' -Value escaped' -PassThru; Set-Content -LiteralPath '{child_path}' -Value $descendant.Id; Start-Sleep -Seconds 30"
        );
        let (child, containment) = launch_contained_process(
            Path::new("powershell.exe"),
            &[
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                &script,
            ],
        )
        .expect("launch contained tree");

        assert!(
            wait_for_path(&child_pid, Duration::from_secs(3)),
            "parent must have launched the descendant before containment closes"
        );
        drop(containment);
        let started = Instant::now();
        while child.try_wait().expect("query contained parent").is_none()
            && started.elapsed() < Duration::from_secs(3)
        {
            thread::sleep(Duration::from_millis(10));
        }
        assert!(
            child.try_wait().expect("query contained parent").is_some(),
            "job closure must terminate the direct worker as well"
        );
        thread::sleep(Duration::from_secs(4));
        assert!(
            !descendant_marker.exists(),
            "a descendant scheduled to write after job closure must be killed with its parent"
        );
        let _ = fs::remove_file(child_pid);
        let _ = fs::remove_file(descendant_marker);
    }
}
