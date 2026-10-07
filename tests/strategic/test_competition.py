from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import call, patch

from opportunity_scout import github, paid_verification
from opportunity_scout.strategic import competition
from opportunity_scout.types import GitHubComment, SourceFailureReason
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
        self.assertIsNone(
            competition.strategic_claim_reason(
                issue(body="This needs an implementation decision.", created_at=recent),
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

    def test_aws_reporter_owned_followup_pr_is_active_ownership(self) -> None:
        recent = datetime.now(timezone.utc).isoformat()
        aws_issue = issue(
            html_url=(
                "https://github.com/kubernetes-sigs/aws-load-balancer-controller/issues/4874"
            ),
            user={"login": "a7i"},
            created_at=recent,
            body=(
                "Support discovering NLB EIP allocations via EC2 tags. "
                "Implementation PR will follow from fork "
                "`a7i/aws-load-balancer-controller`."
            ),
        )

        self.assertEqual(
            competition.strategic_claim_reason(aws_issue, []),
            "issue author already has an implementation/fix in progress",
        )

    def test_reporter_followup_pr_requires_matching_fork_owner(self) -> None:
        recent = datetime.now(timezone.utc).isoformat()
        for body in (
            "Implementation PR will follow after maintainer review.",
            "Implementation PR will follow from fork `someone-else/project`.",
        ):
            with self.subTest(body=body):
                self.assertIsNone(
                    competition.strategic_claim_reason(
                        issue(
                            user={"login": "reporter"},
                            created_at=recent,
                            body=body,
                        ),
                        [],
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
        self.assertIsNone(
            competition.strategic_claim_reason(
                issue(html_url="https://github.com/nodejs/undici/issues/5912", body=""),
                [
                    {
                        "body": "https://github.com/observer/undici/tree/fetch/issue-6000",
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


class CanonicalIssueReferenceTests(unittest.TestCase):
    def test_cilium_context_issue_redirects_to_open_canonical_issue(self) -> None:
        item = issue(
            html_url="https://github.com/cilium/cilium/issues/45712",
            body=(
                "We also found an existing issue "
                "(https://github.com/cilium/cilium/issues/5051) "
                "requesting for incremental xDS in envoy."
            ),
        )
        with patch.object(
            github,
            "github_get",
            return_value={
                "state": "open",
                "html_url": "https://github.com/cilium/cilium/issues/5051",
            },
        ) as getter:
            self.assertEqual(
                competition.canonical_open_issue_reason(item, "t"),
                (
                    "same work is already tracked by open canonical issue: "
                    "https://github.com/cilium/cilium/issues/5051"
                ),
            )
        getter.assert_called_once_with(
            "https://api.github.com/repos/cilium/cilium/issues/5051",
            "t",
        )

    def test_cilium_contributor_duplicate_redirect_verifies_open_canonical_issue(self) -> None:
        item = issue(html_url="https://github.com/cilium/cilium/issues/47400", body="")
        comments: list[GitHubComment] = [
            {
                "author_association": "CONTRIBUTOR",
                "body": (
                    "I should note that it seems likely that this is a duplicate of #46260; "
                    "it may be more fruitful to move discussion to there."
                ),
            }
        ]
        with patch.object(
            github,
            "github_get",
            return_value={
                "state": "open",
                "html_url": "https://github.com/cilium/cilium/issues/46260",
            },
        ) as getter:
            self.assertEqual(
                competition.canonical_open_issue_reason(item, "t", comments),
                (
                    "same work is already tracked by open canonical issue: "
                    "https://github.com/cilium/cilium/issues/46260"
                ),
            )
        getter.assert_called_once_with(
            "https://api.github.com/repos/cilium/cilium/issues/46260",
            "t",
        )

    def test_comment_duplicate_redirect_requires_authority_redirect_and_open_target(self) -> None:
        item = issue(body="")
        cases: tuple[tuple[GitHubComment, bool], ...] = (
            (
                {
                    "author_association": "NONE",
                    "body": "This seems likely a duplicate of #17; move discussion to there.",
                },
                False,
            ),
            (
                {
                    "author_association": "CONTRIBUTOR",
                    "body": "This seems likely a duplicate of #17.",
                },
                False,
            ),
            (
                {
                    "author_association": "CONTRIBUTOR",
                    "body": "This is not a duplicate of #17; move discussion to there.",
                },
                False,
            ),
        )
        for candidate_comment, should_fetch in cases:
            with (
                self.subTest(comment=candidate_comment),
                patch.object(github, "github_get") as getter,
            ):
                self.assertIsNone(
                    competition.canonical_open_issue_reason(item, "t", [candidate_comment])
                )
            self.assertEqual(getter.called, should_fetch)

        with patch.object(
            github,
            "github_get",
            return_value={"state": "closed"},
        ) as getter:
            self.assertIsNone(
                competition.canonical_open_issue_reason(
                    item,
                    "t",
                    [
                        {
                            "author_association": "MEMBER",
                            "body": (
                                "It appears this is a duplicate of #17; "
                                "please move discussion to there."
                            ),
                        }
                    ],
                )
            )
        getter.assert_called_once_with(
            "https://api.github.com/repos/example/project/issues/17",
            "t",
        )

    def test_comment_canonical_references_skip_self_and_dedupe_body_target(self) -> None:
        item = issue(body="The canonical issue #17 tracks this feature already.")
        comments: list[GitHubComment] = [
            {
                "author_association": "CONTRIBUTOR",
                "body": ("It appears this is a duplicate of #42; move discussion to there."),
            },
            {
                "author_association": "MEMBER",
                "body": (
                    "It appears this is a duplicate of #17; move discussion to there. "
                    "#17 is the target."
                ),
            },
        ]
        with patch.object(
            github,
            "github_get",
            return_value={"state": "closed"},
        ) as getter:
            self.assertIsNone(competition.canonical_open_issue_reason(item, "t", comments))
        getter.assert_called_once_with(
            "https://api.github.com/repos/example/project/issues/17",
            "t",
        )

    def test_canonical_issue_shorthand_is_verified_and_closed_target_is_allowed(self) -> None:
        item = issue(
            body="The canonical issue #17 tracks this feature already.",
        )
        with patch.object(
            github,
            "github_get",
            return_value={"state": "closed"},
        ) as getter:
            self.assertIsNone(competition.canonical_open_issue_reason(item, "t"))
        getter.assert_called_once_with(
            "https://api.github.com/repos/example/project/issues/17",
            "t",
        )

    def test_ordinary_cross_references_do_not_trigger_canonical_lookup(self) -> None:
        ordinary = (
            "Part of #9455.",
            "xref: #3320",
            "See https://github.com/example/project/issues/18 for background.",
            "We found an existing issue #19 for historical context.",
            "The current issue #42 requests the same feature.",
        )
        for body in ordinary:
            with self.subTest(body=body), patch.object(github, "github_get") as getter:
                self.assertIsNone(competition.canonical_open_issue_reason(issue(body=body), "t"))
            getter.assert_not_called()

    def test_canonical_issue_fallback_url_and_duplicate_references(self) -> None:
        item = issue(
            body=(
                "The canonical issue #42 tracks this feature. "
                "An existing issue #17 tracks this feature, and #17 tracks the same work."
            ),
        )
        with patch.object(
            github,
            "github_get",
            return_value={"state": "open"},
        ) as getter:
            self.assertEqual(
                competition.canonical_open_issue_reason(item, "t"),
                (
                    "same work is already tracked by open canonical issue: "
                    "https://github.com/example/project/issues/17"
                ),
            )
        getter.assert_called_once_with(
            "https://api.github.com/repos/example/project/issues/17",
            "t",
        )

    def test_canonical_lookup_fails_closed_on_unusable_or_unknown_state(self) -> None:
        item = issue(body="An existing issue #17 requests the same feature.")
        unusable_results: tuple[object, ...] = (None, [], {"state": "unknown"})
        for result in unusable_results:
            with (
                self.subTest(result=result),
                patch.object(
                    github,
                    "github_get",
                    return_value=result,
                ),
            ):
                reason = competition.canonical_open_issue_reason(item, "t")
            self.assertEqual(reason, "could not verify canonical issue reference")
            self.assertIsInstance(reason, SourceFailureReason)

    def test_canonical_lookup_ignores_unidentifiable_issue(self) -> None:
        with patch.object(github, "github_get") as getter:
            self.assertIsNone(
                competition.canonical_open_issue_reason(
                    {"html_url": "bad", "body": "Existing issue #17 tracks this feature."},
                    "t",
                )
            )
        getter.assert_not_called()


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
            self.assertEqual(
                getter.call_args_list,
                [
                    call("https://api.github.com/repos/example/project/pulls/10", "t"),
                    call("https://api.github.com/repos/example/project/pulls/11", "t"),
                ],
            )

    def test_linked_pr_uses_same_repo_urls_and_fails_closed_on_unusable_results(self) -> None:
        comments: list[GitHubComment] = [
            {
                "body": (
                    "See https://github.com/other/project/pull/88 for background. "
                    "Related fix #12 may also help."
                )
            }
        ]
        unusable_results: tuple[object, ...] = (None, [], {"state": "unknown"})
        for unusable in unusable_results:
            with self.subTest(unusable=unusable):
                with patch.object(github, "github_get", return_value=unusable):
                    reason = competition.linked_open_pr_reason(issue(), "t", comments)
                self.assertEqual(reason, "could not verify linked implementation PR")
                self.assertIsInstance(reason, SourceFailureReason)

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

    def test_tailscale_issue_body_rejects_merged_linked_implementation(self) -> None:
        item = issue(
            html_url="https://github.com/tailscale/tailscale/issues/21617",
            comments=0,
            body=(
                "Opt-out patch and tests. Want a local opt-out so people without permissions "
                "can opt out. https://github.com/tailscale/tailscale/pull/21616"
            ),
        )
        with patch.object(
            github,
            "github_get",
            return_value={
                "state": "closed",
                "merged": True,
                "html_url": "https://github.com/tailscale/tailscale/pull/21616",
            },
        ) as getter:
            self.assertEqual(
                competition.linked_open_pr_reason(item, "t", []),
                (
                    "linked implementation PR is already merged: "
                    "https://github.com/tailscale/tailscale/pull/21616"
                ),
            )
        getter.assert_called_once_with(
            "https://api.github.com/repos/tailscale/tailscale/pulls/21616",
            "t",
        )

    def test_closed_unmerged_and_generic_merged_comment_links_remain_available(self) -> None:
        item = issue(
            comments=0,
            body=("Implementation patch: https://github.com/example/project/pull/13"),
        )
        with patch.object(
            github,
            "github_get",
            return_value={
                "state": "closed",
                "merged": False,
                "html_url": "https://github.com/example/project/pull/13",
            },
        ):
            self.assertIsNone(competition.linked_open_pr_reason(item, "t", []))

        comments: list[GitHubComment] = [
            {"body": "See https://github.com/example/project/pull/14 for related context."}
        ]
        with patch.object(
            github,
            "github_get",
            return_value={
                "state": "closed",
                "merged": True,
                "html_url": "https://github.com/example/project/pull/14",
            },
        ):
            self.assertIsNone(competition.linked_open_pr_reason(issue(), "t", comments))

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

    def test_linked_pr_ignores_flux_historical_bare_pr_reference(self) -> None:
        comments: list[GitHubComment] = [
            {"body": "PR #1625's health-check requeue is also downstream of build/apply."}
        ]
        with patch.object(github, "github_get") as getter:
            self.assertIsNone(
                competition.linked_open_pr_reason(
                    issue(html_url="https://github.com/fluxcd/flux2/issues/5889"),
                    "t",
                    comments,
                )
            )
        getter.assert_not_called()

    def test_linked_pr_ignores_external_listing_pr_bare_number_echo(self) -> None:
        comments: list[GitHubComment] = [
            {
                "body": (
                    "Listed via https://github.com/github/forgoodfirstissue/pull/494. "
                    "For Good First Issue PR #494 tracks the directory update."
                )
            }
        ]
        with patch.object(github, "github_get") as getter:
            self.assertIsNone(
                competition.linked_open_pr_reason(
                    issue(html_url=("https://github.com/duct-tape2/ai-language-partner/issues/52")),
                    "t",
                    comments,
                )
            )
        getter.assert_not_called()

    def test_linked_pr_ignores_external_implementation_pr_number_echo(self) -> None:
        comments: list[GitHubComment] = [
            {
                "body": (
                    "Verified external-effect advance. Receiver implementation PR #13406: "
                    "https://github.com/QwenLM/qwen-code/pull/13406. "
                    "The receiver implementation remains open and unmerged."
                )
            }
        ]
        with patch.object(github, "github_get") as getter:
            self.assertIsNone(
                competition.linked_open_pr_reason(
                    issue(
                        html_url=(
                            "https://github.com/Nakagawa-master/"
                            "nakagawa-theory-archive/issues/402"
                        )
                    ),
                    "t",
                    comments,
                )
            )
        getter.assert_not_called()

    def test_external_pr_echo_only_suppresses_nearby_matching_number(self) -> None:
        comments: list[GitHubComment] = [
            {
                "body": (
                    "External background: https://github.com/other/project/pull/12. "
                    + ("unrelated context " * 30)
                    + "Implementation PR #12"
                )
            }
        ]
        with patch.object(
            github,
            "github_get",
            return_value={
                "state": "open",
                "html_url": "https://github.com/example/project/pull/12",
            },
        ) as getter:
            self.assertEqual(
                competition.linked_open_pr_reason(issue(), "t", comments),
                "existing open implementation PR: https://github.com/example/project/pull/12",
            )
        getter.assert_called_once_with(
            "https://api.github.com/repos/example/project/pulls/12",
            "t",
        )

    def test_linked_pr_keeps_strong_same_repo_implementation_shorthand(self) -> None:
        comments: list[GitHubComment] = [{"body": "Implementation PR #12"}]
        with patch.object(
            github,
            "github_get",
            return_value={
                "state": "open",
                "html_url": "https://github.com/example/project/pull/12",
            },
        ) as getter:
            self.assertEqual(
                competition.linked_open_pr_reason(issue(), "t", comments),
                "existing open implementation PR: https://github.com/example/project/pull/12",
            )
        getter.assert_called_once_with(
            "https://api.github.com/repos/example/project/pulls/12",
            "t",
        )

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
    def test_timeline_delegates_valid_issue_to_canonical_checker(self) -> None:
        reason = "existing open implementation PR: https://github.com/example/project/pull/9"
        with patch.object(
            paid_verification,
            "has_existing_implementation_pr",
            return_value=reason,
        ) as checker:
            self.assertEqual(competition.timeline_open_pr_reason(issue(), "t"), reason)

        checker.assert_called_once_with("example/project", 42, "t")

    def test_timeline_rejects_unidentifiable_issue_without_canonical_check(self) -> None:
        with patch.object(paid_verification, "has_existing_implementation_pr") as checker:
            self.assertEqual(
                competition.timeline_open_pr_reason({"html_url": "bad"}, "t"),
                "could not identify repository/issue number",
            )

        checker.assert_not_called()


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

    def test_strategic_competition_short_circuits_on_canonical_issue(self) -> None:
        reason = "same work is already tracked by open canonical issue: canonical"
        self.assertEqual(
            competition.strategic_competition_reason(
                issue(),
                "t",
                [],
                canonical_issue_checker=lambda *_: reason,
                strategic_claim_checker=lambda *_: None,
                linked_pr_checker=lambda *_: self.fail("linked PR check should not be reached"),
                timeline_pr_checker=lambda *_: self.fail("timeline PR check should not be reached"),
            ),
            reason,
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

    def test_extended_competition_short_circuits_existing_and_linked_prs(self) -> None:
        self.assertEqual(
            competition.extended_competition_reason(
                issue(),
                "t",
                [],
                existing_pr_checker=lambda *_: "search pr",
                linked_pr_checker=lambda *_: self.fail("linked PR check should not be reached"),
            ),
            "search pr",
        )
        self.assertEqual(
            competition.extended_competition_reason(
                issue(),
                "t",
                [],
                existing_pr_checker=lambda *_: None,
                linked_pr_checker=lambda *_: "linked pr",
                supplemental_claim_checker=lambda *_: self.fail(
                    "supplemental claim check should not be reached"
                ),
            ),
            "linked pr",
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
