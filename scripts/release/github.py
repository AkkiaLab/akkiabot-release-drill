"""GitHub (and Multica webhook) access for the release flow.

``GitHub`` is the interface the logic depends on; ``GhCli`` implements it with
``gh api`` so the token stays in ``gh``'s environment (``GH_TOKEN`` in Actions,
the logged-in ``gh`` on the runtime machine) and never passes through Python.
Tests use an in-memory fake (tests/release/conftest.py).
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from scripts.release.versions import ReleaseError

ACCEPTANCE_CONTEXT = "staging-acceptance"
#: Ruleset whose ``include`` list holds every sealed release branch (docs/release.md §4).
SEALED_RULESET = "releases 封版"
#: Rule types the sealed ruleset must enforce on a branch before a formal tag is created.
SEALED_RULE_TYPES = frozenset({"update", "deletion", "non_fast_forward"})
CONFLICT_LABEL = "release-forward-conflict"
FORWARD_LABEL = "release-forward"
FIX_LABEL = "release:fix"
#: Bot user ID of the workspace agent App ``akkialab-agent`` (App ID 5760780, formerly
#: ``akkiabot-reviewer``; docs/reviewer-github-app.md). Its approval is the only one that
#: lets the release flow land a conflict resolution. Matched by ID, never by login: the
#: login follows the App name, the ID survives renames (AKKIA-25).
REVIEWER_BOT_ID = 356702926
#: Identity label of that App in gate results. Logins never contain spaces or
#: parentheses, so no other account can map to this label.
REVIEWER = f"Reviewer（App bot 用户 ID {REVIEWER_BOT_ID}）"


def actor_identity(author: dict[str, Any] | None) -> str:
    """GraphQL author → the identity used by the gate.

    The Reviewer App is recognised only by ``__typename == "Bot"`` *and* its bot
    user ID (``databaseId``), and becomes ``REVIEWER``. Other Apps become
    ``<login>[bot]`` (GraphQL omits the ``[bot]`` suffix REST shows); users keep
    their login, so a *user* or another App named like the Reviewer can never
    pass for it. Deleted accounts (``None``) become ``""``.
    """
    if not author:
        return ""
    login = author.get("login") or ""
    if author.get("__typename") == "Bot":
        if author.get("databaseId") == REVIEWER_BOT_ID:
            return REVIEWER
        return f"{login}[bot]" if login else ""
    # GitHub user logins cannot contain brackets; never let one look like an App.
    return f"user:{login}" if login.endswith("[bot]") else login


@dataclass
class PullRequest:
    number: int
    title: str
    base: str
    head_ref: str
    head_sha: str
    author: str
    labels: set[str] = field(default_factory=set)
    state: str = "open"
    merged: bool = False


@dataclass
class Issue:
    number: int
    body: str
    url: str = ""


class GitHub(Protocol):
    def acceptance(self, sha: str) -> str | None: ...
    def sealed_branches(self) -> set[str]: ...
    def set_sealed(self, branch: str, *, sealed: bool) -> None: ...
    def branch_rule_types(self, branch: str) -> set[str]: ...
    def find_issue(self, marker: str) -> Issue | None: ...
    def open_conflict_issues(self) -> list[Issue]: ...
    def create_issue(self, title: str, body: str, labels: list[str]) -> Issue: ...
    def comment_issue(self, number: int, body: str) -> None: ...
    def close_issue(self, number: int, comment: str) -> None: ...
    def prs_for_commit(self, sha: str) -> list[PullRequest]: ...
    def open_prs(self, base: str | None = None) -> list[PullRequest]: ...
    def get_pr(self, number: int) -> PullRequest: ...
    def retarget_pr(self, number: int, base: str) -> None: ...
    def comment_pr(self, number: int, body: str) -> None: ...
    def review_gate(self, number: int, head: str) -> list[str]: ...
    def checks_passed(self, sha: str, contexts: tuple[str, ...]) -> bool: ...
    def approved_run(self, workflow: str, title: str, job: str, environment: str) -> bool: ...
    def dispatch(self, workflow: str, inputs: dict[str, str], ref: str = "master") -> None: ...
    def run_and_wait(
        self, workflow: str, inputs: dict[str, str], title: str, timeout: float = 1800
    ) -> str: ...
    def notify(self, payload: dict[str, Any]) -> str: ...


class GhError(ReleaseError):
    pass


class RunNotTerminatedError(ReleaseError):
    """A dispatched run may still be running (or may still start): the caller must keep
    whatever it protects (the staging lease) until a person confirms it ended."""


class GhCli:
    """``GitHub`` over ``gh api``; ``repo`` is ``owner/name``."""

    def __init__(self, repo: str, *, webhook_env: str = "MULTICA_RELEASE_WEBHOOK_URL") -> None:
        self.repo = repo
        self.webhook_env = webhook_env
        self.sleep = time.sleep
        self.clock = time.monotonic
        self.poll_interval = 10.0
        self.cancel_timeout = 300.0

    # ----------------------------------------------------------- plumbing
    def _api(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        paginate: bool = False,
        allow_404: bool = False,
    ) -> Any:
        args = ["gh", "api", "-X", method, "-H", "Accept: application/vnd.github+json"]
        if paginate:
            args += ["--paginate", "--slurp"]
        args.append(path)
        stdin = None
        if body is not None:
            args += ["--input", "-"]
            stdin = json.dumps(body)
        proc = subprocess.run(  # noqa: S603 - fixed executable; token stays inside gh
            args, input=stdin, capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            if allow_404 and "HTTP 404" in proc.stderr:
                return None
            raise GhError(f"gh api {method} {path} 失败：{proc.stderr.strip()[:300]}")
        if not proc.stdout.strip():
            return None
        data = json.loads(proc.stdout)
        if paginate:
            merged: list[Any] = []
            for page in data:
                merged.extend(page if isinstance(page, list) else [page])
            return merged
        return data

    def _r(self, path: str) -> str:
        return f"repos/{self.repo}/{path}"

    # ----------------------------------------------------------- statuses
    def acceptance(self, sha: str) -> str | None:
        statuses = self._api(self._r(f"commits/{sha}/statuses?per_page=100"), paginate=True)
        for status in statuses or []:  # newest first
            if status.get("context") == ACCEPTANCE_CONTEXT:
                return status.get("state")
        return None

    def checks_passed(self, sha: str, contexts: tuple[str, ...]) -> bool:
        pages = self._api(self._r(f"commits/{sha}/check-runs?per_page=100"), paginate=True) or []
        conclusions: dict[str, str | None] = {}
        for page in pages:
            for run in page.get("check_runs", []) if isinstance(page, dict) else []:
                conclusions.setdefault(run["name"], run.get("conclusion"))
        statuses = self._api(self._r(f"commits/{sha}/status")) or {}
        for status in statuses.get("statuses", []):
            conclusions.setdefault(status["context"], status.get("state"))
        return all(conclusions.get(c) == "success" for c in contexts)

    # ------------------------------------------------------------ rulesets
    def _sealed_ruleset(self) -> dict[str, Any] | None:
        rulesets = self._api(self._r("rulesets?per_page=100"), paginate=True) or []
        for ruleset in rulesets:
            if ruleset.get("name") == SEALED_RULESET:
                return self._api(self._r(f"rulesets/{ruleset['id']}"))
        return None

    def sealed_branches(self) -> set[str]:
        """Branches listed in the sealed ruleset; none while it does not exist yet."""
        ruleset = self._sealed_ruleset()
        if ruleset is None:
            return set()
        include = ruleset["conditions"]["ref_name"]["include"]
        return {ref.removeprefix("refs/heads/") for ref in include}

    def set_sealed(self, branch: str, *, sealed: bool) -> None:
        ruleset = self._sealed_ruleset()
        if ruleset is None:
            raise GhError(
                f"仓库中没有名为 {SEALED_RULESET!r} 的规则集，无法锁定（接入步骤见 docs/release.md §7）"
            )
        ref = f"refs/heads/{branch}"
        include = list(ruleset["conditions"]["ref_name"]["include"])
        if sealed and ref not in include:
            include.append(ref)
        elif not sealed and ref in include:
            include.remove(ref)
        else:
            return
        conditions = ruleset["conditions"]
        conditions["ref_name"]["include"] = include
        body = {
            "name": ruleset["name"],
            "target": ruleset["target"],
            "enforcement": ruleset["enforcement"],
            "conditions": conditions,
            "rules": ruleset["rules"],
            "bypass_actors": ruleset.get("bypass_actors", []),
        }
        self._api(self._r(f"rulesets/{ruleset['id']}"), method="PUT", body=body)

    def branch_rule_types(self, branch: str) -> set[str]:
        quoted = urllib.parse.quote(branch, safe="")
        rules = self._api(self._r(f"rules/branches/{quoted}?per_page=100"), paginate=True) or []
        return {rule["type"] for rule in rules}

    # -------------------------------------------------------------- issues
    def find_issue(self, marker: str) -> Issue | None:
        issues = self._api(
            self._r(f"issues?state=open&labels={CONFLICT_LABEL}&per_page=100"), paginate=True
        )
        for issue in issues or []:
            if marker in (issue.get("body") or ""):
                return Issue(issue["number"], issue["body"], issue.get("html_url", ""))
        return None

    def open_conflict_issues(self) -> list[Issue]:
        issues = self._api(
            self._r(f"issues?state=open&labels={CONFLICT_LABEL}&per_page=100"), paginate=True
        )
        return [
            Issue(i["number"], i.get("body") or "", i.get("html_url", ""))
            for i in issues or []
            if "pull_request" not in i
        ]

    def create_issue(self, title: str, body: str, labels: list[str]) -> Issue:
        data = self._api(
            self._r("issues"), method="POST", body={"title": title, "body": body, "labels": labels}
        )
        return Issue(data["number"], data["body"], data.get("html_url", ""))

    def comment_issue(self, number: int, body: str) -> None:
        self._api(self._r(f"issues/{number}/comments"), method="POST", body={"body": body})

    def close_issue(self, number: int, comment: str) -> None:
        self.comment_issue(number, comment)
        self._api(self._r(f"issues/{number}"), method="PATCH", body={"state": "closed"})

    # ----------------------------------------------------------- pull requests
    @staticmethod
    def _pr(data: dict[str, Any]) -> PullRequest:
        return PullRequest(
            number=data["number"],
            title=data["title"],
            base=data["base"]["ref"],
            head_ref=data["head"]["ref"],
            head_sha=data["head"]["sha"],
            author=data["user"]["login"],
            labels={label["name"] for label in data.get("labels", [])},
            state=data.get("state", "open"),
            merged=bool(data.get("merged_at")),
        )

    def prs_for_commit(self, sha: str) -> list[PullRequest]:
        data = self._api(self._r(f"commits/{sha}/pulls"), allow_404=True) or []
        return [self._pr(pr) for pr in data]

    def open_prs(self, base: str | None = None) -> list[PullRequest]:
        query = "state=open&per_page=100" + (f"&base={urllib.parse.quote(base)}" if base else "")
        return [self._pr(pr) for pr in self._api(self._r(f"pulls?{query}"), paginate=True) or []]

    def get_pr(self, number: int) -> PullRequest:
        return self._pr(self._api(self._r(f"pulls/{number}")))

    def retarget_pr(self, number: int, base: str) -> None:
        self._api(self._r(f"pulls/{number}"), method="PATCH", body={"base": base})

    def comment_pr(self, number: int, body: str) -> None:
        self.comment_issue(number, body)

    def review_gate(self, number: int, head: str) -> list[str]:
        """Problems that forbid landing ``head`` of PR ``number`` (empty = may land).

        The agent App bypasses the merge rules, so it re-checks them itself:
        the Reviewer's newest review approves exactly ``head``; nobody's newest
        review requests changes; every review thread is resolved; and nobody but
        the PR author and the Reviewer commented after that approval (feedback
        that came later must be handled and re-approved first).
        """
        pr = self._review_data(number)
        return review_problems(
            author=pr["author"],
            head=head,
            unresolved=sum(1 for t in pr["threads"] if not t["isResolved"]),
            reviews=[
                (
                    actor_identity(r["author"]),
                    r["state"],
                    (r["commit"] or {}).get("oid", ""),
                    r["submittedAt"] or "",
                )
                for r in pr["reviews"]
            ],
            comments=[(actor_identity(c["author"]), c["createdAt"]) for c in pr["comments"]],
        )

    #: One GraphQL query per connection, paged oldest first until ``hasNextPage`` is false.
    REVIEW_CONNECTIONS: ClassVar[dict[str, str]] = {
        "threads": "reviewThreads(first: 100, after: $after) { pageInfo { hasNextPage endCursor } nodes { isResolved } }",
        "reviews": "reviews(first: 100, after: $after) { pageInfo { hasNextPage endCursor } nodes { author { __typename login ... on Bot { databaseId } } state submittedAt commit { oid } } }",
        "comments": "comments(first: 100, after: $after) { pageInfo { hasNextPage endCursor } nodes { author { __typename login ... on Bot { databaseId } } createdAt } }",
    }
    MAX_REVIEW_PAGES: int = 50

    def _graphql(self, query: str, variables: dict[str, Any]) -> Any:
        args = ["gh", "api", "graphql", "-f", f"query={query}"]
        for key, value in variables.items():
            if value is None:
                continue
            args += ["-F" if isinstance(value, int) else "-f", f"{key}={value}"]
        proc = subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603
        if proc.returncode != 0:
            raise GhError(f"GraphQL 查询失败：{proc.stderr.strip()[:300]}")
        data = json.loads(proc.stdout)
        if data.get("errors"):
            raise GhError(f"GraphQL 查询返回错误：{str(data['errors'])[:300]}")
        return data["data"]

    def _review_data(self, number: int) -> dict[str, Any]:
        """Every review thread, review and comment of the PR (all pages, or an error)."""
        owner, name = self.repo.split("/", 1)
        result: dict[str, Any] = {"author": ""}
        for key, connection in self.REVIEW_CONNECTIONS.items():
            field = connection.split("(", 1)[0]
            query = (
                "query($owner: String!, $name: String!, $number: Int!, $after: String) {"
                " repository(owner: $owner, name: $name) { pullRequest(number: $number) {"
                f" author {{ __typename login ... on Bot {{ databaseId }} }} {connection} }} }} }}"
            )
            nodes: list[Any] = []
            after: str | None = None
            for _ in range(self.MAX_REVIEW_PAGES):
                data = self._graphql(
                    query, {"owner": owner, "name": name, "number": number, "after": after}
                )
                pr = data["repository"]["pullRequest"]
                result["author"] = actor_identity(pr["author"])
                page = pr[field]
                nodes.extend(page["nodes"])
                if not page["pageInfo"]["hasNextPage"]:
                    break
                after = page["pageInfo"]["endCursor"]
            else:
                raise GhError(
                    f"PR #{number} 的 {field} 超过 {self.MAX_REVIEW_PAGES} 页，未读全，拒绝落地"
                )
            result[key] = nodes
        return result

    # ------------------------------------------------------------ workflows
    def approved_run(self, workflow: str, title: str, job: str, environment: str) -> bool:
        """The newest master run of ``workflow`` named ``title`` had ``job`` succeed after an
        approval of ``environment`` (the run's review history, not just a green job).

        Only the newest run counts, so a rollback or retry of an old tag needs a
        fresh approval instead of reusing the one given at its first release."""
        runs = self._api(
            self._r(
                f"actions/workflows/{workflow}/runs?branch=master&event=workflow_dispatch&per_page=100"
            )
        )
        for run in (runs or {}).get("workflow_runs", []):  # newest first
            if run.get("display_title") != title or run.get("head_branch") != "master":
                continue
            jobs = self._api(self._r(f"actions/runs/{run['id']}/jobs?per_page=100")) or {}
            if not any(
                j["name"] == job and j.get("conclusion") == "success" for j in jobs.get("jobs", [])
            ):
                return False
            approvals = self._api(self._r(f"actions/runs/{run['id']}/approvals")) or []
            return any(
                approval.get("state") == "approved"
                and environment in {env.get("name") for env in approval.get("environments", [])}
                for approval in approvals
            )
        return False

    def dispatch(self, workflow: str, inputs: dict[str, str], ref: str = "master") -> None:
        self._api(
            self._r(f"actions/workflows/{workflow}/dispatches"),
            method="POST",
            body={"ref": ref, "inputs": inputs},
        )

    def run_and_wait(
        self, workflow: str, inputs: dict[str, str], title: str, timeout: float = 1800
    ) -> str:
        """Dispatch ``workflow`` (its ``run-name`` must equal ``title``), wait, return the conclusion.

        Returns only once the run is known to have ended. On timeout or API
        failure the run is cancelled and its end confirmed; when that cannot be
        confirmed (run not found, cancel or polling failing) ``RunNotTerminatedError``
        is raised so the caller keeps its lease rather than letting a late
        upload overwrite the next RC.
        """
        self.dispatch(workflow, inputs)
        deadline = self.clock() + timeout
        run_id: int | None = None
        try:
            while self.clock() < deadline:
                self.sleep(self.poll_interval)
                if run_id is None:
                    run_id = self._find_run(workflow, title)
                    continue
                run = self._api(self._r(f"actions/runs/{run_id}"))
                if run.get("status") == "completed":
                    return run.get("conclusion") or "unknown"
            problem = f"{timeout:.0f} 秒内未结束"
        except GhError as exc:
            problem = f"查询失败（{exc}）"
        if run_id is None:
            try:
                run_id = self._find_run(workflow, title)
            except GhError:
                run_id = None
        if run_id is None:
            raise RunNotTerminatedError(
                f"{workflow} 运行 {title!r} {problem}，且找不到该运行，无法确认它不会继续执行"
            )
        if self._cancel_and_confirm(run_id):
            return "cancelled"
        raise RunNotTerminatedError(f"{workflow} 运行 {run_id} {problem}，取消后仍未确认结束")

    def _find_run(self, workflow: str, title: str) -> int | None:
        runs = self._api(
            self._r(f"actions/workflows/{workflow}/runs?event=workflow_dispatch&per_page=30")
        )
        for run in (runs or {}).get("workflow_runs", []):
            if run.get("display_title") == title:
                return run["id"]
        return None

    def _cancel_and_confirm(self, run_id: int) -> bool:
        """Cancel (force-cancel if needed) and wait until GitHub reports the run completed."""
        deadline = self.clock() + self.cancel_timeout
        forced = False
        # A 409 when it already finished is fine: the status below decides.
        with contextlib.suppress(GhError):
            self._api(self._r(f"actions/runs/{run_id}/cancel"), method="POST")
        while self.clock() < deadline:
            self.sleep(self.poll_interval)
            try:
                run = self._api(self._r(f"actions/runs/{run_id}"))
            except GhError:
                continue
            if run.get("status") == "completed":
                return True
            if not forced and self.clock() > deadline - self.cancel_timeout / 2:
                forced = True
                with contextlib.suppress(GhError):
                    self._api(self._r(f"actions/runs/{run_id}/force-cancel"), method="POST")
        return False

    # -------------------------------------------------------------- webhook
    def notify(self, payload: dict[str, Any]) -> str:
        """POST to the Multica autopilot webhook. The URL is a secret: never printed."""
        url = os.environ.get(self.webhook_env, "")
        if not url:
            return f"未配置 {self.webhook_env}，未通知 Multica"
        if not url.startswith("https://"):
            return f"{self.webhook_env} 不是 https 地址，未通知 Multica"
        request = urllib.request.Request(  # noqa: S310 - https enforced above
            url,
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                return f"已通知 Multica（HTTP {response.status}）"
        except urllib.error.HTTPError as exc:
            return f"通知 Multica 失败（HTTP {exc.code}）"
        except (urllib.error.URLError, TimeoutError) as exc:
            return f"通知 Multica 失败（{type(exc).__name__}）"


def review_problems(
    *,
    author: str,
    head: str,
    unresolved: int,
    reviews: list[tuple[str, str, str, str]],
    comments: list[tuple[str, str]],
) -> list[str]:
    """Pure part of ``GhCli.review_gate``. ``reviews``: (login, state, commit, time), oldest first;
    ``comments``: (login, time). Times are ISO 8601 UTC strings (comparable as text)."""
    problems: list[str] = []
    latest: dict[str, tuple[str, str, str]] = {}
    for login, state, commit, at in reviews:
        if login != author and state in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            latest[login] = (state, commit, at)
    reviewer = latest.get(REVIEWER)
    if reviewer is None or reviewer[0] != "APPROVED" or reviewer[1] != head:
        problems.append(f"{REVIEWER} 尚未在当前 head {head[:12]} 上给出准出（Approve）")
    blocked = sorted(
        login for login, (state, _, _) in latest.items() if state == "CHANGES_REQUESTED"
    )
    if blocked:
        problems.append(f"仍有未处理的 Request changes：{', '.join(blocked)}")
    if unresolved:
        problems.append(f"还有 {unresolved} 个未解决的评审线程")
    if reviewer is not None and reviewer[0] == "APPROVED":
        approved_at = reviewer[2]
        late = sorted(
            {
                login
                for login, at in comments
                if at > approved_at and login not in {author, REVIEWER}
            }
            | {
                login
                for login, state, _, at in reviews
                if at > approved_at and login not in {author, REVIEWER} and state == "COMMENTED"
            }
        )
        if late:
            problems.append(f"准出之后还有未经 Reviewer 复核的评论：{', '.join(late)}")
    return problems
