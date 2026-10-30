# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Generic S3-compatible object-storage CDN adapter.

Backs any CDN whose origin is S3-compatible object storage fronted by the
provider's CDN network. Per ADR 0006 + ADR 0020, CivicCast ships this for
**Fastly Object Storage** (endpoint ``https://<region>.object.fastlystorage.app``)
and **Akamai / Linode Object Storage** (endpoint
``https://<region>.linodeobjects.com``); the same class serves any other
S3-compatible origin -- the factory constructs the endpoint per provider.
Cloudflare R2 keeps its own :class:`~civiccast.stream.cdn.cloudflare_r2.CloudflareR2Adapter`
(its endpoint is derived from the account id rather than a region).

Credentials (all operator-supplied):

  - ``endpoint_url``: the S3 API endpoint (HTTPS), e.g.
    ``https://us-east.object.fastlystorage.app`` or
    ``https://us-east-1.linodeobjects.com``.
  - ``access_key_id`` + ``secret_access_key``: the provider's S3 token pair.
  - ``bucket``: the bucket name.
  - ``public_base_url``: the public HTTPS URL prefix residents use (a bound
    custom domain or the provider's default public host). The adapter never
    synthesizes it.
  - ``region``: the S3 region label (S3-compatible providers require one).

Optional dependency. Install with ``pip install civiccast[s3-cdn]`` (adds boto3
+ botocore). The default install does not pull these so stations on BunnyCDN
keep their dependency tree small.

Operator setup: USER-MANUAL.md §CDN Configuration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from civiccast.stream.cdn import cache_control

__all__ = ["S3CDNError", "S3CDNNotInstalledError", "S3CompatibleCDNAdapter"]

_DEFAULT_TIMEOUT = 120  # seconds — match the other adapters for large segment batches


class S3CDNError(RuntimeError):
    """Network or API error from an S3-compatible CDN origin.

    Carries the underlying boto3 error code (e.g. ``"NoSuchBucket"``,
    ``"AccessDenied"``) when present so the operator can map it to the
    provider dashboard.
    """

    def __init__(self, message: str, error_code: str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


class S3CDNNotInstalledError(RuntimeError):
    """boto3 is not installed in this environment.

    Raised when the adapter is constructed without the ``s3-cdn`` extra
    installed; the message names the install command.
    """


def _import_boto3() -> Any:
    """Import boto3 lazily and translate ImportError into the adapter's own
    exception so callers get an actionable message rather than a raw
    ImportError from deep in a constructor."""
    try:
        import boto3
    except ImportError as exc:
        raise S3CDNNotInstalledError(
            "S3-compatible CDN support requires the 's3-cdn' optional "
            "dependency. Install with: pip install 'civiccast[s3-cdn]' "
            "(or uv add civiccast[s3-cdn]). See USER-MANUAL.md "
            "§CDN Configuration."
        ) from exc
    return boto3


class S3CompatibleCDNAdapter:
    """CDN adapter backed by any S3-compatible object store + a public URL.

    boto3's S3 client is pointed at ``endpoint_url`` with SigV4 signing.
    The public URL prefix is operator-supplied; the adapter does not synthesize
    it because the host depends on provider configuration outside our
    visibility.

    Raises :class:`S3CDNNotInstalledError` if the ``s3-cdn`` optional
    dependency is not installed.

    Args:
        endpoint_url: the S3 API endpoint (must be HTTPS).
        access_key_id: the provider's S3 access key id.
        secret_access_key: the provider's S3 secret.
        bucket: bucket name.
        public_base_url: public URL prefix (HTTPS, no trailing slash).
        region: S3 region label (default ``us-east-1``).
        timeout: HTTP timeout in seconds per S3 request (default 120).
    """

    def __init__(
        self,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        public_base_url: str,
        region: str = "us-east-1",
        timeout: float = _DEFAULT_TIMEOUT,
        *,
        _client: Any = None,
    ) -> None:
        if not all([endpoint_url, access_key_id, secret_access_key, bucket, public_base_url]):
            raise ValueError(
                "S3CompatibleCDNAdapter requires endpoint_url, access_key_id, "
                "secret_access_key, bucket, and public_base_url. "
                "See USER-MANUAL.md §CDN Configuration."
            )
        if not endpoint_url.startswith("https://"):
            raise ValueError("endpoint_url must start with https://.")
        if not public_base_url.startswith("https://"):
            raise ValueError(
                "public_base_url must start with https:// -- civic video served "
                "over plain HTTP is rejected per spec §4.1, §15."
            )

        self._bucket = bucket
        self._public_base_url = public_base_url.rstrip("/")

        # Test hook: callers may inject a pre-constructed boto3 S3 client (e.g. a
        # moto-mocked one). moto's mock_aws() cannot intercept a custom
        # endpoint_url, so tests inject the client instead.
        if _client is not None:
            self._client = _client
            return

        boto3 = _import_boto3()
        from botocore.config import Config

        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region,
            config=Config(
                signature_version="s3v4",
                connect_timeout=timeout,
                read_timeout=timeout,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    def upload_file(self, local_path: Path, remote_key: str) -> str:
        """Upload ``local_path`` to the bucket at ``remote_key``.

        Returns the public URL for the uploaded object. Raises
        :class:`S3CDNError` on S3 API or network failure.
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
            raise S3CDNError(
                f"S3 upload failed for {remote_key}: {exc}", error_code=err_code
            ) from exc
        except S3UploadFailedError as exc:
            raise S3CDNError(f"S3 upload failed for {remote_key}: {exc}") from exc
        except BotoCoreError as exc:
            raise S3CDNError(f"Network error uploading {remote_key}: {exc}") from exc
        return self.public_url(remote_key)

    def delete_file(self, remote_key: str) -> None:
        """Delete ``remote_key`` from the bucket. Silent no-op when absent."""
        from botocore.exceptions import BotoCoreError, ClientError

        remote_key = remote_key.lstrip("/")
        try:
            self._client.delete_object(Bucket=self._bucket, Key=remote_key)
        except ClientError as exc:
            err_code = exc.response.get("Error", {}).get("Code")
            if err_code in {"NoSuchKey", "404"}:
                return
            raise S3CDNError(
                f"S3 delete failed for {remote_key}: {exc}", error_code=err_code
            ) from exc
        except BotoCoreError as exc:
            raise S3CDNError(f"Network error deleting {remote_key}: {exc}") from exc

    def public_url(self, remote_key: str) -> str:
        """Return the public CDN URL for ``remote_key`` without uploading."""
        remote_key = remote_key.lstrip("/")
        return f"{self._public_base_url}/{remote_key}"

    def health_check(self) -> bool:
        """Return True if the bucket is reachable and the credentials work.

        Catches and returns False on every error so a caller can present a
        single pass/fail (see the setup wizard "Test connection").
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
    """Map filename extension to Content-Type; S3 stores do not auto-detect."""
    suffix = Path(remote_key).suffix.lower()
    return _CONTENT_TYPE_BY_EXT.get(suffix, "application/octet-stream")
