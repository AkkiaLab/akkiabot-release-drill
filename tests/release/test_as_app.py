"""scripts/release/as-app.sh with fake op / curl (no 1Password, no network).

The private key below is generated for the test (a MOCK key, never used anywhere
else). Checks that no key material is left on disk on either exit path and that
no credential appears in any command's arguments (BOT-40 review of PR #41).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/release/as-app.sh"
MOCK_INSTALLATION_TOKEN = "mock-installation-token-not-real"

# op read <ref>: the folded PEM of a hidden field (line breaks -> spaces), as in 1Password
FAKE_OP = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >>"$FAKE_ARGS"
case "$2" in
  */app_id) echo 4242 ;;
  */private_key) tr '\n' ' ' <"$MOCK_KEY" ;;
esac
"""

# curl -sSf -K - ... <url>: records argv and the config read from stdin
FAKE_CURL = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >>"$FAKE_ARGS"
cat >>"$FAKE_STDIN"
[[ "${FAKE_CURL_FAIL:-}" == 1 ]] && exit 22
case "${@: -1}" in
  */installation) echo '{"id": 7}' ;;
  */access_tokens) echo '{"token": "MOCK_TOKEN"}' ;;
esac
"""


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    if shutil.which("openssl") is None:
        pytest.skip("openssl not installed")
    mock_key = tmp_path / "mock-app-key.pem"
    subprocess.run(
        [
            "openssl",
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            str(mock_key),
        ],
        check=True,
        capture_output=True,
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("op", FAKE_OP), ("curl", FAKE_CURL)):
        (bin_dir / name).write_text(body.replace("MOCK_TOKEN", MOCK_INSTALLATION_TOKEN))
        (bin_dir / name).chmod(0o755)
    token_file = tmp_path / "mock-op-token"
    token_file.write_text("mock-op-service-account-token\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    return {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "TMPDIR": str(scratch),
        "AKKIABOT_OP_TOKEN_FILE": str(token_file),
        "MOCK_KEY": str(mock_key),
        "FAKE_ARGS": str(tmp_path / "args.log"),
        "FAKE_STDIN": str(tmp_path / "stdin.log"),
        "RELEASE_REPO": "AkkiaLab/mock-repo",
    }


def run(env: dict[str, str], *command: str, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *command],
        env={**env, **extra},
        capture_output=True,
        text=True,
        check=False,
    )


def _no_key_left(env: dict[str, str]) -> None:
    assert list(Path(env["TMPDIR"]).iterdir()) == []


def test_success_hands_token_via_environment_only(env: dict[str, str]) -> None:
    proc = run(
        env, "bash", "-c", 'echo "$GH_TOKEN|${OP_SERVICE_ACCOUNT_TOKEN:-unset}|$GIT_CONFIG_KEY_0"'
    )
    assert proc.returncode == 0, proc.stderr
    _no_key_left(env)
    assert proc.stdout.strip() == (
        f"{MOCK_INSTALLATION_TOKEN}|unset|http.https://github.com/.extraheader"
    )
    args = Path(env["FAKE_ARGS"]).read_text()
    for secret in ("Authorization", "Bearer", "BEGIN", "mock-op-service-account-token"):
        assert secret not in args
    # The JWT reached curl through its stdin config, once per API call.
    assert Path(env["FAKE_STDIN"]).read_text().count('header = "Authorization: Bearer ') == 2


def test_failure_leaves_no_key_and_does_not_run_the_command(env: dict[str, str]) -> None:
    proc = run(env, "touch", str(Path(env["HOME"]) / "ran"), FAKE_CURL_FAIL="1")
    assert proc.returncode != 0
    assert not (Path(env["HOME"]) / "ran").exists()
    _no_key_left(env)
    assert "Bearer" not in Path(env["FAKE_ARGS"]).read_text()
