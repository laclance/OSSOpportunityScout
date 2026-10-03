from __future__ import annotations

import unittest
from pathlib import Path

WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "oss-opportunity-scout.yml"
)


class ProductionWorkflowConfigurationTests(unittest.TestCase):
    def test_manual_only_least_privilege_and_private_report_wiring(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("on:\n  workflow_dispatch:\n", workflow)
        self.assertNotIn("\n  schedule:", workflow)
        self.assertIn("    permissions:\n      contents: write\n", workflow)
        self.assertNotIn("      issues: write\n", workflow)
        self.assertIn(
            "PRIVATE_GITHUB_REPORTS_REPOSITORY: "
            "${{ secrets.PRIVATE_GITHUB_REPORTS_REPOSITORY }}",
            workflow,
        )
        self.assertIn(
            "PRIVATE_GITHUB_REPORTS_TOKEN: "
            "${{ secrets.PRIVATE_GITHUB_REPORTS_TOKEN }}",
            workflow,
        )
        self.assertNotIn("GITHUB_REPORTS_ENABLED:", workflow)


if __name__ == "__main__":
    unittest.main()
