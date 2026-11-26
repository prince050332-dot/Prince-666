"""iOS-inspired Dark & Light QSS design system with modern loopless Thai typography (Prompt / Sukhumvit Set)."""

FONT_STACK = '"Prompt", "Sukhumvit Set", "Noto Sans Thai", "Kanit", "Segoe UI Emoji", "Segoe UI Symbol", sans-serif'

DARK_QSS = f"""
QMainWindow, QDialog {{
    background-color: #0d0d10;
    color: #f5f5f7;
    font-family: {FONT_STACK};
    font-size: 13px;
}}
QWidget {{
    color: #f5f5f7;
    font-family: {FONT_STACK};
    font-size: 13px;
}}
QScrollArea, QScrollArea > QWidget > QWidget {{
    background: transparent;
    border: none;
}}

/* iOS Sidebar Shell */
QFrame#sidebar {{
    background-color: #161618;
    border-right: 1px solid #2c2c2e;
}}
QLabel#brandTitle {{
    font-size: 17px;
    font-weight: 700;
    color: #ffffff;
}}
QLabel#brandSub {{
    font-size: 12px;
    color: #8e8e93;
}}

/* iOS Sidebar Navigation Pills */
QPushButton#navBtn {{
    background-color: transparent;
    color: #aeaeb2;
    border: none;
    border-radius: 11px;
    padding: 7px 12px;
    text-align: left;
    font-weight: 600;
    font-size: 13px;
}}
QPushButton#navBtn:hover {{
    background-color: rgba(255, 255, 255, 0.07);
    color: #ffffff;
}}
QPushButton#navBtn[active="true"] {{
    background-color: rgba(10, 132, 255, 0.20);
    color: #0a84ff;
}}

/* iOS Squircle Cards & Surfaces */
QFrame#card, QFrame#statCard {{
    background-color: #1c1c1e;
    border: 1px solid #2c2c2e;
    border-radius: 16px;
}}
QFrame#fbPreviewCard {{
    background-color: #1c1c1e;
    border: 1px solid #2c2c2e;
    border-radius: 18px;
}}
QFrame#fbLinkCard {{
    background-color: #242426;
    border: 1px solid #38383a;
    border-radius: 12px;
}}
QFrame#dropZone {{
    background-color: #161618;
    border: 1.5px dashed #3a3a3c;
    border-radius: 14px;
}}
QFrame#dropZone[dragActive="true"] {{
    background-color: rgba(10, 132, 255, 0.14);
    border: 1.5px dashed #0a84ff;
}}
QFrame#thumbCard {{
    background-color: #242426;
    border: 1px solid #38383a;
    border-radius: 12px;
}}
QFrame#scheduleCard {{
    background-color: #242426;
    border: 1px solid #323234;
    border-radius: 14px;
}}
QFrame#hairline {{
    background-color: #2c2c2e;
    max-height: 1px;
    border: none;
}}

/* Typography */
QLabel#pageHeading {{
    font-size: 20px;
    font-weight: 700;
    color: #ffffff;
}}
QLabel#sectionTitle {{
    font-size: 12px;
    font-weight: 600;
    color: #98989d;
}}
QLabel#mutedLabel, QLabel#statLabel {{
    color: #8e8e93;
    font-size: 12px;
}}
QLabel#statValue {{
    font-size: 21px;
    font-weight: 700;
    color: #ffffff;
}}
QLabel#dropMainLabel {{
    font-weight: 600;
    color: #e5e5ea;
}}
QLabel#fbPageName {{
    font-size: 14px;
    font-weight: 700;
    color: #ffffff;
}}
QLabel#fbBodyText {{
    font-size: 13px;
    color: #f2f2f7;
}}
QLabel#fbBodyText[placeholder="true"] {{
    color: #8e8e93;
    font-style: italic;
}}
QLabel#fbLinkDomain {{
    font-size: 11px;
    font-weight: 700;
    color: #0a84ff;
}}
QLabel#fbLinkUrl {{
    font-size: 12px;
    color: #d1d1d6;
}}
QLabel#fbPhotoTile {{
    background-color: #141416;
    border-radius: 10px;
}}
QLabel#fbMoreBadge {{
    background-color: rgba(10, 132, 255, 0.20);
    color: #64d2ff;
    border-radius: 10px;
    padding: 3px 10px;
    font-size: 12px;
    font-weight: 600;
}}
QLabel#fbFooterAction {{
    color: #98989d;
    font-size: 12px;
    font-weight: 600;
    padding: 4px;
}}
QLabel#statusBarLabel {{
    background-color: #1c1c1e;
    border: 1px solid #2c2c2e;
    border-radius: 12px;
    padding: 6px 14px;
    color: #98989d;
    font-size: 12px;
}}

/* iOS Rounded Inputs */
QPlainTextEdit, QLineEdit, QDateTimeEdit, QComboBox {{
    background-color: #242426;
    color: #ffffff;
    border: 1px solid #38383a;
    border-radius: 11px;
    padding: 6px 10px;
    selection-background-color: #0a84ff;
    selection-color: #ffffff;
}}
QSpinBox {{
    background-color: #242426;
    color: #ffffff;
    border: 1px solid #38383a;
    border-radius: 11px;
    padding: 5px 8px;
    selection-background-color: #0a84ff;
    selection-color: #ffffff;
}}
QPlainTextEdit:focus, QLineEdit:focus, QDateTimeEdit:focus, QComboBox:focus, QSpinBox:focus {{
    border: 1px solid #0a84ff;
}}
QComboBox::drop-down, QDateTimeEdit::drop-down {{
    border: none;
    background: transparent;
    width: 20px;
}}
QComboBox QAbstractItemView {{
    background-color: #1c1c1e;
    color: #f5f5f7;
    border: 1px solid #38383a;
    border-radius: 10px;
    padding: 4px;
    selection-background-color: #0a84ff;
    selection-color: #ffffff;
    outline: none;
}}
QComboBox QAbstractItemView::item {{
    padding: 6px 10px;
    border-radius: 6px;
    min-height: 22px;
    color: #f5f5f7;
    background-color: transparent;
}}
QComboBox QAbstractItemView::item:hover,
QComboBox QAbstractItemView::item:selected {{
    background-color: #0a84ff;
    color: #ffffff;
}}
QMenu {{
    background-color: #1c1c1e;
    color: #f5f5f7;
    border: 1px solid #38383a;
    border-radius: 10px;
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 14px;
    border-radius: 6px;
}}
QMenu::item:selected {{
    background-color: #0a84ff;
    color: #ffffff;
}}

/* iOS Dark Calendar Popup */
QCalendarWidget QWidget {{
    background-color: #1c1c1e;
    color: #f5f5f7;
    alternate-background-color: #242426;
}}
QCalendarWidget QWidget#qt_calendar_navigationbar {{
    background-color: #242426;
    border-bottom: 1px solid #38383a;
    padding: 4px;
}}
QCalendarWidget QToolButton {{
    background-color: transparent;
    color: #f5f5f7;
    border: none;
    border-radius: 8px;
    padding: 4px 10px;
    font-weight: 700;
}}
QCalendarWidget QToolButton:hover {{
    background-color: rgba(10, 132, 255, 0.22);
    color: #0a84ff;
}}
QCalendarWidget QSpinBox {{
    background-color: #1c1c1e;
    color: #f5f5f7;
    border: 1px solid #38383a;
    border-radius: 8px;
    padding: 2px 6px;
}}
QCalendarWidget QAbstractItemView:enabled {{
    background-color: #1c1c1e;
    color: #f5f5f7;
    selection-background-color: #0a84ff;
    selection-color: #ffffff;
    outline: none;
}}
QCalendarWidget QAbstractItemView:disabled {{
    color: #636366;
}}
QLineEdit#searchInput {{
    background-color: #2c2c2e;
    border: none;
    padding: 7px 12px;
    border-radius: 12px;
    font-size: 12px;
}}
QLineEdit#searchInput:focus {{
    border: 1.5px solid #0a84ff;
}}

/* Lists & Tables */
QListWidget {{
    background-color: transparent;
    border: none;
    outline: none;
}}
QListWidget::item {{
    padding: 7px 10px;
    margin: 2px 0px;
    border-radius: 11px;
    color: #f5f5f7;
}}
QListWidget::item:hover {{
    background-color: rgba(255, 255, 255, 0.06);
}}
QListWidget::item:selected {{
    background-color: rgba(10, 132, 255, 0.22);
    color: #ffffff;
}}

QTableWidget {{
    background-color: #161618;
    alternate-background-color: #1c1c1e;
    color: #f5f5f7;
    border: 1px solid #2c2c2e;
    border-radius: 14px;
    gridline-color: #2c2c2e;
    selection-background-color: rgba(10, 132, 255, 0.26);
    selection-color: #ffffff;
}}
QTableWidget::item {{
    padding: 6px 10px;
}}
QHeaderView::section {{
    background-color: #1c1c1e;
    color: #98989d;
    border: none;
    border-bottom: 1px solid #2c2c2e;
    padding: 8px 10px;
    font-weight: 700;
    font-size: 12px;
}}

/* iOS Capsule / Pill Buttons */
QPushButton {{
    background-color: #2c2c2e;
    color: #f5f5f7;
    border: none;
    border-radius: 13px;
    padding: 6px 13px;
    font-weight: 600;
}}
QPushButton:hover {{
    background-color: #3a3a3c;
}}
QPushButton:pressed {{
    background-color: #48484a;
}}
QPushButton:disabled {{
    background-color: #242426;
    color: #636366;
}}

/* iOS Filled Primary Pill Button */
QPushButton#primaryBtn {{
    background-color: #0a84ff;
    border: none;
    border-radius: 13px;
    color: #ffffff;
    padding: 8px 16px;
    font-size: 13px;
    font-weight: 700;
}}
QPushButton#primaryBtn:hover {{
    background-color: #2693ff;
}}
QPushButton#primaryBtn:pressed {{
    background-color: #006ee6;
}}
QPushButton#primaryBtn:disabled {{
    background-color: rgba(10, 132, 255, 0.28);
    color: rgba(255, 255, 255, 0.45);
}}

/* iOS Tinted Secondary Pill Button */
QPushButton#ghostBtn {{
    background-color: rgba(10, 132, 255, 0.15);
    color: #409cff;
    border: none;
    border-radius: 13px;
    padding: 6px 12px;
    font-size: 12px;
    font-weight: 600;
}}
QPushButton#ghostBtn:hover {{
    background-color: rgba(10, 132, 255, 0.25);
    color: #6cb4ff;
}}
QPushButton#ghostBtn:pressed {{
    background-color: rgba(10, 132, 255, 0.32);
}}
QPushButton#ghostBtn:disabled {{
    background-color: #242426;
    color: #636366;
}}

/* iOS Segmented Filter Pill */
QPushButton#pillBtn {{
    background-color: #2c2c2e;
    color: #aeaeb2;
    border: none;
    border-radius: 13px;
    padding: 5px 12px;
    font-size: 12px;
    font-weight: 600;
}}
QPushButton#pillBtn:hover {{
    background-color: #3a3a3c;
    color: #ffffff;
}}
QPushButton#pillBtn[active="true"] {{
    background-color: #0a84ff;
    color: #ffffff;
}}

/* iOS Destructive Tinted Pill Button */
QPushButton#dangerBtn {{
    background-color: rgba(255, 69, 58, 0.16);
    border: none;
    border-radius: 13px;
    color: #ff453a;
    padding: 6px 12px;
    font-size: 12px;
    font-weight: 600;
}}
QPushButton#dangerBtn:hover {{
    background-color: rgba(255, 69, 58, 0.26);
    color: #ff6961;
}}
QPushButton#dangerBtn:pressed {{
    background-color: rgba(255, 69, 58, 0.36);
}}
QPushButton#dangerBtn:disabled {{
    background-color: #242426;
    color: #636366;
}}

QPushButton#thumbRemoveBtn {{
    background-color: #ff453a;
    color: #ffffff;
    border: none;
    border-radius: 10px;
    padding: 0px;
    font-weight: 700;
    font-size: 13px;
}}
QPushButton#thumbRemoveBtn:hover {{
    background-color: #ff6961;
}}

/* iOS Segmented Tabs (Settings Wizard) */
QTabWidget::pane {{
    border: 1px solid #2c2c2e;
    border-radius: 16px;
    background-color: #1c1c1e;
    padding: 8px;
}}
QTabBar::tab {{
    background-color: #242426;
    color: #98989d;
    border: none;
    border-radius: 12px;
    padding: 8px 16px;
    margin-right: 6px;
    margin-bottom: 6px;
    font-weight: 600;
}}
QTabBar::tab:selected {{
    background-color: #0a84ff;
    color: #ffffff;
}}

/* Checkboxes & Slim Scrollbars */
QCheckBox {{
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    border-radius: 9px;
    border: 1.5px solid #48484a;
    background-color: #242426;
}}
QCheckBox::indicator:hover {{
    border-color: #0a84ff;
}}
QCheckBox::indicator:checked {{
    background-color: #0a84ff;
    border-color: #0a84ff;
}}
QScrollBar:vertical, QScrollBar:horizontal {{
    background: transparent;
    width: 7px;
    height: 7px;
    margin: 0px;
}}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background: #3a3a3c;
    border-radius: 3.5px;
    min-height: 24px;
    min-width: 24px;
}}
QScrollBar::handle:hover {{
    background: #48484a;
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    width: 0px;
    height: 0px;
}}
"""

LIGHT_QSS = f"""
QMainWindow, QDialog {{
    background-color: #f2f2f7;
    color: #1c1c1e;
    font-family: {FONT_STACK};
    font-size: 13px;
}}
QWidget {{
    color: #1c1c1e;
    font-family: {FONT_STACK};
    font-size: 13px;
}}
QScrollArea, QScrollArea > QWidget > QWidget {{
    background: transparent;
    border: none;
}}

/* iOS Sidebar Shell */
QFrame#sidebar {{
    background-color: #ffffff;
    border-right: 1px solid #e5e5ea;
}}
QLabel#brandTitle {{
    font-size: 17px;
    font-weight: 700;
    color: #1c1c1e;
}}
QLabel#brandSub {{
    font-size: 12px;
    color: #8e8e93;
}}

/* iOS Sidebar Navigation Pills */
QPushButton#navBtn {{
    background-color: transparent;
    color: #636366;
    border: none;
    border-radius: 11px;
    padding: 7px 12px;
    text-align: left;
    font-weight: 600;
    font-size: 13px;
}}
QPushButton#navBtn:hover {{
    background-color: #f2f2f7;
    color: #1c1c1e;
}}
QPushButton#navBtn[active="true"] {{
    background-color: rgba(0, 122, 255, 0.12);
    color: #007aff;
}}

/* iOS Squircle Cards & Surfaces */
QFrame#card, QFrame#statCard {{
    background-color: #ffffff;
    border: 1px solid #e5e5ea;
    border-radius: 16px;
}}
QFrame#fbPreviewCard {{
    background-color: #ffffff;
    border: 1px solid #e5e5ea;
    border-radius: 18px;
}}
QFrame#fbLinkCard {{
    background-color: #f2f2f7;
    border: 1px solid #e5e5ea;
    border-radius: 12px;
}}
QFrame#dropZone {{
    background-color: #f8f8fa;
    border: 1.5px dashed #c7c7cc;
    border-radius: 14px;
}}
QFrame#dropZone[dragActive="true"] {{
    background-color: rgba(0, 122, 255, 0.08);
    border: 1.5px dashed #007aff;
}}
QFrame#thumbCard {{
    background-color: #ffffff;
    border: 1px solid #e5e5ea;
    border-radius: 12px;
}}
QFrame#scheduleCard {{
    background-color: #f2f2f7;
    border: 1px solid #e5e5ea;
    border-radius: 14px;
}}
QFrame#hairline {{
    background-color: #e5e5ea;
    max-height: 1px;
    border: none;
}}

/* Typography */
QLabel#pageHeading {{
    font-size: 20px;
    font-weight: 700;
    color: #1c1c1e;
}}
QLabel#sectionTitle {{
    font-size: 12px;
    font-weight: 600;
    color: #636366;
}}
QLabel#mutedLabel, QLabel#statLabel {{
    color: #8e8e93;
    font-size: 12px;
}}
QLabel#statValue {{
    font-size: 21px;
    font-weight: 700;
    color: #1c1c1e;
}}
QLabel#dropMainLabel {{
    font-weight: 600;
    color: #3a3a3c;
}}
QLabel#fbPageName {{
    font-size: 14px;
    font-weight: 700;
    color: #1c1c1e;
}}
QLabel#fbBodyText {{
    font-size: 13px;
    color: #1c1c1e;
}}
QLabel#fbBodyText[placeholder="true"] {{
    color: #8e8e93;
    font-style: italic;
}}
QLabel#fbLinkDomain {{
    font-size: 11px;
    font-weight: 700;
    color: #007aff;
}}
QLabel#fbLinkUrl {{
    font-size: 12px;
    color: #48484a;
}}
QLabel#fbPhotoTile {{
    background-color: #f2f2f7;
    border-radius: 10px;
}}
QLabel#fbMoreBadge {{
    background-color: rgba(0, 122, 255, 0.12);
    color: #007aff;
    border-radius: 10px;
    padding: 3px 10px;
    font-size: 12px;
    font-weight: 600;
}}
QLabel#fbFooterAction {{
    color: #636366;
    font-size: 12px;
    font-weight: 600;
    padding: 4px;
}}
QLabel#statusBarLabel {{
    background-color: #ffffff;
    border: 1px solid #e5e5ea;
    border-radius: 12px;
    padding: 6px 14px;
    color: #636366;
    font-size: 12px;
}}

/* iOS Rounded Inputs */
QPlainTextEdit, QLineEdit, QDateTimeEdit, QComboBox {{
    background-color: #ffffff;
    color: #1c1c1e;
    border: 1px solid #d1d1d6;
    border-radius: 11px;
    padding: 6px 10px;
    selection-background-color: #007aff;
    selection-color: #ffffff;
}}
QSpinBox {{
    background-color: #ffffff;
    color: #1c1c1e;
    border: 1px solid #d1d1d6;
    border-radius: 11px;
    padding: 5px 8px;
    selection-background-color: #007aff;
    selection-color: #ffffff;
}}
QPlainTextEdit:focus, QLineEdit:focus, QDateTimeEdit:focus, QComboBox:focus, QSpinBox:focus {{
    border: 1px solid #007aff;
}}
QComboBox::drop-down, QDateTimeEdit::drop-down {{
    border: none;
    background: transparent;
    width: 20px;
}}
QComboBox QAbstractItemView {{
    background-color: #ffffff;
    color: #1c1c1e;
    border: 1px solid #d1d1d6;
    border-radius: 10px;
    padding: 4px;
    selection-background-color: #007aff;
    selection-color: #ffffff;
    outline: none;
}}
QComboBox QAbstractItemView::item {{
    padding: 6px 10px;
    border-radius: 6px;
    min-height: 22px;
    color: #1c1c1e;
    background-color: transparent;
}}
QComboBox QAbstractItemView::item:hover,
QComboBox QAbstractItemView::item:selected {{
    background-color: #007aff;
    color: #ffffff;
}}
QMenu {{
    background-color: #ffffff;
    color: #1c1c1e;
    border: 1px solid #d1d1d6;
    border-radius: 10px;
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 14px;
    border-radius: 6px;
}}
QMenu::item:selected {{
    background-color: #007aff;
    color: #ffffff;
}}

/* iOS Light Calendar Popup */
QCalendarWidget QWidget {{
    background-color: #ffffff;
    color: #1c1c1e;
    alternate-background-color: #f2f2f7;
}}
QCalendarWidget QWidget#qt_calendar_navigationbar {{
    background-color: #f2f2f7;
    border-bottom: 1px solid #e5e5ea;
    padding: 4px;
}}
QCalendarWidget QToolButton {{
    background-color: transparent;
    color: #1c1c1e;
    border: none;
    border-radius: 8px;
    padding: 4px 10px;
    font-weight: 700;
}}
QCalendarWidget QToolButton:hover {{
    background-color: rgba(0, 122, 255, 0.14);
    color: #007aff;
}}
QCalendarWidget QSpinBox {{
    background-color: #ffffff;
    color: #1c1c1e;
    border: 1px solid #d1d1d6;
    border-radius: 8px;
    padding: 2px 6px;
}}
QCalendarWidget QAbstractItemView:enabled {{
    background-color: #ffffff;
    color: #1c1c1e;
    selection-background-color: #007aff;
    selection-color: #ffffff;
    outline: none;
}}
QCalendarWidget QAbstractItemView:disabled {{
    color: #aeaeb2;
}}
QLineEdit#searchInput {{
    background-color: #f2f2f7;
    border: none;
    padding: 7px 12px;
    border-radius: 12px;
    font-size: 12px;
}}
QLineEdit#searchInput:focus {{
    border: 1.5px solid #007aff;
    background-color: #ffffff;
}}

/* Lists & Tables */
QListWidget {{
    background-color: transparent;
    border: none;
    outline: none;
}}
QListWidget::item {{
    padding: 7px 10px;
    margin: 2px 0px;
    border-radius: 11px;
    color: #1c1c1e;
}}
QListWidget::item:hover {{
    background-color: #f2f2f7;
}}
QListWidget::item:selected {{
    background-color: rgba(0, 122, 255, 0.14);
    color: #007aff;
}}

QTableWidget {{
    background-color: #ffffff;
    alternate-background-color: #f8f8fa;
    color: #1c1c1e;
    border: 1px solid #e5e5ea;
    border-radius: 14px;
    gridline-color: #f2f2f7;
    selection-background-color: rgba(0, 122, 255, 0.16);
    selection-color: #007aff;
}}
QTableWidget::item {{
    padding: 6px 10px;
}}
QHeaderView::section {{
    background-color: #f8f8fa;
    color: #636366;
    border: none;
    border-bottom: 1px solid #e5e5ea;
    padding: 8px 10px;
    font-weight: 700;
    font-size: 12px;
}}

/* iOS Capsule / Pill Buttons */
QPushButton {{
    background-color: #e5e5ea;
    color: #1c1c1e;
    border: none;
    border-radius: 13px;
    padding: 6px 13px;
    font-weight: 600;
}}
QPushButton:hover {{
    background-color: #d1d1d6;
}}
QPushButton:pressed {{
    background-color: #c7c7cc;
}}
QPushButton:disabled {{
    background-color: #f2f2f7;
    color: #aeaeb2;
}}

/* iOS Filled Primary Pill Button */
QPushButton#primaryBtn {{
    background-color: #007aff;
    border: none;
    border-radius: 13px;
    color: #ffffff;
    padding: 8px 16px;
    font-size: 13px;
    font-weight: 700;
}}
QPushButton#primaryBtn:hover {{
    background-color: #006ae0;
}}
QPushButton#primaryBtn:pressed {{
    background-color: #005ec7;
}}
QPushButton#primaryBtn:disabled {{
    background-color: rgba(0, 122, 255, 0.35);
    color: #ffffff;
}}

/* iOS Tinted Secondary Pill Button */
QPushButton#ghostBtn {{
    background-color: rgba(0, 122, 255, 0.11);
    color: #007aff;
    border: none;
    border-radius: 13px;
    padding: 6px 12px;
    font-size: 12px;
    font-weight: 600;
}}
QPushButton#ghostBtn:hover {{
    background-color: rgba(0, 122, 255, 0.19);
}}
QPushButton#ghostBtn:pressed {{
    background-color: rgba(0, 122, 255, 0.26);
}}
QPushButton#ghostBtn:disabled {{
    background-color: #f2f2f7;
    color: #aeaeb2;
}}

/* iOS Segmented Filter Pill */
QPushButton#pillBtn {{
    background-color: #e5e5ea;
    color: #636366;
    border: none;
    border-radius: 13px;
    padding: 5px 12px;
    font-size: 12px;
    font-weight: 600;
}}
QPushButton#pillBtn:hover {{
    background-color: #d1d1d6;
    color: #1c1c1e;
}}
QPushButton#pillBtn[active="true"] {{
    background-color: #007aff;
    color: #ffffff;
}}

/* iOS Destructive Tinted Pill Button */
QPushButton#dangerBtn {{
    background-color: rgba(255, 59, 48, 0.11);
    border: none;
    border-radius: 13px;
    color: #ff3b30;
    padding: 6px 12px;
    font-size: 12px;
    font-weight: 600;
}}
QPushButton#dangerBtn:hover {{
    background-color: rgba(255, 59, 48, 0.19);
}}
QPushButton#dangerBtn:pressed {{
    background-color: rgba(255, 59, 48, 0.28);
}}
QPushButton#dangerBtn:disabled {{
    background-color: #f2f2f7;
    color: #aeaeb2;
}}

QPushButton#thumbRemoveBtn {{
    background-color: #ff3b30;
    color: #ffffff;
    border: none;
    border-radius: 10px;
    padding: 0px;
    font-weight: 700;
    font-size: 13px;
}}
QPushButton#thumbRemoveBtn:hover {{
    background-color: #d70015;
}}

/* iOS Segmented Tabs (Settings Wizard) */
QTabWidget::pane {{
    border: 1px solid #e5e5ea;
    border-radius: 16px;
    background-color: #ffffff;
    padding: 8px;
}}
QTabBar::tab {{
    background-color: #e5e5ea;
    color: #636366;
    border: none;
    border-radius: 12px;
    padding: 8px 16px;
    margin-right: 6px;
    margin-bottom: 6px;
    font-weight: 600;
}}
QTabBar::tab:selected {{
    background-color: #007aff;
    color: #ffffff;
}}

/* Checkboxes & Slim Scrollbars */
QCheckBox {{
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    border-radius: 9px;
    border: 1.5px solid #c7c7cc;
    background-color: #ffffff;
}}
QCheckBox::indicator:hover {{
    border-color: #007aff;
}}
QCheckBox::indicator:checked {{
    background-color: #007aff;
    border-color: #007aff;
}}
QScrollBar:vertical, QScrollBar:horizontal {{
    background: transparent;
    width: 7px;
    height: 7px;
    margin: 0px;
}}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background: #c7c7cc;
    border-radius: 3.5px;
    min-height: 24px;
    min-width: 24px;
}}
QScrollBar::handle:hover {{
    background: #aeaeb2;
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    width: 0px;
    height: 0px;
}}
"""


def get_stylesheet(theme: str) -> str:
    return LIGHT_QSS if theme == "light" else DARK_QSS
