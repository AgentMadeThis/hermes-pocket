"""Native Hermes platform adapter; bridge alone owns authenticated radio and RF."""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
import json
import inspect
import re
from pathlib import Path
from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent
from .ipc import IPCServer
from .music import MusicHTTP, ACTIONS
from .location import SQLiteSnapshotReader
from .wire import decode, correlation, reply, music_payload, truncate
from .protocol import Journal, bounded_text, full_key, validate_event
from agent.secret_scope import get_secret


class PocketAdapter(BasePlatformAdapter):
    supports_async_delivery = False
    splits_long_messages = True
    supports_message_editing = False

    def __init__(self, config, *, policy=None):
        super().__init__(config, Platform('pocket'))
        extra = config.extra
        allowed = {'socket_dir', 'owners', 'timeout', 'home_music', 'home_source', 'home_volume_supported', 'gps_snapshot', 'group_sessions_per_user', 'thread_sessions_per_user'}
        if not isinstance(extra, dict) or set(extra) - allowed:
            raise ValueError('invalid Pocket configuration')
        for native_flag in ('group_sessions_per_user', 'thread_sessions_per_user'):
            if native_flag in extra and type(extra[native_flag]) is not bool:
                raise ValueError('invalid native session flag')
        owners = extra.get('owners')
        if not isinstance(owners, list) or not owners or len(owners) > 8:
            raise ValueError('explicit full owners required')
        self.owners = frozenset(full_key(owner) for owner in owners)
        self.directory = Path(extra['socket_dir'])
        self.policy = policy
        self.timeout = extra.get('timeout', 90)
        if type(self.timeout) not in (int, float) or not 0 < self.timeout <= 120:
            raise ValueError('invalid timeout')
        self.home_music = extra.get('home_music', False)
        if type(self.home_music) is not bool:
            raise ValueError('explicit boolean home_music required')
        volume_supported = extra.get('home_volume_supported', True)
        if type(volume_supported) is not bool:
            raise ValueError('explicit volume support boolean required')
        self.music = MusicHTTP(source=extra.get('home_source'), volume_supported=volume_supported)
        self.location = SQLiteSnapshotReader(extra['gps_snapshot']) if extra.get('gps_snapshot') else None
        self.journal = self.ipc = None
        self.pending = {}
        self.admission = asyncio.Lock()

    def toolsets_for_source(self, source):
        # Empty lists are ignored by installed run_turn.py:469. This native
        # nonempty sentinel is explicitly consumed by tools_config.py:746.
        return ['no_mcp']

    def _native_policy_ok(self):
        return self.policy is not None and self.policy.ready()

    async def connect(self, *, is_reconnect=False):
        if self.ipc:
            return True
        if not check_requirements():
            return False
        if not self._native_policy_ok():
            raise RuntimeError('Pocket execution veto is not active in a dedicated home')
        encoded = get_secret('POCKET_HMAC_KEY')
        if not isinstance(encoded, str) or re.fullmatch('[0-9a-f]{64}', encoded) is None:
            raise ValueError('POCKET_HMAC_KEY must be exactly 64 lowercase hexadecimal characters')
        key = bytes.fromhex(encoded)
        self.journal = Journal(self.directory)
        self.ipc = IPCServer(self.directory, key, self.receive, self.timeout + 2)
        try:
            await self.ipc.start()
        except BaseException:
            self.journal.close()
            self.journal = self.ipc = None
            raise
        self._running = True
        return True

    async def disconnect(self):
        self._running = False
        if self.ipc:
            await self.ipc.close()
            self.ipc = None
        if self.journal:
            self.journal.close()
            self.journal = None

    async def get_chat_info(self, chat_id):
        if chat_id not in self.owners:
            raise ValueError('unknown owner')
        return {'name': 'Pocket owner', 'type': 'dm', 'id': chat_id}

    async def handle_message(self, event):
        # Gateway startup resume and queued replays also use this entry point.
        # Only the currently authenticated IPC request may enter a native turn.
        pending = self.pending.get(event.message_id)
        source = event.source
        if (not pending or pending[1].done() or event.internal or event.allow_gateway_control or
                not source or source.chat_id != pending[0] or source.user_id != pending[0]):
            return
        await super().handle_message(event)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        # No broadcast, thread fallback, lifecycle notification or unsolicited RF.
        request_id = reply_to
        pending = self.pending.get(request_id)
        if chat_id not in self.owners or not pending or pending[0] != chat_id or pending[1].done():
            return SendResult(False, error='Pocket response has no active request', retryable=False)
        try:
            content = (truncate(content, min(91, pending[2]) - 14) if pending[3]
                       else bounded_text(content, pending[2]))
        except (ValueError, UnicodeError):
            return SendResult(False, error='Invalid Pocket response', retryable=False)
        pending[1].set_result(content)
        return SendResult(True, message_id=request_id,
                          raw_response={'state': 'ResponseReady', 'radio_delivery': 'NotAttempted',
                                        'meaning': 'Accepted by local bridge handoff only'})

    async def receive(self, event):
        try:
            validate_event(event, self.owners)
        except (ValueError, TypeError, KeyError, UnicodeError):
            return {'state': 'Rejected', 'code': 'ADMISSION_DENIED'}
        if not self._running or not self.journal:
            return {'state': 'Unavailable', 'code': 'ADAPTER_OFFLINE'}
        if self.admission.locked():
            return {'state': 'Busy', 'code': 'SINGLE_WORKER_BUSY'}
        async with self.admission:
            try:
                if not self.journal.claim(event):
                    return {'state': 'Duplicate', 'prior_state': self.journal.state(event['request_id']),
                            'code': 'NO_AUTOMATIC_REPLAY'}
            except ValueError:
                return {'state': 'Rejected', 'code': 'LEDGER_CAPACITY_OR_RATE'}
            request_id = event['request_id']
            try:
                response = await self._dispatch(event)
                response['request_id'] = request_id
                if 'text' in response:
                    response['text'] = bounded_text(response['text'], event['budget'])
                self.journal.finish(request_id, response['state'])
                return response
            except (Exception, asyncio.CancelledError):
                self.journal.finish(request_id, 'Unknown')
                if asyncio.current_task().cancelling():
                    raise
                response = {'state': 'Unknown', 'request_id': request_id, 'code': 'NO_AUTOMATIC_REPLAY'}
                cid = correlation(event['text']) if event['text'].startswith('P1') else None
                if cid:
                    response['text'] = reply(cid, 'u', 'Outcome unknown; no automatic retry.', event['budget'])
                return response
            finally:
                pending = self.pending.pop(request_id, None)
                if pending and not pending[1].done():
                    pending[1].cancel()

    async def _dispatch(self, event):
        # Event validation and durable outer claim MUST precede this decode.
        text = event['text']
        if not text.startswith('P1'):
            return await self._perform(event, text)
        cid = correlation(text)
        try:
            request = decode(text)
        except (ValueError, UnicodeError):
            result = {'state': 'Rejected', 'code': 'MALFORMED_P1'}
        else:
            if request.action in {'c', 'n'}:
                if request.action == 'c':
                    # Chat cannot smuggle a P1 command envelope into the model.
                    if request.text.startswith('P1'):
                        result = {'state': 'Rejected', 'code': 'NESTED_P1_NOT_ALLOWED'}
                    else:
                        result = await self._perform(event, request.text)
                else:
                    result = await self._perform(event, '/note ' + request.text)
            elif request.action == 'l':
                result = await self._perform(event, '/location')
            else:
                action = {'p': 'play', 's': 'pause', 'x': 'next', 'q': 'status', 'v': 'volume'}[request.action]
                result = await self._music(action, request.volume)
        if cid:
            state = result['state']
            letter = ('m' if state == 'Observed' and 'running' in result else
                      'u' if state in {'Unknown', 'Duplicate'} else
                      'e' if state in {'Rejected', 'Unavailable', 'Busy'} else 'o')
            payload = music_payload(result) if letter == 'm' else result.get('text') or result.get('code', state)
            result['text'] = reply(cid, letter, payload, event['budget'])
        return result

    async def _music(self, action, volume=None):
        if not self.home_music:
            return {'state': 'Unavailable', 'code': 'HOME_MUSIC_NOT_ENABLED',
                    'text': 'HOME music unavailable: not enabled.'}
        result = await self.music.perform('HOME', action, volume)
        if result['state'] == 'Observed':
            result['text'] = f"HOME music status {music_payload(result)}; no audible-output proof."
        else:
            result['text'] = result['code']
        return result

    async def _perform(self, event, text):
        owner = event['sender']
        if text == '/help':
            return {'state': 'ResponseReady', 'text': '/status, /note text, /location, /music HOME status; other text asks Hermes.'}
        if text == '/status':
            return {'state': 'ResponseReady', 'text': 'Pocket native adapter ready; radio delivery not observed.'}
        if text.startswith('/note '):
            note = text[6:].strip()
            if not note:
                return {'state': 'Rejected', 'code': 'EMPTY_NOTE'}
            self.journal.note(event, note)
            return {'state': 'ResponseReady', 'text': 'Note saved in Pocket plugin journal.'}
        if text == '/location':
            return self.location.read(owner) if self.location else {
                'state': 'Unavailable', 'code': 'LOCATION_SNAPSHOT_UNAVAILABLE',
                'text': 'Location unavailable: authenticated snapshot not configured.'}
        if text.startswith('/music '):
            parts = text.split(' ')
            if len(parts) != 3 or parts[1] != 'HOME' or parts[2] not in ACTIONS:
                return {'state': 'Rejected', 'code': 'HOME_ALLOWLIST_ONLY'}
            return await self._music(parts[2])
        if text.startswith('/'):
            return {'state': 'Rejected', 'code': 'COMMAND_NOT_ALLOWED'}
        if not self._message_handler or not self._native_policy_ok():
            return {'state': 'Unavailable', 'code': 'NATIVE_SESSION_POLICY_NOT_READY'}
        future = asyncio.get_running_loop().create_future()
        self.pending[event['request_id']] = (owner, future, event['budget'], event['text'].startswith('P1'))
        source = self.build_source(chat_id=owner, user_id=owner, message_id=event['request_id'])
        message = MessageEvent(text=text, source=source, user_id=owner, message_id=event['request_id'],
                               timestamp=datetime.fromtimestamp(event['timestamp'], timezone.utc),
                               allow_gateway_control=False, reply_expected=True,
                               metadata={'pocket_request_id': event['request_id']})
        async with asyncio.timeout(self.timeout):
            await self.handle_message(message)
            answer = await future
        return {'state': 'ResponseReady', 'text': answer}


def check_requirements():
    return (hasattr(BasePlatformAdapter, 'toolsets_for_source') and
            'allow_gateway_control' in inspect.signature(MessageEvent).parameters)


def register(ctx):
    from .policy import install
    policy = install(ctx)
    def adapter_factory(config):
        # Capability registration must be inert; enforce dedication only when
        # the gateway actually requests a Pocket runtime adapter.
        policy.activate()
        return PocketAdapter(config, policy=policy)
    handle = ctx.register_platform(name='pocket', label='Hermes Pocket', adapter_factory=adapter_factory,
                          check_fn=check_requirements, required_env=[],
                          allowed_users_env='POCKET_ALLOWED_USERS',
                          install_hint='Requires a dedicated Pocket-only in-process profile; see docs/INSTALL.md.')
    policy.platform_handle = handle
