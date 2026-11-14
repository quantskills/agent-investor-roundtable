"""量化大富翁图标 v5 —— 极简对称柱状图。

约束：
- 颜色 ≤ 2：金色 + 白色（深色底为容器不算）
- 元素 ≤ 2：金色圆环 + 白色对称柱状图
- 左右对称：柱子高度从两侧到中央递增，形成"山形"对称
"""
from __future__ import annotations
import io, struct
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "quant_billionaire.ico"
PREVIEW = ROOT / "quant_billionaire_preview.png"

GOLD = (228, 176, 48, 255)
WHITE = (255, 255, 255, 240)

def _rounded_mask(size: int, radius: int) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m

def draw_icon(size: int = 256) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 深色渐变底
    top_c, bot_c = (14, 18, 27), (24, 32, 52)
    for y in range(size):
        t = y / max(1, size - 1)
        c = tuple(round(top_c[i] + (bot_c[i] - top_c[i]) * t) for i in range(3))
        d.line([(0, y), (size, y)], fill=c + (255,))
    img.putalpha(_rounded_mask(size, int(size * 0.22)))

    # ── 元素1：金色圆环 ──
    m = int(size * 0.10)
    rw = max(3, size // 28)
    d.ellipse([m, m, size - m, size - m], outline=GOLD, width=rw)

    # ── 元素2：对称柱状图（5根白色柱子，中间最高，两侧递减） ──
    cx = size / 2
    bar_w = size * 0.07
    gap = size * 0.025
    base_y = size * 0.74

    # 对称高度比例：外侧矮、中间高
    heights = [0.14, 0.24, 0.34, 0.24, 0.14]
    n = len(heights)
    total_w = n * bar_w + (n - 1) * gap
    start_x = cx - total_w / 2

    for i, h_ratio in enumerate(heights):
        x = start_x + i * (bar_w + gap)
        bh = size * h_ratio
        d.rectangle([x, base_y - bh, x + bar_w, base_y], fill=WHITE)

    return img

def main() -> None:
    sizes = [(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)]
    frames = [draw_icon(w).convert("RGBA") for w, _ in sizes]
    images = []
    for im in frames:
        b = io.BytesIO(); im.save(b, format="PNG"); images.append(b.getvalue())
    header = struct.pack("<HHH", 0, 1, len(images))
    off = 6 + 16 * len(images)
    entries = b""
    payload = b""
    for (w, h), img in zip(sizes, images):
        bw = 0 if w >= 256 else w
        bh = 0 if h >= 256 else h
        entries += struct.pack("<BBBBHHII", bw, bh, 0, 0, 1, 32, len(img), off)
        payload += img
        off += len(img)
    OUT.write_bytes(header + entries + payload)
    print("ICON_SAVED", OUT, OUT.stat().st_size)
    draw_icon(256).save(str(PREVIEW), format="PNG")
    print("PREVIEW_SAVED", PREVIEW, PREVIEW.stat().st_size)

if __name__ == "__main__":
    main()
