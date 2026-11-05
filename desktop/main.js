/**
 * 课脉 · 桌面外壳
 *
 * 做三件事：
 *   1. 启动本地 Python 后端（uvicorn），窗口里没有黑框；
 *   2. 等后端健康检查通过，再把界面加载进独立窗口；
 *   3. 退出时把后端进程一起收掉，不留孤儿进程占端口。
 *
 * 界面由后端在同一个端口托管（`/` 挂 frontend/dist），所以整机只跑一个 Python 进程，
 * 不需要 vite dev server，也不需要开浏览器。
 */

const { app, BrowserWindow, Menu, Tray, nativeImage, dialog, shell } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const http = require('http');

// 开发时根目录是项目目录；装成 exe 后是 resources 目录（extraResources 都放在这里）
const ROOT = app.isPackaged ? process.resourcesPath : path.resolve(__dirname, '..');
const PORT = Number(process.env.KEMAI_PORT || 8000);
const BASE = `http://127.0.0.1:${PORT}`;
const PYTHON_CANDIDATES = [
  // 随包分发的可重定位 Python（python-build-standalone + site-packages）
  path.join(ROOT, 'python', 'python.exe'),
  // 开发机上的 venv
  path.join(ROOT, '.venv', 'Scripts', 'python.exe'),
  path.join(path.resolve(__dirname, '..'), '.venv', 'Scripts', 'python.exe'),
];
const PYTHON = PYTHON_CANDIDATES.find((candidate) => fs.existsSync(candidate)) || PYTHON_CANDIDATES[0];
const ICON = path.join(ROOT, 'assets', 'kemai.ico');

// 打包后的 exe 会把自定义参数当成 Chromium 开关而报 bad option，
// 所以自检开关走环境变量。
const SELFTEST = process.env.KEMAI_SELFTEST === '1';
const SHOT_PATH = process.env.KEMAI_SHOT || path.join(ROOT, '_package', 'desktop-selftest.png');

const T0 = Date.now();
function sinceStart() { return ((Date.now() - T0) / 1000).toFixed(2) + 's'; }

let backend = null;
let win = null;
let tray = null;
let trayHintShown = false;
let quitting = false;

// 装在 Program Files 时安装目录不可写，日志必须落在 userData
const LOG_PATH = () => path.join(app.getPath('userData'), 'backend.log');
const LOG_LIMIT = 2 * 1024 * 1024;
let logStream = null;

// 界面报错时会提示"看 backend.log"，所以这里必须真的写文件。
// GUI 进程的 stdout 在 Windows 上会被丢弃，只 console.log 等于没日志。
function openLog() {
  try {
    const target = LOG_PATH();
    fs.mkdirSync(path.dirname(target), { recursive: true });
    try {
      if (fs.statSync(target).size > LOG_LIMIT) fs.renameSync(target, `${target}.1`);
    } catch {
      /* 文件不存在就不用轮转 */
    }
    logStream = fs.createWriteStream(target, { flags: 'a' });
    log('日志文件：' + target);
  } catch (error) {
    console.error('日志文件打不开', error);
  }
}

function log(...args) {
  const line = `[课脉 +${sinceStart()}] ${args.join(' ')}`;
  console.log(line);
  if (logStream) logStream.write(`${line}\n`);
}

function portOpen() {
  return new Promise((resolve) => {
    const request = http.get(`${BASE}/health`, { timeout: 1200 }, (response) => {
      response.resume();
      resolve(response.statusCode === 200);
    });
    request.on('timeout', () => { request.destroy(); resolve(false); });
    request.on('error', () => resolve(false));
  });
}

async function waitForBackend(maxMs = 90000) {
  const started = Date.now();
  while (Date.now() - started < maxMs) {
    if (await portOpen()) return true;
    if (backend && backend.exitCode !== null) return false;
    await new Promise((resolve) => setTimeout(resolve, 400));
  }
  return false;
}

function startBackend() {
  if (!fs.existsSync(PYTHON)) {
    throw new Error(`找不到 Python 运行环境：${PYTHON}\n请先在项目目录执行 uv sync --frozen --python 3.12`);
  }
  // windowsHide: 关键。不加这个，Windows 上会弹一个黑色控制台窗口。
  backend = spawn(
    PYTHON,
    ['-u', '-m', 'uvicorn', 'app.main:app', '--app-dir', 'backend', '--host', '127.0.0.1', '--port', String(PORT)],
    {
      cwd: ROOT,
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
      // 国内直连 huggingface.co 会超时，嵌入模型根本下不下来。
      // 指到镜像后首次约 30 秒下完，之后走缓存。
      env: {
        ...process.env,
        HF_ENDPOINT: process.env.HF_ENDPOINT || 'https://hf-mirror.com',
        // 后端靠这个每隔几秒确认父进程还在，被强杀时能自己收尾，不留孤儿占端口
        KEMAI_PARENT_PID: String(process.pid),
      },
    },
  );
  backend.stdout.on('data', (chunk) => log(String(chunk).trim()));
  backend.stderr.on('data', (chunk) => log(String(chunk).trim()));
  backend.on('exit', (code) => {
    log('后端退出，代码', code);
    backend = null;
    if (!quitting && win) {
      dialog.showErrorBox('课脉 · 后端已停止', `本地服务意外退出（代码 ${code}）。\n请重启课脉；如果反复出现，查看 data/backend.log。`);
    }
  });
}

function stopBackend() {
  if (!backend) return;
  try {
    if (process.platform === 'win32') {
      // uvicorn 会再开子进程，只 kill 父进程会留孤儿，所以按进程树结束
      spawn('taskkill', ['/pid', String(backend.pid), '/T', '/F'], { windowsHide: true });
    } else {
      backend.kill('SIGTERM');
    }
  } catch (error) {
    log('结束后端失败', error.message);
  }
  backend = null;
}

// ── 托盘 ────────────────────────────────────────────────
// 关窗不退出：进程留住后端，下次点图标就是秒开（不用再等 5 秒起 Python）。
// 真要退出走托盘右键菜单，那条路径才会 stopBackend。
function showWindow() {
  if (!win || win.isDestroyed()) {
    createWindow();
    return;
  }
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

function createTray() {
  if (tray) return;
  const source = fs.existsSync(ICON) ? nativeImage.createFromPath(ICON) : nativeImage.createEmpty();
  // 托盘是 16px 的位置，直接塞 256px 会糊
  tray = new Tray(source.isEmpty() ? source : source.resize({ width: 16, height: 16 }));
  tray.setToolTip('课脉 · 课程知识体系');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: '打开课脉', click: showWindow },
    { type: 'separator' },
    { label: '退出课脉', click: () => { quitting = true; app.quit(); } },
  ]));
  tray.on('click', showWindow);
  log('托盘已就绪');
  tray.on('double-click', showWindow);
}

function createWindow() {
  win = new BrowserWindow({
    width: 1320,
    height: 880,
    minWidth: 960,
    minHeight: 640,
    // 立即显示：双击后先看到品牌启动页，而不是等后端起来才出窗口
    show: true,
    backgroundColor: '#f4f6fb',
    title: '课脉',
    icon: fs.existsSync(ICON) ? ICON : undefined,
    autoHideMenuBar: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      spellcheck: false,
    },
  });

  Menu.setApplicationMenu(null);

  // 先给一个启动页，避免后端没起来时白屏
  win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(`<!doctype html>
<html><head><meta charset="utf-8"><style>
html,body{height:100%;margin:0;background:#f4f6fb;color:#24324b;
  font:14px/1.7 -apple-system,"Microsoft YaHei",sans-serif;display:flex;
  align-items:center;justify-content:center;flex-direction:column;gap:14px}
.mark{width:64px;height:64px;border-radius:14px;background:#3455db;color:#fff;
  display:flex;align-items:center;justify-content:center;font-size:34px;font-weight:700}
.t{font-size:15px;font-weight:600}.s{color:#66738a;font-size:12.5px}
</style></head><body><div class="mark"><svg width="36" height="36" viewBox="0 0 36 40" fill="none"><circle cx="6" cy="20" r="3" fill="#fff"/><path d="M9 20h6M15 20l13-9M15 20h13M15 20l13 9" stroke="#fff" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/></svg></div><div class="t">课脉</div><div class="s">正在启动本地服务，通常 3–5 秒…</div></body></html>`));

  win.once('ready-to-show', () => { win.show(); win.focus(); });

  // 点 × 只是收起：后端继续活着，下次唤出来是热的
  win.on('close', (event) => {
    if (quitting) return;
    event.preventDefault();
    win.hide();
    if (!trayHintShown) {
      trayHintShown = true;
      try {
        tray?.displayBalloon({
          title: '课脉仍在后台运行',
          content: '点托盘图标可以重新打开；要彻底退出请右键托盘图标。',
        });
      } catch {
        /* 有些 Windows 版本不支持气泡，忽略即可 */
      }
    }
  });

  // 站内链接留在窗口里，外部链接交给系统浏览器
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith(BASE)) return { action: 'allow' };
    shell.openExternal(url);
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith(BASE) && !url.startsWith('data:')) {
      event.preventDefault();
      shell.openExternal(url);
    }
  });

  win.on('closed', () => { win = null; });
}

async function boot() {
  log('开始启动后端');
  createWindow();
  log('窗口已创建');

  if (await portOpen()) {
    log('检测到已有后端在运行，直接复用');
  } else {
    log('启动后端…');
    startBackend();
  }

  const ready = await waitForBackend();
  if (!ready) {
    dialog.showErrorBox('课脉 · 启动失败', '本地服务没有在 90 秒内就绪。\n请查看日志文件，或手动运行 uv run python -m uvicorn app.main:app --app-dir backend');
    app.quit();
    return;
  }

  log('后端就绪，加载界面');
  await win.loadURL(BASE + '/');
  log('界面已加载');

  if (SELFTEST) {
    win.show();
    await new Promise((resolve) => setTimeout(resolve, 3000));
    const image = await win.webContents.capturePage();
    fs.mkdirSync(path.dirname(SHOT_PATH), { recursive: true });
    fs.writeFileSync(SHOT_PATH, image.toPNG());
    console.log('SELFTEST_OK ' + SHOT_PATH);
    quitting = true;
    stopBackend();
    app.quit();
  }
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    // 第二次双击图标：单实例锁让它立刻退出，这里负责把已有窗口唤出来
    log('已有实例在运行，唤回窗口');
    showWindow();
  });

  app.whenReady().then(() => {
    openLog();
    log('Electron 就绪');
    createTray();
    boot().catch((error) => {
      const message = String(error && error.message ? error.message : error);
      console.error('[课脉] 启动异常：' + message);
      if (!SELFTEST) dialog.showErrorBox('课脉 · 启动异常', message);
      quitting = true;
      stopBackend();
      app.quit();
    });
  });

  // 有托盘时关窗只是隐藏，窗口没被销毁，这个事件不会触发；
  // 留着是为了走真正的退出路径（托盘菜单 / 启动失败）时能干净收尾。
  app.on('window-all-closed', () => { if (quitting) { stopBackend(); app.quit(); } });
  app.on('before-quit', () => { quitting = true; stopBackend(); });
  process.on('exit', stopBackend);
}
