"""Offline failure-injection checks for the watched runner."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from client import RITReadError
from run import demo, main


class RunnerResilienceTests(unittest.TestCase):
    """Ensure recoverable reads resume and uncertain strategy failures latch."""

    def test_uncertain_step_halts_across_reset_without_reexecution(self) -> None:
        """Keep polling after a failed submission without calling the bot again."""

        first = demo('volatility')
        first['case']['tick'] = 20
        reset = demo('volatility')
        client, bot, executor = MagicMock(), MagicMock(), MagicMock()
        client.snapshot.side_effect = [first, reset, KeyboardInterrupt()]
        bot.step.side_effect = TimeoutError('unknown fill outcome')
        with patch('sys.argv', ['run.py', 'volatility', '--source', 'api', '--trade',
                                '--watch', '--exit-on-session-change']), \
             patch('run.Client', return_value=client), patch('run.Bot', return_value=bot), \
             patch('run.Executor', return_value=executor), patch('run.time.sleep'), \
             patch('builtins.print') as output:
            main()
        bot.step.assert_called_once()
        self.assertEqual(client.snapshot.call_count, 3)
        self.assertTrue(any('submissions disabled' in str(call) for call in output.call_args_list))
        executor.close.assert_called_once()

    def test_read_failure_recovers_without_halt(self) -> None:
        """A snapshot timeout cannot be confused with an ambiguous order outcome."""

        client, bot = MagicMock(), MagicMock()
        client.snapshot.side_effect = [RITReadError('temporary'), demo('volatility'), KeyboardInterrupt()]
        bot.step.return_value = {'wait': 'no signal'}
        with patch('sys.argv', ['run.py', 'volatility', '--source', 'api', '--plan', '--watch']), \
             patch('run.Client', return_value=client), patch('run.Bot', return_value=bot), \
             patch('run.time.sleep'), patch('builtins.print'):
            main()
        bot.step.assert_called_once()
