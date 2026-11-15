"""把程序装进这台电脑：复制文件、建快捷方式、开机自启、卸载项。

为什么要有这个东西：分发给别人时对方机器上没有 Python，也没有这个项目的路径，
所以只能给一个打包好的 exe；而 Windows 11 不允许程序自己把图标钉到任务栏
（`TaskbarManager` 是受限功能，`GetDefault()` 直接返回
"Feature com.microsoft.windows.taskbar.pin is not available"，
Shell 动词里也没有 "固定到任务栏" 这一项），所以「钉任务栏」只能人点一下。
除此之外的全部事情——开始菜单、开机自启、桌面快捷方式、卸载项——都由这里包办。

装好之后的结构（和打包目录一致，只是换了位置）：

    %LOCALAPPDATA%\\Programs\\desktopbeauty\\
        desktopbeauty.exe        转发器（任务栏那颗图标点的就是它，120ms）
        target.txt               告诉转发器真正的程序在哪
        app\\desktopbeauty-app.exe  PyInstaller 打出来的本体
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import shutil
import subprocess
import sys
import tempfile
import time
import winreg
from pathlib import Path

from . import __version__, shortcut

APP_NAME = "desktop beauty"
LAUNCHER = "desktopbeauty.exe"
APP_SUBDIR = "app"
APP_EXE = "desktopbeauty-app.exe"
REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\desktopbeauty"

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008

START_ITEM = APP_NAME + ".lnk"
PANEL_ITEM = APP_NAME + " 面板.lnk"


# ---------------------------------------------------------------- 位置
def install_dir() -> Path:
    """程序装到哪。测试时用 DESKBEAUTY_INSTALL_DIR 指到临时目录。"""
    override = os.environ.get("DESKBEAUTY_INSTALL_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
    return Path(base) / "Programs" / "desktopbeauty"


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))


def programs_dir() -> Path:
    override = os.environ.get("DESKBEAUTY_PROGRAMS_DIR")
    if override:
        return Path(override)
    return _appdata() / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def startup_dir() -> Path:
    override = os.environ.get("DESKBEAUTY_STARTUP_DIR")
    if override:
        return Path(override)
    return (programs_dir() / "Startup")


def desktop_dir() -> Path:
    override = os.environ.get("DESKBEAUTY_DESKTOP_DIR")
    if override:
        return Path(override)
    return Path(os.environ.get("USERPROFILE") or Path.home()) / "Desktop"


def reg_key() -> str:
    override = os.environ.get("DESKBEAUTY_REG_KEY")
    return override or REG_KEY


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def dist_root() -> Path:
    """发行目录：打包后是 app 的上一层，源码里是项目根。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent.parent
    return Path(__file__).resolve().parent.parent


def installed_app_exe() -> Path:
    return install_dir() / APP_SUBDIR / APP_EXE


def installed_launcher() -> Path:
    return install_dir() / LAUNCHER


def running_from_install() -> bool:
    """现在这个进程是不是从安装目录跑起来的。"""
    if not is_frozen():
        return False
    try:
        return Path(sys.executable).resolve().parent == (
            install_dir() / APP_SUBDIR).resolve()
    except OSError:
        return False


def is_installed() -> bool:
    return installed_launcher().is_file() and installed_app_exe().is_file()


def dev_install_present() -> bool:
    """这台机器上是不是已经有「源码版」安装（target.txt 指向 python*.exe）。

    有的话不弹安装窗、也不覆盖——开发机上的快捷方式是拿来跑项目的，
    被打包版顶掉的话开机自启就指到别处去了。
    """
    txt = install_dir() / "target.txt"
    try:
        first = txt.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        return False
    return Path(first).name.lower() in ("python.exe", "pythonw.exe")


def state() -> str:
    """source（源码跑）/ portable（打包版，还没装）/ installed（从安装目录跑）。"""
    if not is_frozen():
        return "source"
    return "installed" if running_from_install() else "portable"


def should_offer() -> bool:
    """第一次双击打包版 → 值得问一句「装到这台电脑吗」。"""
    return (state() == "portable" and not dev_install_present()
            and not dismissed())


def _marker() -> Path:
    from .config import DATA_DIR
    return DATA_DIR / "portable_dismissed"


def dismissed() -> bool:
    """这个文件夹点过「直接用」了没有。

    记号里记的是**发行目录**：换个文件夹重新解压会再问一次。只记一个布尔
    值的话，用户把压缩包重下重解到别处，就再也没有安装入口了。
    """
    try:
        return _marker().read_text(encoding="utf-8").strip() == str(
            dist_root().resolve())
    except (OSError, ValueError):
        return False


def mark_dismissed() -> None:
    try:
        _marker().parent.mkdir(parents=True, exist_ok=True)
        _marker().write_text(str(dist_root().resolve()), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------- 安装
def install(src: Path | None = None, force: bool = False) -> bool:
    """把发行目录整个装进 %LOCALAPPDATA%\\Programs\\desktopbeauty。

    返回 True 表示装好了；False 表示这台是开发机、被 force=False 挡住了。
    """
    if dev_install_present() and not force:
        return False
    src = Path(src) if src is not None else dist_root()
    dst = install_dir()
    dst.mkdir(parents=True, exist_ok=True)

    # 0. 覆盖安装：旧版本要是正从安装目录跑着，先请它退出，否则文件全被占着
    stopped = stop_installed()
    if stopped == "timeout":
        raise RuntimeError("旧版本还在运行，文件被占着。先在面板里退出再点安装。")

    # 0b. 自带的那份 TranslucentTB 要是正跑着，也得先停：它是独立进程，注入在
    #     explorer 里的 dll 也从它那个目录加载，不停的话升级时 dll 全被占着。
    #     判据是「**正在跑的那个**镜像在不在我们自己这几个目录里」，不是
    #     `exe_path()`——它只看谁先存在，跑着的可能是另一份（运行副本 / 解压目录）。
    #     停完第 6 步会从安装目录重新拉起。
    roots = (str(dst).lower(), str(src).lower())
    tb_was_running = False
    try:
        from . import taskbar as _tb
        roots = roots + (str(_tb.runtime_dir()).lower(),)
        for pid in _tb.pids():
            image = _image_of(pid)
            if image is None:
                continue
            low = str(image).lower()
            if any(low.startswith(root) for root in roots):
                tb_was_running = True
                break
        if tb_was_running:
            _tb.stop()
    except Exception:  # noqa: BLE001
        pass

    # 1. 本体（PyInstaller 的 app 文件夹）
    app_src = src / APP_SUBDIR
    if not app_src.is_dir():
        raise FileNotFoundError(f"打包目录里没有 {APP_SUBDIR}\\：{src}")
    shutil.copytree(app_src, dst / APP_SUBDIR, dirs_exist_ok=True)

    # 2. 转发器
    launcher_src = src / LAUNCHER
    if not launcher_src.is_file():
        raise FileNotFoundError(f"打包目录里没有 {LAUNCHER}：{src}")
    shutil.copy2(launcher_src, dst / LAUNCHER)

    # 2b. 自带的 TranslucentTB（任务栏去黑边靠它注入 explorer）。没有也能用，
    #     只是任务栏不会透明 —— 以前只有开发机上另装了一份，分出去的包就没人管。
    tb_src = src / "translucenttb"
    if tb_src.is_dir():
        shutil.copytree(tb_src, dst / "translucenttb", dirs_exist_ok=True)

    # 3. target.txt：第一行是程序本体（相对转发器所在目录），第二行是工作目录
    (dst / "target.txt").write_text(
        f"{APP_SUBDIR}\\{APP_EXE}\n{APP_SUBDIR}\n", encoding="utf-8")

    # 4. 快捷方式
    launcher = dst / LAUNCHER
    app_exe = dst / APP_SUBDIR / APP_EXE
    programs = programs_dir()
    icon = str(launcher)
    shortcut.create_lnk(programs / START_ITEM, launcher, "toggle", dst,
                        icon, description=APP_NAME)
    shortcut.create_lnk(programs / PANEL_ITEM, launcher, "panel", dst,
                        icon, description=f"{APP_NAME} 面板")
    # 开机自启直接拉起本体，不经过转发器（转发器是给「点一下」用的）
    shortcut.create_lnk(startup_dir() / START_ITEM, app_exe, "", app_exe.parent,
                        icon, description=APP_NAME)
    shortcut.create_lnk(desktop_dir() / START_ITEM, launcher, "toggle", dst,
                        icon, description=APP_NAME)

    # 5. 卸载项
    _write_uninstall_entry(dst, launcher, app_exe)

    # 6. 刚才停掉的 TranslucentTB 再拉起来（不然任务栏会黑一下，要等 watchdog 8 秒）
    if tb_was_running:
        try:
            from . import taskbar as _tb2
            _tb2.start()
        except Exception:  # noqa: BLE001
            pass
    return True


def _dir_size_kb(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            pass
    return max(1, total // 1024)


def _write_uninstall_entry(dst: Path, launcher: Path, app_exe: Path) -> None:
    key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, reg_key())
    try:
        values = (
            ("DisplayName", APP_NAME),
            ("DisplayIcon", str(launcher)),
            ("DisplayVersion", __version__),
            ("Publisher", APP_NAME),
            ("InstallLocation", str(dst)),
            ("InstallDate", time.strftime("%Y%m%d")),
            ("UninstallString", f'"{app_exe}" --uninstall'),
            ("QuietUninstallString", f'"{app_exe}" --uninstall'),
        )
        for name, value in values:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD,
                          _dir_size_kb(dst))
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
    finally:
        key.Close()


def install_shortcuts_only() -> None:
    """给已经装好的目录补快捷方式（比如用户手动删了开始菜单那一项）。"""
    dst = install_dir()
    launcher = dst / LAUNCHER
    app_exe = dst / APP_SUBDIR / APP_EXE
    icon = str(launcher)
    programs = programs_dir()
    shortcut.create_lnk(programs / START_ITEM, launcher, "toggle", dst,
                        icon, description=APP_NAME)
    shortcut.create_lnk(programs / PANEL_ITEM, launcher, "panel", dst,
                        icon, description=f"{APP_NAME} 面板")
    shortcut.create_lnk(startup_dir() / START_ITEM, app_exe, "", app_exe.parent,
                        icon, description=APP_NAME)


# ---------------------------------------------------------------- 卸载
_kernel32 = ctypes.WinDLL("kernel32")
_kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_kernel32.OpenProcess.restype = ctypes.c_void_p
_kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
_kernel32.QueryFullProcessImageNameW.argtypes = [
    ctypes.c_void_p, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
_kernel32.QueryFullProcessImageNameW.restype = wt.BOOL


def running_app_pid() -> int | None:
    """常驻实例的 pid：QLockFile 里第一行就是它。"""
    try:
        text = (Path(tempfile.gettempdir()) / "deskbeauty.lock").read_text(
            encoding="utf-8")
        return int(text.splitlines()[0].strip())
    except (OSError, ValueError, IndexError):
        return None


def _image_of(pid: int) -> Path | None:
    """这个 pid 的可执行文件全路径。"""
    handle = _kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(1024)
        if not _kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size)):
            return None
        return Path(buf.value) if buf.value else None
    finally:
        _kernel32.CloseHandle(handle)


def _is_our_instance() -> bool:
    """正在跑的那个是不是「这份安装」里的本体。

    比一下进程镜像路径：源码版（pythonw.exe）或者另一个位置的副本，
    都不算——那种情况下不能让它去毁灭世界（会把人家自己的桌面清掉）。
    """
    pid = running_app_pid()
    if not pid:
        return False
    image = _image_of(pid)
    if image is None:
        return False
    try:
        return image.resolve() == installed_app_exe().resolve()
    except OSError:
        return False


def installed_version() -> str:
    """注册表里记的已装版本；没装过返回空串。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_key()) as key:
            value, _kind = winreg.QueryValueEx(key, "DisplayVersion")
            return str(value)
    except OSError:
        return ""


def stop_installed(timeout: float = 20.0) -> str:
    """覆盖安装前把「这份安装」里正在跑的实例停掉。

    不停的话 `app\\desktopbeauty-app.exe` 和 `app\\_internal\\*.dll` 全被占着，
    `copytree` 直接 PermissionError。只认自己这份安装的进程（镜像路径 = 安装目录
    里那个本体），源码版或者别的目录里的副本不动。

    返回：none（没在跑）/ other（跑的是别的实例，不动）/ stopped / timeout。
    """
    from . import signal as sig

    pid = running_app_pid()
    if not pid or _image_of(pid) is None:
        return "none"
    if not _is_our_instance():
        return "other"
    # 不能只发一次：进程刚启动的那几百毫秒里，锁已经写好了、但信号事件还没建出来
    # （main() 先 tryLock 再构造 Controller），这时 send 会失败，之后就再也等不到。
    deadline = time.time() + timeout
    sent = False
    while time.time() < deadline:
        if not sent:
            sent = sig.send("quit")
        if _image_of(pid) is None:
            return "stopped"
        time.sleep(0.1)
    return "timeout"


def _boxes_left() -> int:
    """`%APPDATA%\\DeskBeauty\\boxes` 里还剩几个盒子（每个盒子一个目录）。"""
    from .config import DATA_DIR
    try:
        return sum(1 for p in (DATA_DIR / "boxes").iterdir() if p.is_dir())
    except OSError:
        return 0


def _run_reset_quit(timeout: float) -> bool:
    """拉起一份只做「毁灭世界 + 退出」的实例，等它自己退干净。"""
    exe = installed_app_exe()
    if not exe.is_file():
        return False
    try:
        subprocess.run([str(exe), "--reset-quit"], cwd=str(exe.parent),
                       timeout=timeout, check=False,
                       creationflags=CREATE_NO_WINDOW)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def wipe_before_uninstall(timeout: float = 45.0) -> str:
    """卸载前先做一次「毁灭世界」：图标全部放回桌面，卡片清空。

    返回发生了什么（测试和日志用）：
      reset      —— 通知正在跑的实例，等它放完图标退出
      spawned    —— 进程没在跑但盒子还在，拉起一份 `--reset-quit` 做完
      noop       —— 没在跑，也没有盒子，没什么要放的
      other      —— 跑着的是别的实例（源码版 / 别处的副本），不动它
      timeout    —— 通知了但没在 timeout 内退出
    等待用的是**进程本身还在不在**，不是那个 `quit` 事件：命名事件是整个
    登录会话共享的，只要还有别的实例（比如开发机上的源码版）活着，事件就
    一直在，拿它当「退干净了吗」会白等到超时。
    """
    from . import signal as sig

    what = "noop"
    pid = running_app_pid()
    if pid and _image_of(pid) is not None:
        if not _is_our_instance():
            return "other"
        # 老版本没有 reset 事件：退而求其次，至少让它退出
        if not sig.send("reset"):
            sig.send("quit")
        deadline = time.time() + timeout
        what = "timeout"
        while time.time() < deadline:
            if _image_of(pid) is None:
                what = "reset"
                break
            time.sleep(0.1)

    # 保险：盒子目录还在就再拉一份做完（老版本没有 reset、或者有文件挪不出去）
    if _boxes_left() and what != "other":
        if _run_reset_quit(timeout):
            what = "spawned" if what == "noop" else what + "+spawned"
    return what


def uninstall(remove_files: bool = True, wipe: bool = True) -> str:
    """删快捷方式、卸载项；**先把图标放回桌面**，再删程序文件。

    顺序：毁灭世界（= 面板里那个按钮）→ 快捷方式 → 卸载项 → 延迟删目录。
    反过来的话，图标还在 `%APPDATA%\\DeskBeauty\\boxes\\` 里，程序一删
    用户桌面就空了。
    """
    what = wipe_before_uninstall() if wipe else "skipped"
    for path in (programs_dir() / START_ITEM, programs_dir() / PANEL_ITEM,
                 startup_dir() / START_ITEM, desktop_dir() / START_ITEM):
        try:
            path.unlink()
        except OSError:
            pass
    _delete_reg_key(reg_key())
    if remove_files:
        # 自带的 TranslucentTB 要是正跑着（安装目录那份，或者 APPDATA 里的运行副本），
        # 先停掉：它注入在 explorer 里，光删文件它还会赖着，任务栏也还是透明的
        try:
            from . import taskbar as _tb
            roots = (str(install_dir()).lower(), str(_tb.runtime_dir()).lower())
            if any(str(image or "").lower().startswith(root)
                   for pid in _tb.pids() for image in (_image_of(pid),)
                   for root in roots):
                _tb.stop()
        except Exception:  # noqa: BLE001
            pass
        _delete_dir_later(install_dir())
    return what


def _delete_reg_key(path: str) -> None:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                             winreg.KEY_ALL_ACCESS)
    except OSError:
        return
    try:
        while True:
            try:
                sub = winreg.EnumKey(key, 0)
            except OSError:
                break
            _delete_reg_key(f"{path}\\{sub}")
        winreg.DeleteKey(key, "")
    except OSError:
        pass
    finally:
        key.Close()


def _delete_dir_later(path: Path) -> None:
    """进程还占着 exe/dll，只能退出去再删；ping 是拿来做延迟的。"""
    if not path.exists():
        return
    try:
        subprocess.Popen(
            ["cmd", "/c", "ping", "-n", "4", "127.0.0.1", ">nul",
             "&", "rmdir", "/s", "/q", str(path)],
            creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
            close_fds=True,
        )
    except OSError:
        pass


# ---------------------------------------------------------------- 启动
def relaunch_installed(panel: bool = False) -> bool:
    """装完以后换成安装目录里的那一份继续跑。"""
    exe = installed_app_exe()
    if not exe.is_file():
        return False
    args = f'--panel' if panel else ''
    # 等当前进程真的退出、把 QLockFile 放开再起，否则新实例会直接退出
    cmd = f'ping -n 3 127.0.0.1 >nul & start "" /d "{exe.parent}" "{exe}" {args}'
    try:
        subprocess.Popen(["cmd", "/c", cmd],
                         creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
                         close_fds=True)
        return True
    except OSError:
        return False


def open_start_menu_folder() -> None:
    """开始菜单程序文件夹（用户想自己看/删快捷方式时用）。"""
    try:
        subprocess.Popen(["explorer", str(programs_dir())], close_fds=True)
    except OSError:
        pass
