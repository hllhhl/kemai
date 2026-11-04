/**
 * 知页 · 视频伴侣｜后台
 *
 * 三件事：
 *   proxyFetch —— 带登录态代发 B 站接口（content script 受 CORS 限制，也读不到 HttpOnly SESSDATA）
 *   api        —— 转发到本地知页（127.0.0.1:8000），WBI 签名由后端出，扩展里不实现 MD5
 *   状态       —— 记住当前课程与 API 地址
 *
 * 合规：只请求公开可见的字幕与播放器接口，不请求视频源、不绕过任何付费或会员限制。
 */

const DEFAULT_API = 'http://127.0.0.1:8000';

// 三种获取方式，代价不同，交给用户自己选：
//   auto    —— 先试字幕（借用浏览器已有登录态），失败自动降级到画面 OCR
//   subtitle—— 只走字幕，不发画面
//   ocr     —— 从不借用登录态，只截画面做本地 OCR（隐私最保守）
const ACCESS_MODES = ['auto', 'subtitle', 'ocr'];

async function getConfig() {
  const stored = await chrome.storage.local.get({ apiBase: DEFAULT_API, courseId: '', accessMode: 'auto' });
  const mode = ACCESS_MODES.includes(stored.accessMode) ? stored.accessMode : 'auto';
  return {
    apiBase: String(stored.apiBase || DEFAULT_API).replace(/\/$/, ''),
    courseId: String(stored.courseId || ''),
    accessMode: mode,
  };
}

async function proxyFetch({ url, headers = {} }) {
  const { accessMode } = await getConfig();
  // ocr 模式下不携带任何凭证：不借用登录态，请求按匿名处理。
  const response = await fetch(url, {
    method: 'GET',
    credentials: accessMode === 'ocr' ? 'omit' : 'include',
    headers: {
      'User-Agent': navigator.userAgent,
      'Referer': 'https://www.bilibili.com/',
      ...headers,
    },
  });
  return { status: response.status, text: await response.text() };
}

async function callApi(path, init = {}) {
  const { apiBase } = await getConfig();
  const response = await fetch(`${apiBase}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init.headers || {}) },
  });
  const text = await response.text();
  let payload;
  try { payload = text ? JSON.parse(text) : {}; } catch { payload = { detail: text }; }
  if (!response.ok) {
    const detail = payload && payload.detail;
    throw new Error(typeof detail === 'string' ? detail : (detail && detail.message) || `请求失败（${response.status}）`);
  }
  return payload;
}

async function importTranscript({ bvid, cid, part, partTitle, lines }) {
  const { courseId } = await getConfig();
  if (!courseId) throw new Error('还没选课程。点扩展图标，先选一门知页里的课程。');
  return callApi(`/courses/${encodeURIComponent(courseId)}/finder/import-transcript`, {
    method: 'POST',
    body: JSON.stringify({ bvid, cid, part, part_title: partTitle || '', lines }),
  });
}

async function digestPart({ bvid, part, partTitle }) {
  const { courseId } = await getConfig();
  if (!courseId) throw new Error('还没选课程');
  return callApi(`/courses/${encodeURIComponent(courseId)}/finder/digest-part`, {
    method: 'POST',
    body: JSON.stringify({ bvid, part, part_title: partTitle || '' }),
  });
}

async function visualCheck({ bvid, part, frames }) {
  const { courseId } = await getConfig();
  if (!courseId) throw new Error('还没选课程');
  return callApi(`/courses/${encodeURIComponent(courseId)}/finder/visual-check`, {
    method: 'POST',
    body: JSON.stringify({ bvid, part, frames }),
  });
}

async function ocrFrames({ bvid, part, partTitle, frames }) {
  const { courseId } = await getConfig();
  if (!courseId) throw new Error('还没选课程');
  return callApi(`/courses/${encodeURIComponent(courseId)}/finder/ocr-frames`, {
    method: 'POST',
    body: JSON.stringify({ bvid, part, part_title: partTitle || '', frames }),
  });
}

async function sign({ bvid, cid }) {
  const { courseId } = await getConfig();
  if (!courseId) throw new Error('还没选课程');
  const payload = await callApi(`/courses/${encodeURIComponent(courseId)}/finder/sign?bvid=${encodeURIComponent(bvid)}&cid=${cid}`);
  return payload.query;
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  const payload = message && message.payload;
  (async () => {
    switch (message.action) {
      case 'proxyFetch': return await proxyFetch(payload);
      case 'sign': return { query: await sign(payload) };
      case 'importTranscript': return await importTranscript(payload);
      case 'digestPart': return await digestPart(payload);
      case 'visualCheck': return await visualCheck(payload);
      case 'ocrFrames': return await ocrFrames(payload);
      case 'courses': {
        const { apiBase } = await getConfig();
        const list = await fetch(`${apiBase}/courses`).then((r) => r.json());
        return { apiBase, courses: list };
      }
      case 'config': {
        if (payload) {
          const patch = {};
          if (payload.apiBase !== undefined) patch.apiBase = payload.apiBase;
          if (payload.courseId !== undefined) patch.courseId = payload.courseId;
          if (ACCESS_MODES.includes(payload.accessMode)) patch.accessMode = payload.accessMode;
          await chrome.storage.local.set(patch);
        }
        return await getConfig();
      }
      case 'openPanel': {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (tab) await chrome.tabs.sendMessage(tab.id, { action: 'togglePanel' });
        return { ok: true };
      }
      case 'captureTab': {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (!tab) throw new Error('没有活动标签页');
        return { dataUrl: await chrome.tabs.captureVisibleTab(tab.windowId, { format: 'jpeg', quality: 70 }) };
      }
      default: throw new Error('未知动作：' + message.action);
    }
  })().then(sendResponse).catch((error) => sendResponse({ error: error.message }));
  return true;
});
