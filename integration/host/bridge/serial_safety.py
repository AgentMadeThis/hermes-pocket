"""Bound the pre-reader serial seam locally; never edit the installed SDK."""
from __future__ import annotations

import asyncio

from meshcore.serial_cx import SerialConnection, serial_asyncio

FRAME_LIMIT = 64
MAX_FRAME_BYTES = 300  # Reviewed SDK serial protocol limit, including packet type.
MAX_RX_BYTES = (MAX_FRAME_BYTES + 3) * FRAME_LIMIT
CLOSE_TIMEOUT = 1.0


class BoundedSerialConnection(SerialConnection):
    @classmethod
    def protect(cls, cx, dispatcher):
        # The inherited SDK factory constructs a stock instance before calling
        # SafeMeshCore. Promote only that unopened exact type, preserving identity
        # for protocol/caller references, without modifying any SDK class/global.
        if type(cx) is not SerialConnection:
            raise RuntimeError('unsupported serial adapter')
        if (cx.transport or cx._connected_event.is_set() or cx.reader is not None
                or cx._background_tasks or cx.header or cx.inframe):
            raise RuntimeError('serial adapter already active')
        cx.__class__ = cls
        cx.dispatcher = dispatcher
        cx.frames = asyncio.Queue(maxsize=FRAME_LIMIT)
        cx.buffer = bytearray()
        cx.worker = None
        cx.closed = False
        cx.fault = None
        return cx

    def _discard(self):
        self.buffer.clear()
        while not self.frames.empty():
            self.frames.get_nowait()
            self.frames.task_done()

    def _fail(self, reason):
        self.fault = self.fault or reason
        self.dispatcher.fault = self.dispatcher.fault or self.fault
        self._discard()

    class MCSerialClientProtocol(SerialConnection.MCSerialClientProtocol):
        def connection_lost(self, exc):
            self.cx._connected_event.clear()
            # Do not use upstream's per-notification background task spawn.
            if not self.cx.closed:
                self.cx._fail('SDK serial connection lost')

    def handle_rx(self, data):
        if self.closed or self.fault or self.dispatcher.fault:
            return
        if not self.dispatcher.running or self.reader is None:
            self._fail('SDK serial ingress not ready')
            return
        # Cap per-callback work BEFORE copying input. Retain only one partial
        # frame (<=303 bytes); complete frames live only in the bounded queue.
        if len(data) > MAX_RX_BYTES:
            self._fail('SDK serial input limit exceeded')
            return
        data = memoryview(data)
        pos = 0
        while pos < len(data):
            target = 3
            if len(self.buffer) >= 3:
                target += int.from_bytes(self.buffer[1:3], 'little')
            take = min(target - len(self.buffer), len(data) - pos)
            self.buffer.extend(data[pos:pos + take])
            pos += take
            if self.buffer and self.buffer[0] != 0x3e:
                self._fail('SDK serial malformed framing')
                return
            if len(self.buffer) < 3:
                continue
            size = int.from_bytes(self.buffer[1:3], 'little')
            if not 1 <= size <= MAX_FRAME_BYTES:
                self._fail('SDK serial invalid frame length')
                return
            if len(self.buffer) < size + 3:
                continue
            try:
                self.frames.put_nowait(bytes(self.buffer[3:]))
            except asyncio.QueueFull:
                self._fail('SDK serial frame overflow')
                return
            self.buffer.clear()
            if self.worker is None:
                self.worker = self._spawn_background(self._read_frames())
                self.worker.add_done_callback(self._reader_done)

    def _reader_done(self, task):
        # Also catches cancellation before the coroutine's first instruction.
        if not task.cancelled():
            task.exception()
        if not self.closed:
            self._fail('SDK serial reader stopped')

    async def _read_frames(self):
        try:
            while not self.closed and not self.fault and not self.dispatcher.fault:
                frame = await self.frames.get()
                try:
                    await self.reader.handle_rx(frame)
                finally:
                    self.frames.task_done()
                # One reader, with dispatcher progress between frames.
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            if not self.closed:
                self._fail('SDK serial reader cancelled')
            raise
        except Exception:
            self._fail('SDK serial reader failed')
        finally:
            self._discard()

    async def connect(self, timeout=10.0):
        if self.closed or self.fault:
            raise RuntimeError('SDK serial adapter unavailable')
        self._connected_event.clear()
        transport = None
        try:
            transport, _ = await serial_asyncio.create_serial_connection(
                asyncio.get_running_loop(), lambda: self.MCSerialClientProtocol(self),
                self.port, baudrate=self.baudrate)
            # Keep local ownership even if connection_made has not fired yet.
            self.transport = transport
            await asyncio.wait_for(self._connected_event.wait(), timeout)
            self.dispatcher.check_health()
            return self.port
        except BaseException:
            await self.disconnect()
            raise

    async def disconnect(self):
        self.closed = True
        transport, self.transport = self.transport, None
        self._connected_event.clear()
        try:
            if transport is not None:
                transport.close()
        finally:
            tasks = set(self._background_tasks)
            if self.worker is not None:
                tasks.add(self.worker)
            for task in tasks:
                task.cancel()
            if tasks:
                done, pending = await asyncio.wait(tasks, timeout=CLOSE_TIMEOUT)
                await asyncio.gather(*done, return_exceptions=True)
                if pending:
                    self._fail('SDK serial shutdown timeout')
                    raise RuntimeError('SDK serial shutdown timeout')
            self._background_tasks.clear()
            self.worker = None
            self._discard()
