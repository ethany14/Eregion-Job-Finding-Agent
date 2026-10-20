"""Layer-1 source route experiment: isolation and the offline zero-call precheck (spec 11.3-11.4, F4)."""
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools' / 'scripts'))
import source_route_experiment as experiment  # noqa: E402


def test_offline_precheck_runs_both_routes_with_zero_network_and_zero_model_calls(tmp_path):
    out = tmp_path / 'precheck'
    done = subprocess.run([sys.executable, str(ROOT / 'tools/scripts/source_route_experiment.py'), 'precheck', '--out', str(out)],
                          capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=240)
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    record = json.loads((out / 'exit.json').read_text(encoding='utf-8'))
    assert record['outcome'] == 'completed' and record['killed_descendants'] == []
    for route, allowed in (('A', {'boards', 'linkedin_mcp'}), ('B', {'jobspy_indeed', 'jobspy_linkedin'})):
        snapshot = json.loads((out / f'snapshot-{route}.json').read_text(encoding='utf-8'))
        guards = snapshot['guards']
        assert guards['valid'] and guards['model_attempts'] == 0 and guards['blocked_sockets'] == 0
        assert guards['blocked_subprocesses'] == 0 and guards['refused_hosts'] == []
        # Each route contains only its own sources: no cross-route fallback, no browser search.
        assert all(set(q['sources']) == allowed for q in snapshot['queries'])
        assert [q['id'] for q in snapshot['queries']] == [q['id'] for q in experiment.MANIFEST['queries']]
    metrics = json.loads((out / 'metrics.json').read_text(encoding='utf-8'))
    board = metrics['A']['backend-python']['boards']
    # Board APIs cannot apply the registered filters: reported as unapplied, never as applied.
    assert board['unapplied_filters'] == ['experience_level', 'posted'] and board['applied_filters'] == {}
    jobspy = metrics['B']['backend-python']['jobspy_indeed']
    assert jobspy['applied_filters'] == {'posted': 'week'} and jobspy['unapplied_filters'] == ['experience_level']
    # Missing company or description stays visible; nothing is borrowed from the other route.
    assert jobspy['missing_company'] == 1 and jobspy['jd'].get('missing') == 1
    assert set(metrics['cross_route']) == {'backend-python', 'junior-data'}


def test_route_a_refuses_a_host_outside_its_allowlist():
    guards = experiment.Guards(offline=False)
    transport = experiment.allowlist_transport(httpx.MockTransport(lambda request: httpx.Response(200, json={})), guards)
    with httpx.Client(transport=transport) as client:
        assert client.get('https://boards-api.greenhouse.io/v1/boards/x/jobs').status_code == 200
        with pytest.raises(httpx.ConnectError):
            client.get('https://html.duckduckgo.com/html/?q=jobs')  # the public-search fallback is not a route A source
    report = guards.report()
    assert report['refused_hosts'] == ['html.duckduckgo.com'] and not report['valid']


def test_a_hidden_model_call_is_refused_and_invalidates_the_run():
    from eregion.runtime.provider_usage import provider_call, usage_scope
    guards = experiment.Guards(offline=False)
    sent = []
    with usage_scope(provider_guard=guards.provider_guard):
        with pytest.raises(experiment.ModelCallForbidden):
            with provider_call('fixture-model', input_token_reservation=10):
                sent.append(True)
    assert sent == [] and guards.report()['model_attempts'] == 1 and not guards.report()['valid']


def test_layer2_builds_isolated_routes_from_frozen_snapshots_and_dry_runs_without_calls(tmp_path):
    layer1 = tmp_path / 'layer1'
    script = lambda name: str(ROOT / 'tools/scripts' / name)
    run = lambda *args: subprocess.run([sys.executable, *args], capture_output=True, text=True, encoding='utf-8',
                                       errors='replace', timeout=300)
    assert run(script('source_route_experiment.py'), 'precheck', '--out', str(layer1)).returncode == 0
    out = tmp_path / 'layer2'
    for route in ('A', 'B'):
        done = run(script('source_route_scoring.py'), 'dry', '--snapshot', str(layer1 / f'snapshot-{route}.json'), '--out', str(out))
        exit_record = out / f'exit-dry-{route}' / 'exit.json'
        # The guard writes the exception into exit.json, which tmp_path deletes; keep it in the failure message.
        assert done.returncode == 0, (done.stdout[-2000:] + done.stderr[-2000:]
                                      + (exit_record.read_text(encoding='utf-8')[-4000:] if exit_record.exists() else ''))
        record = json.loads((out / f'exit-dry-{route}' / 'exit.json').read_text(encoding='utf-8'))
        result = record['result']
        assert record['outcome'] == 'completed' and result['task_usage']['calls'] == 0 and result['board_hosts_refused'] == []
        manifest = json.loads((out / f'layer2-{route}.json').read_text(encoding='utf-8'))
        assert manifest['isolation']['mcp_servers'] == 0 and 'search_jobs' in manifest['isolation']['refused_tools']
        # A posting without a description stays visible and unscorable; it is never filled from the other route.
        assert manifest['imported'] + manifest['unscorable_missing_jd'] == len(manifest['included'])
    # Separate databases and decision caches per route.
    assert (out / 'route-A.sqlite3').exists() and (out / 'route-B.sqlite3').exists()
    b = json.loads((out / 'layer2-B.json').read_text(encoding='utf-8'))
    assert b['unscorable_missing_jd'] == 2 and all(e['reason'] == 'missing_title_or_company' for e in b['excluded'])


def test_layer2_refuses_a_warm_cache_or_a_different_code_version(tmp_path):
    script = lambda name: str(ROOT / 'tools/scripts' / name)
    run = lambda *args: subprocess.run([sys.executable, *args], capture_output=True, text=True, encoding='utf-8',
                                       errors='replace', timeout=300)
    layer1 = tmp_path / 'layer1'
    assert run(script('source_route_experiment.py'), 'precheck', '--out', str(layer1)).returncode == 0
    snapshot = lambda route: str(layer1 / f'snapshot-{route}.json')
    out = tmp_path / 'layer2'
    assert run(script('source_route_scoring.py'), 'build', '--snapshot', snapshot('A'), '--out', str(out)).returncode == 0
    manifest = json.loads((out / 'layer2-A.json').read_text(encoding='utf-8'))
    assert manifest['cold_start']['analysis_cache_rows'] == 0 and manifest['code_tree_sha256']
    # The same route again: its database (and so its decision cache) is not new.
    detail = lambda folder, route: json.loads((folder / f'exit-build-{route}' / 'exit.json').read_text(encoding='utf-8'))['detail']
    again = run(script('source_route_scoring.py'), 'build', '--snapshot', snapshot('A'), '--out', str(out))
    assert again.returncode != 0 and 'fresh database' in detail(out, 'A')
    # A decision cache left next to route B's database: refused before anything is built.
    warm = tmp_path / 'warm'; warm.mkdir()
    (warm / 'route-B.jev-fit.sqlite3').write_bytes(b'')
    refused = run(script('source_route_scoring.py'), 'build', '--snapshot', snapshot('B'), '--out', str(warm))
    assert refused.returncode != 0 and 'cold Jev decision cache' in detail(warm, 'B')
    # Route B against a different recorded version: refused.
    (out / 'code-fingerprint.json').write_text(json.dumps({'tree_sha256': 'other', 'files': 0}), encoding='utf-8')
    changed = run(script('source_route_scoring.py'), 'build', '--snapshot', snapshot('B'), '--out', str(out))
    assert changed.returncode != 0 and 'same version' in detail(out, 'B')


def test_cost_breakdown_reads_a_run_without_provider_calls(tmp_path):
    script = lambda name: str(ROOT / 'tools/scripts' / name)
    run = lambda *args: subprocess.run([sys.executable, *args], capture_output=True, text=True, encoding='utf-8',
                                       errors='replace', timeout=300)
    layer1, out = tmp_path / 'layer1', tmp_path / 'layer2'
    assert run(script('source_route_experiment.py'), 'precheck', '--out', str(layer1)).returncode == 0
    assert run(script('source_route_scoring.py'), 'dry', '--snapshot', str(layer1 / 'snapshot-A.json'), '--out', str(out)).returncode == 0
    report_path = tmp_path / 'cost.json'
    done = run(script('scoring_cost_breakdown.py'), str(out), 'A', '--out', str(report_path))
    assert done.returncode == 0, done.stderr[-2000:]
    report = json.loads(report_path.read_text(encoding='utf-8'))['A']
    # Nothing was paid or scored: no chunks, every sampled job unscored, its description length still reported.
    assert report['chunks'] == [] and report['jobs'] == [] and report['main_model_turns']['count'] == 0
    assert len(report['unscored']) == 16 and all(u['description_chars'] > 0 for u in report['unscored'])
