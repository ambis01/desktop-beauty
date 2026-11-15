"""收纳盒的数据模型：盒子索引 + 每个盒子的文件目录。

收纳的方式就是「真的把快捷方式移走」：文件从桌面移到
%APPDATA%\\DeskBeauty\\boxes\\<id>\\，桌面上自然就不显示了；
放回桌面就是把文件移回去。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import desktop_icons as di
from . import ns_items
from .config import DATA_DIR, ensure_dirs
from .shortcut import make_clsid_link

BOX_ROOT = DATA_DIR / "boxes"
INDEX_PATH = DATA_DIR / "boxes.json"
# 桌面的显示名会藏掉这些后缀（资源管理器「隐藏已知文件类型的扩展名」），
# 按显示名找文件时要同时认 name 和 stem。
SHORTCUT_EXT = (".lnk", ".url")
DEBUG = bool(os.environ.get("DESKBEAUTY_DEBUG"))


def _log(msg: str, *args) -> None:
    if not DEBUG:
        return
    try:
        path = os.path.join(os.environ.get("TEMP", "."), "deskbeauty-moves.log")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} {msg % args if args else msg}\n")
    except OSError:
        pass


def _protected_dirs() -> set[Path]:
    """绝对不能整个搬进盒子的目录：桌面本身、用户目录。"""
    out: set[Path] = set()
    try:
        for d in di.desktop_dirs():
            out.add(d.resolve())
        out.add(Path.home().resolve())
    except OSError:
        pass
    return out


def _same_volume(a: Path, b: Path) -> bool:
    """是不是同一个卷。

    同卷 `shutil.move` 就是一次 rename（文件夹多大都是瞬间）；跨卷是整棵树的
    拷贝，而且是在主线程里搬 —— 拖一个几十 G 的目录进来能把界面卡死，所以不收。
    """
    try:
        return os.stat(str(a)).st_dev == os.stat(str(b)).st_dev
    except OSError:
        try:
            return a.anchor.lower() == b.anchor.lower()
        except Exception:  # noqa: BLE001
            return False


# 跨卷（不是同一个卷）只收这么大的东西：同卷 move 是 rename（瞬间），
# 跨卷是整棵拷贝、还在主线程里搬 —— 太大会把界面卡住。约 256 MB。
CROSS_VOLUME_MAX = 256 * 1024 * 1024


def _size_within(path: Path, cap: int) -> bool:
    """path 的总大小是否 <= cap（超了立刻停，不会把整棵大树扫完）。"""
    total = 0
    try:
        if path.is_file():
            return path.stat().st_size <= cap
    except OSError:
        return False
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        # 不跟符号链接：桌面上的快捷方式/junction 可能绕回自己
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
        if total > cap:
            return False
    return True


def _on_desktop(path: Path) -> bool:
    """path 是不是「桌面上的」文件夹（桌面目录本身或它下面的子目录）。"""
    try:
        for d in di.desktop_dirs():
            try:
                root = d.resolve()
            except OSError:
                root = d
            if path == root or root in path.parents:
                return True
    except OSError:
        pass
    return False


def _acceptable(src: Path) -> bool:
    """能收进盒子的东西：任意普通文件，或者**一个文件夹**。

    以前这里只放行 `.lnk` / `.url`（后来又放开到所有文件）。现在文件夹也收，
    但有几条硬保护 —— 文件夹挪走之后不好收拾，而且跨卷拷贝会卡死界面：

    - 文件夹只收**桌面上的**（桌面目录或它下面的子目录），以及盒子里的那些
      （盒子之间互相搬是正常操作）。从资源管理器随便拖一个 `C:/Windows/...` 不行
    - 盘符根、桌面本身、用户目录：不收
    - 别的卷上的东西：**只收 ≤ 256 MB 的**（同卷是 rename，跨卷是整棵拷贝）
    - `DATA_DIR` 内部（盒子目录除外）、以及 `DATA_DIR` 的**祖先**：不收
      （后者等于把装有数据的目录搬进它自己）
    """
    try:
        if not src.is_absolute() or not src.name:
            return False
        is_dir = src.is_dir()
        if not is_dir and not src.is_file():
            return False
        # 桌面的系统文件：收进去 explorer 会重新生成一份，没意义还可能惹事
        if src.name.lower() in ("desktop.ini", "thumbs.db"):
            return False
        resolved = src.resolve()
        data = DATA_DIR.resolve()
        # 盒子之间互相搬东西是正常操作，所以盒子目录里的要放行；
        # 但 DATA_DIR 底下别的（配置 / 日志 / 图标缓存）绝对不许进来。
        if resolved == data:
            return False
        if data in resolved.parents:
            if BOX_ROOT.resolve() not in resolved.parents:
                return False
        elif resolved in data.parents:
            return False
        box_root = BOX_ROOT.resolve()
        in_box = box_root in resolved.parents
        if is_dir:
            if resolved.parent == resolved:
                return False      # 盘符根
            if resolved in _protected_dirs():
                return False
            if not in_box and not _on_desktop(resolved):
                return False      # 只收桌面上的文件夹
        if not _same_volume(resolved, data) and not _size_within(resolved, CROSS_VOLUME_MAX):
            return False          # 跨卷只收小的
        return True
    except OSError:
        return False


def acceptable(path) -> bool:
    """给别的模块用（app / dragwatch 判断「这一项能不能收」）：接受 str / Path。"""
    try:
        return _acceptable(Path(str(path)))
    except (TypeError, ValueError):
        return False


@dataclass
class BoxData:
    id: str
    name: str = "收纳盒"
    x: int = 240
    y: int = 240
    order: list[str] = field(default_factory=list)
    origins: dict[str, str] = field(default_factory=dict)   # 文件名 -> 原目录
    # 命名空间项（此电脑/回收站/控制面板…）：文件名 -> CLSID
    # 这些文件是我们自己生成的 CLSID 快捷方式，桌面上的原图标被隐藏了
    hidden: dict[str, str] = field(default_factory=dict)
    # 常驻展开：三点菜单里勾上之后，点盒子外面也不会收起来
    pinned: bool = False
    # 收起成「只剩那条可拖拽的横杠」（双击拖动区切换），重启后也保持
    bar: bool = False
    # 收起尺寸：auto = 跟着图标数量长（默认），否则是固定格子 "2x2"/"3x3"/...
    size: str = "auto"


def _name_key(name: str) -> tuple:
    """按名称排序的键：数字按大小比，「游戏2」排在「游戏10」前面。"""
    return tuple((0, int(part)) if part.isdigit() else (1, part.casefold())
                 for part in re.split(r"(\d+)", name) if part)


def _unique_path(dest_dir: Path, name: str) -> Path:
    target = dest_dir / name
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for i in range(2, 10000):
        cand = dest_dir / f"{stem} ({i}){suffix}"
        if not cand.exists():
            return cand
    raise FileExistsError(name)


def default_desktop() -> Path:
    dirs = di.desktop_dirs()
    if dirs:
        return dirs[0]
    return Path.home() / "Desktop"


class BoxStore:
    def __init__(self) -> None:
        ensure_dirs()
        BOX_ROOT.mkdir(parents=True, exist_ok=True)
        self.boxes: list[BoxData] = []
        self._load()
        self._drop_excluded_ns()
        self._prune_order()

    def _prune_order(self) -> None:
        """把 order 里已经不存在的名字清掉（删删加加会堆一串同名残留）。"""
        changed = False
        for box in self.boxes:
            folder = self.box_dir(box)
            try:
                names = {p.name for p in folder.iterdir() if p.is_file() or p.is_dir()}
            except OSError:
                continue
            keep = [n for n in box.order if n in names]
            if len(keep) != len(box.order):
                box.order = keep
                changed = True
        if changed:
            self.save()

    def _drop_excluded_ns(self) -> None:
        """把「不许进盒子」的系统图标（回收站）从盒子里请出去。

        老版本把回收站收进过盒子：桌面图标被隐藏 = 没地方可以拖文件回收。
        这里把桌面原图标放出来，并删掉盒子里那个 CLSID 副本。
        """
        changed = False
        for box in self.boxes:
            for name, clsid in list(box.hidden.items()):
                if not ns_items.is_excluded(clsid):
                    continue
                ns_items.set_hidden(clsid, False)
                try:
                    (self.box_dir(box) / name).unlink()
                except OSError:
                    pass
                box.hidden.pop(name, None)
                box.order = [n for n in box.order if n != name]
                _log("dropped excluded ns %s from %s", name, box.id)
                changed = True
        if changed:
            self.save()

    # ---------- 索引 ----------
    def _load(self) -> None:
        try:
            raw = json.loads(INDEX_PATH.read_text("utf-8-sig"))
        except (OSError, ValueError):
            self.boxes = []
            return
        boxes = []
        for item in raw.get("boxes", []):
            try:
                boxes.append(BoxData(
                    id=str(item["id"]),
                    name=str(item.get("name", "收纳盒")),
                    x=int(item.get("x", 240)),
                    y=int(item.get("y", 240)),
                    order=[str(n) for n in item.get("order", [])],
                    origins={str(k): str(v) for k, v in item.get("origins", {}).items()},
                    hidden={str(k): str(v) for k, v in item.get("hidden", {}).items()},
                    pinned=bool(item.get("pinned", False)),
                    bar=bool(item.get("bar", False)),
                    size=str(item.get("size", "auto") or "auto"),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        self.boxes = boxes

    def save(self) -> None:
        ensure_dirs()
        tmp = INDEX_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"boxes": [asdict(b) for b in self.boxes]}, ensure_ascii=False, indent=2,
        ), "utf-8")
        tmp.replace(INDEX_PATH)

    def get(self, box_id: str) -> BoxData | None:
        return next((b for b in self.boxes if b.id == box_id), None)

    def box_dir(self, box: BoxData, create: bool = False) -> Path:
        """盒子目录。**默认不创建**——否则删掉空盒子之后随便读一下又把它建回来了。"""
        path = BOX_ROOT / box.id
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def items(self, box: BoxData) -> list[Path]:
        folder = self.box_dir(box)
        try:
            files = [p for p in folder.iterdir() if p.is_file() or p.is_dir()]
        except OSError:
            return []
        rank = {name: i for i, name in enumerate(box.order)}
        files.sort(key=lambda p: (rank.get(p.name, 10**6), p.name.lower()))
        return files

    # ---------- 增删 ----------
    def create(self, name: str, x: int, y: int, paths=()) -> BoxData:
        box = BoxData(id=uuid.uuid4().hex[:8], name=name or "收纳盒", x=int(x), y=int(y))
        self.boxes.append(box)
        self.box_dir(box, create=True)
        self.add_paths(box, paths)
        self.save()
        return box

    def _owner_of(self, src: Path) -> BoxData | None:
        """这个文件现在躺在哪个盒子目录里；不在任何盒子里返回 None。"""
        try:
            parent = src.resolve().parent
        except OSError:
            return None
        for box in self.boxes:
            try:
                if self.box_dir(box).resolve() == parent:
                    return box
            except OSError:
                continue
        return None

    def add_paths(self, box: BoxData, paths) -> int:
        folder = self.box_dir(box, create=True)
        added = 0
        for raw in paths:
            src = Path(raw)
            if not src.exists() or not _acceptable(src):
                _log("reject %s", src)
                continue
            try:
                if src.resolve().parent == folder.resolve():
                    continue  # 已经在这个盒子里
                owner = self._owner_of(src)
                dest = _unique_path(folder, src.name)
                _log("move %s -> %s", src, dest)
                shutil.move(str(src), str(dest))
            except (OSError, shutil.Error):
                _log("move failed %s", src)
                continue
            # 文件已经离开桌面，但 explorer 的桌面视图要攒着批量刷新（半秒到
            # 一秒）。主动通知它「这个没了」，图标当场消失。
            di.notify_deleted(src)
            box.order.append(dest.name)
            if owner is not None:
                # 从别的盒子拖过来的：原目录和系统图标的 CLSID 得跟着走，
                # 否则以后拖回桌面会落错地方、图标还会变成白纸
                origin = owner.origins.pop(src.name, "")
                clsid = owner.hidden.pop(src.name, None)
                owner.order = [n for n in owner.order if n != src.name]
                if origin:
                    box.origins[dest.name] = origin
                if clsid:
                    box.hidden[dest.name] = clsid
            else:
                box.origins[dest.name] = str(src.parent)
            added += 1
        if added:
            self.save()
        return added

    def add_ns(self, box: BoxData, name: str, clsid: str) -> Path | None:
        """把系统图标收进盒子：生成 CLSID 快捷方式 + 隐藏桌面上的原图标。"""
        if ns_items.is_excluded(clsid):
            _log("ns rejected (excluded) %s %s", name, clsid)
            return None
        folder = self.box_dir(box, create=True)
        try:
            dest = _unique_path(folder, f"{name}.lnk")
        except FileExistsError:
            return None
        if not make_clsid_link(dest, clsid, name):
            _log("clsid link failed %s %s", name, clsid)
            return None
        box.order.append(dest.name)
        box.hidden[dest.name] = clsid
        ns_items.set_hidden(clsid, True)
        self.save()
        return dest

    def is_ns(self, box: BoxData, path: Path) -> bool:
        return path.name in box.hidden

    def restore_ns(self, box: BoxData, path: Path) -> bool:
        """系统图标放回桌面：删掉盒子里那个 CLSID 快捷方式，恢复桌面图标。"""
        clsid = box.hidden.pop(path.name, None)
        if not clsid:
            return False
        box.order = [n for n in box.order if n != path.name]
        try:
            path.unlink()
        except OSError:
            pass
        ns_items.set_hidden(clsid, False)
        self.save()
        return True

    def renamed(self, box: BoxData, old_name: str, new_name: str) -> None:
        """盒子里的图标改了名：order / origins / hidden 里的键跟着换。"""
        box.order = [new_name if n == old_name else n for n in box.order]
        if old_name in box.origins:
            box.origins[new_name] = box.origins.pop(old_name)
        if old_name in box.hidden:
            box.hidden[new_name] = box.hidden.pop(old_name)
        self.save()

    def swap_items(self, box: BoxData, name_a: str, name_b: str) -> None:
        """盒内两个图标换位置（只改 order，不碰文件）。"""
        try:
            i = box.order.index(name_a)
            j = box.order.index(name_b)
        except ValueError:
            return
        box.order[i], box.order[j] = box.order[j], box.order[i]
        self.save()

    def set_order(self, box: BoxData, names: list[str]) -> None:
        """按给的顺序重排盒内图标（只改 order，不碰文件）。

        顺手把已经不在盒子里的旧条目和重复项清掉——以前删删加加会在 order 里
        堆一串同名残留。
        """
        seen: set[str] = set()
        order: list[str] = []
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            order.append(name)
        box.order = order
        self.save()

    def sort_by_name(self, box: BoxData) -> int:
        """按名称重排盒内图标（只改 order，不碰文件）。返回条目数。"""
        names = [p.name for p in self.items(box)]
        names.sort(key=_name_key)
        self.set_order(box, names)
        return len(names)

    def forget(self, box: BoxData, name: str) -> None:
        """文件已经不在盒子里了（删到回收站等）：把索引里那一条抹掉。"""
        box.order = [n for n in box.order if n != name]
        box.origins.pop(name, None)
        clsid = box.hidden.pop(name, None)
        if clsid:
            # 系统图标的快捷方式被删了 → 桌面上的原图标得放出来，否则就真丢了
            ns_items.set_hidden(clsid, False)
        self.save()

    def remove_to(self, box: BoxData, path: Path, dest_dir: Path | None = None) -> Path | None:
        """把盒子里的文件移出去（优先放回原目录，写不进去就退到个人桌面）。"""
        if not (path.is_file() or path.is_dir()):
            return None
        candidates: list[Path] = []
        if dest_dir is not None:
            candidates.append(dest_dir)
        else:
            origin = box.origins.get(path.name)
            if origin:
                candidates.append(Path(origin))
            candidates.append(default_desktop())
        moved: Path | None = None
        for folder in candidates:
            try:
                folder.mkdir(parents=True, exist_ok=True)
                target = _unique_path(folder, path.name)
                _log("restore %s -> %s", path, target)
                shutil.move(str(path), str(target))
                moved = target
                break
            except (OSError, shutil.Error):
                _log("restore failed %s -> %s", path, folder)
                continue
        if moved is None:
            return None
        # 快捷方式没写死图标时 explorer 会渲染成白纸（写死成「用 exe 自己的图标」
        # 也一样慢到抠不出来），回去之前先把图标来源换成我们自己的 .ico
        try:
            from . import icons as _icons
            _icons.fix_shortcut_icon(moved)
        except Exception:  # noqa: BLE001 - 补图标失败不影响放回
            pass
        # 放回桌面的文件要立刻长出图标，否则空等 explorer 批量刷新，看着像没反应
        di.notify_created(moved)
        # 刚从盒子目录搬回来，explorer 偶尔按旧路径缓存画成一张白纸，
        # 再发一次「这一项变了」逼它重读图标（`_land_restored` 踩过同样的坑）。
        # 以前只对 .lnk/.url 发，普通文件（.doc/图片/folder）同样会白，所以全都发。
        di.notify_updated(moved)
        box.order = [n for n in box.order if n != path.name]
        box.origins.pop(path.name, None)
        # 理论上 remove_to 不会被用来放回系统图标（走 restore_ns），这里兜一下：
        # 万一路过，也别让桌面上的原图标一直藏着
        clsid = box.hidden.pop(path.name, None)
        if clsid:
            ns_items.set_hidden(clsid, False)
        self.save()
        return moved

    def merge(self, src: BoxData, dst: BoxData) -> int:
        """把 src 里的东西全并进 dst，然后删掉 src 盒子。返回搬过去的数量。

        系统图标的 CLSID 快捷方式连同 hidden 一起迁移，桌面上的原图标继续
        保持隐藏——合并之后它还在目标盒子里。
        """
        dst_dir = self.box_dir(dst, create=True)
        moved = 0
        for path in self.items(src):
            try:
                dest = _unique_path(dst_dir, path.name)
                shutil.move(str(path), str(dest))
            except (OSError, shutil.Error):
                _log("merge move failed %s", path)
                continue
            dst.order.append(dest.name)
            origin = src.origins.pop(path.name, None)
            if origin:
                dst.origins[dest.name] = origin
            clsid = src.hidden.pop(path.name, None)
            if clsid:
                dst.hidden[dest.name] = clsid
            moved += 1
        src.order = []
        self.save()
        # 还有没搬走的（文件被占用之类）就留着这个盒子，别连文件一起删
        if moved and not self.items(src):
            self.delete(src)
        return moved

    def dissolve(self, box: BoxData) -> int:
        """解散：把图标放回桌面并删除盒子。

        只要有文件没能挪出去，就**保留盒子**，绝不删目录，避免连文件一起没了。
        """
        moved = 0
        stuck: list[str] = []
        for path in self.items(box):
            if self.is_ns(box, path):
                # 系统图标：删掉 CLSID 快捷方式、把桌面图标放出来，不要在桌面留副本
                if self.restore_ns(box, path):
                    moved += 1
                else:
                    stuck.append(path.name)
                continue
            if self.remove_to(box, path):
                moved += 1
            else:
                stuck.append(path.name)
        if stuck:
            _log("dissolve stuck: %s", stuck)
            self.save()
            return moved
        self.delete(box)
        return moved

    def delete(self, box: BoxData, force: bool = False) -> bool:
        folder = BOX_ROOT / box.id
        if not force and folder.is_dir():
            try:
                if any(folder.iterdir()):
                    _log("refuse delete non-empty box %s", folder)
                    return False
            except OSError:
                return False
        # 目录监听/资源管理器偶尔会短暂占着目录，删不掉就重试几次
        for _ in range(5):
            shutil.rmtree(folder, ignore_errors=True)
            if not folder.exists():
                break
            time.sleep(0.04)
        self.boxes = [b for b in self.boxes if b.id != box.id]
        self.save()
        return True

    def rename(self, box: BoxData, name: str) -> None:
        box.name = name.strip() or box.name
        self.save()

    def move(self, box: BoxData, x: int, y: int) -> None:
        box.x, box.y = int(x), int(y)
        self.save()
