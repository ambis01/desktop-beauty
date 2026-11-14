"""取大图标（256px）：收纳盒里只有 52px，用系统 32px 图标放大就会糊。

走的是资源管理器自己的图像列表（SHGetImageList / SHIL_JUMBO），
比 QFileIconProvider 拿到的 32px 图标清楚得多。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import uuid

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QImage, QPixmap

shell32 = ctypes.WinDLL("shell32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

SHGFI_SYSICONINDEX = 0x00004000
ILD_TRANSPARENT = 0x1
SHIL_JUMBO = 4          # 256px
SHIL_EXTRALARGE = 2     # 48px


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


class _SHFILEINFOW(ctypes.Structure):
    _fields_ = [("hIcon", wt.HICON), ("iIcon", ctypes.c_int), ("dwAttributes", wt.DWORD),
                ("szDisplayName", ctypes.c_wchar * 260), ("szTypeName", ctypes.c_wchar * 80)]


class _ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wt.BOOL), ("xHotspot", wt.DWORD), ("yHotspot", wt.DWORD),
                ("hbmMask", wt.HBITMAP), ("hbmColor", wt.HBITMAP)]


class _BITMAP(ctypes.Structure):
    _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long),
                ("bmHeight", ctypes.c_long), ("bmWidthBytes", ctypes.c_long),
                ("bmPlanes", ctypes.c_ushort), ("bmBitsPixel", ctypes.c_ushort),
                ("bmBits", ctypes.c_void_p)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


_IID_IMAGELIST = _GUID()
ctypes.memmove(ctypes.byref(_IID_IMAGELIST),
               uuid.UUID("{46EB5926-582E-4017-9FDF-E8998DAA0950}").bytes_le, 16)

shell32.SHGetFileInfoW.restype = ctypes.c_size_t
shell32.SHGetFileInfoW.argtypes = [wt.LPCWSTR, wt.DWORD, ctypes.POINTER(_SHFILEINFOW),
                                   wt.UINT, ctypes.c_void_p]
shell32.SHGetImageList.restype = ctypes.c_long
shell32.SHGetImageList.argtypes = [ctypes.c_int, ctypes.POINTER(_GUID),
                                   ctypes.POINTER(ctypes.c_void_p)]
user32.GetIconInfo.argtypes = [wt.HICON, ctypes.POINTER(_ICONINFO)]
user32.DestroyIcon.argtypes = [wt.HICON]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.GetObjectW.restype = ctypes.c_int
gdi32.GetObjectW.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p]
gdi32.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p,
                            ctypes.c_void_p, wt.UINT]
gdi32.DeleteObject.argtypes = [wt.HANDLE]
gdi32.DeleteDC.argtypes = [wt.HDC]


_IMAGELISTS: dict[int, object] = {}
_INDEX_CACHE: dict[str, int] = {}


def _sys_index(path: str) -> int:
    hit = _INDEX_CACHE.get(path)
    if hit is not None:
        return hit
    info = _SHFILEINFOW()
    ok = shell32.SHGetFileInfoW(path, 0, ctypes.byref(info), ctypes.sizeof(info),
                                SHGFI_SYSICONINDEX)
    idx = info.iIcon if ok else -1
    _INDEX_CACHE[path] = idx
    return idx


def _candidates_image(path: str) -> QImage | None:
    """给一个候选源取图标（后台线程里调）。"""
    return ink_image(path)


def best_image(paths) -> QImage | None:
    """从几个候选源里挑最清楚的那张图标（裁过透明边）。

    取图标会调 Windows 外壳，一次可能上百毫秒——**只能在工作线程里调**，
    别在 paintEvent 里干这事（盒子里十几个图标就能把界面卡死）。
    """
    best = None
    for cand in paths:
        img = ink_image(str(cand))
        if img is None or img.isNull():
            continue
        if best is None or img.width() > best.width():
            best = img
    return best


def _image_list(shil: int):
    hit = _IMAGELISTS.get(shil)
    if hit is not None:
        return hit or None
    p = ctypes.c_void_p()
    if shell32.SHGetImageList(shil, ctypes.byref(_IID_IMAGELIST), ctypes.byref(p)) != 0:
        _IMAGELISTS[shil] = 0
        return None
    _IMAGELISTS[shil] = p if p else 0
    return p if p else None


def _get_icon(piml, index: int):
    vtbl = ctypes.cast(piml, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    get_icon = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_int, wt.UINT,
                                  ctypes.POINTER(wt.HICON))(vtbl[10])
    h = wt.HICON()
    if get_icon(piml, index, ILD_TRANSPARENT, ctypes.byref(h)) != 0 or not h.value:
        return None
    return h


def _hicon_to_image(hicon) -> QImage | None:
    info = _ICONINFO()
    if not user32.GetIconInfo(hicon, ctypes.byref(info)):
        return None
    try:
        bm = _BITMAP()
        if not gdi32.GetObjectW(info.hbmColor, ctypes.sizeof(bm), ctypes.byref(bm)):
            return None
        w, h = int(bm.bmWidth), int(bm.bmHeight)
        if w <= 0 or h <= 0:
            return None
        bi = _BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bi.biWidth = w
        bi.biHeight = -h
        bi.biPlanes = 1
        bi.biBitCount = 32
        bi.biCompression = 0  # BI_RGB
        buf = ctypes.create_string_buffer(w * h * 4)
        hdc = gdi32.CreateCompatibleDC(None)
        try:
            if gdi32.GetDIBits(hdc, info.hbmColor, 0, h, buf,
                               ctypes.byref(bi), 0) == 0:
                return None
        finally:
            gdi32.DeleteDC(hdc)
        return QImage(buf.raw, w, h, w * 4, QImage.Format_ARGB32).copy()
    finally:
        if info.hbmColor:
            gdi32.DeleteObject(info.hbmColor)
        if info.hbmMask:
            gdi32.DeleteObject(info.hbmMask)


def raw_image(path: str) -> QImage | None:
    """按路径取系统图标（尽量 256px），返回**原始尺寸**的图，不做缩放。

    调用方靠它的 width() 判断这份图标到底有多清楚：.url / 某些 .lnk 只能拿到
    16–32px 的图，放大会糊成一团，得换别的候选源。
    """
    if not path:
        return None
    index = _sys_index(path)
    if index < 0:
        return None
    for shil in (SHIL_JUMBO, SHIL_EXTRALARGE):
        piml = _image_list(shil)
        if not piml:
            continue
        hicon = _get_icon(piml, index)
        if not hicon:
            continue
        try:
            img = _hicon_to_image(hicon)
        finally:
            user32.DestroyIcon(hicon)
        if img is None or img.isNull():
            continue
        return img
    return None


def big_pixmap(path: str, size: int) -> QPixmap | None:
    """尽量拿 256px 的图标再缩到 size；拿不到返回 None。"""
    img = ink_image(path)
    if img is None:
        return None
    pm = QPixmap.fromImage(img).scaled(size, size, Qt.KeepAspectRatio,
                                       Qt.SmoothTransformation)
    if pm.isNull():
        return None
    pm.setDevicePixelRatio(1.0)
    return pm


def _ink_bbox(img: QImage) -> QRect | None:
    """找出真正有内容的矩形。

    有些图标（比如无畏契约、底特律的 .ico）在 256×256 的画布里只占中间一个
    32px 的小图，四周全是透明——直接缩到格子大小，画出来就是一个小点。
    这里按 alpha 找边界，把空白裁掉，再缩到格子大小才是正常的图标大小。
    用 32×32 降采样来找边界：够准，而且不用逐像素扫 256×256。
    """
    small = img.scaled(32, 32, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    xs: list[int] = []
    ys: list[int] = []
    for y in range(32):
        for x in range(32):
            if small.pixelColor(x, y).alpha() > 6:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    sx = img.width() / 32.0
    sy = img.height() / 32.0
    x0 = max(0, int(min(xs) * sx) - 2)
    y0 = max(0, int(min(ys) * sy) - 2)
    x1 = min(img.width(), int((max(xs) + 1) * sx) + 2)
    y1 = min(img.height(), int((max(ys) + 1) * sy) + 2)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    return QRect(x0, y0, x1 - x0, y1 - y0)


def ink_image(path: str) -> QImage | None:
    """取图标并裁掉四周透明边（结果尺寸 = 真正有内容的尺寸）。"""
    img = raw_image(path)
    if img is None or img.isNull():
        return None
    img = img.convertToFormat(QImage.Format_ARGB32)
    bbox = _ink_bbox(img)
    return img.copy(bbox) if bbox is not None else img


def write_ico(img: QImage, path) -> bool:
    """把一张图写成一个 .ico（单张 PNG 压缩条目，Vista 以后都认）。"""
    import struct

    from PySide6.QtCore import QBuffer, QIODevice

    if img is None or img.isNull():
        return False
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    if not img.save(buf, "PNG"):
        return False
    png = bytes(buf.data())
    buf.close()
    w = 0 if img.width() >= 256 else img.width()
    h = 0 if img.height() >= 256 else img.height()
    head = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(png), 22)
    try:
        with open(str(path), "wb") as fh:
            fh.write(head + entry + png)
    except OSError:
        return False
    return True


def _replace_with_fixed(path, ico) -> bool:
    """改写不了原文件时的绕路：复制一份 → 改副本 → 删原文件 → 挪回来。

    临时文件必须和目标同一个目录（同一卷），rename 才是原子的。
    """
    import os
    import shutil

    from . import shortcut

    tmp = path.with_name(f"~db-{path.name}")
    try:
        if tmp.exists():
            tmp.unlink()
        shutil.copyfile(str(path), str(tmp))
    except OSError:
        return False
    if not shortcut.set_icon_location(tmp, ico, 0):
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    try:
        path.unlink()      # 老文件正好有「删除」权限
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    try:
        os.rename(str(tmp), str(path))
    except OSError:
        return False
    return True


def fix_shortcut_icon(lnk) -> bool:
    """给「图标全靠目标 exe」的 .lnk 写一个明确的图标来源。

    用户报的 bug：NVIDIA App 拖出收纳盒之后，桌面图标变成一张白纸。
    实测原因是这类快捷方式没有写死 IconLocation，explorer 自己那套「从目标 exe
    取图标」的路径会失败（同一个文件拷一份新的也一样白），但只要给 .lnk 指一个
    明确的 .ico 就立刻正常。所以这里用我们自己能取到的 256px 图生成一份 .ico，
    再把快捷方式指过去（目标/参数/工作目录都不动）。
    """
    from pathlib import Path

    from . import shortcut
    from .config import DATA_DIR

    try:
        p = Path(str(lnk))
        if p.suffix.lower() != ".lnk" or not p.is_file():
            return False
        loc = shortcut.icon_location(p)
        if loc is None:
            return False
        icon_file, _index = loc
        if icon_file and Path(icon_file).exists():
            return False      # 已经写死了图标，别动用户的设置
        target = shortcut.target_of(p)
        img = raw_image(str(target) if target else str(p))
        if img is None or img.isNull() or img.width() < 32:
            return False
        import hashlib
        tag = hashlib.sha1(str(str(target) or str(p)).lower().encode("utf-8"))
        folder = DATA_DIR / "icons" / "lnk"
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        out = folder / f"{tag.hexdigest()[:16]}.ico"
        if not out.exists() and not write_ico(img, out):
            return False
        if not shortcut.set_icon_location(p, out, 0):
            # 管理员装出来的快捷方式，普通用户只有「删除」权限，内容改不了。
            # 那就照内容复制一份（新文件继承桌面目录的权限）→ 删原文件 → 挪回来。
            if not _replace_with_fixed(p, out):
                return False
        try:
            from . import desktop_icons as _di
            _di.notify_updated(p)
        except Exception:  # noqa: BLE001
            pass
        return True
    except Exception:  # noqa: BLE001 - 图标修补失败绝不能影响放回桌面
        return False

