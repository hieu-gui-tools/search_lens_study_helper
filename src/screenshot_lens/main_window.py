"""
Main application window.
"""
from __future__ import annotations
import io
import datetime
import os
import sys
from pathlib import Path

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QCheckBox, QFileDialog,
    QSystemTrayIcon, QMenu, QApplication, QLineEdit,
    QGroupBox, QComboBox
)
from PySide6.QtCore import Qt, QProcess, QTimer, Signal, Slot, QSettings
from PySide6.QtGui import QKeySequence, QShortcut, QPixmap, QImage, QIcon
from PIL import Image

from .capture import RegionSelector
from .search import SearchWorker
from .overlay import ResultOverlay


SAVE_DIR_DEFAULT = str(Path.home() / "Pictures" / "ScreenshotLens")

PROMPTS = {
    "Đa năng (Theo cấu trúc)": "Giải chi tiết bài tập, câu hỏi hoặc giải thích nội dung trong ảnh bằng tiếng Việt. Trình bày rõ ràng theo cấu trúc: 'Đáp án đúng:', 'Giải thích chi tiết:', 'Phân tích:' và 'Tóm tắt:'.",
    "Tự nhiên (Giống Google Chrome)": "Hãy giải thích chi tiết nội dung hoặc giải bài tập trong bức ảnh này bằng tiếng Việt.",
    "Trắc nghiệm": "Giải chi tiết bài tập trắc nghiệm trong ảnh bằng tiếng Việt. Trình bày: 'Đáp án đúng:', 'Giải thích logic:', và 'Phân tích các phương án còn lại:'.",
    "Giải thích khái niệm": "Giải thích chi tiết các khái niệm, ý chính hoặc bối cảnh của nội dung trong ảnh bằng tiếng Việt. Trình bày rõ ràng theo cấu trúc: 'Tóm tắt:' và 'Giải thích:'.",
    "Dịch thuật": "Dịch toàn bộ nội dung trong ảnh sang tiếng Việt một cách tự nhiên, dễ hiểu. Nếu có từ lóng hoặc thành ngữ, hãy giải thích thêm.",
    "Dịch và tóm tắt": "Dịch đoạn văn trong hình ảnh sang tiếng Việt và tóm tắt nội dung đoạn văn, yêu cầu dịch sang tiếng Việt chuẩn theo ngữ cảnh. Trình bày rõ ràng theo 2 phần tách biệt (bắt đầu nội dung ở dòng mới dưới mỗi tiêu đề): 'Bản dịch:' và 'Tóm tắt:'.",
}


STYLE = """
QMainWindow, QWidget  { background:#1e1e2e; color:#cdd6f4; font-family:'Segoe UI'; }
QPushButton {
    background:#89b4fa; color:#1e1e2e; border:none;
    border-radius:8px; padding:8px 20px; font-weight:bold; font-size:13px;
}
QPushButton:hover  { background:#b4befe; }
QPushButton:disabled { background:#45475a; color:#6c7086; }
QGroupBox {
    border:1px solid #45475a; border-radius:8px;
    margin-top:10px; padding:8px; color:#a6e3a1; font-weight:bold;
}
QGroupBox::title { subcontrol-origin:margin; left:10px; }
QCheckBox { spacing:8px; }
QLineEdit {
    background:#313244; border:1px solid #45475a;
    border-radius:6px; padding:4px 8px; color:#cdd6f4;
}
QStatusBar { background:#181825; color:#a6adc8; font-size:11px; }
QComboBox {
    background:#313244; border:1px solid #45475a;
    border-radius:6px; padding:4px 8px; color:#cdd6f4;
}
QComboBox::drop-down { border:none; }
QComboBox QAbstractItemView {
    background:#313244; color:#cdd6f4; selection-background-color:#89b4fa; selection-color:#1e1e2e;
}
"""


class MainWindow(QMainWindow):
    _global_capture_requested = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("📷 Screenshot Lens")
        self.setMinimumWidth(480)
        self.setStyleSheet(STYLE)

        # Icon
        _icon_path = Path(__file__).resolve().parents[2] / "icon.ico"
        if _icon_path.exists():
            self.setWindowIcon(QIcon(str(_icon_path)))

        # Căn giữa màn hình
        from PySide6.QtGui import QScreen
        screen: QScreen = QApplication.primaryScreen()
        if screen:
            sg = screen.availableGeometry()
            self.setGeometry(
                sg.x() + (sg.width() - 480) // 2,
                sg.y() + (sg.height() - 700) // 2,
                480, 700,
            )

        self._last_image: Image.Image | None = None
        self._overlay = ResultOverlay()
        self._worker: SearchWorker | None = None
        self._save_dir = SAVE_DIR_DEFAULT
        self._global_hotkey_handle = None
        self._capture_pending = False
        self._show_main_after_capture = True
        self._force_search_after_capture = False

        self._build_ui()
        self._load_settings()
        
        self._global_capture_requested.connect(self.start_hotkey_capture)
        self._setup_hotkeys()
        self._setup_tray()
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._cleanup_global_hotkey)
        self._warm_up_lens()

    # ── UI ─────────────────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        # Title
        title = QLabel("📷 Screenshot Lens")
        title.setStyleSheet("font-size:20px; font-weight:bold; color:#89b4fa;")
        layout.addWidget(title)

        sub = QLabel("Chụp màn hình  →  Google Lens  →  Gemini  →  Kết quả")
        sub.setStyleSheet("color:#6c7086; font-size:12px;")
        layout.addWidget(sub)

        # ── Capture ──
        cap_group = QGroupBox("📸 Chụp màn hình")
        cap_layout = QVBoxLayout(cap_group)

        self._btn_capture = QPushButton("🖱  Chọn vùng & Chụp")
        self._btn_capture.clicked.connect(self.start_capture)
        cap_layout.addWidget(self._btn_capture)

        hint = QLabel("Hotkey global: Ctrl+Shift+S  |  Esc để huỷ")
        hint.setStyleSheet("color:#6c7086; font-size:11px;")
        cap_layout.addWidget(hint)
        layout.addWidget(cap_group)

        # ── Options ──
        opt_group = QGroupBox("⚙️ Tuỳ chọn")
        opt_layout = QVBoxLayout(opt_group)

        self._chk_clipboard = QCheckBox("Sao chép ảnh vào Clipboard")
        self._chk_clipboard.setChecked(True)
        opt_layout.addWidget(self._chk_clipboard)

        self._chk_use_prompt = QCheckBox("Sử dụng AI Prompt (Gemini)")
        self._chk_use_prompt.setChecked(True)
        opt_layout.addWidget(self._chk_use_prompt)

        self._combo_prompt = QComboBox()
        self._combo_prompt.addItems(list(PROMPTS.keys()))
        opt_layout.addWidget(self._combo_prompt)
        
        self._chk_use_prompt.toggled.connect(self._combo_prompt.setEnabled)

        self._chk_save = QCheckBox("Lưu ảnh vào thư mục")
        self._chk_save.setChecked(False)
        opt_layout.addWidget(self._chk_save)

        dir_row = QHBoxLayout()
        self._dir_input = QLineEdit(self._save_dir)
        self._dir_input.setEnabled(False)
        btn_dir = QPushButton("…")
        btn_dir.setFixedWidth(36)
        btn_dir.clicked.connect(self._choose_dir)
        dir_row.addWidget(self._dir_input)
        dir_row.addWidget(btn_dir)
        opt_layout.addLayout(dir_row)
        self._chk_save.toggled.connect(self._dir_input.setEnabled)

        self._chk_auto_search = QCheckBox("Tự động tìm kiếm ngay sau khi chụp")
        self._chk_auto_search.setChecked(True)
        opt_layout.addWidget(self._chk_auto_search)

        self._chk_headless = QCheckBox("🔍 Headless (ẩn cửa sổ trình duyệt)")
        self._chk_headless.setChecked(True)
        self._chk_headless.setToolTip(
            "Bật: Playwright chạy ẩn (headless) – nhanh hơn.\n"
            "Tắt: Hiện cửa sổ Chrome thật để kiểm tra kết quả trực tiếp."
        )
        opt_layout.addWidget(self._chk_headless)

        self._chk_clipboard.toggled.connect(self._save_settings)
        self._chk_save.toggled.connect(self._save_settings)
        self._chk_auto_search.toggled.connect(self._save_settings)
        self._chk_use_prompt.toggled.connect(self._save_settings)
        self._combo_prompt.currentIndexChanged.connect(self._save_settings)
        self._chk_headless.toggled.connect(self._on_headless_toggled)

        layout.addWidget(opt_group)

        # ── Manual search ──
        search_group = QGroupBox("🔍 Tìm kiếm")
        search_layout = QHBoxLayout(search_group)
        self._btn_search = QPushButton("🔍  Tìm kiếm ảnh vừa chụp")
        self._btn_search.setEnabled(False)
        self._btn_search.clicked.connect(self.start_search)
        search_layout.addWidget(self._btn_search)
        layout.addWidget(search_group)

        # ── App actions ──
        app_group = QGroupBox("Ứng dụng")
        app_layout = QHBoxLayout(app_group)
        self._btn_show_overlay = QPushButton("✨  Hiện lại overlay")
        self._btn_show_overlay.clicked.connect(self.show_overlay)
        app_layout.addWidget(self._btn_show_overlay)
        self._btn_restart = QPushButton("↻  Khởi động lại app")
        self._btn_restart.clicked.connect(self.restart_app)
        app_layout.addWidget(self._btn_restart)
        layout.addWidget(app_group)

        # ── Preview ──
        prev_group = QGroupBox("🖼 Preview")
        prev_layout = QVBoxLayout(prev_group)
        self._preview = QLabel("(Chưa có ảnh)")
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setFixedHeight(140)
        self._preview.setStyleSheet("background:#313244; border-radius:6px; color:#6c7086;")
        prev_layout.addWidget(self._preview)
        layout.addWidget(prev_group)

        layout.addStretch()
        self.statusBar().showMessage("Sẵn sàng.")

    def _load_settings(self):
        settings = QSettings()
        self._chk_clipboard.setChecked(settings.value("chk_clipboard", True, type=bool))
        self._chk_save.setChecked(settings.value("chk_save", False, type=bool))
        self._chk_auto_search.setChecked(settings.value("chk_auto_search", True, type=bool))
        self._chk_use_prompt.setChecked(settings.value("chk_use_prompt", True, type=bool))
        
        prompt_idx = settings.value("combo_prompt_idx", 0, type=int)
        if 0 <= prompt_idx < self._combo_prompt.count():
            self._combo_prompt.setCurrentIndex(prompt_idx)
            
        self._save_dir = settings.value("save_dir", SAVE_DIR_DEFAULT, type=str)
        self._dir_input.setText(self._save_dir)
        self._dir_input.setEnabled(self._chk_save.isChecked())
        self._combo_prompt.setEnabled(self._chk_use_prompt.isChecked())

        headless = settings.value("headless", True, type=bool)
        self._chk_headless.blockSignals(True)
        self._chk_headless.setChecked(headless)
        self._chk_headless.blockSignals(False)
        try:
            from .lens_browser import set_headless
            set_headless(headless)
        except Exception:
            pass

    def _save_settings(self, *args):
        settings = QSettings()
        settings.setValue("chk_clipboard", self._chk_clipboard.isChecked())
        settings.setValue("chk_save", self._chk_save.isChecked())
        settings.setValue("chk_auto_search", self._chk_auto_search.isChecked())
        settings.setValue("chk_use_prompt", self._chk_use_prompt.isChecked())
        settings.setValue("combo_prompt_idx", self._combo_prompt.currentIndex())
        settings.setValue("save_dir", self._save_dir)
        settings.setValue("headless", self._chk_headless.isChecked())

    def _on_headless_toggled(self, checked: bool):
        self._save_settings()
        try:
            from .lens_browser import set_headless
            set_headless(checked)
        except Exception:
            pass
        mode = "ẩn" if checked else "hiện (debug)"
        self.statusBar().showMessage(f"🌐 Chế độ trình duyệt: {mode}. Hiệu lực từ lần tìm kiếm tiếp theo.")

    # ── Hotkeys ───────────────────────────────────────────────────────────

    def _setup_hotkeys(self):
        self._local_capture_shortcut = QShortcut(QKeySequence("Ctrl+Shift+S"), self)
        self._local_capture_shortcut.activated.connect(self.start_hotkey_capture)
        self._register_global_hotkey()

    def _register_global_hotkey(self):
        try:
            import keyboard

            self._global_hotkey_handle = keyboard.add_hotkey(
                "ctrl+shift+s",
                lambda: self._global_capture_requested.emit(),
                suppress=False,
            )
        except Exception as exc:
            self._global_hotkey_handle = None
            self.statusBar().showMessage(f"⚠️ Hotkey global không khả dụng: {exc}")

    def _cleanup_global_hotkey(self):
        if self._global_hotkey_handle is None:
            return
        try:
            import keyboard

            keyboard.remove_hotkey(self._global_hotkey_handle)
        except Exception:
            pass
        self._global_hotkey_handle = None

    # ── System tray ───────────────────────────────────────────────────────

    def _setup_tray(self):
        self._tray = QSystemTrayIcon(self)
        _icon_path = Path(__file__).resolve().parents[2] / "icon.ico"
        if _icon_path.exists():
            self._tray.setIcon(QIcon(str(_icon_path)))
        else:
            self._tray.setIcon(self.style().standardIcon(
                self.style().StandardPixmap.SP_ComputerIcon))
        menu = QMenu()
        menu.addAction("📷 Chụp màn hình", self.start_hotkey_capture)
        menu.addAction("✨ Hiện lại overlay", self.show_overlay)
        menu.addAction("Hiện cửa sổ", self.show)
        menu.addAction("↻ Khởi động lại app", self.restart_app)
        menu.addSeparator()
        menu.addAction("Thoát", QApplication.quit)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._tray_activated)
        self._tray.show()

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show(); self.raise_()

    def _warm_up_lens(self):
        try:
            from .lens_browser import warm_up_lens_browser
            warm_up_lens_browser()
        except Exception:
            pass

    @Slot()
    def show_overlay(self):
        if self._overlay.isVisible():
            self._overlay.raise_()
            self._overlay.activateWindow()
            return

        self._overlay.slide_in()

    @Slot()
    def restart_app(self):
        program, args = self._restart_command()
        self.statusBar().showMessage("↻ Đang khởi động lại app…")
        self._btn_restart.setEnabled(False)
        QApplication.processEvents()

        detached = QProcess.startDetached(program, args, os.getcwd())
        ok = detached[0] if isinstance(detached, tuple) else bool(detached)
        if ok:
            QApplication.quit()
            return

        self._btn_restart.setEnabled(True)
        self.statusBar().showMessage("❌ Không khởi động lại được app.")

    def _restart_command(self) -> tuple[str, list[str]]:
        if getattr(sys, "frozen", False):
            return sys.executable, []
        return sys.executable, ["-m", "screenshot_lens"]

    # ── Capture flow ──────────────────────────────────────────────────────

    @Slot()
    def start_capture(self):
        self._start_capture(show_main_after_capture=True, force_search=False)

    @Slot()
    def start_hotkey_capture(self):
        self._start_capture(show_main_after_capture=False, force_search=True)

    def _start_capture(self, show_main_after_capture: bool, force_search: bool):
        if self._capture_pending:
            return
        self._capture_pending = True
        self._show_main_after_capture = show_main_after_capture
        self._force_search_after_capture = force_search
        self._warm_up_lens()
        self.hide()
        QTimer.singleShot(200, self._open_selector)

    def _open_selector(self):
        self._selector = RegionSelector()
        self._selector.destroyed.connect(lambda *_: setattr(self, "_capture_pending", False))
        self._selector.region_captured.connect(self._on_captured)
        self._selector.show()
        self._selector.activateWindow()

    @Slot(object)
    def _on_captured(self, img: Image.Image):
        self._last_image = img
        if self._show_main_after_capture:
            self.show()
        self._update_preview(img)
        self._btn_search.setEnabled(True)

        if self._chk_clipboard.isChecked():
            self._copy_to_clipboard(img)

        if self._chk_save.isChecked():
            path = self._save_image(img)
            self.statusBar().showMessage(f"✅ Đã lưu: {path}")
        else:
            self.statusBar().showMessage("✅ Đã chụp xong.")

        if self._force_search_after_capture or self._chk_auto_search.isChecked():
            self.start_search()

    # ── Search flow ───────────────────────────────────────────────────────

    @Slot()
    def start_search(self):
        if self._last_image is None:
            return
        if self._worker and self._worker.isRunning():
            return  # already searching

        self._btn_search.setEnabled(False)
        self._btn_capture.setEnabled(False)
        self.statusBar().showMessage("🔍 Đang upload ảnh lên Google Lens & đọc kết quả…")

        self._overlay.show_loading(self._last_image)

        prompt = None
        if self._chk_use_prompt.isChecked():
            prompt_key = self._combo_prompt.currentText()
            prompt = PROMPTS.get(prompt_key)

        self._worker = SearchWorker(self._last_image, prompt=prompt)
        self._worker.result_ready.connect(self._on_results)
        self._worker.error_occurred.connect(self._on_error)
        self._worker.finished.connect(self._on_worker_done)
        self._worker.start()

    @Slot(str, str, str, str)
    def _on_results(self, lens_url: str, display_text: str, ocr_text: str, result_json: str):
        self._overlay.show_results(lens_url, display_text, ocr_text, result_json)
        self.statusBar().showMessage("✅ Tìm kiếm hoàn tất.")

    @Slot(str)
    def _on_error(self, msg: str):
        self._overlay.show_error(msg)
        self.statusBar().showMessage(f"❌ Lỗi tìm kiếm.")

    @Slot()
    def _on_worker_done(self):
        self._btn_search.setEnabled(True)
        self._btn_capture.setEnabled(True)
        self._worker = None

    # ── Helpers ───────────────────────────────────────────────────────────

    def _update_preview(self, img: Image.Image):
        cp = img.copy().convert("RGB")
        cp.thumbnail((440, 130), Image.LANCZOS)
        buf = io.BytesIO()
        cp.save(buf, format="PNG")
        buf.seek(0)
        px = QPixmap()
        px.loadFromData(buf.read())
        self._preview.setPixmap(px)

    def _copy_to_clipboard(self, img: Image.Image):
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        buf.seek(0)
        qimg = QImage()
        qimg.loadFromData(buf.read())
        QApplication.clipboard().setImage(qimg)

    def _save_image(self, img: Image.Image) -> str:
        os.makedirs(self._save_dir, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self._save_dir, f"screenshot_{ts}.png")
        img.save(path)
        return path

    def _choose_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Chọn thư mục lưu ảnh", self._save_dir)
        if d:
            self._save_dir = d
            self._dir_input.setText(d)
            self._save_settings()
