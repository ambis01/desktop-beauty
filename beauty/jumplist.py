"""把「打开面板 / 启用桌面美化 / 退出」写进任务栏图标的右键跳转列表。

任务栏按钮右键出来的是 Windows 自己的跳转列表（原生毛玻璃、圆角、和系统
其他地方一模一样），里面能放什么完全由 `ICustomDestinationList` 决定。
这里用纯 ctypes 调 COM：

1. 在 `%APPDATA%\\DeskBeauty\\tasks\\` 里写三个 .lnk（就是列表里的三行）；
2. 给它们和任务栏那个快捷方式都打上同一个 AppUserModelID；
3. 用 `ICustomDestinationList` 把这三个 .lnk 注册成「任务」类别。

只要 AppID 对得上，任务栏图标（哪怕程序没在跑）右键就一定看得到这几项。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import uuid
from pathlib import Path

ole32 = ctypes.WinDLL("ole32")
shell32 = ctypes.WinDLL("shell32")

CLSCTX_INPROC_SERVER = 1
COINIT_APARTMENTTHREADED = 2
GPS_READWRITE = 2
VT_LPWSTR = 31
STGM_READWRITE = 2

CLSID_SHELLLINK = "{00021401-0000-0000-C000-000000000046}"
IID_ISHELLLINKW = "{000214F9-0000-0000-C000-000000000046}"
IID_IPERSISTFILE = "{0000010b-0000-0000-C000-000000000046}"
IID_ISHELLITEM = "{43826d1e-e718-42ee-bc55-a1e261c37bfe}"
IID_IOBJECTARRAY = "{92CA9DCD-5622-4bba-A805-5E9F541BD8C9}"
IID_IOBJECTCOLLECTION = "{5632b1a4-e38a-400a-928a-d4cd63230295}"
IID_IPROPERTYSTORE = "{886d8eeb-8cf2-4446-8d02-cdba1dbdcf99}"
CLSID_ENUMERABLE_OBJECT_COLLECTION = "{2d3468c1-36a7-43b6-ac24-d3f02fd9607a}"
CLSID_DESTINATION_LIST = "{77f10cf0-3db5-4966-b520-b7c54fd35ed6}"

PKEY_APPUSERMODEL_ID = ("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}", 5)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wt.DWORD)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort),
                ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("ptr", ctypes.c_void_p), ("high", ctypes.c_void_p)]


def _guid(text: str) -> GUID:
    g = GUID()
    ctypes.memmove(ctypes.byref(g), uuid.UUID(text).bytes_le, 16)
    return g


def _vt(ptr, index: int, restype, *argtypes):
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(vtbl[index])


def _release(ptr) -> None:
    if ptr:
        _vt(ptr, 2, ctypes.c_ulong)(ptr)


def _qi(ptr, iid_text: str):
    out = ctypes.c_void_p()
    ok = _vt(ptr, 0, ctypes.c_long, ctypes.POINTER(GUID),
             ctypes.POINTER(ctypes.c_void_p))(ptr, ctypes.byref(_guid(iid_text)),
                                              ctypes.byref(out))
    return out if ok == 0 and out.value else None


def _co_create(clsid: str, iid: str):
    out = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(ctypes.byref(_guid(clsid)), None, CLSCTX_INPROC_SERVER,
                                ctypes.byref(_guid(iid)), ctypes.byref(out))
    return (out, hr)


# ---------------------------------------------------------------- .lnk
def write_shortcut(path: Path, target: str, args: str = "", icon: str = "",
                   desc: str = "", workdir: str = "") -> bool:
    p = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(ctypes.byref(_guid(CLSID_SHELLLINK)), None,
                                CLSCTX_INPROC_SERVER,
                                ctypes.byref(_guid(IID_ISHELLLINKW)), ctypes.byref(p))
    if hr != 0 or not p:
        return False
    try:
        _vt(p, 20, ctypes.c_long, wt.LPCWSTR)(p, target)          # SetPath
        if args:
            _vt(p, 11, ctypes.c_long, wt.LPCWSTR)(p, args)        # SetArguments
        if desc:
            _vt(p, 7, ctypes.c_long, wt.LPCWSTR)(p, desc)         # SetDescription
        if workdir:
            _vt(p, 9, ctypes.c_long, wt.LPCWSTR)(p, workdir)      # SetWorkingDirectory
        if icon:
            _vt(p, 17, ctypes.c_long, wt.LPCWSTR, ctypes.c_int)(
                p, icon.split(",")[0], 0)                          # SetIconLocation
        pf = _qi(p, IID_IPERSISTFILE)
        if not pf:
            return False
        try:
            hr = _vt(pf, 6, ctypes.c_long, wt.LPCWSTR, wt.BOOL)(pf, str(path), True)
        finally:
            _release(pf)
        return hr == 0
    finally:
        _release(p)


def set_app_id(path: Path, app_id: str) -> bool:
    """给快捷方式打上 AppUserModelID；跳转列表就是按这个 ID 认人的。"""
    store = ctypes.c_void_p()
    hr = shell32.SHGetPropertyStoreFromParsingName(
        str(path), None, GPS_READWRITE, ctypes.byref(_guid(IID_IPROPERTYSTORE)),
        ctypes.byref(store))
    if hr != 0 or not store:
        return False
    try:
        key = PROPERTYKEY(_guid(PKEY_APPUSERMODEL_ID[0]), PKEY_APPUSERMODEL_ID[1])
        buf = ctypes.create_unicode_buffer(app_id)
        var = PROPVARIANT()
        var.vt = VT_LPWSTR
        var.ptr = ctypes.cast(buf, ctypes.c_void_p)
        hr = _vt(store, 6, ctypes.c_long, ctypes.POINTER(PROPERTYKEY),
                 ctypes.POINTER(PROPVARIANT))(store, ctypes.byref(key), ctypes.byref(var))
        if hr != 0:
            return False
        return _vt(store, 7, ctypes.c_long)(store) == 0   # Commit
    finally:
        _release(store)


# ---------------------------------------------------------------- 注册
def _shell_item(path: Path):
    item = ctypes.c_void_p()
    hr = shell32.SHCreateItemFromParsingName(str(path), None,
                                             ctypes.byref(_guid(IID_ISHELLITEM)),
                                             ctypes.byref(item))
    return item if hr == 0 and item else None


def register(app_id: str, tasks: list[Path]) -> int:
    """把 tasks 注册成 app_id 这个程序的「任务」类别。返回 HRESULT（0 = 成功）。"""
    items = []
    coll = ctypes.c_void_p()
    try:
        hr = ole32.CoCreateInstance(
            ctypes.byref(_guid(CLSID_ENUMERABLE_OBJECT_COLLECTION)), None,
            CLSCTX_INPROC_SERVER, ctypes.byref(_guid(IID_IOBJECTCOLLECTION)),
            ctypes.byref(coll))
        if hr != 0 or not coll:
            return hr
        add = _vt(coll, 5, ctypes.c_long, ctypes.c_void_p)       # AddObject
        for t in tasks:
            item = _shell_item(t)
            if not item:
                return -1
            items.append(item)
            if add(coll, item) != 0:
                return -2
        array = _qi(coll, IID_IOBJECTARRAY)
        if not array:
            return -3

        dl = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(_guid(CLSID_DESTINATION_LIST)), None,
            CLSCTX_INPROC_SERVER, ctypes.byref(_guid("{6332debf-87b5-4670-90c0-5e57b408a49e}")),
            ctypes.byref(dl))
        if hr != 0 or not dl:
            return hr if hr else -4
        try:
            if _vt(dl, 3, ctypes.c_long, wt.LPCWSTR)(dl, app_id) != 0:
                return -5
            removed = ctypes.c_void_p()
            min_slots = wt.UINT(0)
            # 注意参数顺序：pcMinSlots 在前，然后是 riid、ppv（MSDN 的 C++ 语法块
            # 和 SWT 的绑定都是这个顺序；写成 riid 在前会一直 E_NOINTERFACE）
            hr = _vt(dl, 4, ctypes.c_long, ctypes.POINTER(wt.UINT),
                     ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(
                dl, ctypes.byref(min_slots), ctypes.byref(_guid(IID_IOBJECTARRAY)),
                ctypes.byref(removed))
            _release(removed)
            if hr != 0:
                return hr
            hr = _vt(dl, 7, ctypes.c_long, ctypes.c_void_p)(dl, array)   # AddUserTasks
            if hr != 0:
                return hr
            return _vt(dl, 8, ctypes.c_long)(dl)                          # CommitList
        finally:
            _release(dl)
            _release(array)
    finally:
        for item in items:
            _release(item)
        _release(coll)
