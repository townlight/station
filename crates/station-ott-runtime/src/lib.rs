//! Server-owned OTT session and beacon boundary primitives.
//!
//! This crate deliberately contains no HTTP or database code.  It owns the
//! bounded request/response shapes and the deterministic identities used by
//! the storage and API adapters.

use serde::{Deserialize, Serialize};
use station_ott_contract::{
    ClientPlaybackBeacon, PlatformCapabilityDeclaration, PlaybackSessionAuthority,
};

pub const MAX_SESSION_REQUEST_ID_BYTES: usize = 128;
pub const MAX_BEACONS_PER_SESSION: u32 = 10_000;
pub const MAX_TOTAL_SESSIONS: u64 = 10_000;
pub const MAX_TOTAL_BEACONS: u64 = 100_000;
pub const MAX_SESSION_ISSUES_PER_WINDOW: u32 = 5;
pub const MAX_BEACONS_PER_WINDOW: u32 = 48;
pub const ADMISSION_WINDOW_MS: i64 = 60 * 1_000;
pub const MIN_BEACON_INTERVAL_MS: i64 = 1_000;
pub const SESSION_RETENTION_AFTER_EXPIRY_MS: i64 = 24 * 60 * 60 * 1_000;
pub const MAX_RECLAIMED_SESSIONS_PER_REQUEST: u32 = 64;
pub const MAX_RECLAIMED_BEACONS_PER_REQUEST: u32 = 4_096;
pub const MAX_BEACON_POSITION_MS: u64 = station_ott_contract::MAX_VOD_DURATION_MS;
pub const MAX_BEACON_CLOCK_SKEW_MS: i64 = 5 * 60 * 1_000;
pub const SESSION_TTL_MS: i64 = 15 * 60 * 1_000;
pub const MAX_RETRY_AFTER_SECONDS: u64 = (15 * 60) + (24 * 60 * 60);

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlaybackSessionIssueRequest {
    pub request_id: String,
    pub content_id: String,
    pub client: PlatformCapabilityDeclaration,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlaybackSessionIssueResponse {
    pub session: PlaybackSessionAuthority,
    pub session_capability: String,
    pub replayed: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlaybackBeaconReceipt {
    pub beacon_id: String,
    pub session_id: String,
    pub accepted_at_unix_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlaybackBeaconResponse {
    pub receipt: PlaybackBeaconReceipt,
    pub replayed: bool,
}

pub fn validate_issue_request(request: &PlaybackSessionIssueRequest) -> Result<(), String> {
    if request.request_id.is_empty()
        || request.request_id.len() > MAX_SESSION_REQUEST_ID_BYTES
        || request.request_id.trim() != request.request_id
        || request.request_id.chars().any(char::is_control)
    {
        return Err("OTT session request identity is invalid or unbounded".into());
    }
    if request.content_id.len() != 36 {
        return Err("OTT session content identity is invalid".into());
    }
    request.client.validate()
}

pub fn validate_beacon_input(beacon: &ClientPlaybackBeacon) -> Result<(), String> {
    beacon.validate_shape()?;
    if beacon.position_ms > MAX_BEACON_POSITION_MS {
        return Err("OTT playback position is unbounded".into());
    }
    Ok(())
}

pub fn new_uuid() -> Result<String, String> {
    let mut bytes = [0_u8; 16];
    getrandom::fill(&mut bytes).map_err(|error| format!("generate OTT identity: {error}"))?;
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

/// A bearer value returned to the OTT client. Storage persists only its
/// protected copy and SHA-256 digest, never the plaintext in SQLite.
pub fn new_session_capability() -> Result<String, String> {
    let mut bytes = [0_u8; 32];
    getrandom::fill(&mut bytes)
        .map_err(|error| format!("generate OTT session capability: {error}"))?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generated_uuid_is_canonical_v4() {
        let value = new_uuid().unwrap();
        assert_eq!(value.len(), 36);
        assert_eq!(&value[14..15], "4");
        assert!(matches!(&value[19..20], "8" | "9" | "a" | "b"));
    }

    #[test]
    fn admission_rates_cannot_exhaust_retained_row_capacity() {
        let retained_windows = u64::try_from(
            (SESSION_TTL_MS + SESSION_RETENTION_AFTER_EXPIRY_MS + ADMISSION_WINDOW_MS - 1)
                / ADMISSION_WINDOW_MS,
        )
        .unwrap()
        .saturating_add(1);
        let maximum_sessions = retained_windows * u64::from(MAX_SESSION_ISSUES_PER_WINDOW);
        let maximum_beacons = retained_windows * u64::from(MAX_BEACONS_PER_WINDOW);
        assert!(maximum_sessions * 4 <= MAX_TOTAL_SESSIONS * 3);
        assert!(maximum_beacons * 4 <= MAX_TOTAL_BEACONS * 3);
        assert_eq!(
            MAX_RETRY_AFTER_SECONDS,
            u64::try_from((SESSION_TTL_MS + SESSION_RETENTION_AFTER_EXPIRY_MS) / 1_000).unwrap()
        );
    }
}
