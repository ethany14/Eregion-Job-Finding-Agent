# Tools and safety

[Internals](README.md) · [Decision loop](decision-loop.md) ·
[Application agent](application-agent.md)

Tools are how the model acts. This page explains how tools are defined and
offered, how a call is checked before and after it runs, how failures are
reported back so the model can repair them, and which boundaries no tool call,
prompt or page content can cross.

## Defining and offering tools

Tools are OpenAI-style function definitions: a name, a description and a JSON
parameter schema. Career tools are declared in
[`career/harness.py`](../../backend/eregion/runtime/career/harness.py), control
tools (`update_plan`, `say`, `ask_user`, `wait_for_user`, `pause_task`,
`wait_for_result`, `finish_task`) in
[`tasks/loop.py`](../../backend/eregion/runtime/tasks/loop.py), and application
tools in [`auto_apply/agent.py`](../../backend/eregion/runtime/auto_apply/agent.py).

A host advertises a different subset each turn, based on observed state rather
than the model's intent:

- **Career:** browser tools other than `browser_open` appear only when a
  research browser exists for the task; `reply_application` disappears when
  every child application already has the latest message; `screen_jobs` needs
  Jev; a pure search request is limited to search, read, import, score and
  decide tools; a scheduled recommendation run gets the same narrow set.
- **Application:** see [Which tools are offered](application-agent.md#which-tools-are-offered).
- **Budget states:** near a budget limit or after reads that added nothing, the
  task host removes reading tools and leaves only finishing tools
  ([Models and budgets](models-and-budgets.md#per-task-career-conversation)).

A tool that is not advertised in the current turn cannot be called: the schema
check rejects it with "Choose a tool advertised in the current observation."

## Checks on every call

1. **Schema** (`validate_call` in
   [`core/engine.py`](../../backend/eregion/runtime/core/engine.py)): advertised
   name, types, enums, required and unknown properties, lengths, item counts and
   ranges. Nothing runs until the arguments match.
2. **Batch rules** (`validate_batch`, application host): checked for the whole
   reply before any effect. See below.
3. **Domain checks inside the tool**: the observation id must be current, a
   field must be on the page, a value must come from a cited source, a choice
   must be one of the observed options, an answer must quote the user's own
   words, and so on. These raise `BrowserBlocked` with an error code.
4. **Repeat guard** (`ApplicationHost.execute` in
   [`auto_apply/runner.py`](../../backend/eregion/runtime/auto_apply/runner.py)):
   each action gets an `outcome_fingerprint` of its name, arguments, the page's
   field values, options and errors, the page, the chat revision and the saved
   answers, bindings and decisions. If the same fingerprint already failed twice,
   the action is not executed again (`repeated_action`); after five failures the
   slice stops with `no_progress`. Changing anything relevant (a new answer, a
   different option, a changed page) produces a new fingerprint.
5. **Field attempt limit**: after 4 unsuccessful attempts with the same target
   on one field, further attempts are refused (`field_attempt_limit`) with the
   decisions that remain: map the fact with `resolve_field`, ask the user, or
   leave an optional field empty with a reason. One read past the limit is still
   allowed, because a read is evidence, not another failed fill. A required field
   that reaches the limit stops the run for the user instead.
6. **After the action**: the page is observed again and the result is compared
   with the live values; `verified: false`, `unchanged` or field failures make
   the outcome a failure the model must address.

### Batch rules

The model may send several independent actions in one reply (one budgeted
call). For the application host, `validate_batch` enforces the order of actions
that touch the same field:

| Earlier → later on the same field | Rule |
| --- | --- |
| evidence → evidence | A field is bound or recorded at most once per turn; merge into one call. |
| write → write | One write per field per turn; check the result next turn. |
| read → write | The write was chosen before the read returned; send the read alone. |
| search → write | The write was chosen before the search returned; send the search alone. |
| write → search | Search first, in its own turn. |
| search → search | Use `extra_queries` on one `search_options` call. |
| write or search → evidence | Evidence must be saved before acting on the field. |

"Evidence" means `bind_education`, `bind_experience`, `bind_document` or
`record_answers`; "search" is `search_options` or `explore_option`; "read" is
`inspect_field` or `read_field`. A read after a write is always allowed (it
verifies the write).

Most conflicts reject the whole batch with `batch_conflict` and name the split.
Two cases repair themselves instead, so the rest of the batch still runs:

- **Overlapping autofill.** A closing `autofill` that names fields other calls
  in the batch already write runs on the remaining keys only and reports
  `dropped_overlap` (or is skipped when nothing remains).
- **Evidence after a search or write of its field.** That binding is held for
  the next turn (`evidence_deferred`); a binding that covers several fields
  runs on the others and reports `deferred_evidence`. If the batch ends with a
  page action, the batch is still refused, because the field would be gone.

Other structural rules: `click_page_action` may only be the last call (it
changes the page); an `autofill` in a batch must name its `field_keys` and be
the last write; an action on a field that has not been observed yet is
refused; a page action closing a batch runs only if every earlier action in the
batch succeeded and verified (`batch_incomplete` otherwise).

## Error recovery

Failures are returned as structured results, not prose the model must guess
from ([`auto_apply/errors.py`](../../backend/eregion/runtime/auto_apply/errors.py)).
Each has `error`, `error_code`, `next` (the repair hint) and `visual_recovery`.
A selection of codes:

| Code | Meaning and expected repair |
| --- | --- |
| `invalid_action` | Wrong arguments or source; correct them. A screenshot does not fix a policy constraint. |
| `stale_observation` | Observe again and use the new field references. |
| `options_changed` | Inspect the field again and choose from its current options. |
| `option_mapping_required` | Map the confirmed fact with `resolve_field` or `explore_option`; do not repeat an unmatched value. |
| `quote_mismatch` | Quote the user's words that state the answer, or ask them. |
| `source_required` | Use a confirmed fact or bind the chat answer; ask only if it is really missing. |
| `validation_error` | Read the field errors and constraints; rewriting the rejected value is not a repair. |
| `date_write_failed`, `date_group_changed` | Inspect the whole date group; keep the confirmed date and fix the input method. |
| `value_not_retained` | The page did not keep the value; it may need a committed option or another format. |
| `batch_conflict`, `batch_incomplete`, `evidence_deferred` | Batch order problems (see above). |
| `repeated_action`, `field_attempt_limit`, `unchanged_failure` | Protective refusals: nothing was executed; change strategy. These are not counted as attempts. |
| `document_pending`, `document_existing_unverified` | Upload the selected document; do not add a second copy of an unverified attachment. |
| `browser_session_lost` | Reopen the browser and restore saved progress; old coordinates are invalid. |
| `dom_interaction_failed` | The DOM action failed; visual tools may be used if enabled (the only code with `visual_recovery: true`). |

Exceptions that are not `BrowserBlocked` are reported by type only, so an
exception message that could contain candidate data is never shown to the model.

## Untrusted content

Everything the agent reads from outside (web pages, job descriptions, resume
text, uploaded files, tool results, stored memory) is data. This is stated in
every prompt ([Prompts](prompts.md#rules-every-prompt-follows)) and enforced in
code:

- Job discovery is read-only and sends no candidate data or cookies
  ([`career/discovery.py`](../../backend/eregion/runtime/career/discovery.py)).
- Application URLs must be public HTTPS pages without embedded credentials, on
  public hosts; local, private-network and metadata addresses are refused
  (`public_https_url` in [`auto_apply/contracts.py`](../../backend/eregion/runtime/auto_apply/contracts.py)).
- An instruction inside a page, posting or answer cannot change the goal,
  reveal secrets or create facts; candidate facts come only from the resume,
  the user's words or confirmed evidence.
- Persisted tool arguments and results are redacted
  ([`security.py`](../../backend/eregion/runtime/security.py)): keys such as
  `password`, `token`, `api_key`, `secret`, `authorization` and `cookie` are
  replaced with `[REDACTED]`.

## Facts that are never guessed

- `candidate_answer` fills a field from the profile only on exact label matches;
  unknown answers and legal or demographic answers are never guessed. Repeated
  school, degree and major fields always come from their own education record.
- Qualifications, eligibility, availability, salary, demographics and consent
  are never filled by a default; they need a cited fact, the user's answer or
  an explicit question.
- Every clause of a compound eligibility or consent question needs evidence; a
  degree or a date does not establish work permission or willingness to
  relocate. Consent needs the user's own first answer.
- Voluntary self-identification is asked, offering the site's "decline" option.
- Interview questions and requirement targets are checked for protected
  attributes ([`protected_attributes.py`](../../backend/eregion/runtime/protected_attributes.py)).

## Credentials and accounts

- Passwords are never requested in chat and never appear in a model tool
  payload. Sign-in runs on the browser worker from a dedicated account flow
  ([`auto_apply/authentication.py`](../../backend/eregion/runtime/auto_apply/authentication.py));
  the model only clicks the visible sign-in control and the host takes over.
- Site login state is stored per owner and site, encrypted with the Windows
  user key ([`browser_sessions.py`](../../backend/eregion/runtime/auto_apply/browser_sessions.py)).
- A CAPTCHA or other verification stops the run with
  `verification_required` and hands the browser to the user.

## The submission fence

Submitting an application is never a model action.

1. Page controls are classified by their label and structure
   (`action_kind` in [`navigation.py`](../../backend/eregion/runtime/auto_apply/navigation.py)):
   `submit`, `next`, `back`, `add_entry`, `authentication`, `field`, `interact`
   or `guarded`. A model's description of a button is never used.
   `click_page_action` can operate navigation, add-entry and similar controls;
   submit and unknown form-submitting buttons are not available to it, and the
   general browser tools refuse applications, messages, purchases and consent.
2. When the form is complete, the run stops at `awaiting_review` with the
   reviewed fields, the optional fields left empty, the answers still pending
   approval, and a **review digest** of the exact fields and page.
3. Only the user's explicit confirmation of that review, carrying the same
   digest, allows submission; any new browser session, edit or chat message
   clears the digest. If any answer used in the run still needs approval, the
   run always waits for review.
4. The host clicks submit once and waits for the employer's confirmation. A
   missing confirmation becomes `submission_unknown` and is never retried
   automatically.

In evaluation runs the test harness additionally replaces the browser with a
review-only browser that blocks every submit, so a test can never submit.

## Tool permissions in sessions

Persisted sessions use registered tools with a risk level and side effect
([`policy.py`](../../backend/eregion/runtime/policy.py)):

| Risk level | Rule |
| --- | --- |
| `read_only` | Allowed when the tool is in the session's allowlist. |
| `local_write` | Needs explicit approval, except server-enabled idempotent local draft tools. |
| `external_write`, `sensitive` | Always need explicit approval. |

The automatic-tool capability is server configuration; tool arguments or
metadata cannot grant it. Skills and MCP tools can narrow the allowed tools,
never widen them.
