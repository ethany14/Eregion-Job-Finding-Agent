"""Memory upgrade stage 1: preview (default) or apply the legacy owner/profile ID normalisation.

    python tools/scripts/memory_scope_migration.py --db <path.sqlite> [--out plan.json]
    python tools/scripts/memory_scope_migration.py --db <path.sqlite> --apply --plan plan.json

Preview opens the database read-only and writes nothing. --apply needs the plan file produced by
a preview of the same database; conflicts in that plan are never applied.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    parser.add_argument('--owner', default='local-user')
    parser.add_argument('--legacy', action='append', default=None, help='legacy ID of the same owner (default: "default")')
    parser.add_argument('--out', help='write the preview plan here')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--plan', help='plan file from a preview (required with --apply)')
    args = parser.parse_args()
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from eregion.runtime.memory.migration import MigrationPlan, PlannedChange, apply, preview
    db = Path(args.db).resolve()
    legacy = tuple(args.legacy or ['default'])
    if not args.apply:
        engine = create_engine(f"sqlite:///file:{db.as_posix()}?mode=ro&uri=true")
        plan = preview(sessionmaker(engine), owner_id=args.owner, legacy_ids=legacy)
        engine.dispose()
        text = json.dumps(plan.to_dict(), ensure_ascii=False, indent=1)
        if args.out:
            Path(args.out).write_text(text, encoding='utf-8')
        print(json.dumps({'changes': len(plan.changes), 'conflicts': len(plan.conflicts), 'unchanged': plan.unchanged}))
        return
    if not args.plan:
        raise SystemExit('--apply needs --plan from a preview of this database.')
    data = json.loads(Path(args.plan).read_text(encoding='utf-8'))
    plan = MigrationPlan(owner_id=data['owner_id'], legacy_ids=data['legacy_ids'], conflicts=data['conflicts'],
                         changes=[PlannedChange(**c) for c in data['changes']])
    engine = create_engine(f"sqlite:///{db.as_posix()}")
    applied = apply(sessionmaker(engine), plan)
    engine.dispose()
    print(json.dumps({'applied': applied, 'conflicts_left': len(plan.conflicts)}))


if __name__ == '__main__':
    main()
