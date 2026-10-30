# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""In-memory facility integration inventory store."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

from civiccast.facility.models import (
    RouterEndpoint,
    RouterInput,
    RouterInventory,
    RouterOutput,
)


class FacilityRouteNotFoundError(Exception):
    """Raised when a router endpoint, source, or destination cannot be found."""

    def __init__(self, kind: str, identifier: str) -> None:
        self.kind = kind
        self.identifier = identifier
        super().__init__(f"{kind} {identifier!r} not found")


class InMemoryFacilityRouterStore:
    """Small router inventory store for tests and local operator previews."""

    def __init__(
        self,
        *,
        endpoints: list[RouterEndpoint],
        sources: list[RouterInput],
        destinations: list[RouterOutput],
    ) -> None:
        self._endpoints = {endpoint.endpoint_id: endpoint for endpoint in endpoints}
        self._sources = {source.input_id: source for source in sources}
        self._destinations = {destination.output_id: destination for destination in destinations}

    @classmethod
    def default(cls) -> InMemoryFacilityRouterStore:
        """Return a safe demo inventory with no real device secret material."""

        return cls(
            endpoints=[
                RouterEndpoint(
                    endpoint_id="control-room-router",
                    label="Control room router",
                    vendor="blackmagic-design",
                    protocol="blackmagic-videohub",
                    transport="tcp",
                    host="192.0.2.10",
                    port=9990,
                    notes="Example endpoint for operator training and tests.",
                )
            ],
            sources=[
                RouterInput(
                    input_id="council-chamber",
                    label="Council chamber",
                    physical_port="1",
                    live_source_id="rtmp-cam-01",
                ),
                RouterInput(
                    input_id="bulletin-board",
                    label="Bulletin board",
                    physical_port="2",
                ),
            ],
            destinations=[
                RouterOutput(
                    output_id="civiccast-capture",
                    label="CivicCast capture",
                    physical_port="7",
                    channel_id="government",
                )
            ],
        )

    def inventory(self) -> RouterInventory:
        return RouterInventory(
            generated_at=datetime.now(UTC),
            endpoints=self.list_endpoints(),
            sources=self.list_sources(),
            destinations=self.list_destinations(),
            proof_boundary="Inventory and command planning only; hardware send is not performed.",
        )

    def list_endpoints(self) -> list[RouterEndpoint]:
        return [
            deepcopy(row) for row in sorted(self._endpoints.values(), key=lambda row: row.label)
        ]

    def list_sources(self) -> list[RouterInput]:
        return [deepcopy(row) for row in sorted(self._sources.values(), key=lambda row: row.label)]

    def list_destinations(self) -> list[RouterOutput]:
        return [
            deepcopy(row) for row in sorted(self._destinations.values(), key=lambda row: row.label)
        ]

    def get_endpoint(self, endpoint_id: str) -> RouterEndpoint:
        endpoint = self._endpoints.get(endpoint_id)
        if endpoint is None:
            raise FacilityRouteNotFoundError("router endpoint", endpoint_id)
        return deepcopy(endpoint)

    def get_source(self, source_id: str) -> RouterInput:
        source = self._sources.get(source_id)
        if source is None:
            raise FacilityRouteNotFoundError("router source", source_id)
        return deepcopy(source)

    def get_destination(self, destination_id: str) -> RouterOutput:
        destination = self._destinations.get(destination_id)
        if destination is None:
            raise FacilityRouteNotFoundError("router destination", destination_id)
        return deepcopy(destination)
