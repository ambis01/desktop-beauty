"""全局 UI 缩放。

面板（以及它里面自绘的控件）里所有尺寸、字号都是写死的像素值。想整体放大
不能只改窗口大小：字、内边距、按钮高、圆角、复选框都得跟着乘同一个系数，
不然要么字小一号，要么文字被裁。

所以这里给一个系数，代码里写 `u(12)`、QSS 走 `scale_qss()`，改 UI_SCALE
一处就整体等比放大。
"""
from __future__ import annotations

import re

UI_SCALE = 1.22

# 面板窗口大小（逻辑像素）。参照 DSH 默认窗口（实测约 1295×829 逻辑像素），
# 面板跟它差不多大，字比窗口里的字号略大一点点。
PANEL_SIZE = (1292, 824)
PANEL_MARGIN = 24          # 面板离屏幕边至少留这么多（逻辑像素）


def u(value: float) -> int:
    """像素值乘上缩放系数（至少 1px，免得线被抹平）。"""
    return max(1, int(round(float(value) * UI_SCALE)))


def uf(value: float) -> float:
    """浮点尺寸（画图用的半径之类）。"""
    return float(value) * UI_SCALE


_PX = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)px")


def scale_qss(qss: str) -> str:
    """把 QSS 里所有 `Npx` 按系数放大；颜色、字体名之类不受影响。"""
    return _PX.sub(lambda m: f"{max(1, round(float(m.group(1)) * UI_SCALE))}px", qss)


def panel_rect(area, size=PANEL_SIZE):
    """面板在这块屏幕上的落点：**放不下就缩小**，再居中。

    面板尺寸是写死的逻辑像素（1292×824），逻辑屏幕比它小的机器（比如
    1920×1080@150% = 1280×720、2560×1440@200%）会被裁掉上下，够不到边。
    页面内容本来就在 QScrollArea 里，窗口缩小不会挤爆。
    """
    from PySide6.QtCore import QRect
    w = max(320, min(int(size[0]), int(area.width()) - PANEL_MARGIN))
    h = max(240, min(int(size[1]), int(area.height()) - PANEL_MARGIN))
    x = int(area.x()) + (int(area.width()) - w) // 2
    y = int(area.y()) + (int(area.height()) - h) // 2
    return QRect(x, y, w, h)
