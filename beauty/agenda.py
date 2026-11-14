"""待办和纪念日：一张卡片的「内容」都存在这里（agenda.json）。

卡片是可换的外观，数据只有一份：所有待办卡看到的是同一份清单，所有倒计时卡
看到的是同一批纪念日。这样加两张卡片不会变成两套互相看不见的数据。

- Todo：一条待办。due 空 = 没定日子，否则是 YYYY-MM-DD。
- Event：一个纪念日，三种模式：
    once    一次性，过了就不显示
    yearly  每年同一天（公历）
    lunar   每年按农历（农历生日、传统节日），date 存 "MM-DD"（农历的月-日）
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from PySide6.QtCore import QObject, Signal

from . import lunar as lunar_mod
from .config import DATA_DIR
from .i18n import tr

AGENDA_PATH = DATA_DIR / "agenda.json"

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

EVENT_MODES = (("once", "一次性"), ("yearly", "每年"), ("lunar", "每年（农历）"))

MODE_LABELS = dict(EVENT_MODES)


# ---------------------------------------------------------------- 解析
def parse_date(text: str) -> date | None:
    """认 2026-06-03 / 2026/6/3 / 2026.6.3 / 20260603 几种写法。"""
    raw = str(text or "").strip().replace("/", "-").replace(".", "-").replace("年", "-") \
        .replace("月", "-").replace("日", "")
    if not raw:
        return None
    if raw.isdigit() and len(raw) == 8:
        raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    parts = [p for p in raw.split("-") if p]
    if len(parts) != 3:
        return None
    try:
        y, m, d = (int(p) for p in parts)
        return date(y, m, d)
    except (TypeError, ValueError):
        return None


def parse_month_day(text: str) -> tuple[int, int] | None:
    """农历的「月-日」：认 08-15 / 8月15 / 八月十五。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    # 中文写法：八月十五 / 农历八月十五（先试它，免得「月」被当成分隔符洗掉）
    names = lunar_mod._MONTH_NAMES          # noqa: SLF001 - 同一套农历表
    days = lunar_mod._DAY_NAMES             # noqa: SLF001
    for i, name in enumerate(names):
        for prefix in (name, "农历" + name):
            if not raw.startswith(prefix):
                continue
            rest = raw[len(prefix):].strip()
            for j, day_name in enumerate(days):
                if rest == day_name:
                    return i + 1, j + 1
    # 数字写法：08-15 / 8-15 / 8月15日
    cleaned = raw
    for ch in ("农历", "月", "日", "号", "-", "/", "."):
        cleaned = cleaned.replace(ch, " ")
    parts = [p for p in cleaned.split() if p]
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        m, d = int(parts[0]), int(parts[1])
        if 1 <= m <= 12 and 1 <= d <= 30:
            return m, d
    return None


def fmt_date(d: date) -> str:
    return f"{d.year}年{d.month}月{d.day}日"


def solar_text(d: date) -> str:
    """2026年11月14日 周六"""
    return f"{fmt_date(d)} {WEEKDAYS[d.weekday()]}"


def lunar_md_text(month: int, day: int) -> str:
    return lunar_mod.month_day_text(month, day)


def lunar_of(d: date) -> str:
    """日期的农历写法（八月十五）；超出农历表范围就空串。"""
    return lunar_mod.lunar_text(d)


def _short_names(items, limit: int = 3) -> str:
    """提醒里最多点出 3 条名字，多的用省略号收尾（别让提示条长到屏幕外）。"""
    names = [str(getattr(t, "text", "") or "").strip() for t in items[:limit]]
    text = "、".join(n for n in names if n)
    return text + "…" if len(items) > limit else text


# ---------------------------------------------------------------- 数据
@dataclass
class Todo:
    id: str = ""
    text: str = ""
    done: bool = False
    due: str = ""              # YYYY-MM-DD，空 = 没定日子
    created: float = 0.0

    def __post_init__(self) -> None:
        if not self.id:
            self.id = uuid.uuid4().hex[:8]
        if not self.created:
            self.created = time.time()

    @property
    def due_date(self) -> date | None:
        return parse_date(self.due)

    def due_label(self, today: date | None = None) -> str:
        d = self.due_date
        if d is None:
            return ""
        today = today or date.today()
        delta = (d - today).days
        if delta == 0:
            return "今天"
        if delta == 1:
            return "明天"
        if delta == 2:
            return "后天"
        if delta == -1:
            return "昨天"
        if d.year != today.year:
            return f"{d.year}年{d.month}月{d.day}日"
        return f"{d.month}月{d.day}日"

    def overdue(self, today: date | None = None) -> bool:
        d = self.due_date
        return bool(d is not None and not self.done
                    and d < (today or date.today()))


@dataclass
class Event:
    id: str = ""
    title: str = ""
    mode: str = "yearly"        # once / yearly / lunar
    date: str = ""              # once、yearly：YYYY-MM-DD；lunar：MM-DD（农历）
    created: float = 0.0

    def __post_init__(self) -> None:
        if not self.id:
            self.id = uuid.uuid4().hex[:8]
        if not self.created:
            self.created = time.time()
        if self.mode not in MODE_LABELS:
            self.mode = "yearly"

    def next_date(self, today: date | None = None) -> date | None:
        """下一次「还有几天」对应的公历日期；过期的一次性事件返回 None。"""
        today = today or date.today()
        if self.mode == "lunar":
            md = parse_month_day(self.date)
            if md is None:
                return None
            month, day = md
            got = lunar_mod.to_lunar(today)
            base = got[0] if got else today.year
            for year in (base, base + 1, base + 2):
                cand = lunar_mod.to_solar(year, month, day)
                if cand is not None and cand >= today:
                    return cand
            return None
        d = parse_date(self.date)
        if d is None:
            return None
        if self.mode == "once":
            return d if d >= today else None
        for year in (today.year, today.year + 1, today.year + 2):
            try:
                cand = date(year, d.month, d.day)
            except ValueError:          # 2 月 29 日：平年跳过
                continue
            if cand >= today:
                return cand
        return None

    def days(self, today: date | None = None) -> int | None:
        nxt = self.next_date(today)
        return None if nxt is None else (nxt - (today or date.today())).days

    def source_text(self) -> str:
        """这个纪念日自己记的是哪一天（公历日期，或「农历八月十五」）。"""
        if self.mode == "lunar":
            md = parse_month_day(self.date)
            if md is None:
                return ""
            return "农历" + lunar_md_text(*md)
        d = parse_date(self.date)
        return solar_text(d) if d else ""


class Agenda(QObject):
    """agenda.json 的读写。任何改动都会 emit changed，所有卡片跟着重画。"""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.todos: list[Todo] = []
        self.events: list[Event] = []
        self.load()

    # ---- 读写 ----
    def load(self) -> None:
        self.todos, self.events = [], []
        try:
            raw = json.loads(AGENDA_PATH.read_text("utf-8-sig"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        for item in raw.get("todos") or []:
            if isinstance(item, dict):
                try:
                    self.todos.append(Todo(**{k: v for k, v in item.items()
                                              if k in Todo.__dataclass_fields__}))
                except TypeError:
                    continue
        for item in raw.get("events") or []:
            if isinstance(item, dict):
                try:
                    self.events.append(Event(**{k: v for k, v in item.items()
                                                if k in Event.__dataclass_fields__}))
                except TypeError:
                    continue

    def save(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            data = {
                "todos": [asdict(t) for t in self.todos],
                "events": [asdict(e) for e in self.events],
            }
            tmp = AGENDA_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
            tmp.replace(AGENDA_PATH)
        except OSError:
            return
        self.changed.emit()

    # ---- 待办 ----
    def todo(self, todo_id: str) -> Todo | None:
        return next((t for t in self.todos if t.id == todo_id), None)

    def add_todo(self, text: str, due: str = "") -> Todo | None:
        text = str(text or "").strip()
        if not text:
            return None
        item = Todo(text=text, due=str(due or "").strip())
        self.todos.append(item)
        self.save()
        return item

    def update_todo(self, todo_id: str, text: str | None = None,
                    due: str | None = None) -> None:
        item = self.todo(todo_id)
        if item is None:
            return
        if text is not None:
            item.text = str(text).strip() or item.text
        if due is not None:
            item.due = str(due).strip()
        self.save()

    def toggle_todo(self, todo_id: str) -> None:
        item = self.todo(todo_id)
        if item is None:
            return
        item.done = not item.done
        self.save()

    def remove_todo(self, todo_id: str) -> None:
        before = len(self.todos)
        self.todos = [t for t in self.todos if t.id != todo_id]
        if len(self.todos) != before:
            self.save()

    def clear_done(self) -> None:
        before = len(self.todos)
        self.todos = [t for t in self.todos if not t.done]
        if len(self.todos) != before:
            self.save()

    def ordered_todos(self, show_done: bool = False,
                      today: date | None = None) -> list[Todo]:
        """卡片上的顺序：有日子的按日期、没日子的按添加时间，完成的排最后。"""
        today = today or date.today()

        def key(item: Todo):
            d = item.due_date
            bucket = 2 if item.done else (0 if d else 1)
            return (bucket, d or date.max, item.created)

        return sorted([t for t in self.todos if show_done or not t.done], key=key)

    def todos_on(self, day: date) -> list[Todo]:
        return [t for t in self.todos if t.due_date == day]

    def open_count(self, today: date | None = None) -> int:
        return sum(1 for t in self.todos if not t.done)

    def remind_text(self, today: date | None = None) -> str:
        """今天到期 / 已经逾期又还没做完的待办，合成一句提醒；没有就返回空串。

        只挑「有日子」的：没定日子的待办没法判断该哪天提醒，硬提醒等于每天
        都在催同一件事。
        """
        today = today or date.today()
        dated = [(t, t.due_date) for t in self.todos if not t.done]
        dated = [(t, d) for t, d in dated if d is not None]
        due_today = [t for t, d in dated if d == today]
        late = [t for t, d in dated if d < today]
        parts: list[str] = []
        if due_today:
            parts.append(tr("todo.remind_due", names=_short_names(due_today)))
        if late:
            parts.append(tr("todo.remind_late", names=_short_names(late)))
        return "；".join(parts)

    # ---- 纪念日 ----
    def event(self, event_id: str) -> Event | None:
        return next((e for e in self.events if e.id == event_id), None)

    def add_event(self, title: str, when: str, mode: str = "yearly") -> Event | None:
        title = str(title or "").strip()
        if not title:
            return None
        mode = mode if mode in MODE_LABELS else "yearly"
        item = Event(title=title, mode=mode, date=str(when or "").strip())
        self.events.append(item)
        self.save()
        return item

    def update_event(self, event_id: str, **changes) -> None:
        item = self.event(event_id)
        if item is None:
            return
        for key, value in changes.items():
            if hasattr(item, key):
                setattr(item, key, value)
        self.save()

    def remove_event(self, event_id: str) -> None:
        before = len(self.events)
        self.events = [e for e in self.events if e.id != event_id]
        if len(self.events) != before:
            self.save()

    def upcoming(self, today: date | None = None) -> list[tuple[Event, date, int]]:
        """[(事件, 下次公历日期, 还有几天)]，按天数从小到大。"""
        today = today or date.today()
        rows = []
        for item in self.events:
            nxt = item.next_date(today)
            if nxt is None:
                continue
            rows.append((item, nxt, (nxt - today).days))
        rows.sort(key=lambda row: (row[2], row[0].created))
        return rows

    def all_events(self, today: date | None = None) -> list:
        """管理窗用：还没到的按天数排，已经过完的一次性事件排最后（好删）。"""
        today = today or date.today()
        rows = self.upcoming(today)
        done = [e for e in self.events if e.next_date(today) is None]
        done.sort(key=lambda e: (parse_date(e.date) or date.min), reverse=True)
        return rows + [(item, None, None) for item in done]

    def events_on(self, day: date, today: date | None = None) -> list[Event]:
        """某一天正好是哪些纪念日（日历卡画小点用）。

        这里不能走 upcoming()：日历翻到别的月份时，那个月里的纪念日也得标出来。
        """
        out = []
        for item in self.events:
            if item.mode == "lunar":
                md = parse_month_day(item.date)
                got = lunar_mod.to_lunar(day)
                if md and got and (got[1], got[2]) == md:
                    out.append(item)
                continue
            d = parse_date(item.date)
            if d is None:
                continue
            if (d == day) if item.mode == "once" \
                    else ((d.month, d.day) == (day.month, day.day)):
                out.append(item)
        return out
