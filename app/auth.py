import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


def validate_init_data(raw: str, token: str) -> dict:
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
        if not token or len(pairs) != len(dict(pairs)):
            raise ValueError()
        params = dict(pairs)
        signature = params.pop('hash')
        key = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
        payload = '\n'.join(f'{k}={v}' for k, v in sorted(params.items()))
        expected = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        age = time.time() - int(params['auth_date'])
        if not -30 <= age <= 3600:
            raise ValueError()
        user = json.loads(params['user'])
        if type(user.get('id')) is not int or user['id'] <= 0:
            raise ValueError()
        name = ' '.join(str(user.get(k) or '') for k in ('first_name', 'last_name')).strip()
        return {'id': user['id'], 'name': (name or user.get('username') or 'Ученик')[:120]}
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError('Открой приложение заново через MAX.') from None
