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
}

impl MaintenanceHost for FakeHost {
    fn stop_station(&mut self, _timeout: Duration) -> Result<(), String> {
        self.operations.push("stop".into());
        Ok(())
    }

    fn configure_station(
        &mut self,
        stationd_path: &Path,
        _database_path: &Path,
    ) -> Result<(), String> {
        self.operations
            .push(format!("configure:{}", stationd_path.display()));
        Ok(())
    }

    fn start_station(&mut self) -> Result<(), String> {
        self.operations.push("start".into());
        Ok(())
    }

    fn prove_station(
        &mut self,
        expected: &ActiveApplication,
        _timeout: Duration,
    ) -> Result<(), String> {
        let version = expected.package_manifest.version.clone();
        self.operations.push(format!("prove:{version}"));
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
