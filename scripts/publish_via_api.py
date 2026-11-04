"""用 GitHub REST API 发布仓库——不依赖 github.com 网页域名。

为什么单独写这个：这台机器上 `github.com` 不通（超时），但 `api.github.com` 通。
网页建库、网页上传、gh 的 device flow 全都落在 `github.com`，所以都走不通；
而 Git Data API 落在 `api.github.com`，只要有一个 Personal Access Token 就能发布。

用法：
    GITHUB_TOKEN=ghp_xxx python scripts/publish_via_api.py [仓库名]
token 需要 repo 权限。脚本不会打印 token，也不会写进任何文件。

流程（一次 commit，不是 184 次提交）：
    1. GET  /user                              确认身份
    2. POST /user/repos                        建库（已存在则复用）
    3. POST /repos/{o}/{r}/git/blobs           每个文件一个 blob
    4. POST /repos/{o}/{r}/git/trees           一棵树装下所有路径
    5. POST /repos/{o}/{r}/git/commits         一次提交
    6. PATCH /repos/{o}/{r}/git/refs/heads/... 把分支指过去
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.github.com"

EXCLUDE_DIRS = {
    ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache",
    "data", ".cache", "htmlcov", ".mypy_cache", "_package",
}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".log", ".db", ".sqlite", ".db-wal", ".db-shm"}
EXCLUDE_FILES = {".env", "bili-session.json"}

COMMIT_MESSAGE = """课脉 v0.1.0：基于知页底座，补上视频取证链路

- PPT 反查整门课：课件页指纹 + 章节编号双向覆盖
- 字幕入库：wbi/v2 四级兜底 + lines 直传，引用可落到「第 3 讲 03:24–04:10」
- 画面 OCR：本地 rapidocr，不需要 B 站登录与模型 Key
- 逐讲要点：时间戳逐条核对，编造的丢弃并计数
- 软件内 B 站扫码登录（官方 passport 流程）
- 浏览器扩展三档获取方式

上游：zhiye-review-agent（MIT）"""


def collect() -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if set(rel.parts) & EXCLUDE_DIRS:
            continue
        if path.name in EXCLUDE_FILES or path.suffix.lower() in EXCLUDE_SUFFIX:
            continue
        files.append((path, str(rel).replace("\\", "/")))
    return files


def main() -> int:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        print("缺少 GITHUB_TOKEN。用法：GITHUB_TOKEN=ghp_xxx python scripts/publish_via_api.py [仓库名]")
        return 2
    repo_name = sys.argv[1] if len(sys.argv) > 1 else "kemai"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    files = collect()
    print(f"待上传 {len(files)} 个文件")

    with httpx.Client(base_url=API, headers=headers, timeout=60.0) as client:
        who = client.get("/user")
        if who.status_code != 200:
            print("token 无效或权限不足：", who.status_code, who.text[:200])
            return 1
        owner = who.json()["login"]
        print(f"身份：{owner}")

        created = client.post("/user/repos", json={
            "name": repo_name,
            "description": "课脉 · 课程知识体系 Agent（基于知页 zhiye-review-agent）",
            "private": False,
            "has_issues": True,
            "has_wiki": False,
        })
        if created.status_code == 201:
            print("已创建仓库：", created.json()["html_url"])
        elif created.status_code == 422:
            print("仓库已存在，复用")
        else:
            print("建库失败：", created.status_code, created.text[:300])
            return 1

        def upload(item: tuple[Path, str]) -> tuple[str, str]:
            path, rel = item
            payload = {"content": base64.b64encode(path.read_bytes()).decode(), "encoding": "base64"}
            response = client.post(f"/repos/{owner}/{repo_name}/git/blobs", json=payload)
            if response.status_code not in (200, 201):
                raise RuntimeError(f"{rel}: {response.status_code} {response.text[:120]}")
            return rel, response.json()["sha"]

        entries = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            for rel, sha in pool.map(upload, files):
                entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": sha})
                if len(entries) % 40 == 0:
                    print(f"  已上传 {len(entries)}/{len(files)}")

        tree = client.post(f"/repos/{owner}/{repo_name}/git/trees", json={"tree": entries})
        if tree.status_code not in (200, 201):
            print("建树失败：", tree.status_code, tree.text[:300])
            return 1
        tree_sha = tree.json()["sha"]

        ref = client.get(f"/repos/{owner}/{repo_name}/git/ref/heads/main")
        parent = ref.json()["object"]["sha"] if ref.status_code == 200 else None
        commit_payload: dict = {"message": COMMIT_MESSAGE, "tree": tree_sha}
        if parent:
            commit_payload["parents"] = [parent]
        commit = client.post(f"/repos/{owner}/{repo_name}/git/commits", json=commit_payload)
        if commit.status_code not in (200, 201):
            print("提交失败：", commit.status_code, commit.text[:300])
            return 1
        commit_sha = commit.json()["sha"]

        if parent:
            updated = client.patch(f"/repos/{owner}/{repo_name}/git/refs/heads/main",
                                   json={"sha": commit_sha, "force": False})
        else:
            updated = client.post(f"/repos/{owner}/{repo_name}/git/refs",
                                  json={"ref": "refs/heads/main", "sha": commit_sha})
        if updated.status_code not in (200, 201):
            print("更新分支失败：", updated.status_code, updated.text[:300])
            return 1

        # 默认分支设为 main
        client.patch(f"/repos/{owner}/{repo_name}", json={"default_branch": "main"})

    print(f"\n完成：https://github.com/{owner}/{repo_name}")
    print(f"commit {commit_sha[:10]} · {len(files)} 个文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
