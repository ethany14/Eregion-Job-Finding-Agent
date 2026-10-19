# Runtime layout

All autonomous decisions use [`core/engine.py`](core/engine.py). A package with
`worker`, `workflow`, `coordinator` or `loop` in its name is not permission to
implement another model-driven decision cycle.

| Layer | Directory | Responsibility |
| --- | --- | --- |
| Decision kernel | `core/` | AgentLoop, Host contract, conversation window, interruptible model waits |
| Conversation owner | `tasks/` | Task lifecycle, TaskHost, pause/resume and user steering |
| Execution owner | `execution/` | Bounded execution slots, cancellation, shutdown and resource ownership |
| Child coordination | `coordination/` | Durable child tasks, leases, dependencies, budgets and deterministic scheduling |
| Other Hosts | `sessions/`, `auto_apply/`, `tools/` | SessionHost, ApplicationHost and tool-run Host; all use the same kernel |
| Career capabilities | `career/` | Search, posting verification, screening and capability handoffs |
| Transports | `mcp/`, `browser/` | MCP server lifecycle and browser sessions |
| Domain services | `application_pack/`, `resumes/`, `job_workflow/`, `interviewer/`, `mock_interview/` | Materials, application workflows and interview operations |
| Shared state | `context/`, `evidence/`, `memory/`, `workspace/`, `assistant/`, `feedback/`, `learning/`, `skills/` | Context, evidence, user state and bounded capabilities |

Shared modules at this package root (provider accounting, tool policy/registry,
request archives, model adapters and telemetry) serve several layers. They are
not additional execution owners.

## Adding a feature

1. Put source retrieval in `career/` through the existing MCP/browser transports.
   Public company boards and configured LinkedIn MCP are the default; JobSpy is opt-in.
2. Expose a bounded domain capability through an existing Host. For genuinely new
   observations or lifecycle requirements, implement the `core/host_protocol.py`
   contract and reuse AgentLoop.
3. Dispatch background work through `execution/`; use `coordination/` when durable
   dependencies/leases are required. Neither layer chooses new model actions.
4. Keep provider calls on the existing accounted path. Keep nested usage,
   cancellation, persisted recovery and approval boundaries intact.

## Canonical namespace

Use `eregion.runtime`, `eregion.domain` and `eregion.api`. The previous top-level
Python packages have been removed; there are no runtime compatibility shims.
`AnalysisWorkflow` is a fixed domain workflow, and has no `AgentLoop` alias.
The only `AgentLoop` definition is `eregion.runtime.core.engine.AgentLoop`.

Historical reports and `git show` comparisons retain their original revision paths.
The offline baseline loader translates imports locally; it does not expose old
packages in the running application. Database table names and migration identifiers
remain unchanged to preserve saved user data.
