import hashlib
import sqlite3
import pytest
from alembic import command
from alembic.config import Config
from eregion.paths import PROJECT_ROOT
from eregion.api.db import require_current_database, DatabaseSchemaError, upgrade_database


def test_0038_is_untouched_until_explicit_upgrade(tmp_path):
    path=tmp_path/'old.sqlite';url=f'sqlite:///{path.as_posix()}'
    config=Config(str(PROJECT_ROOT/'alembic.ini'))
    config.set_main_option('sqlalchemy.url',url);config.attributes['explicit_database_url']=True
    command.upgrade(config,'0038_memory_application_scope_validity')
    with sqlite3.connect(path) as c:
        c.execute("insert into career_tasks (task_id,request_key,version,state_json) values ('t','r',1,'{}')")
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(DatabaseSchemaError):require_current_database(url)
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before
    upgrade_database(url)
    require_current_database(url)
    with sqlite3.connect(path) as c:
        assert c.execute('select version_num from alembic_version').fetchone()[0]=='0039_task_memory'
        assert c.execute('select state_json from career_tasks').fetchone()[0]=='{}'
        assert c.execute('select count(*) from task_memory_sources').fetchone()[0]==0
    command.downgrade(config,'0038_memory_application_scope_validity')
    with sqlite3.connect(path) as c:
        assert c.execute('select count(*) from career_tasks').fetchone()[0]==1
