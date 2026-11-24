const $ = (selector) => document.querySelector(selector);
const labels = {queued: '排队中', running: '生成中', awaiting_upload: '等待上传 GIF', completed: '已完成', failed: '失败'};
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

function updateModeHint() {
  const api = document.querySelector('input[name=backend]:checked').value === 'api';
  $('#mode-hint').textContent = api
    ? '使用官方 Images API 自动生成，消耗 API 项目额度。'
    : '网页模式需要你在 ChatGPT 中提交提示词、下载并上传 GIF。';
}

async function connect() {
  const health = await request('/health');
  sessionStorage.setItem('bridge-token', token);
  $('#login').hidden = true;
  $('#workspace').hidden = false;
  $('#disconnect').hidden = false;
  $('#connection').textContent = '服务已连接';
  $('#connection').className = 'connection online';
  $('#api-option').disabled = !health.api_enabled;
  $('#api-note').textContent = health.api_enabled ? '自动生成 · API 项目计费' : '需在服务端配置 OPENAI_API_KEY';
  if (!health.api_enabled) document.querySelector('input[value=web]').checked = true;
  updateModeHint();
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
    card.querySelector('.job-mode').textContent = job.backend === 'api' ? '官方 API' : 'ChatGPT 网页';
    const status = card.querySelector('.status');
    status.textContent = labels[job.status] || job.status;
    status.classList.add(job.status);
    card.querySelector('.job-prompt').textContent = job.prompt || 'GIF 创作';
    if (job.error) {
      const error = card.querySelector('.job-error');
      error.hidden = false;
      error.textContent = job.error;
    }
    const actions = card.querySelector('.job-actions');
    const path = `/v1/gif/jobs/${job.id}`;
    if (job.status === 'awaiting_upload') {
      addButton(actions, '复制提示词', async () => {
        await navigator.clipboard.writeText('请生成一个可下载的原生循环 GIF。' + job.prompt);
        notify('提示词已复制');
      });
      const open = document.createElement('a');
      open.href = 'https://chatgpt.com/';
      open.target = '_blank';
      open.rel = 'noreferrer';
      open.className = 'action-link secondary';
      open.textContent = '打开 ChatGPT ↗';
      actions.append(open);
      const upload = document.createElement('input');
      upload.type = 'file';
      upload.accept = '.gif,image/gif';
      upload.hidden = true;
      upload.setAttribute('aria-label', '上传生成的 GIF');
      const uploadButton = addButton(actions, '上传 GIF', async () => upload.click(), false);
      upload.addEventListener('change', async () => {
        const file = upload.files[0];
        if (!file) return;
        if (file.size > 25 * 1024 * 1024) return notify('文件需小于 25 MiB');
        uploadButton.disabled = true;
        try {
          await request(path + '/result', {method: 'PUT', body: file, headers: {'Content-Type': 'image/gif'}});
          notify('GIF 已上传');
          await refresh();
        } catch (error) { notify(error.message); }
        finally { uploadButton.disabled = false; upload.value = ''; }
      });
      actions.append(upload);
    }
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
    if (!['queued', 'running'].includes(job.status)) {
      addButton(actions, job.status === 'awaiting_upload' ? '取消任务' : '移除记录', async () => {
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
    const data = await request('/v1/gif/jobs');
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
$('#refresh').addEventListener('click', () => refresh().catch((error) => notify(error.message)));
document.querySelectorAll('input[name=backend]').forEach((input) => input.addEventListener('change', updateModeHint));
$('#create-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('#submit-job').disabled = true;
  try {
    const backend = document.querySelector('input[name=backend]:checked').value;
    await request('/v1/gif/jobs', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({backend, prompt: $('#prompt').value.trim()}),
    });
    $('#prompt').value = '';
    await refresh();
    notify(backend === 'web' ? '任务已创建，请打开 ChatGPT 生成后上传 GIF' : '任务已加入生成队列');
  } catch (error) { notify(error.message); }
  finally { $('#submit-job').disabled = false; }
});
if (token) connect().catch((error) => notify(error.message));
setInterval(() => refresh().catch((error) => notify(error.message)), 5000);
