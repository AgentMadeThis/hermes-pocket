"""Real local sockets; callbacks are explicitly labelled fixture behavior."""
import asyncio
import os
from pathlib import Path
import secrets
import socket
import tempfile
import unittest
from unittest.mock import patch
from plugin.ipc import IPCClient, IPCServer, read_frame, write_frame, tag
from plugin.protocol import Journal
from plugin.music import MusicHTTP

class IPCIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.key = secrets.token_bytes(32)
        self.journal = Journal(self.directory)
        self.calls = []
        async def fixture_callback(payload):
            self.calls.append(payload)
            return {'state': 'ResponseReady', 'text': 'LOOPBACK FIXTURE ONLY'}
        self.server = IPCServer(self.directory, self.key, fixture_callback)
        await self.server.start()
        self.client = IPCClient(self.directory, self.key, timeout=1)

    async def asyncTearDown(self):
        await self.server.close()
        self.journal.close()
        self.tmp.cleanup()

    async def test_mutual_auth_loopback(self):
        result = await self.client.request({'fixture': True})
        self.assertEqual(result['text'], 'LOOPBACK FIXTURE ONLY')
        self.assertEqual(self.calls, [{'fixture': True}])

    async def test_wrong_secret_no_dispatch(self):
        with self.assertRaises(ValueError):
            await IPCClient(self.directory, secrets.token_bytes(32), timeout=1).request({})
        self.assertEqual(self.calls, [])

    async def test_wrong_peer_uid_no_dispatch(self):
        with patch('plugin.ipc.peer_uid', return_value=os.getuid() + 1):
            with self.assertRaises(ValueError): await self.client.request({})
        self.assertEqual(self.calls, [])

    async def test_cross_connection_replay_rejected(self):
        reader, writer = await asyncio.open_unix_connection(str(self.server.path))
        old = await read_frame(reader)
        frame = {'payload': {}, 'mac': tag(self.key, old['challenge'], 'request', {})}
        writer.close(); await writer.wait_closed()
        reader, writer = await asyncio.open_unix_connection(str(self.server.path))
        await read_frame(reader)
        await write_frame(writer, frame)
        self.assertEqual(await reader.read(), b'')
        writer.close(); await writer.wait_closed()
        self.assertEqual(self.calls, [])

    async def test_oversize_frame_rejected(self):
        with self.assertRaises(ValueError): await self.client.request({'x': 'x' * 5000})
        self.assertEqual(self.calls, [])

    async def test_server_timeout_no_replay(self):
        async def slow(payload):
            self.calls.append(payload)
            await asyncio.sleep(5)
        self.server.callback = slow
        self.server.timeout = .02
        with self.assertRaises(ValueError): await self.client.request({'fixture': 'timeout'})
        self.assertEqual(len(self.calls), 1)

    async def test_unsafe_socket_mode_and_directory(self):
        self.server.path.chmod(0o666)
        with self.assertRaises(ValueError): await self.client.request({})
        self.directory.chmod(0o755)
        with self.assertRaises(ValueError): await self.client.request({})
        self.directory.chmod(0o700)
        self.assertEqual(self.calls, [])

class MusicLoopback(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_service_contract_and_home_only(self):
        calls = []
        async def fixture(reader, writer):
            head = await reader.readuntil(b'\r\n\r\n')
            calls.append(head.split(b'\r\n')[0])
            body = b'{"ok":true,"running":true,"paused":false}'
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
            await writer.drain(); writer.close(); await writer.wait_closed()
        server = await asyncio.start_server(fixture, '127.0.0.1', 0)
        try:
            music = MusicHTTP(port=server.sockets[0].getsockname()[1])
            self.assertEqual((await music.perform('HOME', 'status'))['state'], 'Observed')
            self.assertEqual((await music.perform('HOME', 'pause'))['state'], 'Observed')
            for target, action in [('POCKET', 'pause'), ('HOME', 'play-file'), ('HOME', '../stop')]:
                self.assertEqual((await music.perform(target, action))['state'], 'Rejected')
            self.assertEqual(calls, [b'GET /api/status HTTP/1.1', b'POST /api/pause HTTP/1.1', b'GET /api/status HTTP/1.1'])
        finally:
            server.close(); await server.wait_closed()

    async def test_ambiguous_action_timeout(self):
        tasks = []
        async def fixture(reader, writer):
            tasks.append(asyncio.current_task())
            try:
                await reader.readuntil(b'\r\n\r\n')
                await asyncio.sleep(1)
            finally:
                writer.close(); await writer.wait_closed()
        server = await asyncio.start_server(fixture, '127.0.0.1', 0)
        try:
            music = MusicHTTP(port=server.sockets[0].getsockname()[1], timeout=.02)
            self.assertEqual((await music.perform('HOME', 'next'))['state'], 'Unknown')
        finally:
            server.close(); await server.wait_closed()
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

if __name__ == '__main__': unittest.main()
