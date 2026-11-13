"""Where the gateway keeps completed requests, their Jev assessments and
controller interventions.

`MemoryStore` keeps them in process memory; they are lost on restart. It is the
default and what the tests use. `PostgresStore` keeps the same records in
Postgres so they survive a gateway restart; Compose uses it.

Neither store keeps the customer's message: `ChatResponse` has no message field.
"""

import logging
from collections import OrderedDict, deque
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from app_assignment_1.config import PUBLIC_DATABASE_PASSWORD, Settings
from app_assignment_1.records import JevAssessment
from app_assignment_1.release import Release
from app_assignment_1.schemas import ChatResponse, Intervention, Trace

logger = logging.getLogger(__name__)
MIGRATIONS = Path(__file__).parent / "migrations"
INTERVENTION_LIMIT = 50
RELEASE_LIMIT = 50
# Any fixed number works; it only has to be the same for every gateway process.
MIGRATION_LOCK = 670001


class Store(Protocol):
    """Request history, Jev assessments, the controller's intervention log
    and the canary release history.

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

    async def get_request(self, request_id: UUID) -> ChatResponse | None: ...

    async def recent_requests(self, limit: int) -> list[ChatResponse]:
        """Up to `limit` retained requests, newest first."""
        ...

    async def save_interventions(self, items: Sequence[Intervention]) -> None: ...

    async def recent_interventions(self, limit: int) -> list[Intervention]:
        """The last `limit` interventions, oldest first."""
        ...

    async def save_release(self, release: Release) -> None:
        """Add a release change to the history."""
        ...

    async def recent_releases(self, limit: int) -> list[Release]:
        """The last `limit` release changes, newest first. The first one is
        the release in effect."""
        ...


class MemoryStore:
    """Bounded, process-local history. Resets when the gateway restarts."""

    def __init__(self, limit: int, intervention_limit: int = INTERVENTION_LIMIT):
        self.limit = limit
        self.requests: OrderedDict[UUID, ChatResponse] = OrderedDict()
        self.interventions: deque[Intervention] = deque(maxlen=intervention_limit)
        # The newest is always kept, so the release in effect is never lost.
        self.releases: deque[Release] = deque(maxlen=RELEASE_LIMIT)
        # Two per request, so this keeps about as many calls as requests.
        self.jev_assessments: deque[JevAssessment] = deque(maxlen=2 * limit)

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

    async def get_request(self, request_id: UUID) -> ChatResponse | None:
        return self.requests.get(request_id)

    async def recent_requests(self, limit: int) -> list[ChatResponse]:
        return list(self.requests.values())[::-1][:limit]

    async def save_interventions(self, items: Sequence[Intervention]) -> None:
        self.interventions.extend(items)

    async def recent_interventions(self, limit: int) -> list[Intervention]:
        return list(self.interventions)[-limit:] if limit > 0 else []

    async def save_release(self, release: Release) -> None:
        self.releases.append(release)

    async def recent_releases(self, limit: int) -> list[Release]:
        return list(self.releases)[::-1][:limit] if limit > 0 else []


class PostgresStore:
    """Requests and interventions in Postgres, through psycopg 3 and plain SQL.

    Requests are bounded by the same retention limit as the memory store. The
    intervention log is kept in full; it grows by a few rows per cooldown. Jev
    assessments are kept in full for the monitor: two rows per request.
    """

    def __init__(
        self,
        url: str,
        limit: int,
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
                    (id, completed_at, route, model, intent, format_check, response)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    response.id,
                    response.completed_at,
                    response.trace.route,
                    response.trace.model,
                    response.intent,
                    response.trace.format_check,
                    Jsonb(response.model_dump(mode="json")),
                ),
            )
            if jev:
                async with conn.cursor() as cur:
                    await cur.executemany(
                        """
                        INSERT INTO jev_assessments
                            (request_id, call_id, question, status, finished_at,
                             mocked, option_set_version, choice, error_type, record)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                                Jsonb(r.model_dump(mode="json")),
                            )
                            for r in jev
                        ],
                    )
            await conn.execute(
                """
                DELETE FROM requests WHERE seq <= (
                    SELECT seq FROM requests ORDER BY seq DESC OFFSET %s LIMIT 1
                )
                """,
                (self.limit,),
            )

    async def get_request(self, request_id: UUID) -> ChatResponse | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT response FROM requests WHERE id = %s", (request_id,)
            )
            row = await cur.fetchone()
        return None if row is None else stored_response(row[0])

    async def recent_requests(self, limit: int) -> list[ChatResponse]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT response FROM requests ORDER BY seq DESC LIMIT %s", (limit,)
            )
            rows = await cur.fetchall()
        return [stored_response(row[0]) for row in rows]

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

    async def save_release(self, release: Release) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO releases
                    (changed_at, stable, canary, canary_percent, reason)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    release.changed_at,
                    release.stable,
                    release.canary,
                    release.canary_percent,
                    release.reason,
                ),
            )

    async def recent_releases(self, limit: int) -> list[Release]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT changed_at, stable, canary, canary_percent, reason
                FROM releases ORDER BY seq DESC LIMIT %s
                """,
                (limit,),
            )
            rows = await cur.fetchall()
        return [
            Release(
                changed_at=changed_at,
                stable=stable,
                canary=canary,
                canary_percent=percent,
                reason=reason,
            )
            for changed_at, stable, canary, percent, reason in rows
        ]


async def migrate(conn: AsyncConnection) -> list[str]:
    """Apply numbered SQL files in migrations/ that have not run yet.

    Each file runs once, in order, inside one transaction with the version row,
    under an advisory lock so two processes cannot apply the same file.
    Returns the names of the files applied.
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
        cur = await conn.execute("SELECT version FROM schema_migrations")
        done = {row[0] for row in await cur.fetchall()}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            version = int(path.name.split("_", 1)[0])
            if version in done:
                continue
            await conn.execute(path.read_text(encoding="utf-8"))
            await conn.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                (version, path.name),
            )
            applied_now.append(path.name)
    if applied_now:
        logger.info("Applied database migrations: %s", ", ".join(applied_now))
    return applied_now


def stored_response(data: dict[str, Any]) -> ChatResponse:
    """Read a saved ChatResponse, ignoring fields this version does not know.

    A newer gateway may have saved fields that this one lacks, for example
    after the image is rolled back. The API models stay strict; only reads
    from the store are lenient. Images from before the canary splitter do
    not have this, so rolling back past it breaks reads (docs/CANARY.md).
    """
    known = {
        key: value for key, value in data.items() if key in ChatResponse.model_fields
    }
    trace = known.get("trace")
    if isinstance(trace, dict):
        known["trace"] = {k: v for k, v in trace.items() if k in Trace.model_fields}
    return ChatResponse.model_validate(known)


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
            url, settings.request_store_limit, timeout=settings.store_timeout_seconds
        )
    return MemoryStore(settings.request_store_limit)
