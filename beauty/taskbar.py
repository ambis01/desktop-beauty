"""任务栏外观：把 TranslucentTB 收进这个程序里管。

为什么不能自己实现「去掉任务栏黑边」：
26100 的任务栏背景是 **explorer 自己的 XAML** 画的（不是 DWM 画的），
`SetWindowCompositionAttribute`、`TaskbarAcrylicOpacity`、`UseOLEDTaskbarTransparency`
这些一律无效（实测像素零变化）。唯一的路是往 explorer 里注入 DLL 去改它的视觉树
—— 那正是 TranslucentTB 干的事（`ExplorerTAP.dll`）。

所以这里不做注入，而是**替用户管住它**：开机由我们拉起、挂了（比如 explorer 重启
会把它带走）我们拉回来、面板上一个开关就能启停，用户不需要知道它的存在。
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import time
import winreg
from ctypes import wintypes as wt
from pathlib import Path

kernel32 = ctypes.WinDLL("kernel32")

CREATE_NO_WINDOW = 0x08000000
TH32CS_SNAPPROCESS = 0x00000002
PROCESS_TERMINATE = 0x0001

EXE_NAME = "TranslucentTB.exe"
ADVANCED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"


def _candidates() -> list[Path]:
    """TranslucentTB 可能在哪：本程序自带的那份优先，最后才是用户自己装的。"""
    out: list[Path] = []
    try:
        from . import install
        out.append(install.install_dir() / "translucenttb" / EXE_NAME)
        out.append(install.dist_root() / "translucenttb" / EXE_NAME)
        # 源码跑的时候它放在仓库的 vendor\ 下
        out.append(install.dist_root() / "vendor" / "translucenttb" / EXE_NAME)
    except Exception:  # noqa: BLE001 - 拿不到安装路径就只找系统里那份
        pass
    out.append(Path.home() / "AppData" / "Local" / "Programs" / "TranslucentTB" / EXE_NAME)
    return out


def exe_path() -> Path:
    """自带的优先（升级/装的时候会一起复制过去），没有再退回用户自己装的那份。"""
    cands = _candidates()
    for p in cands:
        if p.is_file():
            return p
    return cands[-1]


def available() -> bool:
    return exe_path().is_file()


def _load_raw(path: Path) -> dict:
    # TranslucentTB 自己回写这个文件时是**带 BOM** 的 utf-8，用 utf-8 读会直接
    # JSONDecodeError（表现为「面板开关点了没反应、看门狗也不拉起」）
    text = path.read_text(encoding="utf-8-sig")
    # 文件头有 // 注释，直接喂 json 会炸
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("//")]
    return json.loads("\n".join(lines))


# ---------------------------------------------------------------- 进程
class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)), ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * 260),
    ]


def pids() -> list[int]:
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (0, -1):
        return []
    out: list[int] = []
    entry = _PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
    try:
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == EXE_NAME.lower():
                out.append(int(entry.th32ProcessID))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(snap))
    return out


def is_running() -> bool:
    return bool(pids())


def start() -> bool:
    if not available():
        return False
    if is_running():
        return True
    try:
        subprocess.Popen([str(exe_path())], cwd=str(exe_path().parent),
                         creationflags=CREATE_NO_WINDOW, close_fds=True)
    except OSError:
        return False
    for _ in range(30):
        if is_running():
            return True
        time.sleep(0.05)
    return is_running()


def stop(wait: bool = True) -> None:
    for pid in pids():
        handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if not handle:
            continue
        try:
            kernel32.TerminateProcess(ctypes.c_void_p(handle), 0)
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    # 它自己有单实例互斥量：不等旧进程真的消失就重启，新进程会以为
    # 已经有实例在跑，直接退出 —— 表现就是「重启完反而没了」
    if wait:
        for _ in range(40):
            if not pids():
                return
            time.sleep(0.05)


# ---------------------------------------------------------------- 设置
MAXIMIZED_MODES = ("acrylic", "clear", "normal")


def apply_settings(maximized: str | None = None, clear: bool = True) -> bool:
    """改 TranslucentTB 的 settings.json，然后重启它让改动生效。"""
    if not available():
        return False
    path = exe_path().parent / "settings.json"
    try:
        data = _load_raw(path)
    except (OSError, ValueError):
        return False

    if clear:
        desk = data.setdefault("desktop_appearance", {})
        desk["accent"] = "clear"
        desk["color"] = "#00000000"
        desk["show_line"] = False
        desk["show_peek"] = False
    if maximized in MAXIMIZED_MODES:
        over = data.setdefault("maximized_window_appearance", {})
        over["enabled"] = True
        over["accent"] = maximized
        over["color"] = "#00000000"
        over["show_line"] = False
        over["show_peek"] = True
    # 托盘图标藏掉（文档里说这个键默认不写出来，得手动加），
    # 再加上禁止它自己回写配置：它回写会带 BOM，还会把我们设的模式冲掉
    data["hide_tray"] = True
    data["disable_saving"] = True

    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")
    except OSError:
        return False

    # 它改完不会自己重读，重启一下（TerminateProcess 不会触发它回写旧配置）
    stop()
    start()
    return True


def ensure(clear: bool, maximized: str) -> None:
    """开机 / explorer 重启之后，把它拉回来。"""
    if not clear or not available():
        return
    if is_running():
        return
    # 配置写不进去（文件被锁 / 格式变了）也必须把它拉起来，
    # 否则表现就是「任务栏黑边回来了，面板怎么点都没救」
    if not apply_settings(maximized, clear):
        start()


# ---------------------------------------------------------------- 任务栏对齐
def reg_key() -> str:
    """测试时可用 DESKBEAUTY_TASKBAR_KEY 指到别的键，别动真设置。"""
    return os.environ.get("DESKBEAUTY_TASKBAR_KEY") or ADVANCED_KEY


def set_left_align(on: bool = True) -> bool:
    """任务栏图标靠左：`TaskbarAl = 0`（1 = 居中）。返回有没有真的改动。

    这是 Windows 自己的设置（任务栏设置 → 任务栏行为 → 任务栏对齐方式），
    不需要注入、不用重启 explorer，改完当场生效。以前只在开发机上手动改过，
    分出去的版本里没有，所以别人装完图标还是居中的。
    只在启动时应用一次：watchdog 每 8 秒跑的那个循环里**不**反复写，
    否则用户自己去设置里改成居中，会被我们一直掰回去。
    """
    want = 0 if on else 1
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_key(), 0,
                            winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
            try:
                current, _kind = winreg.QueryValueEx(key, "TaskbarAl")
            except OSError:
                current = None
            if current == want:
                return False
            winreg.SetValueEx(key, "TaskbarAl", 0, winreg.REG_DWORD, want)
            return True
    except OSError:
        return False

