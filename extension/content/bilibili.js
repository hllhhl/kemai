/**
 * B 站适配器：统一接口 getMeta / getVideoEl / extractSubtitles / captureKeyframes
 *
 * 字幕四级兜底来自 video-note-helper 的实战经验；WBI 签名交给本地后端，
 * 扩展里不再塞一份 MD5 实现。
 * 抽帧按「画面有没有变」取样，不按固定时间点：网课的信息在换页瞬间。
 */
(function () {
  'use strict';

  const send = (action, payload) => new Promise((resolve, reject) => {
    chrome.runtime.sendMessage({ action, payload }, (response) => {
      if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
      if (response && response.error) return reject(new Error(response.error));
      resolve(response);
    });
  });

  const proxy = (url) => send('proxyFetch', { url });
  const sign = (params) => send('sign', params).then((r) => r.query);

  function getBvid() {
    const matched = /(BV[0-9A-Za-z]{8,})/.exec(location.href);
    return matched ? matched[1] : '';
  }

  function getPage() {
    return Number(new URLSearchParams(location.search).get('p') || 1);
  }

  function getVideoEl() {
    return document.querySelector('video') || document.querySelector('.bilibili-player-video video') || null;
  }

  function getMeta() {
    const title = (document.querySelector('h1') || {}).textContent || document.title || '';
    return { bvid: getBvid(), title: title.trim().slice(0, 120), page: getPage(), url: location.href };
  }

  async function getCid(bvid) {
    try {
      const state = window.__INITIAL_STATE__ || {};
      if (state.cid) return Number(state.cid);
      const pages = state.videoData && state.videoData.pages;
      const index = getPage() || 1;
      if (pages && pages[index - 1]) return Number(pages[index - 1].cid);
    } catch (error) { /* 继续走接口 */ }
    const response = await proxy(`https://api.bilibili.com/x/player/pagelist?bvid=${encodeURIComponent(bvid)}`);
    try {
      const payload = JSON.parse(response.text);
      if (payload.code === 0 && Array.isArray(payload.data)) {
        const index = (getPage() || 1) - 1;
        return Number((payload.data[index] || payload.data[0]).cid);
      }
    } catch (error) { /* 忽略 */ }
    throw new Error('拿不到 cid，请确认视频已正常加载');
  }

  async function partTitle(bvid, page) {
    try {
      const response = await proxy(`https://api.bilibili.com/x/player/pagelist?bvid=${encodeURIComponent(bvid)}`);
      const payload = JSON.parse(response.text);
      if (payload.code === 0 && Array.isArray(payload.data)) {
        const target = payload.data.find((item) => Number(item.page) === Number(page));
        if (target && target.part) return String(target.part);
      }
    } catch (error) { /* 忽略 */ }
    return '';
  }

  async function subtitleList(bvid, cid) {
    const tried = [];
    const collect = (response, label) => {
      tried.push(`${label}:${response.status}`);
      try {
        const payload = JSON.parse(response.text);
        const list = payload && payload.data && payload.data.subtitle && payload.data.subtitle.subtitles;
        return Array.isArray(list) && list.length ? list : null;
      } catch (error) { return null; }
    };

    let query = null;
    try { query = await sign({ bvid, cid }); } catch (error) { tried.push('sign_failed'); }
    if (query) {
      const hit = collect(await proxy(`https://api.bilibili.com/x/player/wbi/v2?${query}`), 'wbi_signed');
      if (hit) return { list: hit, tried };
    }
    let hit = collect(await proxy(`https://api.bilibili.com/x/player/wbi/v2?bvid=${bvid}&cid=${cid}`), 'wbi_unsigned');
    if (hit) return { list: hit, tried };
    hit = collect(await proxy(`https://api.bilibili.com/x/player/v2?bvid=${bvid}&cid=${cid}`), 'legacy_v2');
    if (hit) return { list: hit, tried };

    const pagelist = await proxy(`https://api.bilibili.com/x/player/pagelist?bvid=${bvid}`);
    tried.push(`pagelist:${pagelist.status}`);
    try {
      const payload = JSON.parse(pagelist.text);
      if (payload.code === 0 && Array.isArray(payload.data)) {
        for (const page of payload.data) {
          if (!page.cid || Number(page.cid) === Number(cid)) continue;
          hit = collect(await proxy(`https://api.bilibili.com/x/player/v2?bvid=${bvid}&cid=${page.cid}`), `p${page.page}`);
          if (hit) return { list: hit, tried };
        }
      }
    } catch (error) { /* 忽略 */ }
    return { list: [], tried };
  }

  function merge(lines) {
    const out = [];
    for (const item of lines) {
      const text = String(item.text || '').replace(/\n/g, ' ').trim();
      if (!text) continue;
      const previous = out[out.length - 1];
      if (previous && previous.text === text) continue;
      if (previous && previous.text.length < 16 && text.length < 16 && Math.abs(item.from - previous.from) < 3) {
        previous.text += text;
        previous.to = item.to;
        continue;
      }
      out.push({ from: item.from, to: item.to, text });
    }
    return out;
  }

  async function extractSubtitles() {
    const bvid = getBvid();
    if (!bvid) throw new Error('请在 B 站视频播放页使用');
    const cid = await getCid(bvid);
    const { list, tried } = await subtitleList(bvid, cid);
    if (!list.length) {
      throw new Error(`这一讲没有公开字幕。接口尝试：${tried.join(' | ')}。AI 字幕需要浏览器已登录 B 站，登录后刷新重试。`);
    }
    const picked = list.slice().sort((a, b) => (a.ai_type === 1 ? 1 : 0) - (b.ai_type === 1 ? 1 : 0))[0];
    let url = picked.subtitle_url;
    if (url.startsWith('//')) url = 'https:' + url;
    const response = await proxy(url);
    if (response.status !== 200) throw new Error('字幕文件下载失败 HTTP ' + response.status);
    let payload;
    try { payload = JSON.parse(response.text); } catch (error) { throw new Error('字幕数据解析失败'); }
    const body = Array.isArray(payload.body) ? payload.body : Array.isArray(payload) ? payload : [];
    const lines = merge(body.map((item) => ({
      from: Number(item.from) || 0,
      to: Number(item.to) || 0,
      text: item.content || '',
    })));
    if (!lines.length) throw new Error('字幕内容为空');
    return { bvid, cid, part: getPage(), partTitle: await partTitle(bvid, getPage()), lines, lan: picked.lan_doc || '', tried };
  }

  const GRID_W = 160, GRID_H = 90, DIFF_THRESHOLD = 0.06;

  function grabGray(video, canvas, ctx, seconds) {
    return new Promise((resolve) => {
      const onSeeked = () => { video.removeEventListener('seeked', onSeeked); resolve(); };
      video.addEventListener('seeked', onSeeked);
      try { video.currentTime = Math.min(seconds, Math.max(0, video.duration - 0.05)); }
      catch (error) { resolve(); }
    })
      // seeked 之后让出一个宏任务，否则概率性读到旧帧，表现为「所有帧都一样」
      .then(() => new Promise((resolve) => setTimeout(resolve, 6)))
      .then(() => {
        ctx.drawImage(video, 0, 0, GRID_W, GRID_H);
        const data = ctx.getImageData(0, 0, GRID_W, GRID_H).data;
        const gray = new Float32Array(GRID_W * GRID_H);
        for (let index = 0, target = 0; index < data.length; index += 4, target += 1) {
          gray[target] = (data[index] * 0.299 + data[index + 1] * 0.587 + data[index + 2] * 0.114) / 255;
        }
        return gray;
      });
  }

  function diffRatio(previous, current) {
    if (!previous) return 1;
    let changed = 0;
    for (let index = 0; index < previous.length; index += 1) {
      if (Math.abs(previous[index] - current[index]) > 0.12) changed += 1;
    }
    return changed / previous.length;
  }

  const mmss = (value) => `${String(Math.floor(value / 60)).padStart(2, '0')}:${String(Math.floor(value % 60)).padStart(2, '0')}`;

  async function captureKeyframes(maxFrames = 6) {
    const video = getVideoEl();
    if (!video) throw new Error('没找到播放器 video 元素');
    const duration = video.duration || 0;
    if (!duration) throw new Error('视频时长未知，等播放器加载完成再试');

    const canvas = document.createElement('canvas');
    canvas.width = GRID_W; canvas.height = GRID_H;
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    const full = document.createElement('canvas');
    const step = Math.max(2, duration / 120);
    let previous = null;
    const frames = [];

    for (let seconds = 0; seconds < duration && frames.length < maxFrames; seconds += step) {
      let gray;
      try { gray = await grabGray(video, canvas, ctx, seconds); }
      catch (error) { break; }
      if (diffRatio(previous, gray) < DIFF_THRESHOLD) { previous = gray; continue; }
      previous = gray;
      try {
        full.width = Math.min(720, video.videoWidth || 640);
        full.height = Math.round(full.width * (video.videoHeight || 360) / (video.videoWidth || 640));
        full.getContext('2d').drawImage(video, 0, 0, full.width, full.height);
        frames.push({ time: mmss(seconds), dataUrl: full.toDataURL('image/jpeg', 0.7) });
      } catch (error) {
        // canvas 被跨域视频污染：回退到可见标签页截图
        try {
          const shot = await send('captureTab');
          if (shot && shot.dataUrl) frames.push({ time: mmss(seconds), dataUrl: shot.dataUrl });
        } catch (inner) { /* 忽略 */ }
      }
    }
    try { video.currentTime = 0; } catch (error) { /* 忽略 */ }
    const page = getPage();
    return { bvid: getBvid(), part: page, partTitle: await partTitle(getBvid(), page), frames };
  }

  window.ZY_ADAPTER = { platform: 'bilibili', getMeta, getVideoEl, extractSubtitles, captureKeyframes };
  window.dispatchEvent(new CustomEvent('zy-adapter-ready'));
})();
