"""Narrow Music Corner client, contract read from Music/sets/music_remote.py.

Never import the service (it reads UI files/imports player code). No shell, URLs,
play-file, search or speaker-connect is accepted from radio. Only bounded
integer volume and fixed operation names are accepted.
"""
import asyncio
import json

ACTIONS = frozenset({'status', 'play', 'stop', 'pause', 'resume', 'next', 'prev', 'volume'})
# Public candidate: only generic service contract names, no private library labels.
APPROVED_SOURCES = frozenset({'lounge', 'house', 'downtempo', 'work', 'nature', 'meditation', 'all'})


class MusicHTTP:
    def __init__(self, *, port=8899, timeout=10, source=None, volume_supported=True):
        if source is not None and source not in APPROVED_SOURCES:
            raise ValueError('approved fixed local HOME playlist required')
        self.port, self.timeout = port, timeout
        self.source, self.volume_supported = source, volume_supported

    async def perform(self, target, action, volume=None):
        if target != 'HOME' or action not in ACTIONS:
            return {'state': 'Rejected', 'code': 'HOME_ALLOWLIST_ONLY'}
        if action == 'volume':
            if not self.volume_supported:
                return {'state': 'Unavailable', 'code': 'HOME_VOLUME_UNSUPPORTED'}
            if type(volume) is not int or not 0 <= volume <= 100:
                return {'state': 'Rejected', 'code': 'HOME_VOLUME_BOUND'}
            endpoint = f'vol/{volume}'
        elif action == 'play':
            before = await self._request('status')
            if before['state'] != 'Observed':
                return before  # No mutation when preflight observation failed.
            if before['running']:
                if not before['paused']:
                    return before
                endpoint = 'resume'
            elif self.source is None:
                return {'state': 'Unavailable', 'code': 'HOME_FIXED_SOURCE_NOT_CONFIGURED'}
            else:
                endpoint = 'play/' + self.source
        else:
            endpoint = action
        result = await self._request(endpoint)
        if action == 'status' or result['state'] != 'Accepted':
            return result
        # Never trust mutation-returned state and NEVER repeat a mutation.
        observed = await self._request('status')
        if observed['state'] != 'Observed':
            return {'state': 'Unknown', 'code': 'HOME_POST_ACTION_STATUS_UNKNOWN'}
        return observed

    async def _request(self, action):
        writer = None
        try:
            async with asyncio.timeout(self.timeout):
                reader, writer = await asyncio.open_connection('127.0.0.1', self.port, limit=8192)
                method = 'GET' if action == 'status' else 'POST'
                writer.write((f'{method} /api/{action} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n'
                              'Content-Length: 0\r\nConnection: close\r\n\r\n').encode())
                await writer.drain()
                head = await reader.readuntil(b'\r\n\r\n')
                lines = head.decode('ascii').split('\r\n')
                fields = {}
                for line in lines[1:]:
                    if line:
                        k, v = line.split(':', 1)
                        k = k.lower()
                        if k in fields:
                            raise ValueError('duplicate header')
                        fields[k] = v.strip()
                size = int(fields['content-length'])
                if not 0 <= size <= 65536 or 'transfer-encoding' in fields:
                    raise ValueError('unsupported response')
                body = json.loads(await reader.readexactly(size))
                if (lines[0].split()[1] != '200' or not isinstance(body, dict) or body.get('ok') is not True):
                    return {'state': 'Unavailable' if action == 'status' else 'Unknown', 'code': 'HOME_SERVICE_REFUSED'}
                if action == 'status':
                    if type(body.get('running')) is not bool or type(body.get('paused')) is not bool:
                        raise ValueError('invalid status fields')
                    return {'state': 'Observed', 'code': 'HOME_STATUS',
                            'running': body['running'], 'paused': body['paused'],
                            'volume': (int(body['volume']) if type(body.get('volume')) in (int, float)
                                       and 0 <= body['volume'] <= 100 and float(body['volume']).is_integer() else None)}
                return {'state': 'Accepted', 'code': 'HOME_SERVICE_ACCEPTED_NOT_AUDIBLE_PROOF'}
        except (Exception, asyncio.CancelledError):
            # After connect/write an action may already have happened. Never retry.
            if isinstance(asyncio.current_task(), asyncio.Task) and asyncio.current_task().cancelling():
                raise
            return {'state': 'Unknown' if action != 'status' else 'Unavailable', 'code': 'HOME_RESULT_UNKNOWN'}
        finally:
            if writer:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
