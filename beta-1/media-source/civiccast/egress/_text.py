# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Encoding safety for free text persisted by the egress daemon/automation.

``civiccast/native/provision/seams.py``'s ``initdb`` runs with no
``-E``/``--encoding``/``--locale`` on any cluster provisioned before this
module existed, so an existing cluster's ``server_encoding`` is whatever the
OS codepage was at initdb time -- WIN1252 on the Windows sandbox/CI fleet
(``initdb_argv`` now pins ``UTF8`` for NEW clusters only; see that module's
docstring). No caller sets ``client_encoding`` either
(:mod:`civiccast.db.session`, :mod:`civiccast.db.url`), so a non-cp1252
character written into a persisted egress free-text column (an
operator-entered source title, a folded child-stderr tail, a Python
exception message, ...) raises ``UnicodeEncodeError`` in psycopg and aborts
the WHOLE automation pass for that channel -- see
``civiccast/egress/daemon.py``'s ``_ascii_safe`` docstring for the T6 soak
evidence that first surfaced this for the child-stderr-tail path alone.

:func:`db_safe_text` is the ONE helper every persisted free-text path in the
egress daemon/automation should go through before the value reaches
``EgressStore.write_state`` / ``append_proof_event``.

Design choice (documented per the reviewer's request to say which fallback
was picked): the daemon only holds the narrow ``EgressStore`` Protocol
(``civiccast/egress/store.py``) -- deliberately, so it stays constructible
against a fake store in tests without any SQLAlchemy session/engine in
sight. Threading a live connection (to run ``SHOW server_encoding`` once and
cache it) through that Protocol into every daemon call site would widen a
seam that exists specifically to keep the daemon storage-agnostic, for a
value that never changes for the life of one connection anyway. Instead this
folds unconditionally to the cp1252-encodable subset: an
``encode("cp1252", errors="replace")`` round trip. This keeps Latin-1-range
accents (e, n, u with diacritics -- the actual common case: an
operator-entered title like "Cafe Reunion") intact, because cp1252 can
represent them, and only degrades characters cp1252 truly cannot (CJK,
emoji, the U+FFFD replacement character itself) -- strictly better than the
plain-ASCII fold ``_ascii_safe`` used to do alone, and safe even on a UTF8
cluster (cp1252's representable subset is a subset of Unicode, so re-encoding
it as UTF8 on write is lossless for exactly the characters this function
does not degrade).
"""

from __future__ import annotations

_DB_SAFE_FALLBACK_ENCODING = "cp1252"


def db_safe_text(text: str) -> str:
    """Fold ``text`` so it can be persisted regardless of the database's
    server encoding. See :func:`db_safe_text_or_none` for the ``str | None``
    convenience wrapper most call sites in this package actually want.
    """

    return text.encode(_DB_SAFE_FALLBACK_ENCODING, errors="replace").decode(
        _DB_SAFE_FALLBACK_ENCODING
    )


def db_safe_text_or_none(text: str | None) -> str | None:
    """:func:`db_safe_text`, but passthrough for ``None`` -- the shape every
    call site in ``civiccast/egress/daemon.py`` actually needs (
    ``current_source_label``, ``last_error``, proof-event ``label`` /
    ``machine_summary`` are all ``str | None``)."""

    if text is None:
        return None
    return db_safe_text(text)


__all__ = ["db_safe_text", "db_safe_text_or_none"]
