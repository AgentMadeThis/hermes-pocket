"""Private /pos1 wire format and atomic, silent telemetry storage.

Only call the parser/store after the core owner/privacy/type/UTF-8/identity
checks. No transport, model, coordinate inference or automatic replies here.
"""
from dataclasses import dataclass
import re

MAX_WIRE_BYTES = 91
TELEMETRY_RATE = 6
SCHEMA = '''
CREATE TABLE IF NOT EXISTS positions (
 owner TEXT NOT NULL, session TEXT NOT NULL, seq INTEGER NOT NULL,
 request_id TEXT NOT NULL, host_received REAL NOT NULL,
 device_timestamp INTEGER NOT NULL, age_ms INTEGER NOT NULL,
 lat_e6 INTEGER NOT NULL, lon_e6 INTEGER NOT NULL,
 alt_m INTEGER NOT NULL, sats INTEGER NOT NULL,
 private INTEGER NOT NULL CHECK(private=1),
 PRIMARY KEY(owner,session,seq));
CREATE INDEX IF NOT EXISTS positions_owner_received
 ON positions(owner,host_received);
'''
_UNSIGNED = r'(?:0|[1-9][0-9]*)'
_SIGNED = r'(?:0|-?[1-9][0-9]*)'
_WIRE = re.compile(r'/pos1 ([0-9a-f]{8}) (' + _UNSIGNED + r') (' +
    _SIGNED + r') (' + _SIGNED + r') (' + _SIGNED + r') (' +
    _UNSIGNED + r') (' + _UNSIGNED + r')', re.ASCII)


def reserved_position(text):
    # Reserve lookalikes too; this is classification, not coordinate parsing.
    return isinstance(text, str) and text.lstrip().casefold().startswith('/pos')


@dataclass(frozen=True)
class Position:
    session: str
    seq: int
    lat_e6: int
    lon_e6: int
    alt_m: int
    sats: int
    age_ms: int


def parse_position(text: str) -> Position:
    try:
        if len(text.encode('ascii')) > MAX_WIRE_BYTES:
            raise ValueError('invalid telemetry')
    except (UnicodeError, AttributeError):
        raise ValueError('invalid telemetry') from None
    match = _WIRE.fullmatch(text)
    if not match:
        raise ValueError('invalid telemetry')
    session, *numbers = match.groups()
    seq, lat, lon, alt, sats, age = map(int, numbers)
    if (session == '00000000' or not 1 <= seq <= 0xffffffff
            or not -90000000 <= lat <= 90000000
            or not -180000000 <= lon <= 180000000
            or not -1000 <= alt <= 100000 or not 0 <= sats <= 255
            or not 0 <= age <= 15000):
        raise ValueError('invalid telemetry')
    return Position(session, seq, lat, lon, alt, sats, age)


def store_position(db, owner, timestamp, request_id, now, point: Position):
    """Single SQLite writer transaction: dedup, rate check, insert, commit.

    First receipt is immutable. A retry may carry a fresh envelope timestamp;
    exactness compares every point field, including reported fix age.
    """
    db.execute('BEGIN IMMEDIATE')
    try:
        old = db.execute('''SELECT lat_e6,lon_e6,alt_m,sats,age_ms FROM positions
            WHERE owner=? AND session=? AND seq=?''',
            (owner, point.session, point.seq)).fetchone()
        values = (point.lat_e6, point.lon_e6, point.alt_m, point.sats, point.age_ms)
        if old is not None:
            status = 'TelemetryDuplicate' if tuple(old) == values else 'TelemetryConflict'
        elif db.execute('SELECT count(*) FROM positions WHERE owner=? AND host_received>?',
                        (owner, now - 60)).fetchone()[0] >= TELEMETRY_RATE:
            status = 'TelemetryRateLimited'
        else:
            db.execute('''INSERT INTO positions
                (owner,session,seq,request_id,host_received,device_timestamp,
                 lat_e6,lon_e6,alt_m,sats,age_ms,private)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,1)''',
                (owner, point.session, point.seq, request_id, now, timestamp, *values))
            status = 'TelemetryStored'
        db.execute('COMMIT')
        return status
    except BaseException:
        db.execute('ROLLBACK')
        raise
