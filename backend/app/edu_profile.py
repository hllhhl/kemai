"""学习者档案：把 ai-shifu 的「个性化讲解」落成软件里的持久化设置。

不是一段提示词模板，而是用户填一次、之后每次对话都自动生效的档案。
存成 data/edu-profile.json，跟着用户数据走，安装升级不会丢。
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import Settings

FIELDS = ("stage", "goal", "level", "weakness", "style")
LABELS = {
    "stage": "学段 / 年级",
    "goal": "当前目标",
    "level": "自评水平",
    "weakness": "薄弱点",
    "style": "偏好的讲解方式",
}
MAX_LEN = 300


def profile_path() -> Path:
    return Settings.from_env().data_dir / "edu-profile.json"


def load() -> dict:
    try:
        data = json.loads(profile_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    return {key: str(data.get(key) or "")[:MAX_LEN] for key in FIELDS}


def save(values: dict) -> dict:
    clean = {key: str((values or {}).get(key) or "").strip()[:MAX_LEN] for key in FIELDS}
    path = profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    return clean


def as_prompt() -> str:
    """拼进 Agent 的 system 提示词。没填任何内容就返回空串。"""
    data = load()
    filled = [(LABELS[key], data[key]) for key in FIELDS if data[key].strip()]
    if not filled:
        # 没填档案时也保留通用的教学要求
        return (
            "\n\n# 教学方式\n"
            "- 信息不足时先问 1 个最关键的澄清问题，别凭空假设后再给一堆建议。\n"
            "- 涉及课件内容时说清依据来自哪一章 / 哪一页，并提醒结论的适用边界。"
        )
    lines = "\n".join(f"- {label}：{value}" for label, value in filled)
    return (
        "\n\n# 学习者档案（据此调整讲解深度、例子和语气）\n" + lines + "\n\n"
        "# 教学方式\n"
        "- 按上面的档案选例子和措辞；信息不足时先问 1 个最关键的澄清问题。\n"
        "- 涉及课件内容时说清依据来自哪一章 / 哪一页，并提醒结论的适用边界。"
    )
