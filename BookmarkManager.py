import sys
import os
import glob
import json
import subprocess
import shlex
import getpass
import configparser
import ctypes
import shutil
import re
import time

# 起動元の識別用（Maya=年バージョン, Houdini=21 等）。省略可。
host_version = sys.argv[1] if len(sys.argv) > 1 else ""

# PySide は環境依存: Maya2025+ / Houdini21 は PySide6、Maya2024以前は PySide2。
# バージョン番号ではなく実際に import できる方を使う（Maya/Houdini 両対応）。
try:
    from PySide6.QtWidgets import (
        QApplication, QWidget, QVBoxLayout, QHBoxLayout, QAbstractItemView,
        QPushButton, QComboBox, QFileDialog, QInputDialog, QMessageBox,
        QTabWidget, QMenu, QColorDialog, QTableWidget, QTableWidgetItem,
        QHeaderView, QDialog, QLineEdit, QSpinBox, QLabel, QFormLayout,
        QDialogButtonBox, QRadioButton, QGroupBox, QToolButton, QFileIconProvider,
        QSizePolicy, QTreeView, QSplitter, QFileSystemModel, QCheckBox,
        QListWidget, QListWidgetItem
    )
    from PySide6.QtGui import QIcon, QColor, QFont, QPalette, QDrag, QCursor
    from PySide6.QtCore import (
        Qt, QSize, QFileInfo, QMimeData, QUrl, QModelIndex,
        QAbstractTableModel, QFileSystemWatcher
    )
    PYSIDE = 6
except ImportError:
    from PySide2.QtWidgets import (
        QApplication, QWidget, QVBoxLayout, QHBoxLayout, QAbstractItemView,
        QPushButton, QComboBox, QFileDialog, QInputDialog, QMessageBox,
        QTabWidget, QMenu, QColorDialog, QTableWidget, QTableWidgetItem,
        QHeaderView, QDialog, QLineEdit, QSpinBox, QLabel, QFormLayout,
        QDialogButtonBox, QRadioButton, QGroupBox, QToolButton, QFileIconProvider,
        QSizePolicy, QTreeView, QSplitter, QFileSystemModel, QCheckBox,
        QListWidget, QListWidgetItem
    )
    from PySide2.QtGui import QIcon, QColor, QFont, QPalette, QDrag, QCursor
    from PySide2.QtCore import (
        Qt, QSize, QFileInfo, QMimeData, QUrl, QModelIndex,
        QAbstractTableModel, QFileSystemWatcher
    )
    PYSIDE = 2

print(f"BookmarkManager: PySide{PYSIDE} (host={host_version or 'n/a'})")


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


# ホスト(mayapy/hython)が自分用に設定する環境変数。これを外部アプリに引き継ぐと
# 相手の内蔵Python/Qtが壊れる（例: Nukeが "No module named 'nuke'"）。起動時に除去する。
_HOST_ENV_STRIP = (
    "PYTHONHOME", "PYTHONPATH", "PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE",
    "PYTHONEXECUTABLE", "PYTHONSTARTUP", "PYTHONNOASSERT",
    "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "QT_QPA_PLATFORM",
    "QML_IMPORT_PATH", "QML2_IMPORT_PATH",
)


def build_env(project_tab):
    """サブプロセス起動用の環境変数を作る"""
    pdata = project_tab.project_data
    env = os.environ.copy()
    # ホスト固有の Python/Qt 変数を除去（外部アプリを素の環境で起動する）
    for key in _HOST_ENV_STRIP:
        env.pop(key, None)
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
        # 高さは固定、幅は可変（ウィンドウを縮めたときにボタンも縮む）
        self.setFixedHeight(32)
        self.setMinimumWidth(36)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
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
        # 枠線＋パディング分を差し引いてボタン内に収める（溢れ防止）
        self.setIconSize(QSize(20 if label else 22, 20 if label else 22))
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
        menu.addSeparator()
        clear_action = menu.addAction("ボタンをクリア")

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

        elif action == clear_action:
            # 初期化: パス・アイコンを消して既定ラベルに戻す
            default_label = f"ボタン{self.button_index + 1}"
            self.program_path = ""
            self.icon_path = ""
            self.set_button_label(default_label)
            if self.update_callback:
                self.update_callback("program", "")
                self.update_callback("icon", "")
                self.update_callback("label", default_label)


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
        elif event.mimeData().hasFormat(SEQ_MIME):
            # 連番のまとめ行はブックマーク登録禁止
            event.ignore()
        elif event.mimeData().hasUrls():
            # --- 外部/ファイルブラウザからのドロップでブックマーク追加 ---
            added = False
            for url in event.mimeData().urls():
                path = url.toLocalFile()
                if path and os.path.exists(path):
                    name = os.path.basename(os.path.normpath(path))
                    stored = os.path.normpath(path).replace("\\", "/")
                    self.append_row(name, stored)
                    added = True
            if added:
                self.parent_tab.commit_bookmarks()
            # コピー扱いにして、ドラッグ元のファイルが移動・削除されないようにする
            event.setDropAction(Qt.CopyAction)
            event.accept()
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
        open_explorer_action = menu.addAction("エクスプローラで開く")
        menu.addSeparator()
        rename_action = menu.addAction("ブックマーク名の変更")
        color_action = menu.addAction("色の設定")
        clear_color_action = menu.addAction("色をクリア")
        set_path_action = menu.addAction("パスを指定")
        edit_path_action = menu.addAction("パスを編集")
        delete_action = menu.addAction("削除")

        action = menu.exec_(self.viewport().mapToGlobal(pos))

        if action == open_explorer_action:
            try:
                self.parent_tab.update_label()
                resolved = resolve_path(path, self.parent_tab)
                if os.path.exists(resolved):
                    reveal_in_explorer(resolved)
                else:
                    QMessageBox.warning(self, "エラー", "フォルダが見つかりません: " + resolved)
            except Exception as e:
                QMessageBox.warning(self, "エラー", f"予期しないエラー: {e}")
        elif action == rename_action:
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
                 source_path=None, show_in_explorer_callback=None,
                 context_changed_callback=None):
        super().__init__()
        self.initializing = True
        self.sort_ascending = True
        self.project_data = project_data
        self.add_project_callback = add_project_callback
        self.open_settings_callback = open_settings_callback
        self.show_in_explorer_callback = show_in_explorer_callback
        self.context_changed_callback = context_changed_callback
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
        self.epsqshot_edit_button = QToolButton()
        self.epsqshot_edit_button.setText("✎")
        self.epsqshot_edit_button.setToolTip("ep / sq / shot を編集")
        self.epsqshot_edit_button.setFixedSize(26, 24)
        self.epsqshot_edit_button.clicked.connect(self.open_epsqshot_editor)
        self.epSqShot_layout.addWidget(self.epsqshot_edit_button)
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
        # ep/sq/shot が変わったらファイルブラウザを追従させる
        if self.context_changed_callback:
            self.context_changed_callback(self)

    def open_epsqshot_editor(self):
        dlg = EpSqShotDialog(self, self.project_data.get("epSqShot", {}))
        if dlg.exec_():
            self.project_data["epSqShot"] = dlg.result_data()
            # コンボを作り直す（信号は抑止して余計な保存/連鎖を防ぐ）
            for c in (self.episode_combo, self.sequence_combo, self.shot_combo):
                c.blockSignals(True)
            self.load_ep_sq_shot()
            for c in (self.episode_combo, self.sequence_combo, self.shot_combo):
                c.blockSignals(False)
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

    def add_bookmark_for(self, path):
        """指定パスをブックマークに追加（ファイルブラウザからの登録用）"""
        stored = os.path.normpath(path).replace("\\", "/")
        default = os.path.basename(os.path.normpath(path)) or stored
        name, ok = QInputDialog.getText(self, "ブックマーク名", "名前を入力:", text=default)
        if not ok or not name:
            return
        self.table.append_row(name, stored)
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
                if self.show_in_explorer_callback:
                    # テンプレート(path)と所有タブも渡し、ep/sq/shot変更で追従できるように
                    self.show_in_explorer_callback(resolved, path, self)
                else:
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


def default_project_data(name):
    """新規/初期プロジェクトの既定値"""
    return {
        "name": name,
        "bookmarks": [],
        "buttons": [{"program": "", "label": f"ボタン{i + 1}"} for i in range(6)],
        "epSqShot": {"ep1": {"s001": ["c001"]}},
        "project_shot_structure": "N:/{proj}/shots/{ep}/{shot}",
        "project_comp_structure": "N:/{proj}/comp/{ep}/{shot}",
        "variables": {},
    }


def load_workspace():
    if os.path.exists(WORKSPACE_PATH):
        try:
            with open(WORKSPACE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"workspace.json の読み込みに失敗: {e}")
    return {}


# ============================================================
#  Windows シェル操作ヘルパー
# ============================================================
def copy_path_forward_slash(path):
    """パスを / 区切りにしてクリップボードへ"""
    text = os.path.normpath(path).replace("\\", "/")
    cb = QApplication.clipboard()
    if cb:
        cb.setText(text)
    return text


def reveal_in_explorer(path):
    """フォルダはそのまま開く。ファイルは選択状態でエクスプローラを開く。"""
    path = os.path.normpath(path)
    if os.path.isdir(path):
        os.startfile(path)
    else:
        subprocess.Popen(f'explorer /select,"{path}"')


def open_with_dialog(path):
    """Windows の「プログラムから開く」ダイアログ"""
    subprocess.Popen(["rundll32.exe", "shell32.dll,OpenAs_RunDLL",
                      os.path.normpath(path)])


def get_sendto_dir():
    appdata = os.environ.get("APPDATA", "")
    return os.path.join(appdata, "Microsoft", "Windows", "SendTo") if appdata else ""


def run_sendto(link_path, target_paths):
    """SendTo のショートカット等に対象ファイルを渡して実行。
    連番など複数ファイルの場合は全パスを引数として渡す。"""
    if isinstance(target_paths, str):
        target_paths = [target_paths]
    params = " ".join(
        f'"{os.path.normpath(p).replace(os.sep, "/")}"' for p in target_paths)
    ctypes.windll.shell32.ShellExecuteW(None, "open", link_path, params, None, 1)


def _sendkeys_escape(text):
    out = []
    for ch in text:
        out.append("{" + ch + "}" if ch in "+^%~(){}[]" else ch)
    return "".join(out)


def open_terminal(dir_path, file_path=None):
    """dir_path に移動したターミナルを開く。file_path があればその名前を入力欄に書き込む。"""
    dir_path = os.path.normpath(dir_path)
    ps_dir = dir_path.replace("'", "''")
    if not file_path:
        script = f"Start-Process -FilePath cmd.exe -ArgumentList '/K','cd /d \"{ps_dir}\"'"
    else:
        keys = _sendkeys_escape('"' + os.path.normpath(file_path) + '"').replace("'", "''")
        script = (
            f"$p = Start-Process -FilePath cmd.exe -ArgumentList '/K','cd /d \"{ps_dir}\"' -PassThru; "
            "Start-Sleep -Milliseconds 350; "
            "$w = New-Object -ComObject WScript.Shell; "
            "[void]$w.AppActivate($p.Id); "
            "Start-Sleep -Milliseconds 120; "
            f"[void]$w.SendKeys('{keys}')"
        )
    subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden",
                      "-Command", script])


def show_properties(path):
    """Windows のプロパティダイアログ"""
    from ctypes import wintypes

    class SHELLEXECUTEINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    SEE_MASK_INVOKEIDLIST = 0x0000000C
    sei = SHELLEXECUTEINFO()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_INVOKEIDLIST
    sei.lpVerb = "properties"
    sei.lpFile = os.path.normpath(path)
    sei.nShow = 1
    ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei))


def unique_path(dir_path, base_name):
    """dir_path 内で衝突しない名前を返す"""
    candidate = os.path.join(dir_path, base_name)
    if not os.path.exists(candidate):
        return candidate
    root, ext = os.path.splitext(base_name)
    i = 2
    while True:
        candidate = os.path.join(dir_path, f"{root} ({i}){ext}")
        if not os.path.exists(candidate):
            return candidate
        i += 1


FO_MOVE, FO_COPY, FO_DELETE = 1, 2, 3
FOF_ALLOWUNDO = 0x0040
FOF_NOCONFIRMATION = 0x0010


def _sh_file_operation(func, sources, dest, flags):
    """SHFileOperation ラッパ（ネイティブのコピー/移動/ごみ箱送り）"""
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    op = SHFILEOPSTRUCTW()
    op.wFunc = func
    op.pFrom = "\0".join(os.path.normpath(p) for p in sources) + "\0\0"
    if dest:
        op.pTo = os.path.normpath(dest) + "\0\0"
    op.fFlags = flags
    res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return res == 0 and not op.fAnyOperationsAborted


def send_to_recycle_bin(paths):
    """ファイル/フォルダをごみ箱へ移動（完全削除ではなく復元可能）"""
    return _sh_file_operation(FO_DELETE, paths, None,
                              FOF_ALLOWUNDO | FOF_NOCONFIRMATION)


def shell_copy_move(sources, dest_dir, move=False):
    """dest_dir へネイティブコピー/移動（進捗・衝突ダイアログはWindows任せ）"""
    return _sh_file_operation(FO_MOVE if move else FO_COPY, sources, dest_dir,
                              FOF_ALLOWUNDO)


ARCHIVE_EXTS = {".zip", ".7z", ".rar", ".tar", ".gz", ".tgz",
                ".bz2", ".xz", ".lzh", ".cab", ".iso"}


def is_archive(path):
    return os.path.isfile(path) and os.path.splitext(path)[1].lower() in ARCHIVE_EXTS


def find_7zip():
    for p in (r"C:\Program Files\7-Zip\7z.exe",
              r"C:\Program Files (x86)\7-Zip\7z.exe"):
        if os.path.exists(p):
            return p
    return shutil.which("7z") or shutil.which("7za")


def extract_archive_here(archive_path):
    """アーカイブと同じ場所に、アーカイブ名のフォルダを作って展開する"""
    d = os.path.dirname(archive_path)
    name = os.path.basename(archive_path)
    if name.lower().endswith(".tar.gz"):
        base = name[:-7]
    else:
        base = os.path.splitext(name)[0]
    dest = unique_path(d, base)
    os.makedirs(dest, exist_ok=True)
    ext = os.path.splitext(archive_path)[1].lower()
    try:
        if ext == ".zip":
            import zipfile
            with zipfile.ZipFile(archive_path) as z:
                z.extractall(dest)
        elif ext in (".tar", ".gz", ".tgz", ".bz2", ".xz"):
            import tarfile
            with tarfile.open(archive_path) as t:
                t.extractall(dest)
        else:  # .7z .rar .lzh .cab .iso など → 7-Zip
            seven = find_7zip()
            if not seven:
                raise RuntimeError("この形式の解凍には 7-Zip が必要です")
            subprocess.run([seven, "x", "-y", f"-o{dest}", archive_path], check=True)
    except Exception:
        try:  # 失敗時は空フォルダを掃除
            if os.path.isdir(dest) and not os.listdir(dest):
                os.rmdir(dest)
        except OSError:
            pass
        raise
    return dest


class _FileTreeView(QTreeView):
    """Deleteキー削除 + 外部ファイルのドロップ（コピー/移動）に対応したツリービュー"""
    def __init__(self, panel):
        super().__init__()
        self.panel = panel

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete:
            self.panel.delete_selected()
        else:
            super().keyPressEvent(event)

    def startDrag(self, supportedActions):
        # ツリーからのドラッグは必ずコピー扱いにする
        # （移動扱いだとドロップ成功後に元ファイルが削除されうるため）
        super().startDrag(Qt.CopyAction)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        md = event.mimeData()
        if not md.hasUrls():
            event.ignore()
            return
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        index = self.indexAt(pos)
        if index.isValid():
            p = self.panel.model.path_at(index)
            dest = p if os.path.isdir(p) else os.path.dirname(p)
        else:
            dest = self.panel.current_root
        sources = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
        sources = [s for s in sources if s]
        if sources and dest:
            self.panel.handle_external_drop(sources, dest)
        event.acceptProposedAction()


# ============================================================
#  連番まとめ対応のディレクトリモデル
# ============================================================
# name.0179.exr / name_0179.exr 形式（.数字.ext または _数字.ext）
SEQ_RE = re.compile(r"^(.*)([._])(\d+)(\.[^.]+)$")

# 連番のまとめ行をブックマーク表へドロップさせないための目印
SEQ_MIME = "application/x-bookmarkmanager-seq"


def human_size(n):
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    f = float(n)
    i = 0
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    return f"{int(f)} B" if i == 0 else f"{f:.1f} {units[i]}"


def format_mtime(mtime):
    return time.strftime("%Y/%m/%d %H:%M", time.localtime(mtime))


_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_SYSTEM = 0x4


def _is_hidden(name, st):
    """Windowsの隠し/システム属性、または .始まりの名前を隠しとみなす"""
    attr = getattr(st, "st_file_attributes", 0)
    if attr & (_FILE_ATTRIBUTE_HIDDEN | _FILE_ATTRIBUTE_SYSTEM):
        return True
    return name.startswith(".")


class _Entry:
    __slots__ = ("display", "paths", "is_dir", "is_seq", "size", "mtime",
                 "sort_name", "ext")

    def __init__(self, display, paths, is_dir, is_seq, size, mtime, sort_name, ext):
        self.display = display
        self.paths = paths
        self.is_dir = is_dir
        self.is_seq = is_seq
        self.size = size
        self.mtime = mtime
        self.sort_name = sort_name
        self.ext = ext


class SeqFileModel(QAbstractTableModel):
    """カレントディレクトリを表示。seq有効時は連番を1行にまとめる。"""
    HEADERS = ["名前", "サイズ", "種類", "更新日時"]
    COL_NAME, COL_SIZE, COL_TYPE, COL_MTIME = 0, 1, 2, 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dir = ""
        self._entries = []
        self._seq_enabled = True
        self._show_hidden = False
        self._icon_provider = QFileIconProvider()
        self._icon_cache = {}
        self._type_cache = {}
        self._sort_col = 0
        self._sort_order = Qt.AscendingOrder

    # ---- 設定 ----
    def set_seq_enabled(self, on):
        if self._seq_enabled != bool(on):
            self._seq_enabled = bool(on)
            self.reload()

    def set_show_hidden(self, on):
        if self._show_hidden != bool(on):
            self._show_hidden = bool(on)
            self.reload()

    def set_directory(self, path):
        self._dir = path
        self.reload()

    def reload(self):
        self.beginResetModel()
        self._entries = self._scan()
        self._apply_sort()
        self.endResetModel()

    # ---- 走査 ----
    def _scan(self):
        d = self._dir
        try:
            names = os.listdir(d)
        except OSError:
            return []
        dirs, files = [], []
        for n in names:
            full = os.path.join(d, n)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if not self._show_hidden and _is_hidden(n, st):
                continue
            if os.path.isdir(full):
                dirs.append(_Entry(n, [full], True, False, 0, st.st_mtime, n.lower(), ""))
            else:
                files.append((n, full, st))

        file_entries = []
        if self._seq_enabled:
            # 桁数はキーに含めない。ゼロ埋めの有無で padding を後から決めるので、
            # 0-9 と 10-52 のような非ゼロ埋め連番も1つにまとまる。
            groups, singles = {}, []
            for n, full, st in files:
                m = SEQ_RE.match(n)
                if m:
                    prefix, sep, frame, ext = m.groups()
                    groups.setdefault((prefix, sep, ext), []).append(
                        (int(frame), full, st, frame))
                else:
                    singles.append((n, full, st))
            for (prefix, sep, ext), members in groups.items():
                if len(members) >= 2:
                    members.sort(key=lambda x: x[0])
                    frames = [mm[0] for mm in members]
                    paths = [mm[1] for mm in members]
                    frame_strs = [mm[3] for mm in members]
                    # 先頭ゼロがあればゼロ埋め連番 → その桁数、無ければ非ゼロ埋め(#1個)
                    padded = any(len(fr) > 1 and fr[0] == "0" for fr in frame_strs)
                    pad = max(len(fr) for fr in frame_strs) if padded else 1
                    total = sum(mm[2].st_size for mm in members)
                    mtime = max(mm[2].st_mtime for mm in members)
                    disp = f"{prefix}{sep}{'#' * pad}{ext} {frames[0]}-{frames[-1]}"
                    file_entries.append(_Entry(
                        disp, paths, False, True, total, mtime,
                        (prefix + sep + ext).lower(), ext.lower()))
                else:
                    _, full, st, _fr = members[0]
                    n = os.path.basename(full)
                    file_entries.append(_Entry(
                        n, [full], False, False, st.st_size, st.st_mtime,
                        n.lower(), os.path.splitext(n)[1].lower()))
            for n, full, st in singles:
                file_entries.append(_Entry(
                    n, [full], False, False, st.st_size, st.st_mtime,
                    n.lower(), os.path.splitext(n)[1].lower()))
        else:
            for n, full, st in files:
                file_entries.append(_Entry(
                    n, [full], False, False, st.st_size, st.st_mtime,
                    n.lower(), os.path.splitext(n)[1].lower()))
        return dirs + file_entries

    def _apply_sort(self):
        col, rev = self._sort_col, (self._sort_order == Qt.DescendingOrder)

        def key(e):
            if col == self.COL_SIZE:
                return e.size
            if col == self.COL_MTIME:
                return e.mtime
            if col == self.COL_TYPE:
                return self._type_for(e).lower()
            return e.sort_name

        dirs = [e for e in self._entries if e.is_dir]
        files = [e for e in self._entries if not e.is_dir]
        dirs.sort(key=lambda e: e.sort_name, reverse=rev)
        files.sort(key=key, reverse=rev)
        self._entries = dirs + files

    # ---- QAbstractTableModel ----
    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._entries)

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.HEADERS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        e = self._entries[index.row()]
        col = index.column()
        if role in (Qt.DisplayRole, Qt.EditRole) and col == self.COL_NAME:
            return e.display
        if role == Qt.DisplayRole:
            if col == self.COL_SIZE:
                return "" if e.is_dir else human_size(e.size)
            if col == self.COL_TYPE:
                return self._type_for(e)
            if col == self.COL_MTIME:
                return format_mtime(e.mtime)
        elif role == Qt.DecorationRole and col == self.COL_NAME:
            return self._icon_for(e)
        elif role == Qt.TextAlignmentRole and col == self.COL_SIZE:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None

    def flags(self, index):
        base = Qt.ItemIsSelectable | Qt.ItemIsEnabled
        if index.isValid():
            base |= Qt.ItemIsDragEnabled
            e = self._entries[index.row()]
            # 名前列は（連番まとめ行を除き）インライン改名を許可
            if index.column() == self.COL_NAME and not e.is_seq:
                base |= Qt.ItemIsEditable
        return base

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.EditRole or not index.isValid() or index.column() != self.COL_NAME:
            return False
        e = self._entries[index.row()]
        if e.is_seq:
            return False
        new = str(value).strip()
        src = e.paths[0]
        old = os.path.basename(os.path.normpath(src))
        if not new or new == old:
            return False
        dst = os.path.join(os.path.dirname(src), new)
        if os.path.exists(dst):
            QMessageBox.warning(None, "エラー", f"既に存在します: {new}")
            return False
        try:
            os.rename(src, dst)
        except OSError as ex:
            QMessageBox.warning(None, "エラー", f"名前を変更できません: {ex}")
            return False
        # モデルをその場で更新（リセットせず編集確定と干渉しないように）
        e.paths[0] = dst
        e.display = new
        e.sort_name = new.lower()
        if not e.is_dir:
            e.ext = os.path.splitext(new)[1].lower()
        self.dataChanged.emit(index, self.index(index.row(), self.columnCount() - 1))
        return True

    def sort(self, column, order=Qt.AscendingOrder):
        self._sort_col = column
        self._sort_order = order
        self.beginResetModel()
        self._apply_sort()
        self.endResetModel()

    def mimeTypes(self):
        return ["text/uri-list"]

    def supportedDragActions(self):
        return Qt.CopyAction

    def mimeData(self, indexes):
        urls, seen = [], set()
        has_seq = False
        for idx in indexes:
            if idx.column() != 0:
                continue
            e = self._entries[idx.row()]
            if e.is_seq:
                has_seq = True
            for p in e.paths:
                if p not in seen:
                    seen.add(p)
                    urls.append(QUrl.fromLocalFile(p))
        md = QMimeData()
        md.setUrls(urls)
        if has_seq:
            # 連番はブックマーク登録禁止の目印
            md.setData(SEQ_MIME, b"1")
        return md

    def _icon_for(self, e):
        key = "<dir>" if e.is_dir else (e.ext or "<file>")
        ic = self._icon_cache.get(key)
        if ic is None:
            if e.is_dir:
                ic = self._icon_provider.icon(QFileIconProvider.Folder)
            else:
                ic = self._icon_provider.icon(QFileInfo(e.paths[0]))
            self._icon_cache[key] = ic
        return ic

    def _type_for(self, e):
        key = "<dir>" if e.is_dir else (e.ext or "<file>")
        t = self._type_cache.get(key)
        if t is None:
            t = self._icon_provider.type(QFileInfo(e.paths[0])) or ""
            self._type_cache[key] = t
        return t

    # ---- ヘルパ ----
    def entry_at(self, index):
        if not index.isValid():
            return None
        return self._entries[index.row()]

    def path_at(self, index):
        e = self.entry_at(index)
        return e.paths[0] if e else ""

    def paths_at(self, index):
        e = self.entry_at(index)
        return list(e.paths) if e else []


class ExplorerPanel(QWidget):
    """右側に埋め込むファイルブラウザ（名前/サイズ/更新日時、連番まとめ対応）"""
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # 上部バー: 上へ / パス / OSで開く / 閉じる
        bar = QHBoxLayout()
        bar.setSpacing(4)
        self.up_button = QToolButton()
        self.up_button.setText("↑")
        self.up_button.setToolTip("上のフォルダへ")
        self.up_button.setFixedSize(26, 24)
        self.path_edit = QLineEdit()
        self.path_edit.setToolTip("パスを入力して Enter で移動")
        self.open_os_button = QToolButton()
        self.open_os_button.setText("⧉")
        self.open_os_button.setToolTip("エクスプローラで開く")
        self.open_os_button.setFixedSize(26, 24)
        self.close_button = QToolButton()
        self.close_button.setText("✕")
        self.close_button.setToolTip("パネルを閉じる")
        self.close_button.setFixedSize(26, 24)
        self.hidden_check = QCheckBox("show hidden")
        self.hidden_check.setToolTip("隠しファイルを表示")
        self.seq_check = QCheckBox("seq")
        self.seq_check.setChecked(True)
        self.seq_check.setToolTip("連番ファイルを 名前.####.ext のように1行にまとめて表示")
        bar.addWidget(self.up_button)
        bar.addWidget(self.path_edit)
        bar.addWidget(self.hidden_check)
        bar.addWidget(self.seq_check)
        bar.addWidget(self.open_os_button)
        bar.addWidget(self.close_button)
        layout.addLayout(bar)

        # ファイルリスト（連番まとめ対応モデル）
        self.model = SeqFileModel(self)
        self.tree = _FileTreeView(self)
        self.tree.setModel(self.model)
        self.tree.setSortingEnabled(True)
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        # ゆっくり2度クリック(選択済みをクリック) / F2 でインライン改名
        self.tree.setEditTriggers(
            QAbstractItemView.SelectedClicked | QAbstractItemView.EditKeyPressed)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        # 外部からのドロップを受け付け、かつ自身もドラッグ元にする
        self.tree.setAcceptDrops(True)
        self.tree.setDragEnabled(True)
        self.tree.setDropIndicatorShown(True)
        self.tree.setDragDropMode(QAbstractItemView.DragDrop)
        self.tree.setDefaultDropAction(Qt.CopyAction)
        header = self.tree.header()
        header.setSectionResizeMode(QHeaderView.Interactive)  # 列を手動リサイズ可能に
        header.setStretchLastSection(False)
        self.tree.setColumnWidth(0, 240)  # 名前
        self.tree.setColumnWidth(1, 80)   # サイズ
        self.tree.setColumnWidth(2, 110)  # 種類
        self.tree.setColumnWidth(3, 130)  # 更新日時
        self.tree.doubleClicked.connect(self._on_double_clicked)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.open_context_menu)
        layout.addWidget(self.tree)

        # 下部ステータスバー: 「12個の項目 ｜ 1個の項目を選択 23.4 KB」
        self.status_label = QLabel("")
        self.status_label.setContentsMargins(2, 0, 2, 0)
        layout.addWidget(self.status_label)

        self.current_root = ""
        self.add_bookmark_callback = None  # ProjectManager 側で設定
        self.watcher = QFileSystemWatcher(self)
        self.watcher.directoryChanged.connect(lambda _p: self.model.reload())
        self.up_button.clicked.connect(self.go_up)
        self.open_os_button.clicked.connect(self.open_in_os)
        self.seq_check.toggled.connect(self.model.set_seq_enabled)
        self.hidden_check.toggled.connect(self.model.set_show_hidden)
        self.path_edit.returnPressed.connect(self._on_path_entered)
        self.tree.selectionModel().selectionChanged.connect(
            lambda *_: self._update_status())
        self.model.modelReset.connect(self._update_status)
        self._update_status()
        # close_button は ProjectManager 側で接続

    def _selected_rows(self):
        return sorted({idx.row() for idx in self.tree.selectedIndexes()})

    def _update_status(self):
        total = self.model.rowCount()
        rows = self._selected_rows()
        if rows:
            size = 0
            for r in rows:
                e = self.model.entry_at(self.model.index(r, 0))
                if e:
                    size += e.size
            self.status_label.setText(
                f"{total}個の項目 ｜ {len(rows)}個の項目を選択 {human_size(size)}")
        else:
            self.status_label.setText(f"{total}個の項目")

    def set_root(self, path):
        self.current_root = path
        self.model.set_directory(path)
        # パス表示は常に / 区切り
        self.path_edit.setText(os.path.normpath(path).replace("\\", "/"))
        self.tree.scrollToTop()
        old = self.watcher.directories()
        if old:
            self.watcher.removePaths(old)
        if os.path.isdir(path):
            self.watcher.addPath(path)

    def _on_path_entered(self):
        text = self.path_edit.text().strip().strip('"')
        if not text:
            return
        p = os.path.normpath(text)  # \ でも / でも受け付ける
        if os.path.isdir(p):
            self.set_root(p)
        elif os.path.isfile(p):
            self.set_root(os.path.dirname(p))  # ファイル指定ならその親へ
        else:
            QMessageBox.warning(self, "エラー", "パスが見つかりません:\n" + text)
            # 表示を現在地へ戻す（/ 区切り）
            if self.current_root:
                self.path_edit.setText(
                    os.path.normpath(self.current_root).replace("\\", "/"))

    def go_up(self):
        if not self.current_root:
            return
        parent = os.path.dirname(os.path.normpath(self.current_root))
        if parent and os.path.isdir(parent) and parent != self.current_root:
            self.set_root(parent)

    def open_in_os(self):
        if self.current_root and os.path.exists(self.current_root):
            os.startfile(self.current_root)

    def _on_double_clicked(self, index):
        e = self.model.entry_at(index)
        if not e:
            return
        if e.is_dir:
            self.set_root(e.paths[0])     # フォルダは中へ潜る
        else:
            try:
                os.startfile(e.paths[0])  # ファイル/連番は先頭フレームを起動
            except Exception as ex:
                QMessageBox.warning(self, "エラー", f"開けませんでした: {ex}")

    def selected_paths(self):
        seen, out = set(), []
        for idx in self.tree.selectedIndexes():
            if idx.column() != 0:
                continue
            for p in self.model.paths_at(idx):  # 連番は全フレームに展開
                if p and p not in seen:
                    seen.add(p)
                    out.append(p)
        return out

    def delete_selected(self):
        paths = self.selected_paths()
        if not paths:
            return
        if len(paths) == 1:
            msg = "次の項目をごみ箱へ移動しますか？\n\n" + os.path.normpath(paths[0])
        else:
            msg = f"{len(paths)} 個の項目をごみ箱へ移動しますか？"
        reply = QMessageBox.question(
            self, "削除の確認", msg,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        try:
            if not send_to_recycle_bin(paths):
                QMessageBox.warning(self, "エラー", "削除に失敗しました。")
        except Exception as e:
            QMessageBox.warning(self, "エラー", f"削除に失敗しました: {e}")
        self.model.reload()

    def handle_external_drop(self, sources, dest_dir):
        """エクスプローラからのドロップ時に コピー/移動 を選ばせる"""
        menu = QMenu(self)
        a_copy = menu.addAction("ここにコピー")
        a_move = menu.addAction("ここに移動")
        menu.addSeparator()
        menu.addAction("キャンセル")
        chosen = menu.exec_(QCursor.pos())
        try:
            if chosen == a_copy:
                shell_copy_move(sources, dest_dir, move=False)
            elif chosen == a_move:
                shell_copy_move(sources, dest_dir, move=True)
        except Exception as e:
            QMessageBox.warning(self, "エラー", f"操作に失敗しました: {e}")
        self.model.reload()

    # ---------- 切り取り / コピー / 貼り付け ----------
    def clipboard_set_files(self, paths, cut):
        if not paths:
            return
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(p) for p in paths])
        # Windows の切り取り/コピー判別: DROPEFFECT_MOVE=2 / DROPEFFECT_COPY=1
        effect = 2 if cut else 1
        md.setData("Preferred DropEffect", effect.to_bytes(4, "little"))
        QApplication.clipboard().setMimeData(md)

    def clipboard_has_files(self):
        md = QApplication.clipboard().mimeData()
        return bool(md and md.hasUrls())

    def paste_into(self, dest_dir):
        md = QApplication.clipboard().mimeData()
        if not (md and md.hasUrls() and os.path.isdir(dest_dir)):
            return
        sources = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
        sources = [s for s in sources if s and os.path.exists(s)]
        if not sources:
            return
        cut = False
        if md.hasFormat("Preferred DropEffect"):
            raw = bytes(md.data("Preferred DropEffect"))
            if len(raw) >= 4 and int.from_bytes(raw[:4], "little") == 2:
                cut = True
        try:
            shell_copy_move(sources, dest_dir, move=cut)
            if cut:
                QApplication.clipboard().clear()
        except Exception as e:
            QMessageBox.warning(self, "エラー", f"貼り付けに失敗しました: {e}")
        self.model.reload()

    # ---------- 右クリックメニュー ----------
    def open_context_menu(self, pos):
        index = self.tree.indexAt(pos)
        # 未選択の項目を右クリックしたらその項目を選択（複数選択中はそのまま）
        if index.isValid() and not self.tree.selectionModel().isSelected(index):
            self.tree.setCurrentIndex(index)
        entry = self.model.entry_at(index) if index.isValid() else None
        is_item = entry is not None
        is_dir = bool(entry and entry.is_dir)
        is_seq = bool(entry and entry.is_seq)
        item_path = entry.paths[0] if entry else ""
        target = item_path if is_item else self.current_root
        if not target:
            return

        menu = QMenu(self)
        act_rename = act_openwith = act_extract = None
        sendto_actions = {}

        if is_item:
            if not is_seq:  # 連番のまとめ行はリネーム対象外
                act_rename = menu.addAction("名前を変更")
            if not is_dir:
                act_openwith = menu.addAction("プログラムから開く")
            if is_archive(item_path):
                act_extract = menu.addAction("その場で解凍")
            send_menu = menu.addMenu("送る")
            sendto_dir = get_sendto_dir()
            entries = []
            if sendto_dir and os.path.isdir(sendto_dir):
                entries = sorted(n for n in os.listdir(sendto_dir)
                                 if n.lower() != "desktop.ini")
            if entries:
                icon_provider = QFileIconProvider()
                for name in entries:
                    label = os.path.splitext(name)[0]
                    full = os.path.join(sendto_dir, name)
                    a = send_menu.addAction(label)
                    ic = icon_provider.icon(QFileInfo(full))
                    if not ic.isNull():
                        a.setIcon(ic)
                    sendto_actions[a] = full
            else:
                send_menu.setEnabled(False)
            menu.addSeparator()

        act_cut = menu.addAction("切り取り") if is_item else None
        act_copyfile = menu.addAction("コピー") if is_item else None
        act_paste = menu.addAction("貼り付け")
        act_paste.setEnabled(self.clipboard_has_files())
        menu.addSeparator()

        act_bookmark = menu.addAction("ブックマークに追加")
        menu.addSeparator()
        act_copy = menu.addAction("パスをコピー")
        act_explorer = menu.addAction("エクスプローラで開く")
        act_terminal = menu.addAction("ターミナルで開く")
        menu.addSeparator()
        act_newfolder = menu.addAction("フォルダを作成")
        act_newtext = menu.addAction("テキストを作成")
        menu.addSeparator()
        act_delete = menu.addAction("削除") if is_item else None
        if is_item:
            menu.addSeparator()
        act_props = menu.addAction("プロパティ")

        action = menu.exec_(self.tree.viewport().mapToGlobal(pos))
        if action is None:
            return

        try:
            if action == act_rename:
                self._rename(item_path)
            elif action is not None and action == act_openwith:
                open_with_dialog(item_path)
            elif act_extract is not None and action == act_extract:
                extract_archive_here(item_path)
                self.model.reload()
            elif action in sendto_actions:
                # 連番は全フレームを引数として渡す
                run_sendto(sendto_actions[action],
                           self.selected_paths() or [item_path])
            elif act_cut is not None and action == act_cut:
                self.clipboard_set_files(self.selected_paths(), cut=True)
            elif act_copyfile is not None and action == act_copyfile:
                self.clipboard_set_files(self.selected_paths(), cut=False)
            elif action == act_paste:
                dest = item_path if (is_item and is_dir) else self.current_root
                self.paste_into(dest)
            elif action == act_bookmark:
                if self.add_bookmark_callback:
                    self.add_bookmark_callback(target)
            elif action == act_copy:
                copy_path_forward_slash(target)
            elif action == act_explorer:
                reveal_in_explorer(target)
            elif action == act_terminal:
                if is_item and not is_dir:
                    open_terminal(os.path.dirname(item_path), item_path)
                else:
                    open_terminal(target)
            elif action == act_newfolder:
                self._create_new("フォルダ")
            elif action == act_newtext:
                self._create_new("テキスト")
            elif act_delete is not None and action == act_delete:
                self.delete_selected()
            elif action == act_props:
                show_properties(target)
        except Exception as e:
            QMessageBox.warning(self, "エラー", f"操作に失敗しました: {e}")

    def _rename(self, path):
        old = os.path.basename(os.path.normpath(path))
        new, ok = QInputDialog.getText(self, "名前を変更", "新しい名前:", text=old)
        if ok and new and new != old:
            os.rename(path, os.path.join(os.path.dirname(path), new))
            self.model.reload()

    def _create_new(self, kind):
        base_dir = self.current_root
        if not os.path.isdir(base_dir):
            return
        if kind == "フォルダ":
            default = "新しいフォルダ"
            new, ok = QInputDialog.getText(self, "フォルダを作成", "フォルダ名:", text=default)
            if ok and new:
                os.makedirs(unique_path(base_dir, new), exist_ok=False)
                self.model.reload()
        else:
            default = "新しいテキスト.txt"
            new, ok = QInputDialog.getText(self, "テキストを作成", "ファイル名:", text=default)
            if ok and new:
                if not os.path.splitext(new)[1]:
                    new += ".txt"
                open(unique_path(base_dir, new), "w", encoding="utf-8").close()
                self.model.reload()


def _reorder_dict(d, key, delta):
    keys = list(d.keys())
    if key not in keys:
        return d
    i = keys.index(key)
    j = i + delta
    if 0 <= j < len(keys):
        keys[i], keys[j] = keys[j], keys[i]
        return {k: d[k] for k in keys}
    return d


def _rename_dict_key(d, old, new):
    return {(new if k == old else k): v for k, v in d.items()}


def _reorder_list(lst, i, delta):
    j = i + delta
    if 0 <= i < len(lst) and 0 <= j < len(lst):
        lst[i], lst[j] = lst[j], lst[i]
        return j
    return i


class EpSqShotDialog(QDialog):
    """ep / sq / shot を3カラムで 追加・削除・並べ替え・改名 する"""
    def __init__(self, parent, epsqshot):
        super().__init__(parent)
        self.setWindowTitle("ep / sq / shot の編集")
        self.setMinimumSize(560, 420)
        self.data = json.loads(json.dumps(epsqshot or {}))  # 編集用ディープコピー
        self._loading = False

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        hint = QLabel("列の項目はダブルクリックで名前変更。ボタンで 追加/削除/並べ替え。")
        hint.setWordWrap(True)
        root.addWidget(hint)

        cols = QHBoxLayout()
        cols.setSpacing(8)
        root.addLayout(cols)
        self.ep_list, ep_box = self._make_column(
            "ep", self.add_ep, self.del_ep,
            lambda: self.move_ep(-1), lambda: self.move_ep(1), self.rename_ep)
        self.sq_list, sq_box = self._make_column(
            "sq", self.add_sq, self.del_sq,
            lambda: self.move_sq(-1), lambda: self.move_sq(1), self.rename_sq)
        self.shot_list, shot_box = self._make_column(
            "shot", self.add_shot, self.del_shot,
            lambda: self.move_shot(-1), lambda: self.move_shot(1), self.rename_shot)
        cols.addLayout(ep_box)
        cols.addLayout(sq_box)
        cols.addLayout(shot_box)

        self.ep_list.currentRowChanged.connect(lambda _r: self._on_ep_changed())
        self.sq_list.currentRowChanged.connect(lambda _r: self._on_sq_changed())

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.refresh_ep()

    def _make_column(self, title, on_add, on_del, on_up, on_down, on_rename):
        box = QVBoxLayout()
        box.setSpacing(4)
        box.addWidget(QLabel(title))
        lst = QListWidget()
        lst.setSelectionMode(QListWidget.SingleSelection)
        lst.itemDoubleClicked.connect(lambda _it: on_rename())
        box.addWidget(lst)
        row = QHBoxLayout()
        row.setSpacing(2)
        for text, tip, fn in (("＋", "追加", on_add), ("－", "削除", on_del),
                              ("↑", "上へ", on_up), ("↓", "下へ", on_down),
                              ("✎", "名前変更", on_rename)):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setFixedSize(26, 24)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch()
        box.addLayout(row)
        return lst, box

    # ---- 選択の取得 ----
    def current_ep(self):
        it = self.ep_list.currentItem()
        return it.text() if it else None

    def current_sq(self):
        it = self.sq_list.currentItem()
        return it.text() if it else None

    def _shots(self):
        ep, sq = self.current_ep(), self.current_sq()
        if ep and sq:
            return self.data.get(ep, {}).get(sq)
        return None

    # ---- 再描画（ep→sq→shot 連鎖）----
    def _on_ep_changed(self):
        if not self._loading:
            self.refresh_sq()

    def _on_sq_changed(self):
        if not self._loading:
            self.refresh_shot()

    def _select_name(self, lst, name):
        if name is not None:
            items = lst.findItems(name, Qt.MatchExactly)
            if items:
                lst.setCurrentItem(items[0])
                return
        if lst.count() > 0:
            lst.setCurrentRow(0)

    def refresh_ep(self, select=None):
        self._loading = True
        self.ep_list.clear()
        for k in self.data:
            self.ep_list.addItem(k)
        self._select_name(self.ep_list, select)
        self._loading = False
        self.refresh_sq()

    def refresh_sq(self, select=None):
        self._loading = True
        self.sq_list.clear()
        ep = self.current_ep()
        if ep and isinstance(self.data.get(ep), dict):
            for k in self.data[ep]:
                self.sq_list.addItem(k)
        self._select_name(self.sq_list, select)
        self._loading = False
        self.refresh_shot()

    def refresh_shot(self, select_row=None):
        self._loading = True
        self.shot_list.clear()
        shots = self._shots()
        if shots:
            for s in shots:
                self.shot_list.addItem(s)
        if select_row is not None and 0 <= select_row < self.shot_list.count():
            self.shot_list.setCurrentRow(select_row)
        elif self.shot_list.count() > 0:
            self.shot_list.setCurrentRow(0)
        self._loading = False

    # ---- ep 操作 ----
    def add_ep(self):
        name, ok = QInputDialog.getText(self, "ep 追加", "ep 名:")
        if ok and name and name not in self.data:
            self.data[name] = {}
            self.refresh_ep(select=name)

    def del_ep(self):
        ep = self.current_ep()
        if ep and ep in self.data:
            del self.data[ep]
            self.refresh_ep()

    def move_ep(self, delta):
        ep = self.current_ep()
        if ep:
            self.data = _reorder_dict(self.data, ep, delta)
            self.refresh_ep(select=ep)

    def rename_ep(self):
        ep = self.current_ep()
        if not ep:
            return
        new, ok = QInputDialog.getText(self, "ep 名の変更", "新しい名前:", text=ep)
        if ok and new and new != ep and new not in self.data:
            self.data = _rename_dict_key(self.data, ep, new)
            self.refresh_ep(select=new)

    # ---- sq 操作 ----
    def add_sq(self):
        ep = self.current_ep()
        if not ep:
            QMessageBox.information(self, "確認", "先に ep を選択/追加してください。")
            return
        name, ok = QInputDialog.getText(self, "sq 追加", "sq 名:")
        if ok and name and name not in self.data[ep]:
            self.data[ep][name] = []
            self.refresh_sq(select=name)

    def del_sq(self):
        ep, sq = self.current_ep(), self.current_sq()
        if ep and sq and sq in self.data[ep]:
            del self.data[ep][sq]
            self.refresh_sq()

    def move_sq(self, delta):
        ep, sq = self.current_ep(), self.current_sq()
        if ep and sq:
            self.data[ep] = _reorder_dict(self.data[ep], sq, delta)
            self.refresh_sq(select=sq)

    def rename_sq(self):
        ep, sq = self.current_ep(), self.current_sq()
        if not (ep and sq):
            return
        new, ok = QInputDialog.getText(self, "sq 名の変更", "新しい名前:", text=sq)
        if ok and new and new != sq and new not in self.data[ep]:
            self.data[ep] = _rename_dict_key(self.data[ep], sq, new)
            self.refresh_sq(select=new)

    # ---- shot 操作 ----
    def add_shot(self):
        shots = self._shots()
        if shots is None:
            QMessageBox.information(self, "確認", "先に ep と sq を選択/追加してください。")
            return
        name, ok = QInputDialog.getText(self, "shot 追加", "shot 名:")
        if ok and name:
            shots.append(name)
            self.refresh_shot(select_row=len(shots) - 1)

    def del_shot(self):
        shots = self._shots()
        i = self.shot_list.currentRow()
        if shots is not None and 0 <= i < len(shots):
            del shots[i]
            self.refresh_shot(select_row=min(i, len(shots) - 1))

    def move_shot(self, delta):
        shots = self._shots()
        i = self.shot_list.currentRow()
        if shots is not None and 0 <= i < len(shots):
            j = _reorder_list(shots, i, delta)
            self.refresh_shot(select_row=j)

    def rename_shot(self):
        shots = self._shots()
        i = self.shot_list.currentRow()
        if shots is not None and 0 <= i < len(shots):
            new, ok = QInputDialog.getText(
                self, "shot 名の変更", "新しい名前:", text=shots[i])
            if ok and new:
                shots[i] = new
                self.refresh_shot(select_row=i)

    def result_data(self):
        return self.data


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
        self.tab_widget.tabBar().setContextMenuPolicy(Qt.CustomContextMenu)
        self.tab_widget.tabBar().customContextMenuRequested.connect(
            self.open_project_context_menu)

        # 左=ブックマーク、右=埋め込みエクスプローラ（初期は非表示で省スペース）
        self.explorer = ExplorerPanel()
        self.explorer.close_button.clicked.connect(self.hide_explorer)
        self.explorer.add_bookmark_callback = self.add_bookmark_from_path
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(self.tab_widget)
        self.splitter.addWidget(self.explorer)
        self.splitter.setCollapsible(0, False)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.explorer.hide()
        self.layout.addWidget(self.splitter)

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

        # まっさらな環境（プロジェクトjsonが1つも無い）なら初期プロジェクトを作る
        if not self.project_tabs:
            data = default_project_data("MyProject")
            home = os.path.expanduser("~").replace("\\", "/")
            data["bookmarks"] = [
                f"Home | {home}",
                f"Desktop | {home}/Desktop",
                f"Downloads | {home}/Downloads",
            ]
            tab = self._add_tab(data, None)
            tab.save_config()  # ~/.BookmarkManager/MyProject.json を作成

        current = ws.get("current")
        if current in self.project_tabs:
            self.tab_widget.setCurrentWidget(self.project_tabs[current])

        self.setLayout(self.layout)

        self._loading = False
        self.tab_widget.currentChanged.connect(self._on_workspace_changed)
        self.tab_widget.tabBar().tabMoved.connect(self._on_workspace_changed)

    def _add_tab(self, data, source_path):
        tab = ProjectTab(data, self.add_new_project, self.open_settings,
                         source_path=source_path,
                         show_in_explorer_callback=self.show_in_explorer,
                         context_changed_callback=self.refresh_explorer_for)
        name = data.get("name", "Unnamed")
        self.tab_widget.addTab(tab, name)
        self.project_tabs[name] = tab
        return tab

    def show_in_explorer(self, resolved_path, template=None, owner_tab=None):
        """ブックマークのダブルクリックで右側パネルにフォルダを表示"""
        if os.path.isdir(resolved_path):
            # ep/sq/shot 変更で追従できるようテンプレートと所有タブを記憶
            self._explorer_template = template
            self._explorer_owner = owner_tab
            was_hidden = self.explorer.isHidden()
            self.explorer.setVisible(True)
            self.explorer.set_root(resolved_path)
            if was_hidden:
                # 初回表示時はパネル分だけウィンドウを広げる
                extra = 480
                self.resize(self.width() + extra, max(self.height(), 500))
                left = max(self.tab_widget.width(), 260)
                self.splitter.setSizes([left, extra])
        else:
            # フォルダでなければ従来どおりOSで開く
            try:
                os.startfile(resolved_path)
            except Exception as e:
                QMessageBox.warning(self, "エラー", f"開けませんでした: {e}")

    def refresh_explorer_for(self, tab):
        """ep/sq/shot が変わったら、そのタブで開いたテンプレートを再解決して追従"""
        if self.explorer.isHidden():
            return
        if getattr(self, "_explorer_owner", None) is not tab:
            return
        template = getattr(self, "_explorer_template", None)
        if not template:
            return
        try:
            resolved = resolve_path(template, tab)
        except Exception:
            return
        if (os.path.isdir(resolved)
                and os.path.normpath(resolved)
                != os.path.normpath(self.explorer.current_root or "")):
            self.explorer.set_root(resolved)

    def hide_explorer(self):
        if not self.explorer.isHidden():
            w = self.explorer.width()
            self.explorer.hide()
            # パネル分だけウィンドウを縮める
            self.resize(max(self.width() - w, 260), self.height())

    def add_bookmark_from_path(self, path):
        """ファイルブラウザから現在のプロジェクトタブへブックマーク登録"""
        tab = self.tab_widget.currentWidget()
        if isinstance(tab, ProjectTab):
            tab.add_bookmark_for(path)
        else:
            QMessageBox.warning(self, "エラー", "追加先のプロジェクトタブがありません。")

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

    def open_project_context_menu(self, pos):
        tab_bar = self.tab_widget.tabBar()
        index = tab_bar.tabAt(pos)
        if index < 0:
            return
        menu = QMenu(self)
        rename_action = menu.addAction("プロジェクト名を編集")
        delete_action = menu.addAction("プロジェクトを削除")
        action = menu.exec_(tab_bar.mapToGlobal(pos))
        if action == rename_action:
            self.rename_project(index)
        elif action == delete_action:
            self.delete_project(index)

    def rename_project(self, index):
        tab = self.tab_widget.widget(index)
        if not isinstance(tab, ProjectTab):
            return
        old_name = tab.project_data.get(
            "name", self.tab_widget.tabText(index))
        new_name, ok = QInputDialog.getText(
            self, "プロジェクト名を編集", "新しいプロジェクト名:",
            text=old_name)
        new_name = new_name.strip()
        if not ok or not new_name or new_name == old_name:
            return

        invalid_chars = '<>:"/\\|?*'
        reserved_names = {
            "CON", "PRN", "AUX", "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }
        if (any(char in new_name for char in invalid_chars)
                or new_name.endswith((".", " "))
                or new_name.split(".", 1)[0].upper() in reserved_names):
            QMessageBox.warning(
                self, "名前エラー",
                "プロジェクト名にファイル名として使用できない文字が含まれています。")
            return
        if new_name in self.project_tabs:
            QMessageBox.warning(
                self, "名前エラー",
                f"プロジェクト「{new_name}」は既に存在します。")
            return

        old_path = os.path.abspath(
            tab.source_path or os.path.join(
                USER_DATA_DIR, f"{old_name}.json"))
        new_path = os.path.join(
            os.path.dirname(old_path), f"{new_name}.json")
        same_path = os.path.normcase(old_path) == os.path.normcase(new_path)
        if not same_path and os.path.exists(new_path):
            QMessageBox.warning(
                self, "名前エラー",
                f"同名のプロジェクトファイルが既に存在します:\n{new_path}")
            return

        moved = False
        try:
            if os.path.isfile(old_path) and old_path != new_path:
                os.rename(old_path, new_path)
                moved = True
            tab.project_data["name"] = new_name
            with open(new_path, "w", encoding="utf-8") as f:
                json.dump(tab.project_data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            tab.project_data["name"] = old_name
            if moved and os.path.exists(new_path) and not os.path.exists(old_path):
                try:
                    os.rename(new_path, old_path)
                except Exception:
                    pass
            QMessageBox.warning(
                self, "名前変更エラー",
                f"プロジェクト名を変更できませんでした:\n\n{e}")
            return

        tab.source_path = new_path
        tab.proj = new_name
        if self.project_tabs.get(old_name) is tab:
            self.project_tabs.pop(old_name, None)
        self.project_tabs[new_name] = tab
        self.tab_widget.setTabText(index, new_name)
        self.save_workspace()

    def delete_project(self, index):
        tab = self.tab_widget.widget(index)
        if not isinstance(tab, ProjectTab):
            return
        name = tab.project_data.get("name", self.tab_widget.tabText(index))
        answer = QMessageBox.question(
            self,
            "プロジェクトの削除",
            f"本当にプロジェクト「{name}」を削除しますか？\n"
            "この操作は元に戻せません。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        json_path = tab.source_path or os.path.join(
            USER_DATA_DIR, f"{name}.json")
        try:
            if os.path.isfile(json_path):
                os.remove(json_path)
        except Exception as e:
            QMessageBox.warning(
                self, "削除エラー",
                f"プロジェクトファイルを削除できませんでした:\n{json_path}\n\n{e}")
            return

        self.tab_widget.removeTab(index)
        if self.project_tabs.get(name) is tab:
            self.project_tabs.pop(name, None)
        tab.deleteLater()
        self.save_workspace()

    def add_new_project(self):
        name, ok = QInputDialog.getText(self, "プロジェクト名", "プロジェクト名を入力:")
        if not ok or not name:
            return
        tab = self._add_tab(default_project_data(name), None)
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
