"""Cascade forward merges against a real git origin."""

import pytest

from scripts.release.branches import create_release
from scripts.release.cascade import Hop, land_forward_pr, plan, run_cascade
from scripts.release.git import Git
from scripts.release.github import CONFLICT_LABEL, FORWARD_LABEL, REVIEWER_LOGIN, PullRequest
from scripts.release.versions import ReleaseError, Version
from tests.release.conftest import FakeGitHub, Repo

V = Version.parse


def _bootstrap(repo: Repo) -> None:
    """v1.0.0 sealed + tagged, v1.0.1 (patch), v1.1.0 (regular) open."""
    create_release(repo.git, V("v1.0.0"))
    repo.commit("releases/v1.0.0", "a.txt", "a\n")
    repo.push(repo.sha("refs/heads/releases/v1.0.0"), "refs/tags/v1.0.0")
    repo.commit("master", "m.txt", "master work\n")
    create_release(repo.git, V("v1.1.0"))
    create_release(repo.git, V("v1.0.1"))


def test_forward_chain_reaches_master_and_keeps_merge_relation(repo: Repo, gh: FakeGitHub) -> None:
    _bootstrap(repo)
    gh.sealed.add("releases/v1.0.0")
    run_cascade(repo.git, gh, trigger="bootstrap")
    fix = repo.commit("releases/v1.0.1", "fix.txt", "fix\n", "BOT-90: fix")

    report = run_cascade(repo.git, gh, trigger="PR #1")

    assert [m.split("（")[0] for m in report.merged] == [
        "releases/v1.0.1->releases/v1.1.0",
        "releases/v1.1.0->master",
    ]
    for ref in ("refs/heads/releases/v1.1.0", "refs/heads/master"):
        assert repo.ancestor(fix, ref)  # merge relation kept (no squash)
        assert repo.show(ref, "fix.txt") == "fix"
    # The forward commit is a real merge (two parents).
    parents = repo.git.out("rev-list", "--parents", "-n1", repo.sha("refs/heads/releases/v1.1.0"))
    assert len(parents.split()) == 3
    assert plan(repo.git, repo.git.state(), gh.sealed) == []


def test_duplicate_and_delayed_runs_are_noops(repo: Repo, gh: FakeGitHub) -> None:
    _bootstrap(repo)
    repo.commit("releases/v1.0.1", "fix.txt", "fix\n")
    run_cascade(repo.git, gh, trigger="first")
    before = {r: repo.sha(r) for r in ("refs/heads/master", "refs/heads/releases/v1.1.0")}
    second = run_cascade(repo.git, gh, trigger="duplicate event")
    assert second.merged == []
    assert {r: repo.sha(r) for r in before} == before


def test_sealed_target_is_skipped_but_sealed_source_still_forwards(
    repo: Repo, gh: FakeGitHub
) -> None:
    _bootstrap(repo)
    run_cascade(repo.git, gh, trigger="sync")
    gh.sealed.update({"releases/v1.0.0", "releases/v1.0.1"})
    v101 = repo.sha("refs/heads/releases/v1.0.1")
    # A late commit on the (sealed) v1.0.0 line, e.g. pushed before the lock.
    late = repo.commit("releases/v1.0.0", "late.txt", "late\n")

    report = run_cascade(repo.git, gh, trigger="late event")

    assert repo.sha("refs/heads/releases/v1.0.1") == v101  # sealed target untouched
    assert repo.ancestor(late, "refs/heads/releases/v1.1.0")
    assert repo.ancestor(late, "refs/heads/master")
    assert report.merged[0].startswith("releases/v1.0.0->releases/v1.1.0")


def test_conflict_pauses_one_direction_dedupes_and_resumes(repo: Repo, gh: FakeGitHub) -> None:
    _bootstrap(repo)
    run_cascade(repo.git, gh, trigger="sync")
    src = repo.commit("releases/v1.0.1", "README.md", "patch line\n", "BOT-91: patch")
    tgt = repo.commit("releases/v1.1.0", "README.md", "regular line\n", "BOT-92: regular")
    gh.commit_prs[src] = [PullRequest(11, "BOT-91: patch", "releases/v1.0.1", "b", src, "a")]
    gh.commit_prs[tgt] = [PullRequest(12, "BOT-92: regular", "releases/v1.1.0", "c", tgt, "a")]

    report = run_cascade(repo.git, gh, trigger="PR #11")

    hop = Hop(V("v1.0.1"), V("v1.1.0"))
    assert report.paused == [hop.key]
    # The other direction (v1.1.0 -> master) still ran.
    assert repo.ancestor(tgt, "refs/heads/master")
    assert len(gh.issues) == 1
    issue = next(iter(gh.issues.values()))
    assert hop.marker in issue.body
    for expected in ("README.md", src, tgt, "#11", "#12", "BOT-91", "BOT-92", FORWARD_LABEL):
        assert expected in issue.body
    assert len(gh.notified) == 1
    assert gh.notified[0]["conflict_files"] == ["README.md"]

    # Repeated / concurrent triggers: no second issue, no second notification.
    run_cascade(repo.git, gh, trigger="again")
    run_cascade(repo.git, gh, trigger="schedule")
    assert len(gh.issues) == 1
    assert len(gh.notified) == 1

    # Resolution lands as a merge on the target (here pushed directly), then resume.
    repo.git.run("checkout", "-q", "--detach", repo.sha("refs/heads/releases/v1.1.0"))
    repo.git.run("merge", "-q", "--no-ff", src, check=False)
    (repo.git.workdir / "README.md").write_text("resolved\n")
    repo.git.run("add", "README.md")
    repo.git.run("commit", "-q", "--no-edit")
    repo.git.run("push", "-q", "origin", "HEAD:refs/heads/releases/v1.1.0")

    resumed = run_cascade(repo.git, gh, trigger="resolved")
    assert resumed.resolved == [hop.key]
    assert not gh.issues
    assert len(gh.closed) == 1
    assert repo.ancestor(src, "refs/heads/master")
    assert repo.show("refs/heads/master", "README.md") == "resolved"


def test_conflict_issue_of_a_rerouted_direction_is_closed_once_resolved(
    repo: Repo, gh: FakeGitHub
) -> None:
    """BOT-40 drill: the source is sealed and a newer patch release now carries its
    changes, so the conflict continues as a new direction. The issue of the old
    direction must be closed, not left open forever."""
    _bootstrap(repo)
    run_cascade(repo.git, gh, trigger="sync")
    src = repo.commit("releases/v1.0.1", "README.md", "patch line\n")
    repo.commit("releases/v1.1.0", "README.md", "regular line\n")
    run_cascade(repo.git, gh, trigger="conflict")
    old = Hop(V("v1.0.1"), V("v1.1.0"))
    assert [old.marker in i.body for i in gh.issues.values()] == [True]

    # v1.0.1 is sealed and tagged; v1.0.2 is cut from that tag and takes over.
    gh.sealed.add("releases/v1.0.1")
    repo.push(src, "refs/tags/v1.0.1")
    create_release(repo.git, V("v1.0.2"))
    rerouted = run_cascade(repo.git, gh, trigger="rerouted")
    new = Hop(V("v1.0.2"), V("v1.1.0"))
    # The old direction is handed over: its changes now travel with v1.0.2,
    # whose own conflict gets a fresh issue.
    assert rerouted.resolved == [old.key]
    assert rerouted.paused == [new.key]
    assert [new.marker in i.body for i in gh.issues.values()] == [True]

    repo.git.run("checkout", "-q", "--detach", repo.sha("refs/heads/releases/v1.1.0"))
    repo.git.run("merge", "-q", "--no-ff", src, check=False)
    (repo.git.workdir / "README.md").write_text("resolved\n")
    repo.git.run("add", "README.md")
    repo.git.run("commit", "-q", "--no-edit")
    repo.git.run("push", "-q", "origin", "HEAD:refs/heads/releases/v1.1.0")

    resumed = run_cascade(repo.git, gh, trigger="resolved")
    assert resumed.resolved == [new.key]
    assert not gh.issues
    assert len(gh.closed) == 2


def test_rejected_push_replans(repo: Repo, gh: FakeGitHub) -> None:
    """Concurrent update of the target between fetch and push: re-plan, no lost commits."""
    _bootstrap(repo)
    run_cascade(repo.git, gh, trigger="sync")
    fix = repo.commit("releases/v1.0.1", "fix.txt", "fix\n")
    moved: list[str] = []

    class RacingGit(Git):
        def push_ref(self, sha: str, ref: str) -> bool:
            if not moved and ref == "refs/heads/releases/v1.1.0":
                moved.append(repo.commit("releases/v1.1.0", "other.txt", "concurrent\n"))
            return super().push_ref(sha, ref)

    racing = RacingGit(repo.git.workdir)
    report = run_cascade(racing, gh, trigger="race")

    assert moved
    for ref in ("refs/heads/releases/v1.1.0", "refs/heads/master"):
        assert repo.ancestor(fix, ref)
        assert repo.ancestor(moved[0], ref) or ref.endswith("v1.1.0")
    assert repo.ancestor(moved[0], "refs/heads/master")
    assert report.merged


def test_land_forward_pr_gates(repo: Repo, gh: FakeGitHub) -> None:
    _bootstrap(repo)
    run_cascade(repo.git, gh, trigger="sync")
    src = repo.commit("releases/v1.0.1", "README.md", "patch line\n")
    repo.commit("releases/v1.1.0", "README.md", "regular line\n")
    run_cascade(repo.git, gh, trigger="conflict")
    assert any(CONFLICT_LABEL for _ in gh.issues)

    # Resolver branch: target + source with the conflict resolved.
    dev = repo.git
    dev.run("checkout", "-q", "--detach", repo.sha("refs/heads/releases/v1.1.0"))
    dev.run("merge", "-q", "--no-ff", src, check=False)
    (dev.workdir / "README.md").write_text("resolved\n")
    dev.run("add", "README.md")
    dev.run("commit", "-q", "--no-edit")
    head = dev.out("rev-parse", "HEAD")
    dev.run("push", "-q", "origin", f"{head}:refs/heads/forward/fix")
    dev.run("push", "-q", "origin", f"{head}:refs/pull/7/head")
    pr = PullRequest(7, "BOT-93: 解决上合冲突", "releases/v1.1.0", "forward/fix", head, "AkkiaS7")
    gh.prs[7] = pr

    with pytest.raises(ReleaseError, match="标签"):
        land_forward_pr(repo.git, gh, 7)
    pr.labels.add(FORWARD_LABEL)
    with pytest.raises(ReleaseError, match="准出"):
        land_forward_pr(repo.git, gh, 7)  # nobody approved
    # Some other account approving is not the Reviewer's release.
    gh.reviews[7] = [("someone", "APPROVED", head, "2026-11-15T10:00:00Z")]
    with pytest.raises(ReleaseError, match="准出"):
        land_forward_pr(repo.git, gh, 7)
    # Reviewer approved an older head: not enough.
    gh.reviews[7].append((REVIEWER_LOGIN, "APPROVED", "0" * 40, "2026-11-15T10:01:00Z"))
    with pytest.raises(ReleaseError, match="准出"):
        land_forward_pr(repo.git, gh, 7)
    gh.reviews[7].append((REVIEWER_LOGIN, "APPROVED", head, "2026-11-15T10:02:00Z"))
    # Approved, but a review thread is still open.
    gh.unresolved[7] = 1
    with pytest.raises(ReleaseError, match="未解决的评审线程"):
        land_forward_pr(repo.git, gh, 7)
    gh.unresolved[7] = 0
    # Approved, but Copilot commented afterwards and nobody re-reviewed.
    gh.pr_comments[7] = [("Copilot", "2026-11-15T10:05:00Z"), ("AkkiaS7", "2026-11-15T10:06:00Z")]
    with pytest.raises(ReleaseError, match="Copilot"):
        land_forward_pr(repo.git, gh, 7)
    gh.reviews[7].append((REVIEWER_LOGIN, "APPROVED", head, "2026-11-15T10:07:00Z"))
    # Someone still requests changes.
    gh.reviews[7].append(("someone", "CHANGES_REQUESTED", head, "2026-11-15T10:08:00Z"))
    with pytest.raises(ReleaseError, match="Request changes"):
        land_forward_pr(repo.git, gh, 7)
    gh.reviews[7].append(("someone", "APPROVED", head, "2026-11-15T10:09:00Z"))
    with pytest.raises(ReleaseError, match="ci-ok"):
        land_forward_pr(repo.git, gh, 7)
    gh.green.add(head)
    # Head pushed while the gates were evaluated: nothing lands.
    before = repo.sha("refs/heads/releases/v1.1.0")
    gh.head_changes[7] = "f" * 40
    with pytest.raises(ReleaseError, match="发生变化"):
        land_forward_pr(repo.git, gh, 7)
    assert repo.sha("refs/heads/releases/v1.1.0") == before
    del gh.head_changes[7]
    pr.head_sha = head

    assert land_forward_pr(repo.git, gh, 7) == head
    assert repo.sha("refs/heads/releases/v1.1.0") == head  # fast-forward, merge kept
    report = run_cascade(repo.git, gh, trigger="landed")
    assert report.resolved
    assert not gh.issues
    assert repo.ancestor(src, "refs/heads/master")


def test_land_forward_pr_refuses_sealed_target(repo: Repo, gh: FakeGitHub) -> None:
    _bootstrap(repo)
    gh.sealed.add("releases/v1.1.0")
    gh.prs[8] = PullRequest(
        8, "x", "releases/v1.1.0", "forward/x", repo.sha("refs/heads/master"), "a", {FORWARD_LABEL}
    )
    with pytest.raises(ReleaseError, match="封版"):
        land_forward_pr(repo.git, gh, 8)


def test_conflict_not_duplicated_when_issue_listing_lags(repo: Repo, gh: FakeGitHub) -> None:
    """GitHub's issue listing is eventually consistent: a run that just created the
    conflict issue must not create it again when it re-plans (BOT-40 drill)."""
    _bootstrap(repo)
    run_cascade(repo.git, gh, trigger="sync")
    repo.commit("releases/v1.0.1", "README.md", "patch line\n", "BOT-91: patch")
    repo.commit("releases/v1.1.0", "README.md", "regular line\n", "BOT-92: regular")
    gh.find_issue = lambda marker: None  # type: ignore[method-assign]  # listing not caught up

    report = run_cascade(repo.git, gh, trigger="PR #11")

    assert report.paused == [Hop(V("v1.0.1"), V("v1.1.0")).key]
    assert len(gh.issues) == 1
    assert len(gh.notified) == 1
