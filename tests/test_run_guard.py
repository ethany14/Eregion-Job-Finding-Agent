import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

BODY = '''
import sys, threading, time, subprocess
sys.path.insert(0, r"{scripts}")
from run_guard import guarded, require
with guarded(r"{out}", deadline_s={deadline}) as run:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    run["result"]["child"] = child.pid
    threading.Thread(target=lambda: time.sleep(120)).start()   # non-daemon: would block a normal exit
    {action}
'''


def scenario(tmp_path, action, deadline=60):
    script = tmp_path / 'scenario.py'
    script.write_text(textwrap.dedent(BODY.format(scripts=ROOT / 'tools' / 'scripts', out=tmp_path / 'out',
                                                  deadline=deadline, action=action)), encoding='utf-8')
    started = time.monotonic()
    done = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, encoding='utf-8',
                          errors='replace', timeout=110)
    record = json.loads((tmp_path / 'out' / 'exit.json').read_text(encoding='utf-8'))
    return done, record, time.monotonic() - started


def alive(pid):
    if sys.platform == 'win32':
        out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/NH', '/FO', 'CSV'], capture_output=True, text=True,
                             encoding='utf-8', errors='replace').stdout or ''
        return str(pid) in out
    try:
        import os; os.kill(pid, 0); return True
    except OSError:
        return False


@pytest.mark.parametrize('action,outcome,code', [
    ('raise RuntimeError("boom")', 'exception', 2),
    ('require(False, "budget scenario already has scores")', 'premise_failed', 4),
    ('pass', 'completed', 0),
])
def test_every_ending_cleans_up_children_and_exits(tmp_path, action, outcome, code):
    done, record, seconds = scenario(tmp_path, action)
    assert done.returncode == code and record['outcome'] == outcome
    assert seconds < 90, 'the non-daemon thread must not keep the process alive'
    assert any(k.startswith(f"{record['result']['child']}:") for k in record['killed_descendants'])
    assert not alive(record['result']['child'])


def test_hard_deadline_stops_a_hung_body(tmp_path):
    done, record, seconds = scenario(tmp_path, 'time.sleep(300)', deadline=5)
    assert done.returncode == 3 and record['outcome'] == 'deadline' and seconds < 90
    assert not alive(record['result']['child'])
    assert 'File' in (tmp_path / 'out' / 'stacks.txt').read_text(encoding='utf-8')


def test_coverage_reports_an_unreached_target_as_not_covered():
    sys.path.insert(0, str(ROOT / 'tools' / 'scripts'))
    from run_guard import Coverage
    coverage = Coverage('affordable_scoring', 'score_tool')
    coverage.hit('score_tool')
    report = coverage.report()
    assert report['verdict'] == 'not_covered' and report['not_covered'] == ['affordable_scoring']
