"""Local, version-gated safety adapter for reviewed meshcore 2.3.17 seams.

Installed package files are never changed. Constructed before connect(), so the
transport never sees the unbounded dispatcher or the lossy private-text reader.
"""
from __future__ import annotations

import asyncio
import inspect
from importlib.metadata import version

from meshcore import MeshCore, EventType
from meshcore.events import EventDispatcher, Event
from meshcore.reader import MessageReader
from meshcore.packets import PacketType
from meshcore.serial_cx import SerialConnection
from .serial_safety import BoundedSerialConnection

INGRESS_LIMIT = 64
CALLBACK_LIMIT = 8
CLOSE_TIMEOUT = 1.0


class BoundedDispatcher(EventDispatcher):
    def __init__(self):
        super().__init__()
        self.fault: str | None = None
        self.dropped = 0
        self.closed = False

    def check_health(self):
        if self.running and self._task and self._task.done():
            self.fault = self.fault or 'SDK dispatcher stopped unexpectedly'
        if self.fault:
            raise RuntimeError(self.fault)

    async def start(self):
        if self.closed:
            raise RuntimeError('SDK dispatcher already closed')
        if self.queue is None:
            self.queue = asyncio.Queue(maxsize=INGRESS_LIMIT)
        await super().start()

    async def dispatch(self, event):
        if not self.running or self.closed:
            raise RuntimeError('SDK dispatcher not running')
        if self.fault:
            self.dropped += 1
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1
            self.fault = 'SDK ingress overflow'
        # Never block the reader: command response waiters need this same path.
        # Overflow latches a fatal error checked by the runner, not silent loss.

    def _spawn_background(self, coro):
        if self.fault or len(self._background_tasks) >= CALLBACK_LIMIT:
            coro.close()
            self.fault = self.fault or 'SDK callback capacity exceeded'
            return None
        return super()._spawn_background(coro)

    async def _execute_callback(self, callback, event):
        if self.fault:
            return
        try:
            result = callback(event)
            if inspect.isawaitable(result):
                await result
        except Exception:
            self.fault = self.fault or 'SDK callback failed'

    async def _process_events(self):
        while self.running:
            event = await self.queue.get()
            try:
                if self.fault:
                    continue
                for subscription in self.subscriptions.copy():
                    if subscription.event_type not in (None, event.type):
                        continue
                    if not all(event.attributes.get(k) == v for k, v in subscription.attribute_filters.items()):
                        continue
                    if inspect.iscoroutinefunction(subscription.callback):
                        self._spawn_background(self._execute_callback(subscription.callback, event.clone()))
                    else:
                        try:
                            result = subscription.callback(event.clone())
                            # Reviewed SDK callbacks are sync futures or async defs.
                            # A disguised coroutine must not escape task bounds.
                            if inspect.iscoroutine(result):
                                result.close()
                                self.fault = self.fault or 'SDK unsupported coroutine callback'
                        except Exception:
                            self.fault = self.fault or 'SDK callback failed'
            finally:
                self.queue.task_done()

    async def stop(self):
        self.closed = True
        self.running = False
        tasks = set(self._background_tasks)
        if self._task:
            tasks.add(self._task)
        for task in tasks:
            task.cancel()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=CLOSE_TIMEOUT)
            await asyncio.gather(*done, return_exceptions=True)
            if pending:
                self.fault = self.fault or 'SDK shutdown timeout'
                raise RuntimeError('SDK shutdown timeout')
        self._background_tasks.clear()
        self._task = None
        if self.queue is not None:
            while not self.queue.empty():
                self.queue.get_nowait()
                self.queue.task_done()


class StrictPrivateReader(MessageReader):
    def __init__(self, dispatcher):
        super().__init__(dispatcher)
        self.invalid_private_frames = 0

    async def handle_rx(self, data):
        # Snapshot mutable transport bytes. Validate BEFORE upstream errors=ignore.
        raw = bytes(data)
        if raw and raw[0] in (PacketType.CONTACT_MSG_RECV.value,
                              PacketType.CONTACT_MSG_RECV_V3.value):
            offset = 3 if raw[0] == PacketType.CONTACT_MSG_RECV_V3.value else 0
            header = 13 + offset
            valid = len(raw) >= header
            if valid:
                text_start = header + (4 if raw[8 + offset] == 2 else 0)
                valid = len(raw) >= text_start and len(raw) - text_start <= 160
                if valid:
                    try:
                        raw[text_start:].decode('utf-8', errors='strict')
                    except UnicodeError:
                        valid = False
            if not valid:
                self.invalid_private_frames += 1
                await self.dispatcher.dispatch(Event(EventType.ERROR, {'reason': 'invalid_private_frame'}))
                return
        await super().handle_rx(bytearray(raw))


class SafeMeshCore(MeshCore):
    def __init__(self, cx, *args, **kwargs):
        if version('meshcore') != '2.3.17':
            raise RuntimeError('unreviewed MeshCore SDK version')
        dispatcher = BoundedDispatcher()
        if isinstance(cx, SerialConnection):
            BoundedSerialConnection.protect(cx, dispatcher)
        super().__init__(cx, *args, **kwargs)
        old = self.dispatcher
        if old.running or old.queue is not None or old._background_tasks:
            raise RuntimeError('SDK dispatcher unexpectedly active during construction')
        self.dispatcher = dispatcher
        self.dispatcher.subscriptions = old.subscriptions
        for subscription in self.dispatcher.subscriptions:
            subscription.dispatcher = self.dispatcher
        self._reader = StrictPrivateReader(self.dispatcher)
        self.commands.set_dispatcher(self.dispatcher)
        self.commands.set_reader(self._reader)
        self.connection_manager.event_dispatcher = self.dispatcher
        self.connection_manager.set_reader(self._reader)

    async def disconnect(self):
        # SDK base closes dispatcher before emitting DISCONNECTED. Close transport
        # first, then cancel bounded callbacks/queued work even on transport error.
        try:
            await self.connection_manager.disconnect()
        finally:
            self.connection_manager._is_connected = False
            try:
                # Manager skips disconnect when connect failed or loss cleared its
                # flag; the serial worker/transport still belong to this instance.
                connection = self.connection_manager.connection
                if isinstance(connection, BoundedSerialConnection) and not connection.closed:
                    await connection.disconnect()
            finally:
                await self.dispatcher.stop()
