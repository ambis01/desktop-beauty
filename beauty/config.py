"""配置与数据目录：现在是「总开关 + 一组卡片」。"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .hotkey import DEFAULT_PANEL, DEFAULT_TOGGLE, normalize

APP_NAME = "DeskBeauty"
DATA_DIR = Path(
    os.environ.get("DESKBEAUTY_HOME")
    or (Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME)
)
CONFIG_PATH = DATA_DIR / "config.json"
BACKUP_DIR = DATA_DIR / "backups"

DEFAULT_NOTE = "今天也要元气满满"


def system_is_dark() -> bool:
    """Windows「应用」主题是不是深色（面板选「跟随系统」时用）。"""
    try:
        import winreg
        key = (r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as hkey:
            value, _ = winreg.QueryValueEx(hkey, "AppsUseLightTheme")
            return not bool(value)
    except OSError:
        return False


@dataclass
class CardData:
    """一张桌面卡片。type 决定是哪一种：clock / todo / calendar / countdown。"""
    id: str = ""
    type: str = "clock"
    enabled: bool = True
    x: int | None = None
    y: int | None = None
    scale: float = 1.0
    opacity: float = 1.0
    # 默认「留在桌面上」：跟收纳盒同一层，被普通窗口/盒子盖住都正常
    always_on_top: bool = False
    show_seconds: bool = True
    timezone: str = ""          # IANA 时区（如 Asia/Shanghai），空 = 跟随本机
    hour24: bool = True         # 24 小时制 / 12 小时制
    lunar: bool = True          # 显示农历和节气（默认开）
    face: str = "digital"       # 表盘样式：digital / flip / analog
    weather: bool = True        # 卡片右边要不要显示天气
    city: str = ""               # 天气城市：空 = 自动定位（见 weather.CITIES）
    font: str = "segoe"         # 时间数字的字体（见 clock_widget.FONTS）
    note: str = DEFAULT_NOTE
    glass_alpha: int = 212
    # ---- 待办卡片 ----
    todo_show_done: bool = False   # 已完成的要不要也列出来
    # ---- 日历卡片 ----
    cal_monday: bool = True        # 每周从周一开始（否则从周日）
    cal_lunar: bool = True         # 格子下面那行小字（今天 / 节气 / 农历初一）
    # ---- 倒计时卡片 ----
    countdown_pin: str = ""        # 固定显示哪个纪念日；空 = 最近的那个
    # ---- 音乐卡片 ----
    bind: str = ""                 # 手动指定的播放器（会话键，如 QQMusic.exe#0）；空 = 自动分配

    def __post_init__(self) -> None:
        if not self.id:
            self.id = uuid.uuid4().hex[:8]

    @classmethod
    def from_dict(cls, raw: dict) -> "CardData":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass
class Config:
    enabled: bool = True                 # 桌面美化总开关
    cards: list[CardData] = field(default_factory=list)
    taskbar_center: int = 0              # 任务栏按钮中心（物理像素），0=还没量过
    box_glass_alpha: int = 212           # 收纳盒玻璃浓度
    box_radius: int = 10                 # 收纳盒圆角（用户要小一点）
    style_preset: str = "mist"           # 选中的美化风格方案（见 style_presets）
    taskbar_left: bool = True            # 任务栏图标靠左（TaskbarAl=0），启动时写一次
    taskbar_clear: bool = True           # 要不要把任务栏黑边去掉（托管 TranslucentTB）
    taskbar_maximized: str = "acrylic"    # 有窗口最大化时任务栏用哪种：acrylic/clear/normal
    cursor_dot: bool = True              # 桌面上用「圆点拖尾」指针
    cursor_color: str = "#2e3238"        # 圆点颜色（默认深一档，浅灰在深色壁纸上看不见）
    cursor_alpha: int = 100              # 圆点透明度（0~100）
    cursor_schemes: list = field(default_factory=list)   # 保存的鼠标样式方案
    panel_dark: bool = False             # 面板用深色（老字段，等价 panel_theme == dark）
    panel_theme: str = "light"           # 面板主题：light / dark / auto（跟随系统）
    auto_align: bool = True               # 自适应对齐：拖动卡片时自动避让 / 贴边
    box_merge: bool = True                # 允许把一个收纳盒拖到另一个上面合并
    lang: str = "zh"                      # 界面语言：zh / en
    hide_fullscreen: bool = True         # 全屏应用（游戏/视频）时把所有卡片藏起来
    # 全局快捷键（mods 位掩码 + vk 虚拟键码），面板的快捷键页里改
    hotkey_panel: dict = field(default_factory=lambda: dict(DEFAULT_PANEL))
    hotkey_toggle: dict = field(default_factory=lambda: dict(DEFAULT_TOGGLE))

    # ---- 老版本的字段：只在迁移时用，不再写回 ----
    x: int | None = None
    y: int | None = None
    scale: float = 1.0
    opacity: float = 1.0
    # 迁移出来的卡片也必须默认「留在桌面上」，不能一上来就置顶
    always_on_top: bool = False
    locked: bool = False
    show_seconds: bool = True
    note: str = DEFAULT_NOTE
    glass_alpha: int = 212

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        try:
            # utf-8-sig：别的工具（PowerShell、记事本）写回时爱加 BOM，
            # 按 utf-8 读会直接解析失败，然后整份配置被当成默认值
            raw = json.loads(CONFIG_PATH.read_text("utf-8-sig"))
        except (OSError, ValueError):
            cfg._ensure_card()
            return cfg
        if not isinstance(raw, dict):
            cfg._ensure_card()
            return cfg
        cfg.enabled = bool(raw.get("enabled", True))
        cfg.taskbar_center = int(raw.get("taskbar_center", 0) or 0)
        cfg.box_glass_alpha = int(raw.get("box_glass_alpha", 212) or 212)
        cfg.box_radius = int(raw.get("box_radius", 20) or 20)
        cfg.style_preset = str(raw.get("style_preset") or "mist")
        if cfg.style_preset not in ("mist", "gauze", "dense"):
            cfg.style_preset = "mist"
        cfg.taskbar_left = bool(raw.get("taskbar_left", True))
        cfg.taskbar_clear = bool(raw.get("taskbar_clear", True))
        cfg.cursor_dot = bool(raw.get("cursor_dot", True))
        cfg.cursor_color = str(raw.get("cursor_color", "#2e3238") or "#2e3238")
        # 老默认色（浅灰）在深色壁纸上看不清：还是这个值的就换成新的深一档，
        # 用户自己挑过的颜色（别的值）一律不动。
        if cfg.cursor_color.lower() == "#60646c":
            cfg.cursor_color = "#2e3238"
        cfg.cursor_alpha = int(raw.get("cursor_alpha", 100) or 100)
        schemes = raw.get("cursor_schemes")
        cfg.cursor_schemes = [s for s in schemes if isinstance(s, dict)] \
            if isinstance(schemes, list) else []
        cfg.panel_dark = bool(raw.get("panel_dark", False))
        theme = str(raw.get("panel_theme") or "").lower()
        if theme not in ("light", "dark", "auto"):
            theme = "dark" if cfg.panel_dark else "light"
        cfg.panel_theme = theme
        cfg.panel_dark = theme == "dark"
        cfg.auto_align = bool(raw.get("auto_align", True))
        cfg.box_merge = bool(raw.get("box_merge", True))
        cfg.lang = str(raw.get("lang", "zh") or "zh")
        cfg.hide_fullscreen = bool(raw.get("hide_fullscreen", True))
        cfg.hotkey_panel = normalize(raw.get("hotkey_panel") or {}, DEFAULT_PANEL)
        cfg.hotkey_toggle = normalize(raw.get("hotkey_toggle") or {}, DEFAULT_TOGGLE)
        mode = str(raw.get("taskbar_maximized", "acrylic") or "acrylic")
        cfg.taskbar_maximized = mode if mode in ("acrylic", "clear", "normal") else "acrylic"
        cards = raw.get("cards")
        if isinstance(cards, list):
            cfg.cards = [CardData.from_dict(c) for c in cards if isinstance(c, dict)]
        if cfg.style_preset == "mist" and str(raw.get("style_preset") or "") == "frost":
            # 以前那个「毛玻璃」方案已经去掉了：浓度是它留下的 0，恢复成标准值
            cfg.box_glass_alpha = 212
            cfg.box_radius = 10
            for card in cfg.cards:
                card.glass_alpha = 212
        # 老版本只有一张时钟卡片的字段
        if not cfg.cards:
            cfg.x = raw.get("x")
            cfg.y = raw.get("y")
            cfg.scale = float(raw.get("scale", 1.0) or 1.0)
            cfg.opacity = float(raw.get("opacity", 1.0) or 1.0)
            cfg.always_on_top = bool(raw.get("always_on_top", False))
            cfg.show_seconds = bool(raw.get("show_seconds", True))
            cfg.note = raw.get("note", DEFAULT_NOTE) or DEFAULT_NOTE
            cfg.glass_alpha = int(raw.get("glass_alpha", 212) or 212)
        cfg._ensure_card()
        return cfg

    def dark(self) -> bool:
        """面板现在到底是不是深色（auto = 跟 Windows 的应用主题）。"""
        if self.panel_theme == "auto":
            return system_is_dark()
        return self.panel_theme != "light"

    def _ensure_card(self) -> None:
        if self.cards:
            return
        self.cards = [CardData(
            x=self.x, y=self.y, scale=self.scale, opacity=self.opacity,
            always_on_top=self.always_on_top, show_seconds=self.show_seconds,
            note=self.note or DEFAULT_NOTE, glass_alpha=max(self.glass_alpha, 212),
        )]

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "enabled": bool(self.enabled),
            "cards": [asdict(c) for c in self.cards],
            "taskbar_center": int(self.taskbar_center),
            "box_glass_alpha": int(self.box_glass_alpha),
            "box_radius": int(self.box_radius),
            "style_preset": str(self.style_preset),
            "taskbar_left": bool(self.taskbar_left),
            "taskbar_clear": bool(self.taskbar_clear),
            "taskbar_maximized": str(self.taskbar_maximized),
            "cursor_dot": bool(self.cursor_dot),
            "cursor_color": str(self.cursor_color),
            "cursor_alpha": int(self.cursor_alpha),
            "cursor_schemes": list(self.cursor_schemes),
            "panel_dark": bool(self.panel_dark),
            "panel_theme": str(self.panel_theme),
            "auto_align": bool(self.auto_align),
            "box_merge": bool(self.box_merge),
            "lang": str(self.lang),
            "hide_fullscreen": bool(self.hide_fullscreen),
            "hotkey_panel": dict(self.hotkey_panel),
            "hotkey_toggle": dict(self.hotkey_toggle),
        }
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(CONFIG_PATH)


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
