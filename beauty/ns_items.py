"""系统图标（此电脑 / 回收站 / 控制面板 / 网络）不是文件，是 explorer 的命名空间项。

它们没法被"移动"，所以收纳的做法是：

1. 在盒子里生成一个指向该 CLSID 的快捷方式（`IShellLink::SetIDList`）
2. 把这个系统图标从桌面上**隐藏**（等价于「桌面图标设置」里取消勾选），
   写 `HKCU\\...\\Explorer\\HideDesktopIcons\\NewStartPanel\\{CLSID} = 1`

放回桌面时再取消隐藏。registry 用的 `winreg`，比 ctypes 干净。
"""
from __future__ import annotations

import ctypes
import uuid
import winreg

ADVANCED = r"Software\Microsoft\Windows\CurrentVersion\Explorer\HideDesktopIcons\NewStartPanel"

# 显示名 → CLSID。别名都转小写比对（explorer 的中文名偶尔带空格）
ITEMS: list[tuple[str, str]] = [
    ("此电脑", "{20D04FE0-3AEA-1069-A2D8-08002B30309D}"),
    ("回收站", "{645FF040-5081-101B-9F08-00AA002F954E}"),
    ("控制面板", "{5399E694-6CE5-4D6C-8FCE-1D8870FDCBA0}"),
    ("网络", "{F02C1A0D-BE21-4350-88B0-7367FC96EF3C}"),
]

ALIASES = {
    "此电脑": "此电脑", "我的电脑": "此电脑", "这台电脑": "此电脑",
    "this pc": "此电脑", "computer": "此电脑",
    "回收站": "回收站", "recycle bin": "回收站",
    "控制面板": "控制面板", "control panel": "控制面板",
    "网络": "网络", "network": "网络",
}

_BY_NAME = {name: clsid for name, clsid in ITEMS}
_BY_CLSID = {clsid: name for name, clsid in ITEMS}

# 这几个系统图标**不许**进收纳盒：
# 回收站要留在桌面上单独用——它被收进盒子（桌面图标被隐藏）之后，
# 「把文件拖到回收站」这个操作就没有地方可拖了，用户明确要求保留。
RECYCLE_BIN = "{645FF040-5081-101B-9F08-00AA002F954E}"
EXCLUDED = (RECYCLE_BIN,)


def is_excluded(clsid: str) -> bool:
    key = str(clsid or "").upper()
    return any(key == c.upper() for c in EXCLUDED)


def _canonical(name: str) -> str | None:
    key = (name or "").strip().lower()
    return ALIASES.get(key)


def clsid_for(name: str) -> str | None:
    canon = _canonical(name)
    return _BY_NAME.get(canon) if canon else None


def name_for(clsid: str) -> str:
    return _BY_CLSID.get(clsid, "系统图标")


def pseudo(clsid: str) -> str:
    """在合并流程里代表一个命名空间项的假路径。"""
    return "ns::" + clsid


def is_pseudo(path: str) -> bool:
    return bool(path) and str(path).startswith("ns::")


def clsid_of(path: str) -> str:
    return str(path)[4:]


def is_hidden(clsid: str) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ADVANCED) as key:
            value, _ = winreg.QueryValueEx(key, clsid)
            return bool(value)
    except OSError:
        return False


def set_hidden(clsid: str, hidden: bool) -> bool:
    """隐藏 / 恢复桌面上的这个系统图标。"""
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, ADVANCED, 0,
                                winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
            winreg.SetValueEx(key, clsid, 0, winreg.REG_DWORD, 1 if hidden else 0)
    except OSError:
        return False
    refresh_desktop()
    return True


def refresh_desktop() -> None:
    """让桌面重新读一遍「显示哪些系统图标」。"""
    try:
        shell32 = ctypes.WinDLL("shell32")
        # SHCNE_ASSOCCHANGED / SHCNF_IDLIST：让 shell 重读设置（桌面图标当场增减）
        shell32.SHChangeNotify(0x08000000, 0, None, None)
    except OSError:
        pass


def clsid_from_pidl(raw: bytes) -> str | None:
    """从 shell 的 pidl 原始字节里认出这是哪个系统图标。

    不解析 pidl 结构，也不调 COM：命名空间项的 pidl 里就是一段标准 GUID
    （`GUID` 内存布局 = `bytes_le`），拿四个已知 CLSID 挨个比对就够了。
    这样「把此电脑拖进盒子」不用走 IShellItem，省掉一大截 vtable。
    """
    if not raw:
        return None
    for _name, clsid in ITEMS:
        try:
            key = uuid.UUID(clsid).bytes_le
        except ValueError:
            continue
        if key in raw:
            return clsid
    return None
