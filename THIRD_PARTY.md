# 第三方组件与许可

本项目自身以 **MIT** 发布（见 `LICENSE`）。它会用到/分发以下第三方东西：

## 运行依赖（源码运行、或打包进 exe）

| 组件 | 许可 | 用在哪 | 备注 |
| --- | --- | --- | --- |
| [PySide6](https://pypi.org/project/PySide6/) / Qt 6 | **LGPL-3.0**（另有商业许可） | 整个界面、窗口、动画、绘制 | 打包版里以**独立 DLL** 形式放在 `app\_internal\`，可以自行替换成别的 Qt 构建；Qt 源码见 [qt.io](https://www.qt.io/)，PySide6 源码见 [pyside/pyside-setup](https://code.qt.io/cgit/pyside/pyside-setup.git/) |
| [Python 3.12](https://www.python.org/) | PSF License | 运行时 | 打包版里是 PyInstaller 打进去的解释器 |
| [PyInstaller](https://pyinstaller.org/) | GPL-2.0 + bootloader 例外条款 | 只用于打 exe | 打包出来的产物不受 GPL 约束（例外条款允许闭源/其它许可分发） |
| [pycaw](https://github.com/AndreMiras/pycaw) | MIT | 音量/会话（可选） | 没装就退化成「窗口标题 + 媒体键」那条路 |
| [winsdk](https://pypi.org/project/winsdk/) | MIT | 系统媒体会话（可选） | 同上 |

`requirements.txt` 里只强制 PySide6；音乐卡片那两个是可选的，装了才有完整的「正在播放」信息。

## 打包版（Release 里的 zip）

zip 里带的是 PyInstaller 的 onedir 产物，Qt 的 DLL 是**独立文件**（没有静态链接、没有改源码），符合 LGPL-3.0 第 4 条「以可替换的方式使用库」的要求。要用自己的 Qt 构建替换，直接把 `app\_internal\PySide6\` 下的 DLL 换掉即可。

如果你把打包版变成自己产品的一部分对外分发，请自己再确认一遍 LGPL 的其余义务（附许可文本、保留声明等）。


## 随包分发的独立程序（GPL）

| 组件 | 许可 | 用在哪 | 备注 |
| --- | --- | --- | --- |
| [TranslucentTB](https://github.com/TranslucentTB/TranslucentTB) | **GPL-3.0** | 任务栏背景透明（`beauty/taskbar.py` 负责拉起 / 看护 / 写它的 `settings.json`） | 放在 zip 的 `translucenttb\` 目录里（安装后是 `%LOCALAPPDATA%\Programs\desktopbeauty\translucenttb\`），**未做任何修改**，与上游 Release 的二进制一致；许可全文 `LICENSE-GPL-3.0.txt`、来源说明 `NOTICE.txt` 都在那个目录里。它和本项目是两个独立程序（mere aggregation），本项目自身仍是 MIT |

不想要它：删掉 `translucenttb\` 目录即可（任务栏就不透明了，其余功能不受影响）。

## 数据 / 服务

| 服务 | 用途 | 许可/条款 |
| --- | --- | --- |
| [Open-Meteo](https://open-meteo.com/) | 天气、空气质量 | CC-BY-4.0，免费无需 key |
| [ip-api.com](http://ip-api.com/) / [ipwho.is](https://ipwho.is/) | 按 IP 猜城市（只用来给天气定位） | 免费接口，仅供非商业/低频率使用 |
| 网易云音乐公开接口 | 给没有 SMTC 的播放器补一张封面 | 非官方接口，随时可能变 |

## 字体 / 图标

- 界面字体用系统的 **Microsoft YaHei UI**；图标字体用 **Segoe MDL2 Assets**。都不随仓库分发（Windows 自带）。
- `assets/logo.png`、`assets/app.ico` 是本项目的图标，随 MIT 一起授权。
- 桌面截图里的应用图标（Steam / QQ / 微信 / B 站…）版权归各自厂商，截图只用于说明界面效果。
