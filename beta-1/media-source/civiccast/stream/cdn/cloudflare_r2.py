# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Cloudflare R2 storage adapter.

Implements the :class:`CDNAdapter` Protocol against Cloudflare R2's
S3-compatible API. Per ADR 0006: BunnyCDN is the v1 default; R2 is the
documented DDoS-protection alternate. Operators behind a high-stakes
council meeting frequently want the Cloudflare WAF in front of their
manifest origin; R2 + the Cloudflare CDN provides that without a
multi-vendor setup.

Credentials (three values):

  - ``account_id``: Cloudflare account id (used to construct the
    endpoint URL).
  - ``access_key_id`` + ``secret_access_key``: R2 token pair issued
    from the Cloudflare dashboard.
  - ``bucket``: R2 bucket name.
  - ``public_base_url``: the public URL prefix served by R2 (either a
    bound custom domain like ``cdn.station.org`` or the auto-generated
    ``pub-XXX.r2.dev`` host). The adapter does not synthesize this; the
    operator supplies it explicitly.

Optional dependency. Install with ``pip install civiccast[cloudflare-r2]``
(adds boto3 + botocore). The default install does not pull these so
stations on BunnyCDN keep their dep tree small.

Operator setup: USER-MANUAL.md §CDN Configuration (Cloudflare R2).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from civiccast.stream.cdn import cache_control

if TYPE_CHECKING:
    pass

__all__ = ["CloudflareR2Adapter", "CloudflareR2Error", "CloudflareR2NotInstalledError"]


_DEFAULT_TIMEOUT = 120  # seconds — match BunnyCDN's posture for large segment batches


class CloudflareR2Error(RuntimeError):
    """Network or API error from Cloudflare R2.

    Carries the underlying boto3 error code (e.g., ``"NoSuchBucket"``,
    ``"AccessDenied"``) when present so the operator can map error
    messages to the Cloudflare dashboard.
    """

    def __init__(self, message: str, error_code: str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


class CloudflareR2NotInstalledError(RuntimeError):
    """boto3 is not installed in this environment.

    Raised when ``CloudflareR2Adapter`` is constructed without the
    ``cloudflare-r2`` extra installed. The error message names the
    install command so the operator gets an actionable next step.
    """


def _import_boto3() -> Any:
    """Import boto3 lazily and translate ImportError into the adapter's
    exception type so callers get an actionable error message rather
    than a raw ImportError from deep in a constructor."""
    try:
        import boto3
    except ImportError as exc:
        raise CloudflareR2NotInstalledError(
            "Cloudflare R2 support requires the 'cloudflare-r2' optional "
            "dependency. Install with: pip install 'civiccast[cloudflare-r2]' "
            "(or uv add civiccast[cloudflare-r2]). See USER-MANUAL.md "
            "§CDN Configuration (Cloudflare R2)."
        ) from exc
    return boto3


class CloudflareR2Adapter:
    """CDN adapter backed by Cloudflare R2 (S3-compatible) + R2 public URL.

    The R2 endpoint is constructed as
    ``https://<account_id>.r2.cloudflarestorage.com``. boto3's S3 client
    is pointed at that endpoint with ``region_name="auto"`` (R2 ignores
    region but boto3 requires the kwarg).

    The public URL prefix is operator-supplied — either a bound custom
    domain or the auto-generated ``pub-*.r2.dev`` host. The adapter does
    not synthesize it because the host depends on the operator's R2
    configuration choices outside our visibility.

    Raises :class:`CloudflareR2NotInstalledError` if the ``cloudflare-r2``
    optional dependency is not installed.

    Args:
        account_id: Cloudflare account id (32-char hex).
        access_key_id: R2 token access key.
        secret_access_key: R2 token secret.
        bucket: R2 bucket name.
        public_base_url: public URL prefix (no trailing slash; scheme
            required, must be HTTPS — see spec §4.1, §15).
        timeout: HTTP timeout in seconds per S3 request (default 120).
    """

    def __init__(
        self,
        account_id: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        public_base_url: str,
        timeout: float = _DEFAULT_TIMEOUT,
        *,
        _client: Any = None,
    ) -> None:
        if not all([account_id, access_key_id, secret_access_key, bucket, public_base_url]):
            raise ValueError(
                "CloudflareR2Adapter requires account_id, access_key_id, "
                "secret_access_key, bucket, and public_base_url. "
                "See USER-MANUAL.md §CDN Configuration (Cloudflare R2)."
            )

        if not public_base_url.startswith("https://"):
            raise ValueError(
                "public_base_url must start with https:// — civic video "
                "served over plain HTTP is rejected per spec §4.1, §15."
            )

        self._bucket = bucket
        self._public_base_url = public_base_url.rstrip("/")

        # Test hook: callers may inject a pre-constructed boto3 S3 client
        # (e.g., a moto-mocked one). When None, the adapter constructs its
        # own client pointed at the R2 endpoint. This is the only way to
        # exercise the adapter against moto, since moto's mock_aws()
        # intercepts boto3 calls but cannot intercept a custom endpoint_url
        # like ``<account>.r2.cloudflarestorage.com``.
        if _client is not None:
            self._client = _client
            return

        boto3 = _import_boto3()
        from botocore.config import Config

        endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
            config=Config(
                signature_version="s3v4",
                connect_timeout=timeout,
                read_timeout=timeout,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    def upload_file(self, local_path: Path, remote_key: str) -> str:
        """Upload ``local_path`` to R2 at ``remote_key``.

        Returns the public URL for the uploaded object. Raises
        :class:`CloudflareR2Error` on S3 API or network failure.
        """
        from boto3.exceptions import S3UploadFailedError
        from botocore.exceptions import BotoCoreError, ClientError

        remote_key = remote_key.lstrip("/")
        try:
            self._client.upload_file(
                Filename=str(local_path),
                Bucket=self._bucket,
                Key=remote_key,
                ExtraArgs={
                    "ContentType": _guess_content_type(remote_key),
                    "CacheControl": cache_control(remote_key),
                },
            )
        except ClientError as exc:
            err_code = exc.response.get("Error", {}).get("Code")
            raise CloudflareR2Error(
                f"R2 upload failed for {remote_key}: {exc}",
                error_code=err_code,
            ) from exc
        except S3UploadFailedError as exc:
            # boto3 wraps some S3 errors (e.g., NoSuchBucket on PutObject)
            # in this higher-level exception instead of letting ClientError
            # surface. Trap it the same way operationally.
            raise CloudflareR2Error(f"R2 upload failed for {remote_key}: {exc}") from exc
        except BotoCoreError as exc:
            raise CloudflareR2Error(f"Network error uploading {remote_key} to R2: {exc}") from exc
        return self.public_url(remote_key)

    def delete_file(self, remote_key: str) -> None:
        """Delete ``remote_key`` from R2. Silent no-op when the key is absent."""
        from botocore.exceptions import BotoCoreError, ClientError

        remote_key = remote_key.lstrip("/")
        try:
            self._client.delete_object(Bucket=self._bucket, Key=remote_key)
        except ClientError as exc:
            err_code = exc.response.get("Error", {}).get("Code")
            if err_code in {"NoSuchKey", "404"}:
                return
            raise CloudflareR2Error(
                f"R2 delete failed for {remote_key}: {exc}",
                error_code=err_code,
            ) from exc
        except BotoCoreError as exc:
            raise CloudflareR2Error(f"Network error deleting {remote_key} from R2: {exc}") from exc

    def public_url(self, remote_key: str) -> str:
        """Return the public CDN URL for ``remote_key`` without uploading."""
        remote_key = remote_key.lstrip("/")
        return f"{self._public_base_url}/{remote_key}"

    def health_check(self) -> bool:
        """Return True if the bucket is reachable and the credentials work.

        Used by ``civiccast doctor`` to surface CDN-config errors before
        the operator tries to publish. Catches and returns False on every
        error so the doctor can present a single boolean status.
        """
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            self._client.head_bucket(Bucket=self._bucket)
        except (ClientError, BotoCoreError):
            return False
        return True


_CONTENT_TYPE_BY_EXT: dict[str, str] = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t",
    ".mp4": "video/mp4",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".vtt": "text/vtt",
}


def _guess_content_type(remote_key: str) -> str:
    """Map filename extension to Content-Type header.

    R2 does not auto-detect MIME types, and the wrong Content-Type on
    ``.m3u8`` will break HLS playback in some browsers. This is the same
    table BunnyCDN uses implicitly via its server-side detection — we
    set it explicitly here.
    """
    suffix = Path(remote_key).suffix.lower()
    return _CONTENT_TYPE_BY_EXT.get(suffix, "application/octet-stream")
