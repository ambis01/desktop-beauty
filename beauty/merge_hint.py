"""拖拽合并时的提示浮层：目标图标高亮 + 「松手创建收纳盒」。"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

PAD = 6
LABEL_H = 26


class MergeHint(QWidget):
    def __init__(self):
        flags = (Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
                 | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        super().__init__(None, flags)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self._ring = QRect()
        self._text = "松手创建收纳盒"

    def show_for(self, rect, text: str = "松手创建收纳盒") -> None:
        left, top, right, bottom = (int(v) for v in rect)
        w = max(24, right - left)
        h = max(24, bottom - top)
        self._ring = QRect(PAD, LABEL_H + PAD, w, h)
        self._text = text
        geo = QRect(left - PAD, top - PAD - LABEL_H, w + PAD * 2, h + PAD * 2 + LABEL_H)
        screen = QApplication.screenAt(geo.center()) or QApplication.primaryScreen()
        avail = screen.availableGeometry()
        x = min(max(geo.left(), avail.left()), avail.right() - geo.width())
        y = min(max(geo.top(), avail.top()), avail.bottom() - geo.height())
        self.setGeometry(QRect(x, y, geo.width(), geo.height()))
        self.show()
        self.raise_()
        self.update()

    def paintEvent(self, _event) -> None:
        if self._ring.isNull():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        ring = QRectF(self._ring).adjusted(1.0, 1.0, -1.0, -1.0)
        p.setBrush(QColor(140, 188, 255, 34))
        p.setPen(QPen(QColor(150, 194, 255, 235), 2.0))
        p.drawRoundedRect(ring, 16, 16)

        font = QFont("Microsoft YaHei UI")
        font.setPixelSize(11)
        p.setFont(font)
        tw = p.fontMetrics().horizontalAdvance(self._text) + 22
        pill = QRectF((self.width() - tw) / 2, 1.0, tw, 21.0)
        p.setBrush(QColor(26, 30, 40, 236))
        p.setPen(QPen(QColor(255, 255, 255, 46), 1.0))
        p.drawRoundedRect(pill, 10.5, 10.5)
        p.setPen(QColor(236, 242, 255))
        p.drawText(pill, Qt.AlignCenter, self._text)
