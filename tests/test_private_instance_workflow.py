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

from tests.workflow_references import validate_workflow_references

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "examples" / "private-instance" / "scout.yml"
DEPENDABOT_EXAMPLE = ROOT / "examples" / "private-instance" / "dependabot.yml"
ACTION = ROOT / "action.yml"

APPROVED_DISTRIBUTED_SCANNER_SHA = "ad6cdb085bc18a2e6f1229f1a3469d2d695a0db0"


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
    def test_optional_dependabot_config_only_updates_the_scanner_action(self) -> None:
        config = document(DEPENDABOT_EXAMPLE)
        self.assertEqual(config["version"], 2)
        self.assertEqual(len(config["updates"]), 1)
        update = config["updates"][0]
        self.assertEqual(update["package-ecosystem"], "github-actions")
        self.assertEqual(update["directory"], "/")
        self.assertEqual(update["schedule"], {"interval": "weekly"})
        self.assertEqual(
            update["allow"],
            [{"dependency-name": "laclance/OSSOpportunityScout"}],
        )
        self.assertNotIn("groups", update)

        template_text = TEMPLATE.read_text(encoding="utf-8")
        self.assertNotIn("dependabot", template_text.lower())
        scanner = document(TEMPLATE)["jobs"]["scout"]["steps"][4]["uses"]
        self.assertRegex(scanner, r"^laclance/OSSOpportunityScout@[0-9a-f]{40}$")
        self.assertNotIn("@main", scanner)

    def test_parsed_workflow_ownership_permissions_serialization_and_pins(self) -> None:
        workflow = document(TEMPLATE)
        validate_workflow_references(workflow)
        self.assertEqual(workflow["on"], {"workflow_dispatch": None})
        self.assertEqual(workflow["permissions"], {})
        self.assertEqual(
            workflow["concurrency"],
            {"group": "scout-seen-state", "cancel-in-progress": False, "queue": "max"},
        )
        self.assertEqual(set(workflow["jobs"]), {"scout", "cancel_queued"})

        scout = workflow["jobs"]["scout"]
        self.assertEqual(scout["permissions"], {"contents": "write"})
        self.assertNotIn("actions", scout["permissions"])
        self.assertEqual(
            scout["outputs"],
            {
                "recovery_required": "${{ steps.recovery_handoff.outputs.required }}",
                "recovery_mode": "${{ steps.recovery_handoff.outputs.recovery_mode }}",
                "recovery_outcome": "${{ steps.recovery_handoff.outputs.recovery_outcome }}",
                "recovery_barrier_outcome": (
                    "${{ steps.recovery_handoff.outputs.recovery_barrier_outcome }}"
                ),
            },
        )
        self.assertEqual(
            scout["env"]["INSTANCE_BRANCH"], "${{ github.event.repository.default_branch }}"
        )
        self.assertNotIn("concurrency", scout)
        steps = scout["steps"]
        self.assertEqual(
            [step.get("id") for step in steps],
            [
                None,
                None,
                "base",
                "recovery_gate",
                "scan",
                "persist",
                "transaction",
                "recovery",
                "recovery_barrier",
                "recovery_handoff",
            ],
        )
        self.assertEqual(steps[1]["with"]["ref"], "${{ github.event.repository.default_branch }}")
        recovery_gate = steps[3]
        scan = steps[4]
        self.assertEqual(
            scan["uses"],
            "laclance/OSSOpportunityScout@" + APPROVED_DISTRIBUTED_SCANNER_SHA,
        )
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
        self.assertNotIn("if", recovery_gate)
        self.assertIn(".scout/recovery-required", recovery_gate["run"])
        self.assertTrue(steps[5]["continue-on-error"])
        self.assertNotIn("continue-on-error", scan)
        self.assertNotIn(
            "if", steps[5]
        )  # Default success() still skips Git persistence on scan failure.
        self.assertEqual(steps[5]["env"]["SCOUT_BASE_SHA"], "${{ steps.base.outputs.sha }}")
        transaction = steps[6]
        self.assertEqual(transaction["if"], "${{ always() }}")
        self.assertEqual(transaction["env"]["SCAN_OUTCOME"], "${{ steps.scan.outcome }}")
        self.assertEqual(
            transaction["env"]["SCAN_RECOVERY_REQUIRED"],
            "${{ steps.scan.outputs.recovery-required }}",
        )
        self.assertEqual(transaction["env"]["PERSIST_OUTCOME"], "${{ steps.persist.outcome }}")
        self.assertIn("mode=exact-state", transaction["run"])
        self.assertIn("mode=reconstruct", transaction["run"])
        recovery = steps[7]
        self.assertEqual(
            recovery["if"],
            "${{ always() && steps.transaction.outputs.mode == 'exact-state' && github.event.repository.private == true }}",
        )
        self.assertEqual(recovery["with"]["path"], "seen_bounties.json")
        self.assertEqual(recovery["with"]["retention-days"], 3)
        self.assertEqual(recovery["with"]["if-no-files-found"], "error")
        self.assertIn("github.run_attempt", recovery["with"]["name"])
        recovery_barrier = steps[8]
        self.assertEqual(
            recovery_barrier["if"],
            "${{ always() && steps.transaction.outputs.required == 'true' }}",
        )
        self.assertTrue(recovery_barrier["continue-on-error"])
        self.assertIn("git fetch --no-tags origin", recovery_barrier["run"])
        self.assertIn("git worktree add --detach", recovery_barrier["run"])
        self.assertIn(".scout/recovery-required", recovery_barrier["run"])
        self.assertIn("push origin", recovery_barrier["run"])
        self.assertNotIn("push --force", recovery_barrier["run"])
        recovery_handoff = steps[9]
        self.assertEqual(
            recovery_handoff["if"],
            "${{ always() && steps.transaction.outputs.required == 'true' }}",
        )
        self.assertEqual(
            recovery_handoff["env"]["RECOVERY_MODE"], "${{ steps.transaction.outputs.mode }}"
        )
        self.assertEqual(
            recovery_handoff["env"]["RECOVERY_OUTCOME"], "${{ steps.recovery.outcome }}"
        )
        self.assertEqual(
            recovery_handoff["env"]["RECOVERY_BARRIER_OUTCOME"],
            "${{ steps.recovery_barrier.outcome }}",
        )
        self.assertIn("required=true", recovery_handoff["run"])
        self.assertIn("recovery_mode=", recovery_handoff["run"])
        self.assertNotIn("--force", steps[5]["run"])

        cancel_job = workflow["jobs"]["cancel_queued"]
        self.assertEqual(cancel_job["needs"], "scout")
        self.assertEqual(
            cancel_job["if"],
            "${{ always() && needs.scout.outputs.recovery_required == 'true' }}",
        )
        self.assertEqual(cancel_job["permissions"], {"actions": "write"})
        self.assertNotIn("contents", cancel_job["permissions"])
        self.assertNotIn("concurrency", cancel_job)
        cancel_steps = cancel_job["steps"]
        self.assertEqual([step.get("id") for step in cancel_steps], ["cancel_queued", None])
        cancel_queued = cancel_steps[0]
        self.assertTrue(cancel_queued["continue-on-error"])
        self.assertEqual(cancel_queued["env"]["GH_TOKEN"], "${{ github.token }}")
        self.assertEqual(cancel_queued["env"]["GH_REPO"], "${{ github.repository }}")
        self.assertEqual(cancel_queued["env"]["CURRENT_RUN_ID"], "${{ github.run_id }}")
        self.assertIn("/actions/concurrency_groups/scout-seen-state", cancel_queued["run"])
        self.assertIn("/actions/runs/$run_id/cancel", cancel_queued["run"])
        self.assertIn("select(.run_id != $CURRENT_RUN_ID)", cancel_queued["run"])
        final_failure = cancel_steps[1]
        self.assertEqual(final_failure["if"], "${{ always() }}")
        self.assertEqual(
            final_failure["env"]["RECOVERY_MODE"],
            "${{ needs.scout.outputs.recovery_mode }}",
        )
        self.assertEqual(
            final_failure["env"]["RECOVERY_OUTCOME"],
            "${{ needs.scout.outputs.recovery_outcome }}",
        )
        self.assertEqual(
            final_failure["env"]["RECOVERY_BARRIER_OUTCOME"],
            "${{ needs.scout.outputs.recovery_barrier_outcome }}",
        )
        self.assertEqual(
            final_failure["env"]["QUEUE_CANCEL_OUTCOME"], "${{ steps.cancel_queued.outcome }}"
        )

        for step in steps + cancel_steps + document(ACTION)["runs"]["steps"]:
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

    def test_transaction_recovery_classification_is_causal(self) -> None:
        step = document(TEMPLATE)["jobs"]["scout"]["steps"][6]
        cases = (
            ("success", "false", "success", {"required": "false", "mode": "none"}),
            ("success", "false", "failure", {"required": "true", "mode": "exact-state"}),
            ("failure", "true", "skipped", {"required": "true", "mode": "reconstruct"}),
            ("failure", "false", "skipped", {"required": "false", "mode": "none"}),
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            for scan_outcome, scan_recovery, persist_outcome, expected in cases:
                with self.subTest(
                    scan_outcome=scan_outcome,
                    scan_recovery=scan_recovery,
                    persist_outcome=persist_outcome,
                ):
                    output.unlink(missing_ok=True)
                    result = shell_step(
                        step,
                        ROOT,
                        {
                            "GITHUB_OUTPUT": str(output),
                            "SCAN_OUTCOME": scan_outcome,
                            "SCAN_RECOVERY_REQUIRED": scan_recovery,
                            "PERSIST_OUTCOME": persist_outcome,
                        },
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    values = dict(
                        line.split("=", 1)
                        for line in output.read_text(encoding="utf-8").splitlines()
                    )
                    self.assertEqual(values, expected)

    def test_persistence_failure_cancels_queued_group_members(self) -> None:
        step = document(TEMPLATE)["jobs"]["cancel_queued"]["steps"][0]
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            fake_bin = folder / "bin"
            fake_bin.mkdir()
            capture = folder / "gh-calls.txt"
            fake_gh = fake_bin / "gh"
            fake_gh.write_text(
                r"""#!/bin/bash
set -euo pipefail
printf '%s\n' "$*" >> "$GH_CAPTURE"
if [[ "$*" == *"actions/concurrency_groups/scout-seen-state"* ]]; then
  printf '101\n102\n'
elif [[ "$*" == *"/actions/runs/101/cancel"* ]]; then
  exit 0
elif [[ "$*" == *"/actions/runs/102/cancel"* ]]; then
  exit 0
else
  exit 23
fi
"""
            )
            fake_gh.chmod(0o755)
            result = shell_step(
                step,
                ROOT,
                {
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "GH_CAPTURE": str(capture),
                    "GH_TOKEN": "fake-actions-token",
                    "GH_REPO": "owner/private-instance",
                    "CURRENT_RUN_ID": "999",
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = capture.read_text()
            self.assertIn("/actions/concurrency_groups/scout-seen-state", calls)
            self.assertIn("/actions/runs/101/cancel", calls)
            self.assertIn("/actions/runs/102/cancel", calls)
            self.assertNotIn("/actions/runs/999/cancel", calls)

    def test_persistence_failure_propagates_queue_lookup_failure(self) -> None:
        step = document(TEMPLATE)["jobs"]["cancel_queued"]["steps"][0]
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            fake_bin = folder / "bin"
            fake_bin.mkdir()
            fake_gh = fake_bin / "gh"
            fake_gh.write_text("#!/bin/bash\nexit 23\n")
            fake_gh.chmod(0o755)
            result = shell_step(
                step,
                ROOT,
                {
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "GH_TOKEN": "fake-actions-token",
                    "GH_REPO": "owner/private-instance",
                    "CURRENT_RUN_ID": "999",
                },
            )
            self.assertEqual(result.returncode, 23)

    def test_recovery_failure_always_fails_after_recovery_attempt(self) -> None:
        step = document(TEMPLATE)["jobs"]["cancel_queued"]["steps"][-1]
        for outcome in ("success", "failure", "skipped", "cancelled", ""):
            with self.subTest(outcome=outcome):
                result = shell_step(
                    step,
                    ROOT,
                    {
                        "RECOVERY_MODE": "exact-state",
                        "RECOVERY_OUTCOME": outcome,
                        "RECOVERY_BARRIER_OUTCOME": "success",
                        "QUEUE_CANCEL_OUTCOME": "success",
                    },
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn("marker", result.stdout)
                self.assertIn("Restore" if outcome == "success" else "Reconstruct", result.stdout)

        result = shell_step(
            step,
            ROOT,
            {
                "RECOVERY_MODE": "reconstruct",
                "RECOVERY_OUTCOME": "skipped",
                "RECOVERY_BARRIER_OUTCOME": "success",
                "QUEUE_CANCEL_OUTCOME": "success",
            },
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("local state saving failed", result.stdout)
        self.assertIn("Reconstruct post-delivery state", result.stdout)
        self.assertNotIn("recovery artifact", result.stdout)

        result = shell_step(
            step,
            ROOT,
            {
                "RECOVERY_MODE": "exact-state",
                "RECOVERY_OUTCOME": "success",
                "RECOVERY_BARRIER_OUTCOME": "success",
                "QUEUE_CANCEL_OUTCOME": "failure",
            },
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("marker blocks later scans", result.stdout)
        self.assertIn("queued-run cancellation failed", result.stdout)

        result = shell_step(
            step,
            ROOT,
            {
                "RECOVERY_MODE": "reconstruct",
                "RECOVERY_OUTCOME": "skipped",
                "RECOVERY_BARRIER_OUTCOME": "failure",
                "QUEUE_CANCEL_OUTCOME": "success",
            },
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("durable recovery barrier could not be established", result.stdout)
        self.assertIn("before any rerun", result.stdout)


class PinnedActionTests(unittest.TestCase):
    def test_explicit_paths_and_separate_credentials(self) -> None:
        action = document(ACTION)
        self.assertEqual(action["runs"]["using"], "composite")
        self.assertEqual(
            action["outputs"],
            {
                "recovery-required": {
                    "description": "Whether scanner delivery succeeded but local state saving failed",
                    "value": "${{ steps.scanner.outputs.recovery-required }}",
                },
                "recovery-mode": {
                    "description": "Recovery evidence mode for the classified scanner failure",
                    "value": "${{ steps.scanner.outputs.recovery-mode }}",
                },
            },
        )
        self.assertEqual(action["runs"]["steps"][1]["id"], "scanner")
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

        run_script = action["runs"]["steps"][1]["run"]
        self.assertIn("post_delivery_save_status=86", run_script)
        for obsolete_signal in ("mktemp", "SCOUT_TRANSACTION_SIGNAL", "signal_file"):
            self.assertNotIn(obsolete_signal, run_script)

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
                "GITHUB_OUTPUT": str(caller / "action-output"),
            }
            for code in (0, 23):
                (caller / "action-output").unlink(missing_ok=True)
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
                outputs = (caller / "action-output").read_text(encoding="utf-8")
                self.assertIn("recovery-required=false", outputs)
                self.assertIn("recovery-mode=none", outputs)

    def test_post_delivery_save_failure_emits_reconstruction_signal(self) -> None:
        step = document(ACTION)["runs"]["steps"][1]
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "pinned source"
            caller = folder / "caller"
            package = source / "opportunity_scout"
            fake_bin = folder / "fake-bin"
            package.mkdir(parents=True)
            caller.mkdir()
            fake_bin.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "run.py").write_text(
                "class PostDeliveryStateSaveError(Exception):\n    pass\n",
                encoding="utf-8",
            )
            (source / "opportunity_scout.py").write_text(
                "from opportunity_scout.run import PostDeliveryStateSaveError\n"
                "try:\n"
                "    raise RuntimeError('root save cause')\n"
                "except RuntimeError as error:\n"
                "    raise PostDeliveryStateSaveError('save failed after delivery') from error\n",
                encoding="utf-8",
            )

            mktemp_capture = folder / "mktemp-called"
            fake_mktemp = fake_bin / "mktemp"
            fake_mktemp.write_text(
                '#!/bin/bash\nprintf invoked > "$MKTEMP_CAPTURE"\nexit 99\n',
                encoding="utf-8",
            )
            fake_mktemp.chmod(0o755)
            unusable_temp = folder / "unusable-temp"
            unusable_temp.write_text("not a directory\n", encoding="utf-8")

            output = caller / "action-output"
            result = shell_step(
                step,
                caller,
                {
                    "PATH": (f"{fake_bin}:{Path(sys.executable).parent}:{os.environ['PATH']}"),
                    "SCANNER_SOURCE": str(source),
                    "SCOUT_CONFIG": "scout.toml",
                    "SCOUT_STATE": "seen.json",
                    "GITHUB_TOKEN": "",
                    "PRIVATE_GITHUB_REPORTS_TOKEN": "",
                    "GITHUB_OUTPUT": str(output),
                    "MKTEMP_CAPTURE": str(mktemp_capture),
                    "TMPDIR": str(unusable_temp),
                },
            )
            self.assertEqual(result.returncode, 86)
            self.assertFalse(mktemp_capture.exists())
            self.assertIn("RuntimeError: root save cause", result.stderr)
            self.assertIn("PostDeliveryStateSaveError: save failed after delivery", result.stderr)
            outputs = output.read_text(encoding="utf-8")
            self.assertIn("recovery-required=true", outputs)
            self.assertIn("recovery-mode=reconstruct", outputs)

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
                    "GITHUB_OUTPUT": str(caller / "action-output"),
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
                "GITHUB_OUTPUT": str(caller / "action-output"),
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
        workflow = document(TEMPLATE)
        steps = workflow["jobs"]["scout"]["steps"]
        self.restore = steps[2]
        self.recovery_gate = steps[3]
        self.persist = steps[5]
        self.transaction = steps[6]
        self.recovery_barrier = steps[8]
        self.cancel_queued = workflow["jobs"]["cancel_queued"]["steps"][0]
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

    def classify_transaction(
        self,
        *,
        scan_outcome: str,
        scan_recovery_required: str,
        persist_outcome: str,
    ) -> dict[str, str]:
        self.output.unlink(missing_ok=True)
        result = shell_step(
            self.transaction,
            self.repo,
            {
                **self.environment,
                "SCAN_OUTCOME": scan_outcome,
                "SCAN_RECOVERY_REQUIRED": scan_recovery_required,
                "PERSIST_OUTCOME": persist_outcome,
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return dict(
            line.split("=", 1) for line in self.output.read_text(encoding="utf-8").splitlines()
        )

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

    def test_recovery_gate_allows_absent_marker_and_blocks_present_marker(self) -> None:
        self.read_base()
        result = shell_step(self.recovery_gate, self.repo, self.environment)
        self.assertEqual(result.returncode, 0, result.stderr)

        self.git(self.other, "fetch", "--no-tags", "origin", "main")
        self.git(self.other, "reset", "--hard", "origin/main")
        marker = self.other / ".scout" / "recovery-required"
        marker.parent.mkdir()
        marker.write_text("operator recovery required\n")
        self.git(self.other, "add", "--", ".scout/recovery-required")
        self.git(self.other, "commit", "-m", "set recovery marker")
        marker_head = self.git(self.other, "rev-parse", "HEAD")
        self.git(self.other, "push", "origin", "main")

        self.assertEqual(self.read_base(), marker_head)
        result = shell_step(self.recovery_gate, self.repo, self.environment)
        self.assertEqual(result.returncode, 1)
        self.assertIn(".scout/recovery-required", result.stdout)

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

    def test_local_save_failure_after_delivery_establishes_reconstruction_barrier(self) -> None:
        base = self.read_base()
        original_state = (self.repo / "seen_bounties.json").read_bytes()
        classification = self.classify_transaction(
            scan_outcome="failure",
            scan_recovery_required="true",
            persist_outcome="skipped",
        )
        self.assertEqual(classification, {"required": "true", "mode": "reconstruct"})

        result = shell_step(self.recovery_barrier, self.repo, self.environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        barrier_head = self.git(self.remote, "rev-parse", "main")
        self.assertEqual(self.git(self.remote, "rev-parse", "main^"), base)
        remote_state = subprocess.run(
            ["git", "show", "main:seen_bounties.json"],
            cwd=self.remote,
            capture_output=True,
            check=True,
        ).stdout
        self.assertEqual(remote_state, original_state)

        self.assertEqual(self.read_base(), barrier_head)
        result = shell_step(self.recovery_gate, self.repo, self.environment)
        self.assertEqual(result.returncode, 1)
        self.assertIn(".scout/recovery-required", result.stdout)

    def test_persistence_failure_barrier_uses_current_remote_head_without_state_overwrite(
        self,
    ) -> None:
        self.read_base()
        delivered_snapshot = b'{"delivered": "local recovery snapshot"}\n'
        (self.repo / "seen_bounties.json").write_bytes(delivered_snapshot)

        newer_remote_state = b'{"newer": "remote state must survive"}\n'
        (self.other / "seen_bounties.json").write_bytes(newer_remote_state)
        newer_head = self.advance_other_writer()

        result = shell_step(self.persist, self.repo, self.environment)
        self.assertEqual(result.returncode, 1)
        self.assertIn("changed during scan", result.stdout)
        self.assertEqual((self.repo / "seen_bounties.json").read_bytes(), delivered_snapshot)

        result = shell_step(self.recovery_barrier, self.repo, self.environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        barrier_head = self.git(self.remote, "rev-parse", "main")
        self.assertEqual(self.git(self.remote, "rev-parse", "main^"), newer_head)
        self.assertNotEqual(barrier_head, newer_head)
        remote_state = subprocess.run(
            ["git", "show", "main:seen_bounties.json"],
            cwd=self.remote,
            capture_output=True,
            check=True,
        ).stdout
        self.assertEqual(remote_state, newer_remote_state)
        self.assertEqual(
            self.git(self.remote, "diff-tree", "--no-commit-id", "--name-only", "-r", "main"),
            ".scout/recovery-required",
        )
        self.assertIn(
            "Recovery required",
            self.git(self.remote, "show", "main:.scout/recovery-required"),
        )

    def test_recovery_marker_blocks_later_run_even_when_queue_cancellation_fails(self) -> None:
        self.read_base()
        result = shell_step(self.recovery_barrier, self.repo, self.environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        barrier_head = self.git(self.remote, "rev-parse", "main")

        fake_bin = self.folder / "fake-bin"
        fake_bin.mkdir()
        fake_gh = fake_bin / "gh"
        fake_gh.write_text("#!/bin/bash\nexit 23\n")
        fake_gh.chmod(0o755)
        result = shell_step(
            self.cancel_queued,
            self.repo,
            {
                **self.environment,
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "GH_TOKEN": "fake-actions-token",
                "GH_REPO": "owner/private-instance",
                "CURRENT_RUN_ID": "999",
            },
        )
        self.assertEqual(result.returncode, 23)

        self.assertEqual(self.read_base(), barrier_head)
        result = shell_step(self.recovery_gate, self.repo, self.environment)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), barrier_head)

    def test_recovery_barrier_creation_failure_is_observable(self) -> None:
        self.read_base()
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        result = shell_step(self.recovery_barrier, self.repo, self.environment)
        self.assertNotEqual(result.returncode, 0)
        show = subprocess.run(
            ["git", "show", "main:.scout/recovery-required"],
            cwd=self.remote,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(show.returncode, 0)

    def test_recovery_marker_is_removed_only_by_explicit_operator_commit(self) -> None:
        self.read_base()
        result = shell_step(self.recovery_barrier, self.repo, self.environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        barrier_head = self.git(self.remote, "rev-parse", "main")

        self.assertEqual(self.read_base(), barrier_head)
        result = shell_step(self.recovery_gate, self.repo, self.environment)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), barrier_head)

        self.git(self.other, "fetch", "--no-tags", "origin", "main")
        self.git(self.other, "reset", "--hard", "origin/main")
        marker = self.other / ".scout" / "recovery-required"
        marker.unlink()
        self.git(self.other, "add", "-u", "--", ".scout/recovery-required")
        self.git(self.other, "commit", "-m", "complete scout recovery")
        recovered_head = self.git(self.other, "rev-parse", "HEAD")
        self.git(self.other, "push", "origin", "main")

        self.assertEqual(self.read_base(), recovered_head)
        result = shell_step(self.recovery_gate, self.repo, self.environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        show = subprocess.run(
            ["git", "show", "main:.scout/recovery-required"],
            cwd=self.remote,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(show.returncode, 0)

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
