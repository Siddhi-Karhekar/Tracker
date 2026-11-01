"""The CI pipeline is part of the attack surface: these keep it locked down."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def test_every_action_is_pinned_to_a_commit():
    for wf in WORKFLOWS:
        for line in wf.read_text().splitlines():
            m = re.search(r"uses:\s*(\S+)", line)
            if m:
                assert re.search(r"@[0-9a-f]{40}$", m.group(1)), f"{wf.name}: {m.group(1)} isn't pinned to a commit SHA"


def test_tokens_are_read_only_by_default_and_no_pr_runs_with_secrets():
    for wf in WORKFLOWS:
        data = yaml.safe_load(wf.read_text())
        assert data.get("permissions") == {"contents": "read"}, f"{wf.name}: top-level token must be read-only"
        triggers = data.get(True) or data.get("on")      # YAML reads the key `on` as True
        assert "pull_request_target" not in (triggers or {}), f"{wf.name}: pull_request_target runs PR code with secrets"
        assert not re.search(r"\$\{\{\s*secrets\.", wf.read_text()), f"{wf.name}: no secrets in CI"
        for job in data["jobs"].values():
            for step in job.get("steps", []):
                if "actions/checkout" in step.get("uses", ""):
                    assert step.get("with", {}).get("persist-credentials") is False, f"{wf.name}: checkout keeps the token"


def test_dependabot_watches_packages_and_actions():
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    assert {u["package-ecosystem"] for u in config["updates"]} == {"pip", "github-actions"}
