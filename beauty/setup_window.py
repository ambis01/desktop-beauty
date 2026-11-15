"""第一次双击（还没装进系统）时弹的那块玻璃：一键装到这台电脑。

只有「打包版、而且不是从安装目录跑起来的」才用它——源码跑的时候这东西不出现，
开发机上的快捷方式也就不会被顶掉。

点「安装」之后：复制文件 → 开始菜单 / 开机自启 / 桌面快捷方式 → 卸载项，
然后把进程换成安装目录里的那一份继续跑。任务栏那颗 Windows 只准人点（见
install.py 顶上的说明），所以这里不假装能装。
"""
from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout)

from . import __version__, install
from .glass import GlassWindow, paint_frost
from .ui import panel_rect, u

W, H = 470, 240


def _logo_path():
    root = install.dist_root()
    for p in (root / "app" / "_internal" / "assets" / "logo.png",
              root / "assets" / "logo.png"):
        if p.is_file():
            return p
    return root / "assets" / "logo.png"


class SetupWindow(GlassWindow):
    """一块白色雾玻璃：标题 + 安装 / 直接用。"""

    done = Signal(bool, str)

    def __init__(self, radius: int = 16, upgrade: bool = False):
        flags = Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
        super().__init__(None, flags, radius=int(radius), activate=True, dark=False)
        self.upgrade = bool(upgrade)
        self.real_blur = False
        self.glass_alpha = 168
        self.tint = QColor(255, 255, 255, 40)
        self.bg_opacity = 0.0
        self.setWindowTitle("desktop beauty")
        self.setStyleSheet(_QSS)
        self._busy = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(u(20), u(16), u(20), u(18))
        outer.setSpacing(u(12))

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        logo = QLabel()
        logo.setObjectName("logo")
        pm = QPixmap(str(_logo_path()))
        if not pm.isNull():
            logo.setPixmap(pm.scaled(u(28), u(28), Qt.KeepAspectRatio,
                                     Qt.SmoothTransformation))
        logo.setFixedSize(u(28), u(28))
        header.addWidget(logo, 0, Qt.AlignVCenter)
        header.addSpacing(u(9))
        title = QLabel("desktop beauty")
        title.setObjectName("title")
        header.addWidget(title, 0, Qt.AlignVCenter)
        header.addStretch(1)
        close = QPushButton("✕")
        close.setObjectName("close")
        close.setFixedSize(u(26), u(26))
        close.clicked.connect(self.dismiss)
        header.addWidget(close, 0, Qt.AlignVCenter)
        outer.addLayout(header)

        self.status = QLabel("")
        self.status.setObjectName("status")
        self.status.setFixedHeight(u(16))
        outer.addWidget(self.status)

        self.btn_install = QPushButton("安装到这台电脑")
        self.btn_install.setObjectName("primary")
        self.btn_install.setFixedHeight(u(40))
        self.btn_install.clicked.connect(self.start_install)
        outer.addWidget(self.btn_install)

        self.btn_later = QPushButton("直接用")
        self.btn_later.setObjectName("ghost")
        self.btn_later.setFixedHeight(u(30))
        self.btn_later.clicked.connect(self.dismiss)
        outer.addWidget(self.btn_later)

        if self.upgrade:
            # 覆盖安装：这里没有「先用着」这回事——旧版本这时候还在跑
            old = install.installed_version()
            self.btn_install.setText("覆盖安装")
            self.btn_later.setText("关闭")
            if old and old != __version__:
                self.status.setText(f"已安装 {old}，将覆盖为 {__version__}")

        self.done.connect(self._finished)
        self._place()

    # ------------------------------------------------------------ 位置
    def _place(self) -> None:
        scr = (QApplication.screenAt(QCursor.pos())
               or QApplication.primaryScreen())
        area = scr.availableGeometry() if scr is not None else self.rect()
        r = panel_rect(area, (W, H))
        self.setGeometry(r.x(), r.y(), W, H)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.dismiss()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event) -> None:
        self._keep_background = True
        p = QPainter(self)
        try:
            paint_frost(self, p, False)
        except (RuntimeError, TypeError):
            pass
        finally:
            p.end()
        super().paintEvent(event)

    # ------------------------------------------------------------ 动作
    def start_install(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.btn_install.setEnabled(False)
        self.btn_install.setText("安装中…")
        self.btn_later.setEnabled(False)
        self.status.setObjectName("status")
        self.status.setText("")
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self) -> None:
        try:
            ok = install.install()
            msg = "" if ok else "dev"
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, str(exc) or exc.__class__.__name__
        self.done.emit(ok, msg)

    def _finished(self, ok: bool, msg: str) -> None:
        if not ok:
            self._busy = False
            self.btn_install.setEnabled(True)
            self.btn_install.setText("安装到这台电脑")
            self.btn_later.setEnabled(True)
            text = {"dev": "这台已经装过了"}.get(msg, msg)
            self.status.setText(text)
            self.status.setStyleSheet("color:#C0303A;")
            self.status.setVisible(True)
            return
        self.btn_install.setText("装好了")
        self.btn_later.setText("关闭")
        self.btn_later.setEnabled(True)
        QTimer.singleShot(700, self._relaunch)

    def _relaunch(self) -> None:
        install.relaunch_installed()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def dismiss(self) -> None:
        install.mark_dismissed()
        self.close()

    def closeEvent(self, event) -> None:
        if not self._busy:
            install.mark_dismissed()
        super().closeEvent(event)


_QSS = """
QLabel { color: #2A3242; font-size: 13px; }
QLabel#title { color: #141A24; font-size: 15px; font-weight: 600; }
QLabel#status { color: rgba(48, 62, 84, 200); font-size: 12px; }
QPushButton {
    background: rgba(255, 255, 255, 130); color: #2A3242;
    border: 1px solid rgba(255, 255, 255, 150); border-radius: 10px;
    font-size: 13px;
}
QPushButton:hover { background: rgba(255, 255, 255, 190); }
QPushButton:pressed { background: rgba(214, 228, 250, 215); }
QPushButton:disabled { color: rgba(60, 76, 100, 90); background: rgba(255, 255, 255, 70); }
QPushButton#primary {
    background: rgba(118, 160, 246, 205); color: #FFFFFF;
    border: 1px solid rgba(96, 140, 232, 190);
}
QPushButton#primary:hover { background: rgba(132, 172, 250, 225); }
QPushButton#primary:pressed { background: rgba(104, 148, 238, 230); }
QPushButton#primary:disabled { background: rgba(150, 176, 226, 150); color: rgba(255,255,255,190); }
QPushButton#ghost { background: rgba(255, 255, 255, 70); color: rgba(48, 64, 92, 210); border: none; }
QPushButton#ghost:hover { background: rgba(255, 255, 255, 140); }
QPushButton#ghost:disabled { color: rgba(60, 76, 100, 80); background: rgba(255, 255, 255, 40); }
QPushButton#close { background: transparent; color: rgba(40, 56, 80, 150); border: none; font-size: 14px; }
QPushButton#close:hover { background: rgba(240, 90, 90, 170); color: white; }
"""
