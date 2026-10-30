# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Resolve a source name to an :class:`AgendaSource` adapter (plan §8 task 5).

All four vendors now resolve to a working adapter (Phase 4 adds
``js_portal``). ``primegov`` and ``civicclerk`` both ignore ``token`` --
neither vendor's public tenant API has a verified token/auth convention
(plan §3), unlike Legistar's. ``js_portal`` ignores ``token`` too (no auth
flows, per its own module docstring) but requires ``portal_url`` -- the
router validates it via :func:`civiccast.agenda_import.config.
validate_portal_url` before it ever reaches here.
"""

from __future__ import annotations

from civiccast.agenda_import.base import AgendaSource, AgendaSourceNotAvailableError
from civiccast.agenda_import.civicclerk import CivicClerkSource
from civiccast.agenda_import.js_portal import JsPortalSource
from civiccast.agenda_import.legistar import LegistarSource
from civiccast.agenda_import.primegov import PrimeGovSource


def build_source(
    name: str,
    *,
    timeout_seconds: float,
    token: str | None,
    portal_url: str | None = None,
    portal_vendor_hint: str | None = None,
) -> AgendaSource:
    if name == "legistar":
        return LegistarSource(timeout_seconds=timeout_seconds, token=token)
    if name == "primegov":
        return PrimeGovSource(timeout_seconds=timeout_seconds)
    if name == "civicclerk":
        return CivicClerkSource(timeout_seconds=timeout_seconds)
    if name == "js_portal":
        if not portal_url:
            # Defensive: the router's model_validator already requires
            # portal_url whenever source == "js_portal" (422 before this
            # function is ever called), so this only fires for a caller that
            # bypasses the router (e.g. a future internal caller, or a test
            # exercising build_source directly) -- fail loud rather than
            # constructing an adapter that can never do anything useful.
            raise AgendaSourceNotAvailableError(
                "Agenda source 'js_portal' requires portal_url (the JS-hydrated "
                "portal's own URL) -- none was provided."
            )
        return JsPortalSource(
            portal_url=portal_url,
            vendor_hint=portal_vendor_hint or "generic",
            timeout_seconds=timeout_seconds,
        )
    raise AgendaSourceNotAvailableError(
        f"Agenda source {name!r} is not implemented in this CivicCast release; "
        "'legistar', 'primegov', 'civicclerk', and 'js_portal' are available."
    )


__all__ = ["build_source"]
