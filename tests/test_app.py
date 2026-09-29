import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.ai import DemoAI


def signed(uid=123, age=0):
    values = {'user': json.dumps({'id': uid, 'first_name': 'Саша'}), 'auth_date': str(int(time.time()) - age)}
    key = hmac.new(b'WebAppData', b'test-token', hashlib.sha256).digest()
    values['hash'] = hmac.new(key, '\n'.join(f'{k}={v}' for k,v in sorted(values.items())).encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


@pytest.fixture
def client(tmp_path):
    settings = Settings(_env_file=None, database_path=str(tmp_path/'test.db'), max_bot_token='test-token', demo_mode=False, bot_mode='disabled')
    with TestClient(create_app(settings, DemoAI())) as c:
        yield c


def login(c, uid=123):
    result = c.post('/api/auth/max', json={'init_data': signed(uid)})
    assert result.status_code == 200
    return {'Authorization': 'Bearer ' + result.json()['token']}


def registered(c, uid=123):
    headers = login(c, uid)
    assert c.post('/api/register', headers=headers).status_code == 200
    return headers


def lesson(c, headers):
    response = c.post('/api/lessons', headers=headers, json={'subject': 'Математика', 'topic': 'Обыкновенные дроби'})
    assert response.status_code == 200, response.text
    return response.json()


def test_auth_rejects_forgery_expiry_duplicates_and_demo(client):
    for data in [signed().replace('123', '999'), signed(age=90000), signed() + '&auth_date=1', 'invalid']:
        assert client.post('/api/auth/max', json={'init_data': data}).status_code == 401
    assert client.post('/api/auth/demo').status_code == 404
    assert client.get('/api/me').status_code == 401


def test_registration_is_idempotent_and_diary_persists(client):
    h = registered(client)
    first = client.get('/api/me', headers=h).json()
    client.post('/api/register', headers=h)
    assert client.get('/api/me', headers=h).json()['student'] == first['student']
    diary = client.get('/api/diary', headers=h).json()['entries']
    # The synthetic timetable now covers the remainder of the school year.
    assert len(diary) >= 20
    assert len(diary) % 4 == 0
    assert all(x['assessment'] == 0 for x in diary)
    assert all(x['homework'] for x in diary)
    assert client.get('/api/diary', headers=login(client)).json()['entries'] == diary


def test_registration_required(client):
    assert client.post('/api/lessons', headers=login(client), json={'subject':'Математика','topic':'Дроби'}).status_code == 403


def test_five_blocks_cannot_skip_and_mood_becomes_good(client):
    h = registered(client)
    l = lesson(client, h)
    path = f"/api/lessons/{l['id']}"
    assert 'expected_answer' not in json.dumps(l)
    assert client.post(path+'/next', headers=h, json={'block_index':0}).status_code == 409
    answers = ['знаменатель', '2/4', '1/2', '3/7', '1/3']
    for i, answer in enumerate(answers):
        check = client.post(path+'/answer', headers=h, json={'block_index':i,'answer':'Не знаю'})
        assert check.status_code == 200, check.text
        assert check.json()['feedback']['correct'] is False
        assert client.post(path+'/next', headers=h, json={'block_index':i}).status_code == 409
        solved = client.post(path+'/answer', headers=h, json={'block_index':i,'answer':answer})
        assert solved.status_code == 200, solved.text
        assert solved.json()['feedback']['correct'] is True
        assert client.post(path+'/answer', headers=h, json={'block_index':i,'answer':answer}).status_code == 409
        moved = client.post(path+'/next', headers=h, json={'block_index':i})
        assert moved.status_code == 200
        assert client.post(path+'/next', headers=h, json={'block_index':i}).status_code == 409
        if i == 1:
            assert client.get('/api/me', headers=h).json()['mood'] == 'good'
    assert moved.json()['completed'] is True
    assert client.get('/api/me', headers=h).json()['daily']['correct'] == 5
    assert client.get(path, headers=h).json()['index'] == 5


def test_accounts_are_isolated_and_blank_answers_rejected(client):
    a, b = registered(client), registered(client, 456)
    l = lesson(client, a)
    path = f"/api/lessons/{l['id']}"
    assert client.get(path, headers=b).status_code == 404
    assert client.post(path+'/answer', headers=b, json={'block_index':0,'answer':'x'}).status_code == 404
    assert client.post(path+'/answer', headers=a, json={'block_index':0,'answer':'  '}).status_code == 422
    assert client.post('/api/lessons', headers=a, json={'subject':'Математика','topic':'Новая'}).status_code == 409


def test_provider_failure_does_not_advance(client):
    h = registered(client)
    l = lesson(client, h)
    async def broken(*args, **kwargs):
        from app.ai import AIError
        raise AIError('Недоступен')
    client.app.state.ai.grade = broken
    assert client.post(f"/api/lessons/{l['id']}/answer", headers=h, json={'block_index':0,'answer':'Ответ'}).status_code == 502
    assert client.get(f"/api/lessons/{l['id']}", headers=h).json()['index'] == 0


def test_simplifying_current_explanation_preserves_question_and_progress(client):
    h = registered(client)
    l = lesson(client, h)
    path = f"/api/lessons/{l['id']}"
    original = l['block']
    response = client.post(path + '/simplify', headers=h, json={'block_index': 0})
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated['index'] == 0
    assert updated['block']['question'] == original['question']
    assert updated['block']['simplified'] is True
    assert 'expected_answer' not in json.dumps(updated)
    assert client.post(path + '/next', headers=h, json={'block_index': 0}).status_code == 409


def test_webhook_authentication(client):
    assert client.post('/webhook/max', json={'update_type':'bot_started'}).status_code == 503
