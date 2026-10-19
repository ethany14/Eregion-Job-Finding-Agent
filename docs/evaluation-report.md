# Eregion evaluation report

[Home](../README.md) · [Configuration](configuration.md) · [Architecture](agent-loop.md)

A summary of how Eregion was tested from October 9 to October 19, 2026, and what
the tests showed. It condenses the maintainers' dated test reports, which hold
the commit hashes, raw measurements and usage ledgers and are kept locally rather
than published. Failures are reported as they happened, and no earlier result has
been re-scored under a later rule. Related published docs:
[architecture](agent-loop.md), [Jev and performance](jev-benchmark.md) and
[configuration](configuration.md).

## At a glance

| Area | Where it stands |
| :-- | :-- |
| Agent architecture | One autonomous loop for chat, applications and sessions, accepted offline; its structural checks pass in CI. |
| Job sources | Company boards (118 verified) and LinkedIn through a local MCP server by default. Treg and JobSpy optional. In a one-sample comparison JobSpy produced fewer recommendations under the same budget, so it stays optional. |
| Search and recommendations | Tiered results. Latest scenario runs: 0 structural errors across 123 shown jobs. Under a tight budget, unscored jobs are listed as unscored and never placed in a tier. |
| Fit scoring with Jev | Three jobs scored in 2.0 s cold and 0.15 s cached. In live route runs Jev took about 5 s of 150–250 s; the main model took the rest. Accuracy against human labels not yet measured. |
| Applications on real sites | Workday at Adobe and Intel reached the review page with correct answers (53–59 model calls). Nothing was ever submitted. |
| CAPTCHA and one-time codes | The agent stops and hands the page to the person, then resumes only on positive evidence that the form is back. Proven on synthetic pages and the providers' official test widgets; not yet on a real site's challenge. No automatic solving. |
| Materials | Claims are checked against the resume, and the verifier was repaired. Human style review not done. |
| Interview practice | First answers were too slow (p50 110 s). Feedback now runs in the background, and a real-Chrome check passed. |

## How Eregion is tested

- **Synthetic applicants for search.** Four made-up resumes cover different
  roles, levels and places: a junior data analyst in New York, a senior backend
  engineer for remote US, a machine-learning intern in California, and a junior
  UX designer in Austin. No real candidate data is used for search tests.
- **Real sites for applications.** Applications are tested on real employer
  sites and real postings with the owner's own details. The owner signs in; the
  agent never enters credentials, never solves a CAPTCHA, and never submits.
- **Offline suites first.** Every change runs against the Python test suite
  (about 2,860 tests), real-Chrome tests on local pages (about 640 more, opt-in)
  and frozen replays of earlier live runs before any paid or online run.
- **Live runs are capped and approved.** Each live run has fixed limits on model
  attempts, input tokens (counted with the reservation before sending), source
  requests and time, approved in advance. Every model call is recorded in a
  usage ledger; unknown usage is kept as unknown, a refused call is recorded as
  refused, and dollar cost is not reported without billing data.
- **Independent checks.** A separate audit model reviews each shown job and the
  final reply. On 2026-10-14 a person reviewed one full scenario set by hand,
  and that review is the reference for result quality.
- **Fixed acceptance rules.** Pass/fail rules are written before testing and
  versioned. When a rule changes, earlier results keep their recorded verdict. A
  run whose premise turns out wrong is reported as not covering its target, not
  as a pass.

## 1 · Agent architecture

The runtime uses a single `AgentLoop` that every autonomous path shares (career
chat, auto-apply and assistant sessions), migrated in seven stages (M1–M7).
`tests/test_unified_architecture.py` pins the structure by reading the code
itself: only the accounted layer may call a model provider, and no legacy loop
may be imported.

- All stages were accepted offline, and the full suite was green at `10a07ae`.
- The structural checks pass on a clean checkout (CI). On a working copy they can
  fail when unrelated, untracked experimental files are present; those files are
  not part of the repository.
- Not done: deployment.

## 2 · Job sources

| Source | Result |
| :-- | :-- |
| **Company boards** (Greenhouse, Lever, Ashby, Workday) | The directory grew from 4 to 118 boards, each verified through its public API. A matching fix on 2026-10-14 accepts every way boards write a remote-US location; before it, 22 of 23 boards returned nothing for a remote request. |
| **LinkedIn MCP** (local, user-signed-in) | Default source since 2026-10-12. Calls are budgeted, and the source stops on sign-in or rate-limit problems. |
| **Treg** (optional, paid by the user) | 15 API calls cost $0.004, with zero unknown charges. Spend is capped per call, per task and per day. Off by default. |
| **JobSpy** (optional, out of process) | Runs `python-jobspy` in a separate interpreter with a request cap per task. Off by default. Comparison below. |
| **Indeed MCP** | Sign-in worked, but the server returns 403 “Client not allowed”: only allow-listed clients are accepted. Not used. |

### JobSpy compared with the default sources (October 18)

Two mutually exclusive routes on the same two queries (backend Python, junior
data engineer; New York; entry level; last 7 days): **A** company boards plus
LinkedIn MCP, **B** JobSpy Indeed plus JobSpy LinkedIn. No route could fall back
on the other, Treg or browser search.

**Layer 1, collection only (zero model calls):**

| Per query | Valid cards of 20 | Full descriptions | Requests | First card |
| :-- | --: | --: | --: | --: |
| A · company boards | 20 | 19 | 59 | about 10 s |
| A · LinkedIn MCP | 20 | 19–20 | 21 | 15–22 s |
| B · JobSpy Indeed | 18 (2 lacked a company) | 20 | 1 | 2–4 s |
| B · JobSpy LinkedIn | 20 | 20 | 22 | about 19 s |

- Filters: boards applied neither filter, LinkedIn MCP the level only, JobSpy
  the date only; filters a source could not apply were reported as such.
- The two routes shared no posting. Only JobSpy reported posting dates; JobSpy
  Indeed gave a direct apply link for every card.
- Two board descriptions were missed because the test's host allowlist omitted a
  company board page; the run was kept as recorded rather than repeated.

**Layer 2, scoring the frozen results** (16 jobs per route, sampled the same way
across each route's two sources, each route with its own new database, cold
caches and ledger, the same code version, and 40 attempts / 800k input tokens):

| | A · boards + LinkedIn MCP | B · JobSpy Indeed + LinkedIn |
| :-- | --: | --: |
| Model attempts | 38 | 39 |
| Input / output tokens | 608,667 / 58,138 | 571,422 / 56,484 |
| Jobs scored | 10 of 16 | 8 of 16 |
| Recommendations | 6 | 2 |
| First recommendation / total time | 108 s / 151 s | 157 s / 249 s |

Recommendations by source: LinkedIn MCP 4, boards 2, JobSpy Indeed 2, JobSpy
LinkedIn 0. Because completion differed under the same budget, this is the
**output of each route under that budget**, not evidence that one source's jobs
are better. A cost breakdown found the difference in main-model time and turns:
Jev scoring took about 5 s in both routes, while route B spent two extra model
turns before scoring and its slowest model turns took 35–68 s. One sample per
route. Decision: boards and LinkedIn MCP stay the default; JobSpy stays optional.

An earlier run of the same comparison scored nothing in route B: one large
scoring batch outgrew its estimate and saved no result when the budget stopped
it. That led to the scoring changes in §3.6.

## 3 · Job search and recommendations

This area had the most testing. The target changed once along the way.

### 3.1 Strict target: 10–15 fully qualified jobs (October 12–13)

The first target was 10 to 15 jobs, each confirmed on role, location, seniority
and every personal requirement. All 16 full live runs failed it; the best
delivered 11–12 confirmed jobs. The cause was supply, not code: with strict
evidence only about 1 in 7 New York LinkedIn postings qualified, and the fixed
call limits could not read enough of them. Usage across the 16 runs: 889 provider
calls and 10.5M input / 1.4M output tokens (three calls had unknown usage, so
these totals are a lower bound).

### 3.2 Changed target: tiered recommendations (October 13 onward)

The owner changed the goal: show relevant jobs even when not every requirement is
confirmed, and say plainly what is missing.

| Tier | Meaning |
| :-- | :-- |
| **Priority match** | Role, location and level match, and the resume shows the core requirements. |
| **Worth trying** | Relevant, but a core skill or experience is clearly missing. Each gap is listed. |
| **Needs your confirmation** | A personal fact (GPA, work authorization, start date and so on) or the job's exact level or location still needs confirming. |

The model chooses the tier; the code checks structure only. Each job must have
quoted evidence, a stretch job must list its gaps, and an unconfirmed level or
location can never be a priority match. There is no minimum count.

### 3.3 Human review (live-set-01)

A person reviewed all 32 items of the first four-scenario set: 0 of 23 shown jobs
were wrong; 0 of the audit model's 8 “substantive error” flags were upheld (audit
judgments became advice only); tiers agreed on 17 of 23, leaning conservative;
and the filter was too strict on near misses.

### 3.4 Consistency of the job check

A borderline verdict now gets a second opinion and the more inclusive verdict is
kept. On the 32 labelled postings, verdicts that flipped between repeats fell
from 9 to 3, and agreement with the human labels rose from 89% to 95%.

### 3.5 Scenario runs after the changes

| Set | Code change tested | Passed | Shown | Structural errors |
| :-- | :-- | :-- | --: | --: |
| live-set-02 | Near misses, second opinion | 3 / 4 | 25 | 0 |
| live-set-03 | Show by default, looser tiers | 4 / 4 | 32 | 0 |
| live-set-04 | Posting conditions checked | 4 / 4 | 36 | 0 |
| live-set-05 | Remote-location fix | 3 / 4 | 50 | 0 |
| live-set-06 | Audit cap grows with jobs shown | 1 / 1 | 5 | 0 |

Among the labelled postings, 28 of 29 are now shown or hidden as the reviewer
wanted; about two in three tiers match, most misses one tier apart; every audited
reply was honest about failures and about what remained.

### 3.6 Budgets, scoring and the final reply (October 18–19)

Replaying saved conversations with a real model exposed problems that the
scenario runs had hidden:

- A tool's schema required a field its code treated as optional, so the model
  received the same argument error five times.
- A refusal to recommend an unread posting did not name the tool that reads it,
  and the model re-read its archive instead.
- Looking up saved jobs matched a whole phrase against title and company only,
  so "software engineer New York" found nothing.
- One scoring batch that outran its budget saved nothing (route B above).
- A final reply listed unscored jobs under "Priority match".

Changes, each with tests:

- Scoring under a budget now runs in saved chunks: a small first chunk, then
  chunks sized from the conversation's measured cost per job. A budget stop keeps
  every finished score and returns the rest as unscored, with the reason.
- Scores saved by an earlier conversation are reused only when the posting,
  resume, profile facts and scorer are unchanged.
- Every final reply is checked against saved state: a job under a tier label
  must be a saved recommendation with that tier.
- Scoring results returned to the model are compact (score, gaps, evidence and
  status per job; the full report on request), so no job's status hides behind
  a truncated preview. Recomputed on route A's ten scored jobs, the result
  shrinks from 398k to 40k characters.

In the second route comparison both routes saved their scores chunk by chunk, no
job was scored twice, and neither final reply placed an unsaved job under a tier.
No chunk was interrupted in that run, so recovery from a stop inside a chunk is
covered by offline tests only.

### 3.7 Known limits (accepted)

- **The model often stops early.** In a dedicated measurement it stopped after a
  single query in 20 of 20 runs. The same backend scenario has produced 20 jobs
  in one run and 5 in another. The reply always offers to continue.
- **Verdicts still vary** a little between calls.
- **Some senior roles land one tier low.**
- **The audit model is not a reliable gate** and is used for advice only.

## 4 · Fit scoring and Jev

| Recorded run (synthetic resumes) | Requests | Time |
| :-- | --: | --: |
| Main model only, cold (2026-10-09) | — | 32–49 s |
| Three jobs scored separately with Jev, cold | 15 | 4.6 s |
| Three jobs in one Jev batch, cold | 7 | **2.0 s** |
| Same batch again from the local cache | 0 | **0.15 s** |

- The batch used 47% fewer tokens than separate requests; the batch limit was
  later raised from 8 to 16 jobs.
- In the October 14 live searches Jev scored 150 real postings at 0.39 s each on
  average, with 0 failed requests of 317.
- A Jev batch interrupted by a budget stop pays only for the decisions it had
  not made yet when retried (tested offline).
- **Not yet measured:** scoring accuracy. A labelled set (6 applicants × 12 real
  postings) is built, but its 96 human reviews are pending, so no accuracy
  figure is claimed.

## 5 · Applications on real sites

All runs used real postings and the owner's details; the owner signed in where a
site required it; nothing was submitted.

| Run | Site | Driver | Result | Model attempts |
| :-- | :-- | :-- | :-- | --: |
| Pilots (Oct 15) | Figma, Palantir, Notion, Ampersand, Veeva | Playwright | Reached review | — |
| Adobe, early (Oct 15–16) | Workday | Playwright | Stalled on "My Experience" and was stopped | up to 523 |
| Adobe-29 (Oct 17) | Workday | Playwright | **Reached review** | 59 |
| Intel-34 (Oct 17) | Workday, second tenant | Playwright | **Reached review**, after fixes for sign-in, repeated questions and split dates | 53 (806k input) |
| Adobe-35 (Oct 18) | Workday | Playwright | **Reached review**; every value matched the owner's facts; one resume card | 58 (828k input) |
| Adobe-36 (Oct 18) | Workday | Patchright | Stopped when "My Experience" used its 30-call page budget; no driver fault found | 45 |

Fixes made along the way, each tested in real Chrome on local pages:

- Workday sign-in through the secure account form; the password never reaches
  the chat or the model.
- Month/day/year fields handled as one answer.
- A resume uploaded once and verified by its card on the page; an unknown or
  older file already shown is left for the owner to decide (checked against the
  card markup saved from Intel).
- Self-identification questions asked rather than guessed.
- Independent field actions accepted in one turn, with real conflicts refused
  and explained.
- Page buttons found again when earlier writes renumber them.
- A second conversation about the same application gets a read-only view and
  is told where the application runs; it cannot start a duplicate.

Patchright is an opt-in driver with one real-site sample; the default stays
Playwright.

## 6 · CAPTCHA, one-time codes and access checks

Before the changes, synthetic local pages reproduced the problems a code review
had predicted:

- The agent resumed whenever no challenge was detected, including on blank pages,
  error pages and pages mid-redirect, and announced the verification as complete.
- Split one-time-code boxes and code fields named only by their label were
  scanned as ordinary answers.
- An access-check interstitial was not detected at all.

The changes:

- **Structured detection:** one-time code, email verification, CAPTCHA, access
  check or unknown, each active, passive, unknown or clear. A detection error is
  unknown, never clear. Code inputs are never scanned as answers and are masked
  in previews.
- **Hand-off:** while the person handles the page, nothing acts (no fill, Next,
  upload, model call or old batch). Pause, cancel and close always win.
- **Positive resume:** the agent continues only after two checks in a row show no
  active or unknown verification, a loaded page that is not sign-in, blank or an
  error, and the same application. The person's own edits are kept, and budgets
  and saved decisions carry over.
- **Provider widgets:** with the official test keys of Cloudflare Turnstile,
  hCaptcha and reCAPTCHA, Turnstile turned out to be invisible to page selectors
  (it renders in a closed shadow root). Widgets are now judged by whether they
  finished. A finished widget does not block the form, but it is not taken as the
  site's acceptance; a rejected or expired one blocks again; one that never
  finishes is handed back after a bounded wait as "not completed or did not load",
  never as passed.

Results: the synthetic and official-widget tests pass under Playwright and
Patchright, with zero submissions in every scenario. Not yet done: a real site's
challenge with the person present. Automatic solving is not implemented, has no
reliability or side-effect evidence, and is not part of the current delivery.

## 7 · Task memory

Optional recovery summaries for long application conversations were built and
piloted in replays of saved conversations with a real model.

- The pilots showed no measurable benefit, so the feature stays **off by
  default**.
- One replay found that a new conversation, told the application belonged to
  another chat, still offered to start a duplicate. Ownership refusals are now
  structured, and the new conversation gets a read-only view instead.

## 8 · Materials and interviews

- **Materials:** generated resumes and cover letters are checked claim by claim
  against the source resume.
  - A verifier bug, where an employer requirement was read as a candidate claim,
    was fixed.
  - Human style review has not been done.
- **Interview practice:** the first measurement had a p50 of 110 s before
  feedback, against a 30 s target. Feedback now runs in the background in order,
  and a real-Chrome run passed in 15.3 s with no JavaScript errors.

## 9 · Test suite state (October 19)

| Suite | Result |
| :-- | :-- |
| Python suite, clean checkout | All tests pass after the fixes below |
| Real Chrome, all opt-in browser files (three CI shards) | All pass after the fixes below |
| Real Chrome, Playwright (application and verification suites) | 643 passed |
| Real Chrome, Patchright (same suites) | 636 passed, **7 failed** |
| Official CAPTCHA test widgets | 8 of 8 under each driver |

Corrections from this round:

- Two job-alert tests had failed since October 17. A change that day made the
  system prompt slightly longer, so a scripted test conversation hit the request
  size budget. Under that budget the observation keeps only the last rows of its
  history, and the scripted model, which read its progress from those rows, went
  in circles. The production behaviour is intended; the test's script now reads
  progress from the furthest step it has reached, which survives the trimmed history.
- Three route-comparison tests read a local, untracked profile file and failed
  in CI. Offline builds now use a synthetic profile; a paid run still requires
  the saved one.
- Two browser tests expected the word "verification" in the one-time-code
  message, which the hand-off rework had dropped. The message says "one-time
  verification code" again.
- The real-Chrome CI job had timed out at 60 minutes on every run since
  October 15. It now runs in three shards of about 5–8 minutes each.

The seven Patchright failures also occur on the code before these changes:

- six startup tests simulate a missing Playwright package, which the Patchright
  driver does not load;
- one sign-in form test fails only after earlier test files have run.

They are listed separately, so the application suites are not described as
passing on both drivers.

## 10 · Open items

1. A real site's verification challenge, with the person present: the agent
   must stop when the challenge appears, resume safely afterwards, and submit
   nothing.
2. Human review of the fit-label set, which would give the first accuracy
   figure for scoring.
3. Human style review of generated materials.
4. The seven Patchright-only test failures.
5. Long Workday pages that use their whole page budget.
6. The early-stop habit in search, accepted as a known limit.
