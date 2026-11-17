"""把收纳盒 / 卡片摆回「桌面正上方、所有普通窗口下面」这一层。

背景（都是实测出来的）：

* 桌面（Progman）是全屏不透明窗口，谁在它下面就被壁纸整个盖住。收纳盒要
  「贴桌面」显示，就只能是「桌面正上方」那一层。
* 点任务栏右下角「显示桌面」时，外壳会把 Progman 抬到普通窗口带的最上面，
  于是收纳盒被盖住——这时候要把它们重新插回 Progman 正上方。
* 反过来更常发生：点一下收纳盒，Windows 就把它**激活并抬到最前**，于是它浮在
  别的窗口上面；外壳把窗口放回来的时候也不一定插在收纳盒上面。以前这里只做
  了「被桌面盖住就往上抬」这一半，抬完就再也回不去，盒子就永久浮在窗口上，
  点一下窗口内容才被盖住（用户 2026-11-17 报的）。

所以这里管**两个方向**：被桌面盖住 → 抬到桌面上面；跑到了某个普通窗口上面 →
压回那个窗口下面。而且**只动真正跑偏的那几块**：每块 `SetWindowPos` 都会让
DWM 重新合成一遍，半透明窗口肉眼就是闪一下，能只挪一块就别挪整串。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as wt

user32 = ctypes.WinDLL("user32")
dwmapi = ctypes.WinDLL("dwmapi")

user32.GetTopWindow.restype = wt.HWND
user32.GetTopWindow.argtypes = [wt.HWND]
user32.GetWindow.restype = wt.HWND
user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
user32.IsWindowVisible.restype = wt.BOOL
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsIconic.restype = wt.BOOL
user32.IsIconic.argtypes = [wt.HWND]
user32.GetWindowLongW.restype = ctypes.c_long
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowRect.restype = wt.BOOL
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.FindWindowW.restype = wt.HWND
user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
user32.GetForegroundWindow.restype = wt.HWND
user32.GetForegroundWindow.argtypes = []
user32.SetWindowPos.restype = wt.BOOL
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p,
                                         wt.DWORD]

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
DWMWA_CLOAKED = 14
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_NOOWNERZORDER = 0x0200

GW_HWNDPREV = 3
GW_HWNDNEXT = 2

# 桌面外壳自己的窗口：它们不是「普通应用窗口」，不能拿来当上限
SHELL_CLASSES = {
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
    "SHELLDLL_DefView", "SysListView32", "Windows.UI.Core.CoreWindow",
    "EdgeUiInputTopWndClass", "DummyDWMListenerWindow",
    "MultitaskingViewFrame", "ForegroundStaging",
}


def visible_chain(limit: int = 800) -> list[int]:
    """从前往后的可见顶层窗口（不含隐藏窗口）。"""
    out: list[int] = []
    h = user32.GetTopWindow(None)
    while h and len(out) < limit:
        if user32.IsWindowVisible(h):
            out.append(int(h))
        h = user32.GetWindow(h, GW_HWNDNEXT)
    return out


def desktop_hwnd() -> int:
    return int(user32.FindWindowW("Progman", None) or 0)


def foreground_hwnd() -> int:
    return int(user32.GetForegroundWindow() or 0)


def class_of(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(wt.HWND(int(hwnd)), buf, 256)
    return buf.value


def _cloaked(hwnd) -> bool:
    v = wt.DWORD(0)
    hr = dwmapi.DwmGetWindowAttribute(wt.HWND(int(hwnd)), DWMWA_CLOAKED,
                                      ctypes.byref(v), ctypes.sizeof(v))
    return (v.value != 0) if hr == 0 else False


def is_normal(hwnd) -> bool:
    """这是不是「一个普通应用窗口」（能压住收纳盒的那种）。

    排除：隐藏 / 最小化（只剩任务栏占位）/ 工具窗口（各种覆盖层）/ 置顶窗口
    （任务栏、输入法条）/ 被 DWM 遮盖的 UWP 幽灵窗口 / 桌面外壳 / 小到看不见的。
    不排除的话，链子里那些看不见的窗口会被当成「压在收纳盒上面的普通窗口」，
    于是每一轮都要重排一次——那就是一直闪。
    """
    try:
        hwnd = int(hwnd)
        if not hwnd or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return False
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if ex & (WS_EX_TOOLWINDOW | WS_EX_TOPMOST):
            return False
        if class_of(hwnd) in SHELL_CLASSES:
            return False
        r = wt.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return False
        if r.right - r.left < 40 or r.bottom - r.top < 40:
            return False
        return not _cloaked(hwnd)
    except OSError:
        return False


def analyze(ours_bottom_to_top: list[int]) -> dict:
    """算这一轮要不要动、动哪几块、插到谁下面。

    返回 {"action": "ok"|"raise"|"demote", "why": str, "anchor": hwnd,
          "move": [hwnd, ...]}，`move` 从下往上。
    """
    ours = [int(h) for h in ours_bottom_to_top if h]
    res = {"action": "ok", "why": "no windows", "anchor": 0, "move": []}
    if not ours:
        return res
    prog = desktop_hwnd()
    if not prog:
        res["why"] = "no progman"
        return res
    chain = visible_chain()
    try:
        prog_i = chain.index(prog)
    except ValueError:
        res["why"] = "progman not visible"
        return res
    pos = {h: i for i, h in enumerate(chain)}
    mine = [(pos[h], h) for h in ours if h in pos]
    if not mine:
        res["why"] = "none visible"
        return res
    if any(i > prog_i for i, _h in mine):
        # 桌面被抬到我们上面（点了「显示桌面」）
        anchor = int(user32.GetWindow(wt.HWND(prog), GW_HWNDPREV) or 0)
        res.update(action="raise", why="desktop covers us", anchor=anchor,
                   move=list(ours))
        return res
    # 普通窗口里最靠下的那一个：我们的窗口必须在它下面
    ours_set = set(ours)
    low = None
    for i in range(prog_i - 1, min(i for i, _h in mine) - 1, -1):
        h = chain[i]
        if h in ours_set:
            continue
        if is_normal(h):
            low = i
            break
    if low is None:
        res["why"] = "in band"
        return res
    anchor = chain[low]
    # 只挪「跑到那个窗口上面去」的那几块；从下往上插，插完顺序才是对的
    move = [h for i, h in sorted(mine, reverse=True) if i < low]
    if not move:
        res.update(why="in band", action="ok")
        return res
    res.update(action="demote", why=f"above {class_of(anchor)}", anchor=anchor,
               move=move)
    return res


def apply_plan(plan: dict) -> int:
    """按 `analyze` 的结果动手；返回真正挪了几块。"""
    anchor = plan.get("anchor") or 0
    moved = 0
    for hwnd in plan.get("move") or []:
        try:
            ok = user32.SetWindowPos(wt.HWND(int(hwnd)), wt.HWND(anchor) if anchor else None,
                                     0, 0, 0, 0,
                                     SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
                                     | SWP_NOOWNERZORDER)
        except OSError:
            continue
        if ok:
            moved += 1
    return moved
