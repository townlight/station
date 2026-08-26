use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use station_catalog::{CatalogAssetRecord, CatalogEditorialState};
use station_schedule::{AssetReadiness, MediaAsset, ScheduleState};
use station_schedule_automation::{
    AssetPickStrategy, AssetPredicate, DaypartBlock, LocalDate, MaterializationWindow,
    RecurrenceTimeZone, RecurringRule, SavedSearch, SavedSearchOrder, SavedSearchQuery,
    SourceEvidenceAuthority, SourceEvidenceKind, SourceEvidenceReceipt, SourceEvidenceVerifier,
};
use station_storage::{
    ScheduleMaterializationApplyOutcome, ScheduleMaterializationOutcome, StationStore,
};

const CHANNEL: &str = "11111111-1111-4111-8111-111111111111";
const OWNER: &str = "22222222-2222-4222-8222-222222222222";
const SEARCH: &str = "33333333-3333-4333-8333-333333333333";
const BLOCK: &str = "44444444-4444-4444-8444-444444444444";
const RULE: &str = "55555555-5555-4555-8555-555555555555";
static SEQUENCE: AtomicU64 = AtomicU64::new(0);

struct TestAuthority {
    now: i64,
    sequence: AtomicU64,
}

impl SourceEvidenceVerifier for TestAuthority {
    fn verify(
        &self,
        _kind: SourceEvidenceKind,
        receipt: &SourceEvidenceReceipt,
        scope_sha256: &str,
        payload_sha256: &str,
        observed_at_utc_ms: i64,
    ) -> Result<(), String> {
        if observed_at_utc_ms != self.now
            || receipt.scope_sha256 != scope_sha256
            || receipt.attestation_sha256 != payload_sha256
        {
            return Err("test source evidence did not authenticate".into());
        }
        Ok(())
    }
}

impl SourceEvidenceAuthority for TestAuthority {
    fn observed_at_utc_ms(&self) -> Result<i64, String> {
        Ok(self.now)
    }

    fn issue_source_evidence(
        &self,
        _kind: SourceEvidenceKind,
        scope_sha256: &str,
        payload_sha256: &str,
        lifetime_ms: i64,
    ) -> Result<SourceEvidenceReceipt, String> {
        let sequence = self.sequence.fetch_add(1, Ordering::Relaxed) + 1;
        Ok(SourceEvidenceReceipt {
            issuer_id: "66666666-6666-4666-8666-666666666666".into(),
            receipt_id: format!("77777777-7777-4777-8777-{sequence:012}"),
            issued_at_utc_ms: self.now,
            expires_at_utc_ms: self.now + lifetime_ms,
            scope_sha256: scope_sha256.into(),
            attestation_sha256: payload_sha256.into(),
        })
    }
}

fn temporary_database(name: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let sequence = SEQUENCE.fetch_add(1, Ordering::Relaxed);
    std::env::temp_dir().join(format!(
        "townlight-materialize-{name}-{nonce}-{sequence}.db"
    ))
}

fn cleanup(path: &Path) {
    for candidate in [
        path.to_path_buf(),
        PathBuf::from(format!("{}-wal", path.display())),
        PathBuf::from(format!("{}-shm", path.display())),
    ] {
        let _ = std::fs::remove_file(candidate);
    }
}

fn seed_authorities(store: &StationStore) {
    let search = SavedSearch {
        search_id: SEARCH.into(),
        name: "Council programs".into(),
        owner_operator_id: OWNER.into(),
        created_at_utc_ms: 1,
        query: SavedSearchQuery {
            all_of: vec![AssetPredicate::TitleContains("Council".into())],
            any_of: vec![],
            order_by: SavedSearchOrder::TitleAscending,
            limit: 8,
        },
        revision: 1,
    };
    let block = DaypartBlock {
        block_id: BLOCK.into(),
        channel_id: CHANNEL.into(),
        label: "Full day".into(),
        starts_local_minute: 0,
        ends_local_minute: 1_440,
        weekday_mask: 0b0111_1111,
        season_starts_on: None,
        season_ends_on: None,
        allow_rule_ids: vec![RULE.into()],
        revision: 1,
    };
    let rule = RecurringRule {
        rule_id: RULE.into(),
        channel_id: CHANNEL.into(),
        saved_search_id: SEARCH.into(),
        saved_search_revision: 1,
        pick_strategy: AssetPickStrategy::Top,
        rolling_window_days: 14,
        avoid_repeat_within_days: None,
        enabled: true,
        priority: 0,
        block_id: BLOCK.into(),
        block_revision: 1,
        weekday_mask: 0b0111_1111,
        interval_weeks: 1,
        anchor_date: LocalDate {
            year: 2026,
            month: 1,
            day: 1,
        },
        timezone: RecurrenceTimeZone::Utc,
        local_start_minute: 0,
        duration_minutes: 60,
        revision: 1,
    };
    store.write_saved_search(&search, None, OWNER, 1).unwrap();
    store.write_daypart_block(&block, None, OWNER, 2).unwrap();
    store.write_recurring_rule(&rule, None, OWNER, 3).unwrap();
}

fn future_midnight(now: i64) -> i64 {
    (now / 86_400_000 + 1) * 86_400_000
}

fn publish_council_asset(store: &StationStore) -> String {
    let asset = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    store
        .put_media_asset(&MediaAsset {
            asset_id: asset.into(),
            media_path: "C:\\station\\council.ts".into(),
            duration_ms: 3_600_000,
            readiness: AssetReadiness::Ready,
        })
        .unwrap();
    let draft = CatalogAssetRecord {
        asset_sha256: asset.into(),
        title: "Council meeting".into(),
        meeting_body: Some("City Council".into()),
        editorial_state: CatalogEditorialState::Draft,
        published_at_utc_ms: None,
        revision: 1,
    };
    store
        .write_catalog_asset(asset, &draft, None, OWNER, 10)
        .unwrap();
    let mut published = draft;
    published.editorial_state = CatalogEditorialState::Published;
    published.published_at_utc_ms = Some(20);
    published.revision = 2;
    store
        .write_catalog_asset(asset, &published, Some(1), OWNER, 20)
        .unwrap();
    asset.into()
}

#[test]
fn zero_outcome_plan_is_durable_and_receipt_randomness_does_not_break_retry() {
    let path = temporary_database("zero");
    let store = StationStore::open(&path).unwrap();
    seed_authorities(&store);
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64;
    let authority = TestAuthority {
        now,
        sequence: AtomicU64::new(0),
    };
    let noon = future_midnight(now) + 12 * 3_600_000;
    let window = MaterializationWindow {
        starts_at_utc_ms: noon,
        ends_at_utc_ms: noon + 3_600_000,
    };
    let first = store
        .materialize_schedule_batch_with_explicit_window(CHANNEL, &window, OWNER, &authority)
        .unwrap();
    let ScheduleMaterializationApplyOutcome::Applied(first) = first else {
        panic!("first materialization must apply")
    };
    assert_eq!(first.plans.len(), 1);
    assert!(first.outcomes.is_empty());
    assert!(
        store
            .read_schedule_materialization_run(&first.run_id)
            .unwrap()
            .is_some()
    );
    let retry = store
        .materialize_schedule_batch_with_explicit_window(CHANNEL, &window, OWNER, &authority)
        .unwrap();
    assert!(matches!(
        retry,
        ScheduleMaterializationApplyOutcome::AlreadyApplied(_)
    ));
    drop(store);
    cleanup(&path);
}

#[test]
fn proposed_occurrence_atomically_creates_one_deterministic_draft_and_history() {
    let path = temporary_database("draft");
    let store = StationStore::open(&path).unwrap();
    seed_authorities(&store);
    let asset = publish_council_asset(&store);
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64;
    let authority = TestAuthority {
        now,
        sequence: AtomicU64::new(0),
    };
    let midnight = future_midnight(now);
    let window = MaterializationWindow {
        starts_at_utc_ms: midnight,
        ends_at_utc_ms: midnight + 2 * 3_600_000,
    };
    let applied = store
        .materialize_schedule_batch_with_explicit_window(CHANNEL, &window, OWNER, &authority)
        .unwrap();
    let ScheduleMaterializationApplyOutcome::Applied(run) = applied else {
        panic!("first materialization must apply")
    };
    let proposed = run
        .outcomes
        .iter()
        .find_map(|record| match &record.outcome {
            ScheduleMaterializationOutcome::Proposed {
                schedule_item_id, ..
            } => Some(schedule_item_id),
            _ => None,
        })
        .unwrap();
    let item = store.read_schedule_item(proposed).unwrap().unwrap();
    assert_eq!(item.asset_id, asset);
    assert_eq!(item.state, ScheduleState::Draft);
    assert_eq!(
        store.list_schedule_item_history(proposed, 8).unwrap().len(),
        1
    );
    drop(store);
    cleanup(&path);
}
