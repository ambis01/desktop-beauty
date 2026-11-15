"""拖完之后的避让运算（收纳盒、卡片共用）。

历史（别再改回去）：
1. 曾经做「边对边吸附」——用户嫌吸附范围太大、会被拽到同一高度，撤了；
2. 后来又按「最短方向推出」——往右半边放会被推到正上方 / 正下方 / 正右边同高，
   用户三次报「强制在上面 / 在下面 / 强制等高」；
3. 再后来只留「先横着让，上下一个像素都不动」，横着让不开才竖着让——结果用户
   报「把一个盒子放在两个盒子中间，根本不对齐，只会掉到下面去」（`place_clear`
   同时被两个盒子夹住时四条边全堵死，返回 None，盒子原地压着不动）。
4. 现在是：**先找「贴着别人边角」的最近空位（`corner_slot`）**——盒子落在两个
   盒子的交界处时，哪一半多就往哪个空角里放（上半多 → 交界处右上，下半多 →
   左下），只挪被拖的这个、绝不去推别人。找不到贴着别人边角的空位，才退回
   `place_clear` 的最短位移推出。

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


def stop_y(rect: QRect, want_top: int, others, gap: int) -> int | None:
    """竖直方向挪 rect：想去 want_top，路上被别的矩形挡住就停在它 gap 外。

    挡路只看**横向有交集**的矩形。已经跟谁叠着（距离 < 0）就返回 None —— 这时候
    动哪儿都还是叠着，用户要的是「距离小于 0 就直接不移动」。挪不动也返回 None。
    """
    cur = rect.top()
    if want_top == cur:
        return None
    down = want_top > cur
    stop = want_top
    for o in others:
        if o.left() > rect.right() or o.right() < rect.left():
            continue
        if down:
            if o.top() <= rect.bottom():
                if o.bottom() >= rect.top():
                    return None            # 已经叠着
                continue                   # 在头顶上方：往下走碰不到
            stop = min(stop, o.top() - gap - 1)
        else:
            if o.bottom() >= rect.top():
                if o.top() <= rect.bottom():
                    return None            # 已经叠着
                continue                   # 在下面：往上走碰不到
            stop = max(stop, o.bottom() + gap + 1)
    if down and stop <= cur:
        return None
    if not down and stop >= cur:
        return None
    return stop


def stop_x(rect: QRect, want_left: int, others, gap: int) -> int | None:
    """横向挪 rect：想去 want_left，路上被别的矩形挡住就停在它 gap 外（规则同 stop_y）。"""
    cur = rect.left()
    if want_left == cur:
        return None
    right = want_left > cur
    stop = want_left
    for o in others:
        if o.top() > rect.bottom() or o.bottom() < rect.top():
            continue
        if right:
            if o.left() <= rect.right():
                if o.right() >= rect.left():
                    return None
                continue
            stop = min(stop, o.left() - gap - 1)
        else:
            if o.right() >= rect.left():
                if o.left() <= rect.right():
                    return None
                continue
            stop = max(stop, o.right() + gap + 1)
    if right and stop <= cur:
        return None
    if not right and stop >= cur:
        return None
    return stop


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


# --------------------------------------------------------------- 贴边角对齐
# 松手时的「贴边距」：和 `GAP`（盒子之间留的缝）同一个数量级，看着才像「本来
# 就摆在那儿」，不像被硬推过去的。
SNAP_GAP = 8
# 判定「压着」的容差：留 2px 不算压着（免得为了 2 个像素把盒子挪走）
CLEAR_GAP = 2


def _hug_offsets(o: QRect, w: int, h: int, gap: int) -> tuple[tuple[int, int], ...]:
    """贴着这个障碍物放时，左上角该在哪儿（右/左 × 上/下 四个角）。"""
    return ((o.right() + 1 + gap, o.top() - gap - h),      # 它右边、它上面
            (o.right() + 1 + gap, o.bottom() + 1 + gap),   # 它右边、它下面
            (o.left() - gap - w, o.top() - gap - h),       # 它左边、它上面
            (o.left() - gap - w, o.bottom() + 1 + gap))    # 它左边、它下面


def corner_slot(rect: QRect, others, clamp, max_shift: int | None = None,
                snap_gap: int = SNAP_GAP, clear_gap: int = CLEAR_GAP) -> QRect | None:
    """压着别人的时候：找**贴着别人边角**的最近空位，而不是把盒子推出老远。

    用户要的原话（2026-11-15）：「自适应对齐不要挤开别的收纳盒，C 在 AB 交界处
    上面那部分比较多，就对齐在交界处的右上边区域；下面那部分比较多，就对齐在
    左下边区域」。所以：

      * 候选位置 = 所有障碍物的「左/右边」× 所有障碍物的「上/下边」的组合，
        加上自己现在的 x / y（允许只挪一个方向）。**跨障碍物组合**是关键：右上
        那个角常常是「A 的右边 + B 的上边」，不是同一个盒子的两条边。
      * 夹进屏幕后还能不压着别人的才要；取**挪得最少**的那个，所以盒子落在
        交界处哪一半，最后就进哪个角——不用再单独判断「上半多还是下半多」。
      * 距离一样时优先「贴两条边」的（真正的角），其次优先上下不动。
      * 只管自己，**绝不动别人**；也绝不把盒子甩到屏幕另一头（`max_shift`）。

    没压着别人就原样返回；贴不到任何边角就返回 None，交给 `place_clear`。
    """
    if not hits(rect, others, clear_gap):
        return rect
    w, h = rect.width(), rect.height()
    xs: list[tuple[int, int]] = [(rect.left(), 0)]      # (坐标, 是不是贴着别人的边)
    ys: list[tuple[int, int]] = [(rect.top(), 0)]
    for o in others:
        for cx, cy in _hug_offsets(o, w, h, snap_gap):
            xs.append((cx, 1))
            ys.append((cy, 1))
    best: QRect | None = None
    best_key: tuple[int, int, int] | None = None
    for x, hx in xs:
        for y, hy in ys:
            cand = clamp(QRect(x, y, w, h))
            if hits(cand, others, clear_gap):
                continue
            dist = abs(cand.left() - rect.left()) + abs(cand.top() - rect.top())
            if max_shift is not None and dist > max_shift:
                continue
            key = (dist, -(hx + hy), abs(cand.top() - rect.top()))
            if best_key is None or key < best_key:
                best, best_key = cand, key
    return best
