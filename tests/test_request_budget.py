import unittest

from request_budget import RemoteRequestBudget


class RequestBudgetTests(unittest.TestCase):
    def test_snapshot_reports_limit_used_and_remaining(self):
        budget = RemoteRequestBudget({"embedding": 2, "fallback": 0})

        self.assertTrue(budget.try_acquire("embedding"))
        self.assertFalse(budget.try_acquire("fallback"))

        snapshot = budget.snapshot()

        self.assertEqual(snapshot["embedding"], {"limit": 2, "used": 1, "remaining": 1})
        self.assertEqual(snapshot["fallback"], {"limit": 0, "used": 0, "remaining": 0})


if __name__ == "__main__":
    unittest.main()
