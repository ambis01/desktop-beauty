"""盒子里图标的右键菜单：照 Windows 11 那张新菜单的样子自己画。

为什么要自己画：Win11 的新版右键菜单是 explorer.exe 自己的 XAML 界面，系统
只给第三方程序 IContextMenu 那套**老版**菜单（白底、条目一大堆）。所以这里
画一张结构一致的：最上面一排「剪切 / 复制 / 重命名 / 共享 / 删除」，中间是常用项
（显示哪些由系统实际有的动词决定，跟 Win11 一样没有的就不画），最后「显示更多
选项」——点它才弹系统原生那张完整菜单（7-Zip、Bandizip、QQ 发送到都在那儿）。

真正干活还是交给系统：剪切/复制/删除/属性/共享/复制文件地址全走
`shell_menu.invoke_verb()`，剪贴板、回收站、确认框都是系统行为。
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QWidget, QWidgetAction

from . import shell_menu
from .dlog import dlog
from .glass import GlassMenu

# Segoe MDL2 Assets 里的图标码位（Win10/11 都自带这个字体）
ICON_CUT = "\uE8C6"
ICON_COPY = "\uE8C8"
ICON_RENAME = "\uE8AC"
ICON_SHARE = "\uE72D"
ICON_DELETE = "\uE74D"
ICON_OPEN = "\uE8E5"
ICON_FOLDER = "\uE8B7"
ICON_LINK = "\uE71B"
ICON_INFO = "\uE946"
ICON_MORE = "\uE712"
ICON_SHIELD = "\uEA18"
ICON_PIN = "\uE840"

GLYPH_FONT = "Segoe MDL2 Assets"

# 排版照 Win11 那套：上面一排按钮窄一点、下面每行矮一点，整体不那么宽
CELL_W = 56
CELL_H = 54
ICON_TOP = 20            # 那排按钮里图标的大小
ICON_ROW = 16            # 下面菜单项里图标的大小

# 只对这张菜单生效的紧凑排版（全局 QSS 的 item 内边距是给别的菜单用的）
COMPACT_QSS = """
QMenu { padding: 6px; border-radius: 10px; }
QMenu::item { padding: 5px 12px 5px 10px; border-radius: 5px; }
QMenu::separator { height: 1px; margin: 4px 8px; }
"""


def _icon_color() -> QColor:
    return QColor(52, 62, 80)


def glyph_icon(char: str, size: int = ICON_ROW) -> QIcon:
    """把字体里的一个符号画成 QIcon（省得到处找 png）。"""
    pm = QPixmap(int(size * 2), int(size * 2))
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)
    f = QFont(GLYPH_FONT)
    f.setPixelSize(int(size * 2))
    p.setFont(f)
    p.setPen(_icon_color())
    p.drawText(pm.rect(), int(Qt.AlignCenter), char)
    p.end()
    pm.setDevicePixelRatio(2.0)
    return QIcon(pm)


class _QuickRow(QWidget):
    """最上面那排快捷按钮：图标在上、字在下，点哪颗就执行哪个。"""

    def __init__(self, buttons, on_pick, parent=None):
        super().__init__(parent)
        self._items = list(buttons)          # [(glyph, 文字, 命令名)]
        self._on_pick = on_pick
        self._hover = -1
        self.picked = ""
        self.setMouseTracking(True)
        self.setFixedHeight(CELL_H)
        self.setFixedWidth(CELL_W * max(1, len(self._items)) + 6)

    def _index_at(self, pos) -> int:
        if not self._items:
            return -1
        i = int((pos.x() - 3) // CELL_W)
        return i if 0 <= i < len(self._items) else -1

    def mouseMoveEvent(self, event) -> None:
        i = self._index_at(event.position())
        if i != self._hover:
            self._hover = i
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = -1
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        event.accept()
        i = self._index_at(event.position())
        if i < 0:
            return
        self.picked = self._items[i][2]
        self._on_pick(self.picked)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        for i, (glyph, label, _cmd) in enumerate(self._items):
            cell = QRectF(3 + i * CELL_W, 0, CELL_W, CELL_H)
            if i == self._hover:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(70, 100, 160, 34))
                p.drawRoundedRect(cell.adjusted(2, 2, -2, -2), 6.0, 6.0)
                p.setBrush(Qt.NoBrush)
            f = QFont(GLYPH_FONT)
            f.setPixelSize(ICON_TOP)
            p.setFont(f)
            p.setPen(_icon_color())
            p.drawText(QRectF(cell.left(), cell.top() + 4, cell.width(), ICON_TOP + 6),
                       int(Qt.AlignCenter), glyph)
            f2 = self.font()
            f2.setPixelSize(11)
            p.setFont(f2)
            p.setPen(QColor(44, 53, 70, 235))
            p.drawText(QRectF(cell.left(), cell.top() + ICON_TOP + 10,
                              cell.width(), 16),
                       int(Qt.AlignCenter), label)
        p.end()


def _add_row(menu: GlassMenu, text: str, glyph: str = "", hint: str = "") -> QAction:
    act = menu.addAction(text)
    if glyph:
        act.setIcon(glyph_icon(glyph))
    if hint:
        act.setProperty("hint", hint)
    return act


# (命令名, 文案, 图标, 快捷键提示, 哪类文件才有)
#   file = 所有东西 / exec = 可执行类 / pin = 能固定到开始屏幕的
# 这里**不问系统要动词**：QueryContextMenu 每问一次 460ms（冷启动 1700ms），
# 右键不能等那一下。按扩展名判断跟系统实际给的几乎一致，真不一样的时候
# 下面 run() 里还有一条「没执行成功就退回原生菜单」的兜底。
_ROWS = (
    ("open", "打开", ICON_OPEN, "Enter", "file"),
    ("share", "共享", ICON_SHARE, "", "file"),
    ("runas", "以管理员身份运行", ICON_SHIELD, "", "exec"),
    ("reveal", "打开文件所在位置", ICON_FOLDER, "", "file"),
    ("pin_start", "固定到“开始”屏幕", ICON_PIN, "", "pin"),
    ("copyaspath", "复制文件地址", ICON_LINK, "Ctrl+Shift+C", "file"),
    ("properties", "属性", ICON_INFO, "Alt+Enter", "file"),
)

_EXEC_EXT = {".exe", ".com", ".bat", ".cmd", ".msi", ".ps1", ".vbs", ".lnk", ".appref-ms"}
_PIN_EXT = {".exe", ".lnk", ".url", ".msi", ".appref-ms"}


def _rows_for(path) -> list[tuple[str, str, str, str]]:
    ext = str(path).lower()
    ext = ext[ext.rfind("."):] if "." in ext.rsplit("\\", 1)[-1] else ""
    out = []
    for cmd, label, glyph, hint, need in _ROWS:
        if need == "exec" and ext not in _EXEC_EXT:
            continue
        if need == "pin" and ext not in _PIN_EXT:
            continue
        out.append((cmd, label, glyph, hint))
    return out


def show_for_item(box, index: int, global_pos: QPoint) -> None:
    """在 (global_pos) 给盒子里的第 index 个图标弹新式右键菜单。"""
    path = box._items[index]
    hwnd = int(box.winId())

    def run(cmd: str) -> None:
        """菜单选完之后执行。系统动词自己会弹框，别在 exec 的嵌套循环里做。"""
        if cmd == "open":
            # 按**路径**启动：菜单开着的时候列表可能已经变了（拖进文件 / 合并 /
            # 文件监听刷新），按下标会打开另一个文件
            box._launch(path)
        elif cmd == "rename":
            box._rename_item(index)
        elif cmd == "more":
            shell_menu.show(hwnd, str(path), global_pos.x(), global_pos.y())
        elif cmd == "cut":
            shell_menu.copy_files(hwnd, [path], move=True)
        elif cmd == "copy":
            shell_menu.copy_files(hwnd, [path], move=False)
        elif cmd == "copyaspath":
            QApplication.clipboard().setText('"%s"' % path)
        elif cmd == "delete":
            shell_menu.delete_files(hwnd, [path])
        elif cmd == "properties":
            shell_menu.show_properties(hwnd, str(path))
        elif cmd == "reveal":
            shell_menu.open_containing(hwnd, str(path))
        elif cmd == "runas":
            if not shell_menu.run_verb(hwnd, str(path), "runas"):
                shell_menu.show(hwnd, str(path), global_pos.x(), global_pos.y())
        elif cmd in ("share", "pin_start"):
            # 这两条没有现成的注册表动词，只能回 IContextMenu 里翻
            verb = {"share": "Windows.ModernShare",
                    "pin_start": "pintohomefile"}.get(cmd, cmd)
            if not shell_menu.invoke_verb(hwnd, str(path), verb):
                dlog(f"item menu: 动词 {verb} 没执行成功，退回原生菜单")
                shell_menu.show(hwnd, str(path), global_pos.x(), global_pos.y())
        if cmd in ("cut", "copy", "delete", "rename", "copyaspath", "pin_start"):
            QTimer.singleShot(500, box.refresh)

    menu = GlassMenu(box)
    menu.setStyleSheet(COMPACT_QSS)
    menu.prepare_backdrop()

    quick = _QuickRow(
        [(ICON_CUT, "剪切", "cut"),
         (ICON_COPY, "复制", "copy"),
         (ICON_RENAME, "重命名", "rename"),
         (ICON_SHARE, "共享", "share"),
         (ICON_DELETE, "删除", "delete")],
        on_pick=lambda cmd: menu.close(),
    )
    wa = QWidgetAction(menu)
    wa.setDefaultWidget(quick)
    menu.addAction(wa)
    # QMenu 会按自己的动作宽度切 QWidgetAction 的控件：不给它一个下限，
    # 上面那排按钮会被挤到文字叠在一起
    menu.setMinimumWidth(quick.width() + 8)
    menu.addSeparator()

    made = {}
    for cmd, label, glyph, hint in _rows_for(path):
        if cmd in made:
            continue
        made[cmd] = _add_row(menu, label, glyph, hint)
    menu.addSeparator()
    act_more = _add_row(menu, "显示更多选项", ICON_MORE)

    chosen = menu.exec(global_pos)
    if quick.picked:
        run(quick.picked)
        return
    if chosen is None:
        return
    if chosen is act_more:
        run("more")
        return
    for cmd, act in made.items():
        if chosen is act:
            run(cmd)
            return
