"""Archive before truncation in the task transaction; publish summaries with CAS."""
import hashlib
import json
import re
from sqlalchemy import select, update, delete
from eregion.runtime.security import canonical_json
from .models import TaskMemorySourceRow, TaskMemorySummaryRow
from .types import application_for, GENERATOR_VERSION


def digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def safe_quote(text):
    # User text is data, never policy. Avoid copying likely credentials into another store.
    if re.search(r"password|passwd|secret|token|api.?key|cookie|密码|密钥", text, re.I):
        return None
    return text[:600]


class TaskMemoryRepository:
    def __init__(self, factory, *, owner_id, config):
        self.factory, self.owner_id, self.config = factory, owner_id, config

    def capture(self, session, state, version):
        if state.get("task_memory_owner_id") != self.owner_id:
            return
        app = application_for(state)
        if not self.config.allows(app):
            return
        for message in state.get("messages", []):
            bound = message.get("context", {}).get("application_id")
            # A mixed-application conversation needs a selection on each source message.
            if bound is None and set(state.get("jobs", [])) == {app}:
                bound = state.get("initial_context", {}).get("application_id")
            if bound != app or message.get("role") != "user" or not message.get("id"):
                continue
            quote = safe_quote(message.get("text", ""))
            if quote:
                self._source(session, state, app, version, "user_quote", message["id"],
                             {"text": quote, "at": message.get("at"), "authority": "historical_user_quote"})
        for entry in state.get("history", []):
            bound = (entry.get("arguments") or {}).get("application_id")
            if bound is None and set(state.get("jobs", [])) == {app}:
                bound = state.get("initial_context", {}).get("application_id")
            if bound != app or not entry.get("call_id"):
                continue
            result = entry.get("result") or {}
            if not isinstance(result, dict):
                continue
            # Do not archive page text, tool instructions, or a guessed success state.
            payload = {"tool": entry.get("tool"), "at": entry.get("at"),
                       "outcome": "failed" if result.get("error") or result.get("error_code") else "observed",
                       "authority": "historical_tool_observation"}
            self._source(session, state, app, version, "tool_observation", entry["call_id"], payload)

    def _source(self, session, state, app, version, kind, key, payload):
        identifier = digest([self.owner_id, app, state["task_id"], kind, key])
        if session.get(TaskMemorySourceRow, identifier) is None:
            session.add(TaskMemorySourceRow(source_id=identifier, owner_id=self.owner_id,
                application_id=app, task_id=state["task_id"], source_kind=kind, source_key=key,
                task_version=version, payload_json=canonical_json(payload)))
            session.flush()

    def read_sources(self, app):
        from eregion.runtime.career.repository import CareerTaskRow
        with self.factory() as session:
            rows = session.scalars(select(TaskMemorySourceRow).where(
                TaskMemorySourceRow.owner_id == self.owner_id, TaskMemorySourceRow.application_id == app
            ).order_by(TaskMemorySourceRow.task_id, TaskMemorySourceRow.task_version, TaskMemorySourceRow.source_id)).all()
            sources, versions = [], {}
            for row in rows:
                task = session.get(CareerTaskRow, row.task_id)
                if task is None or task.trashed_at is not None:
                    continue
                state = json.loads(task.state_json)
                if state.get("task_memory_owner_id") != self.owner_id or app not in state.get("jobs", []):
                    continue
                versions[task.task_id] = task.version
                sources.append({"source_id": row.source_id, "task_id": row.task_id,
                    "kind": row.source_kind, "source_key": row.source_key, **json.loads(row.payload_json)})
            return sources, versions

    def sources_current(self, app, task_versions):
        from eregion.runtime.career.repository import CareerTaskRow
        with self.factory() as session:
            for task_id, version in task_versions.items():
                row = session.get(CareerTaskRow, task_id)
                state = json.loads(row.state_json) if row else {}
                if (row is None or row.version != version or row.trashed_at is not None
                        or state.get("task_memory_owner_id") != self.owner_id or app not in state.get("jobs", [])):
                    return False
        return True

    def version(self, app):
        with self.factory() as session:
            row = session.get(TaskMemorySummaryRow, digest([self.owner_id, app]))
            return row.version if row else 0

    def publish(self, app, payload, expected_version, task_versions):
        from eregion.runtime.career.repository import CareerTaskRow
        identifier, fingerprint = digest([self.owner_id, app]), digest(payload)
        with self.factory.begin() as session:
            for task_id, version in task_versions.items():
                task = session.get(CareerTaskRow, task_id)
                if task is None or task.version != version or task.trashed_at is not None:
                    return False
            row = session.get(TaskMemorySummaryRow, identifier)
            if row is None:
                if expected_version != 0:
                    return False
                session.add(TaskMemorySummaryRow(summary_id=identifier, owner_id=self.owner_id,
                    application_id=app, version=1, fingerprint=fingerprint,
                    generator_version=GENERATOR_VERSION, payload_json=canonical_json(payload)))
            elif row.version != expected_version:
                return False
            elif row.fingerprint != fingerprint:
                result = session.execute(update(TaskMemorySummaryRow).where(
                    TaskMemorySummaryRow.summary_id == identifier, TaskMemorySummaryRow.version == expected_version
                ).values(version=expected_version+1, fingerprint=fingerprint,
                         generator_version=GENERATOR_VERSION, payload_json=canonical_json(payload)))
                return result.rowcount == 1
            return True

    def invalidate(self, app, *, fingerprint=None):
        with self.factory.begin() as session:
            query = delete(TaskMemorySummaryRow).where(
                TaskMemorySummaryRow.owner_id == self.owner_id, TaskMemorySummaryRow.application_id == app)
            if fingerprint is not None:
                query = query.where(TaskMemorySummaryRow.fingerprint == fingerprint)
            session.execute(query)
