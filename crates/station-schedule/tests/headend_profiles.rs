use station_schedule::{
    ChannelConfiguration, HEADEND_PROFILES, HeadendProfileApplyRequest, HeadendTransport,
    OutputProfileId, OutputSinkConfiguration, ScheduleError, apply_headend_profile,
    headend_profile,
};

const CHANNEL_ID: &str = "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e";
const EXISTING_SINK_ID: &str = "22222222-3333-4444-8555-666666666666";

const PROFILE_IDS: [&str; 6] = [
    "generic-udp-spts",
    "comcast-mtd-sd",
    "comcast-mtd-hd",
    "telvue-hypercaster-ip",
    "harmonic-spectrum-ts",
    "leightronix-file-drop",
];

fn existing_channel() -> ChannelConfiguration {
    ChannelConfiguration {
        channel_id: CHANNEL_ID.into(),
        display_name: "Government".into(),
        output_profile: OutputProfileId::Proof360p30,
        output_sinks: vec![OutputSinkConfiguration::required_udp(
            EXISTING_SINK_ID,
            "Streaming backup",
            "192.0.2.20:5500",
            -16.0,
        )],
        enabled: false,
    }
}

fn request(
    profile_id: &str,
    destination_uri: &str,
    keep_existing_sinks: bool,
) -> HeadendProfileApplyRequest {
    HeadendProfileApplyRequest {
        profile_id: profile_id.into(),
        destination_uri: destination_uri.into(),
        muxrate_kbps: None,
        keep_existing_sinks,
    }
}

#[test]
fn catalog_contains_exactly_six_named_profiles_with_honest_metadata() {
    assert_eq!(HEADEND_PROFILES.len(), 6);
    for profile_id in PROFILE_IDS {
        let profile = headend_profile(profile_id).expect("every named profile must be listed");
        profile.validate().expect("catalog metadata must be valid");
        assert!(
            !profile.source_urls.is_empty(),
            "{profile_id} needs sources"
        );
        assert!(
            !profile.operator_must_supply.is_empty(),
            "{profile_id} needs explicit operator fields"
        );
        assert!(
            profile
                .not_claimed
                .iter()
                .any(|item| item.to_ascii_lowercase().contains("field")),
            "{profile_id} must preserve the field-proof boundary"
        );
    }
}

#[test]
fn named_profiles_expose_the_required_cable_resolution_matrix() {
    assert_eq!(
        headend_profile("comcast-mtd-sd")
            .unwrap()
            .canonical_profile
            .profile()
            .width,
        720
    );
    assert_eq!(
        headend_profile("comcast-mtd-sd")
            .unwrap()
            .canonical_profile
            .profile()
            .height,
        480
    );
    assert!(
        headend_profile("comcast-mtd-sd")
            .unwrap()
            .canonical_profile
            .profile()
            .interlaced
    );

    assert_eq!(
        headend_profile("comcast-mtd-hd")
            .unwrap()
            .canonical_profile
            .profile()
            .width,
        1_920
    );
    assert_eq!(
        headend_profile("comcast-mtd-hd")
            .unwrap()
            .canonical_profile
            .profile()
            .height,
        1_080
    );
    assert!(
        headend_profile("comcast-mtd-hd")
            .unwrap()
            .canonical_profile
            .profile()
            .interlaced
    );

    assert_eq!(
        headend_profile("generic-udp-spts")
            .unwrap()
            .canonical_profile
            .profile()
            .width,
        1_280
    );
    assert_eq!(
        headend_profile("generic-udp-spts")
            .unwrap()
            .canonical_profile
            .profile()
            .height,
        720
    );
    assert_eq!(
        headend_profile("telvue-hypercaster-ip")
            .unwrap()
            .canonical_profile
            .profile()
            .width,
        1_280
    );
    assert_eq!(
        headend_profile("harmonic-spectrum-ts")
            .unwrap()
            .canonical_profile
            .profile()
            .width,
        1_920
    );
    assert_eq!(
        headend_profile("leightronix-file-drop")
            .unwrap()
            .canonical_profile
            .profile()
            .width,
        1_280
    );
    assert!(OutputProfileId::Atsc720p5994.is_runnable());
    assert!(!OutputProfileId::Atsc1080i5994.is_runnable());
    assert!(!OutputProfileId::Ntsc480i2997.is_runnable());
}

#[test]
fn strict_operator_fields_reject_unknown_profiles_bad_destinations_and_bad_muxrates() {
    let channel = existing_channel();
    assert!(matches!(
        apply_headend_profile(
            &channel,
            &request("not-a-profile", "udp://192.0.2.10:9000", false)
        ),
        Err(ScheduleError::Invalid(_))
    ));
    assert!(
        apply_headend_profile(&channel, &request("generic-udp-spts", "not-a-uri", false)).is_err()
    );

    let multicast = request("generic-udp-spts", "udp://239.1.1.1:9000", false);
    if headend_profile("generic-udp-spts").unwrap().transport == HeadendTransport::UdpUnicast {
        assert!(apply_headend_profile(&channel, &multicast).is_err());
    }

    let mut invalid_muxrate = request("generic-udp-spts", "udp://192.0.2.10:9000", false);
    invalid_muxrate.muxrate_kbps = Some(0);
    assert!(apply_headend_profile(&channel, &invalid_muxrate).is_err());

    let mut malformed_channel = channel;
    malformed_channel.channel_id = "not-a-uuid-with-too-many-characters-to-be-a-channel".into();
    assert!(
        apply_headend_profile(
            &malformed_channel,
            &request("generic-udp-spts", "udp://192.0.2.10:9000", false)
        )
        .is_err()
    );
}

#[test]
fn applying_a_profile_replaces_sinks_and_copies_the_canonical_profile() {
    let applied = apply_headend_profile(
        &existing_channel(),
        &request("generic-udp-spts", "udp://192.0.2.10:9000", false),
    )
    .unwrap();

    assert_eq!(applied.channel.output_sinks.len(), 1);
    assert_ne!(applied.channel.output_sinks[0].sink_id, EXISTING_SINK_ID);
    assert_eq!(applied.channel.output_sinks[0].loudness_target_lufs, -24.0);
    assert_eq!(
        applied.channel.output_profile,
        headend_profile("generic-udp-spts")
            .unwrap()
            .canonical_profile
    );
}

#[test]
fn every_network_profile_can_be_staged_with_its_default_muxrate() {
    for (profile_id, destination) in [
        ("generic-udp-spts", "udp://192.0.2.10:9000"),
        ("comcast-mtd-sd", "udp://239.1.1.10:9000"),
        ("comcast-mtd-hd", "udp://239.1.1.11:9000"),
        ("telvue-hypercaster-ip", "udp://192.0.2.11:9000"),
        ("harmonic-spectrum-ts", "udp://192.0.2.12:9000"),
    ] {
        let applied = apply_headend_profile(
            &existing_channel(),
            &request(profile_id, destination, false),
        )
        .unwrap_or_else(|error| panic!("{profile_id} must stage: {error:?}"));
        assert!(!applied.channel.enabled);
        assert_eq!(applied.profile_id.storage_name(), profile_id);
    }
}

#[test]
fn staged_headend_contract_cannot_be_enabled_before_the_engine_enforces_it() {
    let mut applied = apply_headend_profile(
        &existing_channel(),
        &request("generic-udp-spts", "udp://192.0.2.10:9000", false),
    )
    .unwrap()
    .channel;
    applied.enabled = true;
    assert!(matches!(applied.validate(), Err(ScheduleError::Invalid(_))));
}

#[test]
fn applying_a_profile_can_keep_existing_sinks_and_only_add_the_headend_sink() {
    let applied = apply_headend_profile(
        &existing_channel(),
        &request("generic-udp-spts", "udp://192.0.2.10:9000", true),
    )
    .unwrap();

    assert_eq!(applied.channel.output_sinks.len(), 2);
    let existing = applied
        .channel
        .output_sinks
        .iter()
        .find(|sink| sink.sink_id == EXISTING_SINK_ID)
        .unwrap();
    assert_eq!(existing.loudness_target_lufs, -16.0);
    let headend = applied
        .channel
        .output_sinks
        .iter()
        .find(|sink| sink.sink_id != EXISTING_SINK_ID)
        .unwrap();
    assert_eq!(headend.loudness_target_lufs, -24.0);
}

#[test]
fn headend_profile_application_never_claims_field_or_headend_proof() {
    let applied = apply_headend_profile(
        &existing_channel(),
        &request("generic-udp-spts", "udp://192.0.2.10:9000", false),
    )
    .unwrap();
    assert!(applied.not_claimed.iter().any(|item| {
        let lower = item.to_ascii_lowercase();
        lower.contains("field") || lower.contains("headend")
    }));
}
