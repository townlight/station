use std::ffi::c_void;
use std::fs::File;
use std::io::Write as _;
use std::os::windows::ffi::OsStrExt as _;
use std::os::windows::io::{FromRawHandle as _, RawHandle};
use std::path::Path;
use std::ptr;
use std::time::{SystemTime, UNIX_EPOCH};

use station_auth::{
    BOOTSTRAP_MAX_LIFETIME_MS, BootstrapCredential, digest_secret, generate_secret_token,
};
use station_storage::StationStore;

type Bool = i32;
type Dword = u32;
type Handle = *mut c_void;

const HANDOFF_SDDL: &str = "D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;OW)";
const INVALID_HANDLE_VALUE: Handle = -1_isize as Handle;
const GENERIC_WRITE: Dword = 0x4000_0000;
const CREATE_NEW: Dword = 1;
const FILE_ATTRIBUTE_NORMAL: Dword = 0x0000_0080;
const SECURITY_DESCRIPTOR_REVISION: Dword = 1;
const ERROR_FILE_EXISTS: Dword = 80;
const ERROR_ALREADY_EXISTS: Dword = 183;

#[repr(C)]
struct SecurityAttributes {
    length: Dword,
    security_descriptor: *mut c_void,
    inherit_handle: Bool,
}

#[link(name = "kernel32")]
unsafe extern "system" {
    fn CreateFileW(
        name: *const u16,
        desired_access: Dword,
        share_mode: Dword,
        security_attributes: *mut SecurityAttributes,
        creation_disposition: Dword,
        flags_and_attributes: Dword,
        template_file: Handle,
    ) -> Handle;
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

struct SecurityDescriptor {
    raw: *mut c_void,
}

impl SecurityDescriptor {
    fn handoff() -> Result<Self, String> {
        let sddl = HANDOFF_SDDL
            .encode_utf16()
            .chain(std::iter::once(0))
            .collect::<Vec<_>>();
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
            Err(last_windows_error(
                "could not create bootstrap handoff security descriptor",
            ))
        } else {
            Ok(Self { raw })
        }
    }
}

impl Drop for SecurityDescriptor {
    fn drop(&mut self) {
        // SAFETY: the SDDL conversion allocated this descriptor; it is freed exactly once.
        unsafe { LocalFree(self.raw) };
    }
}

pub(crate) fn issue_bootstrap_handoff(database: &Path, handoff: &Path) -> Result<(), String> {
    let now_unix_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| "system clock is before the Unix epoch".to_string())?
        .as_millis()
        .try_into()
        .map_err(|_| "system clock cannot be represented in milliseconds".to_string())?;
    issue_bootstrap_handoff_at(database, handoff, now_unix_ms)
}

fn issue_bootstrap_handoff_at(
    database: &Path,
    handoff: &Path,
    now_unix_ms: i64,
) -> Result<(), String> {
    if !database.is_absolute() || !handoff.is_absolute() {
        return Err("bootstrap handoff requires absolute database and handoff paths".into());
    }
    if handoff
        .try_exists()
        .map_err(|error| format!("could not inspect bootstrap handoff path: {error}"))?
    {
        return Err("bootstrap handoff file already exists".into());
    }

    let store = StationStore::open(database)?;
    if store.has_operator_accounts()? {
        return Err("bootstrap handoff cannot be issued after an operator account exists".into());
    }
    if store.read_operator_bootstrap()?.is_some_and(|bootstrap| {
        bootstrap.consumed_at_unix_ms().is_none() && bootstrap.expires_at_unix_ms() > now_unix_ms
    }) {
        return Err("an unexpired bootstrap capability already exists".into());
    }

    let mut file = create_secure_handoff_file(handoff)?;
    let outcome = (|| {
        let token = generate_secret_token().map_err(|error| error.to_string())?;
        let expires_at_unix_ms = now_unix_ms
            .checked_add(BOOTSTRAP_MAX_LIFETIME_MS)
            .ok_or_else(|| "bootstrap expiry cannot be represented".to_string())?;
        let bootstrap =
            BootstrapCredential::new(digest_secret(&token), now_unix_ms, expires_at_unix_ms, None)
                .map_err(|error| error.to_string())?;

        file.write_all(token.expose_secret().as_bytes())
            .and_then(|()| file.write_all(b"\n"))
            .and_then(|()| file.sync_all())
            .map_err(|error| format!("could not write bootstrap handoff file: {error}"))?;

        if !store.provision_operator_bootstrap(&bootstrap)? {
            return Err(
                "bootstrap capability was not provisioned because bootstrap authority changed"
                    .into(),
            );
        }
        Ok(())
    })();

    if let Err(error) = outcome {
        drop(file);
        return match std::fs::remove_file(handoff) {
            Ok(()) => Err(error),
            Err(cleanup) => Err(format!(
                "{error}; the unusable bootstrap handoff file could not be removed: {cleanup}"
            )),
        };
    }
    Ok(())
}

fn create_secure_handoff_file(path: &Path) -> Result<File, String> {
    let wide_path = path
        .as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect::<Vec<_>>();
    let security = SecurityDescriptor::handoff()?;
    let mut attributes = SecurityAttributes {
        length: std::mem::size_of::<SecurityAttributes>() as Dword,
        security_descriptor: security.raw,
        inherit_handle: 0,
    };
    // SAFETY: path and security descriptor remain live through the call, attributes has the
    // Windows ABI layout, CREATE_NEW is atomic, and a successful handle is uniquely owned here.
    let handle = unsafe {
        CreateFileW(
            wide_path.as_ptr(),
            GENERIC_WRITE,
            0,
            &mut attributes,
            CREATE_NEW,
            FILE_ATTRIBUTE_NORMAL,
            ptr::null_mut(),
        )
    };
    if handle == INVALID_HANDLE_VALUE {
        // SAFETY: GetLastError reads the calling thread's error value immediately after failure.
        let code = unsafe { GetLastError() };
        if matches!(code, ERROR_FILE_EXISTS | ERROR_ALREADY_EXISTS) {
            Err("bootstrap handoff file already exists".into())
        } else {
            Err(windows_error(
                "could not create bootstrap handoff file",
                code,
            ))
        }
    } else {
        // SAFETY: CreateFileW returned a unique owned file handle. File closes it exactly once.
        Ok(unsafe { File::from_raw_handle(handle as RawHandle) })
    }
}

fn last_windows_error(operation: &str) -> String {
    // SAFETY: GetLastError reads the calling thread's error value.
    windows_error(operation, unsafe { GetLastError() })
}

fn windows_error(operation: &str, code: Dword) -> String {
    format!(
        "{operation}: {} (Windows error {code})",
        std::io::Error::from_raw_os_error(code as i32)
    )
}

#[cfg(test)]
mod tests {
    use std::ffi::c_void;
    use std::ptr;

    use station_auth::{BOOTSTRAP_MAX_LIFETIME_MS, SecretToken, digest_secret};

    use super::*;

    fn test_root(label: &str) -> std::path::PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("townlight-stationd-{label}-{nonce}"));
        std::fs::create_dir_all(&root).unwrap();
        root
    }

    #[test]
    fn handoff_is_create_new_and_never_overwrites_an_existing_file() {
        let root = test_root("handoff-create-new");
        let database = root.join("station.db");
        let handoff = root.join("bootstrap.capability");
        std::fs::write(&handoff, b"sentinel").unwrap();

        let error = issue_bootstrap_handoff_at(&database, &handoff, 1_000).unwrap_err();

        assert!(
            error.contains("already exists"),
            "unexpected error: {error}"
        );
        assert_eq!(std::fs::read(&handoff).unwrap(), b"sentinel");
        assert!(!database.exists());
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn handoff_contains_one_raw_token_while_sqlite_contains_only_its_digest() {
        let root = test_root("handoff-secret-boundary");
        let database = root.join("station.db");
        let handoff = root.join("bootstrap.capability");

        issue_bootstrap_handoff_at(&database, &handoff, 1_000).unwrap();

        let handoff_bytes = std::fs::read(&handoff).unwrap();
        assert_eq!(handoff_bytes.last(), Some(&b'\n'));
        assert_eq!(
            handoff_bytes.iter().filter(|byte| **byte == b'\n').count(),
            1
        );
        let plaintext = std::str::from_utf8(&handoff_bytes[..handoff_bytes.len() - 1]).unwrap();
        let token = SecretToken::try_from(plaintext).unwrap();
        let store = StationStore::open(&database).unwrap();
        let bootstrap = store.read_operator_bootstrap().unwrap().unwrap();
        assert_eq!(bootstrap.token_digest(), digest_secret(&token));
        assert_eq!(bootstrap.issued_at_unix_ms(), 1_000);
        assert_eq!(
            bootstrap.expires_at_unix_ms(),
            1_000 + BOOTSTRAP_MAX_LIFETIME_MS
        );
        drop(store);
        for sqlite_path in [
            database.clone(),
            std::path::PathBuf::from(format!("{}-wal", database.display())),
            std::path::PathBuf::from(format!("{}-shm", database.display())),
        ] {
            if let Ok(bytes) = std::fs::read(sqlite_path) {
                assert!(
                    !bytes
                        .windows(plaintext.len())
                        .any(|window| window == plaintext.as_bytes()),
                    "SQLite persisted the raw bootstrap capability"
                );
            }
        }
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn replacement_is_refused_until_the_stored_capability_expires() {
        let root = test_root("handoff-expiry");
        let database = root.join("station.db");
        let first_handoff = root.join("first.capability");
        let premature_handoff = root.join("premature.capability");
        let replacement_handoff = root.join("replacement.capability");

        issue_bootstrap_handoff_at(&database, &first_handoff, 1_000).unwrap();
        let error = issue_bootstrap_handoff_at(
            &database,
            &premature_handoff,
            1_000 + BOOTSTRAP_MAX_LIFETIME_MS - 1,
        )
        .unwrap_err();
        assert!(error.contains("unexpired"), "unexpected error: {error}");
        assert!(!premature_handoff.exists());

        issue_bootstrap_handoff_at(
            &database,
            &replacement_handoff,
            1_000 + BOOTSTRAP_MAX_LIFETIME_MS,
        )
        .unwrap();
        assert!(replacement_handoff.exists());
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn handoff_dacl_does_not_inherit_a_permissive_parent() {
        let root = permissive_test_root("handoff-dacl");
        let database = root.join("station.db");
        let handoff = root.join("bootstrap.capability");

        issue_bootstrap_handoff_at(&database, &handoff, 1_000).unwrap();

        let sddl = file_dacl_sddl(&handoff);
        eprintln!("handoff_dacl={sddl}");
        assert!(sddl.starts_with("D:P"), "DACL was not protected: {sddl}");
        for forbidden in [";;;WD)", ";;;BU)", ";;;AU)"] {
            assert!(
                !sddl.contains(forbidden),
                "handoff granted broad access through {forbidden}: {sddl}"
            );
        }
        for required in [";;;SY)", ";;;BA)", ";;;OW)"] {
            assert!(
                sddl.contains(required),
                "handoff omitted required principal {required}: {sddl}"
            );
        }
        assert_eq!(
            sddl.matches("(A;").count(),
            3,
            "handoff contained an unexpected access ACE: {sddl}"
        );
        std::fs::remove_dir_all(root).unwrap();
    }

    type Bool = i32;
    type Dword = u32;

    const SECURITY_DESCRIPTOR_REVISION: Dword = 1;
    const DACL_SECURITY_INFORMATION: Dword = 0x0000_0004;
    const SE_FILE_OBJECT: Dword = 1;

    #[repr(C)]
    struct TestSecurityAttributes {
        length: Dword,
        security_descriptor: *mut c_void,
        inherit_handle: Bool,
    }

    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn CreateDirectoryW(
            path: *const u16,
            security_attributes: *mut TestSecurityAttributes,
        ) -> Bool;
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
        fn GetNamedSecurityInfoW(
            object_name: *mut u16,
            object_type: Dword,
            security_information: Dword,
            owner: *mut *mut c_void,
            group: *mut *mut c_void,
            dacl: *mut *mut c_void,
            sacl: *mut *mut c_void,
            security_descriptor: *mut *mut c_void,
        ) -> Dword;
        fn ConvertSecurityDescriptorToStringSecurityDescriptorW(
            security_descriptor: *mut c_void,
            string_revision: Dword,
            security_information: Dword,
            string_security_descriptor: *mut *mut u16,
            string_security_descriptor_length: *mut Dword,
        ) -> Bool;
    }

    struct TestSecurityDescriptor(*mut c_void);

    impl TestSecurityDescriptor {
        fn from_sddl(value: &str) -> Self {
            let wide = wide_null(value);
            let mut raw = ptr::null_mut();
            // SAFETY: `wide` is null-terminated and `raw` points to writable storage.
            let converted = unsafe {
                ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    wide.as_ptr(),
                    SECURITY_DESCRIPTOR_REVISION,
                    &mut raw,
                    ptr::null_mut(),
                )
            };
            assert_ne!(converted, 0, "could not create test security descriptor");
            Self(raw)
        }
    }

    impl Drop for TestSecurityDescriptor {
        fn drop(&mut self) {
            // SAFETY: the SDDL conversion allocated this descriptor; it is freed once.
            unsafe { LocalFree(self.0) };
        }
    }

    fn permissive_test_root(label: &str) -> std::path::PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("townlight-stationd-{label}-{nonce}"));
        let descriptor = TestSecurityDescriptor::from_sddl("D:P(A;;GA;;;WD)");
        let mut attributes = TestSecurityAttributes {
            length: std::mem::size_of::<TestSecurityAttributes>() as Dword,
            security_descriptor: descriptor.0,
            inherit_handle: 0,
        };
        let wide = wide_null(root.as_os_str().to_str().unwrap());
        // SAFETY: path and descriptor live through the call and attributes has the Windows ABI.
        let created = unsafe { CreateDirectoryW(wide.as_ptr(), &mut attributes) };
        assert_ne!(created, 0, "CreateDirectoryW failed: {}", unsafe {
            GetLastError()
        });
        root
    }

    fn file_dacl_sddl(path: &Path) -> String {
        let mut wide_path = wide_null(path.as_os_str().to_str().unwrap());
        let mut descriptor = ptr::null_mut();
        // SAFETY: the path is null-terminated and descriptor points to writable storage.
        let code = unsafe {
            GetNamedSecurityInfoW(
                wide_path.as_mut_ptr(),
                SE_FILE_OBJECT,
                DACL_SECURITY_INFORMATION,
                ptr::null_mut(),
                ptr::null_mut(),
                ptr::null_mut(),
                ptr::null_mut(),
                &mut descriptor,
            )
        };
        assert_eq!(code, 0, "GetNamedSecurityInfoW failed: {code}");
        let descriptor = TestSecurityDescriptor(descriptor);
        let mut sddl = ptr::null_mut();
        let mut sddl_len = 0;
        // SAFETY: descriptor is valid and output pointers refer to writable storage.
        let converted = unsafe {
            ConvertSecurityDescriptorToStringSecurityDescriptorW(
                descriptor.0,
                SECURITY_DESCRIPTOR_REVISION,
                DACL_SECURITY_INFORMATION,
                &mut sddl,
                &mut sddl_len,
            )
        };
        assert_ne!(converted, 0, "could not render handoff DACL");
        // SAFETY: conversion returned `sddl_len` initialized UTF-16 code units.
        let rendered = String::from_utf16(unsafe {
            std::slice::from_raw_parts(sddl, usize::try_from(sddl_len).unwrap())
        })
        .unwrap();
        // SAFETY: conversion allocated this SDDL string; it is freed once after copying.
        unsafe { LocalFree(sddl.cast()) };
        rendered.trim_end_matches('\0').to_owned()
    }

    fn wide_null(value: &str) -> Vec<u16> {
        value.encode_utf16().chain(std::iter::once(0)).collect()
    }
}
