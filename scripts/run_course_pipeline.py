"""端到端跑一门课：课件 → 找课 → 对回页 → 字幕 → 要点 → 出题。

用法：
    uv run --python 3.12 python scripts/run_course_pipeline.py decks/xxx.pptx
后端需已启动（127.0.0.1:8000）。

每一步都打印真实结果，失败就打印失败原因，不吞异常、不假装成功。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

API = "http://127.0.0.1:8000"
COURSE_NAME = "数字集成电路基础"


def say(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def main() -> int:
    deck_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("decks/数字集成电路基础_第1-3章.pptx")
    if not deck_path.exists():
        print(f"找不到课件：{deck_path}")
        return 1

    with httpx.Client(base_url=API, timeout=180.0) as client:
        if client.get("/health").status_code != 200:
            print("后端没启动。先运行 uvicorn app.main:app --app-dir backend --port 8000")
            return 1

        # ① 建课
        say("① 建课与上传课件")
        courses = client.get("/courses").json()
        course = next((item for item in courses if item["name"] == COURSE_NAME), None)
        if course is None:
            course = client.post("/courses", json={"name": COURSE_NAME}).json()
            print(f"新建课程：{course['name']}（{course['id']}）")
        else:
            print(f"复用课程：{course['name']}（{course['id']}）")
        course_id = course["id"]

        with deck_path.open("rb") as handle:
            uploaded = client.post(
                f"/courses/{course_id}/documents",
                files={"file": (deck_path.name, handle, "application/vnd.openxmlformats-officedocument.presentationml.presentation")},
                data={"kind": "lecture"},
            )
        if uploaded.status_code not in (200, 202):
            print("上传失败：", uploaded.status_code, uploaded.text[:300])
            return 1
        document = uploaded.json()
        print(f"上传：{document['filename']} → {document['status']}")

        # ② 等解析
        say("② 等待解析入库")
        for _ in range(60):
            rows = client.get(f"/courses/{course_id}/documents").json()
            target = next((row for row in rows if row["id"] == document["id"]), None)
            if target and target["status"] in ("ready", "failed"):
                print(f"状态：{target['status']} · 位置 {target['units_with_text']}/{target['units_total']}")
                if target.get("warnings"):
                    print("警告：", "；".join(target["warnings"][:3]))
                if target["status"] == "failed":
                    print("错误：", target.get("error"))
                    return 1
                break
            time.sleep(2)

        # ③ 读课件身份
        say("③ 读出课件身份")
        scan = client.get(f"/courses/{course_id}/finder/scan").json()
        identity = scan["identity"]
        print(f"课程名：{identity['course']} · 页数：{identity['pages']}")
        print("章节：" + " / ".join(f"{item['id']} {item['title']}" for item in identity["chapters"][:8]))
        print("术语：" + "、".join(identity["glossary"][:10]))
        print("检索词：" + " | ".join(identity["keywords"]))

        # ④ B 站找课
        say("④ 到 B 站找用同一套课件的课程（真实请求）")
        started = time.time()
        found = client.post(
            f"/courses/{course_id}/finder/search",
            json={"course_name": COURSE_NAME, "limit": 10},
        )
        if found.status_code != 200:
            print("检索失败：", found.status_code, found.text[:300])
            return 1
        result = found.json()
        print(f"耗时 {time.time() - started:.1f}s，拿到 {len(result['candidates'])} 个候选")
        print("实际检索词：" + " | ".join(item["keyword"] for item in result["keywords"]))
        if result.get("ambiguous"):
            print("⚠ 前两名分差小于 0.05：多门课都高度吻合，这一步需要你自己确认是哪一门，系统不替你拍板。")
        for rank, item in enumerate(result["candidates"], start=1):
            mapped = len(item.get("mapping") or [])
            covered = len({page for row in (item.get("mapping") or []) for page in row["pages"]})
            flag = "需确认" if item.get("need_confirm") else "可继续"
            print(f"  {rank}. [{item['score']:.2f}|{flag}] {item['videos']}讲 映射{mapped} 覆盖{covered}页 · {item['title'][:46]}")
            for reason in item["reasons"][:2]:
                print(f"       · {reason}")

        if not result["candidates"]:
            print("没有候选，流程到此为止")
            return 1
        top = result["candidates"][0]
        print(f"\n选定：{top['bvid']} · {top['title']}")
        first = (top.get("mapping") or [{}])[0]
        print(f"第一讲：P{first.get('part')} {first.get('title')} → 课件 {first.get('labels')}（{first.get('how')}）")

        # ⑤ 字幕
        say("⑤ 取这一讲的字幕")
        imported = client.post(
            f"/courses/{course_id}/finder/import-transcript",
            json={"bvid": top["bvid"], "cid": first.get("cid"), "part": first.get("part"),
                  "part_title": first.get("title", "")},
        )
        if imported.status_code == 201:
            payload = imported.json()
            print(f"字幕入库：{payload['filename']} · {payload['blocks']} 块 · {payload['chunks']} 分块")
            has_transcript = True
        else:
            detail = imported.json().get("detail")
            message = detail.get("message") if isinstance(detail, dict) else detail
            print(f"拿不到字幕（{imported.status_code}）：{message}")
            if isinstance(detail, dict) and detail.get("diagnosis"):
                print("诊断：" + " | ".join(detail["diagnosis"].get("tried", [])[:6]))
            has_transcript = False

        # ⑥ 要点
        say("⑥ 生成这一讲要点")
        if has_transcript:
            digest = client.post(
                f"/courses/{course_id}/finder/digest-part",
                json={"bvid": top["bvid"], "part": first.get("part"), "part_title": first.get("title", "")},
            )
            if digest.status_code == 200:
                data = digest.json()
                mode = "降级摘要（未配模型）" if data["degraded"] else "模型摘要"
                print(f"{mode} · {len(data['keypoints'])} 条 · 丢弃编造时间戳 {data['dropped']} 条")
                for point in data["keypoints"][:5]:
                    print(f"  [{point['at']:>4}s] {point['text'][:60]}")
            else:
                print("生成失败：", digest.status_code, digest.text[:200])
        else:
            print("跳过：没有字幕就没有要点。")

        # ⑦ 出题
        say("⑦ 出题")
        health = client.get("/health").json()
        if not health.get("llm_configured"):
            print("未配置聊天模型，知页的练习生成（practice/generate）无法执行。")
            print("这是当前唯一挡住「出好题」的一步：它需要模型，而模型需要 API Key。")
        else:
            print("模型已配置，可以调用 practice/generate。")

    print("\n完成。上面每一行都是真实返回，没有占位数据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
