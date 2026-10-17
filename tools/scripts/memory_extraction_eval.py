"""Memory stage 2: real-model extraction accuracy on the labelled cases (paid; run only when approved).

    python tools/scripts/memory_extraction_eval.py --out <dir> [--max-calls 12] [--max-input-tokens 60000] [--precheck]

Each case is one provider call through the production StructuredConversationLearningAnalyzer,
capped by an experiment ledger. Scoring compares concept key (normalised), value (normalised),
type, scope_hint, durability, valid_from and attribution against the labels, and reports
per-field accuracy plus false learning (a signal where none is expected). Nothing is written to
any memory store.
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))


def score(case, signals, normalize_key, normalize_value):
    learned = [s for s in signals if s.signal_type.value in {'preference', 'career_fact'} and s.attribution == 'user_self']
    result = {'id': case['id'], 'expected': len(case['expect']), 'learned': len(learned), 'checks': {}}
    if not case['expect']:
        result['false_learning'] = bool(learned)
        return result
    for index, want in enumerate(case['expect']):
        match = next((s for s in learned if s.signal_type.value == want['type'] and
                      ('key' not in want or normalize_key(s.canonical_key) == want['key'])), None)
        checks = {'found': match is not None}
        if match is not None:
            if 'value' in want:
                checks['value'] = normalize_value(want.get('key', ''), match.normalized_value) == normalize_value(want.get('key', ''), want['value'])
            for field in ('scope_hint', 'durability', 'valid_from', 'attribution'):
                if field in want:
                    checks[field] = getattr(match, field) == want[field]
            if 'quote_contains' in want:
                checks['quote'] = want['quote_contains'].casefold() in (match.evidence_quote or '').casefold()
        result['checks'][str(index)] = checks
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', required=True)
    parser.add_argument('--max-calls', type=int, default=12)
    parser.add_argument('--max-input-tokens', type=int, default=60000)
    parser.add_argument('--precheck', action='store_true', help='build and size the first request; send nothing')
    args = parser.parse_args()
    out = Path(args.out).resolve(); out.mkdir(parents=True, exist_ok=True)
    os.environ.update(JOB_AGENT_EXPERIMENT_ID='memory-extraction-' + out.name, JOB_AGENT_EXPERIMENT_LEDGER=str(out / 'ledger.sqlite3'),
                      JOB_AGENT_EXPERIMENT_MAX_PROVIDER_ATTEMPTS=str(args.max_calls),
                      JOB_AGENT_EXPERIMENT_MAX_INPUT_TOKENS=str(args.max_input_tokens))
    from eregion.domain.model import shared_model
    from eregion.runtime.learning.analyzer import LEARNING_OBSERVER_PROMPT, StructuredConversationLearningAnalyzer
    from eregion.runtime.memory.normalization import normalize_key, normalize_value
    from eregion.runtime.provider_usage import experiment_summary, input_reservation
    from eregion.runtime.tools.messages import AgentMessage
    cases = json.loads((ROOT / 'tests/fixtures/memory_extraction_cases.json').read_text(encoding='utf-8'))
    model = shared_model(purpose='fast')
    if args.precheck:
        from langchain_core.messages import HumanMessage, SystemMessage
        first = cases['cases'][0]
        payload = model._get_request_payload([SystemMessage(LEARNING_OBSERVER_PROMPT), HumanMessage(first['user'])])
        print(json.dumps({'model': getattr(model, 'model_name', None), 'cases': len(cases['cases']),
                          'first_reservation': input_reservation(payload)}))
        return
    analyzer = StructuredConversationLearningAnalyzer(model)
    results = []
    for case in cases['cases']:
        user = AgentMessage(role='user', content=case['user'] + (f" (Application: {case['application']})" if case.get('application') else ''))
        assistant = AgentMessage(role='assistant', content='Noted.')
        try:
            observation = analyzer.analyze(user_message=user, assistant_message=assistant, message_date=cases['message_date'])
            results.append({**score(case, observation.signals, normalize_key, normalize_value),
                            'signals': [s.model_dump(mode='json') for s in observation.signals]})
        except Exception as exc:
            results.append({'id': case['id'], 'error': f'{type(exc).__name__}: {str(exc)[:200]}'})
    usage = experiment_summary(str(out / 'ledger.sqlite3'), 'memory-extraction-' + out.name)
    (out / 'results.json').write_text(json.dumps({'results': results, 'usage': usage}, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps({'cases': len(results), 'errors': sum('error' in r for r in results),
                      'usage': {k: usage.get(k) for k in ('calls', 'input_tokens', 'output_tokens', 'unknown_usage_calls')}}))


if __name__ == '__main__':
    main()
