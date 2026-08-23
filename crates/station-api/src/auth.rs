use std::collections::BTreeSet;
use std::net::SocketAddr;
use std::sync::Mutex;

use station_auth::{
    AccountId, AuthCryptoError, CredentialVersion, OperatorAccount, OperatorDisplayName,
    OperatorPassword, OperatorRole, OperatorSession, OperatorUsername, PasswordPhc, RecoveryCode,
    SecretDigest, SecretToken, digest_recovery_code, digest_secret, generate_recovery_codes,
    generate_secret_token, hash_password, verify_password, verify_password_or_dummy,
};
use station_storage::{OperatorBootstrapInstallation, OperatorSessionAuthority, StationStore};

pub const SESSION_COOKIE_NAME: &str = "townlight_session_v1";
pub const CSRF_COOKIE_NAME: &str = "townlight_csrf_v1";
pub const CSRF_HEADER_NAME: &str = "x-townlight-csrf";
const MAX_COOKIE_HEADER_BYTES: usize = 8 * 1024;
const MAX_PASSWORD_VERIFIERS: usize = 2;
const SESSION_IDLE_MS: i64 = 30 * 60 * 1_000;
const SESSION_ABSOLUTE_MS: i64 = 12 * 60 * 60 * 1_000;
const SESSION_TOUCH_AFTER_MS: i64 = 60 * 1_000;
const MAX_LOGIN_DELAY_SECONDS: u64 = 300;

pub struct AuthRuntime {
    boot_id: SecretDigest,
    expected_host: String,
    expected_origin: String,
    password_verifiers: Mutex<usize>,
}

#[derive(Debug)]
pub enum AuthFailure {
    Invalid(&'static str),
    Unauthorized,
    BootstrapRequired,
    Conflict(&'static str),
    Busy,
    Throttled(u64),
    Storage(String),
}

impl From<String> for AuthFailure {
    fn from(error: String) -> Self {
        Self::Storage(error)
    }
}

#[derive(Debug)]
pub struct IssuedSession {
    pub session_token: SecretToken,
    pub csrf_token: SecretToken,
    pub authority: OperatorSessionAuthority,
}

#[derive(Debug)]
pub struct BootstrapResult {
    pub issued: IssuedSession,
    pub recovery_codes: Vec<String>,
}

impl AuthRuntime {
    pub fn new(address: SocketAddr) -> Result<Self, AuthCryptoError> {
        let boot_secret = generate_secret_token()?;
        let expected_host = address.to_string();
        Ok(Self {
            boot_id: digest_secret(&boot_secret),
            expected_origin: format!("http://{expected_host}"),
            expected_host,
            password_verifiers: Mutex::new(0),
        })
    }

    pub const fn boot_id(&self) -> SecretDigest {
        self.boot_id
    }

    pub fn expected_host(&self) -> &str {
        &self.expected_host
    }

    pub fn expected_origin(&self) -> &str {
        &self.expected_origin
    }

    pub fn try_password_permit(&self) -> Option<PasswordPermit<'_>> {
        let mut active = self.password_verifiers.lock().ok()?;
        if *active >= MAX_PASSWORD_VERIFIERS {
            return None;
        }
        *active += 1;
        Some(PasswordPermit { runtime: self })
    }
}

pub fn authenticate_session(
    store: &StationStore,
    runtime: &AuthRuntime,
    cookies: &AuthCookies,
    now_unix_ms: i64,
    touch: bool,
) -> Result<Option<OperatorSessionAuthority>, AuthFailure> {
    let (Some(session), Some(csrf)) = (&cookies.session, &cookies.csrf) else {
        return Ok(None);
    };
    let session_digest = digest_secret(session);
    let csrf_digest = digest_secret(csrf);
    let Some(authority) = store
        .validate_operator_session(
            &session_digest,
            &csrf_digest,
            now_unix_ms,
            &runtime.boot_id(),
        )
        .map_err(AuthFailure::Storage)?
    else {
        return Ok(None);
    };
    if touch
        && now_unix_ms.saturating_sub(authority.session.last_seen_at_unix_ms())
            >= SESSION_TOUCH_AFTER_MS
    {
        let idle_expires = now_unix_ms
            .saturating_add(SESSION_IDLE_MS)
            .min(authority.session.absolute_expires_at_unix_ms());
        if idle_expires <= now_unix_ms
            || !store
                .touch_operator_session(&session_digest, now_unix_ms, idle_expires)
                .map_err(AuthFailure::Storage)?
        {
            return Ok(None);
        }
    }
    Ok(Some(authority))
}

pub fn bootstrap(
    store: &StationStore,
    runtime: &AuthRuntime,
    capability: &str,
    username: &str,
    display_name: &str,
    password: &str,
    now_unix_ms: i64,
) -> Result<BootstrapResult, AuthFailure> {
    if store
        .has_operator_accounts()
        .map_err(AuthFailure::Storage)?
    {
        return Err(AuthFailure::Conflict(
            "operator account bootstrap is already complete",
        ));
    }
    let capability = SecretToken::try_from(capability).map_err(|_| AuthFailure::Unauthorized)?;
    let username = OperatorUsername::try_from(username)
        .map_err(|_| AuthFailure::Invalid("username is invalid"))?;
    let display_name = OperatorDisplayName::try_from(display_name)
        .map_err(|_| AuthFailure::Invalid("display_name is invalid"))?;
    let password =
        OperatorPassword::new(password).map_err(|_| AuthFailure::Invalid("password is invalid"))?;
    let _permit = runtime.try_password_permit().ok_or(AuthFailure::Busy)?;
    let password_phc =
        hash_password(&password).map_err(|error| AuthFailure::Storage(error.to_string()))?;
    let recovery =
        generate_recovery_codes().map_err(|error| AuthFailure::Storage(error.to_string()))?;
    let account = OperatorAccount::new(
        generate_account_id()?,
        username,
        display_name,
        password_phc,
        BTreeSet::from([OperatorRole::SetupAdmin]),
        CredentialVersion::new(1).map_err(|error| AuthFailure::Storage(error.to_string()))?,
        true,
        now_unix_ms,
        now_unix_ms,
    )
    .map_err(|error| AuthFailure::Storage(error.to_string()))?;
    let (session_token, csrf_token, session) = new_session(&account, runtime, now_unix_ms)?;
    let recovery_codes = recovery
        .display_codes
        .iter()
        .map(|code| code.expose_secret().to_owned())
        .collect::<Vec<_>>();
    let installed = store
        .consume_operator_bootstrap(
            &digest_secret(&capability),
            &OperatorBootstrapInstallation {
                account: account.clone(),
                session: session.clone(),
                recovery_digests: recovery.storage_digests.to_vec(),
            },
            now_unix_ms,
        )
        .map_err(AuthFailure::Storage)?;
    if !installed {
        return Err(AuthFailure::Unauthorized);
    }
    Ok(BootstrapResult {
        issued: IssuedSession {
            session_token,
            csrf_token,
            authority: OperatorSessionAuthority { account, session },
        },
        recovery_codes,
    })
}

pub fn login(
    store: &StationStore,
    runtime: &AuthRuntime,
    username: &str,
    password: &str,
    now_unix_ms: i64,
) -> Result<IssuedSession, AuthFailure> {
    if !store
        .has_operator_accounts()
        .map_err(AuthFailure::Storage)?
    {
        return Err(AuthFailure::BootstrapRequired);
    }
    let password =
        OperatorPassword::new(password).map_err(|_| AuthFailure::Invalid("password is invalid"))?;
    let parsed_username = OperatorUsername::try_from(username).ok();
    let authority = parsed_username
        .as_ref()
        .map(|username| store.read_operator_login(username))
        .transpose()
        .map_err(AuthFailure::Storage)?
        .flatten();
    let _permit = runtime.try_password_permit().ok_or(AuthFailure::Busy)?;
    let matched = verify_password_or_dummy(
        &password,
        authority
            .as_ref()
            .map(|authority| authority.account.password_phc()),
    )
    .map_err(|error| AuthFailure::Storage(error.to_string()))?;
    if let Some(authority) = &authority
        && authority
            .locked_until_unix_ms
            .is_some_and(|until| now_unix_ms < until)
    {
        let retry = authority
            .locked_until_unix_ms
            .unwrap_or(now_unix_ms)
            .saturating_sub(now_unix_ms);
        return Err(AuthFailure::Throttled(
            u64::try_from((retry + 999) / 1_000).unwrap_or(MAX_LOGIN_DELAY_SECONDS),
        ));
    }
    let Some(authority) = authority
        .as_ref()
        .filter(|authority| matched && authority.account.enabled())
    else {
        if let Some(authority) = authority.as_ref() {
            let delay_seconds = login_delay_seconds(authority.failed_attempts.saturating_add(1));
            let locked_until = now_unix_ms.saturating_add(
                i64::try_from(delay_seconds.saturating_mul(1_000)).unwrap_or(i64::MAX),
            );
            store
                .record_operator_login_failure(
                    authority.account.account_id(),
                    now_unix_ms,
                    Some(locked_until),
                )
                .map_err(AuthFailure::Storage)?;
        }
        return Err(AuthFailure::Unauthorized);
    };
    store
        .clear_operator_login_failures(authority.account.account_id(), now_unix_ms)
        .map_err(AuthFailure::Storage)?;
    issue_session(store, runtime, &authority.account, now_unix_ms)
}

pub fn recover(
    store: &StationStore,
    runtime: &AuthRuntime,
    username: &str,
    recovery_code: &str,
    new_password: &str,
    now_unix_ms: i64,
) -> Result<(), AuthFailure> {
    let username = OperatorUsername::try_from(username).ok();
    let code = RecoveryCode::try_from(recovery_code).ok();
    let password = OperatorPassword::new(new_password)
        .map_err(|_| AuthFailure::Invalid("new_password is invalid"))?;
    let account = username
        .as_ref()
        .map(|username| store.read_operator_login(username))
        .transpose()
        .map_err(AuthFailure::Storage)?
        .flatten();
    let _permit = runtime.try_password_permit().ok_or(AuthFailure::Busy)?;
    let password_phc =
        hash_password(&password).map_err(|error| AuthFailure::Storage(error.to_string()))?;
    let (Some(account), Some(code)) = (account, code) else {
        return Err(AuthFailure::Unauthorized);
    };
    if store
        .consume_operator_recovery(
            account.account.account_id(),
            &digest_recovery_code(&code),
            &password_phc,
            now_unix_ms,
        )
        .map_err(AuthFailure::Storage)?
        .is_none()
    {
        return Err(AuthFailure::Unauthorized);
    }
    Ok(())
}

pub fn verify_current_password(
    runtime: &AuthRuntime,
    account: &OperatorAccount,
    password: &str,
) -> Result<(), AuthFailure> {
    let password = OperatorPassword::new(password).map_err(|_| AuthFailure::Unauthorized)?;
    let _permit = runtime.try_password_permit().ok_or(AuthFailure::Busy)?;
    if !verify_password(&password, account.password_phc())
        .map_err(|error| AuthFailure::Storage(error.to_string()))?
    {
        return Err(AuthFailure::Unauthorized);
    }
    Ok(())
}

pub fn hash_new_password(
    runtime: &AuthRuntime,
    password: &str,
) -> Result<PasswordPhc, AuthFailure> {
    let password = OperatorPassword::new(password)
        .map_err(|_| AuthFailure::Invalid("new_password is invalid"))?;
    let _permit = runtime.try_password_permit().ok_or(AuthFailure::Busy)?;
    hash_password(&password).map_err(|error| AuthFailure::Storage(error.to_string()))
}

fn issue_session(
    store: &StationStore,
    runtime: &AuthRuntime,
    account: &OperatorAccount,
    now_unix_ms: i64,
) -> Result<IssuedSession, AuthFailure> {
    let (session_token, csrf_token, session) = new_session(account, runtime, now_unix_ms)?;
    store
        .create_operator_session(&session, now_unix_ms)
        .map_err(AuthFailure::Storage)?;
    Ok(IssuedSession {
        session_token,
        csrf_token,
        authority: OperatorSessionAuthority {
            account: account.clone(),
            session,
        },
    })
}

fn new_session(
    account: &OperatorAccount,
    runtime: &AuthRuntime,
    now_unix_ms: i64,
) -> Result<(SecretToken, SecretToken, OperatorSession), AuthFailure> {
    let session_token =
        generate_secret_token().map_err(|error| AuthFailure::Storage(error.to_string()))?;
    let csrf_token =
        generate_secret_token().map_err(|error| AuthFailure::Storage(error.to_string()))?;
    let absolute = now_unix_ms
        .checked_add(SESSION_ABSOLUTE_MS)
        .ok_or(AuthFailure::Invalid("session timestamp is invalid"))?;
    let idle = now_unix_ms
        .checked_add(SESSION_IDLE_MS)
        .ok_or(AuthFailure::Invalid("session timestamp is invalid"))?;
    let session = OperatorSession::new(
        digest_secret(&session_token),
        digest_secret(&csrf_token),
        account.account_id().clone(),
        account.credential_version(),
        runtime.boot_id(),
        now_unix_ms,
        now_unix_ms,
        idle,
        absolute,
    )
    .map_err(|error| AuthFailure::Storage(error.to_string()))?;
    Ok((session_token, csrf_token, session))
}

fn generate_account_id() -> Result<AccountId, AuthFailure> {
    let mut bytes = [0_u8; 16];
    getrandom::fill(&mut bytes).map_err(|error| AuthFailure::Storage(error.to_string()))?;
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    let value = format!(
        "{:02x}{:02x}{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}",
        bytes[0],
        bytes[1],
        bytes[2],
        bytes[3],
        bytes[4],
        bytes[5],
        bytes[6],
        bytes[7],
        bytes[8],
        bytes[9],
        bytes[10],
        bytes[11],
        bytes[12],
        bytes[13],
        bytes[14],
        bytes[15]
    );
    AccountId::try_from(value).map_err(|error| AuthFailure::Storage(error.to_string()))
}

fn login_delay_seconds(failed_attempts: u32) -> u64 {
    let shift = failed_attempts.saturating_sub(1).min(8);
    (1_u64 << shift).min(MAX_LOGIN_DELAY_SECONDS)
}

pub struct PasswordPermit<'a> {
    runtime: &'a AuthRuntime,
}

impl Drop for PasswordPermit<'_> {
    fn drop(&mut self) {
        if let Ok(mut active) = self.runtime.password_verifiers.lock() {
            *active = active.saturating_sub(1);
        }
    }
}

#[derive(Debug)]
pub struct AuthCookies {
    pub session: Option<SecretToken>,
    pub csrf: Option<SecretToken>,
}

impl AuthCookies {
    pub fn parse(header: Option<&str>) -> Result<Self, &'static str> {
        let Some(header) = header else {
            return Ok(Self {
                session: None,
                csrf: None,
            });
        };
        if header.len() > MAX_COOKIE_HEADER_BYTES || header.chars().any(char::is_control) {
            return Err("the Cookie header is invalid or too large");
        }
        let mut session = None;
        let mut csrf = None;
        for part in header.split(';') {
            let Some((name, value)) = part.trim().split_once('=') else {
                return Err("the Cookie header contains an invalid cookie");
            };
            let name = name.trim();
            let value = value.trim();
            let target = match name {
                SESSION_COOKIE_NAME => &mut session,
                CSRF_COOKIE_NAME => &mut csrf,
                _ => continue,
            };
            if target.is_some() {
                return Err("an authentication cookie may appear only once");
            }
            *target = Some(
                SecretToken::try_from(value)
                    .map_err(|_| "an authentication cookie has an invalid token")?,
            );
        }
        Ok(Self { session, csrf })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn runtime_binds_one_boot_to_one_exact_loopback_origin() {
        let first = AuthRuntime::new("127.0.0.1:4070".parse().unwrap()).unwrap();
        let second = AuthRuntime::new("127.0.0.1:4070".parse().unwrap()).unwrap();
        assert_eq!(first.expected_host(), "127.0.0.1:4070");
        assert_eq!(first.expected_origin(), "http://127.0.0.1:4070");
        assert_ne!(first.boot_id(), second.boot_id());
    }

    #[test]
    fn cookie_parser_accepts_only_one_canonical_session_and_csrf_secret() {
        let session = station_auth::generate_secret_token().unwrap();
        let csrf = station_auth::generate_secret_token().unwrap();
        let header = format!(
            "theme=dark; townlight_session_v1={}; townlight_csrf_v1={}",
            session.expose_secret(),
            csrf.expose_secret()
        );
        let parsed = AuthCookies::parse(Some(&header)).unwrap();
        assert_eq!(
            station_auth::digest_secret(parsed.session.as_ref().unwrap()),
            station_auth::digest_secret(&session)
        );
        assert_eq!(
            station_auth::digest_secret(parsed.csrf.as_ref().unwrap()),
            station_auth::digest_secret(&csrf)
        );

        for invalid in [
            format!(
                "townlight_session_v1={}; townlight_session_v1={}",
                session.expose_secret(),
                session.expose_secret()
            ),
            "townlight_session_v1=short".into(),
            "townlight_csrf_v1=bad value".into(),
        ] {
            assert!(
                AuthCookies::parse(Some(&invalid)).is_err(),
                "accepted {invalid}"
            );
        }
    }

    #[test]
    fn expensive_password_verification_is_bounded_to_two_concurrent_permits() {
        let runtime = AuthRuntime::new("127.0.0.1:4070".parse().unwrap()).unwrap();
        let first = runtime.try_password_permit().unwrap();
        let second = runtime.try_password_permit().unwrap();
        assert!(runtime.try_password_permit().is_none());
        drop(first);
        assert!(runtime.try_password_permit().is_some());
        drop(second);
    }

    #[test]
    fn recovery_unknown_and_known_invalid_identity_share_one_observable_failure() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let database = std::env::temp_dir().join(format!("townlight-recovery-oracle-{nonce}.db"));
        let store = StationStore::open(&database).unwrap();
        let password = OperatorPassword::new("A-strong-current-password-47!").unwrap();
        let account = OperatorAccount::new(
            AccountId::try_from("11111111-1111-4111-8111-111111111111").unwrap(),
            OperatorUsername::try_from("known.operator").unwrap(),
            OperatorDisplayName::try_from("Known Operator").unwrap(),
            hash_password(&password).unwrap(),
            BTreeSet::from([OperatorRole::SetupAdmin]),
            CredentialVersion::new(1).unwrap(),
            true,
            10,
            10,
        )
        .unwrap();
        store.insert_operator_account(&account).unwrap();
        let runtime = AuthRuntime::new("127.0.0.1:4070".parse().unwrap()).unwrap();
        let code = "AAAAA-BBBBB-CCCCC-DDDDD";
        let replacement = "A-strong-replacement-password-58!";

        assert!(matches!(
            recover(&store, &runtime, "unknown.operator", code, replacement, 20),
            Err(AuthFailure::Unauthorized)
        ));
        assert!(matches!(
            recover(&store, &runtime, "known.operator", code, replacement, 21),
            Err(AuthFailure::Unauthorized)
        ));
        assert!(matches!(
            recover(&store, &runtime, " invalid ", "bad", replacement, 22),
            Err(AuthFailure::Unauthorized)
        ));

        drop(store);
        for candidate in [
            database.clone(),
            PathBuf::from(format!("{}-wal", database.display())),
            PathBuf::from(format!("{}-shm", database.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }
}
