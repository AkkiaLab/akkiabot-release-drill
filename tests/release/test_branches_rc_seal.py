"""Branch creation, PR guard, 下车, RC, baseline and sealing against a real git origin."""

from datetime import UTC, datetime

import pytest

from scripts.release.branches import create_release, defer_features, guard_pr
from scripts.release.github import FIX_LABEL, FORWARD_LABEL, PullRequest
from scripts.release.rc import check_rc, create_rc, select_baseline
from scripts.release.seal import seal, verify_formal
from scripts.release.versions import ReleaseError, Version
from tests.release.conftest import FakeGitHub, Repo, sh

V = Version.parse
FROZEN = datetime(2026, 11, 19, 10, 30, tzinfo=UTC)  # Thursday 18:30 UTC+8
OPEN = datetime(2026, 11, 18, 10, 30, tzinfo=UTC)  # Wednesday


def _pr(
    repo: Repo, number: int, base: str, head: str, labels: set[str] | None = None, ref: str = ""
) -> PullRequest:
    return PullRequest(
        number,
        f"AKKIA-{number}: x",
        base,
        ref or f"akkia-{number}",
        head,
        "AkkiaS7",
        labels or set(),
    )


def test_create_release_sources_and_idempotence(repo: Repo) -> None:
    master = repo.sha("refs/heads/master")
    assert create_release(repo.git, V("v1.0.0")) == ("master", master)
    assert create_release(repo.git, V("v1.0.0")) == ("master", master)  # rerun: no-op
    with pytest.raises(ReleaseError, match=r"v1\.0\.0"):
        create_release(repo.git, V("v1.0.1"))  # no formal tag yet
    repo.commit("releases/v1.0.0", "a.txt", "a\n")
    tag_sha = repo.sha("refs/heads/releases/v1.0.0")
    repo.push(tag_sha, "refs/tags/v1.0.0")
    repo.commit("master", "m.txt", "m\n")
    assert create_release(repo.git, V("v1.0.1")) == ("v1.0.0", tag_sha)
    assert create_release(repo.git, V("v1.1.0"))[1] == repo.sha("refs/heads/master")
    # Rerun after master moved on: the existing regular branch is kept as cut.
    assert create_release(repo.git, V("v1.0.0")) == ("master", tag_sha)
    repo.push(tag_sha, "refs/tags/v2.0.0")  # a version that only exists as a tag
    with pytest.raises(ReleaseError, match="复用"):
        create_release(repo.git, V("v2.0.0"))


def test_guard_rules(repo: Repo, gh: FakeGitHub) -> None:
    master_head = repo.sha("refs/heads/master")
    early = _pr(repo, 1, "master", master_head)
    assert guard_pr(repo.git, gh, early, OPEN).ok  # not bootstrapped yet: master PRs still fine

    create_release(repo.git, V("v1.0.0"))
    repo.push(repo.sha("refs/heads/releases/v1.0.0"), "refs/tags/v1.0.0")
    repo.commit("master", "later.txt", "later\n")
    create_release(repo.git, V("v1.1.0"))
    create_release(repo.git, V("v1.0.1"))
    gh.sealed.add("releases/v1.0.0")

    result = guard_pr(repo.git, gh, _pr(repo, 2, "master", master_head), OPEN)
    assert not result.ok
    assert "releases/v1.1.0" in result.messages[0]
    assert guard_pr(repo.git, gh, _pr(repo, 3, "master", master_head, {FORWARD_LABEL}), OPEN).ok

    sealed = guard_pr(
        repo.git, gh, _pr(repo, 4, "releases/v1.0.0", master_head, ref="master"), OPEN
    )
    assert not sealed.ok
    assert "封版" in sealed.messages[0]

    sh(repo.dev, "fetch", "-q", "origin")
    sh(repo.dev, "checkout", "-q", "-B", "akkia-5", "origin/releases/v1.0.1")
    (repo.dev / "fix.txt").write_text("fix\n")
    sh(repo.dev, "add", "fix.txt")
    sh(repo.dev, "commit", "-q", "-m", "fix")
    sh(repo.dev, "push", "-q", "origin", "HEAD:refs/heads/akkia-5")
    head5 = sh(repo.dev, "rev-parse", "HEAD")
    assert guard_pr(repo.git, gh, _pr(repo, 5, "releases/v1.0.1", head5), FROZEN).ok

    # Branch cut from master but aimed at the patch release: wrong base.
    sh(repo.dev, "checkout", "-q", "-B", "akkia-6", "origin/master")
    (repo.dev / "feat.txt").write_text("feat\n")
    sh(repo.dev, "add", "feat.txt")
    sh(repo.dev, "commit", "-q", "-m", "feat")
    sh(repo.dev, "push", "-q", "origin", "HEAD:refs/heads/akkia-6")
    head6 = sh(repo.dev, "rev-parse", "HEAD")
    wrong = guard_pr(repo.git, gh, _pr(repo, 6, "releases/v1.0.1", head6), OPEN)
    assert not wrong.ok
    assert "master" in wrong.messages[0]

    # Feature freeze on the current regular release (v1.0.1 is a patch; seal it to make v1.1.0 current).
    gh.sealed.add("releases/v1.0.1")
    sh(repo.dev, "checkout", "-q", "-B", "akkia-7", "origin/releases/v1.1.0")
    (repo.dev / "f7.txt").write_text("f7\n")
    sh(repo.dev, "add", "f7.txt")
    sh(repo.dev, "commit", "-q", "-m", "f7")
    sh(repo.dev, "push", "-q", "origin", "HEAD:refs/heads/akkia-7")
    head7 = sh(repo.dev, "rev-parse", "HEAD")
    assert guard_pr(repo.git, gh, _pr(repo, 7, "releases/v1.1.0", head7), OPEN).ok
    frozen = guard_pr(repo.git, gh, _pr(repo, 7, "releases/v1.1.0", head7), FROZEN)
    assert not frozen.ok
    assert "冻结" in frozen.messages[0]
    assert guard_pr(repo.git, gh, _pr(repo, 7, "releases/v1.1.0", head7, {FIX_LABEL}), FROZEN).ok


def test_defer_features_moves_only_features(repo: Repo, gh: FakeGitHub) -> None:
    create_release(repo.git, V("v1.0.0"))
    head = repo.sha("refs/heads/master")
    gh.prs = {
        1: _pr(repo, 1, "releases/v1.0.0", head),
        2: _pr(repo, 2, "releases/v1.0.0", head, {FIX_LABEL}),
        3: _pr(repo, 3, "releases/v1.0.0", head, {FORWARD_LABEL}),
    }
    assert defer_features(repo.git, gh, OPEN) == []  # not frozen: nothing moves
    moved = defer_features(repo.git, gh, FROZEN)
    assert moved == [(1, "releases/v1.1.0")]
    assert repo.sha("refs/heads/releases/v1.1.0") == head  # next release created from master
    assert gh.prs[1].base == "releases/v1.1.0"
    assert gh.prs[2].base == gh.prs[3].base == "releases/v1.0.0"
    assert defer_features(repo.git, gh, FROZEN) == []  # repeated freeze run: idempotent


def test_rc_lifecycle_and_staleness(repo: Repo, gh: FakeGitHub) -> None:
    create_release(repo.git, V("v1.0.0"))
    rc1, created = create_rc(repo.git, gh, V("v1.0.0"))
    assert created
    assert rc1.tag == "v1.0.0-rc.1"
    again, created_again = create_rc(repo.git, gh, V("v1.0.0"))
    assert (again, created_again) == (rc1, False)  # same head: reuse, no new tag
    check_rc(repo.git.state(), rc1.tag, gh.sealed)

    repo.commit("releases/v1.0.0", "fix.txt", "fix\n")
    with pytest.raises(ReleaseError, match="已过期"):
        check_rc(repo.git.state(), rc1.tag, gh.sealed)
    rc2, _ = create_rc(repo.git, gh, V("v1.0.0"))
    assert rc2.tag == "v1.0.0-rc.2"
    assert repo.sha("refs/tags/v1.0.0-rc.1") == rc1.sha  # old RC never moves
    with pytest.raises(ReleaseError, match=r"rc\.2 更新"):
        check_rc(repo.git.state(), rc1.tag, gh.sealed)
    gh.sealed.add("releases/v1.0.0")
    with pytest.raises(ReleaseError, match="封版"):
        create_rc(repo.git, gh, V("v1.0.0"))


def test_baseline_follows_the_lineage(repo: Repo, gh: FakeGitHub) -> None:
    legacy = repo.commit("master", "legacy.txt", "accepted on master before the flow\n")
    gh.statuses[legacy] = "success"
    repo.commit("master", "next.txt", "n\n")
    create_release(repo.git, V("v1.0.0"))
    rc1, _ = create_rc(repo.git, gh, V("v1.0.0"))
    assert select_baseline(repo.git, gh, rc1.sha) == legacy  # bootstrap: legacy accepted master sha
    gh.statuses[rc1.sha] = "success"
    repo.push(rc1.sha, "refs/tags/v1.0.0")

    # Regular line moves on and gets accepted.
    repo.commit("master", "m2.txt", "m2\n")
    create_release(repo.git, V("v1.1.0"))
    repo.commit("releases/v1.1.0", "r.txt", "r\n")
    rc11, _ = create_rc(repo.git, gh, V("v1.1.0"))
    gh.statuses[rc11.sha] = "success"
    gh.statuses[repo.sha("refs/heads/master")] = "failure"

    # Patch line from v1.0.0: must diff against v1.0.0, never the newer v1.1.0 acceptance.
    create_release(repo.git, V("v1.0.1"))
    repo.commit("releases/v1.0.1", "p.txt", "p\n")
    rc101, _ = create_rc(repo.git, gh, V("v1.0.1"))
    assert select_baseline(repo.git, gh, rc101.sha) == rc1.sha

    # Next RC on the regular line picks its own newest accepted ancestor.
    repo.commit("releases/v1.1.0", "r2.txt", "r2\n")
    rc11b, _ = create_rc(repo.git, gh, V("v1.1.0"))
    assert select_baseline(repo.git, gh, rc11b.sha) == rc11.sha


def _accepted_rc(repo: Repo, gh: FakeGitHub, version: str = "v1.0.0") -> str:
    create_release(repo.git, V(version))
    repo.commit(f"releases/{version}", "a.txt", "a\n")
    candidate, _ = create_rc(repo.git, gh, V(version))
    gh.statuses[candidate.sha] = "success"
    return candidate.sha


def test_seal_happy_path_and_resume(repo: Repo, gh: FakeGitHub) -> None:
    sha = _accepted_rc(repo, gh)
    result = seal(repo.git, gh, V("v1.0.0"), log=lambda _: None)
    assert result.created
    assert result.sha == sha
    assert repo.sha("refs/tags/v1.0.0") == sha
    assert "releases/v1.0.0" in gh.sealed
    again = seal(repo.git, gh, V("v1.0.0"), log=lambda _: None)  # rerun: idempotent
    assert not again.created
    assert repo.sha("refs/tags/v1.0.0") == sha
    gh.jobs.clear()
    assert verify_formal(repo.git, gh, "v1.0.0") == (V("v1.0.0"), sha)


def test_seal_refuses_unaccepted_or_stale_rc(repo: Repo, gh: FakeGitHub) -> None:
    sha = _accepted_rc(repo, gh)
    gh.statuses[sha] = "failure"
    with pytest.raises(ReleaseError, match="staging-acceptance"):
        seal(repo.git, gh, V("v1.0.0"), log=lambda _: None)
    gh.statuses[sha] = "success"
    repo.commit("releases/v1.0.0", "late.txt", "late\n")
    with pytest.raises(ReleaseError, match="已过期"):
        seal(repo.git, gh, V("v1.0.0"), log=lambda _: None)
    assert not gh.seal_calls  # never locked
    assert "v1.0.0" not in repo.git.state().tags.__repr__()


def test_seal_race_unlocks_and_creates_no_tag(repo: Repo, gh: FakeGitHub) -> None:
    _accepted_rc(repo, gh)
    gh.on_seal = lambda branch: (repo.commit(branch, "race.txt", "slipped in\n"), None)[1]
    with pytest.raises(ReleaseError, match="锁定期间"):
        seal(repo.git, gh, V("v1.0.0"), log=lambda _: None)
    assert "releases/v1.0.0" not in gh.sealed  # lock undone
    assert V("v1.0.0") not in repo.git.state().tags


def test_seal_lock_not_effective_creates_no_tag(repo: Repo, gh: FakeGitHub) -> None:
    _accepted_rc(repo, gh)
    gh.broken_rules = True
    with pytest.raises(ReleaseError, match="未生效"):
        seal(repo.git, gh, V("v1.0.0"), log=lambda _: None)
    assert V("v1.0.0") not in repo.git.state().tags
    gh.broken_rules = False  # fixed (e.g. ruleset repaired): rerun resumes
    assert seal(repo.git, gh, V("v1.0.0"), log=lambda _: None).created


def test_existing_formal_tag_is_never_moved(repo: Repo, gh: FakeGitHub) -> None:
    _accepted_rc(repo, gh)
    repo.push(repo.sha("refs/heads/master"), "refs/tags/v1.0.0")
    with pytest.raises(ReleaseError, match="不会被移动"):
        seal(repo.git, gh, V("v1.0.0"), log=lambda _: None)


def test_verify_formal_rejects_unsealed_or_moved(repo: Repo, gh: FakeGitHub) -> None:
    sha = _accepted_rc(repo, gh)
    repo.push(sha, "refs/tags/v1.0.0")  # tag created outside the seal flow
    with pytest.raises(ReleaseError, match="未封版"):
        verify_formal(repo.git, gh, "v1.0.0")
    gh.sealed.add("releases/v1.0.0")
    assert verify_formal(repo.git, gh, "v1.0.0")[1] == sha
    gh.statuses[sha] = "failure"
    with pytest.raises(ReleaseError, match="staging-acceptance"):
        verify_formal(repo.git, gh, "v1.0.0")
