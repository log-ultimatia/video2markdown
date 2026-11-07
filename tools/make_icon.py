#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
make_icon.py —— 生成 assets/youtube2md.ico

设计：深蓝圆角方块 + 白色播放三角 + 文档横线（隐喻"视频 → 文本"）。
大尺寸带细节，小尺寸靠形状辨识（16px 时就是一个蓝方块 + 白三角，这已经够了）。

为什么用 bitmap_format="bmp"：
    ICO 里 48×48 及更小的帧如果不存成 BMP/DIB，Windows 资源管理器会把
    它们渲染成白页（PNG 帧只被部分新版组件支持）。这是踩过的坑。

重新生成：
    .venv\\Scripts\\python.exe make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

LAB_DIR = Path(__file__).resolve().parent
OUT = LAB_DIR / "assets" / "youtube2md.ico"

ACCENT = (31, 58, 95, 255)        # 深蓝，和界面标题栏一致
RED = (220, 53, 53, 255)          # 播放键红
WHITE = (255, 255, 255, 255)

SIZES = (16, 24, 32, 48, 64, 128, 256)


def rounded_rect(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def draw_icon(size: int) -> Image.Image:
    """按目标尺寸画一张，保证各尺寸都是矢量式重绘（不是简单缩放）。"""
    S = 1024                                  # 先在 1024 上画，最后缩到目标尺寸
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆角底板
    pad = int(S * 0.045)
    rounded_rect(d, (pad, pad, S - pad, S - pad), int(S * 0.19), ACCENT)

    # 白色"文档"卡片
    card_l, card_t = int(S * 0.20), int(S * 0.17)
    card_r, card_b = int(S * 0.80), int(S * 0.83)
    rounded_rect(d, (card_l, card_t, card_r, card_b), int(S * 0.07), WHITE)

    # 文档上的横线（代表文本）
    line_x0, line_x1 = int(S * 0.30), int(S * 0.70)
    y = int(S * 0.30)
    for width_ratio in (1.0, 0.82, 0.92):
        x1 = int(line_x0 + (line_x1 - line_x0) * width_ratio)
        d.rounded_rectangle((line_x0, y, x1, y + int(S * 0.045)),
                            radius=int(S * 0.022), fill=(200, 210, 222, 255))
        y += int(S * 0.105)

    # 红色播放圆 + 白色三角（右下角，代表"视频"）
    cx, cy, r = int(S * 0.665), int(S * 0.665), int(S * 0.195)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=RED)

    tri = int(r * 0.46)
    ox, oy = cx - int(r * 0.22), cy
    d.polygon([(ox - tri // 2, oy - tri),
               (ox - tri // 2, oy + tri),
               (ox + tri, oy)], fill=WHITE)

    return img.resize((size, size), Image.LANCZOS)


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    frames = [draw_icon(s) for s in SIZES]
    # 最大那张作为基准，其余作为多尺寸帧嵌入
    frames[-1].save(
        OUT,
        format="ICO",
        sizes=[(s, s) for s in SIZES],
        bitmap_format="bmp",      # 必须：否则小尺寸在资源管理器里显示白页
    )
    kb = OUT.stat().st_size / 1024
    print(f"已生成 {OUT}  ({kb:.1f} KB, {len(SIZES)} 个尺寸)")

    # 顺手导出一张 PNG 方便预览
    png = OUT.with_suffix(".png")
    frames[-1].save(png)
    print(f"预览图 {png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
