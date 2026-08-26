use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use sha2::{Digest, Sha256};
use station_catalog::{CatalogAssetRecord, CatalogEditorialState};
use station_schedule::{
    AssetReadiness, ChannelConfiguration, MediaAsset, OutputProfileId, OutputSinkConfiguration,
};
use station_schedule_automation::{
    AssetPickStrategy, AssetPredicate, DaypartBlock, LocalDate, MaterializationWindow,
    RecurrenceTimeZone, RecurringRule, SavedSearch, SavedSearchOrder, SavedSearchQuery,
    SourceEvidenceAuthority, SourceEvidenceKind, SourceEvidenceReceipt, SourceEvidenceVerifier,
};
use station_storage::{
    ScheduleMaterializationApplyOutcome, ScheduleMaterializationOutcome,
    ScheduleMaterializationRun, StationStore,
};

const CHANNEL: &str = "11111111-1111-4111-8111-111111111111";
const OWNER: &str = "22222222-2222-4222-8222-222222222222";
const SEARCH: &str = "33333333-3333-4333-8333-333333333333";
const BLOCK: &str = "44444444-4444-4444-8444-444444444444";
const RULE_14: &str = "55555555-5555-4555-8555-555555555555";
const RULE_60: &str = "66666666-6666-4666-8666-666666666666";
const ASSET: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const AUTHORITY_NOW: i64 = 4_070_908_800_000; // 2099-01-01T00:00:00Z
const MINUTE_MS: i64 = 60_000;
const DAY_MS: i64 = 86_400_000;

static DATABASE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

struct TestEvidenceAuthority {
    sequence: AtomicU64,
}

impl TestEvidenceAuthority {
    fn new() -> Self {
        Self {
            sequence: AtomicU64::new(1),
        }
    }

    fn attestation(
        kind: SourceEvidenceKind,
        receipt_id: &str,
        scope_sha256: &str,
        payload_sha256: &str,
    ) -> String {
        let mut digest = Sha256::new();
        digest.update(match kind {
            SourceEvidenceKind::CandidateProjection => b"candidate".as_slice(),
            SourceEvidenceKind::PriorAirings => b"prior".as_slice(),
        });
        for value in [receipt_id, scope_sha256, payload_sha256] {
            digest.update((value.len() as u64).to_le_bytes());
            digest.update(value.as_bytes());
        }
        format!("{:x}", digest.finalize())
    }
}

impl SourceEvidenceVerifier for TestEvidenceAuthority {
    fn verify(
        &self,
        kind: SourceEvidenceKind,
        receipt: &SourceEvidenceReceipt,
        scope_sha256: &str,
        payload_sha256: &str,
        observed_at_utc_ms: i64,
    ) -> Result<(), String> {
        if receipt.issuer_id != "99999999-9999-4999-8999-999999999999"
            || receipt.scope_sha256 != scope_sha256
            || receipt.issued_at_utc_ms > observed_at_utc_ms
            || receipt.expires_at_utc_ms <= observed_at_utc_ms
            || receipt.attestation_sha256
                != Self::attestation(kind, &receipt.receipt_id, scope_sha256, payload_sha256)
        {
            return Err("test evidence failed exact authentication".into());
        }
        Ok(())
    }
}

impl SourceEvidenceAuthority for TestEvidenceAuthority {
    fn observed_at_utc_ms(&self) -> Result<i64, String> {
        Ok(AUTHORITY_NOW)
    }

    fn issue_source_evidence(
        &self,
        kind: SourceEvidenceKind,
        scope_sha256: &str,
        payload_sha256: &str,
        lifetime_ms: i64,
    ) -> Result<SourceEvidenceReceipt, String> {
        let sequence = self.sequence.fetch_add(1, Ordering::Relaxed);
        let receipt_id = format!("88888888-8888-4888-8888-{sequence:012}");
        Ok(SourceEvidenceReceipt {
            issuer_id: "99999999-9999-4999-8999-999999999999".into(),
            receipt_id: receipt_id.clone(),
            issued_at_utc_ms: AUTHORITY_NOW,
            expires_at_utc_ms: AUTHORITY_NOW + lifetime_ms,
            scope_sha256: scope_sha256.into(),
            attestation_sha256: Self::attestation(kind, &receipt_id, scope_sha256, payload_sha256),
        })
    }
}

fn temporary_database(name: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let sequence = DATABASE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    std::env::temp_dir().join(format!(
        "townlight-materialization-horizons-{name}-{nonce}-{sequence}.db"
    ))
}

fn expected_start() -> i64 {
    (AUTHORITY_NOW / MINUTE_MS + 2) * MINUTE_MS
}

fn expected_window(days: u8) -> MaterializationWindow {
    MaterializationWindow {
        starts_at_utc_ms: expected_start(),
        ends_at_utc_ms: expected_start() + i64::from(days) * DAY_MS,
    }
}

fn saved_search() -> SavedSearch {
    SavedSearch {
        search_id: SEARCH.into(),
        name: "Council programs".into(),
        owner_operator_id: OWNER.into(),
        created_at_utc_ms: 1,
        query: SavedSearchQuery {
            all_of: vec![AssetPredicate::TitleContains("Council".into())],
            any_of: vec![],
            order_by: SavedSearchOrder::PublishedNewest,
            limit: 20,
        },
        revision: 1,
    }
}

fn daypart_block() -> DaypartBlock {
    DaypartBlock {
        block_id: BLOCK.into(),
        channel_id: CHANNEL.into(),
        label: "All-day recurring schedule".into(),
        starts_local_minute: 0,
        ends_local_minute: 1_440,
        weekday_mask: 0b0111_1111,
        season_starts_on: None,
        season_ends_on: None,
        allow_rule_ids: vec![RULE_14.into(), RULE_60.into()],
        revision: 1,
    }
}

fn recurring_rule(
    rule_id: &str,
    rolling_window_days: u8,
    enabled: bool,
    priority: i16,
    local_start_minute: u16,
) -> RecurringRule {
    RecurringRule {
        rule_id: rule_id.into(),
        channel_id: CHANNEL.into(),
        saved_search_id: SEARCH.into(),
        saved_search_revision: 1,
        pick_strategy: AssetPickStrategy::Top,
        rolling_window_days,
        avoid_repeat_within_days: None,
        enabled,
        priority,
        block_id: BLOCK.into(),
        block_revision: 1,
        weekday_mask: 0b0111_1111,
        interval_weeks: 1,
        anchor_date: LocalDate {
            year: 2099,
            month: 1,
            day: 1,
        },
        timezone: RecurrenceTimeZone::Utc,
        local_start_minute,
        duration_minutes: 60,
        revision: 1,
    }
}

fn publish_candidate(store: &StationStore, database: &Path) {
    let media_path = database.with_extension("candidate.ts");
    std::fs::write(&media_path, b"mixed horizon materialization candidate").unwrap();
    store
        .put_media_asset(&MediaAsset {
            asset_id: ASSET.into(),
            media_path: media_path.to_string_lossy().into_owned(),
            duration_ms: 3_600_000,
            readiness: AssetReadiness::Ready,
        })
        .unwrap();
    let draft = CatalogAssetRecord {
        asset_sha256: ASSET.into(),
        title: "Council meeting".into(),
        meeting_body: Some("City Council".into()),
        editorial_state: CatalogEditorialState::Draft,
        published_at_utc_ms: None,
        revision: 1,
    };
    store
        .write_catalog_asset(ASSET, &draft, None, "catalog", 2)
        .unwrap();
    store
        .write_catalog_asset(
            ASSET,
            &CatalogAssetRecord {
                editorial_state: CatalogEditorialState::Published,
                published_at_utc_ms: Some(3),
                revision: 2,
                ..draft
            },
            Some(1),
            "catalog",
            3,
        )
        .unwrap();
}

fn seed_store(database: &Path, short_enabled: bool, overlapping_slots: bool) -> StationStore {
    let store = StationStore::open(database).unwrap();
    store
        .put_channel(&ChannelConfiguration {
            channel_id: CHANNEL.into(),
            display_name: "Government".into(),
            output_profile: OutputProfileId::Proof360p30,
            output_sinks: vec![OutputSinkConfiguration::required_udp(
                CHANNEL,
                "Primary",
                "127.0.0.1:5500",
                -24.0,
            )],
            enabled: true,
        })
        .unwrap();
    store
        .write_saved_search(&saved_search(), None, "operator", 1)
        .unwrap();
    store
        .write_daypart_block(&daypart_block(), None, "operator", 2)
        .unwrap();
    store
        .write_recurring_rule(
            &recurring_rule(RULE_14, 14, short_enabled, 20, 60),
            None,
            "operator",
            3,
        )
        .unwrap();
    store
        .write_recurring_rule(
            &recurring_rule(
                RULE_60,
                60,
                true,
                10,
                if overlapping_slots { 60 } else { 180 },
            ),
            None,
            "operator",
            4,
        )
        .unwrap();
    publish_candidate(&store, database);
    store
}

fn run_from(outcome: &ScheduleMaterializationApplyOutcome) -> &ScheduleMaterializationRun {
    match outcome {
        ScheduleMaterializationApplyOutcome::Applied(run)
        | ScheduleMaterializationApplyOutcome::AlreadyApplied(run) => run,
    }
}

fn outcome_start(outcome: &ScheduleMaterializationOutcome) -> i64 {
    match outcome {
        ScheduleMaterializationOutcome::Proposed {
            starts_at_utc_ms, ..
        }
        | ScheduleMaterializationOutcome::NoMatch { starts_at_utc_ms }
        | ScheduleMaterializationOutcome::Blocked { starts_at_utc_ms }
        | ScheduleMaterializationOutcome::SuppressedByPrecedence {
            starts_at_utc_ms, ..
        } => *starts_at_utc_ms,
        ScheduleMaterializationOutcome::ExistingAutomationDraftUnchanged { existing, .. }
        | ScheduleMaterializationOutcome::ExistingAutomationDraftChanged { existing, .. }
        | ScheduleMaterializationOutcome::ExistingCommitted { existing, .. }
        | ScheduleMaterializationOutcome::CancelledOccurrence { existing, .. }
        | ScheduleMaterializationOutcome::AlreadyAired { existing, .. }
        | ScheduleMaterializationOutcome::SuppressedByExistingSchedule { existing, .. } => {
            existing.starts_at_utc_ms
        }
    }
}

#[test]
fn enabled_mixed_horizons_share_one_atomic_union_run_but_keep_exact_plan_windows() {
    let database = temporary_database("enabled-mixed");
    let store = seed_store(&database, true, false);
    let authority = TestEvidenceAuthority::new();

    let outcome = store
        .materialize_scheduled_channel(CHANNEL, OWNER, &authority)
        .unwrap();
    assert!(matches!(
        outcome,
        ScheduleMaterializationApplyOutcome::Applied(_)
    ));
    let run = run_from(&outcome);
    assert_eq!(run.window, expected_window(60));
    assert_eq!(run.plans.len(), 2);

    for (rule_id, expected) in [
        (RULE_14, expected_window(14)),
        (RULE_60, expected_window(60)),
    ] {
        let plan = run
            .plans
            .iter()
            .find(|plan| plan.rule_id == rule_id)
            .expect("every enabled rule must have one plan");
        assert_eq!(plan.window, expected);
        assert_eq!(
            plan.prior_airings_coverage.ends_at_utc_ms,
            expected.ends_at_utc_ms
        );
        let records = run
            .outcomes
            .iter()
            .filter(|record| record.rule_id == rule_id)
            .collect::<Vec<_>>();
        assert!(!records.is_empty());
        assert!(records.iter().all(|record| {
            let starts_at = outcome_start(&record.compiled_outcome);
            starts_at >= expected.starts_at_utc_ms && starts_at < expected.ends_at_utc_ms
        }));
        assert!(records.iter().all(|record| {
            record.prior_airings_coverage == plan.prior_airings_coverage
                && record.scope_sha256 == plan.scope_sha256
                && record.source_input_sha256 == plan.source_input_sha256
        }));
    }
    assert!(run.outcomes.iter().any(|record| {
        record.rule_id == RULE_60
            && outcome_start(&record.compiled_outcome) >= expected_window(14).ends_at_utc_ms
    }));
    assert!(!run.outcomes.iter().any(|record| {
        record.rule_id == RULE_14
            && outcome_start(&record.compiled_outcome) >= expected_window(14).ends_at_utc_ms
    }));

    assert_eq!(
        store
            .read_schedule_materialization_run(&run.run_id)
            .unwrap()
            .expect("the complete union run must be durable"),
        *run
    );
}

#[test]
fn disabled_short_horizon_does_not_constrain_the_enabled_sixty_day_rule() {
    let database = temporary_database("disabled-short");
    let store = seed_store(&database, false, false);
    let authority = TestEvidenceAuthority::new();

    let outcome = store
        .materialize_scheduled_channel(CHANNEL, OWNER, &authority)
        .unwrap();
    let run = run_from(&outcome);
    assert_eq!(run.window, expected_window(60));
    assert_eq!(run.plans.len(), 1);
    assert_eq!(run.plans[0].rule_id, RULE_60);
    assert_eq!(run.plans[0].window, expected_window(60));
    assert!(run.outcomes.iter().all(|record| record.rule_id == RULE_60));
    assert!(run.outcomes.iter().any(|record| {
        outcome_start(&record.compiled_outcome) >= expected_window(14).ends_at_utc_ms
    }));
}

#[test]
fn cross_window_arbitration_never_admits_duplicate_or_overlapping_drafts() {
    let database = temporary_database("cross-window-overlap");
    let store = seed_store(&database, true, true);
    let authority = TestEvidenceAuthority::new();

    let outcome = store
        .materialize_scheduled_channel(CHANNEL, OWNER, &authority)
        .unwrap();
    let run = run_from(&outcome);
    let short_end = expected_window(14).ends_at_utc_ms;

    assert!(run.outcomes.iter().any(|record| {
        record.rule_id == RULE_60
            && outcome_start(&record.compiled_outcome) < short_end
            && matches!(
                &record.compiled_outcome,
                ScheduleMaterializationOutcome::SuppressedByPrecedence {
                    winner_rule_id,
                    ..
                } if winner_rule_id == RULE_14
            )
    }));
    assert!(run.outcomes.iter().any(|record| {
        record.rule_id == RULE_60
            && outcome_start(&record.compiled_outcome) >= short_end
            && matches!(
                record.compiled_outcome,
                ScheduleMaterializationOutcome::Proposed { .. }
            )
    }));

    let mut proposed = run
        .outcomes
        .iter()
        .filter_map(|record| match &record.outcome {
            ScheduleMaterializationOutcome::Proposed {
                occurrence_id,
                schedule_item_id,
                starts_at_utc_ms,
                ends_at_utc_ms,
                ..
            } => Some((
                *starts_at_utc_ms,
                *ends_at_utc_ms,
                occurrence_id,
                schedule_item_id,
            )),
            _ => None,
        })
        .collect::<Vec<_>>();
    proposed.sort_by_key(|item| (item.0, item.1));
    assert!(proposed.windows(2).all(|pair| pair[0].1 <= pair[1].0));
    let mut occurrence_ids = BTreeSet::new();
    let mut schedule_item_ids = BTreeSet::new();
    for (_, _, occurrence_id, schedule_item_id) in proposed {
        assert!(occurrence_ids.insert(occurrence_id));
        assert!(schedule_item_ids.insert(schedule_item_id));
    }
}

#[test]
fn same_server_minute_retry_reuses_the_derived_window_and_run() {
    let database = temporary_database("same-minute-retry");
    let store = seed_store(&database, true, false);
    let authority = TestEvidenceAuthority::new();

    let first = store
        .materialize_scheduled_channel(CHANNEL, OWNER, &authority)
        .unwrap();
    let second = store
        .materialize_scheduled_channel(CHANNEL, OWNER, &authority)
        .unwrap();

    assert!(matches!(
        first,
        ScheduleMaterializationApplyOutcome::Applied(_)
    ));
    assert!(matches!(
        second,
        ScheduleMaterializationApplyOutcome::AlreadyApplied(_)
    ));
    assert_eq!(run_from(&first).window, expected_window(60));
    assert_eq!(run_from(&first).run_id, run_from(&second).run_id);
    assert_eq!(
        run_from(&first).request_sha256,
        run_from(&second).request_sha256
    );
    assert_eq!(
        run_from(&first).result_sha256,
        run_from(&second).result_sha256
    );
}
