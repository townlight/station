use std::net::UdpSocket;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use station_orchestration::{CandidateAuthority, ChannelController};
use station_readiness::CandidateIdentity;
use station_schedule::{ChannelConfiguration, OutputProfileId, OutputSinkConfiguration};
use station_storage::{OutputLoudnessProofJob, OutputLoudnessProofJobState, StationStore};

const CHANNEL_ID: &str = "11111111-2222-4333-8444-555555555555";
const SINK_ID: &str = "22222222-3333-4444-8555-666666666666";
const JOB_ID: &str = "33333333-4444-4555-8666-777777777777";
const SOURCE_COMMIT: &str = "0123456789abcdef0123456789abcdef01234567";

struct TestCandidateAuthority;

impl CandidateAuthority for TestCandidateAuthority {
    fn current_candidate(&self) -> Result<CandidateIdentity, String> {
        Ok(CandidateIdentity {
            version: "0.1.0".into(),
            source_commit: Some(SOURCE_COMMIT.into()),
        })
    }
}

struct TempRoot(PathBuf);

impl Drop for TempRoot {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

#[test]
fn capture_failure_after_claim_is_terminal_job_truth_not_a_controller_error() {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is before Unix epoch")
        .as_nanos();
    let root = TempRoot(std::env::temp_dir().join(format!(
        "townlight-output-proof-failure-{}-{nonce}",
        std::process::id()
    )));
    std::fs::create_dir_all(root.0.join("channel-journals")).unwrap();
    let database = root.0.join("station.db");
    let journal = root.0.join("channel-journals").join("worker.tlj");
    let receiver = UdpSocket::bind("127.0.0.1:0").unwrap();
    let channel = ChannelConfiguration {
        channel_id: CHANNEL_ID.into(),
        display_name: "Proof failure executor".into(),
        output_profile: OutputProfileId::Proof360p30,
        output_sinks: vec![OutputSinkConfiguration::required_udp(
            SINK_ID,
            "Cable proof",
            receiver.local_addr().unwrap().to_string(),
            -24.0,
        )],
        enabled: true,
    };
    let store = StationStore::open(&database).unwrap();
    store.put_channel(&channel).unwrap();
    let configuration_hash = store
        .current_channel_output_configuration_hash(CHANNEL_ID)
        .unwrap();
    assert!(
        store
            .create_output_loudness_job(&OutputLoudnessProofJob {
                job_id: JOB_ID.into(),
                channel_id: CHANNEL_ID.into(),
                target_group: "cable".into(),
                sink_id: SINK_ID.into(),
                target_lufs_milli: -24_000,
                candidate_version: "0.1.0".into(),
                source_commit: Some(SOURCE_COMMIT.into()),
                configuration_hash,
                sample_duration_ms: 3_000,
                max_capture_bytes: 1,
                created_at_unix_ms: unix_millis(),
            })
            .unwrap()
    );
    drop(store);

    let mut controller = ChannelController::launch(
        PathBuf::from(env!("CARGO_BIN_EXE_channel-worker")).as_path(),
        &database,
        &journal,
        &channel,
        Duration::from_secs(7),
    )
    .unwrap();
    controller.set_candidate_authority(Arc::new(TestCandidateAuthority));

    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        assert!(
            Instant::now() < deadline,
            "capture failure did not become terminal"
        );
        controller.run_once(unix_millis()).unwrap_or_else(|error| {
            panic!("capture failure escaped the job as a controller error: {error:?}")
        });
        let store = StationStore::open(&database).unwrap();
        let job = store.read_output_loudness_job(JOB_ID).unwrap().unwrap();
        if job.state == OutputLoudnessProofJobState::Failed {
            assert!(
                job.failure_detail
                    .as_deref()
                    .is_some_and(|detail| detail.contains("capture_too_large")),
                "the terminal job must retain the worker capture failure"
            );
            break;
        }
        assert_ne!(job.state, OutputLoudnessProofJobState::Completed);
        std::thread::sleep(Duration::from_millis(50));
    }

    controller.shutdown(Duration::from_secs(3)).unwrap();
}

fn unix_millis() -> i64 {
    i64::try_from(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock is before Unix epoch")
            .as_millis(),
    )
    .unwrap()
}
