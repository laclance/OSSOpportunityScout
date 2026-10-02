from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from bountyscout import github, paid_verification
from bountyscout.strategic import competition
from bountyscout.types import GitHubComment
from tests.helpers import comment, issue


class ClaimCompetitionTests(unittest.TestCase):
    def test_claim_recency_uses_issue_creation_and_comment_updates(self) -> None:
        recent = datetime.now(timezone.utc)
        stale = recent - timedelta(days=competition.STRATEGIC_CLAIM_MAX_AGE_DAYS + 1)

        self.assertTrue(
            competition.claim_source_is_recent(
                comment(created_at=recent.isoformat()),
                issue_body=True,
            )
        )
        self.assertFalse(
            competition.claim_source_is_recent(
                comment(created_at=stale.isoformat()),
                issue_body=True,
            )
        )
        self.assertTrue(
            competition.claim_source_is_recent(
                comment(
                    created_at=stale.isoformat(),
                    updated_at=recent.isoformat(),
                )
            )
        )
        self.assertTrue(
            competition.claim_source_is_recent(comment(created_at=None, updated_at=None))
        )

    def test_strategic_claim_reason_ignores_stale_and_third_person_work(self) -> None:
        recent = datetime.now(timezone.utc).isoformat()
        stale = (
            datetime.now(timezone.utc)
            - timedelta(days=competition.STRATEGIC_CLAIM_MAX_AGE_DAYS + 1)
        ).isoformat()

        self.assertEqual(
            competition.strategic_claim_reason(
                issue(body="I've implemented this locally.", created_at=recent),
                [],
            ),
            "issue author already has an implementation/fix in progress",
        )
        self.assertIsNone(
            competition.strategic_claim_reason(
                issue(body="I've implemented this locally.", created_at=stale),
                [],
            )
        )
        self.assertEqual(
            competition.strategic_claim_reason(
                issue(body=""),
                [
                    {
                        "body": "I'm working on a fix.",
                        "updated_at": recent,
                        "user": {"login": "dev"},
                    }
                ],
            ),
            "active claim by @dev",
        )
        self.assertIsNone(
            competition.strategic_claim_reason(
                issue(body=""),
                [
                    {
                        "body": "Someone else is working on a fix.",
                        "updated_at": recent,
                        "user": {"login": "observer"},
                    },
                    {
                        "body": "I'm working on a fix.",
                        "updated_at": stale,
                        "user": {"login": "old-dev"},
                    },
                ],
            )
        )

    def test_live_claim_wording_pick_up_and_willing_to_contribute_pr(self) -> None:
        recent = datetime.now(timezone.utc).isoformat()

        self.assertEqual(
            competition.strategic_claim_reason(
                issue(body=""),
                [
                    {
                        "body": (
                            "I'd like to pick this up. On current main, the stats are "
                            "not retained. I'll wait for direction before publishing an implementation."
                        ),
                        "updated_at": recent,
                        "user": {"login": "fzlzjerry"},
                    }
                ],
            ),
            "active claim by @fzlzjerry",
        )

        self.assertEqual(
            competition.strategic_claim_reason(
                issue(
                    body=(
                        "Contribution Intention (Optional)\n\n"
                        "- [x] Yes, I am willing to contribute a PR to implement this feature\n"
                        "- [ ] No, I cannot work on a PR at this time"
                    ),
                    created_at=recent,
                ),
                [],
            ),
            "issue author already has an implementation/fix in progress",
        )

        self.assertIsNone(
            competition.strategic_claim_reason(
                issue(body=""),
                [
                    {
                        "body": "I'd like to see someone pick this up.",
                        "updated_at": recent,
                        "user": {"login": "observer"},
                    }
                ],
            )
        )

    def test_chain_love_3969_direct_claim_is_active_ownership(self) -> None:
        recent = datetime.now(timezone.utc).isoformat()
        comments: list[GitHubComment] = [
            {
                "body": (
                    "Claiming this DBIP. Plan: validator rule in json-tools parsing each "
                    "actionButtons cell (JSON array of Markdown links), normalizing destinations "
                    "(trailing slash + scheme/host casing), rejecting duplicate destinations per "
                    "cell with the suggested file/slug/URL/labels error; plus the bounded data fix "
                    "removing redundant duplicate buttons (keep first occurrence, no URL guessing) "
                    "so the rule lands green. Paired data/tools PRs incoming."
                ),
                "updated_at": recent,
                "user": {"login": "elevasyncsolutions-jpg"},
            }
        ]

        self.assertEqual(
            competition.strategic_claim_reason(
                issue(html_url="https://github.com/Chain-Love/chain-love/issues/3969", body=""),
                comments,
            ),
            "active claim by @elevasyncsolutions-jpg",
        )
        self.assertEqual(
            competition.strategic_competition_reason(
                issue(html_url="https://github.com/Chain-Love/chain-love/issues/3969", body=""),
                "t",
                comments,
                linked_pr_checker=lambda *_: self.fail("linked PR check should not be reached"),
                timeline_pr_checker=lambda *_: self.fail("timeline PR check should not be reached"),
            ),
            "active claim by @elevasyncsolutions-jpg",
        )

    def test_taking_this_one_is_active_ownership(self) -> None:
        recent = datetime.now(timezone.utc).isoformat()
        self.assertEqual(
            competition.strategic_claim_reason(
                issue(body=""),
                [
                    {
                        "body": (
                            "Taking this one — I'll trace why the file input reads nothing "
                            "and fix the parsing."
                        ),
                        "updated_at": recent,
                        "user": {"login": "vaputa"},
                    }
                ],
            ),
            "active claim by @vaputa",
        )
        self.assertIsNone(
            competition.strategic_claim_reason(
                issue(body=""),
                [
                    {
                        "body": "Someone else is taking this one.",
                        "updated_at": recent,
                        "user": {"login": "observer"},
                    }
                ],
            )
        )

    def test_working_branch_with_test_is_active_implementation_claim(self) -> None:
        recent = datetime.now(timezone.utc).isoformat()
        self.assertEqual(
            competition.strategic_claim_reason(
                issue(body=""),
                [
                    {
                        "body": (
                            "The quick fix mirrors the token suppression, and I have that "
                            "working with a test on a branch. I am not opening a PR yet because "
                            "I want maintainer direction on the more accurate fix."
                        ),
                        "updated_at": recent,
                        "user": {"login": "david"},
                    }
                ],
            ),
            "active claim by @david",
        )

    def test_local_exploration_with_concrete_changes_is_active_implementation(self) -> None:
        recent = datetime.now(timezone.utc).isoformat()
        self.assertEqual(
            competition.strategic_claim_reason(
                issue(body=""),
                [
                    {
                        "body": (
                            "I poked at this locally. No breaking change needed. "
                            "I made the explicit selector win and moved the preference logic "
                            "into sdk-metrics."
                        ),
                        "updated_at": recent,
                        "user": {"login": "neoLsH"},
                    }
                ],
            ),
            "active claim by @neoLsH",
        )
        self.assertIsNone(
            competition.strategic_claim_reason(
                issue(body=""),
                [
                    {
                        "body": "I poked at this locally but did not change anything.",
                        "updated_at": recent,
                        "user": {"login": "observer"},
                    }
                ],
            )
        )

    def test_issue_numbered_branch_link_by_author_is_active_implementation(self) -> None:
        recent = datetime.now(timezone.utc).isoformat()
        self.assertEqual(
            competition.strategic_claim_reason(
                issue(html_url="https://github.com/nodejs/undici/issues/5912", body=""),
                [
                    {
                        "body": "https://github.com/KhafraDev/undici/tree/fetch/issue-5912",
                        "updated_at": recent,
                        "user": {"login": "KhafraDev"},
                        "author_association": "MEMBER",
                    }
                ],
            ),
            "active implementation branch linked by @KhafraDev",
        )
        self.assertIsNone(
            competition.strategic_claim_reason(
                issue(html_url="https://github.com/nodejs/undici/issues/5912", body=""),
                [
                    {
                        "body": "https://github.com/SomeoneElse/undici/tree/fetch/issue-5912",
                        "updated_at": recent,
                        "user": {"login": "observer"},
                    }
                ],
            )
        )

    def test_claim_reason_handles_unidentifiable_issue_without_branch_matching(self) -> None:
        self.assertIsNone(
            competition.strategic_claim_reason(
                {"html_url": "bad", "body": ""},
                [{"body": "Thanks for the report.", "user": {"login": "observer"}}],
            )
        )

    def test_supplemental_claims_require_first_person_ownership_language(self) -> None:
        self.assertEqual(
            competition.supplemental_claim_reason(
                issue(),
                [{"body": "I can take this one.", "user": {"login": "dev"}}],
            ),
            "active claim by @dev",
        )
        self.assertEqual(
            competition.supplemental_claim_reason(
                issue(),
                [{"body": "Planning a fix."}],
            ),
            "active claim by @someone",
        )
        self.assertIsNone(
            competition.supplemental_claim_reason(
                issue(),
                [
                    {"body": "I can review a PR for this."},
                    {"body": "Someone should work on a fix."},
                    {"body": "Thanks for the report."},
                ],
            )
        )
        self.assertIsNone(competition.supplemental_claim_reason(issue(comments=0), []))


class LinkedPullRequestTests(unittest.TestCase):
    def test_linked_pr_checks_open_state_and_deduplicates_candidates(self) -> None:
        comments: list[GitHubComment] = [
            {"body": ("submitted PR #10; implementation pull request #11; also submitted PR #10")}
        ]
        with patch.object(
            github,
            "github_get",
            side_effect=[
                {"state": "closed", "html_url": "https://github.com/example/project/pull/10"},
                {"state": "open", "html_url": "https://github.com/example/project/pull/11"},
            ],
        ) as getter:
            self.assertEqual(
                competition.linked_open_pr_reason(issue(), "t", comments),
                "existing open implementation PR: https://github.com/example/project/pull/11",
            )
            self.assertEqual(getter.call_count, 2)

    def test_linked_pr_uses_same_repo_urls_and_ignores_non_pr_api_results(self) -> None:
        comments: list[GitHubComment] = [
            {
                "body": (
                    "See https://github.com/other/project/pull/88 for background. "
                    "Related fix #12 may also help."
                )
            }
        ]
        with patch.object(github, "github_get", return_value=[]):
            self.assertIsNone(competition.linked_open_pr_reason(issue(), "t", comments))

        same_repo: list[GitHubComment] = [
            {
                "body": "Implementation: https://github.com/example/project/pull/13",
            }
        ]
        with patch.object(
            github,
            "github_get",
            return_value={"state": "open"},
        ):
            self.assertEqual(
                competition.linked_open_pr_reason(issue(), "t", same_repo),
                "existing open implementation PR: https://github.com/example/project/pull/13",
            )

    def test_linked_pr_detects_implementation_link_in_issue_body_without_comments(self) -> None:
        item = issue(
            comments=0,
            body=(
                "PR https://github.com/example/project/pull/6293 "
                "fixes exactly this and has been open for review."
            ),
        )
        with patch.object(
            github,
            "github_get",
            return_value={
                "state": "open",
                "html_url": "https://github.com/example/project/pull/6293",
            },
        ) as getter:
            self.assertEqual(
                competition.linked_open_pr_reason(item, "t", []),
                "existing open implementation PR: https://github.com/example/project/pull/6293",
            )
        getter.assert_called_once()

        background = issue(
            comments=0,
            body=(
                "For historical context see "
                "https://github.com/example/project/pull/111 from the earlier refactor."
            ),
        )
        with patch.object(github, "github_get") as no_fetch:
            self.assertIsNone(competition.linked_open_pr_reason(background, "t", []))
        no_fetch.assert_not_called()

    def test_linked_pr_short_circuits_invalid_issue_and_zero_comments(self) -> None:
        with patch.object(github, "github_get") as getter:
            self.assertIsNone(
                competition.linked_open_pr_reason(
                    {"html_url": "bad", "comments": 1},
                    "t",
                    [],
                )
            )
            self.assertIsNone(competition.linked_open_pr_reason(issue(comments=0), "t", []))
            getter.assert_not_called()


class TimelinePullRequestTests(unittest.TestCase):
    def test_timeline_requires_open_cross_referenced_pull_request_with_url(self) -> None:
        timeline = [
            {"event": "commented"},
            {"event": "cross-referenced", "source": "bad"},
            {"event": "cross-referenced", "source": {"issue": "bad"}},
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "pull_request": {"url": "x"},
                        "state": "closed",
                        "html_url": "https://github.com/example/project/pull/8",
                    }
                },
            },
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "pull_request": {"url": "x"},
                        "state": "open",
                        "html_url": "https://github.com/example/project/pull/9",
                    }
                },
            },
        ]
        with patch.object(github, "github_get", return_value=timeline):
            self.assertEqual(
                competition.timeline_open_pr_reason(issue(), "t"),
                "existing open implementation PR: https://github.com/example/project/pull/9",
            )

        with patch.object(github, "github_get", return_value={}):
            self.assertEqual(
                competition.timeline_open_pr_reason(issue(), "t"),
                "could not verify open implementation PR timeline",
            )
        self.assertEqual(
            competition.timeline_open_pr_reason({"html_url": "bad"}, "t"),
            "could not identify repository/issue number",
        )

    def test_timeline_handles_invalid_failure_and_no_match_paths(self) -> None:
        self.assertEqual(
            competition.timeline_open_pr_reason({"html_url": "bad"}, "t"),
            "could not identify repository/issue number",
        )

        with patch.object(github, "github_get", return_value={}):
            self.assertEqual(
                competition.timeline_open_pr_reason(issue(), "t"),
                "could not verify open implementation PR timeline",
            )

        no_match_timeline = [
            {"event": "commented"},
            {"event": "cross-referenced", "source": "invalid"},
            {"event": "cross-referenced", "source": {"issue": "invalid"}},
            {"event": "cross-referenced", "source": {"issue": {"state": "open"}}},
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "pull_request": {"url": "x"},
                        "state": "closed",
                        "html_url": "https://github.com/example/project/pull/11",
                    }
                },
            },
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "pull_request": {"url": "y"},
                        "state": "open",
                    }
                },
            },
        ]
        with patch.object(github, "github_get", return_value=no_match_timeline):
            self.assertIsNone(competition.timeline_open_pr_reason(issue(), "t"))


class CompetitionOrchestrationTests(unittest.TestCase):
    def test_strategic_competition_short_circuits_claim_then_link_then_timeline(self) -> None:
        self.assertEqual(
            competition.strategic_competition_reason(
                issue(),
                "t",
                [],
                timeline_pr_checker=lambda *_: "timeline pr",
                linked_pr_checker=lambda *_: "linked pr",
                strategic_claim_checker=lambda *_: "claim",
            ),
            "claim",
        )
        self.assertEqual(
            competition.strategic_competition_reason(
                issue(),
                "t",
                [],
                timeline_pr_checker=lambda *_: "timeline pr",
                linked_pr_checker=lambda *_: "linked pr",
                strategic_claim_checker=lambda *_: None,
            ),
            "linked pr",
        )
        self.assertEqual(
            competition.strategic_competition_reason(
                issue(),
                "t",
                [],
                timeline_pr_checker=lambda *_: "timeline pr",
                linked_pr_checker=lambda *_: None,
                strategic_claim_checker=lambda *_: None,
            ),
            "timeline pr",
        )
        self.assertIsNone(
            competition.strategic_competition_reason(
                issue(),
                "t",
                [],
                timeline_pr_checker=lambda *_: None,
                linked_pr_checker=lambda *_: None,
                strategic_claim_checker=lambda *_: None,
            )
        )

    def test_extended_competition_preserves_paid_claim_rules_then_supplemental(self) -> None:
        with patch.object(paid_verification, "has_existing_implementation_pr", return_value=None):
            self.assertEqual(
                competition.extended_competition_reason(
                    issue(),
                    "t",
                    [{"body": "I'm working on this", "user": {"login": "dev"}}],
                    linked_pr_checker=lambda *_: None,
                ),
                "active claim by @dev",
            )
            self.assertEqual(
                competition.extended_competition_reason(
                    issue(),
                    "t",
                    [{"body": "Planning a fix", "user": {"login": "dev2"}}],
                    linked_pr_checker=lambda *_: None,
                ),
                "active claim by @dev2",
            )
            self.assertIsNone(
                competition.extended_competition_reason(
                    issue(),
                    "t",
                    [{"body": "I can review a PR."}],
                    linked_pr_checker=lambda *_: None,
                )
            )

    def test_competition_rejects_unidentifiable_issue_without_network_calls(self) -> None:
        bad = issue(html_url="bad", comments=1)
        with patch.object(paid_verification, "has_existing_implementation_pr") as existing:
            self.assertEqual(
                competition.strategic_competition_reason(bad, "t", []),
                "could not identify repository/issue number",
            )
            self.assertEqual(
                competition.extended_competition_reason(bad, "t", []),
                "could not identify repository/issue number",
            )
            existing.assert_not_called()


if __name__ == "__main__":
    unittest.main()
