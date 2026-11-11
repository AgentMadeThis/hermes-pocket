"""Authenticated-body codec; call only AFTER full outer admission/journaling."""
from dataclasses import dataclass
import re

ID = re.compile(r'[0-9a-f]{8}', re.ASCII)

@dataclass(frozen=True)
class Request:
    correlation: str
    action: str
    text: str = ''
    volume: int | None = None


def correlation(text):
    parts = text.split('|', 3)
    if len(parts) == 4 and parts[0] == 'P1' and ID.fullmatch(parts[1]) and parts[1] != '00000000':
        return parts[1]
    return None


def decode(text):
    if not isinstance(text, str) or not 14 <= len(text.encode('utf-8')) <= 91:
        raise ValueError('P1 byte bound')
    cid = correlation(text)
    if cid is None:
        raise ValueError('P1 correlation')
    _, _, action, payload = text.split('|', 3)
    if any((ord(c) < 32 and c != '\n') or ord(c) == 127 for c in payload):
        raise ValueError('P1 control character')
    if action in {'c', 'n'}:
        if not payload or len(payload.encode()) > 77:
            raise ValueError('P1 text bound')
        return Request(cid, action, payload)
    if action == 'v' and re.fullmatch(r'0|[1-9][0-9]?|100', payload, re.ASCII):
        return Request(cid, action, volume=int(payload))
    if action in {'p', 's', 'x', 'q', 'l'} and not payload:
        return Request(cid, action)
    raise ValueError('P1 action or payload')


def truncate(text, cap):
    if not isinstance(text, str) or not text or '\x00' in text or '\ufffd' in text:
        raise ValueError('invalid response')
    # Output-only normalization. Validate the original scalar sequence first so
    # forbidden NUL/replacement/surrogates cannot disappear beyond truncation.
    text.encode('utf-8')
    text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\t', ' ')
    text = ''.join(c for c in text if c == '\n' or (ord(c) >= 32 and ord(c) != 127))
    if not text:
        raise ValueError('empty normalized response')
    raw = text.encode('utf-8')
    if len(raw) <= cap:
        return text
    return raw[:cap - 3].decode('utf-8', 'ignore') + '…'


def reply(cid, state, text, budget=91):
    if not isinstance(cid, str) or not ID.fullmatch(cid) or cid == '00000000' or state not in {'o', 'e', 'u', 'm'}:
        raise ValueError('R1 shape')
    header = f'R1|{cid}|{state}|'
    cap = min(91, budget) - len(header)
    if cap < 3:
        raise ValueError('R1 budget')
    if state == 'm' and not re.fullmatch(r'[01],[01],(?:0|[1-9][0-9]?|100|\?)', text, re.ASCII):
        raise ValueError('R1 music shape')
    return header + truncate(text, cap)


def music_payload(value):
    if type(value.get('running')) is not bool or type(value.get('paused')) is not bool:
        raise ValueError('unobserved music status')
    volume = value.get('volume')
    if volume is not None and (type(volume) is not int or not 0 <= volume <= 100):
        raise ValueError('unobserved music volume')
    return f"{int(value['running'])},{int(value['paused'])},{'?' if volume is None else volume}"


class PocketOutcome(RuntimeError):
    """Correlated definite refusal or ambiguous outcome, NOT a new query."""
    def __init__(self, state, text):
        super().__init__('Pocket non-success outcome')
        self.state, self.text = state, text
