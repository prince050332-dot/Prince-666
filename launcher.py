"""HoYoPlay-inspired Game-Style Patch Launcher for Pulse Studio."""
from __future__ import annotations

from bisect import bisect_right
from datetime import datetime
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import warnings

from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, QThread, QTimer, QUrl, Signal
try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
except ImportError:
    QAudioOutput = None
    QMediaPlayer = None
    QVideoSink = None
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontDatabase,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

import config
import ui_icons
import ui_theme
import updater
from version import APP_NAME, APP_VERSION, LAUNCHER_VERSION


def _default_install_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _format_bytes(num_bytes: int) -> str:
    val = float(max(0, num_bytes))
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024.0 or unit == "GB":
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024.0
    return f"{val:.1f} GB"


_ORPHAN_LAUNCHER_WORKERS: set[QThread] = set()


class _LauncherWorker(QThread):
    done = Signal(object)
    failed = Signal(str)
    progressed = Signal(int, int, str, int, int, float)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            self.done.emit(self._fn(self.progressed.emit))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


def _retire_launcher_worker(worker: _LauncherWorker | None, wait_ms: int = 3000) -> None:
    if worker is None or not worker.isRunning():
        return
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        for sig in (worker.done, worker.failed, worker.progressed):
            try:
                sig.disconnect()
            except (RuntimeError, TypeError):
                pass
    worker.quit()
    if not worker.wait(wait_ms):
        _ORPHAN_LAUNCHER_WORKERS.add(worker)
        worker.finished.connect(lambda w=worker: _ORPHAN_LAUNCHER_WORKERS.discard(w))


class PatchNotesDialog(QDialog):
    """Full scrollable Patch Notes modal triggered by the 'รายละเอียดแพตช์' pill button."""

    def __init__(self, title_text: str, notes_text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title_text)
        self.setMinimumSize(620, 440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        lbl = QLabel(title_text)
        lbl.setObjectName("pageHeading")
        layout.addWidget(lbl)

        viewer = QPlainTextEdit()
        viewer.setReadOnly(True)
        viewer.setPlainText(notes_text)
        layout.addWidget(viewer, 1)

        btn_close = QPushButton("ปิดหน้าต่าง")
        btn_close.setObjectName("primaryBtn")
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(btn_close)
        layout.addLayout(row)


BGM_CUE_POINTS_MS: tuple[int, ...] = (
    0,        # 0:00.0 - Gentle harp/celesta intro -> Slide 1
    19000,    # 0:19.0 - Orchestral strings swell -> Slide 2
    41000,    # 0:41.0 - Solo violin theme -> Slide 3
    61000,    # 1:01.0 - Ethereal vocal/choir swell -> Slide 4
    82000,    # 1:22.0 - Rhythmic orchestral build -> Slide 5
    106500,   # 1:46.5 - Grand main theme climax -> Slide 1
    127000,   # 2:07.0 - Violin & soaring bridge -> Slide 2
    145000,   # 2:25.0 - Dramatic choir peak -> Slide 3
    164000,   # 2:44.0 - Brass/choir resolution -> Slide 4
    183500,   # 3:03.5 - Quiet harp/celesta interlude -> Slide 5
    208000,   # 3:28.0 - Warm gentle outro strings -> Slide 1
)
CROSSFADE_DURATION_MS = 2400
INITIAL_FADE_DURATION_MS = 1200
FALLBACK_SLIDE_INTERVAL_MS = 18000
_SLIDE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
_VIDEO_EXTENSIONS = (".mp4", ".webm", ".mkv", ".mov")
_AUDIO_EXTENSIONS = (".mp3", ".wav", ".ogg", ".m4a")


def cover_source_rect(img_w: int, img_h: int, dst_w: int, dst_h: int) -> QRectF:
    """Compute a centered aspect-fill (cover) crop rectangle inside an image."""
    if img_w <= 0 or img_h <= 0 or dst_w <= 0 or dst_h <= 0:
        return QRectF(0.0, 0.0, max(0.0, float(img_w)), max(0.0, float(img_h)))
    src_ratio = img_w / float(img_h)
    dst_ratio = dst_w / float(dst_h)
    if src_ratio > dst_ratio:
        crop_w = img_h * dst_ratio
        crop_x = (img_w - crop_w) * 0.5
        return QRectF(crop_x, 0.0, crop_w, float(img_h))
    crop_h = img_w / dst_ratio
    crop_y = (img_h - crop_h) * 0.5
    return QRectF(0.0, crop_y, float(img_w), crop_h)


def cue_slot_for_position_ms(
    pos_ms: int,
    cue_points: tuple[int, ...] = BGM_CUE_POINTS_MS,
) -> int:
    """Return the active musical cue slot index for a given playback position in ms."""
    if not cue_points:
        return 0
    safe_pos = max(0, int(pos_ms))
    idx = bisect_right(cue_points, safe_pos) - 1
    return max(0, idx)


def slide_index_for_position_ms(
    pos_ms: int,
    slide_count: int,
    cue_points: tuple[int, ...] = BGM_CUE_POINTS_MS,
) -> int:
    """Map an audio playback position (ms) to a slide index synced with musical cues."""
    if slide_count <= 0:
        return 0
    return cue_slot_for_position_ms(pos_ms, cue_points) % slide_count


def ease_in_out_cosine(t: float) -> float:
    """Smooth S-curve easing (0.0 -> 1.0) for gentle cinematic crossfades."""
    clamped = max(0.0, min(1.0, float(t)))
    return 0.5 - 0.5 * math.cos(math.pi * clamped)


def _find_bgm_file(install_dir: Path) -> Path | None:
    base_bundle = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    search_dirs = [install_dir, install_dir / "launcher_audio", base_bundle / "assets" / "launcher_audio"]
    for folder in search_dirs:
        if not folder.is_dir():
            continue
        for ext in _AUDIO_EXTENSIONS:
            cand = folder / f"launcher_bgm{ext}"
            if cand.is_file():
                return cand
    return None


class _HeroCanvas(QWidget):
    """Full-bleed HoYoPlay-style hero backdrop with live video / slideshow crossfade synced to BGM."""

    slide_changed = Signal(int)

    def __init__(self, install_dir: Path, parent=None):
        super().__init__(parent)
        self._install_dir = install_dir
        self._videos: list[Path] = self._load_videos()
        self._slides: list[QPixmap] = [] if self._videos else self._load_slides()
        self._current_idx: int = 0
        self._prev_idx: int | None = None
        self._current_frame: QImage | None = None
        self._prev_frame: QImage | None = None
        self._awaiting_new_video_frame: bool = False
        self._video_player = None
        self._video_sink = None
        self._fade_progress: float = 1.0
        self._fade_duration_ms: int = CROSSFADE_DURATION_MS
        self._fade_started_mono: float = time.monotonic()
        self._last_cue_slot: int | None = None
        self._scaled_cache_size: tuple[int, int] = (0, 0)
        self._scaled_cache: dict[int, QPixmap] = {}
        self._chip_version: str = f"LIVE STUDIO v{APP_VERSION}"

        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(33)
        self._fade_timer.timeout.connect(self._on_fade_tick)

        if self._videos and QMediaPlayer is not None and QVideoSink is not None:
            player = QMediaPlayer(self)
            sink = QVideoSink(self)
            player.setVideoSink(sink)
            player.setLoops(1 if len(self._videos) > 1 else QMediaPlayer.Infinite)
            sink.videoFrameChanged.connect(self._on_video_frame)
            player.mediaStatusChanged.connect(self._on_video_media_status)
            self._video_player = player
            self._video_sink = sink
            if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
                self._fade_progress = 0.0
                self._fade_duration_ms = INITIAL_FADE_DURATION_MS
                self._awaiting_new_video_frame = True
                player.setSource(QUrl.fromLocalFile(str(self._videos[0])))
                player.play()
        elif self._slides and os.environ.get("QT_QPA_PLATFORM") != "offscreen":
            # Gentle opening fade-in synced to the first harp notes
            self._fade_progress = 0.0
            self._fade_duration_ms = INITIAL_FADE_DURATION_MS
            self._fade_started_mono = time.monotonic()
            self._fade_timer.start()

    @property
    def has_videos(self) -> bool:
        return bool(self._videos)

    @property
    def slide_count(self) -> int:
        return len(self._videos) if self._videos else len(self._slides)

    @property
    def current_slide_index(self) -> int:
        return self._current_idx

    def set_chip_version(self, ver_text: str) -> None:
        self._chip_version = ver_text
        self.update()

    def stop_animations(self) -> None:
        self._fade_timer.stop()
        self._awaiting_new_video_frame = False
        if self._video_player is not None:
            self._video_player.stop()
            self._video_player.setVideoSink(None)

    def _load_videos(self) -> list[Path]:
        base_bundle = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        for folder in (self._install_dir / "launcher_videos", base_bundle / "assets" / "launcher_videos"):
            if not folder.is_dir():
                continue
            found = sorted(
                p for p in folder.iterdir()
                if p.is_file() and p.suffix.lower() in _VIDEO_EXTENSIONS
            )
            if found:
                return found
        return []

    def _on_video_media_status(self, status) -> None:
        if QMediaPlayer is not None and status == QMediaPlayer.EndOfMedia:
            if len(self._videos) > 1:
                self.next_slide()

    def _on_video_frame(self, frame) -> None:
        if not frame.isValid():
            return
        img = frame.toImage()
        if img.isNull():
            return
        self._current_frame = img
        if self._awaiting_new_video_frame:
            self._awaiting_new_video_frame = False
            self._fade_started_mono = time.monotonic()
            if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
                self._fade_progress = 1.0
                self._prev_frame = None
            elif not self._fade_timer.isActive():
                self._fade_timer.start()
        self.update()

    def _load_slides(self) -> list[QPixmap]:
        base_bundle = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        # 1) Custom slides folder next to EXE
        # 2) Custom single launcher_bg.* next to EXE
        # 3) Bundled assets/launcher_slides folder
        # 4) Bundled assets/launcher_bg.*
        candidates: list[tuple[str, Path]] = [
            ("dir", self._install_dir / "launcher_slides"),
            ("single", self._install_dir),
            ("dir", base_bundle / "assets" / "launcher_slides"),
            ("single", base_bundle / "assets"),
        ]
        for kind, target in candidates:
            if not target.is_dir():
                continue
            loaded: list[QPixmap] = []
            if kind == "dir":
                files = sorted(
                    p for p in target.iterdir()
                    if p.is_file() and p.suffix.lower() in _SLIDE_EXTENSIONS
                )
                for img_path in files:
                    pix = QPixmap(str(img_path))
                    if not pix.isNull():
                        loaded.append(pix)
            else:
                for ext in _SLIDE_EXTENSIONS:
                    cand = target / f"launcher_bg{ext}"
                    if cand.is_file():
                        pix = QPixmap(str(cand))
                        if not pix.isNull():
                            loaded.append(pix)
                            break
            if loaded:
                return loaded
        return []

    def _get_scaled_slide(self, idx: int, w: int, h: int) -> QPixmap | None:
        if idx < 0 or idx >= len(self._slides) or w <= 0 or h <= 0:
            return None
        if self._scaled_cache_size != (w, h):
            self._scaled_cache.clear()
            self._scaled_cache_size = (w, h)
        cached = self._scaled_cache.get(idx)
        if cached is not None:
            return cached
        raw = self._slides[idx]
        scaled = raw.scaled(w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        sx = max(0, (scaled.width() - w) // 2)
        sy = max(0, (scaled.height() - h) // 2)
        cropped = scaled.copy(sx, sy, w, h)
        self._scaled_cache[idx] = cropped
        return cropped

    def transition_to(self, target_idx: int, duration_ms: int = CROSSFADE_DURATION_MS) -> None:
        total = self.slide_count
        if total <= 0:
            return
        norm_idx = target_idx % total
        if norm_idx == self._current_idx and self._fade_progress >= 1.0 and not self._awaiting_new_video_frame:
            return
        self._fade_duration_ms = max(1, int(duration_ms))

        if self._videos:
            if self._current_frame is not None and not self._current_frame.isNull():
                self._prev_frame = self._current_frame.copy()
            self._current_idx = norm_idx
            if os.environ.get("QT_QPA_PLATFORM") == "offscreen" or self._video_player is None:
                self._fade_progress = 1.0
                self._prev_frame = None
                self._awaiting_new_video_frame = False
            else:
                self._fade_progress = 0.0
                self._awaiting_new_video_frame = True
                self._video_player.setSource(QUrl.fromLocalFile(str(self._videos[norm_idx])))
                self._video_player.play()
            self.slide_changed.emit(self._current_idx)
            self.update()
            return

        self._prev_idx = self._current_idx
        self._current_idx = norm_idx
        self._fade_started_mono = time.monotonic()
        if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
            self._fade_progress = 1.0
            self._prev_idx = None
        else:
            self._fade_progress = 0.0
            if not self._fade_timer.isActive():
                self._fade_timer.start()
        self.slide_changed.emit(self._current_idx)
        self.update()

    def next_slide(self) -> None:
        total = self.slide_count
        if total > 1:
            self.transition_to((self._current_idx + 1) % total)

    def sync_to_audio_position(self, pos_ms: int) -> None:
        if self._videos:
            return
        total = self.slide_count
        if total <= 1:
            return
        slot = cue_slot_for_position_ms(pos_ms)
        if self._last_cue_slot is None:
            self._last_cue_slot = slot
            return
        if slot != self._last_cue_slot:
            self._last_cue_slot = slot
            self.transition_to(slot % total)

    def _on_fade_tick(self) -> None:
        elapsed_ms = (time.monotonic() - self._fade_started_mono) * 1000.0
        self._fade_progress = min(1.0, elapsed_ms / float(self._fade_duration_ms))
        if self._fade_progress >= 1.0:
            self._fade_progress = 1.0
            self._prev_idx = None
            self._prev_frame = None
            self._fade_timer.stop()
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        w, h = self.width(), self.height()
        dst_rect = QRectF(0.0, 0.0, float(w), float(h))

        if self._videos:
            p.fillRect(0, 0, w, h, QColor("#060913"))
            if self._awaiting_new_video_frame:
                if self._prev_frame is not None and not self._prev_frame.isNull():
                    src_r = cover_source_rect(self._prev_frame.width(), self._prev_frame.height(), w, h)
                    p.drawImage(dst_rect, self._prev_frame, src_r)
            else:
                eased = ease_in_out_cosine(self._fade_progress)
                if self._prev_frame is not None and not self._prev_frame.isNull() and self._fade_progress < 1.0:
                    p.setOpacity(max(0.0, 1.0 - eased))
                    src_p = cover_source_rect(self._prev_frame.width(), self._prev_frame.height(), w, h)
                    p.drawImage(dst_rect, self._prev_frame, src_p)
                if self._current_frame is not None and not self._current_frame.isNull():
                    p.setOpacity(eased)
                    src_c = cover_source_rect(self._current_frame.width(), self._current_frame.height(), w, h)
                    p.drawImage(dst_rect, self._current_frame, src_c)
                p.setOpacity(1.0)
        elif self._slides:
            p.fillRect(0, 0, w, h, QColor("#060913"))
            eased = ease_in_out_cosine(self._fade_progress)
            if self._prev_idx is not None and self._fade_progress < 1.0:
                prev_pix = self._get_scaled_slide(self._prev_idx, w, h)
                if prev_pix is not None:
                    p.setOpacity(max(0.0, 1.0 - eased))
                    p.drawPixmap(0, 0, prev_pix)
            cur_pix = self._get_scaled_slide(self._current_idx, w, h)
            if cur_pix is not None:
                p.setOpacity(eased)
                p.drawPixmap(0, 0, cur_pix)
            p.setOpacity(1.0)
        else:
            # 1) Deep midnight cosmic backdrop
            bg = QLinearGradient(0, 0, w, h)
            bg.setColorAt(0.0, QColor("#060913"))
            bg.setColorAt(0.45, QColor("#0B1226"))
            bg.setColorAt(1.0, QColor("#111735"))
            p.fillRect(0, 0, w, h, bg)

            # 2) Nocturnal blue/violet/cyan aura blooms on the right
            for cx, cy, rad, col_in, col_out in (
                (w * 0.72, h * 0.36, 360, QColor(59, 130, 246, 135), QColor(59, 130, 246, 0)),
                (w * 0.84, h * 0.58, 300, QColor(124, 58, 237, 115), QColor(124, 58, 237, 0)),
                (w * 0.63, h * 0.72, 260, QColor(6, 182, 212, 95), QColor(6, 182, 212, 0)),
                (w * 0.78, h * 0.24, 190, QColor(147, 197, 253, 85), QColor(147, 197, 253, 0)),
            ):
                rg = QRadialGradient(QPointF(cx, cy), rad)
                rg.setColorAt(0.0, col_in)
                rg.setColorAt(1.0, col_out)
                p.setBrush(rg)
                p.setPen(Qt.NoPen)
                p.drawEllipse(QPointF(cx, cy), rad, rad)

            # 3) Sweeping neon energy trails
            for offset_y, col1, col2, width_px in (
                (0, QColor(56, 189, 248, 0), QColor(96, 165, 250, 175), 3.0),
                (24, QColor(139, 92, 246, 0), QColor(167, 139, 250, 145), 2.2),
                (-26, QColor(34, 211, 238, 0), QColor(250, 204, 21, 120), 1.6),
            ):
                path = QPainterPath()
                path.moveTo(w * 0.36, h * 0.82 + offset_y)
                path.cubicTo(
                    w * 0.54, h * 0.62 + offset_y,
                    w * 0.70, h * 0.16 + offset_y,
                    w * 1.02, h * 0.34 + offset_y,
                )
                lg = QLinearGradient(w * 0.36, 0, w, 0)
                lg.setColorAt(0.0, col1)
                lg.setColorAt(0.55, col2)
                lg.setColorAt(1.0, QColor(255, 255, 255, 40))
                p.setPen(QPen(QBrush(lg), width_px))
                p.setBrush(Qt.NoBrush)
                p.drawPath(path)

            # 4) Holographic Studio Glass Card + Reels Prism on the right
            p.save()
            p.translate(w * 0.71, h * 0.44)
            p.rotate(-9)
            p.setPen(QPen(QColor(96, 165, 250, 120), 2.0))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(0, 0), 170, 170)
            p.setPen(QPen(QColor(167, 139, 250, 75), 1.2, Qt.DashLine))
            p.drawEllipse(QPointF(0, 0), 208, 208)

            # Back glass card (Reels prism)
            p.setPen(QPen(QColor(148, 163, 184, 70), 1.2))
            p.setBrush(QColor(15, 23, 42, 165))
            p.drawRoundedRect(QRectF(52, -135, 132, 200), 18, 18)
            tri = QPainterPath()
            tri.moveTo(106, -54)
            tri.lineTo(138, -35)
            tri.lineTo(106, -16)
            tri.closeSubpath()
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(56, 189, 248, 220))
            p.drawPath(tri)

            # Front hero studio dashboard glass card
            card_grad = QLinearGradient(-150, -110, 130, 120)
            card_grad.setColorAt(0.0, QColor(30, 41, 78, 215))
            card_grad.setColorAt(1.0, QColor(15, 23, 42, 225))
            p.setBrush(card_grad)
            p.setPen(QPen(QColor(96, 165, 250, 150), 1.6))
            p.drawRoundedRect(QRectF(-145, -102, 250, 182), 20, 20)

            # Blue P brand emblem inside floating card
            p.setPen(Qt.NoPen)
            p.drawPixmap(-126, -84, ui_icons.svg_pixmap("logo", "#FFFFFF", 46))

            # Holographic analytics bars
            for idx, bh in enumerate((28, 46, 38, 64, 52, 82, 72)):
                bx = -116 + idx * 30
                by = 54 - bh
                bg_bar = QLinearGradient(bx, by, bx, 54)
                bg_bar.setColorAt(0.0, QColor("#38BDF8"))
                bg_bar.setColorAt(1.0, QColor("#6366F1"))
                p.setBrush(bg_bar)
                p.drawRoundedRect(QRectF(bx, by, 18, bh), 5, 5)

            # Floating gold accent chip
            p.setBrush(QColor(250, 204, 21, 230))
            p.drawRoundedRect(QRectF(-68, -76, 134, 22), 11, 11)
            p.setPen(QColor("#0F172A"))
            p.setFont(QFont("Prompt", 8, QFont.Bold))
            p.drawText(QRectF(-68, -76, 134, 22), Qt.AlignCenter, self._chip_version)
            p.restore()

            # 5) Floating bokeh sparks
            p.setPen(Qt.NoPen)
            for px, py, pr, pcol in (
                (w * 0.53, h * 0.19, 3.0, QColor(250, 204, 21, 180)),
                (w * 0.60, h * 0.72, 2.5, QColor(56, 189, 248, 200)),
                (w * 0.87, h * 0.25, 3.5, QColor(196, 181, 253, 190)),
                (w * 0.92, h * 0.66, 2.2, QColor(250, 204, 21, 160)),
                (w * 0.48, h * 0.45, 2.0, QColor(147, 197, 253, 150)),
                (w * 0.75, h * 0.16, 2.4, QColor(250, 204, 21, 195)),
                (w * 0.82, h * 0.77, 3.2, QColor(56, 189, 248, 175)),
            ):
                p.setBrush(pcol)
                p.drawEllipse(QPointF(px, py), pr, pr)

        # 6) Left & bottom dark readability vignette so text always pops sharply
        vig = QLinearGradient(0, 0, w * 0.58, 0)
        vig.setColorAt(0.0, QColor(6, 9, 19, 238))
        vig.setColorAt(0.72, QColor(6, 9, 19, 180))
        vig.setColorAt(1.0, QColor(6, 9, 19, 0))
        p.fillRect(0, 0, int(w * 0.60), h, vig)

        bvig = QLinearGradient(0, h * 0.72, 0, h)
        bvig.setColorAt(0.0, QColor(6, 9, 19, 0))
        bvig.setColorAt(1.0, QColor(6, 9, 19, 220))
        p.fillRect(0, int(h * 0.72), w, int(h * 0.28), bvig)

        # Subtle outer window border
        p.setPen(QPen(QColor(148, 163, 184, 50), 1.0))
        p.setBrush(Qt.NoBrush)
        p.drawRect(0, 0, w - 1, h - 1)
        p.end()


class LauncherWindow(QMainWindow):
    """HoYoPlay-style frameless full-bleed Game Patch Launcher for SocialManager.exe."""

    def __init__(
        self,
        *,
        install_dir: Path | None = None,
        allow_local_http: bool = False,
    ):
        super().__init__()
        self.install_dir = (install_dir or _default_install_dir()).resolve()
        self.allow_local_http = allow_local_http
        self.current_theme = "dark"
        self.manifest: updater.PatchManifest | None = None
        self._worker: _LauncherWorker | None = None
        self._cancel_event: threading.Event | None = None
        self._is_downloading: bool = False
        self._update_available: bool = False
        self._drag_pos: QPoint | None = None
        self._bgm_player = None
        self._bgm_audio = None
        self._bgm_muted: bool = False
        self._bgm_path: Path | None = _find_bgm_file(self.install_dir)
        self._slide_dots: list[QPushButton] = []

        self.setWindowTitle(f"{APP_NAME} — Patch Launcher v{LAUNCHER_VERSION}")
        self.setWindowIcon(ui_icons.svg_icon("logo", "#FFFFFF", 32))
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.resize(1020, 580)
        self.setMinimumSize(880, 520)

        self._build_ui()
        self._apply_theme("dark")
        self._refresh_local_state()
        self._init_bgm_and_slideshow()

        cfg = updater.load_launcher_config(self.install_dir)
        if cfg.get("auto_check", True) and cfg.get("manifest_url"):
            if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
                QTimer.singleShot(250, self.check_for_updates)
        elif not cfg.get("manifest_url"):
            self.lbl_status_badge.setText("พร้อมใช้งาน")
            self.lbl_progress_status.setText(
                f"เวอร์ชันในเครื่อง v{updater.get_installed_version(self.install_dir)} — กด 'เข้าใช้งานโปรแกรม' ได้เลย"
            )

    def _build_ui(self):
        self._canvas = _HeroCanvas(self.install_dir, parent=self)
        root = QHBoxLayout(self._canvas)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # === 1) Slim Left Game Rail ===
        left_rail = QFrame()
        left_rail.setFixedWidth(60)
        left_rail.setStyleSheet(
            "QFrame { background-color: rgba(8, 12, 24, 215); border-right: 1px solid rgba(148,163,184,35); }"
        )
        lr = QVBoxLayout(left_rail)
        lr.setContentsMargins(8, 14, 8, 14)
        lr.setSpacing(10)

        top_logo = QLabel()
        top_logo.setPixmap(ui_icons.svg_pixmap("logo", "#FFFFFF", 34))
        top_logo.setAlignment(Qt.AlignCenter)
        top_logo.setStyleSheet("background: transparent; border: none;")
        lr.addWidget(top_logo)
        lr.addStretch(1)

        btn_app_rail = QPushButton()
        btn_app_rail.setFixedSize(42, 42)
        btn_app_rail.setIcon(ui_icons.svg_icon("logo", "#FFFFFF", 28))
        btn_app_rail.setIconSize(QSize(28, 28))
        btn_app_rail.setCursor(Qt.PointingHandCursor)
        btn_app_rail.setToolTip(f"{APP_NAME} — แอปหลัก")
        btn_app_rail.setStyleSheet(
            "QPushButton { background-color: #1877F2; border: 2px solid #F8FAFC; border-radius: 12px; }"
        )
        btn_app_rail.clicked.connect(lambda: self._switch_info_tab(0))
        lr.addWidget(btn_app_rail, 0, Qt.AlignCenter)

        for ic_name, tip, handler in (
            ("analytics", "ดูรายละเอียดแพตช์ทั้งหมด", self.open_patch_notes_dialog),
            ("history", "ดูรายการไฟล์ในแพตช์", lambda: self._switch_info_tab(2)),
        ):
            b = QPushButton()
            b.setFixedSize(40, 40)
            b.setIcon(ui_icons.svg_icon(ic_name, "#94A3B8", 19))
            b.setIconSize(QSize(19, 19))
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(tip)
            b.setStyleSheet(
                "QPushButton { background-color: rgba(30,41,59,170); border: 1px solid rgba(148,163,184,45); border-radius: 11px; }"
                "QPushButton:hover { background-color: rgba(51,65,85,215); border-color: #93C5FD; }"
            )
            b.clicked.connect(handler)
            lr.addWidget(b, 0, Qt.AlignCenter)

        self.btn_open_folder = QPushButton()
        self.btn_open_folder.setFixedSize(40, 40)
        self.btn_open_folder.setIcon(ui_icons.svg_icon("pages", "#94A3B8", 19))
        self.btn_open_folder.setIconSize(QSize(19, 19))
        self.btn_open_folder.setCursor(Qt.PointingHandCursor)
        self.btn_open_folder.setToolTip("เปิดโฟลเดอร์ติดตั้งโปรแกรม")
        self.btn_open_folder.setStyleSheet(
            "QPushButton { background-color: rgba(30,41,59,170); border: 1px solid rgba(148,163,184,45); border-radius: 11px; }"
            "QPushButton:hover { background-color: rgba(51,65,85,215); border-color: #93C5FD; }"
        )
        self.btn_open_folder.clicked.connect(self.open_install_folder)
        lr.addWidget(self.btn_open_folder, 0, Qt.AlignCenter)
        root.addWidget(left_rail)

        # === 2) Main Hero Overlay Area ===
        center = QVBoxLayout()
        center.setContentsMargins(34, 14, 18, 22)
        center.setSpacing(0)

        # Top-right window controls (Minimize, Close)
        win_row = QHBoxLayout()
        win_row.setSpacing(8)
        win_row.addStretch(1)

        top_btn_qss = (
            "QPushButton { background-color: rgba(15,23,42,145); color: #CBD5E1; "
            "border-radius: 15px; border: 1px solid rgba(148,163,184,40); font-weight: 700; }"
            "QPushButton:hover { background-color: rgba(51,65,85,220); color: #FFFFFF; }"
        )
        self.btn_bgm_toggle = QPushButton()
        self.btn_bgm_toggle.setFixedSize(30, 30)
        self.btn_bgm_toggle.setIcon(ui_icons.svg_icon("volume_on", "#FDE047", 15))
        self.btn_bgm_toggle.setIconSize(QSize(15, 15))
        self.btn_bgm_toggle.setCursor(Qt.PointingHandCursor)
        self.btn_bgm_toggle.setToolTip("ปิด / เปิดเสียงเพลงพื้นหลัง (BGM)")
        self.btn_bgm_toggle.setStyleSheet(top_btn_qss)
        self.btn_bgm_toggle.clicked.connect(self.toggle_bgm_mute)
        if self._bgm_path is None or QMediaPlayer is None:
            self.btn_bgm_toggle.setVisible(False)

        self.btn_minimize = QPushButton("-")
        self.btn_minimize.setFixedSize(30, 30)
        self.btn_minimize.setCursor(Qt.PointingHandCursor)
        self.btn_minimize.setToolTip("ย่อหน้าต่าง")
        self.btn_minimize.setStyleSheet(top_btn_qss)
        self.btn_minimize.clicked.connect(self.showMinimized)

        self.btn_close = QPushButton()
        self.btn_close.setFixedSize(30, 30)
        self.btn_close.setIcon(ui_icons.svg_icon("x", "#CBD5E1", 15))
        self.btn_close.setIconSize(QSize(15, 15))
        self.btn_close.setCursor(Qt.PointingHandCursor)
        self.btn_close.setToolTip("ปิด Launcher")
        self.btn_close.setStyleSheet(
            "QPushButton { background-color: rgba(15,23,42,145); border-radius: 15px; border: 1px solid rgba(148,163,184,40); }"
            "QPushButton:hover { background-color: #DC2626; border-color: #EF4444; }"
        )
        self.btn_close.clicked.connect(self.close)

        win_row.addWidget(self.btn_bgm_toggle)
        win_row.addWidget(self.btn_minimize)
        win_row.addWidget(self.btn_close)
        center.addLayout(win_row)

        # Middle split: Left Hero Info + Right Floating Quick-Action Strip
        body_row = QHBoxLayout()
        body_row.setContentsMargins(0, 4, 0, 0)

        left_col = QVBoxLayout()
        left_col.setSpacing(6)

        brand_small = QLabel(f"{APP_NAME}  •  FACEBOOK PAGE STUDIO")
        brand_small.setStyleSheet(
            "color: #93C5FD; font-size: 11px; font-weight: 700; letter-spacing: 1px; background: transparent;"
        )
        left_col.addWidget(brand_small)

        self.lbl_version_pill = QLabel(f"  VERSION {APP_VERSION}  •  STUDIO PATCH LAUNCHER  ")
        self.lbl_version_pill.setStyleSheet(
            "color: #E2E8F0; background-color: rgba(37, 99, 235, 115); "
            "border: 1px solid rgba(96, 165, 250, 145); border-radius: 11px; "
            "padding: 2px 10px; font-size: 11px; font-weight: 600;"
        )
        self.lbl_version_pill.setMaximumWidth(330)
        left_col.addWidget(self.lbl_version_pill)

        self.lbl_hero_title = QLabel(f"ศูนย์อัปเดตแพตช์ {APP_NAME}")
        self.lbl_hero_title.setStyleSheet(
            "color: #FFFFFF; font-size: 26px; font-weight: 700; background: transparent;"
        )
        left_col.addWidget(self.lbl_hero_title)

        self.lbl_hero_sub = QLabel(
            "Facebook Reels • Keyword Auto-Reply • RT Dashboard • Auto-Backup 7 วัน"
        )
        self.lbl_hero_sub.setStyleSheet(
            "color: #FDE047; font-size: 12.5px; font-weight: 600; background: transparent;"
        )
        left_col.addWidget(self.lbl_hero_sub)

        # Action pills row ("รายละเอียดแพตช์" + "ตรวจแพตช์")
        pill_row = QHBoxLayout()
        pill_row.setSpacing(8)

        self.btn_show_notes = QPushButton("รายละเอียดแพตช์")
        self.btn_show_notes.setCursor(Qt.PointingHandCursor)
        self.btn_show_notes.setStyleSheet(
            "QPushButton { color: #F8FAFC; background-color: rgba(15,23,42,165); "
            "border: 1px solid rgba(226,232,240,125); border-radius: 8px; padding: 5px 14px; font-size: 12px; font-weight: 600; }"
            "QPushButton:hover { background-color: rgba(37,99,235,160); border-color: #93C5FD; }"
        )
        self.btn_show_notes.clicked.connect(self.open_patch_notes_dialog)

        self.btn_check = QPushButton("  ตรวจแพตช์ใหม่")
        self.btn_check.setIcon(ui_icons.svg_icon("refresh", "#E2E8F0", 13))
        self.btn_check.setIconSize(QSize(13, 13))
        self.btn_check.setCursor(Qt.PointingHandCursor)
        self.btn_check.setStyleSheet(
            "QPushButton { color: #E2E8F0; background-color: rgba(15,23,42,165); "
            "border: 1px solid rgba(148,163,184,95); border-radius: 8px; padding: 5px 12px; font-size: 12px; }"
            "QPushButton:hover { background-color: rgba(51,65,85,200); border-color: #E2E8F0; }"
        )
        self.btn_check.clicked.connect(self.check_for_updates)

        pill_row.addWidget(self.btn_show_notes)
        pill_row.addWidget(self.btn_check)
        pill_row.addStretch(1)
        left_col.addSpacing(4)
        left_col.addLayout(pill_row)

        # Featured Highlight Banner Card
        left_col.addSpacing(12)
        feat_card = QFrame()
        feat_card.setFixedSize(380, 108)
        feat_card.setStyleSheet(
            "QFrame { border-radius: 14px; border: 1px solid rgba(250, 204, 21, 135); "
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 rgba(30,58,138,220), stop:0.62 rgba(15,23,42,225), stop:1 rgba(180,83,9,185)); }"
        )
        fc_lay = QVBoxLayout(feat_card)
        fc_lay.setContentsMargins(16, 11, 16, 11)
        fc_lay.setSpacing(3)

        self.lbl_feat_tag = QLabel("PATCH HIGHLIGHT  •  SHA-256 VERIFIED")
        self.lbl_feat_tag.setStyleSheet(
            "color: #FDE047; font-size: 10px; font-weight: 700; background: transparent; border: none;"
        )
        self.lbl_feat_title = QLabel("พร้อมตรวจและติดตั้งแพตช์อัตโนมัติ")
        self.lbl_feat_title.setStyleSheet(
            "color: #FFFFFF; font-size: 15px; font-weight: 700; background: transparent; border: none;"
        )
        self.lbl_feat_desc = QLabel(
            "ข้อมูลเพจ ฐานข้อมูล (data.db) และ Token ในเครื่องจะถูกเก็บรักษาไว้ 100% ทุกครั้งที่อัปเดต"
        )
        self.lbl_feat_desc.setWordWrap(True)
        self.lbl_feat_desc.setStyleSheet(
            "color: #CBD5E1; font-size: 11px; background: transparent; border: none;"
        )
        fc_lay.addWidget(self.lbl_feat_tag)
        fc_lay.addWidget(self.lbl_feat_title)
        fc_lay.addWidget(self.lbl_feat_desc)
        left_col.addWidget(feat_card)

        # Interactive Tabbed Info Card (อัปเดตล่าสุด | ประกาศ | ไฟล์แพตช์)
        left_col.addSpacing(10)
        news_card = QFrame()
        news_card.setFixedSize(380, 146)
        news_card.setStyleSheet(
            "QFrame#newsCard { background-color: rgba(8, 12, 24, 185); "
            "border-radius: 12px; border: 1px solid rgba(148,163,184,42); }"
        )
        news_card.setObjectName("newsCard")
        nc = QVBoxLayout(news_card)
        nc.setContentsMargins(14, 9, 14, 10)
        nc.setSpacing(6)

        tab_row = QHBoxLayout()
        tab_row.setSpacing(14)
        self._info_tab_buttons: list[QPushButton] = []
        for idx, t_lbl in enumerate(("อัปเดตล่าสุด", "สถานะเวอร์ชัน", "ไฟล์แพตช์")):
            tb = QPushButton(t_lbl)
            tb.setCursor(Qt.PointingHandCursor)
            tb.setFlat(True)
            tb.clicked.connect(lambda _=False, i=idx: self._switch_info_tab(i))
            self._info_tab_buttons.append(tb)
            tab_row.addWidget(tb)
        tab_row.addStretch(1)
        nc.addLayout(tab_row)

        self._info_stack = QStackedWidget()
        self._info_stack.setStyleSheet("background: transparent; border: none;")

        # Tab 0: Compact Patch Notes Highlights
        tab_notes = QWidget()
        tab_notes.setStyleSheet("background: transparent; border: none;")
        tn_lay = QVBoxLayout(tab_notes)
        tn_lay.setContentsMargins(0, 2, 0, 0)
        tn_lay.setSpacing(4)
        self.txt_patch_notes = QPlainTextEdit()
        self.txt_patch_notes.setReadOnly(True)
        self.txt_patch_notes.setFrameShape(QFrame.NoFrame)
        self.txt_patch_notes.setStyleSheet(
            "QPlainTextEdit { background: transparent; color: #E2E8F0; border: none; font-size: 11.5px; }"
        )
        self.txt_patch_notes.setPlainText(
            "• กด 'ตรวจแพตช์ใหม่' เพื่อเช็กเวอร์ชันล่าสุดจากเซิร์ฟเวอร์\n"
            "• หากมีแพตช์ใหม่ กรุณากดปุ่ม 'อัปเดตแพตช์' ก่อนเข้าใช้งานโปรแกรม\n"
            "• ตรวจสอบความถูกต้อง SHA-256 ทุกไฟล์อัตโนมัติ ข้อมูลเดิมไม่สูญหาย"
        )
        tn_lay.addWidget(self.txt_patch_notes)
        self._info_stack.addWidget(tab_notes)

        # Tab 1: Version & Release Status
        tab_status = QWidget()
        tab_status.setStyleSheet("background: transparent; border: none;")
        ts_lay = QVBoxLayout(tab_status)
        ts_lay.setContentsMargins(0, 4, 0, 0)
        ts_lay.setSpacing(5)
        self.lbl_local_ver = QLabel("ในเครื่อง: -")
        self.lbl_remote_ver = QLabel("เซิร์ฟเวอร์: -")
        self.lbl_release_date = QLabel("วันที่ปล่อยแพตช์: -")
        self.lbl_status_badge = QLabel("พร้อมตรวจสอบแพตช์")
        for lbl in (
            self.lbl_local_ver,
            self.lbl_remote_ver,
            self.lbl_release_date,
            self.lbl_status_badge,
        ):
            lbl.setStyleSheet("color: #E2E8F0; font-size: 11.5px; background: transparent; border: none;")
            ts_lay.addWidget(lbl)
        ts_lay.addStretch(1)
        self._info_stack.addWidget(tab_status)

        # Tab 2: Patch Files List
        tab_files = QWidget()
        tab_files.setStyleSheet("background: transparent; border: none;")
        tf_lay = QVBoxLayout(tab_files)
        tf_lay.setContentsMargins(0, 2, 0, 0)
        self.list_patch_files = QListWidget()
        self.list_patch_files.setFrameShape(QFrame.NoFrame)
        self.list_patch_files.setStyleSheet(
            "QListWidget { background: transparent; color: #E2E8F0; border: none; font-size: 11.5px; }"
            "QListWidget::item { padding: 2px 0; }"
        )
        tf_lay.addWidget(self.list_patch_files)
        self._info_stack.addWidget(tab_files)

        nc.addWidget(self._info_stack, 1)
        left_col.addWidget(news_card)

        # Subtle clickable slide indicator dots (when multiple hero artworks exist)
        if self._canvas.slide_count > 1:
            dots_row = QHBoxLayout()
            dots_row.setContentsMargins(2, 8, 0, 0)
            dots_row.setSpacing(6)
            for s_idx in range(self._canvas.slide_count):
                dot = QPushButton()
                dot.setCursor(Qt.PointingHandCursor)
                media_lbl = "วิดีโอพื้นหลัง" if self._canvas.has_videos else "ภาพพื้นหลัง"
                dot.setToolTip(f"สลับไปยัง{media_lbl}ที่ {s_idx + 1}")
                dot.clicked.connect(lambda _=False, idx=s_idx: self._canvas.transition_to(idx))
                self._slide_dots.append(dot)
                dots_row.addWidget(dot)
            dots_row.addStretch(1)
            left_col.addLayout(dots_row)
            self._canvas.slide_changed.connect(self._update_slide_dots)
            self._update_slide_dots(self._canvas.current_slide_index)

        left_col.addStretch(1)
        self._switch_info_tab(0)

        body_row.addLayout(left_col, 1)

        # Right floating circular quick-action strip
        right_rail = QVBoxLayout()
        right_rail.setSpacing(10)
        right_rail.addStretch(1)
        right_btn_qss = (
            "QPushButton { background-color: rgba(15,23,42,165); border-radius: 17px; "
            "border: 1px solid rgba(148,163,184,55); }"
            "QPushButton:hover { background-color: rgba(37,99,235,180); border-color: #93C5FD; }"
        )
        for ic_name, tip, handler in (
            ("link", "เปิดคู่มือการใช้งาน (.txt)", self.open_manual_file),
            ("refresh", "ตรวจแพตช์ใหม่ทันที", self.check_for_updates),
            ("comment", "ดูบันทึกการอัปเดตทั้งหมด", self.open_patch_notes_dialog),
            ("pages", "เปิดโฟลเดอร์ติดตั้ง", self.open_install_folder),
        ):
            rb = QPushButton()
            rb.setFixedSize(34, 34)
            rb.setIcon(ui_icons.svg_icon(ic_name, "#E2E8F0", 16))
            rb.setIconSize(QSize(16, 16))
            rb.setCursor(Qt.PointingHandCursor)
            rb.setToolTip(tip)
            rb.setStyleSheet(right_btn_qss)
            rb.clicked.connect(handler)
            right_rail.addWidget(rb)
        right_rail.addStretch(1)
        body_row.addLayout(right_rail)
        center.addLayout(body_row, 1)

        # === 3) Bottom Action Deck (Progress Left + HoYoPlay Golden CTA Right) ===
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(12)

        prog_box = QVBoxLayout()
        prog_box.setSpacing(5)
        p_info = QHBoxLayout()
        self.lbl_progress_status = QLabel("พร้อมใช้งาน")
        self.lbl_progress_status.setStyleSheet(
            "color: #E2E8F0; font-size: 11.5px; font-weight: 600; background: transparent;"
        )
        self.lbl_progress_detail = QLabel("")
        self.lbl_progress_detail.setStyleSheet(
            "color: #38BDF8; font-size: 11.5px; font-weight: 700; background: transparent;"
        )
        p_info.addWidget(self.lbl_progress_status, 1)
        p_info.addWidget(self.lbl_progress_detail)
        prog_box.addLayout(p_info)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(8)
        self.progress_bar.setMinimumWidth(340)
        self.progress_bar.setStyleSheet(
            "QProgressBar { border-radius: 4px; background-color: rgba(148,163,184,55); }"
            "QProgressBar::chunk { border-radius: 4px; "
            "background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #38BDF8, stop:1 #FACC15); }"
        )
        prog_box.addWidget(self.progress_bar)
        bottom_row.addLayout(prog_box, 1)

        self.btn_update = QPushButton("  อัปเดตแพตช์")
        self.btn_update.setIcon(ui_icons.svg_icon("refresh", "#FFFFFF", 16))
        self.btn_update.setIconSize(QSize(16, 16))
        self.btn_update.setCursor(Qt.PointingHandCursor)
        self.btn_update.setFixedHeight(48)
        self.btn_update.setMinimumWidth(168)
        self.btn_update.setEnabled(False)
        self.btn_update.setStyleSheet(
            "QPushButton { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #2563EB, stop:1 #06B6D4); "
            "color: #FFFFFF; font-size: 14px; font-weight: 700; border-radius: 24px; border: none; padding: 0 18px; }"
            "QPushButton:hover { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #1D4ED8, stop:1 #0891B2); }"
            "QPushButton:disabled { background: rgba(51,65,85,155); color: #94A3B8; }"
        )
        self.btn_update.clicked.connect(self.start_or_cancel_update)

        self.btn_launch = QPushButton("  เข้าใช้งานโปรแกรม")
        self.btn_launch.setIcon(ui_icons.svg_icon("send", "#111827", 18))
        self.btn_launch.setIconSize(QSize(18, 18))
        self.btn_launch.setCursor(Qt.PointingHandCursor)
        self.btn_launch.setFixedSize(218, 50)
        self.btn_launch.setStyleSheet(
            "QPushButton { background-color: #FACC15; color: #111827; font-size: 15.5px; font-weight: 700; "
            "border-radius: 25px; border: none; padding: 0 18px; }"
            "QPushButton:hover { background-color: #FDE047; }"
            "QPushButton:disabled { background-color: rgba(71,85,105,190); color: #CBD5E1; }"
        )
        self.btn_launch.clicked.connect(self.launch_app)

        bottom_row.addWidget(self.btn_update)
        bottom_row.addWidget(self.btn_launch)
        center.addLayout(bottom_row)

        root.addLayout(center, 1)
        self.setCentralWidget(self._canvas)

    def _switch_info_tab(self, index: int):
        self._info_stack.setCurrentIndex(index)
        for idx, tb in enumerate(self._info_tab_buttons):
            if idx == index:
                tb.setStyleSheet(
                    "QPushButton { color: #FFFFFF; font-size: 12px; font-weight: 700; "
                    "border: none; border-bottom: 2px solid #FACC15; padding: 0 2px 3px 2px; background: transparent; }"
                )
            else:
                tb.setStyleSheet(
                    "QPushButton { color: #94A3B8; font-size: 12px; font-weight: 500; "
                    "border: none; padding: 0 2px 3px 2px; background: transparent; }"
                    "QPushButton:hover { color: #E2E8F0; }"
                )

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and (event.buttons() & Qt.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    def _apply_theme(self, theme: str):
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(ui_theme.get_stylesheet(theme))

    def _target_exe_path(self) -> Path:
        cfg = updater.load_launcher_config(self.install_dir)
        target_name = cfg.get("target_exe") or updater.DEFAULT_TARGET_EXE
        return self.install_dir / target_name

    def _refresh_local_state(self):
        cfg = updater.load_launcher_config(self.install_dir)
        target_name = cfg.get("target_exe") or updater.DEFAULT_TARGET_EXE
        exe_exists = (self.install_dir / target_name).is_file()
        local_ver = updater.get_installed_version(self.install_dir, target_name)
        if exe_exists:
            self.lbl_local_ver.setText(f"ในเครื่อง: v{local_ver}")
        else:
            self.lbl_local_ver.setText("ในเครื่อง: ยังไม่ได้ติดตั้ง")

        # Always require updating first whenever a newer patch is detected
        if self._update_available:
            self.btn_launch.setEnabled(False)
            self.btn_launch.setToolTip("พบแพตช์ใหม่ กรุณากดปุ่ม 'อัปเดตแพตช์' ก่อนเข้าใช้งานโปรแกรม")
        elif not exe_exists:
            self.btn_launch.setEnabled(False)
            self.btn_launch.setToolTip(f"ยังไม่พบไฟล์ {target_name} กรุณากดดาวน์โหลดแพตช์ก่อน")
        else:
            self.btn_launch.setEnabled(not self._is_downloading)
            self.btn_launch.setToolTip(f"เปิดโปรแกรม {APP_NAME} ({target_name})")

    def open_patch_notes_dialog(self):
        ver = f"v{self.manifest.version}" if self.manifest is not None else f"v{APP_VERSION}"
        dlg = PatchNotesDialog(
            f"บันทึกการอัปเดต {APP_NAME} ({ver})",
            self.txt_patch_notes.toPlainText(),
            parent=self,
        )
        dlg.exec()

    def open_manual_file(self):
        manual_path = self.install_dir / "คู่มือการใช้งาน.txt"
        if not manual_path.is_file():
            QMessageBox.information(
                self,
                "ยังไม่พบไฟล์คู่มือ",
                "ยังไม่พบไฟล์ คู่มือการใช้งาน.txt ในโฟลเดอร์ติดตั้ง (จะถูกดาวน์โหลดให้อัตโนมัติเมื่ออัปเดตแพตช์)",
            )
            return
        try:
            if sys.platform == "win32":
                os.startfile(str(manual_path))  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", str(manual_path)])
        except OSError as exc:
            QMessageBox.warning(self, "เปิดคู่มือไม่สำเร็จ", str(exc))

    def open_install_folder(self):
        try:
            self.install_dir.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(str(self.install_dir))  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", str(self.install_dir)])
        except OSError as exc:
            QMessageBox.warning(self, "เปิดโฟลเดอร์ไม่สำเร็จ", str(exc))

    def check_for_updates(self):
        if self._worker is not None and self._worker.isRunning():
            return
        cfg = updater.load_launcher_config(self.install_dir)
        manifest_url = (cfg.get("manifest_url") or "").strip()
        if not manifest_url:
            self.lbl_status_badge.setText("พร้อมใช้งาน")
            self.lbl_progress_status.setText(
                f"เวอร์ชันในเครื่อง v{updater.get_installed_version(self.install_dir)} — กด 'เข้าใช้งานโปรแกรม' ได้เลย"
            )
            return

        self.btn_check.setEnabled(False)
        self.btn_update.setEnabled(False)
        self.lbl_status_badge.setText("กำลังเช็กแพตช์…")
        self.lbl_progress_status.setText("กำลังเชื่อมต่อเซิร์ฟเวอร์อัปเดตเพื่อตรวจเวอร์ชันล่าสุด…")
        self.lbl_progress_detail.setText("")
        gh_tok = updater.get_github_token()

        def job(_emit_progress):
            return updater.fetch_patch_manifest(
                manifest_url,
                install_dir=self.install_dir,
                github_token=gh_tok,
                allow_local_http=self.allow_local_http,
            )

        w = _LauncherWorker(job)
        self._worker = w

        def on_ok(mf: updater.PatchManifest):
            if self._worker is w:
                self._worker = None
            self.btn_check.setEnabled(True)
            self._apply_manifest_to_ui(mf)

        def on_err(err_msg: str):
            if self._worker is w:
                self._worker = None
            self.btn_check.setEnabled(True)
            self.lbl_status_badge.setText("ตรวจแพตช์ไม่สำเร็จ")
            self.lbl_progress_status.setText(err_msg)
            self._refresh_local_state()

        w.done.connect(on_ok)
        w.failed.connect(on_err)
        w.finished.connect(w.deleteLater)
        w.start()

    def _apply_manifest_to_ui(self, mf: updater.PatchManifest):
        self.manifest = mf
        cfg = updater.load_launcher_config(self.install_dir)
        target_name = cfg.get("target_exe") or updater.DEFAULT_TARGET_EXE
        exe_exists = (self.install_dir / target_name).is_file()
        local_ver = updater.get_installed_version(self.install_dir, target_name)
        is_newer = (not exe_exists) or updater.is_newer_version(mf.version, local_ver)
        self._update_available = is_newer

        self.lbl_version_pill.setText(f"  VERSION {mf.version}  •  STUDIO UPDATE  ")
        self._canvas.set_chip_version(f"LIVE STUDIO v{mf.version}")
        self.lbl_remote_ver.setText(f"เซิร์ฟเวอร์: v{mf.version}")
        if mf.released_at:
            try:
                dt = datetime.fromisoformat(mf.released_at.replace("Z", "+00:00"))
                self.lbl_release_date.setText(f"ปล่อยแพตช์: {dt.astimezone().strftime('%Y-%m-%d %H:%M')}")
            except ValueError:
                self.lbl_release_date.setText(f"ปล่อยแพตช์: {mf.released_at[:19]}")
        else:
            self.lbl_release_date.setText("ปล่อยแพตช์: ล่าสุด")

        notes_text = mf.notes.strip() or "ไม่มีบันทึกรายละเอียดแพตช์เพิ่มเติม"
        self.txt_patch_notes.setPlainText(f"เวอร์ชัน v{mf.version}\n{'-' * 40}\n{notes_text}")

        self.list_patch_files.clear()
        total_size = 0
        for f in mf.files:
            total_size += f.size
            size_txt = f" ({_format_bytes(f.size)})" if f.size > 0 else ""
            item = QListWidgetItem(f"{f.name}{size_txt}")
            if f.sha256:
                item.setIcon(ui_icons.svg_icon("check", "#22C55E", 14))
                item.setToolTip(f"SHA-256: {f.sha256}")
            self.list_patch_files.addItem(item)

        self._info_tab_buttons[2].setText(f"ไฟล์แพตช์ ({len(mf.files)})")

        if is_newer:
            if not exe_exists:
                self.lbl_status_badge.setText("พร้อมดาวน์โหลดติดตั้งครั้งแรก")
                self.lbl_feat_title.setText(f"ติดตั้ง {APP_NAME} เวอร์ชัน v{mf.version}")
                self.lbl_feat_desc.setText(
                    f"ชุดติดตั้งครบ {len(mf.files)} ไฟล์ ({_format_bytes(total_size)}) พร้อมตรวจ SHA-256 อัตโนมัติ"
                )
                self.btn_update.setText(f"  ติดตั้ง v{mf.version}")
                self.lbl_progress_status.setText(
                    f"พร้อมดาวน์โหลดติดตั้ง v{mf.version} ({len(mf.files)} ไฟล์)"
                )
            else:
                tag = " (บังคับอัปเดต)" if mf.force_update else ""
                self.lbl_status_badge.setText(f"พบแพตช์ใหม่ v{mf.version}{tag}")
                self.lbl_feat_title.setText(f"พบแพตช์ใหม่ v{mf.version}{tag}")
                self.lbl_feat_desc.setText(
                    f"อัปเดต {len(mf.files)} ไฟล์ ({_format_bytes(total_size)}) • ข้อมูลเพจและฐานข้อมูลเดิมอยู่ครบ 100%"
                )
                self.btn_update.setText(f"  อัปเดตเป็น v{mf.version}")
                self.lbl_progress_status.setText(
                    f"พร้อมอัปเดตเป็น v{mf.version}  •  ในเครื่อง: v{local_ver}"
                )
            self.btn_update.setEnabled(True)
            self.progress_bar.setValue(0)
        else:
            self.lbl_status_badge.setText("เป็นเวอร์ชันล่าสุดแล้ว")
            self.lbl_feat_title.setText(f"เวอร์ชัน v{local_ver} เป็นเวอร์ชันล่าสุดแล้ว")
            self.lbl_feat_desc.setText(
                "กดปุ่มสีเหลือง 'เข้าใช้งานโปรแกรม' เพื่อเริ่มใช้งานได้ทันที หรือกดซ่อมแซมไฟล์หากต้องการ"
            )
            self.btn_update.setText("  ซ่อมแซมไฟล์")
            self.btn_update.setEnabled(True)
            self.lbl_progress_status.setText(f"เวอร์ชัน v{local_ver} เป็นเวอร์ชันล่าสุดแล้ว พร้อมเข้าใช้งาน!")
            self.progress_bar.setValue(100)

        self._refresh_local_state()

    def start_or_cancel_update(self):
        if self._is_downloading:
            if self._cancel_event is not None:
                self._cancel_event.set()
                self.lbl_progress_status.setText("กำลังยกเลิกการดาวน์โหลด…")
            return
        if self.manifest is None:
            return
        if self._worker is not None and self._worker.isRunning():
            return

        mf = self.manifest
        cancel_ev = threading.Event()
        self._cancel_event = cancel_ev
        self._is_downloading = True
        self.btn_check.setEnabled(False)
        self.btn_launch.setEnabled(False)
        self.btn_update.setText("  ยกเลิกการอัปเดต")
        self.progress_bar.setValue(0)
        self.lbl_status_badge.setText(f"กำลังโหลดแพตช์ v{mf.version}…")
        self.lbl_progress_status.setText(f"เริ่มดาวน์โหลดแพตช์ v{mf.version}…")
        gh_tok = updater.get_github_token()

        def job(emit_progress):
            return updater.download_and_apply_patch(
                mf,
                self.install_dir,
                github_token=gh_tok,
                progress_cb=emit_progress,
                cancel_event=cancel_ev,
                allow_local_http=self.allow_local_http,
            )

        w = _LauncherWorker(job)
        self._worker = w

        def on_prog(idx: int, total_f: int, fname: str, done_b: int, tot_b: int, bps: float):
            if tot_b > 0:
                file_frac = min(1.0, done_b / float(tot_b))
                overall_pct = int(((idx - 1 + file_frac) / max(1, total_f)) * 100)
                self.progress_bar.setValue(max(0, min(100, overall_pct)))
                spd = f" • {_format_bytes(int(bps))}/s" if bps > 0 else ""
                self.lbl_progress_detail.setText(f"{overall_pct}%{spd}")
            else:
                self.lbl_progress_detail.setText(_format_bytes(done_b))
            self.lbl_progress_status.setText(f"กำลังดาวน์โหลด ({idx}/{total_f}): {fname}")

        def on_ok(installed_paths: list[Path]):
            if self._worker is w:
                self._worker = None
            self._is_downloading = False
            self._cancel_event = None
            self._update_available = False
            self.btn_check.setEnabled(True)
            self.btn_update.setText("  ซ่อมแซมไฟล์")
            self.btn_update.setEnabled(True)
            self.progress_bar.setValue(100)
            self.lbl_status_badge.setText(f"อัปเดตเป็น v{mf.version} สำเร็จ!")
            self.lbl_feat_title.setText(f"อัปเดตเป็น v{mf.version} สำเร็จแล้ว!")
            self.lbl_progress_status.setText(
                f"ติดตั้งแพตช์ v{mf.version} สำเร็จ ({len(installed_paths)} ไฟล์) — กด 'เข้าใช้งานโปรแกรม' ได้เลย!"
            )
            self.lbl_progress_detail.setText("SHA-256 ผ่านครบทุกไฟล์")
            self._refresh_local_state()

        def on_err(err_msg: str):
            if self._worker is w:
                self._worker = None
            self._is_downloading = False
            self._cancel_event = None
            self.btn_check.setEnabled(True)
            self.btn_update.setText(f"  อัปเดตเป็น v{mf.version}")
            self.btn_update.setEnabled(True)
            self.lbl_status_badge.setText("อัปเดตไม่สำเร็จ")
            self.lbl_progress_status.setText(err_msg)
            self.lbl_progress_detail.setText("")
            self._refresh_local_state()
            if self.isVisible() and "ยกเลิก" not in err_msg:
                QMessageBox.warning(self, "อัปเดตแพตช์ไม่สำเร็จ", err_msg)

        w.progressed.connect(on_prog)
        w.done.connect(on_ok)
        w.failed.connect(on_err)
        w.finished.connect(w.deleteLater)
        w.start()

    def _update_slide_dots(self, active_idx: int) -> None:
        for i, dot in enumerate(self._slide_dots):
            if i == active_idx:
                dot.setFixedSize(22, 6)
                dot.setStyleSheet(
                    "QPushButton { background-color: #FACC15; border: none; border-radius: 3px; }"
                )
            else:
                dot.setFixedSize(7, 6)
                dot.setStyleSheet(
                    "QPushButton { background-color: rgba(226,232,240,95); border: none; border-radius: 3px; }"
                    "QPushButton:hover { background-color: rgba(255,255,255,195); }"
                )

    def _init_bgm_and_slideshow(self) -> None:
        self._fallback_slide_timer = QTimer(self)
        self._fallback_slide_timer.setInterval(FALLBACK_SLIDE_INTERVAL_MS)
        self._fallback_slide_timer.timeout.connect(self._on_fallback_slide_tick)
        if (
            not self._canvas.has_videos
            and self._canvas.slide_count > 1
            and os.environ.get("QT_QPA_PLATFORM") != "offscreen"
        ):
            self._fallback_slide_timer.start()

        if (
            self._bgm_path is None
            or QMediaPlayer is None
            or QAudioOutput is None
            or os.environ.get("QT_QPA_PLATFORM") == "offscreen"
        ):
            return

        player = QMediaPlayer(self)
        audio = QAudioOutput(self)
        audio.setVolume(0.45)
        player.setAudioOutput(audio)
        player.setSource(QUrl.fromLocalFile(str(self._bgm_path)))
        player.setLoops(QMediaPlayer.Infinite)
        if not self._canvas.has_videos:
            player.positionChanged.connect(self._canvas.sync_to_audio_position)
        self._bgm_player = player
        self._bgm_audio = audio
        player.play()

    def _is_bgm_actively_playing(self) -> bool:
        if self._bgm_player is None or QMediaPlayer is None:
            return False
        return self._bgm_player.playbackState() == QMediaPlayer.PlayingState

    def _on_fallback_slide_tick(self) -> None:
        if not self._canvas.has_videos and not self._is_bgm_actively_playing():
            self._canvas.next_slide()

    def toggle_bgm_mute(self) -> None:
        self._bgm_muted = not self._bgm_muted
        if self._bgm_audio is not None:
            self._bgm_audio.setMuted(self._bgm_muted)
        if self._bgm_muted:
            self.btn_bgm_toggle.setIcon(ui_icons.svg_icon("volume_off", "#94A3B8", 15))
            self.btn_bgm_toggle.setToolTip("เปิดเสียงเพลงพื้นหลัง (BGM)")
        else:
            self.btn_bgm_toggle.setIcon(ui_icons.svg_icon("volume_on", "#FDE047", 15))
            self.btn_bgm_toggle.setToolTip("ปิดเสียงเพลงพื้นหลัง (BGM)")

    def _stop_media_and_timers(self) -> None:
        self._canvas.stop_animations()
        if hasattr(self, "_fallback_slide_timer") and self._fallback_slide_timer is not None:
            self._fallback_slide_timer.stop()
        if self._bgm_player is not None:
            self._bgm_player.stop()

    def launch_app(self):
        exe_path = self._target_exe_path()
        if not exe_path.is_file():
            QMessageBox.warning(
                self,
                "ไม่พบไฟล์โปรแกรมหลัก",
                f"ไม่พบไฟล์ {exe_path.name} ในโฟลเดอร์ติดตั้ง กรุณากดดาวน์โหลดแพตช์ก่อนครับ",
            )
            return
        try:
            subprocess.Popen([str(exe_path), "--from-launcher"], cwd=str(self.install_dir))
        except OSError as exc:
            QMessageBox.warning(self, "เปิดโปรแกรมไม่สำเร็จ", str(exc))
            return
        self._stop_media_and_timers()
        self.close()

    def closeEvent(self, event):
        self._stop_media_and_timers()
        if self._cancel_event is not None:
            self._cancel_event.set()
        if self._worker is not None and self._worker.isRunning():
            _retire_launcher_worker(self._worker, wait_ms=3000)
            self._worker = None
        super().closeEvent(event)


def _configure_launcher_font(app: QApplication) -> None:
    base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    fonts_dir = base_dir / "assets" / "fonts"
    if fonts_dir.is_dir():
        for font_file in sorted(fonts_dir.glob("*.ttf")):
            QFontDatabase.addApplicationFont(str(font_file))
    available = set(QFontDatabase.families())
    for fam in ("Prompt", "Sukhumvit Set", "Noto Sans Thai", "Kanit", "IBM Plex Sans Thai"):
        if fam in available:
            font = QFont(fam, 10)
            font.setFamilies([fam, "Segoe UI Emoji", "Segoe UI Symbol", "Noto Color Emoji"])
            app.setFont(font)
            return


if __name__ == "__main__":
    if "--version" in sys.argv:
        print(LAUNCHER_VERSION)
        sys.exit(0)
    if "--smoke-test" in sys.argv:
        import tempfile

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        with tempfile.TemporaryDirectory() as _td:
            app = QApplication(sys.argv)
            app.setStyle("Fusion")
            _configure_launcher_font(app)
            win = LauncherWindow(install_dir=Path(_td), allow_local_http=True)
            for _tab_i in range(3):
                win._switch_info_tab(_tab_i)
            win.close()
            app.processEvents()
        sys.exit(0)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    _configure_launcher_font(app)
    win = LauncherWindow()
    win.show()
    sys.exit(app.exec())
