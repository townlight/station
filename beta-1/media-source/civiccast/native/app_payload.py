# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Policy core for the native Windows APPLICATION payload (`slice:ws5-installer`
WP-6 Part A).

The media runtime closure (`runtime_closure` / `runtime_manifest` /
`runtime_licenses`) makes the product's GStreamer/FFmpeg plumbing shippable.
This module is its sibling for the OTHER half a bootable install needs: the
CPython 3.12 interpreter the product runs on, the `civiccast` application, and
its hash-pinned third-party pip dependencies (the WP-5 app-payload finding,
`wp5-app-payload-finding.md`).

Same hard-won design as the closure, deliberately re-used rather than
re-invented:

* **Deny-by-default authorized distributions.** `AUTHORIZED_APP_DISTRIBUTIONS`
  is the exact reviewed set of pip distributions permitted into the payload. An
  unknown distribution FAILS the build even if its declared license looks fine
  (`runtime_closure.assert_authorized_distributions` proved this matters: a
  renamed/replaced/injected distribution is by construction absent from any
  denylist you thought to write down).
* **Per-distribution evidence-backed license, not the wheel's self-report as
  authority.** `APP_DISTRIBUTION_LICENSE` records the SPDX license this
  investigation confirmed for each authorized distribution (see the sweep in
  `.agent-runs/native-windows/ws5-installer/evidence/wp6-app-payload-design.md`).
  The build ALSO reads each installed wheel's own METADATA and refuses if that
  self-report names a prohibited (GPL/AGPL) license the map missed -- the wheel
  metadata is a cross-check INPUT, never the sole authority (spec-packaging-
  closure D3's stance, applied here).
* **Hard fail on GPL/AGPL/unknown/missing.** No waivers. LGPL is acceptable
  (psycopg 3 is LGPL-3.0-only) under the same posture the closure ships LGPL
  binaries with their texts; GPL/AGPL are refused.

Everything here is pure: no filesystem, no subprocess, no PE parsing. The build
script (`scripts/build_native_app_payload.py`) supplies the installed
distribution names and their live METADATA licenses; the tests supply
dictionaries. So the policy is provable without a ~150 MB staged interpreter.
"""

from __future__ import annotations

import re
from typing import Final

from civiccast.native.runtime_licenses import is_gpl_license

__all__ = [
    "APP_BUILD_REQUIREMENTS_SHA256",
    "APP_BUILD_TOOLCHAIN",
    "APP_BUILD_TOOLCHAIN_LOCK_SHA256",
    "APP_BYTECODE_POLICY_PATH",
    "APP_BYTECODE_POLICY_PREFIX",
    "APP_DISTRIBUTION_LICENSE",
    "APP_EXTERNAL_LICENSE_FILES",
    "APP_MANIFEST_SCHEMA_VERSION",
    "APP_PAYLOAD_COMPONENT",
    "APP_REQUIREMENTS_SHA256",
    "AUTHORIZED_APP_DISTRIBUTIONS",
    "AUTHORIZED_NON_WHEEL_COMPONENTS",
    "CAPTION_PACK_COMPONENT",
    "CAPTION_PACK_CONTRACT",
    "CIVICCAST_CONSOLE_ENTRY_POINTS",
    "CIVICCAST_CONSOLE_LAUNCHERS",
    "CIVICCAST_DISTRIBUTION",
    "CIVICCAST_RETAINED_WHEEL_PATH",
    "EMBEDDED_FFMPEG_BUILD",
    "EMBEDDED_FFMPEG_LICENSE",
    "INTERPRETER_DISTRIBUTION",
    "INTERPRETER_LICENSE",
    "INTERPRETER_SHA256",
    "INTERPRETER_SOURCE_URL",
    "INTERPRETER_VERSION",
    "INTERPRETER_ZIP_BYTES",
    "MSVC_RUNTIME_DISTRIBUTION",
    "MSVC_RUNTIME_FILES",
    "WHISPER_MODEL_DISTRIBUTION",
    "WHISPER_MODEL_FILES",
    "WHISPER_MODEL_LICENSE",
    "WHISPER_MODEL_PAYLOAD_DIR",
    "WHISPER_MODEL_REPO",
    "WHISPER_MODEL_REVISION",
    "WHISTLE_ENGINE_CONTRACT",
    "WHISTLE_ENGINE_DLL_BYTES",
    "WHISTLE_ENGINE_DLL_SHA256",
    "WHISTLE_ENGINE_MEMBER",
    "WHISTLE_ENGINE_VERSION",
    "WHISTLE_ENGINE_WHEEL_BYTES",
    "WHISTLE_ENGINE_WHEEL_SHA256",
    "WHISTLE_ENGINE_WHEEL_URL",
    "WHISTLE_MODEL_BYTES",
    "WHISTLE_MODEL_LICENSE",
    "WHISTLE_MODEL_SHA256",
    "WHISTLE_MODEL_SOURCE_URL",
    "WHISTLE_PACK_COMPONENT",
    "WHISTLE_PACK_CONTRACT",
    "WHISTLE_PACK_FILES",
    "AppPayloadError",
    "ProhibitedLicenseError",
    "UnauthorizedAppDistributionError",
    "UnknownAppLicenseError",
    "assert_authorized_app_distributions",
    "assert_no_prohibited_declared_licenses",
    "canonical_distribution_name",
    "component_version_for_payload_path",
    "is_prohibited_license",
    "license_for_payload_path",
    "resolve_app_license",
]

APP_MANIFEST_SCHEMA_VERSION: Final[int] = 8
#: The pack "component" identity for the signed native-app-payload
#: ``.ccpack`` (``scripts/build_native_app_payload_pack.py``): the CPython
#: 3.12 embeddable interpreter + the ``civiccast`` wheel + its hash-pinned
#: third-party dependency wheels, packaged from the tree
#: ``scripts/build_native_app_payload.py`` builds. Matches the ``component``
#: field ``native_pack_staging.rs``'s ``DEFAULT_REQUIRED_COMPONENTS`` and
#: ``ensure_pack_extracted``'s app-payload bridge check against, and the
#: field ``civiccast.installer.native_packs`` manifests carry -- mirroring
#: ``civiccast.native.provision.pack.SERVER_BINARIES_COMPONENT``'s role for
#: the native-server-binaries pack.
APP_PAYLOAD_COMPONENT: Final[str] = "native-app-payload"
APP_BYTECODE_POLICY_PATH: Final[str] = "Lib/site-packages/distutils-precedence.pth"
APP_BYTECODE_POLICY_PREFIX: Final[bytes] = b"import sys; sys.dont_write_bytecode = True\n"
CIVICCAST_RETAINED_WHEEL_PATH: Final[str] = "WHEELS/civiccast.whl"
# `uv pip install --target` materializes these two console launchers from the
# retained wheel's reviewed entry_points.txt. The builder then rewrites their uv
# trampoline resources to use the payload-relative interpreter and to disable
# bytecode before importing CivicCast. Their final bytes are deterministic and
# independent of the build workspace path.
# payload-relative-to-site-packages path -> (bytes, sha256)
CIVICCAST_CONSOLE_LAUNCHERS: Final[dict[str, tuple[int, str]]] = {
    "bin/civiccast-runtime.exe": (
        46_080,
        "d97f5e86b9d70a4014b7244c760a26976623e922713e289d328c7eb1abcd0f5c",
    ),
    "bin/civiccast.exe": (
        46_080,
        "de38a6cb600838803aae9273991a1b8f15a493b8a0f99ffde7ddbeeb44b0668b",
    ),
}
CIVICCAST_CONSOLE_ENTRY_POINTS: Final[dict[str, str]] = {
    "civiccast": "civiccast.cli:main_entrypoint",
    "civiccast-runtime": "civiccast.native.runtime_cli:main_entrypoint",
}
WHISPER_MODEL_DISTRIBUTION: Final[str] = "faster-whisper-large-v3-model"
WHISPER_MODEL_LICENSE: Final[str] = "MIT"
WHISPER_MODEL_PAYLOAD_DIR: Final[str] = "MODELS/faster-whisper-large-v3"
WHISPER_MODEL_REPO: Final[str] = "Systran/faster-whisper-large-v3"
WHISPER_MODEL_REVISION: Final[str] = "edaa852ec7e145841d8ffdb056a99866b5f0a478"
# Exact files from the immutable Hugging Face revision above. The model repo's
# card declares MIT; the complete upstream Whisper MIT text is shipped as a
# separately pinned license artifact.
# filename -> (bytes, sha256)
WHISPER_MODEL_FILES: Final[dict[str, tuple[int, str]]] = {
    "README.md": (
        2_052,
        "39e96252229f5a3d0141dc81afb65a36fd205461ac21e5b70f2cd1248ef0082c",
    ),
    "config.json": (
        2_394,
        "a9306624f5ec14270a014b647e5c316b6e03a662c369758d1b90697a7b0655b9",
    ),
    "model.bin": (
        3_087_284_237,
        "69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1",
    ),
    "preprocessor_config.json": (
        340,
        "7ccc62c6f2765af1f3b46c00c9b5894426835a05021c8b9c01eecb6dfb542711",
    ),
    "tokenizer.json": (
        2_480_617,
        "6d8cbd7cd0d8d5815e478dac67b85a26bbe77c1f5e0c6d76d1ce2abc0e5f21ca",
    ),
    "vocabulary.json": (
        1_068_114,
        "c69260f2ab26d659b7c398f9a2b2b48ed0df16c3b47d7326782fd9cba71690c1",
    ),
}
WHISTLE_PACK_COMPONENT: Final[str] = "captions-whistle"
WHISTLE_MODEL_SOURCE_URL: Final[str] = (
    "https://huggingface.co/Cactus-Compute/whistle/resolve/main/whistle.cact"
)
WHISTLE_MODEL_BYTES: Final[int] = 16_919_407
WHISTLE_MODEL_SHA256: Final[str] = (
    "b6e02f048568ac5d01a2042556c658061e699acbc0aa2a1439f52f3d461dffeb"
)
WHISTLE_MODEL_LICENSE: Final[str] = "Apache-2.0"
WHISTLE_ENGINE_VERSION: Final[str] = "3.1.0"
WHISTLE_ENGINE_WHEEL_URL: Final[str] = (
    "https://huggingface.co/Cactus-Compute/needle3/resolve/main/"
    "python/cactus_needle-3.1.0-py3-none-win_amd64.whl"
)
WHISTLE_ENGINE_WHEEL_BYTES: Final[int] = 683_889
WHISTLE_ENGINE_WHEEL_SHA256: Final[str] = (
    "4f5fc86abfc50d551cdb237a34b501f36d82d4b6f5911ee7bec4e9532d44dd95"
)
WHISTLE_ENGINE_MEMBER: Final[str] = "needle/libneedle3.dll"
WHISTLE_ENGINE_DLL_BYTES: Final[int] = 1_502_720
WHISTLE_ENGINE_DLL_SHA256: Final[str] = (
    "de2e2c39cd311fbd9971fad4736abc329ed970653674c203e149c4ef27fd1c62"
)
WHISTLE_PACK_FILES: Final[dict[str, tuple[int, str]]] = {
    "libneedle.dll": (WHISTLE_ENGINE_DLL_BYTES, WHISTLE_ENGINE_DLL_SHA256),
    "whistle.cact": (WHISTLE_MODEL_BYTES, WHISTLE_MODEL_SHA256),
}
WHISTLE_PACK_CONTRACT: Final[dict[str, object]] = {
    "component": WHISTLE_PACK_COMPONENT,
    "required": True,
    "model_name": "whistle",
    "model_source_url": WHISTLE_MODEL_SOURCE_URL,
    "model_bytes": WHISTLE_MODEL_BYTES,
    "model_sha256": WHISTLE_MODEL_SHA256,
    "model_license": WHISTLE_MODEL_LICENSE,
    "engine_distribution": "cactus-needle",
    "engine_version": WHISTLE_ENGINE_VERSION,
    "engine_wheel_source_url": WHISTLE_ENGINE_WHEEL_URL,
    "engine_wheel_sha256": WHISTLE_ENGINE_WHEEL_SHA256,
    "engine_library": "libneedle.dll",
    "engine_library_bytes": WHISTLE_ENGINE_DLL_BYTES,
    "engine_library_sha256": WHISTLE_ENGINE_DLL_SHA256,
    "engine_license": "Apache-2.0",
}
WHISTLE_ENGINE_CONTRACT: Final[dict[str, object]] = {
    "distribution": "cactus-needle",
    "version": WHISTLE_ENGINE_VERSION,
    "license": "Apache-2.0",
    "wheel_source_url": WHISTLE_ENGINE_WHEEL_URL,
    "wheel_sha256": WHISTLE_ENGINE_WHEEL_SHA256,
    "package_file": "Lib/site-packages/needle/libneedle3.dll",
    "file_bytes": WHISTLE_ENGINE_DLL_BYTES,
    "file_sha256": WHISTLE_ENGINE_DLL_SHA256,
}
CAPTION_PACK_COMPONENT: Final[str] = "captions-large-v3"
CAPTION_PACK_CONTRACT: Final[dict[str, object]] = {
    "component": CAPTION_PACK_COMPONENT,
    "required": True,
    "model_architecture": "large-v3",
    "model_directory": "faster-whisper-large-v3",
    "model_repository": WHISPER_MODEL_REPO,
    "model_revision": WHISPER_MODEL_REVISION,
    "model_files": {
        name: {"bytes": size, "sha256": digest}
        for name, (size, digest) in WHISPER_MODEL_FILES.items()
    },
    "runtime_backend": "faster-whisper",
    "runtime_version": "1.2.1",
    "ctranslate2_version": "4.8.1",
    "runtime_device": "cpu",
    "runtime_compute_type": "int8",
    "hardware_acceleration_required": False,
    "self_test_audio_file": "jfk.wav",
    "self_test_audio_bytes": 352_078,
    "self_test_audio_sha256": ("59dfb9a4acb36fe2a2affc14bacbee2920ff435cb13cc314a08c13f66ba7860e"),
}
EMBEDDED_FFMPEG_BUILD: Final[str] = "8c9502e9b0-minimal-msvc"
EMBEDDED_FFMPEG_LICENSE: Final[str] = "LGPL-2.1-or-later"
#: SHA-256 of ``requirements-native-app-build.txt``.
APP_BUILD_REQUIREMENTS_SHA256: Final[str] = (
    "f532bd87cf32e853111205b569a0cbfd8450d266f343e2da8a1a84835d3a1862"
)
#: SHA-256 of ``native-windows-build-toolchain.lock.json``. This binds the
#: payload provenance to canonical acquisition URLs, archive hashes, install
#: layouts, and the exact MSVC component recipe rather than only to whichever
#: matching executables happened to exist on one release machine.
APP_BUILD_TOOLCHAIN_LOCK_SHA256: Final[str] = (
    "a9db3110ff557dfb295798b102ddcd2623959f0bde97aed6c90f833f1ffd5424"
)
#: Exact executable identities allowed to influence the deterministic
#: application/portal build. Paths are deliberately absent so the attestation
#: is reproducible across machines.
APP_BUILD_TOOLCHAIN: Final[dict[str, dict[str, str]]] = {
    "node": {
        "version": "v24.15.0",
        "sha256": "3331e1ffe19874215472217c5e94f5a0c6d8e18c4ac7111d3937aa0ad5e9b4a5",
    },
    "npm": {
        "version": "11.12.1",
        "sha256": "21b46c69ad6e2f231f02a9e120f4ba6c8e75fef5a45637103002eab99f888ab8",
        "tree_sha256": "8b3e116059d650842ae2f263e38ff407bacfee0a4ada4bc9387bbb701d660174",
    },
    "python": {
        "version": "Python 3.12.13",
        "sha256": "e9f7e4baa0da1c21ffc3c2c2a644459b1a38dced56dfc7846e16f801645d520f",
        "tree_sha256": "25547ffe58a4fd8f9faa62078c99fe54f79ff3c4a4547ddad61df0eaf6a3d730",
    },
    "python312.dll": {
        "version": "3.12.13",
        "sha256": "f6ad19fa4d285626f583224699211cdbb28eae9d0fb5415bde94a117543105b7",
    },
    "uv": {
        "version": "uv 0.11.15 (3cffe97c2 2026-05-18 x86_64-pc-windows-msvc)",
        "sha256": "d4ffe0b73cbb1fa3d11242567d55c6e9058c4e885fae9272764409583a4e8640",
    },
}
#: SHA-256 of ``requirements-native-app.txt``. The builder refuses if the
#: checked-in lock drifts from this reviewed identity, and the independent
#: verifier requires the same identity in the signed payload manifest.
#: Re-pinned 2026-08-21 for the NATS JetStream removal (owner decision
#: 2026-08-20; see ADR 0023, which supersedes ADR 0001). Re-pinning this
#: constant IS the review step the guard exists to force, so the diff was
#: checked before changing it: exactly one distribution removed (`nats-py`),
#: nothing else changed in the lock. Never update this hash to "make the
#: build pass" without reading the lock diff first -- the whole point is
#: that a silent dependency substitution in the shipped payload cannot slip
#: through.
#: Re-pinned again 2026-08-25 (fix/self-hosted-lane-av-provenance):
#: `git diff` on `requirements-native-app.txt` checked before changing this
#: -- exactly one byte range changed, `av`'s own `--hash=sha256:...` value
#: (445e6a94... -> 0f9427a4...), matching `build_native_pyav_wheel.py`'s
#: `EXPECTED_WHEEL_SHA256`/`BYTES` re-pin in the same commit (that wheel's
#: own embedded `FFMPEG-PROVENANCE.json` now also records the PyAV sdist's
#: identity, which deterministically changes the reviewed wheel's bytes).
#: Nothing else in the lock changed.
#: Re-pinned 2026-10-07 for pypdf 6.19.0 (uploaded-PDF parser fixes) and
#: urllib3 2.8.0 (fixed botocore transport dependency). The generated app-lock
#: diff preserves all other package versions and hashes; resolver attribution
#: comments were refreshed, and the PyAV provenance override remains intact.
#: Re-pinned 2026-10-25 for targeted pypdf/filelock/setuptools advisories.
#: The existing single LGPL PyAV wheel and all other application pins are retained.
APP_REQUIREMENTS_SHA256: Final[str] = (
    "06d7779dc872efc55b88bc67323f836179e38f350a946bf7b6d89838ec201aa6"
)
#: Exact third-party license files the builder places outside site-packages.
#: payload path -> (distribution, version, license, sha256).
APP_EXTERNAL_LICENSE_FILES: Final[dict[str, tuple[str, str, str, str]]] = {
    "THIRD-PARTY-LICENSES/CTranslate2-MIT.txt": (
        "ctranslate2",
        "4.8.1",
        "MIT",
        "54aa79d9fe3c09e67a16dcd95b9e88676405a6ec174efda31036983cf7672ecb",
    ),
    "THIRD-PARTY-LICENSES/FlatBuffers-Apache-2.0.txt": (
        "flatbuffers",
        "25.12.19",
        "Apache-2.0",
        "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
    ),
    "THIRD-PARTY-LICENSES/Tokenizers-Apache-2.0.txt": (
        "tokenizers",
        "0.23.1",
        "Apache-2.0",
        "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4",
    ),
    "THIRD-PARTY-LICENSES/Whisper-MIT.txt": (
        WHISPER_MODEL_DISTRIBUTION,
        WHISPER_MODEL_REVISION,
        WHISPER_MODEL_LICENSE,
        "b5d65a59060e68c4ff940e1eddfa6f94b2d68fdf58ed7f4dd57721c997e35e9d",
    ),
}


# ---------------------------------------------------------------------------
# The interpreter -- CPython 3.12 Windows x64 EMBEDDABLE distribution
# ---------------------------------------------------------------------------
#
# python.org's purpose-built embeddable zip: a self-contained, redistributable
# interpreter layout with no installer of its own, no registry/machine state,
# and a deterministic tree -- the least-surprise fit for "place a CPython beside
# the tree" that the closure's HOST_PYTHON_REQUIREMENT names. It satisfies that
# requirement exactly: cp312, so the media closure's `.pyd` files (which link
# python312.dll) load against it. Pinned by SHA-256; the build verifies the
# downloaded bytes against this pin before extracting.

INTERPRETER_DISTRIBUTION: Final[str] = "cpython-embeddable"
INTERPRETER_VERSION: Final[str] = "3.12.10"
INTERPRETER_SOURCE_URL: Final[str] = (
    "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"
)
#: SHA-256 of the pinned embeddable zip (verified on download 2026-07-24).
INTERPRETER_SHA256: Final[str] = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"
INTERPRETER_ZIP_BYTES: Final[int] = 11133606
#: PSF License Agreement (the `LICENSE.txt` inside the embeddable zip). Bundled
#: in the payload's BOM.
INTERPRETER_LICENSE: Final[str] = "PSF-2.0"

# The CTranslate2 Windows wheel links the Microsoft C++ runtime dynamically.
# A developer workstation normally has this DLL globally installed, which can
# hide a broken supposedly self-contained payload. Ship the reviewed x64
# app-local redistributable and bind its exact bytes into the payload policy.
MSVC_RUNTIME_DISTRIBUTION: Final[str] = "microsoft-vc-runtime"
MSVC_RUNTIME_FILES: Final[dict[str, dict[str, str | int]]] = {
    "msvcp140.dll": {
        "bytes": 553_552,
        "sha256": "def46aa6a8f72f27bafac0c43334419486a4d1dcdb6c479a8ef7034b3e1fa4cb",
        "version": "14.50.35719.0",
        "license": "LicenseRef-Microsoft-VCRedist",
    }
}


# ---------------------------------------------------------------------------
# The application itself
# ---------------------------------------------------------------------------

#: The canonical distribution name for the CivicCast wheel built from THIS repo.
CIVICCAST_DISTRIBUTION: Final[str] = "civiccast"


# ---------------------------------------------------------------------------
# Authorized distributions + their confirmed licenses (deny-by-default)
# ---------------------------------------------------------------------------
#
# One entry per pip distribution permitted into the payload, canonical
# (PEP 503) name -> the SPDX license identifier this investigation confirmed.
# This IS the authorized set (`AUTHORIZED_APP_DISTRIBUTIONS` is its keys), so a
# distribution with no entry here fails the deny-by-default gate.
#
# Derived from the pinned lock `requirements-native-app.txt` (compiled from
# pyproject.toml's base runtime dependencies for cp312/windows) by installing it
# and reading every `*.dist-info/METADATA` license field. Full sweep table +
# per-distribution resolution of the ambiguous ones is in
# `.agent-runs/native-windows/ws5-installer/evidence/wp6-app-payload-design.md`.
#
# Resolution rules for the ambiguous declarations (do not waive -- resolve with
# evidence, per the charter):
#   * License-Expression present (PEP 639, authoritative SPDX) -> used verbatim.
#   * Only a legacy `License:` free-text field or `Classifier: License :: ...`
#     -> mapped to the SPDX id that text names (e.g. classifier "MIT License"
#     -> MIT; reportlab's "BSD license (see license.txt...)" -> BSD-3-Clause;
#     "PSF"/"PSFL" -> PSF-2.0; "ISC License" -> ISC). The mapping is recorded
#     here so it is a checkable fact, not a build-time guess.
#
# NB LGPL is ACCEPTABLE (psycopg 3 is LGPL-3.0-only) under the same posture the
# media closure ships LGPL binaries -- the wheel's own dist-info bundles the
# LGPL text, which ships with the payload. GPL/AGPL are refused by
# `is_prohibited_license`.
APP_DISTRIBUTION_LICENSE: Final[dict[str, str]] = {
    "alembic": "MIT",
    "annotated-doc": "MIT",
    "annotated-types": "MIT",
    "anyio": "MIT",
    "asn1crypto": "MIT",
    # CivicCast builds this wheel itself. PyAV's wrapper is BSD-3-Clause; its
    # separately linked FFmpeg DLL files are classified LGPL-2.1-or-later by
    # the payload builder's path-level license rule.
    "av": "BSD-3-Clause",
    "boto3": "Apache-2.0",
    "botocore": "Apache-2.0",
    "certifi": "MPL-2.0",
    "cffi": "MIT-0",
    "charset-normalizer": "MIT",
    "click": "BSD-3-Clause",
    "colorama": "BSD-3-Clause",  # classifier-only "BSD License"; colorama is BSD-3-Clause
    "cryptography": "Apache-2.0 OR BSD-3-Clause",
    "ctranslate2": "MIT",
    "defusedxml": "PSF-2.0",  # "PSFL"
    "deprecated": "MIT",
    "fastapi": "MIT",
    "faster-whisper": "MIT",
    "cactus-needle": "Apache-2.0",
    "filelock": "MIT",
    "flatbuffers": "Apache-2.0",
    "fsspec": "BSD-3-Clause",
    "greenlet": "MIT AND PSF-2.0",
    "h11": "MIT",
    "hf-xet": "Apache-2.0",
    "httpcore": "BSD-3-Clause",
    "httptools": "MIT",
    "httpx": "BSD-3-Clause",
    "huggingface-hub": "Apache-2.0",
    "idna": "BSD-3-Clause",
    "jaraco-classes": "MIT",  # classifier-only "MIT License"
    "jaraco-context": "MIT",
    "jaraco-functools": "MIT",
    "jmespath": "MIT",
    "keyring": "MIT",
    "lxml": "BSD-3-Clause",
    "mako": "MIT",
    "markdown-it-py": "MIT",  # classifier-only "MIT License"
    "markupsafe": "BSD-3-Clause",
    "mdurl": "MIT",  # classifier-only "MIT License"
    "more-itertools": "MIT",
    "numpy": "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0",
    "nvidia-ml-py": "BSD-3-Clause",  # legacy "BSD" + classifier "BSD License"
    "onnxruntime": "MIT",
    "packaging": "Apache-2.0 OR BSD-2-Clause",
    "pikepdf": "MPL-2.0",
    "pillow": "MIT-CMU",
    "protobuf": "BSD-3-Clause",
    "psutil": "BSD-3-Clause",
    "psycopg": "LGPL-3.0-only",  # LGPL is acceptable (bundles its own text)
    "psycopg-binary": "LGPL-3.0-only",
    "pycparser": "BSD-3-Clause",
    "pydantic": "MIT",
    "pydantic-core": "MIT",
    "pygments": "BSD-2-Clause",
    "pypdf": "BSD-3-Clause",
    "python-dateutil": "Apache-2.0 OR BSD-3-Clause",
    "python-dotenv": "BSD-3-Clause",
    "python-multipart": "Apache-2.0",
    "pywin32": "PSF-2.0",  # "PSF" + classifier PSF License
    "pywin32-ctypes": "BSD-3-Clause",
    "pyyaml": "MIT",
    "reportlab": "BSD-3-Clause",  # "BSD license (see license.txt...)" + classifier BSD
    "rich": "MIT",
    "s3transfer": "Apache-2.0",
    "setuptools": "MIT",
    "shellingham": "ISC",  # "ISC License" + classifier ISC
    "six": "MIT",
    "sqlalchemy": "MIT",
    "starlette": "BSD-3-Clause",
    "tokenizers": "Apache-2.0",
    "tqdm": "MPL-2.0 AND MIT",
    "typer": "MIT",
    "typing-extensions": "PSF-2.0",
    "typing-inspection": "MIT",
    "tzdata": "Apache-2.0",
    "urllib3": "MIT",
    "uvicorn": "BSD-3-Clause",
    "watchfiles": "MIT",
    "websockets": "BSD-3-Clause",
    "wrapt": "BSD-2-Clause",
    # The application itself, built from THIS repo. Apache-2.0 per every source
    # file's SPDX header and pyproject's declared license.
    CIVICCAST_DISTRIBUTION: "Apache-2.0",
}

#: The deny-by-default authorized set: exactly the keys above. A distribution
#: whose canonical name is not here fails the build even if its license is fine.
AUTHORIZED_APP_DISTRIBUTIONS: Final[frozenset[str]] = frozenset(APP_DISTRIBUTION_LICENSE)
AUTHORIZED_NON_WHEEL_COMPONENTS: Final[frozenset[str]] = frozenset(
    {MSVC_RUNTIME_DISTRIBUTION, WHISPER_MODEL_DISTRIBUTION}
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AppPayloadError(RuntimeError):
    """Base for every app-payload policy refusal."""


class UnauthorizedAppDistributionError(AppPayloadError):
    """An installed distribution is not in `AUTHORIZED_APP_DISTRIBUTIONS`."""


class UnknownAppLicenseError(AppPayloadError):
    """A distribution has no confirmed license in `APP_DISTRIBUTION_LICENSE`."""


class ProhibitedLicenseError(AppPayloadError):
    """A distribution names or offers a GPL/AGPL license (never shippable)."""


# ---------------------------------------------------------------------------
# Canonical names
# ---------------------------------------------------------------------------

_CANONICAL_RE: Final[re.Pattern[str]] = re.compile(r"[-_.]+")


def canonical_distribution_name(name: str) -> str:
    """PEP 503 canonical form: lower-case, runs of ``-``/``_``/``.`` collapsed
    to a single ``-``.

    A distribution's `*.dist-info` directory, its RECORD, and pip all spell the
    same project inconsistently (`jaraco.classes` vs `jaraco_classes`,
    `nvidia-ml-py` vs `nvidia_ml_py`). Normalising both sides through this makes
    the authorized-set membership test compare like with like, so a legitimate
    distribution is never rejected -- and a spoof never accepted -- on a spelling
    difference alone.
    """
    return _CANONICAL_RE.sub("-", name.strip().lower())


# ---------------------------------------------------------------------------
# License predicates + gates
# ---------------------------------------------------------------------------

#: Tokeniser identical to `runtime_licenses._LICENSE_TOKEN_RE`, re-declared here
#: only to add the AGPL check without importing a private symbol.
_LICENSE_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z][A-Za-z0-9.+-]*")


def is_prohibited_license(license_expression: str) -> bool:
    """True if ``license_expression`` names or offers a GPL or AGPL license.

    GPL detection is delegated to `runtime_licenses.is_gpl_license`, which is
    token-based and correctly does NOT flag `LGPL-*` (LGPL is acceptable here).
    AGPL is added on top: `AGPL` bare or any `AGPL-*` token. Catches a prohibited
    branch inside an `AND`/`OR` expression too (e.g. a hypothetical
    "MIT OR GPL-3.0-only"), because offering a GPL option is offering GPL.
    """
    if is_gpl_license(license_expression):
        return True
    normalized = license_expression.upper()
    if "GNU AFFERO GENERAL PUBLIC LICENSE" in normalized:
        return True
    for license_term in _LICENSE_TOKEN_RE.findall(normalized):
        if (
            license_term == "AGPL"
            or license_term.startswith("AGPL-")
            or re.fullmatch(r"AGPLV\d+(?:\.\d+)?", license_term)
        ):
            return True
    return False


def resolve_app_license(canonical_name: str) -> str:
    """The confirmed SPDX license for an authorized distribution.

    Raises `UnknownAppLicenseError` for a name with no entry -- AC7's rule, same
    as the closure: never guess a license for a shipped artifact.
    """
    license_ = APP_DISTRIBUTION_LICENSE.get(canonical_name)
    if license_ is None:
        raise UnknownAppLicenseError(
            f"no confirmed license for distribution {canonical_name!r}: add an "
            "evidence-backed entry to APP_DISTRIBUTION_LICENSE (never guess a "
            "license for a shipped artifact)"
        )
    return license_


def license_for_payload_path(distribution: str, path: str) -> str:
    """Return the reviewed component license for a shipped payload path."""

    normalized = path.replace("\\", "/")
    if distribution == "av" and (
        normalized.startswith("Lib/site-packages/av.libs/")
        or "/FFMPEG-PROVENANCE.json" in normalized
        or "/licenses/FFmpeg-LGPL-2.1-or-later.txt" in normalized
    ):
        return EMBEDDED_FFMPEG_LICENSE
    return resolve_app_license(distribution)


def component_version_for_payload_path(
    distribution: str,
    distribution_version: str,
    path: str,
) -> str:
    """Return the actual component identity represented by a payload path."""

    if (
        distribution == "av"
        and license_for_payload_path(distribution, path) == EMBEDDED_FFMPEG_LICENSE
    ):
        return EMBEDDED_FFMPEG_BUILD
    return distribution_version


def assert_authorized_app_distributions(canonical_names: frozenset[str]) -> None:
    """Deny-by-default: every installed distribution must be authorized, and no
    authorized distribution may carry a prohibited license.

    Raises `UnauthorizedAppDistributionError` naming every installed
    distribution absent from `AUTHORIZED_APP_DISTRIBUTIONS`, and
    `ProhibitedLicenseError` if any authorized distribution's confirmed license
    is itself GPL/AGPL (belt-and-braces: the map should never contain one, but
    a bad edit must fail the build, not ship).
    """
    unauthorized = sorted(canonical_names - AUTHORIZED_APP_DISTRIBUTIONS)
    if unauthorized:
        raise UnauthorizedAppDistributionError(
            "refusing to build the app payload -- installed distribution(s) are "
            "not in the reviewed AUTHORIZED_APP_DISTRIBUTIONS allowlist "
            "(deny-by-default: a renamed/replaced/injected distribution is by "
            f"construction absent from any denylist):\n  {', '.join(unauthorized)}"
        )
    prohibited = sorted(
        name for name in canonical_names if is_prohibited_license(APP_DISTRIBUTION_LICENSE[name])
    )
    if prohibited:
        raise ProhibitedLicenseError(
            "refusing to build the app payload -- APP_DISTRIBUTION_LICENSE maps "
            f"a GPL/AGPL license for: {', '.join(prohibited)}. GPL-family "
            "licenses are never shipped in the box."
        )


def assert_no_prohibited_declared_licenses(declared_by_distribution: dict[str, str]) -> None:
    """Cross-check the live wheel METADATA: refuse if any installed distribution
    SELF-REPORTS a prohibited (GPL/AGPL) license.

    `APP_DISTRIBUTION_LICENSE` is the authority, but a wheel whose upstream
    switched to GPL in a version bump would still install (same pinned name) and
    the map could lag. Reading each wheel's own declared license and refusing on
    a prohibited one closes that drift: the wheel metadata is an INPUT the build
    must not contradict, exactly as `gst-inspect` metadata is for the closure.

    ``declared_by_distribution`` maps canonical name -> the license string read
    from that distribution's `*.dist-info/METADATA` (License-Expression, or
    License, or the License classifiers joined). Raises `ProhibitedLicenseError`
    naming every offender.
    """
    offenders = sorted(
        f"{name} (METADATA declares {declared!r})"
        for name, declared in declared_by_distribution.items()
        if declared and is_prohibited_license(declared)
    )
    if offenders:
        raise ProhibitedLicenseError(
            "refusing to build the app payload -- installed wheel METADATA "
            "self-reports a GPL/AGPL license (a prohibited-license drift the "
            "confirmed map did not catch):\n  " + "\n  ".join(offenders)
        )
