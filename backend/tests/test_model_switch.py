"""切换当前模型：只动 active，不碰服务商和凭据。

聊天框里换模型走的是这条独立的路（POST /system/model-registry/active），
而不是设置页那条「整表重新提交」的路——后者要前端把每个服务商的凭据按
「留空=保留」再交一遍，少一个字段就可能把密钥覆盖掉。
"""
from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.model_registry import ModelConfigStore

MODEL = {
    "api": "openai-completions",
    "context_window": 65536,
    "max_tokens": 8192,
    "reasoning": False,
    "supports_images": False,
    "thinking_level": "default",
    "temperature": None,
    "compat": {},
}
PROVIDER = {
    "id": "p1",
    "name": "测试服务商",
    "provider": "custom",
    "base_url": "https://example.com/v1",
    "credential_mode": "api_key",
    "api_key": "sk-test",
    "headers": {},
    "timeout_seconds": 90,
    "models": [
        {"id": "model-a", "name": "A 模型", **MODEL},
        {"id": "model-b", "name": "B 模型", **MODEL},
    ],
}


def make_store(tmp_path) -> ModelConfigStore:
    payload = {"version": 2, "providers": [PROVIDER], "active": {"provider_id": "p1", "model_id": "model-a"}}
    (tmp_path / "model-config.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return ModelConfigStore(Settings(data_dir=tmp_path))


def test_switching_writes_the_new_active_model(tmp_path):
    store = make_store(tmp_path)

    result = store.set_active("p1", "model-b")

    assert result["active"] == {"provider_id": "p1", "model_id": "model-b"}
    assert result["current"]["model"] == "model-b"
    # 落盘了才算数：重启后还得是 B
    saved = json.loads((tmp_path / "model-config.json").read_text(encoding="utf-8"))
    assert saved["active"]["model_id"] == "model-b"


def test_switching_keeps_credentials_and_other_models(tmp_path):
    """切模型不该顺手改动服务商，更不该把 api_key 弄丢。"""
    store = make_store(tmp_path)

    store.set_active("p1", "model-b")

    saved = json.loads((tmp_path / "model-config.json").read_text(encoding="utf-8"))
    provider = saved["providers"][0]
    assert provider["api_key"] == "sk-test"
    assert [model["id"] for model in provider["models"]] == ["model-a", "model-b"]


def test_unknown_provider_is_refused(tmp_path):
    store = make_store(tmp_path)

    with pytest.raises(ValueError, match="服务商已经不在"):
        store.set_active("nope", "model-a")


def test_unknown_model_is_refused(tmp_path):
    store = make_store(tmp_path)

    with pytest.raises(ValueError, match="没有该模型"):
        store.set_active("p1", "gpt-4o")


def test_clearing_the_model_is_allowed(tmp_path):
    """空 provider + 空 model = 暂时不用模型，这是合法状态。"""
    store = make_store(tmp_path)

    result = store.set_active("", "")

    assert result["active"] is None
