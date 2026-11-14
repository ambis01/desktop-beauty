"""天气详情窗：点时钟卡片右边的天气块弹出来。

内容：当前实况、空气质量 / 紫外线 / 日出日落 / 月相、未来 24 小时、未来 7 天。
数据来自 weather.WeatherService（后台线程抓，抓回来会发 updated 自动重画）。
"""
from __future__ import annotations

import time
from datetime import datetime

from PySide6.QtCore import QEventLoop, QRectF, Qt, QTimer
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from . import weather as weather_mod
from . import weather_icons
from .picker import _FrostDialog
from .ui import scale_qss, u


class _Icon(QWidget):
    """一个小方块，里面画天气图标。"""

    def __init__(self, icon: str = "cloud", size: int = 44, parent=None):
        super().__init__(parent)
        self._icon = icon
        self.setFixedSize(size, size)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        weather_icons.paint_icon(
            p, QRectF(0.0, 0.0, float(self.width()), float(self.height())),
            self._icon)
        p.end()


def _weekday(iso: str) -> str:
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    today = datetime.now().date()
    if d.date() == today:
        return "今天"
    if (d.date() - today).days == 1:
        return "明天"
    return "周" + "一二三四五六日"[d.weekday()]


def _hhmm(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except (ValueError, TypeError):
        return ""


def _hour_of(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%H:00")
    except (ValueError, TypeError):
        return ""


def _temp(value) -> str:
    if value is None:
        return "—"
    return f"{round(value)}°"


class WeatherDialog(_FrostDialog):
    def __init__(self, key: str, service, dark: bool = False, parent=None):
        flags = Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
        super().__init__(flags, 10.0, dark, parent)
        self.key = key or ""
        self.service = service
        self.setFixedSize(u(468), u(640))
        self.setStyleSheet(_QSS_DARK if self._dark else _QSS)

        root = QVBoxLayout(self)
        root.setContentsMargins(u(18), u(14), u(18), u(16))
        root.setSpacing(u(9))

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self.lbl_title = QLabel("天气")
        self.lbl_title.setObjectName("title")
        header.addWidget(self.lbl_title)
        self.lbl_updated = QLabel("")
        self.lbl_updated.setObjectName("dim")
        header.addWidget(self.lbl_updated)
        header.addStretch(1)
        self._btn_refresh = QPushButton("刷新")
        self._btn_refresh.setObjectName("ghost")
        self._btn_refresh.clicked.connect(self._refresh)
        header.addWidget(self._btn_refresh)
        btn_close = QPushButton("✕")
        btn_close.setObjectName("close")
        btn_close.setFixedSize(u(24), u(24))
        btn_close.clicked.connect(self.reject)
        header.addWidget(btn_close)
        root.addLayout(header)

        # ---- 当前实况 ----
        now_box = QFrame()
        now_box.setObjectName("plate")
        nrow = QHBoxLayout(now_box)
        nrow.setContentsMargins(u(16), u(12), u(16), u(12))
        nrow.setSpacing(u(12))
        self.icon_now = _Icon("cloud", u(58))
        nrow.addWidget(self.icon_now)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        self.lbl_temp = QLabel("—")
        self.lbl_temp.setObjectName("big")
        col.addWidget(self.lbl_temp)
        self.lbl_what = QLabel("")
        self.lbl_what.setObjectName("small")
        col.addWidget(self.lbl_what)
        nrow.addLayout(col)
        nrow.addStretch(1)
        meta = QVBoxLayout()
        meta.setContentsMargins(0, 0, 0, 0)
        meta.setSpacing(u(2))
        self.lbl_feels = QLabel("")
        self.lbl_humidity = QLabel("")
        self.lbl_wind = QLabel("")
        for lbl in (self.lbl_feels, self.lbl_humidity, self.lbl_wind):
            lbl.setObjectName("small")
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            meta.addWidget(lbl)
        nrow.addLayout(meta)
        root.addWidget(now_box)

        # ---- 空气质量 / 紫外线 / 日出日落 / 月相 ----
        stats_box = QFrame()
        stats_box.setObjectName("plate")
        srow = QHBoxLayout(stats_box)
        srow.setContentsMargins(u(14), u(8), u(14), u(8))
        srow.setSpacing(u(8))
        self._stats = {}
        for key, name in (("aqi", "空气质量"), ("uv", "紫外线"), ("rise", "日出"),
                          ("set", "日落"), ("moon", "月相")):
            cell = QVBoxLayout()
            cell.setContentsMargins(0, 0, 0, 0)
            cell.setSpacing(0)
            value = QLabel("—")
            value.setObjectName("value")
            value.setAlignment(Qt.AlignHCenter)
            name_lbl = QLabel(name)
            name_lbl.setObjectName("dim")
            name_lbl.setAlignment(Qt.AlignHCenter)
            cell.addWidget(value)
            cell.addWidget(name_lbl)
            srow.addLayout(cell, 1)
            self._stats[key] = value
        root.addWidget(stats_box)

        # ---- 未来 24 小时 ----
        self.lbl_hourly_head = QLabel("未来 24 小时")
        self.lbl_hourly_head.setObjectName("head")
        root.addWidget(self.lbl_hourly_head)
        hours_box = QFrame()
        hours_box.setObjectName("plate")
        self.hour_grid = QGridLayout(hours_box)
        self.hour_grid.setContentsMargins(u(10), u(8), u(10), u(8))
        self.hour_grid.setHorizontalSpacing(u(2))
        self.hour_grid.setVerticalSpacing(u(4))
        root.addWidget(hours_box)

        # ---- 7 天 ----
        self.lbl_daily_head = QLabel("未来 7 天")
        self.lbl_daily_head.setObjectName("head")
        root.addWidget(self.lbl_daily_head)
        self.daily_box = QVBoxLayout()
        self.daily_box.setContentsMargins(0, 0, 0, 0)
        self.daily_box.setSpacing(u(3))
        root.addLayout(self.daily_box)
        root.addStretch(1)

        self._cells = []
        self._manual = False
        self._updated_text = ""
        self._flash = QTimer(self)
        self._flash.setSingleShot(True)
        self._flash.timeout.connect(self._restore_updated)
        self._fallback = QTimer(self)
        self._fallback.setSingleShot(True)
        self._fallback.timeout.connect(self._end_refresh)
        self._timer = QTimer(self)
        self._timer.setInterval(60 * 1000)
        self._timer.timeout.connect(self._fill)
        self._timer.start()
        self._fill()

    # ---- 数据 ----
    def _refresh(self) -> None:
        if self.service is None:
            return
        self._btn_refresh.setText("刷新中…")
        self._btn_refresh.setEnabled(False)
        self._manual = True
        self._fallback.start(12000)          # 网络卡死时也别一直转
        try:
            self.service.refresh([self.key], force=True)
        except (AttributeError, TypeError):
            self._end_refresh()

    def _restore_updated(self) -> None:
        self.lbl_updated.setText(self._updated_text)

    def _end_refresh(self) -> None:
        self._fallback.stop()
        self._btn_refresh.setText("刷新")
        self._btn_refresh.setEnabled(True)

    def _clear_hours(self) -> None:
        while self.hour_grid.count():
            item = self.hour_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._cells.clear()

    def _clear_daily(self) -> None:
        while self.daily_box.count():
            item = self.daily_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _fill(self) -> None:
        data = self.service.get(self.key) if self.service is not None else None
        self._clear_hours()
        self._clear_daily()
        if data is None:
            self.lbl_title.setText("天气")
            self.lbl_temp.setText("—")
            self.lbl_what.setText("正在获取…" if self.service else "")
            self.icon_now._icon = "cloud"
            self.icon_now.update()
            for lbl in (self.lbl_feels, self.lbl_humidity, self.lbl_wind):
                lbl.setText("")
            for v in self._stats.values():
                v.setText("—")
            self._end_refresh()
            return

        self.lbl_title.setText(data.city or "天气")
        if data.stamp:
            self._updated_text = "更新于 " + time.strftime(
                "%H:%M", time.localtime(data.stamp))
        if self._manual:
            # 手动刷新完给个明确回执，不然数据没变就跟没点一样
            self._manual = False
            self.lbl_updated.setText("已更新")
            self._flash.start(2500)
        elif not self._flash.isActive():
            self.lbl_updated.setText(self._updated_text)
        self._end_refresh()
        self.icon_now._icon = data.icon
        self.icon_now.update()
        self.lbl_temp.setText(_temp(data.temp))
        self.lbl_what.setText(data.label)
        self.lbl_feels.setText(
            f"体感 {_temp(data.feels)}" if data.feels is not None else "")
        self.lbl_humidity.setText(
            f"湿度 {round(data.humidity)}%" if data.humidity is not None else "")
        self.lbl_wind.setText(
            f"风速 {round(data.wind)} km/h" if data.wind is not None else "")

        aqi = data.aqi
        self._stats["aqi"].setText(
            f"{aqi} {weather_mod.aqi_label(aqi)}" if aqi is not None else "—")
        self._stats["uv"].setText(
            f"{round(data.uv)} {weather_mod.uv_label(data.uv)}"
            if data.uv is not None else "—")
        self._stats["rise"].setText(data.sunrise or "—")
        self._stats["set"].setText(data.sunset or "—")
        moon, illum = weather_mod.moon_phase(datetime.now().date())
        self._stats["moon"].setText(f"{moon} {illum}%")

        # 未来 24 小时：从现在这一小时起，12 × 2 排
        now = datetime.now().replace(minute=0, second=0, microsecond=0)
        items = []
        for item in data.hourly:
            try:
                when = datetime.fromisoformat(item[0])
            except (ValueError, TypeError):
                continue
            if when < now:
                continue
            items.append(item)
            if len(items) >= 24:
                break
        for i, (iso, temp, code, _pop) in enumerate(items):
            cell = QWidget()
            v = QVBoxLayout(cell)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(u(2))
            t = QLabel("现在" if i == 0 else _hour_of(iso))
            t.setObjectName("dim")
            t.setAlignment(Qt.AlignHCenter)
            icon = _Icon(weather_mod.describe(code)[1], u(22))
            temp_lbl = QLabel(_temp(temp))
            temp_lbl.setObjectName("value")
            temp_lbl.setAlignment(Qt.AlignHCenter)
            v.addWidget(t)
            v.addWidget(icon, 0, Qt.AlignHCenter)
            v.addWidget(temp_lbl)
            self.hour_grid.addWidget(cell, i // 12, i % 12)
            self._cells.append(cell)

        for iso, code, high, low, pop in data.daily:
            row = QWidget()
            line = QHBoxLayout(row)
            line.setContentsMargins(u(12), u(3), u(12), u(3))
            line.setSpacing(u(10))
            name = QLabel(_weekday(iso))
            name.setObjectName("small")
            name.setFixedWidth(u(52))
            line.addWidget(name)
            icon = _Icon(weather_mod.describe(code)[1], u(22))
            line.addWidget(icon)
            what = QLabel(weather_mod.describe(code)[0])
            what.setObjectName("small")
            line.addWidget(what)
            line.addStretch(1)
            if pop is not None and pop >= 20:
                rain = QLabel(f"{round(pop)}%")
                rain.setObjectName("dim")
                line.addWidget(rain)
            span = QLabel(f"{_temp(low)} / {_temp(high)}")
            span.setObjectName("value")
            line.addWidget(span)
            self.daily_box.addWidget(row)

    # ---- 打开 ----
    @classmethod
    def show_for(cls, key: str, service, dark: bool = False,
                 parent=None) -> None:
        dlg = cls(key, service, dark, parent)
        dlg.center_over(parent)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        if service is not None:
            try:
                service.refresh([key or ""])
                service.updated.connect(dlg._fill)
                dlg.closed.connect(lambda: _disconnect(service, dlg))
            except (AttributeError, TypeError):
                pass
        loop = QEventLoop()
        dlg.closed.connect(loop.quit)
        loop.exec()
        try:
            dlg._timer.stop()
            dlg.deleteLater()
        except (RuntimeError, TypeError):
            pass


def _disconnect(service, dlg) -> None:
    try:
        service.updated.disconnect(dlg._fill)
    except (RuntimeError, TypeError):
        pass


_QSS = scale_qss("""
QLabel { color: #2A3242; font-size: 12px; }
QLabel#title { font-size: 16px; font-weight: 600; color: #141A24; }
QLabel#head { font-size: 10px; letter-spacing: 2px; color: rgba(58, 74, 100, 150); }
QLabel#big { font-size: 34px; font-weight: 300; color: #1B2330; }
QLabel#small { font-size: 11px; color: rgba(58, 70, 92, 195); }
QLabel#value { font-size: 12px; color: #1E2836; }
QLabel#dim { font-size: 10px; color: rgba(84, 96, 118, 175); }

QFrame#plate { background: rgba(255, 255, 255, 105); border-radius: 12px; }

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
QPushButton#ghost { background: rgba(255, 255, 255, 70); color: rgba(48, 64, 92, 210); }
QPushButton#ghost:hover { background: rgba(255, 255, 255, 140); }
QPushButton#close {
    background: transparent; border: none; border-radius: 12px;
    color: rgba(40, 56, 80, 150); font-size: 13px; padding: 0px;
}
QPushButton#close:hover { background: rgba(240, 90, 90, 170); color: white; }
""")

_QSS_DARK = scale_qss("""
QLabel { color: #E3E8F4; font-size: 12px; }
QLabel#title { font-size: 16px; font-weight: 600; color: #F5F8FF; }
QLabel#head { font-size: 10px; letter-spacing: 2px; color: rgba(196, 206, 228, 145); }
QLabel#big { font-size: 34px; font-weight: 300; color: #F2F6FF; }
QLabel#small { font-size: 11px; color: rgba(200, 210, 234, 200); }
QLabel#value { font-size: 12px; color: #EDF1FA; }
QLabel#dim { font-size: 10px; color: rgba(180, 194, 220, 170); }

QFrame#plate { background: rgba(255, 255, 255, 22); border-radius: 12px; }

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
QPushButton#ghost { background: rgba(255, 255, 255, 10); color: rgba(214, 224, 244, 220); }
QPushButton#ghost:hover { background: rgba(255, 255, 255, 24); }
QPushButton#close {
    background: transparent; border: none; border-radius: 12px;
    color: rgba(210, 220, 240, 175); font-size: 13px; padding: 0px;
}
QPushButton#close:hover { background: rgba(240, 90, 90, 180); color: white; }
""")
