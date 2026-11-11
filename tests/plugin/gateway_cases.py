"""Normal installed gateway startup/turn/shutdown, synthetic provider transport.

No adapter factory/binding/handler/agent loop is replaced. Only HTTP transport is
synthetic. This is NOT physical radio or real model quality proof.
"""
import asyncio
import json
import os
import logging
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
import httpx
import native_cases as base
from test_contract import event, OWNER
from agent.secret_scope import set_secret_scope, reset_secret_scope
from gateway.config import Platform, PlatformConfig, GatewayConfig
from plugin.ipc import IPCClient


class NormalGateway(unittest.IsolatedAsyncioTestCase):
    async def test_normal_discovery_start_bind_agent_turn_reply_shutdown(self):
        from gateway.run import GatewayRunner
        diagnostics = logging.StreamHandler(sys.stderr)
        diagnostics.setLevel(logging.DEBUG)
        root_logger = logging.getLogger()
        old_level = root_logger.level
        root_logger.setLevel(logging.DEBUG)
        root_logger.addHandler(diagnostics)
        from hermes_cli.config import load_config_readonly
        home = Path(os.environ['HERMES_HOME'])
        cfg_path = home/'config.yaml'
        original = cfg_path.read_text()
        cfg = load_config_readonly()
        cfg['model'] = {'default': 'synthetic-pocket-fixture', 'provider': 'custom',
                        'base_url': 'http://127.0.0.1:9/v1', 'api_key': 'synthetic-fixture-not-a-credential',
                        'context_length': 131072}
        cfg['memory'] = {'memory_enabled': False, 'user_profile_enabled': False}
        cfg['compression'] = {'enabled': False}
        cfg['kanban'] = {'dispatch_in_gateway': False, 'notify_in_gateway': False}
        cfg['agent'] = {'max_turns': 3, 'prefill_messages': False}
        cfg['platform_toolsets'] = {'pocket': ['terminal']}
        cfg['gateway']['turn_execution'] = 'in_process'
        cfg_path.write_text(json.dumps(cfg))
        key = bytes(range(32))
        token = set_secret_scope({'POCKET_HMAC_KEY': key.hex(), 'POCKET_ALLOWED_USERS': OWNER})
        os.environ['POCKET_ALLOWED_USERS'] = OWNER
        calls = []
        def synthetic_send(client, request, **kwargs):
            if request.url.path.endswith('/chat/completions'):
                payload = json.loads(request.content)
                calls.append(payload)
                response = {'id': 'synthetic-pocket-completion', 'object': 'chat.completion',
                            'created': 1, 'model': 'synthetic-pocket-fixture',
                            'choices': [{'index': 0, 'message': {'role': 'assistant',
                                         'content': 'SYNTHETIC PROVIDER gateway reply'}, 'finish_reason': 'stop'}],
                            'usage': {'prompt_tokens': 20, 'completion_tokens': 8, 'total_tokens': 28}}
                if payload.get('stream'):
                    chunks = [ {'id': response['id'], 'object': 'chat.completion.chunk', 'created': 1,
                                'model': response['model'], 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': response['choices'][0]['message']['content']}, 'finish_reason': None}]},
                               {'id': response['id'], 'object': 'chat.completion.chunk', 'created': 1,
                                'model': response['model'], 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}], 'usage': response['usage']} ]
                    body = ''.join('data: '+json.dumps(c)+'\n\n' for c in chunks)+'data: [DONE]\n\n'
                    return httpx.Response(200, request=request, content=body.encode(), headers={'Content-Type': 'text/event-stream'})
                return httpx.Response(200, request=request, json=response)
            if request.url.path.endswith('/models'):
                return httpx.Response(200, request=request, json={'data': [{'id': 'synthetic-pocket-fixture', 'context_length': 131072}]})
            raise AssertionError('unexpected HTTP request in synthetic provider fixture')
        platform_config = PlatformConfig(enabled=True, typing_indicator=False, gateway_restart_notification=False,
                                         extra={'socket_dir': str(home/'gateway-state'), 'owners': [OWNER], 'timeout': 60})
        gateway_config = GatewayConfig(platforms={Platform('pocket'): platform_config}, multiplex_profiles=False)
        runner = None
        try:
            with patch.object(httpx.Client, 'send', synthetic_send):
                runner = GatewayRunner(gateway_config)
                self.assertTrue(await asyncio.wait_for(runner.start(), 90))
                self.assertIn(Platform('pocket'), runner.adapters)
                adapter = runner.adapters[Platform('pocket')]
                self.assertIsNotNone(adapter._message_handler)
                self.assertTrue(adapter._native_policy_ok())
                client = IPCClient(home/'gateway-state', key, timeout=80)
                request = event('synthetic normal gateway fixture')
                result = await client.request(request)
                self.assertEqual(result['state'], 'ResponseReady', result)
                self.assertIn('SYNTHETIC PROVIDER gateway reply', result['text'])
                self.assertTrue(calls, 'normal gateway did not call synthetic provider transport')
                self.assertTrue(all(not payload.get('tools') for payload in calls), 'model schema was not empty')
                self.assertTrue(all(payload['model'] == 'synthetic-pocket-fixture' for payload in calls))
                self.assertEqual((await client.request(request))['state'], 'Duplicate')
                await asyncio.wait_for(runner.stop(), 90)
                self.assertFalse(adapter._running)
                self.assertFalse((home/'gateway-state/pocket.sock').exists())
                runner = None
        finally:
            if runner is not None:
                await asyncio.wait_for(runner.stop(), 90)
            reset_secret_scope(token)
            os.environ.pop('POCKET_ALLOWED_USERS', None)
            cfg_path.write_text(original)
            root_logger.removeHandler(diagnostics)
            root_logger.setLevel(old_level)
