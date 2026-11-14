"""待办详情、纪念日列表、添加/修改纪念日：卡片上点开的三个小窗。

和面板、天气详情同一套外观：玻璃底（paint_frost）+ 圆角 + 深浅色跟随面板。
数据都走 agenda.Agenda，改完立刻落盘并通知所有卡片重画。
"""
from __future__ import annotations

from datetime import date, timedelta

from PySide6.QtCore import QEventLoop, QPointF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from . import agenda as ag
from .controls import GlassCombo
from .picker import _FrostDialog
from .ui import u

DUE_CHOICES = (("无日期", ""), ("今天", "today"), ("明天", "tomorrow"), ("下周", "nextweek"))


def _due_value(key: str) -> str:
    today = date.today()
    if key == "today":
        return today.isoformat()
    if key == "tomorrow":
        return (today + timedelta(days=1)).isoformat()
    if key == "nextweek":
        return (today + timedelta(days=7)).isoformat()
    return ""


class _CheckDot(QWidget):
    """左边那个勾选圆圈（和卡片上画的是同一套）。"""

    toggled = Signal()

    def __init__(self, done: bool = False, parent=None):
        super().__init__(parent)
        self._done = bool(done)
        self._hover = False
        self.setFixedSize(u(20), u(20))
        self.setMouseTracking(True)

    def sizeHint(self) -> QSize:
        return QSize(u(20), u(20))

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._done = not self._done
            self.update()
            self.toggled.emit()
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

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        c = QPointF(self.width() / 2.0, self.height() / 2.0)
        r = min(c.x(), c.y()) - u(1)
        if self._done:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(91, 141, 239, 240))
            p.drawEllipse(c, r, r)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(255, 255, 255, 240), u(1.4),
                          Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            path = QPainterPath()
            path.moveTo(c.x() - r * 0.45, c.y() + r * 0.02)
            path.lineTo(c.x() - r * 0.10, c.y() + r * 0.38)
            path.lineTo(c.x() + r * 0.48, c.y() - r * 0.36)
            p.drawPath(path)
        else:
            p.setBrush(QColor(255, 255, 255, 200 if self._hover else 150))
            p.setPen(QPen(QColor(110, 126, 150, 190), max(1.0, u(1.1))))
            p.drawEllipse(c, r, r)
        p.end()


class _ClickLabel(QLabel):
    clicked = Signal()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


# ---------------------------------------------------------------- 待办
class TodoDialog(_FrostDialog):
    """所有待办的管理窗：勾选、改文字、删除、加新的。"""

    def __init__(self, agenda: ag.Agenda, dark: bool = False, parent=None):
        flags = Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
        super().__init__(flags, 10.0, dark, parent)
        self.agenda_ref = agenda
        self.setFixedSize(u(470), u(480))
        self.setStyleSheet(_QSS_DARK if self._dark else _QSS)
        self._rows: list[QWidget] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(u(18), u(14), u(18), u(16))
        root.setSpacing(u(9))

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title = QLabel("待办")
        title.setObjectName("title")
        header.addWidget(title)
        self.lbl_count = QLabel("")
        self.lbl_count.setObjectName("dim")
        header.addWidget(self.lbl_count)
        header.addStretch(1)
        self.btn_clear = QPushButton("清除已完成")
        self.btn_clear.setObjectName("ghost")
        self.btn_clear.clicked.connect(agenda.clear_done)
        header.addWidget(self.btn_clear)
        btn_close = QPushButton("✕")
        btn_close.setObjectName("close")
        btn_close.setFixedSize(u(24), u(24))
        btn_close.clicked.connect(self.reject)
        header.addWidget(btn_close)
        root.addLayout(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)
        holder = QWidget()
        holder.setAutoFillBackground(False)
        self._box = QVBoxLayout(holder)
        self._box.setContentsMargins(0, 0, 0, 0)
        self._box.setSpacing(u(5))
        scroll.setWidget(holder)
        root.addWidget(scroll, 1)

        add_row = QHBoxLayout()
        add_row.setContentsMargins(0, 0, 0, 0)
        add_row.setSpacing(u(8))
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("写一条待办")
        self.edit.returnPressed.connect(self._add)
        add_row.addWidget(self.edit, 1)
        self.cmb_due = GlassCombo(light=not self._dark)
        self.cmb_due.set_items([(name, key) for name, key in DUE_CHOICES], 0)
        add_row.addWidget(self.cmb_due)
        btn_add = QPushButton("添加")
        btn_add.setObjectName("primary")
        btn_add.clicked.connect(self._add)
        add_row.addWidget(btn_add)
        root.addLayout(add_row)

        agenda.changed.connect(self._rebuild)
        self.closed.connect(lambda: _disconnect(agenda, self._rebuild))
        self._rebuild()

    # ---- 列表 ----
    def _clear(self) -> None:
        while self._box.count():
            item = self._box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._rows.clear()

    def _rebuild(self) -> None:
        self._clear()
        items = self.agenda_ref.ordered_todos(True)
        opening = sum(1 for t in items if not t.done)
        self.lbl_count.setText(f"还有 {opening} 项" if opening else "全部完成")
        self.btn_clear.setVisible(any(t.done for t in items))
        if not items:
            empty = QLabel("还没有待办")
            empty.setObjectName("dim")
            empty.setAlignment(Qt.AlignCenter)
            empty.setMinimumHeight(u(80))
            self._box.addWidget(empty)
        for todo in items:
            row = self._row(todo)
            self._box.addWidget(row)
            self._rows.append(row)
        self._box.addStretch(1)

    def _row(self, todo) -> QWidget:
        row = QFrame()
        row.setObjectName("item")
        line = QHBoxLayout(row)
        line.setContentsMargins(u(10), u(6), u(8), u(6))
        line.setSpacing(u(9))

        dot = _CheckDot(todo.done)
        dot.toggled.connect(lambda tid=todo.id: self.agenda_ref.toggle_todo(tid))
        line.addWidget(dot)

        text = _ClickLabel(todo.text)
        text.setObjectName("itemtext")
        text.setWordWrap(False)
        if todo.done:
            font = text.font()
            font.setStrikeOut(True)
            text.setFont(font)
            text.setObjectName("itemdone")
        text.clicked.connect(lambda tid=todo.id: self._edit(tid))
        line.addWidget(text, 1)

        unit = todo.due_label()
        if unit:
            due = QLabel(unit)
            due.setObjectName("duebad" if todo.overdue() else "due")
            line.addWidget(due)

        dele = QPushButton("✕")
        dele.setObjectName("close")
        dele.setFixedSize(u(22), u(22))
        dele.clicked.connect(lambda _=False, tid=todo.id: self.agenda_ref.remove_todo(tid))
        line.addWidget(dele)
        return row

    # ---- 操作 ----
    def _add(self) -> None:
        text = (self.edit.text() or "").strip()
        if not text:
            return
        self.agenda_ref.add_todo(text, _due_value(self.cmb_due.current_data() or ""))
        self.edit.clear()

    def _edit(self, todo_id: str) -> None:
        todo = self.agenda_ref.todo(todo_id)
        if todo is None:
            return
        from .picker import GlassInput
        text = GlassInput.prompt("修改待办", "", todo.text, self._dark, self)
        if text and text != todo.text:
            self.agenda_ref.update_todo(todo_id, text=text)

    @classmethod
    def show_for(cls, agenda: ag.Agenda, dark: bool = False, parent=None) -> None:
        dlg = cls(agenda, dark, parent)
        dlg.center_over(parent)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        dlg.edit.setFocus()
        _run(dlg)


# ---------------------------------------------------------------- 纪念日
class CountdownDialog(_FrostDialog):
    """所有纪念日的列表：按「还有几天」排，可以加、删、改。"""

    def __init__(self, agenda: ag.Agenda, dark: bool = False, parent=None):
        flags = Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
        super().__init__(flags, 10.0, dark, parent)
        self.agenda_ref = agenda
        self.setFixedSize(u(430), u(460))
        self.setStyleSheet(_QSS_DARK if self._dark else _QSS)

        root = QVBoxLayout(self)
        root.setContentsMargins(u(18), u(14), u(18), u(16))
        root.setSpacing(u(9))

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title = QLabel("纪念日")
        title.setObjectName("title")
        header.addWidget(title)
        header.addStretch(1)
        btn_close = QPushButton("✕")
        btn_close.setObjectName("close")
        btn_close.setFixedSize(u(24), u(24))
        btn_close.clicked.connect(self.reject)
        header.addWidget(btn_close)
        root.addLayout(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)
        holder = QWidget()
        holder.setAutoFillBackground(False)
        self._box = QVBoxLayout(holder)
        self._box.setContentsMargins(0, 0, 0, 0)
        self._box.setSpacing(u(5))
        scroll.setWidget(holder)
        root.addWidget(scroll, 1)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.addStretch(1)
        btn_add = QPushButton("＋ 添加纪念日")
        btn_add.setObjectName("primary")
        btn_add.clicked.connect(self._add)
        footer.addWidget(btn_add)
        root.addLayout(footer)

        agenda.changed.connect(self._rebuild)
        self.closed.connect(lambda: _disconnect(agenda, self._rebuild))
        self._rebuild()

    def _clear(self) -> None:
        while self._box.count():
            item = self._box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _rebuild(self) -> None:
        self._clear()
        rows = self.agenda_ref.all_events()
        if not rows:
            empty = QLabel("还没有纪念日")
            empty.setObjectName("dim")
            empty.setAlignment(Qt.AlignCenter)
            empty.setMinimumHeight(u(90))
            self._box.addWidget(empty)
        for event, when, days in rows:
            self._box.addWidget(self._row(event, when, days))
        self._box.addStretch(1)

    def _row(self, event, when, days) -> QWidget:
        row = QFrame()
        row.setObjectName("item")
        line = QHBoxLayout(row)
        line.setContentsMargins(u(12), u(7), u(8), u(7))
        line.setSpacing(u(10))

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(u(1))
        name = _ClickLabel(event.title)
        name.setObjectName("itemtext")
        name.clicked.connect(lambda eid=event.id: self._edit(eid))
        col.addWidget(name)
        if when is None:
            info_text = "已经过去 · " + event.source_text()
        else:
            info_text = ag.solar_text(when)
            lunar = ag.lunar_of(when)
            if lunar:
                info_text += " · " + lunar
        info = QLabel(info_text)
        info.setObjectName("dim")
        col.addWidget(info)
        line.addLayout(col, 1)

        if when is None:
            left = QLabel("已过")
            left.setObjectName("dim")
        else:
            left = QLabel("今天" if days <= 0 else f"{days} 天")
            left.setObjectName("days")
        line.addWidget(left)

        dele = QPushButton("✕")
        dele.setObjectName("close")
        dele.setFixedSize(u(22), u(22))
        dele.clicked.connect(lambda _=False, eid=event.id:
                             self.agenda_ref.remove_event(eid))
        line.addWidget(dele)
        return row

    def _add(self) -> None:
        EventDialog.edit(self.agenda_ref, None, self._dark, self)

    def _edit(self, event_id: str) -> None:
        event = self.agenda_ref.event(event_id)
        if event is not None:
            EventDialog.edit(self.agenda_ref, event, self._dark, self)

    @classmethod
    def show_for(cls, agenda: ag.Agenda, dark: bool = False, parent=None) -> None:
        dlg = cls(agenda, dark, parent)
        dlg.center_over(parent)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        _run(dlg)


class EventDialog(_FrostDialog):
    """加 / 改一个纪念日。"""

    MODES = (("每年", "yearly"), ("一次性", "once"), ("每年（农历）", "lunar"))

    def __init__(self, agenda: ag.Agenda, event=None, dark: bool = False,
                 parent=None):
        flags = Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
        super().__init__(flags, 10.0, dark, parent)
        self.agenda_ref = agenda
        self.event = event
        self.saved = False
        self.setFixedSize(u(400), u(260))
        self.setStyleSheet(_QSS_DARK if self._dark else _QSS)

        root = QVBoxLayout(self)
        root.setContentsMargins(u(18), u(14), u(18), u(16))
        root.setSpacing(u(10))

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title = QLabel("修改纪念日" if event is not None else "添加纪念日")
        title.setObjectName("title")
        header.addWidget(title)
        header.addStretch(1)
        btn_close = QPushButton("✕")
        btn_close.setObjectName("close")
        btn_close.setFixedSize(u(24), u(24))
        btn_close.clicked.connect(self.reject)
        header.addWidget(btn_close)
        root.addLayout(header)

        self.edit_name = QLineEdit()
        self.edit_name.setPlaceholderText("名称，比如 生日 / 春节")
        self.edit_name.setText(event.title if event is not None else "")
        root.addWidget(self._row("名称", self.edit_name))

        self.cmb_mode = GlassCombo(light=not self._dark)
        modes = [(name, key) for name, key in self.MODES]
        initial = 0
        if event is not None:
            keys = [key for _name, key in modes]
            initial = keys.index(event.mode) if event.mode in keys else 0
        self.cmb_mode.set_items(modes, initial)
        self.cmb_mode.changed.connect(lambda _v: self._sync_mode())
        root.addWidget(self._row("重复", self.cmb_mode))

        self.edit_date = QLineEdit()
        self.edit_date.setText(event.date if event is not None else "")
        self.edit_date.textChanged.connect(self._preview)
        root.addWidget(self._row("日期", self.edit_date))

        self.lbl_preview = QLabel("")
        self.lbl_preview.setObjectName("dim")
        self.lbl_preview.setMinimumHeight(u(18))
        root.addWidget(self.lbl_preview)
        root.addStretch(1)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.addStretch(1)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        footer.addWidget(btn_cancel)
        btn_save = QPushButton("保存")
        btn_save.setObjectName("primary")
        btn_save.setDefault(True)
        btn_save.clicked.connect(self._save)
        footer.addWidget(btn_save)
        root.addLayout(footer)

        self._sync_mode()

    def _row(self, label: str, widget) -> QWidget:
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(u(10))
        tag = QLabel(label)
        tag.setObjectName("tag")
        tag.setFixedWidth(u(46))
        line.addWidget(tag)
        line.addWidget(widget, 1)
        return row

    def _mode(self) -> str:
        return str(self.cmb_mode.current_data() or "yearly")

    def _sync_mode(self) -> None:
        lunar = self._mode() == "lunar"
        self.edit_date.setPlaceholderText(
            "农历的 月-日，比如 08-15（八月十五）" if lunar else "2026-12-20")
        self._preview()

    def _parse(self):
        text = (self.edit_date.text() or "").strip()
        if self._mode() == "lunar":
            return ag.parse_month_day(text)
        return ag.parse_date(text)

    def _preview(self) -> None:
        mode = self._mode()
        text = (self.edit_date.text() or "").strip()
        if not text:
            self.lbl_preview.setText("")
            return
        if mode == "lunar":
            md = ag.parse_month_day(text)
            if md is None:
                self._bad("农历日期填「月-日」，比如 08-15")
                return
            month, day = md
            nxt = ag.Event(title="_", mode="lunar",
                           date=f"{month:02d}-{day:02d}").next_date()
            if nxt is None:
                self._bad("这个农历日期今年没有")
                return
            self.lbl_preview.setStyleSheet("")
            self.lbl_preview.setText(
                f"下一次：{ag.solar_text(nxt)} · {ag.lunar_of(nxt)}")
            return
        day = ag.parse_date(text)
        if day is None:
            self._bad("日期填 2026-12-20 这种格式")
            return
        self.lbl_preview.setStyleSheet("")
        line = ag.solar_text(day)
        lunar = ag.lunar_of(day)
        if lunar:
            line += f" · {lunar}"
        self.lbl_preview.setText(line)

    def _bad(self, text: str) -> None:
        self.lbl_preview.setText(text)
        self.lbl_preview.setStyleSheet(
            "color: #C2554F;" if not self._dark else "color: #F0A6A0;")

    def _save(self) -> None:
        title = (self.edit_name.text() or "").strip()
        if not title:
            self._bad("先给纪念日起个名字")
            self.edit_name.setFocus()
            return
        parsed = self._parse()
        if parsed is None:
            self._bad("日期还没填对")
            self.edit_date.setFocus()
            return
        mode = self._mode()
        if mode == "lunar":
            month, day = parsed
            when = f"{month:02d}-{day:02d}"
        else:
            when = parsed.isoformat()
        if self.event is None:
            self.agenda_ref.add_event(title, when, mode)
        else:
            self.agenda_ref.update_event(self.event.id, title=title,
                                         mode=mode, date=when)
        self.saved = True
        self.accept()

    def accept(self) -> None:
        self._finish()

    @classmethod
    def edit(cls, agenda: ag.Agenda, event=None, dark: bool = False,
             parent=None) -> bool:
        dlg = cls(agenda, event, dark, parent)
        dlg.center_over(parent)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        dlg.edit_name.setFocus()
        dlg.edit_name.selectAll()
        _run(dlg)
        return bool(dlg.saved)


def _run(dlg) -> None:
    loop = QEventLoop()
    dlg.closed.connect(loop.quit)
    loop.exec()
    try:
        dlg.deleteLater()
    except (RuntimeError, TypeError):
        pass


def _disconnect(agenda, slot) -> None:
    try:
        agenda.changed.disconnect(slot)
    except (RuntimeError, TypeError):
        pass


_QSS = """
QLabel { color: #2A3242; font-size: 12px; }
QLabel#title { font-size: 16px; font-weight: 600; color: #141A24; }
QLabel#tag { font-size: 12px; color: rgba(52, 68, 94, 200); }
QLabel#dim { font-size: 10px; color: rgba(84, 96, 118, 175); }
QLabel#itemtext { font-size: 13px; color: #1E2836; }
QLabel#itemdone { font-size: 13px; color: rgba(96, 110, 132, 150); }
QLabel#due { font-size: 11px; color: rgba(84, 96, 118, 190); }
QLabel#duebad { font-size: 11px; color: #C2554F; }
QLabel#days { font-size: 15px; font-weight: 600; color: #3F74D6; }

QFrame#item { background: rgba(255, 255, 255, 105); border-radius: 10px; }
QFrame#item:hover { background: rgba(255, 255, 255, 155); }

QPushButton {
    background: rgba(255, 255, 255, 120);
    border: none;
    border-radius: 10px;
    padding: 6px 14px;
    color: #2A3242;
    font-size: 12px;
}
QPushButton:hover { background: rgba(255, 255, 255, 190); }
QPushButton:pressed { background: rgba(214, 228, 250, 215); }
QPushButton#primary { background: #5B8DEF; color: white; }
QPushButton#primary:hover { background: #6D9AF2; }
QPushButton#primary:pressed { background: #4E7CDB; }
QPushButton#ghost { background: rgba(255, 255, 255, 70); color: rgba(48, 64, 92, 210); }
QPushButton#ghost:hover { background: rgba(255, 255, 255, 140); }
QPushButton#close {
    background: transparent; border: none; border-radius: 11px;
    color: rgba(40, 56, 80, 150); font-size: 13px; padding: 0px;
}
QPushButton#close:hover { background: rgba(240, 90, 90, 170); color: white; }

QLineEdit {
    background: rgba(255, 255, 255, 150);
    border: none;
    border-radius: 9px;
    padding: 6px 10px;
    color: #1B2330;
    font-size: 12px;
    selection-background-color: rgba(110, 160, 240, 140);
}
QLineEdit:focus { background: rgba(255, 255, 255, 205); }

QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 6px; margin: 0px; }
QScrollBar::handle:vertical { background: rgba(60, 80, 110, 70); border-radius: 3px; min-height: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
"""

_QSS_DARK = """
QLabel { color: #E3E8F4; font-size: 12px; }
QLabel#title { font-size: 16px; font-weight: 600; color: #F5F8FF; }
QLabel#tag { font-size: 12px; color: rgba(214, 224, 244, 210); }
QLabel#dim { font-size: 10px; color: rgba(180, 194, 220, 170); }
QLabel#itemtext { font-size: 13px; color: #EDF1FA; }
QLabel#itemdone { font-size: 13px; color: rgba(196, 208, 230, 140); }
QLabel#due { font-size: 11px; color: rgba(196, 208, 230, 175); }
QLabel#duebad { font-size: 11px; color: #F0A6A0; }
QLabel#days { font-size: 15px; font-weight: 600; color: #8FB0F5; }

QFrame#item { background: rgba(255, 255, 255, 20); border-radius: 10px; }
QFrame#item:hover { background: rgba(255, 255, 255, 34); }

QPushButton {
    background: rgba(255, 255, 255, 26);
    border: none;
    border-radius: 10px;
    padding: 6px 14px;
    color: #E3E8F4;
    font-size: 12px;
}
QPushButton:hover { background: rgba(255, 255, 255, 44); }
QPushButton:pressed { background: rgba(150, 178, 246, 60); }
QPushButton#primary { background: rgba(110, 140, 240, 235); color: white; }
QPushButton#primary:hover { background: rgba(126, 156, 250, 250); }
QPushButton#primary:pressed { background: rgba(96, 126, 226, 235); }
QPushButton#ghost { background: rgba(255, 255, 255, 10); color: rgba(214, 224, 244, 220); }
QPushButton#ghost:hover { background: rgba(255, 255, 255, 24); }
QPushButton#close {
    background: transparent; border: none; border-radius: 11px;
    color: rgba(210, 220, 240, 175); font-size: 13px; padding: 0px;
}
QPushButton#close:hover { background: rgba(240, 90, 90, 180); color: white; }

QLineEdit {
    background: rgba(255, 255, 255, 16);
    border: none;
    border-radius: 9px;
    padding: 6px 10px;
    color: #EDF1FA;
    font-size: 12px;
    selection-background-color: rgba(126, 156, 246, 140);
}
QLineEdit:focus { background: rgba(255, 255, 255, 28); }

QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 6px; margin: 0px; }
QScrollBar::handle:vertical { background: rgba(255, 255, 255, 60); border-radius: 3px; min-height: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
"""
