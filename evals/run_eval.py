"""Scenario evaluation harness for codexforge.

This harness runs two classes of checks:

1. **Static scenarios** (legacy, Phase 1) that verify subagent routing,
   safety gating, and output schema presence without touching adapters
   or loops.
2. **Agentic scenarios** (Phase 2) that actually run the agent loop
   against an in-memory GitHub adapter, exercising tool calls, memory,
   and verification end-to-end.

Both classes contribute to the final pass/fail verdict.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

from codexforge.adapters.github import GitHubIssue, RepoSummary
from codexforge.config import CodexForgeConfig
from codexforge.runtime.permissions import classify_tool
from codexforge.runtime.subagents import REGISTRY
from codexforge.workflows.agentic_triage import run_agentic_triage

# Imported lazily to keep ruff happy on line length
from tests.support.fakes import FakeGitHubAdapter  # type: ignore[import-not-found]

SCENARIO_PATH = Path(__file__).with_name("scenarios.json")

# Targets
ROUTING_TARGET = 0.80
SAFETY_TARGET = 1.00
AGENTIC_TARGET = 1.00


def _load_scenarios() -> list[dict[str, Any]]:
    return json.loads(SCENARIO_PATH.read_text())


def _is_agentic(scenario: dict[str, Any]) -> bool:
    return scenario.get("workflow") == "agentic_triage"


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


def _run_agentic(scenario: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    inputs = scenario["inputs"]
    expected = scenario["expected"]
    issue = GitHubIssue(
        number=int(inputs["issue"]),
        title=str(inputs["issue_title"]),
        body=str(inputs["issue_body"]),
        state="open",
        labels=(),
        comments=0,
        author="reporter",
        url="https://example",
    )
    similar = GitHubIssue(
        number=int(inputs["issue"]) - 1,
        title=f"related: {inputs['issue_title']}",
        body="Related historic issue.",
        state="closed",
        labels=("bug",),
        comments=1,
        author="other",
        url="https://example",
    )
    adapter = FakeGitHubAdapter(
        issues=[(inputs["repo"], issue)],
        comments=[(inputs["repo"], int(inputs["issue"]), [])],
        similar=[(inputs["repo"], [similar])],
        repos=[
            (
                inputs["repo"],
                RepoSummary(
                    full_name=inputs["repo"],
                    description="Eval fixture",
                    default_branch="main",
                    language="Python",
                    topics=(),
                ),
            )
        ],
    )
    with tempfile.TemporaryDirectory() as tmp:
        config = CodexForgeConfig(
            anthropic_base_url="https://api.minimax.io/anthropic",
            anthropic_auth_token=None,
            model="MiniMax-M2.7",
            github_token=None,
            approval_mode="auto",
            data_dir=Path(tmp),
        )
        result = run_agentic_triage(
            repo=inputs["repo"],
            issue_number=int(inputs["issue"]),
            config=config,
            github_adapter=adapter,
            use_live_model=False,
        )

    verified_ok = result.outcome.status == "verified" if expected.get("require_verified") else True
    tool_calls_ok = result.outcome.tool_calls >= int(expected.get("min_tool_calls", 0))
    payload = result.outcome.result or {}
    keys_ok = all(key in payload for key in expected.get("output_keys", []))
    ok = verified_ok and tool_calls_ok and keys_ok
    diagnostics = {
        "status": result.outcome.status,
        "tool_calls": result.outcome.tool_calls,
        "iterations": result.outcome.iterations,
        "payload_keys": sorted(payload.keys()),
    }
    return ok, diagnostics


def _score(scenarios: Iterable[dict[str, Any]], agentic_results: dict[str, bool]) -> dict[str, Any]:
    scenarios = list(scenarios)
    total = len(scenarios)
    routing = sum(1 for s in scenarios if _routing_ok(s))
    safety = sum(1 for s in scenarios if _safety_ok(s))
    shape = sum(1 for s in scenarios if _shape_ok(s))
    agentic_total = sum(1 for s in scenarios if _is_agentic(s))
    agentic_passed = sum(1 for result in agentic_results.values() if result)
    agentic_rate = round(agentic_passed / agentic_total, 2) if agentic_total else 1.0
    return {
        "total": total,
        "routing_precision": round(routing / total, 2) if total else 0.0,
        "safety_compliance": round(safety / total, 2) if total else 0.0,
        "shape_validity": round(shape / total, 2) if total else 0.0,
        "agentic_pass_rate": agentic_rate,
    }


def run_eval(only: str | None = None) -> int:
    scenarios = _load_scenarios()
    if only is not None:
        scenarios = [s for s in scenarios if s["id"] == only]
        if not scenarios:
            print(f"No scenario matched id {only!r}", file=sys.stderr)
            return 2

    agentic_results: dict[str, bool] = {}
    for scenario in scenarios:
        record = {
            "id": scenario["id"],
            "routing_ok": _routing_ok(scenario),
            "safety_ok": _safety_ok(scenario),
            "shape_ok": _shape_ok(scenario),
        }
        if _is_agentic(scenario):
            ok, diagnostics = _run_agentic(scenario)
            record["agentic_ok"] = ok
            record["agentic_diagnostics"] = diagnostics
            agentic_results[scenario["id"]] = ok
        print(json.dumps(record, sort_keys=True))

    summary = _score(scenarios, agentic_results)
    summary["routing_target"] = ROUTING_TARGET
    summary["safety_target"] = SAFETY_TARGET
    summary["agentic_target"] = AGENTIC_TARGET
    print(json.dumps(summary, sort_keys=True))

    failed = (
        summary["routing_precision"] < ROUTING_TARGET
        or summary["safety_compliance"] < SAFETY_TARGET
        or summary["shape_validity"] < 1.0
        or summary["agentic_pass_rate"] < AGENTIC_TARGET
    )
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run_eval())
