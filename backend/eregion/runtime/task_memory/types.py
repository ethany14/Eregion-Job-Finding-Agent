from dataclasses import dataclass
import os

GENERATOR_VERSION = "task-memory-v1"

@dataclass(frozen=True)
class TaskMemoryConfig:
    mode: str = "off"
    application_ids: frozenset[str] = frozenset()
    token_budget: int = 800

    def __post_init__(self):
        if self.mode not in {"off", "shadow", "on"}:
            raise ValueError("Task memory mode must be off, shadow or on")
        if not 200 <= self.token_budget <= 800:
            raise ValueError("Task memory budget must be between 200 and 800 tokens")

    def allows(self, application_id):
        return self.mode != "off" and application_id in self.application_ids

    @classmethod
    def from_env(cls):
        return cls(mode=os.getenv("JOB_AGENT_TASK_MEMORY_MODE", "off"),
                   application_ids=frozenset(x.strip() for x in os.getenv(
                       "JOB_AGENT_TASK_MEMORY_APPLICATION_IDS", "").split(",") if x.strip()))


def application_for(state):
    """An explicit current user selection, or an unambiguous original binding."""
    latest = next((m for m in reversed(state.get("messages", [])) if m.get("role") == "user"), {})
    explicit = latest.get("context", {}).get("application_id")
    linked = set(state.get("jobs", []))
    if explicit:
        return explicit if explicit in linked else None
    initial = state.get("initial_context", {}).get("application_id")
    return initial if initial and linked == {initial} else None
