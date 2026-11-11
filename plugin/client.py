"""Bridge-side event seam; importing this module cannot open serial or a DB."""
from .ipc import IPCClient
from .wire import correlation, reply, PocketOutcome
from .protocol import validate_event


class BridgeEventQuery:
    def __init__(self, directory, key, owners, unavailable_error=RuntimeError, timeout=125):
        # Provisioned by the bridge operator, not read from any Hermes files.
        if type(key) is not bytes or len(key) != 32:
            raise ValueError('bridge requires explicitly provisioned 32-byte HMAC key')
        self.client = IPCClient(directory, key, timeout=timeout)
        self.owners = frozenset(owners)
        self.unavailable_error = unavailable_error

    async def query_event(self, message, budget):
        event = {'sender': message.sender, 'timestamp': message.timestamp, 'text': message.text,
                 'private': message.private, 'text_type': message.text_type,
                 'request_id': message.identity(), 'budget': budget}
        validate_event(event, self.owners)
        # Transport errors are ambiguous: caller's durable Running -> Unknown.
        # No reconnect, cached-response replay, model fallback or retries.
        cid = correlation(event['text']) if event['text'].startswith('P1') else None
        try:
            response = await self.client.request(event)
        except Exception:
            if cid:
                raise PocketOutcome('Unknown', reply(cid, 'u', 'Outcome unknown; no automatic retry.', budget)) from None
            raise
        if not isinstance(response, dict):
            raise RuntimeError('invalid IPC response')
        if cid:
            if response.get('state') == 'Duplicate':
                raise PocketOutcome('Unknown', reply(cid, 'u', 'Duplicate; no automatic replay.', budget))
            if response.get('request_id') != event['request_id'] or not isinstance(response.get('text'), str):
                raise PocketOutcome('Unknown', reply(cid, 'u', 'Invalid IPC outcome; no automatic retry.', budget))
            answer = response['text']
            if (not answer.startswith('R1|' + cid + '|') or len(answer.encode()) > min(budget, 91)):
                raise PocketOutcome('Unknown', reply(cid, 'u', 'Invalid IPC response; no automatic retry.', budget))
            if response.get('state') in {'Rejected', 'Unavailable', 'Busy', 'Unknown'}:
                raise PocketOutcome('Unknown' if response['state'] == 'Unknown' else 'Unavailable', answer)
            if response.get('state') not in {'ResponseReady', 'Observed', 'Stale'}:
                raise PocketOutcome('Unknown', reply(cid, 'u', 'Unexpected IPC outcome; no automatic retry.', budget))
            return answer
        if response.get('state') in {'Rejected', 'Unavailable', 'Busy'}:
            # These states explicitly mean no native model turn was dispatched;
            # the bridge's QueryUnavailable branch reports definite refusal.
            # Music Unavailable can be after a status read, but never a model turn.
            code = response.get('code')
            if not isinstance(code, str) or not code.isascii() or not code.replace('_', '').isalnum() or len(code) > 64:
                code = 'NATIVE_OPERATION_UNAVAILABLE'
            error = self.unavailable_error('Pocket operation unavailable')
            error.pocket_code = code
            raise error
        if response.get('state') not in {'ResponseReady', 'Observed', 'Stale'}:
            raise RuntimeError('Pocket outcome unknown; replay disabled')
        if response.get('request_id') != event['request_id'] or not isinstance(response.get('text'), str):
            raise RuntimeError('IPC response mismatch')
        return response['text']
