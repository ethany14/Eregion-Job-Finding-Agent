"""Optional live evaluation of confirmed Memory preference adherence."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import uuid4

from eregion.runtime.memory import MemoryProvenance, MemoryScope, MemoryType
from eregion.runtime.tools.messages import AgentMessage
from eregion.api.session_dependencies import create_session_runtime
from eregion.domain.model import create_model

ROOT = Path(__file__).resolve().parent.parent.parent
DATASET = ROOT / "tools/evals" / "memory_adherence_v1.json"
MEMORY_KEY = "resume.summary.max_sentences"
MEMORY_TEXT = "Prefers resume summaries with no more than two sentences."


def grade_case(case: dict, *, response_text: str, memory_ids: list[str]) -> dict:
    lowered = response_text.casefold()
    phrases = [str(item).casefold() for item in case["expected_answer_phrases"]]
    return {
        "memory_selection_correct": bool(memory_ids) == case["expect_memory"],
        "expected_answer_phrase_present": any(
            phrase in lowered for phrase in phrases
        ),
        "passed": (
            bool(memory_ids) == case["expect_memory"]
            and any(phrase in lowered for phrase in phrases)
        ),
    }


def run(*, live: bool) -> dict:
    cases = json.loads(DATASET.read_text(encoding="utf-8"))
    report = {
        "run_metadata": {
            "timestamp": datetime.now(UTC).isoformat(),
            "dataset_version": "memory_adherence_v1",
            "live_evaluation": live,
            "runs_per_case": 1,
        },
        "memory": {"memory_key": MEMORY_KEY, "display_text": MEMORY_TEXT},
        "results": [],
    }
    if not live:
        report["metrics"] = None
        return report

    with TemporaryDirectory(prefix="memory-eval-", dir=ROOT) as directory:
        database_url = f"sqlite:///{(Path(directory) / 'evaluation.sqlite').as_posix()}"
        model = create_model()
        report["run_metadata"]["model"] = str(
            getattr(model, "model_name", None) or getattr(model, "model", "unknown")
        )
        report["run_metadata"]["temperature"] = getattr(model, "temperature", 0)
        runtime = create_session_runtime(database_url=database_url, model=model, migrate_schema=True)
        try:
            owner_id = runtime.owner_resolver.resolve().owner_id
            candidate = runtime.memories.create_candidate(
                owner_id=owner_id,
                scope=MemoryScope.USER,
                scope_id=owner_id,
                memory_key=MEMORY_KEY,
                memory_type=MemoryType.PREFERENCE,
                display_text=MEMORY_TEXT,
                content={"maximum_sentences": 2},
                provenance=[MemoryProvenance(
                    source_type="explicit_user",
                    actor_type="user",
                    user_confirmed=True,
                )],
            )
            confirmed = runtime.memories.confirm(
                candidate.memory_id,
                owner_id=owner_id,
                expected_version=candidate.version,
                confirmed_by_user=True,
            )
            for case in cases:
                started = perf_counter()
                raw_response = ""
                error = None
                memory_ids: list[str] = []
                snapshot_id = None
                outcome_status = None
                token_usage = {"input_tokens": 0, "output_tokens": 0}
                try:
                    state = runtime.coordinator.create_session(
                        session_id=f"memory-eval-{case['id']}-{uuid4()}",
                        user_id=owner_id,
                    )
                    outcome = runtime.coordinator.submit_user_message(
                        state.session_id,
                        AgentMessage(
                            message_id=str(uuid4()), role="user", content=case["prompt"]
                        ),
                        expected_version=state.version,
                    )
                    outcome_status = outcome.status.value
                    if outcome.error_code:
                        error = outcome.error_code
                    raw_response = outcome.final_text or ""
                    snapshot_id = outcome.state.last_context_snapshot_id
                    if snapshot_id:
                        snapshot = runtime.context_snapshots.require(snapshot_id)
                        memory_ids = snapshot.memory_ids
                    token_usage = {
                        "input_tokens": outcome.state.total_input_tokens,
                        "output_tokens": outcome.state.total_output_tokens,
                    }
                except Exception as exc:
                    error = type(exc).__name__
                grade = grade_case(
                    case, response_text=raw_response, memory_ids=memory_ids
                )
                report["results"].append({
                    "case_id": case["id"],
                    "prompt": case["prompt"],
                    "expected_memory": case["expect_memory"],
                    "selected_memory_ids": memory_ids,
                    "confirmed_memory_id": confirmed.memory_id,
                    "snapshot_id": snapshot_id,
                    "session_outcome_status": outcome_status,
                    "raw_response": raw_response,
                    **grade,
                    **token_usage,
                    "latency_seconds": round(perf_counter() - started, 3),
                    "error": error,
                })
        finally:
            runtime.close()
    completed = [item for item in report["results"] if item["error"] is None]
    report["metrics"] = {
        "cases": len(report["results"]),
        "completed": len(completed),
        "passed": sum(item["passed"] for item in report["results"]),
        "adherence_rate": (
            sum(item["passed"] for item in report["results"]) / len(report["results"])
            if report["results"] else 0.0
        ),
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(live=args.live)
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0 if not args.live or report["metrics"]["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
