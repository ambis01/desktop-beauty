"""界面文案：中文 / English 两套。

用法：`from .i18n import tr` 然后 `tr("nav.cards")`；带参数用 `tr("x", n=3)`。
没登记的词条原样返回 key（写错时一眼能看出来）。

语言存在 `cfg.lang`，`set_lang()` 之后重建面板即可生效——文案是**取的时候**
才查表，所以已经建好的控件不会变，需要重建（见 settings.SettingsWindow.rebuild）。
"""
from __future__ import annotations

LANGS = (("zh", "简体中文"), ("en", "English"))

_state = {"lang": "zh"}


def lang() -> str:
    return _state["lang"]


def set_lang(code: str) -> None:
    code = str(code or "").lower()
    _state["lang"] = code if any(code == c for c, _ in LANGS) else "zh"


def lang_label(code: str) -> str:
    return next((name for c, name in LANGS if c == code), LANGS[0][1])


# key: (中文, English)
_STRINGS: dict[str, tuple[str, str]] = {
    # ---------- 面板：左边导航 ----------
    "nav.cards": ("功能卡片", "Cards"),
    "nav.boxes": ("收纳盒", "Boxes"),
    "nav.style": ("美化风格", "Style"),
    "nav.mouse": ("鼠标配置", "Mouse"),
    "nav.keys": ("快捷键", "Hotkeys"),
    "nav.settings": ("设置", "Settings"),
    "nav.lang": ("语言", "Language"),
    "nav.destroy_world": ("毁灭世界", "Doomsday"),
    "nav.quit": ("退出 desktop beauty", "Quit desktop beauty"),
    "tip.quit": ("直接退出 desktop beauty", "Quit desktop beauty right away"),
    # ---------- 功能卡片页 ----------
    "cards.head": ("功能卡片", "Feature cards"),    "cards.empty": ("还没有卡片", "No cards yet"),
    "cards.add": ("＋ 添加卡片", "+ Add card"),
    "cards.hide_fs": ("全屏应用时自动隐藏所有卡片",
                      "Hide every card while a fullscreen app is active"),
    "card.on": ("已启用", "On"),
    "card.off": ("已停用", "Off"),
    "btn.settings": ("设置", "Options"),
    "btn.enable": ("启用", "Enable"),
    "btn.disable": ("停用", "Disable"),
    "btn.delete": ("删除", "Delete"),
    "btn.save": ("保存", "Save"),
    "btn.reset": ("恢复默认", "Reset"),
    "btn.cancel": ("取消", "Cancel"),
    "btn.ok": ("确定", "OK"),
    # ---------- 美化风格页 ----------
    "style.head": ("美化风格", "Style preset"),
    "style.use": ("使用", "Use"),
    "style.using": ("使用中", "In use"),
    "style.mist": ("雾白", "Mist"),
    "style.gauze": ("薄纱", "Gauze"),
    "style.dense": ("浓雾", "Dense"),
    # ---- 自定义外观 ----
    "style.custom": ("自定义", "Custom"),
    "style.box": ("收纳盒", "Boxes"),
    "style.card": ("卡片", "Cards"),
    "style.color": ("颜色", "Colour"),
    "style.alpha": ("透明度", "Opacity"),
    "style.radius": ("圆角", "Corners"),
    "style.default_color": ("默认（白玻璃）", "Default (white glass)"),
    "style.more_color": ("更多颜色…", "More colours…"),
    "style.save_scheme": ("保存为方案", "Save as preset"),
    "style.scheme_title": ("保存美化方案", "Save style preset"),
    "style.scheme_label": ("给这套外观起个名字：", "Name this look:"),
    "style.schemes": ("已保存的方案", "Saved presets"),
    "style.schemes_empty": ("还没有保存的方案", "No saved presets yet"),
    "style.apply": ("套用", "Apply"),
    "style.delete": ("删除", "Delete"),
    "style.reset": ("恢复默认外观", "Reset to default"),
    # ---------- 鼠标页 ----------
    "mouse.head": ("鼠标", "Mouse"),
    "mouse.dot": ("桌面上用圆点指针", "Use a dot cursor on the desktop"),
    "mouse.more": ("调整颜色与透明度…", "Color and opacity…"),
    "mouse.scheme": ("当前设置（不保存）", "Current (not saved)"),
    "mouse.scheme_n": ("方案 {n}", "Preset {n}"),
    "mouse.save_title": ("保存鼠标样式", "Save cursor style"),
    "mouse.save_label": ("给这套配色起个名字：", "Name this setup:"),
    # ---------- 快捷键页 ----------
    "keys.head": ("快捷键", "Hotkeys"),
    "keys.panel": ("打开 / 关闭面板", "Open / close the panel"),
    "keys.toggle": ("启用 / 停用美化", "Enable / disable beauty"),
    "keys.taken": ("「{key}」已经被「{other}」占用了，换一个吧。",
                   "\"{key}\" is already used by \"{other}\". Pick another one."),
    # ---------- 设置页 ----------
    "settings.head": ("基础选项", "Basics"),
    "settings.theme": ("外观", "Appearance"),
    "theme.light": ("浅色", "Light"),
    "theme.dark": ("深色", "Dark"),
    "theme.auto": ("跟随系统", "System"),
    "settings.auto_align": ("自适应对齐（拖动卡片/收纳盒松手时自动避让）",
                            "Smart alignment (avoid overlaps after a drag)"),
    "settings.box_merge": ("允许收纳盒合并（把一个盒子拖到另一个上面）",
                           "Allow boxes to merge (drop one onto another)"),
    "settings.box_merge_bar": ("两个收起状态的收纳盒也能合并",
                               "Also merge two collapsed boxes"),
    # ---- 任务栏 ----
    "settings.taskbar": ("任务栏", "Taskbar"),
    "settings.taskbar_align": ("图标对齐", "Icon alignment"),
    "settings.align_left": ("靠左", "Left"),
    "settings.align_center": ("居中", "Center"),
    "settings.taskbar_clear": ("去掉任务栏黑边",
                               "Remove the taskbar border"),
    "boxes.sort": ("排序", "Sort"),
    "boxes.sort.name": ("名称", "Name"),
    "boxes.sort.size": ("图标数量", "Icon count"),
    "boxes.sort.created": ("创建时间", "Created"),
    "settings.dark": ("深色面板", "Dark panel"),
    "settings.language": ("语言 / Language", "Language"),
    "lang.head": ("界面语言", "Display language"),
    "lang.hint": ("换完立刻生效：面板会重新加载，卡片和盒子上的文字也跟着换。",
                  "Takes effect at once: the panel reloads and so do the texts on "
                  "cards and boxes."),
    # ---------- 毁灭世界 ----------
    "world.title": ("毁灭世界", "Doomsday"),
    "world.ask": ("确定要毁灭世界吗？\n\n所有收纳盒都会被销毁（里面的图标放回桌面），"
                  "所有卡片都会被删除。这个操作不能撤销。",
                  "Really tear everything down?\n\nEvery box will be destroyed "
                  "(its icons go back to the desktop) and every card will be "
                  "deleted. This cannot be undone."),
    "world.ok": ("确定毁灭", "Do it"),
    "world.done": ("世界已经毁灭了", "Everything is gone"),
    "world.nothing": ("没什么可毁灭的", "Nothing left to destroy"),
    # ---------- 卸载 ----------
    "settings.uninstall": ("卸载 desktop beauty", "Uninstall desktop beauty"),
    "uninstall.title": ("卸载 desktop beauty", "Uninstall desktop beauty"),
    "uninstall.ask": ("卸载之后这个程序就没有了。\n\n先把每个收纳盒里的图标放回桌面"
                      "（和「毁灭世界」一样），再删掉快捷方式、开机自启和程序文件。"
                      "%APPDATA%\\DeskBeauty 里的设置会保留。",
                      "The app will be removed.\n\nEvery box's icons go back to the "
                      "desktop first (just like Doomsday), then shortcuts, autostart "
                      "and the program files are deleted. Settings in "
                      "%APPDATA%\\DeskBeauty are kept."),
    "uninstall.ok": ("确定卸载", "Uninstall"),
    # ---------- 收纳盒 ----------
    "box.pin": ("展开收纳盒", "Keep expanded"),
    "box.rename": ("重命名", "Rename"),
    "box.destroy": ("销毁收纳盒", "Destroy this box"),
    "box.destroy_tip": ("图标全部放回桌面（摆在盒子附近），然后删掉这个盒子",
                        "Put every icon back on the desktop near the box, then "
                        "delete the box"),
    "box.destroyed": ("收纳盒已销毁，{n} 个图标已放回桌面",
                      "Box destroyed, {n} icons back on the desktop"),
    "item.open": ("打开", "Open"),
    "item.reveal": ("打开文件所在位置", "Show in folder"),
    "item.rename": ("重命名", "Rename"),
    "item.back": ("移回桌面", "Move back to desktop"),
    "item.trash": ("删除到回收站", "Delete to Recycle Bin"),
    "item.rename_title": ("重命名", "Rename"),
    "item.rename_label": ("新名字：", "New name:"),
    # ---------- 面板里的收纳盒页 ----------
    "boxes.head": ("收纳盒", "Boxes"),
    "boxes.empty": ("还没有收纳盒", "No boxes yet"),
    "boxes.none": ("没找到", "Nothing found"),
    "boxes.count": ("{n} 个", "{n}"),
    "boxes.state.expanded": ("展开", "Open"),
    "boxes.state.mini": ("收起", "Closed"),
    "boxes.state.bar": ("横杠", "Bar"),
    "boxes.locate": ("定位", "Show"),
    "boxes.rename": ("改名", "Rename"),
    "boxes.name_title": ("重命名收纳盒", "Rename box"),
    "boxes.name_label": ("新名字：", "New name:"),
    "boxes.undo": ("撤销上次建盒", "Undo last box"),
    "boxes.undo_n": ("撤销上次建盒（{n}）", "Undo last box ({n})"),
    "boxes.search": ("搜索盒子里的图标", "Search inside boxes"),
    "box.sort_name": ("按名称排序", "Sort by name"),
    "box.size": ("大小", "Size"),
    "box.size.auto": ("自动", "Auto"),
    "box.size.2x2": ("2×2", "2×2"),
    "box.size.3x3": ("3×3", "3×3"),
    "box.size.4x4": ("4×4", "4×4"),
    "box.size.5x5": ("5×5", "5×5"),
    "box.size.4x2": ("4×2 横条", "4×2 wide"),
    "box.size.2x4": ("2×4 竖条", "2×4 tall"),
    # ---------- 待办提醒 ----------
    "todo.remind_due": ("今天到期：{names}", "Due today: {names}"),
    "todo.remind_late": ("已逾期：{names}", "Overdue: {names}"),
    # ---------- 卡片种类 ----------
    "kind.clock": ("时钟卡片", "Clock"),
    "kind.todo": ("待办 / 日程", "To-do / agenda"),
    "kind.calendar": ("日历（月视图）", "Calendar (month)"),
    "kind.countdown": ("倒计时 / 纪念日", "Countdown / anniversary"),
    "kind.music": ("音乐播放器", "Music player"),
    "kind.soon": ("{name}（即将支持）", "{name} (coming soon)"),
}


def tr(key: str, **kwargs) -> str:
    pair = _STRINGS.get(key)
    if pair is None:
        return key
    text = pair[0] if _state["lang"] == "zh" else pair[1]
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text
