"""轻量提示条：屏幕下方浮一条玻璃小胶囊，过一会儿自己消失。

以前这些提示是托盘气泡（QSystemTrayIcon.showMessage），现在托盘已经去掉了。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import QApplication

from .glass import GlassWindow


class Toast(GlassWindow):
    def __init__(self):
        flags = (Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
                 | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        super().__init__(None, flags, radius=14, activate=False, dark=True)
        self.glass_alpha = 232
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.text = ""
        self._font = QFont("Microsoft YaHei UI")
        self._font.setPixelSize(12)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_text(self, text: str, ms: int = 1900) -> None:
        self.text = str(text)
        fm = QFontMetrics(self._font)
        w = min(620, fm.horizontalAdvance(self.text) + 48)
        h = 38
        scr = QApplication.primaryScreen()
        area = scr.availableGeometry() if scr is not None else self.rect()
        x = area.center().x() - w // 2
        y = area.bottom() - h - 92
        self.setGeometry(x, y, w, h)
        self.show()
        self.raise_()
        self.update()
        self._timer.start(max(600, int(ms)))

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setFont(self._font)
        p.setPen(QColor(255, 255, 255, 240))
        p.drawText(self.rect().adjusted(16, 0, -16, 0),
                   Qt.AlignHCenter | Qt.AlignVCenter, self.text)
