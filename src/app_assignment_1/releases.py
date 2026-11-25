"""Model releases: which version each model serves, and where a rollback goes.

Every model starts on `base`, its entry in APP_BACKENDS_PATH. The catalog in
APP_RELEASES_PATH lists other versions per model, each a set of changes to the
base config. With mock backends a version changes only mock settings (latency
and simulated faults); with real backends it will carry a prompt version.

An operator deploys a version (POST /v1/operator/models/{name}/release). The
controller then watches the model for `release_watch_samples` calls: if the
failure rate crosses the threshold in that time, it rolls the release back
instead of disabling the model (see monitor.Controller). After the watch the
release is confirmed. An operator can also roll back by hand.

Each change is a ReleaseEvent. Operator changes are saved before they apply;
the controller's rollbacks apply at once and wait in `unsaved` until the
gateway saves them with the interventions. A restarted gateway with Postgres
reloads the newest event per model (`restore`).

The rollback target is the last version known to be good: on a deploy it is
the current version if that one is stable, else the target stays as it was,
since an unconfirmed release is no place to roll back to.
"""

import logging
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from pydantic import StringConstraints, TypeAdapter

from app_assignment_1.backends import Backend
from app_assignment_1.schemas import (
    BASE_VERSION,
    VERSION_PATTERN,
    BackendConfig,
    BackendName,
    ModelRelease,
    ReleaseActor,
    ReleaseEvent,
    ReleaseVersion,
)

logger = logging.getLogger(__name__)
Catalog = dict[BackendName, dict[str, ReleaseVersion]]
_CATALOG = TypeAdapter(Catalog)
_VERSION_NAME: TypeAdapter[str] = TypeAdapter(
    Annotated[str, StringConstraints(pattern=VERSION_PATTERN)]
)


class UnknownVersion(Exception):
    """The model has no version by that name."""


class AlreadyServing(Exception):
    """The model already serves that version."""


class NoRollbackTarget(Exception):
    """The model has no version to roll back to."""


def load_catalog(path: Path) -> Catalog:
    """Read the versions each model can be deployed to. A missing file means
    no versions beyond base."""
    if not path.exists():
        return {}
    catalog = _CATALOG.validate_json(path.read_text(encoding="utf-8"))
    for name, versions in catalog.items():
        for version in versions:
            if version == BASE_VERSION:
                raise ValueError(f"{name}: '{BASE_VERSION}' is reserved")
            _VERSION_NAME.validate_python(version)
    return catalog


class Releases:
    def __init__(
        self,
        backends: Sequence[Backend],
        catalog: Catalog,
        watch_samples: int,
    ):
        self.backends = {b.config.name: b for b in backends}
        unknown = set(catalog) - set(self.backends)
        if unknown:
            raise ValueError(f"Release catalog names unknown models: {unknown}")
        # Copied before any fault is set at runtime, so base means the file.
        self.base = {
            name: b.config.model_copy(deep=True) for name, b in self.backends.items()
        }
        self.catalog = catalog
        self.watch_samples = watch_samples
        self.state = {
            name: ModelRelease(
                name=name,
                version=BASE_VERSION,
                status="stable",
                watch_samples=watch_samples,
                versions=self.versions(name),
            )
            for name in self.backends
        }
        self.unsaved: list[ReleaseEvent] = []
        # Goes up each time a model's config changes. A call records the value
        # it started with, so an outcome from an older version is not counted
        # against the new one.
        self.generation: dict[BackendName, int] = dict.fromkeys(self.backends, 0)
        # The last release revision handed out per model (see ReleaseEvent).
        # Taken when an event is planned, so an operator change planned
        # before a controller change always has the lower number.
        self.revision: dict[BackendName, int] = dict.fromkeys(self.backends, 0)
        # Called after every change, with the event. The gateway starts or
        # ends the controller's watch and resets the monitor windows.
        self.on_change: list[Callable[[ReleaseEvent], None]] = []

    def _next_revision(self, name: BackendName) -> int:
        self.revision[name] += 1
        return self.revision[name]

    def versions(self, name: BackendName) -> list[str]:
        return [BASE_VERSION, *self.catalog.get(name, {})]

    def config_for(self, name: BackendName, version: str) -> BackendConfig:
        base = self.base[name]
        if version == BASE_VERSION:
            return base.model_copy(deep=True)
        try:
            changes = self.catalog.get(name, {})[version].changes
        except KeyError:
            raise UnknownVersion(version) from None
        return base.model_copy(update=changes.model_dump(exclude_none=True), deep=True)

    def plan_deploy(self, name: BackendName, version: str, reason: str) -> ReleaseEvent:
        """The event for an operator's deploy. Nothing changes until `apply`.

        Every release under watch has a rollback target: a deploy from a
        stable or rolled-back version targets that version, and a deploy over
        a watched one keeps its target."""
        if version not in self.versions(name):
            raise UnknownVersion(version)
        current = self.state[name]
        if version == current.version:
            raise AlreadyServing(version)
        if current.status == "watching" and version == current.rollback_target:
            # Deploying the rollback target during a watch is a rollback: the
            # version is known good, so there is nothing to watch.
            return self.plan_rollback(name, reason, "operator")
        target = (
            current.version if current.status != "watching" else current.rollback_target
        )
        return ReleaseEvent(
            at=datetime.now(UTC),
            backend=name,
            action="deployed",
            version=version,
            from_version=current.version,
            rollback_target=target,
            actor="operator",
            reason=reason,
            revision=self._next_revision(name),
        )

    def plan_rollback(
        self, name: BackendName, reason: str, actor: ReleaseActor
    ) -> ReleaseEvent:
        current = self.state[name]
        if current.rollback_target is None:
            raise NoRollbackTarget(name)
        return ReleaseEvent(
            at=datetime.now(UTC),
            backend=name,
            action="rolled_back",
            version=current.rollback_target,
            from_version=current.version,
            rollback_target=None,
            actor=actor,
            reason=reason,
            revision=self._next_revision(name),
        )

    def roll_back(self, name: BackendName, reason: str) -> str | None:
        """The controller's rollback: apply now, save later. Return the version
        the model serves now, or None if there is nowhere to go."""
        try:
            event = self.plan_rollback(name, reason, "controller")
        except NoRollbackTarget:
            return None
        self.apply(event)
        self.unsaved.append(event)
        logger.info("Controller rolled %s back to %s", name, event.version)
        return event.version

    def confirm(self, name: BackendName) -> None:
        """End the watch: the release served `watch_samples` calls below the
        failure threshold."""
        current = self.state[name]
        if current.status != "watching":
            return
        event = ReleaseEvent(
            at=datetime.now(UTC),
            backend=name,
            action="confirmed",
            version=current.version,
            rollback_target=current.rollback_target,
            actor="controller",
            reason=f"{self.watch_samples} calls below the failure threshold",
            revision=self._next_revision(name),
        )
        self.apply(event)
        self.unsaved.append(event)

    def kept(self, name: BackendName, reason: str) -> None:
        """Record, as a newer change, the release the model kept after an
        operator change whose save could not be confirmed. Once saved, it
        outranks that change if the store has it, so a restart serves what
        the operator was told: the change was not made."""
        current = self.state[name]
        action = {
            "watching": "deployed",
            "stable": "confirmed",
            "rolled_back": "rolled_back",
        }[current.status]
        self.unsaved.append(
            ReleaseEvent(
                at=datetime.now(UTC),
                backend=name,
                action=action,  # type: ignore[arg-type]
                version=current.version,
                rollback_target=current.rollback_target,
                actor="operator",
                reason=reason,
                revision=self._next_revision(name),
            )
        )

    def watched(self, name: BackendName, samples: int) -> None:
        self.state[name].watched_samples = samples

    def apply(self, event: ReleaseEvent, restoring: bool = False) -> None:
        """Make the model serve `event.version` and record the new state."""
        backend = self.backends[event.backend]
        if event.action != "confirmed" or restoring:
            # Replaces any fault set through /v1/faults: a version is the
            # whole config. A confirmation while running leaves the config
            # alone; at startup it sets the confirmed version's config.
            backend.config = self.config_for(event.backend, event.version)
            self.generation[event.backend] += 1
        self.state[event.backend] = ModelRelease(
            name=event.backend,
            version=event.version,
            status={
                "deployed": "watching",
                "confirmed": "stable",
                "rolled_back": "rolled_back",
            }[event.action],  # type: ignore[arg-type]
            rollback_target=event.rollback_target,
            watch_samples=self.watch_samples,
            since=event.at,
            versions=self.versions(event.backend),
        )
        for hook in self.on_change:
            hook(event)

    def restore(self, latest: Iterable[ReleaseEvent]) -> None:
        """Put each model back on the release its newest saved event names. A
        version no longer in the catalog is skipped, with a warning, and the
        model stays on base."""
        for event in latest:
            if event.backend not in self.backends:
                continue
            self.revision[event.backend] = max(
                self.revision[event.backend], event.revision
            )
            known = event.version in self.versions(event.backend) and (
                event.rollback_target is None
                or event.rollback_target in self.versions(event.backend)
            )
            if not known:
                logger.warning(
                    "Saved release %s of %s is not in the catalog; serving %s",
                    event.version,
                    event.backend,
                    BASE_VERSION,
                )
                continue
            self.apply(event, restoring=True)

    def stats(self) -> list[ModelRelease]:
        return [self.state[name].model_copy() for name in self.backends]
