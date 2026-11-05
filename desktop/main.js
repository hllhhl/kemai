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

const { app, BrowserWindow, Menu, dialog, shell } = require('electron');
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

let backend = null;
let win = null;
let quitting = false;

function log(...args) { console.log('[课脉]', ...args); }

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
    { cwd: ROOT, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] },
  );
  backend.stdout.on('data', (chunk) => log(String(chunk).trim()));
  backend.stderr.on('data', (chunk) => log(String(chunk).trim()));
  backend.on('exit', (code) => {
    log('后端退出，代码', code);
    backend = null;
    if (!quitting && win) {
      dialog.showErrorBox('课脉 · 后端已停止', `本地服务意外退出（代码 ${code}）。\n请重启课脉；如果反复出现，查看 _package/backend.log。`);
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

function createWindow() {
  win = new BrowserWindow({
    width: 1320,
    height: 880,
    minWidth: 960,
    minHeight: 640,
    show: false,
    backgroundColor: '#fffdf8',
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
html,body{height:100%;margin:0;background:#fffdf8;color:#201d19;
  font:14px/1.7 -apple-system,"Microsoft YaHei",sans-serif;display:flex;
  align-items:center;justify-content:center;flex-direction:column;gap:14px}
.mark{width:64px;height:64px;border-radius:14px;background:#a4432a;color:#fffdf8;
  display:flex;align-items:center;justify-content:center;font-size:34px;font-weight:700}
.t{font-size:15px;font-weight:600}.s{color:#857c70;font-size:12.5px}
</style></head><body><div class="mark">课</div><div class="t">课脉</div><div class="s">正在启动本地服务…</div></body></html>`));

  win.once('ready-to-show', () => { win.show(); });

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
  createWindow();

  if (await portOpen()) {
    log('检测到已有后端在运行，直接复用');
  } else {
    log('启动后端…');
    startBackend();
  }

  const ready = await waitForBackend();
  if (!ready) {
    dialog.showErrorBox('课脉 · 启动失败', '本地服务没有在 90 秒内就绪。\n请检查 _package/backend.log，或手动运行 uv run python -m uvicorn app.main:app --app-dir backend');
    app.quit();
    return;
  }

  log('后端就绪，加载界面');
  await win.loadURL(BASE + '/');

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
    if (win) { if (win.isMinimized()) win.restore(); win.focus(); }
  });

  app.whenReady().then(() => {
    boot().catch((error) => {
      const message = String(error && error.message ? error.message : error);
      console.error('[课脉] 启动异常：' + message);
      if (!SELFTEST) dialog.showErrorBox('课脉 · 启动异常', message);
      quitting = true;
      stopBackend();
      app.quit();
    });
  });

  app.on('window-all-closed', () => { quitting = true; stopBackend(); app.quit(); });
  app.on('before-quit', () => { quitting = true; stopBackend(); });
  process.on('exit', stopBackend);
}
