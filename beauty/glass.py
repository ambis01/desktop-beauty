"""无边框毛玻璃窗口：手绘玻璃卡片 + 系统级亚克力模糊。"""
from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import (
    QPoint, QPointF, QRect, QRectF, Qt, QTimer,
)
from PySide6.QtGui import (
    QBrush, QColor, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient,
)
from PySide6.QtWidgets import QMenu, QWidget

# 全局 QSS：菜单、按钮、列表统一成深色玻璃风格
GLOBAL_QSS = """
QMenu {
    background: transparent;
    border: none;
    border-radius: 10px;
    padding: 8px;
    color: #EEF1F7;
    font-size: 12px;
}
QMenu::item { padding: 7px 30px 7px 16px; border-radius: 7px; }
QMenu::item:selected { background: rgba(255, 255, 255, 30); }
QMenu::separator { height: 1px; background: rgba(255, 255, 255, 28); margin: 5px 10px; }

QPushButton {
    background: rgba(255, 255, 255, 26);
    border: 1px solid rgba(255, 255, 255, 34);
    border-radius: 9px;
    padding: 8px 10px;
    color: rgba(255, 255, 255, 236);
    font-size: 12px;
}
QPushButton:hover { background: rgba(255, 255, 255, 46); }
QPushButton:pressed { background: rgba(255, 255, 255, 62); }
QPushButton:checked {
    background: rgba(122, 170, 255, 70);
    border-color: rgba(150, 192, 255, 110);
}
QPushButton#primary {
    background: rgba(126, 172, 255, 58);
    border-color: rgba(160, 200, 255, 110);
}
QPushButton#primary:hover { background: rgba(140, 184, 255, 82); }
QPushButton#close {
    background: transparent; border: none; font-size: 14px;
    color: rgba(255, 255, 255, 170); padding: 0px;
}
QPushButton#close:hover { background: rgba(255, 90, 90, 150); color: white; }

QListWidget {
    background: rgba(0, 0, 0, 70);
    border: 1px solid rgba(255, 255, 255, 24);
    border-radius: 10px;
    color: rgba(255, 255, 255, 214);
    font-size: 12px;
    padding: 4px;
    outline: none;
}
QListWidget::item { padding: 4px 6px; border-radius: 5px; }
QListWidget::item:selected { background: rgba(126, 172, 255, 70); color: white; }
QScrollBar:vertical { background: transparent; width: 6px; margin: 4px; }
QScrollBar::handle:vertical { background: rgba(255,255,255,70); border-radius: 3px; min-height: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QToolTip {
    background: rgba(26, 29, 37, 246);
    color: #EEF1F7;
    border: 1px solid rgba(255,255,255,40);
    padding: 4px 8px;
}
"""


# 悬停「弹一下」的公共参数：收纳盒和时钟卡片用同一套，
# 改一个数两边一起变（用户要求「同款」并且要更快）
HOVER_GROW = 0.05      # 玻璃整体放大 5%（窗口不动，纯重绘）
HOVER_MS = 150         # 放大到位用多久；缩回再多 40ms


def paint_frost(widget: QWidget, p: QPainter, dark: bool) -> None:
    """在整块窗口矩形上自绘 DSH 雾玻璃「蓝紫」那套。

    系统亚克力对 Qt 透明窗口只剩纯色，所以面板和面板弹出的窗口都用这个函数
    画背景：雾底 + 三团蓝紫光斑 + 顶部柔光 + 收边。两边共用同一份代码，
    以后调色只改这里，「面板和弹窗长得不一样」的概率就是 0。
    """
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setCompositionMode(QPainter.CompositionMode_Source)
    p.fillRect(widget.rect(), Qt.transparent)
    p.setCompositionMode(QPainter.CompositionMode_SourceOver)
    w = float(widget.width())
    h = float(widget.height())
    if w <= 0.0 or h <= 0.0:
        return
    rad = float(getattr(widget, "radius", 10) or 10)
    plate = QRectF(0.0, 0.0, w, h)
    path = QPainterPath()
    path.addRoundedRect(plate, rad, rad)
    p.save()
    p.setClipPath(path)
    if dark:
        p.fillRect(plate, QColor(18, 19, 24, 250))
        spots = ((0.16, 0.12, 0.62, QColor(72, 110, 214, 100)),
                 (0.86, 0.16, 0.56, QColor(126, 92, 210, 82)),
                 (0.50, 0.50, 0.70, QColor(92, 108, 150, 48)))
        top_alpha, edge = 16, QColor(160, 180, 220, 54)
    else:
        p.fillRect(plate, QColor(247, 248, 252, 250))
        spots = ((0.16, 0.12, 0.62, QColor(96, 140, 255, 86)),
                 (0.86, 0.16, 0.56, QColor(160, 120, 255, 62)),
                 (0.50, 0.50, 0.70, QColor(110, 130, 190, 34)))
        top_alpha, edge = 64, QColor(120, 140, 180, 74)
    reach = max(w, h)
    for cx, cy, rr, color in spots:
        g = QRadialGradient(QPointF(w * cx, h * cy), reach * rr)
        g.setColorAt(0.0, color)
        g.setColorAt(1.0, QColor(color.red(), color.green(), color.blue(), 0))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawRect(plate)
    # 顶部一层很淡的高光，玻璃才有「受光」的感觉
    lg = QLinearGradient(0.0, 0.0, 0.0, h)
    lg.setColorAt(0.0, QColor(255, 255, 255, top_alpha))
    lg.setColorAt(0.35, QColor(255, 255, 255, max(4, top_alpha // 5)))
    lg.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.setBrush(lg)
    p.fillRect(plate, lg)
    p.restore()
    # 一圈很淡的描边（收边用，不能太实）
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(edge, 1.0))
    p.drawPath(path)


class _AccentPolicy(ctypes.Structure):
    _fields_ = [
        ("AccentState", ctypes.c_uint),
        ("AccentFlags", ctypes.c_uint),
        ("GradientColor", ctypes.c_uint),
        ("AnimationId", ctypes.c_uint),
    ]


class _CompositionAttrData(ctypes.Structure):
    _fields_ = [
        ("Attribute", ctypes.c_int),
        ("Data", ctypes.c_void_p),
        ("SizeOfData", ctypes.c_size_t),
    ]


def apply_acrylic(hwnd: int, tint_alpha: int = 0x44, enabled: bool = True,
                  rgb: tuple[int, int, int] = (0x13, 0x1B, 0x24),
                  state: int = 4) -> bool:
    """Win10/11 的背景模糊。enabled=False 时关掉（拖动时用，省性能）。

    state=4 是 ACCENT_ENABLE_ACRYLICBLURBEHIND：模糊 + 叠一层色调（会被
    GradientColor 染色，白色玻璃看起来会「洗白、变透」）。
    state=3 是 ACCENT_ENABLE_BLURBEHIND：**只模糊、不染色**，适合「把底下
    压住的东西糊掉，但别改变盒子本身颜色」的场景。

    rgb 是模糊层叠加的色调：深色玻璃用深蓝黑，白玻璃要给浅冷灰，
    否则白面板背后会压出一层脏灰。
    """
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.WinDLL("user32")
        set_attr = user32.SetWindowCompositionAttribute
    except (AttributeError, OSError):
        return False

    policy = _AccentPolicy()
    if enabled:
        policy.AccentState = int(state)
        policy.AccentFlags = 2
        # ABGR：alpha 太大壁纸就透不过来了
        tint_alpha = max(0, min(0xFF, int(tint_alpha)))
        r, g, b = (max(0, min(0xFF, int(v))) for v in rgb)
        policy.GradientColor = (tint_alpha << 24) | (b << 16) | (g << 8) | r
    else:
        policy.AccentState = 0  # ACCENT_DISABLED

    data = _CompositionAttrData(
        19,  # WCA_ACCENT_POLICY
        ctypes.cast(ctypes.byref(policy), ctypes.c_void_p),
        ctypes.sizeof(policy),
    )
    try:
        return bool(set_attr(hwnd, ctypes.byref(data)))
    except OSError:
        return False


def round_corners(hwnd: int, style: int = 2) -> bool:
    """Win11 的圆角窗口（DWM 在合成层裁，亚克力模糊也会跟着被裁进圆角）。"""
    if sys.platform != "win32":
        return False
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
        pref = ctypes.c_int(style)
        return dwmapi.DwmSetWindowAttribute(
            hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref)) == 0
    except (AttributeError, OSError):
        return False


DWMWA_BORDER_COLOR = 34
DWMWA_COLOR_NONE = 0xFFFFFFFE


def no_border(hwnd: int) -> bool:
    """去掉 Win11 画在窗口最外圈的那 1px 边框。

    26100 上 DWM 默认给窗口画一条很淡的深色边，白玻璃旁边就是一条
    「黑框」——实测像素是壁纸被压暗 10% 左右（135,127,157 对壁纸 148,134,183）。
    DWMWA_COLOR_NONE 让它不画。
    """
    if sys.platform != "win32":
        return False
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
        color = ctypes.c_uint(DWMWA_COLOR_NONE)
        return dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_BORDER_COLOR, ctypes.byref(color),
            ctypes.sizeof(color)) == 0
    except (AttributeError, OSError):
        return False


def apply_backdrop(hwnd: int, kind: int = 3) -> bool:
    """Win11 的系统背景材质：2 = Mica，3 = Acrylic，4 = Tabbed。

    这是真正「和系统自带的一样」的那条路（DWM 自己画模糊 + 噪声）。
    """
    if sys.platform != "win32":
        return False
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
        v = ctypes.c_int(kind)
        return dwmapi.DwmSetWindowAttribute(
            hwnd, 38, ctypes.byref(v), ctypes.sizeof(v)) == 0
    except (AttributeError, OSError):
        return False


def no_transitions(hwnd: int) -> bool:
    """别让 DWM 给这个窗口做过渡动画（DWMWA_TRANSITIONS_FORCEDISABLED = 3）。

    为什么：半透明工具窗口改尺寸/挪位置时，DWM 会「过渡」一下（把旧画面拉过去），
    肉眼就是盒子展开/收起的开头闪一下、或者边在抽。用户 2026-11-16 报的这两种
    毛病都只在 DWM 那一层，程序自己画出来的每一帧都是对的（逐帧抓图验过）。
    """
    if sys.platform != "win32":
        return False
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
        v = ctypes.c_int(1)
        return dwmapi.DwmSetWindowAttribute(
            hwnd, 3, ctypes.byref(v), ctypes.sizeof(v)) == 0
    except (AttributeError, OSError):
        return False


def modal(delta: int) -> None:
    """告诉 app 的事件计时器：这段里开着模态菜单（menu.exec 的嵌套循环）。

    不计这一笔的话，点一下盒子右上角三个点、菜单开一秒，日志里就会多一条
    「slow event: MetaCall dt=1100」——那其实是等用户选菜单，不是卡顿。
    """
    try:
        from .app import _PROF
        _PROF["modal"] = max(0, int(_PROF.get("modal", 0)) + delta)
        if delta < 0:
            _PROF["modal_seq"] = int(_PROF.get("modal_seq", 0)) + 1
    except Exception:  # noqa: BLE001 - 计时器不在（探针里）就算了
        pass


class GlassMenu(QMenu):
    """Win11 风格的亚克力菜单：抓屏模糊当背景 + 半透明深色 + 圆角。

    系统不给 Qt 透明窗口做模糊（实测两条系统接口都只出纯色），所以这里
    自己抓一张背后的屏幕图，缩到 1/8 再放大当模糊。
    """

    RADIUS = 8.0

    def __init__(self, parent=None, light: bool = False):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        # 浅色版给浅色面板上的下拉用（深色菜单压在浅玻璃上太突兀）
        self.light = bool(light)
        # 菜单是 popup 窗口，Windows 会给它投一层系统阴影（CS_DROPSHADOW），
        # 在浅色桌面上看起来就是菜单周围一圈黑边 —— 关掉。
        self.setWindowFlags(self.windowFlags() | Qt.NoDropShadowWindowHint)
        self._desk = None      # 背后桌面的原始像素（不模糊，用来铺满整个窗口）
        self._backdrop = None  # 同一块桌面的模糊版，铺在圆角里面
        self._full = None      # 弹出前抓的整屏图（那时菜单还不在屏幕上）
        self._ready = False
        self._home_geo = QRect()
        self._grow_timer = None
        self._grow_home = QRect()
        self._grow_t0 = 0.0

    def prepare_backdrop(self) -> None:
        """在 exec()/popup() 之前调一次：那会儿窗口还没上屏，抓到的
        才是背后的桌面（否则抓到的是菜单自己的灰底）。"""
        self._pre_capture()

    # 抓屏必须在菜单上屏之前：窗口一显示，抓到的就是菜单自己（灰色的 Qt
    # 菜单底），整块玻璃就变成一块灰板 —— 所以让调用方在 exec() 前调
    # prepare_backdrop()，先把整屏截下来，showEvent 里再按真实几何裁剪。
    def _pre_capture(self) -> None:
        try:
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            scr = self.screen() or (app.primaryScreen() if app else None)
            if scr is None:
                return
            pm = scr.grabWindow(0)
            if pm is not None and not pm.isNull():
                self._full = pm
        except (RuntimeError, TypeError):
            pass

    def prepare_backdrop(self) -> None:
        """在 exec()/popup() 之前调一次：那会儿窗口还没上屏，抓到的
        才是背后的桌面（否则抓到的是菜单自己的灰底）。"""
        self._pre_capture()

    def showEvent(self, event) -> None:
        self._ready = False
        self._desk = None
        self._backdrop = None
        self._home_geo = QRect(self.geometry())
        self._grab_backdrop(self._home_geo)
        super().showEvent(event)
        # 不要再调 DwmSetWindowAttribute（圆角/边框色）：实测只要设过，
        # DWM 就会在窗口最外圈补一条 1px 的近黑边框，浅色桌面上就是
        # 「菜单外面还有四个边角」。让它保持默认什么都不设最干净。
        self._ready = True
        # 「从上往下展开」：高度从 1px 长回最终高度。用自绘的 timer 推进，
        # QPropertyAnimation 在 QMenu.exec 的嵌套事件循环里跑不动。
        #
        # 滑动入场（Shift+Space 启用）必须跳过这一步：那条动画在改 height，滑动在改
        # x/y，两个定时器同一帧各 setGeometry 一次，盒子会抖一下（用户报的「启用时
        # 抽搐一下」）。滑进来本来就有位移当入场效果，不需要再长高。
        home = self._home_geo
        if not getattr(self, "_skip_show_grow", False) and not home.isEmpty():
            try:
                self.setGeometry(home.x(), home.y(), home.width(), 1)
                self._start_grow()
            except (RuntimeError, TypeError):
                pass
        # QMenu 经常在 showEvent 之后才把最终尺寸算准（二级菜单最明显）：
        # 展开动画播完再对一次账，不然玻璃只盖住上半截、下半截是透明的
        QTimer.singleShot(self.GROW_MS + 40, self._settle_home)
        self.update()

    def _settle_home(self) -> None:
        """菜单定下来之后按真实几何重裁一次背景（`_full` 是弹出前抓的整屏图，
        所以这里重新裁不会被菜单自己挡住）。"""
        try:
            if not self.isVisible():
                return
            home = QRect(self.geometry())
            if home.isEmpty() or home == self._home_geo:
                return
            self._home_geo = home
            if self._full is not None and not self._full.isNull():
                self._grab_backdrop(home)
            self.update()
        except (RuntimeError, TypeError):
            pass

    GROW_MS = 160

    def _start_grow(self) -> None:
        import time as _time
        home = self._home_geo
        if home.isEmpty():
            return
        self._grow_home = QRect(home)
        self._grow_t0 = _time.perf_counter()
        if self._grow_timer is None:
            self._grow_timer = QTimer(self)
            self._grow_timer.setInterval(16)
            self._grow_timer.timeout.connect(self._grow_step)
        self._grow_timer.start()

    def _grow_step(self) -> None:
        import time as _time
        home = self._grow_home
        if home.isEmpty() or not self.isVisible():
            if self._grow_timer is not None:
                self._grow_timer.stop()
            return
        t = min(1.0, (_time.perf_counter() - self._grow_t0) * 1000.0
                / float(self.GROW_MS))
        inv = 1.0 - t
        ease = 1.0 - inv * inv * inv          # OutCubic
        h = max(1, int(round(home.height() * ease)))
        self.setGeometry(home.x(), home.y(), home.width(), h)
        if t >= 1.0:
            self._grow_timer.stop()
            self.setGeometry(home)

    def _grab_backdrop(self, home: QRect) -> None:
        """抓背后桌面：原图铺满整窗，模糊版铺圆角内。"""
        try:
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            scr = self.screen() or (app.primaryScreen() if app else None)
            if scr is None or home.isEmpty():
                raise RuntimeError
            pm = self._full
            if pm is None or pm.isNull():
                # 没有预抓图就只能现抓（这时可能抓到菜单自己，尽量别走到）
                pm = scr.grabWindow(0)
            if pm.isNull():
                raise RuntimeError
            dpr = float(pm.devicePixelRatio() or 1.0)
            sg = scr.geometry()
            # +1：逻辑几何 × dpr 常常带 0.5，取整后比窗口实际少 1 像素，
            # 那一行没被覆盖就会露出窗口的黑底（菜单底部一条黑线）。
            x = int(round((home.x() - sg.x()) * dpr))
            y = int(round((home.y() - sg.y()) * dpr))
            w = max(1, int(round(home.width() * dpr)) + 1)
            h = max(1, int(round(home.height() * dpr)) + 1)
            crop = pm.copy(x, y, w, h)
            # dpr 一律记 1：绘制时按物理像素 1:1 贴，才不会因为 125% 缩放插值
            # 出现「窗口矩形」那圈比桌面深/浅一档的边。
            self._desk = crop
            self._desk.setDevicePixelRatio(1.0)
            # div 越大缩得越小、糊得越狠。以前是 4，菜单看着像一块磨砂塑料板
            # （用户：太糊了）。2 只糊一点点，背后的桌面还看得清轮廓。
            div = 2
            small = crop.scaled(max(1, w // div), max(1, h // div),
                                Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            self._backdrop = small.scaled(w, h, Qt.IgnoreAspectRatio,
                                          Qt.SmoothTransformation)
            self._backdrop.setDevicePixelRatio(1.0)
        except (RuntimeError, TypeError):
            self._desk = self._backdrop = None

    def _palette(self) -> dict:
        """菜单配色：深色版（默认）和浅色版（浅色面板上的下拉用）。"""
        if self.light:
            return {
                "plate": QColor(246, 247, 251, 236),
                "top": QColor(255, 255, 255, 92),
                "bot": QColor(120, 140, 180, 16),
                "hover": QColor(70, 100, 160, 34),
                "sep": QColor(60, 80, 110, 36),
                "check": QColor(64, 116, 226),
                "text": QColor(38, 48, 66),
                "shadow": QColor(255, 255, 255, 150),
            }
        # 默认那套以前是「深灰玻璃 + 白字」，压在花壁纸上又黑又糊（用户明确嫌黑）。
        # 现在改成白雾玻璃 + 深色字：跟浅色面板的下拉同一路，只是更透一点。
        return {
            "plate": QColor(234, 237, 243, 214),
            "top": QColor(255, 255, 255, 96),
            "bot": QColor(120, 140, 180, 14),
            "hover": QColor(70, 100, 160, 38),
            "sep": QColor(60, 80, 110, 40),
            "check": QColor(64, 116, 226),
            "text": QColor(34, 41, 54),
            "shadow": QColor(255, 255, 255, 150),
        }

    def _paint_items(self, p) -> None:
        """自己画菜单项：super().paintEvent 会铺一层不透明黑底，不能调；
        QStyle 那条路在 QSS 下画出来是深色字，压在深玻璃上看不见。"""
        from PySide6.QtCore import QRectF, Qt
        from PySide6.QtGui import QColor, QPen
        pal = self._palette()
        p.setFont(self.font())
        # 有勾选项才让出左边那一列，没勾选项的菜单（盒子右上角那个）文字照旧靠左
        has_check = any(a.isCheckable() and not a.isSeparator() for a in self.actions())
        has_icon = any(not a.icon().isNull() for a in self.actions()
                       if not a.isSeparator())
        text_x = 40.0 if has_check else (32.0 if has_icon else 16.0)
        for act in self.actions():
            geo = self.actionGeometry(act)
            if geo.isEmpty() or not act.isVisible():
                continue
            if act.isSeparator():
                y = geo.center().y()
                p.setPen(QPen(pal["sep"], 1.0))
                p.drawLine(geo.left() + 10, y, geo.right() - 10, y)
                continue
            if self.activeAction() is act:
                p.setPen(Qt.NoPen)
                p.setBrush(pal["hover"])
                p.drawRoundedRect(QRectF(geo), 6.0, 6.0)
                p.setBrush(Qt.NoBrush)
            if act.isChecked():
                p.setPen(QPen(pal["check"], 1.8))
                bx = geo.left() + 8
                by = geo.center().y()
                p.drawLine(bx, by, bx + 4, by + 4)
                p.drawLine(bx + 4, by + 4, bx + 10, by - 4)
            alpha = 255 if act.isEnabled() else 150
            ic = act.icon()
            if not ic.isNull():
                # 图标画在左边，垂直居中；和 Win11 菜单一样 16px
                ipm = ic.pixmap(16, 16)
                if not ipm.isNull():
                    p.setOpacity(alpha / 255.0)
                    p.drawPixmap(geo.left() + 10, geo.center().y() - 8, ipm)
                    p.setOpacity(1.0)
            txt = act.text().replace("&", "")
            hint = act.property("hint") or ""
            box = QRectF(geo.left() + text_x, geo.top(),
                         geo.width() - text_x - 8, geo.height())
            flags = int(Qt.AlignVCenter | Qt.AlignLeft)
            # 极淡的描边垫在字下面：浅/深玻璃上白字黑字都不会「浮」起来
            sh = QColor(pal["shadow"])
            sh.setAlpha(int(sh.alpha() * (alpha / 255.0)))
            p.setPen(sh)
            p.drawText(box.translated(0.0, 1.0), flags, txt)
            col = QColor(pal["text"])
            col.setAlpha(alpha)
            p.setPen(col)
            p.drawText(box, flags, txt)
            if hint:
                hcol = QColor(pal["text"])
                hcol.setAlpha(int(alpha * 0.62))
                p.setPen(hcol)
                p.drawText(QRectF(geo.left(), geo.top(),
                                  geo.width() - 14, geo.height()),
                           int(Qt.AlignVCenter | Qt.AlignRight), str(hint))

    def paintEvent(self, event) -> None:
        home = self._home_geo if not self._home_geo.isEmpty() else QRect(self.geometry())
        try:
            from PySide6.QtCore import QRectF, Qt
            from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing, True)
            if not self._ready:
                # 还在抓背景：什么都别画（窗口本身是 opacity 0，看不见）
                p.end()
                return
            w, h = home.width(), home.height()
            whole = QRect(0, 0, w, h)
            dpr = float(self.devicePixelRatioF() or 1.0)
            if dpr <= 0:
                dpr = 1.0
            # 1) 整窗铺桌面原图 —— 按物理像素 1:1 贴。
            #    圆角外面看起来就是桌面，所以窗口不会出现黑边和方形轮廓。
            if self._desk is not None and not self._desk.isNull():
                p.save()
                p.scale(1.0 / dpr, 1.0 / dpr)
                p.drawPixmap(0, 0, self._desk)
                p.restore()
            # 2) 圆角里面：模糊底 + 半透明深色 + 一层很淡的上亮下暗
            #    路径故意画到窗口外面 1px，让玻璃盖住最外圈那条像素 ——
            #    否则贴边会被抗锯齿吃掉半像素，看起来像菜单四周镶了一圈
            #    「没盖住的透明边」。
            path = QPainterPath()
            path.addRoundedRect(QRectF(0.0, 0.0, float(w), float(h)),
                                self.RADIUS, self.RADIUS)
            p.save()
            p.setClipPath(path)
            if self._backdrop is not None and not self._backdrop.isNull():
                p.save()
                p.scale(1.0 / dpr, 1.0 / dpr)
                p.drawPixmap(0, 0, self._backdrop)
                p.restore()
            pal = self._palette()
            p.fillRect(whole, pal["plate"])
            grad = QLinearGradient(0.0, 0.0, 0.0, float(h))
            grad.setColorAt(0.0, pal["top"])
            grad.setColorAt(1.0, pal["bot"])
            p.fillRect(whole, QBrush(grad))
            p.restore()
            # 3) 菜单项
            p.save()
            p.setClipPath(path)
            self._paint_items(p)
            p.restore()
            p.end()
        except (RuntimeError, TypeError):
            pass


class GlassWindow(QWidget):
    """圆角毛玻璃卡片，左键拖动。dark=False 是白色玻璃，True 是深色玻璃。"""

    def __init__(self, parent=None, flags=Qt.FramelessWindowHint | Qt.Tool,
                 radius: float = 22, activate: bool = False, dark: bool = False):
        super().__init__(parent, flags)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        if not activate:
            self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.radius = radius
        self.dark = dark
        # 浅色玻璃上如果要配白字，叠一层很淡的冷灰底，字才看得清（不是黑卡）
        self.tint = None
        # 玻璃本身的颜色（None = 默认白玻璃）。面板里可以按颜色调，
        # 见 paint_frost 里那个 `if rgb is not None` 分支
        self.glass_rgb = None
        self.glass_alpha = 150
        # 背景浓度（0~1）：只稀释玻璃本身，不动窗口里画的字
        # （setWindowOpacity 会把文字一起变淡，那是另一码事）
        self.bg_opacity = 1.0
        self.accent_alpha = 0x44
        self.accent_rgb = (0x13, 0x1B, 0x24)
        # 系统的亚克力模糊会铺满整个矩形窗口（忽略圆角）而且每移动一像素都要
        # 重算一次，所以默认不用它：圆角由 Qt 自己抗锯齿，拖动也不掉帧。
        self.real_blur = False
        self._blurred = False
        self._drag: QPointF | None = None
        # 绘制缩放：窗口比内容大一圈时用（悬停放大只改这个，不动窗口尺寸，
        # 这样是纯重绘，60fps 动画才不会一顿一顿的）
        self.paint_scale = 1.0
        # 窗口边缘到玻璃之间留的透明余量（逻辑像素）。玻璃自绘，不靠窗口
        # 裁形状，所以窗口可以比玻璃大一圈：放大时玻璃有地方长，平时那圈
        # 用 projection 填掉（见 paint_shadow）。
        self.inset = 0
        self.shadow_alpha = 0.0      # 贴着玻璃那条边的阴影不透明度（0~255）
        self.shadow_spread = 0.0     # 阴影往外扩散几像素
        # 「雾」系数：>1 把白色那层加回来（卡片上全是字，比盒子雾一点才看得清）
        self.frost = 1.0

    # ---- 绘制 ----
    def paint_backdrop(self, _p: QPainter) -> None:
        """钩子：在玻璃下面画点东西（收纳盒用它画「重叠区的模糊」）。"""
        return None

    def _inset(self) -> int:
        """窗口边缘到玻璃的透明余量（逻辑像素）。"""
        try:
            return max(0, int(getattr(self, "inset", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def paint_shadow(self, p: QPainter, plate: QRectF, radius: float) -> None:
        """玻璃外面那一圈很淡的投影。

        为什么要有它：窗口必须比玻璃大一圈，否则悬停放大时玻璃没地方长；
        可那圈平时是透明的，看着就是「盒子外面套了个透明框」。用一层很薄的
        阴影把它填上，既像盒子投在桌面上的淡影，又把放大要用的余量藏住了。
        """
        alpha = float(getattr(self, "shadow_alpha", 0) or 0)
        spread = float(getattr(self, "shadow_spread", 0) or 0)
        if alpha <= 0 or spread <= 0 or plate.isEmpty():
            return
        steps = max(3, int(round(spread * 2)))
        p.save()
        p.setBrush(Qt.NoBrush)
        for i in range(1, steps + 1):
            t = i / float(steps)             # 0→1：从玻璃边往外
            d = spread * t
            a = int(round(alpha * (1.0 - t) ** 2.0))
            if a <= 0:
                continue
            pen = QPen(QColor(0, 0, 0, a))
            pen.setWidthF(1.0)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.drawRoundedRect(plate.adjusted(-d, -d, d, d), radius + d, radius + d)
        p.restore()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        # 先把整块画布清成**全透明**再画。
        # 不清的话：悬停放大后缩回去，比盘子大出来的那一圈没人重画，上一帧
        # 的玻璃像素就留在窗口表面上，看着就是盒子外多了一圈「透明框」。
        # 子类如果要先画自己的背景（比如抓屏模糊），设 _keep_background=True。
        if not getattr(self, "_keep_background", False):
            p.setCompositionMode(QPainter.CompositionMode_Source)
            p.fillRect(self.rect(), Qt.transparent)
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        # 原生背景（系统亚克力 / Mica）模式：不自己画玻璃，只把表面清成透明，
        # 让 DWM 的模糊层完整透出来（设置面板用这个）。
        if getattr(self, "native_backdrop", False):
            return
        # 拖窗口时关掉抗锯齿：半透明大窗口每移动一像素就要整窗重绘，
        # 抗锯齿是这里最贵的一步；松手后自动恢复，肉眼只在拖动中略糙一点。
        drag = bool(getattr(self, "_window_dragging", False))
        p.setRenderHint(QPainter.Antialiasing, not drag)
        p.setRenderHint(QPainter.SmoothPixmapTransform, not drag)

        ins = self._inset()
        # 悬停放大：围绕**窗口**中心缩放整块内容（窗口中心 = 玻璃中心，
        # 因为余量四边一样）。窗口尺寸不动，所以不会有「整像素跳一格」，
        # 玻璃轮廓是连续变大的。
        z = float(getattr(self, "paint_scale", 1.0) or 1.0)
        if abs(z - 1.0) > 1e-6:
            c = getattr(self, "_paint_c", None) or self.rect().center()
            p.translate(c.x(), c.y())
            p.scale(z, z)
            p.translate(-c.x(), -c.y())
        # 之后所有绘制都用「玻璃自己的坐标」：左上角 (0,0)
        # 收放动画期间窗口是「起点∪终点」那么大、玻璃在里面长/收，所以要先按
        # 现在这块玻璃的偏移平移、尺寸也按它算（见 BoxWidget._paint_rect）。
        box = getattr(self, "_paint_box", None)
        if box is not None and not box.isNull():
            p.translate(box.x(), box.y())
            w = float(box.width())
            h = float(box.height())
        else:
            p.translate(ins, ins)
            w = float(self.width() - ins * 2)
            h = float(self.height() - ins * 2)
        if w <= 0.0 or h <= 0.0:
            return
        plate = QRectF(0.0, 0.0, w, h)

        # 子类可以先把「背后重叠到的内容」画进来，玻璃再盖在上面
        self.paint_backdrop(p)
        self.paint_shadow(p, plate, float(self.radius))

        a = max(60, min(235, int(self.glass_alpha)))
        o = max(0.0, min(1.0, float(getattr(self, "bg_opacity", 1.0) or 0.0)))
        if o < 0.999:
            a = int(round(a * o))
        # 卡片比收纳盒「雾」一点：卡片上全是字，太透会看不清（frost>1 把白的那层
        # 加回来；盒子保持 1.0，就是最透的那种）
        frost = max(0.2, min(3.0, float(getattr(self, "frost", 1.0) or 1.0)))
        base = QLinearGradient(0.0, 0.0, 0.0, h)
        rgb = getattr(self, "glass_rgb", None)
        if isinstance(rgb, QColor) and rgb.isValid():
            # 自定义颜色：同一色相，上浅下深。透明度比默认那条白玻璃厚一档——
            # 按白玻璃 0.36~0.52 的比例给彩色，选什么颜色都看不出来。
            def _stop(target: tuple, t: float, k: float) -> QColor:
                return QColor(
                    int(round(rgb.red() * (1.0 - t) + target[0] * t)),
                    int(round(rgb.green() * (1.0 - t) + target[1] * t)),
                    int(round(rgb.blue() * (1.0 - t) + target[2] * t)),
                    min(255, int(a * k * frost)))

            base.setColorAt(0.0, _stop((255, 255, 255), 0.24, 0.52))
            base.setColorAt(0.55, _stop((255, 255, 255), 0.10, 0.60))
            base.setColorAt(1.0, _stop((0, 0, 0), 0.10, 0.68))
        elif self.dark:
            base.setColorAt(0.0, QColor(46, 50, 63, int(a * 0.88)))
            base.setColorAt(0.55, QColor(26, 29, 38, int(a * 0.94)))
            base.setColorAt(1.0, QColor(16, 18, 24, a))
        else:
            # 白色那层以前压得太满（合计约 58%），换什么壁纸看过去都是一块带雾的
            # 白板；现在压到 30%~40%，壁纸的颜色和明暗都能透上来。
            base.setColorAt(0.0, QColor(255, 255, 255,
                                        min(255, int(a * 0.36 * frost))))
            base.setColorAt(0.55, QColor(250, 251, 255,
                                         min(255, int(a * 0.44 * frost))))
            base.setColorAt(1.0, QColor(236, 242, 252,
                                        min(255, int(a * 0.52 * frost))))

        path = QPainterPath()
        path.addRoundedRect(plate, self.radius, self.radius)
        p.setPen(Qt.NoPen)
        p.setClipPath(path)
        p.fillRect(plate, base)

        if self.tint is not None:
            p.setPen(Qt.NoPen)
            tint = QColor(self.tint)
            tint.setAlpha(int(round(tint.alpha() * o)))
            p.setBrush(tint)
            p.drawRect(plate)

        # 左上柔光，模拟玻璃受光
        light = QRadialGradient(QPointF(w * 0.18, -h * 0.2), w * 1.05)
        top_light = (40 if self.dark else 62) * frost
        top_light = int(min(200, top_light))
        if o < 0.999:
            top_light = int(round(top_light * o))
        light.setColorAt(0.0, QColor(255, 255, 255, top_light))
        light.setColorAt(0.55, QColor(255, 255, 255, int(top_light * 0.3)))
        light.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillRect(plate, light)

        if not self.dark:
            # 右上一点点冷色，避免整块发灰（铺满圆角内，不再画描边）
            cool = QRadialGradient(QPointF(w * 0.95, h * 0.1), w * 0.7)
            cool.setColorAt(0.0, QColor(186, 214, 255, int(round(26 * o))))
            cool.setColorAt(1.0, QColor(186, 214, 255, 0))
            p.fillRect(plate, cool)

        p.setClipping(False)
        # 不再画白色描边：用户觉得和桌面不协调

    # ---- 拖动 ----
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and not getattr(self, "drag_locked", False):
            self._drag = event.globalPosition() - self.frameGeometry().topLeft()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag is not None and event.buttons() & Qt.LeftButton:
            self.move((event.globalPosition() - self._drag).toPoint())
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._drag is not None:
            self._drag = None
            self.on_drag_finished()
        super().mouseReleaseEvent(event)

    def on_drag_finished(self) -> None:
        pass

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Win11 会给窗口画 1px 边框，白玻璃边上就是一条黑线 → 关掉
        no_border(int(self.winId()))
        if self.real_blur and not self._blurred:
            self._blurred = apply_acrylic(int(self.winId()), self.accent_alpha, rgb=self.accent_rgb)
        if getattr(self, "native_backdrop", False):
            apply_backdrop(int(self.winId()), 3)

    def set_acrylic(self, enabled: bool) -> None:
        if self.real_blur and self._blurred:
            apply_acrylic(int(self.winId()), self.accent_alpha, enabled=enabled,
                          rgb=self.accent_rgb)

    def reapply_blur(self) -> None:
        no_border(int(self.winId()))
        self._blurred = apply_acrylic(int(self.winId()), self.accent_alpha, rgb=self.accent_rgb)
        self.update()
