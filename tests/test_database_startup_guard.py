"""Startup checks never initialize or upgrade a database implicitly."""
import hashlib
import sqlite3
import pytest
from alembic import command
from alembic.config import Config
from eregion.api.db import DatabaseSchemaError, require_current_database, upgrade_database
from eregion.paths import PROJECT_ROOT

@pytest.mark.parametrize('factory', ['session', 'run'])
def test_old_schema_startup_does_not_write(tmp_path, factory):
    from eregion.api.session_dependencies import create_session_runtime
    from eregion.api.runtime import create_run_service
    path = tmp_path / 'old.sqlite'
    url = f'sqlite:///{path.as_posix()}'
    config = Config(str(PROJECT_ROOT / 'alembic.ini'))
    config.set_main_option('sqlalchemy.url', url)
    config.attributes['explicit_database_url'] = True
    command.upgrade(config, '0034_career_job_alerts')
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(DatabaseSchemaError, match='Startup did not migrate'):
        (create_session_runtime if factory == 'session' else create_run_service)(database_url=url)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    with sqlite3.connect(path) as conn:
        assert conn.execute('select version_num from alembic_version').fetchone()[0] == '0034_career_job_alerts'
        assert 'valid_from' not in [r[1] for r in conn.execute('pragma table_info(memory_items)')]

def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / 'missing.sqlite'
    with pytest.raises(DatabaseSchemaError, match='not initialized'):
        require_current_database(f'sqlite:///{path.as_posix()}')
    assert not path.exists()

def test_explicit_migration_target_wins_over_environment(tmp_path, monkeypatch):
    target = tmp_path / 'chosen.sqlite'
    unwanted = tmp_path / 'other.sqlite'
    monkeypatch.setenv('JOB_AGENT_DATABASE_URL', f'sqlite:///{unwanted.as_posix()}')
    url = f'sqlite:///{target.as_posix()}'
    upgrade_database(url)
    require_current_database(url)
    assert not unwanted.exists()

@pytest.mark.parametrize('factory', ['session', 'run'])
def test_opt_in_requires_explicit_target(factory):
    from eregion.api.session_dependencies import create_session_runtime
    from eregion.api.runtime import create_run_service
    with pytest.raises(ValueError, match='explicit database URL'):
        (create_session_runtime if factory == 'session' else create_run_service)(migrate_schema=True)


def test_current_schema_can_start_without_migration(tmp_path, monkeypatch):
    from eregion.api import session_dependencies
    url = f"sqlite:///{(tmp_path / 'current.sqlite').as_posix()}"
    upgrade_database(url)
    def forbidden(*args, **kwargs):
        pytest.fail("Startup attempted a migration despite a current schema")
    monkeypatch.setattr(session_dependencies, 'upgrade_database', forbidden)
    runtime = session_dependencies.create_session_runtime(database_url=url, model=object())
    runtime.close()
