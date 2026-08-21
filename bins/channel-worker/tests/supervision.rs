use std::path::PathBuf;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use station_media_journal::read_events;
use station_media_protocol::{ChannelCommand, WorkerEvent};
use station_runtime::WorkerSupervisor;

const WORKER_ID: &str = "256d5a07-92d3-4718-aec9-05cad42fae7d";
const CHANNEL_ID: &str = "8b626c01-bdf8-419a-8a2e-b0a7caa1ff7e";

fn journal_path() -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is before Unix epoch")
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-supervisor-{nonce}.tlj"))
}

#[test]
fn launches_handshakes_commands_and_cleanly_stops_the_real_worker() {
    let journal = journal_path();
    let launched = WorkerSupervisor::launch(
        PathBuf::from(env!("CARGO_BIN_EXE_channel-worker")).as_path(),
        WORKER_ID,
        CHANNEL_ID,
        &journal,
        Duration::from_secs(2),
    );
    assert!(
        launched.is_ok(),
        "supervisor failed to launch the worker: {:?}",
        launched.err()
    );
    let mut supervisor = launched.unwrap();
    let ready = supervisor.ready().clone();
    assert_eq!(ready.sequence, 1);
    assert!(matches!(
        ready.event,
        WorkerEvent::Ready { graph_revision: 0 }
    ));

    let heartbeat = supervisor
        .command("ping-1", ChannelCommand::Ping, Duration::from_secs(1))
        .unwrap();
    assert_eq!(heartbeat.sequence, 2);
    assert!(matches!(heartbeat.event, WorkerEvent::Heartbeat { .. }));

    let applied = supervisor
        .command(
            "plan-1",
            ChannelCommand::ApplyPlan {
                plan_id: "weekday".into(),
                revision: 7,
            },
            Duration::from_secs(1),
        )
        .unwrap();
    assert_eq!(applied.sequence, 3);
    assert!(matches!(
        applied.event,
        WorkerEvent::Ready { graph_revision: 7 }
    ));

    let stopped = supervisor
        .shutdown("shutdown-1", Duration::from_secs(1))
        .unwrap();
    assert_eq!(stopped.sequence, 4);
    assert!(matches!(stopped.event, WorkerEvent::ShutdownComplete));

    let restarted = WorkerSupervisor::launch(
        PathBuf::from(env!("CARGO_BIN_EXE_channel-worker")).as_path(),
        WORKER_ID,
        CHANNEL_ID,
        &journal,
        Duration::from_secs(2),
    )
    .unwrap();
    let restart_ready = restarted.ready().clone();
    assert_eq!(restart_ready.sequence, 5);
    let restart_stopped = restarted
        .shutdown("shutdown-2", Duration::from_secs(1))
        .unwrap();
    assert_eq!(restart_stopped.sequence, 6);

    assert_eq!(
        read_events(&journal).unwrap(),
        vec![
            ready,
            heartbeat,
            applied,
            stopped,
            restart_ready,
            restart_stopped
        ]
    );
    let _ = std::fs::remove_file(journal);
}
