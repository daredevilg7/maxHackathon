import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo


PRIMARY_SCHEDULE = (
    ('Математика', 'Сложение и вычитание'),
    ('Русский язык', 'Слова и предложения'),
    ('Литературное чтение', 'Работа с текстом'),
    ('Окружающий мир', 'Растения вокруг нас'),
)

MIDDLE_SCHEDULE = (
    ('Математика', 'Дроби'),
    ('Русский язык', 'Части речи'),
    ('Биология', 'Растения'),
    ('История', 'Древняя Русь'),
)

SEVENTH_SCHEDULE = (
    ('Математика', 'Дроби'),
    ('Русский язык', 'Причастия'),
    ('Физика', 'Плотность вещества'),
    ('Биология', 'Строение клетки'),
)


def academic_year_dates(today):
    if today.month >= 9:
        return date(today.year, 9, 1), date(today.year + 1, 5, 31)
    if today.month <= 5:
        return date(today.year - 1, 9, 1), date(today.year, 5, 31)
    return date(today.year, 9, 1), date(today.year + 1, 5, 31)


class Database:
    def __init__(self, path):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS students(id INTEGER PRIMARY KEY, name TEXT NOT NULL, grade INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS diary(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, day TEXT NOT NULL, period INTEGER NOT NULL, subject TEXT NOT NULL, topic TEXT NOT NULL, homework TEXT NOT NULL, assessment INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS lessons(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, subject TEXT NOT NULL, topic TEXT NOT NULL, blocks TEXT NOT NULL, idx INTEGER NOT NULL DEFAULT 0, feedback TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS solved_steps(user_id INTEGER NOT NULL, lesson_id TEXT NOT NULL, block_index INTEGER NOT NULL, solved_on TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(user_id,lesson_id,block_index));
            CREATE TABLE IF NOT EXISTS inbox(id TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS generation_jobs(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, subject TEXT NOT NULL, topic TEXT NOT NULL, status TEXT NOT NULL, lesson_id TEXT, error TEXT, created REAL NOT NULL, updated REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS generation_jobs_user_status ON generation_jobs(user_id,status);
            CREATE TABLE IF NOT EXISTS admin_sessions(token_hash TEXT PRIMARY KEY, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS notifications(
                key TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE, user_id INTEGER NOT NULL,
                subject TEXT NOT NULL, topic TEXT NOT NULL, diary_entry_id INTEGER,
                schedule_day TEXT, state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
                expires REAL NOT NULL, sent_at REAL
            );
            CREATE INDEX IF NOT EXISTS notifications_pending ON notifications(state,retry_at);
            CREATE TABLE IF NOT EXISTS bot_dialogs(
                user_id INTEGER PRIMARY KEY, stage TEXT NOT NULL,
                subject TEXT, updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS schedule_coverage(
                user_id INTEGER NOT NULL, year_end TEXT NOT NULL,
                PRIMARY KEY(user_id, year_end)
            );
            CREATE TABLE IF NOT EXISTS activity_days(
                user_id INTEGER NOT NULL, day TEXT NOT NULL,
                PRIMARY KEY(user_id, day)
            );
            CREATE TABLE IF NOT EXISTS quizzes(
                id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, lesson_id TEXT NOT NULL,
                questions TEXT NOT NULL, idx INTEGER NOT NULL DEFAULT 0,
                answers TEXT NOT NULL DEFAULT '{}', score INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL, completed REAL
            );
            CREATE INDEX IF NOT EXISTS quizzes_user_lesson ON quizzes(user_id,lesson_id,created);
            CREATE TABLE IF NOT EXISTS quiz_jobs(
                id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, lesson_id TEXT NOT NULL,
                status TEXT NOT NULL, quiz_id TEXT, error TEXT,
                created REAL NOT NULL, updated REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS quiz_jobs_user_status ON quiz_jobs(user_id,status);
            ''')
            c.execute('''INSERT OR IGNORE INTO activity_days(user_id,day)
                         SELECT DISTINCT user_id,solved_on FROM solved_steps''')
            # Earlier versions assigned synthetic grades above seven. Keep existing
            # profiles within the supported 1-7 range without replacing IDs.
            c.execute('UPDATE students SET grade=7 WHERE grade>7')

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def session(self, user):
        token = secrets.token_urlsafe(32)
        with self.connect() as c:
            c.execute('DELETE FROM sessions WHERE expires < ?', (time.time(),))
            c.execute('INSERT INTO sessions VALUES(?,?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), user['id'], user['name'], time.time()+86400))
        return token

    def identity(self, token):
        with self.connect() as c:
            r = c.execute('SELECT user_id AS id, name FROM sessions WHERE token=? AND expires>?', (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
            return dict(r) if r else None

    def student(self, uid):
        with self.connect() as c:
            r = c.execute('SELECT * FROM students WHERE id=?', (uid,)).fetchone()
            return dict(r) if r else None

    def register(self, user):
        today = datetime.now(ZoneInfo('Asia/Yekaterinburg')).date()
        school_start, school_end = academic_year_dates(today)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            student = c.execute('SELECT * FROM students WHERE id=?', (user['id'],)).fetchone()
            if not student:
                c.execute('INSERT INTO students VALUES(?,?,?)',
                          (user['id'], user['name'], secrets.choice(tuple(range(1, 8)))))
            marker = school_end.isoformat()
            if not c.execute('SELECT 1 FROM schedule_coverage WHERE user_id=? AND year_end=?',
                             (user['id'], marker)).fetchone():
                self._extend_student_schedule(c, user['id'], school_start, school_end, today)
                c.execute('INSERT INTO schedule_coverage(user_id,year_end) VALUES(?,?)', (user['id'], marker))
            return dict(c.execute('SELECT * FROM students WHERE id=?', (user['id'],)).fetchone())

    def _extend_student_schedule(self, c, uid, school_start, school_end, today):
        grade = c.execute('SELECT grade FROM students WHERE id=?', (uid,)).fetchone()['grade']
        schedule = PRIMARY_SCHEDULE if grade <= 4 else MIDDLE_SCHEDULE if grade <= 6 else SEVENTH_SCHEDULE
        latest = c.execute('SELECT MAX(day) FROM diary WHERE user_id=? AND day BETWEEN ? AND ?',
                           (uid, school_start.isoformat(), school_end.isoformat())).fetchone()[0]
        first = date.fromisoformat(latest) + timedelta(days=1) if latest else max(today, school_start)
        if first > school_end:
            return 0
        entries = []
        day = first
        while day <= school_end:
            if day.weekday() < 5:
                for period, (subject, topic) in enumerate(schedule, 1):
                    entries.append((uid, day.isoformat(), period, subject, topic,
                                    f'Повторить тему «{topic}», выполнить задания 1–3', 0))
            day += timedelta(days=1)
        c.executemany('''INSERT INTO diary(user_id,day,period,subject,topic,homework,assessment)
                         VALUES(?,?,?,?,?,?,?)''', entries)
        return len(entries)

    def extend_school_schedules(self):
        today = datetime.now(ZoneInfo('Asia/Yekaterinburg')).date()
        school_start, school_end = academic_year_dates(today)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            marker = school_end.isoformat()
            students = c.execute('''SELECT id FROM students WHERE NOT EXISTS (
                SELECT 1 FROM schedule_coverage WHERE user_id=students.id AND year_end=?)''',
                (marker,)).fetchall()
            added = 0
            for row in students:
                added += self._extend_student_schedule(c, row['id'], school_start, school_end, today)
                c.execute('INSERT INTO schedule_coverage(user_id,year_end) VALUES(?,?)',
                          (row['id'], marker))
            return added

    def diary(self, uid):
        with self.connect() as c:
            return [dict(x) for x in c.execute('SELECT * FROM diary WHERE user_id=? ORDER BY day,period', (uid,))]

    def diary_between(self, uid, start_day, end_day):
        with self.connect() as c:
            return [dict(row) for row in c.execute(
                'SELECT * FROM diary WHERE user_id=? AND day>=? AND day<=? ORDER BY day,period',
                (uid, start_day, end_day))]

    def completed_lessons(self, uid):
        with self.connect() as c:
            return c.execute('SELECT COUNT(*) FROM lessons WHERE user_id=? AND idx>=5', (uid,)).fetchone()[0]

    def bot_dialog(self, uid):
        with self.connect() as c:
            row = c.execute('SELECT stage,subject FROM bot_dialogs WHERE user_id=? AND updated>?',
                            (uid, time.time() - 1800)).fetchone()
            return dict(row) if row else None

    def set_bot_dialog(self, uid, stage, subject=None):
        with self.connect() as c:
            c.execute('INSERT OR REPLACE INTO bot_dialogs(user_id,stage,subject,updated) VALUES(?,?,?,?)',
                      (uid, stage, subject, time.time()))

    def clear_bot_dialog(self, uid):
        with self.connect() as c:
            c.execute('DELETE FROM bot_dialogs WHERE user_id=?', (uid,))

    def students(self, query='', limit=100, offset=0):
        with self.connect() as c:
            if query:
                pattern = '%' + query.replace('%', '\\%').replace('_', '\\_') + '%'
                rows = c.execute(
                    "SELECT id,name,grade FROM students WHERE name LIKE ? ESCAPE '\\' OR CAST(id AS TEXT) LIKE ? ESCAPE '\\' ORDER BY name,id LIMIT ? OFFSET ?",
                    (pattern, pattern, limit, offset),
                )
            else:
                rows = c.execute('SELECT id,name,grade FROM students ORDER BY name,id LIMIT ? OFFSET ?', (limit, offset))
            return [dict(row) for row in rows]

    def add_diary_entry(self, uid, entry):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT 1 FROM diary WHERE user_id=? AND day=? AND period=?',
                         (uid, entry['day'], entry['period'])).fetchone():
                raise ValueError('На эту дату и номер урока уже есть запись.')
            cursor = c.execute(
                'INSERT INTO diary(user_id,day,period,subject,topic,homework,assessment) VALUES(?,?,?,?,?,?,?)',
                (uid, entry['day'], entry['period'], entry['subject'], entry['topic'], entry['homework'], int(entry['assessment'])),
            )
            return dict(c.execute('SELECT * FROM diary WHERE id=?', (cursor.lastrowid,)).fetchone())

    def update_diary_entry(self, uid, entry_id, entry):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT 1 FROM diary WHERE user_id=? AND day=? AND period=? AND id<>?',
                         (uid, entry['day'], entry['period'], entry_id)).fetchone():
                raise ValueError('На эту дату и номер урока уже есть запись.')
            changed = c.execute(
                'UPDATE diary SET day=?,period=?,subject=?,topic=?,homework=?,assessment=? WHERE id=? AND user_id=?',
                (entry['day'], entry['period'], entry['subject'], entry['topic'], entry['homework'], int(entry['assessment']), entry_id, uid),
            ).rowcount
            if not changed:
                return None
            return dict(c.execute('SELECT * FROM diary WHERE id=?', (entry_id,)).fetchone())

    def delete_diary_entry(self, uid, entry_id):
        with self.connect() as c:
            return c.execute('DELETE FROM diary WHERE id=? AND user_id=?', (entry_id, uid)).rowcount == 1

    def create_admin_session(self):
        token = secrets.token_urlsafe(32)
        with self.connect() as c:
            c.execute('DELETE FROM admin_sessions WHERE expires<?', (time.time(),))
            c.execute('INSERT INTO admin_sessions VALUES(?,?)', (hashlib.sha256(token.encode()).hexdigest(), time.time() + 8 * 3600))
        return token

    def admin_session_valid(self, token):
        if not token:
            return False
        with self.connect() as c:
            return c.execute('SELECT 1 FROM admin_sessions WHERE token_hash=? AND expires>?', (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone() is not None

    def revoke_admin_session(self, token):
        if token:
            with self.connect() as c:
                c.execute('DELETE FROM admin_sessions WHERE token_hash=?', (hashlib.sha256(token.encode()).hexdigest(),))

    def fail_stale_jobs(self):
        with self.connect() as c:
            c.execute("UPDATE generation_jobs SET status='error',error='Сервер перезапустился. Начни урок ещё раз.',updated=? WHERE status='pending'", (time.time(),))
            c.execute("UPDATE quiz_jobs SET status='error',error='Сервер перезапустился. Открой тест ещё раз.',updated=? WHERE status='pending'", (time.time(),))

    def completed_history(self, uid):
        with self.connect() as c:
            rows = c.execute('''SELECT l.id,l.subject,l.topic,
                               COALESCE((SELECT MAX(s.created) FROM solved_steps s
                                         WHERE s.user_id=l.user_id AND s.lesson_id=l.id),l.created) AS completed_at
                               FROM lessons l WHERE l.user_id=? AND l.idx>=5
                               ORDER BY completed_at DESC''', (uid,)).fetchall()
            scores = c.execute('''SELECT l.subject,l.topic,MAX(q.score) AS best
                                  FROM quizzes q JOIN lessons l ON l.id=q.lesson_id
                                  WHERE q.user_id=? AND q.completed IS NOT NULL
                                  GROUP BY l.subject,l.topic''', (uid,)).fetchall()
            best = {}
            for score in scores:
                key = (score['subject'].casefold(), score['topic'].casefold())
                best[key] = max(best.get(key, 0), score['best'])
        seen = set()
        result = []
        for row in rows:
            key = (row['subject'].casefold(), row['topic'].casefold())
            if key in seen:
                continue
            seen.add(key)
            result.append({**dict(row), 'best_score': best.get(key)})
        return result[:100]

    def activity_summary(self, uid, today):
        with self.connect() as c:
            dates = [date.fromisoformat(row['day']) for row in c.execute(
                'SELECT day FROM activity_days WHERE user_id=? ORDER BY day DESC', (uid,))]
        active = set(dates)
        day = date.fromisoformat(today)
        recent = [(day - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)]
        anchor = day if day in active else day - timedelta(days=1)
        streak = 0
        while anchor in active:
            streak += 1
            anchor -= timedelta(days=1)
        return {'streak': streak, 'recent_days': [value for value in recent if date.fromisoformat(value) in active]}

    def pending_quiz_job(self, uid, lesson_id):
        with self.connect() as c:
            row = c.execute("SELECT * FROM quiz_jobs WHERE user_id=? AND lesson_id=? AND status='pending' ORDER BY created DESC LIMIT 1",
                            (uid, lesson_id)).fetchone()
            return dict(row) if row else None

    def create_quiz_job(self, uid, lesson_id):
        job_id = uuid4().hex
        now = time.time()
        with self.connect() as c:
            c.execute('INSERT INTO quiz_jobs(id,user_id,lesson_id,status,created,updated) VALUES(?,?,?,?,?,?)',
                      (job_id, uid, lesson_id, 'pending', now, now))
        return self.quiz_job(uid, job_id)

    def quiz_job(self, uid, job_id):
        with self.connect() as c:
            row = c.execute('SELECT * FROM quiz_jobs WHERE id=? AND user_id=?', (job_id, uid)).fetchone()
            return dict(row) if row else None

    def finish_quiz_job(self, job_id, *, quiz_id=None, error=None):
        with self.connect() as c:
            c.execute("UPDATE quiz_jobs SET status=?,quiz_id=?,error=?,updated=? WHERE id=? AND status='pending'",
                      ('ready' if quiz_id else 'error', quiz_id, error, time.time(), job_id))

    def active_quiz(self, uid, lesson_id):
        with self.connect() as c:
            row = c.execute('SELECT id FROM quizzes WHERE user_id=? AND lesson_id=? AND completed IS NULL ORDER BY created DESC LIMIT 1',
                            (uid, lesson_id)).fetchone()
        return self.quiz(uid, row['id']) if row else None

    def save_quiz(self, uid, lesson_id, questions):
        quiz_id = uuid4().hex
        with self.connect() as c:
            c.execute('INSERT INTO quizzes(id,user_id,lesson_id,questions,created) VALUES(?,?,?,?,?)',
                      (quiz_id, uid, lesson_id, json.dumps(questions, ensure_ascii=False), time.time()))
        return self.quiz(uid, quiz_id)

    def quiz(self, uid, quiz_id):
        with self.connect() as c:
            row = c.execute('SELECT * FROM quizzes WHERE id=? AND user_id=?', (quiz_id, uid)).fetchone()
        if not row:
            return None
        result = dict(row)
        result['questions'] = json.loads(result['questions'])
        result['answers'] = json.loads(result['answers'])
        return result

    def record_quiz_answer(self, quiz, selected, correct, answered_on):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT idx,answers FROM quizzes WHERE id=? AND user_id=?',
                            (quiz['id'], quiz['user_id'])).fetchone()
            if not row or row['idx'] != quiz['idx'] or row['idx'] >= 5:
                raise ValueError('Вопрос уже изменился. Открой тест снова.')
            answers = json.loads(row['answers'])
            if str(row['idx']) in answers:
                raise ValueError('На этот вопрос уже дан ответ.')
            answers[str(row['idx'])] = {'selected': selected, 'correct': correct}
            c.execute('UPDATE quizzes SET answers=?,score=score+? WHERE id=?',
                      (json.dumps(answers, ensure_ascii=False), int(correct), quiz['id']))
            c.execute('INSERT OR IGNORE INTO activity_days(user_id,day) VALUES(?,?)',
                      (quiz['user_id'], answered_on))
        return self.quiz(quiz['user_id'], quiz['id'])

    def advance_quiz(self, quiz):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT idx,answers FROM quizzes WHERE id=? AND user_id=?',
                            (quiz['id'], quiz['user_id'])).fetchone()
            if not row or row['idx'] != quiz['idx'] or str(row['idx']) not in json.loads(row['answers']):
                raise ValueError('Сначала ответь на текущий вопрос.')
            next_index = row['idx'] + 1
            c.execute('UPDATE quizzes SET idx=?,completed=? WHERE id=?',
                      (next_index, time.time() if next_index == 5 else None, quiz['id']))
        return self.quiz(quiz['user_id'], quiz['id'])

    def pending_job(self, uid):
        with self.connect() as c:
            row = c.execute("SELECT * FROM generation_jobs WHERE user_id=? AND status='pending' ORDER BY created DESC LIMIT 1", (uid,)).fetchone()
            return dict(row) if row else None

    def create_job(self, uid, subject, topic):
        job_id = uuid4().hex
        now = time.time()
        with self.connect() as c:
            c.execute('INSERT INTO generation_jobs(id,user_id,subject,topic,status,created,updated) VALUES(?,?,?,?,?,?,?)', (job_id,uid,subject,topic,'pending',now,now))
        return self.job(uid, job_id)

    def job(self, uid, job_id):
        with self.connect() as c:
            row = c.execute('SELECT * FROM generation_jobs WHERE id=? AND user_id=?', (job_id,uid)).fetchone()
            return dict(row) if row else None

    def finish_job(self, job_id, *, lesson_id=None, error=None):
        with self.connect() as c:
            c.execute("UPDATE generation_jobs SET status=?,lesson_id=?,error=?,updated=? WHERE id=? AND status='pending'",
                      ('ready' if lesson_id else 'error', lesson_id, error, time.time(), job_id))

    def cancel_pending_job(self, uid):
        with self.connect() as c:
            row = c.execute("SELECT id FROM generation_jobs WHERE user_id=? AND status='pending'", (uid,)).fetchone()
            if row:
                c.execute("UPDATE generation_jobs SET status='error',error='Подготовка заменена новым уроком.',updated=? WHERE id=? AND status='pending'", (time.time(), row['id']))
            return row['id'] if row else None

    def queue_notification(self, key, uid, subject, topic, *, diary_entry_id=None, schedule_day=None, draft=False):
        token = secrets.token_urlsafe(24)
        expires = time.time() + 14 * 86400
        with self.connect() as c:
            c.execute('''INSERT OR IGNORE INTO notifications
                (key,token,user_id,subject,topic,diary_entry_id,schedule_day,expires,state)
                VALUES(?,?,?,?,?,?,?,?,?)''',
                (key,token,uid,subject,topic,diary_entry_id,schedule_day,expires,'draft' if draft else 'pending'))
            row = c.execute('SELECT * FROM notifications WHERE key=?', (key,)).fetchone()
            return dict(row)

    def queue_due_assessments(self, today):
        target = (date.fromisoformat(today) + timedelta(days=2)).isoformat()
        with self.connect() as c:
            entries = [dict(row) for row in c.execute(
                'SELECT * FROM diary WHERE assessment=1 AND day=? ORDER BY user_id,period', (target,))]
        for entry in entries:
            self.queue_notification(f'assessment:{entry["id"]}:{target}', entry['user_id'],
                                    entry['subject'], entry['topic'],
                                    diary_entry_id=entry['id'], schedule_day=target)

    def pending_notifications(self):
        with self.connect() as c:
            return [dict(row) for row in c.execute(
                "SELECT * FROM notifications WHERE state='pending' AND retry_at<=? ORDER BY rowid LIMIT 20", (time.time(),))]

    def notification_current(self, notification):
        if notification['diary_entry_id'] is None:
            return True
        with self.connect() as c:
            row = c.execute('SELECT day,assessment,subject,topic FROM diary WHERE id=? AND user_id=?',
                            (notification['diary_entry_id'], notification['user_id'])).fetchone()
            return bool(row and row['day'] == notification['schedule_day'] and row['assessment'] and
                        row['subject'] == notification['subject'] and row['topic'] == notification['topic'])

    def notification_by_token(self, uid, token):
        with self.connect() as c:
            row = c.execute('''SELECT * FROM notifications WHERE token=? AND user_id=? AND expires>?
                AND (state='sent' OR (state='draft' AND key LIKE 'bot-choice:%'))''',
                            (token, uid, time.time())).fetchone()
            return dict(row) if row else None

    def notification_result(self, key, *, sent=False, skipped=False):
        with self.connect() as c:
            if sent or skipped:
                c.execute("UPDATE notifications SET state=?,sent_at=? WHERE key=? AND state IN ('pending','draft')",
                          ('sent' if sent else 'skipped', time.time() if sent else None, key))
            else:
                row = c.execute('SELECT attempts FROM notifications WHERE key=?', (key,)).fetchone()
                if row:
                    attempts = row['attempts'] + 1
                    c.execute('UPDATE notifications SET attempts=?,retry_at=?,state=? WHERE key=?',
                              (attempts, time.time() + min(3600, 60 * 2 ** min(attempts, 6)),
                               'failed' if attempts >= 12 else 'pending', key))

    def save_lesson(self, uid, subject, topic, blocks):
        lid = uuid4().hex
        with self.connect() as c:
            c.execute('INSERT INTO lessons(id,user_id,subject,topic,blocks,created) VALUES(?,?,?,?,?,?)', (lid,uid,subject,topic,json.dumps(blocks,ensure_ascii=False),time.time()))
        return self.lesson(uid,lid)

    def lesson(self, uid, lid=None):
        with self.connect() as c:
            if lid:
                r = c.execute('SELECT * FROM lessons WHERE user_id=? AND id=?', (uid,lid)).fetchone()
            else:
                r = c.execute('SELECT * FROM lessons WHERE user_id=? ORDER BY created DESC LIMIT 1', (uid,)).fetchone()
        if not r:
            return None
        result = dict(r)
        result['blocks'] = json.loads(result['blocks'])
        result['feedback'] = json.loads(result['feedback'])
        return result

    def cancel_lesson(self, uid, lid):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            removed = c.execute(
                'DELETE FROM lessons WHERE id=? AND user_id=? AND idx<5',
                (lid, uid),
            ).rowcount
        return removed == 1

    def attempt_context(self, lesson, block_index):
        previous = lesson['feedback'].get(str(block_index)) or {}
        return {
            'attempt_number': int(previous.get('attempts', 0)) + 1,
            'previous_feedback': {
                key: previous[key]
                for key in ('correct', 'explanation', 'addition')
                if key in previous
            },
        }

    def record_attempt(self, lesson, value, solved_on):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute(
                'SELECT idx, feedback FROM lessons WHERE id=? AND user_id=?',
                (lesson['id'], lesson['user_id']),
            ).fetchone()
            if not row or row['idx'] != lesson['idx']:
                raise ValueError('Урок изменился до сохранения ответа.')

            feedback = json.loads(row['feedback'])
            previous = feedback.get(str(lesson['idx'])) or {}
            if previous.get('correct') is True:
                raise ValueError('Этот вопрос уже решён.')

            feedback[str(lesson['idx'])] = {
                **value,
                'attempts': int(previous.get('attempts', 0)) + 1,
            }
            c.execute('INSERT OR IGNORE INTO activity_days(user_id,day) VALUES(?,?)',
                      (lesson['user_id'], solved_on))
            c.execute(
                'UPDATE lessons SET feedback=? WHERE id=? AND user_id=? AND idx=?',
                (json.dumps(feedback, ensure_ascii=False), lesson['id'], lesson['user_id'], lesson['idx']),
            )
            if value.get('correct') is True:
                c.execute(
                    'INSERT OR IGNORE INTO solved_steps(user_id,lesson_id,block_index,solved_on,created) VALUES(?,?,?,?,?)',
                    (lesson['user_id'], lesson['id'], lesson['idx'], solved_on, time.time()),
                )
        return self.lesson(lesson['user_id'], lesson['id'])

    def solved_today(self, uid, day):
        with self.connect() as c:
            count = c.execute(
                'SELECT COUNT(*) FROM solved_steps WHERE user_id=? AND solved_on=?',
                (uid, day),
            ).fetchone()[0]
        return min(count, 5)

    def simplify_block(self, lesson, explanation):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute(
                'SELECT idx, blocks FROM lessons WHERE id=? AND user_id=?',
                (lesson['id'], lesson['user_id']),
            ).fetchone()
            if not row or row['idx'] != lesson['idx']:
                raise ValueError('Урок изменился до сохранения объяснения.')
            blocks = json.loads(row['blocks'])
            blocks[lesson['idx']].update(explanation)
            blocks[lesson['idx']]['simplified'] = True
            c.execute(
                'UPDATE lessons SET blocks=? WHERE id=? AND user_id=? AND idx=?',
                (json.dumps(blocks, ensure_ascii=False), lesson['id'], lesson['user_id'], lesson['idx']),
            )
        return self.lesson(lesson['user_id'], lesson['id'])

    def advance(self, lesson):
        with self.connect() as c:
            c.execute('UPDATE lessons SET idx=idx+1 WHERE id=? AND idx=?', (lesson['id'],lesson['idx']))

    def enqueue(self, event):
        payload = json.dumps(event, sort_keys=True, ensure_ascii=False)
        key = hashlib.sha256(payload.encode()).hexdigest()
        with self.connect() as c:
            c.execute('INSERT OR IGNORE INTO inbox(id,payload) VALUES(?,?)', (key,payload))


def public_lesson(lesson):
    if not lesson:
        return None
    idx = lesson['idx']
    block = None if idx == 5 else {k:v for k,v in lesson['blocks'][idx].items() if k != 'expected_answer'}
    feedback = lesson['feedback'].get(str(idx))
    solved = bool(feedback and feedback.get('correct') is True)
    return {
        'id': lesson['id'],
        'subject': lesson['subject'],
        'topic': lesson['topic'],
        'index': idx,
        'total': 5,
        'completed': idx == 5,
        'block': block,
        'feedback': feedback,
        'reviewed': len(lesson['feedback']),
        'solved': solved,
        'can_advance': solved,
    }


def public_quiz(quiz):
    if not quiz:
        return None
    index = quiz['idx']
    result = {
        'id': quiz['id'], 'lesson_id': quiz['lesson_id'],
        'index': index, 'total': 5, 'score': quiz['score'],
        'completed': index == 5,
    }
    if index == 5:
        return result
    question = quiz['questions'][index]
    result['question'] = {key: question[key] for key in ('kind', 'prompt')}
    if question['kind'] == 'choice':
        result['question']['options'] = question['options']
    else:
        result['question']['left'] = question['left']
        result['question']['right'] = question['right']
    answer = quiz['answers'].get(str(index))
    if answer:
        result['result'] = {
            **answer,
            'correct_answer': question['correct_index'] if question['kind'] == 'choice' else question['correct_order'],
            'explanation': question['explanation'],
        }
    return result
