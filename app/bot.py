import asyncio
import json
import logging
import ssl
import time
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo
import httpx
from .ai import AIError, RouterAI, SUBJECTS, SUBJECT_EXAMPLES, unrelated_topic_message

log = logging.getLogger(__name__)


def max_client(settings):
    context = ssl.create_default_context()
    if settings.max_ca_bundle:
        bundle_path = Path(settings.max_ca_bundle)
        if not bundle_path.exists():
            for candidate in [
                Path.cwd() / settings.max_ca_bundle.lstrip('/'),
                Path(__file__).resolve().parent.parent / settings.max_ca_bundle.lstrip('/'),
            ]:
                if candidate.exists():
                    bundle_path = candidate
                    break
        if bundle_path.exists():
            context.load_verify_locations(str(bundle_path))
        else:
            log.warning('MAX CA bundle not found at %s', settings.max_ca_bundle)
    return httpx.AsyncClient(base_url=settings.max_api_url, headers={'Authorization':settings.max_bot_token}, timeout=40, verify=context)


def keyboard(rows):
    return {'type': 'inline_keyboard', 'payload': {'buttons': rows}}


def message_button(text):
    return {'type': 'message', 'text': text}


def menu_keyboard(settings):
    rows = [
        [message_button('Профиль'), message_button('Расписание')],
        [message_button('Выбрать тему')],
    ]
    if settings.max_bot_username:
        rows.append([{'type': 'link', 'text': 'Открыть мини-приложение',
                      'url': f'https://max.ru/{settings.max_bot_username.lstrip("@")}?startapp'}])
    return keyboard(rows)


async def send_message(client, uid, text, attachments=None):
    payload = {'text': text}
    if attachments:
        payload['attachments'] = attachments
    response = await client.post('/messages', params={'user_id': uid}, json=payload)
    response.raise_for_status()
    await asyncio.sleep(0.55)
    return response.json()


def format_week(db, uid):
    first = datetime.now(ZoneInfo('Asia/Yekaterinburg')).date()
    last = first + timedelta(days=6)
    rows = db.diary_between(uid, first.isoformat(), last.isoformat())
    by_day = {}
    for row in rows:
        by_day.setdefault(row['day'], []).append(row)
    weekdays = ('пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс')
    lines = ['Расписание на ближайшие 7 дней', '']
    for offset in range(7):
        day = first + timedelta(days=offset)
        entries = by_day.get(day.isoformat(), [])
        lines.append(f'{day:%d.%m} · {weekdays[day.weekday()]}')
        if not entries:
            lines.append('Занятий пока нет.')
        for entry in entries:
            label = ' · проверочная' if entry['assessment'] else ''
            lines.append(f'{entry["period"]}. {entry["subject"]}{label} — {entry["topic"]}')
            if entry['homework']:
                lines.append(f'   Задание: {entry["homework"][:140]}')
        lines.append('')
    messages = []
    current = ''
    for line in lines:
        if len(current) + len(line) + 1 > 3500:
            messages.append(current.rstrip())
            current = 'Расписание (продолжение)\n'
        current += line + '\n'
    if current.strip():
        messages.append(current.rstrip())
    return messages


async def send_preparation_card(client, settings, notification, text, image_name):
    if not settings.max_bot_username or not settings.public_base_url:
        raise ValueError('Для подготовки нужны MAX_BOT_USERNAME и PUBLIC_BASE_URL.')
    link = f'https://max.ru/{settings.max_bot_username.lstrip("@")}?startapp=prep_{notification["token"]}'
    image_url = settings.public_base_url.rstrip('/') + '/static/' + image_name
    return await send_message(client, notification['user_id'], text, [
        {'type': 'image', 'payload': {'url': image_url}},
        keyboard([[{'type': 'link', 'text': 'Начать урок', 'url': link}],
                  [message_button('Меню')]]),
    ])


async def handle_event(client, settings, db, event, ai=None):
    if not isinstance(event, dict):
        return
    kind = event.get('update_type')
    if kind == 'bot_started':
        user = event.get('user', {})
        incoming = '/start'
    elif kind == 'message_created':
        message = event.get('message', {})
        if not isinstance(message, dict):
            return
        recipient = message.get('recipient')
        if not isinstance(recipient, dict) or recipient.get('chat_type') != 'dialog':
            return
        user = message.get('sender', {})
        incoming = (message.get('body') or {}).get('text')
    else:
        return
    if not isinstance(user, dict) or user.get('is_bot') or type(user.get('user_id')) is not int or user['user_id'] <= 0:
        return
    uid = user['user_id']
    account = db.student(uid)
    if not account:
        account = db.register({'id': uid, 'name': (user.get('name') or user.get('first_name') or 'Ученик')[:120]})
    if not isinstance(incoming, str):
        return
    incoming = incoming.strip()
    command = incoming.casefold()

    if command in ('/start', '/menu', 'меню', 'отмена'):
        db.clear_bot_dialog(uid)
        greeting = 'Привет! Я Лучик. Здесь можно посмотреть успехи и расписание или выбрать тему для подготовки.'
        await send_message(client, uid, greeting, [menu_keyboard(settings)])
        return

    if command in ('профиль', '/profile'):
        db.clear_bot_dialog(uid)
        completed = db.completed_lessons(uid)
        today = datetime.now(ZoneInfo('Asia/Yekaterinburg')).date().isoformat()
        solved = db.solved_today(uid, today)
        lesson = db.lesson(uid)
        current = (f'\nСейчас изучаешь: {lesson["subject"]} — {lesson["topic"]}.'
                   if lesson and lesson['idx'] < 5 else '')
        text = (f'Профиль: {account["name"]}\n'
                f'Класс: {account["grade"]}\n'
                f'Пройдено занятий: {completed}\n'
                f'Верных ответов сегодня: {solved} из 5. Цель дня — 2.' + current)
        await send_message(client, uid, text, [menu_keyboard(settings)])
        return

    if command in ('расписание', '/schedule'):
        db.clear_bot_dialog(uid)
        for text in format_week(db, uid):
            await send_message(client, uid, text)
        await send_message(client, uid, 'Что посмотрим дальше?', [menu_keyboard(settings)])
        return

    if command in ('выбрать тему', '/topic'):
        db.set_bot_dialog(uid, 'subject')
        rows = [[message_button(subject) for subject in SUBJECTS[index:index + 3]]
                for index in range(0, len(SUBJECTS), 3)]
        rows.append([message_button('Отмена')])
        await send_message(client, uid, 'Выбери предмет для подготовки:', [keyboard(rows)])
        return

    dialog = db.bot_dialog(uid)
    if (dialog and dialog['stage'] == 'subject') or incoming in SUBJECTS:
        if incoming not in SUBJECTS:
            await send_message(client, uid, 'Такого предмета в списке нет. Нажми кнопку с предметом или напиши «Отмена».')
            return
        db.set_bot_dialog(uid, 'topic', incoming)
        await send_message(client, uid,
                           f'{incoming}. Напиши тему, которую хочешь разобрать. '
                           f'Например: «{SUBJECT_EXAMPLES[incoming]}».',
                           [keyboard([[message_button('Отмена')]])])
        return

    if dialog and dialog['stage'] == 'topic':
        if len(incoming) < 2 or len(incoming) > 200:
            await send_message(client, uid, 'Название темы должно быть от 2 до 200 символов. Напиши его ещё раз.')
            return
        subject = dialog['subject']
        if subject not in SUBJECTS:
            db.clear_bot_dialog(uid)
            await send_message(client, uid, 'Выбери предмет ещё раз.', [menu_keyboard(settings)])
            return
        try:
            relevant = await (ai or RouterAI(settings)).validate_topic(subject, incoming, account['grade'])
        except AIError:
            await send_message(client, uid,
                               'Не удалось проверить тему сейчас. Напиши её ещё раз чуть позже или выбери другую.',
                               [keyboard([[message_button('Отмена')]])])
            return
        if not relevant:
            await send_message(client, uid, unrelated_topic_message(subject),
                               [keyboard([[message_button('Отмена')]])])
            return
        notification = db.queue_notification(f'bot-choice:{uid}:{uuid4().hex}', uid,
                                             subject, incoming, draft=True)
        await send_preparation_card(client, settings, notification,
                                    f'Тема выбрана: {subject} — {incoming}. Нажми «Начать урок», чтобы разобрать её шаг за шагом.',
                                    'new-lesson.jpg')
        db.notification_result(notification['key'], sent=True)
        db.clear_bot_dialog(uid)
        return

    await send_message(client, uid, 'Выбери действие:', [menu_keyboard(settings)])


async def process_inbox(client, settings, db, ai=None):
    with db.connect() as c:
        pending = c.execute("SELECT * FROM inbox WHERE state='pending' AND retry_at<=? ORDER BY rowid LIMIT 10",(time.time(),)).fetchall()
    for row in pending:
        try:
            await handle_event(client,settings,db,json.loads(row['payload']),ai)
        except Exception:
            attempts = row['attempts']+1
            with db.connect() as c:
                c.execute('UPDATE inbox SET attempts=?,retry_at=?,state=? WHERE id=?',(attempts,time.time()+min(300,2**attempts),'failed' if attempts >= 8 else 'pending',row['id']))
            log.exception('MAX event delivery failed; attempt=%s',attempts)
        else:
            with db.connect() as c:
                c.execute("UPDATE inbox SET state='done' WHERE id=?",(row['id'],))


async def send_preparation_notification(client, settings, notification):
    if not settings.max_bot_username or not settings.public_base_url:
        raise ValueError('Для напоминаний нужны MAX_BOT_USERNAME и PUBLIC_BASE_URL.')
    link = f'https://max.ru/{settings.max_bot_username.lstrip("@")}?startapp=prep_{notification["token"]}'
    image_url = settings.public_base_url.rstrip('/') + '/static/assessment-reminder.png'
    if notification['key'].startswith('preview:'):
        text = (f'Проверяем напоминания от Лучика. Попробуй подготовиться к теме '
                f'«{notification["topic"]}» по предмету «{notification["subject"]}».')
    else:
        text = (f'Через два дня проверочная по предмету «{notification["subject"]}». '
                f'Тема: {notification["topic"]}. Давай разберём её заранее — короткими шагами и с понятными примерами.')
    payload = {
        'text': text,
        'attachments': [
            {'type': 'image', 'payload': {'url': image_url}},
            {'type': 'inline_keyboard', 'payload': {'buttons': [[
                {'type': 'link', 'text': 'Начать подготовку', 'url': link}
            ]]}},
        ],
    }
    result = await client.post('/messages', params={'user_id': notification['user_id']}, json=payload)
    result.raise_for_status()
    return result.json()


async def process_notifications(client, settings, db):
    now = datetime.now(ZoneInfo('Asia/Yekaterinburg'))
    if now.hour >= 9:
        db.queue_due_assessments(now.date().isoformat())
    for notification in db.pending_notifications():
        if notification['schedule_day'] and notification['schedule_day'] != (now.date() + timedelta(days=2)).isoformat():
            db.notification_result(notification['key'], skipped=True)
            continue
        if not db.notification_current(notification):
            db.notification_result(notification['key'], skipped=True)
            continue
        try:
            await send_preparation_notification(client, settings, notification)
        except (httpx.HTTPError, ValueError) as exc:
            db.notification_result(notification['key'])
            log.warning('MAX preparation reminder failed for %s: %s', notification['key'], exc)
        else:
            db.notification_result(notification['key'], sent=True)
            await asyncio.sleep(0.55)


async def run_bot(settings,db,ai=None):
    ai = ai or RouterAI(settings)
    async with max_client(settings) as client:
        next_reminder_check = 0
        while True:
            try:
                if settings.bot_mode == 'polling':
                    with db.connect() as c:
                        marker = c.execute("SELECT value FROM meta WHERE key='marker'").fetchone()
                    params = {'timeout':5,'types':'bot_started,message_created'}
                    if marker:
                        params['marker'] = marker['value']
                    response = await client.get('/updates',params=params)
                    response.raise_for_status()
                    data = response.json()
                    for event in data.get('updates',[]):
                        db.enqueue(event)
                    if data.get('marker') is not None:
                        with db.connect() as c:
                            c.execute("INSERT OR REPLACE INTO meta VALUES('marker',?)",(str(data['marker']),))
                await process_inbox(client,settings,db,ai)
                if time.monotonic() >= next_reminder_check:
                    await process_notifications(client,settings,db)
                    next_reminder_check = time.monotonic() + 60
            except (httpx.HTTPError,ValueError,KeyError):
                log.warning('MAX connection unavailable; retrying in 5 seconds')
                await asyncio.sleep(5)
            await asyncio.sleep(1)
