"""框选之后「一起搬」：收纳盒和卡片共用这一套。

窗口类各写各的拖动（盒子只有顶部横杠能拖，卡片整块玻璃都能拖），但
「谁跟着动、怎么夹进屏幕、松手怎么落盘」只有一份实现，免得两边跑偏：

- `begin()`     按下时：问宿主「这组里还有谁」，记下它们的起点；
- `follow()`    拖动中：兄弟们按**同一个位移**走，相对位置一个像素都不变；
- `finish()`    松手时：整组一起夹进屏幕（按最靠边的那个算，绝不拆开），
                再让每个窗口把自己的位置落盘。

整组不跑单块的避让：组内本来就挨着，按单个盒子避让会把它们拆散。
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect


def begin(widget) -> list:
    """这次要一起搬的兄弟窗口（没多选就是空列表）。"""
    provider = getattr(widget, "on_group", None)
    if provider is None:
        return []
    try:
        return [w for w in (provider() or []) if w is not widget]
    except (RuntimeError, TypeError):
        return []


def follow(widget, origin: QPoint | None, starts) -> None:
    """拖动中：兄弟们跟着同一个位移走。"""
    if origin is None or not starts:
        return
    d = widget.pos() - origin
    if not d.x() and not d.y():
        return
    for w, start in starts:
        try:
            w.move(start + d)
        except RuntimeError:
            continue


def _rects(widget, members) -> list:
    out = []
    for w in [widget] + list(members):
        try:
            r = w._group_rect()
        except (RuntimeError, TypeError, AttributeError):
            continue
        if r is None:
            continue
        out.append((w, QRect(r)))
    return out


def finish(widget, members) -> int:
    """整组夹进屏幕并落盘；返回真的搬了几个窗口。"""
    items = _rects(widget, members)
    if not items:
        return 0
    union = QRect(items[0][1])
    for _, r in items[1:]:
        union = union.united(r)
    try:
        avail = widget._screen()
    except (RuntimeError, TypeError, AttributeError):
        avail = QRect(union)
    dx = 0
    if union.left() < avail.left():
        dx = avail.left() - union.left()
    elif union.right() > avail.right() + 1:
        dx = avail.right() + 1 - union.right()
    dy = 0
    if union.top() < avail.top():
        dy = avail.top() - union.top()
    elif union.bottom() > avail.bottom() + 1:
        dy = avail.bottom() + 1 - union.bottom()
    moved = 0
    for w, _ in items:
        try:
            w._group_shift(dx, dy)
            moved += 1
        except (RuntimeError, TypeError):
            continue
    return moved
