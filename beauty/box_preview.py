"""拖拽合并时的预览：在目标图标位置先「摆」一个盒子出来，松手就地生成。

只是画两个图标 + 白玻璃，跟真的收纳盒长得一样，松手后由真的盒子接管。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QApplication

from .glass import GlassWindow
from . import icons

COLLAPSED = 96
MINI = 30
GAP = 8


class BoxPreview(GlassWindow):
    def __init__(self):
        flags = (Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
                 | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        super().__init__(None, flags, radius=20, activate=False)
        self.glass_alpha = 212
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._pixmaps: list = []
        self._paths: list[str] = []

    def show_for(self, rect, paths) -> None:
        """rect 是目标图标的屏幕矩形（逻辑像素）。"""
        left, top, right, bottom = (int(v) for v in rect)
        cx = (left + right) // 2
        cy = (top + bottom) // 2
        x = cx - COLLAPSED // 2
        y = cy - COLLAPSED // 2
        screen = QApplication.screenAt(QPoint(cx, cy)) or QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            x = min(max(x, avail.left() + 4), avail.right() - COLLAPSED - 4)
            y = min(max(y, avail.top() + 4), avail.bottom() - COLLAPSED - 4)
        self._paths = [str(p) for p in paths][:4]
        self._pixmaps = []
        for p in self._paths:
            target = p
            if p.lower().endswith(".lnk"):
                from .shortcut import target_of
                hit = target_of(Path(p))
                if hit is not None:
                    target = str(hit)
            pm = icons.big_pixmap(target, MINI)
            self._pixmaps.append(pm)
        self.setGeometry(QRect(x, y, COLLAPSED, COLLAPSED))
        self.show()
        self.raise_()
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        pixmaps = [pm for pm in self._pixmaps[:4] if pm is not None and not pm.isNull()]
        if not pixmaps:
            return
        cols = 2 if len(pixmaps) > 1 else 1
        rows = 1 if len(pixmaps) <= 2 else 2
        w = cols * MINI + (cols - 1) * GAP
        h = rows * MINI + (rows - 1) * GAP
        x0 = (self.width() - w) // 2
        y0 = (self.height() - h) // 2
        for i, pm in enumerate(pixmaps):
            col, row = i % cols, i // cols
            p.drawPixmap(x0 + col * (MINI + GAP), y0 + row * (MINI + GAP), pm)
