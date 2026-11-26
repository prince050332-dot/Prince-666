"""One-click GitHub Releases patch packager for โพสต์ดิ!! รอไร (Social Manager).

Usage examples:
    python build_release.py --repo myname/social-manager --notes "- เพิ่มโหมด Reels\n- แก้บั๊ก Reach/Impressions"
    python build_release.py --repo myname/social-manager --skip-build --force-update
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
from urllib.parse import quote

import updater
from version import APP_NAME, APP_VERSION

ROOT_DIR = Path(__file__).resolve().parent
DIST_DIR = ROOT_DIR / "dist"
DEFAULT_RELEASE_DIR = ROOT_DIR / "release_out"
WEB_DIR = ROOT_DIR / "release_web"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp_{os.getpid()}_{uuid.uuid4().hex}"
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def build_release_bundle(
    *,
    repo: str,
    version_str: str = APP_VERSION,
    notes: str = "",
    force_update: bool = False,
    dist_dir: Path = DIST_DIR,
    output_dir: Path = DEFAULT_RELEASE_DIR,
    web_dir: Path | None = WEB_DIR,
    base_download_url: str = "",
) -> dict:
    """Package `SocialManager.exe` + `คู่มือการใช้งาน.txt` + `SocialManagerLauncher.exe` and generate `version.json`."""
    major, minor, patch = updater.parse_version(version_str)
    clean_ver = f"{major}.{minor}.{patch}"
    tag = f"v{clean_ver}"

    clean_repo = (repo or "").strip()
    if base_download_url.strip():
        dl_base = updater.validate_patch_url(base_download_url.strip()).rstrip("/")
    else:
        if not clean_repo or "/" not in clean_repo:
            raise ValueError(
                "กรุณาระบุ --repo ในรูปแบบ owner/repo (เช่น myname/social-manager) หรือ --base-url"
            )
        # Validate via updater.normalize_manifest_url
        updater.normalize_manifest_url(clean_repo)
        dl_base = f"https://github.com/{clean_repo}/releases/download/{tag}"

    exe_src = dist_dir / "SocialManager.exe"
    manual_src = dist_dir / "คู่มือการใช้งาน.txt"
    launcher_src = dist_dir / "SocialManagerLauncher.exe"

    if not exe_src.is_file():
        raise FileNotFoundError(f"ไม่พบไฟล์ {exe_src} (กรุณารัน build ก่อน)")

    output_dir.mkdir(parents=True, exist_ok=True)
    patch_sources: list[tuple[Path, str]] = [(exe_src, exe_src.name)]
    if manual_src.is_file():
        # GitHub Releases strips non-ASCII characters from uploaded asset names,
        # so upload as pure ASCII `USER_MANUAL_TH.txt` while installing locally as `คู่มือการใช้งาน.txt`.
        patch_sources.append((manual_src, "USER_MANUAL_TH.txt"))

    manifest_files: list[dict] = []
    for src, asset_name in patch_sources:
        dst = output_dir / asset_name
        shutil.copy2(src, dst)
        sha = updater.compute_sha256(dst)
        size = dst.stat().st_size
        encoded_name = quote(asset_name)
        manifest_files.append(
            {
                "name": src.name,
                "url": f"{dl_base}/{encoded_name}",
                "sha256": sha,
                "size": size,
            }
        )

    launcher_info: dict | None = None
    if launcher_src.is_file():
        launcher_dst = output_dir / launcher_src.name
        shutil.copy2(launcher_src, launcher_dst)
        launcher_info = {
            "name": launcher_src.name,
            "url": f"{dl_base}/{quote(launcher_src.name)}",
            "sha256": updater.compute_sha256(launcher_dst),
            "size": launcher_dst.stat().st_size,
        }

    manifest_payload: dict = {
        "app_name": APP_NAME,
        "version": clean_ver,
        "tag": tag,
        "released_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "notes": (notes or f"- อัปเดตเวอร์ชัน v{clean_ver}").strip(),
        "force_update": bool(force_update),
        "files": manifest_files,
    }
    if launcher_info is not None:
        manifest_payload["launcher"] = launcher_info

    # Self-verify generated manifest via updater parser
    updater.parse_manifest_payload(manifest_payload, install_dir=output_dir)

    manifest_json = json.dumps(manifest_payload, ensure_ascii=False, indent=2)
    _atomic_write_text(output_dir / "version.json", manifest_json)

    # Also write ready-to-ship launcher_config.json next to SocialManagerLauncher.exe
    if clean_repo:
        manifest_url = updater.normalize_manifest_url(clean_repo)
        _atomic_write_text(
            output_dir / updater.LAUNCHER_CONFIG_NAME,
            json.dumps(
                {
                    "manifest_url": manifest_url,
                    "auto_check": True,
                    "target_exe": updater.DEFAULT_TARGET_EXE,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )

    if web_dir is not None and web_dir.is_dir():
        _atomic_write_text(web_dir / "version.json", manifest_json)

    return manifest_payload


def _run_pyinstaller(spec_name: str) -> None:
    pyi = ROOT_DIR / ".venv" / "Scripts" / "pyinstaller.exe"
    cmd = [str(pyi) if pyi.is_file() else "pyinstaller", "--noconfirm", spec_name]
    res = subprocess.run(cmd, cwd=str(ROOT_DIR), check=False)
    if res.returncode != 0:
        raise RuntimeError(f"PyInstaller failed for {spec_name} (exit {res.returncode})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build & package GitHub Releases patch bundle for Social Manager."
    )
    parser.add_argument(
        "--repo",
        default="",
        help="GitHub repository in owner/repo format (e.g. myname/social-manager)",
    )
    parser.add_argument(
        "--base-url",
        default="",
        help="Optional custom HTTPS base URL instead of GitHub Releases",
    )
    parser.add_argument("--version", default=APP_VERSION, help="SemVer release version")
    parser.add_argument("--notes", default="", help="Release patch notes text")
    parser.add_argument("--notes-file", default="", help="Path to UTF-8 text file with patch notes")
    parser.add_argument("--force-update", action="store_true", help="Require users to update before launching")
    parser.add_argument("--skip-build", action="store_true", help="Skip PyInstaller build and use existing dist/*.exe")
    parser.add_argument("--output-dir", default=str(DEFAULT_RELEASE_DIR), help="Output release folder")
    args = parser.parse_args(argv)

    notes = args.notes.replace("\\n", "\n")
    if args.notes_file:
        notes = Path(args.notes_file).read_text(encoding="utf-8")

    if not args.skip_build:
        _run_pyinstaller("SocialManager.spec")
        _run_pyinstaller("SocialManagerLauncher.spec")

    out_dir = Path(args.output_dir).resolve()
    manifest = build_release_bundle(
        repo=args.repo,
        version_str=args.version,
        notes=notes,
        force_update=args.force_update,
        output_dir=out_dir,
        base_download_url=args.base_url,
    )
    print(f"Release v{manifest['version']} packaged in: {out_dir}")
    print("Files to upload to GitHub Release:")
    for item in sorted(out_dir.iterdir()):
        if item.is_file():
            print(f"  - {item.name} ({item.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
