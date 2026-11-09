"""Store contract: memory always, Postgres when TEST_DATABASE_URL is set.

Each Postgres test runs in its own new schema, dropped afterwards, so the
database's existing tables are not touched. Example for a local run:

    docker run --rm -d --name gateway-test-db -p 127.0.0.1:55432:5432 \
      -e POSTGRES_PASSWORD=test postgres:18.6-alpine
    TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres \
      uv run pytest tests/test_store.py
"""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from pydantic import ValidationError

from app_assignment_1.api import create_app
from app_assignment_1.config import Settings
from app_assignment_1.schemas import ChatResponse, Intervention, Trace
from app_assignment_1.store import (
    MemoryStore,
    PostgresStore,
    Store,
    create_store,
    migrate,
)

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
MakeStore = Callable[..., Store]


@pytest.fixture
def postgres_url() -> Iterator[str]:
    if not DATABASE_URL:
        pytest.skip("Set TEST_DATABASE_URL to run the Postgres store tests")
    schema = f"test_{uuid4().hex[:12]}"
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    sep = "&" if "?" in DATABASE_URL else "?"
    try:
        yield f"{DATABASE_URL}{sep}options=-csearch_path%3D{schema}"
    finally:
        with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


@pytest.fixture(params=["memory", "postgres"])
def make_store(request: pytest.FixtureRequest) -> MakeStore:
    if request.param == "memory":
        return lambda limit=1000: MemoryStore(limit)
    url = request.getfixturevalue("postgres_url")
    return lambda limit=1000: PostgresStore(url, limit, connect_timeout=10)


def run(store: Store, body: Callable[[Store], Awaitable[None]]) -> None:
    """Open the store, run the body and close it, all on one event loop."""

    async def main() -> None:
        await store.open()
        try:
            await body(store)
        finally:
            await store.close()

    asyncio.run(main())


def response(n: int = 0) -> ChatResponse:
    return ChatResponse(
        completed_at=datetime(2026, 11, 9, 12, 0, n, 123456, tzinfo=UTC),
        id=uuid4(),
        intent="card_arrival",
        reply=f"Reply {n}",
        policy_facts=["Fact one.", "Fact two."],
        trace=Trace(
            router="mock_jev",
            jev_choice="standard",
            route="standard",
            model="qwen3-1.7b",
            reply_source="policy_template",
            confidence=0.87,
            latency_ms=12.5,
            simulated_latency_ms=10,
            format_check="passed",
            fallback_reason=None,
        ),
    )


def intervention(n: int) -> Intervention:
    return Intervention(
        at=datetime(2026, 11, 9, tzinfo=UTC) + timedelta(seconds=n),
        backend="qwen3-1.7b",
        action="disabled",
        reason=f"reason {n}",
    )


def test_saved_request_reads_back_unchanged(make_store: MakeStore) -> None:
    async def body(store: Store) -> None:
        saved = response()
        await store.save_request(saved)
        assert await store.get_request(saved.id) == saved
        assert await store.get_request(uuid4()) is None

    run(make_store(), body)


def test_recent_requests_are_newest_first_and_limited(make_store: MakeStore) -> None:
    async def body(store: Store) -> None:
        saved = [response(n) for n in range(5)]
        for item in saved:
            await store.save_request(item)
        assert await store.recent_requests(3) == saved[::-1][:3]
        assert await store.recent_requests(20) == saved[::-1]

    run(make_store(), body)


def test_retention_limit_drops_the_oldest(make_store: MakeStore) -> None:
    async def body(store: Store) -> None:
        saved = [response(n) for n in range(5)]
        for item in saved:
            await store.save_request(item)
        assert [r.id for r in await store.recent_requests(10)] == [
            r.id for r in saved[:1:-1]
        ]
        assert await store.get_request(saved[0].id) is None
        assert await store.get_request(saved[4].id) == saved[4]

    run(make_store(limit=3), body)


def test_interventions_keep_order_and_return_the_last(make_store: MakeStore) -> None:
    async def body(store: Store) -> None:
        assert await store.recent_interventions(50) == []
        await store.save_interventions([])
        await store.save_interventions([intervention(0), intervention(1)])
        await store.save_interventions([intervention(2)])
        assert await store.recent_interventions(50) == [
            intervention(n) for n in range(3)
        ]
        assert await store.recent_interventions(2) == [intervention(1), intervention(2)]

    run(make_store(), body)


def test_memory_store_keeps_the_last_50_interventions() -> None:
    async def body(store: Store) -> None:
        await store.save_interventions([intervention(n) for n in range(60)])
        kept = await store.recent_interventions(100)
        assert kept == [intervention(n) for n in range(10, 60)]

    run(MemoryStore(10), body)


def test_api_reads_history_from_the_store(make_store: MakeStore) -> None:
    settings = Settings(_env_file=None)
    message = "My card has not arrived, tracking code zebra-quartz"
    with TestClient(create_app(settings, store=make_store())) as client:
        chat = client.post("/v1/chat", json={"message": message}).json()
        fetched = client.get(f"/v1/requests/{chat['id']}")
        assert fetched.status_code == 200
        assert fetched.json() == chat
        recent = client.get("/v1/health").json()["recent_requests"]
        assert [r["completed_at"] for r in recent] == [chat["completed_at"]]
        assert "zebra-quartz" not in fetched.text


def test_postgres_history_survives_a_restart(postgres_url: str) -> None:
    """A new store on the same database is what a restarted gateway sees."""
    settings = Settings(_env_file=None)
    with TestClient(create_app(settings, store=PostgresStore(postgres_url, 10))) as c:
        chat = c.post("/v1/chat", json={"message": "card arrival"}).json()
    with TestClient(create_app(settings, store=PostgresStore(postgres_url, 10))) as c:
        assert c.get(f"/v1/requests/{chat['id']}").json() == chat
        assert len(c.get("/v1/health").json()["recent_requests"]) == 1


def test_postgres_keeps_no_message_text(postgres_url: str) -> None:
    settings = Settings(_env_file=None)
    with TestClient(create_app(settings, store=PostgresStore(postgres_url, 10))) as c:
        c.post("/v1/chat", json={"message": "card arrival zebra-quartz"})
    with psycopg.connect(postgres_url) as conn:
        rows = conn.execute("SELECT row_to_json(requests)::text FROM requests")
        dumped = [row[0] for row in rows.fetchall()]
    assert len(dumped) == 1
    assert "zebra-quartz" not in dumped[0]


def test_migrations_run_once(postgres_url: str) -> None:
    async def main() -> None:
        async with await psycopg.AsyncConnection.connect(postgres_url) as conn:
            first = await migrate(conn)
            second = await migrate(conn)
            cur = await conn.execute("SELECT version, name FROM schema_migrations")
            rows = await cur.fetchall()
        assert first == ["001_requests_and_interventions.sql"]
        assert second == []
        assert rows == [(1, "001_requests_and_interventions.sql")]

    asyncio.run(main())


def test_postgres_store_needs_a_url() -> None:
    with pytest.raises(ValidationError, match="APP_DATABASE_URL is required"):
        Settings(_env_file=None, store="postgres")
    with pytest.raises(ValidationError, match="APP_DATABASE_URL is required"):
        Settings(_env_file=None, store="postgres", database_url="")


def test_database_url_is_secret_and_selects_the_store() -> None:
    url = "postgresql://gateway:hunter2-secret@postgres:5432/gateway"
    settings = Settings(_env_file=None, store="postgres", database_url=url)
    assert "hunter2-secret" not in repr(settings)
    assert isinstance(create_store(settings), PostgresStore)
    assert isinstance(create_store(Settings(_env_file=None)), MemoryStore)


class BrokenStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__(10)
        self.fail = True

    async def save_request(self, response: ChatResponse) -> None:
        if self.fail:
            raise ConnectionError("database is down")
        await super().save_request(response)

    async def save_interventions(self, items: object) -> None:
        if self.fail:
            raise ConnectionError("database is down")
        await super().save_interventions(items)  # type: ignore[arg-type]


def test_store_failure_still_returns_the_reply(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = BrokenStore()
    settings = Settings(
        _env_file=None,
        backends_path="config/backends.chaos.json",
        monitor_min_samples=1,
        monitor_window=1,
    )
    caplog.set_level(logging.WARNING)
    with TestClient(create_app(settings, store=store)) as client:
        chat = client.post("/v1/chat", json={"message": "card arrival"})
        assert chat.status_code == 200
        assert chat.json()["trace"]["controller_action"].startswith("disabled")
        assert "Could not save request (ConnectionError)" in caplog.text
        assert "Could not save interventions (ConnectionError)" in caplog.text
        assert client.get(f"/v1/requests/{chat.json()['id']}").status_code == 404
        # Unsaved interventions are kept and saved once the store recovers.
        store.fail = False
        health = client.get("/v1/health").json()
        assert [i["action"] for i in health["interventions"]] == ["disabled"]
    assert "database is down" not in caplog.text
