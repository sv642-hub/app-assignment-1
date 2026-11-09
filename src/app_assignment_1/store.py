"""Where the gateway keeps completed requests and controller interventions.

`MemoryStore` keeps them in process memory; they are lost on restart. It is the
default and what the tests use. `PostgresStore` keeps the same records in
Postgres so they survive a gateway restart; Compose uses it.

Neither store keeps the customer's message: `ChatResponse` has no message field.
"""

import logging
from collections import OrderedDict, deque
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from app_assignment_1.config import Settings
from app_assignment_1.schemas import ChatResponse, Intervention

logger = logging.getLogger(__name__)
MIGRATIONS = Path(__file__).parent / "migrations"
INTERVENTION_LIMIT = 50
# Any fixed number works; it only has to be the same for every gateway process.
MIGRATION_LOCK = 670001


class Store(Protocol):
    """Request history and the controller's intervention log.

    The monitor's records (Jev assessments and backend responses, drafted in
    docs/MONITOR_RECORDS.md) will get their own methods here and their own
    migration file once that schema is agreed.
    """

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def save_request(self, response: ChatResponse) -> None:
        """Keep a completed request; drop the oldest beyond the retention limit."""
        ...

    async def get_request(self, request_id: UUID) -> ChatResponse | None: ...

    async def recent_requests(self, limit: int) -> list[ChatResponse]:
        """Up to `limit` retained requests, newest first."""
        ...

    async def save_interventions(self, items: Sequence[Intervention]) -> None: ...

    async def recent_interventions(self, limit: int) -> list[Intervention]:
        """The last `limit` interventions, oldest first."""
        ...


class MemoryStore:
    """Bounded, process-local history. Resets when the gateway restarts."""

    def __init__(self, limit: int, intervention_limit: int = INTERVENTION_LIMIT):
        self.limit = limit
        self.requests: OrderedDict[UUID, ChatResponse] = OrderedDict()
        self.interventions: deque[Intervention] = deque(maxlen=intervention_limit)

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def save_request(self, response: ChatResponse) -> None:
        self.requests[response.id] = response
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


class PostgresStore:
    """Requests and interventions in Postgres, through psycopg 3 and plain SQL.

    Requests are bounded by the same retention limit as the memory store. The
    intervention log is kept in full; it grows by a few rows per cooldown.
    """

    def __init__(self, url: str, limit: int, connect_timeout: float = 30):
        self.limit = limit
        self.connect_timeout = connect_timeout
        self.pool = AsyncConnectionPool(
            url,
            open=False,
            min_size=1,
            max_size=4,
            check=AsyncConnectionPool.check_connection,
        )

    async def open(self) -> None:
        await self.pool.open(wait=True, timeout=self.connect_timeout)
        async with self.pool.connection() as conn:
            await migrate(conn)

    async def close(self) -> None:
        await self.pool.close()

    async def save_request(self, response: ChatResponse) -> None:
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


def create_store(settings: Settings) -> Store:
    if settings.store == "postgres":
        if settings.database_url is None:  # Settings already rejects this.
            raise ValueError("APP_DATABASE_URL is required when APP_STORE=postgres")
        return PostgresStore(
            settings.database_url.get_secret_value(), settings.request_store_limit
        )
    return MemoryStore(settings.request_store_limit)
