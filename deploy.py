#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把开发库同步到 ComfyUI 插件目录（部署）。

用法：
    python deploy.py                # 同步并列出改了哪些文件
    python deploy.py --dry-run      # 只看会改什么，不动盘
    python deploy.py --target D:\\别的\\路径   # 临时换目标

只复制代码与文档，不碰：
  .git/、__pycache__/、_backup_*/、*.pyc
  model_metadata.json / node_metadata.json / download_history.json /
  usage_cache.json / search_cache.json / user_model_urls.json /
  hf_candidate_cache.json / settings.json / wmd.log*
这些是插件运行期自己生成的，复制过去会覆盖用户数据。
"""

import argparse
import os
import shutil
import sys

# 插件目录（实机运行的那份）。DEPLOY.md 里也写了同一个路径。
DEFAULT_TARGET = r"E:\AI\ComfyUI-aki-v3.2\ComfyUI\custom_nodes\ComfyUI-Workflow-Models-Downloader"

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".github"}
SKIP_DIR_PREFIX = ("_backup_",)
SKIP_FILES = {
    "model_metadata.json",
    "node_metadata.json",
    "download_history.json",
    "usage_cache.json",
    "search_cache.json",
    "user_model_urls.json",
    "hf_candidate_cache.json",
    "settings.json",
    "node.zip",
}
SKIP_SUFFIX = (".pyc", ".log", ".partial")


def walk_source(root):
    """产出 (相对路径, 绝对路径)，顺序稳定，方便肉眼比对。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in SKIP_DIRS and not d.startswith(SKIP_DIR_PREFIX)
        )
        for name in sorted(filenames):
            if name in SKIP_FILES or name.startswith("wmd.log"):
                continue
            if name.endswith(SKIP_SUFFIX):
                continue
            full = os.path.join(dirpath, name)
            yield os.path.relpath(full, root), full


def same_content(a, b):
    """内容是否相同。换行符差异（CRLF/LF）不算差异——
    git 在不同 core.autocrlf 设置下检出的行尾可能不同，逐字节比会全是假改动。"""
    with open(a, "rb") as fa, open(b, "rb") as fb:
        da, db = fa.read(), fb.read()
    if da == db:
        return True
    return da.replace(b"\r\n", b"\n") == db.replace(b"\r\n", b"\n")


def main():
    ap = argparse.ArgumentParser(description="部署到 ComfyUI 插件目录")
    ap.add_argument("--target", default=DEFAULT_TARGET, help="插件目录")
    ap.add_argument("--dry-run", action="store_true", help="不写盘，只报告")
    args = ap.parse_args()

    source = os.path.dirname(os.path.abspath(__file__))
    target = os.path.abspath(args.target)

    if not os.path.isdir(target):
        sys.exit(f"目标目录不存在：{target}\n路径写错了？先确认插件装在哪。")
    for must in ("__init__.py", "server.py"):
        if not os.path.isfile(os.path.join(target, must)):
            sys.exit(f"目标不像插件目录（缺 {must}）：{target}")

    added, updated, same = [], [], 0
    for rel, src in walk_source(source):
        dst = os.path.join(target, rel)
        if not os.path.exists(dst):
            added.append(rel)
        elif same_content(src, dst):
            same += 1
        else:
            updated.append(rel)

    print(f"源  ：{source}")
    print(f"目标：{target}")
    print(f"未变 {same} 个文件")

    if not added and not updated:
        print("已是最新，无需部署。")
        return

    for rel in added:
        print(f"  + {rel}")
    for rel in updated:
        print(f"  ~ {rel}")

    if args.dry_run:
        print(f"\n[dry-run] 将新增 {len(added)} 个、覆盖 {len(updated)} 个文件，未写盘。")
        return

    for rel in added + updated:
        dst = os.path.join(target, rel)
        os.makedirs(os.path.dirname(dst) or target, exist_ok=True)
        shutil.copy2(os.path.join(source, rel), dst)

    print(f"\n已部署：新增 {len(added)} 个、覆盖 {len(updated)} 个。")

    touched = [r for r in added + updated]
    if any(r.endswith(".py") for r in touched):
        print("→ 改了 .py：必须重启 ComfyUI 才生效（路由在 import 时注册）。")
    if any(r.startswith("js/") or r.endswith((".js", ".css")) for r in touched):
        print("→ 改了前端：浏览器 Ctrl+F5 硬刷新，普通刷新会吃缓存。")


if __name__ == "__main__":
    main()
