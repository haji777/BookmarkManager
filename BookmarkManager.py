import sys
import os
import glob
import json
import subprocess
import shlex
import getpass
import configparser

# Check if a version argument is provided
if len(sys.argv) > 1:
    maya_version = int(sys.argv[1])  # Get the Maya version from the command-line argument
else:
    print("Error: No Maya version provided.")
    sys.exit(1)

# Try importing PySide2 (for Maya 2023) or PySide6 (for Maya 2025 and beyond)
print(f"Using Maya {maya_version}")
if maya_version >= 2025:
    from PySide6.QtWidgets import (
        QApplication, QWidget, QVBoxLayout, QHBoxLayout, QAbstractItemView,
        QPushButton, QComboBox, QFileDialog, QInputDialog, QMessageBox,
        QTabWidget, QMenu, QColorDialog, QTableWidget, QTableWidgetItem,
        QHeaderView, QDialog, QLineEdit, QSpinBox, QLabel, QFormLayout,
        QDialogButtonBox, QRadioButton, QGroupBox, QToolButton, QFileIconProvider
    )
    from PySide6.QtGui import QIcon, QColor, QFont, QPalette, QDrag
    from PySide6.QtCore import Qt, QSize, QFileInfo, QMimeData
else:
    from PySide2.QtWidgets import (
        QApplication, QWidget, QVBoxLayout, QHBoxLayout, QAbstractItemView,
        QPushButton, QComboBox, QFileDialog, QInputDialog, QMessageBox,
        QTabWidget, QMenu, QColorDialog, QTableWidget, QTableWidgetItem,
        QHeaderView, QDialog, QLineEdit, QSpinBox, QLabel, QFormLayout,
        QDialogButtonBox, QRadioButton, QGroupBox, QToolButton, QFileIconProvider
    )
    from PySide2.QtGui import QIcon, QColor, QFont, QPalette, QDrag
    from PySide2.QtCore import Qt, QSize, QFileInfo, QMimeData


# --- アプリ本体とユーザーデータを分離する ---
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
USER_DATA_DIR = os.path.join(os.path.expanduser("~"), ".BookmarkManager")
os.makedirs(USER_DATA_DIR, exist_ok=True)
SETTINGS_PATH = os.path.join(USER_DATA_DIR, "settings.ini")
WORKSPACE_PATH = os.path.join(USER_DATA_DIR, "workspace.json")


def icon(name):
    """icons/ 配下のアイコンを絶対パスで返す"""
    return QIcon(os.path.join(CURRENT_DIR, "icons", name))


# ============================================================
#  グローバル設定 (settings.ini)
# ============================================================
DEFAULT_SETTINGS = {
    "font_size": "9",
    "user_name": "",          # 空ならシステムのユーザー名を使う
    "shared_project_dir": "",  # ネットワーク上の共有プロジェクトjson置き場
}


def load_global_settings():
    cfg = configparser.ConfigParser()
    settings = dict(DEFAULT_SETTINGS)
    if os.path.exists(SETTINGS_PATH):
        try:
            cfg.read(SETTINGS_PATH, encoding="utf-8")
            if cfg.has_section("global"):
                for key in DEFAULT_SETTINGS:
                    if cfg.has_option("global", key):
                        settings[key] = cfg.get("global", key)
        except Exception as e:
            print(f"settings.ini の読み込みに失敗: {e}")
    return settings


def save_global_settings(settings):
    cfg = configparser.ConfigParser()
    cfg["global"] = {k: str(v) for k, v in settings.items()}
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            cfg.write(f)
    except Exception as e:
        print(f"settings.ini の保存に失敗: {e}")


GLOBAL_SETTINGS = load_global_settings()


def current_user_name():
    name = (GLOBAL_SETTINGS.get("user_name") or "").strip()
    if name:
        return name
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USERNAME", "")


# ============================================================
#  変数解決
#  {proj}{show}{ep}{sq}{shot}{user} + 独自変数 + 環境変数(%VAR% / $VAR)
# ============================================================
class _SafeDict(dict):
    """format_map で未定義キーがあっても {key} のまま残す"""
    def __missing__(self, key):
        return "{" + key + "}"


def resolve_path(text, project_tab):
    if not text:
        return text
    pdata = project_tab.project_data
    variables = {
        "proj": project_tab.proj,
        "show": pdata.get("show") or project_tab.proj,
        "ep": project_tab.ep,
        "sq": project_tab.sq,
        "shot": project_tab.shot,
        "user": current_user_name(),
    }
    # プロジェクト独自変数
    variables.update(pdata.get("variables", {}) or {})
    try:
        text = text.format_map(_SafeDict(variables))
    except Exception as e:
        print(f"変数展開に失敗: {e}")
    # Windows環境変数 %VAR% と $VAR / ${VAR}
    text = os.path.expandvars(text)
    return text


def build_env(project_tab):
    """サブプロセス起動用の環境変数を作る"""
    pdata = project_tab.project_data
    env = os.environ.copy()
    env["PROJ"] = project_tab.proj or ""
    env["SHOW"] = (pdata.get("show") or project_tab.proj) or ""
    env["EP"] = project_tab.ep or ""
    env["SQ"] = project_tab.sq or ""
    env["SHOT"] = project_tab.shot or ""
    env["USER_NAME"] = current_user_name()
    env["project_shot_structure"] = pdata.get(
        "project_shot_structure", "N:/{proj}/shots/{ep}/{shot}")
    env["project_comp_structure"] = pdata.get(
        "project_comp_structure", "N:/{proj}/comp/{ep}/{shot}")
    for k, v in (pdata.get("variables", {}) or {}).items():
        env[str(k)] = str(v)
    return env


def program_args(program_path, project_tab):
    """Resolve a saved program setting into an executable path and arguments."""
    resolved = resolve_path(program_path, project_tab).strip()
    unquoted = resolved
    if len(unquoted) >= 2 and unquoted[0] == unquoted[-1] and unquoted[0] in "\"'":
        unquoted = unquoted[1:-1]
    return [unquoted] if os.path.exists(unquoted) else shlex.split(resolved)


def _is_image(path):
    return bool(path) and path.lower().endswith((".png", ".ico", ".jpg", ".jpeg", ".bmp"))


def resolve_button_icon_path(path):
    """Use a matching icon from this installation when a saved old path moved."""
    if not _is_image(path):
        return None
    if os.path.exists(path):
        return path
    local_path = os.path.join(CURRENT_DIR, "icons", os.path.basename(path))
    return local_path if os.path.exists(local_path) else None


# ============================================================
#  ボタンラベル設定ダイアログ（文字列・画像を1画面に統合）
# ============================================================
class ButtonLabelDialog(QDialog):
    def __init__(self, parent, current_label="", current_icon=""):
        super().__init__(parent)
        self.setWindowTitle("ボタンラベル設定")
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        # 文字列と画像は排他ではなく、同時に設定できる。
        layout.addWidget(QLabel("ラベル文字列"))
        self.text_edit = QLineEdit()
        self.text_edit.setText(current_label or "")
        self.text_edit.setPlaceholderText("例: Blender")
        layout.addWidget(self.text_edit)

        layout.addWidget(QLabel("アイコン画像（任意）"))
        img_row = QHBoxLayout()
        self.image_edit = QLineEdit()
        self.image_edit.setText(current_icon or "")
        self.image_edit.setPlaceholderText("未指定の場合はプログラムからアイコンを取得")
        browse_btn = QPushButton("参照...")
        browse_btn.clicked.connect(self.browse_image)
        img_row.addWidget(self.image_edit)
        img_row.addWidget(browse_btn)
        layout.addLayout(img_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def browse_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "画像ファイルを選択", CURRENT_DIR,
            "画像ファイル (*.png *.ico *.jpg *.jpeg *.bmp)")
        if path:
            self.image_edit.setText(path)

    def selected_values(self):
        """文字ラベルと任意のカスタムアイコンパスを返す。"""
        return self.text_edit.text().strip(), self.image_edit.text().strip()


# ============================================================
#  設定ダイアログ（プロジェクト設定 + 全体設定）
# ============================================================
class SettingsDialog(QDialog):
    def __init__(self, parent, project_tab):
        super().__init__(parent)
        self.project_tab = project_tab
        self.setWindowTitle("設定")
        self.setMinimumWidth(460)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        tabs = QTabWidget()
        root.addWidget(tabs)
        tabs.addTab(self._build_project_tab(), "プロジェクト設定")
        tabs.addTab(self._build_global_tab(), "全体設定")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _build_project_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        pdata = self.project_tab.project_data

        self.show_edit = QLineEdit(pdata.get("show", "") or "")
        self.show_edit.setPlaceholderText(self.project_tab.proj)
        self.shot_struct_edit = QLineEdit(
            pdata.get("project_shot_structure", "N:/{proj}/shots/{ep}/{shot}"))
        self.comp_struct_edit = QLineEdit(
            pdata.get("project_comp_structure", "N:/{proj}/comp/{ep}/{shot}"))

        form.addRow("$show", self.show_edit)
        form.addRow("shot構造", self.shot_struct_edit)
        form.addRow("comp構造", self.comp_struct_edit)

        # 独自変数テーブル
        var_group = QGroupBox("独自変数（パス内で {名前} / %名前% として使用）")
        vg = QVBoxLayout(var_group)
        self.var_table = QTableWidget(0, 2)
        self.var_table.setHorizontalHeaderLabels(["変数名", "値"])
        self.var_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.var_table.verticalHeader().setVisible(False)
        for name, value in (pdata.get("variables", {}) or {}).items():
            self._add_var_row(name, value)
        vg.addWidget(self.var_table)
        vbtns = QHBoxLayout()
        add_v = QPushButton("追加")
        del_v = QPushButton("削除")
        add_v.clicked.connect(lambda: self._add_var_row("", ""))
        del_v.clicked.connect(self._del_var_row)
        vbtns.addStretch()
        vbtns.addWidget(add_v)
        vbtns.addWidget(del_v)
        vg.addLayout(vbtns)
        form.addRow(var_group)
        return w

    def _add_var_row(self, name, value):
        r = self.var_table.rowCount()
        self.var_table.insertRow(r)
        self.var_table.setItem(r, 0, QTableWidgetItem(str(name)))
        self.var_table.setItem(r, 1, QTableWidgetItem(str(value)))

    def _del_var_row(self):
        r = self.var_table.currentRow()
        if r >= 0:
            self.var_table.removeRow(r)

    def _build_global_tab(self):
        w = QWidget()
        form = QFormLayout(w)

        self.font_spin = QSpinBox()
        self.font_spin.setRange(6, 32)
        self.font_spin.setValue(int(GLOBAL_SETTINGS.get("font_size", "9") or 9))

        self.user_edit = QLineEdit(GLOBAL_SETTINGS.get("user_name", "") or "")
        try:
            self.user_edit.setPlaceholderText(getpass.getuser())
        except Exception:
            pass

        self.shared_edit = QLineEdit(GLOBAL_SETTINGS.get("shared_project_dir", "") or "")
        shared_row = QHBoxLayout()
        shared_row.addWidget(self.shared_edit)
        browse = QPushButton("参照...")
        browse.clicked.connect(self._browse_shared)
        shared_row.addWidget(browse)
        shared_wrap = QWidget()
        shared_wrap.setLayout(shared_row)

        form.addRow("文字サイズ", self.font_spin)
        form.addRow("ユーザー名", self.user_edit)
        form.addRow("共有プロジェクト\nフォルダ", shared_wrap)
        note = QLabel("※共有フォルダ内の *.json は起動時にタブとして読み込まれます")
        note.setWordWrap(True)
        form.addRow(note)
        return w

    def _browse_shared(self):
        d = QFileDialog.getExistingDirectory(self, "共有プロジェクトフォルダを選択")
        if d:
            self.shared_edit.setText(d)

    def accept(self):
        # --- プロジェクト設定を反映 ---
        pdata = self.project_tab.project_data
        show_val = self.show_edit.text().strip()
        if show_val:
            pdata["show"] = show_val
        else:
            pdata.pop("show", None)
        pdata["project_shot_structure"] = self.shot_struct_edit.text().strip()
        pdata["project_comp_structure"] = self.comp_struct_edit.text().strip()

        variables = {}
        for r in range(self.var_table.rowCount()):
            name_item = self.var_table.item(r, 0)
            val_item = self.var_table.item(r, 1)
            name = name_item.text().strip() if name_item else ""
            val = val_item.text().strip() if val_item else ""
            if name:
                variables[name] = val
        pdata["variables"] = variables
        self.project_tab.save_config()

        # --- 全体設定を反映 ---
        GLOBAL_SETTINGS["font_size"] = str(self.font_spin.value())
        GLOBAL_SETTINGS["user_name"] = self.user_edit.text().strip()
        GLOBAL_SETTINGS["shared_project_dir"] = self.shared_edit.text().strip()
        save_global_settings(GLOBAL_SETTINGS)
        apply_font_size(self.font_spin.value())

        super().accept()


DARK_MODE = False  # 起動時に is_dark_mode() で確定


def is_dark_mode():
    """OSがダークモードかどうか（Windows）"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return val == 0
    except Exception:
        pass
    # フォールバック: 現在のパレットの明度で判定
    app = QApplication.instance()
    if app:
        return app.palette().color(QPalette.Window).lightness() < 128
    return False


def make_dark_palette():
    pal = QPalette()
    window = QColor(60, 60, 60)
    base = QColor(46, 46, 46)
    button = QColor(72, 72, 72)
    text = QColor(235, 235, 235)
    disabled = QColor(130, 130, 130)
    highlight = QColor(74, 144, 226)
    pal.setColor(QPalette.Window, window)
    pal.setColor(QPalette.WindowText, text)
    pal.setColor(QPalette.Base, base)
    pal.setColor(QPalette.AlternateBase, window)
    pal.setColor(QPalette.ToolTipBase, window)
    pal.setColor(QPalette.ToolTipText, text)
    pal.setColor(QPalette.Text, text)
    pal.setColor(QPalette.Button, button)
    pal.setColor(QPalette.ButtonText, text)
    pal.setColor(QPalette.Highlight, highlight)
    pal.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
    pal.setColor(QPalette.PlaceholderText, disabled)
    pal.setColor(QPalette.Disabled, QPalette.Text, disabled)
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, disabled)
    pal.setColor(QPalette.Disabled, QPalette.WindowText, disabled)
    return pal


def build_stylesheet(size):
    if DARK_MODE:
        border = "rgba(255,255,255,45)"
        grid = "rgba(255,255,255,16)"
        btn_bg = "rgba(255,255,255,16)"
        btn_hover = "rgba(255,255,255,40)"
        btn_pressed = "rgba(255,255,255,12)"
        accent = "#5a9cf0"
        tab_bg = "rgba(255,255,255,10)"
        tab_sel = "rgba(90,156,240,55)"
        dim_text = "rgba(235,235,235,150)"
    else:
        border = "rgba(0,0,0,55)"
        grid = "rgba(0,0,0,28)"
        btn_bg = "rgba(0,0,0,8)"
        btn_hover = "rgba(0,0,0,20)"
        btn_pressed = "rgba(0,0,0,32)"
        accent = "#2d6fc8"
        tab_bg = "rgba(0,0,0,8)"
        tab_sel = "rgba(45,111,200,40)"
        dim_text = "rgba(0,0,0,140)"
    return f"""
QWidget {{ font-size: {size}pt; }}
QTabWidget::pane {{ border: 1px solid {border}; }}
QTabBar::tab {{
    padding: 3px 12px; margin-right: 1px;
    border: 1px solid {border}; border-bottom: none;
    border-top-left-radius: 2px; border-top-right-radius: 2px;
    background: {tab_bg}; color: {dim_text};
}}
QTabBar::tab:selected {{
    background: {tab_sel}; color: palette(window-text);
    border-top: 2px solid {accent}; font-weight: bold;
}}
QTableWidget {{ gridline-color: {grid}; border: 1px solid {border}; }}
QTableWidget::item {{ padding: 1px 4px; }}
QHeaderView::section {{ padding: 2px 6px; }}
QToolButton, QPushButton {{
    border: 1px solid {border}; border-radius: 2px;
    padding: 3px 8px; background: {btn_bg};
}}
QToolButton:hover, QPushButton:hover {{ background: {btn_hover}; border-color: {accent}; }}
QToolButton:pressed, QPushButton:pressed {{ background: {btn_pressed}; }}
QComboBox {{ padding: 1px 4px; border-radius: 2px; border: 1px solid {border}; }}
QLineEdit, QSpinBox {{ border-radius: 2px; padding: 1px 3px; border: 1px solid {border}; }}
"""


def apply_appearance(app):
    """ダークモード判定とパレットを適用（起動時に一度だけ）"""
    global DARK_MODE
    DARK_MODE = is_dark_mode()
    if DARK_MODE:
        app.setStyle("Fusion")
        app.setPalette(make_dark_palette())


def apply_font_size(size):
    """フォントとスタイルシートを今すぐ反映（再起動不要）"""
    app = QApplication.instance()
    if not app:
        return
    size = int(size)
    f = app.font()
    f.setPointSize(size)
    app.setFont(f)
    app.setStyleSheet(build_stylesheet(size))


# ============================================================
#  プログラム起動ボタン
# ============================================================
class ConfigurableButton(QPushButton):
    def __init__(self, label, icon_path, initial_program_path, project_tab,
                 button_index, update_callback):
        super().__init__()
        self.program_path = initial_program_path
        self.icon_path = icon_path or ""
        self.project_tab = project_tab
        self.button_index = button_index
        self.update_callback = update_callback
        self.drag_start_pos = None
        self.setFixedSize(80, 32)
        self.setAcceptDrops(True)
        # 枠線とホバーは共通スタイルに任せ、内側パディングだけ詰めてアイコン領域を確保
        self.setStyleSheet("ConfigurableButton { padding: 1px; }")
        self.set_button_label(label)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.open_context_menu)
        self.clicked.connect(self.run_program)

    def set_button_label(self, label):
        icon_path = resolve_button_icon_path(self.icon_path)
        icon = QIcon()
        if icon_path:
            icon = QIcon(icon_path)
            # Some bundled Qt builds cannot decode every multi-size ICO file.
            # In that case, ask Windows for the file's shell icon instead.
            if icon_path.lower().endswith(".ico") and not icon.availableSizes():
                icon = QFileIconProvider().icon(QFileInfo(icon_path))
        else:
            try:
                args = program_args(self.program_path, self.project_tab)
                if args and os.path.exists(args[0]):
                    icon = QFileIconProvider().icon(QFileInfo(args[0]))
            except (ValueError, OSError):
                pass
        self.setIcon(icon)
        self.setIconSize(QSize(20 if label else 26, 20 if label else 26))
        self.setText(label or "")
        self.current_label = label

    def run_program(self):
        if not self.program_path:
            QMessageBox.warning(self, "エラー", "プログラムパスが未設定です。")
            return
        try:
            args = program_args(self.program_path, self.project_tab)
            if not args or not os.path.exists(args[0]):
                QMessageBox.warning(self, "エラー", f"実行ファイルが見つかりません: {args[0] if args else ''}")
                return
            if os.path.splitext(args[0])[1].lower() in (".lnk", ".url"):
                os.startfile(args[0])
                return
            # @echo off only suppresses command output; it does not hide the
            # console window.  Start batch files without creating a console.
            creationflags = 0
            if os.path.splitext(args[0])[1].lower() in (".bat", ".cmd"):
                creationflags = subprocess.CREATE_NO_WINDOW
            subprocess.Popen(
                args,
                env=build_env(self.project_tab),
                creationflags=creationflags,
            )
        except Exception as e:
            QMessageBox.warning(self, "エラー", f"プログラム起動失敗: {e}")

    def dragEnterEvent(self, event):
        source = event.source()
        if isinstance(source, ConfigurableButton) and source.project_tab is self.project_tab:
            event.acceptProposedAction()
            return
        if event.mimeData().hasUrls():
            paths = [url.toLocalFile() for url in event.mimeData().urls()]
            if any(path and os.path.isfile(path) for path in paths):
                event.acceptProposedAction()
                return
        event.ignore()

    def dropEvent(self, event):
        source = event.source()
        if isinstance(source, ConfigurableButton) and source.project_tab is self.project_tab:
            if source is not self:
                self.project_tab.swap_button_settings(
                    source.button_index, self.button_index)
            event.acceptProposedAction()
            return
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path and os.path.isfile(path):
                self.program_path = os.path.normpath(path)
                self.set_button_label(self.current_label)
                if self.update_callback:
                    self.update_callback("program", self.program_path)
                event.acceptProposedAction()
                return
        event.ignore()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_start_pos = (
                event.position().toPoint() if hasattr(event, "position")
                else event.pos()
            )
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (self.drag_start_pos is not None
                and event.buttons() & Qt.LeftButton):
            current_pos = (
                event.position().toPoint() if hasattr(event, "position")
                else event.pos()
            )
            if ((current_pos - self.drag_start_pos).manhattanLength()
                    >= QApplication.startDragDistance()):
                drag = QDrag(self)
                mime = QMimeData()
                mime.setData("application/x-bookmark-manager-button", b"swap")
                drag.setMimeData(mime)
                drag.setPixmap(self.grab())
                if hasattr(drag, "exec"):
                    drag.exec(Qt.MoveAction)
                else:
                    drag.exec_(Qt.MoveAction)
                self.drag_start_pos = None
                return
        super().mouseMoveEvent(event)

    def open_context_menu(self, pos):
        menu = QMenu(self)
        set_path_action = menu.addAction("パスを指定")
        edit_path_action = menu.addAction("パスを編集")
        set_label_action = menu.addAction("ボタンラベルを設定")

        action = menu.exec_(self.mapToGlobal(pos))

        if action == set_path_action:
            new_path, _ = QFileDialog.getOpenFileName(
                self, "プログラムを選択", "",
                "プログラムとショートカット (*.exe *.bat *.cmd *.py *.lnk *.url);;すべてのファイル (*)")
            if new_path:
                self.program_path = new_path
                self.set_button_label(self.current_label)
                if self.update_callback:
                    self.update_callback("program", new_path)

        elif action == edit_path_action:
            current = self.program_path or ""
            new_path, ok = QInputDialog.getText(self, "パスを編集", "パスを入力:", text=current)
            if ok and new_path:
                self.program_path = new_path
                self.set_button_label(self.current_label)
                if self.update_callback:
                    self.update_callback("program", new_path)

        elif action == set_label_action:
            dlg = ButtonLabelDialog(self, self.current_label, self.icon_path)
            if dlg.exec_():
                label, icon_path = dlg.selected_values()
                self.icon_path = icon_path
                self.set_button_label(label)
                if self.update_callback:
                    self.update_callback("label", label)
                    self.update_callback("icon", icon_path)


# ============================================================
#  ブックマークテーブル（名前 | パス、行並べ替え/D&D追加/Del削除）
# ============================================================
def _event_pos(event):
    """PySide2/6 両対応で QDropEvent の位置を取得"""
    if hasattr(event, "position"):
        return event.position().toPoint()
    return event.pos()


class BookmarkTableWidget(QTableWidget):
    def __init__(self, parent_tab):
        super().__init__(0, 2)
        self.parent_tab = parent_tab

        self.setHorizontalHeaderLabels(["名前", "パス"])
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setVisible(False)  # 省スペース化のため列ヘッダ[名前][パス]は非表示
        self.verticalHeader().setVisible(False)
        self.setShowGrid(False)
        self.setAlternatingRowColors(False)  # ゼブラは refresh_zebra() で明示的に塗る
        self.setWordWrap(False)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.verticalHeader().setDefaultSectionSize(22)

        # ドラッグ＆ドロップ（外部ファイル追加 + 内部並べ替え）
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDragDropOverwriteMode(False)

        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)
        self.cellDoubleClicked.connect(self._on_double_click)

    # ---------- 行ユーティリティ ----------
    def append_row(self, name, path, color=None):
        r = self.rowCount()
        self.insertRow(r)
        self._set_row(r, name, path, color)

    def _set_row(self, row, name, path, color=None):
        name_item = QTableWidgetItem(name)
        path_item = QTableWidgetItem(path)
        if color:
            c = QColor(color)
            name_item.setBackground(c)
            path_item.setBackground(c)
            name_item.setData(Qt.UserRole, color)
        self.setItem(row, 0, name_item)
        self.setItem(row, 1, path_item)

    def row_data(self, row):
        name_item = self.item(row, 0)
        path_item = self.item(row, 1)
        name = name_item.text() if name_item else ""
        path = path_item.text() if path_item else ""
        color = name_item.data(Qt.UserRole) if name_item else None
        return name, path, color

    def refresh_zebra(self):
        """1行おきに背景に薄い差をつける。色指定済みの行はその色を維持。
        ダークモードでは暗い行をさらに暗くしても見えないので、逆に少し明るくする。"""
        zebra = QColor(255, 255, 255, 16) if DARK_MODE else QColor(0, 0, 0, 40)
        clear = QColor(0, 0, 0, 0)
        for r in range(self.rowCount()):
            name_item = self.item(r, 0)
            user_color = name_item.data(Qt.UserRole) if name_item else None
            brush = QColor(user_color) if user_color else (zebra if r % 2 else clear)
            for c in range(self.columnCount()):
                it = self.item(r, c)
                if it:
                    it.setBackground(brush)

    # ---------- ドラッグ＆ドロップ ----------
    def dragEnterEvent(self, event):
        if event.source() is self or event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.source() is self or event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def _drop_row_index(self, event):
        pos = _event_pos(event)
        index = self.indexAt(pos)
        if not index.isValid():
            return self.rowCount()
        rect = self.visualRect(index)
        if pos.y() > rect.center().y():
            return index.row() + 1
        return index.row()

    def dropEvent(self, event):
        if event.source() is self:
            # --- 内部並べ替え ---
            drop_row = self._drop_row_index(event)
            rows = sorted({idx.row() for idx in self.selectedIndexes()})
            if not rows:
                event.ignore()
                return
            data = [self.row_data(r) for r in rows]
            for r in reversed(rows):
                self.removeRow(r)
                if r < drop_row:
                    drop_row -= 1
            for i, (name, path, color) in enumerate(data):
                self.insertRow(drop_row + i)
                self._set_row(drop_row + i, name, path, color)
            self.clearSelection()
            for i in range(len(data)):
                self.selectRow(drop_row + i)
            self.parent_tab.commit_bookmarks()
            event.acceptProposedAction()
        elif event.mimeData().hasUrls():
            # --- 外部ファイル/フォルダ追加 ---
            for url in event.mimeData().urls():
                path = url.toLocalFile()
                if path and os.path.exists(path):
                    name = os.path.basename(os.path.normpath(path))
                    self.append_row(name, path)
            self.parent_tab.commit_bookmarks()
            event.acceptProposedAction()
        else:
            event.ignore()

    # ---------- キー操作 ----------
    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.delete_selected_rows()
        else:
            super().keyPressEvent(event)

    def delete_selected_rows(self):
        rows = sorted({idx.row() for idx in self.selectedIndexes()}, reverse=True)
        if not rows:
            return
        for r in rows:
            self.removeRow(r)
        self.parent_tab.commit_bookmarks()

    # ---------- コンテキストメニュー ----------
    def show_context_menu(self, pos):
        row = self.rowAt(pos.y())
        if row < 0:
            return
        self.selectRow(row)
        name, path, color = self.row_data(row)

        menu = QMenu(self)
        rename_action = menu.addAction("ブックマーク名の変更")
        color_action = menu.addAction("色の設定")
        clear_color_action = menu.addAction("色をクリア")
        set_path_action = menu.addAction("パスを指定")
        edit_path_action = menu.addAction("パスを編集")
        delete_action = menu.addAction("削除")

        action = menu.exec_(self.viewport().mapToGlobal(pos))

        if action == rename_action:
            new_name, ok = QInputDialog.getText(self, "名前の変更", "新しい名前:", text=name)
            if ok and new_name:
                self._set_row(row, new_name, path, color)
                self.parent_tab.commit_bookmarks()
        elif action == color_action:
            col = QColorDialog.getColor(parent=self)
            if col.isValid():
                self._set_row(row, name, path, col.name())
                self.parent_tab.commit_bookmarks()
        elif action == clear_color_action:
            self._set_row(row, name, path, None)
            self.parent_tab.commit_bookmarks()
        elif action == set_path_action:
            new_path = QFileDialog.getExistingDirectory(self, "パスを指定")
            if new_path:
                self._set_row(row, name, new_path, color)
                self.parent_tab.commit_bookmarks()
        elif action == edit_path_action:
            new_path, ok = QInputDialog.getText(self, "パスの編集", "パスを編集:", text=path)
            if ok and new_path:
                self._set_row(row, name, new_path, color)
                self.parent_tab.commit_bookmarks()
        elif action == delete_action:
            self.removeRow(row)
            self.parent_tab.commit_bookmarks()

    def _on_double_click(self, row, column):
        self.parent_tab.open_explorer_row(row)


# ============================================================
#  プロジェクトタブ
# ============================================================
class ProjectTab(QWidget):
    def __init__(self, project_data, add_project_callback, open_settings_callback,
                 source_path=None):
        super().__init__()
        self.initializing = True
        self.sort_ascending = True
        self.project_data = project_data
        self.add_project_callback = add_project_callback
        self.open_settings_callback = open_settings_callback
        self.source_path = source_path  # 読み込み元（共有プロジェクト用）

        self.proj = project_data.get("name", "blank_project")
        self.ep = self.sq = self.shot = ""

        self.layout = QVBoxLayout()
        self.layout.setContentsMargins(4, 4, 4, 4)
        self.layout.setSpacing(4)

        # --- ep / sq / shot プルダウン ---
        self.epSqShot_layout = QHBoxLayout()
        self.epSqShot_layout.setSpacing(4)
        self.episode_combo = QComboBox()
        self.sequence_combo = QComboBox()
        self.shot_combo = QComboBox()
        self.load_ep_sq_shot()
        self.episode_combo.currentTextChanged.connect(self.update_sequences)
        self.sequence_combo.currentTextChanged.connect(self.update_shots)
        self.shot_combo.currentTextChanged.connect(self.update_label)
        self.epSqShot_layout.addWidget(self.episode_combo)
        self.epSqShot_layout.addWidget(self.sequence_combo)
        self.epSqShot_layout.addWidget(self.shot_combo)
        self.layout.addLayout(self.epSqShot_layout)

        # --- プログラム起動ミニボタン ---
        self.mini_button_layout = QHBoxLayout()
        self.mini_button_layout.setSpacing(4)
        self.mini_buttons = []
        buttons_config = self.project_data.get("buttons", [])
        for idx, item in enumerate(buttons_config):
            if isinstance(item, str):
                buttons_config[idx] = {"program": item, "label": f"ボタン{idx+1}"}
        while len(buttons_config) < 6:
            buttons_config.append({"program": "", "label": f"ボタン{len(buttons_config)+1}"})
        self.project_data["buttons"] = buttons_config

        for idx, btn_conf in enumerate(buttons_config):
            btn = ConfigurableButton(
                label=btn_conf.get("label", f"ボタン{idx+1}"),
                icon_path=btn_conf.get("icon", ""),
                initial_program_path=btn_conf.get("program", ""),
                project_tab=self,
                button_index=idx,
                update_callback=lambda update_type, new_value, i=idx:
                    self.update_button_setting(i, update_type, new_value),
            )
            self.mini_buttons.append(btn)
            self.mini_button_layout.addWidget(btn)
        self.layout.addLayout(self.mini_button_layout)

        # --- ブックマークテーブル ---
        self.table = BookmarkTableWidget(self)
        self.layout.addWidget(self.table)

        # --- 操作ボタン列 ---
        self.bookmark_button_layout = QHBoxLayout()
        self.bookmark_button_layout.setSpacing(4)
        self.add_bookmark_button = QToolButton()
        self.remove_bookmark_button = QToolButton()
        self.sort_bookmark_button = QToolButton()
        self.new_project_button = QToolButton()
        self.settings_button = QToolButton()

        # 設定ボタンと同じグリフ方式に統一（ダークでも見えるようにアイコン画像は使わない）
        self.add_bookmark_button.setText("＋")
        self.remove_bookmark_button.setText("－")
        self.sort_bookmark_button.setText("⇅")
        self.new_project_button.setText("⊞")
        self.settings_button.setText("⚙")
        for b in (self.add_bookmark_button, self.remove_bookmark_button,
                  self.sort_bookmark_button, self.new_project_button,
                  self.settings_button):
            b.setFixedSize(26, 24)
        self.add_bookmark_button.setToolTip("ブックマーク追加")
        self.remove_bookmark_button.setToolTip("選択を削除 (Delキー)")
        self.sort_bookmark_button.setToolTip("名前で並べ替え")
        self.new_project_button.setToolTip("新規プロジェクト")
        self.settings_button.setToolTip("設定")

        self.bookmark_button_layout.addWidget(self.add_bookmark_button)
        self.bookmark_button_layout.addWidget(self.remove_bookmark_button)
        self.bookmark_button_layout.addWidget(self.sort_bookmark_button)
        self.bookmark_button_layout.addStretch()
        self.bookmark_button_layout.addWidget(self.new_project_button)
        self.bookmark_button_layout.addWidget(self.settings_button)
        self.layout.addLayout(self.bookmark_button_layout)

        self.setLayout(self.layout)

        self.add_bookmark_button.clicked.connect(self.add_bookmark)
        self.remove_bookmark_button.clicked.connect(self.table.delete_selected_rows)
        self.sort_bookmark_button.clicked.connect(self.sort_bookmarks)
        self.new_project_button.clicked.connect(self.add_project_callback)
        self.settings_button.clicked.connect(lambda: self.open_settings_callback(self))

        self.load_bookmarks()
        self.initializing = False

    # ---------- ep/sq/shot ----------
    def load_ep_sq_shot(self):
        ep_data = self.project_data.get("epSqShot", {})
        current_ep = self.project_data.get("current_ep", "")
        current_sq = self.project_data.get("current_sq", "")
        current_shot = self.project_data.get("current_shot", "")

        self.episode_combo.clear()
        self.sequence_combo.clear()
        self.shot_combo.clear()
        self.episode_combo.addItems(ep_data.keys())

        if current_ep in ep_data:
            self.episode_combo.setCurrentText(current_ep)
        elif self.episode_combo.count() > 0:
            current_ep = self.episode_combo.itemText(0)
            self.episode_combo.setCurrentText(current_ep)

        seqs = ep_data.get(current_ep, {})
        self.sequence_combo.addItems(seqs.keys())
        if current_sq in seqs:
            self.sequence_combo.setCurrentText(current_sq)
        elif self.sequence_combo.count() > 0:
            current_sq = self.sequence_combo.itemText(0)
            self.sequence_combo.setCurrentText(current_sq)

        shots = seqs.get(current_sq, [])
        self.shot_combo.addItems(shots)
        if current_shot in shots:
            self.shot_combo.setCurrentText(current_shot)
        elif self.shot_combo.count() > 0:
            current_shot = self.shot_combo.itemText(0)
            self.shot_combo.setCurrentText(current_shot)

        self.ep = current_ep
        self.sq = current_sq
        self.shot = current_shot

    def update_sequences(self, episode):
        data = self.project_data.get("epSqShot", {})
        self.sequence_combo.clear()
        if episode in data:
            self.sequence_combo.addItems(data[episode].keys())

    def update_shots(self, sequence):
        data = self.project_data.get("epSqShot", {})
        self.shot_combo.clear()
        episode = self.episode_combo.currentText()
        if episode in data and sequence in data[episode]:
            self.shot_combo.addItems(data[episode][sequence])
        self.update_label()

    def update_label(self):
        self.ep = self.episode_combo.currentText()
        self.sq = self.sequence_combo.currentText()
        self.shot = self.shot_combo.currentText()
        self.project_data["current_ep"] = self.ep
        self.project_data["current_sq"] = self.sq
        self.project_data["current_shot"] = self.shot
        if getattr(self, "initializing", False):
            return
        self.save_config()

    # ---------- ブックマーク ----------
    def load_bookmarks(self):
        self.table.setRowCount(0)
        for item in self.project_data.get("bookmarks", []):
            if isinstance(item, str):
                text, color = item, None
            elif isinstance(item, dict):
                text, color = item.get("text", ""), item.get("color")
            else:
                continue
            name, path = self._split_text(text)
            self.table.append_row(name, path, color)
        self.table.refresh_zebra()

    @staticmethod
    def _split_text(text):
        if " | " in text:
            name, path = text.split(" | ", 1)
            return name.strip(), path.strip()
        return text.strip(), ""

    def commit_bookmarks(self):
        """テーブルの現在状態を project_data に反映して保存（並べ替え・削除・編集の唯一の保存経路）"""
        bookmarks = []
        for r in range(self.table.rowCount()):
            name, path, color = self.table.row_data(r)
            text = f"{name} | {path}" if path else name
            if color:
                bookmarks.append({"text": text, "color": color})
            else:
                bookmarks.append(text)
        self.project_data["bookmarks"] = bookmarks
        self.table.refresh_zebra()
        self.save_config()

    def add_bookmark(self):
        folder = QFileDialog.getExistingDirectory(self, "フォルダを選択")
        if not folder:
            return
        name, ok = QInputDialog.getText(self, "ブックマーク名", "名前を入力:",
                                         text=os.path.basename(os.path.normpath(folder)))
        if not ok or not name:
            return
        self.table.append_row(name, folder)
        self.commit_bookmarks()

    def sort_bookmarks(self):
        rows = [self.table.row_data(r) for r in range(self.table.rowCount())]
        rows.sort(key=lambda x: x[0].lower(), reverse=not self.sort_ascending)
        self.table.setRowCount(0)
        for name, path, color in rows:
            self.table.append_row(name, path, color)
        self.sort_ascending = not self.sort_ascending
        self.commit_bookmarks()

    def open_explorer_row(self, row):
        name, path, color = self.table.row_data(row)
        if not path:
            QMessageBox.warning(self, "エラー", "無効なブックマークです")
            return
        try:
            self.update_label()
            resolved = resolve_path(path, self)
            if os.path.exists(resolved):
                os.startfile(resolved)
            else:
                QMessageBox.warning(self, "エラー", "フォルダが見つかりません: " + resolved)
        except Exception as e:
            QMessageBox.warning(self, "エラー", f"予期しないエラー: {e}")

    # ---------- 保存 ----------
    def save_config(self):
        if self.source_path:
            target = self.source_path
        else:
            target = os.path.join(
                USER_DATA_DIR, f"{self.project_data['name']}.json")
        try:
            with open(target, "w", encoding="utf-8") as f:
                json.dump(self.project_data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"保存に失敗 ({target}): {e}")

    def update_button_setting(self, index, update_type, new_value):
        buttons = self.project_data.get("buttons", [])
        while len(buttons) < 6:
            buttons.append({"program": "", "label": f"ボタン{len(buttons)+1}"})
        if isinstance(buttons[index], str):
            buttons[index] = {"program": buttons[index], "label": f"ボタン{index+1}"}
        if update_type == "program":
            buttons[index]["program"] = new_value
        elif update_type == "label":
            buttons[index]["label"] = new_value
        elif update_type == "icon":
            buttons[index]["icon"] = new_value
        else:
            return
        self.project_data["buttons"] = buttons
        self.save_config()

    def swap_button_settings(self, source_index, target_index):
        """2つのボタンの program・label・icon 設定を入れ替える。"""
        buttons = self.project_data.get("buttons", [])
        if not (0 <= source_index < len(buttons)
                and 0 <= target_index < len(buttons)):
            return
        buttons[source_index], buttons[target_index] = (
            buttons[target_index], buttons[source_index])
        self.project_data["buttons"] = buttons

        for index in (source_index, target_index):
            config = buttons[index]
            if isinstance(config, str):
                config = {"program": config, "label": f"ボタン{index+1}"}
                buttons[index] = config
            button = self.mini_buttons[index]
            button.program_path = config.get("program", "")
            button.icon_path = config.get("icon", "")
            button.set_button_label(
                config.get("label", f"ボタン{index+1}"))
        self.save_config()


# ============================================================
#  メインウィンドウ
# ============================================================
CONFIG_FILES = {}


def load_json_files():
    """ユーザーデータ + 共有フォルダの *.json を読む。"""
    results = []
    dirs = [USER_DATA_DIR]
    shared = (GLOBAL_SETTINGS.get("shared_project_dir") or "").strip()
    if (shared and os.path.isdir(shared)
            and os.path.abspath(shared) != os.path.abspath(USER_DATA_DIR)):
        dirs.append(shared)

    for d in dirs:
        for file in glob.glob(os.path.join(d, "*.json")):
            try:
                with open(file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                results.append((data, file))
            except json.JSONDecodeError as e:
                print(f"Error reading {file}: {e}")
            except Exception as e:
                print(f"Error reading {file}: {e}")
    return results


def load_workspace():
    if os.path.exists(WORKSPACE_PATH):
        try:
            with open(WORKSPACE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"workspace.json の読み込みに失敗: {e}")
    return {}


class ProjectManager(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Project Bookmark Manager")
        self.setGeometry(100, 100, 520, 460)
        self._loading = True

        self.layout = QVBoxLayout()
        self.layout.setContentsMargins(4, 4, 4, 4)
        self.layout.setSpacing(4)

        self.tab_widget = QTabWidget()
        self.tab_widget.setDocumentMode(True)
        self.tab_widget.setMovable(True)  # タブをドラッグで並べ替え
        self.layout.addWidget(self.tab_widget)

        # 前回のタブ順・選択タブを復元
        ws = load_workspace()
        order = ws.get("order", [])

        entries = [(d, s) for d, s in load_json_files() if "name" in d]

        def rank(entry):
            name = entry[0].get("name")
            return order.index(name) if name in order else len(order)
        entries.sort(key=rank)

        self.project_tabs = {}
        for data, source in entries:
            self._add_tab(data, source)

        current = ws.get("current")
        if current in self.project_tabs:
            self.tab_widget.setCurrentWidget(self.project_tabs[current])

        self.setLayout(self.layout)

        self._loading = False
        self.tab_widget.currentChanged.connect(self._on_workspace_changed)
        self.tab_widget.tabBar().tabMoved.connect(self._on_workspace_changed)

    def _add_tab(self, data, source_path):
        tab = ProjectTab(data, self.add_new_project, self.open_settings,
                         source_path=source_path)
        name = data.get("name", "Unnamed")
        self.tab_widget.addTab(tab, name)
        self.project_tabs[name] = tab
        return tab

    def _on_workspace_changed(self, *args):
        if not self._loading:
            self.save_workspace()

    def save_workspace(self):
        order = [self.tab_widget.tabText(i) for i in range(self.tab_widget.count())]
        idx = self.tab_widget.currentIndex()
        current = self.tab_widget.tabText(idx) if idx >= 0 else ""
        try:
            with open(WORKSPACE_PATH, "w", encoding="utf-8") as f:
                json.dump({"order": order, "current": current}, f,
                          indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"workspace.json の保存に失敗: {e}")

    def add_new_project(self):
        name, ok = QInputDialog.getText(self, "プロジェクト名", "プロジェクト名を入力:")
        if not ok or not name:
            return
        project_data = {
            "name": name,
            "bookmarks": [],
            "buttons": [{"program": "", "label": f"ボタン{i+1}"} for i in range(6)],
            "epSqShot": {"ep1": {"s001": ["c001"]}},
            "project_shot_structure": "N:/{proj}/shots/{ep}/{shot}",
            "project_comp_structure": "N:/{proj}/comp/{ep}/{shot}",
            "variables": {},
        }
        tab = self._add_tab(project_data, None)
        tab.save_config()
        self.tab_widget.setCurrentWidget(tab)
        self.save_workspace()

    def open_settings(self, project_tab):
        dlg = SettingsDialog(self, project_tab)
        dlg.exec_()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    apply_appearance(app)  # ダークモード判定・パレット適用
    apply_font_size(int(GLOBAL_SETTINGS.get("font_size", "9") or 9))
    main_window = ProjectManager()
    main_window.show()
    sys.exit(app.exec_())
