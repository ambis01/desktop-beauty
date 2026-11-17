"""音乐：显示当前在放的歌、控制播放。

两条路，优先用第一条：

1. **系统媒体会话（SMTC）**：QQ音乐 / Spotify / 浏览器 放歌时会往 Windows 注册
   一个「媒体会话」（就是按音量键时左上角那个浮层）。能拿到标题、歌手、封面、
   进度、播放状态，还能控制播放、切歌、循环/随机——信息最全。
2. **窗口标题 + 媒体键**：有些播放器（比如网易云音乐 3.x，它内部的 SMTC 开关
   默认是关的、也不会自己开）完全不注册会话。这时退一步：读它的窗口标题
   「歌名 - 歌手」，控制用系统媒体键，封面去网易云的公开接口按「歌名 歌手」
   搜一张。播放/暂停状态只能自己记（卡片上点过的算数）——问不出来：网易云
   暂停之后它的 WASAPI 音频会话还是 active，没法当状态用。
   这条路拿不到进度和循环/随机，所以模式按钮是灰的。

⚠️ 这里**绝不装全局键盘钩子**（WH_KEYBOARD_LL）：为了猜播放状态去挂一个
   所有按键都要过一遍的钩子，一旦回调慢了或者返回值不对，整块键盘就卡死/失灵。
   不值得。键盘上的媒体键按了就按了，卡片图标最多和播放器差一格，点一下按钮就同步。

整条链路跑在一个后台线程里（winsdk 的异步调用要事件循环，也绝不能占着 Qt
主线程）；结果用 Qt 信号送回界面线程，封面只交出原始字节，由界面线程自己
解码成 QPixmap。

多播放器：系统里可能同时挂着好几个会话（网易云放一首、QQ音乐放一首）。整台
机器只开一个 MusicService，全读，再按卡片分：一张卡片配一个会话，卡片拿到
自己的 MusicChannel 只跟那个播放器说话——两张卡片就能各控各的、同时放两首歌。
"""
from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import queue
import re
import threading
import time
import urllib.parse
import urllib.request
from ctypes import wintypes
from typing import Any

from PySide6.QtCore import QObject, Signal

from .dlog import dlog

# 播放模式：卡片上那一个按钮循环的四档（和 SMTC 的 repeat/shuffle 对应）
MODE_SEQUENTIAL = 0      # 顺序播放：repeat=None, shuffle=off
MODE_LIST = 1            # 列表循环：repeat=List
MODE_ONE = 2             # 单曲循环：repeat=Track
MODE_SHUFFLE = 3         # 随机播放：shuffle=on
MODE_NAMES = ("顺序播放", "列表循环", "单曲循环", "随机播放")
MODE_NEXT = {MODE_SEQUENTIAL: MODE_LIST, MODE_LIST: MODE_ONE,
             MODE_ONE: MODE_SHUFFLE, MODE_SHUFFLE: MODE_SEQUENTIAL}

POLL_S = 0.15            # 命令轮询间隔（点一下按钮要马上有反应）
READ_EVERY = 5           # 每 5 个 tick 读一次会话状态（约 0.75s）
EMPTY_RECONNECT = 4      # 连续几轮一个会话都看不到，就重新要一个会话管理器

# 卡片钉住的三种状态（存进 CardData.bind）：
#   ""  = 自动：谁在放跟谁
#   key = 钉死在这个播放器上（比如 "cloudmusic.exe#0"），它没开就空着
#   NO_FOLLOW = 不跟随：谁的都不跟，这张卡一直空着
NO_FOLLOW = "-"

# 兜底路只认这些播放器：窗口标题里带「歌名 - 歌手」的那种
PLAYER_PROCS = {
    "cloudmusic.exe", "qqmusic.exe", "kugou.exe", "kwmusic.exe", "kuwo.exe",
    "spotify.exe", "foobar2000.exe", "aimp.exe", "musicbee.exe",
    "wmplayer.exe", "itunes.exe", "potplayermini64.exe",
}

# 网页播放器的会话 AUMID 里带这些字样。它们能放歌，也能放视频（B站/YouTube），
# 所以不能只看「有会话」就当音乐跟着走（用户 2026-11-17 朋友反馈：跟去 B站了）。
BROWSER_MARKS = ("msedge", "chrome", "firefox", "opera", "brave", "vivaldi",
                 "qqbrowser", "360se", "360chrome", "sogou", "safari", "arc")

# GSMTC PlaybackType：1 = 音乐，2 = 视频
MEDIA_KIND_MUSIC = 1
MEDIA_KIND_VIDEO = 2

VK_MEDIA_NEXT = 0xB0
VK_MEDIA_PREV = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3
KEYEVENTF_KEYUP = 0x0002

# WM_APPCOMMAND：把「上一首/播放/下一首」直接递给某个播放器的窗口。
# 比合成键盘媒体键准——只打这一个播放器，别的程序抢不到；播放器自己挂了
# 键盘钩子、把合成的按键过滤掉的时候，也还认这条。
WM_APPCOMMAND = 0x0319
APPCOMMAND_MEDIA_NEXTTRACK = 11
APPCOMMAND_MEDIA_PREVIOUSTRACK = 12
APPCOMMAND_MEDIA_PLAY_PAUSE = 14
APPCOMMAND_MEDIA_PLAY = 46
APPCOMMAND_MEDIA_PAUSE = 47

# 这些窗口不是「播放器主窗口」，标题也长得像歌名，得排掉
JUNK_WINDOW_CLASSES = {
    "IME", "MSCTFIME UI", "GDI+ Hook Window Class", "Base_PowerMessageWindow",
    "Chrome_SystemMessageWindow", "DesktopLyrics",
}
JUNK_TITLE_MARKS = ("smtc", "default ime", "msctfime", "gdi+ window")
_GUID_RE = re.compile(r"\{[0-9a-f]{8}-[0-9a-f]{4}", re.I)

_user32 = ctypes.windll.user32
_user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
try:
    _user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
except (AttributeError, OSError):
    pass
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]


def _int_or(value, default: int = 0) -> int:
    """播放器给的枚举可能是 None（网易云就不给循环模式），别直接 int()。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _seconds(value) -> float:
    """TimeSpan（datetime.timedelta）或 100ns 计数 → 秒；None / 异常都给 0。"""
    if value is None:
        return 0.0
    try:
        return float(value.total_seconds())
    except AttributeError:
        pass
    try:
        return float(value) / 1e7
    except (TypeError, ValueError):
        return 0.0


def _send_media_key(vk: int) -> None:
    """系统媒体键：不注册会话的播放器也都吃这一套（它们自己挂了键盘钩子）。"""
    try:
        _user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.03)
        _user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    except OSError as exc:
        dlog(f"music: 发媒体键失败 {exc!r}")


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

_PROC_NAMES: dict[int, str] = {}


def _proc_name(pid: int) -> str:
    """pid → 进程名（小写）。拿不到就空，别让一次权限失败打断枚举。"""
    got = _PROC_NAMES.get(pid)
    if got is not None:
        return got
    name = ""
    handle = _kernel32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
    if handle:
        try:
            buf = ctypes.create_unicode_buffer(520)
            size = wintypes.DWORD(len(buf))
            if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                name = buf.value.rsplit("\\", 1)[-1].lower()
        finally:
            _kernel32.CloseHandle(handle)
    if name:
        _PROC_NAMES[pid] = name
    return name


def _window_rows(pid: int = 0) -> list[tuple[int, int, str, str, str, bool]]:
    """顶层窗口清单：(hwnd, pid, 进程名, 标题, 窗口类名, 是否可见)。给 pid 就只列那个进程。"""
    rows: list[tuple[int, int, str, str, str, bool]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):  # noqa: ANN001
        owner = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if pid and owner.value != pid:
            return True
        proc = _proc_name(owner.value)
        if not proc:
            return True
        n = _user32.GetWindowTextLengthW(hwnd)
        title = ""
        if n:
            buf = ctypes.create_unicode_buffer(n + 2)
            _user32.GetWindowTextW(hwnd, buf, n + 2)
            title = buf.value
        cls = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, cls, 256)
        rows.append((hwnd, int(owner.value), proc, title, cls.value,
                     bool(_user32.IsWindowVisible(hwnd))))
        return True

    try:
        _user32.EnumWindows(_cb, 0)
    except OSError:
        return rows
    return rows


def _looks_junk(title: str, cls: str) -> bool:
    """标题像「MediaPlayer SMTC window - {GUID}」这种系统窗口，不是歌。"""
    low = title.lower()
    if any(mark in low for mark in JUNK_TITLE_MARKS):
        return True
    if cls in JUNK_WINDOW_CLASSES:
        return True
    if title.strip() == cls.strip():       # 标题跟窗口类名一样：系统的，不是歌
        return True
    return bool(_GUID_RE.search(title))


def _split_title(raw: str) -> tuple[str, str]:
    """「歌名 - 歌手」拆开；拆不出来就整条当歌名。"""
    for sep in (" - ", " – ", " — ", "-"):
        if sep in raw:
            head, _sep, tail = raw.rpartition(sep)
            head, tail = head.strip(), tail.strip()
            if head and tail:
                return head, tail
    return raw.strip(), ""


def _player_windows() -> list[tuple[int, str, str]]:
    """已知播放器里「标题像 歌名 - 歌手」的窗口：(pid, 进程名, 标题)。

    可见的排前面。这里**不加「必须正在出声」的条件**：播放器暂停之后音频会话
    会变成 inactive、有些（网易云就这么干）连系统媒体会话都撤掉，可窗口标题上
    还挂着这首没放完的歌——只按声音找，就会得到「没有播放器」。
    """
    out: list[tuple[int, str, str, bool]] = []
    seen: set[tuple[int, str]] = set()
    for _hwnd, owner, proc, title, cls, visible in _window_rows():
        if proc not in PLAYER_PROCS or not title:
            continue
        head, artist = _split_title(title)
        if not head or not artist:
            continue
        if _looks_junk(title, cls):
            continue
        mark = (owner, title.strip())
        if mark in seen:          # 主窗口 + 隐藏的「icon」窗口标题一样，只算一个
            continue
        seen.add(mark)
        out.append((owner, proc, title, visible))
    out.sort(key=lambda row: not row[3])
    return [(owner, proc, title) for owner, proc, title, _v in out]


def _send_appcommand(pid: int, code: int) -> bool:
    """把一条 WM_APPCOMMAND 递给这个播放器的窗口。只发一个窗口：两个窗口一起收
    会当成按了两下。优先发「标题像歌名」的那个（主窗口），没有就发第一个可见的。"""
    rows = [row for row in _window_rows(pid) if row[5]]
    if not rows:
        return False
    target = 0
    for hwnd, _owner, _proc, title, cls, _visible in rows:
        head, artist = _split_title(title or "")
        if head and artist and not _looks_junk(title, cls):
            target = hwnd
            break
    if not target:
        target = rows[0][0]
    try:
        _user32.PostMessageW(target, WM_APPCOMMAND, target,
                             wintypes.LPARAM((code << 16) & 0x7FFFFFFF))
    except OSError as exc:
        dlog(f"music: 发 APPCOMMAND 失败 {exc!r}")
        return False
    return True


def _player_session() -> tuple[int, str, bool] | None:
    """哪个已知播放器正在出声：(pid, 进程名, 是否在放)；不在就 None。

    用 WASAPI 的音频会话判断：有 activity 的会话 = 在放；暂停时它会变 inactive。
    """
    try:
        from pycaw.utils import AudioUtilities
    except Exception:  # noqa: BLE001
        return None
    try:
        sessions = list(AudioUtilities.GetAllSessions())
    except Exception:  # noqa: BLE001
        return None
    fallback = None
    for s in sessions:
        proc = s.Process
        if proc is None:
            continue
        try:
            name = proc.name().lower()
            state = int(s.State)
        except Exception:  # noqa: BLE001
            continue
        if name not in PLAYER_PROCS:
            continue
        if state == 1:                       # AudioSessionStateActive
            return proc.pid, name, True
        if fallback is None:
            fallback = (proc.pid, name, False)
    return fallback


def _empty() -> dict[str, Any]:
    return {"has": False, "playing": False, "title": "", "artist": "",
            "album": "", "app": "", "pos": 0.0, "dur": 0.0,
            "mode": MODE_SEQUENTIAL, "can_prev": False, "can_next": False,
            "can_play": False, "can_mode": False, "src": "", "kind": 0}


class MusicChannel(QObject):
    """一张卡片 ⇄ 一个播放器会话。

    卡片只跟自己的通道打交道：这里的 snap / cover_bytes / 控制命令都只对分配给
    它的那个播放器说话。两张卡片、两个播放器就能各放各的，互不串台。
    """

    changed = Signal()        # 这张卡片的状态变了（界面读 self.snap）
    cover_new = Signal()      # 这张卡片的封面换了（界面读 self.cover_bytes 自己解码）

    def __init__(self, svc: "MusicService", card_id: str):
        super().__init__()
        self._svc = svc
        self.card_id = card_id
        self.key = ""            # 服务分到的会话键；"" = 这张卡现在没有播放器
        self.prefer = ""         # 手动指定的会话键；""=自动，"-"=不跟随任何播放器
        self._snap: dict[str, Any] = _empty()
        self._cover_seen: bytes | None = None   # 上次交给界面的封面

    # ---------------------------------------------------------- 给界面读
    @property
    def snap(self) -> dict[str, Any]:
        return self._snap

    @property
    def cover_bytes(self) -> bytes | None:
        return self._svc.covers.get(self.key) if self.key else None

    @property
    def fallback_app(self) -> str:
        """兜底路（这个播放器没注册系统会话）现在跟的是哪个播放器；不在兜底路就空。"""
        if self._snap.get("src") != "title":
            return ""
        return str(self._snap.get("app") or "")

    def position_now(self) -> float:
        """估算此刻的播放进度：上次轮询的位置 + 已经过去的时间。"""
        snap = self._snap
        pos = float(snap.get("pos") or 0.0)
        if snap.get("playing"):
            pos += max(0.0, time.monotonic() - float(snap.get("_at") or 0.0))
        dur = float(snap.get("dur") or 0.0)
        if dur > 0:
            pos = min(pos, dur)
        return max(0.0, pos)

    # ---------------------------------------------------------- 控制
    def play_pause(self) -> None:
        self._svc.send(self.card_id, "toggle")
        self._flip_local_play()

    def press_together(self, other_card_id: str, want: bool) -> None:
        """和另一张卡片「一起按一下」：这一次两个播放器同时放/停，不留任何长期关系。"""
        ids = [self.card_id, str(other_card_id)]
        self._svc.send_together(ids, want)
        self._flip_to(want)
        other = self._svc.channel_of(other_card_id)
        if other is not None:
            other._flip_to(want)
        dlog(f"music: 一起按 {ids} want={want} other={'有' if other else '没找到'}")

    def play(self) -> None:
        self._svc.send(self.card_id, "play")

    def pause(self) -> None:
        self._svc.send(self.card_id, "pause")

    def next(self) -> None:
        self._svc.send(self.card_id, "next")

    def prev(self) -> None:
        self._svc.send(self.card_id, "prev")

    def set_mode(self, mode: int) -> None:
        mode = int(mode)
        self._svc.send(self.card_id, "mode", mode)
        # 先在本地把模式改掉：连点几下的时候，不能拿上一档的旧状态去算下一档；
        # 图标也当场换，不用等播放器下一轮回报
        if self._snap.get("has") and self._snap.get("can_mode"):
            self._snap = dict(self._snap, mode=mode)
            self.changed.emit()

    def cycle_mode(self) -> int:
        """切到下一档，返回切之后的模式（界面立刻可以把图标换掉）。"""
        mode = MODE_NEXT.get(int(self._snap.get("mode", 0)), MODE_SEQUENTIAL)
        self.set_mode(mode)
        return mode

    # ---------------------------------------------------------- 绑定
    def players(self) -> list[dict]:
        """系统里现在有哪些播放器（「跟随播放器」菜单用）。"""
        return self._svc.players()

    def set_prefer(self, key: str) -> None:
        """把这张卡片钉在某个播放器上；key 空 = 自动，"-" = 不跟随任何播放器。"""
        self.prefer = str(key or "")
        self._svc.set_prefer(self.card_id, key)

    def _flip_local_play(self) -> None:
        """点播放/暂停先本地翻一下：卡片立刻有反馈，下一轮查到的真值会覆盖它。"""
        if not self._snap.get("has"):
            return
        self._flip_to(not self._snap.get("playing"))

    def _flip_to(self, playing: bool) -> None:
        """本地把状态改成 playing（界面立刻有反馈），真值下一轮再覆盖。"""
        if not self._snap.get("has"):
            return
        old_playing = bool(self._snap.get("playing"))
        if self._snap.get("src") == "title":
            # 兜底路：这个就是唯一的状态来源（播放器不告诉我们），得记住
            self._svc.fallback_playing = bool(playing)
        self._snap = dict(self._snap, playing=bool(playing),
                          _at=time.monotonic(),
                          pos=self.position_now() if old_playing
                          else float(self._snap.get("pos") or 0.0))
        self.changed.emit()


class MusicService(QObject):
    """所有播放器会话：轮询、按卡片分配、把命令发给对应的那个播放器。

    整个程序共用一个（Controller 里建）。一张卡片问它要 channel(card)，拿到
    只属于自己的一条通道；一个播放器同一时间只会配给一张卡片，配不上就空着。

    会话键是「进程 AUMID#序号」：一般就是 cloudmusic.exe#0 / QQMusic.exe#0；
    同一个播放器开了两份时序号区分（系统给的会话顺序是稳定的）。
    """

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.ok = False
        self.fallback_playing: bool | None = None    # 兜底路的播放状态（自己记）
        self.covers: dict[str, bytes | None] = {}    # 会话键 → 封面字节
        self._channels: list[MusicChannel] = []      # 按登记顺序（= 卡片顺序）
        self._by_card: dict[str, MusicChannel] = {}
        self._sessions: dict[str, Any] = {}          # 会话键 → 会话对象
        self._snaps: dict[str, dict] = {}            # 会话键 → 快照
        self._cover_state: dict[str, dict] = {}      # 会话键 → 取封面的重试状态
        self._logged: dict[str, tuple] = {}          # 会话键 → 上次记日志的能力组合
        self._web_key: tuple[str, str] | None = None
        self._web_tries = 0
        self._web_done = True
        self._web_searching = False
        self._web_cache: dict[tuple[str, str], bytes | None] = {}
        self._empty_streak = 0
        self._hold_pid = 0                # 兜底路上一轮跟的播放器（没人在放时别来回跳）
        self._fallback_sig: tuple | None = None
        self._cmds: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        try:
            import winsdk  # noqa: F401  只探一下装没装
            self.ok = True
        except Exception:  # noqa: BLE001 - 没装就只走「窗口标题」那条路
            dlog("music: winsdk 没装，只走窗口标题 + 媒体键")
        threading.Thread(target=self._run, name="music-smtc", daemon=True).start()

    # ------------------------------------------------------------ 通道
    def channel(self, card) -> MusicChannel:
        """一张卡片一条通道：同一张卡片反复要，拿到的还是同一条。"""
        cid = str(getattr(card, "id", "") or f"anon{id(card)}")
        ch = self._by_card.get(cid)
        if ch is None:
            ch = MusicChannel(self, cid)
            self._by_card[cid] = ch
            self._channels.append(ch)
        ch.prefer = str(getattr(card, "bind", "") or "")
        return ch

    # ------------------------------------------------------------ 手动指定
    def players(self) -> list[dict]:
        """现在系统里有哪些播放器（给「跟随播放器」菜单用）。"""
        out = []
        for key in list(self._sessions):
            snap = self._snaps.get(key) or _empty()
            out.append({"key": key,
                        "app": snap.get("app") or key,
                        "title": snap.get("title") or "",
                        "artist": snap.get("artist") or ""})
        return out

    def set_prefer(self, card_id: str, key: str) -> None:
        """把某张卡片钉在某个播放器上；key 传空 = 回到自动分配，传 "-" = 不跟随。"""
        ch = self._by_card.get(str(card_id))
        if ch is None:
            return
        ch.prefer = str(key or "")
        ch.key = ""            # 下一轮重新分配：手动指定的优先

    def channel_of(self, card_id) -> MusicChannel | None:
        return self._by_card.get(str(card_id))

    def prune(self, card_ids) -> None:
        """卡片被删了：把它的通道也撤掉，别再占着一个播放器。"""
        keep = {str(c) for c in card_ids}
        for cid in list(self._by_card):
            if cid in keep:
                continue
            ch = self._by_card.pop(cid)
            try:
                self._channels.remove(ch)
            except ValueError:
                pass
            ch.deleteLater()

    def send(self, card_id: str, cmd: str, arg=None) -> None:
        self._cmds.put((card_id, cmd, arg))

    def send_together(self, card_ids, want: bool) -> None:
        """两张卡片一起按一下：want=True 一起放，False 一起停（一次性，不记账）。"""
        self._cmds.put(("", "together", ([str(c) for c in card_ids], bool(want))))

    def shutdown(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------ 后台线程
    def _run(self) -> None:
        try:
            asyncio.run(self._loop())
        except Exception as exc:  # noqa: BLE001
            dlog(f"music: 线程停了 {exc!r}")

    async def _loop(self) -> None:
        from winsdk.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Manager,
        )
        mgr = None
        try:
            mgr = await Manager.request_async()
            dlog("music: 已连上系统媒体会话")
        except Exception as exc:  # noqa: BLE001
            dlog(f"music: 连不上系统媒体会话 {exc!r}")
        n = 0
        while not self._stop.is_set():
            try:
                await self._drain(mgr)
                if n % READ_EVERY == 0:
                    seen = await self._read_all(mgr)
                    if seen:
                        self._empty_streak = 0
                    else:
                        self._empty_streak += 1
                        if (mgr is not None
                                and self._empty_streak >= EMPTY_RECONNECT):
                            # 会话管理器是个长活对象：播放器换人（网易云关掉、QQ音乐
                            # 顶上）之后，它可能一直看不见新会话，卡片就莫名其妙空了。
                            # 空了几轮就重新要一个——看得到会话才代表世界真变了。
                            mgr = await self._reconnect(mgr)
                n += 1
            except Exception as exc:  # noqa: BLE001 - 读失败就下一轮再试
                dlog(f"music: 读会话失败 {exc!r}")
            await asyncio.sleep(POLL_S)

    async def _reconnect(self, mgr):
        """重新要一个会话管理器；顺手把「现在到底有几个会话」记进日志。"""
        from winsdk.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Manager,
        )
        self._empty_streak = 0
        try:
            new = await Manager.request_async()
        except Exception as exc:  # noqa: BLE001
            dlog(f"music: 重连会话管理器失败 {exc!r}")
            return mgr
        seen = "?"
        try:
            seen = len(list(new.get_sessions()))
        except Exception as exc:  # noqa: BLE001
            dlog(f"music: 列会话失败 {exc!r}")
        if seen:
            dlog(f"music: 重新连上会话管理器，看到 {seen} 个会话")
        return new

    async def _drain(self, mgr) -> None:
        while True:
            try:
                card_id, cmd, arg = self._cmds.get_nowait()
            except queue.Empty:
                return
            try:
                if cmd == "together":
                    ids, want = arg or ([], None)
                    await self._press_together(list(ids), want)
                else:
                    ch = self._by_card.get(card_id)
                    key = ch.key if ch is not None else ""
                    cur = self._sessions.get(key) if key else None
                    if cur is not None:
                        await self._run_smtc_command(cur, cmd, arg)
                    elif self._has_fallback(ch):
                        self._run_fallback_command(ch, cmd, arg)
                    else:
                        dlog(f"music: 命令 {cmd} 没送出去（卡片 {card_id} "
                             f"key={key!r} 也没有兜底）")
            except Exception as exc:  # noqa: BLE001
                dlog(f"music: 命令 {cmd} 失败 {exc!r}")
            # 连着发好几条时给播放器喘口气，太快它会丢
            await asyncio.sleep(0.06)

    async def _press_together(self, ids: list[str], want: bool | None = None) -> None:
        """两张卡片一起按一下：两个播放器同时放/停（只这一次，不留关系）。"""
        chans = [self._by_card[i] for i in ids if i in self._by_card]
        if not chans:
            return
        if want is None:
            want = not any(ch.snap.get("playing") for ch in chans)
        want = bool(want)
        for ch in chans:
            cur = self._sessions.get(ch.key) if ch.key else None
            if cur is not None:
                if want:
                    await cur.try_play_async()
                else:
                    await cur.try_pause_async()
            elif self._has_fallback(ch):
                if bool(ch.snap.get("playing")) != want:
                    self._fallback_send(ch, APPCOMMAND_MEDIA_PLAY if want
                                        else APPCOMMAND_MEDIA_PAUSE,
                                        VK_MEDIA_PLAY_PAUSE)
        for ch in chans:
            ch._flip_to(want)

    def _has_fallback(self, ch: MusicChannel | None) -> bool:
        """兜底路（窗口标题 + 媒体键）只给第一张卡片用，别的卡片空着就好。"""
        return bool(self._channels) and ch is self._channels[0]

    @staticmethod
    async def _run_smtc_command(cur, cmd: str, arg) -> None:
        if cmd == "toggle":
            # 别用 try_toggle_play_pause_async：有的播放器暂停后不理 toggle，
            # 按现在的真实状态明说要放还是要停，才按得动。
            info = cur.get_playback_info()
            if _int_or(getattr(info, "playback_status", None), 0) == 4:
                await cur.try_pause_async()
            else:
                await cur.try_play_async()
        elif cmd == "play":
            await cur.try_play_async()
        elif cmd == "pause":
            await cur.try_pause_async()
        elif cmd == "next":
            await cur.try_skip_next_async()
        elif cmd == "prev":
            await cur.try_skip_previous_async()
        elif cmd == "mode":
            await MusicService._apply_mode(cur, int(arg))

    def _fallback_send(self, ch: MusicChannel, code: int, vk: int) -> None:
        """没有系统会话时的控制：先按窗口把 APPCOMMAND 递给这个播放器，
        递不到（窗口关了、在托盘里）再退一步发系统媒体键。"""
        pid = int(ch.snap.get("_pid") or 0)
        if pid and _send_appcommand(pid, code):
            return
        _send_media_key(vk)

    def _run_fallback_command(self, ch: MusicChannel, cmd: str, arg) -> None:
        """没有系统会话时：APPCOMMAND / 媒体键。"""
        if cmd == "next":
            self._fallback_send(ch, APPCOMMAND_MEDIA_NEXTTRACK, VK_MEDIA_NEXT)
        elif cmd == "prev":
            self._fallback_send(ch, APPCOMMAND_MEDIA_PREVIOUSTRACK, VK_MEDIA_PREV)
        elif cmd == "toggle":
            self._fallback_send(ch, APPCOMMAND_MEDIA_PLAY_PAUSE, VK_MEDIA_PLAY_PAUSE)
        elif cmd in ("play", "pause"):
            want = cmd == "play"
            if bool(ch.snap.get("playing")) == want:
                return
            self._fallback_send(ch, APPCOMMAND_MEDIA_PLAY if want
                                else APPCOMMAND_MEDIA_PAUSE, VK_MEDIA_PLAY_PAUSE)
        elif cmd == "mode":
            dlog("music: 这个播放器没有系统会话，改不了循环/随机")

    @staticmethod
    async def _apply_mode(cur, mode: int) -> None:
        """把卡片上的四档模式翻译成 SMTC 的 repeat / shuffle。"""
        if mode == MODE_SHUFFLE:
            await cur.try_change_shuffle_active_async(True)
            await cur.try_change_auto_repeat_mode_async(2)   # 列表循环着随机
            return
        await cur.try_change_shuffle_active_async(False)
        repeat = {MODE_SEQUENTIAL: 0, MODE_LIST: 2, MODE_ONE: 1}.get(mode, 0)
        await cur.try_change_auto_repeat_mode_async(repeat)

    # ------------------------------------------------------------ 读状态
    # 这几个字段才跟「状态变了没有」有关：_at / pos 每轮都在变，不能拿它们判断，
    # 否则每轮都 emit 一次、卡片每轮重画一次（看着就是闪）。
    STABLE = ("has", "playing", "title", "artist", "album", "app", "dur",
              "mode", "can_prev", "can_next", "can_play", "can_mode", "src")

    @classmethod
    def _same(cls, a: dict, b: dict) -> bool:
        return all(a.get(k) == b.get(k) for k in cls.STABLE)

    async def _read_all(self, mgr) -> int:
        """把系统里所有会话都读一遍，然后按卡片分下去。返回看到几个会话。"""
        items: list[tuple[str, Any, dict]] = []
        seen: dict[str, int] = {}
        try:
            sessions = list(mgr.get_sessions()) if mgr is not None else []
        except Exception as exc:  # noqa: BLE001
            dlog(f"music: 列会话失败 {exc!r}")
            sessions = []
        for s in sessions:
            aumid = str(s.source_app_user_model_id or "?")
            n = seen.get(aumid, 0)
            seen[aumid] = n + 1
            key = f"{aumid}#{n}"          # 同一个播放器开两份也能分开（顺序稳定）
            try:
                snap, props = await self._read_one(key, s)
            except Exception as exc:  # noqa: BLE001 - 单个播放器读挂了不影响别人
                dlog(f"music: 读会话失败 {key} {exc!r}")
                continue
            items.append((key, s, snap))
            await self._maybe_cover(key, props, snap)

        self._sessions = {k: s for k, s, _snap in items}
        self._snaps = {k: snap for k, _s, snap in items}
        claim = self._plan([k for k, _s, _snap in items])
        for ch in self._channels:
            key = claim.get(ch.card_id, "")
            if not items and ch.prefer != NO_FOLLOW and self._has_fallback(ch):
                # 一个系统会话都没有：第一张卡片退回「读窗口标题」，其余空着
                fb = self._read_fallback()
                if fb is not None:
                    key = "title"
                    self._snaps["title"] = fb
            ch.key = key
            self._apply_to_channel(ch, self._snaps.get(key) if key else None)
        return len(items)

    @staticmethod
    def _is_browser(app: str) -> bool:
        a = (app or "").lower()
        return any(mark in a for mark in BROWSER_MARKS)

    def _is_video(self, key: str) -> bool:
        snap = self._snaps.get(key) or {}
        return _int_or(snap.get("kind"), 0) == MEDIA_KIND_VIDEO

    def _plan(self, keys: list[str]) -> dict[str, str]:
        """卡片 → 会话键。规则（2026-11-17 改）：

        0. 「不跟随」的卡片不参加；
        1. 手动钉死的优先；
        2. 上一轮认得的继续跟 —— 但两个例外：
           * 那个会话是**视频**（B站/YouTube）→ 不自动跟，空着；
           * 它已经暂停、而别的会话正在放 → 换到在放的那个（朋友反馈：关掉
             视频改放歌，卡片还赖在 B站 上）；
        3. 剩下的按「桌面音乐播放器 > 浏览器音乐 > 其它 > 视频；在放的优先」
           补给还没着落的卡片；**视频会话不参与自动分配**，想跟就在菜单里钉。
        """
        claim: dict[str, str] = {}
        used: set[str] = set()
        keys = list(dict.fromkeys(keys))
        app_of = {k: str((self._snaps.get(k) or {}).get("app") or "") for k in keys}
        playing = {k: bool((self._snaps.get(k) or {}).get("playing")) for k in keys}
        video = {k: self._is_video(k) for k in keys}
        browser = {k: self._is_browser(app_of.get(k, "")) for k in keys}

        def score(k: str) -> tuple:
            # 在放的永远排前面（粘性之外最重要的一条）；视频排在最后，但**不排除**：
            # 只用 B站 网页版听歌的人照样能跟上（用户 2026-11-17 澄清：他那台 B站
            # 本来就不跟、跟的是网易云；要修的是「视频会话被当成首选并粘死」）。
            return (0 if playing.get(k) else 1,
                    1 if video.get(k) else 0,
                    1 if browser.get(k) else 0,
                    app_of.get(k, ""), k)

        ranked = sorted(keys, key=score)
        any_playing = any(playing.values())

        # 0) 不跟随
        for ch in self._channels:
            if ch.prefer == NO_FOLLOW:
                claim[ch.card_id] = ""
        # 1) 手动指定的优先（视频会话也允许钉）
        for ch in self._channels:
            if ch.card_id in claim:
                continue
            if ch.prefer and ch.prefer in keys and ch.prefer not in used:
                claim[ch.card_id] = ch.prefer
                used.add(ch.prefer)
        # 2) 上一轮认得的继续跟（视频会话/该换人时除外）
        for ch in self._channels:
            if ch.card_id in claim or not ch.key:
                continue
            if ch.key not in keys or ch.key in used:
                continue
            if not playing.get(ch.key) and any_playing:
                # 它停了/暂停、而别的会话正在放：换过去。这就是朋友那台「关掉视频
                # 改放歌，卡片还赖在 B站 上」的直接原因。
                continue
            claim[ch.card_id] = ch.key
            used.add(ch.key)
        # 3) 剩下的按排好的顺序补齐（视频会话排最后，实在没别的才用它）
        free = [k for k in ranked if k not in used]
        for ch in self._channels:
            if ch.card_id in claim or not free or ch.prefer:
                continue
            claim[ch.card_id] = free.pop(0)
        return claim

    def _apply_to_channel(self, ch: MusicChannel, snap: dict | None) -> None:
        """把某张卡片的新状态 / 新封面交给界面（没变就不打扰它）。"""
        snap = snap or _empty()
        if not self._same(snap, ch._snap):
            ch._snap = snap
            ch.changed.emit()
        else:
            # 只是进度/时间戳变了：留着给进度条算，别惊动界面
            ch._snap = snap
        cover = self.covers.get(ch.key) if ch.key else None
        if cover is not ch._cover_seen:
            ch._cover_seen = cover
            ch.cover_new.emit()

    async def _read_one(self, key: str, cur) -> tuple[dict, Any]:
        info = cur.get_playback_info()
        ctrl = info.controls
        props = await cur.try_get_media_properties_async()
        tl = cur.get_timeline_properties()
        pos = _seconds(getattr(tl, "position", None))
        dur = _seconds(getattr(tl, "end_time", None))
        # 这些字段有些播放器会给 None（网易云就不支持循环/随机），不能直接 int()
        shuffle = bool(getattr(info, "is_shuffle_active", False))
        repeat = _int_or(getattr(info, "auto_repeat_mode", None), 0)
        if shuffle:
            mode = MODE_SHUFFLE
        elif repeat == 1:
            mode = MODE_ONE
        elif repeat == 2:
            mode = MODE_LIST
        else:
            mode = MODE_SEQUENTIAL
        status = _int_or(getattr(info, "playback_status", None), 0)   # 4=playing 5=paused
        snap = {
            "has": True,
            "kind": _int_or(getattr(info, "playback_type", None), 0),
            "playing": status == 4,
            "title": props.title or "",
            "artist": props.artist or "",
            "album": props.album_title or "",
            "app": cur.source_app_user_model_id or "",
            "pos": max(0.0, pos),
            "dur": max(0.0, dur),
            "mode": mode,
            "can_prev": bool(ctrl.is_previous_enabled),
            "can_next": bool(ctrl.is_next_enabled),
            "can_play": bool(ctrl.is_play_enabled or ctrl.is_pause_enabled
                             or ctrl.is_play_pause_toggle_enabled),
            "can_mode": bool(ctrl.is_repeat_enabled or ctrl.is_shuffle_enabled),
            "src": "smtc",
            "_at": time.monotonic(),
        }
        self._note_capabilities(key, snap)
        if snap["has"]:
            self.fallback_playing = None      # 系统会话回来了：兜底的那份状态作废
        return snap, props

    def _read_fallback(self) -> dict[str, Any] | None:
        """没有系统会话时的兜底：窗口标题 + 网上搜封面。"""
        snap = self._read_window_title()
        if snap is None:
            return None
        self._maybe_web_cover(snap)          # 网上找封面另开线程，不挡读状态/发命令
        self._note_capabilities("title", snap)
        return snap

    def _read_window_title(self) -> dict[str, Any] | None:
        """兜底：从播放器的窗口标题读「歌名 - 歌手」。

        播放/暂停状态问不出来，所以自己记：只记卡片上点过的（不监听键盘，见文件头
        说明）。兜底路拿不到进度和循环模式。

        找窗口的条件只要求「是已知播放器 + 标题长得像 歌名 - 歌手」：不要求它此刻
        正在出声。暂停之后网易云会把系统媒体会话撤掉、音频会话变 inactive，只按声音
        找就会得到「没有播放器」；窗口标题上那首歌还在。
        """
        info = _player_session()             # 只用来判断谁在放、顺便排个优先级
        wins = _player_windows()
        if not wins:
            if self._fallback_sig != ():
                self._fallback_sig = ()
                dlog("music: 没有系统会话，也没有一个播放器窗口可读")
            return None
        pick = None
        playing_pid = info[0] if info is not None and info[2] else 0
        order = [playing_pid, int(getattr(self, "_hold_pid", 0) or 0),
                 info[0] if info is not None else 0]
        for want_pid in order:
            if not want_pid:
                continue
            for row in wins:
                if row[0] == want_pid:
                    pick = row
                    break
            if pick is not None:
                break
        if pick is None:
            pick = wins[0]
        pid, proc, raw = pick
        title, artist = _split_title(raw)
        if self._fallback_sig != (proc, raw):
            self._fallback_sig = (proc, raw)
            dlog(f"music: 兜底读窗口 {proc} {raw[:40]!r} "
                 f"（候选 {len(wins)} 个）")
        # 换歌了：一般就是在放（除非它本来就停着，那也很快会被播放器自己的状态纠正）
        prev = self._channels[0].snap if self._channels else {}
        if (title, artist) != (prev.get("title"), prev.get("artist")):
            self.fallback_playing = True
        if self.fallback_playing is None:
            self.fallback_playing = bool(info[2]) if info is not None else True
        playing = bool(self.fallback_playing)
        self._hold_pid = pid
        return {
            "has": True,
            "playing": playing,
            "title": title,
            "artist": artist,
            "album": "",
            "app": proc,
            "pos": 0.0,
            "dur": 0.0,
            "mode": 0,
            "can_prev": True,
            "can_next": True,
            "can_play": True,
            "can_mode": False,      # 兜底路没有循环/随机接口
            "src": "title",
            "_at": time.monotonic(),
            "_pid": pid,
        }

    def _note_capabilities(self, key: str, snap: dict) -> None:
        sig = (snap["app"], snap["src"], snap["can_prev"], snap["can_next"],
               snap["can_play"], snap["can_mode"])
        if self._logged.get(key) == sig:
            return
        self._logged[key] = sig
        dlog(f"music: 会话 {key} {snap['app'] or '?'} src={snap['src']} "
             f"prev={snap['can_prev']} next={snap['can_next']} "
             f"play={snap['can_play']} mode={snap['can_mode']}")

    # ------------------------------------------------------------ 封面
    # 换歌那一瞬间，播放器给系统会话的东西不一定是齐的：可能先换了歌名，
    # 封面过一会儿才到；也可能这一轮读回来的是空流，甚至还是上一首那张图。
    # 所以「读到字节」不等于「读对了」：读不到、或者读到的图跟换歌前那张一模一样，
    # 就下一轮再取，最多试 COVER_TRIES 轮（约 6 秒）。试满还是老样子才认命：
    # 有过字节就按那一步到位（同专辑同封面本来就该一样），一次都没读到就把封面清掉，
    # 免得上一首的图一直赖在卡片上。
    #
    # 每个播放器（会话键）各记各的账：两张卡片同时在放两首歌，封面不能互相顶掉。
    COVER_TRIES = 8

    @staticmethod
    def _md5(data: bytes | None) -> bytes | None:
        return hashlib.md5(data).digest() if data else None

    def _cover_state_of(self, key: str) -> dict:
        st = self._cover_state.get(key)
        if st is None:
            st = {"ck": "", "try": 0, "done": True, "got": None, "prev": None}
            self._cover_state[key] = st
        return st

    async def _maybe_cover(self, key: str, props, snap: dict) -> None:
        """歌换了才去拿封面：那张图动辄几十 KB，不能每轮都读。"""
        st = self._cover_state_of(key)
        ck = f"{snap['title']}\x00{snap['artist']}\x00{snap['album']}"
        if ck != st["ck"]:
            st["ck"] = ck
            st["try"] = 0
            st["done"] = False
            st["got"] = None
            st["prev"] = self._md5(self.covers.get(key))
            dlog(f"music: {key} 换歌取封面 {snap['title'][:24]!r} / "
                 f"{snap['artist'][:20]!r}")
        if st["done"]:
            return
        if st["try"] >= self.COVER_TRIES:
            st["done"] = True
            if st["got"] is not None:
                self.covers[key] = st["got"]      # 试满：这张图就是它的
            elif st["prev"] is not None:
                dlog(f"music: {key} 封面试满没取到 {snap['title'][:24]!r}")
                self.covers[key] = None           # 拿不到就别留上一首的图
            return
        st["try"] += 1
        thumb = getattr(props, "thumbnail", None)
        data = await self._read_thumb(thumb) if thumb is not None else None
        if not data:
            return                    # 这一轮没拿到：下一轮再试
        st["got"] = data
        if st["prev"] is not None and hashlib.md5(data).digest() == st["prev"]:
            return                    # 歌名换了图还没换：下一轮再试
        st["done"] = True
        self.covers[key] = data
        if st["try"] > 1:
            dlog(f"music: {key} 封面 {snap['title'][:24]!r} 第 {st['try']} 轮取到 "
                 f"{len(data)}B")

    @staticmethod
    async def _read_thumb(thumb) -> bytes | None:
        """把系统会话里的封面引用读成字节（读不到就 None）。"""
        try:
            from winsdk.windows.storage.streams import DataReader
            stream = await thumb.open_read_async()
            size = int(stream.size)
            if size <= 0:
                return None
            reader = DataReader(stream)
            await reader.load_async(size)
            buf = bytearray(size)
            reader.read_bytes(buf)
            return bytes(buf)
        except Exception as exc:  # noqa: BLE001
            dlog(f"music: 封面读失败 {exc!r}")
            return None

    WEB_TRIES = 2
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

    def _maybe_web_cover(self, snap: dict) -> None:
        """兜底路没有系统封面：拿「歌名 歌手」去网上搜一张。

        搜索在单独的线程里跑：以前是 `await asyncio.to_thread(...)`，网一慢就把
        整个轮询线程按住几秒——那几秒里卡片上的按钮全哑。结果写进 covers["title"]，
        兜底路只有第一张卡片在用。
        """
        key = (snap["title"], snap["artist"])
        if key != self._web_key:
            self._web_key = key
            self._web_tries = 0
            self._web_done = False
            self.covers["title"] = None        # 换歌了：先撤掉上一首的图
        if self._web_done:
            return
        if key in self._web_cache:
            self._web_done = True
            self.covers["title"] = self._web_cache[key]
            return
        if self._web_tries >= self.WEB_TRIES:
            self._web_done = True
            return
        if self._web_searching:
            return
        self._web_tries += 1
        self._web_searching = True
        threading.Thread(target=self._web_worker, args=(key,), name="music-cover",
                         daemon=True).start()

    def _web_worker(self, key: tuple[str, str]) -> None:
        try:
            data = self._http_cover(key[0], key[1])
        except Exception as exc:  # noqa: BLE001
            dlog(f"music: 网络封面失败 {exc!r}")
            data = None
        finally:
            self._web_searching = False
        if key != self._web_key:
            return
        if data is None:
            return                    # 下一轮再试（最多 WEB_TRIES 次）
        self._web_cache[key] = data
        if len(self._web_cache) > 40:
            self._web_cache.pop(next(iter(self._web_cache)))
        self.covers["title"] = data
        self._web_done = True

    @classmethod
    def _http_json(cls, url: str, headers: dict | None = None) -> Any:
        req = urllib.request.Request(url, headers=headers or {"User-Agent": cls.UA})
        with urllib.request.urlopen(req, timeout=6) as resp:
            raw = resp.read()
        try:
            return json.loads(raw.decode("utf-8", "ignore"))
        except ValueError:
            return None

    @classmethod
    def _netease_pic(cls, query: str) -> str:
        """网易云搜索（返回里直接带专辑封面图）。

        别用 /api/search/get/web：那条现在返回的是一串加密过的 result，取不出来；
        /api/search/get 还是明文，album.picUrl 直接用。
        """
        headers = {"Referer": "https://music.163.com/", "User-Agent": cls.UA}
        try:
            data = cls._http_json(
                "https://music.163.com/api/search/get?type=1&limit=3&s="
                + urllib.parse.quote(query), headers)
        except Exception as exc:  # noqa: BLE001
            dlog(f"music: 网易云搜索失败 {exc!r}")
            return ""
        result = data.get("result") if isinstance(data, dict) else None
        songs = result.get("songs") if isinstance(result, dict) else None
        for song in songs or []:
            if not isinstance(song, dict):
                continue
            pic = str(((song.get("album") or {}).get("picUrl")) or "").strip()
            if pic:
                return pic
        return ""

    @classmethod
    def _itunes_pic(cls, title: str, artist: str) -> str:
        """网易云搜不到（日文歌、小众）就走 iTunes：它有封面、不用登录。"""
        term = urllib.parse.quote(f"{artist} {title}".strip())
        for country in ("", "CN"):
            url = ("https://itunes.apple.com/search?entity=song&limit=5&term=" + term
                   + (f"&country={country}" if country else ""))
            try:
                data = cls._http_json(url)
            except Exception:  # noqa: BLE001
                continue
            for row in (data or {}).get("results") or []:
                art = str((row or {}).get("artworkUrl100") or "")
                if art:
                    return art.replace("100x100", "500x500")
        return ""

    @classmethod
    def _http_cover(cls, title: str, artist: str) -> bytes | None:
        """网易云 → iTunes → 封面图字节。失败就返回 None（卡片显示音符占位）。"""
        if not title:
            return None
        q = f"{title} {artist}".strip()
        pic = cls._netease_pic(q) or cls._itunes_pic(title, artist)
        if not pic:
            return None
        headers = {"User-Agent": cls.UA}
        if "126.net" in pic:
            headers["Referer"] = "https://music.163.com/"
        req = urllib.request.Request(pic, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = resp.read()
        return data or None
