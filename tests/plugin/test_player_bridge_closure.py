"""Actual Bridge lifecycle tests; isolated local DB, no radio/model/music IO."""
import asyncio
import json
import tempfile
from pathlib import Path
import unittest
from integration.host.bridge.core import Bridge, Inbound, QueryUnavailable

OWNER = 'a1' * 32
FRAMES = []

class FaultQuery:
    def __init__(self, fault): self.fault, self.calls = fault, 0
    async def query_event(self, message, budget):
        self.calls += 1
        raise self.fault

class Closure(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
    def bridge(self, query=None):
        b = Bridge(self.root, [OWNER], query, now=lambda: 1000, payload_budget=91)
        self.addCleanup(b.close)
        return b
    def msg(self, text='P2|1234abcd|q|'):
        return Inbound(OWNER, 1000, text)
    def check(self, b, m, state, kind, body=None):
        row = b.db.execute('SELECT state,response FROM requests WHERE id=?', (m.identity(),)).fetchone()
        self.assertEqual(row['state'], state)
        expected = 'R2|1234abcd|' + kind + '|'
        self.assertTrue(row['response'].startswith(expected), row['response'])
        self.assertLessEqual(len(row['response'].encode()), 91)
        if body: self.assertEqual(row['response'], expected + body.replace(';', ' '))
        FRAMES.append({'case':self.id(),'wire':row['response'],'utf8_bytes':len(row['response'].encode())})
    async def test_queued_expiry(self):
        q=FaultQuery(AssertionError('must not dispatch')); b=self.bridge(q);m=self.msg()
        self.assertEqual(b.admit(m)[0], 'Queued');b.now=lambda:1400
        await b.process_one();self.check(b,m,'Expired','e','Request expired; not started.')
        self.assertEqual(q.calls,0)
    async def test_restart_running_unknown_no_replay(self):
        b=Bridge(self.root,[OWNER],now=lambda:1000,payload_budget=91);m=self.msg()
        b.admit(m);b.db.execute("UPDATE requests SET state='Running'");b.close()
        b=self.bridge();self.check(b,m,'Unknown','u','Interrupted; no automatic retry.')
        self.assertIsNone(await b.process_one());self.assertEqual(b.admit(m)[0],'Unknown')
    async def test_timeout_unknown_once(self):
        q=FaultQuery(TimeoutError('SECRET'));b=self.bridge(q);m=self.msg()
        b.admit(m);await b.process_one();self.check(b,m,'Unknown','u','Outcome unknown; no automatic retry.')
        self.assertIsNone(await b.process_one());self.assertEqual(q.calls,1)
    async def test_cancellation_unknown(self):
        q=FaultQuery(asyncio.CancelledError());b=self.bridge(q);m=self.msg();b.admit(m)
        with self.assertRaises(asyncio.CancelledError):await b.process_one()
        self.check(b,m,'Unknown','u','Interrupted; no automatic retry.')
    async def test_definite_unavailable_correlated(self):
        q=FaultQuery(QueryUnavailable('SECRET'));b=self.bridge(q);m=self.msg();b.admit(m)
        await b.process_one();self.check(b,m,'Unavailable','e')
    async def test_malformed_never_dispatch(self):
        q=FaultQuery(AssertionError('must not dispatch'));b=self.bridge(q);m=self.msg('P2|1234abcd|v|+1')
        b.admit(m);await b.process_one();self.check(b,m,'Unavailable','e','MALFORMED_P2')
        self.assertEqual(q.calls,0)
    async def test_unconfigured_never_model(self):
        class Model:
            async def query(self,*args):raise AssertionError('model called')
        b=self.bridge(Model());m=self.msg();b.admit(m);await b.process_one()
        self.check(b,m,'Unavailable','e','PLAYER_NATIVE_IPC_REQUIRED')
    async def test_actual_authenticated_ipc_timeout_no_replay(self):
        import os
        import time
        from plugin.ipc import IPCServer
        from plugin.client import BridgeEventQuery
        key = os.urandom(32)
        keyfile = self.root/'key'
        keyfile.write_bytes(key);keyfile.chmod(0o600)
        calls=[]
        async def delayed(event):
            calls.append(event)
            await asyncio.sleep(2)
            raise AssertionError('late response must not be used')
        server=IPCServer(self.root/'ipc',key,delayed)
        await server.start();self.addAsyncCleanup(server.close)
        q=BridgeEventQuery(self.root/'ipc',key,[OWNER],timeout=.1)
        b=self.bridge(q);b.now=time.time
        m=Inbound(OWNER,int(time.time()),'P2|1234abcd|v|20')
        self.assertEqual(b.admit(m)[0],'Queued')
        await b.process_one();self.check(b,m,'Unknown','u')
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0]['request_id'],m.identity())
        self.assertEqual(calls[0]['budget'],91)
        self.assertEqual(b.admit(m)[0],'Unknown')
        self.assertIsNone(await b.process_one())
        self.assertEqual(len(calls),1)
    async def test_reserved_and_uncorrelated_malformed_never_model(self):
        q=FaultQuery(AssertionError('must not dispatch'));b=self.bridge(q)
        for text in ['P2 garbage','P2|00000000|q|','R2|1234abcd|u|bad']:
            m=self.msg(text);b.admit(m);await b.process_one()
            self.assertEqual(b.admit(m)[0],'Unavailable')
        self.assertEqual(q.calls,0)
    async def test_outer_admission_unchanged(self):
        b=self.bridge();m=self.msg()
        for bad, expected in [(Inbound('b2'*32,1000,m.text),'Rejected'),
                              (Inbound(OWNER,1000,m.text,request_id='0'*64),'Mismatch'),
                              (Inbound(OWNER,1,m.text),'Expired')]:
            self.assertEqual(b.admit(bad),(expected,None))
        self.assertEqual(b.db.execute('SELECT count(*) FROM requests').fetchone()[0],0)
    async def test_p1_error_bytes_unchanged(self):
        q=FaultQuery(TimeoutError());b=self.bridge(q);m=self.msg('P1|1234abcd|q|')
        b.admit(m);await b.process_one()
        self.assertEqual(b.response(m.identity()),'R1|1234abcd|u|Outcome unknown; no automatic retry.')

if __name__ == '__main__':unittest.main()
