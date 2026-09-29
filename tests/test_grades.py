from app.db import Database
from app.ai import subjects_for_grade
from test_app import registered


def test_new_profiles_cover_grades_one_through_seven_with_suitable_demo_schedule(
    tmp_path, monkeypatch
):
    db = Database(str(tmp_path / 'grades.db'))
    seen_options = []

    def first_grade(options):
        seen_options.append(tuple(options))
        return options[0]

    monkeypatch.setattr('app.db.secrets.choice', first_grade)
    younger = db.register({'id': 101, 'name': 'Маша'})
    assert seen_options == [tuple(range(1, 8))]
    assert younger['grade'] == 1
    younger_subjects = {entry['subject'] for entry in db.diary(101)}
    assert 'Окружающий мир' in younger_subjects
    assert 'Физика' not in younger_subjects

    monkeypatch.setattr('app.db.secrets.choice', lambda options: options[-1])
    older = db.register({'id': 102, 'name': 'Саша'})
    assert older['grade'] == 7
    older_subjects = {entry['subject'] for entry in db.diary(102)}
    assert 'Биология' in older_subjects
    assert 'Окружающий мир' not in older_subjects


def test_legacy_random_grades_are_capped_at_seven(tmp_path):
    path = str(tmp_path / 'legacy.db')
    db = Database(path)
    with db.connect() as connection:
        connection.execute('INSERT INTO students VALUES (?,?,?)', (201, 'Ученик', 9))
    migrated = Database(path)
    assert migrated.student(201)['grade'] == 7


def test_subject_choices_follow_the_students_grade(client, monkeypatch):
    monkeypatch.setattr('app.db.secrets.choice', lambda options: options[0])
    headers = registered(client, 301)
    profile = client.get('/api/me', headers=headers).json()
    assert profile['student']['grade'] == 1
    assert profile['subjects'] == subjects_for_grade(1)
    assert 'Окружающий мир' in profile['subjects']
    assert 'Физика' not in profile['subjects']
    rejected = client.post('/api/lessons', headers=headers,
                           json={'subject': 'Физика', 'topic': 'Закон Ома'})
    assert rejected.status_code == 422

    assert 'Физика' in subjects_for_grade(7)
    assert 'Химия' not in subjects_for_grade(7)
