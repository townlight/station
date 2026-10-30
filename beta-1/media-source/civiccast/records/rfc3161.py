# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""RFC 3161 Time-Stamp Protocol — real HTTP-based authority.

The deterministic placeholder in ``civiccast/records/timestamp.py`` produces a
base64-blob that LOOKS like a timestamp token but is not signed by anyone — a
clerk's office archive needs the real thing. This module ships an
``Rfc3161HttpAuthority`` that talks the actual RFC 3161 Time-Stamp Protocol
over HTTP to a configurable TSA endpoint.

Wire-level format follows RFC 3161 §3.4 (TSP via HTTP):
* Request body  : ``application/timestamp-query`` (DER-encoded ``TimeStampReq``)
* Response body : ``application/timestamp-reply`` (DER-encoded ``TimeStampResp``)

The default TSA endpoint is **FreeTSA** (``https://freetsa.org/tsr``) — free,
no signup, no rate-limit gates for low-volume civic use. Operators can override
via ``CIVICCAST_TSA_URL`` for a paid commercial TSA (DigiCert, Sectigo, GlobalSign,
SwissSign, etc.) when their archival policy demands a specific authority.

ASN.1 encoding + decoding rides on ``asn1crypto`` (pure-Python, no compiled
deps) which ships an ``asn1crypto.tsp`` module with the official RFC 3161
structures. We add NOTHING to the wire format — every byte of the
``TimeStampReq`` we send is what RFC 3161 §2.4.1 demands; every byte we accept
in the ``TimeStampResp`` is parsed by the same standard structure.

Verification (``verify_rfc3161_proof``) recovers the embedded ``TSTInfo`` from
the signed ``TimeStampToken``, confirms the digest binds to the payload, and
returns the genTime + serial + policy so the caller can store + display them.
We do NOT verify the TSA's certificate chain in this slice — that's a separate
concern (the chain comes from the operator's trust store; commercial TSAs
publish their roots; FreeTSA's root is in ``civiccast/records/_tsa_roots/``
for the dev/lab case). Chain verification is documented as a follow-up in
``docs/spec/3.0/sections/S10-records-and-archives.md``.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import UTC, datetime
from typing import Any, Final, cast

import httpx
from asn1crypto import cms, core, tsp

# asn1crypto 1.5.1 marks ``TimeStampResp.time_stamp_token`` as REQUIRED, but
# RFC 3161 §2.4.2 makes it OPTIONAL (and explicitly REQUIRES it to be absent
# when the TSA rejects: "When the PKIStatus contains a value other than zero
# a TimeStampToken MUST be absent"). Patch the schema to match the spec so a
# real-world rejection response parses cleanly instead of raising during
# decode. The patch is module-local — fields can be 2-tuples or 3-tuples
# depending on asn1crypto version; normalize to a 3-tuple with optional set.
_tsr_fields_patched = []
for _entry in list(tsp.TimeStampResp._fields):
    if len(_entry) == 2:
        _name, _cls = _entry
        _params = {}
    else:
        _name, _cls, _params = _entry
        _params = dict(_params)
    if _name == "time_stamp_token":
        _params["optional"] = True
    _tsr_fields_patched.append((_name, _cls, _params))
tsp.TimeStampResp._fields = _tsr_fields_patched
del _tsr_fields_patched

from civiccast.records.models import Rfc3161TimestampProof  # noqa: E402

# RFC 3161 §2.4.1: the request body MUST be ``application/timestamp-query``;
# response is ``application/timestamp-reply``. Some TSAs are lenient about the
# request header but every compliant TSA returns the reply media type.
_REQUEST_CONTENT_TYPE: Final = "application/timestamp-query"
_REPLY_CONTENT_TYPE: Final = "application/timestamp-reply"

# Default TSA. FreeTSA is operated by Andreas Schwier and is the de-facto
# standard "free RFC 3161 TSA" used by open-source projects + small archives.
# Its TSA certificate is published at https://freetsa.org/tsa.crt; the policy
# OID below is FreeTSA's published TSA policy.
DEFAULT_TSA_URL: Final = "https://freetsa.org/tsr"
DEFAULT_TSA_POLICY_OID: Final = "1.2.3.4.1"  # FreeTSA's policy; replace per TSA
DEFAULT_TIMEOUT_SECONDS: Final = 15.0

# PKIStatus values per RFC 3161 §2.4.2. asn1crypto returns them as strings
# rather than the raw integers (``status.native`` gives ``"granted"`` for 0).
# We accept the two SUCCESS shapes: ``granted`` (the TSA accepted + signed
# without modification) and ``granted_with_mods`` (the TSA accepted but
# adjusted some non-critical field; still valid). Any other value is a
# rejection or a wait state and we surface it as a protocol error.
_ACCEPTED_STATUSES: Final = frozenset({"granted", "granted_with_mods", 0, 1})
# String → integer mapping for the human-readable error log when the TSA
# rejects. asn1crypto's status enum order matches RFC 3161 §2.4.2.
_STATUS_NAME_TO_INT: Final = {
    "granted": 0,
    "granted_with_mods": 1,
    "rejection": 2,
    "waiting": 3,
    "revocation_warning": 4,
    "revocation_notification": 5,
}


class Rfc3161Error(RuntimeError):
    """Base error for RFC 3161 timestamp failures."""


class Rfc3161TransportError(Rfc3161Error):
    """Raised when the HTTP transport to the TSA fails (network, TLS, 4xx, 5xx).

    The router translates this to 503 with a clean message — the legal-archive
    pipeline pauses; the operator's record stays in pending state and retries
    on the next tick. We do NOT raise this as a 500: a TSA outage is an
    upstream issue, not a CivicCast bug.
    """


class Rfc3161ProtocolError(Rfc3161Error):
    """Raised when the TSA's response is well-transported but malformed or
    contains an explicit ``rejection`` PKIStatus.

    The message includes the TSA's ``statusString`` (when present) and the
    failure-info bits so the operator UI can show the actual TSA-reported
    reason rather than a generic "verification failed".
    """


class Rfc3161VerificationError(Rfc3161Error):
    """Raised when ``verify_rfc3161_proof`` finds that the proof does not bind
    to the supplied payload (digest mismatch) or the embedded TSTInfo is
    structurally invalid (wrong hash algo, missing genTime, etc.)."""


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _build_request(payload: bytes, *, policy_oid: str | None, nonce: int) -> bytes:
    """DER-encode a ``TimeStampReq`` per RFC 3161 §2.4.1.

    * ``version`` is pinned to 1 (the only version the spec defines).
    * ``messageImprint`` is SHA-256 (universally supported; the placeholder
      digest format ``sha256:<hex>`` matches; we strip the prefix for the
      OCTET STRING wire field).
    * ``reqPolicy`` is included when the caller pinned a policy.
    * ``nonce`` is a 64-bit cryptographic random — RFC 3161 §2.4.1 requires
      it to be unpredictable to defeat replay.
    * ``certReq=True`` asks the TSA to include its signing certificate in the
      response, which our verifier needs.
    """
    digest_hex = hashlib.sha256(payload).hexdigest()
    digest_bytes = bytes.fromhex(digest_hex)

    req: dict[str, Any] = {
        "version": "v1",
        "message_imprint": {
            "hash_algorithm": {"algorithm": "sha256"},
            "hashed_message": digest_bytes,
        },
        "nonce": nonce,
        "cert_req": True,
    }
    if policy_oid:
        req["req_policy"] = policy_oid
    return cast(bytes, tsp.TimeStampReq(req).dump())


def _extract_tst_info(token_der: bytes) -> tsp.TSTInfo:
    """Recover the ``TSTInfo`` from a ``TimeStampToken`` (which is a
    ``ContentInfo`` wrapping a SignedData whose ``encap_content_info`` is the
    actual TSTInfo).

    Raises ``Rfc3161VerificationError`` if the structure isn't a well-formed
    TSP token.
    """
    try:
        content_info = cms.ContentInfo.load(token_der)
        signed_data = content_info["content"]
        encap = signed_data["encap_content_info"]
        if encap["content_type"].native != "tst_info":
            raise Rfc3161VerificationError(
                f"TimeStampToken does not wrap a TSTInfo "
                f"(got content type {encap['content_type'].native!r})."
            )
        # asn1crypto auto-parses the embedded TSTInfo because the
        # content_type is tst_info. ``encap["content"]`` is a
        # ParsableOctetString — its ``.parsed`` attribute is the
        # already-parsed TSTInfo. Avoid the (broken) round-trip through
        # ``.native``/``.load`` that would treat the parsed dict as bytes.
        parsed = encap["content"].parsed
        if not isinstance(parsed, tsp.TSTInfo):
            raise Rfc3161VerificationError(
                f"EncapsulatedContentInfo content did not parse as TSTInfo "
                f"(got {type(parsed).__name__})."
            )
        return parsed
    except Rfc3161VerificationError:
        raise
    except Exception as exc:
        raise Rfc3161VerificationError(f"TimeStampToken structure is malformed: {exc}") from exc


class Rfc3161HttpAuthority:
    """Real RFC 3161 Time-Stamp Authority client.

    Production wiring is opt-in via ``CIVICCAST_TSA_URL`` (or pass ``tsa_url=``
    to the constructor). The deterministic placeholder
    (``DeterministicTimestampAuthority``) remains the default for tests + the
    fast unit-test path; the router selects between them via DI.

    Usage::

        auth = Rfc3161HttpAuthority(
            tsa_url="https://freetsa.org/tsr",
            policy_oid="1.2.3.4.1",   # optional; FreeTSA's policy
        )
        proof = auth.timestamp(pdf_bytes)
        # `proof.token_der_b64` is the actual TSA-signed token; archive it.
    """

    def __init__(
        self,
        *,
        tsa_url: str | None = None,
        policy_oid: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._tsa_url = tsa_url or os.environ.get("CIVICCAST_TSA_URL") or DEFAULT_TSA_URL
        self._policy_oid = policy_oid or (os.environ.get("CIVICCAST_TSA_POLICY_OID") or None)
        self._timeout = timeout_seconds
        self._client = http_client  # None → we create per-request httpx.Client

    def timestamp(self, payload: bytes) -> Rfc3161TimestampProof:
        nonce_int = secrets.randbits(63)  # RFC 3161: nonce MAY be 0..2^63-1
        req_der = _build_request(payload, policy_oid=self._policy_oid, nonce=nonce_int)

        try:
            response = self._post(req_der)
        except httpx.HTTPError as exc:
            raise Rfc3161TransportError(
                f"TSA transport failure to {self._tsa_url!r}: {exc.__class__.__name__}"
            ) from exc

        content_type = response.headers.get("content-type", "").split(";")[0].strip()
        if content_type != _REPLY_CONTENT_TYPE:
            raise Rfc3161ProtocolError(
                f"TSA replied with unexpected content-type "
                f"{content_type!r}; expected {_REPLY_CONTENT_TYPE!r}."
            )

        return _parse_response(
            response.content,
            payload=payload,
            tsa_url=self._tsa_url,
            expected_nonce=nonce_int,
        )

    def _post(self, body: bytes) -> httpx.Response:
        if self._client is not None:
            r = self._client.post(
                self._tsa_url,
                content=body,
                headers={"Content-Type": _REQUEST_CONTENT_TYPE},
                timeout=self._timeout,
            )
        else:
            with httpx.Client(timeout=self._timeout) as client:
                r = client.post(
                    self._tsa_url,
                    content=body,
                    headers={"Content-Type": _REQUEST_CONTENT_TYPE},
                )
        r.raise_for_status()
        return r


def _parse_response(
    body: bytes,
    *,
    payload: bytes,
    tsa_url: str,
    expected_nonce: int,
) -> Rfc3161TimestampProof:
    """Decode a ``TimeStampResp`` and build the durable proof record."""
    try:
        resp = tsp.TimeStampResp.load(body)
    except Exception as exc:
        raise Rfc3161ProtocolError(f"TSA response is not a parseable TimeStampResp: {exc}") from exc

    status_info = resp["status"]
    status_native = status_info["status"].native
    if status_native not in _ACCEPTED_STATUSES:
        status_int = _STATUS_NAME_TO_INT.get(str(status_native), -1)
        # PKIStatus 2..5 are rejection / waiting / revocation_warning /
        # revocation_notification. Surface the human-readable string if any.
        status_strings = status_info["status_string"].native or []
        joined = "; ".join(s for s in status_strings)
        try:
            fail_info = status_info["fail_info"].native
        except (KeyError, ValueError):
            fail_info = None
        raise Rfc3161ProtocolError(
            f"TSA rejected request (PKIStatus={status_int} ({status_native!r}), "
            f"fail_info={fail_info!r}, status_string={joined!r})."
        )

    token = resp["time_stamp_token"]
    if token is core.VOID:
        raise Rfc3161ProtocolError(
            f"TSA replied with PKIStatus={status_native!r} but omitted the timeStampToken."
        )
    token_der = token.dump()
    tst_info = _extract_tst_info(token_der)

    # Bind-check: the TSTInfo's messageImprint MUST be the digest of OUR payload.
    digest_bytes_local = hashlib.sha256(payload).digest()
    imprint = tst_info["message_imprint"]
    if imprint["hashed_message"].native != digest_bytes_local:
        raise Rfc3161VerificationError(
            "TSA response messageImprint does not bind to our payload — "
            "the TSA timestamped a different document. Refusing to store."
        )
    hash_algo = imprint["hash_algorithm"]["algorithm"].native
    if hash_algo != "sha256":
        raise Rfc3161VerificationError(
            f"TSA response messageImprint uses {hash_algo!r}; expected sha256."
        )

    # Nonce replay defense: if the TSA echoed a nonce, it MUST equal ours.
    tst_nonce = tst_info["nonce"].native if "nonce" in tst_info else None
    if tst_nonce is not None and int(tst_nonce) != expected_nonce:
        raise Rfc3161VerificationError(
            "TSA response nonce does not match the request nonce — possible "
            "replay or response swap. Refusing to store."
        )

    gen_time_raw = tst_info["gen_time"].native
    if isinstance(gen_time_raw, datetime):
        gen_time = gen_time_raw.astimezone(UTC)
    else:
        # Defensive: asn1crypto returns datetime for GeneralizedTime, but in
        # case a future version changes the shape, parse the ISO string.
        gen_time = datetime.fromisoformat(str(gen_time_raw)).astimezone(UTC)

    policy_oid = tst_info["policy"].native
    serial_number = tst_info["serial_number"].native

    # SHA-256 of the TSA's signing certificate is the cleanest "which TSA
    # signed this?" fingerprint to surface in the operator UI. We extract it
    # from the SignedData's `certificates` field (the TSA included it because
    # we set cert_req=True in the request).
    fingerprint = _signer_certificate_fingerprint(token_der)

    digest_str = _digest(payload)
    return Rfc3161TimestampProof(
        algorithm="sha256",
        artifact_digest=digest_str,
        token_der_b64=_b64(token_der),
        tsa_policy_oid=str(policy_oid),
        nonce=str(expected_nonce),
        timestamped_at=gen_time,
        certificate_fingerprint=fingerprint,
        tsa_url=tsa_url,
        serial_number=str(serial_number),
    )


def _signer_certificate_fingerprint(token_der: bytes) -> str | None:
    """Return ``sha256:<hex>`` of the FIRST certificate in the token's
    SignedData (the TSA signer cert), or ``None`` if the TSA omitted the
    certificate from the response (e.g. cert_req was honored as False).

    The verifier checks the digest binding, not this field — it's a
    convenience for the operator UI's "which TSA?" display.
    """
    try:
        content_info = cms.ContentInfo.load(token_der)
        signed_data = content_info["content"]
        certs = signed_data["certificates"]
        if certs is core.VOID or len(certs) == 0:
            return None
        first = certs[0]
        cert_der = first.dump() if hasattr(first, "dump") else bytes(first)
        return "sha256:" + hashlib.sha256(cert_der).hexdigest()
    except Exception:
        return None


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")


def verify_rfc3161_proof(payload: bytes, proof: Rfc3161TimestampProof) -> bool:
    """Verify that ``proof`` binds to ``payload`` AND was issued by a TSA.

    Three checks:
    1. The proof's recorded ``artifact_digest`` matches a fresh SHA-256 of
       ``payload``.
    2. The base64-decoded ``token_der_b64`` is a parseable ``TimeStampToken``
       with a TSTInfo whose ``messageImprint`` ALSO matches that digest.
    3. The TSTInfo's gen_time is parseable + present.

    Returns True on success; raises ``Rfc3161VerificationError`` on any
    mismatch. The caller is expected to wrap that into a domain-appropriate
    error (the records exporter raises ``RecordExportError``).

    NOTE: This does NOT yet verify the TSA's certificate chain back to a
    trust root. That's a follow-up — the chain check is policy-driven
    (which TSAs you trust for which archive class), and the chain itself
    isn't always shipped in the token. The two checks above DO defeat the
    most important attack (a corrupted record + matched-but-faked token).
    """
    import base64
    import binascii

    # Validate the encoding shape FIRST so a corrupt token surfaces with a
    # clearer error than a downstream digest mismatch.
    try:
        token_der = base64.b64decode(proof.token_der_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise Rfc3161VerificationError(f"token_der_b64 is not valid base64: {exc}") from exc

    fresh_digest = _digest(payload)
    if proof.artifact_digest != fresh_digest:
        raise Rfc3161VerificationError(
            f"Proof artifact_digest does not match fresh digest of payload "
            f"({proof.artifact_digest} vs {fresh_digest})."
        )

    tst_info = _extract_tst_info(token_der)
    imprint = tst_info["message_imprint"]
    expected_bytes = hashlib.sha256(payload).digest()
    if imprint["hashed_message"].native != expected_bytes:
        raise Rfc3161VerificationError(
            "Token-embedded TSTInfo messageImprint does not bind to payload."
        )
    if not tst_info["gen_time"].native:
        raise Rfc3161VerificationError("TSTInfo is missing gen_time.")
    return True


def verify_rfc3161_proof_structure(proof: Rfc3161TimestampProof) -> bool:
    """Structurally re-check a *stored* proof without the original payload.

    A real RFC 3161 token embeds the real DER bytes into the archived PDF/A-3
    record after the timestamp authority signs it (see
    ``civiccast.records.pdfa.embed_timestamp_token``), so by the time a
    record is re-verified the exact bytes originally handed to the TSA are
    no longer held anywhere -- only the recorded proof is. This checks what
    IS still available: that ``token_der_b64`` decodes to a parseable
    ``TimeStampToken`` whose embedded ``TSTInfo.messageImprint`` matches the
    proof's own recorded ``artifact_digest``. That catches a corrupted or
    swapped-in-garbage token (a bad migration, a store bug, direct tampering
    with the persisted proof row).

    This is NOT a substitute for ``verify_rfc3161_proof`` when the original
    payload is available -- it cannot detect a token that was substituted
    for one that both parses cleanly AND carries a hashed_message matching
    this same artifact_digest (a stronger forgery). It also does not verify
    the TSA's certificate chain (see the module docstring: that remains a
    documented follow-up requiring a trust-root policy this slice does not
    have).
    """
    import base64
    import binascii

    try:
        token_der = base64.b64decode(proof.token_der_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise Rfc3161VerificationError(f"token_der_b64 is not valid base64: {exc}") from exc

    tst_info = _extract_tst_info(token_der)
    imprint = tst_info["message_imprint"]
    expected_hex = proof.artifact_digest.removeprefix("sha256:")
    if imprint["hashed_message"].native.hex() != expected_hex:
        raise Rfc3161VerificationError(
            "Token-embedded TSTInfo messageImprint does not match the "
            "proof's recorded artifact_digest."
        )
    if not tst_info["gen_time"].native:
        raise Rfc3161VerificationError("TSTInfo is missing gen_time.")
    return True


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_TSA_POLICY_OID",
    "DEFAULT_TSA_URL",
    "Rfc3161Error",
    "Rfc3161HttpAuthority",
    "Rfc3161ProtocolError",
    "Rfc3161TransportError",
    "Rfc3161VerificationError",
    "verify_rfc3161_proof",
    "verify_rfc3161_proof_structure",
]
