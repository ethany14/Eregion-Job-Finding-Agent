# Models and budgets

[Internals](README.md) · [Decision loop](decision-loop.md) ·
[Configuration](../configuration.md) · [Jev and performance](../jev-benchmark.md)

This page covers which model handles which call, how every provider call is
counted, and the limits that stop work before it costs more than allowed.

## Model roles

Models are created in [`domain/model.py`](../../backend/eregion/domain/model.py).
`create_model(purpose=...)` picks the model id by purpose and falls back to the
main model:

| Purpose | Model id setting | Reasoning setting |
| --- | --- | --- |
| `agent` (main decisions) | `LLM_MODEL_ID` | `LLM_AGENT_REASONING` |
| `fast` | `LLM_FAST_MODEL_ID`, else `LLM_MODEL_ID` | `LLM_AGENT_REASONING` |
| `extraction`, `search_qualification` | `LLM_EXTRACTION_MODEL_ID`, else `LLM_MODEL_ID` | `LLM_EXTRACTION_REASONING` (qualification defaults to `low` when unset) |
| interview calls (`interview_model`) | `LLM_MODEL_ID` | `LLM_INTERVIEW_REASONING` (default `low`; `off` uses the provider default) |

Reasoning levels are `none`, `minimal`, `low`, `medium` or `high`; another value
is a configuration error.

Environment values win over the project's `.env`. `shared_model` keeps up to 8
clients keyed by a fingerprint of the settings, the purpose and the factory;
`ConfiguredModel` resolves the client on every use, so a model saved later in
the workspace applies without a restart.

### Routing inside an application

`ConfiguredToolModel.invoke_turn`
([`auto_apply/agent.py`](../../backend/eregion/runtime/auto_apply/agent.py))
chooses per turn:

- **Main model** when the turn is a recovery, carries runtime feedback, the page
  has a free-text (`textarea`) field, or the recent decision history contains a
  failure or error code.
- **Fast model** otherwise (ordinary field work).
- Answer verification (does a quoted user statement or a cited resume fact
  support this answer?) uses the fast model with a yes/no structured reply.

When `LLM_FAST_MODEL_ID` is not set, both roles are the same model and the
routing changes nothing.

### Jev decisions

[`fast_decisions.py`](../../backend/eregion/runtime/fast_decisions.py) calls the
TypeSafe API for bounded yes/no and choice decisions (enabled with
`TYPESAFE_ENABLED`). It has no browser effects and generates no text. An answer
is accepted only with confidence ≥ 0.90 and probability ≥ 0.95 for the chosen
option; otherwise the caller falls back to its normal path. Results are cached
by hashed keys. Inside an application, `fast_choices` uses it for option
choices during autofill (purpose `apply_form_options`). Fit scoring can run
entirely on Jev decisions; see [Jev and performance](../jev-benchmark.md).

## The provider ledger

Every provider call goes through one accounted path
([`provider_usage.py`](../../backend/eregion/runtime/provider_usage.py)). The
ledger stores counts and identifiers, **never prompts or credentials**.

- **Attribution.** `usage_scope(...)` is a context manager that tags calls with
  the task id, application run id, request id and a `purpose`. Purposes seen in
  a typical application run: `agent` (the career agent), `apply_turn:<kind>`
  (one application decision, labelled by what the page still needs:
  `semantic_mapping`, `record_binding`, `recovery`, `eligibility`,
  `open_question`, `coordination`), `application_answer_verification`,
  `apply_form_options`. Scopes are re-established when a worker resumes,
  including after a server restart (`track_run`).
- **Before sending** (`UsageLedger.start`): a row is written with an input
  reservation of the request's UTF-8 bytes plus 1,024 (bytes, not a guess at
  tokens; requests with images fail closed under an input cap). Budgets are
  checked here, so a call that would exceed a limit is refused before it is
  sent and recorded as `refused_before_send`.
- **After the reply** (`finish`): the reservation is replaced by the provider's
  actual input, output, cached and reasoning token counts. Absent counters are
  recorded as unknown, never as zero.
- **Late and discarded calls.** A call already sent may still finish and be
  billed after the user steered or cancelled; its row is marked not adopted and
  its result is never used as current evidence. A connection that failed
  before TLS completed is recorded as never sent; any later failure keeps
  unknown usage.
- **Summaries.** `summarize` / `experiment_summary` report calls, completed,
  failed, pending, retries, input and output tokens, cached tokens and cache hit
  ratio, reasoning share, per-purpose groups and whether usage is complete.

## Limits

Limits are checked in several places; the first one reached stops the work with
a saved state, never mid-effect.

### Per task (career conversation)

| Limit | Default | Where |
| --- | --- | --- |
| Model decisions per task | 100 (`TaskLoop(budget=100)`, overridable per task) | [`tasks/loop.py`](../../backend/eregion/runtime/tasks/loop.py) |
| Provider attempts per task | `JOB_AGENT_TASK_MAX_PROVIDER_ATTEMPTS` (unset = no cap) | ledger |
| Input tokens per task | `JOB_AGENT_TASK_MAX_INPUT_TOKENS` (unset = no cap) | ledger |
| Reply reserve | the last 3 attempts are kept for the agent's own decision, reply and one corrected reply | ledger (`REPLY_RESERVE`) |

When the decision budget is used up, the task saves its work, tells the user
"Send continue to allow more steps" and waits (`agent_limit`). Near the end the
snapshot changes the model's options:

- With 3 or fewer decisions left: `completion_required`, research and re-reading
  tools are removed, only `finish_task` and `ask_user` remain.
- With the provider-attempt cap nearly reached (within `REPLY_RESERVE`): no
  further paid search, reading or scoring; record decisions for already-scored
  jobs, then finish.
- After two reads in one revision that added no evidence: `must_decide`, read
  tools are removed.

Tool work other than the agent's own decisions (qualification, scoring, Jev,
verification) that would eat into the reply reserve raises `ReplyBudgetReserved`,
a subclass of `TaskProviderBudgetExceeded` that no fallback may swallow.

### Per application run

| Limit | Default | Where |
| --- | --- | --- |
| Model calls per application | `APPLY_MAX_MODEL_CALLS` = 300; the user can extend by 100 at a time | [`auto_apply/loop.py`](../../backend/eregion/runtime/auto_apply/loop.py) |
| Model calls per business step (page) | 30 (`PAGE_MODEL_CALL_LIMIT`) | [`auto_apply/runner.py`](../../backend/eregion/runtime/auto_apply/runner.py) |
| Unsuccessful attempts per field target | 4 (`FIELD_ATTEMPT_LIMIT`) | runner |
| Worker slice deadline | `max(180 s, model timeout + 5 s)` | runner |

The per-step budget is keyed by a business-step identity, not by observation
revisions, so revisiting a page or pausing never grants new calls
([`steps.py`](../../backend/eregion/runtime/auto_apply/steps.py)). Before a
model request the host checks that the request can finish before the slice
deadline; if not, it yields (a normal, saved stop) instead of starting a call
that would be cut off.

### Per paid experiment

For evaluation runs, `JOB_AGENT_EXPERIMENT_ID` with `JOB_AGENT_EXPERIMENT_LEDGER`,
`JOB_AGENT_EXPERIMENT_MAX_PROVIDER_ATTEMPTS` and
`JOB_AGENT_EXPERIMENT_MAX_INPUT_TOKENS` set a cumulative budget shared by every
task, phase, worker and retry of that experiment. A new task id cannot reset
it, failed calls are counted, and both limits must be set or every call is
refused. Setting the attempts limit to 0 is a zero-send check: the run starts,
and the first call is refused before anything is sent.

## Retries and timeouts

- **Classification.** [`model_runtime.py`](../../backend/eregion/runtime/model_runtime.py)
  maps provider exceptions to safe codes without exposing provider bodies or
  credentials. Only transport failures and unusable response formats are
  recoverable; authentication, payment, configuration and programming errors
  are not retried.
- **Career tasks** retry a failed model request after 1 s and 3 s by default and
  show the user that a retry is in progress. A completed tool is never replayed.
- **Applications** wait before resuming after a failure:
  `model_rate_limited` 5, 15, 45, 120 s; `model_timeout` and `model_unavailable`
  2, 10, 30 s; anything else 1, 3 s; a provider `Retry-After` is honoured, up to
  300 s. An internal error is not retried, because the same code cannot fix it.
- **Interruptible waits.** [`core/model.py`](../../backend/eregion/runtime/core/model.py)
  runs a model call so that cancellation or a new user message interrupts the
  wait; browser objects stay on their owner thread, and an application worker
  keeps the browser's event loop pumping while it waits.

## Telemetry

[`telemetry.py`](../../backend/eregion/runtime/telemetry.py) records timings
and sizes only (message and tool-schema sizes, provider token counters), plus
`outcome_fingerprint` for repeat detection. Tracebacks keep file, line and
function names only, because exception messages can carry candidate data.
