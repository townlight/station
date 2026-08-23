use std::net::SocketAddr;
use std::sync::Mutex;

use station_auth::{
    AuthCryptoError, SecretDigest, SecretToken, digest_secret, generate_secret_token,
};

pub const SESSION_COOKIE_NAME: &str = "townlight_session_v1";
pub const CSRF_COOKIE_NAME: &str = "townlight_csrf_v1";
pub const CSRF_HEADER_NAME: &str = "x-townlight-csrf";
const MAX_COOKIE_HEADER_BYTES: usize = 8 * 1024;
const MAX_PASSWORD_VERIFIERS: usize = 2;

pub struct AuthRuntime {
    boot_id: SecretDigest,
    expected_host: String,
    expected_origin: String,
    password_verifiers: Mutex<usize>,
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
}
