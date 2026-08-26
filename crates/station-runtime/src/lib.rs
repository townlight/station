use std::collections::VecDeque;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use station_media_protocol::{
    ChannelCommand, CommandEnvelope, LoudnessTarget, MAX_FRAME_BYTES, PROTOCOL_VERSION,
    ProtocolError, WorkerBootstrapEnvelope, WorkerCaptionPolicy, WorkerEvent, WorkerEventEnvelope,
    WorkerFallbackSlate, WorkerOutputConfiguration, decode_event_frame, encode_bootstrap_frame,
    encode_command_frame,
};
use station_schedule::OutputProfileId;
use station_windows_ipc::{
    ContainedProcess, IpcError, PipeServer, PipeStream, ProcessContainment,
    launch_contained_process,
};

static PIPE_SEQUENCE: AtomicU64 = AtomicU64::new(1);
const FORCE_TERMINATE_TIMEOUT: Duration = Duration::from_secs(3);
const MAX_PENDING_LIFECYCLE_EVENTS: usize = 64;

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
    LifecycleEventOverflow,
}

pub struct WorkerSupervisor {
    child: Option<ContainedProcess>,
    containment: Option<ProcessContainment>,
    pipe: PipeStream,
    worker_id: String,
    channel_id: String,
    last_sequence: u64,
    ready: WorkerEventEnvelope,
    pending_lifecycle_events: VecDeque<WorkerEventEnvelope>,
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
        Self::launch_with_outputs_roots_and_fallback_slate(
            executable,
            worker_id,
            channel_id,
            journal_path,
            outputs,
            cg_image_root,
            proof_artifact_root,
            None,
            output_profile,
            timeout,
        )
    }

    #[allow(clippy::too_many_arguments)]
    pub fn launch_with_outputs_roots_and_fallback_slate(
        executable: &Path,
        worker_id: &str,
        channel_id: &str,
        journal_path: &Path,
        outputs: &[WorkerOutputConfiguration],
        cg_image_root: &Path,
        proof_artifact_root: &Path,
        fallback_slate: Option<WorkerFallbackSlate>,
        output_profile: OutputProfileId,
        timeout: Duration,
    ) -> Result<Self, SupervisorError> {
        Self::launch_with_outputs_roots_and_authorities(
            executable,
            worker_id,
            channel_id,
            journal_path,
            outputs,
            cg_image_root,
            proof_artifact_root,
            fallback_slate,
            None,
            output_profile,
            timeout,
        )
    }

    #[allow(clippy::too_many_arguments)]
    pub fn launch_with_outputs_roots_and_authorities(
        executable: &Path,
        worker_id: &str,
        channel_id: &str,
        journal_path: &Path,
        outputs: &[WorkerOutputConfiguration],
        cg_image_root: &Path,
        proof_artifact_root: &Path,
        fallback_slate: Option<WorkerFallbackSlate>,
        caption_policy: Option<WorkerCaptionPolicy>,
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
            fallback_slate,
            caption_policy,
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
        let journal_path = journal_path.to_string_lossy();
        let (child, containment) = launch_contained_process(
            executable,
            &[worker_id, channel_id, &journal_path, server.name()],
        )
        .map_err(SupervisorError::Ipc)?;

        let mut pipe = match server.accept_timeout(timeout.saturating_sub(started.elapsed())) {
            Ok(pipe) => pipe,
            Err(error) => {
                return Err(abandon_launch(
                    child,
                    containment,
                    SupervisorError::Ipc(error),
                ));
            }
        };
        if let Err(error) =
            pipe.write_all_timeout(&bootstrap_frame, timeout.saturating_sub(started.elapsed()))
        {
            return Err(abandon_launch(
                child,
                containment,
                SupervisorError::Ipc(error),
            ));
        }
        let mut pending_lifecycle_events = VecDeque::new();
        let mut initial_sequence: Option<u64> = None;
        let ready = loop {
            let event = match read_event(&mut pipe, timeout.saturating_sub(started.elapsed())) {
                Ok(event) => event,
                Err(error) => {
                    return Err(abandon_launch(child, containment, error));
                }
            };
            if let Err(error) = validate_identity(&event, worker_id, channel_id) {
                return Err(abandon_launch(child, containment, error));
            }
            if event.sequence == 0 {
                return Err(abandon_launch(
                    child,
                    containment,
                    SupervisorError::InvalidInitialSequence,
                ));
            }
            if let Some(previous) = initial_sequence {
                let expected = match previous.checked_add(1) {
                    Some(expected) => expected,
                    None => {
                        return Err(abandon_launch(
                            child,
                            containment,
                            SupervisorError::InvalidInitialSequence,
                        ));
                    }
                };
                if event.sequence != expected {
                    return Err(abandon_launch(
                        child,
                        containment,
                        SupervisorError::WrongSequence {
                            expected,
                            actual: event.sequence,
                        },
                    ));
                }
            }
            initial_sequence = Some(event.sequence);
            if matches!(event.event, WorkerEvent::Ready { .. }) {
                break event;
            }
            if !is_recovery_lifecycle_event(&event) {
                return Err(abandon_launch(
                    child,
                    containment,
                    SupervisorError::UnexpectedInitialEvent,
                ));
            }
            if pending_lifecycle_events.len() == MAX_PENDING_LIFECYCLE_EVENTS {
                return Err(abandon_launch(
                    child,
                    containment,
                    SupervisorError::LifecycleEventOverflow,
                ));
            }
            pending_lifecycle_events.push_back(event);
        };

        Ok(Self {
            child: Some(child),
            containment: Some(containment),
            pipe,
            worker_id: worker_id.to_string(),
            channel_id: channel_id.to_string(),
            last_sequence: ready.sequence,
            ready,
            pending_lifecycle_events,
        })
    }

    pub fn ready(&self) -> &WorkerEventEnvelope {
        &self.ready
    }

    /// Returns lifecycle changes that occurred without a command.  These are
    /// sequenced and durably journaled just like command acknowledgements, but
    /// must never be mistaken for a reply to a later command.
    pub fn take_lifecycle_events(&mut self) -> Vec<WorkerEventEnvelope> {
        self.pending_lifecycle_events.drain(..).collect()
    }

    pub fn ensure_running(&mut self) -> Result<(), SupervisorError> {
        let child = self.child.as_ref().ok_or(SupervisorError::AlreadyStopped)?;
        match child.try_wait().map_err(SupervisorError::Ipc)? {
            Some(status) => {
                self.child.take();
                Err(SupervisorError::ProcessExit(Some(status as i32)))
            }
            None => Ok(()),
        }
    }

    /// Ends an ambiguously synchronized worker session. Once a protected-pipe
    /// command times out, the supervisor cannot know whether the worker acted
    /// before its reply was lost; process termination is the only fail-closed
    /// way to prevent that command from continuing in the background.
    pub fn force_terminate(&mut self) -> Result<(), SupervisorError> {
        let containment = self.containment.take();
        let Some(child) = self.child.take() else {
            // The direct worker may already have exited while one of its media
            // descendants remains in the job. Closing containment is still the
            // authoritative whole-tree termination action in that state.
            drop(containment);
            return Ok(());
        };
        // Closing the job asks the kernel to terminate the worker and every
        // descendant immediately. We then reap the direct child only until the
        // explicit bound; no detached waiter can survive this controller.
        terminate_bounded(&child, containment, FORCE_TERMINATE_TIMEOUT)
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
        let started = Instant::now();
        self.drain_unsolicited_events()?;
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
            .write_all_timeout(&frame, timeout.saturating_sub(started.elapsed()))
            .map_err(SupervisorError::Ipc)?;

        loop {
            let event = read_event(&mut self.pipe, timeout.saturating_sub(started.elapsed()))?;
            self.accept_sequenced_event(&event)?;
            if is_unsolicited_lifecycle_event(&event) {
                self.queue_lifecycle_event(event)?;
                continue;
            }
            // A command acknowledgement must be the next non-lifecycle event
            // after its sequence was assigned.  Lifecycle events advance the
            // cursor but cannot be returned as a command response.
            if event.sequence < expected_sequence {
                return Err(SupervisorError::WrongSequence {
                    expected: expected_sequence,
                    actual: event.sequence,
                });
            }
            return Ok(event);
        }
    }

    fn drain_unsolicited_events(&mut self) -> Result<(), SupervisorError> {
        while let Some(event) = read_event_if_ready(&mut self.pipe)? {
            // Do not advance the durable cursor until this frame has been
            // proven to be an unsolicited lifecycle event.  An unexpected
            // command acknowledgement already waiting in the pipe is a
            // terminal synchronization fault, not a consumed event that can
            // be silently folded into the next command's sequence.
            self.validate_sequenced_event(&event)?;
            if !is_unsolicited_lifecycle_event(&event) {
                return Err(SupervisorError::WrongSequence {
                    expected: self.last_sequence + 1,
                    actual: event.sequence,
                });
            }
            let sequence = event.sequence;
            self.queue_lifecycle_event(event)?;
            self.last_sequence = sequence;
        }
        Ok(())
    }

    fn validate_sequenced_event(&self, event: &WorkerEventEnvelope) -> Result<(), SupervisorError> {
        validate_identity(event, &self.worker_id, &self.channel_id)?;
        let expected_sequence = self.last_sequence + 1;
        if event.sequence != expected_sequence {
            return Err(SupervisorError::WrongSequence {
                expected: expected_sequence,
                actual: event.sequence,
            });
        }
        Ok(())
    }

    fn accept_sequenced_event(
        &mut self,
        event: &WorkerEventEnvelope,
    ) -> Result<(), SupervisorError> {
        self.validate_sequenced_event(event)?;
        self.last_sequence = event.sequence;
        Ok(())
    }

    fn queue_lifecycle_event(&mut self, event: WorkerEventEnvelope) -> Result<(), SupervisorError> {
        if self.pending_lifecycle_events.len() == MAX_PENDING_LIFECYCLE_EVENTS {
            return Err(SupervisorError::LifecycleEventOverflow);
        }
        self.pending_lifecycle_events.push_back(event);
        Ok(())
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
        let child = self.child.take().ok_or(SupervisorError::AlreadyStopped)?;
        let status = wait_for_exit(&child, remaining)?;
        if status != 0 {
            return Err(SupervisorError::ProcessExit(Some(status as i32)));
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
        // Job closure is deliberately nonblocking and covers the whole worker
        // tree. Drop must never wait for process exit, because Drop may run while
        // unwinding an unrelated station failure.
        drop(self.containment.take());
        if let Some(child) = self.child.take() {
            let _ = request_termination(&child);
        }
    }
}

fn read_event(
    pipe: &mut PipeStream,
    timeout: Duration,
) -> Result<WorkerEventEnvelope, SupervisorError> {
    let started = Instant::now();
    let mut length = [0_u8; 4];
    pipe.read_exact_timeout(&mut length, timeout)
        .map_err(SupervisorError::Ipc)?;
    let payload_length = u32::from_le_bytes(length) as usize;
    if payload_length > MAX_FRAME_BYTES {
        return Err(SupervisorError::Protocol(ProtocolError::FrameTooLarge(
            payload_length,
        )));
    }
    let mut frame = Vec::with_capacity(4 + payload_length);
    frame.extend_from_slice(&length);
    frame.resize(4 + payload_length, 0);
    pipe.read_exact_timeout(&mut frame[4..], timeout.saturating_sub(started.elapsed()))
        .map_err(SupervisorError::Ipc)?;
    decode_event_frame(&frame).map_err(SupervisorError::Protocol)
}

fn read_event_if_ready(
    pipe: &mut PipeStream,
) -> Result<Option<WorkerEventEnvelope>, SupervisorError> {
    let mut length = [0_u8; 4];
    match pipe.peek_exact_timeout(&mut length, Duration::ZERO) {
        Ok(()) => {}
        Err(IpcError::TimedOut { .. }) => return Ok(None),
        Err(error) => return Err(SupervisorError::Ipc(error)),
    }
    let payload_length = u32::from_le_bytes(length) as usize;
    if payload_length > MAX_FRAME_BYTES {
        return Err(SupervisorError::Protocol(ProtocolError::FrameTooLarge(
            payload_length,
        )));
    }
    let mut frame = vec![0_u8; 4 + payload_length];
    match pipe.peek_exact_timeout(&mut frame, Duration::ZERO) {
        Ok(()) => {}
        Err(IpcError::TimedOut { .. }) => return Ok(None),
        Err(error) => return Err(SupervisorError::Ipc(error)),
    }
    pipe.read_exact_timeout(&mut frame, Duration::ZERO)
        .map_err(SupervisorError::Ipc)?;
    decode_event_frame(&frame)
        .map(Some)
        .map_err(SupervisorError::Protocol)
}

fn is_unsolicited_lifecycle_event(event: &WorkerEventEnvelope) -> bool {
    matches!(
        &event.event,
        WorkerEvent::FallbackToneDeactivated {
            deactivation: Some(deactivation),
            ..
        } if matches!(
            deactivation.reason,
            station_media_protocol::FallbackToneDeactivationReason::Expired
                | station_media_protocol::FallbackToneDeactivationReason::Shutdown
                | station_media_protocol::FallbackToneDeactivationReason::Recovery
        )
    )
}

fn is_recovery_lifecycle_event(event: &WorkerEventEnvelope) -> bool {
    matches!(
        &event.event,
        WorkerEvent::FallbackToneDeactivated {
            deactivation: Some(deactivation),
            ..
        } if deactivation.reason == station_media_protocol::FallbackToneDeactivationReason::Recovery
    )
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

fn wait_for_exit(child: &ContainedProcess, timeout: Duration) -> Result<u32, SupervisorError> {
    let started = Instant::now();
    loop {
        if let Some(status) = child.try_wait().map_err(SupervisorError::Ipc)? {
            return Ok(status);
        }
        if started.elapsed() >= timeout {
            return match terminate_bounded(child, None, FORCE_TERMINATE_TIMEOUT) {
                Ok(()) | Err(SupervisorError::ProcessExitTimedOut) => {
                    Err(SupervisorError::ProcessExitTimedOut)
                }
                Err(error) => Err(error),
            };
        }
        std::thread::sleep(Duration::from_millis(2));
    }
}

fn abandon_launch(
    child: ContainedProcess,
    containment: ProcessContainment,
    error: SupervisorError,
) -> SupervisorError {
    // Releasing the kill-on-close job is a bounded kernel request. The direct handle is
    // then dropped without waiting, so every failed launch path returns even
    // if Windows is slow to report process exit.
    drop(containment);
    drop(child);
    error
}

fn request_termination(child: &ContainedProcess) -> Result<(), SupervisorError> {
    child.terminate().map_err(SupervisorError::Ipc)
}

fn terminate_bounded(
    child: &ContainedProcess,
    containment: Option<ProcessContainment>,
    timeout: Duration,
) -> Result<(), SupervisorError> {
    // Close containment first. This covers descendants that direct termination
    // cannot reach and removes the need for an unbounded detached reaper.
    let job_close_requested_termination = containment.is_some();
    drop(containment);
    match child.try_wait().map_err(SupervisorError::Ipc)? {
        Some(_) => return Ok(()),
        None => match request_termination(child) {
            Ok(()) => {}
            // Closing a job may already have placed its direct process into
            // termination before the immediate zero-time wait observes the
            // signaled state. The owned process handle cannot otherwise be
            // access-denied, so continue the bounded observation in this one
            // proven-contained case.
            Err(SupervisorError::Ipc(IpcError::Os { code: 5, .. }))
                if job_close_requested_termination => {}
            Err(error) => return Err(error),
        },
    }

    let started = Instant::now();
    loop {
        if child.try_wait().map_err(SupervisorError::Ipc)?.is_some() {
            return Ok(());
        }
        if started.elapsed() >= timeout {
            return Err(SupervisorError::ProcessExitTimedOut);
        }
        std::thread::sleep(Duration::from_millis(2));
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
    #[cfg(windows)]
    use std::fs;
    #[cfg(windows)]
    use std::sync::atomic::{AtomicU64, Ordering};

    #[cfg(windows)]
    static TEST_PATH_SEQUENCE: AtomicU64 = AtomicU64::new(1);

    fn destination(value: &str) -> SocketAddr {
        value.parse().expect("test destination is valid")
    }

    #[cfg(windows)]
    fn unique_test_path(name: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "townlight-station-runtime-{name}-{}-{}",
            std::process::id(),
            TEST_PATH_SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ))
    }

    #[cfg(windows)]
    fn wait_for_path(path: &Path, timeout: Duration) -> bool {
        let started = Instant::now();
        while started.elapsed() < timeout {
            if path.exists() {
                return true;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        path.exists()
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

    #[cfg(windows)]
    #[test]
    fn bounded_termination_kills_and_reaps_a_live_child() {
        let (child, containment) = launch_contained_process(
            Path::new("powershell.exe"),
            &[
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Start-Sleep -Seconds 30",
            ],
        )
        .unwrap();
        let started = Instant::now();

        terminate_bounded(&child, Some(containment), Duration::from_secs(3)).unwrap();

        assert!(started.elapsed() < Duration::from_secs(4));
        assert!(child.try_wait().unwrap().is_some());
    }

    #[cfg(windows)]
    #[test]
    fn supervisor_drop_is_bounded_and_releases_worker_containment() {
        let server = PipeServer::bind("runtime-drop-bounded-test").unwrap();
        let name = server.name().to_string();
        let peer = std::thread::spawn(move || {
            let _pipe = PipeStream::connect(&name).unwrap();
            std::thread::sleep(Duration::from_millis(200));
        });
        let pipe = server.accept_timeout(Duration::from_secs(1)).unwrap();
        let (child, containment) = launch_contained_process(
            Path::new("powershell.exe"),
            &[
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Start-Sleep -Seconds 30",
            ],
        )
        .unwrap();
        let supervisor = WorkerSupervisor {
            child: Some(child),
            containment: Some(containment),
            pipe,
            worker_id: "worker".into(),
            channel_id: "channel".into(),
            last_sequence: 1,
            ready: WorkerEventEnvelope {
                version: PROTOCOL_VERSION,
                worker_id: "worker".into(),
                channel_id: "channel".into(),
                sequence: 1,
                event: WorkerEvent::Ready { graph_revision: 1 },
            },
            pending_lifecycle_events: VecDeque::new(),
        };
        let started = Instant::now();

        drop(supervisor);

        assert!(
            started.elapsed() < Duration::from_secs(1),
            "WorkerSupervisor::drop must never wait for process exit"
        );
        peer.join().unwrap();
    }

    #[cfg(windows)]
    #[test]
    fn force_terminate_closes_containment_after_direct_worker_has_exited() {
        let descendant_ready = unique_test_path("descendant-ready");
        let descendant_marker = unique_test_path("descendant-marker");
        let _ = fs::remove_file(&descendant_ready);
        let _ = fs::remove_file(&descendant_marker);
        let ready_path = descendant_ready.to_string_lossy().replace('\'', "''");
        let marker_path = descendant_marker.to_string_lossy().replace('\'', "''");
        let script = format!(
            "$descendant = Start-Process -FilePath powershell.exe -ArgumentList '-NoLogo','-NoProfile','-NonInteractive','-Command','Start-Sleep -Seconds 3; Set-Content -LiteralPath ''{marker_path}'' -Value escaped' -PassThru; Set-Content -LiteralPath '{ready_path}' -Value $descendant.Id"
        );
        let (child, containment) = launch_contained_process(
            Path::new("powershell.exe"),
            &[
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                &script,
            ],
        )
        .unwrap();
        assert!(
            wait_for_path(&descendant_ready, Duration::from_secs(3)),
            "the direct worker must launch its descendant before exiting"
        );
        let exited_at = Instant::now();
        while child.try_wait().unwrap().is_none() {
            assert!(
                exited_at.elapsed() < Duration::from_secs(3),
                "the direct worker did not exit within the test bound"
            );
            std::thread::sleep(Duration::from_millis(10));
        }

        let pipe_suffix = format!(
            "runtime-exited-parent-{}-{}",
            std::process::id(),
            TEST_PATH_SEQUENCE.fetch_add(1, Ordering::Relaxed)
        );
        let server = PipeServer::bind(&pipe_suffix).unwrap();
        let name = server.name().to_string();
        let peer = std::thread::spawn(move || {
            let _pipe = PipeStream::connect(&name).unwrap();
            std::thread::sleep(Duration::from_secs(5));
        });
        let pipe = server.accept_timeout(Duration::from_secs(1)).unwrap();
        let mut supervisor = WorkerSupervisor {
            child: Some(child),
            containment: Some(containment),
            pipe,
            worker_id: "worker".into(),
            channel_id: "channel".into(),
            last_sequence: 1,
            ready: WorkerEventEnvelope {
                version: PROTOCOL_VERSION,
                worker_id: "worker".into(),
                channel_id: "channel".into(),
                sequence: 1,
                event: WorkerEvent::Ready { graph_revision: 1 },
            },
            pending_lifecycle_events: VecDeque::new(),
        };

        assert!(matches!(
            supervisor.ensure_running(),
            Err(SupervisorError::ProcessExit(Some(_)))
        ));
        assert!(supervisor.child.is_none());
        supervisor.force_terminate().unwrap();
        assert!(supervisor.containment.is_none());
        std::thread::sleep(Duration::from_secs(4));
        assert!(
            !descendant_marker.exists(),
            "force_terminate must close the job even after the direct worker exits"
        );

        drop(supervisor);
        peer.join().unwrap();
        let _ = fs::remove_file(descendant_ready);
        let _ = fs::remove_file(descendant_marker);
    }
}
