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
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from pydantic import ValidationError

from app_assignment_1.api import create_app
from app_assignment_1.config import PUBLIC_DATABASE_PASSWORD, Settings
from app_assignment_1.records import JevAssessment
from app_assignment_1.release import Release
from app_assignment_1.router import MockJevRouter
from app_assignment_1.schemas import ChatResponse, Intervention, Trace
from app_assignment_1.service import Gateway
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


def assessments(request_id: UUID) -> list[JevAssessment]:
    """One call's two records: an ok route answer and an intent error."""
    call = {
        "request_id": request_id,
        "call_id": uuid4(),
        "finished_at": datetime(2026, 11, 9, 12, tzinfo=UTC),
        "mocked": False,
        "option_set_version": "labels-0123456789ab",
        "latency_ms": 840.0,
        "jev_model": "jev-1.14.0",
        "provider_request_id": "req_123",
        "input_tokens": 900,
        "output_tokens": 12,
    }
    return [
        JevAssessment(
            **call,
            question="route",
            status="ok",
            choice="standard",
            confidence=0.4,
            probabilities={"standard": 0.7, "complex": 0.3},
        ),
        JevAssessment(
            **call, question="intent", status="error", error_type="wrong_options"
        ),
    ]


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


def test_releases_read_back_newest_first(make_store: MakeStore) -> None:
    releases = [
        Release(
            stable="v1",
            canary=None if n == 2 else "v2-chaos",
            canary_percent=0 if n == 2 else 10 * (n + 1),
            reason=f"change {n}",
            changed_at=datetime(2026, 11, 12, 12, n, 0, 123456, tzinfo=UTC),
        )
        for n in range(3)
    ]

    async def body(store: Store) -> None:
        assert await store.recent_releases(5) == []
        for item in releases:
            await store.save_release(item)
        assert await store.recent_releases(5) == releases[::-1]
        assert await store.recent_releases(1) == [releases[2]]
        assert await store.recent_releases(0) == []

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


def test_memory_store_saves_jev_assessments_with_the_request() -> None:
    store = MemoryStore(1)
    first, second = response(0), response(1)
    asyncio.run(store.save_request(first, assessments(first.id)))
    asyncio.run(store.save_request(second, assessments(second.id)))
    # The request limit is 1, and the assessments keep 2 per request.
    assert list(store.requests) == [second.id]
    assert [r.request_id for r in store.jev_assessments] == [second.id] * 2


def test_postgres_saves_jev_assessments_with_the_request(postgres_url: str) -> None:
    first, second = response(0), response(1)
    records = assessments(first.id)

    async def body(store: Store) -> None:
        await store.save_request(first, records)
        await store.save_request(second, assessments(second.id))

    run(PostgresStore(postgres_url, 1, connect_timeout=10), body)
    with psycopg.connect(postgres_url) as conn:
        rows = conn.execute(
            "SELECT request_id, question, status, mocked, option_set_version,"
            " choice, error_type, record FROM jev_assessments ORDER BY seq"
        ).fetchall()
        kept = conn.execute("SELECT id FROM requests").fetchall()
    # The first request is pruned; its assessments stay for the monitor.
    assert kept == [(second.id,)]
    assert len(rows) == 4
    assert rows[0][:7] == (
        first.id,
        "route",
        "ok",
        False,
        "labels-0123456789ab",
        "standard",
        None,
    )
    assert rows[1][:7] == (
        first.id,
        "intent",
        "error",
        False,
        "labels-0123456789ab",
        None,
        "wrong_options",
    )
    assert [JevAssessment.model_validate(row[7]) for row in rows[:2]] == records


def test_postgres_saves_a_request_and_its_assessments_together(
    postgres_url: str,
) -> None:
    saved = response(0)
    broken = assessments(saved.id)
    broken.append(broken[0])  # a duplicate (call_id, question) row fails

    async def body(store: Store) -> None:
        with pytest.raises(psycopg.errors.UniqueViolation):
            await store.save_request(saved, broken)
        assert await store.get_request(saved.id) is None

    run(PostgresStore(postgres_url, 10, connect_timeout=10), body)


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
        rows = conn.execute(
            "SELECT row_to_json(jev_assessments)::text FROM jev_assessments"
        )
        jev = [row[0] for row in rows.fetchall()]
    assert len(dumped) == 1
    assert "zebra-quartz" not in dumped[0]
    assert len(jev) == 2
    assert not any("zebra-quartz" in row for row in jev)


def test_migrations_run_once(postgres_url: str) -> None:
    async def main() -> None:
        async with await psycopg.AsyncConnection.connect(postgres_url) as conn:
            first = await migrate(conn)
            second = await migrate(conn)
            cur = await conn.execute("SELECT version, name FROM schema_migrations")
            rows = await cur.fetchall()
        assert first == [
            "001_requests_and_interventions.sql",
            "002_jev_assessments.sql",
            "003_releases.sql",
        ]
        assert second == []
        assert sorted(rows) == [
            (1, "001_requests_and_interventions.sql"),
            (2, "002_jev_assessments.sql"),
            (3, "003_releases.sql"),
        ]

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

    async def save_request(
        self, response: ChatResponse, jev: Sequence[JevAssessment] = ()
    ) -> None:
        if self.fail:
            raise ConnectionError("database is down")
        await super().save_request(response, jev)

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


# Review fixes (PR 72): bounded waits, no secrets in logs or errors, order.

SECRET_URL = "postgresql://gw:Hunter2Pass@127.0.0.1:1/gw"


class HangingStore(MemoryStore):
    async def save_request(
        self, response: ChatResponse, jev: Sequence[JevAssessment] = ()
    ) -> None:
        await asyncio.sleep(60)

    async def save_interventions(self, items: object) -> None:
        await asyncio.sleep(60)


def chaos_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "backends_path": "config/backends.chaos.json",
        "monitor_min_samples": 1,
        "monitor_window": 1,
    } | overrides
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_a_hanging_store_delays_the_reply_by_the_timeout_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    settings = chaos_settings(store_timeout_seconds=0.2)
    with TestClient(create_app(settings, store=HangingStore(10))) as client:
        start = time.perf_counter()
        chat = client.post("/v1/chat", json={"message": "card arrival"})
        elapsed = time.perf_counter() - start
    assert chat.status_code == 200
    # One intervention save and one request save, each cut off at 0.2 s.
    assert elapsed < 1.5
    assert "Could not save interventions (TimeoutError)" in caplog.text
    assert "Could not save request (TimeoutError)" in caplog.text


def test_unreachable_postgres_is_quick_and_keeps_the_url_out_of_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    settings = Settings(_env_file=None)

    async def main() -> float:
        store = PostgresStore(SECRET_URL, 10, timeout=0.3)
        await store.pool.open(wait=False)
        gateway = Gateway(settings, MockJevRouter(settings.intents_path), store=store)
        try:
            start = time.perf_counter()
            await gateway.chat("card arrival")
            await asyncio.sleep(0.5)  # let the pool log its connection attempts
            return time.perf_counter() - start - 0.5
        finally:
            await store.close()

    assert asyncio.run(main()) < 2
    assert "Could not save request" in caplog.text
    assert "Hunter2Pass" not in caplog.text
    assert "127.0.0.1" not in caplog.text


def test_a_malformed_url_fails_without_quoting_it() -> None:
    with pytest.raises(ValidationError, match="not a valid Postgres URL") as exc:
        Settings(
            _env_file=None,
            store="postgres",
            database_url="postgresq://gw:Hunter2Pass@db:5432/gw",
        )
    assert "Hunter2Pass" not in str(exc.value)


def test_settings_errors_do_not_print_secrets() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(
            _env_file=None,
            monitor_min_samples=30,
            store="postgres",
            database_url=SECRET_URL,
            jev_api_key="sk-Hunter2Key",
        )
    assert "Hunter2Pass" not in str(exc.value)
    assert "Hunter2Key" not in str(exc.value)


def test_the_public_password_is_refused_outside_local_runs() -> None:
    defaults = Path("env/compose.defaults.env").read_text()
    assert f":{PUBLIC_DATABASE_PASSWORD}@" in defaults
    assert f"POSTGRES_PASSWORD={PUBLIC_DATABASE_PASSWORD}" in defaults
    url = f"postgresql://gateway:{PUBLIC_DATABASE_PASSWORD}@postgres:5432/gateway"
    Settings(_env_file=None, store="postgres", database_url=url)
    for environment in ("staging", "production"):
        with pytest.raises(ValidationError, match="public password"):
            Settings(
                _env_file=None,
                environment=environment,
                store="postgres",
                database_url=url,
            )


def test_postgres_env_template_does_not_set_a_password() -> None:
    lines = Path("env/postgres.env.example").read_text().splitlines()
    assert not [line for line in lines if line.startswith("POSTGRES_PASSWORD")]


def test_a_failed_save_keeps_interventions_in_order() -> None:
    """A retried batch must not land after a newer one saved meanwhile."""

    class FirstSaveFails(MemoryStore):
        def __init__(self) -> None:
            super().__init__(10)
            self.release = asyncio.Event()
            self.calls = 0

        async def save_interventions(self, items: object) -> None:
            self.calls += 1
            if self.calls == 1:
                await self.release.wait()
                raise ConnectionError("database is down")
            await super().save_interventions(items)  # type: ignore[arg-type]

    settings = Settings(_env_file=None)

    async def main() -> list[Intervention]:
        store = FirstSaveFails()
        gateway = Gateway(settings, MockJevRouter(settings.intents_path), store=store)
        gateway.controller.unsaved.append(intervention(0))
        first = asyncio.create_task(gateway.save_interventions())
        await asyncio.sleep(0)
        gateway.controller.unsaved.append(intervention(1))
        second = asyncio.create_task(gateway.save_interventions())
        await asyncio.sleep(0)
        store.release.set()
        await asyncio.gather(first, second)
        return await store.recent_interventions(10)

    assert asyncio.run(main()) == [intervention(0), intervention(1)]


def test_shutdown_saves_pending_interventions() -> None:
    store = MemoryStore(10)
    with TestClient(create_app(Settings(_env_file=None), store=store)) as client:
        # As if a /metrics scrape had noticed a re-enable after the last request.
        client.app.state.gateway.controller.unsaved.append(intervention(0))
    assert list(store.interventions) == [intervention(0)]
