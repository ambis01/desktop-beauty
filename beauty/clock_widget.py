"""桌面时钟卡片。参数都放在 CardData 里，由设置窗口改。

外壳（拖动 / 悬停放大 / 改大小 / 三个点）在 card_base.CardWindow 里，这里
只管「时钟怎么画」：三种表盘、日期农历、右边那块天气。
"""
from __future__ import annotations

import math
from typing import Callable

from PySide6.QtCore import (
    QDateTime, QPoint, QPointF, QRectF, Qt, QTimeZone, QTimer,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout

from .card_base import CardWindow
from .config import CardData
from . import lunar as lunar_mod
from . import weather_icons

WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

# 深灰蓝，和收纳盒里那排标题一个色（白玻璃上不能再用白字）
INK = "rgba(52, 62, 82, 248)"
INK_SOFT = "rgba(58, 70, 92, 196)"
INK_DATE = "rgba(58, 70, 92, 226)"
INK_NOTE = "rgba(84, 96, 118, 190)"

# 时间数字可选字体：key → (family 列表, 权重)
FONTS = {
    "segoe": (("Segoe UI Variable Display", "Segoe UI Light", "Segoe UI"), "light"),
    "yahei": (("Microsoft YaHei UI", "Microsoft YaHei"), "light"),
    "dengxian": (("DengXian", "Microsoft YaHei UI"), "light"),
    "bahnschrift": (("Bahnschrift Light", "Bahnschrift"), "light"),
    "consolas": (("Consolas", "Cascadia Mono"), "normal"),
    "cascadia": (("Cascadia Code", "Cascadia Mono"), "normal"),
    "georgia": (("Georgia", "Times New Roman"), "normal"),
}

FACES = (("digital", "数字"), ("flip", "翻页"), ("analog", "指针"))

CLOCK_W = 302       # 卡片宽度（设计像素，乘 scale）
WEATHER_W = 100     # 卡片右侧天气块占的宽度（在卡片内部，不加宽）


class ClockWidget(CardWindow):
    DESIGN_W = CLOCK_W
    DESIGN_H = 152

    def __init__(self, card: CardData, on_change: Callable[[], None],
                 radius: float = 10):
        # 这些属性 _apply_style / paint 会用到，得在 super().__init__ 之前就绪
        self._hm_text = ""
        self._sec_text = ""
        self._ampm_text = ""
        self._date_text = ""
        self._lunar_text = ""
        self.weather_data = None      # weather.Weather，由 Controller 塞进来
        self.on_open_weather = None   # 点右边天气块 → 打开详细预报
        self.clock_w = CLOCK_W
        self._wblock_w = 0
        self._wblock_x = 0
        self._label_draws = []
        super().__init__(card, on_change, radius)

        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._tick()

    # ---------- 内容控件 ----------
    def build_body(self) -> None:
        self.lbl_hm = QLabel(self)
        self.lbl_sec = QLabel(self)
        self.lbl_ampm = QLabel(self)
        self.lbl_date = QLabel(self)
        self.lbl_lunar = QLabel(self)
        for w in (self.lbl_hm, self.lbl_sec, self.lbl_ampm,
                  self.lbl_date, self.lbl_lunar):
            w.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        # 文字改成自绘（悬停时字要跟玻璃一起放大），QLabel 只留作排版：
        # 布局算好位置之后就不让它自己画了，paintEvent 里按同样的矩形 drawText。
        # 颜色在 _apply_style 里统一给。
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self.lbl_hm)
        row.addWidget(self.lbl_sec, 0, Qt.AlignBottom)
        row.addWidget(self.lbl_ampm, 0, Qt.AlignBottom)
        row.addStretch(1)
        root.addLayout(row)
        root.addWidget(self.lbl_date)
        root.addWidget(self.lbl_lunar)
        root.addStretch(1)

    # ---------- 外观 ----------
    def _face(self) -> str:
        face = str(getattr(self.card, "face", "digital") or "digital")
        return face if face in ("digital", "flip", "analog") else "digital"

    def _apply_style(self) -> None:
        s = self.card.scale
        m = self._px(24) + self.inset
        face = self._face()

        # 时间区高度：数字脸交给字体自己撑；翻页/指针脸要留出画板的格子
        time_h = 0
        if face == "flip":
            time_h = self._px(104)
        elif face == "analog":
            time_h = self._px(132)

        extra = 0 if not time_h else time_h - self._px(56)
        self.clock_w = self._px(CLOCK_W)
        # 天气块在卡片**里面**（右边那块空地），卡片宽度不变
        self._wblock_w = self._px(WEATHER_W)
        self._wblock_x = self.clock_w - self._px(16) - self._wblock_w
        # 窗口 = 玻璃 + 四周余量（余量是透明的，平时看不出来）
        self.setFixedSize(self._px(CLOCK_W) + self.inset * 2,
                          self._px(152) + extra + self.inset * 2)

        root: QVBoxLayout = self.layout()
        root.setContentsMargins(m, self._px(15) + self.inset,
                                m, self._px(15) + self.inset)
        root.setSpacing(self._px(1))

        row: QHBoxLayout = root.itemAt(0).layout()
        row.setSpacing(self._px(7))

        families, weight = FONTS.get(str(getattr(self.card, "font", "segoe") or "segoe"),
                                     FONTS["segoe"])
        hm = QFont()
        hm.setFamilies(list(families))
        hm.setPixelSize(self._px(47))
        hm.setWeight(QFont.Weight.Light if weight == "light" else QFont.Weight.Normal)
        hm.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.6 * s)
        self.lbl_hm.setFont(hm)
        self.lbl_hm.setStyleSheet("color: transparent; background: transparent;")
        # 翻页 / 指针脸：时间由 paintEvent 自己画，这里只把那一格的高度占住
        if time_h:
            self.lbl_hm.setFixedHeight(time_h)
        else:
            self.lbl_hm.setMinimumHeight(0)
            self.lbl_hm.setMaximumHeight(16777215)

        sec = QFont()
        sec.setFamilies(["Segoe UI Variable Text", "Segoe UI"])
        sec.setPixelSize(self._px(13))
        sec.setWeight(QFont.Weight.Medium)
        self.lbl_sec.setFont(sec)
        self.lbl_sec.setContentsMargins(0, 0, 0, self._px(10))
        self.lbl_sec.setStyleSheet("color: transparent; background: transparent;")

        ampm = QFont()
        ampm.setFamilies(["Microsoft YaHei UI", "Segoe UI"])
        ampm.setPixelSize(self._px(11))
        ampm.setWeight(QFont.Weight.Normal)
        self.lbl_ampm.setFont(ampm)
        self.lbl_ampm.setContentsMargins(self._px(4), 0, 0, self._px(11))
        self.lbl_ampm.setStyleSheet("color: transparent; background: transparent;")

        date = QFont()
        date.setFamilies(["Microsoft YaHei UI", "Segoe UI"])
        date.setPixelSize(self._px(12))
        date.setWeight(QFont.Weight.Normal)
        date.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.8 * s)
        self.lbl_date.setFont(date)
        self.lbl_date.setStyleSheet("color: transparent; background: transparent;")

        # 农历单独一行（在日期下面），占的是日期下面那块空白，不加卡片高度
        note = QFont()
        note.setFamilies(["Microsoft YaHei UI", "Segoe UI"])
        note.setPixelSize(self._px(11))
        note.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.4 * s)
        self.lbl_lunar.setFont(note)
        self.lbl_lunar.setStyleSheet("color: transparent; background: transparent;")
        self.lbl_lunar.setVisible(bool(getattr(self.card, "lunar", True)))

        self._label_draws = [
            (self.lbl_hm, QColor(30, 36, 48, 252)),
            (self.lbl_sec, QColor(40, 48, 62, 236)),
            (self.lbl_ampm, QColor(52, 62, 80, 232)),
            (self.lbl_date, QColor(40, 48, 62, 244)),
            (self.lbl_lunar, QColor(52, 62, 80, 232)),
        ]
        # 布局算一遍把矩形定下来（label 自己画的是透明字，实际文字见 paint_content）
        self.layout().activate()

    def _show_weather(self) -> bool:
        return bool(getattr(self.card, "weather", False))

    def _weather_rect(self) -> QRectF:
        """卡片右边的天气块（玻璃坐标）；没开天气就是空矩形。"""
        if not self._show_weather():
            return QRectF()
        _cw, ch = self._glass_size()
        w = float(getattr(self, "_wblock_w", 0) or 0)
        x = float(getattr(self, "_wblock_x", 0) or 0)
        if w <= 0:
            return QRectF()
        return QRectF(x, 0.0, w, float(ch))

    def _open_weather(self) -> None:
        cb = self.on_open_weather
        if cb is not None:
            try:
                cb(self)
            except (RuntimeError, TypeError):
                pass

    def _refresh_note(self) -> None:
        # 便签已经不用了，留着这个空方法兼容旧调用
        return

    # ---------- 绘制 ----------
    def paint_content(self, p: QPainter) -> None:
        face = self._face()
        if face != "digital":
            self._paint_face(p, face)
        # 开了天气时，时钟文字只占天气块左边那块，不许压过去
        clip = self._weather_rect()
        limit = (clip.left() - self._px(14)) if not clip.isEmpty() else 1e9
        for lbl, color in self._label_draws:
            if not lbl.isVisible():
                continue
            text = lbl.text()
            if not text.strip():
                continue
            mg = lbl.contentsMargins()
            g = lbl.geometry()
            r = QRectF(g.x() - self.inset + mg.left(),
                       g.y() - self.inset + mg.top(),
                       max(0.0, g.width() - mg.left() - mg.right()),
                       max(0.0, g.height() - mg.top() - mg.bottom()))
            if r.left() < limit and r.right() > limit:
                r.setRight(limit)
            p.setFont(lbl.font())
            p.setPen(color)
            p.drawText(r, int(lbl.alignment()), text)
        if self._show_weather():
            self._paint_weather(p)

    # ---------- 卡片右边的天气块 ----------
    def _paint_weather(self, p: QPainter) -> None:
        rect = self._weather_rect()
        if rect.isEmpty():
            return
        ch = rect.height()
        # 和时钟之间一条淡淡的分隔线
        x_line = rect.left() - self._px(10)
        p.setPen(QPen(QColor(28, 42, 64, 22), 1.0))
        p.drawLine(QPointF(x_line, 26.0), QPointF(x_line, ch - 26.0))

        data = self.weather_data
        icon = data.icon if data is not None else "cloud"
        block_h = self._px(92)
        top = max(self._px(30), (ch - block_h) / 2.0)
        bx = rect.left()
        ww = rect.width()
        weather_icons.paint_icon(
            p, QRectF(bx, top + self._px(2), self._px(30), self._px(30)), icon)

        temp_font = QFont()
        temp_font.setFamilies(["Segoe UI Variable Display", "Segoe UI"])
        temp_font.setPixelSize(self._px(21))
        temp_font.setWeight(QFont.Weight.Light)
        p.setFont(temp_font)
        p.setPen(QColor(30, 36, 48, 250))
        temp = "—" if data is None or data.temp is None else f"{round(data.temp)}°"
        p.drawText(QRectF(bx + self._px(33), top - self._px(4),
                          max(10.0, ww - self._px(33)), self._px(38)),
                   int(Qt.AlignLeft | Qt.AlignVCenter), temp)

        note = QFont()
        note.setFamilies(["Microsoft YaHei UI", "Segoe UI"])
        note.setPixelSize(self._px(12))
        p.setFont(note)
        p.setPen(QColor(40, 48, 62, 242))
        what = "" if data is None else data.label
        p.drawText(QRectF(bx, top + self._px(40), ww, self._px(18)),
                   int(Qt.AlignLeft | Qt.AlignVCenter), what)

        small = QFont()
        small.setFamilies(["Microsoft YaHei UI", "Segoe UI"])
        small.setPixelSize(self._px(10))
        p.setFont(small)
        p.setPen(QColor(52, 62, 80, 232))
        if data is not None and data.high is not None and data.low is not None:
            p.drawText(QRectF(bx, top + self._px(58), ww, self._px(16)),
                       int(Qt.AlignLeft | Qt.AlignVCenter),
                       f"{round(data.low)}° / {round(data.high)}°")
        city = (data.city if data is not None else "") or \
            str(getattr(self.card, "city", "") or "")
        p.drawText(QRectF(bx, top + self._px(74), ww, self._px(16)),
                   int(Qt.AlignLeft | Qt.AlignVCenter), city)

    # ---------- 三种表盘 ----------
    def _time_rect(self) -> QRectF:
        """时间那一格（玻璃坐标）：翻页/指针脸就往这里画。

        注意宽度只到时时钟区块：右边那块是天气，别把表盘画过去了。
        """
        g = self.lbl_hm.geometry()
        width = float(self.clock_w)
        clip = self._weather_rect()
        if not clip.isEmpty():
            # 开着天气：表盘只占了时钟这半边，别压到天气块上
            width = max(60.0, clip.left() - self._px(14))
        return QRectF(0.0, float(g.y() - self.inset), width, float(g.height()))

    def _hour_min(self) -> tuple[str, str, str]:
        """返回 (时, 分, 上午/下午)；24 小时制时第三项是空串。"""
        dt = self._now()
        t = dt.time()
        if bool(getattr(self.card, "hour24", True)):
            return f"{t.hour():02d}", f"{t.minute():02d}", ""
        hour = t.hour() % 12 or 12
        return f"{hour}", f"{t.minute():02d}", "上午" if t.hour() < 12 else "下午"

    def _paint_face(self, p: QPainter, face: str) -> None:
        rect = self._time_rect()
        if rect.height() <= 4 or rect.width() <= 4:
            return
        hh, mm, ap = self._hour_min()
        if face == "flip":
            self._paint_flip(p, rect, hh, mm, ap)
        else:
            self._paint_analog(p, rect, ap)

    def _paint_flip(self, p: QPainter, rect: QRectF, hh: str, mm: str,
                    ap: str) -> None:
        """翻页钟：两块深色牌 + 中间一道缝，白字。"""
        pad = self._px(4)
        gap = max(4.0, self._px(14))
        h = min(rect.height() - pad * 2, self._px(96))
        h = max(24.0, h)
        w = max(40.0, min(h * 0.92, (rect.width() - gap) / 2.0 - self._px(6)))
        h = min(h, w / 0.92)          # 牌面按宽高比缩，不会被拉成长条
        total = w * 2 + gap
        x0 = rect.x() + (rect.width() - total) / 2.0
        y0 = rect.y() + (rect.height() - h) / 2.0
        families, weight = FONTS.get(str(getattr(self.card, "font", "segoe") or "segoe"),
                                     FONTS["segoe"])
        digit = QFont()
        digit.setFamilies(list(families))
        digit.setPixelSize(max(12, int(round(h * 0.62))))
        digit.setWeight(QFont.Weight.Light if weight == "light"
                        else QFont.Weight.Normal)
        plate = QColor(52, 62, 82, 186)
        text_ink = QColor(255, 255, 255, 244)
        for i, text in enumerate((hh, mm)):
            x = x0 + i * (w + gap)
            r = QRectF(x, y0, w, h)
            p.setPen(Qt.NoPen)
            p.setBrush(plate)
            p.drawRoundedRect(r, h * 0.14, h * 0.14)
            # 中间那道「翻页」的缝
            p.setPen(QPen(QColor(255, 255, 255, 26), 1.0))
            p.drawLine(QPointF(r.left() + 2, r.center().y()),
                       QPointF(r.right() - 2, r.center().y()))
            p.setFont(digit)
            p.setPen(text_ink)
            p.drawText(r, int(Qt.AlignCenter), text)
        if ap:
            note = QFont()
            note.setFamilies(["Microsoft YaHei UI", "Segoe UI"])
            note.setPixelSize(max(9, self._px(11)))
            p.setFont(note)
            p.setPen(QColor(52, 62, 80, 232))
            p.drawText(QRectF(x0, y0 + h, total, max(10.0, rect.height() - h - pad)),
                       int(Qt.AlignHCenter | Qt.AlignTop), ap)

    def _paint_analog(self, p: QPainter, rect: QRectF, ap: str) -> None:
        """极简指针：一圈刻度 + 三根针。"""
        size = min(rect.width(), rect.height()) - self._px(6)
        if size < 24:
            return
        c = QPointF(rect.center().x(), rect.center().y())
        r = size / 2.0
        ink = QColor(30, 36, 48, 252)
        faint = QColor(58, 70, 92, 110)
        # 表盘外圈
        p.setBrush(QColor(255, 255, 255, 60))
        p.setPen(QPen(faint, max(1.0, r * 0.012)))
        p.drawEllipse(c, r, r)
        # 12 个刻度（3/6/9/12 长一点）
        for i in range(12):
            ang = math.radians(i * 30 - 90)
            long_tick = i % 3 == 0
            r1 = r - (r * 0.10 if long_tick else r * 0.06)
            r0 = r - r * 0.02
            p.setPen(QPen(ink, max(1.4, r * (0.028 if long_tick else 0.016)),
                          Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(c.x() + math.cos(ang) * r0, c.y() + math.sin(ang) * r0),
                       QPointF(c.x() + math.cos(ang) * r1, c.y() + math.sin(ang) * r1))
        dt = self._now()
        t = dt.time()
        sec = t.second() + t.msec() / 1000.0
        minute = t.minute() + sec / 60.0
        hour = (t.hour() % 12) + minute / 60.0
        hands = (
            (hour * 30.0, r * 0.50, max(2.0, r * 0.055), ink),
            (minute * 6.0, r * 0.74, max(1.6, r * 0.040), ink),
            (sec * 6.0, r * 0.82, max(1.0, r * 0.016), QColor(58, 70, 92, 155)),
        )
        for deg, length, width, color in hands:
            ang = math.radians(deg - 90)
            p.setPen(QPen(color, width, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(c, QPointF(c.x() + math.cos(ang) * length,
                                  c.y() + math.sin(ang) * length))
        p.setPen(Qt.NoPen)
        p.setBrush(ink)
        p.drawEllipse(c, max(1.6, r * 0.035), max(1.6, r * 0.035))
        if ap:
            note = QFont()
            note.setFamilies(["Microsoft YaHei UI", "Segoe UI"])
            note.setPixelSize(max(9, self._px(11)))
            p.setFont(note)
            p.setPen(QColor(52, 62, 80, 232))
            p.drawText(QRectF(c.x() - r * 0.5, c.y() + r * 0.28, r, r * 0.4),
                       int(Qt.AlignHCenter | Qt.AlignTop), ap)

    # ---------- 点击 ----------
    def hit_test(self, glass: QPointF):
        rect = self._weather_rect()
        if not rect.isEmpty() and rect.contains(glass):
            return "weather"
        return None

    def clicked(self, token) -> None:
        if token == "weather":
            self._open_weather()

    # ---------- 内容 ----------
    def _now(self) -> "QDateTime":
        """当前时间；卡片选了时区就换算过去，没选就用本机时间。"""
        dt = QDateTime.currentDateTime()
        tz_id = (getattr(self.card, "timezone", "") or "").strip()
        if tz_id:
            tz = QTimeZone(tz_id.encode())
            if tz.isValid():
                dt = dt.toTimeZone(tz)
        return dt

    def _tick(self) -> None:
        dt = self._now()
        t = dt.time()
        d = dt.date()
        face = self._face()
        hour24 = bool(getattr(self.card, "hour24", True))
        if hour24:
            hm = f"{t.hour():02d}:{t.minute():02d}"
            ap = ""
        else:
            hm = f"{t.hour() % 12 or 12}:{t.minute():02d}"
            ap = "上午" if t.hour() < 12 else "下午"
        sec = f"{t.second():02d}"
        date = f"{d.month()}月{d.day()}日  {WEEKDAYS[d.dayOfWeek() - 1]}"
        lunar = ""
        if getattr(self.card, "lunar", True):
            lunar = lunar_mod.lunar_line(d.toPython())
        if face != "digital":
            # 翻页/指针脸：数字和秒都不画字，交给 paint_content，但要继续刷新
            self.lbl_hm.setText("")
            self.lbl_sec.setText("")
            self.lbl_ampm.setText("")
            self._hm_text = ""
            self._sec_text = ""
            self._ampm_text = ""
            if date != self._date_text:
                self._date_text = date
                self.lbl_date.setText(date)
            if lunar != self._lunar_text:
                self._lunar_text = lunar
                self.lbl_lunar.setText(lunar)
            self.update()
            return
        if hm != self._hm_text:
            self._hm_text = hm
            self.lbl_hm.setText(hm)
        if sec != self._sec_text:
            self._sec_text = sec
            self.lbl_sec.setText(sec if self.card.show_seconds else "")
        if ap != self._ampm_text:
            self._ampm_text = ap
            self.lbl_ampm.setText(ap)
        if date != self._date_text:
            self._date_text = date
            self.lbl_date.setText(date)
        if lunar != self._lunar_text:
            self._lunar_text = lunar
            self.lbl_lunar.setText(lunar)

    # ---------- 设置窗口用的 ----------
    def set_seconds(self, on: bool) -> None:
        self.card.show_seconds = bool(on)
        self._sec_text = ""
        self._tick()
        self._on_change()

    def set_timezone(self, tz_id: str) -> None:
        """换时区（IANA id）；空字符串 = 跟随本机时区。"""
        self.card.timezone = (tz_id or "").strip()
        self._hm_text = ""
        self._sec_text = ""
        self._date_text = ""
        self._tick()
        self._on_change()

    def set_hour24(self, on: bool) -> None:
        """24 小时制 / 12 小时制。"""
        self.card.hour24 = bool(on)
        self._hm_text = ""
        self._ampm_text = ""
        self._tick()
        self.update()
        self._on_change()

    def set_lunar(self, on: bool) -> None:
        self.card.lunar = bool(on)
        self._apply_style()
        self._tick()
        self.update()
        self._on_change()

    def set_face(self, face: str) -> None:
        """表盘样式：digital / flip / analog。"""
        face = face if face in ("digital", "flip", "analog") else "digital"
        self.card.face = face
        self._hm_text = ""
        self._sec_text = ""
        self._ampm_text = ""
        self._apply_style()
        self._clamp()
        self._tick()
        self.update()
        self._on_change()

    def set_font(self, key: str) -> None:
        self.card.font = key if key in FONTS else "segoe"
        self._apply_style()
        self._clamp()
        self.update()
        self._on_change()

    def set_weather(self, on: bool) -> None:
        self.card.weather = bool(on)
        self._apply_style()
        self._clamp()
        self.update()
        self._on_change()

    def set_city(self, city: str) -> None:
        self.card.city = str(city or "北京")
        self.update()
        self._on_change()

    def set_weather_data(self, data) -> None:
        self.weather_data = data
        self.update()

    def set_note(self, text: str) -> None:
        self.card.note = text.strip()
        self._refresh_note()
        self._on_change()
