from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fontTools.ttLib import TTFont

from kobofix import FontProcessor
from tests.fixture_loader import ensure_libron_fixtures


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "kobofix.py"


class LibronIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture_fonts = ensure_libron_fixtures()
        if not cls.fixture_fonts:
            raise unittest.SkipTest("Libron fixtures are unavailable")

    def _copy_fixture_fonts(self, workdir: Path) -> list[Path]:
        for font_path in self.fixture_fonts:
            shutil.copy2(font_path, workdir / font_path.name)
        return sorted(workdir.glob("*.ttf"))

    def _run_kobofix(self, *args: str) -> None:
        subprocess.run(
            [sys.executable, str(SCRIPT_PATH), *args],
            check=True,
            cwd=REPO_ROOT,
        )

    def test_custom_kf_run_creates_prefixed_fonts_with_kern_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            font_inputs = self._copy_fixture_fonts(workdir)
            self.assertTrue(font_inputs)

            self._run_kobofix(
                "--prefix",
                "KF",
                "--line-percent",
                "0",
                "--kern",
                "add-legacy-kern",
                "--outline",
                "skip",
                *[str(path) for path in font_inputs],
            )

            output_fonts = sorted(workdir.glob("KF_*.ttf"))
            self.assertEqual(len(output_fonts), len(font_inputs))

            for output_path in output_fonts:
                font = TTFont(output_path)
                self.assertIn("kern", font, output_path.name)

                pair_count = sum(
                    len(subtable.kernTable)
                    for subtable in font["kern"].kernTables
                    if hasattr(subtable, "kernTable")
                )
                self.assertGreater(pair_count, 0, output_path.name)

                family_name = font["name"].getBestFamilyName()
                self.assertIsNotNone(family_name)
                self.assertTrue(family_name.startswith("KF "), family_name)

    def test_nv_then_kf_replaces_prefix_instead_of_stacking_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            font_inputs = self._copy_fixture_fonts(workdir)
            self.assertTrue(font_inputs)

            self._run_kobofix(
                "--preset",
                "nv",
                "--line-percent",
                "0",
                *[str(path) for path in font_inputs],
            )

            nv_fonts = sorted(workdir.glob("NV_*.ttf"))
            self.assertEqual(len(nv_fonts), len(font_inputs))

            source_regular = workdir / "Libron-Regular.ttf"
            nv_regular = workdir / "NV_Libron-Regular.ttf"
            source_font = TTFont(source_regular)
            nv_font = TTFont(nv_regular)
            self.assertEqual(
                source_font["glyf"]["h"].program.getBytecode(),
                nv_font["glyf"]["h"].program.getBytecode(),
            )
            source_font.close()
            nv_font.close()

            self._run_kobofix(
                "--preset",
                "kf",
                "--outline",
                "skip",
                *[str(path) for path in nv_fonts],
            )

            kf_fonts = sorted(workdir.glob("KF_*.ttf"))
            self.assertEqual(len(kf_fonts), len(font_inputs))
            self.assertFalse(any(path.name.startswith("KF_NV_") for path in kf_fonts))

            for output_path in kf_fonts:
                font = TTFont(output_path)
                family_name = font["name"].getBestFamilyName()
                self.assertIsNotNone(family_name)
                self.assertTrue(family_name.startswith("KF "), family_name)
                self.assertFalse(family_name.startswith("KF NV "), family_name)

    def test_legacy_kern_only_removes_gpos_but_keeps_kern(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            source_path = next(path for path in self.fixture_fonts if path.name == "Libron-Regular.ttf")
            input_path = workdir / source_path.name
            shutil.copy2(source_path, input_path)

            self._run_kobofix(
                "--prefix",
                "KF",
                "--line-percent",
                "0",
                "--kern",
                "legacy-kern-only",
                "--outline",
                "skip",
                str(input_path),
            )

            output_path = workdir / "KF_Libron-Regular.ttf"
            font = TTFont(output_path)

            self.assertNotIn("GPOS", font)
            self.assertIn("kern", font)
            pair_count = sum(
                len(subtable.kernTable)
                for subtable in font["kern"].kernTables
                if hasattr(subtable, "kernTable")
            )
            self.assertGreater(pair_count, 0)

    def test_kf_outline_processing_replaces_meaningful_hints_with_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            source_path = next(path for path in self.fixture_fonts if path.name == "Libron-Regular.ttf")
            input_path = workdir / source_path.name
            shutil.copy2(source_path, input_path)

            self._run_kobofix(
                "--prefix",
                "KF",
                "--line-percent",
                "0",
                "--kern",
                "skip",
                "--outline",
                "apply",
                str(input_path),
            )

            output_path = workdir / "KF_Libron-Regular.ttf"
            font = TTFont(output_path)

            self.assertNotIn("fpgm", font)
            self.assertNotIn("prep", font)
            self.assertNotIn("cvt ", font)
            self.assertEqual(font["glyf"]["h"].program.getBytecode(), FontProcessor._NOOP_BYTECODE)

    def test_custom_name_updates_family_and_weight_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            source_path = next(path for path in self.fixture_fonts if path.name == "Libron-Bold.ttf")
            input_path = workdir / source_path.name
            shutil.copy2(source_path, input_path)

            self._run_kobofix(
                "--prefix",
                "KF",
                "--name",
                "Library Serif",
                "--line-percent",
                "0",
                "--kern",
                "skip",
                "--outline",
                "skip",
                str(input_path),
            )

            output_path = workdir / "KF_Library_Serif-Bold.ttf"
            font = TTFont(output_path)

            self.assertEqual(font["name"].getBestFamilyName(), "KF Library Serif")
            self.assertEqual(font["name"].getBestFullName(), "KF Library Serif Bold")
            self.assertEqual(font["OS/2"].usWeightClass, 700)
            self.assertEqual(font["OS/2"].panose.bWeight, 8)
            self.assertEqual(font["OS/2"].panose.bLetterForm, 2)


if __name__ == "__main__":
    unittest.main()
