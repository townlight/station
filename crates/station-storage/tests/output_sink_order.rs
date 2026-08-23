use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use station_schedule::{ChannelConfiguration, OutputProfileId, OutputSinkConfiguration};
use station_storage::StationStore;

fn temporary_database() -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-output-order-{nonce}.db"))
}

#[test]
fn sqlite_round_trip_preserves_nonlexicographic_sink_order() {
    let database = temporary_database();
    let channel = ChannelConfiguration {
        channel_id: "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e".into(),
        display_name: "Ordered outputs".into(),
        output_profile: OutputProfileId::Proof360p30,
        output_sinks: vec![
            OutputSinkConfiguration::required_udp(
                "ffffffff-ffff-4fff-8fff-ffffffffffff",
                "Cable first",
                "127.0.0.1:5500",
                -24.0,
            ),
            OutputSinkConfiguration::required_udp(
                "11111111-1111-4111-8111-111111111111",
                "Streaming second",
                "127.0.0.1:5501",
                -16.0,
            ),
            OutputSinkConfiguration::required_udp(
                "88888888-8888-4888-8888-888888888888",
                "Cable third",
                "127.0.0.1:5502",
                -24.0,
            ),
        ],
        enabled: false,
    };

    StationStore::open(&database)
        .unwrap()
        .put_channel(&channel)
        .unwrap();
    let reopened = StationStore::open(&database).unwrap();
    let stored = reopened
        .list_channels()
        .unwrap()
        .into_iter()
        .find(|candidate| candidate.channel_id == channel.channel_id)
        .unwrap();

    assert_eq!(stored.output_sinks, channel.output_sinks);
    drop(reopened);
    let _ = std::fs::remove_file(database);
}
