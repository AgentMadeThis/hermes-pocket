"""P2 RED/GREEN contract tests; production never receives mutation probes."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote
from plugin.player_wire import decode, correlation, frame, list_frame, detail_frame, state_frame
from plugin.player import Catalog, PlayerHTTP

OWNER = 'a1' * 32

class Wire(unittest.TestCase):
    def test_grammar_and_namespaces(self):
        for action in 'qpsxbz':
            self.assertEqual(decode(f'P2|00000001|{action}|').action, action)
        for action, value in [('l','00000000,0'),('l','1234abcd,998'),('d','1234abcd,abcd1234'),
                              ('k','1234abcd,abcd1234'),('v','100'),('j','863999'),('e','soft')]:
            self.assertEqual(decode(f'P2|00000001|{action}|{value}').action,action)
        for bad in ['P1|00000001|q|','P2|00000000|q|','P2|ABCDEF01|q|','P2|00000001|q|x',
                    'P2|00000001|v|01','P2|00000001|v|101','P2|00000001|j|864000',
                    'P2|00000001|j|-1','P2|00000001|e|preset?value=bass',
                    'P2|00000001|l|00000000,01','P2|00000001|l|00000000,1',
                    'P2|00000001|d|00000000,1234abcd','P2|00000001|k|1234abcd,00000000',
                    'P2|00000001|q|\n','R2|00000001|q|']:
            with self.subTest(bad=bad), self.assertRaises(ValueError): decode(bad)
    def test_shared_golden_vectors(self):
        data=json.loads((Path(__file__).resolve().parents[2]/'tests/integration/backend/player-golden.json').read_text())
        for request in data['requests']:decode(request)
        for item in data['frames']:
            self.assertEqual(len(item['wire'].encode()),item['utf8_bytes'])
            self.assertLessEqual(item['utf8_bytes'],91)
        tracks=[{'id':'1234abcd','label':'Я😀'*100},{'id':'abcd1234','label':'Long English title / café / 東京 '*6}]
        self.assertEqual(list_frame('00000001','87654321',0,2,tracks),data['frames'][0]['wire'])
        self.assertEqual(detail_frame('00000001','87654321',tracks[0]),data['frames'][3]['wire'])
    def test_scalar_limits_and_unknowns(self):
        tracks=[{'id':'1234abcd','label':'Я😀'*100},{'id':'abcd1234','label':'界'*100}]
        for value in [list_frame('00000001','1234abcd',998,999,tracks[:1]),
                      list_frame('00000001','1234abcd',0,2,tracks),
                      detail_frame('00000001','1234abcd',tracks[0]),
                      state_frame('00000001','1234abcd',{'id':'00000000','running':False,
                        'paused':False,'volume':None,'position':None,'duration':None,'title':'Я😀'*100})]:
            self.assertLessEqual(len(value.encode()),91)
            self.assertTrue(value.endswith('…'))
        self.assertIn(',?,?,?;',state_frame('00000001','1234abcd',{'id':'00000000',
            'running':True,'paused':True,'volume':None,'position':None,'duration':None,'title':'Unknown'}))
        self.assertNotIn('|bad',detail_frame('00000001','1234abcd',{'id':'1234abcd','label':'a\t\r\nbad|bad,;'}).split('|',3)[3])

class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); (self.root/'lounge').mkdir()
        self.path=self.root/'lounge'/'A & #+ Я.mp3'; self.path.write_bytes(b'fixture')
        self.row={'path':str(self.path),'source':'lounge','label':'Title Я','ext':'.mp3'}
    def cat(self,rows=None,owner=OWNER):
        return Catalog(owner,{'ok':True,'count':len(rows if rows is not None else [self.row]),
            'tracks':rows if rows is not None else [self.row]},roots={'lounge':self.root/'lounge'})
    def test_stable_owner_scoped_revision_and_label_changes(self):
        c=self.cat(); self.assertEqual(c.revision,self.cat().revision)
        self.assertNotEqual(c.tracks[0]['id'],self.cat(owner='b2'*32).tracks[0]['id'])
        changed=self.cat([{**self.row,'label':'Changed'}])
        self.assertEqual(c.tracks[0]['id'],changed.tracks[0]['id'])
        self.assertNotEqual(c.revision,changed.revision)
    def test_escape_symlink_prefix_url_controls_and_duplicate(self):
        outside=self.root/'outside.mp3';outside.write_bytes(b'fixture')
        link=self.root/'lounge'/'link.mp3';link.symlink_to(outside)
        for p in [str(outside),str(link),str(self.root/'lounge-evil'/'a.mp3'),
                  'https://evil/x.mp3',str(self.path)+'\n',str(self.root/'lounge'/'..'/'outside.mp3')]:
            with self.subTest(path=p), self.assertRaises(ValueError):self.cat([{**self.row,'path':p}])
        with self.assertRaises(ValueError): self.cat([self.row,self.row])
        self.assertEqual(self.cat([{**self.row,'source':'requests'}]).tracks,[])
    def test_collision_zero_and_max_count(self):
        with patch('plugin.player.digest8',return_value='00000000'),self.assertRaises(ValueError):self.cat()
        path=self.root/'lounge'/'b.mp3';path.write_bytes(b'x')
        with patch('plugin.player.digest8',return_value='1234abcd'), self.assertRaises(ValueError):
            self.cat([self.row,{**self.row,'path':str(path)}])
        with self.assertRaises(ValueError):self.cat([self.row]*1000)

class HTTP(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        (self.root/'lounge').mkdir();self.path=self.root/'lounge'/'A & #+ Я.mp3';self.path.write_bytes(b'x')
        self.lib={'ok':True,'count':1,'tracks':[{'path':str(self.path),'label':'Я😀'*60,'source':'lounge','ext':'.mp3'}]}
        self.status={'ok':True,'running':True,'paused':True,'volume':13.6,'track':str(self.path),
            'track_label':'Observed Я','position':274.24,'duration':5030.44,'playlist':'lounge',
            'mode':'playlist','queue':[],'eq':{'presets':{'flat':[],'bass':[],'soft':[]}}}
        self.calls=[];self.fail_post=False;self.fail_status=False
        async def fixture(reader,writer):
            try:
                line=(await reader.readuntil(b'\r\n\r\n')).split(b'\r\n')[0].decode()
                self.calls.append(line)
                if line.startswith('POST') and self.fail_post:return
                if line.startswith('GET /api/library '):body=self.lib
                elif line.startswith('GET /api/status '):body={'ok':False} if self.fail_status else self.status
                else:body={'ok':True,'volume':99,'track_label':'DO NOT TRUST POST'}
                raw=getattr(self,'raw_response',None)
                if raw is None:raw=json.dumps(body).encode()
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(raw)).encode()+b'\r\n\r\n'+raw)
                await writer.drain()
            finally:writer.close();await writer.wait_closed()
        self.server=await asyncio.start_server(fixture,'127.0.0.1',0)
        self.player=PlayerHTTP(port=self.server.sockets[0].getsockname()[1],source='lounge',
            roots={'lounge':self.root/'lounge'},timeout=.5)
    async def asyncTearDown(self):
        self.server.close();await self.server.wait_closed();self.tmp.cleanup()
    async def request(self,action,payload=''):
        return await self.player.perform(OWNER,decode('P2|00000001|'+action+'|'+payload))
    async def test_real_http_catalog_state_detail_select_encoded_path(self):
        c=await self.player.catalog(OWNER)
        result=await self.request('q'); self.assertEqual(result['observed']['volume'],14)
        self.assertEqual(result['observed']['position'],274)
        self.assertEqual(result['observed']['duration'],5030)
        self.assertEqual(result['observed']['title'],'Observed Я')
        result=await self.request('k',c.revision+','+c.tracks[0]['id'])
        self.assertEqual(result['state'],'Observed')
        posts=[x for x in self.calls if x.startswith('POST')]
        self.assertEqual(len(posts),1);self.assertIn('play-file?path=%2F',posts[0])
        self.assertIn('%26%20%23%2B%20%D0%AF',posts[0])
        self.assertEqual(self.calls[-1],'GET /api/status HTTP/1.1')
    async def test_stale_unknown_no_mutation(self):
        c=await self.player.catalog(OWNER)
        for payload in ['deadbeef,'+c.tracks[0]['id'],c.revision+',deadbeef']:
            self.assertEqual((await self.request('k',payload))['state'],'Rejected')
        self.assertFalse(any(x.startswith('POST') for x in self.calls))
    async def test_absolute_volume_seek_and_eq_route(self):
        for action,payload,route in [('v','0','vol/0'),('j','123','seek/123'),('e','bass','eq/preset?value=bass')]:
            self.calls.clear();r=await self.request(action,payload)
            self.assertEqual(r['state'],'Observed');self.assertIn('POST /api/'+route+' HTTP/1.1',self.calls)
            self.assertEqual(self.calls[-1],'GET /api/status HTTP/1.1')
    async def test_seek_missing_duration_bounds_and_eq_unavailable(self):
        for duration in [None,0,False,float('nan')]:
            self.status['duration']=duration
            self.assertEqual((await self.request('j','123'))['state'],'Unavailable')
        self.status['duration']=10
        self.assertEqual((await self.request('j','10'))['state'],'Rejected')
        self.status['eq']={}
        self.assertEqual((await self.request('e','bass'))['state'],'Unavailable')
        self.assertFalse(any(x.startswith('POST') for x in self.calls))
    async def test_uncertain_post_never_retry(self):
        self.fail_post=True
        self.assertEqual((await self.request('v','15'))['state'],'Unknown')
        self.assertEqual(sum(x.startswith('POST') for x in self.calls),1)
    async def test_status_unavailable_prevents_mutation(self):
        self.fail_status=True
        self.assertEqual((await self.request('v','15'))['state'],'Unavailable')
        self.assertFalse(any(x.startswith('POST') for x in self.calls))
    async def test_malformed_duplicate_nonfinite_json_failclosed(self):
        for raw in [b'{',b'{"ok":true,"ok":true,"count":0,"tracks":[]}',
                    b'{"ok":true,"count":NaN,"tracks":[]}']:
            self.raw_response=raw
            self.assertEqual((await self.request('v','15'))['state'],'Unavailable')
        self.assertFalse(any(x.startswith('POST') for x in self.calls))
    async def test_missing_observations_not_fake_zero_and_position_pair(self):
        for key in ['position','duration','volume']:self.status.pop(key)
        result=await self.request('q')
        self.assertIn(',?,?,?;',result['text'])
        self.status.update(position=20,duration=10,volume=0)
        result=await self.request('q')
        self.assertIn(',0,?,10;',result['text'])
    async def test_catalog_pagination_empty_end_and_revised_label(self):
        result=await self.request('l','00000000,0');self.assertEqual(result['state'],'Observed')
        c=await self.player.catalog(OWNER)
        result=await self.request('l',c.revision+',2');self.assertEqual(result['state'],'Rejected')
        self.lib['tracks'][0]['label']='new label'
        self.assertEqual((await self.request('d',c.revision+','+c.tracks[0]['id']))['state'],'Rejected')
        self.lib={'ok':True,'count':0,'tracks':[]}
        self.assertTrue((await self.request('l','00000000,0'))['text'].endswith(',0,0'))
    async def test_next_queue_and_foreign_context_refused(self):
        self.status['queue']=[{'track':'/unapproved'}]
        self.assertEqual((await self.request('x'))['state'],'Rejected')
        self.status['track']='/unapproved'
        self.assertEqual((await self.request('p'))['state'],'Rejected')
        self.assertFalse(any(x.startswith('POST') for x in self.calls))

    async def test_selected_file_next_previous_same_approved_source(self):
        other=self.root/'lounge'/'B Я.mp3';other.write_bytes(b'fixture')
        self.lib['tracks'].append({'path':str(other),'label':'Next','source':'lounge','ext':'.mp3'});self.lib['count']=2
        self.status.update(mode='file',playlist=None)
        for action in ('x','b'):
            self.calls.clear();result=await self.request(action)
            self.assertEqual(result['state'],'Observed')
            posts=[c for c in self.calls if c.startswith('POST')]
            self.assertEqual(posts,['POST /api/play-file?path='+quote(str(other),safe='')+' HTTP/1.1'])
            self.assertEqual(self.calls[-1],'GET /api/status HTTP/1.1')
    async def test_selected_file_nonempty_queue_refuses_without_post(self):
        self.status.update(mode='file',playlist=None,queue=['https://unapproved.invalid/a'])
        for action in ('x','b'):
            self.assertEqual((await self.request(action))['state'],'Rejected')
        self.assertFalse(any(c.startswith('POST') for c in self.calls))
    async def test_selected_file_unknown_mode_refuses(self):
        self.status.update(mode='unknown',playlist=None)
        self.assertEqual((await self.request('x'))['state'],'Rejected')
        self.assertFalse(any(c.startswith('POST') for c in self.calls))

class ClientGuards(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_protocol_cid_type_revision_and_unknown_fields(self):
        from plugin.client import BridgeEventQuery
        from plugin.wire import PocketOutcome
        query=object.__new__(BridgeEventQuery)
        e={'text':'P2|00000001|l|1234abcd,0','request_id':'f'*64}
        class Fixture:
            async def request(self,event):return self.answer
        query.client=Fixture()
        for text in ['R1|00000001|m|1,1,14','R2|00000002|l|1234abcd,0,0',
                     'R2|00000001|m|1234abcd,00000000,0,0,?,?,?;Unknown',
                     'R2|00000001|l|deadbeef,0,0','R2|00000001|l|1234abcd,0,0\n']:
            query.client.answer={'state':'Observed','request_id':e['request_id'],'text':text}
            with self.assertRaises(PocketOutcome):await query._query_p2(e,91)
        query.client.answer={'state':'Observed','request_id':e['request_id'],
            'text':'R2|00000001|l|1234abcd,0,0','extra_private_field':'IGNORED NOT EXPORTED'}
        self.assertEqual(await query._query_p2(e,91),'R2|00000001|l|1234abcd,0,0')

if __name__=='__main__':unittest.main()
