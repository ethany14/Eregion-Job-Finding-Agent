"""Pure lowering of a model's record plan; never executes tools or decides values."""
from eregion.runtime.core.engine import ToolArgumentsError
from .record_coverage import candidates, binding_ref
from .observation import record_identity
from .decisions import source_value


def expand(session, call):
    args = call['args']
    if args['observation_id'] != session.observation_id:
        raise ToolArgumentsError('Stale observation. Read the current row before applying a record.')
    ref = args['record']
    if ref not in candidates(session.state):
        raise ToolArgumentsError('Use the exact record_coverage.record (including work: or education:). Available: ' + ', '.join(candidates(session.state)))
    kind, identifier = ref.split(':', 1)
    bindings, decisions, fill = args['bindings'], args['decisions'], args['fill_fields']
    if any(len(items) > 20 for items in (bindings, decisions, fill)) or not (bindings or decisions or fill):
        raise ToolArgumentsError('Provide a nonempty record plan with at most 20 entries per list.')
    fields = {f['key']: f for f in session.fields}
    keys = {item['field_key'] for item in bindings + decisions} | set(fill)
    if not keys <= fields.keys():
        raise ToolArgumentsError('Every target must be an observed field.')
    # A repeated section label is not a row identity. Unsupported sites keep the
    # existing individual tools instead of guessing which controls form a row.
    groups = {record_identity(fields[k]) for k in keys}
    if len(groups) != 1 or None in groups:
        raise ToolArgumentsError('apply_record requires one structurally identified row; use individual tools otherwise.')
    group = next(iter(groups))
    if (group.startswith('workExperience-') and kind != 'work') or (group.startswith('education-') and kind != 'education'):
        raise ToolArgumentsError('The selected record kind does not match the observed row.')
    # Include unselected peers: a row already assigned to another record cannot be
    # repurposed accidentally by sending a narrow plan for one of its fields.
    if any(binding_ref(f, session.state) not in (None, ref) for f in fields.values() if record_identity(f) == group):
        raise ToolArgumentsError('This row is already bound to another record.')
    prefix = ('experience:' if kind == 'work' else 'education:') + identifier + ':'
    for decision in decisions:
        for source in decision['source_refs']:
            if source.startswith(('experience:', 'education:')) and not source.startswith(prefix):
                raise ToolArgumentsError('A record decision cannot cite another record.')
            if source_value(session.state, source) in (None, ''):
                raise ToolArgumentsError('A cited fact is unavailable.')
    control = args.get('then_control_id')
    if control and not any(c.get('id') == control for c in (session.page_context or {}).get('controls', [])):
        raise ToolArgumentsError('Choose an observed final page control.')
    observation = {'observation_id': args['observation_id']}
    actions = [('plan_records', {**observation, 'records': [{'record': ref, 'status': 'include',
        'reason': 'The model explicitly selected this record in apply_record.', 'source_refs': []}]})]
    if bindings:
        actions.append(('bind_experience' if kind == 'work' else 'bind_education',
            {**observation, 'experience_id' if kind == 'work' else 'education_id': identifier, 'fields': bindings}))
    actions.extend(('resolve_field', {**observation, **decision}) for decision in decisions)
    if fill:
        actions.append(('autofill', {'field_keys': fill, 'literal_only': True}))
    if control:
        actions.append(('click_page_action', {**observation, 'control_id': control}))
    return [{'id': f"{call['id']}:{i}", 'name': name, 'args': values} for i, (name, values) in enumerate(actions)]
