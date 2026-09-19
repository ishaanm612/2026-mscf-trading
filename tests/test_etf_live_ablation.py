"""Fresh-heat and policy-isolation regressions for live ETF ablations."""
import unittest
from unittest.mock import patch

from scripts.run_etf_live_ablation import (POLICIES, parse_args, run, verify_flat_new_heat, verify_worker_flat,
                                            wait_for_fresh_heat, worker_command)
from volatility.supervisor import SessionMarker


class CaseClient:
    def __init__(self, rows):
        self.rows = iter(rows)

    def get(self, endpoint):
        self.assert_endpoint = endpoint
        return next(self.rows)


def case(status, tick, period=1):
    return {"status": status, "tick": tick, "period": period}


class LiveAblationTests(unittest.TestCase):
    def test_active_heat_at_launch_is_never_joined(self):
        client = CaseClient([case("ACTIVE", 120), case("ACTIVE", 121), case("STOPPED", 0), case("ACTIVE", 1)])
        with patch("scripts.run_etf_live_ablation.time.sleep"):
            marker = wait_for_fresh_heat(client, .1)
        self.assertEqual(marker.tick, 1)

    def test_active_tick_reset_is_a_boundary_without_a_stop_poll(self):
        client = CaseClient([case("ACTIVE", 298), case("ACTIVE", 299), case("ACTIVE", 0)])
        with patch("scripts.run_etf_live_ablation.time.sleep"):
            marker = wait_for_fresh_heat(client, .1)
        self.assertEqual(marker.tick, 0)

    def test_each_policy_gets_isolated_logs_and_no_basket_flag(self):
        args = parse_args(["--trade", "--gross-limit", "300000", "--net-limit", "200000", "--run-id", "test"])
        commands = [worker_command(args, policy) for policy in POLICIES]
        self.assertTrue(all("--basket" not in command for command in commands))
        self.assertEqual(len({command[command.index("--journal") + 1] for command in commands}), len(POLICIES))
        self.assertIn("--no-staged-tenders", commands[0])
        self.assertIn("--staged-min-active-intervals", commands[1])
        self.assertIn("--record", commands[2])

    def test_nonflat_or_late_account_never_starts_an_arm(self):
        class SnapshotClient:
            def __init__(self, snapshot):
                self.value = snapshot

            def snapshot(self, *_args, **_kwargs):
                return self.value

        base = {"case": case("ACTIVE", 1), "orders": [],
                "securities": [{"ticker": ticker, "position": 0} for ticker in
                               ("CAD", "USD", "BULL", "BEAR", "RITC")]}
        verify_flat_new_heat(SnapshotClient(base), wait_for_fresh_heat(CaseClient([case("STOPPED", 0), case("ACTIVE", 1)]), .001))
        inventory = {**base, "securities": [dict(row) for row in base["securities"]]}
        inventory["securities"][-1]["position"] = 1
        with self.assertRaisesRegex(RuntimeError, "not flat"):
            verify_flat_new_heat(SnapshotClient(inventory), SessionMarker(1, 1, "ACTIVE"))
        late = {**base, "case": case("ACTIVE", 3)}
        with self.assertRaisesRegex(RuntimeError, "advanced"):
            verify_flat_new_heat(SnapshotClient(late), SessionMarker(1, 1, "ACTIVE"))

    def test_nonflat_final_snapshot_does_not_count_as_a_completed_arm(self):
        args = parse_args(["--trade", "--gross-limit", "300000", "--net-limit", "200000", "--run-id", "test"])
        policy = POLICIES[0]
        path = f"data/etf-ablation-{args.run_id}-{policy.name}-decisions.jsonl"
        row = {"account_before": {"positions": {"BULL": 10, "BEAR": 0, "RITC": 0, "USD": 0}}}
        with patch("pathlib.Path.read_text", return_value=__import__("json").dumps(row) + "\n"):
            with self.assertRaisesRegex(RuntimeError, "did not finish flat"):
                verify_worker_flat(args, policy)

    def test_nonflat_completed_heat_does_not_prevent_later_arms(self):
        args = parse_args(["--trade", "--gross-limit", "300000", "--net-limit", "200000", "--max-heats", "2", "--run-id", "test"])
        markers = [SessionMarker(1, 1, "ACTIVE"), SessionMarker(2, 1, "ACTIVE")]
        with patch("scripts.run_etf_live_ablation.Client"), \
             patch("scripts.run_etf_live_ablation.wait_for_fresh_heat", side_effect=markers), \
             patch("scripts.run_etf_live_ablation.verify_flat_new_heat"), \
             patch("scripts.run_etf_live_ablation.subprocess.Popen") as popen, \
             patch("scripts.run_etf_live_ablation.verify_worker_flat", side_effect=[RuntimeError("not flat"), None]):
            popen.return_value.wait.return_value = 0
            run(args)
        self.assertEqual(popen.call_count, 2)


if __name__ == "__main__":
    unittest.main()
