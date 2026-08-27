use station_media_protocol::{
    MAX_SCHEDULED_RECORDING_BYTES, MAX_SCHEDULED_RECORDING_DURATION_MS,
    MIN_SCHEDULED_RECORDING_DURATION_MS,
};
use station_recording::{RecordingLifecycle, ScheduledRecording};
use station_schedule::ChannelConfiguration;

pub(crate) const RECORDING_START_DEADLINE_MS: i64 = 5_000;
pub(crate) const RECORDING_WORKER_ACTOR_ID: &str = "00000000-0000-4000-8000-000000000021";

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ActiveRecordingCapture {
    pub recording_id: String,
    pub output_index: u16,
    pub sink_id: String,
    pub duration_ms: u64,
    pub max_bytes: u64,
    pub artifact_relative_path: String,
    pub worker_instance_id: String,
    pub start_worker_sequence: u64,
    pub graph_revision: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ReadyRecordingCapture {
    pub active: ActiveRecordingCapture,
    pub terminal_worker_sequence: u64,
    pub size_bytes: u64,
    pub capture_sha256: String,
    pub captured_duration_ms: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum RecordingCaptureControllerState {
    Capturing(ActiveRecordingCapture),
    Ready(ReadyRecordingCapture),
}

impl ReadyRecordingCapture {
    pub(crate) fn validate(&self) -> Result<(), String> {
        self.active.validate()?;
        if self.terminal_worker_sequence <= self.active.start_worker_sequence
            || self.size_bytes == 0
            || self.size_bytes > self.active.max_bytes
            || self.captured_duration_ms == 0
            || self.captured_duration_ms > self.active.duration_ms
            || self.capture_sha256.len() != 64
            || !self
                .capture_sha256
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        {
            return Err("scheduled recording terminal capture facts are invalid".into());
        }
        Ok(())
    }
}

impl ActiveRecordingCapture {
    pub(crate) fn validate(&self) -> Result<(), String> {
        if self.artifact_relative_path != format!("{}.ts", self.recording_id) {
            return Err("scheduled recording worker returned an unexpected artifact path".into());
        }
        if self.worker_instance_id.trim().is_empty()
            || self.worker_instance_id.len() > 200
            || self.worker_instance_id.chars().any(char::is_control)
            || self.start_worker_sequence == 0
            || self.graph_revision == 0
        {
            return Err("scheduled recording worker authority is invalid".into());
        }
        if !(MIN_SCHEDULED_RECORDING_DURATION_MS..=MAX_SCHEDULED_RECORDING_DURATION_MS)
            .contains(&self.duration_ms)
            || !(1..=MAX_SCHEDULED_RECORDING_BYTES).contains(&self.max_bytes)
        {
            return Err("scheduled recording worker bounds are invalid".into());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ScheduledRecordingStartPlan {
    pub output_index: u16,
    pub sink_id: String,
    pub duration_ms: u64,
    pub max_bytes: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum ArmedRecordingPlan {
    Wait,
    Start(ScheduledRecordingStartPlan),
    Fail { reason: &'static str },
}

pub(crate) fn plan_armed_recording(
    recording: &ScheduledRecording,
    channel: &ChannelConfiguration,
    now_unix_ms: i64,
) -> Result<ArmedRecordingPlan, String> {
    recording.validate()?;
    channel
        .validate()
        .map_err(|error| format!("scheduled recording channel is invalid: {error:?}"))?;
    if recording.definition.channel_id != channel.channel_id {
        return Err("scheduled recording belongs to a different channel".into());
    }
    if !matches!(recording.lifecycle, RecordingLifecycle::Armed { .. }) {
        return Err("only an armed scheduled recording can be planned for capture".into());
    }
    if now_unix_ms < 0 {
        return Err("scheduled recording controller time must be nonnegative".into());
    }
    if now_unix_ms < recording.definition.scheduled_start_unix_ms {
        return Ok(ArmedRecordingPlan::Wait);
    }
    let start_deadline = recording
        .definition
        .scheduled_start_unix_ms
        .checked_add(RECORDING_START_DEADLINE_MS)
        .ok_or_else(|| "scheduled recording start deadline overflowed".to_string())?;
    if now_unix_ms > start_deadline || now_unix_ms >= recording.definition.scheduled_end_unix_ms {
        return Ok(ArmedRecordingPlan::Fail {
            reason: "The recording missed its bounded scheduled-start deadline.",
        });
    }

    let mut first_enabled = None;
    let mut first_required = None;
    for (worker_index, sink) in channel
        .output_sinks
        .iter()
        .filter(|sink| sink.enabled)
        .enumerate()
    {
        let binding = (
            u16::try_from(worker_index)
                .map_err(|_| "scheduled recording output index exceeds protocol range")?,
            sink.sink_id.clone(),
        );
        first_enabled.get_or_insert_with(|| binding.clone());
        if sink.required && first_required.is_none() {
            first_required = Some(binding);
        }
    }
    let (output_index, sink_id) = first_required
        .or(first_enabled)
        .ok_or_else(|| "scheduled recording channel has no enabled output sink".to_string())?;
    let duration_ms = u64::try_from(recording.definition.scheduled_end_unix_ms - now_unix_ms)
        .map_err(|_| "scheduled recording remaining duration is invalid")?;
    if !(MIN_SCHEDULED_RECORDING_DURATION_MS..=MAX_SCHEDULED_RECORDING_DURATION_MS)
        .contains(&duration_ms)
    {
        return Ok(ArmedRecordingPlan::Fail {
            reason: "The remaining recording window is outside the bounded capture contract.",
        });
    }

    // Convert the exact transport rate and milliseconds to bytes, then add
    // 25% bounded container/queue headroom. The protocol ceiling remains the
    // final authority even if a future profile grows beyond this calculation.
    let transport_bits = u128::from(channel.output_profile.profile().transport_bitrate_bps)
        .checked_mul(u128::from(duration_ms))
        .ok_or_else(|| "scheduled recording byte budget overflowed".to_string())?;
    let transport_bytes = transport_bits.div_ceil(8_000);
    let with_headroom = transport_bytes
        .checked_mul(5)
        .ok_or_else(|| "scheduled recording byte headroom overflowed".to_string())?
        .div_ceil(4);
    let max_bytes = u64::try_from(with_headroom)
        .unwrap_or(MAX_SCHEDULED_RECORDING_BYTES)
        .clamp(1, MAX_SCHEDULED_RECORDING_BYTES);

    Ok(ArmedRecordingPlan::Start(ScheduledRecordingStartPlan {
        output_index,
        sink_id,
        duration_ms,
        max_bytes,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use station_recording::{
        RecordingActor, RecordingActorRole, RecordingCandidateIdentity, RecordingDefinition,
        RecordingExecutionAuthority,
    };
    use station_schedule::{OutputProfileId, OutputSinkConfiguration};

    const CHANNEL_ID: &str = "11111111-1111-4111-8111-111111111111";
    const RECORDING_ID: &str = "22222222-2222-4222-8222-222222222222";
    const SCHEDULE_ID: &str = "33333333-3333-4333-8333-333333333333";
    const DISABLED_SINK_ID: &str = "55555555-5555-4555-8555-555555555555";
    const OPTIONAL_SINK_ID: &str = "66666666-6666-4666-8666-666666666666";
    const REQUIRED_SINK_ID: &str = "77777777-7777-4777-8777-777777777777";

    fn armed_recording() -> ScheduledRecording {
        ScheduledRecording {
            definition: RecordingDefinition {
                recording_id: RECORDING_ID.into(),
                channel_id: CHANNEL_ID.into(),
                schedule_item_id: SCHEDULE_ID.into(),
                schedule_revision: 2,
                scheduled_start_unix_ms: 10_000,
                scheduled_end_unix_ms: 3_610_000,
                requested_by: RecordingActor {
                    actor_id: "44444444-4444-4444-8444-444444444444".into(),
                    role: RecordingActorRole::Operator,
                },
                requested_at_unix_ms: 1_000,
            },
            lifecycle: RecordingLifecycle::Armed {
                armed_at_unix_ms: 2_000,
                execution: RecordingExecutionAuthority {
                    candidate: RecordingCandidateIdentity {
                        version: "0.1.0".into(),
                        source_commit: "a".repeat(40),
                    },
                    channel_configuration_hash: "b".repeat(64),
                },
            },
            revision: 2,
        }
    }

    fn sink(id: &str, port: u16, required: bool, enabled: bool) -> OutputSinkConfiguration {
        let mut sink =
            OutputSinkConfiguration::required_udp(id, id, format!("127.0.0.1:{port}"), -24.0);
        sink.required = required;
        sink.enabled = enabled;
        sink
    }

    fn channel() -> ChannelConfiguration {
        ChannelConfiguration {
            channel_id: CHANNEL_ID.into(),
            display_name: "Council".into(),
            output_profile: OutputProfileId::Atsc720p5994,
            output_sinks: vec![
                sink(DISABLED_SINK_ID, 5_001, true, false),
                sink(OPTIONAL_SINK_ID, 5_002, false, true),
                sink(REQUIRED_SINK_ID, 5_003, true, true),
            ],
            enabled: true,
        }
    }

    #[test]
    fn armed_recording_waits_then_uses_first_required_enabled_worker_sink() {
        let recording = armed_recording();
        let channel = channel();
        assert_eq!(
            plan_armed_recording(&recording, &channel, 9_999).unwrap(),
            ArmedRecordingPlan::Wait
        );
        assert_eq!(
            plan_armed_recording(&recording, &channel, 10_025).unwrap(),
            ArmedRecordingPlan::Start(ScheduledRecordingStartPlan {
                output_index: 1,
                sink_id: REQUIRED_SINK_ID.into(),
                duration_ms: 3_599_975,
                max_bytes: 4_499_968_750,
            })
        );
    }

    #[test]
    fn armed_recording_fails_after_the_explicit_start_deadline() {
        assert!(matches!(
            plan_armed_recording(
                &armed_recording(),
                &channel(),
                10_000 + RECORDING_START_DEADLINE_MS + 1,
            )
            .unwrap(),
            ArmedRecordingPlan::Fail { .. }
        ));
    }

    #[test]
    fn recording_byte_limit_is_bounded_for_the_full_protocol_window() {
        let mut recording = armed_recording();
        recording.definition.scheduled_end_unix_ms =
            recording.definition.scheduled_start_unix_ms + 86_400_000;
        let ArmedRecordingPlan::Start(ScheduledRecordingStartPlan { max_bytes, .. }) =
            plan_armed_recording(&recording, &channel(), 10_000).unwrap()
        else {
            panic!("recording should start at its exact scheduled time");
        };
        assert!(max_bytes <= MAX_SCHEDULED_RECORDING_BYTES);
    }

    #[test]
    fn active_capture_requires_the_exact_managed_artifact_name_and_worker_authority() {
        let capture = ActiveRecordingCapture {
            recording_id: RECORDING_ID.into(),
            output_index: 1,
            sink_id: REQUIRED_SINK_ID.into(),
            duration_ms: 60_000,
            max_bytes: 1_000_000,
            artifact_relative_path: format!("{RECORDING_ID}.ts"),
            worker_instance_id: "worker-boot-one".into(),
            start_worker_sequence: 8,
            graph_revision: 2,
        };
        capture.validate().unwrap();

        let mut escaped = capture.clone();
        escaped.artifact_relative_path = "..\\foreign.ts".into();
        assert!(escaped.validate().is_err());

        let mut unbound = capture;
        unbound.graph_revision = 0;
        assert!(unbound.validate().is_err());
    }

    #[test]
    fn ready_capture_is_bound_to_started_limits_and_lowercase_digest() {
        let active = ActiveRecordingCapture {
            recording_id: RECORDING_ID.into(),
            output_index: 1,
            sink_id: REQUIRED_SINK_ID.into(),
            duration_ms: 60_000,
            max_bytes: 1_000_000,
            artifact_relative_path: format!("{RECORDING_ID}.ts"),
            worker_instance_id: "worker-boot-one".into(),
            start_worker_sequence: 8,
            graph_revision: 2,
        };
        let mut ready = ReadyRecordingCapture {
            active,
            terminal_worker_sequence: 9,
            size_bytes: 750_000,
            capture_sha256: "a".repeat(64),
            captured_duration_ms: 59_000,
        };
        ready.validate().unwrap();
        ready.capture_sha256 = "A".repeat(64);
        assert!(ready.validate().is_err());
    }
}
