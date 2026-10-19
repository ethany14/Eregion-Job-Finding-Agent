"""Exit hygiene and premise checks for local experiment scripts (spec 11.2 F0).

Paid and online experiment scripts run in-process services (task loops, application
workers, MCP stdio servers, JobSpy subprocesses). Those threads are not daemons: after a
crash the interpreter waits for them forever (D1 attempts 1-2, D2 attempt 1 hung for up to
an hour). `guarded` makes every ending explicit:

- success, exception, KeyboardInterrupt and a hard deadline all run the registered closers
  (each bounded by its own timeout), terminate this process's own descendants (never other
  processes), write exit.json and leave with os._exit;
- stack dumps are written periodically to stacks.txt while the run is alive;
- `Coverage` records which target paths a scenario declared and which ones actually ran,
  so a run that never reached its target is reported "not covered", never "passed".
"""
import faulthandler
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path


NAMES = {}


def descendants(pid):
    """This process's descendant PIDs (children first-level outward). Windows uses CIM; POSIX uses ps."""
    if os.name == 'nt':
        command = ['powershell', '-NoProfile', '-Command',
                   'Get-CimInstance Win32_Process | ForEach-Object { "$($_.ProcessId) $($_.ParentProcessId) $($_.Name)" }']
    else:
        command = ['ps', '-eo', 'pid=,ppid=,comm=']
    try:
        lister = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                  encoding='utf-8', errors='replace')
        output = lister.communicate(timeout=30)[0] or ''
    except Exception:
        return []
    children = {}
    for line in output.splitlines():
        parts = line.split(None, 2)
        # A console host belongs to the console, not to the work this script started.
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit() and (parts[2:] or [''])[0].casefold() != 'conhost.exe':
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
            NAMES[int(parts[0])] = (parts[2:] or ['?'])[0]
    found, frontier = [], [pid]
    while frontier:
        current = frontier.pop()
        for child in children.get(current, []):
            if child not in found and child != pid and child != lister.pid:  # never the listing process itself
                found.append(child)
                frontier.append(child)
    return found


def kill_descendants(pid=None):
    pid = pid or os.getpid()
    killed = []
    for child in reversed(descendants(pid)):
        try:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(child)], capture_output=True, timeout=20)
            else:
                os.kill(child, 9)
            killed.append(f'{child}:{NAMES.get(child, "?")}')
        except Exception:
            pass
    return killed


def _bounded(function, seconds):
    error = []
    def run():
        try:
            function()
        except BaseException as exc:  # recorded, never raised: cleanup must continue
            error.append(f'{type(exc).__name__}: {str(exc)[:200]}')
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        return 'timeout'
    return error[0] if error else 'ok'


class Coverage:
    """Declared target paths versus what the run actually exercised."""

    def __init__(self, *targets):
        self.targets = {target: False for target in targets}
        self.notes = {}

    def hit(self, target, note=None):
        if target in self.targets:
            self.targets[target] = True
            if note:
                self.notes[target] = note

    def report(self):
        missing = [t for t, hit in self.targets.items() if not hit]
        return {'targets': self.targets, 'notes': self.notes,
                'verdict': 'covered' if not missing else 'not_covered', 'not_covered': missing}


class Premise(AssertionError):
    """The scenario's initial state is not what the scenario claims to test."""


def require(condition, message):
    if not condition:
        raise Premise(message)


@contextmanager
def guarded(out_dir, *, deadline_s, closer_timeout_s=20, dump_every_s=180):
    """Run a scenario body; on every ending clean up and exit the process.

    The body receives a `run` dict: add closers with run['closers'].append((name, fn)) and
    put JSON-able results in run['result']. The process always ends inside this manager.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stacks = open(out / 'stacks.txt', 'w', encoding='utf-8')
    faulthandler.dump_traceback_later(min(dump_every_s, deadline_s), repeat=True, file=stacks)
    started = time.monotonic()
    run = {'closers': [], 'result': {}, 'outcome': 'running'}
    lock = threading.Lock()
    finished = []

    def finish(outcome, code, detail=None):
        with lock:
            if finished:
                return
            finished.append(outcome)
        faulthandler.cancel_dump_traceback_later()
        closers = {name: _bounded(fn, closer_timeout_s) for name, fn in reversed(run['closers'])}
        killed = kill_descendants()
        record = {'outcome': outcome, 'exit_code': code, 'elapsed_s': round(time.monotonic() - started, 1),
                  'closers': closers, 'killed_descendants': killed, 'detail': detail, 'result': run['result']}
        try:
            (out / 'exit.json').write_text(json.dumps(record, indent=1, ensure_ascii=False, default=str), encoding='utf-8')
        except Exception:
            pass
        if outcome == 'deadline':
            faulthandler.dump_traceback(file=stacks, all_threads=True)
        stacks.flush()
        print('EXIT', json.dumps({k: record[k] for k in ('outcome', 'exit_code', 'elapsed_s', 'closers', 'killed_descendants')},
                                 default=str), flush=True)
        sys.stdout.flush(); sys.stderr.flush()
        os._exit(code)

    timer = threading.Timer(deadline_s, lambda: finish('deadline', 3, f'hard deadline {deadline_s}s'))
    timer.daemon = True
    timer.start()
    try:
        yield run
    except Premise as exc:
        finish('premise_failed', 4, str(exc))
    except KeyboardInterrupt:
        finish('cancelled', 130, 'KeyboardInterrupt')
    except BaseException as exc:
        finish('exception', 2, ''.join(traceback.format_exception(exc))[-4000:])
    else:
        finish(run.get('outcome') if run.get('outcome') not in {None, 'running'} else 'completed', 0)
