"""Tender acknowledgement may precede inventory settlement; never resend POST."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from execution import Executor
from client import RITReadError
from tests.test_trading import Exchange
from tests.test_etf_execution_policy import offer


class TenderConfirmationTests(unittest.TestCase):
    def confirm(self, *, delay=0, unexpected=False, reset=False, timeout=False):
        ex = Exchange("etf")
        tender = offer()
        ex.state["tenders"] = [tender]
        original = ex.get
        reads = 0

        def delayed(endpoint):
            nonlocal reads
            if endpoint == "securities":
                reads += 1
                rows = original(endpoint)
                for s in rows:
                    if s["ticker"] == "RITC":
                        if unexpected:
                            s["position"] = 5000
                        elif timeout or reads <= delay:
                            s["position"] = 0
                if reset:
                    ex.state["case"]["period"] = 2
                return rows
            return original(endpoint)

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "journal.jsonl"
            executor = Executor(ex, path)
            with patch.object(ex, "get", side_effect=delayed), patch("execution.time.sleep"):
                if unexpected or reset or timeout:
                    with self.assertRaisesRegex(RuntimeError, "reconcile"):
                        executor.tender(tender, 0)
                    self.assertTrue(executor.unresolved_intent)
                else:
                    executor.tender(tender, 0)
                    self.assertFalse(executor.unresolved_intent)
            executor.close()
            events = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(ex.requests), 1)
            self.assertEqual(events[0]["expected_position"], 10000)
            if unexpected or reset or timeout:
                self.assertEqual(events[-1]["event"], "tender_confirmation_failed")
                with self.assertRaisesRegex(RuntimeError, "Unresolved"):
                    Executor(ex, path)
            else:
                self.assertEqual(events[-1]["event"], "tender_confirmed")
                self.assertEqual(events[-1]["attempts"], delay + 1)
            return reads

    def test_delayed_settlement_is_confirmed_without_resubmission(self):
        self.assertEqual(self.confirm(delay=3), 4)

    def test_unchanged_position_times_out_and_journal_stays_unresolved(self):
        self.assertEqual(self.confirm(timeout=True), 12)

    def test_partial_or_unexpected_position_stops_immediately(self):
        self.assertEqual(self.confirm(unexpected=True), 1)

    def test_session_reset_cannot_confirm_position_coincidence(self):
        self.assertEqual(self.confirm(reset=True), 1)

    def test_expired_tender_is_not_submitted_or_journaled_as_ambiguous(self):
        ex = Exchange("etf")
        ex.state["case"]["tick"] = 31
        with tempfile.TemporaryDirectory() as d:
            executor = Executor(ex, Path(d) / "journal.jsonl")
            try:
                with self.assertRaises(RITReadError):
                    executor.tender(offer(), 0)
                self.assertFalse(executor.unresolved_intent)
                self.assertEqual(ex.requests, [])
            finally:
                executor.close()


if __name__ == "__main__":
    unittest.main()
