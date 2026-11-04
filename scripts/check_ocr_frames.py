"""验证「画面转文字」这条完全本地的通道：不需要 B 站登录，也不需要模型 Key。

自己渲染两张「课件画面」，走 OCR 接口，再查检索能不能命中。
用法：uv run --python 3.12 python scripts/check_ocr_frames.py <课程名或 id>
"""

from __future__ import annotations

import base64
import io
import sys

import httpx
from PIL import Image, ImageDraw, ImageFont

API = "http://127.0.0.1:8000"
FRAMES = [
    ("00:12", ["3.1.1 栈的定义", "栈是限定在表尾进行插入和删除的线性表", "允许插入删除的一端称为栈顶"]),
    ("00:48", ["3.1.2 栈的顺序存储", "用一段连续空间存放元素", "top 指针指示栈顶位置"]),
]


def render(lines: list[str]) -> str:
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 34)
    image = Image.new("RGB", (1280, 720), "white")
    draw = ImageDraw.Draw(image)
    y = 120
    for line in lines:
        draw.text((90, y), line, fill="black", font=font)
        y += 70
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def main() -> int:
    course = sys.argv[1] if len(sys.argv) > 1 else "数字集成电路基础"
    with httpx.Client(base_url=API, timeout=300.0) as client:
        courses = client.get("/courses").json()
        target = next((item for item in courses if item["name"] == course or item["id"] == course), None)
        if target is None:
            print("找不到课程：", course)
            return 1
        course_id = target["id"]
        print(f"课程：{target['name']}（{course_id}）")

        # 反复跑时先清掉旧的 OCR 资料，保证结果可复现
        try:
            for row in client.get(f"/courses/{course_id}/documents").json():
                if row["kind"] == "transcript" and "OCR" in row["filename"]:
                    client.delete(f"/documents/{row['id']}")
        except Exception as exc:  # noqa: BLE001
            print("清理旧 OCR 资料失败：", exc)

        payload = {
            "bvid": "BVocrTest01",
            "part": 1,
            "part_title": "3.1.1 栈的定义",
            "frames": [{"time": time, "dataUrl": render(lines)} for time, lines in FRAMES],
        }
        response = client.post(f"/courses/{course_id}/finder/ocr-frames", json=payload)
        print("HTTP", response.status_code)
        if response.status_code != 201:
            print(response.text[:500])
            return 1
        result = response.json()
        print(f"入库：{result['filename']} · {result['blocks']} 块 · {result['chunks']} 分块 · 失败帧 {result['failures']}")

        rows = client.get(f"/courses/{course_id}/documents").json()
        print("\n资料库：")
        for row in rows:
            print(f"  {row['kind']:<10} {row['filename']} · {row['status']} · {row['units_total']} 块")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
