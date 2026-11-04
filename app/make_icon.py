"""生成 app/app.ico（16/32/48/256 多尺寸），纯 Python，无第三方依赖。

用法：python app/make_icon.py
设计：深蓝圆角底 + 蓝紫渐变 + 白色 V 字，和程序 UI 的深色蓝紫主题一致。
ICO 内每张图用经典 32bpp BMP 帧（LoadImageW 兼容性最好）。
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

SIZES = (16, 32, 48, 256)
OUT_PATH = Path(__file__).resolve().parent / "app.ico"

# 主题色
C_TOP_LEFT = (13, 27, 62)      # 深蓝 #0D1B3E
C_BOTTOM_RIGHT = (109, 40, 217)  # 蓝紫 #6D28D9
C_GLOW = (59, 130, 246)        # 亮蓝 #3B82F6
C_V = (255, 255, 255)


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))  # type: ignore[return-value]


def _inside_rounded(x: float, y: float, size: float, radius: float) -> bool:
    if x < radius and y < radius:
        return (x - radius) ** 2 + (y - radius) ** 2 <= radius * radius
    if x > size - radius and y < radius:
        return (x - (size - radius)) ** 2 + (y - radius) ** 2 <= radius * radius
    if x < radius and y > size - radius:
        return (x - radius) ** 2 + (y - (size - radius)) ** 2 <= radius * radius
    if x > size - radius and y > size - radius:
        return (x - (size - radius)) ** 2 + (y - (size - radius)) ** 2 <= radius * radius
    return True


def _dist_to_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _render(size: int, samples: int = 4) -> bytes:
    """返回 RGBA 像素（行优先）。用 samples×samples 超采样做抗锯齿。"""
    radius = size * 0.24
    half_thick = size * 0.085
    # V 的两条笔画
    seg_a = (size * 0.26, size * 0.25, size * 0.5, size * 0.74)
    seg_b = (size * 0.74, size * 0.25, size * 0.5, size * 0.74)
    pixels = bytearray(size * size * 4)

    for y in range(size):
        for x in range(size):
            r = g = b = a = 0.0
            for sy in range(samples):
                for sx in range(samples):
                    px = x + (sx + 0.5) / samples
                    py = y + (sy + 0.5) / samples
                    if not _inside_rounded(px, py, size, radius):
                        continue
                    # 对角渐变 + 中心柔光
                    t = (px + py) / (2 * size)
                    base = _lerp(C_TOP_LEFT, C_BOTTOM_RIGHT, min(1.0, t * 1.15))
                    glow = math.exp(-(((px - size * 0.42) ** 2 + (py - size * 0.34) ** 2) / (size * size * 0.22)))
                    col = _lerp(base, C_GLOW, glow * 0.45)
                    d = min(
                        _dist_to_segment(px, py, *seg_a),
                        _dist_to_segment(px, py, *seg_b),
                    )
                    if d <= half_thick:
                        col = C_V
                    elif d <= half_thick + max(1.0, size / 64):
                        # 笔画边缘轻微压暗过渡，避免小尺寸糊成一团
                        edge = (d - half_thick) / max(1.0, size / 64)
                        col = _lerp(col, C_V, 1.0 - edge)
                    r += col[0]
                    g += col[1]
                    b += col[2]
                    a += 255
            total = samples * samples
            if a > 0:
                # 平均时只统计覆盖到的采样，颜色不受透明区域影响
                count = a / 255
                pixels[(y * size + x) * 4 + 0] = int(round(r / count))
                pixels[(y * size + x) * 4 + 1] = int(round(g / count))
                pixels[(y * size + x) * 4 + 2] = int(round(b / count))
                pixels[(y * size + x) * 4 + 3] = int(round(a / total))
    return bytes(pixels)


def _dib(size: int, rgba: bytes) -> bytes:
    """经典 32bpp DIBSection 帧（BGRA，自下而上）+ 1bpp AND 掩码。

    Windows 的 LoadImageW(..., LR_LOADFROMFILE) 对 PNG 压缩的 ICO 兼容性不稳，
    这里统一用 BMP 帧，保证托盘/窗口/资源管理器都能稳定加载。
    """
    header = struct.pack(
        "<IiiHHIIiiII",
        40,          # biSize
        size,        # biWidth
        size * 2,    # biHeight（XOR + AND 两张图）
        1,           # biPlanes
        32,          # biBitCount
        0,           # biCompression = BI_RGB
        size * size * 4,  # biSizeImage
        0, 0, 0, 0,
    )
    rows = bytearray()
    for y in range(size - 1, -1, -1):
        row = rgba[y * size * 4 : (y + 1) * size * 4]
        for x in range(size):
            r, g, b, a = row[x * 4 : x * 4 + 4]
            rows += bytes((b, g, r, a))
    # AND 掩码：alpha=0 处为 1（透明），其余 0；每行按 4 字节对齐
    mask_stride = ((size + 31) // 32) * 4
    mask = bytearray()
    for y in range(size - 1, -1, -1):
        line = bytearray(mask_stride)
        for x in range(size):
            if rgba[(y * size + x) * 4 + 3] == 0:
                line[x // 8] |= 0x80 >> (x % 8)
        mask += line
    return header + bytes(rows) + bytes(mask)


def build_ico() -> None:
    images = [(size, _dib(size, _render(size, 4 if size <= 64 else 2))) for size in SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries = bytearray()
    body = bytearray()
    for size, data in images:
        entries += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,
            0 if size >= 256 else size,
            0, 0, 1, 32, len(data), offset,
        )
        body += data
        offset += len(data)
    OUT_PATH.write_bytes(bytes(header) + bytes(entries) + bytes(body))
    print(f"OK {OUT_PATH} ({OUT_PATH.stat().st_size} bytes, sizes={list(SIZES)})")


if __name__ == "__main__":
    build_ico()
