"""Password-protected schedule editor for the server operator."""

import hmac
import time
from collections import defaultdict, deque
from datetime import date
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field


COOKIE = 'luchik_admin'


class AdminLogin(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class ScheduleEntry(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    day: date
    period: int = Field(ge=1, le=12)
    subject: str = Field(min_length=1, max_length=80)
    topic: str = Field(min_length=1, max_length=200)
    homework: str = Field(max_length=1000)
    assessment: bool = False


def configure_admin(app, db, settings, static):
    attempts = defaultdict(deque)

    def enabled():
        if not settings.admin_password:
            raise HTTPException(404, 'Админ-панель не настроена.')

    def admin(request: Request):
        enabled()
        if not db.admin_session_valid(request.cookies.get(COOKIE, '')):
            raise HTTPException(401, 'Войди в админ-панель.')

    def write_request(request: Request):
        if request.headers.get('x-admin-action') != '1':
            raise HTTPException(403, 'Запрос не подтверждён.')
        origin = request.headers.get('origin')
        if origin and urlsplit(origin).netloc != request.headers.get('host'):
            raise HTTPException(403, 'Неверный источник запроса.')

    def known_student(uid):
        student = db.student(uid)
        if not student:
            raise HTTPException(404, 'Ученик не найден.')
        return student

    @app.get('/admin')
    @app.get('/admin/')
    def admin_page():
        enabled()
        return FileResponse(static / 'admin.html', headers={
            'Cache-Control': 'no-store',
            'Content-Security-Policy': "default-src 'self'; img-src 'self' data:; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        })

    @app.post('/api/admin/login')
    def login(body: AdminLogin, request: Request, response: Response):
        enabled()
        write_request(request)
        client = request.headers.get('x-real-ip') or (request.client.host if request.client else 'unknown')
        queue = attempts[client]
        now = time.monotonic()
        while queue and now - queue[0] > 600:
            queue.popleft()
        if len(queue) >= 8:
            raise HTTPException(429, 'Слишком много попыток. Попробуй через 10 минут.')
        if not hmac.compare_digest(body.password.encode(), settings.admin_password.encode()):
            queue.append(now)
            raise HTTPException(401, 'Неверный пароль.')
        queue.clear()
        token = db.create_admin_session()
        response.set_cookie(
            COOKIE, token, max_age=8 * 3600, httponly=True,
            secure=not settings.demo_mode,
            samesite='strict', path='/',
        )
        return {'ok': True}

    @app.post('/api/admin/logout')
    def logout(request: Request, response: Response, _=Depends(admin)):
        write_request(request)
        db.revoke_admin_session(request.cookies.get(COOKIE, ''))
        response.delete_cookie(COOKIE, path='/')
        return {'ok': True}

    @app.get('/api/admin/me')
    def me(_=Depends(admin)):
        return {'ok': True}

    @app.get('/api/admin/students')
    def students(q: str = '', offset: int = 0, _=Depends(admin)):
        if len(q) > 120 or offset < 0 or offset > 100000:
            raise HTTPException(422, 'Некорректный поиск.')
        rows = db.students(q.strip(), limit=101, offset=offset)
        return {'students': rows[:100], 'has_more': len(rows) > 100}

    @app.get('/api/admin/students/{uid}/schedule')
    def schedule(uid: int, _=Depends(admin)):
        return {'student': known_student(uid), 'entries': db.diary(uid), 'source': 'local'}

    @app.post('/api/admin/students/{uid}/schedule', status_code=201)
    def add_entry(uid: int, body: ScheduleEntry, request: Request, _=Depends(admin)):
        write_request(request)
        known_student(uid)
        try:
            return db.add_diary_entry(uid, body.model_dump(mode='json'))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.put('/api/admin/students/{uid}/schedule/{entry_id}')
    def update_entry(uid: int, entry_id: int, body: ScheduleEntry, request: Request, _=Depends(admin)):
        write_request(request)
        known_student(uid)
        try:
            updated = db.update_diary_entry(uid, entry_id, body.model_dump(mode='json'))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        if not updated:
            raise HTTPException(404, 'Запись расписания не найдена.')
        return updated

    @app.delete('/api/admin/students/{uid}/schedule/{entry_id}')
    def delete_entry(uid: int, entry_id: int, request: Request, _=Depends(admin)):
        write_request(request)
        known_student(uid)
        if not db.delete_diary_entry(uid, entry_id):
            raise HTTPException(404, 'Запись расписания не найдена.')
        return {'deleted': True}
