from __future__ import annotations

import types
import unittest
import inspect
import tempfile
from pathlib import Path
from unittest import mock

from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables import ttProgram
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.ttGlyphPen import TTGlyphPen

from kobofix import FontMetadata, FontProcessor


class KobofixUnitTests(unittest.TestCase):
    def test_set_name_records_uses_utf16_for_unicode_platform(self) -> None:
        font = TTFont()
        name_table = newTable("name")
        name_table.names = []
        name_table.setName("Original", 1, 0, 3, 0x409)
        name_table.setName("Original", 1, 3, 1, 0x409)
        font["name"] = name_table

        FontProcessor._set_name_records(font, 1, "KF Readerly")

        unicode_record = font["name"].getName(1, 0, 3, 0x409)
        windows_record = font["name"].getName(1, 3, 1, 0x409)

        self.assertEqual(unicode_record.toUnicode(), "KF Readerly")
        self.assertEqual(windows_record.toUnicode(), "KF Readerly")
        self.assertEqual(unicode_record.string, "KF Readerly".encode("utf-16-be"))

    def test_extract_format2_pairs_includes_default_right_class(self) -> None:
        font = TTFont()
        font.setGlyphOrder([".notdef", "A", "B", "C"])

        processor = FontProcessor(prefix="KF", line_percent=0)
        subtable = types.SimpleNamespace(
            Coverage=types.SimpleNamespace(glyphs=["A"]),
            ClassDef1=types.SimpleNamespace(classDefs={}),
            ClassDef2=types.SimpleNamespace(classDefs={"B": 1}),
            Class1Record=[
                types.SimpleNamespace(
                    Class2Record=[
                        types.SimpleNamespace(
                            Value1=types.SimpleNamespace(XAdvance=-80),
                            Value2=None,
                        ),
                        types.SimpleNamespace(
                            Value1=types.SimpleNamespace(XAdvance=-40),
                            Value2=None,
                        ),
                    ]
                )
            ],
        )

        params = tuple(inspect.signature(processor._extract_format2_pairs).parameters)
        if len(params) == 2:
            pairs = processor._extract_format2_pairs(font, subtable)
        else:
            pairs = processor._extract_format2_pairs(subtable)

        self.assertEqual(pairs[("A", "B")], -40)
        self.assertEqual(pairs[("A", "C")], -80)

    def test_rename_font_updates_name_id_18_with_full_name(self) -> None:
        font = TTFont()
        name_table = newTable("name")
        name_table.names = []
        for platform_id, plat_enc_id, lang_id in ((1, 0, 0), (3, 1, 0x409)):
            name_table.setName("Readerly", 1, platform_id, plat_enc_id, lang_id)
            name_table.setName("Italic", 2, platform_id, plat_enc_id, lang_id)
            name_table.setName("Readerly Italic", 4, platform_id, plat_enc_id, lang_id)
            name_table.setName("Version 1.000", 3, platform_id, plat_enc_id, lang_id)
            name_table.setName("Readerly Ital", 18, platform_id, plat_enc_id, lang_id)
        font["name"] = name_table

        processor = FontProcessor(prefix="KF", line_percent=0)
        metadata = FontMetadata(
            family_name="Readerly",
            style_name="Italic",
            full_name="Readerly Italic",
            ps_name="KF_Readerly-Italic",
        )

        processor.rename_font(font, metadata)

        self.assertEqual(font["name"].getBestFullName(), "KF Readerly Italic")
        self.assertEqual(font["name"].getName(18, 1, 0, 0).toUnicode(), "KF Readerly Italic")

    def test_analyze_changes_handles_fonts_without_name_table(self) -> None:
        font = TTFont()
        processor = FontProcessor(prefix="KF", line_percent=0)
        metadata = FontMetadata(
            family_name="Readerly",
            style_name="Regular",
            full_name="Readerly",
            ps_name="KF_Readerly",
        )

        changes = processor._analyze_changes(
            font,
            "/tmp/Readerly-Regular.ttf",
            kern_mode="skip",
            metadata=metadata,
        )

        self.assertIn("Rename font to 'KF Readerly'", changes)

    def test_check_and_fix_panose_updates_expected_fields(self) -> None:
        font = TTFont()
        font["OS/2"] = types.SimpleNamespace(
            panose=types.SimpleNamespace(
                bFamilyType=2,
                bWeight=0,
                bLetterForm=0,
            )
        )

        processor = FontProcessor(prefix="KF", line_percent=0)
        processor.check_and_fix_panose(font, "/tmp/Readerly-BoldItalic.ttf")

        self.assertEqual(font["OS/2"].panose.bWeight, 8)
        self.assertEqual(font["OS/2"].panose.bLetterForm, 3)

    def test_update_weight_metadata_updates_os2_weight_class(self) -> None:
        font = TTFont()
        font["OS/2"] = types.SimpleNamespace(usWeightClass=400)

        processor = FontProcessor(prefix="KF", line_percent=0)
        processor.update_weight_metadata(font, "/tmp/Readerly-Bold.ttf")

        self.assertEqual(font["OS/2"].usWeightClass, 700)

    def test_resolve_family_name_strips_known_prefixes(self) -> None:
        font = TTFont()
        name_table = newTable("name")
        name_table.names = []
        name_table.setName("NV Readerly", 1, 3, 1, 0x409)
        font["name"] = name_table

        processor = FontProcessor(prefix="KF", line_percent=0)
        resolved = processor._resolve_family_name(font, new_name=None, remove_prefix=None)

        self.assertEqual(resolved, "Readerly")

    def test_glyph_priority_prefers_ascii_letters_digits_and_punctuation(self) -> None:
        cmap_reverse = {
            "A": ord("A"),
            "nine": ord("9"),
            "exclam": ord("!"),
            "space": ord(" "),
            "Aacute": 0x00C1,
            "Ccaron": 0x010C,
        }

        self.assertLess(
            FontProcessor._glyph_priority("A", cmap_reverse),
            FontProcessor._glyph_priority("space", cmap_reverse),
        )
        self.assertLess(
            FontProcessor._glyph_priority("nine", cmap_reverse),
            FontProcessor._glyph_priority("Aacute", cmap_reverse),
        )
        self.assertLess(
            FontProcessor._glyph_priority("exclam", cmap_reverse),
            FontProcessor._glyph_priority("Ccaron", cmap_reverse),
        )

    def test_glyph_priority_prefers_typographic_punctuation_over_extended_latin(self) -> None:
        cmap_reverse = {
            "quotedblleft": 0x201C,
            "quotedblright": 0x201D,
            "emdash": 0x2014,
            "ellipsis": 0x2026,
            "Aacute": 0x00C1,
            "Ccaron": 0x010C,
        }

        self.assertLess(
            FontProcessor._glyph_priority("quotedblleft", cmap_reverse),
            FontProcessor._glyph_priority("Aacute", cmap_reverse),
        )
        self.assertLess(
            FontProcessor._glyph_priority("emdash", cmap_reverse),
            FontProcessor._glyph_priority("Ccaron", cmap_reverse),
        )
        self.assertEqual(
            FontProcessor._glyph_priority("quotedblright", cmap_reverse),
            FontProcessor._glyph_priority("ellipsis", cmap_reverse),
        )

    def test_kf_noop_hinting_replaces_existing_hints_and_tables(self) -> None:
        pen = TTGlyphPen(None)
        pen.moveTo((0, 0))
        pen.lineTo((100, 0))
        pen.lineTo((100, 100))
        pen.lineTo((0, 100))
        pen.closePath()
        glyph = pen.glyph()
        original_program = ttProgram.Program()
        original_program.fromBytecode(b"\xb0\x01")
        glyph.program = original_program

        glyphs = {
            ".notdef": TTGlyphPen(None).glyph(),
            "A": glyph,
        }

        builder = FontBuilder(1000, isTTF=True)
        builder.setupGlyphOrder(list(glyphs))
        builder.setupGlyf(glyphs)
        builder.setupHorizontalMetrics({
            ".notdef": (500, 0),
            "A": (500, 0),
        })
        builder.setupHorizontalHeader(ascent=800, descent=-200)
        builder.setupMaxp()
        font = builder.font

        for tag in ("fpgm", "prep"):
            table = newTable(tag)
            table.program = ttProgram.Program()
            table.program.fromBytecode(b"\xb0\x00")
            font[tag] = table
        font["cvt "] = newTable("cvt ")
        font["cvt "].values = [0]

        removed = FontProcessor._strip_hinting_tables(font)
        changed = FontProcessor._add_noop_hints(font)

        self.assertEqual(removed, 3)
        self.assertEqual(changed, 1)
        self.assertNotIn("fpgm", font)
        self.assertNotIn("prep", font)
        self.assertNotIn("cvt ", font)
        self.assertEqual(font["glyf"]["A"].program.getBytecode(), FontProcessor._NOOP_BYTECODE)
        self.assertFalse(FontProcessor._font_has_meaningful_hints(font))
        self.assertFalse(FontProcessor._font_needs_noop_hints(font))
        self.assertEqual(font["maxp"].maxSizeOfInstructions, 1)

    def test_validate_output_font_fails_when_ots_is_missing(self) -> None:
        with self.assertLogs("kobofix", level="ERROR") as captured:
            with mock.patch.object(FontProcessor, "_find_available_ots", return_value=None):
                ok = FontProcessor._validate_output_font("/tmp/KF_Readerly-Regular.ttf")

        self.assertFalse(ok)
        self.assertIn("ots-sanitize is required", captured.output[0])

    def test_validate_output_font_uses_available_ots_binary(self) -> None:
        with mock.patch.object(FontProcessor, "_find_available_ots", return_value=Path("/usr/bin/ots-sanitize")):
            with mock.patch.object(FontProcessor, "_validate_font", return_value=(True, "File sanitized successfully!")) as run_validate:
                ok = FontProcessor._validate_output_font("/tmp/KF_Readerly-Regular.ttf")

        self.assertTrue(ok)
        run_validate.assert_called_once_with(
            Path("/usr/bin/ots-sanitize"),
            Path("/tmp/KF_Readerly-Regular.ttf"),
        )

    def test_flatten_composites_converts_components_to_simple_outlines(self) -> None:
        def rect_glyph(x_min: int, y_min: int, x_max: int, y_max: int):
            pen = TTGlyphPen(None)
            pen.moveTo((x_min, y_min))
            pen.lineTo((x_max, y_min))
            pen.lineTo((x_max, y_max))
            pen.lineTo((x_min, y_max))
            pen.closePath()
            return pen.glyph()

        component_pen = TTGlyphPen({"base": None, "mark": None})
        component_pen.addComponent("base", (1, 0, 0, 1, 0, 0))
        component_pen.addComponent("mark", (1, 0, 0, 1, 10, 20))

        glyphs = {
            ".notdef": TTGlyphPen(None).glyph(),
            "base": rect_glyph(0, 0, 100, 100),
            "mark": rect_glyph(0, 0, 20, 20),
            "base.mark": component_pen.glyph(),
        }

        builder = FontBuilder(1000, isTTF=True)
        builder.setupGlyphOrder(list(glyphs))
        builder.setupGlyf(glyphs)
        builder.setupHorizontalMetrics({
            ".notdef": (500, 0),
            "base": (500, 0),
            "mark": (0, 0),
            "base.mark": (500, 0),
        })
        builder.setupHorizontalHeader(ascent=800, descent=-200)
        builder.setupMaxp()
        font = builder.font

        self.assertTrue(font["glyf"]["base.mark"].isComposite())

        flattened = FontProcessor.flatten_composites(font)

        self.assertEqual(flattened, 1)
        self.assertFalse(font["glyf"]["base.mark"].isComposite())
        self.assertEqual(font["glyf"]["base.mark"].numberOfContours, 2)
        self.assertEqual(
            (
                font["glyf"]["base.mark"].xMin,
                font["glyf"]["base.mark"].yMin,
                font["glyf"]["base.mark"].xMax,
                font["glyf"]["base.mark"].yMax,
            ),
            (0, 0, 100, 100),
        )

    def test_process_otf_converts_to_ttf_and_removes_old_cff_names(self) -> None:
        glyph_order = [".notdef", "A"]

        empty_pen = T2CharStringPen(500, None)
        a_pen = T2CharStringPen(500, None)
        a_pen.moveTo((0, 0))
        a_pen.lineTo((400, 0))
        a_pen.lineTo((400, 600))
        a_pen.lineTo((0, 600))
        a_pen.closePath()

        charstrings = {
            ".notdef": empty_pen.getCharString(),
            "A": a_pen.getCharString(),
        }

        builder = FontBuilder(1000, isTTF=False)
        builder.setupGlyphOrder(glyph_order)
        builder.setupCharacterMap({ord("A"): "A"})
        builder.setupHorizontalMetrics({
            ".notdef": (500, 0),
            "A": (500, 0),
        })
        builder.setupHorizontalHeader(ascent=800, descent=-200)
        builder.setupNameTable({
            "familyName": "Old CFF Name",
            "styleName": "Regular",
            "uniqueFontIdentifier": "Old CFF Name Regular",
            "fullName": "Old CFF Name Regular",
            "psName": "OldCFFName-Regular",
            "version": "Version 1.000",
        })
        builder.setupOS2(
            sTypoAscender=800,
            sTypoDescender=-200,
            usWinAscent=800,
            usWinDescent=200,
        )
        builder.setupPost()
        builder.setupCFF(
            "OldCFFName-Regular",
            {
                "FullName": "Old CFF Name Regular",
                "FamilyName": "Old CFF Name",
                "Weight": "Regular",
            },
            charstrings,
            {},
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "Converted-Regular.otf"
            builder.save(input_path)

            processor = FontProcessor(prefix="KF", line_percent=0)
            with mock.patch.object(FontProcessor, "_validate_output_font", return_value=True):
                ok = processor.process_font(
                    kern_mode="skip",
                    font_path=str(input_path),
                    new_name="Converted Serif",
                    outline_mode="skip",
                )

            self.assertTrue(ok)
            output_path = Path(tmpdir) / "KF_Converted_Serif-Regular.ttf"
            output_font = TTFont(output_path)

            self.assertEqual(output_font.sfntVersion, "\x00\x01\x00\x00")
            self.assertIn("glyf", output_font)
            self.assertNotIn("CFF ", output_font)
            self.assertEqual(output_font["name"].getBestFamilyName(), "KF Converted Serif")
            self.assertEqual(output_font["name"].getBestFullName(), "KF Converted Serif")
            self.assertEqual(output_font["name"].getName(6, 3, 1, 0x0409).toUnicode(), "KF_Converted-Serif")

            retained_names = [
                record.toUnicode()
                for record in output_font["name"].names
                if "Old CFF Name" in record.toUnicode() or "OldCFFName" in record.toUnicode()
            ]
            self.assertEqual(retained_names, [])


if __name__ == "__main__":
    unittest.main()
