"""Shared fixtures."""

import os
from collections.abc import Iterator
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def postgres_url() -> Iterator[str]:
    """A URL whose search_path is a new schema, dropped afterwards, so the
    database's existing tables are not touched. Skips without
    TEST_DATABASE_URL."""
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
