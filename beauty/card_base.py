"""桌面卡片的共同外壳：拖动、悬停「弹一下」、改大小、右上角三个点、贴边吸附。

时钟 / 待办 / 日历 / 倒计时四种卡片从外面看是一回事：一块白玻璃，能拖、能
悬停放大、点右上角能改大小。这些逻辑原来都长在时钟卡片里，现在抽到这里，
子类只管三件事：

    build_body()        建自己的 QLabel / 布局
    _apply_style()      按 card.scale 算尺寸和字号（最后调 _set_size）
    paint_content(p)    往玻璃上画字（坐标系已经是玻璃左上角）

点击也统一走 hit_test / clicked：按下时 hit_test 给一个「令牌」，松手时如果
鼠标没怎么动就回调 clicked(令牌)。这样点条目和拖卡片不会打架。
"""
from __future__ import annotations

import time
from datetime import date
from typing import Callable

from PySide6.QtCore import (
    QEasingCurve, QPoint, QPointF, QRect, QRectF, Qt, QTimer, QVariantAnimation,
)
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QApplication

from .config import CardData
from .dlog import dlog
from .gate import gated
from .glass import HOVER_GROW, HOVER_MS, GlassMenu, GlassWindow
from . import aligner
from . import desktop_icons as di
from . import groupmove
from . import screen as screen_mod

DOT_W = 26          # 右上角三个点的点击区域（和收纳盒一个尺寸）
EDGE = 7            # 拖边缘改大小时，多少像素以内算「按到了边上」

# 卡片上的字色：白玻璃上继续用那套深灰蓝，和时钟卡片一致
INK = QColor(30, 36, 48, 252)
INK_SOFT = QColor(40, 48, 62, 244)
INK_NOTE = QColor(52, 62, 80, 232)
INK_FAINT = QColor(66, 76, 94, 168)
ACCENT = QColor(91, 141, 239, 240)
RED = QColor(212, 86, 86, 232)


def card_font(size: float, weight=QFont.Weight.Normal,
              families=("Microsoft YaHei UI", "Segoe UI"),
              spacing: float = 0.0) -> QFont:
    f = QFont()
    f.setFamilies(list(families))
    f.setPixelSize(max(8, int(round(size))))
    f.setWeight(weight)
    if spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    return f


def draw_text(p: QPainter, rect: QRectF, text: str, font: QFont, color: QColor,
              align=Qt.AlignLeft | Qt.AlignVCenter, elide: bool = True) -> None:
    """一行字：太长就省略号，别糊出卡片外面。"""
    if not text:
        return
    p.setFont(font)
    p.setPen(color)
    if elide and rect.width() > 0:
        text = QFontMetricsF(font).elidedText(text, Qt.ElideRight, rect.width())
    p.drawText(rect, int(align), text)


class CardWindow(GlassWindow):
    """一张桌面卡片的外壳。"""

    DESIGN_W = 302.0
    DESIGN_H = 152.0

    def __init__(self, card: CardData, on_change: Callable[[], None],
                 radius: float = 10.0):
        # 默认不用 WindowStaysOnTopHint：卡片要「留在桌面上」，不能被别的
        # 窗口盖过。层级由 Controller.apply_z_order 统一管，只有用户勾了
        # 置顶才浮起来。
        # WindowDoesNotAcceptFocus 同盒子：点卡片不许激活（激活会被 Windows
        # 抬到最前，浮在窗口上闪一下）。卡片里没有键盘输入，放心关。
        flags = Qt.FramelessWindowHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus
        if card.always_on_top:
            flags |= Qt.WindowStaysOnTopHint
        super().__init__(None, flags, radius=radius, dark=False)
        self.card = card
        # 悬停放大用的透明余量：窗口比玻璃每边大这么多，玻璃才有地方长
        self.inset = 8
        self._hover_t = 0.0
        self._hover_until = 0.0
        self._hover_anim = QVariantAnimation(self)
        self._hover_anim.setDuration(HOVER_MS)
        self._hover_anim.setEasingCurve(QEasingCurve.InOutCubic)
        self._hover_anim.valueChanged.connect(self._on_hover_pulse)
        self._hover_anim.finished.connect(self._on_hover_finished)
        self._on_change = on_change
        self.on_z_changed = None
        self.on_closed = None         # Controller 挂上来：卡片菜单里点了「关闭卡片」
        self.on_place_new = None      # Controller 挂上来：避不开时找空位放
        # 多选：桌面上拉框选中几个盒子/卡片，它们一起高亮、一起搬
        self.on_group = None
        self.on_widget_press = None
        self.sel_key = f"card:{getattr(card, 'id', '')}"
        self._is_selected = False
        self._drag_group: list = []
        self._group_origin: QPoint | None = None
        self._group_starts: list = []
        self.always_on_top = bool(card.always_on_top)
        # 白玻璃，参数和收纳盒对齐：白底 + 深灰蓝字，不加冷灰色调层
        self.glass_alpha = max(150, min(255, int(card.glass_alpha or 212)))
        # 卡片上全是字：白的那层比收纳盒多裹一点（1.5 倍），不然透到看不清内容
        self.frost = 1.5
        self.tint = None
        self._hovered = False
        self._resize_mode = False
        self._resizing = None      # (方向, 起点, 起始 scale, 起始玻璃宽高)
        self._swallow_press = False   # 这一下被「有盒子展开着」吞掉了（抬起也吞）
        self._press_token = None
        self._press_start: QPoint | None = None
        self._drag_from: QPoint | None = None
        # 拖动中的整窗快照：拖的时候只贴这张图，不再每帧重画玻璃和内容
        self._window_dragging = False
        self._drag_pm = None
        self._drag_started_at = 0.0
        self._drag_stat: dict | None = None   # 一次拖动的统计（结束后写一行日志）
        # 搬窗口的频率上限：一个显示帧最多搬一次（180Hz ≈ 5.6ms）。鼠标每秒报
        # 400~1000 次，每次都搬只会让 DWM 白合成分量帧。
        self._move_min = 0.005
        self._move_at = 0.0
        self._tail: QPoint | None = None      # 还没搬过去的最新位置
        # ⚠️ 拖窗口别用定时器节流。以前试过「记下最新目标，8ms 搬一次」，结果鼠标
        # 一快（1000Hz）事件队列永远是满的，Qt 的定时器要等队列空才轮得到，实测
        # 只有 62~92 次/秒，而且是不规则的 11ms 一顿——快速来回扫时卡片只落在两个
        # 端点附近。改成每个鼠标事件直接搬（Windows 会把鼠标消息合并，实测 175 次/
        # 秒，就是屏幕刷新率的量级），窗口位置和鼠标一一对应。
        # 改大小模式下滑到哪儿显示哪种缩放箭头：圆点指针在这块卡片上要让位，
        # 但系统的缩放箭头要一直挂住，不然鼠标一停就退回默认箭头了
        self._edge_timer = QTimer(self)
        self._edge_timer.setInterval(120)
        self._edge_timer.timeout.connect(self._refresh_edge_cursor)

        self.build_body()
        self._apply_style()
        self._place()
        # 不透明度只作用在玻璃背景上，字始终是实心的
        self.bg_opacity = max(0.0, min(1.0, float(card.opacity or 1.0)))
        self.setWindowOpacity(1.0)
        # 跨零点：待办卡片上的「今天/明天」、倒计时的天数都要重算
        self._today = date.today()
        self._day_timer = QTimer(self)
        self._day_timer.setInterval(60 * 1000)
        self._day_timer.timeout.connect(self._tick_day)
        self._day_timer.start()

    def _tick_day(self) -> None:
        today = date.today()
        if today == self._today:
            return
        self._today = today
        self.refresh_agenda()
        self.update()

    # ---------- 子类要实现的 ----------
    def build_body(self) -> None:
        """建自己的控件和布局。"""

    def _apply_style(self) -> None:
        self._set_size(self.DESIGN_W, self.DESIGN_H)

    def paint_content(self, p: QPainter) -> None:
        """往玻璃上画内容。坐标系：玻璃左上角 (0,0)。"""

    def hit_test(self, glass: QPointF):
        """按下时命中了什么？返回一个令牌（任何对象都行），没命中返回 None。"""
        return None

    def clicked(self, token) -> None:
        """按下和松开都在同一个位置（没拖动）：算一次点击。"""

    def double_click(self, glass: QPointF) -> bool:
        """双击。返回 True 表示吃掉了。"""
        return False

    def context_menu(self, glass: QPointF) -> bool:
        """右键。返回 True 表示吃掉了。"""
        return False

    def wheel_action(self, steps: int) -> bool:
        """滚轮，steps 正数是往下滚。返回 True 表示吃掉了。"""
        return False

    def refresh_agenda(self) -> None:
        """待办 / 纪念日的数据变了：重画。"""

    def set_weather_data(self, data) -> None:
        """只有时钟卡片用得上，别的卡片忽略。"""

    # ---------- 尺寸 ----------
    def _px(self, base: float) -> int:
        return max(9, int(round(base * self.card.scale)))

    def _scale(self, base: float) -> float:
        """线宽、小半径这种「不能套 9px 下限」的尺寸用这个。"""
        return float(base) * self.card.scale

    def _set_size(self, width: float, height: float) -> None:
        """按设计像素定窗口大小（内部乘 scale，再加四周透明余量）。"""
        self.setFixedSize(self._px(width) + self.inset * 2,
                          self._px(height) + self.inset * 2)

    def _glass_size(self) -> tuple[int, int]:
        return self.width() - self.inset * 2, self.height() - self.inset * 2

    def _place(self) -> None:
        scr = QApplication.primaryScreen().availableGeometry()
        if self.card.x is None or self.card.y is None:
            self.move(scr.right() - self.width() - self._px(26),
                      scr.top() + self._px(26))
        else:
            # card.x/y 存的是**玻璃**左上角（和收纳盒一致），窗口要减掉余量
            self.move(int(self.card.x) - self.inset,
                      int(self.card.y) - self.inset)
        self._clamp()

    def _clamp(self) -> None:
        """贴边允许到 0 距离（以前留 4px，用户觉得「永远差一点」）。"""
        scr = (QApplication.screenAt(self.frameGeometry().center())
               or QApplication.primaryScreen()).availableGeometry()
        m = self.inset
        gw, gh = self._glass_size()
        x = min(max(self.x(), scr.left() - m), scr.right() + 1 - gw - m)
        y = min(max(self.y(), scr.top() - m), scr.bottom() + 1 - gh - m)
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._request_level()

    def _request_level(self) -> None:
        cb = getattr(self, "on_z_changed", None)
        if cb is not None:
            cb()

    # ---------- 绘制 ----------
    def paintEvent(self, event) -> None:
        st = self._drag_stat
        if st is not None:
            st["paints"] += 1
        # 拖动中只贴一张开始拖之前的快照：窗口每帧都在动，重画玻璃 + 内容
        # （再加上底下压着的别的半透明窗口）就会一顿一顿的
        if self._window_dragging and self._drag_pm is not None:
            p = QPainter(self)
            try:
                p.setCompositionMode(QPainter.CompositionMode_Source)
                p.fillRect(self.rect(), Qt.transparent)
                p.setCompositionMode(QPainter.CompositionMode_SourceOver)
                p.drawPixmap(0, 0, self._drag_pm)
            finally:
                p.end()
            return
        z = 1.0 + HOVER_GROW * self._hover_t
        self.paint_scale = z
        super().paintEvent(event)
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            # 和玻璃层用同一套变换：围绕窗口中心缩放，再平移到玻璃左上角。
            # 文字是自绘的，所以会跟玻璃一起「鼓一下」，不会被落下。
            if abs(z - 1.0) > 1e-6:
                c = self.rect().center()
                p.translate(c.x(), c.y())
                p.scale(z, z)
                p.translate(-c.x(), -c.y())
            p.translate(self.inset, self.inset)
            self.paint_content(p)
            if self._hovered or self._resize_mode:
                self._paint_dots(p)
        finally:
            p.end()

    # ---------- 右上角三个点 ----------
    def _dot_rect(self) -> QRectF:
        cw, _ch = self._glass_size()
        return QRectF(cw - 8 - DOT_W, 4, DOT_W, DOT_W)

    def _paint_dots(self, p: QPainter) -> None:
        r = self._dot_rect()
        p.setOpacity(1.0)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(58, 70, 92, 205))
        cy = r.center().y()
        for i in range(3):
            p.drawEllipse(QPointF(r.left() + 6 + i * 7, cy), 2.0, 2.0)

    def _open_menu(self) -> None:
        menu = GlassMenu(self)
        menu.prepare_backdrop()
        before = len(menu.actions())
        extra = self.menu_extra(menu)         # 各卡片自己的菜单项：放在通用项上面
        if len(menu.actions()) > before:
            menu.addSeparator()
        act_size = menu.addAction("改变大小")
        act_size.setCheckable(True)
        act_size.setChecked(self._resize_mode)
        act_off = menu.addAction("关闭卡片")
        chosen = menu.exec(QCursor.pos())
        if chosen is act_size:
            self._set_resize_mode(act_size.isChecked())
        elif chosen is act_off:
            self._close_card()
        elif extra is not None:
            extra(chosen)

    def menu_extra(self, menu):
        """子类可以往这张卡片的菜单里加东西；返回一个处理选中项的回调。"""
        return None

    def _close_card(self) -> None:
        """关闭卡片：只是不显示，数据留着，面板里再启用就回来。"""
        self.card.enabled = False
        self._on_change()                     # 先落盘（store 的 save）
        cb = getattr(self, "on_closed", None)
        if cb is not None:
            # 面板上那一行还要跟着改成「已关闭」，不然用户会以为没关掉
            try:
                cb(self)
            except (RuntimeError, TypeError):
                pass
        self.hide()

    # ---------- 改大小 ----------
    def _edge_at(self, pos: QPoint) -> int:
        cw, ch = self._glass_size()
        right = pos.x() >= cw - EDGE
        bottom = pos.y() >= ch - EDGE
        if right and bottom:
            return 3
        if right:
            return 1
        if bottom:
            return 2
        return 0

    def _set_resize_mode(self, on: bool) -> None:
        self._resize_mode = bool(on)
        if not self._resize_mode:
            self.unsetCursor()
            self._edge_timer.stop()
            # 兜底：万一哪次拖动没收到 release，退出模式时把圆点放回来
            self._notify_resize_drag(False)
        else:
            self._edge_timer.start()
        cb = getattr(self, "on_resize_mode", None)
        if cb is not None:
            try:
                cb(self._resize_mode)
            except (RuntimeError, TypeError):
                pass
        self.update()

    def _refresh_edge_cursor(self) -> None:
        """改大小模式里持续把缩放箭头挂住（鼠标停着的时候也不会掉回默认箭头）。"""
        if not self._resize_mode:
            self._edge_timer.stop()
            return
        if self._resizing is not None:
            return
        local = self.mapFromGlobal(QCursor.pos())
        if not self.rect().contains(local):
            return
        glass = QPoint(local.x() - self.inset, local.y() - self.inset)
        self._apply_edge_cursor(self._edge_at(glass))

    @staticmethod
    def _edge_cursor(edge):
        return {3: Qt.SizeFDiagCursor,
                1: Qt.SizeHorCursor,
                2: Qt.SizeVerCursor}.get(edge)

    def _apply_edge_cursor(self, edge) -> None:
        cur = self._edge_cursor(edge)
        if cur is None:
            self.unsetCursor()
        else:
            self.setCursor(cur)

    def _notify_resize_drag(self, on: bool) -> None:
        cb = getattr(self, "on_resize_drag", None)
        if cb is not None:
            try:
                cb(bool(on))
            except (RuntimeError, TypeError):
                pass

    # ---------- 悬停「弹一下」（和收纳盒同一套） ----------
    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        now = time.monotonic()
        if now >= self._hover_until:
            self._hover_until = now + (HOVER_MS * 2 + 40) / 1000.0
            self._pulse_start()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        # 不打断：开始就播完整段（和收纳盒一致）
        self._hovered = False
        if self._resizing is None:
            self.unsetCursor()
        self.update()
        super().leaveEvent(event)

    def _pulse_start(self) -> None:
        self._hover_anim.stop()
        self._hover_anim.setDuration(HOVER_MS)
        self._hover_anim.setStartValue(self._hover_t)
        self._hover_anim.setEndValue(1.0)
        self._hover_anim.start()

    def _on_hover_pulse(self, value) -> None:
        self._hover_t = float(value)
        self.update()

    def _on_hover_finished(self) -> None:
        if self._hover_t >= 0.99:
            self._hover_anim.setDuration(HOVER_MS + 40)
            self._hover_anim.setStartValue(1.0)
            self._hover_anim.setEndValue(0.0)
            self._hover_anim.start()
        else:
            self._pulse_restore()

    def _pulse_restore(self) -> None:
        self._hover_anim.stop()
        self._hover_t = 0.0
        self.paint_scale = 1.0
        self.update()

    # ---------- 多选（框选）----------
    def set_selected(self, on: bool) -> None:
        """框选中 / 取消：玻璃底色压暗一点，和收纳盒选中时同一个色。"""
        on = bool(on)
        if on == self._is_selected:
            return
        self._is_selected = on
        self.tint = QColor(18, 24, 38, 34) if on else None
        self.update()

    def _group_rect(self) -> QRect:
        """这块卡片在屏幕上占的玻璃矩形（逻辑像素）——整组夹屏幕时用它。"""
        gw, gh = self._glass_size()
        return QRect(self.x() + self.inset, self.y() + self.inset, gw, gh)

    def _group_shift(self, dx: int, dy: int) -> None:
        """整组搬完之后：按新的玻璃左上角落地、夹进屏幕、落盘。"""
        r = self._group_rect().translated(dx, dy)
        self.move(r.left() - self.inset, r.top() - self.inset)
        self._clamp()
        self.card.x, self.card.y = self.x() + self.inset, self.y() + self.inset
        self._on_change()
        self.update()

    # ---------- 鼠标 ----------
    def _glass_pos(self, event) -> QPoint:
        pos = event.position().toPoint()
        return QPoint(pos.x() - self.inset, pos.y() - self.inset)

    def mousePressEvent(self, event) -> None:
        if gated():
            # 有收纳盒正展开着：这一下只负责把那个盒子关掉（它自己在轮询「点外面」），
            # 卡片这边不响应（不拖、不弹菜单、不改大小）
            self._swallow_press = True
            event.accept()
            return
        # 拖动卡片前先把放大收回去，否则会「在放大状态」被拖走
        if (self._hover_t > 0.001
                or self._hover_anim.state() == QVariantAnimation.State.Running):
            self._pulse_restore()
        if event.button() == Qt.LeftButton:
            # 按下就当新一轮拖动开始：上一次要是没收到 release（窗口被挪走、
            # 弹窗抢了鼠标…），这里把残留状态清干净，不然会一直「像在拖」。
            self._reset_drag_state()
            # 多选：按在没被选中的卡片上 = 这一组到此为止（和收纳盒一致，
            # 放在三个点/改大小前面：菜单弹出来之前就把多选结束掉）
            cb = getattr(self, "on_widget_press", None)
            if cb is not None:
                try:
                    cb(self)
                except (RuntimeError, TypeError):
                    pass
            glass = self._glass_pos(event)
            if self._dot_rect().contains(QPointF(glass)):
                event.accept()
                self._open_menu()
                return
            if self._resize_mode:
                edge = self._edge_at(glass)
                if edge:
                    gw, gh = self._glass_size()
                    self._resizing = (edge, event.globalPosition().toPoint(),
                                      float(self.card.scale), gw, gh)
                    # 从按下这一刻起就一直挂住缩放箭头（拖出卡片外面也不换），
                    # 同时让圆点指针整个让位，不然拖出去会闪圆球
                    self.setCursor(self._edge_cursor(edge))
                    self._notify_resize_drag(True)
                    event.accept()
                    return
            token = self.hit_test(QPointF(glass))
            if token is not None:
                self._press_token = token
                self._press_start = event.globalPosition().toPoint()
            # 记下按下时的位置：松手时如果没挪过，就不做避让
            self._drag_from = self.pos()
            # 多选：按在选中的卡片上 = 接下来拖的是整组（兄弟们按同一个位移走）
            self._drag_group = groupmove.begin(self)
            self._group_origin = None
            self._group_starts = []
            if self._drag_group:
                self._group_origin = QPoint(self.pos())
                self._group_starts = [(w, QPoint(w.pos())) for w in self._drag_group]
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._resizing is not None:
            edge, start, s0, gw, gh = self._resizing
            d = event.globalPosition().toPoint() - start
            dx = d.x() / max(1.0, float(gw))
            dy = d.y() / max(1.0, float(gh))
            delta = max(dx, dy) if edge == 3 else (dx if edge == 1 else dy)
            scale = max(0.6, min(2.2, s0 * (1.0 + delta)))
            self.card.scale = round(scale, 2)
            self._apply_style()
            self._place()
            self.update()
            # 拖动全程都用同一根箭头：鼠标挪出卡片外面、光标落到桌面上也不换
            self.setCursor(self._edge_cursor(edge))
            event.accept()
            return
        if self._resize_mode:
            self._apply_edge_cursor(self._edge_at(self._glass_pos(event)))
            super().mouseMoveEvent(event)
            return
        if self._drag is not None and event.buttons() & Qt.LeftButton:
            # 拖窗口：每个事件直接搬到位（Windows 已经帮我们合并过鼠标消息了）。
            # 别在这里加定时器节流——理由见 __init__ 里那段注释。
            if not self._window_dragging:
                self._window_dragging = True
                try:                      # 让盒子的模糊抓图 / 层级重排一起让路
                    from .box_widget import set_dragging
                    set_dragging(True)
                except Exception:         # noqa: BLE001
                    pass
                self._drag_started_at = time.monotonic()
                self._drag_stat = {"t0": time.perf_counter(), "moves": 0,
                                   "move_ms": 0.0, "paints": 0, "gaps": [],
                                   "last": 0.0}
                try:
                    self._drag_pm = self.grab()
                except (RuntimeError, TypeError):
                    self._drag_pm = None
            target = (event.globalPosition() - self._drag).toPoint()
            if target != self.pos():
                now = time.perf_counter()
                st = self._drag_stat
                if st["moves"] and st["last"]:
                    st["gaps"].append((now - st["last"]) * 1000.0)
                st["last"] = now
                self._tail = target
                if now - self._move_at < self._move_min:
                    # 离上一帧不到一个刷新周期：只记下位置，等下一个事件再搬。
                    # 不这么做的话，按住鼠标时每秒有 400~800 个鼠标事件，每个都搬
                    # 一次窗口（DWM 一帧只显示得下一条），白占线程、还把定时器饿死。
                    event.accept()
                    return
                self._move_at = now
                t0 = time.perf_counter()
                self.move(target)
                # 多选：兄弟们跟着同一个位移走，相对位置一个像素都不变
                groupmove.follow(self, self._group_origin, self._group_starts)
                st["move_ms"] += (time.perf_counter() - t0) * 1000.0
                st["moves"] += 1
            event.accept()
            return
        super().mouseMoveEvent(event)

    def _flush_tail(self) -> None:
        """补最后一帧：鼠标停住时，最后那个位置可能因为不到一个刷新周期被跳过。"""
        tail = self._tail
        self._tail = None
        if tail is not None and tail != self.pos():
            self._move_at = time.perf_counter()
            self.move(tail)

    def _log_drag(self) -> None:
        """一次拖动结束：把「每秒搬了几次、每次多久、重画几次」记一行日志。

        这是查「拖着这张卡就卡」的唯一直接证据：如果每秒搬的次数明显低于屏幕
        刷新率，是我们自己的问题；如果次数正常，那是合成器那边的事。
        """
        st = getattr(self, "_drag_stat", None)
        self._drag_stat = None
        if not st or not st["moves"]:
            return
        dur = (time.perf_counter() - st["t0"]) * 1000.0
        rate = st["moves"] / max(0.001, dur / 1000.0)
        gaps = sorted(st["gaps"][1:])          # 第一个间隔含着 grab() 的开销，去掉
        if gaps:
            n = len(gaps)
            p50, p95 = gaps[n // 2], gaps[min(n - 1, int(n * 0.95))]
            gmax = gaps[-1]
        else:
            p50 = p95 = gmax = 0.0
        dlog("card drag: %s moves=%d %.0f/s move_avg=%.2fms paint=%d dur=%.0fms "
             "gap p50=%.0f p95=%.0f max=%.0f %.0fx%d"
             % (self.card.type, st["moves"], rate, st["move_ms"] / st["moves"],
                st["paints"], dur, p50, p95, gmax,
                self.width(), self.height()))

    def _reset_drag_state(self) -> None:
        """把「正在拖窗口」的残留状态清掉（快照、标记）。"""
        self._window_dragging = False
        self._drag_pm = None
        self._drag_started_at = 0.0
        try:
            from .box_widget import set_dragging
            set_dragging(False)
        except Exception:             # noqa: BLE001
            pass

    def mouseReleaseEvent(self, event) -> None:
        if getattr(self, "_swallow_press", False):
            # 按下时被「有盒子展开着」吞了，抬起也一起吞掉
            self._swallow_press = False
            event.accept()
            return
        if self._resizing is not None:
            self._resizing = None
            self._notify_resize_drag(False)
            # 放大之后可能压到旁边的卡片/盒子：松手时也走一遍避让
            self._avoid_overlap()
            if self._hits_sibling():
                cb = getattr(self, "on_place_new", None)
                if cb is not None:
                    try:
                        cb(self, QPoint(self.x() + self.inset,
                                       self.y() + self.inset))
                    except (RuntimeError, TypeError):
                        pass
            self.card.x, self.card.y = self.x() + self.inset, self.y() + self.inset
            self._on_change()
            self._refresh_edge_cursor()
            event.accept()
            return
        if self._window_dragging:
            self._flush_tail()         # 最后一帧的位置别丢
            self._reset_drag_state()
            self.update()
            self._log_drag()           # 一次拖动结束：记下每秒搬了几次
            # 层级不在这里立刻重排：apply_z_order 会把 14 个窗口从头插一遍，
            # 每次 SetWindowPos 都让 DWM 重合成，松手那一下就是一次「顿」。
            # 1.2 秒内的定时器会把它放回桌面层（拖动时临时提到顶层，见 _raise_to_top）。
        token, start = self._press_token, self._press_start
        self._press_token = None
        self._press_start = None
        super().mouseReleaseEvent(event)
        self._drag_group = []          # 整组这次已经搬完（on_drag_finished 里收的尾）
        self._group_origin = None
        self._group_starts = []
        if token is not None and start is not None:
            moved = (event.globalPosition().toPoint() - start).manhattanLength()
            if moved < 5 and event.button() == Qt.LeftButton:
                self.clicked(token)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.double_click(self._glass_pos(event)):
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:
        local = self.mapFromGlobal(event.globalPos())
        glass = QPoint(local.x() - self.inset, local.y() - self.inset)
        if self.context_menu(QPointF(glass)):
            event.accept()
            return
        super().contextMenuEvent(event)

    def wheelEvent(self, event) -> None:
        steps = event.angleDelta().y()
        if steps and self.wheel_action(-1 if steps > 0 else 1):
            event.accept()
            return
        super().wheelEvent(event)

    # ---------- 拖完：吸附 / 避让 ----------
    def on_drag_finished(self) -> None:
        """松手：不再贴边对齐，只做「先横着让」的避让。"""
        # 多选一起搬：整组夹进屏幕、各自落盘，不做单块的避让（会把组拆散）
        if self._drag_group:
            groupmove.finish(self, self._drag_group)
            self._drag_from = None
            return
        if not getattr(self, "auto_align", True):
            # 设置里关掉了「自适应对齐」：松手就停在原地，不避让、也不去抢别人
            # 的位置。只把坐标记下来。
            self._drag_from = None
            m = self.inset
            self.card.x, self.card.y = self.x() + m, self.y() + m
            self._on_change()
            return
        # 只是点了一下（窗口没挪）：别避让，否则每点一次卡片就跳一下
        started = getattr(self, "_drag_from", None)
        self._drag_from = None
        if started is not None and self.pos() == started:
            return
        self._clamp()
        self._avoid_overlap()
        # 让完还压着别人（贴着屏幕边，推一步又被夹回来）：交给 Controller
        # 在手边找一块真正空着的地方放
        if self._hits_sibling():
            cb = getattr(self, "on_place_new", None)
            if cb is not None:
                try:
                    cb(self, QPoint(self.x() + self.inset, self.y() + self.inset))
                except (RuntimeError, TypeError):
                    pass
        self.card.x, self.card.y = self.x() + self.inset, self.y() + self.inset
        self._on_change()

    def _hits_sibling(self) -> bool:
        """现在这块玻璃还压着别的卡片/盒子吗？"""
        try:
            provider = getattr(self, "siblings", None)
            others = [w for w in provider() if w is not self and w.isVisible()] \
                if provider is not None else []
        except (RuntimeError, TypeError):
            return False
        m = self.inset
        mine = QRect(self.x() + m, self.y() + m,
                     self.width() - 2 * m, self.height() - 2 * m)
        for w in others:
            try:
                if mine.intersects(aligner.glass_rect(w)):
                    return True
            except (RuntimeError, TypeError):
                continue
        return False

    def _avoid_overlap(self) -> None:
        """拖完别压住别的卡片/盒子：**先只横着让，上下高度一个像素都不动**。

        原来这里是「沿最短位移推开」＋「18px 内贴边对齐」，用户拖完总觉得
        被强制吸到别人的正上/正下/同高——规则和收纳盒统一了（见 aligner）。
        桌面图标在这里留着：卡片不像盒子那样会把自己压住的图标挤走，
        不躲开就等于把图标藏到玻璃下面去了。
        """
        m = self.inset
        gw, gh = self._glass_size()
        mine = QRect(self.x() + m, self.y() + m, gw, gh)
        gap = 2
        obstacles = self._obstacles()
        if not obstacles:
            return
        scr = (QApplication.screenAt(self.frameGeometry().center())
               or QApplication.primaryScreen()).availableGeometry()

        def clamp(r: QRect) -> QRect:
            x = min(max(r.left(), scr.left()), scr.right() + 1 - r.width())
            y = min(max(r.top(), scr.top()), scr.bottom() + 1 - r.height())
            return QRect(x, y, r.width(), r.height())

        landed = aligner.place_clear(mine, obstacles, gap, clamp,
                                     max_shift=max(gw, gh))
        if landed is None:
            return
        self.move(landed.left() - m, landed.top() - m)

    def _obstacles(self) -> list:
        """挡路的东西：别的卡片 / 盒子（玻璃矩形）+ 桌面图标（逻辑像素）。"""
        out = []
        provider = getattr(self, "siblings", None)
        if provider is not None:
            try:
                others = [w for w in provider() if w is not self and w.isVisible()]
            except (RuntimeError, TypeError):
                others = []
            for w in others:
                try:
                    out.append(aligner.glass_rect(w))
                except (RuntimeError, TypeError):
                    continue
        try:
            out += [QRect(*screen_mod.to_logical((r[0], r[1], r[2], r[3])))
                    for r in di.list_rects().values()]
        except Exception:  # noqa: BLE001 - 桌面接口偶尔抽风，不挡避让
            pass
        return out

    # ---------- 设置窗口用的 ----------
    def set_enabled_scale(self, scale: float) -> None:
        self.card.scale = float(scale)
        self._apply_style()
        self._place()
        self._on_change()

    def set_opacity(self, opacity: float) -> None:
        """只改背景浓度，字的实心程度不动。"""
        self.card.opacity = float(opacity)
        self.bg_opacity = max(0.0, min(1.0, self.card.opacity))
        self.update()
        self._on_change()

    def set_glass_alpha(self, alpha: int) -> None:
        self.card.glass_alpha = int(max(150, min(255, alpha)))
        self.glass_alpha = self.card.glass_alpha
        self.update()
        self._on_change()

    def reset_position(self) -> None:
        self.card.x = self.card.y = None
        self._place()
        self._on_change()
