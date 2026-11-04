const call = (action, payload) => new Promise((resolve) => {
  chrome.runtime.sendMessage({ action, payload }, (response) => resolve(response || {}));
});

const $ = (id) => document.getElementById(id);

function say(text) { $('status').textContent = text; }

const MODE_HINT = {
  auto: '会先尝试读字幕（借用你浏览器里已有的登录态）；读不到就自动改成截画面做本地识别。',
  subtitle: '只读字幕。需要你在这个浏览器里已经登录 B 站，否则 AI 字幕拿不到。',
  ocr: '完全不借用登录态：不请求字幕接口，只截取播放器画面做本地文字识别。慢一些，但不需要登录。',
};

function updateHint() {
  $('modehint').textContent = MODE_HINT[$('mode').value] || '';
}

async function loadCourses() {
  say('正在读取课程…');
  const config = await call('config');
  if (config && config.apiBase) $('api').value = config.apiBase;
  const result = await call('courses');
  if (result.error) {
    say('读不到知页：' + result.error + '\n确认后端已启动（uvicorn --port 8000）。');
    $('course').innerHTML = '<option value="">（读取失败）</option>';
    return;
  }
  if (!result.courses || !result.courses.length) {
    say('知页里还没有课程，先在那里建一门。');
    $('course').innerHTML = '<option value="">（没有课程）</option>';
    return;
  }
  if (config && config.accessMode) $('mode').value = config.accessMode;
  updateHint();
  const current = (config && config.courseId) || '';
  $('course').innerHTML = result.courses
    .map((item) => `<option value="${item.id}"${item.id === current ? ' selected' : ''}>${item.name}</option>`)
    .join('');
  say(`读到 ${result.courses.length} 门课程`);
}

$('mode').onchange = updateHint;

$('save').onclick = async () => {
  const apiBase = $('api').value.trim() || 'http://127.0.0.1:8000';
  const courseId = $('course').value;
  if (!courseId) return say('先选一门课程');
  await call('config', { apiBase, courseId, accessMode: $('mode').value });
  const opened = await call('openPanel');
  if (opened && opened.error) return say(opened.error);
  window.close();
};

$('reload').onclick = () => loadCourses();

loadCourses();
