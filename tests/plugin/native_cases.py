"""Real installed Hermes APIs + real IPC, with labelled non-model callbacks."""
import asyncio
import os
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import patch
from plugin import register
from plugin.adapter import PocketAdapter
from plugin.client import BridgeEventQuery
from plugin.ipc import IPCClient
from plugin.protocol import identity
from test_contract import event, OWNER
from gateway.config import PlatformConfig, Platform
from gateway.platform_registry import platform_registry
from gateway.platforms.base import BasePlatformAdapter
from gateway.run_turn import GatewayTurnMixin
from hermes_cli.plugins import get_plugin_manager, discover_plugins, parse_manifest_file
from agent.secret_scope import set_secret_scope, reset_secret_scope

ROOT = Path(__file__).resolve().parents[2]
manifest = parse_manifest_file(ROOT / 'plugin/plugin.yaml', ROOT / 'plugin', 'user', '')
discover_plugins()
manager = get_plugin_manager()
assert manager._plugins[manifest.name].enabled, repr(manager._plugins[manifest.name])

def make_adapter(config):
    return platform_registry.create_adapter('pocket', config)

class NativeIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.key = secrets.token_bytes(32)
        self.secret_token = set_secret_scope({'POCKET_HMAC_KEY': self.key.hex()})
        self.config = PlatformConfig(enabled=True, typing_indicator=False, gateway_restart_notification=False,
                                     extra={'socket_dir': str(self.root / 'state'),
                                            'owners': [OWNER], 'timeout': 10})
        self.adapter = make_adapter(self.config)
        self.assertTrue(await self.adapter.connect())
        self.client = IPCClient(self.root / 'state', self.key, timeout=20)
        self.seen = []
        async def labelled_native_callback(message):
            self.seen.append(message)
            result = await self.adapter.send(message.source.chat_id, 'NATIVE LOOPBACK FIXTURE — Я😀',
                                             reply_to=message.message_id)
            self.assertTrue(result.success)
            self.assertEqual(result.raw_response['radio_delivery'], 'NotAttempted')
            return None
        self.adapter.set_message_handler(labelled_native_callback)

    async def asyncTearDown(self):
        # Native background callbacks are fixture-owned; close them without model/RF.
        tasks = list(self.adapter._session_tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.adapter.disconnect()
        reset_secret_scope(self.secret_token)
        self.tmp.cleanup()

    async def test_native_session_and_roundtrip_full_identity(self):
        original = event()
        response = await self.client.request(original)
        self.assertEqual(response['state'], 'ResponseReady')
        self.assertTrue(response['text'].startswith('NATIVE LOOPBACK FIXTURE'))
        message = self.seen[0]
        self.assertIsInstance(self.adapter, BasePlatformAdapter)
        self.assertEqual(message.source.platform, Platform('pocket'))
        self.assertEqual(message.source.user_id, OWNER)
        self.assertEqual(message.source.chat_id, OWNER)
        self.assertFalse(message.allow_gateway_control)
        self.assertFalse(message.internal)
        self.assertEqual(message.message_id, original['request_id'])
        self.assertEqual(message.timestamp.timestamp(), original['timestamp'])

    async def test_native_final_text_pipeline(self):
        async def normal_handler(message):
            self.seen.append(message)
            return 'NATIVE FINAL-TEXT LOOPBACK FIXTURE'
        self.adapter.set_message_handler(normal_handler)
        result = await self.client.request(event('normal final fixture'))
        self.assertEqual(result['state'], 'ResponseReady')
        self.assertEqual(result['text'], 'NATIVE FINAL-TEXT LOOPBACK FIXTURE')

    async def test_synthetic_restart_resume_refused(self):
        from gateway.platforms.event import MessageEvent
        source = self.adapter.build_source(OWNER, user_id=OWNER)
        await self.adapter.handle_message(MessageEvent(text='', source=source, internal=True))
        await self.adapter.handle_message(MessageEvent(text='replay', source=source, message_id='old'))
        self.assertEqual(self.seen, [])
        self.assertFalse(self.adapter._session_tasks)

    async def test_native_tool_resolution_excludes_shell_and_mcp(self):
        source = self.adapter.build_source(OWNER, user_id=OWNER)
        class Resolver:
            def _delivery_adapter_for(inner, unused): return self.adapter
        result = GatewayTurnMixin._resolve_enabled_toolsets_for_source(
            Resolver(), {'platform_toolsets': {'pocket': ['hermes-cli']},
                         'mcp_servers': {'fixture': {'command': 'MUST_NOT_RUN'}}}, source, 'pocket')
        self.assertEqual(result, [])
        self.assertTrue(self.adapter._native_policy_ok())

    async def test_unauthorized_and_duplicate_no_native_redispatch(self):
        self.assertEqual((await self.client.request(event(sender='b2' * 32)))['state'], 'Rejected')
        e = event()
        self.assertEqual((await self.client.request(e))['state'], 'ResponseReady')
        self.assertEqual((await self.client.request(e))['state'], 'Duplicate')
        self.assertEqual(len(self.seen), 1)

    async def test_timeout_restart_late_response_not_replayed(self):
        self.adapter.timeout = .03
        started = asyncio.Event()
        release = asyncio.Event()
        async def delayed(message):
            self.seen.append(message); started.set()
            await release.wait()
            result = await self.adapter.send(OWNER, 'late', reply_to=message.message_id)
            self.assertFalse(result.success)
        self.adapter.set_message_handler(delayed)
        e = event()
        response = await self.client.request(e)
        self.assertTrue(started.is_set())
        self.assertEqual(response['state'], 'Unknown')
        release.set()
        await asyncio.gather(*list(self.adapter._session_tasks.values()), return_exceptions=True)
        await self.adapter.disconnect()
        self.adapter = make_adapter(self.config)
        await self.adapter.connect()
        self.assertEqual((await self.client.request(e))['prior_state'], 'Unknown')
        self.assertEqual(len(self.seen), 1)

    async def test_semantics_no_model_or_unrestricted_commands(self):
        for text, state, code in [('/status', 'ResponseReady', None), ('/note fixture', 'ResponseReady', None),
                                  ('/location', 'Unavailable', 'LOCATION_SNAPSHOT_UNAVAILABLE'),
                                  ('/music HOME pause', 'Unavailable', 'HOME_MUSIC_NOT_ENABLED'),
                                  ('/music POCKET pause', 'Rejected', 'HOME_ALLOWLIST_ONLY'),
                                  ('/terminal id', 'Rejected', 'COMMAND_NOT_ALLOWED')]:
            result = await self.client.request(event(text))
            self.assertEqual(result['state'], state)
            if code: self.assertEqual(result['code'], code)
        self.assertEqual(self.seen, [])

    async def test_routed_policy_fails_closed_before_dispatch(self):
        with patch(self.adapter.policy.__class__.__module__ + '.load_config_readonly',
                   return_value={'gateway': {'profile_routes': {'fixture': 'other'}}}):
            result = await self.client.request(event('policy fixture'))
        self.assertEqual(result['state'], 'Unavailable')
        self.assertEqual(result['code'], 'NATIVE_SESSION_POLICY_NOT_READY')
        self.assertEqual(self.seen, [])

    async def test_utf8_outbound_native_budget(self):
        async def long_fixture(message):
            result = await self.adapter.send(OWNER, 'Я😀' * 300, reply_to=message.message_id)
            self.assertTrue(result.success)
        self.adapter.set_message_handler(long_fixture)
        result = await self.client.request(event('UTF8 fixture'))
        self.assertEqual(result['state'], 'ResponseReady')
        self.assertLessEqual(len(result['text'].encode()), 4 * (91 - 6))
        self.assertTrue(result['text'].endswith('…'))

    async def test_no_unbound_or_mismatched_send(self):
        for owner, reply in [(OWNER, None), ('b2' * 32, 'x'), (OWNER, 'x')]:
            self.assertFalse((await self.adapter.send(owner, 'no', reply_to=reply)).success)

    async def test_bridge_client_query_event_path(self):
        class FixtureInbound:
            sender = OWNER; timestamp = event()['timestamp']; text = 'bridge fixture'
            private = True; text_type = 0
            def identity(inner):
                return identity({'sender': inner.sender, 'timestamp': inner.timestamp,
                                 'text_type': inner.text_type, 'text': inner.text})
        client = BridgeEventQuery(self.root / 'state', self.key, [OWNER], timeout=20)
        self.assertTrue((await client.query_event(FixtureInbound(), 91)).startswith('NATIVE LOOPBACK FIXTURE'))
        with self.assertRaises(RuntimeError): await client.query_event(FixtureInbound(), 91)

    async def test_bridge_client_preserves_typed_unavailability(self):
        class LocationFixture:
            sender = OWNER; timestamp = event()['timestamp']; text = '/location'
            private = True; text_type = 0
            def identity(inner):
                return identity({'sender': inner.sender, 'timestamp': inner.timestamp,
                                 'text_type': inner.text_type, 'text': inner.text})
        class FixtureUnavailable(RuntimeError):
            pass
        client = BridgeEventQuery(self.root / 'state', self.key, [OWNER],
                                  unavailable_error=FixtureUnavailable, timeout=20)
        with self.assertRaises(FixtureUnavailable) as caught:
            await client.query_event(LocationFixture(), 91)
        self.assertEqual(caught.exception.pocket_code, 'LOCATION_SNAPSHOT_UNAVAILABLE')
        self.assertEqual(self.seen, [])

    async def test_music_home_fixture_through_plugin(self):
        from plugin.music import MusicHTTP
        calls = []
        async def home_fixture(reader, writer):
            calls.append((await reader.readuntil(b'\r\n\r\n')).split(b'\r\n')[0])
            body = b'{"ok":true,"running":true,"paused":true,"volume":35}'
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
            await writer.drain(); writer.close(); await writer.wait_closed()
        server = await asyncio.start_server(home_fixture, '127.0.0.1', 0)
        try:
            self.adapter.home_music = True
            self.adapter.music = MusicHTTP(port=server.sockets[0].getsockname()[1])
            result = await self.client.request(event('/music HOME pause'))
            self.assertEqual(result['state'], 'Observed')
            self.assertIn('no audible-output proof', result['text'])
            self.assertEqual(calls, [b'POST /api/pause HTTP/1.1', b'GET /api/status HTTP/1.1'])
            self.assertEqual(self.seen, [])
        finally:
            server.close(); await server.wait_closed()

class Metadata(unittest.TestCase):
    def test_real_manifest_and_registration(self):
        self.assertEqual(manifest.name, 'pocket-platform')
        self.assertEqual(manifest.kind, 'platform')
        self.assertEqual(manager._platform_name_from_manifest(manifest), 'pocket')
        self.assertEqual(manifest.version, '0.2.0')
        self.assertIn('pocket', manager._plugin_platform_names)
        self.assertTrue(platform_registry.is_registered('pocket'))
        self.assertEqual(Platform('pocket').value, 'pocket')
        self.assertEqual(PocketAdapter.__abstractmethods__, frozenset())

if __name__ == '__main__': unittest.main()


class IntegratedNative(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = NativeIntegration.asyncSetUp
    asyncTearDown = NativeIntegration.asyncTearDown
    """Candidate Bridge -> actual IPC -> native adapter, fake RF/model ONLY."""
    async def test_p1_full_outer_identity_inner_native_and_fake_rf(self):
        from integration.host.bridge.core import Bridge, Inbound, chunk_text, QueryUnavailable
        e = event('P1|00000001|c|Привет 😀')
        query = BridgeEventQuery(self.root / 'state', self.key, [OWNER], unavailable_error=QueryUnavailable)
        bridge = Bridge(self.root / 'bridge', [OWNER], query, payload_budget=91)
        try:
            msg = Inbound(OWNER, e['timestamp'], e['text'])
            self.assertEqual(bridge.admit(msg), ('Queued', e['request_id']))
            await bridge.process_one()
            result = bridge.response(msg.identity())
            self.assertTrue(result.startswith('R1|00000001|o|'), result)
            self.assertEqual(self.seen[0].text, 'Привет 😀')
            self.assertEqual(self.seen[0].message_id, msg.identity())
            self.assertEqual(self.adapter.journal.state(msg.identity()), 'ResponseReady')
            row = bridge.db.execute('select * from requests').fetchone()
            self.assertEqual(row['text'], e['text'])
            self.assertEqual(row['sender'], OWNER)
            self.assertEqual(row['timestamp'], e['timestamp'])
            self.assertEqual(chunk_text(result, 91), [result])
            class FakeRFDriver:
                def __init__(inner): inner.submitted = []
                async def send(inner, owner, body): inner.submitted.append((owner, body))
            driver = FakeRFDriver()
            self.assertEqual(await bridge.deliver(msg.identity(), driver.send), 'Submitted')
            self.assertEqual(driver.submitted, [(OWNER, result)])
            self.assertEqual(bridge.admit(msg)[0], 'Done')
            self.assertIsNone(await bridge.process_one())
            self.assertEqual(len(self.seen), 1)
            self.assertEqual(await bridge.deliver(msg.identity(), driver.send), 'Submitted')
            self.assertEqual(len(driver.submitted), 1)
        finally: bridge.close()

    async def test_p1_note_malformed_q_l_no_model(self):
        for text, prefix in [('P1|00000001|n|Заметка 😀','R1|00000001|o|'),
                              ('P1|00000002|p|url','R1|00000002|e|'),
                              ('P1|00000003|q|','R1|00000003|e|'),
                              ('P1|00000004|l|','R1|00000004|e|')]:
            result = await self.client.request(event(text))
            self.assertTrue(result['text'].startswith(prefix), result)
        rows = self.adapter.journal.db.execute('select * from notes').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], 'Заметка 😀')
        self.assertEqual(self.seen, [])

    async def test_p1_utf8_reserved_reply_budget(self):
        async def long(message): return 'Я😀' * 500
        self.adapter.set_message_handler(long)
        result = await self.client.request(event('P1|00000005|c|hi'))
        self.assertLessEqual(len(result['text'].encode()), 91)
        self.assertTrue(result['text'].endswith('…'))
        self.assertTrue(result['text'].startswith('R1|00000005|o|'))

    async def test_p1_control_output_callback_actual_r1(self):
        import json
        source = 'Я' * 35 + '\t\r\n\r😀\x01\x7f'
        expected = 'Я' * 35 + ' \n\n😀'
        async def control_fixture(message):
            self.seen.append(message)
            sent = await self.adapter.send(OWNER, source, reply_to=message.message_id)
            self.assertTrue(sent.success)
        self.adapter.set_message_handler(control_fixture)
        original = event('P1|00000007|c|control fixture')
        result = await self.client.request(original)
        # Source/expected are synthetic callback data, NOT live model evidence.
        artifact = {'fixture_only': True, 'source_text': source, 'expected_text': expected,
                    'expected_wire': 'R1|00000007|o|' + expected,
                    'actual_response': result, 'original_event': original}
        (ROOT / 'tests/integration/backend/control-output-r1.json').write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2) + '\n')
        self.assertEqual(result['state'], 'ResponseReady')
        self.assertEqual(result['text'], artifact['expected_wire'])
        self.assertEqual(len(result['text'].encode()), 91)
        self.assertEqual(result['request_id'], original['request_id'])
        self.assertEqual(self.seen[0].message_id, original['request_id'])
        self.assertEqual(self.adapter.journal.state(original['request_id']), 'ResponseReady')

    async def test_p1_invalid_output_fails_closed_correlated(self):
        for index, bad in enumerate(('\x00', '\ufffd', '\ud800')):
            with self.subTest(bad=repr(bad)):
                self.adapter.timeout = .02
                async def invalid_fixture(message):
                    sent = await self.adapter.send(OWNER, 'answer' + bad, reply_to=message.message_id)
                    self.assertFalse(sent.success)
                self.adapter.set_message_handler(invalid_fixture)
                original = event(f'P1|{index + 16:08x}|c|bad output fixture')
                result = await self.client.request(original)
                self.assertEqual(result['state'], 'Unknown')
                self.assertTrue(result['text'].startswith(f'R1|{index + 16:08x}|u|'))
                self.assertEqual(result['request_id'], original['request_id'])

    async def test_p1_timeout_correlated_unknown_and_restart(self):
        self.adapter.timeout = .02
        async def delayed(message): await asyncio.sleep(.1)
        self.adapter.set_message_handler(delayed)
        e = event('P1|00000006|c|hi')
        result = await self.client.request(e)
        self.assertEqual(result['state'], 'Unknown')
        self.assertTrue(result['text'].startswith('R1|00000006|u|'))
        await asyncio.gather(*list(self.adapter._session_tasks.values()), return_exceptions=True)
        await self.adapter.disconnect()
        self.adapter = make_adapter(self.config); await self.adapter.connect()
        self.assertEqual((await self.client.request(e))['prior_state'], 'Unknown')

    async def test_p1_foreign_full_owner_before_decode(self):
        result = await self.client.request(event('P1|00000001|c|hello', sender='a1' * 6 + 'b2' * 26))
        self.assertEqual(result['state'], 'Rejected')
        self.assertNotIn('text', result)
        e = event('P1|00000001|c|hello'); e['request_id'] = identity(event('hello'))
        self.assertEqual((await self.client.request(e))['state'], 'Rejected')
        self.assertEqual(self.seen, [])

    async def test_pos1_ahead_of_queue_no_ipc(self):
        from integration.host.bridge.core import Bridge, Inbound
        bridge = Bridge(self.root / 'gpsbridge', [OWNER], BridgeEventQuery(self.root / 'state', self.key, [OWNER]), payload_budget=91)
        try:
            result = bridge.admit(Inbound(OWNER, event()['timestamp'], '/pos1 00000001 1 12345678 23456789 10 7 2000'))
            self.assertEqual(result, ('TelemetryStored', None))
            self.assertIsNone(await bridge.process_one())
            self.assertEqual(bridge.db.execute('select count(*) from requests').fetchone()[0], 0)
            self.assertEqual(self.adapter.journal.db.execute('select count(*) from events').fetchone()[0], 0)
            self.assertEqual(self.seen, [])
        finally: bridge.close()
