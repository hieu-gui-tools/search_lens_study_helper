"""
Capture module: overlay window for region selection.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import ceil, floor

from PySide6.QtWidgets import QWidget, QApplication, QRubberBand
from PySide6.QtCore import Qt, QRect, QPoint, QSize, Signal
from PySide6.QtGui import QPixmap, QPainter, QColor
import mss
from PIL import Image


@dataclass(frozen=True)
class _DesktopRect:
    left: float
    top: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.left + self.width

    @property
    def bottom(self) -> float:
        return self.top + self.height


@dataclass(frozen=True)
class _ScreenMap:
    logical: _DesktopRect
    physical: _DesktopRect


def _intersect_rect(a: _DesktopRect, b: _DesktopRect) -> _DesktopRect | None:
    left = max(a.left, b.left)
    top = max(a.top, b.top)
    right = min(a.right, b.right)
    bottom = min(a.bottom, b.bottom)

    if right <= left or bottom <= top:
        return None

    return _DesktopRect(left, top, right - left, bottom - top)


def _round_physical_rect(
    left: float,
    top: float,
    right: float,
    bottom: float,
) -> _DesktopRect:
    rounded_left = floor(left)
    rounded_top = floor(top)
    rounded_right = ceil(right)
    rounded_bottom = ceil(bottom)

    return _DesktopRect(
        rounded_left,
        rounded_top,
        max(1, rounded_right - rounded_left),
        max(1, rounded_bottom - rounded_top),
    )


def _logical_to_physical_rects(
    rect: _DesktopRect,
    screen_maps: list[_ScreenMap],
) -> list[_DesktopRect]:
    physical_rects: list[_DesktopRect] = []

    for screen_map in screen_maps:
        logical_part = _intersect_rect(rect, screen_map.logical)
        if logical_part is None:
            continue

        scale_x = screen_map.physical.width / screen_map.logical.width
        scale_y = screen_map.physical.height / screen_map.logical.height
        physical_left = (
            screen_map.physical.left
            + (logical_part.left - screen_map.logical.left) * scale_x
        )
        physical_top = (
            screen_map.physical.top
            + (logical_part.top - screen_map.logical.top) * scale_y
        )
        physical_right = (
            screen_map.physical.left
            + (logical_part.right - screen_map.logical.left) * scale_x
        )
        physical_bottom = (
            screen_map.physical.top
            + (logical_part.bottom - screen_map.logical.top) * scale_y
        )

        physical_rects.append(
            _round_physical_rect(physical_left, physical_top, physical_right, physical_bottom)
        )

    return physical_rects


def _qrect_to_desktop_rect(rect: QRect) -> _DesktopRect:
    return _DesktopRect(rect.x(), rect.y(), rect.width(), rect.height())


def _monitor_to_desktop_rect(monitor: dict[str, int]) -> _DesktopRect:
    return _DesktopRect(
        monitor["left"],
        monitor["top"],
        monitor["width"],
        monitor["height"],
    )


class RegionSelector(QWidget):
    """Full-screen transparent overlay for drag-select region capture."""

    region_captured = Signal(object)  # emits PIL.Image

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setCursor(Qt.CursorShape.CrossCursor)

        # Cover ALL screens
        screens = QApplication.screens()
        total_rect = screens[0].geometry()
        for s in screens[1:]:
            total_rect = total_rect.united(s.geometry())
        self.setGeometry(total_rect)

        self._origin: QPoint | None = None
        self._rubber = QRubberBand(QRubberBand.Shape.Rectangle, self)
        self._backgrounds: list[tuple[QRect, QPixmap]] = []
        self._take_background()

    def _take_background(self):
        """Grab each screen as a frozen background in overlay-local coordinates."""
        origin = self.geometry().topLeft()
        self._backgrounds.clear()

        for screen in QApplication.screens():
            geometry = screen.geometry()
            local_geometry = geometry.translated(-origin.x(), -origin.y())
            self._backgrounds.append((local_geometry, screen.grabWindow(0)))

    def paintEvent(self, event):
        painter = QPainter(self)
        for geometry, pixmap in self._backgrounds:
            painter.drawPixmap(geometry, pixmap)
        # Dark semi-transparent overlay
        painter.fillRect(self.rect(), QColor(0, 0, 0, 100))

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.position().toPoint()
            self._rubber.setGeometry(QRect(self._origin, QSize()))
            self._rubber.show()

    def mouseMoveEvent(self, event):
        if self._origin is not None:
            rect = QRect(self._origin, event.position().toPoint()).normalized()
            self._rubber.setGeometry(rect)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._origin is not None:
            rect = QRect(self._origin, event.position().toPoint()).normalized()
            self._rubber.hide()
            self.hide()
            QApplication.processEvents()

            if rect.width() > 5 and rect.height() > 5:
                img = self._capture_region(rect)
                self.region_captured.emit(img)

            self.close()

    def _capture_region(self, rect: QRect) -> Image.Image:
        with mss.mss() as sct:
            logical_rect = self._local_to_logical_desktop_rect(rect)
            physical_rects = _logical_to_physical_rects(
                logical_rect,
                self._screen_maps(sct.monitors[1:]),
            )

            if not physical_rects:
                physical_rects = [logical_rect]

            return self._grab_physical_rects(sct, physical_rects)

    def _local_to_logical_desktop_rect(self, rect: QRect) -> _DesktopRect:
        origin = self.geometry().topLeft()
        return _DesktopRect(
            origin.x() + rect.x(),
            origin.y() + rect.y(),
            rect.width(),
            rect.height(),
        )

    def _screen_maps(self, mss_monitors: list[dict[str, int]]) -> list[_ScreenMap]:
        screens = QApplication.screens()
        remaining_monitors = list(mss_monitors)
        primary_screen = QApplication.primaryScreen()
        screen_maps: list[_ScreenMap] = []

        for screen in screens:
            logical = _qrect_to_desktop_rect(screen.geometry())
            fallback_physical = self._fallback_physical_rect(screen)

            if remaining_monitors:
                monitor_index = self._best_monitor_index(
                    screen is primary_screen,
                    fallback_physical,
                    remaining_monitors,
                )
                physical = _monitor_to_desktop_rect(remaining_monitors.pop(monitor_index))
            else:
                physical = fallback_physical

            screen_maps.append(_ScreenMap(logical=logical, physical=physical))

        return screen_maps

    def _fallback_physical_rect(self, screen) -> _DesktopRect:
        geometry = screen.geometry()
        ratio = screen.devicePixelRatio()
        return _DesktopRect(
            round(geometry.x() * ratio),
            round(geometry.y() * ratio),
            round(geometry.width() * ratio),
            round(geometry.height() * ratio),
        )

    def _best_monitor_index(
        self,
        is_primary_screen: bool,
        fallback_physical: _DesktopRect,
        monitors: list[dict[str, int]],
    ) -> int:
        def score(index: int) -> float:
            monitor = monitors[index]
            primary_penalty = (
                0
                if bool(monitor.get("is_primary", False)) == is_primary_screen
                else 1_000_000
            )
            return (
                primary_penalty
                + abs(monitor["width"] - fallback_physical.width)
                + abs(monitor["height"] - fallback_physical.height)
            )

        return min(range(len(monitors)), key=score)

    def _grab_physical_rects(self, sct: mss.mss, rects: list[_DesktopRect]) -> Image.Image:
        left = int(min(rect.left for rect in rects))
        top = int(min(rect.top for rect in rects))
        right = int(max(rect.right for rect in rects))
        bottom = int(max(rect.bottom for rect in rects))

        image = Image.new("RGB", (right - left, bottom - top))
        for rect in rects:
            monitor = {
                "left": int(rect.left),
                "top": int(rect.top),
                "width": int(rect.width),
                "height": int(rect.height),
            }
            raw = sct.grab(monitor)
            part = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
            image.paste(part, (int(rect.left) - left, int(rect.top) - top))

        return image
