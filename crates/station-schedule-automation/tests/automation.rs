use station_schedule_automation::{
    DaypartBlock, DstApplication, LocalDate, MaterializationAdmission, MaterializationWindow,
    RecurrenceTimeZone, RecurringRule, SavedSearch, admit_materialization, canonical_intent_bytes,
    canonical_source_bytes, derive_materialization_intent,
};

fn id(suffix: &str) -> String {
    format!("00000000-0000-4000-8000-0000000000{suffix}")
}

fn hex(bytes: &[u8]) -> String {
    bytes
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<Vec<_>>()
        .join("")
}

fn search() -> SavedSearch {
    SavedSearch {
        search_id: id("01"),
        channel_id: id("02"),
        owner_operator_id: id("03"),
        terms: vec!["council".into(), "meeting".into()],
        revision: 1,
    }
}

fn block() -> DaypartBlock {
    DaypartBlock {
        block_id: id("04"),
        channel_id: id("02"),
        label: "Weeknight civic".into(),
        starts_local_minute: 1_080,
        ends_local_minute: 1_320,
        revision: 1,
    }
}

fn rule() -> RecurringRule {
    RecurringRule {
        rule_id: id("05"),
        channel_id: id("02"),
        saved_search_id: id("01"),
        saved_search_revision: 1,
        asset_id: id("06"),
        asset_revision: 7,
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

#[test]
fn deterministic_materialization_is_idempotent_without_dispatch() {
    let intent = derive_materialization_intent(&search(), &block(), &rule(), window()).unwrap();
    assert_eq!(intent.asset_revision, 7);
    assert_eq!(
        admit_materialization(&[], intent.clone(), &search(), &block(), &rule()).unwrap(),
        MaterializationAdmission::Create(Box::new(intent.clone()))
    );
    assert_eq!(
        admit_materialization(
            std::slice::from_ref(&intent),
            intent.clone(),
            &search(),
            &block(),
            &rule(),
        )
        .unwrap(),
        MaterializationAdmission::AlreadyPresent
    );
}

#[test]
fn timezone_dst_policy_and_window_bounds_are_explicit_and_fail_closed() {
    let mut ambiguous = rule();
    ambiguous.timezone = RecurrenceTimeZone::Named {
        iana_name: "America/Denver".into(),
        dst: DstApplication::Standard,
    };
    assert!(derive_materialization_intent(&search(), &block(), &ambiguous, window()).is_err());
    let mut too_wide = window();
    too_wide.ends_at_utc_ms += 14 * 86_400_000;
    assert!(derive_materialization_intent(&search(), &block(), &rule(), too_wide).is_err());
}

#[test]
fn stale_revisions_rebinding_and_duplicate_append_fail_closed() {
    let mut stale = rule();
    stale.saved_search_revision = 2;
    assert!(derive_materialization_intent(&search(), &block(), &stale, window()).is_err());
    let intent = derive_materialization_intent(&search(), &block(), &rule(), window()).unwrap();
    let mut forged = intent.clone();
    forged.asset_id = id("07");
    assert!(admit_materialization(&[], forged, &search(), &block(), &rule()).is_err());
    assert!(
        admit_materialization(
            &[intent.clone(), intent.clone()],
            intent,
            &search(),
            &block(),
            &rule()
        )
        .is_err()
    );
}

#[test]
fn dayparts_are_same_day_only_and_rule_successor_requires_exact_dependencies() {
    let mut overnight = block();
    overnight.ends_local_minute = 60;
    assert!(overnight.validate().is_err());
    let current = rule();
    let mut next = current.clone();
    next.revision = 2;
    next.saved_search_revision = 2;
    assert!(
        next.validate_successor_of(&current, 1, &search(), &block())
            .is_err()
    );
}

#[test]
fn canonical_hashes_match_version_one_golden_vectors() {
    let bytes = canonical_source_bytes(&search(), &block(), &rule()).unwrap();
    let mut expected_prefix = 28_u64.to_le_bytes().to_vec();
    expected_prefix.extend_from_slice(b"townlight.schedule-source.v1");
    expected_prefix.extend_from_slice(&1_u16.to_le_bytes());
    assert_eq!(&bytes[..expected_prefix.len()], expected_prefix.as_slice());
    let intent = derive_materialization_intent(&search(), &block(), &rule(), window()).unwrap();
    let intent_bytes = canonical_intent_bytes(&intent);
    assert_eq!(
        (
            hex(&bytes),
            hex(&intent_bytes),
            intent.source_authority_sha256,
            intent.materialization_id,
        ),
        (
            "1c00000000000000746f776e6c696768742e7363686564756c652d736f757263652e76310100240000000000000030303030303030302d303030302d343030302d383030302d303030303030303030303031240000000000000030303030303030302d303030302d343030302d383030302d303030303030303030303032240000000000000030303030303030302d303030302d343030302d383030302d3030303030303030303030330100000000000000020000000700000000000000636f756e63696c07000000000000006d656574696e67240000000000000030303030303030302d303030302d343030302d383030302d303030303030303030303034240000000000000030303030303030302d303030302d343030302d383030302d3030303030303030303030320f000000000000005765656b6e69676874206369766963380428050100000000000000240000000000000030303030303030302d303030302d343030302d383030302d303030303030303030303035240000000000000030303030303030302d303030302d343030302d383030302d303030303030303030303032240000000000000030303030303030302d303030302d343030302d383030302d303030303030303030303031240000000000000030303030303030302d303030302d343030302d383030302d303030303030303030303036240000000000000030303030303030302d303030302d343030302d383030302d3030303030303030303030340100000000000000070000000000000001000000000000000801ea07010105000000000000006e616d65640e00000000000000416d65726963612f44656e76657207000000000000006561726c69657274043c000100000000000000".to_owned(),
            "2300000000000000746f776e6c696768742e6d6174657269616c697a6174696f6e2d696e74656e742e76310100240000000000000030303030303030302d303030302d343030302d383030302d303030303030303030303035240000000000000030303030303030302d303030302d343030302d383030302d303030303030303030303032240000000000000030303030303030302d303030302d343030302d383030302d303030303030303030303036240000000000000030303030303030302d303030302d343030302d383030302d30303030303030303030303401000000000000000700000000000000010000000000000000a8da769b01000000b0f3be9b01000040000000000000006433616531356339643133303738373064313465366232326666373464643239356232386165386237306332626336656638346232653664376635363733373602000000ea07010174040e00000000000000416d65726963612f44656e766572909dffff00e16e7c9b01000080cfa57c9b01000000ea07010874040e00000000000000416d65726963612f44656e766572909dffff00657ba09b0100008053b2a09b01000000".to_owned(),
            "d3ae15c9d1307870d14e6b22ff74dd295b28ae8b70c2bc6ef84b2e6d7f567376".to_owned(),
            "3eb2fafd14cadfde766741c913c46df185eebe63cc392f3a85afe8cfe057ad52".to_owned(),
        )
    );
}

#[test]
fn claimed_rule_and_search_bounds_fail_closed() {
    for mutation in 0..5 {
        let mut invalid = rule();
        match mutation {
            0 => invalid.weekday_mask = 0,
            1 => invalid.interval_weeks = 5,
            2 => invalid.duration_minutes = 0,
            3 => invalid.local_start_minute = 1_440,
            _ => invalid.anchor_date.year = 2101,
        }
        assert!(derive_materialization_intent(&search(), &block(), &invalid, window()).is_err());
    }
    let mut duplicate_terms = search();
    duplicate_terms.terms = vec!["council".into(), "council".into()];
    assert!(duplicate_terms.validate().is_err());
    let mut unbounded_terms = search();
    unbounded_terms.terms = (0..9).map(|index| format!("term{index}")).collect();
    assert!(unbounded_terms.validate().is_err());
}

#[test]
fn materialization_ledger_bound_fails_closed() {
    let intent = derive_materialization_intent(&search(), &block(), &rule(), window()).unwrap();
    assert!(
        admit_materialization(
            &vec![intent.clone(); 65],
            intent,
            &search(),
            &block(),
            &rule()
        )
        .is_err()
    );
}

#[test]
fn recurring_rule_successor_rejects_stale_cas_asset_channel_and_dependency_rebinding() {
    let current = rule();
    for mutation in 0..4 {
        let mut next = current.clone();
        next.revision = 2;
        let next_search = search();
        match mutation {
            0 => next.asset_id = id("08"),
            1 => next.channel_id = id("08"),
            2 => next.saved_search_id = id("08"),
            _ => next.block_id = id("08"),
        }
        assert!(
            next.validate_successor_of(
                &current,
                if mutation == 0 { 0 } else { 1 },
                &next_search,
                &block()
            )
            .is_err()
        );
    }
}

#[test]
fn jiff_resolves_denver_normal_and_both_fall_back_instants() {
    let normal_window = MaterializationWindow {
        starts_at_utc_ms: 1_767_225_600_000,
        ends_at_utc_ms: 1_767_571_200_000,
    };
    let normal =
        derive_materialization_intent(&search(), &block(), &rule(), normal_window).unwrap();
    assert_eq!(
        normal.resolved_slots(),
        vec![(
            1_767_319_200_000,
            -25_200,
            station_schedule_automation::LocalResolution::Normal
        )]
    );

    for (policy, expected_ms, expected_offset, expected_resolution) in [
        (
            DstApplication::EarlierFold,
            1_793_518_200_000,
            -21_600,
            station_schedule_automation::LocalResolution::EarlierFold,
        ),
        (
            DstApplication::LaterFold,
            1_793_521_800_000,
            -25_200,
            station_schedule_automation::LocalResolution::LaterFold,
        ),
    ] {
        let mut fold_rule = rule();
        fold_rule.anchor_date = LocalDate {
            year: 2026,
            month: 11,
            day: 1,
        };
        fold_rule.weekday_mask = 1 << 6;
        fold_rule.local_start_minute = 90;
        fold_rule.duration_minutes = 60;
        fold_rule.timezone = RecurrenceTimeZone::Named {
            iana_name: "America/Denver".into(),
            dst: policy,
        };
        let mut fold_block = block();
        fold_block.starts_local_minute = 0;
        fold_block.ends_local_minute = 180;
        let fold_window = MaterializationWindow {
            starts_at_utc_ms: 1_793_491_200_000,
            ends_at_utc_ms: 1_793_491_200_000 + 3 * 86_400_000,
        };
        let intent =
            derive_materialization_intent(&search(), &fold_block, &fold_rule, fold_window).unwrap();
        assert_eq!(
            intent.resolved_slots(),
            vec![(expected_ms, expected_offset, expected_resolution)]
        );
    }
}

#[test]
fn jiff_rejects_denver_spring_gap() {
    let mut gap_rule = rule();
    gap_rule.anchor_date = LocalDate {
        year: 2026,
        month: 3,
        day: 8,
    };
    gap_rule.weekday_mask = 1 << 6;
    gap_rule.local_start_minute = 150;
    let mut gap_block = block();
    gap_block.starts_local_minute = 0;
    gap_block.ends_local_minute = 240;
    let gap_window = MaterializationWindow {
        starts_at_utc_ms: 1_772_928_000_000,
        ends_at_utc_ms: 1_774_137_600_000,
    };
    assert!(derive_materialization_intent(&search(), &gap_block, &gap_rule, gap_window).is_err());
}

#[test]
fn positive_revision_two_successor_requires_exact_new_dependencies() {
    let current = rule();
    let mut next_search = search();
    next_search.revision = 2;
    let mut next = current.clone();
    next.revision = 2;
    next.saved_search_revision = 2;
    assert!(
        next.validate_successor_of(&current, 1, &next_search, &block())
            .is_ok()
    );
}
