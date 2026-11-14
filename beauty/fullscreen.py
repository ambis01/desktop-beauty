"""判断「当前前台是不是全屏应用」。

只认**真正全屏**：前台窗口的矩形盖住整个显示器，而且不是最大化窗口
（最大化窗口带标题栏，任务栏也还在，不能算）。

坐标是物理像素（进程是 per-monitor v2 感知，GetWindowRect 和
GetMonitorInfo 都是物理像素，直接比）。
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

_u = ctypes.windll.user32
_u.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p

# 这些是桌面外壳自己的窗口，永远不当全屏应用
_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}
_WS_CAPTION = 0x00C00000


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


_dpi_fixed = False


def _thread_dpi_fix() -> None:
    """Qt 主线程之外调用时也要拿到物理坐标（只设一次就够）。"""
    global _dpi_fixed
    if _dpi_fixed:
        return
    _dpi_fixed = True
    try:
        _u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass


def foreground_is_fullscreen(own_pid: int | None = None) -> bool:
    """前台窗口是不是一个全屏应用（自己的窗口不算）。"""
    _thread_dpi_fix()
    hwnd = _u.GetForegroundWindow()
    if not hwnd or not _u.IsWindowVisible(hwnd):
        return False
    pid = wintypes.DWORD()
    _u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if own_pid is None:
        own_pid = os.getpid()
    if pid.value == own_pid:
        return False
    cls = ctypes.create_unicode_buffer(256)
    _u.GetClassNameW(hwnd, cls, 256)
    if cls.value in _SHELL_CLASSES:
        return False
    # 最大化窗口有标题栏；全屏的没有
    style = _u.GetWindowLongW(hwnd, -16)
    if style & _WS_CAPTION:
        return False
    rect = wintypes.RECT()
    if not _u.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    mon = _u.MonitorFromWindow(hwnd, 2)      # MONITOR_DEFAULTTONEAREST
    if not mon:
        return False
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    if not _u.GetMonitorInfoW(mon, ctypes.byref(info)):
        return False
    m = info.rcMonitor
    # 允许几个像素的误差（有些全屏窗口会比显示器大/小一两个像素）
    slack = 2
    return (rect.left <= m.left + slack and rect.top <= m.top + slack
            and rect.right >= m.right - slack and rect.bottom >= m.bottom - slack)
