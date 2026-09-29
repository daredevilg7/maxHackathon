import json
import httpx
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.ai import RouterAI, AIError, DemoAI, SimpleExplanation
from test_app import registered


def test_old_demo_session_not_accepted_in_real_mode(tmp_path):
    path=str(tmp_path/'switch.db')
    with TestClient(create_app(Settings(_env_file=None,database_path=path,demo_mode=True))) as c:
        token=c.post('/api/auth/demo').json()['token']
    with TestClient(create_app(Settings(_env_file=None,database_path=path,demo_mode=False))) as c:
        assert c.get('/api/me',headers={'Authorization':'Bearer '+token}).status_code == 401


def test_invalid_content_length_is_bad_request(client):
    assert client.post('/api/auth/max',content='{}',headers={'content-length':'oops'}).status_code == 400


@pytest.mark.asyncio
async def test_router_payload_and_malformed_reply(monkeypatch):
    settings=Settings(_env_file=None,routerai_api_key='fake-key')
    provider=RouterAI(settings)
    valid=await DemoAI().generate('Математика','Дроби',7)
    replies=[{'blocks':valid},{'blocks':valid[:4]}]
    real_client=httpx.AsyncClient
    def respond(request):
        body=json.loads(request.content)
        assert body['model']=='inclusionai/ling-3.0-flash-vl'
        assert request.headers['Authorization']=='Bearer fake-key'
        assert 'Саша' not in request.content.decode()
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(replies.pop(0))}}]})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:real_client(transport=httpx.MockTransport(respond),**kwargs))
    assert len(await provider.generate('Математика','Дроби',7))==5
    with pytest.raises(AIError):
        await provider.generate('Математика','Дроби',7)


@pytest.mark.asyncio
async def test_router_grading_timeout_is_actionable(monkeypatch):
    real_client=httpx.AsyncClient
    def respond(request):
        raise httpx.ReadTimeout('timeout',request=request)
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:real_client(transport=httpx.MockTransport(respond),**kwargs))
    with pytest.raises(AIError,match='прогресс сохранён'):
        await RouterAI(Settings(_env_file=None,routerai_api_key='fake')).grade({},'ответ',7)


@pytest.mark.asyncio
async def test_router_grading_accepts_provider_properties_wrapper(monkeypatch):
    real_client = httpx.AsyncClient
    def respond(request):
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({
            'properties': {
                'correct': False,
                'explanation': 'Ответ пока не подходит к вопросу.',
                'addition': 'Вспомни, сколько частей было в примере.',
            }
        }, ensure_ascii=False)}}]})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    feedback = await RouterAI(Settings(_env_file=None, routerai_api_key='fake')).grade(
        {'question': 'Сколько частей?', 'expected_answer': '4'}, 'Не знаю', 7
    )
    assert feedback['correct'] is False
    assert feedback['addition'] == 'Вспомни, сколько частей было в примере.'


@pytest.mark.asyncio
async def test_router_simplification_accepts_provider_properties_wrapper(monkeypatch):
    real_client = httpx.AsyncClient
    def respond(request):
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({
            'properties': {'theory': 'Раздели оба числа на два.', 'example': '6/8 станет 3/4.'}
        }, ensure_ascii=False)}}]})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    explanation = await RouterAI(Settings(_env_file=None, routerai_api_key='fake')).request(
        'Объясни проще', {}, SimpleExplanation
    )
    assert explanation.theory == 'Раздели оба числа на два.'


def test_demo_cannot_run_bot(tmp_path):
    with pytest.raises(RuntimeError,match='DEMO_MODE'):
        create_app(Settings(_env_file=None,database_path=str(tmp_path/'x.db'),demo_mode=True,bot_mode='webhook'))


def test_webhook_secret_and_deduplication(tmp_path):
    settings=Settings(_env_file=None,database_path=str(tmp_path/'x.db'),bot_mode='webhook',max_bot_token='fake',webhook_secret='a'*32)
    # No lifespan here: verify ingress without launching external delivery.
    c=TestClient(create_app(settings))
    event={'update_type':'bot_started','timestamp':123,'user':{'user_id':123}}
    assert c.post('/webhook/max',json=event).status_code==403
    for _ in range(2):
        assert c.post('/webhook/max',json=event,headers={'X-Max-Bot-Api-Secret':'a'*32}).status_code==200
    with c.app.state.db.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0]==1


@pytest.mark.asyncio
async def test_bad_nested_event_does_not_kill_inbox(tmp_path):
    from app.db import Database
    from app.bot import process_inbox
    db=Database(str(tmp_path/'events.db'))
    db.enqueue({'update_type':'bot_started','user':None})
    db.enqueue({'update_type':'message_created','message':None})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={}))) as client:
        await process_inbox(client,Settings(_env_file=None),db)
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM inbox WHERE state='pending'").fetchone()[0]==0
