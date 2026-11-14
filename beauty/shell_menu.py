"""直接弹资源管理器那套原生右键菜单（IContextMenu），不再自己造菜单。

流程就是 shell 的标准三步：
1. `SHParseDisplayName` 把路径变成 PIDL
2. `SHBindToParent` 拿到父文件夹的 `IShellFolder` + 最后一段子 PIDL
3. `GetUIObjectOf(IID_IContextMenu)` → `QueryContextMenu` → `TrackPopupMenuEx`
   → `InvokeCommand`

这样菜单里的「打开 / 打开文件所在位置 / 重命名 / 删除 / 属性 / 发送到…」
全是系统自己的，跟桌面上右键一个样。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time

shell32 = ctypes.WinDLL("shell32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32")
user32 = ctypes.WinDLL("user32")

HRESULT = ctypes.c_long
ULONG = ctypes.c_ulong


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> _GUID:
    import uuid
    u = uuid.UUID(text)
    g = _GUID()
    g.Data1 = u.time_low
    g.Data2 = u.time_mid
    g.Data3 = u.time_hi_version
    for i, b in enumerate(u.bytes[8:]):
        g.Data4[i] = b
    return g


IID_IShellFolder = _guid("{000214E6-0000-0000-C000-000000000046}")
IID_IContextMenu = _guid("{000214E4-0000-0000-C000-000000000046}")

shell32.SHParseDisplayName.restype = HRESULT
shell32.SHParseDisplayName.argtypes = [
    wt.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
    wt.DWORD, ctypes.POINTER(wt.DWORD),
]
shell32.SHBindToParent.restype = HRESULT
shell32.SHBindToParent.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(_GUID),
    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
]
ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
user32.CreatePopupMenu.restype = wt.HMENU
user32.DestroyMenu.argtypes = [wt.HMENU]
user32.TrackPopupMenuEx.restype = wt.BOOL
user32.TrackPopupMenuEx.argtypes = [
    wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, wt.HWND, ctypes.c_void_p,
]


class CMINVOKECOMMANDINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("fMask", wt.DWORD),
        ("hwnd", wt.HWND),
        ("lpVerb", ctypes.c_void_p),
        ("lpParameters", ctypes.c_char_p),
        ("lpDirectory", ctypes.c_char_p),
        ("nShow", ctypes.c_int),
        ("dwHotKey", wt.DWORD),
        ("hIcon", wt.HANDLE),
    ]


_GET_UI_OBJECT_OF = 12
# IContextMenu 也是 COM 接口，前三个槽是 IUnknown
_QUERY_CONTEXT_MENU = 3
_INVOKE_COMMAND = 4
_RELEASE = 2

_GetUIObjectOf = ctypes.WINFUNCTYPE(
    HRESULT, ctypes.c_void_p, wt.HWND, wt.UINT, ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(_GUID), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
_QueryContextMenu = ctypes.WINFUNCTYPE(
    HRESULT, ctypes.c_void_p, wt.HMENU, wt.UINT, wt.UINT, wt.UINT, wt.UINT)
_InvokeCommand = ctypes.WINFUNCTYPE(
    HRESULT, ctypes.c_void_p, ctypes.POINTER(CMINVOKECOMMANDINFO))
_Release = ctypes.WINFUNCTYPE(ULONG, ctypes.c_void_p)


def _vtable(ptr: int, index: int, proto):
    table = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    return proto(table[index])


class DEFCONTEXTMENU(ctypes.Structure):
    _fields_ = [
        ("hwnd", wt.HWND),
        ("pcmcb", ctypes.c_void_p),
        ("pidlFolder", ctypes.c_void_p),
        ("psf", ctypes.c_void_p),
        ("cidl", wt.UINT),
        ("apidl", ctypes.POINTER(ctypes.c_void_p)),
        ("punkAssociationInfo", ctypes.c_void_p),
        ("cKeys", wt.UINT),
        ("aKeys", ctypes.c_void_p),
    ]


shell32.SHCreateDefaultContextMenu.restype = HRESULT
shell32.SHCreateDefaultContextMenu.argtypes = [
    ctypes.POINTER(DEFCONTEXTMENU), ctypes.POINTER(_GUID),
    ctypes.POINTER(ctypes.c_void_p),
]


user32.GetWindowLongW.restype = ctypes.c_long
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
user32.SetLayeredWindowAttributes.argtypes = [
    wt.HWND, wt.DWORD, ctypes.c_ubyte, wt.DWORD]
WS_EX_LAYERED = 0x00080000


def _glassify(hwnd: int) -> None:
    """把系统的 #32768 菜单窗口变半透明 + 背后模糊（Win11 那种亚克力感）。"""
    try:
        from .glass import apply_acrylic
        style = user32.GetWindowLongW(hwnd, -20)
        if not (style & WS_EX_LAYERED):
            user32.SetWindowLongW(hwnd, -20, style | WS_EX_LAYERED)
            # 242/255：几乎不透明，只让背后的模糊透上来一点点
            user32.SetLayeredWindowAttributes(hwnd, 0, 242, 2)
        apply_acrylic(hwnd, 0x48, rgb=(246, 248, 252), state=4)
    except Exception:  # noqa: BLE001
        pass


def _watch_menu(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            h = user32.FindWindowW("#32768", None)
            if h:
                _glassify(int(h))
        except OSError:
            pass
        time.sleep(0.02)


def warmup() -> None:
    """启动时先把 COM 初始化掉：第一次弹菜单慢，主要就是卡在这一步。"""
    try:
        ole32.CoInitializeEx(None, 0x2)
    except OSError:
        pass


def _context_menu(path: str):
    """给一个文件建 IContextMenu：返回 (pcm, menu) 或者 None。"""
    try:
        ole32.CoInitializeEx(None, 0x2)      # STA，失败也没关系
    except OSError:
        pass
    pidl = ctypes.c_void_p()
    eaten = wt.DWORD()
    if shell32.SHParseDisplayName(str(path), None, ctypes.byref(pidl),
                                  0, ctypes.byref(eaten)) != 0 or not pidl:
        return None
    parent = ctypes.c_void_p()
    child = ctypes.c_void_p()
    if shell32.SHBindToParent(pidl, ctypes.byref(IID_IShellFolder),
                              ctypes.byref(parent),
                              ctypes.byref(child)) != 0 or not parent:
        ole32.CoTaskMemFree(pidl)
        return None
    apidl = (ctypes.c_void_p * 1)(child.value)
    dcm = DEFCONTEXTMENU()
    dcm.hwnd = None
    dcm.pidlFolder = None
    dcm.psf = parent
    dcm.cidl = 1
    dcm.apidl = apidl
    pcm = ctypes.c_void_p()
    if shell32.SHCreateDefaultContextMenu(
            ctypes.byref(dcm), ctypes.byref(IID_IContextMenu),
            ctypes.byref(pcm)) != 0 or not pcm:
        ole32.CoTaskMemFree(pidl)
        return None
    return pcm, pidl


_GET_COMMAND_STRING = 5
GCS_VERBW = 4
_GetCommandString = ctypes.WINFUNCTYPE(
    HRESULT, ctypes.c_void_p, ctypes.c_size_t, wt.UINT,
    ctypes.POINTER(wt.UINT), ctypes.POINTER(ctypes.c_wchar), wt.UINT)


def invoke_verb(hwnd: int, path: str, verb: str) -> bool:
    """直接执行这个文件的某个系统动词（open / cut / copy / delete / properties…）。

    自己画菜单的时候用：菜单样式我们自己控，真正干活还是交给 shell 那套
    IContextMenu，剪贴板、回收站、确认框都是系统行为。
    """
    made = _context_menu(path)
    if made is None:
        return False
    pcm, pidl = made
    menu = None
    try:
        menu = user32.CreatePopupMenu()
        if not menu:
            return False
        query = _vtable(pcm.value, _QUERY_CONTEXT_MENU, _QueryContextMenu)
        if query(pcm, menu, 0, 1, 0x7FFF, 0x00000000) < 0:
            return False
        getstr = _vtable(pcm.value, _GET_COMMAND_STRING, _GetCommandString)
        found = 0
        count = int(user32.GetMenuItemCount(menu))
        buf = ctypes.create_unicode_buffer(256)
        for i in range(count):
            mid = int(user32.GetMenuItemID(menu, i)) & 0xFFFFFFFF
            # 分隔符返回 0，二级菜单返回 -1：这两个都不能拿去当命令号问
            # （把 0xFFFFFFFE 当 idCmd 传进去，shell 会直接读野指针崩掉）
            if mid == 0 or mid >= 0x80000000:
                continue
            for k in range(256):
                buf[k] = "\0"
            if getstr(pcm, ctypes.c_size_t(mid - 1), GCS_VERBW, None,
                      ctypes.cast(buf, ctypes.POINTER(ctypes.c_wchar)),
                      256) < 0:
                continue
            if buf.value.lower() == str(verb).lower():
                found = mid - 1
                break
        if not found:
            return False
        user32.SetForegroundWindow(hwnd)
        info = CMINVOKECOMMANDINFO()
        info.cbSize = ctypes.sizeof(info)
        info.hwnd = hwnd
        info.lpVerb = ctypes.c_void_p(found)     # MAKEINTRESOURCE
        info.nShow = 1
        invoke = _vtable(pcm.value, _INVOKE_COMMAND, _InvokeCommand)
        return invoke(pcm, ctypes.byref(info)) == 0
    except (OSError, ValueError):
        return False
    finally:
        if menu:
            user32.DestroyMenu(menu)
        _vtable(pcm.value, _RELEASE, _Release)(pcm)
        if pidl:
            ole32.CoTaskMemFree(pidl)


user32.GetMenuItemCount.argtypes = [wt.HMENU]
user32.GetMenuItemID.argtypes = [wt.HMENU, ctypes.c_int]
user32.GetMenuItemID.restype = wt.UINT


def show(hwnd: int, path: str, x: int, y: int) -> bool:
    """在 (x, y) 弹这个文件的系统右键菜单。返回有没有真的弹出来。"""
    try:
        ole32.CoInitializeEx(None, 0x2)      # STA，失败也没关系
    except OSError:
        pass
    pidl = ctypes.c_void_p()
    eaten = wt.DWORD()
    try:
        if shell32.SHParseDisplayName(str(path), None, ctypes.byref(pidl),
                                      0, ctypes.byref(eaten)) != 0 or not pidl:
            return False
        parent = ctypes.c_void_p()
        child = ctypes.c_void_p()
        if shell32.SHBindToParent(pidl, ctypes.byref(IID_IShellFolder),
                                  ctypes.byref(parent),
                                  ctypes.byref(child)) != 0 or not parent:
            return False
        # 用 SHCreateDefaultContextMenu 拿 IContextMenu：比直接 GetUIObjectOf
        # 稳（后者的 vtable 位置在不同 shell 版本上对不上会直接崩）。
        apidl = (ctypes.c_void_p * 1)(child.value)
        dcm = DEFCONTEXTMENU()
        dcm.hwnd = hwnd
        dcm.pidlFolder = None
        dcm.psf = parent
        dcm.cidl = 1
        dcm.apidl = apidl
        pcm = ctypes.c_void_p()
        if shell32.SHCreateDefaultContextMenu(
                ctypes.byref(dcm), ctypes.byref(IID_IContextMenu),
                ctypes.byref(pcm)) != 0 or not pcm:
            return False
        menu = user32.CreatePopupMenu()
        if not menu:
            return False
        try:
            query = _vtable(pcm.value, _QUERY_CONTEXT_MENU, _QueryContextMenu)
            if query(pcm, menu, 0, 1, 0x7FFF, 0x00000000) < 0:
                return False
            user32.SetForegroundWindow(hwnd)
            # 弹菜单期间开个后台线程盯着 #32768（系统菜单窗口），一出现就套玻璃
            stop = threading.Event()
            th = threading.Thread(target=_watch_menu, args=(stop,), daemon=True)
            th.start()
            try:
                # TPM_RETURNCMD | TPM_RIGHTBUTTON：返回点中的命令 ID
                cmd = user32.TrackPopupMenuEx(menu, 0x0100 | 0x0002, int(x), int(y),
                                              hwnd, None)
            finally:
                stop.set()
            user32.PostMessageW(hwnd, 0x0000, 0, 0)   # WM_NULL
            if not cmd:
                return True
            info = CMINVOKECOMMANDINFO()
            info.cbSize = ctypes.sizeof(info)
            info.hwnd = hwnd
            info.lpVerb = ctypes.c_void_p(int(cmd) - 1)   # MAKEINTRESOURCE
            info.nShow = 1
            invoke = _vtable(pcm.value, _INVOKE_COMMAND, _InvokeCommand)
            invoke(pcm, ctypes.byref(info))
            return True
        finally:
            if menu:
                user32.DestroyMenu(menu)
            _vtable(pcm.value, _RELEASE, _Release)(pcm)
    finally:
        if pidl:
            ole32.CoTaskMemFree(pidl)


# ---------------------------------------------------------------- 快手路径
# 自己画的那张菜单里「剪切 / 复制 / 删除 / 属性 / 打开文件所在位置 / 以管理员
# 身份运行」全是注册表动词或者纯剪贴板的事，没有一条真的需要 IContextMenu。
# 走 IContextMenu 每点一次都要重建一遍再 QueryContextMenu 把外壳扩展全加载
# 一遍——实测 460ms，冷启动那次 1700ms。所以这里直连 Win32。

CF_HDROP = 15
GMEM_MOVEABLE = 0x0002
DROPEFFECT_COPY = 1
DROPEFFECT_MOVE = 2
FO_DELETE = 3
FOF_ALLOWUNDO = 0x0040
FOF_NOCONFIRMMKDIR = 0x0200
SHOP_FILEPATH = 2
SEE_MASK_FLAG_NO_UI = 0x00000400
SW_SHOWNORMAL = 1

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
user32.OpenClipboard.argtypes = [wt.HWND]
user32.EmptyClipboard.restype = wt.BOOL
user32.SetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = [wt.UINT, ctypes.c_void_p]
user32.CloseClipboard.restype = wt.BOOL
user32.RegisterClipboardFormatW.restype = wt.UINT
user32.RegisterClipboardFormatW.argtypes = [wt.LPCWSTR]


class _DROPFILES(ctypes.Structure):
    _fields_ = [("pFiles", wt.DWORD), ("pt", wt.POINT),
                ("fNC", wt.BOOL), ("fWide", wt.BOOL)]


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wt.HWND), ("wFunc", wt.UINT),
                ("pFrom", ctypes.c_void_p), ("pTo", ctypes.c_void_p),
                ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wt.BOOL),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", ctypes.c_void_p)]


class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("fMask", ctypes.c_ulong),
                ("hwnd", wt.HWND), ("lpVerb", wt.LPCWSTR), ("lpFile", wt.LPCWSTR),
                ("lpParameters", wt.LPCWSTR), ("lpDirectory", wt.LPCWSTR),
                ("nShow", ctypes.c_int), ("hInstApp", wt.HINSTANCE),
                ("lpIDList", ctypes.c_void_p), ("lpClass", wt.LPCWSTR),
                ("hkeyClass", wt.HKEY), ("dwHotKey", wt.DWORD),
                ("hIcon", ctypes.c_void_p), ("hProcess", ctypes.c_void_p)]


shell32.SHFileOperationW.restype = ctypes.c_int
shell32.SHFileOperationW.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
shell32.SHObjectProperties.restype = wt.BOOL
shell32.SHObjectProperties.argtypes = [wt.HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR]
shell32.ShellExecuteExW.restype = wt.BOOL
shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]


def _global_bytes(data: bytes):
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h:
        return None
    p = kernel32.GlobalLock(h)
    if not p:
        kernel32.GlobalFree(h)
        return None
    ctypes.memmove(p, data, len(data))
    kernel32.GlobalUnlock(h)
    return h


def copy_files(hwnd: int, paths, move: bool = False) -> bool:
    """剪切 / 复制文件：CF_HDROP 加一个 Preferred DropEffect 就够资源管理器粘贴。"""
    items = [str(p) for p in paths if str(p)]
    if not items:
        return False
    head = _DROPFILES()
    head.pFiles = ctypes.sizeof(_DROPFILES)
    head.pt = wt.POINT(0, 0)
    head.fNC = False
    head.fWide = True
    blob = bytes(head) + ("\0".join(items) + "\0\0").encode("utf-16-le")
    if not user32.OpenClipboard(ctypes.c_void_p(int(hwnd or 0))):
        return False
    try:
        user32.EmptyClipboard()
        h = _global_bytes(blob)
        if not h or not user32.SetClipboardData(CF_HDROP, ctypes.c_void_p(h)):
            return False
        fmt = int(user32.RegisterClipboardFormatW("Preferred DropEffect"))
        if fmt:
            eff = (DROPEFFECT_MOVE if move else DROPEFFECT_COPY).to_bytes(4, "little")
            he = _global_bytes(eff)
            if he:
                user32.SetClipboardData(fmt, ctypes.c_void_p(he))
        return True
    except OSError:
        return False
    finally:
        user32.CloseClipboard()


def delete_files(hwnd: int, paths) -> bool:
    """删除到回收站：SHFileOperation 不走外壳扩展，删快捷方式不会转半天。"""
    items = [str(p) for p in paths if str(p)]
    if not items:
        return False
    src = ctypes.create_unicode_buffer("\0".join(items) + "\0\0")
    op = _SHFILEOPSTRUCTW()
    op.hwnd = hwnd
    op.wFunc = FO_DELETE
    op.pFrom = ctypes.cast(src, ctypes.c_void_p)
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMMKDIR
    try:
        return shell32.SHFileOperationW(ctypes.byref(op)) == 0
    except OSError:
        return False


def show_properties(hwnd: int, path: str) -> bool:
    try:
        return bool(shell32.SHObjectProperties(hwnd, SHOP_FILEPATH, str(path), None))
    except OSError:
        return False


def run_verb(hwnd: int, path: str, verb: str, params: str | None = None) -> bool:
    """直接用 ShellExecuteEx 跑注册表动词（runas / opencontaining …）。"""
    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(_SHELLEXECUTEINFOW)
    info.fMask = SEE_MASK_FLAG_NO_UI
    info.hwnd = hwnd
    info.lpVerb = str(verb)
    info.lpFile = str(path)
    info.lpParameters = params
    info.nShow = SW_SHOWNORMAL
    try:
        return bool(shell32.ShellExecuteExW(ctypes.byref(info)))
    except OSError:
        return False


def open_containing(hwnd: int, path: str) -> bool:
    """打开文件所在位置：先让 shell 找（快捷方式会跳到目标目录），不行再自己开。"""
    if run_verb(hwnd, path, "opencontaining"):
        return True
    import subprocess
    try:
        subprocess.Popen(["explorer", "/select,", str(path)])
        return True
    except OSError:
        return False
