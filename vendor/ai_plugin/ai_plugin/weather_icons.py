"""
天气图标生成器 — 扁平化设计风格，2x 绘制后降采样实现抗锯齿
"""
from __future__ import annotations

import math
from PIL import Image, ImageDraw


def _antialiased(size: int, draw_fn) -> Image.Image:
    """在 2x 画布上绘制，再降采样到目标尺寸，实现抗锯齿"""
    s2 = size * 2
    canvas = Image.new("RGBA", (s2, s2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw_fn(draw, s2)
    return canvas.resize((size, size), Image.LANCZOS)


# ── 太阳 ─────────────────────────────────────────────
def _draw_sun(draw: ImageDraw.ImageDraw, s: int):
    cx, cy = s // 2, s // 2
    r = int(s * 0.2)
    color = (255, 195, 50, 240)
    ray_color = (255, 195, 50, 200)
    lw = max(2, int(s * 0.035))

    # 光芒
    inner = r + int(s * 0.06)
    outer = r + int(s * 0.22)
    for angle in range(0, 360, 45):
        rad = math.radians(angle)
        x1 = cx + int(inner * math.cos(rad))
        y1 = cy + int(inner * math.sin(rad))
        x2 = cx + int(outer * math.cos(rad))
        y2 = cy + int(outer * math.sin(rad))
        draw.line([(x1, y1), (x2, y2)], fill=ray_color, width=lw)
    # 主体
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)


def make_sun(size: int = 64) -> Image.Image:
    return _antialiased(size, _draw_sun)


# ── 云朵 ─────────────────────────────────────────────
def _draw_cloud(draw: ImageDraw.ImageDraw, s: int, alpha: int = 240):
    cx, cy = s // 2, s // 2 + int(s * 0.04)
    color = (255, 255, 255, alpha)
    # 底部大椭圆
    bw, bh = int(s * 0.4), int(s * 0.2)
    draw.ellipse([cx - bw, cy - bh // 2, cx + bw, cy + bh // 2], fill=color)
    # 左上
    lr = int(s * 0.2)
    lx, ly = cx - int(s * 0.18), cy - int(s * 0.12)
    draw.ellipse([lx - lr, ly - lr, lx + lr, ly + lr], fill=color)
    # 右上
    rr = int(s * 0.16)
    rx, ry = cx + int(s * 0.1), cy - int(s * 0.1)
    draw.ellipse([rx - rr, ry - rr, rx + rr, ry + rr], fill=color)
    # 顶部过渡
    tr = int(s * 0.12)
    tx, ty = cx - int(s * 0.02), cy - int(s * 0.18)
    draw.ellipse([tx - tr, ty - tr, tx + tr, ty + tr], fill=color)


def make_cloud(size: int = 64, alpha: int = 240) -> Image.Image:
    return _antialiased(size, lambda d, s: _draw_cloud(d, s, alpha))


# ── 多云（太阳+云）─────────────────────────────────
def make_partly_cloudy(size: int = 64) -> Image.Image:
    def _draw(draw, s):
        # 太阳（左上）
        sun = make_sun(int(s * 0.55))
        draw._image.paste(sun, (int(s * 0.04), int(s * 0.04)), sun)
        # 云（右下）
        _draw_cloud(draw, s, 245)
    canvas = Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    # 太阳
    sun = make_sun(int(size * 1.1))
    canvas.paste(sun, (int(size * 0.08), int(size * 0.08)), sun)
    # 云
    cloud = make_cloud(int(size * 2), 245)
    canvas.paste(cloud, (0, int(size * 0.4)), cloud)
    return canvas.resize((size, size), Image.LANCZOS)


# ── 阴天（两个云叠放）───────────────────────────────
def make_overcast(size: int = 64) -> Image.Image:
    canvas = Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))
    c1 = make_cloud(int(size * 1.7), 170)
    canvas.paste(c1, (int(size * 0.1), int(size * 0.2)), c1)
    c2 = make_cloud(int(size * 2), 230)
    canvas.paste(c2, (int(size * 0.15), int(size * 0.5)), c2)
    return canvas.resize((size, size), Image.LANCZOS)


# ── 雨 ──────────────────────────────────────────────
def make_rain(size: int = 64) -> Image.Image:
    canvas = Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))
    # 云
    cloud = make_cloud(int(size * 2), 230)
    canvas.paste(cloud, (0, int(size * 0.1)), cloud)
    # 雨滴
    draw = ImageDraw.Draw(canvas)
    rain = (140, 195, 255, 220)
    for dx, dy in [(-0.15, 0.35), (0.03, 0.42), (0.18, 0.33)]:
        x = int(size * 2 * (0.5 + dx))
        y = int(size * 2 * dy)
        dw = max(3, int(size * 0.08))
        dh = max(5, int(size * 0.13))
        draw.ellipse([x - dw, y, x + dw, y + dh * 2], fill=rain)
    return canvas.resize((size, size), Image.LANCZOS)


# ── 雪 ──────────────────────────────────────────────
def make_snow(size: int = 64) -> Image.Image:
    canvas = Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))
    cloud = make_cloud(int(size * 2), 220)
    canvas.paste(cloud, (0, int(size * 0.1)), cloud)
    draw = ImageDraw.Draw(canvas)
    snow = (210, 225, 255, 240)
    for dx, dy in [(-0.14, 0.36), (0.04, 0.44), (0.18, 0.34)]:
        x = int(size * 2 * (0.5 + dx))
        y = int(size * 2 * dy)
        r = max(3, int(size * 0.06))
        lw = max(2, int(size * 0.03))
        for angle in range(0, 180, 60):
            rad = math.radians(angle)
            x1 = x + int(r * math.cos(rad))
            y1 = y + int(r * math.sin(rad))
            x2 = x - int(r * math.cos(rad))
            y2 = y - int(r * math.sin(rad))
            draw.line([(x1, y1), (x2, y2)], fill=snow, width=lw)
        draw.ellipse([x - lw, y - lw, x + lw, y + lw], fill=snow)
    return canvas.resize((size, size), Image.LANCZOS)


# ── 雷 ──────────────────────────────────────────────
def make_thunder(size: int = 64) -> Image.Image:
    canvas = Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))
    cloud = make_cloud(int(size * 2), 200)
    canvas.paste(cloud, (0, int(size * 0.1)), cloud)
    draw = ImageDraw.Draw(canvas)
    bolt = (255, 210, 60, 240)
    cx = int(size * 2 * 0.52)
    y0 = int(size * 2 * 0.38)
    y1 = int(size * 2 * 0.78)
    bw = int(size * 2 * 0.1)
    pts = [
        (cx - bw, y0),
        (cx + int(bw * 0.3), y0 + int((y1 - y0) * 0.35)),
        (cx - int(bw * 0.2), y0 + int((y1 - y0) * 0.38)),
        (cx + bw, y1),
        (cx + int(bw * 0.1), y0 + int((y1 - y0) * 0.52)),
        (cx - int(bw * 0.4), y0 + int((y1 - y0) * 0.5)),
    ]
    draw.polygon(pts, fill=bolt)
    return canvas.resize((size, size), Image.LANCZOS)


# ── 雾 ──────────────────────────────────────────────
def make_fog(size: int = 64) -> Image.Image:
    def _draw(draw, s):
        fog = (220, 220, 225, 180)
        cy = s // 2
        lh = max(3, int(s * 0.04))
        for i, (dx, w) in enumerate([(0.15, 0.5), (0.05, 0.65), (0.1, 0.8)]):
            lx = int(s * dx)
            rx = int(s * w)
            ly = cy + int((dx - 0.15) * s * 0.7)
            draw.rounded_rectangle([lx, ly, rx, ly + lh], radius=lh // 2, fill=fog)
    return _antialiased(size, _draw)


# ── 小雨（区别于中雨/大雨）─────────────────────────
def make_light_rain(size: int = 64) -> Image.Image:
    canvas = Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))
    cloud = make_cloud(int(size * 2), 230)
    canvas.paste(cloud, (0, int(size * 0.1)), cloud)
    draw = ImageDraw.Draw(canvas)
    rain = (140, 195, 255, 200)
    # 只有两滴，更小
    for dx, dy in [(-0.08, 0.38), (0.1, 0.35)]:
        x = int(size * 2 * (0.5 + dx))
        y = int(size * 2 * dy)
        dw = max(2, int(size * 0.06))
        dh = max(4, int(size * 0.1))
        draw.ellipse([x - dw, y, x + dw, y + dh * 2], fill=rain)
    return canvas.resize((size, size), Image.LANCZOS)


# ── 阵雨（云+雨滴+太阳露出）────────────────────────
def make_shower(size: int = 64) -> Image.Image:
    canvas = Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))
    # 小太阳（右上角露出）
    sun = make_sun(int(size * 0.9))
    canvas.paste(sun, (int(size * 1.1), int(size * 0.05)), sun)
    # 云（遮住部分太阳）
    cloud = make_cloud(int(size * 2), 230)
    canvas.paste(cloud, (0, int(size * 0.3)), cloud)
    # 雨滴
    draw = ImageDraw.Draw(canvas)
    rain = (140, 195, 255, 210)
    for dx, dy in [(-0.1, 0.42), (0.08, 0.4)]:
        x = int(size * 2 * (0.5 + dx))
        y = int(size * 2 * dy)
        dw = max(3, int(size * 0.07))
        dh = max(5, int(size * 0.12))
        draw.ellipse([x - dw, y, x + dw, y + dh * 2], fill=rain)
    return canvas.resize((size, size), Image.LANCZOS)


# ── 工具函数 ────────────────────────────────────────
def get_icon(weather_code: str, size: int = 64) -> Image.Image:
    """根据天气代码返回对应图标"""
    code = weather_code.lower() if weather_code else ""
    if "lei" in code:
        return make_thunder(size)
    elif "zhenyu" in code or "zh阵" in code:
        return make_shower(size)
    elif "xiaoyu" in code or "小雨" in code:
        return make_light_rain(size)
    elif "yu" in code:
        return make_rain(size)
    elif "xue" in code:
        return make_snow(size)
    elif "wu" in code or "mai" in code:
        return make_fog(size)
    elif "qing" in code:
        return make_sun(size)
    elif "duoyun" in code:
        return make_partly_cloudy(size)
    elif "yin" in code:
        return make_overcast(size)
    else:
        return make_cloud(size)
