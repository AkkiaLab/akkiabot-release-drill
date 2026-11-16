"""Cascade forward merges: every release flows into every later release and finally master.

Model (docs/release.md §3): order the release branches by version and append
``master``. Each branch forwards into the next *receivable* node, i.e. the
next branch that is not sealed, or master. A sealed branch never receives
anything but still forwards as a source, so a late commit on it is not lost.

The planner is ancestry-based and stateless: a hop is needed exactly when the
source tip is not an ancestor of the target tip. That makes every run a full
reconciliation, so duplicate, delayed, dropped or concurrent events are all
harmless: whatever run comes next sees the same gaps and closes them.

A conflicting hop is paused, not retried blindly: an open GitHub issue
labelled ``release-forward-conflict`` with a marker for ``source -> target``
records the pause and deduplicates; Multica is notified once to create a squad
task. The hop resumes when the source becomes an ancestor of the target (the
resolution PR landed, see ``land_forward_pr``), and the issue is closed.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from scripts.release.git import Git, RepoState
from scripts.release.github import (
    CONFLICT_LABEL,
    FORWARD_LABEL,
    GitHub,
    PullRequest,
)
from scripts.release.versions import MASTER, ReleaseError, Version

#: Upper bound on hops per run; a run that hits it fails loudly instead of looping.
MAX_HOPS = 50
#: Push retries when the target moved between fetch and push.
PUSH_ATTEMPTS = 3
#: Checks the resolution PR head must have passed before it is landed.
REQUIRED_CHECKS = ("ci-ok", "secret-scan", "release-guard")
BOT_ID_RE = re.compile(r"\bBOT-[1-9][0-9]*\b")
MARKER_RE = re.compile(r"<!-- release-forward: (\S+)->(\S+) -->")


def node_name(node: Version | None) -> str:
    return MASTER if node is None else node.branch


@dataclass(frozen=True)
class Hop:
    source: Version
    target: Version | None  # None = master

    @property
    def key(self) -> str:
        return f"{node_name(self.source)}->{node_name(self.target)}"

    @property
    def marker(self) -> str:
        return f"<!-- release-forward: {self.key} -->"


def next_receivable(source: Version, state: RepoState, sealed: set[str]) -> Version | None:
    """The node ``source`` forwards into: the next unsealed release, else master."""
    for version in sorted(state.branches):
        if version > source and version.branch not in sealed:
            return version
    return None


def plan(git: Git, state: RepoState, sealed: set[str]) -> list[Hop]:
    """Every hop whose source tip is not yet contained in its target, ascending."""
    hops: list[Hop] = []
    for version in sorted(state.branches):
        target = next_receivable(version, state, sealed)
        target_sha = state.master if target is None else state.branches[target]
        if not git.is_ancestor(state.branches[version], target_sha):
            hops.append(Hop(version, target))
    return hops


@dataclass
class CascadeReport:
    merged: list[str] = field(default_factory=list)
    paused: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def lines(self) -> list[str]:
        out = [f"- 已上合：{m}" for m in self.merged]
        out += [f"- 冲突暂停：{p}" for p in self.paused]
        out += [f"- 冲突已解决：{r}" for r in self.resolved]
        out += [f"- {n}" for n in self.notes]
        return out or ["- 所有版本分支均已逐级上合，无需操作"]


def _related_prs(gh: GitHub, shas: list[str], limit: int = 20) -> list[PullRequest]:
    seen: dict[int, PullRequest] = {}
    for sha in shas[:limit]:
        for pr in gh.prs_for_commit(sha):
            seen.setdefault(pr.number, pr)
    return sorted(seen.values(), key=lambda pr: pr.number)


def _pr_lines(prs: list[PullRequest]) -> list[str]:
    if not prs:
        return ["  - （未找到关联 PR）"]
    return [f"  - #{pr.number} {pr.title}" for pr in prs]


def conflict_body(
    hop: Hop,
    source_sha: str,
    target_sha: str,
    files: list[str],
    source_prs: list[PullRequest],
    target_prs: list[PullRequest],
    trigger: str,
) -> str:
    tasks = sorted({m for pr in source_prs + target_prs for m in BOT_ID_RE.findall(pr.title)})
    src, tgt = node_name(hop.source), node_name(hop.target)
    lines = [
        hop.marker,
        f"逐级上合 `{src}` → `{tgt}` 出现冲突，该方向的传播已暂停；其他方向照常进行。",
        "",
        f"- 触发：{trigger}",
        f"- 来源 `{src}`：`{source_sha}`",
        f"- 目标 `{tgt}`：`{target_sha}`",
        "- 冲突文件：",
        *[f"  - `{f}`" for f in files],
        "- 来源侧待上合的 PR：",
        *_pr_lines(source_prs),
        "- 目标侧改动过冲突文件的 PR：",
        *_pr_lines(target_prs),
        f"- 相关任务：{', '.join(tasks) if tasks else '（PR 标题中未找到 BOT 编号）'}",
        "",
        "恢复（docs/release.md §3.3）：",
        "",
        "```bash",
        f"git fetch origin && git switch -c forward/{hop.source}-to-{tgt.replace('/', '-')} origin/{tgt}",
        f"git merge --no-ff origin/{src}   # 解决冲突后 git commit",
        "```",
        "",
        f"推送后向 `{tgt}` 提 PR，加标签 `{FORWARD_LABEL}`，由 Reviewer 评审。"
        "批准且检查通过后，上合流水线以快进方式落地该 merge commit（保留 merge 关系，不 squash），"
        "随后关闭本 issue 并继续后续传播。",
    ]
    return "\n".join(lines)


def run_cascade(
    git: Git,
    gh: GitHub,
    *,
    trigger: str,
    dry_run: bool = False,
    log: Callable[[str], None] = print,
) -> CascadeReport:
    """Reconcile every forward hop until nothing is left or only paused hops remain."""
    report = CascadeReport()
    attempts: dict[str, int] = {}
    for _ in range(MAX_HOPS):
        state = git.state()
        sealed = gh.sealed_branches()
        hops = plan(git, state, sealed)
        _close_resolved(git, gh, state, sealed, report, dry_run=dry_run)
        actionable = []
        for hop in hops:
            if hop.key in report.paused:
                # Paused earlier in this run: the issue listing may lag behind the
                # issue just created (eventually consistent), so do not ask it again.
                continue
            issue = gh.find_issue(hop.marker)
            if issue is None:
                actionable.append(hop)
            else:
                report.paused.append(hop.key)
        if not actionable:
            return report
        hop = actionable[0]
        source_sha = state.branches[hop.source]
        target_ref = node_name(hop.target)
        target_sha = state.master if hop.target is None else state.branches[hop.target]
        if dry_run:
            report.notes.append(f"（dry-run）待上合 {hop.key}")
            return report
        message = f"上合 {node_name(hop.source)} 到 {target_ref}（{trigger}）"
        merged, conflicts = git.merge_forward(source_sha, target_sha, message)
        if merged is not None:
            if git.push_ref(merged, f"refs/heads/{target_ref}"):
                report.merged.append(f"{hop.key}（{merged[:12]}）")
                log(f"merged {hop.key} as {merged}")
                continue
            attempts[hop.key] = attempts.get(hop.key, 0) + 1
            if attempts[hop.key] >= PUSH_ATTEMPTS:
                raise ReleaseError(
                    f"推送 {target_ref} 连续 {PUSH_ATTEMPTS} 次被拒绝（目标持续变化，或规则禁止写入）"
                )
            log(f"push to {target_ref} rejected, re-planning")
            continue
        _pause(git, gh, hop, source_sha, target_sha, conflicts, trigger)
        report.paused.append(hop.key)
    raise ReleaseError(f"单次运行超过 {MAX_HOPS} 次上合，已停止；请检查分支状态")


def _pause(
    git: Git,
    gh: GitHub,
    hop: Hop,
    source_sha: str,
    target_sha: str,
    files: list[str],
    trigger: str,
) -> None:
    if gh.find_issue(hop.marker) is not None:  # created concurrently: dedupe
        return
    source_prs = _related_prs(gh, git.rev_list(source_sha, target_sha))
    target_prs = _related_prs(gh, git.files_touched(target_sha, source_sha, files))
    body = conflict_body(hop, source_sha, target_sha, files, source_prs, target_prs, trigger)
    issue = gh.create_issue(
        f"上合冲突：{node_name(hop.source)} → {node_name(hop.target)}", body, [CONFLICT_LABEL]
    )
    result = gh.notify(
        {
            "kind": "release-forward-conflict",
            "key": hop.key,
            "source": node_name(hop.source),
            "target": node_name(hop.target),
            "source_sha": source_sha,
            "target_sha": target_sha,
            "conflict_files": files,
            "source_prs": [pr.number for pr in source_prs],
            "target_prs": [pr.number for pr in target_prs],
            "issue": issue.url or issue.number,
            "trigger": trigger,
        }
    )
    gh.comment_issue(issue.number, f"自动化：{result}")


def _close_resolved(
    git: Git,
    gh: GitHub,
    state: RepoState,
    sealed: set[str],
    report: CascadeReport,
    *,
    dry_run: bool,
) -> None:
    """Close every open conflict issue whose source is now contained downstream.

    An issue records the hop that conflicted (``source->target``). The source's
    current target may have changed since: a patch release was created in
    between, or the old target was sealed. So an issue is resolved when its
    source tip is contained in the target it recorded *or* in the source's
    current target; looking only at the current hop would leave the old
    direction's issue open forever (BOT-40 drill).
    """
    for issue in gh.open_conflict_issues():
        match = MARKER_RE.search(issue.body)
        if match is None:
            continue
        try:
            source = _version_or_none(match.group(1))
            recorded = _version_or_none(match.group(2))
        except ReleaseError:
            continue
        if source is None or source not in state.branches:
            continue
        sha = state.branches[source]
        candidates = [recorded, next_receivable(source, state, sealed)]
        for target in dict.fromkeys(candidates):
            if target is not None and target not in state.branches:
                continue
            target_sha = state.master if target is None else state.branches[target]
            if git.is_ancestor(sha, target_sha):
                if not dry_run:
                    gh.close_issue(
                        issue.number,
                        f"已解决：`{sha[:12]}` 已包含在 `{node_name(target)}`"
                        f"（`{target_sha[:12]}`）中，继续逐级上合。",
                    )
                report.resolved.append(f"{match.group(1)}->{match.group(2)}")
                break


def land_forward_pr(git: Git, gh: GitHub, number: int) -> str:
    """Land an approved conflict-resolution PR by fast-forwarding its target.

    The PR head must be a merge that contains the current target tip and the
    paused source tip, carry the ``release-forward`` label, pass the Reviewer
    gate (``GitHub.review_gate``: Reviewer approval of this head, no open
    threads, no unhandled later comments) and the required checks, and still be
    the same head right before the push, because the release App bypasses the
    branch rules that would otherwise enforce this. The
    push is a plain fast-forward (GitHub then marks the PR as merged), so the
    merge relation survives; squash would drop it and re-open the gap.
    """
    pr = gh.get_pr(number)
    if pr.state != "open":
        raise ReleaseError(f"PR #{number} 不是打开状态")
    if FORWARD_LABEL not in pr.labels:
        raise ReleaseError(f"PR #{number} 没有 {FORWARD_LABEL} 标签")
    state = git.state()
    sealed = gh.sealed_branches()
    if pr.base in sealed:
        raise ReleaseError(f"{pr.base} 已封版，不能作为上合目标")
    target_sha = state.master if pr.base == MASTER else _branch_sha(state, pr.base)
    git.fetch_pr(number, pr.head_ref)
    head = pr.head_sha
    if not git.is_ancestor(target_sha, head):
        raise ReleaseError(
            f"PR #{number} 未包含 {pr.base} 当前的 {target_sha[:12]}，请先合入最新目标"
        )
    sources = [
        v for v in state.branches if next_receivable(v, state, sealed) == _version_or_none(pr.base)
    ]
    if not any(
        git.is_ancestor(state.branches[v], head)
        for v in sources
        if gh.find_issue(Hop(v, _version_or_none(pr.base)).marker)
    ):
        raise ReleaseError(f"PR #{number} 没有包含任何处于冲突暂停的来源分支")
    problems = gh.review_gate(number, head)
    if problems:
        raise ReleaseError(f"PR #{number} 不能落地：" + "；".join(problems))
    if not gh.checks_passed(head, REQUIRED_CHECKS):
        raise ReleaseError(f"PR #{number} 的 {', '.join(REQUIRED_CHECKS)} 尚未全部通过")
    latest = gh.get_pr(number)
    if latest.head_sha != head or latest.state != "open" or latest.base != pr.base:
        raise ReleaseError(
            f"PR #{number} 在检查期间发生变化（head/base/状态），未落地；下次对账重新检查"
        )
    if not git.push_ref(head, f"refs/heads/{pr.base}"):
        raise ReleaseError(f"快进 {pr.base} 失败（目标已变化），请合入最新目标后重试")
    return head


def _branch_sha(state: RepoState, branch: str) -> str:
    version = _version_or_none(branch)
    if version is None or version not in state.branches:
        raise ReleaseError(f"{branch} 不是存在的版本分支")
    return state.branches[version]


def _version_or_none(branch: str) -> Version | None:
    return None if branch == MASTER else Version.parse(branch)
