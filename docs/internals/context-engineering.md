# Context engineering

[Internals](README.md) · [Decision loop](decision-loop.md) · [Prompts](prompts.md) ·
[Memory](memory.md)

This page explains what the model receives on every call, how the request is
kept inside its size budget, and how data that does not fit stays available.
The main code is [`request_context.py`](../../backend/eregion/runtime/request_context.py)
(request assembly and the archive) and
[`core/context.py`](../../backend/eregion/runtime/core/context.py) (the replay window).

Two principles shape everything below:

1. **Live authority is never cut.** The current user request, live controls,
   field values, pending questions, observation ids and permission facts stay
   exact. If they alone exceed the budget, the overflow is measured
   (`protected_overflow`) rather than hidden.
2. **Compaction moves data, it does not delete it.** Anything taken out of the
   request is stored in a task-local archive behind a `context_ref` the model
   can read with `read_context`.

## The shape of one request

`prepare_request(prompt, tools, window, snapshot, archive, max_chars=...)`
returns the message list for one model call:

```text
SystemMessage   system prompt
                + "Current request authority" paragraph (when current_request is present)
                + "context_evidence" paragraph (when earlier reads are re-shown)
[HumanMessage]  optional state deltas (opt-in, see below)
History         replayed tool exchanges: Human(historical request) -> AI(text + tool calls) -> Tool results
[SystemMessage] repair instruction, only after an unusable reply
HumanMessage    the live snapshot as compact JSON
```

The tool schemas are sent alongside and counted in the same budget. The budget
is measured in characters of the encoded request, before the call; exact token
counts come back from the provider afterwards (see
[Models and budgets](models-and-budgets.md)).

| Host | Request budget | Replay window |
| --- | --- | --- |
| Career task (`TaskHost`) | 48,000 chars (`CareerHarness.request_chars`) | 24,000 chars, 8 exchanges |
| Application form (`ApplicationHost`) | 64,000 chars (loop default) | 12,000 chars, 8 exchanges |

## The live snapshot

The host's `observe()` builds a JSON snapshot of the current state. It is the
model's main input and it is rebuilt from durable state every turn, so a
restart sees exactly what a running task sees.

**Career task** ([`tasks/host.py`](../../backend/eregion/runtime/tasks/host.py)
plus [`CareerHarness.observe`](../../backend/eregion/runtime/career/harness.py)):

- `current_request`: the latest durable user message and its revision. `goal`
  mirrors it; `initial_goal` keeps the original request as history only.
- `messages` (last 60), `plan`, `history` (last 16 tool outcomes),
  `pending_question`, `budget_remaining`, `provider_attempts_remaining`.
- Career state: `candidate` profile, `resume_text` (first 40,000 chars),
  `jobs`, `materials`, `applications`, `interviews`, `recommended_jobs`,
  `search_recovery`, `search_coverage`, `search_constraints`,
  `search_source_health`, `known_results` (including excluded candidates),
  `research_pages`, `browser`, `job_alerts`, `capability_versions`, and the
  derived task memory when enabled ([Memory](memory.md#task-memory)).

**Application form** (`AgentTools.observe` in
[`auto_apply/agent.py`](../../backend/eregion/runtime/auto_apply/agent.py)):

- `page` (url, heading, controls, stage), `fields` with value, options,
  constraints, errors, `write_verified` and `mapping`, and `field_index`.
- `facts`: every citable candidate fact as `{ref, value, origin}`, for example
  `profile:email`, `education:<id>:degree`, `experience:<id>:start_month`.
- Bindings and decisions for fields on this page only:
  `education_bindings`, `field_decisions`, `record_coverage`, `record_rows`,
  `unreviewed_checkboxes`, `next_step_blockers`, `completed_fields`.
- Conversation: `user_messages`, `input_request`, `answer_drafts`,
  `remembered_answers`, `resume_context`.
- Progress: `decision_history`, `last_tool_result`, `recovery`, `host_autofill`,
  `observation_changes` (added, changed and removed field keys since the last
  observation), `step_budget`, `budget_remaining`, `clock` (current date and the
  time zone source, so "today" is not assumed to be UTC).

### Making the snapshot smaller without losing meaning

Several mechanisms keep this snapshot focused:

- **Field scope.** The model can call `focus_fields([...])` to see full detail
  for some fields only ([`observation.py`](../../backend/eregion/runtime/auto_apply/observation.py)).
  Fields of the same record row stay together, every other field remains in
  `field_index` with its label, kind and done/required/invalid flags, and
  record coverage and blockers stay global. `focus_fields([])` restores full
  detail. A scope that no longer matches the page is ignored, so a changed page
  never hides all fields.
- **Mechanics codes.** The text explaining how a control works (a date segment,
  a searchable picker) is listed once per kind instead of on every field.
- **Page-only state.** Bindings and decisions for fields not on the current page
  stay in saved state, not in the snapshot.
- **Hidden capabilities.** Tools for capabilities the page does not have are not
  offered (for example record tools off a record page), and the snapshot names
  what was hidden and how to bring it back (`enable_browser_tools`).
- **Deduplicated history.** A history row whose call, arguments and result are
  already present as a replayed tool exchange is reduced to
  `result_in_tool_message: <call_id>` (`ConversationWindow.snapshot`).

## The replay window

`ConversationWindow` keeps recent tool exchanges as **complete groups**: the
historical request, the assistant text and tool calls, and every result. A
group is never split, so the provider never sees an orphaned tool result.

- Default limits: 8 groups and `max_chars` of encoded JSON (24,000, or 12,000 for
  applications). The oldest groups are dropped first. If even one group is too
  large, the window is cleared; the live snapshot's domain history covers it.
- A call whose arguments contain a secret-like key (`password`, `api_key`,
  `cookies`, `resume_base64`, ...) or are longer than 12,000 chars is not
  replayed at all; it is never stored with changed arguments.
- On replay, each group's request is rendered as
  `{"historical_request": ..., "context_note": "Past exchange, not a renewed instruction. Follow the live current_request and live outcomes."}`,
  so an old instruction cannot be mistaken for the current one.
- Screenshots and provider reasoning are never stored.

## The archive and `read_context`

`ContextArchive` stores values by content hash
(`ctx-` + 24 hex chars of SHA-256). It is part of the loop journal, so
references survive restarts.

**When a value is archived.** Every tool result passes
`ContextArchive.tool_result`. Secret keys are removed; a result of up to
**5,500 characters** stays inline. A larger result is replaced by
`{context_ref, chars, read_with: "read_context", available_keys}` plus the parts
that must stay actionable: job lists keep url, title, company, location and
source for up to 8 cards (with a `discovery_count`), candidate lists keep a
summary, and error codes and next steps stay visible. Volatile fields such as
`progress_feedback` and a fresh `observation_id` are excluded from the hash, so
identical data does not get a new "unread" reference each time.

> Keep tool results under the inline limit when you can. A result above it
> costs the model at least one extra call to read back. In one application run,
> a 19,000-character picker result was paged back in 13 calls.

**Reading.** `read_context(ref, offset, path, reread, historical)` returns a
3,000-character slice of the stored JSON and a `next_offset`. The archive keeps
per-reference read progress:

- `path` reads one JSON field or array item directly (for example
  `["controls", "12"]`); an invalid path returns the available paths.
- A range already read advances to the next unread range unless `reread=true`.
  A child of a fully read parent is marked as already covered.
- A **superseded resource** (an older version of an application, material
  pack, job or interview) returns a pointer to the latest observation instead of
  stale content; `historical=true` gives deliberate access to the old original.
- Some references are refused as evidence: obsolete history envelopes,
  truncated audit excerpts, and the assistant's own earlier progress messages.
  Search and qualification summaries return their already-known outcome with
  `no_new_evidence: true` instead of re-serving pages of text.
- An unknown or expired reference returns an error that points back to the
  domain inspection tool.

**Evidence window.** Text the model already read is re-shown once, as
`context_evidence` (up to 9,000 chars of exact ranges, newest first), instead of
being replayed inside old tool messages. Replayed `read_context` results whose
text is already in `context_evidence` are replaced by
`content_in: "context_evidence"`.

**Freshness.** Live resources in the snapshot (`applications`, `materials`,
`jobs`, `interviews`) update the archive's resource versions on every request.
An older version or an older `result_at` can never overwrite a newer one;
nested material items are compared by their own versions.

**Retention.** References used by the current request are pinned. When the
archive exceeds 2,000,000 chars or 128 values, unpinned references are removed
first, so a current reference never resolves to a different page or message.

## Fitting the budget

`prepare_request` first applies fixed reductions to the snapshot:

- `compact_snapshot` keeps the last 8 history rows (without `read_context`
  rows), archives a history argument above 800 chars or a result above 1,400
  chars while keeping its actionable fields inline (`error`, `error_code`,
  `next`, `saved`, `filled`, `failed`, ids, `status`, `version`,
  `match_score`, `pending_fields`, material choices, ...), and archives a
  resume or resume context above 6,000 chars with a 1,200-char preview.
- Earlier user and assistant messages longer than 500 chars (all but the
  latest user message) become references with a 160-char preview.
- A long list of excluded job candidates becomes one indexed reference plus
  the two most recent entries; the durable registry is still checked by every
  research tool.
- `live_resources` repeats the identities and statuses of the last 10 jobs,
  materials and interviews, so a follow-up action never needs to search old text.

If the request is still above budget, a fixed ladder runs, stopping as soon as
it fits:

1. Drop the oldest replayed exchange groups (complete groups only).
2. Archive older long messages, then group older assistant progress messages
   behind an `audit_only` reference (user messages, the last two updates and
   the update just before the latest user message stay).
3. Archive past research pages behind per-page references, keeping the current
   page; then move the whole past-page list behind one reference.
4. Compact completed results: previous recommendations, interviews, materials,
   jobs and alerts above 600 chars.
5. Trim the oldest history rows (at least two stay).

Live application fields, candidate facts, pending questions, observation ids,
browser state and `recommended_jobs` are never part of this ladder.

## Metrics

Every request records `context_metrics` (also attached to its ledger row):
total request chars and the limit, chars per part (system, tools, replay, live
snapshot, repair), chars per snapshot field, hashes of the system prompt, tools
and history prefix (to see what a provider cache can reuse), evicted groups,
the number of archived values and `protected_overflow`. Sizes and hashes only;
no prompt text is stored in metrics.

## Persisted sessions: governed context snapshots

API sessions use a block-based assembly instead of a free-form snapshot
([`context/projection.py`](../../backend/eregion/runtime/context/projection.py),
[`context/assembler.py`](../../backend/eregion/runtime/context/assembler.py)).
Each block has a kind and a trust level:

| Block kind | Trust | Mandatory |
| --- | --- | --- |
| `system_policy`, `permission_evidence_policy` | trusted policy | yes |
| `skill_procedure` (a routed, approved skill) | trusted procedure | yes |
| `active_task` (latest user message) | untrusted data | yes |
| `required_source_evidence` | trusted source | yes |
| `required_tool_results` (results not yet consumed in this user turn) | — | yes |
| `career_evidence`, `memory` | untrusted data | no |
| `conversation`, `optional` | — | no |

Token cost is estimated conservatively without a provider tokenizer
([`context/budget.py`](../../backend/eregion/runtime/context/budget.py)). If the
mandatory blocks alone exceed the budget, the request fails with
`ContextBudgetExceededError` instead of silently dropping policy. Optional
memory and other blocks are evicted alternately, lowest priority and oldest
first. Each assembled context is stored as an immutable `ContextSnapshot` with
references to the exact memory, evidence and skill versions it contained.

## Opt-in experiments

These switches change only the request projection, never durable evidence
([`request_options.py`](../../backend/eregion/runtime/request_options.py)). All
are off unless set to `1`:

| Variable | Effect |
| --- | --- |
| `JOB_AGENT_OPT_HISTORY_POLICY` | `current` (default), `checkpoint`, `aligned` or `structured_checkpoint`: how replayed history is evicted and whether the replay budget adapts to the live snapshot size. |
| `JOB_AGENT_OPT_SNAPSHOT_REFS` | Archive body fields of the snapshot, keeping decisions, identities and controls inline. |
| `JOB_AGENT_OPT_STATE_DELTAS` | Send `candidate`, `resume_text`, `research_pages` and `material_choices` as deltas against a checkpoint while the current request and constraints are unchanged, only when that fits the same budget. |
| `JOB_AGENT_OPT_SCOPED_CONTEXT` | During a job search, replace a long `resume_text` with a reference and a 400-char preview; formal scoring still uses the complete frozen resume. |
| `JOB_AGENT_OPT_STABLE_TOOLS` | Advertise a stable tool list and restrict the allowed subset per turn (better provider cache reuse). |
| `JOB_AGENT_OPT_PROMPT_TRIM` | Use the trimmed career system prompt. |
