from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

import yaml

from opportunity_scout import preferences, state

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
