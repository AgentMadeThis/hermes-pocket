"""Real meshcore==2.3.17 serial source. Importing never opens serial.

Only CONTACT_MSG_RECV payload type PRIV, txt_type=0 is accepted. The USB
companion decrypts private packets and identifies a 6-byte sender prefix; we
require a unique full-key resolution in its refreshed trusted contact table.
"""
from __future__ import annotations

import asyncio
import logging
import re
from meshcore import EventType
from .sdk_safety import SafeMeshCore as MeshCore
from .contact_inventory import unique_contact

DRAIN_LIMIT = 256
DRAIN_TIMEOUT = 30
GET_MSG_TIMEOUT = 15

from .core import Bridge, Inbound, NATIVE_TEXT_BYTES, full_key


class MeshCoreSerialSource:
    def __init__(self, bridge: Bridge, device: str, *, baudrate: int = 115200,
                 root_key: str | None = None, required_profile: str | None = None):
        self.bridge, self.device, self.baudrate = bridge, device, baudrate
        self.root_key = full_key(root_key) if root_key is not None else None
        if required_profile not in (None, 'HP2-AUTH1'):
            raise ValueError('unsupported secure-pair profile')
        if required_profile is not None and (self.root_key is None or bridge.payload_budget != 91):
            raise ValueError('secure-pair profile requires a root pin and 91-byte payload budget')
        self.required_profile = required_profile
        self.mc = None
        self.contacts: dict = {}
        self.online = False
        self.notices = asyncio.Queue(maxsize=2)
        self.last_notice: dict[str, float] = {}

    def normalize(self, event) -> Inbound | None:
        p = event.payload
        if event.type != EventType.CONTACT_MSG_RECV or not isinstance(p, dict):
            return None
        if p.get('type') != 'PRIV' or p.get('txt_type') != 0:
            return None
        prefix = p.get('pubkey_prefix')
        if not isinstance(prefix, str) or not re.fullmatch('[0-9a-f]{12}', prefix):
            return None
        candidates = []
        for contact in self.contacts.values():
            key = contact.get('public_key') if isinstance(contact, dict) else None
            try:
                key = full_key(key)
            except ValueError:
                continue
            if key.startswith(prefix):
                candidates.append(key)
        # Do NOT use get_contact_by_key_prefix: upstream returns first match.
        if len(candidates) != 1 or candidates[0] not in self.bridge.owners:
            return None
        try:
            self._owner_contact(candidates[0])
        except (ValueError, RuntimeError):
            return None
        if not isinstance(p.get('text'), str) or type(p.get('sender_timestamp')) is not int:
            return None
        return Inbound(candidates[0], p['sender_timestamp'], p['text'])

    def _check_health(self):
        if self.mc and hasattr(self.mc, 'dispatcher'):
            self.mc.dispatcher.check_health()

    def _owner_contact(self, owner):
        full_key(owner)
        if owner not in self.bridge.owners:
            raise RuntimeError('owner no longer enrolled')
        try:
            return unique_contact(self.contacts, owner)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from None

    async def _receive(self, event):
        self._check_health()
        msg = self.normalize(event)
        if msg:
            # Bounded durable queue; no task spawned for each radio packet.
            status, _ = self.bridge.admit(msg)
            notices = {'Busy': 'Очередь заполнена. Отправьте новый запрос позже.',
                       'RateLimited': 'Слишком много запросов. Подождите минуту.',
                       'Expired': 'Срок истёк. Проверьте часы и повторите запрос.',
                       'Invalid': 'Некорректное личное сообщение.',
                       'Mismatch': 'Запрос отклонён: идентификатор не совпадает.'}
            now = self.bridge.now()
            if (status in notices and not self.notices.full()
                    and now - self.last_notice.get(msg.sender, float('-inf')) >= 60):
                self.last_notice[msg.sender] = now
                self.notices.put_nowait((msg.sender, notices[status]))

    async def send(self, owner: str, text: str):
        if not self.online or not self.mc or owner not in self.bridge.owners:
            raise RuntimeError('radio offline')
        if len(text.encode()) > self.bridge.payload_budget:
            raise ValueError('native payload budget exceeded')
        self._check_health()
        contact = self._owner_contact(owner)  # Recheck immediately before EVERY SDK send.
        result = await asyncio.wait_for(self.mc.commands.send_msg(contact, text), 15)
        self._check_health()
        if result.type != EventType.MSG_SENT:
            raise RuntimeError('radio submission unavailable')
        # Leave a full guarded TX interval plus RX/flash-service recovery
        # between secure parts; never flood the half-duplex handheld queue.
        await asyncio.sleep(3.0 if self.bridge.payload_budget == 91 else 0.25)

    async def _drain_messages(self):
        # Explicit startup/re-notification drain, never the SDK one-message helper.
        # Runs outside dispatcher callbacks: get_msg needs that dispatcher alive.
        async with asyncio.timeout(DRAIN_TIMEOUT):
            for _ in range(DRAIN_LIMIT):
                self._check_health()
                result = await asyncio.wait_for(self.mc.commands.get_msg(), GET_MSG_TIMEOUT)
                self._check_health()
                if result.type == EventType.NO_MORE_MSGS:
                    return
                if result.type not in (EventType.CONTACT_MSG_RECV, EventType.CHANNEL_MSG_RECV,
                                       EventType.CHANNEL_DATA_RECV):
                    raise RuntimeError('native message drain failed')
                await asyncio.sleep(0)  # Allow the bounded receiver callbacks to run.
        raise RuntimeError('native message drain limit exceeded')

    async def _fetch_loop(self, waiting):
        while True:
            await waiting.wait()
            waiting.clear()
            await self._drain_messages()

    async def run(self):
        # Upstream debug logs contain plaintext messages; keep them out of logs.
        logger = logging.getLogger('meshcore')
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        logger.setLevel(logging.CRITICAL + 1)
        fetch_task = None
        waiting_subscription = None
        try:
            self.mc = await asyncio.wait_for(MeshCore.create_serial(self.device, baudrate=self.baudrate), 20)
            if self.root_key is not None:
                info = await asyncio.wait_for(self.mc.commands.send_appstart(), 15)
                if (info.type != EventType.SELF_INFO or info.payload.get('public_key') != self.root_key
                        or info.payload.get('name') != 'Hermes'):
                    raise RuntimeError('root companion identity mismatch')
            if self.required_profile is not None:
                info = await asyncio.wait_for(self.mc.commands.send_device_query(), 15)
                if (info.type != EventType.DEVICE_INFO
                        or info.payload.get('ver') != self.required_profile):
                    raise RuntimeError('root companion security profile mismatch')
            result = await asyncio.wait_for(self.mc.commands.get_contacts(), 15)
            if result.type != EventType.CONTACTS or not isinstance(result.payload, dict):
                raise RuntimeError('trusted contact table unavailable')
            self.contacts = result.payload
            self._check_health()
            for owner in self.bridge.owners:
                self._owner_contact(owner)
            self.mc.subscribe(EventType.CONTACT_MSG_RECV, self._receive)
            waiting = asyncio.Event()
            # Synchronous, constant-size coalescing only. Never await commands here.
            waiting_subscription = self.mc.subscribe(EventType.MESSAGES_WAITING, lambda event: waiting.set())
            await self._drain_messages()
            fetch_task = asyncio.create_task(self._fetch_loop(waiting))
            self.online = True
            while True:
                self._check_health()
                if fetch_task.done():
                    await fetch_task  # Propagate meaningful fetch errors; never silently stop.
                    raise RuntimeError('native message fetch stopped')
                if not self.mc.is_connected:
                    raise RuntimeError('radio offline')
                if not self.notices.empty():
                    owner, notice = self.notices.get_nowait()
                    try:
                        await self.send(owner, notice)
                    except Exception:
                        pass  # Best-effort bounded status, never automatic retry.
                self._check_health()
                await self.bridge.process_one()
                pending = self.bridge.db.execute("SELECT id FROM requests WHERE delivery='pending' AND response IS NOT NULL ORDER BY admitted LIMIT 1").fetchone()
                if pending:
                    await self.bridge.deliver(pending['id'], self.send)
                await asyncio.sleep(0.2)
        finally:
            self.online = False
            if waiting_subscription:
                waiting_subscription.unsubscribe()
            if fetch_task:
                fetch_task.cancel()
                done, pending = await asyncio.wait({fetch_task}, timeout=1)
                if pending:
                    raise RuntimeError('native fetch shutdown timeout')
                # Retrieve exceptions even when another failure triggered shutdown.
                await asyncio.gather(*done, return_exceptions=True)
            if self.mc:
                await asyncio.wait_for(self.mc.disconnect(), 3)
