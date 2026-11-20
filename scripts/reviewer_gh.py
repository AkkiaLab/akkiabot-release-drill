#!/usr/bin/env python3
"""Run ``gh`` as the workspace agent GitHub App ``akkialab-agent``.

``akkialab-agent`` (App ID 5760780, bot user ID 356702926; formerly
``akkiabot-reviewer``) is the one GitHub identity of the workspace's agents
(AKKIA-25). Implementers commit and open PRs as ``AkkiaS7``; the Reviewer uses
this App to approve, resolve threads and merge, and the release flow uses the
same App for its pipeline writes. Because the App is a bypass actor of the
release rulesets, a merge made with it would skip GitHub's own merge gates, so
this script compensates on the tool side:

* every installation token is minted for one *purpose* with only that
  purpose's permissions (``PURPOSES``): ``reviewer-gh gh`` gets a ``review``
  token without Contents write, so it cannot merge, push or touch rulesets;
* ``reviewer-gh merge`` is the only merge path. It mints a short-lived
  ``merge`` token, checks that every required check of the base branch passed
  on the current head, that no review thread is unresolved, that the App's own
  newest review approves that head and that nobody's newest review requests
  changes, then merges pinned to that head SHA and revokes the token.

Token mechanics:

1. sign an RS256 JWT with the App private key (via ``openssl``; the key is
   never loaded into Python and never leaves its file);
2. look up the App installation on the target repository;
3. exchange the JWT for an installation token restricted to that repository
   and to the purpose's permissions;
4. cache ``review`` tokens (0600) until close to expiry; ``merge`` tokens are
   never cached and are revoked right after use.

Only the standard library and ``openssl`` are required, so the installed copy
runs with a bare ``python3`` outside the uv workspace.

Usage::

    reviewer-gh check                 # verify install scope + permissions, no secrets printed
    reviewer-gh gh pr review 12 -R AkkiaLab/AkkiaBot --approve
    reviewer-gh merge 12 --delete-branch
    reviewer-gh revoke                # revoke the cached token and delete the cache

There is deliberately no command that prints the token: agent runtimes capture
stdout into run logs, so the token only ever travels through ``GH_TOKEN`` into
the ``gh`` process.

Files (override with environment variables):

* ``$AKKIABOT_REVIEWER_APP_DIR`` (default ``~/.config/akkiabot/reviewer-app``):
  ``app.json`` (``{"app_id": ..., "client_id": ...}``) and ``private-key.pem``.
* ``$AKKIABOT_REVIEWER_CACHE_DIR`` (default ``~/.cache/akkiabot/reviewer-app``):
  ``token.json``.
* ``$AKKIABOT_REVIEWER_REPO`` (default ``AkkiaLab/AkkiaBot``): the repository
  the token is restricted to. It must be listed in ``AUTHORIZED_REPOS``.

The App is installed on the whole organisation (workspace-shared, AKKIA-25);
``AUTHORIZED_REPOS`` below is the set of repositories *this project's tools*
may mint tokens for, changed only through a reviewed PR with the owner's
approval (docs/reviewer-github-app.md). Only public repositories are listed:
the file is public, so private repository names never go in here.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

API = "https://api.github.com"
DEFAULT_REPO = "AkkiaLab/AkkiaBot"
# Reuse a cached token only while it has at least this many seconds left, so
# a long review never runs with a token that expires half way through.
MIN_REMAINING = 15 * 60
# Repositories this project's tools may mint tokens for, with the issue that
# recorded the decision (``BOT-<n>`` ids belong to the retired Multica workspace
# and are no longer accessible; new entries use ``AKKIA-<n>``). Public
# repositories only (AKKIA-25). The installation itself may reach more of the
# organisation; ``check`` only counts those, never names them, and tokens are
# always restricted to one listed repository.
AUTHORIZED_REPOS: Mapping[str, str] = {
    "AkkiaLab/AkkiaBot": "BOT-3",
    "AkkiaLab/akkiabot-release-drill": "BOT-40",
}
#: Bot user ID of the App (REST ``user.id``, GraphQL ``Bot.databaseId``). It does not
#: change when the App is renamed; ``check`` verifies the current slug maps to it.
BOT_USER_ID = 356702926
#: Installation token permissions per purpose (AKKIA-25). Each token gets exactly
#: one purpose's set, so a review token cannot merge and a merge token cannot touch
#: rulesets or workflows. ``release`` is minted by the release flow itself
#: (``.github/actions/release-setup``, ``scripts/release/as-app.sh``), listed here so
#: ``check`` can tell whether the installation can serve it.
PURPOSES: Mapping[str, Mapping[str, str]] = {
    "review": {
        "actions": "read",
        "checks": "read",
        "contents": "read",
        "metadata": "read",
        "pull_requests": "write",
        "statuses": "read",
    },
    "merge": {
        "checks": "read",
        "contents": "write",
        "metadata": "read",
        "pull_requests": "write",
        "statuses": "read",
    },
    "release": {
        "actions": "write",
        "administration": "write",
        "checks": "read",
        "contents": "write",
        "issues": "write",
        "metadata": "read",
        "pull_requests": "write",
        "statuses": "read",
    },
}
_LEVEL = {"read": 1, "write": 2}


def _union(*sets: Mapping[str, str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for perms in sets:
        for name, level in perms.items():
            if _LEVEL[level] > _LEVEL.get(merged.get(name, ""), 0):
                merged[name] = level
    return merged


#: Everything the installation may hold: the union of all purposes. More is a problem.
ALLOWED_PERMISSIONS: Mapping[str, str] = _union(*PURPOSES.values())
#: The only merge method of ``reviewer-gh merge``: the rulesets allow squash only and
#: the App bypasses them, so the tool must not rely on GitHub to enforce it.
MERGE_METHOD = "squash"
#: Rule types that forbid deleting a branch; ``--delete-branch`` skips such branches
#: because the App would otherwise bypass the protection.
DELETE_PROTECTING_RULES = frozenset({"deletion"})
#: Conflict-resolution PRs must land by fast-forward (docs/release.md §3.3), never squash.
FORWARD_LABEL = "release-forward"
#: Check-run conclusions GitHub itself treats as passing for required checks.
PASSING_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})

# (method, url, bearer, json body) -> parsed JSON response
Http = Callable[[str, str, str, dict[str, Any] | None], Any]


class ReviewerGhError(Exception):
    """A user-facing failure. Messages never contain key material or tokens."""


@dataclass(frozen=True)
class Paths:
    app_dir: Path
    cache_dir: Path

    @property
    def app_json(self) -> Path:
        return self.app_dir / "app.json"

    @property
    def private_key(self) -> Path:
        return self.app_dir / "private-key.pem"

    @property
    def token_cache(self) -> Path:
        return self.cache_dir / "token.json"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Paths:
        home = Path(env.get("HOME") or Path.home())
        return cls(
            app_dir=Path(
                env.get("AKKIABOT_REVIEWER_APP_DIR") or home / ".config/akkiabot/reviewer-app"
            ),
            cache_dir=Path(
                env.get("AKKIABOT_REVIEWER_CACHE_DIR") or home / ".cache/akkiabot/reviewer-app"
            ),
        )


# ---------------------------------------------------------------- JWT


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _ensure_private(path: Path) -> None:
    """Refuse key files readable by group/others, like ssh does."""
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        raise ReviewerGhError(
            f"找不到 App 私钥：{path}（见 docs/reviewer-github-app.md）"
        ) from None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ReviewerGhError(f"{path} 权限过宽（{stat.filemode(mode)}），请执行 chmod 600")


def load_app_issuer(paths: Paths) -> str:
    """Return the JWT ``iss`` claim: the client ID, falling back to the App ID."""
    try:
        data = json.loads(paths.app_json.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ReviewerGhError(
            f"找不到 {paths.app_json}（见 docs/reviewer-github-app.md）"
        ) from None
    issuer = data.get("client_id") or data.get("app_id")
    if not issuer:
        raise ReviewerGhError(f"{paths.app_json} 中缺少 client_id / app_id")
    return str(issuer)


def make_jwt(issuer: str, key_path: Path, now: int) -> str:
    """Build an RS256 app JWT. ``openssl`` reads the key; Python never does."""
    _ensure_private(key_path)
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    # iat is backdated to tolerate clock drift; GitHub caps exp at 10 minutes.
    claims = {"iat": now - 60, "exp": now + 9 * 60, "iss": issuer}
    payload = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    openssl = shutil.which("openssl")
    if openssl is None:
        raise ReviewerGhError("需要 openssl 来签名 JWT，但未在 PATH 中找到")
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [openssl, "dgst", "-sha256", "-sign", str(key_path)],
        input=signing_input,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise ReviewerGhError("openssl 签名 JWT 失败，请检查私钥文件是否完整")
    return f"{header}.{payload}.{_b64url(proc.stdout)}"


# ---------------------------------------------------------------- HTTP


def urllib_http(method: str, url: str, bearer: str, body: dict[str, Any] | None) -> Any:
    if not url.startswith(API + "/"):
        raise ReviewerGhError(f"拒绝请求非 GitHub API 地址：{url}")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)  # noqa: S310 - https only, checked above
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "akkialab-agent-gh")
    req.add_header("Authorization", f"Bearer {bearer}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read()).get("message", "")
        except ValueError:
            message = ""
        path = url.removeprefix(API)
        raise ReviewerGhError(f"GitHub API {method} {path} 返回 {exc.code}：{message}") from None
    except urllib.error.URLError as exc:
        raise ReviewerGhError(f"无法连接 GitHub API：{exc.reason}") from None


# ---------------------------------------------------------------- token


def _parse_ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).timestamp()


def read_cache(path: Path, repo: str, now: float) -> dict[str, Any] | None:
    """Return the cached ``review`` token record if it is for ``repo`` and still fresh."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("repo") != repo or not record.get("token"):
            return None
        # Records from before AKKIA-25 have no purpose and carry the full permissions.
        if record.get("purpose") != "review":
            return None
        if _parse_ts(record["expires_at"]) - now < MIN_REMAINING:
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return record


def write_cache(path: Path, record: dict[str, Any]) -> None:
    """Atomically write the cache with 0700 dir / 0600 file permissions."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".token-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def require_authorized(repo: str) -> None:
    if repo not in AUTHORIZED_REPOS:
        raise ReviewerGhError(
            f"目标仓库 {repo} 不在已授权范围内（AUTHORIZED_REPOS），拒绝签发 token"
        )


def mint_token(
    paths: Paths, repo: str, http: Http, now: float, purpose: str = "review"
) -> dict[str, Any]:
    """Mint a token restricted to ``repo`` and to ``PURPOSES[purpose]``.

    GitHub refuses (422) to grant a permission the installation lacks, so a
    missing permission fails here instead of silently yielding a weaker token.
    """
    if purpose not in ("review", "merge"):
        raise ReviewerGhError(f"reviewer-gh 只签发 review / merge 用途的 token，不支持 {purpose}")
    jwt = make_jwt(load_app_issuer(paths), paths.private_key, int(now))
    installation = http("GET", f"{API}/repos/{repo}/installation", jwt, None)
    installation_id = installation["id"]
    resp = http(
        "POST",
        f"{API}/app/installations/{installation_id}/access_tokens",
        jwt,
        {"repositories": [repo.split("/", 1)[1]], "permissions": dict(PURPOSES[purpose])},
    )
    granted: dict[str, str] = resp.get("permissions", {})
    excess = {
        name: level
        for name, level in granted.items()
        if _LEVEL.get(level, 0) > _LEVEL.get(PURPOSES[purpose].get(name, ""), 0)
    }
    if excess:
        # Never use a token wider than asked for; revoke it right away.
        with contextlib.suppress(ReviewerGhError):
            http("DELETE", f"{API}/installation/token", resp["token"], None)
        raise ReviewerGhError(f"GitHub 签发的 {purpose} token 权限超出预期：{sorted(excess)}")
    return {
        "repo": repo,
        "purpose": purpose,
        "installation_id": installation_id,
        "token": resp["token"],
        "expires_at": resp["expires_at"],
        "permissions": granted,
        "repository_selection": resp.get("repository_selection"),
    }


def get_token(
    paths: Paths,
    repo: str,
    http: Http,
    *,
    refresh: bool = False,
    now: Callable[[], float] = time.time,
) -> tuple[dict[str, Any], bool]:
    """Return ``(record, from_cache)``; mint and cache a new token when needed."""
    require_authorized(repo)
    if not refresh:
        cached = read_cache(paths.token_cache, repo, now())
        if cached is not None:
            return cached, True
    record = mint_token(paths, repo, http, now())
    try:
        write_cache(paths.token_cache, record)
    except OSError as exc:
        # e.g. a sandboxed runtime that cannot write outside its workdir: the
        # token still works, we just mint a fresh one next time.
        print(f"reviewer-gh: 无法写入 token 缓存（{exc.strerror}），本次不缓存", file=sys.stderr)
    return record, False


# ---------------------------------------------------------------- commands


def _covers(granted: Mapping[str, str], wanted: Mapping[str, str]) -> list[str]:
    """Permissions in ``wanted`` that ``granted`` does not reach (empty = covered)."""
    return sorted(
        f"{name}:{level}"
        for name, level in wanted.items()
        if _LEVEL.get(granted.get(name, ""), 0) < _LEVEL[level]
    )


def check_installation(
    paths: Paths, repo: str, http: Http, *, refresh: bool, require_release: bool = False
) -> list[str]:
    """Print a secret-free report and return the list of problems found.

    The ``review`` and ``merge`` purposes must be servable; ``release`` only
    when ``require_release`` (after the release permissions were added to the
    App, AKKIA-25). Permissions beyond ``ALLOWED_PERMISSIONS`` are a problem.
    """
    record, from_cache = get_token(paths, repo, http, refresh=refresh)
    jwt = make_jwt(load_app_issuer(paths), paths.private_key, int(time.time()))
    app = http("GET", f"{API}/app", jwt, None)
    installation = http("GET", f"{API}/app/installations/{record['installation_id']}", jwt, None)
    # The cached token is restricted to ``repo``; listing with it would always
    # show just ``repo``. Mint a throwaway metadata-only token, unrestricted by
    # repository, to see what the installation can really reach.
    probe = http(
        "POST",
        f"{API}/app/installations/{record['installation_id']}/access_tokens",
        jwt,
        {"permissions": {"metadata": "read"}},
    )
    try:
        # Org-wide installations span several pages (default 30 per page): read
        # them all, or the target could look uninstalled and the count be low.
        installed = _paged(http, f"{API}/installation/repositories", probe["token"], "repositories")
    finally:
        http("DELETE", f"{API}/installation/token", probe["token"], None)
    slug = app.get("slug") or ""
    bot = http("GET", f"{API}/users/{slug}%5Bbot%5D", record["token"], None) if slug else {}

    repo_names = {r["full_name"] for r in installed}
    others = len(repo_names - set(AUTHORIZED_REPOS))
    permissions: dict[str, str] = installation.get("permissions", {})
    print(f"App:              {slug} (bot 身份 {slug}[bot]，bot 用户 ID {bot.get('id')})")
    print(f"Installation:     {record['installation_id']} @ {installation['account']['login']}")
    # Only authorised (public) names are printed: run logs and issue comments must
    # not enumerate the organisation's other, possibly private, repositories.
    authorised_installed = sorted(set(AUTHORIZED_REPOS) & repo_names)
    print(
        f"安装范围:         {installation.get('repository_selection')}；"
        f"授权仓库 {', '.join(authorised_installed) or '无'}；其他仓库 {others} 个（不列名称）"
    )
    print(f"权限:             {json.dumps(permissions, sort_keys=True)}")
    for purpose, wanted in PURPOSES.items():
        lacking = _covers(permissions, wanted)
        print(
            f"用途 {purpose + ':':<12}{'可签发' if not lacking else '缺少 ' + ', '.join(lacking)}"
        )
    print(f"token 过期时间:   {record['expires_at']}（{'复用缓存' if from_cache else '新签发'}）")

    missing = sorted(set(AUTHORIZED_REPOS) - set(repo_names))
    if missing:
        print(f"已授权但未安装:   {', '.join(missing)}")

    problems: list[str] = []
    if bot.get("id") != BOT_USER_ID:
        problems.append(f"{slug}[bot] 的用户 ID 应为 {BOT_USER_ID}，实际为 {bot.get('id')}")
    if repo not in repo_names:
        problems.append(f"安装范围不包含目标仓库 {repo}")
    for purpose in ("review", "merge", *(("release",) if require_release else ())):
        lacking = _covers(permissions, PURPOSES[purpose])
        if lacking:
            problems.append(f"权限不足以签发 {purpose} 用途的 token：缺少 {', '.join(lacking)}")
    extra = sorted(
        f"{name}:{level}"
        for name, level in permissions.items()
        if _LEVEL.get(level, 0) > _LEVEL.get(ALLOWED_PERMISSIONS.get(name, ""), 0)
    )
    if extra:
        problems.append(f"存在多余权限：{extra}")
    if app.get("events"):
        problems.append(f"App 不应订阅 webhook 事件：{app.get('events')}")
    return problems


# ---------------------------------------------------------------- merge gate

THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) {
    reviewThreads(first: 100, after: $after) {
      pageInfo { hasNextPage endCursor } nodes { isResolved }
    }
  } }
}
"""
MAX_PAGES = 50


def _paged(http: Http, url: str, token: str, key: str | None = None) -> list[Any]:
    """All items of a REST list endpoint (``per_page=100``), or an error."""
    items: list[Any] = []
    sep = "&" if "?" in url else "?"
    for page in range(1, MAX_PAGES + 1):
        data = http("GET", f"{url}{sep}per_page=100&page={page}", token, None)
        batch = (data or {}).get(key, []) if key else (data or [])
        items.extend(batch)
        if len(batch) < 100:
            return items
    raise ReviewerGhError(f"{url.removeprefix(API)} 超过 {MAX_PAGES} 页，未读全，拒绝合并")


def _unresolved_threads(http: Http, repo: str, number: int, token: str) -> int:
    owner, name = repo.split("/", 1)
    unresolved = 0
    after: str | None = None
    for _ in range(MAX_PAGES):
        variables = {"owner": owner, "name": name, "number": number, "after": after}
        data = http(
            "POST", f"{API}/graphql", token, {"query": THREADS_QUERY, "variables": variables}
        )
        if (data or {}).get("errors"):
            raise ReviewerGhError(f"GraphQL 查询评审线程失败：{str(data['errors'])[:300]}")
        page = data["data"]["repository"]["pullRequest"]["reviewThreads"]
        unresolved += sum(1 for t in page["nodes"] if not t["isResolved"])
        if not page["pageInfo"]["hasNextPage"]:
            return unresolved
        after = page["pageInfo"]["endCursor"]
    raise ReviewerGhError(f"PR #{number} 的评审线程超过 {MAX_PAGES} 页，未读全，拒绝合并")


def _is_status(result: Mapping[str, Any]) -> bool:
    """Commit statuses carry ``state``; check runs carry ``status`` / ``conclusion``."""
    return "state" in result and "conclusion" not in result


def _passing(result: Mapping[str, Any]) -> bool:
    if _is_status(result):
        return result.get("state") == "success"
    return result.get("status") == "completed" and result.get("conclusion") in PASSING_CONCLUSIONS


def _describe(result: Mapping[str, Any]) -> str:
    if _is_status(result):
        return f"status/{result.get('state')}"
    return f"{result.get('status')}/{result.get('conclusion')}"


def check_problems(
    required: list[Mapping[str, Any]],
    check_runs: list[Mapping[str, Any]],
    statuses: list[Mapping[str, Any]],
    *,
    require_present: bool,
) -> list[str]:
    """Conservative required-check verdict for one commit (empty = all pass).

    Every result carrying a required context's name must pass: the newest check
    run per (name, App) *and* the commit status of that name, so a green check
    run never hides a red status or vice versa. With ``require_present`` at
    least one result from the required source (the rule's ``integration_id``
    App, or any source when unset) must exist; without it (test merge commit)
    missing results are fine but any present failing one is not.
    """
    newest: dict[tuple[str, int | None], Mapping[str, Any]] = {}
    for run in check_runs:
        key = (run.get("name", ""), (run.get("app") or {}).get("id"))
        if key not in newest or run.get("id", 0) > newest[key].get("id", 0):
            newest[key] = run
    problems: list[str] = []
    for entry in required:
        context = entry.get("context", "")
        app_id = entry.get("integration_id")
        runs = [(app, run) for (name, app), run in newest.items() if name == context]
        status = [st for st in statuses if st.get("context") == context]
        results = [run for _, run in runs] + status
        sourced = [run for app, run in runs if app_id is None or app == app_id]
        if app_id is None:
            sourced += status
        if any(not _passing(r) for r in results) or (require_present and not sourced):
            seen = ", ".join(_describe(r) for r in results) or "没有结果"
            problems.append(f"必需检查 {context} 未通过（{seen}）")
    return problems


def merge_problems(
    *,
    pr: Mapping[str, Any],
    reviews: list[Mapping[str, Any]],
    unresolved: int,
    required: list[Mapping[str, Any]],
    check_runs: list[Mapping[str, Any]],
    statuses: list[Mapping[str, Any]],
    merge_check_runs: list[Mapping[str, Any]] | None = None,
    merge_statuses: list[Mapping[str, Any]] | None = None,
) -> list[str]:
    """Problems that forbid merging ``pr`` as the App (empty = may merge).

    Pure, so it is tested without the network. ``reviews`` are REST reviews
    (oldest first), ``required`` the ``required_status_checks`` entries of the
    base branch rules, ``check_runs`` / ``statuses`` those of the head commit and
    ``merge_check_runs`` / ``merge_statuses`` those of the test merge commit
    (``merge_commit_sha``), which GitHub may also gate on.
    """
    problems: list[str] = []
    head = pr["head"]["sha"]
    if pr.get("state") != "open":
        problems.append(f"PR 不是打开状态（{pr.get('state')}）")
    if pr.get("draft"):
        problems.append("PR 仍是草稿")
    if any(label.get("name") == FORWARD_LABEL for label in pr.get("labels") or []):
        problems.append(
            f"带 {FORWARD_LABEL} 标签的上合冲突 PR 由发布流水线快进落地，不能 squash 合并"
        )

    author_id = (pr.get("user") or {}).get("id")
    latest: dict[int, tuple[str, str, str]] = {}
    for review in reviews:
        user = review.get("user") or {}
        if user.get("id") is None or user.get("id") == author_id:
            continue
        if review.get("state") in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            latest[user["id"]] = (
                review["state"],
                review.get("commit_id") or "",
                user.get("login", ""),
            )
    mine = latest.get(BOT_USER_ID)
    if mine is None or mine[0] != "APPROVED" or mine[1] != head:
        problems.append(f"本 App 尚未在当前 head {head[:12]} 上给出 Approve")
    blocked = sorted(login for state, _, login in latest.values() if state == "CHANGES_REQUESTED")
    if blocked:
        problems.append(f"仍有未处理的 Request changes：{', '.join(blocked)}")
    if unresolved:
        problems.append(f"还有 {unresolved} 个未解决的评审线程")

    if not required:
        problems.append(
            f"目标分支 {pr['base']['ref']} 没有配置必需检查，按失败处理，拒绝以 App 身份合并"
        )
    problems += check_problems(required, check_runs, statuses, require_present=True)
    merge_sha = pr.get("merge_commit_sha")
    if pr.get("mergeable") is False:
        problems.append("PR 与目标分支有冲突（mergeable=false）")
    elif not merge_sha or pr.get("mergeable") is None:
        problems.append("GitHub 尚未算出测试合并提交（mergeable 未知），请稍后重试")
    else:
        problems += [
            f"测试合并提交 {merge_sha[:12]} 上{p}"
            for p in check_problems(
                required, merge_check_runs or [], merge_statuses or [], require_present=False
            )
        ]
    return problems


@dataclass(frozen=True)
class MergeResult:
    sha: str
    #: What happened to the head branch; ``cleanup_failed`` only when deletion was
    #: attempted and errored (the merge itself succeeded either way).
    cleanup: str = ""
    cleanup_failed: bool = False


def _delete_head_branch(http: Http, repo: str, pr: Mapping[str, Any], token: str) -> MergeResult:
    base_url = f"{API}/repos/{repo}"
    head = pr["head"]
    if (head.get("repo") or {}).get("full_name") != repo:
        return MergeResult("", f"head 分支不在 {repo}，未删除")
    ref = urllib.parse.quote(head["ref"], safe="/")
    try:
        rules = _paged(http, f"{base_url}/rules/branches/{ref}", token)
        protecting = sorted({r.get("type") for r in rules} & DELETE_PROTECTING_RULES)
        branch = http("GET", f"{base_url}/branches/{ref}", token, None) or {}
        if protecting or branch.get("protected"):
            why = f"规则 {', '.join(protecting)}" if protecting else "分支保护"
            return MergeResult("", f"head 分支 {head['ref']} 受{why}保护，未删除")
        http("DELETE", f"{base_url}/git/refs/heads/{ref}", token, None)
    except ReviewerGhError as exc:
        return MergeResult("", f"head 分支 {head['ref']} 删除失败：{exc}", cleanup_failed=True)
    return MergeResult("", f"已删除 head 分支 {head['ref']}")


def merge_pr(
    paths: Paths,
    repo: str,
    number: int,
    http: Http,
    *,
    delete_branch: bool = False,
) -> MergeResult:
    """Squash-merge PR ``number`` after the tool-side gate.

    A dedicated ``merge`` token is minted for this call only and revoked at the
    end, whatever happens. The merge is pinned to the head SHA the gate saw
    (``sha`` parameter), so a push in between makes GitHub refuse it. Head
    branch cleanup runs only after a successful merge, never deletes a branch
    protected against deletion, and its failure does not turn the merge into
    an error (``MergeResult.cleanup_failed``).
    """
    require_authorized(repo)
    record = mint_token(paths, repo, http, time.time(), purpose="merge")
    token = record["token"]
    try:
        base_url = f"{API}/repos/{repo}"
        pr = http("GET", f"{base_url}/pulls/{number}", token, None)
        head = pr["head"]["sha"]
        merge_sha = pr.get("merge_commit_sha")
        rules = _paged(http, f"{base_url}/rules/branches/{pr['base']['ref']}", token)
        required = [
            check
            for rule in rules
            if rule.get("type") == "required_status_checks"
            for check in (rule.get("parameters") or {}).get("required_status_checks", [])
        ]

        def results(sha: str) -> tuple[list[Any], list[Any]]:
            runs = _paged(http, f"{base_url}/commits/{sha}/check-runs", token, "check_runs")
            combined = http("GET", f"{base_url}/commits/{sha}/status", token, None) or {}
            return runs, combined.get("statuses", [])

        head_runs, head_statuses = results(head)
        merge_runs, merge_statuses = results(merge_sha) if merge_sha else ([], [])
        problems = merge_problems(
            pr=pr,
            reviews=_paged(http, f"{base_url}/pulls/{number}/reviews", token),
            unresolved=_unresolved_threads(http, repo, number, token),
            required=required,
            check_runs=head_runs,
            statuses=head_statuses,
            merge_check_runs=merge_runs,
            merge_statuses=merge_statuses,
        )
        if problems:
            raise ReviewerGhError(f"PR #{number} 不满足合并门禁，未合并：" + "；".join(problems))
        result = http(
            "PUT",
            f"{base_url}/pulls/{number}/merge",
            token,
            {"merge_method": MERGE_METHOD, "sha": head},
        )
        if not (result or {}).get("merged"):
            raise ReviewerGhError(f"PR #{number} 合并未成功：{(result or {}).get('message', '')}")
        sha = str(result.get("sha", ""))
        if not delete_branch:
            return MergeResult(sha)
        cleanup = _delete_head_branch(http, repo, pr, token)
        return MergeResult(sha, cleanup.cleanup, cleanup.cleanup_failed)
    finally:
        with contextlib.suppress(ReviewerGhError):
            http("DELETE", f"{API}/installation/token", token, None)


def revoke_cached(paths: Paths, http: Http) -> bool:
    """Revoke the cached token (if any) and delete the cache file."""
    try:
        record = json.loads(paths.token_cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    try:
        if record.get("token"):
            http("DELETE", f"{API}/installation/token", record["token"], None)
    finally:
        paths.token_cache.unlink(missing_ok=True)
    return True


def find_gh(env: Mapping[str, str]) -> str:
    candidates = [
        env.get("REVIEWER_GH_BIN"),
        shutil.which("gh"),
        str(Path.home() / ".local/bin/gh"),
    ]
    for candidate in candidates:
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    raise ReviewerGhError("找不到 gh 可执行文件（可用 REVIEWER_GH_BIN 指定）")


def looks_like_merge(gh_args: list[str]) -> bool:
    """Friendly early refusal for merge attempts through the ``gh`` passthrough.

    Not the security boundary (that is the review token lacking Contents write);
    it only turns GitHub's 403 into a pointer at ``reviewer-gh merge``.
    """
    words = [a for a in gh_args if not a.startswith("-")]
    if words[:2] == ["pr", "merge"]:
        return True
    text = " ".join(gh_args)
    return words[:1] == ["api"] and any(
        marker in text
        for marker in ("/merge", "mergePullRequest", "enablePullRequestAutoMerge", "/merges")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reviewer-gh", description="以工作区共用 GitHub App akkialab-agent 身份运行 gh"
    )
    parser.add_argument("--refresh", action="store_true", help="忽略缓存，强制签发新 token")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="验证安装范围、权限并取得 token（不输出任何凭据）")
    check.add_argument(
        "--require-release", action="store_true", help="同时要求安装权限足以签发发布用途的 token"
    )
    sub.add_parser("revoke", help="吊销缓存中的 token 并删除缓存")
    merge = sub.add_parser(
        "merge", help="核对必需检查、评审线程与批准后 squash 合并 PR（唯一的合并入口）"
    )
    merge.add_argument("number", type=int)
    merge.add_argument(
        "-R", "--repo", help="目标仓库，默认 $AKKIABOT_REVIEWER_REPO 或 AkkiaLab/AkkiaBot"
    )
    merge.add_argument(
        "--delete-branch",
        action="store_true",
        help="合并后删除同仓库、未受删除保护的 head 分支（失败不影响合并，退出码 3）",
    )
    gh = sub.add_parser("gh", help="以 App 身份执行 gh（review 用途 token，不能合并或推送）")
    gh.add_argument("gh_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    env = dict(os.environ)
    paths = Paths.from_env(env)
    repo = env.get("AKKIABOT_REVIEWER_REPO") or DEFAULT_REPO
    try:
        if args.command == "check":
            problems = check_installation(
                paths, repo, urllib_http, refresh=args.refresh, require_release=args.require_release
            )
            for problem in problems:
                print(f"问题：{problem}", file=sys.stderr)
            print("检查通过" if not problems else "检查未通过")
            return 1 if problems else 0
        if args.command == "revoke":
            print("已吊销缓存的 token" if revoke_cached(paths, urllib_http) else "没有缓存的 token")
            return 0
        if args.command == "merge":
            result = merge_pr(
                paths, args.repo or repo, args.number, urllib_http, delete_branch=args.delete_branch
            )
            print(f"已合并 PR #{args.number}（{result.sha[:12]}，squash）")
            if result.cleanup:
                print(
                    f"{'清理未完成' if result.cleanup_failed else '清理'}：{result.cleanup}",
                    file=sys.stderr if result.cleanup_failed else sys.stdout,
                )
            return 3 if result.cleanup_failed else 0
        gh_args = list(args.gh_args)
        if gh_args[:1] == ["--"]:
            gh_args = gh_args[1:]
        if not gh_args:
            raise ReviewerGhError("用法：reviewer-gh gh <gh 参数...>")
        if looks_like_merge(gh_args):
            raise ReviewerGhError(
                "合并请用 reviewer-gh merge <PR 号>（会先核对检查、线程与批准）；"
                "gh 透传使用的 review token 没有 Contents 写权限，本来也无法合并"
            )
        record, _ = get_token(paths, repo, urllib_http, refresh=args.refresh)
        gh_bin = find_gh(env)
        env["GH_TOKEN"] = record["token"]
        env.pop("GITHUB_TOKEN", None)
        os.execve(gh_bin, [gh_bin, *gh_args], env)  # noqa: S606 - replaces this process with gh
    except ReviewerGhError as exc:
        print(f"reviewer-gh: {exc}", file=sys.stderr)
        return 2
    return 0  # pragma: no cover - execve does not return


if __name__ == "__main__":
    sys.exit(main())
