"""解析 .lnk 快捷方式的目标路径（纯 ctypes 调 IShellLinkW + IPersistFile）。

只用来拿“干净”的图标：直接给 .lnk 取图标会带上左下角的小箭头，
取目标 exe 的图标才是桌面应用本来的样子。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import uuid
from pathlib import Path

ole32 = ctypes.WinDLL("ole32")
ole32.CoInitialize.argtypes = [ctypes.c_void_p]
ole32.CoCreateInstance.restype = ctypes.c_long
ole32.CoCreateInstance.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
]
ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]

shell32 = ctypes.WinDLL("shell32")
shell32.SHParseDisplayName.restype = ctypes.c_long
shell32.SHParseDisplayName.argtypes = [
    wt.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
    wt.DWORD, ctypes.POINTER(wt.DWORD),
]

CLSCTX_INPROC_SERVER = 1
STGM_READ = 0


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> _GUID:
    g = _GUID()
    ctypes.memmove(ctypes.byref(g), uuid.UUID(text).bytes_le, 16)
    return g


_CLSID_SHELLLINK = _guid("{00021401-0000-0000-C000-000000000046}")
_IID_SHELLLINKW = _guid("{000214F9-0000-0000-C000-000000000046}")
_IID_PERSISTFILE = _guid("{0000010b-0000-0000-C000-000000000046}")

# IShellLinkW 虚表下标
_ISL_QUERYINTERFACE = 0
_ISL_RELEASE = 2
_ISL_GETPATH = 3
_ISL_SETIDLIST = 5
_ISL_SETDESCRIPTION = 7
_ISL_SETWORKINGDIRECTORY = 9
_ISL_SETARGUMENTS = 11
_ISL_GETICONLOCATION = 16
_ISL_SETICONLOCATION = 17
_ISL_SETPATH = 20
# IPersistFile 虚表下标
_PF_RELEASE = 2
_PF_LOAD = 5
_PF_SAVE = 6
STGM_READWRITE = 2

_com_ready = False
_cache: dict[str, Path | None] = {}


def _ensure_com() -> bool:
    global _com_ready
    if not _com_ready:
        try:
            ole32.CoInitialize(None)
            _com_ready = True
        except OSError:
            return False
    return True


def _method(ptr, index: int, restype, *argtypes):
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(vtbl[index])


def target_of(path: Path | str) -> Path | None:
    """返回 .lnk 的目标路径；解析不了返回 None。"""
    key = str(path)
    if key in _cache:
        return _cache[key]
    result: Path | None = None
    if _ensure_com():
        p = ctypes.c_void_p()
        try:
            if ole32.CoCreateInstance(
                    ctypes.byref(_CLSID_SHELLLINK), None, CLSCTX_INPROC_SERVER,
                    ctypes.byref(_IID_SHELLLINKW), ctypes.byref(p)) == 0:
                pf = ctypes.c_void_p()
                qi = _method(p, _ISL_QUERYINTERFACE, ctypes.c_long,
                             ctypes.c_void_p, ctypes.c_void_p)
                if qi(p, ctypes.byref(_IID_PERSISTFILE), ctypes.byref(pf)) == 0 and pf:
                    try:
                        load = _method(pf, _PF_LOAD, ctypes.c_long,
                                       wt.LPCWSTR, wt.DWORD)
                        if load(pf, str(path), STGM_READ) == 0:
                            buf = ctypes.create_unicode_buffer(1024)
                            getpath = _method(p, _ISL_GETPATH, ctypes.c_long,
                                              wt.LPWSTR, ctypes.c_int, ctypes.c_void_p,
                                              wt.DWORD)
                            if getpath(p, buf, 1024, None, 0) == 0 and buf.value:
                                result = Path(buf.value)
                    finally:
                        _method(pf, _PF_RELEASE, ctypes.c_ulong)(pf)
        finally:
            if p:
                _method(p, _ISL_RELEASE, ctypes.c_ulong)(p)
    if result is not None and not result.exists():
        result = None
    _cache[key] = result
    return result


def _open_lnk(path, mode: int):
    """打开一个 .lnk，返回 (IShellLinkW, IPersistFile) 或 (None, None)。

    调用方负责 Release：这里的返回是裸指针，别泄漏。
    """
    if not _ensure_com():
        return None, None
    p = ctypes.c_void_p()
    if ole32.CoCreateInstance(
            ctypes.byref(_CLSID_SHELLLINK), None, CLSCTX_INPROC_SERVER,
            ctypes.byref(_IID_SHELLLINKW), ctypes.byref(p)) != 0 or not p:
        return None, None
    pf = ctypes.c_void_p()
    qi = _method(p, _ISL_QUERYINTERFACE, ctypes.c_long,
                 ctypes.c_void_p, ctypes.c_void_p)
    if qi(p, ctypes.byref(_IID_PERSISTFILE), ctypes.byref(pf)) != 0 or not pf:
        _method(p, _ISL_RELEASE, ctypes.c_ulong)(p)
        return None, None
    load = _method(pf, _PF_LOAD, ctypes.c_long, wt.LPCWSTR, wt.DWORD)
    if load(pf, str(path), mode) != 0:
        _method(pf, _PF_RELEASE, ctypes.c_ulong)(pf)
        _method(p, _ISL_RELEASE, ctypes.c_ulong)(p)
        return None, None
    return p, pf


def icon_location(path: Path | str) -> tuple[str, int] | None:
    """读 .lnk 里写死的图标来源：(路径, 索引)。没写死返回 ('', 0)。"""
    p, pf = _open_lnk(path, STGM_READ)
    if p is None:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        idx = ctypes.c_int(0)
        get = _method(p, _ISL_GETICONLOCATION, ctypes.c_long, wt.LPWSTR,
                      ctypes.c_int, ctypes.POINTER(ctypes.c_int))
        if get(p, buf, 1024, ctypes.byref(idx)) != 0:
            return None
        return buf.value, int(idx.value)
    finally:
        _method(pf, _PF_RELEASE, ctypes.c_ulong)(pf)
        _method(p, _ISL_RELEASE, ctypes.c_ulong)(p)


def create_lnk(path: Path | str, target: Path | str, args: str = "",
               workdir: Path | str = "", icon: Path | str | None = None,
               icon_index: int = 0, description: str = "") -> bool:
    """新建（或覆盖）一个 .lnk，指向 target。

    装机用的：开始菜单 / 开机自启 / 桌面那三个快捷方式都走这里。
    参数、工作目录、图标都写死，不依赖系统去猜。
    """
    if not _ensure_com():
        return False
    path = Path(path)
    p = ctypes.c_void_p()
    try:
        if ole32.CoCreateInstance(
                ctypes.byref(_CLSID_SHELLLINK), None, CLSCTX_INPROC_SERVER,
                ctypes.byref(_IID_SHELLLINKW), ctypes.byref(p)) != 0 or not p:
            return False
        set_path = _method(p, _ISL_SETPATH, ctypes.c_long, wt.LPCWSTR)
        if set_path(p, str(target)) != 0:
            return False
        if args:
            _method(p, _ISL_SETARGUMENTS, ctypes.c_long,
                    wt.LPCWSTR)(p, str(args))
        if workdir:
            _method(p, _ISL_SETWORKINGDIRECTORY, ctypes.c_long,
                    wt.LPCWSTR)(p, str(workdir))
        if description:
            _method(p, _ISL_SETDESCRIPTION, ctypes.c_long,
                    wt.LPCWSTR)(p, str(description))
        if icon:
            _method(p, _ISL_SETICONLOCATION, ctypes.c_long, wt.LPCWSTR,
                    ctypes.c_int)(p, str(icon), int(icon_index))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        pf = ctypes.c_void_p()
        qi = _method(p, _ISL_QUERYINTERFACE, ctypes.c_long,
                     ctypes.c_void_p, ctypes.c_void_p)
        if qi(p, ctypes.byref(_IID_PERSISTFILE), ctypes.byref(pf)) != 0 or not pf:
            return False
        try:
            save = _method(pf, _PF_SAVE, ctypes.c_long, wt.LPCWSTR, wt.BOOL)
            return save(pf, str(path), True) == 0
        finally:
            _method(pf, _PF_RELEASE, ctypes.c_ulong)(pf)
    finally:
        if p:
            _method(p, _ISL_RELEASE, ctypes.c_ulong)(p)


def set_icon_location(path: Path | str, icon_file: Path | str,
                      index: int = 0) -> bool:
    """把 .lnk 的图标来源改成指定的 .ico（目标、参数、工作目录都原样保留）。"""
    p, pf = _open_lnk(path, STGM_READWRITE)
    if p is None:
        return False
    try:
        si = _method(p, _ISL_SETICONLOCATION, ctypes.c_long, wt.LPCWSTR, ctypes.c_int)
        if si(p, str(icon_file), int(index)) != 0:
            return False
        save = _method(pf, _PF_SAVE, ctypes.c_long, wt.LPCWSTR, wt.BOOL)
        return save(pf, str(path), True) == 0
    finally:
        _method(pf, _PF_RELEASE, ctypes.c_ulong)(pf)
        _method(p, _ISL_RELEASE, ctypes.c_ulong)(p)


def make_clsid_link(path: Path | str, clsid: str, description: str = "") -> bool:
    """给命名空间项（此电脑 / 回收站 …）生成快捷方式。

    不能用 SetPath——那要的是文件路径；命名空间项得先 `SHParseDisplayName`
    拿到 PIDL，再 `IShellLink::SetIDList`，最后 `IPersistFile::Save`。
    """
    if not _ensure_com():
        return False
    p = ctypes.c_void_p()
    pidl = ctypes.c_void_p()
    ok = False
    try:
        if ole32.CoCreateInstance(
                ctypes.byref(_CLSID_SHELLLINK), None, CLSCTX_INPROC_SERVER,
                ctypes.byref(_IID_SHELLLINKW), ctypes.byref(p)) != 0:
            return False
        attrs = wt.DWORD(0)
        if shell32.SHParseDisplayName("::" + clsid, None, ctypes.byref(pidl),
                                      0, ctypes.byref(attrs)) != 0 or not pidl:
            return False
        set_idlist = _method(p, _ISL_SETIDLIST, ctypes.c_long, ctypes.c_void_p)
        if set_idlist(p, pidl) != 0:
            return False
        if description:
            set_desc = _method(p, _ISL_SETDESCRIPTION, ctypes.c_long, wt.LPCWSTR)
            set_desc(p, description)
        pf = ctypes.c_void_p()
        qi = _method(p, _ISL_QUERYINTERFACE, ctypes.c_long,
                     ctypes.c_void_p, ctypes.c_void_p)
        if qi(p, ctypes.byref(_IID_PERSISTFILE), ctypes.byref(pf)) != 0 or not pf:
            return False
        try:
            save = _method(pf, _PF_SAVE, ctypes.c_long, wt.LPCWSTR, wt.BOOL)
            ok = save(pf, str(path), True) == 0
        finally:
            _method(pf, _PF_RELEASE, ctypes.c_ulong)(pf)
    finally:
        if pidl:
            ole32.CoTaskMemFree(pidl)
        if p:
            _method(p, _ISL_RELEASE, ctypes.c_ulong)(p)
    return ok
