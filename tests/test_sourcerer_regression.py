from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fontTools.ttLib import TTFont

from tests.fixture_loader import ensure_sourcerer_fixture
from kobofix import FontProcessor


REPO_ROOT = Path(__file__).resolve().parent.parent


def composite_count(font: TTFont) -> int:
    glyf = font["glyf"]
    return sum(1 for name in font.getGlyphOrder() if glyf[name].isComposite())


class SourcererRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture_font = ensure_sourcerer_fixture()
        if not cls.fixture_font.exists():
            raise unittest.SkipTest("Sourcerer fixture is unavailable")

    def test_kf_processing_flattens_sourcerer_composites(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            input_path = workdir / self.fixture_font.name
            shutil.copy2(self.fixture_font, input_path)

            source_font = TTFont(input_path)
            source_glyf = source_font["glyf"]

            for codepoint in (0x25, 0x0101, 0x1E47):
                glyph_name = source_font.getBestCmap()[codepoint]
                self.assertTrue(source_glyf[glyph_name].isComposite(), glyph_name)
            self.assertGreater(composite_count(source_font), 0)
            source_font.close()

            processor = FontProcessor(prefix="KF", line_percent=0)
            with mock.patch.object(FontProcessor, "simplify_outlines", return_value=True), \
                    mock.patch.object(FontProcessor, "clean_degenerate_contours", return_value=0), \
                    mock.patch.object(FontProcessor, "_validate_output_font", return_value=True):
                ok = processor.process_font(
                    kern_mode="skip",
                    font_path=str(input_path),
                    hint_mode="skip",
                    outline_mode="apply",
                )

            self.assertTrue(ok)
            output_path = workdir / "KF_Sourcerer-Regular.ttf"
            output_font = TTFont(output_path)
            output_glyf = output_font["glyf"]

            self.assertEqual(composite_count(output_font), 0)
            for codepoint in (0x25, 0x0101, 0x1E47):
                glyph_name = output_font.getBestCmap()[codepoint]
                self.assertFalse(output_glyf[glyph_name].isComposite(), glyph_name)


if __name__ == "__main__":
    unittest.main()
