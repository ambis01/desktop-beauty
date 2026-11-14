"""面板弹出的两个小窗口：取色盘、单行输入框。

Qt 自带的 QColorDialog / QInputDialog 是系统那套灰底控件，和自绘的玻璃面板
摆在一起就是「两个软件」。这里全部自己画：

- ColorWheel：圆盘 = 色相（角度）+ 饱和度（半径），光亮单独一根条；
- GlassInput：一行输入 + 确定/取消。
两者都是 GlassWindow，背景走 glass.paint_frost，和面板同一个外观、同一个圆角。
"""
from __future__ import annotations

import math

from PySide6.QtCore import QEventLoop, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor, QConicalGradient, QLinearGradient, QPainter, QPainterPath, QPen,
    QPixmap, QRadialGradient,
)
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from .glass import GlassWindow, paint_frost
from .hotkey import MOD_ALT, MOD_CONTROL, MOD_SHIFT, hotkey_text, qt_to_vk
from .i18n import tr


class _FrostDialog(GlassWindow):
    """取色盘和输入框的共同底座：同样的玻璃、圆角、命中处理。"""

    closed = Signal()

    def __init__(self, flags, radius: float, dark: bool, parent=None):
        super().__init__(parent, flags, radius=radius, activate=True, dark=dark)
        self._dark = bool(dark)
        # 玻璃完全交给 paint_frost 自绘；基类那层白玻璃关掉，否则蓝紫光斑会被
        # 盖成一片灰白（和设置面板同一套处理）。
        self.native_backdrop = False
        self._backdrop = None
        self.real_blur = False
        self.glass_alpha = 130
        self.tint = QColor(255, 255, 255, 40)
        self.bg_opacity = 0.0
        self._keep_background = True
        self._emitted = False
        self.setStyleSheet(_QSS_DARK if self._dark else _QSS)

    # ---- 绘制 ----
    def paintEvent(self, event) -> None:
        p = QPainter(self)
        try:
            paint_frost(self, p, self._dark)
        except (RuntimeError, TypeError):
            pass
        finally:
            p.end()
        super().paintEvent(event)

    # ---- 收尾 ----
    def _finish(self) -> None:
        if self._emitted:
            return
        self._emitted = True
        self.closed.emit()
        self.close()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)

    def reject(self) -> None:
        self._finish()

    def closeEvent(self, event) -> None:
        if not self._emitted:
            self._emitted = True
            self.closed.emit()
        super().closeEvent(event)

    def center_over(self, parent) -> None:
        """摆在父窗口正中间（没有父窗口就摆屏幕中间），再拉回屏幕里。

        卡片可能贴在屏幕边上，小窗照父窗口居中就会有一半跑到屏幕外，所以最后
        按**父窗口所在的那块屏幕**把位置夹回来。
        """
        try:
            home = parent.frameGeometry() if parent is not None else None
        except (RuntimeError, TypeError):
            home = None
        from PySide6.QtWidgets import QApplication
        scr = None
        if parent is not None:
            try:
                scr = parent.screen()
            except (RuntimeError, TypeError, AttributeError):
                scr = None
        if scr is None:
            scr = QApplication.primaryScreen()
        if home is None or home.isNull():
            if scr is not None:
                home = scr.availableGeometry()
        if home is not None and not home.isNull():
            x = home.center().x() - self.width() // 2
            y = home.center().y() - self.height() // 2
            if scr is not None:
                area = scr.availableGeometry()
                x = min(max(x, area.left() + 8),
                        area.right() + 1 - self.width() - 8)
                y = min(max(y, area.top() + 8),
                        area.bottom() + 1 - self.height() - 8)
            self.move(x, y)


class ColorWheel(QWidget):
    """色相/饱和度圆盘。角度 = 色相，到圆心的距离 = 饱和度。"""

    changed = Signal(QColor)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._h = 0
        self._s = 255
        self._v = 255
        self._cache = None          # (w, h, dpr, pixmap)：只缓存色相+饱和度
        self.setMinimumSize(150, 150)
        self.setCursor(Qt.CrossCursor)

    # ---- 数据 ----
    def sizeHint(self) -> QSize:
        return QSize(168, 168)

    def color(self) -> QColor:
        return QColor.fromHsv(self._h, self._s, self._v)

    def hue(self) -> int:
        return self._h

    def saturation(self) -> int:
        return self._s

    def value(self) -> int:
        return self._v

    def set_value(self, v: int) -> None:
        v = max(0, min(255, int(v)))
        if v != self._v:
            self._v = v
            self.update()

    def set_color(self, color: QColor) -> None:
        if not color.isValid():
            return
        h, s, v, _a = color.getHsv()
        self._h = max(0, h)
        self._s, self._v = s, v
        self.update()

    # ---- 几何 ----
    def _disc(self):
        d = float(min(self.width(), self.height()))
        r = max(10.0, d * 0.5 - 5.0)
        return QPointF(self.width() * 0.5, self.height() * 0.5), r

    def _wheel_pixmap(self) -> QPixmap:
        key = (self.width(), self.height(), round(self.devicePixelRatioF(), 3))
        if self._cache is not None and self._cache[0] == key:
            return self._cache[1]
        c, r = self._disc()
        dpr = self.devicePixelRatioF()
        pm = QPixmap(max(1, int(self.width() * dpr)),
                     max(1, int(self.height() * dpr)))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        pp = QPainter(pm)
        pp.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(c.x() - r, c.y() - r, r * 2.0, r * 2.0)
        path = QPainterPath()
        path.addEllipse(rect)
        pp.save()
        pp.setClipPath(path)
        # 1) 色相：锥形渐变，0 度在 3 点钟方向，逆时针
        cone = QConicalGradient(c, 0.0)
        steps = 36
        for i in range(steps + 1):
            pos = i / float(steps)
            cone.setColorAt(pos, QColor.fromHsvF(min(0.9999, pos), 1.0, 1.0))
        pp.setPen(Qt.NoPen)
        pp.setBrush(cone)
        pp.drawEllipse(rect)
        # 2) 饱和度：圆心白 → 边缘透明（白越少越饱和）
        sat = QRadialGradient(c, r)
        sat.setColorAt(0.00, QColor(255, 255, 255, 255))
        sat.setColorAt(0.55, QColor(255, 255, 255, 120))
        sat.setColorAt(1.00, QColor(255, 255, 255, 0))
        pp.setBrush(sat)
        pp.drawEllipse(rect)
        pp.restore()
        # 3) 收边
        pp.setBrush(Qt.NoBrush)
        pp.setPen(QPen(QColor(0, 0, 0, 40), 1.0))
        pp.drawPath(path)
        pp.end()
        self._cache = (key, pm)
        return pm

    # ---- 绘制 ----
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        c, r = self._disc()
        # 圆盘始终画满亮度：跟着亮度一起变暗的话，选深色时整盘糊成一团，
        # 反而看不清该往哪儿拖。亮度只体现在左上角的色块和亮度条上。
        p.drawPixmap(0, 0, self._wheel_pixmap())
        # 当前点：白色实心小圆 + 一圈细暗边。浅色区（低饱和）靠暗边看得见，
        # 深色区（高饱和）靠白心看得见。
        ang = math.radians(float(self._h))
        rr = r * (self._s / 255.0)
        x = c.x() + math.cos(ang) * rr
        y = c.y() - math.sin(ang) * rr
        p.setBrush(QColor(255, 255, 255, 245))
        p.setPen(QPen(QColor(0, 0, 0, 120), 1.5))
        p.drawEllipse(QPointF(x, y), 6.0, 6.0)
        p.end()

    # ---- 交互 ----
    def _pick_at(self, pos: QPointF) -> None:
        c, r = self._disc()
        dx = pos.x() - c.x()
        dy = c.y() - pos.y()
        dist = math.hypot(dx, dy)
        if dist < 1e-3:
            hue = self._h
        else:
            hue = int(round(math.degrees(math.atan2(dy, dx)))) % 360
        sat = int(round(max(0.0, min(1.0, dist / r)) * 255))
        if (hue, sat) == (self._h, self._s):
            return
        self._h, self._s = hue, sat
        self.update()
        self.changed.emit(self.color())

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._pick_at(event.position())
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & Qt.LeftButton:
            self._pick_at(event.position())
            event.accept()


class _GradientBar(QWidget):
    """一条横向渐变条 + 白色滑块。亮度条和透明度条长得一样，只换填充。"""

    changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._v = 255
        self.setFixedHeight(20)
        self.setMinimumWidth(80)

    # ---- 数据 ----
    def value(self) -> int:
        return self._v

    def set_value(self, v: int) -> None:
        v = max(0, min(255, int(v)))
        if v != self._v:
            self._v = v
            self.update()

    # ---- 几何 / 绘制 ----
    def _track(self) -> QRectF:
        return QRectF(3.0, 3.0, max(1.0, self.width() - 6.0), self.height() - 6.0)

    def _paint_fill(self, p: QPainter, track: QRectF) -> None:
        """子类往 track 里填渐变（调用前已经裁好圆角）。"""
        return None

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        track = self._track()
        path = QPainterPath()
        rad = track.height() * 0.5
        path.addRoundedRect(track, rad, rad)
        p.save()
        p.setClipPath(path)
        self._paint_fill(p, track)
        p.restore()
        # 收边
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(0, 0, 0, 40), 1.0))
        p.drawPath(path)
        # 滑块
        t = self._v / 255.0
        x = track.left() + t * track.width()
        knob = QRectF(x - 4.0, 1.0, 8.0, self.height() - 2.0)
        kp = QPainterPath()
        kp.addRoundedRect(knob, 4.0, 4.0)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 250))
        p.drawPath(kp)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(0, 0, 0, 55), 1.0))
        p.drawPath(kp)
        p.end()

    # ---- 交互 ----
    def _set_at(self, x: float) -> None:
        track = self._track()
        t = (x - track.left()) / max(1.0, track.width())
        v = int(round(max(0.0, min(1.0, t)) * 255))
        if v != self._v:
            self._v = v
            self.update()
            self.changed.emit(v)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._set_at(event.position().x())
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & Qt.LeftButton:
            self._set_at(event.position().x())
            event.accept()


class ValueBar(_GradientBar):
    """亮度条：左边是黑，右边是当前色相/饱和度的最亮色。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._h = 0
        self._s = 255

    def set_hue_sat(self, h: int, s: int) -> None:
        if (int(h), int(s)) != (self._h, self._s):
            self._h, self._s = int(h), int(s)
            self.update()

    def _paint_fill(self, p: QPainter, track: QRectF) -> None:
        g = QLinearGradient(track.topLeft(), track.topRight())
        g.setColorAt(0.0, QColor(0, 0, 0))
        g.setColorAt(1.0, QColor.fromHsv(self._h, self._s, 255))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.fillRect(track, g)


def _paint_checker(p: QPainter, rect: QRectF, cell: float = 5.0) -> None:
    """透明区背后的棋盘格（透明度条、色块预览共用）。"""
    p.fillRect(rect, QColor(255, 255, 255))
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(206, 211, 222))
    y = rect.top()
    row = 0
    while y < rect.bottom():
        x = rect.left() + (cell if row % 2 else 0.0)
        while x < rect.right():
            p.drawRect(QRectF(x, y, min(cell, rect.right() - x),
                              min(cell, rect.bottom() - y)))
            x += cell * 2.0
        y += cell
        row += 1


class AlphaBar(_GradientBar):
    """透明度条：调色盘里最常见那种——棋盘格打底，从左到右由全透明到实色。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._color = QColor("#60646c")

    def set_color(self, color: QColor) -> None:
        if color.isValid():
            c = QColor(color)
            c.setAlpha(255)
            if c != self._color:
                self._color = c
                self.update()

    def _paint_fill(self, p: QPainter, track: QRectF) -> None:
        _paint_checker(p, track)
        g = QLinearGradient(track.topLeft(), track.topRight())
        clear = QColor(self._color)
        clear.setAlpha(0)
        solid = QColor(self._color)
        solid.setAlpha(255)
        g.setColorAt(0.0, clear)
        g.setColorAt(1.0, solid)
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.fillRect(track, g)


class ColorPreview(QWidget):
    """当前颜色的小方块：先铺棋盘格再盖颜色，透明度一眼看得出来。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._color = QColor("#60646c")
        self.setFixedSize(56, 56)

    def set_color(self, color: QColor) -> None:
        if color.isValid():
            self._color = QColor(color)
            self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(1.0, 1.0, self.width() - 2.0, self.height() - 2.0)
        path = QPainterPath()
        path.addRoundedRect(rect, 12.0, 12.0)
        p.save()
        p.setClipPath(path)
        _paint_checker(p, rect, 6.0)
        p.fillRect(rect, self._color)
        p.restore()
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(0, 0, 0, 46), 1.0))
        p.drawPath(path)
        p.end()


class ColorPicker(_FrostDialog):
    """圆盘取色器（色相 + 饱和度 + 亮度 + 透明度）。

    ColorPicker.pick(...) 是同步调用：确定返回带 alpha 的 QColor，取消返回 None。
    """

    def __init__(self, color, alpha_percent: int = 100, dark: bool = False,
                 parent=None):
        flags = Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
        super().__init__(flags, 10.0, dark, parent)
        self.result_color = None
        src = QColor(color)
        if not src.isValid():
            src = QColor("#60646c")
        src.setAlpha(self._alpha_to_255(alpha_percent))
        self._src = src
        self.setFixedSize(352, 382)
        self._building = True

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 16)
        root.setSpacing(10)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title = QLabel("鼠标颜色")
        title.setObjectName("title")
        header.addWidget(title)
        header.addStretch(1)
        btn_close = QPushButton("✕")
        btn_close.setObjectName("close")
        btn_close.setFixedSize(24, 24)
        btn_close.clicked.connect(self.reject)
        header.addWidget(btn_close)
        root.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(14)
        self.wheel = ColorWheel()
        self.wheel.changed.connect(self._on_wheel)
        body.addWidget(self.wheel)
        side = QVBoxLayout()
        side.setSpacing(8)
        # 左右两栏高度差挺多（圆盘高、色块那栏矮），上下各留一个弹簧居中，
        # 不然色块和 HEX 顶在上面，下面空一大块
        side.addStretch(1)
        self.preview = ColorPreview()
        side.addWidget(self.preview)
        self.edit_hex = QLineEdit()
        self.edit_hex.setObjectName("hex")
        self.edit_hex.setMaxLength(7)
        self.edit_hex.setPlaceholderText("#RRGGBB")
        self.edit_hex.setFixedWidth(108)
        self.edit_hex.returnPressed.connect(self._apply_hex)
        self.edit_hex.editingFinished.connect(self._apply_hex)
        side.addWidget(self.edit_hex)
        side.addStretch(1)
        body.addLayout(side, 1)
        root.addLayout(body)

        # 亮度 + 透明度两条，同一套外观（左边标签对齐）
        self.bar = ValueBar()
        self.bar.changed.connect(self._on_value)
        root.addLayout(self._bar_row("亮度", self.bar))
        self.alpha_bar = AlphaBar()
        self.alpha_bar.changed.connect(self._on_alpha)
        self.lbl_alpha = QLabel("100%")
        self.lbl_alpha.setObjectName("pct")
        self.lbl_alpha.setFixedWidth(38)
        self.lbl_alpha.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        root.addLayout(self._bar_row("透明度", self.alpha_bar, self.lbl_alpha))

        footer = QHBoxLayout()
        footer.setSpacing(8)
        footer.addStretch(1)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)
        footer.addWidget(self.btn_cancel)
        self.btn_ok = QPushButton("确定")
        self.btn_ok.setObjectName("primary")
        self.btn_ok.setDefault(True)
        self.btn_ok.clicked.connect(self.accept)
        footer.addWidget(self.btn_ok)
        root.addLayout(footer)

        self._load(self._src)
        self._building = False

    # ---- 小工具 ----
    @staticmethod
    def _alpha_to_255(percent) -> int:
        try:
            p = max(0.0, min(100.0, float(percent)))
        except (TypeError, ValueError):
            p = 100.0
        return int(round(p * 255.0 / 100.0))

    @staticmethod
    def _255_to_percent(value: int) -> int:
        return int(round(max(0, min(255, int(value))) * 100.0 / 255.0))

    def _bar_row(self, text: str, bar, right=None) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel(text)
        lbl.setObjectName("tag")
        lbl.setFixedWidth(34)
        row.addWidget(lbl)
        row.addWidget(bar, 1)
        if right is None:
            # 没有数值的那一行留个空位，两条滑条右端才对齐
            pad = QLabel("")
            pad.setFixedWidth(38)
            row.addWidget(pad)
        else:
            row.addWidget(right)
        return row

    # ---- 状态 ----
    def _current(self) -> QColor:
        c = QColor.fromHsv(self.wheel.hue(), self.wheel.saturation(),
                           self.wheel.value())
        c.setAlpha(self.alpha_bar.value())
        return c

    def _view(self) -> None:
        """把预览、HEX、百分比刷成当前状态（不再回写滑条，避免循环）。"""
        c = self._current()
        self.preview.set_color(c)
        self.edit_hex.setText(c.name().upper())
        self.lbl_alpha.setText(f"{self._255_to_percent(c.alpha())}%")
        self.alpha_bar.set_color(c)

    def _load(self, color: QColor) -> None:
        """外部给一个颜色：圆盘、两条滑条、预览全部跟上。"""
        if not color.isValid():
            return
        self._building = True
        try:
            self.wheel.set_color(color)
            h, s, v, a = color.getHsv()
            self.bar.set_hue_sat(max(0, h), s)
            self.bar.set_value(v)
            self.alpha_bar.set_value(a)
            self._view()
        finally:
            self._building = False

    # ---- 事件 ----
    def _apply_hex(self) -> None:
        if self._building:
            return
        text = (self.edit_hex.text() or "").strip()
        if not text.startswith("#"):
            text = "#" + text
        color = QColor(text)
        if not color.isValid():
            self._view()
            return
        color.setAlpha(self.alpha_bar.value())
        self._load(color)

    def _on_wheel(self, _color: QColor) -> None:
        if self._building:
            return
        self._building = True
        try:
            self.bar.set_hue_sat(self.wheel.hue(), self.wheel.saturation())
            self.bar.set_value(self.wheel.value())
            self._view()
        finally:
            self._building = False

    def _on_value(self, v: int) -> None:
        if self._building:
            return
        self._building = True
        try:
            self.wheel.set_value(v)
            self._view()
        finally:
            self._building = False

    def _on_alpha(self, _v: int) -> None:
        if self._building:
            return
        self._building = True
        try:
            self._view()
        finally:
            self._building = False

    # ---- 结果 ----
    def accept(self) -> None:
        self.result_color = self._current()
        self._finish()

    @classmethod
    def pick(cls, color, alpha_percent: int = 100, dark: bool = False,
             parent=None):
        """同步弹一个取色盘：确定返回 QColor（带 alpha），取消返回 None。"""
        dlg = cls(color, alpha_percent, dark, parent)
        dlg.center_over(parent)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        try:
            dlg.edit_hex.clearFocus()
        except (RuntimeError, TypeError):
            pass
        loop = QEventLoop()
        dlg.closed.connect(loop.quit)
        loop.exec()
        try:
            dlg.deleteLater()
        except (RuntimeError, TypeError):
            pass
        return dlg.result_color


class KeyCapture(_FrostDialog):
    """按下组合键的窗口：给面板的快捷键页用。

    打开期间调用方要先把全局快捷键摘掉（hotkey.release()），否则按到旧组合键
    会当场触发面板开/关，自己把自己搞乱。
    """

    def __init__(self, dark: bool = False, parent=None):
        flags = Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
        super().__init__(flags, 10.0, dark, parent)
        self.result = None            # (mods 位掩码, vk) / None
        self._pending = None
        self.setFixedSize(316, 154)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 16)
        root.setSpacing(10)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title = QLabel("设置快捷键")
        title.setObjectName("title")
        header.addWidget(title)
        header.addStretch(1)
        btn_close = QPushButton("✕")
        btn_close.setObjectName("close")
        btn_close.setFixedSize(24, 24)
        btn_close.clicked.connect(self.reject)
        header.addWidget(btn_close)
        root.addLayout(header)

        self.lbl = QLabel("按下组合键…")
        self.lbl.setObjectName("keycap")
        self.lbl.setAlignment(Qt.AlignCenter)
        self.lbl.setFixedHeight(44)
        root.addWidget(self.lbl)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        footer.addStretch(1)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        footer.addWidget(btn_cancel)
        self.btn_ok = QPushButton("确定")
        self.btn_ok.setObjectName("primary")
        self.btn_ok.setEnabled(False)
        self.btn_ok.clicked.connect(self.accept)
        footer.addWidget(self.btn_ok)
        root.addLayout(footer)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.reject()
            return
        mods = 0
        m = event.modifiers()
        if m & Qt.ControlModifier:
            mods |= MOD_CONTROL
        if m & Qt.ShiftModifier:
            mods |= MOD_SHIFT
        if m & Qt.AltModifier:
            mods |= MOD_ALT
        vk = qt_to_vk(event.key())
        if vk is None:
            # 只按了修饰键（Ctrl / Shift 本身），等主键
            return
        self._pending = (mods, vk)
        self.lbl.setText(hotkey_text(mods, vk))
        self.btn_ok.setEnabled(True)

    def accept(self) -> None:
        self.result = self._pending
        self._finish()

    @classmethod
    def capture(cls, dark: bool = False, parent=None):
        """同步弹一个按键捕获窗：确定返回 (mods, vk)，取消返回 None。"""
        dlg = cls(dark, parent)
        dlg.center_over(parent)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        dlg.setFocus(Qt.OtherFocusReason)
        loop = QEventLoop()
        dlg.closed.connect(loop.quit)
        loop.exec()
        try:
            dlg.deleteLater()
        except (RuntimeError, TypeError):
            pass
        return dlg.result


class GlassInput(_FrostDialog):
    """单行输入（给「保存鼠标样式」命名用），外观和取色盘一致。"""

    def __init__(self, title: str, label: str, text: str = "",
                 dark: bool = False, parent=None):
        flags = Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
        super().__init__(flags, 10.0, dark, parent)
        self.result_text = None
        self.setFixedSize(320, 156)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 16)
        root.setSpacing(10)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        head = QLabel(title)
        head.setObjectName("title")
        header.addWidget(head)
        header.addStretch(1)
        btn_close = QPushButton("✕")
        btn_close.setObjectName("close")
        btn_close.setFixedSize(24, 24)
        btn_close.clicked.connect(self.reject)
        header.addWidget(btn_close)
        root.addLayout(header)

        if label:
            lbl = QLabel(label)
            lbl.setObjectName("hint")
            root.addWidget(lbl)

        self.edit = QLineEdit()
        self.edit.setObjectName("hex")
        self.edit.setText(str(text or ""))
        self.edit.returnPressed.connect(self.accept)
        root.addWidget(self.edit)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        footer.addStretch(1)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        footer.addWidget(btn_cancel)
        self.btn_ok = QPushButton("确定")
        self.btn_ok.setObjectName("primary")
        self.btn_ok.setDefault(True)
        self.btn_ok.clicked.connect(self.accept)
        footer.addWidget(self.btn_ok)
        root.addLayout(footer)

    def accept(self) -> None:
        self.result_text = (self.edit.text() or "").strip()
        self._finish()

    @classmethod
    def prompt(cls, title: str, label: str, text: str = "",
               dark: bool = False, parent=None):
        dlg = cls(title, label, text, dark, parent)
        dlg.center_over(parent)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        dlg.edit.setFocus()
        dlg.edit.selectAll()
        loop = QEventLoop()
        dlg.closed.connect(loop.quit)
        loop.exec()
        try:
            dlg.deleteLater()
        except (RuntimeError, TypeError):
            pass
        return dlg.result_text


class GlassConfirm(_FrostDialog):
    """是 / 否确认框（和取色盘同一套玻璃外观）。返回 True = 用户点了确认。"""

    def __init__(self, title: str, text: str, ok_text: str = "确定",
                 dark: bool = False, parent=None):
        flags = Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
        super().__init__(flags, 10.0, dark, parent)
        self.answer = False
        self.setFixedWidth(360)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 16)
        root.setSpacing(10)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        head = QLabel(title)
        head.setObjectName("title")
        header.addWidget(head)
        header.addStretch(1)
        btn_close = QPushButton("✕")
        btn_close.setObjectName("close")
        btn_close.setFixedSize(24, 24)
        btn_close.clicked.connect(self.reject)
        header.addWidget(btn_close)
        root.addLayout(header)

        body = QLabel(text)
        body.setObjectName("hint")
        body.setWordWrap(True)
        root.addWidget(body)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        footer.addStretch(1)
        btn_cancel = QPushButton(tr("btn.cancel"))
        btn_cancel.clicked.connect(self.reject)
        footer.addWidget(btn_cancel)
        self.btn_ok = QPushButton(ok_text)
        self.btn_ok.setObjectName("primary")
        self.btn_ok.setDefault(True)
        self.btn_ok.clicked.connect(self.accept)
        footer.addWidget(self.btn_ok)
        root.addLayout(footer)

    def accept(self) -> None:
        self.answer = True
        self._finish()

    @classmethod
    def ask(cls, title: str, text: str, ok_text: str = "确定",
            dark: bool = False, parent=None) -> bool:
        dlg = cls(title, text, ok_text, dark, parent)
        dlg.adjustSize()
        dlg.center_over(parent)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        dlg.btn_ok.setFocus()
        loop = QEventLoop()
        dlg.closed.connect(loop.quit)
        loop.exec()
        answer = dlg.answer
        try:
            dlg.deleteLater()
        except (RuntimeError, TypeError):
            pass
        return answer


_QSS = """
QLabel { color: #2A3242; font-size: 12px; }
QLabel#title { font-size: 15px; font-weight: 600; color: #141A24; }
QLabel#hint { font-size: 10px; color: rgba(66, 82, 106, 150); }
QLabel#tag { font-size: 11px; color: rgba(52, 68, 94, 200); }
QLabel#pct { font-size: 11px; color: rgba(52, 68, 94, 170); }
QLabel#keycap {
    background: rgba(255, 255, 255, 170);
    border-radius: 9px;
    color: #1B2330;
    font-size: 14px;
    font-weight: 600;
}

QPushButton {
    background: rgba(255, 255, 255, 120);
    border: none;
    border-radius: 10px;
    padding: 7px 16px;
    color: #2A3242;
    font-size: 12px;
}
QPushButton:hover { background: rgba(255, 255, 255, 190); }
QPushButton:pressed { background: rgba(214, 228, 250, 215); }
QPushButton#primary { background: #5B8DEF; color: white; }
QPushButton#primary:hover { background: #6D9AF2; }
QPushButton#primary:pressed { background: #4E7CDB; }
QPushButton#close {
    background: transparent; border: none; border-radius: 12px;
    color: rgba(40, 56, 80, 150); font-size: 13px; padding: 0px;
}
QPushButton#close:hover { background: rgba(240, 90, 90, 170); color: white; }

QLineEdit {
    background: rgba(255, 255, 255, 150);
    border: none;
    border-radius: 9px;
    padding: 7px 10px;
    color: #1B2330;
    font-size: 12px;
    selection-background-color: rgba(110, 160, 240, 140);
}
QLineEdit:focus { background: rgba(255, 255, 255, 205); }
"""

_QSS_DARK = """
QLabel { color: #E3E8F4; font-size: 12px; }
QLabel#title { font-size: 15px; font-weight: 600; color: #F5F8FF; }
QLabel#hint { font-size: 10px; color: rgba(186, 196, 218, 140); }
QLabel#tag { font-size: 11px; color: rgba(214, 224, 244, 210); }
QLabel#pct { font-size: 11px; color: rgba(206, 216, 240, 170); }
QLabel#keycap {
    background: rgba(255, 255, 255, 22);
    border-radius: 9px;
    color: #EDF1FA;
    font-size: 14px;
    font-weight: 600;
}

QPushButton {
    background: rgba(255, 255, 255, 18);
    border: none;
    border-radius: 10px;
    padding: 7px 16px;
    color: #E3E8F4;
    font-size: 12px;
}
QPushButton:hover { background: rgba(255, 255, 255, 34); }
QPushButton:pressed { background: rgba(150, 178, 246, 60); }
QPushButton#primary { background: rgba(110, 140, 240, 235); color: white; }
QPushButton#primary:hover { background: rgba(126, 156, 250, 250); }
QPushButton#primary:pressed { background: rgba(96, 126, 226, 235); }
QPushButton#close {
    background: transparent; border: none; border-radius: 12px;
    color: rgba(210, 220, 240, 175); font-size: 13px; padding: 0px;
}
QPushButton#close:hover { background: rgba(240, 90, 90, 180); color: white; }

QLineEdit {
    background: rgba(255, 255, 255, 16);
    border: none;
    border-radius: 9px;
    padding: 7px 10px;
    color: #EDF1FA;
    font-size: 12px;
    selection-background-color: rgba(126, 156, 246, 140);
}
QLineEdit:focus { background: rgba(255, 255, 255, 28); }
"""
