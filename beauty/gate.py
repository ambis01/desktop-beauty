"""「有盒子正展开着」这一个全局开关：这时候点别处只负责把那个盒子收起来。

用户 2026-11-16 定的：一个收纳盒展开着（状态 1）的时候，点外面的收纳盒 / 卡片
**不能**同时把它点开——否则一个在收起、一个在展开，两个都来抢位置，看着就是
「抽搐」。所以：

  * Controller 启动时注册一个「现在有没有盒子处于展开状态」的取值函数；
  * 盒子 / 卡片在自己不是展开状态时，按下先问一句 `gated()`，是就整下吞掉，
    只让那个展开着的盒子自己的「点外面收起」逻辑生效。

放成独立模块是为了不制造循环 import（box_widget / card_base 都要用）。
"""
from __future__ import annotations

_provider = None


def set_provider(fn) -> None:
    """注册取值函数（Controller 用）：返回 True 表示现在有盒子展开着。"""
    global _provider
    _provider = fn


def gated() -> bool:
    """现在是不是「有盒子展开着」（= 别的窗口这一下不该响应）。"""
    fn = _provider
    if fn is None:
        return False
    try:
        return bool(fn())
    except (RuntimeError, TypeError, AttributeError):
        # 窗口正在销毁之类：别拦着人家，按「没展开」处理
        return False
