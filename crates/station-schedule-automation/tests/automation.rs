use station_schedule_automation::{
    ArbitrationOutcome, AssetCandidate, AssetPickStrategy, AssetResolution, DaypartBlock,
    DstApplication, LocalDate, MaterializationOutcome, MaterializationPlan, MaterializationWindow,
    MetadataFilter, MetadataValue, PriorAiring, RecurrenceTimeZone, RecurringRule,
    RuleSlotCandidate, SavedSearch, SavedSearchOrder, SavedSearchQuery, SlotMaterializationOutcome,
    arbitrate_rule_slots, canonical_intent_bytes, canonical_source_bytes,
    derive_materialization_intent, resolve_asset,
};
use std::collections::BTreeMap;

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
        channel_id: id("02"),
        owner_operator_id: id("03"),
        revision: 1,
        query: SavedSearchQuery {
            all_of: vec![MetadataFilter {
                field: "topic".into(),
                value: MetadataValue::Choice("council".into()),
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
        duration_ms: 3_600_000,
        ready: true,
        published: true,
        published_at_utc_ms,
        indexed_custom_metadata: BTreeMap::from([(
            "topic".into(),
            MetadataValue::Choice("council".into()),
        )]),
    }
}
fn plan(outcome: MaterializationOutcome) -> MaterializationPlan {
    match outcome {
        MaterializationOutcome::Plan(plan) => plan,
    }
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
    let first = plan(
        derive_materialization_intent(
            &search(),
            &block(),
            &rule(),
            window(),
            &[candidate('a', "Alpha council", 1_700_000_000_000)],
            &[],
        )
        .unwrap(),
    );
    let shifted = plan(
        derive_materialization_intent(
            &search(),
            &block(),
            &rule(),
            MaterializationWindow {
                starts_at_utc_ms: 1_767_300_000_000,
                ends_at_utc_ms: 1_768_435_200_000,
            },
            &[candidate('a', "Alpha council", 1_700_000_000_000)],
            &[],
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
    assert!(matches!(
        derive_materialization_intent(&search(), &block(), &rule(), window(), &[], &[]).unwrap(),
        MaterializationOutcome::Plan(plan) if plan.outcomes.iter().all(|outcome| matches!(outcome, SlotMaterializationOutcome::NoMatch { .. }))
    ));
    let mut disabled = rule();
    disabled.enabled = false;
    assert!(matches!(
        derive_materialization_intent(
            &search(),
            &block(),
            &disabled,
            window(),
            &[candidate('a', "Alpha council", 1_700_000_000_000)],
            &[]
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
        derive_materialization_intent(
            &search(),
            &seasonal,
            &rule(),
            window(),
            &[candidate('a', "Alpha council", 1_700_000_000_000)],
            &[]
        )
        .unwrap(),
        MaterializationOutcome::Plan(plan) if plan.outcomes.iter().all(|outcome| matches!(outcome, SlotMaterializationOutcome::Blocked { .. }))
    ));
}

#[test]
fn mixed_season_window_keeps_proposed_and_blocked_slots() {
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
        derive_materialization_intent(
            &search(),
            &seasonal,
            &rule(),
            window(),
            &[candidate('a', "Alpha council", 1_700_000_000_000)],
            &[],
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
fn v2_domains_are_explicit() {
    let source = canonical_source_bytes(&search(), &block(), &rule()).unwrap();
    let plan = plan(
        derive_materialization_intent(
            &search(),
            &block(),
            &rule(),
            window(),
            &[candidate('a', "Alpha council", 1_700_000_000_000)],
            &[],
        )
        .unwrap(),
    );
    let intent = plan.draft_intent.unwrap();
    assert_eq!(
        hex(&source),
        "1c00000000000000746f776e6c696768742e7363686564756c652d736f757263652e76320200240000000000000030303030303030302d303030302d343030302d383030302d303030303030303030303031240000000000000030303030303030302d303030302d343030302d383030302d303030303030303030303032240000000000000030303030303030302d303030302d343030302d383030302d3030303030303030303030330100000000000000000800010000000500000000000000746f706963040700000000000000636f756e63696c00000000240000000000000030303030303030302d303030302d343030302d383030302d303030303030303030303034240000000000000030303030303030302d303030302d343030302d383030302d3030303030303030303030320f000000000000005765656b6e69676874206369766963380428057f00000000000100000000000000240000000000000030303030303030302d303030302d343030302d383030302d303030303030303030303035240000000000000030303030303030302d303030302d343030302d383030302d303030303030303030303032240000000000000030303030303030302d303030302d343030302d383030302d303030303030303030303031240000000000000030303030303030302d303030302d343030302d383030302d30303030303030303030303401000000000000000100000000000000000e000100000801ea07010105000000000000006e616d65640e00000000000000416d65726963612f44656e76657207000000000000006561726c69657274043c000100000000000000"
    );
    assert_eq!(
        hex(&canonical_intent_bytes(&intent)),
        "2300000000000000746f776e6c696768742e6d6174657269616c697a6174696f6e2d696e74656e742e76320200240000000000000030303030303030302d303030302d343030302d383030302d303030303030303030303035240000000000000030303030303030302d303030302d343030302d383030302d303030303030303030303032240000000000000030303030303030302d303030302d343030302d383030302d3030303030303030303030340100000000000000010000000000000000a8da769b01000000b0f3be9b010000400000000000000063303564643065323230333431643866613463353839373766306134376162313136616438396462313161623432666232613435303337343561616534353339020000004000000000000000396265636439623131613536333465346261386339333863353830383031303139663266613839326266643830633966653565363565356632646265643534304000000000000000616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161610d00000000000000416c70686120636f756e63696cea07010174040e00000000000000416d65726963612f44656e766572909dffff00e16e7c9b01000080cfa57c9b010000004000000000000000303630666165393635303061376363363130313038303333316563393065376138373134323461383030303665623933373539383739656235346465663664624000000000000000616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161616161610d00000000000000416c70686120636f756e63696cea07010874040e00000000000000416d65726963612f44656e766572909dffff00657ba09b0100008053b2a09b01000000"
    );
    assert!(
        source
            .windows(b"townlight.schedule-source.v2".len())
            .any(|part| part == b"townlight.schedule-source.v2")
    );
    assert!(
        canonical_intent_bytes(&intent)
            .windows(b"townlight.materialization-intent.v2".len())
            .any(|part| part == b"townlight.materialization-intent.v2")
    );
}

#[test]
fn horizon_and_block_allow_list_fail_closed_at_boundaries() {
    let candidate = candidate('a', "Alpha council", 1_700_000_000_000);
    let mut fourteen = rule();
    fourteen.rolling_window_days = 14;
    let within = MaterializationWindow {
        starts_at_utc_ms: 1_767_225_600_000,
        ends_at_utc_ms: 1_768_435_200_000,
    };
    assert!(
        derive_materialization_intent(
            &search(),
            &block(),
            &fourteen,
            within,
            std::slice::from_ref(&candidate),
            &[],
        )
        .is_ok()
    );
    let past = MaterializationWindow {
        starts_at_utc_ms: 1_767_225_600_000,
        ends_at_utc_ms: 1_768_435_200_001,
    };
    assert!(
        derive_materialization_intent(
            &search(),
            &block(),
            &fourteen,
            past,
            std::slice::from_ref(&candidate),
            &[],
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
        derive_materialization_intent(
            &search(),
            &block(),
            &sixty,
            sixty_window,
            std::slice::from_ref(&candidate),
            &[],
        )
        .is_ok()
    );
    let mut denied = block();
    denied.allow_rule_ids = vec![id("06")];
    assert!(
        derive_materialization_intent(&search(), &denied, &rule(), window(), &[candidate], &[])
            .is_err()
    );
}

#[test]
fn overlap_arbitration_uses_priority_then_rule_id() {
    let first = RuleSlotCandidate {
        rule_id: id("05"),
        priority: 3,
        starts_at_utc_ms: 100,
        ends_at_utc_ms: 200,
    };
    let higher = RuleSlotCandidate {
        rule_id: id("06"),
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
        ArbitrationOutcome::SuppressedByHigherPriority {
            rule_id,
            winner_rule_id,
            ..
        } if rule_id == &first.rule_id && winner_rule_id == &higher.rule_id
    ));
    let later_id = RuleSlotCandidate {
        rule_id: id("07"),
        priority: 3,
        starts_at_utc_ms: 150,
        ends_at_utc_ms: 250,
    };
    let tie = arbitrate_rule_slots(&[later_id, first.clone()]).unwrap();
    assert!(
        matches!(&tie[0], ArbitrationOutcome::Winner { rule_id, .. } if rule_id == &first.rule_id)
    );
}
