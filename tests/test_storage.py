"""Database migrations, immutable cache entries, and schema health."""

import sqlite3
from pathlib import Path

import pytest
from conftest import FakeLLM

from review_analysis.analysis.analyzer import ReviewAnalyzer
from review_analysis.reviews.models import Collection
from review_analysis.reviews.storage import SCHEMA_VERSION, Store


async def test_upgrade_preserves_snapshots_and_cache(
    tmp_path: Path, collection: Collection
) -> None:
    store = Store(tmp_path / "reviews.sqlite3")
    # Reproduce the original schema, including its nullable text primary key.
    with sqlite3.connect(store.path) as connection:
        connection.executescript("""
            CREATE TABLE collections (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE analyses (
                collection_id TEXT NOT NULL REFERENCES collections(id),
                configuration_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (collection_id, configuration_hash)
            );
            PRAGMA user_version = 1;
        """)
    store.save_collection(collection)
    original = await ReviewAnalyzer(FakeLLM()).analyze(collection)
    store.save_analysis(original)

    store.initialize()
    store.initialize()  # Starting again must be harmless.
    assert store.get_collection(collection.metadata.id) == collection
    assert store.get_analysis(collection.metadata.id) == original
    replacement = original.model_copy(update={"warnings": ["must not overwrite"]})
    assert store.save_analysis(replacement) == original
    assert store.healthy()
    with store.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT payload FROM analyses WHERE collection_id = ? "
            "ORDER BY created_at DESC, configuration_hash DESC LIMIT 1",
            (collection.metadata.id,),
        ).fetchall()
        assert any("analyses_latest" in row[3] for row in plan)
        assert not any("TEMP B-TREE" in row[3] for row in plan)


def test_unknown_schema_is_not_modified(tmp_path: Path) -> None:
    store = Store(tmp_path / "future.sqlite3")
    with store.connect() as connection:
        connection.execute("PRAGMA user_version = 999")
    with pytest.raises(RuntimeError, match="Unsupported database schema"):
        store.initialize()
    with store.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 999
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []


async def test_orphan_analysis_is_rejected(tmp_path: Path, collection: Collection) -> None:
    store = Store(tmp_path / "reviews.sqlite3")
    store.initialize()
    analysis = await ReviewAnalyzer(FakeLLM()).analyze(collection)
    with pytest.raises(sqlite3.IntegrityError):
        store.save_analysis(analysis)
    assert store.get_analysis(collection.metadata.id) is None


def test_health_detects_missing_tables(tmp_path: Path) -> None:
    store = Store(tmp_path / "empty.sqlite3")
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        store.healthy()


async def test_latest_analysis_has_stable_tie_break(tmp_path: Path, collection: Collection) -> None:
    store = Store(tmp_path / "reviews.sqlite3")
    store.initialize()
    store.save_collection(collection)
    original = await ReviewAnalyzer(FakeLLM()).analyze(collection)
    for key in ("b", "a"):
        analysis = original.model_copy(
            update={
                "provenance": original.provenance.model_copy(update={"configuration_hash": key})
            }
        )
        store.save_analysis(analysis)
    saved = store.get_analysis(collection.metadata.id)
    assert saved is not None
    assert saved.provenance.configuration_hash == "b"
