# Stage 4A / 4B: application task recovery pilot

This is deterministic derived context, not a new source of authorization or career
facts. It makes zero model calls and starts no background workers.

## Modes and scope

The default is off. `JOB_AGENT_TASK_MEMORY_MODE` accepts `off`, `shadow` (capture and
build, no injection), or `on` (capture, build, revalidate and inject). Both enabled
modes also require an explicit comma-separated application allowlist in
`JOB_AGENT_TASK_MEMORY_APPLICATION_IDS`. No wildcard is supported. No live pilot is
enabled by this change. Neither mode migrates a database.

Only new tasks created through a configured CareerService are stamped with the
server-resolved owner. Legacy tasks are not silently claimed or backfilled. A task
must have a current explicitly selected linked application, or exactly its one
originally bound application. Mixed-application messages require per-message
application selection; ambiguous messages are not archived for an application.

## Source and summary contracts

TaskRepository captures user quotations and minimal tool observations in the same
transaction as the task mutation. It captures the old window before truncation and
the new window after successful version checking. IDs are deterministic per owner,
application, task, kind and message/call ID. Failed mutations roll back both stores.
Each source retains its task version and origin ID; retries are idempotent. No raw
page content, tool arguments, credentials or assistant assertions are archived.
Quotes containing likely credential markers are skipped. Source rows cascade when
the original task is permanently deleted; trashed or unlinked tasks cannot contribute.

Application state, effective confirmed preferences, packs and item versions, open
questions and proposed plans are projected from live repositories. User quotes stay
historical quotes, plans stay proposals and a completed tool call is not proof of a
successful application. Task and material states are not inferred from conversation.

A separately versioned summary stores a generator version and content fingerprint.
It can be discarded using `invalidate(application_id)`. Reads rebuild from current
authorities, publish with optimistic version checking and recheck selected references
before returning. A stale generation cannot overwrite a newer summary. On failed
reads, conflicts or revocation, no old cached text is injected. Ordinary context is
still used. Durable source capture errors roll back the task mutation rather than
acknowledging an unrecorded update; the existing task error handling reports failure.

CareerHarness.observe injects the result into the real TaskHost/TaskLoop request.
It is labelled untrusted recovery data, has an 800 estimated-token cap, preserves
source references, and reports budget omissions. It includes at most eight recent
archived observations; this is bounded recovery context, not complete conversational
recall. Latest user instructions and live execution permissions retain precedence.
The existing conversation window does not replay the task summary as tool history.

## Rollout and validation

Migration 0039 adds two derived-data tables. Explicit migration is required; startup
on 0034 or 0038 refuses to proceed and does not update the DB. Test upgrades and
downgrades use disposable files. Turning the mode off removes injection and stops
capture without a schema downgrade. Real database migration, live application
allowlisting and Stage 5 remain separate decisions.

Offline coverage lives in `tests/test_task_memory.py`,
`tests/test_task_memory_end_to_end.py` and `tests/test_task_memory_migration.py`.
The end-to-end fixture uses the actual CareerHarness and TaskLoop with a scripted
model and synthetic documents. It does not establish real-model recall quality.
On Windows run through `tools/scripts/pytest_workspace.py` with local loopback
allowed; retain the external-network guards in `tests/conftest.py`.
