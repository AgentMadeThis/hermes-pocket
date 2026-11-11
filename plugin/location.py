"""Explicit owner-scoped read-only GPS snapshot interface. No live DB default."""
from contextlib import closing
from pathlib import Path
from typing import Protocol
import math
import re
import sqlite3
import time
from .protocol import full_key

class SnapshotReader(Protocol):
    def read(self, owner: str) -> dict: ...

class SQLiteSnapshotReader:
    """Read native positions schema from an operator-selected snapshot only.

    The snapshot must originate from the authenticated native tracker. This
    reader cannot authenticate arbitrary manually injected SQLite rows.
    Connections/cursors never survive a call, including query/schema failures.
    """
    def __init__(self, path, *, now=time.time, stale_after=60):
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError('explicit absolute GPS snapshot required')
        self.now, self.stale_after = now, stale_after

    def read(self, owner):
        full_key(owner)
        unavailable = {'state': 'Unavailable', 'code': 'LOCATION_SNAPSHOT_UNAVAILABLE',
                       'text': 'Location unavailable: no authenticated owner snapshot.'}
        try:
            with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=.2)) as db:
                db.execute('PRAGMA query_only=ON').close()
                db.row_factory = sqlite3.Row
                with closing(db.execute('''SELECT * FROM positions WHERE owner=? AND private=1
                     ORDER BY host_received DESC, rowid DESC LIMIT 1''', (owner,))) as cursor:
                    row = cursor.fetchone()
            if row is None:
                return unavailable
            received, age = row['host_received'], row['age_ms']
            elapsed = self.now() - received
            if (not math.isfinite(elapsed) or elapsed < 0 or type(age) is not int or not 0 <= age <= 15000
                    or not re.fullmatch('[0-9a-f]{8}', row['session']) or row['session'] == '00000000'
                    or type(row['seq']) is not int or not 1 <= row['seq'] <= 0xffffffff
                    or not re.fullmatch('[0-9a-f]{64}', row['request_id'])
                    or not -90000000 <= row['lat_e6'] <= 90000000
                    or not -180000000 <= row['lon_e6'] <= 180000000):
                return unavailable
            fix_age = elapsed + age / 1000
            state = 'Stale' if fix_age > self.stale_after else 'Observed'
            text = (f"{'Stale' if state == 'Stale' else 'Last'} GPS "
                    f"{row['lat_e6']/1e6:.6f},{row['lon_e6']/1e6:.6f}; "
                    f"rx {int(elapsed)}s ago; fix age {int(fix_age)}s.")
            return {'state': state, 'code': 'LOCATION_' + state.upper(), 'text': text,
                    'received_age_s': elapsed, 'fix_age_s': fix_age,
                    'device_timestamp': row['device_timestamp'], 'session': row['session'], 'seq': row['seq']}
        except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError, KeyError):
            return unavailable
