# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Connect YouTube reach output to existing egress and publish stores."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from civiccast.egress.router import get_egress_store
from civiccast.egress.sinks import HlsSink
from civiccast.syndicate.live_output import YouTubeLiveOutput
from civiccast.syndicate.models import YouTubePublishProof


def build_youtube_output(app: Any) -> YouTubeLiveOutput:
    def source(channel_id: str) -> Path:
        store = app.dependency_overrides.get(get_egress_store, get_egress_store)()
        config = store.get_config(channel_id) if store is not None else None
        if config is None or not config.enabled:
            raise ValueError("The selected channel has no enabled broadcast output.")
        for sink in config.sinks:
            if sink.kind == "hls":
                path = Path(HlsSink(sink).manifest_target())
                if path.is_file() and time.time() - path.stat().st_mtime < 30:
                    return path
        raise ValueError(
            "The selected channel has no fresh local HLS output. Start that channel first."
        )

    def record(asset_id: str, proof: YouTubePublishProof) -> None:
        bundle = getattr(app.state, "store_bundle", None)
        store = bundle.publish_store() if bundle is not None else None
        run = store.get_run(asset_id) if store is not None else None
        if store is None or run is None:
            raise RuntimeError("Publish approval is not saved yet; retry status on the next poll.")
        state = proof.delivery_state
        surfaces = []
        for surface in run.surfaces:
            if surface.id != "youtube-live":
                surfaces.append(surface)
                continue
            surfaces.append(
                surface.model_copy(
                    update={
                        "delivery_state": state,
                        "channel_id": proof.channel_id,
                        **{
                            key: getattr(proof, key)
                            for key in (
                                "broadcast_id",
                                "stream_id",
                                "scheduled_start",
                                "scheduled_end",
                            )
                        },
                        "url": proof.url,
                        "state": "succeeded"
                        if state == "live" or (state == "stopped" and proof.confirmed_live)
                        else (
                            "failed"
                            if state == "failed"
                            else ("unverified" if state == "stopped" else "running")
                        ),
                        "health": "ok"
                        if state == "live" or (state == "stopped" and proof.confirmed_live)
                        else ("error" if state == "failed" else "warning"),
                        "completed_at": surface.completed_at or datetime.now(UTC)
                        if state == "live"
                        else surface.completed_at,
                        "message": f"YouTube Live: {state}."
                        + (
                            " Check YouTube Studio and retry; local portal publication is unaffected."
                            if state == "failed"
                            else ""
                        ),
                        "next_step": "Stop the YouTube output when finished."
                        if state == "live"
                        else "Inspect YouTube Studio for destination status.",
                    }
                )
            )
        if surfaces != run.surfaces and not store.replace_surfaces(
            asset_id, run.surfaces, surfaces
        ):
            raise RuntimeError("Publish status changed concurrently; retry on the next poll.")

    return YouTubeLiveOutput(source, record)


def restore_youtube_schedules(app: Any) -> None:
    """Reload saved future/active destinations without creating provider events."""
    import logging

    from civiccast.platform.providers import PROVIDER_KIND_YOUTUBE

    bundle = getattr(app.state, "store_bundle", None)
    if bundle is None:
        return
    try:
        store = bundle.publish_store()
        assets = bundle.asset_store().list_all()
        runs = store.get_runs([asset.asset_id for asset in assets])
    except Exception:
        logging.getLogger(__name__).warning(
            "YouTube schedules could not be read; recover station storage before retrying in Publish."
        )
        return
    for asset_id, run in runs.items():
        for surface in run.surfaces:
            if (
                surface.id != "youtube-live"
                or surface.delivery_state not in {"scheduled", "starting", "live", "failed"}
                or not surface.broadcast_id
                or not surface.stream_id
            ):
                continue
            try:
                client = app.state.provider_registry.resolve(PROVIDER_KIND_YOUTUBE)
                app.state.youtube_live_output.restore(
                    client,
                    asset_id=asset_id,
                    proof=YouTubePublishProof(
                        target_type="youtube_live",
                        credential_key="civiccast-provider://youtube",
                        url=surface.url,
                        delivery_state=surface.delivery_state,
                        confirmed_live=surface.completed_at is not None,
                        **{
                            key: getattr(surface, key)
                            for key in (
                                "broadcast_id",
                                "stream_id",
                                "scheduled_start",
                                "scheduled_end",
                                "channel_id",
                            )
                        },
                    ),
                )
            except Exception:
                logging.getLogger(__name__).warning(
                    "YouTube schedule could not resume; check provider configuration and retry in Publish."
                )
