"""B 站凭证必须以 DPAPI 密文落盘，且旧明文文件能无感迁移。"""
from __future__ import annotations

import json

import pytest

from app import bili_auth
from app.bili_auth import BiliAuth


@pytest.fixture
def auth(tmp_path):
    return BiliAuth(tmp_path)


def test_cookie_is_not_plaintext_on_disk(auth):
    auth.save("SESSDATA=secret-value; bili_jct=token", uname=" tester ", mid=42)
    raw = auth.path.read_text(encoding="utf-8")
    assert "secret-value" not in raw
    assert "cookieEnc" in raw
    assert json.loads(raw)["v"] == bili_auth._FORMAT_VERSION


def test_cookie_round_trips(auth):
    value = "SESSDATA=abc; DedeUserID=1; bili_jct=xyz"
    auth.save(value, uname="张三", mid=7)
    assert auth.cookie() == value
    status = auth.status()
    assert status["loggedIn"] is True
    assert status["uname"] == "张三"
    assert status["mid"] == 7
    assert status["encrypted"] is True
    assert status["credentialRisk"] is True


def test_legacy_plaintext_file_is_migrated(auth):
    auth.path.write_text(json.dumps({
        "cookie": "OLD_SESSDATA_PLAIN", "uname": "旧用户", "mid": 9, "savedAt": 1,
    }), encoding="utf-8")
    assert auth.cookie() == "OLD_SESSDATA_PLAIN"
    # 读一次之后文件里不再有明文
    raw = auth.path.read_text(encoding="utf-8")
    assert "OLD_SESSDATA_PLAIN" not in raw
    assert "cookieEnc" in raw
    assert auth.status()["encrypted"] is True


def test_legacy_file_without_cookie_stays_logged_out(auth):
    auth.path.write_text(json.dumps({"uname": "", "mid": 0, "savedAt": 0}), encoding="utf-8")
    assert auth.cookie() == ""
    assert auth.status()["loggedIn"] is False


def test_clear_removes_the_file(auth):
    auth.save("SESSDATA=x")
    assert auth.path.exists()
    auth.clear()
    assert not auth.path.exists()
    assert auth.cookie() == ""


def test_non_windows_falls_back_to_plaintext(auth, monkeypatch):
    """跨平台不能因为加密不可用就让登录白做，但要明确记警告。"""
    monkeypatch.setattr(bili_auth, "_dpapi_available", lambda: False)
    auth.save("SESSDATA=fallback", uname="linux-user")
    raw = auth.path.read_text(encoding="utf-8")
    assert "cookie" in json.loads(raw)
    assert auth.cookie() == "SESSDATA=fallback"
    assert auth.status()["encrypted"] is False


def test_corrupted_ciphertext_is_not_leaked(auth):
    auth.path.write_text(json.dumps({"cookieEnc": "!!!not-base64!!!"}), encoding="utf-8")
    assert auth.cookie() == ""
    assert auth.status()["loggedIn"] is False
