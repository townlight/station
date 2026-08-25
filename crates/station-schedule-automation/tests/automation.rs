use station_schedule_automation::{
    ArbitrationOutcome, ArbitrationPrecedence, AssetCandidate, AssetPickStrategy, AssetPredicate,
    AssetResolution, AuthenticatedCandidateProjection, AuthenticatedMaterializationInputs,
    AuthenticatedPriorAirings, BatchMaterializationRequest, CustomMetadataPredicate, DaypartBlock,
    DstApplication, LocalDate, MaterializationAuthorities, MaterializationOutcome,
    MaterializationPlan, MaterializationWindow, MetadataValue, PriorAiring, RecurrenceTimeZone,
    RecurringRule, RuleSlotCandidate, SavedSearch, SavedSearchOrder, SavedSearchQuery,
    SlotMaterializationOutcome, SourceEvidenceKind, SourceEvidenceReceipt, SourceEvidenceVerifier,
    arbitrate_rule_slots, canonical_candidate_projection_bytes, canonical_intent_bytes,
    canonical_source_bytes, derive_arbitrated_materialization_batch, derive_materialization_intent,
    materialization_evidence_scope_sha256, materialization_prior_airings_coverage_window,
    resolve_asset,
};
use std::collections::BTreeMap;

const OBSERVED_AT_UTC_MS: i64 = 1_767_225_600_000;

fn id(suffix: &str) -> String {
    format!("00000000-0000-4000-8000-0000000000{suffix}")
}
fn sha(character: char) -> String {
    character.to_string().repeat(64)
}
fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}
fn search() -> SavedSearch {
    SavedSearch {
        search_id: id("01"),
        name: "Council meeting rotation".into(),
        owner_operator_id: id("03"),
        created_at_utc_ms: 1_700_000_000_000,
        revision: 1,
        query: SavedSearchQuery {
            all_of: vec![AssetPredicate::Custom {
                field: "topic".into(),
                predicate: CustomMetadataPredicate::Equals(MetadataValue::Choice("council".into())),
            }],
            any_of: vec![],
            order_by: SavedSearchOrder::TitleAscending,
            limit: 8,
        },
    }
}
fn block() -> DaypartBlock {
    DaypartBlock {
        block_id: id("04"),
        channel_id: id("02"),
        label: "Weeknight civic".into(),
        starts_local_minute: 1_080,
        ends_local_minute: 1_320,
        weekday_mask: 0b0111_1111,
        season_starts_on: None,
        season_ends_on: None,
        allow_rule_ids: vec![],
        revision: 1,
    }
}
fn rule() -> RecurringRule {
    RecurringRule {
        rule_id: id("05"),
        channel_id: id("02"),
        saved_search_id: id("01"),
        saved_search_revision: 1,
        pick_strategy: AssetPickStrategy::Top,
        rolling_window_days: 14,
        avoid_repeat_within_days: None,
        enabled: true,
        priority: 0,
        block_id: id("04"),
        block_revision: 1,
        weekday_mask: 0b0000_1000,
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
    }
}
fn window() -> MaterializationWindow {
    MaterializationWindow {
        starts_at_utc_ms: 1_767_225_600_000,
        ends_at_utc_ms: 1_768_435_200_000,
    }
}
fn candidate(letter: char, title: &str, published_at_utc_ms: i64) -> AssetCandidate {
    AssetCandidate {
        asset_sha256: sha(letter),
        title: title.into(),
        meeting_body: Some("City Council".into()),
        duration_ms: 3_600_000,
        ready: true,
        published: true,
        published_at_utc_ms,
        custom_metadata_authority_sha256: sha('e'),
        indexed_custom_metadata: BTreeMap::from([(
            "topic".into(),
            MetadataValue::Choice("council".into()),
        )]),
    }
}

#[test]
fn saved_searches_are_station_agnostic_and_keep_owner_and_creation_immutable() {
    let current = search();
    let mut renamed = current.clone();
    renamed.name = "Council meeting archive".into();
    renamed.revision = 2;
    assert!(
        renamed
            .validate_successor_of(&current, current.revision)
            .is_ok()
    );

    let mut rebound_owner = renamed.clone();
    rebound_owner.owner_operator_id = id("09");
    assert!(
        rebound_owner
            .validate_successor_of(&current, current.revision)
            .is_err()
    );
    let mut rebound_created = renamed;
    rebound_created.created_at_utc_ms += 1;
    assert!(
        rebound_created
            .validate_successor_of(&current, current.revision)
            .is_err()
    );

    let mut other_channel_rule = rule();
    other_channel_rule.channel_id = id("09");
    let mut matching_block = block();
    matching_block.channel_id = id("09");
    assert!(
        other_channel_rule
            .validate_against(&current, &matching_block)
            .is_ok()
    );
}

#[test]
fn query_predicates_match_fixed_and_custom_typed_metadata_without_missing_false_matches() {
    let mut candidate = candidate('a', "Annual CAFÉ hearing", 1_700_000_000_000);
    candidate.meeting_body = Some("City Council".into());
    candidate.duration_ms = 3_600_000;
    candidate
        .indexed_custom_metadata
        .insert("priority".into(), MetadataValue::Number(7));
    candidate.indexed_custom_metadata.insert(
        "recorded".into(),
        MetadataValue::DateTimeUnixMs(1_700_000_000_000),
    );
    candidate
        .indexed_custom_metadata
        .insert("published_flag".into(), MetadataValue::Boolean(false));
    candidate
        .indexed_custom_metadata
        .insert("notes".into(), MetadataValue::Text("CAFÉ agenda".into()));
    let mut fixed = search();
    fixed.query.all_of = vec![
        AssetPredicate::TitleContains("cafÉ".into()),
        AssetPredicate::MeetingBodyEquals("City Council".into()),
        AssetPredicate::DurationMs {
            min_inclusive: Some(3_600_000),
            max_inclusive: Some(3_600_000),
        },
        AssetPredicate::PublishedAtUtcMs {
            starts_at_inclusive: Some(1_700_000_000_000),
            ends_before_exclusive: Some(1_700_000_000_001),
        },
        AssetPredicate::Custom {
            field: "priority".into(),
            predicate: CustomMetadataPredicate::NumberRange {
                min_inclusive: Some(7),
                max_inclusive: Some(7),
            },
        },
        AssetPredicate::Custom {
            field: "recorded".into(),
            predicate: CustomMetadataPredicate::DateTimeRange {
                min_inclusive: Some(1_700_000_000_000),
                max_inclusive: Some(1_700_000_000_000),
            },
        },
        AssetPredicate::Custom {
            field: "published_flag".into(),
            predicate: CustomMetadataPredicate::BooleanEquals(false),
        },
        AssetPredicate::Custom {
            field: "notes".into(),
            predicate: CustomMetadataPredicate::Contains("café".into()),
        },
    ];
    assert_eq!(
        resolve_asset(
            &fixed,
            &rule(),
            &[candidate.clone()],
            1_767_319_200_000,
            &[]
        )
        .unwrap(),
        AssetResolution::Selected(candidate.clone())
    );
    let mut at_exclusive_published_end = candidate.clone();
    at_exclusive_published_end.published_at_utc_ms = 1_700_000_000_001;
    assert_eq!(
        resolve_asset(
            &fixed,
            &rule(),
            &[at_exclusive_published_end],
            1_767_319_200_000,
            &[],
        )
        .unwrap(),
        AssetResolution::NoMatch
    );
    let mut missing = fixed;
    missing.query.all_of = vec![AssetPredicate::Custom {
        field: "not_present".into(),
        predicate: CustomMetadataPredicate::BooleanEquals(false),
    }];
    assert_eq!(
        resolve_asset(&missing, &rule(), &[candidate], 1_767_319_200_000, &[]).unwrap(),
        AssetResolution::NoMatch
    );
    let mut boolean_as_equality = search();
    boolean_as_equality.query.all_of = vec![AssetPredicate::Custom {
        field: "published_flag".into(),
        predicate: CustomMetadataPredicate::Equals(MetadataValue::Boolean(false)),
    }];
    assert!(boolean_as_equality.validate().is_err());
}

#[test]
fn query_canonicalization_is_order_independent_but_rejects_duplicates_and_preserves_pick_after_limit()
 {
    let mut first = search();
    first.query.all_of = vec![
        AssetPredicate::TitleContains("council".into()),
        AssetPredicate::Custom {
            field: "topic".into(),
            predicate: CustomMetadataPredicate::Equals(MetadataValue::Choice("council".into())),
        },
    ];
    let mut reordered = first.clone();
    reordered.query.all_of.reverse();
    assert_eq!(
        canonical_source_bytes(&first, &block(), &rule()).unwrap(),
        canonical_source_bytes(&reordered, &block(), &rule()).unwrap()
    );
    let mut duplicate = first.clone();
    duplicate.query.any_of = vec![
        AssetPredicate::TitleContains("council".into()),
        AssetPredicate::TitleContains("council".into()),
    ];
    assert!(duplicate.validate().is_err());

    let mut ordered = search();
    ordered.query.all_of = vec![AssetPredicate::TitleContains("council".into())];
    ordered.query.limit = 1;
    let alpha = candidate('a', "Alpha council", 1_700_000_000_000);
    let beta = candidate('b', "Beta council", 1_760_000_000_000);
    let mut newest = rule();
    newest.pick_strategy = AssetPickStrategy::Newest;
    assert_eq!(
        resolve_asset(
            &ordered,
            &newest,
            &[beta, alpha.clone()],
            1_767_319_200_000,
            &[]
        )
        .unwrap(),
        AssetResolution::Selected(alpha)
    );
}
fn plan(outcome: MaterializationOutcome) -> MaterializationPlan {
    match outcome {
        MaterializationOutcome::Plan(plan) => plan,
    }
}

struct TestEvidenceVerifier;

impl SourceEvidenceVerifier for TestEvidenceVerifier {
    fn verify(
        &self,
        _kind: SourceEvidenceKind,
        receipt: &SourceEvidenceReceipt,
        scope_sha256: &str,
        _payload_sha256: &str,
        observed_at_utc_ms: i64,
    ) -> Result<(), String> {
        if receipt.attestation_sha256 != sha('f')
            || receipt.scope_sha256 != scope_sha256
            || observed_at_utc_ms != OBSERVED_AT_UTC_MS
        {
            return Err("test receipt was not authenticated for its scope".into());
        }
        Ok(())
    }
}

fn inputs(
    search: &SavedSearch,
    block: &DaypartBlock,
    rule: &RecurringRule,
    window: &MaterializationWindow,
    candidates: Vec<AssetCandidate>,
    airings: Vec<PriorAiring>,
) -> AuthenticatedMaterializationInputs {
    let receipt = |kind, suffix| SourceEvidenceReceipt {
        issuer_id: id("06"),
        receipt_id: id(suffix),
        issued_at_utc_ms: OBSERVED_AT_UTC_MS - 1_000,
        expires_at_utc_ms: OBSERVED_AT_UTC_MS + 60_000,
        scope_sha256: materialization_evidence_scope_sha256(search, block, rule, window, kind)
            .unwrap(),
        attestation_sha256: sha('f'),
    };
    AuthenticatedMaterializationInputs {
        candidate_projection: AuthenticatedCandidateProjection {
            receipt: receipt(SourceEvidenceKind::CandidateProjection, "07"),
            candidates,
        },
        prior_airings: AuthenticatedPriorAirings {
            receipt: receipt(SourceEvidenceKind::PriorAirings, "08"),
            airings,
        },
    }
}

fn derive(
    search: &SavedSearch,
    block: &DaypartBlock,
    rule: &RecurringRule,
    window: MaterializationWindow,
    candidates: Vec<AssetCandidate>,
    airings: Vec<PriorAiring>,
) -> Result<MaterializationOutcome, String> {
    let inputs = inputs(search, block, rule, &window, candidates, airings);
    derive_materialization_intent(
        search,
        block,
        rule,
        window,
        OBSERVED_AT_UTC_MS,
        &inputs,
        &TestEvidenceVerifier,
    )
}

#[test]
fn resolver_filters_typed_metadata_and_rejects_invalid_candidate_input() {
    let valid = candidate('a', "Alpha council", 1_700_000_000_000);
    let mut invalid = candidate('b', "Broken", 1_700_000_000_000);
    invalid.asset_sha256 = sha('g');
    assert!(
        resolve_asset(
            &search(),
            &rule(),
            &[valid, invalid],
            1_767_319_200_000,
            &[]
        )
        .is_err()
    );
    let mut off_query = candidate('c', "Not council", 1_700_000_000_000);
    off_query.indexed_custom_metadata.clear();
    assert_eq!(
        resolve_asset(&search(), &rule(), &[off_query], 1_767_319_200_000, &[]).unwrap(),
        AssetResolution::NoMatch
    );
}

#[test]
fn candidate_evidence_binds_metadata_authority_and_preserves_s22_value_bounds() {
    let mut first = candidate('a', "Alpha council", 1_700_000_000_000);
    first
        .indexed_custom_metadata
        .insert("long_notes".into(), MetadataValue::Text("x".repeat(2_048)));
    first
        .indexed_custom_metadata
        .insert("historical_date".into(), MetadataValue::DateTimeUnixMs(-1));
    assert!(first.validate().is_ok());

    let first_bytes = canonical_candidate_projection_bytes(&[first.clone()]).unwrap();
    let mut other_authority = first.clone();
    other_authority.custom_metadata_authority_sha256 = sha('d');
    let other_bytes = canonical_candidate_projection_bytes(&[other_authority]).unwrap();
    assert_ne!(first_bytes, other_bytes);

    let mut invalid_authority = first.clone();
    invalid_authority.custom_metadata_authority_sha256 = sha('g');
    assert!(invalid_authority.validate().is_err());
    let mut oversized = first;
    oversized
        .indexed_custom_metadata
        .insert("long_notes".into(), MetadataValue::Text("x".repeat(2_049)));
    assert!(oversized.validate().is_err());
}

#[test]
fn resolver_fails_closed_on_invalid_occurrence_and_future_airing_evidence() {
    let current_search = search();
    let current_rule = rule();
    let projected = candidate('a', "Alpha council", 0);
    assert!(
        resolve_asset(
            &current_search,
            &current_rule,
            std::slice::from_ref(&projected),
            -1,
            &[]
        )
        .is_err()
    );

    let mut avoiding = current_rule;
    avoiding.avoid_repeat_within_days = Some(60);
    assert_eq!(
        resolve_asset(
            &current_search,
            &avoiding,
            std::slice::from_ref(&projected),
            0,
            &[],
        )
        .unwrap(),
        AssetResolution::Selected(projected.clone())
    );
    assert!(
        resolve_asset(
            &current_search,
            &avoiding,
            &[projected],
            1_000,
            &[PriorAiring {
                asset_sha256: sha('a'),
                aired_at_utc_ms: 1_001,
            }],
        )
        .is_err()
    );
}

#[test]
fn prior_airings_receipts_cover_the_exact_avoid_repeat_lookback() {
    let current_search = search();
    let current_block = block();
    let mut current_rule = rule();
    current_rule.avoid_repeat_within_days = Some(14);
    let current_window = window();
    let coverage =
        materialization_prior_airings_coverage_window(&current_rule, &current_window).unwrap();
    assert_eq!(
        coverage.starts_at_utc_ms,
        current_window.starts_at_utc_ms - 14 * 86_400_000
    );
    assert_eq!(coverage.ends_at_utc_ms, current_window.ends_at_utc_ms);

    let authenticated = inputs(
        &current_search,
        &current_block,
        &current_rule,
        &current_window,
        vec![candidate('a', "Alpha council", 1_700_000_000_000)],
        vec![],
    );
    let plan = plan(
        derive_materialization_intent(
            &current_search,
            &current_block,
            &current_rule,
            current_window,
            OBSERVED_AT_UTC_MS,
            &authenticated,
            &TestEvidenceVerifier,
        )
        .unwrap(),
    );
    assert_eq!(plan.source_input_binding.prior_airings_coverage, coverage);
}

#[test]
fn final_slot_that_would_cross_window_end_is_excluded() {
    let current_window = MaterializationWindow {
        starts_at_utc_ms: 1_767_225_600_000,
        ends_at_utc_ms: 1_767_319_800_000,
    };
    let plan = plan(
        derive(
            &search(),
            &block(),
            &rule(),
            current_window,
            vec![candidate('a', "Alpha council", 1_700_000_000_000)],
            vec![],
        )
        .unwrap(),
    );
    assert!(plan.draft_intent.is_none());
    assert!(plan.outcomes.is_empty());
}

#[test]
fn picks_and_avoid_repeat_are_deterministic() {
    let alpha = candidate('a', "Alpha council", 1_700_000_000_000);
    let beta = candidate('b', "Beta council", 1_760_000_000_000);
    let candidates = vec![beta.clone(), alpha.clone()];
    assert_eq!(
        resolve_asset(&search(), &rule(), &candidates, 1_767_319_200_000, &[]).unwrap(),
        AssetResolution::Selected(alpha.clone())
    );
    let mut newest = rule();
    newest.pick_strategy = AssetPickStrategy::Newest;
    assert_eq!(
        resolve_asset(&search(), &newest, &candidates, 1_767_319_200_000, &[]).unwrap(),
        AssetResolution::Selected(beta.clone())
    );
    let mut least = rule();
    least.pick_strategy = AssetPickStrategy::LeastRecentlyAired;
    assert_eq!(
        resolve_asset(
            &search(),
            &least,
            &candidates,
            1_767_319_200_000,
            &[PriorAiring {
                asset_sha256: sha('a'),
                aired_at_utc_ms: 1_760_000_000_000
            }]
        )
        .unwrap(),
        AssetResolution::Selected(beta.clone())
    );
    let mut random = rule();
    random.pick_strategy = AssetPickStrategy::Random;
    assert_eq!(
        resolve_asset(&search(), &random, &candidates, 1_767_319_200_000, &[]).unwrap(),
        resolve_asset(&search(), &random, &candidates, 1_767_319_200_000, &[]).unwrap()
    );
    let mut avoiding = rule();
    avoiding.avoid_repeat_within_days = Some(14);
    assert_eq!(
        resolve_asset(
            &search(),
            &avoiding,
            &candidates,
            1_767_319_200_000,
            &[PriorAiring {
                asset_sha256: sha('a'),
                aired_at_utc_ms: 1_767_000_000_000
            }]
        )
        .unwrap(),
        AssetResolution::Selected(beta)
    );
}

#[test]
fn per_slot_resolution_emits_stable_draft_occurrences() {
    let current_search = search();
    let current_block = block();
    let current_rule = rule();
    let first = plan(
        derive(
            &current_search,
            &current_block,
            &current_rule,
            window(),
            vec![candidate('a', "Alpha council", 1_700_000_000_000)],
            vec![],
        )
        .unwrap(),
    );
    let shifted = plan(
        derive(
            &current_search,
            &current_block,
            &current_rule,
            MaterializationWindow {
                starts_at_utc_ms: 1_767_300_000_000,
                ends_at_utc_ms: 1_768_435_200_000,
            },
            vec![candidate('a', "Alpha council", 1_700_000_000_000)],
            vec![],
        )
        .unwrap(),
    );
    let first_proposed = first
        .outcomes
        .iter()
        .find_map(|outcome| match outcome {
            SlotMaterializationOutcome::Proposed(occurrence) => Some(occurrence),
            _ => None,
        })
        .unwrap();
    let shifted_proposed = shifted
        .outcomes
        .iter()
        .find_map(|outcome| match outcome {
            SlotMaterializationOutcome::Proposed(occurrence) => Some(occurrence),
            _ => None,
        })
        .unwrap();
    assert_eq!(first_proposed.occurrence_id, shifted_proposed.occurrence_id);
    assert_eq!(first_proposed.asset_sha256, sha('a'));
}

#[test]
fn no_match_disabled_and_seasonal_blocks_are_explicit() {
    let current_search = search();
    let current_block = block();
    let current_rule = rule();
    assert!(matches!(
        derive(&current_search, &current_block, &current_rule, window(), vec![], vec![]).unwrap(),
        MaterializationOutcome::Plan(plan) if plan.outcomes.iter().all(|outcome| matches!(outcome, SlotMaterializationOutcome::NoMatch { .. }))
    ));
    let mut disabled = rule();
    disabled.enabled = false;
    assert!(matches!(
        derive(
            &current_search,
            &current_block,
            &disabled,
            window(),
            vec![candidate('a', "Alpha council", 1_700_000_000_000)],
            vec![]
        )
        .unwrap(),
        MaterializationOutcome::Plan(plan) if plan.outcomes.iter().all(|outcome| matches!(outcome, SlotMaterializationOutcome::Blocked { .. }))
    ));
    let mut seasonal = block();
    seasonal.season_starts_on = Some(LocalDate {
        year: 2026,
        month: 2,
        day: 1,
    });
    seasonal.season_ends_on = Some(LocalDate {
        year: 2026,
        month: 2,
        day: 28,
    });
    assert!(matches!(
        derive(
            &current_search,
            &seasonal,
            &current_rule,
            window(),
            vec![candidate('a', "Alpha council", 1_700_000_000_000)],
            vec![]
        )
        .unwrap(),
        MaterializationOutcome::Plan(plan) if plan.outcomes.iter().all(|outcome| matches!(outcome, SlotMaterializationOutcome::Blocked { .. }))
    ));
}

#[test]
fn mixed_season_window_keeps_proposed_and_blocked_slots() {
    let current_search = search();
    let current_rule = rule();
    let mut seasonal = block();
    seasonal.season_starts_on = Some(LocalDate {
        year: 2026,
        month: 1,
        day: 8,
    });
    seasonal.season_ends_on = Some(LocalDate {
        year: 2026,
        month: 1,
        day: 31,
    });
    let plan = plan(
        derive(
            &current_search,
            &seasonal,
            &current_rule,
            window(),
            vec![candidate('a', "Alpha council", 1_700_000_000_000)],
            vec![],
        )
        .unwrap(),
    );
    assert!(
        plan.outcomes
            .iter()
            .any(|outcome| matches!(outcome, SlotMaterializationOutcome::Blocked { .. }))
    );
    assert!(
        plan.outcomes
            .iter()
            .any(|outcome| matches!(outcome, SlotMaterializationOutcome::Proposed(_)))
    );
    assert!(plan.draft_intent.is_some());
}

#[test]
fn v3_source_and_v3_intent_domains_are_explicit() {
    let source = canonical_source_bytes(&search(), &block(), &rule()).unwrap();
    let current_search = search();
    let current_block = block();
    let current_rule = rule();
    let plan = plan(
        derive(
            &current_search,
            &current_block,
            &current_rule,
            window(),
            vec![candidate('a', "Alpha council", 1_700_000_000_000)],
            vec![],
        )
        .unwrap(),
    );
    let intent = plan.draft_intent.unwrap();
    assert_eq!(
        hex(&source),
        "1c00000000000000746f776e6c696768742e7363686564756c652d736f757263652e76330300240000000000000030303030303030302d303030302d343030302d383030302d3030303030303030303030311800000000000000436f756e63696c206d656574696e6720726f746174696f6e240000000000000030303030303030302d303030302d343030302d383030302d3030303030303030303030330068e5cf8b0100000100000000000000000800010000001f00000000000000040500000000000000746f70696300040700000000000000636f756e63696c00000000240000000000000030303030303030302d303030302d343030302d383030302d303030303030303030303034240000000000000030303030303030302d303030302d343030302d383030302d3030303030303030303030320f000000000000005765656b6e69676874206369766963380428057f00000000000100000000000000240000000000000030303030303030302d303030302d343030302d383030302d303030303030303030303035240000000000000030303030303030302d303030302d343030302d383030302d303030303030303030303032240000000000000030303030303030302d303030302d343030302d383030302d303030303030303030303031240000000000000030303030303030302d303030302d343030302d383030302d30303030303030303030303401000000000000000100000000000000000e000100000801ea07010105000000000000006e616d65640e00000000000000416d65726963612f44656e76657207000000000000006561726c69657274043c000100000000000000"
    );
    assert!(
        source
            .windows(b"townlight.schedule-source.v3".len())
            .any(|part| part == b"townlight.schedule-source.v3")
    );
    assert!(
        canonical_intent_bytes(&intent)
            .windows(b"townlight.materialization-intent.v3".len())
            .any(|part| part == b"townlight.materialization-intent.v3")
    );
    assert!(intent.source_input_binding.combined_sha256.len() == 64);
}

#[test]
fn materialization_requires_two_authenticated_scoped_projections_even_when_empty() {
    struct RejectingVerifier;
    impl SourceEvidenceVerifier for RejectingVerifier {
        fn verify(
            &self,
            _kind: SourceEvidenceKind,
            _receipt: &SourceEvidenceReceipt,
            _scope_sha256: &str,
            _payload_sha256: &str,
            _observed_at_utc_ms: i64,
        ) -> Result<(), String> {
            Err("missing authoritative projection receipt".into())
        }
    }

    let current_search = search();
    let current_block = block();
    let current_rule = rule();
    let current_window = window();
    let empty = inputs(
        &current_search,
        &current_block,
        &current_rule,
        &current_window,
        vec![],
        vec![],
    );
    assert!(
        derive_materialization_intent(
            &current_search,
            &current_block,
            &current_rule,
            current_window,
            OBSERVED_AT_UTC_MS,
            &empty,
            &RejectingVerifier,
        )
        .is_err()
    );
}

#[test]
fn source_receipts_must_be_current_and_short_lived() {
    let current_search = search();
    let current_block = block();
    let current_rule = rule();
    let current_window = window();
    let baseline = inputs(
        &current_search,
        &current_block,
        &current_rule,
        &current_window,
        vec![],
        vec![],
    );
    for changed in [
        {
            let mut changed = baseline.clone();
            changed.candidate_projection.receipt.expires_at_utc_ms = OBSERVED_AT_UTC_MS;
            changed
        },
        {
            let mut changed = baseline.clone();
            changed.candidate_projection.receipt.issued_at_utc_ms = OBSERVED_AT_UTC_MS + 1;
            changed.candidate_projection.receipt.expires_at_utc_ms = OBSERVED_AT_UTC_MS + 60_000;
            changed
        },
        {
            let mut changed = baseline.clone();
            changed.candidate_projection.receipt.issued_at_utc_ms = OBSERVED_AT_UTC_MS - 1;
            changed.candidate_projection.receipt.expires_at_utc_ms = OBSERVED_AT_UTC_MS + 300_000;
            changed
        },
    ] {
        assert!(
            derive_materialization_intent(
                &current_search,
                &current_block,
                &current_rule,
                current_window.clone(),
                OBSERVED_AT_UTC_MS,
                &changed,
                &TestEvidenceVerifier,
            )
            .is_err()
        );
    }
}

#[test]
fn source_evidence_is_bound_to_exact_payload_receipt_and_materialization_scope() {
    struct ExactPayloadVerifier {
        candidate_projection_sha256: String,
        prior_airings_sha256: String,
    }
    impl SourceEvidenceVerifier for ExactPayloadVerifier {
        fn verify(
            &self,
            kind: SourceEvidenceKind,
            receipt: &SourceEvidenceReceipt,
            scope_sha256: &str,
            payload_sha256: &str,
            observed_at_utc_ms: i64,
        ) -> Result<(), String> {
            let expected = match kind {
                SourceEvidenceKind::CandidateProjection => &self.candidate_projection_sha256,
                SourceEvidenceKind::PriorAirings => &self.prior_airings_sha256,
            };
            if receipt.attestation_sha256 == sha('f')
                && receipt.scope_sha256 == scope_sha256
                && payload_sha256 == expected
                && observed_at_utc_ms == OBSERVED_AT_UTC_MS
            {
                Ok(())
            } else {
                Err("receipt payload binding was not authenticated".into())
            }
        }
    }

    let current_search = search();
    let current_block = block();
    let current_rule = rule();
    let current_window = window();
    let valid = inputs(
        &current_search,
        &current_block,
        &current_rule,
        &current_window,
        vec![candidate('a', "Alpha council", 1_700_000_000_000)],
        vec![],
    );
    let first = plan(
        derive_materialization_intent(
            &current_search,
            &current_block,
            &current_rule,
            current_window.clone(),
            OBSERVED_AT_UTC_MS,
            &valid,
            &TestEvidenceVerifier,
        )
        .unwrap(),
    );
    let exact_verifier = ExactPayloadVerifier {
        candidate_projection_sha256: first
            .source_input_binding
            .candidate_projection_sha256
            .clone(),
        prior_airings_sha256: first.source_input_binding.prior_airings_sha256.clone(),
    };
    assert!(
        derive_materialization_intent(
            &current_search,
            &current_block,
            &current_rule,
            current_window.clone(),
            OBSERVED_AT_UTC_MS,
            &valid,
            &exact_verifier,
        )
        .is_ok()
    );
    let mut changed = valid.clone();
    changed.candidate_projection.candidates[0].title = "Changed council".into();
    assert!(
        derive_materialization_intent(
            &current_search,
            &current_block,
            &current_rule,
            current_window.clone(),
            OBSERVED_AT_UTC_MS,
            &changed,
            &exact_verifier,
        )
        .is_err()
    );
    let changed_plan = plan(
        derive_materialization_intent(
            &current_search,
            &current_block,
            &current_rule,
            current_window.clone(),
            OBSERVED_AT_UTC_MS,
            &changed,
            &TestEvidenceVerifier,
        )
        .unwrap(),
    );
    assert_ne!(
        first.source_input_binding.candidate_projection_sha256,
        changed_plan
            .source_input_binding
            .candidate_projection_sha256
    );
    let first_occurrence = first.draft_intent.unwrap().occurrence_ids();
    let changed_occurrence = changed_plan.draft_intent.unwrap().occurrence_ids();
    assert_eq!(first_occurrence, changed_occurrence);

    let mut rebound = valid;
    rebound.candidate_projection.receipt.scope_sha256 = sha('0');
    assert!(
        derive_materialization_intent(
            &current_search,
            &current_block,
            &current_rule,
            current_window,
            OBSERVED_AT_UTC_MS,
            &rebound,
            &TestEvidenceVerifier,
        )
        .is_err()
    );
}

#[test]
fn source_projection_order_and_receipt_independence_fail_closed() {
    let current_search = search();
    let current_block = block();
    let current_rule = rule();
    let current_window = window();
    let mut unordered = inputs(
        &current_search,
        &current_block,
        &current_rule,
        &current_window,
        vec![
            candidate('b', "Beta council", 1_700_000_000_000),
            candidate('a', "Alpha council", 1_700_000_000_000),
        ],
        vec![],
    );
    assert!(
        derive_materialization_intent(
            &current_search,
            &current_block,
            &current_rule,
            current_window.clone(),
            OBSERVED_AT_UTC_MS,
            &unordered,
            &TestEvidenceVerifier,
        )
        .is_err()
    );
    unordered.candidate_projection.candidates.reverse();
    unordered.prior_airings.receipt.receipt_id =
        unordered.candidate_projection.receipt.receipt_id.clone();
    assert!(
        derive_materialization_intent(
            &current_search,
            &current_block,
            &current_rule,
            current_window,
            OBSERVED_AT_UTC_MS,
            &unordered,
            &TestEvidenceVerifier,
        )
        .is_err()
    );
}

#[test]
fn horizon_and_block_allow_list_fail_closed_at_boundaries() {
    let candidate = candidate('a', "Alpha council", 1_700_000_000_000);
    let current_search = search();
    let current_block = block();
    let mut fourteen = rule();
    fourteen.rolling_window_days = 14;
    let within = MaterializationWindow {
        starts_at_utc_ms: 1_767_225_600_000,
        ends_at_utc_ms: 1_768_435_200_000,
    };
    assert!(
        derive(
            &current_search,
            &current_block,
            &fourteen,
            within,
            vec![candidate.clone()],
            vec![],
        )
        .is_ok()
    );
    let past = MaterializationWindow {
        starts_at_utc_ms: 1_767_225_600_000,
        ends_at_utc_ms: 1_768_435_200_001,
    };
    assert!(
        derive(
            &current_search,
            &current_block,
            &fourteen,
            past,
            vec![candidate.clone()],
            vec![],
        )
        .is_err()
    );
    let mut sixty = rule();
    sixty.rolling_window_days = 60;
    let sixty_window = MaterializationWindow {
        starts_at_utc_ms: 1_767_225_600_000,
        ends_at_utc_ms: 1_772_409_600_000,
    };
    assert!(
        derive(
            &current_search,
            &current_block,
            &sixty,
            sixty_window,
            vec![candidate.clone()],
            vec![],
        )
        .is_ok()
    );
    let mut denied = block();
    denied.allow_rule_ids = vec![id("06")];
    assert!(
        materialization_evidence_scope_sha256(
            &current_search,
            &denied,
            &rule(),
            &window(),
            SourceEvidenceKind::CandidateProjection,
        )
        .is_err()
    );
}

#[test]
fn overlap_arbitration_uses_priority_then_rule_id() {
    let first = RuleSlotCandidate {
        rule_id: id("05"),
        channel_id: id("02"),
        priority: 3,
        starts_at_utc_ms: 100,
        ends_at_utc_ms: 200,
    };
    let higher = RuleSlotCandidate {
        rule_id: id("06"),
        channel_id: id("02"),
        priority: 4,
        starts_at_utc_ms: 150,
        ends_at_utc_ms: 250,
    };
    let outcome = arbitrate_rule_slots(&[first.clone(), higher.clone()]).unwrap();
    assert!(
        matches!(&outcome[0], ArbitrationOutcome::Winner { rule_id, .. } if rule_id == &higher.rule_id)
    );
    assert!(matches!(
        &outcome[1],
        ArbitrationOutcome::SuppressedByPrecedence {
            rule_id,
            winner_rule_id,
            candidate_priority,
            winner_priority,
            reason: ArbitrationPrecedence::HigherPriority,
            ..
        } if rule_id == &first.rule_id
            && winner_rule_id == &higher.rule_id
            && candidate_priority == &first.priority
            && winner_priority == &higher.priority
    ));
    let later_id = RuleSlotCandidate {
        rule_id: id("07"),
        channel_id: id("02"),
        priority: 3,
        starts_at_utc_ms: 150,
        ends_at_utc_ms: 250,
    };
    let tie = arbitrate_rule_slots(&[later_id, first.clone()]).unwrap();
    assert!(
        matches!(&tie[0], ArbitrationOutcome::Winner { rule_id, .. } if rule_id == &first.rule_id)
    );
    assert!(matches!(
        &tie[1],
        ArbitrationOutcome::SuppressedByPrecedence {
            reason: ArbitrationPrecedence::RuleIdTieBreak,
            candidate_priority,
            winner_priority,
            ..
        } if candidate_priority == winner_priority
    ));

    let other_channel = RuleSlotCandidate {
        rule_id: id("08"),
        channel_id: id("09"),
        priority: 100,
        starts_at_utc_ms: 100,
        ends_at_utc_ms: 250,
    };
    let independent = arbitrate_rule_slots(&[first, other_channel]).unwrap();
    assert!(
        independent
            .iter()
            .all(|outcome| matches!(outcome, ArbitrationOutcome::Winner { .. }))
    );
}

#[test]
fn authenticated_batch_suppresses_same_channel_lower_priority_drafts() {
    let current_search = search();
    let current_block = block();
    let mut higher = rule();
    higher.priority = 4;
    let mut lower = rule();
    lower.rule_id = id("06");
    lower.priority = 3;
    let current_window = window();
    let higher_inputs = inputs(
        &current_search,
        &current_block,
        &higher,
        &current_window,
        vec![candidate('a', "Alpha council", 1_700_000_000_000)],
        vec![],
    );
    let lower_inputs = inputs(
        &current_search,
        &current_block,
        &lower,
        &current_window,
        vec![candidate('a', "Alpha council", 1_700_000_000_000)],
        vec![],
    );
    let batch = derive_arbitrated_materialization_batch(
        &[
            BatchMaterializationRequest {
                authorities: MaterializationAuthorities {
                    search: &current_search,
                    block: &current_block,
                    rule: &higher,
                },
                inputs: &higher_inputs,
            },
            BatchMaterializationRequest {
                authorities: MaterializationAuthorities {
                    search: &current_search,
                    block: &current_block,
                    rule: &lower,
                },
                inputs: &lower_inputs,
            },
        ],
        current_window,
        OBSERVED_AT_UTC_MS,
        &TestEvidenceVerifier,
    )
    .unwrap();
    let reversed = derive_arbitrated_materialization_batch(
        &[
            BatchMaterializationRequest {
                authorities: MaterializationAuthorities {
                    search: &current_search,
                    block: &current_block,
                    rule: &lower,
                },
                inputs: &lower_inputs,
            },
            BatchMaterializationRequest {
                authorities: MaterializationAuthorities {
                    search: &current_search,
                    block: &current_block,
                    rule: &higher,
                },
                inputs: &higher_inputs,
            },
        ],
        window(),
        OBSERVED_AT_UTC_MS,
        &TestEvidenceVerifier,
    )
    .unwrap();
    assert_eq!(batch, reversed);
    let plans = batch.plans();
    assert!(plans[0].draft_intent.is_some());
    assert!(plans[1].draft_intent.is_none());
    assert!(plans[1].outcomes.iter().all(|outcome| matches!(
        outcome,
        SlotMaterializationOutcome::SuppressedByPrecedence {
            winner_rule_id,
            candidate_priority: 3,
            winner_priority: 4,
            reason: ArbitrationPrecedence::HigherPriority,
            ..
        } if winner_rule_id == &higher.rule_id
    )));
}

#[test]
fn authenticated_batch_records_equal_priority_rule_id_precedence() {
    let current_search = search();
    let current_block = block();
    let mut first = rule();
    first.priority = 3;
    let mut later = rule();
    later.rule_id = id("06");
    later.priority = 3;
    let current_window = window();
    let first_inputs = inputs(
        &current_search,
        &current_block,
        &first,
        &current_window,
        vec![candidate('a', "Alpha council", 1_700_000_000_000)],
        vec![],
    );
    let later_inputs = inputs(
        &current_search,
        &current_block,
        &later,
        &current_window,
        vec![candidate('a', "Alpha council", 1_700_000_000_000)],
        vec![],
    );
    let batch = derive_arbitrated_materialization_batch(
        &[
            BatchMaterializationRequest {
                authorities: MaterializationAuthorities {
                    search: &current_search,
                    block: &current_block,
                    rule: &first,
                },
                inputs: &first_inputs,
            },
            BatchMaterializationRequest {
                authorities: MaterializationAuthorities {
                    search: &current_search,
                    block: &current_block,
                    rule: &later,
                },
                inputs: &later_inputs,
            },
        ],
        current_window,
        OBSERVED_AT_UTC_MS,
        &TestEvidenceVerifier,
    )
    .unwrap();
    assert!(batch.plans()[1].outcomes.iter().all(|outcome| matches!(
        outcome,
        SlotMaterializationOutcome::SuppressedByPrecedence {
            reason: ArbitrationPrecedence::RuleIdTieBreak,
            candidate_priority: 3,
            winner_priority: 3,
            ..
        }
    )));
}

#[test]
fn authenticated_batch_keeps_cross_channel_overlaps_and_revalidates_changed_drafts() {
    let current_search = search();
    let current_block = block();
    let mut higher = rule();
    higher.priority = 4;
    higher.interval_weeks = 2;
    let mut lower = rule();
    lower.rule_id = id("06");
    lower.priority = 3;
    let current_window = window();
    let higher_inputs = inputs(
        &current_search,
        &current_block,
        &higher,
        &current_window,
        vec![candidate('a', "Alpha council", 1_700_000_000_000)],
        vec![],
    );
    let lower_inputs = inputs(
        &current_search,
        &current_block,
        &lower,
        &current_window,
        vec![candidate('a', "Alpha council", 1_700_000_000_000)],
        vec![],
    );
    let individual_lower = plan(
        derive_materialization_intent(
            &current_search,
            &current_block,
            &lower,
            current_window.clone(),
            OBSERVED_AT_UTC_MS,
            &lower_inputs,
            &TestEvidenceVerifier,
        )
        .unwrap(),
    );
    let individual_id = individual_lower
        .draft_intent
        .as_ref()
        .unwrap()
        .materialization_id
        .clone();
    let batch = derive_arbitrated_materialization_batch(
        &[
            BatchMaterializationRequest {
                authorities: MaterializationAuthorities {
                    search: &current_search,
                    block: &current_block,
                    rule: &higher,
                },
                inputs: &higher_inputs,
            },
            BatchMaterializationRequest {
                authorities: MaterializationAuthorities {
                    search: &current_search,
                    block: &current_block,
                    rule: &lower,
                },
                inputs: &lower_inputs,
            },
        ],
        current_window.clone(),
        OBSERVED_AT_UTC_MS,
        &TestEvidenceVerifier,
    )
    .unwrap();
    let retained_lower = batch.plans()[1].draft_intent.as_ref().unwrap();
    assert_ne!(retained_lower.materialization_id, individual_id);
    assert_eq!(retained_lower.occurrence_ids().len(), 1);
    let mut other_channel = lower.clone();
    other_channel.rule_id = id("07");
    other_channel.channel_id = id("09");
    let mut other_block = current_block.clone();
    other_block.block_id = id("08");
    other_block.channel_id = id("09");
    other_channel.block_id = other_block.block_id.clone();
    let other_inputs = inputs(
        &current_search,
        &other_block,
        &other_channel,
        &current_window,
        vec![candidate('a', "Alpha council", 1_700_000_000_000)],
        vec![],
    );
    let cross_channel = derive_arbitrated_materialization_batch(
        &[
            BatchMaterializationRequest {
                authorities: MaterializationAuthorities {
                    search: &current_search,
                    block: &current_block,
                    rule: &lower,
                },
                inputs: &lower_inputs,
            },
            BatchMaterializationRequest {
                authorities: MaterializationAuthorities {
                    search: &current_search,
                    block: &other_block,
                    rule: &other_channel,
                },
                inputs: &other_inputs,
            },
        ],
        current_window,
        OBSERVED_AT_UTC_MS,
        &TestEvidenceVerifier,
    )
    .unwrap();
    assert!(
        cross_channel
            .plans()
            .iter()
            .all(|plan| plan.draft_intent.is_some())
    );
}

#[test]
fn authenticated_batch_rejects_duplicate_recurring_rule_authority() {
    let current_search = search();
    let current_block = block();
    let current_rule = rule();
    let current_window = window();
    let authenticated = inputs(
        &current_search,
        &current_block,
        &current_rule,
        &current_window,
        vec![candidate('a', "Alpha council", 1_700_000_000_000)],
        vec![],
    );
    let request = BatchMaterializationRequest {
        authorities: MaterializationAuthorities {
            search: &current_search,
            block: &current_block,
            rule: &current_rule,
        },
        inputs: &authenticated,
    };
    assert!(
        derive_arbitrated_materialization_batch(
            &[request, request],
            current_window,
            OBSERVED_AT_UTC_MS,
            &TestEvidenceVerifier,
        )
        .unwrap_err()
        .contains("duplicate recurring-rule authority")
    );

    let mut rebound_rule = current_rule.clone();
    rebound_rule.channel_id = id("09");
    let mut rebound_block = current_block.clone();
    rebound_block.channel_id = id("09");
    let rebound_inputs = inputs(
        &current_search,
        &rebound_block,
        &rebound_rule,
        &window(),
        vec![candidate('a', "Alpha council", 1_700_000_000_000)],
        vec![],
    );
    assert!(
        derive_arbitrated_materialization_batch(
            &[
                request,
                BatchMaterializationRequest {
                    authorities: MaterializationAuthorities {
                        search: &current_search,
                        block: &rebound_block,
                        rule: &rebound_rule,
                    },
                    inputs: &rebound_inputs,
                },
            ],
            window(),
            OBSERVED_AT_UTC_MS,
            &TestEvidenceVerifier,
        )
        .unwrap_err()
        .contains("duplicate recurring-rule authority")
    );
}

#[test]
fn arbitration_uses_pairwise_nonoverlapping_winners_for_overlap_chains() {
    let channel_id = id("02");
    let outcomes = arbitrate_rule_slots(&[
        RuleSlotCandidate {
            rule_id: id("05"),
            channel_id: channel_id.clone(),
            priority: 30,
            starts_at_utc_ms: 100,
            ends_at_utc_ms: 200,
        },
        RuleSlotCandidate {
            rule_id: id("06"),
            channel_id: channel_id.clone(),
            priority: 20,
            starts_at_utc_ms: 150,
            ends_at_utc_ms: 250,
        },
        RuleSlotCandidate {
            rule_id: id("07"),
            channel_id,
            priority: 10,
            starts_at_utc_ms: 200,
            ends_at_utc_ms: 300,
        },
    ])
    .unwrap();
    assert!(matches!(
        &outcomes[0],
        ArbitrationOutcome::Winner { rule_id, .. } if rule_id == &id("05")
    ));
    assert!(matches!(
        &outcomes[1],
        ArbitrationOutcome::SuppressedByPrecedence { rule_id, .. } if rule_id == &id("06")
    ));
    assert!(matches!(
        &outcomes[2],
        ArbitrationOutcome::Winner { rule_id, .. } if rule_id == &id("07")
    ));
}
