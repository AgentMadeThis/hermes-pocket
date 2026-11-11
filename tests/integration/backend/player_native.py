"""Actual candidate Bridge → HMAC IPC → installed native adapter; fake HTTP/RF only."""
import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import native_cases as base
from plugin.player import PlayerHTTP, Catalog
from plugin.player_wire import decode
from plugin.client import BridgeEventQuery
from plugin.adapter import PocketAdapter
from test_contract import event, OWNER
from integration.host.bridge.core import Bridge, Inbound, QueryUnavailable

ROOT=Path(__file__).resolve().parents[3]

class PlayerNative(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=base.NativeIntegration.asyncSetUp
    asyncTearDown=base.NativeIntegration.asyncTearDown

    async def fixture(self):
        folder=self.root/'audio';folder.mkdir()
        path=folder/'Fixture Я & #.mp3';path.write_bytes(b'fixture, NOT AUDIO')
        self.library={'ok':True,'count':1,'tracks':[{'path':str(path),'source':'lounge',
            'label':'Offline HTTP fixture Я😀 long title '*5,'ext':'.mp3'}]}
        self.status={'ok':True,'running':True,'paused':True,'volume':13.6,'track':str(path),
            'track_label':'Observed fixture Я😀','position':274.24,'duration':5030.44,
            'mode':'playlist','playlist':'lounge','queue':[],
            'audio':{'private':'MUST NOT LEAVE'},'recent':['PRIVATE HISTORY'],
            'eq':{'presets':{'flat':[],'bass':[],'soft':[]}}}
        self.calls=[];self.drop_post=False;self.drop_status_after_post=False
        async def http(reader,writer):
            try:
                line=(await reader.readuntil(b'\r\n\r\n')).split(b'\r\n')[0].decode()
                self.calls.append(line)
                if self.drop_post and line.startswith('POST'):return
                body=self.library if line.startswith('GET /api/library ') else self.status
                if line.startswith('POST'):body={'ok':True,'track_label':'FAKE POST NOT OBSERVED'}
                if self.drop_status_after_post and line.startswith('GET /api/status ') and any(x.startswith('POST') for x in self.calls):body={'ok':False}
                raw=json.dumps(body,ensure_ascii=False).encode()
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(raw)).encode()+b'\r\n\r\n'+raw)
                await writer.drain()
            finally:writer.close();await writer.wait_closed()
        self.server=await asyncio.start_server(http,'127.0.0.1',0)
        async def close():self.server.close();await self.server.wait_closed()
        self.addAsyncCleanup(close)
        playlist=self.root/'lounge.m3u';playlist.write_text(str(path)+'\n')
        self.player=PlayerHTTP(port=self.server.sockets[0].getsockname()[1],timeout=10,
            source='lounge',roots={'lounge':folder},playlists={'lounge':playlist})
        self.adapter.player=self.player;self.adapter.home_music=True
        self.cat=Catalog(OWNER,self.library,roots={'lounge':folder})

    async def test_bridge_actual_ipc_catalog_detail_select_status_volume(self):
        await self.fixture()
        query=BridgeEventQuery(self.root/'state',self.key,[OWNER],unavailable_error=QueryUnavailable)
        bridge=Bridge(self.root/'playerbridge',[OWNER],query,payload_budget=91)
        self.addCleanup(bridge.close)
        token=self.cat.revision+','+self.cat.tracks[0]['id']
        outputs=[]
        for cid,action in [('00000031','l|00000000,0'),('00000032','d|'+token),
                           ('00000033','k|'+token),('00000034','q|'),('00000035','v|35')]:
            original=event('P2|'+cid+'|'+action)
            msg=Inbound(OWNER,original['timestamp'],original['text'])
            self.assertEqual(bridge.admit(msg)[0],'Queued')
            await bridge.process_one()
            text=bridge.response(msg.identity());outputs.append(text)
            self.assertTrue(text.startswith('R2|'+cid+'|'),text)
            self.assertLessEqual(len(text.encode()),91)
            self.assertNotIn(str(self.root),text);self.assertNotIn('PRIVATE',text)
            self.assertEqual(self.adapter.journal.state(msg.identity()),'Observed')
            self.assertEqual(bridge.admit(msg)[0],'Done')
        self.assertEqual(sum(x.startswith('POST') for x in self.calls),2)
        self.assertTrue(outputs[0].endswith('…'));self.assertTrue(outputs[1].endswith('…'))
        self.assertIn(',1,1,14,274,5030;Observed fixture Я😀',outputs[2])
        self.assertEqual(self.seen,[])

    async def test_unknown_mutation_restart_and_duplicate_never_post_again(self):
        await self.fixture();self.drop_post=True
        original=event('P2|00000036|v|20')
        result=await self.client.request(original)
        self.assertEqual(result['state'],'Unknown');self.assertTrue(result['text'].startswith('R2|00000036|u|'))
        await self.adapter.disconnect()
        self.adapter=base.make_adapter(self.config);await self.adapter.connect()
        self.adapter.home_music=True;self.adapter.player=self.player
        self.assertEqual((await self.client.request(original))['state'],'Duplicate')
        self.assertEqual(sum(x.startswith('POST') for x in self.calls),1)
        self.assertEqual(self.seen,[])

    async def test_owner_digest_age_controls_reserved_not_llm(self):
        await self.fixture()
        bad=event('P2|00000037|k|'+self.cat.revision+','+self.cat.tracks[0]['id'],sender='a1'*31+'b2')
        self.assertEqual((await self.client.request(bad))['state'],'Rejected')
        bad=event('P2|00000038|q|');bad['request_id']='0'*64
        self.assertEqual((await self.client.request(bad))['state'],'Rejected')
        bad=event('P2|00000039|q|');bad['timestamp']-=400
        from plugin.protocol import identity
        bad['request_id']=identity(bad)
        self.assertEqual((await self.client.request(bad))['state'],'Rejected')
        for text in ['P2|00000040|v|+1','P2|00000041|q|\t','R2|00000042|m|bad',
                     'P1|00000043|c|P2|00000044|q|']:
            self.assertEqual((await self.client.request(event(text)))['state'],'Rejected')
        self.assertEqual(self.calls,[]);self.assertEqual(self.seen,[])

    async def test_post_status_failure_unknown_and_p2_errors_correlated_bridge(self):
        await self.fixture();self.drop_status_after_post=True
        query=BridgeEventQuery(self.root/'state',self.key,[OWNER],unavailable_error=QueryUnavailable)
        bridge=Bridge(self.root/'errorbridge',[OWNER],query,payload_budget=91);self.addCleanup(bridge.close)
        for cid,payload,letter in [('00000045','v|20','u'),('00000046','k|deadbeef,1234abcd','e')]:
            original=event('P2|'+cid+'|'+payload);msg=Inbound(OWNER,original['timestamp'],original['text'])
            bridge.admit(msg);await bridge.process_one()
            self.assertTrue(bridge.response(msg.identity()).startswith('R2|'+cid+'|'+letter+'|'))
        self.assertEqual(sum(x.startswith('POST') for x in self.calls),1)

    async def test_all_transport_actions_fixed_context_and_eq(self):
        await self.fixture()
        for index,action in enumerate(['p|','s|','x|','b|','z|','e|bass']):
            result=await self.client.request(event(f'P2|{index+80:08x}|'+action))
            self.assertEqual(result['state'],'Observed',result)
        posts=[x for x in self.calls if x.startswith('POST')]
        self.assertEqual(posts,['POST /api/resume HTTP/1.1','POST /api/pause HTTP/1.1',
            'POST /api/next HTTP/1.1','POST /api/prev HTTP/1.1','POST /api/stop HTTP/1.1',
            'POST /api/eq/preset?value=bass HTTP/1.1'])
        self.assertEqual(self.seen,[])

    async def test_generic_local_configuration_and_fail_closed_bounds(self):
        from gateway.config import PlatformConfig
        root = self.root / 'operator-music'
        config = PlatformConfig(enabled=True, extra={**self.config.extra,
            'home_music_port': 12345, 'home_library_root': str(root)})
        adapter = base.make_adapter(config)
        self.assertEqual(adapter.player.port, 12345)
        self.assertEqual(adapter.music.port, 12345)
        self.assertEqual(adapter.player.roots['lounge'], root / 'lounge')
        self.assertEqual(adapter.player.playlists['all'], root / 'all_sets.m3u')
        self.assertTrue(adapter._native_policy_ok())
        for invalid in [{'home_music_port': True}, {'home_music_port': 0},
                        {'home_music_port': 65536}, {'home_library_root': 'relative'},
                        {'home_library_root': '/srv/pocket/../escape'}]:
            # The real registry logs a constructor refusal and returns None.
            with self.subTest(invalid=invalid):
                self.assertIsNone(base.make_adapter(PlatformConfig(enabled=True,
                    extra={**self.config.extra, **invalid})))

    async def test_p2_disabled_remains_correlated_without_http_or_model(self):
        result = await self.client.request(event('P2|00000062|q|'))
        self.assertEqual(result['state'], 'Unavailable')
        self.assertEqual(result['text'], 'R2|00000062|e|HOME_MUSIC_NOT_ENABLED')
        self.assertEqual(self.seen, [])

    async def test_durable_catalog_collision_failclosed(self):
        await self.fixture()
        result=await self.client.request(event('P2|00000060|q|'));self.assertEqual(result['state'],'Observed')
        await self.adapter.disconnect()
        self.adapter=base.make_adapter(self.config);await self.adapter.connect()
        self.adapter.home_music=True;self.adapter.player=self.player
        # Force a full-digest mismatch in an existing rev8 row; no alias accepted.
        self.adapter.journal.db.execute("UPDATE player_catalog_tokens SET digest=? WHERE kind='rev'",('0'*64,))
        result=await self.client.request(event('P2|00000061|q|'))
        self.assertEqual(result['state'],'Unavailable');self.assertEqual(self.seen,[])
