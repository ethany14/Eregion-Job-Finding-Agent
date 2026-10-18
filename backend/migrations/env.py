"""Alembic migration environment."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from eregion.api.db import Base
import eregion.runtime.analysis_cache  # noqa: F401
import eregion.runtime.provider_usage  # noqa: F401
import eregion.api.models  # noqa: F401
import eregion.runtime.models  # noqa: F401
import eregion.runtime.sessions.models  # noqa: F401
import eregion.runtime.memory.models  # noqa: F401
import eregion.runtime.skills.models  # noqa: F401
import eregion.runtime.skills.evolution_models  # noqa: F401
import eregion.runtime.context.models  # noqa: F401
import eregion.runtime.evidence.models  # noqa: F401
import eregion.runtime.interviewer.models  # noqa: F401
import eregion.runtime.mock_interview.models  # noqa: F401
import eregion.runtime.feedback.models  # noqa: F401
import eregion.runtime.learning.models  # noqa: F401
import eregion.runtime.application_pack.models  # noqa: F401
import eregion.runtime.coordination.models  # noqa: F401
import eregion.runtime.assistant.models  # noqa: F401
import eregion.runtime.workspace.models  # noqa: F401
import eregion.runtime.resumes.models  # noqa: F401
import eregion.domain.workflow.models  # noqa: F401
import eregion.runtime.auto_apply.models  # noqa: F401
import eregion.runtime.career.repository  # noqa: F401
import eregion.runtime.task_memory.models  # noqa: F401
import eregion.runtime.career.alerts  # noqa: F401

config = context.config
if config.config_file_name is not None:
    # Migrations run inside the server at startup; keep the application's
    # already-created loggers (agent failures, task loop) enabled.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

database_url = os.getenv("JOB_AGENT_DATABASE_URL")
if database_url and not config.attributes.get("explicit_database_url"):
    config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
