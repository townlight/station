use station_schedule::{ChannelConfiguration, OutputProfileId, OutputSinkConfiguration};

#[test]
fn an_enabled_channel_accepts_distinct_cable_and_streaming_targets() {
    let channel = ChannelConfiguration {
        channel_id: "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e".into(),
        display_name: "Mixed cable and streaming".into(),
        output_profile: OutputProfileId::Proof360p30,
        output_sinks: vec![
            OutputSinkConfiguration::required_udp(
                "11111111-2222-4333-8444-555555555555",
                "Cable",
                "127.0.0.1:5500",
                -24.0,
            ),
            OutputSinkConfiguration::required_srt(
                "22222222-3333-4444-8555-666666666666",
                "Streaming",
                "srt://127.0.0.1:5600",
                -16.0,
            ),
        ],
        enabled: true,
    };

    channel
        .validate()
        .expect("the media engine enforces one loudness variant per distinct target");
}

#[test]
fn output_targets_reject_precision_that_the_worker_contract_cannot_represent() {
    let channel = ChannelConfiguration {
        channel_id: "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e".into(),
        display_name: "Over-precise target".into(),
        output_profile: OutputProfileId::Proof360p30,
        output_sinks: vec![OutputSinkConfiguration::required_udp(
            "11111111-2222-4333-8444-555555555555",
            "Streaming",
            "127.0.0.1:5500",
            -16.0005,
        )],
        enabled: false,
    };

    let error = channel
        .validate()
        .expect_err("targets finer than one milli-LUFS must fail before persistence");
    assert!(format!("{error:?}").contains("three decimal"));
}

#[test]
fn output_targets_fail_closed_outside_the_two_executable_variants() {
    let channel = ChannelConfiguration {
        channel_id: "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e".into(),
        display_name: "Unsupported target".into(),
        output_profile: OutputProfileId::Proof360p30,
        output_sinks: vec![OutputSinkConfiguration::required_udp(
            "11111111-2222-4333-8444-555555555555",
            "Unknown contract",
            "127.0.0.1:5500",
            -23.0,
        )],
        enabled: false,
    };

    let error = channel
        .validate()
        .expect_err("only the implemented cable and streaming targets may be staged");
    assert!(format!("{error:?}").contains("-24 or -16"));
}
