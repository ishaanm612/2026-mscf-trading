"""CLI wiring and decision-log regressions for ETF basket controls."""
from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch

import run
from scripts.supervise_volatility import parse_args, supervise, worker_command
from volatility.supervisor import SessionMarker


class ETFBasketCLI(unittest.TestCase):
    def test_account_snapshot_keeps_native_currency_pnl_fields(self):
        record = run.etf_account_before_action({"securities": [
            {"ticker": "BULL", "position": 125, "currency": "CAD",
             "realized": 45.5, "unrealized": -12.25},
            {"ticker": "RITC", "position": -50, "currency": "USD",
             "realized": 8.0, "unrealized": 4.5},
        ]})
        self.assertEqual(record["positions"], {"BULL": 125, "RITC": -50})
        self.assertEqual(record["securities"], [
            {"ticker": "BULL", "position": 125, "currency": "CAD",
             "realized_pnl": 45.5, "unrealized_pnl": -12.25},
            {"ticker": "RITC", "position": -50, "currency": "USD",
             "realized_pnl": 8.0, "unrealized_pnl": 4.5},
        ])

    def test_supervisor_forwards_every_basket_control(self):
        args = parse_args([
            "--case", "etf", "--gross-limit", "300000", "--net-limit", "200000",
            "--basket", "--basket-max-hold-ticks", "45", "--basket-min-hold-ticks", "7",
            "--basket-take-profit", "0.04", "--basket-stop-loss", "0.20",
            "--basket-max-quantity", "12000", "--basket-cooldown-ticks", "9",
        ])
        command = worker_command(args)
        for flag, expected in (
            ("--basket-max-hold-ticks", "45"), ("--basket-min-hold-ticks", "7"),
            ("--basket-take-profit", "0.04"), ("--basket-stop-loss", "0.2"),
            ("--basket-max-quantity", "12000"), ("--basket-cooldown-ticks", "9"),
        ):
            self.assertEqual(command[command.index(flag) + 1], expected)

    def test_run_builds_basket_config_and_logs_pre_action_account_state(self):
        bot = MagicMock()
        bot.step.return_value = {"wait": "no eligible basket"}
        logger = MagicMock()
        argv = ["run.py", "etf", "--plan", "--gross-limit", "300000", "--net-limit", "200000",
                "--decision-log", "data/test-etf-basket-cli.jsonl", "--basket-max-hold-ticks", "45",
                "--basket-min-hold-ticks", "7", "--basket-take-profit", "0.04",
                "--basket-stop-loss", "0.20", "--basket-max-quantity", "12000",
                "--basket-cooldown-ticks", "9"]
        with patch.object(sys, "argv", argv), patch("run.load_env_file"), \
             patch("run.configure_case_environment"), patch("run.Bot", return_value=bot) as bot_type, \
             patch("run.StrategyLogger", return_value=logger):
            run.main()
        config = bot_type.call_args.kwargs["basket_config"]
        self.assertEqual(config.max_hold_ticks, 45)
        self.assertEqual(config.min_hold_ticks, 7)
        self.assertEqual(config.max_quantity, 12000)
        fields = logger.write.call_args.args[1]
        account = fields["account_before"]
        self.assertEqual(account["source_timing"], "before_strategy_action")
        self.assertEqual(account["positions"], {"BULL": 0, "BEAR": 0, "RITC": 0, "USD": 0})
        self.assertEqual(account["securities"][0], {
            "ticker": "BULL", "position": 0, "currency": None,
            "realized_pnl": None, "unrealized_pnl": None,
        })

    def test_supervisor_interrupt_sends_sigint_to_worker(self):
        args = parse_args(["--case", "etf", "--gross-limit", "300000", "--net-limit", "200000"])
        worker = MagicMock()
        worker.wait.side_effect = [KeyboardInterrupt(), 0]
        marker = SessionMarker(status="ACTIVE", period=1, tick=1)
        with patch("scripts.supervise_volatility.wait_for_active_case", return_value=marker), \
             patch("scripts.supervise_volatility.Client"), \
             patch("scripts.supervise_volatility.subprocess.Popen", return_value=worker):
            with self.assertRaises(KeyboardInterrupt):
                supervise(args)
        worker.send_signal.assert_called_once_with(__import__("signal").SIGINT)
        self.assertEqual(worker.wait.call_count, 2)


if __name__ == "__main__":
    unittest.main()
