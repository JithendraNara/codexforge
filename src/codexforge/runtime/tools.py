"""Tool factories wiring real adapters into the agent loop.

The agent loop never imports adapters directly. Instead this module
produces typed callables that handle validation, permission policy,
and observation shaping. Replacing an adapter (for example switching
from REST to GraphQL) only touches this file.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from ..adapters.github import GitHubAdapter, GitHubIssue
from .agent_loop import ToolCallable, ToolRegistry


def _issue_to_dict(issue: GitHubIssue) -> dict[str, Any]:
    payload = asdict(issue)
    payload["labels"] = list(issue.labels)
    return payload


def make_github_tools(adapter: GitHubAdapter) -> dict[str, ToolCallable]:
    """Return the tool name → callable map for GitHub operations."""

    def fetch_issue(args: dict[str, Any]) -> dict[str, Any]:
        repo = str(args["repo"])
        number = int(args["number"])
        issue = adapter.fetch_issue(repo, number)
        comments = adapter.fetch_issue_comments(repo, number)
        return {
            "issue": _issue_to_dict(issue),
            "comments": [
                {"author": c.author, "body": c.body, "created_at": c.created_at}
                for c in comments
            ],
        }

    def list_similar_issues(args: dict[str, Any]) -> dict[str, Any]:
        repo = str(args["repo"])
        query = str(args["query"])
        limit = int(args.get("limit", 5))
        matches = adapter.search_similar_issues(repo, query, limit=limit)
        return {"matches": [_issue_to_dict(issue) for issue in matches]}

    def get_repo_context(args: dict[str, Any]) -> dict[str, Any]:
        repo = str(args["repo"])
        summary = adapter.fetch_repo(repo)
        return {
            "full_name": summary.full_name,
            "description": summary.description,
            "default_branch": summary.default_branch,
            "language": summary.language,
            "topics": list(summary.topics),
        }

    return {
        "fetch_issue": fetch_issue,
        "list_similar_issues": list_similar_issues,
        "get_repo_context": get_repo_context,
    }


def register_github_tools(registry: ToolRegistry, adapter: GitHubAdapter) -> None:
    for name, fn in make_github_tools(adapter).items():
        registry.register(name, fn)
