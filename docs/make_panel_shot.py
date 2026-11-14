"""生成 README 用的面板截图：只截面板窗口本身（看不到背后的桌面内容）。

用法：python docs/make_panel_shot.py <输出目录>
"""
import os
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))
os.environ.setdefault("DESKBEAUTY_HOME", str(Path(os.environ.get("TEMP", ".")) / "db-shot-home"))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from beauty.app import Controller  # noqa: E402
from beauty.config import Config  # noqa: E402
from beauty.glass import GLOBAL_QSS  # noqa: E402

out = Path(sys.argv[1] if len(sys.argv) > 1 else PROJ / "docs")
out.mkdir(parents=True, exist_ok=True)

app = QApplication(sys.argv)
app.setQuitOnLastWindowClosed(False)
app.setStyleSheet(GLOBAL_QSS)
font = QFont("Microsoft YaHei UI")
font.setPixelSize(12)
app.setFont(font)

cfg = Config.load()
ctrl = Controller(app, cfg)
ctrl.open_settings()
panel = ctrl.settings


def shoot(page: str, name: str) -> None:
    panel._show_page(page)
    app.processEvents()
    QTimer.singleShot(500, lambda: _grab(page, name))


def _grab(page: str, name: str) -> None:
    panel.grab().save(str(out / name))
    print("saved", out / name)
    QTimer.singleShot(200, next_page)


pages = [("cards", "panel.png")]
idx = {"i": 0}


def next_page() -> None:
    idx["i"] += 1
    if idx["i"] < len(pages):
        shoot(*pages[idx["i"]])
    else:
        ctrl.quit()


QTimer.singleShot(2200, lambda: shoot(*pages[0]))
QTimer.singleShot(12000, ctrl.quit)   # 保险
app.exec()
