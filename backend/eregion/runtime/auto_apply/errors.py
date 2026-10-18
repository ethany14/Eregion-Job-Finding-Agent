"""Action failures tell the agent what to repair, without guessing from prose."""
RECOVERY = {
    'verification_unavailable': ('The user fact is saved but the semantic verifier is unavailable. Request an explicit field choice without treating the original answer as incorrect.', False),
    'invalid_action': ('Read the tool error and correct the arguments or source. A screenshot does not repair a source or policy constraint.', False),
    'quote_mismatch': ('Quote the words in the user message that state this answer; the value must be those words, the same yes/no, or the same option. If the user did not state it, ask them.', False),
    'source_required': ('Use confirmed profile/education facts or bind the relevant chat answer with record_answers. Ask the user only if the fact is actually missing.', False),
    'education_source_mismatch': ('Use the existing binding source named in the error; a month/year control needs that component, not the full date. Rebind only if the record mapping itself is wrong.', False),
    'education_binding_required': ('Bind this field to the correct education record, then retry using that confirmed source.', False),
    'stale_observation': ('Observe the current page again and use its new field/control references.', False),
    'stale_screenshot': ('Capture a fresh viewport before using coordinates; do not reuse stale coordinates.', False),
    'options_changed': ('Inspect this field again and select from its current options. Use visual tools only if the menu cannot be operated through DOM tools.', False),
    'option_mapping_required': ('Inspect the available options and map the confirmed fact with resolve_field or explore_option. Do not repeat an unmatched value.', False),
    'validation_error': ('Read the field errors and constraints, correct the representation or missing choice, and verify it. Rewriting the rejected value is not a repair.', False),
    'batch_incomplete': ('Repair the failed or unverified actions named in the message, then use the page action again.', False),
    'experience_source_mismatch': ('Cite facts of the work record this field is bound to; rebind only if the row belongs to another record.', False),
    'date_write_failed': ('Use read_field to inspect the date group. Keep the confirmed date; repair the input method, not the candidate fact.', False),
    'date_group_changed': ('A sibling date segment changed. Use read_field, then repair only from confirmed facts; this write is not verified.', False),
    'value_not_retained': ('Observe the field again, check whether it needs a committed option or another input format, and choose a different action.', False),
    'document_pending': ('Bind the correct file control and upload the selected document before review.', False),
    'document_existing_unverified': ('The upload control already shows attachments this application has not verified as the current document. Do not add another copy: ask the user to keep them (bind_document keep_existing) or remove them in the browser.', False),
    'input_pending': ('Read and bind the latest user reply. Wait or ask only if an actual fact is still missing.', False),
    'browser_session_lost': ('The browser session closed. Reopen it and restore saved progress; coordinates from the old session cannot be used.', False),
    'dom_interaction_failed': ('DOM interaction failed. Inspect the current viewport and use computer tools if available, then verify the field.', True),
    'field_attempt_limit': ('Repeating this field\'s unsuccessful operation is blocked. Decide from the observed options: map the fact with resolve_field, ask the user, or record resolve_field mode=leave_empty with your reason.', False),
    'unchanged_failure': ('This fill already failed and nothing relevant changed. Choose a different option or source with field tools, leave an optional field, or ask the user; autofill will not retry it until something changes.', False),
    'repeated_action': ('This failed action has not changed the page. Repair its cause, try a different action, or continue independent work; do not repeat it unchanged.', False),
}

class BrowserBlocked(RuntimeError):
    def __init__(self, message, *, code='invalid_action'):
        super().__init__(message)
        self.code = code

def failure_detail(error):
    code = getattr(error, 'code', None)
    if type(error).__name__ == 'TargetClosedError':code = 'browser_session_lost'
    if code not in RECOVERY:
        code = 'dom_interaction_failed' if type(error).__module__.startswith('playwright.') else 'invalid_action'
    hint, visual = RECOVERY[code]
    return {'error': str(error) if isinstance(error, BrowserBlocked) else type(error).__name__,
            'error_code': code, 'next': hint, 'visual_recovery': visual}

def failure(code, message):
    return failure_detail(BrowserBlocked(message, code=code))
