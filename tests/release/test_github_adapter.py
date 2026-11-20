"""GhCli adapter logic without the network: scripted ``_api`` / ``_graphql`` and a fake clock."""

from typing import Any

import pytest

from scripts.release.github import REVIEWER_BOT_ID, GhCli, GhError, RunNotTerminatedError

HEAD = "a" * 40


class ScriptedGh(GhCli):
    def __init__(
        self, routes: dict[str, Any], graph: dict[str, list[dict[str, Any]]] | None = None
    ) -> None:
        super().__init__("AkkiaLab/AkkiaBot")
        self.routes = routes  # path suffix -> value | list of values | Exception
        self.graph = graph or {}
        self.calls: list[tuple[str, str]] = []
        self.queries: list[str] = []
        self.now = 0.0
        self.sleep = self._advance
        self.clock = lambda: self.now
        self.poll_interval = 10
        self.cancel_timeout = 100

    def _advance(self, seconds: float) -> None:
        self.now += seconds

    def _api(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        paginate: bool = False,
        allow_404: bool = False,
    ) -> Any:
        self.calls.append((method, path))
        for suffix, value in self.routes.items():
            if path.split("?")[0].endswith(suffix):
                if isinstance(value, list):
                    value = value.pop(0) if len(value) > 1 else value[0]
                if isinstance(value, Exception):
                    raise value
                return value
        return None

    def _graphql(self, query: str, variables: dict[str, Any]) -> Any:
        self.queries.append(query)
        field = next(f for f in ("reviewThreads", "reviews", "comments") if f"{f}(" in query)
        pages = self.graph.get(field, [{"nodes": [], "hasNext": False}])
        index = int(variables.get("after") or 0)
        page = pages[index]
        return {
            "repository": {
                "pullRequest": {
                    "author": {"__typename": "User", "login": "AkkiaS7"},
                    field: {
                        "pageInfo": {"hasNextPage": page["hasNext"], "endCursor": str(index + 1)},
                        "nodes": page["nodes"],
                    },
                }
            }
        }


RUNS = {"workflow_runs": [{"id": 7, "display_title": "staging x n"}]}


def test_run_and_wait_success() -> None:
    gh = ScriptedGh(
        {
            "/runs": RUNS,
            "/runs/7": [
                {"status": "in_progress"},
                {"status": "completed", "conclusion": "success"},
            ],
        }
    )
    assert gh.run_and_wait("webapp-deploy.yml", {}, "staging x n") == "success"


def test_timeout_cancels_and_confirms_before_returning() -> None:
    still = [{"status": "in_progress"}] * 8
    gh = ScriptedGh(
        {"/runs": RUNS, "/runs/7": [*still, {"status": "completed", "conclusion": "cancelled"}]}
    )
    assert gh.run_and_wait("webapp-deploy.yml", {}, "staging x n", timeout=50) == "cancelled"
    assert ("POST", "repos/AkkiaLab/AkkiaBot/actions/runs/7/cancel") in gh.calls


def test_api_failure_while_uploading_cancels_and_confirms() -> None:
    gh = ScriptedGh(
        {
            "/runs": RUNS,
            "/runs/7": [GhError("HTTP 502"), {"status": "completed", "conclusion": "cancelled"}],
        }
    )
    assert gh.run_and_wait("webapp-deploy.yml", {}, "staging x n") == "cancelled"
    assert any(path.endswith("/cancel") for _, path in gh.calls)


def test_unconfirmed_end_raises_instead_of_returning() -> None:
    gh = ScriptedGh({"/runs": RUNS, "/runs/7": {"status": "in_progress"}})
    with pytest.raises(RunNotTerminatedError, match="未确认结束"):
        gh.run_and_wait("webapp-deploy.yml", {}, "staging x n", timeout=30)
    assert any(path.endswith("/force-cancel") for _, path in gh.calls)


def test_run_never_found_raises() -> None:
    gh = ScriptedGh({"/runs": {"workflow_runs": []}})
    with pytest.raises(RunNotTerminatedError, match="找不到"):
        gh.run_and_wait("webapp-deploy.yml", {}, "staging x n", timeout=30)


#: The Reviewer App as the real GraphQL API reports it (read from PR #53, 2026-11-20):
#: type Bot, login WITHOUT the ``[bot]`` suffix that REST and the UI show, and
#: ``databaseId`` equal to the REST ``user.id``.
REVIEWER_GRAPHQL = {
    "__typename": "Bot",
    "login": "akkiabot-reviewer",
    "databaseId": REVIEWER_BOT_ID,
}
#: The same App after the rename to ``akkialab-agent`` (AKKIA-25): same ID, new login.
RENAMED_REVIEWER_GRAPHQL = {
    "__typename": "Bot",
    "login": "akkialab-agent",
    "databaseId": REVIEWER_BOT_ID,
}
COPILOT_GRAPHQL = {
    "__typename": "Bot",
    "login": "copilot-pull-request-reviewer",
    "databaseId": 175728472,
}


def _user(login: str) -> dict[str, str]:
    return {"__typename": "User", "login": login}


def _review(
    author: dict[str, str] | None, state: str, at: str, commit: str = HEAD
) -> dict[str, Any]:
    return {
        "author": author,
        "state": state,
        "submittedAt": at,
        "commit": {"oid": commit},
    }


def test_review_gate_reads_every_page() -> None:
    approval = _review(REVIEWER_GRAPHQL, "APPROVED", "2026-11-15T10:00:00Z")
    graph = {
        # Unresolved thread only on the second page.
        "reviewThreads": [
            {"nodes": [{"isResolved": True}] * 100, "hasNext": True},
            {"nodes": [{"isResolved": False}], "hasNext": False},
        ],
        # An early Request changes (page 1) still stands after 100+ later reviews.
        "reviews": [
            {
                "nodes": [_review(COPILOT_GRAPHQL, "CHANGES_REQUESTED", "2026-11-15T09:00:00Z")],
                "hasNext": True,
            },
            {
                "nodes": [_review(_user("AkkiaS7"), "COMMENTED", "2026-11-15T09:30:00Z")] * 100,
                "hasNext": True,
            },
            {"nodes": [approval], "hasNext": False},
        ],
    }
    problems = ScriptedGh({}, graph).review_gate(1, HEAD)
    assert any("未解决的评审线程" in p for p in problems)
    assert any("Request changes：copilot-pull-request-reviewer[bot]" in p for p in problems)

    clean = {"reviews": [{"nodes": [approval], "hasNext": False}]}
    assert ScriptedGh({}, clean).review_gate(1, HEAD) == []


def test_review_gate_refuses_when_pages_cannot_be_exhausted() -> None:
    gh = ScriptedGh({}, {"comments": [{"nodes": [], "hasNext": True}] * 3})
    gh.MAX_REVIEW_PAGES = 3
    with pytest.raises(GhError, match="未读全"):
        gh.review_gate(1, HEAD)


@pytest.mark.parametrize("reviewer", [REVIEWER_GRAPHQL, RENAMED_REVIEWER_GRAPHQL])
def test_real_graphql_reviewer_shape_is_recognised(reviewer: dict[str, Any]) -> None:
    """GraphQL says Bot/<slug> without ``[bot]``; the App is matched by its bot user ID,
    so the gate keeps working before and after the App rename."""
    graph = {
        "reviews": [
            {
                "nodes": [_review(reviewer, "APPROVED", "2026-11-15T10:00:00Z")],
                "hasNext": False,
            }
        ]
    }
    assert ScriptedGh({}, graph).review_gate(1, HEAD) == []


def test_review_gate_queries_bot_ids_for_every_author() -> None:
    """Without ``databaseId`` the Reviewer could not be recognised at all."""
    gh = ScriptedGh({})
    gh.review_gate(1, HEAD)
    assert gh.queries
    for query in gh.queries:
        assert query.count("author { __typename login ... on Bot { databaseId } }") == (
            1 if "reviewThreads(" in query else 2
        )


@pytest.mark.parametrize(
    "author",
    [
        _user("akkiabot-reviewer"),  # a user squatting the App's name is not the App
        _user("akkialab-agent[bot]"),
        # Another App with the Reviewer's (old or new) name but a different ID.
        {"__typename": "Bot", "login": "akkiabot-reviewer", "databaseId": 1},
        {"__typename": "Bot", "login": "akkialab-agent", "databaseId": 1},
        # A user whose account ID happens to equal the bot's: only a Bot counts.
        {"__typename": "User", "login": "someone", "databaseId": REVIEWER_BOT_ID},
        {"__typename": "Bot", "login": "some-other-app"},
        None,  # deleted account
    ],
)
def test_only_the_reviewer_app_counts(author: dict[str, str] | None) -> None:
    graph = {
        "reviews": [
            {"nodes": [_review(author, "APPROVED", "2026-11-15T10:00:00Z")], "hasNext": False}
        ]
    }
    problems = ScriptedGh({}, graph).review_gate(1, HEAD)
    assert any("准出" in p for p in problems)


def test_late_bot_comment_after_reviewer_approval_blocks() -> None:
    graph = {
        "reviews": [
            {
                "nodes": [_review(REVIEWER_GRAPHQL, "APPROVED", "2026-11-15T10:00:00Z")],
                "hasNext": False,
            }
        ],
        "comments": [
            {
                "nodes": [{"author": COPILOT_GRAPHQL, "createdAt": "2026-11-15T10:05:00Z"}],
                "hasNext": False,
            }
        ],
    }
    problems = ScriptedGh({}, graph).review_gate(1, HEAD)
    assert problems == ["准出之后还有未经 Reviewer 复核的评论：copilot-pull-request-reviewer[bot]"]
