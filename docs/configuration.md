# Configuration

[Home](../README.md) · [User guide](usage.md) · [Development](development.md)

Run commands from the repository root with the project's virtual environment
active. Keep a single trusted local backend process.

## Model connection

The model and TypeSafe adapters read the repository-root `.env` file. Process
environment variables override values in that file. Restart the backend after
changing configuration so active services use the new settings consistently.

**From the app.** "Model and API key" in the sidebar sets `LLM_API_KEY`,
`LLM_BASE_URL`, `LLM_MODEL_ID`, `LLM_FAST_MODEL_ID` and
`LLM_EXTRACTION_MODEL_ID` in the same `.env` file, without a restart: the next
conversation turn or application uses them, and work already running keeps its
model. "Load models" reads the provider's OpenAI-compatible `GET /models` list
with the key; it makes no model call. The key is write-only: the app shows only
that one is saved and its last four characters. A variable set in the server's
process environment overrides `.env`, so the app shows it as locked. These
settings can be read or changed only from the local workspace (a loopback host
and client, same origin). The sidebar entry shows a dot until a model and key
are configured. A server with no model configured still starts, so this page is
reachable on a fresh install.

The same dialog's **Features** section turns optional parts on and off and
stores them in `.env`: Jev fit scoring (`TYPESAFE_ENABLED`, `TYPESAFE_API_KEY`,
`TYPESAFE_CAREER_ENABLED`, `TYPESAFE_FIT_RESOLVE_UNKNOWNS`), job sources
(`JOB_AGENT_JOB_API_FIRST`, `JOB_AGENT_TREG_ENABLED`, `TREG_TOKEN`,
`JOB_AGENT_TREG_SEARCH_PROVIDER`, `JOB_AGENT_JOBSPY_PYTHON`,
`JOB_AGENT_JOBSPY_SITES`), applications (`JOB_AGENT_BROWSER_DRIVER`,
`JOB_AGENT_HIDDEN_BROWSER`, `JOB_AGENT_TASK_MEMORY_MODE`) and materials
(`JOB_AGENT_ANALYSIS_WARMUP`, `JOB_AGENT_OPT_MATERIAL_PATCHES`). A switch that
needs a key or another switch cannot be turned on without it. Settings whose
readers use the process environment are also set in the running server and are
loaded from `.env` when it starts; task memory applies after a restart. Values
set in the server's own environment win and are shown as locked.

| Variable | Default | Purpose |
| --- | --- | --- |
| `LLM_MODEL_ID` | Required | Main model for chat, planning, writing, and verification. |
| `LLM_API_KEY` | Provider/client configuration | API credential; set it explicitly for your provider. |
| `LLM_BASE_URL` | Client default | Custom OpenAI-compatible endpoint, when needed. |
| `LLM_TIMEOUT` | `45` seconds | Positive timeout for a provider request. |
| `LLM_MAX_RETRIES` | `0` | Client retry setting. Budgeted calls can force zero client retries and manage recovery themselves. |
| `LLM_FAST_MODEL_ID` | Main model | Optional model for eligible routine actions. |
| `LLM_EXTRACTION_MODEL_ID` | Main model | Optional extraction/matching model. |
| `LLM_AGENT_REASONING` | Unset | Reasoning override for agent/fast-purpose clients that use this setting. |
| `LLM_EXTRACTION_REASONING` | Unset | Reasoning override for the extraction-purpose client. |
| `LLM_REASONING_SEARCH_QUALIFICATION` | Extraction reasoning, otherwise `low` | Reasoning for batched semantic job qualification. Uses `LLM_EXTRACTION_MODEL_ID`, falling back to the main model. |

The reasoning settings accept `none`, `minimal`, `low`, `medium`, or `high`.
Only set them when the provider supports the corresponding request parameter.
Tool calling and structured output are required by agent workflows; optional
visual backup also needs image input. Compatibility varies between providers.

## Browser and worker settings

The following settings are read from the **process environment**. Putting them
only in `.env` does not export them to these components. Set them in the shell
before running both migrations and the server where applicable.

| Variable | Default | Purpose |
| --- | --- | --- |
| `JOB_AGENT_DATABASE_URL` | `sqlite:///./job_agent.db` | Database used by the API and Alembic. The default path is relative to the working directory. |
| `JOB_AGENT_APPLY_WORKERS` | `3` | Application worker count, clamped to 1–8. |
| `JOB_AGENT_MAX_OPEN_APPLICATIONS` | `6` | Limit on unfinished application attempts; minimum 1. |
| `JOB_AGENT_HIDDEN_BROWSER` | `offscreen` | `offscreen` uses a normal Chrome window outside the visible desktop; `headless` uses headless Chrome. |
| `JOB_AGENT_BROWSER_CHANNEL` | `chrome` | Playwright browser channel. The default uses installed Google Chrome. |
| `JOB_AGENT_ANALYSIS_WARMUP` | `1` | Precompute uploaded-resume analysis; `0`, `false`, or `off` disables it. Warmup can use model tokens. |
| `JOB_AGENT_BROWSER_LOCALE` | `en-US` | Page language for agent browsers. Without it, Chrome follows the OS locale and sites can serve localized text that leaks into job descriptions. |
| `JOB_AGENT_ALLOWED_HOSTS` | Loopback hosts only | Comma-separated additional accepted Host names. This is not user authentication. |

For example, use headless Chrome with two application workers:

```powershell
# Windows PowerShell
$env:JOB_AGENT_HIDDEN_BROWSER = "headless"
$env:JOB_AGENT_APPLY_WORKERS = "2"
python -m uvicorn eregion.api.main:app --host 127.0.0.1 --port 8000
```

```sh
# macOS / Linux
export JOB_AGENT_HIDDEN_BROWSER=headless
export JOB_AGENT_APPLY_WORKERS=2
python -m uvicorn eregion.api.main:app --host 127.0.0.1 --port 8000
```

Lower concurrency if the provider rate-limits calls or the machine cannot support
the active browsers. A worker limit is not a promise that every task runs at once;
site coordination, waiting questions, and review boundaries still apply.

## Optional Jev acceleration

The main model handles conversation, planning and writing. The general Jev switch
also selects the fit-scoring engine; the career helper switch does not control
fit scoring.

```dotenv
TYPESAFE_API_KEY=your-typesafe-key
TYPESAFE_BASE_URL=https://api.typesafe.ai
TYPESAFE_DEFAULT_MODEL=jev-latest
TYPESAFE_ENABLED=false
TYPESAFE_CAREER_ENABLED=false
TYPESAFE_TIMEOUT=2.5
```

`TYPESAFE_ENABLED=true` selects **Jev fit scoring**, including JD requirement
classification, evidence decisions and citation checks. This applies to chat's
`score_job` / `score_jobs`, quick analysis and saved-job analysis. Every answer
from Jev is a probability, and the score is the **expected credit** across
requirements: each requirement weighs P(requirement) × (2 − P(preferred)) ×
(1 − P(personal condition)) and earns P(fully met) + P(not fully met) × P(related) × 0.5.
The result is always one number. Jev is deterministic for identical input, so
repeated scoring returns the same or nearly the same value, and a `certainty`
(0–100%) in the explanation shows how decisive the underlying answers were.
Jev decides which source spans are candidate requirements, which are optional,
and which are context. Local code preserves source offsets and combines model
probabilities; keyword lists and heading names do not override those decisions.
Jev provider failures remain failures, with no silent fallback.
Optionally, `TYPESAFE_FIT_RESOLVE_UNKNOWNS=true` sends requirements Jev was not
decisive about to the main model (one structured request per job, verbatim-quote
verification). It is off by default because it measurably reduced consistency
and separation (see [Jev and performance](jev-benchmark.md)).
Setting `TYPESAFE_ENABLED` to `false` selects the original analyze pipeline.
Resume upload warmup skips main-model extraction while Jev is enabled.

The switch also enables general option mapping and job screening.
`TYPESAFE_CAREER_ENABLED=true` additionally enables eligible memory, question,
and legacy workflow evidence-matching helpers; it also requires the general switch.

Fit-specific settings:

| Variable | Default | Purpose |
| --- | --- | --- |
| `TYPESAFE_FIT_MODEL` | `jev-1.13.0` | Exact Jev release. When unset, an exact `TYPESAFE_DEFAULT_MODEL` is used; its `jev-latest` / `jev-preview` aliases resolve to the tested `jev-1.13.0` for fit scoring. |
| `TYPESAFE_FIT_TIMEOUT` | `12` seconds | Timeout for each fit request, from 0.1 to 60 seconds. No automatic paid retries. |
| `TYPESAFE_FIT_RESOLVE_UNKNOWNS` | `false` | `true` resolves requirements Jev was not decisive about with one main-model request per job. |
| `TYPESAFE_FIT_UNKNOWN_TTL` | `86400` seconds | Cache lifetime for non-decisive Jev answers (60 s – 7 days). |
| `TYPESAFE_FIT_CACHE_PATH` | Database sibling ending in `.jev-fit.sqlite3`; otherwise `.cache/jev-fit.sqlite3` | Persistent local decision cache. Relative overrides resolve from the server's working directory. |

The cache uses exact resume, JD, supplied facts, owner, model release and scoring
code/prompt identities. Answers expire after 24 hours (non-decisive answers per
`TYPESAFE_FIT_UNKNOWN_TTL`). Changes invalidate reuse. Only hashes, answers and provenance
are stored in the decision cache; application artifacts retain the actual sources.
Chat can batch up to eight imported jobs for the same candidate. Compatible
questions share a request while each job retains its own result and citations.
Paid requests and tokens are accounted once, including shared requests.

Fit requests send extracted resume text, the supplied JD and scoped candidate
facts to Typesafe. A combined input above 60,000 UTF-8 bytes is rejected without
truncation. General helper requests can include observed choices, confirmed source answers, job
snippets, and search preferences. Career requests can include extracted resume
evidence and scoped confirmed memories. These may contain personal information.
The Jev adapter does not upload resume files, screenshots, passwords, or codes.

The general helper timeout accepts 0.1–10 seconds. Those helpers can fall back
or skip Jev under their existing cost gate. **Fit scoring uses the separate
timeout above and makes no main-model calls unless `TYPESAFE_FIT_RESOLVE_UNKNOWNS`
is on.**
See [Jev and performance](jev-benchmark.md) for measurements and limitations.

## Data and credentials

The local database stores candidate information, conversations, evidence,
application state, and provider-usage records. Treat it and any exported artifacts
as personal data. Stop the backend before making a filesystem backup of the
database and associated files; keep any SQLite sidecar files together.

Supported Windows login persistence uses DPAPI encryption under
`.browser-sessions/`, scoped to the candidate and job-site host. It does not store
the supplied password as a reusable agent credential. Other operating systems do
not use a plaintext persistent fallback for this store.

Provider requests can contain the relevant resume, job, answer, or document
content. Requested screenshots are an additional data flow when visual backup
is enabled. Local storage does not mean offline inference.

## Updating an installation

Stop the backend, back up local data, and update from the repository root:

```sh
git pull --ff-only
python -m pip install -r requirements.txt
python -m alembic upgrade head
python -m uvicorn eregion.api.main:app --host 127.0.0.1 --port 8000
```

Use the same `JOB_AGENT_DATABASE_URL` for migrations and the server. Refresh the
web page after updating frontend files, and reload the unpacked Chrome extension
after changing its files. Do not delete the database to resolve a migration error.

The default server address is `127.0.0.1:8000`; the extension uses
`http://localhost:8000`. Changing the server port requires corresponding extension
configuration changes. The health endpoint reports service/MCP state, not proof
that a model request or employer application will succeed.
## Per-purpose reasoning and optimization telemetry

`LLM_REASONING_<PURPOSE>` optionally overrides reasoning for an individual purpose.
The suffix is upper snake case. Common values are `AGENT` (career/application
router), `FAST`, `EXTRACTION`, `SCORING`, `PACK_WRITER`, `CLAIM_VERIFIER`,
`INTERVIEW_QUESTION`, `INTERVIEW_FEEDBACK`, `JOB_SCREENING`, `SKILL_EVIDENCE`,
`SKILL_EVIDENCE_VERIFY`, `QUESTION_CATEGORY`, `MEMORY_RELEVANCE`, `FORM_OPTIONS`,
`RESUME_PROFILE`, `SKILL_EVALUATION`, `API`, and `NONE` (unspecified construction).
`JEV_FIT_RESOLVER` independently controls the main-model unknown-fit resolver.
Structured schemas without an alias use their class name converted to upper snake
case, e.g. `InterviewAnswerAssessment` → `INTERVIEW_ANSWER_ASSESSMENT`.
Jev fit additionally supports `EXTRACT_REQUIREMENTS`, `REFINE_EXTRACTION_YES_NO`,
`CONDITION_CATEGORY_YES_NO`, `SUPPORT_YES_NO`, `FOCUSED_YES_NO`,
`CLARIFY_FULL_SUPPORT_YES_NO`, and `VERIFY_CITATIONS_YES_NO`.

Allowed values: `none`, `minimal`, `low`, `medium`, `high`. Blank means unset;
`none` sends the literal value. Precedence is purpose override, then the existing
path's `LLM_AGENT_REASONING` / `LLM_EXTRACTION_REASONING` /
`LLM_INTERVIEW_REASONING`, then provider default. Mock interview retains its default
`low`. Career `TaskModel` historically sends no agent reasoning by default, while
application router does read `LLM_AGENT_REASONING`; this distinction is preserved.
The complete construction/schema matrix is in
`opt-reasoning-audit-20261011.md`.
Overrides do not leak between semantic purposes on a shared client: for example,
`LLM_REASONING_EXTRACTION` does not change scoring or the Jev resolver; without their
own overrides those retain the legacy extraction reasoning setting.

`JOB_AGENT_REASONING_ESCALATION=1` opts into a bounded validation retry/upgrade:
low → medium and medium → high. Absent reasoning stays absent. Default is off.
Existing grounding, batch and controller retries retain their budgets; only the
failed step is upgraded. Providers rejecting reasoning get one retry without the
parameter. TypeSafe compatibility is unverified until a separately approved live
comparison; leave its overrides unset to keep today's requests.

Reasoning configuration and escalation policy partition result caches. Results
produced after a level upgrade or parameter-free fallback do not populate the
original level's cache key (fit, durable analysis, fast decisions, Jev and resolver).
No new
variable is needed for per-attempt composition/timing telemetry. The usage ledger
stores counts, request-part character sizes, request-budget replay eviction and
monotonic timings, never prompt text. `source=tool_action` rows contain action
timings and are excluded from provider counts. Provider persistence timing covers
start/finish writes, excluding the final timing update; tool persistence timing
covers its pre-effect intent checkpoint. Queue time is measured at executor dispatch.

Read-only report:

```powershell
.venv/Scripts/python.exe evals/opt_report.py path/to/career.db --json report.json
```

Optional `--task-id` / `--run-id` filters select attribution. Missing historical
fields are reported as missing/null. A saved last-request snapshot is labelled
separately and never extrapolated to earlier attempts. Ratios with only partial
cached/reasoning counters use known counters; consult the accompanying missing
counts before comparing runs. No paid run is started by this command.
# Stage 0 calibration controls

- `JOB_AGENT_REASONING_REJECTION_RETRY=1` opts into one retry without the reasoning
  parameter when a configured reasoning value is rejected. Default: off. Applies
  to shared OpenAI-compatible, FastDecisions and Jev fit clients. No configured
  value means no compatibility retry. Mock interview retains its existing retry.
  Fallback attempts are recorded separately and cannot populate the original
  reasoning configuration's cache entry.
- `JOB_AGENT_TASK_MAX_PROVIDER_ATTEMPTS` optionally caps all provider attempts
  attributed to a career task, including nested scoring/extraction, failed calls,
  configured SDK transport retries and reasoning retries. Unset preserves the
  existing router budget. A nonnegative integer is required; zero stops before
  the first provider attempt.
- `JOB_AGENT_TASK_MAX_INPUT_TOKENS` optionally caps input use for the same task.
  Before dispatch, text requests reserve serialized UTF-8 bytes plus 1,024 units
  for framing, conservatively rather than treating character counts as tokens.
  Provider-reported input tokens replace the reservation after completion.
  Failed/unknown and in-flight requests retain their reservations; historical
  unknown usage without a reservation, or image/audio requests without a known
  estimate, stop before dispatch. This is conservative admission accounting,
  not an assertion that a remote provider's tokenizer can be predicted exactly.

Both task limits default to unset, use durable ledger rows (including discarded
attempts), survive restart and are not reset by sending `continue`. Raising or
removing a limit allows more work. Reservations are serialized in the SQLite task
store, including across processes. Under a configured limit, missing/unwritable
accounting or unsupported stores stop closed. Saved work remains available and
the task stops as `needs_input` with `provider_budget`; it never claims completion.
`UsageLedger.summary` exposes `provider_attempts`, `input_tokens`, `retries` and
`budget_input_tokens` (reported usage plus retained reservations).

New context metrics contain hashes and counts only: persistent groups dropped
since the preceding request, request-copy groups dropped, first replayed message
and first complete group's hashes, tools-array hash, and system-prompt hash.
Trimming limits and request content are unchanged.

## Verified company board discovery

`search_jobs` accepts `use_directory: true` plus the existing `query`, `location`
and `limit`. It selects fresh entries from `backend/eregion/runtime/career/board_directory.json`
by role family and observed location, then uses the existing board parser and
role/location/seniority checks. Do not combine `use_directory` with explicit
`boards`. Explicit tokens remain available and are labelled unverified. The tool
list and order do not change. Requested initial sources must still be attempted;
use the directory when those sources are blocked. All links still require read,
posting qualification and opening-level evidence, import, score and recommendation.

- `JOB_AGENT_BOARD_DIRECTORY_MAX_AGE_DAYS`: maximum age in UTC dates, default
  `30`; older matching boards are returned in `stale_boards` and excluded.
- `JOB_AGENT_BOARD_MIN_INTERVAL_SECONDS`: minimum API request start interval per
  host per discovery instance, default `0.25` seconds, clamped to zero or higher.
  A host has one active request; at most three API requests run across hosts on a
  shared instance. A directory search visits at most five boards sequentially.
  HTML sources are never parallelised by directory search or the limiter.

The discovery instance owns a reusable HTTPX client and closes it during harness
shutdown. It keeps environment proxies disabled, validates every redirect and
public DNS target, and removes cookie headers through a public HTTPX request hook.
Cancellation is checked before each board, during rate-limit waits and between
response chunks. A blocked synchronous socket read can take up to the existing
20-second HTTP timeout before cancellation is observed; no subsequent board is
started. No SDK internals or challenge bypass is used.

`search_source_health.sources` stores current-turn tenant outcomes with timestamps;
`history` retains the latest outcome, timestamp, observation count and consecutive
errors across turns. A 404, a blocked source, two errors or two empty searches
excludes that board for the current turn. A new turn can retry. A 404 or repeated
errors sets `refresh_required` until a successful/empty API observation clears it.

Refresh (dry-run by default, read-only public GETs, sequential and rate-limited):

```text
python tools/scripts/verify_board_directory.py
python tools/scripts/verify_board_directory.py --write
python tools/scripts/verify_board_directory.py --health exported-source-health.json
```

The optional health file contains only the exported `search_source_health` object;
it is not a conversation export. `--write` updates dates, counts and coverage labels
only on successful validated responses, retaining old dates on failures and
returning a nonzero exit code. At most 200 entries are allowed. Workday remains
supported for explicit search, but its POST listing endpoint cannot be certified
as `public_api_get`; the verifier flags it and never invents GET verification.
`BoardEntry` is the executable Pydantic schema; `load_directory` validates all
entries and rejects duplicate tenants. Synthetic test entries never enter the
production JSON directory.


## Opt-in request optimization (v3 stage 2)

All flags default off; `JOB_AGENT_OPT_HISTORY_POLICY` defaults to `current`.
They alter model request projection only, not scoring inputs, qualification,
source health or catalogue policy. Existing request character limits remain unchanged.

| Environment variable | Values | Behavior |
|---|---|---|
| `JOB_AGENT_OPT_SNAPSHOT_REFS` | `0` / `1` | Archive long body/preview fields and old tool payloads with inline summaries; elide duplicate request text and assistant progress. `read_context` retrieves originals. Current authority, resume summary, identity/health, qualification, scores and accessed original evidence remain visible. Whole replay groups, including the last group, can be omitted under pressure; sources remain archived. |
| `JOB_AGENT_OPT_STABLE_TOOLS` | `0` / `1` | Deterministic career tool definitions per task kind, concise descriptions and presentation-only schema title removal. Snapshot `allowed_tools` is the live execution mask; validation rejects forbidden calls before effects. Browser, visual, screening, progress, completion, recommendation and search-only restrictions remain enforced. A new user request may change task kind. |
| `JOB_AGENT_OPT_PROMPT_TRIM` | `0` / `1` | Use the concise career system prompt; other hosts' prompts are unchanged. |
| `JOB_AGENT_OPT_HISTORY_POLICY` | `current` / `checkpoint` / `aligned` | `current` preserves legacy independent layers. `checkpoint` evicts groups in chunks and commits the request boundary to persistence. `aligned` derives a persistent history budget from the space remaining in the request and commits the same boundary; the budget survives restart. Invalid values fall back to `current`. |

Enable boolean flags with the exact value `1`. Roll back with `0` and `current`.
Disabling a flag restores legacy construction of subsequent requests; it cannot
restore groups already evicted under an enabled history policy. Durable domain
results and their original context references remain authoritative.

Offline measurements, limitations, and proposed defaults are in
`benchmarks/opt-v3-stage2-20261011/README.md`.
Defaults are deliberately unchanged pending review; no token-cost or cache-hit
claim is inferred from character counts and scripted decisions.

## Optional posting workflow

`JOB_AGENT_OPT_VERIFY_AND_SCORE=1` exposes `verify_and_score` and adds one short
prompt preference. Default: `0` (legacy tool arrays and requests unchanged).
It accepts one posting URL, including an observed candidate or a saved retrieved
body indexed by that URL, plus the existing read/import optional fields. It calls
the existing `read_job`, `import_job` (including posting qualification) and
`score_job` handlers. It never recommends or applies; the model separately decides
`recommend_job` using the score and source-bound model qualification evidence.

Task state records step intent, completed steps and failures. Restart reuses saved
bodies, workspace source/snapshot identity and existing analysis caches. Failures
preserve granular error codes and `next` guidance; internal/validation failures
also include `fallback` guidance to use the granular tools. Source restrictions,
execution masks, no-progress protection and provider budgets still apply.

## Optional Treg job sources

Your own Treg account and key. Nothing is hosted, proxied or billed by the project. Loading order: **process environment > project-root `.env` > defaults**. The project root is fixed (independent of the working directory). An explicitly empty process variable means "not configured" and does not fall back to `.env`. A placeholder token counts as missing. Settings are read into a mapping; `os.environ` is not modified. Invalid values block Treg only.

| Setting | Default | Meaning |
|---|---|---|
| `TREG_TOKEN` | empty | Your Treg key. Sent only as `X-Treg-Token` to `https://treg.to/call/...`; never logged, shown or returned by tools |
| `TREG_ORG` | empty | Team slug, only for identity tokens (`X-Treg-Org`) |
| `JOB_AGENT_TREG_ENABLED` | `0` | Allows Treg calls. A configured key alone never spends |
| `JOB_AGENT_JOB_API_FIRST` | `1` | API-first job search over existing company boards; independent of Treg |
| `JOB_AGENT_TREG_SEARCH_PROVIDER` | empty | `anyapi` or `crawl4ai`. Empty refuses paid LinkedIn search. The model cannot change it |
| `JOB_AGENT_TREG_CRAWL4AI_SCRAPE_ENABLED` | `0` | Separately allows `crawl4ai.web.scrape` for page text |
| `JOB_AGENT_TREG_TASK_MAX_COST_USD` | `0.10` | Cumulative per task (persisted; new turns or restarts do not reset it) |
| `JOB_AGENT_TREG_DAILY_MAX_COST_USD` | `0.50` | Per UTC day for this deployment's database (all tasks and workers); does not limit the Treg account elsewhere |
| `JOB_AGENT_TREG_CALL_MAX_COST_USD` | `0.01` | Per-call cap, sent as `X-Treg-Route-Max-Cost` (Treg refuses with 402, nothing charged) and reserved locally |
| `JOB_AGENT_TREG_MAX_ATTEMPTS` | `20` | All gateway attempts per task, including retries and status queries |
| `JOB_AGENT_TREG_MAX_RESULTS` / `MAX_PAGES` | `10` / `2` | Per logical search |
| `JOB_AGENT_TREG_MAX_CONCURRENCY` | `2` | In-process concurrent Treg requests (budgets are enforced in the database) |
| `JOB_AGENT_TREG_LIVE_API_TEST` | `0` | Explicit opt-in for the isolated live API smoke/comparison only |

Spend records (`external_tool_operations`, migration `0037`) are kept separately from model usage and in integer micro-USD:
- The charge is taken from `X-Treg-Cost-Micro`.
- Replays count 0, with the original cost stored separately.
- Timeouts, lost responses and a 2xx without a cost header are `unknown` and keep their reservation.
- Retries reuse the same `Idempotency-Key` (network error, 409, 429, saturated 503) at most once, within a deadline.
- Responses served through Treg's overflow relay (`X-Treg-Served-Via: overflow:*`) are recorded and quarantined.

The contract record is kept with the maintainers’ local test reports.

## Integrated optimization switches

All switches below default to `0`; history defaults to `current`. Set explicit process
variables for experiments. `.env.example` lists names without enabling behavior; never
overwrite an existing `.env` when adding configuration.

| Setting | Effect when enabled | Rollback |
|---|---|---|
| `JOB_AGENT_OPT_BOARD_RESPONSE_CACHE` | Reuse bounded Ashby board responses within the existing TTL | Set 0; subsequent reads use existing network path |
| `JOB_AGENT_OPT_CANDIDATE_BATCHES` | Bound the actionable candidate batch | Set 0; durable candidates remain |
| `JOB_AGENT_JOB_API_FIRST` | Existing company-board API priority; Treg separately controlled | Set 0; retain source evidence |
| `JOB_AGENT_OPT_SCOPED_CONTEXT` | Search router receives versioned long-resume reference and preview | Set 0; rebuild full current observation |
| `JOB_AGENT_OPT_STATE_DELTAS` | Complete checkpoint plus bounded verified request-side deltas | Set 0; discard only disposable projection, retain evidence/events |
| `JOB_AGENT_OPT_HISTORY_POLICY` | `structured_checkpoint` aligns trim boundaries and marks authoritative state; `aligned`/`checkpoint` remain available | Set `current`; no raw-input cap increase |
| `JOB_AGENT_OPT_MATERIAL_PATCHES` | Cover-letter revisions can replace validated individual paragraphs before normal verification | Set 0; full `revise_blocks` path |
| `JOB_AGENT_OPT_SNAPSHOT_REFS`, `JOB_AGENT_OPT_STABLE_TOOLS`, `JOB_AGENT_OPT_PROMPT_TRIM`, `JOB_AGENT_OPT_VERIFY_AND_SCORE` | Existing Stage 2/4 experiments, independently selected | Set each 0 |

There is no new prompt cache key or automatic model compaction. No patch submits an
application or bypasses review. Treg remains separately disabled, requires the user's
own token and explicit provider selection, and retains unknown spend reservations on
failures. Missing credentials, account access or budget prevents a paid operation;
existing free source paths remain available. See the final report
for evidence and pending networked validation.

The R1 adoption decisions retain
these disabled defaults and `current` history. The real-search gate is still
failed; the user-approved gate amendment allows migration while moving business
acceptance to the API-first configuration in R6/R9. No experiment is enabled by
this policy change. A provider
budget stop now includes saved results in its reply, while retaining `needs_input`
and an explicit incomplete status. This does not relax search acceptance or
increase any budget. See the R2 gate.


## Default job sources

Default research uses public company-board APIs and an explicitly configured LinkedIn MCP server (`server_id: linkedin`). No paid search key is required. Without that server, company boards remain usable and LinkedIn is reported unavailable. A LinkedIn-only request never silently searches other boards. Treg is disabled by default; users may separately enable it with their own key. Set JOB_AGENT_JOB_API_FIRST=0 explicitly only to retain the legacy browser route. Existing explicit environment values take precedence over defaults.

Configure the existing MCP manager using an absolute MCP_CONFIG_PATH in the **backend process environment or project .env** (process values, including an explicitly empty value, take precedence). Copy `docs/examples/linkedin-mcp.json` and adjust executable/profile paths. Install pinned mcp-server-linkedin==4.27.0 in an isolated environment. Log in interactively once using its --login mode with the same --user-data-dir; never use a daily browser profile as that directory. Windows automatic session import may fail even when Chrome is logged in. The --chrome-path option can use an installed Chrome binary; it still uses the isolated profile. Normal server startup uses --no-auto-import, so it never imports another account silently.

Only read-only search/detail tools are used by the career source. The existing manager owns startup/shutdown; the same AgentLoop owns decisions. Per-task MCP budget is 26 calls for smaller searches and 50 for requests of at least 10 jobs, each at most 120 seconds, counted before dispatch and persisted across restarts. Authentication/rate-limit/uncertain-timeout stops prevent automatic paid/browser retries for that source. Successful same-turn searches and JD reads are reused. New user requests refresh evidence without resetting the task-wide call budget. Restart a stopped source only after resolving its session issue; starting a new task gives a new task budget.

Search cards retain title and, when the observed layout is unambiguous, company/location. Missing fields stay unknown. Cards and provider filters do not establish eligibility; complete-JD validation and scoring remain required. Directory and MCP discovery currently run sequentially under one execution owner; concurrent source fetching is not enabled.

## Model-owned job qualification and matching

Search qualification interprets the original user request and captured posting text using a structured model response for role, location, seniority and current vacancy. It processes up to four postings per call, within a 48,000-character input bound, under the same execution owner's provider budget and cancellation guard. Missing evidence, invalid source quotations or unavailable model output remain unknown. The harness does not supply a fallback semantic verdict. Unchanged request/body/model signatures reuse the saved decision; a changed request or source invalidates it.

Jev still classifies requirements and scores personal fit when enabled. Headings, benefits and other source spans reach the model; regex labels do not override its requirement/preference decisions. The main-model scoring route also extracts its requirement catalogue using a model; the obsolete `JOB_AGENT_MAIN_CATALOG` flag has no effect. Source identity, quotation validation, deduplication and score arithmetic remain deterministic. Invalid support or an absent model assessment produces unknown rather than a fabricated negative match.

Qualification defaults to low reasoning on the extraction model (or main model when no extraction model is configured). `LLM_EXTRACTION_MODEL_ID`, `LLM_EXTRACTION_REASONING` and the explicit `LLM_REASONING_SEARCH_QUALIFICATION` override control this purpose. Model calls are metered separately from MCP and Treg source calls. The live batch test runs a separate blind semantic audit, capped at six provider attempts and 200,000 input tokens, after the execution budget. This audit receives original posting evidence rather than production labels; it is a second model pass, not human ground truth.

See the [evaluation report](evaluation-report.md) for how search was tested and its known limits.
