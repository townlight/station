# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""``AgendaSource`` Protocol + the shared error taxonomy (plan §5, §10).

Every adapter (``legistar.py``, ``primegov.py``, ``civicclerk.py``,
``js_portal.py``) implements this Protocol so the router and mapper never
branch on vendor. Errors are vendor-agnostic too, so the router's exception
handling (plan §6 API surface) does not grow a new ``except`` clause per
vendor -- with one deliberate exception, noted below:

* :class:`AgendaSourceAuthRequiredError` — 401/403 from a token-gated tenant.
  Distinct from a generic upstream failure so the operator/LPM gets an
  actionable message ("get a token from city IT"), never a bare 502
  (plan §3 "New risk to design around").
* :class:`AgendaSourceUpstreamError` — 5xx, timeout, or a malformed/empty
  response. "Fail loud, never silently import nothing" (plan Part 1) — an
  empty items list from a real event is treated as malformed, not a valid
  zero-item agenda. PrimeGov also raises this (rather than fabricating
  items) when a meeting has no HTML compiled agenda and no verified
  anonymous PDF fetch path (see ``primegov.py``'s docstring). ``js_portal``
  also raises this for a robots.txt disallow and for an off-origin link
  filtered out of a crawl (plan-equivalent: Agenda Bridge Phase 4 design).
* :class:`AgendaSourceNotAvailableError` — the requested source name is not
  implemented in this release. All four vendors (Legistar, PrimeGov,
  CivicClerk, js_portal) resolve to a working adapter as of Phase 4; this
  error now only fires for a genuinely unknown source name, but stays part
  of the taxonomy so the router's exception handling doesn't need to change
  if a future vendor is wired the same way these were (plan §8 task 5).
* :class:`AgendaSourceDependencyMissingError` — the adapter is implemented
  but its optional runtime dependency isn't installed on this machine
  (``js_portal`` needs the ``civiccast[agenda-js-import]`` extra: crawl4ai +
  Playwright Chromium). This IS a genuinely new failure mode the other three
  vendors never had (they only depend on ``httpx``, already a base
  dependency) -- the router gains exactly one new ``except`` clause for it
  (mapped to 503, not 502: "not installed here" is a station-configuration
  fact, not an upstream vendor failure) rather than silently folding it into
  :class:`AgendaSourceUpstreamError` and losing the distinction the UI needs
  to render a "not installed" state instead of a generic error banner.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from civiccast.agenda_import.models import ExternalAgenda, ExternalMeetingSummary


class AgendaSourceError(RuntimeError):
    """Base error for every :class:`AgendaSource` adapter failure."""


class AgendaSourceAuthRequiredError(AgendaSourceError):
    """A token-gated tenant rejected the request with 401/403."""


class AgendaSourceUpstreamError(AgendaSourceError):
    """5xx, timeout, or a malformed/empty upstream response."""


class AgendaSourceNotAvailableError(AgendaSourceError):
    """The requested source name has no adapter in this release."""


class AgendaSourceDependencyMissingError(AgendaSourceError):
    """The adapter exists but its optional runtime dependency is absent.

    Raised by :class:`~civiccast.agenda_import.js_portal.JsPortalSource` when
    ``crawl4ai``/Playwright are not importable. Kept distinct from
    :class:`AgendaSourceUpstreamError` (see module docstring) so the API
    layer can report a "not installed" posture instead of a generic upstream
    failure.
    """


class AgendaSource(Protocol):
    """One agenda-system adapter: list meetings, fetch one meeting's agenda."""

    def fetch_meetings(
        self, client_code: str, *, since: date | None = None
    ) -> list[ExternalMeetingSummary]:
        """Meetings for ``client_code`` on/after ``since`` (default: today)."""
        ...

    def fetch_agenda(self, client_code: str, event_id: str) -> ExternalAgenda:
        """The full normalized agenda for one meeting."""
        ...


__all__ = [
    "AgendaSource",
    "AgendaSourceAuthRequiredError",
    "AgendaSourceError",
    "AgendaSourceNotAvailableError",
    "AgendaSourceUpstreamError",
]
