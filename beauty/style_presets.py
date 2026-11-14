"""美化风格方案：只用选，不带参数。

一个方案就是一组玻璃浓度 / 圆角 / 卡片玻璃浓度。名称用 i18n 里的
`style.<id>`，所以这里只放参数。
"""
from __future__ import annotations

PRESETS = (
    {"id": "mist", "box_alpha": 212, "radius": 10, "card_alpha": 212},
    {"id": "gauze", "box_alpha": 186, "radius": 14, "card_alpha": 186},
    {"id": "dense", "box_alpha": 242, "radius": 10, "card_alpha": 242},
)

DEFAULT = "mist"


def get(preset_id: str) -> dict | None:
    for item in PRESETS:
        if item["id"] == preset_id:
            return item
    return None
