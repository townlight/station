use std::ffi::c_void;
use std::fs::File;
use std::io::{Read, Write};
use std::os::windows::io::{AsRawHandle, FromRawHandle, RawHandle};
use std::ptr;

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
const PIPE_REJECT_REMOTE_CLIENTS: Dword = 0x0000_0008;
const GENERIC_READ: Dword = 0x8000_0000;
const GENERIC_WRITE: Dword = 0x4000_0000;
const OPEN_EXISTING: Dword = 3;
const SECURITY_SQOS_PRESENT: Dword = 0x0010_0000;
const SECURITY_IDENTIFICATION: Dword = 0x0001_0000;
const SECURITY_DESCRIPTOR_REVISION: Dword = 1;
const ERROR_PIPE_BUSY: Dword = 231;
const ERROR_PIPE_CONNECTED: Dword = 535;
const PIPE_BUFFER_BYTES: Dword = 65_540;
const PIPE_CONNECT_TIMEOUT_MILLISECONDS: Dword = 5_000;
const PIPE_SDDL: &str = "D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;OW)";

#[repr(C)]
struct SecurityAttributes {
    length: Dword,
    security_descriptor: *mut c_void,
    inherit_handle: Bool,
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
                PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
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

    pub fn accept(mut self) -> Result<PipeStream, IpcError> {
        let file = self.file.take().expect("a pipe server owns its handle");
        // SAFETY: the file owns a live named-pipe server handle and no OVERLAPPED operation is used.
        let connected =
            unsafe { ConnectNamedPipe(file.as_raw_handle() as Handle, ptr::null_mut()) };
        if connected == 0 {
            // SAFETY: `GetLastError` reads the calling thread's error value.
            let code = unsafe { GetLastError() };
            if code != ERROR_PIPE_CONNECTED {
                return Err(IpcError::Os {
                    operation: "ConnectNamedPipe",
                    code,
                });
            }
        }
        Ok(PipeStream { file })
    }
}

pub struct PipeStream {
    file: File,
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
