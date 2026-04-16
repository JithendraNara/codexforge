"""Thinker implementations for the agent loop.

Two thinkers are provided:

- :class:`RuleBasedTriageThinker` is a deterministic triage brain that
  produces the same shape of output as a live model. It exists so the
  agent loop can be tested and demoed without any network dependency
  and so codexforge has a *second independent verifier* of its own
  logic. In practice it mirrors the skill instructions in
  ``skills/issue-triage/SKILL.md``.
- :class:`ClaudeAgentThinker` wraps the Claude Agent SDK so the
  codexforge loop can drive MiniMax-M2.7 via the Anthropic-compatible
  endpoint. It is imported lazily so that offline tests and linting do
  not require the SDK to be installed.

Both thinkers conform to :class:`agent_loop.ThinkerProtocol`.
"""

from __future__ import annotations

import importlib
import json
import re
import textwrap
from dataclasses import dataclass
from typing import Any

from .agent_loop import Thought, ToolCall


_SEVERITY_ORDER = ("low", "medium", "high", "critical")
_PRIORITY_ORDER = ("p3", "p2", "p1", "p0")


# --------------------------------------------------------------------- #
# Deterministic thinker (offline agentic behaviour)                     #
# --------------------------------------------------------------------- #


@dataclass(slots=True)
class RuleBasedTriageThinker:
    """Offline triage thinker used for tests and demos.

    Mirrors the skill contract: observe issue text, optionally consult
    similar past issues, then produce a verified triage payload.
    """

    repo: str

    def think(self, *, goal: str, context: str, iteration: int) -> Thought:
        issue_title, issue_body = _extract_issue_fragments(goal)
        if iteration == 1:
            query = _keyword_query(issue_title, issue_body)
            return Thought(
                commentary=(
                    "First, recall similar historic issues to avoid duplicating"
                    " past work."
                ),
                tool_call=ToolCall(
                    name="list_similar_issues",
                    arguments={
                        "repo": self.repo,
                        "query": query,
                        "limit": 5,
                    },
                ),
            )

        similar = _parse_similar_from_context(context)
        category = _infer_category(issue_title, issue_body)
        severity = _infer_severity(issue_body)
        priority = _priority_from_severity(severity, similar_count=len(similar))
        labels = _recommend_labels(category, severity)
        summary = _compose_summary(issue_title, issue_body, severity)

        payload = {
            "category": category,
            "severity": severity,
            "priority": priority,
            "summary": summary,
            "recommended_labels": labels,
            "suggested_next_action": _suggested_action(category, similar_count=len(similar)),
        }

        commentary = (
            f"Classified as {category} at severity {severity}."
            f" Found {len(similar)} similar issue(s)."
        )
        return Thought(commentary=commentary, final_result=payload)


def _extract_issue_fragments(goal: str) -> tuple[str, str]:
    title_match = re.search(r"Issue [^\n]*:\s*(?P<title>.+)", goal)
    title = title_match.group("title").strip() if title_match else ""
    body_match = re.search(r"Body:\n---\n(?P<body>.+?)\n---", goal, re.DOTALL)
    body = body_match.group("body").strip() if body_match else ""
    if not body:
        body = goal
    return title, body


def _keyword_query(title: str, body: str) -> str:
    text = f"{title} {body}".lower()
    tokens = [token for token in re.findall(r"[a-z][a-z0-9_-]{3,}", text)]
    stop = {
        "when",
        "with",
        "that",
        "this",
        "from",
        "steps",
        "reproduce",
        "expected",
        "actual",
        "please",
        "have",
        "will",
    }
    unique: list[str] = []
    for token in tokens:
        if token in stop:
            continue
        if token not in unique:
            unique.append(token)
        if len(unique) >= 4:
            break
    return " ".join(unique) or title


def _parse_similar_from_context(context: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for line in context.splitlines():
        if "tool_result" not in line or "list_similar_issues" not in line:
            continue
        blob_start = line.find("{")
        if blob_start == -1:
            continue
        try:
            payload = json.loads(line[blob_start:])
        except json.JSONDecodeError:
            continue
        data = payload.get("data") or payload
        matches_value = (
            data.get("matches") if isinstance(data, dict) else None
        )
        if isinstance(matches_value, list):
            matches.extend(matches_value)
    return matches


def _infer_category(title: str, body: str) -> str:
    text = f"{title}\n{body}".lower()
    if any(term in text for term in ("crash", "segfault", "regression", "broken", "bug")):
        return "bug"
    if any(term in text for term in ("feature", "add support", "would be nice")):
        return "feature"
    if any(term in text for term in ("docs", "documentation", "typo")):
        return "docs"
    if any(term in text for term in ("question", "how do i", "how to")):
        return "question"
    if "security" in text or "cve" in text:
        return "security"
    return "bug"


def _infer_severity(body: str) -> str:
    text = body.lower()
    if any(term in text for term in ("data loss", "security", "outage", "blocker")):
        return "critical"
    if any(term in text for term in ("crash", "segfault", "cannot", "broken")):
        return "high"
    if any(term in text for term in ("slow", "degraded", "confusing")):
        return "medium"
    return "low"


def _priority_from_severity(severity: str, similar_count: int) -> str:
    base_index = _SEVERITY_ORDER.index(severity)
    if similar_count >= 2:
        base_index = min(base_index + 1, len(_PRIORITY_ORDER) - 1)
    return _PRIORITY_ORDER[base_index]


def _recommend_labels(category: str, severity: str) -> list[str]:
    labels = [category, f"severity/{severity}"]
    if category == "bug":
        labels.append("needs-triage")
    if severity in {"high", "critical"}:
        labels.append("priority/high")
    return labels


def _compose_summary(title: str, body: str, severity: str) -> str:
    body = body.strip().splitlines()[0] if body.strip() else title
    body = textwrap.shorten(body, width=160, placeholder="…")
    if severity in {"high", "critical"} and not any(
        term in body.lower()
        for term in ("crash", "data loss", "outage", "security", "broken")
    ):
        body = f"{body} (reported crash/broken behaviour)"
    return f"{title}. {body}" if title else body


def _suggested_action(category: str, similar_count: int) -> str:
    if similar_count >= 2:
        return "duplicate"
    if category in {"bug", "security"}:
        return "needs_reproduction"
    if category == "feature":
        return "needs_design"
    return "ready"


# --------------------------------------------------------------------- #
# Claude Agent SDK thinker (live MiniMax-M2.7 behaviour)                 #
# --------------------------------------------------------------------- #


class ClaudeAgentThinker:
    """Thin adapter that drives the Claude Agent SDK one iteration at a time.

    The Claude Agent SDK owns its own agent loop, but we want explicit
    control over verification and budgeting. This wrapper issues a single
    ``query`` with a strict system prompt that asks the model to return
    JSON describing either a tool call or a final result. codexforge then
    executes the tool or verifies the result itself.
    """

    def __init__(
        self,
        *,
        model: str,
        base_system_prompt: str,
        tool_catalog: dict[str, str],
    ) -> None:
        self._model = model
        self._base_prompt = base_system_prompt
        self._tool_catalog = tool_catalog

    def think(self, *, goal: str, context: str, iteration: int) -> Thought:
        sdk = importlib.import_module("claude_agent_sdk")
        prompt = self._build_prompt(goal=goal, context=context, iteration=iteration)
        options = sdk.ClaudeAgentOptions(
            model=self._model,
            system_prompt=self._base_prompt,
            allowed_tools=[],
        )
        import asyncio  # local import to avoid top-level dependency surface

        async def _collect() -> str:
            pieces: list[str] = []
            async for message in sdk.query(prompt=prompt, options=options):
                result = getattr(message, "result", None)
                if isinstance(result, str):
                    pieces.append(result)
            return "".join(pieces)

        raw = asyncio.run(_collect())
        return _parse_thought_json(raw)

    # ------------------------------------------------------------------ #

    def _build_prompt(self, *, goal: str, context: str, iteration: int) -> str:
        tool_lines = "\n".join(
            f"- {name}: {doc}" for name, doc in sorted(self._tool_catalog.items())
        )
        return textwrap.dedent(
            f"""
            You are inside codexforge's bounded agent loop (iteration {iteration}).
            Output a single JSON object, nothing else.

            Schema:
            {{
              "commentary": "short reasoning",
              "tool_call": {{"name": "...", "arguments": {{...}}}} OR null,
              "final_result": {{...}} OR null
            }}

            At each turn, choose EITHER to call one tool or to emit a final
            result. Never fabricate tool output.

            Goal:
            {goal}

            Tools available:
            {tool_lines or "- (none)"}

            Context so far:
            {context}
            """
        ).strip()


def _parse_thought_json(raw: str) -> Thought:
    payload = _extract_json(raw)
    commentary = str(payload.get("commentary") or "")
    tool_call_payload = payload.get("tool_call")
    tool_call: ToolCall | None = None
    if isinstance(tool_call_payload, dict) and tool_call_payload.get("name"):
        tool_call = ToolCall(
            name=str(tool_call_payload["name"]),
            arguments=dict(tool_call_payload.get("arguments") or {}),
        )
    final_result = payload.get("final_result")
    if not isinstance(final_result, dict):
        final_result = None
    return Thought(commentary=commentary, tool_call=tool_call, final_result=final_result)


def _extract_json(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return {}
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
