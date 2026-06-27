"""
Search orchestrator: Google Lens Gemini overview (background QThread).
"""
from __future__ import annotations
from PIL import Image
from PySide6.QtCore import QThread, Signal


# ── Background QThread ──────────────────────────────────────────────────────

class SearchWorker(QThread):
    """
    Runs Google Lens Gemini image overview in a background thread.
    Emits structured result when done.
    """

    # lens_url, display_text, ocr_text, lens_result_obj (as JSON str)
    result_ready = Signal(str, str, str, str)
    error_occurred = Signal(str)

    def __init__(self, img: Image.Image, prompt: str | None = None):
        super().__init__()
        self._img = img
        self._prompt = prompt

    def run(self):
        try:
            from .lens_browser import search_with_playwright, LensResult
            import json

            lens_result: LensResult = search_with_playwright(self._img, prompt=self._prompt, timeout_ms=30000)

            gemini_info = lens_result.ocr_text
            if lens_result.error and not gemini_info:
                self.error_occurred.emit(lens_result.error)
                return

            # Serialize result for overlay
            result_json = json.dumps({
                "gemini_info": gemini_info,
                "ocr_text": gemini_info,
                "visual_matches": [],
                "related_searches": [],
                "lens_url": lens_result.lens_url,
                "error": lens_result.error,
            }, ensure_ascii=False)

            display_text = lens_result.to_display_text()

            self.result_ready.emit(
                lens_result.lens_url,
                display_text,
                gemini_info,
                result_json,
            )

        except Exception as e:
            import traceback
            self.error_occurred.emit(f"{e}\n{traceback.format_exc()[-600:]}")
