# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""ActivityPub station key management.

Private keys are file-backed and never serialized through public API models.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey


def generate_activitypub_private_key(path: Path) -> str:
    """Generate a 2048-bit RSA key and return its public key PEM."""

    path.parent.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    _restrict_private_key_file(path)
    return public_key_pem_from_private_key(key)


def load_activitypub_private_key(path: Path) -> RSAPrivateKey:
    """Load the station private key from disk."""

    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, RSAPrivateKey):
        raise ValueError("ActivityPub private key must be an RSA private key.")
    return key


def public_key_pem_from_private_key_path(path: Path) -> str:
    """Return the public key PEM derived from a private key file."""

    return public_key_pem_from_private_key(load_activitypub_private_key(path))


def public_key_pem_from_private_key(key: RSAPrivateKey) -> str:
    """Serialize the public portion of an RSA key."""

    return (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )


def _restrict_private_key_file(path: Path) -> None:
    if os.name == "nt":
        try:
            from civiccast.certs.authority import _restrict_windows_private_key_acl

            _restrict_windows_private_key_acl(path)
        except OSError:
            return
        return
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
