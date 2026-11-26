"""Social Manager — Modern Studio-Grade Facebook Page Manager (PySide6)."""
import csv
from datetime import datetime, timedelta
import io
import os
from pathlib import Path
import random
import re
import sqlite3
import sys
import threading
from urllib.parse import urlparse
import uuid
import warnings
import webbrowser

from PySide6.QtCore import QDateTime, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QFontDatabase
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import config
import db
import facebook
import ui_icons
import ui_theme
from ui_widgets import (
    DayNightThemeToggle,
    DropImageZone,
    FacebookPostPreview,
    IosToggleSwitch,
    StatCard,
    apply_text_watermark,
)
from version import APP_NAME, APP_VERSION

FB_MAX_CHARS = 63206
IMAGE_FILTER = "รูปภาพ (*.png *.jpg *.jpeg *.webp *.gif);;ทุกไฟล์ (*.*)"
VIDEO_FILTER = "วิดีโอ (*.mp4 *.mov *.m4v *.avi *.mkv *.webm);;ทุกไฟล์ (*.*)"


class Worker(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn())
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


_ORPHAN_WORKERS: set[QThread] = set()


def _retire_worker(worker: Worker | None, wait_ms: int = 2000) -> None:
    """Disconnect signals and wait briefly; keep reference alive if still running to avoid QThread fatal crash."""
    if worker is None or not worker.isRunning():
        return
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        for sig in (worker.done, worker.failed, worker.finished):
            try:
                sig.disconnect()
            except RuntimeError:
                pass
    if not worker.wait(wait_ms):
        _ORPHAN_WORKERS.add(worker)
        worker.finished.connect(
            lambda w=worker: (_ORPHAN_WORKERS.discard(w), w.deleteLater())
        )


class SettingsDialog(QDialog):
    """3-Tab Guided Setup Wizard: Self-App Wizard, One-Click Backend, and Direct Token."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ตั้งค่าการเชื่อมต่อ Facebook")
        self.setMinimumWidth(650)
        self._worker: Worker | None = None
        self.pages_added_via_token: list[tuple[str, str]] = []
        self.keyring_saved: bool = True
        self.db_restored: bool = False

        self.tab_widget = QTabWidget()
        self.tab_widget.addTab(self._build_wizard_tab(), "1. ตั้งค่าเอง (Wizard)")
        self.tab_widget.addTab(self._build_backend_tab(), "2. One-Click Backend")
        self.tab_widget.addTab(self._build_token_tab(), "3. วาง Token โดยตรง")

        if config.get_auth_mode() == "backend_oauth":
            self.tab_widget.setCurrentIndex(1)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("บันทึกการตั้งค่า")
        buttons.button(QDialogButtonBox.Save).setObjectName("primaryBtn")
        buttons.button(QDialogButtonBox.Cancel).setText("ปิด")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        self.btn_backup_db = QPushButton("สำรองฐานข้อมูล (Backup DB)")
        self.btn_backup_db.setObjectName("ghostBtn")
        self.btn_backup_db.setCursor(Qt.PointingHandCursor)
        self.btn_backup_db.clicked.connect(self._backup_database)

        self.btn_restore_db = QPushButton("กู้คืนฐานข้อมูล (Restore)…")
        self.btn_restore_db.setObjectName("ghostBtn")
        self.btn_restore_db.setCursor(Qt.PointingHandCursor)
        self.btn_restore_db.setToolTip(
            "กู้คืนข้อมูลเพจ คิว ประวัติ เทมเพลต และกฎอัตโนมัติจากไฟล์สำรอง (.db) รายวันหรือไฟล์ที่สำรองไว้เอง"
        )
        self.btn_restore_db.clicked.connect(self._restore_database)

        footer_row = QHBoxLayout()
        footer_row.addWidget(self.btn_backup_db)
        footer_row.addWidget(self.btn_restore_db)
        footer_row.addStretch(1)
        footer_row.addWidget(buttons)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)
        layout.addWidget(self.tab_widget, 1)
        layout.addLayout(footer_row)

    # --- Tab 1: 4-Step Self-App Wizard ---

    def _build_wizard_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        intro = QLabel(
            "ทำตาม 4 ขั้นตอนด้านล่าง กดปุ่มเปิดหน้าเว็บ Meta ได้ตรงจากที่นี่เลย (ฟรี ไม่ต้องมีเซิร์ฟเวอร์)"
        )
        intro.setObjectName("mutedLabel")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # Step 1: App ID
        s1_row = QHBoxLayout()
        lbl_s1 = QLabel("ขั้นที่ 1: ใส่ FB App ID")
        lbl_s1.setObjectName("sectionTitle")
        btn_open_apps = QPushButton("เปิดหน้า Meta Apps ↗")
        btn_open_apps.setObjectName("ghostBtn")
        btn_open_apps.setCursor(Qt.PointingHandCursor)
        btn_open_apps.clicked.connect(
            lambda: webbrowser.open("https://developers.facebook.com/apps/")
        )
        s1_row.addWidget(lbl_s1)
        s1_row.addStretch(1)
        s1_row.addWidget(btn_open_apps)
        layout.addLayout(s1_row)

        self.input_app_id = QLineEdit(config.get_fb_app_id())
        self.input_app_id.setPlaceholderText("วาง App ID ที่นี่ (ตัวเลขจาก Meta Developer)")
        layout.addWidget(self.input_app_id)

        # Step 2: App Secret with direct link to Basic Settings
        s2_row = QHBoxLayout()
        lbl_s2 = QLabel("ขั้นที่ 2: ใส่ FB App Secret (กด Show แล้วคัดลอกมาวาง)")
        lbl_s2.setObjectName("sectionTitle")
        btn_open_basic = QPushButton("เปิดหน้าดู App Secret ตรงนี้เลย ↗")
        btn_open_basic.setObjectName("ghostBtn")
        btn_open_basic.setCursor(Qt.PointingHandCursor)
        btn_open_basic.clicked.connect(self._open_meta_basic_page)
        s2_row.addWidget(lbl_s2)
        s2_row.addStretch(1)
        s2_row.addWidget(btn_open_basic)
        layout.addLayout(s2_row)

        self.input_app_secret = QLineEdit(config.get_fb_app_secret())
        self.input_app_secret.setEchoMode(QLineEdit.Password)
        self.input_app_secret.setPlaceholderText("วาง App Secret 32 ตัวอักษรจากหน้า Basic")
        layout.addWidget(self.input_app_secret)

        self.chk_show_secret = IosToggleSwitch("แสดง App Secret (เก็บลง Windows Keychain อัตโนมัติ)")
        self.chk_show_secret.set_dark_mode(config.get_theme() == "dark")
        self.chk_show_secret.toggled.connect(self._toggle_secret)
        layout.addWidget(self.chk_show_secret)

        # Step 3: Redirect URI with direct link to FB Login Settings
        s3_row = QHBoxLayout()
        lbl_s3 = QLabel("ขั้นที่ 3: นำ Redirect URI ไปวางใน Facebook Login")
        lbl_s3.setObjectName("sectionTitle")
        btn_open_login = QPushButton("เปิดหน้าวาง Redirect URI ↗")
        btn_open_login.setObjectName("ghostBtn")
        btn_open_login.setCursor(Qt.PointingHandCursor)
        btn_open_login.clicked.connect(self._open_meta_login_settings)
        s3_row.addWidget(lbl_s3)
        s3_row.addStretch(1)
        s3_row.addWidget(btn_open_login)
        layout.addLayout(s3_row)

        self.input_redirect = QLineEdit(facebook.REDIRECT_URI)
        self.input_redirect.setReadOnly(True)
        btn_copy = QPushButton("คัดลอก")
        btn_copy.setObjectName("ghostBtn")
        btn_copy.setCursor(Qt.PointingHandCursor)
        btn_copy.clicked.connect(
            lambda: QApplication.clipboard().setText(facebook.REDIRECT_URI)
        )
        redirect_row = QHBoxLayout()
        redirect_row.addWidget(self.input_redirect, 1)
        redirect_row.addWidget(btn_copy)
        layout.addLayout(redirect_row)

        # Step 4: Verify & Graph API version
        s4_row = QHBoxLayout()
        lbl_ver = QLabel("Graph API:")
        lbl_ver.setObjectName("mutedLabel")
        self.input_graph_ver = QLineEdit(config.get_graph_version())
        self.input_graph_ver.setPlaceholderText(config.DEFAULT_GRAPH_VERSION)
        self.input_graph_ver.setFixedWidth(90)

        self.btn_test_creds = QPushButton("ขั้นที่ 4: ทดสอบ App ID / Secret")
        self.btn_test_creds.setObjectName("ghostBtn")
        self.btn_test_creds.setCursor(Qt.PointingHandCursor)
        self.btn_test_creds.clicked.connect(self._test_credentials)

        self.lbl_cred_status = QLabel("")
        self.lbl_cred_status.setObjectName("mutedLabel")
        self.lbl_cred_status.setTextFormat(Qt.PlainText)
        self.lbl_cred_status.setWordWrap(True)

        s4_row.addWidget(lbl_ver)
        s4_row.addWidget(self.input_graph_ver)
        s4_row.addStretch(1)
        s4_row.addWidget(self.btn_test_creds)
        layout.addLayout(s4_row)
        layout.addWidget(self.lbl_cred_status)

        self.chk_minimize_to_tray = IosToggleSwitch(
            "ย่อเก็บลง System Tray เมื่อกดปิดหน้าต่าง (เพื่อให้คิวตั้งเวลาทำงานต่อเบื้องหลัง)"
        )
        self.chk_minimize_to_tray.set_dark_mode(config.get_theme() == "dark")
        self.chk_minimize_to_tray.setChecked(config.get_minimize_to_tray())
        layout.addWidget(self.chk_minimize_to_tray)

        layout.addStretch(1)
        return tab

    # --- Tab 2: One-Click OAuth Backend ---

    def _build_backend_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)

        desc = QLabel(
            "โหมดสำหรับแจกโปรแกรมให้ผู้ใช้ทั่วไป (แบบ Buffer / Hootsuite):\n"
            "เจ้าของแอปวาง Backend Server ไว้ที่เดียว (โฟลเดอร์ oauth_backend/ ในโปรเจกต์)\n"
            "ผู้ใช้ปลายทางแค่ใส่ URL ด้านล่าง แล้วกด 'เชื่อมต่อ Facebook' ได้เลยโดยไม่ต้องกรอก App ID / Secret"
        )
        desc.setObjectName("mutedLabel")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        self.chk_use_backend = IosToggleSwitch("เปิดใช้โหมด One-Click ผ่าน Backend Server")
        self.chk_use_backend.set_dark_mode(config.get_theme() == "dark")
        self.chk_use_backend.setChecked(config.get_auth_mode() == "backend_oauth")
        layout.addWidget(self.chk_use_backend)

        form = QFormLayout()
        self.input_backend_url = QLineEdit(config.get_backend_url())
        self.input_backend_url.setPlaceholderText("เช่น https://social-manager-auth.vercel.app")
        form.addRow("Backend URL:", self.input_backend_url)
        layout.addLayout(form)

        test_row = QHBoxLayout()
        self.btn_test_backend = QPushButton("ทดสอบการเชื่อมต่อ Server")
        self.btn_test_backend.setObjectName("ghostBtn")
        self.btn_test_backend.setCursor(Qt.PointingHandCursor)
        self.btn_test_backend.clicked.connect(self._test_backend)
        self.lbl_backend_status = QLabel("")
        self.lbl_backend_status.setObjectName("mutedLabel")
        self.lbl_backend_status.setTextFormat(Qt.PlainText)
        self.lbl_backend_status.setWordWrap(True)
        test_row.addWidget(self.btn_test_backend)
        test_row.addWidget(self.lbl_backend_status, 1)
        layout.addLayout(test_row)
        layout.addStretch(1)
        return tab

    # --- Tab 3: Direct Page Token (No App Secret) ---

    def _build_token_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        top = QHBoxLayout()
        desc = QLabel(
            "ไม่ต้องใส่ App ID หรือ App Secret — แค่วาง Page Access Token หรือ User Access Token\n"
            "จาก Graph API Explorer แล้วกดเพิ่มเพจได้ทันที\n"
            "(หากต้องการดูสถิติ Reach / Impressions ให้ติ๊กสิทธิ์ read_insights เพิ่มใน Token ด้วย)"
        )
        desc.setObjectName("mutedLabel")
        desc.setWordWrap(True)
        btn_explorer = QPushButton("เปิด Graph API Explorer ↗")
        btn_explorer.setObjectName("ghostBtn")
        btn_explorer.setCursor(Qt.PointingHandCursor)
        btn_explorer.clicked.connect(
            lambda: webbrowser.open("https://developers.facebook.com/tools/explorer/")
        )
        top.addWidget(desc, 1)
        top.addWidget(btn_explorer)
        layout.addLayout(top)

        self.input_direct_token = QPlainTextEdit()
        self.input_direct_token.setPlaceholderText("วาง Access Token ที่นี่ (EAA...)…")
        self.input_direct_token.setMaximumHeight(110)
        layout.addWidget(self.input_direct_token)

        btn_row = QHBoxLayout()
        self.btn_add_by_token = QPushButton("ดึงเพจและเชื่อมต่อด้วย Token นี้ทันที")
        self.btn_add_by_token.setObjectName("primaryBtn")
        self.btn_add_by_token.setCursor(Qt.PointingHandCursor)
        self.btn_add_by_token.clicked.connect(self._connect_direct_token)
        btn_row.addWidget(self.btn_add_by_token)
        layout.addLayout(btn_row)

        self.lbl_token_status = QLabel("")
        self.lbl_token_status.setObjectName("mutedLabel")
        self.lbl_token_status.setTextFormat(Qt.PlainText)
        self.lbl_token_status.setWordWrap(True)
        layout.addWidget(self.lbl_token_status)
        layout.addStretch(1)
        return tab

    # --- Helpers ---

    def _open_meta_basic_page(self):
        app_id = self.input_app_id.text().strip()
        if app_id and app_id.isascii() and app_id.isdigit():
            webbrowser.open(f"https://developers.facebook.com/apps/{app_id}/settings/basic/")
        else:
            webbrowser.open("https://developers.facebook.com/apps/")

    def _open_meta_login_settings(self):
        app_id = self.input_app_id.text().strip()
        if app_id and app_id.isascii() and app_id.isdigit():
            webbrowser.open(f"https://developers.facebook.com/apps/{app_id}/fb-login/settings/")
        else:
            webbrowser.open("https://developers.facebook.com/apps/")

    def _toggle_secret(self, show: bool):
        self.input_app_secret.setEchoMode(
            QLineEdit.Normal if show else QLineEdit.Password
        )

    def _set_async_buttons_enabled(self, enabled: bool):
        for btn in (self.btn_test_creds, self.btn_test_backend, self.btn_add_by_token):
            btn.setEnabled(enabled)

    def _run_dialog_task(self, fn, on_ok, on_err, _busy_btn: QPushButton | None = None):
        if self._worker and self._worker.isRunning():
            return
        self._set_async_buttons_enabled(False)
        self._worker = Worker(fn)
        self._worker.done.connect(on_ok)
        self._worker.failed.connect(on_err)
        self._worker.finished.connect(lambda: self._set_async_buttons_enabled(True))
        self._worker.start()

    def _test_credentials(self):
        if self._worker and self._worker.isRunning():
            return
        app_id = self.input_app_id.text().strip()
        app_secret = self.input_app_secret.text().strip()
        self.lbl_cred_status.setText("กำลังตรวจสอบกับ Facebook…")
        self._run_dialog_task(
            lambda: facebook.verify_app_credentials(app_id, app_secret),
            lambda _ok: self.lbl_cred_status.setText("✅ App ID และ App Secret ถูกต้อง พร้อมใช้งาน!"),
            lambda err: self.lbl_cred_status.setText(f"❌ ไม่ผ่าน: {err}"),
            self.btn_test_creds,
        )

    def _test_backend(self):
        if self._worker and self._worker.isRunning():
            return
        url = self.input_backend_url.text().strip()
        self.lbl_backend_status.setText("กำลังตรวจสอบ Server…")

        def ok(data):
            if data.get("configured"):
                self.lbl_backend_status.setText("✅ Server พร้อมใช้งาน (ตั้งค่า Meta App บน Server แล้ว)")
            else:
                self.lbl_backend_status.setText("⚠️ เชื่อมต่อได้ แต่ Server ยังไม่ได้ตั้งค่า FB_APP_ID/SECRET")

        self._run_dialog_task(
            lambda: facebook.check_backend_health(url),
            ok,
            lambda err: self.lbl_backend_status.setText(f"❌ {err}"),
            self.btn_test_backend,
        )

    def _connect_direct_token(self):
        if self._worker and self._worker.isRunning():
            return
        raw = self.input_direct_token.toPlainText().strip()
        self.lbl_token_status.setText("กำลังตรวจสอบ Token และดึงข้อมูลเพจ…")

        def ok(pages):
            for pid, name in pages:
                db.save_account(facebook.PLATFORM, pid, name)
            self.pages_added_via_token.extend(pages)
            self.input_direct_token.clear()
            names = ", ".join(n for _, n in pages)
            self.lbl_token_status.setText(f"✅ เพิ่มเพจสำเร็จ {len(pages)} เพจ: {names}")

        self._run_dialog_task(
            lambda: facebook.connect_with_token(raw, exchange_long_lived=True),
            ok,
            lambda err: self.lbl_token_status.setText(f"❌ {err}"),
            self.btn_add_by_token,
        )

    def _backup_database(self):
        if not db.DB_PATH.exists():
            QMessageBox.information(self, "ไม่พบฐานข้อมูล", "ยังไม่มีไฟล์ฐานข้อมูลสำหรับสำรอง")
            return
        default_name = f"social_manager_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        dest, _ = QFileDialog.getSaveFileName(
            self,
            "เลือกตำแหน่งบันทึกไฟล์สำรองฐานข้อมูล",
            str(Path.home() / default_name),
            "SQLite Database (*.db);;ทุกไฟล์ (*.*)",
        )
        if not dest:
            return
        try:
            db.backup_database(dest)
            QMessageBox.information(
                self, "สำรองข้อมูลสำเร็จ", f"สำรองฐานข้อมูลเรียบร้อยแล้วที่:\n{dest}"
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "สำรองข้อมูลไม่สำเร็จ", str(exc))

    def _restore_database(self):
        parent = self.parent()
        if parent is not None:
            for w_attr in (
                "worker",
                "analytics_worker",
                "token_check_worker",
                "auto_reply_worker",
            ):
                w_obj = getattr(parent, w_attr, None)
                if w_obj is not None and w_obj.isRunning():
                    QMessageBox.warning(
                        self,
                        "มีงานเบื้องหลังกำลังทำงาน",
                        "กรุณารอให้งานที่กำลังรันอยู่เสร็จสิ้นก่อนกู้คืนฐานข้อมูล",
                    )
                    return

        bdir = db.get_backup_dir()
        start_dir = str(bdir) if bdir.is_dir() else str(Path.home())
        src_path, _ = QFileDialog.getOpenFileName(
            self,
            "เลือกไฟล์สำรองฐานข้อมูลที่ต้องการกู้คืน (auto_backup_*.db หรือ .db)",
            start_dir,
            "SQLite Database (*.db);;ทุกไฟล์ (*.*)",
        )
        if not src_path:
            return
        try:
            validated_src = db.validate_sqlite_backup_file(src_path)
        except ValueError as exc:
            QMessageBox.warning(self, "ไฟล์สำรองไม่ถูกต้อง", str(exc))
            return

        confirm = QMessageBox.question(
            self,
            "ยืนยันการกู้คืนฐานข้อมูล",
            f"ต้องการกู้คืนฐานข้อมูลจากไฟล์:\n{validated_src}\n\n"
            "ระบบจะสำรองข้อมูลปัจจุบันเก็บไว้ที่โฟลเดอร์ backups (pre_restore_*.db) ให้อัตโนมัติก่อนทับข้อมูล",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            pre_backup = db.restore_database(validated_src)
        except (ValueError, OSError, sqlite3.Error) as exc:
            QMessageBox.warning(self, "กู้คืนฐานข้อมูลไม่สำเร็จ", str(exc))
            return

        self.db_restored = True
        safety_note = f"\n\nสำรองข้อมูลก่อนกู้คืนไว้ที่:\n{pre_backup}" if pre_backup else ""
        QMessageBox.information(
            self,
            "กู้คืนฐานข้อมูลสำเร็จ",
            f"กู้คืนฐานข้อมูลจาก:\n{validated_src}\nเรียบร้อยแล้ว{safety_note}",
        )
        self.accept()

    def _save(self):
        use_backend = self.chk_use_backend.isChecked()
        backend_url = self.input_backend_url.text().strip()
        if use_backend:
            try:
                backend_url = facebook._validate_backend_url(backend_url)
            except RuntimeError as exc:
                QMessageBox.warning(self, "Backend URL ไม่ถูกต้อง", str(exc))
                self.tab_widget.setCurrentIndex(1)
                return
        self.keyring_saved = config.save_fb_settings(
            self.input_app_id.text(),
            self.input_app_secret.text(),
            self.input_graph_ver.text(),
            set_self_mode=not use_backend,
        )
        config.save_backend_settings(
            backend_url,
            use_backend=use_backend,
        )
        config.save_minimize_to_tray(self.chk_minimize_to_tray.isChecked())
        self.accept()

    def _cleanup_worker(self):
        _retire_worker(self._worker, wait_ms=2000)
        self._worker = None

    def done(self, result: int):
        self._cleanup_worker()
        super().done(result)

    def closeEvent(self, event):
        self._cleanup_worker()
        super().closeEvent(event)


class PostPresetsDialog(QDialog):
    """Manage one-click Post Presets / Content Packs (caption + media + Reels + watermark + check-in + follow-up comments)."""

    def __init__(self, composer_snapshot: dict | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("คลังชุดโพสต์สำเร็จรูป (Post Presets / Content Packs)")
        self.setMinimumSize(760, 520)
        self._composer_snapshot = composer_snapshot
        self.selected_preset: dict | None = None
        self._presets: list[dict] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        intro = QLabel(
            "บันทึกชุดโพสต์ที่ใช้บ่อย (แคปชัน + ลิงก์ + รูป/วิดีโอ/Reels + ลายน้ำ + เช็คอิน + คอมเมนต์ต่อท้าย) "
            "เพื่อโหลดกลับมาพร้อมโพสต์ได้ในคลิกเดียว"
        )
        intro.setObjectName("mutedLabel")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        save_card = QFrame()
        save_card.setObjectName("scheduleCard")
        save_row = QHBoxLayout(save_card)
        save_row.setContentsMargins(12, 8, 12, 8)
        save_row.setSpacing(8)
        self.input_preset_name = QLineEdit()
        self.input_preset_name.setPlaceholderText(
            "ตั้งชื่อชุดโพสต์ เช่น โปรเปิดร้านเช้า / ชุดขายดี #1 / คลิป Reels รายวัน…"
        )
        self.btn_save_current = QPushButton("💾 บันทึกจากหน้าเขียนโพสต์ปัจจุบัน")
        self.btn_save_current.setObjectName("primaryBtn")
        self.btn_save_current.setCursor(Qt.PointingHandCursor)
        self.btn_save_current.setEnabled(composer_snapshot is not None)
        self.btn_save_current.clicked.connect(self._save_current_from_composer)
        save_row.addWidget(self.input_preset_name, 1)
        save_row.addWidget(self.btn_save_current)
        layout.addWidget(save_card)

        self.tbl_presets = QTableWidget(0, 4)
        self.tbl_presets.setHorizontalHeaderLabels(
            ["ชื่อชุดโพสต์", "ตัวอย่างข้อความ", "สื่อ / ออปชัน", "อัปเดตล่าสุด"]
        )
        self.tbl_presets.verticalHeader().setVisible(False)
        self.tbl_presets.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_presets.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tbl_presets.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hdr = self.tbl_presets.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.tbl_presets.itemSelectionChanged.connect(self._on_selection_changed)
        self.tbl_presets.cellDoubleClicked.connect(lambda _r, _c: self._load_selected())
        layout.addWidget(self.tbl_presets, 1)

        lbl_prev = QLabel("รายละเอียดชุดโพสต์ที่เลือก:")
        lbl_prev.setObjectName("sectionTitle")
        layout.addWidget(lbl_prev)

        self.preview_box = QPlainTextEdit()
        self.preview_box.setReadOnly(True)
        self.preview_box.setMaximumHeight(110)
        self.preview_box.setPlaceholderText("คลิกเลือกชุดโพสต์ในตารางเพื่อดูรายละเอียด…")
        layout.addWidget(self.preview_box)

        btn_row = QHBoxLayout()
        self.btn_load_preset = QPushButton("📥 โหลดเข้าหน้าเขียนโพสต์")
        self.btn_load_preset.setObjectName("primaryBtn")
        self.btn_load_preset.setCursor(Qt.PointingHandCursor)
        self.btn_load_preset.clicked.connect(self._load_selected)

        self.btn_delete_preset = QPushButton("🗑 ลบชุดที่เลือก")
        self.btn_delete_preset.setObjectName("dangerBtn")
        self.btn_delete_preset.setCursor(Qt.PointingHandCursor)
        self.btn_delete_preset.clicked.connect(self._delete_selected)

        btn_close = QPushButton("ปิด")
        btn_close.setObjectName("ghostBtn")
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.clicked.connect(self.reject)

        btn_row.addWidget(self.btn_delete_preset)
        btn_row.addStretch(1)
        btn_row.addWidget(btn_close)
        btn_row.addWidget(self.btn_load_preset)
        layout.addLayout(btn_row)

        self._refresh_table()

    def _current_preset(self) -> dict | None:
        row = self.tbl_presets.currentRow()
        if row < 0 or row >= len(self._presets):
            return None
        return self._presets[row]

    def _refresh_table(self):
        self._presets = db.list_post_presets()
        self.tbl_presets.setRowCount(len(self._presets))
        for r, p in enumerate(self._presets):
            name_item = QTableWidgetItem(p["name"])
            name_item.setData(Qt.UserRole, p["id"])
            snippet = (p.get("message") or "").replace("\n", " ").strip()[:70] or "(ไม่มีข้อความ)"
            badges: list[str] = []
            if p.get("video_path"):
                badges.append("🎬 Reels" if p.get("is_reel") else "🎬 วิดีโอ")
            elif p.get("image_paths"):
                badges.append(f"🖼 {len(p['image_paths'])} รูป")
            if p.get("link"):
                badges.append("🔗 ลิงก์")
            if p.get("watermark_enabled") and p.get("watermark_text"):
                badges.append("💧 ลายน้ำ")
            if p.get("place_id"):
                badges.append("📍 เช็คอิน")
            if p.get("comments"):
                badges.append(f"💬 {len(p['comments'])} เม้นต์")
            try:
                upd_str = datetime.fromisoformat(p["updated_at"]).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                upd_str = str(p.get("updated_at") or "")
            self.tbl_presets.setItem(r, 0, name_item)
            self.tbl_presets.setItem(r, 1, QTableWidgetItem(snippet))
            self.tbl_presets.setItem(r, 2, QTableWidgetItem(" ".join(badges) or "-"))
            self.tbl_presets.setItem(r, 3, QTableWidgetItem(upd_str))

        has_rows = bool(self._presets)
        if has_rows:
            self.tbl_presets.selectRow(0)
        else:
            self.preview_box.clear()
        self.btn_load_preset.setEnabled(has_rows)
        self.btn_delete_preset.setEnabled(has_rows)

    def _on_selection_changed(self):
        p = self._current_preset()
        if not p:
            self.preview_box.clear()
            return
        lines = [f"ชุดโพสต์: {p['name']}"]
        if p.get("message"):
            lines.append(f"ข้อความ: {p['message'][:240]}")
        if p.get("link"):
            lines.append(f"ลิงก์: {p['link']}")
        if p.get("video_path"):
            kind = "Facebook Reels" if p.get("is_reel") else "วิดีโอปกติ"
            lines.append(f"วิดีโอ ({kind}): {p['video_path']}")
        elif p.get("image_paths"):
            lines.append(f"รูปภาพ ({len(p['image_paths'])} รูป): " + ", ".join(Path(x).name for x in p["image_paths"][:5]))
        if p.get("watermark_enabled") and p.get("watermark_text"):
            lines.append(f"ลายน้ำ: {p['watermark_text']}")
        if p.get("place_id"):
            lines.append(f"เช็คอิน: {p.get('place_name') or p['place_id']} ({p['place_id']})")
        if p.get("comments"):
            lines.append(f"คอมเมนต์ต่อท้าย: {len(p['comments'])} รายการ (หน่วง {p.get('comment_delay', 2.0):.0f} วิ)")
        self.preview_box.setPlainText("\n".join(lines))

    def _save_current_from_composer(self):
        if not self._composer_snapshot:
            return
        name = self.input_preset_name.text().strip()
        if not name:
            QMessageBox.information(self, "กรุณาตั้งชื่อ", "กรุณาพิมพ์ชื่อชุดโพสต์ก่อนกดบันทึก")
            return
        existing_names = {p["name"].casefold() for p in self._presets}
        if name.casefold() in existing_names:
            ans = QMessageBox.question(
                self,
                "มีชื่อชุดโพสต์นี้อยู่แล้ว",
                f"มีชุดโพสต์ชื่อ '{name}' อยู่แล้ว ต้องการบันทึกทับใช่หรือไม่?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if ans != QMessageBox.Yes:
                return
        snap = self._composer_snapshot
        try:
            db.save_post_preset(
                name=name,
                message=snap.get("message", ""),
                link=snap.get("link", ""),
                image_paths=snap.get("image_paths", []),
                video_path=snap.get("video_path", ""),
                is_reel=bool(snap.get("is_reel", False)),
                watermark_enabled=bool(snap.get("watermark_enabled", False)),
                watermark_text=snap.get("watermark_text", ""),
                place_id=snap.get("place_id", ""),
                place_name=snap.get("place_name", ""),
                comments=snap.get("comments", []),
                comment_delay=float(snap.get("comment_delay", 2.0)),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "บันทึกชุดโพสต์ไม่สำเร็จ", str(exc))
            return
        self.input_preset_name.clear()
        self._refresh_table()

    def _load_selected(self):
        p = self._current_preset()
        if not p:
            QMessageBox.information(self, "ยังไม่ได้เลือกชุดโพสต์", "กรุณาเลือกชุดโพสต์ในตารางก่อน")
            return
        self.selected_preset = p
        self.accept()

    def _delete_selected(self):
        p = self._current_preset()
        if not p:
            return
        ans = QMessageBox.question(
            self,
            "ยืนยันการลบชุดโพสต์",
            f"ต้องการลบชุดโพสต์ '{p['name']}' ใช่หรือไม่?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        db.delete_post_preset(int(p["id"]))
        self._refresh_table()


class AutoReplyRulesDialog(QDialog):
    """Configure Keyword Auto-Reply rules, background polling schedule, and view deduplication log."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ตั้งค่าตอบคอมเมนต์อัตโนมัติตามคีย์เวิร์ด (Keyword Auto-Reply)")
        self.setMinimumSize(820, 580)
        self.run_now_requested: bool = False
        self._rules: list[dict] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        top_card = QFrame()
        top_card.setObjectName("scheduleCard")
        top_row = QHBoxLayout(top_card)
        top_row.setContentsMargins(12, 8, 12, 8)
        top_row.setSpacing(10)

        self.chk_auto_enable = IosToggleSwitch("ทำงานอัตโนมัติเบื้องหลัง")
        self.chk_auto_enable.set_dark_mode(config.get_theme() == "dark")
        self.chk_auto_enable.setChecked(config.get_auto_reply_enabled())

        lbl_iv = QLabel("ความถี่ตรวจ:")
        lbl_iv.setObjectName("mutedLabel")
        self.combo_interval = QComboBox()
        for label, sec in (
            ("ทุก 1 นาที", 60),
            ("ทุก 2 นาที", 120),
            ("ทุก 5 นาที", 300),
            ("ทุก 10 นาที", 600),
            ("ทุก 15 นาที", 900),
            ("ทุก 30 นาที", 1800),
        ):
            self.combo_interval.addItem(label, sec)
        cur_iv = config.get_auto_reply_interval_sec()
        matched_iv = 2
        for idx in range(self.combo_interval.count()):
            if int(self.combo_interval.itemData(idx)) == cur_iv:
                matched_iv = idx
                break
        self.combo_interval.setCurrentIndex(matched_iv)
        self.chk_auto_enable.toggled.connect(self._persist_schedule_settings)
        self.combo_interval.currentIndexChanged.connect(self._persist_schedule_settings)

        self.lbl_total_replied = QLabel("")
        self.lbl_total_replied.setObjectName("mutedLabel")

        self.btn_run_now = QPushButton("⚡ ตรวจและตอบตอนนี้")
        self.btn_run_now.setObjectName("primaryBtn")
        self.btn_run_now.setCursor(Qt.PointingHandCursor)
        self.btn_run_now.setToolTip("สแกนโพสต์ล่าสุดของเพจที่ติ๊กเลือกทางซ้ายและตอบตามกฎทันที")
        self.btn_run_now.clicked.connect(self._trigger_run_now)

        top_row.addWidget(self.chk_auto_enable)
        top_row.addWidget(lbl_iv)
        top_row.addWidget(self.combo_interval)
        top_row.addStretch(1)
        top_row.addWidget(self.lbl_total_replied)
        top_row.addWidget(self.btn_run_now)
        layout.addWidget(top_card)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_rules_tab(), "1. กฎคีย์เวิร์ด (Rules)")
        self.tabs.addTab(self._build_log_tab(), "2. ประวัติที่ตอบไปแล้ว (กันตอบซ้ำ)")
        layout.addWidget(self.tabs, 1)

        footer = QHBoxLayout()
        note = QLabel(
            "ระบบบันทึก comment_id ทุกครั้งที่ตอบสำเร็จเพื่อไม่ตอบซ้ำ และข้ามคอมเมนต์ของเพจตัวเองอัตโนมัติ"
        )
        note.setObjectName("mutedLabel")
        btn_close = QPushButton("บันทึกและปิด")
        btn_close.setObjectName("primaryBtn")
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.clicked.connect(self._save_and_close)
        footer.addWidget(note, 1)
        footer.addWidget(btn_close)
        layout.addLayout(footer)

        self._refresh_rules()
        self._refresh_log()

    def _build_rules_tab(self) -> QWidget:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)

        self.tbl_rules = QTableWidget(0, 6)
        self.tbl_rules.setHorizontalHeaderLabels(
            ["สถานะ", "ชื่อกฎ", "โหมดตรวจคำ", "คีย์เวิร์ด", "ข้อความตอบกลับ", "ซ่อนเม้นต์"]
        )
        self.tbl_rules.verticalHeader().setVisible(False)
        self.tbl_rules.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_rules.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tbl_rules.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hdr = self.tbl_rules.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.Stretch)
        hdr.setSectionResizeMode(4, QHeaderView.Stretch)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.tbl_rules.itemSelectionChanged.connect(self._load_selected_rule_into_form)
        lay.addWidget(self.tbl_rules, 1)

        form_card = QFrame()
        form_card.setObjectName("scheduleCard")
        flay = QVBoxLayout(form_card)
        flay.setContentsMargins(12, 10, 12, 10)
        flay.setSpacing(6)

        r1 = QHBoxLayout()
        r1.setSpacing(8)
        self.input_rule_name = QLineEdit()
        self.input_rule_name.setPlaceholderText("ชื่อกฎ (เช่น สอบถามราคา / ขอเลขบัญชี)")
        self.combo_match_mode = QComboBox()
        self.combo_match_mode.addItem("มีคำนี้อยู่ในเม้นต์ (Contains)", "contains")
        self.combo_match_mode.addItem("ตรงทั้งประโยค (Exact)", "exact")
        self.chk_rule_enabled = QCheckBox("เปิดใช้งาน")
        self.chk_rule_enabled.setChecked(True)
        self.chk_hide_after = QCheckBox("ซ่อนคอมเมนต์หลังตอบ")
        self.chk_hide_after.setToolTip("ซ่อนคอมเมนต์ลูกค้าบนโพสต์หลังตอบกลับสำเร็จ (ป้องกันเพจคู่แข่งทักตัดหน้า)")
        r1.addWidget(self.input_rule_name, 1)
        r1.addWidget(self.combo_match_mode)
        r1.addWidget(self.chk_rule_enabled)
        r1.addWidget(self.chk_hide_after)
        flay.addLayout(r1)

        self.input_keywords = QLineEdit()
        self.input_keywords.setPlaceholderText(
            "คีย์เวิร์ด (คั่นด้วยเครื่องหมายจุลภาค , เช่น ราคา, เท่าไหร่, สนใจ, cf)"
        )
        flay.addWidget(self.input_keywords)

        self.editor_reply_template = QPlainTextEdit()
        self.editor_reply_template.setPlaceholderText(
            "ข้อความตอบกลับอัตโนมัติ (รองรับ {commenter_name}, {page_name}, {date}, {time} และ Spintax {สวัสดีครับ|ยินดีให้บริการครับ})"
        )
        self.editor_reply_template.setMaximumHeight(78)
        flay.addWidget(self.editor_reply_template)

        act_row = QHBoxLayout()
        act_row.setSpacing(8)
        self.btn_add_rule = QPushButton("+ เพิ่มเป็นกฎใหม่")
        self.btn_add_rule.setObjectName("primaryBtn")
        self.btn_add_rule.setCursor(Qt.PointingHandCursor)
        self.btn_add_rule.clicked.connect(self._add_new_rule)

        self.btn_update_rule = QPushButton("บันทึกแก้ไขกฎที่เลือก")
        self.btn_update_rule.setObjectName("ghostBtn")
        self.btn_update_rule.setCursor(Qt.PointingHandCursor)
        self.btn_update_rule.clicked.connect(self._update_selected_rule)

        self.btn_toggle_rule = QPushButton("สลับเปิด/ปิด")
        self.btn_toggle_rule.setObjectName("ghostBtn")
        self.btn_toggle_rule.setCursor(Qt.PointingHandCursor)
        self.btn_toggle_rule.clicked.connect(self._toggle_selected_rule)

        self.btn_delete_rule = QPushButton("ลบกฎที่เลือก")
        self.btn_delete_rule.setObjectName("dangerBtn")
        self.btn_delete_rule.setCursor(Qt.PointingHandCursor)
        self.btn_delete_rule.clicked.connect(self._delete_selected_rule)

        act_row.addWidget(self.btn_add_rule)
        act_row.addWidget(self.btn_update_rule)
        act_row.addWidget(self.btn_toggle_rule)
        act_row.addStretch(1)
        act_row.addWidget(self.btn_delete_rule)
        flay.addLayout(act_row)

        lay.addWidget(form_card)
        return tab

    def _build_log_tab(self) -> QWidget:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)

        self.tbl_reply_log = QTableWidget(0, 5)
        self.tbl_reply_log.setHorizontalHeaderLabels(
            ["เวลาที่ตอบ", "Page ID", "ผู้คอมเมนต์", "คอมเมนต์ลูกค้า", "ข้อความที่ตอบกลับ"]
        )
        self.tbl_reply_log.verticalHeader().setVisible(False)
        self.tbl_reply_log.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_reply_log.setEditTriggers(QAbstractItemView.NoEditTriggers)
        lhdr = self.tbl_reply_log.horizontalHeader()
        lhdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        lhdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        lhdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        lhdr.setSectionResizeMode(3, QHeaderView.Stretch)
        lhdr.setSectionResizeMode(4, QHeaderView.Stretch)
        lay.addWidget(self.tbl_reply_log, 1)
        return tab

    def _selected_rule(self) -> dict | None:
        row = self.tbl_rules.currentRow()
        if row < 0 or row >= len(self._rules):
            return None
        return self._rules[row]

    def _refresh_rules(self):
        self._rules = db.list_auto_reply_rules()
        self.tbl_rules.setRowCount(len(self._rules))
        for r, rule in enumerate(self._rules):
            st_item = QTableWidgetItem("🟢 เปิด" if rule["enabled"] else "⚪ ปิด")
            st_item.setData(Qt.UserRole, rule["id"])
            mode_txt = "ตรงทั้งข้อความ" if rule["match_mode"] == "exact" else "มีคำในเม้นต์"
            kw_txt = ", ".join(rule["keywords"])
            rep_txt = rule["reply_template"].replace("\n", " ")[:70]
            hide_txt = "ซ่อน" if rule["hide_after_reply"] else "-"
            self.tbl_rules.setItem(r, 0, st_item)
            self.tbl_rules.setItem(r, 1, QTableWidgetItem(rule["name"]))
            self.tbl_rules.setItem(r, 2, QTableWidgetItem(mode_txt))
            self.tbl_rules.setItem(r, 3, QTableWidgetItem(kw_txt))
            self.tbl_rules.setItem(r, 4, QTableWidgetItem(rep_txt))
            self.tbl_rules.setItem(r, 5, QTableWidgetItem(hide_txt))
        has_sel = self._selected_rule() is not None
        self.btn_update_rule.setEnabled(has_sel)
        self.btn_toggle_rule.setEnabled(has_sel)
        self.btn_delete_rule.setEnabled(has_sel)
        self.lbl_total_replied.setText(f"ตอบไปแล้วสะสม {db.count_auto_replies():,} คอมเมนต์")

    def _refresh_log(self):
        logs = db.list_recent_auto_replies(limit=100)
        self.tbl_reply_log.setRowCount(len(logs))
        for r, item in enumerate(logs):
            try:
                t_disp = datetime.fromisoformat(item["replied_at"]).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                t_disp = str(item.get("replied_at") or "")
            self.tbl_reply_log.setItem(r, 0, QTableWidgetItem(t_disp))
            self.tbl_reply_log.setItem(r, 1, QTableWidgetItem(str(item.get("page_id") or "")))
            self.tbl_reply_log.setItem(r, 2, QTableWidgetItem(str(item.get("commenter_name") or "")))
            self.tbl_reply_log.setItem(r, 3, QTableWidgetItem(str(item.get("comment_text") or "")))
            self.tbl_reply_log.setItem(r, 4, QTableWidgetItem(str(item.get("reply_text") or "")))

    def _load_selected_rule_into_form(self):
        rule = self._selected_rule()
        has_sel = rule is not None
        self.btn_update_rule.setEnabled(has_sel)
        self.btn_toggle_rule.setEnabled(has_sel)
        self.btn_delete_rule.setEnabled(has_sel)
        if not rule:
            return
        self.input_rule_name.setText(rule["name"])
        for idx in range(self.combo_match_mode.count()):
            if self.combo_match_mode.itemData(idx) == rule["match_mode"]:
                self.combo_match_mode.setCurrentIndex(idx)
                break
        self.chk_rule_enabled.setChecked(bool(rule["enabled"]))
        self.chk_hide_after.setChecked(bool(rule["hide_after_reply"]))
        self.input_keywords.setText(", ".join(rule["keywords"]))
        self.editor_reply_template.setPlainText(rule["reply_template"])

    def _add_new_rule(self):
        try:
            db.save_auto_reply_rule(
                name=self.input_rule_name.text(),
                keywords=self.input_keywords.text(),
                match_mode=str(self.combo_match_mode.currentData() or "contains"),
                reply_template=self.editor_reply_template.toPlainText(),
                hide_after_reply=self.chk_hide_after.isChecked(),
                enabled=self.chk_rule_enabled.isChecked(),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "ข้อมูลกฎไม่ครบถ้วน", str(exc))
            return
        self.input_rule_name.clear()
        self.input_keywords.clear()
        self.editor_reply_template.clear()
        self.chk_hide_after.setChecked(False)
        self.chk_rule_enabled.setChecked(True)
        self._refresh_rules()

    def _update_selected_rule(self):
        rule = self._selected_rule()
        if not rule:
            return
        try:
            db.save_auto_reply_rule(
                rule_id=int(rule["id"]),
                name=self.input_rule_name.text(),
                keywords=self.input_keywords.text(),
                match_mode=str(self.combo_match_mode.currentData() or "contains"),
                reply_template=self.editor_reply_template.toPlainText(),
                hide_after_reply=self.chk_hide_after.isChecked(),
                enabled=self.chk_rule_enabled.isChecked(),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "อัปเดตกฎไม่สำเร็จ", str(exc))
            return
        self._refresh_rules()

    def _toggle_selected_rule(self):
        rule = self._selected_rule()
        if not rule:
            return
        db.set_auto_reply_rule_enabled(int(rule["id"]), not bool(rule["enabled"]))
        self._refresh_rules()

    def _delete_selected_rule(self):
        rule = self._selected_rule()
        if not rule:
            return
        ans = QMessageBox.question(
            self,
            "ยืนยันการลบกฎ",
            f"ต้องการลบกฎ '{rule['name']}' ใช่หรือไม่?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        db.delete_auto_reply_rule(int(rule["id"]))
        self._refresh_rules()

    def _persist_schedule_settings(self, *_args):
        config.save_auto_reply_enabled(self.chk_auto_enable.isChecked())
        config.save_auto_reply_interval_sec(int(self.combo_interval.currentData() or 300))

    def _trigger_run_now(self):
        self._persist_schedule_settings()
        if not db.list_auto_reply_rules(only_enabled=True):
            QMessageBox.information(
                self,
                "ยังไม่มีกฎที่เปิดใช้งาน",
                "กรุณาเพิ่มและเปิดใช้งานกฎคีย์เวิร์ดอย่างน้อย 1 ข้อก่อนกดตรวจตอบคอมเมนต์",
            )
            return
        self.run_now_requested = True
        self.accept()

    def _save_and_close(self):
        self._persist_schedule_settings()
        self.accept()


class TemplatesDialog(QDialog):
    """Manage reusable Caption Templates and Hashtag Sets."""

    def __init__(self, initial_text: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("จัดการเทมเพลตแคปชัน & ชุดแฮชแท็ก")
        self.setMinimumSize(560, 460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        lbl_add = QLabel("เพิ่มเทมเพลตใหม่")
        lbl_add.setObjectName("sectionTitle")
        layout.addWidget(lbl_add)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        self.input_name = QLineEdit()
        self.input_name.setPlaceholderText("ชื่อเทมเพลต เช่น แคปชันขายของ, แฮชแท็กกาแฟ")
        self.combo_kind = QComboBox()
        self.combo_kind.addItem("เทมเพลตแคปชัน", "caption")
        self.combo_kind.addItem("ชุดแฮชแท็ก", "hashtag")
        self.combo_kind.setFixedWidth(150)
        top_row.addWidget(self.input_name, 1)
        top_row.addWidget(self.combo_kind)
        layout.addLayout(top_row)

        self.editor_content = QPlainTextEdit()
        self.editor_content.setPlaceholderText(
            "พิมพ์ข้อความแคปชัน หรือชุดแฮชแท็ก เช่น #กาแฟสด #โปรโมชัน …"
        )
        if initial_text:
            self.editor_content.setPlainText(initial_text)
        self.editor_content.setMaximumHeight(110)
        layout.addWidget(self.editor_content)

        btn_save_tpl = QPushButton("+ บันทึกเทมเพลต")
        btn_save_tpl.setObjectName("primaryBtn")
        btn_save_tpl.setCursor(Qt.PointingHandCursor)
        btn_save_tpl.clicked.connect(self._add_template)
        layout.addWidget(btn_save_tpl)

        lbl_list = QLabel("เทมเพลตที่บันทึกไว้")
        lbl_list.setObjectName("sectionTitle")
        layout.addWidget(lbl_list)

        self.list_tpls = QListWidget()
        layout.addWidget(self.list_tpls, 1)

        bottom = QHBoxLayout()
        btn_del = QPushButton("ลบรายการที่เลือก")
        btn_del.setObjectName("dangerBtn")
        btn_del.setCursor(Qt.PointingHandCursor)
        btn_del.clicked.connect(self._delete_selected)
        btn_close = QPushButton("ปิด")
        btn_close.setObjectName("ghostBtn")
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.clicked.connect(self.accept)
        bottom.addWidget(btn_del)
        bottom.addStretch(1)
        bottom.addWidget(btn_close)
        layout.addLayout(bottom)

        self._refresh()

    def _refresh(self):
        self.list_tpls.clear()
        for t in db.list_templates():
            tag = "🏷 แฮชแท็ก" if t["kind"] == "hashtag" else "📝 แคปชัน"
            snippet = t["content"].replace("\n", " ")[:60]
            it = QListWidgetItem(f"[{tag}] {t['name']} — {snippet}")
            it.setData(Qt.UserRole, t["id"])
            it.setToolTip(t["content"])
            self.list_tpls.addItem(it)

    def _add_template(self):
        name = self.input_name.text().strip()
        content = self.editor_content.toPlainText().strip()
        if not name or not content:
            QMessageBox.information(self, "ข้อมูลไม่ครบ", "กรุณากรอกทั้งชื่อเทมเพลตและข้อความ")
            return
        db.add_template(name, self.combo_kind.currentData(), content)
        self.input_name.clear()
        self.editor_content.clear()
        self._refresh()

    def _delete_selected(self):
        it = self.list_tpls.currentItem()
        if not it:
            return
        db.delete_template(int(it.data(Qt.UserRole)))
        self._refresh()


class EditPostDialog(QDialog):
    """Dialog for editing the text/caption of one or multiple published Facebook posts in-place."""

    def __init__(
        self,
        page_name: str,
        post_id: str,
        current_message: str,
        parent=None,
        *,
        page_id: str = "",
        current_link: str = "",
        current_images: list[str] | None = None,
        current_video: str = "",
        current_place_id: str = "",
        current_place_name: str = "",
        entries: list[dict] | None = None,
        batch_entries: list[dict] | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("แก้ไขข้อความโพสต์บน Facebook (รองรับหลายเพจ)")
        self.setMinimumSize(580, 420)

        is_dark = config.get_theme() == "dark"
        self._single_page_id = (page_id or "").strip()
        self._single_page_name = page_name
        self._single_post_id = post_id
        self._entries = list(entries or [])
        self._batch_entries = list(batch_entries or [])
        self._initial_link = (current_link or "").strip()
        self._initial_images = [p for p in (current_images or []) if p]
        self._initial_video = (current_video or "").strip()
        self._initial_place_id = (current_place_id or "").strip()
        self._initial_place_name = (current_place_name or "").strip()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        self.lbl_meta = QLabel()
        self.lbl_meta.setObjectName("sectionTitle")
        self.lbl_meta.setTextFormat(Qt.PlainText)
        self.lbl_meta.setWordWrap(True)
        layout.addWidget(self.lbl_meta)

        self.chk_edit_whole_batch: IosToggleSwitch | None = None
        if len(self._batch_entries) > len(self._entries) and len(self._batch_entries) > 1:
            batch_names = ", ".join(e["page_name"] for e in self._batch_entries[:4])
            if len(self._batch_entries) > 4:
                batch_names += f" (+อีก {len(self._batch_entries) - 4} เพจ)"
            self.chk_edit_whole_batch = IosToggleSwitch(
                f"แก้ไขข้อความทุกเพจในชุดโพสต์เดียวกันพร้อมกัน ({len(self._batch_entries)} เพจ: {batch_names})"
            )
            self.chk_edit_whole_batch.set_dark_mode(is_dark)
            self.chk_edit_whole_batch.setChecked(len(self._entries) <= 1)
            self.chk_edit_whole_batch.toggled.connect(self._sync_meta_header)
            layout.addWidget(self.chk_edit_whole_batch)

        self._sync_meta_header()

        hdr = QHBoxLayout()
        lbl = QLabel("ข้อความโพสต์ใหม่ (รองรับ Spintax {A|B} และ {page_name}, {date}, {time}):")
        lbl.setObjectName("sectionTitle")
        self.lbl_chars = QLabel("0 ตัวอักษร")
        self.lbl_chars.setObjectName("mutedLabel")
        hdr.addWidget(lbl)
        hdr.addStretch(1)
        hdr.addWidget(self.lbl_chars)
        layout.addLayout(hdr)

        self.editor = QPlainTextEdit()
        self.editor.setPlainText(current_message or "")
        self.editor.setPlaceholderText("พิมพ์ข้อความใหม่ที่ต้องการอัปเดตบน Facebook…")
        self.editor.setMinimumHeight(140)
        self.editor.textChanged.connect(self._update_chars)
        layout.addWidget(self.editor, 1)

        # Read-only summary of existing media/check-in + button to open post on Facebook Web
        existing_parts: list[str] = []
        if self._initial_video:
            existing_parts.append(f"🎬 วิดีโอ ({Path(self._initial_video).name})")
        elif self._initial_images:
            existing_parts.append(f"🖼️ รูปภาพ {len(self._initial_images)} รูป")
        if self._initial_link:
            existing_parts.append(f"🔗 {self._initial_link}")
        if self._initial_place_id:
            existing_parts.append(f"📍 {self._initial_place_name or self._initial_place_id}")

        info_card = QFrame()
        info_card.setObjectName("scheduleCard")
        info_layout = QVBoxLayout(info_card)
        info_layout.setContentsMargins(12, 10, 12, 10)
        info_layout.setSpacing(6)
        if existing_parts:
            lbl_existing = QLabel("สื่อ/เช็คอินเดิมของโพสต์นี้: " + "  •  ".join(existing_parts))
            lbl_existing.setObjectName("dropMainLabel")
            lbl_existing.setTextFormat(Qt.PlainText)
            lbl_existing.setWordWrap(True)
            info_layout.addWidget(lbl_existing)

        note = QLabel(
            "• การแก้ไขนี้จะอัปเดตเฉพาะ 'ข้อความ' ทับโพสต์เดิมโดยตรง (รักษา Post ID, รูป/วิดีโอ, เช็คอิน, ยอดไลก์ และคอมเมนต์เดิมครบถ้วน — ไม่ลบโพสต์เดิม)\n"
            "• Facebook API ไม่อนุญาตให้เปลี่ยนรูปภาพ/วิดีโอ หรือเพิ่มกิจกรรมบนโพสต์ที่เผยแพร่แล้ว หากต้องการแก้สื่อ กรุณากดปุ่มด้านล่างเพื่อเปิดแก้บนเว็บ Facebook โดยตรง"
        )
        note.setObjectName("mutedLabel")
        note.setWordWrap(True)
        info_layout.addWidget(note)

        web_row = QHBoxLayout()
        self.btn_open_fb_web = QPushButton("เปิดแก้สื่อ/เช็คอินบนเว็บ Facebook")
        self.btn_open_fb_web.setObjectName("ghostBtn")
        self.btn_open_fb_web.setCursor(Qt.PointingHandCursor)
        self.btn_open_fb_web.clicked.connect(self._open_post_on_facebook)
        web_row.addWidget(self.btn_open_fb_web)
        web_row.addStretch(1)
        info_layout.addLayout(web_row)
        layout.addWidget(info_card)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("บันทึกข้อความไปยัง Facebook")
        buttons.button(QDialogButtonBox.Save).setObjectName("primaryBtn")
        buttons.button(QDialogButtonBox.Cancel).setText("ยกเลิก")
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._update_chars()

    def _sync_meta_header(self, *_args):
        targets = self.target_entries()
        if len(targets) > 1:
            names = ", ".join(e["page_name"] for e in targets[:4])
            if len(targets) > 4:
                names += f" (+อีก {len(targets) - 4} เพจ)"
            self.lbl_meta.setText(f"แก้ไขข้อความพร้อมกัน {len(targets)} เพจ: {names}")
        elif len(targets) == 1:
            e = targets[0]
            self.lbl_meta.setText(f"เพจ: {e['page_name']}  •  Post ID: {e['remote_post_id']}")
        else:
            self.lbl_meta.setText(f"เพจ: {self._single_page_name}  •  Post ID: {self._single_post_id}")

    def target_entries(self) -> list[dict]:
        if (
            self.chk_edit_whole_batch is not None
            and self.chk_edit_whole_batch.isChecked()
            and self._batch_entries
        ):
            return list(self._batch_entries)
        return list(self._entries)

    def _open_post_on_facebook(self):
        targets = self.target_entries()
        opened = 0
        if targets:
            for e in targets[:5]:
                url = facebook.build_post_permalink(
                    e.get("page_id") or "",
                    e.get("remote_post_id") or "",
                    is_reel=bool(e.get("is_reel")),
                )
                if url:
                    webbrowser.open(url)
                    opened += 1
        elif self._single_post_id:
            url = facebook.build_post_permalink(self._single_page_id, self._single_post_id)
            if url:
                webbrowser.open(url)
                opened = 1
        if not opened:
            QMessageBox.warning(self, "เปิดลิงก์ไม่ได้", "ไม่พบ Post ID สำหรับเปิดบน Facebook")

    def _update_chars(self):
        n = len(self.editor.toPlainText())
        if n > FB_MAX_CHARS:
            self.lbl_chars.setText(f"{n:,} / {FB_MAX_CHARS:,} ตัวอักษร (เกินขีดจำกัด)")
            self.lbl_chars.setStyleSheet("color: #ff453a; font-weight: 700;")
        else:
            self.lbl_chars.setText(f"{n:,} ตัวอักษร")
            self.lbl_chars.setStyleSheet("")

    def _validate_and_accept(self):
        txt = self.editor.toPlainText().strip()
        if not txt:
            QMessageBox.information(self, "ข้อความว่าง", "กรุณาพิมพ์ข้อความที่ต้องการแก้ไข")
            return
        if len(txt) > FB_MAX_CHARS:
            QMessageBox.warning(
                self, "ข้อความยาวเกินไป", f"ข้อความต้องไม่เกิน {FB_MAX_CHARS:,} ตัวอักษร"
            )
            return
        self.accept()

    def new_message(self) -> str:
        return self.editor.toPlainText().strip()


class PostCommentsDialog(QDialog):
    """Inbox dialog for reading comments on a Page post and replying as the Page."""

    def __init__(self, page_id: str, page_name: str, post_id: str, parent=None):
        super().__init__(parent)
        self.page_id = page_id
        self.page_name = page_name
        self.post_id = post_id
        self._worker: Worker | None = None

        self.setWindowTitle(f"กล่องตอบคอมเมนต์ — {page_name}")
        self.setMinimumSize(680, 500)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        hdr = QHBoxLayout()
        lbl_meta = QLabel(f"เพจ: {page_name}  •  Post ID: {post_id}")
        lbl_meta.setObjectName("sectionTitle")
        lbl_meta.setTextFormat(Qt.PlainText)
        self.btn_refresh = QPushButton("รีเฟรชคอมเมนต์")
        self.btn_refresh.setObjectName("ghostBtn")
        self.btn_refresh.setCursor(Qt.PointingHandCursor)
        self.btn_refresh.clicked.connect(self.load_comments)
        hdr.addWidget(lbl_meta, 1)
        hdr.addWidget(self.btn_refresh)
        layout.addLayout(hdr)

        self.list_inbox = QListWidget()
        self.list_inbox.currentItemChanged.connect(self._sync_hide_button_label)
        layout.addWidget(self.list_inbox, 1)

        lbl_reply = QLabel("พิมพ์ข้อความตอบกลับคอมเมนต์ที่เลือก (ในนามเพจ):")
        lbl_reply.setObjectName("sectionTitle")
        layout.addWidget(lbl_reply)

        self.editor_reply = QPlainTextEdit()
        self.editor_reply.setPlaceholderText("คลิกเลือกคอมเมนต์ด้านบน แล้วพิมพ์ข้อความตอบกลับที่นี่…")
        self.editor_reply.setMaximumHeight(90)
        layout.addWidget(self.editor_reply)

        self.lbl_status = QLabel("พร้อมใช้งาน")
        self.lbl_status.setObjectName("mutedLabel")
        self.lbl_status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.lbl_status)

        bottom = QHBoxLayout()
        self.btn_send_reply = QPushButton("ส่งข้อความตอบกลับในนามเพจ")
        self.btn_send_reply.setObjectName("primaryBtn")
        self.btn_send_reply.setCursor(Qt.PointingHandCursor)
        self.btn_send_reply.clicked.connect(self.send_reply)
        self.btn_hide_comment = QPushButton("ซ่อนคอมเมนต์")
        self.btn_hide_comment.setObjectName("ghostBtn")
        self.btn_hide_comment.setCursor(Qt.PointingHandCursor)
        self.btn_hide_comment.clicked.connect(self.hide_selected_comment)
        self.btn_delete_comment = QPushButton("ลบคอมเมนต์")
        self.btn_delete_comment.setObjectName("dangerBtn")
        self.btn_delete_comment.setCursor(Qt.PointingHandCursor)
        self.btn_delete_comment.clicked.connect(self.delete_selected_comment)
        btn_close = QPushButton("ปิด")
        btn_close.setObjectName("ghostBtn")
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.clicked.connect(self.accept)
        bottom.addWidget(self.btn_send_reply, 1)
        bottom.addWidget(self.btn_hide_comment)
        bottom.addWidget(self.btn_delete_comment)
        bottom.addWidget(btn_close)
        layout.addLayout(bottom)

    def _set_dialog_busy(self, busy: bool):
        for b in (
            self.btn_refresh,
            self.btn_send_reply,
            self.btn_hide_comment,
            self.btn_delete_comment,
        ):
            b.setEnabled(not busy)

    def _run_task(self, fn, on_ok, on_err):
        if self._worker and self._worker.isRunning():
            return
        self._set_dialog_busy(True)
        self._worker = Worker(fn)
        self._worker.done.connect(on_ok)
        self._worker.failed.connect(on_err)
        self._worker.finished.connect(lambda: self._set_dialog_busy(False))
        self._worker.start()

    def _sync_hide_button_label(self, *_args):
        it = self.list_inbox.currentItem()
        is_hidden = bool(it.data(Qt.UserRole + 1)) if it else False
        self.btn_hide_comment.setText("เลิกซ่อนคอมเมนต์" if is_hidden else "ซ่อนคอมเมนต์")

    def _populate_comments(self, comments: list[dict]):
        self.list_inbox.clear()
        for c in comments:
            t_str = c["created_time"][:16].replace("T", " ")
            msg_line = c["message"].replace("\n", " ")
            replies_tag = f" (ตอบแล้ว {c['reply_count']})" if c.get("reply_count") else ""
            is_hidden = bool(c.get("is_hidden", False))
            prefix = "[ซ่อนแล้ว] " if is_hidden else ""
            it = QListWidgetItem(f"{prefix}[{t_str}] {c['author']}: {msg_line}{replies_tag}")
            it.setData(Qt.UserRole, c["id"])
            it.setData(Qt.UserRole + 1, is_hidden)
            it.setToolTip(f"Comment ID: {c['id']}\nผู้เขียน: {c['author']}\nข้อความ:\n{c['message']}")
            self.list_inbox.addItem(it)
        self._sync_hide_button_label()

    def load_comments(self):
        self.lbl_status.setText("กำลังดึงคอมเมนต์จาก Facebook…")

        def ok(comments: list[dict]):
            self._populate_comments(comments)
            if comments:
                self.lbl_status.setText(f"โหลดคอมเมนต์แล้ว {len(comments)} รายการ")
            else:
                self.lbl_status.setText("ยังไม่มีคอมเมนต์ใต้โพสต์นี้")

        self._run_task(
            lambda: facebook.list_post_comments(self.page_id, self.post_id, limit=50),
            ok,
            lambda err: self.lbl_status.setText(f"❌ โหลดไม่สำเร็จ: {err}"),
        )

    def send_reply(self):
        it = self.list_inbox.currentItem()
        if not it:
            QMessageBox.information(self, "ยังไม่ได้เลือกคอมเมนต์", "กรุณาคลิกเลือกคอมเมนต์ที่ต้องการตอบกลับก่อน")
            return
        cid = str(it.data(Qt.UserRole) or "")
        reply_text = self.editor_reply.toPlainText().strip()
        if not reply_text:
            QMessageBox.information(self, "ข้อความว่าง", "กรุณาพิมพ์ข้อความตอบกลับก่อนส่ง")
            return
        if len(reply_text) > FB_MAX_CHARS:
            QMessageBox.warning(
                self, "ข้อความยาวเกินไป", f"ข้อความตอบกลับต้องไม่เกิน {FB_MAX_CHARS:,} ตัวอักษร"
            )
            return
        self.lbl_status.setText("กำลังส่งข้อความตอบกลับ…")

        def job():
            rid = facebook.reply_to_comment(self.page_id, cid, reply_text)
            try:
                updated = facebook.list_post_comments(self.page_id, self.post_id, limit=50)
            except (RuntimeError, ValueError):
                updated = None
            return rid, updated

        def ok(payload):
            reply_id, updated = payload
            self.editor_reply.clear()
            if updated is not None:
                self._populate_comments(updated)
            self.lbl_status.setText(f"✅ ตอบกลับสำเร็จ (ID: {reply_id})")

        self._run_task(
            job,
            ok,
            lambda err: self.lbl_status.setText(f"❌ ตอบกลับไม่สำเร็จ: {err}"),
        )

    def hide_selected_comment(self):
        it = self.list_inbox.currentItem()
        if not it:
            QMessageBox.information(self, "ยังไม่ได้เลือกคอมเมนต์", "กรุณาคลิกเลือกคอมเมนต์ที่ต้องการซ่อนหรือเลิกซ่อนก่อน")
            return
        cid = str(it.data(Qt.UserRole) or "")
        currently_hidden = bool(it.data(Qt.UserRole + 1))
        target_hidden = not currently_hidden
        self.lbl_status.setText(
            "กำลังซ่อนคอมเมนต์บน Facebook…" if target_hidden else "กำลังเลิกซ่อนคอมเมนต์บน Facebook…"
        )

        def ok(_res):
            it.setData(Qt.UserRole + 1, target_hidden)
            cur_text = it.text()
            if target_hidden:
                if not cur_text.startswith("[ซ่อนแล้ว] "):
                    it.setText(f"[ซ่อนแล้ว] {cur_text}")
                self.lbl_status.setText(f"✅ ซ่อนคอมเมนต์ {cid} บน Facebook เรียบร้อยแล้ว")
            else:
                if cur_text.startswith("[ซ่อนแล้ว] "):
                    it.setText(cur_text[len("[ซ่อนแล้ว] "):])
                self.lbl_status.setText(f"✅ เลิกซ่อนคอมเมนต์ {cid} บน Facebook เรียบร้อยแล้ว")
            self._sync_hide_button_label()

        action_label = "ซ่อน" if target_hidden else "เลิกซ่อน"
        self._run_task(
            lambda: facebook.set_comment_hidden(self.page_id, cid, target_hidden),
            ok,
            lambda err: self.lbl_status.setText(f"❌ {action_label}คอมเมนต์ไม่สำเร็จ: {err}"),
        )

    def delete_selected_comment(self):
        it = self.list_inbox.currentItem()
        if not it:
            QMessageBox.information(self, "ยังไม่ได้เลือกคอมเมนต์", "กรุณาคลิกเลือกคอมเมนต์ที่ต้องการลบก่อน")
            return
        cid = str(it.data(Qt.UserRole) or "")
        reply = QMessageBox.question(
            self,
            "ยืนยันการลบคอมเมนต์",
            f"ต้องการลบคอมเมนต์นี้ออกจากโพสต์บน Facebook ถาวรใช่หรือไม่?\n\n{it.text()}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self.lbl_status.setText("กำลังลบคอมเมนต์บน Facebook…")

        def job():
            facebook.delete_comment(self.page_id, cid)
            try:
                return facebook.list_post_comments(self.page_id, self.post_id, limit=50)
            except (RuntimeError, ValueError):
                return None

        def ok(updated):
            if updated is not None:
                self._populate_comments(updated)
            else:
                row_idx = self.list_inbox.row(it)
                if row_idx >= 0:
                    self.list_inbox.takeItem(row_idx)
            self.lbl_status.setText(f"✅ ลบคอมเมนต์ {cid} เรียบร้อยแล้ว")

        self._run_task(
            job,
            ok,
            lambda err: self.lbl_status.setText(f"❌ ลบคอมเมนต์ไม่สำเร็จ: {err}"),
        )

    def _cleanup_worker(self):
        _retire_worker(self._worker, wait_ms=2000)
        self._worker = None

    def done(self, result: int):
        self._cleanup_worker()
        super().done(result)

    def closeEvent(self, event):
        self._cleanup_worker()
        super().closeEvent(event)


class EditQueueJobDialog(QDialog):
    """Dialog for editing a scheduled job in the local queue (time, repeat rule, message, link, photos, video)."""

    def __init__(self, job: dict, parent=None):
        super().__init__(parent)
        self.job = job
        self.setWindowTitle(f"แก้ไขคิวตั้งเวลา #{job['id']} (ข้อความ / รูปภาพ / คลิปวิดีโอ)")
        self.setMinimumSize(580, 540)
        self.attached_images: list[str] = [
            str(p) for p in (job.get("image_paths") or []) if str(p).strip()
        ]
        self.attached_video: str = str(job.get("video_path") or "").strip()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(8)

        top_row = QHBoxLayout()
        top_row.setSpacing(10)
        lbl_time = QLabel("เวลาที่จะโพสต์:")
        lbl_time.setObjectName("sectionTitle")
        self.dt_run_at = QDateTimeEdit()
        self.dt_run_at.setCalendarPopup(True)
        self.dt_run_at.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.dt_run_at.setMinimumDateTime(QDateTime(2000, 1, 1, 0, 0, 0))
        try:
            dt = datetime.fromisoformat(job["run_at"])
            self.dt_run_at.setDateTime(
                QDateTime.fromSecsSinceEpoch(int(dt.timestamp()))
            )
        except (ValueError, OSError, OverflowError):
            self.dt_run_at.setDateTime(QDateTime.currentDateTime().addSecs(30 * 60))

        lbl_rep = QLabel("ทำซ้ำ:")
        lbl_rep.setObjectName("sectionTitle")
        self.combo_repeat = QComboBox()
        self.combo_repeat.addItem("ไม่ทำซ้ำ", "none")
        self.combo_repeat.addItem("ซ้ำรายวัน", "daily")
        self.combo_repeat.addItem("ซ้ำรายสัปดาห์", "weekly")
        cur_rule = job.get("repeat_rule") or "none"
        for i in range(self.combo_repeat.count()):
            if self.combo_repeat.itemData(i) == cur_rule:
                self.combo_repeat.setCurrentIndex(i)
                break

        top_row.addWidget(lbl_time)
        top_row.addWidget(self.dt_run_at, 1)
        top_row.addWidget(lbl_rep)
        top_row.addWidget(self.combo_repeat)
        layout.addLayout(top_row)

        lbl_msg = QLabel("ข้อความโพสต์:")
        lbl_msg.setObjectName("sectionTitle")
        layout.addWidget(lbl_msg)

        self.editor = QPlainTextEdit()
        self.editor.setPlainText(job.get("message") or "")
        self.editor.setMinimumHeight(80)
        layout.addWidget(self.editor, 1)

        lbl_link = QLabel("ลิงก์แนบ (ไม่บังคับ):")
        lbl_link.setObjectName("sectionTitle")
        self.input_link = QLineEdit(job.get("link") or "")
        layout.addWidget(lbl_link)
        layout.addWidget(self.input_link)

        media_hdr = QHBoxLayout()
        lbl_media = QLabel("เพิ่ม/เปลี่ยนรูปภาพ หรือ คลิปวิดีโอในคิวนี้:")
        lbl_media.setObjectName("sectionTitle")
        self.btn_add_video = QPushButton("+ แนบวิดีโอ (MP4/MOV)")
        self.btn_add_video.setObjectName("ghostBtn")
        self.btn_add_video.setCursor(Qt.PointingHandCursor)
        self.btn_add_video.clicked.connect(self._select_video)
        self.btn_clear_images = QPushButton("ล้างรูปทั้งหมด")
        self.btn_clear_images.setObjectName("ghostBtn")
        self.btn_clear_images.setCursor(Qt.PointingHandCursor)
        self.btn_clear_images.setVisible(bool(self.attached_images))
        self.btn_clear_images.clicked.connect(self._clear_images)
        media_hdr.addWidget(lbl_media)
        media_hdr.addStretch(1)
        media_hdr.addWidget(self.btn_add_video)
        media_hdr.addWidget(self.btn_clear_images)
        layout.addLayout(media_hdr)

        self.video_chip = QFrame()
        self.video_chip.setObjectName("scheduleCard")
        vc_layout = QHBoxLayout(self.video_chip)
        vc_layout.setContentsMargins(12, 6, 12, 6)
        self.lbl_attached_video = QLabel("")
        self.lbl_attached_video.setObjectName("dropMainLabel")
        self.lbl_attached_video.setTextFormat(Qt.PlainText)
        self.lbl_attached_video.setMinimumWidth(80)
        self.chk_video_reel = IosToggleSwitch("ส่งเป็น Reels")
        self.chk_video_reel.set_dark_mode(config.get_theme() == "dark")
        self.chk_video_reel.setChecked(bool(job.get("is_reel")))
        self.chk_video_reel.toggled.connect(self._refresh_video_label)
        self.btn_clear_video = QPushButton("ลบวิดีโอ")
        self.btn_clear_video.setObjectName("dangerBtn")
        self.btn_clear_video.setCursor(Qt.PointingHandCursor)
        self.btn_clear_video.clicked.connect(self._clear_video)
        vc_layout.addWidget(self.lbl_attached_video, 1)
        vc_layout.addWidget(self.chk_video_reel)
        vc_layout.addWidget(self.btn_clear_video)
        self.video_chip.setVisible(False)
        layout.addWidget(self.video_chip)

        self.drop_zone = DropImageZone()
        self.drop_zone.browse_clicked.connect(self._browse_images)
        self.drop_zone.files_dropped.connect(self._add_image_paths)
        self.drop_zone.image_removed.connect(self._remove_image_path)
        layout.addWidget(self.drop_zone)

        lbl_wm = QLabel("ลายน้ำบนรูปภาพ (ไม่บังคับ เช่น © {page_name}):")
        lbl_wm.setObjectName("sectionTitle")
        self.input_watermark = QLineEdit(job.get("watermark_text") or "")
        self.input_watermark.setPlaceholderText("ปล่อยว่างหากไม่ต้องการประทับลายน้ำ")
        layout.addWidget(lbl_wm)
        layout.addWidget(self.input_watermark)

        # Check-in row in EditQueueJobDialog
        qctx_row1 = QHBoxLayout()
        qctx_row1.setSpacing(6)
        lbl_qp = QLabel("📍 เช็คอิน:")
        lbl_qp.setObjectName("sectionTitle")
        self.input_place_id = QLineEdit(job.get("place_id") or "")
        self.input_place_id.setPlaceholderText("เลข Place ID หรือลิงก์สถานที่ (ไม่บังคับ)")
        self.input_place_name = QLineEdit(job.get("place_name") or "")
        self.input_place_name.setPlaceholderText("ชื่อสถานที่")
        self.input_place_name.setMaximumWidth(145)
        qctx_row1.addWidget(lbl_qp)
        qctx_row1.addWidget(self.input_place_id, 1)
        qctx_row1.addWidget(self.input_place_name)
        layout.addLayout(qctx_row1)

        lbl_qmedia_note = QLabel(
            "หมายเหตุ: Facebook API รองรับเฉพาะรูปภาพ (หลายรูป) หรือวิดีโอ (1 คลิป) อย่างใดอย่างหนึ่งต่อ 1 โพสต์"
        )
        lbl_qmedia_note.setObjectName("mutedLabel")
        lbl_qmedia_note.setWordWrap(True)
        layout.addWidget(lbl_qmedia_note)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("บันทึกการแก้ไขคิว")
        buttons.button(QDialogButtonBox.Save).setObjectName("primaryBtn")
        buttons.button(QDialogButtonBox.Cancel).setText("ยกเลิก")
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if self.attached_video:
            self._set_video(self.attached_video)
        elif self.attached_images:
            self.drop_zone.set_images(self.attached_images)

    def _browse_images(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "เลือกรูปภาพสำหรับคิวตั้งเวลา", "", IMAGE_FILTER
        )
        if paths:
            self._add_image_paths(paths)

    def _add_image_paths(self, paths: list[str]):
        if paths and self.attached_video:
            self._set_video("")
        existing_norm = {
            os.path.normcase(os.path.normpath(x)) for x in self.attached_images
        }
        added = 0
        for p in paths:
            norm = os.path.normcase(os.path.normpath(p))
            if norm not in existing_norm:
                self.attached_images.append(p)
                existing_norm.add(norm)
                added += 1
        if added:
            self.drop_zone.set_images(self.attached_images)
            self.btn_clear_images.setVisible(bool(self.attached_images))

    def _remove_image_path(self, path: str):
        if path in self.attached_images:
            self.attached_images.remove(path)
            self.drop_zone.set_images(self.attached_images)
            self.btn_clear_images.setVisible(bool(self.attached_images))

    def _clear_images(self):
        self.attached_images.clear()
        self.drop_zone.set_images([])
        self.btn_clear_images.setVisible(False)

    def _select_video(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "เลือกไฟล์วิดีโอสำหรับคิวตั้งเวลา", "", VIDEO_FILTER
        )
        if path:
            self._set_video(path)

    def _refresh_video_label(self, *_args):
        if not self.attached_video:
            self.lbl_attached_video.setText("")
            return
        vp = Path(self.attached_video)
        size_str = ""
        if vp.is_file():
            size_mb = vp.stat().st_size / (1024 * 1024)
            size_str = f" ({size_mb:.1f} MB)"
        prefix = "🎬 Reels:" if self.chk_video_reel.isChecked() else "🎬 วิดีโอ:"
        self.lbl_attached_video.setText(f"{prefix} {vp.name}{size_str}")

    def _set_video(self, path: str):
        self.attached_video = path
        if path and self.attached_images:
            self.attached_images.clear()
            self.drop_zone.set_images([])
            self.btn_clear_images.setVisible(False)
        if path:
            self._refresh_video_label()
            self.video_chip.setVisible(True)
            self.drop_zone.setVisible(False)
        else:
            self.lbl_attached_video.setText("")
            self.video_chip.setVisible(False)
            self.drop_zone.setVisible(True)

    def _clear_video(self):
        self.chk_video_reel.setChecked(False)
        self._set_video("")

    def _validate_and_accept(self):
        msg = self.editor.toPlainText().strip()
        link = self.input_link.text().strip()
        raw_place = self.input_place_id.text().strip()
        try:
            place_id = facebook.extract_place_or_page_id(raw_place) if raw_place else ""
        except ValueError as exc:
            QMessageBox.warning(self, "ข้อมูลเช็คอินไม่ถูกต้อง", str(exc))
            return
        if self.attached_video and self.chk_video_reel.isChecked() and place_id:
            QMessageBox.warning(
                self,
                "Reels ไม่รองรับเช็คอิน",
                "Facebook Reels API ไม่รองรับการแนบสถานที่เช็คอิน กรุณาล้างช่องเช็คอินหรือปิดสวิตช์ 'ส่งเป็น Reels'",
            )
            return
        has_media = bool(self.attached_images or self.attached_video or place_id)
        if not msg and not link and not has_media:
            QMessageBox.information(self, "ข้อมูลไม่ครบ", "กรุณาระบุข้อความ ลิงก์ รูปภาพ วิดีโอ หรือเช็คอินอย่างน้อย 1 อย่าง")
            return
        if len(msg) > FB_MAX_CHARS:
            QMessageBox.warning(self, "ข้อความยาวเกินไป", f"ข้อความต้องไม่เกิน {FB_MAX_CHARS:,} ตัวอักษร")
            return
        if link and not _is_valid_http_url(link):
            QMessageBox.warning(self, "ลิงก์ไม่ถูกต้อง", "ลิงก์ต้องขึ้นต้นด้วย http:// หรือ https://")
            return
        missing_imgs = [p for p in self.attached_images if not Path(p).is_file()]
        if missing_imgs:
            QMessageBox.warning(
                self,
                "ไม่พบไฟล์รูปภาพ",
                "ไม่พบไฟล์รูปภาพต่อไปนี้ในเครื่อง:\n" + "\n".join(missing_imgs),
            )
            return
        if self.attached_video and not Path(self.attached_video).is_file():
            QMessageBox.warning(
                self, "ไม่พบไฟล์วิดีโอ", f"ไม่พบไฟล์วิดีโอในเครื่อง: {self.attached_video}"
            )
            return
        if (
            self.attached_video
            and self.chk_video_reel.isChecked()
            and Path(self.attached_video).stat().st_size <= 0
        ):
            QMessageBox.warning(
                self,
                "ไฟล์วิดีโอว่างเปล่า",
                "ไฟล์วิดีโอมีขนาด 0 bytes ไม่สามารถอัปโหลดเป็น Facebook Reels ได้",
            )
            return
        run_dt = self.dt_run_at.dateTime().toPython().astimezone()
        if run_dt <= datetime.now().astimezone():
            reply = QMessageBox.question(
                self,
                "เวลาที่ตั้งอยู่ในอดีต",
                "เวลาที่เลือกอยู่ในอดีต หากบันทึก คิวนี้จะถูกส่งโพสต์ทันทีภายในไม่กี่วินาที\nต้องการบันทึกใช่หรือไม่?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
        self.accept()

    def updated_values(self) -> dict:
        run_dt = self.dt_run_at.dateTime().toPython().astimezone()
        raw_place = self.input_place_id.text().strip()
        place_id = facebook.extract_place_or_page_id(raw_place) if raw_place else ""
        return {
            "run_at_iso": run_dt.isoformat(timespec="seconds"),
            "message": self.editor.toPlainText().strip(),
            "link": self.input_link.text().strip(),
            "repeat_rule": str(self.combo_repeat.currentData() or "none"),
            "watermark_text": self.input_watermark.text().strip(),
            "image_paths": list(self.attached_images),
            "video_path": self.attached_video.strip(),
            "is_reel": bool(self.attached_video.strip() and self.chk_video_reel.isChecked()),
            "place_id": place_id,
            "place_name": (self.input_place_name.text().strip() or place_id) if place_id else "",
            "og_action_type_id": "",
            "og_object_id": "",
            "og_label": "",
        }


def import_scheduled_jobs_from_csv(csv_path: str, targets: list[dict]) -> int:
    """Import scheduled jobs from a CSV file into the local scheduled queue.

    Supported CSV columns (with header or positional):
      run_at, message, link (optional), image_paths (optional, ;-separated), video_path (optional)
    """
    if not targets:
        raise ValueError("กรุณาเลือกเพจเป้าหมายอย่างน้อย 1 เพจก่อนนำเข้า CSV")
    p = Path(csv_path)
    if not p.is_file():
        raise ValueError(f"ไม่พบไฟล์ CSV: {csv_path}")

    try:
        text = p.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = p.read_text(encoding="cp874")
    if not text.strip():
        return 0

    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        return 0

    first_lower = [c.strip().lower() for c in rows[0]]
    has_header = any(k in first_lower for k in ("run_at", "message", "เวลา", "ข้อความ"))
    col_map: dict[str, int] = {}
    if has_header:
        for idx, col in enumerate(first_lower):
            if col in ("run_at", "time", "datetime", "เวลา"):
                col_map["run_at"] = idx
            elif col in ("message", "text", "caption", "ข้อความ"):
                col_map["message"] = idx
            elif col in ("link", "url", "ลิงก์"):
                col_map["link"] = idx
            elif col in ("image_paths", "images", "รูปภาพ"):
                col_map["image_paths"] = idx
            elif col in ("video_path", "video", "วิดีโอ"):
                col_map["video_path"] = idx
            elif col in ("is_reel", "reel", "reels", "โหมด reels"):
                col_map["is_reel"] = idx
            elif col in ("repeat_rule", "repeat", "ทำซ้ำ"):
                col_map["repeat_rule"] = idx
            elif col in ("watermark_text", "watermark", "ลายน้ำ"):
                col_map["watermark_text"] = idx
            elif col in ("place_id", "place", "เช็คอิน"):
                col_map["place_id"] = idx
            elif col in ("place_name", "ชื่อสถานที่"):
                col_map["place_name"] = idx
            elif col in ("og_action_type_id", "activity_action", "กิจกรรม"):
                col_map["og_action_type_id"] = idx
            elif col in ("og_object_id", "activity_object", "เป้าหมายกิจกรรม"):
                col_map["og_object_id"] = idx
            elif col in ("og_label", "activity_label", "ชื่อกิจกรรม"):
                col_map["og_label"] = idx
        data_rows = rows[1:]
    else:
        col_map = {
            "run_at": 0,
            "message": 1,
            "link": 2,
            "image_paths": 3,
            "video_path": 4,
            "repeat_rule": 5,
            "watermark_text": 6,
            "place_id": 7,
            "place_name": 8,
            "og_action_type_id": 9,
            "og_object_id": 10,
            "og_label": 11,
        }
        data_rows = rows

    def get_col(row_vals: list[str], key: str) -> str:
        idx = col_map.get(key, -1)
        if 0 <= idx < len(row_vals):
            return row_vals[idx].strip()
        return ""

    def _parse_csv_datetime(raw_time: str) -> datetime:
        cleaned = " ".join(raw_time.strip().split())
        m_ymd = re.match(r"^(\d{4})([-/.])(\d{1,2})\2(\d{1,2})(.*)$", cleaned, flags=re.ASCII)
        if m_ymd and 2400 <= int(m_ymd.group(1)) <= 2799:
            cleaned = (
                f"{int(m_ymd.group(1)) - 543:04d}{m_ymd.group(2)}"
                f"{m_ymd.group(3)}{m_ymd.group(2)}{m_ymd.group(4)}{m_ymd.group(5)}"
            )
        else:
            m_dmy = re.match(r"^(\d{1,2})([/-])(\d{1,2})\2(\d{4})(.*)$", cleaned, flags=re.ASCII)
            if m_dmy and 2400 <= int(m_dmy.group(4)) <= 2799:
                cleaned = (
                    f"{m_dmy.group(1)}{m_dmy.group(2)}{m_dmy.group(3)}"
                    f"{m_dmy.group(2)}{int(m_dmy.group(4)) - 543:04d}{m_dmy.group(5)}"
                )
        try:
            dt = datetime.fromisoformat(cleaned.replace(" ", "T"))
        except ValueError:
            dt = None
            for fmt in (
                "%Y-%m-%d %H:%M",
                "%Y-%m-%d %H:%M:%S",
                "%Y/%m/%d %H:%M",
                "%Y/%m/%d %H:%M:%S",
                "%d/%m/%Y %H:%M",
                "%d/%m/%Y %H:%M:%S",
            ):
                try:
                    dt = datetime.strptime(cleaned, fmt)
                    break
                except ValueError:
                    continue
            if dt is None:
                raise ValueError(
                    f"รูปแบบวันเวลาไม่ถูกต้อง '{raw_time}' (ต้องเป็น YYYY-MM-DD HH:MM หรือ DD/MM/YYYY HH:MM)"
                )
        if dt.tzinfo is None:
            try:
                dt = dt.astimezone()
            except (OSError, OverflowError) as exc:
                raise ValueError(
                    f"ปีของวันเวลาไม่ถูกต้อง '{raw_time}' (กรุณาใช้ปี ค.ศ. ตั้งแต่ 1970 เป็นต้นไป)"
                ) from exc
        return dt

    validated_jobs: list[dict] = []
    for r in data_rows:
        if not any(c.strip() for c in r):
            continue
        raw_time = get_col(r, "run_at")
        msg = get_col(r, "message")
        link = get_col(r, "link")
        raw_imgs = get_col(r, "image_paths")
        video = get_col(r, "video_path")
        raw_is_reel = get_col(r, "is_reel").strip().lower()
        is_reel = raw_is_reel in ("1", "true", "yes", "y", "reel", "reels", "ใช่")
        raw_repeat = get_col(r, "repeat_rule").lower()
        repeat_map = {"รายวัน": "daily", "รายสัปดาห์": "weekly", "daily": "daily", "weekly": "weekly"}
        repeat_rule = repeat_map.get(raw_repeat, "none")
        wm_text = get_col(r, "watermark_text")
        raw_place = get_col(r, "place_id")
        place_id = facebook.extract_place_or_page_id(raw_place) if raw_place else ""
        place_name = (get_col(r, "place_name") or place_id) if place_id else ""
        # Legacy og_* columns in old CSVs are safely ignored (deprecated by Meta Graph API)
        imgs = [x.strip() for x in raw_imgs.split(";") if x.strip()] if raw_imgs else []

        if not raw_time or (not msg and not link and not imgs and not video and not place_id):
            continue
        if len(msg) > FB_MAX_CHARS:
            raise ValueError(f"ข้อความใน CSV ยาวเกิน {FB_MAX_CHARS:,} ตัวอักษร")
        if link and not _is_valid_http_url(link):
            raise ValueError(f"ลิงก์ใน CSV ไม่ถูกต้อง: {link}")
        if imgs and video:
            raise ValueError("Facebook API ไม่รองรับการแนบทั้งรูปภาพและวิดีโอในโพสต์เดียวกันใน CSV")
        if is_reel and not video:
            raise ValueError("รายการใน CSV ที่ตั้งเป็น Reels ต้องระบุไฟล์วิดีโอ (video_path)")
        if is_reel and place_id:
            raise ValueError("Facebook Reels ใน CSV ไม่รองรับการแนบสถานที่เช็คอิน (place_id)")
        dt = _parse_csv_datetime(raw_time)
        validated_jobs.append(
            {
                "run_at_iso": dt.isoformat(timespec="seconds"),
                "message": msg,
                "link": link,
                "image_paths": imgs,
                "video_path": video,
                "is_reel": is_reel,
                "repeat_rule": repeat_rule,
                "watermark_text": wm_text,
                "place_id": place_id,
                "place_name": place_name,
            }
        )

    for job_spec in validated_jobs:
        db.add_scheduled_job(
            run_at_iso=job_spec["run_at_iso"],
            message=job_spec["message"],
            link=job_spec["link"],
            image_paths=job_spec["image_paths"],
            video_path=job_spec["video_path"],
            is_reel=job_spec["is_reel"],
            targets=targets,
            repeat_rule=job_spec["repeat_rule"],
            watermark_text=job_spec["watermark_text"],
            place_id=job_spec["place_id"],
            place_name=job_spec["place_name"],
        )
    return len(validated_jobs)


def _is_valid_http_url(url: str) -> bool:
    if not url:
        return True
    if any(c in url for c in (" ", "\t", "\r", "\n")):
        return False
    try:
        parsed = urlparse(url)
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme in ("http", "https")
        and bool(parsed.hostname)
        and not parsed.username
        and not parsed.password
    )


def _format_activity_preview_label(
    action_id: str, object_id: str, label: str = ""
) -> str:
    clean_action = (action_id or "").strip()
    if not clean_action:
        return ""
    verb = facebook.activity_action_short_label(clean_action)
    clean_label = (label or "").strip()
    if not clean_label and clean_action == facebook.FEELING_ACTION_ID:
        for fid, fname in facebook.FEELING_PRESETS:
            if fid == (object_id or "").strip():
                clean_label = fname
                break
    target_disp = clean_label or (object_id or "").strip()
    return f"{verb} {target_disp}".strip()


class PlaceActivityPickerDialog(QDialog):
    """Search Facebook Places/Pages or pick/save favorite Check-in & Activity targets."""

    def __init__(
        self,
        *,
        kind: str = "place",
        page_id: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.kind = kind if kind in ("place", "activity_target") else "place"
        self.page_id = (page_id or "").strip()
        self._worker: Worker | None = None
        self.selected_id: str = ""
        self.selected_name: str = ""

        is_place = self.kind == "place"
        title = "ค้นหา / เลือกสถานที่เช็คอิน (Check-in)" if is_place else "ค้นหา / เลือกเพจเป้าหมายกิจกรรม (Activity)"
        self.setWindowTitle(title)
        self.setMinimumSize(620, 520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        lbl_search = QLabel(
            "ค้นหาจาก Facebook (พิมพ์ชื่อสถานที่ หรือวางลิงก์/เลข Place ID):"
            if is_place
            else "ค้นหาเพจเป้าหมายกิจกรรมจาก Facebook (เช่น XBOX, ชื่อเกม, ชื่อหนัง หรือวางลิงก์/เลข Page ID):"
        )
        lbl_search.setObjectName("sectionTitle")
        layout.addWidget(lbl_search)

        search_row = QHBoxLayout()
        search_row.setSpacing(6)
        self.input_query = QLineEdit()
        self.input_query.setPlaceholderText(
            "เช่น สยามพารากอน, กรุงเทพมหานคร หรือวางลิงก์เพจสถานที่…"
            if is_place
            else "เช่น XBOX, RoV, หรือวางลิงก์เพจเป้าหมาย…"
        )
        self.input_query.returnPressed.connect(self.run_search)
        self.btn_search = QPushButton("ค้นหาผ่าน Facebook")
        self.btn_search.setObjectName("primaryBtn")
        self.btn_search.setCursor(Qt.PointingHandCursor)
        self.btn_search.clicked.connect(self.run_search)
        search_row.addWidget(self.input_query, 1)
        search_row.addWidget(self.btn_search)
        layout.addLayout(search_row)

        self.lbl_status = QLabel("พร้อมค้นหา หรือเลือกรายการที่บันทึกไว้ด้านล่าง")
        self.lbl_status.setObjectName("mutedLabel")
        self.lbl_status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.lbl_status)

        self.list_results = QListWidget()
        self.list_results.setMaximumHeight(140)
        self.list_results.itemDoubleClicked.connect(self._accept_from_item)
        layout.addWidget(self.list_results)

        res_btn_row = QHBoxLayout()
        self.btn_use_result = QPushButton("ใช้รายการที่เลือกจากผลการค้นหา")
        self.btn_use_result.setObjectName("ghostBtn")
        self.btn_use_result.setCursor(Qt.PointingHandCursor)
        self.btn_use_result.clicked.connect(self._use_selected_result)
        self.btn_save_result = QPushButton("บันทึกเก็บไว้ใช้ซ้ำ")
        self.btn_save_result.setObjectName("ghostBtn")
        self.btn_save_result.setCursor(Qt.PointingHandCursor)
        self.btn_save_result.clicked.connect(self._save_selected_result)
        res_btn_row.addWidget(self.btn_use_result)
        res_btn_row.addWidget(self.btn_save_result)
        res_btn_row.addStretch(1)
        layout.addLayout(res_btn_row)

        # Saved Favorites + Manual Quick Add
        lbl_saved = QLabel(
            "รายการสถานที่เช็คอินที่บันทึกไว้ใช้ซ้ำ:"
            if is_place
            else "รายการเพจกิจกรรมที่บันทึกไว้ใช้ซ้ำ:"
        )
        lbl_saved.setObjectName("sectionTitle")
        layout.addWidget(lbl_saved)

        manual_row = QHBoxLayout()
        manual_row.setSpacing(6)
        self.input_manual_name = QLineEdit()
        self.input_manual_name.setPlaceholderText("ตั้งชื่อเรียก (เช่น สยามพารากอน)" if is_place else "ตั้งชื่อเรียก (เช่น XBOX)")
        self.input_manual_id = QLineEdit()
        self.input_manual_id.setPlaceholderText("เลข Place/Page ID หรือวางลิงก์ Facebook")
        self.btn_add_saved = QPushButton("+ บันทึกเก็บไว้")
        self.btn_add_saved.setObjectName("ghostBtn")
        self.btn_add_saved.setCursor(Qt.PointingHandCursor)
        self.btn_add_saved.clicked.connect(self._save_manual_preset)
        manual_row.addWidget(self.input_manual_name, 1)
        manual_row.addWidget(self.input_manual_id, 1)
        manual_row.addWidget(self.btn_add_saved)
        layout.addLayout(manual_row)

        self.list_saved = QListWidget()
        self.list_saved.itemDoubleClicked.connect(self._accept_from_item)
        layout.addWidget(self.list_saved, 1)

        bottom = QHBoxLayout()
        self.btn_use_saved = QPushButton("เลือกใช้งานรายการที่บันทึกไว้")
        self.btn_use_saved.setObjectName("primaryBtn")
        self.btn_use_saved.setCursor(Qt.PointingHandCursor)
        self.btn_use_saved.clicked.connect(self._use_selected_saved)
        self.btn_del_saved = QPushButton("ลบที่เลือก")
        self.btn_del_saved.setObjectName("dangerBtn")
        self.btn_del_saved.setCursor(Qt.PointingHandCursor)
        self.btn_del_saved.clicked.connect(self._delete_selected_saved)
        btn_close = QPushButton("ปิด")
        btn_close.setObjectName("ghostBtn")
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.clicked.connect(self.reject)
        bottom.addWidget(self.btn_use_saved, 1)
        bottom.addWidget(self.btn_del_saved)
        bottom.addWidget(btn_close)
        layout.addLayout(bottom)

        self.refresh_saved()

    def refresh_saved(self):
        self.list_saved.clear()
        for p in db.list_place_presets(self.kind):
            icon = "📍" if self.kind == "place" else "🎯"
            it = QListWidgetItem(f"{icon} {p['name']}  (ID: {p['fb_id']})")
            it.setData(Qt.UserRole, p["fb_id"])
            it.setData(Qt.UserRole + 1, p["name"])
            it.setData(Qt.UserRole + 2, p["id"])
            self.list_saved.addItem(it)

    def run_search(self):
        q = self.input_query.text().strip()
        if not q:
            QMessageBox.information(self, "ยังไม่ได้พิมพ์คำค้นหา", "กรุณาพิมพ์ชื่อสถานที่/เพจ หรือวางลิงก์ก่อน")
            return
        if not self.page_id:
            QMessageBox.information(
                self,
                "ยังไม่ได้เชื่อมต่อเพจ",
                "กรุณาเชื่อมต่อเพจ Facebook อย่างน้อย 1 เพจก่อนค้นหาผ่าน API\n(หรือกรอกเลข ID ในช่อง 'บันทึกเก็บไว้' ด้านล่างได้ทันที)",
            )
            return
        if self._worker and self._worker.isRunning():
            return
        self.btn_search.setEnabled(False)
        self.lbl_status.setText("กำลังค้นหาจาก Facebook Graph API…")

        def ok(items: list[dict]):
            self.list_results.clear()
            for r in items:
                extra = " • ".join(x for x in (r.get("category"), r.get("location")) if x)
                suffix = f" ({extra})" if extra else ""
                it = QListWidgetItem(f"{r['name']}{suffix}  [ID: {r['id']}]")
                it.setData(Qt.UserRole, r["id"])
                it.setData(Qt.UserRole + 1, r["name"])
                self.list_results.addItem(it)
            if items:
                self.lbl_status.setText(f"พบ {len(items)} รายการ (ดับเบิลคลิกเพื่อเลือกใช้งาน)")
            else:
                self.lbl_status.setText("ไม่พบผลลัพธ์ — ลองวางเลข Place/Page ID โดยตรงในช่องด้านล่าง")

        def fail(err: str):
            self.lbl_status.setText(
                f"ค้นหาผ่าน API ไม่ได้: {err} (สามารถวางเลข Place/Page ID หรือลิงก์ตรงในช่องด้านล่างได้)"
            )

        self._worker = Worker(lambda: facebook.search_places(self.page_id, q))
        self._worker.done.connect(ok)
        self._worker.failed.connect(fail)
        self._worker.finished.connect(lambda: self.btn_search.setEnabled(True))
        self._worker.start()

    def _accept_from_item(self, it: QListWidgetItem | None):
        if it is None:
            return
        self.selected_id = str(it.data(Qt.UserRole) or "").strip()
        self.selected_name = str(it.data(Qt.UserRole + 1) or "").strip()
        if self.selected_id:
            self.accept()

    def _use_selected_result(self):
        it = self.list_results.currentItem()
        if not it:
            QMessageBox.information(self, "ยังไม่ได้เลือกรายการ", "กรุณาคลิกเลือกรายการจากผลการค้นหาก่อน")
            return
        self._accept_from_item(it)

    def _save_selected_result(self):
        it = self.list_results.currentItem()
        if not it:
            QMessageBox.information(self, "ยังไม่ได้เลือกรายการ", "กรุณาคลิกเลือกรายการจากผลการค้นหาก่อน")
            return
        fb_id = str(it.data(Qt.UserRole) or "").strip()
        name = str(it.data(Qt.UserRole + 1) or "").strip()
        if fb_id:
            db.save_place_preset(self.kind, name or fb_id, fb_id)
            self.refresh_saved()
            self.lbl_status.setText(f"บันทึก '{name}' เก็บไว้ใช้ซ้ำเรียบร้อยแล้ว")

    def _save_manual_preset(self):
        raw_id = self.input_manual_id.text().strip()
        name = self.input_manual_name.text().strip()
        try:
            clean_id = facebook.extract_place_or_page_id(raw_id)
        except ValueError as exc:
            QMessageBox.warning(self, "รหัสไม่ถูกต้อง", str(exc))
            return
        if not clean_id:
            QMessageBox.information(self, "ข้อมูลไม่ครบ", "กรุณากรอกเลข Place/Page ID หรือวางลิงก์ Facebook ก่อน")
            return
        db.save_place_preset(self.kind, name or clean_id, clean_id)
        self.input_manual_name.clear()
        self.input_manual_id.clear()
        self.refresh_saved()
        self.lbl_status.setText(f"บันทึก '{name or clean_id}' เก็บไว้ใช้ซ้ำแล้ว")

    def _use_selected_saved(self):
        it = self.list_saved.currentItem()
        if not it:
            QMessageBox.information(self, "ยังไม่ได้เลือกรายการ", "กรุณาคลิกเลือกรายการที่บันทึกไว้ก่อน")
            return
        self._accept_from_item(it)

    def _delete_selected_saved(self):
        it = self.list_saved.currentItem()
        if not it:
            return
        pid = it.data(Qt.UserRole + 2)
        if pid is not None:
            db.delete_place_preset(int(pid))
            self.refresh_saved()

    def _cleanup_worker(self):
        _retire_worker(self._worker, wait_ms=2000)
        self._worker = None

    def done(self, result: int):
        self._cleanup_worker()
        super().done(result)

    def closeEvent(self, event):
        self._cleanup_worker()
        super().closeEvent(event)


def _post_for_platform(
    platform: str,
    page_id: str,
    message: str,
    *,
    link: str = "",
    image_paths: list[str] | None = None,
    video_path: str = "",
    is_reel: bool = False,
    scheduled_time: datetime | None = None,
    place_id: str = "",
    og_action_type_id: str = "",
    og_object_id: str = "",
    og_phrase: str = "",
) -> str:
    if platform == facebook.PLATFORM:
        return facebook.post_to_page(
            page_id,
            message,
            link=link,
            image_paths=image_paths,
            video_path=video_path,
            is_reel=is_reel,
            scheduled_time=scheduled_time,
            place_id=place_id,
            og_action_type_id=og_action_type_id,
            og_object_id=og_object_id,
            og_phrase=og_phrase,
        )
    raise RuntimeError(f"ยังไม่รองรับแพลตฟอร์ม: {platform}")


def _forget_for_platform(platform: str, page_id: str) -> None:
    if platform == facebook.PLATFORM:
        facebook.forget(page_id)


class _NumericTableWidgetItem(QTableWidgetItem):
    """QTableWidgetItem that sorts numerically by its UserRole sort key."""

    def __init__(self, display_text: str, sort_value: float | int):
        super().__init__(display_text)
        self.setData(Qt.UserRole, float(sort_value))

    def __lt__(self, other: QTableWidgetItem) -> bool:
        v1 = self.data(Qt.UserRole)
        v2 = other.data(Qt.UserRole) if other is not None else None
        if isinstance(v1, (int, float)) and isinstance(v2, (int, float)):
            return v1 < v2
        return super().__lt__(other)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.setWindowIcon(ui_icons.svg_icon("logo", "#FFFFFF", 32))
        self.resize(1260, 780)
        self.setMinimumSize(1020, 660)

        self.worker: Worker | None = None
        self.analytics_worker: Worker | None = None
        self.analytics_cancel_event: threading.Event | None = None
        self.analytics_rows: list[dict] = []
        self.token_check_worker: Worker | None = None
        self.token_check_cancel_event: threading.Event | None = None
        self._token_health_cache: dict[tuple[str, str], tuple[bool, str]] = {}
        self.auto_reply_worker: Worker | None = None
        self.auto_reply_cancel_event: threading.Event | None = None
        self._startup_backup_warning: str = ""
        self._syncing_stagger: bool = False
        self.oauth_cancel_event: threading.Event | None = None
        self.task_cancel_event: threading.Event | None = None
        self.attached_images: list[str] = []
        self.attached_video: str = ""
        self.attached_comment_image: str = ""
        self.follow_up_comments: list[str] = []
        self.queued_comments: list[str] = []
        self._loaded_posts_page_id: str | None = None
        self._force_quit: bool = False
        self._restoring_draft: bool = False
        self.current_theme = config.get_theme()
        self.history_filter = "all"

        self._draft_timer = QTimer(self)
        self._draft_timer.setSingleShot(True)
        self._draft_timer.setInterval(500)
        self._draft_timer.timeout.connect(self._save_draft)

        self._build_ui()
        self._init_system_tray()
        self._apply_theme(self.current_theme)
        self._switch_page(0)

        self._run_startup_auto_backup()
        self.refresh_accounts()
        self.refresh_page_presets()
        self.refresh_templates()
        self.refresh_scheduled_queue()
        self.refresh_history()
        self.refresh_saved_groups()
        self._load_cached_analytics()
        self._restore_draft()
        self._update_preview()

        # Check local scheduled queue every 20 seconds
        self.queue_timer = QTimer(self)
        self.queue_timer.setInterval(20_000)
        self.queue_timer.timeout.connect(self._process_due_queue_jobs)
        self.queue_timer.start()

        # Auto-refresh timer for RT Dashboard & Page Analytics (off until user toggles on)
        self.analytics_timer = QTimer(self)
        self.analytics_timer.setInterval(5 * 60 * 1000)
        self.analytics_timer.timeout.connect(self._on_analytics_auto_refresh_tick)

        # Background Keyword Auto-Reply timer
        self.auto_reply_timer = QTimer(self)
        self.auto_reply_timer.timeout.connect(self._on_auto_reply_timer_tick)
        self._sync_auto_reply_timer()
        if self._startup_backup_warning:
            self.status.setText(self._startup_backup_warning)

        # Silent non-blocking token health check on startup (skip in headless/offscreen test mode)
        if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
            QTimer.singleShot(1500, self._run_startup_token_check)

    # --- UI Construction ---

    def _build_ui(self):
        root = QWidget()
        shell = QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        shell.addWidget(self._build_sidebar())

        # Main workspace container
        main_col = QVBoxLayout()
        main_col.setContentsMargins(20, 16, 20, 14)
        main_col.setSpacing(14)

        # Top KPI strip
        main_col.addLayout(self._build_kpi_strip())

        # Stacked views
        self.pages_stack = QStackedWidget()
        self.tabs = self.pages_stack  # compatibility alias
        self.pages_stack.addWidget(self._build_composer_view())
        self.pages_stack.addWidget(self._build_queue_view())
        self.pages_stack.addWidget(self._build_history_view())
        self.pages_stack.addWidget(self._build_comments_view())
        self.pages_stack.addWidget(self._build_groups_share_view())
        self.pages_stack.addWidget(self._build_analytics_view())
        main_col.addWidget(self.pages_stack, 1)

        # Status bar
        self.status = QLabel("พร้อมใช้งาน")
        self.status.setObjectName("statusBarLabel")
        self.status.setTextFormat(Qt.PlainText)
        main_col.addWidget(self.status)

        main_wrap = QWidget()
        main_wrap.setLayout(main_col)
        shell.addWidget(main_wrap, 1)

        self.setCentralWidget(root)

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(288)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(7)

        # Brand Header
        brand_row = QHBoxLayout()
        brand_row.setSpacing(10)
        logo_lbl = QLabel()
        logo_lbl.setPixmap(ui_icons.svg_pixmap("logo", "#FFFFFF", 34))
        logo_lbl.setFixedSize(36, 36)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        lbl_title = QLabel(APP_NAME)
        lbl_title.setObjectName("brandTitle")
        lbl_sub = QLabel("Facebook Page Studio")
        lbl_sub.setObjectName("brandSub")
        brand_text.addWidget(lbl_title)
        brand_text.addWidget(lbl_sub)
        brand_row.addWidget(logo_lbl)
        brand_row.addLayout(brand_text, 1)
        layout.addLayout(brand_row)

        # Navigation Rail
        nav_label = QLabel("เมนูหลัก")
        nav_label.setObjectName("sectionTitle")
        layout.addWidget(nav_label)

        self.btn_nav_compose = QPushButton("  เขียนโพสต์")
        self.btn_nav_queue = QPushButton("  คิวตั้งเวลา")
        self.btn_nav_history = QPushButton("  ประวัติการโพสต์")
        self.btn_nav_comments = QPushButton("  คอมเมนต์ใต้โพสต์")
        self.btn_nav_share = QPushButton("  แชร์โพสต์")
        self.btn_nav_analytics = QPushButton("  RT Dashboard / สถิติ")
        self._nav_buttons = [
            self.btn_nav_compose,
            self.btn_nav_queue,
            self.btn_nav_history,
            self.btn_nav_comments,
            self.btn_nav_share,
            self.btn_nav_analytics,
        ]
        for idx, btn in enumerate(self._nav_buttons):
            btn.setObjectName("navBtn")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setIconSize(QSize(17, 17))
            btn.clicked.connect(lambda _=False, i=idx: self._switch_page(i))
            layout.addWidget(btn)

        # Divider
        div1 = QFrame()
        div1.setObjectName("hairline")
        div1.setFrameShape(QFrame.HLine)
        layout.addWidget(div1)

        # Pages Header
        pages_hdr = QHBoxLayout()
        lbl_pages = QLabel("เพจที่เชื่อมต่อ")
        lbl_pages.setObjectName("sectionTitle")
        self.lbl_selected_count = QLabel("เลือก 0 / 0")
        self.lbl_selected_count.setObjectName("mutedLabel")
        pages_hdr.addWidget(lbl_pages)
        pages_hdr.addStretch(1)
        pages_hdr.addWidget(self.lbl_selected_count)
        layout.addLayout(pages_hdr)

        # Page Search Filter
        self.input_page_search = QLineEdit()
        self.input_page_search.setObjectName("searchInput")
        self.input_page_search.setPlaceholderText("ค้นหาชื่อเพจ…")
        self.input_page_search.textChanged.connect(self._filter_accounts_list)
        layout.addWidget(self.input_page_search)

        # Accounts List
        self.accounts = QListWidget()
        self.accounts.setIconSize(QSize(28, 28))
        self.accounts.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.accounts.setTextElideMode(Qt.ElideRight)
        self.accounts.itemChanged.connect(self._on_accounts_changed)
        layout.addWidget(self.accounts, 1)

        # Select All / None
        sel_row = QHBoxLayout()
        sel_row.setSpacing(6)
        self.btn_select_all = QPushButton("เลือกทั้งหมด")
        self.btn_select_all.setObjectName("ghostBtn")
        self.btn_select_all.setCursor(Qt.PointingHandCursor)
        self.btn_select_none = QPushButton("ไม่เลือก")
        self.btn_select_none.setObjectName("ghostBtn")
        self.btn_select_none.setCursor(Qt.PointingHandCursor)
        self.btn_select_all.clicked.connect(lambda: self._set_all_accounts(True))
        self.btn_select_none.clicked.connect(lambda: self._set_all_accounts(False))
        sel_row.addWidget(self.btn_select_all, 1)
        sel_row.addWidget(self.btn_select_none, 1)
        layout.addLayout(sel_row)

        # Page Presets (Saved Page Sets)
        preset_row = QHBoxLayout()
        preset_row.setSpacing(4)
        self.combo_page_presets = QComboBox()
        self.combo_page_presets.currentIndexChanged.connect(self.apply_selected_page_preset)
        self.combo_page_presets.activated.connect(self.apply_selected_page_preset)
        self.btn_save_page_preset = QPushButton("+ ชุดเพจ")
        self.btn_save_page_preset.setObjectName("ghostBtn")
        self.btn_save_page_preset.setStyleSheet("padding: 7px 10px;")
        self.btn_save_page_preset.setCursor(Qt.PointingHandCursor)
        self.btn_save_page_preset.setToolTip("บันทึกเพจที่ติ๊กเลือกไว้เป็นชุดเพจ")
        self.btn_save_page_preset.clicked.connect(self.save_current_page_preset)
        self.btn_delete_page_preset = QPushButton("×")
        self.btn_delete_page_preset.setObjectName("ghostBtn")
        self.btn_delete_page_preset.setFixedWidth(32)
        self.btn_delete_page_preset.setStyleSheet("padding: 6px 0px; font-weight: 700; font-size: 14px;")
        self.btn_delete_page_preset.setCursor(Qt.PointingHandCursor)
        self.btn_delete_page_preset.setToolTip("ลบชุดเพจที่เลือก")
        self.btn_delete_page_preset.clicked.connect(self.delete_current_page_preset)
        preset_row.addWidget(self.combo_page_presets, 1)
        preset_row.addWidget(self.btn_save_page_preset)
        preset_row.addWidget(self.btn_delete_page_preset)
        layout.addLayout(preset_row)

        self.btn_check_tokens = QPushButton("  ตรวจสถานะ Token")
        self.btn_check_tokens.setObjectName("ghostBtn")
        self.btn_check_tokens.setCursor(Qt.PointingHandCursor)
        self.btn_check_tokens.clicked.connect(self.check_all_tokens_health)
        layout.addWidget(self.btn_check_tokens)

        # Connect / Cancel / Remove
        self.btn_connect = QPushButton("  เชื่อมต่อ Facebook")
        self.btn_connect.setObjectName("primaryBtn")
        self.btn_connect.setCursor(Qt.PointingHandCursor)
        self.btn_connect.clicked.connect(self.connect_fb)

        self.btn_cancel_oauth = QPushButton("  ยกเลิกการเชื่อมต่อ")
        self.btn_cancel_oauth.setObjectName("dangerBtn")
        self.btn_cancel_oauth.setCursor(Qt.PointingHandCursor)
        self.btn_cancel_oauth.setVisible(False)
        self.btn_cancel_oauth.clicked.connect(self.cancel_oauth)

        self.btn_remove = QPushButton("  ลบเพจที่เลือก")
        self.btn_remove.setObjectName("dangerBtn")
        self.btn_remove.setCursor(Qt.PointingHandCursor)
        self.btn_remove.clicked.connect(self.remove_selected)

        layout.addWidget(self.btn_connect)
        layout.addWidget(self.btn_cancel_oauth)
        layout.addWidget(self.btn_remove)

        # Bottom utility bar
        div2 = QFrame()
        div2.setObjectName("hairline")
        div2.setFrameShape(QFrame.HLine)
        layout.addWidget(div2)

        util_row = QHBoxLayout()
        util_row.setSpacing(8)
        self.btn_theme = DayNightThemeToggle(is_dark=(self.current_theme == "dark"))
        self.btn_theme.clicked.connect(self.toggle_theme)

        self.btn_settings = QPushButton(" ตั้งค่า")
        self.btn_settings.setObjectName("ghostBtn")
        self.btn_settings.setFixedHeight(34)
        self.btn_settings.setCursor(Qt.PointingHandCursor)
        self.btn_settings.clicked.connect(self.open_settings)

        util_row.addWidget(self.btn_theme)
        util_row.addWidget(self.btn_settings, 1)
        layout.addLayout(util_row)

        return sidebar

    def _build_kpi_strip(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(12)
        self.stat_pages = StatCard("เพจที่เชื่อมต่อ", "#2563EB", "pages")
        self.stat_selected = StatCard("เพจที่เลือกโพสต์", "#7C3AED", "check")
        self.stat_queued = StatCard("รอในคิวตั้งเวลา", "#D97706", "clock")
        self.stat_published = StatCard("โพสต์สำเร็จทั้งหมด", "#059669", "send")
        for card in (
            self.stat_pages,
            self.stat_selected,
            self.stat_queued,
            self.stat_published,
        ):
            row.addWidget(card, 1)
        return row

    def _build_composer_view(self) -> QWidget:
        view = QWidget()
        outer = QHBoxLayout(view)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(16)

        # Left: Composer Card (pinned heading + scrollable form body + pinned Submit button)
        comp_card = QFrame()
        comp_card.setObjectName("card")
        comp_shell = QVBoxLayout(comp_card)
        comp_shell.setContentsMargins(16, 10, 16, 10)
        comp_shell.setSpacing(8)

        # Heading + char counter
        top_hdr = QHBoxLayout()
        heading = QLabel("เขียนโพสต์ใหม่")
        heading.setObjectName("pageHeading")
        self.lbl_char_count = QLabel("0 ตัวอักษร")
        self.lbl_char_count.setObjectName("mutedLabel")
        top_hdr.addWidget(heading)
        top_hdr.addStretch(1)
        top_hdr.addWidget(self.lbl_char_count)
        comp_shell.addLayout(top_hdr)

        comp_scroll = QScrollArea()
        comp_scroll.setWidgetResizable(True)
        comp_scroll.setFrameShape(QFrame.NoFrame)
        comp_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        comp_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self.comp_scroll = comp_scroll
        comp_body = QWidget()
        comp_body.setObjectName("composerScrollContent")
        comp_body.setStyleSheet("QWidget#composerScrollContent { background: transparent; }")
        comp = QVBoxLayout(comp_body)
        comp.setContentsMargins(0, 0, 4, 0)
        comp.setSpacing(6)

        # Templates, Hashtags & Post Presets quick bar
        tpl_row = QHBoxLayout()
        tpl_row.setSpacing(6)
        self.combo_templates = QComboBox()
        self.combo_templates.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon
        )
        self.combo_templates.setMinimumContentsLength(3)
        self.combo_templates.setMinimumWidth(48)
        self.btn_insert_template = QPushButton("แทรก")
        self.btn_insert_template.setToolTip("แทรกเทมเพลตที่เลือกลงในข้อความ")
        self.btn_insert_template.setObjectName("ghostBtn")
        self.btn_insert_template.setCursor(Qt.PointingHandCursor)
        self.btn_insert_template.clicked.connect(self.insert_selected_template)
        self.btn_manage_templates = QPushButton("เทมเพลต")
        self.btn_manage_templates.setToolTip("จัดการเทมเพลตข้อความ / ชุดแฮชแท็ก")
        self.btn_manage_templates.setObjectName("ghostBtn")
        self.btn_manage_templates.setCursor(Qt.PointingHandCursor)
        self.btn_manage_templates.clicked.connect(self.open_templates_dialog)
        self.btn_post_presets = QPushButton("ชุดโพสต์")
        self.btn_post_presets.setToolTip(
            "คลังชุดโพสต์สำเร็จรูป (บันทึก/โหลดแคปชัน + ลิงก์ + รูป/วิดีโอ/Reels + ลายน้ำ + เช็คอิน + เม้นต์ต่อท้าย)"
        )
        self.btn_post_presets.setObjectName("ghostBtn")
        self.btn_post_presets.setCursor(Qt.PointingHandCursor)
        self.btn_post_presets.clicked.connect(self.open_post_presets_dialog)
        tpl_row.addWidget(self.combo_templates, 1)
        tpl_row.addWidget(self.btn_insert_template)
        tpl_row.addWidget(self.btn_manage_templates)
        tpl_row.addWidget(self.btn_post_presets)
        comp.addLayout(tpl_row)

        # Editor
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText(
            "พิมพ์ข้อความโพสต์ของคุณที่นี่… แล้วติ๊กเลือกเพจทางซ้าย"
        )
        self.editor.setMinimumSize(1, 104)
        self.editor.textChanged.connect(self._on_composer_changed)
        comp.addWidget(self.editor, 1)

        # Link Input
        link_row = QHBoxLayout()
        link_row.setSpacing(8)
        self.lbl_link_icon = QLabel()
        self.lbl_link_icon.setFixedSize(20, 20)
        self.input_link = QLineEdit()
        self.input_link.setPlaceholderText("แนบลิงก์ https://example.com (ไม่บังคับ)")
        self.input_link.textChanged.connect(self._on_composer_changed)
        link_row.addWidget(self.lbl_link_icon)
        link_row.addWidget(self.input_link, 1)
        comp.addLayout(link_row)

        # Drag & Drop Image Zone + Video Attachment
        img_hdr = QHBoxLayout()
        lbl_img = QLabel("รูปภาพ / วิดีโอแนบ")
        lbl_img.setObjectName("sectionTitle")
        self.btn_add_video = QPushButton("+ วิดีโอ")
        self.btn_add_video.setToolTip("แนบไฟล์วิดีโอ (MP4 / MOV) หรือโพสต์เป็น Reels")
        self.btn_add_video.setObjectName("ghostBtn")
        self.btn_add_video.setCursor(Qt.PointingHandCursor)
        self.btn_add_video.clicked.connect(self.select_video)
        self.btn_clear_images = QPushButton("ล้างรูป")
        self.btn_clear_images.setToolTip("ล้างรูปภาพทั้งหมด")
        self.btn_clear_images.setObjectName("ghostBtn")
        self.btn_clear_images.setCursor(Qt.PointingHandCursor)
        self.btn_clear_images.setVisible(False)
        self.btn_clear_images.clicked.connect(self.clear_images)
        img_hdr.addWidget(lbl_img)
        img_hdr.addStretch(1)
        img_hdr.addWidget(self.btn_add_video)
        img_hdr.addWidget(self.btn_clear_images)
        comp.addLayout(img_hdr)

        lbl_media_hint = QLabel(
            "รองรับรูปภาพ (1–หลายรูป) หรือวิดีโอ/Reels (1 คลิป) อย่างใดอย่างหนึ่งต่อ 1 โพสต์"
        )
        lbl_media_hint.setObjectName("mutedLabel")
        lbl_media_hint.setWordWrap(True)
        lbl_media_hint.setMinimumWidth(60)
        comp.addWidget(lbl_media_hint)

        # Video attached chip (hidden when no video selected)
        self.video_chip = QFrame()
        self.video_chip.setObjectName("scheduleCard")
        vchip_layout = QHBoxLayout(self.video_chip)
        vchip_layout.setContentsMargins(10, 6, 10, 6)
        vchip_layout.setSpacing(6)
        self.lbl_attached_video = QLabel("")
        self.lbl_attached_video.setObjectName("dropMainLabel")
        self.lbl_attached_video.setTextFormat(Qt.PlainText)
        self.lbl_attached_video.setWordWrap(True)
        self.lbl_attached_video.setMinimumWidth(40)
        self.chk_video_reel = IosToggleSwitch("Reels")
        self.chk_video_reel.setToolTip(
            "โพสต์วิดีโอนี้เป็น Facebook Reels (/{page_id}/video_reels • ตั้งเวลา FB Native ได้สูงสุด 29 วัน)"
        )
        self.chk_video_reel.toggled.connect(self._on_video_reel_toggled)
        self.btn_clear_video = QPushButton("ลบ")
        self.btn_clear_video.setToolTip("ลบวิดีโอที่แนบออก")
        self.btn_clear_video.setObjectName("dangerBtn")
        self.btn_clear_video.setCursor(Qt.PointingHandCursor)
        self.btn_clear_video.clicked.connect(self.clear_video)
        vchip_layout.addWidget(self.lbl_attached_video, 1)
        vchip_layout.addWidget(self.chk_video_reel)
        vchip_layout.addWidget(self.btn_clear_video)
        self.video_chip.setVisible(False)
        comp.addWidget(self.video_chip)

        self.drop_zone = DropImageZone()
        self.drop_zone.browse_clicked.connect(self.add_images)
        self.drop_zone.files_dropped.connect(self._add_image_paths)
        self.drop_zone.image_removed.connect(self._remove_image_path)
        comp.addWidget(self.drop_zone)

        # Auto Watermark Bar
        wm_row = QHBoxLayout()
        wm_row.setSpacing(8)
        self.chk_watermark = IosToggleSwitch("ประทับลายน้ำบนรูป:")
        self.chk_watermark.toggled.connect(self._on_watermark_toggled)
        self.input_watermark_text = QLineEdit()
        self.input_watermark_text.setPlaceholderText("เช่น © {page_name} (รองรับตัวแปร {page_name}, {date})")
        self.input_watermark_text.setEnabled(False)
        self.input_watermark_text.textChanged.connect(self._schedule_save_draft)
        wm_row.addWidget(self.chk_watermark)
        wm_row.addWidget(self.input_watermark_text, 1)
        comp.addLayout(wm_row)

        # Check-in Card (Activity/Feeling removed because Meta Graph API deprecated og_action_type_id)
        ctx_card = QFrame()
        ctx_card.setObjectName("scheduleCard")
        ctx_layout = QVBoxLayout(ctx_card)
        ctx_layout.setContentsMargins(10, 8, 10, 8)
        ctx_layout.setSpacing(6)

        place_row = QHBoxLayout()
        place_row.setSpacing(6)
        lbl_place = QLabel("📍 เช็คอิน:")
        lbl_place.setObjectName("sectionTitle")
        self.input_place_id = QLineEdit()
        self.input_place_id.setPlaceholderText("เลข Place ID หรือวางลิงก์เพจสถานที่ (ไม่บังคับ)")
        self.input_place_id.textChanged.connect(self._on_composer_changed)
        self.input_place_name = QLineEdit()
        self.input_place_name.setPlaceholderText("ชื่อสถานที่")
        self.input_place_name.setMaximumWidth(120)
        self.input_place_name.textChanged.connect(self._on_composer_changed)
        self.btn_pick_place = QPushButton("ค้นหา")
        self.btn_pick_place.setToolTip("ค้นหาหรือเลือกสถานที่เช็คอิน")
        self.btn_pick_place.setObjectName("ghostBtn")
        self.btn_pick_place.setCursor(Qt.PointingHandCursor)
        self.btn_pick_place.clicked.connect(self._open_place_picker)
        self.btn_clear_place = QPushButton("ล้าง")
        self.btn_clear_place.setObjectName("ghostBtn")
        self.btn_clear_place.setCursor(Qt.PointingHandCursor)
        self.btn_clear_place.clicked.connect(self._clear_place)
        place_row.addWidget(lbl_place)
        place_row.addWidget(self.input_place_id, 1)
        place_row.addWidget(self.input_place_name)
        place_row.addWidget(self.btn_pick_place)
        place_row.addWidget(self.btn_clear_place)
        ctx_layout.addLayout(place_row)
        comp.addWidget(ctx_card)

        # Scheduling Card
        sched_card = QFrame()
        sched_card.setObjectName("scheduleCard")
        sched_layout = QVBoxLayout(sched_card)
        sched_layout.setContentsMargins(10, 6, 10, 6)
        sched_layout.setSpacing(6)

        self.chk_schedule = IosToggleSwitch("ตั้งเวลาโพสต์")
        self.chk_schedule.toggled.connect(self._on_schedule_toggled)

        self.dt_schedule = QDateTimeEdit()
        self.dt_schedule.setCalendarPopup(True)
        self.dt_schedule.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.dt_schedule.setMinimumDateTime(QDateTime(2000, 1, 1, 0, 0, 0))
        self.dt_schedule.setMinimumWidth(108)
        self.dt_schedule.setDateTime(QDateTime.currentDateTime().addSecs(30 * 60))
        self.dt_schedule.setEnabled(False)
        self.dt_schedule.dateTimeChanged.connect(self._update_preview)

        self.combo_schedule_mode = QComboBox()
        self.combo_schedule_mode.addItem("FB Native", "fb_native")
        self.combo_schedule_mode.addItem("คิวในเครื่อง", "local_queue")
        self.combo_schedule_mode.setToolTip(
            "FB Native = ตั้งเวลาบน Facebook โดยตรง (ปิดแอปได้)\nคิวในเครื่อง = ส่งตามเวลาขณะเปิดแอปไว้ (รองรับคอมเมนต์ต่อท้ายและทำซ้ำ)"
        )
        self.combo_schedule_mode.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon
        )
        self.combo_schedule_mode.setMinimumContentsLength(6)
        self.combo_schedule_mode.setEnabled(False)
        self.combo_schedule_mode.currentIndexChanged.connect(self._on_schedule_mode_changed)

        self.combo_repeat_rule = QComboBox()
        self.combo_repeat_rule.addItem("ไม่ทำซ้ำ", "none")
        self.combo_repeat_rule.addItem("ซ้ำรายวัน", "daily")
        self.combo_repeat_rule.addItem("ซ้ำรายสัปดาห์", "weekly")
        self.combo_repeat_rule.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon
        )
        self.combo_repeat_rule.setMinimumContentsLength(6)
        self.combo_repeat_rule.setEnabled(False)
        self.combo_repeat_rule.setToolTip("ตั้งเวลาทำซ้ำอัตโนมัติ (ใช้ได้กับโหมด 'คิวในเครื่อง')")

        sched_r1 = QHBoxLayout()
        sched_r1.setSpacing(6)
        sched_r1.addWidget(self.chk_schedule)
        sched_r1.addWidget(self.dt_schedule, 1)

        sched_r2 = QHBoxLayout()
        sched_r2.setSpacing(6)
        sched_r2.addWidget(self.combo_schedule_mode, 1)
        sched_r2.addWidget(self.combo_repeat_rule, 1)

        sched_layout.addLayout(sched_r1)
        sched_layout.addLayout(sched_r2)
        comp.addWidget(sched_card)

        # Auto Follow-up Comments (post multiple comments right after publishing)
        fc_hdr = QHBoxLayout()
        lbl_fc = QLabel("คอมเมนต์ต่อท้ายอัตโนมัติ")
        lbl_fc.setObjectName("sectionTitle")
        lbl_fc.setToolTip("คอมเมนต์ต่อท้ายโพสต์อัตโนมัติทันทีหลังโพสต์สำเร็จ (เพิ่มได้หลายอัน)")
        self.lbl_followup_count = QLabel("0 คอมเมนต์")
        self.lbl_followup_count.setObjectName("mutedLabel")
        self.btn_clear_followup = QPushButton("ล้าง")
        self.btn_clear_followup.setObjectName("ghostBtn")
        self.btn_clear_followup.setCursor(Qt.PointingHandCursor)
        self.btn_clear_followup.setVisible(False)
        self.btn_clear_followup.clicked.connect(self.clear_followup_comments)
        fc_hdr.addWidget(lbl_fc)
        fc_hdr.addStretch(1)
        fc_hdr.addWidget(self.lbl_followup_count)
        fc_hdr.addWidget(self.btn_clear_followup)
        comp.addLayout(fc_hdr)

        fc_input_row = QHBoxLayout()
        fc_input_row.setSpacing(6)
        self.input_followup_comment = QLineEdit()
        self.input_followup_comment.setPlaceholderText(
            "พิมพ์คอมเมนต์ต่อท้าย (เช่น พิกัดสินค้า/ลิงก์) แล้วกด + เพิ่ม…"
        )
        self.input_followup_comment.returnPressed.connect(self.add_followup_comment)
        self.btn_add_followup = QPushButton("+ เพิ่ม")
        self.btn_add_followup.setObjectName("ghostBtn")
        self.btn_add_followup.setCursor(Qt.PointingHandCursor)
        self.btn_add_followup.clicked.connect(self.add_followup_comment)
        self.btn_remove_followup = QPushButton("ลบอันที่เลือก")
        self.btn_remove_followup.setObjectName("ghostBtn")
        self.btn_remove_followup.setCursor(Qt.PointingHandCursor)
        self.btn_remove_followup.setVisible(False)
        self.btn_remove_followup.clicked.connect(self.remove_selected_followup_comment)

        self.spin_followup_delay = QSpinBox()
        self.spin_followup_delay.setRange(0, 60)
        self.spin_followup_delay.setValue(2)
        self.spin_followup_delay.setSuffix(" วิ")
        self.spin_followup_delay.setToolTip("หน่วงเวลาระหว่างแต่ละคอมเมนต์ (วินาที)")
        self.spin_followup_delay.setFixedWidth(80)

        fc_input_row.addWidget(self.input_followup_comment, 1)
        fc_input_row.addWidget(self.spin_followup_delay)
        fc_input_row.addWidget(self.btn_add_followup)
        fc_input_row.addWidget(self.btn_remove_followup)
        comp.addLayout(fc_input_row)

        self.list_followup_comments = QListWidget()
        self.list_followup_comments.setMinimumSize(1, 38)
        self.list_followup_comments.setMaximumHeight(52)
        self.list_followup_comments.setVisible(False)
        comp.addWidget(self.list_followup_comments)

        # Inter-page anti-spam random delay / schedule stagger (when posting/sharing to multiple pages)
        pdelay_row = QHBoxLayout()
        pdelay_row.setSpacing(6)
        lbl_pdelay = QLabel("กระจายเวลา:")
        lbl_pdelay.setObjectName("mutedLabel")
        lbl_pdelay.setToolTip(
            "โหมดกันสแปม: โพสต์ทันทีจะหน่วงเวลาระหว่างเพจ / ตั้งเวลา FB Native จะกระจายเวลาโพสต์ห่างกันทีละเพจอัตโนมัติ"
        )
        self.combo_stagger_preset = QComboBox()
        self.combo_stagger_preset.addItem("ไม่หน่วง (0 วิ)", (0, 0))
        self.combo_stagger_preset.addItem("เบา (10–30 วิ)", (10, 30))
        self.combo_stagger_preset.addItem("กันสแปม (30–90 วิ)", (30, 90))
        self.combo_stagger_preset.addItem("ปลอดภัย (60–180 วิ)", (60, 180))
        self.combo_stagger_preset.addItem("กำหนดเอง…", None)
        self.combo_stagger_preset.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon
        )
        self.combo_stagger_preset.setMinimumContentsLength(3)
        self.combo_stagger_preset.setMinimumWidth(56)
        self.combo_stagger_preset.currentIndexChanged.connect(self._on_stagger_preset_changed)

        self.spin_page_delay_min = QSpinBox()
        self.spin_page_delay_min.setRange(0, 300)
        self.spin_page_delay_min.setValue(0)
        self.spin_page_delay_min.setSuffix(" วิ")
        self.spin_page_delay_min.setToolTip("เวลาหน่วง/กระจายขั้นต่ำระหว่างเพจ (0 = ไม่หน่วง)")
        self.spin_page_delay_min.setFixedWidth(64)
        self.spin_page_delay_min.valueChanged.connect(self._on_stagger_spins_changed)
        lbl_to = QLabel("–")
        lbl_to.setObjectName("mutedLabel")
        self.spin_page_delay_max = QSpinBox()
        self.spin_page_delay_max.setRange(0, 300)
        self.spin_page_delay_max.setValue(0)
        self.spin_page_delay_max.setSuffix(" วิ")
        self.spin_page_delay_max.setToolTip("เวลาหน่วง/กระจายสูงสุดระหว่างเพจ (สุ่มค่าระหว่างขั้นต่ำ-สูงสุด)")
        self.spin_page_delay_max.setFixedWidth(64)
        self.spin_page_delay_max.valueChanged.connect(self._on_stagger_spins_changed)
        pdelay_row.addWidget(lbl_pdelay)
        pdelay_row.addWidget(self.combo_stagger_preset, 1)
        pdelay_row.addWidget(self.spin_page_delay_min)
        pdelay_row.addWidget(lbl_to)
        pdelay_row.addWidget(self.spin_page_delay_max)
        comp.addLayout(pdelay_row)

        comp_scroll.setWidget(comp_body)
        comp_shell.addWidget(comp_scroll, 1)

        # Submit button (pinned cleanly at bottom)
        self.btn_post = QPushButton("  โพสต์ไปยังเพจที่เลือก")
        self.btn_post.setObjectName("primaryBtn")
        self.btn_post.setCursor(Qt.PointingHandCursor)
        self.btn_post.clicked.connect(self.post_selected)
        comp_shell.addWidget(self.btn_post)

        # Right: Realistic Live Facebook Preview Panel
        preview_col = QVBoxLayout()
        preview_col.setContentsMargins(0, 0, 0, 0)
        preview_col.setSpacing(8)

        pv_hdr = QHBoxLayout()
        lbl_pv = QLabel("พรีวิวโพสต์บน Facebook")
        lbl_pv.setObjectName("sectionTitle")
        self.lbl_preview_badge = QLabel("Live Preview")
        self.lbl_preview_badge.setObjectName("mutedLabel")
        pv_hdr.addWidget(lbl_pv)
        pv_hdr.addStretch(1)
        pv_hdr.addWidget(self.lbl_preview_badge)

        self.post_preview = FacebookPostPreview()
        # Compatibility aliases for tests
        self.lbl_preview_text = self.post_preview.lbl_body
        self.lbl_preview_meta = self.post_preview.lbl_link_url

        preview_col.addLayout(pv_hdr)
        preview_col.addWidget(self.post_preview, 1)

        preview_wrap = QWidget()
        preview_wrap.setLayout(preview_col)
        preview_wrap.setMinimumWidth(260)
        preview_wrap.setMaximumWidth(440)

        outer.addWidget(comp_card, 3)
        outer.addWidget(preview_wrap, 2)
        return view

    def _build_queue_view(self) -> QWidget:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        hdr = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        h = QLabel("คิวตั้งเวลาในเครื่อง")
        h.setObjectName("pageHeading")
        sub = QLabel(
            "โพสต์ที่ตั้งเวลาแบบคิวในเครื่องจะถูกส่งอัตโนมัติเมื่อถึงเวลาขณะเปิดโปรแกรมไว้"
        )
        sub.setObjectName("mutedLabel")
        title_box.addWidget(h)
        title_box.addWidget(sub)

        self.btn_queue_run_now = QPushButton("  โพสต์ทันที")
        self.btn_queue_run_now.setObjectName("primaryBtn")
        self.btn_queue_run_now.setCursor(Qt.PointingHandCursor)
        self.btn_queue_edit = QPushButton("  แก้ไขคิว")
        self.btn_queue_edit.setObjectName("ghostBtn")
        self.btn_queue_edit.setCursor(Qt.PointingHandCursor)
        self.btn_queue_retry_failed = QPushButton("  ส่งซ้ำที่ล้มเหลว")
        self.btn_queue_retry_failed.setObjectName("ghostBtn")
        self.btn_queue_retry_failed.setCursor(Qt.PointingHandCursor)
        self.btn_queue_import_csv = QPushButton("  นำเข้า CSV")
        self.btn_queue_import_csv.setObjectName("ghostBtn")
        self.btn_queue_import_csv.setCursor(Qt.PointingHandCursor)
        self.btn_queue_cancel = QPushButton("  ยกเลิกคิว")
        self.btn_queue_cancel.setObjectName("dangerBtn")
        self.btn_queue_cancel.setCursor(Qt.PointingHandCursor)
        self.btn_queue_refresh = QPushButton("  รีเฟรช")
        self.btn_queue_refresh.setObjectName("ghostBtn")
        self.btn_queue_refresh.setCursor(Qt.PointingHandCursor)

        self.btn_queue_run_now.clicked.connect(self.run_selected_queue_job)
        self.btn_queue_edit.clicked.connect(self.edit_selected_queue_job)
        self.btn_queue_retry_failed.clicked.connect(self.retry_all_failed_queue_jobs)
        self.btn_queue_import_csv.clicked.connect(self.import_queue_from_csv)
        self.btn_queue_cancel.clicked.connect(self.cancel_selected_queue_job)
        self.btn_queue_refresh.clicked.connect(self.refresh_scheduled_queue)

        hdr.addLayout(title_box, 1)
        hdr.addWidget(self.btn_queue_refresh)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        toolbar.addWidget(self.btn_queue_run_now)
        toolbar.addWidget(self.btn_queue_edit)
        toolbar.addWidget(self.btn_queue_retry_failed)
        toolbar.addWidget(self.btn_queue_import_csv)
        toolbar.addStretch(1)
        toolbar.addWidget(self.btn_queue_cancel)

        self.tbl_queue = QTableWidget(0, 6)
        self.tbl_queue.setAlternatingRowColors(True)
        self.tbl_queue.setHorizontalHeaderLabels(
            ["ID", "เวลาที่จะโพสต์", "เพจเป้าหมาย", "ข้อความ", "สื่อ/ลิงก์", "สถานะ"]
        )
        self.tbl_queue.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_queue.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tbl_queue.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_queue.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.tbl_queue.verticalHeader().setVisible(False)
        self.tbl_queue.verticalHeader().setDefaultSectionSize(38)

        layout.addLayout(hdr)
        layout.addLayout(toolbar)
        layout.addWidget(self.tbl_queue, 1)
        return card

    def _build_history_view(self) -> QWidget:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        hdr = QHBoxLayout()
        h = QLabel("ประวัติการโพสต์")
        h.setObjectName("pageHeading")
        hdr.addWidget(h)
        hdr.addStretch(1)

        # Status Filter Pills on top row
        self._filter_pills: dict[str, QPushButton] = {}
        for key, label in (
            ("all", "ทั้งหมด"),
            ("published", "โพสต์สำเร็จ"),
            ("scheduled", "ตั้งเวลาแล้ว"),
            ("failed", "ล้มเหลว"),
        ):
            pbtn = QPushButton(label)
            pbtn.setObjectName("pillBtn")
            pbtn.setCursor(Qt.PointingHandCursor)
            pbtn.setProperty("active", key == "all")
            pbtn.clicked.connect(lambda _=False, k=key: self._set_history_filter(k))
            self._filter_pills[key] = pbtn
            hdr.addWidget(pbtn)

        self.btn_history_repost = QPushButton(" โพสต์ซ้ำ")
        self.btn_history_repost.setObjectName("primaryBtn")
        self.btn_history_repost.setCursor(Qt.PointingHandCursor)
        self.btn_history_select_batch = QPushButton("เลือกทั้งชุด")
        self.btn_history_select_batch.setObjectName("ghostBtn")
        self.btn_history_select_batch.setCursor(Qt.PointingHandCursor)
        self.btn_history_select_batch.setToolTip("เลือกทุกเพจที่โพสต์พร้อมกันในชุดเดียวกันอัตโนมัติ")
        self.btn_history_insights = QPushButton(" ดูสถิติ")
        self.btn_history_insights.setObjectName("ghostBtn")
        self.btn_history_insights.setCursor(Qt.PointingHandCursor)
        self.btn_history_edit_post = QPushButton(" แก้ไขโพสต์")
        self.btn_history_edit_post.setObjectName("ghostBtn")
        self.btn_history_edit_post.setCursor(Qt.PointingHandCursor)
        self.btn_history_edit_post.setToolTip("แก้ไขข้อความ หรือเพิ่ม/เปลี่ยนรูปภาพและคลิปวิดีโอทีเดียวหลายเพจ")
        self.btn_history_comment = QPushButton(" คอมเมนต์")
        self.btn_history_comment.setObjectName("ghostBtn")
        self.btn_history_comment.setCursor(Qt.PointingHandCursor)
        self.btn_history_share = QPushButton(" แชร์โพสต์")
        self.btn_history_share.setObjectName("ghostBtn")
        self.btn_history_share.setCursor(Qt.PointingHandCursor)
        self.btn_history_delete = QPushButton("ลบประวัติ")
        self.btn_history_delete.setObjectName("ghostBtn")
        self.btn_history_delete.setCursor(Qt.PointingHandCursor)
        self.btn_history_delete_fb = QPushButton(" ลบจาก FB")
        self.btn_history_delete_fb.setObjectName("dangerBtn")
        self.btn_history_delete_fb.setCursor(Qt.PointingHandCursor)
        self.btn_history_clear = QPushButton("ล้างทั้งหมด")
        self.btn_history_clear.setObjectName("dangerBtn")
        self.btn_history_clear.setCursor(Qt.PointingHandCursor)

        self.btn_history_repost.clicked.connect(self.repost_selected_history)
        self.btn_history_select_batch.clicked.connect(self.select_same_batch_history)
        self.btn_history_insights.clicked.connect(self.view_selected_history_insights)
        self.btn_history_edit_post.clicked.connect(self.edit_selected_history_post)
        self.btn_history_comment.clicked.connect(self.comment_selected_history)
        self.btn_history_share.clicked.connect(self.share_selected_history)
        self.btn_history_delete.clicked.connect(self.delete_selected_history)
        self.btn_history_delete_fb.clicked.connect(self.delete_selected_history_from_fb)
        self.btn_history_clear.clicked.connect(self.clear_all_history)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        toolbar.addWidget(self.btn_history_repost)
        toolbar.addWidget(self.btn_history_select_batch)
        toolbar.addWidget(self.btn_history_insights)
        toolbar.addWidget(self.btn_history_edit_post)
        toolbar.addWidget(self.btn_history_comment)
        toolbar.addWidget(self.btn_history_share)
        toolbar.addStretch(1)
        toolbar.addWidget(self.btn_history_delete)
        toolbar.addWidget(self.btn_history_delete_fb)
        toolbar.addWidget(self.btn_history_clear)

        self.tbl_history = QTableWidget(0, 6)
        self.tbl_history.setAlternatingRowColors(True)
        self.tbl_history.setHorizontalHeaderLabels(
            ["เวลา", "เพจ", "สถานะ", "ข้อความ", "สื่อ/ลิงก์", "Post ID / ข้อผิดพลาด"]
        )
        self.tbl_history.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_history.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tbl_history.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_history.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.tbl_history.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.tbl_history.verticalHeader().setVisible(False)
        self.tbl_history.verticalHeader().setDefaultSectionSize(38)

        layout.addLayout(hdr)
        layout.addLayout(toolbar)
        layout.addWidget(self.tbl_history, 1)
        return card

    def _build_comments_view(self) -> QWidget:
        view = QWidget()
        outer = QHBoxLayout(view)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(14)

        # Left card: Select Page & Post
        left_card = QFrame()
        left_card.setObjectName("card")
        left = QVBoxLayout(left_card)
        left.setContentsMargins(16, 14, 16, 14)
        left.setSpacing(10)

        h_left = QLabel("1. เลือกเพจและโพสต์")
        h_left.setObjectName("pageHeading")
        left.addWidget(h_left)

        page_row = QHBoxLayout()
        page_row.setSpacing(6)
        self.combo_comment_page = QComboBox()
        self.combo_comment_page.currentIndexChanged.connect(self._on_comment_page_changed)
        self.btn_load_page_posts = QPushButton("โหลดโพสต์ล่าสุด")
        self.btn_load_page_posts.setObjectName("ghostBtn")
        self.btn_load_page_posts.setCursor(Qt.PointingHandCursor)
        self.btn_load_page_posts.clicked.connect(self.load_posts_for_comment)
        page_row.addWidget(self.combo_comment_page, 1)
        page_row.addWidget(self.btn_load_page_posts)
        left.addLayout(page_row)

        lbl_posts = QLabel("โพสต์ล่าสุดของเพจ (คลิกเพื่อเลือก)")
        lbl_posts.setObjectName("sectionTitle")
        left.addWidget(lbl_posts)

        self.list_page_posts = QListWidget()
        self.list_page_posts.itemClicked.connect(self._on_page_post_selected)
        left.addWidget(self.list_page_posts, 1)

        post_act_row = QHBoxLayout()
        post_act_row.setSpacing(6)
        self.btn_edit_page_post = QPushButton("  แก้ไขโพสต์ที่เลือกบน FB")
        self.btn_edit_page_post.setObjectName("ghostBtn")
        self.btn_edit_page_post.setCursor(Qt.PointingHandCursor)
        self.btn_edit_page_post.clicked.connect(self.edit_selected_page_post)
        self.btn_delete_page_post = QPushButton("ลบโพสต์บน FB")
        self.btn_delete_page_post.setObjectName("dangerBtn")
        self.btn_delete_page_post.setCursor(Qt.PointingHandCursor)
        self.btn_delete_page_post.clicked.connect(self.delete_selected_page_post_from_fb)
        post_act_row.addWidget(self.btn_edit_page_post, 1)
        post_act_row.addWidget(self.btn_delete_page_post)
        left.addLayout(post_act_row)

        lbl_pid = QLabel("หรือวางลิงก์โพสต์ (เพจตัวเอง / เพจอื่น) หรือ Post ID:")
        lbl_pid.setObjectName("sectionTitle")
        pid_row = QHBoxLayout()
        pid_row.setSpacing(6)
        self.input_comment_post_id = QLineEdit()
        self.input_comment_post_id.setPlaceholderText(
            "https://www.facebook.com/.../posts/... หรือ 123456_789012"
        )
        self.btn_resolve_post_url = QPushButton("แกะลิงก์")
        self.btn_resolve_post_url.setObjectName("ghostBtn")
        self.btn_resolve_post_url.setCursor(Qt.PointingHandCursor)
        self.btn_resolve_post_url.clicked.connect(self.resolve_comment_post_url)
        pid_row.addWidget(self.input_comment_post_id, 1)
        pid_row.addWidget(self.btn_resolve_post_url)
        left.addWidget(lbl_pid)
        left.addLayout(pid_row)

        self.btn_open_comments_inbox = QPushButton("  กล่องอ่านและตอบคอมเมนต์ของโพสต์นี้")
        self.btn_open_comments_inbox.setObjectName("ghostBtn")
        self.btn_open_comments_inbox.setCursor(Qt.PointingHandCursor)
        self.btn_open_comments_inbox.clicked.connect(self.open_post_comments_inbox)
        left.addWidget(self.btn_open_comments_inbox)

        ar_card = QFrame()
        ar_card.setObjectName("scheduleCard")
        ar_lay = QVBoxLayout(ar_card)
        ar_lay.setContentsMargins(10, 8, 10, 8)
        ar_lay.setSpacing(6)
        self.lbl_auto_reply_status = QLabel("🤖 ตอบคอมเมนต์อัตโนมัติ: ปิดอยู่")
        self.lbl_auto_reply_status.setObjectName("mutedLabel")
        self.lbl_auto_reply_status.setWordWrap(True)
        ar_btn_row = QHBoxLayout()
        ar_btn_row.setSpacing(6)
        self.btn_auto_reply_rules = QPushButton("ตั้งค่ากฎคีย์เวิร์ด…")
        self.btn_auto_reply_rules.setObjectName("ghostBtn")
        self.btn_auto_reply_rules.setCursor(Qt.PointingHandCursor)
        self.btn_auto_reply_rules.clicked.connect(self.open_auto_reply_rules_dialog)
        self.btn_run_auto_reply_now = QPushButton("⚡ ตรวจตอบตอนนี้")
        self.btn_run_auto_reply_now.setObjectName("primaryBtn")
        self.btn_run_auto_reply_now.setCursor(Qt.PointingHandCursor)
        self.btn_run_auto_reply_now.clicked.connect(
            lambda: self.run_auto_reply_now(manual=True)
        )
        ar_btn_row.addWidget(self.btn_auto_reply_rules, 1)
        ar_btn_row.addWidget(self.btn_run_auto_reply_now)
        ar_lay.addWidget(self.lbl_auto_reply_status)
        ar_lay.addLayout(ar_btn_row)
        left.addWidget(ar_card)

        # Right card: Multi-Comment Queue Builder
        right_card = QFrame()
        right_card.setObjectName("card")
        right = QVBoxLayout(right_card)
        right.setContentsMargins(16, 14, 16, 14)
        right.setSpacing(10)

        h_right = QLabel("2. คิวคอมเมนต์หลายอัน")
        h_right.setObjectName("pageHeading")
        sub_right = QLabel(
            "เพิ่มคอมเมนต์ได้ไม่จำกัด (ระบบจะทยอยคอมเมนต์เรียงตามลำดับพร้อมหน่วงเวลา)"
        )
        sub_right.setObjectName("mutedLabel")
        sub_right.setWordWrap(True)
        right.addWidget(h_right)
        right.addWidget(sub_right)

        self.editor_comment_input = QPlainTextEdit()
        self.editor_comment_input.setPlaceholderText(
            "พิมพ์ข้อความคอมเมนต์ที่นี่…\n"
            "(หรือวางหลายบรรทัดแล้วกด 'แยกทีละบรรทัด' เพื่อเพิ่มทีเดียวหลายคอมเมนต์)"
        )
        self.editor_comment_input.setMaximumHeight(110)
        right.addWidget(self.editor_comment_input)

        cimg_row = QHBoxLayout()
        cimg_row.setSpacing(6)
        self.btn_attach_comment_image = QPushButton("📎 แนบรูปในคอมเมนต์")
        self.btn_attach_comment_image.setObjectName("ghostBtn")
        self.btn_attach_comment_image.setCursor(Qt.PointingHandCursor)
        self.btn_attach_comment_image.clicked.connect(self.select_comment_image)
        self.lbl_comment_image = QLabel("")
        self.lbl_comment_image.setObjectName("mutedLabel")
        self.lbl_comment_image.setTextFormat(Qt.PlainText)
        self.btn_clear_comment_image = QPushButton("ลบรูปแนบ")
        self.btn_clear_comment_image.setObjectName("dangerBtn")
        self.btn_clear_comment_image.setCursor(Qt.PointingHandCursor)
        self.btn_clear_comment_image.setVisible(False)
        self.btn_clear_comment_image.clicked.connect(self.clear_comment_image)
        cimg_row.addWidget(self.btn_attach_comment_image)
        cimg_row.addWidget(self.lbl_comment_image, 1)
        cimg_row.addWidget(self.btn_clear_comment_image)
        right.addLayout(cimg_row)

        add_row = QHBoxLayout()
        add_row.setSpacing(6)
        self.btn_add_single_comment = QPushButton("+ เพิ่มเป็น 1 คอมเมนต์")
        self.btn_add_single_comment.setObjectName("ghostBtn")
        self.btn_add_single_comment.setCursor(Qt.PointingHandCursor)
        self.btn_add_single_comment.clicked.connect(self.add_single_queued_comment)

        self.btn_add_lines_comments = QPushButton("+ แยกทีละบรรทัด (หลายอัน)")
        self.btn_add_lines_comments.setObjectName("ghostBtn")
        self.btn_add_lines_comments.setCursor(Qt.PointingHandCursor)
        self.btn_add_lines_comments.clicked.connect(self.add_multiline_queued_comments)

        add_row.addWidget(self.btn_add_single_comment, 1)
        add_row.addWidget(self.btn_add_lines_comments, 1)
        right.addLayout(add_row)

        q_hdr = QHBoxLayout()
        self.lbl_comment_queue_count = QLabel("รายการคอมเมนต์ (0 อัน)")
        self.lbl_comment_queue_count.setObjectName("sectionTitle")
        self.btn_comment_up = QPushButton("ขึ้น")
        self.btn_comment_up.setObjectName("ghostBtn")
        self.btn_comment_down = QPushButton("ลง")
        self.btn_comment_down.setObjectName("ghostBtn")
        self.btn_comment_del = QPushButton("ลบ")
        self.btn_comment_del.setObjectName("ghostBtn")
        self.btn_comment_clear = QPushButton("ล้างทั้งหมด")
        self.btn_comment_clear.setObjectName("dangerBtn")

        self.btn_comment_up.clicked.connect(lambda: self._move_queued_comment(-1))
        self.btn_comment_down.clicked.connect(lambda: self._move_queued_comment(1))
        self.btn_comment_del.clicked.connect(self.remove_selected_queued_comment)
        self.btn_comment_clear.clicked.connect(self.clear_queued_comments)

        q_hdr.addWidget(self.lbl_comment_queue_count)
        q_hdr.addStretch(1)
        q_hdr.addWidget(self.btn_comment_up)
        q_hdr.addWidget(self.btn_comment_down)
        q_hdr.addWidget(self.btn_comment_del)
        q_hdr.addWidget(self.btn_comment_clear)
        right.addLayout(q_hdr)

        self.list_comments = QListWidget()
        right.addWidget(self.list_comments, 1)

        send_row = QHBoxLayout()
        send_row.setSpacing(10)
        lbl_delay = QLabel("หน่วงเวลาระหว่างคอมเมนต์:")
        lbl_delay.setObjectName("mutedLabel")
        self.spin_comment_delay = QSpinBox()
        self.spin_comment_delay.setRange(0, 60)
        self.spin_comment_delay.setValue(2)
        self.spin_comment_delay.setSuffix(" วินาที")
        self.spin_comment_delay.setFixedWidth(96)

        self.btn_send_comments = QPushButton("  ส่งคอมเมนต์ทั้งหมด (0 อัน)")
        self.btn_send_comments.setObjectName("primaryBtn")
        self.btn_send_comments.setCursor(Qt.PointingHandCursor)
        self.btn_send_comments.clicked.connect(self.send_queued_comments)

        send_row.addWidget(lbl_delay)
        send_row.addWidget(self.spin_comment_delay)
        send_row.addWidget(self.btn_send_comments, 1)
        right.addLayout(send_row)

        # Quick Comment Helper for external pages (manual copy + open in browser)
        quick_row = QHBoxLayout()
        quick_row.setSpacing(8)
        self.btn_quick_comment_browser = QPushButton(
            "คัดลอกและเปิดโพสต์ในเบราว์เซอร์ (สำหรับเพจอื่น)"
        )
        self.btn_quick_comment_browser.setObjectName("ghostBtn")
        self.btn_quick_comment_browser.setCursor(Qt.PointingHandCursor)
        self.btn_quick_comment_browser.setToolTip(
            "คัดลอกคอมเมนต์ลงคลิปบอร์ดแล้วเปิดโพสต์ในเบราว์เซอร์ เพื่อให้คุณกด Ctrl+V วางเอง (ใช้ได้กับทุกเพจอย่างปลอดภัย)"
        )
        self.btn_quick_comment_browser.clicked.connect(self.quick_comment_open_browser)

        self.btn_copy_next_comment = QPushButton("คัดลอกอันถัดไป")
        self.btn_copy_next_comment.setObjectName("ghostBtn")
        self.btn_copy_next_comment.setCursor(Qt.PointingHandCursor)
        self.btn_copy_next_comment.setToolTip(
            "คัดลอกคอมเมนต์ที่เลือกแล้วเลื่อนไปอันถัดไปอัตโนมัติ (กรณีเปิดหน้าโพสต์ค้างไว้แล้ว)"
        )
        self.btn_copy_next_comment.clicked.connect(self.copy_next_queued_comment)

        quick_row.addWidget(self.btn_quick_comment_browser, 1)
        quick_row.addWidget(self.btn_copy_next_comment)
        right.addLayout(quick_row)

        outer.addWidget(left_card, 2)
        outer.addWidget(right_card, 3)
        return view

    def _build_groups_share_view(self) -> QWidget:
        view = QWidget()
        outer = QHBoxLayout(view)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(14)

        # Left card: Saved Facebook Groups
        left_card = QFrame()
        left_card.setObjectName("card")
        left = QVBoxLayout(left_card)
        left.setContentsMargins(16, 14, 16, 14)
        left.setSpacing(10)

        h_left = QLabel("1. สมุดบันทึกกลุ่ม Facebook")
        h_left.setObjectName("pageHeading")
        left.addWidget(h_left)

        self.input_group_name = QLineEdit()
        self.input_group_name.setPlaceholderText("ชื่อกลุ่ม เช่น กลุ่มซื้อขายกาแฟ / ชุมชนคนแต่งบ้าน")
        self.input_group_url = QLineEdit()
        self.input_group_url.setPlaceholderText("https://www.facebook.com/groups/...")
        self.btn_add_group = QPushButton("+ บันทึกกลุ่มเข้าสมุด")
        self.btn_add_group.setObjectName("ghostBtn")
        self.btn_add_group.setCursor(Qt.PointingHandCursor)
        self.btn_add_group.clicked.connect(self.add_saved_group)

        left.addWidget(self.input_group_name)
        left.addWidget(self.input_group_url)
        left.addWidget(self.btn_add_group)

        g_hdr = QHBoxLayout()
        lbl_g = QLabel("กลุ่มที่บันทึกไว้ (ติ๊กเลือกเพื่อเปิดแชร์)")
        lbl_g.setObjectName("sectionTitle")
        self.lbl_groups_count = QLabel("เลือก 0 / 0")
        self.lbl_groups_count.setObjectName("mutedLabel")
        g_hdr.addWidget(lbl_g)
        g_hdr.addStretch(1)
        g_hdr.addWidget(self.lbl_groups_count)
        left.addLayout(g_hdr)

        self.list_saved_groups = QListWidget()
        self.list_saved_groups.setIconSize(QSize(26, 26))
        self.list_saved_groups.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_saved_groups.setTextElideMode(Qt.ElideRight)
        self.list_saved_groups.itemChanged.connect(self._on_saved_groups_changed)
        left.addWidget(self.list_saved_groups, 1)

        g_btns = QHBoxLayout()
        g_btns.setSpacing(6)
        self.btn_groups_select_all = QPushButton("เลือกทั้งหมด")
        self.btn_groups_select_all.setObjectName("ghostBtn")
        self.btn_groups_select_all.setCursor(Qt.PointingHandCursor)
        self.btn_groups_select_none = QPushButton("ไม่เลือก")
        self.btn_groups_select_none.setObjectName("ghostBtn")
        self.btn_groups_select_none.setCursor(Qt.PointingHandCursor)
        self.btn_groups_delete = QPushButton("ลบที่เลือก")
        self.btn_groups_delete.setObjectName("dangerBtn")
        self.btn_groups_delete.setCursor(Qt.PointingHandCursor)
        self.btn_groups_select_all.clicked.connect(lambda: self._set_all_saved_groups(True))
        self.btn_groups_select_none.clicked.connect(lambda: self._set_all_saved_groups(False))
        self.btn_groups_delete.clicked.connect(self.delete_selected_saved_groups)
        g_btns.addWidget(self.btn_groups_select_all, 1)
        g_btns.addWidget(self.btn_groups_select_none, 1)
        g_btns.addWidget(self.btn_groups_delete)
        left.addLayout(g_btns)

        # Right card: Quick Share Helper
        right_card = QFrame()
        right_card.setObjectName("card")
        right = QVBoxLayout(right_card)
        right.setContentsMargins(16, 14, 16, 14)
        right.setSpacing(10)

        hdr_r = QHBoxLayout()
        h_right = QLabel("2. แชร์โพสต์ลงเพจตัวเอง / ลงกลุ่ม")
        h_right.setObjectName("pageHeading")
        self.btn_share_load_latest = QPushButton("ดึงโพสต์ล่าสุดจากประวัติ")
        self.btn_share_load_latest.setObjectName("ghostBtn")
        self.btn_share_load_latest.setCursor(Qt.PointingHandCursor)
        self.btn_share_load_latest.clicked.connect(self.load_latest_post_for_share)
        hdr_r.addWidget(h_right)
        hdr_r.addStretch(1)
        hdr_r.addWidget(self.btn_share_load_latest)
        right.addLayout(hdr_r)

        info_note = QLabel(
            "วางลิงก์โพสต์ Facebook (จากเพจอื่น, โพสต์เดิม หรือกลุ่มสาธารณะ) + พิมพ์แคปชัน "
            "แล้วกด 'แชร์ลงหน้าเพจที่ติ๊กเลือก' เพื่อแชร์ลงหน้าเพจของคุณผ่าน API ทันที (หรือใช้ปุ่มด้านล่างสำหรับแชร์ลงกลุ่ม)"
        )
        info_note.setObjectName("mutedLabel")
        info_note.setWordWrap(True)
        right.addWidget(info_note)

        lbl_u = QLabel("ลิงก์โพสต์ที่ต้องการแชร์:")
        lbl_u.setObjectName("sectionTitle")
        self.input_share_url = QLineEdit()
        self.input_share_url.setPlaceholderText("https://www.facebook.com/.../posts/...")
        right.addWidget(lbl_u)
        right.addWidget(self.input_share_url)

        lbl_c = QLabel("ข้อความแนะนำ / แคปชันประกอบตอนแชร์ (ไม่บังคับ):")
        lbl_c.setObjectName("sectionTitle")
        self.editor_share_caption = QPlainTextEdit()
        self.editor_share_caption.setPlaceholderText(
            "พิมพ์แคปชันประกอบโพสต์ที่ต้องการแชร์ลงเพจหรือลงกลุ่ม (ปล่อยว่างได้)…"
        )
        right.addWidget(lbl_c)
        right.addWidget(self.editor_share_caption, 1)

        self.btn_share_to_pages = QPushButton("  แชร์ลงหน้าเพจที่ติ๊กเลือก (0 เพจ)")
        self.btn_share_to_pages.setObjectName("primaryBtn")
        self.btn_share_to_pages.setCursor(Qt.PointingHandCursor)
        self.btn_share_to_pages.clicked.connect(self.share_to_selected_pages)
        right.addWidget(self.btn_share_to_pages)

        self.btn_open_checked_groups = QPushButton("  คัดลอกและเปิดกลุ่มที่ติ๊ก (0 กลุ่ม)")
        self.btn_open_checked_groups.setObjectName("ghostBtn")
        self.btn_open_checked_groups.setCursor(Qt.PointingHandCursor)
        self.btn_open_checked_groups.clicked.connect(self.open_checked_groups_for_share)
        right.addWidget(self.btn_open_checked_groups)

        act_row = QHBoxLayout()
        act_row.setSpacing(8)
        self.btn_open_fb_sharer = QPushButton("  เปิดหน้าต่างแชร์ Facebook (Share Dialog)")
        self.btn_open_fb_sharer.setObjectName("ghostBtn")
        self.btn_open_fb_sharer.setCursor(Qt.PointingHandCursor)
        self.btn_open_fb_sharer.clicked.connect(self.open_facebook_share_dialog)

        self.btn_copy_share_text = QPushButton("คัดลอกลิงก์ + ข้อความ")
        self.btn_copy_share_text.setObjectName("ghostBtn")
        self.btn_copy_share_text.setCursor(Qt.PointingHandCursor)
        self.btn_copy_share_text.clicked.connect(self.copy_share_text)

        act_row.addWidget(self.btn_open_fb_sharer, 1)
        act_row.addWidget(self.btn_copy_share_text)
        right.addLayout(act_row)

        outer.addWidget(left_card, 2)
        outer.addWidget(right_card, 3)
        return view

    def _build_analytics_view(self) -> QWidget:
        """Build View 5: Page Analytics, Page Comparison & RT Dashboard (all pages in one view)."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self.analytics_scroll = scroll

        view = QWidget()
        view.setObjectName("analyticsScrollContent")
        view.setStyleSheet("QWidget#analyticsScrollContent { background: transparent; }")
        layout = QVBoxLayout(view)
        layout.setContentsMargins(0, 0, 6, 6)
        layout.setSpacing(12)

        # 1) Top Controls Bar (Scope, Period, Auto Refresh, Refresh Now, Export CSV)
        ctrl_card = QFrame()
        ctrl_card.setObjectName("card")
        ctrl_v = QVBoxLayout(ctrl_card)
        ctrl_v.setContentsMargins(16, 12, 16, 12)
        ctrl_v.setSpacing(8)

        hdr_row = QHBoxLayout()
        hdr_row.setSpacing(8)
        lbl_title = QLabel("RT Dashboard & Page Analytics (ภาพรวมและเปรียบเทียบทุกเพจ)")
        lbl_title.setObjectName("cardTitle")
        self.lbl_analytics_updated = QLabel("ยังไม่ได้ดึงข้อมูลสด")
        self.lbl_analytics_updated.setObjectName("mutedLabel")
        hdr_row.addWidget(lbl_title)
        hdr_row.addStretch(1)
        hdr_row.addWidget(self.lbl_analytics_updated)
        ctrl_v.addLayout(hdr_row)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.combo_analytics_scope = QComboBox()
        self.combo_analytics_scope.addItem("เปรียบเทียบ: ทุกเพจที่เชื่อมต่อ", "all")
        self.combo_analytics_scope.addItem("เปรียบเทียบ: เฉพาะเพจที่ติ๊กเลือกทางซ้าย", "selected")
        self.combo_analytics_scope.currentIndexChanged.connect(self._on_analytics_filter_changed)

        self.combo_analytics_period = QComboBox()
        self.combo_analytics_period.addItem("วันนี้ (1 วัน)", 1)
        self.combo_analytics_period.addItem("7 วันล่าสุด", 7)
        self.combo_analytics_period.addItem("28 วันล่าสุด", 28)
        self.combo_analytics_period.setCurrentIndex(1)  # default 7 days
        self.combo_analytics_period.currentIndexChanged.connect(self._on_analytics_period_changed)

        self.chk_analytics_auto_refresh = IosToggleSwitch("Auto Refresh")
        self.chk_analytics_auto_refresh.toggled.connect(self._on_analytics_auto_refresh_changed)

        self.combo_analytics_interval = QComboBox()
        self.combo_analytics_interval.addItem("ทุก 1 นาที", 60)
        self.combo_analytics_interval.addItem("ทุก 5 นาที", 300)
        self.combo_analytics_interval.addItem("ทุก 15 นาที", 900)
        self.combo_analytics_interval.setCurrentIndex(1)  # default 5 min
        self.combo_analytics_interval.currentIndexChanged.connect(self._on_analytics_auto_refresh_changed)

        self.btn_analytics_refresh = QPushButton("  รีเฟรชข้อมูลตอนนี้")
        self.btn_analytics_refresh.setObjectName("primaryBtn")
        self.btn_analytics_refresh.setCursor(Qt.PointingHandCursor)
        self.btn_analytics_refresh.clicked.connect(lambda: self.refresh_page_analytics(silent=False))

        self.btn_analytics_export = QPushButton("ส่งออก CSV")
        self.btn_analytics_export.setObjectName("ghostBtn")
        self.btn_analytics_export.setCursor(Qt.PointingHandCursor)
        self.btn_analytics_export.clicked.connect(self.export_analytics_csv)

        bar.addWidget(self.combo_analytics_scope)
        bar.addWidget(self.combo_analytics_period)
        bar.addWidget(self.chk_analytics_auto_refresh)
        bar.addWidget(self.combo_analytics_interval)
        bar.addStretch(1)
        bar.addWidget(self.btn_analytics_refresh)
        bar.addWidget(self.btn_analytics_export)
        ctrl_v.addLayout(bar)
        layout.addWidget(ctrl_card)

        # 2) RT Dashboard Live KPI Strip (4 Cards)
        kpi_row = QHBoxLayout()
        kpi_row.setSpacing(10)
        self.rt_card_reach = StatCard("Live Reach รวม", "#0A84FF", "analytics")
        self.rt_card_engagement = StatCard("Live Engagement รวม", "#30D158", "comment")
        self.rt_card_impressions = StatCard("Impressions / Link Clicks", "#BF5AF2", "pages")
        self.rt_card_followers = StatCard("Followers รวม / เพจมีปัญหา", "#FF9F0A", "check")
        kpi_row.addWidget(self.rt_card_reach, 1)
        kpi_row.addWidget(self.rt_card_engagement, 1)
        kpi_row.addWidget(self.rt_card_impressions, 1)
        kpi_row.addWidget(self.rt_card_followers, 1)
        layout.addLayout(kpi_row)

        # 3) Page Comparison Highlights Card
        cmp_card = QFrame()
        cmp_card.setObjectName("card")
        cmp_v = QVBoxLayout(cmp_card)
        cmp_v.setContentsMargins(16, 12, 16, 12)
        cmp_v.setSpacing(6)

        lbl_cmp = QLabel("สรุปเปรียบเทียบผลงานเพจ (Page Comparison)")
        lbl_cmp.setObjectName("sectionTitle")
        cmp_v.addWidget(lbl_cmp)

        self.lbl_cmp_top_reach = QLabel("🏆 Reach สูงสุด: —")
        self.lbl_cmp_top_engagement = QLabel("🔥 Engagement สูงสุด: —")
        self.lbl_cmp_declining = QLabel("📉 Reach ลดลง: —")
        self.lbl_cmp_growing = QLabel("📈 เพจที่มีการเติบโต: —")
        self.lbl_cmp_stale = QLabel("⚠️ ไม่มีข้อมูลอัปเดต / มีปัญหา: —")
        for lbl in (
            self.lbl_cmp_top_reach,
            self.lbl_cmp_top_engagement,
            self.lbl_cmp_declining,
            self.lbl_cmp_growing,
            self.lbl_cmp_stale,
        ):
            lbl.setTextFormat(Qt.PlainText)
            lbl.setWordWrap(True)
            cmp_v.addWidget(lbl)
        layout.addWidget(cmp_card)

        # 4) Page Analytics Breakdown Table (All 8 Metrics + Growth + Status)
        tbl_card = QFrame()
        tbl_card.setObjectName("card")
        tbl_v = QVBoxLayout(tbl_card)
        tbl_v.setContentsMargins(16, 12, 16, 12)
        tbl_v.setSpacing(8)

        tbl_hdr = QHBoxLayout()
        lbl_tbl = QLabel("ตารางสถิติแยกตามเพจ (คลิกหัวคอลัมน์เรียง • ดับเบิลคลิกเพื่อโพสต์ • คลิกขวาดูเมนู)")
        lbl_tbl.setObjectName("sectionTitle")
        self.lbl_analytics_count = QLabel("0 เพจ")
        self.lbl_analytics_count.setObjectName("mutedLabel")
        tbl_hdr.addWidget(lbl_tbl)
        tbl_hdr.addStretch(1)
        tbl_hdr.addWidget(self.lbl_analytics_count)
        tbl_v.addLayout(tbl_hdr)

        self.tbl_analytics = QTableWidget(0, 11)
        analytics_headers = [
            ("เพจ", "ชื่อเพจ Facebook"),
            ("Reach", "Reach — จำนวนคนที่เข้าถึงโพสต์"),
            ("แนวโน้ม", "แนวโน้ม Reach เทียบช่วงก่อนหน้า (%)"),
            ("Impr.", "Impressions — จำนวนครั้งที่เนื้อหาถูกแสดง"),
            ("Engage", "Engagement — การมีส่วนร่วมทั้งหมด"),
            ("React", "Reactions — จำนวนปฏิกิริยา"),
            ("Com.", "Comments — จำนวนความคิดเห็น"),
            ("Share", "Shares — จำนวนแชร์"),
            ("Clicks", "Link Clicks — จำนวนคลิกลิงก์"),
            ("Follow", "Followers — จำนวนผู้ติดตาม"),
            ("สถานะ / ปัญหา", "สถานะการดึงข้อมูลและปัญหาที่พบ"),
        ]
        for col_idx, (h_text, h_tip) in enumerate(analytics_headers):
            h_item = QTableWidgetItem(h_text)
            h_item.setToolTip(h_tip)
            self.tbl_analytics.setHorizontalHeaderItem(col_idx, h_item)
        self.tbl_analytics.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_analytics.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_analytics.setAlternatingRowColors(True)
        self.tbl_analytics.setSortingEnabled(True)
        self.tbl_analytics.verticalHeader().setVisible(False)
        self.tbl_analytics.setMinimumHeight(280)
        self.tbl_analytics.cellDoubleClicked.connect(self._on_analytics_row_double_clicked)
        self.tbl_analytics.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tbl_analytics.customContextMenuRequested.connect(self._show_analytics_context_menu)
        hdr = self.tbl_analytics.horizontalHeader()
        hdr.setMinimumSectionSize(50)
        col_widths = {0: 150, 1: 72, 2: 72, 3: 72, 4: 72, 5: 66, 6: 62, 7: 62, 8: 62, 9: 72}
        for col, width in col_widths.items():
            hdr.setSectionResizeMode(col, QHeaderView.Interactive)
            self.tbl_analytics.setColumnWidth(col, width)
        hdr.setSectionResizeMode(10, QHeaderView.Stretch)
        tbl_v.addWidget(self.tbl_analytics, 1)

        layout.addWidget(tbl_card, 1)
        scroll.setWidget(view)
        return scroll

    # --- Navigation & Theme ---

    def _switch_page(self, index: int):
        self.pages_stack.setCurrentIndex(index)
        for i, btn in enumerate(self._nav_buttons):
            btn.setProperty("active", i == index)
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        self._refresh_icons()

    def _refresh_icons(self):
        is_dark = self.current_theme == "dark"
        muted = "#AEAEB2" if is_dark else "#636366"
        accent = "#0A84FF" if is_dark else "#007AFF"
        danger = "#FF453A" if is_dark else "#FF3B30"

        nav_specs = [
            ("compose", 0),
            ("clock", 1),
            ("history", 2),
            ("comment", 3),
            ("share", 4),
            ("analytics", 5),
        ]
        for icon_name, idx in nav_specs:
            color = accent if self.pages_stack.currentIndex() == idx else muted
            self._nav_buttons[idx].setIcon(ui_icons.svg_icon(icon_name, color, 17))

        self.btn_connect.setIcon(ui_icons.svg_icon("plus", "#FFFFFF", 16))
        self.btn_cancel_oauth.setIcon(ui_icons.svg_icon("x", danger, 16))
        self.btn_remove.setIcon(ui_icons.svg_icon("trash", danger, 15))
        self.btn_settings.setIcon(ui_icons.svg_icon("settings", accent, 15))
        self.lbl_link_icon.setPixmap(ui_icons.svg_pixmap("link", muted, 18))

        post_icon = "clock" if self.chk_schedule.isChecked() else "send"
        self.btn_check_tokens.setIcon(ui_icons.svg_icon("check", accent, 15))
        self.btn_post.setIcon(ui_icons.svg_icon(post_icon, "#FFFFFF", 16))
        self.btn_queue_run_now.setIcon(ui_icons.svg_icon("send", "#FFFFFF", 15))
        self.btn_queue_edit.setIcon(ui_icons.svg_icon("compose", accent, 15))
        self.btn_queue_retry_failed.setIcon(ui_icons.svg_icon("refresh", accent, 15))
        self.btn_queue_import_csv.setIcon(ui_icons.svg_icon("plus", accent, 15))
        self.btn_queue_cancel.setIcon(ui_icons.svg_icon("trash", danger, 15))
        self.btn_queue_refresh.setIcon(ui_icons.svg_icon("refresh", accent, 15))
        self.btn_history_repost.setIcon(ui_icons.svg_icon("refresh", "#FFFFFF", 15))
        self.btn_history_insights.setIcon(ui_icons.svg_icon("pages", accent, 15))
        self.btn_history_edit_post.setIcon(ui_icons.svg_icon("compose", accent, 15))
        self.btn_history_comment.setIcon(ui_icons.svg_icon("comment", accent, 15))
        self.btn_history_share.setIcon(ui_icons.svg_icon("share", accent, 15))
        self.btn_history_delete_fb.setIcon(ui_icons.svg_icon("trash", danger, 15))
        self.btn_edit_page_post.setIcon(ui_icons.svg_icon("compose", accent, 15))
        self.btn_delete_page_post.setIcon(ui_icons.svg_icon("trash", danger, 15))
        self.btn_open_comments_inbox.setIcon(ui_icons.svg_icon("comment", accent, 15))
        self.btn_send_comments.setIcon(ui_icons.svg_icon("comment", "#FFFFFF", 16))
        self.btn_share_to_pages.setIcon(ui_icons.svg_icon("share", "#FFFFFF", 16))
        self.btn_open_checked_groups.setIcon(ui_icons.svg_icon("share", accent, 15))
        self.btn_open_fb_sharer.setIcon(ui_icons.svg_icon("share", accent, 15))
        self.btn_analytics_refresh.setIcon(ui_icons.svg_icon("refresh", "#FFFFFF", 15))

    def _apply_theme(self, theme: str):
        self.current_theme = theme
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(ui_theme.get_stylesheet(theme))
        self.chk_schedule.set_dark_mode(theme == "dark")
        self.chk_watermark.set_dark_mode(theme == "dark")
        self.chk_video_reel.set_dark_mode(theme == "dark")
        self.chk_analytics_auto_refresh.set_dark_mode(theme == "dark")
        self.btn_theme.set_dark_mode(theme == "dark", animate=self.isVisible())
        self._refresh_icons()

    def _init_system_tray(self):
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(ui_icons.svg_icon("logo", "#2563EB", 32))
        self.tray_icon.setToolTip(f"{APP_NAME} — Facebook Page Studio")

        tray_menu = QMenu(self)
        act_show = QAction(f"เปิดโปรแกรม {APP_NAME}", self)
        act_show.triggered.connect(self._restore_from_tray)
        act_quit = QAction("ออกจากโปรแกรม", self)
        act_quit.triggered.connect(self.quit_application)
        tray_menu.addAction(act_show)
        tray_menu.addSeparator()
        tray_menu.addAction(act_quit)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray_icon.show()

    def _restore_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._restore_from_tray()

    def _notify_tray(self, title: str, message: str):
        if hasattr(self, "tray_icon") and self.tray_icon.isVisible():
            try:
                self.tray_icon.showMessage(title, message, QSystemTrayIcon.Information, 4000)
            except RuntimeError:
                pass

    def quit_application(self):
        self._force_quit = True
        self.close()

    def toggle_theme(self):
        new_theme = "light" if self.current_theme == "dark" else "dark"
        config.save_theme(new_theme)
        self._apply_theme(new_theme)

    def open_settings(self):
        dlg = SettingsDialog(self)
        accepted = dlg.exec() == QDialog.Accepted
        if dlg.db_restored:
            self._token_health_cache.clear()
            self.refresh_accounts()
            self.refresh_page_presets()
            self.refresh_templates()
            self.refresh_scheduled_queue()
            self.refresh_history()
            self.refresh_saved_groups()
            self._load_cached_analytics()
            self._sync_auto_reply_timer()
            self._update_preview()
            self.status.setText("กู้คืนฐานข้อมูลและโหลดข้อมูลใหม่ครบทุกหน้าเรียบร้อยแล้ว")
            return
        if dlg.pages_added_via_token:
            self.refresh_accounts()
            self.status.setText(
                f"เชื่อมต่อด้วย Token สำเร็จ {len(dlg.pages_added_via_token)} เพจ"
            )
        elif accepted:
            mode = (
                "One-Click Backend"
                if config.get_auth_mode() == "backend_oauth"
                else "Facebook App"
            )
            if not dlg.keyring_saved and config.get_fb_app_secret():
                self.status.setText(
                    f"บันทึกการตั้งค่า ({mode}) แล้ว (คำเตือน: OS Keychain ไม่พร้อมใช้งาน จึงเก็บ Secret ลงไฟล์ตั้งค่าแทน)"
                )
            else:
                self.status.setText(f"บันทึกการตั้งค่า ({mode}) เรียบร้อยแล้ว")

    # --- Accounts Management ---

    def refresh_accounts(self):
        prev_checked = {(p, pid) for p, pid, _ in self.checked_targets()}
        self.accounts.blockSignals(True)
        self.accounts.clear()
        for platform, page_id, name in db.list_accounts():
            cached_health = self._token_health_cache.get((platform, page_id))
            if cached_health is not None:
                ok_h, detail_h = cached_health
                disp_name = f"🟢 {name}" if ok_h else f"🔴 {name}"
                tip = f"{name} ({platform}: {page_id}) — {'✅' if ok_h else '❌'} {detail_h}"
            else:
                disp_name = name
                tip = f"{name} ({platform}: {page_id})"
            it = QListWidgetItem(disp_name)
            it.setIcon(ui_icons.page_avatar_icon(name, 28))
            it.setToolTip(tip)
            it.setData(Qt.UserRole, (platform, page_id, name))
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            state = Qt.Checked if (platform, page_id) in prev_checked else Qt.Unchecked
            it.setCheckState(state)
            self.accounts.addItem(it)
        self.accounts.blockSignals(False)
        self._filter_accounts_list(self.input_page_search.text())
        self._sync_comment_pages_combo()
        self._on_accounts_changed()
        if hasattr(self, "tbl_analytics"):
            self._load_cached_analytics()

    def _sync_comment_pages_combo(self):
        prev_data = self.combo_comment_page.currentData()
        self.combo_comment_page.blockSignals(True)
        self.combo_comment_page.clear()
        for platform, page_id, name in db.list_accounts():
            self.combo_comment_page.addItem(name, (platform, page_id, name))
        if prev_data is not None:
            for i in range(self.combo_comment_page.count()):
                if self.combo_comment_page.itemData(i) == prev_data:
                    self.combo_comment_page.setCurrentIndex(i)
                    break
        self.combo_comment_page.blockSignals(False)
        self._on_comment_page_changed()

    def _on_comment_page_changed(self, _index: int = -1):
        data = self.combo_comment_page.currentData()
        cur_pid = data[1] if data else None
        if self._loaded_posts_page_id is not None and cur_pid != self._loaded_posts_page_id:
            self.list_page_posts.clear()
            self._loaded_posts_page_id = None

    def _filter_accounts_list(self, query: str):
        q = (query or "").strip().lower()
        prev_cur = self.accounts.currentItem()
        for i in range(self.accounts.count()):
            it = self.accounts.item(i)
            it.setHidden(bool(q) and q not in it.text().lower())
        cur = self.accounts.currentItem()
        if (prev_cur and prev_cur.isHidden()) or (cur and cur.isHidden()):
            self.accounts.setCurrentRow(-1)
            self.accounts.clearSelection()

    def _set_all_accounts(self, checked: bool):
        self.accounts.blockSignals(True)
        for i in range(self.accounts.count()):
            it = self.accounts.item(i)
            if checked:
                if not it.isHidden():
                    it.setCheckState(Qt.Checked)
            else:
                # Uncheck ALL items (including hidden ones) so hidden pages aren't posted by mistake
                it.setCheckState(Qt.Unchecked)
        self.accounts.blockSignals(False)
        self._on_accounts_changed()

    def checked_targets(self) -> list[tuple[str, str, str]]:
        out = []
        for i in range(self.accounts.count()):
            it = self.accounts.item(i)
            if it.checkState() == Qt.Checked:
                out.append(it.data(Qt.UserRole))
        return out

    def _on_accounts_changed(self):
        total = self.accounts.count()
        sel = len(self.checked_targets())
        self.lbl_selected_count.setText(f"เลือก {sel} / {total}")
        self.stat_pages.set_value(total)
        self.stat_selected.set_value(sel)
        self.btn_share_to_pages.setText(f"  แชร์ลงหน้าเพจที่ติ๊กเลือก ({sel} เพจ)")
        self._update_preview()
        if hasattr(self, "combo_analytics_scope") and self.combo_analytics_scope.currentData() == "selected":
            self._render_analytics_dashboard(self.analytics_rows)

    def remove_selected(self):
        targets = self.checked_targets()
        if not targets:
            cur = self.accounts.currentItem()
            if cur and not cur.isHidden():
                targets = [cur.data(Qt.UserRole)]
        if not targets:
            QMessageBox.information(
                self, "ยังไม่ได้เลือกบัญชี", "ติ๊กเลือกหรือคลิกเพจที่ต้องการลบก่อน"
            )
            return

        names_preview = "\n".join(f"• {name}" for _, _, name in targets)
        reply = QMessageBox.question(
            self,
            "ยืนยันการลบบัญชี",
            f"ต้องการลบบัญชีเพจต่อไปนี้ ({len(targets)} เพจ) ออกจากโปรแกรมใช่หรือไม่?\n\n{names_preview}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        for platform, page_id, _ in targets:
            db.delete_account(platform, page_id)
            _forget_for_platform(platform, page_id)
            self._token_health_cache.pop((platform, page_id), None)
        self.refresh_accounts()
        self.status.setText(f"ลบบัญชีเพจแล้ว {len(targets)} เพจ")

    def _apply_token_health_badges(
        self, health_map: dict[tuple[str, str], tuple[bool, str]]
    ) -> tuple[int, list[str]]:
        """Update cached token health and left-sidebar 🟢/🔴 badges for checked pages."""
        self._token_health_cache.update(health_map)
        ok_count = 0
        bad_lines: list[str] = []
        self.accounts.blockSignals(True)
        for i in range(self.accounts.count()):
            it = self.accounts.item(i)
            platform, page_id, name = it.data(Qt.UserRole)
            if (platform, page_id) not in health_map:
                continue
            ok, detail = health_map[(platform, page_id)]
            if ok:
                ok_count += 1
                it.setText(f"🟢 {name}")
                it.setToolTip(f"{name} ({platform}: {page_id}) — ✅ {detail}")
            else:
                it.setText(f"🔴 {name}")
                it.setToolTip(f"{name} ({platform}: {page_id}) — ❌ {detail}")
                bad_lines.append(f"• {name}: {detail}")
        self.accounts.blockSignals(False)
        return ok_count, bad_lines

    def check_all_tokens_health(self):
        rows = db.list_accounts()
        if not rows:
            QMessageBox.information(
                self, "ยังไม่มีเพจ", "ยังไม่มีเพจที่เชื่อมต่อไว้ในโปรแกรม"
            )
            return

        def job():
            out: dict[tuple[str, str], tuple[bool, str]] = {}
            for platform, page_id, _ in rows:
                if platform == facebook.PLATFORM:
                    out[(platform, page_id)] = facebook.check_page_token_health(page_id)
                else:
                    out[(platform, page_id)] = (False, "ไม่รองรับแพลตฟอร์มนี้")
            return out

        def done(health_map: dict[tuple[str, str], tuple[bool, str]]):
            ok_count, bad_lines = self._apply_token_health_badges(health_map)
            total = len(rows)
            self.status.setText(f"ตรวจสถานะ Token เสร็จสิ้น: ปกติ {ok_count}/{total} เพจ")
            if bad_lines:
                QMessageBox.warning(
                    self,
                    "พบเพจที่ Token มีปัญหาหรือหมดอายุ",
                    f"ใช้งานได้ปกติ {ok_count}/{total} เพจ\nเพจที่ต้องเชื่อมต่อใหม่:\n\n"
                    + "\n".join(bad_lines),
                )

        self._run(job, done, "กำลังตรวจสอบสถานะ Token ของทุกเพจ…")

    def _run_startup_token_check(self):
        """Silently check all connected pages' Token health on startup (dedicated worker, non-blocking)."""
        if self.token_check_worker and self.token_check_worker.isRunning():
            return
        rows = db.list_accounts()
        if not rows:
            return
        cancel_ev = threading.Event()
        self.token_check_cancel_event = cancel_ev

        def job():
            out: dict[tuple[str, str], tuple[bool, str]] = {}
            for platform, page_id, _ in rows:
                if cancel_ev.is_set():
                    break
                if platform == facebook.PLATFORM:
                    out[(platform, page_id)] = facebook.check_page_token_health(page_id)
                else:
                    out[(platform, page_id)] = (False, "ไม่รองรับแพลตฟอร์มนี้")
            return out

        def on_ok(health_map: dict[tuple[str, str], tuple[bool, str]]):
            if not health_map:
                return
            ok_count, bad_lines = self._apply_token_health_badges(health_map)
            total = len(health_map)
            if bad_lines:
                self.status.setText(
                    f"ตรวจ Token อัตโนมัติ: ปกติ {ok_count}/{total} เพจ (พบ {len(bad_lines)} เพจต้องเชื่อมต่อใหม่ 🔴)"
                )
            else:
                self.status.setText(f"ตรวจ Token อัตโนมัติ: ทุกเพจพร้อมใช้งาน ({ok_count}/{total} เพจ 🟢)")

        def on_done():
            if self.token_check_worker is w:
                self.token_check_worker = None

        w = Worker(job)
        self.token_check_worker = w
        w.done.connect(on_ok)
        w.finished.connect(on_done)
        w.start()

    def check_single_page_token_health(self, platform: str, page_id: str, page_name: str):
        """Check Token health for a single page on dedicated worker without blocking posting."""
        if self.token_check_worker and self.token_check_worker.isRunning():
            self.status.setText("กำลังตรวจสอบ Token อยู่แล้ว กรุณารอสักครู่…")
            return
        self.status.setText(f"กำลังตรวจสถานะ Token ของเพจ {page_name}…")

        def job():
            if platform == facebook.PLATFORM:
                res = facebook.check_page_token_health(page_id)
            else:
                res = (False, "ไม่รองรับแพลตฟอร์มนี้")
            return {(platform, page_id): res}

        def on_ok(health_map: dict[tuple[str, str], tuple[bool, str]]):
            ok_count, bad_lines = self._apply_token_health_badges(health_map)
            if ok_count:
                self.status.setText(f"Token เพจ {page_name} ใช้งานได้ปกติ 🟢")
            else:
                detail = bad_lines[0] if bad_lines else page_name
                self.status.setText(f"Token เพจ {page_name} มีปัญหา 🔴")
                QMessageBox.warning(self, "Token เพจมีปัญหา", detail)

        def on_done():
            if self.token_check_worker is w:
                self.token_check_worker = None

        w = Worker(job)
        self.token_check_worker = w
        w.done.connect(on_ok)
        w.finished.connect(on_done)
        w.start()

    def refresh_page_presets(self):
        self.combo_page_presets.blockSignals(True)
        self.combo_page_presets.clear()
        presets = db.list_page_presets()
        self.combo_page_presets.addItem(f"เลือกชุดเพจ ({len(presets)})…", None)
        for p in presets:
            count = len(p.get("page_keys", []))
            self.combo_page_presets.addItem(f"📂 {p['name']} ({count} เพจ)", p)
        self.combo_page_presets.blockSignals(False)

    def apply_selected_page_preset(self, _index: int = -1):
        preset = self.combo_page_presets.currentData()
        if not preset or not isinstance(preset, dict):
            return
        if self.input_page_search.text():
            self.input_page_search.clear()
        keys = set(preset.get("page_keys", []))
        self.accounts.blockSignals(True)
        matched = 0
        for i in range(self.accounts.count()):
            it = self.accounts.item(i)
            pl, pid, _ = it.data(Qt.UserRole)
            if (pl, pid) in keys or f"{pl}:{pid}" in keys or pid in keys:
                it.setCheckState(Qt.Checked)
                matched += 1
            else:
                it.setCheckState(Qt.Unchecked)
        self.accounts.blockSignals(False)
        self._on_accounts_changed()
        self.status.setText(f"เลือกชุดเพจ '{preset['name']}' แล้ว ({matched} เพจ)")

    def save_current_page_preset(self):
        targets = self.checked_targets()
        if not targets:
            QMessageBox.information(
                self, "ยังไม่ได้เลือกเพจ", "กรุณาติ๊กเลือกเพจที่ต้องการจัดชุดอย่างน้อย 1 เพจก่อนบันทึก"
            )
            return
        name, ok = QInputDialog.getText(
            self,
            "บันทึกชุดเพจ (Page Preset)",
            f"ตั้งชื่อชุดเพจ ({len(targets)} เพจที่เลือกไว้):",
        )
        if not ok or not name.strip():
            return
        page_keys = [(pl, pid) for pl, pid, _ in targets]
        db.save_page_preset(name.strip(), page_keys)
        self.refresh_page_presets()
        self.status.setText(f"บันทึกชุดเพจ '{name.strip()}' ({len(targets)} เพจ) เรียบร้อยแล้ว")

    def delete_current_page_preset(self):
        preset = self.combo_page_presets.currentData()
        if not preset or not isinstance(preset, dict):
            QMessageBox.information(
                self, "ยังไม่ได้เลือกชุดเพจ", "กรุณาเลือกชุดเพจจากรายการดรอปดาวน์ก่อนลบ"
            )
            return
        db.delete_page_preset(int(preset["id"]))
        self.refresh_page_presets()
        self.status.setText(f"ลบชุดเพจ '{preset['name']}' แล้ว")

    # --- Composer Helpers ---

    def _schedule_save_draft(self, *_args):
        if self._restoring_draft:
            return
        self._draft_timer.start()

    def _save_draft(self, *_args):
        if self._restoring_draft:
            return
        if self._draft_timer.isActive():
            self._draft_timer.stop()
        draft = {
            "text": self.editor.toPlainText(),
            "link": self.input_link.text(),
            "images": list(self.attached_images),
            "video": self.attached_video,
            "is_reel": bool(self.attached_video and self.chk_video_reel.isChecked()),
            "followup_comments": list(self.follow_up_comments),
            "followup_delay": int(self.spin_followup_delay.value()),
            "watermark_enabled": self.chk_watermark.isChecked(),
            "watermark_text": self.input_watermark_text.text(),
            "place_id": self.input_place_id.text(),
            "place_name": self.input_place_name.text(),
            "page_delay_min": int(self.spin_page_delay_min.value()),
            "page_delay_max": int(self.spin_page_delay_max.value()),
        }
        try:
            config.save_composer_draft(draft)
        except OSError:
            pass

    def _restore_draft(self):
        draft = config.load_composer_draft()
        if not draft or not isinstance(draft, dict):
            return
        self._restoring_draft = True
        try:
            if draft.get("text"):
                self.editor.setPlainText(str(draft["text"]))
            if draft.get("link"):
                self.input_link.setText(str(draft["link"]))
            wm_text = str(draft.get("watermark_text") or "")
            if wm_text:
                self.input_watermark_text.setText(wm_text)
            if draft.get("watermark_enabled"):
                self.chk_watermark.setChecked(True)
            if "page_delay_min" in draft or "page_delay_max" in draft:
                try:
                    dmin = max(0, min(int(draft.get("page_delay_min") or 0), 300))
                    dmax = max(0, min(int(draft.get("page_delay_max") or 0), 300))
                    self._syncing_stagger = True
                    self.spin_page_delay_min.setValue(dmin)
                    self.spin_page_delay_max.setValue(dmax)
                    self._syncing_stagger = False
                    self._sync_stagger_combo_from_spins()
                except (TypeError, ValueError):
                    self._syncing_stagger = False
            self._set_composer_context(
                place_id=str(draft.get("place_id") or ""),
                place_name=str(draft.get("place_name") or ""),
            )
            vid = str(draft.get("video") or "")
            if vid and Path(vid).is_file():
                self.chk_video_reel.setChecked(bool(draft.get("is_reel")))
                self.set_attached_video(vid)
            else:
                self.chk_video_reel.setChecked(False)
                imgs = [
                    str(p)
                    for p in (draft.get("images") or [])
                    if p and Path(str(p)).is_file()
                ]
                if imgs:
                    self.attached_images = imgs
                    self._refresh_image_list()
            if "followup_delay" in draft:
                try:
                    self.spin_followup_delay.setValue(
                        max(0, min(int(draft.get("followup_delay") or 2), 60))
                    )
                except (TypeError, ValueError):
                    pass
            f_comments = [
                str(c).strip()
                for c in (draft.get("followup_comments") or [])
                if str(c).strip()
            ]
            if f_comments:
                self.follow_up_comments = f_comments
                self._refresh_followup_list()
        finally:
            self._restoring_draft = False

    def _set_composer_context(
        self,
        *,
        place_id: str = "",
        place_name: str = "",
        og_action_type_id: str = "",
        og_object_id: str = "",
        og_label: str = "",
    ):
        _ = (og_action_type_id, og_object_id, og_label)
        self.input_place_id.setText((place_id or "").strip())
        self.input_place_name.setText((place_name or "").strip())
        self._schedule_save_draft()
        self._update_preview()

    def _active_fb_page_id(self) -> str:
        targets = self.checked_targets()
        for pl, pid, _name in targets:
            if pl == facebook.PLATFORM:
                return pid
        for pl, pid, _name in db.list_accounts():
            if pl == facebook.PLATFORM:
                return pid
        return ""

    def _open_place_picker(self):
        dlg = PlaceActivityPickerDialog(
            kind="place",
            page_id=self._active_fb_page_id(),
            parent=self,
        )
        if dlg.exec() == QDialog.Accepted and dlg.selected_id:
            self.input_place_id.setText(dlg.selected_id)
            self.input_place_name.setText(dlg.selected_name or dlg.selected_id)

    def _clear_place(self):
        self.input_place_id.clear()
        self.input_place_name.clear()

    def _extract_composer_context(self) -> dict[str, str]:
        raw_place = self.input_place_id.text().strip()
        place_id = facebook.extract_place_or_page_id(raw_place) if raw_place else ""
        place_name = self.input_place_name.text().strip() or place_id
        return {
            "place_id": place_id,
            "place_name": place_name if place_id else "",
            "og_action_type_id": "",
            "og_object_id": "",
            "og_label": "",
        }

    def _on_watermark_toggled(self, checked: bool):
        self.input_watermark_text.setEnabled(checked)
        self._save_draft()

    def _on_stagger_preset_changed(self, _index: int = -1):
        if self._syncing_stagger:
            return
        val = self.combo_stagger_preset.currentData()
        if isinstance(val, tuple) and len(val) == 2:
            self._syncing_stagger = True
            try:
                self.spin_page_delay_min.setValue(int(val[0]))
                self.spin_page_delay_max.setValue(int(val[1]))
            finally:
                self._syncing_stagger = False
            self._schedule_save_draft()

    def _sync_stagger_combo_from_spins(self):
        pair = (int(self.spin_page_delay_min.value()), int(self.spin_page_delay_max.value()))
        matched_idx = self.combo_stagger_preset.count() - 1
        for i in range(self.combo_stagger_preset.count()):
            if self.combo_stagger_preset.itemData(i) == pair:
                matched_idx = i
                break
        self._syncing_stagger = True
        try:
            self.combo_stagger_preset.setCurrentIndex(matched_idx)
        finally:
            self._syncing_stagger = False

    def _on_stagger_spins_changed(self, _val: int = 0):
        if self._syncing_stagger:
            return
        self._sync_stagger_combo_from_spins()
        self._schedule_save_draft()

    def _on_schedule_mode_changed(self, _index: int = -1):
        is_local = (
            self.chk_schedule.isChecked()
            and self.combo_schedule_mode.currentData() == "local_queue"
        )
        self.combo_repeat_rule.setEnabled(is_local)
        self._update_preview()

    def _on_composer_changed(self):
        length = len(self.editor.toPlainText())
        if length > FB_MAX_CHARS:
            self.lbl_char_count.setText(
                f"{length:,} / {FB_MAX_CHARS:,} ตัวอักษร (เกินขีดจำกัด)"
            )
            self.lbl_char_count.setStyleSheet("color: #ef4444; font-weight: 700;")
        else:
            self.lbl_char_count.setText(f"{length:,} ตัวอักษร")
            self.lbl_char_count.setStyleSheet("")
        self._schedule_save_draft()
        self._update_preview()

    def _on_schedule_toggled(self, checked: bool):
        self.dt_schedule.setEnabled(checked)
        self.combo_schedule_mode.setEnabled(checked)
        self.combo_repeat_rule.setEnabled(
            checked and self.combo_schedule_mode.currentData() == "local_queue"
        )
        self.btn_post.setText(
            "  ตั้งเวลาโพสต์ไปยังเพจที่เลือก" if checked else "  โพสต์ไปยังเพจที่เลือก"
        )
        if checked and self.dt_schedule.dateTime() <= QDateTime.currentDateTime():
            self.dt_schedule.setDateTime(QDateTime.currentDateTime().addSecs(30 * 60))
        self._refresh_icons()
        self._update_preview()

    def _update_preview(self):
        targets = self.checked_targets()
        page_names = [name for _, _, name in targets]
        schedule_label = ""
        if self.chk_schedule.isChecked():
            dt_str = self.dt_schedule.dateTime().toString("yyyy-MM-dd HH:mm")
            mode = (
                "FB Native"
                if self.combo_schedule_mode.currentData() == "fb_native"
                else "คิวในเครื่อง"
            )
            schedule_label = f"{dt_str} ({mode})"

        place_disp = (
            self.input_place_name.text().strip()
            or self.input_place_id.text().strip()
        )
        self.post_preview.update_preview(
            page_names=page_names,
            text=self.editor.toPlainText(),
            link=self.input_link.text(),
            image_paths=self.attached_images,
            video_path=self.attached_video,
            is_reel=bool(self.attached_video and self.chk_video_reel.isChecked()),
            schedule_label=schedule_label,
            place_label=place_disp,
            activity_label="",
        )

    def refresh_templates(self):
        self.combo_templates.clear()
        tpls = db.list_templates()
        if not tpls:
            self.combo_templates.addItem("ยังไม่มีเทมเพลต (กด 'จัดการเทมเพลต' เพื่อเพิ่ม)", None)
            self.btn_insert_template.setEnabled(False)
            return
        for t in tpls:
            tag = "🏷" if t["kind"] == "hashtag" else "📝"
            self.combo_templates.addItem(f"{tag} {t['name']}", t["content"])
        self.btn_insert_template.setEnabled(True)

    def insert_selected_template(self):
        content = self.combo_templates.currentData()
        if not content:
            return
        cur = self.editor.toPlainText()
        if cur and not cur.endswith(("\n", " ")):
            sep = "\n\n"
        else:
            sep = ""
        self.editor.setPlainText(f"{cur}{sep}{content}")
        self.status.setText("แทรกเทมเพลตลงในข้อความแล้ว")

    def open_templates_dialog(self):
        dlg = TemplatesDialog(initial_text=self.editor.toPlainText().strip(), parent=self)
        dlg.exec()
        self.refresh_templates()

    def _snapshot_composer_for_preset(self) -> dict:
        try:
            ctx = self._extract_composer_context()
            place_id = ctx["place_id"]
            place_name = ctx["place_name"]
        except ValueError:
            place_id = self.input_place_id.text().strip()
            place_name = self.input_place_name.text().strip()
        return {
            "message": self.editor.toPlainText().strip(),
            "link": self.input_link.text().strip(),
            "image_paths": list(self.attached_images),
            "video_path": self.attached_video.strip(),
            "is_reel": bool(self.attached_video and self.chk_video_reel.isChecked()),
            "watermark_enabled": self.chk_watermark.isChecked(),
            "watermark_text": self.input_watermark_text.text().strip(),
            "place_id": place_id,
            "place_name": place_name,
            "comments": list(self.follow_up_comments),
            "comment_delay": float(self.spin_followup_delay.value()),
        }

    def open_post_presets_dialog(self):
        dlg = PostPresetsDialog(
            composer_snapshot=self._snapshot_composer_for_preset(),
            parent=self,
        )
        if dlg.exec() == QDialog.Accepted and dlg.selected_preset:
            self.apply_post_preset(dlg.selected_preset)

    def apply_post_preset(self, preset: dict):
        missing_files: list[str] = []
        self._restoring_draft = True
        try:
            self.editor.setPlainText(str(preset.get("message") or ""))
            self.input_link.setText(str(preset.get("link") or ""))
            wm_text = str(preset.get("watermark_text") or "")
            self.input_watermark_text.setText(wm_text)
            self.chk_watermark.setChecked(bool(preset.get("watermark_enabled")))
            self._set_composer_context(
                place_id=str(preset.get("place_id") or ""),
                place_name=str(preset.get("place_name") or ""),
            )

            vid = str(preset.get("video_path") or "").strip()
            if vid:
                self.clear_images()
                if Path(vid).is_file():
                    self.chk_video_reel.setChecked(bool(preset.get("is_reel")))
                    self.set_attached_video(vid)
                else:
                    missing_files.append(vid)
                    self.clear_video()
            else:
                self.clear_video()
                raw_imgs = [str(x).strip() for x in (preset.get("image_paths") or []) if str(x).strip()]
                valid_imgs: list[str] = []
                for p in raw_imgs:
                    if Path(p).is_file():
                        valid_imgs.append(p)
                    else:
                        missing_files.append(p)
                self.attached_images = valid_imgs
                self._refresh_image_list()

            valid_comments: list[str] = []
            for spec in preset.get("comments") or []:
                c_txt, c_img = facebook.split_comment_spec(str(spec))
                if c_img and not Path(c_img).is_file():
                    missing_files.append(c_img)
                    c_img = ""
                if c_txt or c_img:
                    valid_comments.append(facebook.format_comment_spec(c_txt, c_img))
            self.follow_up_comments = valid_comments
            try:
                self.spin_followup_delay.setValue(
                    max(0, min(int(round(float(preset.get("comment_delay", 2.0)))), 60))
                )
            except (TypeError, ValueError):
                self.spin_followup_delay.setValue(2)
            self._refresh_followup_list()
        finally:
            self._restoring_draft = False

        self._save_draft()
        self._update_preview()
        name = str(preset.get("name") or "")
        if missing_files:
            self.status.setText(
                f"โหลดชุดโพสต์ '{name}' แล้ว (ไม่พบไฟล์สื่อเดิม {len(missing_files)} ไฟล์ในเครื่อง)"
            )
            if self.isVisible():
                QMessageBox.warning(
                    self,
                    "บางไฟล์สื่อในชุดโพสต์ถูกย้ายหรือลบไปแล้ว",
                    "โหลดข้อความและตัวเลือกของชุดโพสต์แล้ว แต่ไม่พบไฟล์ต่อไปนี้ในเครื่อง:\n"
                    + "\n".join(missing_files[:8]),
                )
        else:
            self.status.setText(f"โหลดชุดโพสต์ '{name}' เข้าสู่หน้าเขียนโพสต์เรียบร้อยแล้ว")

    def select_video(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "เลือกไฟล์วิดีโอสำหรับโพสต์", "", VIDEO_FILTER
        )
        if not path:
            return
        self.set_attached_video(path)

    def _refresh_attached_video_label(self):
        if not self.attached_video:
            self.lbl_attached_video.setText("")
            return
        vp = Path(self.attached_video)
        size_str = ""
        if vp.is_file():
            size_mb = vp.stat().st_size / (1024 * 1024)
            size_str = f" ({size_mb:.1f} MB)"
        prefix = "🎬 Reels:" if self.chk_video_reel.isChecked() else "🎬 วิดีโอ:"
        self.lbl_attached_video.setText(f"{prefix} {vp.name}{size_str}")

    def _on_video_reel_toggled(self, _checked: bool = False):
        self._refresh_attached_video_label()
        self._schedule_save_draft()
        self._update_preview()

    def set_attached_video(self, path: str):
        had_images = bool(self.attached_images)
        self.attached_video = path
        if path and self.attached_images:
            self.attached_images.clear()
            self.drop_zone.set_images([])
            self.btn_clear_images.setVisible(False)
        if path:
            self._refresh_attached_video_label()
            self.video_chip.setVisible(True)
            self.drop_zone.setVisible(False)
            if had_images and hasattr(self, "status"):
                self.status.setText(
                    "สลับเป็นโหมดวิดีโอแล้ว (ล้างรูปเดิมออก — Facebook API ไม่รองรับการผสมรูปภาพ+วิดีโอในโพสต์เดียวกัน)"
                )
        else:
            self.lbl_attached_video.setText("")
            self.video_chip.setVisible(False)
            self.drop_zone.setVisible(True)
        self._save_draft()
        self._update_preview()

    def clear_video(self):
        self.chk_video_reel.setChecked(False)
        self.set_attached_video("")

    def add_images(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "เลือกรูปภาพสำหรับแนบโพสต์", "", IMAGE_FILTER
        )
        if paths:
            self._add_image_paths(paths)

    def _add_image_paths(self, paths: list[str]):
        had_video = bool(self.attached_video)
        if paths and self.attached_video:
            self.clear_video()
        existing_norm = {
            os.path.normcase(os.path.normpath(x)) for x in self.attached_images
        }
        added = 0
        for p in paths:
            norm = os.path.normcase(os.path.normpath(p))
            if norm not in existing_norm:
                self.attached_images.append(p)
                existing_norm.add(norm)
                added += 1
        if added:
            self._refresh_image_list()
            if had_video and hasattr(self, "status"):
                self.status.setText(
                    "สลับเป็นโหมดรูปภาพแล้ว (ล้างวิดีโอเดิมออก — Facebook API ไม่รองรับการผสมรูปภาพ+วิดีโอในโพสต์เดียวกัน)"
                )

    def _remove_image_path(self, path: str):
        if path in self.attached_images:
            self.attached_images.remove(path)
            self._refresh_image_list()

    def clear_images(self):
        self.attached_images.clear()
        self._refresh_image_list()

    def _refresh_image_list(self):
        self.drop_zone.set_images(self.attached_images)
        self.btn_clear_images.setVisible(bool(self.attached_images))
        self._save_draft()
        self._update_preview()

    # --- Worker Orchestration ---

    def _set_busy(self, busy: bool, *, is_oauth: bool = False):
        widgets = (
            self.btn_connect,
            self.btn_remove,
            self.btn_select_all,
            self.btn_select_none,
            self.combo_page_presets,
            self.btn_save_page_preset,
            self.btn_delete_page_preset,
            self.btn_check_tokens,
            self.btn_settings,
            self.btn_post,
            self.combo_stagger_preset,
            self.spin_page_delay_min,
            self.spin_page_delay_max,
            self.btn_add_video,
            self.btn_clear_video,
            self.combo_templates,
            self.btn_manage_templates,
            self.btn_clear_images,
            self.drop_zone,
            self.chk_watermark,
            self.input_place_id,
            self.input_place_name,
            self.btn_pick_place,
            self.btn_clear_place,
            self.editor,
            self.input_link,
            self.accounts,
            self.chk_schedule,
            self.btn_queue_run_now,
            self.btn_queue_edit,
            self.btn_queue_retry_failed,
            self.btn_queue_import_csv,
            self.btn_queue_cancel,
            self.btn_queue_refresh,
            self.btn_history_repost,
            self.btn_history_select_batch,
            self.btn_history_insights,
            self.btn_history_edit_post,
            self.btn_history_comment,
            self.btn_history_share,
            self.btn_history_delete,
            self.btn_history_delete_fb,
            self.btn_history_clear,
            self.input_followup_comment,
            self.btn_add_followup,
            self.btn_remove_followup,
            self.btn_clear_followup,
            self.spin_followup_delay,
            self.combo_comment_page,
            self.btn_load_page_posts,
            self.list_page_posts,
            self.btn_edit_page_post,
            self.btn_delete_page_post,
            self.input_comment_post_id,
            self.btn_resolve_post_url,
            self.btn_open_comments_inbox,
            self.editor_comment_input,
            self.btn_attach_comment_image,
            self.btn_clear_comment_image,
            self.btn_add_single_comment,
            self.btn_add_lines_comments,
            self.btn_comment_up,
            self.btn_comment_down,
            self.btn_comment_del,
            self.btn_comment_clear,
            self.list_comments,
            self.spin_comment_delay,
            self.btn_send_comments,
            self.btn_quick_comment_browser,
            self.btn_copy_next_comment,
            self.input_group_name,
            self.input_group_url,
            self.btn_add_group,
            self.list_saved_groups,
            self.btn_groups_select_all,
            self.btn_groups_select_none,
            self.btn_groups_delete,
            self.btn_share_load_latest,
            self.input_share_url,
            self.editor_share_caption,
            self.btn_share_to_pages,
            self.btn_open_checked_groups,
            self.btn_open_fb_sharer,
            self.btn_copy_share_text,
            *self._filter_pills.values(),
        )
        for w in widgets:
            w.setEnabled(not busy)
        self.btn_insert_template.setEnabled(
            (not busy) and (self.combo_templates.currentData() is not None)
        )
        self.input_watermark_text.setEnabled(
            (not busy) and self.chk_watermark.isChecked()
        )
        self.dt_schedule.setEnabled((not busy) and self.chk_schedule.isChecked())
        self.combo_schedule_mode.setEnabled(
            (not busy) and self.chk_schedule.isChecked()
        )
        self.combo_repeat_rule.setEnabled(
            (not busy)
            and self.chk_schedule.isChecked()
            and self.combo_schedule_mode.currentData() == "local_queue"
        )
        self.btn_cancel_oauth.setVisible(busy and is_oauth)
        self.btn_cancel_oauth.setEnabled(busy and is_oauth)

    def _run(
        self,
        fn,
        on_done,
        busy_msg: str,
        *,
        is_oauth: bool = False,
        on_failed=None,
    ):
        if self.worker and self.worker.isRunning():
            return
        self.status.setText(busy_msg)
        self._set_busy(True, is_oauth=is_oauth)
        self.worker = Worker(fn)
        self.worker.done.connect(on_done)
        if on_failed is not None:
            self.worker.failed.connect(on_failed)
        self.worker.failed.connect(self._failed)
        self.worker.finished.connect(self._unlock)
        self.worker.start()

    def _unlock(self):
        self.oauth_cancel_event = None
        self.task_cancel_event = None
        self._set_busy(False, is_oauth=False)

    def _failed(self, msg: str):
        self.status.setText("เกิดข้อผิดพลาด")
        if "ยกเลิกการเชื่อมต่อ" in msg:
            self.status.setText(msg)
            return
        if not self.isVisible():
            self._notify_tray(f"{APP_NAME} — เกิดข้อผิดพลาด", msg[:180])
            return
        QMessageBox.warning(self, "ผิดพลาด", msg)

    # --- OAuth Connect & Cancel ---

    def _is_auth_configured(self) -> bool:
        if config.get_auth_mode() == "backend_oauth":
            return bool(config.get_backend_url())
        return bool(config.get_fb_app_id() and config.get_fb_app_secret())

    def connect_fb(self):
        if not self._is_auth_configured():
            reply = QMessageBox.question(
                self,
                "ยังไม่ได้ตั้งค่าการเชื่อมต่อ",
                "ยังไม่ได้ตั้งค่า Facebook App หรือ Backend Server ต้องการเปิดหน้าตั้งค่า (Setup Wizard) ตอนนี้หรือไม่?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if reply == QMessageBox.Yes:
                self.open_settings()
            if not self._is_auth_configured():
                return

        cancel_ev = threading.Event()
        self.oauth_cancel_event = cancel_ev

        def done(pages):
            for pid, name in pages:
                db.save_account(facebook.PLATFORM, pid, name)
            self.refresh_accounts()
            self.status.setText(f"เชื่อมต่อแล้ว {len(pages)} เพจ")

        self._run(
            lambda: facebook.connect(cancel_event=cancel_ev),
            done,
            "กำลังรอล็อกอินในเบราว์เซอร์… (กด 'ยกเลิกการเชื่อมต่อ' ได้ทุกเมื่อ)",
            is_oauth=True,
        )

    def cancel_oauth(self):
        if self.oauth_cancel_event:
            self.oauth_cancel_event.set()
            self.btn_cancel_oauth.setEnabled(False)
            self.status.setText("กำลังยกเลิกการเชื่อมต่อ…")

    # --- Posting & Scheduling ---

    def post_selected(self):
        text = self.editor.toPlainText().strip()
        link = self.input_link.text().strip()
        images = list(self.attached_images)
        video = self.attached_video.strip()
        is_reel = bool(video and self.chk_video_reel.isChecked())
        followup_comments = list(self.follow_up_comments)
        followup_delay = float(self.spin_followup_delay.value())
        targets = self.checked_targets()

        if not targets:
            QMessageBox.information(
                self, "ยังไม่ได้เลือกเพจ", "กรุณาติ๊กเลือกเพจทางซ้ายอย่างน้อย 1 เพจ"
            )
            return
        try:
            post_ctx = self._extract_composer_context()
        except ValueError as exc:
            QMessageBox.warning(self, "ข้อมูลเช็คอินไม่ถูกต้อง", str(exc))
            return
        place_id = post_ctx["place_id"]
        place_name = post_ctx["place_name"]
        if is_reel and place_id:
            QMessageBox.warning(
                self,
                "Reels ไม่รองรับเช็คอิน",
                "Facebook Reels API ไม่รองรับการแนบสถานที่เช็คอิน กรุณาล้างช่องเช็คอินหรือปิดสวิตช์ 'ส่งเป็น Reels'",
            )
            return

        if (
            not text
            and not link
            and not images
            and not video
            and not place_id
        ):
            QMessageBox.information(
                self,
                "ข้อมูลไม่ครบ",
                "กรุณาพิมพ์ข้อความ ใส่ลิงก์ แนบรูปภาพ แนบวิดีโอ หรือเช็คอินอย่างน้อย 1 อย่าง",
            )
            return
        if len(text) > FB_MAX_CHARS:
            QMessageBox.warning(
                self,
                "ข้อความยาวเกินไป",
                f"ข้อความต้องไม่เกิน {FB_MAX_CHARS:,} ตัวอักษร",
            )
            return
        if not _is_valid_http_url(link):
            QMessageBox.warning(
                self, "ลิงก์ไม่ถูกต้อง", "ลิงก์ต้องขึ้นต้นด้วย http:// หรือ https://"
            )
            return

        missing = [p for p in images if not Path(p).is_file()]
        if missing:
            QMessageBox.warning(
                self,
                "ไม่พบไฟล์รูปภาพ",
                "ไฟล์รูปภาพต่อไปนี้ไม่พบในเครื่อง:\n" + "\n".join(missing),
            )
            return
        if video and not Path(video).is_file():
            QMessageBox.warning(self, "ไม่พบไฟล์วิดีโอ", f"ไม่พบไฟล์วิดีโอ: {video}")
            return
        if is_reel and video and Path(video).stat().st_size <= 0:
            QMessageBox.warning(
                self,
                "ไฟล์วิดีโอว่างเปล่า",
                "ไฟล์วิดีโอมีขนาด 0 bytes ไม่สามารถอัปโหลดเป็น Facebook Reels ได้",
            )
            return

        scheduled_dt: datetime | None = None
        if self.chk_schedule.isChecked():
            scheduled_dt = self.dt_schedule.dateTime().toPython().astimezone()
            mode = self.combo_schedule_mode.currentData()
            if mode == "local_queue":
                if scheduled_dt <= datetime.now().astimezone():
                    QMessageBox.warning(
                        self, "เวลาไม่ถูกต้อง", "กรุณาเลือกเวลาตั้งโพสต์ในอนาคต"
                    )
                    return
                target_dicts = [
                    {"platform": pl, "page_id": pid, "name": name}
                    for pl, pid, name in targets
                ]
                repeat_rule = str(self.combo_repeat_rule.currentData() or "none")
                wm_text = (
                    self.input_watermark_text.text().strip()
                    if self.chk_watermark.isChecked()
                    else ""
                )
                db.add_scheduled_job(
                    run_at_iso=scheduled_dt.isoformat(timespec="seconds"),
                    message=text,
                    link=link,
                    image_paths=images,
                    video_path=video,
                    is_reel=is_reel,
                    targets=target_dicts,
                    comments=followup_comments,
                    comment_delay=followup_delay,
                    repeat_rule=repeat_rule,
                    watermark_text=wm_text,
                    place_id=place_id,
                    place_name=place_name,
                )
                self._clear_composer()
                self.refresh_scheduled_queue()
                self.status.setText(
                    f"เพิ่มเข้าคิวตั้งเวลาในเครื่องแล้ว ({scheduled_dt.strftime('%Y-%m-%d %H:%M')})"
                )
                return

            if followup_comments:
                QMessageBox.warning(
                    self,
                    "ไม่รองรับคอมเมนต์อัตโนมัติในโหมด Facebook Native",
                    "Facebook ไม่อนุญาตให้คอมเมนต์ล่วงหน้าบนโพสต์ที่ยังไม่เผยแพร่\n"
                    "หากต้องการให้คอมเมนต์ต่อท้ายอัตโนมัติ กรุณาเปลี่ยนโหมดเป็น 'คิวในเครื่อง' หรือเอาคอมเมนต์ต่อท้ายออกก่อน",
                )
                return

            try:
                max_stagger_sec = max(
                    int(self.spin_page_delay_min.value()),
                    int(self.spin_page_delay_max.value()),
                ) * max(0, len(targets) - 1)
                last_page_dt = scheduled_dt + timedelta(seconds=max_stagger_sec)
                if is_reel:
                    facebook.validate_fb_reel_schedule_time(scheduled_dt)
                    if max_stagger_sec > 0:
                        facebook.validate_fb_reel_schedule_time(last_page_dt)
                else:
                    facebook.validate_fb_schedule_time(scheduled_dt)
                    if max_stagger_sec > 0:
                        facebook.validate_fb_schedule_time(last_page_dt)
            except ValueError as exc:
                QMessageBox.warning(self, "เวลาตั้งโพสต์ไม่ถูกต้อง", str(exc))
                return

        wm_text = (
            self.input_watermark_text.text().strip()
            if self.chk_watermark.isChecked()
            else ""
        )
        self._execute_post_batch(
            targets=targets,
            message=text,
            link=link,
            images=images,
            video_path=video,
            is_reel=is_reel,
            scheduled_dt=scheduled_dt,
            comments=followup_comments,
            comment_delay=followup_delay,
            page_delay_min=float(self.spin_page_delay_min.value()),
            page_delay_max=float(self.spin_page_delay_max.value()),
            watermark_text=wm_text,
            place_id=place_id,
            place_name=place_name,
            clear_composer_on_success=True,
        )

    def _clear_composer(self):
        self._restoring_draft = True
        try:
            self.editor.clear()
            self.input_link.clear()
            self.clear_images()
            self.clear_video()
            self.clear_followup_comments()
            self._clear_place()
            self.chk_schedule.setChecked(False)
            self.combo_repeat_rule.setCurrentIndex(0)
        finally:
            self._restoring_draft = False
        self._save_draft()

    def _execute_post_batch(
        self,
        *,
        targets: list[tuple[str, str, str]],
        message: str,
        link: str,
        images: list[str],
        video_path: str = "",
        is_reel: bool = False,
        scheduled_dt: datetime | None,
        comments: list[str] | None = None,
        comment_delay: float = 2.0,
        page_delay_min: float = 0.0,
        page_delay_max: float = 0.0,
        watermark_text: str = "",
        place_id: str = "",
        place_name: str = "",
        og_action_type_id: str = "",
        og_object_id: str = "",
        og_label: str = "",
        clear_composer_on_success: bool,
        on_complete=None,
        on_error=None,
    ):
        sched_iso = scheduled_dt.isoformat(timespec="seconds") if scheduled_dt else ""
        success_status = "scheduled" if scheduled_dt else "published"
        clean_comments = [
            c for c in (comments or []) if c and any(facebook.split_comment_spec(c))
        ]
        d_low = max(0.0, min(float(page_delay_min), float(page_delay_max)))
        d_high = max(0.0, max(float(page_delay_min), float(page_delay_max)))
        wm_template = (watermark_text or "").strip()
        eff_place_id = place_id
        eff_place_name = place_name
        eff_action_id = og_action_type_id
        eff_object_id = og_object_id
        eff_og_label = og_label
        batch_id = uuid.uuid4().hex[:12]
        cancel_ev = threading.Event()
        self.task_cancel_event = cancel_ev

        def job():
            results = []
            stagger_offset_sec = 0.0
            for idx, (platform, page_id, page_name) in enumerate(targets):
                if cancel_ev.is_set():
                    for _, _, rem_name in targets[idx:]:
                        results.append((rem_name, "", "ถูกยกเลิกกลางคัน"))
                    break
                # For FB Native scheduled multi-page posts with anti-spam stagger,
                # offset each subsequent page's scheduled_publish_time instead of sleeping!
                page_sched_dt = scheduled_dt
                if scheduled_dt is not None and idx > 0 and d_high > 0:
                    step_sec = random.uniform(d_low, d_high) if d_high > d_low else d_high
                    stagger_offset_sec += step_sec
                    page_sched_dt = scheduled_dt + timedelta(seconds=int(round(stagger_offset_sec)))
                page_sched_iso = (
                    page_sched_dt.isoformat(timespec="seconds") if page_sched_dt else sched_iso
                )
                rendered_msg = facebook.render_spintax_and_variables(
                    message, page_name=page_name
                )
                rendered_comments = []
                for c in clean_comments:
                    ct, ci = facebook.split_comment_spec(c)
                    rt = facebook.render_spintax_and_variables(ct, page_name=page_name) if ct else ""
                    rendered_comments.append(facebook.format_comment_spec(rt, ci))
                temp_wm_files: list[str] = []
                upload_images = list(images)
                try:
                    if wm_template and upload_images:
                        wm_text = facebook.render_spintax_and_variables(
                            wm_template, page_name=page_name
                        )
                        processed_imgs: list[str] = []
                        for img_path in upload_images:
                            out_p, is_tmp = apply_text_watermark(img_path, wm_text)
                            processed_imgs.append(out_p)
                            if is_tmp:
                                temp_wm_files.append(out_p)
                        upload_images = processed_imgs

                    post_id = _post_for_platform(
                        platform,
                        page_id,
                        rendered_msg,
                        link=link,
                        image_paths=upload_images,
                        video_path=video_path,
                        is_reel=is_reel,
                        scheduled_time=page_sched_dt,
                        place_id=place_id,
                        og_action_type_id=og_action_type_id,
                        og_object_id=og_object_id,
                    )
                    comment_errs = []
                    if rendered_comments and not page_sched_dt and platform == facebook.PLATFORM:
                        c_res = facebook.post_multiple_comments(
                            page_id,
                            post_id,
                            rendered_comments,
                            delay_seconds=comment_delay,
                            cancel_event=cancel_ev,
                        )
                        comment_errs = [
                            f"คอมเมนต์ #{c_idx}: {err}"
                            for c_idx, _, _, err in c_res
                            if err
                        ]

                    try:
                        db.add_post_history(
                            platform=platform,
                            page_id=page_id,
                            page_name=page_name,
                            message=rendered_msg,
                            link=link,
                            image_paths=images,
                            video_path=video_path,
                            is_reel=is_reel,
                            scheduled_for=page_sched_iso,
                            status=success_status,
                            remote_post_id=post_id,
                            error="; ".join(comment_errs),
                            batch_id=batch_id,
                            place_id=eff_place_id,
                            place_name=eff_place_name,
                            og_action_type_id=eff_action_id,
                            og_object_id=eff_object_id,
                            og_label=eff_og_label,
                        )
                    except (sqlite3.Error, OSError):
                        pass
                    err_summary = "; ".join(comment_errs) if comment_errs else None
                    results.append((page_name, post_id, err_summary))
                except Exception as exc:  # noqa: BLE001
                    err_msg = str(exc)
                    try:
                        db.add_post_history(
                            platform=platform,
                            page_id=page_id,
                            page_name=page_name,
                            message=rendered_msg,
                            link=link,
                            image_paths=images,
                            video_path=video_path,
                            is_reel=is_reel,
                            scheduled_for=page_sched_iso,
                            status="failed",
                            error=err_msg,
                            batch_id=batch_id,
                            place_id=eff_place_id,
                            place_name=eff_place_name,
                            og_action_type_id=eff_action_id,
                            og_object_id=eff_object_id,
                            og_label=eff_og_label,
                        )
                    except (sqlite3.Error, OSError):
                        pass
                    results.append((page_name, "", err_msg))
                finally:
                    for tmp_p in temp_wm_files:
                        try:
                            Path(tmp_p).unlink(missing_ok=True)
                        except OSError:
                            pass

                if (
                    scheduled_dt is None
                    and idx < len(targets) - 1
                    and d_high > 0
                    and not cancel_ev.is_set()
                ):
                    wait_sec = random.uniform(d_low, d_high) if d_high > d_low else d_high
                    cancel_ev.wait(wait_sec)
            return results

        def done(results):
            ok = sum(1 for _, _, e in results if e is None)
            errs = [f"{name}: {e}" for name, _, e in results if e]
            action = "ตั้งเวลาสำเร็จ" if scheduled_dt else "โพสต์สำเร็จ"
            if clean_comments and not scheduled_dt:
                action = f"โพสต์ + คอมเมนต์ {len(clean_comments)} อัน สำเร็จ"
            self.status.setText(f"{action} {ok}/{len(results)} เพจ")
            self.refresh_history()
            if on_complete:
                on_complete(results, errs)
            if errs:
                if self.isVisible():
                    QMessageBox.warning(self, "บางรายการไม่สำเร็จ", "\n".join(errs))
                elif not on_complete:
                    self._notify_tray(f"{APP_NAME} — บางรายการไม่สำเร็จ", errs[0][:180])
            elif clear_composer_on_success:
                self._clear_composer()

        busy_msg = (
            "กำลังตั้งเวลาโพสต์…"
            if scheduled_dt
            else (
                f"กำลังโพสต์และคอมเมนต์ต่อท้าย ({len(clean_comments)} อัน)…"
                if clean_comments
                else "กำลังโพสต์ไปยังเพจ…"
            )
        )
        self._run(job, done, busy_msg, on_failed=on_error)

    # --- Local Scheduled Queue ---

    def refresh_scheduled_queue(self):
        jobs = db.list_scheduled_jobs(include_done=False)
        pending_count = sum(1 for j in jobs if j["status"] in ("pending", "running"))
        self.stat_queued.set_value(pending_count)
        badge = f"  คิวตั้งเวลา ({pending_count})" if pending_count else "  คิวตั้งเวลา"
        self.btn_nav_queue.setText(badge)

        self.tbl_queue.setRowCount(len(jobs))
        for r, job in enumerate(jobs):
            id_item = QTableWidgetItem(str(job["id"]))
            id_item.setData(Qt.UserRole, job["id"])
            try:
                dt_display = datetime.fromisoformat(job["run_at"]).strftime(
                    "%Y-%m-%d %H:%M"
                )
            except ValueError:
                dt_display = job["run_at"]
            rep = job.get("repeat_rule", "none")
            if rep == "daily":
                dt_display = f"{dt_display} (🔁 รายวัน)"
            elif rep == "weekly":
                dt_display = f"{dt_display} (🔁 รายสัปดาห์)"
            target_names = ", ".join(
                t.get("name", t.get("page_id", "")) for t in job["targets"]
            )
            snippet = job["message"].replace("\n", " ").strip()[:80]
            if not snippet:
                if job.get("video_path"):
                    snippet = "(โพสต์ Reels)" if job.get("is_reel") else "(โพสต์วิดีโอ)"
                elif job["image_paths"]:
                    snippet = "(โพสต์รูปภาพ)"
                elif job["link"]:
                    snippet = "(โพสต์ลิงก์)"
                elif job.get("og_action_type_id"):
                    snippet = f"({_format_activity_preview_label(job.get('og_action_type_id', ''), job.get('og_object_id', ''), job.get('og_label', ''))})"
                elif job.get("place_id"):
                    snippet = f"(เช็คอิน: {job.get('place_name') or job.get('place_id')})"
            media_parts = []
            if job["image_paths"]:
                media_parts.append(f"🖼 {len(job['image_paths'])}")
            if job.get("video_path"):
                media_parts.append("🎬 Reels" if job.get("is_reel") else "🎬 วิดีโอ")
            if job["link"]:
                media_parts.append("🔗 ลิงก์")
            if job.get("place_id"):
                media_parts.append("📍 เช็คอิน")
            if job.get("og_action_type_id"):
                media_parts.append("🎯 กิจกรรม")

            self.tbl_queue.setItem(r, 0, id_item)
            self.tbl_queue.setItem(r, 1, QTableWidgetItem(dt_display))
            self.tbl_queue.setItem(
                r, 2, QTableWidgetItem(f"{len(job['targets'])} เพจ ({target_names})")
            )
            self.tbl_queue.setItem(r, 3, QTableWidgetItem(snippet))
            self.tbl_queue.setItem(
                r, 4, QTableWidgetItem(" ".join(media_parts) or "-")
            )
            st_map = {
                "pending": "🕒 รอโพสต์",
                "running": "⏳ กำลังโพสต์",
                "failed": "❌ ล้มเหลว",
            }
            st_text = st_map.get(job["status"], job["status"])
            if job["status"] == "failed" and job.get("last_error"):
                st_text = f"{st_text}: {job['last_error'][:50]}"
            st_item = QTableWidgetItem(st_text)
            if job.get("last_error"):
                st_item.setToolTip(job["last_error"])
            self.tbl_queue.setItem(r, 5, st_item)
        self.tbl_queue.resizeColumnToContents(0)
        self.tbl_queue.resizeColumnToContents(1)
        self.tbl_queue.resizeColumnToContents(4)
        self.tbl_queue.resizeColumnToContents(5)

    def _selected_queue_job_id(self) -> int | None:
        row = self.tbl_queue.currentRow()
        if row < 0:
            return None
        it = self.tbl_queue.item(row, 0)
        return int(it.data(Qt.UserRole)) if it else None

    def cancel_selected_queue_job(self):
        job_id = self._selected_queue_job_id()
        if job_id is None:
            return
        reply = QMessageBox.question(
            self,
            "ยืนยันการยกเลิกคิว",
            f"ต้องการยกเลิกคิวตั้งเวลา #{job_id} ใช่หรือไม่?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        db.delete_scheduled_job(job_id)
        self.refresh_scheduled_queue()
        self.status.setText(f"ยกเลิกคิวตั้งเวลา #{job_id} แล้ว")

    def run_selected_queue_job(self):
        job_id = self._selected_queue_job_id()
        if job_id is None:
            return
        job = db.get_scheduled_job(job_id)
        if not job:
            return
        self._dispatch_queue_job(job)

    def edit_selected_queue_job(self):
        job_id = self._selected_queue_job_id()
        if job_id is None:
            QMessageBox.information(
                self, "ยังไม่ได้เลือกคิว", "กรุณาคลิกเลือกรายการคิวตั้งเวลาที่ต้องการแก้ไขก่อน"
            )
            return
        job = db.get_scheduled_job(job_id)
        if not job:
            return
        if job.get("status") == "running":
            QMessageBox.information(
                self, "กำลังทำงาน", "คิวนี้กำลังส่งโพสต์อยู่ ไม่สามารถแก้ไขได้ในขณะนี้"
            )
            return
        dlg = EditQueueJobDialog(job, parent=self)
        if dlg.exec() != QDialog.Accepted:
            return
        vals = dlg.updated_values()
        if vals["link"] and not _is_valid_http_url(vals["link"]):
            QMessageBox.warning(
                self, "ลิงก์ไม่ถูกต้อง", "ลิงก์ต้องขึ้นต้นด้วย http:// หรือ https://"
            )
            return
        db.update_scheduled_job(
            job_id,
            run_at_iso=vals["run_at_iso"],
            message=vals["message"],
            link=vals["link"],
            repeat_rule=vals["repeat_rule"],
            watermark_text=vals["watermark_text"],
            image_paths=vals.get("image_paths"),
            video_path=vals.get("video_path"),
            is_reel=vals.get("is_reel"),
            place_id=vals.get("place_id"),
            place_name=vals.get("place_name"),
            og_action_type_id=vals.get("og_action_type_id"),
            og_object_id=vals.get("og_object_id"),
            og_label=vals.get("og_label"),
        )
        self.refresh_scheduled_queue()
        self.status.setText(f"แก้ไขคิวตั้งเวลา #{job_id} เรียบร้อยแล้ว")

    def retry_all_failed_queue_jobs(self):
        count = db.retry_failed_scheduled_jobs()
        if count == 0:
            QMessageBox.information(
                self, "ไม่มีรายการล้มเหลว", "ไม่พบคิวตั้งเวลาที่อยู่ในสถานะล้มเหลว"
            )
            return
        self.refresh_scheduled_queue()
        self.status.setText(f"รีเซ็ตคิวที่ล้มเหลวกลับมาเตรียมส่งใหม่ {count} รายการ")
        self._process_due_queue_jobs()

    def import_queue_from_csv(self):
        targets = self.checked_targets()
        if not targets:
            QMessageBox.information(
                self,
                "ยังไม่ได้เลือกเพจเป้าหมาย",
                "กรุณาติ๊กเลือกเพจทางซ้ายอย่างน้อย 1 เพจที่จะใช้เป็นเป้าหมายของคิวจากไฟล์ CSV",
            )
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "เลือกไฟล์ CSV สำหรับนำเข้าคิวตั้งเวลา (คอลัมน์: run_at,message,link,image_paths,video_path)",
            "",
            "CSV Files (*.csv);;ทุกไฟล์ (*.*)",
        )
        if not path:
            return
        target_dicts = [
            {"platform": pl, "page_id": pid, "name": name}
            for pl, pid, name in targets
        ]
        try:
            count = import_scheduled_jobs_from_csv(path, target_dicts)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "นำเข้า CSV ไม่สำเร็จ", str(exc))
            return
        self.refresh_scheduled_queue()
        self.status.setText(f"นำเข้าคิวตั้งเวลาจาก CSV สำเร็จ {count} รายการ")

    def _process_due_queue_jobs(self):
        if QApplication.activeModalWidget() is not None:
            return
        if self.worker and self.worker.isRunning():
            return
        due = db.get_due_scheduled_jobs()
        if due:
            self._dispatch_queue_job(due[0])

    def _dispatch_queue_job(self, job: dict):
        if self.worker and self.worker.isRunning():
            return
        job_id = job["id"]
        db.update_scheduled_job_status(job_id, "running")
        self.refresh_scheduled_queue()

        targets = [
            (t["platform"], t["page_id"], t.get("name", t["page_id"]))
            for t in job["targets"]
        ]

        def on_complete(_results, errs):
            eff_errs = list(errs)
            if not _results and not eff_errs:
                eff_errs = ["ไม่มีเพจเป้าหมายสำหรับโพสต์"]
            if eff_errs:
                db.update_scheduled_job_status(job_id, "failed", "; ".join(eff_errs))
                self._notify_tray(
                    "คิวโพสต์ตั้งเวลาล้มเหลวบางส่วน",
                    f"คิว #{job_id}: {eff_errs[0]}",
                )
            else:
                if job.get("repeat_rule") in ("daily", "weekly"):
                    next_iso = db.advance_recurring_scheduled_job(job_id)
                    self._notify_tray(
                        "โพสต์ตามคิวสำเร็จ (เลื่อนรอบถัดไป)",
                        f"คิว #{job_id} โพสต์สำเร็จ และตั้งเวลารอบถัดไปที่ {next_iso or ''}",
                    )
                else:
                    db.update_scheduled_job_status(job_id, "done")
                    self._notify_tray(
                        "โพสต์ตามคิวตั้งเวลาสำเร็จ",
                        f"คิว #{job_id} โพสต์ลง {len(targets)} เพจเรียบร้อยแล้ว",
                    )
            self.refresh_scheduled_queue()
            if not eff_errs:
                QTimer.singleShot(500, self._process_due_queue_jobs)

        def on_error(err_msg: str):
            db.update_scheduled_job_status(job_id, "failed", str(err_msg))
            self.refresh_scheduled_queue()

        self._execute_post_batch(
            targets=targets,
            message=job["message"],
            link=job["link"],
            images=job["image_paths"],
            video_path=job.get("video_path", ""),
            is_reel=bool(job.get("is_reel")),
            scheduled_dt=None,
            comments=job.get("comments", []),
            comment_delay=job.get("comment_delay", 2.0),
            page_delay_min=float(self.spin_page_delay_min.value()),
            page_delay_max=float(self.spin_page_delay_max.value()),
            watermark_text=job.get("watermark_text", ""),
            place_id=job.get("place_id", ""),
            place_name=job.get("place_name", ""),
            og_action_type_id=job.get("og_action_type_id", ""),
            og_object_id=job.get("og_object_id", ""),
            og_label=job.get("og_label", ""),
            clear_composer_on_success=False,
            on_complete=on_complete,
            on_error=on_error,
        )

    # --- Post History ---

    def _set_history_filter(self, key: str):
        self.history_filter = key
        for k, btn in self._filter_pills.items():
            btn.setProperty("active", k == key)
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        self.refresh_history()

    def refresh_history(self):
        published_total = db.count_post_history("published")
        self.stat_published.set_value(published_total)

        entries = db.list_post_history(status_filter=self.history_filter)
        self.tbl_history.setRowCount(len(entries))
        status_labels = {
            "published": "✅ สำเร็จ",
            "scheduled": "🕒 ตั้งเวลาแล้ว",
            "failed": "❌ ล้มเหลว",
        }
        for r, item in enumerate(entries):
            try:
                t_disp = datetime.fromisoformat(item["created_at"]).strftime(
                    "%Y-%m-%d %H:%M"
                )
            except ValueError:
                t_disp = item["created_at"]
            time_item = QTableWidgetItem(t_disp)
            time_item.setData(Qt.UserRole, item["id"])
            time_item.setData(Qt.UserRole + 1, item.get("batch_id", ""))

            snippet = item["message"].replace("\n", " ").strip()[:90]
            if not snippet:
                if item.get("video_path"):
                    snippet = "(โพสต์ Reels)" if item.get("is_reel") else "(โพสต์วิดีโอ)"
                elif item["image_paths"]:
                    snippet = "(โพสต์รูปภาพ)"
                elif item["link"]:
                    snippet = "(โพสต์ลิงก์)"
                elif item.get("og_action_type_id"):
                    snippet = f"({_format_activity_preview_label(item.get('og_action_type_id', ''), item.get('og_object_id', ''), item.get('og_label', ''))})"
                elif item.get("place_id"):
                    snippet = f"(เช็คอิน: {item.get('place_name') or item.get('place_id')})"
            media_parts = []
            if item["image_paths"]:
                media_parts.append(f"🖼 {len(item['image_paths'])}")
            if item.get("video_path"):
                media_parts.append("🎬 Reels" if item.get("is_reel") else "🎬 วิดีโอ")
            if item["link"]:
                media_parts.append("🔗 ลิงก์")
            if item.get("place_id"):
                media_parts.append("📍 เช็คอิน")
            if item.get("og_action_type_id"):
                media_parts.append("🎯 กิจกรรม")
            detail = item["error"] or item["remote_post_id"] or "-"
            if item["status"] == "scheduled" and item["scheduled_for"]:
                detail = f"ตั้งเวลา {item['scheduled_for']} ({detail})"

            self.tbl_history.setItem(r, 0, time_item)
            self.tbl_history.setItem(r, 1, QTableWidgetItem(item["page_name"]))
            self.tbl_history.setItem(
                r, 2, QTableWidgetItem(status_labels.get(item["status"], item["status"]))
            )
            self.tbl_history.setItem(r, 3, QTableWidgetItem(snippet))
            self.tbl_history.setItem(
                r, 4, QTableWidgetItem(" ".join(media_parts) or "-")
            )
            self.tbl_history.setItem(r, 5, QTableWidgetItem(detail))

        self.tbl_history.resizeColumnToContents(0)
        self.tbl_history.resizeColumnToContents(1)
        self.tbl_history.resizeColumnToContents(2)
        self.tbl_history.resizeColumnToContents(4)

    def _selected_history_id(self) -> int | None:
        ids = self._selected_history_ids()
        return ids[0] if ids else None

    def _selected_history_ids(self) -> list[int]:
        sel_model = self.tbl_history.selectionModel()
        rows: list[int] = []
        if sel_model is not None:
            rows = sorted({idx.row() for idx in sel_model.selectedIndexes()})
        if not rows:
            cur = self.tbl_history.currentRow()
            if cur >= 0:
                rows = [cur]
        out: list[int] = []
        for r in rows:
            it = self.tbl_history.item(r, 0)
            if it is not None and it.data(Qt.UserRole) is not None:
                out.append(int(it.data(Qt.UserRole)))
        return out

    def _find_same_batch_entries(self, seed_entry: dict) -> list[dict]:
        batch_id = (seed_entry.get("batch_id") or "").strip()
        if batch_id:
            candidates = db.list_post_history_by_batch(batch_id)
        else:
            # Legacy fallback for posts created before batch_id column existed
            seed_minute = (seed_entry.get("created_at") or "")[:16]
            seed_msg = seed_entry.get("message") or ""
            seed_link = seed_entry.get("link") or ""
            candidates = [
                e
                for e in db.list_post_history(limit=200)
                if (e.get("created_at") or "")[:16] == seed_minute
                and (e.get("message") or "") == seed_msg
                and (e.get("link") or "") == seed_link
            ]
        return [
            e
            for e in candidates
            if e.get("remote_post_id") and e.get("platform") == facebook.PLATFORM
        ]

    def select_same_batch_history(self):
        entry_id = self._selected_history_id()
        if entry_id is None:
            QMessageBox.information(
                self, "ยังไม่ได้เลือกรายการ", "คลิกเลือกรายการโพสต์ในประวัติอย่างน้อย 1 แถวก่อน"
            )
            return
        seed = db.get_post_history(entry_id)
        if not seed:
            return
        batch_id = (seed.get("batch_id") or "").strip()
        if batch_id:
            target_ids = {e["id"] for e in db.list_post_history_by_batch(batch_id)}
        else:
            seed_min = (seed.get("created_at") or "")[:16]
            target_ids = {
                e["id"]
                for e in db.list_post_history(limit=200)
                if (e.get("created_at") or "")[:16] == seed_min
                and (e.get("message") or "") == (seed.get("message") or "")
                and (e.get("link") or "") == (seed.get("link") or "")
            }
        sel_model = self.tbl_history.selectionModel()
        if sel_model is None:
            return
        from PySide6.QtCore import QItemSelectionModel

        self.tbl_history.clearSelection()
        matched_rows = 0
        for r in range(self.tbl_history.rowCount()):
            it = self.tbl_history.item(r, 0)
            if it and int(it.data(Qt.UserRole)) in target_ids:
                sel_model.select(
                    self.tbl_history.model().index(r, 0),
                    QItemSelectionModel.Select | QItemSelectionModel.Rows,
                )
                matched_rows += 1
        self.status.setText(f"เลือกโพสต์ชุดเดียวกันแล้ว {matched_rows} รายการ")

    def repost_selected_history(self):
        entry_id = self._selected_history_id()
        if entry_id is None:
            QMessageBox.information(
                self, "ยังไม่ได้เลือกรายการ", "คลิกเลือกรายการในประวัติก่อน"
            )
            return
        entry = db.get_post_history(entry_id)
        if not entry:
            return
        self.editor.setPlainText(entry["message"])
        self.input_link.setText(entry["link"])
        self._set_composer_context(
            place_id=entry.get("place_id") or "",
            place_name=entry.get("place_name") or "",
            og_action_type_id=entry.get("og_action_type_id") or "",
            og_object_id=entry.get("og_object_id") or "",
            og_label=entry.get("og_label") or "",
        )
        self.clear_followup_comments()
        vid = entry.get("video_path") or ""
        missing_video = False
        if vid and Path(vid).is_file():
            self.chk_video_reel.setChecked(bool(entry.get("is_reel")))
            self.set_attached_video(vid)
            missing_count = 0
        else:
            missing_video = bool(vid)
            self.clear_video()
            existing_imgs = [p for p in entry["image_paths"] if Path(p).is_file()]
            missing_count = len(entry["image_paths"]) - len(existing_imgs)
            self.attached_images = existing_imgs
            self._refresh_image_list()
        self._switch_page(0)
        if missing_video:
            self.status.setText("โหลดโพสต์เดิมแล้ว (ไม่พบไฟล์วิดีโอเดิมในเครื่อง)")
        elif missing_count:
            self.status.setText(
                f"โหลดโพสต์เดิมแล้ว (มีรูปภาพ {missing_count} รูปที่ถูกลบ/ย้ายออกจากเครื่อง)"
            )
        else:
            self.status.setText("โหลดข้อความและสื่อจากประวัติเข้าสู่หน้าเขียนโพสต์แล้ว")

    def delete_selected_history(self):
        entry_ids = self._selected_history_ids()
        if not entry_ids:
            return
        for eid in entry_ids:
            db.delete_post_history(eid)
        self.refresh_history()
        self.status.setText(f"ลบรายการประวัติแล้ว {len(entry_ids)} รายการ")

    def _remove_post_from_page_list(self, post_id: str) -> None:
        for i in range(self.list_page_posts.count() - 1, -1, -1):
            it = self.list_page_posts.item(i)
            if str(it.data(Qt.UserRole) or "") == post_id:
                self.list_page_posts.takeItem(i)
        if self.input_comment_post_id.text().strip() == post_id:
            self.input_comment_post_id.clear()

    def delete_selected_history_from_fb(self):
        entry_ids = self._selected_history_ids()
        if not entry_ids:
            QMessageBox.information(
                self, "ยังไม่ได้เลือกรายการ", "คลิกเลือกรายการโพสต์ในประวัติก่อน"
            )
            return
        entries = [db.get_post_history(eid) for eid in entry_ids]
        valid_entries = [e for e in entries if e and e.get("remote_post_id")]
        if not valid_entries:
            QMessageBox.information(
                self,
                "ลบไม่ได้",
                "รายการที่เลือกยังไม่มี Post ID บน Facebook (หากต้องการลบเฉพาะประวัติในโปรแกรม ให้กดปุ่ม 'ลบประวัติ')",
            )
            return
        fb_entries = [
            e for e in valid_entries if e.get("platform") == facebook.PLATFORM
        ]
        if not fb_entries:
            QMessageBox.warning(self, "ไม่รองรับ", "ยังไม่รองรับการลบโพสต์ของแพลตฟอร์มนี้")
            return

        if len(fb_entries) == 1:
            e0 = fb_entries[0]
            prompt_msg = f"ต้องการลบโพสต์ {e0['remote_post_id']} ออกจากหน้าเพจ '{e0['page_name']}' บน Facebook อย่างถาวรใช่หรือไม่?"
        else:
            preview_lines = "\n".join(
                f"• {e['page_name']} ({e['remote_post_id']})" for e in fb_entries[:8]
            )
            if len(fb_entries) > 8:
                preview_lines += f"\n…และอีก {len(fb_entries) - 8} เพจ"
            prompt_msg = (
                f"ต้องการลบโพสต์ที่เลือกทั้งหมด {len(fb_entries)} เพจ ออกจาก Facebook อย่างถาวรใช่หรือไม่?\n\n"
                f"{preview_lines}"
            )
        reply = QMessageBox.question(
            self,
            "ยืนยันการลบโพสต์บน Facebook",
            prompt_msg,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        def job():
            deleted: list[dict] = []
            errs: list[str] = []
            for e in fb_entries:
                try:
                    facebook.delete_post(e["page_id"], e["remote_post_id"])
                    deleted.append(e)
                except Exception as exc:  # noqa: BLE001
                    errs.append(f"{e['page_name']}: {exc}")
            return deleted, errs

        def done(payload):
            deleted, errs = payload
            for e in deleted:
                db.delete_post_history(e["id"])
                db.delete_post_history_by_remote_id(
                    facebook.PLATFORM, e["page_id"], e["remote_post_id"]
                )
                self._remove_post_from_page_list(e["remote_post_id"])
            self.refresh_history()
            if len(fb_entries) == 1 and deleted:
                e0 = deleted[0]
                self.status.setText(
                    f"ลบโพสต์ {e0['remote_post_id']} ออกจากหน้าเพจ {e0['page_name']} บน Facebook เรียบร้อยแล้ว"
                )
            else:
                self.status.setText(
                    f"ลบโพสต์ออกจาก Facebook สำเร็จ {len(deleted)}/{len(fb_entries)} เพจ"
                )
            if errs:
                QMessageBox.warning(self, "บางเพจลบไม่สำเร็จ", "\n".join(errs))

        self._run(
            job,
            done,
            f"กำลังลบโพสต์ออกจาก Facebook ({len(fb_entries)} เพจ)…",
        )

    def delete_selected_page_post_from_fb(self):
        data = self.combo_comment_page.currentData()
        if not data:
            QMessageBox.information(self, "ยังไม่มีเพจ", "กรุณาเลือกเพจ Facebook ก่อน")
            return
        _, page_id, page_name = data
        it = self.list_page_posts.currentItem()
        if not it:
            QMessageBox.information(
                self,
                "ยังไม่ได้เลือกโพสต์",
                "กรุณากด 'โหลดโพสต์ล่าสุด' แล้วคลิกเลือกโพสต์ที่ต้องการลบก่อน",
            )
            return
        owner_meta = it.data(Qt.UserRole + 3)
        if isinstance(owner_meta, tuple) and len(owner_meta) == 2:
            page_id, page_name = owner_meta
        post_id = str(it.data(Qt.UserRole) or "")
        if not post_id:
            return
        reply = QMessageBox.question(
            self,
            "ยืนยันการลบโพสต์บน Facebook",
            f"ต้องการลบโพสต์ {post_id} ออกจากหน้าเพจ '{page_name}' บน Facebook อย่างถาวรใช่หรือไม่?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        def done(_ok):
            self._remove_post_from_page_list(post_id)
            db.delete_post_history_by_remote_id(facebook.PLATFORM, page_id, post_id)
            self.refresh_history()
            self.status.setText(
                f"ลบโพสต์ {post_id} ออกจากหน้าเพจ {page_name} บน Facebook เรียบร้อยแล้ว"
            )

        self._run(
            lambda: facebook.delete_post(page_id, post_id),
            done,
            f"กำลังลบโพสต์ {post_id} ออกจากเพจ {page_name} บน Facebook…",
        )

    def _sync_edited_post_in_page_list(
        self, post_id: str, new_msg: str, *, old_post_id: str | None = None
    ) -> None:
        target_old = (old_post_id or post_id).strip()
        if target_old and post_id and target_old != post_id:
            if self.input_comment_post_id.text().strip() == target_old:
                self.input_comment_post_id.setText(post_id)
        snippet = (new_msg or "(ไม่มีข้อความ / โพสต์รูปภาพ)").replace("\n", " ")[:65]
        for i in range(self.list_page_posts.count()):
            it = self.list_page_posts.item(i)
            cur_id = str(it.data(Qt.UserRole) or "")
            if cur_id in (post_id, target_old):
                if cur_id != post_id:
                    it.setData(Qt.UserRole, post_id)
                t_str = str(it.data(Qt.UserRole + 2) or "")
                it.setText(f"[{t_str}] {snippet}")
                it.setToolTip(f"Post ID: {post_id}\n{new_msg}")
                it.setData(Qt.UserRole + 1, new_msg)

    @staticmethod
    def _preserved_schedule_dt(entry: dict | None) -> datetime | None:
        if not entry or entry.get("status") != "scheduled":
            return None
        raw = (entry.get("scheduled_for") or "").strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.astimezone()
            facebook.validate_fb_schedule_time(dt)
            return dt
        except (ValueError, OSError, OverflowError):
            return None

    def _run_bulk_post_edit(
        self,
        targets_to_edit: list[dict],
        *,
        raw_new_msg: str,
    ):
        cancel_ev = threading.Event()
        self.task_cancel_event = cancel_ev

        def job():
            updated_rows: list[tuple[dict, str, str]] = []
            errs: list[str] = []
            for idx, e in enumerate(targets_to_edit):
                if cancel_ev.is_set():
                    for rem in targets_to_edit[idx:]:
                        errs.append(f"{rem['page_name']}: ถูกยกเลิกกลางคัน")
                    break
                pid = e["page_id"]
                pname = e["page_name"]
                old_post_id = e["remote_post_id"]
                rendered_msg = (
                    facebook.render_spintax_and_variables(raw_new_msg, page_name=pname)
                    if raw_new_msg
                    else ""
                )
                try:
                    facebook.edit_post(
                        pid,
                        old_post_id,
                        rendered_msg,
                        is_video=bool(e.get("video_path")),
                    )
                    if e.get("id"):
                        db.update_post_history_message(e["id"], rendered_msg)
                    db.update_post_history_message_by_remote_id(
                        e["platform"], pid, old_post_id, rendered_msg
                    )
                    updated_rows.append((e, old_post_id, rendered_msg))
                except (RuntimeError, ValueError, OSError) as exc:
                    errs.append(f"{pname}: {exc}")
            return updated_rows, errs

        def done(payload):
            updated_rows, errs = payload
            for e, final_post_id, rendered_msg in updated_rows:
                old_id = e["remote_post_id"]
                self._sync_edited_post_in_page_list(
                    final_post_id, rendered_msg, old_post_id=old_id
                )
            self.refresh_history()
            if len(targets_to_edit) == 1 and updated_rows:
                e0, final_id, _ = updated_rows[0]
                self.status.setText(
                    f"แก้ไขข้อความโพสต์ {final_id} บนเพจ {e0['page_name']} เรียบร้อยแล้ว"
                )
            else:
                self.status.setText(
                    f"แก้ไขข้อความโพสต์สำเร็จ {len(updated_rows)}/{len(targets_to_edit)} เพจ"
                )
            if errs:
                QMessageBox.warning(self, "บางเพจแก้ไขไม่สำเร็จ", "\n".join(errs))

        busy_text = f"กำลังอัปเดตข้อความโพสต์บน {len(targets_to_edit)} เพจ…"
        self._run(job, done, busy_text)

    def edit_selected_history_post(self):
        entry_ids = self._selected_history_ids()
        if not entry_ids:
            QMessageBox.information(
                self, "ยังไม่ได้เลือกรายการ", "คลิกเลือกรายการโพสต์ในประวัติก่อน"
            )
            return
        raw_entries = [db.get_post_history(eid) for eid in entry_ids]
        valid_entries = [e for e in raw_entries if e and e.get("remote_post_id")]
        if not valid_entries:
            QMessageBox.information(
                self,
                "แก้ไขไม่ได้",
                "รายการที่เลือกยังไม่มี Post ID ที่เผยแพร่บน Facebook แล้ว",
            )
            return
        selected_entries = [
            e for e in valid_entries if e.get("platform") == facebook.PLATFORM
        ]
        if not selected_entries:
            QMessageBox.warning(self, "ไม่รองรับ", "ยังไม่รองรับการแก้ไขโพสต์ของแพลตฟอร์มนี้")
            return

        primary = selected_entries[0]
        same_batch = self._find_same_batch_entries(primary)
        batch_entries = (
            same_batch if len(same_batch) > len(selected_entries) else selected_entries
        )

        initial_msg = primary.get("message") or ""
        entry_link = (primary.get("link") or "").strip()
        initial_imgs = list(primary.get("image_paths") or [])
        initial_vid = (primary.get("video_path") or "").strip()

        dlg = EditPostDialog(
            primary["page_name"],
            primary["remote_post_id"],
            initial_msg,
            parent=self,
            page_id=primary.get("page_id") or "",
            current_link=entry_link,
            current_images=initial_imgs,
            current_video=initial_vid,
            current_place_id=primary.get("place_id") or "",
            current_place_name=primary.get("place_name") or "",
            entries=selected_entries,
            batch_entries=batch_entries,
        )
        if dlg.exec() != QDialog.Accepted:
            return

        targets_to_edit = dlg.target_entries() or selected_entries
        self._run_bulk_post_edit(
            targets_to_edit,
            raw_new_msg=dlg.new_message(),
        )

    def edit_selected_page_post(self):
        data = self.combo_comment_page.currentData()
        if not data:
            QMessageBox.information(self, "ยังไม่มีเพจ", "กรุณาเลือกเพจ Facebook ก่อน")
            return
        _, page_id, page_name = data
        it = self.list_page_posts.currentItem()
        if not it:
            QMessageBox.information(
                self,
                "ยังไม่ได้เลือกโพสต์",
                "กรุณากด 'โหลดโพสต์ล่าสุด' แล้วคลิกเลือกโพสต์ที่ต้องการแก้ไขก่อน",
            )
            return
        owner_meta = it.data(Qt.UserRole + 3)
        if isinstance(owner_meta, tuple) and len(owner_meta) == 2:
            page_id, page_name = owner_meta
        post_id = str(it.data(Qt.UserRole) or "")
        cur_msg = str(it.data(Qt.UserRole + 1) or "")
        if cur_msg == "(ไม่มีข้อความ / โพสต์รูปภาพ)":
            cur_msg = ""

        existing_entry = db.get_post_history_by_remote_id(
            facebook.PLATFORM, page_id, post_id
        )
        cur_link = (existing_entry.get("link") or "").strip() if existing_entry else ""
        cur_imgs = list(existing_entry.get("image_paths") or []) if existing_entry else []
        cur_vid = (existing_entry.get("video_path") or "").strip() if existing_entry else ""
        synth_entry = existing_entry or {
            "id": 0,
            "platform": facebook.PLATFORM,
            "page_id": page_id,
            "page_name": page_name,
            "remote_post_id": post_id,
            "message": cur_msg,
            "link": cur_link,
            "image_paths": cur_imgs,
            "video_path": cur_vid,
            "status": "published",
            "scheduled_for": "",
        }
        batch_entries = (
            self._find_same_batch_entries(existing_entry)
            if existing_entry
            else [synth_entry]
        )

        dlg = EditPostDialog(
            page_name,
            post_id,
            cur_msg,
            parent=self,
            page_id=page_id,
            current_link=cur_link,
            current_images=cur_imgs,
            current_video=cur_vid,
            current_place_id=(existing_entry.get("place_id") or "") if existing_entry else "",
            current_place_name=(existing_entry.get("place_name") or "") if existing_entry else "",
            entries=[synth_entry],
            batch_entries=batch_entries,
        )
        if dlg.exec() != QDialog.Accepted:
            return
        targets_to_edit = dlg.target_entries() or [synth_entry]
        self._run_bulk_post_edit(
            targets_to_edit,
            raw_new_msg=dlg.new_message(),
        )

    def view_selected_history_insights(self):
        entry_id = self._selected_history_id()
        if entry_id is None:
            QMessageBox.information(
                self, "ยังไม่ได้เลือกรายการ", "คลิกเลือกรายการโพสต์ในประวัติก่อน"
            )
            return
        entry = db.get_post_history(entry_id)
        if not entry or not entry.get("remote_post_id"):
            QMessageBox.information(
                self,
                "ดูสถิติไม่ได้",
                "รายการนี้ยังไม่มี Post ID ที่เผยแพร่บน Facebook แล้ว",
            )
            return
        if entry.get("platform") != facebook.PLATFORM:
            QMessageBox.warning(self, "ไม่รองรับ", "ยังไม่รองรับการดูสถิติของแพลตฟอร์มนี้")
            return

        def done(metrics: dict):
            msg = (
                f"เพจ: {entry['page_name']}\n"
                f"Post ID: {metrics['id']}\n\n"
                f"👍 ถูกใจ (Likes): {metrics['likes']:,}\n"
                f"💬 คอมเมนต์ (Comments): {metrics['comments']:,}\n"
                f"📤 แชร์ (Shares): {metrics['shares']:,}\n\n"
                f"ลิงก์โพสต์: {metrics['permalink_url']}"
            )
            self.status.setText(
                f"สถิติโพสต์ {metrics['id']}: 👍 {metrics['likes']:,} | 💬 {metrics['comments']:,} | 📤 {metrics['shares']:,}"
            )
            QMessageBox.information(self, "สถิติโพสต์จาก Facebook", msg)

        self._run(
            lambda: facebook.get_post_engagement(entry["page_id"], entry["remote_post_id"]),
            done,
            f"กำลังดึงสถิติโพสต์ของ {entry['page_name']}…",
        )

    def clear_all_history(self):
        if self.tbl_history.rowCount() == 0:
            return
        reply = QMessageBox.question(
            self,
            "ยืนยันการล้างประวัติ",
            "ต้องการล้างประวัติการโพสต์ทั้งหมดใช่หรือไม่?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        db.clear_post_history()
        self.refresh_history()
        self.status.setText("ล้างประวัติการโพสต์ทั้งหมดแล้ว")

    # --- Follow-up Comments (Composer) & Multi-Comment Manager ---

    def add_followup_comment(self):
        text = self.input_followup_comment.text().strip()
        if not text:
            return
        self.follow_up_comments.append(text)
        self.input_followup_comment.clear()
        self._refresh_followup_list()

    def remove_selected_followup_comment(self):
        row = self.list_followup_comments.currentRow()
        if 0 <= row < len(self.follow_up_comments):
            self.follow_up_comments.pop(row)
            self._refresh_followup_list()

    def clear_followup_comments(self):
        self.follow_up_comments.clear()
        self._refresh_followup_list()

    def _refresh_followup_list(self):
        self.list_followup_comments.clear()
        for idx, text in enumerate(self.follow_up_comments, start=1):
            self.list_followup_comments.addItem(f"#{idx}: {text}")
        count = len(self.follow_up_comments)
        self.lbl_followup_count.setText(f"{count} คอมเมนต์")
        has_items = count > 0
        self.list_followup_comments.setVisible(has_items)
        self.btn_remove_followup.setVisible(has_items)
        self.btn_clear_followup.setVisible(has_items)
        self._save_draft()

    def comment_selected_history(self):
        entry_id = self._selected_history_id()
        if entry_id is None:
            QMessageBox.information(
                self, "ยังไม่ได้เลือกรายการ", "คลิกเลือกรายการในประวัติก่อน"
            )
            return
        entry = db.get_post_history(entry_id)
        if not entry or not entry.get("remote_post_id"):
            QMessageBox.information(
                self,
                "ไม่มี Post ID",
                "รายการนี้ยังไม่มี Post ID ที่เผยแพร่แล้ว จึงไม่สามารถคอมเมนต์ได้",
            )
            return
        # Select matching page in combo
        matched = False
        for i in range(self.combo_comment_page.count()):
            data = self.combo_comment_page.itemData(i)
            if data and data[1] == entry["page_id"]:
                self.combo_comment_page.setCurrentIndex(i)
                matched = True
                break
        if not matched:
            QMessageBox.warning(
                self,
                "ไม่พบเพจในรายการที่เชื่อมต่อ",
                f"เพจ '{entry['page_name']}' (ID: {entry['page_id']}) ถูกลบออกจากโปรแกรมแล้ว\n"
                "กรุณาเชื่อมต่อเพจนี้ใหม่ก่อนจึงจะคอมเมนต์ในนามเพจได้",
            )
            return
        self.input_comment_post_id.setText(entry["remote_post_id"])
        self._switch_page(3)
        self.status.setText(f"เลือกโพสต์ {entry['remote_post_id']} สำหรับคอมเมนต์แล้ว")

    def load_posts_for_comment(self):
        data = self.combo_comment_page.currentData()
        if not data:
            QMessageBox.information(self, "ยังไม่มีเพจ", "กรุณาเชื่อมต่อเพจ Facebook ก่อน")
            return
        _, page_id, page_name = data

        def done(posts):
            self.list_page_posts.clear()
            self._loaded_posts_page_id = page_id
            for p in posts:
                snippet = p["message"].replace("\n", " ")[:65]
                t_str = p["created_time"][:16].replace("T", " ")
                raw_msg = p.get("raw_message", "" if p["message"] == "(ไม่มีข้อความ / โพสต์รูปภาพ)" else p["message"])
                it = QListWidgetItem(f"[{t_str}] {snippet}")
                it.setToolTip(f"Post ID: {p['id']}\n{p['message']}")
                it.setData(Qt.UserRole, p["id"])
                it.setData(Qt.UserRole + 1, raw_msg)
                it.setData(Qt.UserRole + 2, t_str)
                it.setData(Qt.UserRole + 3, (page_id, page_name))
                self.list_page_posts.addItem(it)
            self.status.setText(f"โหลดโพสต์ล่าสุดของ {page_name} แล้ว {len(posts)} โพสต์")

        self._run(
            lambda: facebook.list_page_posts(page_id, limit=20),
            done,
            f"กำลังโหลดโพสต์ล่าสุดของ {page_name}…",
        )

    def _on_page_post_selected(self, item: QListWidgetItem):
        pid = item.data(Qt.UserRole)
        if pid:
            self.input_comment_post_id.setText(str(pid))

    def resolve_comment_post_url(self):
        data = self.combo_comment_page.currentData()
        if not data:
            QMessageBox.information(self, "ยังไม่มีเพจ", "กรุณาเลือกเพจ Facebook ก่อน")
            return
        _, page_id, _ = data
        raw = self.input_comment_post_id.text().strip()
        if not raw:
            QMessageBox.information(self, "ยังไม่มีลิงก์", "กรุณาวางลิงก์โพสต์ของเพจก่อน")
            return

        def done(resolved_id: str):
            self.input_comment_post_id.setText(resolved_id)
            self.status.setText(f"แกะลิงก์สำเร็จ: Post ID = {resolved_id}")

        self._run(
            lambda: facebook.resolve_post_id_for_page(page_id, raw),
            done,
            "กำลังแกะ Post ID จากลิงก์โพสต์…",
        )

    def open_post_comments_inbox(self):
        data = self.combo_comment_page.currentData()
        if not data:
            QMessageBox.information(self, "ยังไม่มีเพจ", "กรุณาเลือกเพจ Facebook ก่อน")
            return
        _, page_id, page_name = data
        raw = self.input_comment_post_id.text().strip()
        if not raw:
            QMessageBox.information(
                self,
                "ยังไม่ได้เลือกโพสต์",
                "กรุณาคลิกเลือกโพสต์จากรายการ หรือวางลิงก์โพสต์ / Post ID ก่อน",
            )
            return

        def done(resolved_id: str):
            self.input_comment_post_id.setText(resolved_id)
            self._unlock()
            self.status.setText(f"เปิดกล่องคอมเมนต์ของโพสต์ {resolved_id}")
            dlg = PostCommentsDialog(page_id, page_name, resolved_id, parent=self)
            dlg.load_comments()
            dlg.exec()

        self._run(
            lambda: facebook.resolve_post_id_for_page(page_id, raw),
            done,
            "กำลังเตรียมเปิดกล่องคอมเมนต์ของโพสต์…",
        )

    def select_comment_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "เลือกรูปภาพสำหรับแนบในคอมเมนต์", "", IMAGE_FILTER
        )
        if not path:
            return
        self.attached_comment_image = path
        self.lbl_comment_image.setText(f"🖼 {Path(path).name}")
        self.btn_clear_comment_image.setVisible(True)

    def clear_comment_image(self):
        self.attached_comment_image = ""
        self.lbl_comment_image.setText("")
        self.btn_clear_comment_image.setVisible(False)

    def add_single_queued_comment(self):
        text = self.editor_comment_input.toPlainText().strip()
        img = self.attached_comment_image.strip()
        if not text and not img:
            return
        spec = facebook.format_comment_spec(text, img)
        self.queued_comments.append(spec)
        self.editor_comment_input.clear()
        self.clear_comment_image()
        self._refresh_queued_comments()

    def add_multiline_queued_comments(self):
        raw = self.editor_comment_input.toPlainText()
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if not lines:
            return
        self.queued_comments.extend(lines)
        self.editor_comment_input.clear()
        self._refresh_queued_comments()

    def _move_queued_comment(self, delta: int):
        row = self.list_comments.currentRow()
        new_row = row + delta
        if 0 <= row < len(self.queued_comments) and 0 <= new_row < len(self.queued_comments):
            self.queued_comments[row], self.queued_comments[new_row] = (
                self.queued_comments[new_row],
                self.queued_comments[row],
            )
            self._refresh_queued_comments()
            self.list_comments.setCurrentRow(new_row)

    def remove_selected_queued_comment(self):
        row = self.list_comments.currentRow()
        if 0 <= row < len(self.queued_comments):
            self.queued_comments.pop(row)
            self._refresh_queued_comments()

    def clear_queued_comments(self):
        self.queued_comments.clear()
        self._refresh_queued_comments()

    def _refresh_queued_comments(self):
        self.list_comments.clear()
        for idx, spec in enumerate(self.queued_comments, start=1):
            msg, img = facebook.split_comment_spec(spec)
            preview = msg.replace("\n", " ↵ ")
            if img:
                img_tag = f"🖼 [{Path(img).name}]"
                preview = f"{preview} {img_tag}".strip()
            self.list_comments.addItem(f"#{idx}: {preview}")
        n = len(self.queued_comments)
        self.lbl_comment_queue_count.setText(f"รายการคอมเมนต์ ({n} อัน)")
        self.btn_send_comments.setText(f"  ส่งคอมเมนต์ทั้งหมด ({n} อัน)")

    def send_queued_comments(self):
        data = self.combo_comment_page.currentData()
        if not data:
            QMessageBox.information(self, "ยังไม่มีเพจ", "กรุณาเลือกเพจ Facebook ก่อน")
            return
        _, page_id, page_name = data
        raw_post_input = self.input_comment_post_id.text().strip()
        if not raw_post_input:
            QMessageBox.information(
                self, "ยังไม่ได้เลือกโพสต์", "กรุณาคลิกเลือกโพสต์ วางลิงก์โพสต์ หรือวาง Post ID ก่อน"
            )
            return
        comments = list(self.queued_comments)
        if not comments:
            QMessageBox.information(
                self, "ยังไม่มีคอมเมนต์", "กรุณาเพิ่มคอมเมนต์อย่างน้อย 1 อันก่อนส่ง"
            )
            return
        missing_cimgs: list[str] = []
        for idx, spec in enumerate(comments, start=1):
            ct, ci = facebook.split_comment_spec(spec)
            if len(ct) > FB_MAX_CHARS:
                QMessageBox.warning(
                    self,
                    "ข้อความคอมเมนต์ยาวเกินไป",
                    f"คอมเมนต์ #{idx} ยาวเกิน {FB_MAX_CHARS:,} ตัวอักษร",
                )
                return
            if ci and not Path(ci).is_file():
                missing_cimgs.append(f"#{idx}: {ci}")
        if missing_cimgs:
            QMessageBox.warning(
                self,
                "ไม่พบไฟล์รูปแนบในคอมเมนต์",
                "ไฟล์รูปภาพในคอมเมนต์ต่อไปนี้ไม่พบในเครื่อง:\n" + "\n".join(missing_cimgs),
            )
            return
        delay = float(self.spin_comment_delay.value())
        cancel_ev = threading.Event()
        self.task_cancel_event = cancel_ev

        def job():
            resolved_id = facebook.resolve_post_id_for_page(page_id, raw_post_input)
            rendered = []
            for spec in comments:
                ct, ci = facebook.split_comment_spec(spec)
                rt = facebook.render_spintax_and_variables(ct, page_name=page_name) if ct else ""
                rendered.append(facebook.format_comment_spec(rt, ci))
            res = facebook.post_multiple_comments(
                page_id,
                resolved_id,
                rendered,
                delay_seconds=delay,
                cancel_event=cancel_ev,
            )
            return resolved_id, res

        def done(payload):
            resolved_id, results = payload
            self.input_comment_post_id.setText(resolved_id)
            ok = sum(1 for _, _, _, err in results if err is None)
            errs = [f"#{idx}: {err}" for idx, _, _, err in results if err]
            self.status.setText(
                f"ส่งคอมเมนต์บน {page_name} สำเร็จ {ok}/{len(results)} อัน"
            )
            if errs:
                hint = (
                    "\n\n(หมายเหตุ: หากขึ้น error เรื่องสิทธิ์ไม่พอ ให้กด 'เชื่อมต่อ Facebook' ใหม่เพื่อขอสิทธิ์ pages_manage_engagement "
                    "และสามารถคอมเมนต์ได้เฉพาะโพสต์ในเพจที่คุณเป็นแอดมินเท่านั้น)"
                )
                QMessageBox.warning(
                    self, "บางคอมเมนต์ไม่สำเร็จ", "\n".join(errs) + hint
                )
            else:
                self.clear_queued_comments()
                QMessageBox.information(
                    self,
                    "ส่งคอมเมนต์สำเร็จ",
                    f"ส่งคอมเมนต์ทั้งหมด {ok} อันลงโพสต์ {resolved_id} เรียบร้อยแล้ว",
                )

        self._run(
            job,
            done,
            f"กำลังแกะลิงก์และส่งคอมเมนต์ {len(comments)} อันลงโพสต์…",
        )

    def _pick_comment_for_copy(self) -> tuple[str, int, int] | None:
        """Pick currently selected queued comment (or first, or editor text) and advance cursor."""
        data = self.combo_comment_page.currentData()
        page_name = data[2] if isinstance(data, tuple) and len(data) >= 3 else ""
        if self.queued_comments:
            row = self.list_comments.currentRow()
            if row < 0 or row >= len(self.queued_comments):
                row = 0
            text, _ = facebook.split_comment_spec(self.queued_comments[row])
            if text:
                text = facebook.render_spintax_and_variables(text, page_name=page_name)
            next_row = (row + 1) % len(self.queued_comments)
            self.list_comments.setCurrentRow(next_row)
            return text, row + 1, len(self.queued_comments)
        draft = self.editor_comment_input.toPlainText().strip()
        if draft:
            draft = facebook.render_spintax_and_variables(draft, page_name=page_name)
            return draft, 1, 1
        return None

    def copy_next_queued_comment(self):
        picked = self._pick_comment_for_copy()
        if not picked:
            QMessageBox.information(
                self,
                "ยังไม่มีข้อความคอมเมนต์",
                "กรุณาพิมพ์ข้อความหรือเพิ่มคอมเมนต์ในรายการก่อนคัดลอก",
            )
            return
        text, idx, total = picked
        if not text:
            QMessageBox.information(
                self,
                "คอมเมนต์นี้มีเฉพาะรูปภาพ",
                f"คอมเมนต์ #{idx}/{total} ไม่มีข้อความให้คัดลอก (เป็นคอมเมนต์แนบรูปภาพสำหรับส่งผ่าน API)",
            )
            return
        QApplication.clipboard().setText(text)
        nxt_hint = f" (เลื่อนรออันถัดไปแล้ว)" if total > 1 else ""
        self.status.setText(
            f"คัดลอกคอมเมนต์ #{idx}/{total} ลงคลิปบอร์ดแล้ว — กด Ctrl+V วางได้เลย{nxt_hint}"
        )

    def quick_comment_open_browser(self):
        raw_target = self.input_comment_post_id.text().strip()
        if not raw_target:
            QMessageBox.information(
                self,
                "ยังไม่ได้วางลิงก์โพสต์",
                "กรุณาวางลิงก์โพสต์ Facebook (ของเพจตัวเองหรือเพจอื่น) ในช่องทางซ้ายก่อน",
            )
            return

        # Accept either a Facebook URL or a Post ID
        low = raw_target.lower()
        is_url_like = low.startswith(("http://", "https://")) or any(
            d in low for d in ("facebook.com/", "fb.com/", "fb.watch/")
        )
        try:
            if is_url_like:
                target_url = facebook.validate_facebook_url(raw_target, label="ลิงก์โพสต์ Facebook")
            else:
                clean_post_id = facebook._validate_graph_id(raw_target, "Post ID")
                data = self.combo_comment_page.currentData()
                pid = data[1] if data else ""
                target_url = facebook.build_post_permalink(pid, clean_post_id)
                target_url = facebook.validate_facebook_url(target_url, label="ลิงก์โพสต์ Facebook")
        except ValueError as exc:
            QMessageBox.warning(self, "ลิงก์โพสต์ไม่ถูกต้อง", str(exc))
            return

        picked = self._pick_comment_for_copy()
        if picked and picked[0]:
            text, idx, total = picked
            QApplication.clipboard().setText(text)
            webbrowser.open(target_url)
            self.status.setText(
                f"คัดลอกคอมเมนต์ #{idx}/{total} ลงคลิปบอร์ดและเปิดโพสต์ในเบราว์เซอร์แล้ว — กด Ctrl+V วางได้เลย"
            )
        else:
            webbrowser.open(target_url)
            self.status.setText("เปิดโพสต์ในเบราว์เซอร์แล้ว (ยังไม่มีข้อความคอมเมนต์ให้คัดลอก)")

    # --- Saved Facebook Groups & Quick Share Helper ---

    def refresh_saved_groups(self):
        prev_ids = {g["id"] for g in self.checked_saved_groups()}
        self.list_saved_groups.blockSignals(True)
        self.list_saved_groups.clear()
        for g in db.list_saved_groups():
            it = QListWidgetItem(g["name"])
            it.setIcon(ui_icons.page_avatar_icon(g["name"], 26))
            it.setToolTip(g["url"])
            it.setData(Qt.UserRole, g)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if g["id"] in prev_ids else Qt.Unchecked)
            self.list_saved_groups.addItem(it)
        self.list_saved_groups.blockSignals(False)
        self._on_saved_groups_changed()

    def add_saved_group(self):
        name = self.input_group_name.text().strip()
        raw_url = self.input_group_url.text().strip()
        if not name:
            QMessageBox.information(self, "ยังไม่ได้ใส่ชื่อกลุ่ม", "กรุณากรอกชื่อกลุ่มก่อนบันทึก")
            return
        try:
            clean_url = facebook.validate_facebook_group_url(raw_url)
        except ValueError as exc:
            QMessageBox.warning(self, "ลิงก์กลุ่มไม่ถูกต้อง", str(exc))
            return
        db.add_saved_group(name, clean_url)
        self.input_group_name.clear()
        self.input_group_url.clear()
        self.refresh_saved_groups()
        self.status.setText(f"บันทึกกลุ่ม '{name}' เรียบร้อยแล้ว")

    def _set_all_saved_groups(self, checked: bool):
        self.list_saved_groups.blockSignals(True)
        state = Qt.Checked if checked else Qt.Unchecked
        for i in range(self.list_saved_groups.count()):
            self.list_saved_groups.item(i).setCheckState(state)
        self.list_saved_groups.blockSignals(False)
        self._on_saved_groups_changed()

    def checked_saved_groups(self) -> list[dict]:
        out = []
        for i in range(self.list_saved_groups.count()):
            it = self.list_saved_groups.item(i)
            if it.checkState() == Qt.Checked:
                out.append(it.data(Qt.UserRole))
        return out

    def _on_saved_groups_changed(self):
        total = self.list_saved_groups.count()
        sel = len(self.checked_saved_groups())
        self.lbl_groups_count.setText(f"เลือก {sel} / {total}")
        self.btn_open_checked_groups.setText(
            f"  คัดลอกและเปิดกลุ่มที่ติ๊ก ({sel} กลุ่ม)"
        )

    def delete_selected_saved_groups(self):
        targets = self.checked_saved_groups()
        if not targets:
            cur = self.list_saved_groups.currentItem()
            if cur:
                targets = [cur.data(Qt.UserRole)]
        if not targets:
            QMessageBox.information(
                self, "ยังไม่ได้เลือกกลุ่ม", "ติ๊กเลือกหรือคลิกกลุ่มที่ต้องการลบก่อน"
            )
            return
        names = "\n".join(f"• {g['name']}" for g in targets)
        reply = QMessageBox.question(
            self,
            "ยืนยันการลบกลุ่ม",
            f"ต้องการลบกลุ่มต่อไปนี้ ({len(targets)} กลุ่ม) ออกจากสมุดบันทึกใช่หรือไม่?\n\n{names}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        for g in targets:
            db.delete_saved_group(g["id"])
        self.refresh_saved_groups()
        self.status.setText(f"ลบกลุ่มที่บันทึกไว้แล้ว {len(targets)} กลุ่ม")

    def share_selected_history(self):
        entry_id = self._selected_history_id()
        if entry_id is None:
            QMessageBox.information(
                self, "ยังไม่ได้เลือกรายการ", "คลิกเลือกรายการในประวัติก่อน"
            )
            return
        entry = db.get_post_history(entry_id)
        if not entry or not entry.get("remote_post_id"):
            QMessageBox.information(
                self,
                "ไม่มีลิงก์โพสต์",
                "รายการนี้ยังไม่มี Post ID ที่เผยแพร่แล้ว จึงยังไม่สามารถสร้างลิงก์แชร์ได้",
            )
            return
        permalink = facebook.build_post_permalink(
            entry["page_id"],
            entry["remote_post_id"],
            is_reel=bool(entry.get("is_reel")),
        )
        self.input_share_url.setText(permalink)
        self.editor_share_caption.setPlainText(entry.get("message") or "")
        self._switch_page(4)
        self.status.setText("โหลดลิงก์โพสต์และข้อความเข้าสู่หน้าแชร์ลงกลุ่มแล้ว")

    def load_latest_post_for_share(self):
        entries = db.list_post_history(status_filter="published", limit=20)
        for entry in entries:
            if entry.get("remote_post_id"):
                permalink = facebook.build_post_permalink(
                    entry["page_id"],
                    entry["remote_post_id"],
                    is_reel=bool(entry.get("is_reel")),
                )
                self.input_share_url.setText(permalink)
                self.editor_share_caption.setPlainText(entry.get("message") or "")
                self.status.setText(
                    f"ดึงโพสต์ล่าสุดจากเพจ {entry['page_name']} เรียบร้อยแล้ว"
                )
                return
        QMessageBox.information(
            self, "ยังไม่มีโพสต์ที่เผยแพร่แล้ว", "ยังไม่มีประวัติโพสต์ที่สำเร็จสำหรับนำมาแชร์"
        )

    def _build_share_clipboard_text(self) -> str:
        url = self.input_share_url.text().strip()
        caption = self.editor_share_caption.toPlainText().strip()
        if caption:
            targets = self.checked_targets()
            page_name = targets[0][2] if targets else ""
            caption = facebook.render_spintax_and_variables(caption, page_name=page_name)
        if caption and url:
            return f"{caption}\n\n{url}"
        return url or caption

    def copy_share_text(self):
        payload = self._build_share_clipboard_text()
        if not payload:
            QMessageBox.information(
                self, "ยังไม่มีข้อมูล", "กรุณาใส่ลิงก์โพสต์หรือข้อความก่อนคัดลอก"
            )
            return
        QApplication.clipboard().setText(payload)
        self.status.setText("คัดลอกลิงก์และข้อความลงคลิปบอร์ดแล้ว (พร้อมกด Ctrl+V)")

    def share_to_selected_pages(self):
        targets = self.checked_targets()
        if not targets:
            QMessageBox.information(
                self,
                "ยังไม่ได้เลือกเพจ",
                "กรุณาติ๊กเลือกเพจทางซ้ายอย่างน้อย 1 เพจที่ต้องการแชร์ลงไป",
            )
            return
        url = self.input_share_url.text().strip()
        if not url or not _is_valid_http_url(url):
            QMessageBox.warning(
                self,
                "ลิงก์โพสต์ไม่ถูกต้อง",
                "กรุณาวางลิงก์โพสต์ที่ขึ้นต้นด้วย https:// หรือ http:// ก่อนแชร์ลงเพจ",
            )
            return
        caption = self.editor_share_caption.toPlainText().strip()
        if len(caption) > FB_MAX_CHARS:
            QMessageBox.warning(
                self,
                "ข้อความยาวเกินไป",
                f"แคปชันประกอบต้องไม่เกิน {FB_MAX_CHARS:,} ตัวอักษร",
            )
            return

        def on_complete(_results, errs):
            if not errs:
                self.editor_share_caption.clear()
                self.status.setText(f"แชร์โพสต์ลงเพจที่เลือกสำเร็จ {len(targets)} เพจ")

        self._execute_post_batch(
            targets=targets,
            message=caption,
            link=url,
            images=[],
            video_path="",
            scheduled_dt=None,
            page_delay_min=float(self.spin_page_delay_min.value()),
            page_delay_max=float(self.spin_page_delay_max.value()),
            watermark_text="",
            clear_composer_on_success=False,
            on_complete=on_complete,
        )

    def open_checked_groups_for_share(self):
        groups = self.checked_saved_groups()
        if not groups:
            QMessageBox.information(
                self,
                "ยังไม่ได้เลือกกลุ่ม",
                "กรุณาติ๊กเลือกกลุ่มทางซ้ายอย่างน้อย 1 กลุ่มก่อน",
            )
            return
        payload = self._build_share_clipboard_text()
        if not payload:
            QMessageBox.information(
                self,
                "ยังไม่มีลิงก์หรือข้อความ",
                "กรุณาใส่ลิงก์โพสต์หรือกด 'ดึงโพสต์ล่าสุดจากประวัติ' ก่อน",
            )
            return
        if len(groups) > 5:
            reply = QMessageBox.question(
                self,
                "เปิดหลายแท็บพร้อมกัน",
                f"คุณเลือกไว้ {len(groups)} กลุ่ม ต้องการเปิดเบราว์เซอร์ทั้งหมด {len(groups)} แท็บพร้อมกันใช่หรือไม่?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if reply != QMessageBox.Yes:
                return

        QApplication.clipboard().setText(payload)
        opened = 0
        for g in groups:
            try:
                clean_url = facebook.validate_facebook_group_url(g["url"])
                webbrowser.open(clean_url)
                opened += 1
            except ValueError:
                continue
        self.status.setText(
            f"คัดลอกลงคลิปบอร์ดและเปิด {opened} กลุ่มในเบราว์เซอร์แล้ว — กด Ctrl+V เพื่อวางได้เลย"
        )

    def open_facebook_share_dialog(self):
        url = self.input_share_url.text().strip()
        try:
            sharer_url = facebook.build_facebook_share_dialog_url(url)
        except ValueError as exc:
            QMessageBox.warning(self, "ลิงก์ไม่ถูกต้อง", str(exc))
            return
        payload = self._build_share_clipboard_text()
        if payload:
            QApplication.clipboard().setText(payload)
        webbrowser.open(sharer_url)
        self.status.setText("เปิดหน้าต่างแชร์ของ Facebook ในเบราว์เซอร์แล้ว")

    # --- Page Analytics, Page Comparison & RT Dashboard ---

    def _analytics_period_days(self) -> int:
        val = self.combo_analytics_period.currentData()
        try:
            return max(1, min(int(val or 7), 90))
        except (TypeError, ValueError):
            return 7

    def _analytics_target_accounts(self) -> list[tuple[str, str, str]]:
        scope = self.combo_analytics_scope.currentData()
        if scope == "selected":
            targets = self.checked_targets()
            return [t for t in targets if t[0] == facebook.PLATFORM]
        return [t for t in db.list_accounts() if t[0] == facebook.PLATFORM]

    def _filtered_analytics_rows(self, rows: list[dict]) -> list[dict]:
        scope = self.combo_analytics_scope.currentData()
        if scope != "selected":
            return list(rows)
        selected_ids = {pid for pl, pid, _ in self.checked_targets() if pl == facebook.PLATFORM}
        return [r for r in rows if r.get("page_id") in selected_ids]

    def _load_cached_analytics(self):
        period_days = self._analytics_period_days()
        cached = db.get_latest_page_analytics(period_days=period_days)
        self.analytics_rows = cached
        self._render_analytics_dashboard(cached)
        if cached:
            latest_ts = max((str(r.get("fetched_at") or "") for r in cached), default="")
            if latest_ts:
                self.lbl_analytics_updated.setText(f"ข้อมูลล่าสุด (แคช): {latest_ts[:19].replace('T', ' ')}")
        else:
            self.lbl_analytics_updated.setText("ยังไม่ได้ดึงข้อมูลสด — กด 'รีเฟรชข้อมูลตอนนี้'")

    def _on_analytics_filter_changed(self, _index: int = -1):
        self._render_analytics_dashboard(self.analytics_rows)

    def _on_analytics_period_changed(self, _index: int = -1):
        self._load_cached_analytics()

    def _on_analytics_auto_refresh_changed(self, *_args):
        if not hasattr(self, "analytics_timer"):
            return
        if self.chk_analytics_auto_refresh.isChecked():
            secs = int(self.combo_analytics_interval.currentData() or 300)
            self.analytics_timer.setInterval(max(30, secs) * 1000)
            self.analytics_timer.start()
            self.status.setText(f"เปิด Auto Refresh RT Dashboard แล้ว (ทุก {secs // 60} นาที)")
        else:
            self.analytics_timer.stop()
            self.status.setText("ปิด Auto Refresh RT Dashboard แล้ว")

    def _on_analytics_auto_refresh_tick(self):
        if self.analytics_worker and self.analytics_worker.isRunning():
            return
        if not self._analytics_target_accounts():
            return
        self.refresh_page_analytics(silent=True)

    @staticmethod
    def _format_delta_label(delta: float | None) -> str:
        if delta is None:
            return "—"
        sign = "+" if delta > 0 else ""
        return f"{sign}{delta:.1f}%"

    def _render_analytics_dashboard(self, rows: list[dict]):
        visible = self._filtered_analytics_rows(rows)
        summary = facebook.analyze_page_comparison(visible)

        self.rt_card_reach.set_value(f"{summary['total_reach']:,}")
        self.rt_card_engagement.set_value(f"{summary['total_engagement']:,}")
        self.rt_card_impressions.set_value(
            f"{summary['total_impressions']:,} / {summary['total_link_clicks']:,}"
        )
        issue_count = len(summary["stale_or_no_data"])
        self.rt_card_followers.set_value(f"{summary['total_followers']:,} / {issue_count}")
        self.lbl_analytics_count.setText(f"{len(visible)} เพจ")

        if not visible:
            self.lbl_cmp_top_reach.setText("🏆 Reach สูงสุด: —")
            self.lbl_cmp_top_engagement.setText("🔥 Engagement สูงสุด: —")
            self.lbl_cmp_declining.setText("📉 Reach ลดลง: —")
            self.lbl_cmp_growing.setText("📈 เพจที่มีการเติบโต: —")
            self.lbl_cmp_stale.setText(
                "⚠️ ไม่มีข้อมูลอัปเดต / มีปัญหา: — (กด 'รีเฟรชข้อมูลตอนนี้' หรือเลือกเพจทางซ้าย)"
            )
        else:
            top_r = summary["top_reach"]
            self.lbl_cmp_top_reach.setText(
                f"🏆 Reach สูงสุด: {top_r['page_name']} ({int(top_r['reach']):,})"
                if top_r
                else "🏆 Reach สูงสุด: —"
            )
            top_e = summary["top_engagement"]
            self.lbl_cmp_top_engagement.setText(
                f"🔥 Engagement สูงสุด: {top_e['page_name']} ({int(top_e['engagement']):,})"
                if top_e
                else "🔥 Engagement สูงสุด: —"
            )
            declining = summary["declining_reach"]
            if declining:
                d_txt = ", ".join(
                    f"{r['page_name']} ({self._format_delta_label(r.get('reach_delta_pct'))})"
                    for r in declining[:5]
                )
                if len(declining) > 5:
                    d_txt += f" (+อีก {len(declining) - 5} เพจ)"
                self.lbl_cmp_declining.setText(f"📉 Reach ลดลง ({len(declining)} เพจ): {d_txt}")
            else:
                self.lbl_cmp_declining.setText("📉 Reach ลดลง: ไม่มีเพจที่ Reach ลดลง")

            growing = summary["growing_pages"]
            if growing:
                g_parts = []
                for r in growing[:5]:
                    rd = r.get("reach_delta_pct")
                    ed = r.get("engagement_delta_pct")
                    pct = rd if (rd is not None and rd > 0) else ed
                    g_parts.append(f"{r['page_name']} ({self._format_delta_label(pct)})")
                g_txt = ", ".join(g_parts)
                if len(growing) > 5:
                    g_txt += f" (+อีก {len(growing) - 5} เพจ)"
                self.lbl_cmp_growing.setText(f"📈 เพจที่มีการเติบโต ({len(growing)} เพจ): {g_txt}")
            else:
                self.lbl_cmp_growing.setText("📈 เพจที่มีการเติบโต: —")

            stale = summary["stale_or_no_data"]
            if stale:
                s_parts = []
                for r in stale[:5]:
                    tag = "Error" if r.get("status") == "error" else "ไม่มีข้อมูลอัปเดต"
                    s_parts.append(f"{r['page_name']} ({tag})")
                s_txt = ", ".join(s_parts)
                if len(stale) > 5:
                    s_txt += f" (+อีก {len(stale) - 5} เพจ)"
                self.lbl_cmp_stale.setText(f"⚠️ ไม่มีข้อมูลอัปเดต / มีปัญหา ({len(stale)} เพจ): {s_txt}")
            else:
                self.lbl_cmp_stale.setText("⚠️ ไม่มีข้อมูลอัปเดต / มีปัญหา: ทุกเพจปกติ ✅")

        # Populate sortable breakdown table
        was_sorting = self.tbl_analytics.isSortingEnabled()
        self.tbl_analytics.setSortingEnabled(False)
        self.tbl_analytics.setRowCount(0)
        for row_idx, r in enumerate(visible):
            self.tbl_analytics.insertRow(row_idx)
            p_plat = str(r.get("platform") or facebook.PLATFORM)
            p_id = str(r.get("page_id") or "")
            p_name = str(r.get("page_name") or p_id)
            name_item = QTableWidgetItem(p_name)
            name_item.setData(Qt.UserRole, (p_plat, p_id, p_name))
            name_item.setToolTip(f"{p_name} (ID: {p_id}) — ดับเบิลคลิกเพื่อเขียนโพสต์ / คลิกขวาเพื่อเปิดเมนู")
            self.tbl_analytics.setItem(row_idx, 0, name_item)

            reach_val = int(r.get("reach") or 0)
            self.tbl_analytics.setItem(
                row_idx, 1, _NumericTableWidgetItem(f"{reach_val:,}", reach_val)
            )

            r_delta = r.get("reach_delta_pct")
            delta_item = _NumericTableWidgetItem(
                self._format_delta_label(r_delta),
                float(r_delta) if r_delta is not None else -999999.0,
            )
            if r_delta is not None:
                if float(r_delta) > 0:
                    delta_item.setForeground(QColor("#30D158"))
                elif float(r_delta) < 0:
                    delta_item.setForeground(QColor("#FF453A"))
            self.tbl_analytics.setItem(row_idx, 2, delta_item)

            for col_idx, key in (
                (3, "impressions"),
                (4, "engagement"),
                (5, "reactions"),
                (6, "comments"),
                (7, "shares"),
                (8, "link_clicks"),
                (9, "followers"),
            ):
                val = int(r.get(key) or 0)
                self.tbl_analytics.setItem(
                    row_idx, col_idx, _NumericTableWidgetItem(f"{val:,}", val)
                )

            st = str(r.get("status") or "ok")
            err = str(r.get("error") or "").strip()
            if st == "error":
                st_text = f"❌ ผิดพลาด: {err or 'ดึงข้อมูลไม่สำเร็จ'}"
                st_color = QColor("#FF453A")
            elif st == "no_data":
                st_text = f"⚠️ ไม่มีข้อมูลอัปเดต ({err})" if err else "⚠️ ไม่มีข้อมูลอัปเดต"
                st_color = QColor("#FF9F0A")
            elif st == "partial":
                st_text = f"⚠️ ข้อมูลบางส่วน ({err})" if err else "⚠️ ข้อมูลบางส่วน"
                st_color = QColor("#FF9F0A")
            else:
                st_text = "✅ ปกติ"
                st_color = QColor("#30D158")
            st_item = QTableWidgetItem(st_text)
            st_item.setForeground(st_color)
            if err:
                st_item.setToolTip(err)
            self.tbl_analytics.setItem(row_idx, 10, st_item)

        self.tbl_analytics.setSortingEnabled(was_sorting)

    def _focus_page_in_composer(self, platform: str, page_id: str, page_name: str):
        """Select only the specified page in the left sidebar and jump to Composer."""
        matched = False
        self.accounts.blockSignals(True)
        for i in range(self.accounts.count()):
            it = self.accounts.item(i)
            pl, pid, _nm = it.data(Qt.UserRole)
            if pl == platform and pid == page_id:
                it.setCheckState(Qt.Checked)
                matched = True
            else:
                it.setCheckState(Qt.Unchecked)
        self.accounts.blockSignals(False)
        self._on_accounts_changed()
        self._switch_page(0)
        self.editor.setFocus()
        if matched:
            self.status.setText(f"เลือกเพจ '{page_name}' พร้อมเขียนโพสต์แล้ว")
        else:
            self.status.setText(f"สลับไปหน้าเขียนโพสต์แล้ว (ไม่พบเพจ '{page_name}' ในรายการ)")

    def _on_analytics_row_double_clicked(self, row: int, _col: int):
        item = self.tbl_analytics.item(row, 0)
        if item is None:
            return
        meta = item.data(Qt.UserRole)
        if not isinstance(meta, tuple) or len(meta) != 3:
            return
        platform, page_id, page_name = meta
        self._focus_page_in_composer(platform, page_id, page_name)

    def _open_page_on_facebook(self, page_id: str) -> bool:
        try:
            clean_pid = facebook._validate_graph_id(page_id, "Page ID")
        except ValueError as exc:
            QMessageBox.warning(self, "รหัสเพจไม่ถูกต้อง", str(exc))
            return False
        webbrowser.open(f"https://www.facebook.com/{clean_pid}")
        self.status.setText(f"เปิดหน้าเพจ {clean_pid} บน Facebook แล้ว")
        return True

    def _show_analytics_context_menu(self, pos):
        idx = self.tbl_analytics.indexAt(pos)
        if not idx.isValid():
            return
        row = idx.row()
        item = self.tbl_analytics.item(row, 0)
        if item is None:
            return
        meta = item.data(Qt.UserRole)
        if not isinstance(meta, tuple) or len(meta) != 3:
            return
        platform, page_id, page_name = meta
        menu = QMenu(self)
        act_compose = menu.addAction(f"เขียนโพสต์ลงเพจ '{page_name}' ทันที")
        act_open_fb = menu.addAction("เปิดหน้าเพจบน Facebook")
        act_check_tok = menu.addAction("ตรวจสถานะ Token เพจนี้")
        chosen = menu.exec(self.tbl_analytics.viewport().mapToGlobal(pos))
        if chosen == act_compose:
            self._focus_page_in_composer(platform, page_id, page_name)
        elif chosen == act_open_fb:
            self._open_page_on_facebook(page_id)
        elif chosen == act_check_tok:
            self.check_single_page_token_health(platform, page_id, page_name)

    def refresh_page_analytics(self, *, silent: bool = False):
        if self.analytics_worker and self.analytics_worker.isRunning():
            if not silent:
                self.status.setText("กำลังดึงข้อมูล Page Analytics อยู่แล้ว กรุณารอสักครู่…")
            return
        targets = self._analytics_target_accounts()
        if not targets:
            if not silent:
                QMessageBox.information(
                    self,
                    "ยังไม่มีเพจ",
                    "กรุณาเชื่อมต่อเพจ Facebook (หรือติ๊กเลือกเพจทางซ้ายหากเลือกโหมดเฉพาะเพจที่เลือก) ก่อนดึงสถิติ",
                )
            return

        period_days = self._analytics_period_days()
        cancel_ev = threading.Event()
        self.analytics_cancel_event = cancel_ev
        self.btn_analytics_refresh.setEnabled(False)
        self.btn_analytics_refresh.setText("  กำลังดึงข้อมูล…")
        if not silent:
            self.status.setText(f"กำลังดึงสถิติ Page Analytics ({len(targets)} เพจ, {period_days} วัน)…")

        def job():
            fetched_rows: list[dict] = []
            for pl, pid, pname in targets:
                if cancel_ev.is_set():
                    break
                prev_snap = db.get_previous_page_analytics(pl, pid, period_days=period_days)
                try:
                    data = facebook.get_page_analytics(
                        pid,
                        pname,
                        period_days=period_days,
                        prev_snapshot=prev_snap,
                    )
                except (RuntimeError, ValueError, OSError) as exc:
                    data = {
                        "platform": pl,
                        "page_id": pid,
                        "page_name": pname,
                        "period_days": period_days,
                        "reach": 0,
                        "impressions": 0,
                        "engagement": 0,
                        "reactions": 0,
                        "comments": 0,
                        "shares": 0,
                        "link_clicks": 0,
                        "followers": 0,
                        "prev_reach": 0,
                        "prev_engagement": 0,
                        "reach_delta_pct": None,
                        "engagement_delta_pct": None,
                        "latest_post_at": "",
                        "status": "error",
                        "error": str(exc),
                        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    }
                db.save_page_analytics_snapshot(
                    platform=data["platform"],
                    page_id=data["page_id"],
                    page_name=data["page_name"],
                    period_days=period_days,
                    reach=data["reach"],
                    impressions=data["impressions"],
                    engagement=data["engagement"],
                    reactions=data["reactions"],
                    comments=data["comments"],
                    shares=data["shares"],
                    link_clicks=data["link_clicks"],
                    followers=data["followers"],
                    prev_reach=data["prev_reach"],
                    prev_engagement=data["prev_engagement"],
                    reach_delta_pct=data["reach_delta_pct"],
                    engagement_delta_pct=data["engagement_delta_pct"],
                    latest_post_at=data["latest_post_at"],
                    status=data["status"],
                    error=data["error"],
                    fetched_at=data["fetched_at"],
                )
                fetched_rows.append(data)
            return db.get_latest_page_analytics(period_days=period_days), len(fetched_rows)

        def on_ok(payload):
            all_latest, refreshed_cnt = payload
            # Guard against race condition if user switched period_days while worker was running
            if self._analytics_period_days() != period_days:
                self._load_cached_analytics()
                if not silent:
                    self.status.setText(
                        f"บันทึกสถิติช่วง {period_days} วันแล้ว {refreshed_cnt} เพจ (กำลังแสดงช่วง {self._analytics_period_days()} วัน)"
                    )
                return
            self.analytics_rows = all_latest
            self._render_analytics_dashboard(all_latest)
            now_str = datetime.now().strftime("%H:%M:%S")
            self.lbl_analytics_updated.setText(f"อัปเดตล่าสุด: {now_str} ({period_days} วัน)")
            if not silent:
                self.status.setText(f"อัปเดต RT Dashboard & Page Analytics สำเร็จ {refreshed_cnt} เพจ")

        def on_err(err_msg: str):
            if not silent:
                QMessageBox.warning(self, "ดึงข้อมูล Page Analytics ไม่สำเร็จ", err_msg)
            self.status.setText(f"ดึงข้อมูลสถิติไม่สำเร็จ: {err_msg}")

        def on_done():
            self.btn_analytics_refresh.setEnabled(True)
            self.btn_analytics_refresh.setText("  รีเฟรชข้อมูลตอนนี้")
            if self.analytics_worker is w:
                self.analytics_worker = None

        w = Worker(job)
        self.analytics_worker = w
        w.done.connect(on_ok)
        w.failed.connect(on_err)
        w.finished.connect(on_done)
        w.start()

    def export_analytics_csv(self):
        visible = self._filtered_analytics_rows(self.analytics_rows)
        if not visible:
            QMessageBox.information(
                self,
                "ยังไม่มีข้อมูลสถิติ",
                "กรุณากด 'รีเฟรชข้อมูลตอนนี้' เพื่อดึงสถิติเพจก่อนส่งออก CSV",
            )
            return
        default_name = f"page_analytics_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        path, _ = QFileDialog.getSaveFileName(
            self, "ส่งออกรายงาน Page Analytics (CSV)", default_name, "CSV (*.csv)"
        )
        if not path:
            return
        dest_path = Path(path)
        tmp_path = dest_path.parent / f".{dest_path.name}.tmp_{os.getpid()}_{uuid.uuid4().hex}"
        try:
            with open(tmp_path, "w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(
                    [
                        "Page ID",
                        "Page Name",
                        "Period (Days)",
                        "Reach",
                        "Reach Delta (%)",
                        "Impressions",
                        "Engagement",
                        "Reactions",
                        "Comments",
                        "Shares",
                        "Link Clicks",
                        "Followers",
                        "Status",
                        "Error",
                        "Fetched At",
                    ]
                )
                for r in visible:
                    writer.writerow(
                        [
                            r.get("page_id", ""),
                            r.get("page_name", ""),
                            r.get("period_days", self._analytics_period_days()),
                            int(r.get("reach") or 0),
                            "" if r.get("reach_delta_pct") is None else r.get("reach_delta_pct"),
                            int(r.get("impressions") or 0),
                            int(r.get("engagement") or 0),
                            int(r.get("reactions") or 0),
                            int(r.get("comments") or 0),
                            int(r.get("shares") or 0),
                            int(r.get("link_clicks") or 0),
                            int(r.get("followers") or 0),
                            r.get("status", ""),
                            r.get("error", ""),
                            r.get("fetched_at", ""),
                        ]
                    )
            os.replace(tmp_path, dest_path)
            self.status.setText(
                f"ส่งออกรายงาน Page Analytics ({len(visible)} เพจ) ไปยัง {dest_path.name} แล้ว"
            )
        except OSError as exc:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            QMessageBox.warning(self, "ส่งออก CSV ไม่สำเร็จ", str(exc))

    # --- Auto-Backup & Keyword Auto-Reply Orchestration ---

    def _run_startup_auto_backup(self):
        try:
            db.run_daily_auto_backup(keep_days=7)
        except (OSError, sqlite3.Error, ValueError) as exc:
            self._startup_backup_warning = f"สำรองฐานข้อมูลอัตโนมัติรายวันไม่สำเร็จ: {exc}"

    def _sync_auto_reply_timer(self):
        enabled = config.get_auto_reply_enabled()
        iv_sec = config.get_auto_reply_interval_sec()
        active_rules = db.list_auto_reply_rules(only_enabled=True)
        total_replied = db.count_auto_replies()
        if hasattr(self, "auto_reply_timer"):
            self.auto_reply_timer.setInterval(iv_sec * 1000)
            if enabled and active_rules:
                if not self.auto_reply_timer.isActive():
                    self.auto_reply_timer.start()
            else:
                self.auto_reply_timer.stop()

        if hasattr(self, "lbl_auto_reply_status"):
            if enabled and active_rules:
                mins = max(1, iv_sec // 60)
                self.lbl_auto_reply_status.setText(
                    f"🤖 ตอบอัตโนมัติ: เปิดอยู่ ({len(active_rules)} กฎ • ทุก {mins} นาที • ตอบแล้ว {total_replied:,})"
                )
            elif enabled and not active_rules:
                self.lbl_auto_reply_status.setText(
                    "🤖 ตอบอัตโนมัติ: เปิดอยู่แต่ยังไม่มีกฎที่เปิดใช้งาน"
                )
            else:
                self.lbl_auto_reply_status.setText(
                    f"🤖 ตอบคอมเมนต์อัตโนมัติ: ปิดอยู่ ({len(active_rules)} กฎ • ตอบสะสม {total_replied:,})"
                )

    def open_auto_reply_rules_dialog(self):
        dlg = AutoReplyRulesDialog(parent=self)
        dlg.exec()
        self._sync_auto_reply_timer()
        if dlg.run_now_requested:
            self.run_auto_reply_now(manual=True)

    def _on_auto_reply_timer_tick(self):
        if QApplication.activeModalWidget() is not None:
            return
        if not config.get_auto_reply_enabled():
            self._sync_auto_reply_timer()
            return
        self.run_auto_reply_now(manual=False)

    def run_auto_reply_now(self, *, manual: bool = False):
        if self.auto_reply_worker and self.auto_reply_worker.isRunning():
            if manual:
                self.status.setText("กำลังสแกนและตอบคอมเมนต์อัตโนมัติอยู่แล้ว…")
            return
        rules = db.list_auto_reply_rules(only_enabled=True)
        if not rules:
            if manual:
                QMessageBox.information(
                    self,
                    "ยังไม่มีกฎคีย์เวิร์ด",
                    "กรุณากด 'ตั้งค่ากฎคีย์เวิร์ด…' เพื่อเพิ่มกฎอย่างน้อย 1 ข้อก่อน",
                )
            return
        fb_pages = [
            (pid, name)
            for pl, pid, name in self.checked_targets()
            if pl == facebook.PLATFORM
        ]
        if not fb_pages:
            if manual:
                QMessageBox.information(
                    self,
                    "ยังไม่ได้เลือกเพจ",
                    "กรุณาติ๊กเลือกเพจทางซ้ายอย่างน้อย 1 เพจที่ต้องการให้สแกนตอบคอมเมนต์",
                )
            return

        cancel_ev = threading.Event()
        self.auto_reply_cancel_event = cancel_ev
        if hasattr(self, "btn_run_auto_reply_now"):
            self.btn_run_auto_reply_now.setEnabled(False)
        if manual:
            self.status.setText(
                f"กำลังสแกนโพสต์ล่าสุดของ {len(fb_pages)} เพจเพื่อตอบคอมเมนต์ตามคีย์เวิร์ด…"
            )

        def job():
            return facebook.run_keyword_auto_reply_pass(
                fb_pages,
                rules,
                posts_per_page=5,
                comments_per_post=25,
                delay_seconds=0.8,
                cancel_event=cancel_ev,
            )

        w = Worker(job)
        self.auto_reply_worker = w

        def on_ok(summary: dict):
            if self.auto_reply_worker is w:
                self.auto_reply_worker = None
            if hasattr(self, "btn_run_auto_reply_now"):
                self.btn_run_auto_reply_now.setEnabled(True)
            self._sync_auto_reply_timer()
            replied = int(summary.get("replied_count") or 0)
            hidden = int(summary.get("hidden_count") or 0)
            scanned = int(summary.get("comments_scanned") or 0)
            errs = list(summary.get("errors") or [])
            msg = (
                f"ตรวจคอมเมนต์เสร็จสิ้น ({scanned} เม้นต์): ตอบกลับอัตโนมัติ {replied} รายการ"
                + (f", ซ่อน {hidden} รายการ" if hidden else "")
            )
            if errs:
                msg += f" (พบข้อผิดพลาด {len(errs)} จุด)"
            if manual or replied > 0:
                self.status.setText(msg)
            if replied > 0 and not self.isVisible():
                self._notify_tray(f"{APP_NAME} — ตอบคอมเมนต์อัตโนมัติ", msg)
            if manual and errs and self.isVisible():
                QMessageBox.warning(
                    self,
                    "พบข้อผิดพลาดบางส่วนขณะตอบคอมเมนต์อัตโนมัติ",
                    "\n".join(errs[:8]),
                )

        def on_err(err_msg: str):
            if self.auto_reply_worker is w:
                self.auto_reply_worker = None
            if hasattr(self, "btn_run_auto_reply_now"):
                self.btn_run_auto_reply_now.setEnabled(True)
            self._sync_auto_reply_timer()
            if manual:
                self.status.setText(f"ตอบคอมเมนต์อัตโนมัติล้มเหลว: {err_msg}")
                if self.isVisible():
                    QMessageBox.warning(self, "ตอบคอมเมนต์อัตโนมัติไม่สำเร็จ", err_msg)

        w.done.connect(on_ok)
        w.failed.connect(on_err)
        w.finished.connect(w.deleteLater)
        w.start()

    # --- Clean Shutdown ---

    def closeEvent(self, event):
        self._save_draft()
        if (
            config.get_minimize_to_tray()
            and not self._force_quit
            and QSystemTrayIcon.isSystemTrayAvailable()
            and self.isVisible()
        ):
            event.ignore()
            self.hide()
            self._notify_tray(
                f"{APP_NAME} ยังทำงานอยู่เบื้องหลัง",
                "โปรแกรมถูกย่อเก็บลง System Tray เพื่อรอส่งคิวโพสต์ตั้งเวลา (คลิกไอคอนเพื่อเปิดใหม่)",
            )
            return
        if hasattr(self, "tray_icon"):
            self.tray_icon.hide()
        self.queue_timer.stop()
        if hasattr(self, "analytics_timer"):
            self.analytics_timer.stop()
        if hasattr(self, "auto_reply_timer"):
            self.auto_reply_timer.stop()
        if self.oauth_cancel_event:
            self.oauth_cancel_event.set()
        if self.task_cancel_event:
            self.task_cancel_event.set()
        if self.analytics_cancel_event:
            self.analytics_cancel_event.set()
        if self.token_check_cancel_event:
            self.token_check_cancel_event.set()
        if self.auto_reply_cancel_event:
            self.auto_reply_cancel_event.set()
        if self.worker and self.worker.isRunning():
            _retire_worker(self.worker, wait_ms=3000)
            self.worker = None
        if self.analytics_worker and self.analytics_worker.isRunning():
            _retire_worker(self.analytics_worker, wait_ms=3000)
            self.analytics_worker = None
        if self.token_check_worker and self.token_check_worker.isRunning():
            _retire_worker(self.token_check_worker, wait_ms=3000)
            self.token_check_worker = None
        if self.auto_reply_worker and self.auto_reply_worker.isRunning():
            _retire_worker(self.auto_reply_worker, wait_ms=3000)
            self.auto_reply_worker = None
        super().closeEvent(event)
        app_inst = QApplication.instance()
        if app_inst is not None and not app_inst.quitOnLastWindowClosed():
            app_inst.quit()


def _configure_app_font(app: QApplication) -> None:
    """Load bundled loopless Thai font (Prompt) and prefer modern loopless Thai fonts."""
    base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    fonts_dir = base_dir / "assets" / "fonts"
    if fonts_dir.is_dir():
        for font_file in sorted(fonts_dir.glob("*.ttf")):
            QFontDatabase.addApplicationFont(str(font_file))
    available = set(QFontDatabase.families())
    for fam in ("Prompt", "Sukhumvit Set", "Noto Sans Thai", "Kanit", "IBM Plex Sans Thai", "Anuphan"):
        if fam in available:
            font = QFont(fam, 10)
            font.setFamilies([fam, "Segoe UI Emoji", "Segoe UI Symbol", "Noto Color Emoji"])
            app.setFont(font)
            return


def _maybe_redirect_to_launcher(argv: list[str] | None = None) -> bool:
    """When frozen SocialManager.exe is opened directly, launch SocialManagerLauncher.exe first."""
    args = list(sys.argv if argv is None else argv)
    if not getattr(sys, "frozen", False):
        return False
    if any(flag in args for flag in ("--from-launcher", "--no-launcher", "--smoke-test", "--version")):
        return False
    launcher_exe = Path(sys.executable).resolve().parent / "SocialManagerLauncher.exe"
    if not launcher_exe.is_file():
        return False
    import subprocess

    try:
        subprocess.Popen([str(launcher_exe)], cwd=str(launcher_exe.parent))
        return True
    except OSError:
        return False


if __name__ == "__main__":
    if "--version" in sys.argv:
        print(APP_VERSION)
        sys.exit(0)
    if "--smoke-test" in sys.argv:
        import tempfile

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        with tempfile.TemporaryDirectory() as _smoke_dir:
            db.DB_PATH = Path(_smoke_dir) / "smoke.db"
            config.SETTINGS_PATH = Path(_smoke_dir) / "settings.json"
            db.init()
            db.recover_stuck_scheduled_jobs()
            app = QApplication(sys.argv)
            app.setQuitOnLastWindowClosed(False)
            app.setStyle("Fusion")
            _configure_app_font(app)
            w = MainWindow()
            for _page_idx in range(6):
                w._switch_page(_page_idx)
            app.processEvents()
            for _dlg in (
                SettingsDialog(w),
                PostPresetsDialog(w._snapshot_composer_for_preset(), parent=w),
                AutoReplyRulesDialog(parent=w),
            ):
                _dlg.close()
            w.quit_application()
            app.processEvents()
        sys.exit(0)

    if _maybe_redirect_to_launcher(sys.argv):
        sys.exit(0)

    db.init()
    db.recover_stuck_scheduled_jobs()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")
    _configure_app_font(app)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
