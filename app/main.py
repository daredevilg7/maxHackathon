import asyncio
import contextlib
import hmac
import logging
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .ai import AIError, DemoAI, RouterAI, SUBJECTS, subjects_for_grade, SimpleExplanation, QuizContent, unrelated_topic_message
from .auth import validate_init_data
from .config import Settings
from .db import Database, public_lesson, public_quiz

logger = logging.getLogger(__name__)


class Login(BaseModel):
    init_data: str = Field(min_length=1, max_length=16000)


class Topic(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    subject: str = Field(min_length=1,max_length=80)
    topic: str = Field(min_length=2,max_length=200)


class Step(BaseModel):
    block_index: int = Field(ge=0,le=4)


class Answer(Step):
    model_config = ConfigDict(str_strip_whitespace=True)
    answer: str = Field(min_length=1,max_length=3000)


class PreparationLink(BaseModel):
    token: str = Field(min_length=20, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')


class QuizStep(BaseModel):
    index: int = Field(ge=0, le=4)


class QuizAnswer(QuizStep):
    choice_index: int | None = Field(default=None, ge=0, le=3)
    matching_order: list[int] | None = None


def create_app(settings=None, ai=None):
    settings = settings or Settings()
    db = Database(settings.database_path)
    if settings.demo_mode and settings.bot_mode != 'disabled':
        raise RuntimeError('DEMO_MODE требует BOT_MODE=disabled. Для MAX выключите деморежим.')
    if settings.bot_mode != 'disabled' and not settings.max_bot_token:
        raise RuntimeError('Для запуска бота нужен MAX_BOT_TOKEN.')
    if settings.bot_mode == 'webhook' and len(settings.webhook_secret) < 24:
        raise RuntimeError('WEBHOOK_SECRET должен содержать минимум 24 символа.')
    if settings.admin_password and len(settings.admin_password) < 16:
        raise RuntimeError('ADMIN_PASSWORD должен содержать минимум 16 символов.')
    locks = defaultdict(asyncio.Lock)
    budgets = defaultdict(deque)
    generation_tasks = set()
    generation_tasks_by_id = {}

    @asynccontextmanager
    async def lifespan(app):
        from .bot import run_bot
        added = db.extend_school_schedules()
        if added:
            logger.info('Продлено расписание: %s записей', added)
        db.fail_stale_jobs()
        task = asyncio.create_task(run_bot(settings,db,app.state.ai)) if settings.bot_mode != 'disabled' else None
        yield
        for pending in generation_tasks:
            pending.cancel()
        if generation_tasks:
            await asyncio.gather(*generation_tasks, return_exceptions=True)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title='Учёба с Лучиком API', lifespan=lifespan)
    app.state.ai = ai or (DemoAI() if settings.demo_mode else RouterAI(settings))
    app.state.db = db
    static = Path(__file__).parent/'static'
    app.mount('/static', StaticFiles(directory=static), name='static')
    from .admin import configure_admin
    configure_admin(app, db, settings, static)

    @app.middleware('http')
    async def response_headers(request, call_next):
        try:
            length = int(request.headers.get('content-length','0') or 0)
            if length < 0:
                raise ValueError()
        except ValueError:
            return JSONResponse({'detail':'Некорректная длина запроса.'}, status_code=400)
        if length > 65536:
            return JSONResponse({'detail':'Слишком большой запрос.'}, status_code=413)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(AIError)
    async def ai_error(request, exc):
        return JSONResponse({'detail':str(exc)}, status_code=502)

    def identity(authorization: Annotated[str | None, Header()] = None):
        token = authorization[7:] if authorization and authorization.startswith('Bearer ') else ''
        user = db.identity(token) if token else None
        if not user or (user['id'] < 0 and not settings.demo_mode):
            raise HTTPException(401,'Войди через MAX заново.')
        return user

    def student(user=Depends(identity)):
        s = db.student(user['id'])
        if not s:
            raise HTTPException(403,'Сначала создай учебный аккаунт.')
        return s

    def owned(uid,lid):
        l = db.lesson(uid,lid)
        if not l:
            raise HTTPException(404,'Урок не найден.')
        return l

    def check_step(l, index):
        if l['idx'] == 5 or l['idx'] != index:
            raise HTTPException(409,'Этот шаг уже изменился. Обнови страницу.')

    def limit(uid):
        queue = budgets[uid]
        now = time.monotonic()
        while queue and now-queue[0] > 60:
            queue.popleft()
        if len(queue) >= 20:
            raise HTTPException(429,'Слишком много запросов. Подожди минуту.')
        queue.append(now)

    @app.get('/')
    def index():
        return FileResponse(static/'index.html', headers={'Cache-Control': 'no-store'})

    @app.get('/health')
    def health():
        with db.connect() as c:
            c.execute('SELECT 1')
        return {'status':'ok'}

    @app.get('/api/config')
    def config():
        return {'demo_mode':settings.demo_mode,'subjects':SUBJECTS}

    @app.post('/api/auth/max')
    def max_auth(body: Login):
        try:
            user = validate_init_data(body.init_data, settings.max_bot_token)
        except ValueError as exc:
            raise HTTPException(401,str(exc)) from None
        return {'token':db.session(user)}

    @app.post('/api/auth/demo')
    def demo_auth():
        if not settings.demo_mode:
            raise HTTPException(404)
        return {'token':db.session({'id':-1,'name':'Саша'})}

    @app.post('/api/register')
    def register(user=Depends(identity)):
        return {'student':db.register(user)}

    @app.get('/api/me')
    def me(user=Depends(identity)):
        s = db.student(user['id'])
        l = db.lesson(user['id']) if s else None
        today = datetime.now(ZoneInfo('Asia/Yekaterinburg')).date().isoformat()
        correct = db.solved_today(user['id'], today) if s else 0
        activity = db.activity_summary(user['id'], today) if s else {'streak': 0, 'recent_days': []}
        daily = {
            'date': today,
            'correct': correct,
            'target': 2,
            'total': 5,
            'mood': 'good' if correct >= 2 else 'focused',
        }
        return {
            'name': user['name'],
            'student': s,
            'subjects': subjects_for_grade(s['grade']) if s else [],
            'lesson': public_lesson(l),
            'mood': daily['mood'] if s else 'ready',
            'daily': daily,
            'activity': activity,
            'generation_job': ({'id': job['id'], 'subject': job['subject'], 'topic': job['topic']}
                               if (job := db.pending_job(user['id'])) else None),
        }

    @app.get('/api/diary')
    def diary(s=Depends(student)):
        return {'mock':True,'entries':db.diary(s['id'])}

    @app.get('/api/history')
    def history(s=Depends(student)):
        return {'topics': db.completed_history(s['id'])}

    async def generate_quiz_job(job):
        try:
            lesson = db.lesson(job['user_id'], job['lesson_id'])
            if not lesson or lesson['idx'] != 5:
                db.finish_quiz_job(job['id'], error='Урок больше не доступен для теста.')
                return
            questions = await app.state.ai.generate_quiz(lesson, db.student(job['user_id'])['grade'])
            validated = QuizContent(questions=questions)
            async with locks[job['user_id']]:
                current = db.quiz_job(job['user_id'], job['id'])
                if current and current['status'] == 'pending':
                    saved = db.save_quiz(job['user_id'], job['lesson_id'],
                                         [question.model_dump() for question in validated.questions])
                    db.finish_quiz_job(job['id'], quiz_id=saved['id'])
        except asyncio.CancelledError:
            db.finish_quiz_job(job['id'], error='Подготовка теста прервана.')
            raise
        except AIError as exc:
            db.finish_quiz_job(job['id'], error=str(exc))
        except Exception:
            logger.exception('Не удалось подготовить тест')
            db.finish_quiz_job(job['id'], error='Не удалось подготовить тест. Попробуй ещё раз.')

    @app.post('/api/history/{lesson_id}/quiz-jobs', status_code=202)
    async def start_quiz_job(lesson_id: str, s=Depends(student)):
        async with locks[s['id']]:
            source = db.lesson(s['id'], lesson_id)
            if not source or source['idx'] != 5:
                raise HTTPException(404, 'Завершённая тема не найдена.')
            active = db.active_quiz(s['id'], lesson_id)
            if active:
                return {'status': 'ready', 'quiz': public_quiz(active)}
            pending = db.pending_quiz_job(s['id'], lesson_id)
            if pending:
                return {'status': 'pending', 'id': pending['id']}
            limit(s['id'])
            job = db.create_quiz_job(s['id'], lesson_id)
            task = asyncio.create_task(generate_quiz_job(job))
            generation_tasks.add(task)
            task.add_done_callback(generation_tasks.discard)
            return {'status': 'pending', 'id': job['id']}

    @app.get('/api/quiz-jobs/{job_id}')
    def quiz_job_status(job_id: str, s=Depends(student)):
        job = db.quiz_job(s['id'], job_id)
        if not job:
            raise HTTPException(404, 'Подготовка теста не найдена.')
        result = {'id': job['id'], 'status': job['status']}
        if job['status'] == 'ready':
            result['quiz'] = public_quiz(db.quiz(s['id'], job['quiz_id']))
        elif job['status'] == 'error':
            result['error'] = job['error'] or 'Не удалось подготовить тест.'
        return result

    @app.get('/api/quizzes/{quiz_id}')
    def get_quiz(quiz_id: str, s=Depends(student)):
        quiz = db.quiz(s['id'], quiz_id)
        if not quiz:
            raise HTTPException(404, 'Тест не найден.')
        return public_quiz(quiz)

    @app.post('/api/quizzes/{quiz_id}/answer')
    async def answer_quiz(quiz_id: str, body: QuizAnswer, s=Depends(student)):
        limit(s['id'])
        async with locks[s['id']]:
            quiz = db.quiz(s['id'], quiz_id)
            if not quiz:
                raise HTTPException(404, 'Тест не найден.')
            if quiz['idx'] != body.index or quiz['idx'] == 5:
                raise HTTPException(409, 'Этот вопрос уже изменился.')
            question = quiz['questions'][body.index]
            if question['kind'] == 'choice':
                if body.choice_index is None or body.matching_order is not None:
                    raise HTTPException(422, 'Выбери один вариант ответа.')
                selected = body.choice_index
                correct = selected == question['correct_index']
            else:
                if body.choice_index is not None or body.matching_order is None or sorted(body.matching_order) != [0, 1, 2, 3]:
                    raise HTTPException(422, 'Соедини все четыре пары по одному разу.')
                selected = body.matching_order
                correct = selected == question['correct_order']
            answered_on = datetime.now(ZoneInfo('Asia/Yekaterinburg')).date().isoformat()
            try:
                updated = db.record_quiz_answer(quiz, selected, correct, answered_on)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            return public_quiz(updated)

    @app.post('/api/quizzes/{quiz_id}/next')
    async def next_quiz_question(quiz_id: str, body: QuizStep, s=Depends(student)):
        async with locks[s['id']]:
            quiz = db.quiz(s['id'], quiz_id)
            if not quiz:
                raise HTTPException(404, 'Тест не найден.')
            if quiz['idx'] != body.index or quiz['idx'] == 5:
                raise HTTPException(409, 'Этот вопрос уже изменился.')
            try:
                updated = db.advance_quiz(quiz)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            return public_quiz(updated)

    @app.post('/api/lessons')
    async def new_lesson(body:Topic, s=Depends(student)):
        if body.subject not in subjects_for_grade(s['grade']):
            raise HTTPException(422,'Выбери предмет из списка.')
        if settings.demo_mode and (body.subject != 'Математика' or body.topic != 'Обыкновенные дроби'):
            raise HTTPException(422,'В деморежиме доступен готовый урок «Обыкновенные дроби».')
        limit(s['id'])
        async with locks[s['id']]:
            previous = db.lesson(s['id'])
            if previous and previous['idx'] < 5:
                raise HTTPException(409,'Сначала заверши текущий урок.')
            if db.pending_job(s['id']):
                raise HTTPException(409, 'Урок уже готовится. Подожди немного.')
            try:
                relevant = await app.state.ai.validate_topic(body.subject, body.topic, s['grade'])
            except AIError as exc:
                raise HTTPException(503, str(exc)) from None
            if not relevant:
                raise HTTPException(422, unrelated_topic_message(body.subject))
            blocks = await app.state.ai.generate(body.subject,body.topic,s['grade'])
            from .ai import LessonContent
            validated = LessonContent(blocks=blocks)
            return public_lesson(db.save_lesson(s['id'],body.subject,body.topic,[b.model_dump() for b in validated.blocks]))

    async def generate_lesson_job(job):
        try:
            grade = db.student(job['user_id'])['grade']
            relevant = await app.state.ai.validate_topic(job['subject'], job['topic'], grade)
            if not relevant:
                db.finish_job(job['id'], error=unrelated_topic_message(job['subject']))
                return
            blocks = await app.state.ai.generate(job['subject'], job['topic'], grade)
            from .ai import LessonContent
            validated = LessonContent(blocks=blocks)
            async with locks[job['user_id']]:
                current = db.job(job['user_id'], job['id'])
                if current and current['status'] == 'pending':
                    saved = db.save_lesson(job['user_id'], job['subject'], job['topic'], [b.model_dump() for b in validated.blocks])
                    db.finish_job(job['id'], lesson_id=saved['id'])
        except asyncio.CancelledError:
            db.finish_job(job['id'], error='Подготовка урока отменена.')
            raise
        except AIError as exc:
            db.finish_job(job['id'], error=str(exc))
        except Exception:
            logger.exception('Не удалось подготовить урок')
            db.finish_job(job['id'], error='Не удалось подготовить урок. Попробуй ещё раз.')
        finally:
            generation_tasks_by_id.pop(job['id'], None)

    def schedule_generation(job):
        task = asyncio.create_task(generate_lesson_job(job))
        generation_tasks.add(task)
        generation_tasks_by_id[job['id']] = task
        task.add_done_callback(generation_tasks.discard)

    @app.post('/api/preparations/open')
    async def open_preparation(body: PreparationLink, s=Depends(student)):
        notification = db.notification_by_token(s['id'], body.token)
        if not notification:
            raise HTTPException(404, 'Ссылка на подготовку недоступна или устарела.')
        async with locks[s['id']]:
            active = db.lesson(s['id'])
            pending = db.pending_job(s['id'])
            if active and active['idx'] < 5 and active['subject'] == notification['subject'] and active['topic'] == notification['topic']:
                return {'status': 'ready', 'lesson': public_lesson(active)}
            if pending and pending['subject'] == notification['subject'] and pending['topic'] == notification['topic']:
                return {'status': 'pending', 'id': pending['id']}
            limit(s['id'])
            if pending:
                db.cancel_pending_job(s['id'])
                old_task = generation_tasks_by_id.get(pending['id'])
                if old_task:
                    old_task.cancel()
            if active and active['idx'] < 5:
                db.cancel_lesson(s['id'], active['id'])
            job = db.create_job(s['id'], notification['subject'], notification['topic'])
            schedule_generation(job)
            return {'status': 'pending', 'id': job['id']}

    @app.post('/api/lesson-jobs', status_code=202)
    async def start_lesson_job(body: Topic, s=Depends(student)):
        if body.subject not in subjects_for_grade(s['grade']):
            raise HTTPException(422, 'Выбери предмет из списка.')
        if settings.demo_mode and (body.subject != 'Математика' or body.topic != 'Обыкновенные дроби'):
            raise HTTPException(422, 'В деморежиме доступен готовый урок «Обыкновенные дроби».')
        async with locks[s['id']]:
            previous = db.lesson(s['id'])
            if previous and previous['idx'] < 5:
                raise HTTPException(409, 'Сначала заверши текущий урок.')
            pending = db.pending_job(s['id'])
            if pending:
                return {'id': pending['id'], 'status': 'pending'}
            limit(s['id'])
            job = db.create_job(s['id'], body.subject, body.topic)
            schedule_generation(job)
            return {'id': job['id'], 'status': 'pending'}

    @app.get('/api/lesson-jobs/{job_id}')
    def lesson_job_status(job_id: str, s=Depends(student)):
        job = db.job(s['id'], job_id)
        if not job:
            raise HTTPException(404, 'Подготовка урока не найдена.')
        result = {'id': job['id'], 'status': job['status']}
        if job['status'] == 'ready':
            result['lesson'] = public_lesson(db.lesson(s['id'], job['lesson_id']))
            if not result['lesson']:
                result = {'id': job['id'], 'status': 'error', 'error': 'Урок уже закрыт. Выбери тему заново.'}
        elif job['status'] == 'error':
            result['error'] = job['error'] or 'Не удалось подготовить урок.'
        return result

    @app.get('/api/lessons/{lid}')
    def get_lesson(lid:str,s=Depends(student)):
        return public_lesson(owned(s['id'],lid))

    @app.post('/api/lessons/{lid}/cancel')
    async def cancel_lesson(lid:str,s=Depends(student)):
        async with locks[s['id']]:
            if not db.cancel_lesson(s['id'], lid):
                raise HTTPException(404,'Активное занятие не найдено.')
            return {'cancelled': True}

    @app.post('/api/lessons/{lid}/simplify')
    async def simplify(lid:str,body:Step,s=Depends(student)):
        limit(s['id'])
        async with locks[s['id']]:
            l = owned(s['id'],lid)
            check_step(l,body.block_index)
            explanation = SimpleExplanation.model_validate(
                await app.state.ai.simplify(l['blocks'][l['idx']], s['grade'])
            ).model_dump()
            try:
                updated = db.simplify_block(l, explanation)
            except ValueError as exc:
                raise HTTPException(409,str(exc)) from None
            return public_lesson(updated)

    @app.post('/api/lessons/{lid}/answer')
    async def answer(lid:str,body:Answer,s=Depends(student)):
        limit(s['id'])
        async with locks[s['id']]:
            l = owned(s['id'],lid)
            check_step(l,body.block_index)
            current_feedback = l['feedback'].get(str(l['idx'])) or {}
            if current_feedback.get('correct') is True:
                raise HTTPException(409,'Этот вопрос уже решён. Продолжай к следующему шагу.')
            context = db.attempt_context(l,l['idx'])
            solved_on = datetime.now(ZoneInfo('Asia/Yekaterinburg')).date().isoformat()
            from .ai import Feedback
            feedback = Feedback.model_validate(await app.state.ai.grade(
                l['blocks'][l['idx']],
                body.answer,
                s['grade'],
                attempt_number=context['attempt_number'],
                previous_feedback=context['previous_feedback'],
            )).model_dump()
            try:
                updated = db.record_attempt(l,feedback,solved_on)
            except ValueError as exc:
                raise HTTPException(409,str(exc)) from None
            return public_lesson(updated)

    @app.post('/api/lessons/{lid}/next')
    async def next_block(lid:str,body:Step,s=Depends(student)):
        async with locks[s['id']]:
            l = owned(s['id'],lid)
            check_step(l,body.block_index)
            feedback = l['feedback'].get(str(l['idx'])) or {}
            if feedback.get('correct') is not True:
                raise HTTPException(409,'Сначала дай верный ответ на этот вопрос.')
            db.advance(l)
            return public_lesson(db.lesson(s['id'],lid))

    @app.post('/webhook/max')
    async def webhook(request:Request):
        if settings.bot_mode != 'webhook':
            raise HTTPException(503,'Webhook выключен.')
        supplied = request.headers.get('X-Max-Bot-Api-Secret','')
        if not hmac.compare_digest(supplied, settings.webhook_secret):
            raise HTTPException(403,'Неверный секрет webhook.')
        try:
            event = await request.json()
        except ValueError:
            raise HTTPException(400,'Некорректный JSON.') from None
        if not isinstance(event,dict) or not isinstance(event.get('update_type'),str):
            raise HTTPException(422,'Некорректное событие.')
        db.enqueue(event)
        return {'ok':True}

    return app
