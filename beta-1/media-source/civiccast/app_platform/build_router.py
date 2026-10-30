# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Staff API for OTT app builds + store submissions (S12 / build step 8, slice 3).

Net-new staff endpoints over the build orchestration + store (slices 1-2). The
DI seams self-resolve a default file-backed store / node build runner from
``request.app.state`` (mirroring ``get_app_platform_config_store``), so no app
factory wiring is required; tests override them. Builds queue with
``setup_admin``; reads/submission edits allow ``publish_operator`` too.
``built_by`` comes from the verified token identity. The build runs the in-tree
generic-shell build (the node toolchain is the platform-coupled seam); device /
store proof is the OTT lab/store lane.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from civiccast.app_platform.build_models import (
    AppBuildRecord,
    StoreSubmissionMetadata,
    SubmissionStatus,
)
from civiccast.app_platform.build_orchestrator import (
    BuildOrchestrationError,
    BuildRunner,
    default_shell_build_runner,
    orchestrate_build,
)
from civiccast.app_platform.build_store import (
    AppBuildStore,
    AppBuildStoreError,
    default_app_build_store_path,
)
from civiccast.app_platform.models import AppBuildTier, AppTarget
from civiccast.app_platform.router import get_app_platform_config_store
from civiccast.app_platform.store import AppPlatformConfigStore
from civiccast.auth.models import OperatorIdentity
from civiccast.auth.roles import require_any_role
from civiccast.installer.storage import default_storage_dir

build_staff_router = APIRouter(prefix="/api/staff/app", tags=["staff", "app-platform"])

_LOG = logging.getLogger(__name__)

_WRITE_ROLES = ("setup_admin", "publish_operator")
_QUEUE_ROLES = ("setup_admin",)
_APP_BUILD_TOOLING_MESSAGE = (
    "App build tooling is not configured in this runtime. Meeting capture and scheduled "
    "recording are unaffected; app-shell builds are optional and require the station app "
    "build toolchain."
)


# --- DI seams (self-resolving, mirror get_app_platform_config_store) --------


def get_app_build_store(request: Request) -> AppBuildStore:
    store = getattr(request.app.state, "app_build_store", None)
    if isinstance(store, AppBuildStore):
        return store
    store = AppBuildStore(default_app_build_store_path())
    request.app.state.app_build_store = store
    return store


def _shells_dir() -> Path:
    # civiccast/app_platform/build_router.py -> civiccast/apps/app-platform-shells
    return (Path(__file__).resolve().parent.parent / "apps" / "app-platform-shells").resolve()


def _artifacts_dir() -> Path:
    configured = os.environ.get("CIVICCAST_APP_BUILD_ARTIFACTS_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return (default_storage_dir() / "app-build-artifacts").expanduser().resolve()


def get_app_build_runner(request: Request) -> BuildRunner:
    runner = getattr(request.app.state, "app_build_runner", None)
    if callable(runner):
        return cast(BuildRunner, runner)
    return default_shell_build_runner(_shells_dir())


def _operator_id(request: Request) -> str:
    identity = getattr(request.state, "operator_identity", None)
    if not isinstance(identity, OperatorIdentity):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Staff identity is required for this action.",
        )
    return identity.operator_id


def _build_failure_detail(exc: BuildOrchestrationError) -> str:
    message = str(exc).lower()
    if "node" in message or "app shell build command failed" in message:
        return _APP_BUILD_TOOLING_MESSAGE
    if "not a buildable app shell target" in message:
        return "That app target is not buildable from App Admin."
    return f"Build failed ({type(exc).__name__}). Check server logs for details."


# --- bodies -----------------------------------------------------------------


class BuildRequest(BaseModel):
    """Body for POST /builds."""

    model_config = ConfigDict(extra="forbid")

    app_target: AppTarget
    build_tier: AppBuildTier | None = None


class StoreSubmissionUpdate(BaseModel):
    """Body for PATCH /store-submissions/{app_target} (only set fields applied)."""

    model_config = ConfigDict(extra="forbid")

    store_account_email: Annotated[str | None, Field(default=None, max_length=200)] = None
    package_id: Annotated[str | None, Field(default=None, max_length=200)] = None
    version_code: Annotated[int | None, Field(default=None, ge=0)] = None
    version_name: Annotated[str | None, Field(default=None, min_length=1, max_length=40)] = None
    submission_status: SubmissionStatus | None = None
    submission_notes: Annotated[str | None, Field(default=None, max_length=2000)] = None
    published_url: Annotated[str | None, Field(default=None, max_length=500)] = None
    support_contact: Annotated[str | None, Field(default=None, max_length=200)] = None


# --- build records ----------------------------------------------------------


@build_staff_router.get(
    "/builds",
    response_model=list[AppBuildRecord],
    summary="List OTT app build records (newest first)",
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
)
def list_builds(
    app_target: str | None = Query(default=None),
    store: AppBuildStore = Depends(get_app_build_store),
) -> list[AppBuildRecord]:
    return store.list_builds(app_target=app_target)


@build_staff_router.get(
    "/builds/{record_id}",
    response_model=AppBuildRecord,
    summary="Read one OTT app build record",
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    responses={404: {"description": "Build record not found"}},
)
def get_build(
    record_id: str, store: AppBuildStore = Depends(get_app_build_store)
) -> AppBuildRecord:
    record = store.get_build(record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No build {record_id!r}")
    return record


@build_staff_router.post(
    "/builds",
    response_model=AppBuildRecord,
    status_code=status.HTTP_201_CREATED,
    summary="Build + record one OTT app target (local artifact, SHA-256 verified)",
    dependencies=[Depends(require_any_role(*_QUEUE_ROLES))],
    responses={422: {"description": "Target not buildable / artifact missing"}},
)
def create_build(
    body: BuildRequest,
    request: Request,
    store: AppBuildStore = Depends(get_app_build_store),
    config_store: AppPlatformConfigStore = Depends(get_app_platform_config_store),
    runner: BuildRunner = Depends(get_app_build_runner),
) -> AppBuildRecord:
    work_dir = _artifacts_dir() / secrets.token_hex(8)
    try:
        return orchestrate_build(
            config=config_store.read_config(),
            app_target=body.app_target,
            store=store,
            work_dir=work_dir,
            build_runner=runner,
            build_tier=body.build_tier,
            built_by=_operator_id(request),
        )
    except BuildOrchestrationError as exc:
        # Log the full exception (it can include the server work-dir path) but
        # return only the exception TYPE to the operator — never the raw message
        # / filesystem path (matches the commit_service error-detail pattern).
        _LOG.error("OTT build failed for target %r: %s", body.app_target, exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=_build_failure_detail(exc),
        ) from exc
    except FileNotFoundError as exc:
        _LOG.error("OTT build tooling is missing for target %r: %s", body.app_target, exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=_APP_BUILD_TOOLING_MESSAGE,
        ) from exc
    except AppBuildStoreError as exc:
        # Build artifact was produced but the record could not be persisted
        # (disk full / permission). Surface a clean 503, not an unhandled 500.
        _LOG.error("OTT build artifact produced but not recorded: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Build artifact produced but could not be recorded "
                f"({type(exc).__name__}). Check storage and retry."
            ),
        ) from exc


@build_staff_router.get(
    "/builds/{record_id}/download",
    summary="Download a build artifact",
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    responses={404: {"description": "Build record or artifact not found"}},
)
def download_build(
    record_id: str, store: AppBuildStore = Depends(get_app_build_store)
) -> FileResponse:
    record = store.get_build(record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No build {record_id!r}")
    artifact = Path(record.artifact_path)
    # Defence-in-depth: the production write path always lands under
    # _artifacts_dir(), but a tampered store file could point artifact_path
    # anywhere readable by the server. Refuse to serve anything outside the
    # managed directory before touching the filesystem.
    try:
        artifact.resolve().relative_to(_artifacts_dir())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Build artifact path is outside the managed artifacts directory.",
        ) from exc
    if not artifact.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Build artifact file is no longer present on disk.",
        )
    return FileResponse(
        path=str(artifact),
        filename=artifact.name,
        media_type="application/octet-stream",
    )


# --- store submissions ------------------------------------------------------


@build_staff_router.get(
    "/store-submissions",
    response_model=list[StoreSubmissionMetadata],
    summary="List external store submission trackers",
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
)
def list_store_submissions(
    store: AppBuildStore = Depends(get_app_build_store),
) -> list[StoreSubmissionMetadata]:
    return store.list_submissions()


@build_staff_router.patch(
    "/store-submissions/{app_target}",
    response_model=StoreSubmissionMetadata,
    summary="Update an external store submission tracker (operator-maintained)",
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
)
def update_store_submission(
    app_target: AppTarget,
    body: StoreSubmissionUpdate,
    request: Request,
    store: AppBuildStore = Depends(get_app_build_store),
) -> StoreSubmissionMetadata:
    existing = store.get_submission(app_target)
    base: dict[str, Any] = (
        existing.model_dump() if existing is not None else {"app_target": app_target}
    )
    # Stamp who/when on every mutation so status transitions (draft->approved
    # ->rejected->published) are auditable, mirroring AppBuildRecord.built_by.
    merged = {
        **base,
        **body.model_dump(exclude_unset=True),
        "app_target": app_target,
        "updated_by": _operator_id(request),
        "updated_at": datetime.now(UTC),
    }
    return store.upsert_submission(StoreSubmissionMetadata.model_validate(merged))
