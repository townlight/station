# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Local CA creation, inspection, and service certificate rotation."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from civiccast.certs.models import (
    CertificateAuthorityStatus,
    CertificateRotationStatus,
    ServiceCertificateStatus,
)
from civiccast.native.service_identity import SERVICE_SID

REQUIRED_SERVICE_IDENTITIES = ("civiccast-api", "civiccast-worker")
ROTATION_DANGER_WINDOW_DAYS = 30


class LocalCertificateAuthority:
    """Filesystem-backed CivicCast local CA with private-key-safe status APIs."""

    def __init__(self, install_root: Path | str) -> None:
        self.root = Path(install_root)
        self.ca_dir = self.root / "ca"
        self.service_dir = self.root / "services"
        self.retired_dir = self.root / "retired"
        self.ca_certificate_path = self.ca_dir / "civiccast-local-ca.crt"
        self._ca_key_path = self.ca_dir / "civiccast-local-ca.key"

    def create_ca(self, *, common_name: str = "CivicCast Local CA") -> CertificateAuthorityStatus:
        self.ca_dir.mkdir(parents=True, exist_ok=True)
        self.service_dir.mkdir(parents=True, exist_ok=True)
        self.retired_dir.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(UTC)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )
        self._write_private_key(self._ca_key_path, key)
        self.ca_certificate_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return self._ca_status(cert)

    def inspect_ca(self) -> CertificateAuthorityStatus:
        cert = self._load_ca_certificate()
        return self._ca_status(cert)

    def issue_service_certificate(
        self, service_identity: str, *, valid_days: int = 90
    ) -> ServiceCertificateStatus:
        _require_known_identity(service_identity)
        if not self.ca_certificate_path.exists() or not self._ca_key_path.exists():
            self.create_ca()
        self.service_dir.mkdir(parents=True, exist_ok=True)
        ca_cert = self._load_ca_certificate()
        ca_key = cast(
            rsa.RSAPrivateKey,
            serialization.load_pem_private_key(self._ca_key_path.read_bytes(), password=None),
        )
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(UTC)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, service_identity)])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=valid_days))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(service_identity)]),
                critical=False,
            )
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=True,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                # NATS JetStream was removed from the product (owner decision
                # 2026-08-20, ADR 0023). It was the only SERVER_AUTH service
                # identity here; every remaining identity is a CLIENT_AUTH-only
                # mTLS client certificate.
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
                critical=False,
            )
            .sign(ca_key, hashes.SHA256())
        )
        cert_path = self._service_cert_path(service_identity)
        key_path = self._service_key_path(service_identity)
        self._write_private_key(key_path, key)
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return self._service_status(service_identity, cert, cert_path)

    def inspect_service_certificate(self, service_identity: str) -> ServiceCertificateStatus:
        _require_known_identity(service_identity)
        cert_path = self._service_cert_path(service_identity)
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        return self._service_status(service_identity, cert, cert_path)

    def rotation_status(self, service_identity: str) -> CertificateRotationStatus:
        _require_known_identity(service_identity)
        cert_path = self._service_cert_path(service_identity)
        if not cert_path.exists():
            return CertificateRotationStatus(
                service_identity=service_identity,
                state="missing",
                rotation_due=True,
                next_step=f"Run `civiccast cert rotate {service_identity}` to issue credentials.",
            )
        status = self.inspect_service_certificate(service_identity)
        now = datetime.now(UTC)
        if status.not_after <= now:
            return CertificateRotationStatus(
                service_identity=service_identity,
                state="expired",
                rotation_due=True,
                expires_at=status.not_after,
                next_step=f"Run `civiccast cert rotate {service_identity}` before starting services.",
            )
        if status.not_after - now <= timedelta(days=ROTATION_DANGER_WINDOW_DAYS):
            return CertificateRotationStatus(
                service_identity=service_identity,
                state="rotation_due",
                rotation_due=True,
                expires_at=status.not_after,
                next_step=f"Run `civiccast cert rotate {service_identity}` before expiry.",
            )
        return CertificateRotationStatus(
            service_identity=service_identity,
            state="healthy",
            rotation_due=False,
            expires_at=status.not_after,
            next_step="No rotation needed. Recheck during the next installer readiness pass.",
        )

    def rotate_service_certificate(self, service_identity: str) -> ServiceCertificateStatus:
        _require_known_identity(service_identity)
        retired_fingerprint: str | None = None
        cert_path = self._service_cert_path(service_identity)
        if cert_path.exists():
            before = self.inspect_service_certificate(service_identity)
            retired_fingerprint = before.fingerprint_sha256
            self.retired_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
            cert_path.replace(self.retired_dir / f"{service_identity}-{stamp}.crt")
            key_path = self._service_key_path(service_identity)
            if key_path.exists():
                key_path.replace(self.retired_dir / f"{service_identity}-{stamp}.key")
        issued = self.issue_service_certificate(service_identity)
        return issued.model_copy(
            update={"retired_certificate_fingerprint_sha256": retired_fingerprint}
        )

    def _ca_status(self, cert: x509.Certificate) -> CertificateAuthorityStatus:
        return CertificateAuthorityStatus(
            common_name=_common_name(cert.subject),
            ca_certificate_path=self.ca_certificate_path,
            fingerprint_sha256=cert.fingerprint(hashes.SHA256()).hex(),
            not_before=_aware(cert.not_valid_before_utc),
            not_after=_aware(cert.not_valid_after_utc),
        )

    def _service_status(
        self, service_identity: str, cert: x509.Certificate, cert_path: Path
    ) -> ServiceCertificateStatus:
        ca_cert = self._load_ca_certificate()
        try:
            san_ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            sans = san_ext.value.get_values_for_type(x509.DNSName)
        except x509.ExtensionNotFound:
            sans = []
        return ServiceCertificateStatus(
            service_identity=service_identity,
            certificate_path=cert_path,
            fingerprint_sha256=cert.fingerprint(hashes.SHA256()).hex(),
            issuer_fingerprint_sha256=ca_cert.fingerprint(hashes.SHA256()).hex(),
            subject_alternative_names=list(sans),
            not_before=_aware(cert.not_valid_before_utc),
            not_after=_aware(cert.not_valid_after_utc),
        )

    def _load_ca_certificate(self) -> x509.Certificate:
        return x509.load_pem_x509_certificate(self.ca_certificate_path.read_bytes())

    def _service_cert_path(self, service_identity: str) -> Path:
        return self.service_dir / f"{service_identity}.crt"

    def _service_key_path(self, service_identity: str) -> Path:
        return self.service_dir / f"{service_identity}.key"

    def _write_private_key(self, path: Path, key: rsa.RSAPrivateKey) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        if os.name != "nt":
            path.chmod(0o600)
        else:
            _restrict_windows_private_key_acl(path)


def rotate_service_certificate(root: Path | str, identity: str) -> ServiceCertificateStatus:
    """Rotate a known service certificate under the configured certificate root."""

    return LocalCertificateAuthority(root).rotate_service_certificate(identity)


def _require_known_identity(service_identity: str) -> None:
    if service_identity not in REQUIRED_SERVICE_IDENTITIES:
        example = REQUIRED_SERVICE_IDENTITIES[0]
        raise ValueError(
            f"{service_identity!r} is not a known service identity. Use one of "
            f"{', '.join(REQUIRED_SERVICE_IDENTITIES)}. Example: civiccast cert rotate {example}"
        )


def _common_name(name: x509.Name) -> str:
    attributes = name.get_attributes_for_oid(NameOID.COMMON_NAME)
    return str(attributes[0].value) if attributes else "CivicCast Local CA"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


#: Well-known, locale- and domain-independent SIDs. ``icacls`` accepts a raw
#: SID as a principal when it is prefixed with ``*`` (``icacls /?``), and a
#: well-known SID resolves identically on EVERY Windows machine -- English or
#: localized, domain-joined or workgroup. We grant these BY SID instead of by
#: the display names ``SYSTEM``/``Administrators`` (which are localized) and,
#: critically, instead of ``%USERNAME%`` -- under the LocalSystem supervisor
#: service that serves ``POST /api/setup/first-admin`` the ``USERNAME`` env
#: var is the MACHINE ACCOUNT ``COMPUTERNAME$``, which does NOT resolve as a
#: local security principal on a non-domain (workgroup) station. icacls then
#: exits 1332 (ERROR_NONE_MAPPED, "No mapping between account names and
#: security IDs was done") and, with ``check=True``, propagated an unhandled
#: 500 out of first-admin setup (blocker N-01). Same house rule as
#: :mod:`civiccast.native.pgdata_acl` ("well-known SID aliases, never
#: localized account NAMES"; read identity from the TOKEN, never ``%USERNAME%``).
_SYSTEM_SID = "S-1-5-18"
_ADMINISTRATORS_SID = "S-1-5-32-544"

#: A textual SID as ``ConvertSidToStringSid`` returns it. Validated before it
#: is ever spliced into an ``icacls`` argv so a malformed or hostile value can
#: never smuggle an extra grantee into the command line.
_SID_RE = re.compile(r"^S-1-\d+(-\d+)+$")


def _current_process_sid() -> str:
    """Return the calling process token's user SID as an ``S-1-...`` string.

    Read from the process TOKEN, never from ``%USERNAME%``: under the
    LocalSystem supervisor service (which is what serves
    ``POST /api/setup/first-admin``) ``%USERNAME%`` is the machine account
    ``COMPUTERNAME$``, which does not resolve on a non-domain station. The
    token's user SID is exactly the principal the process runs as and always
    resolves (under LocalSystem it is ``S-1-5-18``). Same primitive
    :func:`civiccast.native.pgdata_acl._current_process_sid` already uses.
    """

    import win32api
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    sid, _attributes = win32security.GetTokenInformation(token, win32security.TokenUser)
    return str(win32security.ConvertSidToStringSid(sid))


def _windows_private_key_acl_command(
    path: Path, *, principal_sid: str, service_can_replace: bool = False
) -> list[str]:
    """Return the ``icacls`` command that strips inherited access from a local
    key/secret file and re-grants Full control to the writer, SYSTEM and admins,
    each named by a raw SID (``*S-1-...``) so every grant resolves on a
    non-domain station and on a non-English Windows:

    * ``principal_sid`` -- the account THIS process runs as, so it keeps
      access to the material it just wrote. Under the LocalSystem service this
      is ``S-1-5-18``; under an elevated installer it is the installing admin.
    * ``S-1-5-18`` (:data:`_SYSTEM_SID`) -- ``NT AUTHORITY\\SYSTEM``, the
      identity the supervisor service (and therefore the runtime that must
      read this key) runs as.
    * ``S-1-5-32-544`` (:data:`_ADMINISTRATORS_SID`) -- ``BUILTIN\\Administrators``,
      so the installer/operator tooling can manage and remove it.

    ``principal_sid`` is validated against :data:`_SID_RE`; anything that is
    not a textual SID raises before an ``icacls`` argv is built.
    """

    if not _SID_RE.match(principal_sid or ""):
        raise ValueError(
            f"Refusing to build an icacls grant from {principal_sid!r}, which is not a "
            "textual SID (expected 'S-1-...')."
        )
    command = [
        "icacls",
        str(path),
        "/inheritance:r",
        "/grant:r",
        f"*{principal_sid}:F",
        f"*{_SYSTEM_SID}:F",
        f"*{_ADMINISTRATORS_SID}:F",
    ]
    # The restricted runtime needs the same secrets after an administrator
    # creates or resets them, without permission to rewrite their ACLs.
    if principal_sid != SERVICE_SID:
        rights = "R,W,D" if service_can_replace else "R,W"
        command.append(f"*{SERVICE_SID}:({rights})")
    return command


def _restrict_windows_private_key_acl(
    path: Path,
    *,
    sid_reader: Callable[[], str] | None = None,
    service_can_replace: bool = False,
) -> None:
    """Restrict local private-key ACLs on Windows after writing key material.

    Grants Full control to the calling process's OWN token SID plus SYSTEM and
    Administrators -- all by well-known/textual SID, never by name -- and
    strips inheritance. Fails LOUD, with the icacls exit code and stderr, if
    the grant does not apply: the file just hardened holds private-key or
    secret-hash material, so a swallowed failure would leave it readable. The
    previous implementation granted ``%USERNAME%``, which under the LocalSystem
    service is the unresolvable machine account ``COMPUTERNAME$`` -- the
    root cause of the first-admin 500 on a workgroup station (blocker N-01).

    ``service_can_replace`` additionally grants DELETE for atomically saved
    station state. Private keys retain the default service read/write grant.

    ``sid_reader`` is an injectable seam (production default
    :func:`_current_process_sid`) so the decision logic is unit-testable
    without Windows.
    """

    read_sid = sid_reader if sid_reader is not None else _current_process_sid
    try:
        principal_sid = read_sid()
    except Exception as exc:  # pragma: no cover - exercised via injected seam
        raise RuntimeError(
            "Cannot restrict CivicCast private-key ACL on Windows because the calling "
            f"process's own user SID could not be read: {exc}"
        ) from exc

    command = _windows_private_key_acl_command(
        path, principal_sid=principal_sid, service_can_replace=service_can_replace
    )
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(  # noqa: S603 - fixed icacls argv; no shell or user-built command line.
        command,
        check=False,
        capture_output=True,
        text=True,
        creationflags=creationflags,
    )
    if result.returncode == 1332 and SERVICE_SID in f"{result.stdout}\n{result.stderr}":
        # icacls resolves even a *S-1-... trustee through the local account
        # database. A virtual service SID has no account mapping before its
        # service is registered, while Windows ACL APIs accept the SID itself.
        import win32security

        grants = {
            principal_sid: "FA",
            _SYSTEM_SID: "FA",
            _ADMINISTRATORS_SID: "FA",
        }
        grants.setdefault(SERVICE_SID, "FRFWSD" if service_can_replace else "FRFW")
        sddl = "D:P" + "".join(f"(A;;{rights};;;{sid})" for sid, rights in grants.items())
        descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(sddl, 1)
        try:
            win32security.SetFileSecurity(
                str(path),
                win32security.DACL_SECURITY_INFORMATION
                | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
                descriptor,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Failed to restrict CivicCast private-key ACL on Windows with native SID ACL: {exc}"
            ) from exc
        return
    if result.returncode != 0:
        raise RuntimeError(
            "Failed to restrict CivicCast private-key ACL on Windows: icacls exited "
            f"{result.returncode} for {str(path)!r} "
            f"(stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r})."
        )
