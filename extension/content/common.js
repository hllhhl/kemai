/**
 * 页面内悬浮面板：抓字幕 → 导入知页 → 抽帧做画面确认 → 生成要点。
 * 不自动弹出，点工具栏图标才出现。
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

  const state = { subtitles: null, frames: null, busy: false, mode: 'auto' };
  let box = null;

  function status(text, bad) {
    const el = box && box.querySelector('#zy-status');
    if (el) { el.textContent = text; el.style.color = bad ? '#b3382c' : '#4a443c'; }
  }

  function build() {
    box = document.createElement('div');
    box.id = 'zy-panel';
    box.innerHTML = `
      <div id="zy-head">知页 · 视频伴侣 <span id="zy-close">×</span></div>
      <div id="zy-meta"></div>
      <div id="zy-mode"></div>
      <div id="zy-status">就绪</div>
      <div id="zy-actions">
        <button data-act="one" class="primary">一键处理这一讲</button>
        <button data-act="subs">① 抓取字幕</button>
        <button data-act="import" class="primary">② 导入到知页</button>
        <button data-act="frames">③ 抽关键帧</button>
        <button data-act="visual">④ 画面确认</button>
        <button data-act="digest">⑤ 生成这一讲要点</button>
        <button data-act="ocr">⑥ 画面转文字并导入（不需登录）</button>
      </div>
      <div id="zy-foot">只取公开可见字幕与播放器画面，不下载视频。数据只进本机。</div>`;
    document.body.appendChild(box);

    const style = document.createElement('style');
    style.textContent = `
      #zy-panel{position:fixed;right:20px;top:80px;width:300px;z-index:2147483647;background:#fffdf8;
        border:1px solid #e3ddd1;border-radius:10px;box-shadow:0 12px 34px rgba(32,29,25,.2);
        font:13px/1.6 -apple-system,"Microsoft YaHei",sans-serif;color:#201d19;padding:12px}
      #zy-head{font-weight:700;font-size:14px;display:flex;justify-content:space-between;cursor:move}
      #zy-close{cursor:pointer;color:#857c70}
      #zy-meta{font-size:11.5px;color:#857c70;margin:6px 0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      #zy-status{font-size:12px;background:#faf7f0;border-radius:6px;padding:7px 9px;margin-bottom:8px;min-height:18px}
      #zy-actions{display:flex;flex-direction:column;gap:6px}
      #zy-actions button{padding:7px;border:1px solid #e3ddd1;border-radius:6px;background:#fff;cursor:pointer;color:#4a443c;text-align:left}
      #zy-actions button:hover{border-color:#a4432a;color:#a4432a}
      #zy-actions button.primary{background:#a4432a;border-color:#a4432a;color:#fff;font-weight:600}
      #zy-actions button:disabled{opacity:.5;cursor:not-allowed}
      #zy-mode{font-size:11px;color:#a4432a;background:#faf3ef;border-radius:5px;padding:4px 7px;margin-bottom:6px}
      #zy-foot{font-size:10.5px;color:#857c70;margin-top:9px;line-height:1.6}`;
    document.head.appendChild(style);

    box.querySelector('#zy-close').onclick = () => { box.style.display = 'none'; };
    box.querySelector('#zy-actions').addEventListener('click', (event) => run(event.target.dataset.act));
    box.style.display = 'none';
  }

  const MODE_LABEL = {
    auto: '自动：先试字幕，读不到就改用画面识别',
    subtitle: '只用字幕：需要浏览器已登录 B 站',
    ocr: '只用画面识别：不借用登录态，全程本地',
  };

  function paintMode() {
    const el = box && box.querySelector('#zy-mode');
    if (el) el.textContent = '获取方式 · ' + (MODE_LABEL[state.mode] || state.mode);
    const subs = box && box.querySelector('[data-act="subs"]');
    if (subs) {
      subs.disabled = state.mode === 'ocr';
      subs.title = state.mode === 'ocr' ? '当前选了只用画面识别，不会请求字幕接口' : '';
    }
  }

  async function oneClick() {
    const adapter = window.ZY_ADAPTER;
    if (!adapter) throw new Error('适配器未就绪，刷新页面再试');
    const notes = [];

    if (state.mode !== 'ocr') {
      try {
        status('① 尝试读字幕…');
        state.subtitles = await adapter.extractSubtitles();
        const imported = await send('importTranscript', {
          bvid: state.subtitles.bvid, cid: state.subtitles.cid,
          part: state.subtitles.part, partTitle: state.subtitles.partTitle,
          lines: state.subtitles.lines,
        });
        notes.push(`字幕 ${imported.blocks} 块`);
      } catch (error) {
        notes.push('字幕不可用：' + error.message.split('。')[0]);
        state.subtitles = null;
        if (state.mode === 'subtitle') {
          status(`只用字幕模式，但读不到：${error.message}`, true);
          return;
        }
      }
    }

    if (!state.subtitles) {
      status('② 改成截画面做本地识别…');
      state.frames = await adapter.captureKeyframes(12);
      if (!state.frames.frames.length) throw new Error('没抽到画面');
      const imported = await send('ocrFrames', {
        bvid: state.frames.bvid, part: state.frames.part,
        partTitle: state.frames.partTitle || '',
        frames: state.frames.frames,
      });
      notes.push(`画面 ${imported.blocks} 屏（失败帧 ${imported.failures}）`);
    }

    const part = (state.subtitles && state.subtitles.part) || (state.frames && state.frames.part);
    const bvid = (state.subtitles && state.subtitles.bvid) || (state.frames && state.frames.bvid);
    const title = (state.subtitles && state.subtitles.partTitle) || (state.frames && state.frames.partTitle) || '';
    try {
      status('③ 生成这一讲要点…');
      const digest = await send('digestPart', { bvid, part, partTitle: title });
      notes.push(`要点 ${digest.keypoints.length} 条`);
    } catch (error) {
      notes.push('要点失败：' + error.message.split('。')[0]);
    }
    status('完成 · ' + notes.join(' · '));
  }

  async function run(act) {
    if (!act || state.busy) return;
    const adapter = window.ZY_ADAPTER;
    if (!adapter) return status('适配器未就绪，刷新页面再试', true);

    state.busy = true;
    const buttons = Array.from(box.querySelectorAll('button'));
    buttons.forEach((button) => { button.disabled = true; });
    try {
      if (act === 'one') {
        await oneClick();
      } else if (act === 'subs') {
        status('正在抓取字幕…');
        state.subtitles = await adapter.extractSubtitles();
        status(`拿到 ${state.subtitles.lines.length} 行字幕（${state.subtitles.lan || '字幕'}）`);
      } else if (act === 'import') {
        if (!state.subtitles) throw new Error('先抓字幕');
        status('正在导入知页…');
        const result = await send('importTranscript', {
          bvid: state.subtitles.bvid, cid: state.subtitles.cid,
          part: state.subtitles.part, partTitle: state.subtitles.partTitle,
          lines: state.subtitles.lines,
        });
        status(result.reused ? '这一讲已经在资料库里' : `已入库 ${result.blocks} 块，可以在知页里检索了`);
      } else if (act === 'frames') {
        status('正在按画面变化抽帧…');
        state.frames = await adapter.captureKeyframes(6);
        status(`抽到 ${state.frames.frames.length} 张关键帧`);
      } else if (act === 'visual') {
        if (!state.frames || !state.frames.frames.length) throw new Error('先抽关键帧');
        status('正在读画面并比对课件页…');
        const result = await send('visualCheck', {
          bvid: state.frames.bvid, part: state.frames.part, frames: state.frames.frames,
        });
        status(result.degraded ? result.note : `${result.verdict}（置信 ${Math.round(result.confidence * 100)}%）`);
      } else if (act === 'ocr') {
        if (!state.frames || !state.frames.frames.length) throw new Error('先抽关键帧');
        status('正在本地识别画面文字…');
        const result = await send('ocrFrames', {
          bvid: state.frames.bvid, part: state.frames.part,
          partTitle: (state.subtitles && state.subtitles.partTitle) || '',
          frames: state.frames.frames,
        });
        status(`识别入库 ${result.blocks} 屏，失败帧 ${result.failures}`);
      } else if (act === 'digest') {
        if (!state.subtitles) throw new Error('先抓字幕');
        status('正在生成要点…');
        const result = await send('digestPart', {
          bvid: state.subtitles.bvid, part: state.subtitles.part, partTitle: state.subtitles.partTitle,
        });
        status(`${result.degraded ? '降级摘要' : '已生成'}：${result.keypoints.length} 条要点，丢掉了 ${result.dropped} 条编造时间戳`);
      }
    } catch (error) {
      status(error.message || String(error), true);
    } finally {
      state.busy = false;
      buttons.forEach((button) => { button.disabled = false; });
    }
  }

  function init() {
    if (document.getElementById('zy-panel')) return;
    build();
    const adapter = window.ZY_ADAPTER;
    const meta = adapter && adapter.getMeta();
    if (meta && box) box.querySelector('#zy-meta').textContent = `${meta.title} · ${meta.bvid} P${meta.page}`;
    chrome.runtime.sendMessage({ action: 'config' }, (config) => {
      if (config && config.accessMode) {
        state.mode = config.accessMode;
        paintMode();
      }
    });
  }

  chrome.runtime.onMessage.addListener((message) => {
    if (message.action === 'togglePanel') {
      if (!box) init();
      box.style.display = box.style.display === 'none' ? 'block' : 'none';
    }
  });

  window.addEventListener('zy-adapter-ready', () => {
    if (!box) init();
    const meta = window.ZY_ADAPTER && window.ZY_ADAPTER.getMeta();
    if (meta && box) box.querySelector('#zy-meta').textContent = `${meta.title} · ${meta.bvid} P${meta.page}`;
  });

  init();
})();
