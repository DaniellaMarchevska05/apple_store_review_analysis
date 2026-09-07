import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from review_analysis.reviews.models import Analysis, AppError, Collection

SCHEMA_VERSION = 3


class Store:
    """Two immutable JSON document types; SQL owns identity and uniqueness."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in range(SCHEMA_VERSION + 1):
                raise RuntimeError(f"Unsupported database schema version: {version}")
            connection.execute("PRAGMA journal_mode = WAL")
            # Rebuilding the parent table must not rewrite or invalidate child references.
            connection.execute("PRAGMA foreign_keys = OFF")
            # Explicit transaction keeps schema changes and the version update atomic.
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS collections (
                    id TEXT PRIMARY KEY NOT NULL,
                    payload TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS analyses (
                    collection_id TEXT NOT NULL REFERENCES collections(id),
                    configuration_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (collection_id, configuration_hash)
                )
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS analyses_latest
                ON analyses (collection_id, created_at DESC, configuration_hash DESC)
            """)
            if (
                version < 3
                and not connection.execute("PRAGMA table_info(collections)").fetchone()[3]
            ):
                connection.execute(
                    "CREATE TABLE collections_new "
                    "(id TEXT PRIMARY KEY NOT NULL, payload TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO collections_new SELECT id, payload FROM collections"
                )
                connection.execute("DROP TABLE collections")
                connection.execute("ALTER TABLE collections_new RENAME TO collections")
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise sqlite3.IntegrityError("Database contains orphaned analysis records")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def save_collection(self, collection: Collection) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO collections (id, payload) VALUES (?, ?)",
                (collection.metadata.id, collection.model_dump_json()),
            )

    def get_collection(self, collection_id: str) -> Collection:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM collections WHERE id = ?", (collection_id,)
            ).fetchone()
        if row is None:
            raise AppError("collection_not_found", "Collection does not exist.", 404)
        return Collection.model_validate_json(row[0])

    def get_analysis(
        self, collection_id: str, configuration_hash: str | None = None
    ) -> Analysis | None:
        with self.connect() as connection:
            if configuration_hash is None:
                row = connection.execute(
                    "SELECT payload FROM analyses WHERE collection_id = ? "
                    "ORDER BY created_at DESC, configuration_hash DESC LIMIT 1",
                    (collection_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT payload FROM analyses "
                    "WHERE collection_id = ? AND configuration_hash = ?",
                    (collection_id, configuration_hash),
                ).fetchone()
        return Analysis.model_validate_json(row[0]) if row else None

    def save_analysis(self, analysis: Analysis) -> Analysis:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO analyses (collection_id, configuration_hash, created_at, payload) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT (collection_id, configuration_hash) DO NOTHING",
                (
                    analysis.collection_id,
                    analysis.provenance.configuration_hash,
                    analysis.provenance.created_at.isoformat(),
                    analysis.model_dump_json(),
                ),
            )
            # Read the winning result in the same transaction, including a cache conflict.
            row = connection.execute(
                "SELECT payload FROM analyses WHERE collection_id = ? AND configuration_hash = ?",
                (analysis.collection_id, analysis.provenance.configuration_hash),
            ).fetchone()
            if row is None:
                raise sqlite3.DatabaseError("Saved analysis could not be read back")
            return Analysis.model_validate_json(row[0])

    def healthy(self) -> bool:
        with self.connect() as connection:
            # SELECT 1 succeeds even when the application's tables are missing.
            connection.execute("SELECT id, payload FROM collections LIMIT 1").fetchone()
            connection.execute(
                "SELECT collection_id, configuration_hash, created_at, payload "
                "FROM analyses LIMIT 1"
            ).fetchone()
            return True
