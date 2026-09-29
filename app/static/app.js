'use strict';

const $ = id => document.getElementById(id);
let token = sessionStorage.getItem('ponyato-token') || sessionStorage.getItem('buddy-token') || '';
let config, profile, lesson, quiz, quizSource;
let busy = false;
let lessonPhase = 0;
let cancelPreviousFocus = null;
let quizChoice = -1;
let quizMatching = [-1, -1, -1, -1];
const views = ['welcome', 'dashboard', 'catalog', 'lesson-view', 'complete', 'history', 'quiz-view', 'quiz-complete'];

const subjectTopics = {
  'Математика': ['Обыкновенные дроби', 'Проценты', 'Линейные уравнения', 'Степени числа', 'Площадь фигур'],
  'Русский язык': ['Безударные гласные', 'Части речи', 'Однородные члены', 'Причастный оборот', 'Прямая речь'],
  'Физика': ['Сила и движение', 'Плотность вещества', 'Давление', 'Закон Ома', 'Энергия'],
  'Биология': ['Строение клетки', 'Фотосинтез', 'Органы человека', 'Экосистемы', 'Наследственность'],
  'История': ['Древняя Русь', 'Монгольское нашествие', 'Пётр I', 'Война 1812 года', 'Великая Отечественная война'],
  'Химия': ['Атомы и молекулы', 'Периодическая таблица', 'Валентность', 'Химические реакции', 'Кислоты и основания'],
  'География': ['Географические координаты', 'Климат', 'Природные зоны', 'Рельеф Земли', 'Население России'],
  'Информатика': ['Алгоритмы', 'Системы счисления', 'Логика', 'Электронные таблицы', 'Безопасность в интернете'],
  'Английский язык': ['Present Simple', 'Past Simple', 'Future Simple', 'Неправильные глаголы', 'Как задать вопрос']
};

function show(view) {
  views.forEach(name => { $(name).hidden = name !== view; });
  $('loading').hidden = true;
  if (view === 'welcome') $('class-label').hidden = true;
  const activeView = $(view);
  activeView.scrollTop = 0;
  activeView.querySelectorAll('.subject-grid, .mission-sheet-content').forEach(panel => { panel.scrollTop = 0; });
}

function error(message) {
  $('error').textContent = message;
  $('error').hidden = !message;
}

async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? {'Authorization': 'Bearer ' + token} : {})
    },
    ...(body === undefined ? {} : {body: JSON.stringify(body)})
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) {
      sessionStorage.removeItem('ponyato-token');
      sessionStorage.removeItem('buddy-token');
    }
    throw new Error(typeof data.detail === 'string' ? data.detail : 'Лучик потерял связь. Попробуй ещё раз.');
  }
  return data;
}

async function action(button, text, fn) {
  if (busy) return;
  busy = true;
  error('');
  const original = button.textContent;
  const disabledButtons = [...document.querySelectorAll('button')].map(item => [item, item.disabled]);
  disabledButtons.forEach(([item]) => { item.disabled = true; });
  button.textContent = text;
  try {
    await fn();
  } catch (e) {
    error(e.message || 'Лучик потерял связь. Попробуй ещё раз.');
  } finally {
    busy = false;
    if (button.textContent === text) button.textContent = original;
    disabledButtons.forEach(([item, disabled]) => { item.disabled = disabled; });
  }
}

async function withAiLoading(message, operation) {
  const overlay = $('ai-loading');
  const video = $('thinking-video');
  const shell = $('app-shell');
  const previousFocus = document.activeElement;
  $('ai-loading-text').textContent = message;
  overlay.classList.remove('video-playing');
  overlay.hidden = false;
  shell.inert = true;
  shell.setAttribute('aria-hidden', 'true');
  overlay.focus();

  if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    video.muted = true;
    video.defaultMuted = true;
    video.playsInline = true;
    const playing = video.play();
    if (playing && typeof playing.then === 'function') {
      playing.then(() => {
        if (!overlay.hidden) overlay.classList.add('video-playing');
      }).catch(() => {});
    }
  }

  try {
    return await operation();
  } finally {
    video.pause();
    try { video.currentTime = 0; } catch (_) {}
    overlay.classList.remove('video-playing');
    overlay.hidden = true;
    shell.inert = false;
    shell.removeAttribute('aria-hidden');
    if (previousFocus && previousFocus.isConnected && !previousFocus.hidden) previousFocus.focus();
  }
}

async function waitForLesson(jobId) {
  const deadline = Date.now() + 360000;
  while (Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 2500));
    let job;
    try {
      job = await api('/api/lesson-jobs/' + jobId);
    } catch (e) {
      if (e instanceof TypeError) {
        $('ai-loading-text').textContent = 'Связь прервалась. Лучик продолжает готовить урок…';
        continue;
      }
      throw e;
    }
    if (job.status === 'ready') {
      if (!job.lesson) throw new Error('Урок уже закрыт. Выбери тему заново.');
      return job.lesson;
    }
    if (job.status === 'error') throw new Error(job.error || 'Не удалось подготовить урок. Попробуй ещё раз.');
  }
  throw new Error('Урок готовится дольше обычного. Открой приложение ещё раз через минуту.');
}

async function dashboard() {
  profile = await api('/api/me');
  if (!profile.student) {
    show('welcome');
    return;
  }
  lesson = profile.lesson;
  const daily = profile.daily || {date: '', correct: 0, target: 2, total: 5};
  const count = Math.max(0, Math.min(5, Number(daily.correct) || 0));
  const good = count >= 2;

  $('class-label').textContent = profile.student.grade + ' класс';
  $('class-label').hidden = false;
  $('hud-count').textContent = Math.min(count, 2) + ' / 2';
  $('greeting').textContent = 'Привет, ' + (profile.student.name || profile.name || 'друг').split(' ')[0] + '!';
  $('mood-copy').textContent = good
    ? count >= 5 ? 'На сегодня всё готово. До встречи!' : 'Можно выбрать ещё одну тему.'
    : count === 1 ? 'Осталось одно задание до цели.' : 'С какой темы начнём?';
  $('mood-mascot').src = good
    ? (count >= 5 ? '/static/mascot-proud.png' : '/static/mascot-happy.png')
    : '/static/mascot-focused.png';
  $('mood-mascot').alt = good ? 'Лучик радуется твоим успехам' : 'Лучик ждёт новый урок';
  $('mini-progress').textContent = count + ' / 5';
  $('user-progress-track').setAttribute('aria-valuenow', String(count));
  $('user-progress-track').querySelectorAll('.quest-pip').forEach((pip, index) => {
    pip.classList.toggle('filled', index < count);
  });
  $('daily-status').textContent = count >= 5
    ? 'Пять заданий выполнено.'
    : good ? 'Цель дня выполнена.'
      : count === 1 ? 'До цели осталось одно задание.'
        : 'Цель дня: два верных ответа.';

  const activity = profile.activity || {streak: 0, recent_days: []};
  const streak = Number(activity.streak) || 0;
  $('streak-count').textContent = streak + ' ' + (streak % 10 === 1 && streak % 100 !== 11 ? 'день' :
    streak % 10 >= 2 && streak % 10 <= 4 && (streak % 100 < 12 || streak % 100 > 14) ? 'дня' : 'дней');
  const activeDays = new Set(activity.recent_days || []);
  const baseDay = new Date(daily.date + 'T12:00:00Z');
  const weekdayNames = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];
  $('streak-days').replaceChildren(...Array.from({length: 7}, (_, index) => {
    const day = new Date(baseDay);
    day.setUTCDate(baseDay.getUTCDate() - 6 + index);
    const iso = day.toISOString().slice(0, 10);
    const item = document.createElement('span');
    item.className = 'streak-day' + (activeDays.has(iso) ? ' active' : '') + (index === 6 ? ' today' : '');
    item.textContent = weekdayNames[day.getUTCDay()];
    item.setAttribute('aria-label', iso + (activeDays.has(iso) ? ': ответ был' : ': ответов не было'));
    return item;
  }));

  const active = Boolean(lesson && !lesson.completed);
  $('resume-view').hidden = !active;
  $('start-mission').hidden = active;
  if (active) {
    $('resume-title').textContent = lesson.topic;
    $('resume-copy').textContent = lesson.subject + ' · шаг ' + (lesson.index + 1) + ' из 5';
  }
  show('dashboard');
}

function setPhase(phase) {
  lessonPhase = Math.max(0, Math.min(2, phase));
  $('lesson-view').querySelector('.mission-sheet-content').scrollTop = 0;
  $('theory-section').hidden = lessonPhase !== 0;
  $('example-section').hidden = lessonPhase !== 1;
  $('question-section').hidden = lessonPhase !== 2;
  $('phase-back').hidden = lessonPhase === 0;
  $('phase-next').hidden = lessonPhase === 2;
  $('phase-next').textContent = lessonPhase === 0 ? 'Показать пример →' : 'Попробовать самому →';
  $('phase-label').textContent = ['Идея · 1/3', 'Пример · 2/3', 'Твой ход · 3/3'][lessonPhase];
  const solved = lesson.can_advance === true || lesson.feedback?.correct === true;
  $('next').hidden = !(lessonPhase === 2 && solved);
  $('lesson-mascot').src = solved ? '/static/mascot-proud.png' : '/static/mascot-focused.png';
  $('buddy-line').textContent = lessonPhase === 0
    ? 'Сначала одна простая идея.'
    : lessonPhase === 1
      ? 'Посмотри на пример по шагам.'
      : solved ? 'Ура! Теперь можем идти дальше.'
        : lesson.feedback ? 'Посмотри на подсказку. Я рядом.'
          : 'Попробуй сам. Ошибаться здесь можно!';
}

function renderFeedback() {
  const feedback = lesson.feedback;
  const solved = lesson.can_advance === true || feedback?.correct === true;
  $('feedback').hidden = !feedback;
  $('answer-form').hidden = solved;
  $('feedback').classList.toggle('needs-work', Boolean(feedback && !feedback.correct));
  if (!feedback) return;
  $('feedback-title').textContent = feedback.correct ? 'Да, всё верно!' : 'Давай подумаем ещё';
  $('feedback-explanation').textContent = feedback.explanation;
  $('feedback-addition').textContent = feedback.addition;
  $('feedback-hint-label').textContent = feedback.correct ? 'Почему это работает' : 'Подсказка Лучика';
  $('feedback-mascot').src = feedback.correct ? '/static/mascot-proud.png' : '/static/mascot-focused.png';
  $('attempt-count').textContent = feedback.correct
    ? 'Шаг пройден'
    : 'Попытка ' + (feedback.attempts || 1) + ' · попробуй ещё раз';
  $('next').textContent = lesson.index === 4 ? 'Завершить урок ✓' : 'Следующий шаг →';
}

function renderLesson() {
  if (lesson.completed) {
    $('complete-topic').textContent = lesson.subject + ' · ' + lesson.topic;
    show('complete');
    return;
  }
  $('lesson-subject').textContent = lesson.subject;
  $('lesson-topic').textContent = lesson.topic;
  $('step-label').textContent = 'Шаг ' + (lesson.index + 1) + ' из 5';
  $('step-progress-track').setAttribute('aria-valuenow', String(lesson.index + 1));
  $('steps').replaceChildren(...Array.from({length: 5}, (_, index) => {
    const item = document.createElement('li');
    const number = document.createElement('span');
    item.className = index < lesson.index ? 'done' : index === lesson.index ? 'active' : '';
    number.textContent = index < lesson.index ? '✓' : String(index + 1);
    item.append(number, document.createTextNode(index === 4 ? 'Итог' : 'Шаг ' + (index + 1)));
    return item;
  }));
  $('block-title').textContent = lesson.block.title;
  $('theory').textContent = lesson.block.theory;
  $('example').textContent = lesson.block.example;
  $('question').textContent = lesson.block.question;
  $('answer').value = '';
  renderFeedback();
  show('lesson-view');
  setPhase(lesson.can_advance ? 2 : 0);
}

async function waitForQuiz(jobId) {
  const deadline = Date.now() + 360000;
  while (Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 2500));
    let job;
    try {
      job = await api('/api/quiz-jobs/' + jobId);
    } catch (e) {
      if (e instanceof TypeError) {
        $('ai-loading-text').textContent = 'Связь прервалась. Лучик продолжает готовить вопросы…';
        continue;
      }
      throw e;
    }
    if (job.status === 'ready') {
      if (!job.quiz) throw new Error('Тест не найден. Открой тему ещё раз.');
      return job.quiz;
    }
    if (job.status === 'error') throw new Error(job.error || 'Не удалось подготовить тест. Попробуй ещё раз.');
  }
  throw new Error('Тест готовится дольше обычного. Попробуй открыть его через минуту.');
}

async function openHistory() {
  const history = await api('/api/history');
  const topics = history.topics || [];
  $('history-list').replaceChildren(...topics.map(item => {
    const button = document.createElement('button');
    const icon = document.createElement('span');
    const copy = document.createElement('span');
    const subject = document.createElement('span');
    const title = document.createElement('strong');
    const detail = document.createElement('small');
    const arrow = document.createElement('span');
    button.type = 'button';
    button.className = 'history-item';
    icon.className = 'history-item-icon';
    icon.textContent = '✓';
    icon.setAttribute('aria-hidden', 'true');
    copy.className = 'history-item-copy';
    subject.textContent = item.subject;
    title.textContent = item.topic;
    const finished = new Intl.DateTimeFormat('ru-RU', {day: 'numeric', month: 'long', timeZone: 'Asia/Yekaterinburg'})
      .format(new Date(item.completed_at * 1000));
    detail.textContent = item.best_score == null
      ? finished + ' · тест из 5 вопросов'
      : finished + ' · лучший результат: ' + item.best_score + ' из 5';
    copy.append(subject, title, detail);
    arrow.className = 'history-item-arrow';
    arrow.textContent = '→';
    arrow.setAttribute('aria-hidden', 'true');
    button.append(icon, copy, arrow);
    button.onclick = () => action(button, 'Готовим…', () => startQuiz(item));
    return button;
  }));
  $('history-empty').hidden = topics.length > 0;
  show('history');
}

async function startQuiz(source) {
  quizSource = source;
  quizChoice = -1;
  quizMatching = [-1, -1, -1, -1];
  quiz = await withAiLoading('Лучик составляет пять вопросов по теме', async () => {
    const job = await api('/api/history/' + source.id + '/quiz-jobs', {});
    return job.status === 'ready' ? job.quiz : waitForQuiz(job.id);
  });
  renderQuiz();
}

function renderQuiz() {
  if (!quiz) return;
  if (quiz.completed) {
    $('quiz-score').textContent = quiz.score + ' из 5';
    $('quiz-score-copy').textContent = quiz.score === 5
      ? 'Все ответы верные! Эту тему ты знаешь отлично.'
      : quiz.score >= 3 ? 'Хорошая работа. К теме можно вернуться в любой момент.'
        : 'Попробуй повторить тему и пройти тест ещё раз.';
    show('quiz-complete');
    return;
  }
  const question = quiz.question;
  const result = quiz.result;
  $('quiz-topic-label').textContent = quizSource?.topic || 'Проверка знаний';
  $('quiz-step-label').textContent = 'Вопрос ' + (quiz.index + 1) + ' из 5';
  $('quiz-progress-track').setAttribute('aria-valuenow', String(quiz.index + 1));
  $('quiz-progress-fill').style.width = ((quiz.index + 1) * 20) + '%';
  $('quiz-kind-label').textContent = question.kind === 'choice' ? 'Выбери один ответ' : 'Соедини пары';
  $('quiz-prompt').textContent = question.prompt;
  const options = $('quiz-options');
  options.replaceChildren();
  if (question.kind === 'choice') {
    question.options.forEach((option, index) => {
      const button = document.createElement('button');
      const letter = document.createElement('span');
      const text = document.createElement('strong');
      button.type = 'button';
      button.className = 'quiz-choice';
      button.disabled = Boolean(result);
      button.classList.toggle('selected', !result && quizChoice === index);
      button.classList.toggle('correct', Boolean(result && result.correct_answer === index));
      button.classList.toggle('wrong', Boolean(result && result.selected === index && !result.correct));
      letter.textContent = 'АБВГ'[index];
      text.textContent = option;
      button.append(letter, text);
      button.onclick = () => { quizChoice = index; renderQuiz(); };
      options.append(button);
    });
  } else {
    const note = document.createElement('p');
    const key = document.createElement('div');
    note.className = 'quiz-match-note';
    note.textContent = 'Выбери номер для каждой буквы. Каждый номер можно использовать один раз.';
    key.className = 'quiz-match-key';
    question.right.forEach((value, index) => {
      const item = document.createElement('span');
      item.textContent = (index + 1) + ' — ' + value;
      key.append(item);
    });
    options.append(note, key);
    question.left.forEach((value, rowIndex) => {
      const row = document.createElement('div');
      const title = document.createElement('span');
      const picks = document.createElement('div');
      row.className = 'quiz-match';
      title.textContent = 'АБВГ'[rowIndex] + ' — ' + value;
      picks.className = 'quiz-match-picks';
      for (let number = 0; number < 4; number++) {
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = String(number + 1);
        button.disabled = Boolean(result);
        button.classList.toggle('selected', !result && quizMatching[rowIndex] === number);
        button.classList.toggle('correct', Boolean(result && result.correct_answer[rowIndex] === number));
        button.classList.toggle('wrong', Boolean(result && result.selected[rowIndex] === number && result.correct_answer[rowIndex] !== number));
        button.onclick = () => {
          quizMatching = quizMatching.map((selected, index) => index !== rowIndex && selected === number ? -1 : selected);
          quizMatching[rowIndex] = number;
          renderQuiz();
        };
        picks.append(button);
      }
      row.append(title, picks);
      options.append(row);
    });
  }
  $('quiz-feedback').hidden = !result;
  $('quiz-feedback').classList.toggle('needs-work', Boolean(result && !result.correct));
  if (result) {
    $('quiz-feedback-title').textContent = result.correct ? 'Верно!' : 'Почти! Посмотри правильный ответ';
    const answer = question.kind === 'choice'
      ? question.options[result.correct_answer]
      : result.correct_answer.map((number, index) => 'АБВГ'[index] + '–' + (number + 1)).join(', ');
    $('quiz-feedback-text').textContent = (result.correct ? '' : 'Ответ: ' + answer + '. ') + result.explanation;
  }
  $('quiz-submit').hidden = Boolean(result);
  $('quiz-submit').disabled = Boolean(result) || (question.kind === 'choice' ? quizChoice < 0 : quizMatching.includes(-1));
  $('quiz-next').hidden = !result;
  $('quiz-next').textContent = quiz.index === 4 ? 'Узнать результат →' : 'Следующий вопрос →';
  show('quiz-view');
}

function selectSubject(name, reveal = false) {
  $('subject').value = name;
  $('current-subject-badge').textContent = name;
  document.querySelectorAll('.subject-card').forEach(card => {
    const active = card.dataset.subject === name;
    card.classList.toggle('active', active);
    card.setAttribute('aria-pressed', String(active));
  });
  if (!reveal) return;
  $('topic').value = '';
  $('generate').disabled = true;
  const topics = config.demo_mode ? ['Обыкновенные дроби'] : (subjectTopics[name] || []);
  $('topic-list-label').textContent = config.demo_mode ? 'Демо-тема' : 'Пять важных тем';
  $('catalog').classList.toggle('demo-topic-stage', config.demo_mode);
  $('topic-picks').replaceChildren(...topics.map((topic, index) => {
    const button = document.createElement('button');
    const number = document.createElement('span');
    const label = document.createElement('strong');
    button.type = 'button';
    button.className = 'topic-pick';
    button.style.setProperty('--topic-index', String(index));
    number.className = 'topic-pick-number';
    number.textContent = String(index + 1).padStart(2, '0');
    label.textContent = topic;
    button.append(number, label, document.createTextNode('→'));
    button.onclick = () => {
      $('topic').value = topic;
      $('generate').disabled = false;
      $('topic-picks').querySelectorAll('.topic-pick').forEach(item => {
        item.classList.toggle('selected', item === button);
        item.setAttribute('aria-pressed', String(item === button));
      });
    };
    button.setAttribute('aria-pressed', 'false');
    return button;
  }));
  $('catalog').classList.add('topic-stage');
  $('topic-form').hidden = false;
  if (config.demo_mode && topics.length) $('topic-picks').firstElementChild.click();
}

function createSubjectCard(subject, index) {
  const card = document.createElement('button');
  const icon = document.createElement('span');
  const label = document.createElement('span');
  card.type = 'button';
  card.className = 'subject-card';
  card.dataset.subject = subject;
  card.setAttribute('aria-pressed', 'false');
  icon.className = 'subject-card-icon';
  icon.setAttribute('aria-hidden', 'true');
  icon.textContent = String(index + 1).padStart(2, '0');
  label.className = 'subject-card-name';
  label.textContent = subject;
  card.append(icon, label);
  card.onclick = () => selectSubject(subject, true);
  return card;
}

$('register').onclick = () => action($('register'), 'Знакомимся…', async () => {
  await api('/api/register', {});
  await dashboard();
});
$('start-mission').onclick = () => {
  $('catalog').classList.remove('topic-stage');
  $('topic-form').hidden = true;
  show('catalog');
};
$('history-open').onclick = () => action($('history-open'), 'Открываем…', openHistory);
$('history-back').onclick = () => action($('history-back'), 'Возвращаемся…', dashboard);
$('history-new').onclick = () => $('start-mission').click();
$('quiz-back').onclick = () => action($('quiz-back'), 'Открываем темы…', openHistory);
$('quiz-to-history').onclick = () => action($('quiz-to-history'), 'Открываем темы…', openHistory);
$('quiz-to-home').onclick = () => action($('quiz-to-home'), 'Возвращаемся…', dashboard);
$('quiz-submit').onclick = () => action($('quiz-submit'), 'Проверяем…', async () => {
  const answer = quiz.question.kind === 'choice'
    ? {index: quiz.index, choice_index: quizChoice}
    : {index: quiz.index, matching_order: quizMatching};
  quiz = await api('/api/quizzes/' + quiz.id + '/answer', answer);
  renderQuiz();
});
$('quiz-next').onclick = () => action($('quiz-next'), 'Дальше…', async () => {
  quiz = await api('/api/quizzes/' + quiz.id + '/next', {index: quiz.index});
  quizChoice = -1;
  quizMatching = [-1, -1, -1, -1];
  renderQuiz();
});
$('change-subject').onclick = () => {
  $('catalog').classList.remove('topic-stage');
  $('topic-form').hidden = true;
};
function openCancelDialog() {
  if (!lesson || lesson.completed) return;
  cancelPreviousFocus = document.activeElement;
  $('cancel-dialog').hidden = false;
  $('app-shell').inert = true;
  $('app-shell').setAttribute('aria-hidden', 'true');
  $('cancel-dialog').focus();
}

function closeCancelDialog() {
  $('cancel-dialog').hidden = true;
  $('app-shell').inert = false;
  $('app-shell').removeAttribute('aria-hidden');
  if (cancelPreviousFocus?.isConnected) cancelPreviousFocus.focus();
  cancelPreviousFocus = null;
}

$('cancel-from-home').onclick = openCancelDialog;
$('cancel-from-lesson').onclick = openCancelDialog;
$('keep-lesson').onclick = closeCancelDialog;
$('cancel-dialog').onkeydown = event => {
  if (event.key === 'Escape') closeCancelDialog();
};
$('confirm-cancel').onclick = () => {
  const lessonId = lesson?.id;
  closeCancelDialog();
  if (!lessonId) return;
  action($('confirm-cancel'), 'Отменяем…', async () => {
    await api('/api/lessons/' + lessonId + '/cancel', {});
    lesson = null;
    await dashboard();
  });
};
$('topic').oninput = () => {
  $('generate').disabled = $('topic').value.trim().length < 2;
  $('topic-picks').querySelectorAll('.topic-pick').forEach(item => {
    item.classList.remove('selected');
    item.setAttribute('aria-pressed', 'false');
  });
};
for (const id of ['back', 'home']) {
  $(id).onclick = () => action($(id), 'Возвращаемся…', dashboard);
}
$('catalog-back').onclick = () => {
  if ($('catalog').classList.contains('topic-stage')) $('change-subject').click();
  else action($('catalog-back'), 'Возвращаемся…', dashboard);
};
$('resume').onclick = () => action($('resume'), 'Открываем…', async () => {
  lesson = await api('/api/lessons/' + lesson.id);
  renderLesson();
});
$('phase-back').onclick = () => setPhase(lessonPhase - 1);
$('phase-next').onclick = () => setPhase(lessonPhase + 1);

$('topic-form').onsubmit = event => {
  event.preventDefault();
  action($('generate'), 'Готовим урок…', async () => {
    lesson = await withAiLoading('Лучик проверяет тему и готовит пять шагов', async () => {
      const job = await api('/api/lesson-jobs', {
        subject: $('subject').value,
        topic: $('topic').value.trim()
      });
      return waitForLesson(job.id);
    });
    renderLesson();
  });
};

$('simplify').onclick = () => action($('simplify'), 'Объясняю…', async () => {
  const answer = $('answer').value;
  lesson = await withAiLoading('Лучик ищет слова попроще', () => api('/api/lessons/' + lesson.id + '/simplify', {
    block_index: lesson.index
  }));
  $('theory').textContent = lesson.block.theory;
  $('example').textContent = lesson.block.example;
  $('answer').value = answer;
  renderFeedback();
  setPhase(0);
  $('buddy-line').textContent = 'Вот ещё проще. Посмотри!';
});

$('answer-form').onsubmit = event => {
  event.preventDefault();
  action($('check'), 'Проверяю…', async () => {
    const answer = $('answer').value.trim();
    lesson = await withAiLoading('Лучик внимательно читает твой ответ', () => api('/api/lessons/' + lesson.id + '/answer', {
      block_index: lesson.index,
      answer
    }));
    renderFeedback();
    setPhase(2);
  });
};

$('next').onclick = () => action($('next'), 'Следующий шаг…', async () => {
  lesson = await api('/api/lessons/' + lesson.id + '/next', {block_index: lesson.index});
  renderLesson();
});

async function start() {
  try {
    config = await api('/api/config');
    $('demo-banner').hidden = !config.demo_mode;
    $('subject-chips').replaceChildren();
    $('subject').replaceChildren();
    (config.demo_mode ? ['Математика'] : config.subjects).forEach((subject, index) => {
      const option = document.createElement('option');
      option.value = subject;
      option.textContent = subject;
      $('subject').append(option);
      $('subject-chips').append(createSubjectCard(subject, index));
    });
    if (config.subjects.length) selectSubject(config.subjects[0]);
    $('subject').onchange = () => selectSubject($('subject').value, true);
    if (config.demo_mode) {
      selectSubject('Математика');
      $('subject').disabled = true;
      $('topic').readOnly = true;
    }

    const bridge = window.WebApp;
    bridge?.ready?.();
    const initData = bridge?.initData || new URLSearchParams(location.hash.slice(1)).get('WebAppData');
    if (initData) {
      token = (await api('/api/auth/max', {init_data: initData})).token;
    } else if (config.demo_mode) {
      token = (await api('/api/auth/demo', {})).token;
    } else if (!token) {
      show('welcome');
      $('register').disabled = true;
      $('registration-note').textContent = 'Открой мини-приложение из MAX, чтобы войти.';
      return;
    }
    sessionStorage.setItem('ponyato-token', token);
    await dashboard();
    const hashParams = new URLSearchParams(location.hash.slice(1));
    const startParam = bridge?.initDataUnsafe?.start_param || bridge?.start_param || new URLSearchParams(location.search).get('WebAppStartParam') || hashParams.get('WebAppStartParam');
    if (startParam && startParam.startsWith('prep_')) {
      const prepToken = startParam.slice(5);
      lesson = await withAiLoading('Лучик готовит тему к проверочной', async () => {
        const opened = await api('/api/preparations/open', {token: prepToken});
        return opened.status === 'ready' ? opened.lesson : waitForLesson(opened.id);
      });
      renderLesson();
    } else if (profile.generation_job) {
      lesson = await withAiLoading('Лучик заканчивает готовить урок', () => waitForLesson(profile.generation_job.id));
      renderLesson();
    }
  } catch (e) {
    $('loading').hidden = true;
    error(e.message || 'Сервер временно недоступен. Обнови страницу.');
  }
}

start();
