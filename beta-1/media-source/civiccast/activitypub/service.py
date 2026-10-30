# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""ActivityPub document builders and inbox handling."""

from __future__ import annotations

import hashlib
import html
import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from civiccast._version import __version__
from civiccast.activitypub.config import ActivityPubConfig
from civiccast.activitypub.models import (
    DeliveryRecord,
    FollowerRecord,
    FollowerStatus,
    OutboxRecord,
)
from civiccast.activitypub.rate_limit import InboxRateLimiter
from civiccast.activitypub.remote import (
    ActivityPubDeliveryClient,
    ActivityPubRemoteError,
    DeliveryResult,
    RemoteActor,
    RemoteActorFetcher,
)
from civiccast.activitypub.signatures import (
    HttpSignatureError,
    parse_signature_header,
    verify_http_signature,
)
from civiccast.activitypub.store import ActivityPubStore, new_delivery_id
from civiccast.publish.models import PublishAssetStatus

ACTIVITYSTREAMS_CONTEXT = "https://www.w3.org/ns/activitystreams"


class ActivityPubError(ValueError):
    """Base class for local ActivityPub contract errors."""


class ActivityPubBlockedError(ActivityPubError):
    """Raised when a remote instance is blocked by operator policy."""


class ActivityPubDisabledError(ActivityPubError):
    """Raised when federation is disabled for the deployment."""


class ActivityPubRateLimitError(ActivityPubError):
    """Raised when inbox traffic exceeds the configured rate limit."""


class ActivityPubSignatureError(ActivityPubError):
    """Raised when a signed federation request fails verification."""


class ActivityPubPolicyError(ActivityPubError):
    """Raised when a remote instance is outside the station federation policy."""


def normalized_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def actor_id(base_url: str) -> str:
    return f"{normalized_base_url(base_url)}/ap/actor"


def inbox_url(base_url: str) -> str:
    return f"{normalized_base_url(base_url)}/ap/inbox"


def outbox_url(base_url: str) -> str:
    return f"{normalized_base_url(base_url)}/ap/outbox"


def followers_url(base_url: str) -> str:
    return f"{normalized_base_url(base_url)}/ap/followers"


def actor_domain(actor: str) -> str:
    parsed = urlparse(actor)
    if not parsed.scheme or not parsed.netloc:
        raise ActivityPubError("ActivityPub actor must be an absolute URL.")
    return parsed.hostname.lower() if parsed.hostname else parsed.netloc.lower()


def webfinger_document(*, base_url: str, host: str, config: ActivityPubConfig) -> dict[str, Any]:
    return {
        "subject": f"acct:{config.handle}@{host}",
        "aliases": [actor_id(base_url)],
        "links": [
            {
                "rel": "self",
                "type": "application/activity+json",
                "href": actor_id(base_url),
            }
        ],
    }


def actor_document(*, base_url: str, config: ActivityPubConfig) -> dict[str, Any]:
    local_actor = actor_id(base_url)
    return {
        "@context": [
            ACTIVITYSTREAMS_CONTEXT,
            "https://w3id.org/security/v1",
        ],
        "id": local_actor,
        "type": "Application",
        "preferredUsername": config.handle,
        "name": config.display_name,
        "summary": "CivicCast station publication account.",
        "inbox": inbox_url(base_url),
        "outbox": outbox_url(base_url),
        "followers": followers_url(base_url),
        "publicKey": {
            "id": f"{local_actor}#main-key",
            "owner": local_actor,
            "publicKeyPem": config.public_key_pem,
        },
    }


def nodeinfo_document(*, base_url: str) -> dict[str, Any]:
    return {
        "version": "2.0",
        "software": {"name": "civiccast", "version": __version__},
        "protocols": ["activitypub"],
        "services": {"inbound": [], "outbound": []},
        "openRegistrations": False,
        "usage": {"users": {"total": 1}},
        "metadata": {
            "actor": actor_id(base_url),
            "externalFediverseProof": "available_when_enabled",
        },
    }


def handle_follow_activity(
    *,
    activity: dict[str, Any],
    raw_body: bytes,
    method: str,
    path_and_query: str,
    headers: dict[str, str],
    base_url: str,
    config: ActivityPubConfig,
    store: ActivityPubStore,
    rate_limiter: InboxRateLimiter,
    actor_fetcher: RemoteActorFetcher,
    delivery_client: ActivityPubDeliveryClient,
) -> tuple[int, dict[str, Any]]:
    return handle_inbox_activity(
        activity=activity,
        raw_body=raw_body,
        method=method,
        path_and_query=path_and_query,
        headers=headers,
        base_url=base_url,
        config=config,
        store=store,
        rate_limiter=rate_limiter,
        actor_fetcher=actor_fetcher,
        delivery_client=delivery_client,
    )


def handle_inbox_activity(
    *,
    activity: dict[str, Any],
    raw_body: bytes,
    method: str,
    path_and_query: str,
    headers: dict[str, str],
    base_url: str,
    config: ActivityPubConfig,
    store: ActivityPubStore,
    rate_limiter: InboxRateLimiter,
    actor_fetcher: RemoteActorFetcher,
    delivery_client: ActivityPubDeliveryClient,
) -> tuple[int, dict[str, Any]]:
    if config.federation_mode == "disabled":
        raise ActivityPubDisabledError(
            "ActivityPub federation is disabled for this CivicCast deployment."
        )
    activity_type = activity.get("type")
    if activity_type == "Follow":
        return _handle_follow_activity(
            activity=activity,
            raw_body=raw_body,
            method=method,
            path_and_query=path_and_query,
            headers=headers,
            base_url=base_url,
            config=config,
            store=store,
            rate_limiter=rate_limiter,
            actor_fetcher=actor_fetcher,
            delivery_client=delivery_client,
        )
    if activity_type == "Undo":
        return _handle_undo_activity(
            activity=activity,
            raw_body=raw_body,
            method=method,
            path_and_query=path_and_query,
            headers=headers,
            config=config,
            store=store,
            rate_limiter=rate_limiter,
            actor_fetcher=actor_fetcher,
        )
    raise ActivityPubError(
        "Only ActivityPub Follow and Undo activities are accepted by this inbox."
    )


def _handle_follow_activity(
    *,
    activity: dict[str, Any],
    raw_body: bytes,
    method: str,
    path_and_query: str,
    headers: dict[str, str],
    base_url: str,
    config: ActivityPubConfig,
    store: ActivityPubStore,
    rate_limiter: InboxRateLimiter,
    actor_fetcher: RemoteActorFetcher,
    delivery_client: ActivityPubDeliveryClient,
) -> tuple[int, dict[str, Any]]:

    remote_actor = activity.get("actor")
    activity_id_value = activity.get("id")
    if not isinstance(remote_actor, str) or not isinstance(activity_id_value, str):
        raise ActivityPubError("Follow activity must include string actor and id fields.")

    domain = actor_domain(remote_actor)
    remote = _fetch_and_verify_actor(
        actor_url=remote_actor,
        method=method,
        path_and_query=path_and_query,
        headers=headers,
        raw_body=raw_body,
        actor_fetcher=actor_fetcher,
        require_digest=True,
    )
    if remote.actor_id.rstrip("/") != remote_actor.rstrip("/"):
        raise ActivityPubSignatureError("Signed ActivityPub actor does not match Follow.actor.")
    if config.federation_mode == "limited" and domain not in config.allowed_instances:
        raise ActivityPubPolicyError(
            f"ActivityPub follow rejected because {domain} is not in the allowlist."
        )
    if domain in config.blocked_instances:
        store.upsert_follower(
            FollowerRecord(
                actor=remote_actor,
                domain=domain,
                status="blocked",
                activity_id=activity_id_value,
                inbox_url=remote.inbox,
                shared_inbox_url=remote.shared_inbox,
                public_key_id=remote.public_key_id,
                public_key_pem=remote.public_key_pem,
                created_at=datetime.now(UTC),
            )
        )
        raise ActivityPubBlockedError(
            f"ActivityPub follow blocked because {domain} is in the instance blocklist."
        )

    if not rate_limiter.allow(
        domain,
        limit=config.inbox_rate_limit,
        window_seconds=config.inbox_rate_window_seconds,
    ):
        raise ActivityPubRateLimitError(
            "ActivityPub inbox rate limit reached for this remote instance. Try again later."
        )

    follower_status: FollowerStatus = (
        "pending" if config.federation_mode == "approval-only" else "accepted"
    )
    store.upsert_follower(
        FollowerRecord(
            actor=remote_actor,
            domain=domain,
            status=follower_status,
            activity_id=activity_id_value,
            inbox_url=remote.inbox,
            shared_inbox_url=remote.shared_inbox,
            public_key_id=remote.public_key_id,
            public_key_pem=remote.public_key_pem,
            created_at=datetime.now(UTC),
        )
    )
    if follower_status == "pending":
        return (
            202,
            {
                "status": "pending_operator_approval",
                "message": "Follow request received and queued for operator approval.",
            },
        )
    return (
        202,
        _deliver_accept(
            base_url=base_url,
            follow_activity=activity,
            inbox_url=remote.shared_inbox or remote.inbox,
            delivery_client=delivery_client,
            store=store,
        ),
    )


def _handle_undo_activity(
    *,
    activity: dict[str, Any],
    raw_body: bytes,
    method: str,
    path_and_query: str,
    headers: dict[str, str],
    config: ActivityPubConfig,
    store: ActivityPubStore,
    rate_limiter: InboxRateLimiter,
    actor_fetcher: RemoteActorFetcher,
) -> tuple[int, dict[str, Any]]:
    remote_actor = activity.get("actor")
    activity_id_value = activity.get("id")
    if not isinstance(remote_actor, str) or not isinstance(activity_id_value, str):
        raise ActivityPubError("Undo activity must include string actor and id fields.")
    object_value = activity.get("object")
    follow_activity_id = activity_id_value
    if isinstance(object_value, dict):
        if object_value.get("type") != "Follow":
            raise ActivityPubError("Only Undo of Follow activities is accepted by this inbox.")
        if object_value.get("actor") != remote_actor:
            raise ActivityPubSignatureError("Undo actor must match the embedded Follow actor.")
        follow_id = object_value.get("id")
        if isinstance(follow_id, str) and follow_id:
            follow_activity_id = follow_id
    elif isinstance(object_value, str):
        follow_activity_id = object_value
    else:
        raise ActivityPubError("Undo activity must include a Follow object or Follow id.")

    domain = actor_domain(remote_actor)
    remote = _fetch_and_verify_actor(
        actor_url=remote_actor,
        method=method,
        path_and_query=path_and_query,
        headers=headers,
        raw_body=raw_body,
        actor_fetcher=actor_fetcher,
        require_digest=True,
    )
    if remote.actor_id.rstrip("/") != remote_actor.rstrip("/"):
        raise ActivityPubSignatureError("Signed ActivityPub actor does not match Undo.actor.")
    if not rate_limiter.allow(
        domain,
        limit=config.inbox_rate_limit,
        window_seconds=config.inbox_rate_window_seconds,
    ):
        raise ActivityPubRateLimitError(
            "ActivityPub inbox rate limit reached for this remote instance. Try again later."
        )

    existing = store.get_follower(remote_actor)
    if existing is not None and existing.status == "blocked":
        return (
            202,
            {
                "status": "blocked",
                "message": "Undo received, but the follower remains blocked by operator policy.",
            },
        )
    if existing is None:
        store.upsert_follower(
            FollowerRecord(
                actor=remote_actor,
                domain=domain,
                status="removed",
                activity_id=follow_activity_id,
                inbox_url=remote.inbox,
                shared_inbox_url=remote.shared_inbox,
                public_key_id=remote.public_key_id,
                public_key_pem=remote.public_key_pem,
                created_at=datetime.now(UTC),
            )
        )
    else:
        store.set_follower_status(actor=remote_actor, status="removed")
    return (
        202,
        {
            "status": "removed",
            "message": "Follow relationship removed.",
        },
    )


def followers_collection(*, base_url: str, store: ActivityPubStore) -> dict[str, Any]:
    followers = [str(record.actor).rstrip("/") for record in store.list_followers()]
    return {
        "@context": ACTIVITYSTREAMS_CONTEXT,
        "id": followers_url(base_url),
        "type": "OrderedCollection",
        "totalItems": len(followers),
        "orderedItems": followers,
    }


def outbox_collection(*, base_url: str, store: ActivityPubStore) -> dict[str, Any]:
    records = store.list_outbox()
    return {
        "@context": ACTIVITYSTREAMS_CONTEXT,
        "id": outbox_url(base_url),
        "type": "OrderedCollection",
        "totalItems": len(records),
        "orderedItems": [record.activity for record in records],
    }


def record_publish_activity(
    *,
    base_url: str,
    status: PublishAssetStatus,
    store: ActivityPubStore,
) -> OutboxRecord | None:
    portal_surface = next(
        (surface for surface in status.surfaces if surface.id == "portal" and surface.url),
        None,
    )
    if portal_surface is None or not status.canonical_public:
        return None
    local_actor = actor_id(base_url)
    published_at = datetime.now(UTC)
    note_id = f"{local_actor}/notes/{status.asset_id}"
    activity_id_value = f"{local_actor}/activities/create-{status.asset_id}"
    content = html.escape(f"New CivicCast recording published: {status.title}.")
    activity: dict[str, Any] = {
        "@context": ACTIVITYSTREAMS_CONTEXT,
        "id": activity_id_value,
        "type": "Create",
        "actor": local_actor,
        "published": published_at.isoformat().replace("+00:00", "Z"),
        "to": [ACTIVITYSTREAMS_CONTEXT + "#Public"],
        "object": {
            "id": note_id,
            "type": "Note",
            "attributedTo": local_actor,
            "content": content,
            "url": portal_surface.url,
            "published": published_at.isoformat().replace("+00:00", "Z"),
        },
    }
    return store.append_outbox(
        OutboxRecord(
            activity_id=activity_id_value,
            activity=activity,
            created_at=published_at,
        )
    )


def require_authorized_fetch(
    *,
    method: str,
    path_and_query: str,
    headers: dict[str, str],
    base_url: str,
    config: ActivityPubConfig,
    actor_fetcher: RemoteActorFetcher,
) -> None:
    """Verify signed GET fetches when the operator enables authorized fetch."""

    if not config.authorized_fetch:
        return
    signature_header = headers.get("signature") or headers.get("Signature")
    if not signature_header:
        raise ActivityPubSignatureError("Authorized fetch requires a valid ActivityPub signature.")
    try:
        params = parse_signature_header(signature_header)
    except HttpSignatureError as exc:
        raise ActivityPubSignatureError(str(exc)) from exc
    remote = _fetch_remote_actor_for_key(params.key_id, actor_fetcher)
    try:
        verify_http_signature(
            method=method,
            path_and_query=path_and_query,
            headers=headers,
            body=b"",
            public_key_pem=remote.public_key_pem,
            require_digest=False,
        )
    except HttpSignatureError as exc:
        raise ActivityPubSignatureError(str(exc)) from exc
    _enforce_policy(config=config, domain=actor_domain(remote.actor_id))


def approve_pending_follower(
    *,
    actor: str,
    base_url: str,
    config: ActivityPubConfig,
    store: ActivityPubStore,
    delivery_client: ActivityPubDeliveryClient,
) -> FollowerRecord:
    """Approve a queued follower and deliver a signed Accept."""

    record = store.get_follower(actor)
    if record is None:
        raise ActivityPubError(f"Unknown ActivityPub follower: {actor}")
    if record.status != "pending":
        raise ActivityPubError("Only pending ActivityPub followers can be approved.")
    _enforce_policy(config=config, domain=record.domain)
    approved = store.set_follower_status(actor=actor, status="accepted")
    if approved is None:
        raise ActivityPubError(f"Unknown ActivityPub follower: {actor}")
    follow_activity = {
        "id": approved.activity_id,
        "type": "Follow",
        "actor": approved.actor,
        "object": actor_id(base_url),
    }
    _deliver_accept(
        base_url=base_url,
        follow_activity=follow_activity,
        inbox_url=approved.shared_inbox_url or approved.inbox_url,
        delivery_client=delivery_client,
        store=store,
    )
    return approved


def reject_pending_follower(
    *,
    actor: str,
    base_url: str,
    store: ActivityPubStore,
    delivery_client: ActivityPubDeliveryClient,
) -> FollowerRecord:
    """Reject a queued follower and deliver a signed Reject."""

    record = store.get_follower(actor)
    if record is None:
        raise ActivityPubError(f"Unknown ActivityPub follower: {actor}")
    if record.status != "pending":
        raise ActivityPubError("Only pending ActivityPub followers can be rejected.")
    rejected = store.set_follower_status(actor=actor, status="rejected")
    if rejected is None:
        raise ActivityPubError(f"Unknown ActivityPub follower: {actor}")
    follow_activity = {
        "id": rejected.activity_id,
        "type": "Follow",
        "actor": rejected.actor,
        "object": actor_id(base_url),
    }
    _deliver_reject(
        base_url=base_url,
        follow_activity=follow_activity,
        inbox_url=rejected.shared_inbox_url or rejected.inbox_url,
        delivery_client=delivery_client,
        store=store,
    )
    return rejected


def block_follower(*, actor: str, store: ActivityPubStore) -> FollowerRecord:
    blocked = store.set_follower_status(actor=actor, status="blocked")
    if blocked is None:
        raise ActivityPubError(f"Unknown ActivityPub follower: {actor}")
    return blocked


def deliver_publish_activity(
    *,
    record: OutboxRecord,
    store: ActivityPubStore,
    delivery_client: ActivityPubDeliveryClient,
    now: datetime | None = None,
) -> list[DeliveryRecord]:
    """Deliver to every accepted follower; queue failures for the retry worker.

    A delivery that fails (network error → status 0, or HTTP >= 400) is
    enqueued as a durable retry row (Stage F) so a follower inbox that is down
    at publish time still hears about the recording later.

    ``now`` is the clock used when enqueuing a failed delivery's first
    ``next_attempt_at``; it defaults to wall-clock. Tests inject a fixed clock
    so the enqueue time matches the time they scan the worker with (without it,
    enqueue uses real ``datetime.now`` and a fixed-past scan time looks
    not-yet-due).
    """

    from civiccast.activitypub.retry_worker import enqueue_failed_delivery

    deliveries: list[DeliveryRecord] = []
    for follower in store.list_followers(status="accepted"):
        inbox = follower.shared_inbox_url or follower.inbox_url
        result = delivery_client.deliver(inbox_url=inbox, activity=record.activity)
        deliveries.append(_record_delivery(record.activity_id, result, store))
        if result.status_code == 0 or result.status_code >= 400:
            enqueue_failed_delivery(
                store=store,
                activity_id=record.activity_id,
                inbox_url=inbox,
                activity=dict(record.activity),
                status_code=result.status_code,
                error=result.response_body,
                now=now,
            )
    return deliveries


def _fetch_and_verify_actor(
    *,
    actor_url: str,
    method: str,
    path_and_query: str,
    headers: dict[str, str],
    raw_body: bytes,
    actor_fetcher: RemoteActorFetcher,
    require_digest: bool,
) -> RemoteActor:
    signature_header = headers.get("signature") or headers.get("Signature")
    if not signature_header:
        raise ActivityPubSignatureError("ActivityPub inbox requires HTTP Signature.")
    try:
        params = parse_signature_header(signature_header)
    except HttpSignatureError as exc:
        raise ActivityPubSignatureError(str(exc)) from exc
    remote = _fetch_remote_actor_for_key(params.key_id, actor_fetcher, fallback_actor_url=actor_url)
    try:
        verify_http_signature(
            method=method,
            path_and_query=path_and_query,
            headers=headers,
            body=raw_body,
            public_key_pem=remote.public_key_pem,
            require_digest=require_digest,
        )
    except HttpSignatureError as exc:
        raise ActivityPubSignatureError(str(exc)) from exc
    if params.key_id != remote.public_key_id:
        raise ActivityPubSignatureError("ActivityPub keyId does not match the actor public key.")
    return remote


def _fetch_remote_actor_for_key(
    key_id: str,
    actor_fetcher: RemoteActorFetcher,
    *,
    fallback_actor_url: str | None = None,
) -> RemoteActor:
    key_actor_url = key_id.split("#", 1)[0]
    target = key_actor_url or fallback_actor_url
    if not target:
        raise ActivityPubSignatureError("ActivityPub keyId does not identify an actor.")
    try:
        return actor_fetcher.fetch(target)
    except ActivityPubRemoteError as exc:
        raise ActivityPubSignatureError(str(exc)) from exc


def _enforce_policy(*, config: ActivityPubConfig, domain: str) -> None:
    if domain in config.blocked_instances:
        raise ActivityPubBlockedError(
            f"ActivityPub request blocked because {domain} is in the instance blocklist."
        )
    if config.federation_mode == "limited" and domain not in config.allowed_instances:
        raise ActivityPubPolicyError(
            f"ActivityPub request rejected because {domain} is not in the allowlist."
        )


def _deliver_accept(
    *,
    base_url: str,
    follow_activity: dict[str, Any],
    inbox_url: str,
    delivery_client: ActivityPubDeliveryClient,
    store: ActivityPubStore,
) -> dict[str, Any]:
    return _deliver_follow_response(
        response_type="Accept",
        base_url=base_url,
        follow_activity=follow_activity,
        inbox_url=inbox_url,
        delivery_client=delivery_client,
        store=store,
    )


def _deliver_reject(
    *,
    base_url: str,
    follow_activity: dict[str, Any],
    inbox_url: str,
    delivery_client: ActivityPubDeliveryClient,
    store: ActivityPubStore,
) -> dict[str, Any]:
    return _deliver_follow_response(
        response_type="Reject",
        base_url=base_url,
        follow_activity=follow_activity,
        inbox_url=inbox_url,
        delivery_client=delivery_client,
        store=store,
    )


def _deliver_follow_response(
    *,
    response_type: str,
    base_url: str,
    follow_activity: dict[str, Any],
    inbox_url: str,
    delivery_client: ActivityPubDeliveryClient,
    store: ActivityPubStore,
) -> dict[str, Any]:
    local_actor = actor_id(base_url)
    digest = hashlib.sha256(
        json.dumps(
            {
                "type": response_type,
                "follow": follow_activity,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:24]
    activity_id_value = f"{local_actor}/{response_type.lower()}s/{digest}"
    accept_activity: dict[str, object] = {
        "@context": ACTIVITYSTREAMS_CONTEXT,
        "id": activity_id_value,
        "type": response_type,
        "actor": local_actor,
        "object": follow_activity,
    }
    store.append_outbox(
        OutboxRecord(
            activity_id=activity_id_value,
            activity=accept_activity,
            created_at=datetime.now(UTC),
        )
    )
    result = delivery_client.deliver(inbox_url=inbox_url, activity=accept_activity)
    _record_delivery(activity_id_value, result, store)
    return {
        "status": "accepted" if response_type == "Accept" else "rejected",
        "activity_id": activity_id_value,
        "delivery_status_code": result.status_code,
    }


def _record_delivery(
    activity_id_value: str,
    result: DeliveryResult,
    store: ActivityPubStore,
) -> DeliveryRecord:
    return store.append_delivery(
        DeliveryRecord(
            delivery_id=new_delivery_id(),
            activity_id=activity_id_value,
            inbox_url=result.inbox_url,
            status_code=result.status_code,
            response_body=result.response_body,
            created_at=result.delivered_at,
        )
    )
