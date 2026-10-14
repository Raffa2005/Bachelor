import unittest
from unittest.mock import patch

from tools.dm_cli import Lc0


class EngineFallbackTest(unittest.TestCase):
    def test_uci_rejects_missing_fallback_support(self):
        with patch('tools.dm_cli.subprocess.Popen'), \
             patch('tools.dm_cli.threading.Thread'), \
             patch('tools.dm_cli._OutputBuffer') as buffer, \
             patch.object(Lc0, '_send'), \
             patch.object(Lc0, '_wait_for', return_value=(1, 'uciok')), \
             patch.object(Lc0, 'quit') as quit_engine:
            buffer.return_value.read.return_value = ['uciok']
            with self.assertRaisesRegex(RuntimeError, 'refusing an unrouted'):
                Lc0('/engine', '/variant', fallback_weights='/standard')
            quit_engine.assert_called_once()
            quit_engine.reset_mock()
            with self.assertRaisesRegex(RuntimeError, 'refusing an unrouted'):
                Lc0('/engine', '/variant', opts={'FallbackWeights': '/standard'})
            quit_engine.assert_called_once()

    def test_uci_sets_advertised_fallback_before_new_game(self):
        with patch('tools.dm_cli.subprocess.Popen'), \
             patch('tools.dm_cli.threading.Thread'), \
             patch('tools.dm_cli._OutputBuffer') as buffer, \
             patch.object(Lc0, '_send') as send, \
             patch.object(Lc0, '_wait_for', return_value=(1, 'uciok')):
            buffer.return_value.read.return_value = [
                'option name FallbackWeights type string default ', 'uciok']
            Lc0('/engine', '/variant', fallback_weights='/standard')
            commands = [call.args[0] for call in send.call_args_list]
            self.assertLess(commands.index('setoption name FallbackWeights value /standard'),
                            commands.index('ucinewgame'))


if __name__ == '__main__':
    unittest.main()
