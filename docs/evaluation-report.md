# Eregion evaluation report

[Home](../README.md) · [Configuration](configuration.md) · [Architecture](agent-loop.md)

A summary of how Eregion was tested from October 9 to October 19, 2026, and what
the tests showed. It condenses the maintainers' dated test reports, which hold
the commit hashes, raw measurements and usage ledgers and are kept locally rather
than published. Failures are reported as they happened, and no earlier result has
been re-scored under a later rule. Each section gives the measured numbers and
the effect of each change; where an effect was not measured, the section says
so. Related published docs: [architecture](agent-loop.md),
[Jev and performance](jev-benchmark.md) and [configuration](configuration.md).

Conventions: "attempts" are model provider attempts from the usage ledger;
input tokens include cached input; model dollar cost is never reported because
no bill was available; "one sample" means the number is a single run, not an average.

## At a glance

| Area | Where it stands | Key numbers |
| :-- | :-- | :-- |
| Agent architecture | One autonomous loop for chat, applications and sessions, accepted offline; structural checks pass in CI. | 7 migration stages; full suite 2,242 passed / 0 failed at acceptance. |
| Job sources | Company boards and LinkedIn MCP by default; JobSpy optional. Treg was removed on October 19. | 118 verified boards (from 4); remote-US matching fixed on 22 of 23 boards that returned nothing. |
| Search and recommendations | Tiered results with a saved-state check on every final reply. | 0 structural errors across 148 shown jobs; 0 of 23 shown jobs wrong in human review. |
| Fit scoring with Jev | Fast and cached; accuracy not yet measured. | 3 jobs in 2.0 s cold, 0.15 s cached; 150 live postings at 0.39 s each, 0 failed of 317 requests. |
| Applications on real sites | Workday at Adobe and Intel reached the review page; nothing submitted. | 53–59 attempts per pass (from 523 cumulative in the stalled first attempt); 0 submissions in every run. |
| CAPTCHA and one-time codes | The agent hands the page to the person and resumes only on positive evidence. Not yet tried on a real site's challenge. | 6 of 11 synthetic pages were wrongly resumed before; 0 after. Official test widgets 8 of 8 under each driver. |
| Materials | Claims checked against the resume; verifier repaired. | Real re-check: 2 attempts, `awaiting_review`. Human style review not done. |
| Interview practice | Feedback moved to the background; the 30 s target is **not yet demonstrated** with a real model. | First samples 43 / 110 / 164 s (p50 110 s); UI check passed in real Chrome in 15.3 s. |
| Chat interface | Thinking indicator and progressive replies added on October 19. | Replies written out over 0.5–3 s; all motion off under reduced motion; 27 of 27 JS tests pass. |
| Tests and CI | CI green again after 44 consecutive red or cancelled runs. | 3,125 Python tests, 366 of them real-Chrome; Chrome job split from one 60-minute timeout into three 7–12 minute shards. |

## How Eregion is tested

- **Synthetic applicants for search.** Four made-up resumes cover different
  roles, levels and places: a junior data analyst in New York, a senior backend
  engineer for remote US, a machine-learning intern in California, and a junior
  UX designer in Austin. No real candidate data is used for search tests.
- **Real sites for applications.** Applications are tested on real employer
  sites and real postings with the owner's own details. The owner signs in; the
  agent never enters credentials, never solves a CAPTCHA, and never submits.
- **Offline suites first.** Every change runs against the Python suite (3,125
  tests in 239 files), the opt-in real-Chrome tests on local pages (366 of those
  tests, in 35 files, skipped unless `JOB_AGENT_BROWSER_TESTS=1`), 27 JavaScript
  tests, and frozen replays of earlier live runs before any paid or online run.
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

| Stage | What changed | Full suite at acceptance |
| :-- | :-- | :-- |
| M1 | Host contract; invalid or truncated tool calls never run | 2,176 passed, 0 failed |
| M2 | Sessions on the shared loop; 58 of 58 parity decisions inside the loop | 2,301 passed, 0 failed |
| M3 | Deterministic analysis workflow; verifier false positive fixed | 2,339 passed, 0 failed |
| M4–M5 | One background execution service; shutdown drains before resources | one regression found and fixed |
| M6 | In-flight state mapped one-to-one onto the new loop | coverage dry-run passed |
| M7 | Legacy decision paths removed; structure test added | **2,242 passed, 134 skipped, 0 failed** (644 s) |

- The count fell from M3 to M7 because M7 removed 116 duplicated "legacy"
  variants of the session tests, partly offset by new tests.
- One acceptance run hung at 20 %: a self-deadlock in the interview queue. It
  was fixed before the final run, and the hung log is kept.
- Impact: one place now owns budgets, cancellation and accounting for every
  autonomous path, so a fix to the loop applies to chat, applications and
  sessions at once.
- The structural checks pass on a clean checkout and in CI. On a working copy
  they can fail when unrelated, untracked experimental files are present.
- Not done: deployment.

## 2 · Job sources

| Source | Result |
| :-- | :-- |
| **Company boards** (Greenhouse, Lever, Ashby, Workday) | The directory grew from 4 to 118 boards, each verified through its public API. Before the 2026-10-14 matching fix, 22 of 23 boards returned nothing for a remote-US request; the fix accepts every way boards write a remote-US location. |
| **LinkedIn MCP** (local, user-signed-in) | Default source since 2026-10-12. Calls are budgeted, and the source stops on sign-in or rate-limit problems. |
| **Treg** (removed October 19) | Was an optional paid source: 15 API calls cost $0.004 as reported by Treg, with zero unknown charges. Removed as unnecessary; company boards and LinkedIn MCP cover the default route. |
| **JobSpy** (optional, out of process) | Runs `python-jobspy` 1.2.0 in a separate interpreter with a request cap per task (default 80). Off by default. Comparison below. |
| **Indeed MCP** | Sign-in worked, but the server returns 403 “Client not allowed”: only allow-listed clients are accepted. Not used. |

### JobSpy probe (October 17, zero model calls)

| Site and query | Requests | Time | Rows | Full descriptions | Direct apply links |
| :-- | --: | --: | --: | --: | --: |
| Indeed · backend, New York | 1 | 2.2 s | 20 | 20 | 20 |
| LinkedIn · backend, New York | 23 | 26.3 s | 20 | 19 | 0 |
| Indeed · ML, San Francisco | 1 | 2.0 s | 20 | 20 | 20 |
| LinkedIn · ML, San Francisco | 22 | 18.8 s | 20 | 13 | 0 |

When JobSpy was available but not requested, the model did not choose it: in
two paid requests it ran one default search each and never passed JobSpy as a
source (0 of 2). An earlier paid comparison with JobSpy merely available was
therefore inconclusive: both arms spent their 40-attempt cap inside one scoring
batch, with 0 recommendations each and 0 JobSpy requests.

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

- Totals: route A took 160.8 s; route B 45.9 s with 46 requests.
- Filters: boards applied neither filter, LinkedIn MCP the level only, JobSpy
  the date only; filters a source could not apply were reported as such.
- Posting dates were known for 100 % of JobSpy cards and 0 % of route A's.
  Direct apply links: boards 6 and 4, JobSpy Indeed 20 and 20, LinkedIn 0.
- The two routes shared no posting.
- Two board descriptions were missed because the test's host allowlist omitted a
  company board page; the run was kept as recorded rather than repeated.

**Layer 2, scoring the frozen results** (16 jobs per route, sampled the same way
across each route's two sources, each route with its own new database, cold
caches and ledger, the same code version, and a cap of 40 attempts / 800k input
tokens):

| | A · boards + LinkedIn MCP | B · JobSpy Indeed + LinkedIn |
| :-- | --: | --: |
| Model attempts (cap 40) | 38 | 39 |
| Input / output tokens | 608,667 / 58,138 | 571,422 / 56,484 |
| Unknown usage / retries / refused | 0 / 0 / 0 | 0 / 0 / 0 |
| Jobs scored | 10 of 16 | 8 of 16 |
| Scoring chunks (Jev requests) | 4 jobs (13) + 6 jobs (16) | 4 jobs (15) + 4 jobs (14) |
| Recommendations | 6 (1 priority, 3 stretch, 2 to confirm) | 2 (1 stretch, 1 to confirm) |
| First recommendation / total time | 108 s / 151 s | 157 s / 249 s |
| Final reply against saved state | 0 stray, 0 misplaced | 0 stray, 0 misplaced |

By source (sampled / scored / recommended): boards 9 / 6 / 2, LinkedIn MCP
7 / 4 / 4, JobSpy Indeed 8 / 4 / 2, JobSpy LinkedIn 8 / 4 / 0.

Because completion differed under the same budget, this is the **output of each
route under that budget**, not evidence that one source's jobs are better. A
cost breakdown located the difference:

| | A | B |
| :-- | --: | --: |
| Main model busy (turns) | 111.7 s (6) | 209.5 s (7) |
| Qualification busy | 27.0 s | 29.9 s |
| Jev scoring busy | 5.4 s | 5.1 s |
| Turns before the first scoring request | 1 | 3 |
| Mean extracted requirements per scored job | 14.3 | 22.5 |

Route B's three slowest model turns took 68, 56 and 35 s against A's 33, 18 and
23 s for similar output, so most of the time gap is provider generation speed
during the run. Route B scored fewer jobs because it spent two extra turns
before scoring and its first chunk cost 15 rather than 13 Jev requests. One
sample per route. Decision: boards and LinkedIn MCP stay the default; JobSpy
stays optional.

An earlier run of the same comparison scored nothing in route B (0 of 16, 0
recommendations, 39 attempts): one scoring batch outgrew its estimate and saved
no result when the budget stopped it. That led to the scoring changes in §3.6;
on the rerun route B scored 8 jobs.

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

A person reviewed all 32 items of the first four-scenario set:

| Check | Result |
| :-- | :-- |
| Shown jobs that were wrong | 0 of 23 |
| Audit model's "substantive error" flags upheld | 0 of 8 (audit judgments became advice only) |
| Tier agreement | 17 of 23 (74 %), leaning conservative |
| Filter | too strict on near misses |

### 3.4 Consistency of the job check

A borderline verdict now gets a second opinion and the more inclusive verdict is
kept. On the 32 labelled postings, verdicts that flipped between repeats fell
from 9 to 3, and agreement with the human labels rose from 89 % to 95 %.

### 3.5 Scenario runs after the changes

| Set | Code change tested | Passed | Shown | Structural errors |
| :-- | :-- | :-- | --: | --: |
| live-set-02 | Near misses, second opinion | 3 / 4 | 25 | 0 |
| live-set-03 | Show by default, looser tiers | 4 / 4 | 32 | 0 |
| live-set-04 | Posting conditions checked | 4 / 4 | 36 | 0 |
| live-set-05 | Remote-location fix | 3 / 4 | 50 | 0 |
| live-set-06 | Audit cap grows with jobs shown | 1 / 1 | 5 | 0 |

Impact: 148 jobs were shown across sets 02–06 (123 in the latest four sets),
with 0 structural errors; jobs shown per four-scenario set rose from 25 to 50
between sets 02 and 05. Among the labelled
postings, 28 of 29 are now shown or hidden as the reviewer wanted; about two in
three tiers match, most misses one tier apart; every audited reply was honest
about failures and about what remained.

### 3.6 Budgets, scoring and the final reply (October 18–19)

A paid replay on October 18 (21 attempts, 529,085 input tokens) and the first
route comparison exposed problems that the scenario runs had hidden:

| Problem | Measured cost before the fix | After the fix |
| :-- | :-- | :-- |
| A tool's schema required a field its code treated as optional | 5 identical argument errors in one conversation | schema matches the code; tested through a real loop turn |
| A refusal to recommend an unread posting named no next step | 16 re-reads of archived views, then the budget ran out | the refusal names `read_job` and the posting's URL |
| Saved-job lookup matched one whole phrase | "software engineer New York" found 0 jobs | every word must appear in title, company or location |
| One scoring batch saved nothing when stopped | route B: 0 of 16 scored | saved chunks; rerun scored 8 of 16 |
| A final reply listed unscored jobs under "Priority match" | 5 unscored jobs shown as priority | refused: the check flags all 11 unsaved jobs in that reply |

Changes, each with tests:

- Scoring under a budget now runs in saved chunks: a first chunk of 4 jobs,
  then chunks sized from the conversation's measured cost per job. A budget stop
  keeps every finished score and returns the rest as unscored, with the reason.
  Offline: 7 jobs in chunks of 4 + 3 used 37 calls with 0 refused, and a retry
  after an interrupted chunk scored each job exactly once.
- Scores saved by an earlier conversation are reused only when the posting,
  resume, profile facts and scorer are unchanged. Offline: 4 saved scores
  recovered in a new conversation with 0 model calls; after a resume change,
  0 of 3 were reused.
- Every final reply is checked against saved state: a job under a tier label
  must be a saved recommendation with that tier.
- Scoring results returned to the model are compact (score, gaps, evidence and
  status per job; the full report on request), so no job's status hides behind
  a truncated preview. On route A's ten scored jobs the result shrinks from
  397,906 to 40,323 characters (−90 %).

Measured effect in the second route comparison: both routes saved their scores
chunk by chunk, each scored job had exactly one match report (A 10, B 8), no job
was scored twice, and both final replies had 0 stray and 0 misplaced jobs with 0
refused completions. No chunk was interrupted in that run, so recovery from a
stop inside a chunk is covered by offline tests only.

### 3.7 Known limits (accepted)

- **The model often stops early.** In a dedicated measurement it stopped after a
  single query in 20 of 20 runs. The same backend scenario has produced 20 jobs
  in one run and 5 in another. The reply always offers to continue.
- **Verdicts still vary** a little between calls (3 of 32 labelled postings
  flipped between repeats).
- **Some senior roles land one tier low.**
- **The audit model is not a reliable gate** (0 of 8 flags upheld) and is used
  for advice only.

## 4 · Fit scoring and Jev

| Recorded run (synthetic resumes) | Requests | Time |
| :-- | --: | --: |
| Main model only, cold (2026-10-09) | — | 32–49 s |
| Three jobs scored separately with Jev, cold | 15 | 4.6 s |
| Three jobs in one Jev batch, cold | 7 | **2.0 s** |
| Same batch again from the local cache | 0 | **0.15 s** |

- Impact: scoring three jobs fell from 32–49 s on the main model to 2.0 s cold
  (16–25× faster) and 0.15 s cached.
- The batch used 47 % fewer tokens than separate requests; the batch limit was
  later raised from 8 to 16 jobs.
- In the October 14 live searches Jev scored 150 real postings at 0.39 s each on
  average, with 0 failed requests of 317.
- In the October 18 route comparison Jev took 5.1–5.4 s of 151–249 s per route
  (2–4 %); the main model took 74–84 %.
- A Jev batch interrupted by a budget stop pays only for the decisions it had
  not made yet when retried (tested offline).
- **Not yet measured:** scoring accuracy. A labelled set (6 applicants × 12 real
  postings, 72 pairs and 288 prediction rows) is built, but its 96 human reviews
  are pending, so no accuracy figure is claimed.

## 5 · Applications on real sites

All runs used real postings and the owner's details; the owner signed in where a
site required it; nothing was submitted. Caps for the Workday runs were 60
attempts, 1M input tokens, 1,200 active seconds and 30 attempts per page, except
Intel-34 (90 / 1.5M, an owner decision).

| Run | Site | Driver | Result | Attempts | Input tokens | Active time |
| :-- | :-- | :-- | :-- | --: | --: | --: |
| Pilots (Oct 15) | Figma, Palantir, Notion, Ampersand, Veeva | Playwright | Reached review | — | — | — |
| Adobe, early (Oct 15–16) | Workday | Playwright | Stalled on "My Experience" and was stopped | 523 cumulative | — | — |
| Adobe-09 (Oct 16) | Workday | Playwright | Stopped at the 30-attempt page cap on "My Experience" | 44 | 712,305 | about 525 s |
| Adobe-29 (Oct 17) | Workday | Playwright | **Reached review** | 59 | 866,791 | about 931 s |
| Intel-34 (Oct 17) | Workday, second tenant | Playwright | **Reached review**, with 3 data issues (below) | 53 | 806,047 | about 605 s |
| Adobe-35 (Oct 18) | Workday | Playwright | **Reached review**; every value matched the owner's facts | 58 | 827,618 | about 833 s |
| Adobe-36 (Oct 18) | Workday | Patchright | Stopped when "My Experience" used its 30-attempt page budget | 45 | 708,575 | about 665 s |

The early Adobe total (523 attempts) spans several code versions, resumes and
restarts, so it is evidence of the failure, not a performance baseline. In it,
the host autofill ran 272 times and failed 164 times, mostly repeating the same
failure.

Fixes and their measured effect:

| Problem | Effect before | Change | Effect after |
| :-- | :-- | :-- | :-- |
| Autofill repeated unchanged failures | 164 of 272 autofill runs failed | a failure is retried only when the target, value or options change | covered by a browser test that fails on the old code |
| Each request carried the whole saved state | about 95k characters against a 64k budget | only the current page's decisions and bindings | the same saved state: 50,452 → 28,865 characters (−43 %) |
| Re-rendered Workday fields got new ids | counts and bindings lost (education-10 → education-148) | stable keys without instance numbers | reproduced in a fixture that fails on the old code |
| Dates typed with select-all-and-replace | "08/2008", "12/2012" | focus, type and read back on all three write paths | parametrized fixture; all three paths pass |
| Resume upload appended each time | Intel-34 review listed the resume 3 times | durable upload record with a content fingerprint | Adobe-35: cards 0 → 1, one card on review |
| City box next to a State control | Intel-34 showed "Ithaca, NY, NY" | the model types the verbatim city part | not yet re-run on Intel |
| Self-identification left empty without asking | Intel-34 left one question empty | leaving it empty needs a cited answer from the person | Adobe-35 asked before choosing; the owner confirmed |
| A saved optional omission still looked unresolved | Adobe-09 revisited it until the page cap | a valid saved omission counts as complete | Adobe-29 and Adobe-35 then reached review |
| The off-screen browser window kept the keyboard focus on Windows | the person's typing could reach the field the agent had focused; a local test read "2i026" in 1 of 3 runs | focus handed back after each window opens, on pop-ups and on hand-offs (`31b9e8f`) | see §10 |

Other fixes, each tested in real Chrome on local pages: Workday sign-in through
the secure account form, with the password never reaching the chat or the
model; month/day/year fields handled as one answer; independent field actions
accepted in one turn, with real conflicts refused and explained; page buttons
found again when earlier writes renumber them; and a second conversation about
the same application gets a read-only view instead of a duplicate start.

Review-page accuracy in the two October 17 passes: every personal, work and
education value matched the owner's facts or stated answers, 0 defaults were
used, and every field left empty was optional with its reason logged (13 at
Adobe, 6 at Intel). Reaching review is not the same as fully correct data, which
is why Intel-34's three issues are listed above.

Patchright is an opt-in driver. On 31 local real-Chrome test files it passed
312 tests against Playwright's 313; the one difference (back navigation) was
then fixed. Its one real-site sample (Adobe-36) stopped on the page
budget without a driver fault. Adobe-36 was at the end of the page when it hit
the cap; Adobe-35 needed 27 attempts on the same page. The default stays
Playwright.

## 6 · CAPTCHA, one-time codes and access checks

Before the changes, 11 synthetic local pages reproduced the problems a code
review had predicted:

| Problem on the old code | Pages affected (of 11) |
| :-- | --: |
| Challenge not detected (access-check interstitial, split code boxes, label-named code field, code field in a frame) | 4 |
| Agent would resume and announce the verification as complete (the 4 above plus a blank page and an error page) | 6 |
| One-time-code boxes scanned as ordinary answers (6 split digit boxes; one label-named field) | 2 |

The changes:

- **Structured detection:** one-time code, email verification, CAPTCHA, access
  check or unknown, each active, passive, unknown or clear. A detection error is
  unknown, never clear. Code inputs are never scanned as answers and are masked
  in previews. Labels are recognised in English, Chinese, Spanish, German and
  French.
- **Hand-off:** while the person handles the page, nothing acts (no fill, Next,
  upload, model call or old batch), and waiting makes no model call. Pause,
  cancel and close always win.
- **Positive resume:** the agent continues only after two checks in a row, at
  least 3 s apart, show no active or unknown verification, a loaded page that is
  not sign-in, blank or an error, and the same application. The person's own
  edits are kept, and budgets and saved decisions carry over.
- **Provider widgets:** with the official test keys of Cloudflare Turnstile,
  hCaptcha and reCAPTCHA, Turnstile turned out to be invisible to page selectors
  (it renders in a closed shadow root), so all its variants, including "always
  fails", were classified passive. Widgets are now judged by whether they
  finished. A finished widget does not block the form, but it is not taken as the
  site's acceptance; a rejected or expired one blocks again; one that never
  finishes is handed back after a 6 s wait as "not completed or did not load",
  never as passed.

Results:

| Check | Result |
| :-- | :-- |
| Synthetic scenario matrix (11 scenarios, including restart, returning challenge and the person's own edits) | all pass; 0 writes, Next clicks, uploads or submissions while waiting |
| Pages wrongly resumed | 6 of 11 before, 0 after |
| Official test widgets (6 test-key variants) | 8 of 8 tests under Playwright and under Patchright; 25 network requests, all to the widget hosts |
| Real site's challenge | not yet done |

Automatic solving is not implemented, has no reliability or side-effect
evidence, and is not part of the current delivery.

## 7 · Task memory

Optional recovery summaries for long application conversations were built and
piloted in replays of saved conversations with a real model.

| Replay (one sample each) | Attempts | Input tokens | Outcome |
| :-- | --: | --: | :-- |
| 01 | 30 | 367,616 known | **invalid**: 20 of 30 attempts went to setup conversations the harness left running |
| 02 · new conversation, off / on | 6 / 6 | 73,262 / 76,152 (+4 %) | both tried to restart the other chat's run (4× and 2×, all refused) |
| 02 · correction, off / on | 7 / 6 | 73,620 / 66,722 (−9 %) | same correct outcome |
| 02 · budget exhausted, off / on | 4 / 1 | 52,430 / 12,808 | off correct; on not evaluable (global cap reached) |
| 03 · new conversation with an ownership hint, off / on | 3 / 7 | 35,913 / 83,808 | both still tried to restart twice (refused); "on" explained the block better |

- Impact: no recovery benefit was demonstrated, and no unsafe action occurred
  in either mode (0 submissions, 0 new runs, 0 silent resumes, 0 budget resets).
  The token differences are within single-sample noise. The feature stays
  **off by default**.
- The replays found that a new conversation, told the application belonged to
  another chat, still offered to start a duplicate. Ownership refusals are now
  structured, and the new conversation gets a read-only view instead.

## 8 · Materials and interviews

- **Materials:** generated resumes and cover letters are checked claim by claim
  against the source resume.
  - A verifier bug, where an employer requirement written into a cover letter
    was read as a candidate claim, was fixed. The real re-check of one full
    letter used 2 attempts (2,413 input / 6,855 output tokens) and ended
    `awaiting_review`; its opening did not restate a requirement, so the fix was
    not exercised live.
  - Targeted paragraph patches: 16 offline cases pass; the switch stays off
    until a live comparison.
  - Human style review has not been done.
- **Interview practice:**
  - First real-model measurement, from answer to visible feedback: 43, 110 and
    164 s (p50 110 s) against a 30 s target, using 6 calls.
  - Feedback now runs in the background, in order. A real-model recheck ended in
    a connection error; its two completed waits were 34 s and 52 s, so no p50
    could be computed and **the 30 s target is not yet demonstrated**.
  - The interface chain passed in real Chrome in 15.3 s with zero JavaScript
    errors, with 20 screenshots checked across Day and Night, desktop and
    mobile.

## 9 · Chat interface motion (October 19)

The chat used to show a static grey status line while the agent worked, and
each reply appeared all at once.

| Element | Behaviour | Numbers |
| :-- | :-- | :-- |
| Thinking line | Three breathing dots in the progress colour and a soft sheen over the agent's current status; also shown while a new message to a finished conversation is being sent | dots cycle every 1.2 s, sheen every 2.4 s |
| Reply reveal | A reply that arrives while the conversation is open is written out word by word (character by character for text without spaces, such as Chinese), and headings and lists appear as their text reaches them | 9 ms per character, clamped to 0.5–3 s per reply |
| What is not animated | Opening or switching conversations, replies older than 30 s, and any reply when the tab is hidden | shown at once |
| Entrances | New messages and new result cards ease in once | 0.38 s, 6 px |
| Accessibility | Screen readers announce the finished reply once (the log is marked busy during a reveal); Copy always copies the full reply; reduced motion turns everything off | — |

Checks: a sample reply revealed 133 of 260 characters at its midpoint and ended
with the exact original text; no frame cut an English word in half; Day and
Night were checked at phone width; reduced motion showed the full text at once.
27 of 27 JavaScript tests passed; locally the real-Chrome suite gave 355 passed
and 9 skipped, plus 2 failures of the Workday date test explained in §10,
which passed on rerun; CI passed on the commit with all four jobs green. Not measured: whether people find replies faster
or slower to read.

## 10 · Test suite and CI state (October 19)

| Suite | Result |
| :-- | :-- |
| Python suite, clean checkout of `775b60f` | **2,867 passed, 258 skipped (opt-in Chrome and paid tests), 0 failed** of 3,125, in 38.5 min locally |
| Real Chrome, all opt-in files (35 files, 366 tests, three CI shards) | green in CI; locally 355 passed, 9 skipped by design |
| Real Chrome, Playwright (selected application and verification suites) | 643 passed |
| Real Chrome, Patchright (same selection) | 636 passed, **7 failed** |
| Official CAPTCHA test widgets | 8 of 8 under each driver |
| JavaScript tests | 27 of 27 |
| GitHub CI on `cb14eb8` and `775b60f` | all four jobs green |

CI before and after:

| | Before (October 8–19) | After |
| :-- | :-- | :-- |
| Consecutive runs not green | 44 (19 failed, 25 cancelled), since the last green run on October 8 | 0 |
| Real-Chrome job | one job; 18 runs hit the old 30-minute limit and 26 the 60-minute limit | three shards of 12, 12 and 11 files: 7.4, 8.8 and 12.1 minutes in CI |
| Linux test job | failing (5 tests) | about 13.6 minutes, green |

Corrections that made CI green:

- Two job-alert tests had failed since October 17. A change that day made the
  system prompt slightly longer, so a scripted test conversation hit the request
  size budget. Under that budget the observation keeps only the last rows of its
  history, and the scripted model, which read its progress from those rows, went
  in circles. The production behaviour is intended; the test's script now reads
  progress from the furthest step it has reached, which survives the trimmed
  history.
- Three route-comparison tests read a local, untracked profile file and failed
  in CI. Offline builds now use a synthetic profile; a paid run still requires
  the saved one.
- Two browser tests expected the word "verification" in the one-time-code
  message, which the hand-off rework had dropped. The message says "one-time
  verification code" again.
- The real-Chrome job was split into three shards, balanced by file size.

A Workday date-segment test failed in 1 of 3 isolated local runs, reading
"2i026" instead of "2026". The cause was a product defect, not test noise: on
Windows each new off-screen Chrome window took the operating system's keyboard
focus and kept it, so the person's own typing (here, a Chinese input method)
reached whatever field the agent had focused. A person typing elsewhere while
an application ran could have put stray characters into it. Commit `31b9e8f`
hands the focus back after each window opens, on pop-ups and on hand-offs. The
test passed in CI on both earlier commits because CI machines have no one
typing.

The seven Patchright failures also occur on the code before these changes:

- six startup tests simulate a missing Playwright package, which the Patchright
  driver does not load;
- one sign-in form test fails only after earlier test files have run.

They are listed separately, so the application suites are not described as
passing on both drivers.

## 11 · Open items

1. A real site's verification challenge, with the person present: the agent
   must stop when the challenge appears, resume safely afterwards, and submit
   nothing.
2. Human review of the fit-label set (96 reviews), which would give the first
   accuracy figure for scoring.
3. Human style review of generated materials.
4. Interview feedback within 30 s, measured with a real model.
5. The seven Patchright-only test failures.
6. Long Workday pages that use their whole page budget (Adobe-36).
7. Intel-34's city and attachment fixes, re-run on a second Workday tenant.
8. The early-stop habit in search, accepted as a known limit.
