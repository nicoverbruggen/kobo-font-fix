from __future__ import annotations

import shutil
import urllib.request
import zipfile
from pathlib import Path


READERLY_ZIP_URL = "https://github.com/nicoverbruggen/readerly/releases/latest/download/Readerly.zip"
SOURCERER_ZIP_URL = "https://github.com/nicoverbruggen/sourcerer/releases/latest/download/Sourcerer.zip"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def ensure_readerly_fixtures() -> list[Path]:
    """Download and extract the Readerly font fixtures on first use."""
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

    zip_path = FIXTURES_DIR / "Readerly.zip"

    if not zip_path.exists():
        with urllib.request.urlopen(READERLY_ZIP_URL) as response, zip_path.open("wb") as output:
            shutil.copyfileobj(response, output)

    fonts = sorted(FIXTURES_DIR.glob("Readerly*.ttf"))
    if not fonts:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(FIXTURES_DIR)

    fonts = sorted(FIXTURES_DIR.glob("Readerly*.ttf"))
    if not fonts:
        raise RuntimeError("Readerly.zip exists but no .ttf files were extracted")

    return fonts


def ensure_sourcerer_fixture() -> Path:
    """Download and extract the Sourcerer regression fixture on first use."""
    sourcerer_dir = FIXTURES_DIR / "sourcerer"
    sourcerer_dir.mkdir(parents=True, exist_ok=True)

    zip_path = sourcerer_dir / "Sourcerer-latest.zip"
    font_path = sourcerer_dir / "Sourcerer-Regular.ttf"

    if not zip_path.exists():
        with urllib.request.urlopen(SOURCERER_ZIP_URL) as response, zip_path.open("wb") as output:
            shutil.copyfileobj(response, output)

    if not font_path.exists():
        with zipfile.ZipFile(zip_path) as archive:
            with archive.open("Sourcerer-Regular.ttf") as source, font_path.open("wb") as output:
                shutil.copyfileobj(source, output)

    if not font_path.exists():
        raise RuntimeError("Sourcerer fixture could not be extracted")

    return font_path
