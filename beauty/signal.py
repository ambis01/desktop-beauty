"""跨进程发一个小信号：给「任务栏快捷方式」用的。

不带 PySide6，纯 ctypes，启动只要几十毫秒，所以钉在任务栏上的快捷方式
每次单击都很快。协议就是四个命名事件：toggle / panel / quit / reset
（reset 是卸载前那句「毁灭世界」——图标放回桌面再退出）。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

kernel32 = ctypes.WinDLL("kernel32")
advapi32 = ctypes.WinDLL("advapi32")

WAIT_OBJECT_0 = 0
EVENT_NAMES = {
    "toggle": "Local\\desktopbeauty.toggle",
    "panel": "Local\\desktopbeauty.panel",
    "quit": "Local\\desktopbeauty.quit",
    "reset": "Local\\desktopbeauty.reset",
}


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wt.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wt.BOOL)]


# 默认 DACL 是「创建者进程的 token」，别的进程（比如任务栏那个转发用的 exe）
# 去 OpenEvent 会吃 ERROR_ACCESS_DENIED(5)。这里显式给 Everyone 全权限，
# 反正只是个本地会话里的开关信号。
_SDDL = "D:(A;;GA;;;WD)"


def _sa():
    sd = ctypes.c_void_p()
    ok = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        _SDDL, 1, ctypes.byref(sd), None)
    sa = _SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(_SECURITY_ATTRIBUTES)
    sa.lpSecurityDescriptor = sd if ok else None
    sa.bInheritHandle = False
    return sa, sd


def create(name: str) -> int:
    """常驻程序启动时建事件，返回句柄（0 = 失败）。"""
    sa, sd = _sa()
    handle = kernel32.CreateEventW(ctypes.byref(sa), False, False, EVENT_NAMES[name])
    if sd:
        kernel32.LocalFree(sd)
    return int(handle or 0)


def fired(handle: int) -> bool:
    """非阻塞地问一句「有人按了吗」，按了就自动复位。"""
    if not handle:
        return False
    return kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0) == WAIT_OBJECT_0


def send(name: str) -> bool:
    """转发进程用：把对应事件置位。"""
    handle = kernel32.OpenEventW(0x0002, False, EVENT_NAMES[name])  # EVENT_MODIFY_STATE
    if not handle:
        return False
    try:
        return bool(kernel32.SetEvent(ctypes.c_void_p(handle)))
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def exists(name: str) -> bool:
    """常驻实例还在不在（事件对象跟着进程走，进程一退就没了）。

    卸载器用它来等「毁灭世界 + 退出」真的做完，再删文件。
    """
    handle = kernel32.OpenEventW(0x0002, False, EVENT_NAMES[name])
    if not handle:
        return False
    kernel32.CloseHandle(ctypes.c_void_p(handle))
    return True
