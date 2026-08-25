//! Pure, fail-closed scheduled-recording authority contracts.
//!
//! Capture, persistence, schedule materialization, API, and operator-interface
//! boundaries are deliberately outside this crate.

use serde::{Deserialize, Serialize};

const MAX_TEXT: usize = 200;
const MAX_REASON: usize = 1_000;
const MAX_RECORDING_WINDOW_MS: i64 = 86_400_000;
const MAX_FINALIZATION_WINDOW_MS: i64 = 604_800_000;
const MAX_CAPTURE_DURATION_DRIFT_MS: i64 = 300_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RecordingActorRole {
    Operator,
    Supervisor,
    RecordingWorker,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RecordingActor {
    pub actor_id: String,
    pub role: RecordingActorRole,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RecordingCandidateIdentity {
    pub version: String,
    pub source_commit: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RecordingExecutionAuthority {
    pub candidate: RecordingCandidateIdentity,
    pub channel_configuration_hash: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RecordingDefinition {
    pub recording_id: String,
    pub channel_id: String,
    pub schedule_item_id: String,
    pub schedule_revision: u64,
    pub scheduled_start_unix_ms: i64,
    pub scheduled_end_unix_ms: i64,
    pub requested_by: RecordingActor,
    pub requested_at_unix_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RecordingLifecycle {
    Draft,
    Armed {
        armed_at_unix_ms: i64,
        execution: RecordingExecutionAuthority,
    },
    Capturing {
        capture_started_at_unix_ms: i64,
        execution: RecordingExecutionAuthority,
    },
    Finalizing {
        capture_started_at_unix_ms: i64,
        finalization_started_at_unix_ms: i64,
        recovered_after_interruption: bool,
        execution: RecordingExecutionAuthority,
    },
    Completed {
        capture_started_at_unix_ms: i64,
        finalized_at_unix_ms: i64,
        captured_asset_hash: String,
        captured_duration_ms: i64,
        recovered_after_interruption: bool,
        execution: RecordingExecutionAuthority,
    },
    Failed {
        failed_at_unix_ms: i64,
        reason: String,
        execution: RecordingExecutionAuthority,
    },
    CancelledBeforeArm {
        cancelled_at_unix_ms: i64,
        reason: String,
    },
    CancelledAfterArm {
        cancelled_at_unix_ms: i64,
        reason: String,
        execution: RecordingExecutionAuthority,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RecordingTransitionKind {
    Arm,
    StartCapture,
    BeginFinalization,
    RecoverForFinalization,
    FinalizeSuccess,
    Fail,
    Cancel,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", tag = "kind", deny_unknown_fields)]
pub enum RecordingEvent {
    Arm,
    StartCapture,
    BeginFinalization,
    RecoverForFinalization {
        reason: String,
    },
    FinalizeSuccess {
        captured_asset_hash: String,
        captured_duration_ms: i64,
    },
    Fail {
        reason: String,
    },
    Cancel {
        reason: String,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RecordingTransitionRequest {
    pub expected_revision: u64,
    pub actor: RecordingActor,
    pub occurred_at_unix_ms: i64,
    pub execution: Option<RecordingExecutionAuthority>,
    pub event: RecordingEvent,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RecordingTransitionReceipt {
    pub recording_id: String,
    pub prior_revision: u64,
    pub resulting_revision: u64,
    pub kind: RecordingTransitionKind,
    pub actor: RecordingActor,
    pub occurred_at_unix_ms: i64,
    pub execution: Option<RecordingExecutionAuthority>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScheduledRecording {
    pub definition: RecordingDefinition,
    pub lifecycle: RecordingLifecycle,
    pub revision: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RecordingTransitionOutcome {
    pub recording: ScheduledRecording,
    pub receipt: RecordingTransitionReceipt,
}

impl RecordingActor {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.actor_id, "recording actor identity")
    }

    fn is_operator(&self) -> bool {
        matches!(
            self.role,
            RecordingActorRole::Operator | RecordingActorRole::Supervisor
        )
    }

    fn is_worker(&self) -> bool {
        self.role == RecordingActorRole::RecordingWorker
    }
}

impl RecordingCandidateIdentity {
    pub fn validate(&self) -> Result<(), String> {
        validate_visible(&self.version, MAX_TEXT, "recording candidate version")?;
        if !is_source_commit(&self.source_commit) {
            return Err(
                "recording candidate source commit must be a 40-character lowercase hash".into(),
            );
        }
        Ok(())
    }
}

impl RecordingExecutionAuthority {
    pub fn validate(&self) -> Result<(), String> {
        self.candidate.validate()?;
        if !is_sha256(&self.channel_configuration_hash) {
            return Err("recording execution configuration hash must be a SHA-256 hash".into());
        }
        Ok(())
    }
}

impl RecordingDefinition {
    pub fn validate(&self) -> Result<(), String> {
        validate_uuid(&self.recording_id, "recording identity")?;
        validate_uuid(&self.channel_id, "recording channel identity")?;
        validate_uuid(&self.schedule_item_id, "recording schedule item identity")?;
        if self.schedule_revision == 0 {
            return Err("recording schedule revision must be nonzero".into());
        }
        if self.scheduled_start_unix_ms < 0
            || self.scheduled_end_unix_ms <= self.scheduled_start_unix_ms
            || self.scheduled_end_unix_ms - self.scheduled_start_unix_ms > MAX_RECORDING_WINDOW_MS
        {
            return Err("recording schedule window is invalid or unbounded".into());
        }
        self.requested_by.validate()?;
        if !self.requested_by.is_operator() {
            return Err("recording definition requires an operator or supervisor identity".into());
        }
        if self.requested_at_unix_ms < 0
            || self.requested_at_unix_ms >= self.scheduled_start_unix_ms
        {
            return Err(
                "recording request time must be nonnegative and before scheduled start".into(),
            );
        }
        Ok(())
    }
}

impl RecordingEvent {
    pub const fn kind(&self) -> RecordingTransitionKind {
        match self {
            Self::Arm => RecordingTransitionKind::Arm,
            Self::StartCapture => RecordingTransitionKind::StartCapture,
            Self::BeginFinalization => RecordingTransitionKind::BeginFinalization,
            Self::RecoverForFinalization { .. } => RecordingTransitionKind::RecoverForFinalization,
            Self::FinalizeSuccess { .. } => RecordingTransitionKind::FinalizeSuccess,
            Self::Fail { .. } => RecordingTransitionKind::Fail,
            Self::Cancel { .. } => RecordingTransitionKind::Cancel,
        }
    }
}

impl ScheduledRecording {
    pub fn new(definition: RecordingDefinition) -> Result<Self, String> {
        definition.validate()?;
        Ok(Self {
            definition,
            lifecycle: RecordingLifecycle::Draft,
            revision: 1,
        })
    }

    pub fn validate(&self) -> Result<(), String> {
        self.definition.validate()?;
        if self.revision == 0 {
            return Err("recording revision must be nonzero".into());
        }
        validate_lifecycle(&self.definition, &self.lifecycle)
    }

    pub fn transition(
        &self,
        request: RecordingTransitionRequest,
    ) -> Result<RecordingTransitionOutcome, String> {
        self.validate()?;
        validate_request(&request)?;
        if request.expected_revision != self.revision {
            return Err(
                "recording transition revision does not match current recording revision".into(),
            );
        }
        let lifecycle = transition_lifecycle(&self.definition, &self.lifecycle, &request)?;
        let resulting_revision = self
            .revision
            .checked_add(1)
            .ok_or_else(|| "recording revision overflow is invalid".to_string())?;
        let recording = Self {
            definition: self.definition.clone(),
            lifecycle,
            revision: resulting_revision,
        };
        recording.validate()?;
        Ok(RecordingTransitionOutcome {
            receipt: RecordingTransitionReceipt {
                recording_id: self.definition.recording_id.clone(),
                prior_revision: self.revision,
                resulting_revision,
                kind: request.event.kind(),
                actor: request.actor,
                occurred_at_unix_ms: request.occurred_at_unix_ms,
                execution: request.execution,
            },
            recording,
        })
    }
}

fn validate_request(request: &RecordingTransitionRequest) -> Result<(), String> {
    if request.expected_revision == 0 || request.occurred_at_unix_ms < 0 {
        return Err("recording transition revision or time is invalid".into());
    }
    request.actor.validate()?;
    if let Some(execution) = &request.execution {
        execution.validate()?;
    }
    match &request.event {
        RecordingEvent::RecoverForFinalization { reason }
        | RecordingEvent::Fail { reason }
        | RecordingEvent::Cancel { reason } => {
            validate_visible(reason, MAX_REASON, "recording reason")
        }
        RecordingEvent::FinalizeSuccess {
            captured_asset_hash,
            captured_duration_ms,
        } => {
            if !is_sha256(captured_asset_hash) {
                return Err("recording captured asset hash must be a SHA-256 hash".into());
            }
            if *captured_duration_ms <= 0 {
                return Err("recording captured duration must be positive".into());
            }
            Ok(())
        }
        RecordingEvent::Arm | RecordingEvent::StartCapture | RecordingEvent::BeginFinalization => {
            Ok(())
        }
    }
}

fn transition_lifecycle(
    definition: &RecordingDefinition,
    lifecycle: &RecordingLifecycle,
    request: &RecordingTransitionRequest,
) -> Result<RecordingLifecycle, String> {
    let occurred_at = request.occurred_at_unix_ms;
    match (lifecycle, &request.event) {
        (RecordingLifecycle::Draft, RecordingEvent::Arm) => {
            require_operator(&request.actor)?;
            let execution = require_execution(request)?.clone();
            require_inclusive_window(
                occurred_at,
                definition.requested_at_unix_ms,
                definition.scheduled_start_unix_ms - 1,
                "arming time",
            )?;
            Ok(RecordingLifecycle::Armed {
                armed_at_unix_ms: occurred_at,
                execution,
            })
        }
        (RecordingLifecycle::Armed { execution, .. }, RecordingEvent::StartCapture) => {
            require_worker(&request.actor)?;
            require_matching_execution(request, execution)?;
            require_inclusive_window(
                occurred_at,
                definition.scheduled_start_unix_ms,
                definition.scheduled_end_unix_ms - 1,
                "capture start time",
            )?;
            Ok(RecordingLifecycle::Capturing {
                capture_started_at_unix_ms: occurred_at,
                execution: execution.clone(),
            })
        }
        (
            RecordingLifecycle::Capturing {
                capture_started_at_unix_ms,
                execution,
            },
            RecordingEvent::BeginFinalization,
        ) => {
            require_worker(&request.actor)?;
            require_matching_execution(request, execution)?;
            require_finalization_time(definition, occurred_at)?;
            Ok(RecordingLifecycle::Finalizing {
                capture_started_at_unix_ms: *capture_started_at_unix_ms,
                finalization_started_at_unix_ms: occurred_at,
                recovered_after_interruption: false,
                execution: execution.clone(),
            })
        }
        (
            RecordingLifecycle::Capturing {
                capture_started_at_unix_ms,
                execution,
            },
            RecordingEvent::RecoverForFinalization { .. },
        ) => {
            require_worker(&request.actor)?;
            require_matching_execution(request, execution)?;
            require_finalization_time(definition, occurred_at)?;
            Ok(RecordingLifecycle::Finalizing {
                capture_started_at_unix_ms: *capture_started_at_unix_ms,
                finalization_started_at_unix_ms: occurred_at,
                recovered_after_interruption: true,
                execution: execution.clone(),
            })
        }
        (
            RecordingLifecycle::Finalizing {
                capture_started_at_unix_ms,
                finalization_started_at_unix_ms,
                recovered_after_interruption,
                execution,
            },
            RecordingEvent::FinalizeSuccess {
                captured_asset_hash,
                captured_duration_ms,
            },
        ) => {
            require_worker(&request.actor)?;
            require_matching_execution(request, execution)?;
            if occurred_at < *finalization_started_at_unix_ms
                || occurred_at - *finalization_started_at_unix_ms > MAX_FINALIZATION_WINDOW_MS
            {
                return Err("recording finalization time is invalid or unbounded".into());
            }
            validate_capture_duration(definition, *captured_duration_ms)?;
            Ok(RecordingLifecycle::Completed {
                capture_started_at_unix_ms: *capture_started_at_unix_ms,
                finalized_at_unix_ms: occurred_at,
                captured_asset_hash: captured_asset_hash.clone(),
                captured_duration_ms: *captured_duration_ms,
                recovered_after_interruption: *recovered_after_interruption,
                execution: execution.clone(),
            })
        }
        (RecordingLifecycle::Draft, RecordingEvent::Cancel { reason }) => {
            require_operator(&request.actor)?;
            require_no_execution(request)?;
            if occurred_at < definition.requested_at_unix_ms
                || occurred_at >= definition.scheduled_start_unix_ms
            {
                return Err(
                    "recording cancellation is not allowed at or after scheduled start".into(),
                );
            }
            Ok(RecordingLifecycle::CancelledBeforeArm {
                cancelled_at_unix_ms: occurred_at,
                reason: reason.clone(),
            })
        }
        (
            RecordingLifecycle::Armed {
                armed_at_unix_ms,
                execution,
            },
            RecordingEvent::Cancel { reason },
        ) => {
            require_operator(&request.actor)?;
            require_matching_execution(request, execution)?;
            if occurred_at < *armed_at_unix_ms || occurred_at >= definition.scheduled_start_unix_ms
            {
                return Err(
                    "recording cancellation is not allowed at or after scheduled start".into(),
                );
            }
            Ok(RecordingLifecycle::CancelledAfterArm {
                cancelled_at_unix_ms: occurred_at,
                reason: reason.clone(),
                execution: execution.clone(),
            })
        }
        (
            RecordingLifecycle::Armed {
                armed_at_unix_ms,
                execution,
            },
            RecordingEvent::Fail { reason },
        ) => {
            require_worker(&request.actor)?;
            require_matching_execution(request, execution)?;
            if occurred_at < *armed_at_unix_ms
                || occurred_at > definition.scheduled_end_unix_ms + MAX_FINALIZATION_WINDOW_MS
            {
                return Err("recording failure time is invalid or unbounded".into());
            }
            Ok(RecordingLifecycle::Failed {
                failed_at_unix_ms: occurred_at,
                reason: reason.clone(),
                execution: execution.clone(),
            })
        }
        (
            RecordingLifecycle::Capturing {
                capture_started_at_unix_ms,
                execution,
            },
            RecordingEvent::Fail { reason },
        ) => {
            require_worker(&request.actor)?;
            require_matching_execution(request, execution)?;
            if occurred_at < *capture_started_at_unix_ms
                || occurred_at > definition.scheduled_end_unix_ms + MAX_FINALIZATION_WINDOW_MS
            {
                return Err("recording failure time is invalid or unbounded".into());
            }
            Ok(RecordingLifecycle::Failed {
                failed_at_unix_ms: occurred_at,
                reason: reason.clone(),
                execution: execution.clone(),
            })
        }
        (
            RecordingLifecycle::Finalizing {
                finalization_started_at_unix_ms,
                execution,
                ..
            },
            RecordingEvent::Fail { reason },
        ) => {
            require_worker(&request.actor)?;
            require_matching_execution(request, execution)?;
            if occurred_at < *finalization_started_at_unix_ms
                || occurred_at > definition.scheduled_end_unix_ms + MAX_FINALIZATION_WINDOW_MS
            {
                return Err("recording failure time is invalid or unbounded".into());
            }
            Ok(RecordingLifecycle::Failed {
                failed_at_unix_ms: occurred_at,
                reason: reason.clone(),
                execution: execution.clone(),
            })
        }
        _ => Err("recording transition is not allowed from the current lifecycle state".into()),
    }
}

fn validate_lifecycle(
    definition: &RecordingDefinition,
    lifecycle: &RecordingLifecycle,
) -> Result<(), String> {
    match lifecycle {
        RecordingLifecycle::Draft => Ok(()),
        RecordingLifecycle::Armed {
            armed_at_unix_ms,
            execution,
        } => {
            execution.validate()?;
            require_inclusive_window(
                *armed_at_unix_ms,
                definition.requested_at_unix_ms,
                definition.scheduled_start_unix_ms - 1,
                "arming time",
            )
        }
        RecordingLifecycle::Capturing {
            capture_started_at_unix_ms,
            execution,
        } => {
            execution.validate()?;
            require_inclusive_window(
                *capture_started_at_unix_ms,
                definition.scheduled_start_unix_ms,
                definition.scheduled_end_unix_ms - 1,
                "capture start time",
            )
        }
        RecordingLifecycle::Finalizing {
            capture_started_at_unix_ms,
            finalization_started_at_unix_ms,
            execution,
            ..
        } => {
            execution.validate()?;
            require_inclusive_window(
                *capture_started_at_unix_ms,
                definition.scheduled_start_unix_ms,
                definition.scheduled_end_unix_ms - 1,
                "capture start time",
            )?;
            require_finalization_time(definition, *finalization_started_at_unix_ms)
        }
        RecordingLifecycle::Completed {
            capture_started_at_unix_ms,
            finalized_at_unix_ms,
            captured_asset_hash,
            captured_duration_ms,
            execution,
            ..
        } => {
            execution.validate()?;
            require_inclusive_window(
                *capture_started_at_unix_ms,
                definition.scheduled_start_unix_ms,
                definition.scheduled_end_unix_ms - 1,
                "capture start time",
            )?;
            if *finalized_at_unix_ms < definition.scheduled_end_unix_ms
                || *finalized_at_unix_ms - definition.scheduled_end_unix_ms
                    > MAX_FINALIZATION_WINDOW_MS
                || !is_sha256(captured_asset_hash)
            {
                return Err("recording completed lifecycle is invalid".into());
            }
            validate_capture_duration(definition, *captured_duration_ms)
        }
        RecordingLifecycle::Failed {
            failed_at_unix_ms,
            reason,
            execution,
        } => {
            execution.validate()?;
            if *failed_at_unix_ms < definition.requested_at_unix_ms
                || *failed_at_unix_ms
                    > definition.scheduled_end_unix_ms + MAX_FINALIZATION_WINDOW_MS
            {
                return Err("recording failure time is invalid or unbounded".into());
            }
            validate_visible(reason, MAX_REASON, "recording failure reason")
        }
        RecordingLifecycle::CancelledBeforeArm {
            cancelled_at_unix_ms,
            reason,
        } => {
            if *cancelled_at_unix_ms < definition.requested_at_unix_ms
                || *cancelled_at_unix_ms >= definition.scheduled_start_unix_ms
            {
                return Err("recording cancellation time is invalid".into());
            }
            validate_visible(reason, MAX_REASON, "recording cancellation reason")
        }
        RecordingLifecycle::CancelledAfterArm {
            cancelled_at_unix_ms,
            reason,
            execution,
        } => {
            execution.validate()?;
            if *cancelled_at_unix_ms < definition.requested_at_unix_ms
                || *cancelled_at_unix_ms >= definition.scheduled_start_unix_ms
            {
                return Err("recording cancellation time is invalid".into());
            }
            validate_visible(reason, MAX_REASON, "recording cancellation reason")
        }
    }
}

fn require_operator(actor: &RecordingActor) -> Result<(), String> {
    if actor.is_operator() {
        Ok(())
    } else {
        Err("recording transition requires an operator or supervisor identity".into())
    }
}

fn require_worker(actor: &RecordingActor) -> Result<(), String> {
    if actor.is_worker() {
        Ok(())
    } else {
        Err("recording transition requires the recording worker identity".into())
    }
}

fn require_execution(
    request: &RecordingTransitionRequest,
) -> Result<&RecordingExecutionAuthority, String> {
    request
        .execution
        .as_ref()
        .ok_or_else(|| "recording transition requires server-derived execution authority".into())
}

fn require_no_execution(request: &RecordingTransitionRequest) -> Result<(), String> {
    if request.execution.is_none() {
        Ok(())
    } else {
        Err("draft recording cancellation must not claim execution authority".into())
    }
}

fn require_matching_execution(
    request: &RecordingTransitionRequest,
    expected: &RecordingExecutionAuthority,
) -> Result<(), String> {
    let actual = require_execution(request)?;
    if actual != expected {
        return Err(
            "recording transition execution authority does not match armed authority".into(),
        );
    }
    Ok(())
}

fn require_finalization_time(
    definition: &RecordingDefinition,
    occurred_at_unix_ms: i64,
) -> Result<(), String> {
    require_inclusive_window(
        occurred_at_unix_ms,
        definition.scheduled_end_unix_ms,
        definition.scheduled_end_unix_ms + MAX_FINALIZATION_WINDOW_MS,
        "recording finalization time after scheduled end",
    )
}

fn validate_capture_duration(
    definition: &RecordingDefinition,
    captured_duration_ms: i64,
) -> Result<(), String> {
    let scheduled_duration_ms =
        definition.scheduled_end_unix_ms - definition.scheduled_start_unix_ms;
    if captured_duration_ms <= 0
        || captured_duration_ms < scheduled_duration_ms - MAX_CAPTURE_DURATION_DRIFT_MS
        || captured_duration_ms > scheduled_duration_ms + MAX_CAPTURE_DURATION_DRIFT_MS
    {
        return Err("recording captured duration is outside the bounded scheduled window".into());
    }
    Ok(())
}

fn require_inclusive_window(
    value: i64,
    minimum: i64,
    maximum: i64,
    field: &str,
) -> Result<(), String> {
    if value < minimum || value > maximum {
        return Err(format!("recording {field} is outside its allowed window"));
    }
    Ok(())
}

fn validate_uuid(value: &str, field: &str) -> Result<(), String> {
    let bytes = value.as_bytes();
    if bytes.len() != 36
        || ![8, 13, 18, 23].iter().all(|&index| bytes[index] == b'-')
        || bytes.iter().enumerate().any(|(index, byte)| {
            index != 8
                && index != 13
                && index != 18
                && index != 23
                && !(byte.is_ascii_digit() || (b'a'..=b'f').contains(byte))
        })
    {
        return Err(format!("{field} must be a lowercase canonical UUID"));
    }
    Ok(())
}

fn validate_visible(value: &str, maximum: usize, field: &str) -> Result<(), String> {
    if value.is_empty()
        || value.trim() != value
        || value.len() > maximum
        || value.chars().any(char::is_control)
    {
        return Err(format!(
            "{field} must be nonempty, trimmed, visible, and bounded"
        ));
    }
    Ok(())
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn is_source_commit(value: &str) -> bool {
    value.len() == 40
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}
