from __future__ import annotations

import shutil
import urllib.request
import zipfile
from pathlib import Path


READERLY_ZIP_URL = "https://github.com/nicoverbruggen/readerly/releases/latest/download/Readerly.zip"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def ensure_readerly_fixtures() -> list[Path]:
    """Download and extract the Readerly font fixtures on first use."""
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

    zip_path = FIXTURES_DIR / "Readerly.zip"

    if not zip_path.exists():
        with urllib.request.urlopen(READERLY_ZIP_URL) as response, zip_path.open("wb") as output:
            shutil.copyfileobj(response, output)

    fonts = sorted(FIXTURES_DIR.glob("*.ttf"))
    if not fonts:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(FIXTURES_DIR)

    fonts = sorted(FIXTURES_DIR.glob("*.ttf"))
    if not fonts:
        raise RuntimeError("Readerly.zip exists but no .ttf files were extracted")

    return fonts
