"""Additive P2/R2 codec. P1/R1 remains entirely in wire.py."""
from dataclasses import dataclass
import re
from .wire import ID, truncate

DECIMAL = re.compile(r'0|[1-9][0-9]*', re.ASCII)

@dataclass(frozen=True)
class Request:
    correlation: str
    action: str
    payload: str = ''
    revision: str = ''
    track_id: str = ''
    value: int | None = None


def correlation(text):
    parts = text.split('|', 3)
    return parts[1] if (len(parts) == 4 and parts[0] == 'P2' and
        ID.fullmatch(parts[1]) and parts[1] != '00000000') else None


def decode(text):
    if not isinstance(text, str) or not 14 <= len(text.encode('utf-8')) <= 91:
        raise ValueError('P2 bytes')
    cid = correlation(text)
    if not cid:
        raise ValueError('P2 correlation')
    _, _, action, payload = text.split('|', 3)
    if action in {'q', 'p', 's', 'x', 'b', 'z'} and not payload:
        return Request(cid, action)
    if action in {'v', 'j'} and DECIMAL.fullmatch(payload):
        value = int(payload)
        if value <= (100 if action == 'v' else 863999):
            return Request(cid, action, payload, value=value)
    if action == 'e' and payload in {'flat', 'bass', 'soft'}:
        return Request(cid, action, payload)
    if action in {'l', 'd', 'k'}:
        parts = payload.split(',')
        if len(parts) == 2 and ID.fullmatch(parts[0]):
            rev, token = parts
            if action == 'l' and DECIMAL.fullmatch(token) and int(token) <= 999:
                offset = int(token)
                if offset % 2 == 0 and (rev != '00000000' or offset == 0):
                    return Request(cid, action, payload, revision=rev, value=offset)
            if action in {'d', 'k'} and rev != '00000000' and ID.fullmatch(token) and token != '00000000':
                return Request(cid, action, payload, revision=rev, track_id=token)
    raise ValueError('P2 action or payload')


def clean(text):
    if not isinstance(text, str) or not text:
        return 'Unknown'
    # Validate even the suffix that will be clipped. Match output-only scalar policy.
    value = truncate(text, max(3, len(text.encode('utf-8'))))
    return ''.join(' ' if c in ',;|\n' else c for c in value) or 'Unknown'


def frame(cid, kind, payload, budget=91):
    if not ID.fullmatch(cid) or cid == '00000000' or kind not in {'m','l','d','e','u'}:
        raise ValueError('R2 shape')
    head = f'R2|{cid}|{kind}|'
    if len((head + payload).encode()) > min(91, budget):
        raise ValueError('R2 budget')
    return head + payload


def failure(cid, kind, text, budget=91):
    return frame(cid, kind, truncate(clean(text), min(91, budget)-14), budget)


def list_frame(cid, rev, offset, total, tracks, budget=91):
    if not ID.fullmatch(rev) or rev == '00000000' or not 0 <= total <= 999 or not 0 <= offset <= total:
        raise ValueError('R2 list bounds')
    if offset % 2 or (total and offset >= total) or len(tracks) != min(2, total-offset):
        raise ValueError('R2 list coherence')
    prefix = f'{rev},{offset},{total}'
    remaining = min(91,budget)-14-len(prefix.encode())-sum(10 for _ in tracks)
    if tracks and remaining < 3 * len(tracks):
        raise ValueError('R2 list title budget')
    for index, track in enumerate(tracks):
        cap = remaining // (len(tracks)-index)
        title = truncate(clean(track['label']), cap)
        prefix += ';' + track['id'] + '=' + title
        remaining -= len(title.encode())
    return frame(cid,'l',prefix,budget)


def detail_frame(cid, rev, track, budget=91):
    prefix = rev + ',' + track['id'] + ';'
    return frame(cid,'d',prefix+truncate(clean(track['label']),min(91,budget)-14-len(prefix)),budget)


def state_frame(cid, rev, value, budget=91):
    number = lambda key: '?' if value.get(key) is None else str(value[key])
    prefix = (f"{rev},{value['id']},{int(value['running'])},{int(value['paused'])},"
              f"{number('volume')},{number('position')},{number('duration')};")
    return frame(cid,'m',prefix+truncate(clean(value['title']),min(91,budget)-14-len(prefix)),budget)
