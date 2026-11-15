"""面板用的几个小控件：玻璃下拉、色板、细滑条。

Qt 自带的 QComboBox 是原生箭头 + 系统边框，和这套玻璃面板放在一起很突兀，
所以下拉改成一个按钮 + 玻璃菜单；颜色则用一排色块（点一下就换），比再弹一个
系统取色器快，也不会被面板挡住。
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QSlider, QWidget

from .glass import GlassMenu
from .ui import u, uf

# 一排常用色：先灰阶（默认那个灰在前），再几个饱和色，最后一格调出系统取色器
PALETTE = (
    "#60646c", "#8a8f98", "#b9bec7", "#4a4f58", "#2b2f36", "#111318",
    "#e5484d", "#f76b15", "#f5c518", "#30a46c", "#0091ff", "#6e56cf",
)


class GlassCombo(QPushButton):
    """值下拉：按钮显示当前项，点开是玻璃菜单（和收纳盒菜单同一套）。"""

    changed = Signal(object)

    def __init__(self, items=None, parent=None, light: bool = False):
        super().__init__(parent)
        self._items = list(items or [])
        self._index = 0
        self._light = bool(light)
        self.setMinimumHeight(u(30))
        self.clicked.connect(self._open)
        self._refresh()

    def set_light(self, light: bool) -> None:
        """跟着面板深浅色走：浅色面板上不要弹一块深灰菜单。"""
        self._light = bool(light)

    # ---- 数据 ----
    def set_items(self, items, index: int = 0) -> None:
        self._items = list(items)
        self._index = max(0, min(index, len(self._items) - 1))
        self._refresh()

    def set_current_data(self, value) -> None:
        for i, (_label, v) in enumerate(self._items):
            if v == value:
                self._index = i
                self._refresh()
                return

    def current_data(self):
        if 0 <= self._index < len(self._items):
            return self._items[self._index][1]
        return None

    def _refresh(self) -> None:
        label = self._items[self._index][0] if self._items else ""
        self.setText(f"{label}    ▾")

    def _open(self) -> None:
        if not self._items:
            return
        menu = GlassMenu(self, light=self._light)
        menu.prepare_backdrop()
        acts = []
        for i, (label, _value) in enumerate(self._items):
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(i == self._index)
            acts.append((act, i))
        chosen = menu.exec(QCursor.pos())
        for act, i in acts:
            if chosen is act:
                self._index = i
                self._refresh()
                self.changed.emit(self._items[i][1])
                return


class ColorSwatches(QWidget):
    """一排色块（可多行）。点一下发 picked(QColor)。

    `cell` / `gap` / `cols` 可以调：面板里那些自定义外观的色板要小一号，
    不然一排占掉半页，滑条被挤到贴边。
    """

    picked = Signal(QColor)

    COLS = 6
    CELL = u(30)
    GAP = u(6)

    def __init__(self, colors=None, parent=None, cell=None, gap=None, cols=None):
        super().__init__(parent)
        if cell:
            self.CELL = u(int(cell))
        if gap is not None:
            self.GAP = u(int(gap))
        if cols:
            self.COLS = int(cols)
        self._colors = [QColor(c) for c in (colors or PALETTE)]
        self._current = self._colors[0] if self._colors else QColor("#60646c")
        rows = (len(self._colors) + self.COLS - 1) // self.COLS
        self.setFixedHeight(rows * self.CELL + (rows - 1) * self.GAP)

    def set_current(self, color: QColor) -> None:
        c = QColor(color)
        if c.isValid():
            self._current = c
            self._current = self._closest(c)
            self.update()

    def _closest(self, color: QColor) -> QColor:
        """不在色板里就显示最接近的那个（选中圈画在它上面）。"""
        best, dist = None, 10 ** 9
        for c in self._colors:
            d = (c.red() - color.red()) ** 2 + (c.green() - color.green()) ** 2 \
                + (c.blue() - color.blue()) ** 2
            if d < dist:
                best, dist = c, d
        return best or color

    def _cell_rect(self, index: int) -> QRectF:
        col = index % self.COLS
        row = index // self.COLS
        return QRectF(col * (self.CELL + self.GAP),
                      row * (self.CELL + self.GAP),
                      self.CELL, self.CELL)

    def sizeHint(self) -> QSize:
        rows = (len(self._colors) + self.COLS - 1) // self.COLS
        return QSize(self.COLS * (self.CELL + self.GAP) - self.GAP,
                     rows * self.CELL + max(0, rows - 1) * self.GAP)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        for i, color in enumerate(self._colors):
            r = self._cell_rect(i)
            rr = r.adjusted(uf(2.0), uf(2.0), -uf(2.0), -uf(2.0))
            path = QPainterPath()
            path.addRoundedRect(rr, uf(9.0), uf(9.0))
            p.setPen(Qt.NoPen)
            p.setBrush(color)
            p.drawPath(path)
            if color == self._current:
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(QColor(255, 255, 255, 230), uf(2.0)))
                p.drawPath(path)
        p.end()

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            return
        pos = event.position()
        for i, color in enumerate(self._colors):
            if self._cell_rect(i).contains(pos):
                self._current = color
                self.update()
                self.picked.emit(QColor(color))
                return


class CleanSlider(QSlider):
    """细轨道 + 圆头的滑条（样式在 QSS 里，这里只统一点击行为）。"""

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.setRange(0, 100)
        self.setSingleStep(1)
        self.setPageStep(5)
        self.setFixedHeight(u(22))

    def mousePressEvent(self, event) -> None:
        # 点轨道直接跳到那个位置，不用先拖；滚轮**故意不接**（见下）
        if event.button() == Qt.LeftButton:
            value = self._value_at(event.position().x())
            self.setValue(value)
        super().mousePressEvent(event)

    def wheelEvent(self, event) -> None:
        """滚轮交给外层面板去滚动，别拿来改数值。

        QSlider 默认滚轮一格就改一步——面板里滑条一多，往下滚页面时鼠标正好
        从滑条上过，数值就被悄悄拖走了（用户报的「很容易误触」）。这里直接忽略，
        事件会往上冒给 QScrollArea。
        """
        event.ignore()

    def _value_at(self, x: float) -> int:
        w = max(1, self.width())
        ratio = max(0.0, min(1.0, x / w))
        return int(round(self.minimum() + ratio * (self.maximum() - self.minimum())))


class GlassCheck(QWidget):
    """玻璃风复选框：圆角小方框 + 手画的对勾。

    Qt 自带 QCheckBox 的选中态只能整块填色（QSS 只能换底色），和这套自绘玻璃
    放一起像贴上去的色块；这里自己画框和对勾，选中时框变蓝、里面打勾。
    """

    toggled = Signal(bool)

    BOX = 17.0          # 框的边长（基准值，会乘 UI 缩放）

    def __init__(self, text: str = "", parent=None, dark: bool = False):
        super().__init__(parent)
        self._text = str(text)
        self._checked = False
        self._hover = False
        self._dark = bool(dark)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        font = self.font()
        font.setPixelSize(u(13))
        self.setFont(font)
        self.setFixedHeight(u(24))

    # ---- 状态 ----
    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, on: bool) -> None:
        on = bool(on)
        if on == self._checked:
            return
        self._checked = on
        self.update()
        self.toggled.emit(on)

    def setText(self, text: str) -> None:
        self._text = str(text)
        self.updateGeometry()
        self.update()

    def text(self) -> str:
        return self._text

    def set_dark(self, dark: bool) -> None:
        self._dark = bool(dark)
        self.update()

    def toggle(self) -> None:
        self.setChecked(not self._checked)

    # ---- 尺寸 ----
    def _box(self) -> float:
        return self.BOX * u(1)

    def sizeHint(self) -> QSize:
        box = self._box()
        fm = self.fontMetrics()
        return QSize(int(box + u(9) + fm.horizontalAdvance(self._text)) + u(2),
                     int(max(box, fm.height()) + u(4)))

    # ---- 交互 ----
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.toggle()
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self.toggle()
            event.accept()
            return
        super().keyPressEvent(event)

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    # ---- 绘制 ----
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        box = self._box()
        r = QRectF(0.0, (self.height() - box) / 2.0, box, box)
        radius = box * 0.34
        if self._dark:
            fill = QColor(255, 255, 255, 40 if self._hover else 22)
            border = QColor(255, 255, 255, 120 if not self._checked else 0)
            accent = QColor("#7FA4F5")
            text_color = QColor(0xE3, 0xE8, 0xF4)
        else:
            fill = QColor(255, 255, 255, 220 if self._hover else 178)
            border = QColor(30, 50, 90, 60 if not self._checked else 0)
            accent = QColor("#5B8DEF")
            text_color = QColor(0x2A, 0x32, 0x42)
        p.setBrush(fill)
        p.setPen(QPen(border, 1.0) if border.alpha() else Qt.NoPen)
        p.drawRoundedRect(r, radius, radius)
        if self._checked:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(accent, box * 0.12, Qt.SolidLine, Qt.RoundCap,
                          Qt.RoundJoin))
            path = QPainterPath()
            path.moveTo(r.left() + box * 0.24, r.top() + box * 0.52)
            path.lineTo(r.left() + box * 0.43, r.top() + box * 0.70)
            path.lineTo(r.left() + box * 0.77, r.top() + box * 0.31)
            p.drawPath(path)
        if self._text:
            p.setPen(text_color)
            p.setFont(self.font())
            tr = QRectF(box + u(9), 0.0, max(0.0, self.width() - box - u(9)),
                        float(self.height()))
            p.drawText(tr, int(Qt.AlignLeft | Qt.AlignVCenter), self._text)
        p.end()


class ThemeOption(QWidget):
    """「浅色 / 深色 / 跟随系统」里的一张卡片（DSH 那种：图标 + 文字）。"""

    clicked = Signal(str)

    def __init__(self, code: str, label: str, dark: bool = False, parent=None):
        super().__init__(parent)
        self.code = code
        self._label = label
        self._dark = bool(dark)
        self._checked = False
        self._hover = False
        self.setFixedSize(u(104), u(78))
        self.setMouseTracking(True)

    def set_dark(self, dark: bool) -> None:
        self._dark = bool(dark)
        self.update()

    def set_checked(self, on: bool) -> None:
        if bool(on) != self._checked:
            self._checked = bool(on)
            self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.code)
            event.accept()
            return
        super().mousePressEvent(event)

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def _draw_glyph(self, p: QPainter, r: QRectF, color: QColor) -> None:
        """太阳 / 月亮 / 显示器：都用几何图形画，免得依赖字体里有没有这个字。"""
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(color, max(1.4, r.width() * 0.075),
                      Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        cx, cy = r.center().x(), r.center().y()
        rad = r.width() * 0.20
        if self.code == "light":
            p.setBrush(color)
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(cx, cy), rad, rad)
            p.setPen(QPen(color, max(1.4, r.width() * 0.075),
                          Qt.SolidLine, Qt.RoundCap))
            import math
            for i in range(8):
                ang = math.pi * i / 4.0
                x0 = cx + math.cos(ang) * rad * 1.55
                y0 = cy + math.sin(ang) * rad * 1.55
                x1 = cx + math.cos(ang) * rad * 2.15
                y1 = cy + math.sin(ang) * rad * 2.15
                p.drawLine(QPointF(x0, y0), QPointF(x1, y1))
        elif self.code == "dark":
            path = QPainterPath()
            path.addEllipse(QPointF(cx, cy + rad * 0.18), rad * 1.26, rad * 1.26)
            cut = QPainterPath()
            cut.addEllipse(QPointF(cx + rad * 0.86, cy - rad * 0.30),
                           rad * 1.16, rad * 1.16)
            p.setBrush(color)
            p.setPen(Qt.NoPen)
            p.drawPath(path.subtracted(cut))
        else:   # auto / 跟随系统
            body = QRectF(cx - rad * 1.5, cy - rad * 1.15, rad * 3.0, rad * 2.1)
            p.drawRoundedRect(body, rad * 0.28, rad * 0.28)
            p.drawLine(QPointF(cx, body.bottom()),
                       QPointF(cx, body.bottom() + rad * 0.55))
            p.drawLine(QPointF(cx - rad * 0.58, body.bottom() + rad * 0.55),
                       QPointF(cx + rad * 0.58, body.bottom() + rad * 0.55))

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        outer = QRectF(1.0, 1.0, self.width() - 2.0, self.height() - 2.0)
        radius = 10.0
        if self._dark:
            accent = QColor("#7FA4F5")
            text_color = QColor(0xE9, 0xEE, 0xFA)
            fill = QColor(255, 255, 255, 34 if self._checked
                          else (22 if self._hover else 10))
            border = accent if self._checked else QColor(255, 255, 255, 46)
        else:
            accent = QColor("#5B8DEF")
            text_color = QColor(0x2A, 0x32, 0x42)
            fill = QColor(255, 255, 255, 205 if self._checked
                          else (165 if self._hover else 118))
            border = accent if self._checked else QColor(40, 62, 100, 46)
        p.setBrush(fill)
        p.setPen(QPen(border, 1.6 if self._checked else 1.0))
        p.drawRoundedRect(outer, radius, radius)
        glyph = QRectF(outer.width() * 0.5 - u(20), u(13), u(40), u(30))
        self._draw_glyph(p, glyph, accent if self._checked else text_color)
        p.setPen(text_color)
        f = self.font()
        f.setPixelSize(max(10, int(u(12))))
        p.setFont(f)
        p.drawText(QRectF(0.0, u(44), outer.width(), u(24)),
                   int(Qt.AlignHCenter | Qt.AlignVCenter), self._label)
        p.end()


class ThemePicker(QWidget):
    """三张卡片选面板主题：浅色 / 深色 / 跟随系统。"""

    changed = Signal(str)

    def __init__(self, items, current: str = "light", dark: bool = False,
                 parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(u(10))
        self._options: list[ThemeOption] = []
        for code, label in items:
            opt = ThemeOption(code, label, dark, self)
            opt.set_checked(code == current)
            opt.clicked.connect(self._pick)
            row.addWidget(opt)
            self._options.append(opt)
        row.addStretch(1)
        self._current = current

    def _pick(self, code: str) -> None:
        self.set_current(code)
        self.changed.emit(code)

    def set_current(self, code: str) -> None:
        self._current = code
        for opt in self._options:
            opt.set_checked(opt.code == code)

    def current(self) -> str:
        return self._current

    def set_dark(self, dark: bool) -> None:
        for opt in self._options:
            opt.set_dark(dark)
