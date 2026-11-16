"""程序入口：桌面时钟挂件 + 收纳盒 + 拖拽合并侦测。"""
from __future__ import annotations

import faulthandler
import os
import sys
import tempfile
import time
import traceback
from datetime import date
from pathlib import Path

from PySide6.QtCore import (
    QAbstractAnimation, QEasingCurve, QLockFile, QObject, QParallelAnimationGroup,
    QPauseAnimation, QPoint, QPointF, QPropertyAnimation, QRect,
    QSequentialAnimationGroup, Qt, QTimer, QUrl,
)
from PySide6.QtGui import (
    QColor, QDesktopServices, QFont, QIcon, QLinearGradient, QPainter,
    QPen, QPixmap,
)
from PySide6.QtWidgets import QApplication

from .agenda import Agenda
from .dlog import dlog
from .i18n import set_lang as i18n_set_lang
from .i18n import tr
from .box_preview import BoxPreview
from .box_widget import (
    COLLAPSED, BoxWidget,
    SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOOWNERZORDER, SWP_NOSIZE,
    user32 as box_user32,
)
from .boxes import BoxStore, acceptable as boxable
from .calendar_widget import CalendarWidget
from .clock_widget import ClockWidget
from .config import (
    DATA_DIR, CardData, Config, clamp_alpha, ensure_dirs, norm_color,
)
from .countdown_widget import CountdownWidget
from .dragwatch import DragWatcher
from .glass import GLOBAL_QSS
from .music import MusicService
from .music_widget import MusicWidget
from .hotkey import (
    DEFAULT_PANEL, DEFAULT_TOGGLE, HotkeyWatcher, hotkey_text, normalize,
)
from .launcher import TaskbarButton
from .toast import Toast
from .todo_widget import TodoWidget
from . import aligner
from . import desktop_icons as di
from . import fullscreen
from . import ns_items
from . import screen
from . import signal as sig
from . import taskbar as tb
from . import weather as weather_mod


LOGO_PATH = Path(__file__).resolve().parent.parent / "assets" / "logo.png"


def make_app_icon(size: int = 64) -> QIcon:
    """程序图标：优先用 assets/logo.png（db 徽标），没有就退回画的时钟。"""
    src = QPixmap(str(LOGO_PATH)) if LOGO_PATH.exists() else QPixmap()
    if not src.isNull():
        icon = QIcon()
        for s in (16, 20, 24, 32, 40, 48, 64, 96, 128, 256):
            icon.addPixmap(src.scaled(s, s, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        return icon

    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0.0, QColor(96, 148, 255))
    grad.setColorAt(1.0, QColor(72, 96, 226))
    p.setPen(Qt.NoPen)
    p.setBrush(grad)
    p.drawRoundedRect(3, 3, size - 6, size - 6, size * 0.24, size * 0.24)
    c = size / 2.0
    p.setPen(QPen(QColor(255, 255, 255, 240), max(2.0, size * 0.05),
                  Qt.SolidLine, Qt.RoundCap))
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(QPointF(c, c), size * 0.27, size * 0.27)
    p.drawLine(QPointF(c, c), QPointF(c, c - size * 0.16))
    p.drawLine(QPointF(c, c), QPointF(c + size * 0.13, c + size * 0.03))
    p.end()
    return QIcon(pm)


def box_pos_on_icon(rect) -> tuple[int, int]:
    """收起的盒子中心对准目标图标中心，返回盒子左上角。

    用户要的就是「盒子出在被拖到的那个图标的位置上」。
    """
    cx = (int(rect[0]) + int(rect[2])) / 2.0
    cy = (int(rect[1]) + int(rect[3])) / 2.0
    return (int(round(cx - COLLAPSED / 2)), int(round(cy - COLLAPSED / 2)))


# 框选的容差（逻辑像素）：贴边的盒子要完整框住，框的边只能停在屏幕最后一像素，
# 差 1px 就永远框不住、鼠标一抖就闪（见 Controller._band_select）。
BAND_TOL = 4


class Controller(QObject):
    def __init__(self, app: QApplication, cfg: Config):
        super().__init__()
        self.app = app
        self.cfg = cfg
        # 语言要在建任何窗口之前设好：面板/卡片的文案是建的时候取的
        try:
            i18n_set_lang(getattr(cfg, "lang", "zh"))
        except Exception:  # noqa: BLE001
            pass
        self.store = BoxStore()
        self.windows: dict[str, BoxWidget] = {}
        # 布局：每次从「家」重算（见 _relayout）。`_in_layout` 防回调套回调，
        # `_layout_pass` 表示现在只在算逻辑坐标，窗口等算完统一滑过去。
        self._in_layout = False
        self._layout_pass = False
        self._push_level = 0
        self._relayout_more = False
        self.card_widgets: dict[str, ClockWidget] = {}
        self.settings = None
        # 撤销栈：按建盒顺序记盒子 id，面板「撤销上次建盒」一次弹一个
        self._undo: list[str] = []
        # 框选多选的窗口（收纳盒 + 卡片，键是 box:<id> / card:<id>）
        self.sel_items: set[str] = set()
        self._cards_hidden = False      # 全屏应用正开着，卡片全体收起来
        self._todo_day: date | None = None   # 今天的待办提醒报过了没有再报
        # 待办 / 纪念日：所有卡片共用这一份数据，改完通知所有卡片重画
        self.agenda = Agenda(self)
        self.agenda.changed.connect(self._agenda_changed)
        # 天气：后台线程抓，抓回来刷新所有卡片
        self.weather = weather_mod.WeatherService(self)
        self.weather.updated.connect(self._weather_updated)
        # 音乐：后台线程盯着 Windows 的媒体会话（当前在放什么、能不能控制）
        self.music = MusicService(self)

        self.watcher = DragWatcher()
        self.preview = BoxPreview()
        # 拖两个图标到一起时那个「合成预览」也用盒子的外观
        self.preview.glass_alpha = clamp_alpha(self.cfg.box_glass_alpha)
        self.preview.radius = float(self.cfg.box_radius)
        self.preview.glass_rgb = (QColor(self.cfg.box_color)
                                  if self.cfg.box_color else None)
        self.watcher.armed.connect(self._on_armed)
        self.watcher.clear.connect(self.preview.hide)
        self.watcher.merged.connect(self._on_merged)
        self.watcher.wheel_box.connect(self._on_desktop_wheel_box)
        self.watcher.band_started.connect(lambda _pt: self.clear_selection())
        self.watcher.band_moved.connect(self._band_select)
        self.watcher.band_finished.connect(self._band_select)

        for box in list(self.store.boxes):
            self._spawn(box)
        # 旧版本把「挤开的位置」也写进了配置，可能有几块的家是叠在一起的：
        # 等窗口真的 show 出来（事件循环转起来）再分开一次、再归一化一次
        QTimer.singleShot(400, self._startup_layout)

        # 和卡片同一套玻璃浓度（默认 212），换主题时一起跟着变
        self.toast = Toast(alpha=int(getattr(self.cfg, "glass_alpha", 212) or 212))
        # 任务栏上的入口（如果启用了）：自己画的那颗按钮
        self.button = None
        # 任务栏改成固定策略（面板里不再暴露开关）：
        #   总是去掉黑边（桌面时透明）；有窗口最大化时用深色实底
        self.cfg.taskbar_clear = True
        self.cfg.taskbar_maximized = "normal"
        self.cfg.save()
        self.apply_cards(animate=False)
        self._settle_cards()
        # 层级统一由这里管：时钟卡片在最下、收起的盒子在中间、展开了的盒子在最上
        self._z_timer = QTimer(self)
        self._z_timer.setInterval(1200)
        self._z_timer.timeout.connect(self.apply_z_order)
        self._z_timer.start()
        self.set_beautify_enabled(self.cfg.enabled, save=False, animate=False)
        self._start_signal_loop()
        self._start_taskbar_watch()
        self._start_hotkeys()
        # 全屏应用（全屏游戏 / 视频 / PPT）时把卡片全体藏起来
        self._fs_timer = QTimer(self)
        self._fs_timer.setInterval(700)
        self._fs_timer.timeout.connect(self._watch_fullscreen)
        self._fs_timer.start()
        # 点「显示桌面」会把桌面抬到所有窗口上面，贴着桌面的盒子/卡片就被盖住了
        self._desk_timer = QTimer(self)
        self._desk_timer.setInterval(700)
        self._desk_timer.timeout.connect(self._keep_above_desktop)
        self._desk_timer.start()
        # 天气：启动先抓一次，之后每 5 分钟看一眼（20 分钟内的缓存直接复用）
        self._weather_timer = QTimer(self)
        self._weather_timer.setInterval(5 * 60 * 1000)
        self._weather_timer.timeout.connect(self._refresh_weather)
        self._weather_timer.start()
        QTimer.singleShot(1500, self._refresh_weather)
        # 待办提醒：今天到期 / 已逾期，每天最多浮一条
        self._todo_timer = QTimer(self)
        self._todo_timer.setInterval(60 * 1000)
        self._todo_timer.timeout.connect(self._check_todos)
        self._todo_timer.start()
        QTimer.singleShot(4000, self._check_todos)
        # 提前初始化 COM：第一次弹系统右键菜单「转一会」主要就花在这儿
        try:
            from . import shell_menu
            shell_menu.warmup()
        except Exception:  # noqa: BLE001
            pass
        # 桌面范围的圆点拖尾指针（只在桌面/收纳盒上接管，别的窗口不受影响）
        self.cursor_overlay = None
        try:
            from .cursor import restore_system_cursor
            # 上一次如果被强杀，系统箭头可能还停在透明状态，先恢复一次
            restore_system_cursor()
        except Exception:  # noqa: BLE001
            pass
        self.set_cursor_dot(bool(self.cfg.cursor_dot))
        self._start_poller()
        self._start_stall_watch()
        # 面板第一次 Ctrl+Space 要现建整棵控件树，实测 350ms。空闲时先把它建好，
        # 第一次打开就只剩 show()。
        QTimer.singleShot(15000, self._prewarm_settings)
        # 打包版：还没装到这台电脑上就先问一句；装好之后提醒一次任务栏那颗
        self.setup_win = None
        self._install_prompt()

    # ------------------------------------------------------------ 安装 / 卸载
    def _install_prompt(self) -> None:
        """「装到这台电脑」只对打包版有意义；源码跑的时候这里什么都不做。"""
        try:
            from . import install as install_mod
            if install_mod.state() == "installed":
                QTimer.singleShot(6000, self._pin_hint)
                return
            if not install_mod.should_offer():
                return
            QTimer.singleShot(900, self.offer_install)
        except Exception:  # noqa: BLE001
            pass

    def offer_install(self) -> None:
        try:
            from . import install as install_mod
            if not install_mod.should_offer():
                return
            from .setup_window import SetupWindow
            self.setup_win = SetupWindow(radius=int(self.cfg.box_radius or 10))
            self.setup_win.show()
        except Exception:  # noqa: BLE001
            self.setup_win = None

    def _pin_hint(self) -> None:
        """任务栏那颗 Windows 只准人点（见 install.py），所以提醒一次。"""
        try:
            from . import install as install_mod
            marker = DATA_DIR / "pin_hint_seen"
            if marker.is_file():
                return
            marker.write_text("1", encoding="utf-8")
            self.toast.show_text(
                "右键开始菜单里的 desktop beauty → 固定到任务栏", 4200)
        except Exception:  # noqa: BLE001
            pass

    def _start_stall_watch(self) -> None:
        """主线程卡住超过 120ms 就把它的调用栈写进日志。

        查「鼠标一顿一顿」只靠计时器猜不出来：这里是唯一能直接指出「卡在哪个
        函数里」的办法。心跳用 20ms 的定时器推进，看门狗线程发现它不动了就
        抓 `sys._current_frames()` 里主线程的栈。

        平时不开：心跳 50 次/秒加一个 40ms 醒一次的线程，是纯开销，只有查卡顿
        的时候才值得付（`DESKBEAUTY_DEBUG=1` 启动一次 app 就会落盘）。
        """
        if not os.environ.get("DESKBEAUTY_DEBUG"):
            return
        import threading
        ident = threading.get_ident()
        self._beat = 0
        beat = QTimer(self)
        beat.setInterval(20)
        beat.timeout.connect(self._bump_beat)
        beat.start()
        self._beat_timer = beat

        def run() -> None:
            last = 0
            miss = 0
            while True:
                time.sleep(0.04)
                now_beat = self._beat
                if now_beat == last:
                    miss += 1
                else:
                    miss = 0
                    last = now_beat
                if miss >= 3:            # 120ms 没推进心跳
                    frame = sys._current_frames().get(ident)
                    stack = []
                    while frame is not None and len(stack) < 10:
                        stack.append("%s:%d(%s)"
                                     % (frame.f_code.co_filename.rsplit("\\", 1)[-1],
                                        frame.f_lineno, frame.f_code.co_name))
                        frame = frame.f_back
                    dlog("stall>120ms: " + " <- ".join(stack or ["(取不到栈)"]))
                    last = now_beat
                    miss = 0

        threading.Thread(target=run, daemon=True, name="stall-watch").start()

    def _bump_beat(self) -> None:
        self._beat += 1

    def _start_hotkeys(self) -> None:
        """按配置挂两条全局快捷键：打开/关闭面板、启用/停用美化。"""
        self.hotkeys = HotkeyWatcher(self, external=True)
        self.apply_hotkeys()
        _log(f"hotkeys ready: toggle={self.cfg.hotkey_toggle.get('text')} "
             f"panel={self.cfg.hotkey_panel.get('text')}")

    def apply_hotkeys(self) -> None:
        """（重新）登记两条快捷键：先全摘掉再按配置挂，改完立刻生效。"""
        hk = getattr(self, "hotkeys", None)
        if hk is None:
            return
        hk.release()
        toggle = normalize(self.cfg.hotkey_toggle, DEFAULT_TOGGLE)
        panel = normalize(self.cfg.hotkey_panel, DEFAULT_PANEL)
        self.cfg.hotkey_toggle = dict(toggle)
        self.cfg.hotkey_panel = dict(panel)
        hk.bind(toggle["mods"], toggle["vk"], self.toggle_beautify)
        hk.bind(panel["mods"], panel["vk"], self.open_settings)
        hk.install()

    def set_hotkey(self, which: str, mods: int, vk: int) -> None:
        """面板里改了组合键：which 是 "panel" / "toggle"。"""
        item = {"mods": int(mods), "vk": int(vk), "text": hotkey_text(mods, vk)}
        if which == "panel":
            self.cfg.hotkey_panel = item
        else:
            self.cfg.hotkey_toggle = item
        self.cfg.save()
        self.apply_hotkeys()
        _log(f"hotkey {which} -> {item['text']}")

    def reset_hotkeys(self) -> None:
        self.cfg.hotkey_panel = dict(DEFAULT_PANEL)
        self.cfg.hotkey_toggle = dict(DEFAULT_TOGGLE)
        self.cfg.save()
        self.apply_hotkeys()

    # ------------------------------------------------------------ 卡片
    def _card_widget(self, card):
        """按卡片类型造窗口：时钟 / 待办 / 日历 / 倒计时。"""
        makers = {
            "clock": lambda: ClockWidget(card, self.cfg.save,
                                         radius=self.cfg.card_radius),
            "todo": lambda: TodoWidget(card, self.cfg.save, self.agenda,
                                       radius=self.cfg.card_radius),
            "calendar": lambda: CalendarWidget(card, self.cfg.save, self.agenda,
                                               radius=self.cfg.card_radius),
            "countdown": lambda: CountdownWidget(card, self.cfg.save, self.agenda,
                                                 radius=self.cfg.card_radius),
            "music": lambda: MusicWidget(card, self.cfg.save,
                                         self.music.channel(card),
                                         radius=self.cfg.card_radius),
        }
        make = makers.get(str(getattr(card, "type", "clock")), makers["clock"])
        win = make()
        win.on_z_changed = self.apply_z_order
        win.on_closed = self._card_closed
        win.on_place_new = self._place_new_card
        win.siblings = self._siblings
        # 框选多选：卡片和收纳盒用同一套（一起压暗、一起搬）
        win.on_group = lambda w=win: self._group_for(w)
        win.on_widget_press = self._on_widget_press
        win.on_resize_mode = lambda on, w=win: self._cursor_block(w, on)
        win.on_resize_drag = lambda on, w=win: self._cursor_drag(w, on)
        win.auto_align = bool(getattr(self.cfg, "auto_align", True))
        if card.type == "clock":
            win.on_open_weather = lambda w: self.open_weather(w.card.city, w)
            try:
                win.set_weather_data(self.weather.get(card.city or ""))
            except (RuntimeError, TypeError):
                pass
        self._apply_card_look(win)
        return win

    def _agenda_changed(self) -> None:
        """待办 / 纪念日改了：每张卡片都重画一遍（没有的会忽略）。"""
        for win in self.card_widgets.values():
            try:
                win.refresh_agenda()
            except (RuntimeError, TypeError):
                continue
        # 刚加了「今天到期」的待办：下一分钟就该提醒，不用等到明天
        QTimer.singleShot(1500, self._check_todos)

    def _check_todos(self) -> None:
        """每天最多浮一次：今天到期 + 已经逾期的待办。"""
        today = date.today()
        if self._todo_day == today:
            return
        try:
            text = self.agenda.remind_text(today)
        except Exception:  # noqa: BLE001 - 提醒抽风不能连累别的定时器
            return
        if not text:
            return
        self._todo_day = today
        _log(f"todo remind: {text}")
        self.toast.show_text(text, 6000)

    def _cursor_drag(self, win, on: bool) -> None:
        """拖缩放控制点期间：圆点整体停掉，只留系统的缩放箭头。"""
        overlay = getattr(self, "cursor_overlay", None)
        if overlay is None:
            return
        try:
            overlay.set_suppressed(bool(on))
        except (RuntimeError, TypeError):
            pass

    def _cursor_block(self, win, on: bool) -> None:
        """卡片进改大小模式：这块窗口上不画圆点，露出系统的缩放箭头。"""
        overlay = getattr(self, "cursor_overlay", None)
        if overlay is None:
            return
        try:
            overlay.set_blocked(win.winId(), bool(on))
        except (RuntimeError, TypeError):
            pass

    def _cursor_register(self, win) -> None:
        """把窗口登记给圆点拖尾指针：鼠标在它上面时才接管光标。"""
        overlay = getattr(self, "cursor_overlay", None)
        if overlay is None:
            return
        try:
            if win.isVisible() or win.isHidden():
                overlay.register(win.winId())
        except (RuntimeError, TypeError):
            pass

    def apply_cards(self, animate: bool = True, settle: bool = True) -> None:
        """让窗口和配置里的卡片列表对上（增删/开关都在这里收口）。"""
        wanted = {c.id: c for c in self.cfg.cards}
        shown = False
        for cid in list(self.card_widgets):
            if cid not in wanted:
                win = self.card_widgets.pop(cid)
                win.close()
                win.deleteLater()
        # 卡片删/加了之后，音乐通道也跟着对一下：删掉的卡片不能再占着一个播放器
        music = getattr(self, "music", None)
        if music is not None:
            try:
                music.prune([c.id for c in self.cfg.cards
                             if str(getattr(c, "type", "")) == "music"])
            except (RuntimeError, TypeError):
                pass
        for cid, card in wanted.items():
            win = self.card_widgets.get(cid)
            if win is None:
                win = self._card_widget(card)
                self.card_widgets[cid] = win
            if card.enabled and self.cfg.enabled and not self._cards_hidden:
                if self._slide_show(win, animate):
                    shown = True
                self._cursor_register(win)
            else:
                if win.isVisible():
                    if animate:
                        self._slide_out(win)
                    else:
                        self._stop_slide(win)
                        win.hide()
        # 刚启用/新建的卡片可能正好压在别的卡片或盒子上：摆到空位去
        if shown and not animate and settle:
            self._settle_cards()
        self.apply_z_order()
        # show() 之后窗口才是 visible，下一帧再排一次，免得刚建的卡片停在最上面
        QTimer.singleShot(0, self.apply_z_order)

    # ------------------------------------------------------------ 开关滑动
    # 停用/启用都是「一排东西滑出去/滑回来」。以前有三个毛病，一起修掉：
    #   1. 左边半屏的只挪了自己一个宽度，停在屏幕里，然后 hide() 一下就没——看着是
    #      「啪」地消失；右边半屏按整屏宽度算，中间的盒子一帧要走几百像素，像瞬移。
    #   2. 所有盒子同一个时长、同一时刻起步，没有先来后到，像一整块板子平移。
    #   3. 滑到一半又切回来时，`_slide_home` 会被写成半路的位置，回来就停歪了。
    SLIDE_OUT_SPEED = 2.2          # px/ms：滑出去的限速（近的别拖、远的别飙）
    SLIDE_OUT_MS = (220, 360)
    SLIDE_IN_SPEED = 1.8           # 回来慢一点，落地更软
    SLIDE_IN_MS = (280, 470)
    SLIDE_STAGGER = 100            # 波的错开：越靠边的越早走，最多晚 100ms
    SLIDE_GAP = 3                  # 停到屏幕外面再多走 3px，保证一点都看不见

    def _slide_track(self, w):
        """(home, off, dist)：出去和回来共用这一份几何。

        家位置只在**静止**的时候更新（拖动、避让之后位置会变）；正滑到一半又切
        回来时沿用记着的家，不然回来会停在半路。
        """
        home = getattr(w, "_slide_home", None)
        busy = bool(getattr(w, "_slide_busy", False))
        # 只在「看得见、又没在滑」的时候把当前位置当作家（拖动、避让之后会变）。
        # 藏起来之后窗口停在屏幕外，那时候的位置不是家。
        if home is None or (w.isVisible() and not busy):
            home = QPoint(w.pos())
            w._slide_home = QPoint(home)
        r = (w.screen() or self.app.primaryScreen()).availableGeometry()
        if home.x() + w.width() / 2.0 < r.center().x():
            # 整个窗口（含四周透明余量）都到屏幕左边外面
            off = QPoint(r.left() - w.width() - self.SLIDE_GAP, home.y())
        else:
            off = QPoint(r.right() + 1 + self.SLIDE_GAP, home.y())
        return QPoint(home), off, abs(off.x() - home.x())

    def _slide_duration(self, dist: int, span, speed: float) -> int:
        lo, hi = span
        return int(min(hi, max(lo, dist / max(0.5, speed))))

    def _slide_delay(self, w, dist: int) -> int:
        """越靠边（走得越近）越先动，一层层推出去的感觉。"""
        try:
            width = max(1, (w.screen() or self.app.primaryScreen()).availableGeometry().width())
        except (RuntimeError, AttributeError):
            width = 1
        return int(min(self.SLIDE_STAGGER, dist * self.SLIDE_STAGGER / width))

    def _stop_slide(self, w) -> None:
        old = getattr(w, "_slide_anim", None)
        w._slide_busy = False
        w._slide_anim = None
        if old is not None:
            try:
                old.stop()
            except (RuntimeError, TypeError):
                pass

    def _slide_run(self, w, start: QPoint, end: QPoint, dur: int, delay: int,
                   curve, fade=None, on_done=None) -> None:
        """跑一段滑动：错开的延时 + 位移 + 透明度，合成**一个**动画，随时能整体 cancel。

        `fade=(起点, 终点)` 是窗口透明度（0/1）。透明度用**线性**：位置已经在缓动
        了，透明再叠一层缓动，两头会跟位移对不上，看着像各走各的。

        ⚠️ 这两个窗口本来就是 `WS_EX_LAYERED`（半透明玻璃），改 windowOpacity 只是
        乘一个 alpha，不会切渲染路径——实测 0.6/0.3/0 三档的颜色就是和壁纸线性混
        合（119,110,116 → 98,80,87 → 83,61,67 → 68,43,48），不会闪。
        """
        self._stop_slide(w)
        w._slide_busy = True
        body = QParallelAnimationGroup(w)
        move = QPropertyAnimation(w, b"pos", w)
        move.setDuration(int(dur))
        move.setEasingCurve(curve)
        move.setStartValue(start)
        move.setEndValue(end)
        body.addAnimation(move)
        if fade is not None:
            a0 = max(0.0, min(1.0, float(fade[0])))
            a1 = max(0.0, min(1.0, float(fade[1])))
            op = QPropertyAnimation(w, b"windowOpacity", w)
            op.setDuration(int(dur))
            op.setEasingCurve(QEasingCurve.Linear)
            op.setStartValue(a0)
            op.setEndValue(a1)
            body.addAnimation(op)
        group = QSequentialAnimationGroup(w)
        if delay > 0:
            group.addAnimation(QPauseAnimation(int(delay), group))
        group.addAnimation(body)

        def _done():
            w._slide_busy = False
            w._slide_anim = None
            if fade is not None:
                # 收尾兜一下：动画被中断时这里不会跑，所以半透明只可能停在半路，
                # 而停下之后一律要么全实（看得见）要么全透（已经藏起来）两种情况。
                try:
                    w.setWindowOpacity(0.0 if fade[1] < 0.5 else 1.0)
                except (RuntimeError, TypeError):
                    pass
            if on_done is not None:
                try:
                    on_done()
                except (RuntimeError, TypeError):
                    pass

        group.finished.connect(_done)
        w._slide_anim = group
        group.start(QAbstractAnimation.DeleteWhenStopped)

    def _slide_out(self, w) -> None:
        """停用：往最近的屏幕边滑出去，边滑边淡出，整块出了屏幕再藏起来。"""
        if not w.isVisible():
            return
        _home, off, _dist = self._slide_track(w)
        start = QPoint(w.pos())
        remain = abs(off.x() - start.x())
        busy = bool(getattr(w, "_slide_busy", False))
        self._slide_run(
            w, start, off,
            self._slide_duration(remain, self.SLIDE_OUT_MS, self.SLIDE_OUT_SPEED),
            0 if busy else self._slide_delay(w, remain),
            QEasingCurve.InCubic, fade=(w.windowOpacity(), 0.0), on_done=w.hide)

    def _slide_in(self, w) -> None:
        """启用：从屏幕外面滑回来，边滑边淡入。"""
        home, off, _dist = self._slide_track(w)
        visible = w.isVisible()
        if not visible:
            # 这两面旗子必须在 show() 之前立：showEvent 里「从 1px 长高」和
            # `_clamp_to_screen()` 都会在同一个事件里改几何，跟滑动抢窗口位置，
            # 表现出来就是启用时抖一下。
            w._skip_show_grow = True
            w._slide_busy = True
            w.move(off)
            w.setWindowOpacity(0.0)      # 全透明起步，滑进来的同时淡进来
            w.show()
            w._skip_show_grow = False
        start = QPoint(w.pos())
        remain = abs(home.x() - start.x())
        a0 = 0.0 if not visible else max(0.0, min(1.0, w.windowOpacity()))
        self._slide_run(
            w, start, home,
            self._slide_duration(remain, self.SLIDE_IN_MS, self.SLIDE_IN_SPEED),
            0 if visible else self._slide_delay(w, remain),
            QEasingCurve.OutQuint, fade=(a0, 1.0))

    def _slide_show(self, w, animate: bool) -> bool:
        """让窗口回到可见状态；**正在滑出去的也要拉回来**。

        以前这里只看 `isVisible()`：停用滑到一半（还没 hide）时又按启用，窗口
        仍然是「可见」的，于是被跳过；紧接着那次滑出动画收尾 `hide()` 掉——开关
        是启用的、桌面却空空，再也回不来（01:40 真被按出来过）。
        """
        if w.isVisible() and not getattr(w, "_slide_busy", False):
            return False
        if animate:
            self._slide_in(w)
            return True
        self._stop_slide(w)
        home = getattr(w, "_slide_home", None)
        if home is not None:
            w.move(home)
        try:
            w.setWindowOpacity(1.0)
        except (RuntimeError, TypeError):
            pass
        w.show()
        return True

    def add_clock_card(self) -> None:
        card = CardData(scale=1.0, opacity=1.0, note="新卡片")
        self.cfg.cards.append(card)
        self.cfg.save()
        self.apply_cards()

    # 卡片种类：时钟、待办 / 日程、日历、倒计时（文案跟着界面语言走）
    @property
    def CARD_KINDS(self) -> dict:
        return {k: tr(f"kind.{k}")
                for k in ("clock", "todo", "calendar", "countdown", "music")}

    CARD_READY = ("clock", "todo", "calendar", "countdown", "music")

    def add_card(self, kind: str = "clock") -> str | None:
        """面板「添加卡片」：排在已有卡片那一列的最下面，放不下就往左开新列。"""
        if kind not in self.CARD_READY:
            return None
        card = CardData(type=kind, scale=1.0, opacity=1.0)
        self.cfg.cards.append(card)
        # 摆位置是「锦上添花」：它要是抽风了，卡片也得留下、配置也得存下来，
        # 面板列表更不能因为一个异常就停在旧数据上（用户报过「加了卡片面板里没有」）
        try:
            # 先把新卡片摆到空位，再收拾别人：反过来的话新卡片会先占住
            # 右上角、把本来站在那儿的卡片挤走（用户看到的就是「加卡片把布局弄乱了」）
            self.apply_cards(animate=False, settle=False)
            win = self.card_widgets.get(card.id)
            if win is not None:
                self._place_new_card(win)
            self._settle_cards()
        except Exception as exc:  # noqa: BLE001
            _log(f"card add place failed: {exc!r}")
        self.cfg.save()
        _log(f"card added: {card.id} ({kind})")
        return card.id

    def _obstacles(self, exclude=None) -> list:
        """所有挡路的东西：别的卡片、所有盒子、桌面图标（玻璃矩形，逻辑像素）。"""
        rects = []
        for w in list(self.card_widgets.values()) + list(self.windows.values()):
            if w is exclude:
                continue
            try:
                if not w.isVisible():
                    continue
                rects.append(aligner.glass_rect(w))
            except (RuntimeError, TypeError):
                continue
        try:
            rects += [QRect(*screen.to_logical((r[0], r[1], r[2], r[3])))
                      for r in di.list_rects().values()]
        except Exception:  # noqa: BLE001 - 桌面接口偶尔抽风，不挡摆放
            pass
        return rects

    def _settle_cards(self) -> None:
        """启动 / 刚启用卡片时，把压住别人（卡片、盒子、桌面图标）的卡片挪开。"""
        moved = False
        for card in list(self.cfg.cards):
            win = self.card_widgets.get(card.id)
            if win is None or not win.isVisible():
                continue
            try:
                mine = aligner.glass_rect(win)
                # 没落过位置的卡片（新加的 / 以前崩在半路的）：把现在的位置存下来
                if card.x is None or card.y is None:
                    card.x, card.y = int(mine.x()), int(mine.y())
                    moved = True
                if any(mine.intersects(r) for r in self._obstacles(exclude=win)):
                    # 就近挪：卡片本来就在这儿，只是压着别人，别甩到屏幕别处去
                    self._place_new_card(win, mine.topLeft())
                    moved = True
            except (RuntimeError, TypeError, ValueError):
                # 单张卡片出问题不能连累整个 apply_cards（会留下「桌面有、面板没有」）
                continue
        if moved:
            self.cfg.save()
            _log("cards settled: overlaps fixed")

    def _place_new_card(self, win, near: QPoint | None = None) -> None:
        """找一块不压住任何卡片 / 盒子 / 桌面图标的空地放卡片。

        near 有值时（拖完松手发现避不开）：从**松手的地方**往外找最近的一块，
        贴着旁边的卡片/盒子对齐。以前是从右上角往下扫，卡片只要放不进那道
        缝里就会被甩到屏幕另一头的空档——看着就是「自己跳回原位了」。
        """
        scr = (win.screen() or self.app.primaryScreen()).availableGeometry()
        m = int(win.inset)
        gap, margin = 12, 24
        gw, gh = win.width() - 2 * m, win.height() - 2 * m
        others = self._obstacles(exclude=win)

        def fits(x: int, y: int) -> bool:
            mine = QRect(x, y, gw, gh)
            return not any(mine.adjusted(-gap, -gap, gap, gap).intersects(r)
                           for r in others)

        def put(x: int, y: int) -> None:
            win.move(x - m, y - m)
            win.card.x, win.card.y = x, y

        xmin, xmax = scr.left(), scr.right() + 1 - gw
        ymin, ymax = scr.top(), scr.bottom() + 1 - gh

        if near is not None and xmax >= xmin and ymax >= ymin:
            nx = min(max(int(near.x()), xmin), xmax)
            ny = min(max(int(near.y()), ymin), ymax)
            if fits(nx, ny):
                put(nx, ny)
                return
            # 候选位置 = 贴着每个障碍物的四条边 / 贴着屏幕边
            xs, ys = {nx, xmin, xmax}, {ny, ymin, ymax}
            for r in others:
                xs.add(r.left() - gap - gw)
                xs.add(r.right() + gap + 1)
                ys.add(r.top() - gap - gh)
                ys.add(r.bottom() + gap + 1)
            cands = []
            for x in xs:
                if x < xmin or x > xmax:
                    continue
                for y in ys:
                    if y < ymin or y > ymax:
                        continue
                    cands.append((abs(x - nx) + abs(y - ny), x, y))
            cands.sort()
            # 「整个飞回拖出来之前的位置」是用户最烦的一种：卡在两三张卡中间松手，
            # 明明只差一点点就能贴边站好，它却回到原位。所以只要松手点离原位超过
            # 一点点，就把原位从候选里排掉，优先贴到最近的边上；真没别的地方了才回去。
            # 刚加进来、还没落过位置的卡片，card.x/y 是 None（以前这里直接
            # int(None) 抛异常，于是「添加卡片」只加到了桌面、面板列表没刷新）
            ox = win.card.x if win.card.x is not None else win.x() + m
            oy = win.card.y if win.card.y is not None else win.y() + m
            origin = (int(ox), int(oy))
            far = (abs(nx - origin[0]) + abs(ny - origin[1])) > 40

            def at_origin(x: int, y: int) -> bool:
                return abs(x - origin[0]) <= 2 and abs(y - origin[1]) <= 2

            for skip_origin in ((True, False) if far else (False,)):
                for _dist, x, y in cands[:600]:
                    if skip_origin and at_origin(x, y):
                        continue
                    if fits(x, y):
                        put(x, y)
                        return
        # 兜底（新加的卡片、实在没地方）：从右上角往下扫，再往左一列
        x = scr.right() + 1 - margin - gw
        y = scr.top() + margin
        for _ in range(160):
            if fits(x, y):
                put(x, y)
                return
            hit = next((r for r in others
                        if QRect(x, y, gw, gh).adjusted(-gap, -gap, gap, gap)
                        .intersects(r)), None)
            y = (hit.bottom() if hit is not None else y + gh) + gap + 1
            if y + gh > scr.bottom() + 1 - margin:
                x -= gw + gap
                y = scr.top() + margin
                if x < scr.left() + margin:
                    break
        x = max(scr.left() + margin, x)
        y = scr.top() + margin
        put(x, y)

    def set_card_enabled(self, card_id: str, on: bool) -> None:
        for c in self.cfg.cards:
            if c.id == card_id:
                c.enabled = bool(on)
        self.cfg.save()
        self.apply_cards(animate=False)

    def _card_closed(self, win) -> None:
        """卡片自己的菜单里点了「关闭卡片」：把它真的收掉，面板那一行也要改。

        以前只改了 card.enabled + 存盘 + hide()，面板没收到通知，列表里那一行
        还写着「已启用 / 关闭」，用户以为没关掉（面板是按自己的数据画的）。
        """
        self.cfg.save()
        self.apply_cards(animate=False)
        panel = getattr(self, "settings", None)
        if panel is not None:
            try:
                panel.refresh_cards()
            except (RuntimeError, TypeError):
                pass
        _log(f"card closed from menu: {getattr(win.card, 'id', '?')}")

    def set_card_seconds(self, card_id: str, on: bool) -> None:
        """时钟卡片是否读秒（卡片上立刻改，面板里的数值随手存）。"""
        self.set_card_option(card_id, "show_seconds", bool(on))

    def set_card_timezone(self, card_id: str, tz_id: str) -> None:
        """换时区：传 IANA id（Asia/Shanghai…），空字符串 = 跟随本机。"""
        self.set_card_option(card_id, "timezone", (tz_id or "").strip())

    def set_card_option(self, card_id: str, key: str, value) -> None:
        """改卡片上的某一项设置，卡片当场跟着变，然后落盘。"""
        card = next((c for c in self.cfg.cards if c.id == card_id), None)
        if card is None or not hasattr(card, key):
            return
        setattr(card, key, value)
        win = self.card_widgets.get(card_id)
        if win is not None:
            apply = {
                "show_seconds": getattr(win, "set_seconds", None),
                "timezone": getattr(win, "set_timezone", None),
                "hour24": getattr(win, "set_hour24", None),
                "lunar": getattr(win, "set_lunar", None),
                "face": getattr(win, "set_face", None),
                "font": getattr(win, "set_font", None),
                "weather": getattr(win, "set_weather", None),
                "city": getattr(win, "set_city", None),
                "opacity": getattr(win, "set_opacity", None),
                "todo_show_done": getattr(win, "set_show_done", None),
                "cal_monday": getattr(win, "set_monday_first", None),
                "cal_lunar": getattr(win, "set_lunar_labels", None),
                "countdown_pin": getattr(win, "set_pin", None),
            }.get(key)
            if apply is not None:
                apply(value)
            if key == "city":
                # 换城市立刻抓一次，抓回来 _weather_updated 会把图换掉
                self.weather.refresh([str(value or "")], force=True)
            elif key == "weather" and value:
                self.weather.refresh([card.city or ""])
        self.cfg.save()

    def open_weather(self, city: str, win=None) -> None:
        """点卡片右边的天气块：弹天气预报。"""
        from .weather_dialog import WeatherDialog
        parent = self.settings if getattr(self, "settings", None) is not None else None

        def use_auto(name: str) -> None:
            """「重新定位」成功：这张卡片从「钉死某个城市」改回自动定位。"""
            if win is None or not getattr(win, "card", None):
                return
            try:
                win.card.city = ""
                win.set_weather_data(self.weather.get(""))
                self._weather_updated()
            except (RuntimeError, TypeError, AttributeError):
                return
            self.cfg.save()
            _log(f"weather: card {win.card.id} switched to auto locate ({name})")

        WeatherDialog.show_for(city or "", self.weather,
                               bool(getattr(self.cfg, "panel_dark", False)),
                               parent, use_auto)

    def _weather_updated(self) -> None:
        for card in self.cfg.cards:
            win = self.card_widgets.get(card.id)
            if win is None:
                continue
            try:
                win.set_weather_data(self.weather.get(card.city or ""))
            except (RuntimeError, TypeError):
                continue

    def _refresh_weather(self) -> None:
        cities = [(c.city or "") for c in self.cfg.cards
                  if c.type == "clock" and getattr(c, "weather", True)]
        if cities:
            self.weather.refresh(cities)

    def set_hide_fullscreen(self, on: bool) -> None:
        """全屏应用时自动隐藏所有卡片（全局设置）。"""
        self.cfg.hide_fullscreen = bool(on)
        self.cfg.save()
        if not self.cfg.hide_fullscreen and self._cards_hidden:
            self._cards_hidden = False
            self.apply_cards(animate=False)

    def _watch_fullscreen(self) -> None:
        wanted = bool(getattr(self.cfg, "hide_fullscreen", True)) \
            and fullscreen.foreground_is_fullscreen()
        if wanted == self._cards_hidden:
            return
        self._cards_hidden = wanted
        _log(f"fullscreen: cards hidden={wanted}")
        self.apply_cards(animate=False)

    def remove_card(self, card_id: str) -> None:
        self.cfg.cards = [c for c in self.cfg.cards if c.id != card_id]
        self.cfg.save()
        self.apply_cards(animate=False)
        _log(f"card removed: {card_id}")

    def card_label(self, card) -> str:
        """面板里显示的名字：同类型有多张时编个号（时钟卡片 1 / 2）。"""
        name = self.CARD_KINDS.get(card.type, card.type)
        same = [c for c in self.cfg.cards if c.type == card.type]
        if len(same) > 1:
            try:
                name = f"{name} {same.index(card) + 1}"
            except ValueError:
                pass
        return name

    def toggle_beautify(self) -> None:
        """桌面图标单击：开 / 关。"""
        self.set_beautify_enabled(not self.cfg.enabled)

    def set_beautify_enabled(self, on: bool, save: bool = True,
                             animate: bool = True) -> None:
        self.cfg.enabled = bool(on)
        if save:
            self.cfg.save()
        self.apply_cards(animate=animate)
        for win in self.windows.values():
            if on:
                self._slide_show(win, animate)
            else:
                if win.isVisible():
                    if animate:
                        self._slide_out(win)
                    else:
                        self._stop_slide(win)
                        win.hide()
        # 总开关关掉时把收纳手势也停掉（钩子也一起摘了）
        if on:
            if not getattr(self, "_watcher_running", False):
                self.watcher.start()
                self._watcher_running = True
        else:
            self.watcher.stop()
            self._watcher_running = False
            self.preview.hide()
        self._sync_ui()

    def _prewarm_settings(self) -> None:
        """趁没人用的时候把面板建出来。

        碰到正在拖图标 / 盒子展开着 / 全屏游戏，就往后挪 5 秒再来——建面板那
        350ms 卡在这一刻会被当成「拖拽一顿」。
        """
        if self.settings is not None or not self.cfg.enabled:
            return
        from .box_widget import is_dragging
        busy = (self._cards_hidden or is_dragging()
                or any(w.is_expanded for w in self.windows.values()))
        if busy:
            QTimer.singleShot(5000, self._prewarm_settings)
            return
        from .settings import SettingsWindow
        self.settings = SettingsWindow(self)

    def open_settings(self) -> None:
        """Ctrl+Space：面板没开就居中打开，已经开着就直接关掉。

        原来每次调用都先 center_on_screen()，用户把面板拖到别处之后再按一下，
        面板会先瞬移回屏幕中间再关，很怪。现在开着就等于「收起」。
        """
        from .settings import SettingsWindow
        if self.settings is None:
            self.settings = SettingsWindow(self)
        if self.settings.isVisible():
            self.settings.close()
            return
        # 面板关着的这段时间可能建过/拆过盒子：打开时按当前数据重画一遍
        self.settings.sync_state()
        self.settings.center_on_screen()
        self.settings.show()
        self.settings.raise_()
        self.settings.activateWindow()

    # ------------------------------------------------------------ 收纳盒
    def _spawn(self, box) -> BoxWidget:
        win = BoxWidget(self.store, box)
        win.changed.connect(self.store.save)
        win.removed.connect(self._on_box_removed)
        win.warn.connect(lambda text: self.toast.show_text(text, 3200))
        win.on_z_changed = self.apply_z_order
        win.on_toast = lambda text: self.toast.show_text(text, 2200)
        win.on_merge = self.merge_boxes
        win.on_merge_group = self.merge_group
        # 面板里的「两个收起状态的收纳盒也能合并」：默认关（横杠太细，容易误并）
        win.allow_bar_merge = bool(getattr(self.cfg, "box_merge_bar", False))
        win.on_new_box = self._on_new_box
        win.on_new_box_from_selection = self._on_new_box_from_selection
        win.on_drag_preview = self._on_drag_preview
        win.on_grow_request = self._grow_request
        win.on_shrunk = self._on_shrunk
        win.on_collapsed = lambda _w: self._relayout()
        # 展开（1）：正下方挤开、侧面左右推开；勾常驻展开之前先问让不让得开
        win.on_expanded = self._expand_room
        win.on_pin_room = self._pin_check
        win.on_grew = self._after_grow
        win.on_group = lambda w=win: self._group_for(w)
        win.on_widget_press = self._on_widget_press
        # 展开时要把「压住的那些窗口」糊掉，得能拿到所有兄弟窗口
        win.siblings = self._siblings
        # 面板里的「自适应对齐」开关：以前只设到卡片上，盒子永远当它是开的，
        # 于是关掉开关盒子照样被推走（用户以为开关坏了）
        win.auto_align = bool(getattr(self.cfg, "auto_align", True))
        self._apply_box_look(win)
        win.show()
        self.windows[box.id] = win
        self._cursor_register(win)
        if getattr(box, "pinned", False):
            QTimer.singleShot(30, lambda: win.toggle(True))
        return win

    def _siblings(self):
        return list(self.windows.values()) + list(self.card_widgets.values())

    def _keep_above_desktop(self) -> None:
        """「显示桌面」之后把盒子/卡片重新摆回桌面上面。

        那个按钮不是把窗口最小化，而是把 Progman（桌面）抬到最上层——我们的
        窗口本来就贴着桌面放，于是一起被盖住。发现自己在 Progman 后面就把整串
        窗口重新插回桌面正上方。
        """
        try:
            progman = box_user32.FindWindowW("Progman", None)
        except OSError:
            return
        if not progman:
            return
        wins = self._z_windows()
        hwnds = []
        for w in wins:
            try:
                hwnds.append(int(w.winId()))
            except (RuntimeError, TypeError):
                continue
        if not hwnds:
            return
        chain = []
        h = box_user32.GetTopWindow(None)
        while h and len(chain) < 600:
            chain.append(h)
            if h == progman:
                break
            h = box_user32.GetWindow(h, 2)
        idx = {h: i for i, h in enumerate(chain)}
        prog_i = idx.get(progman)
        if prog_i is None:
            return
        if not any(idx.get(h, 1 << 30) > prog_i for h in hwnds):
            return
        # 想插到 Progman 上面：不能拿 Progman 当锚点（那是插到它下面），
        # 要插到「Progman 上方那个窗口」的下面
        try:
            anchor = int(box_user32.GetWindow(progman, 3)) or 0     # GW_HWNDPREV
        except OSError:
            anchor = 0
        for w in wins:
            try:
                hwnd = int(w.winId())
                box_user32.SetWindowPos(
                    hwnd, anchor, 0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER)
            except (RuntimeError, TypeError, OSError):
                continue
        _log("desktop level restored")

    def _slide_widget_to(self, win, target: int, dur: int = 220,
                         axis: str = "y") -> None:
        """卡片没有 slide_to：用属性动画挪窗口（挤开、吸附共用，横竖都走这里）。"""
        old = getattr(win, "_push_anim", None)
        if old is not None:
            try:
                old.stop()
            except (RuntimeError, TypeError):
                pass
        start = win.pos()
        if axis == "y":
            if abs(start.y() - target) < 1:
                return
            end = QPoint(start.x(), int(target))
        else:
            if abs(start.x() - target) < 1:
                return
            end = QPoint(int(target), start.y())
        anim = QPropertyAnimation(win, b"pos", self)
        anim.setDuration(dur)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setStartValue(start)
        anim.setEndValue(end)
        win._push_anim = anim
        anim.start(QAbstractAnimation.DeleteWhenStopped)

    # ------------------------------------------------------------ 挤开 / 吸附
    SETTLE_GAP = 10

    def _flat_rect(self, w) -> QRect:
        """盒子/卡片现在在屏幕上占的那块**玻璃**区域（展开的盒子按展开尺寸算）。"""
        if hasattr(w, "collapsed_rect"):
            if getattr(w, "is_expanded", False):
                m = w._win_margin()
                return w._expanded_rect().adjusted(m, m, -m, -m)
            return w.collapsed_rect()
        return aligner.glass_rect(w)

    def _label(self, other, is_box: bool) -> str:
        """日志里认人用的短名字（盒子写名字，卡片写类型）。"""
        try:
            if is_box:
                name = str(getattr(other.box, "name", "") or "")
                return name or other.box.id[:8]
            return str(getattr(getattr(other, "card", None), "type", "") or "card")
        except (RuntimeError, TypeError, AttributeError):
            return "?"

    def _ident(self, other, is_box: bool) -> str:
        """盒子/卡片在配置里的 id（挤开的记录按它认人）。"""
        if is_box:
            return other.box.id
        return getattr(getattr(other, "card", None), "id", "")

    def _snapshot(self, win) -> list[tuple]:
        """别的盒子/卡片现在占的玻璃矩形：[(widget, 是不是盒子, QRect)]（不含 win）。"""
        out = []
        for other in self._siblings():
            try:
                if other is win or not other.isVisible():
                    continue
                out.append((other, hasattr(other, "collapsed_rect"),
                            self._flat_rect(other)))
            except (RuntimeError, TypeError, AttributeError):
                continue
        return out

    def _rects_except(self, skip, virt=None) -> list[QRect]:
        """除 skip 之外所有人的玻璃矩形；virt 里是「已经挪走、还没动画落地」的位置。"""
        out = []
        for w in self._siblings():
            try:
                if not w.isVisible() or any(w is s for s in skip):
                    continue
                known = virt.get(id(w)) if virt else None
                out.append(known if known is not None else self._flat_rect(w))
            except (RuntimeError, TypeError, AttributeError):
                continue
        return out

    def _free_at(self, rect: QRect, others, gap: int, axis: str, value: int) -> bool:
        """沿某个轴挪到 value 之后，那块地方是空的吗（离别人至少留一个空隙）。

        为什么需要它：`aligner.stop_x / stop_y` 是「一路滑到撞上为止」的语义，
        **不允许从别的盒子上面越过去**。可一排邻居是一起往外让的，排在第二位的
        那个最终位置其实在前面那位的外侧——用 stop_* 判断会一路停在它面前，看着
        就是「展开了却不让开」或者「收起时第一个回去了、第二个卡住不回来」。
        """
        moved = (rect.translated(value - rect.left(), 0) if axis == "x"
                 else rect.translated(0, value - rect.top()))
        probe = moved.adjusted(-gap, -gap, gap, gap)
        for other in others:
            try:
                if probe.intersects(other):
                    return False
            except (TypeError, AttributeError):
                continue
        return True

    def _axis_pos(self, w, is_box: bool, axis: str) -> int:
        """某一块在某个轴上的位置（**玻璃坐标**）。

        盒子往上长的时候 `box.y` 是不动的，玻璃在 `box.y - _up`（底边钉住，
        见 `BoxWidget._glass_top`）。挤开/吸回全部按玻璃坐标算，所以这里要减掉
        `_up`——不然一个往上长过的盒子会被当成还在下面 154px 处（用户报的
        「展开 A 却把游戏启动收成横杠」就是这里算错了 cur）。
        """
        if is_box:
            if axis == "y":
                return int(w.box.y) - int(getattr(w, "_up", 0) or 0)
            return w.box.x
        return w.card.y if axis == "y" else w.card.x

    def _axis_move(self, w, is_box: bool, axis: str, value: int) -> None:
        """沿一个轴挪过去（**玻璃坐标**），带滑动动画。

        `_layout_pass` 期间只改逻辑坐标（`box.x/box.y`、`card.x/card.y`），
        窗口一个像素都不动——布局是一次算完再统一滑过去的（见 `_relayout`）。
        """
        if self._layout_pass:
            if is_box:
                if axis == "y":
                    w.box.y = int(value) + int(getattr(w, "_up", 0) or 0)
                else:
                    w.box.x = int(value)
            else:
                if axis == "y":
                    w.card.y = int(value)
                else:
                    w.card.x = int(value)
            return
        if is_box:
            if axis == "y":
                # value 是玻璃 y；box.y 得把往上长的偏移加回来
                w.box.y = int(value) + int(getattr(w, "_up", 0) or 0)
                w.store.move(w.box, w.box.x, w.box.y)   # 只是显示位置，家不动
                w.slide_to(int(value))
            else:
                w.box.x = value
                w.store.move(w.box, w.box.x, w.box.y)
                w.slide_x_to(value)
        else:
            inset = int(getattr(w, "inset", 0) or 0)
            if axis == "y":
                w.card.y = value
                self.cfg.save()
                self._slide_widget_to(w, value - inset)
            else:
                w.card.x = value
                self.cfg.save()
                self._slide_widget_to(w, value - inset, axis="x")

    def _zone(self, r: QRect, target: QRect, base: QRect | None = None) -> str:
        """这一块相对「盒子要长成的那块」(target) 算什么：

        `below` 正下方 / `side` 正侧面 / `above` 正上方 / `skip` 侧下方（不管）。

        用户 2026-11-15 定的几块：
          * **正下方**：横向中心落在盒子自己身上（`base` = 现在这份收起状态）→ 往下让；
          * **正侧面**：横向中心在盒子左右两边、而且**上沿在盒子下沿之上**（同一排）
            → 往左右推；
          * **正上方**：整块在盒子上沿之上、又被「往上抬着展开」的那块盖住 → 往上推
            （只有底部放不下、盒子往上抬/展开面往上长的时候才出现）；
          * **侧下方**（斜着那一块）：被展开面盖住、又不在上面几块的 → **一律不动**，
            盖住 + 模糊就行。用户原话「侧边空间不够的状态下要只能推动正下方，侧下方
            不要推」。

        历史：更早是拿「横向压掉**展开面**一半以上宽度」判下方，展开面一宽就把明明
        在右边的邻居判成下方（用户报的「外设控制怎么推到下面去了」）。
        """
        if base is None:
            base = target
        if r.top() >= target.bottom() - 1:
            return "below"          # 整个在展开面下面：down 那套自己会判成不用动
        if r.bottom() <= target.top() + 1:
            return "skip"           # 整个在上面
        if r.bottom() <= base.top() + 1:
            return "above"          # 整个在盒子上沿之上：往上推开
        # 只是**往下长高**（宽度没变，比如横杠 → 0 状态）：这时候没有「往旁边让」
        # 这条路可走，展开面下面压着的东西一律往下让，斜着错开的也算。
        # 不这么判的话，边界正好斜着交叠的两块（用户报的「视听变回正常就压在编程
        # 身上」）会落到「侧下方不推」里，只能盖住。
        if target.width() <= base.width() + 1 and r.top() >= base.top() - 1:
            return "below"
        cx = (r.left() + r.right()) / 2.0
        if base.left() <= cx <= base.right():
            return "below"
        if r.top() < base.bottom():
            return "side"
        return "skip"

    def _is_below(self, r: QRect, target: QRect, base: QRect | None = None) -> bool:
        """兼容老接口：只有「正下方」那一块算 True（侧下方也是 False）。"""
        return self._zone(r, target, base) == "below"

    def _split(self, win, target, items, base=None) -> tuple[list, list, list]:
        """按 `_zone` 把「横向碰得到展开面」的东西分成 正下方 / 正侧面 / 正上方 三拨。"""
        if base is None:
            try:
                base = win.collapsed_rect()
            except (RuntimeError, TypeError, AttributeError):
                base = target
        below: list[tuple] = []
        side: list[tuple] = []
        above: list[tuple] = []
        for other, is_box, rect in items:
            if rect.right() < target.left() or rect.left() > target.right():
                continue
            zone = self._zone(rect, target, base)
            if zone == "below":
                below.append((other, is_box, rect))
            elif zone == "side":
                side.append((other, is_box, rect))
            elif zone == "above":
                above.append((other, is_box, rect))
        return below, side, above

    def _virt_items(self, items, moves) -> list[tuple]:
        """把「已经决定往下让开」的那几块换成让开之后的矩形（只算，不动）。"""
        moved = {id(other): ny for other, _is_box, ny in moves}
        out = []
        for other, is_box, rect in items:
            ny = moved.get(id(other))
            out.append((other, is_box,
                        rect if ny is None else rect.translated(0, ny - rect.top())))
        return out

    # ------------------------------------------- 布局：每次从「家」重算整张桌面
    def _relayout(self, win=None) -> bool:
        """把所有人摆回自己的家，再让占地方的那几个把别人推开。

        每一块的位置都是**从家算出来的**，不是「上次推到哪儿、再按记录吸回去」。
        以前是记录 + 吸回：只要有一环没吸回去（你亲手拖过、路上被挡了一下、连着
        展开两个盒子、或者那一步正好在动画中间），记录就和实际对不上，位置越推
        越歪、再也回不到原位（用户报的「各种怪异」）。现在挤开只是这一次的临时
        结果，收起时按家重算一遍，必然回得去。

        `win`：刚变大的那个盒子（横杠 → 正常 / 拖进图标变高）。它自己那块也算
        占地方，别人要给它让开；让不开返回 False，调用方抖两下。
        """
        if self._in_layout:
            # 回调套回调（收横杠 → on_shrunk 之类）：这一轮算完就够了
            self._relayout_more = True
            return True
        from .box_widget import is_dragging
        if is_dragging():
            # 正在拖盒子：位置由鼠标说了算，这时候重排会把它从手里拽走
            return True
        self._in_layout = True
        ok = True
        # 「展开时把挡住的推开」按等级一条一条开（见 Config.push_rules）：
        # 0 = 只盖住、谁都不动；1 = 正下方；2 = +正侧面；3 = +往上；4 = +临时收横杠。
        # 每级都过了压力测试才往上升（用户 2026-11-16 定的：一次只加一条）。
        try:
            level = int(getattr(self.cfg, "push_rules", 0) or 0)
        except (TypeError, ValueError):
            level = 0
        level = max(0, min(5, level))
        self._push_level = level
        align_ok = level >= 1
        try:
            self._layout_pass = True
            boxes = [w for w in self.windows.values() if w.isVisible()]
            cards = [c for c in self.card_widgets.values() if c.isVisible()]
            if not boxes:
                return False
            # 动画起点：现在显示在哪儿（这一步只改逻辑坐标，窗口还没动）
            disp = {id(w): (int(w.box.x), int(w.box.y)) for w in boxes}
            disp_card = {id(c): (int(c.card.x or 0), int(c.card.y or 0))
                         for c in cards}
            # 1) 全部回自己的家；上次挤开时临时收成横杠的先按原样算
            for w in boxes:
                w.box.x, w.box.y = int(w.box.home_x), int(w.box.home_y)
                w._up = 0
                if getattr(w, "_tmp_bar", False):
                    w._tmp_bar = False
                    w._bar = False
            # 2) 展开的面板先占位；开着「挤开」才把挡路的推开
            for w in sorted((x for x in boxes if x.is_expanded),
                            key=lambda x: (float(x.box.created or 0.0), x.box.id)):
                if align_ok:
                    got = self._panel_for(
                        w, allow_collapse=not bool(getattr(w.box, "pinned", False)))
                else:
                    got = self._panel_free(w)
                if got is None:
                    continue
                want, side, up, plan = got
                try:
                    w._expand_side = side
                    w._grew_up = bool(up)
                    w.set_up(int(w.box.y) - int(want.top()))
                except (RuntimeError, TypeError, AttributeError):
                    continue
                _log(f"grow: expand {w.box.name} side={side} up={up} at {want.left()},{want.top()}")
                if align_ok and plan is not None:
                    self._apply_plan(w, want, plan)
            # 3) 刚变大的那个：它自己那块也算占地方
            if win is not None and win in boxes and not win.is_expanded:
                from .box_widget import TITLE_H
                # 横杠 → 正常这一步发生在 `_set_bar` 把 `_bar` 改掉**之前**，
                # 所以不能用 `_collapsed_h()`（那还是横杠的 28），要按长完的高度算
                h_after = (int(win._side_h) + TITLE_H if getattr(win, "_bar", False)
                           else int(win._collapsed_h()))
                r = QRect(int(win.box.home_x), int(win.box.home_y),
                          int(win._side_w), h_after)
                # 长完之后可能顶出屏幕（比如家就在最下面、它又变高了）：先夹回来，
                # 夹完的位置就是它这次显示的位置（收起时还是回家）
                try:
                    scr = (win.screen() or self.app.primaryScreen()).availableGeometry()
                    r.moveLeft(min(max(r.left(), scr.left()),
                                   scr.right() + 1 - r.width()))
                    r.moveTop(min(max(r.top(), scr.top()),
                                  scr.bottom() + 1 - r.height()))
                except (RuntimeError, TypeError, AttributeError):
                    pass
                win.box.x, win.box.y = int(r.x()), int(r.y())
                items = self._snapshot(win)
                plan = self._plan_all(win, r, items, r) if align_ok else None
                if plan is None:
                    ok = not align_ok      # 没开自适应：不推别人，但也不算「放不下」
                else:
                    _log(f"grow: place {win.box.name} {r.left()},{r.top()} {r.width()}x{r.height()}")
                    self._apply_plan(win, r, plan)
            # 3b) 不再做「每次重排都重新打包」：那是之前「盒子自己乱跳」的来源。
            #     真叠住的家由启动时 _untangle_homes 修一次，其余交给「盖住」。
            # 3c) 「长大了」的那几块（横杠长回正常、拖进图标变高）也要占地方：
            #     现尺寸 > 家的尺寸就算长大，每次重排都按这个条件重新让开——
            #     所以既不会把别人的位置写死，也不会「让开一次就丢」。
            if level >= 5:
                grown = []
                for w in boxes:
                    if w.is_expanded:
                        continue
                    hw = int(getattr(w.box, "home_w", -1) or -1)
                    hh = int(getattr(w.box, "home_h", -1) or -1)
                    if hw <= 0 or hh <= 0:
                        continue
                    try:
                        if int(w._side_w) > hw or int(w._collapsed_h()) > hh:
                            grown.append(w)
                    except (RuntimeError, TypeError, AttributeError):
                        continue
                for w in sorted(grown,
                                key=lambda x: (float(x.box.created or 0.0), x.box.id)):
                    try:
                        r = QRect(int(w.box.x), int(w.box.y),
                                  int(w._side_w), int(w._collapsed_h()))
                    except (RuntimeError, TypeError, AttributeError):
                        continue
                    items = self._snapshot(w)
                    plan = self._plan_all(w, r, items, r)
                    if plan is not None:
                        _log(f"grow: grown {w.box.name} {r.width()}x{r.height()}")
                        self._apply_plan(w, r, plan)
            self._layout_pass = False
            # 4) 一次性落地：位置变了才滑过去（横竖各一段，互不打断）
            for w in boxes:
                if w.is_expanded:
                    continue
                ox, oy = disp[id(w)]
                try:
                    if int(w.box.x) != ox:
                        w.slide_x_to(int(w.box.x))
                    if int(w.box.y) != oy:
                        w.slide_to(int(w.box.y))
                except (RuntimeError, TypeError):
                    continue
            card_moved = False
            for c in cards:
                ox, oy = disp_card[id(c)]
                if (int(c.card.x or 0), int(c.card.y or 0)) == (ox, oy):
                    continue
                card_moved = True
                inset = int(getattr(c, "inset", 0) or 0)
                try:
                    if int(c.card.x or 0) != ox:
                        self._slide_widget_to(c, int(c.card.x) - inset, axis="x")
                    if int(c.card.y or 0) != oy:
                        self._slide_widget_to(c, int(c.card.y) - inset)
                except (RuntimeError, TypeError):
                    continue
            if card_moved:
                try:
                    self.cfg.save()
                except OSError:
                    pass
            # 5) 横杠：挤开时临时收的、临时收完又要长回来的，都在这儿动
            for w in boxes:
                if w.is_expanded:
                    continue
                want_bar = bool(w._bar)
                if want_bar == (w._bar_t > 0.5):
                    continue
                try:
                    w._set_bar(want_bar, persist=False)
                except (RuntimeError, TypeError):
                    pass
            return ok
        finally:
            self._layout_pass = False
            self._in_layout = False
            self._relayout_more = False

    def _panel_for(self, win, allow_collapse: bool = True):
        """展开面摆哪儿：(want, side, up, plan) 或 None。全部按「家」算。"""
        try:
            m = win._win_margin()
            size = win._expanded_rect()
            w = size.width() - 2 * m
            h = size.height() - 2 * m
            base = QRect(int(win.box.home_x), int(win.box.home_y),
                         int(win._side_w), int(win._collapsed_h()))
            items = self._snapshot(win)
        except (RuntimeError, TypeError, AttributeError):
            return None
        return self._place(win, w, h, items, base, allow_collapse=allow_collapse)

    def _startup_layout(self) -> None:
        """启动后归一化一次布局：先把叠在一起的家分开，再按展开状态排一遍。

        为什么放到事件循环里：窗口刚 `show()` 的那一下 `isVisible()` 还是 False，
        当场算会把所有盒子都当成「不存在」，等于什么都没做。
        """
        try:
            # 老数据没有「基准尺寸」：把现在这一刻的尺寸当成基准（不会因此挪任何东西），
            # 之后它再变大（横杠长回正常 / 拖进图标）才有东西可比
            fixed = 0
            for w in self.windows.values():
                if (int(getattr(w.box, "home_w", -1) or -1) <= 0
                        or int(getattr(w.box, "home_h", -1) or -1) <= 0):
                    w.box.home_w = int(w._side_w)
                    w.box.home_h = int(w._collapsed_h())
                    fixed += 1
            if fixed:
                self.store.save()
                _log(f"home sizes seeded: {fixed}")
            self._untangle_homes()
        except Exception as exc:  # noqa: BLE001 - 摆位置出错不能拦着启动
            _log(f"untangle failed: {exc!r}")
        try:
            self._relayout()
        except Exception as exc:  # noqa: BLE001
            _log(f"startup relayout failed: {exc!r}")

    def _on_shrunk(self, win, _freed: int = 0) -> None:
        """盒子收成横杠了（用户双击）：它那块地方腾出来了，整体重排一次。"""
        self._relayout()

    def _untangle_homes(self) -> int:
        """启动时把互相压着的「家」分开（只动真压着的，算完存盘）。

        旧版本把挤开的位置也写进了配置，可能留下两块叠在一起的家；不修的话
        一展开就更容易叠，看着还是「又乱了」。没有重叠时一个像素都不动。
        """
        moved = 0
        wins = [w for w in self.windows.values() if w.isVisible()]
        if not wins:
            return 0
        wins.sort(key=lambda w: (float(getattr(w.box, "created", 0.0) or 0.0),
                                 w.box.id))
        try:
            scr = (wins[0].screen() or self.app.primaryScreen()).availableGeometry()
        except (RuntimeError, TypeError, AttributeError):
            return 0
        # ⚠️ 这里只管「真叠住了」，不是「离得不够远」。用户的布局里盒子之间常
        # 只隔 2~8px，按「留一个空隙」去判会把它们每次启动都顶开几像素，
        # 看着就是「什么都没点，盒子自己在动」。
        gap = 0
        placed: list[QRect] = []

        def hits(r: QRect) -> bool:
            probe = r.adjusted(-gap, -gap, gap, gap)
            return any(probe.intersects(o) for o in placed)

        for w in wins:
            w0, h0 = int(w._side_w), int(w._collapsed_h())
            hx, hy = int(w.box.home_x), int(w.box.home_y)
            r = QRect(hx, hy, w0, h0)
            # 先夹进屏幕（老配置可能存过一个跑到屏幕外的位置）
            r.moveLeft(min(max(r.left(), scr.left()), scr.right() + 1 - w0))
            r.moveTop(min(max(r.top(), scr.top()), scr.bottom() + 1 - h0))
            if not hits(r):
                if (r.x(), r.y()) != (hx, hy):
                    w.box.home_x, w.box.home_y = r.x(), r.y()
                    w.box.x, w.box.y = r.x(), r.y()
                    try:
                        w._apply_collapsed_geometry()
                    except (RuntimeError, TypeError):
                        pass
                    moved += 1
                placed.append(r)
                continue
            best = self._free_slot(r, placed, scr)
            if best is None:
                placed.append(r)      # 一个空位都没有：留着，别乱动
                continue
            w.box.home_x, w.box.home_y = best.x(), best.y()
            w.box.x, w.box.y = best.x(), best.y()
            try:
                w._apply_collapsed_geometry()
            except (RuntimeError, TypeError):
                pass
            moved += 1
            placed.append(best)
        self.store.save()
        _log(f"homes untangled: {moved} moved")
        return moved

    def _panel_free(self, win):
        """不推任何东西时展开面摆哪儿：(rect, side, up, None)。

        只在四个候选里挑**整块都在屏幕内**的（右+下 → 左+下 → 右+上 → 左+上），
        不看别人、更不挪别人——展开只是盖上去（压住的会糊掉）。
        """
        try:
            m = win._win_margin()
            size = win._expanded_rect()
            w = size.width() - 2 * m
            h = size.height() - 2 * m
            base = QRect(int(win.box.home_x), int(win.box.home_y),
                         int(win._side_w), int(win._collapsed_h()))
            scr = (win.screen() or self.app.primaryScreen()).availableGeometry()
        except (RuntimeError, TypeError, AttributeError):
            return None
        for side, up in (("right", False), ("left", False),
                         ("right", True), ("left", True)):
            x = base.x() if side == "right" else base.x() + base.width() - w
            y = base.y() + base.height() - h if up else base.y()
            rect = QRect(x, y, w, h)
            if (rect.left() < scr.left() or rect.right() > scr.right()
                    or rect.top() < scr.top() or rect.bottom() > scr.bottom()):
                continue
            return rect, side, up, None
        x = max(scr.left(), min(base.x(), scr.right() + 1 - w))
        y = max(scr.top(), min(base.y(), scr.bottom() + 1 - h))
        return QRect(x, y, w, h), "right", False, None

    def _free_slot(self, r: QRect, placed, scr, gap: int = 0) -> QRect | None:
        """离 r 最近的空位：不压 placed 里任何一块，整块还在屏幕内。

        搜索按「一圈一圈往外」走（下 → 右 → 上 → 左），所以挪动的距离最小；
        原地空着就直接返回原地（调用方据此判断「不用动」）。
        """
        def hits(rr: QRect) -> bool:
            probe = rr.adjusted(-gap, -gap, gap, gap)
            return any(probe.intersects(o) for o in placed)

        def fixed(rr: QRect) -> QRect:
            out = QRect(rr)
            out.moveLeft(min(max(out.left(), scr.left()),
                             max(scr.left(), scr.right() + 1 - out.width())))
            out.moveTop(min(max(out.top(), scr.top()),
                            max(scr.top(), scr.bottom() + 1 - out.height())))
            return out

        base = fixed(r)
        if not hits(base):
            return base
        step_x = max(8, r.width() + 8)
        step_y = max(8, r.height() + 8)
        seen = {(base.x(), base.y())}
        for k in range(1, 40):
            for cand in (r.translated(0, k * step_y), r.translated(k * step_x, 0),
                         r.translated(0, -k * step_y), r.translated(-k * step_x, 0)):
                cand = fixed(cand)
                key = (cand.x(), cand.y())
                if key in seen:
                    continue
                seen.add(key)
                if not hits(cand):
                    return cand
        return None

    def _pack_collapsed(self, boxes) -> None:
        """收起的盒子互相压住时，后建的那个挪到最近的空位（只改临时位置，家不动）。

        为什么要有这一步：盒子变大（横杠 → 正常、拖进图标）时邻居会让开，可下
        一次重排是从家算起的，那个「让开」本来会被抹掉，于是「一会儿叠、一会儿
        不叠」。这里按「先建的先占位」重排一遍，让开的结果就稳定了。

        只处理**真的压住**的：用户自己摆的盒子常常只隔几像素，不能当成错误顶开。
        """
        try:
            scr = (boxes[0].screen() or self.app.primaryScreen()).availableGeometry()
        except (RuntimeError, TypeError, AttributeError, IndexError):
            return
        placed: list[QRect] = []
        for w in sorted((x for x in boxes if not x.is_expanded),
                        key=lambda x: (float(x.box.created or 0.0), x.box.id)):
            try:
                r = QRect(int(w.box.x), int(w.box.y),
                          int(w._side_w), int(w._collapsed_h()))
            except (RuntimeError, TypeError, AttributeError):
                continue
            best = self._free_slot(r, placed, scr)
            if best is None:
                best = r
            w.box.x, w.box.y = int(best.x()), int(best.y())
            placed.append(QRect(best))

    def _plan_all(self, win, target, items, base, allow_collapse: bool = True):
        """一份完整方案：(down, collapsed, row, up, side, blocked)；让不开就等于空方案。

        按 `_push_level` 只做已经验证过的那几条规则，其余的**一律盖住**：
          1 正下方往下让；2 再加正侧面；3 再加往上；4 再加临时收横杠。
        「让不开」不再等于失败（以前会直接放弃这次展开），而是盖住 + 模糊——
        展开本身永远成立，这样每加一条规则都不会把「展开」整个搞坏。
        """
        try:
            level = int(getattr(self, "_push_level", 0) or 0)
        except (TypeError, ValueError):
            level = 0
        below, sideable, above = self._split(win, target, items, base)
        if level >= 1:
            plan = self._plan_below(win, target, below,
                                    bool(allow_collapse) and level >= 4)
        else:
            plan = ([], set(), [])
        if plan is None:
            plan = ([], set(), [])          # 让不开：盖住，不动别人
        moves, collapsed, row = plan
        up: list = []
        if above and level >= 3:
            up = self._plan_up(win, target, above) or []
        virt = self._virt_items(items, list(moves) + list(up))
        if level >= 2:
            side, blocked = self._plan_sides(win, target, virt, sideable)
        else:
            side, blocked = [], [o for o, _b, _r in sideable]
        return moves, collapsed, row, up, side, blocked

    def _place(self, win, w: int, h: int, items, base,
               allow_collapse: bool = True, sides=("right", "left")):
        """给「要长成 w×h 的展开面」挑一个位置：(rect, side, up, plan) 或 None。

        用户 2026-11-15 定的优先顺序：
          * 默认**往右长、往下展开**；
          * 右边让不开（或者出了屏幕）→ **往左长**（右边对齐盒子右边）；
          * 下面让不开（或者出了屏幕）→ **往上长**（底边不动，`_up`）；
          * 四个都不行就用第一个（右+下），让不开的盖住+模糊。

        「让得开」= `_plan_all` 算得出来，而且**侧面没有 blocked**（侧面盖住是
        允许的，所以只有算不出来才淘汰）。

        另外排序按「盖住的东西越少越好」：屏幕底部那种情况，往下展开会压住下面
        那个盒子（它斜着错开一点，按「斜下方不推」的规矩不会被推开），上面明明
        还有地方——那就该往上长。所以四个候选按
        （要盖住几个没让开的, 要不要把邻居收横杠, 侧面有几个推不开）比大小，
        全零直接定。都不好就挑最小的那个。
        """
        cur_h = base.height()
        top0 = int(base.top())
        bottom = top0 + cur_h
        try:
            scr = (win.screen() or self.app.primaryScreen()).availableGeometry()
        except (RuntimeError, TypeError):
            return None
        best = None
        best_key = None
        for side in sides:
            ups = (False, True) if self._push_level >= 3 else (False,)
            for up in ups:
                y = bottom - h if up else top0
                if side == "right":
                    x = win.box.x
                else:
                    x = win.box.x + win._side_w - w    # 右边对齐盒子右边，往左长
                want = QRect(x, y, w, h)
                if want.left() < scr.left() or want.right() > scr.right():
                    continue
                if want.top() < scr.top() or want.bottom() > scr.bottom():
                    continue
                plan = self._plan_all(win, want, items, base, allow_collapse)
                if plan is None:
                    continue
                moved = {id(entry[0]) for entry in plan[0]}      # 往下让的
                moved |= {id(entry[0]) for entry in plan[3]}     # 往上让的
                moved |= {id(entry[0]) for entry in plan[4]}     # 往侧边让的
                covered = 0
                for other, _is_box, rect in items:
                    if id(other) in moved:
                        continue
                    if rect.intersects(want):
                        covered += 1
                key = (covered, 1 if plan[1] else 0, len(plan[5]))
                if best_key is None or key < best_key:
                    best_key, best = key, (want, side, up, plan)
                if key == (0, 0, 0):
                    return want, side, up, plan      # 谁都不挡、也不用收横杠
        return best

    def _apply_plan(self, win, target, plan, remember: bool = False) -> None:
        """把方案搬到窗口上：收横杠、往下推、往上推、往左右推。

        只改逻辑坐标——挤开是这一次的临时结果，**家不动**（见 `_relayout`）。
        `remember` 留着只为兼容老调用，已经不需要了：位置不再靠记录吸回来。
        """
        moves, collapsed, row, up, side, _blocked = plan
        for _top, is_box, other, _rect in row:
            if is_box and other.box.id in collapsed:
                # 临时收成横杠腾地方：只改这一刻的样子，**不落盘**（落盘就变永久了）
                other._tmp_bar = True
                other._bar = True
        for other, is_box, ny in up:
            old = self._axis_pos(other, is_box, "y")
            try:
                self._axis_move(other, is_box, "y", ny)
            except (RuntimeError, OSError, TypeError, AttributeError):
                continue
            _log(f"grow: up {self._label(other, is_box)} {old}->{ny}")
        for other, is_box, ny in moves:
            old = self._axis_pos(other, is_box, "y")
            try:
                self._axis_move(other, is_box, "y", ny)
            except (RuntimeError, OSError, TypeError, AttributeError):
                continue
            _log(f"grow: down {self._label(other, is_box)} {old}->{ny}")
        for other, is_box, axis, here, got in side:
            try:
                self._axis_move(other, is_box, axis, got)
            except (RuntimeError, OSError, TypeError, AttributeError):
                continue
            _log(f"grow: side {self._label(other, is_box)} {here}->{got}")
        if collapsed:
            _log(f"grow: collapse {len(collapsed)} move {len(moves)}")

    def _plan_up(self, win, target, above):
        """底部放不下、把盒子往上抬着展开：把上方挡路的**往上推**。

        用户 2026-11-15 定的：默认往下推；只有底部空间不够、顶部还有余的时候才
        往上推，收起时同样吸回来。上下都没空间就返回 None（调用方抖两下）。
        moves = [(other, 是不是盒子, 新 y)]。
        """
        gap = self.SETTLE_GAP
        scr = (win.screen() or self.app.primaryScreen()).availableGeometry()
        row = sorted(above, key=lambda it: -it[2].bottom())   # 离盒子近的先摆
        placed: list[tuple[int, int, int]] = []               # (left, right, top)
        moves: list[tuple] = []
        pushed = {id(other) for other, _b, _r in row}
        obstacles = [(o, r) for o, _b, r in self._snapshot(win)
                     if id(o) not in pushed]
        for other, is_box, rect in row:
            h = rect.height()
            left, right = rect.left(), rect.right()
            cur = self._axis_pos(other, is_box, "y")
            want = target.top() - gap - h
            for o_left, o_right, o_top in placed:
                if left <= o_right and o_left <= right:
                    want = min(want, o_top - gap - h)
            ny = min(cur, want)
            # 往上推的时候不能撞到**没人推**的那些（卡片、别的盒子）：
            # 撞着就继续往上抬，抬到它们上面去；抬出屏幕就说明放不下
            for _ in range(8):
                probe = rect.translated(0, ny - rect.top())
                clash = None
                for o2, r2 in obstacles:
                    if o2 is other:
                        continue
                    if probe.adjusted(-gap, -gap, gap, gap).intersects(r2):
                        clash = r2
                        break
                if clash is None:
                    break
                ny = min(ny, clash.top() - gap - h)
            if ny != cur:
                moves.append((other, is_box, ny))
            if ny < scr.top():
                return None                  # 顶到屏幕边了：放不下
            placed.append((left, right, ny))
        return moves

    def _plan_below(self, win, target, below, allow_collapse: bool = True):
        """正下方让开方案：返回 (moves, collapsed, row)，放不下返回 None。

        moves = [(other, 是不是盒子, 新 y)]，collapsed = 被收成横杠的盒子 id 集合。
        并排挨着的两块**一起**让到同一条线上（谁也不许多让一格）；只有同一列
        （横向有交集）的上方才继续往下挤。`below` 是 `_split` 挑出来的那一拨
        （侧下方不在这里，用户定的：侧下方不推）。
        """
        from .box_widget import TITLE_H
        gap = self.SETTLE_GAP
        scr = (win.screen() or self.app.primaryScreen()).availableGeometry()
        limit = scr.y() + scr.height()
        row = [[rect.top(), is_box, other, rect] for other, is_box, rect in below]
        row.sort(key=lambda item: item[0])

        def height_of(is_box, other, rect, collapsed) -> int:
            if not is_box:
                return rect.height()
            if other.box.id in collapsed:
                return TITLE_H
            return other._collapsed_h()

        target_bottom = target.y() + target.height()

        def sim(collapsed, pack):
            placed: list[tuple[int, int, int]] = []   # 已摆好的：(left, right, bottom)
            stop = limit
            moves = []
            for _top, is_box, other, rect in row:
                if is_box and other.is_expanded:
                    top = rect.top()
                    if any(left <= rect.right() and rect.left() <= right
                           and bottom + gap > top for left, right, bottom in placed):
                        return None
                    stop = min(stop, top)
                    break
                h = height_of(is_box, other, rect, collapsed)
                left, right = rect.left(), rect.right()
                cur = self._axis_pos(other, is_box, "y")
                need = 0
                if left <= target.right() and target.left() <= right:
                    need = target_bottom + gap                # 只有「同一列」的上方才继续往下挤；并排的互不影响
                for o_left, o_right, o_bottom in placed:
                    if left <= o_right and o_left <= right:
                        need = max(need, o_bottom + gap)
                # pack：贴紧排（没东西压着就往上吸）；否则只往下让
                ny = need if (pack and need) else max(cur, need)
                if ny != cur:
                    moves.append((other, is_box, ny))
                bottom = ny + h
                import os as _dbg_os
                if _dbg_os.environ.get("DB_DEBUG_BELOW"):
                    print("   DBG item", self._label(other, is_box), "cur", cur,
                          "need", need, "ny", ny, "h", h, "bottom", bottom,
                          "stop", stop, "expanded", getattr(other, "is_expanded", None),
                          "placed", placed, "target_bottom", target_bottom)
                if bottom > stop:
                    return None
                placed.append((left, right, bottom))
            return moves

        plan = sim(set(), False)
        import os as _os
        if _os.environ.get("DB_DEBUG_BELOW"):
            print("DBG plan_below target", tuple(target.getRect()),
                  "limit", limit, "row", [(self._label(o, b), tuple(r.getRect()))
                                          for _t, b, o, r in row],
                  "->", plan)
        if plan is not None:
            return plan, set(), row
        if not allow_collapse:
            return None
        collapsed: set[str] = set()
        while True:
            victim = None
            for _top, is_box, other, _rect in row:
                if not is_box or other.is_expanded:
                    continue
                if other.box.id in collapsed or getattr(other, "_bar", False):
                    continue
                victim = other
            if victim is None:
                return None
            collapsed.add(victim.box.id)
            plan = sim(collapsed, True)
            if plan is not None:
                return plan, collapsed, row

    def _plan_sides(self, win, target, items, cand_in):
        """把「正侧面」那几块往左右推：(moves, blocked)。

        用户定的方向：**右边有地方就先往右**，右边不够才试左边；两边都不够就算
        「侧面没空间」——它留在原地被盖住（展开会把重叠区糊掉，见
        `BoxWidget._set_expanded_blur`）。

        `cand` 是 `_split` 挑出来的「正侧面」那一拨（侧下方不在这里，用户定的：
        斜着那块不推）；`items` 是全部障碍物，用来算让开之后会不会撞上。
        moves = [(other, 是不是盒子, 轴 "x", 原来 x, 推到 x)]；
        blocked = 推不开、只能被盖住的那几个（勾常驻展开时见到它就勾不上）。
        只有展开面**比盒子现在宽**时才做（-1 → 0 只是变高，侧面不动）。

        **不会跨到另一侧**：右边的东西只往右让、左边的只往左让。它自己那一侧
        没地方就进 blocked（盖住 + 模糊）——用户报过「展开外设控制，它跟收纳盒
        换位了」，就是右边被挡住时把收纳盒推到了左边去。同一排已经让开的会连在
        一起（后一块排在先走的那个外面），所以一排邻居是整体往右/往左挪。
        """
        try:
            cur_w = win.collapsed_rect().width()
        except (RuntimeError, TypeError, AttributeError):
            cur_w = 0
        if target.width() <= cur_w + 1:
            return [], []
        gap = self.SETTLE_GAP
        scr = (win.screen() or self.app.primaryScreen()).availableGeometry()
        cand = []
        blocked: list = []
        for other, is_box, rect in cand_in:
            if is_box and getattr(other, "is_expanded", False):
                # 已经展开的盒子不横着推：整块面太大，推一下像把它搬走
                blocked.append(other)
                continue
            cand.append((other, is_box, rect))

        def need(item) -> int:
            rect = item[2]
            return min(target.right() + 1 + gap - rect.left(),
                       rect.right() - (target.left() - gap - 1))

        cand.sort(key=need)          # 挪得少的先落位，后面的按新位置算
        virt: dict[int, QRect] = {}
        moves: list[tuple] = []
        for other, is_box, rect in cand:
            others = [virt.get(id(o2)) or r2 for o2, _b2, r2 in items if o2 is not other]
            here = rect.left()
            got = None
            right = target.right() + 1 + gap
            left = target.left() - gap - rect.width()
            # 同一排已经让开的那几个要连起来：它们先走了，后面的就跟在它们外侧，
            # 一排东西一起往右让，而不是第二块又叠回第一个的位置上
            for vr in virt.values():
                if rect.top() < vr.bottom() and vr.top() < rect.bottom():
                    right = max(right, vr.right() + 1 + gap)
                    left = min(left, vr.left() - gap - rect.width())
            # 只能往**自己那一侧**推：右边的东西往右让，左边的东西往左让。
            # 以前只看哪边有地方——右边被挡住（比如同一排已经排满）时会把它推到
            # 左边去，整块跨过展开面，看着就是「展开外设控制，它跟收纳盒换位了」。
            on_right = rect.center().x() > target.center().x()
            for want, roomy in ((right, right + rect.width() - 1 <= scr.right()),
                                (left, left >= scr.left())):
                if not roomy or (want == right) != on_right:
                    continue
                if self._free_at(rect, others, gap, "x", want):
                    got = want
                    break
            if got is None:
                blocked.append(other)
                continue
            virt[id(other)] = rect.translated(got - here, 0)
            moves.append((other, is_box, "x", here, got))
        return moves, blocked

    def _grow_request(self, win, target=None, remember: bool = True) -> bool:
        """盒子要变大（横杠 → 正常 / 永久变大）：有地方吗？有就整张桌面重排一次。

        位置不再靠记录：`_relayout` 先把所有人摆回家，再按新尺寸把挡路的推开，
        收起时同样按家重算。`target` / `remember` 只为兼容老调用留着。
        """
        if self._in_layout:
            return True
        return self._relayout(win)

    def _expand_room(self, win) -> bool:
        """展开到 1：整张桌面重排一次（展开的面板在 `_relayout` 里占位）。

        用户 2026-11-15 定的：**右边没地方就往左长**——编程在屏幕右边，右边是
        时钟卡（贴着屏幕边、推不动），那就往左展开，把左边的收纳盒往左挤，右边
        两张卡一个像素都不动；上下同理（下面没地方就往上长）。侧面推不开就
        盖住+模糊。
        """
        if self._in_layout:
            return True
        return self._relayout()

    def _pin_check(self, win) -> bool:
        """勾「常驻展开」之前问一句：四个方向都让得开吗？（只算，绝不动）

        用户定的：勾着就一定得是 1 状态；所以让不开就不许勾（勾选直接不成功，
        盒子抖两下），但**点击**展开照样能展到 1，被盖住的东西模糊掉。
        另外常驻不许把下方 0 状态的盒子收成横杠来腾地方——那是「展开一下」才有
        的待遇。
        """
        try:
            try:
                level = int(getattr(self.cfg, "push_rules", 0) or 0)
            except (TypeError, ValueError):
                level = 0
            self._push_level = max(0, min(5, level))
            if self._push_level == 0:
                # 不推别人：只要展开面整块放得下就算勾得上
                return self._panel_free(win) is not None
            got = self._panel_for(win, allow_collapse=False)
            if got is None:
                return False
            plan = got[3]
            if plan is None:
                return True
            return not plan[5]              # 侧面有推不开的就勾不上
        except Exception:  # noqa: BLE001 - 算不出来就别拦着人家
            return True

    def _after_grow(self, win) -> None:
        """盒子自动变大之后：新压住的桌面图标挪出去，挡路的盒子/卡片让开。"""
        try:
            win._push_desktop_icons()
        except Exception:  # noqa: BLE001 - 桌面接口抽风不能挡住别的
            pass
        try:
            self._relayout(win)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ 窗口层级
    def _z_windows(self) -> list:
        """从下到上的次序：时钟卡片 → 收起的盒子 → 展开的盒子。"""
        cards = [w for w in self.card_widgets.values()
                 if w.isVisible() and not getattr(w, "always_on_top", False)]
        collapsed = [w for w in self.windows.values()
                     if w.isVisible() and not w.is_expanded]
        expanded = [w for w in self.windows.values()
                    if w.isVisible() and w.is_expanded]
        return cards + collapsed + expanded

    def apply_z_order(self) -> None:
        """把窗口排成固定的层级：展开的盒子最前，其次收起的盒子，时钟卡片最后。

        ⚠️ `SetWindowPos(hwnd, insert_after, ...)` 的语义是「把 hwnd 插到
        insert_after 的**后面**（更深）」——所以「想让 X 更靠前」只能把 X 插到
        「它下面那个窗口的上一个窗口」的后面，不能拿 Progman 当锚点（那等于压到
        最底下）。`want` 是从前到后的次序，逐个别到「它前面那一个」的后面即可。

        ⚠️ 只有在**真的不对**的时候才动窗口：每次 SetWindowPos 都会让 DWM
        重新合成一遍，半透明窗口肉眼就是闪一下。层级已经正确时直接返回。
        """
        t_all = time.perf_counter()
        try:
            progman = box_user32.FindWindowW("Progman", None)
        except OSError:
            return
        if not progman:
            return
        from .box_widget import is_dragging
        if is_dragging():
            return   # 正在拖盒子：别重排，否则会把被拖的窗口压回下面
        now = time.monotonic()
        if any(getattr(w, "_window_dragging", False)
               and 0.0 <= now - float(getattr(w, "_drag_started_at", 0.0) or 0.0) < 6.0
               for w in self.card_widgets.values()):
            return   # 正在拖卡片：重排会让半透明窗口闪一下
        # 拖动标记要是残留下来了（没收到 release），超过 6 秒就不再挡着层级
        # 期望次序：前 → 后
        want = []
        for win in reversed(self._z_windows()):
            try:
                want.append(int(win.winId()))
            except (RuntimeError, TypeError):
                continue
        if not want:
            return
        chain = []
        h = box_user32.GetTopWindow(None)
        while h:
            chain.append(h)
            h = box_user32.GetWindow(h, 2)   # GW_HWNDNEXT
        pos = {h: i for i, h in enumerate(chain)}
        prog = pos.get(progman, len(chain))
        actual = sorted((h for h in want if h in pos), key=lambda x: pos[x])
        if actual == want and all(pos.get(h, 1 << 30) < prog for h in want):
            return   # 已经是对的，一个窗口都别动
        _log(f"z-order applied: {len(want)} windows")
        t_walk = time.perf_counter()
        # 只挪**真的排错**的那几个：拖完一次其实只有一个窗口（被拖的那张）跑到
        # 顶层去了，其余 13 个相对次序没变。以前是 14 个窗口整条链从头插一遍，
        # 每个 SetWindowPos 都让 DWM 重合成一次，松手后定时器一响就是一次
        # 100~200ms 的顿。这里用最长递增子序列挑出「本来就已经有序」的那些跳过。
        ordered = [h for h in chain if h in want]
        rank = {h: i for i, h in enumerate(ordered)}
        keep = _lis_keep([rank[h] for h in want])
        moved = 0
        for i, hwnd in enumerate(want):
            if i in keep:
                continue
            if i == 0:
                # 要排到**最前**的那个：插到「第二个窗口的正上方」。
                # ⚠️ 这里以前是 `prev = progman` 一路往下插：SetWindowPos 的语义是
                # 「把窗口插到锚点**下面**」，拿 Progman 当锚点就等于压到最底下——
                # 展开的收纳盒正好总是排在最前的那一个，于是每次展开都被压到垫底、
                # 被收起的盒子盖住。要「更前」只能锚定在下面那个窗口的上一个窗口上。
                below = want[1] if len(want) > 1 else 0
                anchor = 0                              # 0 = HWND_TOP
                try:
                    if below:
                        anchor = int(box_user32.GetWindow(below, 3))  # GW_HWNDPREV
                except OSError:
                    anchor = 0
                if not anchor:
                    anchor = progman                    # 实在找不到锚点：至少别掉到桌面底下
            else:
                anchor = want[i - 1]
            try:
                # 已经紧挨在锚点下面（更深一层）的就别动了：每次 SetWindowPos 都要
                # DWM 重新合成一遍，十几个窗口全套重插就是一次 100~200ms 的卡顿
                if anchor and int(box_user32.GetWindow(anchor, 2)) == hwnd:  # NEXT
                    continue
                if not box_user32.SetWindowPos(
                        hwnd, anchor, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER):
                    continue
            except OSError:
                continue
            moved += 1
        if moved:
            _log("z-order moved %d/%d walk=%.1fms move=%.1fms"
                 % (moved, len(want), (t_walk - t_all) * 1000.0,
                    (time.perf_counter() - t_walk) * 1000.0))

    # ------------------------------------------------- 外观：盒子 / 卡片
    def _apply_box_look(self, win) -> None:
        """面板里改的颜色 / 浓度 / 圆角，盒子当场跟着变。"""
        win.glass_alpha = clamp_alpha(self.cfg.box_glass_alpha)
        win.radius = float(self.cfg.box_radius)
        win.glass_rgb = QColor(self.cfg.box_color) if self.cfg.box_color else None
        win.update()

    def _apply_card_look(self, win) -> None:
        """卡片那三个参数和盒子是分开的：颜色 / 浓度 / 圆角各管各的。"""
        win.glass_alpha = clamp_alpha(self.cfg.card_glass_alpha)
        win.radius = float(self.cfg.card_radius)
        win.glass_rgb = QColor(self.cfg.card_color) if self.cfg.card_color else None
        win.update()

    def apply_style_preset(self, preset_id: str) -> None:
        """换内置风格：玻璃浓度 / 圆角 / 卡片玻璃浓度一次套上去（不动颜色）。"""
        from . import style_presets
        preset = style_presets.get(preset_id)
        if preset is None:
            return
        self.cfg.style_preset = preset["id"]
        self.cfg.box_glass_alpha = clamp_alpha(preset["box_alpha"])
        self.cfg.box_radius = int(preset["radius"])
        self.cfg.card_glass_alpha = clamp_alpha(preset["card_alpha"])
        self.cfg.card_radius = int(preset["radius"])
        for card in self.cfg.cards:
            card.glass_alpha = self.cfg.card_glass_alpha
        self.cfg.save()
        self.apply_box_style()
        # 提示条跟卡片同一个浓度（它自己不是卡片，单独更新一次）
        try:
            self.toast.glass_alpha = clamp_alpha(self.cfg.card_glass_alpha)
        except (RuntimeError, AttributeError):
            pass
        _log(f"style preset: {preset['id']}")

    def apply_box_style(self) -> None:
        """把所有活着的窗口按配置重刷一遍（面板改外观、套方案都走这里）。"""
        for win in self.windows.values():
            self._apply_box_look(win)
        for win in self.card_widgets.values():
            try:
                self._apply_card_look(win)
            except (RuntimeError, TypeError):
                continue
        try:
            self.preview.glass_alpha = clamp_alpha(self.cfg.box_glass_alpha)
            self.preview.radius = float(self.cfg.box_radius)
            self.preview.glass_rgb = (QColor(self.cfg.box_color)
                                      if self.cfg.box_color else None)
            self.preview.update()
        except (RuntimeError, AttributeError):
            pass

    # ---- 自定义外观 / 方案 ----
    def style_values(self) -> dict:
        """现在这套外观（存方案、写面板都用它）。"""
        cfg = self.cfg
        return {
            "box_color": str(cfg.box_color),
            "box_alpha": int(cfg.box_glass_alpha),
            "box_radius": int(cfg.box_radius),
            "card_color": str(cfg.card_color),
            "card_alpha": int(cfg.card_glass_alpha),
            "card_radius": int(cfg.card_radius),
        }

    def apply_style_values(self, item: dict) -> None:
        """把一组外观参数套上去（颜色空 = 默认白玻璃）。"""
        cfg = self.cfg
        cfg.box_color = norm_color(item.get("box_color"))
        cfg.card_color = norm_color(item.get("card_color"))
        cfg.box_glass_alpha = clamp_alpha(item.get("box_alpha",
                                                   cfg.box_glass_alpha))
        cfg.card_glass_alpha = clamp_alpha(item.get("card_alpha",
                                                    cfg.card_glass_alpha))
        cfg.box_radius = max(0, min(30, int(item.get("box_radius",
                                                     cfg.box_radius) or 0)))
        cfg.card_radius = max(0, min(30, int(item.get("card_radius",
                                                      cfg.card_radius) or 0)))
        for card in cfg.cards:
            card.glass_alpha = cfg.card_glass_alpha
        cfg.save()
        self.apply_box_style()

    def set_style_color(self, which: str, color: str) -> None:
        value = norm_color(color)
        if which == "card":
            self.cfg.card_color = value
        else:
            self.cfg.box_color = value
        self.cfg.save()
        self.apply_box_style()

    def set_style_alpha(self, which: str, value) -> None:
        alpha = clamp_alpha(value)
        if which == "card":
            self.cfg.card_glass_alpha = alpha
            for card in self.cfg.cards:
                card.glass_alpha = alpha
        else:
            self.cfg.box_glass_alpha = alpha
        self.cfg.save()
        self.apply_box_style()

    def set_style_radius(self, which: str, value) -> None:
        radius = max(0, min(30, int(value)))
        if which == "card":
            self.cfg.card_radius = radius
        else:
            self.cfg.box_radius = radius
        self.cfg.save()
        self.apply_box_style()

    def apply_style_scheme(self, name: str) -> bool:
        item = next((s for s in self.cfg.style_schemes
                     if str(s.get("name")) == str(name)), None)
        if item is None:
            return False
        self.apply_style_values(item)
        _log(f"style scheme: {name}")
        return True

    def save_style_scheme(self, name: str) -> bool:
        name = str(name or "").strip()
        if not name:
            return False
        item = dict(self.style_values(), name=name)
        self.cfg.style_schemes = [
            s for s in self.cfg.style_schemes if str(s.get("name")) != name
        ] + [item]
        self.cfg.save()
        return True

    def del_style_scheme(self, name: str) -> None:
        self.cfg.style_schemes = [
            s for s in self.cfg.style_schemes if str(s.get("name")) != str(name)
        ]
        self.cfg.save()

    def reset_style(self) -> None:
        """恢复成现在这套默认外观：白玻璃、标准浓度、圆角 10。"""
        self.apply_style_values({"box_color": "", "card_color": "",
                                 "box_alpha": 212, "card_alpha": 212,
                                 "box_radius": 10, "card_radius": 10})

    def set_taskbar_align(self, code) -> None:
        """任务栏图标靠左 / 居中：写 Windows 自己的 TaskbarAl，当场生效。"""
        align = "center" if str(code) == "center" else "left"
        self.cfg.taskbar_align = align
        self.cfg.taskbar_left = align == "left"
        self.cfg.save()
        try:
            tb.set_left_align(self.cfg.taskbar_left)
        except Exception:  # noqa: BLE001 - 改不了对齐不影响别的
            pass
        _log(f"taskbar align: {align}")

    def _on_box_removed(self, box_id: str) -> None:
        """盒子把自己删掉了（里面的图标被拖空）。"""
        self.windows.pop(box_id, None)
        self.sel_items.discard(f"box:{box_id}")
        self._forget_undo(box_id)
        self._sync_ui()

    # ------------------------------------------------- 框选：收纳盒 + 卡片一起选
    def _sel_widgets(self) -> dict:
        """桌面上能被框选的东西：所有收纳盒 + 所有卡片（键 → 窗口）。"""
        out: dict = {}
        for w in list(self.windows.values()) + list(self.card_widgets.values()):
            key = getattr(w, "sel_key", None)
            if key:
                out[key] = w
        return out

    def select_items(self, keys) -> None:
        """框选结果：选中的东西玻璃压暗一点（和拖东西进盒子同一个效果）。"""
        keys = {str(k) for k in keys}
        if keys == self.sel_items:
            return
        widgets = self._sel_widgets()
        for key in self.sel_items - keys:
            w = widgets.get(key)
            if w is not None:
                try:
                    w.set_selected(False)
                except (RuntimeError, AttributeError):
                    pass
        for key in keys - self.sel_items:
            w = widgets.get(key)
            if w is not None:
                try:
                    w.set_selected(True)
                except (RuntimeError, AttributeError):
                    pass
        self.sel_items = keys
        _log(f"selection: {sorted(keys)}")

    def clear_selection(self) -> None:
        self.select_items(set())

    def _sel_rect(self, win):
        """东西在屏幕上占的玻璃矩形；拿不到就返回 None。"""
        try:
            if isinstance(win, BoxWidget):
                if win.is_expanded:
                    m = win._win_margin()
                    return win._expanded_rect().adjusted(m, m, -m, -m)
                return win.collapsed_rect()
            return aligner.glass_rect(win)
        except (RuntimeError, AttributeError, TypeError):
            return None

    def _band_select(self, rect) -> None:
        """物理像素的框 → 逻辑像素，**整个被框住**的才选中；空框 = 一个都不选。"""
        if rect is None or rect.isNull() or rect.width() < 2 or rect.height() < 2:
            self.clear_selection()
            return
        try:
            l, t, r, b = screen.to_logical(
                (rect.x(), rect.y(), rect.x() + rect.width(), rect.y() + rect.height()))
        except Exception:  # noqa: BLE001
            return
        area = QRect(l, t, max(1, r - l), max(1, b - t))
        # 贴边的盒子必须给这点容差：鼠标停在屏幕边上时框的边只能到最后一像素
        # （物理 2559 → 逻辑 2047），而贴边盒子右边是 2048——差 1px 就永远「没框
        # 住」，鼠标一抖就在选中/不选中之间来回跳（用户报的「贴边框起来闪烁」）。
        area = area.adjusted(-BAND_TOL, -BAND_TOL, BAND_TOL, BAND_TOL)
        keys = []
        for key, win in self._sel_widgets().items():
            try:
                if not win.isVisible():
                    continue
            except RuntimeError:
                continue
            rr = self._sel_rect(win)
            # 只压住一角不算：必须整块都在框里（用户要求）
            if rr is not None and area.contains(rr):
                keys.append(key)
        self.select_items(keys)

    def _group_for(self, win) -> list:
        """拖一个被选中的东西时，返回「跟着一起搬」的其它窗口（盒子、卡片都行）。"""
        key = getattr(win, "sel_key", None)
        if not key or key not in self.sel_items or len(self.sel_items) < 2:
            return []
        out = []
        widgets = self._sel_widgets()
        for other_key in self.sel_items:
            if other_key == key:
                continue
            other = widgets.get(other_key)
            if other is None:
                continue
            try:
                if other.isVisible():
                    out.append(other)
            except RuntimeError:
                continue
        return out

    def _on_widget_press(self, win) -> None:
        """按在没被选中的东西上：这一组多选到此为止。"""
        key = getattr(win, "sel_key", None)
        if self.sel_items and key not in self.sel_items:
            self.clear_selection()
        # 按到别的窗口上：别的盒子里「双击还没落地」的启动作废。以前每个盒子各自
        # 计时 180ms，在 A 里双击一下、马上又去 B 里双击，两边都会到点启动
        # —— 用户看到的就是「打开一个，另一个也跟着开了」。
        for other in list(self.windows.values()) + list(self.card_widgets.values()):
            if other is win:
                continue
            cb = getattr(other, "cancel_pending_launch", None)
            if cb is not None:
                try:
                    cb()
                except (RuntimeError, TypeError):
                    continue


    def merge_boxes(self, src_win, dst_win) -> None:
        """两个盒子合并：src 的图标全进 dst，src 自己消失。"""
        if not getattr(self.cfg, "box_merge", True):
            return   # 设置里关了合并：什么都不做（拖上去也不会并）
        if self._merge_one(src_win, dst_win) <= 0:
            # 一个都没搬过去（文件被占用等）：源盒子留在原地，别让它凭空消失
            try:
                if not src_win.is_expanded:
                    src_win.toggle(True)
            except (RuntimeError, TypeError):
                pass
            return
        self._after_merge(dst_win)

    def merge_group(self, src_wins, dst_win) -> None:
        """多选之后整组拖到另一个盒子上松手：这一整组都并进那个盒子。

        用户的要求（2026-11-15）：选中多个收纳盒，拖其中一个时把它的拖动区压到
        另一个盒子的拖动区上松手 —— 不是只并被拖的那个，**整组都并进去**。
        """
        if not getattr(self.cfg, "box_merge", True):
            return
        total = 0
        for win in list(src_wins):
            if win is dst_win:
                continue
            total += self._merge_one(win, dst_win)
        if total <= 0:
            return
        self.clear_selection()      # 并掉的盒子已经没了，别留一堆悬空的多选
        self._after_merge(dst_win)

    def _merge_one(self, src_win, dst_win) -> int:
        """把一个盒子的图标全搬进另一个盒子（只管搬，收尾交给调用方）。"""
        src = getattr(src_win, "box", None)
        dst = getattr(dst_win, "box", None)
        if src is None or dst is None or src.id == dst.id:
            return 0
        moved = self.store.merge(src, dst)
        if moved <= 0:
            return 0
        try:
            src_win.dispose()
        except Exception:  # noqa: BLE001 - 监听器停不掉也不能挡着合并
            pass
        src_win.close()
        src_win.deleteLater()
        self.windows.pop(src.id, None)
        self.sel_items.discard(f"box:{src.id}")
        self._forget_undo(src.id)
        return moved

    def _after_merge(self, dst_win) -> None:
        """并完的收尾：目标盒子重排（图标多了展开尺寸要跟着长）并抬到最上层。"""
        dst_win.refresh()
        if not dst_win.is_expanded:
            dst_win.toggle(True)
        dst_win.raise_()
        self._sync_ui()

    def _on_hint(self, rect, text: str) -> None:
        # 提示浮层已经去掉（用户嫌突兀），这里保留兼容用的空实现
        return

    def _on_armed(self, rect, src: str, dst: str) -> None:
        """拖到目标图标上停住了：在目标位置先摆一个盒子预览。"""
        if not getattr(self.cfg, "box_merge", True):
            return   # 合并被关掉了就别弹预览了
        self.preview.show_for(screen.to_logical(rect), [src, dst])

    def _on_merged(self, rect, src: str, dst: str) -> None:
        """两个图标被拖到一起了。

        src/dst 可能是：桌面上的普通文件（快捷方式、文档、图片…都算），
        或者 `ns::{CLSID}`——后者是此电脑/回收站/控制面板这类命名空间项（不是文件）。
        """
        files: list[Path] = []
        nss: list[str] = []
        for raw in (src, dst):
            if ns_items.is_pseudo(raw):
                nss.append(ns_items.clsid_of(raw))
                continue
            p = Path(raw)
            if not boxable(p):
                return
            files.append(p)
        if not files and not nss:
            return
        x, y = box_pos_on_icon(screen.to_logical(rect))
        self.preview.hide()
        box = self.store.create("收纳盒", x, y, [str(p) for p in files])
        for clsid in nss:
            self.store.add_ns(box, ns_items.name_for(clsid), clsid)
        win = self._spawn(box)
        win.toggle(False)
        self._push_undo(box.id)
        # 两个图标已经被 SHChangeNotify 当场从桌面撤掉了（在 store 里做的），
        # 所以可以立刻把邻居挤开，不用再等 explorer 收拾残局
        QTimer.singleShot(0, lambda: self._make_room(win))
        self._sync_ui()

    def _on_drag_preview(self, rect, paths) -> None:
        """盒子里拖出来的图标压在桌面图标上：先在那儿摆个盒子预览。"""
        if not getattr(self.cfg, "box_merge", True):
            return
        if rect is None or not paths:
            self.preview.hide()
            return
        self.preview.show_for(screen.to_logical(rect), paths)

    def _on_new_box(self, target_rect, target_path, paths, tries: int = 0) -> None:
        """盒子里的图标被丢到桌面的另一个图标上：就地建个盒子装下这两个。"""
        self.preview.hide()
        try:
            target = Path(str(target_path))
        except (TypeError, ValueError):
            return
        if not target.is_file() or not boxable(target):
            return
        # 松手那一刻 explorer 会先把拖出来的文件放到桌面，它慢半拍才落地，
        # 所以这里跟 _land_restored 一样等一会儿再找。
        live = []
        for raw in paths:
            p = Path(str(raw))
            if p.exists():
                live.append(p)
                continue
            hit = di.find_desktop_copy(p.name)
            if hit is not None:
                live.append(hit)
        if not live:
            if tries < 20:
                QTimer.singleShot(40, lambda: self._on_new_box(
                    target_rect, target_path, paths, tries + 1))
            return
        x, y = box_pos_on_icon(screen.to_logical(target_rect))
        self._new_box_at([str(p) for p in live] + [str(target)], x, y)

    def _on_new_box_from_selection(self, paths, rect) -> None:
        """收纳盒里滚轮键选中的图标：在盒子外面旁边建个新盒子。"""
        self._new_box_at([str(p) for p in paths], 0, 0, beside=rect)

    def _on_desktop_wheel_box(self, point) -> None:
        """桌面上选中图标之后按滚轮键：在它们中间建个新盒子。"""
        try:
            indexes = di.selected_indexes()
        except Exception:  # noqa: BLE001 - 桌面接口抽风就当没按过
            return
        _log(f"wheel box: {len(indexes)} selected")
        files: list[str] = []
        nss: list[str] = []
        rects: list[tuple[int, int, int, int]] = []
        for idx in indexes:
            name = di.icon_name(idx)
            if not name:
                continue
            rect = di.item_rect(idx)
            if rect is not None:
                rects.append(rect)
            p = di.resolve_item(name)
            if p is not None and boxable(p):
                files.append(str(p))
                continue
            clsid = ns_items.clsid_for(name)
            if clsid and not ns_items.is_excluded(clsid):
                nss.append(clsid)
        if not files and not nss:
            return
        if rects:
            # 「几个图标中间」= 图标的几何重心（图标本身大小按外接矩形算权重）
            cx = sum((r[0] + r[2]) / 2.0 for r in rects) / len(rects)
            cy = sum((r[1] + r[3]) / 2.0 for r in rects) / len(rects)
        else:
            px, py = screen.to_physical((point.x(), point.y(), point.x(), point.y()))[0:2]
            cx, cy = float(px), float(py)
        lx, ly = screen.to_logical((cx, cy, cx, cy))[0:2]
        self._new_box_at(files, 0, 0, center=(lx, ly), nss=nss)

    def _new_box_at(self, paths, x, y, beside=None, center=None, nss=()) -> BoxWidget | None:
        """建一个新盒子并摆好：beside = 贴在某个矩形外面，center = 以某点为心。"""
        paths = [str(p) for p in paths if str(p)]
        if len(paths) + len(nss) < 2:
            return None      # 只有一个图标的盒子建了也会立刻解散，干脆不建
        box = self.store.create("收纳盒", int(x), int(y), paths)
        for clsid in nss:
            self.store.add_ns(box, ns_items.name_for(clsid), clsid)
        if len(self.store.items(box)) + len(box.hidden) < 2:
            # 有文件没搬进来（被占用 / 不合法）：把搬进去的放回原处，别留个空盒
            self.store.dissolve(box)
            return None
        win = self._spawn(box)
        win.toggle(False)
        self._push_undo(box.id)
        w, h = int(win._side_w), int(win._collapsed_h())
        if beside is not None:
            px, py = self._beside_pos(beside, w, h, skip=win)
        elif center is not None:
            px, py = int(center[0] - w / 2.0), int(center[1] - h / 2.0)
        else:
            px, py = int(x), int(y)
        px, py = self._clamp_box_pos(px, py, w, h)
        win.box.x, win.box.y = px, py
        self.store.place(win.box, px, py, w, h)   # 新建的盒子：这儿就是它的家
        win._apply_collapsed_geometry()
        if beside is not None:
            # 「贴边建盒」时如果一圈都占满了，最后还得把新盒子推出去；
            # 重心建盒 / 落在图标上建盒不能这么干——那个位置本来就是目标点
            win._avoid_overlap()
            if (win.box.x, win.box.y) != (px, py):
                self.store.place(win.box, win.box.x, win.box.y, w, h)
        QTimer.singleShot(0, lambda: self._make_room(win))
        self._sync_ui()
        return win

    def _beside_pos(self, rect, w: int, h: int, skip=None) -> tuple[int, int]:
        """贴在 rect 外面：右边一列往下排，左边一列往下排，最后铺到上下两行。

        连着按几次滚轮键时，算出来的「右边第一个位置」已经被上一个新盒子占住
        了，所以要从上往下一路找空位，不能每回都落在同一个点上（会直接叠住）。
        """
        gap = 10
        cands = []
        for i in range(12):
            cands.append((rect.right() + gap, rect.top() + i * (h + gap)))
            cands.append((rect.left() - gap - w, rect.top() + i * (h + gap)))
        for i in range(12):
            cands.append((rect.left() + i * (w + gap), rect.bottom() + gap))
            cands.append((rect.left() + i * (w + gap), rect.top() - gap - h))
        first = None
        for x, y in cands:
            px, py = self._clamp_box_pos(x, y, w, h)
            if first is None:
                first = (px, py)
            if not self._occupied(px, py, w, h, skip):
                return int(px), int(py)
        return first

    def _occupied(self, x, y, w: int, h: int, skip=None) -> bool:
        """这个位置是不是已经被盒子/卡片占了（留 6px 缝，别看重叠）。

        比的是**玻璃**矩形：窗口比玻璃多一圈透明余量，拿 frameGeometry 比会把
        本来留好的 10px 缝也算成占用。
        """
        rect = QRect(int(x), int(y), int(w), int(h)).adjusted(-6, -6, 6, 6)
        for other in self._siblings():
            if other is skip:
                continue
            try:
                r = (other.collapsed_rect() if hasattr(other, "collapsed_rect")
                     else aligner.glass_rect(other))
                if rect.intersects(r):
                    return True
            except (RuntimeError, TypeError):
                continue
        return False

    def _clamp_box_pos(self, x, y, w: int, h: int) -> tuple[int, int]:
        scr = QApplication.screenAt(QPoint(int(x), int(y))) or self.app.primaryScreen()
        avail = scr.availableGeometry()
        x = max(avail.left() + 4, min(int(x), avail.right() - int(w) - 4))
        y = max(avail.top() + 4, min(int(y), avail.bottom() - int(h) - 4))
        return int(x), int(y)

    def _make_room(self, win: BoxWidget) -> None:
        """盒子压到的桌面图标，挪到最近的空位去（自动排列打开时挪不动，跳过）。"""
        try:
            # 盒子坐标是逻辑像素，桌面图标那边要物理像素
            r = win.collapsed_rect()
            rect = (r.x(), r.y(), r.x() + r.width(), r.y() + r.height())
            di.make_room(screen.to_physical(rect))
        except Exception:  # noqa: BLE001 - 桌面接口偶尔抽风，别影响收纳
            pass

    def collapse_all(self) -> None:
        for win in self.windows.values():
            win.toggle(False)

    # ------------------------------------------------------------ 撤销栈
    UNDO_DEPTH = 12

    def _push_undo(self, box_id: str) -> None:
        """记下「这个盒子是刚建的」：面板的撤销按钮一次弹一个。"""
        self._undo = [b for b in self._undo if b != box_id]
        self._undo.append(str(box_id))
        if len(self._undo) > self.UNDO_DEPTH:
            del self._undo[:len(self._undo) - self.UNDO_DEPTH]

    def _forget_undo(self, box_id: str) -> None:
        """盒子被拆/被合并掉了：它不该再留在撤销栈里。"""
        box_id = str(box_id)
        if box_id in self._undo:
            self._undo = [b for b in self._undo if b != box_id]

    def undo_depth(self) -> int:
        """现在还能撤几个（栈里那些盒子可能已经被拆掉了，按活着的算）。"""
        return sum(1 for bid in self._undo
                   if self.store.get(bid) is not None and bid in self.windows)

    def undo_last(self) -> None:
        """撤销「上一次建盒」：把那个盒子解散、图标放回桌面。

        连续建了好几个盒子时，每按一次退一个（以前只记最后一个，撤一次就没了）。
        """
        while self._undo:
            box_id = self._undo.pop()
            box = self.store.get(box_id)
            win = self.windows.get(box_id)
            if box is None or win is None:
                continue          # 这个盒子已经被拆掉/合并了，往栈里退一个接着找
            moved = self.store.dissolve(box)
            still_there = self.store.get(box.id) is not None
            if not still_there:
                self.windows.pop(box_id, None)
                try:
                    win.dispose()
                except Exception:  # noqa: BLE001 - 监听器停不掉也得把窗口关掉
                    pass
                win.close()
                win.deleteLater()
            if still_there:
                self.toast.show_text(
                    f"{moved} 个图标已放回桌面；有图标挪不动，收纳盒保留了", 4000)
            else:
                self.toast.show_text(f"已撤销，{moved} 个图标放回桌面", 2500)
            self._sync_ui()
            return
        self._sync_ui()

    def rename_box(self, box_id: str, name: str) -> None:
        """面板里给盒子改名（桌面上双击标题那条路走的是盒子自己）。"""
        box = self.store.get(box_id)
        if box is None:
            return
        self.store.rename(box, str(name))
        win = self.windows.get(box_id)
        if win is not None:
            try:
                win.update()
            except RuntimeError:
                pass
        self._sync_ui()

    def open_boxes_dir(self) -> None:
        from .boxes import BOX_ROOT
        BOX_ROOT.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(BOX_ROOT)))

    def realign_button(self) -> None:
        """任务栏上增删了图标之后，手动让它重新对齐。"""
        button = getattr(self, "button", None)
        if button is not None:
            button._resync()
            self.toast.show_text("已按当前任务栏重新对齐", 1600)
        else:
            self.toast.show_text("现在用的是系统固定按钮，位置请直接拖动", 2000)

    # ------------------------------------------------------------ 外部信号
    def _start_signal_loop(self) -> None:
        """任务栏 / 开始菜单里的快捷方式按一下就置位一个事件，这里轮询。"""
        self._events = {name: sig.create(name)
                        for name in ("toggle", "panel", "quit", "reset")}

    def _start_poller(self) -> None:
        """光标 / 快捷键 / 外部信号三路轮询合成一个 30ms 的 tick。

        以前是三个定时器（30 / 40 / 30ms），一秒 90 多次唤醒，每次就查几个
        Win32 状态。合成一路之后唤醒少 60 次，代价只是三件事挤在同一帧里做。
        """
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(30)
        self._poll_timer.timeout.connect(self._poll_all)
        self._poll_timer.start()

    def _poll_all(self) -> None:
        self._poll_signals()
        overlay = getattr(self, "cursor_overlay", None)
        if overlay is not None:
            overlay.poll()
        hk = getattr(self, "hotkeys", None)
        if hk is not None:
            hk.poll()

    def _poll_signals(self) -> None:
        try:
            if sig.fired(self._events["toggle"]):
                _log("signal: toggle")
                self.toggle_beautify()
            if sig.fired(self._events["panel"]):
                _log("signal: panel")
                self.open_settings()
            if sig.fired(self._events["quit"]):
                _log("signal: quit")
                self.quit()
            if sig.fired(self._events["reset"]):
                # 卸载器发来的：先把图标放回桌面再退，别删完文件桌面空空如也
                _log("signal: reset（卸载前放回图标）")
                self.wipe_and_quit()
        except Exception:  # noqa: BLE001 - 别因为一个坏了的事件把程序拖死
            _log("signal 处理出错: " + traceback.format_exc())

    # ------------------------------------------------------------ 任务栏外观
    def _start_taskbar_watch(self) -> None:
        """任务栏透明靠 TranslucentTB 注入 explorer，explorer 一重启它就没了，
        所以这里常驻盯着，掉了就拉回来。

        顺带把「任务栏图标靠左」（`TaskbarAl = 0`）写一次——那是 Windows 自己的
        设置，不需要注入；以前只在开发机上手动改过，分出去的版本里没有这个值，
        别人装完图标还是居中的。只在启动时写一次，watchdog 里不重复写，
        免得用户自己去设置里改成居中又被掰回来。
        """
        try:
            if tb.set_left_align(getattr(self.cfg, "taskbar_left", True)):
                _log("任务栏图标已改为靠左 (TaskbarAl=0)")
        except Exception:  # noqa: BLE001 - 改不了对齐不影响别的
            pass
        tb.ensure(self.cfg.taskbar_clear, self.cfg.taskbar_maximized)
        self._tb_timer = QTimer(self)
        self._tb_timer.setInterval(8000)
        self._tb_timer.timeout.connect(self._watch_taskbar)
        self._tb_timer.start()

    def _watch_taskbar(self) -> None:
        try:
            if self.cfg.taskbar_clear and not tb.is_running():
                tb.ensure(self.cfg.taskbar_clear, self.cfg.taskbar_maximized)
                self._sync_ui()
        except Exception:  # noqa: BLE001
            pass

    def set_taskbar_clear(self, on: bool) -> None:
        self.cfg.taskbar_clear = bool(on)
        self.cfg.save()
        if on:
            tb.ensure(True, self.cfg.taskbar_maximized)
        else:
            tb.stop()
        self._sync_ui()

    def set_taskbar_maximized(self, mode: str) -> None:
        self.cfg.taskbar_maximized = mode if mode in tb.MAXIMIZED_MODES else "acrylic"
        self.cfg.save()
        if self.cfg.taskbar_clear:
            tb.apply_settings(self.cfg.taskbar_maximized, True)
        self._sync_ui()

    def set_panel_dark(self, on: bool) -> None:
        """面板切深色 / 浅色（蓝紫两套雾玻璃）。"""
        self.cfg.panel_dark = bool(on)
        self.cfg.panel_theme = "dark" if on else "light"
        self.cfg.save()

    def set_panel_theme(self, code: str) -> None:
        """面板主题：light / dark / auto（跟随 Windows 应用主题）。"""
        code = str(code or "light").lower()
        if code not in ("light", "dark", "auto"):
            code = "light"
        self.cfg.panel_theme = code
        self.cfg.panel_dark = code == "dark"
        self.cfg.save()

    def set_auto_align(self, on: bool) -> None:
        """自适应对齐：关掉之后拖卡片/收纳盒松手都不再吸边、也不避让。

        以前这里只往卡片上设，盒子那边根本读不到这个值；现在两边都设
        （盒子在 `_avoid_overlap` 里判断）。
        """
        self.cfg.auto_align = bool(on)
        self.cfg.save()
        for win in list(self.card_widgets.values()) + list(self.windows.values()):
            try:
                win.auto_align = bool(on)
            except (RuntimeError, TypeError):
                continue

    def set_box_merge(self, on: bool) -> None:
        """收纳盒合并开关：关掉之后把一个盒子拖到另一个上面不会合并。"""
        self.cfg.box_merge = bool(on)
        self.cfg.save()

    def set_box_merge_bar(self, on: bool) -> None:
        """两个都只剩横杠（状态 -1）的盒子要不要也能互相合并（默认不要）。"""
        self.cfg.box_merge_bar = bool(on)
        self.cfg.save()
        for win in list(getattr(self, "windows", {}).values()):
            try:
                win.allow_bar_merge = bool(on)
            except (RuntimeError, TypeError):
                continue

    def set_box_sort(self, key: str) -> None:
        """面板里收纳盒列表的排序：name（默认）/ size / created。"""
        key = str(key or "name")
        self.cfg.box_sort = key if key in ("name", "size", "created") else "name"
        self.cfg.save()

    def set_lang(self, code: str) -> None:
        """换界面语言（zh / en）。

        文案是**用的时候**才查表，所以卡片/盒子菜单下次弹出就变了；面板是建窗口
        时取的字，得由面板自己重建一次（见 settings.SettingsWindow._on_lang）。
        """
        code = str(code or "").lower()
        self.cfg.lang = code if code in ("zh", "en") else "zh"
        self.cfg.save()
        i18n_set_lang(self.cfg.lang)

    def destroy_world(self) -> int:
        """毁灭世界：销毁所有收纳盒（图标放回桌面、摆在盒子附近），删掉所有卡片。

        返回销毁掉的收纳盒数量。卡片是配置里的列表，清空 + apply_cards 就没了。
        """
        count = 0
        for win in list(self.windows.values()):
            try:
                if win.destroy_box(silent=True):
                    count += 1
            except (RuntimeError, TypeError, AttributeError):
                continue
        self.cfg.cards = []
        self.cfg.save()
        self.apply_cards(animate=False)
        _log(f"world destroyed: {count} boxes gone, cards cleared")
        return count

    def wipe_and_quit(self) -> None:
        """卸载前那句「毁灭世界」：图标全部放回桌面，然后退出。

        顺序很关键：先把图标挪出盒子（`destroy_world` 里每个盒子都做一次
        `remove_to`，文件回桌面、位置摆在盒子附近），再让进程退出。卸载器
        在事件消失之后才删文件——反过来的话，图标就跟着 `%APPDATA%` 一起
        被删掉，桌面直接空掉。
        """
        if getattr(self, "_wiping", False):
            return
        self._wiping = True
        try:
            n = self.destroy_world()
            _log(f"uninstall wipe done: {n} boxes back to desktop")
        except Exception:  # noqa: BLE001
            _log("uninstall wipe 出错: " + traceback.format_exc())
        # 留一点时间让图标位置 / 配置落盘，然后正常退出（走 aboutToQuit）。
        # 注意：放回桌面的图标要等 explorer 画出来才能摆位置（实测 0.5–1s，
        # 十几个一起更慢），所以这里等 `place_pending()` 归零再退，
        # 否则定时器被一起杀掉，图标就留在桌面左上角第一格。
        QTimer.singleShot(500, lambda: self._quit_after_places())

    def _quit_after_places(self, tries: int = 0) -> None:
        try:
            from . import box_widget as _bw
            left = _bw.place_pending()
        except Exception:  # noqa: BLE001
            left = 0
        if left and tries < 50:      # 50 × 100ms = 5s 封顶
            QTimer.singleShot(100, lambda: self._quit_after_places(tries + 1))
            return
        if left:
            _log(f"uninstall wipe: 还有 {left} 个图标没落位，先退出")
        else:
            _log("uninstall wipe: 图标都落位了")
        self.quit()

    def uninstall_app(self) -> None:
        """面板里点「卸载 desktop beauty」：图标先回桌面，再删安装。

        和 `desktopbeauty-app.exe --uninstall` 是同一套删除逻辑（`install.uninstall`），
        区别在于这里**我们自己**就是那个常驻实例：`destroy_world()` 当场做掉，
        不能再去发 reset 事件——那会变成自己等自己退出。删目录仍旧交给
        `_delete_dir_later()`（进程还占着 exe，只能退出去再 `rmdir`）。
        """
        if getattr(self, "_wiping", False):
            return
        self._wiping = True
        try:
            n = self.destroy_world()
            _log(f"panel uninstall: {n} boxes back to desktop, cards cleared")
        except Exception:  # noqa: BLE001
            _log("panel uninstall wipe 出错: " + traceback.format_exc())
        try:
            from . import install as install_mod
            what = install_mod.uninstall(wipe=False)
            _log(f"panel uninstall: shortcuts / uninstall entry gone ({what})")
        except Exception:  # noqa: BLE001
            _log("panel uninstall 出错: " + traceback.format_exc())
        try:
            if self.settings is not None:
                self.settings.close()
        except (RuntimeError, TypeError):
            pass
        QTimer.singleShot(500, self.quit)

    def set_cursor_style(self, color: str | None = None,
                         alpha: int | None = None, save: bool = True) -> None:
        """面板里改圆点颜色 / 透明度。save=False 只预览（拖滑条时用）。"""
        if color is not None:
            self.cfg.cursor_color = str(color)
        if alpha is not None:
            self.cfg.cursor_alpha = max(0, min(100, int(alpha)))
        if save:
            self.cfg.save()
        overlay = getattr(self, "cursor_overlay", None)
        if overlay is not None:
            overlay.set_style(self.cfg.cursor_color, self.cfg.cursor_alpha)

    def set_cursor_dot(self, on: bool) -> None:
        """桌面范围的圆点拖尾指针：开关一拨立刻生效。"""
        self.cfg.cursor_dot = bool(on)
        self.cfg.save()
        if on and self.cursor_overlay is None:
            try:
                from .cursor import CursorOverlay, restore_system_cursor
                restore_system_cursor()
                self.cursor_overlay = CursorOverlay(external=True)
                self.cursor_overlay.set_style(self.cfg.cursor_color,
                                              self.cfg.cursor_alpha)
                self.app.aboutToQuit.connect(self.cursor_overlay.stop)
                for w in list(self.windows.values()) + list(self.card_widgets.values()):
                    self._cursor_register(w)
            except Exception:  # noqa: BLE001 - 指针是锦上添花，坏了不能影响桌面
                self.cursor_overlay = None
                try:
                    import traceback
                    _log_force("cursor overlay unavailable:\n%s",
                               traceback.format_exc())
                except Exception:  # noqa: BLE001
                    pass
        elif not on and self.cursor_overlay is not None:
            try:
                self.cursor_overlay.stop()
            except (RuntimeError, TypeError):
                pass
            self.cursor_overlay = None
        self._sync_ui()

    def taskbar_state(self) -> tuple[bool, bool]:
        """(能不能用, 现在在不在跑)"""
        return tb.available(), tb.is_running()

    # ------------------------------------------------------------ 界面同步
    def _sync_ui(self) -> None:
        """总开关、撤销可用性变了以后，把任务栏按钮和面板一起刷新。"""
        button = getattr(self, "button", None)
        if button is not None:
            button.refresh()
        if self.settings is not None:
            self.settings.sync_state()

    def quit(self) -> None:
        _log("quit requested (exit 快捷方式 / 信号)")
        self.watcher.stop()
        music = getattr(self, "music", None)
        if music is not None:
            music.shutdown()
        hk = getattr(self, "hotkeys", None)
        if hk is not None:
            hk.release()
        self.preview.hide()
        if self.settings is not None:
            self.settings.close()
        button = getattr(self, "button", None)
        if button is not None:
            button.close()
        self.store.save()
        self.cfg.save()
        self.app.quit()


def _log(msg: str) -> None:
    """记一行日志：pythonw 没有控制台，出事后只能靠这个看它是怎么没的。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        p = DATA_DIR / "app.log"
        if p.exists() and p.stat().st_size > 256 * 1024:
            p.write_text("", encoding="utf-8")
        with p.open("a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:  # noqa: BLE001
        pass


def _excepthook(exc_type, exc, tb) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with (DATA_DIR / "error.log").open("a", encoding="utf-8") as fh:
        traceback.print_exception(exc_type, exc, tb, file=fh)
    sys.__excepthook__(exc_type, exc, tb)


_PROF = {"last_exit": 0.0, "prev": "", "win": 0.0, "n": 0, "sup": 0,
         "modal": 0, "modal_seq": 0}
_EV_NAMES: dict[int, str] = {}


def _ev_name(event) -> str:
    """事件类型名（缓存：这个函数每条事件都会走一遍）。"""
    try:
        t = int(event.type())
    except Exception:  # noqa: BLE001
        return "?"
    name = _EV_NAMES.get(t)
    if name is None:
        try:
            name = getattr(event.type(), "name", None) or str(t)
        except Exception:  # noqa: BLE001
            name = str(t)
        _EV_NAMES[t] = name
    return name


def _obj_name(obj) -> str:
    try:
        n = obj.objectName()
        if n:
            return n
    except Exception:  # noqa: BLE001
        pass
    try:
        return type(obj).__name__
    except Exception:  # noqa: BLE001
        return "?"


def _lis_keep(seq: list[int]) -> set[int]:
    """最长递增子序列的下标：这些窗口的相对次序已经是对的，不用动。"""
    n = len(seq)
    if n <= 1:
        return set(range(n))
    best = [1] * n
    back = [-1] * n
    for i in range(n):
        for j in range(i):
            if seq[j] < seq[i] and best[j] + 1 > best[i]:
                best[i] = best[j] + 1
                back[i] = j
    i = max(range(n), key=lambda k: best[k])
    keep: set[int] = set()
    while i >= 0:
        keep.add(i)
        i = back[i]
    return keep


class _ProfApp(QApplication):
    """给每一条事件分发计时：一条超过 25ms、或者两条之间空了 60ms 就写进日志。

    查「拖卡片一顿一顿」时，只看我们自己的 Python 没用：实测 `move()` 0.5ms、
    paintEvent 1~2ms，慢的部分全在 `notify()` 里面的 C++ 分支（Qt 布局/合成、
    定时器里的抓图……）。看门狗抓到的栈永远只有 `app.exec()`，就是这个原因。
    这里连**接收者**一起记下来，一眼能看出是哪个窗口、哪类事件占住了主线程。
    """

    def notify(self, receiver, event) -> bool:  # noqa: N802
        t0 = time.perf_counter()
        modal0 = _PROF["modal"]
        seq0 = _PROF["modal_seq"]
        try:
            return super().notify(receiver, event)
        finally:
            try:
                t1 = time.perf_counter()
                dt = (t1 - t0) * 1000.0
                gap = (t0 - _PROF["last_exit"]) * 1000.0 if _PROF["last_exit"] else 0.0
                _PROF["last_exit"] = t1
                prev = _PROF["prev"]
                _PROF["prev"] = _ev_name(event)
                # 菜单是模态嵌套循环：open 期间（以及包着它的那条事件）时间不是
                # 卡顿，别记成慢事件（以前点一下三个点就写一条「MetaCall dt=1100」）
                in_modal = bool(modal0 or _PROF["modal"]
                                or _PROF["modal_seq"] != seq0)
                if (dt > 25.0 or gap > 60.0) and not in_modal:
                    if t1 - _PROF["win"] > 0.2:
                        _PROF["win"] = t1
                        _PROF["n"] = 0
                    _PROF["n"] += 1
                    if _PROF["n"] <= 10:
                        drag = getattr(receiver, "_window_dragging", None)
                        dlog("slow event: %s dt=%.0f gap=%.0f prev=%s recv=%s drag=%s"
                             % (_ev_name(event), dt, gap, prev, _obj_name(receiver),
                                "-" if drag is None else drag))
                    else:
                        _PROF["sup"] += 1
            except Exception:  # noqa: BLE001 - 计时出错绝不能影响界面
                pass


def _shot(app: QApplication, ctrl: Controller, out_dir: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    def grab(widget, name: str, pad: int = 60) -> None:
        scr = widget.screen() or app.primaryScreen()
        dpr = scr.devicePixelRatio()
        full = scr.grabWindow(0).toImage()
        geo = widget.frameGeometry()
        x = int(max(0, (geo.x() - scr.geometry().x() - pad) * dpr))
        y = int(max(0, (geo.y() - scr.geometry().y() - pad) * dpr))
        w = min(int((geo.width() + pad * 2) * dpr), full.width() - x)
        h = min(int((geo.height() + pad * 2) * dpr), full.height() - y)
        full.copy(x, y, w, h).save(str(out / name))

    def step() -> None:
        cards = list(ctrl.card_widgets.values())
        if cards:
            grab(cards[0], "clock.png")
        if ctrl.windows:
            box = next(iter(ctrl.windows.values()))
            box.toggle(True)
            QTimer.singleShot(400, lambda: (grab(box, "box.png"), app.quit()))
        else:
            app.quit()

    QTimer.singleShot(1200, step)


def _run_setup_only(app: QApplication) -> int:
    """升级 / 覆盖安装：只弹那块安装窗。

    不建 Controller（盒子、卡片、快捷键、鼠标钩子全都不碰），也不抢
    `%TEMP%\\deskbeauty.lock`——旧版本很可能正跑着，抢锁会让本进程直接退出，
    用户看到的就是「双击了新版却没反应」。安装动作本身（停旧实例 + 覆盖复制）
    在 `install.install()` 里。
    """
    from .config import Config
    from .setup_window import SetupWindow

    _log("setup-only 模式（升级 / 覆盖安装）")
    try:
        cfg = Config.load()
        radius = int(getattr(cfg, "box_radius", 0) or 10)
    except Exception:  # noqa: BLE001 - 配置坏了也得能装
        radius = 10
    app.setQuitOnLastWindowClosed(True)
    win = SetupWindow(radius=radius, upgrade=True)
    win.show()
    return app.exec()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    ensure_dirs()
    sys.excepthook = _excepthook

    # 打包版的两个命令行入口（「应用和功能」卸载、以及装机的静默路径）
    if "--uninstall" in argv:
        from . import install as install_mod
        install_mod.uninstall()
        return 0
    if "--install-silent" in argv:
        from . import install as install_mod
        try:
            return 0 if install_mod.install() else 2
        except Exception:  # noqa: BLE001
            return 1

    # 硬崩溃（段错误 / 访问违规）不会走 excepthook，用 faulthandler 落盘。
    try:
        _crash_fh = (DATA_DIR / "crash.log").open("a", encoding="utf-8")
        faulthandler.enable(file=_crash_fh, all_threads=True)
        _log("faulthandler 已启用")
    except Exception:  # noqa: BLE001
        pass

    app = _ProfApp(argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("desktop beauty")
    app.setApplicationDisplayName("desktop beauty")
    app.setWindowIcon(make_app_icon())
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(12)
    app.setFont(font)
    app.setStyleSheet(GLOBAL_QSS)

    if "--setup" in argv:
        return _run_setup_only(app)

    lock = QLockFile(str(Path(tempfile.gettempdir()) / "deskbeauty.lock"))
    if not lock.tryLock(120):
        _log("已在运行，本次启动退出")
        return 0

    _log(f"启动 pid={os.getpid()} 参数={argv[1:]}")
    app.aboutToQuit.connect(lambda: _log(f"正常退出 pid={os.getpid()}"))

    cfg = Config.load()
    ctrl = Controller(app, cfg)
    app.aboutToQuit.connect(lambda: lock.unlock())

    if "--panel" in argv:
        QTimer.singleShot(500, ctrl.open_settings)

    if "--install" in argv:
        QTimer.singleShot(300, ctrl.offer_install)

    if "--reset-quit" in argv:
        # 卸载器在「常驻进程没跑」时拉起的：毁灭世界一次，然后自己退
        QTimer.singleShot(1200, ctrl.wipe_and_quit)

    if "--shot" in argv:
        idx = argv.index("--shot")
        target = argv[idx + 1] if len(argv) > idx + 1 else str(Path.cwd() / "shots")
        _shot(app, ctrl, target)

    return app.exec()
