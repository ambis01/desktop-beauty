"""全局鼠标钩子：捕捉「把一个桌面图标拖到另一个图标上」这个手势。

只做三件事：
1. 低层鼠标钩子里**只记坐标**（回调必须极快，不能做任何耗时操作）；
2. 拖拽中用 20ms 的定时器拿 LVM_HITTEST 判断光标压在哪个图标上；
3. 悬停 0.18 秒就把 explorer 的拖拽用 Esc 抢过来（拖拽一死，松手就不可能
   再触发 Windows 的「用目标程序打开」），然后**松手**才建盒。按住不放不会
   建盒。快速掠过（低于 0.18 秒）完全不受影响。

这样普通拖拽整理图标（落在空白处）还是系统的行为，只有「停在另一个图标上」
才会变成合并。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import time

from PySide6.QtCore import QObject, QPoint, QRect, QTimer, Signal

from . import desktop_icons as di
from . import ns_items
from .boxes import acceptable as boxable

DEBUG = bool(os.environ.get("DESKBEAUTY_DEBUG"))


def _log(msg: str, *args) -> None:
    if not DEBUG:
        return
    try:
        with open(os.path.join(os.environ.get("TEMP", "."), "deskbeauty-drag.log"),
                  "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} {msg % args if args else msg}\n")
    except OSError:
        pass

WH_MOUSE_LL = 14
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_MBUTTONDOWN = 0x0207
VK_LBUTTON = 0x01
VK_ESCAPE = 0x1B
# 悬停到 0.18s 就发 Esc 把 explorer 的拖拽抢过来：explorer 的拖拽一死，
# 松手就不可能再变成「用 xx 打开」——这是把那个功能彻底取消掉的关键。
DWELL_HINT = 0.15
DRAG_THRESHOLD = 8
IDLE_INTERVAL = 50    # 平时 50ms 轮询
DRAG_INTERVAL = 20    # 拖拽中加快，别漏掉松手前那一刻的命中

user32 = ctypes.WinDLL("user32")
kernel32 = ctypes.WinDLL("kernel32")

user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, wt.HINSTANCE, wt.DWORD]
user32.CallNextHookEx.restype = wt.LPARAM
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.keybd_event.argtypes = [wt.BYTE, wt.BYTE, wt.DWORD, ctypes.c_size_t]
user32.WindowFromPoint.restype = wt.HWND
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.GetClassNameW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]

KEYEVENTF_KEYUP = 0x0002
# 光标压在桌面本体上才算「停在图标上」：压在我们自己的收纳盒上时要放行，
# 否则「把图标拖进收纳盒」会被这里抢断。
DESKTOP_CLASSES = {"SysListView32", "SHELLDLL_DefView", "Progman", "WorkerW"}


def _over_desktop(x: int, y: int) -> bool:
    try:
        hwnd = user32.WindowFromPoint(wt.POINT(x, y))
        if not hwnd:
            return True
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, buf, 64)
        return buf.value in DESKTOP_CLASSES
    except Exception:  # noqa: BLE001 - 判断不出来就按桌面算，别把功能卡死
        return True


def _cancel_system_drag() -> None:
    """给当前拖拽发一个 Esc：让 explorer 放弃 drop，避免它执行「用目标程序打开」。"""
    try:
        user32.keybd_event(VK_ESCAPE, 0, 0, 0)
        user32.keybd_event(VK_ESCAPE, 0, KEYEVENTF_KEYUP, 0)
        _log("sent ESC to cancel shell drag")
    except OSError:
        pass


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wt.POINT),
        ("mouseData", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


_HOOKPROC = ctypes.WINFUNCTYPE(wt.LPARAM, ctypes.c_int, wt.WPARAM, wt.LPARAM)


class DragWatcher(QObject):
    hint = Signal(object, str)            # (屏幕矩形, 提示文字)：老的浮层，已不用
    armed = Signal(object, str, str)      # (目标矩形, 源路径, 目标路径)：可以预览了
    clear = Signal()
    merged = Signal(object, str, str)     # (屏幕矩形, 源路径, 目标路径)
    cancelled = Signal()
    # 滚轮键按在桌面上：把桌面上选中的图标收成一个新盒子
    wheel_box = Signal(object)            # (按下点的屏幕坐标 QPoint)
    # 桌面上拉框选收纳盒：按下 / 拖动中 / 松开（物理像素；松手时空矩形 = 单击）
    band_started = Signal(object)         # QPoint
    band_moved = Signal(object)           # QRect
    band_finished = Signal(object)        # QRect

    def __init__(self, parent=None):
        super().__init__(parent)
        self._x = self._y = 0
        self._down: tuple[int, int, float] | None = None
        self._dragging = False
        self._up_pending = False
        self._up_pt: tuple[int, int] | None = None
        self._source: tuple[int, str, str] | None = None   # idx, name, path
        self._target: tuple[int, str, str] | None = None   # idx, name, path
        self._target_rect: tuple[int, int, int, int] | None = None
        self._last_target: tuple[int, str, str] | None = None
        self._target_since = 0.0
        self._target_lost = 0.0
        self._hinted = False
        self._committed = False
        self._esc_sent = False
        self._swallow_up = False
        self._hint_rect = None
        # 抢过 explorer 的拖拽之后，图标要由我们自己带着走
        self._own_drag = False
        self._src_home: tuple[int, int] | None = None
        self._center_off: tuple[int, int] | None = None
        self._rects: dict[int, tuple[int, int, int, int]] = {}
        self._names: dict[int, tuple[str, str | None]] = {}
        # 空闲期缓存：鼠标都没按的时候先把「每个图标在屏幕哪儿、叫什么」抄下来。
        # 必须在按下之前拿到——explorer 自己的拖拽一启动，LVM_HITTEST 就返回 -1，
        # 那时再问 listview「我按的是谁」就问不出来了。
        self._idle_rects: dict[int, tuple[int, int, int, int]] = {}
        self._idle_names: dict[int, str] = {}
        self._cache_at = 0.0
        self._cache_ok = False
        # 滚轮键：钩子里只记坐标，真正的活儿交给 _tick（钩子里调 Qt 信号
        # 会把整段逻辑塞进系统回调，那边卡一下全系统的鼠标都跟着卡）
        self._wheel_pt: tuple[int, int] | None = None
        # 框选收纳盒：起点（按下点，物理像素）+ 上一次发出去的矩形
        self._band: tuple[int, int] | None = None
        self._band_rect: QRect | None = None
        self._down_desktop = False

        self._proc = _HOOKPROC(self._callback)
        hmod = kernel32.GetModuleHandleW(None)
        self._hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc, hmod, 0)

        self._timer = QTimer(self)
        self._timer.setInterval(IDLE_INTERVAL)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # ------------------------------------------------------------ 钩子
    def _callback(self, code, wparam, lparam):
        if code >= 0:
            try:
                data = _MSLLHOOKSTRUCT.from_address(lparam)
                self._x, self._y = data.pt.x, data.pt.y
                if wparam == WM_MBUTTONDOWN and _over_desktop(self._x, self._y):
                    self._wheel_pt = (self._x, self._y)
                if wparam == WM_LBUTTONDOWN:
                    self._down = (self._x, self._y, time.monotonic())
                    self._down_desktop = _over_desktop(self._x, self._y)
                    self._band = None
                    self._band_rect = None
                    self._dragging = False
                    self._source = None
                    self._committed = False
                    self._esc_sent = False
                    self._swallow_up = False
                    self._own_drag = False
                    self._src_home = None
                    self._center_off = None
                    self._rects = {}
                    self._names = {}
                    self._up_pt = None
                    self._reset_target()
                    _log("down at %s,%s", self._x, self._y)
                elif wparam == WM_MOUSEMOVE and self._down and not self._dragging:
                    dx = self._x - self._down[0]
                    dy = self._y - self._down[1]
                    if dx * dx + dy * dy > DRAG_THRESHOLD * DRAG_THRESHOLD:
                        self._dragging = True
                        _log("drag start")
                elif wparam == WM_LBUTTONUP:
                    self._up_pt = (self._x, self._y)
                    self._up_pending = True
                    if (self._dragging and self._source is not None
                            and self._target_at(self._x, self._y) is not None):
                        # 拖到另一个图标上松手（不管松得多快）：这一下**不能让
                        # explorer 收到** —— 它会把「快捷方式丢在快捷方式上」执行
                        # 成「用目标程序打开」（用户反复报的 bug）。吞掉 up，
                        # _tick 里补一次「抢拖拽 + 提交」。
                        _log("swallow up at %s,%s", self._x, self._y)
                        if not self._esc_sent:
                            self._swallow_up = True
                        return 1
            except Exception:  # noqa: BLE001 - 钩子里不能抛
                pass
        return user32.CallNextHookEx(self._hook, code, wparam, lparam)

    # ------------------------------------------------------------ 轮询
    def _tick(self) -> None:
        if self._wheel_pt is not None:
            pt, self._wheel_pt = self._wheel_pt, None
            self.wheel_box.emit(QPoint(pt[0], pt[1]))
        # 拖拽中看得更勤一点，松手那一刻尽量别漏
        want = DRAG_INTERVAL if self._dragging else IDLE_INTERVAL
        if self._timer.interval() != want:
            self._timer.setInterval(want)

        # 空闲时（鼠标没按下）把图标位置抄一份缓存，按下后就不必再问 listview
        now = time.monotonic()
        if not self._down and not self._dragging and now - self._cache_at > 0.4:
            self._cache_at = now
            self._refresh_cache()

        if self._up_pending:
            self._up_pending = False
            self._finish()
            return

        if not self._dragging:
            if self._down and not (user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000):
                self._down = None
            return

        if not (user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000):
            self._finish()
            return

        # 正在桌面上拉框选收纳盒：只更新矩形，别去认图标
        if self._band is not None:
            self._band_update()
            return

        if self._source is None:
            sx, sy = self._down[0], self._down[1]
            if not _over_desktop(sx, sy):
                # 按在别的窗口里（拖文字 / 拖图片…）：这个手势不是桌面图标拖拽，
                # 别按坐标缓存硬认成某个图标——不然后面松手落点上正好有图标时
                # 会被我们当成「拖到一起建盒」，把别人的拖拽吞掉。
                _log("press not on desktop -> ignore")
                self._abort()
                return
            # 先用空闲期缓存：explorer 的拖拽一启动，LVM_HITTEST 就对不出索引了，
            # 这时候再去问 listview「我按的是谁」只会得到 -1。
            # 但缓存太旧（>1.5s）就不可信——宁可这次不建盒，也不能把错的图标收走。
            fresh = self._cache_ok and (time.monotonic() - self._cache_at) <= 1.5
            idx = self._hit_in(sx, sy, self._idle_rects) if fresh else -1
            name = self._idle_names.get(idx, "") if idx >= 0 else ""
            path = self._path_for(name) if name else None
            if not name:
                # 缓存是冷的（程序刚起来 / 桌面刚变过）→ 再直接问一次
                idx, name, path = di.item_at_screen(sx, sy)
                path = str(path) if path else None
            _log("source hit idx=%s name=%r path=%s", idx, name, path)
            if not path and name:
                # 此电脑 / 回收站 / 控制面板 不是文件，走 `ns::{CLSID}` 假路径，
                # 跟目标那边 `_info()` 的处理保持一致
                path = self._path_for(name)
                _log("source resolved to %s", path)
            if not path:
                # 不是从图标开始的拖拽：这一下就是「在桌面上拉框选收纳盒」
                self._start_band(sx, sy)
                return
            self._source = (idx, name, path)
            # 拍一张图标矩形快照：之后每 20ms 用纯几何判断光标压在谁身上，
            # 比每次跨进程 LVM_HITTEST 快，也不会把「被拖着的自己」算成目标
            self._rects = dict(self._idle_rects)
            if not self._rects:
                try:
                    self._rects = di.list_rects()
                except di.DesktopIconsError:
                    self._rects = {}
            self._names = {idx: (name, path)}

        if self._own_drag and self._source is not None:
            # explorer 的拖拽已经被 Esc 掐掉，图标得我们自己带着走，
            # 否则用户中途放弃时图标会“隐身”在原地，而且再也拖不动
            di.follow_item(self._source[0], self._x, self._y,
                           self._center_off or (0, 0))

        idx = self._hit(self._x, self._y)
        info = self._info(idx) if idx >= 0 else None
        if info is None or not info[1] or not _over_desktop(self._x, self._y):
            # 光标离开了目标图标：先保留 0.4s，避免松手瞬间手抖导致不触发
            if self._target is not None:
                self._target = None
                self._target_lost = time.monotonic()
                self.clear.emit()
            elif self._hinted and time.monotonic() - self._target_lost > 0.4:
                self._hinted = False
                self._hint_rect = None
                self.clear.emit()
            return

        name, path = info
        if self._target is None or self._target[0] != idx:
            self._target = (idx, name, path)
            self._last_target = self._target
            self._target_rect = self._rects.get(idx)
            self._target_since = time.monotonic()
            self._target_lost = 0.0
            self._hinted = False
            self._hint_rect = None
            self.clear.emit()
            return

        elapsed = time.monotonic() - self._target_since
        if not self._hinted and elapsed >= DWELL_HINT:
            if self._target_rect:
                self._hinted = True
                self._hint_rect = self._target_rect
                # 把 explorer 这次拖拽抢过来：之后松手只会是收纳，不会是「用xx打开」
                self._cancel_once()
                self._take_over()
                src = self._source
                dst = self._target
                if src and dst and src[2] and dst[2]:
                    self.armed.emit(self._target_rect, str(src[2]), str(dst[2]))
        # 注意：这里**不**自动收纳。必须等用户真的松开左键（见 _finish），
        # 否则按住不放盒子就自己冒出来了。

    # ------------------------------------------------------------ 命中
    def _hit(self, x: int, y: int) -> int:
        """光标压在哪个图标上（不算自己拖着的那个）；空白处 -1。"""
        src = self._source[0] if self._source else -1
        for idx, (left, top, right, bottom) in self._rects.items():
            if idx == src:
                continue
            if left <= x <= right and top <= y <= bottom:
                return idx
        return -1

    def _hit_in(self, x: int, y: int, rects) -> int:
        """纯几何命中（给定矩形表），不跟 listview 通信。"""
        for idx, (left, top, right, bottom) in (rects or {}).items():
            if left <= x <= right and top <= y <= bottom:
                return idx
        return -1

    def _target_at(self, x: int, y: int):
        """松手点是不是压在另一个桌面图标上 → 返回 (idx, 名字, 路径)。

        只用空闲期抄好的缓存，钩子里不能做重调用。
        """
        try:
            if not _over_desktop(x, y):
                try:
                    hwnd = user32.WindowFromPoint(wt.POINT(x, y))
                    buf = ctypes.create_unicode_buffer(64)
                    user32.GetClassNameW(hwnd, buf, 64)
                    _log("target_at: not desktop at %s,%s hwnd=%s cls=%r",
                         x, y, hwnd, buf.value)
                except Exception:  # noqa: BLE001
                    pass
                return None
            idx = self._hit_in(x, y, self._idle_rects)
            if idx < 0 or (self._source and idx == self._source[0]):
                _log("target_at: no hit at %s,%s (rects=%s)", x, y,
                     len(self._idle_rects))
                return None
            name = self._idle_names.get(idx, "")
            path = self._path_for(name) if name else None
            _log("target_at: idx=%s name=%r path=%s", idx, name, path)
            return (idx, name, path) if path else None
        except Exception as exc:  # noqa: BLE001 - 钩子里不许抛
            _log("target_at error %r", exc)
            return None

    def _refresh_cache(self) -> None:
        """空闲期抄一份「图标索引 → 屏幕矩形 / 名字」。

        必须在鼠标按下之前抄：explorer 进入自己的拖拽循环后，`LVM_HITTEST`
        和 `LVM_GETITEMRECT` 都拿不到东西了。
        """
        try:
            rects = di.list_rects()
            icons = di.list_icons()
        except Exception:  # noqa: BLE001 - 桌面接口偶尔抽风，保留上一份缓存
            self._cache_ok = False
            return
        self._idle_rects = rects
        self._idle_names = {it.index: it.name for it in icons}
        self._cache_ok = True

    def _path_for(self, name: str) -> str | None:
        """显示名 → 可收纳的路径：普通文件（含快捷方式）的真实路径，或系统图标的 `ns::{CLSID}`。"""
        if not name:
            return None
        p = di.resolve_item(name)
        if p is not None and boxable(p):
            return str(p)
        clsid = ns_items.clsid_for(name)
        if not clsid or ns_items.is_excluded(clsid):
            return None      # 回收站这类图标不参与「拖到一起建盒」
        return ns_items.pseudo(clsid)

    def _info(self, idx: int) -> tuple[str, str | None] | None:
        """图标索引 → (显示名, 快捷方式路径)；不是快捷方式返回 None。

        此电脑 / 回收站 / 控制面板 / 网络 不是文件，给一个 `ns::{CLSID}` 的假路径，
        让合并流程知道「这一项是系统图标」，由上层去建 CLSID 快捷方式并隐藏桌面图标。
        """
        if idx in self._names:
            return self._names[idx]
        name = di.icon_name(idx)
        path = self._path_for(name) if name else None
        key = (name, path)
        self._names[idx] = key
        return key

    # ------------------------------------------------------------ 提交
    def _cancel_once(self) -> None:
        if not self._esc_sent:
            self._esc_sent = True
            _cancel_system_drag()

    def _take_over(self) -> None:
        """Esc 掐掉 explorer 的拖拽之后，由我们自己把图标带着走。

        不这么做的话：用户看到提示后不松手又挪开，图标会瞬间“隐身”回原位，
        之后松在空白处也挪不动图标——很容易在正常整理图标时误触。
        """
        if self._own_drag or self._source is None:
            return
        self._own_drag = True
        idx = self._source[0]
        self._src_home = di.item_position(idx)
        self._center_off = di.icon_center_offset(idx)
        _log("take over drag idx=%s home=%s off=%s",
             idx, self._src_home, self._center_off)

    def _commit(self, rect) -> None:
        """到点了：系统拖拽已经放弃，这里只负责把两个文件收进盒子。"""
        self._committed = True
        self._cancel_once()
        src, dst = self._source, self._target
        self._hard_reset()
        self.clear.emit()
        _log("commit src=%s dst=%s", src[2] if src else None, dst[2] if dst else None)
        if src and dst and src[2] and dst[2]:
            # 必须显式转 str：信号参数是 str，直接发 Path 会被 Qt 静默转成空串
            self.merged.emit(rect, str(src[2]), str(dst[2]))
        else:
            self.cancelled.emit()

    def _up_on_target(self) -> bool:
        rect = self._target_rect
        if not rect:
            return False
        x, y = self._up_pt or (self._x, self._y)
        pad = 6
        return (rect[0] - pad <= x <= rect[2] + pad
                and rect[1] - pad <= y <= rect[3] + pad)

    # ------------------------------------------------------------ 框选
    def _start_band(self, x: int, y: int) -> None:
        """在桌面空白处拖起来：拉框选收纳盒（矩形交给 Controller 去挑盒子）。"""
        self._band = (int(x), int(y))
        self._band_rect = None
        _log("band start %s,%s", x, y)
        self.band_started.emit(QPoint(int(x), int(y)))

    def _band_rect_now(self) -> QRect:
        if self._band is None:
            return QRect()
        x0, y0 = self._band
        return QRect(QPoint(min(x0, self._x), min(y0, self._y)),
                     QPoint(max(x0, self._x), max(y0, self._y)))

    def _band_update(self) -> None:
        rect = self._band_rect_now()
        if self._band_rect is not None and rect == self._band_rect:
            return
        self._band_rect = rect
        self.band_moved.emit(rect)

    def _band_finish(self) -> None:
        rect = self._band_rect_now()
        self._band = None
        self._band_rect = None
        _log("band finish %s", rect)
        self.band_finished.emit(rect)
        self._hard_reset()

    def _finish(self) -> None:
        """鼠标松开。

        松在目标图标上 → 收纳；松在别处 → 把被拖的图标放到松手位置（我们自己
        接管的拖拽要负责收尾，不然图标会停在半路上）。
        """
        if self._band is not None:
            self._band_finish()
            return
        _log("finish: swallow=%s esc=%s target=%s up=%s",
             self._swallow_up, self._esc_sent, self._target, self._up_pt)
        if self._swallow_up:
            # up 被钩子吞了：explorer 还停在它自己的拖拽循环里，必须发 Esc 收掉，
            # 否则它一直以为鼠标还按着，后面每次拖拽都会错乱。
            self._swallow_up = False
            self._cancel_once()
            self._take_over()
            if self._target is None:
                # 松得太快，轮询还没来得及把「目标」记下来：按松手点现算一次
                tgt = self._target_at(self._x, self._y)
                if tgt is not None:
                    self._target = tgt
                    self._target_rect = self._idle_rects.get(tgt[0])
                    self._up_pt = (self._x, self._y)
        if not self._committed and self._esc_sent and self._target is not None \
                and self._up_on_target():
            _log("released on target -> merge")
            self._commit(self._target_rect)
            return

        if self._own_drag and self._source is not None:
            x, y = self._up_pt or (self._x, self._y)
            # 用户要求：松在哪儿就放哪儿；位置被别的图标占着就取最近的空格子，
            # 不能交给 explorer 自己把孩子塞到别处（那样每次落点都不一样）
            di.place_in_slot(self._source[0], x, y, offset=self._center_off or (0, 0))
            _log("aborted drag, icon placed at %s,%s", x, y)

        if self._down_desktop and self._source is None:
            # 在桌面空白处点了一下（没拖起来）：清掉收纳盒的多选
            self.band_finished.emit(QRect())

        self._hard_reset()
        self.clear.emit()
        self.cancelled.emit()

    def _abort(self) -> None:
        self._hard_reset()
        self.clear.emit()
        self.cancelled.emit()

    def _hard_reset(self) -> None:
        self._down = None
        self._dragging = False
        self._source = None
        self._committed = False
        self._esc_sent = False
        self._swallow_up = False
        self._own_drag = False
        self._src_home = None
        self._center_off = None
        self._rects = {}
        self._names = {}
        self._up_pt = None
        self._band = None
        self._band_rect = None
        self._reset_target()

    def _reset_target(self) -> None:
        if self._target is not None:
            self._target = None
            self.clear.emit()
        self._last_target = None
        self._target_rect = None
        self._hinted = False
        self._hint_rect = None

    def start(self) -> None:
        """（重新）装钩子并开始轮询。总开关关掉再打开时用得到。"""
        if not self._hook:
            hmod = kernel32.GetModuleHandleW(None)
            self._hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc, hmod, 0)
        if not self._timer.isActive():
            self._timer.setInterval(IDLE_INTERVAL)
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
