"""Pocket v1 local contract. No radio, runtime, model or service imports."""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

EVENT_FIELDS = {'sender', 'timestamp', 'text', 'private', 'text_type', 'request_id', 'budget'}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode('utf-8')


def identity(value):
    # Exactly bridge.core.Inbound.identity; do NOT sort this positional tuple.
    raw = json.dumps(['meshcore-native-v1', value['sender'], value['timestamp'],
                      value['text_type'], value['text']], ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(raw.encode()).hexdigest()


def full_key(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('invalid full owner identity')
    return value


def validate_event(e, owners, now=None):
    if not isinstance(e, dict) or set(e) != EVENT_FIELDS:
        raise ValueError('invalid event fields')
    if full_key(e['sender']) not in owners or e['private'] is not True:
        raise ValueError('unauthorized event')
    if type(e['text_type']) is not int or e['text_type'] != 0:
        raise ValueError('unsupported message type')
    if type(e['timestamp']) is not int or not 0 <= e['timestamp'] <= 0xffffffff:
        raise ValueError('invalid timestamp')
    if type(e['budget']) is not int or not 24 <= e['budget'] <= 160:
        raise ValueError('invalid budget')
    text = e['text']
    try:
        valid = (isinstance(text, str) and bool(text) and '\x00' not in text and
                 '\ufffd' not in text and len(text.encode()) <= e['budget'])
    except UnicodeError:
        valid = False
    if not valid or e['request_id'] != identity(e):
        raise ValueError('invalid text or identity')
    now = time.time() if now is None else now
    if not now - 300 <= e['timestamp'] <= now + 30:
        raise ValueError('expired event')
    return e


def bounded_text(text, budget):
    """Bound aggregate to four labelled bridge chunks, codepoint safe.

    Bridge remains the sole RF chunker. Its labels occupy six bytes each.
    """
    if not isinstance(text, str) or not text or '\x00' in text or '\ufffd' in text:
        raise ValueError('invalid response')
    raw = text.encode()
    cap = 4 * (budget - 6)
    if len(raw) <= cap:
        return text
    return raw[:cap - len('…'.encode())].decode('utf-8', errors='ignore') + '…'


def private_dir(path):
    path = Path(path)
    # Never fix unsafe operator-owned permissions or follow symlinks.
    if not path.is_absolute():
        raise ValueError('absolute private directory required')
    owner_fenced = False
    for parent in (*reversed(path.parents), path):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            if parent != path:
                raise ValueError('parent directory missing') from None
            parent.mkdir(mode=0o700)
            info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError('symlink/non-directory ancestor')
        if parent == path and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError('private directory must be owned 0700')
        if (not owner_fenced and parent != path and info.st_mode & 0o022
                and not info.st_mode & stat.S_ISVTX):
            raise ValueError('writable ancestor')
        if info.st_uid == os.getuid() and not info.st_mode & 0o077:
            owner_fenced = True
    return path


def private_file(path):
    path = Path(path)
    private_dir(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ValueError('private owned regular file required')
        return os.read(fd, 4097)
    finally:
        os.close(fd)


class Journal:
    """Candidate-owned ledger only. At-most-once even across ambiguous restarts."""
    def __init__(self, directory):
        self.directory = private_dir(directory)
        self.lock = os.open(self.directory / 'owner.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.lock)
            raise
        target = self.directory / 'events.sqlite'
        try:
            fd = os.open(target, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            info = os.fstat(fd)
            os.close(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('unsafe ledger')
            self.db = sqlite3.connect(target, isolation_level=None)
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.executescript('''CREATE TABLE IF NOT EXISTS events
                (id TEXT PRIMARY KEY, owner TEXT NOT NULL, admitted REAL NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS notes
                (id TEXT PRIMARY KEY, owner TEXT NOT NULL, text TEXT NOT NULL);''')
            self.db.execute("UPDATE events SET state='Unknown' WHERE state='Running'")
        except BaseException:
            if hasattr(self, 'db'):
                self.db.close()
            os.close(self.lock)
            raise

    def claim(self, event):
        if self.state(event['request_id']) is not None:
            return False
        if self.db.execute('SELECT count(*) FROM events').fetchone()[0] >= 4096:
            raise ValueError('ledger capacity reached; owner review required')
        if self.db.execute('SELECT count(*) FROM events WHERE owner=? AND admitted>?',
                           (event['sender'], time.time() - 60)).fetchone()[0] >= 6:
            raise ValueError('rate limited')
        self.db.execute("INSERT INTO events VALUES(?,?,?,'Running')",
                        (event['request_id'], event['sender'], time.time()))
        return True

    def state(self, identity):
        row = self.db.execute('SELECT state FROM events WHERE id=?', (identity,)).fetchone()
        return row[0] if row else None

    def finish(self, identity, state):
        self.db.execute("UPDATE events SET state=? WHERE id=? AND state='Running'", (state, identity))

    def note(self, event, text):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('INSERT INTO notes VALUES(?,?,?)', (event['request_id'], event['sender'], text))
            self.finish(event['request_id'], 'ResponseReady')
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def close(self):
        self.db.close()
        os.close(self.lock)
