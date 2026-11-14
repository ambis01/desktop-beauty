"""音乐播放器卡片：显示当前在放的歌（封面 / 歌名 / 歌手 / 进度），
并且能播放暂停、上一首、下一首、切换播放模式。

数据和控制全走 music.MusicService（Windows 系统媒体会话），卡片自己不碰
任何播放器进程。样式：整张卡片压一层专辑封面的淡色，左边一块清晰封面，
右边是歌名、歌手、一排按钮和进度条——封面上那点颜色就是这张卡的主色。
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QColor, QCursor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
)

from .card_base import ACCENT, INK, INK_FAINT, INK_NOTE, INK_SOFT, CardWindow, card_font, draw_text
from .config import CardData
from .dlog import dlog
from .music import (MODE_LIST, MODE_ONE, MODE_SEQUENTIAL, MODE_SHUFFLE,
                    NO_FOLLOW)

ICON_FAMILY = "Segoe Fluent Icons"
# 图标字体里的媒体图标（Segoe MDL2 Assets 里也有同一套）
GLYPH = {
    "play": "\uE768",
    "pause": "\uE769",
    "prev": "\uE892",
    "next": "\uE893",
    "mode0": "\uE8FD",     # 顺序播放：列表
    "mode1": "\uE8EE",     # 列表循环
    "mode2": "\uE8ED",     # 单曲循环
    "mode3": "\uE8B1",     # 随机
}

HOLD_MS = 1000          # 按住播放按钮多久算「长按」（开始拉线）
LINK_HOVER_MS = 1000    # 把线拖到另一颗播放按钮上，停多久算「连上」


class MusicWidget(CardWindow):
    DESIGN_W = 348.0
    DESIGN_H = 152.0

    def __init__(self, card: CardData, on_change: Callable[[], None], music,
                 radius: float = 10):
        self.music = music
        self._hover_btn: str | None = None
        self._cover_pm: QPixmap | None = None
        self._wash_pm = None        # 封面压出来的那层淡色（按卡片比例裁好、左边浓右边淡）
        self._tick: QTimer | None = None
        # ---- 长按拉线（两张卡片联动）----
        self._hold_timer: QTimer | None = None
        self._hold_pos: QPoint | None = None
        self._linking = False
        self._ring = 0              # 自己这颗播放按钮的提示圈：0 无 / 1 按住中 / 2 已拉线
        self._ring_hot = 0          # 别人把线搭过来时，这颗按钮的圈：0/1/2
        self._target_arm: QTimer | None = None
        self._target_pm = None      # 正在搭线的目标卡片
        self._probe_logged = False  # 这次拉线有没有记过「距离」调试行
        super().__init__(card, on_change, radius)
        self.music.changed.connect(self._on_state)
        self.music.cover_new.connect(self._on_cover)
        self._on_cover()
        self._tick = QTimer(self)
        self._tick.setInterval(500)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()
        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.timeout.connect(self._hold_fired)
        self._target_arm = QTimer(self)
        self._target_arm.setSingleShot(True)
        self._target_arm.timeout.connect(self._target_armed)

    # ------------------------------------------------------------ 尺寸
    def _apply_style(self) -> None:
        self._set_size(self.DESIGN_W, self.DESIGN_H)
        self._wash_pm = None
        self._build_wash()

    def _build_wash(self) -> None:
        """把封面缩成极小再放大，得到一层「这张专辑的颜色」，垫在玻璃上。

        不直接铺原图：原图里的圆环、文字、高光铺开会变成一块块白斑，很脏。
        缩小到 1/20 再放大之后只剩颜色分布，左边浓、右边淡（右边要放字）。
        """
        cw, ch = self._glass_size()
        pm = self._cover_pm
        if pm is None or cw <= 8 or ch <= 8:
            self._wash_pm = None
            return
        out = QPixmap(cw, ch)
        out.fill(Qt.transparent)
        g = QPainter(out)
        g.setRenderHint(QPainter.SmoothPixmapTransform, True)
        small = pm.scaled(max(2, cw // 20), max(2, ch // 20),
                          Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        g.drawPixmap(QRectF(0, 0, cw, ch), small, QRectF(small.rect()))
        g.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        grad = QLinearGradient(0, 0, cw, 0)
        grad.setColorAt(0.0, QColor(0, 0, 0, 255))
        grad.setColorAt(0.45, QColor(0, 0, 0, 150))
        grad.setColorAt(1.0, QColor(0, 0, 0, 30))
        g.fillRect(QRectF(0, 0, cw, ch), grad)
        g.end()
        self._wash_pm = out

    def _cover_rect(self) -> QRectF:
        s = self._scale(98)
        return QRectF(self._scale(16), self._scale(27), s, s)

    def _text_left(self) -> float:
        return self._scale(130)

    def _btn_centers(self) -> dict[str, QPointF]:
        """上一首/播放/下一首在「文字栏」里居中放，不跟左边挤在一起。

        支持循环/随机的播放器才会在最右边多出一颗模式按钮（那颗本来就是角上的
        小开关）；不支持的时候三颗照样居中，左右留白一样宽，看着才不歪。
        """
        w, _h = self._glass_size()
        left = self._text_left()
        right = w - self._scale(16)
        cx = (left + right) / 2.0
        gap = self._scale(38)
        y = self._scale(92)
        return {
            "prev": QPointF(cx - gap, y),
            "play": QPointF(cx, y),
            "next": QPointF(cx + gap, y),
            "mode": QPointF(w - self._scale(26), y),
        }

    def _progress_rect(self) -> QRectF:
        w, _h = self._glass_size()
        left = self._text_left()
        right = w - self._scale(16)
        return QRectF(left, self._scale(128), max(10.0, right - left), self._scale(3))

    def _hit(self, center: QPointF, r: float, glass: QPointF) -> bool:
        dx = glass.x() - center.x()
        dy = glass.y() - center.y()
        return dx * dx + dy * dy <= r * r

    # ------------------------------------------------------------ 绘制
    def paint_content(self, p: QPainter) -> None:
        snap = self.music.snap
        has = bool(snap.get("has"))
        playing = bool(snap.get("playing"))
        cw, ch = self._glass_size()
        radius = float(self.radius)

        # 1) 整张卡片压一层「这张专辑的颜色」（左边浓、右边淡，右边要放字）
        if self._wash_pm is not None:
            path = QPainterPath()
            path.addRoundedRect(QRectF(0, 0, cw, ch), radius, radius)
            p.save()
            p.setClipPath(path)
            p.setOpacity(0.85)
            p.drawPixmap(0, 0, self._wash_pm)
            p.setOpacity(1.0)
            # 一层薄白，保证歌名和按钮在任何封面上都看得清
            p.fillRect(QRectF(0, 0, cw, ch), QColor(255, 255, 255, 96))
            p.restore()

        # 2) 左边那块清晰封面
        self._paint_cover(p, has)

        # 3) 歌名 / 歌手
        if has:
            left = self._text_left()
            width = cw - left - self._scale(14)
            draw_text(p, QRectF(left, self._scale(24), width, self._scale(24)),
                      snap.get("title") or "",
                      card_font(self._px(15), QFont.Weight.DemiBold), INK,
                      Qt.AlignLeft | Qt.AlignVCenter)
            artist = snap.get("artist") or snap.get("album") or ""
            if artist:
                draw_text(p, QRectF(left, self._scale(50), width, self._scale(18)),
                          artist, card_font(self._px(11)), INK_SOFT,
                          Qt.AlignLeft | Qt.AlignVCenter)

        # 4) 进度条（有总时长才画，直播之类的没有就别画一条假的）
        dur = float(snap.get("dur") or 0.0)
        if has and dur > 0:
            pos = self.music.position_now()
            frac = max(0.0, min(1.0, pos / dur))
            r = self._progress_rect()
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(70, 80, 102, 46))
            p.drawRoundedRect(r, r.height() / 2.0, r.height() / 2.0)
            if frac > 0.001:
                fill = QRectF(r.left(), r.top(), max(r.height(), r.width() * frac),
                              r.height())
                p.setBrush(ACCENT)
                p.drawRoundedRect(fill, r.height() / 2.0, r.height() / 2.0)

        # 5) 一排按钮
        centers = self._btn_centers()
        can_play = bool(snap.get("can_play"))
        self._paint_icon(p, centers["prev"], GLYPH["prev"], 14,
                         bool(snap.get("can_prev")), hover=self._hover_btn == "prev")
        self._paint_icon(p, centers["play"], GLYPH["pause" if playing else "play"], 18,
                         can_play, hover=self._hover_btn == "play", disc=True,
                         ring=max(int(self._ring), int(self._ring_hot)))
        self._paint_icon(p, centers["next"], GLYPH["next"], 14,
                         bool(snap.get("can_next")), hover=self._hover_btn == "next")
        mode = int(snap.get("mode", MODE_SEQUENTIAL))
        # 播放器不支持循环/随机（网易云、QQ音乐都对系统声明不支持）就整颗不画：
        # 一颗灰着、点了没反应的按钮比没有更糟
        if snap.get("can_mode"):
            self._paint_icon(p, centers["mode"],
                             GLYPH.get(f"mode{mode}", GLYPH["mode0"]), 15, True,
                             hover=self._hover_btn == "mode")

    def _paint_cover(self, p: QPainter, has: bool) -> None:
        r = self._cover_rect()
        path = QPainterPath()
        path.addRoundedRect(r, self._scale(12), self._scale(12))
        p.save()
        p.setClipPath(path)
        if self._cover_pm is not None:
            pm = self._cover_pm
            side = min(pm.width(), pm.height())
            src = QRectF((pm.width() - side) / 2.0, (pm.height() - side) / 2.0,
                         side, side)
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            p.drawPixmap(r, pm, src)
        else:
            # 没有封面：一块柔和的白玻璃 + 一个音符，别写字
            p.fillRect(r, QColor(255, 255, 255, 135))
            f = QFont("Segoe UI Symbol")
            f.setPixelSize(self._px(30))
            p.setFont(f)
            col = QColor(INK_FAINT)
            col.setAlpha(150 if has else 110)
            p.setPen(col)
            p.drawText(r, Qt.AlignCenter, "\u266B")
        p.restore()
        # 一圈细边，白底上不至于糊在背景里
        p.setBrush(Qt.NoBrush)
        p.setPen(QColor(255, 255, 255, 190))
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5),
                          self._scale(12), self._scale(12))

    def _paint_icon(self, p: QPainter, center: QPointF, glyph: str, size: float,
                    enabled: bool, hover: bool = False, disc: bool = False,
                    ring: int = 0) -> None:
        r = self._scale(15 if disc else 14)
        if disc:
            col = QColor(38, 46, 60, 26 if not enabled else (44 if hover else 30))
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawEllipse(center, self._scale(17), self._scale(17))
        elif hover and enabled:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 165))
            p.drawEllipse(center, r, r)
        f = QFont(ICON_FAMILY)
        f.setPixelSize(self._px(size))
        p.setFont(f)
        col = QColor(INK if disc else INK_NOTE)
        col.setAlpha(255 if enabled else 96)
        p.setPen(col)
        box = QRectF(center.x() - self._scale(20), center.y() - self._scale(20),
                     self._scale(40), self._scale(40))
        p.drawText(box, Qt.AlignCenter, glyph)
        if ring:
            # 长按拉线时的提示圈：1 = 对方把线搭过来了（淡），2 = 已经连上（实）
            rr = self._scale(21)
            p.setBrush(Qt.NoBrush)
            if ring == 2:
                pen = QPen(QColor(ACCENT.red(), ACCENT.green(), ACCENT.blue(), 235))
                pen.setWidthF(self._scale(2.0))
            else:
                pen = QPen(QColor(ACCENT.red(), ACCENT.green(), ACCENT.blue(), 120))
                pen.setWidthF(self._scale(1.5))
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawEllipse(center, rr, rr)

    # ------------------------------------------------------------ 状态
    def _on_state(self) -> None:
        self.update()

    def _on_cover(self) -> None:
        raw = self.music.cover_bytes
        if not raw:
            self._cover_pm = None
            self._build_wash()
            self.update()
            return
        pm = QPixmap()
        if pm.loadFromData(raw):
            self._cover_pm = pm
        else:
            self._cover_pm = None
        self._build_wash()
        self.update()

    def _on_tick(self) -> None:
        # 只在真有进度可动的时候重画（空卡片别每半秒白刷一次）
        if self.music.snap.get("has") and float(self.music.snap.get("dur") or 0) > 0:
            self.update()

    # ------------------------------------------------------------ 交互
    def hit_test(self, glass: QPointF):
        snap = self.music.snap
        centers = self._btn_centers()
        for name, ok in (("prev", snap.get("can_prev")),
                         ("play", snap.get("can_play")),
                         ("next", snap.get("can_next")),
                         ("mode", snap.get("can_mode"))):
            if ok and self._hit(centers[name], self._scale(17), glass):
                return ("btn", name)
        return None

    def clicked(self, token) -> None:
        if not token or token[0] != "btn":
            return
        name = token[1]
        if name == "play":
            self.music.play_pause()
        elif name == "next":
            self.music.next()
        elif name == "prev":
            self.music.prev()
        elif name == "mode":
            self.music.cycle_mode()
        self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._linking:
            self._link_update(event.globalPosition().toPoint())
            event.accept()
            return
        pt = event.globalPosition().toPoint()
        if self._hold_timer.isActive() and self._hold_pos is not None:
            if (pt - self._hold_pos).manhattanLength() > 8:
                # 手动了：那是点一下或者拖卡片，不是长按
                self._hold_timer.stop()
                self._hold_pos = None
                self._ring = 0
                self.update()
        super().mouseMoveEvent(event)
        if self._window_dragging:
            return
        glass = QPointF(self._glass_pos(event))
        hover = None
        snap = self.music.snap
        centers = self._btn_centers()
        for name, ok in (("prev", snap.get("can_prev")),
                         ("play", snap.get("can_play")),
                         ("next", snap.get("can_next")),
                         ("mode", snap.get("can_mode"))):
            if ok and self._hit(centers[name], self._scale(17), glass):
                hover = name
                break
        if hover != self._hover_btn:
            self._hover_btn = hover
            self.update()

    def leaveEvent(self, event) -> None:
        self._hover_btn = None
        super().leaveEvent(event)

    # ------------------------------------------------------------ 菜单
    def menu_extra(self, menu):
        """卡片菜单里加「跟随播放器」（排在通用项上面）。

        三档：自动 / 不跟随 / 钉在某一个播放器上。播放器直接平铺列出来，不套二级
        菜单：这个菜单本身就贴着屏幕右边，二级菜单会翻到左边、还容易被裁掉。
        """
        svc = self.music
        pref = getattr(svc, "prefer", "")
        head = menu.addAction("跟随播放器")
        head.setEnabled(False)
        acts = {}
        act_auto = menu.addAction("　自动")
        act_auto.setCheckable(True)
        act_auto.setChecked(not pref)
        act_none = menu.addAction("　不跟随")
        act_none.setCheckable(True)
        act_none.setChecked(pref == NO_FOLLOW)
        try:
            players = svc.players()
        except Exception:  # noqa: BLE001
            players = []
        for p in players:
            label = p.get("title") or p.get("app") or p.get("key") or "?"
            if p.get("artist"):
                label = f"{label} · {p['artist']}"
            act = menu.addAction("　" + label)
            act.setCheckable(True)
            act.setChecked(bool(pref) and pref == p.get("key"))
            acts[act] = p.get("key") or ""
        if not players:
            none_act = menu.addAction("　（现在没有播放器）")
            none_act.setEnabled(False)

        def handle(chosen):
            if chosen is None:
                return
            if chosen is act_auto:
                self._bind("")
            elif chosen is act_none:
                self._bind(NO_FOLLOW)
            elif chosen in acts:
                self._bind(acts[chosen])

        return handle

    def _bind(self, key: str) -> None:
        """手动指定这张卡片跟哪个播放器（记进配置，下次启动还认它）。"""
        self.music.set_prefer(key)
        self.card.bind = str(key or "")
        self._on_change()
        self.update()

    # ------------------------------------------------------------ 长按拉线
    # 按住播放键 1 秒：这颗按钮外圈亮一圈，并从它拉出一条（朦胧的白线）跟着鼠标。
    # 把线拖到另一张音乐卡片的播放键上停 1 秒：那颗按钮外圈也亮起来；这时松手，
    # 两个播放器就「一起按一下」——同时放或同时停，只做这一次，不留长期关系。
    def _play_global(self) -> QPoint:
        """播放按钮圆心在屏幕上的位置（逻辑坐标）。"""
        c = self._btn_centers()["play"]
        return self.mapToGlobal(QPoint(int(round(c.x())) + self.inset,
                                       int(round(c.y())) + self.inset))

    def _sibling_widgets(self):
        try:
            raw = getattr(self, "siblings", None)
            if callable(raw):
                raw = raw()
        except (RuntimeError, TypeError):
            return []
        return [w for w in (raw or []) if w is not self]

    def _target_at(self, pt: QPoint):
        """鼠标底下是不是另一张音乐卡片的播放键。"""
        for w in self._sibling_widgets():
            if not isinstance(w, MusicWidget) or not w.isVisible():
                continue
            snap = w.music.snap
            if not snap.get("has") or not snap.get("can_play"):
                continue
            try:
                center = w._play_global()
                dist = (pt - center).manhattanLength()
                radius = w._scale(28)
            except (RuntimeError, TypeError):
                continue
            if dist <= radius:
                return w
        return None

    def _hold_fired(self) -> None:
        snap = self.music.snap
        if self._linking or not (snap.get("has") and snap.get("can_play")):
            return
        self._linking = True
        self._ring = 2
        self._press_token = None          # 松手不再当成一次点击
        self._probe_logged = False
        self.update()
        try:
            from .link_line import show_line
            show_line(self._play_global(), QCursor.pos())
        except Exception:  # noqa: BLE001 - 画不出线也不能耽误连线本身
            pass

    def _link_update(self, pt: QPoint) -> None:
        target = self._target_at(pt)
        if target is not self._target_pm:
            old = self._target_pm
            self._target_pm = target
            if old is not None:
                try:
                    old._ring_hot = 0
                    old.update()
                except RuntimeError:
                    pass
            self._target_arm.stop()
            if target is not None:
                target._ring_hot = 1      # 先给一圈淡的：表示「能连，但要停一会儿」
                try:
                    target.update()
                except RuntimeError:
                    pass
                self._target_arm.start(LINK_HOVER_MS)
        try:
            from .link_line import show_line
            show_line(self._play_global(), pt,
                      hot=bool(target is not None and target._ring_hot == 2))
        except Exception:  # noqa: BLE001 - 画不出线也不能耽误连线本身
            pass

    def _target_armed(self) -> None:
        t = self._target_pm
        if t is None:
            return
        try:
            t._ring_hot = 2               # 停够了：圈变实，松手就接上
            t.update()
        except RuntimeError:
            return
        try:
            from .link_line import show_line
            show_line(self._play_global(), QCursor.pos(), hot=True)
        except Exception:  # noqa: BLE001 - 画不出线也不能耽误连线本身
            pass

    def _finish_pull(self) -> None:
        """松手：搭好了就让两边「一起按一下」，然后线收掉（不留任何长期关系）。"""
        try:
            from .link_line import hide_line
            hide_line()
        except Exception:  # noqa: BLE001 - 画不出线也不能耽误连线本身
            pass
        target, armed = self._target_pm, False
        if target is not None:
            armed = getattr(target, "_ring_hot", 0) == 2
            try:
                target._ring_hot = 0
                target.update()
            except RuntimeError:
                pass
        self._target_pm = None
        self._target_arm.stop()
        self._linking = False
        self._ring = 0
        self.update()
        if armed and target is not None:
            # 有人在放就一起停，都停着就一起放——只这一次
            want = not (bool(self.music.snap.get("playing"))
                        or bool(target.music.snap.get("playing")))
            dlog(f"music: 松手搭上 {target.card.id} want={want}")
            self.music.press_together(target.card.id, want)
        else:
            dlog(f"music: 松手没搭上 armed={armed} target"
                 f"={target.card.id if target is not None else '无'}")

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and not self._resize_mode:
            snap = self.music.snap
            glass = QPointF(self._glass_pos(event))
            if (snap.get("has") and snap.get("can_play")
                    and self._hit(self._btn_centers()["play"], self._scale(17), glass)):
                self._hold_pos = event.globalPosition().toPoint()
                self._ring = 1            # 按住期间先给一圈淡的
                self.update()
                self._hold_timer.start(HOLD_MS)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._linking:
            self._link_update(event.globalPosition().toPoint())
            event.accept()
            return
        pt = event.globalPosition().toPoint()
        if self._hold_timer.isActive() and self._hold_pos is not None:
            if (pt - self._hold_pos).manhattanLength() > 8:
                # 手动了：那是点一下或者拖卡片，不是长按
                self._hold_timer.stop()
                self._hold_pos = None
                self._ring = 0
                self.update()
        super().mouseMoveEvent(event)
        if self._window_dragging:
            return
        glass = QPointF(self._glass_pos(event))
        hover = None
        snap = self.music.snap
        centers = self._btn_centers()
        for name, ok in (("prev", snap.get("can_prev")),
                         ("play", snap.get("can_play")),
                         ("next", snap.get("can_next")),
                         ("mode", snap.get("can_mode"))):
            if ok and self._hit(centers[name], self._scale(17), glass):
                hover = name
                break
        if hover != self._hover_btn:
            self._hover_btn = hover
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        self._hold_timer.stop()
        self._hold_pos = None
        if self._linking:
            self._finish_pull()
            event.accept()
            return
        if self._ring:
            self._ring = 0
            self.update()
        super().mouseReleaseEvent(event)
