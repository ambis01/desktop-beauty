"""天气：定位、城市表、Open-Meteo 拉数据、本地缓存、后台线程服务。

数据源都是 Open-Meteo（免 key）：
- 天气预报：当前天气 + 逐小时 + 7 天（含紫外线、日出日落）
- 空气质量：US AQI / PM2.5（另一个接口）

「城市」可以是具体城市名，也可以是空串 = 自动定位（按公网 IP 定位一次，缓存
12 小时）。网络不通就退回本地缓存 `%APPDATA%/DeskBeauty/weather.json`。
"""
from __future__ import annotations

import json
import math
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from .config import DATA_DIR

CACHE_PATH = DATA_DIR / "weather.json"
CACHE_TTL = 20 * 60          # 20 分钟内的缓存直接用，不再请求
GEO_TTL = 12 * 3600          # 定位结果缓存 12 小时
TIMEOUT = 8.0

# 城市 → (纬度, 经度)。够用就行，用户还能在设置里自己挑。
CITIES: dict[str, tuple[float, float]] = {
    "北京": (39.9042, 116.4074),
    "上海": (31.2304, 121.4737),
    "广州": (23.1291, 113.2644),
    "深圳": (22.5431, 114.0579),
    "杭州": (30.2741, 120.1551),
    "南京": (32.0603, 118.7969),
    "苏州": (31.2989, 120.5853),
    "成都": (30.5728, 104.0668),
    "重庆": (29.5630, 106.5516),
    "武汉": (30.5928, 114.3055),
    "西安": (34.3416, 108.9398),
    "天津": (39.0842, 117.2009),
    "长沙": (28.2282, 112.9388),
    "郑州": (34.7466, 113.6254),
    "青岛": (36.0671, 120.3826),
    "大连": (38.9140, 121.6147),
    "沈阳": (41.8057, 123.4315),
    "哈尔滨": (45.8038, 126.5350),
    "济南": (36.6512, 117.1201),
    "合肥": (31.8206, 117.2272),
    "福州": (26.0745, 119.2965),
    "厦门": (24.4798, 118.0894),
    "昆明": (25.0389, 102.7183),
    "贵阳": (26.6470, 106.6302),
    "南宁": (22.8170, 108.3665),
    "海口": (20.0442, 110.1999),
    "兰州": (36.0611, 103.8343),
    "乌鲁木齐": (43.8256, 87.6168),
    "呼和浩特": (40.8424, 111.7490),
    "太原": (37.8706, 112.5489),
    "南昌": (28.6820, 115.8579),
    "石家庄": (38.0428, 114.5149),
    "西宁": (36.6171, 101.7782),
    "银川": (38.4872, 106.2309),
    "拉萨": (29.6520, 91.1721),
    "香港": (22.3193, 114.1694),
    "澳门": (22.1987, 113.5439),
    "台北": (25.0330, 121.5654),
}

# 设置面板里下拉只列常用的几个（全塞进菜单会顶到屏幕外面）
CITY_CHOICES = (
    "北京", "上海", "广州", "深圳", "杭州", "南京", "成都", "重庆", "武汉",
    "西安", "天津", "苏州", "青岛", "长沙", "厦门", "沈阳", "哈尔滨", "昆明",
    "南昌",
)

# WMO 天气代码 → (中文名, 图标)
_CODE_MAP: dict[int, tuple[str, str]] = {
    0: ("晴", "sun"),
    1: ("少云", "partly"),
    2: ("多云", "partly"),
    3: ("阴", "cloud"),
    45: ("雾", "fog"),
    48: ("霜雾", "fog"),
    51: ("毛毛雨", "drizzle"),
    53: ("毛毛雨", "drizzle"),
    55: ("毛毛雨", "drizzle"),
    56: ("冻雨", "drizzle"),
    57: ("冻雨", "drizzle"),
    61: ("小雨", "rain"),
    63: ("中雨", "rain"),
    65: ("大雨", "heavy_rain"),
    66: ("冻雨", "heavy_rain"),
    67: ("冻雨", "heavy_rain"),
    71: ("小雪", "snow"),
    73: ("中雪", "snow"),
    75: ("大雪", "snow"),
    77: ("米雪", "snow"),
    80: ("阵雨", "rain"),
    81: ("阵雨", "rain"),
    82: ("强阵雨", "heavy_rain"),
    85: ("阵雪", "snow"),
    86: ("强阵雪", "snow"),
    95: ("雷阵雨", "thunder"),
    96: ("雷暴", "thunder"),
    99: ("雷暴冰雹", "thunder"),
}

_MOON_NAMES = ("新月", "娥眉月", "上弦月", "盈凸月", "满月", "亏凸月", "下弦月", "残月")


def describe(code: int) -> tuple[str, str]:
    return _CODE_MAP.get(int(code), ("未知", "cloud"))


def aqi_label(aqi: float | None) -> str:
    if aqi is None:
        return ""
    v = float(aqi)
    for limit, name in ((50, "优"), (100, "良"), (150, "轻度污染"),
                        (200, "中度污染"), (300, "重度污染")):
        if v <= limit:
            return name
    return "严重污染"


def uv_label(uv: float | None) -> str:
    if uv is None:
        return ""
    v = float(uv)
    for limit, name in ((2, "弱"), (5, "中等"), (7, "强"), (10, "很强")):
        if v <= limit:
            return name
    return "极强"


def moon_phase(day: date) -> tuple[str, int]:
    """月相：返回 (名字, 照亮百分比)。用平均朔望月算，一天内够准。"""
    known_new = datetime(2000, 1, 6, 18, 14)
    synodic = 29.530588853
    when = datetime.combine(day, dtime(12, 0))
    age = ((when - known_new).total_seconds() / 86400.0) % synodic
    illum = int(round(100.0 * (1.0 - math.cos(2.0 * math.pi * age / synodic)) / 2.0))
    limit = (synodic / 8.0)
    index = int((age + limit / 2.0) % synodic // limit) % 8
    return _MOON_NAMES[index], illum


@dataclass
class Weather:
    """一个城市的一份天气快照。"""
    city: str
    temp: float | None = None
    feels: float | None = None
    humidity: int | None = None
    wind: float | None = None
    code: int = 0
    high: float | None = None
    low: float | None = None
    uv: float | None = None
    sunrise: str = ""
    sunset: str = ""
    aqi: int | None = None
    pm25: float | None = None
    hourly: list = field(default_factory=list)     # [(iso, 温度, 代码, 降水概率)]
    daily: list = field(default_factory=list)      # [(iso, 代码, 高温, 低温, 降水概率)]
    stamp: float = 0.0

    @property
    def label(self) -> str:
        return describe(self.code)[0]

    @property
    def icon(self) -> str:
        return describe(self.code)[1]

    @property
    def stale(self) -> bool:
        return time.time() - self.stamp > CACHE_TTL * 3

    def to_dict(self) -> dict:
        return {f: getattr(self, f) for f in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, raw: dict) -> "Weather":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})


# ---------------------------------------------------------------- 定位
_geo_cache: dict = {"stamp": 0.0, "city": "", "lat": None, "lon": None}


def _get_json(url: str):
    with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def locate(force: bool = False) -> tuple[str, float, float] | None:
    """按公网 IP 定位，返回 (城市名, 纬度, 经度)；失败返回 None。"""
    now = time.time()
    if not force and _geo_cache["lat"] is not None \
            and now - _geo_cache["stamp"] < GEO_TTL:
        return (_geo_cache["city"], _geo_cache["lat"], _geo_cache["lon"])
    sources = (
        ("http://ip-api.com/json/?lang=zh-CN", "city", "lat", "lon"),
        ("https://ipwho.is/", "city", "latitude", "longitude"),
    )
    for url, ck, lak, lok in sources:
        try:
            raw = _get_json(url)
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        if raw.get("status") in ("fail", False):
            continue
        city = str(raw.get(ck) or "").strip()
        lat, lon = raw.get(lak), raw.get(lok)
        if not city or lat is None or lon is None:
            continue
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            continue
        _geo_cache.update(stamp=now, city=city, lat=lat, lon=lon)
        return city, lat, lon
    return None


def resolve(key: str) -> tuple[str, float, float] | None:
    """城市 key（空串 = 自动定位）→ (显示名, 纬度, 经度)。"""
    if key:
        pos = CITIES.get(key)
        if pos is None:
            return None
        return key, pos[0], pos[1]
    got = locate()
    if got is None:
        return None
    return got


# ---------------------------------------------------------------- 抓数据
def _forecast(lat: float, lon: float) -> dict | None:
    query = urllib.parse.urlencode({
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                   "wind_speed_10m,weather_code",
        "hourly": "temperature_2m,weather_code,precipitation_probability",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max,uv_index_max,sunrise,sunset",
        "timezone": "auto",
        "forecast_days": "7",
    })
    try:
        return _get_json(f"https://api.open-meteo.com/v1/forecast?{query}")
    except (OSError, ValueError):
        return None


def _air(lat: float, lon: float) -> dict | None:
    query = urllib.parse.urlencode({
        "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
        "current": "us_aqi,pm2_5", "timezone": "auto",
    })
    try:
        return _get_json(f"https://air-quality-api.open-meteo.com/v1/air-quality?{query}")
    except (OSError, ValueError):
        return None


def fetch(key: str) -> Weather | None:
    """同步抓一次（key 是城市名或空串 = 自动定位）；失败返回 None。"""
    found = resolve(key)
    if found is None:
        return None
    name, lat, lon = found
    raw = _forecast(lat, lon)
    if raw is None:
        return None
    try:
        cur = raw.get("current") or {}
        daily = raw.get("daily") or {}
        hourly = raw.get("hourly") or {}
        w = Weather(
            city=name,
            temp=cur.get("temperature_2m"),
            feels=cur.get("apparent_temperature"),
            humidity=cur.get("relative_humidity_2m"),
            wind=cur.get("wind_speed_10m"),
            code=int(cur.get("weather_code") or 0),
            stamp=time.time(),
        )
        times = hourly.get("time") or []
        temps = hourly.get("temperature_2m") or []
        codes = hourly.get("weather_code") or []
        pops = hourly.get("precipitation_probability") or []
        w.hourly = [(times[i], temps[i], int(codes[i] or 0),
                     pops[i] if i < len(pops) else None)
                    for i in range(min(len(times), len(temps), len(codes)))]
        dtimes = daily.get("time") or []
        dcodes = daily.get("weather_code") or []
        dmax = daily.get("temperature_2m_max") or []
        dmin = daily.get("temperature_2m_min") or []
        pprob = daily.get("precipitation_probability_max") or []
        uvs = daily.get("uv_index_max") or []
        rises = daily.get("sunrise") or []
        sets = daily.get("sunset") or []
        w.daily = [(dtimes[i], int(dcodes[i] or 0), dmax[i], dmin[i],
                    pprob[i] if i < len(pprob) else None)
                   for i in range(min(len(dtimes), len(dcodes), len(dmax), len(dmin)))]
        if w.daily:
            w.high = w.daily[0][2]
            w.low = w.daily[0][3]
        if uvs:
            w.uv = uvs[0]
        if rises:
            w.sunrise = str(rises[0]).split("T")[-1]
        if sets:
            w.sunset = str(sets[0]).split("T")[-1]
        air = _air(lat, lon)
        if air:
            cur_air = air.get("current") or {}
            if cur_air.get("us_aqi") is not None:
                w.aqi = int(round(cur_air["us_aqi"]))
            w.pm25 = cur_air.get("pm2_5")
        return w
    except (KeyError, IndexError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 缓存
def _read_cache() -> dict[str, Weather]:
    try:
        raw = json.loads(CACHE_PATH.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    if isinstance(raw, dict):
        for key, item in raw.items():
            if isinstance(item, dict):
                try:
                    out[key] = Weather.from_dict(item)
                except (TypeError, ValueError):
                    continue
    return out


def _write_cache(data: dict[str, Weather]) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: v.to_dict() for k, v in data.items()},
                                  ensure_ascii=False), "utf-8")
        tmp.replace(CACHE_PATH)
    except OSError:
        pass


class WeatherService(QObject):
    """后台线程抓天气，数据放在 self.data[城市key]；有新数据发 updated。"""

    updated = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.data: dict[str, Weather] = _read_cache()
        self._lock = threading.Lock()
        self._busy: set[str] = set()
        self._last: dict[str, float] = {}

    def get(self, key: str) -> Weather | None:
        return self.data.get(key or "")

    def refresh(self, keys, force: bool = False) -> None:
        """异步刷新这些城市（20 分钟内、非强制就跳过）。"""
        now = time.time()
        todo = []
        for key in dict.fromkeys(keys):
            key = key or ""
            if key and key not in CITIES:
                continue
            if key in self._busy:
                continue
            cached = self.data.get(key)
            if not force and cached is not None and now - cached.stamp < CACHE_TTL:
                continue
            if not force and now - self._last.get(key, 0.0) < CACHE_TTL:
                continue
            self._busy.add(key)
            todo.append(key)
        if not todo:
            return
        threading.Thread(target=self._worker, args=(todo,), daemon=True).start()

    def _worker(self, keys) -> None:
        changed = False
        for key in keys:
            got = fetch(key)
            try:
                if got is not None:
                    with self._lock:
                        self.data[key] = got
                    self._last[key] = time.time()
                    changed = True
                else:
                    # 抓失败也记时间，别每 700ms 重试把网络打爆
                    self._last[key] = time.time() - CACHE_TTL + 120
            finally:
                self._busy.discard(key)
        if changed:
            _write_cache(self.data)
            self.updated.emit()
