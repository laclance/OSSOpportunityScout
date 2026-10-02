from __future__ import annotations

import io
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest.mock import patch

from bountyscout.strategic import discovery, verification
from bountyscout.types import Candidate, GitHubIssue, IssueRow, RejectionRecord
from tests.helpers import candidate, issue


def row(item: GitHubIssue, *, priority: int = 80, career: int = 80, cash: int = 0) -> IssueRow:
    return priority, career, cash, item


def selection(
    ranked_by_repo: dict[str, list[IssueRow]],
    audit: list[RejectionRecord] | None = None,
) -> discovery.StrategicDiscoverySelection:
    return discovery.StrategicDiscoverySelection(
        ranked_by_repo=ranked_by_repo,
        audit=[] if audit is None else audit,
    )


def verified_candidate(item: GitHubIssue, *, score: int = 80, **overrides: object) -> Candidate:
    values: dict[str, object] = {
        "url": str(item.get("html_url") or ""),
        "priority_score": score,
        "career_score": score,
    }
    values.update(overrides)
    return candidate(**values)


class StrategicVerificationTests(unittest.TestCase):
    def test_preflight_and_upper_bound_skip_network_while_viable_row_verifies(self) -> None:
        viable = issue(
            html_url="https://github.com/g/g/issues/1",
            labels=[{"name": "help wanted"}, {"name": "bug"}],
        )
        preflight = issue(html_url="https://github.com/g/g/issues/2")
        impossible = issue(html_url="https://github.com/g/g/issues/3")
        calls: list[str] = []

        def deep_verify(item_: GitHubIssue) -> tuple[Candidate | None, str | None]:
            calls.append(str(item_.get("html_url") or ""))
            return verified_candidate(item_, score=90), None

        buf = io.StringIO()
        with redirect_stdout(buf):
            result = verification.verify_strategic_selection(
                selection(
                    {
                        "g/g": [
                            row(viable, priority=90, career=90),
                            row(preflight, priority=80, career=80),
                            row(impossible, priority=40, career=40),
                        ]
                    }
                ),
                deep_verify,
                lambda item_: (
                    "issue is marked claimed by the project" if item_ is preflight else None
                ),
            )

        self.assertEqual(calls, [viable["html_url"]])
        self.assertEqual(result.network_checked_rows, 1)
        self.assertEqual(result.selected_rows, 3)
        self.assertEqual([item["url"] for item in result.candidates], [viable["html_url"]])
        self.assertEqual(result.rejected["issue is marked claimed by the project"], 1)
        bound_reason = "career score 40/100 below strategic threshold 55/100"
        self.assertEqual(result.rejected[bound_reason], 1)
        self.assertIn("Strategic deep verification: 1/3", buf.getvalue())

    def test_source_failure_breaker_uses_exact_reasons_and_resets_on_other_result(self) -> None:
        self.assertEqual(
            verification.SOURCE_FAILURE_REASONS,
            {
                "could not refresh source issue",
                "could not refresh issue comments",
                "could not verify open implementation PR timeline",
            },
        )
        items = [
            issue(
                html_url=f"https://github.com/g/g/issues/{index}",
                labels=[{"name": "help wanted"}, {"name": "bug"}],
            )
            for index in range(1, 6)
        ]
        reasons = iter(
            [
                "could not refresh source issue",
                "ordinary rejection",
                "could not refresh issue comments",
                "could not verify open implementation PR timeline",
            ]
        )
        calls: list[str] = []

        def deep_verify(item_: GitHubIssue) -> tuple[Candidate | None, str | None]:
            calls.append(str(item_.get("html_url") or ""))
            return None, next(reasons)

        result = verification.verify_strategic_selection(
            selection(
                {"g/g": [row(item_, career=90 - index) for index, item_ in enumerate(items)]}
            ),
            deep_verify,
            lambda _: None,
        )

        self.assertEqual(len(calls), 4)
        self.assertEqual(result.network_checked_rows, 4)
        self.assertEqual(result.rejected["could not refresh source issue"], 1)
        self.assertEqual(result.rejected["ordinary rejection"], 1)
        self.assertEqual(result.rejected["could not refresh issue comments"], 1)
        self.assertEqual(result.rejected["could not verify open implementation PR timeline"], 1)
        self.assertTrue(
            any(
                item["url"] == items[4]["html_url"]
                and "verification coverage incomplete for g/g" in item["reason"]
                for item in result.audit
            )
        )

    def test_source_failure_on_last_row_marks_incomplete_without_phantom_audit(self) -> None:
        only = issue(
            html_url="https://github.com/g/g/issues/1",
            labels=[{"name": "help wanted"}, {"name": "bug"}],
        )
        result = verification.verify_strategic_selection(
            selection({"g/g": [row(only, career=90)]}),
            lambda _: (None, "could not refresh source issue"),
            lambda _: None,
            refresh_failure_limit=1,
        )

        self.assertEqual(result.candidates, [])
        self.assertEqual(result.audit, [])
        self.assertEqual(result.network_checked_rows, 1)

    def test_repo_settlement_stops_when_remaining_row_cannot_displace_keep_set(self) -> None:
        items = [
            issue(
                html_url=f"https://github.com/g/g/issues/{index}",
                labels=[{"name": "help wanted"}, {"name": "bug"}],
            )
            for index in range(1, 4)
        ]
        scores = [90, 80, 70]
        calls: list[str] = []

        def deep_verify(item_: GitHubIssue) -> tuple[Candidate | None, str | None]:
            calls.append(str(item_["html_url"]))
            index = items.index(item_)
            return verified_candidate(item_, score=scores[index]), None

        result = verification.verify_strategic_selection(
            selection(
                {
                    "g/g": [
                        row(item_, priority=score, career=score)
                        for item_, score in zip(items, scores, strict=True)
                    ]
                }
            ),
            deep_verify,
            lambda _: None,
            keep_per_repo=1,
        )

        self.assertEqual(calls, [items[0]["html_url"], items[1]["html_url"]])
        self.assertEqual([item["url"] for item in result.candidates], [items[0]["html_url"]])
        self.assertEqual(verification.STRATEGIC_KEEP_PER_REPO, 3)
        self.assertEqual(verification.STRATEGIC_VERIFY_SCORE_UPLIFT_BOUND, 11)

    def test_final_filter_audits_near_miss_and_preserves_sort_tuple_and_top_three(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        weak = issue(
            html_url="https://github.com/g/g/issues/1",
            labels=[{"name": "help wanted"}, {"name": "bug"}],
            updated_at=now,
        )
        accepted = [
            issue(html_url=f"https://github.com/g/g/issues/{index}") for index in range(2, 6)
        ]
        returned: dict[str, Candidate] = {
            str(weak["html_url"]): verified_candidate(weak, score=40),
            str(accepted[0]["html_url"]): verified_candidate(
                accepted[0], score=70, cash_score=20, comments=5
            ),
            str(accepted[1]["html_url"]): verified_candidate(
                accepted[1], score=70, cash_score=20, comments=1
            ),
            str(accepted[2]["html_url"]): verified_candidate(
                accepted[2], score=70, cash_score=10, comments=0
            ),
            str(accepted[3]["html_url"]): verified_candidate(
                accepted[3], score=60, cash_score=99, comments=0
            ),
        }

        def deep_verify(item_: GitHubIssue) -> tuple[Candidate | None, str | None]:
            return returned[str(item_["html_url"])], None

        result = verification.verify_strategic_selection(
            selection(
                {
                    "g/g": [
                        row(weak, priority=99, career=90),
                        *[row(item_, priority=80, career=80) for item_ in accepted],
                    ]
                },
                audit=[{"url": "seed", "title": "seed", "reason": "existing discovery audit"}],
            ),
            deep_verify,
            lambda _: None,
            score_uplift_bound=100,
        )

        weak_reason = "career score 40/100 below strategic threshold 55/100"
        self.assertEqual(result.rejected[weak_reason], 1)
        self.assertTrue(any("strong-looking near miss" in item["reason"] for item in result.audit))
        self.assertEqual(result.audit[0]["reason"], "existing discovery audit")
        self.assertEqual(
            [item["url"] for item in result.candidates],
            [accepted[1]["html_url"], accepted[0]["html_url"], accepted[2]["html_url"]],
        )

    def test_below_threshold_candidate_without_miss_signal_skips_audit(self) -> None:
        weak = issue(
            html_url="https://github.com/g/g/issues/9",
            title="Documentation cleanup",
            labels=[],
            updated_at="2025-01-01T00:00:00Z",
        )
        result = verification.verify_strategic_selection(
            selection({"g/g": [row(weak, priority=80, career=80)]}),
            lambda item_: (verified_candidate(item_, score=40), None),
            lambda _: None,
        )

        reason = "career score 40/100 below strategic threshold 55/100"
        self.assertEqual(result.rejected[reason], 1)
        self.assertEqual(result.audit, [])

    def test_rejection_examples_remain_bounded_to_twelve(self) -> None:
        items = [issue(html_url=f"https://github.com/g/g/issues/{index}") for index in range(1, 14)]
        result = verification.verify_strategic_selection(
            selection({"g/g": [row(item_) for item_ in items]}),
            lambda _: self.fail("preflight-rejected rows must not invoke deep verification"),
            lambda _: "blocked",
        )

        self.assertEqual(result.rejected["blocked"], 13)
        self.assertEqual(len(result.examples), 12)
        self.assertEqual(result.network_checked_rows, 0)

    def test_empty_input_is_safe_and_worker_cap_and_repo_order_are_deterministic(self) -> None:
        observed_workers: list[int] = []
        real_executor = ThreadPoolExecutor

        def recording_executor(*, max_workers: int) -> ThreadPoolExecutor:
            observed_workers.append(max_workers)
            return real_executor(max_workers=max_workers)

        empty = verification.verify_strategic_selection(
            selection({}),
            lambda _: self.fail("empty selection must not verify"),
            lambda _: None,
        )
        self.assertEqual(empty.candidates, [])
        self.assertEqual((empty.network_checked_rows, empty.selected_rows), (0, 0))

        ranked: dict[str, list[IssueRow]] = {}
        for index in range(9):
            repo = f"r{index}/r{index}"
            item_ = issue(html_url=f"https://github.com/{repo}/issues/1")
            ranked[repo] = [row(item_, priority=90 - index, career=90 - index)]

        with patch.object(verification, "ThreadPoolExecutor", side_effect=recording_executor):
            result = verification.verify_strategic_selection(
                selection(ranked),
                lambda item_: (verified_candidate(item_, score=80), None),
                lambda _: None,
            )

        self.assertEqual(observed_workers, [8])
        self.assertEqual(
            [item["url"] for item in result.candidates],
            [rows[0][3]["html_url"] for rows in ranked.values()],
        )

    def test_candidates_are_verified_sequentially_within_each_repo(self) -> None:
        items = [issue(html_url=f"https://github.com/g/g/issues/{index}") for index in range(1, 4)]
        calls: list[str] = []

        def deep_verify(item_: GitHubIssue) -> tuple[Candidate | None, str | None]:
            calls.append(str(item_["html_url"]))
            return verified_candidate(item_, score=80), None

        verification.verify_strategic_selection(
            selection({"g/g": [row(item_) for item_ in items]}),
            deep_verify,
            lambda _: None,
        )

        self.assertEqual(calls, [str(item_["html_url"]) for item_ in items])


if __name__ == "__main__":
    unittest.main()
