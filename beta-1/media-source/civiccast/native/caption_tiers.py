# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Per-tier caption model identity for the adaptive-tier caption pack (WP1).

The owner ruling (``.agent-runs/native-windows/wp1-caption-integrity/
OWNER-DECISION-caption-adaptive-tier.md``) requires the caption pack system to
carry AT LEAST two tiers: a measured CPU-only **floor** tier that is the
mandatory baseline, and **large-v3** as the quality tier that is only ever
auto-selected when measured hardware capacity allows.

The defect this module fixes: the caption pack verifier used to hard-code
large-v3's exact file inventory (names, sizes, hashes) as THE required
inventory for the whole component, so a floor tier with a legitimately
different file set (v2-family: 80 mel bins, 51865-token vocabulary, vs.
v3-family: 128 mel bins, 51866-token vocabulary) would structurally fail
verification, and -- the defect actually observed -- a tier's files could be
silently checked against ANOTHER tier's hashes.

:data:`CAPTION_TIER_REGISTRY` fixes this at the root: each tier owns its
COMPLETE, PINNED file inventory, captured once from the real upstream
snapshot by :func:`generate_tier_inventory` (never hand-transcribed), and
verification (see ``civiccast.installer.native_packs.verify_caption_pack_tiers``)
checks each tier declared present in a pack against ITS OWN recorded
inventory -- never against another tier's.

The large-v3 entry re-uses the EXISTING pinned identity in
:mod:`civiccast.native.app_payload` (``WHISPER_MODEL_REPO`` /
``WHISPER_MODEL_REVISION`` / ``WHISPER_MODEL_FILES``) verbatim: this module
does not change the shipped large-v3 identity, it only stops that identity
from being wrongly imposed on every other tier.

The floor entry WAS a placeholder, structurally identical to a bound tier,
pending the R7 measurement naming the floor model. That measurement is now
closed: the owner's BINDING ruling (``OWNER-DECISION-caption-adaptive-tier.md``,
2026-07-30) named ``medium`` as the floor tier, at 10-second caption
segments, on the evidence of three pre-registered CPU-only trials on the
replacement R7 (Ryzen 7 8745HS) landing at 18.68-18.76s against a 20.0s
derived deadline. The floor entry below is now bound to the real, pinned
``Systran/faster-whisper-medium`` snapshot -- exactly the way this docstring
said binding it would look: ``model_repository`` / ``model_revision`` /
``model_directory`` / ``files`` filled in, ``pending`` set to ``False``, no
restructure of this module or of ``native_packs.py``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from civiccast.native.app_payload import (
    WHISPER_MODEL_FILES,
    WHISPER_MODEL_REPO,
    WHISPER_MODEL_REVISION,
)

__all__ = [
    "CAPTION_TIER_REGISTRY",
    "FLOOR_TIER_ID",
    "LARGE_V3_TIER_ID",
    "CaptionTierBindingError",
    "CaptionTierSpec",
    "generate_tier_inventory",
]

LARGE_V3_TIER_ID: Final[str] = "large-v3"
FLOOR_TIER_ID: Final[str] = "floor"


class CaptionTierBindingError(ValueError):
    """A pending (not-yet-owner-bound) caption tier was used where a bound,
    complete identity is required."""


@dataclass(frozen=True)
class CaptionTierSpec:
    """One caption model tier's complete, pinned identity.

    ``files`` maps a filename (relative to the tier's payload directory,
    ``models/<model_directory>/``) to ``(bytes, sha256)``. For a real, bound
    tier this is captured once by :func:`generate_tier_inventory` walking the
    pinned upstream snapshot -- never hand-typed -- then pinned here exactly
    the way ``app_payload.WHISPER_MODEL_FILES`` has always been pinned for
    large-v3.

    ``pending=True`` marks a tier whose repository/revision/files are not yet
    decided. The floor tier carried this placeholder from WP1's initial
    per-tier-inventory fix until the owner's 2026-07-30 BINDING ruling named
    ``medium``; no tier in :data:`CAPTION_TIER_REGISTRY` is pending anymore,
    but the shape stays load-bearing for any future tier awaiting its own
    binding.
    """

    tier_id: str
    model_directory: str
    model_repository: str | None
    model_revision: str | None
    files: dict[str, tuple[int, str]]
    pending: bool = False

    def require_bound(self) -> CaptionTierSpec:
        """Return ``self`` if this tier has a complete pinned identity.

        Raises :class:`CaptionTierBindingError` for a pending placeholder
        tier (e.g. the floor tier before the owner binds it) -- fail closed
        rather than silently verifying against an empty inventory.
        """

        if (
            self.pending
            or self.model_repository is None
            or self.model_revision is None
            or not self.files
        ):
            raise CaptionTierBindingError(
                f"caption tier {self.tier_id!r} is not yet bound to a pinned "
                "model identity (owner binding pending)"
            )
        return self


CAPTION_TIER_REGISTRY: Final[dict[str, CaptionTierSpec]] = {
    LARGE_V3_TIER_ID: CaptionTierSpec(
        tier_id=LARGE_V3_TIER_ID,
        model_directory="faster-whisper-large-v3",
        model_repository=WHISPER_MODEL_REPO,
        model_revision=WHISPER_MODEL_REVISION,
        files=dict(WHISPER_MODEL_FILES),
        pending=False,
    ),
    # BOUND 2026-07-30 per OWNER-DECISION-caption-adaptive-tier.md's BINDING
    # section: the floor tier is `medium`. Identity confirmed live against
    # the Hugging Face API for Systran/faster-whisper-medium (`sha` of the
    # `main` ref) and cross-checked against the local snapshot cached under
    # the same commit hash (huggingface_hub resolves the mutable `main` ref
    # to this immutable commit before ever writing a snapshot directory, so
    # the snapshot directory name IS the pin, never the branch name). Files
    # captured via generate_tier_inventory() against that real local
    # snapshot (dereferencing the hub cache's symlinks first, per that
    # function's own symlink refusal) -- never hand-transcribed. Note the
    # medium (v2-family) repository does not publish a preprocessor_config.json
    # and this snapshot did not include the repo's README.md/.gitattributes
    # (documentation/git-metadata, not required for inference) -- a
    # legitimately different file set from large-v3's, exactly as this
    # module's top docstring anticipated.
    FLOOR_TIER_ID: CaptionTierSpec(
        tier_id=FLOOR_TIER_ID,
        model_directory="faster-whisper-medium",
        model_repository="Systran/faster-whisper-medium",
        model_revision="08e178d48790749d25932bbc082711ddcfdfbc4f",
        files={
            "config.json": (
                2257,
                "3622a2ddc41ec0e0fd4e68c13c6830f03b90c38d89aaad184de02c8c642cf807",
            ),
            "model.bin": (
                1527906378,
                "9b45e1009dcc4ab601eff815b61d80e60ce3fd8c74c1a14f4a282258286b51ae",
            ),
            "tokenizer.json": (
                2203239,
                "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab",
            ),
            "vocabulary.txt": (
                459861,
                "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913",
            ),
        },
        pending=False,
    ),
}


def generate_tier_inventory(model_dir: Path) -> dict[str, tuple[int, str]]:
    """Walk a pinned upstream model snapshot and record its exact inventory.

    Returns ``{relative_posix_path: (bytes, sha256)}`` for every regular file
    under ``model_dir``, computed from the bytes actually on disk. This is
    the ONLY sanctioned way to produce a new :class:`CaptionTierSpec`'s
    ``files`` map: never hand-transcribe filenames or hashes into the
    registry. Symlinks are refused (same posture as the pack builder's
    pinned-input validation) so a reparse point can never stand in for a
    real, reviewed file.
    """

    if not model_dir.is_dir():
        raise ValueError(f"caption tier model directory is missing: {model_dir}")
    inventory: dict[str, tuple[int, str]] = {}
    for path in sorted(model_dir.rglob("*")):
        if path.is_dir():
            continue
        if path.is_symlink():
            raise ValueError(f"caption tier model file is a symlink: {path}")
        relative = path.relative_to(model_dir).as_posix()
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
        inventory[relative] = (size, digest.hexdigest())
    if not inventory:
        raise ValueError(f"caption tier model directory has no files: {model_dir}")
    return inventory
