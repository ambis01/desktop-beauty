# -*- coding: utf-8 -*-
"""生成「雾玻璃鼠标指针」效果对照图（只画图，不碰系统指针）。"""
import os
from PIL import Image, ImageDraw, ImageFilter, ImageFont

TMP = os.environ.get("TEMP", ".")
WALL = os.path.join(TMP, "cm-wall.png")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cursor-glass-mock.png")

# 经典箭头，32×32 单位空间，热点在 (0,0)
POLY = [(0.6, 0.6), (0.6, 25.2), (7.2, 19.0), (11.2, 28.6), (14.7, 27.0),
        (10.9, 18.2), (18.2, 18.2)]

BG = (14, 16, 21)
CARD = (26, 29, 37)
EDGE = (48, 54, 68)
TEXT = (238, 242, 250)
DIM = (150, 160, 178)

FONT = "C:/Windows/Fonts/msyh.ttc"
FONTB = "C:/Windows/Fonts/msyhbd.ttc"


def font(sz, bold=False):
    p = FONTB if bold else FONT
    try:
        return ImageFont.truetype(p, sz, index=0)
    except Exception:
        try:
            return ImageFont.truetype(FONT, sz, index=0)
        except Exception:
            return ImageFont.load_default()


def arrow_mask(s, ss=8):
    n = s * ss
    k = n / 32.0
    im = Image.new("L", (n, n), 0)
    ImageDraw.Draw(im).polygon([(x * k, y * k) for x, y in POLY], fill=255)
    return im.resize((s, s), Image.LANCZOS)


def paste_cursor(base, hot, s, style):
    """把一颗指针画到 base（RGBA 单元格）上，hot = 热点坐标。"""
    pad = 12
    m = arrow_mask(s)
    box = (pad, pad, pad + s, pad + s)

    # 投影（三种都加，真玻璃也需要一点浮起感）
    sh = Image.new("L", (s + 2 * pad, s + 2 * pad), 0)
    sh.paste(m, box)
    sh = sh.filter(ImageFilter.GaussianBlur(2.2))
    shadow = Image.new("RGBA", sh.size, (0, 0, 0, 0))
    shadow.putalpha(sh.point(lambda v: int(v * 0.42)))
    base.alpha_composite(shadow, (hot[0] - pad + 2, hot[1] - pad + 3))

    if style == "truefrost":
        px = hot[0] - pad
        py = hot[1] - pad
        region = base.crop((px, py, px + s + 2 * pad, py + s + 2 * pad))
        region = region.filter(ImageFilter.GaussianBlur(5))
        region = Image.blend(region, Image.new("RGBA", region.size, (255, 255, 255, 255)), 0.16)
        frost = Image.new("L", region.size, 0)
        frost.paste(m, box)
        base.alpha_composite(Image.composite(region, Image.new("RGBA", region.size, (0, 0, 0, 0)), frost), (px, py))

    if style == "fake":
        # 半透明白，垂直方向略渐变，模拟玻璃厚薄
        side = s + 2 * pad
        grad = Image.new("L", (side, side), 0)
        gd = ImageDraw.Draw(grad)
        for y in range(side):
            t = (y - pad) / max(1, s)
            t = 0.0 if t < 0 else (1.0 if t > 1 else t)
            a = int(255 * (0.62 - 0.10 * t))
            gd.line([(0, y), (side, y)], fill=a)
        fill = Image.new("RGBA", (side, side), (250, 252, 255, 255))
        fill.putalpha(grad)
        mask = Image.new("L", (s + 2 * pad, s + 2 * pad), 0)
        mask.paste(m, box)
        fill.putalpha(mask.point(lambda v: v * 168 // 255))
        base.alpha_composite(fill, (hot[0] - pad, hot[1] - pad))

    # 高光描边 + 内亮边
    edge = Image.new("RGBA", (s + 2 * pad, s + 2 * pad), (0, 0, 0, 0))
    d = ImageDraw.Draw(edge)
    pts = [(hot[0] - pad + hx, hot[1] - pad + hy) for hx, hy in
           [(p[0] * s / 32.0, p[1] * s / 32.0) for p in POLY]]
    if style == "system":
        d.line(pts + [pts[0]], fill=(255, 255, 255, 235), width=max(1, round(s / 26)))
        d.line(pts + [pts[0]], fill=(20, 20, 24, 255), width=max(1, round(s / 48)), joint="curve")
    else:
        d.line(pts + [pts[0]], fill=(255, 255, 255, 205), width=max(1, round(s / 30)), joint="curve")
    base.alpha_composite(edge)

    if style == "system":
        blob = Image.new("RGBA", (s + 2 * pad, s + 2 * pad), (0, 0, 0, 0))
        dm = Image.new("L", (s + 2 * pad, s + 2 * pad), 0)
        dm.paste(m, box)
        blob.paste((18, 18, 22, 255), (0, 0), dm)
        base.alpha_composite(blob, (hot[0] - pad, hot[1] - pad))


SIZES = [32, 48, 64]

def make():
    wall = Image.open(WALL).convert("RGBA")
    w, h = wall.size
    cw, ch = 336, 150
    # 取一块细节比较丰富的区域
    cx = int(w * 0.42) - cw // 2
    cy = int(h * 0.50) - ch // 2
    crop = wall.crop((cx, cy, cx + cw, cy + ch))

    rows = [
        ("桌面壁纸", crop.copy()),
        ("白色窗口", Image.new("RGBA", (cw, ch), (245, 246, 248, 255))),
        ("深色窗口", Image.new("RGBA", (cw, ch), (30, 32, 39, 255))),
    ]
    cols = [
        ("系统默认", "system", "Windows 自带的黑箭头"),
        ("假玻璃 .cur", "fake", "半透明白＋白描边（能做）"),
        ("真雾玻璃", "truefrost", "模糊背后像素（做不到）"),
    ]

    LW = 60
    VX = [96, 442, 788]
    VW = 336
    H = 48
    RH = ch
    top = 152
    gap = 10

    W = 1180
    Hc = top + len(rows) * (RH + gap) + 96
    img = Image.new("RGBA", (W, Hc), BG + (255,))
    d = ImageDraw.Draw(img)

    d.text((24, 20), "鼠标指针 × 雾玻璃", font=font(27, True), fill=TEXT)
    d.text((24, 60), "同一套观感，三种可行性，放在真实桌面尺寸里看", font=font(14), fill=DIM)
    d.text((W - 24, 26), "32 / 48 / 64 px", font=font(13), fill=DIM, anchor="ra")
    d.text((W - 24, 46), "热点＝箭尖", font=font(13), fill=DIM, anchor="ra")

    for i, (title, style, sub) in enumerate(cols):
        x = VX[i]
        d.rounded_rectangle([x, top - H - gap + 4, x + VW, top - gap + 4], 11, fill=CARD, outline=EDGE)
        d.text((x + 14, top - H + 6), title, font=font(16, True), fill=TEXT)
        d.text((x + 14, top - H + 27), sub, font=font(11), fill=DIM)

    for r, (rname, cellbg) in enumerate(rows):
        y = top + r * (RH + gap)
        d.text((24, y + ch // 2 - 8), rname, font=font(12), fill=DIM)
        for i, (_, style, _) in enumerate(cols):
            x = VX[i]
            cell = Image.new("RGBA", (cw, ch), (0, 0, 0, 255))
            cell.alpha_composite(cellbg.convert("RGBA"))
            xs = 44
            for s in SIZES:
                hy = 18 if s <= 48 else 14
                paste_cursor(cell, (xs, hy), s, style)
                xs += s + 34
            masked = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
            rounded = Image.new("L", (cw, ch), 0)
            ImageDraw.Draw(rounded).rounded_rectangle([0, 0, cw - 1, ch - 1], 10, fill=255)
            masked.paste(cell, (0, 0), rounded)
            img.alpha_composite(masked, (x, y))
            d.rounded_rectangle([x, y, x + VW - 1, y + ch - 1], 10, outline=EDGE)

    note_y = top + len(rows) * (RH + gap) + 12
    d.text((24, note_y),
           "假玻璃：白窗口上会发灰、深色照片上偏脏；真雾玻璃 .cur 根本存不下（指针位图里没有背景）",
           font=font(13), fill=(196, 205, 220))

    img.convert("RGB").save(OUT, "PNG")
    print("saved", OUT, img.size)


make()
