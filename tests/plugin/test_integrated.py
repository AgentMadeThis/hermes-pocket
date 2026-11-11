"""Offline integrated contract RED/GREEN fixtures; no native runtime required."""
import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from plugin.wire import decode, reply, music_payload
from plugin.location import SQLiteSnapshotReader

OWNER = 'a1' * 32

class Wire(unittest.TestCase):
    def test_utf8_bound_and_header_reserved(self):
        for cap in (24, 91, 160):
            text = reply('00000001', 'o', 'Я😀' * 100, cap)
            self.assertLessEqual(len(text.encode()), min(cap, 91))
            self.assertTrue(text.endswith('…'))
            self.assertTrue(text.startswith('R1|00000001|o|'))
    def test_shared_golden(self):
        import json
        data = json.loads((Path(__file__).resolve().parents[2] / 'tests/integration/backend/golden.json').read_text())
        for value in data['requests']:
            parsed = decode(value['body'])
            self.assertEqual(parsed.action, value['action'])
            if 'text' in value: self.assertEqual(parsed.text, value['text'])
            if 'volume' in value: self.assertEqual(parsed.volume, value['volume'])
        for value in data['invalid']:
            with self.assertRaises(ValueError): decode(value)
        for value in data['music_replies']:
            _, cid, state, payload = value.split('|')
            self.assertEqual(reply(cid, state, payload), value)

    def test_strict_grammar(self):
        for value in ('P1|00000000|c|hi', 'P1|ABCDEF01|c|hi', 'P1|00000001|p|url',
                      'P1|00000001|v|101', 'P1|00000001|v|01', 'P1|00000001|c|\t',
                      'P1|00000001|c|' + 'я' * 39):
            with self.assertRaises(ValueError): decode(value)
        self.assertEqual(decode('P1|00000001|c|Привет 😀').text, 'Привет 😀')
        self.assertEqual(decode('P1|00000001|v|100').volume, 100)
        self.assertEqual(music_payload({'running': True, 'paused': False, 'volume': None}), '1,0,?')
        self.assertEqual(music_payload({'running': False, 'paused': True, 'volume': 35}), '0,1,35')
        for obj in ({'running': 1, 'paused': False}, {'running': True, 'paused': False, 'volume': 101}):
            with self.assertRaises(ValueError): music_payload(obj)

class GPS(unittest.TestCase):
    def test_owner_age_stale_missing_and_closed(self):
        from integration.host.bridge.tracking import SCHEMA
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'snapshot.sqlite'
            with closing(sqlite3.connect(path)) as db:
                db.executescript(SCHEMA)
                db.execute('INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                           (OWNER, '00000001', 1, 'b' * 64, 1000, 999, 2000, 12345678, 23456789, 10, 7, 1))
                db.commit()
            reader = SQLiteSnapshotReader(path, now=lambda: 1005, stale_after=30)
            value = reader.read(OWNER)
            self.assertEqual(value['received_age_s'], 5)
            self.assertEqual(value['fix_age_s'], 7)
            self.assertEqual(value['state'], 'Observed')
            self.assertEqual(reader.read('c2' * 32)['state'], 'Unavailable')
            self.assertEqual(SQLiteSnapshotReader(path, now=lambda: 1100).read(OWNER)['state'], 'Stale')
            self.assertEqual(SQLiteSnapshotReader(path, now=lambda: 900).read(OWNER)['state'], 'Unavailable')
            self.assertEqual(SQLiteSnapshotReader(Path(d) / 'absent').read(OWNER)['state'], 'Unavailable')
            self.assertFalse((Path(d) / 'absent').exists())
            # Read-only connection and explicit closure: no open file descriptor retained.
            import os
            for _ in range(20): reader.read(OWNER)
            targets = []
            for fd in Path('/proc/self/fd').iterdir():
                try: targets.append(os.readlink(fd))
                except FileNotFoundError: pass
            self.assertNotIn(str(path), targets)

if __name__ == '__main__': unittest.main()
