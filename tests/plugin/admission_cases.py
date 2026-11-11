"""Admission regressions: synthetic config, real public registration API."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
import unittest
from plugin import register
from plugin.policy import BLOCK_MESSAGE

GOOD = {'plugins': {'isolation': 'in_process', 'entries': {
    'pocket-platform': {'settings': {'dedicated_pocket_only': True}}}},
    'gateway': {'multiplex_profiles': False, 'platforms': {'pocket': {'enabled': True}}}}

class Context:
    def __init__(self, handles=True):
        self.handles = handles
        self.hook = self.factory = self.unload = None
    def register_hook(self, name, callback):
        assert name == 'pre_tool_call'
        self.hook = callback
        return SimpleNamespace(active=True) if self.handles else None
    def on_unload(self, callback): self.unload = callback
    def register_platform(self, **kwargs):
        self.factory = kwargs['adapter_factory']
        return SimpleNamespace(active=True) if self.handles else None

class Admission(unittest.TestCase):
    def test_probe_none_handles_and_no_configuration_io(self):
        ctx = Context(False)
        with patch('plugin.policy.load_config_readonly', side_effect=AssertionError('registration read config')):
            register(ctx)
        self.assertIsNone(ctx.hook(task_id=None))
        with patch('plugin.policy.load_config_readonly', return_value=GOOD):
            with self.assertRaises(RuntimeError): ctx.factory(None)
        self.assertIsNone(ctx.hook(task_id='unknown'))

    def test_wrong_unconfigured_shared_routed_and_hosted_refuse_without_veto(self):
        configs = [{}, {'plugins': {}}, deepcopy(GOOD), deepcopy(GOOD), deepcopy(GOOD), deepcopy(GOOD), deepcopy(GOOD)]
        configs[2]['plugins']['entries']['pocket-platform']['settings']['dedicated_pocket_only'] = False
        configs[3]['gateway']['platforms']['telegram'] = {'enabled': True}
        configs[4]['gateway']['profile_routes'] = {'telegram': 'other'}
        configs[5]['gateway']['multiplex_profiles'] = True
        configs[6]['plugins']['isolation'] = 'subprocess'
        for config in configs:
            with self.subTest(config=config):
                ctx = Context()
                register(ctx)
                with patch('plugin.policy.load_config_readonly', return_value=config):
                    with self.assertRaises(RuntimeError): ctx.factory(None)
                for identity in (None, 'unknown', 'unrelated-production'):
                    self.assertIsNone(ctx.hook(task_id=identity))

    def test_real_registry_unconfigured_install_does_not_veto_unrelated_tool(self):
        import json
        import native_cases as base
        from hermes_cli.plugins import PluginContext
        from model_tools import handle_function_call
        from tools.registry import registry
        calls = []
        registry.register(name='admission_unrelated_fixture', toolset='admission_fixture',
            schema={'name': 'admission_unrelated_fixture', 'description': 'synthetic',
                    'parameters': {'type': 'object', 'properties': {}}},
            handler=lambda args, **kwargs: calls.append(True) or '{}')
        ctx = PluginContext(base.manifest, base.manager)
        from plugin.policy import install
        policy = install(ctx)
        # Existing fixture's explicitly armed dedicated veto is independently
        # disabled only for this unrelated-profile installation regression.
        from gateway.platform_registry import platform_registry
        entry = platform_registry.get('pocket')
        factory = entry.adapter_factory
        existing = next(c.cell_contents for c in factory.__closure__ if hasattr(c.cell_contents, 'block'))
        try:
            with patch.object(existing, 'active', False), patch('plugin.policy.load_config_readonly', return_value={}):
                self.assertEqual(json.loads(handle_function_call('admission_unrelated_fixture', {}, task_id=None)), {})
                self.assertEqual(calls, [True])
                with self.assertRaises(RuntimeError): policy.activate()
        finally:
            policy.close()
            policy.handle.dispose()

    def test_armed_veto_remains_fail_closed_until_unload(self):
        ctx = Context()
        register(ctx)
        with patch('plugin.policy.load_config_readonly', return_value=GOOD), patch('plugin.adapter.PocketAdapter', return_value='fixture'):
            self.assertEqual(ctx.factory(None), 'fixture')
        for identity in (None, 'unknown', 'mcp_foreign'):
            self.assertEqual(ctx.hook(task_id=identity), {'action': 'block', 'message': BLOCK_MESSAGE})
        ctx.unload()
        self.assertIsNone(ctx.hook(task_id=None))
        with patch('plugin.policy.load_config_readonly', return_value=GOOD):
            with self.assertRaises(RuntimeError): ctx.factory(None)
