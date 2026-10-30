# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Deterministic signed component packs for the native Windows installer."""

from __future__ import annotations

import base64
import hashlib
import json
import stat
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from civiccast.native.app_payload import (
    CAPTION_PACK_CONTRACT,
    WHISTLE_PACK_COMPONENT,
    WHISTLE_PACK_CONTRACT,
    WHISTLE_PACK_FILES,
)
from civiccast.native.caption_tiers import (
    CAPTION_TIER_REGISTRY,
    CaptionTierBindingError,
    CaptionTierSpec,
)

PACK_SCHEMA_VERSION = 1
PACK_PRODUCT = "civiccast-native"
PACK_MANIFEST_NAME = "manifest.json"
PACK_SIGNATURE_NAME = "manifest.sig"
PACK_PAYLOAD_PREFIX = "payload/"
CAPTION_COMPONENT = "captions-large-v3"
WHISTLE_COMPONENT = WHISTLE_PACK_COMPONENT
CAPTION_SELF_TEST_PATH = f"self-test/{CAPTION_PACK_CONTRACT['self_test_audio_file']}"
CAPTION_SELF_TEST_BYTES = int(str(CAPTION_PACK_CONTRACT["self_test_audio_bytes"]))
CAPTION_SELF_TEST_SHA256 = str(CAPTION_PACK_CONTRACT["self_test_audio_sha256"])
OLLAMA_MODEL_LOCK_PATH = (
    Path(__file__).resolve().parents[2] / "native-windows-ollama-models.lock.json"
)
OLLAMA_MODEL_COMPONENTS = frozenset(
    {
        "summary-gemma4-12b",
        "summary-gemma4-e4b",
        "translation-translategemma-4b",
    }
)
SOURCE_BOUND_COMPONENTS = frozenset(
    {
        "native-app-payload",
        "native-server-binaries",
    }
)
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_REPARSE_POINT = 0x400
_MANIFEST_FIELDS = {
    "schema_version",
    "product",
    "component",
    "product_version",
    "compatible_core",
    "signing_key_id",
    "file_count",
    "total_bytes",
    "files",
    "metadata",
}
_WINDOWS_RESERVED_NAMES = {
    "aux",
    "clock$",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
_WINDOWS_FORBIDDEN_CHARS = frozenset('<>"|?*')


class NativePackVerificationError(ValueError):
    """Raised before extraction when a native component pack is untrusted."""


@dataclass(frozen=True)
class NativePackResult:
    """Verified identity and byte inventory for one component pack."""

    path: Path
    sha256: str
    component: str
    product_version: str
    compatible_core: str
    signing_key_id: str
    file_count: int
    total_bytes: int
    metadata: dict[str, Any]
    payload_tree_sha256: str


def payload_tree_sha256(files: Sequence[Mapping[str, Any]]) -> str:
    """Deterministic hash over a native pack's payload CONTENT *and*
    STRUCTURE, computed from the pack's own signed manifest ``files``
    entries -- reused, not re-derived: each entry already carries a
    POSIX-normalized, pack-relative ``path`` and the SHA-256 ``sha256`` of
    that one file's own bytes, both hashed exactly once, by
    ``build_native_pack``/``_validated_manifest`` (``verify_native_pack``'s
    per-entry byte re-hash on read). This function never re-reads or
    re-hashes payload bytes; it only combines hashes the pack format already
    computes and signs.

    This closes the gap where two independently-built packs of the SAME
    payload, signed with two DIFFERENT (machine-local) signing keys, differ
    in ``pack_sha256`` (expected -- the signing key id is embedded in the
    signed container) while ``payload_bytes``/``file_count`` alone cannot
    prove the underlying file CONTENTS and LAYOUT actually matched: same
    total size and count can hide a renamed file, a swapped pair of
    same-size files, or any content difference two files happen to share a
    size with.

    Recipe (documented precisely enough to reimplement independently and get
    the same digest):

      1. Start from a pack's ``files`` manifest entries -- objects with (at
         least) a ``path`` string and a lowercase-hex ``sha256`` string.
      2. Sort the entries by ``path`` using plain Python string ("ordinal"
         / byte-wise on the UTF-8 text) comparison. ``build_native_pack``
         already constructs ``files`` from ``sorted(sources.items())``, so
         this is a no-op in the pack-building path, but sorting here makes
         this function's result independent of whatever order its caller
         happens to pass entries in (a reordered directory walk cannot
         change the result).
      3. Feed one SHA-256 hasher, in that sorted order, for every entry:

             utf8(entry["path"]) + b"\\x00" + ascii(entry["sha256"]) + b"\\n"

         -- the pack-relative path, a NUL separator, the entry's own
         lowercase-hex SHA-256, then a newline terminator. The separator and
         terminator exist so two entries' fields can never concatenate into
         an ambiguous byte stream (e.g. path ``"ab"`` + sha ``"c..."`` vs.
         path ``"a"`` + sha ``"bc..."``).
      4. Return the resulting hex digest.

    Consequences, each following directly from the recipe above:

      * Renaming a payload file changes its ``path`` (step 3's material),
        so the result changes even though total ``payload_bytes``/
        ``file_count`` are identical.
      * A one-byte content change changes that file's own recorded
        ``sha256`` (already a real content hash over that file's bytes), so
        it changes this result too.
      * Signing the identical payload with a different signing key changes
        nothing here: the signing key id and signature live outside
        ``files`` entirely, so two packs built from identical payload
        content under different keys hash to the SAME
        ``payload_tree_sha256`` even though their ``pack_sha256`` (the
        signed container's own bytes) differ.
    """

    hasher = hashlib.sha256()
    for entry in sorted(files, key=lambda item: str(item["path"])):
        hasher.update(str(entry["path"]).encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(str(entry["sha256"]).encode("ascii"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def build_native_pack(
    *,
    output: Path,
    component: str,
    product_version: str,
    compatible_core: str,
    sources: dict[str, Path],
    signing_private_key: Ed25519PrivateKey,
    signing_key_id: str,
    metadata: dict[str, Any] | None = None,
) -> NativePackResult:
    """Build and self-verify a byte-reproducible signed ZIP64 component pack."""

    output = output.expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"native pack output already exists: {output}")
    if not component.strip() or not product_version.strip() or not compatible_core.strip():
        raise ValueError("native pack identity fields must not be empty")
    if not signing_key_id.strip():
        raise ValueError("native pack signing key id must not be empty")
    if not sources:
        raise ValueError("native pack must contain at least one payload file")

    normalized_sources: dict[str, Path] = {}
    casefold_paths: set[str] = set()
    file_entries: list[dict[str, object]] = []
    total_bytes = 0
    for relative_path, source in sorted(sources.items()):
        normalized = _safe_relative_path(relative_path)
        folded = normalized.casefold()
        if folded in casefold_paths:
            raise ValueError(f"duplicate native pack payload path: {normalized}")
        casefold_paths.add(folded)
        source_candidate = Path(source).expanduser()
        _require_regular_source(source_candidate)
        source = source_candidate.resolve(strict=True)
        size, digest = _file_size_sha256(source)
        total_bytes += size
        normalized_sources[normalized] = source
        file_entries.append(
            {
                "bytes": size,
                "path": normalized,
                "sha256": digest,
            }
        )

    pack_metadata = dict(metadata or {})
    manifest: dict[str, object] = {
        "schema_version": PACK_SCHEMA_VERSION,
        "product": PACK_PRODUCT,
        "component": component,
        "product_version": product_version,
        "compatible_core": compatible_core,
        "signing_key_id": signing_key_id,
        "file_count": len(file_entries),
        "total_bytes": total_bytes,
        "files": file_entries,
        "metadata": pack_metadata,
    }
    manifest_bytes = _canonical_json(manifest)
    signature = signing_private_key.sign(manifest_bytes)
    signature_bytes = base64.b64encode(signature) + b"\n"

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f".{output.name}.",
        suffix=".partial",
        dir=output.parent,
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_STORED,
            allowZip64=True,
        ) as archive:
            archive.writestr(_zip_info(PACK_MANIFEST_NAME), manifest_bytes)
            archive.writestr(_zip_info(PACK_SIGNATURE_NAME), signature_bytes)
            for relative_path, source in normalized_sources.items():
                info = _zip_info(PACK_PAYLOAD_PREFIX + relative_path)
                info.file_size = source.stat().st_size
                with (
                    source.open("rb") as input_file,
                    archive.open(
                        info,
                        mode="w",
                        force_zip64=True,
                    ) as output_file,
                ):
                    while chunk := input_file.read(1024 * 1024):
                        output_file.write(chunk)

        verified = verify_native_pack(
            temporary,
            public_key=signing_private_key.public_key(),
            expected_component=component,
            expected_product_version=product_version,
            expected_compatible_core=compatible_core,
            expected_signing_key_id=signing_key_id,
        )
        temporary.replace(output)
        return NativePackResult(
            path=output,
            sha256=_file_size_sha256(output)[1],
            component=verified.component,
            product_version=verified.product_version,
            compatible_core=verified.compatible_core,
            signing_key_id=verified.signing_key_id,
            file_count=verified.file_count,
            total_bytes=verified.total_bytes,
            metadata=verified.metadata,
            payload_tree_sha256=verified.payload_tree_sha256,
        )
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def verify_native_pack(
    pack: Path,
    *,
    public_key: Ed25519PublicKey,
    expected_component: str | None = None,
    expected_product_version: str | None = None,
    expected_compatible_core: str | None = None,
    expected_signing_key_id: str | None = None,
) -> NativePackResult:
    """Verify signature, identity, archive surface, and every payload byte."""

    pack = pack.expanduser().resolve()
    if not pack.is_file():
        raise NativePackVerificationError(f"native pack is missing: {pack}")
    try:
        with zipfile.ZipFile(pack, mode="r", allowZip64=True) as archive:
            infos = archive.infolist()
            info_by_name: dict[str, zipfile.ZipInfo] = {}
            seen_casefold: set[str] = set()
            for info in infos:
                name = _safe_archive_path(info.filename)
                folded = name.casefold()
                if folded in seen_casefold:
                    raise NativePackVerificationError(
                        f"native pack contains a duplicate archive path: {name}"
                    )
                seen_casefold.add(folded)
                if info.is_dir():
                    raise NativePackVerificationError(
                        f"native pack contains an unauthorized directory entry: {name}"
                    )
                info_by_name[name] = info

            for required in (PACK_MANIFEST_NAME, PACK_SIGNATURE_NAME):
                if required not in info_by_name:
                    raise NativePackVerificationError(f"native pack is missing {required}")
            manifest_info = info_by_name[PACK_MANIFEST_NAME]
            if manifest_info.file_size > _MAX_MANIFEST_BYTES:
                raise NativePackVerificationError("native pack manifest is too large")
            manifest_bytes = archive.read(manifest_info)
            signature_bytes = archive.read(info_by_name[PACK_SIGNATURE_NAME])
            try:
                signature = base64.b64decode(signature_bytes.strip(), validate=True)
                public_key.verify(signature, manifest_bytes)
            except (ValueError, InvalidSignature) as exc:
                raise NativePackVerificationError(
                    "native pack manifest signature is invalid"
                ) from exc
            try:
                manifest = json.loads(manifest_bytes)
            except json.JSONDecodeError as exc:
                raise NativePackVerificationError("native pack manifest is invalid JSON") from exc
            if _canonical_json(manifest) != manifest_bytes:
                raise NativePackVerificationError("native pack manifest is not canonical JSON")
            identity = _validated_manifest(
                manifest,
                expected_component=expected_component,
                expected_product_version=expected_product_version,
                expected_compatible_core=expected_compatible_core,
                expected_signing_key_id=expected_signing_key_id,
            )
            expected_entries = {
                PACK_MANIFEST_NAME,
                PACK_SIGNATURE_NAME,
                *(PACK_PAYLOAD_PREFIX + entry["path"] for entry in identity["files"]),
            }
            actual_entries = set(info_by_name)
            unexpected = sorted(actual_entries - expected_entries)
            missing = sorted(expected_entries - actual_entries)
            if unexpected:
                raise NativePackVerificationError(
                    f"native pack contains unexpected entries: {', '.join(unexpected)}"
                )
            if missing:
                raise NativePackVerificationError(
                    f"native pack is missing entries: {', '.join(missing)}"
                )

            observed_total = 0
            for entry in identity["files"]:
                archive_name = PACK_PAYLOAD_PREFIX + entry["path"]
                info = info_by_name[archive_name]
                if info.compress_type != zipfile.ZIP_STORED:
                    raise NativePackVerificationError(
                        f"native pack entry must use stored encoding: {entry['path']}"
                    )
                if info.file_size != entry["bytes"]:
                    raise NativePackVerificationError(
                        f"native pack entry size mismatch: {entry['path']}"
                    )
                digest = hashlib.sha256()
                observed_size = 0
                with archive.open(info, mode="r") as payload:
                    while chunk := payload.read(1024 * 1024):
                        observed_size += len(chunk)
                        digest.update(chunk)
                if observed_size != entry["bytes"]:
                    raise NativePackVerificationError(
                        f"native pack entry size mismatch: {entry['path']}"
                    )
                if digest.hexdigest() != entry["sha256"]:
                    raise NativePackVerificationError(
                        f"native pack entry SHA-256 mismatch: {entry['path']}"
                    )
                observed_total += observed_size
            if observed_total != identity["total_bytes"]:
                raise NativePackVerificationError(
                    "native pack payload total does not match its manifest"
                )
    except zipfile.BadZipFile as exc:
        raise NativePackVerificationError("native pack is not a valid ZIP64 archive") from exc

    return NativePackResult(
        path=pack,
        sha256=_file_size_sha256(pack)[1],
        component=identity["component"],
        product_version=identity["product_version"],
        compatible_core=identity["compatible_core"],
        signing_key_id=identity["signing_key_id"],
        file_count=identity["file_count"],
        total_bytes=identity["total_bytes"],
        metadata=identity["metadata"],
        payload_tree_sha256=payload_tree_sha256(identity["files"]),
    )


def _validated_manifest(
    manifest: object,
    *,
    expected_component: str | None,
    expected_product_version: str | None,
    expected_compatible_core: str | None,
    expected_signing_key_id: str | None,
) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise NativePackVerificationError("native pack manifest root must be an object")
    if set(manifest) != _MANIFEST_FIELDS:
        raise NativePackVerificationError("native pack manifest fields are invalid")
    if manifest.get("schema_version") != PACK_SCHEMA_VERSION:
        raise NativePackVerificationError("native pack schema version is unsupported")
    if manifest.get("product") != PACK_PRODUCT:
        raise NativePackVerificationError("native pack product identity is invalid")
    for field in (
        "component",
        "product_version",
        "compatible_core",
        "signing_key_id",
    ):
        value = manifest.get(field)
        if not isinstance(value, str) or not value or value.strip() != value:
            raise NativePackVerificationError(f"native pack manifest field is invalid: {field}")
    if expected_component is not None and manifest["component"] != expected_component:
        raise NativePackVerificationError(
            "native pack component identity does not match the required component"
        )
    if (
        expected_product_version is not None
        and manifest["product_version"] != expected_product_version
    ):
        raise NativePackVerificationError(
            "native pack product version does not match the required version"
        )
    if (
        expected_compatible_core is not None
        and manifest["compatible_core"] != expected_compatible_core
    ):
        raise NativePackVerificationError(
            "native pack compatible core does not match the required core"
        )
    if (
        expected_signing_key_id is not None
        and manifest["signing_key_id"] != expected_signing_key_id
    ):
        raise NativePackVerificationError(
            "native pack signing key id does not match the embedded trust root"
        )
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise NativePackVerificationError("native pack manifest has no files")
    validated_files: list[dict[str, Any]] = []
    casefold_paths: set[str] = set()
    calculated_total = 0
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"bytes", "path", "sha256"}:
            raise NativePackVerificationError("native pack file entry is malformed")
        path = _safe_relative_path(entry.get("path"))
        folded = path.casefold()
        if folded in casefold_paths:
            raise NativePackVerificationError(
                f"native pack manifest contains a duplicate path: {path}"
            )
        casefold_paths.add(folded)
        size = entry.get("bytes")
        digest = entry.get("sha256")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise NativePackVerificationError(f"native pack file size is invalid: {path}")
        if not (
            isinstance(digest, str)
            and len(digest) == 64
            and all(character in "0123456789abcdef" for character in digest)
        ):
            raise NativePackVerificationError(f"native pack file SHA-256 is invalid: {path}")
        calculated_total += size
        validated_files.append({"bytes": size, "path": path, "sha256": digest})
    file_count = manifest.get("file_count")
    if (
        not isinstance(file_count, int)
        or isinstance(file_count, bool)
        or file_count != len(validated_files)
    ):
        raise NativePackVerificationError("native pack file count is invalid")
    total_bytes = manifest.get("total_bytes")
    if (
        not isinstance(total_bytes, int)
        or isinstance(total_bytes, bool)
        or total_bytes != calculated_total
    ):
        raise NativePackVerificationError("native pack total byte count is invalid")
    metadata = manifest.get("metadata")
    if not isinstance(metadata, dict):
        raise NativePackVerificationError("native pack metadata must be an object")
    identity = {
        "component": manifest["component"],
        "product_version": manifest["product_version"],
        "compatible_core": manifest["compatible_core"],
        "signing_key_id": manifest["signing_key_id"],
        "file_count": len(validated_files),
        "total_bytes": calculated_total,
        "files": validated_files,
        "metadata": metadata,
    }
    _validate_component_contract(identity)
    return identity


def _validate_component_contract(manifest: dict[str, Any]) -> None:
    component = manifest["component"]
    if component in SOURCE_BOUND_COMPONENTS:
        source_sha = manifest["metadata"].get("source_sha")
        if not (
            isinstance(source_sha, str)
            and len(source_sha) == 40
            and all(character in "0123456789abcdef" for character in source_sha)
        ):
            raise NativePackVerificationError(
                f"{component} pack metadata source SHA is missing or invalid"
            )
        if (
            component == "native-app-payload"
            and manifest["metadata"].get("civiccast_source_head") != source_sha
        ):
            raise NativePackVerificationError(
                "native-app-payload source SHA does not match civiccast_source_head"
            )
    if component == CAPTION_COMPONENT:
        # The activation self-test fixture is pack-wide (not tied to any one
        # tier's model directory), so it is checked before per-tier
        # inventories -- and before requiring the newer ``caption_tiers``
        # metadata key, so a pack built under the pre-adaptive-tier contract
        # (missing that key entirely) still fails on the same "large-v3"
        # self-test defect it always did, rather than a confusing new error.
        _require_pinned_caption_file(
            manifest,
            CAPTION_SELF_TEST_PATH,
            CAPTION_SELF_TEST_BYTES,
            CAPTION_SELF_TEST_SHA256,
        )
        present_tier_ids = manifest["metadata"].get("caption_tiers")
        if not isinstance(present_tier_ids, list) or not present_tier_ids:
            raise NativePackVerificationError(
                "caption pack metadata is missing its per-tier inventory declaration"
            )
        # Self-consistency check: whatever tiers this pack CLAIMS to carry
        # must be structurally complete and correct against THEIR OWN
        # recorded inventories (never large-v3's, regardless of which tier
        # is being checked -- the defect this fixes). Which tiers a given
        # consumer REQUIRES is a separate, caller-supplied question --
        # see verify_caption_pack_tiers's ``required_tier_ids`` parameter.
        verify_caption_pack_tiers(manifest, required_tier_ids=present_tier_ids)
        for key, expected in CAPTION_PACK_CONTRACT.items():
            if manifest["metadata"].get(key) != expected:
                raise NativePackVerificationError(
                    f"mandatory large-v3 caption pack metadata mismatch: {key}"
                )
    if component == WHISTLE_COMPONENT:
        if manifest["metadata"] != WHISTLE_PACK_CONTRACT:
            raise NativePackVerificationError(
                "Whistle caption pack metadata does not match its reviewed contract"
            )
        observed = {entry["path"]: entry for entry in manifest["files"]}
        expected_names = set(WHISTLE_PACK_FILES)
        if set(observed) != expected_names:
            missing = sorted(expected_names - set(observed))
            extra = sorted(set(observed) - expected_names)
            detail = []
            if missing:
                detail.append("missing " + ", ".join(missing))
            if extra:
                detail.append("unexpected " + ", ".join(extra))
            raise NativePackVerificationError(
                "Whistle caption pack payload inventory mismatch: " + "; ".join(detail)
            )
        for path, (expected_bytes, expected_sha256) in WHISTLE_PACK_FILES.items():
            item = observed[path]
            if item["bytes"] != expected_bytes or item["sha256"] != expected_sha256:
                raise NativePackVerificationError(
                    f"Whistle caption pack substituted unapproved bytes for {path}"
                )
    if component in OLLAMA_MODEL_COMPONENTS:
        _validate_ollama_model_contract(manifest)


def _require_pinned_caption_file(
    manifest: dict[str, Any],
    path: str,
    expected_bytes: int,
    expected_sha256: str,
) -> None:
    item = next((entry for entry in manifest["files"] if entry["path"] == path), None)
    if item is None:
        raise NativePackVerificationError(f"mandatory large-v3 caption pack is missing {path}")
    if item["bytes"] != expected_bytes or item["sha256"] != expected_sha256:
        raise NativePackVerificationError(
            f"mandatory large-v3 caption pack substituted unapproved bytes for {path}"
        )


def verify_caption_pack_tiers(
    manifest: dict[str, Any],
    *,
    required_tier_ids: Sequence[str],
) -> dict[str, CaptionTierSpec]:
    """Verify every REQUIRED caption tier against ITS OWN recorded inventory.

    This is the fix for the R7-tester-surfaced defect: the old verifier
    hard-coded large-v3's file inventory as THE required inventory for the
    whole ``captions-large-v3`` component, so any other tier -- with its own
    legitimately different file set -- structurally failed, and a tier's
    files could be silently checked against another tier's hashes instead of
    its own.

    Fails closed (raises :class:`NativePackVerificationError`) on:

    * a required tier absent from the pack's declared ``caption_tiers``;
    * a declared tier that is unknown, or known but not yet owner-bound
      (:data:`civiccast.native.caption_tiers.CAPTION_TIER_REGISTRY`'s
      pending floor placeholder, before it is bound);
    * extra files under a tier's ``models/<model_directory>/`` payload
      prefix beyond what that tier's OWN inventory declares;
    * a tier's OWN recorded inventory naming a file the pack does not
      contain;
    * any size/SHA-256 mismatch -- including bytes legitimately belonging to
      a DIFFERENT tier being present under this tier's path (cross-tier file
      borrowing).

    ``required_tier_ids`` is supplied by the caller -- pack self-verification
    passes whatever the pack itself declares present (a self-consistency
    check); a consumer (release build, provisioning, runtime) passes its OWN
    required set. The required set is never assumed inside this function.
    """

    if manifest.get("component") != CAPTION_COMPONENT:
        raise NativePackVerificationError(
            "caption tier verification requires the captions-large-v3 component"
        )
    metadata = manifest.get("metadata")
    if not isinstance(metadata, dict):
        raise NativePackVerificationError("native pack metadata must be an object")
    present_tier_ids = metadata.get("caption_tiers")
    if not isinstance(present_tier_ids, list) or not present_tier_ids:
        raise NativePackVerificationError(
            "caption pack metadata is missing its per-tier inventory declaration"
        )
    missing_required = sorted(set(required_tier_ids) - set(present_tier_ids))
    if missing_required:
        raise NativePackVerificationError(
            f"caption pack is missing required tier(s): {', '.join(missing_required)}"
        )

    files_by_path = {entry["path"]: entry for entry in manifest.get("files", [])}
    verified: dict[str, CaptionTierSpec] = {}
    # Every DECLARED-present tier is verified, not only the required ones:
    # an unrequested tier the pack claims to carry is exactly as capable of
    # smuggling unreviewed bytes as a required one would be, so accepting it
    # unchecked would reopen the same hole this fix closes.
    for tier_id in present_tier_ids:
        if not isinstance(tier_id, str) or tier_id not in CAPTION_TIER_REGISTRY:
            raise NativePackVerificationError(f"caption pack declares an unknown tier: {tier_id!r}")
        try:
            spec = CAPTION_TIER_REGISTRY[tier_id].require_bound()
        except CaptionTierBindingError as exc:
            raise NativePackVerificationError(
                f"caption pack tier {tier_id!r} is not yet bound: {exc}"
            ) from exc

        prefix = f"models/{spec.model_directory}/"
        observed = {
            path[len(prefix) :]: entry
            for path, entry in files_by_path.items()
            if path.startswith(prefix)
        }
        expected_names = set(spec.files)
        observed_names = set(observed)
        extra = sorted(observed_names - expected_names)
        if extra:
            raise NativePackVerificationError(
                f"caption pack tier {tier_id!r} contains unexpected files: {', '.join(extra)}"
            )
        missing = sorted(expected_names - observed_names)
        if missing:
            raise NativePackVerificationError(
                f"caption pack tier {tier_id!r} is missing declared files: {', '.join(missing)}"
            )
        for name, (expected_bytes, expected_sha256) in spec.files.items():
            entry = observed[name]
            if entry["bytes"] != expected_bytes or entry["sha256"] != expected_sha256:
                raise NativePackVerificationError(
                    f"caption pack tier {tier_id!r} substituted unapproved bytes for {name}"
                )
        verified[tier_id] = spec
    return verified


def _validate_ollama_model_contract(manifest: dict[str, Any]) -> None:
    lock = _load_reviewed_ollama_model_lock()
    metadata = manifest["metadata"]
    model_name = metadata.get("model_name")
    if not isinstance(model_name, str):
        raise NativePackVerificationError("native model pack is missing model_name metadata")
    reviewed = lock["models"].get(model_name)
    if not isinstance(reviewed, dict):
        raise NativePackVerificationError(
            f"native model pack is not present in the reviewed model lock: {model_name}"
        )
    if (
        reviewed["component"] != manifest["component"]
        or metadata.get("manifest_sha256") != reviewed["manifest_sha256"]
        or metadata.get("ollama_runtime_version") != lock["ollama_runtime_version"]
    ):
        raise NativePackVerificationError(
            f"native model pack metadata differs from the reviewed model lock: {model_name}"
        )

    files = {entry["path"]: entry for entry in manifest["files"]}
    expected_paths = {"MODEL-PROVENANCE.json"}
    manifest_path = (
        f"manifests/{lock['registry']}/library/{reviewed['repository']}/{reviewed['tag']}"
    )
    _require_reviewed_model_file(
        files,
        manifest_path,
        reviewed["manifest_bytes"],
        reviewed["manifest_sha256"],
    )
    expected_paths.add(manifest_path)
    config_path = f"blobs/sha256-{reviewed['config']['sha256']}"
    _require_reviewed_model_file(
        files,
        config_path,
        reviewed["config"]["bytes"],
        reviewed["config"]["sha256"],
    )
    expected_paths.add(config_path)
    for layer in reviewed["layers"]:
        path = f"blobs/sha256-{layer['sha256']}"
        _require_reviewed_model_file(
            files,
            path,
            layer["bytes"],
            layer["sha256"],
        )
        expected_paths.add(path)
    if set(files) != expected_paths:
        raise NativePackVerificationError(
            f"native model pack inventory differs from the reviewed model lock: {model_name}"
        )
    provenance = files.get("MODEL-PROVENANCE.json")
    if provenance is None or provenance["bytes"] <= 0 or not _is_lower_sha256(provenance["sha256"]):
        raise NativePackVerificationError("native model pack provenance identity is invalid")


def _require_reviewed_model_file(
    files: dict[str, dict[str, Any]],
    path: str,
    expected_bytes: int,
    expected_sha256: str,
) -> None:
    item = files.get(path)
    if item is None:
        raise NativePackVerificationError(
            f"native model pack is missing reviewed model lock file: {path}"
        )
    if item["bytes"] != expected_bytes or item["sha256"] != expected_sha256:
        raise NativePackVerificationError(
            f"native model pack substituted bytes outside the reviewed model lock: {path}"
        )


def _load_reviewed_ollama_model_lock() -> dict[str, Any]:
    try:
        parsed = json.loads(OLLAMA_MODEL_LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NativePackVerificationError(f"reviewed model lock is unreadable: {exc}") from exc
    if (
        not isinstance(parsed, dict)
        or set(parsed) != {"schema_version", "registry", "ollama_runtime_version", "models"}
        or parsed.get("schema_version") != 1
        or parsed.get("registry") != "registry.ollama.ai"
        or parsed.get("ollama_runtime_version") != "0.30.6"
        or not isinstance(parsed.get("models"), dict)
    ):
        raise NativePackVerificationError("reviewed model lock identity is invalid")
    models = parsed["models"]
    if {
        model.get("component") for model in models.values() if isinstance(model, dict)
    } != OLLAMA_MODEL_COMPONENTS:
        raise NativePackVerificationError("reviewed model lock has an incomplete component set")
    allowed_media_types = {
        "application/vnd.ollama.image.model",
        "application/vnd.ollama.image.projector",
        "application/vnd.ollama.image.license",
        "application/vnd.ollama.image.params",
        "application/vnd.ollama.image.template",
    }
    for name, model in models.items():
        if (
            not isinstance(name, str)
            or not isinstance(model, dict)
            or set(model)
            != {
                "component",
                "config",
                "layers",
                "manifest_bytes",
                "manifest_sha256",
                "repository",
                "tag",
            }
            or not isinstance(model["component"], str)
            or not isinstance(model["repository"], str)
            or not isinstance(model["tag"], str)
            or not _valid_locked_blob(model["config"])
            or not isinstance(model["layers"], list)
            or not model["layers"]
            or not all(
                isinstance(layer, dict)
                and set(layer) == {"bytes", "media_type", "sha256"}
                and layer.get("media_type") in allowed_media_types
                and _valid_locked_blob(layer)
                for layer in model["layers"]
            )
            or not isinstance(model["manifest_bytes"], int)
            or isinstance(model["manifest_bytes"], bool)
            or model["manifest_bytes"] <= 0
            or not _is_lower_sha256(model["manifest_sha256"])
        ):
            raise NativePackVerificationError(f"reviewed model lock entry is invalid: {name}")
    return parsed


def _valid_locked_blob(value: object) -> bool:
    return (
        isinstance(value, dict)
        and {"bytes", "sha256"}.issubset(value)
        and isinstance(value.get("bytes"), int)
        and not isinstance(value.get("bytes"), bool)
        and value["bytes"] > 0
        and _is_lower_sha256(value.get("sha256"))
    )


def _is_lower_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _safe_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise NativePackVerificationError("native pack path must be a non-empty string")
    if (
        not value.isascii()
        or "\x00" in value
        or "\\" in value
        or ":" in value
        or value.startswith("/")
    ):
        raise NativePackVerificationError(f"unsafe native pack path: {value!r}")
    path = PurePosixPath(value)
    if any(part in ("", ".", "..") for part in path.parts) or path.as_posix() != value:
        raise NativePackVerificationError(f"unsafe native pack path: {value!r}")
    for part in path.parts:
        if (
            part.endswith((" ", "."))
            or any(ord(character) < 32 for character in part)
            or any(character in _WINDOWS_FORBIDDEN_CHARS for character in part)
            or part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES
        ):
            raise NativePackVerificationError(f"unsafe native pack path: {value!r}")
    return value


def _safe_archive_path(value: str) -> str:
    if value in (PACK_MANIFEST_NAME, PACK_SIGNATURE_NAME):
        return value
    if not value.startswith(PACK_PAYLOAD_PREFIX):
        raise NativePackVerificationError(f"unsafe native pack archive path: {value!r}")
    relative = value.removeprefix(PACK_PAYLOAD_PREFIX)
    return PACK_PAYLOAD_PREFIX + _safe_relative_path(relative)


def _require_regular_source(path: Path) -> None:
    try:
        details = path.lstat()
    except OSError as exc:
        raise ValueError(f"native pack source is unreadable: {path}") from exc
    attributes = int(getattr(details, "st_file_attributes", 0))
    if not stat.S_ISREG(details.st_mode) or path.is_symlink() or attributes & _REPARSE_POINT:
        raise ValueError(f"native pack source must be a regular non-reparse file: {path}")


def _file_size_sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename=name, date_time=_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 0
    info.external_attr = 0
    return info
