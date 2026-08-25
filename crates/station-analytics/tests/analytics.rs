use station_analytics::{
    AnalyticsDimension, AnalyticsEvent, AnalyticsEventKind, AnalyticsSnapshot, AudienceDelivery,
    AudienceWindow, OttBeaconTrust, derive_utc_rollup, validate_event_intake,
    validate_snapshot_write, validate_year_over_year_inputs,
};

fn identity(suffix: &str) -> String {
    format!("00000000-0000-4000-8000-0000000000{suffix}")
}

fn dimensions() -> Vec<AnalyticsDimension> {
    vec![
        AnalyticsDimension {
            key: "platform".into(),
            value: "roku".into(),
        },
        AnalyticsDimension {
            key: "territory".into(),
            value: "us-co".into(),
        },
    ]
}

fn event(event_id: &str, delivery: AudienceDelivery, occurred_at_utc_ms: i64) -> AnalyticsEvent {
    AnalyticsEvent {
        event_id: identity(event_id),
        channel_id: identity("02"),
        content_sha256: "a".repeat(64),
        delivery,
        kind: AnalyticsEventKind::ViewStarted,
        occurred_at_utc_ms,
        dimensions: dimensions(),
        ott_beacon: None,
    }
}

fn window() -> AudienceWindow {
    AudienceWindow {
        starts_at_utc_ms: 0,
        ends_at_utc_ms: 86_400_000,
    }
}

#[test]
fn live_and_vod_rollups_are_deterministic_and_never_coalesced() {
    let live = event("03", AudienceDelivery::Live, 1_000);
    let vod = event("04", AudienceDelivery::Vod, 2_000);
    let live_rollup = derive_utc_rollup(std::slice::from_ref(&live), window(), 1).unwrap();
    assert_eq!(live_rollup.delivery, AudienceDelivery::Live);
    assert_eq!(live_rollup.event_count, 1);
    assert!(derive_utc_rollup(&[live, vod], window(), 1).is_err());

    let snapshot = AnalyticsSnapshot {
        snapshot_id: identity("05"),
        window: window(),
        rollups: vec![live_rollup],
        revision: 1,
    };
    snapshot.validate().unwrap();
}

#[test]
fn ott_beacons_require_current_trust_and_privacy_safe_dimensions() {
    let trust = OttBeaconTrust {
        source_id: identity("06"),
        revision: 4,
        enabled: true,
    };
    let mut beacon = event("07", AudienceDelivery::Vod, 1_000);
    beacon.ott_beacon = Some(station_analytics::OttBeaconAssertion {
        source_id: trust.source_id.clone(),
        source_revision: trust.revision,
    });
    validate_event_intake(&beacon, std::slice::from_ref(&trust)).unwrap();
    beacon.ott_beacon.as_mut().unwrap().source_revision = 3;
    assert!(validate_event_intake(&beacon, &[trust]).is_err());

    let mut unsafe_event = event("08", AudienceDelivery::Live, 1_000);
    unsafe_event.dimensions[0].value = "person@example.com".into();
    assert!(
        validate_event_intake(&unsafe_event, &[])
            .unwrap_err()
            .contains("privacy")
    );

    let mut non_canonical = event("09", AudienceDelivery::Live, 1_000);
    non_canonical.dimensions.reverse();
    assert!(validate_event_intake(&non_canonical, &[]).is_err());
}

#[test]
fn ambiguous_windows_forged_rollups_and_stale_snapshots_fail_closed() {
    let live_event = event("09", AudienceDelivery::Live, 1_000);
    assert!(
        derive_utc_rollup(
            std::slice::from_ref(&live_event),
            AudienceWindow {
                starts_at_utc_ms: 1_000,
                ends_at_utc_ms: 1_000,
            },
            1,
        )
        .is_err()
    );
    let mut rollup = derive_utc_rollup(&[live_event], window(), 1).unwrap();
    rollup.event_count = 99;
    assert!(rollup.validate().is_err());

    let first = AnalyticsSnapshot {
        snapshot_id: identity("10"),
        window: window(),
        rollups: vec![
            derive_utc_rollup(&[event("11", AudienceDelivery::Live, 1_000)], window(), 1).unwrap(),
        ],
        revision: 1,
    };
    validate_snapshot_write(None, None, &first).unwrap();
    assert!(validate_snapshot_write(Some(0), Some(&first), &first).is_err());
}

#[test]
fn year_over_year_inputs_require_comparable_unambiguous_utc_windows() {
    let current = AnalyticsSnapshot {
        snapshot_id: identity("12"),
        window: AudienceWindow {
            starts_at_utc_ms: 31_622_400_000,
            ends_at_utc_ms: 31_708_800_000,
        },
        rollups: vec![
            derive_utc_rollup(
                &[event("13", AudienceDelivery::Live, 31_622_401_000)],
                AudienceWindow {
                    starts_at_utc_ms: 31_622_400_000,
                    ends_at_utc_ms: 31_708_800_000,
                },
                1,
            )
            .unwrap(),
        ],
        revision: 1,
    };
    let prior = AnalyticsSnapshot {
        snapshot_id: identity("14"),
        window: window(),
        rollups: vec![
            derive_utc_rollup(&[event("15", AudienceDelivery::Live, 1_000)], window(), 1).unwrap(),
        ],
        revision: 1,
    };
    assert!(validate_year_over_year_inputs(&current, &prior).is_ok());
    let mut ambiguous = prior;
    ambiguous.window.ends_at_utc_ms += 1;
    assert!(validate_year_over_year_inputs(&current, &ambiguous).is_err());
}
