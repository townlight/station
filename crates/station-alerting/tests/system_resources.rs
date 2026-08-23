use std::time::{SystemTime, UNIX_EPOCH};

use station_alerting::{probe_system_resources, sample_and_record_system_resources};
use station_storage::StationStore;

#[test]
fn probes_real_windows_resources_and_cleans_the_backup_write_test() {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let root = std::env::temp_dir().join(format!("townlight-resource-probe-{nonce}"));
    std::fs::create_dir_all(&root).unwrap();
    let database = root.join("station.db");
    let recorded = sample_and_record_system_resources(&database, 1_000).unwrap();

    assert!(recorded.sample_id.is_some());
    assert!(
        recorded
            .cpu_percent
            .is_some_and(|value| (0.0..=100.0).contains(&value))
    );
    assert!(recorded.ram_total_gb.is_some_and(|value| value > 0.0));
    assert!(recorded.ram_used_gb.is_some_and(|value| value > 0.0));
    assert!(
        recorded
            .media_volume_free_gb
            .is_some_and(|value| value > 0.0)
    );
    assert!(recorded.backup_volume_writable);
    assert_eq!(
        StationStore::open(&database)
            .unwrap()
            .latest_system_resource_sample()
            .unwrap(),
        Some(recorded)
    );
    assert_eq!(std::fs::read_dir(root.join("backups")).unwrap().count(), 0);

    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn live_resource_probe_is_read_only_and_leaves_no_database_authority() {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let root = std::env::temp_dir().join(format!("townlight-readiness-probe-{nonce}"));
    std::fs::create_dir_all(&root).unwrap();
    let database = root.join("station.db");

    let sample = probe_system_resources(&database, 2_000).unwrap();

    assert_eq!(sample.sample_id, None);
    assert_eq!(sample.sampled_at_unix_ms, 2_000);
    assert!(sample.ram_total_gb.is_some_and(|value| value > 0.0));
    assert!(sample.media_volume_free_gb.is_some_and(|value| value > 0.0));
    assert!(sample.backup_volume_writable);
    assert!(
        !database.exists(),
        "a read-only probe created station authority"
    );
    assert_eq!(std::fs::read_dir(root.join("backups")).unwrap().count(), 0);

    std::fs::remove_dir_all(root).unwrap();
}
