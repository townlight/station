//! Pure local-operator authentication primitives.
//!
//! This crate validates bounded identity and credential records and owns all
//! password/token cryptography. It performs no storage, HTTP, Windows, or clock
//! I/O. Secret-bearing values deliberately omit `Clone` and serialization and
//! redact their `Debug` output; callers must explicitly request one-time
//! plaintext through `expose_secret`.

#![forbid(unsafe_code)]

use std::collections::BTreeSet;
use std::fmt;

use argon2::password_hash::{PasswordHash, PasswordHasher, PasswordVerifier, SaltString};
use argon2::{Algorithm, Argon2, Params, Version};
use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use serde::{Deserialize, Serialize};
use sha2::{Digest as _, Sha256};
use subtle::ConstantTimeEq;

const ARGON2_MEMORY_KIB: u32 = 65_536;
const ARGON2_ITERATIONS: u32 = 3;
const ARGON2_LANES: u32 = 1;
const ARGON2_OUTPUT_BYTES: usize = 32;
const ARGON2_SALT_BYTES: usize = 16;
const DUMMY_PASSWORD_PHC: &str = "$argon2id$v=19$m=65536,t=3,p=1$c2FsdHNhbHRzYWx0c2FsdA$QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE";
pub const RECOVERY_CODE_COUNT: usize = 8;
pub const BOOTSTRAP_MAX_LIFETIME_MS: i64 = 15 * 60 * 1_000;
pub const SESSION_IDLE_TIMEOUT_MS: i64 = 30 * 60 * 1_000;
pub const SESSION_ABSOLUTE_TIMEOUT_MS: i64 = 12 * 60 * 60 * 1_000;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuthValidationError(&'static str);

impl fmt::Display for AuthValidationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.0)
    }
}

impl std::error::Error for AuthValidationError {}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuthCryptoError(&'static str);

impl fmt::Display for AuthCryptoError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.0)
    }
}

impl std::error::Error for AuthCryptoError {}

macro_rules! string_value {
    ($name:ident, $validator:ident, $message:literal) => {
        #[derive(Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
        pub struct $name(String);

        impl $name {
            pub fn as_str(&self) -> &str {
                &self.0
            }

            pub fn into_string(self) -> String {
                self.0
            }
        }

        impl TryFrom<&str> for $name {
            type Error = AuthValidationError;

            fn try_from(value: &str) -> Result<Self, Self::Error> {
                if !$validator(value) {
                    return Err(AuthValidationError($message));
                }
                Ok(Self(value.to_owned()))
            }
        }

        impl TryFrom<String> for $name {
            type Error = AuthValidationError;

            fn try_from(value: String) -> Result<Self, Self::Error> {
                if !$validator(&value) {
                    return Err(AuthValidationError($message));
                }
                Ok(Self(value))
            }
        }
    };
}

string_value!(
    AccountId,
    is_canonical_uuid,
    "account id must be a canonical lowercase UUID"
);
string_value!(
    OperatorUsername,
    is_operator_username,
    "operator username must contain 3 to 64 lowercase ASCII letters, digits, dots, underscores, or hyphens"
);
string_value!(
    OperatorDisplayName,
    is_operator_display_name,
    "operator display name must contain 1 to 128 visible characters"
);
string_value!(
    PasswordPhc,
    looks_like_argon2id_phc,
    "password hash must use the supported Argon2id PHC envelope"
);

impl fmt::Debug for AccountId {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.debug_tuple("AccountId").field(&self.0).finish()
    }
}

impl fmt::Debug for OperatorUsername {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_tuple("OperatorUsername")
            .field(&self.0)
            .finish()
    }
}

impl fmt::Debug for OperatorDisplayName {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_tuple("OperatorDisplayName")
            .field(&self.0)
            .finish()
    }
}

impl fmt::Debug for PasswordPhc {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("PasswordPhc([REDACTED])")
    }
}

pub struct OperatorPassword(String);

impl OperatorPassword {
    pub fn new(value: impl Into<String>) -> Result<Self, AuthValidationError> {
        let value = value.into();
        if !(12..=128).contains(&value.len()) || value.chars().any(char::is_control) {
            return Err(AuthValidationError(
                "operator password must contain 12 to 128 bytes without control characters",
            ));
        }
        Ok(Self(value))
    }

    fn as_bytes(&self) -> &[u8] {
        self.0.as_bytes()
    }
}

impl fmt::Debug for OperatorPassword {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("OperatorPassword([REDACTED])")
    }
}

pub fn hash_password(password: &OperatorPassword) -> Result<PasswordPhc, AuthCryptoError> {
    let mut salt_bytes = [0_u8; ARGON2_SALT_BYTES];
    getrandom::fill(&mut salt_bytes)
        .map_err(|_| AuthCryptoError("secure password salt generation failed"))?;
    let salt = SaltString::encode_b64(&salt_bytes)
        .map_err(|_| AuthCryptoError("password salt encoding failed"))?;
    let encoded = argon2_contract()?
        .hash_password(password.as_bytes(), &salt)
        .map_err(|_| AuthCryptoError("Argon2id password hashing failed"))?
        .to_string();
    PasswordPhc::try_from(encoded).map_err(|_| AuthCryptoError("Argon2id output was invalid"))
}

pub fn verify_password(
    password: &OperatorPassword,
    password_phc: &PasswordPhc,
) -> Result<bool, AuthCryptoError> {
    let parsed = PasswordHash::new(password_phc.as_str())
        .map_err(|_| AuthCryptoError("stored password hash was invalid"))?;
    Ok(argon2_contract()?
        .verify_password(password.as_bytes(), &parsed)
        .is_ok())
}

pub fn verify_password_or_dummy(
    password: &OperatorPassword,
    password_phc: Option<&PasswordPhc>,
) -> Result<bool, AuthCryptoError> {
    let dummy;
    let (target, account_exists) = if let Some(stored) = password_phc {
        (stored, true)
    } else {
        dummy = PasswordPhc::try_from(DUMMY_PASSWORD_PHC)
            .map_err(|_| AuthCryptoError("dummy password hash was invalid"))?;
        (&dummy, false)
    };
    let matched = verify_password(password, target)?;
    Ok(account_exists && matched)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OperatorRole {
    SetupAdmin,
    MeetingOperator,
    RecordsClerk,
    PublishOperator,
    SupportAdmin,
}

impl OperatorRole {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::SetupAdmin => "setup_admin",
            Self::MeetingOperator => "meeting_operator",
            Self::RecordsClerk => "records_clerk",
            Self::PublishOperator => "publish_operator",
            Self::SupportAdmin => "support_admin",
        }
    }
}

impl fmt::Display for OperatorRole {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

impl TryFrom<&str> for OperatorRole {
    type Error = AuthValidationError;

    fn try_from(value: &str) -> Result<Self, Self::Error> {
        match value {
            "setup_admin" => Ok(Self::SetupAdmin),
            "meeting_operator" => Ok(Self::MeetingOperator),
            "records_clerk" => Ok(Self::RecordsClerk),
            "publish_operator" => Ok(Self::PublishOperator),
            "support_admin" => Ok(Self::SupportAdmin),
            _ => Err(AuthValidationError("operator role is invalid")),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CredentialVersion(u64);

impl CredentialVersion {
    pub fn new(value: u64) -> Result<Self, AuthValidationError> {
        if value == 0 {
            return Err(AuthValidationError(
                "credential version must be greater than zero",
            ));
        }
        Ok(Self(value))
    }

    pub const fn get(self) -> u64 {
        self.0
    }

    pub fn next(self) -> Result<Self, AuthValidationError> {
        self.0
            .checked_add(1)
            .map(Self)
            .ok_or(AuthValidationError("credential version cannot overflow"))
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OperatorAccount {
    account_id: AccountId,
    username: OperatorUsername,
    display_name: OperatorDisplayName,
    password_phc: PasswordPhc,
    roles: BTreeSet<OperatorRole>,
    credential_version: CredentialVersion,
    enabled: bool,
    created_at_unix_ms: i64,
    updated_at_unix_ms: i64,
}

impl OperatorAccount {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        account_id: AccountId,
        username: OperatorUsername,
        display_name: OperatorDisplayName,
        password_phc: PasswordPhc,
        roles: BTreeSet<OperatorRole>,
        credential_version: CredentialVersion,
        enabled: bool,
        created_at_unix_ms: i64,
        updated_at_unix_ms: i64,
    ) -> Result<Self, AuthValidationError> {
        if roles.is_empty() {
            return Err(AuthValidationError(
                "operator account must have at least one role",
            ));
        }
        if created_at_unix_ms < 0 || updated_at_unix_ms < created_at_unix_ms {
            return Err(AuthValidationError(
                "operator account timestamps must be nonnegative and monotonic",
            ));
        }
        Ok(Self {
            account_id,
            username,
            display_name,
            password_phc,
            roles,
            credential_version,
            enabled,
            created_at_unix_ms,
            updated_at_unix_ms,
        })
    }

    pub fn account_id(&self) -> &AccountId {
        &self.account_id
    }
    pub fn username(&self) -> &OperatorUsername {
        &self.username
    }
    pub fn display_name(&self) -> &OperatorDisplayName {
        &self.display_name
    }
    pub fn password_phc(&self) -> &PasswordPhc {
        &self.password_phc
    }
    pub fn roles(&self) -> &BTreeSet<OperatorRole> {
        &self.roles
    }
    pub const fn credential_version(&self) -> CredentialVersion {
        self.credential_version
    }
    pub const fn enabled(&self) -> bool {
        self.enabled
    }
    pub const fn created_at_unix_ms(&self) -> i64 {
        self.created_at_unix_ms
    }
    pub const fn updated_at_unix_ms(&self) -> i64 {
        self.updated_at_unix_ms
    }
}

pub struct SecretToken(String);

impl SecretToken {
    pub fn expose_secret(&self) -> &str {
        &self.0
    }
}

impl fmt::Debug for SecretToken {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("SecretToken([REDACTED])")
    }
}

impl TryFrom<&str> for SecretToken {
    type Error = AuthValidationError;

    fn try_from(value: &str) -> Result<Self, Self::Error> {
        if value.len() != 43 {
            return Err(AuthValidationError(
                "secret token must encode exactly 256 bits",
            ));
        }
        let decoded = URL_SAFE_NO_PAD
            .decode(value)
            .map_err(|_| AuthValidationError("secret token must be canonical base64url"))?;
        if decoded.len() != 32 || URL_SAFE_NO_PAD.encode(&decoded) != value {
            return Err(AuthValidationError(
                "secret token must encode exactly 256 bits",
            ));
        }
        Ok(Self(value.to_owned()))
    }
}

impl TryFrom<String> for SecretToken {
    type Error = AuthValidationError;

    fn try_from(value: String) -> Result<Self, Self::Error> {
        Self::try_from(value.as_str()).map(|_| Self(value))
    }
}

#[derive(Clone, Copy)]
pub struct SecretDigest([u8; 32]);

impl SecretDigest {
    pub const fn from_bytes(bytes: [u8; 32]) -> Self {
        Self(bytes)
    }

    pub const fn as_bytes(&self) -> &[u8; 32] {
        &self.0
    }

    pub const fn into_bytes(self) -> [u8; 32] {
        self.0
    }
}

impl PartialEq for SecretDigest {
    fn eq(&self, other: &Self) -> bool {
        bool::from(self.0.ct_eq(&other.0))
    }
}

impl Eq for SecretDigest {}

impl fmt::Debug for SecretDigest {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("SecretDigest([REDACTED])")
    }
}

pub fn generate_secret_token() -> Result<SecretToken, AuthCryptoError> {
    let mut bytes = [0_u8; 32];
    getrandom::fill(&mut bytes).map_err(|_| AuthCryptoError("secure token generation failed"))?;
    SecretToken::try_from(URL_SAFE_NO_PAD.encode(bytes))
        .map_err(|_| AuthCryptoError("generated secret token was invalid"))
}

pub fn digest_secret(token: &SecretToken) -> SecretDigest {
    let digest: [u8; 32] = Sha256::digest(token.expose_secret().as_bytes()).into();
    SecretDigest::from_bytes(digest)
}

pub struct RecoveryCode(String);

impl RecoveryCode {
    pub fn expose_secret(&self) -> &str {
        &self.0
    }
}

impl fmt::Debug for RecoveryCode {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("RecoveryCode([REDACTED])")
    }
}

impl TryFrom<&str> for RecoveryCode {
    type Error = AuthValidationError;

    fn try_from(value: &str) -> Result<Self, Self::Error> {
        let mut parts = value.split('-');
        if parts.next() != Some("TL")
            || !(0..4).all(|_| {
                parts.next().is_some_and(|part| {
                    part.len() == 5
                        && part
                            .bytes()
                            .all(|byte| byte.is_ascii_uppercase() || (b'2'..=b'7').contains(&byte))
                })
            })
            || parts.next().is_some()
        {
            return Err(AuthValidationError(
                "recovery code must use the canonical grouped Base32 form",
            ));
        }
        Ok(Self(value.to_owned()))
    }
}

pub struct GeneratedRecoveryCodes {
    pub display_codes: [RecoveryCode; RECOVERY_CODE_COUNT],
    pub storage_digests: [SecretDigest; RECOVERY_CODE_COUNT],
}

impl fmt::Debug for GeneratedRecoveryCodes {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("GeneratedRecoveryCodes")
            .field("count", &RECOVERY_CODE_COUNT)
            .finish_non_exhaustive()
    }
}

pub fn generate_recovery_codes() -> Result<GeneratedRecoveryCodes, AuthCryptoError> {
    let mut unique = BTreeSet::new();
    let mut generated = Vec::with_capacity(RECOVERY_CODE_COUNT);
    for _ in 0..64 {
        if generated.len() == RECOVERY_CODE_COUNT {
            break;
        }
        let mut random = [0_u8; 13];
        getrandom::fill(&mut random)
            .map_err(|_| AuthCryptoError("secure recovery-code generation failed"))?;
        let encoded = encode_base32_100_bits(random);
        let value = format!(
            "TL-{}-{}-{}-{}",
            &encoded[0..5],
            &encoded[5..10],
            &encoded[10..15],
            &encoded[15..20]
        );
        if unique.insert(value.clone()) {
            generated.push(
                RecoveryCode::try_from(value.as_str())
                    .map_err(|_| AuthCryptoError("generated recovery code was invalid"))?,
            );
        }
    }
    let codes: [RecoveryCode; RECOVERY_CODE_COUNT] = generated
        .try_into()
        .map_err(|_| AuthCryptoError("unique recovery-code generation failed"))?;
    let storage_digests = std::array::from_fn(|index| digest_recovery_code(&codes[index]));
    Ok(GeneratedRecoveryCodes {
        display_codes: codes,
        storage_digests,
    })
}

pub fn digest_recovery_code(code: &RecoveryCode) -> SecretDigest {
    let digest: [u8; 32] = Sha256::digest(code.expose_secret().as_bytes()).into();
    SecretDigest::from_bytes(digest)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BootstrapInvalidReason {
    NotYetValid,
    Expired,
    Consumed,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BootstrapCredential {
    token_digest: SecretDigest,
    issued_at_unix_ms: i64,
    expires_at_unix_ms: i64,
    consumed_at_unix_ms: Option<i64>,
}

impl BootstrapCredential {
    pub fn new(
        token_digest: SecretDigest,
        issued_at_unix_ms: i64,
        expires_at_unix_ms: i64,
        consumed_at_unix_ms: Option<i64>,
    ) -> Result<Self, AuthValidationError> {
        if issued_at_unix_ms < 0
            || expires_at_unix_ms <= issued_at_unix_ms
            || expires_at_unix_ms - issued_at_unix_ms > BOOTSTRAP_MAX_LIFETIME_MS
            || consumed_at_unix_ms.is_some_and(|consumed| {
                consumed < issued_at_unix_ms || consumed >= expires_at_unix_ms
            })
        {
            return Err(AuthValidationError(
                "bootstrap credential timestamps must describe a bounded valid lifetime",
            ));
        }
        Ok(Self {
            token_digest,
            issued_at_unix_ms,
            expires_at_unix_ms,
            consumed_at_unix_ms,
        })
    }

    pub const fn token_digest(&self) -> SecretDigest {
        self.token_digest
    }
    pub const fn issued_at_unix_ms(&self) -> i64 {
        self.issued_at_unix_ms
    }
    pub const fn expires_at_unix_ms(&self) -> i64 {
        self.expires_at_unix_ms
    }
    pub const fn consumed_at_unix_ms(&self) -> Option<i64> {
        self.consumed_at_unix_ms
    }

    pub fn validate_at(&self, now_unix_ms: i64) -> Result<(), BootstrapInvalidReason> {
        if self.consumed_at_unix_ms.is_some() {
            Err(BootstrapInvalidReason::Consumed)
        } else if now_unix_ms < self.issued_at_unix_ms {
            Err(BootstrapInvalidReason::NotYetValid)
        } else if now_unix_ms >= self.expires_at_unix_ms {
            Err(BootstrapInvalidReason::Expired)
        } else {
            Ok(())
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecoveryCredential {
    code_digest: SecretDigest,
    account_id: AccountId,
    created_at_unix_ms: i64,
    consumed_at_unix_ms: Option<i64>,
}

impl RecoveryCredential {
    pub fn new(
        code_digest: SecretDigest,
        account_id: AccountId,
        created_at_unix_ms: i64,
        consumed_at_unix_ms: Option<i64>,
    ) -> Result<Self, AuthValidationError> {
        if created_at_unix_ms < 0
            || consumed_at_unix_ms.is_some_and(|consumed| consumed < created_at_unix_ms)
        {
            return Err(AuthValidationError(
                "recovery credential timestamps must be nonnegative and monotonic",
            ));
        }
        Ok(Self {
            code_digest,
            account_id,
            created_at_unix_ms,
            consumed_at_unix_ms,
        })
    }

    pub const fn code_digest(&self) -> SecretDigest {
        self.code_digest
    }
    pub fn account_id(&self) -> &AccountId {
        &self.account_id
    }
    pub const fn created_at_unix_ms(&self) -> i64 {
        self.created_at_unix_ms
    }
    pub const fn consumed_at_unix_ms(&self) -> Option<i64> {
        self.consumed_at_unix_ms
    }
    pub const fn is_available(&self) -> bool {
        self.consumed_at_unix_ms.is_none()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionInvalidReason {
    AccountDisabled,
    CredentialChanged,
    DifferentServiceBoot,
    NotYetValid,
    IdleExpired,
    AbsoluteExpired,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OperatorSession {
    session_digest: SecretDigest,
    csrf_digest: SecretDigest,
    account_id: AccountId,
    credential_version: CredentialVersion,
    boot_id: SecretDigest,
    created_at_unix_ms: i64,
    last_seen_at_unix_ms: i64,
    idle_expires_at_unix_ms: i64,
    absolute_expires_at_unix_ms: i64,
}

impl OperatorSession {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        session_digest: SecretDigest,
        csrf_digest: SecretDigest,
        account_id: AccountId,
        credential_version: CredentialVersion,
        boot_id: SecretDigest,
        created_at_unix_ms: i64,
        last_seen_at_unix_ms: i64,
        idle_expires_at_unix_ms: i64,
        absolute_expires_at_unix_ms: i64,
    ) -> Result<Self, AuthValidationError> {
        if session_digest == csrf_digest {
            return Err(AuthValidationError(
                "session and CSRF secrets must be independently generated",
            ));
        }
        if created_at_unix_ms < 0
            || last_seen_at_unix_ms < created_at_unix_ms
            || idle_expires_at_unix_ms <= last_seen_at_unix_ms
            || absolute_expires_at_unix_ms <= created_at_unix_ms
            || idle_expires_at_unix_ms > absolute_expires_at_unix_ms
            || idle_expires_at_unix_ms - last_seen_at_unix_ms > SESSION_IDLE_TIMEOUT_MS
            || absolute_expires_at_unix_ms - created_at_unix_ms > SESSION_ABSOLUTE_TIMEOUT_MS
        {
            return Err(AuthValidationError(
                "session timestamps must describe bounded monotonic idle and absolute lifetimes",
            ));
        }
        Ok(Self {
            session_digest,
            csrf_digest,
            account_id,
            credential_version,
            boot_id,
            created_at_unix_ms,
            last_seen_at_unix_ms,
            idle_expires_at_unix_ms,
            absolute_expires_at_unix_ms,
        })
    }

    pub const fn session_digest(&self) -> SecretDigest {
        self.session_digest
    }
    pub const fn csrf_digest(&self) -> SecretDigest {
        self.csrf_digest
    }
    pub fn account_id(&self) -> &AccountId {
        &self.account_id
    }
    pub const fn credential_version(&self) -> CredentialVersion {
        self.credential_version
    }
    pub const fn boot_id(&self) -> SecretDigest {
        self.boot_id
    }
    pub const fn created_at_unix_ms(&self) -> i64 {
        self.created_at_unix_ms
    }
    pub const fn last_seen_at_unix_ms(&self) -> i64 {
        self.last_seen_at_unix_ms
    }
    pub const fn idle_expires_at_unix_ms(&self) -> i64 {
        self.idle_expires_at_unix_ms
    }
    pub const fn absolute_expires_at_unix_ms(&self) -> i64 {
        self.absolute_expires_at_unix_ms
    }

    pub fn validate_at(
        &self,
        now_unix_ms: i64,
        current_boot_id: &SecretDigest,
        current_credential_version: CredentialVersion,
        account_enabled: bool,
    ) -> Result<(), SessionInvalidReason> {
        if !account_enabled {
            Err(SessionInvalidReason::AccountDisabled)
        } else if self.credential_version != current_credential_version {
            Err(SessionInvalidReason::CredentialChanged)
        } else if self.boot_id != *current_boot_id {
            Err(SessionInvalidReason::DifferentServiceBoot)
        } else if now_unix_ms < self.created_at_unix_ms {
            Err(SessionInvalidReason::NotYetValid)
        } else if now_unix_ms >= self.absolute_expires_at_unix_ms {
            Err(SessionInvalidReason::AbsoluteExpired)
        } else if now_unix_ms >= self.idle_expires_at_unix_ms {
            Err(SessionInvalidReason::IdleExpired)
        } else {
            Ok(())
        }
    }
}

fn encode_base32_100_bits(random: [u8; 13]) -> String {
    const ALPHABET: &[u8; 32] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
    let mut output = String::with_capacity(20);
    let mut buffer = 0_u32;
    let mut buffered_bits = 0_u8;
    let mut input = random.into_iter();
    while output.len() < 20 {
        while buffered_bits < 5 {
            buffer = (buffer << 8) | u32::from(input.next().expect("13 bytes cover 100 bits"));
            buffered_bits += 8;
        }
        buffered_bits -= 5;
        let index = usize::try_from((buffer >> buffered_bits) & 0x1f).expect("base32 index");
        output.push(char::from(ALPHABET[index]));
        buffer &= (1_u32 << buffered_bits).saturating_sub(1);
    }
    output
}

fn is_canonical_uuid(value: &str) -> bool {
    value.len() == 36
        && value.bytes().enumerate().all(|(index, byte)| match index {
            8 | 13 | 18 | 23 => byte == b'-',
            _ => byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte),
        })
}

fn is_operator_username(value: &str) -> bool {
    (3..=64).contains(&value.len())
        && value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'.' | b'_' | b'-')
        })
}

fn is_operator_display_name(value: &str) -> bool {
    value.trim() == value
        && !value.is_empty()
        && value.len() <= 128
        && !value.chars().any(char::is_control)
}

fn looks_like_argon2id_phc(value: &str) -> bool {
    if value.len() > 256 || value.chars().any(char::is_control) {
        return false;
    }
    let Ok(parsed) = PasswordHash::new(value) else {
        return false;
    };
    let mut salt_bytes = [0_u8; 64];
    parsed.algorithm.as_str() == "argon2id"
        && parsed.version == Some(19)
        && parsed.params.iter().count() == 3
        && parsed.params.get_decimal("m") == Some(ARGON2_MEMORY_KIB)
        && parsed.params.get_decimal("t") == Some(ARGON2_ITERATIONS)
        && parsed.params.get_decimal("p") == Some(ARGON2_LANES)
        && parsed.salt.is_some_and(|salt| {
            salt.decode_b64(&mut salt_bytes)
                .is_ok_and(|value| value.len() == ARGON2_SALT_BYTES)
        })
        && parsed
            .hash
            .is_some_and(|output| output.len() == ARGON2_OUTPUT_BYTES)
}

fn argon2_contract() -> Result<Argon2<'static>, AuthCryptoError> {
    let params = Params::new(
        ARGON2_MEMORY_KIB,
        ARGON2_ITERATIONS,
        ARGON2_LANES,
        Some(ARGON2_OUTPUT_BYTES),
    )
    .map_err(|_| AuthCryptoError("Argon2id parameters were invalid"))?;
    Ok(Argon2::new(Algorithm::Argon2id, Version::V0x13, params))
}
