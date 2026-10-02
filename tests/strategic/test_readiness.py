from __future__ import annotations

import unittest
from bountyscout.strategic import readiness
from bountyscout.types import GitHubComment
from tests.helpers import comment, issue


class ReadinessLabelTests(unittest.TestCase):
    def test_pending_label_dialects_and_ready_override(self) -> None:
        for labels_text in (
            "needs-triage",
            "status/triage_pending",
            "kind/bug/possible",
            "needs_analysis",
            "needs/investigation",
        ):
            with self.subTest(labels_text=labels_text):
                self.assertTrue(readiness.triage_pending_signal(labels_text))
        self.assertFalse(readiness.triage_pending_signal("enhancement"))

        self.assertEqual(
            readiness.readiness_pending_label_reason(
                issue(labels=[{"name": "status/needs-reproduction"}])
            ),
            "awaiting reproduction confirmation",
        )
        self.assertEqual(
            readiness.readiness_pending_label_reason(issue(labels=["needs/design"])),
            "awaiting maintainer design decision",
        )
        self.assertIsNone(
            readiness.readiness_pending_label_reason(
                issue(labels=["needs-investigation"]),
                ready_override=True,
            )
        )

        rotten = issue(labels=["lifecycle/rotten"])
        self.assertEqual(
            readiness.abandoned_lifecycle_reason(rotten),
            "issue is in an abandoned/rotten lifecycle state",
        )
        self.assertIsNone(readiness.abandoned_lifecycle_reason(rotten, ready_override=True))

    def test_label_set_handles_dict_string_and_empty_labels(self) -> None:
        self.assertEqual(
            readiness.issue_label_set(
                issue(labels=[{"name": " Help Wanted "}, "BUG", {"name": ""}, ""])
            ),
            {"help wanted", "bug"},
        )

    def test_proposal_stage_recognizes_title_and_label_dialects(self) -> None:
        self.assertTrue(readiness.proposal_stage_signal(issue(title="RFC: retry policy")))
        self.assertTrue(readiness.proposal_stage_signal(issue(labels=[{"name": "kind/proposal"}])))
        self.assertTrue(readiness.proposal_stage_signal(issue(labels=["needs/discussion"])))
        self.assertFalse(readiness.proposal_stage_signal(issue(labels=["enhancement"])))


class MaintainerReadinessTests(unittest.TestCase):
    def test_authority_requires_maintainer_or_explicit_project_action(self) -> None:
        self.assertTrue(
            readiness.maintainer_comment_authority(
                comment(body="Thanks.", author_association="MEMBER")
            )
        )
        self.assertTrue(
            readiness.maintainer_comment_authority(
                comment(
                    body="I'm going to hand it off to the Engineering team.",
                    author_association="CONTRIBUTOR",
                )
            )
        )
        self.assertFalse(
            readiness.maintainer_comment_authority(
                {"body": "Thanks.", "author_association": "CONTRIBUTOR"}
            )
        )
        self.assertFalse(
            readiness.maintainer_comment_authority(
                {
                    "body": "We'll reevaluate this.",
                    "author_association": "NONE",
                }
            )
        )

    def test_node_log_request_is_a_diagnostic_hold(self) -> None:
        comments: list[GitHubComment] = [
            {
                "body": (
                    "Could you share the node logs if you have collected them from the "
                    "affected instance?"
                ),
                "author_association": "COLLABORATOR",
            }
        ]
        self.assertEqual(
            readiness.maintainer_readiness_comment_state(issue(), comments),
            (False, "maintainer is waiting for requested diagnostic evidence"),
        )

    def test_requested_diagnostic_is_cleared_when_evidence_arrives(self) -> None:
        comments: list[GitHubComment] = [
            {
                "body": "Could you provide a heap profile from the affected process?",
                "author_association": "MEMBER",
            },
            {
                "body": "Here’s the requested heap profile attached with the logs.",
                "author_association": "NONE",
            },
        ]
        self.assertEqual(
            readiness.maintainer_readiness_comment_state(issue(), comments),
            (None, None),
        )

    def test_latest_explicit_stance_wins(self) -> None:
        hold = comment(
            body="Please wait before implementing; this needs clarification.",
            author_association="MEMBER",
        )
        ready = comment(
            body="Clarification is complete. This is ready for implementation.",
            author_association="MEMBER",
        )

        self.assertEqual(
            readiness.maintainer_readiness_comment_state(issue(), [hold, ready]),
            (True, None),
        )
        self.assertEqual(
            readiness.maintainer_readiness_comment_state(issue(), [ready, hold]),
            (False, "maintainer asked contributors to wait before implementation"),
        )

    def test_redirect_detection_requires_actual_redirection(self) -> None:
        context_only: list[GitHubComment] = [
            {
                "body": "Related code lives in distribution/reference for context.",
                "author_association": "MEMBER",
            }
        ]
        redirect: list[GitHubComment] = [
            {
                "body": (
                    "This requires a change in https://github.com/distribution/reference. "
                    "Please open an issue there."
                ),
                "author_association": "COLLABORATOR",
            }
        ]

        self.assertEqual(
            readiness.maintainer_readiness_comment_state(issue(), context_only),
            (None, None),
        )
        self.assertEqual(
            readiness.maintainer_readiness_comment_state(issue(), redirect),
            (
                False,
                "maintainer redirected implementation/discussion to another project",
            ),
        )
        core_redirect: list[GitHubComment] = [
            {
                "body": (
                    "Closing it here would help it being backported to previous node. "
                    "But I think we should do it in core."
                ),
                "author_association": "MEMBER",
            }
        ]
        self.assertEqual(
            readiness.maintainer_readiness_comment_state(issue(), core_redirect),
            (
                False,
                "maintainer redirected implementation/discussion to another project",
            ),
        )

    def test_duplicate_detection_avoids_related_and_not_duplicate_text(self) -> None:
        duplicate: list[GitHubComment] = [
            {
                "body": "Looks like a possible duplicate of #123; discussion is tracked there.",
                "author_association": "OWNER",
            }
        ]
        distinct: list[GitHubComment] = [
            {
                "body": "This is not a duplicate of #123; the symptoms are different.",
                "author_association": "OWNER",
            }
        ]
        untrusted: list[GitHubComment] = [
            {
                "body": "Looks like a duplicate of #123.",
                "author_association": "NONE",
            }
        ]

        self.assertEqual(
            readiness.maintainer_readiness_comment_state(issue(), duplicate),
            (
                False,
                "maintainer indicates this is probably tracked by another canonical issue",
            ),
        )
        self.assertEqual(
            readiness.maintainer_readiness_comment_state(issue(), distinct),
            (None, None),
        )
        self.assertEqual(
            readiness.maintainer_readiness_comment_state(issue(), untrusted),
            (None, None),
        )

    def test_wrong_solution_from_maintainer_blocks_requested_approach(self) -> None:
        wrong_solution: list[GitHubComment] = [
            {
                "body": (
                    "A merge option would be the wrong solution for this. "
                    "The profile should be defined declaratively instead."
                ),
                "author_association": "MEMBER",
            }
        ]
        self.assertEqual(
            readiness.maintainer_readiness_comment_state(issue(), wrong_solution),
            (
                False,
                "maintainer indicates the proposed implementation approach is not wanted",
            ),
        )
        provider_direction: list[GitHubComment] = [
            {
                "body": (
                    "The prevailing wisdom on the maintainer team is that there is no "
                    "one correct deep merge. This is a use case for provider functions."
                ),
                "author_association": "CONTRIBUTOR",
            }
        ]
        self.assertEqual(
            readiness.maintainer_readiness_comment_state(issue(), provider_direction),
            (
                False,
                "maintainer indicates the proposed implementation approach is not wanted",
            ),
        )

        untrusted: list[GitHubComment] = [
            {
                "body": "This is the wrong solution; use something else.",
                "author_association": "NONE",
            }
        ]
        self.assertEqual(
            readiness.maintainer_readiness_comment_state(issue(), untrusted),
            (None, None),
        )

        later_ready = wrong_solution + [
            {
                "body": "We reconsidered this; a PR in this repository is welcome.",
                "author_association": "MEMBER",
            }
        ]
        self.assertEqual(
            readiness.maintainer_readiness_comment_state(issue(), later_ready),
            (True, None),
        )

    def test_feedback_language_only_blocks_proposal_stage(self) -> None:
        comment: list[GitHubComment] = [
            {
                "body": "We want to gather feedback before committing.",
                "author_association": "OWNER",
            }
        ]
        self.assertEqual(
            readiness.maintainer_readiness_comment_state(issue(), comment),
            (None, None),
        )
        self.assertEqual(
            readiness.maintainer_readiness_comment_state(
                issue(labels=[{"name": "kind/proposal"}]),
                comment,
            ),
            (False, "proposal is still gathering feedback"),
        )

    def test_hold_reason_branches_are_directly_owned_here(self) -> None:
        regular = issue(title="Network bug")
        proposal = issue(title="Network metrics", labels=[{"name": "kind/proposal"}])

        cases = (
            (
                "We need more investigation before coding.",
                "maintainer says issue still needs investigation",
            ),
            (
                "Are you sure you reproduced this on the current CLI?",
                "maintainer says reproduction is still required",
            ),
            (
                "We need clarification on the API contract.",
                "maintainer says issue still needs clarification",
            ),
        )
        for body, expected in cases:
            with self.subTest(body=body):
                state, reason = readiness.maintainer_readiness_comment_state(
                    regular,
                    [{"body": body, "author_association": "MEMBER"}],
                )
                self.assertFalse(state)
                self.assertEqual(reason, expected)

        state, reason = readiness.maintainer_readiness_comment_state(
            proposal,
            [
                {
                    "body": "This discussion is time-boxed to six months.",
                    "author_association": "OWNER",
                }
            ],
        )
        self.assertFalse(state)
        self.assertEqual(reason, "proposal is still gathering feedback")


class SubmissionAndReporterResolutionTests(unittest.TestCase):
    def test_trusted_issue_author_can_explicitly_forbid_prs(self) -> None:
        blocked = issue(
            author_association="MEMBER",
            body=(
                "If you are an agent reading this, do not open a PR for this issue; "
                "it will be closed due to this issue representing a breaking change."
            ),
        )
        self.assertEqual(
            readiness.maintainer_submission_hold_reason(blocked),
            "maintainer explicitly says not to open a PR for this issue",
        )
        self.assertIsNone(
            readiness.maintainer_submission_hold_reason(
                issue(
                    author_association="NONE",
                    body="Do not open a PR for this issue.",
                )
            )
        )
        self.assertIsNone(
            readiness.maintainer_submission_hold_reason(
                issue(
                    author_association="MEMBER",
                    body="Please add validation in the arguments package.",
                )
            )
        )

    def test_reporter_latest_resolution_state_wins(self) -> None:
        resolved: list[GitHubComment] = [
            {
                "body": "It looks like the issue got fixed; I don't see it in the latest version.",
                "user": {"login": "reporter"},
            }
        ]
        self.assertEqual(
            readiness.reporter_resolution_reason(issue(), resolved),
            "issue reporter says the problem is already resolved",
        )

        reopened = resolved + [
            {
                "body": "Correction: it still reproduces on the current build.",
                "user": {"login": "reporter"},
            }
        ]
        self.assertIsNone(readiness.reporter_resolution_reason(issue(), reopened))
        self.assertIsNone(
            readiness.reporter_resolution_reason(
                issue(),
                [{"body": "Looks fixed to me.", "user": {"login": "someone-else"}}],
            )
        )
        self.assertIsNone(
            readiness.reporter_resolution_reason(
                issue(),
                [
                    {
                        "body": "Thanks for checking; I will test another configuration.",
                        "user": {"login": "reporter"},
                    }
                ],
            )
        )

    def test_trusted_spec_level_discussion_is_a_hold(self) -> None:
        comments: list[GitHubComment] = [
            {
                "body": (
                    "OpenMetrics 2.0 will not allow absent sum. I'd suggest rejecting such "
                    "histograms. This is being discussed on spec level here."
                ),
                "author_association": "MEMBER",
            }
        ]
        self.assertEqual(
            readiness.maintainer_readiness_comment_state(issue(), comments),
            (False, "maintainer says issue still needs discussion"),
        )

    def test_trusted_worth_discussion_and_community_feedback_are_holds(self) -> None:
        for body in (
            "Thanks for filing this. It is worth discussion.",
            "Like to hear from the community before we choose an approach.",
        ):
            with self.subTest(body=body):
                self.assertEqual(
                    readiness.maintainer_readiness_comment_state(
                        issue(),
                        [{"body": body, "author_association": "COLLABORATOR"}],
                    ),
                    (False, "maintainer says issue still needs discussion"),
                )


class MaintainerIssueDecisionTests(unittest.TestCase):
    def test_trusted_issue_author_can_mark_semantics_as_still_undecided(self) -> None:
        deciding = issue(
            author_association="MEMBER",
            body=(
                "This issue is to decide what these metrics should mean "
                "before the new protocol is called stable."
            ),
        )
        self.assertEqual(
            readiness.maintainer_issue_decision_reason(deciding),
            "maintainer-authored issue is still deciding implementation semantics",
        )

        self.assertIsNone(
            readiness.maintainer_issue_decision_reason(
                issue(
                    author_association="NONE",
                    body="This issue is to decide what the API should mean.",
                )
            )
        )
        self.assertIsNone(
            readiness.maintainer_issue_decision_reason(
                issue(
                    author_association="MEMBER",
                    body="The implementation follows the decision from #123.",
                )
            )
        )


class SecurityDisclosureTests(unittest.TestCase):
    def test_explicit_and_structured_security_disclosures_are_not_normal_tasks(self) -> None:
        self.assertEqual(
            readiness.security_disclosure_reason(
                issue(
                    title="[Security Disclosure] DNS record injection",
                    body="Public vulnerability report.",
                )
            ),
            "security disclosure, not a normal contributor task",
        )
        self.assertEqual(
            readiness.security_disclosure_reason(
                issue(
                    title="DNS authorization issue",
                    body=(
                        "Severity high. CVSS 3.1: 8.1. CWE-285. "
                        "Disclosure timeline: discovered today."
                    ),
                )
            ),
            "security disclosure, not a normal contributor task",
        )
        self.assertIsNone(
            readiness.security_disclosure_reason(
                issue(
                    title="Security hardening: validate hostname",
                    body="Add validation and tests for a normal public hardening task.",
                )
            )
        )


class RewardHistoryTests(unittest.TestCase):
    def test_hall_of_fame_payout_summary_is_not_open_paid_work(self) -> None:
        hall = issue(
            title="🏆 Hall of Fame — October 2026",
            labels=[{"name": "hall-of-fame"}],
            body=(
                "## 🥇 Top Contributors\n"
                "## ⚡ Fastest Fixes\n"
                "## 📊 Monthly Stats\n"
                "Total Bounty Distributed | **$4770**"
            ),
        )
        self.assertEqual(
            readiness.reward_history_reason(hall),
            "bounty history/leaderboard, not an open paid task",
        )
        self.assertIsNone(
            readiness.reward_history_reason(
                issue(
                    title="Implement leaderboard pagination",
                    labels=[{"name": "bounty"}],
                    body="Open task with a $50 reward.",
                )
            )
        )


class ReporterSupportTriageTests(unittest.TestCase):
    def test_guidance_questionnaire_is_support_triage_not_implementation(self) -> None:
        support = issue(
            body=(
                "We would like to determine whether this is expected behavior, an environment "
                "interaction, a product bug, or configuration. We would particularly appreciate "
                "guidance on:\n"
                "1. Is this expected in this environment?\n"
                "2. Are there known network idle-timeout issues?\n"
                "3. Could the platform networking cause this?\n"
                "4. Would switching transports be a recommended workaround?\n"
            )
        )
        self.assertEqual(
            readiness.reporter_support_triage_reason(support),
            "support/triage issue rather than a contributor task",
        )
        self.assertIsNone(
            readiness.reporter_support_triage_reason(
                issue(
                    body=(
                        "Implement retries for reusable request bodies. "
                        "Should FormData stay non-replayable?"
                    )
                )
            )
        )


class ManualTrackingIssueTests(unittest.TestCase):
    def test_multi_child_umbrella_tracker_is_not_single_implementation_task(self) -> None:
        tracker = issue(
            body=(
                "Track and resolve the following API refinements and bug fixes:\n\n"
                "- [ ] #8314\n"
                "- [ ] #9456\n"
                "- [ ] #9457\n"
                "- [ ] #9458\n"
            )
        )
        self.assertEqual(
            readiness.manual_tracking_issue_reason(tracker),
            "umbrella tracking issue, not a single implementation task",
        )

    def test_trusted_issue_author_can_declare_an_umbrella_tracker(self) -> None:
        tracker = issue(
            author_association="MEMBER",
            body=(
                "This is an umbrella issue to keep an overview over current feature "
                "requests and limitations around metrics."
            ),
        )
        self.assertEqual(
            readiness.manual_tracking_issue_reason(tracker),
            "umbrella tracking issue, not a single implementation task",
        )
        self.assertIsNone(
            readiness.manual_tracking_issue_reason(
                issue(
                    author_association="NONE", body="This is an umbrella issue for related ideas."
                )
            )
        )

    def test_tracking_container_that_delegates_work_to_subissues_is_umbrella(self) -> None:
        tracker = issue(
            title="[2026] Tracking issue for flaky tests",
            author_association="MEMBER",
            body=(
                "This is the 2026 equivalent of the previous flaky-test tracker. "
                "We can use this issue for tracking, and create sub-issues per test failure?"
            ),
        )
        self.assertEqual(
            readiness.manual_tracking_issue_reason(tracker),
            "umbrella tracking issue, not a single implementation task",
        )
        self.assertIsNone(
            readiness.manual_tracking_issue_reason(
                issue(body="Use this issue for tracking the regression while implementing it here.")
            )
        )
        self.assertIsNone(
            readiness.manual_tracking_issue_reason(
                issue(body="Create a sub-issue for optional cleanup after this fix lands.")
            )
        )

    def test_normal_checklists_and_small_cross_references_stay_actionable(self) -> None:
        self.assertIsNone(
            readiness.manual_tracking_issue_reason(
                issue(
                    body=(
                        "Implementation checklist:\n"
                        "- [ ] update parser\n"
                        "- [ ] add tests\n"
                        "- [ ] update docs\n"
                    )
                )
            )
        )
        self.assertIsNone(
            readiness.manual_tracking_issue_reason(
                issue(body=("Track the following follow-up work:\n- [ ] #10\n- [ ] #11\n"))
            )
        )
        self.assertEqual(
            readiness.manual_tracking_issue_reason(
                issue(body="### Tasks\n- [x] Initial implementation\n- [ ] Further improvements"),
                [
                    {
                        "body": "Let me know if I should add additional tasks to this umbrella issue.",
                        "author_association": "MEMBER",
                    }
                ],
            ),
            "umbrella tracking issue, not a single implementation task",
        )
        self.assertIsNone(
            readiness.manual_tracking_issue_reason(
                issue(body="### Tasks\n- [ ] one change"),
                [
                    {
                        "body": "I think this could be an umbrella issue.",
                        "author_association": "NONE",
                    }
                ],
            )
        )


class LifecycleClassificationTests(unittest.TestCase):
    def test_dependency_dashboard_requires_automated_tracking_context(self) -> None:
        dashboard = issue(
            title="Dependency Dashboard",
            labels=[{"name": "dependencies"}],
            user={"login": "renovate-sh-app[bot]"},
        )
        self.assertEqual(
            readiness.automated_tracking_issue_reason(dashboard),
            "automated dependency dashboard, not an implementation task",
        )
        self.assertIsNone(
            readiness.automated_tracking_issue_reason(
                issue(title="Dependency Dashboard", user={"login": "human"})
            )
        )
        self.assertIsNone(
            readiness.automated_tracking_issue_reason(
                issue(title="Nightly status", user={"login": "example[bot]"})
            )
        )
        nightly = issue(
            title="cmux NIGHTLY build is failing on main",
            labels=[{"name": "nightly-failure"}],
            user={"login": "github-actions[bot]"},
            body=(
                "This issue closes itself on the next successful publish. "
                "The next successful publication will update this incident."
            ),
        )
        self.assertEqual(
            readiness.automated_tracking_issue_reason(nightly),
            "automated CI/release incident, not an implementation task",
        )

    def test_bot_managed_non_actionable_monitoring_tracker_is_rejected(self) -> None:
        tracker = issue(
            title="[aw] Detection Runs",
            user={"login": "github-actions[bot]"},
            body=(
                "This issue tracks all runs where threat detection flagged problems in "
                "agentic workflows. It helps monitor the health of the threat detection system.\n\n"
                "This issue is automatically managed by GitHub Agentic Workflows. "
                "Do not close this issue manually.\n\n"
                "No action to take - Do not assign to an agent."
            ),
        )
        self.assertEqual(
            readiness.automated_tracking_issue_reason(tracker),
            "automated monitoring tracker, not an implementation task",
        )

        do_not_assign = issue(
            title="Detection run tracker",
            user={"login": "github-actions[bot]"},
            body=(
                "This issue tracks detection runs and is automatically managed. "
                "Do not assign to an agent."
            ),
        )
        self.assertEqual(
            readiness.automated_tracking_issue_reason(do_not_assign),
            "automated monitoring tracker, not an implementation task",
        )

    def test_automated_tracker_rule_keeps_actionable_and_human_issues(self) -> None:
        self.assertIsNone(
            readiness.automated_tracking_issue_reason(
                issue(
                    title="Tracking request regression",
                    user={"login": "human"},
                    body="Tracking requests can leak workers. Please fix the cleanup path.",
                )
            )
        )
        self.assertIsNone(
            readiness.automated_tracking_issue_reason(
                issue(
                    title="CI found reproducible parser failure",
                    user={"login": "github-actions[bot]"},
                    body=(
                        "Automation reproduced this parser crash on main. "
                        "Please investigate and add a regression fix."
                    ),
                )
            )
        )

    def test_release_tracking_and_release_only_work_are_distinct(self) -> None:
        self.assertEqual(
            readiness.release_tracking_reason(issue(title="Release 2.0 tracking checklist")),
            "release planning/tracking issue, not implementation work",
        )
        self.assertEqual(
            readiness.release_tracking_reason(issue(title="Plan to release v3.5.34")),
            "release planning/tracking issue, not implementation work",
        )
        self.assertEqual(
            readiness.release_tracking_reason(
                issue(
                    title="Planned SDK 3.0 Release (Important Dates and Information)",
                    labels=[{"name": "announcement 📢"}],
                )
            ),
            "release planning/tracking issue, not implementation work",
        )

        implemented = issue(
            body=(
                "PR #123 merged last week. The release tag has not yet been published. "
                "Request: please tag a new release."
            )
        )
        self.assertEqual(
            readiness.release_tracking_reason(implemented),
            "implementation already merged; only release/tagging remains",
        )

        self.assertIsNone(
            readiness.release_tracking_reason(issue(body="PR #123 merged last week."))
        )
        self.assertIsNone(
            readiness.release_tracking_reason(issue(body="Please publish a new release."))
        )
        self.assertIsNone(readiness.release_tracking_reason(issue(title="Bug in release parser")))

        republish = issue(
            title=(
                "undici-types 6.21.x: a trusted-publisher re-release would unblock current users"
            ),
            body=(
                "Suggestion: publish undici-types@6.21.1 with the same content as 6.21.0 "
                "through the current trusted-publisher workflow."
            ),
        )
        self.assertEqual(
            readiness.release_tracking_reason(republish),
            "existing package content; only release/publication remains",
        )

        missing_release = issue(
            title="Version 3.13.4 missing release",
            body=(
                "The version update was merged in #19862, however the release action "
                "failed on Go testing. The release step then never triggered. "
                "This leaves us with a v3.13.4 tag, but no release with downloads."
            ),
        )
        self.assertEqual(
            readiness.release_tracking_reason(missing_release),
            "implementation already merged; only release/tagging remains",
        )

        undici = issue(
            comments=2,
        )
        undici_comments: list[GitHubComment] = [
            {
                "body": (
                    "Looks like this was fixed on main by #5864. "
                    "It is not released yet; should be good with the next release."
                ),
                "author_association": "NONE",
            }
        ]
        self.assertEqual(
            readiness.release_tracking_reason(undici, undici_comments),
            "implementation already merged; only release/tagging remains",
        )

        self.assertIsNone(
            readiness.release_tracking_reason(
                issue(),
                [{"body": "This bug reproduces on main and may affect the next release."}],
            )
        )
        self.assertIsNone(
            readiness.release_tracking_reason(
                issue(),
                [{"body": "Fixed on main, but please also backport this to the release branch."}],
            )
        )


if __name__ == "__main__":
    unittest.main()
