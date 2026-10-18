# Eregion backend

There is one application package, `eregion`:

```text
backend/
  eregion/
    runtime/     # single AgentLoop, Hosts, execution, tools and state
      core/      # the only autonomous decision kernel
      tasks/     # conversation lifecycle and user steering
      coordination/ # deterministic child-task scheduling
    domain/      # extraction, scoring, materials and fixed analysis workflow
    api/         # HTTP endpoints and application composition
    paths.py     # checkout paths independent of working directory
  migrations/    # versioned database schema history
```

Install from the repository root with `python -m pip install -r requirements.txt`.
The editable project installation exposes the `eregion` package. Start the server:

```sh
python -m uvicorn eregion.api.main:app --host 127.0.0.1 --port 8000
```

The root `main.py` is the file-analysis CLI and imports the same domain services.
Use `python main.py --help` for its arguments. Tests run with `python -m pytest`.
Database migrations use the root `alembic.ini`; its paths are anchored to that file.

There are no `custom_agent`, `job_agent`, `agent_runtime` or standalone `api` Python
packages, no `compat` package, and no old module aliases in `sys.modules`.
The domain's `AnalysisWorkflow` is a deterministic sequence, not an AgentLoop.

Historical database table names, migration IDs, environment variable names and
the existing database filename remain stable to preserve user data/configuration.
Historical evidence also retains the names of the revision it measured.

See the [runtime guide](eregion/runtime/README.md) and [documentation index](../docs/README.md).

Startup checks the database schema and never migrates it by default. A missing or
outdated schema stops startup with an explicit error before runtime resources open.
Back up the intended database and run the Alembic migration as a separate, approved
operation before restarting. Internal test/evaluation factories may opt in using
`migrate_schema=True` together with an explicit temporary `database_url`; the URL
wins over `JOB_AGENT_DATABASE_URL` for that migration.

Memory replacements must be valid now. A future-dated replacement stays a candidate:
retry the explicit supersede action when its validity period begins. The current
confirmed preference remains active until then. Equal values with different validity
intervals are distinct statements and require explicit replacement confirmation.

On restricted Windows hosts, use `python tools/scripts/pytest_workspace.py ...`.
It checks loopback before pytest starts: Windows asyncio uses a local TCP socketpair,
and blocked loopback can hang TestClient before application startup. Run offline tests
in an environment allowing loopback; retain the external-network guards in conftest.

The opt-in Stage 4A/4B implementation and offline rollout limits are documented in
[task memory](eregion/runtime/task_memory/README.md). It defaults to off.
