"""Print the real-Chrome test files for one CI shard: browser_test_files.py SHARD COUNT.

A real-Chrome test file is a tests/test_*.py that opts in through JOB_AGENT_BROWSER_TESTS.
Files are sorted by size and dealt round-robin, so each shard gets a similar share; the
ordinary suite runs separately, so these shards only add the real-Chrome work."""
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parents[2] / 'tests'


def browser_files():
    files = [p for p in TESTS.glob('test_*.py')
             if ' (' not in p.name and 'JOB_AGENT_BROWSER_TESTS' in p.read_text(encoding='utf-8', errors='replace')]
    return sorted(files, key=lambda p: (-p.stat().st_size, p.name))


def shard(index, count):
    return [p for i, p in enumerate(browser_files()) if i % count == index]


if __name__ == '__main__':
    index, count = int(sys.argv[1]), int(sys.argv[2])
    print(' '.join(f'tests/{p.name}' for p in shard(index, count)))
