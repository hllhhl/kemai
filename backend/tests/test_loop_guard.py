"""循环守卫：同一调用重复多久算原地打转。

阈值照 open-cowork 的 loop-guard 收紧了——课脉一轮只做一个动作，同样的调用
第 2 次就已经说明模型没在推进。
"""
from __future__ import annotations

from app.agent_runtime import TOOL_HALT, TOOL_WARN, note_call


class FakeRun:
    """只带守卫要用的三个计数器，别的都不需要。"""

    def __init__(self):
        self.call_streak: dict[str, int] = {}
        self.tool_totals: dict[str, int] = {}
        self.last_signature = ""


def test_same_call_twice_warns_and_thrice_halts():
    run = FakeRun()
    signature = "kemai.course_brief|{}"

    assert note_call(run, "kemai.course_brief", signature) == ""
    assert note_call(run, "kemai.course_brief", signature) == "warn"
    assert note_call(run, "kemai.course_brief", signature) == "halt"
    # 拦下之后仍然继续计数，否则永远停在 warn 升不到 halt
    assert note_call(run, "kemai.course_brief", signature) == "halt"


def test_a_different_call_resets_the_streak():
    """A/B/A/B 不是死循环，换个动作就该重新算。"""
    run = FakeRun()
    first, second = "kemai.course_brief|{}", 'kemai.notes_read|{"chapter":"1"}'

    assert note_call(run, "kemai.course_brief", first) == ""
    assert note_call(run, "kemai.notes_read", second) == ""
    assert note_call(run, "kemai.course_brief", first) == ""


def test_one_tool_with_many_arguments_is_caught_by_the_second_layer():
    """同一工具换着参数反复查，签名各不相同，只能靠累计次数抓。"""
    run = FakeRun()
    verdicts = [note_call(run, "read", f"read|{index}") for index in range(TOOL_HALT)]

    assert verdicts[TOOL_WARN - 1] == "warn"
    assert verdicts[TOOL_HALT - 1] == "halt"
    assert all(v == "" for v in verdicts[: TOOL_WARN - 1])


def test_counters_stay_per_run():
    """两次运行互不影响：一个任务的重复不能把新任务也拦掉。"""
    first, second = FakeRun(), FakeRun()
    signature = "kemai.course_brief|{}"

    note_call(first, "kemai.course_brief", signature)
    note_call(first, "kemai.course_brief", signature)

    assert note_call(second, "kemai.course_brief", signature) == ""
