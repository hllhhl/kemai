"""执行前的哨兵：决定一个动作是 allow / ask / deny。

借鉴 nanoMuse 的 sentinel（风险分级 × 模式 + 规则 + 记忆），但课脉的动作类型
是有限的几种，所以按**动作类型**判风险，不引 tool glob —— 通配符是给开放式
工具集用的，课脉这里用不上。

为什么必须有这一层：

    「把第三章做成笔记，再出 10 道题」在执行面上等于一次授权了两个写操作。
    没有这层判断，「生成笔记」和「删掉这门课」走的是同一条代码路径，
    区别只剩下用户有没有把那句话读仔细。

三个概念对应关系（照 nanoMuse 的分法）：

    low        只读，不改变任何已有内容        → allow
    moderate   会产生/覆盖学习资产             → 默认 allow，严格模式 ask
    sensitive  删除、移动、覆盖已有笔记        → ask（严格模式下也一样）

判定顺序（先命中者胜，后面的只能把结论往"更谨慎"的方向抬，不能放宽）：

    1. deny        硬拒绝
    2. always_ask  用户明确要求"这类先问我"
    3. always_allow 用户已授权（本次会话或长期）
    4. 风险 × 模式
    5. 覆盖升级    已有产物被覆盖时抬到 ask —— 这一条不受模式影响
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ALLOW = "allow"
ASK = "ask"
DENY = "deny"

# 风险等级可以直接比大小，模式判断要用
LEVELS = {"low": 0, "moderate": 1, "sensitive": 2}

# 每种动作的风险，以及"为什么是这个等级"——界面要显示原因，不是显示一个颜色
RISK: dict[str, tuple[str, str]] = {
    "answer": ("low", "只是回答，不改动任何内容"),
    "finder": ("low", "只读检索，比对课件与视频"),
    "notes": ("moderate", "会生成一份复习笔记，覆盖同章节的旧版本"),
    "practice": ("moderate", "会生成题目，覆盖同签名下的旧题"),
    "organize": ("sensitive", "会移动或重命名你的文件"),
    "delete": ("sensitive", "会删除内容，删了不能撤销"),
}

# 默认"这类东西值得先问一句"的动作
DEFAULT_ASK = {"organize", "delete"}

CONFIG_NAME = "sentinel.json"


def _config_path() -> Path:
    # 和 data/ 放一起：跟着数据走，不跟代码走
    base = Path(os.getenv("KEMAI_DATA_DIR") or (Path(__file__).resolve().parent.parent.parent / "data"))
    return base / CONFIG_NAME


class Sentinel:
    """模式 + 授权记忆。线程安全，因为生成任务是后台线程在跑。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session_grants: set[str] = set()
        self._state: dict[str, Any] = {"mode": "ask", "always_allow": [], "always_ask": [], "deny": []}

    # ── 持久化 ────────────────────────────────────────────
    def load(self) -> None:
        path = _config_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("哨兵配置读不出来，按默认值走", exc_info=True)
            return
        if isinstance(data, dict):
            with self._lock:
                self._state.update({k: v for k, v in data.items() if k in self._state})

    def save(self) -> None:
        path = _config_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                payload = dict(self._state)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            logger.warning("哨兵配置写不进去", exc_info=True)

    # ── 状态 ──────────────────────────────────────────────
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "mode": self._state["mode"],
                "always_allow": sorted(self._state["always_allow"]),
                "always_ask": sorted(self._state["always_ask"]),
                "deny": sorted(self._state["deny"]),
                "session_grants": sorted(self._session_grants),
            }

    def set_mode(self, mode: str) -> None:
        if mode not in ("auto", "ask", "strict"):
            raise ValueError(f"未知的哨兵模式：{mode}")
        with self._lock:
            self._state["mode"] = mode
        self.save()

    def grant(self, action_type: str, scope: str = "session") -> dict:
        """记住一个授权。

        scope=session  只对当前进程有效——重启就忘，适合"这一次别问了"
        scope=always   写进配置文件，长期有效，适合"这门课我信你"

        撤销走 revoke，没有"只撤一次"这种东西：用户点过"以后都别问"，
        再让他去撤销的路径必须短。
        """
        if action_type not in RISK:
            raise ValueError(f"未知的动作类型：{action_type}")
        if scope == "session":
            with self._lock:
                self._session_grants.add(action_type)
        elif scope == "always":
            with self._lock:
                allow = set(self._state["always_allow"])
                allow.add(action_type)
                self._state["always_allow"] = sorted(allow)
            self.save()
        else:
            raise ValueError(f"未知的授权范围：{scope}")
        return self.snapshot()

    def revoke(self, action_type: str) -> dict:
        with self._lock:
            self._session_grants.discard(action_type)
            self._state["always_allow"] = [t for t in self._state["always_allow"] if t != action_type]
        self.save()
        return self.snapshot()

    # ── 判定 ──────────────────────────────────────────────
    def assess(self, action: dict, context: dict | None = None) -> dict:
        """给一个动作下结论。

        返回值直接挂在计划的动作上，前端照着渲染，不需要懂判定逻辑。
        """
        context = context or {}
        kind = str(action.get("type") or "")
        level, why = RISK.get(kind, ("moderate", "未知动作，按会改东西处理"))

        with self._lock:
            mode = self._state["mode"]
            denied = kind in self._state["deny"]
            always_allow = kind in self._state["always_allow"] or kind in self._session_grants
            always_ask = kind in self._state["always_ask"]

        decision = ALLOW
        reason = why
        # 用户明确授权过（点过"别问我"）——这是他的决定，自动升级不该反悔
        explicit_allow = False
        # 用户明确授权过（点过"别问我"）——这是他的决定，自动升级不该反悔
        explicit_allow = False

        if denied:
            decision, reason = DENY, "这类动作已被停用"
        elif always_ask:
            decision, reason = ASK, "你设置过：这类动作先问一句"
        elif always_allow:
            decision, reason = ALLOW, "你已授权过这类动作"
            explicit_allow = True
            explicit_allow = True
        else:
            if mode == "auto":
                decision = ALLOW
            elif mode == "strict":
                decision = ASK if LEVELS[level] >= LEVELS["moderate"] else ALLOW
            else:  # ask
                decision = ASK if LEVELS[level] >= LEVELS["sensitive"] else ALLOW
            if decision == ASK:
                reason = f"{why}（{ {'ask': '提问模式', 'strict': '严格模式'}[mode] }）"

        # 覆盖升级：已有产物会被盖掉，这一条不受模式影响。
        # 和 nanoMuse 的 taint 一个道理——它只能让结论更谨慎，不能让结论放宽。
        overwrite = bool(context.get("overwrites")) and decision != DENY
        if overwrite and explicit_allow:
            # 授权优先：已经答应过这类动作不再问，就不能拿覆盖当借口再问一遍
            reason = "你已授权过这类动作（会覆盖旧版本）"
        elif overwrite:
            decision = ASK
            reason = "会覆盖已有的同名产物，先确认一次"

        return {
            "risk": level,
            "risk_note": why,
            "decision": decision,
            "reason": reason,
            "overwrites": overwrite,
        }


_sentinel = Sentinel()
_sentinel.load()


def sentinel() -> Sentinel:
    return _sentinel
