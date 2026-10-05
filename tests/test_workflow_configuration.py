from __future__ import annotations

import os
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
        self.assertEqual(set(workflow["jobs"]), {"branch-flow", "quality", "compatibility"})
        branch_flow = workflow["jobs"]["branch-flow"]
        self.assertEqual(branch_flow["name"], "branch-flow")
        self.assertNotIn("permissions", branch_flow)
        self.assertEqual(len(branch_flow["steps"]), 1)
        branch_flow_step = branch_flow["steps"][0]
        self.assertNotIn("uses", branch_flow_step)
        self.assertEqual(
            set(branch_flow_step["env"]),
            {
                "EVENT_NAME",
                "BASE_REF",
                "HEAD_REF",
                "HEAD_REPOSITORY",
                "CURRENT_REPOSITORY",
            },
        )
        for job_name in ("quality", "compatibility"):
            job = workflow["jobs"][job_name]
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

    def test_quality_workflow_runs_on_main_and_dev_pushes(self) -> None:
        workflow = yaml.load(
            (ROOT / ".github" / "workflows" / "python-quality.yml").read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )
        push = workflow["on"]["push"]
        self.assertEqual(push["branches"], ["main", "dev"])
        self.assertEqual(
            push["paths"],
            [
                "**/*.py",
                ".coveragerc",
                "mypy.ini",
                "ruff.toml",
                "requirements-dev.txt",
                "Makefile",
                ".github/workflows/python-quality.yml",
                "action.yml",
                "examples/**",
                "scout.example.toml",
                ".gitignore",
            ],
        )

    def _run_branch_flow(
        self,
        *,
        event_name: str,
        base_ref: str,
        head_ref: str,
        head_repository: str = "laclance/OSSOpportunityScout",
        current_repository: str = "laclance/OSSOpportunityScout",
    ) -> subprocess.CompletedProcess[str]:
        workflow = yaml.load(
            (ROOT / ".github" / "workflows" / "python-quality.yml").read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )
        script = workflow["jobs"]["branch-flow"]["steps"][0]["run"]
        return subprocess.run(
            ["bash", "-eu", "-o", "pipefail", "-c", script],
            cwd=ROOT,
            env={
                **os.environ,
                "EVENT_NAME": event_name,
                "BASE_REF": base_ref,
                "HEAD_REF": head_ref,
                "HEAD_REPOSITORY": head_repository,
                "CURRENT_REPOSITORY": current_repository,
            },
            capture_output=True,
            text=True,
            check=False,
        )

    def test_branch_flow_accepts_topic_pull_request_to_dev(self) -> None:
        result = self._run_branch_flow(
            event_name="pull_request",
            base_ref="dev",
            head_ref="feature/example",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_branch_flow_accepts_dev_promotion_to_main(self) -> None:
        result = self._run_branch_flow(
            event_name="pull_request",
            base_ref="main",
            head_ref="dev",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_branch_flow_rejects_fork_dev_promotion_to_main(self) -> None:
        result = self._run_branch_flow(
            event_name="pull_request",
            base_ref="main",
            head_ref="dev",
            head_repository="someone/fork",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "Pull requests to main must come from this repository's dev branch.",
            result.stdout + result.stderr,
        )

    def test_branch_flow_rejects_topic_pull_requests_to_main(self) -> None:
        for head_ref in ("feature/example", "fix/example", "dev/example"):
            with self.subTest(head_ref=head_ref):
                result = self._run_branch_flow(
                    event_name="pull_request",
                    base_ref="main",
                    head_ref=head_ref,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "Pull requests to main must come from this repository's dev branch.",
                    result.stdout + result.stderr,
                )

    def test_branch_flow_succeeds_on_non_pull_request_events(self) -> None:
        result = self._run_branch_flow(
            event_name="push",
            base_ref="",
            head_ref="",
            head_repository="",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

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

    def test_mutable_external_reusable_workflow_is_rejected(self) -> None:
        workflow = {
            "jobs": {
                "reusable": {
                    "uses": "example/example/.github/workflows/ci.yml@v1",
                }
            }
        }
        with self.assertRaisesRegex(ValueError, "reusable workflow"):
            validate_workflow_references(workflow)

    def test_expression_reusable_workflow_is_rejected(self) -> None:
        workflow = {
            "jobs": {
                "reusable": {
                    "uses": "${{ inputs.workflow_reference }}",
                }
            }
        }
        with self.assertRaisesRegex(ValueError, "reusable workflow"):
            validate_workflow_references(workflow)

    def test_external_reusable_workflow_full_sha_is_accepted(self) -> None:
        workflow = {
            "jobs": {
                "reusable": {
                    "uses": (
                        "example/example/.github/workflows/ci.yml@"
                        "0123456789abcdef0123456789abcdef01234567"
                    ),
                }
            }
        }
        validate_workflow_references(workflow)

    def test_same_repository_reusable_workflows_are_accepted(self) -> None:
        for reference in (
            "./.github/workflows/local.yml",
            "$/.github/workflows/local.yml",
        ):
            with self.subTest(reference=reference):
                validate_workflow_references({"jobs": {"reusable": {"uses": reference}}})

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
