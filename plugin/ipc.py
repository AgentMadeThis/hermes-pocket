"""Bounded Linux AF_UNIX RPC: same UID + private directory + mutual HMAC.

No retries. Challenge binds both messages to this connection, not a reusable bearer.
"""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import struct
from .protocol import canonical, private_dir

MAX_FRAME = 4096


def peer_uid(writer):
    peer = writer.get_extra_info('socket')
    return struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]


def tag(key, challenge, direction, payload):
    return hmac.new(key, canonical([challenge, direction, payload]), hashlib.sha256).hexdigest()


async def read_frame(reader):
    data = await reader.readline()
    if not data.endswith(b'\n') or len(data) > MAX_FRAME:
        raise ValueError('invalid frame')
    return json.loads(data)


async def write_frame(writer, value):
    raw = canonical(value) + b'\n'
    if len(raw) > MAX_FRAME:
        raise ValueError('frame too large')
    writer.write(raw)
    await writer.drain()


def verify(key, challenge, direction, frame):
    if not isinstance(frame, dict) or set(frame) != {'payload', 'mac'}:
        raise ValueError('invalid authenticated frame')
    if not isinstance(frame['mac'], str) or not hmac.compare_digest(
            frame['mac'], tag(key, challenge, direction, frame['payload'])):
        raise ValueError('authentication failed')
    return frame['payload']


class IPCServer:
    """Caller holds Journal's exclusive directory lock for the server lifetime."""
    def __init__(self, directory, key, callback, timeout=120):
        if not isinstance(key, bytes) or len(key) != 32:
            raise ValueError('32-byte key required')
        self.path = private_dir(directory) / 'pocket.sock'
        self.key, self.callback, self.timeout = key, callback, timeout
        self.server = None
        self.tasks = set()
        self.inode = None

    async def start(self):
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError('foreign socket path')
            # Exclusive journal lock proves no cooperative server still owns it.
            self.path.unlink()
        self.server = await asyncio.start_unix_server(self._handle, path=str(self.path), limit=MAX_FRAME)
        os.chmod(self.path, 0o600)
        self.inode = self.path.lstat().st_ino

    async def _handle(self, reader, writer):
        task = asyncio.current_task()
        if len(self.tasks) >= 8:
            writer.close()
            return
        self.tasks.add(task)
        try:
            if peer_uid(writer) != os.getuid():
                return
            challenge = secrets.token_hex(32)
            async with asyncio.timeout(2):
                await write_frame(writer, {'challenge': challenge, 'schema': 'pocket-ipc-v1'})
                frame = await read_frame(reader)
                payload = verify(self.key, challenge, 'request', frame)
            async with asyncio.timeout(self.timeout):
                result = await self.callback(payload)
                await write_frame(writer, {'payload': result, 'mac': tag(self.key, challenge, 'response', result)})
        except (Exception, asyncio.CancelledError):
            # Never serialize exception strings, payloads or credentials into logs.
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            self.tasks.discard(task)

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            if self.path.lstat().st_ino == self.inode:
                self.path.unlink()
        except FileNotFoundError:
            pass


class IPCClient:
    def __init__(self, directory, key, timeout=125):
        self.directory, self.key, self.timeout = Path(directory), key, timeout
        if not isinstance(key, bytes) or len(key) != 32:
            raise ValueError('32-byte key required')

    async def request(self, payload):
        directory = private_dir(self.directory)
        target = directory / 'pocket.sock'
        info = target.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('unsafe server socket')
        writer = None
        try:
            async with asyncio.timeout(self.timeout):
                reader, writer = await asyncio.open_unix_connection(str(target), limit=MAX_FRAME)
                if peer_uid(writer) != os.getuid():
                    raise ValueError('foreign server')
                hello = await read_frame(reader)
                if (not isinstance(hello, dict) or set(hello) != {'challenge', 'schema'} or
                        hello['schema'] != 'pocket-ipc-v1' or not isinstance(hello['challenge'], str) or
                        len(hello['challenge']) != 64):
                    raise ValueError('invalid challenge')
                challenge = hello['challenge']
                await write_frame(writer, {'payload': payload, 'mac': tag(self.key, challenge, 'request', payload)})
                return verify(self.key, challenge, 'response', await read_frame(reader))
        finally:
            if writer:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
