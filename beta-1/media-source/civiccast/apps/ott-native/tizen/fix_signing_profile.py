#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Patch Tizen Studio CLI's broken signing-profile passwords before `tizen package`.

Root cause (found on a diagnostic ci-ott-apps run: a base64'd dump of the
generated profiles.xml, needed because GitHub Actions' log masking hides
the plaintext otherwise -- see the `tizen` job in
.github/workflows/ci-ott-apps.yml for exactly where this runs):

Tizen Studio CLI 2.5.25's `tizen security-profiles add` writes

    password="/home/runner/tizen-studio-data/keystore/author/CivicCastCI.pwd"

into profiles.xml for BOTH the author profile we create and the default
distributor profile it auto-attaches -- a path to a `.pwd` sidecar file
that is never created, instead of the real plaintext password. `tizen
package`'s signer then reads that path string literally as the PKCS#12
password and fails with:

    org.tizen.common.sign.exception.CertificationException: Invaild password

at `ReadSigningProfileFileCommand.checkPkcs12Password`. This is not a
CivicCast-specific misconfiguration: two other projects hit the identical
stack trace running `tizen package` headlessly (jellyfin/jellyfin-tizen#66,
fgl27/smarttv-twitch#41); the latter's documented fix is the same one
applied here -- replace the bogus `.pwd` path with the real plaintext
password Tizen Studio's interactive Certificate Manager would have written.

Two passwords need patching:
  - The author profile's password is the one the workflow set explicitly
    via `tizen certificate -a CivicCastCI -p tizenpkcs ...`.
  - The distributor profile's password is Tizen SDK's own documented,
    public, well-known password for the sample distributor certificate it
    ships and auto-attaches (tools/certificate-generator/certificates/
    distributor/tizen-distributor-signer.p12) when no `-d/-dp` distributor
    profile is supplied. Using it is fine here: S12 is code-verify only,
    no store submission (2026-06-14 owner decision), so this is not a real
    signing identity, just what makes the headless CLI's own default
    profile actually load.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

AUTHOR_PASSWORD = "tizenpkcs"  # noqa: S105 -- matches the workflow's own `tizen certificate -p` arg, not a secret
DISTRIBUTOR_PASSWORD = "tizenpkcs12passfordsigner"  # noqa: S105 -- Tizen SDK's own public sample-cert password, not a secret

# Matches `key="...CivicCastCI.p12" password="...some/path.pwd"` and the
# distributor equivalent, regardless of the exact sidecar path Tizen wrote
# (it includes $HOME, which varies by runner).
_PATCHES = (
    (
        re.compile(r'(key="[^"]*CivicCastCI\.p12")\s+password="[^"]*\.pwd"'),
        rf'\1 password="{AUTHOR_PASSWORD}"',
    ),
    (
        re.compile(r'(key="[^"]*tizen-distributor-signer\.p12")\s+password="[^"]*\.pwd"'),
        rf'\1 password="{DISTRIBUTOR_PASSWORD}"',
    ),
)


def main() -> int:
    profiles_path = Path.home() / "tizen-studio-data" / "profile" / "profiles.xml"
    if not profiles_path.is_file():
        print(f"fix_signing_profile: {profiles_path} does not exist", file=sys.stderr)
        return 1

    xml = profiles_path.read_text(encoding="utf-8")
    patched = xml
    for pattern, replacement in _PATCHES:
        patched, count = pattern.subn(replacement, patched)
        if count == 0:
            print(
                f"fix_signing_profile: pattern {pattern.pattern!r} matched nothing "
                "-- Tizen CLI's security-profiles add output may have changed; "
                "re-diagnose (dump profiles.xml as base64 in CI) before assuming "
                "this fix still applies",
                file=sys.stderr,
            )
            return 1

    profiles_path.write_text(patched, encoding="utf-8")
    print(f"fix_signing_profile: patched {profiles_path} ({len(_PATCHES)} password field(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
