# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""HTTP Signature helpers for ActivityPub server-to-server traffic."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime, parsedate_to_datetime
from urllib.parse import urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey


class HttpSignatureError(ValueError):
    """Raised when an ActivityPub HTTP Signature is missing or invalid."""


@dataclass(frozen=True)
class SignatureParameters:
    key_id: str
    algorithm: str
    headers: tuple[str, ...]
    signature_b64: str


def digest_header(body: bytes) -> str:
    """Return the SHA-256 Digest header value for a request body."""

    return "SHA-256=" + base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")


def parse_signature_header(value: str) -> SignatureParameters:
    """Parse the common Cavage-style ActivityPub Signature header."""

    fields: dict[str, str] = {}
    for raw in value.split(","):
        key, sep, item = raw.strip().partition("=")
        if not sep:
            continue
        fields[key] = item.strip().strip('"')
    key_id = fields.get("keyId")
    signature_b64 = fields.get("signature")
    algorithm = fields.get("algorithm", "rsa-sha256").lower()
    headers = tuple(fields.get("headers", "date").lower().split())
    if not key_id or not signature_b64:
        raise HttpSignatureError("Signature header must include keyId and signature.")
    if algorithm != "rsa-sha256":
        raise HttpSignatureError("Only rsa-sha256 ActivityPub signatures are supported.")
    return SignatureParameters(
        key_id=key_id,
        algorithm=algorithm,
        headers=headers,
        signature_b64=signature_b64,
    )


def verify_http_signature(
    *,
    method: str,
    path_and_query: str,
    headers: Mapping[str, str],
    body: bytes,
    public_key_pem: str,
    require_digest: bool,
    now: datetime | None = None,
    max_age_seconds: int = 300,
) -> SignatureParameters:
    """Verify an inbound ActivityPub HTTP Signature."""

    normalized_headers = {key.lower(): value for key, value in headers.items()}
    signature_header = normalized_headers.get("signature")
    if not signature_header:
        raise HttpSignatureError("Missing ActivityPub Signature header.")
    params = parse_signature_header(signature_header)
    required = {"(request-target)", "host", "date"}
    if require_digest:
        required.add("digest")
    missing = required.difference(params.headers)
    if missing:
        raise HttpSignatureError(
            "ActivityPub Signature is missing required signed headers: "
            + ", ".join(sorted(missing))
        )
    _verify_date(normalized_headers.get("date", ""), now=now, max_age_seconds=max_age_seconds)
    if require_digest:
        _verify_digest(normalized_headers.get("digest", ""), body)
    signed = _signature_base(
        method=method,
        path_and_query=path_and_query,
        headers=normalized_headers,
        signed_headers=params.headers,
    )
    try:
        signature = base64.b64decode(params.signature_b64.encode("ascii"), validate=True)
    except ValueError as exc:
        raise HttpSignatureError("ActivityPub signature is not valid base64.") from exc
    public_key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
    if not isinstance(public_key, RSAPublicKey):
        raise HttpSignatureError("Remote ActivityPub key must be an RSA public key.")
    try:
        public_key.verify(signature, signed.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as exc:
        raise HttpSignatureError("ActivityPub HTTP Signature verification failed.") from exc
    return params


def signed_request_headers(
    *,
    method: str,
    url: str,
    body: bytes,
    private_key: RSAPrivateKey,
    key_id: str,
    now: datetime | None = None,
) -> dict[str, str]:
    """Build signed ActivityPub request headers for outbound delivery."""

    parsed = urlparse(url)
    if not parsed.netloc:
        raise HttpSignatureError("Cannot sign ActivityPub request without a URL host.")
    path_and_query = parsed.path or "/"
    if parsed.query:
        path_and_query += f"?{parsed.query}"
    date_value = format_datetime(now or datetime.now(UTC), usegmt=True)
    headers = {
        "host": parsed.netloc,
        "date": date_value,
        "digest": digest_header(body),
    }
    signed_headers = ("(request-target)", "host", "date", "digest")
    base = _signature_base(
        method=method,
        path_and_query=path_and_query,
        headers=headers,
        signed_headers=signed_headers,
    )
    signature = private_key.sign(base.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    signature_b64 = base64.b64encode(signature).decode("ascii")
    return {
        "Host": parsed.netloc,
        "Date": date_value,
        "Digest": headers["digest"],
        "Signature": (
            f'keyId="{key_id}",algorithm="rsa-sha256",'
            f'headers="{" ".join(signed_headers)}",signature="{signature_b64}"'
        ),
        "Content-Type": "application/activity+json",
        "Accept": "application/activity+json",
    }


def _signature_base(
    *,
    method: str,
    path_and_query: str,
    headers: Mapping[str, str],
    signed_headers: tuple[str, ...],
) -> str:
    lines: list[str] = []
    for header in signed_headers:
        name = header.lower()
        if name == "(request-target)":
            lines.append(f"(request-target): {method.lower()} {path_and_query}")
            continue
        value = headers.get(name)
        if value is None:
            raise HttpSignatureError(f"Signed header {name!r} is missing from the request.")
        lines.append(f"{name}: {value}")
    return "\n".join(lines)


def _verify_date(value: str, *, now: datetime | None, max_age_seconds: int) -> None:
    if not value:
        raise HttpSignatureError("Signed ActivityPub request is missing Date.")
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError) as exc:
        raise HttpSignatureError("Signed ActivityPub request has an invalid Date.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    if abs(reference - parsed.astimezone(UTC)) > timedelta(seconds=max_age_seconds):
        raise HttpSignatureError("Signed ActivityPub request Date is outside the allowed window.")


def _verify_digest(value: str, body: bytes) -> None:
    if not value:
        raise HttpSignatureError("Signed ActivityPub request is missing Digest.")
    expected = digest_header(body)
    if value.lower() != expected.lower():
        raise HttpSignatureError("Signed ActivityPub request Digest does not match the body.")
