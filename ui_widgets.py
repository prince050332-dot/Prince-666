"""Custom modern UI widgets: StatCard, DropImageZone, and FacebookPostPreview."""
from pathlib import Path
import tempfile
from urllib.parse import urlparse
import uuid

from PySide6.QtCore import Property, QEasingCurve, QPointF, QPropertyAnimation, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QFont,
    QFontMetrics,
    QImage,
    QImageReader,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

import ui_icons

ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r}, {g}, {b}, {alpha:.2f})"


class IosToggleSwitch(QCheckBox):
    """Smooth animated iOS-style pill toggle switch (drop-in QCheckBox replacement)."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)
        self._dark = True
        self._track_w = 44
        self._track_h = 26
        self._margin = 3
        self._offset = 1.0 if self.isChecked() else 0.0
        self._anim = QPropertyAnimation(self, b"thumb_offset", self)
        self._anim.setDuration(170)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self.stateChanged.connect(self._animate_thumb)

    def set_dark_mode(self, is_dark: bool) -> None:
        self._dark = bool(is_dark)
        self.update()

    def _get_offset(self) -> float:
        return self._offset

    def _set_offset(self, val: float) -> None:
        self._offset = float(val)
        self.update()

    thumb_offset = Property(float, _get_offset, _set_offset)

    def _animate_thumb(self, _state: int) -> None:
        self._anim.stop()
        target = 1.0 if self.isChecked() else 0.0
        if not self.isVisible():
            self._offset = target
            self.update()
            return
        self._anim.setStartValue(self._offset)
        self._anim.setEndValue(target)
        self._anim.start()

    def sizeHint(self) -> QSize:
        fm = self.fontMetrics()
        text_w = fm.horizontalAdvance(self.text()) if self.text() else 0
        gap = 10 if text_w else 0
        w = self._track_w + gap + text_w + 24
        h = max(self._track_h + 4, fm.height() + 8)
        return QSize(w, h)

    def hitButton(self, pos) -> bool:
        return self.rect().contains(pos)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        cy = self.height() / 2.0
        track_y = cy - self._track_h / 2.0
        track_rect = QRectF(0.0, track_y, float(self._track_w), float(self._track_h))
        radius = self._track_h / 2.0

        # Track color: iOS System Green when ON, subtle Apple grey when OFF
        if not self.isEnabled():
            track_color = QColor("#3A3A3C") if self._dark else QColor("#D1D1D6")
            track_color.setAlpha(120)
        elif self.isChecked():
            track_color = QColor("#30D158") if self._dark else QColor("#34C759")
        else:
            track_color = QColor("#3A3A3C") if self._dark else QColor("#D1D1D6")

        painter.setPen(Qt.NoPen)
        painter.setBrush(track_color)
        painter.drawRoundedRect(track_rect, radius, radius)

        # Sliding circular white thumb
        thumb_d = self._track_h - 2 * self._margin
        travel = self._track_w - 2 * self._margin - thumb_d
        thumb_x = self._margin + travel * self._offset
        thumb_y = track_y + self._margin

        # Subtle drop shadow under thumb
        painter.setBrush(QColor(0, 0, 0, 50))
        painter.drawEllipse(QRectF(thumb_x, thumb_y + 1.0, thumb_d, thumb_d))

        painter.setBrush(QColor("#FFFFFF"))
        painter.drawEllipse(QRectF(thumb_x, thumb_y, thumb_d, thumb_d))

        # Label text
        if self.text():
            txt_color = QColor("#F5F5F7") if self._dark else QColor("#1C1C1E")
            if not self.isEnabled():
                txt_color.setAlpha(130)
            painter.setPen(txt_color)
            text_rect = QRectF(
                self._track_w + 10.0,
                0.0,
                self.width() - self._track_w - 10.0,
                float(self.height()),
            )
            painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, self.text())

        painter.end()


def _lerp_color(c1: QColor, c2: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, float(t)))
    return QColor(
        int(c1.red() + (c2.red() - c1.red()) * t),
        int(c1.green() + (c2.green() - c1.green()) * t),
        int(c1.blue() + (c2.blue() - c1.blue()) * t),
        int(c1.alpha() + (c2.alpha() - c1.alpha()) * t),
    )


def _four_point_star_path(cx: float, cy: float, r: float) -> QPainterPath:
    """Create a crisp 4-point sparkle star centered at (cx, cy)."""
    path = QPainterPath()
    inner = r * 0.28
    path.moveTo(cx, cy - r)
    path.quadTo(cx + inner, cy - inner, cx + r, cy)
    path.quadTo(cx + inner, cy + inner, cx, cy + r)
    path.quadTo(cx - inner, cy + inner, cx - r, cy)
    path.quadTo(cx - inner, cy - inner, cx, cy - r)
    path.closeSubpath()
    return path


class DayNightThemeToggle(QPushButton):
    """Animated Day/Night sky toggle button (Sun + Clouds <-> Cratered Moon + Stars)."""

    def __init__(self, is_dark: bool = True, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setFlat(True)
        self.setFixedHeight(34)
        self.setMinimumWidth(96)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._is_dark = bool(is_dark)
        self._hovered = False
        self._progress = 1.0 if self._is_dark else 0.0
        self._anim = QPropertyAnimation(self, b"progress", self)
        self._anim.setDuration(380)
        self._anim.setEasingCurve(QEasingCurve.InOutCubic)
        self._sync_tooltip()

    def _get_progress(self) -> float:
        return self._progress

    def _set_progress(self, val: float) -> None:
        self._progress = max(0.0, min(1.0, float(val)))
        self.update()

    progress = Property(float, _get_progress, _set_progress)

    def _sync_tooltip(self) -> None:
        if self._is_dark:
            self.setToolTip("โหมดมืด (คลิกเพื่อสลับเป็นโหมดสว่าง)")
        else:
            self.setToolTip("โหมดสว่าง (คลิกเพื่อสลับเป็นโหมดมืด)")

    def set_dark_mode(self, is_dark: bool, *, animate: bool = True) -> None:
        self._is_dark = bool(is_dark)
        self._sync_tooltip()
        target = 1.0 if self._is_dark else 0.0
        self._anim.stop()
        if not animate or not self.isVisible():
            self._progress = target
            self.update()
            return
        self._anim.setStartValue(self._progress)
        self._anim.setEndValue(target)
        self._anim.start()

    def sizeHint(self) -> QSize:
        return QSize(104, 34)

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        w = float(self.width())
        h = float(self.height())
        if w <= 4 or h <= 4:
            return

        p = self._progress
        radius = h / 2.0
        track_rect = QRectF(1.0, 1.0, w - 2.0, h - 2.0)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        clip_path = QPainterPath()
        clip_path.addRoundedRect(track_rect, radius - 1.0, radius - 1.0)
        painter.save()
        painter.setClipPath(clip_path)

        # 1) Animated Sky Gradient (Day Blue <-> Cosmic Night Navy)
        sky_top = _lerp_color(QColor("#2F80ED"), QColor("#11172E"), p)
        sky_bot = _lerp_color(QColor("#69BDFD"), QColor("#1F2A4D"), p)
        sky_grad = QLinearGradient(0.0, 0.0, 0.0, h)
        sky_grad.setColorAt(0.0, sky_top)
        sky_grad.setColorAt(1.0, sky_bot)
        painter.fillRect(track_rect, sky_grad)

        # Orb center travels smoothly across the capsule
        margin = 3.5
        knob_d = h - 2.0 * margin
        knob_r = knob_d / 2.0
        start_cx = margin + knob_r
        end_cx = w - margin - knob_r
        knob_cx = start_cx + (end_cx - start_cx) * p
        knob_cy = h / 2.0

        # 2) Concentric Translucent Halo Rings behind Sun/Moon
        painter.setPen(Qt.NoPen)
        for mult, alpha in ((2.55, 16), (2.0, 22), (1.48, 30)):
            hr = knob_r * mult
            painter.setBrush(QColor(255, 255, 255, alpha))
            painter.drawEllipse(QPointF(knob_cx, knob_cy), hr, hr)

        # 3) Day Fluffy Clouds (slide down & fade out when switching to Night)
        cloud_alpha = max(0.0, 1.0 - p * 1.35)
        if cloud_alpha > 0.01:
            cy_off = p * h * 0.95
            # Back soft blue-white cloud layer
            painter.setBrush(QColor(220, 238, 255, int(190 * cloud_alpha)))
            for rx, ry, rr in (
                (0.56, 0.94, 0.34),
                (0.70, 0.84, 0.36),
                (0.84, 0.72, 0.40),
                (0.95, 0.56, 0.42),
            ):
                painter.drawEllipse(QPointF(w * rx, h * ry + cy_off), h * rr, h * rr)
            # Front crisp white cloud puffs
            painter.setBrush(QColor(255, 255, 255, int(245 * cloud_alpha)))
            for rx, ry, rr in (
                (0.47, 1.05, 0.28),
                (0.61, 0.98, 0.31),
                (0.75, 0.90, 0.34),
                (0.89, 0.78, 0.36),
                (1.00, 0.64, 0.36),
            ):
                painter.drawEllipse(QPointF(w * rx, h * ry + cy_off), h * rr, h * rr)

        # 4) Night Sparkle Stars (slide down from top & fade in when switching to Night)
        star_alpha = max(0.0, (p - 0.15) / 0.85)
        if star_alpha > 0.01:
            sy_off = -(1.0 - p) * h * 0.75
            # 4-point sparkle stars
            painter.setBrush(QColor(255, 255, 255, int(240 * star_alpha)))
            for sx, sy, sr in (
                (0.22, 0.30, 0.135),
                (0.45, 0.25, 0.105),
                (0.35, 0.68, 0.120),
                (0.56, 0.55, 0.095),
            ):
                painter.drawPath(_four_point_star_path(w * sx, h * sy + sy_off, h * sr))
            # Soft stardust dots
            painter.setBrush(QColor(215, 228, 255, int(190 * star_alpha)))
            for dx, dy, dr in (
                (0.14, 0.62, 1.1),
                (0.29, 0.48, 0.95),
                (0.50, 0.76, 1.1),
                (0.60, 0.32, 0.9),
            ):
                painter.drawEllipse(QPointF(w * dx, h * dy + sy_off), dr, dr)

        # 5) Subtle drop shadow under Celestial Orb
        painter.setBrush(QColor(0, 0, 0, 58))
        painter.drawEllipse(QPointF(knob_cx, knob_cy + 1.2), knob_r, knob_r)

        # 6) Celestial Orb (Golden Sun smoothly morphing to Silver Cratered Moon)
        orb_clip = QPainterPath()
        orb_clip.addEllipse(QPointF(knob_cx, knob_cy), knob_r, knob_r)
        painter.save()
        painter.setClipPath(orb_clip)

        # Base Sun Golden Gradient
        sun_grad = QRadialGradient(
            knob_cx - knob_r * 0.28, knob_cy - knob_r * 0.28, knob_d * 0.9
        )
        sun_grad.setColorAt(0.0, QColor("#FFF099"))
        sun_grad.setColorAt(0.45, QColor("#FACC15"))
        sun_grad.setColorAt(1.0, QColor("#D9820B"))
        painter.setBrush(sun_grad)
        painter.drawEllipse(QPointF(knob_cx, knob_cy), knob_r, knob_r)

        # Moon surface overlay slides in from right side of orb as p -> 1.0
        if p > 0.01:
            moon_dx = (1.0 - p) * knob_d * 1.1
            moon_center = QPointF(knob_cx + moon_dx, knob_cy)
            moon_grad = QRadialGradient(
                moon_center.x() - knob_r * 0.25,
                moon_center.y() - knob_r * 0.25,
                knob_d * 0.95,
            )
            moon_grad.setColorAt(0.0, QColor("#F1F5F9"))
            moon_grad.setColorAt(0.55, QColor("#CBD5E1"))
            moon_grad.setColorAt(1.0, QColor("#94A3B8"))
            painter.setBrush(moon_grad)
            painter.drawEllipse(moon_center, knob_r, knob_r)

            # 3 realistic lunar craters with subtle rim highlight
            for cx_f, cy_f, cr_f in (
                (-0.24, 0.14, 0.25),
                (0.22, -0.26, 0.17),
                (0.24, 0.30, 0.14),
            ):
                cr_pt = QPointF(
                    moon_center.x() + knob_r * cx_f,
                    moon_center.y() + knob_r * cy_f,
                )
                cr_rad = knob_r * cr_f
                painter.setBrush(QColor("#94A3B8"))
                painter.drawEllipse(cr_pt, cr_rad, cr_rad)
                painter.setBrush(QColor(100, 116, 139, 135))
                painter.drawEllipse(
                    QPointF(cr_pt.x() - 0.4, cr_pt.y() - 0.4), cr_rad * 0.82, cr_rad * 0.82
                )

        painter.restore()  # end orb clip

        # Inner recessed top shadow inside capsule
        inner_pen = QPen(QColor(0, 0, 0, 75), 1.4)
        painter.setPen(inner_pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(track_rect.adjusted(0.6, 0.6, -0.6, -0.6), radius - 1.5, radius - 1.5)

        painter.restore()  # end capsule clip

        # Subtle outer border / hover highlight ring
        border_color = (
            QColor(10, 132, 255, 210)
            if self._hovered
            else (QColor(255, 255, 255, 38) if self._is_dark else QColor(0, 0, 0, 32))
        )
        painter.setPen(QPen(border_color, 1.4 if self._hovered else 1.0))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(track_rect, radius - 1.0, radius - 1.0)
        painter.end()


class StatCard(QFrame):
    """Compact KPI metric card for the top dashboard strip."""

    def __init__(self, label: str, accent: str = "#2563EB", icon_name: str = "pages", parent=None):
        super().__init__(parent)
        self.setObjectName("statCard")
        self._accent = accent
        self._icon_name = icon_name

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(12)

        self.icon_box = QLabel()
        self.icon_box.setFixedSize(38, 38)
        self.icon_box.setAlignment(Qt.AlignCenter)
        self.icon_box.setStyleSheet(
            f"background-color: {_hex_to_rgba(accent, 0.14)}; "
            f"border-radius: 10px; "
            f"border: 1px solid {_hex_to_rgba(accent, 0.28)};"
        )
        self.icon_box.setPixmap(ui_icons.svg_pixmap(icon_name, color=accent, size=18))

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        self.lbl_value = QLabel("0")
        self.lbl_value.setObjectName("statValue")
        self.lbl_caption = QLabel(label)
        self.lbl_caption.setObjectName("statLabel")
        text_col.addWidget(self.lbl_value)
        text_col.addWidget(self.lbl_caption)

        layout.addWidget(self.icon_box)
        layout.addLayout(text_col, 1)

    def set_value(self, value: str | int):
        self.lbl_value.setText(str(value))


def _load_image_auto_orient(path: str) -> QImage:
    """Load an image while applying EXIF orientation transforms automatically."""
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    img = reader.read()
    if img.isNull():
        return QImage(str(path))
    return img


def _load_pixmap_auto_orient(path: str) -> QPixmap:
    img = _load_image_auto_orient(path)
    return QPixmap.fromImage(img) if not img.isNull() else QPixmap()


class _ThumbCard(QFrame):
    removed = Signal(str)

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self.path = path
        self.setObjectName("thumbCard")
        self.setFixedSize(76, 68)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(1)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addStretch(1)
        btn_x = QPushButton("×")
        btn_x.setObjectName("thumbRemoveBtn")
        btn_x.setFixedSize(18, 18)
        btn_x.setCursor(Qt.PointingHandCursor)
        btn_x.setToolTip(f"ลบรูป {Path(path).name}")
        btn_x.clicked.connect(lambda: self.removed.emit(self.path))
        top.addWidget(btn_x)

        lbl_img = QLabel()
        lbl_img.setAlignment(Qt.AlignCenter)
        lbl_img.setFixedHeight(42)
        pix = _load_pixmap_auto_orient(path)
        if not pix.isNull():
            lbl_img.setPixmap(
                pix.scaled(66, 40, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
        else:
            lbl_img.setText(Path(path).name[:10])
            lbl_img.setObjectName("mutedLabel")
        lbl_img.setToolTip(path)

        layout.addLayout(top)
        layout.addWidget(lbl_img, 1)


class DropImageZone(QFrame):
    """Drag-and-drop image upload area with visual thumbnail strip."""

    files_dropped = Signal(list)
    browse_clicked = Signal()
    image_removed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dropZone")
        self.setAcceptDrops(True)
        self.setFixedHeight(84)

        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(10, 4, 10, 4)
        self._root.setSpacing(2)

        # Empty state prompt
        self.empty_box = QWidget()
        eb_layout = QHBoxLayout(self.empty_box)
        eb_layout.setContentsMargins(6, 4, 6, 4)
        eb_layout.setSpacing(10)

        self.lbl_drop_icon = QLabel()
        self.lbl_drop_icon.setPixmap(ui_icons.svg_pixmap("image", color="#64748B", size=24))
        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        lbl_main = QLabel("ลากรูปภาพมาวางที่นี่ หรือกดปุ่มเพื่อเลือกไฟล์")
        lbl_main.setObjectName("dropMainLabel")
        lbl_main.setWordWrap(True)
        lbl_main.setMinimumWidth(60)
        lbl_sub = QLabel("รองรับ PNG, JPG, WEBP, GIF — แนบได้ทั้งรูปเดี่ยวและหลายรูป")
        lbl_sub.setObjectName("mutedLabel")
        lbl_sub.setWordWrap(True)
        lbl_sub.setMinimumWidth(60)
        text_col.addWidget(lbl_main)
        text_col.addWidget(lbl_sub)

        self.btn_browse = QPushButton("เลือกรูปภาพ")
        self.btn_browse.setObjectName("ghostBtn")
        self.btn_browse.setCursor(Qt.PointingHandCursor)
        self.btn_browse.clicked.connect(self.browse_clicked.emit)

        eb_layout.addWidget(self.lbl_drop_icon)
        eb_layout.addLayout(text_col, 1)
        eb_layout.addWidget(self.btn_browse)

        # Thumbnail strip (shown when images exist)
        self.scroll = QScrollArea()
        self.scroll.setMinimumSize(0, 0)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("background: transparent;")

        self.strip_container = QWidget()
        self.strip_container.setStyleSheet("background: transparent;")
        self.strip_layout = QHBoxLayout(self.strip_container)
        self.strip_layout.setContentsMargins(2, 1, 2, 1)
        self.strip_layout.setSpacing(8)
        self.strip_layout.addStretch(1)
        self.scroll.setWidget(self.strip_container)
        self.scroll.setVisible(False)

        self._root.addWidget(self.empty_box)
        self._root.addWidget(self.scroll)

    def set_images(self, paths: list[str]):
        # Clear previous thumbs
        while self.strip_layout.count() > 0:
            item = self.strip_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        if not paths:
            self.empty_box.setVisible(True)
            self.scroll.setVisible(False)
            return

        self.empty_box.setVisible(False)
        self.scroll.setVisible(True)

        for p in paths:
            card = _ThumbCard(p)
            card.removed.connect(self.image_removed.emit)
            self.strip_layout.addWidget(card)

        # Quick "+ เพิ่ม" tile at the end
        btn_more = QPushButton("+ เพิ่ม")
        btn_more.setObjectName("ghostBtn")
        btn_more.setFixedSize(64, 68)
        btn_more.setCursor(Qt.PointingHandCursor)
        btn_more.clicked.connect(self.browse_clicked.emit)
        self.strip_layout.addWidget(btn_more)
        self.strip_layout.addStretch(1)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile():
                    fp = Path(url.toLocalFile())
                    if fp.is_file() and fp.suffix.lower() in ALLOWED_IMAGE_EXTS:
                        event.acceptProposedAction()
                        self.setProperty("dragActive", True)
                        self.style().unpolish(self)
                        self.style().polish(self)
                        return
        event.ignore()

    def dragLeaveEvent(self, event):
        self.setProperty("dragActive", False)
        self.style().unpolish(self)
        self.style().polish(self)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent):
        self.setProperty("dragActive", False)
        self.style().unpolish(self)
        self.style().polish(self)
        paths = []
        for url in event.mimeData().urls():
            if url.isLocalFile():
                fp = url.toLocalFile()
                p = Path(fp)
                if p.is_file() and p.suffix.lower() in ALLOWED_IMAGE_EXTS:
                    paths.append(fp)
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()


class FacebookPostPreview(QFrame):
    """Realistic Facebook Page post live preview mockup."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("fbPreviewCard")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 12)
        outer.setSpacing(10)

        # Post Header (Avatar + Page name + timestamp)
        header = QHBoxLayout()
        header.setSpacing(10)

        self.lbl_avatar = QLabel()
        self.lbl_avatar.setFixedSize(40, 40)
        self.lbl_avatar.setPixmap(ui_icons.page_avatar_pixmap("S", 40))

        meta_col = QVBoxLayout()
        meta_col.setSpacing(1)
        self.lbl_page_name = QLabel("เพจ Facebook ของคุณ")
        self.lbl_page_name.setObjectName("fbPageName")
        self.lbl_page_name.setTextFormat(Qt.PlainText)
        self.lbl_page_name.setWordWrap(True)
        self.lbl_context_meta = QLabel("")
        self.lbl_context_meta.setObjectName("mutedLabel")
        self.lbl_context_meta.setTextFormat(Qt.PlainText)
        self.lbl_context_meta.setWordWrap(True)
        self.lbl_context_meta.setVisible(False)
        self.lbl_timestamp = QLabel("เมื่อสักครู่  •  สาธารณะ")
        self.lbl_timestamp.setObjectName("mutedLabel")
        self.lbl_timestamp.setTextFormat(Qt.PlainText)
        meta_col.addWidget(self.lbl_page_name)
        meta_col.addWidget(self.lbl_context_meta)
        meta_col.addWidget(self.lbl_timestamp)

        header.addWidget(self.lbl_avatar)
        header.addLayout(meta_col, 1)
        outer.addLayout(header)

        # Post Body Text
        self.lbl_body = QLabel("ข้อความโพสต์ของคุณจะแสดงพรีวิวที่นี่แบบเรียลไทม์…")
        self.lbl_body.setObjectName("fbBodyText")
        self.lbl_body.setTextFormat(Qt.PlainText)
        self.lbl_body.setWordWrap(True)
        self.lbl_body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        outer.addWidget(self.lbl_body)

        # Media Grid container
        self.media_frame = QFrame()
        self.media_frame.setObjectName("fbMediaFrame")
        self.media_grid = QGridLayout(self.media_frame)
        self.media_grid.setContentsMargins(0, 0, 0, 0)
        self.media_grid.setSpacing(4)
        self.media_frame.setVisible(False)
        outer.addWidget(self.media_frame)

        # Link Preview Card
        self.link_card = QFrame()
        self.link_card.setObjectName("fbLinkCard")
        lc_layout = QVBoxLayout(self.link_card)
        lc_layout.setContentsMargins(12, 10, 12, 10)
        lc_layout.setSpacing(2)
        self.lbl_link_domain = QLabel("")
        self.lbl_link_domain.setObjectName("fbLinkDomain")
        self.lbl_link_domain.setTextFormat(Qt.PlainText)
        self.lbl_link_url = QLabel("")
        self.lbl_link_url.setObjectName("fbLinkUrl")
        self.lbl_link_url.setTextFormat(Qt.PlainText)
        self.lbl_link_url.setWordWrap(True)
        lc_layout.addWidget(self.lbl_link_domain)
        lc_layout.addWidget(self.lbl_link_url)
        self.link_card.setVisible(False)
        outer.addWidget(self.link_card)

        # Video Preview Card
        self.video_card = QFrame()
        self.video_card.setObjectName("fbLinkCard")
        vc_layout = QVBoxLayout(self.video_card)
        vc_layout.setContentsMargins(14, 18, 14, 18)
        vc_layout.setSpacing(4)
        self.lbl_video_badge = QLabel("▶  วิดีโอแนบ")
        self.lbl_video_badge.setObjectName("fbLinkDomain")
        self.lbl_video_badge.setAlignment(Qt.AlignCenter)
        self.lbl_video_name = QLabel("")
        self.lbl_video_name.setObjectName("fbLinkUrl")
        self.lbl_video_name.setTextFormat(Qt.PlainText)
        self.lbl_video_name.setAlignment(Qt.AlignCenter)
        self.lbl_video_name.setWordWrap(True)
        vc_layout.addWidget(self.lbl_video_badge)
        vc_layout.addWidget(self.lbl_video_name)
        self.video_card.setVisible(False)
        outer.addWidget(self.video_card)

        outer.addStretch(1)

        # Divider + Authentic Facebook Footer Actions
        div = QFrame()
        div.setObjectName("hairline")
        div.setFrameShape(QFrame.HLine)
        outer.addWidget(div)

        footer = QHBoxLayout()
        footer.setContentsMargins(4, 2, 4, 0)
        for text in ("👍  ถูกใจ", "💬  แสดงความคิดเห็น", "📤  แชร์"):
            lbl = QLabel(text)
            lbl.setObjectName("fbFooterAction")
            lbl.setAlignment(Qt.AlignCenter)
            footer.addWidget(lbl, 1)
        outer.addLayout(footer)

    def update_preview(
        self,
        *,
        page_names: list[str],
        text: str,
        link: str,
        image_paths: list[str],
        video_path: str = "",
        is_reel: bool = False,
        schedule_label: str = "",
        place_label: str = "",
        activity_label: str = "",
    ):
        if page_names:
            first = page_names[0]
            if len(page_names) == 1:
                self.lbl_page_name.setText(first)
            else:
                self.lbl_page_name.setText(f"{first}  (+อีก {len(page_names) - 1} เพจ)")
            self.lbl_avatar.setPixmap(ui_icons.page_avatar_pixmap(first, 40))
        else:
            self.lbl_page_name.setText("ยังไม่ได้เลือกเพจ")
            self.lbl_avatar.setPixmap(ui_icons.page_avatar_pixmap("?", 40))

        ctx_parts = []
        if is_reel and (video_path or "").strip():
            ctx_parts.append("🎬 Facebook Reels")
        if (activity_label or "").strip():
            ctx_parts.append(f"🎯 {(activity_label or '').strip()}")
        if (place_label or "").strip():
            ctx_parts.append(f"📍 ที่ {(place_label or '').strip()}")
        if ctx_parts:
            self.lbl_context_meta.setText("  •  ".join(ctx_parts))
            self.lbl_context_meta.setVisible(True)
        else:
            self.lbl_context_meta.setText("")
            self.lbl_context_meta.setVisible(False)

        if schedule_label:
            self.lbl_timestamp.setText(f"ตั้งเวลา {schedule_label}  •  สาธารณะ")
        else:
            self.lbl_timestamp.setText("เมื่อสักครู่  •  สาธารณะ")

        clean_text = (text or "").strip()
        if clean_text:
            snippet = clean_text if len(clean_text) <= 450 else clean_text[:450] + "…"
            self.lbl_body.setText(snippet)
            self.lbl_body.setProperty("placeholder", False)
        else:
            self.lbl_body.setText("ข้อความโพสต์ของคุณจะแสดงพรีวิวที่นี่แบบเรียลไทม์…")
            self.lbl_body.setProperty("placeholder", True)
        self.lbl_body.style().unpolish(self.lbl_body)
        self.lbl_body.style().polish(self.lbl_body)

        # Link preview card
        clean_link = (link or "").strip()
        if clean_link:
            try:
                parsed = urlparse(clean_link)
                domain = (parsed.netloc or clean_link).upper()
            except ValueError:
                domain = clean_link.upper()
            self.lbl_link_domain.setText(domain)
            note = " (จะต่อท้ายข้อความเมื่อมีสื่อ)" if (image_paths or video_path) else ""
            self.lbl_link_url.setText(f"{clean_link}{note}")
            self.link_card.setVisible(True)
        else:
            self.link_card.setVisible(False)

        # Video preview card
        clean_video = (video_path or "").strip()
        if clean_video:
            vp = Path(clean_video)
            size_str = ""
            if vp.is_file():
                size_mb = vp.stat().st_size / (1024 * 1024)
                size_str = f" ({size_mb:.1f} MB)"
            self.lbl_video_badge.setText(
                "🎬  Facebook Reels (วิดีโอสั้น)" if is_reel else "▶  วิดีโอแนบ"
            )
            self.lbl_video_name.setText(f"{vp.name}{size_str}")
            self.video_card.setVisible(True)
        else:
            self.video_card.setVisible(False)

        # Media grid
        while self.media_grid.count():
            it = self.media_grid.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()

        valid_imgs = [p for p in image_paths if p]
        if not valid_imgs:
            self.media_frame.setVisible(False)
            return

        self.media_frame.setVisible(True)
        shown = valid_imgs[:4]
        extra = len(valid_imgs) - 4

        def make_tile(path: str, w: int, h: int, overlay: str = "") -> QLabel:
            lbl = QLabel()
            lbl.setObjectName("fbPhotoTile")
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setFixedHeight(h)
            pix = _load_pixmap_auto_orient(path)
            if not pix.isNull():
                scaled = pix.scaled(w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                lbl.setPixmap(scaled)
            else:
                lbl.setText(Path(path).name)
            if overlay:
                lbl.setToolTip(f"{Path(path).name} ({overlay})")
            return lbl

        if len(shown) == 1:
            self.media_grid.addWidget(make_tile(shown[0], 380, 210), 0, 0)
        elif len(shown) == 2:
            self.media_grid.addWidget(make_tile(shown[0], 188, 170), 0, 0)
            self.media_grid.addWidget(make_tile(shown[1], 188, 170), 0, 1)
        elif len(shown) == 3:
            self.media_grid.addWidget(make_tile(shown[0], 380, 130), 0, 0, 1, 2)
            self.media_grid.addWidget(make_tile(shown[1], 188, 110), 1, 0)
            self.media_grid.addWidget(make_tile(shown[2], 188, 110), 1, 1)
        else:
            self.media_grid.addWidget(make_tile(shown[0], 188, 110), 0, 0)
            self.media_grid.addWidget(make_tile(shown[1], 188, 110), 0, 1)
            self.media_grid.addWidget(make_tile(shown[2], 188, 110), 1, 0)
            last_tile = make_tile(shown[3], 188, 110, f"+{extra} รูป" if extra > 0 else "")
            self.media_grid.addWidget(last_tile, 1, 1)
            if extra > 0:
                badge = QLabel(f"+{extra} รูป")
                badge.setObjectName("fbMoreBadge")
                badge.setAlignment(Qt.AlignCenter)
                self.media_grid.addWidget(badge, 2, 0, 1, 2)


def apply_text_watermark(image_path: str, watermark_text: str) -> tuple[str, bool]:
    """Render `watermark_text` at the bottom-right corner of `image_path`.

    Returns `(output_path, is_temp_file)`. If `watermark_text` is empty or the
    file cannot be decoded as a raster image, returns `(image_path, False)`.
    """
    text = (watermark_text or "").strip()
    if not text or not image_path:
        return image_path, False
    src = Path(image_path)
    if not src.is_file():
        return image_path, False

    img = _load_image_auto_orient(str(src))
    if img.isNull() or img.width() < 16 or img.height() < 16:
        return image_path, False

    canvas = img.convertToFormat(QImage.Format_ARGB32)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.TextAntialiasing)

    px_size = max(12, min(48, int(min(canvas.width(), canvas.height()) * 0.042)))
    font = QFont("Prompt")
    font.setPixelSize(px_size)
    font.setBold(True)
    painter.setFont(font)

    fm = QFontMetrics(font)
    pad_x = max(8, int(px_size * 0.6))
    pad_y = max(4, int(px_size * 0.35))
    margin = max(8, int(px_size * 0.6))

    text_w = min(fm.horizontalAdvance(text), max(32, canvas.width() - 2 * margin - 2 * pad_x))
    box_w = text_w + 2 * pad_x
    box_h = fm.height() + 2 * pad_y
    box_x = max(4, canvas.width() - box_w - margin)
    box_y = max(4, canvas.height() - box_h - margin)

    pill_rect = QRectF(float(box_x), float(box_y), float(box_w), float(box_h))
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(0, 0, 0, 145))
    painter.drawRoundedRect(pill_rect, box_h / 2.0, box_h / 2.0)

    painter.setPen(QColor("#FFFFFF"))
    elided = fm.elidedText(text, Qt.ElideRight, int(text_w))
    painter.drawText(pill_rect, Qt.AlignCenter, elided)
    painter.end()

    ext = src.suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        ext = ".png"
    out_path = Path(tempfile.gettempdir()) / f"sm_wm_{uuid.uuid4().hex}{ext}"
    if canvas.save(str(out_path)):
        return str(out_path), True
    return image_path, False
