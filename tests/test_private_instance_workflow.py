from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "examples" / "private-instance" / "scout.yml"
ACTION = ROOT / "action.yml"


def document(path: Path) -> dict[str, Any]:
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")))


def shell_step(
    step: dict[str, Any], cwd: Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=cwd,
        env={**os.environ, **environment},
        capture_output=True,
        text=True,
        check=False,
    )


class PrivateInstanceContractTests(unittest.TestCase):
    def test_parsed_workflow_ownership_permissions_serialization_and_pins(self) -> None:
        workflow = document(TEMPLATE)
        self.assertEqual(workflow["on"], {"workflow_dispatch": None})
        self.assertEqual(workflow["permissions"], {})
        self.assertEqual(
            workflow["concurrency"],
            {"group": "scout-seen-state", "cancel-in-progress": False, "queue": "max"},
        )
        self.assertEqual(set(workflow["jobs"]), {"scout"})
        job = workflow["jobs"]["scout"]
        self.assertEqual(job["permissions"], {"contents": "write"})
        self.assertEqual(
            job["env"]["INSTANCE_BRANCH"], "${{ github.event.repository.default_branch }}"
        )
        steps = job["steps"]
        self.assertEqual(
            [step.get("id") for step in steps],
            [None, None, "base", "scan", "persist", "recovery", None],
        )
        self.assertEqual(steps[1]["with"]["ref"], "${{ github.event.repository.default_branch }}")
        scan = steps[3]
        self.assertEqual(scan["with"]["config-path"], "scout.toml")
        self.assertEqual(scan["with"]["state-path"], "seen_bounties.json")
        self.assertEqual(scan["with"]["github-token"], "${{ github.token }}")
        self.assertEqual(
            scan["with"]["private-reports-token"], "${{ secrets.PRIVATE_GITHUB_REPORTS_TOKEN }}"
        )
        self.assertEqual(
            scan["with"]["private-reports-repository"],
            "${{ secrets.PRIVATE_GITHUB_REPORTS_REPOSITORY }}",
        )
        self.assertNotIn("GITHUB_REPORTS_ENABLED", str(workflow))
        self.assertTrue(steps[4]["continue-on-error"])
        self.assertNotIn("continue-on-error", scan)
        self.assertNotIn("if", steps[4])  # Default success() skips persistence after scan failure.
        self.assertEqual(steps[4]["env"]["SCOUT_BASE_SHA"], "${{ steps.base.outputs.sha }}")
        recovery = steps[5]
        self.assertEqual(
            recovery["if"],
            "${{ always() && steps.persist.outcome == 'failure' && github.event.repository.private == true }}",
        )
        self.assertEqual(recovery["with"]["path"], "seen_bounties.json")
        self.assertEqual(recovery["with"]["retention-days"], 3)
        self.assertEqual(recovery["with"]["if-no-files-found"], "error")
        self.assertIn("github.run_attempt", recovery["with"]["name"])
        self.assertEqual(steps[6]["if"], "${{ always() && steps.persist.outcome == 'failure' }}")
        self.assertNotIn("--force", steps[4]["run"])
        for step in steps + document(ACTION)["runs"]["steps"]:
            if "uses" in step:
                self.assertRegex(step["uses"], r"^[\w/-]+@[0-9a-f]{40}$")
            else:
                self.assertEqual(step["shell"], "bash")
                self.assertNotIn("${{", step["run"])
                result = subprocess.run(
                    ["bash", "-n"], input=step["run"], capture_output=True, text=True
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_private_and_default_branch_guards(self) -> None:
        step = document(TEMPLATE)["jobs"]["scout"]["steps"][0]
        for private, ref, expected in [
            ("true", "refs/heads/main", 0),
            ("false", "refs/heads/main", 1),
            ("", "refs/heads/main", 1),
            ("true", "refs/heads/other", 1),
        ]:
            with self.subTest(private=private, ref=ref):
                result = shell_step(
                    step,
                    ROOT,
                    {"INSTANCE_PRIVATE": private, "GITHUB_REF": ref, "INSTANCE_BRANCH": "main"},
                )
                self.assertEqual(result.returncode, expected)

    def test_persistence_failure_always_fails_after_recovery_attempt(self) -> None:
        step = document(TEMPLATE)["jobs"]["scout"]["steps"][-1]
        for outcome in ("success", "failure", "skipped", "cancelled", ""):
            with self.subTest(outcome=outcome):
                result = shell_step(step, ROOT, {"RECOVERY_OUTCOME": outcome})
                self.assertEqual(result.returncode, 1)
                self.assertIn("before rerunning", result.stdout)
                self.assertIn("Restore" if outcome == "success" else "Reconstruct", result.stdout)


class PinnedActionTests(unittest.TestCase):
    def test_explicit_paths_and_separate_credentials(self) -> None:
        action = document(ACTION)
        self.assertEqual(action["runs"]["using"], "composite")
        for name in ("config-path", "state-path", "github-token"):
            self.assertTrue(action["inputs"][name]["required"])
        env = action["runs"]["steps"][1]["env"]
        self.assertEqual(env["SCANNER_SOURCE"], "${{ github.action_path }}")
        self.assertEqual(env["GITHUB_TOKEN"], "${{ inputs.github-token }}")
        self.assertEqual(env["PRIVATE_GITHUB_REPORTS_TOKEN"], "${{ inputs.private-reports-token }}")
        self.assertEqual(env["GITHUB_REPORTS_ENABLED"], "false")
        self.assertEqual(action["runs"]["steps"][1]["working-directory"], "${{ github.workspace }}")
        for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK_URL"):
            self.assertIn("inputs.", env[name])

    def test_executes_action_source_with_quoted_paths_and_propagates_failure(self) -> None:
        step = document(ACTION)["runs"]["steps"][1]
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "pinned source"
            caller = folder / "caller"
            source.mkdir()
            caller.mkdir()
            (caller / "opportunity_scout.py").write_text(
                "raise AssertionError('caller source executed')\n"
            )
            (caller / "json.py").write_text("raise AssertionError('caller module imported')\n")
            (source / "opportunity_scout.py").write_text(
                "import json, os, sys\n"
                "from pathlib import Path\n"
                "Path('capture.json').write_text(json.dumps({'argv': sys.argv[1:], 'source': __file__, 'tokens': [os.environ['GITHUB_TOKEN'], os.environ['PRIVATE_GITHUB_REPORTS_TOKEN']]}))\n"
                "sys.exit(int(os.environ['SCANNER_EXIT']))\n"
            )
            environment = {
                "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}",
                "SCANNER_SOURCE": str(source),
                "SCOUT_CONFIG": "config space;$(false).toml",
                "SCOUT_STATE": "state space;$(false).json",
                "GITHUB_TOKEN": "fake-discovery",
                "PRIVATE_GITHUB_REPORTS_TOKEN": "fake-report",
                "PYTHONPATH": str(caller),
            }
            for code in (0, 23):
                result = shell_step(step, caller, {**environment, "SCANNER_EXIT": str(code)})
                self.assertEqual(result.returncode, code, result.stderr)
                capture = json.loads((caller / "capture.json").read_text())
                self.assertEqual(
                    capture["argv"],
                    [
                        "--config",
                        environment["SCOUT_CONFIG"],
                        "--state",
                        environment["SCOUT_STATE"],
                    ],
                )
                self.assertEqual(capture["source"], str(source / "opportunity_scout.py"))
                self.assertEqual(capture["tokens"], ["fake-discovery", "fake-report"])

    def test_real_action_invalid_config_fails_before_state_or_network(self) -> None:
        step = document(ACTION)["runs"]["steps"][1]
        with tempfile.TemporaryDirectory() as temporary:
            caller = Path(temporary)
            snapshot = b"private-state-must-not-be-read-or-replaced"
            (caller / "seen.json").write_bytes(snapshot)
            (caller / "bad.toml").write_text("unknown = true\n")
            result = shell_step(
                step,
                caller,
                {
                    "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}",
                    "SCANNER_SOURCE": str(ROOT),
                    "SCOUT_CONFIG": "bad.toml",
                    "SCOUT_STATE": "seen.json",
                    "PYTHONPATH": str(caller),
                },
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("ScoutPreferencesError", result.stderr)
            self.assertEqual((caller / "seen.json").read_bytes(), snapshot)

    def test_real_action_valid_config_custom_state_and_corruption(self) -> None:
        step = document(ACTION)["runs"]["steps"][1]
        with tempfile.TemporaryDirectory() as temporary:
            caller = Path(temporary)
            (caller / "offline.toml").write_text(
                "version = 1\n[lanes]\npaid = false\nstrategic = false\n"
            )
            environment = {
                "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}",
                "SCANNER_SOURCE": str(ROOT),
                "SCOUT_CONFIG": "offline.toml",
                "SCOUT_STATE": "custom state.json",
                "GITHUB_TOKEN": "",
                "PRIVATE_GITHUB_REPORTS_TOKEN": "",
                "GITHUB_REPORTS_ENABLED": "false",
            }
            for snapshot, expected in (
                (b'{"version":2,"seen":{}}\n', 0),
                (b"corrupt-existing-state", 1),
            ):
                with self.subTest(expected=expected):
                    (caller / "custom state.json").write_bytes(snapshot)
                    result = shell_step(step, caller, environment)
                    self.assertEqual(result.returncode, expected, result.stderr)
                    self.assertEqual((caller / "custom state.json").read_bytes(), snapshot)
                    self.assertFalse((caller / "seen_bounties.json").exists())
                    if expected:
                        self.assertIn("SeenStateLoadError", result.stderr)
                    else:
                        self.assertIn("No new verified OSS opportunities", result.stdout)


class StateTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.remote = self.folder / "remote.git"
        self.repo = self.folder / "instance"
        self.other = self.folder / "writer"
        self.environment = {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "INSTANCE_BRANCH": "main",
        }
        self.git(self.folder, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.git(self.folder, "clone", str(self.remote), str(self.repo))
        self.git(self.repo, "config", "user.name", "Offline Test")
        self.git(self.repo, "config", "user.email", "offline@example.invalid")
        (self.repo / "scout.toml").write_text("schema_version = 1\n")
        (self.repo / "seen_bounties.json").write_bytes(b'{"old": "state"}\n')
        self.git(self.repo, "add", ".")
        self.git(self.repo, "commit", "-m", "seed")
        self.git(self.repo, "push", "origin", "main")
        self.git(self.folder, "clone", str(self.remote), str(self.other))
        self.git(self.other, "config", "user.name", "Other Writer")
        self.git(self.other, "config", "user.email", "other@example.invalid")
        steps = document(TEMPLATE)["jobs"]["scout"]["steps"]
        self.restore = steps[2]
        self.persist = steps[4]
        self.output = self.folder / "output"
        self.environment["GITHUB_OUTPUT"] = str(self.output)

    def git(self, cwd: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            env={**os.environ, **self.environment},
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def read_base(self) -> str:
        self.output.unlink(missing_ok=True)
        result = shell_step(self.restore, self.repo, self.environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        base = self.output.read_text().strip().removeprefix("sha=")
        self.assertRegex(base, r"^[0-9a-f]{40}$")
        self.environment["SCOUT_BASE_SHA"] = base
        return base

    def advance_other_writer(self) -> str:
        (self.other / "unrelated.txt").write_text("intervening private instance update\n")
        self.git(self.other, "add", ".")
        self.git(self.other, "commit", "-m", "intervening update")
        self.git(self.other, "push", "origin", "main")
        return self.git(self.other, "rev-parse", "HEAD")

    def test_reads_latest_state_after_queue_instead_of_dispatch_snapshot(self) -> None:
        (self.other / "seen_bounties.json").write_bytes(b'{"newer": "state"}\n')
        head = self.advance_other_writer()
        self.assertEqual(self.read_base(), head)
        self.assertEqual((self.repo / "seen_bounties.json").read_bytes(), b'{"newer": "state"}\n')

    def test_restore_requires_config_and_state_and_rejects_symlinks(self) -> None:
        for name in ("scout.toml", "seen_bounties.json"):
            for symlink in (False, True):
                with self.subTest(name=name, symlink=symlink):
                    self.git(self.other, "reset", "--hard", "origin/main")
                    path = self.other / name
                    path.unlink()
                    if symlink:
                        path.symlink_to(
                            "seen_bounties.json" if name == "scout.toml" else "scout.toml"
                        )
                    self.git(self.other, "add", ".")
                    self.git(self.other, "commit", "-m", "bad instance")
                    self.git(self.other, "push", "origin", "main")
                    result = shell_step(self.restore, self.repo, self.environment)
                    self.assertNotEqual(result.returncode, 0)
                    # Restore the valid seed for the next independent case.
                    self.git(self.other, "revert", "--no-edit", "HEAD")
                    self.git(self.other, "push", "origin", "main")

    def test_no_change_no_push_and_successful_exact_state_persistence(self) -> None:
        base = self.read_base()
        result = shell_step(self.persist, self.repo, self.environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), base)
        snapshot = b'{"resulting": "delivered state", "preserve": "bytes"}\n\n'
        (self.repo / "seen_bounties.json").write_bytes(snapshot)
        result = shell_step(self.persist, self.repo, self.environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        remote_state = subprocess.run(
            ["git", "show", "main:seen_bounties.json"],
            cwd=self.remote,
            capture_output=True,
            check=True,
        ).stdout
        self.assertEqual(remote_state, snapshot)
        self.assertEqual(
            self.git(self.repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"),
            "seen_bounties.json",
        )

    def test_stale_state_is_rejected_and_exact_recovery_file_survives(self) -> None:
        self.read_base()
        snapshot = b'{"resulting": "delivered state"}\n\n'
        (self.repo / "seen_bounties.json").write_bytes(snapshot)
        head = self.advance_other_writer()
        result = shell_step(self.persist, self.repo, self.environment)
        self.assertEqual(result.returncode, 1)
        self.assertIn("changed during scan", result.stdout)
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), head)
        self.assertEqual((self.repo / "seen_bounties.json").read_bytes(), snapshot)

    def test_fetch_and_push_failures_preserve_exact_result_for_artifact(self) -> None:
        for failure in ("fetch", "push", "commit"):
            with self.subTest(failure=failure):
                self.read_base()
                snapshot = b'{"delivered": "must survive"}\n\n'
                (self.repo / "seen_bounties.json").write_bytes(snapshot)
                if failure == "fetch":
                    self.git(
                        self.repo, "remote", "set-url", "origin", str(self.folder / "absent.git")
                    )
                elif failure == "push":
                    hook = self.remote / "hooks" / "pre-receive"
                    hook.write_text("#!/bin/sh\nexit 1\n")
                    hook.chmod(0o755)
                else:
                    hook = self.repo / ".git" / "hooks" / "pre-commit"
                    hook.write_text("#!/bin/sh\nexit 1\n")
                    hook.chmod(0o755)
                result = shell_step(self.persist, self.repo, self.environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((self.repo / "seen_bounties.json").read_bytes(), snapshot)
                self.git(self.repo, "remote", "set-url", "origin", str(self.remote))
                for hook in (
                    self.remote / "hooks" / "pre-receive",
                    self.repo / ".git" / "hooks" / "pre-commit",
                ):
                    hook.unlink(missing_ok=True)

    def test_remote_race_after_fetch_is_rejected_without_overwriting_state(self) -> None:
        self.read_base()
        snapshot = b'{"delivered": "race recovery"}\n'
        (self.repo / "seen_bounties.json").write_bytes(snapshot)
        hook = self.repo / ".git" / "hooks" / "pre-push"
        # Simulate another actor advancing the branch after the persistence fetch.
        # Updating the bare ref inside pre-push guarantees the race is exercised.
        seed = self.git(self.remote, "rev-parse", "main")
        tree = self.git(self.remote, "rev-parse", "main^{tree}")
        result = subprocess.run(
            [
                "git",
                "-c",
                "user.name=Race",
                "-c",
                "user.email=race@example.invalid",
                "commit-tree",
                tree,
                "-p",
                seed,
                "-m",
                "race",
            ],
            cwd=self.remote,
            env={**os.environ, **self.environment},
            capture_output=True,
            text=True,
            check=True,
        )
        race = result.stdout.strip()
        hook.write_text(
            f'#!/bin/sh\ngit --git-dir="{self.remote}" update-ref refs/heads/main {race} {seed}\n'
        )
        hook.chmod(0o755)
        result = shell_step(self.persist, self.repo, self.environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), race)
        self.assertEqual((self.repo / "seen_bounties.json").read_bytes(), snapshot)
