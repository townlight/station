use std::io::{Read, Write};
use std::net::SocketAddr;
use std::path::Path;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use station_media_protocol::{
    ChannelCommand, CommandEnvelope, MAX_FRAME_BYTES, PROTOCOL_VERSION, ProtocolError, WorkerEvent,
    WorkerEventEnvelope, decode_event_frame, encode_command_frame,
};
use station_windows_ipc::{IpcError, PipeServer, PipeStream};

static PIPE_SEQUENCE: AtomicU64 = AtomicU64::new(1);

#[derive(Debug)]
pub enum SupervisorError {
    Ipc(IpcError),
    Io {
        operation: &'static str,
        message: String,
    },
    Protocol(ProtocolError),
    WrongWorker {
        expected: String,
        actual: String,
    },
    WrongChannel {
        expected: String,
        actual: String,
    },
    WrongSequence {
        expected: u64,
        actual: u64,
    },
    InvalidInitialSequence,
    UnexpectedInitialEvent,
    UnexpectedShutdownEvent,
    AlreadyStopped,
    ProcessExit(Option<i32>),
    ProcessExitTimedOut,
}

pub struct WorkerSupervisor {
    child: Option<Child>,
    pipe: PipeStream,
    worker_id: String,
    channel_id: String,
    last_sequence: u64,
    ready: WorkerEventEnvelope,
}

impl WorkerSupervisor {
    pub fn launch(
        executable: &Path,
        worker_id: &str,
        channel_id: &str,
        journal_path: &Path,
        udp_destination: SocketAddr,
        timeout: Duration,
    ) -> Result<Self, SupervisorError> {
        let started = Instant::now();
        let suffix = format!(
            "worker-{}-{}",
            std::process::id(),
            PIPE_SEQUENCE.fetch_add(1, Ordering::Relaxed)
        );
        let server = PipeServer::bind(&suffix).map_err(SupervisorError::Ipc)?;
        let mut child = Command::new(executable)
            .args([worker_id, channel_id])
            .arg(journal_path)
            .arg(server.name())
            .arg(udp_destination.to_string())
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|error| io_error("spawn channel worker", error))?;

        let mut pipe = match server.accept_timeout(timeout.saturating_sub(started.elapsed())) {
            Ok(pipe) => pipe,
            Err(error) => {
                terminate(&mut child);
                return Err(SupervisorError::Ipc(error));
            }
        };
        let ready = match read_event(&mut pipe, timeout.saturating_sub(started.elapsed())) {
            Ok(event) => event,
            Err(error) => {
                terminate(&mut child);
                return Err(error);
            }
        };
        if let Err(error) = validate_identity(&ready, worker_id, channel_id) {
            terminate(&mut child);
            return Err(error);
        }
        if ready.sequence == 0 {
            terminate(&mut child);
            return Err(SupervisorError::InvalidInitialSequence);
        }
        if !matches!(ready.event, WorkerEvent::Ready { .. }) {
            terminate(&mut child);
            return Err(SupervisorError::UnexpectedInitialEvent);
        }

        Ok(Self {
            child: Some(child),
            pipe,
            worker_id: worker_id.to_string(),
            channel_id: channel_id.to_string(),
            last_sequence: ready.sequence,
            ready,
        })
    }

    pub fn ready(&self) -> &WorkerEventEnvelope {
        &self.ready
    }

    pub fn command(
        &mut self,
        command_id: &str,
        command: ChannelCommand,
        timeout: Duration,
    ) -> Result<WorkerEventEnvelope, SupervisorError> {
        if self.child.is_none() {
            return Err(SupervisorError::AlreadyStopped);
        }
        let expected_sequence = self.last_sequence + 1;
        let envelope = CommandEnvelope {
            version: PROTOCOL_VERSION,
            channel_id: self.channel_id.clone(),
            command_id: command_id.to_string(),
            expected_sequence,
            command,
        };
        let frame = encode_command_frame(&envelope).map_err(SupervisorError::Protocol)?;
        self.pipe
            .write_all(&frame)
            .map_err(|error| io_error("write worker command", error))?;
        self.pipe
            .flush()
            .map_err(|error| io_error("flush worker command", error))?;

        let event = read_event(&mut self.pipe, timeout)?;
        validate_identity(&event, &self.worker_id, &self.channel_id)?;
        if event.sequence != expected_sequence {
            return Err(SupervisorError::WrongSequence {
                expected: expected_sequence,
                actual: event.sequence,
            });
        }
        self.last_sequence = event.sequence;
        Ok(event)
    }

    pub fn shutdown(
        mut self,
        command_id: &str,
        timeout: Duration,
    ) -> Result<WorkerEventEnvelope, SupervisorError> {
        let started = Instant::now();
        let event = self.command(command_id, ChannelCommand::Shutdown, timeout)?;
        if !matches!(event.event, WorkerEvent::ShutdownComplete) {
            return Err(SupervisorError::UnexpectedShutdownEvent);
        }

        let remaining = timeout.saturating_sub(started.elapsed());
        let mut child = self.child.take().ok_or(SupervisorError::AlreadyStopped)?;
        let status = wait_for_exit(&mut child, remaining)?;
        if !status.success() {
            return Err(SupervisorError::ProcessExit(status.code()));
        }
        Ok(event)
    }
}

impl Drop for WorkerSupervisor {
    fn drop(&mut self) {
        if let Some(mut child) = self.child.take() {
            terminate(&mut child);
        }
    }
}

fn read_event(
    pipe: &mut PipeStream,
    timeout: Duration,
) -> Result<WorkerEventEnvelope, SupervisorError> {
    let started = Instant::now();
    pipe.wait_for_bytes(4, timeout)
        .map_err(SupervisorError::Ipc)?;
    let mut length = [0_u8; 4];
    pipe.read_exact(&mut length)
        .map_err(|error| io_error("read worker event length", error))?;
    let payload_length = u32::from_le_bytes(length) as usize;
    if payload_length > MAX_FRAME_BYTES {
        return Err(SupervisorError::Protocol(ProtocolError::FrameTooLarge(
            payload_length,
        )));
    }
    pipe.wait_for_bytes(payload_length, timeout.saturating_sub(started.elapsed()))
        .map_err(SupervisorError::Ipc)?;
    let mut frame = Vec::with_capacity(4 + payload_length);
    frame.extend_from_slice(&length);
    frame.resize(4 + payload_length, 0);
    pipe.read_exact(&mut frame[4..])
        .map_err(|error| io_error("read worker event payload", error))?;
    decode_event_frame(&frame).map_err(SupervisorError::Protocol)
}

fn validate_identity(
    event: &WorkerEventEnvelope,
    worker_id: &str,
    channel_id: &str,
) -> Result<(), SupervisorError> {
    if event.worker_id != worker_id {
        return Err(SupervisorError::WrongWorker {
            expected: worker_id.to_string(),
            actual: event.worker_id.clone(),
        });
    }
    if event.channel_id != channel_id {
        return Err(SupervisorError::WrongChannel {
            expected: channel_id.to_string(),
            actual: event.channel_id.clone(),
        });
    }
    Ok(())
}

fn wait_for_exit(child: &mut Child, timeout: Duration) -> Result<ExitStatus, SupervisorError> {
    let started = Instant::now();
    loop {
        if let Some(status) = child
            .try_wait()
            .map_err(|error| io_error("query channel worker exit", error))?
        {
            return Ok(status);
        }
        if started.elapsed() >= timeout {
            terminate(child);
            return Err(SupervisorError::ProcessExitTimedOut);
        }
        std::thread::sleep(Duration::from_millis(2));
    }
}

fn terminate(child: &mut Child) {
    match child.try_wait() {
        Ok(Some(_)) => {}
        Ok(None) | Err(_) => {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn io_error(operation: &'static str, error: std::io::Error) -> SupervisorError {
    SupervisorError::Io {
        operation,
        message: error.to_string(),
    }
}
