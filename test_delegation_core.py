import unittest
from unittest.mock import Mock

import delegation_core


class DelegationCoreTests(unittest.TestCase):
    def test_worker_receives_workspace_and_unverified_result_is_explicit(self):
        api = {
            '_snap': Mock(return_value='same'),
            '_git': Mock(return_value=''),
            '_agy': Mock(return_value=('OK', 'checked', '', 'c', {})),
            '_log': Mock(),
            '_item_check': Mock(return_value=None),
            '_verify': Mock(),
            '_role_prompt': Mock(return_value='role'),
            'WORKER': 'test-model',
            'REPLY_LIMIT': 32000,
            'DIFF_LIMIT': 6000,
        }
        result = delegation_core.work('task', '.', 'review', 1, False, '', None, api)
        self.assertEqual(result['status'], 'OK')
        self.assertIn('SKIPPED', result['verify'])
        self.assertEqual(api['_agy'].call_args.args[1], '.')


if __name__ == '__main__':
    unittest.main()
