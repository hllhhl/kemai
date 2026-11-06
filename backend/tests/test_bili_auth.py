"""B 站扫码登录的单元测试（离线，用 MockTransport 模拟 passport 接口）。"""

import json
from pathlib import Path

import httpx
import pytest

from app.bili_auth import (
    BiliAuth,
    GENERATE_URL,
    NAV_URL,
    POLL_URL,
    STATE_EXPIRED,
    STATE_OK,
    STATE_SCANNED,
)


def make_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_start_returns_qrcode_svg(tmp_path):
    def handler(request):
        assert str(request.url).startswith(GENERATE_URL)
        return httpx.Response(200, json={
            "code": 0,
            "data": {"url": "https://passport.bilibili.com/h5-app/passport/login/scan?qrcode_key=abc",
                     "qrcode_key": "abc"},
        })

    auth = BiliAuth(tmp_path)
    result = auth.start(make_client(handler))
    assert result["key"] == "abc"
    assert result["image"].startswith("data:image/svg+xml")
    assert "qrcode_key=abc" in result["url"]


def test_start_reports_upstream_error(tmp_path):
    def handler(_request):
        return httpx.Response(200, json={"code": -412, "message": "请求被拦截"})

    with pytest.raises(ValueError, match="请求被拦截"):
        BiliAuth(tmp_path).start(make_client(handler))


def test_poll_states(tmp_path):
    def handler(request):
        code = int(httpx.QueryParams(request.url.query).get("qrcode_key") == "scanned") and STATE_SCANNED
        return httpx.Response(200, json={"code": 0, "data": {"code": code or 86101, "message": ""}})

    auth = BiliAuth(tmp_path)
    client = make_client(handler)
    assert auth.poll(client, "waiting").state == "waiting"
    assert auth.poll(client, "scanned").state == "scanned"


def test_poll_expired(tmp_path):
    def handler(_request):
        return httpx.Response(200, json={"code": 0, "data": {"code": STATE_EXPIRED, "message": "二维码已失效"}})

    state = BiliAuth(tmp_path).poll(make_client(handler), "k")
    assert state.state == "expired"
    assert "失效" in state.message


def test_poll_success_saves_cookie_and_profile(tmp_path):
    def handler(request):
        if str(request.url).startswith(POLL_URL):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"code": STATE_OK, "message": "成功"}},
                headers={"set-cookie": "SESSDATA=secretvalue; Path=/; HttpOnly"},
            )
        if str(request.url).startswith(NAV_URL):
            return httpx.Response(200, json={"code": 0, "data": {"uname": "张三", "mid": 42, "isLogin": True}})
        return httpx.Response(404)

    auth = BiliAuth(tmp_path)
    state = auth.poll(make_client(handler), "k")
    assert state.state == "ok"
    assert state.uname == "张三" and state.mid == 42

    saved = tmp_path / "bili-session.json"
    assert saved.exists()
    payload = json.loads(saved.read_text(encoding="utf-8"))
    # 凭证现在以 DPAPI 密文落盘，明文不能出现在文件里
    assert "SESSDATA=secretvalue" not in saved.read_text(encoding="utf-8")
    assert payload.get("cookieEnc")
    assert payload["uname"] == "张三"

    status = auth.status()
    assert status["loggedIn"] is True and status["mid"] == 42


def test_clear_removes_cookie(tmp_path):
    auth = BiliAuth(tmp_path)
    auth.save("SESSDATA=x", uname="某人", mid=1)
    assert auth.status()["loggedIn"] is True
    auth.clear()
    assert auth.status()["loggedIn"] is False
    assert auth.cookie() == ""


def test_status_without_file(tmp_path):
    status = BiliAuth(tmp_path).status()
    assert status["loggedIn"] is False
    assert status["uname"] == "" and status["mid"] == 0 and status["savedAt"] == 0
    assert status["encrypted"] is False and status["credentialRisk"] is False


def test_bilibili_client_sends_cookie_when_present(tmp_path):
    from app.bilibili import BilibiliClient

    seen = {}

    def handler(request):
        seen["cookie"] = request.headers.get("Cookie", "")
        return httpx.Response(200, json={"code": 0, "data": {"pages": [{"page": 1, "cid": 1, "part": "3.1 栈", "duration": 60}]}})

    client = BilibiliClient(transport=httpx.MockTransport(handler), cookie="SESSDATA=abc")
    client.view("BV1xx411c7mD")
    assert seen["cookie"] == "SESSDATA=abc"
    client.close()

    seen.clear()
    anonymous = BilibiliClient(transport=httpx.MockTransport(handler))
    anonymous.view("BV1xx411c7mD")
    assert seen["cookie"] == ""
    anonymous.close()
