# Documentation index

## Using Eregion

- [User guide](usage.md): daily workflow, approvals, saved answers, alerts and recovery
- [Configuration](configuration.md): model, job sources (company boards, LinkedIn MCP, JobSpy), Jev, browser and local data
- [Evaluation report](evaluation-report.md): how Eregion was tested and what the results showed

## Building on Eregion

- [Single AgentLoop architecture](agent-loop.md)
- [How Eregion works inside](internals/README.md): the decision loop, context
  engineering, prompts, memory, models and budgets, tools and safety, the
  application agent and the career workflows, in detail
- [Runtime directory map](../backend/eregion/runtime/README.md)
- [Backend namespace and startup](../backend/README.md)
- [Development and repository map](development.md)
- [Jev and performance](jev-benchmark.md)
- [LinkedIn MCP configuration example](examples/linkedin-mcp.json)

The evaluation report is the one published summary of testing. Detailed dated
test reports, plans and raw measurements are kept locally by the maintainers and
are not part of the repository.
