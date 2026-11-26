"""Configuration and settings management for Social Manager."""
import json
import os
from pathlib import Path
import re
import sys
import uuid

import keyring
import keyring.errors

APP_DIR = Path.home() / ".social_manager"
APP_DIR.mkdir(exist_ok=True)

SETTINGS_PATH = APP_DIR / "settings.json"
KEYRING_SERVICE = "social_manager"
KEYRING_SECRET_KEY = "config:fb_app_secret"
DEFAULT_GRAPH_VERSION = "v22.0"


def _parse_env_value(raw: str) -> str:
    """Strip quotes and inline comments (outside quotes) from a .env value."""
    val = raw.strip()
    if not val:
        return ""
    if val[0] in ('"', "'"):
        quote = val[0]
        end = val.find(quote, 1)
        if end != -1:
            return val[1:end]
        return val[1:]
    # Strip unquoted inline comment (preceded by whitespace or start)
    if " #" in val:
        val = val.split(" #", 1)[0]
    elif "\t#" in val:
        val = val.split("\t#", 1)[0]
    elif val.startswith("#"):
        val = ""
    return val.strip()


def _load_env_file(path: Path) -> dict[str, str]:
    parsed: dict[str, str] = {}
    if not path.is_file():
        return parsed
    try:
        try:
            raw_text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            try:
                raw_text = path.read_text(encoding="cp874")
            except UnicodeDecodeError:
                raw_text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return parsed
    for line in raw_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        key = k.strip()
        val = _parse_env_value(v)
        if key and val:
            parsed[key] = val
    return parsed


def _load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_settings(data: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = SETTINGS_PATH.parent / f".{SETTINGS_PATH.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, SETTINGS_PATH)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _load_env() -> None:
    """Load non-empty keys from local .env then ~/.social_manager/.env without shadowing."""
    for p in (_base_dir() / ".env", APP_DIR / ".env"):
        for k, v in _load_env_file(p).items():
            if not os.environ.get(k):
                os.environ[k] = v


def get_fb_app_id() -> str:
    settings = _load_settings()
    if settings.get("fb_app_id"):
        return str(settings["fb_app_id"]).strip()
    return os.getenv("FB_APP_ID", "").strip()


def get_fb_app_secret() -> str:
    try:
        secret = keyring.get_password(KEYRING_SERVICE, KEYRING_SECRET_KEY)
        if secret:
            return secret.strip()
    except keyring.errors.KeyringError:
        pass
    settings = _load_settings()
    if settings.get("fb_app_secret"):
        return str(settings["fb_app_secret"]).strip()
    return os.getenv("FB_APP_SECRET", "").strip()


def _normalize_graph_version(raw: str) -> str:
    ver = (raw or "").strip()
    if ver and not ver.startswith("v"):
        ver = f"v{ver}"
    if re.fullmatch(r"v[0-9]{1,2}\.[0-9]{1,2}", ver):
        return ver
    return DEFAULT_GRAPH_VERSION


def get_graph_version() -> str:
    settings = _load_settings()
    raw = str(settings.get("fb_graph_version") or os.getenv("FB_GRAPH_VERSION") or DEFAULT_GRAPH_VERSION)
    return _normalize_graph_version(raw)


def get_auth_mode() -> str:
    settings = _load_settings()
    mode = str(settings.get("auth_mode") or os.getenv("SM_AUTH_MODE") or "self_app").strip().lower()
    return mode if mode in ("self_app", "backend_oauth") else "self_app"


def get_backend_url() -> str:
    settings = _load_settings()
    url = str(settings.get("backend_url") or os.getenv("SM_BACKEND_URL") or "").strip()
    return url.rstrip("/")


def save_backend_settings(backend_url: str, use_backend: bool = True) -> None:
    settings = _load_settings()
    settings["backend_url"] = backend_url.strip().rstrip("/")
    settings["auth_mode"] = "backend_oauth" if use_backend else "self_app"
    _save_settings(settings)
    reload_config()


def save_fb_settings(
    app_id: str,
    app_secret: str,
    graph_version: str = DEFAULT_GRAPH_VERSION,
    *,
    set_self_mode: bool = True,
) -> bool:
    """Save FB App settings. Returns True if secret was stored in OS Keychain (or empty)."""
    app_id = app_id.strip()
    app_secret = app_secret.strip()
    graph_version = _normalize_graph_version(graph_version)

    settings = _load_settings()
    settings["fb_app_id"] = app_id
    settings["fb_graph_version"] = graph_version
    if set_self_mode:
        settings["auth_mode"] = "self_app"

    # Store secret in OS keychain when possible; remove plaintext fallback if keychain succeeds
    stored_in_keyring = False
    try:
        if app_secret:
            keyring.set_password(KEYRING_SERVICE, KEYRING_SECRET_KEY, app_secret)
        else:
            try:
                keyring.delete_password(KEYRING_SERVICE, KEYRING_SECRET_KEY)
            except keyring.errors.PasswordDeleteError:
                pass
        stored_in_keyring = True
    except keyring.errors.KeyringError:
        stored_in_keyring = False

    if stored_in_keyring:
        settings.pop("fb_app_secret", None)
    else:
        settings["fb_app_secret"] = app_secret

    _save_settings(settings)
    reload_config()
    return stored_in_keyring


def get_theme() -> str:
    theme = str(_load_settings().get("theme", "dark")).lower()
    return theme if theme in ("dark", "light") else "dark"


def save_theme(theme: str) -> None:
    if theme not in ("dark", "light"):
        return
    settings = _load_settings()
    settings["theme"] = theme
    _save_settings(settings)


def get_minimize_to_tray() -> bool:
    return bool(_load_settings().get("minimize_to_tray", False))


def save_minimize_to_tray(enabled: bool) -> None:
    settings = _load_settings()
    settings["minimize_to_tray"] = bool(enabled)
    _save_settings(settings)


def get_auto_reply_enabled() -> bool:
    return bool(_load_settings().get("auto_reply_enabled", False))


def save_auto_reply_enabled(enabled: bool) -> None:
    settings = _load_settings()
    settings["auto_reply_enabled"] = bool(enabled)
    _save_settings(settings)


def get_auto_reply_interval_sec() -> int:
    raw = _load_settings().get("auto_reply_interval_sec", 300)
    try:
        val = int(raw)
    except (TypeError, ValueError):
        val = 300
    return max(60, min(val, 3600))


def save_auto_reply_interval_sec(seconds: int) -> None:
    try:
        val = int(seconds)
    except (TypeError, ValueError):
        val = 300
    settings = _load_settings()
    settings["auto_reply_interval_sec"] = max(60, min(val, 3600))
    _save_settings(settings)


def load_composer_draft() -> dict:
    raw = _load_settings().get("composer_draft")
    return raw if isinstance(raw, dict) else {}


def save_composer_draft(draft: dict) -> None:
    settings = _load_settings()
    if draft:
        settings["composer_draft"] = draft
    else:
        settings.pop("composer_draft", None)
    _save_settings(settings)


def reload_config() -> None:
    global FB_APP_ID, FB_APP_SECRET, FB_GRAPH_VERSION
    _load_env()
    FB_APP_ID = get_fb_app_id()
    FB_APP_SECRET = get_fb_app_secret()
    FB_GRAPH_VERSION = get_graph_version()


FB_APP_ID = ""
FB_APP_SECRET = ""
FB_GRAPH_VERSION = DEFAULT_GRAPH_VERSION
reload_config()
