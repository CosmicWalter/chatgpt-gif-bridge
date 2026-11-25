const $ = (selector) => document.querySelector(selector);
const labels = {queued: '排队中', running: '生成中', completed: '已完成', failed: '失败', waiting_verification: '等待登录 / 验证'};
let token = sessionStorage.getItem('bridge-management-key') || localStorage.getItem('bridge-management-key') || '';
let apiKeys = [];
let editedKey = null;
let deletedKey = null;
const keyPath = '/v8/management/config/access/api-keys';
let lastSnapshot = '';
let previewUrls = [];
let refreshing = false;
let generation = 0;
let toastTimer;

function notify(message) {
  $('#message').textContent = message;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $('#message').textContent = ''; }, 6000);
}

async function request(path, options = {}, asBlob = false) {
  const response = await fetch(path, {
    ...options,
    headers: {...options.headers, Authorization: `Bearer ${token}`},
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    if (response.status === 401) disconnect();
    throw new Error(data.error?.message || data.error || `请求失败（${response.status}）`);
  }
  return asBlob ? response.blob() : response.json();
}

function disconnect() {
  generation++;
  token = '';
  sessionStorage.removeItem('bridge-management-key');
  localStorage.removeItem('bridge-management-key');
  apiKeys = [];
  editedKey = null;
  deletedKey = null;
  $('#edit-key-dialog').close();
  $('#delete-key-dialog').close();
  $('#edit-api-key').value = '';
  $('#new-api-key').value = '';
  $('#management-key-form').reset();
  $('#api-key-list').replaceChildren();
  $('#sidebar').hidden = true;
  document.body.classList.add('signed-out');
  $('#workspace').hidden = true;
  $('#login').hidden = false;
  $('#disconnect').hidden = true;
  $('#connection').textContent = '尚未连接';
  $('#connection').className = 'connection';
  $('#job-list').replaceChildren();
  previewUrls.forEach(URL.revokeObjectURL);
  previewUrls = [];
  lastSnapshot = '';
}

function showSession(session) {
  const names = {verification_required:'需要真人验证', starting:'启动中', ready:'已连接', busy:'生成中', login_required:'未登录', opening_login:'打开登录窗口', signing_in:'等待登录', checking:'验证登录', error:'连接异常'};
  $('#account-status').textContent = names[session.status] || session.status;
  $('#dashboard-status').textContent = names[session.status] || session.status;
  $('#account-status').className = 'status ' + (session.status === 'ready' ? 'completed' : '');
  $('#account-login').disabled = !['ready', 'verification_required', 'login_required', 'error'].includes(session.status);
  $('#account-finish').hidden = session.status !== 'signing_in';
  $('#submit-job').disabled = !['ready', 'busy'].includes(session.status);
  $('#account-help').textContent = session.error || (session.status === 'signing_in'
    ? '请在窗口中登录 Plus 账号或完成真人验证，再点击“已完成登录 / 验证”。'
    : session.status === 'ready' || session.status === 'busy'
      ? '登录状态已保存，生成任务复用同一个 Chrome 会话，可最小化窗口。'
      : '首次使用请打开登录窗口，登录支持 GIF 的 ChatGPT Plus 账号。');
}

async function connect() {
  await request('/v8/management/status');
  sessionStorage.setItem('bridge-management-key', token);
  if ($('#remember-key').checked) localStorage.setItem('bridge-management-key', token);
  else localStorage.removeItem('bridge-management-key');
  $('#sidebar').hidden = false;
  document.body.classList.remove('signed-out');
  $('#api-base').value = location.origin + '/v1';
  $('#login').hidden = true;
  $('#workspace').hidden = false;
  $('#disconnect').hidden = false;
  $('#connection').textContent = '服务已连接';
  $('#connection').className = 'connection online';
  lastSnapshot = '';
  await Promise.all([refresh(), loadKeys()]);
  showPage();
}

function addButton(container, text, action, secondary = true) {
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = text;
  if (secondary) button.className = 'secondary';
  button.addEventListener('click', async () => {
    button.disabled = true;
    try { await action(); } catch (error) { notify(error.message); }
    finally { button.disabled = false; }
  });
  container.append(button);
  return button;
}

async function renderJobs(jobs) {
  const currentGeneration = ++generation;
  previewUrls.forEach(URL.revokeObjectURL);
  previewUrls = [];
  $('#job-list').replaceChildren();
  $('#empty').hidden = jobs.length > 0;
  $('#job-count').textContent = jobs.length;
  for (const job of jobs) {
    const card = $('#job-template').content.firstElementChild.cloneNode(true);
    card.querySelector('.job-mode').textContent = 'ChatGPT · 普通 Chrome';
    const status = card.querySelector('.status');
    status.textContent = labels[job.status] || job.status;
    status.classList.add(job.status);
    card.querySelector('.job-prompt').textContent = job.prompt || 'GIF 任务';
    if (job.error) {
      const error = card.querySelector('.job-error');
      error.hidden = false;
      error.textContent = job.error;
    }
    const actions = card.querySelector('.job-actions');
    const path = `/v1/gif/jobs/${job.id}`;
    if (job.status === 'completed') {
      addButton(actions, '下载 GIF', async () => {
        const blob = await request(path + '/result', {}, true);
        const url = URL.createObjectURL(blob);
        const download = document.createElement('a');
        download.href = url;
        download.download = 'animation.gif';
        download.click();
        setTimeout(() => URL.revokeObjectURL(url), 30000);
      }, false);
      request(path + '/result', {}, true).then((blob) => {
        if (generation !== currentGeneration || !card.isConnected) return;
        const url = URL.createObjectURL(blob);
        previewUrls.push(url);
        const image = card.querySelector('.preview');
        image.src = url;
        image.hidden = false;
      }).catch((error) => notify(error.message));
    }
    if (!['queued', 'running', 'waiting_verification'].includes(job.status)) {
      addButton(actions, '移除记录', async () => {
        await request(path, {method: 'DELETE'});
        await refresh();
      });
    }
    $('#job-list').append(card);
  }
}

async function refresh() {
  if (!token || refreshing) return;
  refreshing = true;
  try {
    const [data, session] = await Promise.all([request('/v1/gif/jobs'), request('/session')]);
    showSession(session);
    const snapshot = JSON.stringify(data.jobs);
    if (snapshot !== lastSnapshot) {
      lastSnapshot = snapshot;
      await renderJobs(data.jobs);
    }
  } finally { refreshing = false; }
}

$('#login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const submit = $('#login-form button[type=submit]');
  submit.disabled = true;
  $('#login-error').textContent = '';
  token = $('#token').value.trim();
  try { await connect(); $('#token').value = ''; }
  catch (error) { $('#login-error').textContent = error.message; }
  finally { submit.disabled = false; }
});
$('#disconnect').addEventListener('click', disconnect);
$('#refresh').addEventListener('click', () => refresh().catch((error) => notify(error.message)));
$('#account-login').addEventListener('click', async () => {
  $('#account-login').disabled = true;
  try { showSession(await request('/session/login', {method:'POST'})); }
  catch (error) { notify(error.message); }
  finally { await refresh().catch((error) => notify(error.message)); }
});
$('#account-finish').addEventListener('click', async () => {
  $('#account-finish').disabled = true;
  try { showSession(await request('/session/finish', {method:'POST'})); }
  catch (error) { notify(error.message); }
  finally { $('#account-finish').disabled = false; await refresh().catch((error) => notify(error.message)); }
});
$('#create-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('#submit-job').disabled = true;
  try {
    await request('/v1/gif/jobs', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({prompt: $('#prompt').value.trim()}),
    });
    $('#prompt').value = '';
    location.hash = 'jobs';
    showPage();
    await refresh();
    notify('任务已加入队列');
  } catch (error) { notify(error.message); }
  finally { await refresh().catch((error) => notify(error.message)); }
});
$('#login-address').value = location.origin;
$('#remember-key').checked = Boolean(localStorage.getItem('bridge-management-key'));
if (token) connect().catch((error) => { $('#login-error').textContent = error.message; });
setInterval(() => refresh().catch((error) => notify(error.message)), 5000);


function showPage() {
  const titles = {dashboard:'仪表盘', keys:'API 密钥', account:'账号认证', jobs:'任务记录', system:'系统设置'};
  const requested = location.hash.slice(1);
  const page = Object.hasOwn(titles, requested) ? requested : 'dashboard';
  document.querySelectorAll('[data-view]').forEach((el) => { el.hidden = el.dataset.view !== page; });
  document.querySelectorAll('[data-page]').forEach((el) => el.classList.toggle('selected', el.dataset.page === page));
  $('#page-title').textContent = titles[page];
  $('#page-breadcrumb').textContent = titles[page];
}
window.addEventListener('hashchange', showPage);
showPage();

async function loadKeys() {
  const data = await request(keyPath);
  apiKeys = data['api-keys'];
  $('#key-count').textContent = apiKeys.length;
  $('#keys-empty').hidden = apiKeys.length !== 0;
  $('#api-key-list').replaceChildren();
  apiKeys.forEach((key, index) => {
    const row = document.createElement('div');
    row.className = 'key-row';
    const number = document.createElement('span');
    number.className = 'key-number';
    number.textContent = String(index + 1).padStart(2, '0');
    const input = document.createElement('input');
    input.type = 'password';
    input.readOnly = true;
    input.value = key;
    input.setAttribute('aria-label', 'API key ' + (index + 1));
    row.append(number, input);
    const reveal = addButton(row, '显示', () => {
      input.type = input.type === 'password' ? 'text' : 'password';
      reveal.textContent = input.type === 'password' ? '显示' : '隐藏';
    });
    addButton(row, '复制', async () => { await navigator.clipboard.writeText(key); notify('API key 已复制'); });
    addButton(row, '编辑', () => { editedKey = key; $('#edit-api-key').value = key; $('#edit-key-dialog').showModal(); });
    const remove = addButton(row, '删除', () => { deletedKey = key; $('#delete-key-dialog').showModal(); });
    remove.classList.add('danger');
    $('#api-key-list').append(row);
  });
}
async function mutateKey(method, body) {
  await request(keyPath, {method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  await loadKeys();
}
$('#generate-api-key').addEventListener('click', async () => {
  const button = $('#generate-api-key');
  button.disabled = true;
  try { await mutateKey('POST', {}); notify('API key 已生成'); }
  catch (error) { notify(error.message); }
  finally { button.disabled = false; }
});
$('#add-key-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  try { await mutateKey('POST', {key:$('#new-api-key').value.trim()}); $('#new-api-key').value = ''; notify('API key 已添加'); }
  catch (error) { notify(error.message); }
});
$('#edit-key-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  try { await mutateKey('PATCH', {old:editedKey, new:$('#edit-api-key').value.trim()}); $('#edit-key-dialog').close(); $('#edit-api-key').value = ''; editedKey = null; notify('API key 已更新'); }
  catch (error) { notify(error.message); }
});
$('#cancel-edit-key').addEventListener('click', () => { $('#edit-key-dialog').close(); $('#edit-api-key').value = ''; editedKey = null; });
$('#cancel-delete-key').addEventListener('click', () => { $('#delete-key-dialog').close(); deletedKey = null; });
$('#confirm-delete-key').addEventListener('click', async () => {
  try { await mutateKey('DELETE', {key:deletedKey}); $('#delete-key-dialog').close(); deletedKey = null; notify('API key 已删除'); }
  catch (error) { notify(error.message); }
});
$('#toggle-login-key').addEventListener('click', () => {
  const visible = $('#token').type === 'password';
  $('#token').type = visible ? 'text' : 'password';
  $('#toggle-login-key').textContent = visible ? '隐藏' : '显示';
  $('#toggle-login-key').setAttribute('aria-label', visible ? '隐藏管理密钥' : '显示管理密钥');
});
$('#management-key-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const next = $('#new-management-key').value;
  if (next !== $('#confirm-management-key').value) { notify('两次输入的新管理密钥不一致'); return; }
  try {
    await request('/v8/management/management-key', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({current_key:$('#current-management-key').value, new_key:next})});
    $('#management-key-form').reset();
    disconnect();
    notify('管理密钥已修改，请使用新密钥登录');
  } catch (error) { notify(error.message); }
});
