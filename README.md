<p align="center">
  <picture>
    <source media="(prefers-color-scheme: light)" srcset="frontend/site/assets/readme/banner-light.webp">
    <img src="frontend/site/assets/readme/banner-dark.webp" alt="Eregion: your job search, in one conversation." width="100%">
  </picture>
</p>

<p align="center">
  <a href="#get-started"><img src="https://img.shields.io/badge/python-3.12-3d6a4a?style=flat-square&logo=python&logoColor=white" alt="Python 3.12"></a>
  <a href="docs/configuration.md"><img src="https://img.shields.io/badge/models-OpenAI--compatible-3d6a4a?style=flat-square" alt="Any OpenAI-compatible model"></a>
  <a href="#where-jobs-come-from"><img src="https://img.shields.io/badge/sources-company%20boards%20%C2%B7%20LinkedIn-9a7428?style=flat-square" alt="Company boards and LinkedIn"></a>
  <a href="docs/evaluation-report.md"><img src="https://img.shields.io/badge/evaluation-report-c0532a?style=flat-square" alt="Evaluation report"></a>
  <a href="#-faster-fit-scores-with-jev"><img src="https://img.shields.io/badge/fit%20scoring-Jev%20optional-d9b86a?style=flat-square" alt="Optional Jev fit scoring"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-6c776f?style=flat-square" alt="MIT License"></a>
</p>

<p align="center">
  <b><a href="#get-started">Get started</a></b> ·
  <a href="https://ethany14.github.io/Eregion-Job-Finding-Agent/guide.html">User guide</a> ·
  <a href="docs/configuration.md">Configuration</a> ·
  <a href="docs/evaluation-report.md">Evaluation</a> ·
  <b><a href="https://ethany14.github.io/Eregion-Job-Finding-Agent/">Website</a></b>
</p>

---

Eregion is a job-search agent that runs on your computer. Tell it what you are
looking for and it searches company job boards and LinkedIn, reads each posting
in full, and comes back with jobs sorted by how well they fit your resume. Each
job lists its gaps and what you still need to confirm. It also tailors your
resume and cover letter, fills in application forms in Chrome up to the review
page, and helps you practise for interviews.

Everything stays in a local database. Model work uses the API key you provide,
and **nothing is ever submitted without you**.

## A workspace for the whole search

<table>
  <tr>
    <td width="50%" valign="top">
      <picture>
        <source media="(prefers-color-scheme: light)" srcset="frontend/site/assets/screens/chat-light.webp">
        <img src="frontend/site/assets/screens/chat-night.webp" alt="Council: the agent's search summary and a priority match card for a Product Analyst role, with its reason, gap and next step.">
      </picture>
      <h3>💬 Council</h3>
      Your conversations with the agent. Results arrive as cards with their tier, reason, gaps and next step.
    </td>
    <td width="50%" valign="top">
      <picture>
        <source media="(prefers-color-scheme: light)" srcset="frontend/site/assets/screens/home-light.webp">
        <img src="frontend/site/assets/screens/home-night.webp" alt="The Chronicle home view: today's events, beacons that need you, and applications on the road.">
      </picture>
      <h3>📖 Chronicle</h3>
      Home: what happened today, and which conversations are waiting on you.
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <picture>
        <source media="(prefers-color-scheme: light)" srcset="frontend/site/assets/screens/road-light.webp">
        <img src="frontend/site/assets/screens/road-night.webp" alt="The Road: application totals, a winding map of stations, and a progress table.">
      </picture>
      <h3>🛤️ The Road</h3>
      Every application on one map, board or table, with response rates.
    </td>
    <td width="50%" valign="top">
      <picture>
        <source media="(prefers-color-scheme: light)" srcset="frontend/site/assets/screens/forge-light.webp">
        <img src="frontend/site/assets/screens/forge-night.webp" alt="The Forge: tailored resumes and cover letters with a drafted, tempered and sealed trail and a verified preview.">
      </picture>
      <h3>⚒️ The Forge</h3>
      Materials are <i>drafted</i>, <i>tempered</i> (each claim checked against your resume) and <i>sealed</i> when you approve them.
    </td>
  </tr>
</table>

Job alerts run recurring searches and send you digests, and **The Lookout**
shows the agent's browser so you can take control. Switch between parchment
**Day** and holly-green **Night** from the sidebar.

## How a job search works

Ask in plain words, for example *“Find senior backend roles, remote in the US,
for my resume.”* The agent:

1. **Searches** the verified company-board directory and LinkedIn, mixing
   results across employers.
2. **Reads each posting in full** and checks it against what you asked for:
   role, location, level, and whether the opening is current. Only clear
   mismatches are dropped, such as a different occupation, a Staff role when you
   asked for senior, or a city you ruled out. A near miss is kept, with the
   difference stated.
3. **Scores fit** against your resume, using your main model or optional
   [Jev](#-faster-fit-scores-with-jev). Scoring that runs under a budget is
   saved in small batches, so a budget stop keeps every finished score.
4. **Sorts the results into three tiers**, each with evidence quoted from the
   posting:

| Tier | Means |
| :-- | :-- |
| **Priority match** | Role, location and level match, and your resume shows the core requirements. |
| **Worth trying** | Relevant, but a core skill or experience isn't on your resume. Each gap is listed with how much it matters. |
| **Needs your confirmation** | Something only you can answer: GPA, work authorization, start date, or a level or location the posting doesn't make clear. |

There is no quota and no padding with weak matches. Only jobs the agent saved as
recommendations can appear under a tier; jobs that were not scored, or were
scored and set aside, are listed separately as such. The reply says what was
searched, what failed and what is left, and you can reply *“search more”* to
continue.

## One road, from first sight to offer

Every application moves through the same five stations. The agent does the work;
you make the decisions.

```mermaid
flowchart LR
    A(["<b>Sighted</b><br/>search boards<br/>and LinkedIn"]) --> B(["<b>Prepared</b><br/>tiered fit · tailor resume,<br/>cover letter, answers"])
    B --> C(["<b>Sent</b><br/>fill forms in Chrome,<br/>ask you for the rest"])
    C --> D(["<b>In interview</b><br/>job-specific practice<br/>and feedback"])
    D --> E(["<b>Offer</b>"])
    classDef st fill:#16211d,stroke:#7fb08e,color:#e4ece7,stroke-width:1.5px;
    classDef gold fill:#16211d,stroke:#d9b86a,color:#d9b86a,stroke-width:1.5px;
    class A,B,C,D st;
    class E gold;
```

| Station | What Eregion does |
| :-- | :-- |
| **Sighted** | Searches boards and LinkedIn on request or on a schedule, and returns tiered recommendations with gaps and items to confirm. |
| **Prepared** | Shows the fit score with evidence. Drafts a tailored resume, cover letter and answers, and checks every claim against your resume. |
| **Sent** | Works through the application in Chrome page by page up to the review page: fills in known details, uploads approved documents once, and asks you in chat for anything missing. |
| **In interview** | Practises questions for the specific job, with feedback and a final report. |

You can correct, pause or take over at any time. **Approving materials and the
final submit are always yours.**

### Applying on real sites

Applications have been tested on real employer sites, with the owner's own
details and sign-in, always stopping at the review page:

- **Workday** (Adobe and Intel tenants) reached the review page with every
  answer matching the applicant's facts. Sign-in goes through a secure account
  form; your password never enters the chat or the model.
- Postings at **Figma, Palantir, Notion, Ampersand and Veeva** reached the review
  page in earlier pilots.
- A resume is uploaded once and checked on the page; a file already shown, or one
  the agent did not upload, is left for you to decide.
- When a site asks for a **CAPTCHA, one-time code or access check**, the agent
  stops, tells you, and waits while you handle it in the same browser. It
  continues only when the application form is back, and never reads or stores
  your codes. Automatic CAPTCHA solving is not part of Eregion.

<a id="where-jobs-come-from"></a>
## Where jobs come from

| Source | Setup | Notes |
| :-- | :-- | :-- |
| **Company boards** (Greenhouse, Lever, Ashby, Workday) | None | A directory of 118 boards, each verified through its public API. |
| **LinkedIn**, through a local MCP server | One-time sign-in in a separate browser profile | On by default once configured. Calls are budgeted, and the source stops on sign-in or rate-limit problems. See [default sources](docs/configuration.md#default-job-sources). |
| **JobSpy** (optional) | A separate Python with `python-jobspy` installed, set as `JOB_AGENT_JOBSPY_PYTHON` | Indeed and LinkedIn cards with full descriptions; Indeed answers a search in one request. Off by default, requests per task are capped. In a one-sample side-by-side test it was fast and complete on text but produced fewer recommendations under the same budget, so boards and LinkedIn stay the default ([details](docs/evaluation-report.md#2--job-sources)). |
| **Treg** (optional) | Your own Treg key; you pay Treg directly | LinkedIn search and page extraction, with spend capped per call ($0.01), per task ($0.10) and per day ($0.50). Off by default. See [configuration](docs/configuration.md#optional-treg-job-sources). |
| **A posting you paste or open** | None | Share a link or job text in chat, or use the **Eregion Lens** Chrome extension on any job page. |

Indeed's MCP server only accepts allow-listed clients, so Eregion does not use it.

## ⚡ Faster fit scores with Jev

[Jev](docs/jev-benchmark.md) is a fast yes/no decision model from Typesafe. With
it on, fit scoring uses Jev, while your main model still handles conversation,
planning and writing. Eregion works fully without it.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: light)" srcset="frontend/site/assets/readme/jev-speed-light.webp">
    <img src="frontend/site/assets/readme/jev-speed-dark.webp" alt="Time to score fit, seconds per job on one scale: main model only 32–49 s; Jev, average real posting 0.39 s; Jev, slowest live search 0.59 s per posting." width="70%">
  </picture>
</p>

In the October 14 scenario runs, Jev scored **150 real postings inside 13 live
searches**:

| Measure | Result |
| :-- | --: |
| Jev time per posting, average | **0.39 s** |
| Jev time per posting, per search | 0.28–0.59 s |
| Jev requests per posting | 2.1 |
| Failed Jev requests | 0 of 317 |
| Main model only, one job (earlier synthetic test) | 32–49 s |

Jev time is the total of its request times, and postings were scored in shared
batches. The main-model figure comes from a separate cold test, so the two rows
compare scale, not identical conditions. To turn Jev on, add these lines to
`.env` and restart:

```dotenv
TYPESAFE_API_KEY=your-typesafe-key
TYPESAFE_BASE_URL=https://api.typesafe.ai
TYPESAFE_DEFAULT_MODEL=jev-latest
TYPESAFE_ENABLED=true
```

- Up to 16 jobs are scored per batch, and decisions are cached locally, so a
  scoring batch cut short never pays twice for the same decision.
- A requirement Jev can't decide stays *unresolved*, and the score is shown as a
  range.
- There is no fallback to the main model: if Typesafe is unreachable, scoring
  stops until you fix the key or turn Jev off.

Fit requests send the extracted resume text and the job description; resume
files, screenshots and passwords never leave your machine. Every setting is in
[configuration](docs/configuration.md#optional-jev-acceleration).

## How well it works

Search is tested with four synthetic applicants (a junior data analyst in New
York, a senior backend engineer for remote US, an ML intern in California and a
junior UX designer in Austin); applications are tested on real employer sites.
Every live run has fixed call and token budgets, and nothing is submitted.

- **Search results:** in the latest four-scenario runs, 123 jobs were shown with
  0 structural errors. In a human review, 0 of 23 shown jobs were wrong, and 28
  of 29 labelled postings were shown or hidden the way the reviewer wanted.
- **Tiers:** about two in three match the reviewer; most misses are one tier
  apart.
- **Applications:** Workday applications at Adobe and Intel reached the review
  page with correct answers, in 53–59 model calls per application.
- **Honest replies:** replies state what failed and what is left; under a tight
  budget, unscored jobs are listed as unscored, never as recommendations.

Known limits:

- The agent often stops searching early, and says so with an offer to continue.
- Verdicts can vary slightly between runs.
- Scoring accuracy has not been measured against human labels yet.
- Long Workday pages can use their whole per-page budget; the run then pauses
  with everything filled so far kept.

The full summary, including what failed along the way, is in the
**[evaluation report](docs/evaluation-report.md)**.

## Get started

> [!NOTE]
> You need **Python 3.12**, **Google Chrome**, and an API key for an
> **OpenAI-compatible** model that supports tool calling and structured output.
> The visual browser fallback also needs image input.

### 1 · Install

```sh
git clone https://github.com/ethany14/Eregion-Job-Finding-Agent.git
cd Eregion-Job-Finding-Agent
```

<details open>
<summary><b>macOS or Linux</b></summary>

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

</details>

<details>
<summary><b>Windows PowerShell</b></summary>

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

</details>

### 2 · Connect your model

Create `.env` in the repository root:

```dotenv
LLM_MODEL_ID=your-model-id
LLM_API_KEY=your-api-key
# Only if your provider uses a custom compatible endpoint.
LLM_BASE_URL=https://your-provider.example/v1
```

> [!IMPORTANT]
> Keep `.env` private; Git ignores it.

To add LinkedIn as a source, follow [default sources](docs/configuration.md#default-job-sources).
It takes a one-time sign-in in a separate browser profile; company boards work
without it.

### 3 · Start Eregion

```sh
python -m alembic upgrade head
python -m uvicorn eregion.api.main:app --host 127.0.0.1 --port 8000
```

Open **[localhost:8000/apply](http://localhost:8000/apply)**, upload a PDF resume
with selectable text, check **Your details**, and ask for what you want:

> 🔎 *Find junior data analyst roles in New York for my resume. Explain any gaps.*

> ✍️ *Tailor my resume and write a cover letter for this role. Keep every claim
> supported by my experience.*

> ⏰ *Every weekday at 9 AM New York time, recommend new roles like these.*

Keep the server running while the agent works or scheduled searches are due.

### Optional switches

All are off by default; add them to `.env` and restart.

| Setting | Effect |
| :-- | :-- |
| `JOB_AGENT_JOBSPY_PYTHON=path\to\python` | Enables JobSpy as an extra source the agent may choose. `JOB_AGENT_JOBSPY_SITES=indeed,linkedin` picks the sites (default `indeed`). It can also be switched on and off in the app under **Model and API key → Features**. |
| `JOB_AGENT_BROWSER_DRIVER=patchright` | Runs applications with Patchright (included in `requirements.txt`) instead of the default Playwright. |
| `JOB_AGENT_TASK_MAX_PROVIDER_ATTEMPTS`, `JOB_AGENT_TASK_MAX_INPUT_TOKENS` | Hard limits per conversation, counted before each request is sent. |
| `JOB_AGENT_TASK_MEMORY_MODE=on` | Experimental recovery summaries for long application conversations. |

## Good to know

| | |
| :-- | :-- |
| 🔒 **Local, single-user** | The server listens on your machine only. It is not a hosted service. |
| 🧩 **Some steps are yours** | Sign-in, MFA, CAPTCHA and one-time codes pause the agent until you handle them in the same browser; it then checks that the form is back before continuing. |
| 📊 **Scores describe evidence** | A score reflects what your resume shows, not an employer's decision. |
| ✅ **Review before you send** | Materials are checked against your resume, but read them before use. |
| 📄 **Text PDFs** | Resumes need selectable text; scanned files need text extraction first. |

## Documentation

| Guide | Covers |
| :-- | :-- |
| 📘 [Using Eregion](docs/usage.md) | Daily workflow, approvals, saved answers, alerts and recovery |
| ⚙️ [Configuration](docs/configuration.md) | Model, job sources, LinkedIn MCP, Treg, Jev, browser and local data |
| 🧭 [Architecture](docs/agent-loop.md) | The single agent loop, evidence, context and browser workers |
| 📈 [Evaluation report](docs/evaluation-report.md) | How Eregion was tested and what the results showed |
| ⚡ [Jev and performance](docs/jev-benchmark.md) | Fast scoring, recorded measurements and how to reproduce them |
| 🛠️ [Development](docs/development.md) | Repository layout, checks, live evaluation and contributing |
| 🌍 [Website](https://ethany14.github.io/Eregion-Job-Finding-Agent/) | Overview, user guide and docs · [site source](frontend/site/README.md) |

<details>
<summary><b>For developers</b></summary>

Code lives in `backend/` (one Python namespace: `eregion.runtime`,
`eregion.domain`, `eregion.api`), `frontend/` and `tools/`. Start with the
[runtime map](backend/eregion/runtime/README.md) and the
[documentation index](docs/README.md). Run the checks from the repository root:

```sh
python -m pytest -q
node --test "tests/*.mjs"
```

Real-Chrome tests are opt-in: set `JOB_AGENT_BROWSER_TESTS=1` (and
`JOB_AGENT_BROWSER_DRIVER=patchright` to repeat them with Patchright). Tests
that load the CAPTCHA providers' official test widgets also need
`JOB_AGENT_OFFICIAL_WIDGETS=1`.

The running server also serves [API docs](http://localhost:8000/docs) and a
[health check](http://localhost:8000/health).

</details>

## License

Eregion is released under the [MIT License](LICENSE).

<p align="center">
  <br>
  <img src="frontend/site/assets/holly.svg" width="36" alt=""><br>
  <sub><i>A little less paperwork. A step closer.</i></sub>
</p>
