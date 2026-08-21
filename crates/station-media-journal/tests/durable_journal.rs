use std::fs::OpenOptions;
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use station_media_journal::{JournalError, JournalWriter, read_events};
use station_media_protocol::{PROTOCOL_VERSION, WorkerEvent, WorkerEventEnvelope};

const WORKER_ID: &str = "ae819b97-2920-434b-9d96-0b51a8ea9abb";
const CHANNEL_ID: &str = "62a47b7e-2a03-48d6-8703-a6e5ce986527";

fn journal_path(name: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is before Unix epoch")
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-journal-{name}-{nonce}.tlj"))
}

fn event(sequence: u64, event: WorkerEvent) -> WorkerEventEnvelope {
    WorkerEventEnvelope {
        version: PROTOCOL_VERSION,
        worker_id: WORKER_ID.into(),
        channel_id: CHANNEL_ID.into(),
        sequence,
        event,
    }
}

#[test]
fn persists_events_across_reopen_without_sequence_loss() {
    let path = journal_path("reopen");
    let first = event(1, WorkerEvent::Ready { graph_revision: 7 });
    let second = event(
        2,
        WorkerEvent::Heartbeat {
            monotonic_milliseconds: 500,
        },
    );

    let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
    writer.append(&first).unwrap();
    drop(writer);

    let mut reopened = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
    assert_eq!(reopened.next_sequence(), 2);
    reopened.append(&second).unwrap();
    drop(reopened);

    assert_eq!(read_events(&path).unwrap(), vec![first, second]);
    let _ = std::fs::remove_file(path);
}

#[test]
fn rejects_non_monotonic_or_wrong_channel_events_without_writing_them() {
    let path = journal_path("sequence");
    let first = event(1, WorkerEvent::Ready { graph_revision: 1 });
    let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
    writer.append(&first).unwrap();

    let duplicate = event(1, WorkerEvent::ShutdownComplete);
    assert_eq!(
        writer.append(&duplicate),
        Err(JournalError::NonMonotonic {
            expected: 2,
            actual: 1
        })
    );
    let mut wrong_channel = event(2, WorkerEvent::ShutdownComplete);
    wrong_channel.channel_id = "8dfbf23e-eb00-4546-9655-e828ae9ca645".into();
    assert_eq!(
        writer.append(&wrong_channel),
        Err(JournalError::WrongIdentity)
    );
    drop(writer);

    assert_eq!(read_events(&path).unwrap(), vec![first]);
    let _ = std::fs::remove_file(path);
}

#[test]
fn reports_checksum_corruption_instead_of_silently_dropping_an_event() {
    let path = journal_path("corrupt");
    let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
    writer
        .append(&event(1, WorkerEvent::Ready { graph_revision: 1 }))
        .unwrap();
    drop(writer);

    let mut file = OpenOptions::new()
        .read(true)
        .write(true)
        .open(&path)
        .unwrap();
    file.seek(SeekFrom::End(-1)).unwrap();
    let mut byte = [0_u8; 1];
    file.read_exact(&mut byte).unwrap();
    file.seek(SeekFrom::End(-1)).unwrap();
    file.write_all(&[byte[0] ^ 0xff]).unwrap();
    file.sync_all().unwrap();
    drop(file);

    assert!(matches!(
        read_events(&path),
        Err(JournalError::Corrupt { reason, .. }) if reason.contains("checksum")
    ));
    let _ = std::fs::remove_file(path);
}

#[test]
fn reports_a_partially_written_tail_instead_of_accepting_a_shorter_history() {
    let path = journal_path("truncated");
    let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
    writer
        .append(&event(1, WorkerEvent::Ready { graph_revision: 1 }))
        .unwrap();
    drop(writer);

    let file = OpenOptions::new().write(true).open(&path).unwrap();
    let shortened = file.metadata().unwrap().len() - 7;
    file.set_len(shortened).unwrap();
    file.sync_all().unwrap();
    drop(file);

    assert!(matches!(
        read_events(&path),
        Err(JournalError::Corrupt { reason, .. }) if reason.contains("truncated event frame")
    ));
    let _ = std::fs::remove_file(path);
}
