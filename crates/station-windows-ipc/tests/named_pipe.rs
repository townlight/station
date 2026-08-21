use std::io::{Read, Write};
use std::thread;
use std::time::{SystemTime, UNIX_EPOCH};

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
