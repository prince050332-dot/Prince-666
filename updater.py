"""Patch check, SHA-256 verification, and atomic file-swap engine for Social Manager Launcher."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Callable
from urllib.parse import urlparse
import uuid

import keyring
import keyring.errors
import requests

import config
from version import APP_NAME, APP_VERSION, DEFAULT_MANIFEST_URL

LAUNCHER_CONFIG_NAME = "launcher_config.json"
INSTALLED_VERSION_NAME = "installed_version.json"
KEYRING_GITHUB_TOKEN_KEY = "launcher:github_token"
DEFAULT_TARGET_EXE = "SocialManager.exe"

_SEMVER_RE = re.compile(r"^[vV]?([0-9]+)\.([0-9]+)\.([0-9]+)(?:[-+][0-9A-Za-z.-]+)?$", flags=re.ASCII)
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$", flags=re.ASCII)
_GH_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", flags=re.ASCII)
_ALLOWED_PATCH_EXTS = {".exe", ".txt", ".json", ".dll", ".pyd", ".md"}


@dataclass
class PatchFileEntry:
    name: str
    url: str
    sha256: str = ""
    size: int = 0


@dataclass
class PatchManifest:
    version: str
    released_at: str = ""
    notes: str = ""
    force_update: bool = False
    files: list[PatchFileEntry] = field(default_factory=list)
    source_url: str = ""


def parse_version(raw: str) -> tuple[int, int, int]:
    """Parse strict ASCII SemVer (`1.5.0` or `v1.5.0`) into `(major, minor, patch)`."""
    clean = (raw or "").strip()
    m = _SEMVER_RE.fullmatch(clean)
    if not m:
        raise ValueError(f"รูปแบบเลขเวอร์ชันไม่ถูกต้อง: '{raw}' (ต้องเป็นเช่น 1.5.0)")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def is_newer_version(remote_version: str, local_version: str) -> bool:
    """Return True strictly when `remote_version > local_version`."""
    return parse_version(remote_version) > parse_version(local_version)


def normalize_manifest_url(raw_input: str, *, allow_local_http: bool = False) -> str:
    """Normalize either a GitHub `owner/repo` shorthand or an HTTPS manifest/release URL."""
    raw = (raw_input or "").strip()
    if not raw:
        raise ValueError("กรุณาระบุ GitHub Repository (เช่น username/repo) หรือลิงก์ version.json")
    if any(ch in raw for ch in (" ", "\t", "\r", "\n")):
        raise ValueError("URL หรือชื่อ Repository ต้องไม่มีช่องว่าง")

    # Shorthand: owner/repo -> GitHub Releases latest version.json
    if _GH_REPO_RE.fullmatch(raw) and not raw.lower().startswith(("http://", "https://")):
        return f"https://github.com/{raw}/releases/latest/download/version.json"

    # Full GitHub repo page URL -> latest version.json
    parsed = urlparse(raw)
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"URL พอร์ตไม่ถูกต้อง: {raw}") from exc

    if parsed.username or parsed.password:
        raise ValueError("ไม่อนุญาตให้ฝัง username/password ใน URL อัปเดต")

    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http":
        if not (allow_local_http and host in ("127.0.0.1", "localhost")):
            raise ValueError("ลิงก์อัปเดตแพตช์ต้องขึ้นต้นด้วย https:// เท่านั้น")
    elif parsed.scheme != "https":
        raise ValueError("ลิงก์อัปเดตแพตช์ต้องขึ้นต้นด้วย https:// เท่านั้น")

    if not host:
        raise ValueError("URL อัปเดตไม่ถูกต้อง (ไม่พบชื่อโฮสต์)")

    # If user pasted https://github.com/owner/repo or .../releases, normalize to latest version.json
    if host in ("github.com", "www.github.com"):
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) == 2 or (len(parts) == 3 and parts[2].lower() == "releases"):
            return f"https://github.com/{parts[0]}/{parts[1]}/releases/latest/download/version.json"
    return raw


def validate_patch_url(url: str, *, allow_local_http: bool = False) -> str:
    """Validate a file download URL inside a patch manifest."""
    raw = (url or "").strip()
    if not raw or any(ch in raw for ch in (" ", "\t", "\r", "\n")):
        raise ValueError(f"ลิงก์ดาวน์โหลดไฟล์แพตช์ไม่ถูกต้อง: {url}")
    parsed = urlparse(raw)
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"ลิงก์ดาวน์โหลดพอร์ตไม่ถูกต้อง: {url}") from exc
    if parsed.username or parsed.password:
        raise ValueError("ไม่อนุญาตให้ฝัง credentials ในลิงก์ดาวน์โหลดแพตช์")
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http":
        if not (allow_local_http and host in ("127.0.0.1", "localhost")):
            raise ValueError("ลิงก์ดาวน์โหลดแพตช์ต้องใช้ https:// เท่านั้น")
    elif parsed.scheme != "https":
        raise ValueError("ลิงก์ดาวน์โหลดแพตช์ต้องใช้ https:// เท่านั้น")
    if not host:
        raise ValueError("ลิงก์ดาวน์โหลดแพตช์ไม่มีชื่อโฮสต์")
    return raw


def validate_target_filename(name: str, install_dir: Path) -> Path:
    """Reject any path traversal or unsafe filename; return resolved path strictly under `install_dir`."""
    clean = (name or "").strip()
    if not clean:
        raise ValueError("ชื่อไฟล์ในแพตช์ต้องไม่ว่าง")
    if (
        "/" in clean
        or "\\" in clean
        or "\x00" in clean
        or ":" in clean
        or clean in (".", "..")
        or ".." in clean
    ):
        raise ValueError(f"ปฏิเสธชื่อไฟล์ที่ไม่ปลอดภัยในแพตช์: {name}")
    ext = Path(clean).suffix.lower()
    if ext not in _ALLOWED_PATCH_EXTS:
        raise ValueError(f"ไม่อนุญาตให้แพตช์เขียนไฟล์นามสกุล '{ext}' ({clean})")
    base_dir = install_dir.resolve()
    target = (base_dir / clean).resolve()
    if target.parent != base_dir:
        raise ValueError(f"ปฏิเสธไฟล์ที่อยู่นอกโฟลเดอร์ติดตั้ง: {name}")
    return target


def validate_sha256_hex(sha256_str: str) -> str:
    clean = (sha256_str or "").strip().lower()
    if not _SHA256_RE.fullmatch(clean):
        raise ValueError("ค่า SHA-256 ในแพตช์ไม่ถูกต้อง (ต้องเป็นเลขฐานสิบหก 64 ตัวอักษร)")
    return clean


def compute_sha256(file_path: str | Path) -> str:
    """Compute lowercase SHA-256 hex digest using 64KB streaming chunks."""
    h = hashlib.sha256()
    with Path(file_path).open("rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest().lower()


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp_{os.getpid()}_{uuid.uuid4().hex}"
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def get_launcher_config_path(install_dir: Path) -> Path:
    local_cfg = install_dir / LAUNCHER_CONFIG_NAME
    if local_cfg.is_file():
        return local_cfg
    try:
        install_dir.mkdir(parents=True, exist_ok=True)
        probe = install_dir / f".write_probe_{os.getpid()}_{uuid.uuid4().hex[:6]}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return local_cfg
    except OSError:
        return config.APP_DIR / LAUNCHER_CONFIG_NAME


def load_launcher_config(install_dir: Path) -> dict:
    """Load launcher config from install_dir (or APP_DIR fallback)."""
    for cand in (install_dir / LAUNCHER_CONFIG_NAME, config.APP_DIR / LAUNCHER_CONFIG_NAME):
        if cand.is_file():
            try:
                raw = json.loads(cand.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return {
                        "manifest_url": str(raw.get("manifest_url") or "").strip(),
                        "auto_check": bool(raw.get("auto_check", True)),
                        "target_exe": str(raw.get("target_exe") or DEFAULT_TARGET_EXE).strip()
                        or DEFAULT_TARGET_EXE,
                    }
            except (OSError, ValueError):
                continue
    return {
        "manifest_url": (DEFAULT_MANIFEST_URL or "").strip(),
        "auto_check": True,
        "target_exe": DEFAULT_TARGET_EXE,
    }


def save_launcher_config(
    install_dir: Path,
    *,
    manifest_url: str,
    auto_check: bool = True,
    target_exe: str = DEFAULT_TARGET_EXE,
    allow_local_http: bool = False,
) -> Path:
    clean_url = (
        normalize_manifest_url(manifest_url, allow_local_http=allow_local_http)
        if (manifest_url or "").strip()
        else ""
    )
    clean_exe = (target_exe or DEFAULT_TARGET_EXE).strip() or DEFAULT_TARGET_EXE
    validate_target_filename(clean_exe, install_dir)
    cfg_path = get_launcher_config_path(install_dir)
    _atomic_write_json(
        cfg_path,
        {
            "manifest_url": clean_url,
            "auto_check": bool(auto_check),
            "target_exe": clean_exe,
        },
    )
    return cfg_path


def get_github_token() -> str:
    try:
        tok = keyring.get_password(config.KEYRING_SERVICE, KEYRING_GITHUB_TOKEN_KEY)
        return (tok or "").strip()
    except keyring.errors.KeyringError:
        return ""


def save_github_token(token: str) -> bool:
    clean = (token or "").strip()
    try:
        if clean:
            keyring.set_password(config.KEYRING_SERVICE, KEYRING_GITHUB_TOKEN_KEY, clean)
        else:
            try:
                keyring.delete_password(config.KEYRING_SERVICE, KEYRING_GITHUB_TOKEN_KEY)
            except keyring.errors.PasswordDeleteError:
                pass
        return True
    except keyring.errors.KeyringError:
        return False


def get_installed_version(install_dir: Path, target_exe: str = DEFAULT_TARGET_EXE) -> str:
    """Read installed version from `installed_version.json`, or fallback to `0.0.0` if exe missing."""
    exe_path = install_dir / target_exe
    ver_file = install_dir / INSTALLED_VERSION_NAME
    if ver_file.is_file():
        try:
            data = json.loads(ver_file.read_text(encoding="utf-8"))
            v = str(data.get("version") or "").strip()
            if v:
                parse_version(v)
                if exe_path.is_file():
                    return v
        except (OSError, ValueError):
            pass
    if exe_path.is_file():
        return APP_VERSION
    return "0.0.0"


def save_installed_version(install_dir: Path, version_str: str, files: list[str]) -> None:
    parse_version(version_str)
    _atomic_write_json(
        install_dir / INSTALLED_VERSION_NAME,
        {
            "app_name": APP_NAME,
            "version": version_str.lstrip("vV"),
            "updated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "files": list(files),
        },
    )


def parse_manifest_payload(
    data: dict,
    *,
    source_url: str = "",
    install_dir: Path | None = None,
    allow_local_http: bool = False,
) -> PatchManifest:
    """Parse either our `version.json` schema OR a GitHub Releases API JSON response."""
    if not isinstance(data, dict):
        raise ValueError("รูปแบบไฟล์ข้อมูลแพตช์ (JSON) ไม่ถูกต้อง")

    # Support direct GitHub Releases API payload (`tag_name` + `assets`)
    if "tag_name" in data and "assets" in data and "version" not in data:
        raw_ver = str(data.get("tag_name") or "").strip()
        major, minor, patch = parse_version(raw_ver)
        clean_ver = f"{major}.{minor}.{patch}"
        notes = str(data.get("body") or "").strip()
        released_at = str(data.get("published_at") or "").strip()
        entries: list[PatchFileEntry] = []
        for asset in data.get("assets") or []:
            if not isinstance(asset, dict):
                continue
            fname = str(asset.get("name") or "").strip()
            dl_url = str(asset.get("browser_download_url") or "").strip()
            if not fname or not dl_url:
                continue
            if Path(fname).suffix.lower() not in _ALLOWED_PATCH_EXTS:
                continue
            if fname.lower() == "version.json":
                continue
            if install_dir is not None:
                validate_target_filename(fname, install_dir)
            validate_patch_url(dl_url, allow_local_http=allow_local_http)
            digest = str(asset.get("digest") or "").strip()
            sha = ""
            if digest.lower().startswith("sha256:"):
                sha = validate_sha256_hex(digest.split(":", 1)[1])
            entries.append(
                PatchFileEntry(
                    name=fname,
                    url=dl_url,
                    sha256=sha,
                    size=max(0, int(asset.get("size") or 0)),
                )
            )
        if not entries:
            raise ValueError("ไม่พบไฟล์แพตช์ (.exe / คู่มือ) ใน GitHub Release นี้")
        return PatchManifest(
            version=clean_ver,
            released_at=released_at,
            notes=notes,
            force_update=False,
            files=entries,
            source_url=source_url,
        )

    raw_ver = str(data.get("version") or "").strip()
    major, minor, patch = parse_version(raw_ver)
    clean_ver = f"{major}.{minor}.{patch}"
    released_at = str(data.get("released_at") or "").strip()
    notes = str(data.get("notes") or "").strip()
    force_update = bool(data.get("force_update", False))

    raw_files = data.get("files")
    entries = []
    if isinstance(raw_files, list) and raw_files:
        for item in raw_files:
            if not isinstance(item, dict):
                continue
            fname = str(item.get("name") or "").strip()
            dl_url = validate_patch_url(str(item.get("url") or ""), allow_local_http=allow_local_http)
            if install_dir is not None:
                validate_target_filename(fname, install_dir)
            sha_raw = str(item.get("sha256") or "").strip()
            sha = validate_sha256_hex(sha_raw) if sha_raw else ""
            size_val = max(0, int(item.get("size") or 0))
            entries.append(PatchFileEntry(name=fname, url=dl_url, sha256=sha, size=size_val))
    elif data.get("download_url"):
        dl_url = validate_patch_url(str(data["download_url"]), allow_local_http=allow_local_http)
        fname = str(data.get("filename") or DEFAULT_TARGET_EXE).strip() or DEFAULT_TARGET_EXE
        if install_dir is not None:
            validate_target_filename(fname, install_dir)
        sha_raw = str(data.get("sha256") or "").strip()
        sha = validate_sha256_hex(sha_raw) if sha_raw else ""
        size_val = max(0, int(data.get("size") or 0))
        entries.append(PatchFileEntry(name=fname, url=dl_url, sha256=sha, size=size_val))

    if not entries:
        raise ValueError("ไฟล์ version.json ไม่มีรายการไฟล์สำหรับดาวน์โหลด (files หรือ download_url)")

    return PatchManifest(
        version=clean_ver,
        released_at=released_at,
        notes=notes,
        force_update=force_update,
        files=entries,
        source_url=source_url,
    )


def _request_headers(github_token: str = "") -> dict[str, str]:
    headers = {
        "User-Agent": f"SocialManagerLauncher/{APP_VERSION}",
        "Accept": "application/json, application/vnd.github+json",
    }
    tok = (github_token or "").strip()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    return headers


def fetch_patch_manifest(
    manifest_url: str,
    *,
    install_dir: Path | None = None,
    github_token: str = "",
    timeout: int = 20,
    allow_local_http: bool = False,
) -> PatchManifest:
    """Download and validate the remote patch manifest (`version.json` or GitHub Releases API)."""
    clean_url = normalize_manifest_url(manifest_url, allow_local_http=allow_local_http)
    try:
        r = requests.get(
            clean_url,
            headers=_request_headers(github_token),
            timeout=timeout,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"เชื่อมต่อเซิร์ฟเวอร์อัปเดตไม่สำเร็จ: {exc}") from exc

    if r.status_code == 404:
        raise RuntimeError(
            "ไม่พบไฟล์ version.json บนเซิร์ฟเวอร์อัปเดต (HTTP 404) — กรุณาตรวจสอบว่าอัปโหลด version.json ใน GitHub Release แล้ว"
        )
    if r.status_code in (401, 403):
        raise RuntimeError(
            f"ไม่มีสิทธิ์เข้าถึงเซิร์ฟเวอร์อัปเดต (HTTP {r.status_code}) — หากเป็น Private Repo กรุณาใส่ GitHub Token ในหน้าตั้งค่า"
        )
    if r.status_code != 200:
        raise RuntimeError(f"เซิร์ฟเวอร์อัปเดตตอบกลับผิดพลาด (HTTP {r.status_code})")

    try:
        payload = r.json()
    except ValueError as exc:
        raise RuntimeError("ข้อมูลที่ได้รับจากเซิร์ฟเวอร์อัปเดตไม่ใช่ JSON ที่ถูกต้อง") from exc

    return parse_manifest_payload(
        payload,
        source_url=clean_url,
        install_dir=install_dir,
        allow_local_http=allow_local_http,
    )


class PatchCancelled(RuntimeError):
    """Raised when the user cancels a patch download."""


def download_and_apply_patch(
    manifest: PatchManifest,
    install_dir: Path,
    *,
    github_token: str = "",
    progress_cb: Callable[[int, int, str, int, int, float], None] | None = None,
    cancel_event: threading.Event | None = None,
    timeout: int = 60,
    allow_local_http: bool = False,
) -> list[Path]:
    """Download all files in `manifest`, verify every SHA-256, and atomically swap into `install_dir`.

    If ANY download, hash check, or file replacement fails, all staged temp files are deleted
    and any already-replaced targets are rolled back from `.bak` copies cleanly.
    """
    if not manifest.files:
        raise ValueError("แพตช์นี้ไม่มีไฟล์ให้ติดตั้ง")

    base_dir = install_dir.resolve()
    base_dir.mkdir(parents=True, exist_ok=True)

    # Pre-validate every entry before starting any network traffic
    plan: list[tuple[PatchFileEntry, Path]] = []
    for entry in manifest.files:
        target_path = validate_target_filename(entry.name, base_dir)
        validate_patch_url(entry.url, allow_local_http=allow_local_http)
        if entry.sha256:
            validate_sha256_hex(entry.sha256)
        plan.append((entry, target_path))

    staged: list[tuple[Path, Path]] = []  # (tmp_path, target_path)
    total_files = len(plan)
    dl_headers = _request_headers(github_token)
    dl_headers["Accept"] = "application/octet-stream"

    try:
        # Phase 1: Stream-download & verify SHA-256 for every file before touching existing installs
        for idx, (entry, target_path) in enumerate(plan, start=1):
            if cancel_event and cancel_event.is_set():
                raise PatchCancelled("ยกเลิกการดาวน์โหลดแพตช์แล้ว")

            tmp_path = base_dir / f".patch_{idx}_{os.getpid()}_{uuid.uuid4().hex}.tmp"
            staged.append((tmp_path, target_path))
            hasher = hashlib.sha256()
            bytes_done = 0
            started = time.monotonic()

            try:
                with requests.get(
                    entry.url,
                    headers=dl_headers,
                    stream=True,
                    timeout=timeout,
                    allow_redirects=True,
                ) as resp:
                    if resp.status_code != 200:
                        raise RuntimeError(
                            f"ดาวน์โหลด {entry.name} ไม่สำเร็จ (HTTP {resp.status_code})"
                        )
                    cl_header = resp.headers.get("Content-Length", "").strip()
                    header_total = (
                        int(cl_header)
                        if cl_header.isascii() and cl_header.isdigit()
                        else 0
                    )
                    bytes_total = header_total or entry.size
                    with tmp_path.open("wb") as out_fh:
                        for chunk in resp.iter_content(chunk_size=65536):
                            if cancel_event and cancel_event.is_set():
                                raise PatchCancelled("ยกเลิกการดาวน์โหลดแพตช์แล้ว")
                            if not chunk:
                                continue
                            out_fh.write(chunk)
                            hasher.update(chunk)
                            bytes_done += len(chunk)
                            if progress_cb is not None:
                                elapsed = max(0.05, time.monotonic() - started)
                                progress_cb(
                                    idx,
                                    total_files,
                                    entry.name,
                                    bytes_done,
                                    bytes_total,
                                    bytes_done / elapsed,
                                )
            except PatchCancelled:
                raise
            except requests.RequestException as exc:
                raise RuntimeError(f"ดาวน์โหลด {entry.name} ขัดข้อง: {exc}") from exc

            if bytes_done <= 0:
                raise RuntimeError(f"ไฟล์ {entry.name} ที่ดาวน์โหลดมีขนาด 0 bytes")

            actual_sha = hasher.hexdigest().lower()
            if entry.sha256 and actual_sha != entry.sha256.lower():
                raise RuntimeError(
                    f"ตรวจความถูกต้องไฟล์ {entry.name} ไม่ผ่าน (SHA-256 ไม่ตรงกัน — ไฟล์อาจโหลดไม่ครบหรือถูกแก้ไข)"
                )

            if progress_cb is not None:
                progress_cb(idx, total_files, entry.name, bytes_done, max(bytes_done, entry.size), 0.0)

        # Phase 2: Atomic swap with rollback protection
        swapped_backups: list[tuple[Path, Path | None]] = []  # (target_path, backup_path_or_None)
        installed_paths: list[Path] = []
        try:
            for tmp_path, target_path in staged:
                bak_path: Path | None = None
                if target_path.is_file():
                    bak_path = base_dir / f".bak_{target_path.name}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
                    try:
                        os.replace(target_path, bak_path)
                    except PermissionError as exc:
                        raise RuntimeError(
                            f"ไม่สามารถอัปเดตไฟล์ {target_path.name} ได้เนื่องจากโปรแกรมกำลังเปิดใช้งานอยู่ — กรุณาปิดโปรแกรมหลักก่อนแล้วกดอัปเดตอีกครั้ง"
                        ) from exc
                swapped_backups.append((target_path, bak_path))
                os.replace(tmp_path, target_path)
                installed_paths.append(target_path)
        except Exception:
            # Roll back any already-swapped files
            for target_path, bak_path in reversed(swapped_backups):
                if bak_path is not None and bak_path.is_file():
                    try:
                        os.replace(bak_path, target_path)
                    except OSError:
                        pass
                elif target_path.is_file():
                    try:
                        target_path.unlink(missing_ok=True)
                    except OSError:
                        pass
            raise
        else:
            # Cleanup old backups after all files swapped cleanly
            for _target_path, bak_path in swapped_backups:
                if bak_path is not None:
                    try:
                        bak_path.unlink(missing_ok=True)
                    except OSError:
                        pass

        save_installed_version(
            base_dir,
            manifest.version,
            [p.name for p in installed_paths],
        )
        return installed_paths
    finally:
        for tmp_path, _target_path in staged:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
