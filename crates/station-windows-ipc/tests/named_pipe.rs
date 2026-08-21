use std::io::{Read, Write};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use station_windows_ipc::{IpcError, PipeServer, PipeStream};

fn unique_suffix(name: &str) -> String {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is before Unix epoch")
        .as_nanos();
    format!("{name}-{nonce}")
}

#[test]
fn refuses_a_second_server_for_the_same_station_pipe() {
    let suffix = unique_suffix("exclusive");
    let first = PipeServer::bind(&suffix).expect("first server owns the pipe");
    let second = PipeServer::bind(&suffix);
    assert!(second.is_err(), "a second server acquired {}", first.name());
}

#[test]
fn exchanges_bytes_in_both_directions_over_the_station_pipe() {
    let suffix = unique_suffix("duplex");
    let server = PipeServer::bind(&suffix).unwrap();
    let name = server.name().to_string();
    let client = thread::spawn(move || {
        let mut stream = PipeStream::connect(&name).unwrap();
        stream.write_all(b"worker-ready").unwrap();
        stream.flush().unwrap();
        let mut reply = [0_u8; 12];
        stream.read_exact(&mut reply).unwrap();
        reply
    });

    let mut stream = server.accept().unwrap();
    let mut message = [0_u8; 12];
    stream.read_exact(&mut message).unwrap();
    assert_eq!(&message, b"worker-ready");
    stream.write_all(b"station-ping").unwrap();
    stream.flush().unwrap();
    assert_eq!(&client.join().unwrap(), b"station-ping");
}

#[test]
fn rejects_names_outside_the_scoped_station_namespace() {
    assert!(matches!(PipeServer::bind(""), Err(IpcError::InvalidName)));
    assert!(matches!(
        PipeServer::bind("..\\other"),
        Err(IpcError::InvalidName)
    ));
    assert!(matches!(
        PipeStream::connect(r"\\.\pipe\unrelated"),
        Err(IpcError::InvalidName)
    ));
}

#[test]
fn bounds_the_wait_for_a_worker_to_connect() {
    let server = PipeServer::bind(&unique_suffix("timeout")).unwrap();
    let started = Instant::now();
    let result = server.accept_timeout(Duration::from_millis(40));
    let elapsed = started.elapsed();

    assert!(matches!(
        result,
        Err(IpcError::TimedOut {
            operation: "ConnectNamedPipe"
        })
    ));
    assert!(
        elapsed >= Duration::from_millis(30),
        "connection wait returned too early after {elapsed:?}"
    );
    assert!(elapsed < Duration::from_secs(1));
}

#[test]
fn bounds_the_wait_for_worker_bytes() {
    let server = PipeServer::bind(&unique_suffix("read-timeout")).unwrap();
    let name = server.name().to_string();
    let client = thread::spawn(move || {
        let _stream = PipeStream::connect(&name).unwrap();
        thread::sleep(Duration::from_millis(100));
    });
    let stream = server.accept_timeout(Duration::from_secs(1)).unwrap();

    let started = Instant::now();
    let result = stream.wait_for_bytes(1, Duration::from_millis(40));
    let elapsed = started.elapsed();
    assert!(matches!(
        result,
        Err(IpcError::TimedOut {
            operation: "PeekNamedPipe"
        })
    ));
    assert!(
        elapsed >= Duration::from_millis(30),
        "byte wait returned too early after {elapsed:?}"
    );
    assert!(elapsed < Duration::from_secs(1));
    client.join().unwrap();
}
