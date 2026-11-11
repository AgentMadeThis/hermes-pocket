"""Fail-closed admission, durable single-worker queue, and bounded replies.

The trusted boundary is a local USB companion and its contact table, not names.
Only the serial adapter may resolve a six-byte sender prefix to a full key.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Protocol

from plugin.wire import PocketOutcome, correlation, reply
from plugin import player_wire

from .tracking import SCHEMA as TRACKING_SCHEMA, parse_position, reserved_position, store_position

NATIVE_TEXT_BYTES = 160  # MeshCore BaseChatMesh.h: 10 * CIPHER_BLOCK_SIZE
MAX_PARTS = 4
MAX_INPUT_BYTES = 160


def player_failure(text, kind, message, budget, fallback):
    """Additive P2 error framing; leave all legacy response bytes untouched."""
    cid = player_wire.correlation(text) if text.startswith('P2') else None
    return player_wire.failure(cid, kind, message, budget) if cid else fallback


def full_key(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("owner key must be exactly 64 lowercase hex characters")
    return value


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Inbound:
    sender: str
    timestamp: int
    text: str
    private: bool = True
    text_type: int = 0
    request_id: str | None = None

    def identity(self) -> str:
        return digest(["meshcore-native-v1", self.sender, self.timestamp, self.text_type, self.text])


def chunk_text(text: str, budget: int = NATIVE_TEXT_BYTES, max_parts: int = MAX_PARTS) -> list[str]:
    """UTF-8 codepoint-safe, including part labels in the byte budget.

    Oversize answers end in a visible truncation marker, never hidden parts.
    """
    if budget < 24 or not 1 <= max_parts <= 9:
        raise ValueError("invalid chunk bounds")
    if len(text.encode()) <= budget:
        return [text]
    usable = budget - len(f"[{max_parts}/{max_parts}] ".encode())
    pieces, current = [], ""
    for char in text:
        if len((current + char).encode()) > usable:
            pieces.append(current)
            current = ""
        current += char
        if len(pieces) == max_parts:
            break
    else:
        if current:
            pieces.append(current)
    if len(pieces) >= max_parts and "".join(pieces) != text:
        pieces = pieces[:max_parts]
        tail = pieces[-1]
        marker = ' …[сокращено]' if len(' …[сокращено]'.encode()) <= usable else '…'
        while len((tail + marker).encode()) > usable:
            tail = tail[:-1]
        pieces[-1] = tail + marker
    return [f"[{i + 1}/{len(pieces)}] {part}" for i, part in enumerate(pieces)]


class QueryUnavailable(RuntimeError):
    """Definite refusal before provider dispatch (no ambiguous effect)."""


class QueryAdapter(Protocol):
    async def query(self, sender: str, text: str) -> str: ...


class DisabledQuery:
    async def query(self, sender: str, text: str) -> str:
        return "Модель выключена. Доступны /help, /status и /note текст."


class Bridge:
    """One instance per state directory, enforced across real processes.

    Queued jobs survive restart; a previously running job becomes Unknown and
    is never automatically replayed. Note insertion and Done commit atomically.
    """
    def __init__(self, state: Path, owners: list[str], query: QueryAdapter | None = None,
                 *, queue_limit: int = 8, rate_limit: int = 6, ttl: int = 300,
                 now=time.time, payload_budget: int = NATIVE_TEXT_BYTES):
        if not owners:
            raise ValueError("no owner pinned; bridge refuses startup")
        self.owners = frozenset(full_key(k) for k in owners)
        if type(payload_budget) is not int or not 24 <= payload_budget <= NATIVE_TEXT_BYTES:
            raise ValueError("invalid transport payload budget")
        self.payload_budget = payload_budget
        if queue_limit < 1 or rate_limit < 1 or ttl < 1:
            raise ValueError("invalid limits")
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(state, 0o700)
        self.lock = (state / "bridge.lock").open("a+")
        os.chmod(state / "bridge.lock", 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError("bridge already running") from None
        self.db = sqlite3.connect(state / "bridge.sqlite", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        os.chmod(state / "bridge.sqlite", 0o600)
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS requests (
          id TEXT PRIMARY KEY, sender TEXT NOT NULL, timestamp INTEGER NOT NULL,
          payload_hash TEXT NOT NULL, text TEXT NOT NULL, admitted REAL NOT NULL,
          state TEXT NOT NULL, response TEXT, delivery TEXT NOT NULL DEFAULT 'pending');
        CREATE TABLE IF NOT EXISTS notes (
          request_id TEXT PRIMARY KEY REFERENCES requests(id), sender TEXT NOT NULL,
          created REAL NOT NULL, text TEXT NOT NULL);
        ''')
        self.db.executescript(TRACKING_SCHEMA)
        for interrupted in self.db.execute("SELECT id,text FROM requests WHERE state='Running'").fetchall():
            cid = correlation(interrupted['text']) if interrupted['text'].startswith('P1') else None
            response = (reply(cid, 'u', 'Interrupted; no automatic retry.', payload_budget) if cid else
                        'Unknown: execution interrupted; no automatic retry.')
            response = player_failure(interrupted['text'], 'u', 'Interrupted; no automatic retry.', payload_budget, response)
            self.db.execute("UPDATE requests SET state='Unknown',response=? WHERE id=?", (response, interrupted['id']))
        self.db.execute("UPDATE requests SET delivery='Unknown' WHERE delivery='Sending'")
        self.query = query or DisabledQuery()
        self.queue_limit, self.rate_limit, self.ttl, self.now = queue_limit, rate_limit, ttl, now
        self.worker_lock = asyncio.Lock()
        self.reject_revoked_deliveries()

    def reject_revoked_deliveries(self):
        # No oldest-pending head-of-line starvation after enrollment changes.
        placeholders = ','.join('?' for _ in self.owners)
        self.db.execute(f"UPDATE requests SET delivery='Rejected' WHERE delivery='pending' AND sender NOT IN ({placeholders})",
                        tuple(self.owners))

    def close(self):
        self.db.close()
        self.lock.close()

    def admit(self, message: Inbound) -> tuple[str, str | None]:
        status, identity = self._admit(message)
        # Classify only AFTER admission's full boundary checks. All reserved
        # failures are silent at the unchanged serial adapter notice whitelist.
        if status in {'Invalid', 'Mismatch', 'Expired'} and reserved_position(message.text):
            return 'Telemetry' + status, None
        return status, identity

    def _admit(self, message: Inbound) -> tuple[str, str | None]:
        # No command parsing, model context, or response for unauthorized traffic.
        if message.sender not in self.owners or message.private is not True or type(message.text_type) is not int or message.text_type != 0:
            return "Rejected", None
        if (type(message.timestamp) is not int or not 0 <= message.timestamp <= 0xFFFFFFFF
                or not isinstance(message.text, str) or not message.text
                or "\x00" in message.text or "\ufffd" in message.text):
            return "Invalid", None
        try:
            if len(message.text.encode()) > min(MAX_INPUT_BYTES, self.payload_budget):
                return "Invalid", None
        except UnicodeError:
            return "Invalid", None
        identity = message.identity()
        if message.request_id is not None and message.request_id != identity:
            return "Mismatch", None
        if reserved_position(message.text):
            now = self.now()
            if now - message.timestamp > self.ttl or message.timestamp > now + 30:
                return 'TelemetryExpired', None
            try:
                point = parse_position(message.text)
            except ValueError:
                return 'TelemetryInvalid', None
            return store_position(self.db, message.sender, message.timestamp,
                                  identity, now, point), None
        payload_hash = digest([message.sender, message.timestamp, message.text_type, message.text])
        old = self.db.execute("SELECT * FROM requests WHERE id=?", (identity,)).fetchone()
        if old:
            if old['payload_hash'] != payload_hash or old['sender'] != message.sender:
                return "Mismatch", None
            return old['state'], identity
        now = self.now()
        if now - message.timestamp > self.ttl or message.timestamp > now + 30:
            return "Expired", None
        if self.db.execute("SELECT count(*) FROM requests WHERE state IN ('Queued','Running')").fetchone()[0] >= self.queue_limit:
            return "Busy", None
        if self.db.execute("SELECT count(*) FROM requests WHERE sender=? AND admitted>?", (message.sender, now - 60)).fetchone()[0] >= self.rate_limit:
            return "RateLimited", None
        self.db.execute("INSERT INTO requests(id,sender,timestamp,payload_hash,text,admitted,state) VALUES(?,?,?,?,?,?,'Queued')",
                        (identity, message.sender, message.timestamp, payload_hash, message.text, now))
        return "Queued", identity

    def response(self, identity: str) -> str | None:
        row = self.db.execute("SELECT response FROM requests WHERE id=?", (identity,)).fetchone()
        return row[0] if row else None

    async def process_one(self) -> str | None:
        async with self.worker_lock:
            row = self.db.execute("SELECT * FROM requests WHERE state='Queued' ORDER BY admitted,rowid LIMIT 1").fetchone()
            if not row:
                return None
            identity = row['id']
            # Revalidate enrollment after restart/reconfiguration, before context use.
            if row['sender'] not in self.owners:
                self.db.execute("UPDATE requests SET state='Rejected',text='',delivery='Rejected' WHERE id=?", (identity,))
                return identity
            if self.now() - row['timestamp'] > self.ttl:
                cid = correlation(row['text']) if row['text'].startswith('P1') else None
                response = reply(cid, 'e', 'Request expired; not started.', self.payload_budget) if cid else 'Request expired; not started.'
                response = player_failure(row['text'], 'e', 'Request expired; not started.', self.payload_budget, response)
                self.db.execute("UPDATE requests SET state='Expired',response=? WHERE id=?", (response, identity))
                return identity
            self.db.execute("UPDATE requests SET state='Running' WHERE id=?", (identity,))
            text = row['text']
            try:
                if text.startswith('P2'):
                    try:
                        player_wire.decode(text)
                    except (ValueError, UnicodeError):
                        raise PocketOutcome('Unavailable', player_failure(text, 'e', 'MALFORMED_P2', self.payload_budget, 'Unavailable: MALFORMED_P2')) from None
                    if not callable(getattr(self.query, 'query_event', None)):
                        raise PocketOutcome('Unavailable', player_failure(text, 'e', 'PLAYER_NATIVE_IPC_REQUIRED', self.payload_budget, 'Unavailable: PLAYER_NATIVE_IPC_REQUIRED'))
                if text.startswith('R2'):
                    raise PocketOutcome('Unavailable', 'Unavailable: RESERVED_R2_INPUT')
                if callable(getattr(self.query, 'query_event', None)):
                    # Admission/authentication and Running journaling already occurred.
                    # Preserve the exact original envelope identity and payload budget.
                    response = await self.query.query_event(
                        Inbound(row['sender'], row['timestamp'], text,
                                private=True, text_type=0, request_id=identity),
                        self.payload_budget)
                elif text == '/help':
                    response = '/help — помощь; /status — состояние; /note текст — заметка. Другой текст — вопрос модели (по умолчанию выключена). Без управления устройствами.'
                elif text == '/status':
                    # Local read-only facts only: no hostnames, addresses, secrets or process list.
                    with open('/proc/uptime', encoding='ascii') as f:
                        uptime = int(float(f.read().split()[0]))
                    model_state = ('выключена' if isinstance(self.query, DisabledQuery) else 'включена')
                    response = f'Pocket v0.1; время работы {uptime} с; модель {model_state}.'
                elif text.startswith('/note '):
                    note = text[6:].strip()
                    response = 'Заметка сохранена.' if note else 'Формат: /note текст'
                    self.db.execute('BEGIN IMMEDIATE')
                    try:
                        if note:
                            self.db.execute("INSERT INTO notes VALUES(?,?,?,?)", (identity, row['sender'], self.now(), note))
                        self.db.execute("UPDATE requests SET state='Done',response=? WHERE id=?", (response, identity))
                        self.db.execute('COMMIT')
                    except BaseException:
                        self.db.execute('ROLLBACK')
                        raise
                    return identity
                elif text.startswith('/'):
                    response = 'Неизвестная команда. Справка: /help.'
                else:
                    response = await self.query.query(row['sender'], text)
                # Persist bounded output; never save unbounded provider output.
                parts = chunk_text(response, budget=self.payload_budget)
                response = response if len(parts) == 1 else ''.join(p.split('] ', 1)[1] for p in parts)
                self.db.execute("UPDATE requests SET state='Done',response=? WHERE id=?", (response, identity))
            except PocketOutcome as exc:
                self.db.execute("UPDATE requests SET state=?,response=? WHERE id=?", (exc.state, exc.text, identity))
            except QueryUnavailable as exc:
                if text.startswith('P2'):
                    refusal = player_failure(text, 'e', 'NATIVE_OPERATION_UNAVAILABLE', self.payload_budget, 'Unavailable: NATIVE_OPERATION_UNAVAILABLE')
                    self.db.execute("UPDATE requests SET state='Unavailable',response=? WHERE id=?", (refusal, identity))
                elif callable(getattr(self.query, 'query_event', None)):
                    # Typed, sanitized semantic refusal; do not call it a model failure.
                    refusal = 'Unavailable: ' + getattr(exc, 'pocket_code', 'NATIVE_OPERATION_UNAVAILABLE')
                    self.db.execute("UPDATE requests SET state='Unavailable',response=? WHERE id=?", (refusal, identity))
                else:
                    self.db.execute("UPDATE requests SET state='Unavailable',response='Модель недоступна: среда Hermes не готова. Запрос не отправлен.' WHERE id=?", (identity,))
            except asyncio.CancelledError:
                cid = correlation(text) if text.startswith('P1') else None
                response = reply(cid, 'u', 'Interrupted; no automatic retry.', self.payload_budget) if cid else 'Unknown: execution interrupted; no automatic retry.'
                response = player_failure(text, 'u', 'Interrupted; no automatic retry.', self.payload_budget, response)
                self.db.execute("UPDATE requests SET state='Unknown',response=? WHERE id=?", (response, identity))
                raise
            except Exception:
                # No exception strings: these can contain provider tokens or prompt text.
                cid = correlation(text) if text.startswith('P1') else None
                response = reply(cid, 'u', 'Outcome unknown; no automatic retry.', self.payload_budget) if cid else 'Unknown: outcome unknown; no automatic retry.'
                response = player_failure(text, 'u', 'Outcome unknown; no automatic retry.', self.payload_budget, response)
                self.db.execute("UPDATE requests SET state='Unknown',response=? WHERE id=?", (response, identity))
            return identity

    async def deliver(self, identity: str, send) -> str:
        row = self.db.execute('SELECT * FROM requests WHERE id=?', (identity,)).fetchone()
        if not row:
            return 'NotReady'
        if row['sender'] not in self.owners:
            self.db.execute("UPDATE requests SET delivery='Rejected' WHERE id=? AND delivery='pending'", (identity,))
            return 'Rejected'
        if not row['response']:
            return 'NotReady'
        if row['delivery'] != 'pending':
            return row['delivery']
        self.db.execute("UPDATE requests SET delivery='Sending' WHERE id=?", (identity,))
        try:
            for part in chunk_text(row['response'], budget=self.payload_budget):
                await send(row['sender'], part)
        except Exception:
            self.db.execute("UPDATE requests SET delivery='Unknown' WHERE id=?", (identity,))
            return 'Unknown'
        self.db.execute("UPDATE requests SET delivery='Submitted' WHERE id=?", (identity,))
        return 'Submitted'  # Native MSG_SENT != over-radio delivery ACK.
