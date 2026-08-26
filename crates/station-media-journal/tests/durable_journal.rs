use std::fs::OpenOptions;
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use station_media_journal::{
    FallbackToneJournalEntry, JournalError, JournalWriter, inspect_journal, read_events,
};
use station_media_protocol::{
    FallbackToneDeactivationReason, FallbackToneKind, OutputTransportObservation, PROTOCOL_VERSION,
    ProtocolError, ProtocolVersion, WorkerEvent, WorkerEventEnvelope, WorkerFallbackTone,
    decode_event_frame, encode_event_frame,
};

const WORKER_ID: &str = "ae819b97-2920-434b-9d96-0b51a8ea9abb";
const CHANNEL_ID: &str = "62a47b7e-2a03-48d6-8703-a6e5ce986527";

fn journal_path(name: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is before Unix epoch")
        .as_nanos();
    std::env::temp_dir().join(format!("townlight-journal-{name}-{nonce}.tlj"))
}

fn compaction_temp_path(path: &Path) -> PathBuf {
    let mut temporary = path.as_os_str().to_owned();
    temporary.push(".compact.tmp");
    temporary.into()
}

fn crc32(bytes: &[u8]) -> u32 {
    let mut checksum = u32::MAX;
    for byte in bytes {
        checksum ^= u32::from(*byte);
        for _ in 0..8 {
            let mask = 0_u32.wrapping_sub(checksum & 1);
            checksum = (checksum >> 1) ^ (0xedb8_8320 & mask);
        }
    }
    !checksum
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

fn persisted_event_frame(sequence: u64, major: u16) -> (WorkerEventEnvelope, Vec<u8>) {
    let mut event = event(sequence, WorkerEvent::Ready { graph_revision: 1 });
    event.version = ProtocolVersion { major, minor: 0 };
    let frame = encode_event_frame(&event).unwrap();
    (event, frame)
}

fn write_event_record(path: &Path, frame: &[u8], journal_version: u16) {
    let mut file = OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .open(path)
        .unwrap();
    file.write_all(b"TLJR").unwrap();
    file.write_all(&journal_version.to_le_bytes()).unwrap();
    file.write_all(&0_u16.to_le_bytes()).unwrap();
    file.write_all(&(frame.len() as u32).to_le_bytes()).unwrap();
    file.write_all(&crc32(frame).to_le_bytes()).unwrap();
    file.write_all(frame).unwrap();
    file.sync_all().unwrap();
}

fn write_fallback_tone_audit_record(path: &Path, entry: &FallbackToneJournalEntry) {
    write_fallback_tone_audit_records(path, std::slice::from_ref(entry));
}

fn write_fallback_tone_audit_records(path: &Path, entries: &[FallbackToneJournalEntry]) {
    let mut file = OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .open(path)
        .unwrap();
    for entry in entries {
        let payload = serde_json::to_vec(entry).unwrap();
        file.write_all(b"TLJR").unwrap();
        file.write_all(&2_u16.to_le_bytes()).unwrap();
        file.write_all(&3_u16.to_le_bytes()).unwrap();
        file.write_all(&(payload.len() as u32).to_le_bytes())
            .unwrap();
        file.write_all(&crc32(&payload).to_le_bytes()).unwrap();
        file.write_all(&payload).unwrap();
    }
    file.sync_all().unwrap();
}

#[test]
fn persists_events_across_reopen_without_sequence_loss() {
    let path = journal_path("reopen");
    let first = event(1, WorkerEvent::Ready { graph_revision: 7 });
    let second = event(
        2,
        WorkerEvent::Heartbeat {
            monotonic_milliseconds: 500,
            outputs: Vec::new(),
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
fn preserves_exact_fallback_tone_activation_and_expiry_audit_across_reopen() {
    let path = journal_path("fallback-tone-audit");
    let tone = WorkerFallbackTone {
        activation_id: "1c63ef7e-6e4b-4fa6-b5d8-c15e2b735c05".into(),
        authority_revision: 3,
        kind: FallbackToneKind::Test,
        frequency_hz: 1_000,
        level_milli_dbfs: -20_000,
        duration_ms: 1_000,
    };
    let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
    writer
        .append_fallback_tone_audit(&FallbackToneJournalEntry::Activated {
            worker_id: WORKER_ID.into(),
            channel_id: CHANNEL_ID.into(),
            tone: tone.clone(),
            activated_at_monotonic_ms: 500,
        })
        .unwrap();
    writer
        .append_fallback_tone_audit(&FallbackToneJournalEntry::Deactivated {
            worker_id: WORKER_ID.into(),
            channel_id: CHANNEL_ID.into(),
            tone: tone.clone(),
            reason: FallbackToneDeactivationReason::Expired,
            deactivated_at_monotonic_ms: 1_500,
        })
        .unwrap();
    drop(writer);

    assert_eq!(
        inspect_journal(&path).unwrap().fallback_tone_audit,
        vec![
            FallbackToneJournalEntry::Activated {
                worker_id: WORKER_ID.into(),
                channel_id: CHANNEL_ID.into(),
                tone: tone.clone(),
                activated_at_monotonic_ms: 500,
            },
            FallbackToneJournalEntry::Deactivated {
                worker_id: WORKER_ID.into(),
                channel_id: CHANNEL_ID.into(),
                tone,
                reason: FallbackToneDeactivationReason::Expired,
                deactivated_at_monotonic_ms: 1_500,
            },
        ]
    );
    let _ = std::fs::remove_file(path);
}

#[test]
fn recovers_one_active_fallback_tone_and_allows_a_new_activation() {
    let path = journal_path("fallback-tone-recovery");
    let tone = WorkerFallbackTone {
        activation_id: "1c63ef7e-6e4b-4fa6-b5d8-c15e2b735c05".into(),
        authority_revision: 3,
        kind: FallbackToneKind::Test,
        frequency_hz: 1_000,
        level_milli_dbfs: -20_000,
        duration_ms: 1_000,
    };
    let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
    writer
        .append_fallback_tone_audit(&FallbackToneJournalEntry::Activated {
            worker_id: WORKER_ID.into(),
            channel_id: CHANNEL_ID.into(),
            tone: tone.clone(),
            activated_at_monotonic_ms: 500,
        })
        .unwrap();
    assert_eq!(
        writer.recover_active_fallback_tones().unwrap(),
        vec![(tone.clone(), 500)]
    );
    writer
        .append_fallback_tone_audit(&FallbackToneJournalEntry::Activated {
            worker_id: WORKER_ID.into(),
            channel_id: CHANNEL_ID.into(),
            tone: tone.clone(),
            activated_at_monotonic_ms: 1,
        })
        .unwrap();
    assert!(matches!(
        writer.append_fallback_tone_audit(&FallbackToneJournalEntry::Activated {
            worker_id: WORKER_ID.into(),
            channel_id: CHANNEL_ID.into(),
            tone: tone.clone(),
            activated_at_monotonic_ms: 2,
        }),
        Err(JournalError::InvalidFallbackToneAudit(_))
    ));
    drop(writer);

    let audit = inspect_journal(&path).unwrap().fallback_tone_audit;
    assert!(matches!(
        &audit[1],
        FallbackToneJournalEntry::Deactivated {
            reason: FallbackToneDeactivationReason::Recovery,
            deactivated_at_monotonic_ms: 500,
            ..
        }
    ));
    let _ = std::fs::remove_file(path);
}

#[test]
fn rejects_fallback_tone_audits_with_invalid_tones_or_inconsistent_expiry() {
    let path = journal_path("invalid-fallback-tone-audit");
    let mut tone = WorkerFallbackTone {
        activation_id: "1c63ef7e-6e4b-4fa6-b5d8-c15e2b735c05".into(),
        authority_revision: 3,
        kind: FallbackToneKind::Test,
        frequency_hz: 1_000,
        level_milli_dbfs: -20_000,
        duration_ms: 1_000,
    };
    let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
    tone.frequency_hz = 1_501;
    assert!(
        writer
            .append_fallback_tone_audit(&FallbackToneJournalEntry::Activated {
                worker_id: WORKER_ID.into(),
                channel_id: CHANNEL_ID.into(),
                tone: tone.clone(),
                activated_at_monotonic_ms: 500,
            })
            .is_err()
    );

    tone.frequency_hz = 1_000;
    writer
        .append_fallback_tone_audit(&FallbackToneJournalEntry::Activated {
            worker_id: WORKER_ID.into(),
            channel_id: CHANNEL_ID.into(),
            tone: tone.clone(),
            activated_at_monotonic_ms: 500,
        })
        .unwrap();
    assert!(
        writer
            .append_fallback_tone_audit(&FallbackToneJournalEntry::Deactivated {
                worker_id: WORKER_ID.into(),
                channel_id: CHANNEL_ID.into(),
                tone,
                reason: FallbackToneDeactivationReason::Expired,
                deactivated_at_monotonic_ms: 1_499,
            })
            .is_err()
    );
    drop(writer);
    let _ = std::fs::remove_file(path);
}

#[test]
fn replay_rejects_an_invalid_fallback_tone_audit_record() {
    let path = journal_path("invalid-fallback-tone-audit-replay");
    write_fallback_tone_audit_record(
        &path,
        &FallbackToneJournalEntry::Activated {
            worker_id: WORKER_ID.into(),
            channel_id: CHANNEL_ID.into(),
            tone: WorkerFallbackTone {
                activation_id: "1c63ef7e-6e4b-4fa6-b5d8-c15e2b735c05".into(),
                authority_revision: 3,
                kind: FallbackToneKind::Test,
                frequency_hz: 1_501,
                level_milli_dbfs: -20_000,
                duration_ms: 1_000,
            },
            activated_at_monotonic_ms: 500,
        },
    );

    assert!(matches!(
        inspect_journal(&path),
        Err(JournalError::Corrupt { reason, .. }) if reason.contains("invalid fallback tone audit")
    ));
    let _ = std::fs::remove_file(path);
}

#[test]
fn rejects_a_second_active_fallback_tone_on_append_and_replay() {
    let path = journal_path("multiple-active-fallback-tones");
    let first = WorkerFallbackTone {
        activation_id: "1c63ef7e-6e4b-4fa6-b5d8-c15e2b735c05".into(),
        authority_revision: 3,
        kind: FallbackToneKind::Test,
        frequency_hz: 1_000,
        level_milli_dbfs: -20_000,
        duration_ms: 1_000,
    };
    let mut second = first.clone();
    second.activation_id = "2c63ef7e-6e4b-4fa6-b5d8-c15e2b735c05".into();
    second.authority_revision = 4;
    let first_entry = FallbackToneJournalEntry::Activated {
        worker_id: WORKER_ID.into(),
        channel_id: CHANNEL_ID.into(),
        tone: first,
        activated_at_monotonic_ms: 1,
    };
    let second_entry = FallbackToneJournalEntry::Activated {
        worker_id: WORKER_ID.into(),
        channel_id: CHANNEL_ID.into(),
        tone: second,
        activated_at_monotonic_ms: 2,
    };
    let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
    writer.append_fallback_tone_audit(&first_entry).unwrap();
    assert!(matches!(
        writer.append_fallback_tone_audit(&second_entry),
        Err(JournalError::InvalidFallbackToneAudit(_))
    ));
    drop(writer);
    let _ = std::fs::remove_file(&path);

    write_fallback_tone_audit_records(&path, &[first_entry, second_entry]);
    assert!(
        matches!(inspect_journal(&path), Err(JournalError::Corrupt { reason, .. }) if reason.contains("another tone is active"))
    );
    let _ = std::fs::remove_file(path);
}

#[test]
fn replay_fails_closed_when_fallback_tone_expiry_overflows() {
    let path = journal_path("fallback-tone-expiry-overflow");
    let tone = WorkerFallbackTone {
        activation_id: "1c63ef7e-6e4b-4fa6-b5d8-c15e2b735c05".into(),
        authority_revision: 3,
        kind: FallbackToneKind::Test,
        frequency_hz: 1_000,
        level_milli_dbfs: -20_000,
        duration_ms: 1_000,
    };
    write_fallback_tone_audit_records(
        &path,
        &[
            FallbackToneJournalEntry::Activated {
                worker_id: WORKER_ID.into(),
                channel_id: CHANNEL_ID.into(),
                tone: tone.clone(),
                activated_at_monotonic_ms: u64::MAX,
            },
            FallbackToneJournalEntry::Deactivated {
                worker_id: WORKER_ID.into(),
                channel_id: CHANNEL_ID.into(),
                tone,
                reason: FallbackToneDeactivationReason::Expired,
                deactivated_at_monotonic_ms: u64::MAX,
            },
        ],
    );
    assert!(
        matches!(inspect_journal(&path), Err(JournalError::Corrupt { reason, .. }) if reason.contains("expiry overflows"))
    );
    let _ = std::fs::remove_file(path);
}

#[cfg(windows)]
#[test]
fn refuses_a_second_writer_until_the_first_owner_retires() {
    let path = journal_path("exclusive-writer");
    let first = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();

    // The sidecar is opened with an exclusive Windows handle, so this is the
    // same rejection a replacement worker in another process receives.
    assert!(matches!(
        JournalWriter::open(&path, WORKER_ID, CHANNEL_ID),
        Err(JournalError::WriterAlreadyActive)
    ));

    drop(first);
    let replacement = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
    drop(replacement);
    let _ = std::fs::remove_file(path);
}

#[test]
fn reopens_and_replays_an_immediately_previous_major_durable_event() {
    let path = journal_path("legacy-v8-replay");
    let (legacy, frame) = persisted_event_frame(1, PROTOCOL_VERSION.major - 1);
    write_event_record(&path, &frame, 1);

    let mut reopened = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
    assert_eq!(reopened.next_sequence(), 2);
    assert_eq!(read_events(&path).unwrap(), vec![legacy.clone()]);

    let current = event(2, WorkerEvent::ShutdownComplete);
    reopened.append(&current).unwrap();
    drop(reopened);
    assert_eq!(read_events(&path).unwrap(), vec![legacy, current]);
    let _ = std::fs::remove_file(path);
}

#[test]
fn durable_replay_rejects_older_and_future_protocol_majors_while_ipc_rejects_v8() {
    for (label, major) in [("legacy-v9", 9_u16), ("future-v12", 12_u16)] {
        let path = journal_path(label);
        let (_, frame) = persisted_event_frame(1, major);
        write_event_record(&path, &frame, 1);
        assert_eq!(
            read_events(&path),
            Err(JournalError::Protocol(ProtocolError::UnsupportedMajor(
                major,
            )))
        );
        assert!(matches!(
            JournalWriter::open(&path, WORKER_ID, CHANNEL_ID),
            Err(JournalError::Protocol(ProtocolError::UnsupportedMajor(actual)))
                if actual == major
        ));
        let _ = std::fs::remove_file(path);
    }

    let (_, v8_frame) = persisted_event_frame(1, PROTOCOL_VERSION.major - 1);
    assert_eq!(
        decode_event_frame(&v8_frame),
        Err(ProtocolError::UnsupportedMajor(PROTOCOL_VERSION.major - 1))
    );
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

#[test]
fn bounds_raw_heartbeat_growth_without_losing_transition_or_sequence_truth() {
    let path = journal_path("heartbeat-retention");
    let ready = event(1, WorkerEvent::Ready { graph_revision: 1 });
    let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
    writer.append(&ready).unwrap();
    for sequence in 2..=701 {
        writer
            .append(&event(
                sequence,
                WorkerEvent::Heartbeat {
                    monotonic_milliseconds: sequence * 5_000,
                    outputs: Vec::new(),
                },
            ))
            .unwrap();
    }
    let transition = event(
        702,
        WorkerEvent::OnAirChanged {
            source_kind: "asset".into(),
            source_id: "retained-asset".into(),
        },
    );
    writer.append(&transition).unwrap();
    drop(writer);

    let retained = read_events(&path).unwrap();
    let retained_heartbeats = retained
        .iter()
        .filter(|event| matches!(event.event, WorkerEvent::Heartbeat { .. }))
        .count();
    assert!(
        retained_heartbeats <= 512,
        "journal retained {retained_heartbeats} raw heartbeats"
    );
    assert!(retained.contains(&ready));
    assert!(retained.contains(&transition));

    let inspection = inspect_journal(&path).unwrap();
    assert_eq!(inspection.worker_id.as_deref(), Some(WORKER_ID));
    assert_eq!(inspection.channel_id.as_deref(), Some(CHANNEL_ID));
    assert_eq!(inspection.next_sequence, 703);
    assert_eq!(inspection.compacted_heartbeat_ranges[0].first_sequence, 2);
    assert!(
        inspection.compacted_heartbeat_ranges[0]
            .digest_chain
            .iter()
            .any(|byte| *byte != 0)
    );

    let reopened = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
    assert_eq!(reopened.next_sequence(), 703);
    drop(reopened);
    let _ = std::fs::remove_file(path);
}

#[test]
fn refuses_a_retained_sequence_gap_when_the_compaction_summary_is_missing() {
    let path = journal_path("missing-summary");
    let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
    writer
        .append(&event(1, WorkerEvent::Ready { graph_revision: 1 }))
        .unwrap();
    for sequence in 2..=701 {
        writer
            .append(&event(
                sequence,
                WorkerEvent::Heartbeat {
                    monotonic_milliseconds: sequence * 5_000,
                    outputs: Vec::new(),
                },
            ))
            .unwrap();
    }
    drop(writer);

    let bytes = std::fs::read(&path).unwrap();
    assert_eq!(u16::from_le_bytes(bytes[4..6].try_into().unwrap()), 2);
    assert_eq!(u16::from_le_bytes(bytes[6..8].try_into().unwrap()), 1);
    let summary_bytes = 16 + u32::from_le_bytes(bytes[8..12].try_into().unwrap()) as usize;
    let mut without_compaction_records = Vec::new();
    let mut cursor = 0_usize;
    while cursor < bytes.len() {
        let kind = u16::from_le_bytes(bytes[cursor + 6..cursor + 8].try_into().unwrap());
        let payload = u32::from_le_bytes(bytes[cursor + 8..cursor + 12].try_into().unwrap());
        let next = cursor + 16 + payload as usize;
        if kind == 0 {
            without_compaction_records.extend_from_slice(&bytes[cursor..next]);
        }
        cursor = next;
    }
    assert!(summary_bytes < bytes.len());
    std::fs::write(&path, without_compaction_records).unwrap();

    assert_eq!(
        read_events(&path),
        Err(JournalError::NonMonotonic {
            expected: 2,
            actual: 259,
        })
    );
    let _ = std::fs::remove_file(path);
}

#[test]
fn refuses_a_compacted_active_journal_without_its_commit_record() {
    let path = journal_path("missing-compaction-commit");
    let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
    writer
        .append(&event(1, WorkerEvent::Ready { graph_revision: 1 }))
        .unwrap();
    for sequence in 2..=514 {
        writer
            .append(&event(
                sequence,
                WorkerEvent::Heartbeat {
                    monotonic_milliseconds: sequence * 5_000,
                    outputs: Vec::new(),
                },
            ))
            .unwrap();
    }
    drop(writer);

    let bytes = std::fs::read(&path).unwrap();
    let mut without_commit = Vec::new();
    let mut cursor = 0_usize;
    while cursor < bytes.len() {
        let kind = u16::from_le_bytes(bytes[cursor + 6..cursor + 8].try_into().unwrap());
        let payload = u32::from_le_bytes(bytes[cursor + 8..cursor + 12].try_into().unwrap());
        let next = cursor + 16 + payload as usize;
        if kind != 2 {
            without_commit.extend_from_slice(&bytes[cursor..next]);
        }
        cursor = next;
    }
    std::fs::write(&path, without_commit).unwrap();

    assert!(matches!(
        inspect_journal(&path),
        Err(JournalError::Corrupt { reason, .. }) if reason.contains("no compaction commit")
    ));
    let _ = std::fs::remove_file(path);
}

#[test]
fn recovers_a_synced_compaction_temp_when_the_active_file_is_missing() {
    let path = journal_path("recover-temp");
    let temporary = compaction_temp_path(&path);
    let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
    writer
        .append(&event(1, WorkerEvent::Ready { graph_revision: 1 }))
        .unwrap();
    for sequence in 2..=520 {
        writer
            .append(&event(
                sequence,
                WorkerEvent::Heartbeat {
                    monotonic_milliseconds: sequence * 5_000,
                    outputs: Vec::new(),
                },
            ))
            .unwrap();
    }
    drop(writer);
    std::fs::copy(&path, &temporary).unwrap();
    std::fs::remove_file(&path).unwrap();

    let recovered = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
    assert_eq!(recovered.next_sequence(), 521);
    assert!(path.is_file());
    assert!(!temporary.exists());
    drop(recovered);
    let _ = std::fs::remove_file(path);
}

#[test]
fn does_not_mask_active_corruption_with_a_stale_compaction_temp() {
    let path = journal_path("corrupt-active-with-temp");
    let temporary = compaction_temp_path(&path);
    let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
    writer
        .append(&event(1, WorkerEvent::Ready { graph_revision: 1 }))
        .unwrap();
    drop(writer);
    std::fs::copy(&path, &temporary).unwrap();
    let mut bytes = std::fs::read(&path).unwrap();
    let last = bytes.last_mut().unwrap();
    *last ^= 0xff;
    std::fs::write(&path, bytes).unwrap();

    assert!(matches!(
        JournalWriter::open(&path, WORKER_ID, CHANNEL_ID),
        Err(JournalError::Corrupt { reason, .. }) if reason.contains("checksum")
    ));
    assert!(temporary.is_file());
    let _ = std::fs::remove_file(path);
    let _ = std::fs::remove_file(temporary);
}

#[test]
fn does_not_promote_a_wrong_identity_compaction_temp() {
    let path = journal_path("wrong-identity-temp");
    let temporary = compaction_temp_path(&path);
    let wrong_worker = "086a55f6-e6fc-4940-9234-da67b8a67ac9";
    let mut writer = JournalWriter::open(&temporary, wrong_worker, CHANNEL_ID).unwrap();
    let mut wrong_event = event(1, WorkerEvent::Ready { graph_revision: 1 });
    wrong_event.worker_id = wrong_worker.into();
    writer.append(&wrong_event).unwrap();
    for sequence in 2..=520 {
        let mut wrong_heartbeat = event(
            sequence,
            WorkerEvent::Heartbeat {
                monotonic_milliseconds: sequence * 5_000,
                outputs: Vec::new(),
            },
        );
        wrong_heartbeat.worker_id = wrong_worker.into();
        writer.append(&wrong_heartbeat).unwrap();
    }
    drop(writer);

    assert!(matches!(
        JournalWriter::open(&path, WORKER_ID, CHANNEL_ID),
        Err(JournalError::WrongIdentity)
    ));
    assert!(
        !path.exists(),
        "wrong-identity temp became the active journal"
    );
    assert!(temporary.is_file());
    let _ = std::fs::remove_file(temporary);
}

#[test]
fn does_not_promote_an_empty_uncommitted_compaction_temp() {
    let path = journal_path("empty-uncommitted-temp");
    let temporary = compaction_temp_path(&path);
    std::fs::write(&temporary, []).unwrap();

    assert!(matches!(
        JournalWriter::open(&path, WORKER_ID, CHANNEL_ID),
        Err(JournalError::Corrupt { reason, .. }) if reason.contains("committed compaction")
    ));
    assert!(
        !path.exists(),
        "empty uncommitted temp became the active journal"
    );
    assert!(temporary.is_file());
    let _ = std::fs::remove_file(temporary);
}

#[test]
fn does_not_promote_a_truncated_compaction_commit() {
    let path = journal_path("truncated-compaction-commit");
    let temporary = compaction_temp_path(&path);
    let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
    writer
        .append(&event(1, WorkerEvent::Ready { graph_revision: 1 }))
        .unwrap();
    for sequence in 2..=514 {
        writer
            .append(&event(
                sequence,
                WorkerEvent::Heartbeat {
                    monotonic_milliseconds: sequence * 5_000,
                    outputs: Vec::new(),
                },
            ))
            .unwrap();
    }
    drop(writer);
    std::fs::rename(&path, &temporary).unwrap();
    let file = OpenOptions::new().write(true).open(&temporary).unwrap();
    file.set_len(file.metadata().unwrap().len() - 1).unwrap();
    file.sync_all().unwrap();
    drop(file);

    assert!(matches!(
        JournalWriter::open(&path, WORKER_ID, CHANNEL_ID),
        Err(JournalError::Corrupt { reason, .. }) if reason.contains("truncated event frame")
    ));
    assert!(!path.exists());
    assert!(temporary.is_file());
    let _ = std::fs::remove_file(temporary);
}

#[test]
fn reopens_a_legacy_v1_journal_and_appends_v2_without_sequence_loss() {
    let path = journal_path("legacy-v1");
    let first = event(1, WorkerEvent::Ready { graph_revision: 1 });
    let second = event(
        2,
        WorkerEvent::Heartbeat {
            monotonic_milliseconds: 5_000,
            outputs: Vec::new(),
        },
    );
    let third = event(3, WorkerEvent::ShutdownComplete);
    let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
    writer.append(&first).unwrap();
    writer.append(&second).unwrap();
    drop(writer);

    let mut bytes = std::fs::read(&path).unwrap();
    let mut offset = 0_usize;
    while offset < bytes.len() {
        bytes[offset + 4..offset + 6].copy_from_slice(&1_u16.to_le_bytes());
        let payload = u32::from_le_bytes(bytes[offset + 8..offset + 12].try_into().unwrap());
        offset += 16 + payload as usize;
    }
    std::fs::write(&path, bytes).unwrap();

    let mut reopened = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
    assert_eq!(reopened.next_sequence(), 3);
    reopened.append(&third).unwrap();
    drop(reopened);
    assert_eq!(read_events(&path).unwrap(), vec![first, second, third]);
    let _ = std::fs::remove_file(path);
}

#[test]
fn repeated_compaction_retains_every_sparse_as_run_transition() {
    let path = journal_path("repeated-compaction");
    let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
    writer
        .append(&event(1, WorkerEvent::Ready { graph_revision: 1 }))
        .unwrap();
    let mut transitions = Vec::new();
    for sequence in 2..=1_601 {
        let entry = if sequence % 400 == 0 {
            let transition = event(
                sequence,
                WorkerEvent::OnAirChanged {
                    source_kind: "asset".into(),
                    source_id: format!("asset-{sequence}"),
                },
            );
            transitions.push(transition.clone());
            transition
        } else {
            event(
                sequence,
                WorkerEvent::Heartbeat {
                    monotonic_milliseconds: sequence * 5_000,
                    outputs: Vec::new(),
                },
            )
        };
        writer.append(&entry).unwrap();
    }
    drop(writer);

    let retained = read_events(&path).unwrap();
    for transition in &transitions {
        assert!(retained.contains(transition));
    }
    assert!(
        retained
            .iter()
            .filter(|event| matches!(event.event, WorkerEvent::Heartbeat { .. }))
            .count()
            <= 512
    );
    assert!(
        retained.len() < 600,
        "retained {} raw events",
        retained.len()
    );
    let reopened = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
    assert_eq!(reopened.next_sequence(), 1_602);
    drop(reopened);
    let _ = std::fs::remove_file(path);
}

#[test]
fn splits_many_sparse_heartbeat_ranges_across_bounded_summary_records() {
    let path = journal_path("bounded-summary-records");
    let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
    writer
        .append(&event(1, WorkerEvent::Ready { graph_revision: 1 }))
        .unwrap();
    let mut sequence = 2_u64;
    for heartbeat in 0..700_u64 {
        writer
            .append(&event(
                sequence,
                WorkerEvent::Heartbeat {
                    monotonic_milliseconds: heartbeat * 5_000,
                    outputs: Vec::new(),
                },
            ))
            .unwrap();
        sequence += 1;
        writer
            .append(&event(
                sequence,
                WorkerEvent::OnAirChanged {
                    source_kind: "asset".into(),
                    source_id: format!("asset-{heartbeat}"),
                },
            ))
            .unwrap();
        sequence += 1;
    }
    drop(writer);

    let bytes = std::fs::read(&path).unwrap();
    let mut offset = 0_usize;
    let mut summary_records = 0_usize;
    while offset < bytes.len() {
        let kind = u16::from_le_bytes(bytes[offset + 6..offset + 8].try_into().unwrap());
        let payload = u32::from_le_bytes(bytes[offset + 8..offset + 12].try_into().unwrap());
        summary_records += usize::from(kind == 1);
        offset += 16 + payload as usize;
    }
    assert!(
        summary_records > 1,
        "expected bounded summary chunks, found {summary_records}"
    );
    let reopened = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
    assert_eq!(reopened.next_sequence(), sequence);
    drop(reopened);
    let _ = std::fs::remove_file(path);
}

#[test]
fn rejects_summary_output_endpoints_that_change_identity() {
    let path = journal_path("invalid-summary-outputs");
    let mut writer = JournalWriter::open(&path, WORKER_ID, CHANNEL_ID).unwrap();
    writer
        .append(&event(1, WorkerEvent::Ready { graph_revision: 1 }))
        .unwrap();
    for sequence in 2..=520 {
        writer
            .append(&event(
                sequence,
                WorkerEvent::Heartbeat {
                    monotonic_milliseconds: sequence * 5_000,
                    outputs: vec![OutputTransportObservation {
                        output_index: 0,
                        bytes_sent_total: sequence * 1_000,
                        audio_buffers_total: sequence * 10,
                    }],
                },
            ))
            .unwrap();
    }
    drop(writer);

    let bytes = std::fs::read(&path).unwrap();
    let payload_length = u32::from_le_bytes(bytes[8..12].try_into().unwrap()) as usize;
    let mut summary: serde_json::Value =
        serde_json::from_slice(&bytes[16..16 + payload_length]).unwrap();
    summary["ranges"][0]["last_outputs"][0]["output_index"] = 1.into();
    let payload = serde_json::to_vec(&summary).unwrap();
    let mut corrupted = Vec::with_capacity(bytes.len() - payload_length + payload.len());
    corrupted.extend_from_slice(&bytes[..8]);
    corrupted.extend_from_slice(&(payload.len() as u32).to_le_bytes());
    corrupted.extend_from_slice(&crc32(&payload).to_le_bytes());
    corrupted.extend_from_slice(&payload);
    corrupted.extend_from_slice(&bytes[16 + payload_length..]);
    std::fs::write(&path, corrupted).unwrap();

    assert!(matches!(
        inspect_journal(&path),
        Err(JournalError::Corrupt { reason, .. }) if reason.contains("output endpoints")
    ));
    let _ = std::fs::remove_file(path);
}
