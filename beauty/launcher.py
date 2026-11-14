"""任务栏上那颗 desktop beauty 按钮：单击开关美化，右键出毛玻璃菜单。

为什么是「自己画在任务栏上的一个窗口」，而不是真的固定按钮：

* Windows 只允许**用户手动拖动**排序 pinned 图标（顺序存在 explorer 的
  Taskband 二进制里），程序改不了。想让它稳稳卡在「第 2、3 个图标之间」，
  只能自己画；
* 单击如果要的是「开关」而不是「启动程序」，真按钮每次点击都得拉起一个小
  进程把命令转发给常驻进程（约 0.2s）；自绘是 0 延迟；
* 自绘的右键菜单可以直接是这套亚克力圆角菜单，不用去写 ICustomDestinationList。

代价：旁边图标增删时它不会自己跟着动 —— 所以有一个「重新对齐」的定时器，
每 8 秒重新量一次任务栏上图标的位置，位置变了就跟过去。
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes as wt
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QWidget

from . import screen
from .glass import apply_acrylic

user32 = ctypes.WinDLL("user32")
gdi32 = ctypes.WinDLL("gdi32")
dwmapi = ctypes.WinDLL("dwmapi")

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_NOOWNERZORDER = 0x0200
HWND_TOPMOST = -1
GWLP_HWNDPARENT = -8

DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2
PW_RENDERFULLCONTENT = 2

ASSETS = Path(__file__).resolve().parent.parent / "assets"

TRAY_CLASS = "Shell_TrayWnd"
ICON = 24              # 图标本体（逻辑像素），任务栏图标就是 24
WIN_W = 37             # 窗口宽（逻辑）—— 比图标宽一圈，留出悬停高亮
WIN_H = 48             # 窗口高（逻辑）= 任务栏高度
REGION_W = 27          # 真正吃鼠标的宽度（物理像素写在 _apply_region 里换算）
DEFAULT_CENTER = 1280  # 兜底：这台机器实测「任务视图 | 资源管理器」之间是中点 1280（物理）


# ---------------------------------------------------------------- 菜单
_MENU_QSS = """
QMenu {
    background: rgba(30, 33, 41, 196);
    border: 1px solid rgba(255, 255, 255, 34);
    padding: 5px;
    color: #F3F6FB;
    font-size: 12px;
}
QMenu::item { padding: 7px 34px 7px 14px; border-radius: 6px; margin: 1px 2px; }
QMenu::item:selected { background: rgba(255, 255, 255, 30); }
QMenu::item:disabled { color: rgba(255, 255, 255, 96); }
QMenu::separator { height: 1px; background: rgba(255, 255, 255, 26); margin: 5px 12px; }
QMenu::indicator { width: 14px; height: 14px; }
"""


class GlassMenu(QMenu):
    """Win11 那种：圆角 + 亚克力模糊 + 细描边。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet(_MENU_QSS)
        self._skinned = False

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._skinned:
            self._skinned = True
            self._skin()

    def _skin(self) -> None:
        try:
            hwnd = int(self.winId())
        except Exception:  # noqa: BLE001
            return
        apply_acrylic(hwnd, 0x30)
        pref = ctypes.c_int(DWMWCP_ROUND)
        try:
            dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(pref),
                ctypes.sizeof(pref))
        except OSError:
            pass


# ---------------------------------------------------------------- 量任务栏
class _BMIH(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class _BMI(ctypes.Structure):
    _fields_ = [("bmiHeader", _BMIH), ("bmiColors", wt.DWORD * 3)]


def tray_rect() -> tuple[int, int, int, int] | None:
    hwnd = user32.FindWindowW(TRAY_CLASS, None)
    if not hwnd:
        return None
    r = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return (r.left, r.top, r.right, r.bottom)


def _grab_window(hwnd: int, w: int, h: int) -> bytes | None:
    """PrintWindow 抓任务栏。GDI 的屏幕拷贝抓不到 XAML 画的图标。"""
    screen_dc = user32.GetDC(0)
    if not screen_dc:
        return None
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    bmp = gdi32.CreateCompatibleBitmap(screen_dc, w, h)
    old = gdi32.SelectObject(mem_dc, bmp)
    try:
        user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT)
        bi = _BMI()
        bi.bmiHeader.biSize = ctypes.sizeof(_BMIH)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h          # 负 = 从上到下
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(bi), 0)
        return buf.raw
    finally:
        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(0, screen_dc)


def measure_gap() -> int | None:
    """量出任务栏上第 2、3 个图标中间那条缝的 x（物理像素）。量不准就 None。"""
    rect = tray_rect()
    if rect is None:
        return None
    left, top, right, bottom = rect
    w, h = right - left, bottom - top
    if w < 200 or h < 24:
        return None
    hwnd = user32.FindWindowW(TRAY_CLASS, None)
    data = _grab_window(hwnd, w, h)
    if not data:
        return None

    y0, y1 = int(h * 0.18), int(h * 0.82)
    step = 2
    cols: list[int] = []
    for x in range(0, w, step):
        n = 0
        for y in range(y0, y1, step):
            i = (y * w + x) * 4
            b, g, r = data[i], data[i + 1], data[i + 2]
            mx, mn = max(r, g, b), min(r, g, b)
            if mx > 70 and (mx - mn > 25 or mn > 150):
                n += 1
        cols.append(n)

    runs: list[list[int]] = []
    cur: list[int] | None = None
    for idx, n in enumerate(cols):
        x = idx * step
        if n >= 2:
            if cur is None:
                cur = [x, x]
            else:
                cur[1] = x
        elif cur is not None:
            runs.append(cur)
            cur = None
    if cur is not None:
        runs.append(cur)

    # 任务栏图标宽 20~40（物理），太窄的是抗锯齿杂点（实测 13~15px）
    boxes = [(a, b) for a, b in runs if 17 <= b - a <= 42]
    if len(boxes) < 4:
        return None
    centers = [(a + b) / 2.0 for a, b in boxes]
    # 取最左边那一串间距一致（±12px）的
    group = [centers[0]]
    for c in centers[1:]:
        if 34 <= c - group[-1] <= 70:
            group.append(c)
        else:
            break
    if len(group) < 3:
        return None
    mid = (group[1] + group[2]) / 2.0
    if not (w * 0.2 < mid < w * 0.8):   # 量到托盘那一堆去了，不要
        return None
    return int(round(mid))


# ---------------------------------------------------------------- 按钮
class TaskbarButton(QWidget):
    def __init__(self, ctrl):
        flags = (Qt.FramelessWindowHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus
                 | Qt.WindowStaysOnTopHint)
        super().__init__(None, flags)
        self.ctrl = ctrl
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setToolTip("desktop beauty · 单击开关")
        self._hover = False
        self._pressed = False
        self._down_pos: QPoint | None = None
        self._last_click = 0.0
        self._glyph_pm: QPixmap | None = None
        self._glyph_for = 0
        self._center = int(getattr(ctrl.cfg, "taskbar_center", 0) or 0) or DEFAULT_CENTER
        self.menu = GlassMenu(self)
        self._sync_timer = QTimer(self)
        self._sync_timer.setInterval(8000)
        self._sync_timer.timeout.connect(self._resync)
        self._sync_timer.start()
        self._front_timer = QTimer(self)
        self._front_timer.setInterval(1000)
        self._front_timer.timeout.connect(self._taskbar_level)
        self._front_timer.start()

    # ---- 几何 ----
    def place(self, center: int | None = None) -> None:
        rect = tray_rect()
        if rect is None:
            return
        _, top, _, bottom = rect
        if center:
            self._center = int(center)
        win_w = int(round(WIN_W * screen.dpr()))
        win_h = int(round(WIN_H * screen.dpr()))
        left = int(round(self._center - win_w / 2.0))
        top_px = int(round((top + bottom) / 2.0 - win_h / 2.0))
        x, y, r, b = screen.to_logical((left, top_px, left + win_w, top_px + win_h))
        self.setGeometry(x, y, max(20, r - x), max(20, b - y))
        self._apply_region(win_w, win_h)

    def _apply_region(self, win_w: int, win_h: int) -> None:
        """只让图标本体那一小块吃鼠标，别抢邻居的点击。"""
        reg_w = int(round(REGION_W * screen.dpr()))
        reg_h = win_h - int(round(6 * screen.dpr()))
        rx = (win_w - reg_w) // 2
        ry = (win_h - reg_h) // 2
        try:
            rgn = gdi32.CreateRoundRectRgn(rx, ry, rx + reg_w + 1, ry + reg_h + 1, 14, 14)
            user32.SetWindowRgn(int(self.winId()), rgn, True)
        except OSError:
            pass

    def _resync(self) -> None:
        center = measure_gap()
        if center is None:
            self.place()
        else:
            old = self._center
            self.place(center)
            if abs(old - center) > 2:
                self.ctrl.cfg.taskbar_center = center
                self.ctrl.cfg.save()

    # ---- 层级 ----
    def showEvent(self, event) -> None:
        super().showEvent(event)
        center = measure_gap()
        self.place(center)
        self._taskbar_level()

    def event(self, ev):
        # 任务栏刷新时会把自己抬起来，顺手跟一下
        from PySide6.QtCore import QEvent
        if ev.type() == QEvent.Type.WindowActivate:
            QTimer.singleShot(0, self._taskbar_level)
        return super().event(ev)

    def _taskbar_level(self) -> None:
        """压在任务栏上面。

        任务栏自己是 topmost，而且它每次刷新都会把自己再抬一次，所以这里
        不是「设一次就完事」——下面有个 1 秒的定时器反复把它按回最顶层。
        """
        try:
            user32.SetWindowPos(
                int(self.winId()), HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        except OSError:
            pass

    # ---- 绘制 ----
    def _glyph(self) -> QPixmap | None:
        dpr = int(round(screen.dpr() * ICON))
        if self._glyph_pm is not None and self._glyph_for == dpr:
            return self._glyph_pm
        src = QPixmap(str(ASSETS / "logo.png"))
        if src.isNull():
            return None
        self._glyph_pm = src.scaled(dpr, dpr, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._glyph_for = dpr
        return self._glyph_pm

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        on = bool(self.ctrl.cfg.enabled)
        p.setOpacity(1.0 if on else 0.45)

        if self._hover:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 28 if on else 18))
            p.drawRoundedRect(self.rect().adjusted(2, 4, -2, -4), 7, 7)

        pm = self._glyph()
        if pm is not None:
            size = pm.deviceIndependentSize()
            gw, gh = int(size.width()), int(size.height())
            s = 0.92 if self._pressed else 1.0
            p.save()
            c = self.rect().center()
            p.translate(c)
            p.scale(s, s)
            p.translate(-c)
            p.drawPixmap(c.x() - gw // 2, c.y() - gh // 2, pm)
            p.restore()

    # ---- 鼠标 ----
    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._pressed = True
            self._down_pos = event.globalPosition().toPoint()
            self.update()
        elif event.button() == Qt.RightButton:
            self._show_menu(event.globalPosition().toPoint())
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._pressed:
            self._pressed = False
            self.update()
            pos = event.globalPosition().toPoint()
            moved = (pos - self._down_pos).manhattanLength() > 6 if self._down_pos else True
            self._down_pos = None
            inside = self.rect().contains(self.mapFromGlobal(pos))
            if inside and not moved:
                self._click()
        event.accept()

    def _click(self) -> None:
        now = time.monotonic()
        if now - self._last_click < 0.45:   # 双击只算一次，别开开关关
            self._last_click = now
            return
        self._last_click = now
        self.ctrl.toggle_beautify()

    # ---- 右键菜单 ----
    def _show_menu(self, pos: QPoint) -> None:
        menu = self.menu
        menu.clear()
        act_panel = menu.addAction("打开面板")
        act_panel.triggered.connect(self.ctrl.open_settings)
        menu.addSeparator()
        act_on = menu.addAction("启用桌面美化")
        act_on.setCheckable(True)
        act_on.setChecked(bool(self.ctrl.cfg.enabled))
        act_on.triggered.connect(self.ctrl.set_beautify_enabled)
        menu.addSeparator()
        menu.addAction("退出", self.ctrl.quit)
        self._taskbar_level()
        menu.exec(pos)
        self.update()

    def refresh(self) -> None:
        self.update()
