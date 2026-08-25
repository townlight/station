//! Pure, fail-closed underwriting spot and proof-of-airing authority contracts.
//!
//! Scheduling, persistence, as-run ingestion, billing export delivery, API, and
//! operator UI boundaries are deliberately outside this crate.

use std::collections::HashSet;

use serde::{Deserialize, Serialize};

pub const MAX_FLIGHT_CHANNELS: usize = 32;
pub const MAX_PROOF_OF_AIRING_JOINS: usize = 100_000;
pub const MAX_PROOFS_PER_EXPORT: usize = 10_000;
pub const MAX_TEXT_BYTES: usize = 200;
pub const MAX_SPOT_DURATION_MS: u64 = 300_000;
pub const MAX_FLIGHT_WINDOW_MS: i64 = 366 * 86_400_000;
pub const MAX_AIRINGS_PER_FLIGHT: u32 = 100_000;
pub const MAX_AIRINGS_PER_DAY: u16 = 1_440;
pub const MAX_MINIMUM_SEPARATION_MS: u64 = 86_400_000;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UnderwritingSpot {
    pub spot_id: String,
    pub sponsor_id: String,
    pub asset_id: String,
    pub label: String,
    pub duration_ms: u64,
    pub revision: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlacementConstraints {
    /// Bit zero is Monday and bit six is Sunday.
    pub allowed_weekdays: u8,
    /// Inclusive minute of the station-local day, 0 through 1439.
    pub daypart_start_minute: u16,
    /// Exclusive minute of the station-local day, 1 through 1440.
    pub daypart_end_minute: u16,
    pub max_airings: u32,
    pub max_airings_per_day: u16,
    pub minimum_separation_ms: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UnderwritingFlight {
    pub flight_id: String,
    pub sponsor_id: String,
    pub spot_id: String,
    pub spot_revision: u64,
    pub starts_at_unix_ms: i64,
    pub ends_at_unix_ms: i64,
    pub channel_ids: Vec<String>,
    pub placement: PlacementConstraints,
    pub revision: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UnderwritingPlacementIntent {
    pub placement_id: String,
    pub flight_id: String,
    pub flight_revision: u64,
    pub channel_id: String,
    pub scheduled_at_unix_ms: i64,
    pub station_weekday: u8,
    pub station_minute_of_day: u16,
    pub revision: u64,
}

/// A future persistence boundary may only append these exact joins. It must not
/// update/delete a join or substitute a later spot/flight/placement revision.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProofOfAiringJoinIntent {
    pub proof_id: String,
    pub sponsor_id: String,
    pub spot_id: String,
    pub spot_revision: u64,
    pub flight_id: String,
    pub flight_revision: u64,
    pub placement_id: String,
    pub placement_revision: u64,
    pub as_run_event_id: String,
    pub aired_at_unix_ms: i64,
    pub aired_duration_ms: u64,
}

/// A validated request to build one billing export from immutable proof IDs.
/// It is not an invoice, delivery receipt, tax calculation, or payment claim.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BillingExportIntent {
    pub export_id: String,
    pub sponsor_id: String,
    pub period_start_unix_ms: i64,
    pub period_end_unix_ms: i64,
    pub proof_ids: Vec<String>,
    pub revision: u64,
}

impl UnderwritingSpot {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.spot_id, "underwriting spot identity")?;
        validate_uuid(&self.sponsor_id, "underwriting sponsor identity")?;
        if !is_sha256(&self.asset_id)
            || self.duration_ms == 0
            || self.duration_ms > MAX_SPOT_DURATION_MS
            || self.revision == 0
        {
            return Err(
                "underwriting spot asset, duration, or revision is invalid or unbounded".into(),
            );
        }
        validate_visible(&self.label, "underwriting spot label")
    }
}

impl PlacementConstraints {
    pub fn validate(&self) -> Result<(), String> {
        if self.allowed_weekdays == 0
            || self.allowed_weekdays & !0b0111_1111 != 0
            || self.daypart_start_minute >= self.daypart_end_minute
            || self.daypart_end_minute > 1_440
            || self.max_airings == 0
            || self.max_airings > MAX_AIRINGS_PER_FLIGHT
            || self.max_airings_per_day == 0
            || self.max_airings_per_day > MAX_AIRINGS_PER_DAY
            || u32::from(self.max_airings_per_day) > self.max_airings
            || self.minimum_separation_ms > MAX_MINIMUM_SEPARATION_MS
        {
            return Err("underwriting placement constraints are invalid or unbounded".into());
        }
        Ok(())
    }
}

impl UnderwritingFlight {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.flight_id, "underwriting flight identity")?;
        validate_uuid(&self.sponsor_id, "underwriting sponsor identity")?;
        validate_uuid(&self.spot_id, "underwriting flight spot identity")?;
        if self.spot_revision == 0
            || self.revision == 0
            || self.starts_at_unix_ms < 0
            || self.ends_at_unix_ms <= self.starts_at_unix_ms
            || self.ends_at_unix_ms - self.starts_at_unix_ms > MAX_FLIGHT_WINDOW_MS
            || self.channel_ids.is_empty()
            || self.channel_ids.len() > MAX_FLIGHT_CHANNELS
        {
            return Err("underwriting flight identity, revision, window, or channel count is invalid or unbounded".into());
        }
        let mut channels = HashSet::with_capacity(self.channel_ids.len());
        for channel_id in &self.channel_ids {
            validate_uuid(channel_id, "underwriting flight channel identity")?;
            if !channels.insert(channel_id.as_str()) {
                return Err("underwriting flight channel identities must be unique".into());
            }
        }
        self.placement.validate()
    }
}

impl UnderwritingPlacementIntent {
    pub fn validate_shape(&self) -> Result<(), String> {
        validate_uuid(&self.placement_id, "underwriting placement identity")?;
        validate_uuid(&self.flight_id, "underwriting placement flight identity")?;
        validate_uuid(&self.channel_id, "underwriting placement channel identity")?;
        if self.flight_revision == 0
            || self.revision == 0
            || self.scheduled_at_unix_ms < 0
            || self.station_weekday > 6
            || self.station_minute_of_day >= 1_440
        {
            return Err(
                "underwriting placement identity, revision, or station-local time is invalid"
                    .into(),
            );
        }
        Ok(())
    }
}

impl ProofOfAiringJoinIntent {
    pub fn validate_shape(&self) -> Result<(), String> {
        for (value, field) in [
            (&self.proof_id, "proof-of-airing identity"),
            (&self.sponsor_id, "proof-of-airing sponsor identity"),
            (&self.spot_id, "proof-of-airing spot identity"),
            (&self.flight_id, "proof-of-airing flight identity"),
            (&self.placement_id, "proof-of-airing placement identity"),
            (
                &self.as_run_event_id,
                "proof-of-airing as-run event identity",
            ),
        ] {
            validate_uuid(value, field)?;
        }
        if self.spot_revision == 0
            || self.flight_revision == 0
            || self.placement_revision == 0
            || self.aired_at_unix_ms < 0
            || self.aired_duration_ms == 0
            || self.aired_duration_ms > MAX_SPOT_DURATION_MS
        {
            return Err("proof-of-airing join revisions or timing are invalid or unbounded".into());
        }
        Ok(())
    }
}

impl BillingExportIntent {
    pub fn validate_shape(&self) -> Result<(), String> {
        validate_uuid(&self.export_id, "billing export identity")?;
        validate_uuid(&self.sponsor_id, "billing export sponsor identity")?;
        if self.revision != 1
            || self.period_start_unix_ms < 0
            || self.period_end_unix_ms <= self.period_start_unix_ms
            || self.period_end_unix_ms - self.period_start_unix_ms > MAX_FLIGHT_WINDOW_MS
            || self.proof_ids.is_empty()
            || self.proof_ids.len() > MAX_PROOFS_PER_EXPORT
        {
            return Err("billing export identity, immutable revision, period, or proof count is invalid or unbounded".into());
        }
        let mut proofs = HashSet::with_capacity(self.proof_ids.len());
        for proof_id in &self.proof_ids {
            validate_uuid(proof_id, "billing export proof identity")?;
            if !proofs.insert(proof_id.as_str()) {
                return Err("billing export proof identities must be unique".into());
            }
        }
        Ok(())
    }
}

pub fn validate_spot_write(
    current: Option<&UnderwritingSpot>,
    expected_revision: Option<u64>,
    proposed: &UnderwritingSpot,
) -> Result<(), String> {
    proposed.validate()?;
    match current {
        None if expected_revision.is_none() && proposed.revision == 1 => Ok(()),
        None => {
            Err("underwriting spot creation requires revision one and no expected revision".into())
        }
        Some(current) => {
            current.validate()?;
            require_next_revision(
                current.revision,
                expected_revision,
                proposed.revision,
                "underwriting spot",
            )?;
            if proposed.spot_id != current.spot_id
                || proposed.sponsor_id != current.sponsor_id
                || proposed.asset_id != current.asset_id
            {
                return Err(
                    "underwriting spot identity, sponsor, and asset cannot be rebound".into(),
                );
            }
            Ok(())
        }
    }
}

pub fn validate_flight_write(
    current: Option<&UnderwritingFlight>,
    expected_revision: Option<u64>,
    proposed: &UnderwritingFlight,
    spot: &UnderwritingSpot,
) -> Result<(), String> {
    proposed.validate()?;
    spot.validate()?;
    if proposed.sponsor_id != spot.sponsor_id
        || proposed.spot_id != spot.spot_id
        || proposed.spot_revision != spot.revision
    {
        return Err("underwriting flight must bind the exact current sponsor spot revision".into());
    }
    match current {
        None if expected_revision.is_none() && proposed.revision == 1 => Ok(()),
        None => Err(
            "underwriting flight creation requires revision one and no expected revision".into(),
        ),
        Some(current) => {
            current.validate()?;
            require_next_revision(
                current.revision,
                expected_revision,
                proposed.revision,
                "underwriting flight",
            )?;
            if proposed.flight_id != current.flight_id
                || proposed.sponsor_id != current.sponsor_id
                || proposed.spot_id != current.spot_id
            {
                return Err(
                    "underwriting flight identity, sponsor, and spot cannot be rebound".into(),
                );
            }
            Ok(())
        }
    }
}

pub fn validate_placement_intent(
    placement: &UnderwritingPlacementIntent,
    flight: &UnderwritingFlight,
) -> Result<(), String> {
    placement.validate_shape()?;
    flight.validate()?;
    if placement.flight_id != flight.flight_id || placement.flight_revision != flight.revision {
        return Err("underwriting placement must bind the exact flight revision".into());
    }
    if !flight
        .channel_ids
        .iter()
        .any(|channel_id| channel_id == &placement.channel_id)
        || placement.scheduled_at_unix_ms < flight.starts_at_unix_ms
        || placement.scheduled_at_unix_ms >= flight.ends_at_unix_ms
        || flight.placement.allowed_weekdays & (1 << placement.station_weekday) == 0
        || placement.station_minute_of_day < flight.placement.daypart_start_minute
        || placement.station_minute_of_day >= flight.placement.daypart_end_minute
    {
        return Err("underwriting placement violates its flight constraints".into());
    }
    Ok(())
}

pub fn validate_placement_write(
    current: Option<&UnderwritingPlacementIntent>,
    expected_revision: Option<u64>,
    proposed: &UnderwritingPlacementIntent,
    flight: &UnderwritingFlight,
) -> Result<(), String> {
    validate_placement_intent(proposed, flight)?;
    match current {
        None if expected_revision.is_none() && proposed.revision == 1 => Ok(()),
        None => Err(
            "underwriting placement creation requires revision one and no expected revision".into(),
        ),
        Some(current) => {
            current.validate_shape()?;
            require_next_revision(
                current.revision,
                expected_revision,
                proposed.revision,
                "underwriting placement",
            )?;
            if proposed.placement_id != current.placement_id
                || proposed.flight_id != current.flight_id
                || proposed.channel_id != current.channel_id
            {
                return Err(
                    "underwriting placement identity, flight, and channel cannot be rebound".into(),
                );
            }
            Ok(())
        }
    }
}

pub fn validate_proof_of_airing_append(
    existing: &[ProofOfAiringJoinIntent],
    proposed: &ProofOfAiringJoinIntent,
    spot: &UnderwritingSpot,
    flight: &UnderwritingFlight,
    placement: &UnderwritingPlacementIntent,
) -> Result<(), String> {
    proposed.validate_shape()?;
    spot.validate()?;
    validate_placement_intent(placement, flight)?;
    if existing.len() >= MAX_PROOF_OF_AIRING_JOINS {
        return Err("proof-of-airing join context exceeds its bounded record count".into());
    }
    if proposed.sponsor_id != spot.sponsor_id
        || proposed.spot_id != spot.spot_id
        || proposed.spot_revision != spot.revision
        || proposed.flight_id != flight.flight_id
        || proposed.flight_revision != flight.revision
        || proposed.placement_id != placement.placement_id
        || proposed.placement_revision != placement.revision
        || proposed.aired_at_unix_ms != placement.scheduled_at_unix_ms
        || proposed.aired_duration_ms != spot.duration_ms
    {
        return Err(
            "proof-of-airing join must bind exact spot, flight, placement, and as-run facts".into(),
        );
    }
    let mut proof_ids = HashSet::with_capacity(existing.len());
    let mut as_run_ids = HashSet::with_capacity(existing.len());
    for proof in existing {
        proof.validate_shape()?;
        if !proof_ids.insert(proof.proof_id.as_str())
            || !as_run_ids.insert(proof.as_run_event_id.as_str())
        {
            return Err("existing proof-of-airing joins are not immutable and unique".into());
        }
    }
    if proof_ids.contains(proposed.proof_id.as_str())
        || as_run_ids.contains(proposed.as_run_event_id.as_str())
    {
        return Err("proof-of-airing identity and as-run event may be joined only once".into());
    }
    Ok(())
}

pub fn validate_billing_export_intent(
    export: &BillingExportIntent,
    proofs: &[ProofOfAiringJoinIntent],
) -> Result<(), String> {
    export.validate_shape()?;
    let requested = export.proof_ids.iter().collect::<HashSet<_>>();
    if requested.len() != proofs.len() {
        return Err(
            "billing export must provide one exact immutable proof for every requested identity"
                .into(),
        );
    }
    for proof in proofs {
        proof.validate_shape()?;
        if !requested.contains(&proof.proof_id)
            || proof.sponsor_id != export.sponsor_id
            || proof.aired_at_unix_ms < export.period_start_unix_ms
            || proof.aired_at_unix_ms >= export.period_end_unix_ms
        {
            return Err("billing export proof binding, sponsor, or period is invalid".into());
        }
    }
    Ok(())
}

fn require_next_revision(
    current: u64,
    expected: Option<u64>,
    proposed: u64,
    subject: &str,
) -> Result<(), String> {
    if expected != Some(current) || current.checked_add(1) != Some(proposed) {
        return Err(format!(
            "{subject} write has a stale or invalid compare-and-set revision"
        ));
    }
    Ok(())
}

fn validate_uuid(value: &str, field: &str) -> Result<(), String> {
    let bytes = value.as_bytes();
    if bytes.len() != 36
        || ![8, 13, 18, 23].iter().all(|&index| bytes[index] == b'-')
        || bytes.iter().enumerate().any(|(index, byte)| {
            !matches!(index, 8 | 13 | 18 | 23)
                && !(byte.is_ascii_digit() || (b'a'..=b'f').contains(byte))
        })
    {
        return Err(format!("{field} must be a lowercase canonical UUID"));
    }
    Ok(())
}

fn validate_visible(value: &str, field: &str) -> Result<(), String> {
    if value.is_empty()
        || value.trim() != value
        || value.len() > MAX_TEXT_BYTES
        || value.chars().any(char::is_control)
    {
        return Err(format!(
            "{field} must be nonempty, trimmed, visible, and bounded"
        ));
    }
    Ok(())
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

#[cfg(test)]
mod tests {
    use super::*;

    const SPONSOR: &str = "11111111-1111-4111-8111-111111111111";
    const SPOT: &str = "22222222-2222-4222-8222-222222222222";
    const FLIGHT: &str = "33333333-3333-4333-8333-333333333333";
    const CHANNEL: &str = "44444444-4444-4444-8444-444444444444";

    fn spot(revision: u64) -> UnderwritingSpot {
        UnderwritingSpot {
            spot_id: SPOT.into(),
            sponsor_id: SPONSOR.into(),
            asset_id: "a".repeat(64),
            label: "City Council acknowledgement".into(),
            duration_ms: 30_000,
            revision,
        }
    }
    fn flight(revision: u64) -> UnderwritingFlight {
        UnderwritingFlight {
            flight_id: FLIGHT.into(),
            sponsor_id: SPONSOR.into(),
            spot_id: SPOT.into(),
            spot_revision: 1,
            starts_at_unix_ms: 1_700_000_000_000,
            ends_at_unix_ms: 1_700_086_400_000,
            channel_ids: vec![CHANNEL.into()],
            placement: PlacementConstraints {
                allowed_weekdays: 0b0000_0010,
                daypart_start_minute: 480,
                daypart_end_minute: 1_020,
                max_airings: 10,
                max_airings_per_day: 2,
                minimum_separation_ms: 3_600_000,
            },
            revision,
        }
    }
    fn placement(revision: u64) -> UnderwritingPlacementIntent {
        UnderwritingPlacementIntent {
            placement_id: "55555555-5555-4555-8555-555555555555".into(),
            flight_id: FLIGHT.into(),
            flight_revision: 1,
            channel_id: CHANNEL.into(),
            scheduled_at_unix_ms: 1_700_000_036_000,
            station_weekday: 1,
            station_minute_of_day: 600,
            revision,
        }
    }
    fn proof() -> ProofOfAiringJoinIntent {
        ProofOfAiringJoinIntent {
            proof_id: "66666666-6666-4666-8666-666666666666".into(),
            sponsor_id: SPONSOR.into(),
            spot_id: SPOT.into(),
            spot_revision: 1,
            flight_id: FLIGHT.into(),
            flight_revision: 1,
            placement_id: "55555555-5555-4555-8555-555555555555".into(),
            placement_revision: 1,
            as_run_event_id: "77777777-7777-4777-8777-777777777777".into(),
            aired_at_unix_ms: 1_700_000_036_000,
            aired_duration_ms: 30_000,
        }
    }

    #[test]
    fn validates_revision_bound_spot_flight_placement_proof_and_export() {
        let spot = spot(1);
        let flight = flight(1);
        let placement = placement(1);
        let proof = proof();
        assert!(validate_spot_write(None, None, &spot).is_ok());
        assert!(validate_flight_write(None, None, &flight, &spot).is_ok());
        assert!(validate_placement_write(None, None, &placement, &flight).is_ok());
        assert!(validate_proof_of_airing_append(&[], &proof, &spot, &flight, &placement).is_ok());
        let export = BillingExportIntent {
            export_id: "88888888-8888-4888-8888-888888888888".into(),
            sponsor_id: SPONSOR.into(),
            period_start_unix_ms: 1_700_000_000_000,
            period_end_unix_ms: 1_700_000_100_000,
            proof_ids: vec![proof.proof_id.clone()],
            revision: 1,
        };
        assert!(validate_billing_export_intent(&export, &[proof]).is_ok());
    }

    #[test]
    fn rejects_placement_that_violates_channel_day_or_daypart_constraints() {
        let flight = flight(1);
        let mut invalid = placement(1);
        invalid.station_weekday = 0;
        assert!(validate_placement_intent(&invalid, &flight).is_err());
        invalid.station_weekday = 1;
        invalid.station_minute_of_day = 1_100;
        assert!(validate_placement_intent(&invalid, &flight).is_err());
        invalid.station_minute_of_day = 600;
        invalid.channel_id = "99999999-9999-4999-8999-999999999999".into();
        assert!(validate_placement_intent(&invalid, &flight).is_err());
    }

    #[test]
    fn rejects_stale_cas_and_identity_rebinding() {
        let current = spot(1);
        let mut update = spot(2);
        assert!(validate_spot_write(Some(&current), Some(1), &update).is_ok());
        assert!(validate_spot_write(Some(&current), Some(2), &update).is_err());
        update.asset_id = "b".repeat(64);
        assert!(validate_spot_write(Some(&current), Some(1), &update).is_err());
    }

    #[test]
    fn proof_of_airing_joins_are_append_only_and_billing_binds_exact_proofs() {
        let spot = spot(1);
        let flight = flight(1);
        let placement = placement(1);
        let proof = proof();
        assert!(
            validate_proof_of_airing_append(
                std::slice::from_ref(&proof),
                &proof,
                &spot,
                &flight,
                &placement
            )
            .is_err()
        );
        let export = BillingExportIntent {
            export_id: "88888888-8888-4888-8888-888888888888".into(),
            sponsor_id: SPONSOR.into(),
            period_start_unix_ms: 1_700_000_000_000,
            period_end_unix_ms: 1_700_000_100_000,
            proof_ids: vec![proof.proof_id.clone()],
            revision: 1,
        };
        let mut foreign = proof.clone();
        foreign.sponsor_id = "99999999-9999-4999-8999-999999999999".into();
        assert!(validate_billing_export_intent(&export, &[foreign]).is_err());
    }

    #[test]
    fn rejects_unknown_serde_and_unbounded_inputs() {
        assert!(
            serde_json::from_str::<UnderwritingSpot>(
                r#"{"spot_id":"22222222-2222-4222-8222-222222222222","unexpected":true}"#
            )
            .is_err()
        );
        let mut invalid = flight(1);
        invalid.placement.max_airings = MAX_AIRINGS_PER_FLIGHT + 1;
        assert!(invalid.validate().is_err());
        let mut export = BillingExportIntent {
            export_id: "88888888-8888-4888-8888-888888888888".into(),
            sponsor_id: SPONSOR.into(),
            period_start_unix_ms: 1_700_000_000_000,
            period_end_unix_ms: 1_700_000_100_000,
            proof_ids: vec![proof().proof_id],
            revision: 1,
        };
        export.revision = 2;
        assert!(export.validate_shape().is_err());
        export.revision = 1;
        export.proof_ids =
            vec!["66666666-6666-4666-8666-666666666666".into(); MAX_PROOFS_PER_EXPORT + 1];
        assert!(export.validate_shape().is_err());
    }
}
