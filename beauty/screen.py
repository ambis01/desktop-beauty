r"""物理像素 ↔ Qt 逻辑像素的换算（按显示器算）。

Win32 那一套（桌面图标 LVM_*、鼠标钩子、GetWindowRect）给的都是**物理像素**，
Qt 窗口的坐标是**逻辑像素**。单屏时两者只差一个 dpr；多屏（尤其缩放不一样、
或者副屏摆在主屏左边/上边）时，每块屏的 dpr 和原点都不同，拿主屏一个除法/乘法
去算，副屏上的图标和框选会偏一大截。

所以这里按显示器做仿射：
  - 用 `EnumDisplayMonitors` 拿每块屏的**物理矩形**和设备名（`\\.\DISPLAY1`）；
  - 用 `QScreen.name()` 跟它配对（Qt 在 Windows 上用的就是同一个设备名）；
  - 物理 → 逻辑：`logical = geo.x + (phys - mon.x) / dpr`
  - 逻辑 → 物理：`phys = mon.x + (logical - geo.x) * dpr`
单屏、且主屏在 (0,0) 时，这跟以前的 `v * dpr` 完全等价。

对外只有 `to_logical` / `to_physical` / `dpr` 三个函数，调用方不用改：传进来的
矩形是物理的就按物理找屏、逻辑的就按逻辑找屏。
"""
from __future__ import annotations

import ctypes
import time
import ctypes.wintypes as wt

from PySide6.QtCore import QRect
from PySide6.QtGui import QGuiApplication

try:                                    # 只有 Windows 有这几支
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.EnumDisplayMonitors.restype = wt.BOOL
    _user32.GetMonitorInfoW.restype = wt.BOOL
except Exception:                       # noqa: BLE001 - 非 Windows / 抽风都不影响主流程
    _user32 = None

_MONITORINFOF_PRIMARY = 0x1
_CACHE_TTL = 1.0                        # 显示器布局很少变，1 秒足够，还能跟上插拔
_cache: tuple[float, list] = (0.0, [])

MONITORINFOEXW = None
if _user32 is not None:
    class MONITORINFOEXW(ctypes.Structure):     # noqa: D101 - Win32 结构
        _fields_ = [
            ("cbSize", wt.DWORD),
            ("rcMonitor", wt.RECT),
            ("rcWork", wt.RECT),
            ("dwFlags", wt.DWORD),
            ("szDevice", ctypes.c_wchar * 32),
        ]

    _MRECT = wt.RECT


def _enum_monitors() -> dict[str, QRect]:
    """设备名 → 物理矩形（整个显示器，不含任务栏）。"""
    out: dict[str, QRect] = {}
    if _user32 is None or MONITORINFOEXW is None:
        return out

    def _cb(hmon, _hdc, _rect, _data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if _user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            out[str(info.szDevice)] = QRect(r.left, r.top,
                                            r.right - r.left, r.bottom - r.top)
        return True

    try:
        _user32.EnumDisplayMonitors(
            None, None,
            ctypes.WINFUNCTYPE(wt.BOOL, ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.POINTER(wt.RECT), ctypes.c_void_p)(_cb), 0)
    except Exception:                   # noqa: BLE001
        return {}
    return out


def _pairs() -> list[tuple[QRect, float, QRect]]:
    """[(Qt 逻辑矩形, dpr, 显示器物理矩形)]，每块屏一项。

    配对不上（非 Windows、设备名对不上）就退回老办法：主屏 + 主屏 dpr + 原点 0。
    """
    global _cache
    now = time.monotonic()
    ts, cached = _cache
    if cached and now - ts < _CACHE_TTL:
        return cached
    app = QGuiApplication.instance()
    screens = list(app.screens()) if app is not None else []
    if not screens:
        _cache = (now, [])
        return []
    mons = _enum_monitors()
    out: list[tuple[QRect, float, QRect]] = []
    for scr in screens:
        try:
            geo = QRect(scr.geometry())
            dpr = float(scr.devicePixelRatio() or 1.0)
            name = str(scr.name() or "")
        except (RuntimeError, TypeError):
            continue
        mon = mons.get(name)
        if mon is None and len(screens) == 1:
            # 单屏且设备名对不上：按「物理原点 = 逻辑原点 × dpr」硬套（老行为）
            mon = QRect(int(round(geo.x() * dpr)), int(round(geo.y() * dpr)),
                        int(round(geo.width() * dpr)), int(round(geo.height() * dpr)))
        out.append((geo, dpr if dpr > 0 else 1.0, mon or QRect(0, 0, 0, 0)))
    _cache = (now, out)
    return out


def _pick(pairs, x: int, y: int):
    """点落在哪块屏上（不 trim 到矩形里，只为了拿 dpr / 原点）。"""
    for item in pairs:
        geo = item[0]
        if geo.left() <= x <= geo.right() and geo.top() <= y <= geo.bottom():
            return item
    best = None
    best_d = None
    for item in pairs:
        geo = item[0]
        dx = max(geo.left() - x, 0, x - geo.right())
        dy = max(geo.top() - y, 0, y - geo.bottom())
        d = dx * dx + dy * dy
        if best is None or d < best_d:
            best, best_d = item, d
    return best


def _map_physical_to_logical(pairs, rect) -> tuple[int, int, int, int]:
    l, t, r, b = rect
    item = _pick(pairs, int((l + r) / 2), int((t + b) / 2))
    if item is None:
        return tuple(int(round(v)) for v in rect)  # type: ignore[return-value]
    geo, dpr, mon = item
    dx = geo.x() - mon.x() / dpr
    dy = geo.y() - mon.y() / dpr
    return tuple(int(round(v / dpr + (dx if i % 2 == 0 else dy)))
                 for i, v in enumerate(rect))  # type: ignore[return-value]


def _map_logical_to_physical(pairs, rect) -> tuple[int, int, int, int]:
    l, t, r, b = rect
    item = _pick(pairs, int((l + r) / 2), int((t + b) / 2))
    if item is None:
        return tuple(int(round(v)) for v in rect)  # type: ignore[return-value]
    geo, dpr, mon = item
    ox = mon.x() - geo.x() * dpr
    oy = mon.y() - geo.y() * dpr
    return tuple(int(round(v * dpr + (ox if i % 2 == 0 else oy)))
                 for i, v in enumerate(rect))  # type: ignore[return-value]


def dpr() -> float:
    scr = QGuiApplication.primaryScreen()
    if scr is None:
        return 1.0
    try:
        value = float(scr.devicePixelRatio())
    except Exception:  # noqa: BLE001
        return 1.0
    return value if value > 0 else 1.0


def to_logical(rect) -> tuple[int, int, int, int]:
    """Win32 屏幕矩形（物理）→ Qt 矩形（逻辑）。"""
    return _map_physical_to_logical(_pairs(), rect)


def to_physical(rect) -> tuple[int, int, int, int]:
    """Qt 矩形（逻辑）→ Win32 屏幕矩形（物理）。"""
    return _map_logical_to_physical(_pairs(), rect)
