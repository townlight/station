//! Process-local authentication for scheduling source projections.
//!
//! The authority owns a random HMAC key, issuer identity, and monotonic clock
//! origin for one process lifetime. It signs exact scheduling evidence scopes
//! and canonical payload digests. The key is deliberately not persisted:
//! receipts issued by a prior process become unverifiable after restart and
//! must be regenerated from a fresh authoritative snapshot.

use hmac::{Hmac, Mac};
use sha2::Sha256;
use station_schedule_automation::{
    MAX_EVIDENCE_RECEIPT_LIFETIME_MS, SourceEvidenceKind, SourceEvidenceReceipt,
    SourceEvidenceVerifier,
};
use std::fmt::{Display, Formatter};
use std::time::{Instant, SystemTime, UNIX_EPOCH};
use zeroize::Zeroize;

const KEY_BYTES: usize = 32;

type HmacSha256 = Hmac<Sha256>;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SourceEvidenceAuthorityError {
    EntropyUnavailable,
    ClockBeforeUnixEpoch,
    ClockOverflow,
    InvalidScopeSha256,
    InvalidProjectionSha256,
    InvalidReceiptLifetime { requested_ms: i64, maximum_ms: i64 },
    HmacInitialization,
}

impl Display for SourceEvidenceAuthorityError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::EntropyUnavailable => {
                formatter.write_str("secure evidence entropy is unavailable")
            }
            Self::ClockBeforeUnixEpoch => {
                formatter.write_str("source evidence clock is before the Unix epoch")
            }
            Self::ClockOverflow => formatter.write_str("source evidence UTC time overflowed"),
            Self::InvalidScopeSha256 => {
                formatter.write_str("evidence scope is not a lowercase SHA-256")
            }
            Self::InvalidProjectionSha256 => {
                formatter.write_str("canonical projection is not a lowercase SHA-256")
            }
            Self::InvalidReceiptLifetime {
                requested_ms,
                maximum_ms,
            } => write!(
                formatter,
                "source evidence lifetime {requested_ms}ms is outside 1..={maximum_ms}ms"
            ),
            Self::HmacInitialization => {
                formatter.write_str("source evidence HMAC initialization failed")
            }
        }
    }
}

impl std::error::Error for SourceEvidenceAuthorityError {}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EvidenceScopeSha256(String);

impl EvidenceScopeSha256 {
    pub fn new(value: impl Into<String>) -> Result<Self, SourceEvidenceAuthorityError> {
        let value = value.into();
        if !lowercase_sha256(&value) {
            return Err(SourceEvidenceAuthorityError::InvalidScopeSha256);
        }
        Ok(Self(value))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl TryFrom<&str> for EvidenceScopeSha256 {
    type Error = SourceEvidenceAuthorityError;

    fn try_from(value: &str) -> Result<Self, Self::Error> {
        Self::new(value)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CanonicalProjectionSha256(String);

impl CanonicalProjectionSha256 {
    pub fn new(value: impl Into<String>) -> Result<Self, SourceEvidenceAuthorityError> {
        let value = value.into();
        if !lowercase_sha256(&value) {
            return Err(SourceEvidenceAuthorityError::InvalidProjectionSha256);
        }
        Ok(Self(value))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl TryFrom<&str> for CanonicalProjectionSha256 {
    type Error = SourceEvidenceAuthorityError;

    fn try_from(value: &str) -> Result<Self, Self::Error> {
        Self::new(value)
    }
}

enum BootClock {
    System {
        origin_utc_ms: i64,
        origin_monotonic: Instant,
    },
    #[cfg(test)]
    Deterministic(std::sync::atomic::AtomicI64),
}

impl BootClock {
    fn system() -> Result<Self, SourceEvidenceAuthorityError> {
        let origin_monotonic = Instant::now();
        let elapsed = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| SourceEvidenceAuthorityError::ClockBeforeUnixEpoch)?;
        let origin_utc_ms = i64::try_from(elapsed.as_millis())
            .map_err(|_| SourceEvidenceAuthorityError::ClockOverflow)?;
        Ok(Self::System {
            origin_utc_ms,
            origin_monotonic,
        })
    }

    fn now_utc_ms(&self) -> Result<i64, SourceEvidenceAuthorityError> {
        match self {
            Self::System {
                origin_utc_ms,
                origin_monotonic,
            } => {
                let elapsed_ms = i64::try_from(origin_monotonic.elapsed().as_millis())
                    .map_err(|_| SourceEvidenceAuthorityError::ClockOverflow)?;
                origin_utc_ms
                    .checked_add(elapsed_ms)
                    .ok_or(SourceEvidenceAuthorityError::ClockOverflow)
            }
            #[cfg(test)]
            Self::Deterministic(now) => Ok(now.load(std::sync::atomic::Ordering::SeqCst)),
        }
    }

    #[cfg(test)]
    fn set_now_utc_ms(&self, now_utc_ms: i64) {
        let Self::Deterministic(now) = self else {
            panic!("only a deterministic evidence clock can be changed")
        };
        now.store(now_utc_ms, std::sync::atomic::Ordering::SeqCst);
    }
}

/// Process-scoped server-only authority for exact candidate and prior-airing
/// projection receipts. Production must construct exactly one instance at
/// stationd startup and share it for that process lifetime.
pub struct StationSourceEvidenceAuthority {
    issuer_id: String,
    key: [u8; KEY_BYTES],
    clock: BootClock,
}

impl Drop for StationSourceEvidenceAuthority {
    fn drop(&mut self) {
        self.key.zeroize();
    }
}

impl StationSourceEvidenceAuthority {
    pub fn new() -> Result<Self, SourceEvidenceAuthorityError> {
        let mut key = [0_u8; KEY_BYTES];
        getrandom::fill(&mut key).map_err(|_| SourceEvidenceAuthorityError::EntropyUnavailable)?;
        let issuer_id = match random_uuid() {
            Ok(issuer_id) => issuer_id,
            Err(error) => {
                key.zeroize();
                return Err(error);
            }
        };
        let clock = match BootClock::system() {
            Ok(clock) => clock,
            Err(error) => {
                key.zeroize();
                return Err(error);
            }
        };
        Ok(Self {
            issuer_id,
            key,
            clock,
        })
    }

    pub fn issuer_id(&self) -> &str {
        &self.issuer_id
    }

    /// Returns the authority-owned UTC observation derived from a fixed boot
    /// wall-clock origin plus monotonic elapsed time. Callers pass this value
    /// to the pure S19 layer; verification independently observes it again.
    pub fn observed_at_utc_ms(&self) -> Result<i64, SourceEvidenceAuthorityError> {
        self.clock.now_utc_ms()
    }

    /// Issues a receipt for one typed canonical projection digest. Issue time
    /// comes only from the authority-owned boot-monotonic clock. Lifetime is
    /// positive and no longer than five minutes.
    pub fn issue(
        &self,
        kind: SourceEvidenceKind,
        scope_sha256: &EvidenceScopeSha256,
        payload_sha256: &CanonicalProjectionSha256,
        lifetime_ms: i64,
    ) -> Result<SourceEvidenceReceipt, SourceEvidenceAuthorityError> {
        if !(1..=MAX_EVIDENCE_RECEIPT_LIFETIME_MS).contains(&lifetime_ms) {
            return Err(SourceEvidenceAuthorityError::InvalidReceiptLifetime {
                requested_ms: lifetime_ms,
                maximum_ms: MAX_EVIDENCE_RECEIPT_LIFETIME_MS,
            });
        }
        let issued_at_utc_ms = self.clock.now_utc_ms()?;
        let expires_at_utc_ms = issued_at_utc_ms
            .checked_add(lifetime_ms)
            .ok_or(SourceEvidenceAuthorityError::ClockOverflow)?;
        let mut receipt = SourceEvidenceReceipt {
            issuer_id: self.issuer_id.clone(),
            receipt_id: random_uuid()?,
            issued_at_utc_ms,
            expires_at_utc_ms,
            scope_sha256: scope_sha256.as_str().into(),
            attestation_sha256: String::new(),
        };
        receipt.attestation_sha256 = self.attestation(kind, &receipt, payload_sha256)?;
        Ok(receipt)
    }

    fn attestation(
        &self,
        kind: SourceEvidenceKind,
        receipt: &SourceEvidenceReceipt,
        payload_sha256: &CanonicalProjectionSha256,
    ) -> Result<String, SourceEvidenceAuthorityError> {
        let mut mac = HmacSha256::new_from_slice(&self.key)
            .map_err(|_| SourceEvidenceAuthorityError::HmacInitialization)?;
        encode_attestation(&mut mac, kind, receipt, payload_sha256.as_str());
        Ok(hex(mac.finalize().into_bytes().as_slice()))
    }

    #[cfg(test)]
    fn deterministic(
        key: [u8; KEY_BYTES],
        issuer_id: &str,
        now_utc_ms: i64,
    ) -> Result<Self, SourceEvidenceAuthorityError> {
        if now_utc_ms < 0 {
            return Err(SourceEvidenceAuthorityError::ClockBeforeUnixEpoch);
        }
        Ok(Self {
            issuer_id: issuer_id.into(),
            key,
            clock: BootClock::Deterministic(std::sync::atomic::AtomicI64::new(now_utc_ms)),
        })
    }

    #[cfg(test)]
    fn set_test_now_utc_ms(&self, now_utc_ms: i64) {
        self.clock.set_now_utc_ms(now_utc_ms);
    }
}

impl SourceEvidenceVerifier for StationSourceEvidenceAuthority {
    fn verify(
        &self,
        kind: SourceEvidenceKind,
        receipt: &SourceEvidenceReceipt,
        scope_sha256: &str,
        payload_sha256: &str,
        _caller_observed_at_utc_ms: i64,
    ) -> Result<(), String> {
        // The trait carries an observation for pure/test verifiers. Production
        // authentication deliberately ignores it and observes its own clock.
        let observed_at_utc_ms = self.clock.now_utc_ms().map_err(|error| error.to_string())?;
        let scope_sha256 =
            EvidenceScopeSha256::try_from(scope_sha256).map_err(|error| error.to_string())?;
        let payload_sha256 = CanonicalProjectionSha256::try_from(payload_sha256)
            .map_err(|error| error.to_string())?;
        if receipt.issuer_id != self.issuer_id
            || receipt.scope_sha256 != scope_sha256.as_str()
            || receipt.issued_at_utc_ms < 0
            || receipt.issued_at_utc_ms > observed_at_utc_ms
            || receipt.expires_at_utc_ms <= observed_at_utc_ms
            || receipt.expires_at_utc_ms <= receipt.issued_at_utc_ms
            || receipt.expires_at_utc_ms - receipt.issued_at_utc_ms
                > MAX_EVIDENCE_RECEIPT_LIFETIME_MS
        {
            return Err("source evidence receipt is stale, malformed, or rebound".into());
        }
        let supplied = decode_sha256(&receipt.attestation_sha256)?;
        let mut mac = HmacSha256::new_from_slice(&self.key)
            .map_err(|_| SourceEvidenceAuthorityError::HmacInitialization.to_string())?;
        encode_attestation(&mut mac, kind, receipt, payload_sha256.as_str());
        mac.verify_slice(&supplied)
            .map_err(|_| "source evidence attestation failed authentication".to_string())?;
        Ok(())
    }
}

fn encode_attestation(
    mac: &mut HmacSha256,
    kind: SourceEvidenceKind,
    receipt: &SourceEvidenceReceipt,
    payload_sha256: &str,
) {
    put(mac, "townlight.source-evidence-attestation.v1");
    put(mac, kind_name(kind));
    put(mac, &receipt.issuer_id);
    put(mac, &receipt.receipt_id);
    mac.update(&receipt.issued_at_utc_ms.to_le_bytes());
    mac.update(&receipt.expires_at_utc_ms.to_le_bytes());
    put(mac, &receipt.scope_sha256);
    put(mac, payload_sha256);
}

fn put(mac: &mut HmacSha256, value: &str) {
    mac.update(&(value.len() as u64).to_le_bytes());
    mac.update(value.as_bytes());
}

fn kind_name(kind: SourceEvidenceKind) -> &'static str {
    match kind {
        SourceEvidenceKind::CandidateProjection => "candidate-projection",
        SourceEvidenceKind::PriorAirings => "prior-airings",
    }
}

fn random_uuid() -> Result<String, SourceEvidenceAuthorityError> {
    let mut bytes = [0_u8; 16];
    getrandom::fill(&mut bytes).map_err(|_| SourceEvidenceAuthorityError::EntropyUnavailable)?;
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    Ok(format!(
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
    ))
}

fn lowercase_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn decode_sha256(value: &str) -> Result<[u8; 32], String> {
    if !lowercase_sha256(value) {
        return Err("source evidence attestation is not a lowercase SHA-256".into());
    }
    let mut bytes = [0_u8; 32];
    for (index, output) in bytes.iter_mut().enumerate() {
        *output = u8::from_str_radix(&value[index * 2..index * 2 + 2], 16)
            .map_err(|_| "source evidence attestation is not hexadecimal".to_string())?;
    }
    Ok(bytes)
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    const NOW: i64 = 1_700_000_000_000;
    const ISSUER: &str = "00000000-0000-4000-8000-000000000001";

    fn sha(character: char) -> String {
        character.to_string().repeat(64)
    }

    fn scope_digest(character: char) -> EvidenceScopeSha256 {
        EvidenceScopeSha256::new(sha(character)).unwrap()
    }

    fn payload_digest(character: char) -> CanonicalProjectionSha256 {
        CanonicalProjectionSha256::new(sha(character)).unwrap()
    }

    fn deterministic_authority(key_byte: u8) -> StationSourceEvidenceAuthority {
        StationSourceEvidenceAuthority::deterministic([key_byte; KEY_BYTES], ISSUER, NOW).unwrap()
    }

    #[test]
    fn exact_receipt_verifies_only_for_its_kind_scope_payload_and_process() {
        let authority = deterministic_authority(1);
        let scope = scope_digest('a');
        let payload = payload_digest('b');
        let receipt = authority
            .issue(
                SourceEvidenceKind::CandidateProjection,
                &scope,
                &payload,
                60_000,
            )
            .unwrap();
        assert_eq!(receipt.issuer_id, authority.issuer_id());
        assert_eq!(receipt.issued_at_utc_ms, NOW);
        assert_eq!(receipt.attestation_sha256.len(), 64);
        authority.set_test_now_utc_ms(NOW + 1);
        assert!(
            authority
                .verify(
                    SourceEvidenceKind::CandidateProjection,
                    &receipt,
                    scope.as_str(),
                    payload.as_str(),
                    i64::MIN,
                )
                .is_ok()
        );
        assert!(
            authority
                .verify(
                    SourceEvidenceKind::PriorAirings,
                    &receipt,
                    scope.as_str(),
                    payload.as_str(),
                    NOW + 1,
                )
                .is_err()
        );
        assert!(
            authority
                .verify(
                    SourceEvidenceKind::CandidateProjection,
                    &receipt,
                    scope_digest('c').as_str(),
                    payload.as_str(),
                    NOW + 1,
                )
                .is_err()
        );
        assert!(
            authority
                .verify(
                    SourceEvidenceKind::CandidateProjection,
                    &receipt,
                    scope.as_str(),
                    payload_digest('d').as_str(),
                    NOW + 1,
                )
                .is_err()
        );
        let other_process = deterministic_authority(2);
        other_process.set_test_now_utc_ms(NOW + 1);
        assert!(
            other_process
                .verify(
                    SourceEvidenceKind::CandidateProjection,
                    &receipt,
                    scope.as_str(),
                    payload.as_str(),
                    NOW + 1,
                )
                .is_err()
        );
    }

    #[test]
    fn tampering_and_authority_time_fail_closed() {
        let authority = deterministic_authority(1);
        let scope = scope_digest('a');
        let payload = payload_digest('b');
        let receipt = authority
            .issue(SourceEvidenceKind::PriorAirings, &scope, &payload, 60_000)
            .unwrap();
        authority.set_test_now_utc_ms(NOW + 1);
        for mutate in [
            |value: &mut SourceEvidenceReceipt| value.receipt_id = random_uuid().unwrap(),
            |value: &mut SourceEvidenceReceipt| value.issued_at_utc_ms += 1,
            |value: &mut SourceEvidenceReceipt| value.expires_at_utc_ms += 1,
            |value: &mut SourceEvidenceReceipt| value.attestation_sha256 = sha('c'),
        ] {
            let mut tampered = receipt.clone();
            mutate(&mut tampered);
            assert!(
                authority
                    .verify(
                        SourceEvidenceKind::PriorAirings,
                        &tampered,
                        scope.as_str(),
                        payload.as_str(),
                        NOW + 1,
                    )
                    .is_err()
            );
        }
        authority.set_test_now_utc_ms(NOW - 1);
        assert!(
            authority
                .verify(
                    SourceEvidenceKind::PriorAirings,
                    &receipt,
                    scope.as_str(),
                    payload.as_str(),
                    NOW + 1,
                )
                .is_err()
        );
        authority.set_test_now_utc_ms(NOW + 60_000);
        assert!(
            authority
                .verify(
                    SourceEvidenceKind::PriorAirings,
                    &receipt,
                    scope.as_str(),
                    payload.as_str(),
                    NOW + 1,
                )
                .is_err()
        );
    }

    #[test]
    fn typed_digests_and_issuance_bounds_fail_closed() {
        assert_eq!(
            EvidenceScopeSha256::new(sha('g')).unwrap_err(),
            SourceEvidenceAuthorityError::InvalidScopeSha256
        );
        assert_eq!(
            CanonicalProjectionSha256::new(sha('g')).unwrap_err(),
            SourceEvidenceAuthorityError::InvalidProjectionSha256
        );
        let authority = deterministic_authority(1);
        for lifetime_ms in [0, -1, MAX_EVIDENCE_RECEIPT_LIFETIME_MS + 1] {
            assert!(
                authority
                    .issue(
                        SourceEvidenceKind::CandidateProjection,
                        &scope_digest('a'),
                        &payload_digest('b'),
                        lifetime_ms,
                    )
                    .is_err()
            );
        }
    }

    #[test]
    fn attestation_encoding_has_a_fixed_known_answer() {
        let authority = deterministic_authority(0x11);
        let receipt = SourceEvidenceReceipt {
            issuer_id: ISSUER.into(),
            receipt_id: "00000000-0000-4000-8000-000000000002".into(),
            issued_at_utc_ms: NOW,
            expires_at_utc_ms: NOW + 60_000,
            scope_sha256: sha('a'),
            attestation_sha256: String::new(),
        };
        assert_eq!(
            authority
                .attestation(
                    SourceEvidenceKind::CandidateProjection,
                    &receipt,
                    &payload_digest('b'),
                )
                .unwrap(),
            "49a01b7f88065f9268caed3048e0a7a383ae739e8be6585808e90303d002a2ed"
        );
    }
}
