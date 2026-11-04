"""B 站扫码登录（走 B 站官方 passport 流程）。

**这是官方提供的登录方式，不是我们代登录。**
用户在自己的手机 App 上确认授权，我们只做两件事：展示二维码、接收回调。
全程不接触账号密码，也不伪造凭证。

流程（B 站 passport）：
    1. GET  /x/passport-login/web/qrcode/generate  → {url, qrcode_key}
    2. 用户用手机 App 扫码并确认
    3. GET  /x/passport-login/web/qrcode/poll      → data.code
        86101 未扫码 / 86090 已扫码未确认 / 86038 已失效 / 0 成功
    4. 成功时响应会带回 Set-Cookie（SESSDATA 等），httpx 自动收进 cookie jar

Cookie 落在本机 ``data_dir/bili-session.json``，权限收到 600，退出登录即删除。
它是账号凭证，所以：**不明文写进日志、不发给任何第三方、不上传**。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
from threading import Lock
import time

import segno

logger = logging.getLogger(__name__)

GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
REFERER = "https://www.bilibili.com/"

# B 站 poll 的 data.code
STATE_UNSCANNED = 86101
STATE_SCANNED = 86090
STATE_EXPIRED = 86038
STATE_OK = 0

_MESSAGES = {
    STATE_UNSCANNED: "等待扫码",
    STATE_SCANNED: "已扫码，请在手机上确认",
    STATE_EXPIRED: "二维码已失效，请刷新",
    STATE_OK: "登录成功",
}


@dataclass(frozen=True)
class LoginState:
    state: str          # waiting | scanned | expired | ok | error
    message: str
    uname: str = ""
    mid: int = 0


def _state_name(code: int) -> str:
    return {
        STATE_UNSCANNED: "waiting",
        STATE_SCANNED: "scanned",
        STATE_EXPIRED: "expired",
        STATE_OK: "ok",
    }.get(code, "error")


class BiliAuth:
    """二维码登录会话 + 本地 cookie 存取。"""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "bili-session.json"
        self.lock = Lock()
        self._sessions: dict[str, float] = {}

    # ── cookie ──────────────────────────────────────────────
    def cookie(self) -> str:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        return str(payload.get("cookie") or "")

    def status(self) -> dict:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"loggedIn": False, "uname": "", "mid": 0, "savedAt": 0}
        return {
            "loggedIn": bool(payload.get("cookie")),
            "uname": payload.get("uname") or "",
            "mid": payload.get("mid") or 0,
            "savedAt": payload.get("savedAt") or 0,
        }

    def save(self, cookie: str, *, uname: str = "", mid: int = 0) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({
                "cookie": cookie, "uname": uname, "mid": mid, "savedAt": int(time.time()),
            }, ensure_ascii=False), encoding="utf-8")
            # 账号凭证：只给当前用户读写
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                logger.warning("无法收紧 B 站会话文件权限：%s", self.path)

    def clear(self) -> None:
        with self.lock:
            try:
                self.path.unlink()
            except OSError:
                pass

    # ── 二维码 ──────────────────────────────────────────────
    def start(self, http) -> dict:
        """生成二维码。``http`` 是 httpx.Client，用于共享 cookie jar。"""
        response = http.get(GENERATE_URL, headers={"Referer": REFERER})
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError("B 站登录接口返回了非 JSON 内容，可能被限流") from exc
        if payload.get("code") != 0:
            raise ValueError(payload.get("message") or "B 站没有返回二维码")
        data = payload.get("data") or {}
        url = str(data.get("url") or "")
        key = str(data.get("qrcode_key") or "")
        if not url or not key:
            raise ValueError("二维码数据不完整")
        with self.lock:
            self._sessions[key] = time.time()
        return {
            "key": key,
            "url": url,
            # SVG 直接给前端当 <img src>，不落盘、不留中间文件
            "image": segno.make(url, error="m").svg_data_uri(scale=6, dark="#201d19", light="#fffdf8"),
        }

    def poll(self, http, key: str) -> LoginState:
        if not key:
            raise ValueError("缺少二维码 key")
        started = self._sessions.get(key)
        if started and time.time() - started > 600:
            return LoginState("expired", "二维码已超过 10 分钟，请刷新")

        response = http.get(POLL_URL, params={"qrcode_key": key}, headers={"Referer": REFERER})
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError("B 站登录接口返回了非 JSON 内容") from exc

        data = payload.get("data") or {}
        code = int(data.get("code", -1))
        message = data.get("message") or _MESSAGES.get(code, "未知状态")

        if code != STATE_OK:
            return LoginState(_state_name(code), message)

        cookie = "; ".join(f"{item.name}={item.value}" for item in http.cookies.jar)
        if not cookie:
            return LoginState("error", "登录看似成功，但没有拿到凭证（可能是接口变了）")

        uname, mid = self._profile(http)
        self.save(cookie, uname=uname, mid=mid)
        with self.lock:
            self._sessions.pop(key, None)
        return LoginState("ok", "登录成功", uname=uname, mid=mid)

    def _profile(self, http) -> tuple[str, int]:
        """拿昵称只为界面上显示「已登录为谁」，拿不到不影响登录本身。"""
        try:
            payload = http.get(NAV_URL, headers={"Referer": REFERER}).json()
        except (ValueError, OSError):
            return "", 0
        data = payload.get("data") or {}
        return str(data.get("uname") or ""), int(data.get("mid") or 0)


_auth: BiliAuth | None = None


def get_auth(data_dir: Path) -> BiliAuth:
    global _auth
    if _auth is None:
        _auth = BiliAuth(data_dir)
    return _auth
