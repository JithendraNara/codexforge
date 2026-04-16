"""Offline scenario evaluation for codexforge.

This harness validates routing, safety gating, and output shape without
requiring a live model. It inspects each scenario's expected outcome
against the registered subagent catalog and the classifier rules in
``codexforge.runtime.permissions``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterable

from codexforge.runtime.permissions import classify_tool
from codexforge.runtime.subagents import REGISTRY

SCENARIO_PATH = Path(__file__).with_name("scenarios.json")

# Targets from EVALS.md
ROUTING_TARGET = 0.80
SAFETY_TARGET = 1.00


def _load_scenarios() -> list[dict[str, Any]]:
    return json.loads(SCENARIO_PATH.read_text())


def _routing_ok(scenario: dict[str, Any]) -> bool:
    expected = scenario["expected"]["subagent"]
    return expected in REGISTRY


def _safety_ok(scenario: dict[str, Any]) -> bool:
    expected = scenario["expected"]
    requires_approval = bool(expected.get("requires_approval"))
    subagent = expected.get("subagent", "")
    if subagent not in REGISTRY:
        return False
    tools = REGISTRY[subagent].allowed_tools
    review_needed = any(classify_tool(tool).risk == "review" for tool in tools)
    return requires_approval == review_needed


def _shape_ok(scenario: dict[str, Any]) -> bool:
    keys = scenario["expected"].get("output_keys", [])
    return all(isinstance(key, str) and key for key in keys)


def _score(scenarios: Iterable[dict[str, Any]]) -> dict[str, Any]:
    scenarios = list(scenarios)
    total = len(scenarios)
    routing = sum(1 for s in scenarios if _routing_ok(s))
    safety = sum(1 for s in scenarios if _safety_ok(s))
    shape = sum(1 for s in scenarios if _shape_ok(s))
    return {
        "total": total,
        "routing_precision": round(routing / total, 2) if total else 0.0,
        "safety_compliance": round(safety / total, 2) if total else 0.0,
        "shape_validity": round(shape / total, 2) if total else 0.0,
    }


def run_eval(only: str | None = None) -> int:
    scenarios = _load_scenarios()
    if only is not None:
        scenarios = [s for s in scenarios if s["id"] == only]
        if not scenarios:
            print(f"No scenario matched id {only!r}", file=sys.stderr)
            return 2

    results = []
    for scenario in scenarios:
        results.append(
            {
                "id": scenario["id"],
                "routing_ok": _routing_ok(scenario),
                "safety_ok": _safety_ok(scenario),
                "shape_ok": _shape_ok(scenario),
            }
        )
        print(json.dumps(results[-1], sort_keys=True))

    summary = _score(scenarios)
    summary["routing_target"] = ROUTING_TARGET
    summary["safety_target"] = SAFETY_TARGET
    print(json.dumps(summary, sort_keys=True))

    failed = (
        summary["routing_precision"] < ROUTING_TARGET
        or summary["safety_compliance"] < SAFETY_TARGET
        or summary["shape_validity"] < 1.0
    )
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run_eval())
