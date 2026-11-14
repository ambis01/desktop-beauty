"""拖完之后的避让运算（收纳盒、卡片共用）。

历史（别再改回去）：
1. 曾经做「边对边吸附」——用户嫌吸附范围太大、会被拽到同一高度，撤了；
2. 后来又按「最短方向推出」——往右半边放会被推到正上方 / 正下方 / 正右边同高，
   用户三次报「强制在上面 / 在下面 / 强制等高」；
3. 现在是：**先只横着让，上下高度一个像素都不动**；横着让不开（贴屏幕边、
   挤了一排）才竖着让；再不行才退回最短位移。任何情况下都不做对齐。

桌面图标不在这里算——那是「谁被压住谁让」的事，压着的图标由
`BoxWidget._push_desktop_icons` / `DesktopIcons.make_room` 挪开。
"""
from __future__ import annotations

from PySide6.QtCore import QRect


def glass_rect(widget) -> QRect:
    """窗口里「玻璃」那一块的屏幕矩形（窗口比玻璃多一圈透明余量）。"""
    g = widget.frameGeometry()
    ins = int(getattr(widget, "inset", 0) or getattr(widget, "_win_margin", lambda: 0)() or 0)
    x = g.x() + ins
    y = g.y() + ins
    w = g.width() - ins * 2
    h = g.height() - ins * 2
    return QRect(x, y, w, h)


def _near(rect: QRect, gap: int) -> QRect:
    return rect.adjusted(-gap, -gap, gap, gap)


def hits(rect: QRect, others, gap: int) -> bool:
    """这块矩形有没有压着别人（留 gap 的缝也算贴上）。"""
    near = _near(rect, gap)
    return any(near.intersects(r) for r in others)


def clash_count(rect: QRect, others, gap: int) -> int:
    near = _near(rect, gap)
    return sum(1 for r in others if near.intersects(r))


def main_clash(rect: QRect, others, gap: int) -> QRect | None:
    """压住的那一堆里，重叠面积最大的那个——往哪边让由它决定。"""
    near = _near(rect, gap)
    best = None
    area_best = 0
    for r in others:
        i = near.intersected(r)
        if i.isEmpty():
            continue
        area = i.width() * i.height()
        if area > area_best:
            best, area_best = r, area
    return best


# 横向和竖向「要让出去多远」差不多的时候，判成横向：用户摆盒子绝大多数是横着
# 排的，往右边放被推到下面是他们最不能接受的那种（报过三次）。竖向要明显更浅
# （比如只压住几个像素）才判竖向。
X_BIAS = 1.25


def place_clear(rect: QRect, others, gap: int, clamp,
                max_shift: int | None = None) -> QRect | None:
    """松手后别叠着：沿**重叠最浅**的那条边推出去。

    三层规矩（都踩过坑）：
      1. **只按真正压住的障碍算**。按「y 带里所有盒子」算的话，远处一个盒子就
         能算出几百像素的位移——用户报的「飞到不知道多远的地方」。
      2. **推最近的一条边**。固定「水平优先」的话，盒子放在别人上面、只压住几
         个像素时会被甩到侧边（用户报的「但凡有一点重叠就直接索到侧边」）。
         横竖差不多远时判横向（`X_BIAS`）。
      3. **绝不跨到对面**（右边出不去就走上下，不走左边）。那看着像盒子自己飞走。
    让不干净（四条边全堵死、或者被屏幕边夹回来）就返回 None，保持原样：
    宁可留一点重叠，也不许盒子跳到别的格子去。

    `max_shift`：一次让开最多挪多远。让开一格还多的距离就不叫「让一下」了，
    叫搬家——挤成一堆的时候宁可压着（用户报的「飞到不知道多远的地方」）。
    """
    if not hits(rect, others, gap):
        return rect
    clashing = [o for o in others if _near(rect, gap).intersects(o)]
    if not clashing:
        return rect
    right = max(o.right() + gap + 1 - rect.left() for o in clashing)
    left = min(o.left() - gap - rect.right() - 1 for o in clashing)
    down = max(o.bottom() + gap + 1 - rect.top() for o in clashing)
    up = min(o.top() - gap - rect.bottom() - 1 for o in clashing)
    horiz = sorted([(abs(right), right, 0), (abs(left), left, 0)])
    vert = sorted([(abs(down), 0, down), (abs(up), 0, up)])
    if horiz[0][0] <= vert[0][0] * X_BIAS:
        # 横着最近：先横向最近的那条，横着出不去才竖着（上下都试，取近的）
        cands = [horiz[0], vert[0], vert[1]]
    else:
        # 竖着明显更近（放在别人上面/下面）：先竖着，堵住了才往最近的那侧让
        cands = [vert[0], horiz[0]]
    for _, dx, dy in cands:
        if max_shift is not None and abs(dx) + abs(dy) > max_shift:
            continue
        cand = clamp(rect.translated(dx, dy))
        if not hits(cand, others, gap):
            return cand
    return None
