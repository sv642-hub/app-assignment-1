"""Canary releases: which config version answers each request.

A version is a named set of gateway config. Today that is the mock backend
config; once the Qwen backends are real it will also carry the prompt. The
release names the stable version, an optional canary version, and the share
of requests the canary gets.

The split is decided per request from a hash of the request ID, so it can be
repeated from a saved request. There is no login or session, so one customer's
requests can land on different versions.
"""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Self
from uuid import UUID

from pydantic import Field, StringConstraints, TypeAdapter, model_validator

from app_assignment_1.schemas import StrictModel

VersionId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,39}$")]
BUCKETS = 100


class VersionConfig(StrictModel):
    id: VersionId
    description: str = Field(min_length=1, max_length=200)
    # None means APP_BACKENDS_PATH, so the chaos and outage Compose overrides
    # still change the version they are run with.
    backends_path: Path | None = None


class VersionInfo(StrictModel):
    """What the API shows for a version; paths stay server-side."""

    id: VersionId
    description: str


class ReleaseChange(StrictModel):
    """A requested release. The gateway checks that both versions exist."""

    stable: VersionId
    canary: VersionId | None = None
    canary_percent: int = Field(default=0, ge=0, le=100, strict=True)
    reason: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
    ]

    @model_validator(mode="after")
    def _canary_is_consistent(self) -> Self:
        if self.canary is None and self.canary_percent:
            raise ValueError("canary_percent must be 0 when there is no canary.")
        if self.canary is not None and self.canary == self.stable:
            raise ValueError("The canary must differ from the stable version.")
        return self


class Release(ReleaseChange):
    """A release in effect from `changed_at`. Saved once per change."""

    changed_at: datetime


class ReleaseStatus(StrictModel):
    current: Release
    versions: list[VersionInfo]
    history: list[Release] = Field(description="Newest first.")


def load_versions(path: Path) -> list[VersionConfig]:
    versions = TypeAdapter(list[VersionConfig]).validate_python(
        json.loads(path.read_text())
    )
    if not versions:
        raise ValueError("Configure at least one version")
    ids = [v.id for v in versions]
    if len(set(ids)) != len(ids):
        raise ValueError("Version ids must be unique")
    return versions


def canary_bucket(request_id: UUID) -> int:
    """A bucket from 0 to 99, fixed for a request ID."""
    digest = hashlib.sha256(request_id.bytes).digest()
    return int.from_bytes(digest[:8], "big") % BUCKETS


def pick_version(release: Release, request_id: UUID) -> tuple[str, bool]:
    """The version that answers this request, and whether it is the canary.

    A request goes to the canary when its bucket is below `canary_percent`,
    so raising the percent only moves stable requests to the canary.
    """
    if release.canary is not None and (
        canary_bucket(request_id) < release.canary_percent
    ):
        return release.canary, True
    return release.stable, False
