# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""In-memory playback policy store and evaluator."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from threading import Lock

from civiccast.installer.storage import default_storage_dir
from civiccast.playback_policy.models import (
    PlaybackPolicyAuditEvent,
    PlaybackPolicyAuditLog,
    PlaybackPolicyConfig,
    PlaybackPolicyEvaluation,
    PlaybackPolicyEvaluationRequest,
    PlaybackPolicyUpdate,
    PlaybackSubjectType,
    utc_now,
)

_STATE_FILE_NAME = "playback-policy-state.json"


class PlaybackPolicyStore:
    """Thread-safe policy store for app-owned playback decisions."""

    def __init__(self, state_path: Path | None = None) -> None:
        self._lock = Lock()
        self._state_path = state_path
        self._policies: dict[tuple[PlaybackSubjectType, str], PlaybackPolicyConfig] = {}
        self._audit_events: list[PlaybackPolicyAuditEvent] = []
        self._load_state()

    def upsert_policy(
        self,
        subject_type: PlaybackSubjectType,
        subject_id: str,
        update: PlaybackPolicyUpdate,
    ) -> PlaybackPolicyConfig:
        with self._lock:
            policy = PlaybackPolicyConfig(
                subject_type=subject_type,
                subject_id=subject_id,
                updated_at=utc_now(),
                **update.model_dump(),
            )
            self._policies[(subject_type, subject_id)] = policy
            self._persist_locked()
            return policy.model_copy(deep=True)

    def get_policy(
        self,
        subject_type: PlaybackSubjectType,
        subject_id: str,
    ) -> PlaybackPolicyConfig:
        with self._lock:
            policy = self._policies.get((subject_type, subject_id))
            if policy is None:
                policy = PlaybackPolicyConfig(
                    subject_type=subject_type,
                    subject_id=subject_id,
                    updated_at=utc_now(),
                )
            return policy.model_copy(deep=True)

    def effective_policy(self, asset_id: str, channel_id: str) -> PlaybackPolicyConfig:
        with self._lock:
            return self._effective_policy_locked(asset_id, channel_id).model_copy(deep=True)

    def evaluate(self, request: PlaybackPolicyEvaluationRequest) -> PlaybackPolicyEvaluation:
        with self._lock:
            policy = self._effective_policy_locked(request.asset_id, request.channel_id)
            allowed, reason = _decision_for(policy, request)
            event = PlaybackPolicyAuditEvent(
                event_id=f"playback-{len(self._audit_events) + 1}",
                asset_id=request.asset_id,
                channel_id=request.channel_id,
                viewer_account_id=request.viewer.account_id if request.viewer else None,
                decision="allowed" if allowed else "blocked",
                reason=reason,
                access_tier=policy.access_tier,
                preroll_creative_ids=[
                    creative.creative_id for creative in policy.preroll.creatives
                ],
                occurred_at=utc_now(),
            )
            self._audit_events.append(event)
            self._persist_locked()
            return PlaybackPolicyEvaluation(
                allowed=allowed,
                reason=reason,
                policy=policy.model_copy(deep=True),
                audit_event=event.model_copy(deep=True),
                proof_boundary="playback-policy-to-audited-decision",
            )

    def audit_log(self) -> PlaybackPolicyAuditLog:
        with self._lock:
            return PlaybackPolicyAuditLog(
                generated_at=utc_now(),
                events=[event.model_copy(deep=True) for event in self._audit_events],
                proof_boundary="playback-decision-to-staff-audit-log",
            )

    def _effective_policy_locked(self, asset_id: str, channel_id: str) -> PlaybackPolicyConfig:
        asset_policy = self._policies.get(("asset", asset_id))
        if asset_policy is not None:
            return asset_policy
        channel_policy = self._policies.get(("channel", channel_id))
        if channel_policy is not None:
            return channel_policy
        return PlaybackPolicyConfig(
            subject_type="asset",
            subject_id=asset_id,
            updated_at=utc_now(),
        )

    def _load_state(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        policies = payload.get("policies", [])
        audit_events = payload.get("audit_events", [])
        self._policies = {
            (policy.subject_type, policy.subject_id): policy
            for policy in (PlaybackPolicyConfig.model_validate(item) for item in policies)
        }
        self._audit_events = [
            PlaybackPolicyAuditEvent.model_validate(item) for item in audit_events
        ]

    def _persist_locked(self) -> None:
        if self._state_path is None:
            return
        payload = {
            "policies": [
                policy.model_dump(mode="json")
                for policy in sorted(
                    self._policies.values(),
                    key=lambda item: (item.subject_type, item.subject_id),
                )
            ],
            "audit_events": [event.model_dump(mode="json") for event in self._audit_events[-500:]],
        }
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._state_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if os.name != "nt":
            tmp_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        tmp_path.replace(self._state_path)
        if os.name != "nt":
            self._state_path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def default_playback_policy_state_path() -> Path | None:
    configured = os.environ.get("CIVICCAST_PLAYBACK_POLICY_STATE_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.environ.get("CIVICCAST_ALLOW_EPHEMERAL_STORES") == "1":
        return None
    return (default_storage_dir() / _STATE_FILE_NAME).expanduser().resolve()


def _decision_for(
    policy: PlaybackPolicyConfig,
    request: PlaybackPolicyEvaluationRequest,
) -> tuple[bool, str]:
    if policy.access_tier == "public":
        return True, "Playback is public."
    if request.viewer is None:
        return False, "Sign in with a viewer account to play this content."
    if policy.access_tier == "authenticated":
        return True, "Viewer account is authenticated for playback."
    required_group = policy.invite_group_id
    if required_group and required_group in request.viewer.invite_groups:
        return True, "Viewer account has the required invite."
    return False, "This content requires a matching invite."
