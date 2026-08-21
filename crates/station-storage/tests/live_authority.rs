use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use station_live::{
    LiveEndpoint, LiveSource, LiveSourceWriteError, TakeoverSession, TakeoverState,
};
use station_schedule::{ChannelConfiguration, OutputSinkConfiguration};
use station_storage::StationStore;

const CHANNEL: &str = "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e";
const SOURCE: &str = "3f5f721c-bdf8-419a-8a2e-b0a7caa1ff7e";

#[test]
fn persists_revisioned_channel_scoped_source_and_cas_takeover_audit() {
    let database = database_path("live");
    let store = StationStore::open(&database).unwrap();
    store
        .put_channel(&ChannelConfiguration {
            channel_id: CHANNEL.into(),
            display_name: "Primary".into(),
            output_profile: Default::default(),
            output_sinks: vec![OutputSinkConfiguration::required_udp(
                "22222222-3333-4444-8555-666666666666",
                "Primary",
                "127.0.0.1:5500",
                -16.0,
            )],
            enabled: true,
        })
        .unwrap();
    let source = LiveSource {
        source_id: SOURCE.into(),
        channel_id: CHANNEL.into(),
        label: "Council live".into(),
        enabled: true,
        endpoint: LiveEndpoint::UdpMpegTs {
            bind_ip: "239.1.2.3".into(),
            port: 5000,
        },
        revision: 1,
    };
    assert_eq!(store.write_live_source(&source, 0).unwrap(), source);
    assert!(matches!(
        store.write_live_source(&source, 0),
        Err(LiveSourceWriteError::Conflict(1))
    ));

    let session = TakeoverSession {
        session_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".into(),
        channel_id: CHANNEL.into(),
        source_id: SOURCE.into(),
        source_revision: 1,
        authority_revision: 1,
        operator_id: "operator-scott".into(),
        reason: Some("Emergency briefing".into()),
        requested_at_unix_ms: 1_000,
        expires_at_unix_ms: 61_000,
        taken_at_unix_ms: None,
        returned_at_unix_ms: None,
        state: TakeoverState::Arming,
        failure_detail: None,
    };
    store.request_takeover(&session, 0).unwrap();
    assert!(
        store
            .request_takeover(
                &TakeoverSession {
                    session_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb".into(),
                    ..session.clone()
                },
                1
            )
            .is_err()
    );
    let armed = store
        .transition_takeover(
            &session.session_id,
            TakeoverState::Arming,
            TakeoverState::Armed,
            2_000,
            None,
            "stationd",
            1,
        )
        .unwrap();
    assert_eq!(armed.authority_revision, 2);
    assert_eq!(store.read_takeover_authority_revision(CHANNEL).unwrap(), 2);
    assert!(
        store
            .transition_takeover(
                &session.session_id,
                TakeoverState::Armed,
                TakeoverState::TakeRequested,
                2_500,
                None,
                "stale-client",
                1,
            )
            .is_err()
    );
    assert_eq!(store.read_takeover_authority_revision(CHANNEL).unwrap(), 2);
    assert_eq!(
        store.read_active_takeover(CHANNEL).unwrap().unwrap().state,
        TakeoverState::Armed
    );
    let requested = store
        .transition_takeover(
            &session.session_id,
            TakeoverState::Armed,
            TakeoverState::TakeRequested,
            3_000,
            None,
            "operator-scott",
            2,
        )
        .unwrap();
    assert_eq!(requested.authority_revision, 3);
    let on_air = store
        .transition_takeover(
            &session.session_id,
            TakeoverState::TakeRequested,
            TakeoverState::OnAir,
            4_000,
            None,
            "stationd",
            3,
        )
        .unwrap();
    assert_eq!(on_air.taken_at_unix_ms, Some(4_000));
    let returning = store
        .transition_takeover(
            &session.session_id,
            TakeoverState::OnAir,
            TakeoverState::ReturnRequested,
            5_000,
            None,
            "operator-scott",
            4,
        )
        .unwrap();
    assert_eq!(returning.state, TakeoverState::ReturnRequested);
    store
        .transition_takeover(
            &session.session_id,
            TakeoverState::ReturnRequested,
            TakeoverState::Returning,
            6_000,
            None,
            "stationd",
            5,
        )
        .unwrap();
    let completed = store
        .transition_takeover(
            &session.session_id,
            TakeoverState::Returning,
            TakeoverState::Completed,
            7_000,
            None,
            "stationd",
            6,
        )
        .unwrap();
    assert_eq!(completed.returned_at_unix_ms, Some(7_000));
    assert_eq!(store.read_takeover_authority_revision(CHANNEL).unwrap(), 7);
    assert!(store.read_active_takeover(CHANNEL).unwrap().is_none());

    let next_session = TakeoverSession {
        session_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc".into(),
        authority_revision: 8,
        requested_at_unix_ms: 8_000,
        expires_at_unix_ms: 68_000,
        ..session.clone()
    };
    store.request_takeover(&next_session, 7).unwrap();
    assert_eq!(
        store
            .read_active_takeover(CHANNEL)
            .unwrap()
            .unwrap()
            .session_id,
        next_session.session_id
    );
    assert_eq!(store.read_takeover_authority_revision(CHANNEL).unwrap(), 8);
    let expiry_error = store
        .transition_takeover(
            &next_session.session_id,
            TakeoverState::Arming,
            TakeoverState::Armed,
            next_session.expires_at_unix_ms,
            None,
            "stationd",
            8,
        )
        .unwrap_err();
    assert!(expiry_error.contains("expired"));
    store
        .transition_takeover(
            &next_session.session_id,
            TakeoverState::Arming,
            TakeoverState::Failed,
            next_session.expires_at_unix_ms,
            Some("expired at the exact authority boundary"),
            "stationd",
            8,
        )
        .unwrap();
    assert!(store.read_active_takeover(CHANNEL).unwrap().is_none());
    assert_eq!(store.read_takeover_authority_revision(CHANNEL).unwrap(), 9);
    assert_eq!(store.list_channel_live_sources(CHANNEL).unwrap().len(), 1);
    let audit = store.list_takeover_audit(&session.session_id).unwrap();
    assert_eq!(audit.len(), 7);
    assert_eq!(audit[1].operator_id, "stationd");
    assert_eq!(audit[2].operator_id, "operator-scott");
    drop(store);
    remove_database(&database);
}

fn database_path(label: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-live-{label}-{nonce}.db"))
}

fn remove_database(path: &Path) {
    for candidate in [
        path.to_path_buf(),
        PathBuf::from(format!("{}-wal", path.display())),
        PathBuf::from(format!("{}-shm", path.display())),
    ] {
        let _ = std::fs::remove_file(candidate);
    }
}
