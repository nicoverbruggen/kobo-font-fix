#!/usr/bin/env python3
"""
Validate fonts using ots-sanitize (OpenType Sanitizer).

On first run, downloads the latest ots release from GitHub for the current
operating system and caches the binary under .tools/. Subsequent runs reuse
the cached binary.
"""

import argparse
import io
import json
import logging
import os
import platform
import stat
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

OTS_REPO = "khaledhosny/ots"
TOOLS_DIR = Path(__file__).resolve().parent / ".tools"

# ots release assets are named ots-<version>-<platform>.zip
PLATFORM_ASSET = {
    "Linux": "Linux",
    "Darwin": "macOS",
    "Windows": "Windows",
}


def _platform_asset_key() -> str:
    system = platform.system()
    key = PLATFORM_ASSET.get(system)
    if not key:
        raise RuntimeError(f"Unsupported platform: {system}")
    return key


def _binary_name() -> str:
    return "ots-sanitize.exe" if platform.system() == "Windows" else "ots-sanitize"


def _find_binary(root: Path) -> Optional[Path]:
    for path in root.rglob(_binary_name()):
        if path.is_file():
            return path
    return None


def _fetch_latest_release() -> dict:
    url = f"https://api.github.com/repos/{OTS_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _ensure_ots() -> Path:
    """Return a path to ots-sanitize, downloading it on first use."""
    TOOLS_DIR.mkdir(exist_ok=True)

    cached = _find_binary(TOOLS_DIR)
    if cached:
        logger.debug(f"Using cached ots-sanitize at {cached}")
        return cached

    logger.info("ots-sanitize not cached; fetching latest release...")
    release = _fetch_latest_release()
    tag = release["tag_name"]
    key = _platform_asset_key()

    asset = next(
        (a for a in release["assets"] if a["name"].endswith(f"-{key}.zip")),
        None,
    )
    if not asset:
        raise RuntimeError(f"No {key} asset in ots release {tag}")

    size_kb = asset.get("size", 0) // 1024
    logger.info(f"  Downloading {asset['name']} ({size_kb} KB)...")
    with urllib.request.urlopen(asset["browser_download_url"], timeout=120) as resp:
        data = resp.read()

    extract_dir = TOOLS_DIR / f"ots-{tag}"
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(extract_dir)

    binary = _find_binary(extract_dir)
    if not binary:
        raise RuntimeError(f"ots-sanitize not found in extracted archive at {extract_dir}")

    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    logger.info(f"  Installed to {binary}")
    return binary


def validate_font(ots: Path, font_path: Path) -> Tuple[bool, str]:
    """Run ots-sanitize against a font. Returns (ok, combined_output)."""
    result = subprocess.run(
        [str(ots), str(font_path), os.devnull],
        capture_output=True,
        text=True,
    )
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate fonts using ots-sanitize (downloads on first run)."
    )
    parser.add_argument("fonts", nargs="+", help="Font files to validate (.ttf / .otf).")
    parser.add_argument("--verbose", action="store_true", help="Show debug output.")
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    try:
        ots = _ensure_ots()
    except Exception as e:
        logger.error(f"Failed to obtain ots-sanitize: {e}")
        sys.exit(1)

    missing: List[str] = []
    failures = 0
    checked = 0

    for font in args.fonts:
        path = Path(font)
        if not path.is_file():
            missing.append(font)
            continue

        checked += 1
        ok, output = validate_font(ots, path)
        status = "OK  " if ok else "FAIL"
        logger.info(f"[{status}] {font}")
        if output:
            for line in output.splitlines():
                logger.info(f"       {line}")
        if not ok:
            failures += 1

    logger.info("")
    logger.info("=" * 50)
    logger.info(f"Validated {checked - failures}/{checked} fonts.")
    if missing:
        logger.warning(f"Skipped {len(missing)} missing file(s):")
        for m in missing:
            logger.warning(f"  {m}")

    if failures or missing:
        sys.exit(1)


if __name__ == "__main__":
    main()
