import base64
import json
import os
import shutil
import stat
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from scripts import reviewer_gh
from scripts.reviewer_gh import (
    API,
    BOT_USER_ID,
    PURPOSES,
    Paths,
    ReviewerGhError,
    check_installation,
    get_token,
    main,
    make_jwt,
    merge_pr,
    revoke_cached,
    urllib_http,
)

REPO = "AkkiaLab/AkkiaBot"
#: Installation permissions of the pre-AKKIA-25 Reviewer App (before the release
#: permissions are added): review and merge work, release does not yet.
REVIEWER_ONLY_PERMISSIONS = {
    "actions": "read",
    "checks": "read",
    "contents": "write",
    "metadata": "read",
    "pull_requests": "write",
    "statuses": "read",
}
HEAD_SHA = "a" * 40
MERGE_SHA = "d" * 40  # the test merge commit GitHub computes for the PR
MOCK_GH_TOKEN = "ghs_fake-token-value"  # mock: 人工构造的不可用测试值，不是凭据

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="需要 openssl")


def _b64decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    app_dir = tmp_path / "app"
    app_dir.mkdir(mode=0o700)
    key = app_dir / "private-key.pem"
    subprocess.run(
        [
            "openssl",
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            str(key),
        ],
        check=True,
        capture_output=True,
    )
    key.chmod(0o600)
    (app_dir / "app.json").write_text(json.dumps({"app_id": 42, "client_id": "Iv-test"}))
    return Paths(app_dir=app_dir, cache_dir=tmp_path / "cache")


class FakeGitHub:
    """Records calls and answers like the GitHub API does for a healthy install."""

    def __init__(self, *, selection: str = "selected", repos: list[str] | None = None) -> None:
        self.calls: list[tuple[str, str, str, dict[str, Any] | None]] = []
        self.minted = 0
        self.permissions = dict(PURPOSES["release"])
        self.selection = selection
        self.repos = repos if repos is not None else [REPO]
        self.events: list[str] = []
        self.slug = "akkialab-agent"
        self.bot_id = BOT_USER_ID
        self.over_grant: dict[str, str] = {}
        self.revoked: list[str] = []
        self.routes: dict[tuple[str, str], Any] = {}
        self.repo_pages = 0

    def __call__(self, method: str, url: str, bearer: str, body: dict[str, Any] | None) -> Any:
        self.calls.append((method, url, bearer, body))
        path = url.removeprefix(API)
        if path.startswith("/repos/") and path.endswith("/installation"):
            return {"id": 7}
        if path == "/app/installations/7/access_tokens":
            self.minted += 1
            wanted = (body or {}).get("permissions")
            return {
                "token": f"{MOCK_GH_TOKEN}-{self.minted}",
                "expires_at": _iso(time.time() + 3600),
                "permissions": {**(wanted or self.permissions), **self.over_grant},
                "repository_selection": "selected",
            }
        if path == "/app":
            return {"slug": self.slug, "events": self.events}
        if path == f"/users/{self.slug}%5Bbot%5D":
            return {"login": f"{self.slug}[bot]", "id": self.bot_id, "type": "Bot"}
        if path == "/app/installations/7":
            return {
                "account": {"login": "AkkiaLab"},
                "repository_selection": self.selection,
                "permissions": self.permissions,
            }
        if path.split("?")[0] == "/installation/repositories":
            # Paginated like GitHub: honour per_page (default 30) and page.
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            per_page, page = int(query.get("per_page", 30)), int(query.get("page", 1))
            self.repo_pages += 1
            chunk = self.repos[(page - 1) * per_page : page * per_page]
            return {
                "total_count": len(self.repos),
                "repositories": [{"full_name": r} for r in chunk],
            }
        if path == "/installation/token" and method == "DELETE":
            self.revoked.append(bearer)
            return None
        for (m, prefix), value in self.routes.items():
            if m == method and path.split("?")[0] == prefix:
                return value(body) if callable(value) else value
        raise AssertionError(f"unexpected call {method} {path}")


# ---------------------------------------------------------------- JWT


def test_jwt_is_signed_by_the_app_key(paths: Paths, tmp_path: Path) -> None:
    now = int(time.time())
    jwt = make_jwt("Iv-test", paths.private_key, now)
    header, payload, signature = jwt.split(".")
    assert json.loads(_b64decode(header)) == {"alg": "RS256", "typ": "JWT"}
    claims = json.loads(_b64decode(payload))
    assert claims == {"iat": now - 60, "exp": now + 540, "iss": "Iv-test"}

    pub = tmp_path / "pub.pem"
    sig = tmp_path / "sig.bin"
    subprocess.run(
        ["openssl", "pkey", "-in", str(paths.private_key), "-pubout", "-out", str(pub)],
        check=True,
        capture_output=True,
    )
    sig.write_bytes(_b64decode(signature))
    verify = subprocess.run(
        ["openssl", "dgst", "-sha256", "-verify", str(pub), "-signature", str(sig)],
        input=f"{header}.{payload}".encode(),
        capture_output=True,
        check=False,
    )
    assert verify.returncode == 0, verify.stderr


def test_rejects_world_readable_key(paths: Paths) -> None:
    paths.private_key.chmod(0o644)
    with pytest.raises(ReviewerGhError, match="chmod 600"):
        make_jwt("Iv-test", paths.private_key, int(time.time()))


def test_missing_key_is_reported_without_traceback(paths: Paths) -> None:
    paths.private_key.unlink()
    with pytest.raises(ReviewerGhError, match="找不到 App 私钥"):
        make_jwt("Iv-test", paths.private_key, int(time.time()))


def test_issuer_falls_back_to_app_id(paths: Paths) -> None:
    paths.app_json.write_text(json.dumps({"app_id": 42}))
    assert reviewer_gh.load_app_issuer(paths) == "42"


# ---------------------------------------------------------------- token cache


def test_mints_token_scoped_to_repo_and_caches_privately(paths: Paths) -> None:
    gh = FakeGitHub()
    record, from_cache = get_token(paths, REPO, gh)
    assert not from_cache
    assert record["token"] == f"{MOCK_GH_TOKEN}-1"
    mint = next(c for c in gh.calls if c[0] == "POST")
    # Review tokens are restricted to the repo AND to the review permissions:
    # no Contents write, so they cannot merge or push.
    assert mint[3] == {"repositories": ["AkkiaBot"], "permissions": dict(PURPOSES["review"])}
    assert PURPOSES["review"]["contents"] == "read"
    assert "administration" not in PURPOSES["review"]
    assert record["purpose"] == "review"
    # The JWT, not a token, authenticates the installation lookup and mint.
    assert all(c[2].count(".") == 2 for c in gh.calls)
    assert stat.S_IMODE(paths.token_cache.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.cache_dir.stat().st_mode) == 0o700


def test_reuses_fresh_cached_token(paths: Paths) -> None:
    gh = FakeGitHub()
    get_token(paths, REPO, gh)
    record, from_cache = get_token(paths, REPO, gh)
    assert from_cache
    assert record["token"] == f"{MOCK_GH_TOKEN}-1"
    assert gh.minted == 1


@pytest.mark.parametrize("remaining", [-10, 0, reviewer_gh.MIN_REMAINING - 1])
def test_refreshes_token_close_to_expiry(paths: Paths, remaining: int) -> None:
    gh = FakeGitHub()
    get_token(paths, REPO, gh)
    cached = json.loads(paths.token_cache.read_text())
    cached["expires_at"] = _iso(time.time() + remaining)
    paths.token_cache.write_text(json.dumps(cached))

    record, from_cache = get_token(paths, REPO, gh)
    assert not from_cache
    assert record["token"] == f"{MOCK_GH_TOKEN}-2"


def test_refresh_flag_and_corrupt_or_foreign_cache_mint_new_token(paths: Paths) -> None:
    gh = FakeGitHub()
    get_token(paths, REPO, gh)
    assert not get_token(paths, REPO, gh, refresh=True)[1]
    paths.token_cache.write_text("{not json")
    assert not get_token(paths, REPO, gh)[1]
    cached = json.loads(paths.token_cache.read_text())
    cached["repo"] = "someone/else"
    paths.token_cache.write_text(json.dumps(cached))
    assert not get_token(paths, REPO, gh)[1]
    assert gh.minted == 4


def test_revoke_deletes_token_and_cache(paths: Paths) -> None:
    gh = FakeGitHub()
    get_token(paths, REPO, gh)
    assert revoke_cached(paths, gh)
    assert gh.calls[-1][:3] == ("DELETE", f"{API}/installation/token", f"{MOCK_GH_TOKEN}-1")
    assert not paths.token_cache.exists()
    assert not revoke_cached(paths, gh)


# ---------------------------------------------------------------- check


def test_check_passes_and_never_prints_tokens(
    paths: Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub()
    assert check_installation(paths, REPO, gh, refresh=False) == []
    out = capsys.readouterr()
    assert f"akkialab-agent[bot]，bot 用户 ID {BOT_USER_ID}" in out.out
    assert MOCK_GH_TOKEN not in out.out + out.err
    # The unrestricted probe token is revoked right after use.
    assert gh.revoked == [f"{MOCK_GH_TOKEN}-2"]


@pytest.mark.parametrize(
    ("tweak", "fragment"),
    [
        (lambda g: g.repos.remove(REPO), "不包含目标仓库"),
        (lambda g: g.permissions.update(contents="read"), "merge 用途"),
        (lambda g: g.permissions.update(pull_requests="read"), "review 用途"),
        (lambda g: g.permissions.update(secrets="write"), "多余权限"),
        (lambda g: g.permissions.update(checks="write"), "多余权限"),
        (lambda g: g.events.append("push"), "webhook"),
        (lambda g: setattr(g, "bot_id", 1), "用户 ID"),
    ],
)
def test_check_reports_misconfiguration(paths: Paths, tweak: Any, fragment: str) -> None:
    gh = FakeGitHub()
    tweak(gh)
    problems = check_installation(paths, REPO, gh, refresh=False)
    assert any(fragment in p for p in problems), problems


def test_check_reports_authorized_repo_not_installed(
    paths: Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(repos=[REPO])
    assert check_installation(paths, REPO, gh, refresh=False) == []
    out = capsys.readouterr().out
    assert "已授权但未安装:   AkkiaLab/akkiabot-release-drill" in out


def test_org_wide_installation_passes_without_naming_other_repos(
    paths: Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    """AKKIA-25: the workspace App is installed on all repositories. Other repos
    (possibly private) are counted, never named, and are not a failure."""
    secret_names = ["AkkiaLab/mock-private-one", "AkkiaLab/mock-private-two"]
    gh = FakeGitHub(selection="all", repos=[REPO, "AkkiaLab/akkiabot-release-drill", *secret_names])
    assert check_installation(paths, REPO, gh, refresh=False, require_release=True) == []
    out = capsys.readouterr()
    assert "其他仓库 2 个（不列名称）" in out.out
    for name in secret_names:
        assert name.split("/")[1] not in out.out + out.err
    # Tokens stay restricted to the single target repository.
    mint = next(c for c in gh.calls if c[1].endswith("/access_tokens"))
    assert (mint[3] or {})["repositories"] == ["AkkiaBot"]


def test_check_reads_every_page_of_an_org_wide_installation(
    paths: Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    """Target repo on a later page: still found, others counted, none named."""
    others = [f"AkkiaLab/mock-private-{i:03d}" for i in range(230)]
    gh = FakeGitHub(selection="all", repos=[*others, REPO, "AkkiaLab/akkiabot-release-drill"])
    assert check_installation(paths, REPO, gh, refresh=False) == []
    assert gh.repo_pages == 3  # 232 repositories at 100 per page
    out = capsys.readouterr()
    assert "其他仓库 230 个（不列名称）" in out.out
    assert "已授权但未安装" not in out.out
    assert "mock-private" not in out.out + out.err
    # The unrestricted probe token is still revoked after all pages were read.
    assert gh.revoked == [f"{MOCK_GH_TOKEN}-2"]


def test_authorized_repos_are_the_two_public_ones() -> None:
    assert dict(reviewer_gh.AUTHORIZED_REPOS) == {
        "AkkiaLab/AkkiaBot": "BOT-3",
        "AkkiaLab/akkiabot-release-drill": "BOT-40",
    }


def test_check_for_another_authorized_repo_mints_token_for_it(paths: Paths) -> None:
    drill = "AkkiaLab/akkiabot-release-drill"
    gh = FakeGitHub(repos=[REPO, drill])
    assert check_installation(paths, drill, gh, refresh=False) == []
    mint = next(c for c in gh.calls if c[1].endswith("/access_tokens"))
    assert (mint[3] or {})["repositories"] == ["akkiabot-release-drill"]


def test_unauthorized_target_repo_is_refused_before_any_call(paths: Paths) -> None:
    # Installed (org-wide) but not authorised for this project: still refused.
    gh = FakeGitHub(selection="all", repos=[REPO, "AkkiaLab/other"])
    with pytest.raises(ReviewerGhError, match="不在已授权范围"):
        get_token(paths, "AkkiaLab/other", gh)
    with pytest.raises(ReviewerGhError, match="不在已授权范围"):
        check_installation(paths, "AkkiaLab/other", gh, refresh=False)
    assert gh.calls == []


# ---------------------------------------------------------------- CLI


def test_http_refuses_non_github_urls() -> None:
    with pytest.raises(ReviewerGhError, match="拒绝"):
        urllib_http("GET", "https://example.com/x", "jwt", None)


def test_cli_has_no_command_that_prints_the_token(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["token"])
    assert "invalid choice" in capsys.readouterr().err


def test_cli_gh_requires_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["gh"]) == 2
    assert "用法" in capsys.readouterr().err


def test_cli_gh_execs_gh_with_token_in_env(
    paths: Paths, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gh = FakeGitHub()
    fake_gh = tmp_path / "gh"
    fake_gh.write_text("#!/bin/sh\n")
    fake_gh.chmod(0o700)
    monkeypatch.setenv("AKKIABOT_REVIEWER_APP_DIR", str(paths.app_dir))
    monkeypatch.setenv("AKKIABOT_REVIEWER_CACHE_DIR", str(paths.cache_dir))
    monkeypatch.setenv("REVIEWER_GH_BIN", str(fake_gh))
    monkeypatch.setenv("GITHUB_TOKEN", "should-be-dropped")
    monkeypatch.setattr(reviewer_gh, "urllib_http", gh)
    seen: dict[str, Any] = {}

    def fake_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
        seen.update(path=path, argv=argv, env=env)

    monkeypatch.setattr(os, "execve", fake_execve)
    assert main(["gh", "pr", "review", "1", "--approve"]) == 0
    assert seen["argv"] == [str(fake_gh), "pr", "review", "1", "--approve"]
    assert seen["env"]["GH_TOKEN"] == f"{MOCK_GH_TOKEN}-1"
    assert "GITHUB_TOKEN" not in seen["env"]


def test_unwritable_cache_degrades_to_minting_each_time(
    paths: Paths, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    readonly = Paths(app_dir=paths.app_dir, cache_dir=blocker / "cache")
    gh = FakeGitHub()
    assert get_token(readonly, REPO, gh)[0]["token"] == f"{MOCK_GH_TOKEN}-1"
    assert get_token(readonly, REPO, gh)[0]["token"] == f"{MOCK_GH_TOKEN}-2"
    err = capsys.readouterr().err
    assert "无法写入 token 缓存" in err
    assert MOCK_GH_TOKEN not in err


# ---------------------------------------------------------------- purposes (AKKIA-25)


def test_check_before_release_permissions_passes_unless_release_is_required(
    paths: Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    """Between merging AKKIA-25 and the owner adding the release permissions."""
    gh = FakeGitHub()
    gh.permissions = dict(REVIEWER_ONLY_PERMISSIONS)
    gh.slug = "akkiabot-reviewer"  # not renamed yet: same bot user ID
    assert check_installation(paths, REPO, gh, refresh=False) == []
    assert "用途 release:" in capsys.readouterr().out
    problems = check_installation(paths, REPO, gh, refresh=False, require_release=True)
    assert problems == [
        "权限不足以签发 release 用途的 token：缺少 actions:write, administration:write, issues:write"
    ]


def test_full_shared_app_passes_release_check(paths: Paths) -> None:
    assert check_installation(paths, REPO, FakeGitHub(), refresh=False, require_release=True) == []


def test_token_wider_than_requested_is_revoked_and_refused(paths: Paths) -> None:
    gh = FakeGitHub()
    gh.over_grant = {"administration": "write"}
    with pytest.raises(ReviewerGhError, match="权限超出预期"):
        get_token(paths, REPO, gh)
    assert gh.revoked == [f"{MOCK_GH_TOKEN}-1"]
    assert not paths.token_cache.exists()


def test_legacy_cache_without_purpose_is_not_reused(paths: Paths) -> None:
    gh = FakeGitHub()
    get_token(paths, REPO, gh)
    cached = json.loads(paths.token_cache.read_text())
    del cached["purpose"]  # pre-AKKIA-25 record: full-permission token
    paths.token_cache.write_text(json.dumps(cached))
    assert not get_token(paths, REPO, gh)[1]


@pytest.mark.parametrize(
    "gh_args",
    [
        ["pr", "merge", "12", "-R", REPO, "--squash"],
        ["pr", "merge", "--auto", "12"],
        ["api", "-X", "PUT", f"repos/{REPO}/pulls/12/merge"],
        ["api", "graphql", "-f", "query=mutation{mergePullRequest(input:{}){clientMutationId}}"],
    ],
)
def test_gh_passthrough_refuses_merges(
    gh_args: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["gh", *gh_args]) == 2
    assert "reviewer-gh merge" in capsys.readouterr().err


# ---------------------------------------------------------------- merge gate


def _review(user_id: int, state: str, commit: str = HEAD_SHA, login: str = "") -> dict[str, Any]:
    return {
        "user": {"id": user_id, "login": login or str(user_id)},
        "state": state,
        "commit_id": commit,
    }


class MergeScenario:
    """A PR that satisfies every gate; tests break one thing at a time."""

    def __init__(self) -> None:
        self.pr: dict[str, Any] = {
            "state": "open",
            "draft": False,
            "user": {"id": 68485070, "login": "AkkiaS7"},
            "head": {"sha": HEAD_SHA, "ref": "akkia-9-x", "repo": {"full_name": REPO}},
            "base": {"ref": "releases/v1.1.0"},
            "mergeable": True,
            "merge_commit_sha": MERGE_SHA,
        }
        self.reviews = [_review(BOT_USER_ID, "APPROVED", login="akkialab-agent[bot]")]
        self.threads = [True, True]
        self.required = [
            {"context": "ci-ok", "integration_id": 15368},
            {"context": "secret-scan", "integration_id": 15368},
            {"context": "release-guard"},
        ]
        self.check_runs: list[dict[str, Any]] = [
            {
                "id": i,
                "name": n,
                "status": "completed",
                "conclusion": "success",
                "app": {"id": 15368},
            }
            for i, n in enumerate(["ci-ok", "secret-scan", "release-guard"], start=1)
        ]
        self.statuses: list[dict[str, Any]] = []
        # Results on the test merge commit: usually none (fine), but a red one blocks.
        self.merge_check_runs: list[dict[str, Any]] = []
        self.merge_statuses: list[dict[str, Any]] = []
        # Rules on the head branch (deletion protection is honoured by --delete-branch).
        self.head_rules: list[dict[str, Any]] = []
        self.head_protected = False
        self.delete_error: str | None = None
        self.merged: list[dict[str, Any]] = []
        self.deleted: list[str] = []

    def install(self, gh: FakeGitHub) -> None:
        base = f"/repos/{REPO}"

        def graphql(body: dict[str, Any]) -> dict[str, Any]:
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [{"isResolved": r} for r in self.threads],
                            }
                        }
                    }
                }
            }

        def delete(body: dict[str, Any] | None) -> None:
            if self.delete_error:
                raise ReviewerGhError(self.delete_error)
            self.deleted.append("akkia-9-x")

        def merge(body: dict[str, Any]) -> dict[str, Any]:
            self.merged.append(body)
            return {"merged": True, "sha": "b" * 40}

        gh.routes.update(
            {
                ("GET", f"{base}/pulls/12"): self.pr,
                ("GET", f"{base}/pulls/12/reviews"): self.reviews,
                ("POST", "/graphql"): graphql,
                ("GET", f"{base}/rules/branches/releases/v1.1.0"): [
                    {"type": "pull_request", "parameters": {}},
                    {
                        "type": "required_status_checks",
                        "parameters": {"required_status_checks": self.required},
                    },
                ],
                ("GET", f"{base}/commits/{HEAD_SHA}/check-runs"): {"check_runs": self.check_runs},
                ("GET", f"{base}/commits/{HEAD_SHA}/status"): {"statuses": self.statuses},
                ("GET", f"{base}/commits/{MERGE_SHA}/check-runs"): {
                    "check_runs": self.merge_check_runs
                },
                ("GET", f"{base}/commits/{MERGE_SHA}/status"): {"statuses": self.merge_statuses},
                ("PUT", f"{base}/pulls/12/merge"): merge,
                ("GET", f"{base}/rules/branches/akkia-9-x"): self.head_rules,
                ("GET", f"{base}/branches/akkia-9-x"): lambda _: {"protected": self.head_protected},
                ("DELETE", f"{base}/git/refs/heads/akkia-9-x"): delete,
            }
        )


def _merge(paths: Paths, scenario: MergeScenario, **kwargs: Any) -> tuple[FakeGitHub, Any]:
    gh = FakeGitHub()
    scenario.install(gh)
    try:
        return gh, merge_pr(paths, REPO, 12, gh, **kwargs)
    except ReviewerGhError as exc:
        return gh, exc


def test_merge_passes_gate_pins_head_and_revokes_token(paths: Paths) -> None:
    scenario = MergeScenario()
    gh, result = _merge(paths, scenario, delete_branch=True)
    assert result.sha == "b" * 40
    assert not result.cleanup_failed
    assert scenario.deleted == ["akkia-9-x"]
    assert scenario.merged == [{"merge_method": "squash", "sha": HEAD_SHA}]
    mint = next(c for c in gh.calls if c[1].endswith("/access_tokens"))
    assert (mint[3] or {})["permissions"] == dict(PURPOSES["merge"])
    # The merge token is never cached and is revoked at the end.
    assert not paths.token_cache.exists()
    assert gh.revoked == [f"{MOCK_GH_TOKEN}-1"]


def _fail_run(s: MergeScenario) -> None:
    s.check_runs[0]["conclusion"] = "failure"


def _rerun_failed(s: MergeScenario) -> None:
    # An older green run does not hide a newer red one.
    s.check_runs.append({**s.check_runs[0], "id": 99, "conclusion": "failure"})


def _pending(s: MergeScenario) -> None:
    s.check_runs[1].update(status="in_progress", conclusion=None)


def _wrong_app(s: MergeScenario) -> None:
    s.check_runs[0]["app"] = {"id": 1}  # same name from another App does not count


def _status_red_run_green(s: MergeScenario) -> None:
    # Same name as a green check run, but a red commit status: both must pass.
    s.statuses.append({"context": "ci-ok", "state": "failure"})


def _status_green_run_red(s: MergeScenario) -> None:
    s.check_runs[2]["conclusion"] = "failure"
    s.statuses.append({"context": "release-guard", "state": "success"})


def _other_app_red(s: MergeScenario) -> None:
    # A red same-name run from another App is not hidden by the required App's green run.
    s.check_runs.append({**s.check_runs[0], "id": 50, "app": {"id": 1}, "conclusion": "failure"})


def _merge_commit_red(s: MergeScenario) -> None:
    # Head is green, but GitHub's test merge commit failed the check.
    s.merge_check_runs.append({**s.check_runs[1], "id": 70, "conclusion": "failure"})


def _merge_commit_status_red(s: MergeScenario) -> None:
    s.merge_statuses.append({"context": "release-guard", "state": "error"})


@pytest.mark.parametrize(
    ("breaker", "fragment"),
    [
        (_fail_run, "必需检查 ci-ok 未通过"),
        (_rerun_failed, "必需检查 ci-ok 未通过"),
        (_pending, "必需检查 secret-scan 未通过"),
        (_wrong_app, "必需检查 ci-ok 未通过"),
        (_status_red_run_green, "必需检查 ci-ok 未通过（completed/success, status/failure）"),
        (_status_green_run_red, "必需检查 release-guard 未通过"),
        (_other_app_red, "必需检查 ci-ok 未通过"),
        (_merge_commit_red, f"测试合并提交 {MERGE_SHA[:12]} 上必需检查 secret-scan 未通过"),
        (_merge_commit_status_red, "测试合并提交"),
        (lambda s: s.pr.update(mergeable=None), "mergeable 未知"),
        (lambda s: s.pr.update(merge_commit_sha=None), "mergeable 未知"),
        (lambda s: s.pr.update(mergeable=False), "有冲突"),
        (lambda s: s.check_runs.pop(), "必需检查 release-guard 未通过"),
        (lambda s: s.required.clear(), "没有配置必需检查"),
        (lambda s: s.threads.append(False), "1 个未解决的评审线程"),
        (lambda s: s.reviews.clear(), "尚未在当前 head"),
        (lambda s: s.reviews[0].update(commit_id="c" * 40), "尚未在当前 head"),
        (lambda s: s.reviews.append(_review(BOT_USER_ID, "DISMISSED")), "尚未在当前 head"),
        # An approval by another account (even one named like the App) does not count.
        (
            lambda s: s.reviews.__setitem__(0, _review(1, "APPROVED", login="akkialab-agent[bot]")),
            "尚未在当前 head",
        ),
        (
            lambda s: s.reviews.insert(0, _review(175728472, "CHANGES_REQUESTED", login="copilot")),
            "Request changes：copilot",
        ),
        (lambda s: s.pr.update(state="closed"), "不是打开状态"),
        (lambda s: s.pr.update(draft=True), "草稿"),
        (lambda s: s.pr.update(labels=[{"name": "release-forward"}]), "快进落地"),
    ],
)
def test_merge_gate_refuses(paths: Paths, breaker: Any, fragment: str) -> None:
    scenario = MergeScenario()
    breaker(scenario)
    gh, result = _merge(paths, scenario)
    assert isinstance(result, ReviewerGhError)
    assert fragment in str(result)
    assert "未合并" in str(result)
    assert scenario.merged == []
    assert gh.revoked == [f"{MOCK_GH_TOKEN}-1"]


def test_merge_accepts_commit_status_and_skipped_runs(paths: Paths) -> None:
    scenario = MergeScenario()
    scenario.check_runs.pop()  # release-guard reported as a commit status instead
    scenario.statuses.append({"context": "release-guard", "state": "success"})
    scenario.check_runs[1]["conclusion"] = "skipped"
    _, result = _merge(paths, scenario)
    assert result.sha == "b" * 40


def test_merge_is_squash_only() -> None:
    """The rulesets allow squash only and the App bypasses them: the tool enforces it."""
    with pytest.raises(TypeError):
        merge_pr(Paths(Path("x"), Path("y")), REPO, 1, FakeGitHub(), method="merge")  # type: ignore[call-arg]
    for method in ("merge", "rebase", "squash"):
        with pytest.raises(SystemExit):
            main(["merge", "12", "--method", method])


@pytest.mark.parametrize(
    "protect",
    [
        lambda s: s.head_rules.append({"type": "deletion", "ruleset_id": 1}),
        lambda s: setattr(s, "head_protected", True),
    ],
)
def test_delete_branch_skips_protected_head(paths: Paths, protect: Any) -> None:
    scenario = MergeScenario()
    protect(scenario)
    _, result = _merge(paths, scenario, delete_branch=True)
    assert result.sha == "b" * 40
    assert "保护，未删除" in result.cleanup
    assert not result.cleanup_failed
    assert scenario.deleted == []


def test_cleanup_failure_is_reported_separately_from_the_merge(
    paths: Paths, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scenario = MergeScenario()
    scenario.delete_error = "GitHub API DELETE 返回 422：Reference does not exist"
    _, result = _merge(paths, scenario, delete_branch=True)
    assert result.sha == "b" * 40
    assert result.cleanup_failed
    assert "删除失败" in result.cleanup

    # CLI: merged is reported on stdout, cleanup failure separately, exit code 3.
    monkeypatch.setattr(reviewer_gh, "merge_pr", lambda *a, **k: result)
    assert main(["merge", "12", "--delete-branch"]) == 3
    out = capsys.readouterr()
    assert "已合并 PR #12" in out.out
    assert "清理未完成" in out.err


def test_no_delete_without_flag(paths: Paths) -> None:
    scenario = MergeScenario()
    gh, result = _merge(paths, scenario)
    assert result.cleanup == ""
    assert scenario.deleted == []
    assert not any("/branches/akkia-9-x" in c[1] for c in gh.calls)


def test_merge_refuses_unauthorized_repo_before_any_call(paths: Paths) -> None:
    gh = FakeGitHub()
    with pytest.raises(ReviewerGhError, match="不在已授权范围"):
        merge_pr(paths, "AkkiaLab/other", 1, gh)
    assert gh.calls == []
