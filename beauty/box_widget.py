"""毛玻璃收纳盒窗口：收起时 2×2 缩略图，点一下在原地放大成图标网格。"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import math
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve, QEvent, QFileInfo, QMimeData, QObject, QPoint, QPointF, QRect,
    QRectF, QSize, Qt, QTimer, QUrl, QVariantAnimation, QFileSystemWatcher, Signal,
)
from PySide6.QtGui import (
    QColor, QCursor, QDrag, QFont, QFontMetrics, QPainter, QPainterPath, QPen,
    QPixmap, QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication, QFileIconProvider, QInputDialog, QLineEdit, QMenu,
)

from .glass import GlassMenu, GlassWindow, HOVER_GROW, HOVER_MS, modal
from .i18n import tr
from .shortcut import target_of
from . import desktop_icons as di
from . import aligner
from . import groupmove
from . import icons
from . import ns_items
from . import screen

COLLAPSED = 96
# ---------------------------------------------------------------- 自定义指针
# 和 DSH 雾玻璃插件同款的「圆点 + 光晕 + 拖影」，颜色换成灰的。
# 只在鼠标位于收纳盒窗口里时生效：系统光标换成 BlankCursor，自己画圆点。
CURSOR_DOT = 12.0
CURSOR_HALO = 24.0
CURSOR_TRAIL = 6
CURSOR_CORE = QColor(104, 110, 120, 242)
CURSOR_GLOW = QColor(104, 110, 120, 100)
CURSOR_TRAIL_C = QColor(104, 110, 120, 180)
COLS = 4
CELL_W = 74
CELL_H = 92
PAD = 16
HEADER = 40
MINI = 34
MINI_GAP = 10
MINI_PAD = 16          # 收起时缩略图四周的留白（2×2 时合计 110，比原来大一圈，名字放得下）
MAX_MINI_COLS = 5      # 收起最多 5×5 缩略图（25 个图标），再多就不画了
# 三点菜单里的「大小」：auto = 跟着图标数量长（现在的行为），其余是固定格子
# ("列, 行")：4×2 是横条、2×4 是竖条
SIZE_CHOICES = ("auto", "2x2", "3x3", "4x4", "5x5", "4x2", "2x4")
_FIXED_GRID = {
    "2x2": (2, 2), "3x3": (3, 3), "4x4": (4, 4), "5x5": (5, 5),
    "4x2": (4, 2), "2x4": (2, 4),
}
GAP = 8                # 拖动结束时跟别的盒子/卡片之间留的距离
TITLE_H = 28           # 标题栏高度：左名称、右三点、中间拖动横杠
DOT_W = 26             # 三点按钮的点击宽度
GRIP_W = 46            # 中间可拖动区域的宽度（横杠就画在这儿）
GRIP_MIN = 40          # 再窄也要给拖动区留这么多
GRIP_H = 3             # 横杠粗细
SLIDE_MS = 180         # 换位时图标滑到新格子的时间
# 有盒子的窗口正在被拖动时置 True：这时别去 grab 兄弟窗口做模糊，
# 否则每 700ms 一次的抓图会让拖动一顿一顿的（实测每个兄弟窗口 grab 要几 ms）。
_DRAGGING = False
# 这一次拖拽里被别的收纳盒接走的文件（旧路径）。源盒子要靠它判断「图标是
# 有人接走了，还是落在桌面上」，否则会白等一秒去找桌面副本。
_TAKEN: set[str] = set()


def is_dragging() -> bool:
    """Controller 用：正在拖盒子的时候别重排窗口层级（会把被拖的压回去）。"""
    return _DRAGGING


def set_dragging(on: bool) -> None:
    """卡片拖动时也置位。

    这个开关不只给盒子自己用：展开的盒子每 700ms 会把压住它的窗口 `grab()`
    下来做模糊（`_refresh_backdrop`），`apply_z_order` 也要跳过重排。以前只有
    「拖盒子」会置位，所以**拖卡片的时候盒子照样在抓图**——卡片正好停在展开的
    盒子上方时，每 700ms 就是一次几毫秒到几十毫秒的顿。
    """
    global _DRAGGING
    _DRAGGING = bool(on)


# 图标缓存跨实例共享：盒子重建、换尺寸都不用再问一遍 Windows 外壳
_ICON_CACHE: dict[tuple[str, int], object] = {}
_EMPTY_PM = None


def _empty_pm():
    """1×1 透明图：图标还没取到时先画它（比空 QPixmap 安全，不会刷警告）。"""
    global _EMPTY_PM
    if _EMPTY_PM is None:
        _EMPTY_PM = QPixmap(1, 1)
        _EMPTY_PM.fill(Qt.transparent)
    return _EMPTY_PM


def _icon_candidates(path) -> list[str]:
    """同一个图标可能有几个来源：.lnk 的目标 exe、.url 里写的 IconFile、快捷方式本身。

    ⚠️ 里面 `target_of` / 读 .url 都是文件/外壳操作（实测能到 120ms+），
    **只能在后台线程调**，不许在 paintEvent 里碰。

    ⚠️ 参数可能是 `Path` 也可能是 `str`（后台线程收到的是 str）：以前
    `_url_icon_file` 里写的是 `path.read_bytes()`，str 上来就 AttributeError，
    被这里 `except` 吞掉 ⇒ 所有 .url 都只剩通用地球图标（用户报的「两个游戏
    图标变成地球」就是这么来的）。所以这里统一先转成 Path。
    """
    out: list[str] = []
    path = Path(path)
    low = str(path).lower()
    if low.endswith(".lnk"):
        try:
            targets = target_of(path)   # .lnk 直接取图标会带小箭头
        except Exception as exc:  # noqa: BLE001
            _log_force("icon candidates: target_of failed %s (%r)", path, exc)
            targets = None
        if targets is not None:
            out.append(str(targets))
    elif low.endswith(".url"):
        try:
            icon_file = _url_icon_file(path)
        except Exception as exc:  # noqa: BLE001
            _log_force("icon candidates: url icon failed %s (%r)", path, exc)
            icon_file = None
        if icon_file:
            out.append(icon_file)
    out.append(str(path))
    return out


class _IconLoader(QObject):
    """后台线程取图标。

    取一个图标要调 Windows 外壳（`SHGetFileInfo` + 系统图像列表），实测单个
    能到 120ms 以上；十几个图标串起来就是「桌面一直一顿一顿」。所以全部挪到
    这条线程里，主线程只收结果。
    """

    done = Signal(str, int, object)     # 候选键, 尺寸, QImage 或 None

    def __init__(self):
        super().__init__()
        self._q: queue.Queue = queue.Queue()
        self._busy: set[tuple[str, int]] = set()
        threading.Thread(target=self._run, daemon=True,
                         name="icon-loader").start()

    def submit(self, key: tuple[str, int], path: str) -> None:
        if key in self._busy:
            return
        self._busy.add(key)
        self._q.put((key, path))

    def _run(self) -> None:
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 0x2)   # STA：外壳调用的前提
        except OSError:
            pass
        while True:
            key, path = self._q.get()
            img = None
            try:
                img = icons.best_image(_icon_candidates(path))
            except Exception:  # noqa: BLE001 - 一个图标失败不能拖垮线程
                img = None
            try:
                self.done.emit(key[0], key[1], img)
            except RuntimeError:      # 窗口都销毁了
                pass
            self._busy.discard(key)


_LOADER: _IconLoader | None = None


def icon_loader() -> _IconLoader:
    global _LOADER
    if _LOADER is None:
        _LOADER = _IconLoader()
    return _LOADER
HOVER_TOTAL = (HOVER_MS * 2 + 40) / 1000.0

user32 = ctypes.WinDLL("user32")
user32.FindWindowW.restype = wt.HWND
user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
user32.GetTopWindow.restype = wt.HWND
user32.GetWindow.restype = wt.HWND
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
VK_LBUTTON = 0x01
user32.SetWindowPos.argtypes = [
    wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT,
]
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_NOOWNERZORDER = 0x0200

shell32 = ctypes.WinDLL("shell32", use_last_error=True)


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wt.HWND),
        ("wFunc", wt.UINT),
        ("pFrom", wt.LPCWSTR),
        ("pTo", wt.LPCWSTR),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wt.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wt.LPCWSTR),
    ]


def _recycle(path: Path) -> bool:
    """删到回收站（可撤销），而不是直接删文件。"""
    op = _SHFILEOPSTRUCTW()
    op.wFunc = 3                       # FO_DELETE
    op.pFrom = str(path) + "\0\0"      # 双 \0 结尾
    op.fFlags = 0x0040 | 0x0010 | 0x0004   # ALLOWUNDO | NOCONFIRMATION | SILENT
    try:
        return shell32.SHFileOperationW(ctypes.byref(op)) == 0
    except OSError:
        return False


def _url_icon_file(path: Path | str) -> str | None:
    """.url（Steam 游戏那种）里写着 IconFile=，那才是真正的图标。

    直接让 shell 按 .url 取图标，只会给一个 16px 的小地球——盒子里放大之后
    就是一团糊。
    """
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        _log_force("url icon: read failed %s (%s)", path, exc)
        return None
    for enc in ("utf-8", "mbcs", "utf-16"):
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        for line in text.splitlines():
            if line.lower().startswith("iconfile="):
                cand = line.split("=", 1)[1].strip().strip('"')
                if cand and Path(cand).exists():
                    return cand
                _log_force("url icon: IconFile missing %s -> %s", path, cand)
        break
    return None



def _cidl_items(raw: bytes) -> list[bytes]:
    """拆 `CFSTR_SHELLIDLIST`（CIDA）里的每个**子** pidl 字节。

    CIDA = `UINT cidl` + `UINT aoffset[cidl+1]`；`aoffset[0]` 指向父 pidl，
    `aoffset[1..cidl]` 是各个子项。子项通常是「相对父项」的 pidl，但系统图标
    那一段 CLSID（GUID）就在里面，够我们认出是哪个系统图标了。
    """
    if len(raw) < 8:
        return []
    cidl = int.from_bytes(raw[0:4], "little")
    if not 0 < cidl <= 64:
        return []
    out: list[bytes] = []
    for i in range(1, cidl + 1):
        pos = 4 + 4 * i
        if pos + 4 > len(raw):
            break
        off = int.from_bytes(raw[pos:pos + 4], "little")
        if off < 4 or off >= len(raw):
            continue
        # 一个 pidl 由若干 ITEMID 组成，每项头 2 字节是 cb，cb=0 收尾
        j = off
        while j + 2 <= len(raw):
            cb = int.from_bytes(raw[j:j + 2], "little")
            if cb == 0:
                j += 2
                break
            j += cb
        out.append(raw[off:min(j, len(raw))])
    return out


def _elide(text: str, font: QFont, width: int) -> str:
    return QFontMetrics(font).elidedText(text, Qt.ElideRight, width)


def _log(msg: str, *args) -> None:
    if not os.environ.get("DESKBEAUTY_DEBUG"):
        return
    try:
        path = os.path.join(os.environ.get("TEMP", "."), "deskbeauty-box.log")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} {msg % args if args else msg}\n")
    except OSError:
        pass


def _log_force(msg: str, *args) -> None:
    """不受 DEBUG 开关限制的日志：专门用来抓拖动性能数据。"""
    try:
        path = os.path.join(os.environ.get("TEMP", "."), "deskbeauty-box.log")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} {msg % args if args else msg}\n")
    except OSError:
        pass


# 放回桌面时「等图标出现」的耐心。explorer 的桌面视图批量刷新实测 485–1028ms，
# 毁灭世界一次放回十几个更慢；原来只等 60×8 = 480ms 就放弃，放弃之后图标就停在
# explorer 给的默认格子（桌面左上角第一格）——用户报的「直接跑到左上角」。
_PLACE_WAIT_MS = 12
_PLACE_TRIES = 240              # ≈ 2.9s
_place_pending = 0              # 还有几个图标在等落位（卸载时要等它归零再退）


def place_pending() -> int:
    """还有多少个图标在等 explorer 把它们画出来（`wipe_and_quit` 用它收尾）。"""
    return _place_pending


def _place_icon_at(name: str, sx: int, sy: int, tries: int = 0) -> None:
    """把刚放回桌面的图标摆到指定屏幕位置（图标中心对准该点），不找格子。

    explorer 建图标需要一点时间（已经用 SHChangeNotify 催过了），找不到就隔
    12ms 再看，最多等约 2.9 秒。**放弃时图标会留在 explorer 默认的左上角**，
    所以宁可多等一会儿。
    """
    global _place_pending
    if tries == 0:
        _place_pending += 1
    try:
        try:
            icons = di.list_icons()
        except Exception:  # noqa: BLE001
            icons = []
        low = name.strip().lower()
        base = low.rsplit(".", 1)[0]
        # 桌面显示名可能带扩展名也可能不带（取决于「隐藏已知文件类型的扩展名」）：
        # 名字和去后缀两种都认。调用方传**完整文件名**（`p.name`），这样两种都对得上。
        hit = next((i for i in icons
                    if i.name.strip().lower() in (low, base)), None)
        if hit is None:
            if tries < _PLACE_TRIES:
                QTimer.singleShot(
                    _PLACE_WAIT_MS, lambda: _place_icon_at(name, sx, sy, tries + 1))
            else:
                _log("place give up (icon never showed): %s", name)
                _place_pending = max(0, _place_pending - 1)
            return
        try:
            off = di.cell_center_offset()
            # 放到「离这个点最近的空格子」：不让 explorer 自己把孩子塞到别处
            di.place_in_slot(hit.index, sx, sy, offset=off)
            _log("restored %s placed at %s,%s", name, sx, sy)
            QTimer.singleShot(160, lambda: _verify_icon_at(name, sx, sy))
        except Exception:  # noqa: BLE001
            pass
        _place_pending = max(0, _place_pending - 1)
    except Exception:  # noqa: BLE001 - 收尾别把异常吞成「一直 pending」
        _place_pending = max(0, _place_pending - 1)
        raise


def _verify_icon_at(name: str, sx: int, sy: int, tries: int = 0) -> None:
    """再确认一次：图标要是被 explorer 挪到别处（常见就是左上角）就再放一遍。"""
    try:
        icons = di.list_icons()
    except Exception:  # noqa: BLE001
        return
    low = name.strip().lower()
    base = low.rsplit(".", 1)[0]
    # 桌面显示名通常是去掉扩展名的（.lnk/.url/.txt 都可能被隐藏），两种都认
    hit = next((i for i in icons
                if i.name.strip().lower() in (low, base)), None)
    if hit is None:
        return
    r = di.item_rect(hit.index)
    if r is None:
        return
    cx, cy = (r[0] + r[2]) / 2.0, (r[1] + r[3]) / 2.0
    if abs(cx - sx) <= 140 and abs(cy - sy) <= 140:
        return
    if tries < 3:
        try:
            di.place_in_slot(hit.index, sx, sy, offset=di.cell_center_offset())
            _log("re-placed %s at %s,%s (was %s,%s)", name, sx, sy, cx, cy)
        except Exception:  # noqa: BLE001
            pass
        QTimer.singleShot(160, lambda: _verify_icon_at(name, sx, sy, tries + 1))


def _land_restored(paths: list[Path], drop: QPoint, tries: int = 0) -> None:
    """把刚从盒子里拖到桌面上的图标「当场」放到松手的位置。

    explorer 把文件搬到桌面目录之后，桌面图标要攒着批量刷新，半秒到一秒才
    冒出来（实测 485–1028ms）。这里自己发 SHChangeNotify(SHCNE_CREATE) 催它
    立刻建图标（4–35ms），再摆到松手点。
    """
    landed: list[Path] = []
    for path in paths:
        hit = di.find_desktop_copy(path.name)
        if hit is None:
            continue   # 被拖进文件夹 / 回收站 / 拖拽取消：不归我们管
        # 先修图标再通知：快捷方式没写死图标时 explorer 会画成一张白纸
        # （NVIDIA App 必现），给它指一个明确的 .ico 就正常了。
        icons.fix_shortcut_icon(hit)
        di.notify_created(hit)
        # .lnk 从盒子目录搬回桌面时，explorer 偶尔按旧路径缓存画成一张白纸，
        # 再发一次「这一项变了」，逼它重新读图标
        di.notify_updated(hit)
        landed.append(hit)
    if not landed:
        # explorer 偶尔比 DoDragDrop 慢半拍才把文件落到桌面目录，等一会儿再看
        if tries < 25:
            QTimer.singleShot(
                40, lambda: _land_restored(paths, drop, tries + 1))
        return
    px, py = screen.to_physical((drop.x(), drop.y(), drop.x(), drop.y()))[0:2]
    cell = di.item_spacing()
    step = cell[0] if cell and cell[0] > 0 else 80
    for i, path in enumerate(landed):
        _place_icon_at(path.name, px + i * step, py)


class BoxWidget(GlassWindow):
    changed = Signal()
    removed = Signal(str)     # 盒子把自己删掉了（图标被拖空）

    def __init__(self, store, box):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool, radius=20,
                         activate=False)
        self.store = store
        self.box = box
        self.glass_alpha = 212
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover, True)

        self._provider = QFileIconProvider()
        self._icons: dict[tuple[str, int], object] = {}
        # 图标缓存 + 补图标的队列：paintEvent 里只查缓存，取图标走后台线程
        self._icon_wait: list[tuple[str, int]] = []
        self._icon_ready_conn = True
        icon_loader().done.connect(self._on_icon_ready)
        self._items = store.items(box)
        self._had_items = bool(self._items)
        self._expanded = False
        self._t = 0.0
        self._press_global: QPoint | None = None
        self._win_pos: QPoint | None = None
        self._moved = False
        self._pressed_item: int | None = None
        self._item_drag = False
        self._busy_drop = False       # 正在收尾一次拖放：这期间忽略目录变化
        self._hot = False
        self._hovered = False
        self._band: QRect | None = None
        self._band_origin: QPoint | None = None
        self._band_latest: QPoint | None = None      # 最新的鼠标位置（每帧用它重算一次）
        self._band_at = 0.0                          # 上一次重算的时间
        self._hover_at = 0.0                         # 上一次算悬停的时间
        self._selected: set[int] = set()
        self._drag_set: list[int] = []   # 这次拖出去的是哪几个（框选多个时）
        self._editing = False
        self._edit: QLineEdit | None = None
        self._born = time.monotonic()
        self._side_w, self._side_h = self._collapsed_size()   # 收起尺寸（随图标数量变）
        self._hover_t = 0.0                   # 悬停放大进度 0→1
        self._hover_index: int | None = None  # 鼠标当前压在哪个图标上
        self._bounce_p: dict[int, float] = {}  # 每个图标各自的弹跳进度
        self._last_tick = time.monotonic()
        self._hover_until = 0.0               # 「放大→缩回」这一轮播到什么时候
        self._window_dragging = False         # 正在拖整个盒子（横杠变亮）
        self._drag_area = False               # 这次按下落在拖动横杠上
        self._press_in_glass = False          # 这次按下落在玻璃本体上（不是那圈透明余量）
        # 拖动区上的「双击」：只认两次完整的按下+抬起（见 _grip_click），
        # 单击什么都不做——用户明确要求点拖动区不许把盒子展开
        self._grip_click_t = 0.0
        self._grip_click_pos: QPoint | None = None
        self._auto_expanded = False           # 展开是因为有东西拖进来
        self._internal_drag: int | None = None  # 盒内拖动换位的图标
        self._drag_preview: list[int] | None = None  # 拖动中图标的临时顺序（原始下标）
        self._drag_xy: QPoint | None = None
        self._press_item_pos = QPoint(0, 0)
        self._drag_pm: QPixmap | None = None   # 拖动时的整窗快照
        # 自定义指针：只在鼠标位于盒子里时接管（离开就还原系统光标）
        self._cursor_on = False
        self._cur_pt = QPointF(-100.0, -100.0)
        self._cur_last = QPointF(-100.0, -100.0)
        self._trail = [QPointF(-100.0, -100.0) for _ in range(CURSOR_TRAIL)]
        self._trail_act = 0.0
        self._trail_timer: QTimer | None = None
        self._trail_stamp = 0.0
        self._drag_paints = 0
        self._drag_move_ms = 0.0
        self._blur_on = False                 # 展开时是否画了「重叠区模糊」
        self._backdrop: list[tuple[QRect, QPixmap]] = []
        # Controller 挂上来的回调：层级重排、toast 提示、盒子合并
        self.on_z_changed = None
        self.on_toast = None
        self.on_merge = None
        self.on_grow_request = None   # 盒子要长高：先问 Controller 有没有地方
        self.on_shrunk = None         # 收到横杠：让下面的东西吸上来
        self.on_grew = None           # 自动变大之后：把被压住的盒子和桌面图标挪开
        self._grow_pending = False    # 长大了还没清场（拖图标进来时通常还是展开的）
        # 多选：桌面上拉框选中几个盒子，它们一起高亮、一起搬
        self.on_group = None          # 返回「跟我一起搬的盒子/卡片」（没多选就空）
        self.on_widget_press = None   # 在盒子上按下（用来清掉别人的多选）
        self.sel_key = f"box:{getattr(box, 'id', '')}"   # 多选时给 Controller 认的键
        self._is_selected = False     # 被框选中了：玻璃压暗一点（和拖进来时同一套）
        self._drag_group: list = []   # 这次拖动要一起搬的兄弟窗口
        self._group_origin: QPoint | None = None
        self._group_starts: list = []
        self._shake_anim = None
        self._slide = None
        self._slide_timer = None
        self._menu_busy = False   # 正弹着菜单：点菜单不算「点盒子外面」
        # 合并手势：拖动时压在另一个盒子的拖动区上
        self._merge_target = None
        self._merge_was_expanded = False
        # 停住一会儿再展开目标盒子：一路扫过去的时候每个盒子都展开一次动画，
        # 拖起来就一顿一顿的
        self._merge_timer = QTimer(self)
        self._merge_timer.setSingleShot(True)
        self._merge_timer.setInterval(110)
        self._merge_timer.timeout.connect(self._expand_merge_target)
        # 双击打开要延迟一点点再执行：拖图标时第二下按下会被 Qt 判成双击，
        # 不延迟就会在拖拽途中把软件打开
        self._launch_timer: QTimer | None = None
        self._pending_launch: int | None = None

        self._watch = QFileSystemWatcher(self)
        self._watch.directoryChanged.connect(self._on_dir_changed)
        self._watch.addPath(str(store.box_dir(box)))
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(60)
        self._refresh_timer.timeout.connect(self._on_dir_settled)

        # 收放动画：自己按 8ms 推进（约 120fps）。QVariantAnimation 在 PySide6 里
        # 没有 setUpdateInterval，固定 16ms 一帧，180Hz 屏上看着是一条一条的。
        self._anim_timer = QTimer(self)
        self._anim_timer.setTimerType(Qt.PreciseTimer)
        self._anim_timer.setInterval(8)
        self._anim_timer.timeout.connect(self._tick_anim)
        self._anim_curve = QEasingCurve(QEasingCurve.Type.InOutCubic)
        # 一次动画推「_t」（展开↔收起）或者「_bar_t」（缩略↔只剩横杠）
        self._anim_field = "t"
        self._anim_from = 0.0
        self._anim_to = 0.0
        self._anim_p = 0.0
        self._anim_dur = 0.28
        self._anim_frames = 0
        self._anim_t0 = 0.0
        self._anim_last = 0.0
        self._anim_gap_max = 0.0
        # 几何按「起止矩形」插值：这样从横杠、缩略、展开任意两态之间都能直接滑，
        # 中途尺寸变了也不会跳。
        self._anim_rect_from = QRect()
        self._anim_rect_to = QRect()
        # 收起形态：False = 带缩略图标的方块，True = 只剩那条可拖拽的横杠
        self._bar = False
        self._bar_t = 0.0        # 实际进度：0 = 缩略，1 = 只剩横杠
        if bool(getattr(box, "bar", False)):
            # 上次退出时就是一条横杠：直接就按横杠起来
            self._bar = True
            self._bar_t = 1.0

        # 鼠标移到收起的盒子上：轻轻放大一点再缩回（**改窗口尺寸**，内容铺满，
        # 这样结束时正好回到静止状态，不会有最后一次「跳」）
        self._pulse_anim = QVariantAnimation(self)
        self._pulse_anim.setDuration(HOVER_MS)
        self._pulse_anim.setEasingCurve(QEasingCurve.InOutCubic)
        self._pulse_anim.valueChanged.connect(self._on_pulse)
        self._pulse_anim.finished.connect(self._on_pulse_finished)

        # 「有东西要落进来」的提示：拖着桌面图标压在盒子上、或者拖另一个盒子
        # 过来合并时，整块玻璃压暗一点；松手 / 移开就淡回去。
        self._hot_t = 0.0
        self._hot_anim = QVariantAnimation(self)
        self._hot_anim.setDuration(130)
        self._hot_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._hot_anim.valueChanged.connect(self._on_hot)

        # 图标悬停弹跳：每个图标一个进度，用一个 16ms 定时器推进
        self._bounce_timer = QTimer(self)
        self._bounce_timer.setInterval(16)
        self._bounce_timer.timeout.connect(self._tick_bounce)

        # 换位时的滑动：每个图标一条「离目标格还差多远」的偏移，150ms 收回去
        self._slot_ofs: dict[int, list] = {}      # 下标 -> [dx, dy, 起始时间]
        self._move_timer = QTimer(self)
        self._move_timer.setInterval(16)
        self._move_timer.timeout.connect(self._tick_moves)

        # 拖动用 16ms 定时器统一搬窗口：鼠标回报率再高，窗口也最多 60fps 更新，
        # 而且总是移动到**最新**位置（不丢帧），比每个 WM_MOUSEMOVE 都
        # SetWindowPos 轻得多，也不会一顿一顿。
        self._drag_target: QPoint | None = None
        self._box_move_at = 0.0        # 上一次搬盒子的时间（一帧最多搬一次）
        self._drag_timer = QTimer(self)
        self._drag_timer.setInterval(16)
        self._drag_timer.timeout.connect(self._apply_drag_move)

        # 拖到盒子外面就自动收起（拖进来的东西移开了）
        self._collapse_timer = QTimer(self)
        self._collapse_timer.setSingleShot(True)
        self._collapse_timer.setInterval(120)
        self._collapse_timer.timeout.connect(self._auto_collapse)

        # 拉框选图标时统一按 60fps 重画：鼠标回报率是 1000Hz 的话，原来每个
        # WM_MOUSEMOVE 都要重画一遍整窗，事件队列直接堵死——这就是「一次选中
        # 一片图标会卡」的原因。
        self._band_timer = QTimer(self)
        self._band_timer.setInterval(16)
        self._band_timer.timeout.connect(self._band_frame)
        # 这一帧要重画哪一块（框选时只画动过的地方，整窗重画在 DWM 那边太贵）
        self._band_pending: QRect | None = None
        # 图标文字「拖拽中省略成什么样」只跟名字和字号有关，框选时每帧重新
        # elide 一遍纯属浪费，按路径缓存起来
        self._label_cache: dict[str, str] = {}

        # 展开状态下监听「点了盒子外面」→ 收起
        self._was_down = False
        self._outside_timer = QTimer(self)
        self._outside_timer.setInterval(60)
        self._outside_timer.timeout.connect(self._watch_outside_click)

        self._apply_collapsed_geometry()
        self._clamp_to_screen()
        # 只有一个图标的盒子没有存在意义（含上次退出时遗留的）：起来一会儿就
        # 自动解散，把那个图标放回桌面
        if len(self._items) <= 1:
            QTimer.singleShot(1200, self._check_thin)

    # ---------------------------------------------------------------- 收起尺寸
    def _size_key(self) -> str:
        """这个盒子选的是哪种大小（老配置没有这个字段 = 自动）。"""
        key = str(getattr(self.box, "size", "auto") or "auto")
        return key if key in SIZE_CHOICES else "auto"

    def _size_grid(self) -> tuple[int, int]:
        """收起状态的格子数 (列, 行)。

        固定大小（2×2 / 3×3 / 4×4 / 5×5 / 4×2 / 2×4）就是菜单里那个；自动按
        图标数开方，**永远是正方形**——形状只在手动选的时候才有可预期的含义。
        """
        fixed = _FIXED_GRID.get(self._size_key())
        if fixed:
            return fixed
        n = len(self._items)
        k = math.ceil(math.sqrt(n)) if n > 0 else 2
        k = max(2, min(int(k), MAX_MINI_COLS))
        return k, k

    def _grid_px(self, count: int) -> int:
        """n 个格子连起来有多长（含留白）。"""
        return MINI * count + MINI_GAP * (count - 1) + MINI_PAD * 2

    def _collapsed_size(self) -> tuple[int, int]:
        cols, rows = self._size_grid()
        return self._grid_px(cols), self._grid_px(rows)

    def _collapsed_h(self) -> int:
        """收起状态的高度：图标区 + 上面那条标题栏。收到「只剩横杠」时只有标题栏。"""
        if self._bar:
            return TITLE_H
        return self._side_h + TITLE_H

    def collapsed_rect(self) -> QRect:
        """收起状态下盒子占的屏幕矩形（不含悬停放大）。"""
        return QRect(self.box.x, self.box.y, self._side_w, self._collapsed_h())

    def _win_margin(self) -> int:
        """窗口比盒子本体多出来的那一圈（尽量小，静止时完全透明）。

        这一圈不能省：Windows 的窗口尺寸只能是整数像素，靠改窗口尺寸做悬停
        放大一定是一格一格跳；唯一平滑的办法是**窗口不动、只把玻璃画大**，
        而画大就得有地方画。这里按实际放大幅度算：玻璃每边最多长
        `side * HOVER_GROW / 2`，再加 1px 抗锯齿余量。

        试过给它加阴影（好让那圈「有东西」），用户觉得突兀，撤了。纯透明
        的那几像素合成到桌面上就是壁纸本身，看不出来。
        """
        side = max(int(getattr(self, "_side_w", 96) or 96),
                   int(getattr(self, "_side_h", 96) or 96))
        grow = int(math.ceil(side * HOVER_GROW / 2.0)) + 1
        return max(3, grow)

    def _inset(self) -> int:
        """给 GlassWindow 用：玻璃距窗口边多少像素。"""
        return self._win_margin()

    def _cw(self) -> float:
        """玻璃的宽（窗口宽减去两边的余量）。"""
        return float(self.width() - self._win_margin() * 2)

    def _ch(self) -> float:
        """玻璃的高。"""
        return float(self.height() - self._win_margin() * 2)

    def _to_content(self, pos: QPoint) -> QPoint:
        """窗口坐标 → 玻璃坐标（绘制和命中判断都用玻璃坐标）。"""
        m = self._win_margin()
        return QPoint(pos.x() - m, pos.y() - m)

    def _paint_z(self) -> float:
        """当前这一帧玻璃被放大的比例（和 paintEvent 里算的 z 一致）。"""
        return (1.0 - self._t) * self._hover_scale() + self._t

    def _in_glass(self, pos: QPoint) -> bool:
        """窗口坐标里的点是不是落在**看得见的那块玻璃**上。

        悬停时玻璃是绕窗口中心放大的（窗口本身不动），所以不能直接拿窗口
        矩形判断，要按绘制时的变换反算回玻璃坐标再比。
        """
        z = self._paint_z()
        if z <= 1e-6:
            z = 1.0
        c = self.rect().center()
        m = self._win_margin()
        gx = (pos.x() - c.x() * (1.0 - z)) / z - m
        gy = (pos.y() - c.y() * (1.0 - z)) / z - m
        return 0.0 <= gx < self._cw() and 0.0 <= gy < self._ch()

    def _win_w(self) -> int:
        return self._side_w + self._win_margin() * 2

    def _win_h(self) -> int:
        return self._collapsed_h() + self._win_margin() * 2

    def _hover_scale(self) -> float:
        """>1 的时候整块玻璃（含阴影、图标）一起放大。

        窗口尺寸一个像素都不动，只把绘制缩放/平移——所以不会出现「边缘一格
        一格跳」。玻璃轮廓是真的在变大，圆角也由 QPainter 自己画，不会被
        窗口边界切成直角。
        """
        return 1.0 + HOVER_GROW * self._hover_t

    def _apply_collapsed_geometry(self) -> None:
        """窗口位置固定（含余量），尺寸固定——悬停时只改 paint_scale。"""
        m = self._win_margin()
        self.setGeometry(self.box.x - m, self.box.y - m,
                         self._win_w(), self._win_h())

    def _on_hover_anim(self, value) -> None:
        """老接口，保留为空实现（悬停改成窗口尺寸动画后不再用绘制缩放）。"""
        return None

    def _sync_side(self) -> bool:
        """格子数变了就改收起尺寸：**左上角当锚点**，往右下长/收。

        原来是左右各让一半（横向居中），用户加一个图标盒子会同时往两边长，
        摆在屏幕边上的盒子还会被挤得乱跑；现在只往右下走，位置一动不动。
        宽高分开算：4×2 这类横条只往宽里长。
        """
        w, h = self._collapsed_size()
        if (w, h) == (self._side_w, self._side_h):
            return False
        self._side_w, self._side_h = w, h
        return True

    def _clamp_to_screen(self) -> None:
        """确保整个盒子在可视区内（任务栏上方），否则收起来的盒子会“消失”。

        边距是 0：用户要盒子贴着屏幕边缘，之前留 8px 看着就是「永远差一小块」。
        """
        avail = self._screen()
        x = min(max(self.box.x, avail.left()), avail.right() + 1 - self._side_w)
        y = min(max(self.box.y, avail.top()), avail.bottom() + 1 - self._collapsed_h())
        if (x, y) != (self.box.x, self.box.y):
            self.box.x, self.box.y = x, y
            self.store.move(self.box, x, y)
        if not self._expanded:
            self._apply_collapsed_geometry()

    # ---------------------------------------------------------------- 数据
    def _on_dir_changed(self, _path: str) -> None:
        self._refresh_timer.start()

    def _on_dir_settled(self) -> None:
        # 正在拖、或者正在处理一次落点：文件是一个一个搬的，中途刷新会让盒子
        # 连着变好几次大小（看着就是抖），等收尾那次统一刷新就行
        if self._item_drag or self._busy_drop or self._internal_drag is not None:
            return
        self.refresh()

    def refresh(self) -> None:
        self._items = self.store.items(self.box)
        self._label_cache.clear()
        self._slot_ofs.clear()      # 图标列表可能变了，旧的滑动偏移没意义
        if self._items:
            self._had_items = True
        folder = str(self.store.box_dir(self.box))
        if folder not in self._watch.directories():
            self._watch.addPath(folder)
        if self._selected:
            self._selected = {i for i in self._selected if i < len(self._items)}
        old_w, old_h = self._side_w, self._side_h
        resized = self._sync_side()
        if resized:
            # 变大了：等窗口几何定下来之后要让邻居和桌面图标让开（见 _flush_grow）；
            # 变小了就把这件事取消掉——没有再压住谁
            self._grow_pending = (self._side_w > old_w or self._side_h > old_h)
        self._rect_collapsed = QRect(self.box.x - self._win_margin(),
                                     self.box.y - self._win_margin(),
                                     self._win_w(), self._win_h())
        # 展开时的目标矩形也要跟着算：不然「拖进来的同时尺寸变了」会先按旧矩形
        # 收一下，下一帧再跳回新尺寸，看着就是抖一下
        self._rect_expanded = self._expanded_rect()
        if self._anim_timer.isActive():
            # 动画途中尺寸变了（加/删图标）：换成新的目标矩形，按当前进度重画
            self._anim_rect_to = (self._rect_expanded if self._expanded
                                  else self._rect_collapsed)
            self._anim_geometry()
        elif self._expanded:
            self.setGeometry(self._rect_expanded)
        elif resized:
            self._clamp_to_screen()
        self.update()
        if self._grow_pending and not self._expanded:
            self._flush_grow()
        # 图标被拖走 / 被滚轮键拆走，只剩一个（或没有）→ 盒子没有意义了，
        # 自己解散把剩下的放回桌面。刚建出来的那一秒先等一等：建盒和合并时
        # 图标是一个一个搬进去的，中途会短暂只有 0~1 个，当场解散会把刚搬进
        # 去的那个又扔回桌面。
        if len(self._items) <= 1 and not self._item_drag:
            wait = 1.0 - (time.monotonic() - self._born)
            if wait > 0:
                QTimer.singleShot(int(wait * 1000) + 30, self._check_thin)
            else:
                self._dissolve_when_thin()

    def _check_thin(self) -> None:
        """等那一秒过去了再看一眼：这期间又加回图标就不解散。"""
        if self._item_drag or not self.isVisible():
            return
        if len(self.store.items(self.box)) > 1:
            self.refresh()
            return
        self._dissolve_when_thin()

    def _flush_grow(self) -> None:
        """自动变大之后清场：让 Controller 把新压住的盒子、卡片、桌面图标挪开。

        拖图标进来时盒子通常是**展开**的，那时窗口还按展开矩形摆着，清场没有
        意义；所以先记标记，等真的收起来、按新尺寸落位时再清。
        """
        if not self._grow_pending:
            return
        self._grow_pending = False
        cb = getattr(self, "on_grew", None)
        if cb is None:
            return
        try:
            cb(self)
        except (RuntimeError, TypeError):
            pass

    def _dissolve_when_thin(self) -> None:
        """只剩 ≤1 个图标：把剩下的放回桌面，盒子自己消失。

        放回来的图标要落在**盒子原来的位置**，不自动找格子。
        """
        # 收起状态下的盒子位置（逻辑像素）→ 物理屏幕坐标
        cx = self.box.x + self._side_w // 2
        cy = self.box.y + self._collapsed_h() // 2
        px, py = screen.to_physical((cx, cy, cx, cy))[0:2]
        names = [(p.name, self.store.is_ns(self.box, p))
                 for p in self.store.items(self.box)]
        self.dispose()
        for path in self.store.items(self.box):
            if self.store.is_ns(self.box, path):
                # 系统图标：恢复桌面图标，不在桌面留副本
                self.store.restore_ns(self.box, path)
            else:
                self.store.remove_to(self.box, path)
        if self.store.items(self.box):
            # 有文件放不回去 → 保留盒子，绝不丢文件
            try:
                self._watch.addPath(str(self.store.box_dir(self.box)))
            except Exception:  # noqa: BLE001
                pass
            self.changed.emit()
            return
        if self.store.get(self.box.id) is not None:
            self.store.delete(self.box)
        _log("thin box %s dissolved", self.box.id)
        for name, is_ns in names:
            if is_ns:
                continue   # 系统图标已经被放出来了，不用再摆位置
            _place_icon_at(name, px, py)
        self.close()
        self.deleteLater()
        self.removed.emit(self.box.id)

    def _remove_when_empty(self) -> None:
        """空盒子没有存在意义，直接销毁（数据目录 + 窗口）。"""
        # 先停掉目录监听：它还挂着这个目录，rmtree 可能删不掉
        self.dispose()
        if not self.store.delete(self.box):
            try:
                self._watch.addPath(str(self.store.box_dir(self.box)))
            except Exception:  # noqa: BLE001
                pass
            return  # 目录里还有东西（可能正在被写），下次再说
        _log("empty box %s removed", self.box.id)
        self.close()
        self.deleteLater()
        self.removed.emit(self.box.id)

    def add_files(self, paths: list[str]) -> int:
        added = self.store.add_paths(self.box, paths)
        if added:
            self.refresh()
            self.changed.emit()
        return added

    def _icon(self, path, size: int):
        key = (str(path), size)
        hit = self._icons.get(key)
        if hit is None:
            hit = _ICON_CACHE.get(key)
            if hit is not None:
                self._icons[key] = hit
        if hit is not None:
            return hit
        # ⚠️ paintEvent 里**绝不能**去问 Windows 外壳要图标：一次能卡 120ms 以上
        # （看门狗抓到的栈就是 paintEvent → _icon → icons.raw_image → shell）。
        # 缺图标的先画空，丢给后台线程，回来了再重画。
        if key not in self._icon_wait:
            self._icon_wait.append(key)
            icon_loader().submit(key, str(path))
        return _empty_pm()

    def _on_icon_ready(self, path_s: str, size: int, img) -> None:
        """后台线程把图标取回来了：转成 QPixmap 缓存起来（QPixmap 只能在主线程造）。"""
        key = (path_s, size)
        if key in self._icon_wait:
            self._icon_wait.remove(key)
        pm = None
        if img is not None and not img.isNull():
            pm = QPixmap.fromImage(img)
            if not pm.isNull():
                pm = pm.scaled(size, size, Qt.KeepAspectRatio,
                               Qt.SmoothTransformation)
        if pm is None or pm.isNull():
            _log_force("icon fallback: provider used for %s", path_s)
            try:
                pm = self._provider.icon(QFileInfo(path_s)).pixmap(QSize(size, size))
            except Exception:  # noqa: BLE001
                pm = QPixmap()
        if pm.isNull():
            return
        pm.setDevicePixelRatio(1.0)  # 系统图标带的缩放因子会让它画得很小
        _ICON_CACHE[key] = pm
        self._icons[key] = pm
        self.update()

    def _icon_candidates(self, path: Path) -> list[str]:
        return _icon_candidates(path)

    # ---------------------------------------------------------------- 右键菜单
    def contextMenuEvent(self, event) -> None:
        """盒子里**图标**的右键：直接弹资源管理器那套原生菜单。"""
        _log("context menu expanded=%s pos=%s", self._expanded, event.pos())
        # 先无条件吞掉：不然事件会继续传下去，落到盒子底下的桌面图标上，
        # 弹出一个「此电脑」的菜单（用户报的 bug）。
        event.accept()
        if not self._expanded:
            return
        index = self._item_at(self._to_content(event.pos()))
        _log("context menu idx=%s n=%s", index, len(self._items))
        if index is None:
            return
        path = self._items[index]
        gp = event.globalPos()
        # 先让 Qt 的事件返回，再弹菜单（它是模态循环，不要在里面重入）
        QTimer.singleShot(0, lambda: self._open_item_menu(index, gp))

    def _open_item_menu(self, index: int, gp) -> None:
        """新式菜单（自己画的 Win11 风格）；里面「显示更多选项」才弹系统那张。"""
        from . import item_menu
        t0 = time.perf_counter()
        self._menu_busy = True
        try:
            item_menu.show_for_item(self, index, gp)
        except Exception as exc:  # noqa: BLE001 - 菜单炸了也别影响盒子
            _log("item menu failed: %r", exc)
        finally:
            self._menu_busy = False
        _log("item menu took %.0fms", (time.perf_counter() - t0) * 1000.0)
        QTimer.singleShot(400, self.refresh)

    def _open_system_menu(self, path, gp) -> None:
        from . import shell_menu
        t0 = time.perf_counter()
        try:
            ok = shell_menu.show(int(self.winId()), str(path), gp.x(), gp.y())
        except Exception:  # noqa: BLE001 - 菜单炸了也别影响盒子
            ok = False
        _log("system menu %s -> %s took %.0fms", path.name, ok,
             (time.perf_counter() - t0) * 1000.0)
        # 系统菜单可能改了文件名/删了文件，回来对一下账
        QTimer.singleShot(600, self.refresh)

    # ---------------------------------------------------------------- 盒子菜单
    def _open_box_menu(self) -> None:
        """右上角三个点：常驻展开 / 重命名 / 销毁收纳盒。"""
        menu = GlassMenu(self)
        # 抓背景要在 exec 之前：菜单一上屏，抓到的就是它自己的灰底
        menu.prepare_backdrop()
        act_pin = menu.addAction(tr("box.pin"))
        act_pin.setCheckable(True)
        act_pin.setChecked(bool(getattr(self.box, "pinned", False)))
        act_ren = menu.addAction(tr("box.rename"))
        act_sort = menu.addAction(tr("box.sort_name"))
        # 大小：二级菜单（自动 / 2×2 / 3×3 / 4×4 / 5×5）
        size_menu = GlassMenu(menu)
        size_menu.setTitle(tr("box.size"))
        # 二级菜单是弹出来那一刻才抓背景的：那时父菜单已经在屏幕上，
        # 抓到的才是「父菜单在后面」的样子（提前抓会抓到桌面）
        size_menu.aboutToShow.connect(size_menu.prepare_backdrop)
        menu.addMenu(size_menu)
        size_acts = []
        current = self._size_key()
        for key in SIZE_CHOICES:
            act = size_menu.addAction(tr("box.size." + key))
            act.setCheckable(True)
            act.setChecked(key == current)
            size_acts.append((act, key))
        menu.addSeparator()
        act_del = menu.addAction(tr("box.destroy"))
        self._menu_busy = True
        modal(1)
        try:
            chosen = menu.exec(QCursor.pos())
        finally:
            modal(-1)
            self._menu_busy = False
        if chosen is act_pin:
            self._set_pinned(act_pin.isChecked())
        elif chosen is act_ren:
            self._begin_inline_rename()
        elif chosen is act_sort:
            self.store.sort_by_name(self.box)
            self._items = self.store.items(self.box)
            self._label_cache.clear()
            self._slot_ofs.clear()      # 顺序变了，旧的滑动偏移会套到别的图标上
            self.update()
        elif chosen is act_del:
            self.destroy_box()
        else:
            for act, key in size_acts:
                if chosen is act:
                    self.set_size(key)
                    break

    def destroy_box(self, silent: bool = False) -> bool:
        """销毁收纳盒：里面的图标**全部放回桌面**（摆在盒子附近），再删掉盒子。

        返回 True = 盒子已经没了；False = 还有文件挪不回去，盒子留着（绝不丢文件）。
        图标位置不是回到「原来桌面上的格子」，而是原地散在盒子周围——用户要的
        「返还桌面的位置自适应放在盒子附近」。
        """
        cx = self.box.x + self._side_w // 2
        cy = self.box.y + self._collapsed_h() // 2
        try:
            px, py = screen.to_physical((cx, cy, cx, cy))[0:2]
        except Exception:  # noqa: BLE001
            px, py = 400, 400
        items = list(self.store.items(self.box))
        # 完整文件名：桌面上普通文件是带扩展名显示的（.doc/.txt…），只拿 stem
        # 去对名字永远匹配不上 → 图标会留在 explorer 默认的左上角格子
        names = [(p.name, self.store.is_ns(self.box, p)) for p in items]
        self.dispose()          # 先停目录监听：还挂着这个目录 rmtree 可能删不掉
        for path in items:
            if self.store.is_ns(self.box, path):
                # 系统图标（此电脑/回收站…）：删掉盒子里的 CLSID 快捷方式，
                # 把桌面上被隐藏的原图标放出来，不在桌面留副本
                self.store.restore_ns(self.box, path)
            else:
                self.store.remove_to(self.box, path)
        if self.store.items(self.box):
            # 有文件放不回去（被占用等）→ 保留盒子，绝不清掉文件
            try:
                self._watch.addPath(str(self.store.box_dir(self.box)))
            except Exception:  # noqa: BLE001
                pass
            self.changed.emit()
            return False
        if self.store.get(self.box.id) is not None:
            self.store.delete(self.box)
        for name, is_ns in names:
            if is_ns:
                continue
            _place_icon_at(name, px, py)
        _log("box %s destroyed (%d items)", self.box.id, len(names))
        self.close()
        self.deleteLater()
        self.removed.emit(self.box.id)
        return True

    def _set_pinned(self, on: bool) -> None:
        """常驻展开：开着的时候点盒子外面也不会收起。"""
        self.box.pinned = bool(on)
        self.store.save()
        self._log_state(f"pinned={self.box.pinned}")
        if on:
            self._auto_expanded = False
            self._collapse_timer.stop()
            self.toggle(True)

    def set_size(self, key: str) -> None:
        """三点菜单「大小」：自动 / 2×2 / 3×3 / 4×4 / 5×5 / 4×2 / 2×4。

        自动才是默认：新盒子、老配置都没这个字段，一律当自动（按图标数长）。
        固定大小是**你自己选的**，所以图标少一个多一个都不再变形；装不下时
        最后一格画反斜杠（`_paint_more`），展开还是能看全。4×2 / 2×4 是横条
        和竖条——只有手动选的时候才有，自动永远正方形。
        """
        key = key if key in SIZE_CHOICES else "auto"
        if self._size_key() == key:
            return
        old_w, old_h = self._side_w, self._side_h
        self.box.size = key
        try:
            self.store.save()
        except OSError:
            pass
        self._sync_side()
        grew = self._side_w > old_w or self._side_h > old_h
        shrank = self._side_w < old_w or self._side_h < old_h
        self._rect_collapsed = QRect(self.box.x - self._win_margin(),
                                     self.box.y - self._win_margin(),
                                     self._win_w(), self._win_h())
        self._rect_expanded = self._expanded_rect()
        if self._expanded:
            self.setGeometry(self._rect_expanded)
            # 展开着改大小：等收起来那一刻再清场（和自动扩容同一条路）
            self._grow_pending = grew
        else:
            self._grow_pending = False
            if grew:
                cb = getattr(self, "on_grow_request", None)
                if cb is not None:
                    try:
                        cb(self, self.collapsed_rect())
                    except (RuntimeError, TypeError):
                        pass
            elif shrank:
                # 往下让出来的高度：下面的盒子/卡片吸上来（宽度变化不影响它们）
                freed = (old_h + TITLE_H) - (self._side_h + TITLE_H)
                cb = getattr(self, "on_shrunk", None)
                if cb is not None and freed > 0:
                    try:
                        cb(self, freed)
                    except (RuntimeError, TypeError):
                        pass
            self._clamp_to_screen()
            self._apply_collapsed_geometry()
        self.update()
        _log_force("box %s size=%s side=%dx%d", self.box.id, key,
                   self._side_w, self._side_h)

    def _auto_collapse(self) -> None:
        """拖进来的东西又移走了：把「因为拖拽才展开」的盒子收回去。"""
        if not self._auto_expanded:
            return
        if getattr(self.box, "pinned", False):
            self._auto_expanded = False
            return
        self._auto_expanded = False
        if self._expanded:
            self.toggle(False)

    def _sibling_rects(self) -> list[QRect]:
        """别的**收纳盒**的玻璃矩形（逻辑像素）。

        ⚠️ 卡片不在这里：卡片的玻璃又大又空（时钟那类 300×150 起），盒子只要
        擦到它一点，按「不重叠」算就得推出几百像素——用户报的「飞到不知道多远
        的地方」。盒子压住卡片就让它压着，用户自己挪一下；卡片那边反过来仍然
        躲盒子（`CardWindow._obstacles`）。
        """
        provider = getattr(self, "siblings", None)
        if provider is None:
            return []
        try:
            others = [w for w in provider() if w is not self and w.isVisible()]
        except (RuntimeError, TypeError):
            return []
        rects: list[QRect] = []
        for w in others:
            if not isinstance(w, BoxWidget):
                continue
            try:
                if w.is_expanded:
                    # 用**目标展开矩形**：展开动画那 300ms 里窗口还是旧的收起尺寸
                    r = w._expanded_rect()
                    m = w._win_margin()
                    rects.append(r.adjusted(m, m, -m, -m))
                else:
                    rects.append(w.collapsed_rect())
            except (RuntimeError, TypeError):
                continue
        return rects

    def _clamp_pos(self, pos: QPoint, size) -> QPoint:
        avail = self._screen()
        x = min(max(pos.x(), avail.left()), avail.right() + 1 - size.width())
        y = min(max(pos.y(), avail.top()), avail.bottom() + 1 - size.height())
        return QPoint(x, y)

    def _avoid_overlap(self) -> None:
        """松手后别跟别的盒子叠着：**沿重叠最浅的那条边推出去**。

        这一块的规矩被用户来回纠正过四次，别再自作聪明：
          1. **推最近的一条边**：放上面就往上、放右边就往右，距离永远是最小的；
             固定「水平优先」会让「放在别人上面只压住几像素」被甩到侧边去。
          2. **横竖差不多远时判横向**（`aligner.X_BIAS`）：「往右半边放」是用户
             最常用的动作，差一点点就被推到下面是他最不能接受的。
          3. **只算真正压住的盒子**：远处同高度的盒子不参与，否则一推就飞出去。
          4. **绝不跨到对面**：右边出不去就走上下，不走左边。
          5. 推不干净就保持原样——宁可留一点重叠，也不许盒子跳到别的格子去。
        桌面图标不算障碍（`_push_desktop_icons` 会挪图标）；卡片也不算
        （见 `_sibling_rects` 的说明）；展开着的盒子按**展开矩形**算。
        """
        if self._expanded:
            m = self._win_margin()
            mine = self._expanded_rect().adjusted(m, m, -m, -m)
        else:
            mine = self.collapsed_rect()
        others = self._sibling_rects()
        gap = 2
        size = mine.size()

        def clamp(r: QRect) -> QRect:
            return QRect(self._clamp_pos(r.topLeft(), size), size)

        landed = aligner.place_clear(mine, others, gap, clamp,
                                     max_shift=max(size.width(), size.height()))
        if landed is None or landed.topLeft() == mine.topLeft():
            return
        self.box.x, self.box.y = landed.x(), landed.y()
        if self._expanded:
            # 展开态松手：按展开矩形搬，别按收起尺寸重排（那样会当场缩成小块）
            self._rect_expanded = self._expanded_rect()
            self.setGeometry(self._rect_expanded)
        else:
            self._apply_collapsed_geometry()
        _log("avoid overlap -> %s,%s", self.box.x, self.box.y)

    def _log_state(self, msg: str) -> None:
        _log("%s box=%s", msg, self.box.id)

    def _show_item_menu(self, index: int, global_pos: QPoint) -> None:
        path = self._items[index]
        menu = GlassMenu(self)
        menu.prepare_backdrop()
        act_open = menu.addAction(tr("item.open"))
        act_reveal = menu.addAction(tr("item.reveal"))
        act_rename = menu.addAction(tr("item.rename"))
        menu.addSeparator()
        act_back = menu.addAction(tr("item.back"))
        act_del = menu.addAction(tr("item.trash"))
        chosen = menu.exec(global_pos)
        if chosen is None:
            return
        if chosen is act_open:
            self._launch(index)
        elif chosen is act_reveal:
            try:
                subprocess.Popen(["explorer", "/select,", str(path)])
            except OSError:
                pass
        elif chosen is act_rename:
            self._rename_item(index)
        elif chosen is act_back:
            self._restore_item(index)
        elif chosen is act_del:
            self._delete_item(index)

    def _rename_item(self, index: int) -> None:
        path = self._items[index]
        old = path.stem if path.suffix.lower() in (".lnk", ".url") else path.name
        text, ok = QInputDialog.getText(self, "重命名", "新名字：", text=old)
        text = (text or "").strip()
        if not ok or not text or text == old:
            return
        target = path.with_name(text + path.suffix)
        try:
            path.rename(target)
        except OSError:
            self.toast_error("改名失败（名字被占用或者字符不合法）")
            return
        self.store.renamed(self.box, path.name, target.name)
        self.refresh()
        self.changed.emit()

    def _restore_item(self, index: int) -> None:
        path = self._items[index]
        if self.store.is_ns(self.box, path):
            # 系统图标：删掉 CLSID 快捷方式 + 把桌面上的原图标放出来
            if not self.store.restore_ns(self.box, path):
                self.toast_error("没能恢复这个系统图标")
                return
            self.refresh()
            self.changed.emit()
            return
        moved = self.store.remove_to(self.box, path)
        if moved is None:
            self.toast_error("放不回桌面（目标目录写不进去）")
            return
        # 落在盒子当前位置，和「盒子只剩一个图标时自动解散」的落点保持一致
        cx = self.box.x + self._side_w // 2
        cy = self.box.y + self._collapsed_h() // 2
        px, py = screen.to_physical((cx, cy, cx, cy))[0:2]
        _place_icon_at(moved.stem if moved.suffix.lower() in (".lnk", ".url")
                       else moved.name, px, py)
        self.refresh()
        self.changed.emit()

    def _delete_item(self, index: int) -> None:
        path = self._items[index]
        if self.store.is_ns(self.box, path):
            # 系统图标删不得（桌面图标是系统设置的一部分），当成「移回桌面」
            self.store.restore_ns(self.box, path)
            self.refresh()
            self.changed.emit()
            return
        if not _recycle(path):
            self.toast_error("删不掉这个快捷方式")
            return
        self.store.forget(self.box, path.name)
        di.notify_deleted(path)
        self.refresh()
        self.changed.emit()

    def toast_error(self, text: str) -> None:
        """盒子自己不方便弹窗，交给 app 的 toast（没有就静默）。"""
        cb = getattr(self, "on_toast", None)
        if cb is not None:
            cb(text)

    def _launch(self, index: int) -> None:
        if 0 <= index < len(self._items):
            QDesktopServices_open(self._items[index])

    # ---------------------------------------------------------------- 几何
    def _screen(self) -> QRect:
        scr = self.screen() or QApplication.primaryScreen()
        return scr.availableGeometry()

    def _expanded_rect(self) -> QRect:
        n = max(1, len(self._items))
        rows = max(1, math.ceil(n / COLS))
        w = PAD * 2 + COLS * CELL_W
        h = HEADER + rows * CELL_H + PAD
        avail = self._screen()
        # 和收起状态一致：贴边（不留 8px 缝）
        x = min(self.box.x, avail.right() + 1 - w)
        y = min(self.box.y, avail.bottom() + 1 - h)
        x = max(avail.left(), x)
        y = max(avail.top(), y)
        # 返回**窗口**矩形：玻璃左上角贴在 (x, y)，四周再让出阴影/放大用的余量
        m = self._win_margin()
        return QRect(x - m, y - m, w + m * 2, h + m * 2)

    def _item_rect(self, index: int) -> QRect:
        col = index % COLS
        row = index // COLS
        return QRect(PAD + col * CELL_W, HEADER + row * CELL_H, CELL_W, CELL_H)

    def _item_at(self, pos: QPoint) -> int | None:
        if not self._expanded:
            return None
        for i in range(len(self._items)):
            if self._item_rect(i).adjusted(0, 0, 0, -14).contains(pos):
                return i
        return None

    def _title_rect(self) -> QRect:
        """展开时标题所在区域（玻璃坐标）：点这里就开始重命名。"""
        return QRect(PAD, 6, max(40, int(self._cw()) - PAD * 2 - 34), 30)

    def _mini_rect(self, index: int) -> QRectF:
        """收起时的缩略图格子：按当前格子数居中铺开（行和列可以不一样）。

        坐标是**玻璃坐标**（窗口坐标减去四周余量），绘制前 painter 已经平移过。
        **这里不取整**：缩放比例一变，取整误差就跟着变（±0.5px），十几个图标
        同时错位看着就是「抖一下」。
        """
        s = self._mini_scale()
        cols, rows = self._size_grid()
        mini = MINI * s
        gap = MINI_GAP * s
        total_w = mini * cols + gap * (cols - 1)
        total_h = mini * rows + gap * (rows - 1)
        x0 = (self._cw() - total_w) / 2.0
        y0 = TITLE_H + (self._ch() - TITLE_H - total_h) / 2.0
        col, row = index % cols, index // cols
        return QRectF(x0 + col * (mini + gap), y0 + row * (mini + gap), mini, mini)

    def _mini_scale(self) -> float:
        if not self._side_w:
            return 1.0
        return self._cw() / float(self._side_w)

    # ---------------------------------------------------------------- 动画
    def toggle(self, expanded: bool | None = None) -> None:
        target = (not self._expanded) if expanded is None else bool(expanded)
        if target == self._expanded:
            return
        if self._editing:
            # 收起/展开时先把改名结束掉，别留一个输入框挂在窗口上
            self._finish_inline_rename(True)
        self._expanded = target
        self._rect_collapsed = QRect(self.box.x - self._win_margin(),
                                     self.box.y - self._win_margin(),
                                     self._win_w(), self._win_h())
        self._rect_expanded = self._expanded_rect()
        self._anim_timer.stop()
        if not target:
            # 变大的那一刻如果还展开着，清场推迟到这儿：现在盒子要落回新尺寸了
            self._flush_grow()
        # 收放都是「两头慢、中间快」：起步有加速、落地有缓冲，看着是滑过去
        # 而不是「啪」地弹开（OutQuint 起手太猛、尾巴太长，容易觉得先冲后顿）
        self._anim_field = "t"
        self._anim_from = self._t
        self._anim_to = 1.0 if target else 0.0
        self._anim_dur = 0.30 if target else 0.26
        self._anim_rect_from = self.geometry()
        self._anim_rect_to = self._rect_expanded if target else self._rect_collapsed
        self._start_anim()
        # 展开时盯着「有没有点到盒子外面」
        if target:
            self._was_down = bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
            self._outside_timer.start()
        else:
            self._outside_timer.stop()
            self._hover_index = None
            self._bounce_p.clear()
            self._bounce_timer.stop()
            self._selected.clear()
            self._band = None
            self._band_origin = None
            self._band_timer.stop()
            self._internal_drag = None
            self._drag_xy = None
            self._drag_preview = None
            self._auto_expanded = False
            self._collapse_timer.stop()
            self._set_expanded_blur(False)   # 收起后立刻关掉模糊
        self._notify_z()

    def _save_bar(self) -> None:
        """把「是不是一条横杠」记进 boxes.json，重启后还是那个样子。"""
        want = bool(self._bar)
        if bool(getattr(self.box, "bar", False)) == want:
            return
        self.box.bar = want
        try:
            self.store.save()
        except OSError:
            pass

    def _set_bar(self, on: bool) -> None:
        """-1（只剩横杠）↔ 0（正常，带缩略图标）：只动高度，盒子位置不变。

        on=True 收到横杠（-1），on=False 长回正常（0）。展开状态（1）不走这里，
        调用方先 toggle(False) —— 那条路是 1 → -1 一步收到底。
        """
        on = bool(on)
        if not on and not self._expanded:
            cb = getattr(self, "on_grow_request", None)
            if cb is not None:
                try:
                    if not cb(self, QRect(self.box.x, self.box.y,
                                          self._side_w, self._side_h + TITLE_H)):
                        self.shake()
                        return
                except (RuntimeError, TypeError):
                    pass
        self._bar = on
        if self._expanded:
            return
        target = 1.0 if on else 0.0
        if abs(target - self._bar_t) < 1e-6:
            self._apply_collapsed_geometry()
            self.update()
            return
        self._save_bar()
        self._anim_timer.stop()
        if on:
            cb = getattr(self, "on_shrunk", None)
            if cb is not None:
                try:
                    cb(self, self._side_h)
                except (RuntimeError, TypeError):
                    pass
        self._anim_field = "bar"
        self._anim_from = self._bar_t
        self._anim_to = target
        self._anim_dur = 0.24
        self._anim_rect_from = self.geometry()
        self._anim_rect_to = QRect(self.box.x - self._win_margin(),
                                   self.box.y - self._win_margin(),
                                   self._win_w(), self._win_h())
        self._start_anim()

    def slide_to(self, target_y: int, dur: float = 0.22) -> None:
        """把窗口平滑挪到新的玻璃 y（挤开、吸附都走这里，别瞬移）。"""
        pad = self._win_margin()
        start = float(self.y())
        end = float(target_y - pad)
        if abs(start - end) < 0.5:
            return
        self._slide = {"t0": time.perf_counter(), "a": start, "b": end, "dur": max(0.01, dur)}
        if self._slide_timer is None:
            self._slide_timer = QTimer(self)
            self._slide_timer.setInterval(15)
            self._slide_timer.timeout.connect(self._slide_tick)
        self._slide_timer.start()

    def _slide_tick(self) -> None:
        s = self._slide
        if not s:
            self._slide_timer.stop()
            return
        t = (time.perf_counter() - s["t0"]) / s["dur"]
        if t >= 1.0:
            self.move(self.x(), int(round(s["b"])))
            self._slide = None
            self._slide_timer.stop()
            return
        ease = 1.0 - (1.0 - t) ** 3
        self.move(self.x(), int(round(s["a"] + (s["b"] - s["a"]) * ease)))

    def shake(self) -> None:
        """长不出来：左右抖两下。"""
        if getattr(self, "_shake_anim", None) is not None:
            return
        home = QPoint(self.pos())
        anim = QVariantAnimation(self)
        anim.setDuration(320)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)

        def step(value) -> None:
            t = float(value)
            off = math.sin(t * 4.0 * math.pi) * 7.0 * (1.0 - t)
            self.move(home.x() + int(round(off)), home.y())

        def done() -> None:
            self.move(home)
            self._shake_anim = None

        anim.valueChanged.connect(step)
        anim.finished.connect(done)
        self._shake_anim = anim
        anim.start()

    def _start_anim(self) -> None:
        """起一次动画：先出一帧，别让动画「等一下才开始」。"""
        self._anim_p = 0.0
        self._anim_frames = 0
        self._anim_t0 = time.perf_counter()
        self._anim_last = self._anim_t0
        self._anim_gap_max = 0.0
        # 动画期间别留着重叠区模糊图：它是按旧尺寸抓的，拉伸着画会跳
        self._backdrop = []
        self._tick_anim()
        self._anim_timer.start()

    def _tick_anim(self) -> None:
        """收放动画的一帧：按真实经过的时间取值，掉帧了也是顺着走、不会慢半拍。"""
        now = time.perf_counter()
        p = 1.0 if self._anim_dur <= 0 else (now - self._anim_t0) / self._anim_dur
        done = p >= 1.0
        eased = self._anim_curve.valueForProgress(min(1.0, p))
        self._anim_p = eased
        v = self._anim_from + (self._anim_to - self._anim_from) * eased
        if self._anim_field == "bar":
            self._on_bar_anim(v)
        else:
            self._on_anim(v)
        if done:
            self._anim_timer.stop()
            self._on_anim_finished()

    def _anim_geometry(self) -> None:
        """按这次动画的起止矩形插值窗口几何（缓动进度在 _anim_p）。"""
        a, b = self._anim_rect_from, self._anim_rect_to
        if a.isNull() or b.isNull():
            return
        p = self._anim_p
        self.setGeometry(QRect(
            round(a.x() + (b.x() - a.x()) * p),
            round(a.y() + (b.y() - a.y()) * p),
            round(a.width() + (b.width() - a.width()) * p),
            round(a.height() + (b.height() - a.height()) * p),
        ))

    def _anim_frame_stat(self) -> None:
        now = time.perf_counter()
        self._anim_frames += 1
        if self._anim_last:
            gap = now - self._anim_last
            if gap > self._anim_gap_max:
                self._anim_gap_max = gap
        self._anim_last = now

    def _on_anim(self, value) -> None:
        self._t = float(value)
        self._anim_frame_stat()
        self._anim_geometry()
        self.update()

    def _on_bar_anim(self, value) -> None:
        self._bar_t = float(value)
        self._anim_frame_stat()
        self._anim_geometry()
        self.update()

    def _on_anim_finished(self) -> None:
        dur = (time.perf_counter() - self._anim_t0) * 1000.0 if self._anim_t0 else 0.0
        _log_force("toggle anim box=%s to=%s frames=%d dur=%.0fms max_gap=%.0fms",
                   self.box.id,
                   "expanded" if self._expanded else
                   ("bar" if self._bar_t > 0.5 else "mini"),
                   self._anim_frames, dur, self._anim_gap_max * 1000.0)
        self._anim_t0 = 0.0
        if not self._expanded:
            self._bar_t = 1.0 if self._bar else 0.0
            self._apply_collapsed_geometry()
            self._set_expanded_blur(False)
            self.update()
            return
        # 展开完成：收起形态回到「缩略图标」（下次收起 / 拖动按它算）
        self._bar = False
        self._bar_t = 0.0
        self._save_bar()
        # 展开：等动画彻底落地再去抓「重叠区模糊」的底图。抓图要把压在下面的
        # 每个窗口各 grab 一次、再缩了放大，几十毫秒起步——放在动画里做，
        # 最后两帧就会明显顿一下（用户说的「不丝滑」）。
        self._set_expanded_blur(False)
        QTimer.singleShot(90, self._blur_after_anim)

    def _blur_after_anim(self) -> None:
        if self._expanded and self._t >= 0.999:
            self._set_expanded_blur(True)

    # ---------------------------------------------------------------- 重叠区模糊
    def _set_expanded_blur(self, on: bool) -> None:
        """展开后，把**压在别的盒子/卡片上**的那几块糊掉。

        注意：不是给整个盒子加模糊（那会把盒子自己的颜色也改掉，用户明确不要）。
        做法是把底下那几个窗口各自 `grab()` 成图，缩小再放大当模糊，然后**只**
        画在两者相交的那块矩形里，最后玻璃照常盖在上面——所以只有重叠部分是糊的，
        盒子外观一点不变。
        """
        self._blur_on = bool(on)
        self._refresh_backdrop()
        if self._blur_on and getattr(self, "_backdrop_timer", None) is None:
            self._backdrop_timer = QTimer(self)
            self._backdrop_timer.setInterval(700)
            self._backdrop_timer.timeout.connect(self._refresh_backdrop)
        if getattr(self, "_backdrop_timer", None) is not None:
            if self._blur_on:
                self._backdrop_timer.start()
            else:
                self._backdrop_timer.stop()
        self.update()

    def _refresh_backdrop(self) -> None:
        self._backdrop = []
        if _DRAGGING:
            return          # 正在拖窗口，抓图会让拖动卡顿，先跳过这一轮
        if not self._expanded or not self._blur_on:
            self.update()
            return
        provider = getattr(self, "siblings", None)
        if provider is None:
            return
        mine = QRect(self.pos(), self.size())
        try:
            others = list(provider())
        except (RuntimeError, TypeError):
            return
        for other in others:
            try:
                if other is self or not other.isVisible():
                    continue
                r = QRect(other.pos(), other.size())
                inter = r.intersected(mine)
                if inter.width() < 4 or inter.height() < 4:
                    continue
                pm = other.grab()
                if pm.isNull():
                    continue
                # 模糊 = 缩到很小再放大（比上 QGraphicsBlurEffect 便宜得多）
                small = pm.scaled(max(1, pm.width() // 14), max(1, pm.height() // 14),
                                  Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                blurred = small.scaled(pm.size(), Qt.IgnoreAspectRatio,
                                       Qt.SmoothTransformation)
                self._backdrop.append((r, blurred))
            except (RuntimeError, TypeError):
                continue
        self.update()

    def paint_backdrop(self, p: QPainter) -> None:
        """把「重叠到的那些窗口的模糊版」画在自己的玻璃下面。

        调用时 painter 已经平移到玻璃左上角，所以这里用玻璃坐标。
        拖动窗口期间直接跳过：那几张模糊图是整窗大小的 pixmap，一张就要几 ms，
        拖动时叠着画会让帧时间飙到 16ms 以上（实测），肉眼只觉得卡。
        """
        if not self._backdrop or _DRAGGING:
            return
        m = self._win_margin()
        path = QPainterPath()
        path.addRoundedRect(QRectF(0.0, 0.0, self._cw(), self._ch()),
                            self.radius, self.radius)
        p.save()
        p.setClipPath(path)
        for rect, pm in self._backdrop:
            # rect 是那个窗口的屏幕矩形（含各自的余量），转成自己的玻璃坐标
            tgt = rect.translated(-(self.x() + m), -(self.y() + m))
            p.drawPixmap(tgt, pm)
        p.restore()

    # ---------------------------------------------------------------- 绘制
    def paintEvent(self, event) -> None:
        _dbg = bool(os.environ.get("DESKBEAUTY_DEBUG"))
        _t0 = time.perf_counter() if _dbg else 0.0
        # 拖动时直接贴一张开始拖动前的快照：不管盒子里有多少图标、字多复杂，
        # 拖动中每帧都只是画一张位图，不会因为重绘内容多而掉帧。
        if self._window_dragging and self._drag_pm is not None:
            p = QPainter(self)
            p.setCompositionMode(QPainter.CompositionMode_Source)
            p.fillRect(self.rect(), Qt.transparent)
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            p.drawPixmap(0, 0, self._drag_pm)
            p.translate(self._win_margin(), self._win_margin())
            self._paint_cursor(p)
            if _dbg:
                self._drag_paints = getattr(self, "_drag_paints", 0) + 1
            return
        # 悬停「弹一下」只改这个比例（纯重绘，窗口尺寸不动），展开动画里从
        # 「当前缩放」平滑过渡到 1.0
        z = (1.0 - self._t) * self._hover_scale() + self._t * 1.0
        self.paint_scale = z
        super().paintEvent(event)
        p = QPainter(self)
        drag = self._window_dragging
        p.setRenderHint(QPainter.Antialiasing, not drag)
        p.setRenderHint(QPainter.SmoothPixmapTransform, not drag)
        # 和玻璃层用同一套变换：先围绕窗口中心缩放，再平移到玻璃左上角。
        # 放大后有窗口那一圈余量接着，不需要任何裁剪，圆角也不会被切方。
        if abs(z - 1.0) > 1e-6:
            c = self.rect().center()
            p.translate(c.x(), c.y())
            p.scale(z, z)
            p.translate(-c.x(), -c.y())
        p.translate(self._win_margin(), self._win_margin())
        self._paint_titlebar(p)
        # 悬停只放大、不提亮（用户要的是「不变白」）；局部重画时把裁剪区传下去
        clip = event.rect() if abs(z - 1.0) < 1e-6 else None
        if self._t >= 0.5:
            self._paint_expanded(p, min(1.0, (self._t - 0.5) * 2), clip)
        else:
            self._paint_collapsed(p, max(0.0, 1 - self._t * 2))
        # 盒内拖动换位：图标跟着光标走
        if self._internal_drag is not None and self._drag_xy is not None:
            idx = self._internal_drag
            if 0 <= idx < len(self._items):
                pm = self._icon(self._items[idx], 52)
                p.setOpacity(0.82)
                p.drawPixmap(QPoint(self._drag_xy.x() - pm.width() // 2,
                                    self._drag_xy.y() - pm.height() // 2), pm)
                p.setOpacity(1.0)
        # 有东西要落进来：整块压暗一点当提示（拖图标靠过来 / 拖盒子来合并）
        self._paint_hot(p)
        self._paint_cursor(p)
        if _dbg:
            _log("paint %.2fms drag=%s z=%.2f exp=%s box=%s",
                 (time.perf_counter() - _t0) * 1000.0,
                 self._window_dragging, z, self._expanded, self.box.id)

    # ---------------------------------------------------------------- 标题栏
    def _title_font(self) -> QFont:
        key = "title_font_e" if self._expanded else "title_font_c"
        f = getattr(self, key, None)
        if f is None:
            f = QFont("Microsoft YaHei UI")
            f.setPixelSize(13 if self._expanded else 11)
            f.setWeight(QFont.Weight.DemiBold)
            setattr(self, key, f)
        return f

    def _name_rect(self) -> QRectF:
        """左上角的名字区域（玻璃坐标）。整体下移一点，别和横杠挤在一起。"""
        cw = self._cw()
        return QRectF(7.0, 8.0,
                      max(18.0, min(cw * 0.5, cw - DOT_W - GRIP_MIN)),
                      TITLE_H - 8.0)

    def _dot_rect(self) -> QRectF:
        """右上角三个点的点击区域（整体下移一点）。"""
        return QRectF(self._cw() - 5.0 - DOT_W, 7.0, DOT_W, TITLE_H - 7.0)

    def _grip_rect(self) -> QRectF:
        """标题栏里除了三个点都是拖动区（含名字那一块）。

        用户要的是「按住顶部中间就能拖」，判定范围给足，横杠只是个提示。
        """
        return QRectF(0.0, 0.0, max(8.0, self._dot_rect().left() - 4.0), TITLE_H)

    def _grip_global(self) -> QRect:
        """拖动区在屏幕上的矩形（逻辑像素），合并判定用。"""
        m = self._win_margin()
        r = self._grip_rect()
        return QRect(self.x() + m + int(r.x()), self.y() + m + int(r.y()),
                     int(r.width()), TITLE_H)

    def _merge_hover(self) -> None:
        """拖着盒子走时：自己的拖动区压到别的盒子的拖动区上，那个盒子就展开。

        松手即合并（把图标全并过去），移开就把它收回原来的状态。
        """
        if not self._moved:
            return
        provider = getattr(self, "siblings", None)
        mine = self._grip_global()
        target = None
        if provider is not None:
            try:
                others = list(provider())
            except (RuntimeError, TypeError):
                others = []
            for w in others:
                if w is self or not isinstance(w, BoxWidget) or not w.isVisible():
                    continue
                try:
                    if mine.intersects(w._grip_global()):
                        target = w
                        break
                except (RuntimeError, TypeError):
                    continue
        if target is self._merge_target:
            return
        old = self._merge_target
        old_was_expanded = self._merge_was_expanded
        self._merge_target = target
        self._merge_was_expanded = False
        # 离开原来的目标：取消压暗；如果它是被这次拖动展开的，收回去
        if old is not None:
            try:
                old.set_hot(False)
                if not old_was_expanded:
                    old.toggle(False)
            except (RuntimeError, TypeError):
                pass
        if target is None:
            self._merge_timer.stop()
        if target is not None:
            self._merge_was_expanded = bool(target._expanded)
            target.set_hot(True)
            if not target._expanded:
                self._merge_timer.start()
            _log("merge armed -> %s", target.box.id)

    def _expand_merge_target(self) -> None:
        """指针在目标盒子上停住了：这时候才真的展开它（免得一路扫过去狂播动画）。"""
        target = self._merge_target
        if target is None or target is self or not self._moved:
            return
        try:
            if not target._expanded:
                target.toggle(True)
        except (RuntimeError, TypeError):
            pass

    def _paint_titlebar(self, p: QPainter, alpha: float = 1.0) -> None:
        """顶部一条：左名称、右三个点、中间拖动横杠。"""
        if alpha <= 0.02:
            return
        p.setOpacity(alpha)
        cw = self._cw()
        # 名称：就地改名时输入框盖在这里，自己就别再画一遍了
        # （输入框是透明底的，两遍字会叠在一起，看着就是两行字重叠）
        if not self._editing:
            font = self._title_font()
            p.setFont(font)
            p.setPen(QColor(30, 36, 48, 250))
            r = self._name_rect()
            p.drawText(r, Qt.AlignLeft | Qt.AlignVCenter,
                       _elide(self.box.name, font, int(r.width())))
        # 三个点
        dr = self._dot_rect()
        cx, cy = dr.center().x(), dr.center().y()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(58, 70, 92, 205))
        for dx in (-5.0, 0.0, 5.0):
            p.drawEllipse(QRectF(cx + dx - 1.6, cy - 1.6, 3.2, 3.2))
        # 拖动横杠：只在鼠标停在盒子上时露出来，靠上一点避免压到名字；
        # 纯白、单层、长度随盒子宽度走（用户要「白色 + 自适应 + 简洁」）
        if self._hovered and not self._editing:
            cw = self._cw()
            bar_w = max(14.0, min(60.0, cw * 0.36))
            bar = QRectF(cw / 2.0 - bar_w / 2.0, 5.0, bar_w, GRIP_H)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255,
                              150 if self._window_dragging else 96))
            p.drawRoundedRect(bar, GRIP_H / 2, GRIP_H / 2)
        p.setOpacity(1.0)

    def _paint_collapsed(self, p: QPainter, alpha: float) -> None:
        if alpha <= 0.02:
            return
        # 「只剩横杠」形态：下面那排缩略图标跟着动画淡出，收到底就一点都不画
        alpha *= max(0.0, 1.0 - self._bar_t)
        if alpha <= 0.02:
            return
        p.setOpacity(alpha)
        if not self._items:
            f = QFont("Microsoft YaHei UI")
            f.setPixelSize(26)
            p.setFont(f)
            p.setPen(QColor(70, 80, 98, 190))
            p.drawText(QRectF(0.0, TITLE_H, self._cw(), self._ch() - TITLE_H),
                       Qt.AlignCenter, "＋")
            return
        # 格子数就是容量（自动模式下最多 5×5）。装不下的时候最后一格不画图标，
        # 换成一条灰色反斜杠——比 +N 角标安静，也不会挤掉一个图标还看不出来。
        cols, rows = self._size_grid()
        capacity = cols * rows
        overflow = len(self._items) > capacity
        shown = capacity - 1 if overflow else capacity
        # 缓存的图固定按「2 倍物理像素」取：静止时是缩小采样（清晰），悬停放大
        # 时也只是缩得少一点，不会出现「这张图是放大、下一帧变成1:1」的滤波切换
        # ——那种切换肉眼就是图标闪一下。
        dpr = float(self.devicePixelRatioF() or 1.0)
        icon_px = max(16, int(round(MINI * dpr * 2)))
        for i, path in enumerate(self._items[:shown]):
            r = self._mini_rect(i)
            pm = self._icon(path, icon_px)
            p.drawPixmap(r, pm, QRectF(pm.rect()))
        if overflow:
            self._paint_more(p, self._mini_rect(capacity - 1))

    def _paint_more(self, p: QPainter, cell: QRectF) -> None:
        """放不下的那一格：一条灰色反斜杠，从格子左上拉到右下。"""
        d = float(min(cell.width(), cell.height()))
        ins = max(3.5, d * 0.16)          # 别贴着格子边缘，否则抗锯齿会糊成一团
        pen = QPen(QColor(86, 96, 114, 195))
        pen.setWidthF(max(2.4, d * 0.085))   # 34px 格子约 2.9px，比头发线明显
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawLine(QPointF(cell.left() + ins, cell.top() + ins),
                   QPointF(cell.right() - ins, cell.bottom() - ins))

    def _paint_expanded(self, p: QPainter, alpha: float,
                        clip: QRect | None = None) -> None:
        if alpha <= 0.02:
            return
        p.setOpacity(alpha)
        label_font = QFont("Microsoft YaHei UI")
        label_font.setPixelSize(11)
        selected = self._selected
        band = self._band
        p.setFont(label_font)
        # 局部重画时跳过框外的图标：省下的正是框选时最贵的那部分
        m = self._win_margin()
        clip_glass = None if clip is None else clip.translated(-m, -m)
        order = self._display_order()
        for slot_i, idx in enumerate(order):
            if idx == self._internal_drag:
                continue            # 拖着的那个原地消失，跟着光标画
            path = self._items[idx]
            slot = self._item_rect(slot_i)
            ox, oy = self._slot_offset(idx)
            if ox or oy:
                slot = slot.translated(int(round(ox)), int(round(oy)))
            if clip_glass is not None and not clip_glass.intersects(slot):
                continue
            if idx in selected:
                # 选中反馈：一层淡灰圆角底，没有边框（用户不要蓝框，也不要蓝色）
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(120, 126, 138, 62))
                p.drawRoundedRect(QRect(slot.left() + 2, slot.top() + 1,
                                        CELL_W - 4, CELL_H - 4), 9.0, 9.0)
                p.setBrush(Qt.NoBrush)
            icon_rect = QRect(slot.left(), slot.top() + 6, CELL_W, 52)
            pm = self._icon(path, 52)
            # 鼠标停在这个图标上：往上跳一下、落回去、再弹一小下
            # _bounce_offset 返回负值表示「往上」，所以这里是 + lift
            lift = int(round(self._bounce_offset(idx)))
            p.drawPixmap(
                QPoint(icon_rect.center().x() - pm.width() // 2,
                       icon_rect.top() + (52 - pm.height()) // 2 + lift), pm)
            p.setPen(QColor(26, 32, 42, 244))
            name = path.stem if path.suffix.lower() in (".lnk", ".url") else path.name
            key = str(path)
            text = self._label_cache.get(key)
            if text is None:
                text = _elide(name, label_font, CELL_W - 8)
                self._label_cache[key] = text
            p.drawText(QRect(slot.left() + 2, slot.top() + 62, CELL_W - 4, 18),
                       Qt.AlignHCenter | Qt.AlignTop, text)

        # 拉框时把框住的区域铺一层灰（不画边框）
        if band is not None and band.width() > 2 and band.height() > 2:
            p.setOpacity(1.0)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(104, 110, 122, 122))
            p.drawRect(band)
            p.setBrush(Qt.NoBrush)

    # ---------------------------------------------------------------- 鼠标
    def _display_order(self) -> list[int]:
        """画图标时用的顺序：平时就是原顺序，拖拽换位时用临时顺序。"""
        if self._internal_drag is not None and self._drag_preview \
                and len(self._drag_preview) == len(self._items):
            return self._drag_preview
        return list(range(len(self._items)))

    def _slot_index(self, pos: QPoint) -> int:
        """指针落在第几个格子上（超出范围就往两边夹）。"""
        n = len(self._items)
        if n <= 0:
            return 0
        col = int((pos.x() - PAD) // CELL_W)
        row = int((pos.y() - HEADER) // CELL_H)
        col = max(0, min(COLS - 1, col))
        row = max(0, row)
        return max(0, min(n - 1, row * COLS + col))

    def _reorder_to(self, pos: QPoint) -> None:
        """手机式换位：把拖着的图标插到指针所在格，中间那些顺次补齐。

        拖着 A 经过 B 时，A 原位留空，B 及之后的图标往前顶一格，把 A 的位置
        腾到指针底下；反方向同理。松手才真正写进 order。
        """
        src = self._internal_drag
        preview = self._drag_preview
        if src is None or preview is None or len(preview) < 2:
            return
        slot = self._slot_index(pos)
        rest = [i for i in preview if i != src]
        k = max(0, min(int(slot), len(rest)))
        new = rest[:k] + [src] + rest[k:]
        if new != preview:
            self._slide_to(preview, new)
            self._drag_preview = new

    # ---------------------------------------------------------------- 换位滑动
    def _slide_to(self, old: list[int], new: list[int]) -> None:
        """顺序变了：让被顶开的图标从旧格子滑到新格子，别瞬移。"""
        old_slot = {idx: pos for pos, idx in enumerate(old)}
        new_slot = {idx: pos for pos, idx in enumerate(new)}
        now = time.monotonic()
        dur = SLIDE_MS / 1000.0
        for idx, n in new_slot.items():
            o = old_slot.get(idx)
            if o is None or o == n:
                continue
            # 上一段还没滑完的话，从它「现在画在哪儿」接着滑，别跳回格子
            cur_dx = cur_dy = 0.0
            cur = self._slot_ofs.get(idx)
            if cur is not None:
                p = (now - cur[2]) / dur
                left = max(0.0, 1.0 - p) ** 2
                cur_dx, cur_dy = cur[0] * left, cur[1] * left
            dx = ((o % COLS) - (n % COLS)) * CELL_W + cur_dx
            dy = ((o // COLS) - (n // COLS)) * CELL_H + cur_dy
            self._slot_ofs[idx] = [float(dx), float(dy), now]
        if self._slot_ofs and not self._move_timer.isActive():
            self._move_timer.start()

    def _slot_offset(self, index: int) -> tuple[float, float]:
        """这个图标现在离它的格子还有多远（越接近 0 越到位）。"""
        item = self._slot_ofs.get(index)
        if not item:
            return 0.0, 0.0
        dx, dy, t0 = item
        p = (time.monotonic() - t0) / (SLIDE_MS / 1000.0)
        if p >= 1.0:
            self._slot_ofs.pop(index, None)
            return 0.0, 0.0
        # 剩余比例用 (1-p)^2：起步就算在旧位置、收尾慢慢贴合
        left = max(0.0, 1.0 - p) ** 2
        return dx * left, dy * left

    def _tick_moves(self) -> None:
        now = time.monotonic()
        for idx in list(self._slot_ofs):
            if (now - self._slot_ofs[idx][2]) * 1000.0 >= SLIDE_MS:
                self._slot_ofs.pop(idx, None)
        if not self._slot_ofs:
            self._move_timer.stop()
        self.update()

    def _bounce_offset(self, index: int) -> float:
        """图标「跳一下再弹一小下」的竖直位移（负值 = 往上）。

        每个图标各算各的进度，多个图标可以同时跳。
        """
        t = self._bounce_p.get(index)
        if t is None or t <= 0.0:
            return 0.0
        if t < 0.55:
            return -9.0 * math.sin(math.pi * (t / 0.55))    # 主跳
        u = (t - 0.55) / 0.45
        return -3.5 * math.sin(math.pi * u)                 # 回弹

    def _track_hover_item(self, pos: QPoint) -> None:
        """鼠标经过某个图标 → 给这个图标排一段弹跳（别的图标照常各播各的）。"""
        if _DRAGGING and not self._window_dragging:
            return
        if self._band_origin is not None:
            return          # 正在拉框选图标：别再叠一层弹跳动画去抢重绘
        if not self._expanded:
            self._hover_index = None
            return
        idx = self._item_at(pos)
        if idx == self._hover_index:
            return          # 停在同一个图标上不重复播
        self._hover_index = idx
        if idx is None or idx in self._bounce_p:
            return
        self._bounce_p[idx] = 0.0
        if not self._bounce_timer.isActive():
            self._last_tick = time.monotonic()
            self._bounce_timer.start()
        _log("bounce start idx=%s", idx)

    def _tick_bounce(self) -> None:
        now = time.monotonic()
        dt = now - self._last_tick
        self._last_tick = now
        for idx in list(self._bounce_p):
            self._bounce_p[idx] += dt / 0.62
            if self._bounce_p[idx] >= 1.0:
                self._bounce_p.pop(idx, None)
        if not self._bounce_p:
            self._bounce_timer.stop()
        self.update()

    def enterEvent(self, event) -> None:
        self._hovered = True
        # 别的盒子正在被拖：不要跟着播「弹一下」、也别显示横杠。
        # 拖窗口时窗口总比鼠标慢半拍，鼠标会瞬间压到下面的盒子上，一个个
        # 触发悬停动画——几个盒子同时做缩放重绘，拖动就会卡成幻灯片。
        if _DRAGGING and not self._window_dragging:
            self._hovered = False
            super().enterEvent(event)
            return
        # 只要鼠标经过就开始放「放大→缩回」；这一段没播完之前再经过也不重播
        if (not self._expanded and self._press_global is None
                and not self._pulse_busy()):
            self._start_pulse()
        self.update()
        super().enterEvent(event)

    def _pulse_busy(self) -> bool:
        # 用时间戳而不是「动画播完的回调」：动画被 stop()（比如按下去展开）
        # 时 finished 根本不会发，用布尔量会永远卡住
        return time.monotonic() < self._hover_until

    def _start_pulse(self) -> None:
        self._hover_until = time.monotonic() + HOVER_TOTAL
        _log("hover pulse start")
        if self._expanded or self._press_global is not None:
            return
        self._pulse_anim.stop()
        self._pulse_anim.setDuration(HOVER_MS)
        self._pulse_anim.setStartValue(self._hover_t)
        self._pulse_anim.setEndValue(1.0)
        self._pulse_anim.start()

    def _on_pulse(self, value) -> None:
        """内容整体放大/缩回：只重绘，窗口一像素都不动。"""
        self._hover_t = float(value)
        self.update()

    # ---------------------------------------------------------------- 落点提示
    def set_hot(self, on: bool) -> None:
        """拖着东西压在盒子上：玻璃压暗一点。on=False 时淡回原色。"""
        on = bool(on)
        if on == self._hot:
            return
        self._hot = on
        self._sync_hot()

    def set_selected(self, on: bool) -> None:
        """框选中 / 取消：和「有东西拖进来」同一个压暗效果。"""
        on = bool(on)
        if on == self._is_selected:
            return
        self._is_selected = on
        self._sync_hot()

    def _sync_hot(self) -> None:
        """两种来源（拖放悬停、框选中）叠在一起算：只要有一个成立就压暗。"""
        want = bool(self._hot or self._is_selected)
        self._hot_anim.stop()
        self._hot_anim.setStartValue(self._hot_t)
        self._hot_anim.setEndValue(1.0 if want else 0.0)
        self._hot_anim.start()

    def _on_hot(self, value) -> None:
        self._hot_t = float(value)
        # 不是盖一层半透明色（那会把图标一起压暗、看着发灰），而是把**玻璃底色**
        # 稍微调黑一点：tint 是画在玻璃上、内容之前的，图标文字不受影响。
        if self._hot_t <= 0.01:
            self.tint = None
        else:
            self.tint = QColor(18, 24, 38, int(round(34 * self._hot_t)))
        self.update()

    def _band_frame(self) -> None:
        """框选中的节拍：16ms 一次，鼠标报得再快也只算、只画这么多帧。"""
        if self._band_origin is None:
            self._band_timer.stop()
            return
        self._band_update()          # 把最新鼠标位置算进去（一帧最多一次）
        area = self._band_pending
        self._band_pending = None
        if area is not None and not area.isEmpty():
            self.update(area)
        else:
            self.update()

    def _band_update(self) -> None:
        """按最新鼠标位置重算框选和选中项（不含重画）。"""
        latest = self._band_latest
        if self._band_origin is None or latest is None:
            return
        old_band = self._band
        band = QRect(self._band_origin, latest).normalized()
        selected = {
            i for i in range(len(self._items))
            if self._item_rect(i).intersects(band)
        }
        changed = self._selected ^ selected
        self._band = band
        self._selected = selected
        self._band_dirty(old_band, band, changed)

    def _band_dirty(self, old: QRect | None, new: QRect,
                    changed: set) -> None:
        """框选时只标一小块：旧框、新框、选中状态变了的格子、再加上光标那一圈。"""
        m = self._win_margin()
        area = QRect(new) if old is None else QRect(old).united(new)
        for i in changed:
            area = area.united(self._item_rect(i))
        area.translate(m, m)
        area.adjust(-3, -3, 3, 3)
        if self._cursor_on:
            # 盒子里的圆点和拖影是自己画的，光标走过的地方也得一起重画
            cx = int(self._cur_pt.x()) + m
            cy = int(self._cur_pt.y()) + m
            area = area.united(QRect(cx - 46, cy - 46, 92, 92))
        self._band_pending = area if self._band_pending is None \
            else self._band_pending.united(area)

    def _paint_hot(self, p: QPainter) -> None:
        """老做法：玻璃上盖一层深色。现在改用 tint（只暗底色、不动图标），保留空实现。"""
        return None

    def _pulse_restore(self) -> None:
        """收尾：把放大比例清零（窗口本来就没动过）。"""
        self._pulse_anim.stop()
        self._hover_t = 0.0
        self.paint_scale = 1.0
        self.update()

    def _on_pulse_finished(self) -> None:
        if self._expanded or self._press_global is not None:
            self._pulse_restore()
            return
        if self._hover_t >= 0.99:
            # 放大到位 → 缩回去
            self._pulse_anim.setDuration(HOVER_MS + 40)
            self._pulse_anim.setStartValue(1.0)
            self._pulse_anim.setEndValue(0.0)
            self._pulse_anim.start()
        else:
            self._pulse_restore()

    def leaveEvent(self, event) -> None:
        # 注意：这里**不**打断动画——用户要求「一开始就播完整段」
        self._hovered = False
        self._hover_index = None
        self.update()
        super().leaveEvent(event)

    # ---------------------------------------------------------------- 自定义指针
    def _cursor_enter(self) -> None:
        """鼠标进盒子里：藏掉系统光标，自己画圆点 + 拖影。"""
        try:
            pos = self.mapFromGlobal(QCursor.pos())
        except RuntimeError:
            return
        here = QPointF(self._to_content(pos))
        self._cur_pt = QPointF(here)
        self._cur_last = QPointF(here)
        self._trail = [QPointF(here) for _ in range(CURSOR_TRAIL)]
        self._trail_act = 0.0
        self._cursor_on = True
        self.setCursor(Qt.BlankCursor)
        if self._trail_timer is None:
            self._trail_timer = QTimer(self)
            self._trail_timer.setInterval(16)
            self._trail_timer.timeout.connect(self._tick_trail)
        self._trail_stamp = 0.0
        self._trail_timer.start()
        self.update()

    def _cursor_leave(self) -> None:
        if not self._cursor_on:
            return
        self._cursor_on = False
        self.unsetCursor()
        if self._trail_timer is not None:
            self._trail_timer.stop()
        self._trail_act = 0.0

    def _tick_trail(self) -> None:
        """拖影链：每个点追前一个点，鼠标快就拉长，停下就收拢淡出。"""
        now = time.perf_counter()
        dt = min(0.064, now - self._trail_stamp) if self._trail_stamp else 1.0 / 60.0
        self._trail_stamp = now
        ratio = max(0.001, dt / (1.0 / 60.0))
        speed = (self._cur_pt - self._cur_last).manhattanLength() / ratio
        self._cur_last = QPointF(self._cur_pt)
        target = min(1.0, speed / 7.0)
        self._trail_act += (target - self._trail_act) * (1.0 - 0.72 ** ratio)
        from_pt = QPointF(self._cur_pt)
        settled = True
        for i, node in enumerate(self._trail):
            follow = 0.45 - i * 0.02
            k = 1.0 - (1.0 - follow) ** ratio
            node += (from_pt - node) * k
            from_pt = QPointF(node)
            if (node - self._cur_pt).manhattanLength() > 0.3:
                settled = False
        if self._band_origin is not None:
            # 拉框期间由 _band_timer 统一按 60fps 重画（它连光标一起画），
            # 这里再 update 一次就是双倍开销
            return
        self.update()
        if settled and self._trail_act < 0.01:
            self._trail_act = 0.0
            if self._trail_timer is not None:
                self._trail_timer.stop()

    def _paint_cursor(self, p: QPainter) -> None:
        if not self._cursor_on:
            return
        n = getattr(self, "_cur_dbg", 0)
        if n < 3:
            self._cur_dbg = n + 1
            _log_force("cursor paint at %s,%s", self._cur_pt.x(), self._cur_pt.y())
        p.setOpacity(1.0)
        p.setPen(Qt.NoPen)
        p.setOpacity(1.0)
        p.setPen(Qt.NoPen)
        cx, cy = self._cur_pt.x(), self._cur_pt.y()
        # 拖影（由内到外：越靠后越小、越淡）
        for i, node in enumerate(self._trail):
            weight = 1.0 - i / (CURSOR_TRAIL + 1.0)
            a = 0.72 * weight * self._trail_act
            if a <= 0.01:
                continue
            size = CURSOR_DOT - i * 0.85
            r = size / 2.0
            g = QRadialGradient(node, r)
            c0 = QColor(CURSOR_TRAIL_C)
            c0.setAlphaF(min(1.0, 0.65 * a * 2.2))
            g.setColorAt(0.0, c0)
            g.setColorAt(0.72, c0)
            g.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setBrush(g)
            p.drawEllipse(node, r, r)
        # 光晕
        hr = CURSOR_HALO / 2.0
        hg = QRadialGradient(QPointF(cx, cy), hr)
        hc = QColor(CURSOR_GLOW)
        hc.setAlphaF(0.35 * 0.5)
        hg.setColorAt(0.0, hc)
        hg.setColorAt(0.7, QColor(0, 0, 0, 0))
        p.setBrush(hg)
        p.drawEllipse(QPointF(cx, cy), hr, hr)
        # 主圆点：扁平的实心灰点 + 左上高光（不做羽化，不然像颗玻璃球）
        dr = CURSOR_DOT / 2.0
        p.setBrush(CURSOR_CORE)
        p.drawEllipse(QPointF(cx, cy), dr, dr)
        hg = QRadialGradient(QPointF(cx - dr * 0.34, cy - dr * 0.34), dr * 0.62)
        hg.setColorAt(0.0, QColor(255, 255, 255, 172))
        hg.setColorAt(0.55, QColor(255, 255, 255, 40))
        hg.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(hg)
        p.drawEllipse(QPointF(cx, cy), dr, dr)

    def _begin_grip_press(self, event) -> None:
        """按下拖动区：先只记录，真拖起来才搬盒子，没拖就是一次「点击」。"""
        # 悬停「弹一下」的动画先停掉，否则按下的那一刻盒子还在放大状态
        if self._hover_t > 0.001 or self._pulse_anim.state() == QVariantAnimation.State.Running:
            self._pulse_restore()
        self._drag_area = True
        self._press_global = event.globalPosition().toPoint()
        self._win_pos = self.pos()
        self._moved = False
        self._item_drag = False
        self._pressed_item = None
        self._drag_t0 = time.perf_counter()
        self._drag_moves = 0
        self._drag_move_ms = 0.0
        # 多选状态下拖这个盒子 = 拖整组：记下兄弟们现在的窗口位置
        self._drag_group = groupmove.begin(self)
        self._group_origin = None
        self._group_starts = []
        if self._drag_group:
            self._group_origin = QPoint(self.pos())
            self._group_starts = [(w, QPoint(w.pos())) for w in self._drag_group]

    def _state(self) -> int:
        """盒子三种形态（用户定的语义）：

        -1 = 只剩那条可拖拽的横杠；0 = 正常（带缩略图标）；1 = 展开。
        """
        if self._expanded:
            return 1
        return -1 if self._bar else 0

    def _grip_click(self, gpos: QPoint) -> None:
        """拖动区上一次完整的「按下 + 抬起」。

        单击：只记下时间，什么都不做（点这儿不许把盒子展开）。
        连着两次完整点击（间隔在系统双击时间内、位置基本没挪）才算双击，
        按用户定的状态机走：
          1（展开）  --双击拖动区--> -1（只剩横杠）
          -1（横杠） --双击拖动区--> 0（正常）
          0（正常）  --双击拖动区--> -1（只剩横杠）
        ``mouseDoubleClickEvent`` 里那次按下也会走到这里，所以数的是**两次
        真正按下又抬起**，按住不放或者第二下拖走了都不算。
        """
        now = time.monotonic()
        try:
            interval = max(0.25, float(QApplication.doubleClickInterval()) / 1000.0)
        except (RuntimeError, TypeError, ValueError):
            interval = 0.5
        prev_t = self._grip_click_t
        prev_p = self._grip_click_pos
        near = prev_p is not None and (gpos - prev_p).manhattanLength() <= 12
        if prev_t and near and now - prev_t <= interval:
            self._grip_click_t = 0.0
            self._grip_click_pos = None
            st = self._state()
            _log_force("grip double click box=%s dt=%.0fms %d->%d", self.box.id,
                       (now - prev_t) * 1000.0, st, 0 if st == -1 else -1)
            if st == 1:
                # 展开 → -1：一步收到只剩横杠（缩略图标这一路都不画，
                # 不然动画最后一帧会「啪」地消失）
                self._bar = True
                self._bar_t = 1.0
                self._save_bar()
                self.toggle(False)
            elif st == -1:
                # -1 → 0：横杠长回正常大小，缩略图标跟着淡进来
                self._set_bar(False)
            else:
                # 0 → -1：正常 → 只剩横杠
                self._set_bar(True)
        else:
            self._grip_click_t = now
            self._grip_click_pos = QPoint(gpos)
            _log("grip click at %s ignored (single)", gpos)

    def mousePressEvent(self, event) -> None:
        pos = self._to_content(event.position().toPoint())
        _log("press raw pos=%s button=%s anim=%s", pos, event.button(),
             self._anim_timer.isActive())
        if event.button() == Qt.RightButton:
            # 右键由 contextMenuEvent 处理；这里吞掉，防止穿透到底下的桌面图标
            event.accept()
            return
        if event.button() == Qt.MiddleButton:
            # 滚轮键：把选中的图标拆成新盒子，摆在盒子外面
            event.accept()
            self._box_from_selection(pos)
            return
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        # 在盒子上按下 = 结束了「框选」这个状态：除非这个盒子本来就在选中的那组里
        cb = getattr(self, "on_widget_press", None)
        if cb is not None:
            try:
                cb(self)
            except (RuntimeError, TypeError):
                pass
        # 记下这一下按在不在盒子本体里。松手时还要再对一次，两次都落在
        # 盒子里才算「点了一下」，按下之后把鼠标挪到外面再松手不算。
        self._press_in_glass = self._in_glass(event.position().toPoint())
        if self._editing:
            # 正在改名字：这一下只负责结束改名，绝不能顺手把盒子拖走
            self._finish_inline_rename(True)
            event.accept()
            return
        # 悬停「弹一下」的动画先停掉，否则按下的那一刻盒子还在放大状态
        if self._hover_t > 0.001 or self._pulse_anim.state() == QVariantAnimation.State.Running:
            self._pulse_restore()
        # 右上角三个点 → 功能菜单（放到下一轮事件里弹：菜单是模态循环，
        # 在按下事件里直接 exec 会把整个按下处理卡住，慢事件日志记成几十秒）
        if self._dot_rect().contains(pos.x(), pos.y()):
            self._press_global = None
            self._win_pos = None
            self._moved = False
            QTimer.singleShot(0, self._open_box_menu)
            event.accept()
            return
        # 只有按在顶部那条横杠上才能拖盒子（用户要求的「只能拖这儿」）
        if self._grip_rect().contains(pos.x(), pos.y()):
            self._begin_grip_press(event)
            event.accept()
            return
        # 按在身子上：不拖盒子，只处理点开 / 选图标
        self._drag_area = False
        self._press_global = None
        self._win_pos = None
        self._moved = False
        self._item_drag = False
        item = self._item_at(pos)
        self._pressed_item = item
        self._press_item_pos = pos
        if self._expanded and item is None and pos.y() >= TITLE_H:
            # 空白处按下 → 拉框选图标（按住 Ctrl 时在已有选择上追加）
            self._band_origin = pos
            self._band = QRect(pos, QSize(1, 1))
            if not (event.modifiers() & Qt.ControlModifier):
                self._selected = set()
            self.update()
        elif self._expanded and item is not None \
                and (event.modifiers() & Qt.ControlModifier):
            # Ctrl + 点图标：把这一个加进选择 / 从选择里去掉
            if item in self._selected:
                self._selected.discard(item)
            else:
                self._selected.add(item)
            self._pressed_item = None
            self.update()
        event.accept()

    def _apply_drag_move(self) -> None:
        t = self._drag_target
        if t is None:
            self._drag_timer.stop()
            return
        if t != self.pos():
            self.move(t)
            self._drag_moves = getattr(self, "_drag_moves", 0) + 1

    def mouseMoveEvent(self, event) -> None:
        global _DRAGGING
        here = self._to_content(event.position().toPoint())
        if self._cursor_on:
            self._cur_pt = QPointF(here)
            if self._trail_timer is not None and not self._trail_timer.isActive():
                self._trail_stamp = 0.0
                self._trail_timer.start()
        # 悬停高亮每帧最多算一次：鼠标 1000Hz 时这个循环一遍图标也要几百毫秒/秒
        now = time.perf_counter()
        if now - self._hover_at >= 0.006:
            self._hover_at = now
            self._track_hover_item(here)
        if not (event.buttons() & Qt.LeftButton):
            # 没有按着左键 —— 说明之前那次按下没收到 release。把状态清干净，
            # 否则鼠标一动盒子就自己跟着跑了。
            self._press_global = None
            self._win_pos = None
            self._moved = False
            self._drag_area = False
            self._window_dragging = False
            _DRAGGING = False
            self._drag_target = None
            self._drag_timer.stop()
            self._pressed_item = None
            self._internal_drag = None
            self._drag_preview = None
            if self._band_origin is not None:
                self._band_origin = None
                self._band = None
                self._band_timer.stop()
                self._band_pending = None
                self.update()
            return
        if self._band_origin is not None:
            # ⚠️ 框选的重算（每个图标算一次矩形、求交）**不能每个鼠标事件都做**：
            # 按住不动、只晃鼠标时鼠标每秒报上千次，这里就是几百毫秒/秒的 Python
            # 开销，事件队列永远不空，Qt 的定时器（以前画圆点的那个）就轮不上，
            # 表现就是「按住盒子晃鼠标，鼠标一顿一顿」。改成每帧最多算一次，
            # 用最新位置重算；重画仍旧交给 _band_timer（60fps、只画动过的那块）。
            self._band_latest = QPoint(here)
            now = time.perf_counter()
            if now - self._band_at >= 0.006:
                self._band_at = now
                self._band_update()
            if not self._band_timer.isActive():
                self._band_timer.start()
            return
        if self._pressed_item is not None and self._expanded and self._internal_drag is None:
            # 在盒子内部拖图标：先不启动 QDrag（可能是换位），跟手移动
            if (here - self._press_item_pos).manhattanLength() > 4:
                self._internal_drag = self._pressed_item
                # 按住的图标在框选里、而且选了不止一个 → 一起拖出去
                if (self._pressed_item in self._selected
                        and len(self._selected) > 1):
                    self._drag_set = sorted(self._selected)
                else:
                    self._drag_set = [self._pressed_item]
                self._selected = set(self._drag_set)
                self._pressed_item = None
                self._drag_xy = here
                self._drag_preview = list(range(len(self._items)))
                self._slot_ofs.clear()
                self._bounce_p.pop(self._internal_drag, None)
                self.update()
                return
        if self._internal_drag is not None:
            # 盒内拖动：图标跟着光标走；一旦拖出盒子就转成「拖到桌面」
            self._drag_xy = here
            if not self.rect().contains(event.position().toPoint()):
                idx = self._internal_drag
                group = self._drag_set or [idx]
                self._internal_drag = None
                self._drag_xy = None
                self._drag_preview = None
                self._selected = set()
                self.set_acrylic(True)
                self._start_item_drag(group)
                return
            self._reorder_to(here)
            self.update()
            return
        if self._press_global is None or self._item_drag:
            return
        delta = event.globalPosition().toPoint() - self._press_global
        _log("move delta=%s moved=%s", delta, self._moved)
        if not self._moved and abs(delta.x()) + abs(delta.y()) > 4:
            self._moved = True
            self._window_dragging = True
            self._drag_paints = 0
            # 把被拖的窗口提到最前：用户的最新线索是「只有最后建的那个不卡」，
            # 也就是 z-order 越靠后越卡——先排除被别的窗口压着合成这一项。
            try:
                self.raise_()
            except RuntimeError:
                pass
            try:
                self._drag_pm = self.grab()      # 抓一张当前样子的快照
            except RuntimeError:
                self._drag_pm = None
            _DRAGGING = True
            if getattr(self, "_backdrop_timer", None) is not None:
                self._backdrop_timer.stop()
            self.set_acrylic(False)
            self.update()
        if not self._moved:
            return
        target = self._win_pos + delta
        if target != self.pos():
            # 每个 mouseMove 直接搬窗口（定时器驱动会落后鼠标一点，手感更差），
            # 但一帧最多搬一次：鼠标 1000Hz 时每秒上千个事件，每个都搬只会让
            # DWM 白合成分量帧，还把别的窗口/指针的定时器饿死。
            self._drag_target = target
            now = time.perf_counter()
            if now - self._box_move_at >= 0.005:
                self._box_move_at = now
                _mt = time.perf_counter()
                self.move(target)
                # 多选状态：兄弟们跟着同一个位移走，相对位置一个像素都不变
                groupmove.follow(self, self._group_origin, self._group_starts)
                self._drag_move_ms = getattr(self, "_drag_move_ms", 0.0) + \
                    (time.perf_counter() - _mt) * 1000.0
                self._drag_moves = getattr(self, "_drag_moves", 0) + 1
            _log("moved to %s actual %s", target, self.pos())
        self._merge_hover()

    def mouseReleaseEvent(self, event) -> None:
        global _DRAGGING
        if event.button() != Qt.LeftButton:
            # 右键松开也会走到这里。以前没判断按键，于是「右键一下」被当成
            # 「左键点了一下」→ 盒子当场展开（然后右键菜单又弹出来）。
            return
        # 这一下松手是不是「拖动区上的一次点击」：先取走，任何分支返回都不会漏状态
        was_grip = self._drag_area
        self._drag_area = False
        pos = self._to_content(event.position().toPoint())
        # 松手这个点落在不在盒子里：只认「按下在盒内 + 松开也在盒内」为点击。
        in_glass = self._in_glass(event.position().toPoint())
        _log("release moved=%s press=%s pos=%s in=%s",
             self._moved, self._press_global, self.pos(), in_glass)
        if self._band_origin is not None:
            tiny = self._band is not None and self._band.width() < 4 \
                and self._band.height() < 4
            self._band_origin = None
            self._band = None
            self._band_timer.stop()
            self._band_pending = None
            if tiny:
                self._selected = set()
            self.update()
            return
        if self._internal_drag is not None:
            self._finish_internal_drag(pos)
            return
        was_moved = self._moved
        # 松手前把最后一次目标位置落实，再停掉定时器
        self._apply_drag_move()
        self._drag_target = None
        self._drag_timer.stop()
        self._press_global = None
        self._pressed_item = None
        self._window_dragging = False
        self.update()

        if was_moved:
            if was_grip:
                # 这几下是在拖动区按下去又拖走了：不算「点击」，把上一次点击作废
                self._grip_click_t = 0.0
                self._grip_click_pos = None
            # 合并手势：拖动区压住另一个盒子的拖动区，松手就把图标全并过去
            self._merge_timer.stop()
            target = self._merge_target
            if target is not None:
                still = False
                try:
                    still = self._grip_global().intersects(target._grip_global())
                except (RuntimeError, TypeError):
                    still = False
                self._merge_target = None
                try:
                    target.set_hot(False)
                except (RuntimeError, TypeError):
                    pass
                if still:
                    cb = getattr(self, "on_merge", None)
                    if cb is not None:
                        _DRAGGING = False
                        _log("merge into %s", target.box.id)
                        cb(self, target)
                        return
                if not self._merge_was_expanded:
                    try:
                        target.toggle(False)
                    except (RuntimeError, TypeError):
                        pass
            self.set_acrylic(True)
            _DRAGGING = False
            if self._expanded and self._blur_on \
                    and getattr(self, "_backdrop_timer", None) is not None:
                self._backdrop_timer.start()
            self._log_state_drag()
            _log("drag done: %d moves in %.0fms",
                 getattr(self, "_drag_moves", 0),
                 (time.perf_counter() - getattr(self, "_drag_t0", time.perf_counter())) * 1000.0)
            _log("drag move time total %.1fms",
                 getattr(self, "_drag_move_ms", 0.0))
            self._drag_move_ms = 0.0
            # 窗口比盒子本体每边多一圈透明余量，存位置时把余量减掉
            pad = self._win_margin()
            if self._drag_group:
                self._finish_group_drag()
                self._drag_group = []
                self._group_origin = None
                self._group_starts = []
                return
            self.box.x, self.box.y = self.x() + pad, self.y() + pad
            _log("before clamp pos=%s box=%s", self.pos(), (self.box.x, self.box.y))
            self._clamp_to_screen()
            _log("after clamp pos=%s box=%s", self.pos(), (self.box.x, self.box.y))
            self._avoid_overlap()
            self.store.move(self.box, self.box.x, self.box.y)
            self.changed.emit()
            # 盒子拖到哪儿，压在下面的桌面图标就挪开——放到事件之后再做，
            # 免得松手瞬间卡一下
            QTimer.singleShot(0, self._push_desktop_icons)
            return

        # 按在拖动区、又没有真的拖走 → 这是一次「点击」。单击什么都不做，
        # 只有连着两次完整点击（按下+抬起各两回）才收起 / 展开。
        if was_grip:
            clicked = self._press_in_glass and in_glass
            self._press_in_glass = False
            if clicked:
                self._grip_click(event.globalPosition().toPoint())
            return

        # 没拖动 = 点了一下。按下和松开必须都在盒子里：按在盒子上、把鼠标
        # 挪到外面（桌面上、别的窗口上）再松手，那是误触，不能把盒子打开。
        if not (self._press_in_glass and in_glass):
            self._press_in_glass = False
            _log("release outside glass -> ignore click")
            return
        self._press_in_glass = False
        if not self._expanded:
            self.toggle(True)
        elif pos.y() >= TITLE_H:
            self._selected = set()
            self.update()

    def _log_state_drag(self) -> None:
        _log_force("DRAGSTAT box=%s icons=%d size=%dx%d paints=%d move_ms=%.1f",
                   self.box.id, len(self._items), self._side_w, self._collapsed_h(),
                   getattr(self, "_drag_paints", 0),
                   getattr(self, "_drag_move_ms", 0.0))
        self._drag_pm = None
        self._drag_paints = 0
        self._drag_move_ms = 0.0

    def _finish_internal_drag(self, pos: QPoint) -> None:
        """盒内换位：拖到哪儿就把图标插到哪儿（中间那些已经自动补齐了）。"""
        src = self._internal_drag
        group = list(self._drag_set)
        preview = self._drag_preview
        self._internal_drag = None
        self._drag_set = []
        self._drag_xy = None
        self._drag_preview = None
        if src is None:
            return
        if len(group) > 1:
            # 框选了多个再拖回盒内：不做换位，保持选中
            self._selected = set(group)
            self.update()
            return
        if preview and len(preview) == len(self._items) \
                and preview != list(range(len(self._items))):
            names = [self._items[i].name for i in preview]
            # 还没滑完的动画：下标会随着新顺序变，按名字搬到新下标上，
            # 否则残留的偏移会套到别的图标头上（松手瞬间抽一下）
            pending = {self._items[i].name: ofs for i, ofs in self._slot_ofs.items()}
            self.store.set_order(self.box, names)
            self._items = self.store.items(self.box)
            self.changed.emit()
            if pending:
                index_now = {p.name: i for i, p in enumerate(self._items)}
                self._slot_ofs = {index_now[n]: v for n, v in pending.items()
                                  if n in index_now}
            _log("reorder: %s", [p.name for p in self._items][:12])
        self.update()

    # ---- 多选一起搬（groupmove 要的两个钩子）----
    def _group_rect(self) -> QRect:
        """这块盒子在屏幕上占的玻璃矩形（逻辑像素）——整组夹屏幕时用它。

        ⚠️ 必须按**窗口当前位置**算，不能按 `box.x/y`：拖动过程中只有窗口在动，
        `box.x/y` 要等松手才更新。按旧坐标算的话，整组收尾会把这个盒子拽回起点。
        """
        m = self._win_margin()
        if self._expanded:
            return QRect(self.x() + m, self.y() + m,
                         max(1, self.width() - 2 * m), max(1, self.height() - 2 * m))
        return QRect(self.x() + m, self.y() + m, self._side_w, self._collapsed_h())

    def _group_shift(self, dx: int, dy: int) -> None:
        """整组搬完之后：按新的玻璃左上角落地、落盘、把压住的桌面图标挤开。"""
        r = self._group_rect().translated(dx, dy)
        self.box.x, self.box.y = r.x(), r.y()
        if self._expanded:
            self._rect_expanded = self._expanded_rect()
            self.setGeometry(self._rect_expanded)
        else:
            self._apply_collapsed_geometry()
        self.store.move(self.box, self.box.x, self.box.y)
        self.changed.emit()
        QTimer.singleShot(0, self._push_desktop_icons)

    def _finish_group_drag(self) -> None:
        """一组窗口一起搬完：整组夹进屏幕，相对位置一个像素都不变。

        单个盒子拖完会跑 `_avoid_overlap`；整组不跑——组内本来就挨着，
        按单个盒子避让会把它们拆散。夹边界的算法在 groupmove 里（和卡片共用）。
        """
        n = groupmove.finish(self, self._drag_group)
        _log("group move: %d windows -> %s,%s", n, self.box.x, self.box.y)

    def _push_desktop_icons(self) -> None:
        """把被盒子压住的桌面图标挤到空位。"""
        try:
            r = self.collapsed_rect()
            # 盒子是逻辑像素，桌面图标要物理像素，不换算会挤错位置
            moved = di.make_room(screen.to_physical(
                (r.x(), r.y(), r.x() + r.width(), r.y() + r.height())))
            _log("push icons out, moved=%s", moved)
        except Exception:  # noqa: BLE001 - 桌面接口抽风不能影响盒子
            pass

    def mouseDoubleClickEvent(self, event) -> None:
        # 刚出现的盒子忽略双击，避免合并手势松手时误启动里面的图标
        if time.monotonic() - self._born < 0.8:
            return
        # 第二下「按下」被 Qt 标成双击送过来。拖动区上的第二下必须照常开始
        # 一次按下记录，否则这次抬起落在一个没有按下状态上，收起/展开和拖动都会乱。
        if event.button() == Qt.LeftButton:
            pos = self._to_content(event.position().toPoint())
            if self._grip_rect().contains(pos.x(), pos.y()):
                self._press_in_glass = self._in_glass(event.position().toPoint())
                self._begin_grip_press(event)
                event.accept()
                return
        if not self._expanded:
            return
        idx = self._item_at(self._to_content(event.position().toPoint()))
        if idx is None:
            return
        event.accept()
        # 不立刻启动：先等 180ms。用户拖图标时第二下按下会被 Qt 判成双击，
        # 立刻启动就会在拖拽途中把软件打开；延迟之后只要中途动了就取消。
        self._pending_launch = idx
        if self._launch_timer is None:
            self._launch_timer = QTimer(self)
            self._launch_timer.setSingleShot(True)
            self._launch_timer.timeout.connect(self._maybe_launch)
        self._launch_timer.start(180)

    def _maybe_launch(self) -> None:
        idx = self._pending_launch
        self._pending_launch = None
        if idx is None or not (0 <= idx < len(self._items)):
            return
        # 中途动过（拖动/拖出/换位）或者鼠标还按着 → 这是拖拽，不是双击
        if self._moved or self._internal_drag is not None or self._item_drag:
            return
        if self._press_global is not None:
            return
        self._launch(idx)

    def _start_item_drag(self, indexes) -> None:
        """把选中的一个或多个图标拖出去。indexes 可以是 int 或 int 列表。"""
        if isinstance(indexes, int):
            indexes = [indexes]
        indexes = [i for i in indexes if 0 <= i < len(self._items)]
        if self._item_drag or not indexes:
            return
        self._item_drag = True
        paths = [self._items[i] for i in indexes]
        _log("drag out start %s", [str(p) for p in paths])
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
        drag = QDrag(self)
        drag.setMimeData(mime)
        pm = self._icon(paths[0], 52)
        drag.setPixmap(pm)
        drag.setHotSpot(QPoint(pm.width() // 2, pm.height() // 2))
        _TAKEN.clear()
        self._drag_over = None
        self._preview_key = None
        self._drag_paths = [str(p) for p in paths]
        # 拖的过程中盯着光标：压到桌面快捷方式上就开始摆盒子预览，
        # 松手就地新建一个盒子（explorer 那边照旧先接住文件，收尾时我们搬走）
        poll = QTimer(self)
        poll.setInterval(60)
        poll.timeout.connect(self._poll_drag_over)
        poll.start()
        try:
            result = drag.exec(Qt.MoveAction)
        finally:
            poll.stop()
            poll.deleteLater()
            self._drag_paths = []
        drop = QCursor.pos()   # 松手那一刻光标在哪儿（拖到桌面时图标就落这儿）
        _log("drag out done result=%s n=%s drop=%s over=%s", result, len(paths),
             drop, self._drag_over and self._drag_over[1])
        self._item_drag = False
        # 拖完之后把「按下」状态清干净：不清的话盒子会一直黏着鼠标走
        self._press_global = None
        self._win_pos = None
        self._moved = False
        self._pressed_item = None
        self.set_acrylic(True)
        self._settle_after_drag(paths, drop)
        self._collapse_after_drag_out(paths)

    def _box_from_selection(self, pos: QPoint) -> None:
        """滚轮键：把这个盒子里选中的图标收成一个新盒子。

        有框选/多选就用选中的那些；只按在一张图标上就是它一个；按在空白处
        又没有选择就什么都不做。
        """
        sel = sorted(self._selected)
        if not sel:
            hit = self._item_at(pos)
            if hit is None:
                return
            sel = [hit]
        paths = [self._items[i] for i in sel if 0 <= i < len(self._items)]
        cb = getattr(self, "on_new_box_from_selection", None)
        if not paths or cb is None:
            return
        _log("wheel box from %s: %s", self.box.id, [p.name for p in paths])
        cb(paths, self.frameGeometry())
        self._selected = set()
        self.update()

    def _poll_drag_over(self) -> None:
        """拖拽途中看光标底下压着谁：桌面的快捷方式 → 预告「松手在这儿建盒」。"""
        if getattr(self, "on_new_box", None) is None:
            return
        prev = getattr(self, "on_drag_preview", None)
        cur = QCursor.pos()
        key = None
        rect = None
        path = None
        if QApplication.widgetAt(cur) is None:
            # 压在我们自己的窗口上（别的盒子 / 卡片）就是「搬进那个盒子」，
            # 不走建盒这条路
            px, py = screen.to_physical((cur.x(), cur.y(), cur.x(), cur.y()))[0:2]
            idx, _name, _path = di.item_at_screen(int(px), int(py))
            if _path is not None:
                rect = di.item_rect(idx)
                if rect is not None:
                    path = str(_path)
                    key = (idx, path)
        if key == getattr(self, "_preview_key", None):
            return
        self._preview_key = key
        self._drag_over = None if key is None else (key[0], "", path, rect)
        if prev is not None:
            prev(rect, (list(self._drag_paths) + [path]) if key else [])

    def _collapse_after_drag_out(self, paths) -> None:
        """图标已经拖出去了：盒子跟着收起来（手机里把 app 拖出文件夹也是这样）。

        拖到一半按 Esc 取消的话文件还在盒子里，这时不动它；钉住的盒子也不动。
        """
        if not self.isVisible() or not self._expanded:
            return
        if getattr(self.box, "pinned", False):
            return
        if any(p in self._items for p in paths):
            return
        self.toggle(False)

    def _settle_after_drag(self, paths: list[Path], drop: QPoint) -> None:
        """松手之后的收尾，越快越好。

        explorer 把文件放到桌面上之后，桌面图标也是攒着刷新的（半秒起步）。
        这里主动通知 shell 并立刻摆到松手的位置，不给用户「等一下才出来」的机会。
        """
        self._busy_drop = True
        try:
            self._settle_after_drag_inner(paths, drop)
        finally:
            self._busy_drop = False

    def _settle_after_drag_inner(self, paths: list[Path], drop: QPoint) -> None:
        # 系统图标被拖出去了：原来的做法会在桌面留下一个「此电脑.lnk」副本，
        # 而真正该做的是把系统图标放出来。所以这边删掉桌面副本 + 取消隐藏。
        ns_dragged = [p for p in paths if self.store.is_ns(self.box, p)]
        if ns_dragged:
            for path in ns_dragged:
                clsid = self.box.hidden.pop(path.name, None)
                if clsid:
                    ns_items.set_hidden(clsid, False)
                try:
                    path.unlink()
                except OSError:
                    pass
                landed = di.find_desktop_copy(path.name)
                if landed is not None:
                    try:
                        landed.unlink()
                    except OSError:
                        pass
            self.store.save()
            paths = [p for p in paths if p not in ns_dragged]
        # 盒子里少了图标：当场刷新（可能因此解散）
        self.refresh()
        if not paths:
            return
        # 搬到别的盒子里了：文件已经不在这儿，别再按「落在桌面」处理一遍
        left = [p for p in paths if os.path.normcase(str(p)) not in _TAKEN]
        over = getattr(self, "_drag_over", None)
        self._drag_over = None
        if not left:
            return
        if over is not None:
            cb = getattr(self, "on_new_box", None)
            if cb is not None:
                cb(over[3], over[2], left)
                return
        _land_restored(left, drop)

    # ---------------------------------------------------------------- 拖放
    def _shell_clsids(self, mime) -> list[str]:
        """拖拽里带的系统图标（此电脑 / 回收站 / 控制面板…）→ CLSID 列表。

        explorer 拖命名空间项时给的是 `CFSTR_SHELLIDLIST`（不是文件，没有
        CF_HDROP），Qt 把它转成 `...;value="Shell IDList Array"` 的字节。这里
        把 CIDA 里的每个子 pidl 拆出来，再从字节里认 CLSID（见
        `ns_items.clsid_from_pidl`），只关心那四个系统图标，不解析 COM。
        """
        for fmt in mime.formats():
            if "Shell IDList Array" not in fmt:
                continue
            raw = bytes(mime.data(fmt))
            found: list[str] = []
            for blob in _cidl_items(raw):
                clsid = ns_items.clsid_from_pidl(blob)
                if clsid and clsid not in found:
                    found.append(clsid)
            return found
        return []

    def _accept_action(self, event) -> None:
        """接受拖放，并且**沿用对方提议的动作**。

        以前是硬写 Copy：从桌面上拖进来时 explorer 提议 Copy，正好对上；
        但收纳盒之间互相拖，源盒子只提供 Move，硬改成 Copy 会让 Qt 判成
        「动作对不上」，落点就不确定了。这里对方给什么就收什么。
        """
        action = event.proposedAction()
        if action not in (Qt.MoveAction, Qt.CopyAction, Qt.LinkAction):
            action = Qt.CopyAction
        event.setDropAction(action)
        event.accept()

    def dragEnterEvent(self, event) -> None:
        mime = event.mimeData()
        has_files = mime.hasUrls() and any(u.isLocalFile() for u in mime.urls())
        nss = [] if has_files else self._shell_clsids(mime)
        _log("dragEnter files=%s ns=%s", has_files, nss)
        if not has_files and not nss:
            return
        self._accept_action(event)
        self.set_hot(True)
        self._collapse_timer.stop()
        # 图标拖到盒子上来 → 自动展开，免得盲投
        if not self._expanded:
            self._auto_expanded = True
            self.toggle(True)
        self.update()

    def dragLeaveEvent(self, event) -> None:
        self.set_hot(False)
        self.update()
        # 拖进来的东西又移开了：过一会儿把「因拖拽才展开」的盒子收回去
        if self._auto_expanded:
            self._collapse_timer.start()

    def dropEvent(self, event) -> None:
        self.set_hot(False)
        self._collapse_timer.stop()
        self._auto_expanded = False
        mime = event.mimeData()
        paths = [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]
        nss = self._shell_clsids(mime)
        _log("drop paths=%s ns=%s", paths, nss)
        self._accept_action(event)
        # 先记下来「这些文件被我接走了」：_add_dropped 是下一帧才跑的，
        # 而源盒子在 exec 返回后马上就判断了
        for p in paths:
            _TAKEN.add(os.path.normcase(str(p)))
        if not self._expanded:
            self.toggle(True)
        if paths or nss:
            # 先把 drop 还给 explorer：它正卡在自己的 DoDragDrop 里等我们返回。
            # 搬文件之后要发同步的 SHChangeNotify(FLUSH)，不能在这个回调里做，
            # 否则可能会去等一个正等着我们的线程。下一帧做完，肉眼看不出区别。
            QTimer.singleShot(0, lambda: self._add_dropped(paths, nss))

    def _add_dropped(self, paths: list[str], nss: list[str]) -> None:
        """拖进盒子的东西：普通文件走搬移，系统图标走 CLSID 快捷方式。"""
        self._busy_drop = True
        try:
            added = 0
            if paths:
                added += self.add_files(paths)
            for clsid in nss:
                if self.store.add_ns(self.box, ns_items.name_for(clsid), clsid):
                    added += 1
            if added:
                self.refresh()
                self.changed.emit()
        finally:
            self._busy_drop = False

    # ---------------------------------------------------------------- 就地改名
    def _begin_inline_rename(self) -> None:
        """在标题位置直接编辑，不弹对话框。"""
        self._outside_timer.stop()
        # 上一次按下可能没收到 release（焦点刚被输入框抢走），先清干净
        self._press_global = None
        self._win_pos = None
        self._moved = False
        self._pressed_item = None
        self.activateWindow()
        if self._edit is None:
            self._edit = QLineEdit(self)
            self._edit.setFrame(False)
            # 不要任何底色/边框：看起来就是标题文字本身变成可编辑，
            # 颜色跟标题一致（深灰蓝），不做蓝色选中框。
            # 字号不写死在 QSS 里——每次改名用 _title_font() 同步过去，
            # 收起/展开两种状态下的字才和原来一样大，不会突然变粗变大。
            self._edit.setStyleSheet(
                "QLineEdit { background: transparent; border: none; padding: 0px;"
                " color: rgb(58,70,92);"
                " selection-background-color: rgba(70,132,240,70);"
                " selection-color: rgb(58,70,92); }")
            self._edit.returnPressed.connect(lambda: self._finish_inline_rename(True))
            self._edit.installEventFilter(self)
        self._editing = True
        self._edit.setFont(self._title_font())
        self._edit.setText(self.box.name)
        m = self._win_margin()
        self._edit.setGeometry(
            self._name_rect().toRect().adjusted(0, 1, -3, -1).translated(m, m))
        self._edit.show()
        self._edit.raise_()
        self._edit.setFocus(Qt.OtherFocusReason)
        # 不要全选（全选会整段变蓝），光标放到末尾就行
        self._edit.deselect()
        self._edit.setCursorPosition(len(self._edit.text()))
        self.update()

    def _finish_inline_rename(self, commit: bool) -> None:
        if not self._editing or self._edit is None:
            return
        text = self._edit.text().strip()
        self._editing = False
        self._edit.hide()
        if commit and text:
            self.store.rename(self.box, text)
        if self._expanded:
            self._was_down = bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
            self._outside_timer.start()
        self.update()

    def eventFilter(self, obj, ev) -> bool:
        if obj is self._edit:
            if ev.type() == QEvent.Type.KeyPress and ev.key() == Qt.Key_Escape:
                self._finish_inline_rename(False)
                return True
            if ev.type() == QEvent.Type.FocusOut:
                # 点到别处（包括盒子外面）就当场确认，不用非得按回车
                self._finish_inline_rename(True)
        return super().eventFilter(obj, ev)

    def focusOutEvent(self, event) -> None:
        if self._editing:
            self._finish_inline_rename(True)
        super().focusOutEvent(event)

    def dispose(self) -> None:
        self._refresh_timer.stop()
        self._outside_timer.stop()
        if getattr(self, "_icon_ready_conn", False):
            self._icon_ready_conn = False
            try:
                icon_loader().done.disconnect(self._on_icon_ready)
            except (RuntimeError, TypeError):
                pass
        try:
            for folder in self._watch.directories():
                self._watch.removePath(folder)
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- 点外面收起
    def _watch_outside_click(self) -> None:
        # 正在拖盒里的图标、或者正在拖盒子本身时不算「点外面」
        if self._item_drag or self._press_global is not None:
            return
        down = bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
        self._collapse_if_clicked_outside(down, QCursor.pos())

    def _collapse_if_clicked_outside(self, down: bool, pos: QPoint) -> None:
        """展开状态下，在盒子外面按下左键就收起。"""
        was = self._was_down
        self._was_down = down
        if not (down and not was and self._expanded):
            return
        if self.frameGeometry().contains(pos) or getattr(self.box, "pinned", False):
            return
        # 弹出的菜单（图标右键那张、盒子菜单）常常伸到盒子外面：点菜单不算「点外面」
        try:
            from PySide6.QtWidgets import QApplication
            popup = QApplication.activePopupWidget()
        except (RuntimeError, TypeError):
            popup = None
        if popup is not None and popup.isVisible():
            return
        if self._menu_open():
            return
        _log("clicked outside at %s, collapse", pos)
        self.toggle(False)

    def _menu_open(self) -> bool:
        """现在是不是正弹着菜单（右键菜单弹在盒子外面，点它不该把盒子收起来）。"""
        return bool(getattr(self, "_menu_busy", False))

    # ---------------------------------------------------------------- 窗口层级
    @property
    def is_expanded(self) -> bool:
        return bool(self._expanded)

    def _notify_z(self) -> None:
        """层级交给 Controller 统一定（见 Controller.apply_z_order）。

        以前每个盒子自己把自己插到 Progman 上面，谁后插谁就在最上面，于是
        几个盒子互相「横跳」——展开的盒子反而会被收起的盒子盖住。
        """
        cb = getattr(self, "on_z_changed", None)
        if cb is not None:
            cb()
            return
        self._desktop_level()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # 滑动入场时别钳：那时窗口正停在屏幕外面，`_clamp_to_screen()` 会按
        # `box.x/y`（家位置）把它搬回来，于是「先闪一下家门口再滑进来」——就是
        # 用户看到的抽搐。滑完它本来就在家位置，不需要钳。
        if not self._expanded and not getattr(self, "_slide_busy", False):
            self._clamp_to_screen()
        # 层级统一交给 Controller（它有「已经对了就别动」的判断，不会闪）；
        # 这里不再各挂一个定时器，否则 N 个盒子每秒重排 N 次，窗口一直闪
        self._notify_z()

    def event(self, ev):
        if ev.type() == QEvent.Type.WindowActivate:
            QTimer.singleShot(0, self._notify_z)
        return super().event(ev)

    def _desktop_level(self) -> None:
        """兜底（没有 Controller 时）：插到 Progman 正上方。"""
        try:
            progman = user32.FindWindowW("Progman", None)
            if not progman or not self.isVisible():
                return
            user32.SetWindowPos(int(self.winId()), progman, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER)
        except OSError:
            pass


def QDesktopServices_open(path) -> None:
    from PySide6.QtGui import QDesktopServices
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
