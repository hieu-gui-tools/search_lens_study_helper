"""
Result overlay panel: slides in from the right side of the screen.
Displays Google's Gemini overview and supports TTS via edge-tts (Sonia voice).

TTS behaviour:
  - Select text in the Gemini panel → click 🔊 to read just that selection.
  - No selection → 🔊 reads the full Gemini result.
  - ⏹ stops playback immediately.
  - Default voice: en-GB-SoniaNeural (British English, female).
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import re
import tempfile
import urllib.parse
import urllib.request

import keyboard as _kb
from PIL import Image
from PySide6.QtCore import (
    QEasingCurve, QMetaObject, QPropertyAnimation, QRect, Qt, QThread, QUrl,
    Signal, Slot, QTimer,
)
from PySide6.QtGui import QDesktopServices, QCursor
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)


# ── TTS voice ───────────────────────────────────────────────────────────────
TTS_VOICE = "en-GB-SoniaNeural"


# ── Stylesheet ───────────────────────────────────────────────────────────────
STYLE = """
QWidget {
    background-color: #1e1e2e;
    color: #cdd6f4;
    font-family: 'Segoe UI', 'Arial', sans-serif;
    font-size: 13px;
}
QTabWidget::pane {
    border: 1px solid #45475a;
    border-radius: 6px;
    background: #181825;
}
QTabBar::tab {
    background: #313244; color: #a6adc8;
    padding: 8px 16px; border-radius: 4px;
    margin-right: 3px;
    font-size: 13px;
}
QTabBar::tab:selected { background: #89b4fa; color: #1e1e2e; font-weight: bold; }
QTextBrowser {
    background: #313244; border: 1px solid #45475a;
    border-radius: 6px; padding: 14px; color: #cdd6f4;
    font-size: 16px;
}
QPushButton#primary {
    background: #89b4fa; color: #1e1e2e;
    border: none; border-radius: 7px;
    padding: 9px 16px; font-weight: bold;
    font-size: 13px;
}
QPushButton#primary:hover { background: #b4befe; }
QPushButton#tts_btn {
    background: #a6e3a1; color: #1e1e2e;
    border: none; border-radius: 7px;
    padding: 9px 14px; font-weight: bold;
    font-size: 13px;
}
QPushButton#tts_btn:hover  { background: #94e2d5; }
QPushButton#tts_btn:disabled { background: #45475a; color: #6c7086; }
QPushButton#stop_btn {
    background: #f38ba8; color: #1e1e2e;
    border: none; border-radius: 7px;
    padding: 9px 12px; font-weight: bold;
    font-size: 13px;
}
QPushButton#stop_btn:hover { background: #eba0ac; }
QPushButton#stop_btn:disabled { background: #45475a; color: #6c7086; }
QPushButton#close_btn {
    background: #f38ba8; color: #1e1e2e;
    border: none; border-radius: 6px; padding: 4px 10px; font-weight: bold;
}
QPushButton#close_btn:hover { background: #eba0ac; }
QLabel#status { color: #fab387; font-size: 13px; }
QFrame#divider { background: #45475a; max-height: 1px; }
"""


# ── TTS worker thread ────────────────────────────────────────────────────────

class _TtsWorker(QThread):
    """Generates speech MP3 via edge-tts in a background thread."""

    audio_ready = Signal(str)   # emits temp file path when done
    tts_error   = Signal(str)   # emits error message

    def __init__(self, text: str, voice: str = TTS_VOICE, parent=None):
        super().__init__(parent)
        self._text  = text
        self._voice = voice
        self._tmp   = ""

    def run(self):
        async def _gen():
            import edge_tts
            tmp = tempfile.NamedTemporaryFile(
                suffix=".mp3", delete=False, prefix="lens_tts_"
            )
            tmp.close()
            communicate = edge_tts.Communicate(self._text, self._voice)
            await communicate.save(tmp.name)
            return tmp.name

        try:
            loop = asyncio.new_event_loop()
            path = loop.run_until_complete(_gen())
            loop.close()
            self._tmp = path
            self.audio_ready.emit(path)
        except Exception as exc:
            self.tts_error.emit(str(exc))

    def cleanup(self):
        """Delete temp audio file."""
        if self._tmp and os.path.exists(self._tmp):
            try:
                os.unlink(self._tmp)
            except OSError:
                pass
        self._tmp = ""


# ── Translation ──────────────────────────────────────────────────────────────

class _TranslationWorker(QThread):
    """Fetches translation from Google Translate."""
    translation_ready = Signal(str)
    translation_error = Signal(str)

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self._text = text

    def run(self):
        try:
            url = 'https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=vi&dt=t&q=' + urllib.parse.quote(self._text)
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode())
                translated = "".join([sentence[0] for sentence in data[0] if sentence[0]])
                self.translation_ready.emit(translated)
        except Exception as exc:
            self.translation_error.emit(f"Lỗi dịch: {exc}")


class _TranslationPopup(QWidget):
    """Floating frameless popup to show translation results."""
    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setStyleSheet("""
            QWidget {
                background-color: #313244;
                color: #cdd6f4;
                border: 1px solid #89b4fa;
                border-radius: 6px;
                font-family: 'Segoe UI', Arial, sans-serif;
                font-size: 14px;
            }
            QLabel { padding: 10px; border: none; }
        """)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.lbl = QLabel()
        self.lbl.setWordWrap(True)
        self.lbl.setMaximumWidth(400)
        layout.addWidget(self.lbl)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_translation(self, text: str, pos):
        self.lbl.setText(text)
        self.adjustSize()
        # Offset slightly from cursor
        self.move(pos.x() + 15, pos.y() + 15)
        self.show()
        self._timer.start(8000)

    def mousePressEvent(self, event):
        self.hide()


# ── HTML formatter ───────────────────────────────────────────────────────────

def _format_gemini_html(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        lines = ["(Không có thông tin Gemini)"]

    blocks: list[str] = []
    paragraph_lines: list[str] = []

    def flush_paragraph():
        if not paragraph_lines:
            return
        paragraph = html.escape(" ".join(paragraph_lines))
        blocks.append(f"<p>{paragraph}</p>")
        paragraph_lines.clear()

    for line in lines:
        escaped = html.escape(line)
        if re.match(r"^Đáp án đúng\b", line, re.IGNORECASE):
            flush_paragraph()
            blocks.append(f'<div class="answer">{escaped}</div>')
        elif re.match(r"^[A-D][\.\)]\s+", line):
            flush_paragraph()
            blocks.append(f'<div class="choice">{escaped}</div>')
        elif re.match(
            r"^(Câu hỏi|Giải thích|Giải thích chi tiết|Phân tích|Kết luận|Tóm tắt|Kiến thức|Bổ sung|Trả lời|Bản dịch)\b",
            line, re.IGNORECASE,
        ) or re.match(r"^\d+[\.\)]\s+", line):
            flush_paragraph()
            blocks.append(f'<div class="section">{escaped}</div>')
        else:
            paragraph_lines.append(line)

    flush_paragraph()

    return """
    <html>
    <head>
    <style>
        body {
            color: #cdd6f4;
            font-family: 'Segoe UI', Arial, sans-serif;
            font-size: 16px;
            line-height: 1.55;
            margin: 0;
        }
        p { margin: 0 0 10px 0; }
        .section {
            color: #a6e3a1;
            font-size: 17px;
            font-weight: 700;
            margin: 14px 0 6px 0;
        }
        .answer {
            background: #a6e3a1;
            color: #1e1e2e;
            border-radius: 6px;
            font-size: 17px;
            font-weight: 800;
            margin: 10px 0;
            padding: 9px 11px;
        }
        .choice {
            background: #242438;
            border-left: 4px solid #89b4fa;
            border-radius: 4px;
            margin: 6px 0;
            padding: 7px 10px;
        }
        ::selection { background: #585b70; }
    </style>
    </head>
    <body>
    """ + "\n".join(blocks) + """
    </body>
    </html>
    """


# ── Overlay widget ────────────────────────────────────────────────────────────

class ResultOverlay(QWidget):
    """Animated side-panel overlay showing Gemini's image overview + TTS."""

    PANEL_WIDTH = 520

    def __init__(self, parent=None):
        super().__init__(
            parent,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setStyleSheet(STYLE)
        self._lens_url = ""
        self._current_gemini_text = ""
        self._last_spoken_text: str = ""   # text of last TTS call (for replay label)

        # TTS state
        self._tts_worker: _TtsWorker | None = None
        self._player = QMediaPlayer(self)
        self._audio_out = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_out)
        self._audio_out.setVolume(1.0)
        self._player.playbackStateChanged.connect(self._on_playback_state)
        # _tts_file: persists after playback so replay can reuse it without re-generating
        self._tts_file: str = ""

        # Translation state
        self._trans_worker: _TranslationWorker | None = None
        self._trans_popup = _TranslationPopup()

        self._build_ui()
        self._register_hotkeys()
        self._position_hidden()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        self.setFixedWidth(self.PANEL_WIDTH)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        # Header
        hdr = QHBoxLayout()
        title = QLabel("✨ Gemini Lens")
        title.setStyleSheet("font-size:18px; font-weight:bold; color:#89b4fa;")
        self._status_lbl = QLabel("⏳ Đang xử lý…")
        self._status_lbl.setObjectName("status")
        close_btn = QPushButton("✕")
        close_btn.setObjectName("close_btn")
        close_btn.setFixedSize(32, 32)
        close_btn.clicked.connect(self.slide_out)
        hdr.addWidget(title)
        hdr.addStretch()
        hdr.addWidget(close_btn)
        root.addLayout(hdr)

        # Tabs (chiếm phần lớn không gian)
        self._tabs = QTabWidget()
        root.addWidget(self._tabs, 1)

        # Gemini browser
        self._gemini_browser = QTextBrowser()
        self._gemini_browser.document().setDocumentMargin(10)
        self._gemini_browser.setPlaceholderText(
            "Thông tin Gemini về hình ảnh sẽ xuất hiện ở đây…"
        )
        self._gemini_browser.selectionChanged.connect(self._on_selection_changed)
        self._tabs.addTab(self._gemini_browser, "✨ Gemini")

        # Log browser
        self._raw_browser = QTextBrowser()
        self._raw_browser.document().setDocumentMargin(10)
        self._raw_browser.setPlaceholderText("Nhật ký chi tiết…")
        self._tabs.addTab(self._raw_browser, "📋 Log")

        # Action row
        action_row = QHBoxLayout()

        self._tts_btn = QPushButton("🔊 Đọc")
        self._tts_btn.setObjectName("tts_btn")
        self._tts_btn.setToolTip(
            "Đọc kết quả Gemini bằng giọng Sonia (Anh).\n"
            "Bôi đen một đoạn trước khi nhấn để chỉ đọc đoạn đó."
        )
        self._tts_btn.setEnabled(False)
        self._tts_btn.clicked.connect(self._on_tts_clicked)

        self._stop_btn = QPushButton("⏹")
        self._stop_btn.setObjectName("stop_btn")
        self._stop_btn.setToolTip("Dừng đọc")
        self._stop_btn.setEnabled(False)
        self._stop_btn.setFixedWidth(44)
        self._stop_btn.clicked.connect(self._stop_tts)

        self._replay_btn = QPushButton("🔁")
        self._replay_btn.setObjectName("tts_btn")
        self._replay_btn.setToolTip("Đọc lại cụm từ / đoạn vừa đọc")
        self._replay_btn.setEnabled(False)
        self._replay_btn.setFixedWidth(44)
        self._replay_btn.clicked.connect(self._on_replay_clicked)

        self._copy_btn = QPushButton("📋 Copy")
        self._copy_btn.setObjectName("primary")
        self._copy_btn.clicked.connect(self._copy_result)
        self._copy_btn.setEnabled(False)

        action_row.addWidget(self._tts_btn)
        action_row.addWidget(self._stop_btn)
        action_row.addWidget(self._replay_btn)
        action_row.addStretch()
        action_row.addWidget(self._copy_btn)
        root.addLayout(action_row)

        # TTS status (giữa)
        # Status label — duy nhất, dưới cùng
        self._status_lbl.setStyleSheet(
            "color:#6c7086; font-size:11px; padding: 2px 0;"
        )
        self._status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._status_lbl)


    # ── Positioning / animation ───────────────────────────────────────────────

    def _screen_geo(self):
        return QApplication.primaryScreen().geometry()

    def _visible_rect(self) -> QRect:
        g = self._screen_geo()
        return QRect(g.right() - self.PANEL_WIDTH, g.top(), self.PANEL_WIDTH, g.height())

    def _hidden_rect(self) -> QRect:
        g = self._screen_geo()
        return QRect(g.right(), g.top(), self.PANEL_WIDTH, g.height())

    def _position_hidden(self):
        self.setGeometry(self._hidden_rect())

    def slide_in(self):
        self.setGeometry(self._hidden_rect())
        self.show()
        self.raise_()
        anim = QPropertyAnimation(self, b"geometry", self)
        anim.setDuration(320)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(self._hidden_rect())
        anim.setEndValue(self._visible_rect())
        anim.start()
        self._anim = anim

    def slide_out(self):
        self._stop_tts()
        anim = QPropertyAnimation(self, b"geometry", self)
        anim.setDuration(260)
        anim.setEasingCurve(QEasingCurve.Type.InCubic)
        anim.setStartValue(self._visible_rect())
        anim.setEndValue(self._hidden_rect())
        anim.finished.connect(self.hide)
        anim.start()
        self._anim = anim

    def _register_hotkeys(self):
        """Register global hotkeys (thread-safe)."""
        try:
            _kb.add_hotkey(
                "ctrl+shift+a",
                lambda: QMetaObject.invokeMethod(
                    self, "_on_tts_hotkey", Qt.ConnectionType.QueuedConnection
                ),
            )
            _kb.add_hotkey(
                "ctrl+shift+d",
                lambda: QMetaObject.invokeMethod(
                    self, "_on_translate_hotkey", Qt.ConnectionType.QueuedConnection
                ),
            )
        except Exception:
            pass  # keyboard lib may not be available in all environments

    @Slot()
    def _on_tts_hotkey(self):
        """Called from hotkey Ctrl+Shift+A — only acts when overlay is visible."""
        if self.isVisible():
            if self._tts_btn.isEnabled():
                self._on_tts_clicked()

    @Slot()
    def _on_translate_hotkey(self):
        """Called from hotkey Ctrl+Shift+D."""
        if self.isVisible() and self._has_selection():
            sel = self._gemini_browser.textCursor().selectedText().strip()
            if not sel:
                return

            self._trans_popup.show_translation("⏳ Đang dịch...", QCursor.pos())

            if self._trans_worker and self._trans_worker.isRunning():
                self._trans_worker.quit()
                self._trans_worker.wait(1000)

            self._trans_worker = _TranslationWorker(sel, parent=self)
            self._trans_worker.translation_ready.connect(self._on_translation_ready)
            self._trans_worker.translation_error.connect(self._on_translation_error)
            self._trans_worker.start()

    @Slot(str)
    def _on_translation_ready(self, translated: str):
        self._trans_popup.show_translation(translated, QCursor.pos())

    @Slot(str)
    def _on_translation_error(self, err: str):
        self._trans_popup.show_translation(err, QCursor.pos())

    # ── Public display API ────────────────────────────────────────────────────

    def show_loading(self, img: Image.Image):
        """Called immediately after capture."""
        self._stop_tts()
        self._gemini_browser.setHtml(
            _format_gemini_html("Đang chờ Google Gemini tạo câu trả lời…")
        )
        self._raw_browser.setPlainText("")
        self._tabs.setTabText(0, "✨ Gemini")
        self._status_lbl.setText("⏳ Đang upload ảnh lên Google Lens…")
        self._copy_btn.setEnabled(False)
        self._tts_btn.setEnabled(False)
        self._lens_url = ""
        self._current_gemini_text = ""
        self._tabs.setCurrentIndex(0)
        self.slide_in()

    @Slot(str, str, str, str)
    def show_results(self, lens_url: str, display_text: str, ocr_text: str, result_json: str):
        self._lens_url = lens_url
        self._status_lbl.setText("✅ Đã lấy thông tin Gemini")

        try:
            data = json.loads(result_json)
        except Exception:
            data = {}

        gemini = (
            data.get("gemini_info")
            or data.get("ocr_text")
            or ocr_text
            or "(Không có thông tin Gemini)"
        )
        has_gemini = bool(gemini and gemini != "(Không có thông tin Gemini)")
        self._current_gemini_text = gemini if has_gemini else ""
        self._copy_btn.setEnabled(has_gemini)
        self._tts_btn.setEnabled(False)  # enabled only when user selects text
        self._gemini_browser.setHtml(_format_gemini_html(gemini))
        self._gemini_browser.verticalScrollBar().setValue(0)
        if has_gemini:
            self._tabs.setTabText(0, f"✨ Gemini ({len(gemini.split())} từ)")
            self._status_lbl.setText("✅ Đã lấy thông tin Gemini")

        err = data.get("error", "")
        self._raw_browser.setPlainText(
            f"URL: {lens_url}\n\nGemini chars: {len(gemini) if gemini else 0}\n"
            + (f"\nError: {err}" if err else "")
            + f"\n\nFull text:\n{display_text[:8000]}"
        )
        self._tabs.setCurrentIndex(0 if gemini else 1)

    def show_error(self, msg: str):
        self._status_lbl.setText("❌ Lỗi")
        self._current_gemini_text = ""
        self._copy_btn.setEnabled(False)
        self._tts_btn.setEnabled(False)
        self._raw_browser.setPlainText(f"LỖI:\n{msg}")
        self._tabs.setCurrentIndex(1)

    # ── TTS logic ─────────────────────────────────────────────────────────────

    def _has_selection(self) -> bool:
        """True when the user has highlighted text in the Gemini browser."""
        return bool(self._gemini_browser.textCursor().selectedText().strip())

    def _on_selection_changed(self):
        """Enable / disable the Read button based on whether text is selected."""
        has_sel = self._has_selection()
        # Only toggle when not currently playing/generating
        if self._player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            if bool(self._current_gemini_text):
                self._tts_btn.setEnabled(has_sel)

    def _on_tts_clicked(self):
        # If already playing, stop first
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._stop_tts()
            return

        # Determine text to speak: selection > full result
        sel = self._gemini_browser.textCursor().selectedText().strip()
        text_to_speak = sel if sel else self._current_gemini_text
        if not text_to_speak:
            return

        self._speak(text_to_speak)

    def _on_replay_clicked(self):
        """Replay last audio WITHOUT re-generating — just seek to 0 and play."""
        if not self._tts_file or not os.path.exists(self._tts_file):
            # File gone; fall back to re-generating
            if self._last_spoken_text:
                self._speak(self._last_spoken_text)
            return
        # Stop if currently playing
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.stop()
        # Reuse existing file — no network call, instant
        self._player.setSource(QUrl.fromLocalFile(self._tts_file))
        self._player.setPosition(0)
        self._player.play()
        self._tts_btn.setText("⏸ Dừng")
        self._stop_btn.setEnabled(True)
        self._replay_btn.setEnabled(False)
        self._status_lbl.setText("🔁 Đang phát lại…")

    def _speak(self, text: str):
        """Generate new TTS audio for text and play it."""
        self._last_spoken_text = text
        self._replay_btn.setEnabled(False)
        self._tts_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._status_lbl.setText("⏳ Đang tạo giọng nói…")

        # Delete previous audio file before generating a new one
        if self._tts_file and os.path.exists(self._tts_file):
            try:
                os.unlink(self._tts_file)
            except OSError:
                pass
        self._tts_file = ""

        # Stop any previous worker
        if self._tts_worker and self._tts_worker.isRunning():
            self._tts_worker.quit()
            self._tts_worker.wait(2000)

        worker = _TtsWorker(text, TTS_VOICE, parent=self)
        worker.audio_ready.connect(self._on_audio_ready)
        worker.tts_error.connect(self._on_tts_error)
        self._tts_worker = worker
        worker.start()

    def _stop_tts(self):
        self._player.stop()
        # Do NOT delete _tts_file here — keep it so replay can reuse it
        self._stop_btn.setEnabled(False)
        self._tts_btn.setEnabled(self._has_selection())
        self._tts_btn.setText("🔊 Đọc")
        self._replay_btn.setEnabled(bool(self._tts_file and os.path.exists(self._tts_file)))
        if self._current_gemini_text:
            self._status_lbl.setText("✅ Đã lấy thông tin Gemini")

    @Slot(str)
    def _on_audio_ready(self, path: str):
        # Store file persistently so replay can reuse it
        self._tts_file = path
        self._player.setSource(QUrl.fromLocalFile(path))
        self._player.play()
        self._tts_btn.setEnabled(True)
        self._tts_btn.setText("⏸ Dừng")
        self._stop_btn.setEnabled(True)
        self._status_lbl.setText("🔊 Đang đọc…")

    @Slot(str)
    def _on_tts_error(self, msg: str):
        self._tts_btn.setEnabled(self._has_selection())
        self._tts_btn.setText("🔊 Đọc")
        self._stop_btn.setEnabled(False)
        self._status_lbl.setText(f"❌ TTS lỗi: {msg[:60]}")

    @Slot(QMediaPlayer.PlaybackState)
    def _on_playback_state(self, state: QMediaPlayer.PlaybackState):
        if state == QMediaPlayer.PlaybackState.StoppedState:
            self._tts_btn.setText("🔊 Đọc")
            self._tts_btn.setEnabled(self._has_selection())
            self._stop_btn.setEnabled(False)
            # Enable replay only when file still exists on disk
            self._replay_btn.setEnabled(
                bool(self._tts_file and os.path.exists(self._tts_file))
            )
            if self._current_gemini_text:
                self._status_lbl.setText("✅ Đọc xong · Bấm 🔁 để nghe lại")
            # NOTE: do NOT delete _tts_file here — kept for instant replay

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _copy_result(self):
        if not self._current_gemini_text:
            return
        QApplication.clipboard().setText(self._current_gemini_text)
        self._status_lbl.setText("✅ Đã copy kết quả Gemini")
