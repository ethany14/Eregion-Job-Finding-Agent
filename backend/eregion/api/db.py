"""Database configuration for persisted API runs."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from eregion.paths import PROJECT_ROOT

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DEFAULT_DATABASE_URL = "sqlite:///./job_agent.db"


class Base(DeclarativeBase):
    pass


@dataclass(frozen=True)
class Database:
    engine: Engine
    session_factory: sessionmaker[Session]

    def create_all_for_tests(self) -> None:
        # Importing registers model metadata on Base before create_all runs.
        import eregion.runtime.analysis_cache  # noqa: F401
        import eregion.runtime.provider_usage  # noqa: F401
        import eregion.runtime.career.external_tool_usage  # noqa: F401
        import eregion.api.models  # noqa: F401
        import eregion.runtime.models  # noqa: F401
        import eregion.runtime.sessions.models  # noqa: F401
        import eregion.runtime.memory.models  # noqa: F401
        import eregion.runtime.skills.models  # noqa: F401
        import eregion.runtime.skills.evolution_models  # noqa: F401
        import eregion.runtime.assistant.models  # noqa: F401
        import eregion.runtime.context.models  # noqa: F401
        import eregion.runtime.evidence.models  # noqa: F401
        import eregion.runtime.interviewer.models  # noqa: F401
        import eregion.runtime.mock_interview.models  # noqa: F401
        import eregion.runtime.feedback.models  # noqa: F401
        import eregion.runtime.learning.models  # noqa: F401
        import eregion.runtime.application_pack.models  # noqa: F401
        import eregion.runtime.coordination.models  # noqa: F401
        import eregion.runtime.workspace.models  # noqa: F401
        import eregion.runtime.resumes.models  # noqa: F401
        import eregion.domain.workflow.models  # noqa: F401
        import eregion.runtime.auto_apply.models  # noqa: F401
        import eregion.runtime.career.repository  # noqa: F401
        import eregion.runtime.career.alerts  # noqa: F401

        Base.metadata.create_all(self.engine)

    def close(self) -> None:
        self.engine.dispose()


def resolve_database_url(database_url: str | None = None) -> str:
    return database_url or os.getenv("JOB_AGENT_DATABASE_URL", DEFAULT_DATABASE_URL)


def upgrade_database(database_url: str | None = None) -> None:
    """Apply Alembic migrations for an application database."""
    config_path = PROJECT_ROOT / "alembic.ini"
    config = Config(str(config_path))
    config.set_main_option("sqlalchemy.url", resolve_database_url(database_url))
    config.attributes["explicit_database_url"] = True
    command.upgrade(config, "head")


class DatabaseSchemaError(RuntimeError):
    """Startup cannot safely use this database without an explicit migration."""


def require_current_database(database_url: str | None = None) -> None:
    """Check schema without creating a SQLite file or changing database pragmas."""
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    expected = set(ScriptDirectory.from_config(config).get_heads())
    url = make_url(resolve_database_url(database_url))
    options = {}
    if url.get_backend_name() == "sqlite":
        if not url.database or url.database == ":memory:" or not Path(url.database).is_file():
            raise DatabaseSchemaError("Database is not initialized. Run an explicit database migration before startup.")
        uri = Path(url.database).resolve().as_uri() + "?mode=ro"
        options["creator"] = lambda: sqlite3.connect(uri, uri=True)
    engine = create_engine(url, **options)
    try:
        with engine.connect() as connection:
            current = set(MigrationContext.configure(connection).get_current_heads())
        if current != expected:
            raise DatabaseSchemaError(
                f"Database schema {sorted(current) or ['uninitialized']} does not match required "
                f"{sorted(expected)}. Startup did not migrate it. Back up and explicitly migrate before restarting."
            )
    finally:
        engine.dispose()


def create_database(
    database_url: str | None = None, *, create_schema_for_tests: bool = False
) -> Database:
    url = resolve_database_url(database_url)
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def enable_foreign_keys(dbapi_connection, _) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            # WAL lets readers (UI polling) proceed while a run writes, and the
            # longer timeout absorbs short write bursts from concurrent runs.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.close()
    database = Database(
        engine=engine,
        session_factory=sessionmaker(
            bind=engine,
            class_=Session,
            expire_on_commit=False,
        ),
    )
    if create_schema_for_tests:
        database.create_all_for_tests()
    return database
