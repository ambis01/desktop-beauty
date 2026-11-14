"""时钟卡片的设置小窗：表盘、字体、24/12 小时、读秒、农历、时区、文字深浅、玻璃浓度。

从面板「卡片」页某张时钟卡片的「设置」进去。改动**立刻生效**，没有确定/取消；
上面那块预览就用当前选择实时算，换什么都能当场看到效果。

外观和取色盘同一套：玻璃底（paint_frost）、圆角、深浅色跟随面板。
"""
from __future__ import annotations

from PySide6.QtCore import QDateTime, QEventLoop, Qt, QTimeZone, QTimer
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)

from . import weather as weather_mod
from .clock_widget import FACES, FONTS
from .controls import GlassCheck, GlassCombo
from .lunar import lunar_line
from .picker import _FrostDialog
from .ui import scale_qss, u

# 常用时区。宁可给一份短名单，也不要把 400 多个 IANA 时区全塞进一个菜单。
TIMEZONES = (
    ("本机时区", ""),
    ("北京 UTC+8", "Asia/Shanghai"),
    ("香港 UTC+8", "Asia/Hong_Kong"),
    ("台北 UTC+8", "Asia/Taipei"),
    ("东京 UTC+9", "Asia/Tokyo"),
    ("首尔 UTC+9", "Asia/Seoul"),
    ("新加坡 UTC+8", "Asia/Singapore"),
    ("曼谷 UTC+7", "Asia/Bangkok"),
    ("印度 UTC+5:30", "Asia/Kolkata"),
    ("迪拜 UTC+4", "Asia/Dubai"),
    ("伦敦 UTC+0", "Europe/London"),
    ("巴黎 / 柏林 UTC+1", "Europe/Paris"),
    ("莫斯科 UTC+3", "Europe/Moscow"),
    ("纽约 UTC-5", "America/New_York"),
    ("芝加哥 UTC-6", "America/Chicago"),
    ("丹佛 UTC-7", "America/Denver"),
    ("洛杉矶 UTC-8", "America/Los_Angeles"),
    ("圣保罗 UTC-3", "America/Sao_Paulo"),
    ("悉尼 UTC+11", "Australia/Sydney"),
    ("奥克兰 UTC+13", "Pacific/Auckland"),
    ("UTC 协调世界时", "UTC"),
)

FONT_LABELS = {
    "segoe": "Segoe UI（默认）",
    "yahei": "微软雅黑",
    "dengxian": "等线 DengXian",
    "bahnschrift": "Bahnschrift",
    "consolas": "Consolas 等宽",
    "cascadia": "Cascadia Code 等宽",
    "georgia": "Georgia 衬线",
}

FACE_LABELS = dict(FACES)


def _offset_text(minutes: int) -> str:
    sign = "+" if minutes >= 0 else "-"
    m = abs(int(minutes))
    hh, mm = divmod(m, 60)
    return f"UTC{sign}{hh}" + (f":{mm:02d}" if mm else "")


class ClockSettingsDialog(_FrostDialog):
    """一张时钟卡片的设置。on_change(key, value) 立刻写回卡片。"""

    def __init__(self, card, on_change, dark: bool = False, parent=None):
        flags = Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
        super().__init__(flags, 10.0, dark, parent)
        self.card = card
        self._on_change = on_change
        self._state = {
            "show_seconds": bool(getattr(card, "show_seconds", True)),
            "hour24": bool(getattr(card, "hour24", True)),
            "lunar": bool(getattr(card, "lunar", True)),
            "face": str(getattr(card, "face", "digital") or "digital"),
            "font": str(getattr(card, "font", "segoe") or "segoe"),
            "timezone": str(getattr(card, "timezone", "") or ""),
            "weather": bool(getattr(card, "weather", False)),
            "city": str(getattr(card, "city", "") or ""),
        }
        self._building = True
        self.setFixedSize(u(520), u(510))
        self.setStyleSheet(_QSS_DARK if self._dark else _QSS)

        root = QVBoxLayout(self)
        root.setContentsMargins(u(18), u(14), u(18), u(16))
        root.setSpacing(u(9))

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title = QLabel("时钟卡片")
        title.setObjectName("title")
        header.addWidget(title)
        header.addStretch(1)
        btn_close = QPushButton("✕")
        btn_close.setObjectName("close")
        btn_close.setFixedSize(u(24), u(24))
        btn_close.clicked.connect(self.reject)
        header.addWidget(btn_close)
        root.addLayout(header)

        # 预览
        plate = QFrame()
        plate.setObjectName("plate")
        pcol = QVBoxLayout(plate)
        pcol.setContentsMargins(u(16), u(10), u(16), u(10))
        pcol.setSpacing(u(2))
        self.lbl_time = QLabel("")
        self.lbl_time.setObjectName("big")
        pcol.addWidget(self.lbl_time)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(u(8))
        self.lbl_date = QLabel("")
        self.lbl_date.setObjectName("small")
        row.addWidget(self.lbl_date)
        row.addStretch(1)
        self.lbl_zone = QLabel("")
        self.lbl_zone.setObjectName("small")
        row.addWidget(self.lbl_zone)
        pcol.addLayout(row)
        # 农历这一行高度固定住：开关它的时候下面的控件不会跳一下
        self.lbl_lunar = QLabel("")
        self.lbl_lunar.setObjectName("small")
        self.lbl_lunar.setFixedHeight(u(15))
        pcol.addWidget(self.lbl_lunar)
        root.addWidget(plate)

        # 表盘 / 字体 / 时间格式 / 时区
        self.cmb_face = GlassCombo(light=not self._dark)
        self.cmb_face.set_items([(FACE_LABELS[k], k) for k, _ in FACES],
                                _index_of([k for k, _ in FACES], self._state["face"]))
        self.cmb_face.changed.connect(
            lambda v: self._set("face", str(v)))
        root.addLayout(self._row("表盘", self.cmb_face))

        self.cmb_font = GlassCombo(light=not self._dark)
        font_items = [(FONT_LABELS.get(k, k), k) for k in FONTS]
        self.cmb_font.set_items(font_items,
                                _index_of([k for k in FONTS], self._state["font"]))
        self.cmb_font.changed.connect(lambda v: self._set("font", str(v)))
        root.addLayout(self._row("字体", self.cmb_font))

        self.cmb_hour = GlassCombo(light=not self._dark)
        self.cmb_hour.set_items([("24 小时制", "24"), ("12 小时制", "12")],
                                0 if self._state["hour24"] else 1)
        self.cmb_hour.changed.connect(
            lambda v: self._set("hour24", str(v) == "24"))
        root.addLayout(self._row("时间", self.cmb_hour))

        items = list(TIMEZONES)
        if self._state["timezone"] and all(
                v != self._state["timezone"] for _l, v in items):
            items.append((self._state["timezone"], self._state["timezone"]))
        self.cmb_zone = GlassCombo(light=not self._dark)
        self.cmb_zone.set_items(items, _index_of([v for _l, v in items],
                                                 self._state["timezone"]))
        self.cmb_zone.changed.connect(lambda v: self._set("timezone", str(v)))
        root.addLayout(self._row("时区", self.cmb_zone))

        # 两个开关并排，省一行高度
        checks = QHBoxLayout()
        checks.setContentsMargins(0, 0, 0, 0)
        checks.setSpacing(u(22))
        self.chk_seconds = GlassCheck("显示秒", dark=self._dark)
        self.chk_seconds.setChecked(self._state["show_seconds"])
        self.chk_seconds.toggled.connect(
            lambda on: self._set("show_seconds", bool(on)))
        checks.addWidget(self.chk_seconds)
        self.chk_lunar = GlassCheck("农历和节气", dark=self._dark)
        self.chk_lunar.setChecked(self._state["lunar"])
        self.chk_lunar.toggled.connect(lambda on: self._set("lunar", bool(on)))
        checks.addWidget(self.chk_lunar)
        checks.addStretch(1)
        root.addLayout(checks)

        # ---- 天气 ----
        head = QLabel("天气")
        head.setObjectName("head")
        root.addWidget(head)
        self.chk_weather = GlassCheck("在卡片右边显示天气", dark=self._dark)
        self.chk_weather.setChecked(self._state["weather"])
        self.chk_weather.toggled.connect(
            lambda on: self._set("weather", bool(on)))
        root.addWidget(self.chk_weather)

        names = [c for c in weather_mod.CITY_CHOICES]
        if self._state["city"] and self._state["city"] not in names:
            names.append(self._state["city"])
        items = [("自动定位（按 IP）", "")] + [(c, c) for c in names]
        self.cmb_city = GlassCombo(light=not self._dark)
        self.cmb_city.set_items(
            items, _index_of([v for _l, v in items], self._state["city"]))
        self.cmb_city.changed.connect(lambda v: self._set("city", str(v or "")))
        root.addLayout(self._row("城市", self.cmb_city))

        root.addStretch(1)
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.addStretch(1)
        self.btn_ok = QPushButton("完成")
        self.btn_ok.setObjectName("primary")
        self.btn_ok.clicked.connect(self.accept)
        footer.addWidget(self.btn_ok)
        root.addLayout(footer)

        self._building = False
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick_preview)
        self._timer.start()
        self._tick_preview()

    # ---------- 布局小工具 ----------
    def _row(self, label: str, widget) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(u(10))
        lbl = QLabel(label)
        lbl.setObjectName("tag")
        lbl.setFixedWidth(u(58))
        row.addWidget(lbl)
        row.addWidget(widget, 1)
        return row

    # ---------- 预览 ----------
    def _now(self) -> QDateTime:
        now = QDateTime.currentDateTime()
        tz_id = self._state["timezone"]
        if tz_id:
            tz = QTimeZone(tz_id.encode())
            if tz.isValid():
                return now.toTimeZone(tz)
        return now

    def _tick_preview(self) -> None:
        now = self._now()
        offset = now.offsetFromUtc() // 60
        hour24 = self._state["hour24"]
        fmt = "HH:mm" if hour24 else "h:mm"
        if self._state["show_seconds"]:
            fmt += ":ss"
        if not hour24:
            fmt += " AP"
        self.lbl_time.setText(now.toString(fmt))
        self.lbl_date.setText(now.toString("M月d日  dddd"))
        self.lbl_zone.setText(_offset_text(offset))
        self.lbl_lunar.setText(
            lunar_line(now.date().toPython()) if self._state["lunar"] else "")

    # ---------- 交互 ----------
    def _set(self, key: str, value) -> None:
        self._state[key] = value
        self._tick_preview()
        if not self._building:
            self._on_change(key, value)

    def accept(self) -> None:
        self._finish()

    # ---------- 打开 ----------
    @classmethod
    def edit(cls, card, on_change, dark: bool = False, parent=None) -> None:
        dlg = cls(card, on_change, dark, parent)
        dlg.center_over(parent)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        loop = QEventLoop()
        dlg.closed.connect(loop.quit)
        loop.exec()
        try:
            dlg._timer.stop()
            dlg.deleteLater()
        except (RuntimeError, TypeError):
            pass


def _index_of(values, wanted) -> int:
    for i, v in enumerate(values):
        if v == wanted:
            return i
    return 0


# ---------------------------------------------------------------- 其它卡片
class _SimpleSettings(_FrostDialog):
    """待办 / 日历 / 倒计时 卡片的设置：都是一两个开关，样子统一。

    改动立刻生效（和时钟那张一样没有确定/取消），下面那个按钮用来打开
    对应的管理窗口。
    """

    TITLE = ""
    SIZE = (460, 250)

    def __init__(self, card, on_change, dark: bool = False, parent=None, **extra):
        flags = Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
        super().__init__(flags, 10.0, dark, parent)
        self.card = card
        self._on_change = on_change
        self._agenda = extra.get("agenda")
        self.setFixedSize(u(self.SIZE[0]), u(self.SIZE[1]))
        self.setStyleSheet(_QSS_DARK if self._dark else _QSS)

        root = QVBoxLayout(self)
        root.setContentsMargins(u(18), u(14), u(18), u(16))
        root.setSpacing(u(9))

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title = QLabel(self.TITLE)
        title.setObjectName("title")
        header.addWidget(title)
        header.addStretch(1)
        btn_close = QPushButton("✕")
        btn_close.setObjectName("close")
        btn_close.setFixedSize(u(24), u(24))
        btn_close.clicked.connect(self.reject)
        header.addWidget(btn_close)
        root.addLayout(header)

        self._col = QVBoxLayout()
        self._col.setContentsMargins(0, 0, 0, 0)
        self._col.setSpacing(u(9))
        root.addLayout(self._col)
        root.addStretch(1)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.addStretch(1)
        btn_ok = QPushButton("完成")
        btn_ok.setObjectName("primary")
        btn_ok.clicked.connect(self.accept)
        footer.addWidget(btn_ok)
        root.addLayout(footer)

        self.build()

    def build(self) -> None:
        """子类往 self._col 里加东西。"""

    def _row(self, label: str, widget) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(u(10))
        lbl = QLabel(label)
        lbl.setObjectName("tag")
        lbl.setFixedWidth(u(64))
        row.addWidget(lbl)
        row.addWidget(widget, 1)
        return row

    def _check(self, text: str, on: bool, key: str) -> GlassCheck:
        chk = GlassCheck(text, dark=self._dark)
        chk.setChecked(bool(on))
        chk.toggled.connect(lambda value: self._set(key, bool(value)))
        self._col.addWidget(chk)
        return chk

    def _button(self, text: str, slot) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName("ghost")
        btn.clicked.connect(slot)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(btn, 0, Qt.AlignLeft)
        self._col.addLayout(row)
        return btn

    def _set(self, key: str, value) -> None:
        self._on_change(key, value)

    def accept(self) -> None:
        self._finish()

    @classmethod
    def edit(cls, card, on_change, dark: bool = False, parent=None, **extra) -> None:
        dlg = cls(card, on_change, dark, parent, **extra)
        dlg.center_over(parent)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        loop = QEventLoop()
        dlg.closed.connect(loop.quit)
        loop.exec()
        try:
            dlg.deleteLater()
        except (RuntimeError, TypeError):
            pass


class TodoSettingsDialog(_SimpleSettings):
    TITLE = "待办卡片"
    SIZE = (460, 200)

    def build(self) -> None:
        self._check("显示已完成的待办", getattr(self.card, "todo_show_done", False),
                    "todo_show_done")
        self._button("打开待办列表", self._open_list)

    def _open_list(self) -> None:
        from .agenda_dialog import TodoDialog
        if self._agenda is not None:
            TodoDialog.show_for(self._agenda, self._dark, self)


class CalendarSettingsDialog(_SimpleSettings):
    TITLE = "日历卡片"
    SIZE = (460, 216)

    def build(self) -> None:
        self._check("每周从周一开始", getattr(self.card, "cal_monday", True),
                    "cal_monday")
        self._check("格子下面显示农历和节气", getattr(self.card, "cal_lunar", True),
                    "cal_lunar")


class CountdownSettingsDialog(_SimpleSettings):
    TITLE = "倒计时卡片"
    SIZE = (460, 236)

    def build(self) -> None:
        items = [("自动（最近的一个）", "")]
        pin = str(getattr(self.card, "countdown_pin", "") or "")
        if self._agenda is not None:
            for event, _when, _days in self._agenda.upcoming():
                items.append((event.title, event.id))
        values = [v for _l, v in items]
        if pin and pin not in values:
            items.append((pin, pin))
            values.append(pin)
        combo = GlassCombo(light=not self._dark)
        combo.set_items(items, _index_of(values, pin))
        combo.changed.connect(lambda value: self._set("countdown_pin", str(value or "")))
        self._col.addLayout(self._row("显示", combo))
        self._button("管理纪念日", self._open_list)

    def _open_list(self) -> None:
        from .agenda_dialog import CountdownDialog
        if self._agenda is not None:
            CountdownDialog.show_for(self._agenda, self._dark, self)


_QSS = scale_qss("""
QLabel { color: #2A3242; font-size: 12px; }
QLabel#title { font-size: 15px; font-weight: 600; color: #141A24; }
QLabel#tag { font-size: 12px; color: rgba(52, 68, 94, 200); }
QLabel#big {
    font-size: 40px;
    font-weight: 300;
    color: #1B2330;
    letter-spacing: 1px;
}
QLabel#small { font-size: 11px; color: rgba(58, 70, 92, 205); }
QLabel#head { font-size: 10px; letter-spacing: 2px; color: rgba(58, 74, 100, 150); }

QFrame#plate { background: rgba(255, 255, 255, 105); border-radius: 12px; }

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

""")

_QSS_DARK = scale_qss("""
QLabel { color: #E3E8F4; font-size: 12px; }
QLabel#title { font-size: 15px; font-weight: 600; color: #F5F8FF; }
QLabel#tag { font-size: 12px; color: rgba(214, 224, 244, 210); }
QLabel#big {
    font-size: 40px;
    font-weight: 300;
    color: #F2F6FF;
    letter-spacing: 1px;
}
QLabel#small { font-size: 11px; color: rgba(200, 210, 234, 200); }
QLabel#head { font-size: 10px; letter-spacing: 2px; color: rgba(196, 206, 228, 145); }

QFrame#plate { background: rgba(255, 255, 255, 22); border-radius: 12px; }

QPushButton {
    background: rgba(255, 255, 255, 26);
    border: none;
    border-radius: 10px;
    padding: 7px 16px;
    color: #E3E8F4;
    font-size: 12px;
}
QPushButton:hover { background: rgba(255, 255, 255, 44); }
QPushButton:pressed { background: rgba(150, 178, 246, 60); }
QPushButton#primary { background: #5B8DEF; color: white; }
QPushButton#primary:hover { background: #6D9AF2; }
QPushButton#primary:pressed { background: #4E7CDB; }
QPushButton#close {
    background: transparent; border: none; border-radius: 12px;
    color: rgba(210, 220, 240, 175); font-size: 13px; padding: 0px;
}
QPushButton#close:hover { background: rgba(240, 90, 90, 180); color: white; }

""")
