"""Every recorded transaction case still replays to the same numbers.

See tests/transactions/replay.py for what these fixtures are and are not. In
short: they pin today's behaviour so a refactor cannot change money silently.
They are not a correctness argument on their own.

If one fails, do not edit the expected block. Either fix the change, or run
`python tests/transactions/replay.py --regenerate` and record the behaviour
change in the audit ledger.
"""

from __future__ import annotations

import unittest

from .transactions.replay import check, load_cases


class TestTransactionReplay(unittest.TestCase):
    def test_every_recorded_case_replays_unchanged(self) -> None:
        cases = load_cases()
        self.assertGreater(len(cases), 0, "no transaction cases found")
        for case in cases:
            with self.subTest(case=case["name"]):
                result = check(case)
                self.assertTrue(result.passed, result.detail)

    def test_every_case_documents_what_it_covers(self) -> None:
        """A fixture nobody can read is a fixture nobody will maintain."""
        for case in load_cases():
            with self.subTest(case=case["name"]):
                self.assertTrue(case.get("description", "").strip())
                self.assertTrue(case.get("sequence"))
                self.assertEqual(len(case["expected"]), len(case["sequence"]))


if __name__ == "__main__":
    unittest.main()
