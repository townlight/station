# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Signed distribution indexes for the small native Windows bootstrap.

The bootstrap executable intentionally does not contain the multi-gigabyte
station payload.  This module defines the build-side/reference verifier for
the signed index that makes those sidecar bytes an exact, fail-closed set.
The Rust bootstrap implements the same format before it downloads or imports
any component.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

DISTRIBUTION_SCHEMA_VERSION = 1
DISTRIBUTION_PRODUCT = "civiccast-native"
DISTRIBUTION_KINDS = frozenset({"channel-index", "station-index"})
REQUIRED_COMPONENTS = (
    "core",
    "captions-large-v3",
    "summary-gemma4-12b",
    "summary-gemma4-e4b",
    "translation-translategemma-4b",
)
STATION_REQUIRED_COMPONENTS = (
    "core",
    "captions-floor",
    "summary-gemma4-12b",
    "summary-gemma4-e4b",
    "translation-translategemma-4b",
    "captions-whistle",
)
_MAX_INDEX_BYTES = 4 * 1024 * 1024
_ENVELOPE_FIELDS = {"manifest", "signature"}
_MANIFEST_FIELDS = {
    "schema_version",
    "product",
    "kind",
    "channel",
    "product_version",
    "compatible_core",
    "signing_key_id",
    "created_epoch",
    "packs",
}
_PACK_FIELDS = {
    "component",
    "filename",
    "bytes",
    "sha256",
    "required",
    "urls",
}
_COMPONENT_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\Z")
_LOWER_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_WINDOWS_RESERVED_NAMES = {
    "aux",
    "clock$",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
_WINDOWS_FORBIDDEN_CHARS = frozenset('<>:"/\\|?*')
_REPARSE_POINT = 0x400


class NativeDistributionError(ValueError):
    """Raised before acquisition when a distribution index is not trustworthy."""


@dataclass(frozen=True)
class DistributionPack:
    """Outer byte identity and source locations for one signed component pack."""

    component: str
    filename: str
    bytes: int
    sha256: str
    required: bool
    urls: tuple[str, ...]


@dataclass(frozen=True)
class DistributionIndex:
    """Verified identity for an online channel index or offline station index."""

    path: Path
    sha256: str
    kind: str
    channel: str
    product_version: str
    compatible_core: str
    signing_key_id: str
    created_epoch: int
    packs: tuple[DistributionPack, ...]


def canonical_json(value: object) -> bytes:
    """Return the one byte representation covered by Ed25519 signatures."""

    return (
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def build_distribution_index(
    *,
    output: Path,
    kind: str,
    channel: str,
    product_version: str,
    compatible_core: str,
    packs: Mapping[str, Path],
    urls: Mapping[str, Sequence[str]],
    signing_private_key: Ed25519PrivateKey,
    signing_key_id: str,
    created_epoch: int,
    required_components: Sequence[str] | None = None,
) -> DistributionIndex:
    """Build and self-verify a deterministic signed distribution index."""

    output = output.expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"native distribution index already exists: {output}")
    _require_identity("kind", kind)
    if kind not in DISTRIBUTION_KINDS:
        raise NativeDistributionError(f"unsupported native distribution kind: {kind}")
    for field, value in (
        ("channel", channel),
        ("product_version", product_version),
        ("compatible_core", compatible_core),
        ("signing_key_id", signing_key_id),
    ):
        _require_identity(field, value)
    if not isinstance(created_epoch, int) or isinstance(created_epoch, bool) or created_epoch < 0:
        raise NativeDistributionError("native distribution created_epoch is invalid")
    if not isinstance(packs, Mapping) or not packs:
        raise NativeDistributionError("native distribution has no component packs")
    required_components, required_component_set = _required_component_policy(
        kind, required_components
    )
    if set(packs) < required_component_set:
        missing = sorted(required_component_set - set(packs))
        raise NativeDistributionError(
            "native distribution required component set is incomplete: " + ", ".join(missing)
        )
    if set(urls) != set(packs):
        raise NativeDistributionError(
            "native distribution URL mapping must exactly match its component packs"
        )

    entries: list[dict[str, object]] = []
    seen_filenames: set[str] = set()
    for raw_component in sorted(
        packs, key=lambda component: _component_sort_key(component, required_components)
    ):
        component = _require_component(raw_component)
        pack = Path(packs[component]).expanduser()
        _require_regular_file(pack, label=f"{component} component pack")
        pack = pack.resolve(strict=True)
        filename = _safe_pack_filename(pack.name)
        folded = filename.casefold()
        if folded in seen_filenames:
            raise NativeDistributionError(
                f"native distribution contains a duplicate pack filename: {filename}"
            )
        seen_filenames.add(folded)
        size, digest = _file_size_sha256(pack)
        if size <= 0:
            raise NativeDistributionError(f"native component pack must not be empty: {filename}")
        locations = _validate_urls(kind, urls[component], component=component)
        entries.append(
            {
                "component": component,
                "filename": filename,
                "bytes": size,
                "sha256": digest,
                "required": component in required_component_set,
                "urls": locations,
            }
        )

    manifest: dict[str, object] = {
        "schema_version": DISTRIBUTION_SCHEMA_VERSION,
        "product": DISTRIBUTION_PRODUCT,
        "kind": kind,
        "channel": channel,
        "product_version": product_version,
        "compatible_core": compatible_core,
        "signing_key_id": signing_key_id,
        "created_epoch": created_epoch,
        "packs": entries,
    }
    signature = signing_private_key.sign(canonical_json(manifest))
    envelope = {
        "manifest": manifest,
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    payload = canonical_json(envelope)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="xb",
            prefix=f".{output.name}.",
            suffix=".partial",
            dir=output.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if output.exists():
            raise FileExistsError(f"native distribution index already exists: {output}")
        temporary.replace(output)
        temporary = None
        return verify_distribution_index(
            output,
            public_key=signing_private_key.public_key(),
            expected_kind=kind,
            expected_channel=channel,
            expected_product_version=product_version,
            expected_compatible_core=compatible_core,
            expected_signing_key_id=signing_key_id,
            required_components=required_components,
        )
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def verify_distribution_index(
    index_path: Path,
    *,
    public_key: Ed25519PublicKey,
    expected_kind: str | None = None,
    expected_channel: str | None = None,
    expected_product_version: str | None = None,
    expected_compatible_core: str | None = None,
    expected_signing_key_id: str | None = None,
    required_components: Sequence[str] | None = None,
) -> DistributionIndex:
    """Verify the signed index, all identities, and the complete pack set."""

    index_path = Path(index_path).expanduser()
    _require_regular_file(index_path, label="native distribution index")
    index_path = index_path.resolve(strict=True)
    size = index_path.stat().st_size
    if size > _MAX_INDEX_BYTES:
        raise NativeDistributionError("native distribution index is too large")
    raw = index_path.read_bytes()
    try:
        envelope: Any = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NativeDistributionError("native distribution index is invalid JSON") from exc
    if canonical_json(envelope) != raw:
        raise NativeDistributionError("native distribution index is not canonical JSON")
    if not isinstance(envelope, dict) or set(envelope) != _ENVELOPE_FIELDS:
        raise NativeDistributionError("native distribution envelope fields are invalid")
    manifest = envelope.get("manifest")
    signature_text = envelope.get("signature")
    if not isinstance(signature_text, str):
        raise NativeDistributionError("native distribution signature is invalid")
    try:
        signature = base64.b64decode(signature_text, validate=True)
        public_key.verify(signature, canonical_json(manifest))
    except (ValueError, InvalidSignature) as exc:
        raise NativeDistributionError("native distribution index signature is invalid") from exc

    identity = _validate_manifest(
        manifest,
        expected_kind=expected_kind,
        expected_channel=expected_channel,
        expected_product_version=expected_product_version,
        expected_compatible_core=expected_compatible_core,
        expected_signing_key_id=expected_signing_key_id,
        required_components=required_components,
    )
    return DistributionIndex(
        path=index_path,
        sha256=hashlib.sha256(raw).hexdigest(),
        kind=identity["kind"],
        channel=identity["channel"],
        product_version=identity["product_version"],
        compatible_core=identity["compatible_core"],
        signing_key_id=identity["signing_key_id"],
        created_epoch=identity["created_epoch"],
        packs=tuple(identity["packs"]),
    )


def verify_station_media(
    station_index: Path,
    *,
    public_key: Ed25519PublicKey,
    expected_channel: str | None = None,
    expected_product_version: str | None = None,
    expected_compatible_core: str | None = None,
    expected_signing_key_id: str | None = None,
    required_components: Sequence[str] = STATION_REQUIRED_COMPONENTS,
) -> DistributionIndex:
    """Verify an air-gapped index and every adjacent outer pack byte."""

    result = verify_distribution_index(
        station_index,
        public_key=public_key,
        expected_kind="station-index",
        expected_channel=expected_channel,
        expected_product_version=expected_product_version,
        expected_compatible_core=expected_compatible_core,
        expected_signing_key_id=expected_signing_key_id,
        required_components=required_components,
    )
    root = result.path.parent.resolve(strict=True)
    for pack in result.packs:
        candidate = root / pack.filename
        _require_regular_file(candidate, label=f"{pack.component} station pack")
        resolved = candidate.resolve(strict=True)
        if resolved.parent != root:
            raise NativeDistributionError(
                f"native station pack escapes its media directory: {pack.filename}"
            )
        size, digest = _file_size_sha256(resolved)
        if size != pack.bytes:
            raise NativeDistributionError(f"native station pack size mismatch: {pack.filename}")
        if digest != pack.sha256:
            raise NativeDistributionError(f"native station pack SHA-256 mismatch: {pack.filename}")
    return result


def _validate_manifest(
    manifest: object,
    *,
    expected_kind: str | None,
    expected_channel: str | None,
    expected_product_version: str | None,
    expected_compatible_core: str | None,
    expected_signing_key_id: str | None,
    required_components: Sequence[str] | None,
) -> dict[str, Any]:
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS:
        raise NativeDistributionError("native distribution manifest fields are invalid")
    if manifest.get("schema_version") != DISTRIBUTION_SCHEMA_VERSION:
        raise NativeDistributionError("native distribution schema version is unsupported")
    if manifest.get("product") != DISTRIBUTION_PRODUCT:
        raise NativeDistributionError("native distribution product identity is invalid")
    for field in (
        "kind",
        "channel",
        "product_version",
        "compatible_core",
        "signing_key_id",
    ):
        _require_identity(field, manifest.get(field))
    kind = manifest["kind"]
    if kind not in DISTRIBUTION_KINDS:
        raise NativeDistributionError("native distribution kind is invalid")
    required_components, required_component_set = _required_component_policy(
        kind, required_components
    )
    for field, expected in (
        ("kind", expected_kind),
        ("channel", expected_channel),
        ("product_version", expected_product_version),
        ("compatible_core", expected_compatible_core),
        ("signing_key_id", expected_signing_key_id),
    ):
        if expected is not None and manifest[field] != expected:
            label = field.replace("_", " ")
            raise NativeDistributionError(
                f"native distribution {label} does not match the bootstrap"
            )
    created_epoch = manifest.get("created_epoch")
    if not isinstance(created_epoch, int) or isinstance(created_epoch, bool) or created_epoch < 0:
        raise NativeDistributionError("native distribution created_epoch is invalid")
    raw_packs = manifest.get("packs")
    if not isinstance(raw_packs, list) or not raw_packs:
        raise NativeDistributionError("native distribution has no component packs")

    packs: list[DistributionPack] = []
    seen_components: set[str] = set()
    seen_filenames: set[str] = set()
    for raw_pack in raw_packs:
        if not isinstance(raw_pack, dict) or set(raw_pack) != _PACK_FIELDS:
            raise NativeDistributionError("native distribution component entry is malformed")
        component = _require_component(raw_pack.get("component"))
        if component in seen_components:
            raise NativeDistributionError(
                f"native distribution contains duplicate component: {component}"
            )
        seen_components.add(component)
        filename = _safe_pack_filename(raw_pack.get("filename"))
        folded = filename.casefold()
        if folded in seen_filenames:
            raise NativeDistributionError(
                f"native distribution contains duplicate pack filename: {filename}"
            )
        seen_filenames.add(folded)
        byte_count = raw_pack.get("bytes")
        if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count <= 0:
            raise NativeDistributionError(
                f"native distribution pack byte count is invalid: {component}"
            )
        digest = raw_pack.get("sha256")
        if not isinstance(digest, str) or not _LOWER_SHA256_RE.fullmatch(digest):
            raise NativeDistributionError(
                f"native distribution pack SHA-256 is invalid: {component}"
            )
        required = raw_pack.get("required")
        if not isinstance(required, bool):
            raise NativeDistributionError(
                f"native distribution required flag is invalid: {component}"
            )
        if component in required_component_set and not required:
            raise NativeDistributionError(
                f"native distribution component must be required: {component}"
            )
        locations = tuple(_validate_urls(kind, raw_pack.get("urls"), component=component))
        packs.append(
            DistributionPack(
                component=component,
                filename=filename,
                bytes=byte_count,
                sha256=digest,
                required=required,
                urls=locations,
            )
        )

    missing = required_component_set - seen_components
    if missing:
        raise NativeDistributionError(
            "native distribution required component set is incomplete: "
            + ", ".join(sorted(missing))
        )
    expected_order = sorted(
        seen_components, key=lambda component: _component_sort_key(component, required_components)
    )
    if [pack.component for pack in packs] != expected_order:
        raise NativeDistributionError(
            "native distribution component entries are not in canonical order"
        )
    return {
        "kind": kind,
        "channel": manifest["channel"],
        "product_version": manifest["product_version"],
        "compatible_core": manifest["compatible_core"],
        "signing_key_id": manifest["signing_key_id"],
        "created_epoch": created_epoch,
        "packs": packs,
    }


def _validate_urls(
    kind: str,
    values: object,
    *,
    component: str,
) -> list[str]:
    if not isinstance(values, (list, tuple)) or any(not isinstance(value, str) for value in values):
        raise NativeDistributionError(f"native distribution pack URLs are invalid: {component}")
    locations = list(values)
    if kind == "station-index":
        if locations:
            raise NativeDistributionError("native station index must not contain network locations")
        return []
    if not locations:
        raise NativeDistributionError(
            f"native online index requires an HTTPS location: {component}"
        )
    if len(set(locations)) != len(locations):
        raise NativeDistributionError(
            f"native online index contains duplicate HTTPS locations: {component}"
        )
    for location in locations:
        try:
            parsed = urlsplit(location)
            port = parsed.port
        except ValueError as exc:
            raise NativeDistributionError(
                f"native online index contains an invalid HTTPS location: {component}"
            ) from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.path in {"", "/"}
            or (port is not None and not (1 <= port <= 65535))
        ):
            raise NativeDistributionError(
                f"native online index requires unambiguous HTTPS locations: {component}"
            )
    return locations


def _require_identity(field: str, value: object) -> None:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise NativeDistributionError(f"native distribution identity field is invalid: {field}")
    if any(ord(character) < 0x20 or ord(character) > 0x7E for character in value):
        raise NativeDistributionError(
            f"native distribution identity field is not portable ASCII: {field}"
        )


def _require_component(component: object) -> str:
    if not isinstance(component, str) or not _COMPONENT_RE.fullmatch(component):
        raise NativeDistributionError(
            f"native distribution component identity is invalid: {component!r}"
        )
    return component


def _required_component_policy(
    kind: str,
    required_components: Sequence[str] | None,
) -> tuple[tuple[str, ...], frozenset[str]]:
    selected = tuple(
        required_components
        if required_components is not None
        else (STATION_REQUIRED_COMPONENTS if kind == "station-index" else REQUIRED_COMPONENTS)
    )
    if not selected or len(set(selected)) != len(selected):
        raise NativeDistributionError("native distribution required component policy is invalid")
    for component in selected:
        _require_component(component)
    return selected, frozenset(selected)


def _component_sort_key(
    component: str,
    required_components: Sequence[str] = REQUIRED_COMPONENTS,
) -> tuple[int, int | str]:
    try:
        return (0, required_components.index(component))
    except ValueError:
        return (1, component)


def _safe_pack_filename(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise NativeDistributionError("native distribution pack filename is invalid")
    if value.strip() != value or value in {".", ".."}:
        raise NativeDistributionError("native distribution pack filename is unsafe")
    if any(
        ord(character) < 0x20 or ord(character) > 0x7E or character in _WINDOWS_FORBIDDEN_CHARS
        for character in value
    ):
        raise NativeDistributionError("native distribution pack filename is unsafe")
    if value.endswith((" ", ".")) or Path(value).name != value:
        raise NativeDistributionError("native distribution pack filename is unsafe")
    stem = value.split(".", 1)[0].casefold()
    if stem in _WINDOWS_RESERVED_NAMES:
        raise NativeDistributionError("native distribution pack filename is unsafe")
    if not value.casefold().endswith(".ccpack"):
        raise NativeDistributionError("native distribution pack filename must end in .ccpack")
    return value


def _require_regular_file(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise NativeDistributionError(f"{label} is missing: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise NativeDistributionError(f"{label} must not be a symbolic link: {path}")
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    if file_attributes & _REPARSE_POINT:
        raise NativeDistributionError(f"{label} must not be a reparse point: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise NativeDistributionError(f"{label} must be a regular file: {path}")


def _file_size_sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    observed = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            observed += len(chunk)
            digest.update(chunk)
    return observed, digest.hexdigest()


__all__ = [
    "DISTRIBUTION_KINDS",
    "DISTRIBUTION_PRODUCT",
    "DISTRIBUTION_SCHEMA_VERSION",
    "REQUIRED_COMPONENTS",
    "STATION_REQUIRED_COMPONENTS",
    "DistributionIndex",
    "DistributionPack",
    "NativeDistributionError",
    "build_distribution_index",
    "canonical_json",
    "verify_distribution_index",
    "verify_station_media",
]
