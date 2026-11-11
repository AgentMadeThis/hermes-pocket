"""Native backend vertical-slice fixtures; NEVER model/RF/audible-output proof."""
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import unittest
import native_cases as base
from plugin.client import BridgeEventQuery
from plugin.location import SQLiteSnapshotReader
from plugin.music import MusicHTTP
from plugin.wire import PocketOutcome
from integration.host.bridge.core import Bridge, Inbound, QueryUnavailable, chunk_text
from integration.host.bridge.tracking import SCHEMA
from test_contract import OWNER, event

class Backend(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.NativeIntegration.asyncSetUp
    asyncTearDown = base.NativeIntegration.asyncTearDown

    async def home(self, responses):
        self.http_calls = []
        async def fixture(reader, writer):
            try:
                line = (await reader.readuntil(b'\r\n\r\n')).split(b'\r\n')[0].decode()
                self.http_calls.append(line)
                response = responses.pop(0)
                body = json.dumps(response).encode()
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
                await writer.drain()
            finally:
                writer.close(); await writer.wait_closed()
        server = await asyncio.start_server(fixture, '127.0.0.1', 0)
        self.addAsyncCleanup(self.close_server, server)
        self.adapter.home_music = True
        self.adapter.music = MusicHTTP(port=server.sockets[0].getsockname()[1], source='lounge', timeout=.5)
        return server

    async def close_server(self, server):
        server.close(); await server.wait_closed()

    async def test_actual_bridge_home_start_refresh_volume_and_fake_rf(self):
        stopped = {'ok': True, 'running': False, 'paused': False, 'volume': 20}
        playing = {'ok': True, 'running': True, 'paused': False, 'volume': 35}
        await self.home([stopped, {'ok': True}, playing, playing, {'ok': True}, playing])
        query = BridgeEventQuery(self.root / 'state', self.key, [OWNER], unavailable_error=QueryUnavailable)
        bridge = Bridge(self.root / 'homebridge', [OWNER], query, payload_budget=91)
        sent = []
        async def fake_rf(owner, text): sent.append((owner, text))
        try:
            for cid, action in [('00000011', 'p|'), ('00000012', 'q|'), ('00000013', 'v|35')]:
                msg = Inbound(OWNER, event()['timestamp'], f'P1|{cid}|{action}')
                self.assertEqual(bridge.admit(msg)[0], 'Queued')
                await bridge.process_one()
                answer = bridge.response(msg.identity())
                self.assertEqual(answer, f'R1|{cid}|m|1,0,35')
                self.assertEqual(chunk_text(answer, 91), [answer])
                self.assertEqual(await bridge.deliver(msg.identity(), fake_rf), 'Submitted')
            self.assertEqual(len(sent), 3)
            self.assertEqual(self.http_calls, ['GET /api/status HTTP/1.1', 'POST /api/play/lounge HTTP/1.1',
                'GET /api/status HTTP/1.1', 'GET /api/status HTTP/1.1', 'POST /api/vol/35 HTTP/1.1', 'GET /api/status HTTP/1.1'])
            self.assertEqual(self.seen, [])
        finally: bridge.close()

    async def test_stopped_without_fixed_source_is_precise_unavailable(self):
        await self.home([{'ok': True, 'running': False, 'paused': False}])
        self.adapter.music.source = None
        response = await self.client.request(event('P1|00000014|p|'))
        self.assertEqual(response['state'], 'Unavailable')
        self.assertEqual(response['text'], 'R1|00000014|e|HOME_FIXED_SOURCE_NOT_CONFIGURED')
        self.assertEqual(self.http_calls, ['GET /api/status HTTP/1.1'])

    async def test_resume_running_paused_and_observed_unknown_volume(self):
        await self.home([{'ok': True, 'running': True, 'paused': True}, {'ok': True},
                         {'ok': True, 'running': True, 'paused': False}])
        result = await self.client.request(event('P1|00000015|p|'))
        self.assertEqual(result['text'], 'R1|00000015|m|1,0,?')
        self.assertEqual(self.http_calls, ['GET /api/status HTTP/1.1', 'POST /api/resume HTTP/1.1', 'GET /api/status HTTP/1.1'])

    async def test_mutation_accepted_failed_status_unknown_never_repeat(self):
        await self.home([{'ok': True}, {'ok': False}])
        result = await self.client.request(event('P1|00000016|x|'))
        self.assertEqual(result['state'], 'Unknown')
        self.assertTrue(result['text'].startswith('R1|00000016|u|'))
        self.assertEqual(self.http_calls, ['POST /api/next HTTP/1.1', 'GET /api/status HTTP/1.1'])

    async def test_unsupported_volume_no_io(self):
        await self.home([])
        self.adapter.music.volume_supported = False
        result = await self.client.request(event('P1|00000017|v|30'))
        self.assertEqual(result['state'], 'Unavailable')
        self.assertEqual(result['text'], 'R1|00000017|e|HOME_VOLUME_UNSUPPORTED')
        self.assertEqual(self.http_calls, [])

    async def test_owner_snapshot_location_route_and_l_not_llm(self):
        path = self.root / 'fixture-snapshot.sqlite'
        with closing(sqlite3.connect(path)) as db:
            db.executescript(SCHEMA)
            db.execute('INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (OWNER, '00000001', 1, 'b' * 64, 1000, 999, 2000, 0, 0, 10, 7, 1))
            db.commit()
        self.adapter.location = SQLiteSnapshotReader(path, now=lambda: 1005)
        for text in ['/location', 'P1|00000018|l|']:
            result = await self.client.request(event(text))
            self.assertEqual(result['state'], 'Observed')
            self.assertIn('0.000000,0.000000', result['text'])
            self.assertIn('rx 5s ago; fix age 7s', result['text'])
        self.adapter.location = SQLiteSnapshotReader(path, now=lambda: 1100)
        result = await self.client.request(event('P1|00000019|l|'))
        self.assertEqual(result['state'], 'Stale')
        self.assertTrue(result['text'].startswith('R1|00000019|o|Stale GPS'))
        self.assertEqual(self.seen, [])

    async def test_bridge_running_restart_and_expiry_correlated(self):
        state = self.root / 'restartbridge'
        bridge = Bridge(state, [OWNER], payload_budget=91)
        msg = Inbound(OWNER, event()['timestamp'], 'P1|00000020|c|hi')
        self.assertEqual(bridge.admit(msg)[0], 'Queued')
        bridge.db.execute("update requests set state='Running'")
        bridge.close()
        bridge = Bridge(state, [OWNER], payload_budget=91)
        try:
            self.assertTrue(bridge.response(msg.identity()).startswith('R1|00000020|u|'))
            self.assertIsNone(await bridge.process_one())
        finally: bridge.close()
        bridge = Bridge(self.root / 'expiredbridge', [OWNER], payload_budget=91)
        try:
            self.assertEqual(bridge.admit(msg)[0], 'Queued')
            bridge.now = lambda: msg.timestamp + 400
            await bridge.process_one()
            self.assertTrue(bridge.response(msg.identity()).startswith('R1|00000020|e|'))
        finally: bridge.close()

    async def test_bridge_unknown_and_unavailable_states_preserved(self):
        query = BridgeEventQuery(self.root / 'state', self.key, [OWNER], unavailable_error=QueryUnavailable)
        bridge = Bridge(self.root / 'unknownbridge', [OWNER], query, payload_budget=91)
        try:
            msg = Inbound(OWNER, event()['timestamp'], 'P1|00000021|q|')
            bridge.admit(msg); await bridge.process_one()
            row = bridge.db.execute('select state,response from requests').fetchone()
            self.assertEqual(row['state'], 'Unavailable')
            self.assertTrue(row['response'].startswith('R1|00000021|e|'))
            self.adapter.timeout = .02
            async def delayed(message): await asyncio.sleep(.1)
            self.adapter.set_message_handler(delayed)
            msg = Inbound(OWNER, event()['timestamp'], 'P1|00000022|c|hi')
            bridge.admit(msg); await bridge.process_one()
            row = bridge.db.execute('select state,response from requests where id=?', (msg.identity(),)).fetchone()
            self.assertEqual(row['state'], 'Unknown')
            self.assertTrue(row['response'].startswith('R1|00000022|u|'))
            await asyncio.gather(*list(self.adapter._session_tasks.values()), return_exceptions=True)
        finally: bridge.close()
