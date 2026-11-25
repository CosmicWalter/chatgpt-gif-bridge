const $ = (selector) => document.querySelector(selector);
const labels = {queued: '排队中', running: '生成中', completed: '已完成', failed: '失败', waiting_verification: '等待登录 / 验证'};
let token = sessionStorage.getItem('bridge-token') || '';
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
    throw new Error(data.error || `请求失败（${response.status}）`);
  }
  return asBlob ? response.blob() : response.json();
}

function disconnect() {
  generation++;
  token = '';
  sessionStorage.removeItem('bridge-token');
  $('#api-key').value = '';
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
  $('#account-status').className = 'status ' + (session.status === 'ready' ? 'completed' : '');
  $('#account-login').disabled = !['ready', 'verification_required', 'login_required', 'error'].includes(session.status);
  $('#account-finish').hidden = session.status !== 'signing_in';
  $('#submit-job').disabled = !['ready', 'busy'].includes(session.status);
  $('#account-help').textContent = session.error || (session.status === 'signing_in'
    ? '请在窗口中登录 Plus 账号或完成真人验证，再点击“已完成登录 / 验证”。'
    : session.status === 'ready' || session.status === 'busy'
      ? '登录状态已保存，生成任务使用无头浏览器。'
      : '首次使用请打开登录窗口，登录支持 GIF 的 ChatGPT Plus 账号。');
}

async function connect() {
  const health = await request('/health');
  sessionStorage.setItem('bridge-token', token);
  $('#api-base').value = location.origin + '/v1';
  $('#api-key').value = token;
  $('#login').hidden = true;
  $('#workspace').hidden = false;
  $('#disconnect').hidden = false;
  $('#connection').textContent = '服务已连接';
  $('#connection').className = 'connection online';
  lastSnapshot = '';
  await refresh();
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
    card.querySelector('.job-mode').textContent = 'ChatGPT · 无头浏览器';
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
  token = $('#token').value.trim();
  try { await connect(); $('#token').value = ''; }
  catch (error) { notify(error.message); }
});
$('#disconnect').addEventListener('click', disconnect);
$('#copy-api-key').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(token); notify('API key 已复制'); }
  catch { notify('复制失败，请手动选择密钥复制'); }
});
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
    await refresh();
    notify('任务已加入队列');
  } catch (error) { notify(error.message); }
  finally { await refresh().catch((error) => notify(error.message)); }
});
if (token) connect().catch((error) => notify(error.message));
setInterval(() => refresh().catch((error) => notify(error.message)), 5000);
