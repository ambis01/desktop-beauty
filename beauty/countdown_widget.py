"""倒计时 / 纪念日卡片：离最近的那个纪念日还有多少天。

一张卡一次只显示「最近的一个」，点卡片看成列表、加新的（双击也能加）。
数据在 agenda.Agenda 里，和日历卡片共用同一批纪念日。
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen

from .agenda import lunar_of, solar_text
from .card_base import ACCENT, INK, INK_NOTE, INK_SOFT, CardWindow, card_font, draw_text
from .config import CardData

PAD = 24.0


class CountdownWidget(CardWindow):
    DESIGN_W = 302.0
    DESIGN_H = 152.0

    def __init__(self, card: CardData, on_change: Callable[[], None], agenda,
                 radius: float = 10):
        self.agenda = agenda
        self._hover_add = False
        super().__init__(card, on_change, radius)

    def build_body(self) -> None:
        self.setMouseTracking(True)

    # ---------- 尺寸 ----------
    def _add_rect(self) -> QRectF:
        w, _h = self._glass_size()
        return QRectF(w - PAD - 24 - 26, 11, 26, 26)

    def _number_rect(self) -> QRectF:
        _w, _h = self._glass_size()
        return QRectF(PAD, 26, 148, 72)

    # ---------- 数据 ----------
    def _entry(self):
        """(事件, 那天, 还有几天)；没有纪念日返回 None。"""
        rows = self.agenda.upcoming()
        if not rows:
            return None
        pin = str(getattr(self.card, "countdown_pin", "") or "")
        if pin:
            for row in rows:
                if row[0].id == pin:
                    return row
        return rows[0]

    # ---------- 绘制 ----------
    def paint_content(self, p: QPainter) -> None:
        w, _h = self._glass_size()
        draw_text(p, QRectF(PAD, 15, 140, 14), "倒计时",
                  card_font(self._px(10), spacing=1.6), INK_NOTE,
                  Qt.AlignLeft | Qt.AlignVCenter, elide=False)
        self._paint_add(p)

        entry = self._entry()
        if entry is None:
            draw_text(p, QRectF(PAD, 46, w - PAD * 2, 30), "还没有纪念日",
                      card_font(self._px(13)), INK_NOTE,
                      Qt.AlignLeft | Qt.AlignVCenter, elide=False)
            return

        event, when, days = entry
        # 大数字：还剩几天；今天就直接写「今天」
        if days <= 0:
            num, unit, color = "今天", "", ACCENT
        else:
            num, unit, color = str(days), "天", INK
        size = self._px(30) if days <= 0 else self._px(52)
        font = card_font(size, QFont.Weight.Light,
                         families=("Segoe UI Variable Display", "Segoe UI"))
        nr = self._number_rect()
        draw_text(p, nr, num, font, color, Qt.AlignLeft | Qt.AlignBottom, elide=False)
        if unit:
            width = QFontMetricsF(font).horizontalAdvance(num)
            uf = card_font(self._px(13), QFont.Weight.Medium)
            draw_text(p, QRectF(nr.left() + width + self._scale(5), nr.top(),
                                self._scale(34), nr.height()),
                      unit, uf, INK_SOFT, Qt.AlignLeft | Qt.AlignBottom, elide=False)

        # 右边：后面还排着哪几个纪念日（填掉那块空地）
        rows = self.agenda.upcoming()
        others = [row for row in rows if row[0].id != event.id][:3]
        if others:
            self._paint_upcoming(p, others)

        title = f"距离 {event.title}" if days > 0 else event.title
        draw_text(p, QRectF(PAD, 100, w - PAD * 2, 20), title,
                  card_font(self._px(13)), INK_SOFT,
                  Qt.AlignLeft | Qt.AlignVCenter)

        line = solar_text(when)
        lunar = lunar_of(when)
        if lunar:
            line += f" · {lunar}"
        draw_text(p, QRectF(PAD, 120, w - PAD * 2, 16), line,
                  card_font(self._px(10.5)), INK_NOTE,
                  Qt.AlignLeft | Qt.AlignVCenter)

    def _paint_upcoming(self, p: QPainter, rows) -> None:
        w, _h = self._glass_size()
        left = w - PAD - 96
        for i, (event, _when, days) in enumerate(rows):
            y = 40 + i * self._scale(21)
            draw_text(p, QRectF(left, y, 60, 16), event.title,
                      card_font(self._px(10.5)), INK_NOTE,
                      Qt.AlignLeft | Qt.AlignVCenter)
            draw_text(p, QRectF(left + 60, y, 36, 16),
                      "今天" if days <= 0 else f"{days} 天",
                      card_font(self._px(10.5)), INK_SOFT,
                      Qt.AlignRight | Qt.AlignVCenter, elide=False)

    def _paint_add(self, p: QPainter) -> None:
        r = self._add_rect()
        c = r.center()
        if self._hover_add:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 150))
            p.drawEllipse(c, r.width() / 2.0, r.height() / 2.0)
        p.setPen(QPen(QColor(84, 96, 118, 220), max(1.2, self._scale(1.5)),
                      Qt.SolidLine, Qt.RoundCap))
        arm = r.width() * 0.22
        p.drawLine(QPointF(c.x() - arm, c.y()), QPointF(c.x() + arm, c.y()))
        p.drawLine(QPointF(c.x(), c.y() - arm), QPointF(c.x(), c.y() + arm))

    # ---------- 交互 ----------
    def hit_test(self, glass: QPointF):
        if self._add_rect().contains(glass):
            return ("add",)
        return ("open",)

    def clicked(self, token) -> None:
        if token[0] == "add":
            self.add_event()
        else:
            self.open_dialog()

    def double_click(self, glass: QPointF) -> bool:
        if self._add_rect().contains(glass):
            return False
        self.add_event()
        return True

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(event)
        if self._window_dragging:
            return      # 拖窗口时显示的是快照，算「鼠标在不在 + 上」没意义
        hover = self._add_rect().contains(self._glass_pos(event))
        if hover != self._hover_add:
            self._hover_add = hover
            self.update()

    def leaveEvent(self, event) -> None:
        self._hover_add = False
        super().leaveEvent(event)

    # ---------- 数据 ----------
    def refresh_agenda(self) -> None:
        self.update()

    def add_event(self) -> None:
        from .agenda_dialog import EventDialog
        if EventDialog.edit(self.agenda, None, False, self):
            self.update()

    def open_dialog(self) -> None:
        from .agenda_dialog import CountdownDialog
        CountdownDialog.show_for(self.agenda, False, self)

    def set_pin(self, event_id: str) -> None:
        self.card.countdown_pin = str(event_id or "")
        self.update()
        self._on_change()
