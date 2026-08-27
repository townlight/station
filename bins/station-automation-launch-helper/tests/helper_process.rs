use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

fn root(name: &str) -> PathBuf {
    let fixture_root = std::env::var_os("TOWNLIGHT_HELPER_TEST_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(std::env::temp_dir);
    fixture_root.join(format!(
        "townlight-launch-helper-process-{name}-{}",
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ))
}

fn fixture(name: &str) -> (PathBuf, PathBuf, String, Value) {
    let root = root(name);
    let input = root.join("input");
    let output = root.join("output");
    std::fs::create_dir_all(&input).unwrap();
    std::fs::create_dir_all(&output).unwrap();
    std::fs::write(input.join("one.txt"), b"fixture").unwrap();
    let authority = root.join("root-authority.json");
    let authority_bytes = serde_json::to_vec(&json!({
        "authority_id":"aaaaaaaa-2222-4333-8444-555555555555",
        "revision":1,
        "input_root":root,
        "output_root":root
    }))
    .unwrap();
    std::fs::write(&authority, &authority_bytes).unwrap();
    let authority_sha = format!("{:x}", Sha256::digest(&authority_bytes));
    let request = json!({
        "job_id":"33333333-2222-4333-8444-555555555555",
        "claim_id":"77777777-2222-4333-8444-555555555555",
        "adapter_id":"11111111-2222-4333-8444-555555555555",
        "adapter_revision":1,
        "candidate_version":"0.1.0",
        "candidate_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "install_generation":7,
        "configuration_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "root_authority_sha256":authority_sha,
        "input_relative_path":"input\\one.txt",
        "input_sha256":format!("{:x}", Sha256::digest(b"fixture")),
        "input_bytes":7,
        "output_relative_path":"output\\one.json",
        "action_timeout_ms":60_000
    });
    (root, authority, authority_sha, request)
}

fn spawn(authority: &Path, authority_sha: &str) -> Child {
    Command::new(env!("CARGO_BIN_EXE_station-automation-launch-helper"))
        .args([
            "--root-authority",
            authority.to_str().unwrap(),
            "--root-authority-sha256",
            authority_sha,
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap()
}

fn send(child: &mut Child, request: &Value) {
    let body = serde_json::to_vec(request).unwrap();
    let stdin = child.stdin.as_mut().unwrap();
    stdin.write_all(&(body.len() as u32).to_le_bytes()).unwrap();
    stdin.write_all(&body).unwrap();
    child.stdin.take();
}

fn response(mut child: Child) -> Value {
    let status = child.wait().unwrap();
    assert!(status.success());
    let mut bytes = Vec::new();
    child
        .stdout
        .take()
        .unwrap()
        .read_to_end(&mut bytes)
        .unwrap();
    let mut stderr = Vec::new();
    child
        .stderr
        .take()
        .unwrap()
        .read_to_end(&mut stderr)
        .unwrap();
    assert!(stderr.is_empty());
    serde_json::from_slice(&bytes).unwrap()
}

#[test]
fn actual_helper_accepts_only_framed_attested_managed_input_and_stays_unavailable() {
    let (root, authority, authority_sha, request) = fixture("valid");
    let mut child = spawn(&authority, &authority_sha);
    send(&mut child, &request);
    assert_eq!(
        response(child),
        json!({"status":"adapter_unavailable","code":"adapter_unavailable"})
    );
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn unavailable_helper_has_no_artifact_path_race_surface() {
    let (root, authority, authority_sha, request) = fixture("path-swap");
    let outside = root.join("outside");
    std::fs::create_dir_all(&outside).unwrap();
    std::fs::write(outside.join("one.txt"), b"outside-secret").unwrap();

    // Launch first, then deterministically replace both request-named parents
    // before releasing the framed request. The unavailable helper has no
    // parent-acquisition/leaf-open phase: contract validation must not touch
    // either junction target.
    let mut child = spawn(&authority, &authority_sha);
    std::fs::rename(root.join("input"), root.join("input-original")).unwrap();
    std::fs::rename(root.join("output"), root.join("output-original")).unwrap();
    for name in ["input", "output"] {
        let status = Command::new("cmd.exe")
            .args(["/d", "/c", "mklink", "/J"])
            .arg(root.join(name))
            .arg(&outside)
            .status()
            .unwrap();
        assert!(status.success());
    }
    send(&mut child, &request);
    assert_eq!(
        response(child),
        json!({"status":"adapter_unavailable","code":"adapter_unavailable"})
    );
    assert_eq!(
        std::fs::read(outside.join("one.txt")).unwrap(),
        b"outside-secret"
    );
    assert!(!outside.join("one.json").exists());
    std::fs::remove_dir(root.join("input")).unwrap();
    std::fs::remove_dir(root.join("output")).unwrap();
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn trickled_partial_frame_exits_at_the_reader_deadline() {
    let (root, authority, authority_sha, _) = fixture("trickle");
    let mut child = spawn(&authority, &authority_sha);
    child
        .stdin
        .as_mut()
        .unwrap()
        .write_all(&100_u32.to_le_bytes())
        .unwrap();
    child.stdin.as_mut().unwrap().write_all(b"{").unwrap();
    let started = Instant::now();
    loop {
        if child.try_wait().unwrap().is_some() {
            break;
        }
        assert!(started.elapsed() < Duration::from_secs(7));
        std::thread::sleep(Duration::from_millis(20));
    }
    assert_eq!(response(child)["status"], "rejected");
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn oversized_frame_and_root_rebind_fail_closed() {
    let (root, authority, authority_sha, mut request) = fixture("hostile");
    let mut oversized = spawn(&authority, &authority_sha);
    oversized
        .stdin
        .as_mut()
        .unwrap()
        .write_all(&(65_537_u32).to_le_bytes())
        .unwrap();
    oversized.stdin.take();
    assert_eq!(response(oversized)["status"], "rejected");
    request["root_authority_sha256"] = Value::String("c".repeat(64));
    let mut rebound = spawn(&authority, &authority_sha);
    send(&mut rebound, &request);
    assert_eq!(response(rebound)["status"], "rejected");
    request["root_authority_sha256"] = Value::String(authority_sha.clone());
    request["input_bytes"] = Value::from(137_438_953_473_u64);
    let mut oversized_input = spawn(&authority, &authority_sha);
    send(&mut oversized_input, &request);
    assert_eq!(response(oversized_input)["status"], "rejected");
    request["input_bytes"] = Value::from(0_u64);
    let mut empty_input = spawn(&authority, &authority_sha);
    send(&mut empty_input, &request);
    assert_eq!(response(empty_input)["status"], "rejected");
    std::fs::remove_dir_all(root).unwrap();
}
