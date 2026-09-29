'use strict';

const $ = id => document.getElementById(id);
let selectedStudent = null;
let entries = [];
let editingId = null;
let searchQuery = '';
let nextOffset = 0;
let searchTimer = null;
const dateLabel = new Intl.DateTimeFormat('ru-RU', {weekday: 'long', day: 'numeric', month: 'long'});

function node(tag, className = '', text = '') {
  const item = document.createElement(tag);
  if (className) item.className = className;
  item.textContent = text;
  return item;
}

function showError(id, message) {
  $(id).textContent = message || '';
  $(id).hidden = !message;
}

function showLogin() {
  $('login-screen').hidden = false;
  $('workspace').hidden = true;
  $('logout').hidden = true;
  $('schedule-content').hidden = true;
  $('empty-state').hidden = false;
  $('students-list').replaceChildren();
  selectedStudent = null;
  entries = [];
}

function showWorkspace() {
  $('login-screen').hidden = true;
  $('workspace').hidden = false;
  $('logout').hidden = false;
}

async function request(path, options = {}) {
  const headers = {...(options.headers || {})};
  if (options.method && options.method !== 'GET') headers['X-Admin-Action'] = '1';
  if (options.body) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, {credentials: 'same-origin', ...options, headers});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && path !== '/api/admin/login') showLogin();
    throw new Error(typeof data.detail === 'string' ? data.detail : 'Не удалось выполнить запрос.');
  }
  return data;
}

function studentButton(student) {
  const button = node('button', 'student-item');
  button.type = 'button';
  button.dataset.studentId = String(student.id);
  button.classList.toggle('selected', selectedStudent?.id === student.id);
  const label = node('span');
  label.append(node('strong', '', student.name), node('small', '', `${student.grade} класс · ID ${student.id}`));
  button.append(label, node('span', 'chevron', '›'));
  button.onclick = () => selectStudent(student.id);
  return button;
}

async function loadStudents(reset = true) {
  const query = searchQuery;
  const offset = reset ? 0 : nextOffset;
  const data = await request(`/api/admin/students?q=${encodeURIComponent(query)}&offset=${offset}`);
  if (query !== searchQuery) return;
  if (reset) $('students-list').replaceChildren();
  if (reset && !data.students.length) $('students-list').append(node('p', 'muted', 'Ученики не найдены.'));
  $('students-list').append(...data.students.map(studentButton));
  nextOffset = offset + data.students.length;
  $('load-more').hidden = !data.has_more;
}

function scheduleRow(entry) {
  const row = node('article', 'entry-row');
  row.append(node('span', 'period', String(entry.period).padStart(2, '0')));
  const main = node('div', 'entry-main');
  main.append(node('strong', '', entry.subject));
  if (entry.assessment) main.append(node('span', 'assessment', 'Проверочная'));
  main.append(node('p', '', entry.topic));
  if (entry.homework) main.append(node('p', 'homework', entry.homework));
  row.append(main);
  const actions = node('div', 'entry-actions');
  const edit = node('button', '', 'Изменить');
  edit.type = 'button';
  edit.setAttribute('aria-label', `Изменить ${entry.subject}, ${entry.topic}`);
  edit.onclick = () => openEditor(entry);
  const remove = node('button', 'delete', 'Удалить');
  remove.type = 'button';
  remove.setAttribute('aria-label', `Удалить ${entry.subject}, ${entry.topic}`);
  remove.onclick = () => deleteEntry(entry);
  actions.append(edit, remove);
  row.append(actions);
  return row;
}

function renderSchedule() {
  $('schedule-list').replaceChildren();
  if (!entries.length) {
    $('schedule-list').append(node('p', 'no-entries', 'Записей пока нет. Добавь первый урок в расписание.'));
    return;
  }
  const groups = new Map();
  for (const entry of entries) {
    if (!groups.has(entry.day)) groups.set(entry.day, []);
    groups.get(entry.day).push(entry);
  }
  for (const [day, rows] of groups) {
    const section = node('section', 'day-group');
    section.append(node('h3', '', dateLabel.format(new Date(`${day}T12:00:00`))));
    const list = node('div', 'entry-list');
    list.append(...rows.map(scheduleRow));
    section.append(list);
    $('schedule-list').append(section);
  }
}

async function selectStudent(uid) {
  showError('schedule-error', '');
  try {
    const data = await request(`/api/admin/students/${uid}/schedule`);
    selectedStudent = data.student;
    entries = data.entries;
    $('empty-state').hidden = true;
    $('schedule-content').hidden = false;
    $('schedule-title').textContent = selectedStudent.name;
    $('student-meta').textContent = `${selectedStudent.grade} класс · ID ${selectedStudent.id}`;
    document.querySelectorAll('.student-item').forEach(button => {
      button.classList.toggle('selected', button.dataset.studentId === String(selectedStudent.id));
    });
    renderSchedule();
  } catch (error) {
    showError('schedule-error', error.message);
  }
}

function localToday() {
  const date = new Date();
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

function openEditor(entry = null) {
  editingId = entry?.id || null;
  $('editor-title').textContent = entry ? 'Изменить запись' : 'Новая запись';
  $('entry-day').value = entry?.day || localToday();
  $('entry-period').value = entry?.period || 1;
  $('entry-subject').value = entry?.subject || '';
  $('entry-topic').value = entry?.topic || '';
  $('entry-homework').value = entry?.homework || '';
  $('entry-assessment').checked = Boolean(entry?.assessment);
  showError('editor-error', '');
  $('editor').showModal();
  $('entry-day').focus();
}

async function deleteEntry(entry) {
  if (!selectedStudent || !window.confirm(`Удалить «${entry.subject} — ${entry.topic}» из расписания?`)) return;
  try {
    await request(`/api/admin/students/${selectedStudent.id}/schedule/${entry.id}`, {method: 'DELETE'});
    await selectStudent(selectedStudent.id);
  } catch (error) {
    showError('schedule-error', error.message);
  }
}

$('login-form').onsubmit = async event => {
  event.preventDefault();
  const button = $('login-form').querySelector('button');
  button.disabled = true;
  showError('login-error', '');
  try {
    await request('/api/admin/login', {method: 'POST', body: JSON.stringify({password: $('password').value})});
    $('password').value = '';
    showWorkspace();
    await loadStudents();
  } catch (error) {
    showError('login-error', error.message);
  } finally {
    button.disabled = false;
  }
};

$('logout').onclick = async () => {
  try {
    await request('/api/admin/logout', {method: 'POST'});
    showLogin();
  } catch (error) {
    showError('schedule-error', error.message);
  }
};

$('student-search').oninput = () => {
  clearTimeout(searchTimer);
  searchQuery = $('student-search').value.trim();
  searchTimer = setTimeout(() => loadStudents().catch(error => showError('schedule-error', error.message)), 250);
};

$('load-more').onclick = () => loadStudents(false).catch(error => showError('schedule-error', error.message));
$('add-entry').onclick = () => openEditor();
$('close-editor').onclick = () => $('editor').close();
$('cancel-editor').onclick = () => $('editor').close();

$('entry-form').onsubmit = async event => {
  event.preventDefault();
  if (!selectedStudent) return;
  const button = $('save-entry');
  button.disabled = true;
  showError('editor-error', '');
  const body = {
    day: $('entry-day').value,
    period: Number($('entry-period').value),
    subject: $('entry-subject').value.trim(),
    topic: $('entry-topic').value.trim(),
    homework: $('entry-homework').value.trim(),
    assessment: $('entry-assessment').checked,
  };
  const base = `/api/admin/students/${selectedStudent.id}/schedule`;
  try {
    await request(editingId ? `${base}/${editingId}` : base, {
      method: editingId ? 'PUT' : 'POST', body: JSON.stringify(body),
    });
    $('editor').close();
    await selectStudent(selectedStudent.id);
  } catch (error) {
    showError('editor-error', error.message);
  } finally {
    button.disabled = false;
  }
};

request('/api/admin/me').then(() => {
  showWorkspace();
  return loadStudents();
}).catch(() => showLogin());
