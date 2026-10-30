# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""The pure D2 per-verb delivery/replay policy for the worker control seam.

On Windows the worker control channel is a named pipe carrying acknowledged
delivery (spec D2): the strategy sends ``{"v":1,"id":<uuid>,"cmd":"<line>"}`` and
the worker replies ``{"v":1,"id":<same>,"result":"applied"|"error",...}`` after
the engine applies the command. The four ``parse_control_line`` verbs have
different safety shapes, and this module is the pure policy that decides — with
no pipe, no thread, no engine — what the STRATEGY does on each delivery outcome
and what the WORKER does about redelivery:

* ``reload`` / ``swap`` — at-least-once, idempotent. A lost ack or a
  reconnect reissues the channel's CURRENT DESIRED STATE (the current graph for
  reload, the current role for swap), never a replay of command history.
* ``caption`` — at-most-once, never replayed. Cues are time-bound; a stale
  replayed cue is worse than a missed one, so a lost ack or a restart is
  reported to the daemon as dropped, with no redelivery.
* ``stop`` — terminal, exactly-once-EFFECTIVE. An unacknowledged stop pins the
  channel ``stopping`` and SUPPRESSES both restart and desired-state replay;
  the ground truth for resolution is the observed process exit (the handle),
  not the ack. A stopping channel never resurrects.

Worker-side idempotent redelivery is an LRU of applied ids: a redelivered id
that has already been applied is acknowledged again but not re-enacted.

The engine/pipe I/O lives in the worker and strategy modules; this is the pure
decision they call, so the four falsifications D2 owes (lost ack, duplicate
delivery, worker restart between write and apply, reconnect under load) are
CI-testable against a fake transport.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Verb = Literal["reload", "swap", "caption", "stop", "emergency"]
"""The four control verbs (``parse_control_line`` grammar, unchanged)."""

LostAckOutcome = Literal["reissue_desired_state", "report_dropped", "keep_stopping"]
"""What the strategy does when a command's ack is lost or times out."""


class DeliverySemantics(BaseModel):
    """The pure per-verb contract. ``replayed`` is whether the strategy re-sends
    the intent after a loss/reconnect; ``at_most_once`` is whether a lost cue is
    dropped rather than retried; ``on_lost_ack`` is the action taken."""

    model_config = ConfigDict(extra="forbid")

    verb: Verb
    replayed: bool
    at_most_once: bool
    on_lost_ack: LostAckOutcome


_SEMANTICS: dict[str, DeliverySemantics] = {
    "emergency": DeliverySemantics(
        verb="emergency", replayed=True, at_most_once=False, on_lost_ack="reissue_desired_state"
    ),
    "reload": DeliverySemantics(
        verb="reload", replayed=True, at_most_once=False, on_lost_ack="reissue_desired_state"
    ),
    "swap": DeliverySemantics(
        verb="swap", replayed=True, at_most_once=False, on_lost_ack="reissue_desired_state"
    ),
    "caption": DeliverySemantics(
        verb="caption", replayed=False, at_most_once=True, on_lost_ack="report_dropped"
    ),
    "stop": DeliverySemantics(
        verb="stop", replayed=False, at_most_once=False, on_lost_ack="keep_stopping"
    ),
}


def delivery_semantics(verb: Verb) -> DeliverySemantics:
    """The pure per-verb delivery contract."""

    return _SEMANTICS[verb]


class Command(BaseModel):
    """One control command on the versioned envelope."""

    model_config = ConfigDict(extra="forbid")

    id: str
    verb: Verb
    line: str


class ChannelReplay(BaseModel):
    """The strategy-side per-channel replay state. Pure: mutated only through
    the methods below, each of which is a total function of the current state
    plus the delivery event."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str
    # Desired state is the CURRENT intent, overwritten in place — reissue sends
    # this, never a history of commands. None until first set.
    desired_reload_line: str | None = None
    desired_swap_line: str | None = None
    desired_emergency_line: str | None = None
    # Terminal: once an unacknowledged (or acknowledged) stop pins the channel,
    # no reissue and no restart may resurrect it.
    stopping: bool = False
    dropped_captions: list[str] = Field(default_factory=list)

    def record_sent(self, command: Command) -> None:
        """Register a command's intent as the current desired state (reload/swap)
        or note a stop. Caption carries no desired state (at-most-once)."""

        if command.verb == "reload":
            self.desired_reload_line = command.line
        elif command.verb == "swap":
            self.desired_swap_line = command.line
        elif command.verb == "emergency":
            self.desired_emergency_line = command.line
        elif command.verb == "stop":
            self.stopping = True

    def on_lost_ack(self, command: Command) -> LostAckOutcome:
        """Decide (and record) the outcome of a lost/timed-out ack for
        ``command``. A stopping channel suppresses every reissue."""

        semantics = delivery_semantics(command.verb)
        if self.stopping:
            # stop dominates: no desired-state replay once stopping is pinned.
            if command.verb == "caption":
                self.dropped_captions.append(command.id)
                return "report_dropped"
            return "keep_stopping"
        if semantics.on_lost_ack == "report_dropped":
            self.dropped_captions.append(command.id)
        return semantics.on_lost_ack

    def reissue_on_reconnect(self) -> list[Command]:
        """The commands to re-send on reconnect: the CURRENT desired state for
        reload/swap, in that dependency order, and NOTHING once stopping (a
        stopping channel is not resurrected). Caption is never reissued."""

        if self.stopping:
            return []
        out: list[Command] = []
        if self.desired_reload_line is not None:
            out.append(
                Command(
                    id=f"reissue-reload-{self.channel_id}",
                    verb="reload",
                    line=self.desired_reload_line,
                )
            )
        if self.desired_swap_line is not None:
            out.append(
                Command(
                    id=f"reissue-swap-{self.channel_id}", verb="swap", line=self.desired_swap_line
                )
            )
        if self.desired_emergency_line is not None:
            out.append(
                Command(
                    id=f"reissue-emergency-{self.channel_id}",
                    verb="emergency",
                    line=self.desired_emergency_line,
                )
            )
        return out


class AppliedIdCache:
    """The worker-side LRU of applied ids for idempotent redelivery. A
    redelivered id already in the cache is acknowledged again (the ack was
    lost, not the application) but NOT re-enacted."""

    def __init__(self, capacity: int = 1024) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._capacity = capacity
        self._ids: OrderedDict[str, None] = OrderedDict()

    def should_apply(self, command_id: str) -> bool:
        """True iff this id has not been applied before. Recording happens in
        :meth:`mark_applied` after the engine actually enacts it, so a command
        that errors on application is not falsely remembered as applied."""

        return command_id not in self._ids

    def mark_applied(self, command_id: str) -> None:
        if command_id in self._ids:
            self._ids.move_to_end(command_id)
            return
        self._ids[command_id] = None
        while len(self._ids) > self._capacity:
            self._ids.popitem(last=False)

    def __contains__(self, command_id: str) -> bool:
        return command_id in self._ids

    def __len__(self) -> int:
        return len(self._ids)


__all__ = [
    "AppliedIdCache",
    "ChannelReplay",
    "Command",
    "DeliverySemantics",
    "LostAckOutcome",
    "Verb",
    "delivery_semantics",
]
