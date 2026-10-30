# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""The in-product operator manual ("docsite").

Serves ``docs/USER-MANUAL.md`` inside the operator console so an operator can
click a link on a setup guide or provider card and land on the matching
manual section -- no internet connection required.

The manual is never parsed at runtime. ``scripts/render_docsite_manual.py``
renders ``docs/USER-MANUAL.md`` into ``civiccast/docsite/manual.json`` (a
single flat HTML fragment plus a table of contents) at commit time, using the
same pandoc toolchain and hash-pinning pattern as
``scripts/render_user_manual.py`` renders the PDF/DOCX. ``service.py`` only
ever reads the pre-rendered, already-sanitized JSON file that ships inside
the ``civiccast`` package -- see ``docs/docsite-sync.md`` for the full
staleness-proof contract.
"""
