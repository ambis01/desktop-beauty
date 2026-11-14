"""轻量日志：和 app.py 的 `_log` 写同一个 app.log。

卡片、指针这些底层模块没法 import app（会循环引用），但拖动的耗时、卡顿这类
信息只能从它们内部拿到，所以单独放一个小模块。
"""
from __future__ import annotations

import time

from .config import DATA_DIR


def dlog(msg: str) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        p = DATA_DIR / "app.log"
        if p.exists() and p.stat().st_size > 256 * 1024:
            p.write_text("", encoding="utf-8")
        with p.open("a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:  # noqa: BLE001 - 日志写不进去不能影响主流程
        pass
