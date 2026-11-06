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

Cookie 落在本机 ``data_dir/bili-session.json``。SESSDATA 是**完整账号凭证**
（能发评论、改资料、看私信），所以：

- Windows 上用 DPAPI（CryptProtectData）按当前用户身份加密后落盘，
  别的进程/用户拿到文件也解不开；非 Windows 回退到明文 + 0600 并记警告。
- 旧的明文文件读取时自动迁移成加密格式，用户无感。
- **不明文写进日志、不发给任何第三方、不上传**。
- ``os.chmod`` 在 Windows 只影响只读位，不构成 ACL，不再把它当安全措施。
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
import ctypes
import json
import logging
import os
from pathlib import Path
import sys
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



# ── Windows DPAPI ────────────────────────────────────────────
# 按当前登录用户的密钥加密，密文只能由同一个 Windows 账户解开。
# 这样即使别的进程读到 bili-session.json，也拿不到 SESSDATA。
_ENTROPY = b"kemai-bili-session-v1"
_FORMAT_VERSION = 2


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.c_void_p)]


def _dpapi_available() -> bool:
    return sys.platform.startswith("win")


def _dpapi_setup():
    crypt32 = ctypes.windll.crypt32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.c_wchar_p, ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_DataBlob)]
    crypt32.CryptProtectData.restype = ctypes.c_bool
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.c_wchar_p, ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_DataBlob)]
    crypt32.CryptUnprotectData.restype = ctypes.c_bool
    ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    return crypt32


def _blob(buffer) -> _DataBlob:
    return _DataBlob(len(buffer), ctypes.cast(buffer, ctypes.c_void_p).value)


def _protect(text: str) -> str:
    data = text.encode("utf-8")
    crypt32 = _dpapi_setup()
    in_buf = ctypes.create_string_buffer(data, len(data))
    entropy_buf = ctypes.create_string_buffer(_ENTROPY, len(_ENTROPY))
    out_blob = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(_blob(in_buf)), None, ctypes.byref(_blob(entropy_buf)),
        None, None, 0x01, ctypes.byref(out_blob))  # CRYPTPROTECT_UI_FORBIDDEN
    if not ok:
        raise OSError(f"CryptProtectData 失败：{ctypes.get_last_error()}")
    try:
        return base64.b64encode(ctypes.string_at(out_blob.pbData, out_blob.cbData)).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def _unprotect(encoded: str) -> str:
    data = base64.b64decode(encoded.encode("ascii"))
    crypt32 = _dpapi_setup()
    in_buf = ctypes.create_string_buffer(data, len(data))
    entropy_buf = ctypes.create_string_buffer(_ENTROPY, len(_ENTROPY))
    out_blob = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(_blob(in_buf)), None, ctypes.byref(_blob(entropy_buf)),
        None, None, 0x01, ctypes.byref(out_blob))
    if not ok:
        raise OSError(f"CryptUnprotectData 失败：{ctypes.get_last_error()}")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


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
    def _read(self) -> tuple[str, dict]:
        """返回 (cookie 明文, 原始 payload)。旧明文文件会就地迁移成加密格式。"""
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "", {}

        if payload.get("cookieEnc"):
            try:
                return _unprotect(str(payload["cookieEnc"])), payload
            except (OSError, ValueError, KeyError) as exc:
                logger.warning("B 站会话解密失败：%s", exc)
                return "", payload

        # 旧版本明文格式：读出来后立刻重写成加密格式，用户无感。
        plain = str(payload.get("cookie") or "")
        if plain and payload.get("v") != _FORMAT_VERSION:
            try:
                self.save(plain, uname=payload.get("uname") or "", mid=int(payload.get("mid") or 0))
            except Exception as exc:  # noqa: BLE001 - 迁移失败不能影响读取
                logger.warning("B 站会话迁移到加密格式失败：%s", exc)
        return plain, payload

    def cookie(self) -> str:
        return self._read()[0]

    def status(self) -> dict:
        cookie, payload = self._read()
        encrypted = bool(payload.get("cookieEnc"))
        return {
            "loggedIn": bool(cookie),
            "uname": payload.get("uname") or "",
            "mid": payload.get("mid") or 0,
            "savedAt": payload.get("savedAt") or 0,
            "encrypted": encrypted,
            # 前端据此显示醒目提示：这把凭证能开整个账号
            "credentialRisk": bool(cookie),
        }

    def save(self, cookie: str, *, uname: str = "", mid: int = 0) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            record = {"v": _FORMAT_VERSION, "uname": uname, "mid": mid,
                      "savedAt": int(time.time())}
            if _dpapi_available():
                try:
                    record["cookieEnc"] = _protect(cookie)
                except Exception as exc:  # noqa: BLE001 - 加密失败要退化，不能让登录白做
                    logger.warning("DPAPI 加密失败，回退明文存储：%s", exc)
                    record["cookie"] = cookie
            else:
                logger.warning("非 Windows 平台，B 站凭证以明文存储（已设 0600）")
                record["cookie"] = cookie
            self.path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            # 只作为非 Windows 上的辅助措施；Windows 上 ACL 由 DPAPI 保证。
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
