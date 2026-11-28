"""Where the gateway keeps completed requests, their Jev assessments and
controller interventions.

`MemoryStore` keeps them in process memory; they are lost on restart. It is the
default and what the tests use. `PostgresStore` keeps the same records in
Postgres so they survive a gateway restart; Compose uses it.

Neither store keeps the customer's message: `ChatResponse` has no message field.
"""

import asyncio
import logging
from collections import Counter, OrderedDict, deque
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol
from uuid import UUID

from loguru import logger
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from app_assignment_1.config import PUBLIC_DATABASE_PASSWORD, Settings
from app_assignment_1.records import JevAssessment, ReplyCheck
from app_assignment_1.schemas import (
    KEPT_REASON_MARKER,
    ChatResponse,
    Intervention,
    MonitorSample,
    ReleaseEvent,
)

MIGRATIONS = Path(__file__).parent / "migrations"
INTERVENTION_LIMIT = 50
RELEASE_EVENT_LIMIT = 200
# Any fixed number works; it only has to be the same for every gateway process.
MIGRATION_LOCK = 670001


class Store(Protocol):
    """Request history, Jev assessments and the controller's intervention log.

    The semantic monitor will add the methods that read Jev assessments. The
    backend response records will get their own methods and migration once
    their schema is agreed.
    """

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def save_request(
        self, response: ChatResponse, jev: Sequence[JevAssessment] = ()
    ) -> None:
        """Keep a completed request and its Jev assessments in one write; drop
        the oldest request beyond the retention limit. Assessments are kept
        for the monitor even after their request is dropped."""
        ...

    async def save_reply_check(self, check: ReplyCheck) -> None:
        """Keep the second Jev call's record. Saved after the request, on its
        own, so a failure here never touches the request."""
        ...

    async def get_request(self, request_id: UUID) -> ChatResponse | None: ...

    async def recent_requests(self, limit: int) -> list[ChatResponse]:
        """Up to `limit` retained requests, newest first."""
        ...

    async def save_interventions(self, items: Sequence[Intervention]) -> None: ...

    async def recent_interventions(self, limit: int) -> list[Intervention]:
        """The last `limit` interventions, oldest first."""
        ...

    async def latest_interventions(self, actions: Sequence[str]) -> list[Intervention]:
        """Each model's newest intervention of each of `actions`, however old,
        oldest first. Restore reads resets and holds from this, so a long run
        of newer interventions on the other model can't hide them."""
        ...

    async def save_monitor_samples(self, items: Sequence[MonitorSample]) -> None: ...

    async def recent_monitor_samples(self, per_window: int) -> list[MonitorSample]:
        """The newest `per_window` samples of each model's window (per model,
        monitor and traffic), oldest first."""
        ...

    async def save_release_events(self, items: Sequence[ReleaseEvent]) -> None:
        """Save release changes. Saving a change already saved is a no-op. A
        different change saved under the same (backend, revision) is a
        conflict: the others are saved, then RevisionTaken names the ones
        that were not."""
        ...

    async def recent_release_events(self, limit: int) -> list[ReleaseEvent]:
        """The last `limit` release changes, oldest first."""
        ...

    async def latest_release_changes(self) -> list[ReleaseEvent]:
        """Each model's newest deploy or rollback by revision, however old. A
        confirmation is not a change. Neither is a kept record, which has no
        from_version, so an operator's reason can't pass for one, nor the
        change a kept record follows: it was not made. Restore cuts the
        monitor windows at these."""
        ...

    async def latest_release_events(self) -> list[ReleaseEvent]:
        """The release change with the highest revision for each model: what
        it served last."""
        ...


class RevisionTaken(Exception):
    """Another release change was already saved under these revisions."""

    def __init__(self, events: Sequence[ReleaseEvent]):
        super().__init__(f"{len(events)} release revision(s) already taken")
        self.events = list(events)


class MemoryStore:
    """Bounded, process-local history. Resets when the gateway restarts."""

    def __init__(self, limit: int, intervention_limit: int = INTERVENTION_LIMIT):
        self.limit = limit
        self.requests: OrderedDict[UUID, ChatResponse] = OrderedDict()
        self.interventions: deque[Intervention] = deque(maxlen=intervention_limit)
        # Two per request, so this keeps about as many calls as requests.
        self.jev_assessments: deque[JevAssessment] = deque(maxlen=2 * limit)
        self.reply_checks: deque[ReplyCheck] = deque(maxlen=limit)
        # About two per request: a failure sample and a semantic one.
        self.monitor_samples: deque[MonitorSample] = deque(maxlen=4 * limit)
        self.monitor_sample_ids: set[UUID] = set()
        self.release_events: deque[ReleaseEvent] = deque(maxlen=RELEASE_EVENT_LIMIT)
        # Kept apart from the bounded history, so it never loses a model.
        self.latest_releases: dict[str, ReleaseEvent] = {}
        self.saved_revisions: dict[tuple[str, int], ReleaseEvent] = {}
        # Every deploy and rollback, and the revisions a kept record voided,
        # kept apart for the same reason (a handful per model).
        self.changes: dict[str, dict[int, ReleaseEvent]] = {}
        self.voided: set[tuple[str, int]] = set()

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def save_request(
        self, response: ChatResponse, jev: Sequence[JevAssessment] = ()
    ) -> None:
        self.requests[response.id] = response
        self.jev_assessments.extend(jev)
        while len(self.requests) > self.limit:
            self.requests.popitem(last=False)

    async def save_reply_check(self, check: ReplyCheck) -> None:
        self.reply_checks.append(check)

    async def get_request(self, request_id: UUID) -> ChatResponse | None:
        return self.requests.get(request_id)

    async def recent_requests(self, limit: int) -> list[ChatResponse]:
        return list(self.requests.values())[::-1][:limit]

    async def save_interventions(self, items: Sequence[Intervention]) -> None:
        self.interventions.extend(items)

    async def recent_interventions(self, limit: int) -> list[Intervention]:
        return list(self.interventions)[-limit:] if limit > 0 else []

    async def latest_interventions(self, actions: Sequence[str]) -> list[Intervention]:
        latest: dict[tuple[str, str], int] = {}
        items = list(self.interventions)
        for index, item in enumerate(items):
            if item.action in actions:
                latest[(item.backend, item.action)] = index
        return [items[index] for index in sorted(latest.values())]

    async def save_monitor_samples(self, items: Sequence[MonitorSample]) -> None:
        for item in items:
            if item.id in self.monitor_sample_ids:
                continue  # a retried save
            if len(self.monitor_samples) == self.monitor_samples.maxlen:
                self.monitor_sample_ids.discard(self.monitor_samples[0].id)
            self.monitor_samples.append(item)
            self.monitor_sample_ids.add(item.id)

    async def recent_monitor_samples(self, per_window: int) -> list[MonitorSample]:
        kept: list[MonitorSample] = []
        counts: Counter[tuple[str, str, str | None]] = Counter()
        for sample in reversed(self.monitor_samples):
            key = (sample.model, sample.monitor, sample.traffic)
            if counts[key] < per_window:
                counts[key] += 1
                kept.append(sample)
        return kept[::-1]

    async def save_release_events(self, items: Sequence[ReleaseEvent]) -> None:
        taken = []
        for item in items:
            key = (item.backend, item.revision)
            saved = self.saved_revisions.get(key)
            if saved is not None:
                if saved != item:
                    taken.append(item)
                continue
            self.saved_revisions[key] = item
            self.release_events.append(item)
            if _is_kept(item):
                self.voided.add((item.backend, item.revision - 1))
            elif item.action != "confirmed" and item.from_version is not None:
                self.changes.setdefault(item.backend, {})[item.revision] = item
            latest = self.latest_releases.get(item.backend)
            if latest is None or item.revision > latest.revision:
                self.latest_releases[item.backend] = item
        if taken:
            raise RevisionTaken(taken)

    async def recent_release_events(self, limit: int) -> list[ReleaseEvent]:
        return list(self.release_events)[-limit:] if limit > 0 else []

    async def latest_release_changes(self) -> list[ReleaseEvent]:
        newest = []
        for backend, by_revision in self.changes.items():
            live = [
                e for r, e in by_revision.items() if (backend, r) not in self.voided
            ]
            if live:
                newest.append(max(live, key=lambda e: e.revision))
        return newest

    async def latest_release_events(self) -> list[ReleaseEvent]:
        return list(self.latest_releases.values())


class PostgresStore:
    """Requests and interventions in Postgres, through psycopg 3 and plain SQL.

    Requests are kept in full unless `limit` is set, so they can be analysed
    later. The intervention log is kept in full; it grows by a few rows per
    cooldown. Jev assessments are kept in full: two rows per request.
    """

    def __init__(
        self,
        url: str,
        limit: int | None = None,
        connect_timeout: float = 30,
        timeout: float = 2,
    ):
        self.limit = limit
        self.connect_timeout = connect_timeout
        # psycopg_pool logs libpq's connection errors at WARNING, and those can
        # quote the whole URL, password included. Callers log the error type.
        logging.getLogger("psycopg").setLevel(logging.ERROR)
        self.pool = AsyncConnectionPool(
            url,
            open=False,
            min_size=1,
            max_size=4,
            timeout=timeout,
            check=AsyncConnectionPool.check_connection,
        )

    async def open(self) -> None:
        await self.pool.open(wait=True, timeout=self.connect_timeout)
        async with self.pool.connection() as conn:
            await migrate(conn)

    async def close(self) -> None:
        await self.pool.close()

    async def save_request(
        self, response: ChatResponse, jev: Sequence[JevAssessment] = ()
    ) -> None:
        # The pool commits once when the block ends, so the request and its
        # assessments are saved together or not at all.
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO requests
                    (id, completed_at, route, model, intent, format_check,
                     settings_version, escalation_score, escalated, response)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    response.id,
                    response.completed_at,
                    response.trace.route,
                    response.trace.model,
                    response.intent,
                    response.trace.format_check,
                    response.trace.settings_version,
                    response.trace.escalation_score,
                    response.trace.escalated,
                    Jsonb(response.model_dump(mode="json")),
                ),
            )
            if jev:
                async with conn.cursor() as cur:
                    await cur.executemany(
                        """
                        INSERT INTO jev_assessments
                            (request_id, call_id, question, status, finished_at,
                             mocked, option_set_version, choice, error_type,
                             option_order, record)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        [
                            (
                                r.request_id,
                                r.call_id,
                                r.question,
                                r.status,
                                r.finished_at,
                                r.mocked,
                                r.option_set_version,
                                r.choice,
                                r.error_type,
                                r.option_order,
                                Jsonb(r.model_dump(mode="json")),
                            )
                            for r in jev
                        ],
                    )
            if self.limit is not None:
                await conn.execute(
                    """
                    DELETE FROM requests WHERE seq <= (
                        SELECT seq FROM requests ORDER BY seq DESC OFFSET %s LIMIT 1
                    )
                    """,
                    (self.limit,),
                )

    async def save_reply_check(self, check: ReplyCheck) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO jev_reply_checks
                    (request_id, call_id, status, finished_at, mocked,
                     option_set_version, latency_ms, error_type, route_first,
                     route_second, intent_first, intent_second, reply_fit,
                     route_agrees, intent_agrees, record)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s)
                """,
                (
                    check.request_id,
                    check.call_id,
                    check.status,
                    check.finished_at,
                    check.mocked,
                    check.option_set_version,
                    check.latency_ms,
                    check.error_type,
                    check.route_first,
                    check.route_second,
                    check.intent_first,
                    check.intent_second,
                    check.reply_fit,
                    check.route_agrees,
                    check.intent_agrees,
                    Jsonb(check.model_dump(mode="json")),
                ),
            )

    async def get_request(self, request_id: UUID) -> ChatResponse | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT response FROM requests WHERE id = %s", (request_id,)
            )
            row = await cur.fetchone()
        return None if row is None else ChatResponse.model_validate(row[0])

    async def recent_requests(self, limit: int) -> list[ChatResponse]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT response FROM requests ORDER BY seq DESC LIMIT %s", (limit,)
            )
            rows = await cur.fetchall()
        return [ChatResponse.model_validate(row[0]) for row in rows]

    async def save_interventions(self, items: Sequence[Intervention]) -> None:
        if not items:
            return
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO interventions (at, backend, action, reason)"
                    " VALUES (%s, %s, %s, %s)",
                    [(i.at, i.backend, i.action, i.reason) for i in items],
                )

    async def recent_interventions(self, limit: int) -> list[Intervention]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT at, backend, action, reason FROM (
                    SELECT * FROM interventions ORDER BY seq DESC LIMIT %s
                ) AS latest ORDER BY seq
                """,
                (limit,),
            )
            rows = await cur.fetchall()
        return [
            Intervention(at=at, backend=backend, action=action, reason=reason)
            for at, backend, action, reason in rows
        ]

    async def latest_interventions(self, actions: Sequence[str]) -> list[Intervention]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT at, backend, action, reason FROM (
                    SELECT DISTINCT ON (backend, action)
                        seq, at, backend, action, reason
                    FROM interventions WHERE action = ANY(%s)
                    ORDER BY backend, action, seq DESC
                ) AS latest ORDER BY seq
                """,
                (list(actions),),
            )
            rows = await cur.fetchall()
        return [
            Intervention(at=at, backend=backend, action=action, reason=reason)
            for at, backend, action, reason in rows
        ]

    async def save_monitor_samples(self, items: Sequence[MonitorSample]) -> None:
        if not items:
            return
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO monitor_samples"
                    " (id, at, model, monitor, outcome, traffic)"
                    " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                    [
                        (s.id, s.at, s.model, s.monitor, s.outcome, s.traffic)
                        for s in items
                    ],
                )

    async def recent_monitor_samples(self, per_window: int) -> list[MonitorSample]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT id, at, model, monitor, outcome, traffic FROM (
                    SELECT *, row_number() OVER (
                        PARTITION BY model, monitor, traffic ORDER BY seq DESC
                    ) AS newest
                    FROM monitor_samples
                ) AS ranked
                WHERE newest <= %s
                ORDER BY seq
                """,
                (per_window,),
            )
            rows = await cur.fetchall()
        return [
            MonitorSample(
                id=id,
                at=at,
                model=model,
                monitor=monitor,
                outcome=outcome,
                traffic=traffic,
            )
            for id, at, model, monitor, outcome, traffic in rows
        ]

    async def save_release_events(self, items: Sequence[ReleaseEvent]) -> None:
        if not items:
            return
        taken = []
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(
                    """
                    INSERT INTO model_releases (at, backend, action, version,
                        from_version, rollback_target, actor, reason, revision)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (backend, revision) DO NOTHING
                    RETURNING backend
                    """,
                    [
                        (
                            e.at,
                            e.backend,
                            e.action,
                            e.version,
                            e.from_version,
                            e.rollback_target,
                            e.actor,
                            e.reason,
                            e.revision,
                        )
                        for e in items
                    ],
                    returning=True,
                )
                written = []
                while True:
                    written.append(await cur.fetchone() is not None)
                    if not cur.nextset():
                        break
                for item, wrote in zip(items, written, strict=True):
                    if wrote:
                        continue
                    # A row was already there: the same change saved before
                    # is fine; a different one is a conflict.
                    await cur.execute(
                        f"SELECT {RELEASE_COLUMNS} FROM model_releases"
                        " WHERE backend = %s AND revision = %s",
                        (item.backend, item.revision),
                    )
                    row = await cur.fetchone()
                    if row is None or _release_event(row) != item:
                        taken.append(item)
        if taken:
            raise RevisionTaken(taken)

    async def recent_release_events(self, limit: int) -> list[ReleaseEvent]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"""
                SELECT {RELEASE_COLUMNS} FROM (
                    SELECT * FROM model_releases ORDER BY seq DESC LIMIT %s
                ) AS latest ORDER BY seq
                """,
                (limit,),
            )
            rows = await cur.fetchall()
        return [_release_event(row) for row in rows]

    async def latest_release_changes(self) -> list[ReleaseEvent]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"""
                SELECT DISTINCT ON (m.backend) {RELEASE_COLUMNS}
                FROM model_releases AS m
                WHERE m.action <> 'confirmed' AND m.from_version IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM model_releases AS k
                    WHERE k.backend = m.backend AND k.revision = m.revision + 1
                      AND k.from_version IS NULL AND position(%s IN k.reason) > 0
                  )
                ORDER BY m.backend, m.revision DESC
                """,
                (KEPT_REASON_MARKER,),
            )
            rows = await cur.fetchall()
        return [_release_event(row) for row in rows]

    async def latest_release_events(self) -> list[ReleaseEvent]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"""
                SELECT DISTINCT ON (backend) {RELEASE_COLUMNS}
                FROM model_releases ORDER BY backend, revision DESC
                """
            )
            rows = await cur.fetchall()
        return [_release_event(row) for row in rows]


def _is_kept(event: ReleaseEvent) -> bool:
    """A record of a model keeping its release after an operator change that
    was not confirmed as saved. It has no from_version, which no deploy or
    rollback lacks, and the operator does not write its reason."""
    return event.from_version is None and KEPT_REASON_MARKER in event.reason


RELEASE_COLUMNS = (
    "at, backend, action, version, from_version, rollback_target, actor, reason,"
    " revision"
)


def _release_event(row: Sequence[object]) -> ReleaseEvent:
    return ReleaseEvent.model_validate(
        dict(zip(RELEASE_COLUMNS.split(", "), row, strict=True))
    )


async def migrate(conn: AsyncConnection) -> list[str]:
    """Apply numbered SQL files in migrations/ that have not run yet.

    Each file runs once, in order, inside one transaction with the version row,
    under an advisory lock so two processes cannot apply the same file.
    Returns the names of the files applied. A version recorded under another
    file name stops startup.
    """
    applied_now = []
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version integer PRIMARY KEY,
                name text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        cur = await conn.execute("SELECT version, name FROM schema_migrations")
        done = {row[0]: row[1] for row in await cur.fetchall()}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            version = int(path.name.split("_", 1)[0])
            if version in done:
                if done[version] != path.name:
                    # The same number ran from a different file, so this file
                    # would be skipped for good. Say so now, not at the first
                    # query that needs its tables.
                    raise RuntimeError(
                        f"Migration {version} ran as {done[version]}, but the"
                        f" file is now {path.name}; renumber one of them"
                    )
                continue
            await conn.execute(
                await asyncio.to_thread(path.read_text, encoding="utf-8")
            )
            await conn.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                (version, path.name),
            )
            applied_now.append(path.name)
    if applied_now:
        logger.info("Applied database migrations: {}", ", ".join(applied_now))
    return applied_now


def create_store(settings: Settings) -> Store:
    if settings.store == "postgres":
        if settings.database_url is None:  # Settings already rejects this.
            raise ValueError("APP_DATABASE_URL is required when APP_STORE=postgres")
        url = settings.database_url.get_secret_value()
        if PUBLIC_DATABASE_PASSWORD in url:
            logger.warning(
                "Using the public database password from env/compose.defaults.env;"
                " not for deployment"
            )
        return PostgresStore(
            url,
            settings.postgres_request_limit,
            timeout=settings.store_timeout_seconds,
        )
    return MemoryStore(settings.request_store_limit)
