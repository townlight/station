use station_recording::{
    RecordingActor, RecordingActorRole, RecordingCandidateIdentity, RecordingDefinition,
    RecordingEvent, RecordingExecutionAuthority, RecordingLifecycle, RecordingTransitionKind,
    RecordingTransitionRequest, ScheduledRecording,
};

const START: i64 = 10_000_000;
const END: i64 = START + 3_600_000;

fn identity(suffix: &str) -> String {
    format!("00000000-0000-4000-8000-0000000000{suffix}")
}

fn candidate() -> RecordingCandidateIdentity {
    RecordingCandidateIdentity {
        version: "0.1.0".into(),
        source_commit: "a".repeat(40),
    }
}

fn execution() -> RecordingExecutionAuthority {
    RecordingExecutionAuthority {
        candidate: candidate(),
        channel_configuration_hash: "b".repeat(64),
    }
}

fn operator() -> RecordingActor {
    RecordingActor {
        actor_id: identity("04"),
        role: RecordingActorRole::Operator,
    }
}

fn worker() -> RecordingActor {
    RecordingActor {
        actor_id: identity("05"),
        role: RecordingActorRole::RecordingWorker,
    }
}

fn definition() -> RecordingDefinition {
    RecordingDefinition {
        recording_id: identity("01"),
        channel_id: identity("02"),
        schedule_item_id: identity("03"),
        schedule_revision: 7,
        scheduled_start_unix_ms: START,
        scheduled_end_unix_ms: END,
        requested_by: operator(),
        requested_at_unix_ms: START - 60_000,
    }
}

fn request(
    expected_revision: u64,
    actor: RecordingActor,
    occurred_at_unix_ms: i64,
    event: RecordingEvent,
) -> RecordingTransitionRequest {
    RecordingTransitionRequest {
        expected_revision,
        actor,
        occurred_at_unix_ms,
        execution: Some(execution()),
        event,
    }
}

fn draft_request(
    expected_revision: u64,
    actor: RecordingActor,
    occurred_at_unix_ms: i64,
    event: RecordingEvent,
) -> RecordingTransitionRequest {
    RecordingTransitionRequest {
        expected_revision,
        actor,
        occurred_at_unix_ms,
        execution: None,
        event,
    }
}

fn arm(recording: &ScheduledRecording) -> ScheduledRecording {
    recording
        .transition(request(
            recording.revision,
            operator(),
            START - 60_000,
            RecordingEvent::Arm,
        ))
        .unwrap()
        .recording
}

fn capture(recording: &ScheduledRecording) -> ScheduledRecording {
    recording
        .transition(request(
            recording.revision,
            worker(),
            START,
            RecordingEvent::StartCapture,
        ))
        .unwrap()
        .recording
}

fn finalizing(recording: &ScheduledRecording) -> ScheduledRecording {
    recording
        .transition(request(
            recording.revision,
            worker(),
            END,
            RecordingEvent::BeginFinalization,
        ))
        .unwrap()
        .recording
}

#[test]
fn recording_runs_through_audited_candidate_bound_lifecycle() {
    let draft = ScheduledRecording::new(definition()).unwrap();
    assert_eq!(draft.revision, 1);
    assert_eq!(draft.lifecycle, RecordingLifecycle::Draft);

    let armed = draft
        .transition(request(
            draft.revision,
            operator(),
            START - 60_000,
            RecordingEvent::Arm,
        ))
        .unwrap();
    assert_eq!(armed.receipt.kind, RecordingTransitionKind::Arm);
    assert_eq!(armed.receipt.actor, operator());
    assert_eq!(armed.receipt.execution, Some(execution()));

    let capturing = capture(&armed.recording);
    let finalizing = finalizing(&capturing);
    let completed = finalizing
        .transition(request(
            finalizing.revision,
            worker(),
            END + 10_000,
            RecordingEvent::FinalizeSuccess {
                captured_asset_hash: "c".repeat(64),
                captured_duration_ms: END - START,
            },
        ))
        .unwrap();

    assert_eq!(completed.recording.revision, 5);
    assert_eq!(
        completed.recording.lifecycle,
        RecordingLifecycle::Completed {
            capture_started_at_unix_ms: START,
            finalized_at_unix_ms: END + 10_000,
            captured_asset_hash: "c".repeat(64),
            captured_duration_ms: END - START,
            recovered_after_interruption: false,
            execution: execution(),
        }
    );
    assert_eq!(
        completed.receipt.kind,
        RecordingTransitionKind::FinalizeSuccess
    );
}

#[test]
fn scheduling_survives_pre_arm_changes_but_post_arm_execution_changes_fail_closed() {
    let draft = ScheduledRecording::new(definition()).unwrap();
    let stale = draft.transition(request(2, operator(), START - 60_000, RecordingEvent::Arm));
    assert!(stale.unwrap_err().contains("revision"));

    let mut updated_execution = execution();
    updated_execution.candidate.source_commit = "d".repeat(40);
    let mut arm_on_current_execution = request(
        draft.revision,
        operator(),
        START - 60_000,
        RecordingEvent::Arm,
    );
    arm_on_current_execution.execution = Some(updated_execution.clone());
    let armed = draft
        .transition(arm_on_current_execution)
        .unwrap()
        .recording;
    assert!(matches!(
        armed.lifecycle,
        RecordingLifecycle::Armed {
            execution: ref armed_execution,
            ..
        } if armed_execution == &updated_execution
    ));

    let mut wrong_candidate = request(
        armed.revision,
        worker(),
        START,
        RecordingEvent::StartCapture,
    );
    wrong_candidate
        .execution
        .as_mut()
        .unwrap()
        .candidate
        .source_commit = "e".repeat(40);
    assert!(
        armed
            .transition(wrong_candidate)
            .unwrap_err()
            .contains("armed authority")
    );

    let mut wrong_configuration = request(
        armed.revision,
        worker(),
        START,
        RecordingEvent::StartCapture,
    );
    wrong_configuration
        .execution
        .as_mut()
        .unwrap()
        .channel_configuration_hash = "e".repeat(64);
    assert!(
        armed
            .transition(wrong_configuration)
            .unwrap_err()
            .contains("armed authority")
    );
}

#[test]
fn lifecycle_and_actor_rules_fail_closed() {
    let draft = ScheduledRecording::new(definition()).unwrap();
    assert!(
        draft
            .transition(request(
                draft.revision,
                worker(),
                START,
                RecordingEvent::StartCapture,
            ))
            .unwrap_err()
            .contains("not allowed")
    );

    let armed = arm(&draft);
    assert!(
        armed
            .transition(request(
                armed.revision,
                operator(),
                START,
                RecordingEvent::StartCapture,
            ))
            .unwrap_err()
            .contains("recording worker")
    );

    let capturing = capture(&armed);
    assert!(
        capturing
            .transition(request(
                capturing.revision,
                operator(),
                END,
                RecordingEvent::BeginFinalization,
            ))
            .unwrap_err()
            .contains("recording worker")
    );
}

#[test]
fn recovery_is_a_bounded_finalization_path_not_a_resumed_capture() {
    let draft = ScheduledRecording::new(definition()).unwrap();
    let capturing = capture(&arm(&draft));
    assert!(
        capturing
            .transition(request(
                capturing.revision,
                worker(),
                END - 1,
                RecordingEvent::RecoverForFinalization {
                    reason: "worker restart after partial capture".into(),
                },
            ))
            .unwrap_err()
            .contains("scheduled end")
    );

    let recovered = capturing
        .transition(request(
            capturing.revision,
            worker(),
            END + 1,
            RecordingEvent::RecoverForFinalization {
                reason: "worker restart after partial capture".into(),
            },
        ))
        .unwrap();
    assert_eq!(
        recovered.recording.lifecycle,
        RecordingLifecycle::Finalizing {
            capture_started_at_unix_ms: START,
            finalization_started_at_unix_ms: END + 1,
            recovered_after_interruption: true,
            execution: execution(),
        }
    );
    assert_eq!(
        recovered.receipt.kind,
        RecordingTransitionKind::RecoverForFinalization
    );

    let completed = recovered
        .recording
        .transition(request(
            recovered.recording.revision,
            worker(),
            END + 2,
            RecordingEvent::FinalizeSuccess {
                captured_asset_hash: "c".repeat(64),
                captured_duration_ms: END - START,
            },
        ))
        .unwrap();
    assert!(matches!(
        completed.recording.lifecycle,
        RecordingLifecycle::Completed {
            recovered_after_interruption: true,
            ..
        }
    ));
}

#[test]
fn finalization_refuses_unbounded_or_implausible_capture_claims() {
    let draft = ScheduledRecording::new(definition()).unwrap();
    let finalizing = finalizing(&capture(&arm(&draft)));
    assert!(
        finalizing
            .transition(request(
                finalizing.revision,
                worker(),
                END + 1,
                RecordingEvent::FinalizeSuccess {
                    captured_asset_hash: "c".repeat(64),
                    captured_duration_ms: 0,
                },
            ))
            .unwrap_err()
            .contains("duration")
    );
    assert!(
        finalizing
            .transition(request(
                finalizing.revision,
                worker(),
                END + 1,
                RecordingEvent::FinalizeSuccess {
                    captured_asset_hash: "c".repeat(64),
                    captured_duration_ms: END - START + 300_001,
                },
            ))
            .unwrap_err()
            .contains("duration")
    );
    assert!(
        finalizing
            .transition(request(
                finalizing.revision,
                worker(),
                END + 1,
                RecordingEvent::FinalizeSuccess {
                    captured_asset_hash: "not-a-hash".into(),
                    captured_duration_ms: END - START,
                },
            ))
            .unwrap_err()
            .contains("asset hash")
    );
}

#[test]
fn definitions_reject_unbounded_or_invalid_schedule_authority() {
    let mut invalid = definition();
    invalid.scheduled_end_unix_ms = invalid.scheduled_start_unix_ms;
    assert!(
        ScheduledRecording::new(invalid)
            .unwrap_err()
            .contains("schedule window")
    );

    let mut invalid = definition();
    invalid.schedule_revision = 0;
    assert!(
        ScheduledRecording::new(invalid)
            .unwrap_err()
            .contains("schedule revision")
    );

    let mut invalid = definition();
    invalid.requested_by.role = RecordingActorRole::RecordingWorker;
    assert!(
        ScheduledRecording::new(invalid)
            .unwrap_err()
            .contains("operator")
    );

    let mut impossible_to_arm = definition();
    impossible_to_arm.requested_at_unix_ms = impossible_to_arm.scheduled_start_unix_ms;
    assert!(
        ScheduledRecording::new(impossible_to_arm)
            .unwrap_err()
            .contains("before scheduled start")
    );
}

#[test]
fn terminal_and_cancelled_recordings_cannot_be_reanimated() {
    let draft = ScheduledRecording::new(definition()).unwrap();
    let cancelled = draft
        .transition(draft_request(
            draft.revision,
            operator(),
            START - 1,
            RecordingEvent::Cancel {
                reason: "operator cancelled schedule item".into(),
            },
        ))
        .unwrap()
        .recording;
    assert!(matches!(
        cancelled.lifecycle,
        RecordingLifecycle::CancelledBeforeArm { .. }
    ));
    assert!(
        cancelled
            .transition(request(
                cancelled.revision,
                operator(),
                START,
                RecordingEvent::Arm,
            ))
            .unwrap_err()
            .contains("not allowed")
    );
}

#[test]
fn events_cannot_claim_to_have_happened_before_the_state_they_follow() {
    let draft = ScheduledRecording::new(definition()).unwrap();
    let armed = arm(&draft);
    assert!(
        armed
            .transition(request(
                armed.revision,
                operator(),
                START - 60_001,
                RecordingEvent::Cancel {
                    reason: "backdated cancellation".into(),
                },
            ))
            .unwrap_err()
            .contains("cancellation")
    );

    let capturing = capture(&armed);
    assert!(
        capturing
            .transition(request(
                capturing.revision,
                worker(),
                START - 1,
                RecordingEvent::Fail {
                    reason: "backdated worker failure".into(),
                },
            ))
            .unwrap_err()
            .contains("failure time")
    );
}

#[test]
fn corrupt_lifecycle_execution_authority_fails_validation() {
    let corrupt = ScheduledRecording {
        definition: definition(),
        lifecycle: RecordingLifecycle::Armed {
            armed_at_unix_ms: START - 60_000,
            execution: RecordingExecutionAuthority {
                candidate: candidate(),
                channel_configuration_hash: "not-a-hash".into(),
            },
        },
        revision: 1,
    };
    assert!(
        corrupt
            .validate()
            .unwrap_err()
            .contains("execution configuration hash")
    );
}
