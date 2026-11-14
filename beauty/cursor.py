"""桌面上的自绘指针（那枚圆点）。

⚠️ 关键决定：**圆点不做成窗口，直接换掉系统光标本身**（`SetSystemCursor`）。

以前是另开一块半透明小窗跟着鼠标画（还带拖影）。它的位置要经过
「鼠标事件 → 重绘 → Qt 把整块表面交给 DWM → 合成上屏」才看得到，
天生比硬件光标慢 1~3 帧；180Hz 屏上快速移动时就是明显的「拖手 / 卡」，
而且那块窗每秒要整块上传给 DWM 上百次，把主线程和定时器一起拖累。
用户实测：换成系统箭头（不画圆点）就不卡——所以这里彻底改成系统光标：
零延迟、零合成开销。代价是没有拖影了（拖影只能靠自绘窗口，那正是卡的来源）。
"""
from __future__ import annotations

import atexit
import ctypes
import ctypes.wintypes as wt
import os
import time

from PySide6.QtCore import QObject, QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QRadialGradient
from PySide6.QtWidgets import QApplication

from .dlog import dlog

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

OCR_NORMAL = 32512
SPI_SETCURSORS = 0x0057
DESKTOP_READOBJECTS = 0x0001
UOI_NAME = 2

# 圆点 / 光晕（尺寸和 DSH 雾玻璃插件那枚指针对齐：dot 12 / halo 24 CSS px）
DOT = 12.0
HALO = 24.0
# 默认深一档：以前 (96,100,108) 那枚浅灰球，压在深色壁纸/深色面板上几乎看不见
CORE_RGB = (44, 48, 56)
GLOW = QColor(104, 110, 120, 100)
DESKTOP_CLASSES = ("Progman", "WorkerW", "SHELLDLL_DefView", "SysListView32")

user32.SystemParametersInfoW.restype = wt.BOOL
user32.SystemParametersInfoW.argtypes = [wt.UINT, wt.UINT, ctypes.c_void_p, wt.UINT]
user32.WindowFromPoint.restype = ctypes.c_void_p
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]
user32.GetCursorPos.restype = wt.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.LoadCursorW.restype = ctypes.c_void_p
user32.LoadCursorW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.CopyIcon.restype = ctypes.c_void_p
user32.CopyIcon.argtypes = [ctypes.c_void_p]
user32.SetSystemCursor.restype = wt.BOOL
user32.SetSystemCursor.argtypes = [ctypes.c_void_p, wt.DWORD]
user32.CreateIconIndirect.restype = ctypes.c_void_p
user32.OpenInputDesktop.restype = ctypes.c_void_p
user32.OpenInputDesktop.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
user32.CloseDesktop.restype = wt.BOOL
user32.CloseDesktop.argtypes = [ctypes.c_void_p]
user32.GetThreadDesktop.restype = ctypes.c_void_p
user32.GetThreadDesktop.argtypes = [wt.DWORD]
user32.GetUserObjectInformationW.restype = wt.BOOL
user32.GetUserObjectInformationW.argtypes = [
    ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wt.DWORD,
    ctypes.POINTER(wt.DWORD)]


class _ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wt.BOOL), ("xHotspot", wt.DWORD),
                ("yHotspot", wt.DWORD), ("hbmMask", wt.HBITMAP),
                ("hbmColor", wt.HBITMAP)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


gdi32.CreateDIBSection.restype = ctypes.c_void_p
gdi32.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.POINTER(_BITMAPINFO),
                                   wt.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   ctypes.c_void_p, wt.DWORD]
gdi32.CreateBitmap.restype = ctypes.c_void_p
gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wt.UINT, wt.UINT,
                               ctypes.c_void_p]
gdi32.DeleteObject.argtypes = [wt.HANDLE]


def _desktop_name(handle) -> str:
    if not handle:
        return ""
    buf = ctypes.create_unicode_buffer(128)
    need = wt.DWORD()
    ok = user32.GetUserObjectInformationW(
        ctypes.c_void_p(handle), UOI_NAME, buf, ctypes.sizeof(buf),
        ctypes.byref(need))
    return buf.value if ok else ""


_OWN_DESKTOP = _desktop_name(
    user32.GetThreadDesktop(kernel32.GetCurrentThreadId()))


def input_desktop_is_ours() -> bool:
    """鼠标现在所在的「输入桌面」还是不是我们这块。

    打不开也当不是自己的：正常桌面下这个调用有权限，打不开基本就说明
    已经切到安全桌面（UAC / 锁屏 / Ctrl+Alt+Del）了。
    """
    handle = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    if not handle:
        return False
    try:
        name = _desktop_name(handle)
    finally:
        user32.CloseDesktop(ctypes.c_void_p(handle))
    return not _OWN_DESKTOP or name == _OWN_DESKTOP


def _transparent_cursor() -> int:
    """32×32 全透明光标（旧方案，留着给「不想画圆点」时用）。"""
    rows = 32
    and_bits = (ctypes.c_ubyte * (rows * 4))(*([0xFF] * (rows * 4)))
    xor_bits = (ctypes.c_ubyte * (rows * 4))(*([0x00] * (rows * 4)))
    return int(user32.CreateCursor(None, 0, 0, 32, 32, and_bits, xor_bits) or 0)


def _hicon_from_image(img: QImage) -> int:
    """QImage（ARGB）→ HICON，热点放在正中间。"""
    img = img.convertToFormat(QImage.Format_ARGB32)
    w, h = img.width(), img.height()
    bmi = _BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h          # 自上而下
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0      # BI_RGB（带 alpha 的 32 位）
    buf = ctypes.c_void_p()
    hbm = gdi32.CreateDIBSection(None, ctypes.byref(bmi), 0,
                                 ctypes.byref(buf), None, 0)
    if not hbm:
        return 0
    ctypes.memmove(buf, bytes(img.constBits()), min(w * h * 4, img.sizeInBytes()))
    hbm_mask = gdi32.CreateBitmap(w, h, 1, 1, None)
    info = _ICONINFO()
    info.fIcon = True
    info.xHotspot = w // 2
    info.yHotspot = h // 2
    info.hbmMask = hbm_mask
    info.hbmColor = hbm
    hicon = int(user32.CreateIconIndirect(ctypes.byref(info)) or 0)
    for hnd in (hbm, hbm_mask):
        try:
            gdi32.DeleteObject(hnd)
        except (OSError, TypeError):
            pass
    return hicon


def _dot_cursor(color: QColor, op: float, dpr: float) -> int:
    """把圆点画成 32×32 的光标位图（硬件光标自己会画，不用我们每帧刷）。

    只画一枚干净的实心圆：以前那层径向光晕画进光标里，压在模糊/深色窗口上
    看着就是「一圈发虚的脏边」，太小一块地方也放不下柔光。
    """
    size = 32
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    try:
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        cx = cy = size / 2.0
        core_r = max(2.0, DOT / 2.0 * dpr)
        core = QColor(color)
        core.setAlpha(int(max(0.0, min(1.0, op)) * 245))
        p.setBrush(core)
        p.drawEllipse(QPointF(cx, cy), core_r, core_r)
    finally:
        p.end()
    return _hicon_from_image(img)


def install_dot_cursor(color: QColor, op: float, dpr: float) -> bool:
    """把 OCR_NORMAL 换成我们的圆点（系统接管句柄，不用我们释放）。"""
    h = _dot_cursor(color, op, dpr)
    if not h:
        return False
    if user32.SetSystemCursor(ctypes.c_void_p(h), OCR_NORMAL):
        return True
    try:
        user32.DestroyCursor(ctypes.c_void_p(h))
    except (OSError, TypeError):
        pass
    return False


def hide_system_cursor() -> None:
    h = _transparent_cursor()
    if h:
        user32.SetSystemCursor(h, OCR_NORMAL)


def restore_system_cursor() -> None:
    """把系统箭头恢复回来。

    两条路都走：先把标准箭头（句柄副本）设回 OCR_NORMAL，再从注册表整套
    重载一次。实测只靠 SystemParametersInfo 时偶尔「返回成功但箭头还是不显」。
    """
    try:
        arrow = user32.LoadCursorW(None, ctypes.c_void_p(32512))
        if arrow:
            copy = user32.CopyIcon(ctypes.c_void_p(arrow))
            if copy:
                user32.SetSystemCursor(ctypes.c_void_p(copy), OCR_NORMAL)
    except (OSError, TypeError):
        pass
    user32.SystemParametersInfoW(SPI_SETCURSORS, 0, None, 0)


atexit.register(restore_system_cursor)


class CursorOverlay(QObject):
    """管理「什么时候用圆点光标、什么时候还回系统箭头」。

    自己不画任何东西（这就是关键）：只按着鼠标底下的窗口切换系统光标。
    """

    def __init__(self, external: bool = False):
        super().__init__()
        self._pid = os.getpid()
        self._blocked: set[int] = set()
        # 只有登记过的窗口（卡片 / 收纳盒）才允许出现圆点。面板、对话框、取色盘
        # 一律不登记 —— 它们上面永远是系统箭头，这是用户要的「面板里用原生箭头」。
        self._registered: set[int] = set()
        self._suppressed = False
        self._on_desktop = False
        self._installed = False
        self._secure = False
        self._resume_at = 0.0
        self._sweep = 0
        self._base = QColor(*CORE_RGB)
        self._op = 1.0
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._tick)
        if not external:          # 交给 app 那条合成轮询统一驱动
            self._timer.start()

    def poll(self) -> None:
        """由外部的合成 tick 调用（见 app._poll_all）。"""
        self._tick()

    # ---------- 对外的几个口子（app.py 调这些） ----------
    def register(self, winid) -> None:
        """登记「圆点可以出现」的窗口（卡片、收纳盒）。

        以前这里什么都不做，判断只看进程号——结果面板和小弹窗也被换成圆点，
        深色面板上那枚灰球看不清（用户报的）。现在只认登记过的窗口。
        """
        try:
            self._registered.add(int(winid))
        except (TypeError, ValueError):
            pass

    def set_blocked(self, winid, on: bool) -> None:
        if on:
            self._blocked.add(int(winid))
        else:
            self._blocked.discard(int(winid))
        self._apply()

    def set_suppressed(self, on: bool) -> None:
        self._suppressed = bool(on)
        self._apply()

    def set_style(self, color=None, alpha_percent=None) -> None:
        if color is not None:
            try:
                c = QColor(color)
                if c.isValid():
                    self._base = c
            except (TypeError, ValueError):
                pass
        if alpha_percent is not None:
            try:
                self._op = max(0.0, min(1.0, float(alpha_percent) / 100.0))
            except (TypeError, ValueError):
                pass
        if self._installed:      # 换颜色/透明度：直接换一枚新光标
            self._installed = False
            self._apply()

    def bump(self, _gpos=None) -> None:
        """老接口：以前由鼠标事件推圆点一帧，现在由系统光标自己跟手。"""

    def stop(self) -> None:
        try:
            self._timer.stop()
        except RuntimeError:
            pass
        if self._installed:
            self._installed = False
            restore_system_cursor()

    # ---------- 内部 ----------
    def _apply(self) -> None:
        # ⚠️ 必须带上 _on_desktop：SetSystemCursor 换掉的是**整个会话**的箭头，
        # 漏了这个条件圆点就会出现在所有窗口上（用户实测：DSH 上也是圆球）。
        want = (self._on_desktop and not self._suppressed and not self._secure
                and time.monotonic() >= self._resume_at)
        if want:
            if not self._installed:
                dpr = 1.0
                try:
                    screens = QApplication.screens()
                    if screens:
                        dpr = float(screens[0].devicePixelRatio() or 1.0)
                except Exception:  # noqa: BLE001
                    dpr = 1.0
                self._installed = install_dot_cursor(self._base, self._op, dpr)
        elif self._installed:
            self._installed = False
            restore_system_cursor()

    def _tick(self) -> None:
        self._sweep += 1
        # 安全桌面（UAC / 锁屏）上绝对不能动光标：那上面是另一个桌面
        if self._secure or self._sweep % 8 == 0:
            if not input_desktop_is_ours():
                if not self._secure:
                    self._secure = True
                    self._resume_at = time.monotonic() + 0.3
                    dlog("cursor: secure desktop")
                    self._apply()
                return
            if self._secure:
                self._secure = False
                dlog("cursor: back from secure desktop")
        ph = wt.POINT()
        user32.GetCursorPos(ctypes.byref(ph))
        hwnd = user32.WindowFromPoint(ph)
        on = self._is_ours_or_desktop(int(hwnd or 0))
        if on != self._on_desktop:
            self._on_desktop = on
        self._apply()

    def _is_ours_or_desktop(self, hwnd: int) -> bool:
        if self._suppressed or not hwnd:
            return False
        if int(hwnd) in self._blocked:
            return False
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(ctypes.c_void_p(hwnd), buf, 64)
        if buf.value in DESKTOP_CLASSES:
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
        # 只有卡片 / 收纳盒这两个「桌面上的东西」才换圆点；面板、对话框都不换
        return pid.value == self._pid and int(hwnd) in self._registered
