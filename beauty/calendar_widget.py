"""日历卡片：一个月一屏，今天圈出来，有事的格子点个小点。

点上面的 ‹ › 或滚轮翻月，点标题回到本月；点某一天在下面一行显示那天的农历和
安排，双击某一天直接给那天加一条待办。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen

from . import lunar as lunar_mod
from .agenda import WEEKDAYS, lunar_of
from .card_base import ACCENT, INK, INK_FAINT, INK_NOTE, INK_SOFT, CardWindow, card_font, draw_text
from .config import CardData

PAD = 24.0
HEAD_TOP = 12.0
HEAD_H = 28.0
WEEK_TOP = 44.0
WEEK_H = 16.0
GRID_TOP = 64.0
ROW_H = 27.0
FOOT_TOP = 232.0
FOOT_H = 16.0


class CalendarWidget(CardWindow):
    DESIGN_W = 302.0
    DESIGN_H = 262.0

    def __init__(self, card: CardData, on_change: Callable[[], None], agenda,
                 radius: float = 10):
        self.agenda = agenda
        today = date.today()
        # 显示的是哪个月（用当月 1 号表示）和选中了哪一天
        self._cursor = date(today.year, today.month, 1)
        self._selected = today
        self._hover_day: date | None = None
        super().__init__(card, on_change, radius)

    def build_body(self) -> None:
        self.setMouseTracking(True)

    # ---------- 尺寸 ----------
    def _monday_first(self) -> bool:
        return bool(getattr(self.card, "cal_monday", True))

    def _lunar_labels(self) -> bool:
        return bool(getattr(self.card, "cal_lunar", True))

    def _grid_width(self) -> float:
        w, _h = self._glass_size()
        return w - PAD * 2

    def _cell_w(self) -> float:
        return self._grid_width() / 7.0

    def _cell_rect(self, index: int) -> QRectF:
        """index 0..41：按 6 行 × 7 列从左到右、从上到下。"""
        col = index % 7
        row = index // 7
        return QRectF(PAD + col * self._cell_w(), GRID_TOP + row * ROW_H,
                      self._cell_w(), ROW_H)

    def _header_rect(self) -> QRectF:
        w, _h = self._glass_size()
        return QRectF(PAD, HEAD_TOP, w - PAD * 2, HEAD_H)

    def _arrow_rects(self) -> tuple[QRectF, QRectF]:
        h = self._header_rect()
        w = h.width()
        return (QRectF(h.left(), h.top() + (h.height() - 20) / 2.0, 22, 20),
                QRectF(h.right() - 22, h.top() + (h.height() - 20) / 2.0, 22, 20))

    # ---------- 日期 ----------
    def _days(self) -> list[date]:
        first = self._cursor
        offset = first.weekday() if self._monday_first() else (first.weekday() + 1) % 7
        start = first - timedelta(days=offset)
        return [start + timedelta(days=i) for i in range(42)]

    def _month_text(self) -> str:
        return f"{self._cursor.year} 年 {self._cursor.month} 月"

    def _go(self, delta: int) -> None:
        y, m = self._cursor.year, self._cursor.month + delta
        while m < 1:
            y, m = y - 1, m + 12
        while m > 12:
            y, m = y + 1, m - 12
        self._cursor = date(y, m, 1)
        if (self._selected.year, self._selected.month) != (y, m):
            self._selected = self._cursor
        self.update()

    def _go_today(self) -> None:
        today = date.today()
        self._cursor = date(today.year, today.month, 1)
        self._selected = today
        self.update()

    # ---------- 绘制 ----------
    def paint_content(self, p: QPainter) -> None:
        w, _h = self._glass_size()
        today = date.today()

        # 标题 + 两个箭头
        draw_text(p, QRectF(PAD + 22, HEAD_TOP, w - PAD * 2 - 44, HEAD_H),
                  self._month_text(), card_font(self._px(15), QFont.Weight.Medium),
                  INK, Qt.AlignHCenter | Qt.AlignVCenter, elide=False)
        left, right = self._arrow_rects()
        self._paint_chevron(p, left, 1)
        self._paint_chevron(p, right, -1)

        # 星期
        names = ("一", "二", "三", "四", "五", "六", "日") if self._monday_first() \
            else ("日", "一", "二", "三", "四", "五", "六")
        cw = self._cell_w()
        for i, name in enumerate(names):
            draw_text(p, QRectF(PAD + i * cw, WEEK_TOP, cw, WEEK_H), name,
                      card_font(self._px(10)), INK_NOTE,
                      Qt.AlignHCenter | Qt.AlignVCenter, elide=False)

        # 42 个格子
        for i, day in enumerate(self._days()):
            self._paint_day(p, i, day, today)

        # 底下那行：选中那天的农历 / 待办 / 纪念日
        self._paint_footer(p, today)

    def _paint_chevron(self, p: QPainter, rect: QRectF, direction: int) -> None:
        c = rect.center()
        p.setPen(QPen(QColor(84, 96, 118, 170), max(1.0, self._scale(1.5)),
                      Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        dx = self._scale(3.6) * direction
        dy = self._scale(5.2)
        p.drawLine(QPointF(c.x() + dx, c.y() - dy), QPointF(c.x() - dx, c.y()))
        p.drawLine(QPointF(c.x() - dx, c.y()), QPointF(c.x() + dx, c.y() + dy))

    def _paint_day(self, p: QPainter, index: int, day: date, today: date) -> None:
        rect = self._cell_rect(index)
        cx = rect.center().x()
        in_month = (day.year, day.month) == (self._cursor.year, self._cursor.month)
        selected = day == self._selected
        is_today = day == today
        number_color = INK if in_month else INK_FAINT
        if is_today and not selected:
            number_color = ACCENT

        r = self._scale(10.0)
        cy = rect.top() + self._scale(10.5)
        # 选中那天：蓝底白字；今天：蓝圈
        if selected:
            p.setPen(Qt.NoPen)
            p.setBrush(ACCENT)
            p.drawEllipse(QPointF(cx, cy), r, r)
            number_color = QColor(255, 255, 255, 248)
        elif is_today:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(91, 141, 239, 170), max(1.0, self._scale(1.2))))
            p.drawEllipse(QPointF(cx, cy), r, r)

        draw_text(p, QRectF(rect.left(), rect.top() + self._scale(1),
                            rect.width(), self._scale(19)),
                  str(day.day), card_font(self._px(12)),
                  number_color, Qt.AlignHCenter | Qt.AlignVCenter, elide=False)

        # 第二行小字：今天 / 节气 / 初一（农历月名）；选中的那天不写，免得压住圆圈
        sub = ""
        if not selected:
            if is_today:
                sub = "今天"
            elif self._lunar_labels():
                term = lunar_mod.solar_term(day)
                got = lunar_mod.to_lunar(day)
                if term:
                    sub = term
                elif got is not None and got[2] == 1:
                    sub = lunar_mod.month_day_text(got[1], 1)
        has_event = bool(self.agenda.events_on(day))
        if sub:
            draw_text(p, QRectF(rect.left(), rect.top() + self._scale(17.5),
                                rect.width(), self._scale(9.5)),
                      sub, card_font(self._scale(8)), INK_NOTE,
                      Qt.AlignHCenter | Qt.AlignVCenter, elide=False)
        if has_event:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 220) if selected
                       else QColor(212, 86, 86, 205))
            dot_x = rect.right() - self._scale(6) if sub else cx
            p.drawEllipse(QPointF(dot_x, rect.top() + self._scale(22)),
                          self._scale(1.7), self._scale(1.7))

    def _paint_footer(self, p: QPainter, today: date) -> None:
        w, _h = self._glass_size()
        day = self._selected
        text = f"{day.month}月{day.day}日 {WEEKDAYS[day.weekday()]}"
        lunar = lunar_of(day)
        if lunar:
            text += f" · {lunar}"
        events = self.agenda.events_on(day)
        todos = self.agenda.todos_on(day)
        if events:
            text += f" · {events[0].title}"
        if todos:
            text += f" · {len(todos)} 项待办"
        draw_text(p, QRectF(PAD, FOOT_TOP, w - PAD * 2, FOOT_H), text,
                  card_font(self._px(10.5)), INK_SOFT,
                  Qt.AlignLeft | Qt.AlignVCenter)

    # ---------- 交互 ----------
    def _day_at(self, glass: QPointF) -> date | None:
        for i, day in enumerate(self._days()):
            if self._cell_rect(i).contains(glass):
                return day
        return None

    def hit_test(self, glass: QPointF):
        left, right = self._arrow_rects()
        if left.contains(glass):
            return ("nav", -1)
        if right.contains(glass):
            return ("nav", 1)
        if self._header_rect().contains(glass):
            return ("today",)
        day = self._day_at(glass)
        if day is not None:
            return ("day", day)
        return None

    def clicked(self, token) -> None:
        kind = token[0]
        if kind == "nav":
            self._go(int(token[1]))
        elif kind == "today":
            self._go_today()
        elif kind == "day":
            day = token[1]
            self._selected = day
            if (day.year, day.month) != (self._cursor.year, self._cursor.month):
                self._cursor = date(day.year, day.month, 1)
            self.update()

    def double_click(self, glass: QPointF) -> bool:
        day = self._day_at(glass)
        if day is None:
            return False
        from .picker import GlassInput
        text = GlassInput.prompt(f"{day.month}月{day.day}日 加一条待办", "", "", False, self)
        if text:
            self._selected = day
            self.agenda.add_todo(text, due=day.isoformat())
        return True

    def wheel_action(self, steps: int) -> bool:
        self._go(1 if steps > 0 else -1)
        return True

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(event)
        if self._window_dragging:
            return      # 拖窗口时显示的是快照，算哪一格被悬停没意义
        day = self._day_at(self._glass_pos(event))
        if day != self._hover_day:
            self._hover_day = day
            self.update()

    def leaveEvent(self, event) -> None:
        self._hover_day = None
        super().leaveEvent(event)

    # ---------- 数据 ----------
    def refresh_agenda(self) -> None:
        self.update()

    def set_monday_first(self, on: bool) -> None:
        self.card.cal_monday = bool(on)
        self.update()
        self._on_change()

    def set_lunar_labels(self, on: bool) -> None:
        self.card.cal_lunar = bool(on)
        self.update()
        self._on_change()

    def reset_month(self) -> None:
        self._go_today()
