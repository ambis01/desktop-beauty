"""自绘天气图标（不依赖任何图片资源，跟着卡片缩放一起变大变小）。"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF

SUN = QColor(240, 176, 64)
CLOUD = QColor(124, 137, 158)
CLOUD_SOFT = QColor(152, 164, 184)
RAIN = QColor(74, 122, 202)
SNOW = QColor(102, 148, 206)
BOLT = QColor(240, 176, 64)
FOG = QColor(136, 150, 170)


def _cloud(p: QPainter, cx: float, cy: float, s: float, color: QColor) -> None:
    path = QPainterPath()
    path.addEllipse(QPointF(cx - 0.26 * s, cy + 0.04 * s), 0.19 * s, 0.19 * s)
    path.addEllipse(QPointF(cx, cy - 0.12 * s), 0.27 * s, 0.27 * s)
    path.addEllipse(QPointF(cx + 0.27 * s, cy + 0.03 * s), 0.21 * s, 0.21 * s)
    path.addRoundedRect(QRectF(cx - 0.46 * s, cy + 0.02 * s, 0.93 * s, 0.28 * s),
                        0.14 * s, 0.14 * s)
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawPath(path.simplified())


def _sun(p: QPainter, cx: float, cy: float, s: float) -> None:
    p.setPen(Qt.NoPen)
    p.setBrush(SUN)
    p.drawEllipse(QPointF(cx, cy), 0.20 * s, 0.20 * s)
    pen = QPen(SUN, max(1.2, 0.055 * s), Qt.SolidLine, Qt.RoundCap)
    p.setPen(pen)
    for i in range(8):
        a = math.radians(i * 45)
        p.drawLine(QPointF(cx + math.cos(a) * 0.28 * s, cy + math.sin(a) * 0.28 * s),
                   QPointF(cx + math.cos(a) * 0.40 * s, cy + math.sin(a) * 0.40 * s))


def _drops(p: QPainter, cx: float, cy: float, s: float, color: QColor,
           count: int = 3, length: float = 0.16) -> None:
    p.setPen(QPen(color, max(1.2, 0.05 * s), Qt.SolidLine, Qt.RoundCap))
    gap = 0.24 * s
    x0 = cx - gap * (count - 1) / 2.0
    for i in range(count):
        x = x0 + i * gap
        p.drawLine(QPointF(x, cy), QPointF(x - 0.03 * s, cy + length * s))


def _flakes(p: QPainter, cx: float, cy: float, s: float, count: int = 3) -> None:
    p.setPen(QPen(SNOW, max(1.0, 0.042 * s), Qt.SolidLine, Qt.RoundCap))
    gap = 0.26 * s
    x0 = cx - gap * (count - 1) / 2.0
    r = 0.06 * s
    for i in range(count):
        x = x0 + i * gap
        for ang in (0.0, 60.0, 120.0):
            a = math.radians(ang)
            p.drawLine(QPointF(x - math.cos(a) * r, cy - math.sin(a) * r),
                       QPointF(x + math.cos(a) * r, cy + math.sin(a) * r))


def _bolt(p: QPainter, cx: float, cy: float, s: float) -> None:
    poly = QPolygonF([
        QPointF(cx + 0.04 * s, cy - 0.06 * s),
        QPointF(cx - 0.10 * s, cy + 0.16 * s),
        QPointF(cx + 0.01 * s, cy + 0.16 * s),
        QPointF(cx - 0.05 * s, cy + 0.36 * s),
        QPointF(cx + 0.14 * s, cy + 0.10 * s),
        QPointF(cx + 0.02 * s, cy + 0.10 * s),
    ])
    p.setPen(Qt.NoPen)
    p.setBrush(BOLT)
    p.drawPolygon(poly)


def paint_icon(p: QPainter, rect: QRectF, icon: str) -> None:
    """在 rect 里画一个天气图标（居中、按短边缩放）。"""
    s = min(rect.width(), rect.height())
    if s <= 2:
        return
    cx = rect.center().x()
    cy = rect.center().y()
    kind = icon or "cloud"
    if kind == "sun":
        _sun(p, cx, cy, s)
        return
    if kind == "partly":
        p.save()
        _sun(p, cx + 0.16 * s, cy - 0.20 * s, s * 0.72)
        p.restore()
        _cloud(p, cx - 0.06 * s, cy + 0.10 * s, s * 0.86, CLOUD)
        return
    if kind == "fog":
        _cloud(p, cx, cy - 0.10 * s, s * 0.88, CLOUD_SOFT)
        p.setPen(QPen(FOG, max(1.2, 0.05 * s), Qt.SolidLine, Qt.RoundCap))
        for i in range(3):
            y = cy + (0.16 + i * 0.11) * s
            p.drawLine(QPointF(cx - 0.34 * s, y), QPointF(cx + 0.34 * s, y))
        return
    # 其余都是「云 + 下面对应天气」
    _cloud(p, cx, cy - 0.12 * s, s * 0.9, CLOUD)
    base = cy + 0.16 * s
    if kind == "cloud":
        return
    if kind == "drizzle":
        _drops(p, cx, base, s, RAIN, count=2, length=0.10)
    elif kind == "rain":
        _drops(p, cx, base, s, RAIN, count=3, length=0.17)
    elif kind == "heavy_rain":
        _drops(p, cx, base, s, RAIN, count=4, length=0.22)
    elif kind == "snow":
        _flakes(p, cx, base + 0.02 * s, s, count=3)
    elif kind == "thunder":
        _bolt(p, cx, base - 0.04 * s, s)
    else:
        _drops(p, cx, base, s, RAIN, count=3, length=0.15)
