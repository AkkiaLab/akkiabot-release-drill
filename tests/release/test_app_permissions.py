"""The release-purpose token permissions are declared in three places (AKKIA-25);
they must stay identical so no path silently gets more (or less) than the others."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from scripts.reviewer_gh import ALLOWED_PERMISSIONS, PURPOSES

ROOT = Path(__file__).resolve().parents[2]


def test_release_setup_action_requests_release_permissions() -> None:
    action = yaml.safe_load((ROOT / ".github/actions/release-setup/action.yml").read_text())
    step = next(s for s in action["runs"]["steps"] if s.get("id") == "app")
    requested = {
        key.removeprefix("permission-").replace("-", "_"): value
        for key, value in step["with"].items()
        if key.startswith("permission-")
    }
    assert requested == dict(PURPOSES["release"])


def test_as_app_requests_release_permissions() -> None:
    script = (ROOT / "scripts/release/as-app.sh").read_text()
    match = re.search(r"^permissions='(\{.*\})'$", script, re.M)
    assert match
    assert json.loads(match.group(1)) == dict(PURPOSES["release"])


def test_review_purpose_cannot_merge_or_administer() -> None:
    review = PURPOSES["review"]
    assert review["contents"] == "read"
    assert "administration" not in review
    assert review.get("actions") != "write"
    merge = PURPOSES["merge"]
    assert "administration" not in merge
    assert "actions" not in merge
    # The installation may hold exactly the release set (a superset of the others).
    assert dict(ALLOWED_PERMISSIONS) == dict(PURPOSES["release"])
