"""设置面板：左侧一栏模块，右边是对应页面。

模块：卡片 / 鼠标配置 / 快捷键 / 退出（点一下直接退）。

以前所有控件堆在一列里，加模块就只能往下接，越滚越长；现在左边是导航，
右边是页面，加功能只要再塞一个页。背景还是自绘的雾玻璃（paint_frost），
跟取色盘、下拉菜单同一套。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QStackedWidget, QVBoxLayout, QWidget,
)

from .controls import ColorSwatches, GlassCheck, GlassCombo, ThemePicker
from .glass import GlassMenu, GlassWindow, paint_frost, round_corners
from .hotkey import (
    DEFAULT_PANEL, DEFAULT_TOGGLE, hotkey_text, normalize,
)
from .i18n import LANGS, lang_label, tr
from .picker import ColorPicker, GlassInput, KeyCapture
from .ui import PANEL_SIZE, panel_rect, scale_qss, u

BOX_ALPHAS = (("清透", 190), ("标准", 212), ("厚一点", 240))
BOX_RADII = (("小 10", 10), ("中 14", 14), ("大 20", 20))
DEFAULT_CURSOR_COLOR = "#2e3238"
DEFAULT_CURSOR_ALPHA = 100

PAGES = ("cards", "boxes", "style", "mouse", "keys", "settings", "lang")

# 面板标题左边的徽标（就是程序图标那张，assets/logo.png 是它的 1024 版）
LOGO_PATH = Path(__file__).resolve().parent.parent / "assets" / "logo.png"


def _logo_pixmap(size: int) -> QPixmap:
    """按实际像素密度取一张清晰的徽标；没有图就返回空 pixmap（标题照常显示）。"""
    src = QPixmap(str(LOGO_PATH)) if LOGO_PATH.exists() else QPixmap()
    if src.isNull():
        return QPixmap()
    scr = QApplication.primaryScreen()
    dpr = float(scr.devicePixelRatio()) if scr is not None else 1.0
    dpr = dpr if dpr > 0 else 1.0
    px = max(1, int(round(size * dpr)))
    pm = src.scaled(px, px, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    pm.setDevicePixelRatio(dpr)
    return pm


def _divider() -> QFrame:
    line = QFrame()
    line.setObjectName("line")
    line.setFixedHeight(1)
    return line


def _clear_layout(layout) -> None:
    """清空一个纵向列表：先摘下父窗口再 deleteLater。

    只 deleteLater 的话，这一轮重画里那些旧行还在（它们仍然挂在 holder 上），
    会叠在新列表后面露出一截，看着像重影。
    """
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()


def _head(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("head")
    return label


class SettingsWindow(GlassWindow):
    """非模态玻璃面板。所有改动立刻生效并落盘。"""

    def __init__(self, controller):
        flags = Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
        super().__init__(None, flags,
                         radius=int(getattr(controller.cfg, "box_radius", 10) or 10),
                         activate=True, dark=False)
        self.ctrl = controller
        self.native_backdrop = False
        self._backdrop = None
        self.real_blur = False
        self.glass_alpha = 130
        self.tint = QColor(255, 255, 255, 40)
        self.bg_opacity = 0.0
        self.setWindowTitle("desktop beauty")
        self.resize(*PANEL_SIZE)
        self.center_on_screen()
        self._dark = bool(controller.cfg.dark())
        self.setStyleSheet(_QSS_DARK if self._dark else _QSS)
        self._building = False
        self._pending_page = "cards"

        outer = QVBoxLayout(self)
        outer.setContentsMargins(u(18), u(14), u(18), u(16))
        outer.setSpacing(u(10))

        # ---- 标题栏 ----
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        logo = QLabel()
        logo.setObjectName("logo")
        pm = _logo_pixmap(u(30))
        if not pm.isNull():
            logo.setPixmap(pm)
        logo.setFixedSize(u(30), u(30))
        header.addWidget(logo, 0, Qt.AlignVCenter)
        header.addSpacing(u(9))
        title = QLabel("desktop beauty")
        title.setObjectName("title")
        header.addWidget(title, 0, Qt.AlignVCenter)
        header.addStretch(1)
        self.btn_close = QPushButton("✕")
        self.btn_close.setObjectName("close")
        self.btn_close.setFixedSize(u(26), u(26))
        self.btn_close.clicked.connect(self.close)
        header.addWidget(self.btn_close, 0, Qt.AlignVCenter)
        outer.addLayout(header)

        # ---- 左边导航 + 右边页面 ----
        body = QHBoxLayout()
        body.setSpacing(u(14))
        body.addWidget(self._build_sidebar())
        self.stack = QStackedWidget()
        self.stack.setObjectName("stack")
        self.stack.addWidget(self._build_cards_page())
        self.stack.addWidget(self._build_boxes_page())
        self.stack.addWidget(self._build_style_page())
        self.stack.addWidget(self._build_mouse_page())
        self.stack.addWidget(self._build_keys_page())
        self.stack.addWidget(self._build_settings_page())
        self.stack.addWidget(self._build_lang_page())
        body.addWidget(self.stack, 1)
        outer.addLayout(body, 1)

        self._show_page("cards")
        self.refresh()
        self.sync_state()
        self._sync_cursor()
        self._sync_hotkeys()
        self._apply_style()

    # ------------------------------------------------------------ 左边导航
    def _build_sidebar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("sidebar")
        bar.setFixedWidth(u(146))
        col = QVBoxLayout(bar)
        col.setContentsMargins(u(8), u(8), u(8), u(8))
        col.setSpacing(u(4))
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        self._nav_buttons = {}
        for key, label_key in (("cards", "nav.cards"),
                               ("boxes", "nav.boxes"),
                               ("style", "nav.style"),
                               ("mouse", "nav.mouse"),
                               ("keys", "nav.keys"),
                               ("settings", "nav.settings"),
                               ("lang", "nav.lang")):
            btn = QPushButton(tr(label_key))
            btn.setObjectName("nav")
            btn.setCheckable(True)
            btn.setMinimumHeight(u(34))
            btn.clicked.connect(lambda _=False, k=key: self._show_page(k))
            self._nav_group.addButton(btn)
            self._nav_buttons[key] = btn
            col.addWidget(btn)
        col.addStretch(1)
        # 毁灭世界：把所有收纳盒和卡片一次性清掉（要二次确认）
        self.btn_world = QPushButton(tr("nav.destroy_world"))
        self.btn_world.setObjectName("navquit")
        self.btn_world.setMinimumHeight(u(34))
        self.btn_world.clicked.connect(self._destroy_world)
        col.addWidget(self.btn_world)
        self.btn_quit = QPushButton(tr("nav.quit"))
        self.btn_quit.setObjectName("navquit")
        self.btn_quit.setToolTip(tr("tip.quit"))
        self.btn_quit.setMinimumHeight(u(34))
        self.btn_quit.clicked.connect(self.ctrl.quit)
        col.addWidget(self.btn_quit)
        return bar

    def _show_page(self, key: str) -> None:
        if key not in PAGES:
            return
        self._pending_page = key
        self.stack.setCurrentIndex(PAGES.index(key))
        if key == "boxes":
            self._rebuild_boxes()
        btn = self._nav_buttons.get(key)
        if btn is not None and not btn.isChecked():
            btn.setChecked(True)

    # ------------------------------------------------------------ 卡片页面
    def _build_cards_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(u(8))
        col.addWidget(_head(tr("cards.head")))
        empty = QLabel(tr("cards.empty"))
        empty.setObjectName("empty")
        self.lbl_cards_empty = empty
        col.addWidget(empty)

        scroll = QScrollArea()
        scroll.setObjectName("cards")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)
        holder = QWidget()
        holder.setObjectName("cardholder")
        holder.setAutoFillBackground(False)
        self._cards_box = QVBoxLayout(holder)
        self._cards_box.setContentsMargins(0, 0, 0, 0)
        self._cards_box.setSpacing(u(6))
        scroll.setWidget(holder)
        col.addWidget(scroll, 1)

        self.btn_add_card = QPushButton(tr("cards.add"))
        self.btn_add_card.clicked.connect(self._add_card_menu)
        col.addWidget(self.btn_add_card)

        self.chk_fullscreen = GlassCheck(tr("cards.hide_fs"), dark=self._dark)
        self.chk_fullscreen.toggled.connect(self._on_hide_fullscreen)
        col.addWidget(self.chk_fullscreen)
        return page

    def _card_row(self, card) -> QWidget:
        row = QFrame()
        row.setObjectName("cardrow")
        h = QHBoxLayout(row)
        h.setContentsMargins(u(12), u(8), u(8), u(8))
        h.setSpacing(u(8))
        name = QLabel(self.ctrl.card_label(card))
        name.setObjectName("cardname")
        h.addWidget(name, 1)
        state = QLabel(tr("card.on") if card.enabled else tr("card.off"))
        state.setObjectName("state" if card.enabled else "stateoff")
        h.addWidget(state)
        if card.type in ("clock", "todo", "calendar", "countdown"):
            # 每种卡片都有自己的设置（开关 / 显示项）
            cfg_btn = QPushButton(tr("btn.settings"))
            cfg_btn.setObjectName("ghost")
            cfg_btn.setMinimumWidth(u(56))
            cfg_btn.clicked.connect(
                lambda _=False, cid=card.id: self._open_card_settings(cid))
            h.addWidget(cfg_btn)
        toggle = QPushButton(tr("btn.disable") if card.enabled else tr("btn.enable"))
        toggle.setMinimumWidth(u(56))
        toggle.clicked.connect(
            lambda _=False, cid=card.id, on=not card.enabled: self._toggle_card(cid, on))
        h.addWidget(toggle)
        dele = QPushButton(tr("btn.delete"))
        dele.setObjectName("danger")
        dele.setMinimumWidth(u(52))
        dele.clicked.connect(lambda _=False, cid=card.id: self._del_card(cid))
        h.addWidget(dele)
        return row

    def _rebuild_cards(self) -> None:
        _clear_layout(self._cards_box)
        cards = list(self.ctrl.cfg.cards)
        self.lbl_cards_empty.setVisible(not cards)
        for card in cards:
            self._cards_box.addWidget(self._card_row(card))
        self._cards_box.addStretch(1)

    def refresh_cards(self) -> None:
        """外面（卡片自己的菜单里点了「关闭卡片」）改了卡片状态：列表重画一遍。"""
        self._rebuild_cards()

    # ------------------------------------------------------------ 收纳盒页面
    def _build_boxes_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(u(8))
        col.addWidget(_head(tr("boxes.head")))

        self.edit_search = QLineEdit()
        self.edit_search.setObjectName("search")
        self.edit_search.setPlaceholderText(tr("boxes.search"))
        self.edit_search.setClearButtonEnabled(True)
        self.edit_search.textChanged.connect(self._on_box_search)
        col.addWidget(self.edit_search)

        empty = QLabel(tr("boxes.empty"))
        empty.setObjectName("empty")
        self.lbl_boxes_empty = empty
        col.addWidget(empty)

        scroll = QScrollArea()
        scroll.setObjectName("cards")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)
        holder = QWidget()
        holder.setObjectName("cardholder")
        holder.setAutoFillBackground(False)
        self._boxes_box = QVBoxLayout(holder)
        self._boxes_box.setContentsMargins(0, 0, 0, 0)
        self._boxes_box.setSpacing(u(6))
        scroll.setWidget(holder)
        col.addWidget(scroll, 1)

        self.btn_undo = QPushButton(tr("boxes.undo"))
        self.btn_undo.clicked.connect(self._undo_box)
        col.addWidget(self.btn_undo)
        return page

    def _box_row(self, data) -> QWidget:
        row = QFrame()
        row.setObjectName("cardrow")
        h = QHBoxLayout(row)
        h.setContentsMargins(u(12), u(8), u(8), u(8))
        h.setSpacing(u(8))
        name = QLabel(str(getattr(data, "name", "") or ""))
        name.setObjectName("cardname")
        h.addWidget(name, 1)
        count = QLabel(tr("boxes.count", n=len(self._box_items(data))))
        count.setObjectName("stateoff")
        h.addWidget(count)
        size_key = str(getattr(data, "size", "auto") or "auto")
        if size_key != "auto":
            size = QLabel(size_key.replace("x", "×"))
            size.setObjectName("stateoff")
            h.addWidget(size)
        state = QLabel(self._box_state_text(data))
        state.setObjectName("state" if self._box_expanded(data) else "stateoff")
        h.addWidget(state)

        locate = QPushButton(tr("boxes.locate"))
        locate.setObjectName("ghost")
        locate.setMinimumWidth(u(56))
        locate.clicked.connect(
            lambda _=False, bid=str(data.id): self._locate_box(bid))
        h.addWidget(locate)

        rename = QPushButton(tr("boxes.rename"))
        rename.setObjectName("ghost")
        rename.setMinimumWidth(u(56))
        rename.clicked.connect(
            lambda _=False, bid=str(data.id): self._rename_box(bid))
        h.addWidget(rename)

        destroy = QPushButton(tr("btn.delete"))
        destroy.setObjectName("danger")
        destroy.setMinimumWidth(u(52))
        destroy.clicked.connect(
            lambda _=False, bid=str(data.id): self._destroy_box(bid))
        h.addWidget(destroy)
        return row

    def _result_row(self, box_id: str, item_name: str, box_name: str) -> QWidget:
        """搜索命中的一行：图标名 + 它在哪个盒子 + 定位。"""
        row = QFrame()
        row.setObjectName("cardrow")
        h = QHBoxLayout(row)
        h.setContentsMargins(u(12), u(8), u(8), u(8))
        h.setSpacing(u(8))
        name = QLabel(item_name)
        name.setObjectName("cardname")
        h.addWidget(name, 1)
        where = QLabel(box_name)
        where.setObjectName("stateoff")
        h.addWidget(where)
        locate = QPushButton(tr("boxes.locate"))
        locate.setObjectName("ghost")
        locate.setMinimumWidth(u(56))
        locate.clicked.connect(lambda _=False, bid=box_id: self._locate_box(bid))
        h.addWidget(locate)
        return row

    def _box_items(self, data) -> list:
        try:
            return self.ctrl.store.items(data)
        except (OSError, AttributeError, TypeError):
            return []

    def _box_expanded(self, data) -> bool:
        win = self.ctrl.windows.get(str(data.id))
        try:
            return bool(win is not None and win.is_expanded)
        except (RuntimeError, AttributeError):
            return False

    def _box_state_text(self, data) -> str:
        if self._box_expanded(data):
            return tr("boxes.state.expanded")
        if bool(getattr(data, "bar", False)):
            return tr("boxes.state.bar")
        return tr("boxes.state.mini")

    def _on_box_search(self, _text: str) -> None:
        self._rebuild_boxes()

    def _rebuild_boxes(self) -> None:
        holder = getattr(self, "_boxes_box", None)
        if holder is None:
            return
        _clear_layout(holder)
        query = ""
        edit = getattr(self, "edit_search", None)
        if edit is not None:
            query = edit.text().strip().casefold()
        shown = 0
        boxes = list(self.ctrl.store.boxes)
        if query:
            for data in boxes:
                for path in self._box_items(data):
                    if query in path.name.casefold():
                        holder.addWidget(self._result_row(
                            str(data.id), path.name, str(data.name)))
                        shown += 1
        else:
            for data in boxes:
                holder.addWidget(self._box_row(data))
                shown += 1
        holder.addStretch(1)
        empty = getattr(self, "lbl_boxes_empty", None)
        if empty is not None:
            empty.setText(tr("boxes.empty") if not query else tr("boxes.none"))
            empty.setVisible(shown == 0)
        btn = getattr(self, "btn_undo", None)
        if btn is not None:
            depth = 0
            try:
                depth = int(self.ctrl.undo_depth())
            except (AttributeError, TypeError, RuntimeError):
                depth = 0
            btn.setEnabled(depth > 0)
            btn.setText(tr("boxes.undo_n", n=depth) if depth else tr("boxes.undo"))

    def _locate_box(self, box_id: str) -> None:
        """展开那个盒子并让开：面板一直置顶，不关掉就白定位。"""
        win = self.ctrl.windows.get(box_id)
        if win is None:
            self._rebuild_boxes()
            return
        try:
            if not win.is_expanded:
                win.toggle(True)
            win.raise_()
        except (RuntimeError, AttributeError):
            pass
        self.close()

    def _rename_box(self, box_id: str) -> None:
        data = self.ctrl.store.get(box_id)
        if data is None:
            return
        name = GlassInput.prompt(tr("boxes.name_title"), tr("boxes.name_label"),
                                 str(getattr(data, "name", "")), self._dark, self)
        if not name:
            return
        self.ctrl.rename_box(box_id, name)

    def _destroy_box(self, box_id: str) -> None:
        win = self.ctrl.windows.get(box_id)
        if win is not None:
            try:
                win.destroy_box()
            except (RuntimeError, AttributeError):
                pass
        self._rebuild_boxes()

    def _undo_box(self) -> None:
        self.ctrl.undo_last()
        self._rebuild_boxes()

    # ------------------------------------------------------------ 美化风格页
    def _build_style_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(u(8))
        col.addWidget(_head(tr("style.head")))
        scroll = QScrollArea()
        scroll.setObjectName("cards")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)
        holder = QWidget()
        holder.setObjectName("cardholder")
        holder.setAutoFillBackground(False)
        self._styles_box = QVBoxLayout(holder)
        self._styles_box.setContentsMargins(0, 0, 0, 0)
        self._styles_box.setSpacing(u(6))
        scroll.setWidget(holder)
        col.addWidget(scroll, 1)
        self._rebuild_styles()
        return page

    def _rebuild_styles(self) -> None:
        box = getattr(self, "_styles_box", None)
        if box is None:
            return
        _clear_layout(box)
        from . import style_presets
        active = str(getattr(self.ctrl.cfg, "style_preset", "") or style_presets.DEFAULT)
        for preset in style_presets.PRESETS:
            row = QFrame()
            row.setObjectName("cardrow")
            h = QHBoxLayout(row)
            h.setContentsMargins(u(12), u(8), u(8), u(8))
            h.setSpacing(u(8))
            name = QLabel(tr("style." + preset["id"]))
            name.setObjectName("cardname")
            h.addWidget(name, 1)
            using = preset["id"] == active
            btn = QPushButton(tr("style.using") if using else tr("style.use"))
            btn.setEnabled(not using)
            btn.setMinimumWidth(u(72))
            btn.clicked.connect(
                lambda _=False, pid=preset["id"]: self._use_style(pid))
            h.addWidget(btn)
            box.addWidget(row)
        box.addStretch(1)

    def _use_style(self, preset_id: str) -> None:
        self.ctrl.apply_style_preset(preset_id)
        self.radius = float(getattr(self.ctrl.cfg, "box_radius", 10) or 10)
        self.update()
        self._rebuild_styles()

    def _toggle_card(self, card_id: str, on: bool) -> None:
        self.ctrl.set_card_enabled(card_id, on)
        self._rebuild_cards()

    def _del_card(self, card_id: str) -> None:
        self.ctrl.remove_card(card_id)
        self._rebuild_cards()

    def _open_card_settings(self, card_id: str) -> None:
        card = next((c for c in self.ctrl.cfg.cards if c.id == card_id), None)
        if card is None:
            return
        from . import card_dialog
        on_change = lambda key, value: self.ctrl.set_card_option(card_id, key, value)
        dialogs = {
            "clock": card_dialog.ClockSettingsDialog,
            "todo": card_dialog.TodoSettingsDialog,
            "calendar": card_dialog.CalendarSettingsDialog,
            "countdown": card_dialog.CountdownSettingsDialog,
        }
        maker = dialogs.get(str(card.type))
        if maker is None:
            return
        extra = {}
        if card.type in ("todo", "countdown"):
            extra["agenda"] = self.ctrl.agenda
        maker.edit(card, on_change, self._dark, self, **extra)

    def _add_card_menu(self) -> None:
        menu = GlassMenu(self, light=not self._dark)
        menu.prepare_backdrop()
        acts = []
        ready = set(getattr(self.ctrl, "CARD_READY", ("clock",)))
        kinds = getattr(self.ctrl, "CARD_KINDS", {"clock": tr("kind.clock")})
        for kind, label in kinds.items():
            text = label if kind in ready else tr("kind.soon", name=label)
            act = menu.addAction(text)
            act.setEnabled(kind in ready)
            acts.append((act, kind))
        chosen = menu.exec(self.btn_add_card.mapToGlobal(
            self.btn_add_card.rect().bottomLeft()))
        for act, kind in acts:
            if chosen is act and kind in ready:
                try:
                    self.ctrl.add_card(kind)
                finally:
                    # 不管加卡片过程中出了什么岔子，列表都要按配置重画一遍
                    self._rebuild_cards()
                return

    # ------------------------------------------------------------ 鼠标页面
    def _build_mouse_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(u(8))
        col.addWidget(_head(tr("mouse.head")))
        self.chk_cursor = GlassCheck(tr("mouse.dot"), dark=self._dark)
        self.chk_cursor.toggled.connect(self._on_cursor_dot)
        col.addWidget(self.chk_cursor)

        self.swatches = ColorSwatches()
        self.swatches.picked.connect(self._on_color_picked)
        col.addWidget(self.swatches)

        self.btn_more_color = QPushButton(tr("mouse.more"))
        self.btn_more_color.setObjectName("ghost")
        self.btn_more_color.clicked.connect(self._pick_cursor_color)
        col.addWidget(self.btn_more_color)

        scheme_row = QHBoxLayout()
        scheme_row.setSpacing(8)
        self.cmb_scheme = GlassCombo(light=not self._dark)
        self.cmb_scheme.changed.connect(self._on_scheme)
        scheme_row.addWidget(self.cmb_scheme, 1)
        self.btn_scheme_save = QPushButton(tr("btn.save"))
        self.btn_scheme_save.clicked.connect(self._save_scheme)
        scheme_row.addWidget(self.btn_scheme_save)
        self.btn_scheme_del = QPushButton(tr("btn.delete"))
        self.btn_scheme_del.clicked.connect(self._del_scheme)
        scheme_row.addWidget(self.btn_scheme_del)
        col.addLayout(scheme_row)

        self.btn_cursor_reset = QPushButton(tr("btn.reset"))
        self.btn_cursor_reset.clicked.connect(self._reset_cursor_style)
        col.addWidget(self.btn_cursor_reset)
        col.addStretch(1)
        return page

    # ------------------------------------------------------------ 快捷键页面
    def _build_keys_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(u(8))
        col.addWidget(_head(tr("keys.head")))

        self.btn_hk_panel = QPushButton("")
        self.btn_hk_panel.setObjectName("keybtn")
        self.btn_hk_panel.clicked.connect(lambda: self._capture_hotkey("panel"))
        col.addLayout(self._key_row(tr("keys.panel"), self.btn_hk_panel))

        self.btn_hk_toggle = QPushButton("")
        self.btn_hk_toggle.setObjectName("keybtn")
        self.btn_hk_toggle.clicked.connect(lambda: self._capture_hotkey("toggle"))
        col.addLayout(self._key_row(tr("keys.toggle"), self.btn_hk_toggle))

        self.lbl_hk_err = QLabel("")
        self.lbl_hk_err.setObjectName("err")
        self.lbl_hk_err.setWordWrap(True)
        self.lbl_hk_err.setVisible(False)
        col.addWidget(self.lbl_hk_err)

        col.addSpacing(u(4))
        self.btn_hk_reset = QPushButton(tr("btn.reset"))
        self.btn_hk_reset.clicked.connect(self._reset_hotkeys)
        col.addWidget(self.btn_hk_reset)
        col.addStretch(1)
        return page

    # ------------------------------------------------------------ 设置页面
    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(u(8))
        col.addWidget(_head(tr("settings.head")))

        # 外观：浅色 / 深色 / 跟随系统（DSH 那种三张卡片），放最上面
        self.picker = ThemePicker(
            [("light", tr("theme.light")), ("dark", tr("theme.dark")),
             ("auto", tr("theme.auto"))],
            current=str(getattr(self.ctrl.cfg, "panel_theme", "light")),
            dark=self._dark)
        self.picker.changed.connect(self._on_theme)
        col.addWidget(self._row_label(tr("settings.theme")))
        col.addWidget(self.picker)

        self.chk_auto_align = GlassCheck(tr("settings.auto_align"), dark=self._dark)
        self.chk_auto_align.toggled.connect(self._on_auto_align)
        col.addWidget(self.chk_auto_align)

        self.chk_box_merge = GlassCheck(tr("settings.box_merge"), dark=self._dark)
        self.chk_box_merge.toggled.connect(self._on_box_merge)
        col.addWidget(self.chk_box_merge)

        # 卸载：这一页最下面，隔一条线跟上面的开关分开（也离得远一点，免得手滑）
        col.addStretch(1)
        line = QFrame()
        line.setObjectName("line")
        line.setFixedHeight(1)
        col.addWidget(line)
        self.btn_uninstall = QPushButton(tr("settings.uninstall"))
        self.btn_uninstall.setObjectName("uninstall")
        self.btn_uninstall.setFixedHeight(u(30))
        self.btn_uninstall.setMaximumWidth(u(240))
        self.btn_uninstall.clicked.connect(self._uninstall)
        col.addWidget(self.btn_uninstall, 0, Qt.AlignLeft)
        return page

    # ------------------------------------------------------------ 语言页面
    def _build_lang_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(u(8))
        col.addWidget(_head(tr("lang.head")))
        self.cmb_lang = GlassCombo(light=not self._dark)
        self.cmb_lang.changed.connect(self._on_lang)
        col.addWidget(self.cmb_lang)
        col.addStretch(1)
        return page

    def _row_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("tag")
        return lbl

    def _on_auto_align(self, on: bool) -> None:
        if self._building:
            return
        self.ctrl.set_auto_align(bool(on))

    def _on_box_merge(self, on: bool) -> None:
        if self._building:
            return
        self.ctrl.set_box_merge(bool(on))

    def _on_panel_dark(self, on: bool) -> None:
        if self._building:
            return
        self._dark = bool(on)
        try:
            self.ctrl.set_panel_dark(self._dark)
        except AttributeError:
            pass
        self._apply_style()

    def _on_theme(self, code) -> None:
        """浅色 / 深色 / 跟随系统：存下来，面板当场换皮。"""
        if self._building or not code:
            return
        try:
            self.ctrl.set_panel_theme(str(code))
        except AttributeError:
            return
        self._dark = bool(self.ctrl.cfg.dark())
        self._apply_style()

    def _on_lang(self, code) -> None:
        """换语言：所有文案都是建窗口时取的，只能把面板重建一遍。"""
        if self._building or not code:
            return
        code = str(code)
        if code == self.ctrl.cfg.lang:
            return
        self.ctrl.set_lang(code)
        ctrl = self.ctrl
        QTimer.singleShot(0, lambda: self._rebuild_panel(ctrl))

    def _rebuild_panel(self, ctrl) -> None:
        try:
            ctrl.settings = None      # 让 open_settings 造一个新的（按新语言取字）
            self.close()
            self.deleteLater()
            ctrl.open_settings()
        except (RuntimeError, TypeError, AttributeError):
            pass

    def _destroy_world(self) -> None:
        """毁灭世界：先问一句，确定后销毁所有收纳盒 + 删掉所有卡片。"""
        from .picker import GlassConfirm
        ok = GlassConfirm.ask(tr("world.title"), tr("world.ask"),
                              tr("world.ok"), self._dark, self)
        if not ok:
            return
        n = self.ctrl.destroy_world()
        self._rebuild_cards()

    def _uninstall(self) -> None:
        """卸载 desktop beauty：问一句，确定后放回图标、删快捷方式和程序文件。"""
        from .picker import GlassConfirm
        ok = GlassConfirm.ask(tr("uninstall.title"), tr("uninstall.ask"),
                              tr("uninstall.ok"), self._dark, self)
        if not ok:
            return
        self.ctrl.uninstall_app()

    def _key_row(self, text: str, btn) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(u(10))
        lbl = QLabel(text)
        lbl.setObjectName("tag")
        lbl.setFixedWidth(u(120))
        row.addWidget(lbl)
        # 面板变宽之后，组合键按钮要是跟着拉满会横一整条，看着像根白条
        btn.setMinimumWidth(u(230))
        btn.setMaximumWidth(u(360))
        row.addWidget(btn)
        row.addStretch(1)
        return row

    def _sync_hotkeys(self) -> None:
        cfg = self.ctrl.cfg
        panel = normalize(cfg.hotkey_panel, DEFAULT_PANEL)
        toggle = normalize(cfg.hotkey_toggle, DEFAULT_TOGGLE)
        self.btn_hk_panel.setText(panel["text"])
        self.btn_hk_toggle.setText(toggle["text"])

    def _capture_hotkey(self, which: str) -> None:
        cfg = self.ctrl.cfg
        btns = {"panel": self.btn_hk_panel, "toggle": self.btn_hk_toggle}
        names = {"panel": tr("keys.panel"), "toggle": tr("keys.toggle")}
        other = (cfg.hotkey_toggle if which == "panel" else cfg.hotkey_panel)
        other = normalize(other, DEFAULT_TOGGLE if which == "panel" else DEFAULT_PANEL)
        hk = getattr(self.ctrl, "hotkeys", None)
        # 捕获期间必须把全局快捷键摘掉：按到旧组合键会当场把面板关掉，
        # 看起来就像「一按设置面板就没了」
        got = None
        if hk is not None:
            hk.release()
        try:
            got = KeyCapture.capture(self._dark, self)
        finally:
            if got is None:
                self.ctrl.apply_hotkeys()
        if got is None:
            return
        mods, vk = got
        text = hotkey_text(mods, vk)
        if (mods, vk) == (other["mods"], other["vk"]):
            self.ctrl.apply_hotkeys()
            other_name = tr("keys.toggle") if which == "panel" else tr("keys.panel")
            self.lbl_hk_err.setText(tr("keys.taken", key=text, other=other_name))
            self.lbl_hk_err.setVisible(True)
            return
        self.lbl_hk_err.setVisible(False)
        self.ctrl.set_hotkey(which, mods, vk)
        self._sync_hotkeys()
        btns[which].setText(text)
        self.btn_hk_panel.setToolTip(names[which])

    def _reset_hotkeys(self) -> None:
        self.ctrl.reset_hotkeys()
        self.lbl_hk_err.setVisible(False)
        self._sync_hotkeys()

    # ------------------------------------------------------------ 背景
    def grab_backdrop(self) -> None:
        self._backdrop = None

    def paintEvent(self, event) -> None:
        self._keep_background = True
        p = QPainter(self)
        try:
            paint_frost(self, p, self._dark)
        except (RuntimeError, TypeError):
            pass
        finally:
            p.end()
        super().paintEvent(event)

    def _apply_style(self) -> None:
        self.setStyleSheet(_QSS_DARK if self._dark else _QSS)
        for w in (self.chk_cursor, self.chk_fullscreen, self.chk_auto_align,
                  self.chk_box_merge):
            w.set_dark(self._dark)
        self.picker.set_dark(self._dark)
        self.cmb_scheme.set_light(not self._dark)
        self.cmb_lang.set_light(not self._dark)
        self.update()

    def _toggle_theme(self) -> None:
        self._dark = not self._dark
        try:
            self.ctrl.set_panel_dark(self._dark)
        except AttributeError:
            pass
        self._apply_style()

    def center_on_screen(self) -> None:
        """居中到**鼠标所在那块屏**，屏幕放不下时先缩小（`ui.panel_rect`）。

        以前固定放在主屏正中且从不缩小：逻辑屏幕比面板小的机器（1080p@150%、
        1440p@200%、1366×768）会被裁掉一截，而且副屏上叫出来也跑到主屏去。
        """
        scr = (QApplication.screenAt(QCursor.pos())
               or QApplication.primaryScreen())
        if scr is None:
            return
        r = panel_rect(scr.availableGeometry())
        self.resize(r.width(), r.height())
        self.move(r.topLeft())

    def prepare_intro(self) -> None:
        try:
            from PySide6.QtCore import QRect
            self.center_on_screen()
            home = QRect(self.geometry())
            self._home_geo = home
            cx, cy = home.center().x(), home.center().y()
            w0, h0 = int(home.width() * 0.80), int(home.height() * 0.80)
            self.setGeometry(cx - w0 // 2, cy - h0 // 2, w0, h0)
        except (RuntimeError, TypeError):
            pass

    def play_intro(self) -> None:
        try:
            from PySide6.QtCore import QEasingCurve, QPropertyAnimation
            home = getattr(self, "_home_geo", None) or self.geometry()
            anim = QPropertyAnimation(self, b"geometry", self)
            anim.setDuration(240)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            anim.setStartValue(self.geometry())
            anim.setEndValue(home)
            anim.finished.connect(lambda: self.setGeometry(home))
            self._intro = anim
            anim.start()
        except (RuntimeError, TypeError):
            pass

    # ------------------------------------------------------------ 鼠标数据
    def _on_cursor_dot(self, on: bool) -> None:
        if self._building:
            return
        self.ctrl.set_cursor_dot(bool(on))
        self._building = True
        try:
            self._sync_cursor()
        finally:
            self._building = False

    def _sync_cursor(self) -> None:
        cfg = self.ctrl.cfg
        self.chk_cursor.setChecked(bool(cfg.cursor_dot))
        try:
            color = QColor(str(cfg.cursor_color))
            if color.isValid():
                self.swatches.set_current(color)
        except (TypeError, ValueError):
            pass
        alpha = max(0, min(100, int(cfg.cursor_alpha)))
        items = [(tr("mouse.scheme"), -1)]
        for i, s in enumerate(cfg.cursor_schemes):
            items.append((str(s.get("name") or tr("mouse.scheme_n", n=i + 1)), i))
        self.cmb_scheme.set_items(items, 0)
        self.btn_scheme_del.setEnabled(bool(cfg.cursor_schemes))
        self.btn_more_color.setToolTip(f"当前：{cfg.cursor_color} · {alpha}%")

    def _on_color_picked(self, color: QColor) -> None:
        if self._building:
            return
        self.ctrl.set_cursor_style(color.name(), None)

    def _pick_cursor_color(self) -> None:
        cfg = self.ctrl.cfg
        current = QColor(str(cfg.cursor_color))
        picked = ColorPicker.pick(current, int(cfg.cursor_alpha), self._dark, self)
        if picked is None or not picked.isValid():
            return
        percent = int(round(picked.alpha() * 100.0 / 255.0))
        self.ctrl.set_cursor_style(picked.name(), percent)
        self._sync_cursor()

    # ------------------------------------------------------------ 鼠标样式方案
    def _on_scheme(self, data) -> None:
        if self._building or data is None:
            return
        try:
            i = int(data)
        except (TypeError, ValueError):
            return
        schemes = self.ctrl.cfg.cursor_schemes
        if 0 <= i < len(schemes):
            s = schemes[i]
            self.ctrl.set_cursor_style(str(s.get("color") or "#60646c"),
                                       int(s.get("alpha", 100)))
            self._sync_cursor()

    def _save_scheme(self) -> None:
        cfg = self.ctrl.cfg
        default = tr("mouse.scheme_n", n=len(cfg.cursor_schemes) + 1)
        name = GlassInput.prompt(tr("mouse.save_title"), tr("mouse.save_label"),
                                 default, self._dark, self)
        if not name:
            return
        item = {"name": name, "color": str(cfg.cursor_color),
                "alpha": int(cfg.cursor_alpha)}
        cfg.cursor_schemes = [s for s in cfg.cursor_schemes
                              if str(s.get("name")) != name] + [item]
        cfg.save()
        self._sync_cursor()

    def _del_scheme(self) -> None:
        cfg = self.ctrl.cfg
        data = self.cmb_scheme.current_data()
        if data is None:
            return
        try:
            i = int(data)
        except (TypeError, ValueError):
            return
        if 0 <= i < len(cfg.cursor_schemes):
            cfg.cursor_schemes = [s for j, s in enumerate(cfg.cursor_schemes)
                                  if j != i]
            cfg.save()
            self._sync_cursor()

    def _reset_cursor_style(self) -> None:
        self.ctrl.set_cursor_style(DEFAULT_CURSOR_COLOR, DEFAULT_CURSOR_ALPHA)
        self._sync_cursor()

    # ------------------------------------------------------------ 数据同步
    def refresh(self) -> None:
        self._building = True
        try:
            cfg = self.ctrl.cfg
            self.chk_cursor.setChecked(bool(cfg.cursor_dot))
            self.chk_fullscreen.setChecked(
                bool(getattr(cfg, "hide_fullscreen", True)))
            self.chk_auto_align.setChecked(bool(getattr(cfg, "auto_align", True)))
            self.chk_box_merge.setChecked(bool(getattr(cfg, "box_merge", True)))
            self.picker.set_current(str(getattr(cfg, "panel_theme", "light")))
            codes = [c for c, _ in LANGS]
            cur = str(getattr(cfg, "lang", "zh") or "zh")
            self.cmb_lang.set_items([(lang_label(c), c) for c in codes],
                                    codes.index(cur) if cur in codes else 0)
            self._sync_cursor()
            self._sync_hotkeys()
            self._rebuild_cards()
            self._rebuild_boxes()
            self.radius = float(self.ctrl.cfg.box_radius or 10)
        finally:
            self._building = False

    def _on_hide_fullscreen(self, on: bool) -> None:
        if self._building:
            return
        self.ctrl.set_hide_fullscreen(bool(on))

    def sync_state(self) -> None:
        """外部状态变了（比如桌面被停用、建/拆了盒子）：列表都刷一遍。"""
        try:
            self._rebuild_cards()
            self._rebuild_boxes()
        except (RuntimeError, TypeError, AttributeError):
            pass

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        round_corners(int(self.winId()), 2)

    def closeEvent(self, event) -> None:
        try:
            self.setWindowOpacity(0.0)
        except (RuntimeError, TypeError):
            pass
        try:
            self.hide()
        except (RuntimeError, TypeError):
            pass
        try:
            self.setWindowOpacity(1.0)
        except (RuntimeError, TypeError):
            pass
        self._backdrop = None
        anim = getattr(self, "_intro", None)
        if anim is not None:
            try:
                anim.stop()
            except (RuntimeError, TypeError):
                pass
            self._intro = None
        home = getattr(self, "_home_geo", None)
        if home is not None:
            self.move(home.topLeft())
        self.ctrl.cfg.save()
        super().closeEvent(event)


_COMMON = """
QLabel { font-size: 12px; }
QLabel#title { font-size: 15px; font-weight: 600; }
QLabel#head { font-size: 10px; letter-spacing: 2px; }
QLabel#tag { font-size: 11px; }
QLabel#cardname { font-size: 12px; }
QLabel#state { font-size: 10px; }
QLabel#stateoff { font-size: 10px; }
QFrame#line { border: none; max-height: 1px; }
QFrame#sidebar { border-radius: 10px; }

QPushButton {
    border: none;
    border-radius: 10px;
    padding: 8px 12px;
    font-size: 12px;
}
QPushButton#close { border-radius: 13px; font-size: 13px; padding: 0px; }
QPushButton#nav {
    text-align: left;
    padding: 8px 10px;
    font-size: 12px;
    background: transparent;
}
QPushButton#navquit {
    text-align: left;
    padding: 8px 6px;
    font-size: 11px;
}
QPushButton#keybtn { text-align: left; font-size: 12px; }
QPushButton#uninstall {
    text-align: left;
    padding: 8px 6px;
    font-size: 11px;
    background: transparent;
}
QPushButton#ghost { }
QPushButton#danger { }
QLineEdit#search {
    border: none;
    border-radius: 10px;
    padding: 8px 12px;
    font-size: 12px;
}
QFrame#cardrow { border-radius: 10px; }
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 6px; margin: 0px; }
QScrollBar::handle:vertical { border-radius: 3px; min-height: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QStackedWidget { background: transparent; }
"""

_QSS = scale_qss(_COMMON + """
QLabel { color: #2A3242; }
QLabel#title { color: #141A24; }
QLabel#head { color: rgba(58, 74, 100, 150); }
QLabel#tag { color: rgba(52, 68, 94, 210); }
QLabel#cardname { color: #1E2836; }
QLabel#state { color: rgba(48, 120, 96, 220); }
QLabel#stateoff { color: rgba(90, 100, 118, 160); }
QLabel#err { color: #C0303A; font-size: 11px; }
QFrame#line { background: rgba(28, 42, 64, 20); }
QFrame#sidebar { background: rgba(255, 255, 255, 72); }
QFrame#cardrow { background: rgba(255, 255, 255, 118); }

QPushButton { background: rgba(255, 255, 255, 120); color: #2A3242; }
QPushButton:hover { background: rgba(255, 255, 255, 185); }
QPushButton:pressed { background: rgba(214, 228, 250, 215); }
QPushButton:disabled { color: rgba(60, 76, 100, 85); background: rgba(255, 255, 255, 60); }
QPushButton#close { background: transparent; color: rgba(40, 56, 80, 150); }
QPushButton#close:hover { background: rgba(240, 90, 90, 170); color: white; }
QPushButton#nav { color: rgba(52, 68, 94, 190); }
QPushButton#nav:hover { background: rgba(255, 255, 255, 150); }
QPushButton#nav:checked {
    background: rgba(255, 255, 255, 225);
    color: #14203A;
    font-weight: 600;
}
QPushButton#navquit { background: transparent; color: #C0303A; }
QPushButton#navquit:hover { background: rgba(255, 210, 210, 190); }
QPushButton#keybtn {
    background: rgba(255, 255, 255, 160);
    color: #1B2330;
    font-weight: 600;
}
QPushButton#keybtn:hover { background: rgba(255, 255, 255, 220); }
QPushButton#ghost { background: rgba(255, 255, 255, 70); color: rgba(48, 64, 92, 210); }
QPushButton#ghost:hover { background: rgba(255, 255, 255, 140); }
QPushButton#danger { background: transparent; color: #C0303A; }
QPushButton#danger:hover { background: rgba(255, 190, 190, 175); }
QPushButton#uninstall { color: #C0303A; }
QPushButton#uninstall:hover { background: rgba(255, 190, 190, 175); }
QLineEdit#search { background: rgba(255, 255, 255, 120); color: #2A3242; }
QLineEdit#search:focus { background: rgba(255, 255, 255, 205); }
QScrollBar::handle:vertical { background: rgba(60, 80, 110, 70); }

QSlider::groove:horizontal { height: 4px; background: rgba(40, 60, 100, 40); border-radius: 2px; }
QSlider::sub-page:horizontal { background: #6F8FE8; border-radius: 2px; }
QSlider::handle:horizontal {
    width: 16px; height: 16px; margin: -6px 0;
    border-radius: 8px; background: #FFFFFF;
    border: 1px solid rgba(70, 100, 170, 90);
}
""")

_QSS_DARK = scale_qss(_COMMON + """
QLabel { color: #E3E8F4; }
QLabel#title { color: #F5F8FF; }
QLabel#head { color: rgba(196, 206, 228, 145); }
QLabel#tag { color: rgba(214, 224, 244, 210); }
QLabel#cardname { color: #EDF1FA; }
QLabel#state { color: rgba(140, 220, 180, 225); }
QLabel#stateoff { color: rgba(190, 200, 220, 140); }
QLabel#err { color: #FF8E93; font-size: 11px; }
QFrame#line { background: rgba(255, 255, 255, 24); }
QFrame#sidebar { background: rgba(255, 255, 255, 14); }
QFrame#cardrow { background: rgba(255, 255, 255, 20); }

QPushButton { background: rgba(255, 255, 255, 18); color: #E3E8F4; }
QPushButton:hover { background: rgba(255, 255, 255, 34); }
QPushButton:pressed { background: rgba(150, 178, 246, 60); }
QPushButton:disabled { color: rgba(200, 208, 228, 80); background: rgba(255, 255, 255, 8); }
QPushButton#close { background: transparent; color: rgba(210, 220, 240, 175); }
QPushButton#close:hover { background: rgba(240, 90, 90, 180); color: white; }
QPushButton#nav { color: rgba(206, 216, 240, 190); }
QPushButton#nav:hover { background: rgba(255, 255, 255, 22); }
QPushButton#nav:checked {
    background: rgba(255, 255, 255, 40);
    color: #F5F8FF;
    font-weight: 600;
}
QPushButton#navquit { background: transparent; color: #FF8E93; }
QPushButton#navquit:hover { background: rgba(255, 120, 120, 55); }
QPushButton#keybtn {
    background: rgba(255, 255, 255, 24);
    color: #EDF1FA;
    font-weight: 600;
}
QPushButton#keybtn:hover { background: rgba(255, 255, 255, 40); }
QPushButton#ghost { background: rgba(255, 255, 255, 10); color: rgba(214, 224, 244, 220); }
QPushButton#ghost:hover { background: rgba(255, 255, 255, 24); }
QPushButton#danger { background: transparent; color: #FF8E93; }
QPushButton#danger:hover { background: rgba(255, 120, 120, 60); }
QPushButton#uninstall { color: #FF8E93; }
QPushButton#uninstall:hover { background: rgba(255, 120, 120, 60); }
QLineEdit#search { background: rgba(255, 255, 255, 18); color: #E3E8F4; }
QLineEdit#search:focus { background: rgba(255, 255, 255, 30); }
QScrollBar::handle:vertical { background: rgba(255, 255, 255, 60); }
""")
