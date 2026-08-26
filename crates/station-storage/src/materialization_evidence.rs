use std::collections::BTreeSet;

use station_schedule_automation::{
    DaypartBlock, MAX_PRIOR_AIRINGS, MaterializationWindow, PriorAiring, RecurringRule,
    SavedSearch, canonical_prior_airings_sha256, materialization_prior_airings_coverage_window,
};

use super::*;

/// Complete, unauthenticated S19 materialization evidence from one SQLite
/// read snapshot. This carries owned authorities so a receipt issuer cannot be
/// accidentally rebound to caller-supplied copies after the read completes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MaterializationEvidenceSnapshot {
    pub search: SavedSearch,
    pub block: DaypartBlock,
    pub rule: RecurringRule,
    pub window: MaterializationWindow,
    pub prior_airings_coverage: MaterializationWindow,
    pub candidate_projection: CatalogCandidateProjection,
    pub prior_airings: Vec<PriorAiring>,
    pub prior_airings_sha256: String,
}

impl StationStore {
    /// Resolves the current recurring rule and all evidence it needs in one
    /// SQLite read transaction. The rule is read first; its exact pinned
    /// historical saved-search and daypart revisions are then loaded and
    /// validated before either catalog or as-run data is consulted.
    ///
    /// `window` is a server-owned materialization request, not an HTTP input.
    /// It is validated here again because it becomes part of both receipts.
    pub fn snapshot_materialization_evidence(
        &self,
        rule_id: &str,
        window: &MaterializationWindow,
    ) -> Result<MaterializationEvidenceSnapshot, String> {
        self.execute("BEGIN")?;
        let outcome = (|| {
            let rule = self
                .read_recurring_rule(rule_id)?
                .ok_or_else(|| "recurring rule authority is unavailable".to_string())?;
            let search = self
                .read_saved_search_revision(&rule.saved_search_id, rule.saved_search_revision)?
                .ok_or_else(|| {
                    "recurring rule historical saved-search authority is missing".to_string()
                })?;
            let block = self
                .read_daypart_block_revision(&rule.block_id, rule.block_revision)?
                .ok_or_else(|| {
                    "recurring rule historical daypart authority is missing".to_string()
                })?;
            rule.validate_against(&search, &block).map_err(|error| {
                format!("recurring rule historical authorities are rebound or invalid: {error}")
            })?;

            let prior_airings_coverage =
                materialization_prior_airings_coverage_window(&rule, window)?;
            let candidate_projection = self.snapshot_catalog_candidates_in_transaction()?;
            let raw_airings = self
                .list_actual_airings_for_materialization(
                    &rule.channel_id,
                    prior_airings_coverage.starts_at_utc_ms,
                    prior_airings_coverage.ends_at_utc_ms,
                )
                .map_err(|error| format!("read prior-airing authority: {error:?}"))?;

            // Raw as-run facts are deliberately lossless. S19's input model
            // is a set of asset/time keys, so only this projection layer
            // removes duplicate facts with the same canonical key.
            let mut prior_keys = BTreeSet::new();
            for fact in raw_airings {
                prior_keys.insert((fact.asset_sha256, fact.aired_at_utc_ms));
            }
            let prior_airings = prior_keys
                .into_iter()
                .map(|(asset_sha256, aired_at_utc_ms)| PriorAiring {
                    asset_sha256,
                    aired_at_utc_ms,
                })
                .collect::<Vec<_>>();
            if prior_airings.len() > MAX_PRIOR_AIRINGS {
                return Err("prior-airings projection exceeds its explicit S19 capacity".into());
            }
            let prior_airings_sha256 = canonical_prior_airings_sha256(&prior_airings)?;

            Ok(MaterializationEvidenceSnapshot {
                search,
                block,
                rule,
                window: window.clone(),
                prior_airings_coverage,
                candidate_projection,
                prior_airings,
                prior_airings_sha256,
            })
        })();
        self.finish_value_transaction(outcome)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use station_schedule_automation::{
        AssetPickStrategy, AssetPredicate, DstApplication, LocalDate, RecurrenceTimeZone,
        SavedSearchOrder, SavedSearchQuery,
    };

    fn authorities() -> (SavedSearch, DaypartBlock, RecurringRule) {
        let search = SavedSearch {
            search_id: "00000000-0000-4000-8000-000000000001".into(),
            name: "Council rotation".into(),
            owner_operator_id: "00000000-0000-4000-8000-000000000002".into(),
            created_at_utc_ms: 1_700_000_000_000,
            query: SavedSearchQuery {
                all_of: vec![AssetPredicate::TitleContains("Council".into())],
                any_of: Vec::new(),
                order_by: SavedSearchOrder::TitleAscending,
                limit: 8,
            },
            revision: 1,
        };
        let block = DaypartBlock {
            block_id: "00000000-0000-4000-8000-000000000003".into(),
            channel_id: "00000000-0000-4000-8000-000000000004".into(),
            label: "Evening".into(),
            starts_local_minute: 1_080,
            ends_local_minute: 1_320,
            weekday_mask: 0b0111_1111,
            season_starts_on: None,
            season_ends_on: None,
            allow_rule_ids: vec!["00000000-0000-4000-8000-000000000005".into()],
            revision: 1,
        };
        let rule = RecurringRule {
            rule_id: "00000000-0000-4000-8000-000000000005".into(),
            channel_id: block.channel_id.clone(),
            saved_search_id: search.search_id.clone(),
            saved_search_revision: 1,
            pick_strategy: AssetPickStrategy::Top,
            rolling_window_days: 14,
            avoid_repeat_within_days: Some(1),
            enabled: true,
            priority: 0,
            block_id: block.block_id.clone(),
            block_revision: 1,
            weekday_mask: 0b0111_1111,
            interval_weeks: 1,
            anchor_date: LocalDate {
                year: 2026,
                month: 1,
                day: 1,
            },
            timezone: RecurrenceTimeZone::Named {
                iana_name: "America/Denver".into(),
                dst: DstApplication::EarlierFold,
            },
            local_start_minute: 1_140,
            duration_minutes: 60,
            revision: 1,
        };
        (search, block, rule)
    }

    fn open_seeded_store() -> (
        StationStore,
        std::path::PathBuf,
        SavedSearch,
        DaypartBlock,
        RecurringRule,
    ) {
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path =
            std::env::temp_dir().join(format!("townlight-materialization-evidence-{nonce}.db"));
        let store = StationStore::open(&path).unwrap();
        let (search, block, rule) = authorities();
        store
            .write_saved_search(&search, None, &search.owner_operator_id, 1)
            .unwrap();
        store
            .write_daypart_block(&block, None, &search.owner_operator_id, 2)
            .unwrap();
        store
            .write_recurring_rule(&rule, None, &search.owner_operator_id, 3)
            .unwrap();
        (store, path, search, block, rule)
    }

    fn recent_window() -> MaterializationWindow {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_millis();
        let now = i64::try_from(now).unwrap();
        MaterializationWindow {
            starts_at_utc_ms: now - 1_000,
            ends_at_utc_ms: now + 1_000,
        }
    }

    fn cleanup(path: &std::path::Path) {
        for candidate in [
            path.to_path_buf(),
            std::path::PathBuf::from(format!("{}-wal", path.display())),
            std::path::PathBuf::from(format!("{}-shm", path.display())),
        ] {
            let _ = std::fs::remove_file(candidate);
        }
    }

    #[test]
    fn one_snapshot_binds_pinned_authorities_and_empty_evidence() {
        let (store, path, search, block, rule) = open_seeded_store();
        let window = recent_window();
        let snapshot = store
            .snapshot_materialization_evidence(&rule.rule_id, &window)
            .unwrap();
        assert_eq!(snapshot.search, search);
        assert_eq!(snapshot.block, block);
        assert_eq!(snapshot.rule, rule);
        assert_eq!(snapshot.window, window);
        assert_eq!(
            snapshot.prior_airings_coverage.starts_at_utc_ms,
            window.starts_at_utc_ms - 86_400_000
        );
        assert!(snapshot.candidate_projection.candidates.is_empty());
        assert!(snapshot.prior_airings.is_empty());
        drop(store);
        cleanup(&path);
    }

    #[test]
    fn tampered_current_rule_fails_closed_before_any_projection_is_issued() {
        let (store, path, _search, _block, rule) = open_seeded_store();
        store.execute("UPDATE schedule_automation_recurring_rules SET channel_id='00000000-0000-4000-8000-000000000006'").unwrap();
        let error = store
            .snapshot_materialization_evidence(&rule.rule_id, &recent_window())
            .unwrap_err();
        assert!(error.contains("indexed authority and JSON disagree"));
        drop(store);
        cleanup(&path);
    }

    #[test]
    fn prior_airings_deduplicate_only_the_s19_projection_beyond_one_ui_page() {
        let (store, path, _search, _block, rule) = open_seeded_store();
        let window = recent_window();
        let aired_at = window.starts_at_utc_ms;
        let asset = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        for index in 0..2_u64 {
            let observation = ActualAiringObservation {
                airing_id: format!("00000000-0000-4000-8000-{:012}", index + 10),
                channel_id: rule.channel_id.clone(),
                asset_sha256: asset.into(),
                schedule_item_id: None,
                schedule_item_revision: None,
                report_id: format!("raw-projection-{index}"),
                aired_at_utc_ms: aired_at,
                media_offset_ms: 0,
                worker_boot_id: "00000000-0000-4000-8000-000000000100".into(),
                worker_sequence: index + 1,
                worker_id: "test-worker".into(),
                worker_ack_id: format!("00000000-0000-4000-8000-{:012}", index + 20),
            };
            store.append_actual_airing(&observation).unwrap();
        }
        let snapshot = store
            .snapshot_materialization_evidence(&rule.rule_id, &window)
            .unwrap();
        assert_eq!(
            snapshot.prior_airings,
            vec![PriorAiring {
                asset_sha256: asset.into(),
                aired_at_utc_ms: aired_at
            }]
        );
        for index in 1_000..1_129_u64 {
            let observation = ActualAiringObservation {
                airing_id: format!("00000000-0000-4000-8000-{index:012}"),
                channel_id: rule.channel_id.clone(),
                asset_sha256: asset.into(),
                schedule_item_id: None,
                schedule_item_revision: None,
                report_id: format!("raw-capacity-{index}"),
                aired_at_utc_ms: aired_at,
                media_offset_ms: 0,
                worker_boot_id: "00000000-0000-4000-8000-000000000100".into(),
                worker_sequence: index + 1,
                worker_id: "test-worker".into(),
                worker_ack_id: format!("00000000-0000-4000-8000-{:012}", index + 10_000),
            };
            store.append_actual_airing(&observation).unwrap();
        }
        let snapshot = store
            .snapshot_materialization_evidence(&rule.rule_id, &window)
            .unwrap();
        assert_eq!(
            snapshot.prior_airings,
            vec![PriorAiring {
                asset_sha256: asset.into(),
                aired_at_utc_ms: aired_at,
            }]
        );
        drop(store);
        cleanup(&path);
    }

    #[test]
    fn materialization_accepts_more_than_one_public_page_of_distinct_airings() {
        let (store, path, _search, _block, rule) = open_seeded_store();
        let window = recent_window();
        for index in 1..=168_u64 {
            let observation = ActualAiringObservation {
                airing_id: format!("00000000-0000-4000-8000-{index:012}"),
                channel_id: rule.channel_id.clone(),
                asset_sha256: format!("{index:064x}"),
                schedule_item_id: None,
                schedule_item_revision: None,
                report_id: format!("distinct-airing-{index}"),
                aired_at_utc_ms: window.starts_at_utc_ms + i64::try_from(index).unwrap(),
                media_offset_ms: 0,
                worker_boot_id: "00000000-0000-4000-8000-000000000100".into(),
                worker_sequence: index,
                worker_id: "test-worker".into(),
                worker_ack_id: format!("00000000-0000-4000-9000-{index:012}"),
            };
            store.append_actual_airing(&observation).unwrap();
        }

        let snapshot = store
            .snapshot_materialization_evidence(&rule.rule_id, &window)
            .unwrap();
        assert_eq!(snapshot.prior_airings.len(), 168);
        assert_eq!(
            snapshot.prior_airings.first().unwrap().asset_sha256,
            format!("{:064x}", 1)
        );
        assert_eq!(
            snapshot.prior_airings.last().unwrap().asset_sha256,
            format!("{:064x}", 168)
        );

        drop(store);
        cleanup(&path);
    }
}
