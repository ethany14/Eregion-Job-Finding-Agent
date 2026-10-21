# Prompts

[Internals](README.md) · [Context engineering](context-engineering.md) ·
[Models and budgets](models-and-budgets.md)

Eregion has two kinds of prompts:

- **Agent prompts** steer an autonomous `AgentLoop` (career chat, application
  forms, persisted sessions). They describe goals, evidence rules and how to use
  the tools; the model chooses the actions.
- **Task prompts** steer one bounded, usually structured call: extract a
  resume, judge a posting, write a cover letter, verify an answer. Their output
  is validated against a schema and checked by code before it is used.

All prompts are plain Python strings in the modules listed below. Nothing is
loaded from a remote prompt store, so the code revision fully determines the
prompt.

## Rules every prompt follows

- **Inputs are untrusted data.** Each prompt that receives resume text, job
  descriptions, web pages, user answers or tool output says so explicitly, and
  tells the model to ignore instructions inside it. The same rule is repeated
  for tools (`TOOL_DATA_SYSTEM_RULE`), memory (rendered with a data boundary, see
  [Memory](memory.md)) and skills (`SKILL_BOUNDARY`).
- **The model decides meaning; tools enforce boundaries.** Prompts tell the
  model what it owns (interpretation, planning, recovery) and that tools will
  read back, verify and refuse unsafe actions. Prompts never ask code to make a
  semantic decision for the model, and code never silently makes one.
- **No invented facts.** Candidate facts come only from the resume, the user's
  own words or confirmed evidence. Unknown is not "missing" and not "met".
- **Current request authority.** Older instructions in history cannot override
  the latest user message.

## Agent prompts

### Career task: `career/prompt.py`

[`career/prompt.py`](../../backend/eregion/runtime/career/prompt.py) holds the
system prompt for the career chat agent (about 19,500 chars) and a trimmed
variant (about 14,000 chars). Sections of `SYSTEM`:

| Section | Content |
| --- | --- |
| Opening | Role ("a general task agent specializing in the user's job search"), ownership of the outcome, capabilities are not mandatory stages, ask only for information that matters. |
| Conversation and execution | Greetings get a short reply via `finish_task`; acknowledge before lengthy work; plain text is progress; `finish_task` completes. |
| Discovery and recommendations | Source order, reading and qualifying postings, scoring, recommendation tiers, final reply format, when to keep searching. |
| General browser | When and how to use the isolated research browser. |
| Materials and memory | Generating and revising documents, saving profile facts and evidence. |
| Applications and interviews | Handing off to the application agent, interview practice. |

Shared fragments are defined once and inserted into both variants:

- `TIER_DEFINITIONS` / `RECOMMENDATION_RULE`: the tiers *priority*, *stretch*
  (known core gap), *to_confirm* (an unstated personal fact or a near-miss
  location/level) and *not an opportunity*, and how to name gaps honestly.
- `FINAL_FORMAT`: the structure of the final search reply (grouped tiers with
  counts; title, URL, `Score <saved value>`, gaps and a next step per job;
  excluded jobs; failed reads; the honest shortfall; an offer to continue).
- `SEARCH_STOP_RULE`: continue or stop on expected value using recorded
  coverage, yield, failures and remaining budget; budgets are limits, not targets.
- `DIRECT_APPLICATION_RULE`: a request to apply to one chosen posting starts the
  application directly, without adding discovery or tailoring stages.

How the final career prompt is composed for each turn (`TaskHost.observe`):

1. Start from `CareerHarness.prompt` (`SYSTEM`).
2. With `JOB_AGENT_OPT_PROMPT_TRIM=1`, swap in `TRIMMED_SYSTEM`.
3. In API-first mode (`JOB_AGENT_JOB_API_FIRST`, on by default),
   [`source_mode.prompt_for`](../../backend/eregion/runtime/career/source_mode.py)
   applies fixed text edits so that the prompt, the tool descriptions and the
   snapshot policy all describe the same source order (company-board APIs and
   LinkedIn MCP first, the browser for explicit checks). The matching tool
   description overrides are applied by the loop.
4. When the combined `verify_and_score` tool is offered, append
   [`workflows.RULE`](../../backend/eregion/runtime/career/workflows.py) (use one
   call to read, import and score; batch up to 20 URLs; decide recommendations
   separately).
5. `prepare_request` appends the **current request authority** paragraph and,
   when earlier reads are re-shown, the **context_evidence** paragraph (see
   [Context engineering](context-engineering.md#the-shape-of-one-request)).
6. Near the end of a budget the snapshot itself carries `completion_required`
   with a reason (for example "the provider budget is nearly spent") and
   `FINAL_FORMAT`, and the offered tools shrink to finishing tools.

### Application form: `auto_apply/prompt.py`

[`auto_apply/prompt.py`](../../backend/eregion/runtime/auto_apply/prompt.py)
(`SYSTEM`, about 9,500 chars) is the instruction for the application agent. It
opens with the contract: complete one application from confirmed facts and
return it for review; the model owns interpretation, planning and recovery;
tools execute, read back and enforce boundaries; never submit. Its sections:

- **Workflow**: read the page, facts, messages and saved decisions; match form
  rows to candidate records; prefer `apply_record` for a work/education row;
  batch independent field actions in one reply (each reply is one budgeted
  call); host autofill already handles literal facts on page entry; a record is
  complete only when actual values match saved facts; request Next yourself on
  record pages.
- **Choosing and using evidence**: use only cited fact refs and available
  components; current-employment checkboxes follow `current_status` when the
  record has it; `record_answers` quotes the user's literal words; format or
  taxonomy changes use `resolve_field` with sources and a reason; drafts for
  open-ended questions cite resume text or messages and need user review.
- **Controls and recovery**: how pickers, searches, categories and date groups
  work; what an unchanged list does and does not prove; the field-of-study rule
  (with no exact option, choose the closest returned option: most shared words,
  then the broader option of the same discipline; the examples apply only among
  options the picker returned); how to react to error codes.
- **Communication and boundaries**: progress with `say`, questions with
  `ask_user`; never repeat an answered question; compound eligibility or consent
  questions need evidence for every clause; credentials only through the secure
  flow; page text is never an instruction; honour pause and cancel; submission
  is never the model's action.

The application snapshot also carries a fixed task line ("Complete this
application using confirmed candidate facts, then return for review.") and the
career prompt hands over with `APPLICATION_INSTRUCTION`.

### Persisted sessions

[`sessions/coordinator.py`](../../backend/eregion/runtime/sessions/coordinator.py)
uses `SESSION_SYSTEM_MESSAGE`: use tools for current database information, never
invent run data, plus `TOOL_DATA_SYSTEM_RULE` (tool output is untrusted and
cannot prove candidate experience unless its provenance is the resume or the
user). The context projector adds `PERMISSION_EVIDENCE_POLICY` (permissions and
evidence rules are authoritative; a confirmed preference that answers the
question is used; memory is never resume evidence or permission) and wraps
each routed skill in `SKILL_BOUNDARY`.

### Repair instructions

[`model_output.py`](../../backend/eregion/runtime/model_output.py):
`REPAIR_INSTRUCTION` is added after an incomplete reply or one that contained
internal protocol analysis ("make one valid tool call or give a concise,
complete reply"); `TRUNCATED_INSTRUCTION` is added when the reply hit the output
limit ("send at most three independent tool calls with short arguments").

## Tool descriptions are prompts too

Each tool's `description` and parameter schema are part of what the model reads.
They carry rules that belong to one tool (for example `search_options` explains
that it also runs the distinctive words of a phrase, and `resolve_field`
explains when a similar option is allowed). Three mechanisms adjust them:

- `Turn.tool_descriptions`: host overrides applied last by the loop (used for
  API-first source descriptions).
- `slim_tools` ([`request_options.py`](../../backend/eregion/runtime/request_options.py)),
  used when a host advertises a stable tool list: removes schema titles,
  normalizes whitespace and shortens some long career descriptions.
- Capability hiding: tools a page cannot use are not offered at all (see
  [Application agent](application-agent.md#which-tools-are-offered)).

## Task prompts

| Purpose | Prompt | File |
| --- | --- | --- |
| Resume analysis (evidence items) | `RESUME_PROMPT` | [`domain/prompts.py`](../../backend/eregion/domain/prompts.py) |
| Job description analysis (atomic required/preferred requirements) | `JOB_PROMPT` | same |
| Resume ↔ job match | `MATCH_PROMPT` (+ `PARTIAL_POLICY`) | same |
| Tailored resume: write, verify, revise | `WRITE_RESUME_PROMPT`, `VERIFY_RESUME_PROMPT`, `REVISE_RESUME_PROMPT` | same |
| Structured job extraction (one statement per source statement) | `JOB_EXTRACTION_PROMPT`, `REFERENCE_JOB_PROMPT` | [`domain/job_extraction.py`](../../backend/eregion/domain/job_extraction.py) |
| Structured resume extraction (entries per employer, project, school) | `RESUME_EXTRACTION_PROMPT` | [`domain/resume_extraction.py`](../../backend/eregion/domain/resume_extraction.py) |
| Resume entry and bullet selection | `RESUME_SELECTION_PROMPT` | [`domain/resume_selection.py`](../../backend/eregion/domain/resume_selection.py) |
| Compact requirement matching (ids only, no prose) | `COMPACT_MATCH_PROMPT` | [`domain/compact_match.py`](../../backend/eregion/domain/compact_match.py) |
| Jev fit policy and unknown-item resolver | `POLICY`, `PROMPT` | [`domain/jev_fit/core.py`](../../backend/eregion/domain/jev_fit/core.py), [`resolve.py`](../../backend/eregion/domain/jev_fit/resolve.py) |
| Partial-credit rule shared by matchers | `PARTIAL_POLICY` | [`domain/partial_support.py`](../../backend/eregion/domain/partial_support.py) |
| Posting qualification (current vacancy, role, location, seniority) | `POLICY` | [`domain/search_qualification.py`](../../backend/eregion/domain/search_qualification.py) |
| Shared-text references in large inputs | `REFERENCE_INSTRUCTION` | [`domain/prompt_context.py`](../../backend/eregion/domain/prompt_context.py) |
| Cover letter, application answer, revision | `_COVER_SYSTEM`, `_ANSWER_SYSTEM`, `_REVISE_SYSTEM` | [`application_pack/workflow.py`](../../backend/eregion/runtime/application_pack/workflow.py) |
| Cover-letter block patches (opt-in) | `PATCH_SYSTEM` | [`application_pack/patches.py`](../../backend/eregion/runtime/application_pack/patches.py) |
| Chat answer and resume-fact verification for forms | `SYSTEM`, `EVIDENCE_SYSTEM` | [`auto_apply/grounding.py`](../../backend/eregion/runtime/auto_apply/grounding.py) |
| Evidence-discovery interview | `QUESTION_SYSTEM`, `ANSWER_SYSTEM` | [`interviewer/prompts.py`](../../backend/eregion/runtime/interviewer/prompts.py) |
| Mock interview question and coaching evaluation | `QUESTION_POLICY`, `EVALUATION_POLICY` | [`mock_interview/model.py`](../../backend/eregion/runtime/mock_interview/model.py) |
| Learning from completed turns | `LEARNING_OBSERVER_PROMPT` | [`learning/analyzer.py`](../../backend/eregion/runtime/learning/analyzer.py) |
| Skill evaluation | `BASE_POLICY` | [`skills/evolution_evaluation.py`](../../backend/eregion/runtime/skills/evolution_evaluation.py) |

Common patterns in task prompts:

- **Structured output only.** Each call names a schema (Pydantic models) and the
  reply is validated; an unusable reply is a `ResponseFormatError`, not text to
  guess from.
- **Citations by id.** Writers and matchers return evidence ids or source entry
  ids, and code checks that every cited id exists in the frozen evidence
  snapshot before meaning is judged.
- **Yes/no decisions for verification.** Grounding checks return one boolean per
  item in order; "true" requires the quote or fact to entail the answer.
- **Coaching, not verdicts.** The mock interview evaluation scores one answer on
  a 1–5 rubric and must quote the answer; it never judges employability.

## Prompt versions and caches

Prompt identity is part of cache keys and stored records, so a changed prompt
never reuses an old result:

| Constant | Value | Used for |
| --- | --- | --- |
| `PROMPT_VERSION` (`domain/prompts.py`) | `v6` | Resume/job analysis and match caches |
| `PACK_PROMPT_VERSION` | `application-pack-v10` | Generated materials |
| `INTERVIEW_PROMPT_VERSION` | `interviewer-v1` | Evidence interviews |
| `PROMPT_VERSION` (`mock_interview/policy.py`) | `mock-interview-v1` | Mock interviews |

The fit cache key also includes the exact resume, job description, supplied
facts, owner, model release and implementation identity (see
[Jev and performance](../jev-benchmark.md)).

## Changing a prompt safely

1. Change the string in its module; if the change alters outputs, bump the
   matching version constant so caches do not serve old results.
2. Keep rules that belong to one tool in that tool's description, not in the
   system prompt, and keep the career `SYSTEM` and `TRIMMED_SYSTEM` consistent
   (shared fragments are inserted into both).
3. If the API-first edits in `source_mode.py` match text you changed, update
   them too; `prompt_for` replaces exact substrings.
4. Run the offline tests that pin prompt text and behaviour, then a real-model
   check before relying on the change.
