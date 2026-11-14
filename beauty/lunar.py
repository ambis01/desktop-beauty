"""农历与二十四节气。

农历部分不依赖任何第三方库：这里存一张**月长表**（每年闰几月、每个月 29 还是
30 天），从 1900 年除夕开始逐月往下算。表是纯事实数据（1900–2098 年），
换算代码在下面自己写。

节气用「寿星公式」：`天数 = int(Y*0.2422 + C) - int((Y-1)/4)`，Y 是年份后两位，
C 是每个节气的系数，2000–2099 年精度足够（这也是各家日历 App 常用的算法）。
"""
from __future__ import annotations

from datetime import date, timedelta

FIRST_YEAR = 1900
LAST_YEAR = 2098

# 每项 = (闰月 << 13) | 月长位图；位图的第 i 位是 1 表示该月 30 天，否则 29 天。
# 闰月 = 0 表示这一年没有闰月。
_TABLE = (
    0x116d2, 0x00752, 0x00ea5, 0x0b64a, 0x0064b, 0x00a9b, 0x09556, 0x0056a, 0x00b59, 0x05752, 0x00752, 0x0db25,
    0x00b25, 0x00a4b, 0x0b4ab, 0x002ad, 0x0056b, 0x04b69, 0x00da9, 0x0fd92, 0x00e92, 0x00d25, 0x0ba4d, 0x00a56,
    0x002b6, 0x095b5, 0x006d4, 0x00ea9, 0x05e92, 0x00e92, 0x0cd26, 0x0052b, 0x00a57, 0x0b2d6, 0x00b5a, 0x006d4,
    0x06ec9, 0x00749, 0x0f693, 0x00a93, 0x0052b, 0x0ca5b, 0x00aad, 0x0056a, 0x09b55, 0x00ba4, 0x00b49, 0x05a93,
    0x00a95, 0x0f52d, 0x00536, 0x00aad, 0x0b5aa, 0x005b2, 0x00ba5, 0x07d4a, 0x00d4a, 0x10a95, 0x00a97, 0x00556,
    0x0cab5, 0x00ad5, 0x006d2, 0x08ea5, 0x00ea5, 0x0064a, 0x06c97, 0x00a9b, 0x0f55a, 0x0056a, 0x00b69, 0x0b752,
    0x00b52, 0x00b25, 0x0964b, 0x00a4b, 0x114ab, 0x002ad, 0x005ad, 0x0cb69, 0x00da9, 0x00d92, 0x09d25, 0x00d25,
    0x15a4d, 0x00a56, 0x002b6, 0x0c5b5, 0x006d5, 0x00ea9, 0x0be92, 0x00e92, 0x00d26, 0x06a56, 0x00a57, 0x114d6,
    0x0035a, 0x006d5, 0x0b6c9, 0x00749, 0x00693, 0x0952b, 0x0052b, 0x00a5b, 0x0555a, 0x0056a, 0x0fb55, 0x00ba4,
    0x00b49, 0x0ba93, 0x00a95, 0x0052d, 0x08aad, 0x00ab5, 0x135aa, 0x005d2, 0x00da5, 0x0dd4a, 0x00d4a, 0x00c95,
    0x0952e, 0x00556, 0x00ab5, 0x055b2, 0x006d2, 0x0cea5, 0x00725, 0x0064b, 0x0ac97, 0x00cab, 0x0055a, 0x06ad6,
    0x00b69, 0x17752, 0x00b52, 0x00b25, 0x0da4b, 0x00a4b, 0x004ab, 0x0a55b, 0x005ad, 0x00b6a, 0x05b52, 0x00d92,
    0x0fd25, 0x00d25, 0x00a55, 0x0b4ad, 0x004b6, 0x005b5, 0x06daa, 0x00ec9, 0x11e92, 0x00e92, 0x00d26, 0x0ca56,
    0x00a57, 0x00556, 0x086d5, 0x00755, 0x00749, 0x06e93, 0x00693, 0x0f52b, 0x0052b, 0x00a5b, 0x0b55a, 0x0056a,
    0x00b65, 0x0974a, 0x00b4a, 0x11a95, 0x00a95, 0x0052d, 0x0caad, 0x00ab5, 0x005aa, 0x08ba5, 0x00da5, 0x00d4a,
    0x07c95, 0x00c96, 0x0f94e, 0x00556, 0x00ab5, 0x0b5b2, 0x006d2, 0x00ea5, 0x08e4a, 0x0068b, 0x10c97, 0x004ab,
    0x0055b, 0x0cad6, 0x00b6a, 0x00752, 0x09725, 0x00b45, 0x00a8b,
)

# 农历 1900-01-31 是正月初一（历法锚点）
_ANCHOR = date(1900, 1, 31)

_MONTH_NAMES = ("正月", "二月", "三月", "四月", "五月", "六月",
                "七月", "八月", "九月", "十月", "冬月", "腊月")

_DAY_NAMES = (
    "初一", "初二", "初三", "初四", "初五", "初六", "初七", "初八", "初九", "初十",
    "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
    "廿一", "廿二", "廿三", "廿四", "廿五", "廿六", "廿七", "廿八", "廿九", "三十",
)

# 二十四节气（按公历顺序，小寒起）
_TERMS = (
    ("小寒", 5.4055), ("大寒", 20.12), ("立春", 3.87), ("雨水", 18.73),
    ("惊蛰", 5.63), ("春分", 20.646), ("清明", 4.81), ("谷雨", 20.1),
    ("立夏", 5.52), ("小满", 21.04), ("芒种", 5.678), ("夏至", 21.37),
    ("小暑", 7.108), ("大暑", 22.83), ("立秋", 7.5), ("处暑", 23.13),
    ("白露", 7.646), ("秋分", 23.042), ("寒露", 8.318), ("霜降", 23.438),
    ("立冬", 7.438), ("小雪", 22.36), ("大雪", 7.18), ("冬至", 21.94),
)

# 节气所在月份（小寒是 1 月，往后每两个一个月份）
_TERM_MONTHS = tuple(m for m in range(1, 13) for _ in range(2))


def _year_row(year: int) -> tuple[int, int]:
    if not FIRST_YEAR <= year <= LAST_YEAR:
        return 0, 0
    raw = _TABLE[year - FIRST_YEAR]
    return raw >> 13, raw & 0x1FFF


def _month_count(leap: int) -> int:
    return 12 + (1 if leap else 0)


def _month_days(leap: int, bits: int, index: int) -> int:
    return 30 if bits >> index & 1 else 29


def _month_of_index(leap: int, index: int) -> tuple[int, bool]:
    """第 index 个农历月（0 起）是几月、是不是闰月。"""
    if not leap:
        return index + 1, False
    if index < leap:
        return index + 1, False
    if index == leap:
        return leap, True
    return index, False


def to_lunar(day: date) -> tuple[int, int, int, bool] | None:
    """公历 → (农历年, 月, 日, 是否闰月)；超出表范围返回 None。"""
    offset = (day - _ANCHOR).days
    if offset < 0:
        return None
    year = FIRST_YEAR
    while year <= LAST_YEAR:
        leap, bits = _year_row(year)
        total = sum(_month_days(leap, bits, i) for i in range(_month_count(leap)))
        if offset < total:
            break
        offset -= total
        year += 1
    else:
        return None
    leap, bits = _year_row(year)
    if not (leap or bits):
        return None
    index = 0
    while index < _month_count(leap):
        days = _month_days(leap, bits, index)
        if offset < days:
            break
        offset -= days
        index += 1
    month, is_leap = _month_of_index(leap, index)
    return year, month, offset + 1, is_leap


def _year_days(year: int) -> int:
    leap, bits = _year_row(year)
    return sum(_month_days(leap, bits, i) for i in range(_month_count(leap)))


def new_year_date(year: int) -> date | None:
    """农历某年的正月初一（公历日期）；超出表范围返回 None。"""
    if not FIRST_YEAR <= year <= LAST_YEAR:
        return None
    return _ANCHOR + timedelta(days=sum(_year_days(y)
                                        for y in range(FIRST_YEAR, year)))


def to_solar(year: int, month: int, day: int, leap: bool = False) -> date | None:
    """农历 → 公历：返回 农历 year 年 month 月 day 日 对应的公历日期。

    纪念日（农历生日、传统节日）要算「今年是哪天」就靠它；该年没有这个闰月
    或者日子超出这个月的天数，返回 None。
    """
    if not 1 <= month <= 12 or day < 1:
        return None
    leap_month, bits = _year_row(year)
    if not (leap_month or bits):
        return None
    index = None
    for i in range(_month_count(leap_month)):
        m, is_leap = _month_of_index(leap_month, i)
        if m == month and is_leap == bool(leap):
            index = i
            break
    if index is None:
        return None
    if day > _month_days(leap_month, bits, index):
        return None
    base = new_year_date(year)
    if base is None:
        return None
    offset = sum(_month_days(leap_month, bits, i) for i in range(index))
    return base + timedelta(days=offset + day - 1)


def lunar_text(day: date) -> str:
    """「十月初六」这种写法；算不出来就返回空串。"""
    got = to_lunar(day)
    if got is None:
        return ""
    _y, month, dom, is_leap = got
    return month_day_text(month, dom, is_leap)


def month_day_text(month: int, day: int, leap: bool = False) -> str:
    """农历的「月+日」写法（不带年份），比如 (8, 15) → 八月十五。"""
    if not (1 <= month <= 12 and 1 <= day <= 30):
        return ""
    name = ("闰" if leap else "") + _MONTH_NAMES[month - 1]
    return name + _DAY_NAMES[day - 1]


def solar_term(day: date) -> str:
    """落在某个节气当天就返回节气名，否则空串。"""
    if not 2000 <= day.year <= 2099:
        return ""
    pairs = list(zip(_TERM_MONTHS, _TERMS))
    for (month, (name, c)) in pairs:
        if month != day.month:
            continue
        y = day.year % 100
        dom = int(y * 0.2422 + c) - int((y - 1) / 4)
        if dom == day.day:
            return name
    return ""


def lunar_line(day: date) -> str:
    """卡片上那一行：「十月初六」或「十月初六 · 立冬」。"""
    text = lunar_text(day)
    term = solar_term(day)
    if text and term:
        return f"{text} · {term}"
    return text or term
