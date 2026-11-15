"""Windows 桌面图标底层：枚举、命中测试、图标矩形、显示名→真实文件。

桌面图标是 explorer.exe 里 Progman/WorkerW → SHELLDLL_DefView → SysListView32
这个 ListView。跨进程读名字/坐标要往 explorer 里申请共享内存，让 explorer
把数据填进来。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32")

LVM_FIRST = 0x1000
LVM_GETITEMCOUNT = LVM_FIRST + 4
LVM_GETNEXTITEM = LVM_FIRST + 12
LVM_GETITEMRECT = LVM_FIRST + 14
LVM_SETITEMPOSITION = LVM_FIRST + 15
LVM_GETITEMPOSITION = LVM_FIRST + 16
LVM_HITTEST = LVM_FIRST + 18
LVM_GETITEMSPACING = LVM_FIRST + 51
LVM_GETITEMTEXTW = LVM_FIRST + 115

LVNI_SELECTED = 0x0002

LVIF_TEXT = 0x0001

PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04
SMTO_BLOCK = 0x0001
SMTO_ABORTIFHUNG = 0x0002

user32.FindWindowW.restype = wt.HWND
user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
user32.FindWindowExW.restype = wt.HWND
user32.FindWindowExW.argtypes = [wt.HWND, wt.HWND, wt.LPCWSTR, wt.LPCWSTR]
user32.SendMessageTimeoutW.restype = wt.LPARAM
user32.SendMessageTimeoutW.argtypes = [
    wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM, wt.UINT, wt.UINT,
    ctypes.POINTER(ctypes.c_size_t),
]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.ScreenToClient.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]

kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.VirtualAllocEx.restype = ctypes.c_void_p
kernel32.VirtualAllocEx.argtypes = [
    wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, wt.DWORD,
]
kernel32.VirtualFreeEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD]
kernel32.WriteProcessMemory.argtypes = [
    wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
kernel32.ReadProcessMemory.argtypes = [
    wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
kernel32.CloseHandle.argtypes = [wt.HANDLE]


class DesktopIconsError(RuntimeError):
    pass


class LVITEMW(ctypes.Structure):
    """commctrl.h 的 LVITEMW（x64 下 88 字节）。"""
    _fields_ = [
        ("mask", wt.UINT),
        ("iItem", ctypes.c_int),
        ("iSubItem", ctypes.c_int),
        ("state", wt.UINT),
        ("stateMask", wt.UINT),
        ("pszText", ctypes.c_void_p),
        ("cchTextMax", ctypes.c_int),
        ("iImage", ctypes.c_int),
        ("lParam", wt.LPARAM),
        ("iIndent", ctypes.c_int),
        ("iGroupId", ctypes.c_int),
        ("cColumns", wt.UINT),
        ("puColumns", ctypes.c_void_p),
        ("piColFmt", ctypes.c_void_p),
        ("iGroup", ctypes.c_int),
    ]


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


@dataclass
class IconInfo:
    index: int
    name: str
    x: int
    y: int


# ---------------------------------------------------------------- 基础查找

def _send(hwnd: int, msg: int, wparam: int = 0, lparam: int = 0,
          timeout: int = 1500) -> tuple[bool, int]:
    out = ctypes.c_size_t()
    ok = user32.SendMessageTimeoutW(hwnd, msg, wparam, lparam,
                                    SMTO_BLOCK | SMTO_ABORTIFHUNG, timeout,
                                    ctypes.byref(out))
    return bool(ok), out.value


def _find_defview() -> int:
    progman = user32.FindWindowW("Progman", None)
    if progman:
        dv = user32.FindWindowExW(progman, None, "SHELLDLL_DefView", None)
        if dv:
            return dv
    hwnd = None
    while True:  # 壁纸轮播时 DefView 会挂到某个 WorkerW 下
        hwnd = user32.FindWindowExW(None, hwnd, "WorkerW", None)
        if not hwnd:
            return 0
        dv = user32.FindWindowExW(hwnd, None, "SHELLDLL_DefView", None)
        if dv:
            return dv


def _find_listview() -> int:
    dv = _find_defview()
    if not dv:
        return 0
    return user32.FindWindowExW(dv, None, "SysListView32", None)


class _Remote:
    """在 explorer 进程里申请一块可读写内存。"""

    def __init__(self, pid: int, size: int = 4096):
        self.h = kernel32.OpenProcess(
            PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE
            | PROCESS_QUERY_INFORMATION, False, pid)
        if not self.h:
            raise DesktopIconsError(f"OpenProcess 失败（{ctypes.get_last_error()}）")
        self.addr = kernel32.VirtualAllocEx(self.h, None, size,
                                            MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
        if not self.addr:
            kernel32.CloseHandle(self.h)
            raise DesktopIconsError("VirtualAllocEx 失败")

    def write(self, obj, offset: int = 0) -> None:
        n = ctypes.c_size_t()
        if not kernel32.WriteProcessMemory(
                self.h, ctypes.c_void_p(self.addr + offset), ctypes.byref(obj),
                ctypes.sizeof(obj), ctypes.byref(n)):
            raise DesktopIconsError("WriteProcessMemory 失败")

    def read(self, obj, offset: int = 0) -> None:
        n = ctypes.c_size_t()
        if not kernel32.ReadProcessMemory(
                self.h, ctypes.c_void_p(self.addr + offset), ctypes.byref(obj),
                ctypes.sizeof(obj), ctypes.byref(n)):
            raise DesktopIconsError("ReadProcessMemory 失败")

    def close(self) -> None:
        if self.h:
            kernel32.VirtualFreeEx(self.h, ctypes.c_void_p(self.addr), 0, MEM_RELEASE)
            kernel32.CloseHandle(self.h)
            self.h = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _remote_for_listview(lv: int) -> _Remote:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(lv, ctypes.byref(pid))
    return _Remote(pid.value)


# ---------------------------------------------------------------- 读取

def available() -> bool:
    lv = _find_listview()
    return bool(lv)


def icon_count() -> int:
    lv = _find_listview()
    if not lv:
        raise DesktopIconsError("找不到桌面图标列表")
    ok, count = _send(lv, LVM_GETITEMCOUNT)
    if not ok:
        raise DesktopIconsError("读取图标数量超时")
    return count


def list_icons() -> list[IconInfo]:
    lv = _find_listview()
    if not lv:
        raise DesktopIconsError("找不到桌面图标列表（explorer 没运行？）")
    ok, count = _send(lv, LVM_GETITEMCOUNT)
    if not ok:
        raise DesktopIconsError("读取图标数量超时")

    TEXT_OFF, POINT_OFF = 1024, 2048
    icons: list[IconInfo] = []
    with _remote_for_listview(lv) as mem:
        item = LVITEMW()
        text = (ctypes.c_wchar * 260)()
        pt = wt.POINT()
        for i in range(count):
            ctypes.memset(ctypes.byref(item), 0, ctypes.sizeof(item))
            item.mask = LVIF_TEXT
            item.iItem = i
            item.pszText = ctypes.c_void_p(mem.addr + TEXT_OFF)
            item.cchTextMax = 260
            mem.write(item, 0)
            ok, _ = _send(lv, LVM_GETITEMTEXTW, i, mem.addr)
            if not ok:
                continue
            mem.read(text, TEXT_OFF)
            name = text.value

            ctypes.memset(ctypes.byref(pt), 0, ctypes.sizeof(pt))
            mem.write(pt, POINT_OFF)
            ok, _ = _send(lv, LVM_GETITEMPOSITION, i, mem.addr + POINT_OFF)
            if not ok:
                continue
            mem.read(pt, POINT_OFF)
            icons.append(IconInfo(index=i, name=name, x=pt.x, y=pt.y))
    return icons


def icon_name(index: int) -> str:
    lv = _find_listview()
    if not lv:
        return ""
    with _remote_for_listview(lv) as mem:
        item = LVITEMW()
        text = (ctypes.c_wchar * 260)()
        ctypes.memset(ctypes.byref(item), 0, ctypes.sizeof(item))
        item.mask = LVIF_TEXT
        item.iItem = index
        item.pszText = ctypes.c_void_p(mem.addr + 1024)
        item.cchTextMax = 260
        mem.write(item, 0)
        ok, _ = _send(lv, LVM_GETITEMTEXTW, index, mem.addr)
        if not ok:
            return ""
        mem.read(text, 1024)
        return text.value


def selected_indexes() -> list[int]:
    """桌面视图上现在选中的图标（explorer 自己的拉框 / Ctrl 点选都算）。"""
    lv = _find_listview()
    if not lv:
        return []
    out: list[int] = []
    idx = -1
    for _ in range(4000):
        ok, res = _send(lv, LVM_GETNEXTITEM, idx, LVNI_SELECTED, timeout=800)
        if not ok:
            break
        idx = int(res)
        if idx < 0 or idx >= 0x7FFFFFFF:
            break
        out.append(idx)
    return out


def hit_test(x: int, y: int) -> int:
    """屏幕坐标 → 桌面图标索引，空白处返回 -1。"""
    lv = _find_listview()
    if not lv:
        return -1
    with _remote_for_listview(lv) as mem:
        pt = wt.POINT(x, y)
        user32.ScreenToClient(lv, ctypes.byref(pt))
        mem.write(pt, 0)
        ok, res = _send(lv, LVM_HITTEST, 0, mem.addr, timeout=800)
        if not ok:
            return -1
        return -1 if res >= 0x7FFFFFFF else res


def item_rect(index: int) -> tuple[int, int, int, int] | None:
    """图标在屏幕上的外接矩形（left, top, right, bottom）。"""
    lv = _find_listview()
    if not lv:
        return None
    with _remote_for_listview(lv) as mem:
        r = _RECT()
        mem.write(r, 0)
        ok, res = _send(lv, LVM_GETITEMRECT, index, mem.addr, timeout=800)
        if not ok or not res:
            return None
        mem.read(r, 0)
        p1 = wt.POINT(r.left, r.top)
        p2 = wt.POINT(r.right, r.bottom)
        user32.ClientToScreen(lv, ctypes.byref(p1))
        user32.ClientToScreen(lv, ctypes.byref(p2))
        return (p1.x, p1.y, p2.x, p2.y)


# ---------------------------------------------------------------- 挪开图标
#
# 收纳盒占了桌面一块地方后，压在这块地方下面的图标会被「挤」到最近的空位。
# 坐标有两套，别搞混：
#   * LVM_* 消息里的坐标都是 ListView 的**客户区**坐标；
#   * 我们窗口是屏幕坐标，进出这个模块时各转一次。

LVS_AUTOARRANGE = 0x0100
GWL_STYLE = -16

user32.GetWindowLongW.restype = ctypes.c_long
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(_RECT)]
user32.InvalidateRect.argtypes = [wt.HWND, ctypes.c_void_p, wt.BOOL]
user32.SystemParametersInfoW.argtypes = [wt.UINT, wt.UINT, ctypes.c_void_p, wt.UINT]


def auto_arrange() -> bool:
    """桌面开了「自动排列图标」的话，位置由 explorer 说了算，挪了也会弹回去。"""
    lv = _find_listview()
    if not lv:
        return True
    return bool(user32.GetWindowLongW(lv, GWL_STYLE) & LVS_AUTOARRANGE)


def item_spacing() -> tuple[int, int]:
    """桌面图标的格子大小（大图标视图）。wParam 必须是 0。"""
    lv = _find_listview()
    if not lv:
        return (0, 0)
    ok, res = _send(lv, LVM_GETITEMSPACING, 0)
    if not ok:
        return (0, 0)
    return (res & 0xFFFF, (res >> 16) & 0xFFFF)


def list_positions() -> list[tuple[int, int, int]]:
    """只要位置不要名字，比 list_icons 少一半跨进程消息。"""
    lv = _find_listview()
    if not lv:
        raise DesktopIconsError("找不到桌面图标列表")
    ok, count = _send(lv, LVM_GETITEMCOUNT)
    if not ok:
        raise DesktopIconsError("读取图标数量超时")

    out: list[tuple[int, int, int]] = []
    with _remote_for_listview(lv) as mem:
        pt = wt.POINT()
        for i in range(count):
            ctypes.memset(ctypes.byref(pt), 0, ctypes.sizeof(pt))
            mem.write(pt, 0)
            ok, _ = _send(lv, LVM_GETITEMPOSITION, i, mem.addr)
            if not ok:
                continue
            mem.read(pt, 0)
            out.append((i, pt.x, pt.y))
    return out


def list_rects() -> dict[int, tuple[int, int, int, int]]:
    """每个图标「看得见的那块」在屏幕上的矩形（含图标+文字）。

    注意 LVM_GETITEMRECT 的坐标系和 LVM_GETITEMPOSITION 差一点点（这台机器上
    横向差 16px、纵向差 3px），判断「盒子有没有压住图标」得用看得见的矩形，
    判断「挪到哪个格子」才用 position。
    """
    lv = _find_listview()
    if not lv:
        raise DesktopIconsError("找不到桌面图标列表")
    ok, count = _send(lv, LVM_GETITEMCOUNT)
    if not ok:
        raise DesktopIconsError("读取图标数量超时")

    out: dict[int, tuple[int, int, int, int]] = {}
    with _remote_for_listview(lv) as mem:
        r = _RECT()
        for i in range(count):
            # left=LVIR_BOUNDS(0)，其余清零
            ctypes.memset(ctypes.byref(r), 0, ctypes.sizeof(r))
            mem.write(r, 0)
            ok, res = _send(lv, LVM_GETITEMRECT, i, mem.addr, timeout=800)
            if not ok or not res:
                continue
            mem.read(r, 0)
            p1 = wt.POINT(r.left, r.top)
            p2 = wt.POINT(r.right, r.bottom)
            user32.ClientToScreen(lv, ctypes.byref(p1))
            user32.ClientToScreen(lv, ctypes.byref(p2))
            out[i] = (p1.x, p1.y, p2.x, p2.y)
    return out


def set_item_position(index: int, x: int, y: int) -> bool:
    """把图标挪到客户区坐标 (x, y)。LVM_SETITEMPOSITION 的坐标打包在 lParam 里。"""
    lv = _find_listview()
    if not lv:
        return False
    lparam = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
    ok, _ = _send(lv, LVM_SETITEMPOSITION, index, lparam, timeout=800)
    return ok


def _overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _overlap_area(a, b) -> int:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(0, w) * max(0, h)


def plan_moves(positions: dict[int, tuple[int, int]],
               avoid: tuple[int, int, int, int],
               cell: tuple[int, int],
               bounds: tuple[int, int, int, int],
               threshold: float = 0.2,
               cover: dict[int, tuple[int, int, int, int]] | None = None
               ) -> dict[int, tuple[int, int]]:
    """纯几何：算出哪些图标要挪、挪到哪。

    positions: {图标索引: (x, y)}（客户区坐标）
    avoid:     要空出来的矩形（客户区坐标，一般是收纳盒的范围）
    cell:      桌面格子大小（LVM_GETITEMSPACING）
    bounds:    可用范围（客户区）
    threshold: 图标被压住多少面积才算「重叠」。用看得见的矩形算，盒子对在
               格子上时只擦到邻居几个像素，那种不算重叠。
    cover:     每个图标看得见的矩形（客户区坐标）；不给就用格子代替
    """
    cx, cy = cell
    if cx <= 0 or cy <= 0 or not positions:
        return {}
    area_limit = threshold * cx * cy
    cells_of = {i: (x, y, x + cx, y + cy) for i, (x, y) in positions.items()}

    def covered(idx: int) -> bool:
        r = (cover or {}).get(idx) or cells_of[idx]
        area = max(1, (r[2] - r[0]) * (r[3] - r[1]))
        return _overlap_area(r, avoid) >= threshold * area

    moving = [i for i in cells_of if covered(i)]
    if not moving:
        return {}
    # 压得最狠的先安排，它最需要找位置
    def _hit(idx: int) -> int:
        r = (cover or {}).get(idx) or cells_of[idx]
        return _overlap_area(r, avoid)

    moving.sort(key=lambda i: (-_hit(i), i))

    base_x = min(x for x, _ in positions.values())
    base_y = min(y for _, y in positions.values())
    cols = max(1, (bounds[2] - base_x) // cx)
    rows = max(1, (bounds[3] - base_y) // cy)
    cells = [(base_x + gx * cx, base_y + gy * cy)
             for gx in range(cols + 1) for gy in range(rows + 1)]
    # 最下面/最右边允许贴着边，但不许越界
    cells = [c for c in cells
             if c[0] + cx <= bounds[2] and c[1] + cy <= bounds[3]]

    taken = dict(cells_of)
    result: dict[int, tuple[int, int]] = {}
    for idx in moving:
        x0, y0 = positions[idx]
        order = sorted(cells, key=lambda p: ((p[0] - x0) ** 2 + (p[1] - y0) ** 2, p[1], p[0]))
        for px, py in order:
            cand = (px, py, px + cx, py + cy)
            if _overlap_area(cand, avoid) >= area_limit:
                continue
            if any(_overlap(cand, r) for i, r in taken.items() if i != idx):
                continue
            result[idx] = (px, py)
            taken[idx] = cand
            break
    return result


def item_position(index: int) -> tuple[int, int] | None:
    """单个图标的位置（客户区坐标）。"""
    lv = _find_listview()
    if not lv:
        return None
    with _remote_for_listview(lv) as mem:
        pt = wt.POINT()
        mem.write(pt, 0)
        ok, _ = _send(lv, LVM_GETITEMPOSITION, index, mem.addr, timeout=800)
        if not ok:
            return None
        mem.read(pt, 0)
        return (pt.x, pt.y)


def client_rect_to_screen(rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    lv = _find_listview()
    if not lv:
        return rect
    p1 = wt.POINT(int(rect[0]), int(rect[1]))
    p2 = wt.POINT(int(rect[2]), int(rect[3]))
    user32.ClientToScreen(lv, ctypes.byref(p1))
    user32.ClientToScreen(lv, ctypes.byref(p2))
    return (p1.x, p1.y, p2.x, p2.y)


def client_bounds() -> tuple[int, int, int, int]:
    """桌面图标区的客户区范围，用来防止把图标挤出屏幕。"""
    lv = _find_listview()
    if not lv:
        return (0, 0, 0, 0)
    cr = _RECT()
    if not user32.GetClientRect(lv, ctypes.byref(cr)):
        return (0, 0, 0, 0)
    return (cr.left, cr.top, cr.right, cr.bottom)


SPI_GETWORKAREA = 0x0030


def work_area_client() -> tuple[int, int, int, int]:
    """屏幕可用区（扣掉任务栏）换算成 ListView 客户区坐标。

    桌面 ListView 的客户区是整个屏幕，连任务栏底下那一块都算；图标被挪到
    任务栏下面就等于没了，所以有空位也只往可用区里放。
    """
    r = _RECT()
    if not user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0):
        return client_bounds()
    lv = _find_listview()
    if not lv:
        return (r.left, r.top, r.right, r.bottom)
    p1 = wt.POINT(r.left, r.top)
    p2 = wt.POINT(r.right, r.bottom)
    user32.ScreenToClient(lv, ctypes.byref(p1))
    user32.ScreenToClient(lv, ctypes.byref(p2))
    return (p1.x, p1.y, p2.x, p2.y)


def make_room(rect: tuple[int, int, int, int]) -> int:
    """把压在 rect（屏幕坐标）下面的桌面图标挤到空位，返回挪开的个数。"""
    lv = _find_listview()
    if not lv or auto_arrange():
        return 0
    cell = item_spacing()
    if cell[0] <= 0 or cell[1] <= 0:
        return 0
    bounds = work_area_client()
    if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
        return 0
    p1 = wt.POINT(int(rect[0]), int(rect[1]))
    p2 = wt.POINT(int(rect[2]), int(rect[3]))
    user32.ScreenToClient(lv, ctypes.byref(p1))
    user32.ScreenToClient(lv, ctypes.byref(p2))
    avoid = (p1.x, p1.y, p2.x, p2.y)

    positions = {i: (x, y) for i, x, y in list_positions()}
    # 判断「压住谁」用看得见的矩形，比格子准
    cover: dict[int, tuple[int, int, int, int]] = {}
    try:
        for idx, r in list_rects().items():
            q1 = wt.POINT(r[0], r[1])
            q2 = wt.POINT(r[2], r[3])
            user32.ScreenToClient(lv, ctypes.byref(q1))
            user32.ScreenToClient(lv, ctypes.byref(q2))
            cover[idx] = (q1.x, q1.y, q2.x, q2.y)
    except DesktopIconsError:
        cover = {}
    moves = plan_moves(positions, avoid, cell, bounds, cover=cover)
    moved = 0
    for idx, (x, y) in moves.items():
        # 图标对应的文件可能已经没了（刚被收进盒子）。这种千万别挪：挪一下
        # explorer 会拖着不删，图标在桌面上残留一两秒——这就是「建盒之后图标
        # 还留在原地」的元凶。
        try:
            name = icon_name(idx)
            path = resolve_item(name) if name else None
        except DesktopIconsError:
            path = None
        if path is None or not Path(path).exists():
            continue
        if set_item_position(idx, x, y):
            moved += 1
    if moved:
        user32.InvalidateRect(lv, None, True)
    return moved


def icon_center_offset(index: int) -> tuple[int, int]:
    """图标可见区域中心相对 LVM_GETITEMPOSITION 的偏移（这台机器约 (30, 46)）。

    跟着鼠标拖图标时要用它把「看得见的那块」对准光标，而不是让 position 对准。
    """
    r = item_rect(index)
    p = item_position(index)
    if not r or not p:
        return (0, 0)
    sx, sy, _, _ = client_rect_to_screen((p[0], p[1], p[0], p[1]))
    return ((r[0] + r[2]) // 2 - sx, (r[1] + r[3]) // 2 - sy)


_OFFSET_CACHE: tuple[int, int] | None = None


def cell_center_offset() -> tuple[int, int]:
    """拿一个「已经稳稳待在桌面上」的图标算偏移，缓存起来给落位用。

    刚被 explorer 建出来的图标 LVM_GETITEMRECT 可能还没算好，那时候
    icon_center_offset() 会返回 (0,0)，落位就偏 30~46 像素。
    """
    global _OFFSET_CACHE
    if _OFFSET_CACHE is not None:
        return _OFFSET_CACHE
    try:
        for i, _x, _y in list_positions():
            off = icon_center_offset(i)
            if off != (0, 0):
                _OFFSET_CACHE = off
                return off
    except DesktopIconsError:
        pass
    return (0, 0)


def follow_item(index: int, screen_x: int, screen_y: int,
                offset: tuple[int, int] = (0, 0)) -> bool:
    """把图标挪到屏幕点（图标中心跟着光标）。用于我们自己接管的拖拽。"""
    lv = _find_listview()
    if not lv:
        return False
    p = wt.POINT(int(screen_x), int(screen_y))
    user32.ScreenToClient(lv, ctypes.byref(p))
    return set_item_position(index, p.x - offset[0], p.y - offset[1])


def place_item(index: int, screen_x: int, screen_y: int,
               offset: tuple[int, int] = (0, 0)) -> bool:
    """松手：图标就放在松手的位置，**不**自动找最近的格子。

    用户明确要求「拖到哪儿就放哪儿」：自动吸附会把桌面布局搞得乱七八糟。
    """
    return follow_item(index, screen_x, screen_y, offset)


def nearest_free_slot(screen_x: int, screen_y: int, skip: int | None = None,
                      offset: tuple[int, int] = (0, 0)) -> tuple[int, int] | None:
    """屏幕点 → 最近的**空格子**左上角（客户区坐标）。

    桌面图标都被 explorer 吸在格子上。要是把图标放到别人身上，它会自己挑一个
    空位塞进去（用户看到的就是「又被自动塞到别处」）。这里由我们自己按距离挑
    最近的空格子，结果可预期。skip 是正在落位的那个图标自己的索引。
    """
    lv = _find_listview()
    if not lv:
        return None
    cx, cy = item_spacing()
    if cx <= 0 or cy <= 0:
        return None
    p = wt.POINT(int(screen_x), int(screen_y))
    user32.ScreenToClient(lv, ctypes.byref(p))
    want_x, want_y = p.x - offset[0], p.y - offset[1]

    try:
        positions = {i: (x, y) for i, x, y in list_positions()}
    except DesktopIconsError:
        return (want_x, want_y)
    if not positions:
        return (want_x, want_y)

    base_x = min(x for x, _y in positions.values())
    base_y = min(y for _x, y in positions.values())
    taken = {(round((x - base_x) / cx), round((y - base_y) / cy))
             for idx, (x, y) in positions.items() if idx != skip}
    k0 = round((want_x - base_x) / cx)
    m0 = round((want_y - base_y) / cy)
    bounds = work_area_client()
    has_bounds = bounds[2] > bounds[0] and bounds[3] > bounds[1]

    best: tuple[int, int, float] | None = None
    for ring in range(0, 80):
        for dk in range(-ring, ring + 1):
            for dm in range(-ring, ring + 1):
                if max(abs(dk), abs(dm)) != ring:
                    continue
                k, m = k0 + dk, m0 + dm
                if (k, m) in taken:
                    continue
                x, y = base_x + k * cx, base_y + m * cy
                if has_bounds and (x < bounds[0] or y < bounds[1]
                                   or x + cx > bounds[2] or y + cy > bounds[3]):
                    continue
                dist = (x - want_x) ** 2 + (y - want_y) ** 2
                if best is None or dist < best[2]:
                    best = (x, y, float(dist))
        if best is not None:
            return (best[0], best[1])
    return (want_x, want_y)


def place_in_slot(index: int, screen_x: int, screen_y: int,
                  offset: tuple[int, int] = (0, 0)) -> bool:
    """把图标放进离该屏幕点最近的空格子（找不到就退回「松手处」）。"""
    slot = nearest_free_slot(screen_x, screen_y, skip=index, offset=offset)
    if slot is None:
        return place_item(index, screen_x, screen_y, offset=offset)
    return set_item_position(index, slot[0], slot[1])


# ---------------------------------------------------------------- 让 shell 立刻更新
#
# 文件搬进/搬出桌面目录之后，explorer 的桌面视图是**攒着批量刷新**的。实测
# （51 个图标、本机 125% 缩放）不通知的话：
#   图标出现 485–1028ms，图标消失 485–510ms
# 带上 SHCNF_FLUSH 的 SHChangeNotify（等 shell 真处理完再返回）之后：
#   出现 4–35ms，消失 4ms
# 这就是「收纳/放回」那一下看着要卡半秒的原因，别去掉 FLUSH。

SHCNE_CREATE = 0x00000002
SHCNE_DELETE = 0x00000004
SHCNE_UPDATEITEM = 0x00002000
SHCNF_PATHW = 0x0005
SHCNF_FLUSH = 0x1000

shell32.SHChangeNotify.restype = None
shell32.SHChangeNotify.argtypes = [ctypes.c_long, wt.UINT, ctypes.c_void_p, ctypes.c_void_p]


WM_CANCELMODE = 0x001F
WM_LBUTTONUP = 0x0202


def cancel_shell_drag() -> None:
    """让 explorer 的桌面 ListView 彻底退出「拖拽中」状态。

    `dragwatch` 为了不让 explorer 把「快捷方式丢到快捷方式上」当成「用目标程序
    打开」，会把松手那一下**在全局钩子里吞掉**（钩子返回 1，任何窗口都收不到）。
    后果是 explorer 的拖拽循环被 Esc 掐掉之后，桌面 ListView 还停在「鼠标按着」
    的状态里 —— 偶发留下一个跟着鼠标跑的**残影**（用户报的那个 bug）。

    这里补两下：先 WM_CANCELMODE 让它作废当前模式，再直接投递一个
    WM_LBUTTONUP（坐标用当前光标的客户区坐标），最后让桌面重画一遍。
    此刻 explorer 的拖拽已经被 Esc 取消了，所以这一下 up 只会被当成一次落空的
    点击，不会触发「用 xx 打开」。
    """
    lv = _find_listview()
    if not lv:
        return
    try:
        _send(lv, WM_CANCELMODE)
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.ScreenToClient(lv, ctypes.byref(pt))
        lparam = (pt.y << 16) | (pt.x & 0xFFFF)
        _send(lv, WM_LBUTTONUP, 0, lparam)
        user32.InvalidateRect(lv, None, True)
        _log("shell drag cancelled (WM_CANCELMODE + WM_LBUTTONUP)")
    except Exception:  # noqa: BLE001 - 收尾动作，失败也不该炸
        pass


def _notify(event: int, path) -> None:
    try:
        shell32.SHChangeNotify(event, SHCNF_PATHW | SHCNF_FLUSH, str(path), None)
    except OSError:
        pass


def notify_created(path) -> None:
    """告诉 shell 这个文件刚出现，并且等它处理完（否则桌面图标要等半秒）。"""
    _notify(SHCNE_CREATE, path)


def notify_deleted(path) -> None:
    """告诉 shell 这个文件已经没了，立刻把桌面图标撤掉。"""
    _notify(SHCNE_DELETE, path)


def notify_updated(path) -> None:
    """让 shell 重新读一遍这个文件的图标（快捷方式拖回桌面偶尔画成白纸）。"""
    _notify(SHCNE_UPDATEITEM, path)


# ---------------------------------------------------------------- 显示名 → 文件

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


_FOLDERID_DESKTOP = uuid.UUID("{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}")

shell32.SHGetKnownFolderPath.restype = ctypes.c_long
shell32.SHGetKnownFolderPath.argtypes = [
    ctypes.POINTER(_GUID), wt.DWORD, wt.HANDLE, ctypes.POINTER(ctypes.c_wchar_p),
]
ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]


def _known_folder(guid: uuid.UUID) -> Path | None:
    g = _GUID()
    raw = guid.bytes_le
    ctypes.memmove(ctypes.byref(g), raw, 16)
    out = ctypes.c_wchar_p()
    hr = shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out))
    if hr != 0:
        return None
    try:
        return Path(out.value)
    finally:
        ole32.CoTaskMemFree(out)


def desktop_dirs() -> list[Path]:
    dirs: list[Path] = []
    known = _known_folder(_FOLDERID_DESKTOP)
    if known:
        dirs.append(known)
    home = os.environ.get("USERPROFILE")
    if home:
        p = Path(home) / "Desktop"
        if p not in dirs:
            dirs.append(p)
    public = os.environ.get("PUBLIC")
    if public:
        p = Path(public) / "Desktop"
        if p not in dirs:
            dirs.append(p)
    return [d for d in dirs if d.is_dir()]


_INDEX_CACHE: tuple[float, dict[str, Path]] | None = None


def desktop_index() -> dict[str, Path]:
    """显示名（小写）→ 真实路径。

    资源管理器会藏掉「已知文件类型」的后缀（.lnk / .url / .doc / .txt / .png …），
    所以 name 和 stem 都要登记；快捷方式只是其中一例，普通文件同样要认。
    拖拽时每 20ms 就要查一次，做个 0.5 秒的缓存，避免反复列目录。
    """
    global _INDEX_CACHE
    now = time.monotonic()
    if _INDEX_CACHE and now - _INDEX_CACHE[0] < 0.5:
        return _INDEX_CACHE[1]
    index: dict[str, Path] = {}
    for root in desktop_dirs():
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for p in entries:
            index.setdefault(p.name.lower(), p)
            if p.is_file():
                index.setdefault(p.stem.lower(), p)
    _INDEX_CACHE = (now, index)
    return index


def resolve_item(name: str) -> Path | None:
    return desktop_index().get(name.strip().lower())


def find_desktop_copy(name: str) -> Path | None:
    """在桌面目录里找这个文件名。

    先把文件拖出盒子时，explorer 可能因为撞名把它改成「名字 (2).lnk」，所以
    找不到完全同名的还要认一下带序号的版本。
    """
    stem = Path(name).stem
    suffix = Path(name).suffix.lower()
    dirs = desktop_dirs()
    for root in dirs:
        exact = root / name
        try:
            if exact.is_file() or exact.is_dir():   # 文件夹也要认（盒子里能收文件夹）
                return exact
        except OSError:
            continue
    for root in dirs:
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.suffix.lower() != suffix or not p.stem.startswith(stem):
                continue
            tail = p.stem[len(stem):].strip()
            if tail.startswith("(") and tail.endswith(")"):
                return p
    return None


def item_at_screen(x: int, y: int) -> tuple[int, str, Path | None]:
    """屏幕坐标下命中的图标：索引、显示名、可收纳路径（普通文件也算，目录不算）。"""
    idx = hit_test(x, y)
    if idx < 0:
        return -1, "", None
    name = icon_name(idx)
    path = resolve_item(name) if name else None
    if path is not None and not path.is_file():
        path = None
    return idx, name, path
