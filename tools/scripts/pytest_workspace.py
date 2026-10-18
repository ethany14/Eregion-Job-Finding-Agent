"""Run pytest with inherited Windows workspace ACLs in restricted environments.

Python 3.13 mkdir(mode=0o700) installs an owner-only DACL that excludes the
sandbox identity. Use normal inherited ACLs for test scratch directories only.
Network/config isolation remains owned by tests/conftest.py.
"""
import os
import sys
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
if os.name == 'nt':
    original_mkdir = os.mkdir
    def inherited_mkdir(path, mode=0o777, *, dir_fd=None):
        return original_mkdir(path, 0o777 if mode == 0o700 else mode, dir_fd=dir_fd)
    os.mkdir = inherited_mkdir

def check_windows_loopback():
    """Fail before starting TestClient threads if Windows socketpair cannot connect."""
    if os.name != 'nt':
        return
    try:
        subprocess.run([sys.executable, '-c',
                        'import socket; a,b=socket.socketpair(); a.close(); b.close()'],
                       check=True, capture_output=True, timeout=10)
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            "Windows loopback socketpair is unavailable. TestClient cannot start its asyncio "
            "event loop in this environment. Allow local loopback or run the offline tests "
            "outside the network sandbox; do not disable tests/conftest.py network isolation."
        ) from exc


if __name__ == '__main__':
    try:
        check_windows_loopback()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    import pytest
    raise SystemExit(pytest.main(sys.argv[1:]))
