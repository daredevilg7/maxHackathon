import argparse
import asyncio
import secrets
from .bot import max_client
from .config import Settings


async def run(command):
    s = Settings()
    if command == 'secret':
        print(secrets.token_urlsafe(32))
        return
    if not s.max_bot_token:
        raise SystemExit('Заполните MAX_BOT_TOKEN в .env')
    async with max_client(s) as client:
        if command == 'check':
            response = await client.get('/me')
        else:
            if not s.public_base_url.startswith('https://') or len(s.webhook_secret)<24:
                raise SystemExit('Нужны PUBLIC_BASE_URL=https://ваш-домен и WEBHOOK_SECRET длиной от 24 символов')
            response = await client.post('/subscriptions',json={'url':s.public_base_url.rstrip('/')+'/webhook/max','update_types':['bot_started','message_created'],'secret':s.webhook_secret})
        if response.is_error:
            raise SystemExit(f'MAX вернул HTTP {response.status_code}. Проверьте ключ, права и HTTPS.')
        data = response.json()
        if command == 'check':
            print('Подключено:',data.get('name'), '| username:',data.get('username'))
        else:
            print('Webhook подключён:',s.public_base_url.rstrip('/')+'/webhook/max')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command',choices=['check','webhook','secret'])
    asyncio.run(run(parser.parse_args().command))
