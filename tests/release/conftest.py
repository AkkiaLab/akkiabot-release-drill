"""Fixtures for the release flow: a real bare ``origin`` + clone, and a fake GitHub."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from scripts.release.git import Git
from scripts.release.github import SEALED_RULE_TYPES, Issue, PullRequest, review_problems


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@dataclass
class Repo:
    """``origin`` (bare) plus a developer clone ``dev`` and the automation clone ``git``."""

    origin: Path
    dev: Path
    git: Git

    def commit(self, branch: str, path: str, content: str, message: str | None = None) -> str:
        sh(self.dev, "fetch", "-q", "origin")
        exists = subprocess.run(
            ["git", "rev-parse", "--verify", "-q", f"origin/{branch}"],
            cwd=self.dev,
            capture_output=True,
            check=False,
        )
        if exists.returncode == 0:
            sh(self.dev, "checkout", "-q", "-B", branch, f"origin/{branch}")
        else:
            sh(self.dev, "checkout", "-q", "-B", branch)
        file = self.dev / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
        sh(self.dev, "add", path)
        sh(self.dev, "commit", "-q", "-m", message or f"{branch}: {path}")
        sh(self.dev, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        return sh(self.dev, "rev-parse", "HEAD")

    def push(self, sha: str, ref: str) -> None:
        sh(self.dev, "push", "-q", "origin", f"{sha}:{ref}")

    def sha(self, ref: str) -> str:
        return sh(self.origin, "rev-parse", ref)

    def ancestor(self, a: str, b: str) -> bool:
        return (
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", a, b], cwd=self.origin, check=False
            ).returncode
            == 0
        )

    def show(self, ref: str, path: str) -> str:
        return sh(self.origin, "show", f"{ref}:{path}")


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    origin = tmp_path / "origin.git"
    sh(tmp_path, "init", "-q", "--bare", "-b", "master", str(origin))
    dev = tmp_path / "dev"
    sh(tmp_path, "clone", "-q", str(origin), str(dev))
    for clone in (dev,):
        sh(clone, "config", "user.name", "test")
        sh(clone, "config", "user.email", "test@example.invalid")
    (dev / "README.md").write_text("base\n")
    sh(dev, "add", ".")
    sh(dev, "commit", "-q", "-m", "init")
    sh(dev, "push", "-q", "origin", "HEAD:refs/heads/master")
    auto = tmp_path / "auto"
    sh(tmp_path, "clone", "-q", str(origin), str(auto))
    sh(auto, "config", "user.name", "release-bot")
    sh(auto, "config", "user.email", "bot@example.invalid")
    return Repo(origin, dev, Git(auto))


@dataclass
class FakeGitHub:
    statuses: dict[str, str] = field(default_factory=dict)
    sealed: set[str] = field(default_factory=set)
    broken_rules: bool = False
    issues: dict[int, Issue] = field(default_factory=dict)
    closed: dict[int, Issue] = field(default_factory=dict)
    comments: list[tuple[int, str]] = field(default_factory=list)
    prs: dict[int, PullRequest] = field(default_factory=dict)
    commit_prs: dict[str, list[PullRequest]] = field(default_factory=dict)
    reviews: dict[int, list[tuple[str, str, str, str]]] = field(default_factory=dict)
    pr_comments: dict[int, list[tuple[str, str]]] = field(default_factory=dict)
    unresolved: dict[int, int] = field(default_factory=dict)
    head_changes: dict[int, str] = field(default_factory=dict)
    gate_reads: int = 0
    runs: list[tuple[str, dict[str, str], str]] = field(default_factory=list)
    run_conclusion: str = "success"
    on_run: Callable[[dict[str, str]], None] | None = None
    green: set[str] = field(default_factory=set)
    jobs: set[tuple[str, str, str, str]] = field(default_factory=set)
    dispatched: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    notified: list[dict[str, Any]] = field(default_factory=list)
    on_seal: Callable[[str], None] | None = None
    seal_calls: list[tuple[str, bool]] = field(default_factory=list)

    def acceptance(self, sha: str) -> str | None:
        return self.statuses.get(sha)

    def sealed_branches(self) -> set[str]:
        return set(self.sealed)

    def set_sealed(self, branch: str, *, sealed: bool) -> None:
        self.seal_calls.append((branch, sealed))
        if sealed:
            self.sealed.add(branch)
            if self.on_seal:
                self.on_seal(branch)
        else:
            self.sealed.discard(branch)

    def branch_rule_types(self, branch: str) -> set[str]:
        if branch in self.sealed and not self.broken_rules:
            return set(SEALED_RULE_TYPES)
        return set()

    def find_issue(self, marker: str) -> Issue | None:
        return next((i for i in self.issues.values() if marker in i.body), None)

    def open_conflict_issues(self) -> list[Issue]:
        return list(self.issues.values())

    def create_issue(self, title: str, body: str, labels: list[str]) -> Issue:
        number = len(self.issues) + len(self.closed) + 1
        issue = Issue(number, body, f"https://example.invalid/issues/{number}")
        self.issues[number] = issue
        return issue

    def comment_issue(self, number: int, body: str) -> None:
        self.comments.append((number, body))

    def close_issue(self, number: int, comment: str) -> None:
        self.comments.append((number, comment))
        self.closed[number] = self.issues.pop(number)

    def prs_for_commit(self, sha: str) -> list[PullRequest]:
        return self.commit_prs.get(sha, [])

    def open_prs(self, base: str | None = None) -> list[PullRequest]:
        return [p for p in self.prs.values() if p.state == "open" and base in (None, p.base)]

    def get_pr(self, number: int) -> PullRequest:
        pr = self.prs[number]
        if number in self.head_changes:  # simulate a push to the PR during the checks
            self.gate_reads += 1
            if self.gate_reads > 1:
                pr.head_sha = self.head_changes[number]
        return pr

    def retarget_pr(self, number: int, base: str) -> None:
        self.prs[number].base = base

    def comment_pr(self, number: int, body: str) -> None:
        self.comments.append((number, body))

    def review_gate(self, number: int, head: str) -> list[str]:
        return review_problems(
            author=self.prs[number].author,
            head=head,
            unresolved=self.unresolved.get(number, 0),
            reviews=self.reviews.get(number, []),
            comments=self.pr_comments.get(number, []),
        )

    def run_and_wait(
        self, workflow: str, inputs: dict[str, str], title: str, timeout: float = 1800
    ) -> str:
        self.runs.append((workflow, inputs, title))
        if self.on_run:
            self.on_run(inputs)
        return self.run_conclusion

    def checks_passed(self, sha: str, contexts: tuple[str, ...]) -> bool:
        return sha in self.green

    def approved_run(self, workflow: str, title: str, job: str, environment: str) -> bool:
        return (workflow, title, job, environment) in self.jobs

    def dispatch(self, workflow: str, inputs: dict[str, str], ref: str = "master") -> None:
        self.dispatched.append((workflow, inputs))

    def notify(self, payload: dict[str, Any]) -> str:
        self.notified.append(payload)
        return "已通知 Multica（fake）"


@pytest.fixture
def gh() -> FakeGitHub:
    return FakeGitHub()
