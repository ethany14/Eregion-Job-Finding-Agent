# Application agent

[Internals](README.md) · [Decision loop](decision-loop.md) ·
[Tools and safety](tools-and-safety.md) · [Context engineering](context-engineering.md)

The application agent fills one job application in a real browser from
confirmed candidate facts and stops at review. It is an `ApplicationHost` on the
shared `AgentLoop`, with its own observation, tools and budgets. The code is in
[`runtime/auto_apply/`](../../backend/eregion/runtime/auto_apply/).

## Lifecycle of a run

[`loop.py`](../../backend/eregion/runtime/auto_apply/loop.py) (`ApplicationLoop`)
owns runs. Each run is one durable row per posting
([`repository.py`](../../backend/eregion/runtime/auto_apply/repository.py)):
a second attempt for the same posting is refused, and at most
`JOB_AGENT_MAX_OPEN_APPLICATIONS` (default 6) unfinished runs may be open at
once, because each can keep a browser window open.

- **Workers.** Playwright objects belong to the thread that created them, so all
  work for one application runs on that application's worker thread; there are
  `JOB_AGENT_APPLY_WORKERS` workers (default 3, at most 8)
  ([`worker.py`](../../backend/eregion/runtime/auto_apply/worker.py)). A waiting
  worker keeps the browser's event loop pumping.
- **States.** `queued` → `running` → one of the pauses `needs_input` (a question
  for the user) or `awaiting_review` (filled, waiting for the user), or a final
  state `submitted`, `submission_unknown`, `cancelled`, `failed`. A run in
  `submitting` cannot be changed. Cancelling before submission can be undone; a
  submission attempt cannot.
- **Slices.** The worker runs the loop in slices with a deadline; reaching it
  yields and the run continues in the next slice with the same journal and
  budgets.
- **Steering.** A user message increases the run's chat revision; a decision
  made for an older revision is discarded.

## What happens when a page is entered

Before the model is asked anything, `_prepare_page` in
[`runner.py`](../../backend/eregion/runtime/auto_apply/runner.py) does the
mechanical work:

1. Scan the page (fields, controls, page heading and stage).
2. Stop for the user if the site needs sign-in or shows a verification
   challenge.
3. On a new page (a changed page fingerprint), run a **literal autofill**: every
   field whose answer is a known literal fact is filled and read back. Semantic
   option choices are left for the model.
4. Read the options of closed "Select One" menus once (open, read, close;
   nothing chosen), so the model can answer them without opening each menu.
5. Decide whether the page can advance **without** a model turn. It returns the
   page to the model if anything is incomplete: an invalid or empty required
   field, an unanswered consent, a field whose desired answer does not match,
   an unresolved option path, a visible draft, an education gap, a pending
   document, an optional questionnaire question without a saved decision, or a
   page with work/education rows that is not complete.
   - On a record page the host advances only when `ready_to_advance` holds:
     every candidate record of the kinds the page shows is complete (bound and
     every field read back against its fact or saved decision) or explicitly
     omitted by the model's plan, **and** a model turn has already seen the
     current chat revision.
   - Otherwise, with exactly one enabled Next control, the host clicks it and
     repeats from step 1 (up to 12 pages per observation).
6. On the review page with nothing left, the host calls `ready_for_review` and
   the run stops at review.

The result of the page-entry autofill is shown to the model as `host_autofill`.

## The observation

See [Context engineering](context-engineering.md#the-live-snapshot) for the
full list. The parts that drive decisions:

- **`fields`**: every control with its label, kind, value, options,
  constraints, errors, `required`, `write_verified` and `mapping` (whether the
  current value is resolved).
- **`facts`**: citable candidate facts, each `{ref, value, origin}`. References
  look like `profile:email`, `education:<id>:field_of_study`,
  `experience:<id>:start_month`, `answer:<field_key>` or `message:<id>`. Fields
  can only be filled from a cited ref; a value typed by the model without a
  source is refused.
- **`record_rows`** and **`record_coverage`**: which DOM rows exist, and for
  each resume record whether it is bound, verified or still pending.
- **`next_step_blockers`**, **`unreviewed_checkboxes`**, **`completed_fields`**.
- **`user_messages`**, **`input_request`**, **`answer_drafts`**,
  **`remembered_answers`**.
- **`step_budget`** (calls left on this page) and **`clock`**.

## How fields get their values

### Literal facts

`desired_answer` ([`autofill.py`](../../backend/eregion/runtime/auto_apply/autofill.py))
returns the value a field should have, in this order of precedence:

1. a saved model decision (on a bound field, only if it cites that record, an
   answer or a message);
2. a recorded user answer;
3. the selected cover letter text, for a cover-letter text box;
4. a bound work or education record component;
5. a **structural fact**;
6. an exact-label profile answer;
7. the consent policy (by default, consent is asked, never assumed).

- **Structural facts** ([`structural_facts.py`](../../backend/eregion/runtime/auto_apply/structural_facts.py))
  apply only to controls identified by their structure, never by wording.
  Example: a Workday `phoneNumber--phoneNumber` text field takes the national
  part of a profile phone written with an explicit `+<country code>` prefix, and
  an `address--city` field takes the city of a profile value written exactly as
  `City, XX`. Anything else is left to the model.
- **Exact labels** (`candidate_answer` in
  [`contracts.py`](../../backend/eregion/runtime/auto_apply/contracts.py)) map
  first name, last name, email and similar labels to profile fields; legal and
  demographic answers are never guessed, and school, degree and major fields
  are never filled from a single profile value.

Autofill reads every write back. A failed fill records a failure signature (the
target answer, the control's value and options, its error state); it is not
retried until one of them changes (`unchanged_failure`).

### Work and education records

Resume records are cited by id
([`experience.py`](../../backend/eregion/runtime/auto_apply/experience.py),
[`education.py`](../../backend/eregion/runtime/auto_apply/education.py)). The
model decides which form row belongs to which record and binds them:

- `plan_records` states which records to include or omit (with a reason).
- `bind_experience` / `bind_education` bind each field of a row to a record
  component (title, company, location, start and end month and year,
  `current_status`; school, degree, field of study, dates).
- `apply_record` combines bindings, the model's own decisions and literal fills
  for one row in a single call; the host lowers it to ordinary calls
  ([`record_actions.py`](../../backend/eregion/runtime/auto_apply/record_actions.py)).
- When a row reveals new controls after it was bound (unchecking "I currently
  work here" reveals the end date), those controls inherit the row's binding
  and the host fills them.
- **`current_status`** is a derived fact for a current-employment checkbox:
  true only for an explicit current marker ("Present"), false for an explicit
  no or an end date that has passed. A missing or future end date stays
  undecided and is the model's or the user's decision.
- Dates are projected into the page's representation (month and year
  segments, padding) without inventing missing parts.
- Coverage ([`record_coverage.py`](../../backend/eregion/runtime/auto_apply/record_coverage.py))
  checks that every requested record was created and verified. It never decides
  which records belong; that is the model's plan.

### The model's decisions

`resolve_field` saves a decision with sources and a reason, in one of four modes:

| Mode | Use |
| --- | --- |
| `format` | The same fact in another format (a date split, a city without its state). The value must preserve the source; inventing digits or facts is refused. |
| `equivalent` | An observed option that represents the fact (a "Masters" option for an M.Eng.). |
| `default` | Incidental choices only, such as a phone type. Never for qualifications, eligibility, availability, salary, demographics or consent. |
| `leave_empty` | An optional field with no fact, with the reason shown to the user at review. Required fields cannot be left empty. |

Decisions are tied to the field's signature; a saved omission is complete only
while the field is actually empty
([`decisions.py`](../../backend/eregion/runtime/auto_apply/decisions.py)).

### Choice controls and pickers

- `inspect_field` opens a menu, `search_options` types a grounded search,
  `choose_option` selects an observed option, and `explore_option` opens a
  category (a category alone is never an answer).
- A search query must be the cited fact itself, or a shorter or alternative term
  with a `query_reason`; a query never changes the saved answer.
- **Word fallback.** Pickers such as Workday's filter on single words. When a
  multi-word phrase has no literal match, the same `search_options` call also
  runs its distinctive words (at most three, stopwords skipped) and reports each
  as an automatic search.
- **Full filtered lists.** These menus render only the rows near the visible
  part of the list. When every rendered option contains a word of the query (a
  filtered result), the browser scrolls the list to its end in bounded steps and
  merges each read; the unfiltered catalogue that a phrase can leave is not
  scrolled. Selecting an option that has scrolled out of view scrolls the menu
  to find the exact label.
- **Options sharing words.** Without a literal match, the result lists
  `options_sharing_words`: observed options sharing a distinctive word with the
  fact, most shared words first, then the broader option of the same discipline.
  This is retrieval, not a decision; the model saves the choice with
  `resolve_field mode=equivalent` (for a field of study it chooses the closest
  returned option rather than leaving it empty).
- **Reuse.** An identical search on the same page with the field unchanged
  returns its earlier results instead of driving the picker again.
- Matching of options is field-aware
  ([`matching.py`](../../backend/eregion/runtime/auto_apply/matching.py)):
  cities match complete geographic components, schools and countries have their
  own rules, and a salary needs compatible units.

### Answers from the user

- `ask_user` asks for a genuinely missing fact, naming the field and record.
  Several questions can be answered in one reply; short ordered replies are
  bound to the questions actually shown before any model call
  ([`conversation.py`](../../backend/eregion/runtime/auto_apply/conversation.py)).
- `record_answers` saves an answer by quoting the user's literal words plus the
  model's interpretation of meaning, polarity and scope. A plain "yes"
  confirms exactly the value the agent proposed in the question before it.
- Non-literal answers (a yes/no inferred from a sentence, a choice inferred
  from resume facts) are checked by a separate verification call that must
  return true for each item ([`grounding.py`](../../backend/eregion/runtime/auto_apply/grounding.py)).
- An answer to one question never answers a separate consent.

### Open-ended questions and documents

- `draft_answer` writes an answer from literal resume text or the user's
  messages, states missing details, and must be reviewed by the user through
  `request_input` ([`drafting.py`](../../backend/eregion/runtime/auto_apply/drafting.py)).
- `bind_document` binds a file control to the resume or cover letter selected
  for this application. An upload is skipped only when this application's own
  record shows the same file content was uploaded under that name; a file name
  alone never proves content. Unverified existing attachments are not
  duplicated; the user decides.

## Which tools are offered

The model sees only tools that apply to the current page (`capability_hidden`
in [`runner.py`](../../backend/eregion/runtime/auto_apply/runner.py)):

- `apply_record` and `plan_records` only on record pages;
- `bind_document` only with a file control; `draft_answer` only with a
  free-text or narrative question;
- `search_options`, `explore_option`, `choose_option`, `inspect_field` only
  with choice controls;
- `use_remembered_answer` only when saved answers exist and reuse is on;
- `autofill` only when some field still differs from its known answer;
- low-level browser tools only after `enable_browser_tools` (or outside the
  application form), and visual tools (`view_browser`, `computer_*`) only when
  visual fallback is enabled.

Asking, saying, reading, waiting, field decisions and page actions are always
offered. The observation names the hidden tools and how to restore them;
hiding changes visibility only, never safety checks.

## Budgets and stops

Per page: 30 model calls; per application: 300 by default (extendable by the
user); per field target: 4 unsuccessful attempts. See
[Models and budgets](models-and-budgets.md#per-application-run). The run stops
with a saved state and a clear message for: a page or application budget, a
question for the user, sign-in or verification, repeated failures
(`no_progress`), or review. It never submits; see
[the submission fence](tools-and-safety.md#the-submission-fence).
