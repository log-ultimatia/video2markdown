#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
check_links.py —— 检查 Markdown 里的相对链接是否都指向存在的文件

为什么要它：README 和 docs/ 里互相引用得比较多，还引用截图。
改文件名或移动目录时，断链靠肉眼很难发现，CI 里自动查一遍最省事。

用法（在项目根目录执行）：
    python tools/check_links.py

退出码：0 = 全部有效，1 = 有断链
"""

from __future__ import annotations

import pathlib
import re
import sys

# Windows 的英文版控制台默认 cp1252，输出中文会 UnicodeEncodeError。
# 保留控制台编码，只把无法编码的字符替换掉，避免脚本直接崩。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

# 只检查这些标记内的链接：[文字](目标)
LINK_RE = re.compile(r"\]\(([^)\s]+?)\)")
SKIP_PREFIX = ("http://", "https://", "mailto:", "#", "data:")
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules",
             "models", "audio", "downloads", "output", "logs"}


def find_markdown(root: pathlib.Path) -> list[pathlib.Path]:
    """找出仓库里需要检查的 Markdown 文件（跳过运行时产物目录）。"""
    out = []
    for p in root.rglob("*.md"):
        if any(part in SKIP_DIRS or part.startswith(".") and part != "." 
               for part in p.relative_to(root).parts[:-1]):
            continue
        out.append(p)
    return sorted(out)


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    files = find_markdown(root)

    if not files:
        print("没有找到 Markdown 文件")
        return 1

    broken = []
    checked = 0

    for md in files:
        try:
            text = md.read_text(encoding="utf-8")
        except Exception as exc:
            print(f"[警告] 读不了 {md}: {exc}")
            continue

        for m in LINK_RE.finditer(text):
            target = m.group(1).strip()
            if not target or target.startswith(SKIP_PREFIX):
                continue

            # 去掉锚点和查询串
            path_part = target.split("#")[0].split("?")[0]
            if not path_part:
                continue
            # 站内绝对路径 / 目录引用无法可靠判断，跳过
            if path_part.startswith("/"):
                continue

            checked += 1
            resolved = (md.parent / path_part).resolve()
            if not resolved.exists():
                broken.append(f"{md.relative_to(root).as_posix()} -> {target}")

    print(f"检查了 {len(files)} 个 Markdown 文件、{checked} 个相对链接")

    if broken:
        print(f"\n发现 {len(broken)} 个断链：")
        for b in broken:
            print(f"  - {b}")
        return 1

    print("没有断链")
    return 0


if __name__ == "__main__":
    sys.exit(main())
