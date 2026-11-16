"""deploy/upgrade.sh against fake docker / curl executables (no daemon, no network)."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "upgrade.sh"
OLD = "a" * 40
NEW = "b" * 40

FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_LOG"
tag="${AKKIABOT_TAG:-$(sed -n 's/^AKKIABOT_TAG=//p' .env)}"
case "$*" in
  "compose pull"*) [[ -z "${FAKE_PULL_FAIL:-}" ]] ;;
  "compose exec -T postgres pg_dump"*) [[ -z "${FAKE_BACKUP_FAIL:-}" ]] && echo dump ;;
  "compose up"*) [[ "$tag" != "${FAKE_BAD_TAG:-none}" ]] && echo "$tag" > .running ;;
  *) exit 0 ;;
esac
"""

FAKE_CURL = r"""#!/usr/bin/env bash
out=""; url=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    --retry|--max-time) shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
if [[ "$url" == */readyz ]]; then
  [[ -f .running && "$(cat .running)" != "${FAKE_UNHEALTHY_TAG:-none}" ]]
  exit
fi
path="${url#*/deploy/}"
[[ -f "$FAKE_SRC/$path" ]] || exit 22
cp "$FAKE_SRC/$path" "$out"
"""

FAKE_OP = r"""#!/usr/bin/env bash
# op inject -f -i <in> -o <out>; like the real op, it drains a piped stdin
[[ -t 0 ]] || cat >/dev/null
cp "$4" "$6"
"""


@pytest.fixture
def stack(tmp_path: Path) -> dict[str, Path]:
    src = tmp_path / "src"
    (src / "observability").mkdir(parents=True)
    (src / "staging").mkdir()
    for name in ("compose.yaml", "backup.sh", "restore.sh", ".env.example", "app.env.example"):
        shutil.copy(REPO_ROOT / "deploy" / name, src / name)
    shutil.copy(REPO_ROOT / "deploy/observability/config.alloy", src / "observability/config.alloy")
    (src / "staging/app.env.tpl").write_text("TELEGRAM_BOT_TOKEN={{ op://vault/item/field }}\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL), ("op", FAKE_OP)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    env_dir = tmp_path / "stack"
    env_dir.mkdir()
    env_text = (
        (src / ".env.example").read_text().replace("AKKIABOT_TAG=latest", f"AKKIABOT_TAG={OLD}")
    )
    (env_dir / ".env").write_text(env_text)
    shutil.copy(src / "app.env.example", env_dir / "app.env")
    shutil.copy(src / "compose.yaml", env_dir / "compose.yaml")
    shutil.copy(src / "backup.sh", env_dir / "backup.sh")
    (env_dir / ".running").write_text(OLD)
    return {"src": src, "bin": bin_dir, "dir": env_dir, "log": tmp_path / "docker.log"}


def run(stack: dict[str, Path], *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    full_env = {
        "PATH": f"{stack['bin']}:{os.environ['PATH']}",
        "FAKE_SRC": str(stack["src"]),
        "FAKE_LOG": str(stack["log"]),
        "UPGRADE_HEALTH_TRIES": "2",
        "UPGRADE_HEALTH_DELAY": "0",
        "HOME": str(stack["dir"]),
        **env,
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=stack["dir"],
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )


def tag_of(stack: dict[str, Path]) -> str:
    for line in (stack["dir"] / ".env").read_text().splitlines():
        if line.startswith("AKKIABOT_TAG="):
            return line.split("=", 1)[1]
    raise AssertionError("no AKKIABOT_TAG")


def log(stack: dict[str, Path]) -> str:
    return (stack["dir"] / "releases.log").read_text()


def test_upgrade_success_and_idempotent_retry(stack: dict[str, Path]) -> None:
    proc = run(stack, NEW, "--migrations", "unchanged", "--release", "v1.0.0")
    assert proc.returncode == 0, proc.stderr
    assert tag_of(stack) == NEW
    assert (stack["dir"] / ".running").read_text().strip() == NEW
    assert list((stack["dir"] / "backups").glob("akkiabot-*.dump"))
    assert "\tv1.0.0\t" + OLD + "\t" + NEW in log(stack)
    assert log(stack).rstrip().endswith("ok")
    assert oct((stack["dir"] / "releases.log").stat().st_mode & 0o777) == "0o600"

    again = run(stack, NEW, "--migrations", "unchanged", "--release", "v1.0.0")
    assert again.returncode == 0
    assert "already running" in again.stdout
    assert log(stack).rstrip().endswith("already-current")


def test_missing_configuration_changes_nothing(stack: dict[str, Path]) -> None:
    env = stack["dir"] / ".env"
    env.write_text(
        "\n".join(
            line
            for line in env.read_text().splitlines()
            if not line.startswith("GRAFANA_CLOUD_TOKEN=")
        )
    )
    proc = run(stack, NEW, "--migrations", "unchanged")
    assert proc.returncode == 3
    assert "GRAFANA_CLOUD_TOKEN" in proc.stderr
    assert tag_of(stack) == OLD
    assert not (stack["dir"] / "backups").exists()


def test_migration_needs_confirmation(stack: dict[str, Path]) -> None:
    refused = run(stack, NEW, "--migrations", "changed")
    assert refused.returncode == 3
    assert "--allow-migration" in refused.stderr
    assert tag_of(stack) == OLD
    allowed = run(stack, NEW, "--migrations", "changed", "--allow-migration")
    assert allowed.returncode == 0
    assert tag_of(stack) == NEW


def test_pull_or_backup_failure_changes_nothing(stack: dict[str, Path]) -> None:
    assert run(stack, NEW, "--migrations", "unchanged", FAKE_PULL_FAIL="1").returncode == 4
    assert tag_of(stack) == OLD
    assert run(stack, NEW, "--migrations", "unchanged", FAKE_BACKUP_FAIL="1").returncode == 5
    assert tag_of(stack) == OLD
    assert "pull-failed" in log(stack)
    assert "backup-failed" in log(stack)


def test_partial_failure_without_migration_rolls_back(stack: dict[str, Path]) -> None:
    proc = run(stack, NEW, "--migrations", "unchanged", FAKE_BAD_TAG=NEW)
    assert proc.returncode == 7, proc.stderr
    assert tag_of(stack) == OLD
    assert (stack["dir"] / ".running").read_text().strip() == OLD
    assert log(stack).rstrip().endswith("failed-rolled-back")
    # Retry of the same version once the cause is fixed.
    assert run(stack, NEW, "--migrations", "unchanged").returncode == 0
    assert tag_of(stack) == NEW


def test_unhealthy_after_migration_is_left_for_a_person(stack: dict[str, Path]) -> None:
    proc = run(stack, NEW, "--migrations", "changed", "--allow-migration", FAKE_UNHEALTHY_TAG=NEW)
    assert proc.returncode == 6
    assert "NOT rolling back" in proc.stderr
    assert "§5.2" in proc.stderr
    assert tag_of(stack) == NEW  # no automatic restore of anything
    assert "restore" not in stack["log"].read_text()


def test_rollback_failure_is_reported(stack: dict[str, Path]) -> None:
    proc = run(stack, NEW, "--migrations", "unchanged", FAKE_UNHEALTHY_TAG=NEW, FAKE_BAD_TAG=OLD)
    assert proc.returncode == 8
    assert log(stack).rstrip().endswith("failed-rollback-failed")


def test_staging_template_is_rendered(stack: dict[str, Path]) -> None:
    proc = run(stack, NEW, "--migrations", "unchanged", "--render-template", "staging/app.env.tpl")
    assert proc.returncode == 0, proc.stderr
    assert "op://" in (stack["dir"] / "app.env").read_text()  # fake op copies the template


def test_bad_arguments() -> None:
    assert (
        subprocess.run(
            ["bash", str(SCRIPT), "abc", "--migrations", "unchanged"],
            check=False,
            capture_output=True,
        ).returncode
        == 2
    )
    assert (
        subprocess.run(["bash", str(SCRIPT), NEW], check=False, capture_output=True).returncode == 2
    )


def test_upgrade_piped_on_stdin_runs_to_the_end(stack: dict[str, Path]) -> None:
    """`deploy` pipes the script into `bash -s`; a command reading stdin (op inject)
    must not swallow the rest of the script (BOT-40: it ended silently with 0)."""
    full_env = {
        "PATH": f"{stack['bin']}:{os.environ['PATH']}",
        "FAKE_SRC": str(stack["src"]),
        "FAKE_LOG": str(stack["log"]),
        "UPGRADE_HEALTH_TRIES": "2",
        "UPGRADE_HEALTH_DELAY": "0",
        "HOME": str(stack["dir"]),
    }
    args = [NEW, "--migrations", "unchanged", "--release", "v1.0.0-rc.1"]
    proc = subprocess.run(
        ["bash", "-s", "--", *args, "--render-template", "staging/app.env.tpl"],
        input=SCRIPT.read_text(),
        cwd=stack["dir"],
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert tag_of(stack) == NEW
    assert (stack["dir"] / ".running").read_text().strip() == NEW
    assert log(stack).rstrip().endswith("ok")
