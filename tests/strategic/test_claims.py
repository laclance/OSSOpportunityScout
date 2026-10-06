from __future__ import annotations

import unittest

from opportunity_scout.strategic.claims import normalized_claim_text, strategic_claim_text


class StrategicClaimTextTests(unittest.TestCase):
    def test_normalizes_curly_apostrophes(self) -> None:
        self.assertEqual(
            normalized_claim_text("I’d like to work on this."),
            "I'd like to work on this.",
        )
        self.assertEqual(normalized_claim_text("I‘ll open a PR."), "I'll open a PR.")

    def test_recognizes_supported_claim_categories(self) -> None:
        claims = (
            "I'd like to pick this up.",
            "Taking this one — I'll trace the parser.",
            "Claiming this DBIP. Plan: add validation.",
            "/attempt",
            "I have that working with a test on a branch.",
            "I poked at this locally. I made the selector win and moved the logic.",
            "Planning a fix.",
            "Submitted a PR.",
            "I'd be happy to submit a pull request.",
            "Would maintainers welcome a PR?",
            "My plan is to add coverage.",
            "I'm going to refactor the parser.",
        )
        for claim in claims:
            with self.subTest(claim=claim):
                self.assertTrue(strategic_claim_text(claim))

    def test_recognition_is_case_insensitive_and_accepts_curly_contractions(self) -> None:
        self.assertTrue(strategic_claim_text("I’M GOING TO FIX THIS."))
        self.assertTrue(strategic_claim_text("I’D LIKE TO WORK ON THIS."))

    def test_multiple_claim_forms_in_one_body_remain_a_claim(self) -> None:
        self.assertTrue(
            strategic_claim_text(
                "I'd like to pick this up. I can prepare a fix and I'll open a PR."
            )
        )

    def test_rejects_third_person_discussion_and_non_implementation_activity(self) -> None:
        non_claims = (
            "Someone else is working on a fix.",
            "Someone else is taking this one.",
            "I'd like to see someone pick this up.",
            "A contributor could implement this.",
            "Maybe Alice will submit a PR.",
            "The parser currently implements this behavior.",
            "I poked at this locally but did not change anything.",
            "I'm going to review the PR.",
        )
        for text in non_claims:
            with self.subTest(text=text):
                self.assertFalse(strategic_claim_text(text))


if __name__ == "__main__":
    unittest.main()
