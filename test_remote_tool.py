import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

WORKERS = {'worker-one': {'host': 'gg-worker', 'launcher': 'C:/work/launch.cmd', 'workdir': 'C:/work/fixture'}}


def load_plugin():
    spec = importlib.util.spec_from_file_location("gogogoogle_plugin_remote", Path(__file__).with_name("__init__.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Ctx:
    def __init__(self, **settings):
        self.settings, self.tools, self.hooks = settings, {}, []

    def get_config(self, key, default):
        return self.settings.get(key, default)

    def register_tool(self, **kwargs):
        self.tools[kwargs['name']] = kwargs

    def register_hook(self, name, fn):
        self.hooks.append((name, fn))


class RemoteToolTests(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch.object(self.plugin, '_home', return_value=Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.home = Path(tmp.name)

    def register(self, **settings):
        ctx = Ctx(**settings)
        self.plugin.register(ctx)
        return ctx

    def configured(self):
        ctx = self.register(remote_workers=WORKERS)
        return ctx.tools['antigravity_remote_review']['handler']

    def test_without_the_setting_there_is_no_remote_tool_and_no_transport(self):
        for settings in ({}, {'remote_workers': {}}, {'remote_workers': None}):
            plugin = load_plugin()
            ctx = Ctx(**settings)
            plugin.register(ctx)
            self.assertEqual(sorted(ctx.tools), ['antigravity_delegate', 'antigravity_job', 'antigravity_stats'])
            self.assertIsNone(plugin._remote_transport)

    def test_bad_setting_shapes_raise(self):
        bad = [{'Bad Name': WORKERS['worker-one']}, {'x': {'host': 'h', 'launcher': 'l'}},
               {'x': {**WORKERS['worker-one'], 'host': ''}}, {'x': 'not an object'}, ['list'], {'a' * 33: WORKERS['worker-one']}]
        for value in bad:
            with self.assertRaises(ValueError, msg=value):
                self.register(remote_workers=value)

    def test_tool_schema_has_only_task_and_worker(self):
        schema = self.register(remote_workers=WORKERS).tools['antigravity_remote_review']['schema']
        self.assertEqual(sorted(schema['parameters']['properties']), ['task', 'worker'])
        self.assertEqual(schema['parameters']['properties']['worker']['enum'], ['worker-one'])
        self.assertEqual(sorted(schema['parameters']['required']), ['task', 'worker'])

    def test_bad_calls_return_an_error_and_never_call_invoke(self):
        handler = self.configured()
        with patch.object(self.plugin._remote_transport, 'invoke') as invoke:
            for args in ({'task': 'review it', 'worker': 'nobody'}, {'task': '', 'worker': 'worker-one'},
                         {'task': '  ', 'worker': 'worker-one'}, {'task': 'x' * 1001, 'worker': 'worker-one'},
                         {'task': 5, 'worker': 'worker-one'}, {'task': 'review it'}, None):
                self.assertIn('error', json.loads(handler(args)), args)
            invoke.assert_not_called()

    def test_valid_call_sends_exactly_the_fixed_request(self):
        handler = self.configured()
        with patch.object(self.plugin._remote_transport, 'invoke', return_value={'status': 'OK'}) as invoke:
            self.assertEqual(json.loads(handler({'task': ' review calc.py ', 'worker': 'worker-one'})), {'status': 'OK'})
        invoke.assert_called_once_with('gg-worker', 'C:/work/launch.cmd', {
            'task': 'review calc.py', 'workdir': 'C:/work/fixture', 'role': 'review', 'minutes': 1,
            'verify': False, 'background': False}, 200)

    def test_model_cannot_change_workdir_role_or_minutes(self):
        handler = self.configured()
        with patch.object(self.plugin._remote_transport, 'invoke', return_value={'status': 'OK'}) as invoke:
            handler({'task': 't', 'worker': 'worker-one', 'workdir': 'C:/elsewhere', 'role': 'do', 'minutes': 30,
                     'verify': True, 'background': True, 'resume': 'abc'})
        request = invoke.call_args.args[2]
        self.assertEqual((request['workdir'], request['role'], request['minutes'], request['verify'],
                          request['background'], 'resume' in request), ('C:/work/fixture', 'review', 1, False, False, False))

    def test_unknown_result_is_returned_unchanged_and_called_once(self):
        handler = self.configured()
        unknown = {'status': 'UNKNOWN', 'error': 'ssh timed out', 'note': 'do not automatically retry'}
        with patch.object(self.plugin._remote_transport, 'invoke', return_value=unknown) as invoke:
            self.assertEqual(json.loads(handler({'task': 't', 'worker': 'worker-one'})), unknown)
        self.assertEqual(invoke.call_count, 1)

    def test_one_log_row_without_the_task_text(self):
        handler = self.configured()
        with patch.object(self.plugin._remote_transport, 'invoke', return_value={'status': 'OK'}):
            handler({'task': 'SECRET-TASK-TEXT', 'worker': 'worker-one'})
        rows = [json.loads(line) for line in (self.home / 'runs.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['role'], rows[0]['status'], rows[0]['remote']), ('review', 'OK', 'worker-one'))
        self.assertIn('secs', rows[0])
        self.assertNotIn('SECRET-TASK-TEXT', json.dumps(rows[0]))

    def test_approval_prompt_names_destination_first_and_task_last(self):
        self.register(remote_workers=WORKERS)
        task = 'Review calc.py for bugs.'
        first = self.plugin._approval('antigravity_remote_review', {'task': ' ' + task + ' ', 'worker': 'worker-one'})
        second = self.plugin._approval('antigravity_remote_review', {'task': task, 'worker': 'worker-one'})
        message = first['message']
        self.assertEqual(first['action'], 'approve')
        self.assertTrue(message.startswith('REMOTE agy review on worker worker-one (ssh gg-worker), remote folder C:/work/fixture'))
        for phrase in ("Antigravity account", 'verifier off', 'not a sandbox', 'outside the remote folder', 'Google'):
            self.assertIn(phrase, message)
        import hashlib
        digest = hashlib.sha256(task.encode()).hexdigest()[:12]
        self.assertTrue(message.endswith(f'Task ({len(task)} chars, sha256 {digest}):\n{task}'))
        self.assertNotEqual(first['rule_key'], second['rule_key'])

    def test_approval_for_other_tools_is_unchanged(self):
        self.register(remote_workers=WORKERS)
        self.assertIsNone(self.plugin._approval('terminal', {'task': 'x'}))
        local = self.plugin._approval('antigravity_delegate', {'task': 'x', 'workdir': 'C:/w', 'role': 'review'})
        self.assertTrue(local['message'].startswith('agy review in C:/w'))


if __name__ == '__main__':
    unittest.main()
