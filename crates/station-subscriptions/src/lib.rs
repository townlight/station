//! Pure, fail-closed optional-subscription authority contracts.
//!
//! It contains no provider client, raw card data, raw token, raw webhook body, or
//! email delivery. A receipt records authority intent only, never payment or delivery.

use std::collections::HashSet;

use serde::{Deserialize, Serialize};

pub const MAX_PLAN_TEXT_BYTES: usize = 200;
pub const MAX_RECEIPT_DETAIL_BYTES: usize = 500;
pub const MAX_MAGIC_LINK_LIFETIME_MS: i64 = 86_400_000;
pub const MAX_ENTITLEMENT_PERIOD_MS: i64 = 366 * 86_400_000;
pub const MAX_WEBHOOK_INTENTS: usize = 100_000;
pub const MAX_RECEIPTS: usize = 100_000;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SubscriptionPlan {
    pub plan_id: String,
    pub label: String,
    pub period_days: u16,
    /// Plans are created off and must be explicitly enabled in a later CAS write.
    pub checkout_enabled: bool,
    pub revision: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EntitlementState {
    Disabled,
    PendingCheckout,
    Active,
    Suspended,
    Cancelled,
    Expired,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SubscriptionEntitlement {
    pub entitlement_id: String,
    pub account_id: String,
    pub plan_id: String,
    pub plan_revision: u64,
    pub state: EntitlementState,
    pub period_start_unix_ms: Option<i64>,
    pub period_end_unix_ms: Option<i64>,
    pub revision: u64,
}

/// A request for a future hosted-checkout adapter. It deliberately has no provider
/// session ID, provider URL, customer token, or card-data field.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HostedCheckoutIntent {
    pub checkout_id: String,
    pub entitlement_id: String,
    pub entitlement_revision: u64,
    pub plan_id: String,
    pub plan_revision: u64,
    pub requested_at_unix_ms: i64,
}

/// An expiring single-use link represented exclusively by a SHA-256 digest.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MagicLinkIntent {
    pub link_id: String,
    pub entitlement_id: String,
    pub entitlement_revision: u64,
    pub token_digest: String,
    pub issued_at_unix_ms: i64,
    pub expires_at_unix_ms: i64,
}

/// A request for later signature verification. Its delivery and signature are digests;
/// accepting it does not assert that a provider webhook was actually verified.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SignedWebhookVerificationIntent {
    pub verification_id: String,
    pub delivery_digest: String,
    pub signature_digest: String,
    pub entitlement_id: String,
    pub entitlement_revision: u64,
    pub received_at_unix_ms: i64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SubscriptionReceiptSubject {
    Checkout,
    MagicLink,
    WebhookVerification,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SubscriptionReceiptOutcome {
    AcceptedForFutureProcessing,
    RejectedBeforeProcessing,
}

/// Append-only local authority receipt. It is not a checkout, email, webhook, or
/// payment result.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SubscriptionReceipt {
    pub receipt_id: String,
    pub subject: SubscriptionReceiptSubject,
    pub subject_id: String,
    pub recorded_at_unix_ms: i64,
    pub outcome: SubscriptionReceiptOutcome,
    pub detail: String,
}

impl SubscriptionPlan {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.plan_id, "subscription plan identity")?;
        validate_visible(&self.label, MAX_PLAN_TEXT_BYTES, "subscription plan label")?;
        if self.period_days == 0 || self.period_days > 366 || self.revision == 0 {
            return Err("subscription plan period or revision is invalid or unbounded".into());
        }
        Ok(())
    }
}

impl SubscriptionEntitlement {
    pub fn validate_shape(&self) -> Result<(), String> {
        validate_uuid(&self.entitlement_id, "subscription entitlement identity")?;
        validate_uuid(
            &self.account_id,
            "subscription entitlement account identity",
        )?;
        validate_uuid(&self.plan_id, "subscription entitlement plan identity")?;
        if self.plan_revision == 0 || self.revision == 0 {
            return Err("subscription entitlement revisions must be positive".into());
        }
        validate_entitlement_period(
            self.state,
            self.period_start_unix_ms,
            self.period_end_unix_ms,
        )
    }
}

impl HostedCheckoutIntent {
    pub fn validate_shape(&self) -> Result<(), String> {
        validate_uuid(&self.checkout_id, "hosted checkout identity")?;
        validate_uuid(&self.entitlement_id, "hosted checkout entitlement identity")?;
        validate_uuid(&self.plan_id, "hosted checkout plan identity")?;
        if self.entitlement_revision == 0
            || self.plan_revision == 0
            || self.requested_at_unix_ms < 0
        {
            return Err("hosted checkout revisions or request time are invalid".into());
        }
        Ok(())
    }
}

impl MagicLinkIntent {
    pub fn validate_shape(&self) -> Result<(), String> {
        validate_uuid(&self.link_id, "magic-link identity")?;
        validate_uuid(&self.entitlement_id, "magic-link entitlement identity")?;
        if self.entitlement_revision == 0
            || !is_sha256(&self.token_digest)
            || self.issued_at_unix_ms < 0
            || self.expires_at_unix_ms <= self.issued_at_unix_ms
            || self.expires_at_unix_ms - self.issued_at_unix_ms > MAX_MAGIC_LINK_LIFETIME_MS
        {
            return Err("magic-link digest, revision, or expiry is invalid or unbounded".into());
        }
        Ok(())
    }
}

impl SignedWebhookVerificationIntent {
    pub fn validate_shape(&self) -> Result<(), String> {
        validate_uuid(&self.verification_id, "webhook verification identity")?;
        validate_uuid(
            &self.entitlement_id,
            "webhook verification entitlement identity",
        )?;
        if !is_sha256(&self.delivery_digest)
            || !is_sha256(&self.signature_digest)
            || self.entitlement_revision == 0
            || self.received_at_unix_ms < 0
        {
            return Err("webhook verification digest, revision, or receipt time is invalid".into());
        }
        Ok(())
    }
}

impl SubscriptionReceipt {
    pub fn validate_shape(&self) -> Result<(), String> {
        validate_uuid(&self.receipt_id, "subscription receipt identity")?;
        validate_uuid(&self.subject_id, "subscription receipt subject identity")?;
        if self.recorded_at_unix_ms < 0 {
            return Err("subscription receipt time is invalid".into());
        }
        validate_visible(
            &self.detail,
            MAX_RECEIPT_DETAIL_BYTES,
            "subscription receipt detail",
        )
    }
}

pub fn validate_plan_write(
    current: Option<&SubscriptionPlan>,
    expected_revision: Option<u64>,
    proposed: &SubscriptionPlan,
) -> Result<(), String> {
    proposed.validate()?;
    match current {
        None if expected_revision.is_none() && proposed.revision == 1 && !proposed.checkout_enabled => Ok(()),
        None => Err("subscription plan creation requires revision one, no expected revision, and checkout disabled".into()),
        Some(current) => {
            current.validate()?;
            require_next_revision(current.revision, expected_revision, proposed.revision, "subscription plan")?;
            if proposed.plan_id != current.plan_id {
                return Err("subscription plan identity cannot be rebound".into());
            }
            Ok(())
        }
    }
}

pub fn validate_entitlement_write(
    current: Option<&SubscriptionEntitlement>,
    expected_revision: Option<u64>,
    proposed: &SubscriptionEntitlement,
    plan: &SubscriptionPlan,
) -> Result<(), String> {
    proposed.validate_shape()?;
    plan.validate()?;
    if proposed.plan_id != plan.plan_id || proposed.plan_revision != plan.revision {
        return Err("subscription entitlement must bind the exact current plan revision".into());
    }
    match current {
        None if expected_revision.is_none() && proposed.revision == 1 && proposed.state == EntitlementState::Disabled => Ok(()),
        None => Err("subscription entitlement creation requires revision one, no expected revision, and disabled state".into()),
        Some(current) => {
            current.validate_shape()?;
            require_next_revision(current.revision, expected_revision, proposed.revision, "subscription entitlement")?;
            if proposed.entitlement_id != current.entitlement_id
                || proposed.account_id != current.account_id
                || proposed.plan_id != current.plan_id
            {
                return Err("subscription entitlement identity, account, and plan cannot be rebound".into());
            }
            if !allows_transition(current.state, proposed.state)
                || is_terminal(current.state) && proposed.state != current.state
            {
                return Err("subscription entitlement transition is invalid or terminal reanimation".into());
            }
            if matches!(proposed.state, EntitlementState::PendingCheckout) && !plan.checkout_enabled {
                return Err("pending checkout requires an explicitly enabled subscription plan".into());
            }
            Ok(())
        }
    }
}

pub fn validate_hosted_checkout_intent(
    intent: &HostedCheckoutIntent,
    entitlement: &SubscriptionEntitlement,
    plan: &SubscriptionPlan,
) -> Result<(), String> {
    intent.validate_shape()?;
    entitlement.validate_shape()?;
    plan.validate()?;
    if !plan.checkout_enabled
        || entitlement.state != EntitlementState::PendingCheckout
        || intent.entitlement_id != entitlement.entitlement_id
        || intent.entitlement_revision != entitlement.revision
        || intent.plan_id != plan.plan_id
        || intent.plan_revision != plan.revision
    {
        return Err(
            "hosted checkout intent must bind an enabled plan and exact pending entitlement".into(),
        );
    }
    Ok(())
}

pub fn validate_magic_link_issue(
    intent: &MagicLinkIntent,
    entitlement: &SubscriptionEntitlement,
) -> Result<(), String> {
    intent.validate_shape()?;
    entitlement.validate_shape()?;
    if entitlement.state != EntitlementState::Active
        || intent.entitlement_id != entitlement.entitlement_id
        || intent.entitlement_revision != entitlement.revision
    {
        return Err("magic-link issue must bind an exact active entitlement".into());
    }
    Ok(())
}

pub fn validate_magic_link_redemption_append(
    existing: &[SubscriptionReceipt],
    receipt: &SubscriptionReceipt,
    link: &MagicLinkIntent,
    presented_token_digest: &str,
) -> Result<(), String> {
    link.validate_shape()?;
    if !is_sha256(presented_token_digest)
        || presented_token_digest != link.token_digest
        || receipt.subject != SubscriptionReceiptSubject::MagicLink
        || receipt.subject_id != link.link_id
        || receipt.recorded_at_unix_ms < link.issued_at_unix_ms
        || receipt.recorded_at_unix_ms >= link.expires_at_unix_ms
    {
        return Err("magic-link redemption must be one exact unexpired digest-backed link".into());
    }
    validate_receipt_append(existing, receipt)
}

pub fn validate_webhook_verification_append(
    existing: &[SignedWebhookVerificationIntent],
    proposed: &SignedWebhookVerificationIntent,
    entitlement: &SubscriptionEntitlement,
) -> Result<(), String> {
    proposed.validate_shape()?;
    entitlement.validate_shape()?;
    if existing.len() >= MAX_WEBHOOK_INTENTS
        || proposed.entitlement_id != entitlement.entitlement_id
        || proposed.entitlement_revision != entitlement.revision
    {
        return Err(
            "webhook verification intent has an invalid entitlement binding or unbounded context"
                .into(),
        );
    }
    let mut verification_ids = HashSet::with_capacity(existing.len());
    let mut deliveries = HashSet::with_capacity(existing.len());
    for intent in existing {
        intent.validate_shape()?;
        if !verification_ids.insert(intent.verification_id.as_str())
            || !deliveries.insert(intent.delivery_digest.as_str())
        {
            return Err(
                "existing webhook verification intents are not immutable and unique".into(),
            );
        }
    }
    if verification_ids.contains(proposed.verification_id.as_str())
        || deliveries.contains(proposed.delivery_digest.as_str())
    {
        return Err(
            "webhook verification identity and delivery may be represented only once".into(),
        );
    }
    Ok(())
}

pub fn validate_receipt_append(
    existing: &[SubscriptionReceipt],
    proposed: &SubscriptionReceipt,
) -> Result<(), String> {
    proposed.validate_shape()?;
    if existing.len() >= MAX_RECEIPTS {
        return Err("subscription receipt context exceeds its bounded record count".into());
    }
    let mut receipt_ids = HashSet::with_capacity(existing.len());
    let mut subjects = HashSet::with_capacity(existing.len());
    for receipt in existing {
        receipt.validate_shape()?;
        if !receipt_ids.insert(receipt.receipt_id.as_str())
            || !subjects.insert((receipt.subject, receipt.subject_id.as_str()))
        {
            return Err("existing subscription receipts are not immutable and unique".into());
        }
    }
    if receipt_ids.contains(proposed.receipt_id.as_str())
        || subjects.contains(&(proposed.subject, proposed.subject_id.as_str()))
    {
        return Err("subscription receipt identity and subject may be recorded only once".into());
    }
    Ok(())
}

fn validate_entitlement_period(
    state: EntitlementState,
    start: Option<i64>,
    end: Option<i64>,
) -> Result<(), String> {
    match (state, start, end) {
        (EntitlementState::Disabled | EntitlementState::PendingCheckout, None, None) => Ok(()),
        (_, Some(start), Some(end))
            if start >= 0 && end > start && end - start <= MAX_ENTITLEMENT_PERIOD_MS =>
        {
            Ok(())
        }
        _ => Err(
            "subscription entitlement period is invalid, unbounded, or incompatible with its state"
                .into(),
        ),
    }
}

fn allows_transition(current: EntitlementState, proposed: EntitlementState) -> bool {
    matches!(
        (current, proposed),
        (
            EntitlementState::Disabled,
            EntitlementState::Disabled | EntitlementState::PendingCheckout
        ) | (
            EntitlementState::PendingCheckout,
            EntitlementState::PendingCheckout
                | EntitlementState::Active
                | EntitlementState::Disabled
                | EntitlementState::Cancelled
        ) | (
            EntitlementState::Active,
            EntitlementState::Active
                | EntitlementState::Suspended
                | EntitlementState::Cancelled
                | EntitlementState::Expired
        ) | (
            EntitlementState::Suspended,
            EntitlementState::Suspended
                | EntitlementState::Active
                | EntitlementState::Cancelled
                | EntitlementState::Expired
        ) | (EntitlementState::Cancelled, EntitlementState::Cancelled)
            | (EntitlementState::Expired, EntitlementState::Expired)
    )
}

fn is_terminal(state: EntitlementState) -> bool {
    matches!(
        state,
        EntitlementState::Cancelled | EntitlementState::Expired
    )
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

fn validate_visible(value: &str, max_bytes: usize, field: &str) -> Result<(), String> {
    if value.is_empty()
        || value.trim() != value
        || value.len() > max_bytes
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

    const PLAN: &str = "11111111-1111-4111-8111-111111111111";
    const ACCOUNT: &str = "22222222-2222-4222-8222-222222222222";
    const ENTITLEMENT: &str = "33333333-3333-4333-8333-333333333333";

    fn plan(revision: u64, checkout_enabled: bool) -> SubscriptionPlan {
        SubscriptionPlan {
            plan_id: PLAN.into(),
            label: "Monthly supporter".into(),
            period_days: 30,
            checkout_enabled,
            revision,
        }
    }
    fn entitlement(
        revision: u64,
        plan_revision: u64,
        state: EntitlementState,
    ) -> SubscriptionEntitlement {
        let period = matches!(
            state,
            EntitlementState::Active
                | EntitlementState::Suspended
                | EntitlementState::Cancelled
                | EntitlementState::Expired
        )
        .then_some((Some(1_700_000_000_000), Some(1_700_002_592_000)));
        SubscriptionEntitlement {
            entitlement_id: ENTITLEMENT.into(),
            account_id: ACCOUNT.into(),
            plan_id: PLAN.into(),
            plan_revision,
            state,
            period_start_unix_ms: period.and_then(|(start, _)| start),
            period_end_unix_ms: period.and_then(|(_, end)| end),
            revision,
        }
    }
    fn checkout() -> HostedCheckoutIntent {
        HostedCheckoutIntent {
            checkout_id: "44444444-4444-4444-8444-444444444444".into(),
            entitlement_id: ENTITLEMENT.into(),
            entitlement_revision: 2,
            plan_id: PLAN.into(),
            plan_revision: 2,
            requested_at_unix_ms: 1_700_000_000_000,
        }
    }
    fn magic_link() -> MagicLinkIntent {
        MagicLinkIntent {
            link_id: "55555555-5555-4555-8555-555555555555".into(),
            entitlement_id: ENTITLEMENT.into(),
            entitlement_revision: 3,
            token_digest: "a".repeat(64),
            issued_at_unix_ms: 1_700_000_000_000,
            expires_at_unix_ms: 1_700_000_060_000,
        }
    }
    fn receipt(subject: SubscriptionReceiptSubject, subject_id: String) -> SubscriptionReceipt {
        SubscriptionReceipt {
            receipt_id: "66666666-6666-4666-8666-666666666666".into(),
            subject,
            subject_id,
            recorded_at_unix_ms: 1_700_000_001_000,
            outcome: SubscriptionReceiptOutcome::AcceptedForFutureProcessing,
            detail: "recorded without delivery claim".into(),
        }
    }

    #[test]
    fn creates_default_off_plan_and_disabled_entitlement_then_binds_checkout() {
        let initial_plan = plan(1, false);
        assert!(validate_plan_write(None, None, &initial_plan).is_ok());
        assert!(
            validate_entitlement_write(
                None,
                None,
                &entitlement(1, 1, EntitlementState::Disabled),
                &initial_plan
            )
            .is_ok()
        );
        let enabled_plan = plan(2, true);
        assert!(validate_plan_write(Some(&initial_plan), Some(1), &enabled_plan).is_ok());
        let pending = entitlement(2, 2, EntitlementState::PendingCheckout);
        assert!(
            validate_entitlement_write(
                Some(&entitlement(1, 1, EntitlementState::Disabled)),
                Some(1),
                &pending,
                &enabled_plan
            )
            .is_ok()
        );
        assert!(validate_hosted_checkout_intent(&checkout(), &pending, &enabled_plan).is_ok());
    }

    #[test]
    fn rejects_raw_card_fields_unbounded_periods_and_stale_or_rebound_writes() {
        assert!(serde_json::from_str::<HostedCheckoutIntent>(r#"{"checkout_id":"44444444-4444-4444-8444-444444444444","card_number":"4111111111111111"}"#).is_err());
        assert!(
            serde_json::from_str::<MagicLinkIntent>(
                r#"{"link_id":"55555555-5555-4555-8555-555555555555","raw_token":"not-persisted"}"#
            )
            .is_err()
        );
        assert!(validate_plan_write(None, None, &plan(1, true)).is_err());
        let mut invalid = plan(1, false);
        invalid.period_days = 367;
        assert!(invalid.validate().is_err());
        let mut update = plan(2, true);
        update.plan_id = "99999999-9999-4999-8999-999999999999".into();
        assert!(validate_plan_write(Some(&plan(1, false)), Some(1), &update).is_err());
        assert!(validate_plan_write(Some(&plan(1, false)), Some(2), &plan(2, true)).is_err());
    }

    #[test]
    fn magic_links_are_digest_only_expiring_and_single_use() {
        let link = magic_link();
        let active = entitlement(3, 2, EntitlementState::Active);
        assert!(validate_magic_link_issue(&link, &active).is_ok());
        let receipt = receipt(SubscriptionReceiptSubject::MagicLink, link.link_id.clone());
        assert!(
            validate_magic_link_redemption_append(&[], &receipt, &link, &link.token_digest).is_ok()
        );
        assert!(
            validate_magic_link_redemption_append(
                std::slice::from_ref(&receipt),
                &receipt,
                &link,
                &link.token_digest
            )
            .is_err()
        );
        let mut expired = link.clone();
        expired.expires_at_unix_ms = receipt.recorded_at_unix_ms;
        assert!(
            validate_magic_link_redemption_append(&[], &receipt, &expired, &expired.token_digest)
                .is_err()
        );
    }

    #[test]
    fn webhook_verification_intent_rejects_delivery_replay_without_claiming_verification() {
        let proposed = SignedWebhookVerificationIntent {
            verification_id: "77777777-7777-4777-8777-777777777777".into(),
            delivery_digest: "b".repeat(64),
            signature_digest: "c".repeat(64),
            entitlement_id: ENTITLEMENT.into(),
            entitlement_revision: 3,
            received_at_unix_ms: 1_700_000_000_000,
        };
        let active = entitlement(3, 2, EntitlementState::Active);
        assert!(validate_webhook_verification_append(&[], &proposed, &active).is_ok());
        let mut replay = proposed.clone();
        replay.verification_id = "88888888-8888-4888-8888-888888888888".into();
        assert!(validate_webhook_verification_append(&[proposed], &replay, &active).is_err());
    }

    #[test]
    fn cancelled_and_expired_entitlements_cannot_be_reanimated_and_receipts_are_immutable() {
        let enabled_plan = plan(2, true);
        let cancelled = entitlement(4, 2, EntitlementState::Cancelled);
        let mut revived = entitlement(5, 2, EntitlementState::Active);
        assert!(
            validate_entitlement_write(Some(&cancelled), Some(4), &revived, &enabled_plan).is_err()
        );
        revived.account_id = "99999999-9999-4999-8999-999999999999".into();
        assert!(
            validate_entitlement_write(
                Some(&entitlement(3, 2, EntitlementState::Active)),
                Some(3),
                &revived,
                &enabled_plan
            )
            .is_err()
        );
        let receipt = receipt(SubscriptionReceiptSubject::Checkout, checkout().checkout_id);
        assert!(validate_receipt_append(std::slice::from_ref(&receipt), &receipt).is_err());
    }
}
