import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 任务栏 / 开始菜单的快捷方式走这条快路：只碰 ctypes，不加载 PySide6，
# 几十毫秒就把信号转给常驻的那个进程。没在跑就当作正常启动。
if len(sys.argv) > 1 and sys.argv[1] in ("--toggle", "--panel", "--quit"):
    from beauty.signal import send  # noqa: E402

    if send(sys.argv[1][2:]):
        raise SystemExit(0)

from beauty.app import main  # noqa: E402

raise SystemExit(main())
