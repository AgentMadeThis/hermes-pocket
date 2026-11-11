"""Offline contract regressions: generated owners, no radio/runtime imports."""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from plugin.protocol import identity, validate_event, bounded_text, Journal

OWNER = 'a1' * 32

def event(text='Hello', **changes):
    value = dict(sender=OWNER, timestamp=int(time.time()), text=text, private=True,
                 text_type=0, budget=91)
    value.update(changes)
    value['request_id'] = identity(value)
    return value

class ContractTests(unittest.TestCase):
    def test_full_identity_and_owner(self):
        e = event()
        self.assertEqual(validate_event(e, {OWNER}), e)
        with self.assertRaises(ValueError): validate_event(e, {'b2' * 32})
        with self.assertRaises(ValueError): validate_event(event(sender=OWNER[:12]), {OWNER})
        with self.assertRaises(ValueError): validate_event(event(private=False), {OWNER})

    def test_utf8_budget_and_invalid(self):
        for budget in (24, 91, 160):
            value = bounded_text('Я😀' * 400, budget)
            self.assertLessEqual(len(value.encode()), 4 * (budget - 6))
            self.assertTrue(value.endswith('…'))
        for text in ('я' * 46, '\x00', '\ufffd', '\ud800'):
            with self.assertRaises(ValueError): validate_event(event(text), {OWNER})

    def test_expiry_mismatch_and_types(self):
        for changes in ({'timestamp': 0}, {'timestamp': True}, {'text_type': False}, {'budget': 161}):
            with self.assertRaises(ValueError): validate_event(event(**changes), {OWNER})
        e = event(); e['request_id'] = '0' * 64
        with self.assertRaises(ValueError): validate_event(e, {OWNER})

    def test_restart_duplicate_never_replay(self):
        with tempfile.TemporaryDirectory() as d:
            j = Journal(Path(d)); e = event()
            self.assertTrue(j.claim(e)); self.assertFalse(j.claim(e)); j.close()
            j = Journal(Path(d))
            self.assertEqual(j.state(e['request_id']), 'Unknown')
            self.assertFalse(j.claim(e)); j.close()

    def test_note_atomic_and_dedup(self):
        with tempfile.TemporaryDirectory() as d:
            j = Journal(Path(d)); e = event('/note test')
            self.assertTrue(j.claim(e)); j.note(e, 'test')
            self.assertEqual(j.state(e['request_id']), 'ResponseReady')
            self.assertFalse(j.claim(e))
            self.assertEqual(j.db.execute('select count(*) from notes').fetchone()[0], 1)
            j.close()

    def test_real_process_restart_does_not_replay(self):
        import json
        import os
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as d:
            e = event('crash fixture')
            code = ('import os,sys,json; from pathlib import Path; from plugin.protocol import Journal; '
                    'j=Journal(Path(sys.argv[1])); assert j.claim(json.loads(sys.argv[2])); '
                    'print("FIXTURE_DURABLE_RUNNING",flush=True); os._exit(0)')
            run = subprocess.run([sys.executable, '-B', '-c', code, d, json.dumps(e)],
                                 cwd=Path(__file__).resolve().parents[2],
                                 env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
                                 timeout=15, text=True, capture_output=True)
            self.assertEqual(run.returncode, 0)
            self.assertEqual(run.stdout.strip(), 'FIXTURE_DURABLE_RUNNING')
            j = Journal(Path(d))
            try:
                self.assertEqual(j.state(e['request_id']), 'Unknown')
                self.assertFalse(j.claim(e))
            finally:
                j.close()

    def test_exclusive_state_owner(self):
        with tempfile.TemporaryDirectory() as d:
            j = Journal(Path(d))
            with self.assertRaises((RuntimeError, BlockingIOError)): Journal(Path(d))
            j.close()

if __name__ == '__main__': unittest.main()
