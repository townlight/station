use sha2::{Digest, Sha256};
use station_maintenance::{
    ACTIVE_SLOT_RECEIPT_NAME, ACTIVE_SLOT_SCHEMA_VERSION, ActiveApplication, ActiveSlotReceipt,
    INSTALL_RECEIPT_NAME, MAINTENANCE_SCHEMA_VERSION, MaintenanceHost, MaintenancePhase,
    MaintenanceRequest, TRANSACTION_DIRECTORY_NAME, activate_prepared_slot,
    latest_maintenance_transaction, read_active_slot_receipt, recover_incomplete_transaction,
};
use station_update::{
    PreparedSlotRecord, ReleaseManifest, UPDATE_PACKAGE_SCHEMA_VERSION, UpdatePackageFile,
    UpdatePackageManifest,
};
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const NOW: i64 = 1_800_000_000_000;
const CURRENT_COMMIT: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const CANDIDATE_COMMIT: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

#[derive(Default)]
struct FakeHost {
    operations: Vec<String>,
    fail_candidate_proof: bool,
    fail_previous_proof: bool,
    panic_candidate_proof: bool,
    fail_stop_on_call: Option<usize>,
    stop_calls: usize,
    mutate_candidate_journal: bool,
    journal_directory: Option<PathBuf>,
    candidate_configured: bool,
}

impl MaintenanceHost for FakeHost {
    fn stop_station(&mut self, _timeout: Duration) -> Result<(), String> {
        self.stop_calls += 1;
        self.operations.push("stop".into());
        if self.fail_stop_on_call == Some(self.stop_calls) {
            Err("injected stop failure".into())
        } else {
            Ok(())
        }
    }

    fn configure_station(
        &mut self,
        stationd_path: &Path,
        _database_path: &Path,
    ) -> Result<(), String> {
        self.operations
            .push(format!("configure:{}", stationd_path.display()));
        self.candidate_configured = stationd_path.display().to_string().contains("slot-0.2.0");
        Ok(())
    }

    fn start_station(&mut self) -> Result<(), String> {
        self.operations.push("start".into());
        if self.mutate_candidate_journal && self.candidate_configured {
            let directory = self.journal_directory.as_ref().unwrap();
            std::fs::write(directory.join("channel-a.tlj"), candidate_v2_journal()).unwrap();
            std::fs::write(directory.join("candidate-created.tlj"), b"candidate-only").unwrap();
        }
        Ok(())
    }

    fn prove_station(
        &mut self,
        expected: &ActiveApplication,
        _timeout: Duration,
    ) -> Result<(), String> {
        let version = expected.package_manifest.version.clone();
        self.operations.push(format!("prove:{version}"));
        if version == "0.2.0" && self.panic_candidate_proof {
            panic!("injected crash during candidate proof");
        }
        if (version == "0.2.0" && self.fail_candidate_proof)
            || (version == "0.1.0" && self.fail_previous_proof)
        {
            Err(format!("proof failed for {version}"))
        } else {
            Ok(())
        }
    }
}

#[test]
fn commits_only_after_candidate_proof_and_records_the_previous_application() {
    let fixture = fixture();
    let mut host = FakeHost::default();
    let result = activate_prepared_slot(
        &fixture.request,
        &fixture.database,
        &fixture.application,
        NOW + 10,
        &mut host,
    )
    .unwrap();

    assert_eq!(result.phase, MaintenancePhase::Committed);
    assert_eq!(
        host.operations,
        vec![
            "stop",
            &format!(
                "configure:{}",
                fixture
                    .application
                    .join("slots")
                    .join(&fixture.request.prepared_slot.slot_directory_name)
                    .join("stationd.exe")
                    .display()
            ),
            "start",
            "prove:0.2.0",
        ]
    );
    let active = read_active_slot_receipt(&fixture.data.join(ACTIVE_SLOT_RECEIPT_NAME)).unwrap();
    assert_eq!(active.generation, 2);
    assert_eq!(active.active.package_manifest.version, "0.2.0");
    assert_eq!(active.previous.unwrap().package_manifest.version, "0.1.0");
    let install: serde_json::Value =
        serde_json::from_slice(&std::fs::read(fixture.data.join(INSTALL_RECEIPT_NAME)).unwrap())
            .unwrap();
    assert_eq!(install["version"], "0.2.0");
    assert_eq!(install["media_runtime_sha256"], "C".repeat(64));
    assert_eq!(
        latest_maintenance_transaction(&fixture.data)
            .unwrap()
            .unwrap()
            .phase,
        MaintenancePhase::Committed
    );
    cleanup(&fixture.root);
}

#[test]
fn restores_and_proves_the_previous_application_when_candidate_proof_fails() {
    let fixture = fixture();
    let original_receipt = std::fs::read(fixture.data.join(INSTALL_RECEIPT_NAME)).unwrap();
    let mut host = FakeHost {
        fail_candidate_proof: true,
        ..FakeHost::default()
    };
    let result = activate_prepared_slot(
        &fixture.request,
        &fixture.database,
        &fixture.application,
        NOW + 10,
        &mut host,
    )
    .unwrap();

    assert_eq!(result.phase, MaintenancePhase::RolledBack);
    assert_eq!(host.operations.last().unwrap(), "prove:0.1.0");
    assert_eq!(
        read_active_slot_receipt(&fixture.data.join(ACTIVE_SLOT_RECEIPT_NAME))
            .unwrap()
            .active
            .package_manifest
            .version,
        "0.1.0"
    );
    assert_eq!(
        std::fs::read(fixture.data.join(INSTALL_RECEIPT_NAME)).unwrap(),
        original_receipt
    );
    cleanup(&fixture.root);
}

#[test]
fn restores_the_channel_journals_byte_for_byte_and_removes_candidate_files_on_rollback() {
    let fixture = fixture();
    let original =
        std::fs::read(fixture.data.join("channel-journals").join("channel-a.tlj")).unwrap();
    let mut host = FakeHost {
        fail_candidate_proof: true,
        mutate_candidate_journal: true,
        journal_directory: Some(fixture.data.join("channel-journals")),
        ..FakeHost::default()
    };

    let result = activate_prepared_slot(
        &fixture.request,
        &fixture.database,
        &fixture.application,
        NOW + 10,
        &mut host,
    )
    .unwrap();

    assert_eq!(result.phase, MaintenancePhase::RolledBack);
    assert_eq!(
        std::fs::read(fixture.data.join("channel-journals").join("channel-a.tlj")).unwrap(),
        original
    );
    assert_eq!(v1_only_sequence_count(&original), 520);
    assert!(
        !fixture
            .data
            .join("channel-journals")
            .join("candidate-created.tlj")
            .exists()
    );
    cleanup(&fixture.root);
}

#[test]
fn failed_candidate_stop_never_touches_the_live_journal_tree() {
    let fixture = fixture();
    let mut host = FakeHost {
        fail_candidate_proof: true,
        fail_stop_on_call: Some(2),
        mutate_candidate_journal: true,
        journal_directory: Some(fixture.data.join("channel-journals")),
        ..FakeHost::default()
    };

    assert!(
        activate_prepared_slot(
            &fixture.request,
            &fixture.database,
            &fixture.application,
            NOW + 10,
            &mut host,
        )
        .is_err()
    );
    assert_eq!(
        std::fs::read(fixture.data.join("channel-journals/channel-a.tlj")).unwrap(),
        candidate_v2_journal()
    );
    assert!(
        fixture
            .data
            .join("channel-journals/candidate-created.tlj")
            .is_file()
    );
    assert_eq!(
        latest_maintenance_transaction(&fixture.data)
            .unwrap()
            .unwrap()
            .phase,
        MaintenancePhase::RollbackFailed
    );
    cleanup(&fixture.root);
}

#[test]
fn refuses_to_start_candidate_when_channel_journal_snapshot_fails() {
    let fixture = fixture();
    std::fs::remove_dir_all(fixture.data.join("channel-journals")).unwrap();
    std::fs::write(fixture.data.join("channel-journals"), b"not-a-directory").unwrap();
    let mut host = FakeHost::default();

    let result = activate_prepared_slot(
        &fixture.request,
        &fixture.database,
        &fixture.application,
        NOW + 10,
        &mut host,
    );

    assert!(result.is_ok());
    assert!(
        !host
            .operations
            .iter()
            .any(|operation| operation.contains("slot-0.2.0"))
    );
    assert!(
        !host
            .operations
            .iter()
            .any(|operation| operation == "prove:0.2.0")
    );
    cleanup(&fixture.root);
}

#[test]
fn discards_the_journal_snapshot_only_after_durable_commit() {
    let fixture = fixture();
    let mut host = FakeHost::default();
    let result = activate_prepared_slot(
        &fixture.request,
        &fixture.database,
        &fixture.application,
        NOW + 10,
        &mut host,
    )
    .unwrap();

    assert_eq!(result.phase, MaintenancePhase::Committed);
    let transaction_directory = fixture
        .data
        .join("updates")
        .join(TRANSACTION_DIRECTORY_NAME);
    assert_eq!(
        std::fs::read_dir(transaction_directory)
            .unwrap()
            .filter_map(Result::ok)
            .count(),
        1
    );
    cleanup(&fixture.root);
}

#[test]
fn restores_journals_when_recovery_resumes_from_commit_started() {
    let fixture = fixture();
    let original =
        std::fs::read(fixture.data.join("channel-journals").join("channel-a.tlj")).unwrap();
    let mut failing = FakeHost {
        panic_candidate_proof: true,
        mutate_candidate_journal: true,
        journal_directory: Some(fixture.data.join("channel-journals")),
        ..FakeHost::default()
    };
    let crash = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let _ = activate_prepared_slot(
            &fixture.request,
            &fixture.database,
            &fixture.application,
            NOW + 10,
            &mut failing,
        );
    }));
    assert!(crash.is_err());
    let transaction_path = fixture
        .data
        .join("updates")
        .join(TRANSACTION_DIRECTORY_NAME)
        .join(format!("{}.json", fixture.request.transaction_id));
    let mut transaction: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&transaction_path).unwrap()).unwrap();
    transaction["phase"] = "commit_started".into();
    std::fs::write(&transaction_path, serde_json::to_vec(&transaction).unwrap()).unwrap();
    std::fs::write(
        fixture.data.join("channel-journals").join("channel-a.tlj"),
        candidate_v2_journal(),
    )
    .unwrap();
    std::fs::write(
        fixture
            .data
            .join("channel-journals")
            .join("candidate-created.tlj"),
        b"candidate-only",
    )
    .unwrap();

    let mut recovering = FakeHost::default();
    let recovered = recover_incomplete_transaction(
        &transaction_path,
        &fixture.database,
        &fixture.application,
        NOW + 20,
        &mut recovering,
    )
    .unwrap();

    assert_eq!(recovered.phase, MaintenancePhase::RolledBack);
    assert_eq!(
        std::fs::read(fixture.data.join("channel-journals").join("channel-a.tlj")).unwrap(),
        original
    );
    assert!(
        !fixture
            .data
            .join("channel-journals")
            .join("candidate-created.tlj")
            .exists()
    );
    cleanup(&fixture.root);
}

#[test]
fn recovers_prior_application_after_a_crash_during_snapshot_publication() {
    let fixture = fixture();
    let mut crashing = FakeHost {
        panic_candidate_proof: true,
        ..FakeHost::default()
    };
    let crash = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let _ = activate_prepared_slot(
            &fixture.request,
            &fixture.database,
            &fixture.application,
            NOW + 10,
            &mut crashing,
        );
    }));
    assert!(crash.is_err());
    let transaction_path = fixture
        .data
        .join("updates")
        .join(TRANSACTION_DIRECTORY_NAME)
        .join(format!("{}.json", fixture.request.transaction_id));
    let mut transaction: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&transaction_path).unwrap()).unwrap();
    let snapshot_directory = transaction_path.parent().unwrap().join(
        transaction["journal_snapshot"]["relative_directory"]
            .as_str()
            .unwrap(),
    );
    std::fs::remove_file(snapshot_directory.join("manifest.json")).unwrap();
    transaction["phase"] = "service_stopped".into();
    transaction["journal_snapshot"] = serde_json::Value::Null;
    std::fs::write(&transaction_path, serde_json::to_vec(&transaction).unwrap()).unwrap();

    let mut recovering = FakeHost::default();
    let recovered = recover_incomplete_transaction(
        &transaction_path,
        &fixture.database,
        &fixture.application,
        NOW + 20,
        &mut recovering,
    )
    .unwrap();

    assert_eq!(recovered.phase, MaintenancePhase::RolledBack);
    assert!(!snapshot_directory.exists());
    assert!(
        recovering
            .operations
            .iter()
            .any(|operation| operation == "prove:0.1.0")
    );
    cleanup(&fixture.root);
}

#[test]
fn records_a_terminal_failure_when_both_candidate_and_rollback_proof_fail() {
    let fixture = fixture();
    let mut host = FakeHost {
        fail_candidate_proof: true,
        fail_previous_proof: true,
        ..FakeHost::default()
    };
    let error = activate_prepared_slot(
        &fixture.request,
        &fixture.database,
        &fixture.application,
        NOW + 10,
        &mut host,
    )
    .unwrap_err();
    assert!(error.contains("recovery also failed"));
    let transaction: serde_json::Value = serde_json::from_slice(
        &std::fs::read(
            fixture
                .data
                .join("updates")
                .join(TRANSACTION_DIRECTORY_NAME)
                .join(format!("{}.json", fixture.request.transaction_id)),
        )
        .unwrap(),
    )
    .unwrap();
    assert_eq!(transaction["phase"], "rollback_failed");
    assert_eq!(
        latest_maintenance_transaction(&fixture.data)
            .unwrap()
            .unwrap()
            .phase,
        MaintenancePhase::RollbackFailed
    );
    cleanup(&fixture.root);
}

#[test]
fn recovers_an_interrupted_candidate_start_by_restoring_the_complete_prior_receipt() {
    let fixture = fixture();
    let mut failing = FakeHost {
        fail_candidate_proof: true,
        fail_previous_proof: true,
        ..FakeHost::default()
    };
    let _ = activate_prepared_slot(
        &fixture.request,
        &fixture.database,
        &fixture.application,
        NOW + 10,
        &mut failing,
    );
    let transaction_path = fixture
        .data
        .join("updates")
        .join(TRANSACTION_DIRECTORY_NAME)
        .join(format!("{}.json", fixture.request.transaction_id));
    let mut recovering = FakeHost::default();
    let recovered = recover_incomplete_transaction(
        &transaction_path,
        &fixture.database,
        &fixture.application,
        NOW + 20,
        &mut recovering,
    )
    .unwrap();

    assert_eq!(recovered.phase, MaintenancePhase::RolledBack);
    assert_eq!(recovering.operations.last().unwrap(), "prove:0.1.0");
    assert_eq!(
        read_active_slot_receipt(&fixture.data.join(ACTIVE_SLOT_RECEIPT_NAME))
            .unwrap()
            .active
            .package_manifest
            .version,
        "0.1.0"
    );
    cleanup(&fixture.root);
}

struct Fixture {
    root: PathBuf,
    application: PathBuf,
    data: PathBuf,
    database: PathBuf,
    request: MaintenanceRequest,
}

fn fixture() -> Fixture {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let root = std::env::temp_dir().join(format!("townlight-maintenance-{nonce}"));
    let application = root.join("application");
    let data = root.join("data");
    std::fs::create_dir_all(&application).unwrap();
    std::fs::create_dir_all(&data).unwrap();
    std::fs::create_dir_all(data.join("channel-journals").join("empty")).unwrap();
    std::fs::write(
        data.join("channel-journals").join("channel-a.tlj"),
        legacy_v1_journal(520),
    )
    .unwrap();
    let database = data.join("station.db");
    std::fs::write(&database, b"proof database").unwrap();

    let current_manifest = write_application(&application, "0.1.0", CURRENT_COMMIT, false);
    let active = ActiveSlotReceipt {
        schema_version: ACTIVE_SLOT_SCHEMA_VERSION,
        generation: 1,
        activated_at_unix_ms: NOW - 10_000,
        transaction_id: "bootstrap-install".into(),
        active: ActiveApplication {
            relative_directory: ".".into(),
            package_manifest: current_manifest,
        },
        previous: None,
    };
    std::fs::write(
        data.join(ACTIVE_SLOT_RECEIPT_NAME),
        serde_json::to_vec(&active).unwrap(),
    )
    .unwrap();
    std::fs::write(
        data.join(INSTALL_RECEIPT_NAME),
        format!(
            "{{\"product\":\"TownLight Station\",\"version\":\"0.1.0\",\"source_commit\":\"{CURRENT_COMMIT}\",\"media_runtime_sha256\":\"{}\"}}",
            "C".repeat(64)
        ),
    )
    .unwrap();

    let slot_name = format!("slot-0.2.0-{CANDIDATE_COMMIT}");
    let slot = application.join("slots").join(&slot_name);
    std::fs::create_dir_all(&slot).unwrap();
    let candidate_manifest = write_application(&slot, "0.2.0", CANDIDATE_COMMIT, true);
    std::fs::write(
        slot.join("slot-manifest.json"),
        serde_json::to_vec(&candidate_manifest).unwrap(),
    )
    .unwrap();
    set_read_only(&slot.join("slot-manifest.json"));
    let release = ReleaseManifest {
        schema_version: 1,
        product: "TownLight Station".into(),
        release_channel: "stable".into(),
        key_id: "release-2026-a".into(),
        version: "0.2.0".into(),
        source_commit: CANDIDATE_COMMIT.into(),
        published_at_unix_ms: NOW - 1_000,
        expires_at_unix_ms: NOW + 86_400_000,
        minimum_updater_version: "0.1.0".into(),
        package_url: "https://releases.example.test/station/0.2.0.tlu".into(),
        package_size_bytes: 1_000,
        package_sha256: "A".repeat(64),
        release_notes_markdown: "# 0.2.0\n\nMaintenance proof.".into(),
    };
    let request = MaintenanceRequest {
        schema_version: MAINTENANCE_SCHEMA_VERSION,
        transaction_id: format!("update-{CANDIDATE_COMMIT}-{NOW}"),
        requested_at_unix_ms: NOW + 2,
        requested_by: "station operator".into(),
        prepared_slot: PreparedSlotRecord {
            prepared_at_unix_ms: NOW + 1,
            manifest: release,
            package_file_name: format!("{}.tlu", "A".repeat(64).to_ascii_lowercase()),
            slot_directory_name: slot_name,
        },
    };
    Fixture {
        root,
        application,
        data,
        database,
        request,
    }
}

fn write_application(
    directory: &Path,
    version: &str,
    source_commit: &str,
    read_only: bool,
) -> UpdatePackageManifest {
    let payloads: [(&str, &[u8]); 2] = [
        (
            "stationd.exe",
            format!("stationd-{version}").leak().as_bytes(),
        ),
        (
            "channel-worker.exe",
            format!("channel-worker-{version}").leak().as_bytes(),
        ),
    ];
    let mut files = Vec::new();
    for (path, bytes) in payloads {
        let destination = directory.join(path);
        std::fs::write(&destination, bytes).unwrap();
        if read_only {
            set_read_only(&destination);
        }
        files.push(UpdatePackageFile {
            path: path.into(),
            size_bytes: bytes.len() as u64,
            sha256: format!("{:X}", Sha256::digest(bytes)),
        });
    }
    UpdatePackageManifest {
        schema_version: UPDATE_PACKAGE_SCHEMA_VERSION,
        product: "TownLight Station".into(),
        version: version.into(),
        source_commit: source_commit.into(),
        files,
    }
}

fn legacy_v1_journal(record_count: u64) -> Vec<u8> {
    let mut journal = Vec::new();
    for sequence in 1..=record_count {
        let event = if sequence == 1 {
            serde_json::json!({"type": "ready", "graph_revision": 1})
        } else {
            serde_json::json!({
                "type": "heartbeat",
                "monotonic_milliseconds": sequence * 5_000,
                "outputs": []
            })
        };
        journal.extend(journal_record(1, sequence, event));
    }
    journal
}

fn candidate_v2_journal() -> Vec<u8> {
    journal_record(
        2,
        1,
        serde_json::json!({"type": "ready", "graph_revision": 2}),
    )
}

fn journal_record(version: u16, sequence: u64, event: serde_json::Value) -> Vec<u8> {
    let json = serde_json::to_vec(&serde_json::json!({
        "version": {"major": 1, "minor": 4},
        "worker_id": "ae819b97-2920-434b-9d96-0b51a8ea9abb",
        "channel_id": "62a47b7e-2a03-48d6-8703-a6e5ce986527",
        "sequence": sequence,
        "event": event
    }))
    .unwrap();
    let mut frame = Vec::with_capacity(4 + json.len());
    frame.extend_from_slice(&(json.len() as u32).to_le_bytes());
    frame.extend_from_slice(&json);
    let mut record = Vec::with_capacity(16 + frame.len());
    record.extend_from_slice(b"TLJR");
    record.extend_from_slice(&version.to_le_bytes());
    record.extend_from_slice(&0_u16.to_le_bytes());
    record.extend_from_slice(&(frame.len() as u32).to_le_bytes());
    record.extend_from_slice(&crc32(&frame).to_le_bytes());
    record.extend_from_slice(&frame);
    record
}

fn v1_only_sequence_count(journal: &[u8]) -> u64 {
    let mut offset = 0_usize;
    let mut expected_sequence = 1_u64;
    while offset < journal.len() {
        assert!(journal.len() - offset >= 16);
        assert_eq!(&journal[offset..offset + 4], b"TLJR");
        assert_eq!(
            u16::from_le_bytes(journal[offset + 4..offset + 6].try_into().unwrap()),
            1
        );
        assert_eq!(
            u16::from_le_bytes(journal[offset + 6..offset + 8].try_into().unwrap()),
            0
        );
        let payload_length =
            u32::from_le_bytes(journal[offset + 8..offset + 12].try_into().unwrap()) as usize;
        let checksum = u32::from_le_bytes(journal[offset + 12..offset + 16].try_into().unwrap());
        let end = offset + 16 + payload_length;
        assert!(end <= journal.len());
        let frame = &journal[offset + 16..end];
        assert_eq!(crc32(frame), checksum);
        let json_length = u32::from_le_bytes(frame[..4].try_into().unwrap()) as usize;
        assert_eq!(json_length + 4, frame.len());
        let envelope: serde_json::Value = serde_json::from_slice(&frame[4..]).unwrap();
        assert_eq!(envelope["sequence"].as_u64(), Some(expected_sequence));
        expected_sequence += 1;
        offset = end;
    }
    expected_sequence - 1
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

fn set_read_only(path: &Path) {
    let mut permissions = std::fs::metadata(path).unwrap().permissions();
    permissions.set_readonly(true);
    std::fs::set_permissions(path, permissions).unwrap();
}

#[allow(clippy::permissions_set_readonly_false)]
fn cleanup(root: &Path) {
    for entry in walk(root) {
        if let Ok(metadata) = std::fs::metadata(&entry)
            && metadata.is_file()
        {
            let mut permissions = metadata.permissions();
            permissions.set_readonly(false);
            let _ = std::fs::set_permissions(entry, permissions);
        }
    }
    std::fs::remove_dir_all(root).unwrap();
}

fn walk(root: &Path) -> Vec<PathBuf> {
    let mut result = Vec::new();
    let mut pending = vec![root.to_path_buf()];
    while let Some(directory) = pending.pop() {
        if let Ok(entries) = std::fs::read_dir(directory) {
            for entry in entries.flatten() {
                if entry.path().is_dir() {
                    pending.push(entry.path());
                } else {
                    result.push(entry.path());
                }
            }
        }
    }
    result
}
