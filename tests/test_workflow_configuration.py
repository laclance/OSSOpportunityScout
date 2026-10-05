from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

import yaml

from opportunity_scout import preferences, state
from tests.workflow_references import validate_workflow_references

ROOT = Path(__file__).resolve().parents[1]


class UpstreamDistributionTests(unittest.TestCase):
    def test_active_workflows_only_run_development_ci_with_read_permissions(self) -> None:
        workflows = sorted((ROOT / ".github" / "workflows").iterdir())
        self.assertEqual([path.name for path in workflows], ["python-quality.yml"])
        # BaseLoader retains GitHub's unquoted `on` key as text, not a YAML 1.1 boolean.
        workflow = yaml.load(workflows[0].read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow["on"]), {"pull_request", "push"})
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(set(workflow["jobs"]), {"quality", "compatibility"})
        for job in workflow["jobs"].values():
            self.assertNotIn("permissions", job)
            for step in job["steps"]:
                self.assertNotIn("env", step)
                if "run" in step:
                    for forbidden in (
                        "opportunity_scout.py --config",
                        "python opportunity_scout.py",
                        "scout-state",
                        "git worktree",
                        "git push",
                        "secrets.",
                    ):
                        self.assertNotIn(forbidden, step["run"])
        self.assertNotIn("oss-opportunity-scout.yml", str(workflow))

    def test_active_workflow_external_actions_use_approved_immutable_shas(self) -> None:
        expected = {
            "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
            "actions/setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97",
        }
        workflow = yaml.load(
            (ROOT / ".github" / "workflows" / "python-quality.yml").read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )
        validate_workflow_references(workflow, approved_actions=expected)

    def test_quality_toolchain_uses_known_green_exact_direct_pins(self) -> None:
        expected = {
            "coverage": "7.16.2",
            "mypy": "2.4.0",
            "ruff": "0.16.9",
            "PyYAML": "6.0.3",
            "types-PyYAML": "6.0.12.20260906",
        }
        requirements = {}
        for line in (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines():
            name, separator, version = line.partition("==")
            self.assertEqual(separator, "==", line)
            requirements[name] = version
        self.assertEqual(requirements, expected)

        workflow = yaml.load(
            (ROOT / ".github" / "workflows" / "python-quality.yml").read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )
        install_step = next(
            step
            for step in workflow["jobs"]["compatibility"]["steps"]
            if step["name"] == "Install offline workflow validation dependency"
        )
        self.assertEqual(
            install_step["run"],
            f"python -m pip install PyYAML=={requirements['PyYAML']}",
        )

    def test_instance_files_are_untracked_and_ignored_while_examples_remain_generic(self) -> None:
        tracked = subprocess.run(
            ["git", "ls-files", "--", "scout.toml", "seen_bounties.json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(tracked.stdout, "")
        for name in ("scout.toml", "seen_bounties.json"):
            result = subprocess.run(
                ["git", "check-ignore", "--no-index", name],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), name)
        self.assertEqual(
            state.load_seen_state(ROOT / "examples" / "seen_bounties.example.json").urls(),
            set(),
        )
        self.assertEqual(
            preferences.load_scout_preferences(ROOT / "scout.example.toml"),
            preferences.ScoutPreferences(),
        )
        self.assertTrue((ROOT / "action.yml").is_file())
        self.assertTrue((ROOT / "examples" / "private-instance" / "scout.yml").is_file())


if __name__ == "__main__":
    unittest.main()
