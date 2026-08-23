use std::io::{Read, Write};
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use station_media_protocol::{
    ChannelCommand, CommandEnvelope, LoudnessTarget, MAX_FRAME_BYTES, PROTOCOL_VERSION,
    ProtocolError, WorkerBootstrapEnvelope, WorkerEvent, WorkerEventEnvelope,
    WorkerOutputConfiguration, decode_event_frame, encode_bootstrap_frame, encode_command_frame,
};
use station_schedule::OutputProfileId;
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
        Self::launch_with_output_profile(
            executable,
            worker_id,
            channel_id,
            journal_path,
            udp_destination,
            OutputProfileId::default(),
            timeout,
        )
    }

    pub fn launch_with_output_profile(
        executable: &Path,
        worker_id: &str,
        channel_id: &str,
        journal_path: &Path,
        udp_destination: SocketAddr,
        output_profile: OutputProfileId,
        timeout: Duration,
    ) -> Result<Self, SupervisorError> {
        Self::launch_with_output_profile_and_destinations(
            executable,
            worker_id,
            channel_id,
            journal_path,
            &[udp_destination],
            output_profile,
            timeout,
        )
    }

    pub fn launch_with_output_profile_and_destinations(
        executable: &Path,
        worker_id: &str,
        channel_id: &str,
        journal_path: &Path,
        udp_destinations: &[SocketAddr],
        output_profile: OutputProfileId,
        timeout: Duration,
    ) -> Result<Self, SupervisorError> {
        let outputs = udp_outputs_for_profile(udp_destinations, output_profile)?;
        Self::launch_with_outputs(
            executable,
            worker_id,
            channel_id,
            journal_path,
            &outputs,
            output_profile,
            timeout,
        )
    }

    pub fn launch_with_outputs(
        executable: &Path,
        worker_id: &str,
        channel_id: &str,
        journal_path: &Path,
        outputs: &[WorkerOutputConfiguration],
        output_profile: OutputProfileId,
        timeout: Duration,
    ) -> Result<Self, SupervisorError> {
        Self::launch_with_outputs_and_cg_image_root(
            executable,
            worker_id,
            channel_id,
            journal_path,
            outputs,
            &journal_path
                .parent()
                .unwrap_or_else(|| Path::new("."))
                .join("cg-images"),
            output_profile,
            timeout,
        )
    }

    #[allow(clippy::too_many_arguments)]
    pub fn launch_with_outputs_and_cg_image_root(
        executable: &Path,
        worker_id: &str,
        channel_id: &str,
        journal_path: &Path,
        outputs: &[WorkerOutputConfiguration],
        cg_image_root: &Path,
        output_profile: OutputProfileId,
        timeout: Duration,
    ) -> Result<Self, SupervisorError> {
        let proof_artifact_root = default_output_proof_root(journal_path);
        Self::launch_with_outputs_and_roots(
            executable,
            worker_id,
            channel_id,
            journal_path,
            outputs,
            cg_image_root,
            &proof_artifact_root,
            output_profile,
            timeout,
        )
    }

    #[allow(clippy::too_many_arguments)]
    pub fn launch_with_outputs_and_roots(
        executable: &Path,
        worker_id: &str,
        channel_id: &str,
        journal_path: &Path,
        outputs: &[WorkerOutputConfiguration],
        cg_image_root: &Path,
        proof_artifact_root: &Path,
        output_profile: OutputProfileId,
        timeout: Duration,
    ) -> Result<Self, SupervisorError> {
        let cg_image_root = if cg_image_root.is_absolute() {
            cg_image_root.to_path_buf()
        } else {
            std::env::current_dir()
                .map_err(|error| io_error("resolve CG image root", error))?
                .join(cg_image_root)
        };
        let proof_artifact_root = if proof_artifact_root.is_absolute() {
            proof_artifact_root.to_path_buf()
        } else {
            std::env::current_dir()
                .map_err(|error| io_error("resolve output proof root", error))?
                .join(proof_artifact_root)
        };
        let bootstrap = WorkerBootstrapEnvelope {
            version: PROTOCOL_VERSION,
            worker_id: worker_id.to_string(),
            channel_id: channel_id.to_string(),
            output_profile: output_profile.storage_name().to_string(),
            cg_image_root: cg_image_root.to_string_lossy().into_owned(),
            proof_artifact_root: proof_artifact_root.to_string_lossy().into_owned(),
            outputs: outputs.to_vec(),
        };
        let bootstrap_frame =
            encode_bootstrap_frame(&bootstrap).map_err(SupervisorError::Protocol)?;
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
        if let Err(error) = pipe.write_all(&bootstrap_frame).and_then(|_| pipe.flush()) {
            terminate(&mut child);
            return Err(io_error("write worker bootstrap", error));
        }
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

    pub fn ensure_running(&mut self) -> Result<(), SupervisorError> {
        let child = self.child.as_mut().ok_or(SupervisorError::AlreadyStopped)?;
        match child
            .try_wait()
            .map_err(|error| io_error("query channel worker liveness", error))?
        {
            Some(status) => {
                self.child.take();
                Err(SupervisorError::ProcessExit(status.code()))
            }
            None => Ok(()),
        }
    }

    /// Ends an ambiguously synchronized worker session. Once a protected-pipe
    /// command times out, the supervisor cannot know whether the worker acted
    /// before its reply was lost; process termination is the only fail-closed
    /// way to prevent that command from continuing in the background.
    pub fn force_terminate(&mut self) {
        if let Some(mut child) = self.child.take() {
            terminate(&mut child);
        }
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

fn default_output_proof_root(journal_path: &Path) -> PathBuf {
    let journal_directory = journal_path.parent().unwrap_or_else(|| Path::new("."));
    let managed_data_root = if journal_directory.file_name().and_then(|name| name.to_str())
        == Some("channel-journals")
    {
        journal_directory.parent().unwrap_or(journal_directory)
    } else {
        journal_directory
    };
    managed_data_root.join("output-proofs")
}

fn udp_outputs_for_profile(
    destinations: &[SocketAddr],
    output_profile: OutputProfileId,
) -> Result<Vec<WorkerOutputConfiguration>, SupervisorError> {
    let loudness_target_lufs =
        LoudnessTarget::from_lufs(f64::from(output_profile.profile().loudness_target_lufs))
            .map_err(|message| SupervisorError::Io {
                operation: "resolve output profile loudness target",
                message: message.into(),
            })?;
    Ok(destinations
        .iter()
        .enumerate()
        .map(|(index, destination)| WorkerOutputConfiguration::UdpTs {
            sink_id: format!("00000000-0000-4000-8000-{:012x}", index + 1),
            destination: destination.to_string(),
            loudness_target_lufs,
            headend: None,
        })
        .collect())
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

#[cfg(test)]
mod tests {
    use super::*;

    fn destination(value: &str) -> SocketAddr {
        value.parse().expect("test destination is valid")
    }

    #[test]
    fn default_proof_root_is_a_sibling_of_the_production_journal_directory() {
        assert_eq!(
            default_output_proof_root(Path::new(
                r"C:\ProgramData\TownLight Station\channel-journals\channel.tlj",
            )),
            PathBuf::from(r"C:\ProgramData\TownLight Station\output-proofs")
        );
        assert_eq!(
            default_output_proof_root(Path::new(r"C:\temp\channel.tlj")),
            PathBuf::from(r"C:\temp\output-proofs")
        );
    }

    #[test]
    fn udp_bootstrap_outputs_use_the_selected_profile_target() {
        let proof = udp_outputs_for_profile(
            &[destination("127.0.0.1:5500")],
            OutputProfileId::Proof360p30,
        )
        .expect("the proof profile target is valid");
        let hd = udp_outputs_for_profile(
            &[destination("127.0.0.1:5501")],
            OutputProfileId::Atsc720p5994,
        )
        .expect("the HD profile target is valid");

        let WorkerOutputConfiguration::UdpTs {
            loudness_target_lufs: proof_target,
            ..
        } = proof[0]
        else {
            panic!("the proof profile must produce a UDP output");
        };
        let WorkerOutputConfiguration::UdpTs {
            loudness_target_lufs: hd_target,
            ..
        } = hd[0]
        else {
            panic!("the HD profile must produce a UDP output");
        };
        assert_eq!(proof_target, LoudnessTarget::STREAMING);
        assert_eq!(hd_target, LoudnessTarget::CABLE);
    }

    #[test]
    fn every_udp_branch_keeps_the_selected_target_and_stable_destination_order() {
        let outputs = udp_outputs_for_profile(
            &[destination("127.0.0.1:5500"), destination("127.0.0.1:5501")],
            OutputProfileId::Atsc720p5994,
        )
        .expect("the selected profile target is valid");

        assert_eq!(outputs.len(), 2);
        for (output, expected_destination) in
            outputs.iter().zip(["127.0.0.1:5500", "127.0.0.1:5501"])
        {
            let WorkerOutputConfiguration::UdpTs {
                destination,
                loudness_target_lufs,
                ..
            } = output
            else {
                panic!("all configured destinations must remain UDP outputs");
            };
            assert_eq!(destination, expected_destination);
            assert_eq!(*loudness_target_lufs, LoudnessTarget::CABLE);
        }
    }
}
