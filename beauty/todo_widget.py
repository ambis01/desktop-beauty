"""待办 / 日程卡片：抬头 + 几条待办，点圆点打勾、点文字改、双击空白加一条。

数据在 agenda.Agenda 里（和「待办」详情窗、日历卡共用同一份），这里只负责画
和点。行数是固定的（6 行），多了就显示「还有 N 项」，卡片高度永远不变。
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QCursor, QFont, QPainter, QPainterPath, QPen

from .card_base import ACCENT, INK, INK_FAINT, INK_NOTE, INK_SOFT, RED, CardWindow, card_font, draw_text
from .config import CardData
from .glass import GlassMenu

PAD = 24.0          # 左右内边距（和时钟卡片一致）
HEAD_TOP = 15.0
HEAD_H = 22.0
ROW_TOP = 44.0
ROW_H = 24.0
ROWS = 6            # 固定显示 6 行，多的走「还有 N 项」
UNIT_W = 58.0       # 右边到期日那一栏的宽度


class TodoWidget(CardWindow):
    DESIGN_W = 302.0
    DESIGN_H = 200.0

    def __init__(self, card: CardData, on_change: Callable[[], None], agenda,
                 radius: float = 10):
        self.agenda = agenda
        self._rows: list = []           # [(kind, payload, QRectF)]，画的时候算
        self._hover_row = -1
        self._hover_add = False
        super().__init__(card, on_change, radius)

    def build_body(self) -> None:
        self.setMouseTracking(True)

    # ---------- 尺寸 ----------
    def _show_done(self) -> bool:
        return bool(getattr(self.card, "todo_show_done", False))

    def _add_rect(self) -> QRectF:
        w, _h = self._glass_size()
        return QRectF(w - PAD - 24 - 26, HEAD_TOP - 4, 26, 26)

    def _row_rect(self, index: int) -> QRectF:
        w, _h = self._glass_size()
        return QRectF(PAD - 8, ROW_TOP + index * ROW_H,
                      w - PAD * 2 + 16, ROW_H)

    def _check_rect(self, index: int) -> QRectF:
        r = self._row_rect(index)
        return QRectF(PAD - 6, r.top() + (ROW_H - 20) / 2.0, 20, 20)

    # ---------- 行 ----------
    def _layout_rows(self) -> list:
        """把当前待办排成 6 行：多的收成「还有 N 项」。"""
        items = self.agenda.ordered_todos(self._show_done())
        rows: list = []
        shown = items[:ROWS]
        if len(items) > ROWS:
            shown = items[:ROWS - 1]
        for i, todo in enumerate(shown):
            rows.append(("todo", todo, self._row_rect(i)))
        if len(items) > ROWS:
            rows.append(("more", len(items) - len(shown), self._row_rect(len(shown))))
        self._rows = rows
        return rows

    def _row_index_at(self, glass: QPointF) -> int:
        for i, (_kind, _payload, rect) in enumerate(self._rows):
            if rect.contains(glass):
                return i
        return -1

    def _row_of(self, todo_id: str):
        for kind, payload, _rect in self._rows:
            if kind == "todo" and payload.id == todo_id:
                return payload
        return None

    # ---------- 绘制 ----------
    def paint_content(self, p: QPainter) -> None:
        w, _h = self._glass_size()
        rows = self._layout_rows()

        # 抬头：待办 / 未完成数 / 加号
        draw_text(p, QRectF(PAD, HEAD_TOP, 120, HEAD_H), "待办",
                  card_font(self._px(13), QFont.Weight.Medium), INK_SOFT,
                  Qt.AlignLeft | Qt.AlignVCenter, elide=False)
        if self.agenda.todos:
            opening = self.agenda.open_count()
            label = "全部完成" if opening == 0 else f"{opening} 项"
            left = PAD + 44
            count = QRectF(left, HEAD_TOP,
                           max(0.0, self._add_rect().left() - 8 - left), HEAD_H)
            draw_text(p, count, label, card_font(self._px(11)), INK_NOTE,
                      Qt.AlignRight | Qt.AlignVCenter, elide=False)
        self._paint_add(p)

        if not rows:
            done = bool(self.agenda.todos)
            draw_text(p, QRectF(PAD, ROW_TOP, w - PAD * 2, ROW_H * 2),
                      "都完成了" if done else "还没有待办",
                      card_font(self._px(12)), INK_NOTE,
                      Qt.AlignLeft | Qt.AlignTop, elide=False)
            return

        for i, (kind, payload, rect) in enumerate(rows):
            if kind == "more":
                self._paint_more(p, payload, rect)
            else:
                self._paint_todo(p, payload, rect, i == self._hover_row)

    def _paint_add(self, p: QPainter) -> None:
        r = self._add_rect()
        c = r.center()
        if self._hover_add:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 150))
            p.drawEllipse(c, r.width() / 2.0, r.height() / 2.0)
        pen = QPen(QColor(84, 96, 118, 220), max(1.2, self._scale(1.5)),
                   Qt.SolidLine, Qt.RoundCap)
        p.setPen(pen)
        arm = r.width() * 0.22
        p.drawLine(QPointF(c.x() - arm, c.y()), QPointF(c.x() + arm, c.y()))
        p.drawLine(QPointF(c.x(), c.y() - arm), QPointF(c.x(), c.y() + arm))

    def _paint_todo(self, p: QPainter, todo, rect: QRectF, hover: bool) -> None:
        if hover:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 120))
            p.drawRoundedRect(rect, self._px(9), self._px(9))
        cy = rect.center().y()
        # 左边那个勾选圆圈
        if todo.done:
            p.setPen(Qt.NoPen)
            p.setBrush(ACCENT)
            p.drawEllipse(QPointF(PAD + 6, cy), self._scale(6.8), self._scale(6.8))
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(255, 255, 255, 240), max(1.0, self._scale(1.4)),
                          Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            path = QPainterPath()
            p0 = QPointF(PAD + 2.6, cy + 0.2)
            path.moveTo(p0)
            path.lineTo(QPointF(PAD + 5.2, cy + self._scale(3)))
            path.lineTo(QPointF(PAD + 9.8, cy - self._scale(3.2)))
            p.drawPath(path)
        else:
            p.setBrush(QColor(255, 255, 255, 130))
            p.setPen(QPen(QColor(108, 124, 148, 180), max(0.9, self._scale(1.2))))
            p.drawEllipse(QPointF(PAD + 6, cy), self._scale(6.4), self._scale(6.4))

        # 右边到期日
        unit = todo.due_label()
        if unit:
            color = RED if todo.overdue() else (
                QColor(91, 141, 239, 235) if unit in ("今天", "明天", "后天")
                else INK_NOTE)
            draw_text(p, QRectF(rect.right() - UNIT_W - 8, rect.top(),
                                UNIT_W, ROW_H), unit,
                      card_font(self._px(11)), color,
                      Qt.AlignRight | Qt.AlignVCenter, elide=False)

        font = card_font(self._px(13))
        color = INK
        if todo.done:
            font.setStrikeOut(True)
            color = INK_FAINT
        draw_text(p, QRectF(PAD + 20, rect.top(),
                            rect.width() - 20 - UNIT_W - 16, ROW_H),
                  todo.text, font, color)

    def _paint_more(self, p: QPainter, count: int, rect: QRectF) -> None:
        if rect.top() > self._glass_size()[1] - 6:
            return
        draw_text(p, QRectF(PAD, rect.top(), rect.width(), ROW_H),
                  f"还有 {count} 项", card_font(self._px(11)), INK_NOTE,
                  Qt.AlignLeft | Qt.AlignVCenter, elide=False)

    # ---------- 交互 ----------
    def hit_test(self, glass: QPointF):
        if not self._rows:
            self._layout_rows()
        if self._add_rect().contains(glass):
            return ("add",)
        i = self._row_index_at(glass)
        if i < 0:
            return None
        kind, payload, rect = self._rows[i]
        if kind == "more":
            return ("more",)
        if self._check_rect(i).contains(glass):
            return ("check", payload.id)
        return ("edit", payload.id)

    def clicked(self, token) -> None:
        kind = token[0]
        if kind == "add":
            self.add_todo()
        elif kind == "check":
            self.agenda.toggle_todo(token[1])
        elif kind == "edit":
            self.edit_todo(token[1])
        elif kind == "more":
            self.open_dialog()

    def double_click(self, glass: QPointF) -> bool:
        """双击空白处：加一条。"""
        if self._add_rect().contains(glass) or self._row_index_at(glass) >= 0:
            return False
        self.add_todo()
        return True

    def context_menu(self, glass: QPointF) -> bool:
        i = self._row_index_at(glass)
        if i < 0 or self._rows[i][0] != "todo":
            return False
        todo = self._rows[i][1]
        menu = GlassMenu(self, light=True)
        menu.prepare_backdrop()
        act_done = menu.addAction("取消勾选" if todo.done else "标记完成")
        act_edit = menu.addAction("修改内容")
        menu.addSeparator()
        act_today = menu.addAction("今天到期")
        act_tomorrow = menu.addAction("明天到期")
        act_clear = menu.addAction("清除日期")
        if not todo.due:
            act_clear.setEnabled(False)
        menu.addSeparator()
        act_del = menu.addAction("删除")
        chosen = menu.exec(QCursor.pos())
        from datetime import date, timedelta
        today = date.today()
        if chosen is act_done:
            self.agenda.toggle_todo(todo.id)
        elif chosen is act_edit:
            self.edit_todo(todo.id)
        elif chosen is act_today:
            self.agenda.update_todo(todo.id, due=today.isoformat())
        elif chosen is act_tomorrow:
            self.agenda.update_todo(todo.id, due=(today + timedelta(days=1)).isoformat())
        elif chosen is act_clear:
            self.agenda.update_todo(todo.id, due="")
        elif chosen is act_del:
            self.agenda.remove_todo(todo.id)
        return True

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(event)
        if self._window_dragging:
            return      # 拖窗口时显示的是快照，算行悬停没意义，还会白重画
        pos = self._glass_pos(event)
        index = self._row_index_at(pos)
        hover_add = self._add_rect().contains(pos)
        if index != self._hover_row or hover_add != self._hover_add:
            self._hover_row = index
            self._hover_add = hover_add
            self.update()

    def leaveEvent(self, event) -> None:
        self._hover_row = -1
        self._hover_add = False
        super().leaveEvent(event)

    # ---------- 数据 ----------
    def refresh_agenda(self) -> None:
        self.update()

    def add_todo(self) -> None:
        from .picker import GlassInput
        text = GlassInput.prompt("添加待办", "", "", self._dark_input(), self)
        if text:
            self.agenda.add_todo(text)

    def edit_todo(self, todo_id: str) -> None:
        todo = self.agenda.todo(todo_id)
        if todo is None:
            return
        from .picker import GlassInput
        text = GlassInput.prompt("修改待办", "", todo.text, self._dark_input(), self)
        if text and text != todo.text:
            self.agenda.update_todo(todo_id, text=text)

    def open_dialog(self) -> None:
        from .agenda_dialog import TodoDialog
        TodoDialog.show_for(self.agenda, self._dark_input(), self)

    def set_show_done(self, on: bool) -> None:
        self.card.todo_show_done = bool(on)
        self.update()
        self._on_change()

    def _dark_input(self) -> bool:
        """卡片是白玻璃，弹出来的小窗也用浅色。"""
        return False
