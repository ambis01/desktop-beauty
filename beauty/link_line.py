"""两张音乐卡片之间的那条「连线」。

拖动时要跨卡片窗口画一条线，所以得有一个鼠标点得穿过去、永远在最上面的透明
窗口。窗口只占线附近那一小块（跟着线挪），不是铺满整个桌面——铺满的话每帧都要
重画一大张透明位图，拖动会一顿一顿的。

样式：一根细的白线，外面裹一层虚虚的白光，像一道光而不是一根笔画的线。
朦胧感是靠十几层同心的半透明描边叠出来的：越往外越宽、越透明，透明度按中间高
两头低排成一条曲线，叠起来就近似一条「细线被模糊过」的光带。比每帧把整张桌面
模糊一遍便宜得多（拖动时每帧都要重画）。
"""
from __future__ import annotations

import time

from PySide6.QtCore import QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from .dlog import dlog

INK = QColor(255, 255, 255)      # 线本身：白色的
GRID = 64                        # 窗口位置/尺寸按这个对齐：别每帧都改窗口大小

# 亮芯：细，但不能细到看不见（4 逻辑像素 ≈ 125% 缩放下的 5 物理像素）
CORE_W = 4.0
# (线宽, 透明度)：从外到内。一层比一层窄、一层比一层实，叠出来的横截面近似
# 一条高斯曲线（一条细线被模糊过的样子），所以边缘是化开的、不是硬边
HALO = ((28.0, 8), (23.0, 11), (19.0, 15), (15.5, 20), (12.5, 26),
        (10.0, 34), (8.0, 44), (6.4, 56), (5.0, 74))


class _LinkLine(QWidget):
    def __init__(self) -> None:
        super().__init__(None,
                         Qt.Tool | Qt.FramelessWindowHint
                         | Qt.WindowStaysOnTopHint
                         | Qt.WindowTransparentForInput
                         | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self._p0: QPoint | None = None
        self._p1: QPoint | None = None
        self._hot = False         # 已经搭到目标上了（样子不变，只留给逻辑用）
        self._frames = 0          # 这次拖动画了多少帧 / 一共多久：用来看卡不卡
        self._paint_ms = 0.0
        self._worst_ms = 0.0

    def _place(self, p0: QPoint, p1: QPoint) -> None:
        """把窗口挪到这条线要占的那一小块，而不是铺满整个桌面。

        铺满桌面的时候，每帧都要把 2560×1440 的透明窗口整个重画一遍再交给合成器，
        拖动就一顿一顿的；窗口只有线附近这么大，就快了。
        位置按 GRID 对齐，免得每帧都改一次窗口尺寸。
        """
        box = self._bbox(p0, p1)
        x = box.x() // GRID * GRID
        y = box.y() // GRID * GRID
        w = -(-box.width() // GRID) * GRID + GRID
        h = -(-box.height() // GRID) * GRID + GRID
        rect = QRect(x, y, w, h)
        if self.geometry() != rect:
            self.setGeometry(rect)

    @staticmethod
    def _bbox(a: QPoint, b: QPoint) -> QRect:
        """这条线要占的框（含光晕和圆头）。"""
        rect = QRect(a, b).normalized()
        m = int(max(w for w, _ in HALO) * 0.5) + 10
        return rect.adjusted(-m, -m, m, m)

    def set_line(self, p0: QPoint, p1: QPoint, hot: bool = False) -> None:
        self._place(p0, p1)
        self._p0, self._p1, self._hot = QPoint(p0), QPoint(p1), bool(hot)
        if not self.isVisible():
            self.show()
        self.update()

    def clear_line(self) -> None:
        if self._frames >= 8:
            dlog(f"line: {self._frames} 帧 平均 {self._paint_ms / self._frames:.1f}ms "
                 f"最慢 {self._worst_ms:.1f}ms")
        self._frames = 0
        self._paint_ms = 0.0
        self._worst_ms = 0.0
        self._p0 = self._p1 = None
        self._hot = False
        self.hide()

    def paintEvent(self, _event) -> None:
        if self._p0 is None or self._p1 is None:
            return
        t0 = time.perf_counter()
        origin = self.pos()
        a = QPointF(self._p0 - origin)
        b = QPointF(self._p1 - origin)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        # 光晕：由外到内一层层叠上去，外面散得开、里面实，边缘就是化开的
        for width, alpha in HALO:
            col = QColor(INK)
            col.setAlpha(alpha)
            pen = QPen(col, width)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawLine(a, b)
        # 亮芯：中间那根白线。搭上目标之后不再加亮，免得刺眼——那时两颗按钮
        # 的圈已经各自亮着了，足够说明「搭上了」
        col = QColor(INK)
        col.setAlpha(245)
        pen = QPen(col, CORE_W)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawLine(a, b)
        p.end()
        dt = (time.perf_counter() - t0) * 1000.0
        self._frames += 1
        self._paint_ms += dt
        self._worst_ms = max(self._worst_ms, dt)


_win: _LinkLine | None = None


def _window() -> _LinkLine:
    global _win
    if _win is None:
        _win = _LinkLine()
    return _win


def show_line(p0: QPoint, p1: QPoint, hot: bool = False) -> None:
    _window().set_line(p0, p1, hot)


def hide_line() -> None:
    if _win is not None:
        _win.clear_line()
