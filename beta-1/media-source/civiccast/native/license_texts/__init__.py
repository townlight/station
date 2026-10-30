# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Canonical, version-controlled license text bundled with the native
Windows runtime closure (`spec-packaging-closure` D3, Codex audit finding
CC-WS5-PKG-004).

D3 requires the required upstream notices to be BUNDLED with the runtime --
LGPL-2.1-or-later and MPL-2.0 in particular REQUIRE the license text itself
to accompany the binaries, and a bill-of-materials table naming a license is
not a substitute for that text. Before this module existed,
`scripts/build_native_runtime_closure.py` wrote only a per-distribution
SUMMARY (`<out>/licenses/<distribution>.txt`, the wheel's own declared SPDX
expression) -- never the license text itself.

This directory holds the fix: one `<spdx-id>.txt` file per SPDX identifier
that `civiccast.native.runtime_licenses` actually resolves a shipped file
to, fetched from spdx.org's own published license-list-data and committed
to the repo AS DATA, so a build never needs the network to be legally
complete (a build that needs the network to bundle a legally required
notice is a build that fails at the worst moment -- see the module doc on
`scripts/build_native_runtime_closure.py`). Each text file is stored
byte-for-byte as published, with no CivicCast commentary inside it; the
provenance for each is recorded in `LICENSE_TEXT_SOURCES` below instead of
as an in-band header, so the bundled text stays a clean, verbatim copy.

The one exception is `LicenseRef-Microsoft-VCRedist.txt`, which is NOT a
reproduction of any license text -- see its own contents and the entry in
`LICENSE_TEXT_SOURCES` for why: the Microsoft VC++ Redistributable EULA is
proprietary and CivicCast has no license to reproduce it, so that file is a
pointer to where an operator can find the real terms, plainly labelled as
such.

`scripts/build_native_runtime_closure.py` decides WHICH of these texts get
copied into a given build by walking the built tree's own manifest/BOM
(`available_license_texts()` below is a directory listing, not a hardcoded
"ship these N licenses" list) -- so a future license appearing in the
shipped BOM with no corresponding file here is a build-time gap this module
can be asked about, not a silent omission.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = [
    "LICENSE_TEXTS_DIR",
    "LICENSE_TEXT_SOURCES",
    "SPDX_LICENSE_LIST_COMMIT",
    "LicenseTextSource",
    "LicenseTextTamperError",
    "available_license_texts",
    "license_text_sha256",
    "verify_bundled_license_texts",
]

#: This package's own directory -- where every `<spdx-id>.txt` file (plus
#: this module) lives.
LICENSE_TEXTS_DIR: Final[Path] = Path(__file__).resolve().parent


@dataclass(frozen=True)
class LicenseTextSource:
    """Where one `<spdx-id>.txt` file's content came from, and when."""

    source: str
    fetched: str
    #: SHA-256 of the bundled file's bytes with newlines normalised to LF.
    #: Normalised because git may check LF content out as CRLF on Windows --
    #: that is a checkout artifact, not a content difference, and hashing raw
    #: bytes would make this check fail on one platform and pass on another.
    #: Empty ONLY for a file with no upstream to pin against.
    sha256: str = ""
    note: str = ""


#: The IMMUTABLE spdx/license-list-data commit every bundled SPDX text was
#: taken from. Round 1 asked for version-pinned sources; round 2 correctly
#: rejected the first attempt because `.../main/text/...` is a MOVING ref --
#: it names a branch, not a version, so it describes where a file came from
#: today and says nothing about what it will serve tomorrow. A provenance
#: record that cannot fail is not a provenance record. This commit plus the
#: per-entry `sha256` below is the actual pin: `verify_bundled_license_texts`
#: turns it into a check that a build can fail on.
SPDX_LICENSE_LIST_COMMIT: Final[str] = "5bf6d9610255540bfbee6890765a616042bf1e11"

#: URL template for a text at the pinned commit. Every SPDX-sourced entry's
#: `source` is built from this, so the pin cannot drift entry-by-entry.
_SPDX_TEXT_URL: Final[str] = (
    "https://raw.githubusercontent.com/spdx/license-list-data/"
    f"{SPDX_LICENSE_LIST_COMMIT}/text/{{spdx_id}}.txt"
)


def _spdx_source(spdx_id: str) -> str:
    """The immutable upstream URL for one SPDX identifier's canonical text."""
    return _SPDX_TEXT_URL.format(spdx_id=spdx_id)


#: Provenance for every bundled text file, keyed by SPDX identifier (matches
#: the `<spdx-id>.txt` filename this module's directory holds for that key).
#: Kept out-of-band from the text files themselves (see module docstring)
#: so the committed `.txt` files stay byte-for-byte verbatim copies with no
#: CivicCast prose mixed into legally operative text.
#:
#: Every `sha256` was confirmed against the pinned commit by fetching each
#: text and comparing bytes -- not copied from an audit report.
LICENSE_TEXT_SOURCES: Final[dict[str, LicenseTextSource]] = {
    "LGPL-2.0-or-later": LicenseTextSource(
        source=_spdx_source("LGPL-2.0-or-later"),
        fetched="2026-10-14",
        sha256="86dc99d7e5060915ab1dfc1378b7dd351c62088bfa74067e8aa1868c6fdba7d8",
        note=(
            "Governs the GStreamer code compiled into gstfft-1.0-0.dll; "
            "see the pinned GStreamer 1.28.5 gst-plugins-base FFT sources."
        ),
    ),
    "LGPL-2.1-or-later": LicenseTextSource(
        source=_spdx_source("LGPL-2.1-or-later"),
        fetched="2026-07-23",
        sha256="5749785c8bdefafcb5d798270ed0a967036fe2ca63dcedade1627565dfef81d2",
    ),
    "MIT": LicenseTextSource(
        source=_spdx_source("MIT"),
        fetched="2026-07-23",
        sha256="b05785f9f18e6716bab63424b11454513b9943a222595b70411009202fc592b5",
    ),
    "Apache-2.0": LicenseTextSource(
        source=_spdx_source("Apache-2.0"),
        fetched="2026-07-23",
        sha256="074e6e32c86a4c0ef8b3ed25b721ca23aca83df277cd88106ef7177c354615ff",
    ),
    "BSD-2-Clause": LicenseTextSource(
        source=_spdx_source("BSD-2-Clause"),
        fetched="2026-07-23",
        sha256="f32fb3b417a194167cfad068223fc975ba96c5960513a10f66a3c28720aec1df",
    ),
    "BSD-3-Clause": LicenseTextSource(
        source=_spdx_source("BSD-3-Clause"),
        fetched="2026-07-23",
        sha256="5a93d5831e1297ab10fe643e1a631e83be392896da14ee2951285a79012df69d",
    ),
    "MPL-2.0": LicenseTextSource(
        source=_spdx_source("MPL-2.0"),
        fetched="2026-07-23",
        sha256="66a3107d5ad6a058aab753eaac2047ccb2ed0e39465dd0fe5844da3e300d5172",
    ),
    "FTL": LicenseTextSource(
        source=_spdx_source("FTL"),
        fetched="2026-07-23",
        sha256="ced6622122ce451cb1ea0c3c3f507a640e2a44c075c04900ddd9fae8acb5369f",
        note="The FreeType Project License. Governs freetype-6.dll.",
    ),
    "HPND-sell-variant": LicenseTextSource(
        source=_spdx_source("HPND-sell-variant"),
        fetched="2026-07-23",
        sha256="235abc578371bf9861e3d6eee0a9ad16228f9be18408e17fc569c6a98ba69d5f",
        note=(
            "SPDX's canonical text for the HPND-sell-variant family "
            "(the 'and sell' / no-advertising Historic Permission Notice "
            "and Disclaimer variant). This is fontconfig's PRIMARY grant but "
            "NOT the whole of fontconfig's terms -- fontconfig 2.16.1 is a "
            "cumulative multi-notice component, so its full COPYING is "
            "bundled separately as LicenseRef-Fontconfig-2.16.1. See "
            "civiccast.native.runtime_licenses."
        ),
    ),
    # AFL-2.1 was elected ONLY by DBus-1.0.typelib / DBusGLib-1.0.typelib, both
    # PRUNED from the shipped closure (owner-approved 2026-07-24 -- see
    # OWNER-DECISION-licensing-dispositions.md). No shipped file resolves to
    # AFL-2.1 any longer, so its bundled text was removed with the typelibs and
    # its provenance entry with it -- verify_bundled_license_texts() enforces
    # exact set equality in both directions, so the ledger entry and the on-disk
    # AFL-2.1.txt had to go together.
    "Libpng": LicenseTextSource(
        source=_spdx_source("Libpng"),
        fetched="2026-07-23",
        sha256="7667a8c88c7a63690244988d626bcddd27ed895526e2c3ab1a9adb463a5fa287",
        note="Governs png16.dll.",
    ),
    "Zlib": LicenseTextSource(
        source=_spdx_source("Zlib"),
        fetched="2026-07-23",
        sha256="bfb1112d49db5b1daecdfef24bd7e2f3ea0bafb33aa67aa0ab51e2bf8407c03d",
        note="Governs z-1.dll.",
    ),
    "bzip2-1.0.6": LicenseTextSource(
        source=_spdx_source("bzip2-1.0.6"),
        fetched="2026-07-23",
        sha256="0a56dbabe7d2ff65dea26e8b795fe42c6204a192b974d8e9304fe356ccda9fd1",
        note="Julian Seward's bzip2/libbzip2 license. Governs bz2.dll.",
    ),
    "blessing": LicenseTextSource(
        source=_spdx_source("blessing"),
        fetched="2026-07-23",
        sha256="592db1199aab67aafe4515db9808b420db122083807c5329b2f061951e79acbe",
        note="SQLite's public-domain dedication ('the blessing'). Governs sqlite3-0.dll.",
    ),
    "MIT-Modern-Variant": LicenseTextSource(
        source=_spdx_source("MIT-Modern-Variant"),
        fetched="2026-07-23",
        sha256="d7366190045ad81a5e612799b8f4eef242f9b389a0040836aad61bebe9849647",
        note=(
            "The HarfBuzz-derived 'without written agreement and without "
            "license or royalty fees' grant. Confirmed by comparison -- not "
            "assumed -- to be the exact text carried by fontconfig 2.16.1's "
            "src/fcatomic.h and src/fcmutex.h, both compiled into "
            "fontconfig-1.dll."
        ),
    ),
    "Unicode-TOU": LicenseTextSource(
        source=_spdx_source("Unicode-TOU"),
        fetched="2026-07-23",
        sha256="cb6ba87a4979d8152c726eac13b4480d5bbddd206e0ade0cb91b769e5ca30025",
        note=(
            "Unicode Terms of Use. fontconfig's COPYING points at "
            "unicode.org/terms_of_use.html for fc-case/CaseFolding.txt, whose "
            "derived case-folding table is compiled into fontconfig-1.dll. "
            "The COPYING cites the URL; this bundles the terms themselves so "
            "the shipped tree is complete without the network."
        ),
    ),
    "LicenseRef-Fontconfig-2.16.1": LicenseTextSource(
        source=(
            "https://gitlab.freedesktop.org/fontconfig/fontconfig/-/raw/"
            "fdfc3445d1cc9c1c7e587fb2a1287871de16faf9/COPYING"
        ),
        fetched="2026-07-23",
        sha256="51a51aa9823704fd90bccc616cdd17ebabb5b2b3e9cbde886ca02c7002288067",
        note=(
            "fontconfig 2.16.1's OWN COPYING, verbatim, from the exact "
            "upstream release commit -- the operative notice for "
            "fontconfig-1.dll and fontconfig-2.0.typelib. Bundled because "
            "fontconfig is a CUMULATIVE multi-notice component, not a single "
            "licence: COPYING carries the main HPND-sell-variant grant PLUS "
            "separate Unicode, HarfBuzz-derived, MIT, and public-domain "
            "notices for compiled sources and data. Mapping the DLL to "
            "HPND-sell-variant alone (as CivicCast previously did) named the "
            "primary grant and dropped the rest. The public-domain components "
            "(src/fcmd5.h, src/ftglue.[ch]) get no separate SPDX identifier "
            "in the expression because a public-domain dedication imposes no "
            "conditions to satisfy -- their dedications are reproduced "
            "verbatim inside this file, which is what discharges the notice."
        ),
    ),
    "LicenseRef-Microsoft-VCRedist": LicenseTextSource(
        source="(none -- proprietary; see the file itself)",
        fetched="2026-07-23",
        # PINNED TOO, despite having no upstream. Round 4 (CC-WS5-PKG-012)
        # was right that leaving one entry unpinned is a hole, not a nuance:
        # this file is a legal-posture STATEMENT that ships to operators, so it
        # should not be silently editable either. The hash is of CivicCast's own
        # authored text; changing it is a deliberate act that must show up here.
        sha256="84e6678fc347644a24c605d172355f7529c07243a9a10152d939b1ffafcf535d",
        note=(
            "NOT a reproduced license text. Microsoft's VC++ Redistributable "
            "EULA is proprietary; CivicCast has no license to reproduce it. "
            "This entry's .txt file is a pointer to Microsoft's own published "
            "terms, plainly labelled as not being the text itself -- see "
            "spec-packaging-closure D3's instruction to 'record a pointer' "
            "for this one. Governs msvcp140.dll, vcruntime140.dll, "
            "vcruntime140_1.dll."
        ),
    ),
}


def available_license_texts() -> dict[str, Path]:
    """SPDX identifier -> path to its bundled `<spdx-id>.txt` file, for
    every text file actually present in this directory.

    A directory listing, not `LICENSE_TEXT_SOURCES.keys()` -- so a text file
    someone adds to this directory without also updating the source ledger
    above is still found (and, separately, so a ledger entry with no
    corresponding file is NOT reported as available; `LICENSE_TEXT_SOURCES`
    is provenance metadata, this function is what the builder actually
    trusts to decide a text exists).
    """
    return {path.stem: path for path in sorted(LICENSE_TEXTS_DIR.glob("*.txt"))}


class LicenseTextTamperError(RuntimeError):
    """A bundled license text does not match its recorded SHA-256."""


def license_text_sha256(path: Path) -> str:
    """SHA-256 of ``path``'s bytes with newlines normalised to LF.

    Normalised deliberately: git may materialise LF-committed text as CRLF on
    a Windows checkout, so hashing raw bytes would make an identical file
    verify on Linux and fail on Windows. Normalising compares CONTENT, which
    is what a license text's integrity actually means.
    """
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def verify_bundled_license_texts() -> None:
    """Confirm every bundled text still matches its pinned upstream hash.

    This is what turns `LICENSE_TEXT_SOURCES` from a comment into a control.
    Round 1 asked for pinned sources and hashes; the first attempt recorded
    a `main`-branch URL and a date, which can describe where bytes came from
    but can never DETECT that the bytes changed. The builder copies whatever
    committed bytes it finds, so without this check an altered license text
    would be blessed into a fresh manifest with a valid-looking hash of the
    wrong content -- the manifest would faithfully attest to a corrupted
    notice.

    Since the round-4 CC-WS5-PKG-012 fix, EVERY current entry is pinned --
    including `LicenseRef-Microsoft-VCRedist.txt`, which is CivicCast-authored
    prose with no upstream to fetch but is integrity-pinned all the same, so
    mutating it raises like any other text. The check also enforces exact set
    equality between the bundled texts and this ledger IN BOTH DIRECTIONS: an
    unledgered text on disk is a refusal, not a pass.

    Raises `LicenseTextTamperError` naming every mismatch, every entry whose
    file is missing, and every file the ledger does not name.
    """
    available = available_license_texts()
    problems: list[str] = []

    # EXACT correspondence, both directions. Round 4 (CC-WS5-PKG-012) showed the
    # ledger-driven loop below only ever asked "does each LEDGERED entry match?"
    # -- so a `.txt` file nobody had reviewed could sit in this directory,
    # verify clean, and be staged into the shipped tree as an authoritative
    # notice. `available_license_texts()` is a directory listing precisely so
    # the builder trusts what is on disk, which makes an unledgered file a
    # supply-chain gap rather than an untidiness.
    unledgered = sorted(set(available) - set(LICENSE_TEXT_SOURCES))
    if unledgered:
        problems.append(
            "text file(s) present with NO provenance entry: "
            + ", ".join(f"{name}.txt" for name in unledgered)
            + " -- every bundled licence text must be accounted for in "
            "LICENSE_TEXT_SOURCES before it can ship"
        )

    for spdx_id, source in sorted(LICENSE_TEXT_SOURCES.items()):
        if not source.sha256:
            continue
        path = available.get(spdx_id)
        if path is None:
            problems.append(
                f"{spdx_id}: pinned in LICENSE_TEXT_SOURCES but no {spdx_id}.txt on disk"
            )
            continue
        actual = license_text_sha256(path)
        if actual != source.sha256:
            problems.append(
                f"{spdx_id}: content does not match its pinned upstream\n"
                f"    expected {source.sha256}\n"
                f"    actual   {actual}\n"
                f"    upstream {source.source}"
            )

    if problems:
        raise LicenseTextTamperError(
            "bundled license text(s) do not match their pinned upstream sources:\n  "
            + "\n  ".join(problems)
            + "\n\nA license text is legally operative content, not source code: if it "
            "changed, either it was altered in this repo (which must be reverted, not "
            "re-pinned) or the pinned upstream was deliberately updated (in which case "
            "re-fetch from the new immutable commit and update both the commit and the "
            "sha256 here in the same change)."
        )
