"""V2 tests of real installed dispatch and normal loader (synthetic inputs)."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import unittest
from unittest.mock import patch
import native_cases as base
from test_contract import event, OWNER
from agent.secret_scope import set_secret_scope, reset_secret_scope, set_multiplex_active
from gateway.config import PlatformConfig
from gateway.run_turn import GatewayTurnMixin
from plugin.adapter import PocketAdapter
from plugin.client import BridgeEventQuery
from plugin.policy import install, dedicated_profile, BLOCK_MESSAGE


class PublicPolicy(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.NativeIntegration.asyncSetUp
    asyncTearDown = base.NativeIntegration.asyncTearDown

    async def test_real_dispatch_unknown_concurrent_builtin_plugin_mcp_injection(self):
        from model_tools import handle_function_call
        from tools.registry import registry
        calls = []
        for name, toolset in [('pocket_fixture_tool', 'pocket_fixture'), ('mcp_fixture_attack', 'mcp_fixture')]:
            registry.register(name=name, toolset=toolset,
                              schema={'name': name, 'description': 'synthetic injected test tool',
                                      'parameters': {'type': 'object', 'properties': {}}},
                              handler=lambda args, **kwargs: calls.append(args) or '{}')
        def execute(index):
            name = ['terminal', 'pocket_fixture_tool', 'mcp_fixture_attack'][index % 3]
            return json.loads(handle_function_call(name, {}, task_id=None if index % 2 else 'unknown-fixture'))
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(execute, range(12)))
        self.assertTrue(all(r.get('error') in {BLOCK_MESSAGE, 'pre_tool_call plugin callback timed out or is still running'} for r in results), results)
        self.assertEqual(calls, [])

    async def test_installed_empty_override_is_ignored_but_sentinel_selects_no_tools(self):
        from model_tools import _select_tool_names, get_tool_definitions
        adapter = self.adapter
        source = adapter.build_source(OWNER, user_id=OWNER)
        class Resolver:
            def _delivery_adapter_for(self, unused): return adapter
        config = {'platform_toolsets': {'pocket': ['terminal']}}
        with patch.object(adapter, 'toolsets_for_source', return_value=[]):
            ignored = GatewayTurnMixin._resolve_enabled_toolsets_for_source(Resolver(), config, source, 'pocket')
        self.assertIn('terminal', ignored)
        selected = GatewayTurnMixin._resolve_enabled_toolsets_for_source(Resolver(), config, source, 'pocket')
        self.assertEqual(selected, [])
        self.assertEqual(_select_tool_names(selected, None, quiet_mode=True), set())
        self.assertEqual(get_tool_definitions(enabled_toolsets=selected, quiet_mode=True), [])

    async def test_secret_strict_format_and_scoped_failure_before_socket(self):
        for bad in [None, '', 'A' * 64, 'a' * 63, 'a' * 65, 'a' * 64 + '\n', ' ' + 'a' * 64]:
            token = set_secret_scope({'POCKET_HMAC_KEY': bad})
            adapter = base.make_adapter(PlatformConfig(enabled=True, extra={'socket_dir': str(self.root/'invalid'), 'owners': [OWNER]}))
            try:
                with self.assertRaises(ValueError): await adapter.connect()
                self.assertIsNone(adapter.ipc)
            finally:
                reset_secret_scope(token)
        token = set_secret_scope(None)
        set_multiplex_active(True)
        try:
            # Profile policy rejects multiplex before even attempting a secret lookup.
            with self.assertRaises(RuntimeError): await adapter.connect()
        finally:
            set_multiplex_active(False); reset_secret_scope(token)

    async def test_registration_failure_cannot_create_platform(self):
        class Refused:
            def register_hook(self, *args): return None
            def on_unload(self, callback): pass
        refused = install(Refused())
        with self.assertRaises(RuntimeError): refused.activate()
        unbound = PocketAdapter(self.config)
        with self.assertRaises(RuntimeError): await unbound.connect()

    async def test_inactive_hook_handle_stops_admission(self):
        policy = self.adapter.policy
        handle = policy.handle
        with patch.object(handle, '_disposed', True):
            result = await self.client.request(event('disposed hook'))
        self.assertEqual(result['state'], 'Unavailable')
        self.assertEqual(self.seen, [])

    async def test_busy_and_unknown_no_automatic_replay(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def held(message):
            self.seen.append(message); started.set(); await release.wait()
            return 'synthetic held response'
        self.adapter.set_message_handler(held)
        first = asyncio.create_task(self.client.request(event('held request')))
        await asyncio.wait_for(started.wait(), 10)
        try:
            self.assertEqual((await self.client.request(event('concurrent request')))['state'], 'Busy')
        finally:
            release.set()
        self.assertEqual((await first)['state'], 'ResponseReady')
        self.assertEqual(len(self.seen), 1)

    async def test_bridge_provisioning_rejects_paths_and_encoded_strings(self):
        for bad in ['a'*64, self.root/'key', b'a'*31, bytearray(32)]:
            with self.assertRaises(ValueError): BridgeEventQuery(self.root, bad, [OWNER])

    async def test_hook_exception_fails_closed_real_dispatch(self):
        from hermes_cli.plugins import PluginContext
        from model_tools import handle_function_call
        def broken(**kwargs): raise RuntimeError('synthetic policy fault')
        context = PluginContext(base.manifest, base.manager)
        policy = self.adapter.policy
        policy.handle.dispose()
        handle = context.register_hook('pre_tool_call', broken)
        try:
            result = json.loads(handle_function_call('terminal', {}, task_id='fixture'))
            self.assertIn('error', result)
            self.assertNotEqual(result['error'], BLOCK_MESSAGE)
        finally:
            handle.dispose()
            policy.handle = context.register_hook('pre_tool_call', policy.block)

    async def test_context_engine_schema_injection_obeys_empty_native_selection(self):
        from agent.agent_init import _inject_context_engine_tools
        from types import SimpleNamespace
        calls = []
        class Engine:
            def get_tool_schemas(self):
                calls.append('schemas')
                return [{'name': 'injected_context_attack', 'description': 'fixture',
                         'parameters': {'type': 'object', 'properties': {}}}]
            def on_session_start(self, *args, **kwargs): pass
        agent = SimpleNamespace(context_compressor=Engine(), tools=[], enabled_toolsets=[],
                                valid_tool_names=set(), session_id='fixture', platform='pocket', model='synthetic')
        _inject_context_engine_tools(agent)
        self.assertEqual(agent.tools, [])
        self.assertEqual(calls, [])
        # Positive control proves the same native injector is active, not a stub.
        agent.enabled_toolsets = ['context_engine']
        _inject_context_engine_tools(agent)
        self.assertEqual(agent.tools[0]['function']['name'], 'injected_context_attack')
        from model_tools import handle_function_call
        self.assertEqual(json.loads(handle_function_call('injected_context_attack', {})), {'error': BLOCK_MESSAGE})

    async def test_secret_helper_scopes_restore_and_unscoped_fails_closed(self):
        from agent.secret_scope import get_secret, UnscopedSecretError
        first = set_secret_scope({'POCKET_HMAC_KEY': 'a' * 64})
        try:
            self.assertTrue(get_secret('POCKET_HMAC_KEY') == 'a' * 64)
            second = set_secret_scope({'POCKET_HMAC_KEY': 'b' * 64})
            try: self.assertTrue(get_secret('POCKET_HMAC_KEY') == 'b' * 64)
            finally: reset_secret_scope(second)
            self.assertTrue(get_secret('POCKET_HMAC_KEY') == 'a' * 64)
            empty = set_secret_scope(None); set_multiplex_active(True)
            try:
                with self.assertRaises(UnscopedSecretError): get_secret('POCKET_HMAC_KEY')
            finally:
                set_multiplex_active(False); reset_secret_scope(empty)
        finally: reset_secret_scope(first)
