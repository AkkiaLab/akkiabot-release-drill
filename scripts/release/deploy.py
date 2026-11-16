"""Deploying a release to staging (RC tag) or production (formal tag).

The host side is ``deploy/upgrade.sh`` taken from the commit being deployed
(so the procedure is reviewed with the release); this module adds the gates
that need GitHub and git:

* staging (``release_staging``): the RC is current (``rc.check_rc``); a
  machine-local lock plus the repository-wide staging lease (``lease.py``) are
  held from the Mini App upload through the backend deploy to the end of the
  acceptance, so two RCs or a manual frontend run never interleave;
* production: the formal tag passes ``seal.verify_formal`` and the ``approve``
  job of ``release-production.yml`` for that commit succeeded, i.e. the owner
  approved the ``production`` environment. Without it nothing runs.

After the backend, the public ``/readyz`` (and on staging the Mini App
``version.json``) must confirm the deployed sha; a failure there is a failed
deploy (exit 9 / 10) and stops everything that would follow. Production then
dispatches ``webapp-promote.yml`` for the same sha (the Mini App keeps its own
``webapp-production`` approval).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shlex
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from scripts.release.git import Git, GitError
from scripts.release.github import GitHub
from scripts.release.rc import check_rc, select_baseline
from scripts.release.seal import verify_formal
from scripts.release.versions import ReleaseError

MIGRATIONS_DIR = "packages/core/src/akkiabot_core/db/migrations/versions"
PRODUCTION_WORKFLOW = "release-production.yml"
APPROVE_JOB = "approve"


def production_title(tag: str) -> str:
    """``run-name`` of release-production.yml; the deploy gate looks the run up by it."""
    return f"生产发布 {tag}"


@dataclass(frozen=True)
class Target:
    name: str
    directory: str
    ssh: str | None
    health_url: str
    webapp_version_url: str | None
    template: str | None


TARGETS = {
    "staging": Target(
        name="staging",
        directory="/opt/akkiabot/staging",
        ssh=None,
        health_url="https://bot-api-staging.akkia.moe/readyz",
        webapp_version_url="https://staging.akkiabot-webapp.pages.dev/version.json",
        template="staging/app.env.tpl",
    ),
    "production": Target(
        name="production",
        directory="~/akkiabot/production",
        ssh="greencloud-tyo-01",
        health_url="https://bot-api.akkia.moe/readyz",
        webapp_version_url=None,
        template=None,
    ),
}

#: Exit codes of deploy/upgrade.sh and what they mean for the operator.
UPGRADE_RESULTS = {
    0: "成功",
    3: "被门禁拒绝（配置缺项或迁移未确认），环境未改变",
    4: "准备失败（下载/拉取镜像），环境未改变，可直接重试",
    5: "备份失败，环境未改变",
    6: "切换后失败且涉及迁移：未自动回滚，按 docs/operations.md §5.2 人工处理",
    7: "切换后失败，已自动回滚到旧版本（无迁移）",
    8: "切换后失败且自动回滚也失败：需要人工处理",
    75: "另一次升级正在进行",
    9: "后端已切换到新版本，但公网 /readyz 检查失败：后续步骤已停止；查因后重试同一版本或按 §4.4 回滚",
    10: "后端已切换到新版本，但 staging Mini App version.json 缺失或不是该 sha：已停止，不进入验收",
}
PUBLIC_HEALTH_FAILED = 9
WEBAPP_VERSION_MISMATCH = 10


Runner = Callable[[list[str], str | None, dict[str, str] | None], subprocess.CompletedProcess[str]]


def _run(
    args: list[str], stdin: str | None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed executables (bash/ssh), quoted arguments
        args, input=stdin, capture_output=True, text=True, check=False, env=env
    )


def template_environment() -> dict[str, str]:
    """Environment with the machine's 1Password service account token for ``op inject``
    (same token file as the acceptance tool); the token never appears in arguments."""
    from akkiabot_e2e.onepassword import default_token_file, op_environment

    env, _ = op_environment(os.environ, default_token_file())
    return env


def host_shell(target: Target, command: str) -> list[str]:
    """argv running ``command`` inside the target directory (locally or over ssh)."""
    remote = f"cd {target.directory} && {command}"
    if target.ssh:
        return ["ssh", target.ssh, remote]
    return ["bash", "-c", remote]


def host_command(target: Target, script_args: list[str]) -> list[str]:
    """argv running the script read from stdin with ``script_args``."""
    return host_shell(target, f"bash -s -- {shlex.join(script_args)}")


@contextlib.contextmanager
def environment_lock(name: str, lock_dir: Path | None = None) -> Iterator[None]:
    """Machine-local lock of one environment (staging: deploy + acceptance)."""
    lock_dir = (
        lock_dir or Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "akkiabot"
    )
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"release-{name}.lock"
    with path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReleaseError(f"{name} 环境正被另一次发版占用（{path}），请等待其结束") from exc
        yield


def current_tag(target: Target, run: Runner = _run) -> str:
    proc = run(host_shell(target, "sed -n 's/^AKKIABOT_TAG=//p' .env | tail -n1"), None, None)
    if proc.returncode != 0:
        raise ReleaseError(f"读取 {target.name} 当前版本失败：{proc.stderr.strip()[:200]}")
    return proc.stdout.strip()


def migrations_changed(git: Git, old: str, new: str) -> bool:
    """Unknown old versions (``latest``, missing commit) count as changed: fail safe."""
    if len(old) != 40:
        return True
    try:
        return bool(git.changed_files(old, new, MIGRATIONS_DIR))
    except GitError:  # old sha not present locally: be conservative
        return True


def _is_ancestor(git: Git, ancestor: str, descendant: str) -> bool:
    try:
        return git.is_ancestor(ancestor, descendant)
    except GitError:  # unknown commit: not provably a downgrade
        return False


#: Cloudflare in front of the bot API and the Mini App answers 403 to urllib's
#: default ``Python-urllib`` user agent (BOT-40 staging rehearsal).
USER_AGENT = "akkiabot-release (+https://github.com/AkkiaLab/AkkiaBot)"


def _get(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310 - fixed https URLs


def fetch_json(url: str) -> dict[str, object] | None:
    try:
        with urllib.request.urlopen(_get(url), timeout=15) as response:  # noqa: S310 - fixed https URLs
            return json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(_get(url), timeout=15) as response:  # noqa: S310 - fixed https URLs
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError):
        return False


@dataclass
class DeployResult:
    sha: str
    old: str
    code: int
    lines: list[str]


def deploy(
    git: Git,
    gh: GitHub,
    target: Target,
    tag: str,
    *,
    allow_migration: bool = False,
    run: Runner = _run,
    log: Callable[[str], None] = print,
) -> DeployResult:
    if target.name == "staging":
        state = git.state()
        candidate = check_rc(state, tag, gh.sealed_branches())
        sha = candidate.sha
    else:
        _, sha = verify_formal(git, gh, tag)
        if not gh.approved_run(
            PRODUCTION_WORKFLOW, production_title(tag), APPROVE_JOB, "production"
        ):
            raise ReleaseError(
                f"{tag} 的生产审批未通过：{PRODUCTION_WORKFLOW} 的 {APPROVE_JOB} 任务（环境 production）"
                "尚未由负责人批准；不执行生产部署"
            )
    old = current_tag(target, run)
    changed = migrations_changed(git, old, sha)
    if changed and len(old) == 40 and old != sha and _is_ancestor(git, sha, old):
        raise ReleaseError(
            f"{tag} 比当前版本 {old[:12]} 旧且跨越了数据库迁移：回滚需要恢复备份，"
            "按 docs/operations.md §5.2 人工执行并经负责人确认，流水线不自动处理"
        )
    script = git.out("show", f"{sha}:deploy/upgrade.sh")
    args = [sha, "--migrations", "changed" if changed else "unchanged", "--release", tag]
    if allow_migration:
        args.append("--allow-migration")
    if target.template:
        args += ["--render-template", target.template]
    log(f"{target.name}: {old or '?'} -> {sha}（{tag}），迁移{'有变化' if changed else '无变化'}")
    env = template_environment() if target.template and not target.ssh else None
    proc = run(host_command(target, args), script, env)
    lines = [
        line
        for line in (proc.stdout + proc.stderr).splitlines()
        if line.startswith("upgrade:") or line.startswith("backup:")
    ]
    meaning = UPGRADE_RESULTS.get(proc.returncode, f"未知退出码 {proc.returncode}")
    lines.append(f"upgrade.sh 结果：{meaning}")
    code = proc.returncode
    if code == 0:
        healthy = http_ok(target.health_url)
        lines.append(f"公网健康检查 {target.health_url}：{'通过' if healthy else '失败'}")
        if not healthy:
            code = PUBLIC_HEALTH_FAILED
        elif target.webapp_version_url:
            version = fetch_json(target.webapp_version_url) or {}
            seen = version.get("sha")
            lines.append(
                f"Mini App version.json：{seen or '无法读取'}（{'一致' if seen == sha else '不一致'}）"
            )
            if seen != sha:
                code = WEBAPP_VERSION_MISMATCH
        if code != 0:
            lines.append(f"结果：{UPGRADE_RESULTS[code]}")
    return DeployResult(sha, old, code, lines)


WEBAPP_WORKFLOW = "webapp-deploy.yml"
#: Exit code when the staging lease is kept because a frontend run could not be confirmed ended.
LEASE_KEPT = 11


def webapp_title(sha: str, nonce: str) -> str:
    """``run-name`` of a leased staging run of webapp-deploy.yml."""
    return f"staging {sha} {nonce}"


@dataclass
class StagingResult:
    code: int
    lines: list[str]


def release_staging(
    git: Git,
    gh: GitHub,
    target: Target,
    tag: str,
    *,
    scenarios: str | None,
    accept: bool,
    allow_migration: bool = False,
    run: Runner = _run,
    accept_run: Callable[[list[str]], int] | None = None,
    log: Callable[[str], None] = print,
) -> StagingResult:
    """RC to staging under the lease: Mini App → backend → checks → acceptance → release.

    Every step must succeed before the next one starts; the lease is released
    at the end (also on failure), never while the RC is still being accepted.
    """
    from scripts.release import lease as staging_lease

    candidate = check_rc(git.state(), tag, gh.sealed_branches())
    command = staging_acceptance_command(git, gh, candidate.sha, scenarios)
    lines: list[str] = []
    held = staging_lease.acquire(git, candidate.sha, tag)
    keep = False
    try:
        lines.append(f"已取得 staging 租约（{held.commit[:12]}，到 {held.expires.isoformat()}）")
        nonce = held.commit[:12]
        try:
            conclusion = gh.run_and_wait(
                WEBAPP_WORKFLOW,
                {"sha": candidate.sha, "nonce": nonce},
                webapp_title(candidate.sha, nonce),
            )
        except Exception as exc:
            # The upload may still be running (or may still start): releasing now
            # would let it overwrite the next RC. Keep the lease for a person.
            keep = True
            lines.append(f"前端部署状态无法确认：{exc}")
            lines.append(
                "已保留 staging 租约。恢复：在 Actions 中确认该 webapp-deploy 运行已结束（必要时取消），"
                f"再执行 `just release lease release {candidate.sha}`，然后重跑 `just release-staging`；"
                f"租约最迟 {held.expires.isoformat()} 过期"
            )
            return StagingResult(LEASE_KEPT, lines)
        lines.append(f"staging Mini App（{WEBAPP_WORKFLOW}）：{conclusion}")
        if conclusion != "success":
            lines.append(
                "前端未部署成功（运行已结束）：后端未改动，已停止；查因后重跑 `just release-staging`"
            )
            return StagingResult(1, lines)
        result = deploy(git, gh, target, tag, allow_migration=allow_migration, run=run, log=log)
        lines += result.lines
        if result.code != 0:
            return StagingResult(result.code, lines)
        lines.append(f"验收命令：`{' '.join(command)}`")
        if not accept:
            lines.append(
                "未带 --accept：租约随本命令结束释放，验收须重新运行 `just release-staging … --accept`"
            )
            return StagingResult(0, lines)
        runner = accept_run or (lambda argv: subprocess.run(argv, check=False).returncode)  # noqa: S603
        code = runner(command)
        lines.append(f"验收命令退出码：{code}")
        return StagingResult(code, lines)
    finally:
        if not keep and not staging_lease.release(git, held):
            lines.append("释放 staging 租约失败：它已过期并被接管，或已被人工释放")


def staging_acceptance_command(git: Git, gh: GitHub, sha: str, scenarios: str | None) -> list[str]:
    """The ``just e2e-staging`` invocation for an RC: diff against the lineage baseline."""
    args = ["just", "e2e-staging", "--record-status", sha]
    if not scenarios:
        return args  # full acceptance
    baseline = select_baseline(git, gh, sha)
    if baseline is None:
        raise ReleaseError(
            f"{sha[:12]} 没有已通过 staging 验收的祖先：本谱系首个 RC 须全量验收（不带场景）"
        )
    return [*args, "--baseline", baseline, "--scenarios", scenarios]
