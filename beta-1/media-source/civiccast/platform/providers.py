# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Config-driven provider registry for external-service adapters (Stage C).

Every external-provider touchpoint (Internet Archive, local NAS archive,
YouTube syndication, subscriber mail, subscriber webhooks) resolves its client
through this registry instead of hard-instantiating a class at the call site.
The deterministic mock/local clients stay the registered defaults; real
adapters ship for Internet Archive, YouTube, and mail (Beta sprint B5) and are
selected per kind with the kind's environment variable, e.g.
``CIVICCAST_PROVIDER_YOUTUBE=real``. Selecting ``real`` without that
provider's credential variables fails fast with the exact names — never a
silent mock fallback.

Unknown names fail fast with the registered options so a typo'd or
not-yet-shipped provider can never silently fall back to a mock.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from civiccast.platform.provider_settings import PROVIDER_SETUP_IDS, saved_fields

PROVIDER_KIND_INTERNET_ARCHIVE = "internet_archive"
PROVIDER_KIND_LOCAL_NAS = "local_nas"
PROVIDER_KIND_YOUTUBE = "youtube"
PROVIDER_KIND_MAIL = "mail"
PROVIDER_KIND_WEBHOOK = "webhook"

PROVIDER_KINDS: tuple[str, ...] = (
    PROVIDER_KIND_INTERNET_ARCHIVE,
    PROVIDER_KIND_LOCAL_NAS,
    PROVIDER_KIND_YOUTUBE,
    PROVIDER_KIND_MAIL,
    PROVIDER_KIND_WEBHOOK,
)

_DEFAULT_NAME = "mock"
_REAL_NAME = "real"

__all__ = [
    "PROVIDER_KINDS",
    "PROVIDER_KIND_INTERNET_ARCHIVE",
    "PROVIDER_KIND_LOCAL_NAS",
    "PROVIDER_KIND_MAIL",
    "PROVIDER_KIND_WEBHOOK",
    "PROVIDER_KIND_YOUTUBE",
    "ProviderConfiguration",
    "ProviderConfigurationError",
    "ProviderRegistry",
    "default_registry",
    "describe_provider",
]


class ProviderConfigurationError(RuntimeError):
    """Raised when a provider kind or name cannot be resolved."""


def _env_key(kind: str) -> str:
    return f"CIVICCAST_PROVIDER_{kind.upper()}"


def selected_provider_name(kind: str) -> str:
    explicit = os.environ.get(_env_key(kind), "").strip().lower()
    if explicit:
        return explicit
    provider_id = PROVIDER_SETUP_IDS.get(kind)
    fields = saved_fields(provider_id) if provider_id else {}
    if kind == PROVIDER_KIND_MAIL:
        fields = {key: value for key, value in fields.items() if key.startswith("smtp_") and value}
    return _REAL_NAME if fields else _DEFAULT_NAME


class ProviderRegistry:
    """Maps (kind, name) to a client factory; name selected per-kind from env."""

    def __init__(self) -> None:
        self._factories: dict[str, dict[str, Callable[[], Any]]] = {}

    def register(self, kind: str, name: str, factory: Callable[[], Any]) -> None:
        self._factories.setdefault(kind, {})[name] = factory

    def registered_names(self, kind: str) -> tuple[str, ...]:
        return tuple(sorted(self._factories.get(kind, {})))

    def resolve(self, kind: str) -> Any:
        """Build the configured client for ``kind`` (default: ``mock``)."""

        factories = self._factories.get(kind)
        if not factories:
            raise ProviderConfigurationError(
                f"Unknown provider kind {kind!r}; registered kinds: "
                f"{', '.join(sorted(self._factories))}."
            )
        name = selected_provider_name(kind)
        factory = factories.get(name)
        if factory is None:
            raise ProviderConfigurationError(
                f"{_env_key(kind)}={name!r} does not name a registered "
                f"{kind} provider; registered: {', '.join(self.registered_names(kind))}. "
                "CivicCast ships deterministic mock/local providers by default; "
                "real adapters must be registered before they can be selected."
            )
        return factory()


def default_registry(*, youtube_output: Any = None) -> ProviderRegistry:
    """Registry with CivicCast's shipped (mock/local) providers registered."""

    from civiccast.archive.models import MockInternetArchiveClient, MockLocalNasArchiveClient
    from civiccast.subscribe.delivery import LocalMailbox, LocalWebhookClient
    from civiccast.syndicate.models import MockYouTubeClient

    registry = ProviderRegistry()
    registry.register(PROVIDER_KIND_INTERNET_ARCHIVE, _DEFAULT_NAME, MockInternetArchiveClient)
    registry.register(PROVIDER_KIND_LOCAL_NAS, _DEFAULT_NAME, MockLocalNasArchiveClient)
    registry.register(PROVIDER_KIND_YOUTUBE, _DEFAULT_NAME, MockYouTubeClient)
    registry.register(PROVIDER_KIND_MAIL, _DEFAULT_NAME, LocalMailbox)
    registry.register(PROVIDER_KIND_WEBHOOK, _DEFAULT_NAME, LocalWebhookClient)
    # Real adapters (Beta sprint B5, decision #6). Each factory reads and
    # validates its credentials from the environment at resolution time, so a
    # station that never selects "real" never needs them.
    registry.register(PROVIDER_KIND_INTERNET_ARCHIVE, _REAL_NAME, _real_internet_archive)
    registry.register(PROVIDER_KIND_YOUTUBE, _REAL_NAME, lambda: _real_youtube(youtube_output))
    registry.register(PROVIDER_KIND_MAIL, _REAL_NAME, _real_mail)
    registry.register(PROVIDER_KIND_WEBHOOK, _REAL_NAME, _real_webhook)
    registry.register(PROVIDER_KIND_LOCAL_NAS, _REAL_NAME, _real_local_nas)
    return registry


@dataclass(frozen=True)
class ProviderConfiguration:
    """What a station's configuration says will happen for one provider kind.

    Answers the question a pre-broadcast checklist needs: *after this meeting,
    will this publish surface actually reach the outside world?* Reading the
    env var is not enough — ``real`` selected without its credentials fails at
    resolution time, which is the moment a recording is being published rather
    than a moment an operator is watching.

    ``simulated`` is the default posture: the shipped mock clients produce
    proofs that are explicitly marked as simulated and write nothing anywhere
    (see :mod:`civiccast.archive.models`).
    """

    kind: str
    selected_name: str
    simulated: bool
    usable: bool
    error: str | None = None


def describe_provider(kind: str, registry: ProviderRegistry | None = None) -> ProviderConfiguration:
    """Resolve ``kind`` and report the posture without raising.

    Resolution is side-effect free for every shipped provider: each factory
    reads and validates environment variables and constructs a client object,
    but performs no network or filesystem writes. That makes this safe to call
    from a read-only evaluator such as the pre-broadcast checklist.
    """

    registry = registry or default_registry()
    name = selected_provider_name(kind)
    try:
        registry.resolve(kind)
    except Exception as error:
        # Deliberately broad: any provider's credential validation may raise its
        # own exception type, and a readiness check must report the problem
        # rather than propagate it into the caller's request. The shipped
        # settings classes name missing VARIABLES, never their values.
        return ProviderConfiguration(
            kind=kind,
            selected_name=name,
            simulated=name == _DEFAULT_NAME,
            usable=False,
            error=str(error),
        )
    return ProviderConfiguration(
        kind=kind,
        selected_name=name,
        simulated=name == _DEFAULT_NAME,
        usable=True,
    )


def _real_internet_archive() -> Any:
    from civiccast.archive.internet_archive import (
        InternetArchiveClient,
        InternetArchiveSettings,
    )

    return InternetArchiveClient(InternetArchiveSettings.from_env())


def _real_youtube(live_output: Any = None) -> Any:
    from civiccast.syndicate.youtube import YouTubeClient, YouTubeSettings

    return YouTubeClient(YouTubeSettings.from_env(), live_output=live_output)


def _real_mail() -> Any:
    from civiccast.subscribe.smtp import SmtpMailbox, SmtpSettings

    return SmtpMailbox(SmtpSettings.from_env())


def _real_webhook() -> Any:
    from civiccast.subscribe.webhook import HttpWebhookClient, WebhookSettings

    return HttpWebhookClient(WebhookSettings.from_env())


def _real_local_nas() -> Any:
    from civiccast.archive.local_nas import LocalNasArchiveClient, LocalNasSettings

    return LocalNasArchiveClient(LocalNasSettings.from_env())
