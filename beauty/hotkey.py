"""全局快捷键：默认 Shift+Space 启停美化、Ctrl+Space 打开面板。

不用 `RegisterHotKey`：它要求挂在一个真实窗口上，`WM_HOTKEY` 还得靠 Qt 的
native event filter 接，结果时灵时不灵。这里直接 40ms 轮询 `GetAsyncKeyState`，
按下沿触发一次，简单可靠，也不需要装键盘钩子。

组合键在配置里存成 {"mods": 位掩码, "vk": 虚拟键码, "text": "Ctrl+Space"}，
面板的快捷键页负责改它们（PanelKeyCapture 把 Qt 的按键翻译成 VK）。
"""
from __future__ import annotations

import ctypes

from PySide6.QtCore import QObject, Qt, QTimer

user32 = ctypes.WinDLL("user32")
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]

VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_MENU = 0x12
VK_SPACE = 0x20

MOD_CONTROL = 0x0001
MOD_SHIFT = 0x0002
MOD_ALT = 0x0004
_MOD_VK = {MOD_CONTROL: VK_CONTROL, MOD_SHIFT: VK_SHIFT, MOD_ALT: VK_MENU}

# 默认两套组合键（面板里「恢复默认」也恢复成这个）
DEFAULT_TOGGLE = {"mods": MOD_SHIFT, "vk": VK_SPACE, "text": "Shift+Space"}
DEFAULT_PANEL = {"mods": MOD_CONTROL, "vk": VK_SPACE, "text": "Ctrl+Space"}

# 主键的显示名（没列到的字母/数字直接用字符）
_VK_NAMES = {
    0x20: "Space", 0x09: "Tab", 0x0D: "Enter", 0x1B: "Esc", 0x08: "Backspace",
    0x2D: "Insert", 0x2E: "Delete", 0x24: "Home", 0x23: "End",
    0x21: "PgUp", 0x22: "PgDn", 0x25: "←", 0x26: "↑", 0x27: "→", 0x28: "↓",
    0x2C: "PrtSc", 0x13: "Pause", 0xC0: "`", 0xBD: "-", 0xBB: "=",
    0xDB: "[", 0xDD: "]", 0xDC: "\\", 0xBA: ";", 0xDE: "'",
    0xBC: ",", 0xBE: ".", 0xBF: "/", 0x90: "NumLock",
}

# Qt 键 → Windows 虚拟键码（字母、数字直接用 ASCII）
_QT_VK = {
    Qt.Key_Space: 0x20, Qt.Key_Tab: 0x09, Qt.Key_Return: 0x0D,
    Qt.Key_Enter: 0x0D, Qt.Key_Escape: 0x1B, Qt.Key_Backspace: 0x08,
    Qt.Key_Insert: 0x2D, Qt.Key_Delete: 0x2E, Qt.Key_Home: 0x24,
    Qt.Key_End: 0x23, Qt.Key_PageUp: 0x21, Qt.Key_PageDown: 0x22,
    Qt.Key_Left: 0x25, Qt.Key_Up: 0x26, Qt.Key_Right: 0x27, Qt.Key_Down: 0x28,
    Qt.Key_Print: 0x2C, Qt.Key_Pause: 0x13, Qt.Key_QuoteLeft: 0xC0,
    Qt.Key_Minus: 0xBD, Qt.Key_Equal: 0xBB, Qt.Key_BracketLeft: 0xDB,
    Qt.Key_BracketRight: 0xDD, Qt.Key_Backslash: 0xDC, Qt.Key_Semicolon: 0xBA,
    Qt.Key_Apostrophe: 0xDE, Qt.Key_Comma: 0xBC, Qt.Key_Period: 0xBE,
    Qt.Key_Slash: 0xBF,
}


def qt_to_vk(key: int) -> int | None:
    """Qt 的按键码转 Windows VK；不认识的返回 None。"""
    if key in _QT_VK:
        return _QT_VK[key]
    if Qt.Key_A <= key <= Qt.Key_Z:
        return int(key)                     # 0x41..0x5A 和 VK_A..VK_Z 一致
    if Qt.Key_0 <= key <= Qt.Key_9:
        return int(key)                     # 0x30..0x39 一致
    if Qt.Key_F1 <= key <= Qt.Key_F24:
        return 0x70 + (int(key) - int(Qt.Key_F1))
    return None


def vk_name(vk: int) -> str:
    if vk in _VK_NAMES:
        return _VK_NAMES[vk]
    if 0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39:
        return chr(vk)
    if 0x70 <= vk <= 0x87:
        return f"F{vk - 0x6F}"
    return f"VK{vk:X}"


def hotkey_text(mods: int, vk: int) -> str:
    parts = []
    if mods & MOD_CONTROL:
        parts.append("Ctrl")
    if mods & MOD_SHIFT:
        parts.append("Shift")
    if mods & MOD_ALT:
        parts.append("Alt")
    parts.append(vk_name(vk))
    return "+".join(parts)


def normalize(raw, default: dict) -> dict:
    """配置里读出来的一套组合键：字段不对就退回默认值。"""
    try:
        mods = int(raw.get("mods", default["mods"]))
        vk = int(raw.get("vk", default["vk"]))
        if not (0 < mods < 8) or not (0 < vk < 0x100):
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        return dict(default)
    return {"mods": mods, "vk": vk, "text": hotkey_text(mods, vk)}


def _down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


class HotkeyWatcher(QObject):
    """轮询式全局快捷键。bind() 返回是否登记成功（永远 True，保留接口）。"""

    def __init__(self, parent=None, external: bool = False):
        super().__init__(parent)
        # (必须按住的修饰键, 必须松开的修饰键, 主键, 回调, 上一次是否按着)
        self._binds: list[tuple[list[int], list[int], int, object, bool]] = []
        self._external = bool(external)
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)

    def bind(self, modifiers: int, vk: int, callback) -> bool:
        """modifiers 之外的其他修饰键必须松开，避免 Ctrl+Shift+Space 同时触发两条。"""
        mods = [v for bit, v in _MOD_VK.items() if modifiers & bit]
        others = [v for bit, v in _MOD_VK.items() if not (modifiers & bit)]
        self._binds.append((mods, others, vk, callback, False))
        return True

    def install(self) -> None:
        if self._external:        # 由 app 那条合成轮询统一驱动
            return
        if not self._timer.isActive():
            self._timer.start()

    def release(self) -> None:
        self._timer.stop()
        self._binds.clear()

    def poll(self) -> None:
        if self._binds:
            self._tick()

    def _tick(self) -> None:
        for i, (mods, others, vk, cb, was) in enumerate(self._binds):
            try:
                now = (all(_down(m) for m in mods)
                       and all(not _down(o) for o in others)
                       and _down(vk))
            except OSError:
                continue
            if now and not was:
                try:
                    cb()
                except Exception:  # noqa: BLE001
                    pass
            self._binds[i] = (mods, others, vk, cb, now)
