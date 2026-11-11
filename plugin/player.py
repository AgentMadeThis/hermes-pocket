"""Deterministic owner-scoped local catalog and single-attempt Music Corner P2.

No production mutation on import, no shell/model and no arbitrary URL endpoint.
Only metadata leaves this module; raw status (audio IDs/history/paths) stays local.
"""
import asyncio
import hashlib
import json
import math
from pathlib import Path
from urllib.parse import quote
from .music import APPROVED_SOURCES
from .protocol import canonical, full_key
from . import player_wire as wire

AUDIO_EXTS = frozenset({'.m4a','.mp3','.wav','.flac','.ogg','.opus','.aac','.webm'})
ROOTS = {source: Path('/srv/pocket/music') / source for source in APPROVED_SOURCES}
PLAYLISTS = {source: Path('/srv/pocket/music') / ('all_sets.m3u' if source == 'all' else source+'.m3u')
             for source in APPROVED_SOURCES}


def digest8(value):
    return hashlib.sha256(value).hexdigest()[:8]


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON field')
        result[key] = value
    return result


def safe_path(raw, root):
    if not isinstance(raw,str) or not raw or any(ord(c)<32 or ord(c)==127 for c in raw):
        raise ValueError('invalid local path')
    raw.encode('utf-8')
    p = Path(raw)
    if not p.is_absolute() or '..' in p.parts or p.suffix.lower() not in AUDIO_EXTS:
        raise ValueError('local audio required')
    root = Path(root)
    if not root.is_absolute() or root.resolve(strict=True) != root:
        raise ValueError('trusted canonical root required')
    if not p.is_relative_to(root) or p == root:
        raise ValueError('path outside source root')
    # Reject ALL symlink aliases, including same-root aliases. No prefix matching.
    for part in (p, *p.parents):
        if part.is_symlink():
            raise ValueError('symlink forbidden')
    if p.resolve(strict=True) != p or not p.is_file():
        raise ValueError('canonical regular audio required')
    return str(p)


class Catalog:
    def __init__(self, owner, body, *, roots=None):
        full_key(owner)
        roots = ROOTS if roots is None else roots
        if (not isinstance(body,dict) or body.get('ok') is not True or
            not isinstance(body.get('tracks'),list) or type(body.get('count')) is not int or
            body['count'] != len(body['tracks']) or body['count'] > 4096):
            raise ValueError('malformed library')
        tracks, ids, paths = [], set(), set()
        for item in body['tracks']:
            if not isinstance(item,dict) or not isinstance(item.get('source'),str):
                raise ValueError('malformed track')
            if item['source'] not in APPROVED_SOURCES:
                continue
            if item['source'] not in roots:
                raise ValueError('source root unavailable')
            path = safe_path(item.get('path'), roots[item['source']])
            if item.get('ext') != Path(path).suffix.lower() or not isinstance(item.get('label'),str):
                raise ValueError('invalid track metadata')
            wire.clean(item['label'])  # Validate full Unicode before any truncation.
            track_id = digest8(canonical(['pocket-player-id-v2', owner, path]))
            if track_id == '00000000' or track_id in ids or path in paths:
                raise ValueError('catalog identity collision')
            ids.add(track_id); paths.add(path)
            tracks.append({'id':track_id,'path':path,'source':item['source'],'label':item['label']})
        if len(tracks)>999:
            raise ValueError('catalog capacity')
        self.tracks = sorted(tracks,key=lambda t:t['path'])
        self.manifest = canonical(['pocket-player-rev-v2',owner,self.tracks])
        self.revision = digest8(self.manifest)
        if self.revision == '00000000':
            raise ValueError('reserved catalog revision')
        self.by_id = {t['id']:t for t in self.tracks}
        self.by_path = {t['path']:t for t in self.tracks}


def observed_number(value, maximum, *, nearest=False):
    if type(value) not in (int,float) or not math.isfinite(value) or not 0 <= value <= maximum:
        return None
    return int(math.floor(value+.5)) if nearest else int(value)


class PlayerHTTP:
    def __init__(self, *, port=8899, timeout=10, source=None, roots=None, playlists=None):
        if source is not None and source not in APPROVED_SOURCES:
            raise ValueError('fixed approved source required')
        self.port, self.timeout, self.source = port, timeout, source
        self.roots = ROOTS if roots is None else roots
        self.playlists = PLAYLISTS if playlists is None else playlists
        self._revisions = {}
        self.journal = None

    def bind_journal(self, journal):
        self.journal = journal
        journal.db.execute('CREATE TABLE IF NOT EXISTS player_catalog_tokens '
            '(owner TEXT NOT NULL, kind TEXT NOT NULL, token TEXT NOT NULL, digest TEXT NOT NULL, '
            'PRIMARY KEY(owner,kind,token))')

    def register_tokens(self, owner, cat):
        if self.journal is None:
            return  # Standalone HTTP fixtures only; adapter always binds durable journal.
        tokens = [('rev', cat.revision, hashlib.sha256(cat.manifest).hexdigest())]
        tokens += [('id', t['id'], hashlib.sha256(canonical(['pocket-player-id-v2',owner,t['path']])).hexdigest())
                   for t in cat.tracks]
        db = self.journal.db
        db.execute('BEGIN IMMEDIATE')
        try:
            count = db.execute('SELECT count(*) FROM player_catalog_tokens').fetchone()[0]
            for kind, token, digest in tokens:
                row = db.execute('SELECT digest FROM player_catalog_tokens WHERE owner=? AND kind=? AND token=?',
                    (owner,kind,token)).fetchone()
                if row and row[0] != digest:
                    raise ValueError('durable catalog token collision')
                if not row:
                    if count >= 16384:raise ValueError('durable catalog token capacity')
                    db.execute('INSERT INTO player_catalog_tokens VALUES(?,?,?,?)',(owner,kind,token,digest))
                    count += 1
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise

    async def _request(self, endpoint, *, mutate=False):
        writer = None
        try:
            async with asyncio.timeout(self.timeout):
                reader, writer = await asyncio.open_connection('127.0.0.1',self.port,limit=8192)
                method = 'POST' if mutate else 'GET'
                writer.write((f'{method} /api/{endpoint} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n'
                    'Content-Length: 0\r\nConnection: close\r\n\r\n').encode('ascii'))
                await writer.drain()
                head = (await reader.readuntil(b'\r\n\r\n')).decode('ascii').split('\r\n')
                fields = {}
                for line in head[1:]:
                    if line:
                        key,value = line.split(':',1); key=key.lower()
                        if key in fields:raise ValueError('duplicate HTTP header')
                        fields[key]=value.strip()
                size=int(fields['content-length'])
                if not 0 <= size <= 1048576 or 'transfer-encoding' in fields:
                    raise ValueError('HTTP bound')
                body=json.loads(await reader.readexactly(size),object_pairs_hook=strict_object,
                    parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
                if head[0].split()[1]!='200' or not isinstance(body,dict) or body.get('ok') is not True:
                    raise ValueError('service refusal')
                return body
        finally:
            if writer:
                writer.close()
                try:await writer.wait_closed()
                except OSError:pass

    async def catalog(self,owner):
        cat=Catalog(owner,await self._request('library'),roots=self.roots)
        key=(owner,cat.revision)
        manifest_digest = hashlib.sha256(cat.manifest).hexdigest()
        if key in self._revisions and self._revisions[key] != manifest_digest:
            raise ValueError('revision collision')
        # Bounded collision ledger: never evict a seen revision and silently accept reuse.
        if key not in self._revisions and len(self._revisions)>=4096:
            raise ValueError('revision ledger capacity')
        self.register_tokens(owner,cat)
        self._revisions[key]=manifest_digest
        return cat

    def context(self,status,cat):
        track=cat.by_path.get(status.get('track'))
        if not track:
            raise ValueError('unapproved current context')
        return track

    def playlist_safe(self,status,cat):
        source=status.get('playlist')
        if source not in APPROVED_SOURCES or status.get('mode')!='playlist' or status.get('queue')!=[]:
            raise ValueError('unapproved playlist or nonempty queue')
        file=self.playlists.get(source)
        if file is None or file.is_symlink():raise ValueError('playlist unavailable')
        lines=file.read_text(encoding='utf-8').splitlines()
        paths=[line.strip() for line in lines if line.strip() and not line.strip().startswith('#')]
        if not paths or len(paths)>999:raise ValueError('playlist capacity')
        for path in paths:
            if path not in cat.by_path:raise ValueError('playlist outside approved catalog')
            item=cat.by_path[path]
            safe_path(path,self.roots[item['source']])

    def observe(self,status,cat):
        if type(status.get('running')) is not bool or type(status.get('paused')) is not bool:
            raise ValueError('unobserved status')
        track=cat.by_path.get(status.get('track'))
        title=status.get('track_label')
        if not isinstance(title,str) or not title:title='Unknown'
        position=observed_number(status.get('position'),863999)
        duration=observed_number(status.get('duration'),863999)
        if position is not None and duration is not None and position>duration:
            position=None  # Invalid pair is unknown, never manufacture a clamped value.
        return {'id':track['id'] if track else '00000000', 'running':status['running'],
            'paused':status['paused'],'volume':observed_number(status.get('volume'),100,nearest=True),
            'position':position, 'duration':duration,'title':wire.clean(title)}

    async def perform(self,owner,request,budget=91):
        cid=request.correlation
        def error(state,code):
            return {'state':state,'code':code,'text':wire.failure(cid,'u' if state=='Unknown' else 'e',code,budget)}
        if budget < 74:
            return error('Unavailable','PLAYER_REPLY_BUDGET')
        try:
            cat=await self.catalog(owner)
            if request.action in {'l','d','k'}:
                if request.revision!='00000000' and request.revision!=cat.revision:
                    return error('Rejected','CATALOG_STALE_REFRESH')
                if request.action=='l':
                    offset=request.value
                    if offset>len(cat.tracks) or (cat.tracks and offset>=len(cat.tracks)):
                        return error('Rejected','CATALOG_OFFSET')
                    return {'state':'Observed','text':wire.list_frame(cid,cat.revision,offset,len(cat.tracks),cat.tracks[offset:offset+2],budget)}
                track=cat.by_id.get(request.track_id)
                if not track:return error('Rejected','CATALOG_UNKNOWN_ID')
                if request.action=='d':
                    return {'state':'Observed','text':wire.detail_frame(cid,cat.revision,track,budget)}
            status=await self._request('status')
            observed=self.observe(status,cat)
        except (Exception,):
            return error('Unavailable','PLAYER_OBSERVATION_UNAVAILABLE')
        action=request.action
        if action=='q':
            return {'state':'Observed','observed':observed,'text':wire.state_frame(cid,cat.revision,observed,budget)}
        try:
            if action=='k':
                path=safe_path(track['path'],self.roots[track['source']])
                endpoint='play-file?path='+quote(path,safe='')
            elif action=='v':endpoint='vol/'+str(request.value)
            elif action=='j':
                self.context(status,cat)
                duration=status.get('duration')
                if observed_number(duration,863999) is None or duration<=0:
                    return error('Unavailable','PLAYER_SEEK_DURATION_UNKNOWN')
                # Controller clamps to duration-1; reject rather than silently clamp.
                if request.value>max(0,duration-1):return error('Rejected','PLAYER_SEEK_BOUND')
                endpoint='seek/'+str(request.value)
            elif action=='e':
                if status['running']:self.context(status,cat)
                eq=status.get('eq')
                if not isinstance(eq,dict) or request.payload not in eq.get('presets',{}):
                    return error('Unavailable','PLAYER_EQ_UNAVAILABLE')
                endpoint='eq/preset?value='+request.payload
            elif action=='p':
                if status['running']:
                    self.context(status,cat)
                    if not status['paused']:
                        return {'state':'Observed','observed':observed,'text':wire.state_frame(cid,cat.revision,observed,budget)}
                    endpoint='resume'
                else:
                    if self.source is None:return error('Unavailable','PLAYER_FIXED_SOURCE_REQUIRED')
                    self.playlist_safe({**status,'playlist':self.source,'mode':'playlist'},cat)
                    endpoint='play/'+self.source
            else:
                current=self.context(status,cat)
                if action in {'x','b'} and status.get('mode')=='file':
                    # Catalog selection uses play-file, not a controller playlist.
                    # Derive the adjacent canonical entry only within that same
                    # approved source; never follow an injected queue or URL.
                    if status.get('queue')!=[]:raise ValueError('nonempty file queue')
                    siblings=[item for item in cat.tracks if item['source']==current['source']]
                    index=next(i for i,item in enumerate(siblings) if item['id']==current['id'])
                    target=siblings[(index+(1 if action=='x' else -1))%len(siblings)]
                    path=safe_path(target['path'],self.roots[target['source']])
                    endpoint='play-file?path='+quote(path,safe='')
                else:
                    if action in {'x','b'}:self.playlist_safe(status,cat)
                    endpoint={'s':'pause','x':'next','b':'prev','z':'stop'}[action]
        except (Exception,):
            return error('Rejected','PLAYER_CONTEXT_REFUSED')
        try:
            # At most ONE mutation attempt. Ambiguous errors do NOT repeat it.
            await self._request(endpoint,mutate=True)
            fresh=await self._request('status')
            observed=self.observe(fresh,cat)
            text=wire.state_frame(cid,cat.revision,observed,budget)
        except (Exception,):
            return error('Unknown','PLAYER_OUTCOME_UNKNOWN_NO_RETRY')
        return {'state':'Observed','observed':observed,'text':text}
